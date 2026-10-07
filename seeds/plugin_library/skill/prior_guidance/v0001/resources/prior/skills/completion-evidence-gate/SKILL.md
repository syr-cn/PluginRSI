---
name: completion-evidence-gate
description: Prevent submission until acceptance criteria and scope are backed by fresh evidence.
---

# Completion Evidence Gate

**Internal ID:** `S27`  
**Category:** `verification`  
**Tier:** `core`  
**Output:** `CompletionDecision`

## Activate when

- The workflow reaches the `verification` stage and the required inputs are present.

## Applicable roles

- Finalizer
- Termination Controller

## Required inputs

- `issue_profile`
- `verification_result`
- `diff_inspection`
- `budget`

## Optional inputs

- `known_blockers`

## Procedure

1. Enumerate hard acceptance criteria and their evidence status.
2. Check target reproduction, relevant regressions, scope, and unresolved unknowns.
3. Return submit only when every hard item passes.
4. Otherwise return continue, backtrack, or blocked with the smallest next evidence target.

## Verification

- No hard criterion is silently omitted.
- Evidence references current repository state.

## Stop conditions

- A single explicit completion action is produced.

## Constraints

- Do not submit because budget is exhausted.
- Unknown is not pass.

## Allowed tools

- `read_file`

## Provenance

Synthesized from design patterns in `mini-swe-agent`, `superpowers`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.