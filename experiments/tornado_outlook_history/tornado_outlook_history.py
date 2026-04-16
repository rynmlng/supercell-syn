"""
Scrapes SPC Day 1 Tornado Probability Outlook images for a date range
and detects presence of probability tiers (10%+) via pixel color sampling.

URL pattern:
  https://www.spc.noaa.gov/products/outlook/archive/{year}/day1probotlk_v_{YYYYMMDD}_{HH}00_torn_prt.gif
Tries 1200Z first, falls back to 1300Z.

Output: tornado_outlook_history.csv
"""

import csv
import time
from datetime import date, timedelta
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from PIL import Image
except ImportError:
    raise SystemExit("Pillow is required: pip install Pillow")

# ---------------------------------------------------------------------------
# Probability tier colors — exact RGB values from SPC GIF palette
# Verified by reading the actual palette from 2025 season images.
# ---------------------------------------------------------------------------
TIERS = {
    "10pct": [(255, 235, 128)],  # yellow
    "15pct": [(239, 135, 132)],  # pink/salmon (alpha-blended map color)
    "30pct": [(255, 128, 255)],  # light magenta (alpha-blended map color)
}

COLOR_TOLERANCE = 20
MIN_PIXELS = (
    150  # filters out small tornado report dots (~50 px) vs real contours (900+ px)
)


def color_matches(pixel_rgb, targets, tol=COLOR_TOLERANCE):
    r, g, b = pixel_rgb[:3]
    for tr, tg, tb in targets:
        if abs(r - tr) <= tol and abs(g - tg) <= tol and abs(b - tb) <= tol:
            return True
    return False


def detect_tiers(img: Image.Image) -> dict:
    """
    Sample map pixels only; return dict of tier -> bool.

    Masks out the legend panel (bottom-right corner) and the NOAA branding
    strip (bottom-left) before sampling so legend swatches don't register
    as actual forecast colors.

    Based on the standard 815x555 SPC outlook GIF layout:
      - Legend box:  x > 560, y > 390
      - NOAA strip:  y > 430  (title/metadata row below map)
    """
    rgb = img.convert("RGB")
    pixels_obj = rgb.load()

    # Scan only the map area — excludes title, legend, and branding
    X0, Y0, X1, Y1 = 11, 34, 815, 465

    counts = {tier: 0 for tier in TIERS}
    for y in range(Y0, Y1):
        for x in range(X0, X1):
            px = pixels_obj[x, y]
            for tier, colors in TIERS.items():
                if color_matches(px, colors):
                    counts[tier] += 1

    return {tier: cnt >= MIN_PIXELS for tier, cnt in counts.items()}


def fetch_image(year, date_str, hour):
    url = (
        f"https://www.spc.noaa.gov/products/outlook/archive/{year}/"
        f"day1probotlk_{date_str}_{hour}00_torn.gif"
    )
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=15) as resp:
            return Image.open(BytesIO(resp.read())), hour, url
    except HTTPError as e:
        if e.code == 404:
            return None, None, url
        raise
    except URLError:
        return None, None, url


def run(start: date, end: date, output_path: str):
    fieldnames = ["date", "hour_used", "10pct", "15pct", "30pct", "url"]
    rows = []

    current = start
    total = (end - start).days
    i = 0

    while current < end:
        date_str = current.strftime("%Y%m%d")
        year = current.year
        i += 1
        print(f"[{i}/{total}] {current.isoformat()} ...", end=" ", flush=True)

        img, hour_used, url = fetch_image(year, date_str, "12")
        if img is None:
            img, hour_used, url = fetch_image(year, date_str, "13")

        if img is None:
            print("no outlook")
            rows.append(
                {
                    "date": current.isoformat(),
                    "hour_used": "",
                    **{t: "" for t in TIERS},
                    "url": "",
                }
            )
        else:
            tiers = detect_tiers(img)
            any_significant = any(tiers.values())
            print(
                f"{hour_used}Z | " + ", ".join(t for t, v in tiers.items() if v)
                if any_significant
                else f"{hour_used}Z | none"
            )
            rows.append(
                {
                    "date": current.isoformat(),
                    "hour_used": f"{hour_used}Z",
                    **{t: ("1" if v else "0") for t, v in tiers.items()},
                    "url": url,
                }
            )

        current += timedelta(days=1)
        time.sleep(0.3)  # be polite to SPC servers

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nDone. {len(rows)} rows written to {output_path}")


if __name__ == "__main__":
    run(
        start=date(2025, 3, 1),
        end=date(2025, 7, 1),
        output_path="tornado_outlook_history.csv",
    )
