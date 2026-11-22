"""The parts of the MeetStream API the agent uses: schedule a bot, read its transcript."""

from datetime import datetime

import httpx

# What MeetStream should transcribe the call with. "meetstream" needs no other
# account; "deepgram" needs Deepgram connected to your MeetStream account.
TRANSCRIPT_PROVIDERS = {
    "meetstream": {"meetstream": {"language": "auto"}},
    "deepgram": {"deepgram": {"model": "nova-3", "language": "en", "punctuate": True, "smart_format": True}},
}


class MeetStream:
    def __init__(self, api_key: str, base_url: str, http: httpx.AsyncClient):
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Token {api_key}"}
        self._http = http

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        resp = await self._http.request(method, f"{self._base}{path}", headers=self._headers, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def create_bot(
        self,
        meeting_link: str,
        callback_url: str | None,
        bot_name: str,
        join_at: datetime | None = None,
        transcript_provider: str = "meetstream",
    ) -> str:
        """Send a bot now, or schedule it with join_at (MeetStream dispatches it
        about two minutes early). Returns the bot id."""
        body = {
            "meeting_link": meeting_link,
            "bot_name": bot_name,
            "recording_config": {
                # Without a retention policy MeetStream keeps no audio, so there
                # is nothing to transcribe. A day is enough to write the notes.
                "retention": {"type": "timed", "hours": 24},
                "transcript": {"provider": TRANSCRIPT_PROVIDERS[transcript_provider]},
            },
            # Leave a minute after the last person does (the default is five),
            # so the notes follow the end of the call quickly.
            "automatic_leave": {"everyone_left_timeout": 60},
            # The notes need only audio, and MeetStream records video unless told not to.
            "video_required": False,
        }
        if callback_url:
            body["callback_url"] = callback_url
        if join_at:
            body["join_at"] = join_at.isoformat()
        return (await self._request("POST", "/bots/create_bot", json=body))["bot_id"]

    async def reschedule(self, bot_id: str, join_at: datetime, meeting_link: str) -> None:
        await self._request(
            "PATCH",
            f"/calendar/scheduled_bots/{bot_id}",
            json={"scheduled_join_time": join_at.isoformat(), "meeting_link": meeting_link},
        )

    async def cancel(self, bot_id: str) -> None:
        await self._request("DELETE", f"/calendar/scheduled_bots/{bot_id}")

    async def remove(self, bot_id: str) -> None:
        """Take a bot that has already joined out of the call."""
        await self._request("GET", f"/bots/{bot_id}/remove_bot")

    async def connect_calendar(self, client_id: str, client_secret: str, refresh_token: str) -> dict:
        return await self._request(
            "POST",
            "/calendar/create_calendar",
            json={
                "google_client_id": client_id,
                "google_client_secret": client_secret,
                "google_refresh_token": refresh_token,
                "replace": True,  # reconnecting refreshes the stored token
            },
        )

    async def connect_outlook_calendar(
        self, client_id: str, client_secret: str, refresh_token: str, tenant: str
    ) -> dict:
        body = {
            "microsoft_client_id": client_id,
            "microsoft_client_secret": client_secret,
            "microsoft_refresh_token": refresh_token,
            "replace": True,
        }
        if tenant and tenant != "common":
            body["microsoft_tenant_id"] = tenant
        return await self._request("POST", "/calendar/create_outlook_calendar", json=body)

    async def calendar_connections(self) -> list[dict]:
        """Every connected calendar account, each tagged with its provider."""
        found = await self._request("GET", "/calendar/connections")
        return [{**c, "provider": provider} for provider in ("google", "outlook") for c in found.get(provider) or []]

    async def calendar_events(self, max_pages: int = 20) -> list[dict]:
        """Every synced event across the connected calendars. MeetStream pages
        them 50 at a time; "next" is an opaque cursor, sent back as ?cursor=."""
        events: list[dict] = []
        params: dict[str, str] = {}
        for _ in range(max_pages):
            page = await self._request("GET", "/calendar/events", params=params)
            events += page.get("results") or []
            if not page.get("next"):
                break
            params = {"cursor": page["next"]}
        return events

    async def status(self, bot_id: str) -> str:
        return (await self._request("GET", f"/bots/{bot_id}/status")).get("status") or ""

    async def transcript_status(self, bot_id: str) -> str:
        """ "ready", "failed" or "pending" for the call's transcript (not Meet's own captions)."""
        runs = (await self._request("GET", f"/bots/{bot_id}/transcriptions")).get("transcriptions") or []
        run: dict = next((r for r in runs if r.get("transcript_id")), {})
        status = (run.get("status") or "").lower()
        if status == "success":
            return "ready"
        return "failed" if status in ("failed", "error") else "pending"

    async def transcript_id(self, bot_id: str) -> str | None:
        # A scheduled bot has no transcript id until it has joined, so it is
        # looked up after the call rather than kept from create_bot.
        detail = await self._request("GET", f"/bots/{bot_id}/detail")
        found = (detail.get("bot_details") or {}).get("transcript_id")
        if found:
            return found
        runs = (await self._request("GET", f"/bots/{bot_id}/transcriptions")).get("transcriptions") or []
        return runs[0].get("transcript_id") if runs else None

    async def transcript(self, transcript_id: str) -> list[dict]:
        """[{"speaker": ..., "text": ...}] in speaking order."""
        items = await self._request("GET", f"/transcript/{transcript_id}/get_transcript")
        segments: list[dict] = []
        item: dict
        for item in items if isinstance(items, list) else []:
            # Current responses carry "speaker" and "transcript"; older ones only
            # a "participant" object and per-word "text".
            text: str = (item.get("transcript") or "").strip() or " ".join(
                w.get("punctuated_word") or w.get("word") or w.get("text") or "" for w in item.get("words") or []
            ).strip()
            if text:
                speaker = item.get("speaker") or (item.get("participant") or {}).get("name") or "Unknown"
                segments.append({"speaker": speaker, "text": text})
        return segments
