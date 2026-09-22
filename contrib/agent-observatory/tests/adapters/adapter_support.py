"""Shared helpers for the adapter tests.

Imports ``agent_observatory`` by putting the package root on ``sys.path``, so
the tests work under the documented discovery command and under direct module
execution alike.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS_DIR = os.path.dirname(HERE)
PACKAGE_ROOT = os.path.dirname(TESTS_DIR)
if PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, PACKAGE_ROOT)

from agent_observatory.adapters import AdapterContext  # noqa: E402
from agent_observatory.inventory import records_to_jsonl  # noqa: E402
from agent_observatory.store import ObservatoryStore  # noqa: E402

FIXTURES = os.path.join(TESTS_DIR, "fixtures", "adapters")
CITY = "city-test"
HOST = "host-test"
CONTEXT = AdapterContext(city_id=CITY, host_id=HOST)


def fixture(provider: str, name: str = "sample.jsonl") -> str:
    """Return the absolute path of a fixture file."""

    return os.path.join(FIXTURES, provider, name)


def write_temp_jsonl(directory: str, records: list[dict[str, Any]], name: str = "records.jsonl") -> str:
    """Write normalized records as JSONL under *directory*."""

    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(records_to_jsonl(records))
    return path


def import_records(store_path: str, records: list[dict[str, Any]], directory: str, name: str = "records.jsonl"):
    """Import *records* into a projection and return the import result."""

    path = write_temp_jsonl(directory, records, name=name)
    with ObservatoryStore(store_path) as store:
        return store.import_jsonl(path)


def zstd_available() -> bool:
    """Return whether the ``zstd`` binary is usable in this environment."""

    return shutil.which("zstd") is not None


def compress_zstd(source: str, destination: str) -> None:
    """Compress *source* to *destination* using the ``zstd`` binary."""

    binary = shutil.which("zstd")
    if binary is None:
        raise RuntimeError("zstd binary is not available")
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(source, "rb") as reader, open(destination, "wb") as writer:
        process = subprocess.run([binary, "-q", "-c"], stdin=reader, stdout=writer, stderr=subprocess.PIPE, check=False)
    if process.returncode != 0:
        raise RuntimeError(process.stderr.decode("utf-8", "replace"))


def dsh_source(temp_dir: str, name: str = "sample.jsonl") -> str:
    """Compress the dsh fixture into a session layout and return the path."""

    target = os.path.join(
        temp_dir,
        ".dsh",
        "sessions",
        "--tmp--",
        "session-dsh-1",
        "session.v3.jsonl.zstd",
    )
    compress_zstd(fixture("dsh", name), target)
    return target


def claude_source(temp_dir: str, name: str = "sample.jsonl") -> str:
    """Copy the Claude fixture into a ``.claude/projects`` layout."""

    target = os.path.join(temp_dir, ".claude", "projects", "-tmp-proj", "session-file.jsonl")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copyfile(fixture("claude", name), target)
    return target


def codex_source(temp_dir: str, name: str = "sample.jsonl") -> str:
    """Copy the Codex fixture into a ``.codex/sessions`` layout."""

    target = os.path.join(temp_dir, ".codex", "sessions", "2026", "09", "21", "rollout-test.jsonl")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copyfile(fixture("codex", name), target)
    return target


def kinds(records: list[dict[str, Any]]) -> list[str]:
    return [record["kind"] for record in records]


def by_kind(records: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [record for record in records if record["kind"] == kind]


def make_temp_dir() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory()
