<div align="center">

# agentmail-meetstream

**MeetStream meeting tools for AI agents that have an [AgentMail](https://agentmail.to) inbox.**

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](#install)
[![MIT License](https://img.shields.io/badge/license-MIT-yellow)](https://github.com/meetstream-ai/agentmail-integration/blob/main/toolkit/LICENSE)
[![OpenAI Agents SDK](https://img.shields.io/badge/OpenAI%20Agents%20SDK-supported-10B981)](#use)
[![LangChain](https://img.shields.io/badge/LangChain-supported-10B981)](#use)

</div>

---

Add your agent to a meeting invite. [MeetStream](https://meetstream.ai) puts it in the call,
and it emails everyone the notes.

## Install

Install straight from GitHub, with the extra for your framework:

```bash
# OpenAI Agents SDK
pip install "agentmail-meetstream[openai] @ git+https://github.com/meetstream-ai/agentmail-integration#subdirectory=toolkit"
# LangChain
pip install "agentmail-meetstream[langchain] @ git+https://github.com/meetstream-ai/agentmail-integration#subdirectory=toolkit"
```

To run the example agent (Python 3.11+), clone the repo and install from it:

```bash
git clone https://github.com/meetstream-ai/agentmail-integration.git
cd agentmail-integration
pip install "./toolkit[example]"
```

## Use

```python
from agentmail_meetstream.openai import MeetStreamToolkit  # or .langchain

tools = MeetStreamToolkit(allowed_senders=["@yourcompany.com"]).get_tools()
```

Reads `AGENTMAIL_API_KEY` and `MEETSTREAM_API_KEY` from the environment, or pass
`agentmail=AgentMail(...)` and `meetstream=MeetStream(api_key=...)`. Without a framework,
`agentmail_meetstream.MeetStreamToolkit` returns plain tools you call with `tool.func(**args)`.

## Tools

| Tool | What it does |
|---|---|
| `join_meeting_from_email` | Reads a calendar invite or meeting link in an AgentMail message and schedules a bot for the start time; for a recurring invite, its next meeting |
| `join_meeting` | Sends a bot to a Zoom, Google Meet or Teams link, now or at a set time |
| `get_meeting_status` | Where the bot is, and whether the transcript is ready |
| `get_meeting_transcript` | The transcript as `Speaker: words` lines, plus who took part |
| `leave_meeting` | Cancels a scheduled bot or takes it out of the call |
| `list_meetings` | Recent meetings |

## Pairs with agentmail-toolkit

`allowed_senders` decides who can send the agent into meetings by email; empty means
nobody. Pair it with `agentmail-toolkit` for the email side, preferably just
`reply_to_message`, so the agent only answers the thread it was written to.
The tool names don't overlap. Each bot keeps the `inbox_id`, `message_id` and `thread_id`
of the invite it came from, plus the meeting's time, host and attendees, so the notes can
be a reply in the invite's thread with the meeting's details at the top.

## Learn more

- **Try it:** the repo README's [Quickstart](https://github.com/meetstream-ai/agentmail-integration#quickstart)
- **Guide:** [docs/integrate-meetstream.md](https://github.com/meetstream-ai/agentmail-integration/blob/main/toolkit/docs/integrate-meetstream.md)
- **Complete agent:** [examples/notetaker_agent.py](https://github.com/meetstream-ai/agentmail-integration/blob/main/toolkit/examples/notetaker_agent.py)

## Development

```bash
pip install -e ".[dev]"
pytest
```
