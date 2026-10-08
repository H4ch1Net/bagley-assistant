"""The watchdog: facts about this computer, compared with what it looked like before.

Collectors (``collectors.py``) each check one thing (failed services, journal errors, disks,
battery, updates, devices on the network, listening ports, SSH logins, vulnerable packages)
through an injectable command runner, so tests fake every program. ``report.py`` runs them
together and formats the result for the chat, the terminal and notifications. ``baseline.py``
remembers known devices, ports and peers, so new ones stand out. ``briefing.py`` is the
``briefing`` automation that posts the report every morning.
"""

from __future__ import annotations

from bagley.watchdog.collectors import COLLECTORS, Context, Finding, Section
from bagley.watchdog.report import Report, collect, context_for, section_ids

__all__ = [
    "COLLECTORS",
    "Context",
    "Finding",
    "Report",
    "Section",
    "collect",
    "context_for",
    "section_ids",
]
