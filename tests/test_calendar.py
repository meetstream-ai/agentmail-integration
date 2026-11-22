"""Google Calendar, synced by MeetStream, against mocked Google, MeetStream and AgentMail APIs."""

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlparse

import httpx
import pytest
import respx

from tests.test_flow import GOOGLE_ICS, INBOX, MEETSTREAM, Harness, _transcript_mocks

SEND = f"https://api.agentmail.to/v0/inboxes/{quote(INBOX, safe='')}/messages/send"
LOCAL = "http://localhost"


@pytest.fixture
def h(tmp_path):
    with respx.mock(assert_all_called=False) as router:
        harness = Harness(tmp_path, router, google_client_id="gid", google_client_secret="gsecret")
        harness.sent = []
        router.post(SEND).mock(
            side_effect=lambda r: (
                harness.sent.append(json.loads(r.content))
                or httpx.Response(200, json={"message_id": "n1", "thread_id": "t9"})
            )
        )
        yield harness
        harness.client.__exit__(None, None, None)


def event(**overrides):
    item = {
        "id": "ev-1",
        "calendar_id": "cal-1",
        "ical_uid": "gcal-123@google.com",
        "start_time": "2026-10-20T10:00:00-04:00",
        "end_time": "2026-10-20T10:30:00-04:00",
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "is_deleted": False,
        "raw": {
            "summary": "Design review #notetaker",
            "status": "confirmed",
            "start": {"dateTime": "2026-10-20T10:00:00-04:00", "timeZone": "America/New_York"},
            "end": {"dateTime": "2026-10-20T10:30:00-04:00", "timeZone": "America/New_York"},
            "organizer": {"email": "Priya@acme.com", "displayName": "Priya Shah"},
            "attendees": [
                {"email": "priya@acme.com", "responseStatus": "accepted"},
                {"email": "sam@acme.com", "responseStatus": "needsAction"},
                {"email": "lee@acme.com", "responseStatus": "declined"},
                {"email": "room-4@resource.calendar.google.com", "resource": True},
            ],
        },
    }
    raw = overrides.pop("raw", {})
    item.update(overrides)
    item["raw"].update(raw)
    return item


def calendar(h, *events):
    h.router.get(f"{MEETSTREAM}/calendar/events").respond(json={"results": list(events), "next": None})


def sync(h):
    h.client.portal.call(h.client.app.state.agent.sync_calendar)


def connected(h):
    h.client.app.state.agent.store.add_calendar("cal-1", "priya@acme.com")
    h.clock = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)  # the morning of the meeting


def test_calendar_pages_are_refused_through_the_tunnel(h):
    assert h.client.get(f"{LOCAL}/calendar").status_code == 200
    assert h.client.get(f"{LOCAL}/calendar", headers={"cf-connecting-ip": "1.2.3.4"}).status_code == 404
    assert h.client.get("https://abc.trycloudflare.com/calendar").status_code == 404


def test_connecting_a_calendar(h):
    go = h.client.get(f"{LOCAL}/calendar/connect", follow_redirects=False)
    google = urlparse(go.headers["location"])
    query = parse_qs(google.query)
    assert google.netloc == "accounts.google.com"
    assert query["access_type"] == ["offline"] and "calendar.events.readonly" in query["scope"][0]

    token = h.router.post("https://oauth2.googleapis.com/token").respond(json={"refresh_token": "rt-1"})
    create = h.router.post(f"{MEETSTREAM}/calendar/create_calendar").respond(
        json={"calendar_id": "cal-1", "primary_calendar_id": "priya@acme.com", "user_email": "priya@acme.com"}
    )
    calendar(h)
    back = h.client.get(f"{LOCAL}/calendar/callback?state={query['state'][0]}&code=c-1", follow_redirects=False)

    assert back.headers["location"] == "/calendar?connected=priya%40acme.com"
    assert parse_qs(token.calls.last.request.content.decode())["code"] == ["c-1"]
    assert json.loads(create.calls.last.request.content) == {
        "google_client_id": "gid",
        "google_client_secret": "gsecret",
        "google_refresh_token": "rt-1",
        "replace": True,
    }
    assert h.client.app.state.agent.store.calendars() == {"cal-1": "priya@acme.com", "priya@acme.com": "priya@acme.com"}


def test_callback_needs_the_state_it_issued(h):
    assert h.client.get(f"{LOCAL}/calendar/callback?state=forged&code=c-1").status_code == 400


def test_upcoming_calendar_meeting_gets_a_bot(h):
    connected(h)
    calendar(h, event())
    sync(h)
    [bot] = h.created
    assert bot["meeting_link"] == "https://meet.google.com/abc-defg-hij"
    assert bot["join_at"] == "2026-10-20T14:00:00+00:00"
    meeting = h.meeting("gcal-123@google.com")
    assert meeting.source == "calendar" and meeting.status == "scheduled"
    assert meeting.attendees == ["priya@acme.com", "sam@acme.com"]  # not the declined guest or the room
    assert meeting.host == "Priya Shah (priya@acme.com)"
    sync(h)
    assert len(h.created) == 1  # unchanged on the next pass


def test_a_bot_the_host_turned_away_is_not_sent_again(h):
    connected(h)
    calendar(h, event())
    sync(h)
    token = h.created[0]["callback_url"].rsplit("/", 1)[1]
    h.client.post(f"/webhooks/meetstream/{token}", json={"bot_event": "bot.denied", "bot_id": "bot-1"})
    sync(h)
    assert len(h.created) == 1


def test_a_bot_meetstream_could_not_create_is_tried_again(h):
    connected(h)
    calendar(h, event())
    h.router.post(f"{MEETSTREAM}/bots/create_bot").mock(
        side_effect=[httpx.Response(500), httpx.Response(201, json={"bot_id": "bot-9"})]
    )
    sync(h)
    assert h.meeting("gcal-123@google.com").status == "launch_failed"
    sync(h)
    assert h.meeting("gcal-123@google.com").bot_id == "bot-9"


def test_only_calendars_connected_here_are_synced(h):
    connected(h)
    calendar(h, event(calendar_id="someone-elses-calendar"))
    sync(h)
    assert h.created == []


def test_nothing_happens_before_a_calendar_is_connected(h):
    events = h.router.get(f"{MEETSTREAM}/calendar/events").respond(400, json={"message": "No calendar connected."})
    sync(h)
    assert not events.called


def test_meetings_the_agent_is_invited_to_are_left_to_the_email_path(h):
    connected(h)
    calendar(h, event(raw={"attendees": [{"email": INBOX}, {"email": "sam@acme.com"}]}))
    sync(h)
    assert h.created == []


def test_far_off_past_and_linkless_meetings_are_skipped(h):
    connected(h)
    calendar(
        h,
        event(ical_uid="far", start_time="2026-10-25T10:00:00-04:00", end_time="2026-10-25T11:00:00-04:00"),
        event(ical_uid="past", start_time="2026-10-14T06:00:00+00:00", end_time="2026-10-14T07:00:00+00:00"),
        event(ical_uid="nolink", meeting_url=None),
        event(ical_uid="allday", raw={"start": {"date": "2026-10-20"}}),
    )
    sync(h)
    assert h.created == []


def test_moved_calendar_meeting_is_rescheduled(h):
    connected(h)
    calendar(h, event())
    sync(h)
    patch = h.router.patch(f"{MEETSTREAM}/calendar/scheduled_bots/bot-1").respond(json={})
    calendar(h, event(start_time="2026-10-20T11:00:00-04:00", end_time="2026-10-20T11:30:00-04:00"))
    sync(h)
    assert json.loads(patch.calls.last.request.content)["scheduled_join_time"] == "2026-10-20T15:00:00+00:00"
    assert len(h.created) == 1


def test_cancelled_calendar_meeting_calls_off_its_bot(h):
    connected(h)
    calendar(h, event())
    sync(h)
    cancel = h.router.delete(f"{MEETSTREAM}/calendar/scheduled_bots/bot-1").respond(json={})
    calendar(h, event(is_deleted=True))
    sync(h)
    assert cancel.called
    assert h.meeting("gcal-123@google.com").status == "cancelled"


def test_calendar_meeting_notes_arrive_as_a_new_email(h):
    connected(h)
    calendar(h, event())
    sync(h)
    _transcript_mocks(h)
    token = h.created[0]["callback_url"].rsplit("/", 1)[1]
    h.client.post(f"/webhooks/meetstream/{token}", json={"bot_event": "transcription.processed", "bot_id": "bot-1"})
    [notes] = h.sent
    assert notes["to"] == ["priya@acme.com", "sam@acme.com"]
    assert notes["subject"] == "Notes: Design review #notetaker"
    assert "Time: 10:00 – 10:30 EDT (America/New_York)" in notes["text"]
    assert h.replies == []  # nothing to reply to


def test_untagged_meetings_get_no_bot(h):
    connected(h)
    calendar(h, event(raw={"summary": "GTM sync", "description": "weekly"}))
    sync(h)
    assert h.created == []


def test_tag_can_be_in_the_description(h):
    connected(h)
    calendar(h, event(raw={"summary": "GTM sync", "description": "Agenda... #NoteTaker please"}))
    sync(h)
    assert len(h.created) == 1


def test_removing_the_tag_calls_the_bot_off(h):
    connected(h)
    calendar(h, event())
    sync(h)
    cancel = h.router.delete(f"{MEETSTREAM}/calendar/scheduled_bots/bot-1").respond(json={})
    calendar(h, event(raw={"summary": "Design review"}))
    sync(h)
    assert cancel.called and h.meeting("gcal-123@google.com").status == "cancelled"


def test_a_called_off_meeting_is_not_relaunched(h):
    connected(h)
    calendar(h, event())
    sync(h)
    h.client.app.state.agent.store.set_status("gcal-123@google.com", "cancelled")
    sync(h)
    assert len(h.created) == 1


def test_every_page_of_events_is_read(h):
    connected(h)
    h.router.get(f"{MEETSTREAM}/calendar/events", params={"cursor": "opaque=="}).respond(
        json={"results": [event()], "next": None}
    )
    h.router.get(f"{MEETSTREAM}/calendar/events").respond(json={"results": [], "next": "opaque=="})
    sync(h)
    assert len(h.created) == 1


def test_a_meeting_about_to_start_is_not_mistaken_for_a_moved_one(h):
    connected(h)
    calendar(h, event())
    sync(h)
    h.clock = datetime(2026, 10, 20, 13, 59, 30, tzinfo=timezone.utc)  # 30 seconds before it starts
    cancel = h.router.delete(f"{MEETSTREAM}/calendar/scheduled_bots/bot-1").respond(json={})
    sync(h)
    assert not cancel.called and len(h.created) == 1


def test_calendar_problems_go_to_the_calendars_owner(h):
    connected(h)  # cal-1 belongs to priya@acme.com
    calendar(h, event(raw={"organizer": {"email": "outsider@vendor.io", "displayName": "Vendor"}}))
    sync(h)
    assert h.meeting("gcal-123@google.com").requester == "priya@acme.com"


def test_hosted_calendar_pages_need_the_admin_token(tmp_path):
    with respx.mock(assert_all_called=False) as router:
        hosted = Harness(tmp_path, router, admin_token="s3cret-token")
        try:
            site, proxied = "https://notetaker.example.com", {"x-forwarded-for": "198.51.100.7"}
            assert hosted.client.get(f"{site}/calendar", headers=proxied).status_code == 404
            assert hosted.client.get(f"{site}/calendar/login", headers=proxied).status_code == 200
            wrong = hosted.client.post(f"{site}/calendar/login", content=b"token=nope", headers=proxied)
            assert wrong.status_code == 403
            signed_in = hosted.client.post(
                f"{site}/calendar/login", content=b"token=s3cret-token", headers=proxied, follow_redirects=False
            )
            assert signed_in.status_code == 303 and "notetaker_admin" in signed_in.headers["set-cookie"]
            assert hosted.client.get(f"{site}/calendar", headers=proxied).status_code == 200  # the cookie opens it
        finally:
            hosted.client.__exit__(None, None, None)


def test_without_an_admin_token_there_is_no_login_page(h):
    assert h.client.get(f"{LOCAL}/calendar/login").status_code == 404


def test_a_remote_client_cannot_pose_as_local(tmp_path):
    from fastapi.testclient import TestClient

    with respx.mock(assert_all_called=False) as router:
        h = Harness(tmp_path, router)
        try:
            remote = TestClient(h.client.app, client=("203.0.113.9", 50000))
            # Host: localhost is just a header the client chose.
            assert remote.get("http://localhost/calendar").status_code == 404
        finally:
            h.client.__exit__(None, None, None)


def test_a_sign_in_attempt_expires(h, monkeypatch):
    import app.main

    go = h.client.get(f"{LOCAL}/calendar/connect", follow_redirects=False)
    state = parse_qs(urlparse(go.headers["location"]).query)["state"][0]
    monkeypatch.setattr(app.main, "OAUTH_STATE_TTL", -1)
    assert h.client.get(f"{LOCAL}/calendar/callback?state={state}&code=c-1").status_code == 400


def test_sign_in_errors_keep_their_punctuation(h):
    go = h.client.get(f"{LOCAL}/calendar/connect", follow_redirects=False)
    state = parse_qs(urlparse(go.headers["location"]).query)["state"][0]
    h.router.post("https://oauth2.googleapis.com/token").respond(400, json={"error": "invalid_grant"})
    back = h.client.get(f"{LOCAL}/calendar/callback?state={state}&code=c-1", follow_redirects=False)
    message = parse_qs(urlparse(back.headers["location"]).query)["error"][0]
    assert "400 Bad Request" in message


def test_outlook_style_thin_connect_response_falls_back_to_the_connections_list(h):
    """MeetStream's API reference shows an empty connect response; its connections list has the ids."""
    go = h.client.get(f"{LOCAL}/calendar/connect", follow_redirects=False)
    state = parse_qs(urlparse(go.headers["location"]).query)["state"][0]
    h.router.post("https://oauth2.googleapis.com/token").respond(json={"refresh_token": "rt-1"})
    h.router.post(f"{MEETSTREAM}/calendar/create_calendar").respond(json={})
    h.router.get(f"{MEETSTREAM}/calendar/connections").respond(
        json={
            "google": [
                {
                    "account_id": "priya@acme.com",
                    "email": "priya@acme.com",
                    "primary_calendar_id": "cal-1",
                    "status": "ACTIVE",
                }
            ],
            "outlook": [{"account_id": "someone@else.com", "email": "someone@else.com"}],
        }
    )
    calendar(h)
    back = h.client.get(f"{LOCAL}/calendar/callback?state={state}&code=c-1", follow_redirects=False)
    assert back.headers["location"] == "/calendar?connected=priya%40acme.com"
    assert set(h.client.app.state.agent.store.calendars()) == {"cal-1", "priya@acme.com"}


def test_an_unidentifiable_connection_is_reported_not_guessed(h):
    go = h.client.get(f"{LOCAL}/calendar/connect", follow_redirects=False)
    state = parse_qs(urlparse(go.headers["location"]).query)["state"][0]
    h.router.post("https://oauth2.googleapis.com/token").respond(json={"refresh_token": "rt-1"})
    h.router.post(f"{MEETSTREAM}/calendar/create_calendar").respond(json={})
    h.router.get(f"{MEETSTREAM}/calendar/connections").respond(
        json={"google": [{"account_id": "a@x.com"}, {"account_id": "b@x.com"}], "outlook": []}
    )
    back = h.client.get(f"{LOCAL}/calendar/callback?state={state}&code=c-1", follow_redirects=False)
    assert "error=" in back.headers["location"]
    assert h.client.app.state.agent.store.calendars() == {}


def test_events_are_matched_by_account_when_the_calendar_id_differs(h):
    connected(h)  # stored: cal-1 -> priya@acme.com
    calendar(h, event(calendar_id="AAMkADBjZjY...", account_id="priya@acme.com"))
    sync(h)
    assert len(h.created) == 1


def test_notes_interrupted_mid_write_are_picked_up_again(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    store = h.client.app.state.agent.store
    store.set_status("7kq2m1v0abc@google.com", "writing_notes")  # the server stopped mid-write
    assert not store.claim_for_notes("7kq2m1v0abc@google.com")  # still fresh: someone may be writing
    h.clock = datetime(2026, 10, 14, 12, 11, tzinfo=timezone.utc)  # 11 minutes later
    assert [m.uid for m in store.unfinished()] == ["7kq2m1v0abc@google.com"]
    assert store.claim_for_notes("7kq2m1v0abc@google.com")
