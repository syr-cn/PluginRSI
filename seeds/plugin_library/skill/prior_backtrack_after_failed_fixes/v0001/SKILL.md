---
name: backtrack-after-failed-fixes
description: Recover from repeated ineffective edits by reverting unsupported changes and reopening diagnosis.
---

# Backtrack After Failed Fixes

**Internal ID:** `S30`  
**Category:** `recovery`  
**Tier:** `core`  
**Output:** `BacktrackPlan`

## Activate when

- The workflow reaches the `recovery` stage and the required inputs are present.

## Applicable roles

- Termination Controller
- Critic
- Solver

## Required inputs

- `trajectory`
- `candidate_patch`
- `verification_failures`

## Optional inputs

- `component_credit`

## Procedure

1. Identify edits that failed their predicted effect or introduced regressions.
2. Separate useful evidence from unsupported code changes.
3. Revert the smallest failed change set.
4. Update hypotheses and choose a new discriminating check before repatching.

## Verification

- The working state after backtrack is known.
- Retained edits have independent evidence.

## Stop conditions

- The system returns to a coherent state with a new diagnostic target.

## Constraints

- Do not accumulate speculative patches.
- Do not erase useful failure evidence.

## Allowed tools

- `git_diff`
- `apply_patch`
- `shell_exec`

## Provenance

Synthesized from design patterns in `superpowers`, `addy-agent-skills`, `deepseek-harness`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.