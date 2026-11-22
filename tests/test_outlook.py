"""Outlook calendars: Microsoft sign-in, and Microsoft Graph events as MeetStream syncs them."""

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from app.calendar import parse_event
from tests.test_calendar import LOCAL, SEND, calendar, connected, sync
from tests.test_flow import MEETSTREAM, Harness, _transcript_mocks


@pytest.fixture
def h(tmp_path):
    with respx.mock(assert_all_called=False) as router:
        harness = Harness(tmp_path, router, microsoft_client_id="mid", microsoft_client_secret="msecret")
        harness.sent = []
        router.post(SEND).mock(
            side_effect=lambda r: (
                harness.sent.append(json.loads(r.content))
                or httpx.Response(200, json={"message_id": "n1", "thread_id": "t9"})
            )
        )
        yield harness
        harness.client.__exit__(None, None, None)


def outlook_event(**raw_overrides):
    raw = {
        "id": "AAMkAD...",
        "iCalUId": "040000008200E00074C5B7101A82E008",
        "subject": "Vendor review #notetaker",
        "isAllDay": False,
        "isCancelled": False,
        "originalStartTimeZone": "India Standard Time",
        "start": {"dateTime": "2026-10-20T14:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-10-20T14:30:00.0000000", "timeZone": "UTC"},
        "location": {"displayName": "Microsoft Teams Meeting"},
        "bodyPreview": "Quarterly check-in",
        "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0"},
        "organizer": {"emailAddress": {"name": "Dana Kim", "address": "Dana@contoso.com"}},
        "attendees": [
            {"type": "required", "status": {"response": "accepted"}, "emailAddress": {"address": "dana@contoso.com"}},
            {"type": "optional", "status": {"response": "none"}, "emailAddress": {"address": "sam@contoso.com"}},
            {"type": "required", "status": {"response": "declined"}, "emailAddress": {"address": "lee@contoso.com"}},
            {"type": "resource", "status": {"response": "accepted"}, "emailAddress": {"address": "room@contoso.com"}},
        ],
    }
    raw.update(raw_overrides)
    return {
        "id": "ev-9",
        "calendar_id": "cal-1",
        "platform": "outlook_calendar",
        "ical_uid": raw["iCalUId"],
        "raw": raw,
    }


def test_graph_event_is_read():
    event = parse_event(outlook_event())
    assert event.title == "Vendor review #notetaker"
    assert event.start.isoformat() == "2026-10-20T14:00:00+00:00"
    assert event.timezone == "Asia/Kolkata"  # from Windows' "India Standard Time"
    assert event.meeting_link.startswith("https://teams.microsoft.com/l/meetup-join/")
    assert event.organizer == "dana@contoso.com" and event.organizer_name == "Dana Kim"
    assert event.attendees == ["dana@contoso.com", "sam@contoso.com"]  # not the decliner or the room


def test_graph_all_day_cancelled_and_series_events():
    assert parse_event(outlook_event(isAllDay=True)) is None
    assert parse_event(outlook_event(isCancelled=True)).cancelled
    one, two = (
        parse_event(outlook_event(seriesMasterId="m1", type="occurrence", start={"dateTime": day, "timeZone": "UTC"}))
        for day in ("2026-10-20T14:00:00.0000000", "2026-10-27T14:00:00.0000000")
    )
    assert one.uid != two.uid  # each occurrence is its own meeting


def test_teams_link_found_in_the_body_when_not_an_online_meeting():
    event = parse_event(outlook_event(onlineMeeting=None, body={"content": "Join: https://acme.zoom.us/j/123456789"}))
    assert event.meeting_link == "https://acme.zoom.us/j/123456789"


def test_connecting_an_outlook_calendar(h):
    go = h.client.get(f"{LOCAL}/calendar/outlook/connect", follow_redirects=False)
    login = urlparse(go.headers["location"])
    query = parse_qs(login.query)
    assert login.netloc == "login.microsoftonline.com" and login.path == "/common/oauth2/v2.0/authorize"
    assert "offline_access" in query["scope"][0] and "Calendars.Read" in query["scope"][0]

    token = h.router.post("https://login.microsoftonline.com/common/oauth2/v2.0/token").respond(
        json={"refresh_token": "mrt"}
    )
    create = h.router.post(f"{MEETSTREAM}/calendar/create_outlook_calendar").respond(
        json={
            "provider": "outlook",
            "account_id": "dana@contoso.com",
            "user_email": "dana@contoso.com",
            "primary_calendar_id": "cal-1",
        }
    )
    calendar(h)
    back = h.client.get(f"{LOCAL}/calendar/outlook/callback?state={query['state'][0]}&code=c-9", follow_redirects=False)

    assert back.headers["location"] == "/calendar?connected=dana%40contoso.com"
    assert parse_qs(token.calls.last.request.content.decode())["code"] == ["c-9"]
    assert json.loads(create.calls.last.request.content) == {
        "microsoft_client_id": "mid",
        "microsoft_client_secret": "msecret",
        "microsoft_refresh_token": "mrt",
        "replace": True,
    }
    assert "cal-1" in h.client.app.state.agent.store.calendars()


def test_microsoft_sign_in_errors_are_shown(h):
    go = h.client.get(f"{LOCAL}/calendar/outlook/connect", follow_redirects=False)
    state = parse_qs(urlparse(go.headers["location"]).query)["state"][0]
    back = h.client.get(
        f"{LOCAL}/calendar/outlook/callback?state={state}&error=access_denied&error_description=AADSTS65004+declined",
        follow_redirects=False,
    )
    assert "AADSTS65004" in back.headers["location"]


def test_outlook_page_is_refused_through_the_tunnel(h):
    assert h.client.get("https://abc.trycloudflare.com/calendar/outlook/connect").status_code == 404


def test_tagged_outlook_meeting_gets_a_bot_and_notes(h):
    connected(h)
    calendar(h, outlook_event())
    sync(h)
    [bot] = h.created
    assert (
        bot["meeting_link"].startswith("https://teams.microsoft.com/") and bot["join_at"] == "2026-10-20T14:00:00+00:00"
    )

    _transcript_mocks(h)
    token = bot["callback_url"].rsplit("/", 1)[1]
    h.client.post(f"/webhooks/meetstream/{token}", json={"bot_event": "transcription.processed", "bot_id": "bot-1"})
    [notes] = h.sent
    # the attendees, plus the calendar's owner who asked for the notetaker
    assert notes["to"] == ["dana@contoso.com", "priya@acme.com", "sam@contoso.com"]
    assert "Time: 19:30 – 20:00 IST (Asia/Kolkata)" in notes["text"]
    assert "Platform: Microsoft Teams" in notes["text"]
