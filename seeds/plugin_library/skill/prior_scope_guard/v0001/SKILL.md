---
name: scope-guard
description: Detect repository restructuring, unrelated refactors, test weakening, and incidental changes.
---

# Scope Guard

**Internal ID:** `S19`  
**Category:** `review`  
**Tier:** `core`  
**Output:** `ScopeAssessment`

## Activate when

- The workflow reaches the `review` stage and the required inputs are present.

## Applicable roles

- Verifier
- Critic
- Finalizer

## Required inputs

- `issue_profile`
- `git_diff`
- `repository_rules`

## Optional inputs

- `git_status`

## Procedure

1. Map each changed file and hunk to an acceptance criterion or required validation.
2. Flag top-level additions, moves, broad renames, formatting churn, generated files, and unrelated cleanup.
3. Check tests and configuration for weakening or unjustified changes.
4. Recommend precise reverts for incidental edits.

## Verification

- Every retained hunk has a causal justification.
- Untracked and generated artifacts are accounted for.

## Stop conditions

- The diff is bounded to the issue and necessary validation.

## Constraints

- Do not create a new top-level directory unless the issue explicitly requires it.
- Do not reorganize the repository or refactor unrelated code.
- Do not modify tests merely to hide a failure.
- Do not change dependencies, lockfiles, build files, or CI configuration unless causally necessary.
- Prefer the smallest coherent source patch that satisfies the issue and preserves public behavior.

## Allowed tools

- `git_status`
- `git_diff`
- `read_file`

## Provenance

Synthesized from design patterns in `mini-swe-agent`, `superpowers`, `addy-agent-skills`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.