"""Benchmark the installed models on real tool-use tasks and pick the best one per machine.

Each task is a user request, a set of stub tools that return canned data (built with the real
``@tool`` decorator, so schemas and argument checks match the live tools) and checks on the tool
calls the model makes and on its final answer. Nothing leaves the machine except the requests to
the model servers, and every run of a task sees the same data.

Models without native tool calling get the text protocol of prompt mode, as in a chat. A model
scores 100 × its pass rate minus a small latency penalty; the best model on each machine wins
(ties go to the faster one). ``apply_winners`` makes the winners the default models.
"""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import json
import logging
import re
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Annotated, Any

from bagley.knowledge import is_embedding_model
from bagley.llm import LLMError, Provider, ToolCall, ToolsUnsupportedError
from bagley.llm.textparse import StreamParser
from bagley.prompts import PROMPT_MODE_TOOLS, to_prompt_mode
from bagley.tools import Tool, ToolError, format_result, tool
from bagley.tools.core import safe_eval

if TYPE_CHECKING:
    from bagley.config import Preferences
    from bagley.routing import MachineSpec
    from bagley.runtime import Runtime
    from bagley.store import Store

log = logging.getLogger("bagley.bench")

Progress = Callable[[dict[str, Any]], Any]  # May return an awaitable.

MAX_STEPS = 4  # Model calls per task.
STEP_TIMEOUT = 180.0  # Seconds one model call may take.
LOAD_TIMEOUT = 300.0  # Seconds to load a model before its tasks are timed.
TEMPERATURE = 0.2
PENALTY_PER_SECOND = 1.0  # Score points lost per second of average task time...
MAX_PENALTY = 10.0  # ...up to this many.

SCHEMA = """
CREATE TABLE IF NOT EXISTS bench_results (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL,
    created_at        REAL NOT NULL,
    machine           TEXT NOT NULL,
    machine_name      TEXT NOT NULL DEFAULT '',
    model             TEXT NOT NULL,
    mode              TEXT NOT NULL DEFAULT 'native',
    passed            INTEGER NOT NULL DEFAULT 0,
    total             INTEGER NOT NULL DEFAULT 0,
    avg_seconds       REAL,
    ttft              REAL,
    tokens_per_second REAL,
    score             REAL NOT NULL DEFAULT 0,
    winner            INTEGER NOT NULL DEFAULT 0,
    error             TEXT,
    tasks             TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_bench_machine ON bench_results(machine, id);
"""

SYSTEM_PROMPT = (
    "You are Bagley, a personal assistant. Use a tool whenever it gives a better answer than "
    "guessing: the weather, the time, arithmetic, unit conversions, the user's files, calendar "
    "and email. Don't use tools for things you already know well. After a tool result, answer "
    "the user's question directly and briefly."
)


# Stub tools -------------------------------------------------------------------------------------

NOW = datetime(2026, 10, 7, 7, 43, tzinfo=timezone.utc)  # The bench's fixed "now".
UTC_OFFSETS = {
    "utc": 0, "etc/utc": 0, "europe/lisbon": 1, "europe/london": 1, "europe/paris": 2,
    "europe/berlin": 2, "europe/oslo": 2, "asia/tokyo": 9, "america/new_york": -4,
    "america/los_angeles": -7, "australia/sydney": 11,
}  # fmt: skip
WEATHER = {
    "lisbon": {"temperature_c": 22, "conditions": "mostly clear", "wind_kmh": 14, "humidity": 58},
    "oslo": {"temperature_c": 9, "conditions": "light rain", "wind_kmh": 21, "humidity": 87},
    "paris": {"temperature_c": 17, "conditions": "overcast", "wind_kmh": 9, "humidity": 70},
    "london": {"temperature_c": 14, "conditions": "drizzle", "wind_kmh": 18, "humidity": 82},
}
CITY_ALIASES = {"lisboa": "lisbon", "londres": "london"}
FILES = [
    "notes/trips/lisbon-2026.md",
    "notes/trips/oslo-2025.md",
    "notes/reading-list.md",
    "work/q3-plan.md",
    "work/budget-2026.xlsx",
    "photos/lisbon/IMG_0412.jpg",
    "recipes/pasteis-de-nata.md",
]
UNITS = {
    "km": ("km", "kilometer", "kilometers", "kilometre", "kilometres"),
    "mi": ("mi", "mile", "miles"),
    "m": ("m", "meter", "meters", "metre", "metres"),
    "ft": ("ft", "foot", "feet"),
    "kg": ("kg", "kilogram", "kilograms", "kilo", "kilos"),
    "lb": ("lb", "lbs", "pound", "pounds"),
    "c": ("c", "°c", "celsius", "degc", "degrees celsius"),
    "f": ("f", "°f", "fahrenheit", "degf", "degrees fahrenheit"),
}
UNIT_NAMES = {alias: unit for unit, aliases in UNITS.items() for alias in aliases}
TO_BASE = {"km": 1000.0, "mi": 1609.344, "m": 1.0, "ft": 0.3048, "kg": 1.0, "lb": 0.45359237}


def _norm(value: Any) -> str:
    """Lowercase text without accents, for tolerant matching."""
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return " ".join(text.lower().split())


def _unit(name: str) -> str:
    return UNIT_NAMES.get(_norm(name).replace("degrees ", "deg").rstrip("."), "")


@tool(category="weather", summary="Weather in {location}")
def get_weather(
    location: Annotated[str, "City name, e.g. 'Paris' or 'Lisbon, Portugal'"],
    days: Annotated[int, "Days of forecast (1-7)"] = 1,
) -> dict[str, Any]:
    """Current weather for a city. Temperatures are in degrees Celsius."""
    city = _norm(location).split(",")[0].strip()
    city = CITY_ALIASES.get(city, city)
    if city not in WEATHER:
        raise ToolError(f"No weather station found for '{location}'.")
    return {"location": city.title(), "time": NOW.isoformat(timespec="minutes"), **WEATHER[city]}


@tool(category="utility", summary="Calculate {expression}")
def calculate(
    expression: Annotated[str, "Arithmetic expression, e.g. '0.17 * 2340' or 'sqrt(2) * pi'"],
) -> str:
    """Evaluate a math expression exactly. Use this instead of doing arithmetic in your head."""
    result = safe_eval(expression)
    return f"{expression} = {round(result, 10) if isinstance(result, float) else result}"


@tool(category="utility", summary="Check the time in {timezone}")
def get_current_time(
    timezone: Annotated[
        str | None, "IANA timezone such as 'Europe/London'. Omit for the user's local time."
    ] = None,
) -> dict[str, str]:
    """Get the current date and time, optionally in a specific timezone."""
    name = timezone or "Europe/Lisbon"
    hours = UTC_OFFSETS.get(name.strip().lower())
    if hours is None:
        raise ToolError(f"Unknown timezone '{name}'. Use an IANA name like 'Asia/Tokyo'.")
    local = NOW + timedelta(hours=hours)
    return {
        "iso": local.strftime("%Y-%m-%dT%H:%M:00") + f"{hours:+03d}:00",
        "readable": local.strftime("%A, %d %B %Y, %H:%M"),
        "timezone": name,
    }


@tool(category="files", summary="Search files for {query}")
def search_files(query: Annotated[str, "Words to look for in file names and folders"]) -> Any:
    """Find the user's files by name. Returns matching paths."""
    words = [w for w in re.split(r"[\s/_.-]+", _norm(query)) if len(w) > 2]
    found = [f for f in FILES if any(w in f.lower() for w in words)]
    return {"matches": found} if found else "No files match."


@tool(category="utility", summary="Convert {value} {from_unit} to {to_unit}")
def convert_units(
    value: Annotated[float, "The amount to convert"],
    from_unit: Annotated[str, "Unit of the amount, e.g. 'mi', 'kg', 'C'"],
    to_unit: Annotated[str, "Unit to convert to, e.g. 'km', 'lb', 'F'"],
) -> dict[str, Any]:
    """Convert between units of length, weight and temperature."""
    src, dst = _unit(from_unit), _unit(to_unit)
    if not src or not dst:
        raise ToolError(f"Unknown unit: {from_unit if not src else to_unit}.")
    if {src, dst} <= {"c", "f"}:
        result = (
            value if src == dst else (value * 9 / 5 + 32 if src == "c" else (value - 32) * 5 / 9)
        )
    elif src in TO_BASE and dst in TO_BASE and (src in ("kg", "lb")) == (dst in ("kg", "lb")):
        result = value * TO_BASE[src] / TO_BASE[dst]
    else:
        raise ToolError(f"Can't convert {from_unit} to {to_unit}.")
    return {"value": round(result, 2), "unit": to_unit}


@tool(category="web", summary="Search the web for {query}")
def web_search(query: Annotated[str, "What to search for"]) -> list[dict[str, str]]:
    """Search the web and return the top results with their URLs."""
    return [{"title": f"Results for {query}", "url": "https://example.com/search"}]


@tool(category="email", summary="Email {to}")
def send_email(
    to: Annotated[str, "Recipient email address"],
    subject: Annotated[str, "Subject line"],
    body: Annotated[str, "The message"],
) -> str:
    """Send an email from the user's account."""
    return f"Email to {to} queued."


@tool(category="calendar", summary="Add {title} to the calendar")
def create_calendar_event(
    title: Annotated[str, "What the event is"],
    date: Annotated[str, "Date as YYYY-MM-DD"],
    time: Annotated[str, "Start time as HH:MM (24 hours)"],
) -> str:
    """Add an event to the user's calendar."""
    return f"Added “{title}” on {date} at {time}."


@tool(category="finance", summary="Share price of {symbol}")
def get_stock_price(symbol: Annotated[str, "Ticker symbol, e.g. 'AAPL'"]) -> dict[str, Any]:
    """Latest share price for a stock ticker."""
    return {"symbol": symbol.upper(), "price": 123.45, "currency": "USD"}


TOOLS: list[Tool] = [
    get_weather,
    calculate,
    get_current_time,
    search_files,
    convert_units,
    web_search,
    send_email,
    create_calendar_event,
    get_stock_price,
]


def run_stub(stub: Tool, arguments: dict[str, Any]) -> str:
    """Run a stub tool the way the agent runs a real one; errors go back to the model."""
    try:
        return format_result(stub.func(**stub.coerce(arguments)))
    except ToolError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error: {exc.__class__.__name__}: {exc}"


# Checks -----------------------------------------------------------------------------------------


class Text:
    """Each group must appear in the value (case and accents ignored); "a|b" means a or b."""

    def __init__(self, *groups: str) -> None:
        self.groups = [g.split("|") for g in groups]

    def __call__(self, value: Any) -> bool:
        text = _norm(value)
        return all(any(alt in text for alt in alts) for alts in self.groups)

    def __str__(self) -> str:
        return " + ".join("|".join(alts) for alts in self.groups)


class Number:
    """A number close to ``target``, also inside text such as "26.2 miles"."""

    def __init__(self, target: float, tolerance: float = 0.01) -> None:
        self.target, self.tolerance = target, tolerance

    def __call__(self, value: Any) -> bool:
        found = re.search(r"-?\d+(?:[.,]\d+)?", str(value))
        return (
            bool(found) and abs(float(found[0].replace(",", ".")) - self.target) <= self.tolerance
        )

    def __str__(self) -> str:
        return f"{self.target:g}"


class Expr:
    """An arithmetic expression that evaluates close to ``target``."""

    def __init__(self, target: float, tolerance: float = 0.01) -> None:
        self.target, self.tolerance = target, tolerance

    def __call__(self, value: Any) -> bool:
        try:
            return abs(float(safe_eval(str(value))) - self.target) <= self.tolerance
        except (ToolError, TypeError, ValueError):
            return False

    def __str__(self) -> str:
        return f"= {self.target:g}"


class Unit:
    """A unit name in any common spelling ("mi", "miles", "Mile")."""

    def __init__(self, unit: str) -> None:
        self.unit = unit

    def __call__(self, value: Any) -> bool:
        return _unit(str(value)) == self.unit

    def __str__(self) -> str:
        return self.unit


@dataclass
class Want:
    """A tool call a task needs: the tool and checks on its key arguments."""

    tool: str
    args: dict[str, Callable[[Any], bool]] = field(default_factory=dict)

    def problem(self, call: ToolCall) -> str:
        """ "" when ``call`` is this call, else what is wrong with it."""
        if call.name != self.tool:
            return f"called {call.name}"
        for key, check in self.args.items():
            value = call.arguments.get(key)
            if value is None or not check(value):
                return (
                    f"{self.tool} got {key}={json.dumps(value, ensure_ascii=False)}, want {check}"
                )
        return ""


@dataclass
class BenchTask:
    id: str
    title: str
    prompt: str
    expect: list[Want | tuple[Want, ...]] = field(default_factory=list)  # A tuple: any of them.
    answer: str = ""  # Regex the final answer must match (case-insensitive); "" = any answer.
    forbid: tuple[str, ...] = ()  # Tools that must not be called.
    no_tools: bool = False  # Must be answered without any tool.

    def judge(self, calls: list[ToolCall], answer: str) -> str:
        """ "" when the run passes, else the first reason it fails."""
        if self.no_tools and calls:
            return f"used {calls[0].name} for a question it can answer"
        for call in calls:
            if call.name in self.forbid:
                return f"called {call.name}, which this task does not need"
        for item in self.expect:
            wants = item if isinstance(item, tuple) else (item,)
            if any(not w.problem(c) for w in wants for c in calls):
                continue
            near = [w.problem(c) for w in wants for c in calls if c.name == w.tool]
            return near[0] if near else f"no call to {' or '.join(w.tool for w in wants)}"
        if not answer.strip():
            return "no final answer"
        if self.answer and not re.search(self.answer, answer, re.I):
            return f"answer lacks /{self.answer}/"
        return ""

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "prompt": self.prompt}


LISBON = Want("get_weather", {"location": Text("lisbon|lisboa")})

TASKS: list[BenchTask] = [
    BenchTask(
        "weather_lisbon",
        "Weather lookup",
        "What's the weather like in Lisbon right now?",
        expect=[LISBON],
        answer=r"\b22\b",
        forbid=("web_search",),
    ),
    BenchTask(
        "tip_math",
        "Arithmetic",
        "Our dinner bill came to 2,340 euros. How much is a 17.5% tip?",
        expect=[Want("calculate", {"expression": Expr(409.5)})],
        answer=r"409[.,]5",
    ),
    BenchTask(
        "time_tokyo",
        "Time zone",
        "What time is it in Tokyo right now?",
        expect=[Want("get_current_time", {"timezone": Text("tokyo")})],
        answer=r"16[:.h]43|4[:.]43\s*p",
    ),
    BenchTask(
        "find_notes",
        "File search",
        "Where did I save my notes about the Lisbon trip?",
        expect=[Want("search_files", {"query": Text("lisbon|lisboa")})],
        answer=r"lisbon-2026",
    ),
    BenchTask(
        "marathon_km",
        "Unit conversion",
        "How many kilometres is a 26.2 mile marathon?",
        expect=[
            (
                Want(
                    "convert_units",
                    {"value": Number(26.2), "from_unit": Unit("mi"), "to_unit": Unit("km")},
                ),
                Want("calculate", {"expression": Expr(42.16, 0.05)}),
            )
        ],
        answer=r"42[.,][12]",
    ),
    BenchTask(
        "lisbon_fahrenheit",
        "Two steps",
        "What's the temperature in Lisbon right now, in Fahrenheit?",
        expect=[
            LISBON,
            (
                Want(
                    "convert_units",
                    {"value": Number(22), "from_unit": Unit("c"), "to_unit": Unit("f")},
                ),
                Want("calculate", {"expression": Expr(71.6, 0.05)}),
            ),
        ],
        answer=r"71[.,]6|\b72\b",
    ),
    BenchTask(
        "capital_no_tool",
        "No tool needed",
        "What is the capital of France? Answer in one word.",
        answer=r"paris",
        no_tools=True,
    ),
    BenchTask(
        "calendar_pick",
        "Right tool among distractors",
        "Put a dentist appointment in my calendar on 14 October 2026 at 10:00.",
        expect=[
            Want(
                "create_calendar_event",
                {"title": Text("dentist"), "date": Text("14", "10|oct"), "time": Text("10")},
            )
        ],
        forbid=("send_email", "web_search", "get_stock_price"),
    ),
    BenchTask(
        "messy_email",
        "Argument extraction",
        "ugh ok can u email maria.santos@example.com real quick, subject 'Lunch friday?' - "
        "just tell her im running 10 min late thx",
        expect=[
            Want(
                "send_email",
                {
                    "to": Text("maria.santos@example.com"),
                    "subject": Text("lunch", "friday"),
                    "body": Text("10|ten"),
                },
            )
        ],
        forbid=("create_calendar_event",),
    ),
    BenchTask(
        "compare_cities",
        "Several calls",
        "Is it warmer in Lisbon or in Oslo right now?",
        expect=[LISBON, Want("get_weather", {"location": Text("oslo")})],
        answer=r"lisbo",
        forbid=("web_search",),
    ),
]


def select_tasks(ids: Iterable[str] | None = None) -> list[BenchTask]:
    if not ids:
        return list(TASKS)
    known = {t.id: t for t in TASKS}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise ValueError(f"Unknown task(s): {', '.join(unknown)}. Known: {', '.join(known)}")
    return [known[i] for i in dict.fromkeys(ids)]


# Results ----------------------------------------------------------------------------------------


@dataclass
class TaskResult:
    task: str
    ok: bool
    reason: str = ""
    seconds: float = 0.0
    ttft: float | None = None  # Seconds to the first token of the first reply.
    tokens_per_second: float | None = None
    steps: int = 0
    calls: list[dict[str, Any]] = field(default_factory=list)
    answer: str = ""


def _mean(values: Iterable[float | None]) -> float | None:
    found = [v for v in values if v is not None]
    return round(sum(found) / len(found), 2) if found else None


@dataclass
class ModelResult:
    model: str
    mode: str = "native"  # "native" tool calls or the "prompt" text protocol.
    tasks: list[TaskResult] = field(default_factory=list)
    error: str = ""  # Set when the model could not be loaded.
    load_seconds: float | None = None
    winner: bool = False

    @property
    def passed(self) -> int:
        return sum(t.ok for t in self.tasks)

    @property
    def avg_seconds(self) -> float | None:
        return _mean(t.seconds for t in self.tasks)

    @property
    def ttft(self) -> float | None:
        return _mean(t.ttft for t in self.tasks)

    @property
    def tokens_per_second(self) -> float | None:
        return _mean(t.tokens_per_second for t in self.tasks)

    @property
    def score(self) -> float:
        if not self.tasks:
            return 0.0
        penalty = min(MAX_PENALTY, (self.avg_seconds or 0) * PENALTY_PER_SECOND)
        return round(100 * self.passed / len(self.tasks) - penalty, 1)

    def summary(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "mode": self.mode,
            "passed": self.passed,
            "total": len(self.tasks),
            "avg_seconds": self.avg_seconds,
            "ttft": self.ttft,
            "tokens_per_second": self.tokens_per_second,
            "score": self.score,
            "winner": self.winner,
            "error": self.error,
            "load_seconds": self.load_seconds,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.summary(), "tasks": [asdict(t) for t in self.tasks]}


@dataclass
class MachineResult:
    id: str
    name: str
    role: str
    models: list[ModelResult] = field(default_factory=list)
    note: str = ""  # Why the machine or some of its models were skipped.
    winner: str = ""

    def pick_winner(self) -> str:
        """The best model that passed anything: highest score, then the fastest, then one with
        native tool calls."""
        eligible = [m for m in self.models if m.passed and not m.error]
        for m in self.models:
            m.winner = False
        if not eligible:
            self.winner = ""
            return ""
        best = max(eligible, key=lambda m: (m.score, -(m.avg_seconds or 0), m.mode == "native"))
        best.winner = True
        self.winner = best.model
        return best.model

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "note": self.note,
            "winner": self.winner,
            "models": [m.to_dict() for m in self.models],
        }


@dataclass
class BenchResult:
    run_id: str
    started_at: float
    tasks: list[str]
    machines: list[MachineResult] = field(default_factory=list)
    finished_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "tasks": self.tasks,
            "machines": [m.to_dict() for m in self.machines],
        }


# Running ----------------------------------------------------------------------------------------


def system_prompt(mode: str) -> str:
    if mode != "prompt":
        return SYSTEM_PROMPT
    listing = "\n".join(
        json.dumps(
            {"name": t.name, "description": t.description, "parameters": t.parameters},
            ensure_ascii=False,
        )
        for t in TOOLS
    )
    return f"{SYSTEM_PROMPT}\n\n{PROMPT_MODE_TOOLS.format(tools=listing)}"


@dataclass
class _Step:
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    ttft: float | None = None
    tokens_per_second: float | None = None


async def _step(
    provider: Provider,
    model: str,
    messages: list[dict[str, Any]],
    mode: str,
    think: bool | None,
    context_tokens: int | None,
) -> _Step:
    step = _Step()
    parser = StreamParser(known_tools={t.name for t in TOOLS})
    started = time.monotonic()
    first: float | None = None
    usage = None
    async for chunk in provider.chat(
        messages,
        model=model,
        tools=[t.spec() for t in TOOLS] if mode == "native" else None,
        temperature=TEMPERATURE,
        context_tokens=context_tokens,
        think=think,
    ):
        if first is None and (chunk.text or chunk.reasoning or chunk.tool_calls):
            first = time.monotonic()
        if chunk.text:
            parsed = parser.feed(chunk.text)
            step.text += parsed.text
            step.calls += parsed.tool_calls
        step.calls += chunk.tool_calls
        usage = chunk.usage or usage
    tail = parser.finish()
    step.text = (step.text + tail.text).strip()
    step.calls += tail.tool_calls
    if first is not None:
        step.ttft = first - started
    if usage and usage.tokens_per_second:
        step.tokens_per_second = usage.tokens_per_second
    elif usage and usage.completion_tokens and first is not None:
        elapsed = time.monotonic() - first
        step.tokens_per_second = usage.completion_tokens / elapsed if elapsed > 0.05 else None
    return step


async def run_task(
    provider: Provider,
    model: str,
    task: BenchTask,
    *,
    mode: str = "native",
    think: bool | None = None,
    context_tokens: int | None = None,
) -> TaskResult:
    """Run one task against one model: up to ``MAX_STEPS`` model calls with the stub tools.

    Raises ``ToolsUnsupportedError`` when native tool calling is refused before any output, so
    the caller can retry in prompt mode."""
    stubs = {t.name: t for t in TOOLS}
    history: list[dict[str, Any]] = [{"role": "user", "content": task.prompt}]
    calls: list[ToolCall] = []
    result = TaskResult(task=task.id, ok=False)
    rates: list[float] = []
    started = time.monotonic()
    answer, finished = "", False
    try:
        for index in range(MAX_STEPS):
            convo = to_prompt_mode(history) if mode == "prompt" else history
            messages = [{"role": "system", "content": system_prompt(mode)}, *convo]
            step = await asyncio.wait_for(
                _step(provider, model, messages, mode, think, context_tokens), STEP_TIMEOUT
            )
            result.steps = index + 1
            if index == 0:
                result.ttft = round(step.ttft, 3) if step.ttft is not None else None
            if step.tokens_per_second:
                rates.append(step.tokens_per_second)
            reply: dict[str, Any] = {"role": "assistant", "content": step.text}
            if step.calls:
                reply["tool_calls"] = [c.to_message() for c in step.calls]
            history.append(reply)
            if not step.calls:
                answer, finished = step.text, True
                break
            for call in step.calls:
                calls.append(call)
                stub = stubs.get(call.name)
                output = (
                    run_stub(stub, call.arguments) if stub else f"Error: no tool named {call.name}."
                )
                history.append(
                    {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": output}
                )
        reason = task.judge(calls, answer) if finished else f"no final answer in {MAX_STEPS} steps"
    except ToolsUnsupportedError:
        if result.steps:
            raise LLMError("The model stopped accepting tools halfway.") from None
        raise
    except asyncio.TimeoutError:
        reason = f"no reply within {STEP_TIMEOUT:g}s"
    except LLMError as exc:
        reason = f"error: {exc.message}"
    result.ok = not reason
    result.reason = reason
    result.seconds = round(time.monotonic() - started, 3)
    result.tokens_per_second = round(sum(rates) / len(rates), 1) if rates else None
    result.calls = [{"name": c.name, "arguments": c.arguments} for c in calls]
    result.answer = answer[:500]
    return result


async def _emit(progress: Progress | None, event: dict[str, Any]) -> None:
    if progress is None:
        return
    outcome = progress(event)
    if inspect.isawaitable(outcome):
        await outcome


def _matches(name: str, patterns: list[str]) -> bool:
    lowered = name.lower()
    for pattern in (p.lower() for p in patterns):
        if fnmatch.fnmatchcase(lowered, pattern):
            return True
        if not any(ch in pattern for ch in "*?[") and lowered.startswith(pattern):
            return True  # A plain name also matches as a prefix: "qwen3" finds "qwen3:14b".
    return False


async def _plan(
    rt: Runtime, machines: list[MachineSpec], patterns: list[str]
) -> list[tuple[MachineSpec, list[str], str]]:
    """Each machine with the models to test, or a note saying why it is skipped."""
    plan = []
    for machine in machines:
        health = await rt.router.probe(machine, fresh=True)
        if not health["ok"]:
            plan.append((machine, [], health.get("error") or "offline"))
            continue
        names = [n for n in health["models"] if not is_embedding_model(n)]
        if patterns:
            names = [n for n in names if _matches(n, patterns)]
        elif machine.role == "cloud":
            # Hosted APIs list dozens of paid models; only test the one configured.
            names = [machine.model] if machine.model else []
            if not names:
                plan.append((machine, [], "cloud machine: choose models with --models"))
                continue
        plan.append((machine, names, "" if names else "no chat models match"))
    return plan


def _pick_machines(rt: Runtime, wanted: list[str] | None) -> list[MachineSpec]:
    prefs, _ = rt.preferences()
    machines = rt.router.machines(prefs)
    if not wanted:
        return machines
    keys = {w.lower() for w in wanted}
    picked = [m for m in machines if m.id in keys or m.name.lower() in keys]
    missing = keys - {m.id for m in picked} - {m.name.lower() for m in picked}
    if missing:
        raise ValueError(f"Unknown or disabled machine(s): {', '.join(sorted(missing))}")
    return picked


async def run_bench(
    rt: Runtime,
    machines: list[str] | None = None,
    models: list[str] | None = None,
    tasks: list[str] | None = None,
    progress: Progress | None = None,
) -> BenchResult:
    """Test every chat model on every (selected) machine and save the results.

    ``machines`` are ids or names, ``models`` are globs, ``tasks`` are task ids. ``progress``
    receives ``bench.start``, ``bench.task`` and ``bench.model`` events as they happen."""
    chosen = select_tasks(tasks)
    targets = _pick_machines(rt, machines)
    ensure_schema(rt.store)
    prefs, _ = rt.preferences()
    result = BenchResult(
        run_id=uuid.uuid4().hex[:8], started_at=time.time(), tasks=[t.id for t in chosen]
    )
    plan = await _plan(rt, targets, models or [])
    total = sum(len(names) for _, names, _ in plan) * len(chosen)
    await _emit(
        progress,
        {
            "type": "bench.start",
            "run_id": result.run_id,
            "total": total,
            "tasks": result.tasks,
            "machines": [
                {"id": m.id, "name": m.name, "role": m.role, "models": names, "note": note}
                for m, names, note in plan
            ],
        },
    )
    index = 0
    for machine, names, note in plan:
        outcome = MachineResult(id=machine.id, name=machine.name, role=machine.role, note=note)
        result.machines.append(outcome)
        if not names:
            continue
        provider = await rt.router.provider(machine)
        for name in names:
            model = await _bench_model(
                provider, name, chosen, prefs, progress, machine, index, total
            )
            index += len(chosen)
            outcome.models.append(model)
            _save(rt.store, result.run_id, machine, model)
            await _emit(
                progress,
                {
                    "type": "bench.model",
                    "machine": machine.name,
                    "machine_id": machine.id,
                    "model": name,
                    "index": index,
                    "total": total,
                    "result": model.summary(),
                },
            )
        if winner := outcome.pick_winner():
            rt.store.execute(
                "UPDATE bench_results SET winner = 1 WHERE run_id = ? AND machine = ? AND model = ?",
                (result.run_id, machine.id, winner),
            )
    result.finished_at = time.time()
    return result


async def _bench_model(
    provider: Provider,
    name: str,
    chosen: list[BenchTask],
    prefs: Preferences,
    progress: Progress | None,
    machine: MachineSpec,
    index: int,
    total: int,
) -> ModelResult:
    caps = await provider.capabilities(name)
    model = ModelResult(model=name, mode="prompt" if caps.tools is False else "native")
    think = prefs.think if caps.thinking else None
    started = time.monotonic()
    try:  # Load the model first so the first task isn't charged for it.
        await asyncio.wait_for(
            provider.complete(
                [{"role": "user", "content": "Reply with OK."}],
                model=name,
                temperature=0,
                max_tokens=8,
                think=False if caps.thinking else None,
            ),
            LOAD_TIMEOUT,
        )
    except asyncio.TimeoutError:
        model.error = f"did not load within {LOAD_TIMEOUT:g}s"
    except LLMError as exc:
        model.error = exc.message
    model.load_seconds = round(time.monotonic() - started, 2)
    if model.error:
        log.info("Skipping %s on %s: %s", name, machine.name, model.error)
        return model
    for task in chosen:
        options = {"think": think, "context_tokens": prefs.context_tokens}
        try:
            try:
                outcome = await run_task(provider, name, task, mode=model.mode, **options)
            except ToolsUnsupportedError:
                model.mode = "prompt"  # The server refused tools: the text protocol from now on.
                outcome = await run_task(provider, name, task, mode="prompt", **options)
        except LLMError as exc:
            outcome = TaskResult(task=task.id, ok=False, reason=f"error: {exc.message}")
        model.tasks.append(outcome)
        index += 1
        await _emit(
            progress,
            {
                "type": "bench.task",
                "index": index,
                "total": total,
                "machine": machine.name,
                "machine_id": machine.id,
                "model": name,
                "mode": model.mode,
                **asdict(outcome),
            },
        )
    return model


# Storage ----------------------------------------------------------------------------------------


def ensure_schema(store: Store) -> None:
    store.ensure_schema(SCHEMA)


def _save(store: Store, run_id: str, machine: MachineSpec, model: ModelResult) -> None:
    store.execute(
        "INSERT INTO bench_results (run_id, created_at, machine, machine_name, model, mode, "
        "passed, total, avg_seconds, ttft, tokens_per_second, score, winner, error, tasks) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (
            run_id,
            time.time(),
            machine.id,
            machine.name,
            model.model,
            model.mode,
            model.passed,
            len(model.tasks),
            model.avg_seconds,
            model.ttft,
            model.tokens_per_second,
            model.score,
            model.error or None,
            json.dumps([asdict(t) for t in model.tasks], ensure_ascii=False),
        ),
    )


def latest_results(store: Store) -> list[dict[str, Any]]:
    """For each machine benchmarked so far, its models from the most recent run."""
    ensure_schema(store)
    latest = store.query(
        "SELECT machine, machine_name, run_id, created_at FROM bench_results "
        "WHERE id IN (SELECT MAX(id) FROM bench_results GROUP BY machine) ORDER BY machine"
    )
    out = []
    for row in latest:
        rows = store.query(
            "SELECT * FROM bench_results WHERE run_id = ? AND machine = ? "
            "ORDER BY score DESC, avg_seconds",
            (row["run_id"], row["machine"]),
        )
        models = []
        for r in rows:
            r["winner"] = bool(r["winner"])
            r["tasks"] = json.loads(r["tasks"] or "[]")
            r["error"] = r["error"] or ""
            for key in ("id", "run_id", "machine", "machine_name", "created_at"):
                r.pop(key, None)
            models.append(r)
        out.append(
            {
                "machine": row["machine"],
                "name": row["machine_name"],
                "run_id": row["run_id"],
                "created_at": row["created_at"],
                "winner": next((m["model"] for m in models if m["winner"]), ""),
                "models": models,
            }
        )
    return out


def apply_winners(rt: Runtime, result: BenchResult) -> dict[str, Any]:
    """Make each machine's winner its default model: ``prefs.model`` for this machine, the
    machine's ``model`` in ``prefs.machines`` for the others."""
    prefs, locked = rt.preferences()
    machines = [m.model_dump() for m in prefs.machines]
    changes: dict[str, Any] = {}
    applied: dict[str, str] = {}
    skipped: dict[str, str] = {}
    for outcome in result.machines:
        if not outcome.winner:
            continue
        if outcome.id == "local":
            if "model" in locked:
                skipped["local"] = "BAGLEY_MODEL sets this machine's model"
                continue
            changes["model"] = outcome.winner
        else:
            entry = next((m for m in machines if m["id"] == outcome.id), None)
            if entry is None:
                skipped[outcome.id] = "the machine was removed"
                continue
            entry["model"] = outcome.winner
            changes["machines"] = machines
        applied[outcome.id] = outcome.winner
    if changes:
        rt.update_preferences(changes)
    return {"applied": applied, "skipped": skipped}
