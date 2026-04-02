#!/usr/bin/env python3
"""
HRRR availability audit script.

Polls Pivotal Weather to record exactly when a new HRRR run and its
associated resources become available. Zero API cost — HTTP only.

Tracks:
  - When a new run appears in the status API
  - When each fh image (dew point + reflectivity) becomes available
  - When the sounding service responds for that run

Exits automatically once all tracked resources are confirmed available,
then prints a full timeline summary.

Usage:
  python audit_hrrr_availability.py
  python audit_hrrr_availability.py --run 2026040216   # target a specific run
  python audit_hrrr_availability.py --interval 30      # poll every 30s (default 60)
"""

import argparse
import logging
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_POLL_INTERVAL = 60  # seconds

FH_TARGETS = [6, 9, 12]
PARAMS = {
    "sfctd-imp": "Dew Point",
    "refcmp": "Reflectivity",
}

# Fixed central US probe location for sounding check
SOUNDING_LAT = 38.0
SOUNDING_LON = -97.5

HRRR_STATUS_URL = "https://www.pivotalweather.com/status_model.php?m=hrrr&s=1"
HRRR_IMAGE_URL = (
    "https://m2o.pivotalweather.com/maps/models/hrrr/{rh}/{fh:03d}/{param}.conus.png"
)
MODEL_PAGE_URL = (
    "https://www.pivotalweather.com/model.php"
    "?rh={rh}&fh=6&dpdt=&mc=&r=us_c&p=refcmp&m=hrrr"
)
SOUNDING_PAGE_URL = (
    "https://www.pivotalweather.com/sounding.php"
    f"?rh={{rh}}&fh=6&dpdt=&mc=&lat={SOUNDING_LAT:.4f}&lon={SOUNDING_LON:.4f}&r=us_c&p=refcmp&m=hrrr"
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

session = requests.Session()
session.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Ch-Ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Upgrade-Insecure-Requests": "1",
    }
)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def get_latest_run() -> str | None:
    """Return the rh string of the latest HRRR run from the status API, or None on failure."""
    try:
        r = session.get(HRRR_STATUS_URL, timeout=15)
        r.raise_for_status()
        runs = r.json()
        if runs:
            return runs[0]["rh"]
    except Exception as exc:
        log.warning("Status API error: %s", exc)
    return None


def check_image(rh: str, fh: int, param: str) -> bool:
    """Return True if the fh image for this run/param is available (HTTP 200)."""
    url = HRRR_IMAGE_URL.format(rh=rh, fh=fh, param=param)
    try:
        r = session.head(url, timeout=10)
        return r.status_code == 200
    except Exception:
        return False


def check_sounding(rh: str) -> bool:
    """Return True if a real sounding image is available for this run.

    Completes all three steps of the Pivotal Weather sounding flow:
    1. Fetch sounding.php and extract snd_token
    2. Call make_sounding.php and confirm an image filename is returned
    3. HEAD the sounding image to confirm it exists
    """
    model_url = MODEL_PAGE_URL.format(rh=rh)
    sounding_url = SOUNDING_PAGE_URL.format(rh=rh)
    try:
        # Step 1: establish session and extract token
        session.get("https://www.pivotalweather.com/", timeout=20)
        session.get(model_url, timeout=20)
        r = session.get(
            sounding_url,
            headers={
                "Referer": model_url,
                "Sec-Fetch-Dest": "iframe",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
            },
            timeout=20,
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        token_div = soup.find(id="snd_token")
        if not token_div:
            return False
        token = token_div.get("data-token", "")
        if not token:
            return False

        # Step 2: call make_sounding.php and confirm image filename returned
        make_url = (
            f"https://i1o.pivotalweather.com/make_sounding.php"
            f"?m=hrrr&rh={rh}&fh=6&t={token}"
            f"&lat={SOUNDING_LAT:.4f}&lon={SOUNDING_LON:.4f}"
        )
        r2 = session.get(make_url, headers={"Referer": sounding_url}, timeout=20)
        r2.raise_for_status()

        import xml.etree.ElementTree as ET

        root = ET.fromstring(r2.text)
        image_filename = root.get("image")
        if not image_filename:
            log.warning(
                "Sounding: make_sounding.php returned no image (error: %s)",
                root.get("error", "unknown"),
            )
            return False

        # Step 3: confirm the image actually exists
        img_url = f"https://i1o.pivotalweather.com/sounding_images/{image_filename}"
        r3 = session.head(img_url, timeout=10)
        return r3.status_code == 200

    except Exception as exc:
        log.warning("Sounding check error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="HRRR availability audit")
    parser.add_argument(
        "--run",
        metavar="YYYYMMDDHH",
        help="Target a specific HRRR run instead of waiting for a new one",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_POLL_INTERVAL,
        help=f"Poll interval in seconds (default: {DEFAULT_POLL_INTERVAL})",
    )
    args = parser.parse_args()

    log.info("=== HRRR Availability Audit starting ===")
    log.info(
        "Poll interval: %ds | Sounding probe: %.1f, %.1f",
        args.interval,
        SOUNDING_LAT,
        SOUNDING_LON,
    )

    # Determine target run
    if args.run:
        target_rh = args.run
        log.info("Targeting specified run: %s", target_rh)
        baseline_rh = None
    else:
        baseline_rh = get_latest_run()
        target_rh = None
        if baseline_rh:
            log.info(
                "Baseline run at startup: %s — waiting for a newer run", baseline_rh
            )
        else:
            log.warning(
                "Could not determine baseline run — will track whatever appears first"
            )

    # Build resource tracker: key -> first-seen UTC datetime (None = not yet seen)
    resources: dict[str, datetime | None] = {}
    run_detected_at: datetime | None = None

    def build_resources(rh: str) -> None:
        for param, label in PARAMS.items():
            for fh in FH_TARGETS:
                key = f"{label} fh={fh:02d}"
                if key not in resources:
                    resources[key] = None
        if "Sounding" not in resources:
            resources["Sounding"] = None

    if target_rh:
        build_resources(target_rh)

    # Poll loop
    while True:
        now = datetime.now(timezone.utc)

        # Step 1: detect target run if not yet known
        if not target_rh:
            latest = get_latest_run()
            if latest and latest != baseline_rh:
                target_rh = latest
                run_detected_at = now
                log.info(
                    "NEW RUN DETECTED: %s (at %s UTC)",
                    target_rh,
                    now.strftime("%H:%M:%S"),
                )
                build_resources(target_rh)
            else:
                log.info(
                    "No new run yet (latest: %s) — sleeping %ds",
                    latest or "unknown",
                    args.interval,
                )
                time.sleep(args.interval)
                continue

        # Step 2: check each resource not yet confirmed
        pending = [k for k, v in resources.items() if v is None]

        for key in list(pending):
            if key == "Sounding":
                available = check_sounding(target_rh)
            else:
                # Parse "Dew Point fh=06" or "Reflectivity fh=09"
                label, fh_part = key.rsplit(" fh=", 1)
                fh = int(fh_part)
                param = next(p for p, l in PARAMS.items() if l == label)
                available = check_image(target_rh, fh, param)

            if available:
                resources[key] = now
                log.info("AVAILABLE: %-22s at %s UTC", key, now.strftime("%H:%M:%S"))

        pending_after = [k for k, v in resources.items() if v is None]

        if not pending_after:
            log.info("All resources confirmed available — shutting down.")
            break

        log.info(
            "Still pending (%d/%d): %s",
            len(pending_after),
            len(resources),
            ", ".join(pending_after),
        )
        time.sleep(args.interval)

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print(f"HRRR AVAILABILITY AUDIT — Run {target_rh}")
    print("=" * 60)

    if run_detected_at:
        print(f"  Run detected:        {run_detected_at.strftime('%H:%M:%S UTC')}")

    for key, ts in sorted(
        resources.items(),
        key=lambda x: x[1] or datetime.max.replace(tzinfo=timezone.utc),
    ):
        delta = ""
        if run_detected_at and ts:
            secs = int((ts - run_detected_at).total_seconds())
            delta = f"  (+{secs // 60}m {secs % 60:02d}s after run detected)"
        ts_str = ts.strftime("%H:%M:%S UTC") if ts else "never"
        print(f"  {key:<24} {ts_str}{delta}")

    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
