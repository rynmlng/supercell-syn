#!/usr/bin/env python3
"""
HRRR availability audit script.

Polls Pivotal Weather to record exactly when new HRRR runs and their
associated resources become available. Zero API cost — HTTP only.

Tracks per run:
  - When a new run appears in the status API
  - When each fh image (dew point + reflectivity) becomes available
  - When the sounding service responds for that run

Continuous mode (default): runs all day, auto-detecting each new HRRR run
as it appears (hourly). Prints a summary after each run completes, writes
results to a persistent daily log file. Press Ctrl+C for a full-day summary.

Single-run mode (--run): targets one specific run, exits when complete.

Usage:
  python audit_hrrr_availability.py                      # continuous all-day
  python audit_hrrr_availability.py --run 2026040216     # single run
  python audit_hrrr_availability.py --interval 30        # poll every 30s (default 60)
"""

import argparse
import logging
import signal
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

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

LOG_DIR = Path(__file__).parent / "runs" / "audit"

# ---------------------------------------------------------------------------
# Logging — console + daily file
# ---------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger(__name__)


def setup_file_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_path)
    fh.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.getLogger().addHandler(fh)
    log.info("Logging to %s", log_path)


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
# Data
# ---------------------------------------------------------------------------
@dataclass
class RunRecord:
    rh: str
    detected_at: datetime
    resources: dict[str, datetime | None] = field(default_factory=dict)

    def is_complete(self) -> bool:
        return all(v is not None for v in self.resources.values())

    def pending(self) -> list[str]:
        return [k for k, v in self.resources.items() if v is None]


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
            return max(runs, key=lambda x: x["rh"])["rh"]
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
# Summary printing
# ---------------------------------------------------------------------------
def print_run_summary(rec: RunRecord) -> None:
    print("\n" + "=" * 60)
    print(f"HRRR AVAILABILITY AUDIT — Run {rec.rh}")
    print("=" * 60)
    print(f"  Run detected:        {rec.detected_at.strftime('%H:%M:%S UTC')}")

    for key, ts in sorted(
        rec.resources.items(),
        key=lambda x: x[1] or datetime.max.replace(tzinfo=timezone.utc),
    ):
        secs = int((ts - rec.detected_at).total_seconds()) if ts else None
        delta = f"  (+{secs // 60}m {secs % 60:02d}s)" if secs is not None else ""
        ts_str = ts.strftime("%H:%M:%S UTC") if ts else "never"
        print(f"  {key:<24} {ts_str}{delta}")

    print("=" * 60 + "\n")


def print_day_summary(completed: list[RunRecord]) -> None:
    if not completed:
        print("\nNo runs completed today.\n")
        return

    print("\n" + "=" * 70)
    print(f"FULL DAY SUMMARY — {len(completed)} run(s) completed")
    print("=" * 70)

    resource_keys = list(completed[0].resources.keys())

    # Header
    header = f"  {'Run':<12}"
    for key in resource_keys:
        header += f"  {key:<22}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for rec in completed:
        row = f"  {rec.rh:<12}"
        for key in resource_keys:
            ts = rec.resources.get(key)
            if ts:
                secs = int((ts - rec.detected_at).total_seconds())
                cell = f"+{secs // 60}m{secs % 60:02d}s"
            else:
                cell = "never"
            row += f"  {cell:<22}"
        print(row)

    print("=" * 70 + "\n")


# ---------------------------------------------------------------------------
# Core: track a single run until all resources confirmed
# ---------------------------------------------------------------------------
def track_run(rec: RunRecord, interval: int) -> None:
    """Poll until all resources in rec are confirmed, updating rec.resources in place."""
    # Initialize resource keys if not already set
    if not rec.resources:
        for param, label in PARAMS.items():
            for fh in FH_TARGETS:
                rec.resources[f"{label} fh={fh:02d}"] = None
        rec.resources["Sounding"] = None

    while True:
        now = datetime.now(timezone.utc)
        pending = rec.pending()

        for key in list(pending):
            if key == "Sounding":
                available = check_sounding(rec.rh)
            else:
                label, fh_part = key.rsplit(" fh=", 1)
                fh = int(fh_part)
                param = next(p for p, l in PARAMS.items() if l == label)
                available = check_image(rec.rh, fh, param)

            if available:
                rec.resources[key] = now
                log.info("AVAILABLE: %-22s at %s UTC", key, now.strftime("%H:%M:%S"))

        remaining = rec.pending()
        if not remaining:
            log.info("Run %s: all resources confirmed.", rec.rh)
            return

        log.info(
            "Run %s — pending (%d/%d): %s",
            rec.rh,
            len(remaining),
            len(rec.resources),
            ", ".join(remaining),
        )
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="HRRR availability audit")
    parser.add_argument(
        "--run",
        metavar="YYYYMMDDHH",
        help="Target a specific HRRR run (single-run mode — exits when complete)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_POLL_INTERVAL,
        help=f"Poll interval in seconds (default: {DEFAULT_POLL_INTERVAL})",
    )
    args = parser.parse_args()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = LOG_DIR / f"audit_{today}.log"
    setup_file_logging(log_path)

    log.info("=== HRRR Availability Audit starting ===")
    log.info(
        "Mode: %s | Poll interval: %ds | Sounding probe: %.1f, %.1f",
        "single-run" if args.run else "continuous",
        args.interval,
        SOUNDING_LAT,
        SOUNDING_LON,
    )

    # ----- Single-run mode -----
    if args.run:
        now = datetime.now(timezone.utc)
        rec = RunRecord(rh=args.run, detected_at=now)
        log.info("Targeting specified run: %s", args.run)
        track_run(rec, args.interval)
        print_run_summary(rec)
        return

    # ----- Continuous mode -----
    completed: list[RunRecord] = []

    def handle_sigint(sig, frame):
        log.info("Interrupted — printing day summary.")
        print_day_summary(completed)
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sigint)

    baseline_rh = get_latest_run()
    if baseline_rh:
        log.info("Baseline run at startup: %s — waiting for a newer run", baseline_rh)
    else:
        log.warning("Could not determine baseline — will track whatever appears first")

    current_rh: str | None = None

    while True:
        now = datetime.now(timezone.utc)

        # Detect a new run
        latest = get_latest_run()
        if latest and latest != baseline_rh:
            if latest != current_rh:
                current_rh = latest
                log.info(
                    "NEW RUN DETECTED: %s (at %s UTC)",
                    current_rh,
                    now.strftime("%H:%M:%S"),
                )
                rec = RunRecord(rh=current_rh, detected_at=now)
                track_run(rec, args.interval)
                print_run_summary(rec)
                completed.append(rec)
                # Advance baseline so we wait for the next new run
                baseline_rh = current_rh
                current_rh = None
        else:
            log.info(
                "No new run yet (latest: %s) — sleeping %ds",
                latest or "unknown",
                args.interval,
            )
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
