"""The MeetStream API calls the toolkit needs, synchronous like the AgentMail SDK."""

import os
import re
from datetime import datetime
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.meetstream.ai/api/v1"

# Settings proven in the reference notetaker: keep the audio long enough to
# transcribe (without a retention policy there is nothing to transcribe), use
# MeetStream's own transcription, leave a minute after everyone else does, and
# record audio only (MeetStream records video unless told not to).
DEFAULT_BOT = {
    "recording_config": {
        "retention": {"type": "timed", "hours": 24},
        "transcript": {"provider": {"meetstream": {"language": "auto"}}},
    },
    "automatic_leave": {"everyone_left_timeout": 60},
    "video_required": False,
}


def is_named(speaker: str) -> bool:
    """A speaker label that is a person's name, not a placeholder like
    "Unknown" or the "SPEAKER_00" a transcript uses when it has no names."""
    label = speaker.strip()
    return bool(label) and label.lower() != "unknown" and not re.fullmatch(r"speaker[ _]?\d+", label, re.IGNORECASE)


class MeetStream:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, http: httpx.Client | None = None):
        api_key = api_key or os.environ.get("MEETSTREAM_API_KEY")
        if not api_key:
            raise ValueError("Pass api_key or set MEETSTREAM_API_KEY")
        self._base = (base_url or os.environ.get("MEETSTREAM_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._headers = {"Authorization": f"Token {api_key}"}
        self._http = http or httpx.Client(timeout=30)

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs) -> Any:
        resp = self._http.request(method, f"{self._base}{path}", headers=self._headers, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def create_bot(
        self, meeting_link: str, bot_name: str, join_at: datetime | None = None, custom_attributes: dict | None = None
    ) -> dict:
        body: dict = {"meeting_link": meeting_link, "bot_name": bot_name, **DEFAULT_BOT}
        if join_at:
            body["join_at"] = join_at.isoformat()
        if custom_attributes:
            body["custom_attributes"] = custom_attributes
        return self._request("POST", "/bots/create_bot", json=body)

    def detail(self, bot_id: str) -> dict:
        return self._request("GET", f"/bots/{bot_id}/detail").get("bot_details") or {}

    def transcript_runs(self, bot_id: str) -> list[dict]:
        return self._request("GET", f"/bots/{bot_id}/transcriptions").get("transcriptions") or []

    def transcript(self, transcript_id: str) -> list[dict]:
        """[{"speaker": ..., "text": ...}] in speaking order."""
        items = self._request("GET", f"/transcript/{transcript_id}/get_transcript")
        segments = []
        for item in items if isinstance(items, list) else []:
            text = (item.get("transcript") or "").strip() or " ".join(
                w.get("punctuated_word") or w.get("word") or w.get("text") or "" for w in item.get("words") or []
            ).strip()
            if text:
                speaker = item.get("speaker") or (item.get("participant") or {}).get("name") or "Unknown"
                segments.append({"speaker": speaker, "text": text})
        return segments

    def cancel_scheduled(self, bot_id: str) -> None:
        self._request("DELETE", f"/calendar/scheduled_bots/{bot_id}")

    def remove(self, bot_id: str) -> None:
        self._request("GET", f"/bots/{bot_id}/remove_bot")

    def list_bots(self, limit: int) -> list[dict]:
        return self._request("GET", "/bots", params={"limit": limit}).get("bots") or []
