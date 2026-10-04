"""The learning loop.

After a task that took several tool calls, Bagley looks back at what it did, in the background
and on the same local model. It saves the procedure as a skill (or improves a skill it followed)
and keeps any lasting facts the user stated about themselves.

It sees the user's words, the tool calls and its own answer, never the tool results. Arguments
and the answer can still echo something a web page said, so when a run read web content, used
helpers, past chats or plugin tools, a learned skill is only a draft until the user approves it,
and a fact is kept only when most of its words are the user's own.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from bagley.llm.textparse import StreamParser
from bagley.skills import SkillError, slug

if TYPE_CHECKING:
    from bagley.runtime import Runtime

log = logging.getLogger("bagley.learning")

LEARN_AFTER = 5  # Tool calls in one reply before Bagley reflects on it.
UNTRUSTED_TOOLS = {"web_search", "fetch_webpage", "delegate_task", "search_chats"}
STOPWORDS = {"the", "user", "user's", "users", "their", "they", "them", "with", "and", "for",
    "that", "this", "from", "about", "into", "has", "have", "had", "are", "was", "were", "is",
    "be", "been", "being", "who", "what", "when", "where", "which", "will", "would", "should",
    "could", "also", "very", "just", "like", "some", "any", "all", "not", "but", "its", "it's",
    "your", "you", "our"}  # fmt: skip

PROMPT = """You just finished a task for the user. Decide whether anything is worth keeping for next time.

The user's request:
{request}

The tools you called, in order:
{calls}

Your final answer (shortened):
{answer}

Saved skills:
{skills}
{used}
What you already remember about the user:
{memories}

Reply with JSON only, in this shape:
{{"skill": null or {{"name": "lowercase-with-hyphens", "description": "one sentence: what it does and when to use it", "instructions": "numbered Markdown steps that name the tools"}}, "facts": ["lasting facts the user stated about themselves"]}}

Rules:
- Save a skill only for a procedure that is likely to come up again with different details. Write it in general terms, not about this one case, and include what you learned about doing it well.
- To improve a skill you followed, use its exact name and give the complete improved instructions.
- Don't repeat an existing skill or memory. Facts must come from the user's own words: preferences, location, projects. Not task details.
- If nothing is worth keeping, reply {{"skill": null, "facts": []}}."""


def _calls(trace: list[dict[str, Any]]) -> str:
    lines = []
    for call in trace[:30]:
        args = json.dumps(call["arguments"], ensure_ascii=False)
        if len(args) > 160:
            args = args[:157] + "…"
        lines.append(f"- {call['name']} {args} → {'ok' if call['ok'] else 'failed'}")
    return "\n".join(lines)


def _json(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def reflect(
    rt: Runtime,
    *,
    model: str,
    request: str,
    trace: list[dict[str, Any]],
    answer: str,
    trusted: bool = True,
) -> dict[str, Any]:
    """Look back at one finished reply. Returns what was learned. With ``trusted`` False a new or
    improved skill is saved as a draft for the user to review."""
    used = [
        str(c["arguments"].get("name", "")).strip().lower()
        for c in trace
        if c["name"] == "read_skill" and c["ok"]
    ]
    followed = "".join(
        f"\nThe skill you followed, '{s.name}':\n{s.body[:2500]}\n"
        for s in (rt.skills.get(n) for n in dict.fromkeys(used))
        if s
    )
    skills = "\n".join(f"- {s['name']}: {s['description']}" for s in rt.skills.index()) or "(none)"
    memories = "\n".join(f"- {m['content']}" for m in rt.store.list_memories()[-40:]) or "(none)"
    prompt = PROMPT.format(
        request=request[:1500],
        calls=_calls(trace),
        answer=answer[:1500] or "(none)",
        skills=skills,
        used=followed,
        memories=memories,
    )
    provider = await rt.provider()
    caps = await provider.capabilities(model)
    raw = await provider.complete(
        [{"role": "user", "content": prompt}],
        model=model,
        temperature=0.2,
        max_tokens=1200,
        think=False if caps.thinking else None,
    )
    parser = StreamParser(parse_tools=False)
    data = _json(parser.feed(raw).text + parser.finish().text)
    learned: dict[str, Any] = {"skill": None, "improved": False, "facts": [], "draft": False}

    skill = data.get("skill")
    if isinstance(skill, dict) and all(
        isinstance(skill.get(k), str) for k in ("name", "description", "instructions")
    ):
        try:
            name = slug(skill["name"])
            existing = rt.skills.get(name)
            # Only rewrite an existing skill if this run actually followed it.
            if existing is None or name in used:
                saved = rt.skills.save(
                    name,
                    skill["description"],
                    skill["instructions"],
                    source="learned",
                    pending=not trusted,
                )
                learned["skill"] = saved.summary()
                learned["improved"] = existing is not None
                learned["draft"] = not trusted
        except SkillError as exc:
            log.debug("Skipped a learned skill: %s", exc)

    facts = data.get("facts")
    if isinstance(facts, list):
        known = {m["content"].lower() for m in rt.store.list_memories()}
        for fact in facts[:3]:
            if isinstance(fact, str) and 4 <= len(fact.strip()) <= 200:
                text = " ".join(fact.split())
                if text.lower() not in known and _stated(text, request):
                    learned["facts"].append(rt.store.add_memory(text)["content"])

    if learned["skill"]:
        s = learned["skill"]
        await rt.broadcast({"type": "skills.changed"})
        if learned["draft"]:
            await rt.notify(
                "A skill is waiting for review",
                f"{s['name']}: {s['description']} Approve it in Settings → Skills.",
            )
        else:
            title = "Improved a skill" if learned["improved"] else "Learned a new skill"
            await rt.notify(title, f"{s['name']}: {s['description']}")
    if learned["facts"]:
        await rt.broadcast({"type": "memories.changed"})
        await rt.notify("Remembered", "; ".join(learned["facts"]))
    return learned


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _stated(fact: str, request: str) -> bool:
    """Most of a fact's words must be the user's own, so it can't come from somewhere else."""
    words = [_stem(w) for w in re.findall(r"[^\W\d_]{3,}", fact.lower()) if w not in STOPWORDS]
    said = [_stem(w) for w in re.findall(r"[^\W\d_]{3,}", request.lower())]
    if not words:
        return False
    hits = sum(any(w.startswith(s) or s.startswith(w) for s in said) for w in words)
    return hits / len(words) >= 0.6
