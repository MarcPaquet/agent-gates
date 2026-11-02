"""The task store: one JSON file holding every task, read and written safely.

The store is `{"version": 1, "items": {<task id>: <item>}}`. An item has these keys, in this order:
id, title, state, contract, owns, dispatched_to, dispatched_at, pr, review_cycles, verdicts,
merge_approved_head, merge_approved_at, escalation, history.

Writes are atomic (temp file, fsync, rename) and commands that change the store hold an exclusive file lock
from the moment they read it until they have written it back.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gates import machine

STORE_VERSION = 1
ITEM_ID_PATTERN = re.compile(r"T-\d{3,}")
LOCK_RETRY_SECONDS = 0.02


class StoreError(machine.GateError):
    """The store file cannot be read, written or locked, or a new item is not valid."""


def now_iso() -> str:
    """Return the current UTC time as ISO 8601 to the second, for example `2026-11-02T18:27:37Z`."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_store_path() -> Path:
    """Return the default store location: `gates.json` at the repository root."""
    return Path(__file__).resolve().parents[1] / "gates.json"


def empty_store() -> dict[str, Any]:
    """Return a store with no tasks."""
    return {"version": STORE_VERSION, "items": {}}


def _next_item_id(store: Mapping[str, Any]) -> str:
    """Return the id one above the highest existing `T-NNN` id (`T-001` when there is none)."""
    highest = 0
    for existing_id in store["items"]:
        if ITEM_ID_PATTERN.fullmatch(existing_id):
            highest = max(highest, int(existing_id[2:]))
    return f"T-{highest + 1:03d}"


def new_item(
    store: Mapping[str, Any],
    *,
    title: str,
    contract: str,
    owns: list[str],
    actor: str,
    at: str,
    item_id: str | None = None,
) -> dict[str, Any]:
    """Return a new store with one more task in state `draft`; the input store is not changed.

    The id is `item_id` when given, otherwise the next free `T-NNN`. Raises `StoreError` for a duplicate
    id, an id that is not `T-` followed by at least three digits, an empty title, an empty contract path,
    or an empty `owns` list or an empty glob in it.
    """
    if item_id is None:
        item_id = _next_item_id(store)
    if not ITEM_ID_PATTERN.fullmatch(item_id):
        raise StoreError(
            f"The task id '{item_id}' is not valid. Use 'T-' followed by at least three digits, for example T-001."
        )
    if item_id in store["items"]:
        raise StoreError(
            f"The task {item_id} already exists. Choose another id or leave the id out to get the next free one."
        )
    if not title.strip():
        raise StoreError("The title is empty. Give the task a title that says what it delivers.")
    if not contract.strip():
        raise StoreError("The contract path is empty. Give the path of the task's contract file.")
    if not owns:
        raise StoreError("The task owns no paths. Give at least one glob, for example 'src/**'.")
    if any(not glob.strip() for glob in owns):
        raise StoreError("One of the owned globs is empty. Remove it or give a path pattern.")

    item: dict[str, Any] = {
        "id": item_id,
        "title": title,
        "state": "draft",
        "contract": contract,
        "owns": list(owns),
        "dispatched_to": None,
        "dispatched_at": None,
        "pr": None,
        "review_cycles": 0,
        "verdicts": [],
        "merge_approved_head": None,
        "merge_approved_at": None,
        "escalation": None,
        "history": [{"at": at, "from": None, "to": "draft", "actor": actor, "reason": "created"}],
    }
    updated = copy.deepcopy(dict(store))
    updated["items"][item_id] = item
    return updated


def load(path: Path) -> dict[str, Any]:
    """Read the store at `path`; a missing file gives an empty store. Raises `StoreError` if it is invalid."""
    if not path.exists():
        return empty_store()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise StoreError(
            f"The store file {path} is not valid JSON ({error}). "
            "Restore it from git or fix it, then run the command again."
        ) from error
    if not isinstance(data, dict):
        raise StoreError(f"The store file {path} does not hold a JSON object. Restore it from git.")
    if data.get("version") != STORE_VERSION:
        raise StoreError(
            f"The store file {path} has version {data.get('version')!r}, but this kit reads version {STORE_VERSION}. "
            "Use the kit version that wrote the file."
        )
    if not isinstance(data.get("items"), dict):
        raise StoreError(f"The store file {path} has no 'items' object. Restore it from git.")
    return data


def save(path: Path, store: Mapping[str, Any]) -> None:
    """Write the store to `path` atomically: temp file in the same directory, flush, fsync, rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(store, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
    temp_file = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False
    )
    try:
        with temp_file:
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_file.name, path)
    except BaseException:
        Path(temp_file.name).unlink(missing_ok=True)
        raise


@contextmanager
def locked(path: Path, *, timeout: float = 5.0) -> Iterator[dict[str, Any]]:
    """Hold an exclusive lock on `<path>.lock`, yield the loaded store, and save it on a clean exit.

    If the block raises, nothing is saved and the exception propagates. Raises `StoreError` when the lock
    is still held by another command after `timeout` seconds. The lock file stays on disk.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a") as lock_file:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StoreError(
                        f"Another command holds the store lock ({lock_path}). "
                        "Wait for it to finish and run the command again."
                    ) from None
                time.sleep(LOCK_RETRY_SECONDS)
        try:
            data = load(path)
            yield data
            save(path, data)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
