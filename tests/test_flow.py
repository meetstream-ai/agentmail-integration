"""The whole loop against mocked AgentMail, MeetStream and Gemini APIs."""

import base64
import json
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from svix.webhooks import Webhook

from app.config import Settings
from app.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"
GOOGLE_ICS = (FIXTURES / "google_invite.ics").read_text()
INBOX = "notetaker@agentmail.to"
SECRET = "whsec_" + base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

AGENTMAIL = f"https://api.agentmail.to/v0/inboxes/{quote(INBOX, safe='')}/messages"
MEETSTREAM = "https://api.meetstream.ai/api/v1"
GEMINI = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
GROK = "https://api.x.ai/v1/chat/completions"


class Harness:
    def __init__(self, tmp_path, router: respx.MockRouter, **overrides):
        self.router = router
        self.replies: list[dict] = []
        self.created: list[dict] = []
        self.event_count = 0
        settings = Settings(
            _env_file=None,
            agentmail_api_key="am_test",
            agentmail_inbox_id=INBOX,
            agentmail_webhook_secret=SECRET,
            meetstream_api_key="ms_test",
            gemini_api_key="gm_test",
            public_base_url="https://agent.example",
            allowed_senders="@acme.com",
            poll_seconds=0,
            database_path=str(tmp_path / "test.db"),
            **overrides,
        )
        router.post(f"{MEETSTREAM}/bots/create_bot").mock(side_effect=self._create_bot)
        self.clock = NOW
        self.client = TestClient(
            create_app(settings, httpx.AsyncClient(), now=lambda: self.clock), client=("127.0.0.1", 50000)
        )
        self.client.__enter__()

    def _create_bot(self, request):
        self.created.append(json.loads(request.content))
        return httpx.Response(201, json={"bot_id": f"bot-{len(self.created)}", "status": "Scheduled"})

    def email(
        self,
        message_id,
        sender="Priya Shah <priya@acme.com>",
        ics=None,
        text="",
        raw=None,
        deliver=True,
        subject="Invitation: Roadmap sync",
    ):
        """Register an email in the mocked inbox and deliver its webhook."""
        url = f"{AGENTMAIL}/{quote(message_id, safe='')}"
        attachments = []
        if ics is not None:
            attachments.append(
                {"attachment_id": "att1", "filename": "invite.ics", "content_type": "text/calendar; method=REQUEST"}
            )
            self.router.get(f"{url}/attachments/att1").respond(
                json={"download_url": f"https://cdn.example/{message_id}.ics"}
            )
            self.router.get(f"https://cdn.example/{message_id}.ics").respond(content=ics.encode())
        else:
            if raw is None:
                plain = EmailMessage()
                plain.set_content(text or "hello")
                raw = plain.as_bytes()
            self.router.get(f"{url}/raw").respond(json={"download_url": f"https://cdn.example/{message_id}.eml"})
            self.router.get(f"https://cdn.example/{message_id}.eml").respond(content=raw)
        self.router.get(url).respond(
            json={
                "inbox_id": INBOX,
                "message_id": message_id,
                "thread_id": "thread-1",
                "from": sender,
                "to": [INBOX, "sam@acme.com"],
                "subject": subject,
                "text": text,
                "attachments": attachments,
            }
        )
        self.router.post(f"{url}/reply").mock(side_effect=self._reply)
        if not deliver:
            return None
        return self.deliver(
            {"event_type": "message.received", "message": {"inbox_id": INBOX, "message_id": message_id}}
        )

    def _reply(self, request):
        self.replies.append(json.loads(request.content))
        return httpx.Response(200, json={"message_id": "out", "thread_id": "thread-1"})

    def deliver(self, payload, event_id=None, signature_secret=SECRET):
        self.event_count += 1
        event_id = event_id or f"evt-{self.event_count}"
        body = json.dumps({"event_id": event_id, **payload})
        msg_id = f"msg_{event_id}"
        signature = Webhook(signature_secret).sign(msg_id, NOW_REAL(), body)
        headers = {"svix-id": msg_id, "svix-timestamp": str(int(NOW_REAL().timestamp())), "svix-signature": signature}
        return self.client.post("/webhooks/agentmail", content=body, headers=headers)

    def meeting(self, uid="7kq2m1v0abc@google.com"):
        return self.client.app.state.agent.store.get(uid)


def NOW_REAL():
    # Svix rejects signatures more than 5 minutes from the wall clock.
    return datetime.now(timezone.utc)


@pytest.fixture
def h(tmp_path):
    with respx.mock(assert_all_called=False) as router:
        harness = Harness(tmp_path, router)
        yield harness
        harness.client.__exit__(None, None, None)


def test_invite_schedules_bot_and_confirms(h):
    assert h.email("<m1@acme.com>", ics=GOOGLE_ICS).status_code == 200

    [bot] = h.created
    assert bot["meeting_link"] == "https://meet.google.com/abc-defg-hij"
    assert bot["join_at"] == "2026-10-20T14:00:00+00:00"
    assert bot["callback_url"].startswith("https://agent.example/webhooks/meetstream/")
    [reply] = h.replies
    assert '"Roadmap sync" at Tue 20 Oct 2026, 14:00 UTC' in reply["text"]
    assert "to" not in reply  # the confirmation goes to the sender only
    meeting = h.meeting()
    assert meeting.status == "scheduled"
    assert meeting.attendees == ["priya@acme.com", "sam@acme.com"]


def test_duplicate_delivery_is_ignored(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    h.deliver(
        {"event_type": "message.received", "message": {"inbox_id": INBOX, "message_id": "<m1@acme.com>"}},
        event_id="evt-1",
    )
    assert len(h.created) == 1


def test_bad_signature_is_rejected(h):
    other = "whsec_" + base64.b64encode(b"x" * 32).decode()
    resp = h.deliver(
        {"event_type": "message.received", "message": {"inbox_id": INBOX, "message_id": "<m1@acme.com>"}},
        signature_secret=other,
    )
    assert resp.status_code == 400
    assert h.created == []


def test_sender_outside_allowlist_gets_nothing(h):
    h.email("<m1@evil.com>", sender="mallory@evil.com", ics=GOOGLE_ICS)
    assert h.created == [] and h.replies == []


def test_updated_invite_reschedules(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    reschedule = h.router.patch(f"{MEETSTREAM}/calendar/scheduled_bots/bot-1").respond(json={})
    moved = GOOGLE_ICS.replace("SEQUENCE:0", "SEQUENCE:1").replace("T100000", "T110000").replace("T103000", "T113000")
    h.email("<m2@acme.com>", ics=moved)

    assert json.loads(reschedule.calls.last.request.content)["scheduled_join_time"] == "2026-10-20T15:00:00+00:00"
    assert len(h.created) == 1
    assert "Updated" in h.replies[-1]["text"]
    assert h.meeting().start_at == "2026-10-20T15:00:00+00:00"


def test_stale_update_is_ignored(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS.replace("SEQUENCE:0", "SEQUENCE:3"))
    h.email("<m2@acme.com>", ics=GOOGLE_ICS.replace("SEQUENCE:0", "SEQUENCE:2").replace("T100000", "T110000"))
    assert len(h.replies) == 1


def test_cancellation_cancels_the_bot(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    cancel = h.router.delete(f"{MEETSTREAM}/calendar/scheduled_bots/bot-1").respond(json={})
    cancelled = GOOGLE_ICS.replace("METHOD:REQUEST", "METHOD:CANCEL").replace("SEQUENCE:0", "SEQUENCE:1")
    h.email("<m2@acme.com>", ics=cancelled)

    assert cancel.called
    assert h.meeting().status == "cancelled"
    assert "cancelled" in h.replies[-1]["text"]


def test_bare_link_joins_now(h):
    h.email("<m1@acme.com>", text="Can you join? https://acme.zoom.us/j/123456789")
    [bot] = h.created
    assert bot["meeting_link"] == "https://acme.zoom.us/j/123456789"
    assert "join_at" not in bot
    assert h.replies[-1]["text"].startswith("Joining")
    # No invite to read the time from: the notes header shows when the bot joined
    assert h.meeting("adhoc:<m1@acme.com>").starts_at == NOW.isoformat()


def test_inline_calendar_part_in_raw_email(h):
    raw = EmailMessage()
    raw.set_content("Invitation")
    raw.add_alternative(GOOGLE_ICS, subtype="calendar", params={"method": "REQUEST"})
    h.email("<m1@acme.com>", raw=raw.as_bytes())
    assert h.created[0]["join_at"] == "2026-10-20T14:00:00+00:00"


def test_ordinary_email_is_left_alone(h):
    h.email("<m1@acme.com>", text="Thanks for the notes!")
    assert h.created == [] and h.replies == []


def _transcript_mocks(h):
    h.router.get(f"{MEETSTREAM}/bots/bot-1/detail").respond(json={"bot_details": {"transcript_id": "tr-1"}})
    h.router.get(f"{MEETSTREAM}/transcript/tr-1/get_transcript").respond(
        json=[
            # the current response shape...
            {"speaker": "Priya", "transcript": "Ship Friday.", "words": [{"word": "ship", "punctuated_word": "Ship"}]},
            # ...and the older one
            {
                "participant": {"name": "Sam"},
                "words": [
                    {"text": "I'll"},
                    {"text": "write"},
                    {"text": "the"},
                    {"text": "release"},
                    {"text": "notes."},
                ],
            },
        ]
    )
    notes = {
        "summary": "Agreed to ship on Friday.",
        "decisions": ["Ship Friday"],
        "action_items": [{"task": "Write the release notes", "owner": "Sam", "due": "Thursday"}],
    }
    return h.router.post(GEMINI).respond(json={"candidates": [{"content": {"parts": [{"text": json.dumps(notes)}]}}]})


def _token(h):
    return h.created[0]["callback_url"].rsplit("/", 1)[1]


def test_transcript_becomes_notes_for_every_attendee(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    gemini = _transcript_mocks(h)
    token = _token(h)

    h.client.post(f"/webhooks/meetstream/{token}", json={"bot_event": "bot.inmeeting", "bot_id": "bot-1"})
    assert h.meeting().status == "in_call"
    resp = h.client.post(
        f"/webhooks/meetstream/{token}", json={"bot_event": "transcription.processed", "bot_id": "bot-1"}
    )
    assert resp.status_code == 200

    prompt = json.loads(gemini.calls.last.request.content)["contents"][0]["parts"][0]["text"]
    assert "Priya: Ship Friday.\nSam: I'll write the release notes." in prompt
    notes = h.replies[-1]
    assert notes["to"] == ["priya@acme.com", "sam@acme.com"]
    assert "- [ ] Write the release notes (Sam, due Thursday)" in notes["text"]
    assert "<li>Write the release notes (Sam, due Thursday)</li>" in notes["html"]
    assert h.meeting().status == "notes_sent"

    # A repeated delivery does not send the notes twice.
    h.client.post(f"/webhooks/meetstream/{token}", json={"bot_event": "transcription.processed", "bot_id": "bot-1"})
    assert len(h.replies) == 2


def test_notes_failure_can_be_retried(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    gemini = _transcript_mocks(h)
    gemini.side_effect = [httpx.Response(503), gemini.return_value]
    token = _token(h)

    h.client.post(f"/webhooks/meetstream/{token}", json={"bot_event": "transcription.processed", "bot_id": "bot-1"})
    assert h.meeting().status == "notes_failed"
    h.client.post(f"/webhooks/meetstream/{token}", json={"bot_event": "transcription.processed", "bot_id": "bot-1"})
    assert h.meeting().status == "notes_sent"


def test_failed_join_tells_the_requester(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    h.client.post(
        f"/webhooks/meetstream/{_token(h)}",
        json={"bot_event": "bot.denied", "bot_id": "bot-1", "message": "The host denied entry"},
    )
    assert h.meeting().status == "failed"
    assert h.replies[-1] == {
        "text": 'I couldn\'t get into "Roadmap sync": The host denied entry',
        "to": ["priya@acme.com"],
    }


def test_unknown_callback_token_is_404(h):
    assert h.client.post("/webhooks/meetstream/nope", json={"bot_event": "bot.done"}).status_code == 404


def test_event_for_another_bot_is_ignored(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    h.client.post(f"/webhooks/meetstream/{_token(h)}", json={"bot_event": "bot.denied", "bot_id": "someone-else"})
    assert h.meeting().status == "scheduled"


def test_grok_writes_the_notes(tmp_path):
    with respx.mock(assert_all_called=False) as router:
        h = Harness(tmp_path, router, llm_provider="grok", xai_api_key="xai_test", xai_model="grok-test")
        try:
            h.email("<m1@acme.com>", ics=GOOGLE_ICS)
            _transcript_mocks(h)
            notes = {
                "summary": "Agreed to ship on Friday.",
                "decisions": [],
                "action_items": [{"task": "Write the release notes"}],
            }
            grok = router.post(GROK).respond(json={"choices": [{"message": {"content": json.dumps(notes)}}]})
            h.client.post(
                f"/webhooks/meetstream/{_token(h)}", json={"bot_event": "transcription.processed", "bot_id": "bot-1"}
            )

            request = grok.calls.last.request
            assert request.headers["authorization"] == "Bearer xai_test"
            body = json.loads(request.content)
            assert body["model"] == "grok-test"
            assert body["response_format"]["json_schema"]["schema"]["properties"]["summary"] == {"type": "string"}
            assert "- [ ] Write the release notes" in h.replies[-1]["text"]
            assert h.meeting().status == "notes_sent"
        finally:
            h.client.__exit__(None, None, None)


def test_callback_token_is_stored_before_the_bot_exists(h):
    # MeetStream posts the first status event while create_bot is still returning.
    seen = []
    original = h._create_bot

    def create_and_check(request):
        token = json.loads(request.content)["callback_url"].rsplit("/", 1)[1]
        seen.append(h.client.app.state.agent.store.by_token(token))
        return original(request)

    h.router.post(f"{MEETSTREAM}/bots/create_bot").mock(side_effect=create_and_check)
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    assert seen[0] is not None and seen[0].status == "creating"
    assert h.meeting().bot_id == "bot-1"


def test_bot_leaves_an_empty_call_after_a_minute(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    assert h.created[0]["automatic_leave"] == {"everyone_left_timeout": 60}


def test_bot_records_audio_only(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    assert h.created[0]["video_required"] is False


def test_invite_moved_to_a_time_already_started_joins_now(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    cancel = h.router.delete(f"{MEETSTREAM}/calendar/scheduled_bots/bot-1").respond(json={})
    started = (
        GOOGLE_ICS.replace("SEQUENCE:0", "SEQUENCE:1")
        .replace("20261020T100000", "20261014T075500")
        .replace("20261020T103000", "20261014T083000")
    )
    h.email("<m2@acme.com>", ics=started)

    assert cancel.called
    assert "join_at" not in h.created[1]
    assert h.replies[-1]["text"] == 'Updated: I\'ll join "Roadmap sync" now.'
    assert h.meeting().bot_id == "bot-2" and h.meeting().status == "joining"


def test_a_failed_launch_is_retried_without_a_new_email(h):
    h.router.post(f"{MEETSTREAM}/bots/create_bot").mock(
        side_effect=[httpx.Response(500), httpx.Response(201, json={"bot_id": "bot-9"})]
    )
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    assert h.meeting().bot_id == "bot-9" and h.meeting().status == "scheduled"
    assert len(h.replies) == 1  # one confirmation, not one per try


def test_deepgram_transcription_can_be_chosen(tmp_path):
    with respx.mock(assert_all_called=False) as router:
        h = Harness(tmp_path, router, transcript_provider="deepgram")
        try:
            h.email("<m1@acme.com>", ics=GOOGLE_ICS)
            assert list(h.created[0]["recording_config"]["transcript"]["provider"]) == ["deepgram"]
        finally:
            h.client.__exit__(None, None, None)


def _check(h):
    h.client.portal.call(h.client.app.state.agent.check_unfinished)


def _bot_state(h, status, transcript):
    h.router.get(f"{MEETSTREAM}/bots/bot-1/status").respond(json={"status": status})
    h.router.get(f"{MEETSTREAM}/bots/bot-1/transcriptions").respond(
        json={"transcriptions": [{"transcript_id": "tr-1", "provider": "meetstream", "status": transcript}]}
    )


def test_safety_net_sends_notes_when_the_webhook_never_came(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    _transcript_mocks(h)
    h.clock = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)  # after the meeting
    _bot_state(h, "Done", "Success")
    _check(h)
    assert h.meeting().status == "notes_sent"
    assert "Write the release notes" in h.replies[-1]["text"]


def test_safety_net_gives_up_on_a_transcript_that_never_comes(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    h.clock = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    _bot_state(h, "Done", "Not started")
    _check(h)
    assert h.meeting().status == "awaiting_transcript"
    h.clock = datetime(2026, 10, 20, 15, 10, tzinfo=timezone.utc)
    _check(h)
    assert h.meeting().status == "awaiting_transcript"
    h.clock = datetime(2026, 10, 20, 15, 16, tzinfo=timezone.utc)
    _check(h)
    assert h.meeting().status == "no_transcript"
    assert h.replies[-1] == {
        "text": 'I was in "Roadmap sync", but MeetStream didn\'t produce a transcript, so I have no notes to send.',
        "to": ["priya@acme.com"],
    }


def test_safety_net_reports_a_bot_that_errored(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    h.clock = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    _bot_state(h, "Error", "Not started")
    _check(h)
    assert h.meeting().status == "failed"
    assert "MeetStream: Error" in h.replies[-1]["text"]


def test_safety_net_leaves_future_meetings_alone(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    status = h.router.get(f"{MEETSTREAM}/bots/bot-1/status").respond(json={"status": "Scheduled"})
    _check(h)
    assert not status.called


def test_moved_after_the_bot_joined_gets_a_fresh_bot(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    h.client.post(f"/webhooks/meetstream/{_token(h)}", json={"bot_event": "bot.inmeeting", "bot_id": "bot-1"})
    moved = GOOGLE_ICS.replace("SEQUENCE:0", "SEQUENCE:1").replace("20261020T", "20261021T")
    h.email("<m2@acme.com>", ics=moved)
    assert h.meeting().bot_id == "bot-2"
    assert h.created[1]["join_at"] == "2026-10-21T14:00:00+00:00"


def test_bot_keeps_its_recording_long_enough_to_transcribe(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    assert h.created[0]["recording_config"] == {
        "retention": {"type": "timed", "hours": 24},
        "transcript": {"provider": {"meetstream": {"language": "auto"}}},
    }


def test_bots_can_run_without_callbacks(tmp_path):
    with respx.mock(assert_all_called=False) as router:
        h = Harness(tmp_path, router, meetstream_callbacks=False)
        try:
            h.email("<m1@acme.com>", ics=GOOGLE_ICS)
            assert "callback_url" not in h.created[0]
            _transcript_mocks(h)
            h.clock = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
            _bot_state(h, "Done", "Success")
            _check(h)
            assert h.meeting().status == "notes_sent"
        finally:
            h.client.__exit__(None, None, None)


def test_all_day_event_asks_for_a_time(h):
    all_day = GOOGLE_ICS.replace(
        "DTSTART;TZID=America/New_York:20261020T100000", "DTSTART;VALUE=DATE:20261020"
    ).replace("DTEND;TZID=America/New_York:20261020T103000", "DTEND;VALUE=DATE:20261021")
    h.email("<m1@acme.com>", ics=all_day)
    assert h.created == []
    assert "all-day event" in h.replies[-1]["text"]


def test_moving_a_meeting_into_the_past_calls_off_its_bot(h):
    h.email("<m1@acme.com>", text="Join? https://acme.zoom.us/j/123456789")  # bot joins now
    remove = h.router.get(f"{MEETSTREAM}/bots/bot-1/remove_bot").respond(json={})
    past = (
        GOOGLE_ICS.replace("UID:7kq2m1v0abc@google.com", "UID:adhoc:<m1@acme.com>")
        .replace("SEQUENCE:0", "SEQUENCE:1")
        .replace("20261020T100000", "20261013T100000")
        .replace("20261020T103000", "20261013T103000")
    )
    h.email("<m3@acme.com>", ics=past)
    assert remove.called
    assert h.meeting("adhoc:<m1@acme.com>").status == "cancelled"
    assert "already ended" in h.replies[-1]["text"]


def test_cancelling_a_meeting_in_progress_removes_the_bot(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    h.client.post(f"/webhooks/meetstream/{_token(h)}", json={"bot_event": "bot.inmeeting", "bot_id": "bot-1"})
    remove = h.router.get(f"{MEETSTREAM}/bots/bot-1/remove_bot").respond(json={})
    h.email(
        "<m2@acme.com>", ics=GOOGLE_ICS.replace("METHOD:REQUEST", "METHOD:CANCEL").replace("SEQUENCE:0", "SEQUENCE:1")
    )
    assert remove.called
    assert h.meeting().status == "cancelled"


def test_notes_open_with_the_meeting_details(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    _transcript_mocks(h)
    h.client.post(f"/webhooks/meetstream/{_token(h)}", json={"bot_event": "transcription.processed", "bot_id": "bot-1"})
    text = h.replies[-1]["text"]
    # Times are shown in the organiser's own zone, not UTC.
    assert text.startswith(
        "Notes from Roadmap sync\n\n"
        "Meeting: Roadmap sync\n"
        "Date: Tuesday, 20 October 2026\n"
        "Time: 10:00 – 10:30 EDT (America/New_York)\n"
        "Hosted by: Priya Shah (priya@acme.com)\n"
        "Platform: Google Meet\n"
        "Participants: Priya, Sam\n"
        "Invited: priya@acme.com, sam@acme.com\n\n"
        "Summary:\nAgreed to ship on Friday."
    )
    assert '<td style="padding:2px 0">Priya Shah (priya@acme.com)</td>' in h.replies[-1]["html"]


def test_notes_for_an_email_without_a_subject_get_a_subject_of_their_own(h):
    sent = []
    h.router.post(f"{AGENTMAIL}/send").mock(
        side_effect=lambda r: sent.append(json.loads(r.content)) or httpx.Response(200, json={"message_id": "n1"})
    )
    h.email("<m1@acme.com>", text="https://acme.zoom.us/j/123456789", subject=None)
    assert h.replies[-1]["text"].startswith('Joining "Zoom meeting" now.')
    _transcript_mocks(h)
    h.client.post(f"/webhooks/meetstream/{_token(h)}", json={"bot_event": "transcription.processed", "bot_id": "bot-1"})
    [notes] = sent  # a new email, not a reply that would have no subject
    assert notes["subject"] == "Notes: Zoom meeting"
    assert notes["to"] == ["priya@acme.com", "sam@acme.com"]


def test_bare_link_notes_show_when_the_bot_joined(h):
    h.email("<m1@acme.com>", text="Can you join? https://acme.zoom.us/j/123456789")
    _transcript_mocks(h)
    h.client.post(f"/webhooks/meetstream/{_token(h)}", json={"bot_event": "transcription.processed", "bot_id": "bot-1"})
    header = h.replies[-1]["text"].split("\n\nSummary:")[0]
    assert "Date: Wednesday, 14 October 2026\nTime: 12:00 UTC\n" in header  # no invite, so no end time
    # A link names no host, so the header says who asked rather than guessing who hosted
    assert "Requested by: priya@acme.com" in header and "Hosted by" not in header
    assert "Platform: Zoom" in header


def test_notes_have_all_five_sections():
    from app.notes import SCHEMA, render

    assert set(SCHEMA["required"]) == {"summary", "decisions", "action_items", "open_questions", "follow_ups"}
    text, html = render(
        {"Meeting": "DB choice"},
        {
            "summary": "Picked the primary database.",
            "decisions": [
                {
                    "decision": "Use PostgreSQL",
                    "rationale": "joins, transactions",
                    "alternatives": "MongoDB",
                    "approved_by": "Priya",
                }
            ],
            "action_items": [
                {
                    "task": "Finish OAuth integration",
                    "owner": "Thalha",
                    "due": "Wednesday",
                    "status": "in progress",
                    "depends_on": "client secret",
                }
            ],
            "open_questions": [{"item": "AWS or Google Cloud?", "kind": "postponed"}, {"item": "Load test budget"}],
            "follow_ups": [{"item": "Revisit deployment costs", "when": "after load testing next week"}],
        },
    )
    body = text.split("Summary:")[1]
    assert (
        body.index("Decisions:")
        < body.index("Action items:")
        < body.index("Open questions:")
        < body.index("Follow-ups and next steps:")
    )
    assert "- Use PostgreSQL (why: joins, transactions; alternatives: MongoDB; approved by Priya)" in text
    assert (
        "- [ ] Finish OAuth integration (Thalha, due Wednesday, status: in progress, depends on client secret)" in text
    )
    assert "- [Postponed] AWS or Google Cloud?" in text and "- Load test budget" in text
    assert "- Revisit deployment costs (after load testing next week)" in text
    assert "<h3>Open questions</h3>" in html


def test_empty_sections_say_so():
    from app.notes import render

    text, _ = render(
        {"Meeting": "Standup"},
        {"summary": "Quick sync.", "decisions": [], "action_items": [], "open_questions": [], "follow_ups": []},
    )
    assert text.count("- None recorded") == 4


# ---- review fixes -------------------------------------------------------------------


def test_an_email_that_keeps_failing_can_be_delivered_again(h):
    url = f"{AGENTMAIL}/{quote('<m1@acme.com>', safe='')}"
    h.router.get(url).respond(503)
    h.deliver(
        {"event_type": "message.received", "message": {"inbox_id": INBOX, "message_id": "<m1@acme.com>"}},
        event_id="evt-x",
    )
    assert h.created == []
    # Every try failed, so the delivery was forgotten: a redelivery is handled, not skipped as a duplicate.
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    h.deliver(
        {"event_type": "message.received", "message": {"inbox_id": INBOX, "message_id": "<m1@acme.com>"}},
        event_id="evt-x",
    )
    assert len(h.created) == 1


def test_notes_that_fail_are_retried_by_the_safety_net(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    gemini = _transcript_mocks(h)
    gemini.side_effect = [httpx.Response(503), gemini.return_value]
    h.client.post(f"/webhooks/meetstream/{_token(h)}", json={"bot_event": "transcription.processed", "bot_id": "bot-1"})
    assert h.meeting().status == "notes_failed"
    # No new MeetStream event comes: the periodic check picks the meeting up again.
    h.clock = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    _bot_state(h, "Done", "Success")
    _check(h)
    assert h.meeting().status == "notes_sent"


def test_notes_that_never_succeed_are_reported(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    gemini = _transcript_mocks(h)
    gemini.side_effect = None
    gemini.return_value = httpx.Response(503)
    h.client.post(f"/webhooks/meetstream/{_token(h)}", json={"bot_event": "transcription.processed", "bot_id": "bot-1"})
    h.clock = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    _bot_state(h, "Done", "Success")
    _check(h)
    _check(h)
    assert h.meeting().status == "notes_abandoned"
    assert h.replies[-1]["text"].endswith("couldn't write the notes, so there are none to send.")
    _check(h)
    assert gemini.call_count == 3  # no more tries once abandoned


@pytest.mark.parametrize("status", ["Denied", "NotAllowed", "Failed", "BotRejected"])
def test_every_failure_status_is_reported_by_the_safety_net(h, status):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    h.clock = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    _bot_state(h, status, "Not started")
    _check(h)
    assert h.meeting().status == "failed"
    assert f"MeetStream: {status}" in h.replies[-1]["text"]


def test_an_update_arriving_while_the_bot_is_being_created_waits_its_turn(h):
    import asyncio

    release = asyncio.Event()

    async def slow_create(request):
        await release.wait()
        return h._create_bot(request)

    h.router.post(f"{MEETSTREAM}/bots/create_bot").mock(side_effect=slow_create)
    h.email("<m1@acme.com>", ics=GOOGLE_ICS, deliver=False)
    moved = GOOGLE_ICS.replace("SEQUENCE:0", "SEQUENCE:1").replace("T100000", "T110000").replace("T103000", "T113000")
    h.email("<m2@acme.com>", ics=moved, deliver=False)
    reschedule = h.router.patch(f"{MEETSTREAM}/calendar/scheduled_bots/bot-1").respond(json={})
    agent = h.client.app.state.agent

    async def both():
        first = asyncio.create_task(agent.handle_email(INBOX, "<m1@acme.com>"))
        second = asyncio.create_task(agent.handle_email(INBOX, "<m2@acme.com>"))
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.gather(first, second)

    h.client.portal.call(both)
    assert len(h.created) == 1  # the update moved the first bot instead of sending a second
    assert reschedule.called


def test_meetstream_callback_with_a_bad_body_is_rejected(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    resp = h.client.post(
        f"/webhooks/meetstream/{_token(h)}", content=b"not json", headers={"content-type": "application/json"}
    )
    assert resp.status_code == 400


def test_meeting_locks_are_forgotten_after_use(h):
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    agent = h.client.app.state.agent
    assert agent._locks == {} and agent._lock_users == {}


def test_openai_writes_the_notes(tmp_path):
    with respx.mock(assert_all_called=False) as router:
        h = Harness(tmp_path, router, llm_provider="openai", openai_api_key="sk-test", openai_model="gpt-test")
        try:
            h.email("<m1@acme.com>", ics=GOOGLE_ICS)
            _transcript_mocks(h)
            notes = {"summary": "Shipped.", "decisions": [], "action_items": [], "open_questions": [], "follow_ups": []}
            openai = router.post("https://api.openai.com/v1/chat/completions").respond(
                json={"choices": [{"message": {"content": json.dumps(notes)}}]}
            )
            h.client.post(
                f"/webhooks/meetstream/{_token(h)}", json={"bot_event": "transcription.processed", "bot_id": "bot-1"}
            )
            body = json.loads(openai.calls.last.request.content)
            assert openai.calls.last.request.headers["authorization"] == "Bearer sk-test"
            assert body["model"] == "gpt-test" and "temperature" not in body  # reasoning models take only the default
            assert body["response_format"]["type"] == "json_schema"
            assert h.meeting().status == "notes_sent"
        finally:
            h.client.__exit__(None, None, None)


def test_create_inbox_script(monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "create_inbox", Path(__file__).parents[1] / "scripts" / "create_inbox.py"
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    with respx.mock() as router:
        route = router.post("https://api.agentmail.to/v0/inboxes").respond(
            json={"inbox_id": "my-notetaker@agentmail.to"}
        )
        with httpx.Client() as http:
            assert script.create_inbox("my-notetaker", "am_test", http) == ("my-notetaker@agentmail.to", True)
        assert json.loads(route.calls.last.request.content)["username"] == "my-notetaker"

    # Running it again for an inbox you already own isn't an error
    taken = {"name": "AlreadyExistsError", "code": "already_exists"}
    with respx.mock() as router:
        router.post("https://api.agentmail.to/v0/inboxes").respond(403, json=taken)
        router.get("https://api.agentmail.to/v0/inboxes").respond(
            json={"inboxes": [{"inbox_id": "my-notetaker@agentmail.to"}]}
        )
        with httpx.Client() as http:
            assert script.create_inbox("my-notetaker", "am_test", http) == ("my-notetaker@agentmail.to", False)

    # A name someone else has is reported plainly
    with respx.mock() as router:
        router.post("https://api.agentmail.to/v0/inboxes").respond(403, json=taken)
        router.get("https://api.agentmail.to/v0/inboxes").respond(json={"inboxes": []})
        with httpx.Client() as http, pytest.raises(SystemExit, match="taken by someone else"):
            script.create_inbox("my-notetaker", "am_test", http)


def test_participants_skip_the_bot_and_unnamed_speakers():
    from app.notes import meeting_details
    from app.store import Meeting

    m = Meeting(
        uid="x",
        sequence=0,
        inbox_id="",
        message_id="",
        requester="",
        title="Sync",
        meeting_link="",
        start_at=None,
        attendees=[],
        bot_id=None,
        callback_token="",
        status="",
        updated_at="",
    )
    details = meeting_details(m, ["Priya", "AgentMail Notetaker", "Unknown", "Sam", "Priya"], "AgentMail Notetaker")
    assert details["Participants"] == "Priya, Sam"
    assert "Invited" not in details  # nobody on an invite: the row is left out
    # A transcript without names labels speakers SPEAKER_00, SPEAKER_01...: those aren't people to list
    assert "Participants" not in meeting_details(m, ["SPEAKER_00", "Speaker 1"], "AgentMail Notetaker")
