#!/usr/bin/env python3
"""USV Occupancy automatic data pipeline v7.

This script is designed for GitHub Actions / server cron.
It fetches Orar + academic-calendar data into temporary files, validates them,
and only publishes the new JSON if it is healthy. If something fails, the last
known good JSON stays in place so the frontend/APK does not break.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Tuple

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
BACKUP = ROOT / "backup"
LOGS = ROOT / "logs"
TOOLS = ROOT / "tools"
OCC_FINAL = DATA / "occupancy-corp-e.json"
CAL_FINAL = DATA / "academic-calendar.json"
HEALTH_FINAL = DATA / "health.json"
OCC_LAST_GOOD = BACKUP / "last-good-occupancy-corp-e.json"
CAL_LAST_GOOD = BACKUP / "last-good-academic-calendar.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def log(message: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    line = f"[{now_iso()}] {message}"
    print(line)
    with (LOGS / "update.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(cmd: list[str]) -> Tuple[int, str]:
    log("RUN " + " ".join(str(x) for x in cmd))
    proc = subprocess.run(cmd, cwd=str(ROOT), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = proc.stdout or ""
    for line in out.splitlines():
        log("  " + line)
    return proc.returncode, out


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_health(status: str, message: str, occ_msg: str = "", cal_msg: str = "") -> None:
    rooms = 0
    events = 0
    generated_at = None
    calendar_state = "unknown"
    try:
        occ = read_json(OCC_FINAL)
        meta = occ.get("meta", {})
        rooms = int(meta.get("roomCount") or len(occ.get("rooms", {}) or {}))
        events = int(meta.get("eventCount") or sum(len(v.get("events", [])) for v in (occ.get("rooms", {}) or {}).values() if isinstance(v, dict)))
        generated_at = meta.get("generatedAt")
    except Exception:
        pass
    try:
        cal = read_json(CAL_FINAL)
        calendar_state = "ok" if (cal.get("teachingPeriods") or cal.get("vacations")) else "weak"
    except Exception:
        calendar_state = "missing"

    payload = {
        "status": status,
        "lastUpdated": now_iso(),
        "occupancyGeneratedAt": generated_at,
        "message": message,
        "occupancyValidation": occ_msg,
        "calendarValidation": cal_msg,
        "rooms": rooms,
        "events": events,
        "calendar": calendar_state,
        "source": "orar.usv.ro + usv.ro academic calendar",
        "version": "v7-auto-pipeline"
    }
    DATA.mkdir(parents=True, exist_ok=True)
    HEALTH_FINAL.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log("HEALTH " + json.dumps(payload, ensure_ascii=False))


def validate_occupancy(path: Path, min_rooms: int, min_events: int) -> Tuple[bool, str]:
    if not path.exists():
        return False, f"missing file: {path}"
    try:
        data = read_json(path)
    except Exception as exc:
        return False, f"invalid JSON: {exc}"
    rooms = data.get("rooms")
    meta = data.get("meta", {})
    if not isinstance(rooms, dict):
        return False, "expected object with rooms dictionary"
    room_count = len(rooms)
    event_count = int(meta.get("eventCount") or sum(len(v.get("events", [])) for v in rooms.values() if isinstance(v, dict)))
    if room_count < min_rooms:
        return False, f"too few rooms: {room_count} < {min_rooms}"
    if event_count < min_events:
        return False, f"too few events: {event_count} < {min_events}"
    bad_time = 0
    empty_room_codes = 0
    for code, info in rooms.items():
        if not str(code).strip():
            empty_room_codes += 1
        events = info.get("events", []) if isinstance(info, dict) else []
        for ev in events:
            start = str(ev.get("start") or "")
            end = str(ev.get("end") or "")
            if not start or not end or start >= end:
                bad_time += 1
                if bad_time > 25:
                    return False, "many invalid time intervals"
    if empty_room_codes:
        return False, f"empty room codes: {empty_room_codes}"
    return True, f"OK rooms={room_count}, events={event_count}, badTime={bad_time}"


def validate_calendar(path: Path) -> Tuple[bool, str]:
    if not path.exists():
        return False, f"missing file: {path}"
    try:
        data = read_json(path)
    except Exception as exc:
        return False, f"invalid JSON: {exc}"
    teaching = data.get("teachingPeriods") or []
    vacations = data.get("vacations") or []
    sessions = data.get("sessions") or []
    if len(teaching) < 2:
        return False, f"too few teaching periods: {len(teaching)}"
    if len(vacations) < 1:
        return False, f"too few vacations: {len(vacations)}"
    return True, f"OK teaching={len(teaching)}, vacations={len(vacations)}, sessions={len(sessions)}"


def backup_current(final_path: Path, last_good_path: Path) -> None:
    BACKUP.mkdir(parents=True, exist_ok=True)
    if final_path.exists():
        shutil.copy2(final_path, last_good_path)


def promote(tmp_path: Path, final_path: Path, last_good_path: Path) -> None:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    backup_current(final_path, last_good_path)
    shutil.copy2(tmp_path, final_path)
    shutil.copy2(tmp_path, last_good_path)
    log(f"PROMOTED {tmp_path.name} -> {final_path}")


def restore_last_good(final_path: Path, last_good_path: Path, label: str) -> bool:
    if last_good_path.exists():
        final_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(last_good_path, final_path)
        log(f"RESTORED last-good {label}: {last_good_path} -> {final_path}")
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-rooms", type=int, default=60)
    parser.add_argument("--min-events", type=int, default=500)
    parser.add_argument("--scan-max", type=int, default=850)
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()

    DATA.mkdir(parents=True, exist_ok=True)
    BACKUP.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    tmp_occ = DATA / f"_tmp_occupancy-corp-e-{stamp()}.json"
    tmp_cal = DATA / f"_tmp_academic-calendar-{stamp()}.json"

    log("========== USV OCCUPANCY UPDATE START ==========")
    occ_msg = "not run"
    cal_msg = "not run"
    status = "ok"
    message_parts = []

    occ_code, _ = run([sys.executable, str(TOOLS / "fetch_usv_corp_e_orar.py"), "--output", str(tmp_occ), "--scan-max", str(args.scan_max), "--workers", str(args.workers)])
    occ_ok, occ_msg = validate_occupancy(tmp_occ, args.min_rooms, args.min_events)
    if occ_code == 0 and occ_ok:
        promote(tmp_occ, OCC_FINAL, OCC_LAST_GOOD)
        message_parts.append("occupancy updated")
    else:
        status = "degraded"
        message_parts.append("occupancy rejected; using last-good if available")
        log(f"Occupancy update rejected. exit={occ_code}; validation={occ_msg}")
        if not restore_last_good(OCC_FINAL, OCC_LAST_GOOD, "occupancy"):
            log("No last-good occupancy backup exists yet.")
            status = "error"

    cal_code, _ = run([sys.executable, str(TOOLS / "fetch_usv_academic_calendar.py"), "--out", str(tmp_cal)])
    cal_ok, cal_msg = validate_calendar(tmp_cal)
    if cal_code == 0 and cal_ok:
        promote(tmp_cal, CAL_FINAL, CAL_LAST_GOOD)
        message_parts.append("calendar updated")
    else:
        status = "degraded" if status != "error" else "error"
        message_parts.append("calendar rejected; using last-good if available")
        log(f"Calendar update rejected. exit={cal_code}; validation={cal_msg}")
        if not restore_last_good(CAL_FINAL, CAL_LAST_GOOD, "calendar"):
            log("No last-good calendar backup exists yet.")
            status = "error"

    for tmp in [tmp_occ, tmp_cal]:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass

    write_health(status, "; ".join(message_parts), occ_msg, cal_msg)
    log("========== USV OCCUPANCY UPDATE END ==========")
    return 0 if (OCC_FINAL.exists() and CAL_FINAL.exists() and status in {"ok", "degraded"}) else 1


if __name__ == "__main__":
    raise SystemExit(main())
