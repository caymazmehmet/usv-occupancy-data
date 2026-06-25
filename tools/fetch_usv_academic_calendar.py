"""Fetch USV academic calendar and write data/academic-calendar.json.

This script is intentionally conservative: it first tries to parse the official
USV calendar page, then falls back to the known 2025-2026 general calendar so
that the map never breaks if the web layout changes.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

CALENDAR_URL = "https://usv.ro/academic/calendar-academic/"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "academic-calendar.json"

FALLBACK = {
    "meta": {
        "academicYear": "2025-2026",
        "sourceUrl": CALENDAR_URL,
        "fetchedAt": None,
        "parser": "fallback",
    },
    "teachingPeriods": [
        {"name": "Semester I - Teaching", "start": "2025-09-29", "end": "2025-12-24"},
        {"name": "Semester I - Teaching", "start": "2026-01-08", "end": "2026-01-18"},
        {"name": "Semester II - Teaching", "start": "2026-02-23", "end": "2026-04-12"},
        {"name": "Semester II - Teaching", "start": "2026-04-20", "end": "2026-06-07"},
    ],
    "vacations": [
        {"name": "Winter vacation", "start": "2025-12-25", "end": "2026-01-07"},
        {"name": "Intersemester vacation", "start": "2026-02-16", "end": "2026-02-22"},
        {"name": "Semester II vacation", "start": "2026-04-13", "end": "2026-04-19"},
    ],
    "sessions": [
        {"name": "Winter exam session", "start": "2026-01-19", "end": "2026-02-08"},
        {"name": "Winter resit session", "start": "2026-02-09", "end": "2026-02-15"},
        {"name": "Summer exam session", "start": "2026-06-08", "end": "2026-06-28"},
        {"name": "Summer resit / vacation", "start": "2026-06-29", "end": "2026-07-05"},
    ],
    "specialNoClassDays": [
        {"date": "2025-12-01", "name": "No regular teaching activity"},
        {"date": "2026-04-10", "name": "No regular teaching activity"},
        {"date": "2026-05-01", "name": "No regular teaching activity"},
        {"date": "2026-06-01", "name": "No regular teaching activity"},
    ],
}

DATE_RE = re.compile(r"(\d{2})[./](\d{2})[./](\d{4})")
RANGE_RE = re.compile(r"(\d{2}[./]\d{2}[./]\d{4})\s*[-–—]\s*(\d{2}[./]\d{2}[./]\d{4})")


def to_iso(d: str) -> str:
    m = DATE_RE.search(d)
    if not m:
        raise ValueError(f"Bad date: {d}")
    day, month, year = m.groups()
    return f"{year}-{month}-{day}"


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def classify(label: str) -> str:
    low = label.lower()
    if "vacan" in low:
        return "vacations"
    if "sesi" in low or "restan" in low or "examen" in low:
        return "sessions"
    if "semestr" in low or "activitate didactic" in low:
        return "teachingPeriods"
    return "unknown"


def parse_tables(soup: BeautifulSoup) -> dict:
    result = {"teachingPeriods": [], "vacations": [], "sessions": [], "specialNoClassDays": []}
    for tr in soup.find_all("tr"):
        cells = [normalize_text(c.get_text(" ")) for c in tr.find_all(["th", "td"])]
        if not cells:
            continue
        label = cells[0]
        joined = " ".join(cells)
        target = classify(label or joined)
        if target == "unknown":
            continue
        for a, b in RANGE_RE.findall(joined):
            item = {"name": label or target, "start": to_iso(a), "end": to_iso(b)}
            if item not in result[target]:
                result[target].append(item)
    return result


def parse_free_text(soup: BeautifulSoup) -> dict:
    result = {"teachingPeriods": [], "vacations": [], "sessions": [], "specialNoClassDays": []}
    text = soup.get_text("\n")
    lines = [normalize_text(x) for x in text.splitlines() if normalize_text(x)]
    for i, line in enumerate(lines):
        target = classify(line)
        if target == "unknown":
            continue
        # Search the current line and a small window after it because some pages put dates on the next line.
        window = " ".join(lines[i:i + 4])
        for a, b in RANGE_RE.findall(window):
            item = {"name": line, "start": to_iso(a), "end": to_iso(b)}
            if item not in result[target]:
                result[target].append(item)
    return result


def merge(parsed: dict) -> dict:
    data = json.loads(json.dumps(FALLBACK))
    data["meta"]["fetchedAt"] = datetime.now().isoformat(timespec="seconds")
    data["meta"]["parser"] = "usv_page_with_fallback"
    for key in ["teachingPeriods", "vacations", "sessions", "specialNoClassDays"]:
        if parsed.get(key):
            # Use parsed values only when enough structure was found.
            data[key] = parsed[key]
    return data


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=CALENDAR_URL)
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    print("Fetching academic calendar:", args.url)
    try:
        r = requests.get(args.url, timeout=25, headers={"User-Agent": "USV Campus Occupancy educational project"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        parsed = parse_tables(soup)
        if not parsed["teachingPeriods"]:
            parsed = parse_free_text(soup)
        data = merge(parsed)
    except Exception as exc:
        print("Calendar fetch/parse failed; using fallback:", exc)
        data = json.loads(json.dumps(FALLBACK))
        data["meta"]["fetchedAt"] = datetime.now().isoformat(timespec="seconds")
        data["meta"]["parser"] = "fallback_after_error"

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("DONE")
    print("Written:", out)
    print("Teaching periods:", len(data.get("teachingPeriods", [])))
    print("Vacations:", len(data.get("vacations", [])))
    print("Sessions:", len(data.get("sessions", [])))


if __name__ == "__main__":
    main()
