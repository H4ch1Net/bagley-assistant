"""Extra ``bagley`` subcommands.

Every module in this package that defines ``register(subparsers)`` adds its own subcommands.
``register`` creates a parser and sets ``func`` to a handler that takes the parsed arguments and
returns an exit code:

    def register(sub):
        p = sub.add_parser("hello", help="Say hello")
        p.set_defaults(func=lambda args: print("hello") or 0)
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil


def register_all(subparsers: argparse._SubParsersAction) -> None:
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        module = importlib.import_module(f"{__name__}.{info.name}")
        register = getattr(module, "register", None)
        if callable(register):
            register(subparsers)
