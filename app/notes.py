"""Transcript in, meeting notes out: summary, decisions, action items, open questions, follow-ups."""

import json
from datetime import datetime, timezone
from html import escape
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from agentmail_meetstream.meetstream import is_named
from app.invites import platform_of

SYSTEM = """You write meeting notes from a call transcript. Only include what was
actually said: never invent an owner, date, reason or decision. Leave a field out
when the transcript doesn't state it, and leave a list empty when nothing fits.

summary: a concise overview: why the meeting happened, the key topics, and the outcome.
decisions: decisions that were explicitly made. For each, the rationale, the
  alternatives that were considered, and who approved it, when stated.
action_items: what needs to be done. For each, who owns it, the deadline, its
  status (e.g. not started, in progress, done) and what it depends on, when stated.
open_questions: what was left unresolved: unanswered questions, blockers,
  disagreements, risks and decisions that were postponed. Label each with its kind.
follow_ups: next steps: upcoming meetings, milestones, promised deliverables and
  topics to revisit, with when, if stated."""


def _list_of(properties: dict, required: str) -> dict:
    return {"type": "array", "items": {"type": "object", "properties": properties, "required": [required]}}


TEXT = {"type": "string"}
SCHEMA = {
    "type": "object",
    "properties": {
        "summary": TEXT,
        "decisions": _list_of(
            {"decision": TEXT, "rationale": TEXT, "alternatives": TEXT, "approved_by": TEXT}, "decision"
        ),
        "action_items": _list_of(
            {"task": TEXT, "owner": TEXT, "due": TEXT, "status": TEXT, "depends_on": TEXT}, "task"
        ),
        "open_questions": _list_of(
            {
                "item": TEXT,
                "kind": {"type": "string", "enum": ["question", "blocker", "disagreement", "risk", "postponed"]},
            },
            "item",
        ),
        "follow_ups": _list_of({"item": TEXT, "when": TEXT}, "item"),
    },
    "required": ["summary", "decisions", "action_items", "open_questions", "follow_ups"],
}


def _gemini_schema(node):
    # Gemini's responseSchema spells JSON Schema types in upper case.
    if isinstance(node, dict):
        return {k: v.upper() if k == "type" else _gemini_schema(v) for k, v in node.items()}
    return node


def transcript_text(segments: list[dict]) -> str:
    return "\n".join(f"{s['speaker']}: {s['text']}" for s in segments)


def _prompt(title: str, segments: list[dict]) -> str:
    return f"Meeting: {title}\n\nTranscript:\n{transcript_text(segments)}"


class NoteWriter:
    def __init__(self, api_key: str, model: str, http: httpx.AsyncClient):
        self._api_key = api_key
        self._model = model
        self._http = http

    async def write(self, title: str, segments: list[dict]) -> dict:
        raise NotImplementedError


class GeminiNotes(NoteWriter):
    async def write(self, title: str, segments: list[dict]) -> dict:
        resp = await self._http.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self._model}:generateContent",
            headers={"x-goog-api-key": self._api_key},
            json={
                "systemInstruction": {"parts": [{"text": SYSTEM}]},
                "contents": [{"role": "user", "parts": [{"text": _prompt(title, segments)}]}],
                "generationConfig": {
                    "temperature": 0.2,
                    "responseMimeType": "application/json",
                    "responseSchema": _gemini_schema(SCHEMA),
                },
            },
            timeout=120,
        )
        resp.raise_for_status()
        return json.loads(resp.json()["candidates"][0]["content"]["parts"][0]["text"])


class ChatCompletionsNotes(NoteWriter):
    """Any OpenAI-style chat completions API with JSON-schema output."""

    base_url = "https://api.openai.com/v1"
    temperature: float | None = None  # reasoning models accept only the default

    async def write(self, title: str, segments: list[dict]) -> dict:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": _prompt(title, segments)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "meeting_notes", "schema": SCHEMA},
            },
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        resp = await self._http.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=body,
            timeout=120,
        )
        resp.raise_for_status()
        return json.loads(resp.json()["choices"][0]["message"]["content"])


class OpenAINotes(ChatCompletionsNotes):
    pass


class GrokNotes(ChatCompletionsNotes):
    base_url = "https://api.x.ai/v1"
    temperature = 0.2


def make_note_writer(settings, http: httpx.AsyncClient) -> NoteWriter:
    if settings.llm_provider == "openai":
        return OpenAINotes(settings.openai_api_key, settings.openai_model, http)
    if settings.llm_provider == "grok":
        return GrokNotes(settings.xai_api_key, settings.xai_model, http)
    return GeminiNotes(settings.gemini_api_key, settings.gemini_model, http)


def _entry(main: str, parts: list[str | None], sep: str = ", ") -> str:
    """ "Main text (part, part)" with just the parts that have a value."""
    extra = sep.join(p for p in parts if p)
    return f"{main} ({extra})" if extra else main


def _with(prefix: str, value) -> str:
    return f"{prefix}{value}" if value else ""


def _as_dict(value, key: str) -> dict:
    return value if isinstance(value, dict) else {key: str(value)}


def sections(notes: dict) -> list[tuple[str, list[str]]]:
    """(heading, lines) for each part of the notes after the summary, in order."""
    decisions = [
        # Semicolons: a rationale or list of alternatives often contains commas.
        _entry(
            d["decision"],
            [
                _with("why: ", d.get("rationale")),
                _with("alternatives: ", d.get("alternatives")),
                _with("approved by ", d.get("approved_by")),
            ],
            "; ",
        )
        for d in (_as_dict(x, "decision") for x in notes.get("decisions") or [])
    ]
    actions = [
        _entry(
            a["task"],
            [
                a.get("owner"),
                _with("due ", a.get("due")),
                _with("status: ", a.get("status")),
                _with("depends on ", a.get("depends_on")),
            ],
        )
        for a in (_as_dict(x, "task") for x in notes.get("action_items") or [])
    ]
    questions = [
        f"[{q['kind'].capitalize()}] {q['item']}" if q.get("kind") else q["item"]
        for q in (_as_dict(x, "item") for x in notes.get("open_questions") or [])
    ]
    follow_ups = [
        _entry(f["item"], [f.get("when")]) for f in (_as_dict(x, "item") for x in notes.get("follow_ups") or [])
    ]
    return [
        ("Decisions", decisions),
        ("Action items", actions),
        ("Open questions", questions),
        ("Follow-ups and next steps", follow_ups),
    ]


def meeting_details(meeting, speakers: list[str] | None = None, bot_name: str = "") -> dict[str, str]:
    """The header rows of the notes email, in order. Rows we have no data for are left out.
    `speakers` are the transcript's speakers: who was actually in the call."""
    details = {"Meeting": meeting.title}
    if meeting.starts_at:
        zone = _zone(meeting.timezone)
        start = datetime.fromisoformat(meeting.starts_at).astimezone(zone)
        details["Date"] = start.strftime("%A, %d %B %Y").replace(" 0", " ")
        time = start.strftime("%H:%M")
        if meeting.ends_at:
            time += " – " + datetime.fromisoformat(meeting.ends_at).astimezone(zone).strftime("%H:%M")
        details["Time"] = f"{time} {start.strftime('%Z')}" + (
            f" ({meeting.timezone})" if meeting.timezone and meeting.timezone != "UTC" else ""
        )
    if meeting.host:
        # A meeting sent as a bare link names no host; all we know is who asked for the notes.
        link_only = getattr(meeting, "uid", "").startswith("adhoc:")
        details["Requested by" if link_only else "Hosted by"] = meeting.host
    platform = platform_of(meeting.meeting_link)
    if platform:
        details["Platform"] = platform
    bot = bot_name.strip().lower()
    spoke = list(dict.fromkeys(s for s in speakers or [] if is_named(s) and s.strip().lower() != bot))
    if spoke:
        details["Participants"] = ", ".join(spoke)
    if meeting.attendees:
        details["Invited"] = ", ".join(meeting.attendees)
    return details


def _zone(name):
    try:
        return ZoneInfo(name) if name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def render(details: dict[str, str], notes: dict) -> tuple[str, str]:
    """Plain-text and HTML bodies for the notes email. Every section is shown,
    with "None recorded" when empty, so a reader can tell nothing was missed."""
    title = details.get("Meeting", "Meeting")
    summary = notes.get("summary", "").strip()

    text = [f"Notes from {title}", ""] + [f"{k}: {v}" for k, v in details.items()]
    text += ["", "Summary:", summary]
    rows = "".join(
        f'<tr><td style="padding:2px 16px 2px 0;color:#666">{escape(k)}</td><td style="padding:2px 0">{escape(v)}</td></tr>'
        for k, v in details.items()
    )
    html = [
        f"<h2>Notes from {escape(title)}</h2>",
        f"<table>{rows}</table>",
        "<h3>Summary</h3>",
        f"<p>{escape(summary)}</p>",
    ]

    for heading, lines in sections(notes):
        bullet = "- [ ] " if heading == "Action items" else "- "
        text += ["", f"{heading}:"] + ([bullet + line for line in lines] or ["- None recorded"])
        html += (
            [f"<h3>{heading}</h3><ul>"]
            + ([f"<li>{escape(line)}</li>" for line in lines] or ["<li>None recorded</li>"])
            + ["</ul>"]
        )
    return "\n".join(text), "\n".join(html)
