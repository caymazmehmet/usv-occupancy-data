#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DAY_NAMES = {0: "Sunday", 1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday", 5: "Friday", 6: "Saturday"}


def room_floor_guess(room_code: str) -> str:
    c = str(room_code or "").upper().replace(" ", "")
    m = re.match(r"^E(\d)", c)
    if m:
        return m.group(1)
    m = re.match(r"^ED(\d)", c)
    if m:
        return m.group(1)
    return "unknown"


def parse_hhmm(value: Any) -> Optional[time]:
    m = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*$", str(value or ""))
    if not m:
        return None
    h = int(m.group(1)); mn = int(m.group(2) or 0)
    if not (0 <= h <= 23 and 0 <= mn <= 59):
        return None
    return time(h, mn)


def get_week_index(selected: date, week1_start: str) -> Optional[int]:
    try:
        start = datetime.fromisoformat(week1_start).date()
    except Exception:
        return None
    if selected < start:
        return None
    return ((selected - start).days // 7) + 1


def event_matches(ev: Dict[str, Any], selected_dt: datetime, week1_start: Optional[str]) -> bool:
    selected_date = selected_dt.date()
    if ev.get("date"):
        try:
            if datetime.fromisoformat(str(ev["date"])).date() != selected_date:
                return False
        except Exception:
            return False
    elif ev.get("dayIndex") is not None:
        # JS getDay: Sunday=0, Monday=1
        if int(ev["dayIndex"]) != ((selected_dt.weekday() + 1) % 7):
            return False

    weeks = ev.get("weeks")
    if weeks and week1_start:
        w = get_week_index(selected_date, week1_start)
        if w is not None and w not in [int(x) for x in weeks if str(x).isdigit()]:
            return False

    start = parse_hhmm(ev.get("start") or ev.get("startTime"))
    end = parse_hhmm(ev.get("end") or ev.get("endTime"))
    if not start or not end:
        return False
    start_dt = datetime.combine(selected_date, start)
    end_dt = datetime.combine(selected_date, end)
    return start_dt <= selected_dt < end_dt


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate USV Corp E occupancy JSON generated from Orar.")
    ap.add_argument("--file", default="data/occupancy-corp-e.json")
    ap.add_argument("--date", help="Optional test date, YYYY-MM-DD")
    ap.add_argument("--time", help="Optional test time, HH:MM")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: file not found: {path}")
        return 2

    data = json.loads(path.read_text(encoding="utf-8"))
    meta = data.get("meta", {}) if isinstance(data, dict) else {}
    rooms = data.get("rooms", {}) if isinstance(data, dict) else {}
    if not isinstance(rooms, dict):
        print("ERROR: JSON format invalid: expected object with 'rooms'.")
        return 3

    total_events = 0
    rooms_no_events = []
    rooms_no_url = []
    bad_time = []
    bad_day = []
    duplicate_keys = Counter()
    events_by_floor = Counter()
    rooms_by_floor = Counter()
    source_counter = Counter()
    busy_at_selected: Dict[str, List[Dict[str, Any]]] = {}

    selected_dt = None
    if args.date and args.time:
        selected_dt = datetime.fromisoformat(args.date + "T" + args.time + ":00")

    for code, info in rooms.items():
        code = str(code).upper()
        floor = room_floor_guess(code)
        rooms_by_floor[floor] += 1
        events = info.get("events", []) if isinstance(info, dict) else []
        if not info.get("url"):
            rooms_no_url.append(code)
        if not events:
            rooms_no_events.append(code)
        total_events += len(events)

        seen_for_room = set()
        for idx, ev in enumerate(events):
            events_by_floor[floor] += 1
            source_counter[str(ev.get("source", "unknown"))] += 1
            start = parse_hhmm(ev.get("start") or ev.get("startTime"))
            end = parse_hhmm(ev.get("end") or ev.get("endTime"))
            if not start or not end or not (start < end):
                bad_time.append({"room": code, "index": idx, "start": ev.get("start"), "end": ev.get("end"), "raw": ev.get("raw", "")[:120]})
            if ev.get("date"):
                try:
                    datetime.fromisoformat(str(ev["date"]))
                except Exception:
                    bad_day.append({"room": code, "index": idx, "date": ev.get("date")})
            elif ev.get("dayIndex") is not None:
                try:
                    di = int(ev["dayIndex"])
                    if di < 0 or di > 6:
                        bad_day.append({"room": code, "index": idx, "dayIndex": ev.get("dayIndex")})
                except Exception:
                    bad_day.append({"room": code, "index": idx, "dayIndex": ev.get("dayIndex")})

            dup_key = (ev.get("date"), ev.get("dayIndex"), ev.get("start"), ev.get("end"), ev.get("subject"), ev.get("teacher"), ev.get("group"))
            if dup_key in seen_for_room:
                duplicate_keys[code] += 1
            seen_for_room.add(dup_key)

            if selected_dt and event_matches(ev, selected_dt, meta.get("week1StartDate")):
                busy_at_selected.setdefault(code, []).append(ev)

    report = {
        "file": str(path),
        "generatedAt": meta.get("generatedAt"),
        "source": meta.get("source"),
        "week1StartDate": meta.get("week1StartDate"),
        "rooms": len(rooms),
        "events": total_events,
        "roomsByFloorGuess": dict(sorted(rooms_by_floor.items())),
        "eventsByFloorGuess": dict(sorted(events_by_floor.items())),
        "eventsBySource": dict(source_counter),
        "roomsWithoutEventsCount": len(rooms_no_events),
        "roomsWithoutEvents": rooms_no_events,
        "roomsWithoutUrlCount": len(rooms_no_url),
        "roomsWithoutUrl": rooms_no_url,
        "badTimeCount": len(bad_time),
        "badTimeSamples": bad_time[:25],
        "badDateOrDayCount": len(bad_day),
        "badDateOrDaySamples": bad_day[:25],
        "duplicateEventRooms": dict(duplicate_keys),
    }
    if selected_dt:
        report["selectedDateTime"] = selected_dt.isoformat(timespec="minutes")
        report["busyRoomCount"] = len(busy_at_selected)
        report["busyRooms"] = sorted(busy_at_selected.keys())
        report["busySamples"] = {k: v[:3] for k, v in list(sorted(busy_at_selected.items()))[:20]}

    out_json = Path("data/occupancy-validation-report.json")
    out_txt = Path("data/occupancy-validation-report.txt")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = []
    lines.append("USV Corp E Occupancy Validation Report")
    lines.append("=" * 42)
    lines.append(f"File: {path}")
    lines.append(f"Generated at: {report.get('generatedAt')}")
    lines.append(f"Rooms: {len(rooms)}")
    lines.append(f"Events: {total_events}")
    lines.append(f"Rooms by floor guess: {report['roomsByFloorGuess']}")
    lines.append(f"Events by source: {report['eventsBySource']}")
    lines.append(f"Rooms without events: {len(rooms_no_events)}")
    if rooms_no_events:
        lines.append("  " + ", ".join(rooms_no_events[:40]) + ("..." if len(rooms_no_events) > 40 else ""))
    lines.append(f"Rooms without URL: {len(rooms_no_url)}")
    lines.append(f"Bad time rows: {len(bad_time)}")
    lines.append(f"Bad date/day rows: {len(bad_day)}")
    lines.append(f"Duplicate event rooms: {dict(duplicate_keys)}")
    if selected_dt:
        lines.append(f"Busy at {selected_dt.isoformat(timespec='minutes')}: {len(busy_at_selected)} rooms")
        lines.append("  " + ", ".join(sorted(busy_at_selected.keys())[:80]))
    lines.append("")
    lines.append(f"Written: {out_json}")
    lines.append(f"Written: {out_txt}")
    out_txt.write_text("\n".join(lines), encoding="utf-8")

    print("\n".join(lines))
    if total_events <= 0 or len(rooms) <= 0:
        return 4
    if bad_time or bad_day:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
