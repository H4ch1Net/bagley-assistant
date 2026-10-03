"""Search and read the user's private knowledge base (their indexed folders)."""

from __future__ import annotations

from typing import Annotated, Any

from bagley.tools import ToolContext, ToolError, tool

MAX_CHARS = 12_000


def _kb(ctx: ToolContext) -> Any:
    kb = getattr(ctx.runtime, "knowledge", None)
    if kb is None:
        raise ToolError("The knowledge base is not available here.")
    return kb


@tool(category="knowledge", summary="Search my files for “{query}”")
async def search_knowledge(
    ctx: ToolContext,
    query: Annotated[str, "What to look for, in the user's words"],
    limit: Annotated[int, "Number of passages (1-10)"] = 5,
) -> list[dict[str, Any]] | str:
    """Search the user's own documents and notes (their knowledge base folders) by keyword and
    meaning. Use it for questions about their notes, projects, documents or anything they wrote."""
    hits = await _kb(ctx).search(query, max(1, min(limit, 10)))
    return hits or "Nothing in the knowledge base matches. Try other words."


@tool(category="knowledge", summary="Read {path}")
def read_document(
    ctx: ToolContext,
    path: Annotated[str, "Path exactly as shown in search_knowledge results"],
    offset: Annotated[int, "Character offset to continue from"] = 0,
) -> dict[str, Any]:
    """Read a whole document from the knowledge base, e.g. after search_knowledge found it."""
    from bagley.knowledge import read_text

    kb = _kb(ctx)
    file = kb.resolve_document(path)
    if file is None or not file.is_file():
        raise ToolError(f"'{path}' is not in the knowledge base. Use a path from search_knowledge.")
    text = read_text(file)
    start = max(0, offset)
    part = text[start : start + MAX_CHARS]
    end = start + len(part)
    return {
        "path": path,
        "content": part,
        "total_chars": len(text),
        "next_offset": end if end < len(text) else None,
    }
