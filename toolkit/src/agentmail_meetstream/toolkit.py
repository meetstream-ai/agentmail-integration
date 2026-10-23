from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from agentmail import AgentMail

from .functions import Clients
from .meetstream import MeetStream
from .tools import Tool, tools

T = TypeVar("T")


def error_message(error: BaseException, limit: int = 500) -> str:
    """A short, model-readable reason, never a whole API error body."""
    response = getattr(error, "response", None)
    text = str(error)
    if response is not None:
        try:
            body = response.json()
            text = body.get("message") or body.get("error") or body.get("detail") or text
        except ValueError:
            pass
        text = f"{text} (HTTP {response.status_code})"
    return text if len(text) <= limit else text[:limit] + "…"


class Toolkit(Generic[T], ABC):
    """MeetStream meeting tools for an agent that has an AgentMail inbox.

    Pair it with AgentMail's own toolkit: these tools put the agent in the
    meeting; AgentMail's send_message and reply_to_message email the notes.
    """

    def __init__(
        self,
        agentmail: AgentMail | None = None,
        meetstream: MeetStream | None = None,
        bot_name: str = "AgentMail Notetaker",
        allowed_senders: list[str] | None = None,
    ):
        self.clients = Clients(
            agentmail=agentmail or AgentMail(),
            meetstream=meetstream or MeetStream(),
            bot_name=bot_name,
            allowed_senders=allowed_senders or [],
        )
        self._tools = {tool.name: self._build_tool(tool) for tool in tools}

    @abstractmethod
    def _build_tool(self, tool: Tool) -> T: ...

    def close(self) -> None:
        """Release the HTTP connections the toolkit opened."""
        self.clients.http.close()
        self.clients.meetstream.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get_tools(self, names: list[str] | None = None) -> list[T]:
        if not names:
            return list(self._tools.values())
        return [self._tools[name] for name in names if name in self._tools]
