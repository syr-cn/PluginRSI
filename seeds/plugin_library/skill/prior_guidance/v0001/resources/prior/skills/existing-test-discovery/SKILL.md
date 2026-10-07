---
name: existing-test-discovery
description: Find the smallest existing tests that exercise the changed behavior and nearby contracts.
---

# Existing Test Discovery

**Internal ID:** `S23`  
**Category:** `verification`  
**Tier:** `core`  
**Output:** `RelevantTests`

## Activate when

- The workflow reaches the `verification` stage and the required inputs are present.

## Applicable roles

- Verifier
- Planner
- Solver

## Required inputs

- `issue_profile`
- `changed_symbols`
- `repository_orientation`

## Optional inputs

- `repository_map`

## Procedure

1. Search test names, imports, fixtures, and assertions referencing changed symbols or behavior.
2. Trace test organization from repository instructions.
3. Rank target tests, neighboring regression tests, and required setup.
4. Return exact commands and expected runtime.

## Verification

- Each selected test has a coverage rationale.
- Commands respect repository conventions.

## Stop conditions

- A focused test set exists or the absence of tests is documented.

## Constraints

- Do not run the entire suite before locating focused tests unless the project offers no alternative.

## Allowed tools

- `test_discovery`
- `text_search`
- `symbol_search`
- `read_file`

## Provenance

Synthesized from design patterns in `agentless`, `mini-swe-agent`, `aider`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.