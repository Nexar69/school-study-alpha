from __future__ import annotations

import json
import os
import re
import urllib.request
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

OUT = Path(__file__).resolve().parents[1] / "edupage-tests.json"
BERLIN = ZoneInfo("Europe/Berlin")

SUBJECTS = {
    "en": "English", "english": "English",
    "de": "Deutsch", "deutsch": "Deutsch",
    "ge": "Geschichte", "geschichte": "Geschichte",
    "geo": "Geographie", "geographie": "Geographie",
    "ma": "Mathematik", "mathematik": "Mathematik",
    "bio": "Biologie", "biologie": "Biologie",
    "ch": "Chemie", "chemie": "Chemie",
    "ph": "Physik", "physik": "Physik",
    "sp": "Spanisch", "span": "Spanisch", "spanisch": "Spanisch",
}

TEST_RE = re.compile(
    r"\b(test|klassenarbeit|klausur|lernerfolgskontrolle|lernkontrolle|"
    r"leistungskontrolle|schriftliche arbeit|arbeit|vas\s*\d)\b",
    re.IGNORECASE,
)
SUBJECT_RE = re.compile(r"(?:Schulfach|Fach):\s*([^\s,;:]+)", re.IGNORECASE)
PREFIX_RE = re.compile(
    r"^\s*(?:test|klassenarbeit|klausur|lernerfolgskontrolle|lernkontrolle|"
    r"leistungskontrolle|schriftliche arbeit|arbeit)\s*[:\-–—]?\s*",
    re.IGNORECASE,
)

def unfold_ics(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for line in lines:
        if line.startswith((" ", "\t")) and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out

def unescape(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )

def parse_dt(key: str, value: str) -> datetime:
    params = key.split(";")[1:]
    tzid = None
    for param in params:
        if param.startswith("TZID="):
            tzid = param.split("=", 1)[1]
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=ZoneInfo("UTC"))
    if "T" in value:
        dt = datetime.strptime(value, "%Y%m%dT%H%M%S")
        return dt.replace(tzinfo=ZoneInfo(tzid) if tzid else BERLIN)
    return datetime.strptime(value, "%Y%m%d").replace(tzinfo=BERLIN)

def normalize_subject(code: str | None) -> str:
    if not code:
        return "Unbekannt"
    return SUBJECTS.get(code.strip().lower(), code.strip())

def parse_events(text: str) -> list[dict]:
    events: list[dict] = []
    current: dict[str, str] | None = None
    for line in unfold_ics(text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        key, value = line.split(":", 1)
        base = key.split(";", 1)[0]
        if base in {"UID", "SUMMARY", "DESCRIPTION", "LOCATION", "DTSTART", "DTEND"}:
            current[key if base in {"DTSTART", "DTEND"} else base] = unescape(value)
    return events

def assessment_from_event(event: dict[str, str]) -> dict | None:
    title = event.get("SUMMARY", "").strip()
    description = event.get("DESCRIPTION", "")
    if not title or not TEST_RE.search(title):
        return None

    start_key = next((k for k in event if k.startswith("DTSTART")), None)
    end_key = next((k for k in event if k.startswith("DTEND")), None)
    if not start_key or not end_key:
        return None

    subject_match = SUBJECT_RE.search(description)
    subject_code = subject_match.group(1) if subject_match else None
    topic = PREFIX_RE.sub("", title).strip() or title

    return {
        "id": event.get("UID", f"{title}-{event[start_key]}"),
        "title": title,
        "topic": topic,
        "subject": normalize_subject(subject_code),
        "subjectCode": subject_code,
        "start": parse_dt(start_key, event[start_key]).isoformat(),
        "end": parse_dt(end_key, event[end_key]).isoformat(),
        "location": event.get("LOCATION", "").strip() or None,
        "source": "edupage",
    }

def merge_adjacent(items: list[dict]) -> list[dict]:
    items = sorted(items, key=lambda item: item["start"])
    merged: list[dict] = []
    for item in items:
        if merged:
            prev = merged[-1]
            if (
                prev["title"] == item["title"]
                and prev["subject"] == item["subject"]
                and datetime.fromisoformat(prev["end"]) == datetime.fromisoformat(item["start"])
            ):
                prev["end"] = item["end"]
                prev["id"] = f'{prev["id"]}+{item["id"]}'
                continue
        merged.append(item.copy())
    return merged

def main() -> None:
    url = os.environ.get("EDUPAGE_WEBCAL_URL", "").strip()
    if not url:
        raise SystemExit("EDUPAGE_WEBCAL_URL is not configured")

    try:
        completed = subprocess.run(
            [
                "curl", "-4", "--fail", "--silent", "--show-error", "--location",
                "--max-time", "30", "--user-agent", "Studyroom-EduPage-Sync/1.0", url,
            ],
            check=True,
            capture_output=True,
        )
        raw = completed.stdout.decode("utf-8-sig", errors="replace")
    except (FileNotFoundError, subprocess.CalledProcessError):
        request = urllib.request.Request(url, headers={"User-Agent": "Studyroom-EduPage-Sync/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8-sig", errors="replace")

    tests = merge_adjacent(
        [item for event in parse_events(raw) if (item := assessment_from_event(event))]
    )
    payload = {
        "generatedAt": datetime.now(tz=ZoneInfo("UTC")).isoformat(),
        "source": "EduPage Webcal",
        "tests": tests,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Synced {len(tests)} test(s).")

if __name__ == "__main__":
    main()
