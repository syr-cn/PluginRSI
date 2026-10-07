---
name: minimal-patch
description: Implement the smallest coherent source change that repairs a validated root cause.
---

# Minimal Patch

**Internal ID:** `S16`  
**Category:** `patching`  
**Tier:** `core`  
**Output:** `CandidatePatch`

## Activate when

- The workflow reaches the `patching` stage and the required inputs are present.

## Applicable roles

- Solver
- Reviser

## Required inputs

- `diagnosis`
- `edit_locations`
- `repository_rules`

## Optional inputs

- `public_contracts`
- `focused_tests`

## Procedure

1. Translate the root-cause mechanism into the smallest semantic change.
2. Preserve surrounding style, interfaces, and error behavior.
3. Avoid new abstractions unless the fix would otherwise be duplicated or unsafe.
4. Review each changed hunk against the issue and remove incidental edits.

## Verification

- Every changed hunk contributes to the repair or required validation.
- The patch preserves stated invariants.

## Stop conditions

- A coherent patch is ready for focused verification.

## Constraints

- Do not create a new top-level directory unless the issue explicitly requires it.
- Do not reorganize the repository or refactor unrelated code.
- Do not modify tests merely to hide a failure.
- Do not change dependencies, lockfiles, build files, or CI configuration unless causally necessary.
- Prefer the smallest coherent source patch that satisfies the issue and preserves public behavior.

## Allowed tools

- `apply_patch`
- `read_range`
- `git_diff`

## Provenance

Synthesized from design patterns in `mini-swe-agent`, `agentless`, `superpowers`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.