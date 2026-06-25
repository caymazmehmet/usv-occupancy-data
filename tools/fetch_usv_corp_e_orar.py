#!/usr/bin/env python3
"""
USV Corp E occupancy scraper v4
Output: data/occupancy-corp-e.json

Run from the project root:
    python tools/fetch_usv_corp_e_orar.py

This script does NOT touch your map files. It only generates JSON data used by occupancy-portal.js.
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
OUTPUT = Path("data/occupancy-corp-e.json")
WEEK1_START = "2026-02-23"
UA = "Mozilla/5.0 (USV Campus Occupancy Educational Project)"

DAY_MAP = {
    "luni": 1, "monday": 1,
    "marti": 2, "marţi": 2, "tuesday": 2,
    "miercuri": 3, "wednesday": 3,
    "joi": 4, "thursday": 4,
    "vineri": 5, "friday": 5,
    "sambata": 6, "sâmbătă": 6, "saturday": 6,
    "duminica": 0, "duminică": 0, "sunday": 0,
}
MONTHS = {
    # Romanian
    "ian": 1, "ianuarie": 1,
    "feb": 2, "februarie": 2,
    "mar": 3, "martie": 3,
    "apr": 4, "aprilie": 4,
    "mai": 5,
    "iun": 6, "iunie": 6,
    "iul": 7, "iulie": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "septembrie": 9,
    "oct": 10, "octombrie": 10,
    "nov": 11, "noiembrie": 11,
    "dec": 12, "decembrie": 12,

    # English / Orar mixed labels
    "jan": 1, "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

# Fast sanity seed. If this ID cannot be fetched, internet/site access is the issue.
KNOWN_SEED_IDS = {"E005": 156}


def tr(value: Any) -> str:
    text = str(value or "")
    return text.translate(str.maketrans({
        "ă": "a", "â": "a", "î": "i", "ș": "s", "ş": "s", "ț": "t", "ţ": "t",
        "Ă": "A", "Â": "A", "Î": "I", "Ș": "S", "Ş": "S", "Ț": "T", "Ţ": "T",
    }))


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", tr(value).replace("\xa0", " ")).strip().lower()


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def fetch(url: str, timeout: int = 10) -> str:
    r = requests.get(url, timeout=timeout, headers={"User-Agent": UA})
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r.text


def with_print(url: str) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q["print"] = "da"
    return urlunparse(p._replace(query=urlencode(q)))


def extract_room_code(text: str) -> str:
    raw = tr(clean(text)).upper()
    if "AULA" in raw and "E" in raw:
        return "AULAE"
    raw = raw.replace("CORP E -", " ").replace("CORPUL:E", " ").replace("CORPUL: E", " ")
    m = re.search(r"\b(ED|E)\s*[-_. ]?\s*(\d{1,4}[A-Z]?)\b", raw)
    if m:
        pref, num = m.group(1), m.group(2)
        if num.isdigit() and len(num) < 3:
            num = num.zfill(3)
        return f"{pref}{num}"
    return ""


def is_corp_e(soup: BeautifulSoup) -> bool:
    txt = norm(soup.get_text(" ", strip=True)).replace(" ", "")
    return "corpul:e" in txt or "corpe" in txt


def page_room_code(soup: BeautifulSoup) -> str:
    candidates: List[str] = []
    for name in ["h1", "h2", "h3", "h4", "title", "caption", "b", "strong"]:
        for tag in soup.find_all(name):
            t = clean(tag.get_text(" ", strip=True))
            if t:
                candidates.append(t)
    candidates.append(clean(soup.get_text(" ", strip=True))[:500])
    for c in candidates:
        code = extract_room_code(c)
        if code:
            return key(code)
    return ""


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
        n = norm(text)
        if "corp e" not in n and not re.search(r"\bE\s*\d|\bED\s*\d", text, re.I):
            continue
        code = extract_room_code(text)
        if not code:
            continue
        rooms[key(code)] = {"roomCode": key(code), "label": text, "url": with_print(urljoin(INDEX_URL, href))}

    for opt in soup.find_all("option"):
        text = clean(opt.get_text(" ", strip=True))
        val = clean(opt.get("value") or "")
        if "corp e" not in norm(text):
            continue
        code = extract_room_code(text)
        if code and val.isdigit():
            rooms[key(code)] = {"roomCode": key(code), "label": text, "url": ROOM_URL.format(id=val)}

    # Collect visible room codes from text, even if URL IDs are missing.
    text = clean(soup.get_text(" ", strip=True))
    for m in re.finditer(r"corp\s*E\s*-\s*(Aula\s*E|ED\s*\d{1,3}(?:-\d{1,3})?|E\s*\d{1,3})", tr(text), re.I):
        label = "corp E - " + m.group(1)
        code = extract_room_code(label)
        if code and key(code) not in rooms:
            rooms[key(code)] = {"roomCode": key(code), "label": label, "url": ""}

    return rooms


def scan_one(rid: int) -> Optional[Dict[str, str]]:
    url = ROOM_URL.format(id=rid)
    try:
        html = fetch(url, timeout=8)
    except Exception:
        return None
    soup = BeautifulSoup(html, "html.parser")
    if not is_corp_e(soup):
        return None
    code = page_room_code(soup)
    if not code:
        return None
    return {"roomCode": code, "label": f"corp E - {code}", "url": url, "id": str(rid)}


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
                code = item["roomCode"]
                rooms[code] = item
                print(f"  found {code} -> ID {item.get('id')}")
            if done % 100 == 0:
                print(f"  scanned {done}/{len(futures)}")
    return rooms


def parse_int(v: Any, default: int = 1) -> int:
    try:
        return int(v)
    except Exception:
        return default


def table_grid(table: Tag) -> Tuple[Dict[Tuple[int, int], Dict[str, Any]], List[Dict[str, Any]]]:
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


def day_columns(grid: Dict[Tuple[int, int], Dict[str, Any]]) -> Tuple[Optional[int], Dict[int, int]]:
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


def start_hour_for_row(grid: Dict[Tuple[int, int], Dict[str, Any]], row: int) -> Optional[int]:
    # Current row first, then walk upwards because hour labels can rowspan/shift.
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
    # Orar uses separator lines and dots. Keep course chunks readable.
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
            ev.update({"roomCode": room_code, "dayIndex": days[item["col"]], "start": start, "end": end, "source": "regular"})
            events.append(ev)
    return events


def years_from_text(text: str) -> Tuple[int, int]:
    m = re.search(r"anul\s+(\d{2})_(\d{2})", norm(text))
    if m:
        return 2000 + int(m.group(1)), 2000 + int(m.group(2))
    today = date.today()
    return (today.year if today.month >= 9 else today.year - 1, today.year + 1 if today.month >= 9 else today.year)


def parse_ro_date(text: str, y1: int, y2: int) -> Optional[str]:
    m = re.search(r"\((\d{1,2})\.\s*([A-Za-zăâîșşțţĂÂÎȘŞȚŢ]+)\)", text)
    if not m:
        return None
    d = int(m.group(1)); mon = norm(m.group(2))
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
    return f"{int(m.group(1)):02d}:{int(m.group(2) or 0):02d}", f"{int(m.group(3)):02d}:{int(m.group(4) or 0):02d}"


def parse_modular(room_code: str, soup: BeautifulSoup) -> List[Dict[str, Any]]:
    page_text = soup.get_text(" ", strip=True)
    y1, y2 = years_from_text(page_text)
    events: List[Dict[str, Any]] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [norm(c.get_text(" ", strip=True)) for c in rows[0].find_all(["td", "th"])]
        if not any("sapt" in h for h in headers) or not any("interval" in h for h in headers):
            continue
        for tr_ in rows[1:]:
            cells = [clean(c.get_text(" ", strip=True)) for c in tr_.find_all(["td", "th"])]
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


def scrape_room(room: Dict[str, str]) -> Dict[str, Any]:
    code = key(room["roomCode"])
    url = room.get("url") or ""
    if not url:
        return {"label": room.get("label", code), "url": "", "events": [], "error": "No Orar URL discovered for this room"}
    html = fetch(url, timeout=15)
    soup = BeautifulSoup(html, "html.parser")
    events = parse_regular(code, soup) + parse_modular(code, soup)
    return {"label": room.get("label", code), "url": url, "events": events}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=str(OUTPUT))
    ap.add_argument("--scan-min", type=int, default=1)
    ap.add_argument("--scan-max", type=int, default=850)
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

    print("Discovering Corp E rooms...")
    rooms = discover_from_index()
    # Seed known page so there is at least one real page while scan runs.
    rooms.setdefault("E005", {"roomCode": "E005", "label": "corp E - E005", "url": seed_url})
    print(f"Index/text discovery: {len(rooms)} room labels. Now finding real Orar URLs by ID scan.")

    scanned = scan_ids(args.scan_min, args.scan_max, args.workers)
    for code, item in scanned.items():
        rooms[code] = item

    # Keep only rooms with real URLs for scraping; still include labels without URLs if discovered.
    rooms = dict(sorted(rooms.items()))
    if not rooms:
        print("No Corp E rooms discovered.", file=sys.stderr)
        return 3

    items = list(rooms.values())
    if args.limit and args.limit > 0:
        items = items[:args.limit]

    out_rooms: Dict[str, Any] = {}
    total_events = 0
    print(f"Scraping schedules for {len(items)} rooms...")
    for i, room in enumerate(items, 1):
        code = key(room.get("roomCode"))
        try:
            print(f"[{i}/{len(items)}] {code}")
            out_rooms[code] = scrape_room(room)
            n = len(out_rooms[code].get("events", []))
            total_events += n
            print(f"    events: {n}")
            time.sleep(0.05)
        except Exception as e:
            print(f"    error: {e}", file=sys.stderr)
            out_rooms[code] = {"label": room.get("label", code), "url": room.get("url", ""), "events": [], "error": str(e)}

    result = {
        "meta": {
            "source": "orar.usv.ro/orar/vizualizare/orarSPG.php",
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "week1StartDate": args.week1_start_date,
            "roomCount": len(out_rooms),
            "eventCount": total_events,
            "note": "Generated by USV Corp E occupancy scraper v4"
        },
        "rooms": out_rooms,
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nDONE")
    print(f"Written: {path}")
    print(f"Rooms: {len(out_rooms)} | Events: {total_events}")
    if total_events == 0:
        print("WARNING: Rooms were found but no events parsed. Send me the terminal output.")
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
