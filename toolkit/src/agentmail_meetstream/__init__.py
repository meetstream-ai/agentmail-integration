"""MeetStream meeting tools for agents with an AgentMail inbox.

Framework-agnostic tools live here; agentmail_meetstream.openai and
agentmail_meetstream.langchain build the same tools for those frameworks.
"""

from collections.abc import Callable

from pydantic import BaseModel

from .meetstream import MeetStream
from .toolkit import Toolkit
from .tools import Tool as _Tool

__all__ = ["MeetStream", "MeetStreamToolkit", "Tool"]


class Tool(BaseModel):
    name: str
    description: str
    params_schema: type[BaseModel]
    func: Callable


class MeetStreamToolkit(Toolkit[Tool]):
    """Plain tools: call tool.func(**arguments) and get a pydantic model back."""

    def _build_tool(self, tool: _Tool) -> Tool:
        def func(**kwargs):
            return tool.func(self.clients, kwargs)

        return Tool(name=tool.name, description=tool.description, params_schema=tool.params_schema, func=func)
