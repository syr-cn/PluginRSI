---
name: regression-check
description: Check target correctness and nearby behavior without defaulting to an expensive full suite.
---

# Regression Check

**Internal ID:** `S25`  
**Category:** `verification`  
**Tier:** `core`  
**Output:** `RegressionResult`

## Activate when

- The workflow reaches the `verification` stage and the required inputs are present.

## Applicable roles

- Verifier

## Required inputs

- `candidate_patch`
- `test_plan`
- `baseline_evidence`

## Optional inputs

- `repository_rules`

## Procedure

1. Run the target reproduction and focused tests on the patched state.
2. Compare failures with the baseline and separate pre-existing failures.
3. Run broader tests only when changed surface or repository rules justify them.
4. Record exact commands, exit status, salient output, and elapsed time.

## Verification

- Target evidence is fresh.
- New failures are classified as patch-caused, pre-existing, or unknown.

## Stop conditions

- All planned tests are complete or a blocker is documented.

## Constraints

- Do not claim broad regression safety from a single narrow test.

## Allowed tools

- `run_test`
- `shell_exec`
- `inspect_failure`

## Provenance

Synthesized from design patterns in `agentless`, `mini-swe-agent`, `superpowers`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.