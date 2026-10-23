"""What each tool does. Every function takes the clients and the tool's arguments."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parseaddr
from typing import Any

import httpx
from agentmail import AgentMail

from .invites import CALENDAR_TYPES, Invite, find_meeting_link, ics_from_raw_email, occurrences, parse_ics
from .meetstream import MeetStream, is_named
from .schemas import JoinResult, Meeting, MeetingList, MeetingRef, MeetingStatus, Transcript

Kwargs = dict[str, Any]

FINISHED = {
    "Stopped",
    "MediaProcessing",
    "AudioProcessing",
    "VideoProcessing",
    "TranscriptionReady",
    "Done",
    "MediaExpired",
    "Error",
    "BotRejected",
    "BotNotAllowed",
    "NotAllowed",
    "Denied",
    "Failed",
}


@dataclass
class Clients:
    agentmail: AgentMail
    meetstream: MeetStream
    bot_name: str = "AgentMail Notetaker"
    # Who may send the bot into a meeting by email: addresses or "@domain".
    # Empty means nobody. Every bot records other people and spends MeetStream
    # minutes, so access is granted explicitly, never by default.
    allowed_senders: list[str] = field(default_factory=list)
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    http: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=30))

    def download(self, url: str) -> bytes:
        # AgentMail hands out signed https URLs; refuse anything else.
        if not url.startswith("https://"):
            raise ValueError("refusing a non-https download URL")
        resp = self.http.get(url, follow_redirects=False)
        resp.raise_for_status()
        return resp.content

    def sender_allowed(self, address: str) -> bool:
        address = address.strip().lower()
        if not address:
            return False
        return any(
            address == e.lower() or (e.startswith("@") and address.endswith(e.lower())) for e in self.allowed_senders
        )


# ---- join_meeting_from_email -------------------------------------------------


def _read_invite(clients: Clients, inbox_id: str, message_id: str, message) -> tuple[Invite | None, bytes]:
    """The invite in an email, and the calendar data it came from."""
    for att in message.attachments or []:
        content_type = (att.content_type or "").split(";")[0].strip().lower()
        if content_type in CALENDAR_TYPES or (att.filename or "").lower().endswith(".ics"):
            found = clients.agentmail.inboxes.messages.get_attachment(inbox_id, message_id, att.attachment_id)
            ics = clients.download(found.download_url)
            invite = parse_ics(ics)
            if invite:
                return invite, ics
    # Google and Outlook often send the invite inline, not as a named attachment.
    raw = clients.agentmail.inboxes.messages.get_raw(inbox_id, message_id)
    ics = ics_from_raw_email(clients.download(raw.download_url)) or b""
    return (parse_ics(ics) if ics else None), ics


def join_meeting_from_email(clients: Clients, kwargs: Kwargs) -> JoinResult:
    inbox_id, message_id = kwargs["inbox_id"], kwargs["message_id"]
    message = clients.agentmail.inboxes.messages.get(inbox_id, message_id)
    sender = parseaddr(message.from_ or "")[1].lower()
    if not clients.sender_allowed(sender):
        raise PermissionError(f"{sender} is not allowed to send the notetaker into meetings")

    invite, ics = _read_invite(clients, inbox_id, message_id, message)
    if invite is None:
        link = find_meeting_link(message.text, message.html)
        if not link:
            raise ValueError("No calendar invite or Zoom, Google Meet or Teams link in this email")
        recipients = [a.lower() for _, a in getaddresses(list(message.to or []) + list(message.cc or [])) if a]
        invite = Invite(
            uid=message_id,
            title=message.subject or "Meeting",
            meeting_link=link,
            organizer=sender,
            attendees=recipients,
        )
    if invite.cancelled:
        raise ValueError(f'"{invite.title}" is a cancellation; call leave_meeting with the bot_id scheduled for it')
    if invite.all_day:
        raise ValueError(f'"{invite.title}" is an all-day event, so there is no time to join at')
    now = clients.now()
    # Bots already sent for this invite, newest first
    bots = [b for b in clients.meetstream.list_bots(50) if (b.get("custom_attributes") or {}).get("uid") == invite.uid]
    # Meetings that already have a bot in the call, or had one
    taken = {(b.get("custom_attributes") or {}).get("occurrence") for b in bots if b.get("status") != "Scheduled"}
    series = invite.recurring and not invite.recurrence_id
    if series:
        # Join the series' next meeting. The agent calls this again with the
        # same message once that meeting is over, for the one after.
        meeting = next((m for m in occurrences(ics, now) if _occurrence(m) not in taken), None)
        if meeting is None:
            raise ValueError(f'"{invite.title}" has no meetings left')
    else:
        meeting = invite
        if _occurrence(meeting) in taken:
            raise ValueError(f'A bot has already joined "{invite.title}"')
    if not meeting.meeting_link:
        raise ValueError(f'"{invite.title}" has no Zoom, Google Meet or Teams link')
    if meeting.end and meeting.end <= now:
        raise ValueError(f'"{invite.title}" has already ended')

    join_at = meeting.start if meeting.start and meeting.start > now + timedelta(minutes=1) else None
    own = inbox_id.lower()
    people = dict.fromkeys(p for p in [meeting.organizer, sender, *meeting.attendees] if p and p != own)
    # Kept with the bot, so get_meeting_status and get_meeting_transcript can
    # hand back everything the notes need: the thread to reply on and the header.
    ref = {
        "title": meeting.title,
        "inbox_id": inbox_id,
        "message_id": message_id,
        "thread_id": message.thread_id,
        "start": meeting.start.isoformat() if meeting.start else None,
        "end": meeting.end.isoformat() if meeting.end else None,
        "timezone": meeting.timezone,
        "organizer": meeting.organizer,
        "attendees": list(people),
        "recurring": bool(invite.recurring or invite.recurrence_id),
    }
    result = {"meeting_link": meeting.meeting_link, "join_at": join_at.isoformat() if join_at else None, **ref}
    # A newer invite replaces the bot waiting for its meeting, or for the
    # series' next meeting. A change to one meeting leaves the others' bots alone.
    for waiting in (b for b in bots if b.get("status") == "Scheduled"):
        attributes = waiting.get("custom_attributes") or {}
        if not series and attributes.get("occurrence") != _occurrence(meeting):
            continue
        same = (attributes.get("occurrence"), attributes.get("start"), waiting.get("meeting_url"))
        if same == (_occurrence(meeting), ref["start"], meeting.meeting_link):
            return JoinResult(bot_id=waiting["bot_id"], status="Scheduled", **result)  # already covered
        clients.meetstream.cancel_scheduled(waiting["bot_id"])
    # Which meeting the bot is for: the invite's UID and, in a series, the meeting's original start
    key = {"uid": invite.uid, "occurrence": _occurrence(meeting)}
    bot = clients.meetstream.create_bot(
        meeting.meeting_link, clients.bot_name, join_at, custom_attributes={**ref, **key}
    )
    return JoinResult(
        bot_id=bot["bot_id"], status=bot.get("status") or ("Scheduled" if join_at else "Joining"), **result
    )


def _occurrence(meeting: Invite) -> str | None:
    return meeting.recurrence_id.isoformat() if meeting.recurrence_id else None


# ---- the others ----------------------------------------------------------------


def join_meeting(clients: Clients, kwargs: Kwargs) -> JoinResult:
    link = find_meeting_link(kwargs["meeting_link"])
    if not link:
        raise ValueError("Not a Zoom, Google Meet or Microsoft Teams join link")
    join_at = None
    if kwargs.get("join_at"):
        join_at = datetime.fromisoformat(kwargs["join_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
        if join_at <= clients.now() + timedelta(minutes=1):
            join_at = None  # already due: join now
    title = kwargs.get("title")
    bot = clients.meetstream.create_bot(
        link, clients.bot_name, join_at, custom_attributes={"title": title} if title else None
    )
    return JoinResult(
        bot_id=bot["bot_id"],
        status=bot.get("status") or ("Scheduled" if join_at else "Joining"),
        meeting_link=link,
        join_at=join_at.isoformat() if join_at else None,
        title=title,
    )


def _ref(bot_id: str, detail: dict) -> dict:
    attributes = detail.get("custom_attributes") or {}
    kept = {k: attributes[k] for k in MeetingRef.model_fields if k != "bot_id" and attributes.get(k) is not None}
    return {"bot_id": bot_id, **kept}


def _run(runs: list[dict]) -> dict:
    # The recorded transcript, not Meet's own captions (which have no transcript_id)
    return next((r for r in runs if r.get("transcript_id")), {})


def _transcript_id(runs: list[dict]) -> str | None:
    run = _run(runs)
    return run["transcript_id"] if (run.get("status") or "").lower() == "success" else None


def _transcript_state(runs: list[dict], bot_status: str) -> str:
    state = (_run(runs).get("status") or "").lower()
    if state == "success":
        return "ready"
    if state in ("failed", "error", "skipped") or (bot_status in ("Done", "MediaExpired") and not state):
        return "unavailable"
    return "pending"


def get_meeting_status(clients: Clients, kwargs: Kwargs) -> MeetingStatus:
    bot_id = kwargs["bot_id"]
    detail = clients.meetstream.detail(bot_id)
    status = detail.get("Status") or "Unknown"
    state = _transcript_state(clients.meetstream.transcript_runs(bot_id), status)
    return MeetingStatus(
        **_ref(bot_id, detail),
        status=status,
        finished=status in FINISHED,
        transcript_ready=state == "ready",
        transcript=state,
    )


def get_meeting_transcript(clients: Clients, kwargs: Kwargs) -> Transcript:
    bot_id = kwargs["bot_id"]
    detail = clients.meetstream.detail(bot_id)
    transcript_id = _transcript_id(clients.meetstream.transcript_runs(bot_id))
    lines = clients.meetstream.transcript(transcript_id) if transcript_id else []
    return Transcript(
        **_ref(bot_id, detail),
        ready=transcript_id is not None,
        transcript="\n".join(f"{s['speaker']}: {s['text']}" for s in lines),
        participants=list(
            dict.fromkeys(s["speaker"] for s in lines if is_named(s["speaker"]) and s["speaker"] != clients.bot_name)
        ),
    )


def leave_meeting(clients: Clients, kwargs: Kwargs) -> None:
    bot_id = kwargs["bot_id"]
    if clients.meetstream.detail(bot_id).get("Status") == "Scheduled":
        clients.meetstream.cancel_scheduled(bot_id)
    else:
        clients.meetstream.remove(bot_id)


def list_meetings(clients: Clients, kwargs: Kwargs) -> MeetingList:
    bots = clients.meetstream.list_bots(kwargs.get("limit") or 10)
    return MeetingList(
        meetings=[
            Meeting(
                bot_id=b["bot_id"],
                title=(b.get("custom_attributes") or {}).get("title"),
                status=b.get("status"),
                platform=b.get("platform"),
                created_at=b.get("created_at"),
            )
            for b in bots
        ]
    )
