"""Invite parsing lives in the toolkit package; the app uses the same code."""

from agentmail_meetstream.invites import (  # noqa: F401
    CALENDAR_TYPES,
    PLATFORMS,
    Invite,
    find_meeting_link,
    ics_from_raw_email,
    merge_change,
    next_occurrence,
    occurrence_uid,
    parse_ics,
    platform_of,
)
