"""What an automation may do when nobody is watching.

Scheduled tasks run the agent unattended, and anything they read from the web may contain
instructions aimed at the model. The policy keeps such a run from changing state, and once web
content is in the context it stops the run from reading private data or opening new addresses,
so injected text can't send your files or notes anywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from bagley.tools import Tool

BRINGS_WEB = {"web_search", "fetch_webpage"}
READS_PRIVATE = {
    "search_knowledge",
    "read_document",
    "read_file",
    "list_files",
    "search_files",
    "system_status",
    "list_automations",
    "search_chats",
}
CHANGES_STATE = {"remember", "forget", "set_reminder", "cancel_automation", "make_directory"}
URL = re.compile(r"https?://[^\s\"'<>)\]]+")


@dataclass
class UnattendedPolicy:
    web_content: bool = False  # Untrusted text from the web is in the context.
    urls: set[str] = field(default_factory=set)  # Pages that may still be opened after that.

    @classmethod
    def for_prompt(cls, prompt: str) -> UnattendedPolicy:
        return cls(urls={u.rstrip(".,;") for u in URL.findall(prompt)})

    def for_helper(self, prompt: str) -> UnattendedPolicy:
        """A helper started from this run carries its state. Once web content has been read, the
        helper's task text may have been steered by it, so addresses in it aren't trusted."""
        helper = UnattendedPolicy(web_content=self.web_content, urls=set(self.urls))
        if not self.web_content:
            helper.urls |= UnattendedPolicy.for_prompt(prompt).urls
        return helper

    def check(self, tool: Tool, args: dict[str, Any]) -> str | None:
        """Why ``tool`` may not run now, or None if it may."""
        if tool.risk == "confirm":
            return "it needs approval, and nobody is there to give it during a scheduled run."
        if tool.name in CHANGES_STATE:
            return "scheduled runs can't change memories, automations or folders."
        if not self.web_content:
            return None
        if tool.name in READS_PRIVATE or tool.source != "builtin":
            return (
                "after reading web content, a scheduled run can't read private data "
                "or use plugin tools."
            )
        if tool.name == "fetch_webpage" and str(args.get("url", "")).strip() not in self.urls:
            return (
                "after reading web content, a scheduled run can only open pages from "
                "search results or the task itself."
            )
        return None

    def observe(self, tool: Tool, result: str) -> None:
        if tool.name in BRINGS_WEB:
            self.web_content = True
        if tool.name == "web_search":
            self.urls.update(u.rstrip(".,;") for u in URL.findall(result))
