#!/usr/bin/env python3
"""
USV all-campus occupancy scraper.

Output:
    data/occupancy-all-campus.json

Purpose:
    Pull all room/sala pages from orar.usv.ro, parse regular and modular
    schedules, and store one all-campus JSON file.

Important:
    This script only generates data. It does not touch frontend/map files.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Tag

INDEX_URL = "https://orar.usv.ro/orar/vizualizare/orarUp1.php"
ROOM_URL = "https://orar.usv.ro/orar/vizualizare/orarSPG.php?ID={id}&back=&mod=sala&mod2=vizual&print=da"

OUTPUT = Path("data/occupancy-all-campus.json")
WEEK1_START = "2026-02-23"
UA = "Mozilla/5.0 (USV Campus Occupancy Educational Project)"

DAY_MAP = {
    "luni": 1,
    "monday": 1,
    "marti": 2,
    "marţi": 2,
    "tuesday": 2,
    "miercuri": 3,
    "wednesday": 3,
    "joi": 4,
    "thursday": 4,
    "vineri": 5,
    "friday": 5,
    "sambata": 6,
    "sâmbătă": 6,
    "saturday": 6,
    "duminica": 0,
    "duminică": 0,
    "sunday": 0,
}

MONTHS = {
    "ian": 1,
    "ianuarie": 1,
    "feb": 2,
    "februarie": 2,
    "mar": 3,
    "martie": 3,
    "apr": 4,
    "aprilie": 4,
    "mai": 5,
    "iun": 6,
    "iunie": 6,
    "iul": 7,
    "iulie": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "septembrie": 9,
    "oct": 10,
    "octombrie": 10,
    "nov": 11,
    "noiembrie": 11,
    "dec": 12,
    "decembrie": 12,
    "jan": 1,
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

KNOWN_SEED_IDS = {"E005": 156}


def tr(value: Any) -> str:
    text = str(value or "")
    return text.translate(
        str.maketrans(
            {
                "ă": "a",
                "â": "a",
                "î": "i",
                "ș": "s",
                "ş": "s",
                "ț": "t",
                "ţ": "t",
                "Ă": "A",
                "Â": "A",
                "Î": "I",
                "Ș": "S",
                "Ş": "S",
                "Ț": "T",
                "Ţ": "T",
            }
        )
    )


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", tr(value).replace("\xa0", " ")).strip().lower()


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def slug(value: Any) -> str:
    s = norm(value)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "unknown"


def fetch(url: str, timeout: int = 12) -> str:
    r = requests.get(url, timeout=timeout, headers={"User-Agent": UA})
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r.text


def with_print(url: str) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q["print"] = "da"
    return urlunparse(p._replace(query=urlencode(q)))

def canonical_building(value: Any) -> str:
    raw = clean(value)
    n = norm(raw)

    m = re.search(r"\bcorpul\s*[:\-]\s*([a-z0-9]{1,4})\b", n)
    if not m:
        m = re.search(r"\bcorp\s*[:\-]?\s*([a-z0-9]{1,4})\b", n)

    if m:
        token = m.group(1).upper()
        if token not in {"UL", "CORP"}:
            return f"Corp {token}"

    known = [
        ("camera de comert", "Camera de Comerț și Industrie"),
        ("caminul nr.1", "Caminul nr.1"),
        ("caminul nr 1", "Caminul nr.1"),
        ("caminul nr.2", "Caminul nr.2"),
        ("caminul nr 2", "Caminul nr.2"),
        ("complex de natatie", "Complex de natație"),
        ("observator astronomic", "Observator astronomic"),
        ("restaurant", "Restaurant"),
        ("sala de sport", "Sală de sport"),
        ("directie silvica", "Direcție silvică Suceava"),
        ("vatra dornei", "Spații didactice Vatra Dornei"),
    ]

    for needle, label in known:
        if needle in n:
            return label

    return "Unknown"

def extract_building(text: str) -> str:
    return canonical_building(text)


def make_room_key(building: str, room_code: str, fallback_id: str = "") -> str:
    b = slug(building)
    c = key(room_code)
    if b == "unknown" and fallback_id:
        return f"unknown{fallback_id}__{c}"
    return f"{b}__{c}"


def extract_room_code(text: str) -> str:
    raw = tr(clean(text)).upper()

    raw = re.sub(r"\bCORP(?:UL)?\s*[:\-]?\s*[A-Z0-9]{1,4}\s*[-:]*\s*", " ", raw)

    m = re.search(r"\bAULA\s+([A-Z0-9]+)\b", raw)
    if m:
        return key("AULA" + m.group(1))

    for m in re.finditer(r"\b([A-Z]{1,3})\s*[-_. ]?\s*(\d{1,4}[A-Z]?)\b", raw):
        prefix, num = m.group(1), m.group(2)

        if prefix in {"AN", "NR", "ID"}:
            continue

        if num.isdigit() and len(num) < 3:
            num = num.zfill(3)

        return key(f"{prefix}{num}")

    return ""


def page_candidates(soup: BeautifulSoup) -> List[str]:
    candidates: List[str] = []

    for name in ["h1", "h2", "h3", "h4", "title", "caption", "b", "strong"]:
        for tag in soup.find_all(name):
            t = clean(tag.get_text(" ", strip=True))
            if t:
                candidates.append(t)

    candidates.append(clean(soup.get_text(" ", strip=True))[:1200])
    return candidates


def page_room_info(
    soup: BeautifulSoup, rid: Optional[int] = None, fallback_label: str = ""
) -> Optional[Dict[str, str]]:
    candidates = page_candidates(soup)
    full_text = clean(soup.get_text(" ", strip=True))

    room_code = ""
    label = fallback_label or ""

    for c in candidates:
        code = extract_room_code(c)
        if code:
            room_code = key(code)
            label = c
            break

    if not room_code:
        code = extract_room_code(full_text)
        if code:
            room_code = key(code)
            label = fallback_label or full_text[:160]

    if not room_code:
        return None

    building = "Unknown"

    for c in candidates:
        b = extract_building(c)
        if b != "Unknown":
            building = b
            break

    if building == "Unknown":
        b = extract_building(full_text)
        if b != "Unknown":
            building = b

    if building == "Unknown":
        if room_code.startswith("EFS"):
            building = "FEFS / Sport"
        elif re.match(r"^[A-Z]{1,2}\d", room_code):
            building = f"Corp {room_code[0]}"

    room_key = make_room_key(building, room_code, str(rid or ""))

    return {
        "roomCode": room_code,
        "building": building,
        "buildingKey": slug(building),
        "roomKey": room_key,
        "label": label or f"{building} - {room_code}",
    }


def discover_from_index() -> Dict[str, Dict[str, str]]:
    rooms: Dict[str, Dict[str, str]] = {}

    try:
        html = fetch(INDEX_URL, timeout=15)
    except Exception as e:
        print(f"Index read failed: {e}")
        return rooms

    soup = BeautifulSoup(html, "html.parser")

    for a in soup.find_all("a"):
        text = clean(a.get_text(" ", strip=True) or a.get("title") or "")
        href = a.get("href") or ""

        if not text or not href:
            continue

        url = with_print(urljoin(INDEX_URL, href))

        if (
            "orarSPG" not in href
            and "mod=sala" not in href
            and not extract_room_code(text)
        ):
            continue

        mini = BeautifulSoup(f"<html><body>{text}</body></html>", "html.parser")
        info = page_room_info(mini, fallback_label=text)

        if not info:
            continue

        info["url"] = url
        rooms[info["roomKey"]] = info

    for opt in soup.find_all("option"):
        text = clean(opt.get_text(" ", strip=True))
        val = clean(opt.get("value") or "")

        if not text or not val.isdigit():
            continue

        mini = BeautifulSoup(f"<html><body>{text}</body></html>", "html.parser")
        info = page_room_info(mini, rid=int(val), fallback_label=text)

        if not info:
            continue

        info["url"] = ROOM_URL.format(id=val)
        info["id"] = val
        rooms[info["roomKey"]] = info

    return rooms


def scan_one(rid: int) -> Optional[Dict[str, str]]:
    url = ROOM_URL.format(id=rid)

    try:
        html = fetch(url, timeout=8)
    except Exception:
        return None

    soup = BeautifulSoup(html, "html.parser")
    info = page_room_info(soup, rid=rid)

    if not info:
        return None

    info["url"] = url
    info["id"] = str(rid)
    return info


def scan_ids(min_id: int, max_id: int, workers: int) -> Dict[str, Dict[str, str]]:
    rooms: Dict[str, Dict[str, str]] = {}

    print(f"Scanning Orar room IDs {min_id}-{max_id}...")

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(scan_one, rid): rid for rid in range(min_id, max_id + 1)}
        done = 0

        for fut in as_completed(futures):
            done += 1
            item = fut.result()

            if item:
                rooms[item["roomKey"]] = item
                print(
                    f"  found {item['building']} / {item['roomCode']} -> ID {item.get('id')}"
                )

            if done % 100 == 0:
                print(f"  scanned {done}/{len(futures)}")

    return rooms


def parse_int(v: Any, default: int = 1) -> int:
    try:
        return int(v)
    except Exception:
        return default


def table_grid(
    table: Tag,
) -> Tuple[Dict[Tuple[int, int], Dict[str, Any]], List[Dict[str, Any]]]:
    grid: Dict[Tuple[int, int], Dict[str, Any]] = {}
    originals: List[Dict[str, Any]] = []

    for r, tr_ in enumerate(table.find_all("tr")):
        c = 0

        for cell in tr_.find_all(["td", "th"]):
            while (r, c) in grid:
                c += 1

            rs = max(1, parse_int(cell.get("rowspan"), 1))
            cs = max(1, parse_int(cell.get("colspan"), 1))

            item = {
                "row": r,
                "col": c,
                "rowspan": rs,
                "colspan": cs,
                "cell": cell,
                "tag": cell.name,
                "text": clean(cell.get_text(" ", strip=True)),
                "raw": cell.get_text("\n", strip=True).replace("\xa0", " "),
            }

            originals.append(item)

            for rr in range(rs):
                for cc in range(cs):
                    grid[(r + rr, c + cc)] = item

            c += cs

    return grid, originals


def find_main_table(soup: BeautifulSoup) -> Optional[Tag]:
    for t in soup.find_all("table"):
        tx = norm(t.get_text(" ", strip=True))
        if "luni" in tx and "mart" in tx and "vineri" in tx:
            return t

    return None


def day_columns(
    grid: Dict[Tuple[int, int], Dict[str, Any]],
) -> Tuple[Optional[int], Dict[int, int]]:
    rows: Dict[int, List[Tuple[int, str]]] = {}

    for (r, c), item in grid.items():
        rows.setdefault(r, []).append((c, norm(item["text"])))

    for r, cells in rows.items():
        days = {}

        for c, tx in cells:
            if tx in DAY_MAP:
                days[c] = DAY_MAP[tx]

        if len(days) >= 3:
            return r, days

    return None, {}


def start_hour_for_row(
    grid: Dict[Tuple[int, int], Dict[str, Any]], row: int
) -> Optional[int]:
    for rr in range(row, max(-1, row - 4), -1):
        for c in range(0, 3):
            item = grid.get((rr, c))

            if not item:
                continue

            m = re.match(r"^(\d{1,2})\b", clean(item["text"]))

            if m:
                h = int(m.group(1))

                if 0 <= h <= 23:
                    return h

    return None


def split_events(raw: str) -> List[str]:
    raw = raw.replace("\r", "\n")
    raw = re.sub(r"\*+", "\n", raw)

    parts: List[str] = []

    for line in re.split(r"\n+", raw):
        line = clean(line.strip(" .;-"))

        if not line or re.fullmatch(r"[.\-]+", line):
            continue

        for sub in re.split(r"\s*\.\.\.\s*|\s*\.\.\s*", line):
            sub = clean(sub.strip(" .;-"))

            if sub:
                parts.append(sub)

    return parts


def weeks_from_text(text: str) -> Optional[List[int]]:
    tx = norm(text)

    m = re.search(r"primele\s+(\d+)\s+sapt", tx)

    if m:
        n = int(m.group(1))
        return list(range(1, n + 1))

    weeks: List[int] = []

    for m in re.finditer(r"sapt(?:amana|amani|\.)?\s*([0-9,\-\s]+)", tx):
        for part in re.split(r"[,\s]+", m.group(1)):
            if not part:
                continue

            if "-" in part:
                try:
                    a, b = [int(x) for x in part.split("-", 1)]
                    weeks.extend(range(a, b + 1))
                except Exception:
                    pass
            elif part.isdigit():
                weeks.append(int(part))

    w = sorted(set(x for x in weeks if 1 <= x <= 30))
    return w or None


def parse_course(text: str) -> Dict[str, Any]:
    txt = clean(text)
    chunks = [clean(x) for x in txt.split(",")]

    event = {
        "subject": chunks[0] if chunks else txt,
        "type": chunks[1] if len(chunks) > 1 else "",
        "teacher": chunks[2] if len(chunks) > 2 else "",
        "group": ", ".join(chunks[3:]) if len(chunks) > 3 else "",
        "raw": txt,
    }

    w = weeks_from_text(txt)

    if w:
        event["weeks"] = w

    return event


def parse_regular(room_code: str, soup: BeautifulSoup) -> List[Dict[str, Any]]:
    table = find_main_table(soup)

    if not table:
        return []

    grid, originals = table_grid(table)
    header_row, days = day_columns(grid)

    if header_row is None:
        return []

    events: List[Dict[str, Any]] = []
    seen = set()

    for item in originals:
        if item["row"] <= header_row or item["col"] not in days or item["tag"] == "th":
            continue

        cell_id = id(item["cell"])

        if cell_id in seen:
            continue

        seen.add(cell_id)

        raw = item["raw"]

        if not clean(raw) or re.fullmatch(r"[.\-\s]+", clean(raw)):
            continue

        sh = start_hour_for_row(grid, item["row"])

        if sh is None:
            continue

        duration = max(1, parse_int(item.get("rowspan"), 1))

        start = f"{sh:02d}:00"
        end = f"{min(23, sh + duration):02d}:00"

        for part in split_events(raw):
            ev = parse_course(part)

            ev.update(
                {
                    "roomCode": room_code,
                    "dayIndex": days[item["col"]],
                    "start": start,
                    "end": end,
                    "source": "regular",
                }
            )

            events.append(ev)

    return events


def years_from_text(text: str) -> Tuple[int, int]:
    m = re.search(r"anul\s+(\d{2})_(\d{2})", norm(text))

    if m:
        return 2000 + int(m.group(1)), 2000 + int(m.group(2))

    today = date.today()

    return (
        today.year if today.month >= 9 else today.year - 1,
        today.year + 1 if today.month >= 9 else today.year,
    )


def parse_ro_date(text: str, y1: int, y2: int) -> Optional[str]:
    m = re.search(r"\((\d{1,2})\.\s*([A-Za-zăâîșşțţĂÂÎȘŞȚŢ]+)\)", text)

    if not m:
        return None

    d = int(m.group(1))
    mon = norm(m.group(2))

    month = MONTHS.get(mon[:3]) or MONTHS.get(mon)

    if not month:
        return None

    year = y2 if month <= 8 else y1

    try:
        return date(year, month, d).isoformat()
    except Exception:
        return None


def interval(text: str) -> Tuple[Optional[str], Optional[str]]:
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*[-–]\s*(\d{1,2})(?::(\d{2}))?", text)

    if not m:
        return None, None

    return (
        f"{int(m.group(1)):02d}:{int(m.group(2) or 0):02d}",
        f"{int(m.group(3)):02d}:{int(m.group(4) or 0):02d}",
    )


def parse_modular(room_code: str, soup: BeautifulSoup) -> List[Dict[str, Any]]:
    page_text = soup.get_text(" ", strip=True)
    y1, y2 = years_from_text(page_text)

    events: List[Dict[str, Any]] = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")

        if not rows:
            continue

        headers = [
            norm(c.get_text(" ", strip=True)) for c in rows[0].find_all(["td", "th"])
        ]

        if not any("sapt" in h for h in headers) or not any(
            "interval" in h for h in headers
        ):
            continue

        for tr_ in rows[1:]:
            cells = [
                clean(c.get_text(" ", strip=True)) for c in tr_.find_all(["td", "th"])
            ]

            if len(cells) < 6:
                continue

            week_text, day_text, int_text, subject, typ, teacher = cells[:6]
            start, end = interval(int_text)

            if not start or not end:
                continue

            ev_date = parse_ro_date(day_text, y1, y2)
            day_idx = None

            if ev_date:
                day_idx = datetime.fromisoformat(ev_date).weekday() + 1

                if day_idx == 7:
                    day_idx = 0

            ev = {
                "roomCode": room_code,
                "date": ev_date,
                "dayIndex": day_idx,
                "start": start,
                "end": end,
                "subject": subject,
                "type": typ,
                "teacher": teacher,
                "group": cells[6] if len(cells) > 6 else "",
                "raw": clean(" | ".join(cells)),
                "source": "modular",
            }

            nums = [int(x) for x in re.findall(r"\d+", week_text)]

            if nums:
                ev["weeks"] = nums

            events.append(ev)

    return events


def dedupe_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []

    for e in events:
        sig = (
            e.get("roomCode", ""),
            e.get("date", ""),
            e.get("dayIndex", ""),
            e.get("start", ""),
            e.get("end", ""),
            e.get("subject", ""),
            e.get("teacher", ""),
            e.get("group", ""),
            e.get("raw", ""),
        )

        if sig in seen:
            continue

        seen.add(sig)
        out.append(e)

    return out


def scrape_room(room: Dict[str, str]) -> Dict[str, Any]:
    code = key(room["roomCode"])
    building = room.get("building") or "Unknown"
    building_key = room.get("buildingKey") or slug(building)
    room_key = room.get("roomKey") or make_room_key(building, code, room.get("id", ""))
    url = room.get("url") or ""

    base = {
        "building": building,
        "buildingKey": building_key,
        "roomCode": code,
        "roomKey": room_key,
        "label": room.get("label", f"{building} - {code}"),
        "url": url,
        "events": [],
    }

    if not url:
        base["error"] = "No Orar URL discovered for this room"
        return base

    html = fetch(url, timeout=15)
    soup = BeautifulSoup(html, "html.parser")

    parsed_info = page_room_info(
        soup, rid=int(room.get("id", "0") or 0), fallback_label=base["label"]
    )

    if parsed_info:
        building = parsed_info.get("building") or building
        building_key = parsed_info.get("buildingKey") or slug(building)
        code = parsed_info.get("roomCode") or code
        room_key = parsed_info.get("roomKey") or make_room_key(
            building, code, room.get("id", "")
        )

    events = parse_regular(code, soup) + parse_modular(code, soup)
    events = dedupe_events(events)

    for e in events:
        e["building"] = building
        e["buildingKey"] = building_key
        e["roomKey"] = room_key
        e["roomCode"] = code

    base.update(
        {
            "building": building,
            "buildingKey": building_key,
            "roomCode": code,
            "roomKey": room_key,
            "label": (
                parsed_info.get("label", base["label"])
                if parsed_info
                else base["label"]
            ),
            "events": events,
        }
    )

    return base


def build_result(
    out_rooms: Dict[str, Any], args: argparse.Namespace, total_events: int
) -> Dict[str, Any]:
    buildings: Dict[str, Any] = {}
    rooms_flat: Dict[str, Any] = {}
    rooms_by_key: Dict[str, Any] = {}

    bad_time = 0
    modular_date_null = 0

    for old_room_key, room in sorted(out_rooms.items()):
        building = room.get("building") or "Unknown"
        building_key = room.get("buildingKey") or slug(building)
        room_code = room.get("roomCode") or old_room_key
        real_room_key = room.get("roomKey") or old_room_key

        b = buildings.setdefault(
            building_key,
            {
                "label": building,
                "buildingKey": building_key,
                "roomCount": 0,
                "eventCount": 0,
                "rooms": {},
            },
        )

        events = room.get("events", [])

        for e in events:
            if not e.get("start") or not e.get("end"):
                bad_time += 1

            if e.get("source") == "modular" and not e.get("date"):
                modular_date_null += 1

        compact_room = dict(room)

        b["rooms"][room_code] = compact_room
        b["roomCount"] = len(b["rooms"])
        b["eventCount"] += len(events)

        rooms_by_key[real_room_key] = compact_room

        if room_code not in rooms_flat or building_key == "corpe":
            rooms_flat[room_code] = compact_room

    return {
        "meta": {
            "source": "orar.usv.ro/orar/vizualizare/orarSPG.php",
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "week1StartDate": args.week1_start_date,
            "buildingCount": len(buildings),
            "roomCount": len(out_rooms),
            "eventCount": total_events,
            "badTime": bad_time,
            "modularDateNull": modular_date_null,
            "note": "Generated by USV all-campus occupancy scraper. Frontend may display only buildings with prepared map geometry.",
        },
        "buildings": buildings,
        "roomsByKey": rooms_by_key,
        "rooms": rooms_flat,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=str(OUTPUT))
    ap.add_argument("--scan-min", type=int, default=1)
    ap.add_argument("--scan-max", type=int, default=5000)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--week1-start-date", default=WEEK1_START)
    ap.add_argument("--limit", type=int, default=0, help="For quick test only")
    args = ap.parse_args()

    print("Testing Orar access with known E005 page...")

    seed_url = ROOM_URL.format(id=KNOWN_SEED_IDS["E005"])

    try:
        seed_html = fetch(seed_url, timeout=15)

        if "E005" not in seed_html:
            print("Warning: E005 page opened but content did not contain E005.")
    except Exception as e:
        print("Cannot access Orar from this computer/network:", e, file=sys.stderr)
        print("Open this in browser to test:", seed_url, file=sys.stderr)
        return 2

    print("Discovering all campus rooms...")

    rooms = discover_from_index()

    rooms.setdefault(
        make_room_key("Corp E", "E005"),
        {
            "roomCode": "E005",
            "building": "Corp E",
            "buildingKey": "corpe",
            "roomKey": make_room_key("Corp E", "E005"),
            "label": "Corp E - E005",
            "url": seed_url,
            "id": str(KNOWN_SEED_IDS["E005"]),
        },
    )

    print(
        f"Index/text discovery: {len(rooms)} room labels. Now finding real Orar URLs by ID scan."
    )

    scanned = scan_ids(args.scan_min, args.scan_max, args.workers)

    for room_key, item in scanned.items():
        rooms[room_key] = item

    rooms = dict(sorted(rooms.items()))

    if not rooms:
        print("No campus rooms discovered.", file=sys.stderr)
        return 3

    items = list(rooms.values())

    if args.limit and args.limit > 0:
        items = items[: args.limit]

    out_rooms: Dict[str, Any] = {}
    total_events = 0

    print(f"Scraping schedules for {len(items)} rooms...")

    for i, room in enumerate(items, 1):
        code = key(room.get("roomCode"))
        building = room.get("building", "Unknown")
        room_key = room.get("roomKey") or make_room_key(
            building, code, room.get("id", "")
        )

        try:
            print(f"[{i}/{len(items)}] {building} / {code}")

            out_rooms[room_key] = scrape_room(room)

            n = len(out_rooms[room_key].get("events", []))
            total_events += n

            print(f"    events: {n}")

            time.sleep(0.04)
        except Exception as e:
            print(f"    error: {e}", file=sys.stderr)

            out_rooms[room_key] = {
                "building": building,
                "buildingKey": slug(building),
                "roomCode": code,
                "roomKey": room_key,
                "label": room.get("label", f"{building} - {code}"),
                "url": room.get("url", ""),
                "events": [],
                "error": str(e),
            }

    result = build_result(out_rooms, args, total_events)

    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\nDONE")
    print(f"Written: {path}")
    print(
        f"Buildings: {result['meta']['buildingCount']} | "
        f"Rooms: {result['meta']['roomCount']} | "
        f"Events: {result['meta']['eventCount']}"
    )
    print(
        f"Quality: badTime={result['meta']['badTime']} | "
        f"modularDateNull={result['meta']['modularDateNull']}"
    )

    if total_events == 0:
        print(
            "WARNING: Rooms were found but no events parsed. Send me the terminal output."
        )
        return 4

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
