---
name: final-diff-inspection
description: Review the final working-tree diff for scope, correctness, artifacts, and incomplete edits.
---

# Final Diff Inspection

**Internal ID:** `S26`  
**Category:** `review`  
**Tier:** `core`  
**Output:** `DiffInspection`

## Activate when

- The workflow reaches the `review` stage and the required inputs are present.

## Applicable roles

- Verifier
- Finalizer
- Critic

## Required inputs

- `issue_profile`
- `git_diff`
- `git_status`
- `verification_result`

## Optional inputs

- `repository_rules`

## Procedure

1. Inspect every changed file and untracked artifact.
2. Map each hunk to the diagnosis, repair, or required validation.
3. Check debug prints, temporary scripts, formatting churn, generated files, and stale comments.
4. Check that the code matches the tested behavior and no planned edit is missing.

## Verification

- Every hunk is explained.
- No unexpected tracked or untracked artifact remains.

## Stop conditions

- The final diff is ready to submit or has a precise cleanup list.

## Constraints

- Do not create a new top-level directory unless the issue explicitly requires it.
- Do not reorganize the repository or refactor unrelated code.
- Do not modify tests merely to hide a failure.
- Do not change dependencies, lockfiles, build files, or CI configuration unless causally necessary.
- Prefer the smallest coherent source patch that satisfies the issue and preserves public behavior.
- Run focused verification before broad test suites.
- Inspect the final diff and revert incidental edits before submission.

## Allowed tools

- `git_status`
- `git_diff`
- `read_range`

## Provenance

Synthesized from design patterns in `mini-swe-agent`, `superpowers`, `openhands-extensions`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.