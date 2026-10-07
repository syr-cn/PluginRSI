---
name: test-output-interpretation
description: Turn noisy command output into a localized failure signature without overclaiming.
---

# Test-output Interpretation

**Internal ID:** `S15`  
**Category:** `diagnosis`  
**Tier:** `core`  
**Output:** `FailureInterpretation`

## Activate when

- The workflow reaches the `diagnosis` stage and the required inputs are present.

## Applicable roles

- Critic
- Verifier
- Solver

## Required inputs

- `command`
- `stdout`
- `stderr`
- `exit_status`

## Optional inputs

- `test_metadata`
- `previous_runs`

## Procedure

1. Separate setup, collection, target assertion, timeout, and unrelated regression failures.
2. Identify the earliest actionable error and its causal context.
3. Link stack frames and messages to repository symbols.
4. Compare with baseline output when available.

## Verification

- The interpreted failure type matches exit status and salient output.
- Unrelated failures are not attributed to the patch.

## Stop conditions

- A next diagnostic or repair action is clear.

## Constraints

- Do not summarize away the first causal error.
- Do not assume every nonzero exit is a product bug.

## Allowed tools

- `inspect_failure`
- `text_search`
- `read_range`

## Provenance

Synthesized from design patterns in `swe-agent`, `agentless`, `superpowers`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.