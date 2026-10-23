# Changelog

## [0.1.0] - 2026-10-22

First release.

### Added

- **Reference app** (`app/`): invite the agent by email and it sends a MeetStream bot at the
  start time, follows updates and cancellations, and replies with the notes on the invite's
  thread.
- **Recurring meetings**: a recurring invite gets a bot for every meeting in the series, one
  at a time; a change or cancellation of one meeting moves or skips just that one.
- **Calendar sync**: connect Google Calendar or Outlook; meetings tagged `#notetaker` get a
  bot that moves with the meeting and is called off when it's cancelled.
- **Notes**: a header (meeting, date, time, host, platform, participants, invited), then
  summary, decisions, action items, open questions, and follow-ups. Written by OpenAI, Grok
  or Gemini. A meeting sent as a bare link shows when the bot joined and who asked for it,
  and its notes always arrive with a subject.
- **Zoom, Google Meet and Microsoft Teams**, including meetings someone else hosts.
- **`agentmail-meetstream` 0.1.0** (`toolkit/`): six meeting tools for agents with an
  AgentMail inbox, adapters for the OpenAI Agents SDK and LangChain, and an example agent.
- **Reliability**: webhook de-duplication, retries, a safety net that checks on unfinished
  meetings, and an email to the sender when notes can't be made.
- **Deployment**: a Dockerfile, `docker-compose.yml` and a [deploy guide](docs/deploy.md).
- **Calendar setup guide**: [Google and Microsoft sign-in, step by step](docs/calendar-setup.md).
- **Audio only**: bots send `video_required: false`, since the notes need no video.
- **Security**: Svix-verified AgentMail webhooks, a secret token per MeetStream callback, a
  fail-closed `ALLOWED_SENDERS`, and calendar pages that are local-only or need `ADMIN_TOKEN`.
