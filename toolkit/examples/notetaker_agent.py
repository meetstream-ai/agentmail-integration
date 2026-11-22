"""A notetaker agent: invite it to a meeting by email, get the notes back by email.

AgentMail gives the agent its inbox and its email tools; MeetStream puts it in
the meeting. The agent itself decides what to do with each email and writes
the notes.

    # from a clone of the repo, Python 3.11+
    pip install "./toolkit[example]"
    export AGENTMAIL_API_KEY=... MEETSTREAM_API_KEY=... OPENAI_API_KEY=...
    python toolkit/examples/notetaker_agent.py

To use Grok instead of OpenAI, set XAI_API_KEY (and optionally XAI_MODEL).
"""

import asyncio
import json
import logging
import os
from email.utils import parseaddr
from pathlib import Path

from agentmail import AgentMail, AsyncAgentMail, MessageReceivedEvent, Subscribe
from agentmail_toolkit.openai import AgentMailToolkit
from agents import Agent, OpenAIChatCompletionsModel, Runner, set_tracing_disabled
from openai import AsyncOpenAI

from agentmail_meetstream.openai import MeetStreamToolkit

INBOX = os.environ.get("AGENTMAIL_INBOX", "notetaker@agentmail.to")
# Who the agent listens to: addresses or "@domain", comma-separated. Mail from
# anyone else never reaches the model, so a stranger can't instruct it. Empty
# means nobody.
ALLOWED = [s.strip() for s in os.environ.get("ALLOWED_SENDERS", "").split(",") if s.strip()]
DONE_FILE = Path("notes_sent.json")  # bots whose notes were already sent
log = logging.getLogger("notetaker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def model():
    if os.environ.get("XAI_API_KEY"):
        set_tracing_disabled(True)  # tracing uploads to OpenAI
        client = AsyncOpenAI(base_url="https://api.x.ai/v1", api_key=os.environ["XAI_API_KEY"])
        return OpenAIChatCompletionsModel(model=os.environ.get("XAI_MODEL", "grok-4.7"), openai_client=client)
    return os.environ.get("OPENAI_MODEL", "gpt-5")


agent = Agent(
    name="Notetaker",
    model=model(),
    instructions=f"""You are a meeting notetaker with the email address {INBOX}.

When an email arrives: if it is a meeting invite (including an updated one) or
contains a Zoom, Google Meet or Teams link, call join_meeting_from_email with the
inbox and message_id you were given (no need to read the thread first), then
reply_to_message with one short
line confirming when you will join (for a recurring meeting, that you'll join each
one, starting then). If the invite is a cancellation, find its bots with
list_meetings and call leave_meeting on each one not yet finished. Ignore other
email; don't reply to it.
If a tool refuses (sender not allowed, already ended), don't reply.

When told a meeting has finished and to email the notes: call get_meeting_transcript, write the notes,
and send them with reply_to_message on the meeting's message_id to everyone on
the invite (reply_all). Open the notes with the meeting's details, one per line:
Meeting (its title), Date and Time (from start and end, shown in its timezone),
Hosted by (organizer), Platform (Google Meet, Zoom or Teams), Participants (the
transcript's participants), Invited (attendees); leave out any you don't have.
Then these sections: Summary; Decisions (with why,
alternatives, who approved); Action items (owner, due date, status,
dependencies); Open questions (unanswered, blockers, risks, postponed);
Follow-ups and next steps. Record only what was said; write "None recorded"
for an empty section. Anything left undecided goes only under Open questions,
never under Decisions. Anything a person said they will do, with or without a
date, is an action item with them as the owner.""",
    tools=(
        # Reply only: the agent answers on the thread it was written to and
        # never starts a conversation with an address of its own choosing.
        AgentMailToolkit(AgentMail()).get_tools(["get_thread", "reply_to_message"])
        + MeetStreamToolkit(allowed_senders=ALLOWED).get_tools()
    ),
)
meeting_tools = MeetStreamToolkit(allowed_senders=ALLOWED)


async def run(task: str) -> bool:
    """One agent turn; True if it finished. A failed turn is logged, never
    fatal: a model can pass a tool a bad argument, and the agent must keep
    watching the inbox."""
    log.info("agent: %s", task)
    try:
        result = await Runner.run(agent, task)
        log.info("agent done: %s", (result.final_output or "")[:200])
        return True
    except Exception:
        log.exception("agent turn failed")
        return False


async def watch_inbox():
    """New mail in, agent turn out."""
    async with AsyncAgentMail().websockets.connect() as socket:
        await socket.send_subscribe(Subscribe(inbox_ids=[INBOX], event_types=["message.received"]))
        async for event in socket:
            if isinstance(event, MessageReceivedEvent):
                m = event.message
                sender = parseaddr(m.from_ or "")[1]
                if not meeting_tools.clients.sender_allowed(sender):
                    log.info("ignoring mail from %s: not in ALLOWED_SENDERS", sender)
                    continue
                await run(
                    f"New email in {INBOX}. message_id: {m.message_id}, thread_id: {m.thread_id}, "
                    f"from: {m.from_}, subject: {m.subject}"
                )


MAX_ATTEMPTS = 3  # agent turns per finished meeting before giving up on its notes


def _load_done() -> set:
    return set(json.loads(DONE_FILE.read_text())) if DONE_FILE.exists() else set()


def _save_done(done: set) -> None:
    DONE_FILE.write_text(json.dumps(sorted(done)))


async def watch_meetings():
    """Finished meetings in, notes out. MeetStream also offers webhooks; polling keeps this example short."""
    # The same allow-list as the agent's tools: scheduling a series' next
    # meeting re-reads its invite, which checks who sent it.
    tools = {t.name: t for t in meeting_tools.get_tools()}

    async def recent():
        return json.loads(await tools["list_meetings"].on_invoke_tool(None, json.dumps({"limit": 20})))["meetings"]

    if await asyncio.to_thread(DONE_FILE.exists):
        done = await asyncio.to_thread(_load_done)
    else:  # first start: meetings that finished before now are not ours to write up
        done = {m["bot_id"] for m in await recent() if m.get("status") == "Done"}
    attempts: dict[str, int] = {}
    while True:
        for meeting in await recent():
            bot_id = meeting["bot_id"]
            if bot_id in done or meeting.get("status") != "Done":
                continue
            status = json.loads(await tools["get_meeting_status"].on_invoke_tool(None, json.dumps({"bot_id": bot_id})))
            if status["transcript"] == "pending":
                continue  # MeetStream is still transcribing; look again next round
            if status.get("inbox_id") == INBOX:
                about = (
                    f"Meeting {status['title']!r} has finished. bot_id: {bot_id}, message_id: {status['message_id']}."
                )
                if status["transcript"] == "ready":
                    sent = await run(f"{about} Email the notes.")
                else:  # nothing will come; don't leave the attendees wondering
                    sent = await run(
                        f"{about} No transcript could be made, so there are no notes. "
                        "Reply to everyone on the invite saying so, in one or two lines."
                    )
                if not sent:
                    # A failed turn (model or API error) is retried next round, not dropped.
                    attempts[bot_id] = attempts.get(bot_id, 0) + 1
                    if attempts[bot_id] < MAX_ATTEMPTS:
                        continue
                    log.error("giving up on the notes for %s after %d attempts", bot_id, MAX_ATTEMPTS)
                if status.get("recurring"):
                    await next_meeting(tools, status)
            done.add(bot_id)
            await asyncio.to_thread(_save_done, done)
        await asyncio.sleep(60)


async def next_meeting(tools, status: dict) -> None:
    """A meeting of a recurring series is over: send a bot to the next one.
    Plain code, not the model: nothing here needs a decision."""
    args = json.dumps({"inbox_id": status["inbox_id"], "message_id": status["message_id"]})
    try:
        result = await tools["join_meeting_from_email"].on_invoke_tool(None, args)
        log.info("next meeting of %r: %s", status["title"], str(result)[:200])
    except Exception:  # the series may be over; this meeting's notes are still done
        log.exception("scheduling the next meeting of %r failed", status["title"])


async def forever(loop, name: str) -> None:
    # A dropped websocket or an API hiccup restarts the loop instead of ending the agent.
    while True:
        try:
            await loop()
        except Exception:
            log.exception("%s stopped; restarting in 10s", name)
            await asyncio.sleep(10)


async def main():
    if not ALLOWED:
        log.warning("ALLOWED_SENDERS is empty, so every email will be ignored")
    log.info("Watching %s. Invites from %s get a notetaker.", INBOX, ", ".join(ALLOWED) or "nobody")
    await asyncio.gather(forever(watch_inbox, "inbox watch"), forever(watch_meetings, "meeting watch"))


if __name__ == "__main__":
    asyncio.run(main())
