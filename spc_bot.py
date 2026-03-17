#!/usr/bin/env python3
"""SPC Convective Outlook Bot — scrapes outlook images and posts to X."""

import argparse
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests
import tweepy
from dotenv import load_dotenv

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
        logging.FileHandler(os.path.join(LOG_DIR, "spcbot.log")),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SPC_BASE = "https://www.spc.noaa.gov/products/outlook"
DAY48_URL = "https://www.spc.noaa.gov/products/exper/day4-8/day48prob.gif"
LATE_RUN_THRESHOLD_MINUTES = 60
HTTP_HEADERS = {"User-Agent": "SupercellSynBot/1.0 (+https://x.com/SupercellSyn)"}
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIR = os.path.join(PROJECT_DIR, "images")


# ---------------------------------------------------------------------------
# Image discovery
# ---------------------------------------------------------------------------
def fetch_outlook_timestamp(day: int) -> str | None:
    """Fetch the HTML page for a given day (1-3) and extract the latest
    categorical outlook timestamp from show_tab('otlk_XXXX')."""
    url = f"{SPC_BASE}/day{day}otlk.html"
    log.info("Fetching timestamp from %s", url)
    resp = _get_with_retry(url)
    if resp is None:
        return None
    matches = re.findall(r"show_tab\(['\"]otlk_(\d{4})['\"]\)", resp.text)
    if not matches:
        log.warning("No timestamp found on %s", url)
        return None
    # Use the first categorical timestamp found
    ts = matches[0]
    log.info("Day %d timestamp: %s", day, ts)
    return ts


def build_image_url(day: int, timestamp: str) -> str:
    return f"{SPC_BASE}/day{day}otlk_{timestamp}.png"


def download_image(url: str, dest: str) -> bool:
    """Download an image to dest. Returns True on success."""
    log.info("Downloading %s", url)
    resp = _get_with_retry(url)
    if resp is None:
        return False
    with open(dest, "wb") as f:
        f.write(resp.content)
    log.info("Saved to %s (%d bytes)", dest, len(resp.content))
    return True


def _get_with_retry(url: str, retries: int = 3) -> requests.Response | None:
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=30, headers=HTTP_HEADERS)
            if resp.status_code == 404:
                log.warning("404 for %s — not published yet", url)
                return None
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            wait = 2 ** attempt
            log.warning("Attempt %d failed for %s: %s — retrying in %ds",
                        attempt + 1, url, exc, wait)
            time.sleep(wait)
    log.error("All %d attempts failed for %s", retries, url)
    return None


# ---------------------------------------------------------------------------
# Posting to X
# ---------------------------------------------------------------------------
def post_tweet(image_paths: list[str], dry_run: bool = False) -> None:
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    labels = ["Day 1", "Day 2", "Day 3", "Day 4-8"]
    available = [labels[i] for i, p in enumerate(image_paths) if p]
    text = (
        f"SPC Convective Outlooks for {today}\n\n"
        f"{' \u00b7 '.join(available)}\n\n"
        "#wxtwitter #severeweather"
    )
    log.info("Tweet text:\n%s", text)

    if dry_run:
        log.info("DRY RUN — skipping post")
        return

    # v1.1 auth for media upload
    auth = tweepy.OAuth1UserHandler(
        os.getenv("X_API_KEY"),
        os.getenv("X_API_SECRET"),
        os.getenv("X_ACCESS_TOKEN"),
        os.getenv("X_ACCESS_TOKEN_SECRET"),
    )
    api = tweepy.API(auth)

    # v2 client for tweet creation
    client = tweepy.Client(
        consumer_key=os.getenv("X_API_KEY"),
        consumer_secret=os.getenv("X_API_SECRET"),
        access_token=os.getenv("X_ACCESS_TOKEN"),
        access_token_secret=os.getenv("X_ACCESS_TOKEN_SECRET"),
    )

    media_ids = []
    for path in image_paths:
        if path is None:
            continue
        log.info("Uploading %s", path)
        media = api.media_upload(filename=path)
        media_ids.append(media.media_id)
        log.info("Uploaded media ID: %s", media.media_id)

    response = client.create_tweet(text=text, media_ids=media_ids)
    if response.data:
        log.info("Tweet posted! ID: %s", response.data["id"])
    else:
        log.error("Tweet creation returned no data: %s", response)


# ---------------------------------------------------------------------------
# Late-run detection
# ---------------------------------------------------------------------------
def is_late() -> bool:
    now = datetime.now(timezone.utc)
    minutes_past_midnight = now.hour * 60 + now.minute
    return minutes_past_midnight > LATE_RUN_THRESHOLD_MINUTES


def relaunch_interactive() -> None:
    """Open a Terminal window and re-run this script with --confirm-late-run."""
    script_path = os.path.abspath(__file__)
    python = sys.executable
    cmd = f'{python} "{script_path}" --confirm-late-run'
    applescript = (
        f'tell application "Terminal"\n'
        f'  activate\n'
        f'  do script "{cmd}"\n'
        f'end tell'
    )
    log.info("Late run detected — launching interactive Terminal")
    subprocess.run(["osascript", "-e", applescript], check=True)


def confirm_late_run() -> bool:
    print("\n*** LATE RUN ***")
    print(f"Current UTC time: {datetime.now(timezone.utc).strftime('%H:%M')}")
    print("This run is more than 60 minutes past 00:00 UTC.")
    answer = input("Proceed with posting? (y/n): ").strip().lower()
    return answer == "y"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="SPC Convective Outlook Bot")
    parser.add_argument("--dry-run", action="store_true",
                        help="Download images but don't post to X")
    parser.add_argument("--confirm-late-run", action="store_true",
                        help="Interactive confirmation for late runs")
    args = parser.parse_args()

    log.info("=== SPC Bot starting (dry_run=%s) ===", args.dry_run)

    # Late-run handling
    if not args.dry_run and not args.confirm_late_run and is_late():
        relaunch_interactive()
        return

    if args.confirm_late_run:
        if not confirm_late_run():
            log.info("User declined late run — exiting")
            return

    # Scrape images into local images/ directory
    os.makedirs(IMAGE_DIR, exist_ok=True)
    image_paths: list[str | None] = [None, None, None, None]

    # Days 1-3
    for day in range(1, 4):
        ts = fetch_outlook_timestamp(day)
        if ts is None:
            continue
        url = build_image_url(day, ts)
        dest = os.path.join(IMAGE_DIR, f"day{day}.png")
        if download_image(url, dest):
            image_paths[day - 1] = dest

    # Day 4-8
    dest48 = os.path.join(IMAGE_DIR, "day48.gif")
    if download_image(DAY48_URL, dest48):
        image_paths[3] = dest48

    available = [p for p in image_paths if p]
    if not available:
        log.error("No outlook images available — nothing to post")
        return

    log.info("Downloaded %d of 4 outlooks", len(available))
    post_tweet(image_paths, dry_run=args.dry_run)

    log.info("=== SPC Bot finished ===")


if __name__ == "__main__":
    main()
