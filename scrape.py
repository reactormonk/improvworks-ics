#!/usr/bin/env python3
"""Build events.ics from improvworksberlin.com/shows.

The page is a Wix site whose Events widget ships every upcoming show as
structured JSON inside <script id="wix-warmup-data">. We parse that JSON
(no HTML scraping) and emit a single iCalendar file.
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
OUTPUT = Path(__file__).resolve().parent / "events.ics"
USER_AGENT = "improvworks-ics/1.0 (+https://github.com/)"


def fetch_warmup_data(url: str) -> dict:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    tag = soup.find("script", id="wix-warmup-data")
    if tag is None or not tag.string:
        sys.exit("error: <script id='wix-warmup-data'> not found on page")
    return json.loads(tag.string)


def find_events(payload: object) -> list[dict]:
    """Locate the events list within the Wix warmup payload."""

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if (
                    k == "events"
                    and isinstance(v, list)
                    and v
                    and isinstance(v[0], dict)
                    and "id" in v[0]
                    and "scheduling" in v[0]
                ):
                    return v
                r = walk(v)
                if r is not None:
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = walk(v)
                if r is not None:
                    return r
        return None

    events = walk(payload)
    if not events:
        sys.exit("error: no events list found in warmup data")
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
    for ev in events:
        if ev.get("status") != 0:
            continue
        sched = ev.get("scheduling", {}).get("config", {})
        if not sched.get("startDate") or not sched.get("endDate"):
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

    print(f"emitted {kept} of {len(events)} events", file=sys.stderr)
    return cal


def main() -> int:
    payload = fetch_warmup_data(SOURCE_URL)
    events = find_events(payload)
    cal = build_calendar(events)
    OUTPUT.write_bytes(cal.to_ical())
    print(f"wrote {OUTPUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
