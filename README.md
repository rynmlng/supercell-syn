# supercell-syn
Bots that feed storm-chasing for the Supercell Syndicate

<img src="/images/brands/x-logo-black.png" height="12"/>&nbsp;&nbsp;<a href="https://x.com/SupercellSyn">Follow us on X</a> to use our bot and spectate our annual chase!

---

## Bots

### Otto — SPC Outlook Bot (`spc_bot.py`)
Posts the daily SPC convective outlook images to X every morning during storm season.

- Pulls Day 1, Day 2, Day 3, and Day 4–8 outlook images from [SPC](https://www.spc.noaa.gov/products/outlook/)
- Runs daily at 1235Z (March–June) via GitHub Actions, a few minutes after SPC publishes at ~1230Z

### Chase — AI Storm Intercept Bot (`chase_bot.py`)
An AI-powered agentic storm intercept recommendation. Only activates on days with an SPC Day 1 Enhanced or higher risk.

- Fetches the SPC Day 1 outlook GeoJSON and bails early if no ENH/MDT/HIGH label is present
- Runs a Claude Opus agentic loop that analyzes HRRR model imagery and sounding data
- Identifies the hatch area (tornado target zone), extracts the Bunkers Right storm motion vector, and calculates a positioning location for chasers
- Generates an annotated CONUS reflectivity map and posts it to X with a brief caption
- Runs daily at 1430Z (March–June) via GitHub Actions, after the HRRR 12Z run is available

---

## Cost

| Bot | Per activation | Notes |
|-----|---------------|-------|
| Otto | $0 | No AI — purely image fetching and posting |
| Chase | ~$2–5 | Claude Opus 4.6 with adaptive thinking; ~6 agent turns with CONUS map images and sounding charts per run |

Chase only activates on ENH+ days (~15–25 per season), putting the **seasonal cost at roughly $30–125**.

---

## Tech Stack

- Python 3.12
- [tweepy](https://github.com/tweepy/tweepy) — X API
- [anthropic](https://github.com/anthropics/anthropic-sdk-python) — Claude API (Chase only)
- [Pillow](https://python-pillow.org/) — map annotation (Chase only)
- [python-dotenv](https://github.com/theskumar/python-dotenv) — environment management

## Environment

API keys live in `.env` (never committed):

```
X_API_KEY
X_API_SECRET
X_ACCESS_TOKEN
X_ACCESS_TOKEN_SECRET
ANTHROPIC_API_KEY
```
