---
name: focused-test-selection
description: Choose an ordered, budget-aware test ladder from reproduction to targeted regressions.
---

# Focused Test Selection

**Internal ID:** `S24`  
**Category:** `verification`  
**Tier:** `core`  
**Output:** `TestPlan`

## Activate when

- The workflow reaches the `verification` stage and the required inputs are present.

## Applicable roles

- Verifier
- Planner

## Required inputs

- `issue_profile`
- `candidate_patch`
- `relevant_tests`
- `budget`

## Optional inputs

- `historical_test_times`

## Procedure

1. Start with the original reproduction or most direct target test.
2. Add tests for changed public contracts and nearby branches.
3. Order commands by information gain divided by cost.
4. Reserve budget for one rerun after repair and final diff inspection.

## Verification

- Each test closes a named evidence gap.
- The plan fits the time budget.

## Stop conditions

- All hard criteria have at least one planned evidence source.

## Constraints

- Do not select tests solely because they are fast.
- Do not starve verification by spending the full budget on generation.

## Allowed tools

- `test_discovery`
- `read_file`

## Provenance

Synthesized from design patterns in `agentless`, `mini-swe-agent`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.