# AGENTS.md

Guidance for AI coding agents working in this repository. It holds two things that share
one invite parser:

- `app/`: a FastAPI reference server. Invite by email or connect a calendar, and it sends a
  MeetStream bot, then emails the notes from an AgentMail inbox.
- `toolkit/`: `agentmail-meetstream`, the AgentMail native plugin. Meeting tools for agents,
  with adapters for the OpenAI Agents SDK and LangChain, and an example agent.

## Use the live docs for API behaviour

This repo's code is not the source of truth for either API. Before changing how it calls
one, check the current docs, and the MeetStream MCP (`https://mcp.meetstream.ai/mcp`) if you
have it:

- MeetStream: https://docs.meetstream.ai
- AgentMail: https://docs.agentmail.to

## Layout

```
app/main.py         webhooks, calendar pages, the background loop
app/flows.py        invite → bot → transcript → notes; calendar sync; safety net
app/meetstream.py   MeetStream REST client
app/agentmail.py    AgentMail REST client
app/notes.py        notes prompt, model clients, notes email
app/calendar.py     Google and Microsoft OAuth; Google and Outlook event parsing
app/store.py        SQLite (schema changes are ALTER TABLE migrations in place)
scripts/            create_inbox.py, register_webhook.py
toolkit/src/agentmail_meetstream/   the package; invites.py is shared with app/
toolkit/examples/notetaker_agent.py the example agent
tests/, toolkit/tests/              everything mocked; no keys needed
```

## Commands

```bash
pip install -r requirements-dev.txt "./toolkit[dev]"
ruff check . && ruff format --check .
mypy app scripts --ignore-missing-imports
pytest
cd toolkit && pytest
```

CI runs these on Python 3.11 and 3.12, and the toolkit alone on 3.10. The app needs 3.11+;
the toolkit must keep working on 3.10.

## API rules that are easy to get wrong

These were found against the live APIs. Don't "fix" code that follows them.

**MeetStream**

- REST auth is `Authorization: Token <key>`, not `Bearer`.
- The bot field is `meeting_link`, not `meeting_url`.
- Send `recording_config.retention`. Without it MeetStream keeps no audio, so there is
  nothing to transcribe. We use `{"type": "timed", "hours": 24}`.
- Send `"video_required": false`. The API records video unless told not to, and the notes
  need only audio.
- A scheduled bot has no transcript id until it joins. Look it up after the call from
  `/bots/{id}/detail` → `bot_details.transcript_id`, falling back to `/bots/{id}/transcriptions`.
- Transcript segments carry `speaker` and `transcript`. Older responses carry only a
  `participant` object and per-word `words`. Handle both.
- Scheduled bots are moved with `PATCH /calendar/scheduled_bots/{id}` and cancelled with
  `DELETE` on the same path. A bot already in the call is removed with
  `GET /bots/{id}/remove_bot`.
- `/calendar/events` pages with an opaque `next` cursor that goes back as `?cursor=`.
  It is not a URL or a page number.
- `/calendar/connections` returns `{"google": [...], "outlook": [...]}`.
- Per-bot `callback_url` webhooks are **not signed**. Each callback URL carries its own
  random token. Never drop it or accept a callback without it.

**AgentMail**

- Webhooks are Svix-signed. Verify the raw body before parsing. `svix` 2.8's `verify()`
  returns `None`, so parse the JSON yourself after it succeeds.
- Raw messages and attachments come back as a signed `download_url`; fetch that.
- Notes go out as a reply on the invite's thread, so they land in the same conversation.
  Only send a new email when there is no thread (calendar meetings).

**Invites**

- A change to a meeting is the same `UID` with a higher `SEQUENCE`. Ignore an older
  `SEQUENCE` that arrives late. `METHOD:CANCEL` is a cancellation.

## Security

- Never commit a key. `.env` is git-ignored; `.env.example` holds placeholders only.
- Never log a key, a token, a transcript or participant data.
- `ALLOWED_SENDERS` fails closed: empty means nobody. Keep it that way. The example agent
  filters senders before its model sees the email, and gives the model only
  `reply_to_message` for email.
- The calendar pages answer only local requests (client 127.0.0.1 or ::1, no forwarding
  headers, local hostname) or a valid `ADMIN_TOKEN` cookie.
- Calendar meetings need the `#notetaker` tag by default. Don't make joining every meeting
  the default.

## Before you finish

- ruff, mypy and both test suites pass.
- New behaviour has a test. Tests never call a real API.
- README updated if setup or behaviour changed; `CHANGELOG.md` entry added and the toolkit
  version bumped in `toolkit/pyproject.toml` if the package changed.
- Say what you verified live and what you didn't.
