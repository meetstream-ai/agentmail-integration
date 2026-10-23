"""The tools for the OpenAI Agents SDK: MeetStreamToolkit().get_tools() -> [FunctionTool]."""

import asyncio
import json

from agents import FunctionTool, RunContextWrapper

from .toolkit import Toolkit, error_message
from .tools import Tool


class MeetStreamToolkit(Toolkit[FunctionTool]):
    def _build_tool(self, tool: Tool) -> FunctionTool:
        async def on_invoke_tool(ctx: RunContextWrapper, input_str: str) -> str:
            try:
                result = await asyncio.to_thread(tool.func, self.clients, json.loads(input_str or "{}"))
            except Exception as e:
                raise RuntimeError(error_message(e)) from e
            return "OK" if result is None else result.model_dump_json()

        schema = tool.params_schema.model_json_schema()
        schema["additionalProperties"] = False
        return FunctionTool(
            name=tool.name,
            description=tool.description,
            params_json_schema=schema,
            on_invoke_tool=on_invoke_tool,
            # Optional fields keep their defaults; strict mode would make every field required.
            strict_json_schema=False,
        )
