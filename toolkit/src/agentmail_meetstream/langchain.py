"""The tools for LangChain: MeetStreamToolkit().get_tools() -> [BaseTool]."""

from langchain.tools import BaseTool, ToolException
from langchain.tools import tool as langchain_tool

from .toolkit import Toolkit, error_message
from .tools import Tool


class MeetStreamToolkit(Toolkit[BaseTool]):
    def _build_tool(self, tool: Tool) -> BaseTool:
        def runnable(**kwargs):
            try:
                result = tool.func(self.clients, kwargs)
            except Exception as e:
                raise ToolException(error_message(e)) from e
            return "OK" if result is None else result.model_dump_json()

        # LangChain wraps a plain callable passed as runnable; its type hints only admit a Runnable.
        built = langchain_tool(  # type: ignore[call-overload]
            name_or_callable=tool.name,
            description=tool.description,
            args_schema=tool.params_schema,
            runnable=runnable,
        )
        built.handle_tool_error = True  # a failed call becomes a message the model can act on
        return built
