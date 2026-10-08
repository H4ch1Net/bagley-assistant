"""Private knowledge base over the user's own folders.

Files are split into passages and indexed with SQLite FTS5 (keyword search with stemming). When
an embedding model is available on the model server (for example ``nomic-embed-text`` in Ollama),
passages are also embedded and search becomes hybrid: keyword and semantic rankings are merged.
Everything stays in ``<data dir>/knowledge.db``.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import logging
import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bagley.runtime import Runtime

log = logging.getLogger("bagley.knowledge")

TEXT_SUFFIXES = {
    ".md", ".markdown", ".txt", ".rst", ".org", ".adoc", ".tex", ".log", ".py", ".js", ".ts", ".tsx",
    ".jsx", ".java", ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt",
    ".sql", ".sh", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".json", ".csv", ".tsv", ".html", ".htm",
    ".xml", ".css", ".scss", ".vue", ".svelte", ".lua", ".r", ".m", ".ipynb",
}  # fmt: skip
SKIP_DIRS = {"node_modules", "__pycache__", "venv", "dist", "build", "target", "site-packages"}
MAX_FILE_BYTES = 2_000_000
MAX_FILES = 20_000
CHUNK_CHARS = 900
EMBED_BATCH = 32
EMBED_NAMES = ("embed", "minilm", "bge-", "e5-", "gte-", "arctic-embed", "granite-embedding")
REINDEX_EVERY = 15 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path       TEXT PRIMARY KEY,
    folder     TEXT NOT NULL,
    mtime      REAL NOT NULL,
    size       INTEGER NOT NULL,
    chunks     INTEGER NOT NULL,
    indexed_at REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(path UNINDEXED, heading, body, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS vectors (
    id    INTEGER PRIMARY KEY,
    model TEXT NOT NULL,
    vec   BLOB NOT NULL
);
"""


def is_embedding_model(name: str) -> bool:
    return any(part in name.lower() for part in EMBED_NAMES)


@dataclass
class Passage:
    heading: str
    body: str


def read_text(path: Path) -> str:
    """Best-effort text of a supported file ("" if it isn't one)."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader  # Optional dependency: pip install bagley-assistant[pdf]
        except ImportError:
            return ""
        try:
            return "\n\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
        except Exception:
            return ""
    if suffix not in TEXT_SUFFIXES:
        return ""
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if b"\0" in raw[:4096]:
        return ""
    text = raw.decode("utf-8", "replace")
    if suffix in (".html", ".htm"):
        from bagley.tools.web import html_to_text

        title, body = html_to_text(text)
        return f"# {title}\n\n{body}" if title else body
    return text


def chunk(text: str, title: str) -> list[Passage]:
    """Split into passages of about CHUNK_CHARS, tracking the nearest Markdown heading."""
    passages: list[Passage] = []
    heading = title
    buf: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buf, size
        body = "\n\n".join(buf).strip()
        if body:
            passages.append(Passage(heading, body))
        buf, size = [], 0

    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        first = block.splitlines()[0]
        if re.match(r"#{1,6} ", first):
            flush()
            heading = f"{title} › {first.lstrip('#').strip()}"
        while len(block) > CHUNK_CHARS * 1.5:  # Long blocks (code, minified text): hard split.
            cut = block.rfind("\n", 0, CHUNK_CHARS) if "\n" in block[:CHUNK_CHARS] else CHUNK_CHARS
            cut = cut if cut > CHUNK_CHARS // 3 else CHUNK_CHARS
            if size:
                flush()
            buf, size = [block[:cut]], cut
            flush()
            block = block[cut:].lstrip()
        if size + len(block) > CHUNK_CHARS and size:
            flush()
        buf.append(block)
        size += len(block)
    flush()
    return passages


def _fts_query(text: str) -> str:
    words = [w for w in re.findall(r"\w+", text.lower()) if len(w) > 1][:16]
    return " OR ".join(f'"{w}"' for w in words)


def _normalise(vec: list[float]) -> array.array:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return array.array("f", (x / norm for x in vec))


class KnowledgeBase:
    def __init__(self, runtime: Runtime) -> None:
        self.rt = runtime
        self.path = Path(runtime.config.data_dir) / "knowledge.db"
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()
        self.state = "idle"
        self.error = ""
        self.progress = ""
        self.last_indexed: float | None = None
        self.embedding_model: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._pending = False  # Asked to reindex while a pass was running.
        self._loop_task: asyncio.Task[None] | None = None

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # Folders -------------------------------------------------------------------------------

    def folders(self) -> list[Path]:
        prefs, _ = self.rt.preferences()
        out = [Path(self.rt.config.workspace or ".").resolve()]
        for raw in [*prefs.knowledge_folders, *prefs.vaults]:  # Obsidian vaults too.
            path = Path(raw).expanduser().resolve()
            if path not in out:
                out.append(path)
        return out

    @staticmethod
    def label(folder: Path) -> str:
        return folder.name or str(folder)

    def display_path(self, path: str) -> str:
        for folder in self.folders():
            with contextlib.suppress(ValueError):
                return f"{self.label(folder)}/{Path(path).relative_to(folder).as_posix()}"
        return path

    # Indexing ------------------------------------------------------------------------------

    def start(self) -> None:
        """Index now and then every few minutes (server mode)."""
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        for task in (self._loop_task, self._task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _loop(self) -> None:
        while True:
            await self.reindex()
            await asyncio.sleep(REINDEX_EVERY)

    def request_reindex(self) -> None:
        if self.state == "indexing" or (self._task and not self._task.done()):
            self._pending = True
            return
        self._task = asyncio.create_task(self.reindex())

    async def reindex(self) -> dict[str, int]:
        if self.state == "indexing":
            self._pending = True  # The running pass goes round again when it finishes.
            return {}
        self.state, self.error = "indexing", ""
        await self._broadcast()
        try:
            while True:
                self._pending = False
                counts = await asyncio.to_thread(self._scan)
                await self._embed_missing()
                if not self._pending:
                    break
            self.last_indexed = time.time()
            return counts
        except Exception as exc:
            log.exception("Indexing failed")
            self.error = str(exc)
            return {}
        finally:
            self.state, self.progress = "idle", ""
            await self._broadcast()

    def _walk(self) -> list[tuple[Path, Path]]:
        found: list[tuple[Path, Path]] = []
        for folder in self.folders():
            if not folder.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(folder):
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]
                for name in filenames:
                    if name.startswith("."):
                        continue
                    path = Path(dirpath) / name
                    suffix = path.suffix.lower()
                    if suffix in TEXT_SUFFIXES or suffix == ".pdf":
                        found.append((folder, path))
                        if len(found) >= MAX_FILES:
                            return found
        return found

    def _scan(self) -> dict[str, int]:
        files = self._walk()
        with self._lock:
            known = {
                r["path"]: (r["mtime"], r["size"])
                for r in self._db.execute("SELECT path, mtime, size FROM files")
            }
        seen: set[str] = set()
        added = updated = 0
        for i, (folder, path) in enumerate(files):
            key = str(path)
            seen.add(key)
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_size > MAX_FILE_BYTES:
                continue
            if known.get(key) == (st.st_mtime, st.st_size):
                continue
            self.progress = f"{i + 1}/{len(files)}"
            text = read_text(path)
            passages = chunk(text, path.stem) if text.strip() else []
            with self._lock:
                self._delete_file(key)
                for p in passages:
                    self._db.execute(
                        "INSERT INTO passages (path, heading, body) VALUES (?, ?, ?)",
                        (key, p.heading, p.body),
                    )
                self._db.execute(
                    "INSERT INTO files (path, folder, mtime, size, chunks, indexed_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (key, str(folder), st.st_mtime, st.st_size, len(passages), time.time()),
                )
                self._db.commit()
            if key in known:
                updated += 1
            else:
                added += 1
        removed = 0
        with self._lock:
            for key in set(known) - seen:
                self._delete_file(key)
                removed += 1
            self._db.commit()
        return {"added": added, "updated": updated, "removed": removed}

    def _delete_file(self, key: str) -> None:
        self._db.execute(
            "DELETE FROM vectors WHERE id IN (SELECT rowid FROM passages WHERE path = ?)", (key,)
        )
        self._db.execute("DELETE FROM passages WHERE path = ?", (key,))
        self._db.execute("DELETE FROM files WHERE path = ?", (key,))

    def forget_folder(self, folder: Path) -> None:
        with self._lock:
            keys = [
                r["path"]
                for r in self._db.execute("SELECT path FROM files WHERE folder = ?", (str(folder),))
            ]
            for key in keys:
                self._delete_file(key)
            self._db.commit()

    async def resolve_embedding_model(self) -> str | None:
        prefs, _ = self.rt.preferences()
        if prefs.embedding_model == "off":
            return None
        if prefs.embedding_model:
            return prefs.embedding_model
        try:
            provider = await self.rt.provider()
            if provider.kind != "ollama":
                return None
            models = await provider.list_models()
        except Exception:
            return None
        return next((m.name for m in models if is_embedding_model(m.name)), None)

    async def _embed_missing(self) -> None:
        model = await self.resolve_embedding_model()
        self.embedding_model = model
        if not model:
            return
        with self._lock:
            self._db.execute("DELETE FROM vectors WHERE model != ?", (model,))
            todo = self._db.execute(
                "SELECT rowid, heading, body FROM passages WHERE rowid NOT IN (SELECT id FROM vectors)"
            ).fetchall()
        if not todo:
            return
        provider = await self.rt.provider()
        for start in range(0, len(todo), EMBED_BATCH):
            batch = todo[start : start + EMBED_BATCH]
            self.progress = f"embedding {start + len(batch)}/{len(todo)}"
            try:
                vectors = await provider.embed(
                    [f"{r['heading']}\n{r['body']}" for r in batch], model=model
                )
            except Exception as exc:
                self.error = f"Embedding with {model} failed: {getattr(exc, 'message', exc)}"
                self.embedding_model = None
                return
            with self._lock:
                self._db.executemany(
                    "INSERT OR REPLACE INTO vectors (id, model, vec) VALUES (?, ?, ?)",
                    [
                        (r["rowid"], model, _normalise(v).tobytes())
                        for r, v in zip(batch, vectors, strict=False)
                    ],
                )
                self._db.commit()

    async def _broadcast(self) -> None:
        await self.rt.broadcast({"type": "knowledge.changed", "status": self.status()})

    # Queries -------------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            files = self._db.execute(
                "SELECT count(*), coalesce(sum(chunks), 0) FROM files"
            ).fetchone()
            vectors = self._db.execute("SELECT count(*) FROM vectors").fetchone()[0]
            per_folder = {
                r["folder"]: r["n"]
                for r in self._db.execute("SELECT folder, count(*) AS n FROM files GROUP BY folder")
            }
        workspace = Path(self.rt.config.workspace or ".").resolve()
        prefs, _ = self.rt.preferences()
        added = {Path(p).expanduser().resolve() for p in prefs.knowledge_folders}
        vaults = {Path(p).expanduser().resolve() for p in prefs.vaults} - added
        return {
            "state": self.state,
            "progress": self.progress,
            "error": self.error,
            "files": files[0],
            "passages": files[1],
            "embedded": vectors,
            "embedding_model": self.embedding_model,
            "last_indexed": self.last_indexed,
            "folders": [
                {
                    "path": str(f),
                    "label": self.label(f),
                    "files": per_folder.get(str(f), 0),
                    "exists": f.is_dir(),
                    # Vaults are listed under "Your life" and removed there.
                    "removable": f != workspace and f not in vaults,
                    "vault": f in vaults,
                }
                for f in self.folders()
            ],
        }

    async def search(self, query: str, limit: int = 6) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 20))
        fts = _fts_query(query)
        keyword: list[int] = []
        if fts:
            with self._lock:
                keyword = [
                    r[0]
                    for r in self._db.execute(
                        "SELECT rowid FROM passages WHERE passages MATCH ? ORDER BY bm25(passages, 0, 2.0, 1.0) LIMIT 50",
                        (fts,),
                    )
                ]
        semantic = await self._semantic(query, 50)
        scores: dict[int, float] = {}
        for ranking in (keyword, semantic):
            for rank, rowid in enumerate(ranking):
                scores[rowid] = scores.get(rowid, 0.0) + 1.0 / (
                    60 + rank
                )  # Reciprocal rank fusion.
        best = sorted(scores, key=scores.get, reverse=True)[:limit]  # type: ignore[arg-type]
        if not best:
            return []
        with self._lock:
            rows = {
                r["rowid"]: r
                for r in self._db.execute(
                    f"SELECT rowid, path, heading, body FROM passages WHERE rowid IN ({','.join('?' * len(best))})",
                    tuple(best),
                )
            }
        hits = []
        for rowid in best:
            r = rows.get(rowid)
            if not r:
                continue
            hits.append(
                {
                    "path": self.display_path(r["path"]),
                    "section": r["heading"],
                    "text": r["body"][:700],
                    "match": "both"
                    if rowid in keyword and rowid in semantic
                    else "keyword"
                    if rowid in keyword
                    else "meaning",
                }
            )
        return hits

    async def _semantic(self, query: str, limit: int) -> list[int]:
        model = self.embedding_model
        if not model:
            return []
        try:
            provider = await self.rt.provider()
            qvec = _normalise((await provider.embed([query], model=model))[0])
        except Exception as exc:
            log.debug("Query embedding failed: %s", exc)
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT id, vec FROM vectors WHERE model = ?", (model,)
            ).fetchall()
        if not rows:
            return []
        try:
            import numpy as np  # Optional speed-up.

            matrix = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float32).reshape(
                len(rows), -1
            )
            sims = matrix @ np.frombuffer(qvec.tobytes(), dtype=np.float32)
            order = np.argsort(-sims)[:limit]
            return [rows[i]["id"] for i in order if sims[i] > 0.25]
        except ImportError:
            pass
        scored = []
        for r in rows:
            vec = array.array("f")
            vec.frombytes(r["vec"])
            if len(vec) == len(qvec):
                scored.append((sum(a * b for a, b in zip(vec, qvec, strict=False)), r["id"]))
        scored.sort(reverse=True)
        return [rowid for sim, rowid in scored[:limit] if sim > 0.25]

    def resolve_document(self, display: str) -> Path | None:
        """Map a path shown in search results back to an indexed file."""
        with self._lock:
            paths = [r["path"] for r in self._db.execute("SELECT path FROM files")]
        for key in paths:
            if self.display_path(key) == display or key == display:
                return Path(key)
        return None
