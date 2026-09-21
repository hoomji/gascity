"""Conservative command categorization.

Commands are treated as inert text: this module never executes anything. It
splits compound shell commands on the usual control operators and recognizes
well-known programs/subcommands. Anything it does not confidently recognize is
reported as ``unknown`` rather than guessed at. A compound command can belong to
several categories at once.
"""

from __future__ import annotations

import re
import shlex
from typing import Iterable, Sequence

CATEGORIES = (
    "search",
    "read",
    "edit",
    "test",
    "lint",
    "typecheck",
    "build",
    "package",
    "git",
    "worktree",
    "ci",
    "review",
    "dispatch",
    "wait",
    "unknown",
)

_SHELL_OPERATORS = re.compile(r"&&|\|\||;|\n|\|")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPERS = {"sudo", "command", "env", "nice", "ionice", "nohup", "time", "stdbuf", "exec"}

_PROGRAM_CATEGORIES: dict[str, str] = {
    # search
    "rg": "search",
    "ripgrep": "search",
    "grep": "search",
    "egrep": "search",
    "fgrep": "search",
    "ag": "search",
    "ack": "search",
    "fd": "search",
    "fdfind": "search",
    "locate": "search",
    # read
    "cat": "read",
    "bat": "read",
    "head": "read",
    "tail": "read",
    "less": "read",
    "more": "read",
    "nl": "read",
    "wc": "read",
    "cut": "read",
    "sort": "read",
    "uniq": "read",
    "tr": "read",
    "strings": "read",
    "ls": "read",
    "tree": "read",
    "stat": "read",
    "file": "read",
    # edit
    "apply_patch": "edit",
    "patch": "edit",
    "tee": "edit",
    "truncate": "edit",
    "rm": "edit",
    "mv": "edit",
    "cp": "edit",
    "install": "edit",
    "chmod": "edit",
    "chown": "edit",
    "touch": "edit",
    "mkdir": "edit",
    "rmdir": "edit",
    "ln": "edit",
    "dd": "edit",
    "vim": "edit",
    "nvim": "edit",
    "nano": "edit",
    "emacs": "edit",
    # test runners
    "pytest": "test",
    "tox": "test",
    "jest": "test",
    "vitest": "test",
    "mocha": "test",
    "rspec": "test",
    "phpunit": "test",
    "bats": "test",
    "ctest": "test",
    "prove": "test",
    "gotestsum": "test",
    # lint
    "golangci-lint": "lint",
    "golint": "lint",
    "staticcheck": "lint",
    "revive": "lint",
    "ruff": "lint",
    "flake8": "lint",
    "pyflakes": "lint",
    "pylint": "lint",
    "eslint": "lint",
    "shellcheck": "lint",
    "hadolint": "lint",
    "markdownlint": "lint",
    "yamllint": "lint",
    "tflint": "lint",
    "vale": "lint",
    # typecheck
    "mypy": "typecheck",
    "pyright": "typecheck",
    "pytype": "typecheck",
    "tsc": "typecheck",
    "flow": "typecheck",
    # build
    "cmake": "build",
    "bazel": "build",
    "bazelisk": "build",
    "mvn": "build",
    "gradle": "build",
    "gradlew": "build",
    "ninja": "build",
    "rustc": "build",
    "gcc": "build",
    "g++": "build",
    "clang": "build",
    "clang++": "build",
    "cc": "build",
    "make": "build",
    # package management
    "pip": "package",
    "pip3": "package",
    "poetry": "package",
    "pipenv": "package",
    "conda": "package",
    "apt": "package",
    "apt-get": "package",
    "brew": "package",
    "dnf": "package",
    "yum": "package",
    "pacman": "package",
    "apk": "package",
    "gem": "package",
    "composer": "package",
    "npm": "package",
    "yarn": "package",
    "pnpm": "package",
    # wait
    "sleep": "wait",
    "wait": "wait",
    "watch": "wait",
}

_TEST_TARGET_HINTS = ("test", "check", "verify")
_LINT_TARGET_HINTS = ("lint", "fmt-check", "format-check")
_CI_TARGET_HINTS = ("ci", "preflight", "spec-ci", "dashboard-ci", "release-gate")
_TYPECHECK_TARGET_HINTS = ("vet", "typecheck")


def _segments(command: str) -> list[str]:
    return [segment.strip() for segment in _SHELL_OPERATORS.split(command) if segment.strip()]


def _tokens(segment: str) -> list[str]:
    try:
        tokens = shlex.split(segment, comments=False, posix=True)
    except ValueError:
        tokens = segment.split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _WRAPPERS or _ENV_ASSIGNMENT.match(token):
            index += 1
            continue
        break
    return tokens[index:]


def _program(tokens: Sequence[str]) -> str:
    if not tokens:
        return ""
    return tokens[0].rsplit("/", 1)[-1]


def _python_categories(args: Iterable[str]) -> set[str]:
    args = list(args)
    categories: set[str] = set()
    if "-m" in args:
        index = args.index("-m")
        if index + 1 < len(args):
            module = args[index + 1]
            if module in {"unittest", "pytest", "nose2"}:
                categories.add("test")
            elif module in {"mypy", "pyright"}:
                categories.add("typecheck")
            elif module in {"ruff", "flake8", "pylint"}:
                categories.add("lint")
    return categories


def _go_categories(args: Iterable[str]) -> set[str]:
    args = list(args)
    if not args:
        return set()
    subcommand = args[0]
    if subcommand == "test":
        return {"test"}
    if subcommand in {"build", "install"}:
        return {"build"}
    if subcommand in {"get", "mod", "work"}:
        return {"package"}
    if subcommand == "vet":
        return {"typecheck"}
    if subcommand == "generate":
        return {"build"}
    return set()


def _make_categories(args: Iterable[str]) -> set[str]:
    categories: set[str] = set()
    targets = [arg for arg in args if not arg.startswith("-")]
    if not targets:
        return {"build"}
    for target in targets:
        lowered = target.lower()
        if any(hint in lowered for hint in _CI_TARGET_HINTS):
            categories.add("ci")
        elif any(hint in lowered for hint in _LINT_TARGET_HINTS):
            categories.add("lint")
        elif any(hint in lowered for hint in _TYPECHECK_TARGET_HINTS):
            categories.add("typecheck")
        elif any(hint in lowered for hint in _TEST_TARGET_HINTS):
            categories.add("test")
    if not categories:
        categories.add("build")
    return categories


def _git_categories(args: Iterable[str]) -> set[str]:
    args = list(args)
    subcommand = next((arg for arg in args if not arg.startswith("-")), "")
    if subcommand == "worktree":
        return {"git", "worktree"}
    if subcommand in {"grep", "log"}:
        # git grep/log are read-only history/contents inspection and also search.
        return {"git", "search"} if subcommand == "grep" else {"git", "read"}
    if subcommand in {"show", "diff", "status", "blame", "cat-file", "rev-parse", "ls-files"}:
        return {"git", "read"}
    return {"git"}


def _gh_categories(args: Iterable[str]) -> set[str]:
    args = list(args)
    if len(args) >= 2 and args[0] == "pr":
        action = args[1]
        if action == "checks":
            return {"ci"}
        if action in {"review", "view", "diff", "comment", "list", "status"}:
            return {"review"}
        return set()
    if args and args[0] in {"run", "workflow", "actions", "cache"}:
        return {"ci"}
    return set()


def _gc_categories(args: Iterable[str]) -> set[str]:
    args = list(args)
    subcommand = next((arg for arg in args if not arg.startswith("-")), "")
    if subcommand in {"dispatch", "sling", "hook", "nudge", "order", "prime", "mail", "session"}:
        return {"dispatch"}
    if subcommand == "worktree":
        return {"worktree"}
    return set()


def _categorize_segment(segment: str) -> set[str]:
    tokens = _tokens(segment)
    program = _program(tokens)
    args = tokens[1:]
    if not program:
        return set()

    if program in {"python", "python3", "python3.11", "python3.12", "python3.13", "python3.14"} or program.startswith("python3."):
        return _python_categories(args)
    if program == "go":
        return _go_categories(args)
    if program == "make":
        return _make_categories(args)
    if program == "git":
        return _git_categories(args)
    if program == "gh":
        return _gh_categories(args)
    if program == "gc":
        return _gc_categories(args)
    if program == "cargo":
        if args and args[0] in {"test"}:
            return {"test"}
        if args and args[0] in {"check", "clippy"}:
            return {"typecheck"} if args[0] == "check" else {"lint"}
        if args and args[0] in {"build", "run"}:
            return {"build"}
        if args and args[0] in {"add", "update", "remove", "publish"}:
            return {"package"}
        return set()
    if program in {"npm", "yarn", "pnpm"}:
        if args and args[0] in {"test", "t"}:
            return {"test"}
        if args and args[0] in {"install", "i", "ci", "add", "remove", "update", "upgrade"}:
            return {"package"}
        if args and args[0] == "run" and len(args) > 1:
            return _make_categories([args[1]])
        return set()
    if program == "docker" and args and args[0] == "build":
        return {"build"}
    if program == "uv":
        if args and args[0] in {"pip", "add", "sync", "lock"}:
            return {"package"}
        return set()
    if program == "pre-commit":
        return {"ci"}

    category = _PROGRAM_CATEGORIES.get(program)
    if category is None:
        return set()

    if program == "tail" and "-f" in args:
        return {"read", "wait"}
    if program == "sed" and any(arg == "-i" or arg.startswith("-i") for arg in args):
        return {"edit"}
    if program in {"sed", "awk"}:
        return {"read"}
    if program == "gofmt" or program in {"gofumpt", "black", "prettier", "rustfmt"}:
        # Formatting is not one of the recognized categories; stay conservative.
        return set()
    return {category}


def categorize_command(command: str | None) -> frozenset[str]:
    """Return the set of categories for *command*.

    Returns an empty set for a missing/blank command (nothing to categorize) and
    ``{"unknown"}`` when a non-empty command matches no known rule.
    """
    if command is None or not command.strip():
        return frozenset()
    categories: set[str] = set()
    for segment in _segments(command):
        categories.update(_categorize_segment(segment))
    if not categories:
        return frozenset({"unknown"})
    return frozenset(categories)


def category_counts(commands: Iterable[str | None]) -> dict[str, int]:
    """Count categories across commands, always including every category key."""
    counts = {category: 0 for category in CATEGORIES}
    for command in commands:
        for category in categorize_command(command):
            counts[category] += 1
    return counts
