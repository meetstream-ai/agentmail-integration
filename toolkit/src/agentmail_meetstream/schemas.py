from typing import Annotated

from pydantic import BaseModel, Field

BotIdField = Annotated[str, Field(description="Bot ID, from join_meeting, join_meeting_from_email or list_meetings")]


# ---- tool inputs -------------------------------------------------------------


class JoinMeetingFromEmailParams(BaseModel):
    inbox_id: str = Field(description="Inbox ID: the inbox's email address, as in AgentMail's tools")
    message_id: str = Field(description="Message ID of the invite email, with its angle brackets")


class JoinMeetingParams(BaseModel):
    meeting_link: str = Field(description="Zoom, Google Meet or Microsoft Teams join link")
    join_at: str | None = Field(
        default=None,
        description="When to join, ISO 8601 UTC ending in Z, e.g. 2026-04-01T09:00:00Z; omit to join now",
    )
    title: str | None = Field(default=None, description="Meeting name, kept with the bot for later")


class BotParams(BaseModel):
    bot_id: BotIdField


class ListMeetingsParams(BaseModel):
    limit: int | None = Field(default=10, description="How many recent meetings to return")


# ---- tool results ------------------------------------------------------------


class MeetingRef(BaseModel):
    """What the agent needs to follow a meeting up by email later."""

    bot_id: str
    title: str | None = None
    # The invite this meeting came from, so the notes can be a reply on its thread
    inbox_id: str | None = None
    message_id: str | None = None
    thread_id: str | None = None
    # For the notes header
    start: str | None = Field(default=None, description="Start time, ISO 8601 UTC")
    end: str | None = Field(default=None, description="End time, ISO 8601 UTC")
    timezone: str | None = Field(default=None, description="The organiser's time zone, to show the times in")
    organizer: str | None = None
    attendees: list[str] = Field(default_factory=list, description="Everyone on the invite, to email the notes to")
    recurring: bool | None = Field(
        default=None,
        description="One meeting of a recurring series. Once it's over, call join_meeting_from_email "
        "again with the same message_id to schedule the next one.",
    )


class JoinResult(MeetingRef):
    status: str = Field(description="Scheduled, or Joining for a meeting that is already under way")
    meeting_link: str
    join_at: str | None = None


class MeetingStatus(MeetingRef):
    status: str
    finished: bool = Field(description="The bot has left the call")
    transcript_ready: bool
    transcript: str = Field(
        description='"ready"; "pending" while MeetStream works on it; or "unavailable" when it never will be '
        "(nothing was heard, or transcription failed). Unavailable means there are no notes to write."
    )


class Transcript(MeetingRef):
    ready: bool
    transcript: str = Field(description='"Speaker: words" lines in speaking order; empty until ready')
    participants: list[str] = Field(
        default_factory=list, description="Who spoke in the call, in order of first speaking"
    )


class Meeting(BaseModel):
    bot_id: str
    title: str | None = None
    status: str | None = None
    platform: str | None = None
    created_at: str | None = None


class MeetingList(BaseModel):
    meetings: list[Meeting]
