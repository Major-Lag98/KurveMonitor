# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-script monitor (`kurve_monitor.py`) that watches apartment availability at Kurve on Wilshire. It polls the SightMap JSON API that backs the floorplan widget on kurveonwilshire.com, compares available units to the last saved snapshot, posts new/gone units to Discord, and (optionally) logs daily prices to a Google Sheet. It runs every 30 minutes on GitHub Actions. There are no tests, linter, or build step.

## Commands

```bash
pip install -r requirements.txt        # local venv lives in venv/ (Windows: venv\Scripts\activate)
python kurve_monitor.py                # one check; console output only
python kurve_monitor.py -v             # also writes DEBUG logs to kurve_monitor.log
```

Running locally with a populated `.env` has real side effects: it sends Discord messages, writes to the Google Sheet, and overwrites `kurve_units_seen.json`. To test without notifying or touching the sheet, blank out `KURVE_DISCORD_WEBHOOK` / `GOOGLE_SHEET_ID` for that run.

## Architecture

One run of `main()`:
1. `fetch_current_units()` GETs the hardcoded `BOOTSTRAP_URL` (public, no auth) and checks that `data.units` and `data.floor_plans` exist. If the shape is wrong it raises `ScrapeError` and the run exits **without saving state**. This guard keeps an API change from wiping the snapshot and firing a flood of false "no longer available" alerts, so keep it intact.
2. `normalize_units()` joins units to floor plans via `floor_plan_id` (they are parallel lists in the payload). Use the plan's `filter_label` as its display name; the raw `name` field is itself a JSON-encoded string.
3. A unit counts as available if it has a `price` or `available_on` (`is_available`).
4. `log_prices_to_sheet()` writes a wide unit-by-date sheet ("Prices" worksheet): one row per unit ID, one column per LA-local date. Rows are never deleted; off-market days stay blank. The first `UNIT_INFO_HEADERS` columns must match the sheet's header row exactly, or the function raises. Sheet failures are logged but don't stop the run.
5. The run diffs against `kurve_units_seen.json` and calls `notify()` for each new or gone unit. If there is no previous state, it records a baseline and sends nothing. Plans listed in `SPECIAL_PLANS` get `KURVE_DISCORD_MENTION` prepended.
6. `save_current()` writes only the *available* units back to the state file.

All dates and timestamps use `America/Los_Angeles` (`LOCAL_TZ`), not the machine clock, because the Actions runner is on UTC. `tzdata` is in requirements for that reason.

## State file and CI

- `kurve_units_seen.json` is committed on purpose: it's the persistent snapshot between runs. `.github/workflows/kurve-monitor.yml` runs the script, then commits and pushes the file as `github-actions[bot]` whenever it changes. The bot pushes to `main` often, so pull before pushing, and expect conflicts on this file if you've run the script locally (prefer the remote copy).
- The workflow has a `concurrency` group so two runs can't race on the push, and `workflow_dispatch` for manual runs.

## Configuration

Secrets come from environment variables, loaded from a git-ignored `.env` locally and from repo secrets in CI:
- `KURVE_DISCORD_WEBHOOK`, `KURVE_DISCORD_MENTION`
- `GOOGLE_SHEET_ID` (leave empty to turn sheet logging off)
- `KURVE_GOOGLE_SERVICE_ACCOUNT_JSON`: the full service-account key JSON as one string, not a file path

The script docstring mentions a `.env.example`, but that file doesn't exist in the repo.

If the monitor suddenly reports 0 units or shape errors, SightMap probably changed the API path. Open the embed (https://sightmap.com/embed/d7p1mo7xpkx) with the browser network tab open, find the new endpoint, and update `BOOTSTRAP_URL`.
