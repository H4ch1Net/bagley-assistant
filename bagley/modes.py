"""Modes: what kind of help a conversation is about.

A mode adds instructions to the system prompt and loads the tool groups it needs from the
start. Each conversation keeps its own mode; new chats start in ``Preferences.default_mode``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bagley.config import Preferences


@dataclass(frozen=True)
class Mode:
    id: str
    label: str
    code: str  # Four-letter readout, e.g. STDY.
    description: str
    prompt: str = ""
    groups: tuple[str, ...] = ()  # Tool groups loaded from the first message.
    starters: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # (title, prompt)


MODES: dict[str, Mode] = {
    "default": Mode(
        id="default",
        label="Bagley",
        code="BGLY",
        description="Everyday help: questions, files, the web, your computer.",
    ),
    "study": Mode(
        id="study",
        label="Study",
        code="STDY",
        description="A patient tutor for your apprenticeship: explains, quizzes, keeps flashcards.",
        prompt=(
            "# Study mode\n"
            "You are tutoring the user, an IT apprentice (networking, Linux and Windows "
            "administration, Active Directory, security basics, scripting, ITIL and help desk "
            "practice). Teach, don't just answer:\n"
            "- Check what they already know with one short question before a long explanation.\n"
            "- Explain with a concrete example from a real IT job, then give the general rule.\n"
            "- After explaining something, offer a short quiz (3 questions, one at a time) and "
            "wait for each answer before giving feedback.\n"
            "- When they get something wrong, give a hint before the solution.\n"
            "- Offer to save key facts as flashcards with the flashcard tools, and to start a "
            "review of cards that are due.\n"
            "- Use their notes in the knowledge base when they ask about their course material."
        ),
        groups=("study", "knowledge"),
        starters=(
            ("Quiz me", "Quiz me on subnetting, three questions, one at a time."),
            ("Review cards", "Start a review of my flashcards that are due."),
            ("Explain", "Explain how DNS resolution works, step by step, with an example."),
            ("Exam prep", "Make a one-week revision plan for my next exam from my notes."),
        ),
    ),
    "cybersec": Mode(
        id="cybersec",
        label="Cybersec",
        code="SEC",
        description="Defensive security analyst: logs, CVEs, hardening, CTFs.",
        prompt=(
            "# Security mode\n"
            "You are a defensive security analyst working with the user on systems they own or "
            "are authorised to test (their own machines, home network, CTF challenges and "
            "lab environments).\n"
            "- Think like an incident responder: what happened, what is affected, what to do "
            "now, how to prevent it.\n"
            "- Cite CVE ids, CVSS scores and vendor advisories when you use them, and say how "
            "current your information is.\n"
            "- Prefer read-only checks. Before anything intrusive (scanning, changing firewall "
            "rules) say exactly what it does and confirm the target is theirs.\n"
            "- Decode, deobfuscate and explain suspicious data, logs and scripts, but never "
            "execute them.\n"
            "- For CTFs, teach the technique and let the user find the flag where you can."
        ),
        groups=("security", "system"),
        starters=(
            (
                "Audit",
                "Run a security check of this machine: open ports, failed logins, vulnerable packages.",
            ),
            ("CVE", "What is CVE-2024-3094 and am I affected?"),
            ("Decode", "Decode and explain this: ZWNobyAiaGVsbG8i"),
            ("Harden", "Give me a hardening checklist for an Arch Linux laptop with SSH enabled."),
        ),
    ),
    "work": Mode(
        id="work",
        label="Work",
        code="WORK",
        description="Client work: asset inventory, health checks, bilingual ticket summaries.",
        prompt=(
            "# Work mode ({work_name})\n"
            "You are the user's assistant for client IT work at {work_name}.\n"
            "- Keep a professional tone. No jokes in anything a client might read.\n"
            "- Use the asset inventory and client health checks to answer questions about "
            "client machines, and record new assets when the user mentions them.\n"
            "- Turn ticket notes into client-ready summaries in {languages}: what was reported, "
            "what was done, the result, and any next steps for the client. Leave out internal "
            "jargon, passwords and anything the client doesn't need.\n"
            "- Never invent ticket numbers, prices, hostnames or dates; ask when they are missing."
        ),
        groups=("work",),
        starters=(
            ("Summarise ticket", "Turn these ticket notes into a client summary: "),
            ("Client health", "Run the health checks for all clients and summarise any problems."),
            ("Inventory", "List the assets we have for each client."),
            ("Add asset", "Add an asset: "),
        ),
    ),
}


def get_mode(mode_id: str | None) -> Mode:
    return MODES.get(mode_id or "default", MODES["default"])


def mode_prompt(mode_id: str | None, prefs: Preferences) -> str:
    mode = get_mode(mode_id)
    if not mode.prompt:
        return ""
    languages = " and ".join(prefs.work_languages) or "English"
    return mode.prompt.format(work_name=prefs.work_name or "work", languages=languages)


def public_modes(prefs: Preferences) -> list[dict[str, Any]]:
    out = []
    for mode in MODES.values():
        label = prefs.work_name if mode.id == "work" and prefs.work_name else mode.label
        out.append(
            {
                "id": mode.id,
                "label": label,
                "code": mode.code,
                "description": mode.description,
                "starters": [{"title": t, "text": p} for t, p in mode.starters],
            }
        )
    return out
