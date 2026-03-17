# Supercell Syn Bot

## Project Goal
An X (Twitter) bot that automates daily storm chasing content posts.

## Phase 1: SPC Convective Outlook Posts
- Daily, pull convective outlook images from https://www.spc.noaa.gov/products/outlook/
- Post images for Day 1, Day 2, Day 3, and Day 4-8 outlooks to X
- Use tweepy for the X API (proof-of-concept already working in `post_poc.py`)

## Phase 2: AI-Generated Storm Chase Forecast
- Generate an AI image predicting a recommended storm chase base location for the day
- Parameters and data sources (e.g., Pivotal Weather charts) to be defined when Phase 1 is complete

## Tech Stack
- Python 3.13 (venv: venv-supercellsyn-bot-313)
- tweepy for X API
- python-dotenv for env management

## Environment
- API keys live in `.env` (X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET)
- Never commit or expose `.env` values

## Scheduling
- Bot runs daily at 00:00 UTC via launchd (`com.supercellsyn.spcbot.plist`)
- 00:00 UTC = 5:00 PM MST (Nov–Mar) / 6:00 PM MDT (Mar–Nov)
- The plist has two `StartCalendarInterval` entries (Hour 17 and 18) to cover both DST states
- The script itself checks UTC time for late-run detection
