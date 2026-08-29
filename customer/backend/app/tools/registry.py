"""One tool definition, two provider schemas.

Defining a tool's JSON schema twice is how the OpenAI and Anthropic paths silently drift
apart. Each ToolDef emits both.

ToolResult's split is the important design decision: `llm_content` is what the model reads
and reasons over, `events` are what the frontend renders. One tool call feeds both, and
the model never has to describe a UI in prose.

Tools are pure functions of (args, ctx). They never decide when they run — agent/loop.py
does, using the list agent/policy.py permits for that turn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.agent.events import Event
from app.models.profile import AgentProfile
from app.retrieval.index import CatalogIndex
from app.session.models import Session

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    session: Session
    profile: AgentProfile
    index: CatalogIndex

    #: Set by search_catalog. Scoped to one turn, because ToolContext is built per turn.
    #: This is what lets probe_attributes refuse to ask a question with nothing on screen.
    products_shown: bool = False

    @property
    def merchant_id(self) -> str:
        return self.session.merchant_id


@dataclass
class ToolResult:
    llm_content: str = ""
    events: list[Event] = field(default_factory=list)
    error: bool = False
    summary: str = ""

    @classmethod
    def failure(cls, message: str, *, code: str = "tool_error") -> "ToolResult":
        """Errors come back as a tool result so the model can recover conversationally.

        A raised exception kills the stream; a returned error lets the agent say "that
        one is gone, here are two others" and keep the sale alive.
        """
        from app.agent.events import ErrorEvent

        return cls(
            llm_content=f"ERROR: {message}",
            events=[ErrorEvent(code=code, message=message)],
            error=True,
            summary=message,
        )


Handler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict
    handler: Handler
    #: Summary shown in the tool_start event while the tool runs.
    start_summary: str = ""

    async def __call__(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Invoke the handler directly, so a decorated tool stays callable as a function."""
        return await self.handler(args, ctx)

    def to_openai(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_anthropic(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


_TOOLS: dict[str, ToolDef] = {}


def register(tool: ToolDef) -> ToolDef:
    _TOOLS[tool.name] = tool
    return tool


def tool(
    *, name: str, description: str, parameters: dict, start_summary: str = ""
) -> Callable[[Handler], ToolDef]:
    def decorator(handler: Handler) -> ToolDef:
        return register(
            ToolDef(
                name=name,
                description=description,
                parameters=parameters,
                handler=handler,
                start_summary=start_summary,
            )
        )

    return decorator


def get_tool(name: str) -> ToolDef | None:
    return _TOOLS.get(name)


def all_tools() -> list[ToolDef]:
    _ensure_loaded()
    return list(_TOOLS.values())


def schemas_for(tools: list[ToolDef], provider: str) -> list[dict]:
    if provider.lower() == "anthropic":
        return [t.to_anthropic() for t in tools]
    return [t.to_openai() for t in tools]


_loaded = False


def _ensure_loaded() -> None:
    """Import the tool modules so their decorators run. Import-order independence."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    from app.tools import (  # noqa: F401
        build_cart,
        compare_products,
        confirm_and_pay,
        get_product_details,
        preview_transaction,
        probe_attributes,
        search_catalog,
    )


def object_schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }
