---
name: focused-failure-reproduction
description: Execute and stabilize the smallest reproduction before editing.
---

# Focused Failure Reproduction

**Internal ID:** `S12`  
**Category:** `reproduction`  
**Tier:** `core`  
**Output:** `ReproductionResult`

## Activate when

- The workflow reaches the `reproduction` stage and the required inputs are present.

## Applicable roles

- Solver
- Verifier

## Required inputs

- `reproduction_plan`
- `repository_state`

## Optional inputs

- `environment_memory`

## Procedure

1. Run the exact reproduction command on a clean working state.
2. Separate product failure from environment/setup failure.
3. Repeat only when nondeterminism is plausible.
4. Capture command, exit status, salient output, timing, and generated artifacts.

## Verification

- The observed result matches the reported symptom or the discrepancy is explained.
- Evidence is fresh and attributable to the current revision.

## Stop conditions

- Failure is reproducible, disproven, or blocked with evidence.

## Constraints

- Do not patch before recording baseline evidence unless execution is impossible.

## Allowed tools

- `shell_exec`
- `inspect_failure`
- `git_status`

## Provenance

Synthesized from design patterns in `mini-swe-agent`, `superpowers`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.