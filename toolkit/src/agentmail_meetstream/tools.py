from collections.abc import Callable

from pydantic import BaseModel

from .functions import (
    get_meeting_status,
    get_meeting_transcript,
    join_meeting,
    join_meeting_from_email,
    leave_meeting,
    list_meetings,
)
from .schemas import BotParams, JoinMeetingFromEmailParams, JoinMeetingParams, ListMeetingsParams


class Tool(BaseModel):
    name: str
    description: str
    params_schema: type[BaseModel]
    func: Callable


tools: list[Tool] = [
    Tool(
        name="join_meeting_from_email",
        description=(
            "Send a notetaker bot into the meeting a calendar invite email is for. Reads the invite in an "
            "AgentMail inbox (Google, Outlook or Zoom) or a meeting link in the email body, and schedules the "
            "bot for the start time, or sends it now if the meeting has begun. Returns the bot_id, the meeting's "
            "times and attendees, and the thread_id to reply on with the notes later."
        ),
        params_schema=JoinMeetingFromEmailParams,
        func=join_meeting_from_email,
    ),
    Tool(
        name="join_meeting",
        description=(
            "Send a notetaker bot to a Zoom, Google Meet or Microsoft Teams link, now or at a given time. "
            "Returns the bot_id."
        ),
        params_schema=JoinMeetingParams,
        func=join_meeting,
    ),
    Tool(
        name="get_meeting_status",
        description=(
            "Where a meeting's bot is: Scheduled, in the waiting room, in the call, or finished, and whether "
            "its transcript is ready. Also returns the invite thread it came from, if any."
        ),
        params_schema=BotParams,
        func=get_meeting_status,
    ),
    Tool(
        name="get_meeting_transcript",
        description=(
            "The meeting's transcript as 'Speaker: words' lines, once ready (ready is false until then). "
            "Use it to write the meeting notes."
        ),
        params_schema=BotParams,
        func=get_meeting_transcript,
    ),
    Tool(
        name="leave_meeting",
        description="Call a bot off: cancel it if it is scheduled, or take it out of the call if it has joined.",
        params_schema=BotParams,
        func=leave_meeting,
    ),
    Tool(
        name="list_meetings",
        description="Recent meetings the notetaker was sent to, newest first, with their bot_id and status.",
        params_schema=ListMeetingsParams,
        func=list_meetings,
    ),
]
