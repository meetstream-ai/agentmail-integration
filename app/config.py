from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # The repo's .env, whichever directory the server is started from
    model_config = SettingsConfigDict(env_file=Path(__file__).resolve().parents[1] / ".env", extra="ignore")

    # AgentMail: the agent's inbox, and the secret AgentMail signs webhooks with
    agentmail_api_key: str
    agentmail_inbox_id: str
    agentmail_webhook_secret: str = ""
    agentmail_base_url: str = "https://api.agentmail.to/v0"

    # MeetStream: sends the bot into the call and transcribes it
    meetstream_api_key: str
    meetstream_base_url: str = "https://api.meetstream.ai/api/v1"
    bot_name: str = "AgentMail Notetaker"
    transcript_provider: Literal["meetstream", "deepgram"] = "meetstream"
    # Let MeetStream post bot events to this server. With it off, the server
    # learns everything by polling (poll_seconds), which needs no public URL
    # for MeetStream.
    meetstream_callbacks: bool = True
    # How often to check on bots MeetStream has gone quiet about (0 = never)
    poll_seconds: int = 120

    # Google Calendar: an OAuth "Web application" client. Its redirect URI
    # must be registered in Google Cloud exactly as below.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/calendar/callback"
    # Outlook / Microsoft 365: an app registration in Microsoft Entra ("Web"
    # platform) with this redirect URI. "common" lets work, school and personal
    # accounts connect; a single-tenant app uses its tenant id instead.
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_tenant_id: str = "common"
    microsoft_redirect_uri: str = "http://localhost:8000/calendar/outlook/callback"
    # Hosted deployments only: sign in once at http(s)://your-host/calendar/login
    # with this to reach the calendar pages from a browser that isn't this
    # machine. Leave empty to keep them local-only.
    admin_token: str = ""
    # Only calendar meetings with this tag in their title or description get a
    # bot. A connected work calendar is full of meetings whose attendees never
    # asked for a notetaker. Set it empty to join every meeting.
    calendar_tag: str = "#notetaker"

    # The model that turns the transcript into notes: "openai", "grok" or "gemini"
    llm_provider: Literal["openai", "grok", "gemini"] = "gemini"
    openai_api_key: str = ""
    openai_model: str = "gpt-5"
    xai_api_key: str = ""
    xai_model: str = "grok-4.7"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    # Where MeetStream and AgentMail reach this server (an ngrok/cloudflared URL locally)
    public_base_url: str = "http://localhost:8000"

    # Who may send the agent into a meeting. Comma-separated addresses or
    # "@domain.com" entries. Empty means nobody: every bot costs MeetStream
    # minutes, so an open inbox would let anyone spend them.
    allowed_senders: str = ""

    database_path: str = "meetings.db"

    def sender_allowed(self, address: str) -> bool:
        address = address.strip().lower()
        for entry in (e.strip().lower() for e in self.allowed_senders.split(",")):
            if not entry:
                continue
            if entry.startswith("@") and address.endswith(entry):
                return True
            if address == entry:
                return True
        return False


@lru_cache
def get_settings() -> Settings:
    # Required fields come from the environment or .env, which mypy can't see.
    return Settings()  # type: ignore[call-arg]
