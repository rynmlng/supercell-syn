#!/usr/bin/env python3
"""Phase 2: AI-powered storm chase forecast agent.

Runs a Claude Opus agentic loop that fetches and interprets HRRR model
imagery and SPC outlook data, then generates an annotated chase map and
posts it to X.
"""

import argparse
import base64
import json
import logging
import math
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from io import BytesIO

import anthropic
import requests
import tweepy
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, "chasebot.log")),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIR = os.path.join(PROJECT_DIR, "images", "chase")
RUNS_DIR = os.path.join(IMAGE_DIR, "runs")
LAST_RUN_DIR = os.path.join(IMAGE_DIR, "last_run")
os.makedirs(RUNS_DIR, exist_ok=True)
os.makedirs(LAST_RUN_DIR, exist_ok=True)

SPC_BASE = "https://www.spc.noaa.gov/products/outlook"
SPC_DAY1_GEOJSON = (
    "https://www.spc.noaa.gov/products/outlook/day1otlk_cat.nolyr.geojson"
)
SPC_HEADERS = {"User-Agent": "SupercellSynBot/1.0 (+https://x.com/SupercellSyn)"}
ENHANCED_PLUS_LABELS = {"ENH", "MDT", "HIGH"}

PIVOTAL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Pivotal Weather CONUS map bounds (from their page JS)
CONUS_LAT_MIN, CONUS_LAT_MAX = 21.0, 59.01
CONUS_LON_MIN, CONUS_LON_MAX = -129.0, -64.0

MAX_AGENT_TURNS = 30

# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------
_session = requests.Session()
_session.headers.update(PIVOTAL_HEADERS)


def _fetch_image_b64(url: str, extra_headers: dict | None = None) -> str | None:
    """Fetch an image URL and return base64-encoded bytes, or None on failure."""
    headers = dict(extra_headers or {})
    try:
        r = _session.get(url, headers=headers, timeout=30)
        if r.status_code == 404:
            log.warning("404: %s", url)
            return None
        r.raise_for_status()
        return base64.standard_b64encode(r.content).decode("utf-8")
    except requests.RequestException as exc:
        log.error("Failed to fetch %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Geo math
# ---------------------------------------------------------------------------
def _destination_point(
    lat: float, lon: float, bearing_deg: float, distance_miles: float
) -> tuple[float, float]:
    """Return (lat, lon) of destination given start, bearing, and distance."""
    R = 3958.8
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    b = math.radians(bearing_deg)
    d = distance_miles / R

    lat2 = math.asin(
        math.sin(lat_r) * math.cos(d) + math.cos(lat_r) * math.sin(d) * math.cos(b)
    )
    lon2 = lon_r + math.atan2(
        math.sin(b) * math.sin(d) * math.cos(lat_r),
        math.cos(d) - math.sin(lat_r) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def _latlon_to_pixel(lat: float, lon: float, img_w: int, img_h: int) -> tuple[int, int]:
    """Map lat/lon to pixel (x, y) using a linear CONUS approximation."""
    x = int((lon - CONUS_LON_MIN) / (CONUS_LON_MAX - CONUS_LON_MIN) * img_w)
    y = int((CONUS_LAT_MAX - lat) / (CONUS_LAT_MAX - CONUS_LAT_MIN) * img_h)
    return max(0, min(x, img_w - 1)), max(0, min(y, img_h - 1))


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
def _tool_get_available_runs(_inp: dict) -> dict:
    url = "https://www.pivotalweather.com/status_model.php?m=hrrr&s=1"
    try:
        r = _session.get(url, timeout=15)
        r.raise_for_status()
        runs = sorted(r.json(), key=lambda x: x["rh"], reverse=True)
    except Exception as exc:
        return {"error": str(exc)}

    # Walk down the list until we find a run whose fh=6 dew point image is actually
    # published — the status API can report a run before CDN images are available.
    for run in runs[:6]:
        probe_url = (
            f"https://m2o.pivotalweather.com/maps/models/hrrr"
            f"/{run['rh']}/006/sfctd-imp.conus.png"
        )
        try:
            probe = _session.head(probe_url, timeout=10)
            if probe.status_code == 200:
                log.info("Verified run %s (fh=6 image available)", run["rh"])
                return {
                    "latest_rh": run["rh"],
                    "max_fh": run["fh"],
                    "recent_runs": [
                        {"rh": x["rh"], "max_fh": x["fh"]} for x in runs[:6]
                    ],
                }
            log.warning(
                "Run %s fh=6 not yet available (%s) — trying older run",
                run["rh"],
                probe.status_code,
            )
        except Exception as exc:
            log.warning("Probe failed for run %s: %s", run["rh"], exc)

    return {"error": "No verified HRRR run found with fh=6 images available"}


def _tool_get_spc_outlook(_inp: dict) -> list:
    url = f"{SPC_BASE}/day1otlk.html"
    try:
        r = requests.get(url, headers=SPC_HEADERS, timeout=30)
        r.raise_for_status()
        matches = re.findall(r"show_tab\(['\"]otlk_(\d{4})['\"]\)", r.text)
        if not matches:
            return [
                {"type": "text", "text": "Error: no timestamp found on SPC Day 1 page."}
            ]
        # Pick the most recent issuance already published (≤ current UTC HHMM)
        now_hhmm = int(datetime.now(timezone.utc).strftime("%H%M"))
        published = [ts for ts in matches if int(ts) <= now_hhmm]
        ts = max(published) if published else max(matches)
        img_url = f"{SPC_BASE}/day1otlk_{ts}.png"
        b64 = _fetch_image_b64(img_url, extra_headers=SPC_HEADERS)
        if not b64:
            return [
                {
                    "type": "text",
                    "text": f"Error: could not download SPC image from {img_url}",
                }
            ]
        return [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64},
            },
            {
                "type": "text",
                "text": (
                    f"SPC Day 1 Convective Outlook (ts={ts}). "
                    "Risk categories: Marginal=green, Slight=yellow, Enhanced=orange, "
                    "Moderate=red, High=magenta. Note approximate center lat/lon of "
                    "Enhanced/Moderate/High risk areas."
                ),
            },
        ]
    except Exception as exc:
        return [{"type": "text", "text": f"Error fetching SPC outlook: {exc}"}]


def _save_daily_image(b64: str, name: str) -> None:
    """Save a base64 image to images/chase/last_run/ overwriting each run."""
    path = os.path.join(LAST_RUN_DIR, f"{name}.png")
    with open(path, "wb") as f:
        f.write(base64.b64decode(b64))
    log.info("Saved %s", path)


def _tool_get_dew_point(inp: dict) -> list:
    rh, fh = inp["rh"], inp["fh"]
    url = f"https://m2o.pivotalweather.com/maps/models/hrrr/{rh}/{fh:03d}/sfctd-imp.conus.png"
    b64 = _fetch_image_b64(url)
    if not b64:
        return [
            {
                "type": "text",
                "text": f"Error: dew point image unavailable for rh={rh} fh={fh}.",
            }
        ]
    _save_daily_image(b64, f"dew_point_fh{fh:02d}")
    return [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64},
        },
        {
            "type": "text",
            "text": (
                f"HRRR 2m AGL Dew Point — rh={rh}, fh={fh}h. "
                "Identify the dry line: sharp 20-30°F dewpoint drop over ~75 miles, "
                "transitioning from ≥60°F (humid/green) in the east to ≤40°F (arid/tan) "
                "in the west. Note the approximate lat/lon of the dry line."
            ),
        },
    ]


def _tool_get_reflectivity(inp: dict) -> list:
    rh, fh = inp["rh"], inp["fh"]
    url = f"https://m2o.pivotalweather.com/maps/models/hrrr/{rh}/{fh:03d}/refcmp.conus.png"
    b64 = _fetch_image_b64(url)
    if not b64:
        return [
            {
                "type": "text",
                "text": f"Error: reflectivity image unavailable for rh={rh} fh={fh}.",
            }
        ]
    _save_daily_image(b64, f"reflectivity_fh{fh:02d}")
    return [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64},
        },
        {
            "type": "text",
            "text": (
                f"HRRR Composite Reflectivity (dBZ) — rh={rh}, fh={fh}h. "
                "Look for high reflectivities (≥50 dBZ, warm colors) near the dry line. "
                "Large, isolated cells suggest supercell potential. "
                "Note the lat/lon of the most promising convective cores."
            ),
        },
    ]


def _tool_get_sounding(inp: dict) -> list:
    rh, fh = inp["rh"], inp["fh"]
    lat, lon = inp["lat"], inp["lon"]

    sounding_page_url = (
        f"https://www.pivotalweather.com/sounding.php"
        f"?m=hrrr&p=refcmp&rh={rh}&fh={fh}&r=conus"
        f"&lon={lon:.4f}&lat={lat:.4f}"
    )
    try:
        # Step 1: get sounding page to extract the server-generated token
        r = _session.get(
            sounding_page_url,
            headers={"Referer": "https://www.pivotalweather.com/"},
            timeout=30,
        )
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")
        token_div = soup.find(id="snd_token")
        if not token_div:
            return [
                {
                    "type": "text",
                    "text": f"Error: snd_token not found for lat={lat}, lon={lon}",
                }
            ]
        token = token_div.get("data-token", "")
        if not token:
            return [{"type": "text", "text": "Error: empty sounding token."}]

        # Step 2: call make_sounding.php to get the image filename
        make_url = (
            f"https://i1o.pivotalweather.com/make_sounding.php"
            f"?m=hrrr&rh={rh}&fh={fh}&t={token}&lat={lat:.4f}&lon={lon:.4f}"
        )
        r2 = _session.get(
            make_url,
            headers={"Referer": sounding_page_url},
            timeout=30,
        )
        r2.raise_for_status()

        root = ET.fromstring(r2.text)
        image_filename = root.get("image")
        if not image_filename:
            error_msg = root.get("error", "Unknown error from make_sounding.php")
            return [{"type": "text", "text": f"Sounding error: {error_msg}"}]

        snapped_lat = root.get("lat", str(lat))
        snapped_lon = root.get("lon", str(lon))

        # Step 3: fetch the sounding PNG
        img_url = f"https://i1o.pivotalweather.com/sounding_images/{image_filename}"
        b64 = _fetch_image_b64(img_url)
        if not b64:
            return [
                {
                    "type": "text",
                    "text": f"Error: failed to download sounding image {img_url}",
                }
            ]

        # Save to disk (enumerated, stale files cleaned at run start)
        global _sounding_counter
        _sounding_counter += 1
        _save_daily_image(b64, f"sounding_{_sounding_counter}")

        return [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64},
            },
            {
                "type": "text",
                "text": (
                    f"HRRR Sounding — rh={rh}, fh={fh}h, "
                    f"lat={snapped_lat}, lon={snapped_lon}. "
                    "Analyze: (1) hodograph for directional/speed shear and Bunkers Right "
                    "Storm Motion Vector (labeled 'RM' — note its direction in degrees and "
                    "speed in knots), (2) Skew-T for low-level jet at 850-925 hPa, "
                    "(3) overall supercell potential."
                ),
            },
        ]
    except Exception as exc:
        log.exception("Sounding fetch failed for lat=%s lon=%s", lat, lon)
        return [{"type": "text", "text": f"Error fetching sounding: {exc}"}]


MIN_DEW_POINT_FRAMES = 3


def _tool_generate_annotated_map(inp: dict) -> dict:
    import glob as _glob

    dew_point_files = _glob.glob(os.path.join(LAST_RUN_DIR, "dew_point_*.png"))
    if len(dew_point_files) < MIN_DEW_POINT_FRAMES:
        return {
            "error": (
                f"Insufficient dew point coverage: only {len(dew_point_files)} frame(s) fetched, "
                f"minimum {MIN_DEW_POINT_FRAMES} required. "
                "Fetch more dew point forward hours before generating the map."
            )
        }

    hatch_lat = inp["hatch_area_lat"]
    hatch_lon = inp["hatch_area_lon"]
    vector_dir = inp["storm_vector_direction_deg"]
    vector_spd = inp["storm_vector_speed_knots"]
    rh = inp["rh"]
    fh = inp["fh"]

    # Compute positioning location: scale distance with BRM speed (×7 factor targets
    # ~7 hrs of lead time), clamped to 75–250 miles.
    pos_dist = max(75, min(vector_spd * 7, 250))
    pos_lat, pos_lon = _destination_point(hatch_lat, hatch_lon, vector_dir, pos_dist)

    # Download reflectivity chart as base map
    base_url = f"https://m2o.pivotalweather.com/maps/models/hrrr/{rh}/{fh:03d}/refcmp.conus.png"
    b64 = _fetch_image_b64(base_url)
    if not b64:
        return {"error": f"Failed to download base map: {base_url}"}

    img = Image.open(BytesIO(base64.b64decode(b64))).convert("RGBA")
    w, h = img.size
    draw = ImageDraw.Draw(img)

    hatch_px = _latlon_to_pixel(hatch_lat, hatch_lon, w, h)
    pos_px = _latlon_to_pixel(pos_lat, pos_lon, w, h)

    # Storm vector indicator (orange arrow from hatch area)
    sv_tip = _destination_point(hatch_lat, hatch_lon, vector_dir, 120)
    sv_px = _latlon_to_pixel(sv_tip[0], sv_tip[1], w, h)
    draw.line([hatch_px, sv_px], fill=(255, 165, 0, 200), width=2)

    # Line from hatch to positioning
    draw.line([hatch_px, pos_px], fill=(180, 180, 255, 160), width=2)

    # Hatch area marker — red circle with X
    r1 = 16
    draw.ellipse(
        [hatch_px[0] - r1, hatch_px[1] - r1, hatch_px[0] + r1, hatch_px[1] + r1],
        outline=(255, 60, 60, 255),
        width=3,
    )
    o = r1 - 4
    draw.line(
        [hatch_px[0] - o, hatch_px[1] - o, hatch_px[0] + o, hatch_px[1] + o],
        fill=(255, 60, 60, 255),
        width=2,
    )
    draw.line(
        [hatch_px[0] + o, hatch_px[1] - o, hatch_px[0] - o, hatch_px[1] + o],
        fill=(255, 60, 60, 255),
        width=2,
    )

    # Positioning marker — fuchsia filled circle
    FUCHSIA = (255, 0, 255, 255)
    r2 = 16
    draw.ellipse(
        [pos_px[0] - r2, pos_px[1] - r2, pos_px[0] + r2, pos_px[1] + r2],
        outline=FUCHSIA,
        width=3,
    )
    draw.ellipse(
        [pos_px[0] - 6, pos_px[1] - 6, pos_px[0] + 6, pos_px[1] + 6],
        fill=FUCHSIA,
    )

    # Fonts
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 15)
        font_sm = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 12)
    except OSError:
        font = ImageFont.load_default()
        font_sm = font

    # Labels
    LABEL_BG = (255, 255, 255, 204)  # semi-transparent white (~80% opaque)
    PAD = 2

    def _text_with_bg(pos, text, color, font):
        bbox = draw.textbbox(pos, text, font=font)
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rectangle(
            [bbox[0] - PAD, bbox[1] - PAD, bbox[2] + PAD, bbox[3] + PAD],
            fill=LABEL_BG,
        )
        img.alpha_composite(overlay)
        draw.text(pos, text, fill=color, font=font)

    def _label_right(px, text, color, sub=None):
        ox = px[0] + 20
        _text_with_bg((ox, px[1] - 10), text, color, font)
        if sub:
            _text_with_bg((ox, px[1] + 6), sub, color, font_sm)

    def _label_below(px, text, color, sub=None):
        tw = draw.textlength(text, font=font)
        tx = px[0] - tw // 2
        _text_with_bg((tx, px[1] + 22), text, color, font)
        if sub:
            sw = draw.textlength(sub, font=font_sm)
            _text_with_bg((px[0] - sw // 2, px[1] + 38), sub, color, font_sm)

    _label_below(
        hatch_px,
        "HATCH AREA",
        (255, 60, 60, 255),
        f"{hatch_lat:.2f}, {hatch_lon:.2f}",
    )
    _label_right(
        pos_px,
        "POSITION HERE",
        FUCHSIA,
        f"{pos_lat:.2f}, {pos_lon:.2f}",
    )

    # Title bar
    run_dt = datetime.strptime(rh, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    valid_dt = run_dt + timedelta(hours=fh)
    ct_offset = timedelta(hours=-5)  # CDT (Mar–Nov)
    valid_ct = valid_dt + ct_offset
    vector_dir_from = (vector_dir + 180) % 360
    title = (
        f"Supercell Syn Chase Forecast — {valid_dt.strftime('%Y-%m-%d')}   "
        f"Valid: {valid_dt.strftime('%HZ')} / {valid_ct.strftime('%-I%p CT')}   "
        f"Storm Motion: {vector_dir_from:.0f}° @ {vector_spd:.0f} kts"
    )
    draw.rectangle([0, 0, w, 26], fill=(0, 0, 0, 200))
    draw.text((8, 5), title, fill=(255, 255, 255, 255), font=font_sm)

    # Legend
    ly = h - 58
    draw.rectangle([8, ly, 210, h - 8], fill=(0, 0, 0, 170))
    draw.ellipse([16, ly + 8, 34, ly + 26], outline=(255, 60, 60, 255), width=2)
    draw.text((40, ly + 10), "Hatch Area", fill=(255, 60, 60, 255), font=font_sm)
    draw.ellipse([16, ly + 30, 34, ly + 48], outline=FUCHSIA, width=2)
    draw.text((40, ly + 32), "Position Location", fill=FUCHSIA, font=font_sm)

    # Save
    date_label = RETRO_DATE or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = os.path.join(RUNS_DIR, f"chase_{date_label}.png")
    img.convert("RGB").save(out_path, "PNG")
    log.info("Saved annotated map: %s", out_path)

    return {
        "image_path": out_path,
        "hatch_area": {"lat": hatch_lat, "lon": hatch_lon},
        "positioning_location": {"lat": round(pos_lat, 4), "lon": round(pos_lon, 4)},
        "storm_vector": {"direction_deg": vector_dir, "speed_knots": vector_spd},
    }


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "get_available_runs",
        "description": (
            "Get the latest available HRRR model run time (rh) and maximum published "
            "forward hour (max_fh). Call this first before any other tool."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_spc_outlook",
        "description": (
            "Fetch the NOAA SPC Day 1 Convective Outlook image. Shows risk categories: "
            "Marginal (green), Slight (yellow), Enhanced (orange), Moderate (red), "
            "High (magenta). Use this to identify today's primary risk regions."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_dew_point",
        "description": (
            "Fetch the Pivotal Weather HRRR 2m AGL Dew Point chart. "
            "Use this to identify the dry line: a sharp 20-30°F dewpoint drop over "
            "~75 miles, transitioning from ≥60°F humid air in the east to ≤40°F "
            "arid air in the west. Scan multiple forward hours to find the best window."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "rh": {
                    "type": "string",
                    "description": "Model run time, e.g. '2026032219'",
                },
                "fh": {
                    "type": "integer",
                    "description": "Forward hour (0–18)",
                },
            },
            "required": ["rh", "fh"],
        },
    },
    {
        "name": "get_reflectivity",
        "description": (
            "Fetch the Pivotal Weather HRRR Composite Reflectivity (dBZ) chart. "
            "Use this to find high-reflectivity convective cores (≥50 dBZ) near "
            "the dry line. Large, isolated cells indicate supercell potential."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "rh": {"type": "string", "description": "Model run time"},
                "fh": {"type": "integer", "description": "Forward hour (0–18)"},
            },
            "required": ["rh", "fh"],
        },
    },
    {
        "name": "get_sounding",
        "description": (
            "Fetch the HRRR sounding chart (Skew-T Log-P + hodograph) for a specific "
            "lat/lon. Analyze: (1) hodograph for directional/speed shear and the "
            "Bunkers Right Storm Motion Vector ('RM' — note direction in degrees and "
            "speed in knots), (2) Skew-T for low-level jet at 850-925 hPa. "
            "Call this for 2-3 candidate locations near the convective cores."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "rh": {"type": "string", "description": "Model run time"},
                "fh": {"type": "integer", "description": "Forward hour (0–18)"},
                "lat": {
                    "type": "number",
                    "description": "Latitude in decimal degrees (e.g. 35.5)",
                },
                "lon": {
                    "type": "number",
                    "description": "Longitude in decimal degrees (e.g. -97.5)",
                },
            },
            "required": ["rh", "fh", "lat", "lon"],
        },
    },
    {
        "name": "save_analysis_report",
        "description": (
            "Save the full analysis report (markdown) to a file for review. "
            "Call this with your complete written analysis immediately before "
            "your final message. Your final message must then be ONLY the post caption."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report": {
                    "type": "string",
                    "description": "The full markdown analysis report to save",
                },
            },
            "required": ["report"],
        },
    },
    {
        "name": "generate_annotated_map",
        "description": (
            "Generate the final annotated chase forecast map and save it as a PNG. "
            "Provide the hatch area coordinates and Bunkers Right Storm Motion Vector. "
            "The tool computes the positioning location (BRM speed × 7, clamped 75–250 miles "
            "from the hatch area in the storm vector direction) and draws both points "
            "on the HRRR reflectivity chart. Returns the saved image path."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hatch_area_lat": {
                    "type": "number",
                    "description": "Latitude of the hatch area center",
                },
                "hatch_area_lon": {
                    "type": "number",
                    "description": "Longitude of the hatch area center",
                },
                "storm_vector_direction_deg": {
                    "type": "number",
                    "description": (
                        "Bunkers Right Storm Motion direction in degrees "
                        "(direction the storm moves TOWARD: 0=N, 90=E, 180=S, 270=W)"
                    ),
                },
                "storm_vector_speed_knots": {
                    "type": "number",
                    "description": "Bunkers Right Storm Motion speed in knots",
                },
                "rh": {
                    "type": "string",
                    "description": "Model run time used for the base map",
                },
                "fh": {
                    "type": "integer",
                    "description": "Forward hour used for the base map",
                },
            },
            "required": [
                "hatch_area_lat",
                "hatch_area_lon",
                "storm_vector_direction_deg",
                "storm_vector_speed_knots",
                "rh",
                "fh",
            ],
        },
    },
]


def _tool_save_analysis_report(inp: dict) -> dict:
    date_label = RETRO_DATE or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report_path = os.path.join(RUNS_DIR, f"chase_{date_label}_report.txt")
    with open(report_path, "w") as f:
        f.write(inp["report"])
    log.info("Saved analysis report: %s", report_path)
    return {"saved": report_path}


_TOOL_FN = {
    "get_available_runs": _tool_get_available_runs,
    "get_spc_outlook": _tool_get_spc_outlook,
    "get_dew_point": _tool_get_dew_point,
    "get_reflectivity": _tool_get_reflectivity,
    "get_sounding": _tool_get_sounding,
    "save_analysis_report": _tool_save_analysis_report,
    "generate_annotated_map": _tool_generate_annotated_map,
}


# ---------------------------------------------------------------------------
# Retroactive mode — herbie + AWS S3 (HRRR archive), cartopy, metpy
# ---------------------------------------------------------------------------
RETRO_DATE: str | None = None  # "YYYY-MM-DD", set by --date CLI argument
_sounding_counter: int = (
    0  # increments each time a sounding is saved; reset at run start
)


def _retro_run_dt() -> datetime:
    """Return the 12Z naive UTC datetime for RETRO_DATE (herbie requires tz-naive)."""
    return datetime.strptime(RETRO_DATE, "%Y-%m-%d").replace(hour=12)


def _retro_get_available_runs(_inp: dict) -> dict:
    rh = RETRO_DATE.replace("-", "") + "12"
    return {
        "latest_rh": rh,
        "max_fh": 18,
        "recent_runs": [{"rh": rh, "max_fh": 18}],
        "note": f"Retroactive mode: using {RETRO_DATE} 12Z HRRR from AWS S3 via herbie",
    }


def _retro_get_spc_outlook(_inp: dict) -> list:
    date_compact = RETRO_DATE.replace("-", "")
    # Check for a locally provided image first
    retro_dir = os.path.join(PROJECT_DIR, "images", "chase", "retro")
    for ext in ("png", "gif"):
        local_path = os.path.join(retro_dir, f"spc_day1otlk_{date_compact}_1200.{ext}")
        if os.path.exists(local_path):
            log.info("Loading SPC outlook from local file: %s", local_path)
            with open(local_path, "rb") as f:
                b64 = base64.standard_b64encode(f.read()).decode("utf-8")
            media_type = "image/png" if ext == "png" else "image/gif"
            return [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64},
                },
                {
                    "type": "text",
                    "text": (
                        f"SPC Day 1 Convective Outlook — {RETRO_DATE} 1200Z issuance. "
                        "Risk categories: Marginal=green, Slight=yellow, Enhanced=orange, "
                        "Moderate=red, High=magenta. Note approximate center lat/lon of "
                        "Enhanced/Moderate/High risk areas."
                    ),
                },
            ]
    return [
        {
            "type": "text",
            "text": (
                f"Error: SPC outlook not found for {RETRO_DATE}. "
                f"Place it at {retro_dir}/spc_day1otlk_{date_compact}_1200.png"
            ),
        }
    ]


def _herbie_conus_map(
    ds,
    var: str,
    run_dt: datetime,
    fh: int,
    title: str,
    colormap: str,
    levels,
    cbar_label: str,
) -> str | None:
    """Render a 2D herbie Dataset field as a CONUS cartopy map. Returns base64 PNG or None."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import cartopy.crs as ccrs
        import cartopy.feature as cfeature
        import matplotlib.pyplot as plt

        lats = ds.latitude.values
        lons = ds.longitude.values
        data = ds[var].values.squeeze()  # (y, x) after squeezing singleton time dims

        proj = ccrs.LambertConformal(central_longitude=-96, central_latitude=37.5)
        fig = plt.figure(figsize=(14, 8), dpi=100)
        ax = fig.add_subplot(1, 1, 1, projection=proj)
        ax.set_extent([-122, -67, 22, 50], crs=ccrs.PlateCarree())
        ax.add_feature(cfeature.STATES, linewidth=0.5, edgecolor="black")
        ax.add_feature(cfeature.COASTLINE, linewidth=0.5)

        cf = ax.contourf(
            lons,
            lats,
            data,
            levels=levels,
            cmap=colormap,
            extend="both",
            transform=ccrs.PlateCarree(),
        )
        plt.colorbar(
            cf, ax=ax, orientation="horizontal", pad=0.03, shrink=0.8, label=cbar_label
        )

        valid_dt = run_dt + timedelta(hours=fh)
        ax.set_title(
            f"{title} — Init: {run_dt:%Y-%m-%d %HZ}  fh={fh}h  Valid: {valid_dt:%Y-%m-%d %HZ}"
        )

        buf = BytesIO()
        plt.savefig(buf, format="png", bbox_inches="tight", dpi=100)
        plt.close(fig)
        buf.seek(0)
        return base64.standard_b64encode(buf.read()).decode("utf-8")
    except Exception:
        log.exception("Herbie map render failed")
        return None


def _retro_get_dew_point(inp: dict) -> list:
    fh = inp["fh"]
    run_dt = _retro_run_dt()
    try:
        import numpy as np
        from herbie import Herbie

        H = Herbie(run_dt, model="hrrr", product="sfc", fxx=fh, verbose=False)
        ds = H.xarray("DPT:2 m above ground")
        var = list(ds.data_vars)[0]
        ds[var] = (ds[var] - 273.15) * 9 / 5 + 32  # K → °F

        b64 = _herbie_conus_map(
            ds,
            var,
            run_dt,
            fh,
            title="HRRR 2m Dew Point",
            colormap="BrBG",
            levels=np.arange(-20, 80, 5),
            cbar_label="Dew Point (°F)",
        )
        if not b64:
            return [
                {
                    "type": "text",
                    "text": f"Error rendering dew point map for {RETRO_DATE} fh={fh}.",
                }
            ]
        return [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64},
            },
            {
                "type": "text",
                "text": (
                    f"HRRR 2m Dew Point — {RETRO_DATE} 12Z run, fh={fh}h. "
                    "Identify the dry line: sharp 20-30°F dewpoint drop over ~75 miles, "
                    "transitioning from ≥60°F humid air to ≤40°F arid air. "
                    "Note the approximate lat/lon of the dry line."
                ),
            },
        ]
    except Exception as exc:
        log.exception("Retro dew point failed for %s fh=%d", RETRO_DATE, fh)
        return [{"type": "text", "text": f"Error fetching retro dew point: {exc}"}]


def _retro_get_reflectivity(inp: dict) -> list:
    fh = inp["fh"]
    run_dt = _retro_run_dt()
    try:
        import numpy as np
        from herbie import Herbie

        H = Herbie(run_dt, model="hrrr", product="sfc", fxx=fh, verbose=False)
        ds = H.xarray("REFC:entire atmosphere")
        var = list(ds.data_vars)[0]

        b64 = _herbie_conus_map(
            ds,
            var,
            run_dt,
            fh,
            title="HRRR Composite Reflectivity",
            colormap="gist_ncar",
            levels=np.arange(0, 75, 5),
            cbar_label="Composite Reflectivity (dBZ)",
        )
        if not b64:
            return [
                {
                    "type": "text",
                    "text": f"Error rendering reflectivity map for {RETRO_DATE} fh={fh}.",
                }
            ]
        return [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64},
            },
            {
                "type": "text",
                "text": (
                    f"HRRR Composite Reflectivity (dBZ) — {RETRO_DATE} 12Z run, fh={fh}h. "
                    "Look for high reflectivities (≥50 dBZ) near the dry line. "
                    "Large, isolated cells suggest supercell potential. "
                    "Note the lat/lon of the most promising convective cores."
                ),
            },
        ]
    except Exception as exc:
        log.exception("Retro reflectivity failed for %s fh=%d", RETRO_DATE, fh)
        return [{"type": "text", "text": f"Error fetching retro reflectivity: {exc}"}]


def _retro_get_sounding(inp: dict) -> list:
    fh = inp["fh"]
    lat, lon = float(inp["lat"]), float(inp["lon"])
    run_dt = _retro_run_dt()
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import metpy.calc as mpcalc
        import numpy as np
        from herbie import Herbie
        from metpy.plots import Hodograph, SkewT
        from metpy.units import units

        H = Herbie(run_dt, model="hrrr", product="prs", fxx=fh, verbose=False)
        ds_T = H.xarray("TMP:")
        ds_RH = H.xarray("RH:")
        ds_U = H.xarray("UGRD:")
        ds_V = H.xarray("VGRD:")
        ds_Z = H.xarray("HGT:")

        # H.xarray() returns a list for broad PRS searches; extract the isobaric dataset
        def _isobaric(result):
            datasets = result if isinstance(result, list) else [result]
            return next(
                (
                    d
                    for d in datasets
                    if any(
                        "isobaric" in dim for dim in list(d.data_vars.values())[0].dims
                    )
                ),
                datasets[0],
            )

        ds_T = _isobaric(ds_T)

        # Locate nearest grid point; latitude/longitude are 2D coords (y, x)
        lats_2d = ds_T.latitude.values
        lons_2d = ds_T.longitude.values
        # Collapse any leading singleton dims (e.g. time) to get (y, x)
        while lats_2d.ndim > 2:
            lats_2d = lats_2d[0]
            lons_2d = lons_2d[0]
        # HRRR grid uses 0–360 longitude; convert input lon to match
        lon_adj = lon % 360
        dist = (lats_2d - lat) ** 2 + (lons_2d - lon_adj) ** 2
        iy, ix = np.unravel_index(dist.argmin(), dist.shape)
        snapped_lat = float(lats_2d[iy, ix])
        snapped_lon = float(((lons_2d[iy, ix] + 180) % 360) - 180)  # report as -180/180

        def _profile(result):
            """Return (pressure_hPa_array, 1d_profile_at_iy_ix) from H.xarray() result."""
            ds = _isobaric(result)
            var = list(ds.data_vars)[0]
            da = ds[var]
            p_dim = next(
                (
                    d
                    for d in da.dims
                    if d not in ("y", "x", "time", "step", "valid_time")
                ),
                da.dims[0],
            )
            p_vals = (
                ds[p_dim].values if p_dim in ds.coords else np.arange(da.sizes[p_dim])
            )
            arr = da.values
            while arr.ndim > 3:  # collapse time → (pressure, y, x)
                arr = arr[0]
            return p_vals, arr[:, iy, ix]

        p_T, T_K = _profile(ds_T)
        p_RH, RH_pct = _profile(ds_RH)
        p_U, U_ms = _profile(ds_U)
        p_V, V_ms = _profile(ds_V)
        p_Z, Z_m = _profile(ds_Z)

        # Sort all profiles by descending pressure (surface → top)
        si_T = np.argsort(p_T)[::-1]
        si_RH = np.argsort(p_RH)[::-1]
        si_U = np.argsort(p_U)[::-1]
        si_V = np.argsort(p_V)[::-1]
        si_Z = np.argsort(p_Z)[::-1]

        p = p_T[si_T] * units("hPa")
        T_C = (T_K[si_T] - 273.15) * units("degC")
        RH = RH_pct[si_RH] * units("percent")
        u = (U_ms[si_U] * units("m/s")).to(units("knots"))
        v = (V_ms[si_V] * units("m/s")).to(units("knots"))
        z = Z_m[si_Z] * units("meter")

        Td = mpcalc.dewpoint_from_relative_humidity(T_C, RH)

        # Bunkers Right Storm Motion
        rm_u = rm_v = rm_spd = rm_dir_to = rm_dir_from = 0.0
        try:
            rm, lm, mean = mpcalc.bunkers_storm_motion(p, u, v, z)
            rm_u = float(rm[0].to(units("knots")).magnitude)
            rm_v = float(rm[1].to(units("knots")).magnitude)
            rm_spd = float(np.sqrt(rm_u**2 + rm_v**2))
            rm_dir_to = float((np.degrees(np.arctan2(rm_u, rm_v)) + 360) % 360)
            rm_dir_from = float((rm_dir_to + 180) % 360)  # wind convention (FROM)
        except Exception as be:
            log.warning("Bunkers calculation failed: %s", be)

        # Plot Skew-T + hodograph
        fig = plt.figure(figsize=(11, 8.5), dpi=120)
        skew = SkewT(fig, rotation=45, rect=(0.05, 0.05, 0.60, 0.90))

        skew.plot(p, T_C, "r", linewidth=1.5)
        skew.plot(p, Td, "g", linewidth=1.5)
        skew.plot_barbs(p, u, v, length=6, linewidth=0.8)
        skew.ax.set_ylim(1000, 100)
        skew.ax.set_xlim(-40, 50)
        skew.plot_dry_adiabats(alpha=0.2, linewidth=0.6)
        skew.plot_moist_adiabats(alpha=0.2, linewidth=0.6)
        skew.plot_mixing_lines(alpha=0.2, linewidth=0.6)
        skew.ax.set_xlabel("Temperature (°C)", fontsize=9)
        skew.ax.set_ylabel("Pressure (hPa)", fontsize=9)

        ax_hodo = fig.add_axes([0.67, 0.50, 0.30, 0.38])
        h_plot = Hodograph(ax_hodo, component_range=80)
        h_plot.add_grid(increment=20)
        try:
            h_plot.plot_colormapped(u, v, p)
        except Exception:
            h_plot.plot(u, v, color="gray")
        ax_hodo.set_title("Hodograph", fontsize=9)

        # Kinematic parameters box (lower right, Pivotal Weather style)
        try:
            srh_pos, srh_neg, srh_tot = mpcalc.storm_relative_helicity(
                z, u, v, depth=3000 * units("meter"), storm_u=rm[0], storm_v=rm[1]
            )
            srh_str = f"{srh_tot.magnitude:.0f} m²/s²"
        except Exception:
            srh_str = "N/A"

        params_lines = [
            f"Bunkers RM:  {rm_dir_from:.0f}° / {rm_spd:.0f} kt",
            f"0-3 km SRH:  {srh_str}",
        ]
        params_text = "\n".join(params_lines)
        fig.text(
            0.67,
            0.44,
            params_text,
            fontsize=9,
            family="monospace",
            verticalalignment="top",
            bbox=dict(
                boxstyle="round,pad=0.4",
                facecolor="lightyellow",
                edgecolor="gray",
                alpha=0.9,
            ),
        )

        valid_dt = run_dt + timedelta(hours=fh)
        skew.ax.set_title(
            f"HRRR Sounding — {RETRO_DATE} 12Z  fh={fh}h  "
            f"Valid: {valid_dt:%Y-%m-%d %HZ}\n"
            f"Lat: {snapped_lat:.2f}°N   Lon: {snapped_lon:.2f}°",
            fontsize=10,
        )

        buf = BytesIO()
        plt.savefig(buf, format="png", bbox_inches="tight", dpi=100)
        plt.close(fig)

        # Save to disk for review
        retro_dir = os.path.join(PROJECT_DIR, "images", "chase", "retro")
        os.makedirs(retro_dir, exist_ok=True)
        sounding_path = os.path.join(
            retro_dir,
            f"hrrr_{RETRO_DATE.replace('-', '')}_{run_dt.strftime('%Hz')}_sounding_"
            f"{snapped_lat:.2f}_{snapped_lon:.2f}_fh{fh:02d}.png",
        )
        with open(sounding_path, "wb") as f:
            f.write(buf.getvalue())
        log.info("Saved sounding: %s", sounding_path)

        buf.seek(0)
        b64 = base64.standard_b64encode(buf.read()).decode("utf-8")

        rm_text = (
            f"Bunkers Right Motion: {rm_dir_from:.0f}°/{rm_spd:.0f}kt (storm moves TOWARD {rm_dir_to:.0f}°). "
            if rm_spd > 1
            else ""
        )
        return [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64},
            },
            {
                "type": "text",
                "text": (
                    f"HRRR Sounding — {RETRO_DATE} 12Z run, fh={fh}h, "
                    f"lat={snapped_lat:.2f}, lon={snapped_lon:.2f}. "
                    f"{rm_text}"
                    "Analyze hodograph for directional/speed shear, "
                    "Skew-T for low-level jet at 850-925 hPa, and overall supercell potential."
                ),
            },
        ]
    except Exception as exc:
        log.exception(
            "Retro sounding failed for %s fh=%d lat=%s lon=%s", RETRO_DATE, fh, lat, lon
        )
        return [{"type": "text", "text": f"Error fetching retro sounding: {exc}"}]


def _retro_generate_annotated_map(inp: dict) -> dict:
    hatch_lat = inp["hatch_area_lat"]
    hatch_lon = inp["hatch_area_lon"]
    vector_dir = inp["storm_vector_direction_deg"]
    vector_spd = inp["storm_vector_speed_knots"]
    fh = inp["fh"]

    pos_dist = max(75, min(vector_spd * 7, 250))
    pos_lat, pos_lon = _destination_point(hatch_lat, hatch_lon, vector_dir, pos_dist)
    run_dt = _retro_run_dt()
    rh = RETRO_DATE.replace("-", "") + "12"

    out_path = os.path.join(RUNS_DIR, f"chase_{RETRO_DATE}.png")

    # Try Pivotal Weather PNG first (same path as live run)
    base_url = f"https://m2o.pivotalweather.com/maps/models/hrrr/{rh}/{fh:03d}/refcmp.conus.png"
    b64 = _fetch_image_b64(base_url)
    if b64:
        # Reuse the same PIL overlay logic as the live annotated map
        inp_live = dict(inp)
        inp_live["rh"] = rh
        result = _tool_generate_annotated_map(inp_live)
        # Rename the output file to the retro naming convention
        live_path = result.get("image_path", "")
        if live_path and os.path.exists(live_path):
            os.replace(live_path, out_path)
            result["image_path"] = out_path
        log.info("Saved retro annotated map (Pivotal Weather base): %s", out_path)
        return result

    # Fallback: render from herbie HRRR archive via AWS S3
    log.info(
        "Pivotal Weather image unavailable for %s fh=%d, falling back to herbie",
        RETRO_DATE,
        fh,
    )
    try:
        import matplotlib

        matplotlib.use("Agg")
        import cartopy.crs as ccrs
        import cartopy.feature as cfeature
        import matplotlib.pyplot as plt
        import numpy as np
        from herbie import Herbie

        H = Herbie(run_dt, model="hrrr", product="sfc", fxx=fh, verbose=False)
        ds = H.xarray("REFC:entire atmosphere")
        var = list(ds.data_vars)[0]
        lats = ds.latitude.values
        lons = ds.longitude.values
        data = ds[var].values.squeeze()

        proj = ccrs.LambertConformal(central_longitude=-96, central_latitude=37.5)
        fig = plt.figure(figsize=(14, 8), dpi=100)
        ax = fig.add_subplot(1, 1, 1, projection=proj)
        ax.set_extent([-122, -67, 22, 50], crs=ccrs.PlateCarree())
        ax.add_feature(cfeature.STATES, linewidth=0.5, edgecolor="black")
        ax.add_feature(cfeature.COASTLINE, linewidth=0.5)
        ax.contourf(
            lons,
            lats,
            data,
            levels=np.arange(0, 75, 5),
            cmap="gist_ncar",
            extend="both",
            transform=ccrs.PlateCarree(),
        )

        geo = ccrs.PlateCarree()

        # Storm motion indicator
        sv_tip = _destination_point(hatch_lat, hatch_lon, vector_dir, 120)
        ax.plot(
            [hatch_lon, sv_tip[1]],
            [hatch_lat, sv_tip[0]],
            color="orange",
            linewidth=2.5,
            transform=geo,
        )
        ax.plot(sv_tip[1], sv_tip[0], "^", color="orange", markersize=10, transform=geo)

        # Hatch-to-position line
        ax.plot(
            [hatch_lon, pos_lon],
            [hatch_lat, pos_lat],
            color=(0.7, 0.7, 1.0),
            linewidth=2,
            linestyle="--",
            transform=geo,
        )

        # Hatch area marker (red circle + cross)
        ax.plot(
            hatch_lon,
            hatch_lat,
            "ro",
            markersize=16,
            markerfacecolor="none",
            markeredgewidth=3,
            transform=geo,
        )
        ax.plot(
            hatch_lon,
            hatch_lat,
            "r+",
            markersize=14,
            markeredgewidth=2.5,
            transform=geo,
        )
        ax.text(
            hatch_lon + 0.6,
            hatch_lat + 0.3,
            f"HATCH AREA\n{hatch_lat:.2f}, {hatch_lon:.2f}",
            color="red",
            fontsize=8,
            transform=geo,
            bbox=dict(facecolor="black", alpha=0.55, boxstyle="round"),
        )

        # Positioning marker (yellow circle)
        ax.plot(
            pos_lon,
            pos_lat,
            "yo",
            markersize=16,
            markerfacecolor="none",
            markeredgewidth=3,
            transform=geo,
        )
        ax.plot(pos_lon, pos_lat, "yo", markersize=7, transform=geo)
        ax.text(
            pos_lon + 0.6,
            pos_lat + 0.3,
            f"POSITION HERE\n{pos_lat:.2f}, {pos_lon:.2f}",
            color="yellow",
            fontsize=8,
            transform=geo,
            bbox=dict(facecolor="black", alpha=0.55, boxstyle="round"),
        )

        valid_dt = run_dt + timedelta(hours=fh)
        vector_dir_from = (vector_dir + 180) % 360
        ax.set_title(
            f"Supercell Syn Chase Forecast — {RETRO_DATE}   "
            f"Storm Motion: {vector_dir_from:.0f}° @ {vector_spd:.0f} kts\n"
            f"HRRR Init: {run_dt:%Y-%m-%d %HZ}  fh={fh}h  Valid: {valid_dt:%Y-%m-%d %HZ}"
        )

        plt.savefig(out_path, format="png", bbox_inches="tight", dpi=100)
        plt.close(fig)
        log.info("Saved retro annotated map (herbie fallback): %s", out_path)

        return {
            "image_path": out_path,
            "hatch_area": {"lat": hatch_lat, "lon": hatch_lon},
            "positioning_location": {
                "lat": round(pos_lat, 4),
                "lon": round(pos_lon, 4),
            },
            "storm_vector": {"direction_deg": vector_dir, "speed_knots": vector_spd},
        }
    except Exception as exc:
        log.exception("Retro annotated map failed")
        return {"error": f"Error generating retro annotated map: {exc}"}


_RETRO_TOOL_FN = {
    "get_available_runs": _retro_get_available_runs,
    "get_spc_outlook": _retro_get_spc_outlook,
    "get_dew_point": _retro_get_dew_point,
    "get_reflectivity": _retro_get_reflectivity,
    "get_sounding": _retro_get_sounding,
    "generate_annotated_map": _retro_generate_annotated_map,
}


def _dispatch(name: str, inp: dict):
    fn_map = _RETRO_TOOL_FN if RETRO_DATE else _TOOL_FN
    fn = fn_map.get(name)
    if fn is None:
        return f"Unknown tool: {name}"
    log.info(
        "Tool call: %s(%s)",
        name,
        json.dumps({k: v for k, v in inp.items() if k not in ("data",)}),
    )
    return fn(inp)


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------
SYSTEM = """\
You are an expert storm chase meteorologist. Your goal is to identify the best \
daytime supercell chase opportunity and recommend a hatch area and positioning location.

CRITICAL CONSTRAINTS:
- Daytime chasing only. The HRRR 12Z run forward hours in local CDT: fh=6=18Z≈1 PM, \
  fh=9=21Z≈4 PM, fh=12=00Z≈7 PM. Focus your target on fh=3 through fh=9 (15Z–21Z). \
  Do not anchor on late-night or overnight convection.
- Prioritize isolated, discrete supercells. Cells embedded in the middle of a QLCS \
  lack isolation and are in the "wash" — unfavorable. However, do not fully discard \
  a QLCS: the southern-most tip of a QLCS line can bookend and spin off isolated \
  supercells that are worth targeting. If discrete cells exist near the dry line \
  independent of the QLCS, prefer those first.
- The hatch area must be near the dry line. Supercells initiate at the moisture \
  boundary where warm moist air meets drier air aloft. A hatch area deep in the warm \
  sector (far from the dry line) is wrong.

Follow this process in order:
1. Call get_available_runs() to find the latest HRRR run.
2. Call get_spc_outlook() to see today's risk areas (Enhanced/Moderate/High = priority).
3. Call get_dew_point() across at least 3 forward hours spanning the afternoon window \
   (fh=6, fh=9, fh=12 at minimum) to locate and track the dry line — look for a rapid \
   dewpoint drop of 20–30°F across a short distance (≤75 miles). Note the longitude \
   of the dry line's eastern edge (where moisture is still high). If you cannot \
   successfully fetch at least 3 dew point frames, call save_analysis_report() with \
   the reason and stop — do not proceed to generate a map.
4. Call get_reflectivity() at fh=6 to find the FIRST isolated high-reflectivity cells \
   (≥50 dBZ) appearing near the dry line during early afternoon. These "poppers" are \
   the prime supercell candidates. If a QLCS is present, note whether discrete cells \
   exist at its southern tip (bookend supercell potential) or independently near the \
   dry line — prefer those over cells embedded in the middle of the linear structure.
5. Call get_sounding() at 2–3 lat/lon points near the discrete cells found in step 4 \
   (not in the QLCS). Assess the Bunkers Right Motion Vector ('RM'), low-level jet \
   at 850–925 hPa, and hodograph shape for supercell potential.
6. Call generate_annotated_map() with the best hatch area (where discrete cells are \
   firing near the dry line) and the Bunkers Right Storm Motion Vector. Use the fh \
   where those discrete cells first appear (typically fh=6 or fh=9).
7. After the map is generated, call save_analysis_report() with your full written
   analysis (markdown is fine). Then your final message must be ONLY the post caption
   — no headers, no markdown, no extra commentary — in this exact format
   (≤240 characters):
   "Today, Chase recommends positioning in {positioning region}. Hatch area near \
   {hatch region}. SPC {risk} risk with {storm mode}. BRM {dir}°/{spd}kt."
   - {positioning region} and {hatch region} are plain English place names (e.g. "western Kentucky", "central Arkansas")
   - {risk} = Enhanced / Moderate / High
   - {storm mode} = brief description (e.g. "discrete supercells firing ahead of the QLCS")
   - BRM direction is the FROM direction (wind convention), speed in knots
   - No hashtags. No emoji.

If no discrete supercell opportunity exists within the Enhanced+ risk area (e.g. only \
QLCS or marginal shear), still call generate_annotated_map() with the best available \
target and note low confidence in the storm mode description.
"""


def run_agent() -> tuple[str | None, str | None]:
    """Run the storm chase forecast agent. Returns (image_path, caption)."""
    global _sounding_counter
    _sounding_counter = 0

    # Clear all stale images from any previous run
    import glob as _glob

    for stale in _glob.glob(os.path.join(LAST_RUN_DIR, "*.png")):
        os.remove(stale)
        log.info("Removed stale image: %s", stale)

    client = anthropic.Anthropic()
    if RETRO_DATE:
        date_obj = datetime.strptime(RETRO_DATE, "%Y-%m-%d")
        date_str = date_obj.strftime("%A, %B %-d, %Y")
        user_msg = (
            f"The date to analyze is {date_str} (retroactive). "
            "Please analyze this date's storm chase potential and produce a chase forecast map. "
            f"Note: you are analyzing {RETRO_DATE} using archived HRRR data from AWS S3."
        )
    else:
        today = datetime.now(timezone.utc).strftime("%A, %B %-d, %Y")
        user_msg = (
            f"Today is {today} UTC. "
            "Please analyze today's storm chase potential and produce a chase forecast map."
        )
    messages = [{"role": "user", "content": user_msg}]

    final_image_path = None
    final_caption = None

    for turn in range(MAX_AGENT_TURNS):
        log.info("Agent turn %d", turn + 1)
        with client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=10000,
            thinking={"type": "enabled", "budget_tokens": 8000},
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        ) as stream:
            response = stream.get_final_message()

        messages.append({"role": "assistant", "content": response.content})
        log.info("Stop reason: %s", response.stop_reason)

        if response.stop_reason == "end_turn":
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    final_caption = block.text.strip()
            break

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result = _dispatch(block.name, block.input)
                if block.name == "generate_annotated_map" and isinstance(result, dict):
                    final_image_path = result.get("image_path")
                    content = json.dumps(result)
                elif isinstance(result, list):
                    content = result
                else:
                    content = (
                        json.dumps(result) if not isinstance(result, str) else result
                    )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": content,
                    }
                )
            messages.append({"role": "user", "content": tool_results})
        else:
            log.warning("Unexpected stop_reason: %s", response.stop_reason)
            break

    if turn == MAX_AGENT_TURNS - 1:
        log.warning("Hit max agent turns (%d)", MAX_AGENT_TURNS)

    return final_image_path, final_caption


# ---------------------------------------------------------------------------
# Post to X
# ---------------------------------------------------------------------------
def post_to_x(image_path: str, caption: str) -> None:
    log.info("Caption:\n%s", caption)
    auth = tweepy.OAuth1UserHandler(
        os.getenv("X_API_KEY"),
        os.getenv("X_API_SECRET"),
        os.getenv("X_ACCESS_TOKEN"),
        os.getenv("X_ACCESS_TOKEN_SECRET"),
    )
    api = tweepy.API(auth)
    client = tweepy.Client(
        consumer_key=os.getenv("X_API_KEY"),
        consumer_secret=os.getenv("X_API_SECRET"),
        access_token=os.getenv("X_ACCESS_TOKEN"),
        access_token_secret=os.getenv("X_ACCESS_TOKEN_SECRET"),
    )

    log.info("Uploading image: %s", image_path)
    media = api.media_upload(filename=image_path)
    log.info("Uploaded media ID: %s", media.media_id)
    try:
        resp = client.create_tweet(text=caption, media_ids=[media.media_id])
        if resp.data:
            log.info("Posted! ID: %s", resp.data["id"])
        else:
            log.error("Post returned no data: %s", resp)
    except tweepy.errors.Forbidden as exc:
        log.error(
            "403 Forbidden posting tweet: %s",
            exc.response.text if exc.response else exc,
        )


# ---------------------------------------------------------------------------
# SPC pre-flight check
# ---------------------------------------------------------------------------
def spc_has_enhanced_risk() -> bool:
    """Return True if today's SPC Day 1 outlook contains Enhanced, Moderate, or High risk.

    Fetches the GeoJSON categorical outlook and checks feature labels.
    On network/parse failure, returns True (fail-open) so a real chase day
    is never silently skipped due to a transient error.
    """
    try:
        r = requests.get(SPC_DAY1_GEOJSON, headers=SPC_HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
        for feature in data.get("features", []):
            label = feature.get("properties", {}).get("LABEL", "")
            if label in ENHANCED_PLUS_LABELS:
                log.info(
                    "SPC pre-flight: Enhanced+ risk found (%s) — proceeding", label
                )
                return True
        log.info("SPC pre-flight: no Enhanced/Moderate/High risk today — skipping")
        return False
    except Exception as exc:
        log.error(
            "SPC pre-flight check failed (%s) — proceeding anyway (fail-open)", exc
        )
        return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    global RETRO_DATE
    parser = argparse.ArgumentParser(description="Storm Chase Forecast Bot (Phase 2)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the agent and generate the map but do not post to X",
    )
    parser.add_argument(
        "--post-only",
        action="store_true",
        help="Skip agent; read today's saved image and caption and post to X",
    )
    parser.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="Retroactive mode: analyze a past date using HRRR archive from AWS S3 via herbie",
    )
    args = parser.parse_args()

    if args.date:
        RETRO_DATE = args.date
        log.info("Retroactive mode: date=%s", RETRO_DATE)

    date_label = RETRO_DATE or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    image_path = os.path.join(RUNS_DIR, f"chase_{date_label}.png")
    caption_path = os.path.join(RUNS_DIR, f"chase_{date_label}.txt")

    if args.post_only:
        log.info("=== Chase Bot starting (post-only, date=%s) ===", date_label)
        if not os.path.exists(image_path):
            log.error("No saved image found at %s", image_path)
            return
        with open(caption_path) as f:
            caption = f.read().strip()
        log.info("Loaded caption: %s", caption)
        post_to_x(image_path, caption)
        log.info("=== Chase Bot finished ===")
        return

    log.info(
        "=== Chase Bot starting (dry_run=%s, date=%s) ===",
        args.dry_run,
        date_label,
    )

    # Pre-flight: only run the agentic analysis on Enhanced/Moderate/High risk days.
    # Retro mode always proceeds (manual fine-tuning run).
    if not RETRO_DATE and not spc_has_enhanced_risk():
        log.info("=== Chase Bot exiting — no Enhanced+ risk today ===")
        return

    image_path, caption = run_agent()

    if not image_path:
        log.error("Agent did not generate a map image — nothing to post")
        return

    if not caption:
        log.warning("Agent produced no caption — using default")
        caption = (
            f"Storm chase forecast for {date_label}. "
            "See map for positioning recommendation."
        )

    with open(caption_path, "w") as f:
        f.write(caption)
    log.info("Saved caption: %s", caption_path)

    if args.dry_run:
        log.info("DRY RUN — skipping X post")
        log.info("=== Chase Bot finished ===")
        return

    post_to_x(image_path, caption)
    log.info("=== Chase Bot finished ===")


if __name__ == "__main__":
    main()
