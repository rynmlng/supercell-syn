# Supercell Syn

## Bots

### Otto (`spc_bot.py`) — SPC Outlook Bot
Posts daily SPC convective outlook images to X.
- Pulls Day 1, Day 2, Day 3, and Day 4-8 outlook images from https://www.spc.noaa.gov/products/outlook/
- Runs daily at 12:35 UTC via GitHub Actions (March–June), a few minutes after SPC publishes at ~12:30 UTC
- Script checks UTC time for late-run detection: if run is >60 min past 12:35 UTC, prompts for confirmation before posting (macOS interactive relaunch via AppleScript)
- Scheduled via `.github/workflows/spc-bot.yml`

### Chase (`chase_bot.py`) — Chase Intercept Bot
AI-powered agentic storm intercept recommendation. Only runs on days with an SPC Day 1 Enhanced or higher risk.
- Pre-flight check: fetches `day1otlk_cat.nolyr.geojson` and bails if no ENH/MDT/HIGH label
- Runs the Claude Opus agentic loop to analyze HRRR data and generate an annotated chase map
- Posts the map + caption to X
- Runs daily at 12:30 UTC via GitHub Actions (March–June), ~20 min after HRRR 12Z fh=6 is available (~1 fh/min publish rate)
- Scheduled via `.github/workflows/chase-bot.yml`

## Chase Bot — Data Sources

- Name: NOAA SPC Day 1 Convective Outlook
  URL: https://www.spc.noaa.gov/products/outlook/day1otlk.html
  Purpose: Observe NOAA's convection outlook, categorized
- Name: Pivotal Weather HRRR 2 m AGL Dew Point
  URL: https://m2o.pivotalweather.com/maps/models/hrrr/{rh}/{fh:03d}/sfctd-imp.conus.png
  Purpose: Determine the dry-line for cumulus and cumulonimbus hatching.
- Name: Pivotal Weather HRRR Composite Reflectivity (dBZ)
  URL: https://m2o.pivotalweather.com/maps/models/hrrr/{rh}/{fh:03d}/refcmp.conus.png
  Purpose: Predict precipitation and convective cores of storms.
- Name: Pivotal Weather Sounding Chart For Location
  Purpose: Read directional & speed shear from hodograph, storm speed from Bunkers Right Storm Motion Vector, and confirm low-level jet to feed storms from Skew-T Log-P Diagram.
  Fetch flow (3 steps):
    1. GET https://www.pivotalweather.com/sounding.php?m=hrrr&p=refcmp&rh={rh}&fh={fh}&r=conus&lon={lon}&lat={lat}
       Headers: Referer: https://www.pivotalweather.com/
       Extract: `<div id="snd_token" data-token="{token}">` from the HTML response
    2. GET https://i1o.pivotalweather.com/make_sounding.php?m=hrrr&rh={rh}&fh={fh}&t={token}&lat={lat}&lon={lon}
       Returns XML: `<sounding lat="..." lon="..." image="{filename}" />`
       Note: server snaps lat/lon to nearest grid point
    3. GET https://i1o.pivotalweather.com/sounding_images/{filename}
       Returns the sounding chart PNG

## Chase Bot — HRRR URL Format

All Pivotal Weather HRRR map images:
`https://m2o.pivotalweather.com/maps/models/hrrr/{rh}/{fh:03d}/{p}.conus.png`

Available runs: `https://www.pivotalweather.com/status_model.php?m=hrrr&s=1`
Returns JSON array of `{"rh": "2026032220", "fh": 18, "tiers": [...], "final_fh": 18}`.
Use the entry with the highest `rh` where `fh >= 0` as the latest available run.

- `rh` — UTC timestamp of the HRRR model run (YYYYMMDDhh), e.g. `2026032212`
- `fh` — Forward hour, zero-padded to 2 digits (e.g. `06`, `12`)
- `p` — Parameter: `sfctd-imp` (2m AGL Dew Point), `refcmp` (Composite Reflectivity)

## Chase Bot — Recommendation Logic

1. View the "NOAA SPC Day 1 Convective Outlook." Note locations of Enhanced, Moderate, and High risk (orange, red, and magenta).
2. Look at the "Pivotal Weather 2 m AGL Dew Point" parameter and find a dry-line within 500 miles of the risk area. Scan through different forward hours of the latest model run. A sharp dry line is identified by a rapid dewpoint drop of 20–30°F across ~75 miles, transitioning from ≥60°F (humid) east of the line to ≤40°F (arid) to the west.
3. Scan the "Pivotal Weather HRRR Composite Reflectivity (dBZ)" parameter for high reflectivities near the dry line. Large, isolated cells (≥50 dBZ) are indicative of supercells. Prioritize discrete cells near the moisture boundary. The southern tip of a QLCS can also spin off isolated supercells (bookend) — don't fully discard a QLCS, but deprioritize cells buried in the middle of the linear structure.
4. Drill into areas of high reflectivity from (3) and pull sounding charts. Look for low-level jets (LLJs) at 850–925 hPa and notable directional & speed shear in the hodograph. Note these areas as the Hatch Area. Focus on daytime convection (fh=3–13, valid 15Z–01Z, through local sunset) — do not anchor on overnight or pre-dawn convection. Do not artificially cap at an earlier hour if the peak setup clearly occurs later in the afternoon or early evening.
5. From matching soundings in (4), extract the Bunkers Right Storm Motion Vector (BRM) to determine storm motion direction (reported as FROM, wind convention) and speed in knots. Note this as the Storm Vector.
6. Draw a point 250 miles from the Hatch Area in the direction the storm moves TOWARD (BRM direction + 180°). Then draw a point 10 miles south of this. That is the Positioning Location. The Storm Vector speed is not used for drawing points — it is informational for the chaser to know how hard it will be to follow the storm.

### Image Generation
Annotated map uses the HRRR Composite Reflectivity chart as the base layer, with the following overlaid:
- Red circle + cross → Hatch Area
- Yellow filled circle → Positioning Location
- Blue-grey dashed line → path between them
- Orange arrow → storm motion direction indicator
- Title bar showing date, valid time (UTC and CT), and Storm Motion (BRM FROM direction / speed)

### Output / Post Format
Post caption (≤240 chars): state where the hatch area is (tornado target zone, not positioning), SPC risk category, storm mode (discrete supercells preferred), BRM direction and speed, and the target valid time in CT (e.g. "target window 4–6PM CT"). No SRH or other metrics. No hashtags. Start with 🌪️.

## Tech Stack
- Python 3.12 (venv: venv-supercellsyn-bot-312)
- tweepy for X API
- anthropic for Claude API (Chase)
- herbie-data, metpy, cartopy for retroactive HRRR analysis (Chase)
- python-dotenv for env management

## Environment
- API keys live in `.env` (X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET, ANTHROPIC_API_KEY)
- Never commit or expose `.env` values
