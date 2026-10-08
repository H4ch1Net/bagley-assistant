"""Run the collectors together and format the result for the chat, the terminal and
notifications.

    report = await collect(rt, ["ports", "ssh"])
    report.markdown()   # For the chat.
    report.text()       # For the terminal (pass ``paint`` for colours).
    report.headline()   # "2 CRIT · 3 WARN // 41 FAILED SSH LOGINS · /home 92%"
    report.level()      # Notification level: critical, important or info.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from bagley.watchdog.baseline import Baseline
from bagley.watchdog.collectors import (
    COLLECTORS,
    RANK,
    STATUSES,
    Collector,
    Context,
    Section,
    clean,
)

if TYPE_CHECKING:
    from bagley.runtime import Runtime

log = logging.getLogger("bagley.watchdog")

LABELS = {"ok": "OK", "info": "INFO", "warn": "WARN", "crit": "CRIT", "unavailable": "N/A"}
LEVELS = {"crit": "critical", "warn": "important", "ok": "info"}
SECURITY_SECTIONS = ["network", "ports", "ssh", "vulns"]
LAST_REPORT = "cache.last_report"


def section_ids(value: str | Iterable[str] | None) -> list[str]:
    """Validated section ids in their usual order: all of them for None or nothing. Takes a
    comma-separated string or a list. Raises ValueError naming unknown ids."""
    if value is None:
        return list(COLLECTORS)
    parts = [value] if isinstance(value, str) else [str(v) for v in value]
    wanted = {p.strip().lower() for part in parts for p in part.split(",") if p.strip()}
    unknown = sorted(wanted - set(COLLECTORS))
    if unknown:
        raise ValueError(
            f"Unknown section(s): {', '.join(clean(u, 30) for u in unknown[:5])}. "
            f"Choose from: {', '.join(COLLECTORS)}."
        )
    return [i for i in COLLECTORS if i in wanted] or list(COLLECTORS)


def context_for(rt: Runtime) -> Context:
    """The collectors' view of this computer, kept in ``rt.services``. Tests put a ``Context``
    with fake programs there."""
    ctx = rt.services.get("watchdog")
    if ctx is None:
        ctx = rt.services["watchdog"] = Context(baseline=Baseline(rt.store))
    return ctx


def hostname() -> str:
    return clean(socket.gethostname().split(".")[0], 24).upper() or "LOCALHOST"


def _code(text: str) -> str:
    """Text for a Markdown code span or block: no backticks to break out of it."""
    return text.replace("`", "'")


@dataclass
class Report:
    sections: list[Section]
    created_at: float = field(default_factory=time.time)
    host: str = field(default_factory=hostname)
    title: str = "SYSTEM BRIEFING"

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(STATUSES, 0)
        for sec in self.sections:
            counts[sec.status] += 1
        return counts

    def status(self) -> str:
        """``crit``, ``warn`` or ``ok`` (INFO counts as OK)."""
        worst = max((RANK[s.status] for s in self.sections), default=0)
        return "crit" if worst >= RANK["crit"] else "warn" if worst >= RANK["warn"] else "ok"

    def level(self) -> str:
        """Notification level: critical with any CRIT, important with any WARN, else info."""
        return LEVELS[self.status()]

    def title_line(self) -> str:
        stamp = datetime.fromtimestamp(self.created_at).strftime("%d%m%y")
        return f"{self.title} // {stamp} // {self.host}"

    def flagged(self) -> list[Section]:
        """CRIT sections, then WARN sections, each in their usual order."""
        return [s for s in self.sections if s.status == "crit"] + [
            s for s in self.sections if s.status == "warn"
        ]

    def headline(self) -> str:
        """One line for a notification, e.g. ``2 CRIT · 3 WARN // 41 FAILED SSH LOGINS``."""
        counts = self.counts()
        tally = " · ".join(f"{counts[k]} {LABELS[k]}" for k in ("crit", "warn") if counts[k])
        if not tally:
            fine = f"{counts['ok'] + counts['info']} OK"
            missing = f" · {counts['unavailable']} N/A" if counts["unavailable"] else ""
            return f"ALL CLEAR // {fine}{missing}"
        notes = " · ".join(s.summary.split(" // ")[0] for s in self.flagged()[:3])
        return clean(f"{tally} // {notes}", 200)

    def markdown(self) -> str:
        """The report for the chat: a status line per section, then details of the others."""
        width = max((len(s.title) for s in self.sections), default=0) + 2
        lines = [f"**{_code(self.title_line())}**", "", "```text"]
        for sec in self.sections:
            label = f"[{LABELS[sec.status]}]".ljust(7)
            lines.append(_code(f"{label}{sec.title.ljust(width)}{sec.summary}"))
        lines.append("```")
        for sec in self.sections:
            if sec.status in ("ok", "unavailable") or not sec.findings:
                continue
            lines += ["", f"**[{LABELS[sec.status]}] {sec.title}**"]
            for f in sec.findings:
                text = f"{f.text} — {f.detail}" if f.detail else f.text
                lines.append(f"- `{LABELS[f.severity]}` `{_code(text)}`")
        lines += ["", f"`{_code(self.headline())}`"]
        return "\n".join(lines)

    def text(self, paint: Callable[[str, str], str] | None = None) -> str:
        """The report for a terminal. ``paint(role, text)`` adds colour; roles are the
        statuses plus ``title``, ``label`` and ``body``."""

        def p(role: str, text: str) -> str:
            return paint(role, text) if paint else text

        width = max((len(s.title) for s in self.sections), default=0) + 2
        out = [p("title", self.title_line())]
        for sec in self.sections:
            label = f"[{LABELS[sec.status]}]".ljust(7)
            out.append(
                p(sec.status, label) + p("label", sec.title.ljust(width)) + p("body", sec.summary)
            )
            if sec.status in ("ok", "unavailable"):
                continue
            for f in sec.findings:
                line = " " * 7 + p(f.severity, LABELS[f.severity].ljust(5)) + p("body", f.text)
                out.append(line + (p("label", f"  {f.detail}") if f.detail else ""))
        out += [p("label", "--"), p(self.status(), self.headline())]
        return "\n".join(out)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "title_line": self.title_line(),
            "host": self.host,
            "created_at": self.created_at,
            "status": self.status(),
            "level": self.level(),
            "headline": self.headline(),
            "counts": self.counts(),
            "sections": [s.to_dict() for s in self.sections],
        }


async def _run_once(item: Collector, ctx: Context) -> Section:
    try:
        return await asyncio.wait_for(item.func(ctx), item.timeout)
    except asyncio.TimeoutError:
        return Section(item.id, item.title).unavailable(f"TIMED OUT AFTER {item.timeout:g}S")
    except Exception as exc:
        log.exception("Watchdog collector %s failed", item.id)
        reason = clean(exc, 80) or exc.__class__.__name__
        return Section(item.id, item.title).unavailable(f"ERROR: {reason}")


async def _run(item: Collector, ctx: Context) -> Section:
    """Run a collector, or join the run already in progress (a slow check such as debsecan
    shouldn't run twice because the chat and a briefing asked together)."""
    task = ctx.running.get(item.id)
    if task is None:
        task = asyncio.ensure_future(_run_once(item, ctx))
        ctx.running[item.id] = task
        task.add_done_callback(
            lambda done: ctx.running.pop(item.id) if ctx.running.get(item.id) is done else None
        )
    return await asyncio.shield(task)  # One caller giving up doesn't stop the others.


async def collect(
    rt: Runtime | None,
    sections: str | Iterable[str] | None = None,
    *,
    context: Context | None = None,
) -> Report:
    """Run the chosen collectors (all by default) at once, each with its own timeout. Raises
    ValueError for unknown sections."""
    ids = section_ids(sections)
    if context is None:
        assert rt is not None, "collect needs a runtime or a context"
        context = context_for(rt)
    found = await asyncio.gather(*(_run(COLLECTORS[i], context) for i in ids))
    report = Report(list(found), created_at=context.clock())
    if context.baseline is not None and len(ids) == len(COLLECTORS):
        context.baseline.set(LAST_REPORT, report.to_dict(), report.created_at)
    return report


def last_report(baseline: Baseline) -> dict[str, Any] | None:
    """The most recent full report, as ``Report.to_dict`` left it."""
    found = baseline.get(LAST_REPORT)
    return found if isinstance(found, dict) else None
