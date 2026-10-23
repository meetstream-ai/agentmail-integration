"""Turn an invite email into the one thing the agent needs: which call, and when."""

import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from email import message_from_bytes, policy

import recurring_ical_events
from icalendar import Calendar

# Zoom, Google Meet and Teams join links. Stops at whitespace and at the
# characters that usually wrap a link in an email body (quotes, brackets).
MEETING_LINK = re.compile(
    r"https://(?:"
    r"[\w.-]*zoom\.us/(?:j|my|w)/[^\s\"'<>)\]]+"
    r"|meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}"
    r"|teams\.microsoft\.com/l/meetup-join/[^\s\"'<>)\]]+"
    r"|teams\.live\.com/meet/[^\s\"'<>)\]]+"
    r")",
    re.IGNORECASE,
)

CALENDAR_TYPES = ("text/calendar", "application/ics")


def find_meeting_link(*texts: str | None) -> str | None:
    for text in texts:
        if text:
            match = MEETING_LINK.search(text)
            if match:
                return match.group(0).rstrip(".,;")
    return None


@dataclass
class Invite:
    uid: str
    sequence: int = 0
    cancelled: bool = False
    recurring: bool = False
    all_day: bool = False
    title: str = "Meeting"
    start: datetime | None = None
    end: datetime | None = None
    meeting_link: str | None = None
    organizer: str | None = None
    organizer_name: str | None = None
    timezone: str | None = None  # the organiser's zone, e.g. "Asia/Kolkata"
    attendees: list[str] = field(default_factory=list)
    # Set when the invite changes one meeting of a series: that meeting's
    # original start, which is how calendars name it.
    recurrence_id: datetime | None = None


def _address(value) -> str | None:
    if value is None:
        return None
    text = str(value)
    if text.lower().startswith("mailto:"):
        text = text[7:]
    return text.strip().lower() or None


PLATFORMS = {
    "zoom.us": "Zoom",
    "meet.google.com": "Google Meet",
    "teams.microsoft.com": "Microsoft Teams",
    "teams.live.com": "Microsoft Teams",
}


def platform_of(link: str | None) -> str | None:
    for host, name in PLATFORMS.items():
        if link and host in link:
            return name
    return None


def _zone(value) -> str | None:
    tz = getattr(value, "tzinfo", None)
    return getattr(tz, "key", None) or (str(tz) if tz else None)


def _utc(value) -> datetime | None:
    # All-day events (a bare date) have no time to join at.
    if value is None or (isinstance(value, date) and not isinstance(value, datetime)):
        return None
    if value.tzinfo is None:
        # A "floating" time has no zone; reading it as UTC is the RFC's
        # least-bad option and what most calendars send anyway.
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _invite(event, method: str) -> Invite:
    attendees = event.get("ATTENDEE", [])
    if not isinstance(attendees, list):
        attendees = [attendees]
    return Invite(
        uid=str(event.get("UID", "")).strip(),
        sequence=int(event.get("SEQUENCE", 0)),
        cancelled=method == "CANCEL" or str(event.get("STATUS", "")).upper() == "CANCELLED",
        recurring="RRULE" in event or "RDATE" in event,
        all_day="DTSTART" in event and not isinstance(event.decoded("DTSTART"), datetime),
        title=str(event.get("SUMMARY", "")).strip() or "Meeting",
        start=_utc(event.decoded("DTSTART")) if "DTSTART" in event else None,
        end=_utc(event.decoded("DTEND")) if "DTEND" in event else None,
        meeting_link=find_meeting_link(
            str(event.get("X-GOOGLE-CONFERENCE", "")),
            str(event.get("X-MICROSOFT-SKYPETEAMSMEETINGURL", "")),
            str(event.get("LOCATION", "")),
            str(event.get("DESCRIPTION", "")),
        ),
        organizer=_address(event.get("ORGANIZER")),
        organizer_name=str(event["ORGANIZER"].params.get("CN", "")).strip() or None if "ORGANIZER" in event else None,
        timezone=_zone(event.decoded("DTSTART")) if "DTSTART" in event else None,
        attendees=[a for a in (_address(x) for x in attendees) if a],
        recurrence_id=_utc(event.decoded("RECURRENCE-ID")) if "RECURRENCE-ID" in event else None,
    )


def parse_ics(data: bytes) -> Invite | None:
    """The invite's meeting. For a series, that's the series itself (its first
    meeting's details), unless the invite only changes one of its meetings."""
    calendar = Calendar.from_ical(data)
    method = str(calendar.get("METHOD", "")).upper()
    events = [e for e in calendar.walk("VEVENT") if str(e.get("UID", "")).strip()]
    series = [e for e in events if "RECURRENCE-ID" not in e]
    return _invite((series or events)[0], method) if events else None


def occurrence_uid(uid: str, recurrence_id: datetime) -> str:
    """One meeting of a series; every meeting in it shares the series' UID."""
    return f"{uid}#{recurrence_id.isoformat()}"


def occurrences(data: bytes, after: datetime, horizon: timedelta = timedelta(days=366)) -> list[Invite]:
    """The meetings of a series that end after `after`, in order, with changes
    to single meetings applied. Cancelled meetings are left out."""
    calendar = Calendar.from_ical(data)
    method = str(calendar.get("METHOD", "")).upper()
    found = []
    # Start a day early: a meeting that began before `after` may still be on.
    for event in recurring_ical_events.of(calendar).between(after - timedelta(days=1), after + horizon):
        meeting = _invite(event, method)
        if meeting.cancelled or meeting.all_day or not meeting.start or (meeting.end or meeting.start) <= after:
            continue
        found.append(replace(meeting, recurrence_id=meeting.recurrence_id or meeting.start))
    return sorted(found, key=lambda m: m.start or after)


def next_occurrence(data: bytes, after: datetime) -> Invite | None:
    """The series' meeting that is on now or comes next, if any is left."""
    found = occurrences(data, after)
    return found[0] if found else None


def merge_change(series: bytes, change: bytes) -> bytes:
    """Apply an invite that changes or cancels single meetings of a series
    (events with a RECURRENCE-ID) to the stored series."""
    calendar = Calendar.from_ical(series)
    update = Calendar.from_ical(change)
    cancelled = str(update.get("METHOD", "")).upper() == "CANCEL"
    for event in update.walk("VEVENT"):
        if "RECURRENCE-ID" not in event:
            continue
        rid = _utc(event.decoded("RECURRENCE-ID"))
        calendar.subcomponents = [
            c
            for c in calendar.subcomponents
            if not (c.name == "VEVENT" and "RECURRENCE-ID" in c and _utc(c.decoded("RECURRENCE-ID")) == rid)
        ]
        if cancelled:
            event["STATUS"] = "CANCELLED"
        calendar.add_component(event)
    return calendar.to_ical()


def ics_from_raw_email(raw: bytes) -> bytes | None:
    """The calendar part of a raw email. Google and Outlook send it inline as
    text/calendar alongside the HTML body, not always as a named attachment."""
    message = message_from_bytes(raw, policy=policy.default)
    for part in message.walk():
        if part.get_content_type() in CALENDAR_TYPES:
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes) and payload:
                return payload
    return None
