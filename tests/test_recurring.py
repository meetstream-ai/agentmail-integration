"""Recurring invites: the series is followed, one meeting at a time."""

import json
from datetime import datetime, timezone

import pytest
import respx

from tests.test_flow import GOOGLE_ICS, MEETSTREAM, Harness, _transcript_mocks

UID = "7kq2m1v0abc@google.com"
WEEKLY = GOOGLE_ICS.replace("SEQUENCE:0", "RRULE:FREQ=WEEKLY;COUNT=3\nSEQUENCE:0")


def one_meeting(day: str, method: str = "REQUEST", start: str | None = None) -> str:
    """An invite that changes one meeting of the series: the one that was on `day` at 10:00 New York."""
    start = start or f"{day}T100000"
    return (
        GOOGLE_ICS.replace("METHOD:REQUEST", f"METHOD:{method}")
        .replace("SEQUENCE:0", "SEQUENCE:1")
        .replace(
            "DTSTART;TZID=America/New_York:20261020T100000",
            f"RECURRENCE-ID;TZID=America/New_York:{day}T100000\nDTSTART;TZID=America/New_York:{start}",
        )
        .replace("DTEND;TZID=America/New_York:20261020T103000", f"DTEND;TZID=America/New_York:{start[:-4]}3000")
    )


@pytest.fixture
def h(tmp_path):
    with respx.mock(assert_all_called=False) as router:
        harness = Harness(tmp_path, router)
        yield harness
        harness.client.__exit__(None, None, None)


def sync(h):
    h.client.portal.call(h.client.app.state.agent.sync_series)


def test_a_recurring_invite_schedules_its_first_meeting(h):
    h.email("<m1@acme.com>", ics=WEEKLY)

    [bot] = h.created
    assert bot["join_at"] == "2026-10-20T14:00:00+00:00"
    assert h.replies[-1]["text"] == (
        'Thanks, I\'ll join every "Roadmap sync", starting Tue 20 Oct 2026, 14:00 UTC, and email '
        "the notes and action items to everyone on the invite after each one."
    )
    meeting = h.meeting(f"{UID}#2026-10-20T14:00:00+00:00")
    assert meeting.status == "scheduled" and meeting.message_id == "<m1@acme.com>"


def test_the_next_meeting_gets_its_bot_once_one_is_over(h):
    h.email("<m1@acme.com>", ics=WEEKLY)
    sync(h)
    assert len(h.created) == 1  # still waiting for the first one

    h.clock = datetime(2026, 10, 20, 14, 45, tzinfo=timezone.utc)
    sync(h)
    assert h.created[1]["join_at"] == "2026-10-27T14:00:00+00:00"
    # The first meeting's bot is left alone: its notes are still to come.
    assert h.meeting(f"{UID}#2026-10-20T14:00:00+00:00").status == "scheduled"

    h.clock = datetime(2026, 10, 27, 14, 45, tzinfo=timezone.utc)
    sync(h)
    # New York's clocks went back on 1 November: 10:00 there is now 15:00 UTC.
    assert h.created[2]["join_at"] == "2026-11-03T15:00:00+00:00"

    h.clock = datetime(2026, 11, 3, 15, 45, tzinfo=timezone.utc)
    sync(h)
    assert len(h.created) == 3  # COUNT=3: the series is over
    assert h.client.app.state.agent.store.get_series(UID).status == "ended"


def test_each_meetings_notes_reply_on_the_invite_thread(h):
    h.email("<m1@acme.com>", ics=WEEKLY)
    _transcript_mocks(h)
    token = h.created[0]["callback_url"].rsplit("/", 1)[1]
    h.client.post(f"/webhooks/meetstream/{token}", json={"bot_event": "transcription.processed", "bot_id": "bot-1"})

    notes = h.replies[-1]
    assert "Write the release notes" in notes["text"]
    assert "Date: Tuesday, 20 October 2026" in notes["text"]
    assert h.meeting(f"{UID}#2026-10-20T14:00:00+00:00").status == "notes_sent"


def test_cancelling_the_series_calls_off_the_waiting_bot(h):
    h.email("<m1@acme.com>", ics=WEEKLY)
    cancel = h.router.delete(f"{MEETSTREAM}/calendar/scheduled_bots/bot-1").respond(json={})
    h.email("<m2@acme.com>", ics=WEEKLY.replace("METHOD:REQUEST", "METHOD:CANCEL").replace("SEQUENCE:0", "SEQUENCE:1"))

    assert cancel.called
    assert h.replies[-1]["text"] == 'Got it, "Roadmap sync" is cancelled. I won\'t join any more of them.'
    h.clock = datetime(2026, 10, 20, 14, 45, tzinfo=timezone.utc)
    sync(h)
    assert len(h.created) == 1


def test_cancelling_one_meeting_skips_to_the_next(h):
    h.email("<m1@acme.com>", ics=WEEKLY)
    cancel = h.router.delete(f"{MEETSTREAM}/calendar/scheduled_bots/bot-1").respond(json={})
    h.email("<m2@acme.com>", ics=one_meeting("20261020", method="CANCEL"))

    assert cancel.called
    assert h.created[1]["join_at"] == "2026-10-27T14:00:00+00:00"
    assert h.replies[-1]["text"] == 'Got it, I\'ll skip "Roadmap sync" on Tue 20 Oct 2026, 14:00 UTC.'


def test_moving_one_meeting_moves_its_bot(h):
    h.email("<m1@acme.com>", ics=WEEKLY)
    reschedule = h.router.patch(f"{MEETSTREAM}/calendar/scheduled_bots/bot-1").respond(json={})
    h.email("<m2@acme.com>", ics=one_meeting("20261020", start="20261020T113000"))

    assert json.loads(reschedule.calls.last.request.content)["scheduled_join_time"] == "2026-10-20T15:30:00+00:00"
    assert len(h.created) == 1
    assert h.replies[-1]["text"] == 'Updated: I\'ll join "Roadmap sync" on Tue 20 Oct 2026, 15:30 UTC.'


def test_a_change_to_a_series_we_dont_follow_is_ignored(h):
    h.email("<m1@acme.com>", ics=one_meeting("20261027"))
    assert h.created == [] and h.replies == []


def test_the_same_series_sent_twice_is_handled_once(h):
    h.email("<m1@acme.com>", ics=WEEKLY)
    h.email("<m2@acme.com>", ics=WEEKLY)
    assert len(h.created) == 1 and len(h.replies) == 1


def test_a_one_off_made_recurring_keeps_its_bot_for_that_meeting(h):
    h.clock = datetime(2026, 10, 20, 14, 5, tzinfo=timezone.utc)  # the meeting is on
    h.email("<m1@acme.com>", ics=GOOGLE_ICS)
    assert len(h.created) == 1 and "join_at" not in h.created[0]  # joined right away

    h.email("<m2@acme.com>", ics=WEEKLY.replace("SEQUENCE:0", "SEQUENCE:1"))
    assert len(h.created) == 1  # no second bot in the same call

    h.clock = datetime(2026, 10, 20, 14, 45, tzinfo=timezone.utc)
    sync(h)
    assert h.created[1]["join_at"] == "2026-10-27T14:00:00+00:00"


def test_a_series_that_is_over_is_declined(h):
    h.clock = datetime(2026, 12, 1, tzinfo=timezone.utc)
    h.email("<m1@acme.com>", ics=WEEKLY)
    assert h.created == []
    assert h.replies[-1]["text"] == '"Roadmap sync" has no meetings left, so there\'s nothing for me to join.'
