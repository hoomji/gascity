# Upstream report: `mol-scoped-work` teardown force-deletes foreign worktrees

Status: **draft — do not post.** This is the upstream issue text for
`gastownhall/gascity`, written for the mayor to post after confirmation. It
reports a defect in the built-in core pack formula that the owner fork
(`hoomji/gascity`) patches on top of `origin/main`
(`6b8560734a8547ad688264ccf697e95a496fb730`).

## Title

`mol-scoped-work` cleanup-worktree force-deletes worktrees it did not create,
fires on a FAILED body, and retries a refusal three times

## Summary

The built-in core pack template
`internal/bootstrap/packs/core/formulas/mol-scoped-work.toml` is embedded into
every city seeded by `gc city init`. Its `cleanup-worktree` teardown step is
unsafe in three ways:

1. **It force-deletes a worktree it did not create.** The step reads
   `metadata.work_dir` from the work bead and then runs
   `git worktree remove --force "$WORKTREE" || rm -rf "$WORKTREE"`. Nothing
   checks that the directory was created by *this* molecule, that the
   repository is the one this molecule branched from, or that the path is even
   a linked worktree. A run whose work bead carries a `work_dir` pointing at
   the rig root (the shared main checkout) deletes the owner's checkout.
2. **It fires on a FAILED body.** The teardown runs after the body reaches any
   terminal state, including `gc.outcome=fail`. When the body failed there is
   routinely uncommitted work and unreviewed `*-report.md` artifacts that exist
   only inside the worktree; `--force` destroys them with no recovery path.
   Teardown is the one place a failed run's evidence should survive.
3. **It retries a refusal three times.** The step declares
   `max_attempts = 3` / `on_exhausted = "hard_fail"`. Every guard that refuses
   (see below) therefore re-attempts the same destructive operation three
   times before the control bead hard-fails, which is what turns one refusal
   into recurring `BLOCKED` mail.

Separately, the snippet never states that an empty `work_dir` is a *successful*
no-op. Because the removal block is `if [ -n "$WORKTREE" ] && [ -d "$WORKTREE" ]`,
an absent `work_dir` skips everything and exits 0 — but the teardown lane has
nothing in the step text telling it so. Lanes in the field have read the
silence as "the step is incomplete", gone looking for another `work_dir`,
found the *cleanup bead's own* `gc.work_dir` (the rig root, i.e. the shared
main checkout), correctly predicted the main-checkout guard would refuse it,
and mailed the mayor instead of closing. That is the escalation half of this
bug; it is the same root cause as the force-delete.

## Impact

- Data loss risk: a shared main checkout can be deleted by a routine teardown.
- Unrecoverable evidence loss: a failed run's uncommitted diff and untracked
  `*-report.md` lens reports are deleted before anyone reads them.
- Mail flood: each refusal retries three times, then hard-fails; the resulting
  `BLOCKED` reports recur every shift and dominate the mayor's prompt.
- Worktree leak: lanes that refuse for the right reason never complete teardown,
  so `git worktree list` grows unbounded (`pruned 121 -> 74 on 2026-09-13`, then
  regrew because the template stayed unpatched).

## Reproduction

```bash
# 1. Seed a city from the core pack and start a mol-scoped-work molecule
#    (a one-member input convoy; workspace-setup records work_dir on the work
#    bead).
# 2. Close the body with a failed outcome, leaving a file only in the worktree:
echo evidence > "$WORKTREE/run-report.md"
# 3. Run the teardown step.
#    - With work_dir set, the snippet runs:
git worktree remove --force "$WORKTREE" || rm -rf "$WORKTREE"
#      and the only copy of run-report.md is gone.
# 4. Delete work_dir from the work bead and run teardown again:
#    $WORKTREE is empty, the removal block is skipped, the step exits 0, and
#    the step text gives the lane no basis to call that success -- so it
#    escalates to the mayor instead of closing.
```

## Evidence

At `6b8560734a8547ad688264ccf697e95a496fb730`
(`internal/bootstrap/packs/core/formulas/mol-scoped-work.toml:205-221`):

```bash
WORKTREE=$(gc bd show "$WORK_BEAD_ID" --json | jq -r '.[0].metadata.work_dir // empty')
if [ -n "$WORKTREE" ] && [ -d "$WORKTREE" ]; then
  REPO=$(git -C "$WORKTREE" rev-parse --path-format=absolute --git-common-dir)
  if [ -z "$REPO" ]; then
    echo "cleanup: could not resolve the owning repo for $WORKTREE; leaving it registered" >&2
  elif ! git -C "$REPO" worktree remove --force "$WORKTREE"; then
    if [ -f "$WORKTREE/.git" ]; then
      rm -rf "$WORKTREE"
      git -C "$REPO" worktree prune
    else
      echo "cleanup: refusing to rm -rf $WORKTREE: not a linked worktree" >&2
    fi
  fi
fi
gc bd update "$WORK_BEAD_ID" --unset-metadata work_dir
```

with `[steps.retry] max_attempts = 3 / on_exhausted = "hard_fail"` at
`:226-228`. There is no ownership check, no uncommitted-work guard, no report
salvage, and no statement that an empty `work_dir` is success.

Field evidence from the Gateway-LLM city (`ci-lhuyq`, 2026-09-11): mail
`ci-wisp-2ojy7j` from `gateway-llm/codex-5` refused because the cleanup bead's
own `gc.work_dir` realpathed to the shared checkout; the same shape recurs in
`ci-wisp-f81574`, `y1fnhm`, `j5vnx0`, `kjy8wh`, `j23o3r`, `xk9hjf`, `vhc8qx`,
`5dnlgd`, `riz7wh`, and `mdf8ur/wverh3/dx5k07/ducoac/w0wf9n` (`gl-90dphn`, five
retries). `ci-wisp-kjy8wh` is the evidence-loss case: worktree `gl-edge787`
held an untracked `gl-fnfdir-edge-report.md`, and only the uncommitted-work
check kept it alive.

## Proposed fix

In `cleanup-worktree`:

1. Read `work_dir` from the **work bead only** and say so in a comment naming
   the rig root as what a fallback to the cleanup bead's own `gc.work_dir`
   resolves to. An empty `work_dir` is a successful no-op: print that, do not
   mail, and let the step close `gc.outcome=pass`.
2. Treat a `work_dir` whose directory is already gone as the same successful
   no-op.
3. Refuse to touch anything that is not a linked worktree created by this
   molecule (a linked worktree's `.git` is a file; a main checkout's is a
   directory), as a no-op rather than an escalation.
4. Copy untracked `*-report.md` artifacts to a backups directory **before**
   removal and name the destination in the close reason.
5. Do not remove a tree that still holds uncommitted work left by a failed
   body; preserve it for recovery.
6. Do not retry a refusal: drop `max_attempts` to 1 so one refusal is one
   recorded outcome, not three deletion attempts plus a hard-fail.

The owner fork implements exactly this in
`internal/bootstrap/packs/core/formulas/mol-scoped-work.toml`, with a
regression test in
`internal/bootstrap/packs/core/pack_formulas_test.go`
(`TestMolScopedWorkCleanupWorktreeNoOpAndSalvage`), on branch
`fleet/7ce64f4472068ece2af5`.

## Non-goals

- This report does not request a change to `mol-polecat-commit.toml`; its
  `commit-and-push` step has its own opt-in commit gate before removal.
- It does not propose pruning live worktrees or deleting registrations; the fix
  only narrows what a teardown is allowed to remove.
