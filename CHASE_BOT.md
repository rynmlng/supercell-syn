# Chase (`chase_bot.py`) — Design Reference

## Overview

Chase is an agentic storm intercept recommendation bot using Claude Opus 4.6 with adaptive thinking. It runs only on days where the SPC Day 1 outlook contains Enhanced, Moderate, or High risk. It fetches and interprets HRRR model data across multiple sources, generates an annotated chase map, and posts it to X.

---

## Pre-flight Check

Before the agent loop starts, `spc_has_enhanced_risk()` fetches:
```
https://www.spc.noaa.gov/products/outlook/day1otlk_cat.nolyr.geojson
```
If no feature has `LABEL` in `{"ENH", "MDT", "HIGH"}`, the bot exits immediately with no X post and no Claude API call. Fails open on network error (proceeds rather than silently skipping a real chase day).

Bypassed entirely in `--date` retroactive mode.

---

## Tools

| Tool | What it does |
|---|---|
| `get_available_runs` | Queries `status_model.php` for latest HRRR `rh` and `max_fh` |
| `get_spc_outlook` | Downloads today's SPC Day 1 categorical outlook image |
| `get_dew_point(rh, fh)` | Fetches the dew point PNG to find the dry line; saves to `images/chase/dew_point_fh{fh}.png` |
| `get_reflectivity(rh, fh)` | Fetches the composite reflectivity PNG; saves to `images/chase/reflectivity_fh{fh}.png` |
| `get_sounding(rh, fh, lat, lon)` | Full 3-step Pivotal Weather sounding fetch — gets token, calls `make_sounding.php`, returns the sounding chart PNG; saves to `images/chase/sounding_{n}.png` |
| `generate_annotated_map(...)` | Computes positioning location, draws markers on the reflectivity chart, saves to `images/chase/chase_map.png` |

---

## Agent Loop

Chase drives the analysis iteratively:

1. `get_available_runs()` — find the latest HRRR 12Z model run (`rh`) and available forward hours
2. `get_spc_outlook()` — identify Enhanced/Moderate/High risk regions
3. `get_dew_point(rh, fh)` — scan fh=6 and fh=9 to locate a sharp dry line (20–30°F drop over ~75 miles)
4. `get_reflectivity(rh, fh)` — find isolated high-reflectivity cells (≥50 dBZ) near the dry line at fh=6; prefer discrete cells over cells embedded in the middle of a QLCS (southern QLCS tip/bookend cells are valid targets)
5. `get_sounding(rh, fh, lat, lon)` — probe 2–3 candidate locations near discrete cells for LLJ, hodograph shear, and Bunkers Right Storm Motion Vector; focus on daytime convection (fh=3–9, valid 15Z–21Z)
6. `generate_annotated_map(...)` — create the final annotated PNG using the fh where discrete cells first appear
7. Final `end_turn` response from Claude becomes the post caption

The loop runs until Claude issues `end_turn` or hits the 30-turn limit.

---

## Image Generation (`generate_annotated_map`)

Inputs from Claude:
- `hatch_area_lat`, `hatch_area_lon` — center of the convective hatch area
- `storm_vector_direction_deg` — Bunkers Right Storm Motion direction in degrees **TOWARD** (used internally for positioning math)
- `storm_vector_speed_knots` — storm motion speed (informational, shown on map)
- `rh`, `fh` — which HRRR frame to use as the base map

What it computes:
```
positioning_location = hatch_area
    → move 250 miles in storm_vector_direction (TOWARD)
    → move 10 miles south
```

What it draws on the reflectivity chart:
- Red circle with X → Hatch Area (with lat, lon label)
- Yellow filled circle → Positioning Location (with lat, lon label)
- Blue-grey dashed line → path between them
- Orange arrow → storm motion direction indicator
- Title bar: date + Storm Motion displayed as **FROM direction** (storm_vector_direction + 180°) / speed
- Legend

Output saved to `images/chase/chase_map.png` (overwrites on each run).

### Bunkers Right Motion (BRM) Convention
- The tool accepts direction **TOWARD** (e.g. 65° = storm moving ENE)
- Displayed on the map title and sounding parameters box as **FROM** (wind convention), e.g. 245°
- This matches how BRM is reported on Pivotal Weather and SPC soundings

---

## Saved Images

All images in `images/chase/` use fixed filenames and are overwritten on each run. Sounding files are cleaned up at run start so stale files from a prior run (e.g. `sounding_3.png` when today only has 2 soundings) are deleted.

| File | Contents |
|---|---|
| `dew_point_fh06.png` | Pivotal Weather HRRR 2m AGL Dew Point, fh=6 |
| `dew_point_fh09.png` | Pivotal Weather HRRR 2m AGL Dew Point, fh=9 |
| `reflectivity_fh06.png` | Pivotal Weather HRRR Composite Reflectivity, fh=6 |
| `reflectivity_fh09.png` | Pivotal Weather HRRR Composite Reflectivity, fh=9 |
| `sounding_1.png` | Pivotal Weather sounding chart, first candidate location |
| `sounding_2.png` | Pivotal Weather sounding chart, second candidate location |
| `sounding_3.png` | Pivotal Weather sounding chart, third candidate location (if probed) |
| `chase_map.png` | Final annotated chase map |

---

## Output / Post Format

Post caption (≤240 chars):
```
Today, Chase recommends positioning in {positioning region}. Hatch area near {hatch region}. SPC {risk} risk with {storm mode}. BRM {dir}°/{spd}kt.
```
- `{positioning region}` and `{hatch region}` are plain English place names
- `{risk}` = Enhanced / Moderate / High
- `{storm mode}` = brief description (e.g. "discrete supercells firing ahead of the QLCS")
- BRM direction is the FROM direction (wind convention), speed in knots
- No hashtags. No emoji.

---

## Retroactive Mode

Used for fine-tuning and validation against past chase days. Uses herbie + AWS S3 (`noaa-hrrr-bdp-pds`) for archived HRRR data instead of live Pivotal Weather fetches, and cartopy/metpy for rendering sounding charts locally.

Retro images save to `images/chase/retro/` and are not overwritten by live runs.

```bash
# Place SPC outlook image at:
images/chase/retro/spc_day1otlk_{YYYYMMDD}_1200.png

# Then run:
python chase_bot.py --date 2026-03-15 --dry-run
```

---

## Usage

```bash
# Live run — analyze today, post to X
python chase_bot.py

# Live run — analyze today, skip X post
python chase_bot.py --dry-run

# Retroactive — analyze a past date, skip X post
python chase_bot.py --date 2026-03-15 --dry-run
```

Logs written to `logs/chasebot.log`.

---

## Scheduling

Runs daily at **14:30 UTC** via GitHub Actions (`.github/workflows/chase-bot.yml`), March–June. By 14:30 UTC the HRRR 12Z run's fh=6 (valid 18Z / 1 PM CT) is reliably available on S3. The SPC 13Z issuance is also published by then.
