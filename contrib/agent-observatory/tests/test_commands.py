"""Tests for conservative, non-executing command categorization."""

from __future__ import annotations

import unittest

try:
    from . import support  # noqa: F401  (import side effect: adds package root to sys.path)
except ImportError:  # pragma: no cover
    import support  # noqa: F401

from agent_observatory.commands import CATEGORIES, categorize_command, category_counts


class CommandCategoryTest(unittest.TestCase):
    def test_unknown_fallback_for_unrecognized_command(self):
        self.assertEqual(categorize_command("frobnicate --now"), frozenset({"unknown"}))

    def test_ambiguous_command_text_is_not_misread_as_a_test(self):
        # The word "pytest" appears only inside an echo argument; the program is
        # echo, so this must not be classified as a test invocation.
        self.assertEqual(categorize_command('echo "pytest -q"'), frozenset({"unknown"}))

    def test_compound_command_can_have_multiple_categories(self):
        categories = categorize_command("git status && rg pattern --glob '*.py'")
        self.assertIn("git", categories)
        self.assertIn("read", categories)
        self.assertIn("search", categories)

    def test_pipe_separated_commands_are_categorized_independently(self):
        categories = categorize_command("cat file.txt | grep needle")
        self.assertEqual(categories, frozenset({"read", "search"}))

    def test_go_subcommands(self):
        self.assertEqual(categorize_command("go test ./..."), frozenset({"test"}))
        self.assertEqual(categorize_command("go vet ./..."), frozenset({"typecheck"}))
        self.assertEqual(categorize_command("go build ./cmd/gc"), frozenset({"build"}))
        self.assertEqual(categorize_command("go mod tidy"), frozenset({"package"}))

    def test_git_worktree_is_both_git_and_worktree(self):
        self.assertEqual(categorize_command("git worktree add ../x"), frozenset({"git", "worktree"}))

    def test_gh_pr_checks_is_ci_and_pr_review_is_review(self):
        self.assertEqual(categorize_command("gh pr checks 42"), frozenset({"ci"}))
        self.assertEqual(categorize_command("gh pr review 42 --approve"), frozenset({"review"}))

    def test_gc_dispatch_is_dispatch(self):
        self.assertEqual(categorize_command("gc dispatch queued"), frozenset({"dispatch"}))
        self.assertEqual(categorize_command("gc hook --claim --json"), frozenset({"dispatch"}))

    def test_make_targets(self):
        self.assertEqual(categorize_command("make test"), frozenset({"test"}))
        self.assertEqual(categorize_command("make lint"), frozenset({"lint"}))
        self.assertEqual(categorize_command("make"), frozenset({"build"}))

    def test_python_module_invocations(self):
        self.assertEqual(categorize_command("python3 -m unittest discover -s tests"), frozenset({"test"}))
        self.assertEqual(categorize_command("python3 -m mypy src"), frozenset({"typecheck"}))
        self.assertEqual(categorize_command("python3 script.py"), frozenset({"unknown"}))

    def test_blank_command_has_no_categories(self):
        self.assertEqual(categorize_command(None), frozenset())
        self.assertEqual(categorize_command("   "), frozenset())

    def test_category_counts_include_every_known_category(self):
        counts = category_counts(["rg foo", None])
        self.assertEqual(set(counts), set(CATEGORIES))
        self.assertEqual(counts["search"], 1)
        self.assertEqual(counts["unknown"], 0)


if __name__ == "__main__":
    unittest.main()
