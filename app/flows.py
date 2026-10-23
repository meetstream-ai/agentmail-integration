"""The loop: invite email -> scheduled bot -> transcript -> notes email."""

import asyncio
import logging
import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parseaddr

from app.agentmail import AgentMail
from app.calendar import parse_event
from app.config import Settings
from app.invites import (
    CALENDAR_TYPES,
    Invite,
    find_meeting_link,
    ics_from_raw_email,
    merge_change,
    next_occurrence,
    occurrence_uid,
    parse_ics,
    platform_of,
)
from app.meetstream import MeetStream
from app.notes import NoteWriter, meeting_details, render
from app.store import Meeting, Series, Store

log = logging.getLogger("notetaker")

IN_CALL_EVENTS = {"bot.inmeeting", "bot.in_meeting"}
FAILED_EVENTS = {"bot.failed", "bot.denied", "bot.notallowed"}

# MeetStream bot statuses, for when its webhooks don't arrive
BOT_FAILED = {"Error", "BotRejected", "BotNotAllowed", "NotAllowed", "Denied", "Failed"}
BOT_FINISHED = {
    "Stopped",
    "MediaProcessing",
    "AudioProcessing",
    "VideoProcessing",
    "TranscriptionReady",
    "Done",
    "MediaExpired",
}
TRANSCRIPT_WAIT = timedelta(minutes=15)
# How far ahead calendar meetings get a bot, matching MeetStream's own sync window
CALENDAR_LOOKAHEAD = timedelta(hours=24)
NOTES_MAX_ATTEMPTS = 3  # tries at writing the notes before telling the requester


def _when(moment: datetime | None) -> str:
    return moment.strftime("%a %d %b %Y, %H:%M UTC") if moment else "now"


class Agent:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        mail: AgentMail,
        meetstream: MeetStream,
        notes: NoteWriter,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.settings = settings
        self.store = store
        self.mail = mail
        self.meetstream = meetstream
        self.notes = notes
        self.now = now
        # One task at a time per meeting: an updated invite can arrive while
        # the first one's bot is still being created.
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_users: dict[str, int] = {}

    @asynccontextmanager
    async def _one_at_a_time(self, uid: str):
        """Hold this meeting's lock; forget it when nobody else is waiting."""
        lock = self._locks.setdefault(uid, asyncio.Lock())
        self._lock_users[uid] = self._lock_users.get(uid, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._lock_users[uid] -= 1
            if not self._lock_users[uid]:
                del self._lock_users[uid], self._locks[uid]

    # ---- inbound email ------------------------------------------------

    async def handle_email(self, inbox_id: str, message_id: str) -> None:
        msg = await self.mail.get_message(inbox_id, message_id)
        sender = parseaddr(msg.get("from") or "")[1].lower()
        if not sender or sender == self.settings.agentmail_inbox_id.lower():
            return
        if not self.settings.sender_allowed(sender):
            # No reply: answering strangers is how an inbox becomes a spam source.
            log.info("ignoring email from %s: not in ALLOWED_SENDERS", sender)
            return

        invite, ics = await self._find_invite(inbox_id, message_id, msg)
        if invite and ics and (invite.recurring or invite.recurrence_id):
            async with self._one_at_a_time(invite.uid):
                await self._handle_series(invite, ics, inbox_id, message_id, sender)
            return
        if invite is None:
            link = find_meeting_link(msg.get("text"), msg.get("html"))
            if not link:
                return
            # A bare link with no invite attached: join right away. The notes
            # header shows the time the bot joined, as there's no invite to say.
            subject = (msg.get("subject") or "").strip()
            invite = Invite(
                uid=f"adhoc:{message_id}",
                title=subject or f"{platform_of(link) or 'Online'} meeting",
                start=self.now(),
                meeting_link=link,
                organizer=sender,
                attendees=[a.lower() for _, a in getaddresses((msg.get("to") or []) + (msg.get("cc") or [])) if a],
            )
            if not subject:
                # A reply keeps the original's subject, and AgentMail can't change it, so
                # notes for a subjectless email go out as a new email with a subject of their own.
                async with self._one_at_a_time(invite.uid):
                    await self._handle_invite(invite, inbox_id, message_id, sender, thread="")
                return
        async with self._one_at_a_time(invite.uid):
            await self._handle_invite(invite, inbox_id, message_id, sender)

    async def _find_invite(self, inbox_id: str, message_id: str, msg: dict) -> tuple[Invite | None, bytes | None]:
        """The invite in an email, and the calendar data it came from."""
        try:
            for att in msg.get("attachments") or []:
                content_type = (att.get("content_type") or "").split(";")[0].strip().lower()
                if content_type in CALENDAR_TYPES or (att.get("filename") or "").lower().endswith(".ics"):
                    ics = await self.mail.get_attachment(inbox_id, message_id, att["attachment_id"])
                    invite = parse_ics(ics)
                    if invite:
                        return invite, ics
            raw_ics = ics_from_raw_email(await self.mail.get_raw(inbox_id, message_id))
            return (parse_ics(raw_ics), raw_ics) if raw_ics else (None, None)
        except ValueError:
            log.warning("unreadable calendar data in %s", message_id)
            return None, None

    def _attendees(self, invite: Invite, sender: str) -> list[str]:
        own = self.settings.agentmail_inbox_id.lower()
        people = [invite.organizer, sender, *invite.attendees]
        return list(dict.fromkeys(p for p in people if p and p != own))

    async def _handle_invite(
        self, invite: Invite, inbox_id: str, message_id: str, sender: str, thread: str | None = None
    ) -> None:
        """`thread` is the email the notes reply to, when not the invite itself ("" for a new email)."""

        async def reply(text: str) -> None:
            await self.mail.reply(inbox_id, message_id, text)

        notes_on = message_id if thread is None else thread

        existing = self.store.get(invite.uid)
        if existing and invite.sequence < existing.sequence:
            return  # an older version of the invite arriving late

        if invite.cancelled:
            if existing and await self._call_off(existing):
                await reply(f'Got it, "{invite.title}" is cancelled. I won\'t join.')
            return
        if invite.all_day:
            await reply(
                f'"{invite.title}" is an all-day event, so I don\'t know when to join. '
                "Give it a start time and I'll be there."
            )
            return
        if not invite.meeting_link:
            await reply(f"I couldn't find a Zoom, Google Meet or Teams link in \"{invite.title}\", so I can't join it.")
            return
        now = self.now()
        if invite.end and invite.end <= now:
            # A meeting moved into the past also calls off the bot sent for its old time.
            if existing:
                await self._call_off(existing)
            await reply(f'"{invite.title}" has already ended, so there\'s nothing for me to join.')
            return

        join_at = invite.start if invite.start and invite.start > now + timedelta(minutes=1) else None

        if existing and existing.status == "scheduled" and existing.bot_id:
            same_start = existing.starts_at == (invite.start.isoformat() if invite.start else None)
            if existing.meeting_link == invite.meeting_link and same_start:
                return  # the same invite sent again
            if join_at:
                await self.meetstream.reschedule(existing.bot_id, join_at, invite.meeting_link)
                self._save(
                    invite, inbox_id, notes_on, sender, join_at, existing.bot_id, existing.callback_token, "scheduled"
                )
            else:
                # Moved to a time that has already started: no bot to reschedule, send one now.
                await self.meetstream.cancel(existing.bot_id)
                await self._launch(invite, inbox_id, notes_on, sender, None)
            when = f"at {_when(join_at)}" if join_at else "now"
            await reply(f'Updated: I\'ll join "{invite.title}" {when}.')
            return
        if (
            existing
            and existing.status not in ("cancelled", "failed", "launch_failed")
            and invite.sequence <= existing.sequence
        ):
            return  # already joined or finished
        # Otherwise a new meeting, or one moved after its bot already joined: send a fresh bot.

        await self._launch(invite, inbox_id, notes_on, sender, join_at)
        if join_at:
            await reply(
                f'Thanks, I\'ll join "{invite.title}" at {_when(join_at)} and email the notes '
                "and action items to everyone on the invite afterwards."
            )
        else:
            await reply(f'Joining "{invite.title}" now. I\'ll email the notes and action items when it ends.')

    async def _call_off(self, meeting: Meeting) -> bool:
        """Stop the meeting's bot, wherever it has got to. False if there was nothing to stop."""
        if meeting.status in ("cancelled", "notes_sent", "failed", "no_transcript"):
            return False
        if meeting.bot_id and meeting.status == "scheduled":
            await self.meetstream.cancel(meeting.bot_id)
        elif meeting.bot_id and meeting.status in ("joining", "in_call"):
            await self.meetstream.remove(meeting.bot_id)
        self.store.set_status(meeting.uid, "cancelled")
        return True

    async def _launch(
        self, invite, inbox_id, message_id, sender, join_at: datetime | None, source: str = "email"
    ) -> None:
        # MeetStream does not sign per-bot callbacks, so the callback URL
        # carries a secret only this bot's events know. It is stored before
        # the bot exists: MeetStream posts the first event straight away.
        token = secrets.token_urlsafe(24)
        self._save(invite, inbox_id, message_id, sender, join_at, None, token, "creating", source)
        callback = (
            f"{self.settings.public_base_url.rstrip('/')}/webhooks/meetstream/{token}"
            if self.settings.meetstream_callbacks
            else None  # check_unfinished polls MeetStream instead
        )
        try:
            bot_id = await self.meetstream.create_bot(
                invite.meeting_link, callback, self.settings.bot_name, join_at, self.settings.transcript_provider
            )
        except Exception:
            # Not "failed": that means MeetStream's bot failed, which is never retried.
            self.store.set_status(invite.uid, "launch_failed")
            raise
        self._save(
            invite, inbox_id, message_id, sender, join_at, bot_id, token, "scheduled" if join_at else "joining", source
        )

    def _save(self, invite, inbox_id, message_id, sender, join_at, bot_id, token, status, source="email") -> None:
        self.store.save(
            Meeting(
                uid=invite.uid,
                sequence=invite.sequence,
                inbox_id=inbox_id,
                message_id=message_id,
                requester=sender,
                title=invite.title,
                meeting_link=invite.meeting_link,
                start_at=join_at.isoformat() if join_at else None,
                attendees=self._attendees(invite, sender),
                bot_id=bot_id,
                callback_token=token,
                status=status,
                updated_at="",
                starts_at=invite.start.isoformat() if invite.start else None,
                ends_at=invite.end.isoformat() if invite.end else None,
                timezone=invite.timezone,
                host=f"{invite.organizer_name} ({invite.organizer})"
                if invite.organizer_name and invite.organizer
                else invite.organizer or sender,
                source=source,
            )
        )

    # ---- MeetStream events --------------------------------------------

    async def handle_meetstream(self, token: str, payload: dict) -> None:
        meeting = self.store.by_token(token)
        if meeting is None or payload.get("bot_id") not in (None, meeting.bot_id):
            return
        event = payload.get("bot_event") or payload.get("event") or ""
        if event in IN_CALL_EVENTS:
            self.store.set_status(meeting.uid, "in_call")
        elif event in FAILED_EVENTS:
            self.store.set_status(meeting.uid, "failed")
            reason = payload.get("message") or event
            await self._tell_requester(meeting, f'I couldn\'t get into "{meeting.title}": {reason}')
        elif event == "transcription.processed":
            await self._send_notes(meeting)
        elif event == "transcription.failed":
            self.store.set_status(meeting.uid, "notes_failed")
            await self._tell_requester(
                meeting, f'I was in "{meeting.title}", but the transcript failed, so I have no notes to send.'
            )

    # ---- Google Calendar ------------------------------------------------

    async def sync_calendar(self) -> None:
        """Give every upcoming meeting on the connected calendars a bot, and keep
        it in step as meetings move or are cancelled. MeetStream keeps its copy
        of the calendar current; this reads that copy."""
        mine = self.store.calendars()
        if not mine:
            return  # nothing connected here; MeetStream answers 400 rather than an empty list
        for event in filter(None, map(parse_event, await self.meetstream.calendar_events())):
            # The MeetStream account may have other calendars connected (other
            # apps, other people); only ones connected through this app get bots.
            # Match the calendar, or failing that the account it belongs to.
            owner = mine.get(event.calendar_id or "") or (
                event.account_id if event.account_id in mine.values() else None
            )
            if owner is None:
                continue
            async with self._one_at_a_time(event.uid):
                try:
                    await self._sync_event(event, owner=owner)
                except Exception:
                    log.exception("scheduling calendar meeting %s failed", event.uid)

    async def _sync_event(self, event, owner: str) -> None:
        own = self.settings.agentmail_inbox_id.lower()
        if own in event.attendees:
            return  # the agent was invited by email; that path owns this meeting
        existing = self.store.get(event.uid)
        if existing and existing.source != "calendar":
            return
        tag = self.settings.calendar_tag.strip().lower()
        if tag and tag not in f"{event.title} {event.description}".lower():
            if existing:
                await self._call_off(existing)  # the tag was removed
            return
        if event.cancelled:
            if existing:
                await self._call_off(existing)
            return
        now = self.now()
        if not event.meeting_link or not event.start or (event.end and event.end <= now):
            return
        if event.start > now + CALENDAR_LOOKAHEAD:
            return
        invite = Invite(
            uid=event.uid,
            title=event.title,
            start=event.start,
            end=event.end,
            meeting_link=event.meeting_link,
            organizer=event.organizer,
            organizer_name=event.organizer_name,
            timezone=event.timezone,
            attendees=event.attendees,
        )
        # Problems are reported to the calendar's owner, who connected it, not
        # to an organiser who may never have asked for a notetaker.
        await self._keep_in_step(invite, existing, own, "", owner, "calendar")

    async def _keep_in_step(
        self, invite: Invite, existing: Meeting | None, inbox_id: str, message_id: str, requester: str, source: str
    ) -> None:
        """Give a meeting from a calendar or a series a bot, or move its bot to the meeting's new time or link."""
        assert invite.start and invite.meeting_link
        join_at = invite.start if invite.start > self.now() + timedelta(minutes=1) else None
        if existing and existing.status == "scheduled" and existing.bot_id:
            # Compare the meeting's own start, not the planned join time:
            # that one drops to "now" in the last minute before every meeting.
            if (existing.meeting_link, existing.starts_at) == (invite.meeting_link, invite.start.isoformat()):
                return
            if join_at:
                await self.meetstream.reschedule(existing.bot_id, join_at, invite.meeting_link)
                self._save(
                    invite,
                    inbox_id,
                    message_id,
                    requester,
                    join_at,
                    existing.bot_id,
                    existing.callback_token,
                    "scheduled",
                    source,
                )
            else:
                await self.meetstream.cancel(existing.bot_id)
                await self._launch(invite, inbox_id, message_id, requester, None, source)
        elif not existing or existing.status == "launch_failed":
            # A cancelled one stays cancelled: we called it off, or its owner did.
            # A bot MeetStream reported failed (say, the host denied it) isn't sent again.
            await self._launch(invite, inbox_id, message_id, requester, join_at, source)

    # ---- recurring meetings -------------------------------------------

    async def _handle_series(self, invite: Invite, ics: bytes, inbox_id: str, message_id: str, sender: str) -> None:
        """A recurring invite. The series is kept, and its next meeting always has a bot
        waiting (see sync_series); each meeting's notes reply on the invite's thread."""

        async def reply(text: str) -> None:
            await self.mail.reply(inbox_id, message_id, text)

        series = self.store.get_series(invite.uid)
        if invite.recurrence_id:
            # A change to one meeting of the series
            if series is None or series.status != "active":
                return  # not a series we're following
            when = _when(invite.recurrence_id if invite.cancelled else invite.start)
            series.ics = merge_change(series.ics, ics)
            series.inbox_id, series.message_id = inbox_id, message_id
            self.store.save_series(series)
            await self._step_series(series)
            if invite.cancelled:
                await reply(f'Got it, I\'ll skip "{invite.title}" on {when}.')
            else:
                await reply(f'Updated: I\'ll join "{invite.title}" on {when}.')
            return

        if series and invite.sequence < series.sequence:
            return  # an older version of the invite arriving late
        if invite.cancelled:
            if series and series.status == "active":
                series.status = "cancelled"
                self.store.save_series(series)
                for meeting in self.store.meetings_of(series.uid):
                    await self._call_off(meeting)
                await reply(f'Got it, "{invite.title}" is cancelled. I won\'t join any more of them.')
            return
        if invite.all_day:
            await reply(
                f'"{invite.title}" is an all-day event, so I don\'t know when to join. '
                "Give it a start time and I'll be there."
            )
            return
        if not invite.meeting_link:
            await reply(f"I couldn't find a Zoom, Google Meet or Teams link in \"{invite.title}\", so I can't join it.")
            return
        upcoming = next_occurrence(ics, self.now())
        if upcoming is None:
            await reply(f'"{invite.title}" has no meetings left, so there\'s nothing for me to join.')
            return
        again = series is not None and series.status == "active"
        if series and again and invite.sequence == series.sequence and series.ics == ics:
            return  # the same invite sent again
        series = Series(invite.uid, invite.sequence, inbox_id, message_id, sender, ics, "active")
        self.store.save_series(series)
        await self._step_series(series)
        if again:
            await reply(f'Updated: I\'ll join every "{invite.title}", next on {_when(upcoming.start)}.')
        else:
            await reply(
                f'Thanks, I\'ll join every "{invite.title}", starting {_when(upcoming.start)}, and email '
                "the notes and action items to everyone on the invite after each one."
            )

    async def sync_series(self) -> None:
        """Once a meeting of a series is over, give the next one its bot."""
        for series in self.store.active_series():
            async with self._one_at_a_time(series.uid):
                try:
                    await self._step_series(series)
                except Exception:
                    log.exception("scheduling the next meeting of %s failed", series.uid)

    async def _step_series(self, series: Series) -> None:
        upcoming = next_occurrence(series.ics, self.now())
        uid = occurrence_uid(series.uid, upcoming.recurrence_id) if upcoming and upcoming.recurrence_id else None
        # Only the next meeting has a bot waiting. One waiting for a later
        # meeting was left behind by a change to the series. (A meeting that
        # has started is left alone: its bot may be in the call.)
        now = self.now()
        for meeting in self.store.meetings_of(series.uid):
            later = meeting.starts_at is not None and datetime.fromisoformat(meeting.starts_at) > now
            if meeting.uid != uid and later and meeting.status in ("scheduled", "creating", "launch_failed"):
                await self._call_off(meeting)
        if upcoming is None or uid is None:
            series.status = "ended"
            self.store.save_series(series)
            return
        # A meeting first invited as a one-off, then made recurring: its bot
        # still covers the meeting it was sent to, and no other.
        one_off = self.store.get(series.uid)
        if one_off and one_off.status not in ("cancelled", "failed", "launch_failed"):
            if one_off.starts_at == (upcoming.start.isoformat() if upcoming.start else None):
                return
            if one_off.status == "scheduled":
                await self._call_off(one_off)
        if upcoming.meeting_link:
            await self._keep_in_step(
                replace(upcoming, uid=uid),
                self.store.get(uid),
                series.inbox_id,
                series.message_id,
                series.requester,
                "email",
            )

    # ---- the safety net -----------------------------------------------

    async def check_unfinished(self) -> None:
        """Webhooks can go missing. Ask MeetStream directly about every bot that
        should have finished, so no meeting is left without an answer."""
        for meeting in self.store.unfinished():
            try:
                await self._check(meeting)
            except Exception:
                log.exception("checking bot %s failed", meeting.bot_id)

    async def _check(self, meeting: Meeting) -> None:
        if not meeting.bot_id:
            return  # unfinished() only returns meetings with a bot
        status = await self.meetstream.status(meeting.bot_id)
        if status in BOT_FAILED:
            self.store.set_status(meeting.uid, "failed")
            await self._tell_requester(meeting, f'I couldn\'t get into "{meeting.title}" (MeetStream: {status}).')
            return
        if status not in BOT_FINISHED:
            return
        transcript = await self.meetstream.transcript_status(meeting.bot_id)
        if transcript == "ready":
            await self._send_notes(meeting)
        elif meeting.status != "awaiting_transcript":
            self.store.set_status(meeting.uid, "awaiting_transcript")  # starts the wait below
        elif transcript == "failed" or self.now() - datetime.fromisoformat(meeting.updated_at) > TRANSCRIPT_WAIT:
            self.store.set_status(meeting.uid, "no_transcript")
            await self._tell_requester(
                meeting,
                f'I was in "{meeting.title}", but MeetStream didn\'t produce a transcript, so I have no notes to send.',
            )

    async def _tell_requester(self, meeting: Meeting, text: str) -> None:
        await self._email(meeting, [meeting.requester], f'About "{meeting.title}"', text)

    async def _email(self, meeting: Meeting, to: list[str], subject: str, text: str, html: str | None = None) -> None:
        """Reply on the invite's thread; a calendar meeting has none, so it gets a new email."""
        if meeting.message_id:
            await self.mail.reply(meeting.inbox_id, meeting.message_id, text, html, to=to)
        else:
            await self.mail.send(meeting.inbox_id, to, subject, text, html)

    async def _send_notes(self, meeting: Meeting) -> None:
        if not self.store.claim_for_notes(meeting.uid):
            return
        try:
            transcript_id = await self.meetstream.transcript_id(meeting.bot_id) if meeting.bot_id else None
            segments = await self.meetstream.transcript(transcript_id) if transcript_id else []
            if not segments:
                self.store.set_status(meeting.uid, "no_transcript")
                await self._tell_requester(
                    meeting, f'I was in "{meeting.title}", but nobody was transcribed, so there are no notes.'
                )
                return
            details = meeting_details(meeting, [s["speaker"] for s in segments], self.settings.bot_name)
            text, html = render(details, await self.notes.write(meeting.title, segments))
            await self._email(meeting, meeting.attendees, f"Notes: {meeting.title}", text, html)
            self.store.set_status(meeting.uid, "notes_sent")
        except Exception:
            # notes_failed is picked up again by check_unfinished; after a few
            # tries the requester hears about it instead of hearing nothing.
            if self.store.note_failure(meeting.uid) >= NOTES_MAX_ATTEMPTS:
                self.store.set_status(meeting.uid, "notes_abandoned")
                try:
                    await self._tell_requester(
                        meeting,
                        f'I was in "{meeting.title}", but couldn\'t write the notes, so there are none to send.',
                    )
                except Exception:
                    log.exception("telling %s about %s failed", meeting.requester, meeting.uid)
            raise
