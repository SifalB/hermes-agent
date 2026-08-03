#!/usr/bin/env python3
"""Google Calendar MCP server for Hermes, ported from Cassandra's src/gcal.py.

Auth reuses the same service-account key Cassandra used: the JSON blob in
GOOGLE_SERVICE_ACCOUNT_JSON, with the target calendar in GOOGLE_CALENDAR_ID
(optional — falls back to the single calendar shared with the service
account, matching Cassandra's autodiscovery behaviour).

Run under uv so the Google client libraries resolve at runtime without
baking them into the image:

    uv run --with mcp --with google-api-python-client --with google-auth \
        python /opt/hermes/railway/gcal_mcp.py
"""
from __future__ import annotations

import json
import os
import unicodedata
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TZ = ZoneInfo(os.environ.get("HERMES_TIMEZONE", "Europe/Paris"))

# Google Calendar colorId 1-11, keyed by the French names Cassandra accepted
# so existing phrasing ("mets-le en rouge") keeps working.
COLOR_MAP = {
    "lavande": "1", "bleu clair": "1",
    "sauge": "2", "vert clair": "2", "vert d eau": "2",
    "raisin": "3", "violet": "3", "mauve": "3",
    "flamant": "4", "rose": "4", "saumon": "4",
    "banane": "5", "jaune": "5",
    "mandarine": "6", "orange": "6",
    "paon": "7", "turquoise": "7", "cyan": "7",
    "graphite": "8", "gris": "8",
    "myrtille": "9", "bleu": "9", "bleu fonce": "9",
    "basilic": "10", "vert": "10", "vert fonce": "10",
    "tomate": "11", "rouge": "11",
}

mcp = FastMCP("google-calendar")

_service = None
_calendar_id = ""


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower().strip()


def _color_id(name: str | None) -> str | None:
    if not name:
        return None
    key = _norm(name)
    if key in COLOR_MAP:
        return COLOR_MAP[key]
    for k, v in COLOR_MAP.items():
        if k in key or key in k:
            return v
    return None


def _connect():
    """Build the Calendar client once, resolving the calendar id if unset."""
    global _service, _calendar_id
    if _service is not None:
        return _service, _calendar_id

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    info = json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    _service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    _calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "").strip()
    if not _calendar_id:
        sa_email = info.get("client_email", "")
        items = _service.calendarList().list().execute().get("items", [])
        shared = [c for c in items if not c.get("primary") and c.get("id") != sa_email]
        if not shared:
            raise RuntimeError(
                f"No calendar shared with the service account ({sa_email}). "
                "Share the calendar with that address or set GOOGLE_CALENDAR_ID."
            )
        writable = [c for c in shared if c.get("accessRole") in ("writer", "owner")]
        _calendar_id = (writable or shared)[0]["id"]

    return _service, _calendar_id


def _time_fields(start: str, end: str | None, all_day: bool) -> dict:
    if all_day:
        d = date.fromisoformat(start[:10])
        end_d = date.fromisoformat(end[:10]) if end else d + timedelta(days=1)
        return {"start": {"date": d.isoformat()}, "end": {"date": end_d.isoformat()}}
    sdt = datetime.fromisoformat(start)
    if sdt.tzinfo is None:
        sdt = sdt.replace(tzinfo=TZ)
    edt = datetime.fromisoformat(end) if end else sdt + timedelta(hours=1)
    if edt.tzinfo is None:
        edt = edt.replace(tzinfo=TZ)
    return {
        "start": {"dateTime": sdt.isoformat(), "timeZone": TZ.key},
        "end": {"dateTime": edt.isoformat(), "timeZone": TZ.key},
    }


def _fmt(item: dict) -> dict:
    start_raw = item.get("start", {})
    end_raw = item.get("end", {})
    return {
        "id": item.get("id", ""),
        "summary": item.get("summary", "(sans titre)"),
        "start": start_raw.get("dateTime") or start_raw.get("date", ""),
        "end": end_raw.get("dateTime") or end_raw.get("date", ""),
        "all_day": "date" in start_raw,
        "location": item.get("location", "") or "",
    }


@mcp.tool()
def list_events(start: str, end: str) -> list[dict]:
    """List calendar events between two dates (inclusive), 'YYYY-MM-DD' each."""
    svc, cid = _connect()
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    time_min = datetime(s.year, s.month, s.day, tzinfo=TZ)
    time_max = datetime(e.year, e.month, e.day, tzinfo=TZ) + timedelta(days=1)
    resp = svc.events().list(
        calendarId=cid,
        timeMin=time_min.isoformat(),
        timeMax=time_max.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=250,
    ).execute()
    return [_fmt(i) for i in resp.get("items", [])]


@mcp.tool()
def create_event(summary: str, start: str, end: str = "", location: str = "",
                 all_day: bool = False, description: str = "", color: str = "") -> dict:
    """Create an event. `start`/`end` are 'YYYY-MM-DD' when all_day, else
    'YYYY-MM-DDTHH:MM'. `color` accepts French colour names (rouge, bleu...)."""
    svc, cid = _connect()
    body: dict = {"summary": summary, **_time_fields(start, end or None, all_day)}
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    cid_color = _color_id(color)
    if cid_color:
        body["colorId"] = cid_color
    return _fmt(svc.events().insert(calendarId=cid, body=body).execute())


@mcp.tool()
def update_event(event_id: str, summary: str = "", start: str = "", end: str = "",
                 location: str = "", all_day: bool = False, color: str = "") -> dict:
    """Patch an existing event. Only non-empty fields are changed."""
    svc, cid = _connect()
    body: dict = {}
    if summary:
        body["summary"] = summary
    if location:
        body["location"] = location
    if start:
        body.update(_time_fields(start, end or None, all_day))
    if color:
        c = _color_id(color)
        if c:
            body["colorId"] = c
    if not body:
        raise ValueError("Nothing to update.")
    return _fmt(svc.events().patch(calendarId=cid, eventId=event_id, body=body).execute())


@mcp.tool()
def delete_event(event_id: str) -> str:
    """Delete an event by id."""
    svc, cid = _connect()
    svc.events().delete(calendarId=cid, eventId=event_id).execute()
    return f"deleted {event_id}"


if __name__ == "__main__":
    mcp.run()
