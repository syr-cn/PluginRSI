---
name: reproduction-script-design
description: Design a minimal safe script or command that exposes the target behavior.
---

# Reproduction Script Design

**Internal ID:** `S11`  
**Category:** `reproduction`  
**Tier:** `core`  
**Output:** `ReproductionPlan`

## Activate when

- The workflow reaches the `reproduction` stage and the required inputs are present.

## Applicable roles

- Planner
- Solver

## Required inputs

- `issue_profile`
- `localized_context`
- `environment_info`

## Optional inputs

- `existing_tests`

## Procedure

1. Prefer an existing focused test or public API invocation.
2. Minimize fixtures and unrelated dependencies.
3. Specify expected current and fixed outcomes.
4. Define timeout, cleanup, and nondeterminism handling.
5. Keep temporary artifacts outside tracked source when possible.

## Verification

- The reproduction distinguishes the target failure from setup failure.
- The command is repeatable.

## Stop conditions

- A runnable focused reproduction exists or a blocker is documented.

## Constraints

- Do not modify repository tests to force reproduction.
- Do not build a large custom harness.

## Allowed tools

- `read_file`
- `shell_exec`
- `test_discovery`

## Provenance

Synthesized from design patterns in `mini-swe-agent`, `agentless`, `superpowers`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.