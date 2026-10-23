"""SQLite: which invite maps to which bot, and which email thread to answer on."""

import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# A meeting left in "writing_notes" this long was interrupted (the server
# stopped mid-write); it becomes claimable again.
STALE_WRITE = timedelta(minutes=10)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    uid            TEXT PRIMARY KEY,   -- the invite's iCalendar UID
    sequence       INTEGER NOT NULL,
    inbox_id       TEXT NOT NULL,
    message_id     TEXT NOT NULL,      -- the email we reply on
    requester      TEXT NOT NULL,
    title          TEXT NOT NULL,
    meeting_link   TEXT NOT NULL,
    start_at       TEXT,
    attendees      TEXT NOT NULL,      -- JSON list of addresses
    bot_id         TEXT,
    callback_token TEXT NOT NULL UNIQUE,
    status         TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    starts_at      TEXT,
    ends_at        TEXT,
    timezone       TEXT,
    host           TEXT,
    source         TEXT DEFAULT 'email',
    notes_attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS calendars (
    calendar_id TEXT PRIMARY KEY,      -- MeetStream's id for a calendar connected here
    account     TEXT NOT NULL,         -- the Google account it belongs to
    connected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS series (
    uid        TEXT PRIMARY KEY,       -- the recurring invite's iCalendar UID
    sequence   INTEGER NOT NULL,
    inbox_id   TEXT NOT NULL,
    message_id TEXT NOT NULL,          -- the latest invite email: notes reply on its thread
    requester  TEXT NOT NULL,
    ics        BLOB NOT NULL,          -- the series, with changes to single meetings applied
    status     TEXT NOT NULL,          -- active, cancelled or ended
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_events (
    event_id TEXT PRIMARY KEY,
    seen_at  TEXT NOT NULL
);
"""


@dataclass
class Meeting:
    uid: str
    sequence: int
    inbox_id: str
    message_id: str
    requester: str
    title: str
    meeting_link: str
    start_at: str | None
    attendees: list[str]
    bot_id: str | None
    callback_token: str
    status: str
    updated_at: str
    # For the notes header
    starts_at: str | None = None
    ends_at: str | None = None
    timezone: str | None = None
    host: str | None = None
    source: str | None = "email"  # "email" (an invite) or "calendar" (Google Calendar sync)
    notes_attempts: int = 0  # failed tries at writing the notes


@dataclass
class Series:
    """A recurring invite. Each of its meetings is a Meeting whose uid is
    occurrence_uid(series uid, that meeting's original start)."""

    uid: str
    sequence: int
    inbox_id: str
    message_id: str
    requester: str
    ics: bytes
    status: str
    updated_at: str = ""


class Store:
    def __init__(self, path: str, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._clock = now
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            # Columns added after the first release; older databases gain them here.
            have = {r[1] for r in self._db.execute("PRAGMA table_info(meetings)")}
            added = {
                "starts_at": "TEXT",
                "ends_at": "TEXT",
                "timezone": "TEXT",
                "host": "TEXT",
                "source": "TEXT DEFAULT 'email'",
                "notes_attempts": "INTEGER NOT NULL DEFAULT 0",
            }
            for column, kind in added.items():
                if column not in have:
                    self._db.execute(f"ALTER TABLE meetings ADD COLUMN {column} {kind}")

    def _now(self) -> str:
        return self._clock().isoformat()

    def _stale(self) -> str:
        return (self._clock() - STALE_WRITE).isoformat()

    def _row(self, row) -> Meeting | None:
        if row is None:
            return None
        data = dict(row)
        data["attendees"] = json.loads(data["attendees"])
        return Meeting(**data)

    def first_time_seen(self, event_id: str) -> bool:
        """Webhooks are delivered at least once; True only for the first delivery."""
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO seen_events (event_id, seen_at) VALUES (?, ?)",
                (event_id, self._now()),
            )
            return cur.rowcount == 1

    def forget_event(self, event_id: str) -> None:
        """Let a delivery be processed again, after handling it failed."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM seen_events WHERE event_id = ?", (event_id,))

    def get(self, uid: str) -> Meeting | None:
        with self._lock:
            return self._row(self._db.execute("SELECT * FROM meetings WHERE uid = ?", (uid,)).fetchone())

    def by_token(self, token: str) -> Meeting | None:
        with self._lock:
            return self._row(self._db.execute("SELECT * FROM meetings WHERE callback_token = ?", (token,)).fetchone())

    def save(self, meeting: Meeting) -> None:
        meeting.updated_at = self._now()
        data = {**meeting.__dict__, "attendees": json.dumps(meeting.attendees)}
        columns = ", ".join(data)
        placeholders = ", ".join(f":{c}" for c in data)
        with self._lock, self._db:
            self._db.execute(f"INSERT OR REPLACE INTO meetings ({columns}) VALUES ({placeholders})", data)

    def set_status(self, uid: str, status: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE meetings SET status = ?, updated_at = ? WHERE uid = ?", (status, self._now(), uid))

    def unfinished(self) -> list[Meeting]:
        """Meetings whose bot may have finished without MeetStream telling us."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM meetings WHERE bot_id IS NOT NULL AND ("
                "status IN ('joining', 'in_call', 'awaiting_transcript', 'notes_failed') "
                "OR (status = 'scheduled' AND start_at <= ?) "
                "OR (status = 'writing_notes' AND updated_at < ?))",
                (self._now(), self._stale()),
            ).fetchall()
        return [m for m in map(self._row, rows) if m is not None]

    def add_calendar(self, calendar_id: str, account: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO calendars (calendar_id, account, connected_at) VALUES (?, ?, ?)",
                (calendar_id, account, self._now()),
            )

    def calendars(self) -> dict[str, str]:
        """calendar id -> Google account, for calendars connected through this app."""
        with self._lock:
            return dict(self._db.execute("SELECT calendar_id, account FROM calendars").fetchall())

    def save_series(self, series: Series) -> None:
        series.updated_at = self._now()
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO series (uid, sequence, inbox_id, message_id, requester, ics, status, updated_at) "
                "VALUES (:uid, :sequence, :inbox_id, :message_id, :requester, :ics, :status, :updated_at)",
                series.__dict__,
            )

    def get_series(self, uid: str) -> Series | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM series WHERE uid = ?", (uid,)).fetchone()
        return Series(**dict(row)) if row else None

    def active_series(self) -> list[Series]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM series WHERE status = 'active'").fetchall()
        return [Series(**dict(r)) for r in rows]

    def meetings_of(self, series_uid: str) -> list[Meeting]:
        """The meetings of a series that have had a bot, or have one coming."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM meetings WHERE substr(uid, 1, ?) = ?", (len(series_uid) + 1, series_uid + "#")
            ).fetchall()
        return [m for m in map(self._row, rows) if m is not None]

    def note_failure(self, uid: str) -> int:
        """Record a failed try at the notes; returns how many there have been."""
        with self._lock, self._db:
            self._db.execute(
                "UPDATE meetings SET notes_attempts = notes_attempts + 1, status = 'notes_failed', updated_at = ? "
                "WHERE uid = ?",
                (self._now(), uid),
            )
            row = self._db.execute("SELECT notes_attempts FROM meetings WHERE uid = ?", (uid,)).fetchone()
        return row[0] if row else 0

    def claim_for_notes(self, uid: str) -> bool:
        """Exactly one transcript event gets to write and send the notes."""
        with self._lock, self._db:
            cur = self._db.execute(
                "UPDATE meetings SET status = 'writing_notes', updated_at = ? WHERE uid = ? AND ("
                "status NOT IN ('writing_notes', 'notes_sent', 'notes_abandoned', 'cancelled') "
                "OR (status = 'writing_notes' AND updated_at < ?))",
                (self._now(), uid, self._stale()),
            )
            return cur.rowcount == 1
