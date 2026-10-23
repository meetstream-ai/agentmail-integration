"""The toolkit against mocked AgentMail and MeetStream APIs."""

import asyncio
import json
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import httpx
import pytest
import respx
from agentmail import AgentMail

from agentmail_meetstream import MeetStream, MeetStreamToolkit

# Read as text: the same on every platform, whatever line endings the checkout has.
GOOGLE_ICS = (Path(__file__).parents[2] / "tests" / "fixtures" / "google_invite.ics").read_text().encode()
INBOX = "notetaker@agentmail.to"
AM = "https://api.agentmail.to/v0"
MS = "https://api.meetstream.ai/api/v1"
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


class Env:
    def __init__(self, router, **options):
        self.router = router
        self.created = []
        self.bots = []  # what MeetStream's bot list returns, newest first
        self.cancelled = []
        router.post(f"{MS}/bots/create_bot").mock(side_effect=self._create)
        router.get(f"{MS}/bots").mock(side_effect=lambda r: httpx.Response(200, json={"bots": self.bots}))
        router.delete(url__regex=rf"{MS}/calendar/scheduled_bots/[^/]+$").mock(side_effect=self._cancel)
        options.setdefault("allowed_senders", ["@acme.com"])
        kit = MeetStreamToolkit(
            agentmail=AgentMail(api_key="am_test"),
            meetstream=MeetStream(api_key="ms_test"),
            **options,
        )
        kit.clients.now = lambda: NOW
        self.kit = kit
        self.tools = {t.name: t for t in kit.get_tools()}

    def _create(self, request):
        body = json.loads(request.content)
        self.created.append(body)
        bot_id = f"bot-{len(self.created)}"
        self.bots.insert(
            0,
            {
                "bot_id": bot_id,
                "status": "Scheduled" if "join_at" in body else "Joining",
                "meeting_url": body["meeting_link"],
                "custom_attributes": body.get("custom_attributes"),
            },
        )
        return httpx.Response(201, json={"bot_id": bot_id, "status": "Scheduled"})

    def _cancel(self, request):
        bot_id = request.url.path.rsplit("/", 1)[1]
        self.cancelled.append(bot_id)
        self.bots = [b for b in self.bots if b["bot_id"] != bot_id]  # MeetStream deletes it
        return httpx.Response(200, json={})

    def call(self, name, **arguments):
        return self.tools[name].func(**arguments)

    def email(self, message_id, sender="Priya Shah <priya@acme.com>", ics=None, raw=None, text=""):
        """Put an email in the mocked inbox. Paths are matched loosely: the SDK encodes the ids."""
        attachments = []
        if ics is not None:
            attachments.append(
                {"attachment_id": "att1", "filename": "invite.ics", "content_type": "text/calendar", "size": len(ics)}
            )
            self.router.get(url__regex=r".*/messages/.+/attachments/att1$").respond(
                json={
                    "attachment_id": "att1",
                    "size": len(ics),
                    "download_url": "https://cdn.example/invite.ics",
                    "expires_at": "2030-01-01T00:00:00Z",
                }
            )
            self.router.get("https://cdn.example/invite.ics").respond(content=ics)
        else:
            if raw is None:
                plain = EmailMessage()
                plain.set_content(text or "hi")
                raw = plain.as_bytes()
            self.router.get(url__regex=r".*/messages/.+/raw$").respond(
                json={
                    "message_id": message_id,
                    "size": len(raw),
                    "download_url": "https://cdn.example/m.eml",
                    "expires_at": "2030-01-01T00:00:00Z",
                }
            )
            self.router.get("https://cdn.example/m.eml").respond(content=raw)
        self.router.get(url__regex=r".*/inboxes/[^/]+/messages/[^/]+$").respond(
            json={
                "inbox_id": INBOX,
                "thread_id": "thread-7",
                "message_id": message_id,
                "labels": ["received"],
                "timestamp": "2026-10-14T11:00:00Z",
                "from": sender,
                "to": [INBOX, "sam@acme.com"],
                "subject": "Invitation: Roadmap sync",
                "text": text,
                "attachments": attachments,
                "size": 1000,
                "updated_at": "2026-10-14T11:00:00Z",
                "created_at": "2026-10-14T11:00:00Z",
            }
        )


@pytest.fixture
def env():
    with respx.mock(assert_all_called=False) as router:
        yield Env(router)


def test_join_from_an_invite_email(env):
    env.email("<m1@acme.com>", ics=GOOGLE_ICS)
    result = env.call("join_meeting_from_email", inbox_id=INBOX, message_id="<m1@acme.com>")

    [bot] = env.created
    assert bot["meeting_link"] == "https://meet.google.com/abc-defg-hij"
    assert bot["join_at"] == "2026-10-20T14:00:00+00:00"
    assert bot["recording_config"]["retention"] == {"type": "timed", "hours": 24}
    assert bot["video_required"] is False
    # The bot remembers the invite thread, so the notes can be a reply on it later
    assert bot["custom_attributes"] == {
        "title": "Roadmap sync",
        "inbox_id": INBOX,
        "message_id": "<m1@acme.com>",
        "thread_id": "thread-7",
        "start": "2026-10-20T14:00:00+00:00",
        "end": "2026-10-20T14:30:00+00:00",
        "timezone": "America/New_York",
        "organizer": "priya@acme.com",
        "attendees": ["priya@acme.com", "sam@acme.com"],
        "recurring": False,
        "uid": "7kq2m1v0abc@google.com",
        "occurrence": None,
    }
    assert result.bot_id == "bot-1" and result.thread_id == "thread-7"
    assert result.timezone == "America/New_York"
    assert result.attendees == ["priya@acme.com", "sam@acme.com"]


def test_inline_invite_in_the_raw_email(env):
    raw = EmailMessage()
    raw.set_content("Invitation")
    raw.add_alternative(GOOGLE_ICS.decode(), subtype="calendar", params={"method": "REQUEST"})
    env.email("<m1@acme.com>", raw=raw.as_bytes())
    assert env.call("join_meeting_from_email", inbox_id=INBOX, message_id="<m1@acme.com>").title == "Roadmap sync"


def test_a_bare_link_joins_now(env):
    env.email("<m1@acme.com>", text="Join? https://acme.zoom.us/j/123456789")
    result = env.call("join_meeting_from_email", inbox_id=INBOX, message_id="<m1@acme.com>")
    assert "join_at" not in env.created[0] and result.join_at is None


def test_senders_can_be_restricted():
    with respx.mock(assert_all_called=False) as router:
        env = Env(router, allowed_senders=["@acme.com"])
        env.email("<m1@evil.com>", sender="mallory@evil.com", ics=GOOGLE_ICS)
        with pytest.raises(PermissionError):
            env.call("join_meeting_from_email", inbox_id=INBOX, message_id="<m1@evil.com>")
        assert env.created == []


@pytest.mark.parametrize(
    "change, reason",
    [
        (lambda s: s.replace("METHOD:REQUEST", "METHOD:CANCEL"), "cancellation"),
        (
            lambda s: s.replace("DTSTART;TZID=America/New_York:20261020T100000", "DTSTART;VALUE=DATE:20261020").replace(
                "DTEND;TZID=America/New_York:20261020T103000", "DTEND;VALUE=DATE:20261021"
            ),
            "all-day",
        ),
        (lambda s: s.replace("20261020T1", "20261013T1"), "already ended"),
        (
            lambda s: s.replace("X-GOOGLE-CONFERENCE:https://meet.google.com/abc-defg-hij\n", "").replace(
                "Join with Google Meet: https://meet.google.com/abc-defg-hij", "Room 4"
            ),
            "no Zoom",
        ),
    ],
)
def test_invites_it_cannot_join_say_why(env, change, reason):
    env.email("<m1@acme.com>", ics=change(GOOGLE_ICS.decode()).encode())
    with pytest.raises(ValueError, match=reason):
        env.call("join_meeting_from_email", inbox_id=INBOX, message_id="<m1@acme.com>")
    assert env.created == []


WEEKLY = GOOGLE_ICS.decode().replace("SEQUENCE:0", "RRULE:FREQ=WEEKLY;COUNT=3\nSEQUENCE:0").encode()


def test_a_recurring_invite_joins_its_next_meeting(env):
    env.email("<m1@acme.com>", ics=WEEKLY)
    first = env.call("join_meeting_from_email", inbox_id=INBOX, message_id="<m1@acme.com>")
    assert first.recurring and first.join_at == "2026-10-20T14:00:00+00:00"
    assert env.created[0]["custom_attributes"]["occurrence"] == "2026-10-20T14:00:00+00:00"

    # The same invite again changes nothing
    again = env.call("join_meeting_from_email", inbox_id=INBOX, message_id="<m1@acme.com>")
    assert again.bot_id == first.bot_id and len(env.created) == 1

    # Once that meeting is over, the same call schedules the next one
    env.bots[0]["status"] = "Done"
    env.kit.clients.now = lambda: datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    second = env.call("join_meeting_from_email", inbox_id=INBOX, message_id="<m1@acme.com>")
    assert second.join_at == "2026-10-27T14:00:00+00:00" and env.cancelled == []

    env.kit.clients.now = lambda: datetime(2026, 12, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="no meetings left"):
        env.call("join_meeting_from_email", inbox_id=INBOX, message_id="<m1@acme.com>")


def test_a_bot_in_the_call_is_not_doubled(env):
    env.email("<m1@acme.com>", ics=WEEKLY)
    env.call("join_meeting_from_email", inbox_id=INBOX, message_id="<m1@acme.com>")
    env.bots[0]["status"] = "InMeeting"
    env.kit.clients.now = lambda: datetime(2026, 10, 20, 14, 10, tzinfo=timezone.utc)
    later = env.call("join_meeting_from_email", inbox_id=INBOX, message_id="<m1@acme.com>")
    assert later.join_at == "2026-10-27T14:00:00+00:00"  # the next meeting, not a second bot in this one


def test_an_updated_invite_replaces_the_waiting_bot(env):
    env.email("<m1@acme.com>", ics=GOOGLE_ICS)
    env.call("join_meeting_from_email", inbox_id=INBOX, message_id="<m1@acme.com>")
    moved = GOOGLE_ICS.decode().replace("SEQUENCE:0", "SEQUENCE:1").replace("T100000", "T110000")
    env.email("<m2@acme.com>", ics=moved.replace("T103000", "T113000").encode())
    result = env.call("join_meeting_from_email", inbox_id=INBOX, message_id="<m2@acme.com>")
    assert env.cancelled == ["bot-1"] and result.bot_id == "bot-2"
    assert result.join_at == "2026-10-20T15:00:00+00:00"


def test_join_a_link_now_or_later(env):
    later = env.call(
        "join_meeting",
        meeting_link="https://meet.google.com/abc-defg-hij",
        join_at="2026-10-20T14:00:00Z",
        title="Sync",
    )
    assert later.join_at == "2026-10-20T14:00:00+00:00" and env.created[0]["custom_attributes"] == {"title": "Sync"}
    env.call("join_meeting", meeting_link="https://meet.google.com/abc-defg-hij", join_at="2026-10-14T11:00:00Z")
    assert "join_at" not in env.created[1]  # a time already passed means now
    with pytest.raises(ValueError):
        env.call("join_meeting", meeting_link="https://example.com/not-a-meeting")


def _bot(router, status, transcript_status="Success"):
    router.get(f"{MS}/bots/bot-1/detail").respond(
        json={
            "bot_details": {
                "Status": status,
                "custom_attributes": {
                    "title": "Roadmap sync",
                    "inbox_id": INBOX,
                    "message_id": "<m1@acme.com>",
                    "thread_id": "thread-7",
                },
            }
        }
    )
    router.get(f"{MS}/bots/bot-1/transcriptions").respond(
        json={
            "transcriptions": [
                {"transcript_id": "tr-1", "provider": "meetstream", "status": transcript_status},
                {"transcript_id": None, "provider": "meeting_captions", "status": "Success"},
            ]
        }
    )


def test_status_and_transcript(env):
    _bot(env.router, "InMeeting", "Not started")
    status = env.call("get_meeting_status", bot_id="bot-1")
    assert (status.status, status.finished, status.transcript_ready) == ("InMeeting", False, False)
    assert env.call("get_meeting_transcript", bot_id="bot-1").ready is False

    _bot(env.router, "Done")
    env.router.get(f"{MS}/transcript/tr-1/get_transcript").respond(
        json=[
            {"speaker": "Priya", "transcript": "Ship Friday."},
            {"participant": {"name": "Sam"}, "words": [{"text": "Agreed."}]},
        ]
    )
    status = env.call("get_meeting_status", bot_id="bot-1")
    assert status.finished and status.transcript_ready and status.thread_id == "thread-7"
    transcript = env.call("get_meeting_transcript", bot_id="bot-1")
    assert transcript.transcript == "Priya: Ship Friday.\nSam: Agreed." and transcript.message_id == "<m1@acme.com>"


def test_leave_cancels_or_removes(env):
    _bot(env.router, "Scheduled")
    env.call("leave_meeting", bot_id="bot-1")
    assert env.cancelled == ["bot-1"]

    _bot(env.router, "InMeeting")
    remove = env.router.get(f"{MS}/bots/bot-1/remove_bot").respond(202, json={})
    env.call("leave_meeting", bot_id="bot-1")
    assert remove.called


def test_list_meetings(env):
    env.bots = [
        {
            "bot_id": "bot-1",
            "status": "Done",
            "platform": "GMeet",
            "created_at": "2026-10-14T10:00:00Z",
            "custom_attributes": {"title": "Roadmap sync"},
        }
    ]
    [meeting] = env.call("list_meetings", limit=5).meetings
    assert (meeting.bot_id, meeting.title, meeting.status) == ("bot-1", "Roadmap sync", "Done")


def test_openai_agents_sdk_tools():
    from agentmail_meetstream.openai import MeetStreamToolkit as OpenAIToolkit

    with respx.mock(assert_all_called=False) as router:
        _bot(router, "Done")
        kit = OpenAIToolkit(agentmail=AgentMail(api_key="t"), meetstream=MeetStream(api_key="t"))
        tools = {t.name: t for t in kit.get_tools()}
        assert set(tools) == {
            "join_meeting_from_email",
            "join_meeting",
            "get_meeting_status",
            "get_meeting_transcript",
            "leave_meeting",
            "list_meetings",
        }
        assert tools["join_meeting"].params_json_schema["required"] == ["meeting_link"]

        out = asyncio.run(tools["get_meeting_status"].on_invoke_tool(None, json.dumps({"bot_id": "bot-1"})))
        assert json.loads(out)["transcript_ready"] is True

        router.get(f"{MS}/bots/nope/detail").respond(404, json={"message": "Bot not found"})
        with pytest.raises(RuntimeError, match=r"Bot not found \(HTTP 404\)"):
            asyncio.run(tools["get_meeting_status"].on_invoke_tool(None, json.dumps({"bot_id": "nope"})))


def test_langchain_tools():
    from agentmail_meetstream.langchain import MeetStreamToolkit as LangChainToolkit

    with respx.mock(assert_all_called=False) as router:
        _bot(router, "Done")
        kit = LangChainToolkit(agentmail=AgentMail(api_key="t"), meetstream=MeetStream(api_key="t"))
        tools = {t.name: t for t in kit.get_tools(["get_meeting_status", "leave_meeting"])}
        assert set(tools) == {"get_meeting_status", "leave_meeting"}
        assert json.loads(tools["get_meeting_status"].invoke({"bot_id": "bot-1"}))["finished"] is True

        router.get(f"{MS}/bots/nope/detail").respond(404, json={"message": "Bot not found"})
        # A failed call comes back as text the model can act on, not an exception
        assert "Bot not found" in tools["get_meeting_status"].invoke({"bot_id": "nope"})


def test_tool_names_do_not_clash_with_agentmail_toolkit():
    agentmail_tools = {
        "list_inboxes",
        "get_inbox",
        "create_inbox",
        "delete_inbox",
        "list_threads",
        "get_thread",
        "get_attachment",
        "send_message",
        "reply_to_message",
        "update_message",
        "forward_message",
    }
    names = {
        t.name
        for t in MeetStreamToolkit(agentmail=AgentMail(api_key="t"), meetstream=MeetStream(api_key="t")).get_tools()
    }
    assert not names & agentmail_tools


@pytest.mark.parametrize(
    "bot_status, runs, expected",
    [
        ("InMeeting", [{"transcript_id": "tr-1", "status": "Not started"}], "pending"),
        ("Done", [{"transcript_id": "tr-1", "status": "Success"}], "ready"),
        (
            "Done",
            [
                {"transcript_id": "tr-1", "status": "Skipped"},
                {"transcript_id": None, "provider": "meeting_captions", "status": "Success"},
            ],
            "unavailable",
        ),
        ("Done", [{"transcript_id": "tr-1", "status": "Failed"}], "unavailable"),
        ("Done", [], "unavailable"),
        ("MediaProcessing", [], "pending"),
    ],
)
def test_transcript_state_tells_waiting_from_never(env, bot_status, runs, expected):
    env.router.get(f"{MS}/bots/bot-1/detail").respond(json={"bot_details": {"Status": bot_status}})
    env.router.get(f"{MS}/bots/bot-1/transcriptions").respond(json={"transcriptions": runs})
    status = env.call("get_meeting_status", bot_id="bot-1")
    assert status.transcript == expected and status.transcript_ready == (expected == "ready")


def test_an_empty_allow_list_lets_nobody_in():
    with respx.mock(assert_all_called=False) as router:
        env = Env(router, allowed_senders=[])
        env.email("<m1@acme.com>", ics=GOOGLE_ICS)
        with pytest.raises(PermissionError):
            env.call("join_meeting_from_email", inbox_id=INBOX, message_id="<m1@acme.com>")
        assert env.created == []


@pytest.mark.parametrize(
    "address, allowed",
    [
        ("priya@acme.com", True),
        ("PRIYA@ACME.COM", True),
        ("x@evil-acme.com", False),
        ("x@acme.com.evil.io", False),
        ("", False),
        ("boss@partner.io", True),
    ],
)
def test_sender_matching(address, allowed):
    from agentmail_meetstream.functions import Clients

    clients = Clients(agentmail=None, meetstream=None, allowed_senders=["@acme.com", "boss@partner.io"])
    assert clients.sender_allowed(address) is allowed


def test_status_and_transcript_carry_the_meeting_details(env):
    env.router.get(f"{MS}/bots/bot-1/detail").respond(
        json={
            "bot_details": {
                "Status": "Done",
                "custom_attributes": {
                    "title": "Roadmap sync",
                    "start": "2026-10-20T14:00:00+00:00",
                    "timezone": "America/New_York",
                    "organizer": "priya@acme.com",
                    "attendees": ["priya@acme.com", "sam@acme.com"],
                },
            }
        }
    )
    env.router.get(f"{MS}/bots/bot-1/transcriptions").respond(
        json={"transcriptions": [{"transcript_id": "tr-1", "status": "Success"}]}
    )
    env.router.get(f"{MS}/transcript/tr-1/get_transcript").respond(
        json=[
            {"speaker": "Priya", "transcript": "Hi."},
            {"speaker": "Sam", "transcript": "Hey."},
            {"speaker": "Priya", "transcript": "Ship it."},
            {"speaker": "Unknown", "transcript": "(noise)"},
            {"speaker": "SPEAKER_02", "transcript": "Agreed."},  # a voice the transcript couldn't name
        ]
    )
    status = env.call("get_meeting_status", bot_id="bot-1")
    assert (status.timezone, status.organizer, status.attendees) == (
        "America/New_York",
        "priya@acme.com",
        ["priya@acme.com", "sam@acme.com"],
    )
    assert env.call("get_meeting_transcript", bot_id="bot-1").participants == ["Priya", "Sam"]


def test_bots_from_before_these_fields_still_read(env):
    env.router.get(f"{MS}/bots/bot-1/detail").respond(
        json={"bot_details": {"Status": "Done", "custom_attributes": {"title": "Old"}}}
    )
    env.router.get(f"{MS}/bots/bot-1/transcriptions").respond(json={"transcriptions": []})
    status = env.call("get_meeting_status", bot_id="bot-1")
    assert status.title == "Old" and status.attendees == [] and status.start is None
