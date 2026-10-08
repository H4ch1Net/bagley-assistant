"""Shell co-pilot endpoints, used by ``bagley suggest`` / ``bagley why`` and the zsh widget.

* ``POST /api/shell/suggest`` turns a request into one command line, flagged when dangerous.
* ``POST /api/shell/why`` explains a failed command from its status and output, with a fix.
* ``GET /api/shell/facts`` shows what suggestions know about this machine.

Each call is one completion on the light route (no tools, nothing is executed) and shows in the
activity feed with source "shell".
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from bagley import shellhelp
from bagley.api import runtime
from bagley.llm import LLMError

router = APIRouter()


class SuggestBody(BaseModel):
    request: str = Field(max_length=4000)
    cwd: str = Field(default="", max_length=4096)
    shell: str = Field(default="zsh", max_length=64)


class WhyBody(BaseModel):
    command: str = Field(default="", max_length=20_000)
    status: int | None = Field(default=None, ge=-1, le=100_000)
    output: str = Field(default="", max_length=2_000_000)  # Cut to head and tail before use.
    cwd: str = Field(default="", max_length=4096)
    shell: str = Field(default="zsh", max_length=64)


def _failed(exc: LLMError) -> HTTPException:
    return HTTPException(502, f"{exc.message} {exc.hint}".strip())


@router.post("/api/shell/suggest")
async def suggest(body: SuggestBody, request: Request) -> dict[str, Any]:
    if not body.request.strip():
        raise HTTPException(422, "Say what the command should do.")
    try:
        result = await shellhelp.suggest(runtime(request), body.request, body.cwd, body.shell)
    except LLMError as exc:
        raise _failed(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return result.to_dict()


@router.post("/api/shell/why")
async def why(body: WhyBody, request: Request) -> dict[str, Any]:
    if not body.command.strip() and not body.output.strip():
        raise HTTPException(422, "Nothing to explain: send the command or its output.")
    try:
        result = await shellhelp.why(
            runtime(request), body.command, body.status, body.output, body.cwd, body.shell
        )
    except LLMError as exc:
        raise _failed(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return result.to_dict()


@router.get("/api/shell/facts")
async def facts() -> dict[str, Any]:
    return shellhelp.system_facts().to_dict()
