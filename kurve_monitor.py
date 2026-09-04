#!/usr/bin/env python3
"""
Kurve on Wilshire - unit availability monitor
================================================

kurveonwilshire.com/floorplans/ embeds a SightMap interactive map widget
(https://sightmap.com/embed/d7p1mo7xpkx) that gets its unit/pricing data by
calling a JSON API behind the scenes:

    https://sightmap.com/app/api/v1/9zw47dl6w87/sightmaps/89014

That endpoint was found by watching the widget's network traffic, and it
turns out to be a plain public JSON API - no auth, no session/cookie state,
no Referer check required. So instead of spinning up a full headless
browser (Playwright) just to let JavaScript re-discover that same URL every
run, this script hits it directly with a normal HTTP GET. Much lighter,
much faster, and no browser dependency to install or maintain.

The trade-off: if SightMap ever changes this URL or the shape of its
response, a hardcoded call won't "notice" and adapt the way watching all
network traffic would have. To cover that, every response is checked for
the expected `units`/`floor_plans` shape before use - if that check fails,
the run stops with a clear error instead of silently treating a broken
response as "zero units available" (which would otherwise wipe out your
saved state and fire a flood of false "no longer available" alerts).

Each run:
  1. Fetches the bootstrap JSON directly and validates its shape.
  2. Compares currently-available units to the last saved snapshot.
  3. Notifies you about any unit that's newly available or newly gone.
  4. Saves the new snapshot for next time.

The first run just records a baseline - there's nothing to compare against
yet, so it won't send a notification.

Setup: pip install requests python-dotenv
       (and, if using the sheet log: gspread google-auth)

Secrets/config that vary per-person or per-machine (Discord webhook,
Google Sheet ID, Google service account key) are read from
environment variables rather than hardcoded, so this file is safe to
commit to git as-is. Populate them locally via a ".env" file that sits
next to this script and is NOT committed (add it to .gitignore) - see
".env.example" for the full list of variables and their format. On a new
machine, just copy your real ".env" over (never commit it) and everything
else - including this script - can come straight from git.
"""
import os
import argparse
import json
import logging
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()  # loads variables from a local .env file, if present, into os.environ

# Always use LA time for date-stamped output (log timestamps, the Google
# Sheet's date column) instead of the machine's local clock. This matters
# because the machine running this script isn't always in the same
# timezone - your own PC happens to be set to Pacific, but a GitHub
# Actions runner defaults to UTC, so datetime.now() there would return a
# time up to 7-8 hours ahead of LA, rolling the date over too early.
LOCAL_TZ = ZoneInfo("America/Los_Angeles")

LOG_FILE = Path(__file__).parent / "kurve_monitor.log"
WRITE_OUTPUT_TO_FILE = False

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.propagate = False

console_handler = logging.StreamHandler(sys.stderr)
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)

file_logger = logging.getLogger(__name__ + ".file")
file_logger.setLevel(logging.DEBUG)
file_logger.propagate = False


def parse_args():
    parser = argparse.ArgumentParser(description="Kurve unit availability monitor")
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose debug logging",
    )
    return parser.parse_args()


def setup_logging(verbose=False):
    global WRITE_OUTPUT_TO_FILE
    WRITE_OUTPUT_TO_FILE = verbose
    if verbose:
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(console_formatter)
        logger.addHandler(file_handler)
        file_logger.addHandler(file_handler)
        logger.debug("Verbose mode enabled; logging to console and %s", LOG_FILE)


def output(message, level=logging.INFO):
    print(message)
    if WRITE_OUTPUT_TO_FILE:
        file_logger.log(level, message)

# ---------------------------------------------------------------------------
# CONFIG - edit these
# ---------------------------------------------------------------------------

# The widget embed is https://sightmap.com/embed/d7p1mo7xpkx - that page's
# own JS calls the URL below to get unit/pricing data. Found via browser
# devtools network tab. If this monitor ever starts reporting 0 units for
# no reason, that's the first thing to re-check (open the embed URL above
# in a browser, watch the network tab, see if the API path changed).
BOOTSTRAP_URL = "https://sightmap.com/app/api/v1/9zw47dl6w87/sightmaps/89014"

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

STATE_FILE = Path(__file__).parent / "kurve_units_seen.json"

# Discord webhook URL: channel Settings > Integrations > Webhooks.
DISCORD_WEBHOOK_URL = os.environ.get("KURVE_DISCORD_WEBHOOK", "")

# Special plan names to @mention in Discord
SPECIAL_PLANS = ["2","8","9","16","10","18","18.1","22","25.1","26","26.1"]
DISCORD_MENTION = os.environ.get("KURVE_DISCORD_MENTION", "")

# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# GOOGLE SHEETS PRICE LOG (optional) - leave GOOGLE_SHEET_ID blank to disable.
# Logs one row per unit and one column per date; a unit's cell for a given
# day is only written when that unit is currently available, so days it's
# off the market are simply left blank - no rows are ever deleted, so a
# unit that comes back later just resumes filling in from that date on.
# ---------------------------------------------------------------------------

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")  # the long id in the sheet's URL: .../d/<THIS_PART>/edit
GOOGLE_SHEET_WORKSHEET_NAME = "Prices"

# Full contents of the service account JSON key, as one string - not a file
# path. Paste the whole JSON object as the value of KURVE_GOOGLE_SERVICE_
# ACCOUNT_JSON in your .env file. This means the key file itself never
# needs to exist on disk next to the script (or in the repo) at all.
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("KURVE_GOOGLE_SERVICE_ACCOUNT_JSON", "")

UNIT_INFO_HEADERS = ["Unit ID", "Unit Number", "Floor Plan", "Bed/Bath", "Sqft"]

# ---------------------------------------------------------------------------


def build_floor_plan_lookup(floor_plans):
    """floor_plans is data['floor_plans']: a list of plan records keyed by id.
    Note: the plan's own 'name' field is itself a JSON-encoded string (e.g.
    '{"name":"22","provider_id":"152"}') - 'filter_label' is the plain,
    human-readable plan name and is what we actually want to display."""
    lookup = {}
    for fp in floor_plans or []:
        lookup[str(fp.get("id"))] = {
            "name": fp.get("filter_label") or fp.get("name"),
            "bedroom_label": fp.get("bedroom_label"),
            "bathroom_label": fp.get("bathroom_label"),
        }
    return lookup


def normalize_units(payload_data, found):
    """payload_data is the dict at response['data']. Units and floor plans
    are separate parallel lists in the real payload (a unit only carries a
    floor_plan_id, not a nested floor plan object) - join them here and
    store a small, clean record instead of the full raw unit (which also
    carries a large nested static_expenses tree we don't need)."""
    floor_plan_lookup = build_floor_plan_lookup(payload_data.get("floor_plans"))
    units = payload_data.get("units") or []
    logger.debug(
        "normalize_units: %s units, %s floor plans in this payload",
        len(units), len(floor_plan_lookup),
    )
    for u in units:
        uid = str(u.get("id"))
        found[uid] = {
            "id": uid,
            "unit_number": u.get("unit_number"),
            "display_unit_number": u.get("display_unit_number"),
            "area": u.get("area"),
            "display_area": u.get("display_area"),
            "price": u.get("price"),
            "display_price": u.get("display_price"),
            "available_on": u.get("available_on"),
            "display_available_on": u.get("display_available_on"),
            "floor_plan": floor_plan_lookup.get(str(u.get("floor_plan_id")), {}),
        }


class ScrapeError(RuntimeError):
    """Raised when the bootstrap response can't be fetched or doesn't look
    like the SightMap unit payload we expect - a signal to stop and
    investigate rather than silently proceeding with bad data."""


def fetch_current_units():
    """Hit the SightMap bootstrap JSON API directly and return a dict of
    unit records keyed by unit id."""
    logger.debug("Fetching bootstrap JSON: %s", BOOTSTRAP_URL)
    try:
        resp = requests.get(BOOTSTRAP_URL, headers=REQUEST_HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise ScrapeError(f"Request to bootstrap URL failed: {exc}") from exc

    try:
        payload = resp.json()
    except ValueError as exc:
        raise ScrapeError(f"Bootstrap response wasn't valid JSON: {exc}") from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or "units" not in data or "floor_plans" not in data:
        raise ScrapeError(
            "Bootstrap response no longer matches the expected shape "
            "(missing 'units'/'floor_plans' under 'data'). SightMap "
            "likely changed something - needs investigation, not a "
            "silent retry."
        )

    found = {}
    normalize_units(data, found)
    logger.debug("Total unit-shaped records found: %s", len(found))
    return found


def is_available(unit):
    return bool(unit.get("price") or unit.get("available_on"))


def load_previous():
    logger.debug("Loading previous state from %s", STATE_FILE)
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            logger.debug("Loaded %s previous unit records", len(data))
            return data
        except Exception as exc:
            logger.error("Failed to read previous state file: %s", exc)
            return {}
    logger.debug("No previous state file found")
    return {}


def save_current(units):
    logger.debug("Saving %s available unit records to %s", len(units), STATE_FILE)
    try:
        STATE_FILE.write_text(json.dumps(units, indent=2))
    except Exception as exc:
        logger.error("Failed to save current state file: %s", exc)
        raise


def log_prices_to_sheet(available_units):
    """Write today's price for every currently-available unit into a wide
    unit-by-date price sheet (one row per unit, one column per date).

    Units are never removed from the sheet when they go off-market - we
    simply stop writing new cells for them, which leaves those days blank.
    If a unit comes back later, its existing row is reused and filling in
    resumes at the current date, so the gap in between stays blank exactly
    as it should.
    """
    if not GOOGLE_SHEET_ID:
        return  # feature disabled - no sheet configured

    try:
        import gspread
        from gspread.exceptions import WorksheetNotFound
        from gspread.utils import rowcol_to_a1
    except ImportError:
        raise RuntimeError(
            "GOOGLE_SHEET_ID is set but gspread isn't installed. Run:\n"
            "  pip install gspread google-auth"
        )

    logger.debug("Logging prices for %s available unit(s) to Google Sheets", len(available_units))

    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError(
            "GOOGLE_SHEET_ID is set but KURVE_GOOGLE_SERVICE_ACCOUNT_JSON is "
            "empty. Set it in your .env file to the full contents of your "
            "service account JSON key (see .env.example)."
        )
    try:
        service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"KURVE_GOOGLE_SERVICE_ACCOUNT_JSON isn't valid JSON: {exc}"
        ) from exc

    gc = gspread.service_account_from_dict(service_account_info)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet(GOOGLE_SHEET_WORKSHEET_NAME)
    except WorksheetNotFound:
        logger.info("Creating '%s' worksheet with header row", GOOGLE_SHEET_WORKSHEET_NAME)
        ws = sh.add_worksheet(title=GOOGLE_SHEET_WORKSHEET_NAME, rows=1000, cols=50)
        ws.append_row(UNIT_INFO_HEADERS)

    all_values = ws.get_all_values()
    if not all_values:
        ws.append_row(UNIT_INFO_HEADERS)
        all_values = [UNIT_INFO_HEADERS]

    header = all_values[0]

    info_col_count = len(UNIT_INFO_HEADERS)
    if header[:info_col_count] != UNIT_INFO_HEADERS:
        raise RuntimeError(
            "The sheet's header row doesn't match UNIT_INFO_HEADERS "
            f"(expected {UNIT_INFO_HEADERS!r} in the first {info_col_count} "
            f"column(s), found {header[:info_col_count]!r}). This usually "
            "means the sheet was created before a header change (e.g. "
            "adding Sqft). Update the sheet's header row to match, or "
            "delete it and let the script recreate it on the next run "
            "(you'll lose rows already logged)."
        )

    today_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")

    if today_str in header:
        date_col = header.index(today_str) + 1  # 1-indexed
    else:
        date_col = len(header) + 1
        ws.update_cell(1, date_col, today_str)
        header = header + [today_str]
        logger.debug("Added new date column %s at index %s", today_str, date_col)

    row_by_uid = {
        row[0]: i for i, row in enumerate(all_values[1:], start=2) if row and row[0]
    }

    cell_updates = []
    new_rows = []

    for uid, unit in available_units.items():
        price = unit.get("price")
        if price is None:
            continue  # nothing to log yet for this unit today

        if uid in row_by_uid:
            cell_updates.append({
                "range": rowcol_to_a1(row_by_uid[uid], date_col),
                "values": [[price]],
            })
        else:
            fp = unit.get("floor_plan") or {}
            info_values = {
                "Unit ID": uid,
                "Unit Number": unit.get("display_unit_number") or unit.get("unit_number") or "",
                "Floor Plan": fp.get("name") or "?",
                "Bed/Bath": f"{fp.get('bedroom_label', '?')}/{fp.get('bathroom_label', '?')}",
                "Sqft": unit.get("area") or "",
            }
            row = [info_values.get(h, "") for h in UNIT_INFO_HEADERS]
            row += [""] * (len(header) - len(row))
            row[date_col - 1] = price
            new_rows.append(row)

    if cell_updates:
        ws.batch_update(cell_updates)
        logger.debug("Updated %s existing unit price cell(s)", len(cell_updates))

    if new_rows:
        ws.append_rows(new_rows)
        logger.debug("Added %s new unit row(s) to the price sheet", len(new_rows))


def notify(message):
    logger.info("Notify: %s", message)
    output(message)

    if DISCORD_WEBHOOK_URL:
        try:
            logger.debug("Sending Discord notification to webhook URL")
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }
            req = urllib.request.Request(
                DISCORD_WEBHOOK_URL,
                data=json.dumps({"content": message}).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            logger.error("discord notify failed: %s", e)
            output(f"  [discord notify failed: {e}]", level=logging.ERROR)


def describe(unit):
    fp = unit.get("floor_plan") or {}
    fp_name = fp.get("name", "?")
    
    mention = f"{DISCORD_MENTION} " if fp_name in SPECIAL_PLANS else ""
    message = (
        f"{mention}Unit {unit.get('display_unit_number') or unit.get('unit_number')} "
        f"- Plan {fp_name} "
        f"({fp.get('bedroom_label', '?')}/{fp.get('bathroom_label', '?')}), "
        f"{unit.get('display_area', 'size n/a')}, "
        f"{unit.get('display_price', 'price n/a')}, "
        f"{unit.get('display_available_on', 'availability unknown')}"
    )
    return message


def main(verbose=False):
    setup_logging(verbose)

    logger.info("Starting Kurve availability check")
    output(f"[{datetime.now(LOCAL_TZ).isoformat(timespec='seconds')}] checking Kurve availability...")

    try:
        current = fetch_current_units()
    except ScrapeError as exc:
        logger.error("Scrape failed: %s", exc)
        output(f"Scrape failed: {exc}. Nothing saved.", level=logging.ERROR)
        return

    if not current:
        message = "No unit-shaped data captured this run. Nothing saved."
        logger.error(message)
        output(message, level=logging.ERROR)
        return

    available = {uid: u for uid, u in current.items() if is_available(u)}
    logger.debug("Filtered %s available units", len(available))

    try:
        log_prices_to_sheet(available)
    except Exception as exc:
        logger.error("Failed to log prices to Google Sheets: %s", exc)
        output(f"  [Google Sheets logging failed: {exc}]", level=logging.ERROR)

    previous = load_previous()

    new_ids = set(available) - set(previous)
    gone_ids = set(previous) - set(available)
    logger.info(
        "Comparison result: %s new, %s gone, %s total currently available",
        len(new_ids),
        len(gone_ids),
        len(available),
    )

    if not previous:
        logger.info("Baseline run, saving current available units")
        output(f"First run - recorded {len(available)} available unit(s) as the baseline.")
    else:
        for uid in new_ids:
            notify(f"New unit available at Kurve on Wilshire: {describe(available[uid])}")
        for uid in gone_ids:
            logger.info("Unit no longer available: %s", uid)
            notify(f"No longer listed as available: {describe(previous[uid])}")
        if not new_ids and not gone_ids:
            logger.info("No availability changes detected")
            output(f"No change. {len(available)} unit(s) currently available.")

    output(f"Job done: {len(new_ids)} new, {len(gone_ids)} gone, {len(available)} total available.")

    save_current(available)
    logger.info("Saved current state and completed run")


if __name__ == "__main__":
    args = parse_args()
    main(verbose=args.verbose)