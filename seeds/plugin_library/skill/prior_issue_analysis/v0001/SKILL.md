---
name: issue-analysis
description: Translate a software issue into observable acceptance criteria, constraints, risks, and an evidence plan without proposing a patch.
---

# Issue Analysis

**Internal ID:** `S02`  
**Category:** `understanding`  
**Tier:** `core`  
**Output:** `IssueProfile`

## Activate when

- The workflow reaches the `understanding` stage and the required inputs are present.

## Applicable roles

- Task Analyzer
- Planner

## Required inputs

- `issue_text`

## Optional inputs

- `repository_instructions`

## Procedure

1. Separate requested behavior, current behavior, explicit constraints, and unknowns.
2. Rewrite each requirement as an observable acceptance criterion.
3. Identify likely failure surfaces without choosing one.
4. List evidence needed to discriminate between failure surfaces.
5. Record forbidden changes and delivery conditions.

## Verification

- Every acceptance criterion is testable or explicitly marked ambiguous.
- No implementation claim is presented as fact without evidence.

## Stop conditions

- The task profile is sufficient to guide localization and verification.

## Constraints

- Do not propose a patch.
- Do not silently resolve ambiguity.

## Allowed tools

- `read_file`

## Provenance

Synthesized from design patterns in `mini-swe-agent`, `agentless`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.