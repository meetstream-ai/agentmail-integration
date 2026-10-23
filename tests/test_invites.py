from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from app.invites import find_meeting_link, ics_from_raw_email, parse_ics

FIXTURES = Path(__file__).parent / "fixtures"


def test_google_invite():
    invite = parse_ics((FIXTURES / "google_invite.ics").read_bytes())
    assert invite.uid == "7kq2m1v0abc@google.com"
    assert invite.title == "Roadmap sync"
    # 10:00 New York (EDT) is 14:00 UTC
    assert invite.start == datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)
    assert invite.end == datetime(2026, 10, 20, 14, 30, tzinfo=timezone.utc)
    assert invite.meeting_link == "https://meet.google.com/abc-defg-hij"
    assert invite.organizer == "priya@acme.com"
    assert invite.attendees == ["priya@acme.com", "sam@acme.com", "notetaker@agentmail.to"]
    assert not invite.cancelled and not invite.recurring


def test_outlook_teams_link_comes_from_description():
    invite = parse_ics((FIXTURES / "outlook_teams.ics").read_bytes())
    assert invite.sequence == 2
    assert invite.meeting_link.startswith("https://teams.microsoft.com/l/meetup-join/19%3ameeting_ZmFrZQ")
    assert invite.organizer == "dana@contoso.com"


def test_cancellation():
    invite = parse_ics((FIXTURES / "zoom_cancel.ics").read_bytes())
    assert invite.cancelled
    assert invite.meeting_link == "https://us02web.zoom.us/j/81234567890?pwd=abc123"


def test_recurring_is_flagged():
    ics = (FIXTURES / "google_invite.ics").read_text().replace("SEQUENCE:0", "RRULE:FREQ=WEEKLY;BYDAY=TU\nSEQUENCE:0")
    assert parse_ics(ics.encode()).recurring


def test_all_day_event_has_no_start_time():
    ics = (
        "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:x\nSUMMARY:Offsite\n"
        "DTSTART;VALUE=DATE:20261020\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    assert parse_ics(ics.encode()).start is None


def test_link_finder_trims_email_punctuation():
    assert find_meeting_link('Join at <a href="https://meet.google.com/abc-defg-hij">here</a>.') == (
        "https://meet.google.com/abc-defg-hij"
    )
    assert find_meeting_link("Zoom: https://acme.zoom.us/j/123456789.") == "https://acme.zoom.us/j/123456789"
    assert find_meeting_link("no link here", None) is None


def test_calendar_part_is_found_in_raw_email():
    msg = EmailMessage()
    msg["Subject"] = "Invitation: Roadmap sync"
    msg.set_content("You have been invited.")
    msg.add_alternative((FIXTURES / "google_invite.ics").read_text(), subtype="calendar", params={"method": "REQUEST"})
    ics = ics_from_raw_email(msg.as_bytes())
    assert parse_ics(ics).uid == "7kq2m1v0abc@google.com"


def test_raw_email_without_calendar():
    msg = EmailMessage()
    msg.set_content("just text")
    assert ics_from_raw_email(msg.as_bytes()) is None
