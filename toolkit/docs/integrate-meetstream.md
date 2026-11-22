---
title: Integrate MeetStream
subtitle: Give your agent a seat in the meeting, and send the notes from its inbox
---

## Overview

AgentMail gives your agent an email inbox: its asynchronous channel.
[MeetStream](https://meetstream.ai) puts the same agent in a live Zoom, Google Meet or
Microsoft Teams call: its synchronous channel. Together they close the loop:

1. Someone invites the agent's address to a meeting.
2. The agent reads the invite and sends a MeetStream bot to the call.
3. After the call it reads the transcript, writes the notes and replies on the invite's
   thread to everyone who was invited.

The `agentmail-meetstream` package adds six meeting tools to the ones
`agentmail-toolkit` already gives your agent:

| Tool | What it does |
|---|---|
| `join_meeting_from_email` | Reads a calendar invite (Google, Outlook, Zoom) or a meeting link in an AgentMail message and schedules a bot for the start time; for a recurring invite, its next meeting |
| `join_meeting` | Sends a bot to a meeting link, now or at a set time |
| `get_meeting_status` | Scheduled, in the call or finished, and whether the transcript is ready |
| `get_meeting_transcript` | The transcript as `Speaker: words` lines |
| `leave_meeting` | Cancels a scheduled bot, or takes it out of the call |
| `list_meetings` | Recent meetings and their bots |

Every bot remembers the invite it came from, so `get_meeting_status` hands back the
`message_id` to reply on, and the notes land in the same thread as the invite.

## Prerequisites

- An AgentMail API key and an inbox for the agent
- A [MeetStream API key](https://app.meetstream.ai)
- An OpenAI API key (or any model the OpenAI Agents SDK supports; the example also runs on Grok)

## Setup

```bash
# Python 3.11+
pip install agentmail-toolkit \
  "agentmail-meetstream[openai] @ git+https://github.com/meetstream-ai/agentmail-integration#subdirectory=toolkit"
```

```bash
export AGENTMAIL_API_KEY=am_...
export MEETSTREAM_API_KEY=ms_...
export OPENAI_API_KEY=sk-...
export AGENTMAIL_INBOX=notetaker@agentmail.to
export ALLOWED_SENDERS=@yourcompany.com   # who may send the agent into meetings
```

<Note>
A bot records everyone in the call and uses MeetStream minutes, so
`join_meeting_from_email` only acts on invites from `allowed_senders`, and an empty
list allows nobody. Give the agent `reply_to_message` rather than `send_message`, so it
can only answer the thread it was written to. The meeting's host still admits the bot
from the waiting room.
</Note>

## Code

Give one agent both toolkits:

```python
from agentmail import AgentMail
from agentmail_toolkit.openai import AgentMailToolkit
from agentmail_meetstream.openai import MeetStreamToolkit
from agents import Agent

agent = Agent(
    name="Notetaker",
    instructions=(
        "You are a meeting notetaker with the address notetaker@agentmail.to. "
        "When an invite arrives, call join_meeting_from_email and confirm with reply_to_message. "
        "When a meeting has finished, read get_meeting_transcript, write the notes "
        "(summary, decisions, action items, open questions, follow-ups) and send them "
        "with reply_to_message on the meeting's message_id, reply_all."
    ),
    tools=(
        AgentMailToolkit(AgentMail()).get_tools(["get_thread", "reply_to_message"])
        + MeetStreamToolkit(allowed_senders=["@yourcompany.com"]).get_tools()
    ),
)
```

Wake the agent when mail arrives, over AgentMail's WebSocket. Check the sender
before the model sees anything: an agent that reads email will act on what the
email says, so only people you trust should be able to write to it.

```python
from email.utils import parseaddr
from agentmail import AsyncAgentMail, MessageReceivedEvent, Subscribe
from agents import Runner

meeting_tools = MeetStreamToolkit(allowed_senders=["@yourcompany.com"])


async def watch_inbox():
    async with AsyncAgentMail().websockets.connect() as socket:
        await socket.send_subscribe(Subscribe(inbox_ids=["notetaker@agentmail.to"], event_types=["message.received"]))
        async for event in socket:
            if isinstance(event, MessageReceivedEvent):
                m = event.message
                if not meeting_tools.clients.sender_allowed(parseaddr(m.from_ or "")[1]):
                    continue  # strangers never reach the model
                await Runner.run(agent, f"New email. message_id: {m.message_id}, from: {m.from_}, subject: {m.subject}")
```

And again when a meeting finishes, so it can write the notes: watch `get_meeting_status`
(or MeetStream's `transcription.processed` webhook) and run the agent with the
meeting's `bot_id` and `message_id`. The complete program, both loops included, is
[`examples/notetaker_agent.py`](../examples/notetaker_agent.py).

**Recurring meetings.** For a recurring invite, `join_meeting_from_email` schedules the
series' next meeting, and the result says `recurring: true`. Once that meeting is over,
call `join_meeting_from_email` again with the same `message_id`: it schedules the one
after. Calling it again before then changes nothing, and an updated invite replaces the
bot that was waiting. The example agent does this for you.

## That's it

```bash
python notetaker_agent.py
```

Create a meeting in Google Calendar or Outlook, add the agent's address as a guest, and
send the invite. The agent confirms by email, joins at the start time, and after the
call everyone on the invite gets the notes, in the invite's own thread.

The same tools work in LangChain: `from agentmail_meetstream.langchain import MeetStreamToolkit`.
