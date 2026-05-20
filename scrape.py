#!/usr/bin/env python3
"""Build events.ics from improvworksberlin.com/shows.

The page is a Wix site whose Events widget ships every upcoming show as
structured JSON inside <script id="wix-warmup-data">. We parse that JSON
(no HTML scraping) and emit a single iCalendar file.

On failure, dumps debug-page.html and debug-warmup.json next to this
script so a CI artifact step can upload them for inspection.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar, Event

SOURCE_URL = "https://www.improvworksberlin.com/shows"
EVENT_PAGE_BASE = "https://www.improvworksberlin.com/event-info/"
HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "events.ics"
DEBUG_HTML = HERE / "debug-page.html"
DEBUG_JSON = HERE / "debug-warmup.json"

# Wix appears to render the Events widget conditionally on visitor
# locale (the venue is in Berlin and serves German), so hint de-DE in
# case GitHub's US runner IP would otherwise get an English/empty
# variant. We do NOT override Accept-Encoding — requests negotiates it,
# and we install `brotli` so any encoding the server picks decodes
# correctly.
REQUEST_HEADERS = {
    "User-Agent": "improvworks-ics/1.0 (+https://github.com/)",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def fail(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    log(f"error: {msg}")
    sys.exit(1)


def fetch_page(url: str) -> requests.Response:
    log(f"fetching {url}")
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
    log(f"  status={resp.status_code} final_url={resp.url} bytes={len(resp.content)}")
    if resp.history:
        for h in resp.history:
            log(f"  redirect: {h.status_code} {h.url}")
    resp.raise_for_status()
    DEBUG_HTML.write_text(resp.text, encoding="utf-8")
    log(f"  wrote {DEBUG_HTML}")
    return resp


def extract_warmup(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("script", id="wix-warmup-data")
    if tag is None or not tag.string:
        log("diagnostics: wix-warmup-data script tag not found")
        scripts = soup.find_all("script")
        log(f"  total <script> tags on page: {len(scripts)}")
        ids_seen = sorted({s.get("id") for s in scripts if s.get("id")})
        log(f"  script @id values seen: {ids_seen}")
        fail("<script id='wix-warmup-data'> not found on page")
    payload = json.loads(tag.string)
    DEBUG_JSON.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log(f"  wrote {DEBUG_JSON} ({len(tag.string)} chars source)")
    return payload


def find_events(payload: object) -> list[dict]:
    """Locate the events list within the Wix warmup payload.

    Match heuristic: a list whose first element looks like a Wix event
    (has a 'scheduling' or 'startDate'/'endDate' shape). Keeps working
    even if Wix renames the wrapping key.
    """

    def looks_like_event(obj: object) -> bool:
        if not isinstance(obj, dict):
            return False
        if isinstance(obj.get("scheduling"), dict):
            return True
        if "startDate" in obj and "endDate" in obj and "title" in obj:
            return True
        return False

    paths_with_events_key: list[str] = []

    def walk(obj, path: str = ""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                p = f"{path}/{k}"
                if k == "events" and isinstance(v, list):
                    paths_with_events_key.append(f"{p} (len={len(v)})")
                if (
                    isinstance(v, list)
                    and v
                    and looks_like_event(v[0])
                ):
                    log(f"  events list at {p} ({len(v)} items)")
                    return v
                r = walk(v, p)
                if r is not None:
                    return r
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                r = walk(v, f"{path}[{i}]")
                if r is not None:
                    return r
        return None

    events = walk(payload)
    if not events:
        log("diagnostics: no event-shaped list found in warmup payload")
        top_keys = list(payload.keys()) if isinstance(payload, dict) else []
        log(f"  top-level keys: {top_keys}")
        if paths_with_events_key:
            log("  saw 'events' keys at these paths (but none looked event-shaped):")
            for p in paths_with_events_key:
                log(f"    {p}")
        else:
            log("  no 'events' key seen anywhere in the payload")
        fail("no events list found in warmup data")
    return events


def parse_iso_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def build_calendar(events: list[dict]) -> Calendar:
    cal = Calendar()
    cal.add("prodid", "-//improvworks-ics//github-actions//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "ImprovWorks Berlin – Shows")
    cal.add("x-wr-timezone", "Europe/Berlin")
    cal.add("method", "PUBLISH")

    now = datetime.now(timezone.utc)
    kept = 0
    skipped_status = 0
    skipped_no_dates = 0
    for ev in events:
        if ev.get("status") != 0:
            skipped_status += 1
            continue
        sched = ev.get("scheduling", {}).get("config", {})
        if not sched.get("startDate") or not sched.get("endDate"):
            skipped_no_dates += 1
            continue
        start = parse_iso_utc(sched["startDate"])
        end = parse_iso_utc(sched["endDate"])

        loc = ev.get("location") or {}
        loc_parts = [s for s in (loc.get("name"), loc.get("address")) if s]
        location_str = ", ".join(loc_parts)

        slug = ev.get("slug") or ""
        event_url = f"{EVENT_PAGE_BASE}{slug}" if slug else None

        vevent = Event()
        vevent.add("uid", f"{ev['id']}@improvworksberlin.com")
        vevent.add("dtstamp", now)
        vevent.add("dtstart", start)
        vevent.add("dtend", end)
        vevent.add("summary", ev.get("title") or "Improv show")

        description_lines = []
        if ev.get("description"):
            description_lines.append(ev["description"])
        if event_url:
            description_lines.append(event_url)
        if description_lines:
            vevent.add("description", "\n\n".join(description_lines))

        if location_str:
            vevent.add("location", location_str)

        coords = loc.get("coordinates") or {}
        if "lat" in coords and "lng" in coords:
            vevent.add("geo", (coords["lat"], coords["lng"]))

        if event_url:
            vevent.add("url", event_url)

        if ev.get("modified"):
            try:
                vevent.add("last-modified", parse_iso_utc(ev["modified"]))
            except ValueError:
                pass

        vevent.add("status", "CONFIRMED")
        cal.add_component(vevent)
        kept += 1

    log(
        f"kept {kept} of {len(events)} events "
        f"(skipped: status!=0 → {skipped_status}, missing dates → {skipped_no_dates})"
    )
    return cal


def main() -> int:
    resp = fetch_page(SOURCE_URL)
    payload = extract_warmup(resp.text)
    events = find_events(payload)
    cal = build_calendar(events)
    OUTPUT.write_bytes(cal.to_ical())
    log(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
