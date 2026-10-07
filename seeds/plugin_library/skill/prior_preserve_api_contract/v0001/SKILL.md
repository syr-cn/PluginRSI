---
name: preserve-api-contract
description: Check that a repair does not unintentionally change public signatures, return types, errors, or compatibility.
---

# Preserve API Contract

**Internal ID:** `S18`  
**Category:** `patching`  
**Tier:** `core`  
**Output:** `ContractAssessment`

## Activate when

- The workflow reaches the `patching` stage and the required inputs are present.

## Applicable roles

- Solver
- Verifier
- Critic

## Required inputs

- `candidate_patch`
- `public_interfaces`
- `issue_profile`

## Optional inputs

- `downstream_references`

## Procedure

1. Identify public or externally consumed symbols touched by the patch.
2. Compare signatures, return semantics, exceptions, serialization, and ordering.
3. Search direct downstream usage when the contract is not explicit.
4. Mark intentional issue-required changes separately from regressions.

## Verification

- Every touched public contract has a preservation or intentional-change judgment.
- Compatibility risks have evidence.

## Stop conditions

- No unresolved unintended contract change remains.

## Constraints

- Do not block changes explicitly required by the issue.
- Do not infer public status solely from naming.

## Allowed tools

- `symbol_search`
- `text_search`
- `git_diff`
- `read_range`

## Provenance

Synthesized from design patterns in `aider`, `openhands-agent-sdk`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.