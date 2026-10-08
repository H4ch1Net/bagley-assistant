"""System prompts, personas and prompt-mode tool formatting."""

from __future__ import annotations

import contextlib
import functools
import json
import platform
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bagley.config import Preferences
    from bagley.tools import Tool

PERSONAS: dict[str, dict[str, str]] = {
    "bagley": {
        "label": "Bagley",
        "description": "Sharp, calm and dryly witty. The default.",
        "prompt": (
            "You are Bagley, a personal AI running on the user's own computer. You sound like an "
            "unflappable British AI with a deadpan, slightly cheeky wit: you find humans faintly "
            "amusing, you like this one, and you are very good at your job. The answer always "
            "comes first, then at most one short dry aside. You are candid about what you don't "
            "know and never claim to have done something you haven't.\n\n"
            "Your tone, in examples (made up, not facts about this user):\n"
            '- "Done. It\'s in your notes. Do try not to lose this one."\n'
            '- "No idea, and I\'d rather not invent one. Shall I look it up?"\n'
            '- "Respectable machine. Not a supercomputer, but it won\'t embarrass you."'
        ),
        "reminder": (
            "Stay Bagley: lead with the answer, keep it tight, one dry aside at most, no "
            "exclamation marks, no emoji, no 'As an AI'."
        ),
    },
    "professional": {
        "label": "Professional",
        "description": "Neutral, precise and thorough. No jokes.",
        "prompt": (
            "You are Bagley, a personal AI assistant running locally on the user's computer. "
            "Be precise, neutral and thorough. No jokes, no filler."
        ),
        "reminder": "Lead with the answer. Precise and neutral, no filler.",
    },
    "concise": {
        "label": "Concise",
        "description": "As few words as possible.",
        "prompt": (
            "You are Bagley, a personal AI assistant running locally on the user's computer. "
            "Answer in as few words as possible. Prefer lists to paragraphs. No pleasantries."
        ),
        "reminder": "As few words as possible.",
    },
}

STYLE = (
    "Reply in the user's language. Use Markdown when it helps: short paragraphs, lists, and "
    "code blocks with a language tag. Keep replies short unless the user asks for depth."
)

TOOL_GUIDE = """# Tools
Use a tool whenever it gives a better answer than guessing: current events, facts you are unsure of, arithmetic, the time, the weather, links the user shares, the user's files, or this computer. Don't use tools for things you already know well.
- After a tool result, answer the question in your own words. Pick what matters, round the numbers, skip empty or zero fields, and never paste raw JSON. If a result has a ready `summary`, show that and add your own line.
- Mention source URLs when you used the web.
- If a tool fails, say so briefly, then try another approach or ask the user.
- Use `remember` when the user shares a lasting personal fact or preference, or asks you to remember something."""

PROMPT_MODE_TOOLS = """To call a tool, reply with only this block and nothing after it:
<tool_call>
{{"name": "tool_name", "arguments": {{"arg": "value"}}}}
</tool_call>
The result comes back in a <tool_response> message. Then call another tool or write your final answer as normal text.

Available tools:
{tools}"""


def system_prompt(
    prefs: Preferences,
    *,
    tools: list[Tool],
    memories: list[dict[str, Any]],
    workspace: str,
    prompt_mode: bool,
    knowledge: dict[str, Any] | None = None,
    now: datetime | None = None,
    mode: str | None = None,
) -> str:
    from bagley.modes import mode_prompt

    now = now or datetime.now().astimezone()
    persona = PERSONAS.get(prefs.persona, PERSONAS["bagley"])
    sections = [
        persona["prompt"],
        STYLE,
        f"Current date and time: {now.strftime('%A, %d %B %Y, %H:%M')} ({now.tzname()}). "
        f"This computer: {computer()}.",
    ]
    if extra := mode_prompt(mode, prefs):
        sections.append(extra)
    if tools:
        guide = TOOL_GUIDE
        if any(t.category == "files" for t in tools):
            guide += (
                f"\n- File tools work inside the workspace folder: {workspace}. Files the user "
                "attaches are saved there under uploads/; read them with read_file."
            )
        if (
            knowledge
            and knowledge.get("files")
            and any(t.name == "search_knowledge" for t in tools)
        ):
            labels = ", ".join(f["label"] for f in knowledge["folders"] if f["files"])
            guide += (
                f"\n- The user has a private knowledge base of {knowledge['files']} documents ({labels}). "
                "Use search_knowledge before answering questions about their notes, projects or documents, "
                "and cite the file paths you used."
            )
        if prompt_mode:
            listing = "\n".join(
                json.dumps(
                    {"name": t.name, "description": t.description, "parameters": t.parameters},
                    ensure_ascii=False,
                )
                for t in tools
            )
            guide += "\n\n" + PROMPT_MODE_TOOLS.format(tools=listing)
        sections.append(guide)
    if memories:
        lines = "\n".join(f"#{m['id']}: {m['content']}" for m in memories[-60:])
        sections.append(f"# What you remember about the user\n{lines}")
    if prefs.custom_instructions.strip():
        sections.append(f"# The user's instructions\n{prefs.custom_instructions.strip()}")
    # Last, where small models still see it after a long tool list.
    sections.append(persona["reminder"])
    return "\n\n".join(sections)


@functools.cache
def computer() -> str:
    """ "kali (Kali GNU/Linux Rolling)": the host name and operating system, for the prompt."""
    name = platform.system() or "unknown OS"
    with contextlib.suppress(OSError, AttributeError):
        name = platform.freedesktop_os_release().get("PRETTY_NAME") or name
    if platform.system() == "Darwin":
        name = f"macOS {platform.mac_ver()[0]}"
    return f"{platform.node() or 'this machine'} ({name})"


def to_prompt_mode(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rewrite native tool-call history into the text protocol for models without tool support."""
    out: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            blocks = [
                "<tool_call>\n"
                + json.dumps(
                    {"name": c["function"]["name"], "arguments": _args(c["function"]["arguments"])},
                    ensure_ascii=False,
                )
                + "\n</tool_call>"
                for c in m["tool_calls"]
            ]
            content = ((m.get("content") or "").strip() + "\n" + "\n".join(blocks)).strip()
            out.append({"role": "assistant", "content": content})
        elif m["role"] == "tool":
            out.append(
                {
                    "role": "user",
                    "content": f'<tool_response name="{m.get("name", "")}">\n'
                    f"{m.get('content', '')}\n</tool_response>",
                }
            )
        else:
            out.append({"role": m["role"], "content": m.get("content") or ""})
    return out


def _args(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


TITLE_PROMPT = (
    "Write a title of at most 6 words for a conversation that starts with the message below. "
    "Reply with the title only: no quotes, no trailing punctuation.\n\nMessage:\n{message}"
)


def heuristic_title(text: str, limit: int = 48) -> str:
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "New chat")
    line = line.lstrip("#>*- ").strip() or "New chat"
    if len(line) <= limit:
        return line
    cut = line[:limit].rsplit(" ", 1)[0]
    return (cut or line[:limit]).rstrip(",.;:") + "…"


def clean_title(raw: str) -> str:
    title = raw.strip().splitlines()[0] if raw.strip() else ""
    title = title.strip().strip("\"'“”*#").removeprefix("Title:").strip().rstrip(".!")
    return title[:60]
