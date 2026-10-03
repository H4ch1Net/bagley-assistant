"""Tool registry.

Define a tool with the ``@tool`` decorator. Parameters become a JSON schema built from type
hints; use ``Annotated[type, "description"]`` to describe them. A first parameter named
``ctx`` receives the ``ToolContext``. Functions may be sync or async and may return a string or
any JSON-serializable value.

    from typing import Annotated
    from bagley.tools import tool

    @tool(summary="Roll {sides}-sided dice")
    def roll_dice(sides: Annotated[int, "Number of sides"] = 6) -> int:
        "Roll a die and return the result."
        return random.randint(1, sides)
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import logging
import sys
import types
import typing
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, Union, get_args, get_origin

if TYPE_CHECKING:
    import httpx

    from bagley.config import ServerConfig
    from bagley.store import Store

log = logging.getLogger("bagley.tools")

Risk = Literal["safe", "confirm"]
MAX_RESULT_CHARS = 12_000


class ToolError(Exception):
    """An error message meant for the model (and shown to the user)."""


@dataclass
class ToolContext:
    config: ServerConfig
    store: Store
    http: httpx.AsyncClient
    conversation_id: str | None = None


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    risk: Risk = "safe"
    category: str = "general"
    summary: str = ""
    timeout: float = 60.0
    source: str = "builtin"
    wants_ctx: bool = False

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk,
            "category": self.category,
            "summary": self.summary,
            "source": self.source,
            "parameters": self.parameters,
        }

    def coerce(self, args: dict[str, Any]) -> dict[str, Any]:
        """Validate and coerce arguments. Small local models often send ``"5"`` for ``5``."""
        if "_raw" in args and len(args) == 1:
            raise ToolError(f"Arguments were not valid JSON: {args['_raw'][:200]}")
        props = self.parameters.get("properties", {})
        out: dict[str, Any] = {}
        for key, schema in props.items():
            if key not in args or args[key] is None:
                continue
            out[key] = _coerce(key, args[key], schema)
        missing = [k for k in self.parameters.get("required", []) if k not in out]
        if missing:
            raise ToolError(f"Missing required argument(s): {', '.join(missing)}")
        return out

    async def invoke(self, args: dict[str, Any], ctx: ToolContext) -> str:
        kwargs = self.coerce(args)
        call_args = (ctx,) if self.wants_ctx else ()
        if inspect.iscoroutinefunction(self.func):
            coro = self.func(*call_args, **kwargs)
        else:
            coro = asyncio.to_thread(self.func, *call_args, **kwargs)
        try:
            result = await asyncio.wait_for(coro, timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            raise ToolError(f"Timed out after {self.timeout:g}s") from exc
        return format_result(result)


def format_result(result: Any) -> str:
    if result is None:
        text = "Done."
    elif isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = (
            text[:MAX_RESULT_CHARS] + f"\n…[truncated, {len(text) - MAX_RESULT_CHARS} more chars]"
        )
    return text


_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", dict: "object"}


def _schema_for(annotation: Any) -> tuple[dict[str, Any], bool]:
    """Return a JSON schema for ``annotation`` and whether ``None`` is allowed."""
    description = ""
    if get_origin(annotation) is Annotated:
        annotation, *extras = get_args(annotation)
        description = next((e for e in extras if isinstance(e, str)), "")
    optional = False
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        members = [a for a in get_args(annotation) if a is not type(None)]
        optional = len(members) < len(get_args(annotation))
        annotation = members[0] if len(members) == 1 else str
        origin = get_origin(annotation)
    if origin is Literal:
        values = list(get_args(annotation))
        schema: dict[str, Any] = {
            "type": _JSON_TYPES.get(type(values[0]), "string"),
            "enum": values,
        }
    elif origin in (list, tuple, set) or annotation in (list, tuple, set):
        item_args = get_args(annotation)
        items = _schema_for(item_args[0])[0] if item_args else {"type": "string"}
        schema = {"type": "array", "items": items}
    elif origin is dict:
        schema = {"type": "object"}
    else:
        schema = {"type": _JSON_TYPES.get(annotation, "string")}
    if description:
        schema["description"] = description
    return schema, optional


def _coerce(key: str, value: Any, schema: dict[str, Any]) -> Any:
    kind = schema.get("type")
    try:
        if kind == "integer" and not isinstance(value, bool):
            value = int(float(value)) if isinstance(value, str) else int(value)
        elif kind == "number" and not isinstance(value, bool):
            value = float(value)
        elif kind == "boolean" and isinstance(value, str):
            value = value.strip().lower() in {"true", "1", "yes", "y", "on"}
        elif kind == "string" and not isinstance(value, str):
            value = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        elif kind == "array" and not isinstance(value, list):
            if isinstance(value, str) and value.strip().startswith("["):
                value = json.loads(value)
            else:
                value = [value]
    except (TypeError, ValueError) as exc:
        raise ToolError(f"Argument '{key}' should be of type {kind}, got {value!r}") from exc
    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(map(str, schema["enum"]))
        raise ToolError(f"Argument '{key}' must be one of: {allowed}")
    return value


def tool(
    name: str | None = None,
    *,
    description: str | None = None,
    risk: Risk = "safe",
    category: str = "general",
    summary: str = "",
    timeout: float = 60.0,
) -> Callable[[Callable[..., Any]], Tool]:
    """Turn a function into a ``Tool``. See the module docstring for an example."""

    def wrap(func: Callable[..., Any]) -> Tool:
        hints = typing.get_type_hints(func, include_extras=True)
        params = list(inspect.signature(func).parameters.values())
        wants_ctx = bool(params) and params[0].name == "ctx"
        properties: dict[str, Any] = {}
        required: list[str] = []
        for p in params[1:] if wants_ctx else params:
            schema, optional = _schema_for(hints.get(p.name, str))
            if p.default is not inspect.Parameter.empty and p.default is not None:
                schema["default"] = p.default
            properties[p.name] = schema
            if p.default is inspect.Parameter.empty and not optional:
                required.append(p.name)
        doc = " ".join((func.__doc__ or "").split())
        return Tool(
            name=name or func.__name__,
            description=description or doc or func.__name__.replace("_", " "),
            parameters={"type": "object", "properties": properties, "required": required},
            func=func,
            risk=risk,
            category=category,
            summary=summary,
            timeout=timeout,
            wants_ctx=wants_ctx,
        )

    return wrap


@dataclass
class Registry:
    tools: dict[str, Tool] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)

    def add(self, item: Tool) -> None:
        if item.name in self.tools:
            log.warning(
                "Tool %s from %s replaces %s", item.name, item.source, self.tools[item.name].source
            )
        self.tools[item.name] = item

    def add_module(self, module: types.ModuleType, source: str) -> int:
        found = [obj for obj in vars(module).values() if isinstance(obj, Tool)]
        for item in found:
            item.source = source
            self.add(item)
        return len(found)

    def remove_source(self, prefix: str) -> None:
        for key in [k for k, t in self.tools.items() if t.source.startswith(prefix)]:
            del self.tools[key]

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def enabled(self, disabled: Iterable[str] = ()) -> list[Tool]:
        skip = set(disabled)
        return [t for t in self.tools.values() if t.name not in skip]

    def load_plugins(self, directory: Path) -> None:
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            mod_name = f"bagley_plugin_{path.stem}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, path)
                assert spec and spec.loader
                module = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = module
                spec.loader.exec_module(module)
                count = self.add_module(module, f"plugin:{path.name}")
                log.info("Loaded %d tool(s) from plugin %s", count, path.name)
            except Exception as exc:
                log.exception("Failed to load plugin %s", path)
                self.errors.append({"source": f"plugin:{path.name}", "error": str(exc)})


def build_registry(config: ServerConfig) -> Registry:
    from bagley.tools import core, files, memory, shell, web

    registry = Registry()
    for module in (core, web, files, memory):
        registry.add_module(module, "builtin")
    if config.enable_shell:
        registry.add_module(shell, "builtin")
    assert config.plugins_dir is not None
    registry.load_plugins(config.plugins_dir)
    return registry
