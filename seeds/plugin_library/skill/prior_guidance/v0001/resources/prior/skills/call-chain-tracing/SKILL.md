---
name: call-chain-tracing
description: Trace data, control, and state across callers and callees relevant to the failure.
---

# Call-chain Tracing

**Internal ID:** `S10`  
**Category:** `localization`  
**Tier:** `optional`  
**Output:** `CallChain`

## Activate when

- The workflow reaches the `localization` stage and the required inputs are present.

## Applicable roles

- Solver
- Critic

## Required inputs

- `entry_symbol`
- `source_context`

## Optional inputs

- `runtime_trace`
- `repository_map`

## Procedure

1. Trace only the paths that can reach the observed symptom.
2. Record argument transformations, state mutation, exception handling, and return contracts.
3. Mark dynamic dispatch or unresolved targets.
4. Highlight the first point where actual behavior diverges from the expected contract.

## Verification

- The chain is supported by code or runtime evidence.
- Unknown dynamic edges are explicit.

## Stop conditions

- The earliest causal divergence or an evidence gap is identified.

## Constraints

- Do not expand unrelated branches.
- Do not invent runtime order.

## Allowed tools

- `symbol_search`
- `read_range`
- `inspect_failure`

## Provenance

Synthesized from design patterns in `aider`, `superpowers`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.