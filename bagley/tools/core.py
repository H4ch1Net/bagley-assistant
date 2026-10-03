"""Time and math tools."""

from __future__ import annotations

import ast
import math
import operator
import re
from datetime import datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bagley.tools import ToolError, tool


@tool(category="utility", summary="Check the time in {timezone}")
def get_current_time(
    timezone: Annotated[
        str | None, "IANA timezone such as 'Europe/London'. Omit for the user's local time."
    ] = None,
) -> dict[str, str]:
    """Get the current date and time, optionally in a specific timezone."""
    if timezone:
        try:
            now = datetime.now(ZoneInfo(timezone))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ToolError(
                f"Unknown timezone '{timezone}'. Use an IANA name like 'Asia/Tokyo'."
            ) from exc
    else:
        now = datetime.now().astimezone()
    return {
        "iso": now.isoformat(timespec="seconds"),
        "readable": now.strftime("%A, %d %B %Y, %H:%M"),
        "timezone": timezone or str(now.tzinfo),
    }


_BIN_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type, Any] = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS: dict[str, Any] = {
    name: getattr(math, name)
    for name in (
        "sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "atan2", "sinh", "cosh", "tanh",
        "log", "log10", "log2", "exp", "floor", "ceil", "factorial", "radians", "degrees",
        "hypot", "gcd", "comb", "perm",
    )
}  # fmt: skip
_FUNCS.update({"abs": abs, "round": round, "min": min, "max": max})
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf}


def safe_eval(expression: str) -> float | int:
    """Evaluate an arithmetic expression without ``eval``."""
    if len(expression) > 500:
        raise ToolError("Expression is too long.")
    expr = expression.replace("^", "**").replace("×", "*").replace("÷", "/")
    expr = _THOUSANDS.sub("", expr)
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ToolError(f"Not a valid expression: {expression}") from exc

    def walk(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Name) and node.id in _CONSTS:
            return _CONSTS[node.id]
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
            return _UNARY_OPS[type(node.op)](walk(node.operand))
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 1000 or abs(left) > 10**100):
                left = float(left)  # Overflows cleanly instead of building a giant integer.
            return _BIN_OPS[type(node.op)](left, right)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _FUNCS
            and not node.keywords
        ):
            args = [walk(a) for a in node.args]
            if node.func.id in ("factorial", "comb", "perm") and any(
                isinstance(a, (int, float)) and abs(a) > 5000 for a in args
            ):
                raise ToolError(f"{node.func.id} argument too large.")
            return _FUNCS[node.func.id](*args)
        raise ToolError(f"Unsupported syntax in expression: {ast.unparse(node)}")

    try:
        result = walk(tree)
    except ZeroDivisionError as exc:
        raise ToolError("Division by zero.") from exc
    except OverflowError as exc:
        raise ToolError("The result is too large to compute.") from exc
    except (TypeError, ValueError) as exc:
        raise ToolError(f"Math error: {exc}") from exc
    if isinstance(result, float) and result.is_integer() and abs(result) < 1e15:
        return int(result)
    return result


@tool(category="utility", summary="Calculate {expression}")
def calculate(
    expression: Annotated[str, "Arithmetic expression, e.g. '0.17 * 2340' or 'sqrt(2) * pi'"],
) -> str:
    """Evaluate a math expression exactly. Use this instead of doing arithmetic in your head."""
    result = safe_eval(expression)
    if isinstance(result, float):
        result = round(result, 10)
    return f"{expression} = {result}"
