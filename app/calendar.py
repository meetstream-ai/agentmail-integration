"""Google and Outlook calendars, kept in sync by MeetStream: connect one, read its meetings."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from app.invites import find_meeting_link

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
# Read-only: the agent never changes anyone's calendar. These are the scopes
# MeetStream's calendar sync asks for.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


def auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    return (
        GOOGLE_AUTH
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(SCOPES),
                # A refresh token, so MeetStream can keep reading the calendar later
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
            }
        )
    )


async def refresh_token_for(http: httpx.AsyncClient, client_id: str, secret: str, code: str, redirect_uri: str) -> str:
    resp = await http.post(
        GOOGLE_TOKEN,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    resp.raise_for_status()
    token = resp.json().get("refresh_token")
    if not token:
        # Google only issues one on first consent; a stale grant has to be removed first.
        raise ValueError(
            "Google sent no refresh token. Remove the app at https://myaccount.google.com/permissions and connect again."
        )
    return token


MICROSOFT_LOGIN = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/{step}"
# Read-only calendar access, plus offline_access for a refresh token.
MICROSOFT_SCOPES = "offline_access openid profile email User.Read Calendars.Read"


def microsoft_auth_url(client_id: str, tenant: str, redirect_uri: str, state: str) -> str:
    return (
        MICROSOFT_LOGIN.format(tenant=tenant, step="authorize")
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "response_mode": "query",
                "scope": MICROSOFT_SCOPES,
                "prompt": "consent",
                "state": state,
            }
        )
    )


async def microsoft_refresh_token_for(
    http: httpx.AsyncClient, client_id: str, secret: str, tenant: str, code: str, redirect_uri: str
) -> str:
    resp = await http.post(
        MICROSOFT_LOGIN.format(tenant=tenant, step="token"),
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "scope": MICROSOFT_SCOPES,
        },
    )
    if resp.status_code >= 400:
        # Microsoft explains the problem (AADSTS...) in the body; pass it on.
        raise ValueError(resp.json().get("error_description", resp.text)[:300])
    token = resp.json().get("refresh_token")
    if not token:
        raise ValueError("Microsoft sent no refresh token; check the app has the offline_access permission.")
    return token


@dataclass
class CalendarEvent:
    uid: str
    calendar_id: str | None
    account_id: str | None
    title: str
    start: datetime | None
    end: datetime | None
    timezone: str | None
    meeting_link: str | None
    cancelled: bool
    organizer: str | None
    organizer_name: str | None
    description: str = ""
    attendees: list[str] = field(default_factory=list)


def _utc(value: str | None) -> datetime | None:
    if not value:
        return None
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def parse_event(item: dict) -> CalendarEvent | None:
    """One event from MeetStream's /calendar/events. `raw` is the calendar's own
    event: Google's, or Microsoft Graph's for Outlook."""
    raw = item.get("raw") or {}
    outlook = "outlook" in (item.get("platform") or "").lower() or "subject" in raw or "isAllDay" in raw
    return (_parse_outlook if outlook else _parse_google)(item, raw)


def _parse_google(item: dict, raw: dict) -> CalendarEvent | None:
    start = raw.get("start") or {}
    if "date" in start and "dateTime" not in start:
        return None  # all-day: no time to join at
    uid = item.get("ical_uid") or raw.get("iCalUID") or item.get("id")
    if not uid:
        return None
    if raw.get("recurringEventId"):
        # Every occurrence of a series shares one iCalendar UID.
        uid = f"{uid}#{item.get('start_time') or start.get('dateTime')}"
    organizer = raw.get("organizer") or {}
    return CalendarEvent(
        uid=uid,
        calendar_id=item.get("calendar_id"),
        account_id=item.get("account_id"),
        title=(raw.get("summary") or "").strip() or "Meeting",
        start=_utc(item.get("start_time") or start.get("dateTime")),
        end=_utc(item.get("end_time") or (raw.get("end") or {}).get("dateTime")),
        timezone=start.get("timeZone"),
        meeting_link=item.get("meeting_url")
        or raw.get("hangoutLink")
        or find_meeting_link(raw.get("location"), raw.get("description")),
        cancelled=bool(item.get("is_deleted")) or raw.get("status") == "cancelled",
        organizer=(organizer.get("email") or "").lower() or None,
        organizer_name=organizer.get("displayName"),
        description=raw.get("description") or "",
        attendees=[
            a["email"].lower()
            for a in raw.get("attendees") or []
            if a.get("email") and not a.get("resource") and a.get("responseStatus") != "declined"
        ],
    )


# Outlook names time zones the Windows way. The common ones, as IANA names.
WINDOWS_ZONES = {
    "UTC": "UTC",
    "GMT Standard Time": "Europe/London",
    "W. Europe Standard Time": "Europe/Berlin",
    "Romance Standard Time": "Europe/Paris",
    "Central Europe Standard Time": "Europe/Budapest",
    "E. Europe Standard Time": "Europe/Chisinau",
    "GTB Standard Time": "Europe/Bucharest",
    "Russian Standard Time": "Europe/Moscow",
    "Arabian Standard Time": "Asia/Dubai",
    "India Standard Time": "Asia/Kolkata",
    "Sri Lanka Standard Time": "Asia/Colombo",
    "Singapore Standard Time": "Asia/Singapore",
    "China Standard Time": "Asia/Shanghai",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "New Zealand Standard Time": "Pacific/Auckland",
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    "E. South America Standard Time": "America/Sao_Paulo",
    "South Africa Standard Time": "Africa/Johannesburg",
}


def _outlook_zone(raw: dict) -> str | None:
    name = raw.get("originalStartTimeZone") or (raw.get("start") or {}).get("timeZone")
    if not name:
        return None
    return name if "/" in name else WINDOWS_ZONES.get(name)


def _graph_utc(when: dict | None) -> datetime | None:
    # Graph sends {"dateTime": "2026-10-20T14:00:00.0000000", "timeZone": "UTC"}
    value = (when or {}).get("dateTime")
    if not value or ((when or {}).get("timeZone") or "UTC") != "UTC":
        return None
    return _utc(value[:19])


def _parse_outlook(item: dict, raw: dict) -> CalendarEvent | None:
    if raw.get("isAllDay"):
        return None  # no time to join at
    uid = item.get("ical_uid") or raw.get("iCalUId") or item.get("id")
    if not uid:
        return None
    if raw.get("seriesMasterId") or raw.get("type") == "occurrence":
        # Occurrences of a series share one iCalendar UID.
        uid = f"{uid}#{item.get('start_time') or (raw.get('start') or {}).get('dateTime')}"
    organizer = (raw.get("organizer") or {}).get("emailAddress") or {}
    body = (raw.get("body") or {}).get("content") or raw.get("bodyPreview") or ""
    location = (raw.get("location") or {}).get("displayName")
    return CalendarEvent(
        uid=uid,
        calendar_id=item.get("calendar_id"),
        account_id=item.get("account_id"),
        title=(raw.get("subject") or "").strip() or "Meeting",
        start=_utc(item.get("start_time")) or _graph_utc(raw.get("start")),
        end=_utc(item.get("end_time")) or _graph_utc(raw.get("end")),
        timezone=_outlook_zone(raw),
        meeting_link=item.get("meeting_url")
        or (raw.get("onlineMeeting") or {}).get("joinUrl")
        or find_meeting_link(location, body),
        cancelled=bool(item.get("is_deleted")) or bool(raw.get("isCancelled")),
        organizer=(organizer.get("address") or "").lower() or None,
        organizer_name=organizer.get("name"),
        description=body,
        attendees=[
            a["emailAddress"]["address"].lower()
            for a in raw.get("attendees") or []
            if (a.get("emailAddress") or {}).get("address")
            and a.get("type") != "resource"
            and (a.get("status") or {}).get("response") != "declined"
        ],
    )
