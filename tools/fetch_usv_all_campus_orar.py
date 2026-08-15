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
MOBILE_GROUP_URL = "https://orar.usv.ro/orar/mobil/vizualizare/orarSPG.php?ID={id}&back=&mod=grupa&mod2=vizual&print=da"

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


def fetch(
    url: str, timeout: int = 20, retries: int = 4, sleep_seconds: float = 1.5
) -> str:
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": UA})
            r.raise_for_status()
            r.encoding = r.apparent_encoding or r.encoding
            return r.text
        except Exception as e:
            last_error = e

            if attempt < retries:
                print(f"Retry {attempt}/{retries} for: {url}")
                time.sleep(sleep_seconds * attempt)

    raise last_error


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


def is_event_fragment(text: str) -> bool:
    t = clean(text)
    n = norm(t)

    if not t:
        return True

    if n.startswith("sapt") or n.startswith("saptamanile") or n.startswith("primele"):
        return True

    if re.match(r"^sgr\.?\s*[0-9a-z]+(?:\s+sapt.*)?$", n, re.IGNORECASE):
        return True

    if re.match(r"^grupa\s*[0-9a-z]+(?:\s+sapt.*)?$", n, re.IGNORECASE):
        return True

    if re.match(r"^subgrupa\s*[0-9a-z]+(?:\s+sapt.*)?$", n, re.IGNORECASE):
        return True

    group_only_patterns = [
        r"^\d{3,4}[a-zA-Z]?\s*\([^)]+an\s+\d+[^)]*\)$",
        r"^\([^)]+an\s+\d+[^)]*\)$",
        r"^[A-Z0-9_\/\-]{2,}\s+anul?\s+\d+$",
    ]

    for pattern in group_only_patterns:
        if re.search(pattern, t, re.IGNORECASE) or re.search(pattern, n, re.IGNORECASE):
            return True

    chunks = [clean(x) for x in t.split(",")]

    if len(chunks) >= 2:
        typ = norm(chunks[1])
        if typ in {"curs", "sem", "seminar", "lab", "laborator", "proiect", "lp"}:
            return False

    if len(chunks) >= 3:
        first = norm(chunks[0])
        second = norm(chunks[1])
        third = norm(chunks[2])

        if first and (second or third):
            return False

    return False


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

    tx = tx.replace(" si ", " ")
    tx = tx.replace(" și ", " ")
    tx = tx.replace("+", " ")
    tx = tx.replace("hsapt", " sapt ")

    for m in re.finditer(r"sapt(?:amana|amani|\.)?\s*([0-9,\-\s]+)", tx):
        raw_nums = m.group(1)

        for part in re.split(r"[,\s]+", raw_nums):
            part = part.strip()

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


def is_blank_group(value: Any) -> bool:
    v = clean(value)
    return not v or v in {"-", ".", "—", "–"}


def extract_group_from_text(text: str) -> str:
    """
    Extracts group/program information from Orar text.

    Handles examples like:
      - grupa2b(FEAA,MNG an 1), sgr.4
      - AI an 3(FEAA)
      - C an 3(FIESC)
      - Ist. an 2(FIG)
      - G (ID) anul 1
      - modular raw rows separated by pipes, where group is often last column
    """
    txt = clean(text)

    if not txt:
        return ""

    # For modular table rows, the group is usually in the last non-empty column:
    # 2 | Sâmbătă (...) | 08 - 12 | Course | sem | Teacher | | G (ID) anul 1
    if "|" in txt:
        pipe_parts = [clean(x) for x in txt.split("|")]

        for part in reversed(pipe_parts):
            if not part:
                continue

            if re.search(
                r"\b[A-Za-zĂÂÎȘŞȚŢăâîșşțţ0-9_.\/\-]{1,}"
                r"(?:\s*\([^)]+\))?\s+an(?:ul)?\s+\d+\b",
                part,
                re.IGNORECASE,
            ):
                return part

            if re.search(
                r"\bgrupa\s*[0-9a-zA-Z]+|\bsgr\.?\s*[0-9a-zA-Z]+|\bsubgrupa\s*[0-9a-zA-Z]+",
                part,
                re.IGNORECASE,
            ):
                return part

    patterns = [
        # grupa2b(FEAA,MNG an 1), grupa 2, grupa2b
        r"\bgrupa\s*[0-9a-zA-Z]+(?:\s*\([^)]+\))?",
        # sgr.4, sgr 4
        r"\bsgr\.?\s*[0-9a-zA-Z]+",
        # subgrupa 1
        r"\bsubgrupa\s*[0-9a-zA-Z]+",
        # G (ID) anul 1, G (ID) an 1
        r"\b[A-Za-zĂÂÎȘŞȚŢăâîșşțţ0-9_.\/\-]{1,}\s*\([^)]+\)\s+an(?:ul)?\s+\d+\b",
        # C an 3(FIESC), G an 2(FIG), Ist. an 2(FIG), AI an 3(FEAA)
        r"\b[A-Za-zĂÂÎȘŞȚŢăâîșşțţ0-9_.\/\-]{1,}\s+an(?:ul)?\s+\d+\s*\([^)]+\)",
        # C an 3, G an 2, Ist. an 2, MNG an 1
        r"\b[A-Za-zĂÂÎȘŞȚŢăâîșşțţ0-9_.\/\-]{1,}\s+an(?:ul)?\s+\d+\b",
        # 1234A(... an 1 ...)
        r"\b\d{3,4}[a-zA-Z]?\s*\([^)]+an(?:ul)?\s+\d+[^)]*\)",
        # (... an 1 ...)
        r"\([A-Za-zĂÂÎȘŞȚŢăâîșşțţ0-9_,.\-\/\s]+an(?:ul)?\s+\d+[^)]*\)",
    ]

    for pattern in patterns:
        m = re.search(pattern, txt, re.IGNORECASE)

        if m:
            return clean(m.group(0))

    # Fallback on normalized text for diacritics-insensitive matching.
    n = norm(txt)

    fallback_patterns = [
        r"\b[a-z0-9_.\/\-]{1,}\s*\([^)]+\)\s+an(?:ul)?\s+\d+\b",
        r"\b[a-z0-9_.\/\-]{1,}\s+an(?:ul)?\s+\d+\s*\([^)]+\)",
        r"\b[a-z0-9_.\/\-]{1,}\s+an(?:ul)?\s+\d+\b",
    ]

    for pattern in fallback_patterns:
        m = re.search(pattern, n, re.IGNORECASE)

        if m:
            return clean(m.group(0))

    return ""


def parse_course(text: str) -> Dict[str, Any]:
    txt = clean(text)
    chunks = [clean(x) for x in txt.split(",")]

    course_types = {"curs", "sem", "seminar", "lab", "laborator", "proiect", "lp"}

    subject = chunks[0] if chunks else txt
    typ = ""
    teacher = ""
    group = ""

    if len(chunks) >= 2 and norm(chunks[1]) in course_types:
        typ = chunks[1]
        teacher = chunks[2] if len(chunks) > 2 else ""

        group_parts: List[str] = []

        if len(chunks) > 3:
            for x in chunks[3:]:
                nx = norm(x)

                if not nx:
                    continue

                if (
                    nx.startswith("sapt")
                    or nx.startswith("primele")
                    or nx.startswith("saptamanile")
                ):
                    continue

                group_parts.append(clean(x))

        group = ", ".join(group_parts)

    # If group is still empty, extract it from the full raw text.
    # This catches short/group-only rows and rows with commas inside parentheses.
    if is_blank_group(group):
        group = extract_group_from_text(txt)

    event = {
        "subject": subject,
        "type": typ,
        "teacher": teacher,
        "group": group,
        "raw": txt,
    }

    w = weeks_from_text(txt)

    if w:
        event["weeks"] = w

    return event


def attach_fragment_to_event(event: Dict[str, Any], fragment: str) -> None:
    fragment = clean(fragment)

    if not fragment:
        return

    old_raw = clean(event.get("raw", ""))
    event["raw"] = clean((old_raw + ", " + fragment).strip(" ,"))

    if is_blank_group(event.get("group")):
        group = extract_group_from_text(fragment)

        if group:
            event["group"] = group

    weeks = weeks_from_text(fragment)

    if weeks:
        old_weeks = event.get("weeks") if isinstance(event.get("weeks"), list) else []
        event["weeks"] = sorted(set(old_weeks + weeks))


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

        cell_events: List[Dict[str, Any]] = []
        pending_fragments: List[str] = []

        for part in split_events(raw):
            if is_event_fragment(part):
                if cell_events:
                    attach_fragment_to_event(cell_events[-1], part)
                else:
                    pending_fragments.append(part)
                continue

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

            for frag in pending_fragments:
                attach_fragment_to_event(ev, frag)
            pending_fragments = []

            cell_events.append(ev)
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

            raw_line = clean(" | ".join(cells))

            # Group is sometimes cells[6], but in many Orar modular rows
            # cells[6] is empty and the real group is cells[7] or later.
            group = ""

            for extra_cell in cells[6:]:
                candidate = clean(extra_cell)

                if not candidate:
                    continue

                extracted = extract_group_from_text(candidate)
                group = extracted or candidate
                break

            if is_blank_group(group):
                group = extract_group_from_text(raw_line)

            ev = {
                "roomCode": room_code,
                "date": ev_date,
                "dayIndex": day_idx,
                "start": start,
                "end": end,
                "subject": subject,
                "type": typ,
                "teacher": teacher,
                "group": group,
                "raw": raw_line,
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


COURSE_TYPES = {"curs", "sem", "seminar", "lab", "laborator", "proiect", "pr", "lp"}


def normalize_match_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", norm(value))


def normalize_teacher_for_match(value: Any) -> str:
    t = norm(value)
    t = re.sub(r"\b(prof|conf|lect|as|dr|drd|ing|univ)\b\.?", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    parts = t.split()
    if not parts:
        return ""
    # Keep the whole normalized string first; this is strict enough for exact matches.
    return " ".join(parts)


def short_teacher_for_match(value: Any) -> str:
    t = normalize_teacher_for_match(value)
    if not t:
        return ""
    return t.split()[0]


def normalize_group_label(value: Any) -> str:
    label = clean(value)
    if not label:
        return ""

    label = label.replace("___", " • ")
    label = label.replace("__", " • ")
    label = label.replace("_", " ")
    label = re.sub(r"\s*•\s*", " • ", label)
    label = re.sub(r"\s+", " ", label).strip(" •\t\r\n")
    return label


def is_probable_group_label(value: Any) -> bool:
    label = clean(value)
    n = norm(label)

    if not label or len(label) < 3:
        return False

    if any(
        x in n
        for x in ["luni", "marti", "miercuri", "joi", "vineri", "sambata", "duminica"]
    ):
        return False

    if "___" in label:
        return True

    if "grupa" in n or re.search(r"\ban(?:ul)?\s+\d+\b", n):
        return True

    return False


def extract_mobile_group_label(soup: BeautifulSoup, gid: int) -> str:
    candidates: List[str] = []

    for name in ["h1", "h2", "h3", "title", "b", "strong"]:
        for tag in soup.find_all(name):
            t = clean(tag.get_text(" ", strip=True))
            if t:
                candidates.append(t)

    for line in soup.get_text("\n", strip=True).splitlines()[:25]:
        t = clean(line)
        if t:
            candidates.append(t)

    for candidate in candidates:
        if is_probable_group_label(candidate):
            return normalize_group_label(candidate)

    return ""


def split_mobile_activity_segments(text: str) -> List[str]:
    line = clean(text)
    if not line:
        return []

    # Sometimes the mobile page/search text collapses multiple activities into one line.
    # Split only when there is whitespace before the next probable activity, so abbreviations
    # like "L.engl" or "G.F.R." are not broken.
    pattern = r"(?<=\.)\s+(?=[^,]{1,80},\s*(?:curs|sem|seminar|lab|laborator|proiect|pr|lp)\s*,\s*[^,]{1,30}\s*,)"
    pieces = [
        clean(x) for x in re.split(pattern, line, flags=re.IGNORECASE) if clean(x)
    ]

    if len(pieces) <= 1:
        return [line]

    return pieces


def parse_mobile_group_activity_line(
    line: str,
    group_label: str,
    gid: int,
    day_idx: Optional[int],
    start: str,
    end: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    if day_idx is None or not start or not end:
        return out

    for segment in split_mobile_activity_segments(line):
        chunks = [clean(x) for x in segment.split(",")]

        if len(chunks) < 4:
            continue

        typ = chunks[1]
        if norm(typ) not in COURSE_TYPES:
            continue

        room_code = extract_room_code(chunks[2])
        if not room_code:
            continue

        subject = chunks[0]
        teacher = chunks[3]

        if not subject or not teacher:
            continue

        out.append(
            {
                "group": group_label,
                "groupId": gid,
                "roomCode": room_code,
                "dayIndex": day_idx,
                "start": start,
                "end": end,
                "subject": subject,
                "type": typ,
                "teacher": teacher,
                "raw": segment,
                "source": "mobile_group",
            }
        )

    return out


def parse_mobile_group_events(
    soup: BeautifulSoup, group_label: str, gid: int
) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    current_day: Optional[int] = None
    current_start = ""
    current_end = ""

    lines = [clean(x) for x in soup.get_text("\n", strip=True).splitlines()]

    for raw_line in lines:
        line = clean(raw_line)
        if not line:
            continue

        n = norm(line)

        if n in DAY_MAP:
            current_day = DAY_MAP[n]
            current_start = ""
            current_end = ""
            continue

        interval_match = re.match(
            r"^(\d{1,2})(?::(\d{2}))?\s*[-–]\s*(\d{1,2})(?::(\d{2}))?\s*(.*)$", line
        )
        if interval_match:
            current_start = f"{int(interval_match.group(1)):02d}:{int(interval_match.group(2) or 0):02d}"
            current_end = f"{int(interval_match.group(3)):02d}:{int(interval_match.group(4) or 0):02d}"
            rest = clean(interval_match.group(5))

            if rest:
                events.extend(
                    parse_mobile_group_activity_line(
                        rest, group_label, gid, current_day, current_start, current_end
                    )
                )

            continue

        if "," in line and current_day is not None and current_start:
            events.extend(
                parse_mobile_group_activity_line(
                    line, group_label, gid, current_day, current_start, current_end
                )
            )

    return events


def scan_one_mobile_group(gid: int, timeout: int = 5) -> Optional[Dict[str, Any]]:
    url = MOBILE_GROUP_URL.format(id=gid)

    try:
        # Mobile group pages are lightweight. A short timeout prevents long tail stalls
        # when empty/non-existing IDs do not respond quickly.
        html = fetch(url, timeout=timeout, retries=1)
    except Exception:
        return None

    soup = BeautifulSoup(html, "html.parser")
    group_label = extract_mobile_group_label(soup, gid)

    if not group_label:
        return None

    events = parse_mobile_group_events(soup, group_label, gid)

    if not events:
        return None

    return {
        "id": gid,
        "group": group_label,
        "url": url,
        "events": events,
    }


def discover_mobile_group_ids_from_index() -> Dict[int, str]:
    """Try to read group IDs from the mobile index before falling back to ID scan."""
    found: Dict[int, str] = {}
    index_url = "https://orar.usv.ro/orar/mobil/vizualizare/orarUp1.php"

    try:
        html = fetch(index_url, timeout=15, retries=2)
    except Exception:
        return found

    soup = BeautifulSoup(html, "html.parser")

    for a in soup.find_all("a"):
        href = a.get("href") or ""
        text = clean(a.get_text(" ", strip=True) or a.get("title") or "")

        if "mod=grupa" not in href:
            continue

        m = re.search(r"[?&]ID=(\d+)", href)
        if not m:
            continue

        gid = int(m.group(1))
        if is_probable_group_label(text):
            found[gid] = normalize_group_label(text)
        else:
            found[gid] = ""

    for opt in soup.find_all("option"):
        text = clean(opt.get_text(" ", strip=True))
        val = clean(opt.get("value") or "")

        if not val.isdigit() or not is_probable_group_label(text):
            continue

        found[int(val)] = normalize_group_label(text)

    return found


def scan_mobile_group_id_batch(
    ids: List[int], workers: int, timeout: int
) -> List[Dict[str, Any]]:
    """Scan one bounded batch of mobile group IDs.

    Important: we do NOT submit thousands of futures at once. This avoids the
    "scanned groups 3000/3000 and waiting forever" feeling on slow Orar responses.
    """
    found: List[Dict[str, Any]] = []

    if not ids:
        return found

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(scan_one_mobile_group, gid, timeout): gid for gid in ids}

        for fut in as_completed(futures):
            try:
                item = fut.result()
            except Exception:
                item = None

            if item:
                found.append(item)
                print(
                    f"  group {item['id']} -> {item['group']} ({len(item.get('events', []))} events)",
                    flush=True,
                )

    return found


def scan_mobile_groups(args: argparse.Namespace) -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    workers = max(
        1, int(getattr(args, "group_workers", 0) or getattr(args, "workers", 4) or 4)
    )
    timeout = max(2, int(getattr(args, "group_timeout", 5) or 5))
    batch_size = max(20, int(getattr(args, "group_batch_size", 100) or 100))
    stop_after_empty = max(0, int(getattr(args, "group_stop_after_empty", 220) or 0))

    discovered: Dict[int, str] = {}

    if not getattr(args, "group_scan_only", False):
        discovered = discover_mobile_group_ids_from_index()

    if discovered:
        group_ids = sorted(discovered.keys())
        print(f"Mobile group index discovery: {len(group_ids)} candidate group IDs.")
        total = len(group_ids)

        for offset in range(0, total, batch_size):
            batch_ids = group_ids[offset : offset + batch_size]
            batch_found = scan_mobile_group_id_batch(batch_ids, workers, timeout)
            groups.extend(batch_found)
            print(
                f"  scanned groups {min(offset + len(batch_ids), total)}/{total} "
                f"(found {len(groups)})",
                flush=True,
            )

    else:
        print(f"Scanning mobile group IDs {args.group_scan_min}-{args.group_scan_max}...")
        total = args.group_scan_max - args.group_scan_min + 1
        scanned = 0
        empty_streak = 0

        current = args.group_scan_min
        while current <= args.group_scan_max:
            last = min(args.group_scan_max, current + batch_size - 1)
            batch_ids = list(range(current, last + 1))
            batch_found = scan_mobile_group_id_batch(batch_ids, workers, timeout)

            groups.extend(batch_found)
            scanned += len(batch_ids)

            if batch_found:
                # Count empty IDs after the latest found ID inside this batch.
                last_found_id = max(int(x.get("id", 0)) for x in batch_found)
                empty_streak = max(0, last - last_found_id)
            else:
                empty_streak += len(batch_ids)

            print(
                f"  scanned groups {scanned}/{total} "
                f"(found {len(groups)}, empty-streak {empty_streak})",
                flush=True,
            )

            # Orar mobile group IDs are clustered. After a long empty tail,
            # continuing up to 3000 only wastes time and causes the terminal to look stuck.
            if stop_after_empty and empty_streak >= stop_after_empty and groups:
                print(
                    f"  stopping group scan after {empty_streak} empty IDs "
                    f"(last found group around ID {max(int(g.get('id', 0)) for g in groups)})",
                    flush=True,
                )
                break

            current = last + 1

    groups.sort(key=lambda x: (str(x.get("group", "")), int(x.get("id", 0))))
    print(
        f"Mobile group scan complete: pages={len(groups)}, "
        f"events={sum(len(g.get('events', [])) for g in groups)}",
        flush=True,
    )
    return groups


def room_event_match_keys(event: Dict[str, Any]) -> Dict[str, Tuple[Any, ...]]:
    room = key(event.get("roomCode"))
    day = event.get("dayIndex")
    start = event.get("start") or event.get("startTime") or ""
    end = event.get("end") or event.get("endTime") or ""
    subject = normalize_match_text(
        event.get("subject")
        or event.get("discipline")
        or event.get("course")
        or event.get("title")
        or ""
    )
    typ = normalize_match_text(event.get("type") or "")
    teacher = normalize_teacher_for_match(
        event.get("teacher") or event.get("cadru") or event.get("professor") or ""
    )
    teacher_short = short_teacher_for_match(
        event.get("teacher") or event.get("cadru") or event.get("professor") or ""
    )

    return {
        "exact": (room, day, start, subject, typ, teacher),
        "exact_no_end": (room, day, start, subject, typ, teacher_short),
        "loose": (room, day, start, subject, typ),
        "subject_only": (room, day, start, subject),
        "with_end": (room, day, start, end, subject, typ),
    }


def add_group_to_room_event(
    event: Dict[str, Any], group_label: str, source: str, match_level: str
) -> bool:
    group_label = normalize_group_label(group_label)

    if not group_label:
        return False

    groups: List[str] = []

    existing_groups = event.get("groups")
    if isinstance(existing_groups, list):
        groups.extend(
            clean(x) for x in existing_groups if clean(x) and not is_blank_group(x)
        )

    existing_group = clean(event.get("group"))
    if existing_group and not is_blank_group(existing_group):
        groups.append(existing_group)

    normalized_seen = {norm(x) for x in groups}

    if norm(group_label) in normalized_seen:
        return False

    groups.append(group_label)
    event["groups"] = groups
    event["group"] = ", ".join(groups)
    event["groupSource"] = source
    event["groupMatch"] = match_level
    return True


def enrich_rooms_with_mobile_groups(
    out_rooms: Dict[str, Any], args: argparse.Namespace
) -> Dict[str, Any]:
    """
    Fast, indexed, no-silent-hang mobile group enrichment.

    Design:
      1) Scan mobile group schedules.
      2) Immediately save a debug/cache copy to data/mobile-group-schedules-cache.json.
      3) Build several dictionary indexes for room events.
      4) Match group events by O(1) dictionary lookups, not nested loops.
      5) Print progress every 1000 group events so terminal never looks frozen.
    """
    meta = {
        "enabled": True,
        "source": "mobile_group_schedules",
        "groupPages": 0,
        "groupEvents": 0,
        "matchedRoomEvents": 0,
        "multiGroupRoomEvents": 0,
        "ambiguousMatches": 0,
        "unmatchedGroupEvents": 0,
        "matchSeconds": 0.0,
    }

    t0 = time.perf_counter()
    groups = scan_mobile_groups(args)
    meta["groupPages"] = len(groups)
    meta["groupEvents"] = sum(len(g.get("events", [])) for g in groups)

    # Save the scan result before matching. If anything is interrupted later,
    # the expensive group scan is still available for inspection/reuse.
    try:
        cache_path = Path("data/mobile-group-schedules-cache.json")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {
                    "generatedAt": datetime.now().isoformat(timespec="seconds"),
                    "groupPages": meta["groupPages"],
                    "groupEvents": meta["groupEvents"],
                    "groups": groups,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Saved mobile group cache: {cache_path}", flush=True)
    except Exception as e:
        print(f"Warning: could not save mobile group cache: {e}", flush=True)

    if not groups:
        print("No mobile group pages found. Skipping group enrichment.", flush=True)
        return meta

    print(
        f"Matching {meta['groupEvents']} mobile group events to room events...",
        flush=True,
    )

    index_levels = ["exact", "exact_no_end", "with_end", "loose", "subject_only"]
    indexes: Dict[str, Dict[Tuple[Any, ...], List[Dict[str, Any]]]] = {
        level: {} for level in index_levels
    }

    indexed_room_events = 0

    for room in out_rooms.values():
        for event in room.get("events", []) or []:
            if not isinstance(event, dict):
                continue

            keys = room_event_match_keys(event)

            # Room, start and subject are minimum requirements.
            if not keys["exact"][0] or not keys["exact"][2] or not keys["exact"][3]:
                continue

            indexed_room_events += 1

            for level in index_levels:
                sig = keys[level]
                indexes[level].setdefault(sig, []).append(event)

    print(
        f"Built match indexes for {indexed_room_events} room events.",
        flush=True,
    )

    flat_group_events: List[Tuple[str, Dict[str, Any]]] = []
    for group_page in groups:
        group_label = group_page.get("group", "")
        for ge in group_page.get("events", []) or []:
            if isinstance(ge, dict):
                flat_group_events.append((group_label, ge))

    total_group_events = len(flat_group_events)

    for idx, (group_label, ge) in enumerate(flat_group_events, 1):
        keys = room_event_match_keys(ge)
        matched = False

        # Prefer strict keys. Fall back only when the candidate is unique.
        for level in index_levels:
            candidates = indexes[level].get(keys[level], [])

            if len(candidates) == 1:
                if add_group_to_room_event(
                    candidates[0],
                    group_label,
                    "mobile_group_schedule",
                    level,
                ):
                    meta["matchedRoomEvents"] += 1
                matched = True
                break

            if len(candidates) > 1:
                meta["ambiguousMatches"] += 1
                matched = True
                break

        if not matched:
            meta["unmatchedGroupEvents"] += 1

        if idx % 1000 == 0 or idx == total_group_events:
            print(
                f"  matched check {idx}/{total_group_events} "
                f"(matched={meta['matchedRoomEvents']}, "
                f"ambiguous={meta['ambiguousMatches']}, "
                f"unmatched={meta['unmatchedGroupEvents']})",
                flush=True,
            )

    multi = 0
    for room in out_rooms.values():
        for event in room.get("events", []) or []:
            if isinstance(event.get("groups"), list) and len(event.get("groups")) > 1:
                multi += 1

    meta["multiGroupRoomEvents"] = multi
    meta["matchSeconds"] = round(time.perf_counter() - t0, 2)

    print(
        "Group enrichment complete: "
        f"matched={meta['matchedRoomEvents']}, "
        f"multi={meta['multiGroupRoomEvents']}, "
        f"ambiguous={meta['ambiguousMatches']}, "
        f"unmatched={meta['unmatchedGroupEvents']}, "
        f"seconds={meta['matchSeconds']}",
        flush=True,
    )
    return meta


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
        soup,
        rid=int(room.get("id", "0") or 0),
        fallback_label=base["label"],
    )

    if parsed_info:
        building = parsed_info.get("building") or building
        building_key = parsed_info.get("buildingKey") or slug(building)
        code = parsed_info.get("roomCode") or code
        room_key = parsed_info.get("roomKey") or make_room_key(
            building,
            code,
            room.get("id", ""),
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
            # Keep JSON/frontend clean and support multiple groups from mobile group schedules.
            groups_value = e.get("groups")
            cleaned_groups: List[str] = []

            if isinstance(groups_value, list):
                for g in groups_value:
                    g = normalize_group_label(g)
                    if (
                        g
                        and not is_blank_group(g)
                        and norm(g) not in {norm(x) for x in cleaned_groups}
                    ):
                        cleaned_groups.append(g)

            group_value = normalize_group_label(e.get("group"))
            if (
                group_value
                and not is_blank_group(group_value)
                and norm(group_value) not in {norm(x) for x in cleaned_groups}
            ):
                cleaned_groups.append(group_value)

            if cleaned_groups:
                e["groups"] = cleaned_groups
                e["group"] = ", ".join(cleaned_groups)
            else:
                e["group"] = "-"
                e.pop("groups", None)

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
    ap.add_argument("--scan-max", type=int, default=1000)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument(
        "--no-group-enrichment",
        action="store_true",
        help="Disable mobile group schedule enrichment",
    )
    ap.add_argument("--group-scan-min", type=int, default=1)
    ap.add_argument("--group-scan-max", type=int, default=3000)
    ap.add_argument("--group-workers", type=int, default=4)
    ap.add_argument(
        "--group-timeout",
        type=int,
        default=5,
        help="Timeout in seconds for each mobile group page request",
    )
    ap.add_argument(
        "--group-batch-size",
        type=int,
        default=100,
        help="How many mobile group IDs to scan per batch",
    )
    ap.add_argument(
        "--group-stop-after-empty",
        type=int,
        default=50,
        help="Stop direct group scan after this many empty IDs once at least one group was found. 0 disables early stop.",
    )
    ap.add_argument(
        "--group-scan-only",
        action="store_true",
        help="Skip mobile index discovery and scan group IDs directly",
    )
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

    if args.no_group_enrichment:
        group_enrichment_meta = {"enabled": False, "source": "mobile_group_schedules"}
    else:
        print("Enriching room events with mobile group schedules...")
        group_enrichment_meta = enrich_rooms_with_mobile_groups(out_rooms, args)

    result = build_result(out_rooms, args, total_events)
    result["meta"]["groupEnrichment"] = group_enrichment_meta

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
