# Otto (`spc_bot.py`) — Design Reference

## Overview

Otto is a daily SPC convective outlook posting bot. It scrapes the latest categorical outlook images from NOAA's Storm Prediction Center and posts them to X each morning.

---

## What It Posts

Four outlook images in a single post:
- **Day 1** — today's categorical convective outlook
- **Day 2** — tomorrow's outlook
- **Day 3** — day after tomorrow
- **Day 4–8** — extended probabilistic outlook (GIF)

---

## Image Discovery

### Days 1–3
Otto fetches the HTML page for each day:
```
https://www.spc.noaa.gov/products/outlook/day{N}otlk.html
```
It extracts timestamps from `show_tab('otlk_HHMM')` patterns in the page JavaScript, then selects the most recent issuance that has already been published (timestamp ≤ current UTC HHMM). This ensures the daytime 1200Z outlook is used rather than the overnight 0100Z one.

Image URL pattern:
```
https://www.spc.noaa.gov/products/outlook/day{N}otlk_{HHMM}.png
```

### Day 4–8
Fixed URL — always the latest published:
```
https://www.spc.noaa.gov/products/outlook/day4-8/day48prob.gif
```

---

## Post Format

```
{Day of week}'s ({M/D/YY}) fresh SPC convective outlooks brought to you by Otto

Day 1 · Day 2 · Day 3 · Day 4-8
```
Only days with successfully downloaded images are listed.

---

## Late-Run Detection

Otto is scheduled at 12:35 UTC. If run more than 60 minutes late (i.e. after 13:35 UTC) on macOS, it relaunches itself in a new Terminal window and prompts for confirmation before posting. On non-macOS platforms it logs a warning and proceeds.

Bypassed with `--dry-run` or `--confirm-late-run`.

---

## Saved Images

Images are saved to `images/otto/` and committed to the repo after each run:

| File | Contents |
|---|---|
| `day1.png` | SPC Day 1 categorical outlook |
| `day2.png` | SPC Day 2 categorical outlook |
| `day3.png` | SPC Day 3 categorical outlook |
| `day48.gif` | SPC Day 4–8 probabilistic outlook |

Files are overwritten on each run.

---

## Usage

```bash
# Full run — download images and post to X
python spc_bot.py

# Dry run — download images, skip X post
python spc_bot.py --dry-run

# Confirm and proceed with a late run
python spc_bot.py --confirm-late-run
```

Logs written to `logs/spcbot.log`.

---

## Scheduling

Runs daily at **12:35 UTC** via GitHub Actions (`.github/workflows/spc-bot.yml`), March–June. SPC publishes the Day 1 1200Z outlook at approximately 12:30 UTC.
