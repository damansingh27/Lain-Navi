"""
Google Calendar API (OAuth desktop flow) for NAVI tools.

Paths for `google_credentials.json` and `google_token.json` are under `assets/`,
resolved from this module so Calendar works regardless of process CWD. Times use
`America/New_York` as `LOCAL_TZ` for display and voice-friendly summaries.
"""

import datetime
import os
from collections import defaultdict
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]
PROJECT_ROOT = Path(__file__).resolve().parent
CREDENTIALS_PATH = str(PROJECT_ROOT / "assets" / "google_credentials.json")
TOKEN_PATH = str(PROJECT_ROOT / "assets" / "google_token.json")
LOCAL_TZ = ZoneInfo("America/New_York")

_service = None

def get_calendar_service():
    """Authenticates and returns a cached Google Calendar service instance."""
    global _service
    if _service is not None:
        return _service
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                # Token can become invalid after migration/account changes.
                # Force full re-auth and overwrite stale token.
                creds = None
        else:
            creds = None
        if creds is None:
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as token:
            token.write(creds.to_json())
    _service = build("calendar", "v3", credentials=creds)
    return _service


def _format_event_time(event):
    """Extracts and formats start time from an event."""
    start_time = event["start"].get("dateTime", event["start"].get("date"))
    if "T" in start_time:
        dt = datetime.datetime.fromisoformat(start_time).astimezone(LOCAL_TZ)
        return dt.strftime("%I:%M %p")
    return "All day"


def _title_short(title, max_len=28):
    t = (title or "(no title)").strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


def _event_local_date(event):
    start_time = event["start"].get("dateTime", event["start"].get("date"))
    if "T" in str(start_time):
        dt = datetime.datetime.fromisoformat(start_time).astimezone(LOCAL_TZ)
        return dt.date()
    raw = (start_time or "")[:10]
    try:
        y, m, d = (int(x) for x in raw.split("-"))
        return datetime.date(y, m, d)
    except Exception:
        return datetime.datetime.now(LOCAL_TZ).date()


def _voice_format_day_clauses(events, for_relative_day: Optional[str] = None):
    """
    Short spoken-style summary for a single day (today / tomorrow / etc.).
    """
    if not events:
        return "Nothing scheduled."
    day_phrase = for_relative_day or "That day"
    if len(events) == 1:
        e = events[0]
        return (
            f"{day_phrase}: {_format_event_time(e)} — "
            f"{_title_short(e.get('summary'), 72)}"
        )
    parts = [f"{_format_event_time(e)} {_title_short(e.get('summary'))}" for e in events]
    return f"{day_phrase}, {len(events)} events: " + ", ".join(parts)


def _day_bounds(date):
    """Returns (start_iso, end_iso) for a given date in local timezone."""
    start = datetime.datetime(date.year, date.month, date.day, tzinfo=LOCAL_TZ)
    end = start + datetime.timedelta(days=1) - datetime.timedelta(seconds=1)
    return start.isoformat(), end.isoformat()


def _parse_datetime(value):
    """
    Parses an ISO datetime string and returns a timezone-aware datetime in LOCAL_TZ.
    Naive datetimes are interpreted as LOCAL_TZ.
    """
    if not value:
        raise ValueError("datetime value is required")
    dt = datetime.datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    else:
        dt = dt.astimezone(LOCAL_TZ)
    return dt


def _event_datetime(event, key):
    raw = event.get(key, {}).get("dateTime")
    if not raw:
        return None
    dt = datetime.datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)


def _event_summary_payload(event):
    start_dt = _event_datetime(event, "start")
    end_dt = _event_datetime(event, "end")
    return {
        "id": event.get("id"),
        "summary": event.get("summary", "(no title)"),
        "start": start_dt.isoformat() if start_dt else event.get("start", {}).get("date"),
        "end": end_dt.isoformat() if end_dt else event.get("end", {}).get("date"),
        "time_zone": LOCAL_TZ.key,
        "html_link": event.get("htmlLink"),
    }


def get_events_today():
    """Returns today's events as a readable string."""
    service = get_calendar_service()
    today = datetime.datetime.now(LOCAL_TZ).date()
    start, end = _day_bounds(today)
    events_result = service.events().list(
        calendarId="primary",
        timeMin=start,
        timeMax=end,
        singleEvents=True,
        orderBy="startTime"
    ).execute()
    events = events_result.get("items", [])
    if not events:
        return "No events today."
    return _voice_format_day_clauses(events, for_relative_day="Today")


def get_events_tomorrow():
    """Returns tomorrow's events as a readable string."""
    service = get_calendar_service()
    tomorrow = datetime.datetime.now(LOCAL_TZ).date() + datetime.timedelta(days=1)
    start, end = _day_bounds(tomorrow)
    events_result = service.events().list(
        calendarId="primary",
        timeMin=start,
        timeMax=end,
        singleEvents=True,
        orderBy="startTime"
    ).execute()
    events = events_result.get("items", [])
    if not events:
        return "No events tomorrow."
    return _voice_format_day_clauses(events, for_relative_day="Tomorrow")


def get_events_this_week():
    """Returns this week's events as a readable string."""
    service = get_calendar_service()
    now = datetime.datetime.now(LOCAL_TZ)
    end = now + datetime.timedelta(days=7)
    events_result = service.events().list(
        calendarId="primary",
        timeMin=now.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime"
    ).execute()
    events = events_result.get("items", [])
    if not events:
        return "No events in the next 7 days."
    by_day = defaultdict(list)
    for event in events:
        by_day[_event_local_date(event)].append(event)

    def _start_key(ev):
        st = ev["start"].get("dateTime", ev["start"].get("date"))
        if st and "T" in str(st):
            return datetime.datetime.fromisoformat(st).astimezone(LOCAL_TZ)
        d = _event_local_date(ev)
        return datetime.datetime.combine(d, datetime.time.min, tzinfo=LOCAL_TZ)

    intro = (
        f"Next 7 days: {len(events)} calendar item{'s' if len(events) != 1 else ''}, "
        "by day. "
    )
    day_bits = []
    for day in sorted(by_day.keys()):
        dname = day.strftime("%A")
        evs = sorted(by_day[day], key=_start_key)
        line = f"{dname}: " + ", ".join(
            f"{_format_event_time(e)} {_title_short(e.get('summary'), 22)}" for e in evs
        )
        day_bits.append(line)
    out = intro + " ".join(day_bits)
    if len(out) > 1000:
        out = out[:997] + "…"
    return out


def create_event(summary, start_datetime, end_datetime, description=""):
    """Creates a calendar event."""
    service = get_calendar_service()
    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_datetime, "timeZone": LOCAL_TZ.key},
        "end": {"dateTime": end_datetime, "timeZone": LOCAL_TZ.key},
    }
    event = service.events().insert(calendarId="primary", body=event).execute()
    return f"Event created: {event.get('summary')}"


def delete_event(event_summary):
    """Deletes the first upcoming event matching the summary."""
    service = get_calendar_service()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    events_result = service.events().list(
        calendarId="primary",
        timeMin=now,
        maxResults=50,
        singleEvents=True,
        orderBy="startTime"
    ).execute()
    events = events_result.get("items", [])
    for event in events:
        if event_summary.lower() in event.get("summary", "").lower():
            service.events().delete(
                calendarId="primary",
                eventId=event["id"]
            ).execute()
            return f"Deleted event: {event['summary']}"
    return f"No event found matching: {event_summary}"


def move_event(event_summary, new_start_datetime, new_end_datetime):
    """
    Moves the first upcoming event matching event_summary.

    Returns a structured payload with old/new timestamps for predictable tool output.
    """
    service = get_calendar_service()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    events_result = service.events().list(
        calendarId="primary",
        timeMin=now,
        maxResults=50,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    events = events_result.get("items", [])

    target = None
    for event in events:
        if event_summary.lower() in event.get("summary", "").lower():
            target = event
            break

    if target is None:
        return {
            "status": "not_found",
            "event_summary_query": event_summary,
            "message": f"No upcoming event found matching '{event_summary}'.",
        }

    new_start = _parse_datetime(new_start_datetime)
    new_end = _parse_datetime(new_end_datetime)
    if new_end <= new_start:
        return {
            "status": "invalid_time_range",
            "message": "new_end_datetime must be after new_start_datetime.",
            "new_start_datetime": new_start_datetime,
            "new_end_datetime": new_end_datetime,
        }

    old_payload = _event_summary_payload(target)
    target["start"] = {"dateTime": new_start.isoformat(), "timeZone": LOCAL_TZ.key}
    target["end"] = {"dateTime": new_end.isoformat(), "timeZone": LOCAL_TZ.key}

    updated = service.events().update(
        calendarId="primary",
        eventId=target["id"],
        body=target,
    ).execute()

    return {
        "status": "moved",
        "message": f"Moved event '{updated.get('summary', '(no title)')}'.",
        "old": old_payload,
        "new": _event_summary_payload(updated),
    }


def check_freebusy(start_datetime, end_datetime):
    """
    Returns busy windows in the given range as a structured payload.
    Datetimes must be ISO 8601; naive values are treated as LOCAL_TZ.
    """
    service = get_calendar_service()
    start_dt = _parse_datetime(start_datetime)
    end_dt = _parse_datetime(end_datetime)
    if end_dt <= start_dt:
        return {
            "status": "invalid_time_range",
            "message": "end_datetime must be after start_datetime.",
            "start_datetime": start_datetime,
            "end_datetime": end_datetime,
        }

    body = {
        "timeMin": start_dt.isoformat(),
        "timeMax": end_dt.isoformat(),
        "timeZone": LOCAL_TZ.key,
        "items": [{"id": "primary"}],
    }
    result = service.freebusy().query(body=body).execute()
    busy_ranges = (result.get("calendars", {}).get("primary", {}).get("busy", []))

    busy_payload = []
    for item in busy_ranges:
        busy_start = _parse_datetime(item["start"])
        busy_end = _parse_datetime(item["end"])
        busy_payload.append(
            {
                "start": busy_start.isoformat(),
                "end": busy_end.isoformat(),
                "start_display": busy_start.strftime("%A %I:%M %p"),
                "end_display": busy_end.strftime("%A %I:%M %p"),
            }
        )

    return {
        "status": "ok",
        "time_zone": LOCAL_TZ.key,
        "query_window": {
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
        },
        "busy_count": len(busy_payload),
        "busy": busy_payload,
    }