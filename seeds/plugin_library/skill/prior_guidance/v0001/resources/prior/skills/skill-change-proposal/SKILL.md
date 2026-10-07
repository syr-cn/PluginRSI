---
name: skill-change-proposal
description: Propose add/edit/merge/split/retire operations from repeated failures and evaluation evidence.
---

# Skill Change Proposal

**Internal ID:** `S35`  
**Category:** `skill-evolution`  
**Tier:** `meta`  
**Output:** `SkillChangeProposal`

## Activate when

- The workflow reaches the `skill-evolution` stage and the required inputs are present.

## Applicable roles

- Skill Compiler
- Prompt Optimizer

## Required inputs

- `failure_cluster_or_lessons`
- `skill_library`
- `asset_usage`

## Optional inputs

- `counterexamples`

## Procedure

1. Map evidence to the smallest affected skill or missing transformation.
2. Choose add, edit, merge, split, retire, or reject.
3. Preserve typed inputs, outputs, role compatibility, guards, and provenance.
4. Define positive-trigger and negative-trigger evaluation cases.
5. Keep the candidate inactive until evaluation.

## Verification

- The proposal addresses a repeated failure without duplicating the library.
- A rollback condition and evaluation plan exist.

## Stop conditions

- One minimal candidate change is ready for matched-budget testing.

## Constraints

- Do not activate the candidate immediately.
- Do not copy long upstream text verbatim.

## Allowed tools

- `read_file`
- `text_search`

## Provenance

Synthesized from design patterns in `agentskills-spec`, `anthropic-skills`, `openhands-extensions`, `superpowers`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.