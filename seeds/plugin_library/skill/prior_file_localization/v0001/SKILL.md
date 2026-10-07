---
name: file-localization
description: Rank the smallest set of repository files likely to contain the failure or required behavior.
---

# File Localization

**Internal ID:** `S07`  
**Category:** `localization`  
**Tier:** `core`  
**Output:** `RankedFiles`

## Activate when

- The workflow reaches the `localization` stage and the required inputs are present.

## Applicable roles

- Solver
- Localizer

## Required inputs

- `issue_profile`
- `repository_orientation`
- `search_results`

## Optional inputs

- `reproduction_evidence`

## Procedure

1. Extract concrete entities, behavior, and error terms from the issue.
2. Search definitions, error strings, configuration keys, and call sites.
3. Group results by causal path rather than keyword count.
4. Rank files by directness, public contract relevance, and reproduction evidence.

## Verification

- Every file has a reason and cited evidence.
- The top set covers at least one complete failure path.

## Stop conditions

- No more than five primary files remain, unless the issue is intrinsically cross-cutting.

## Constraints

- Do not return an unranked grep dump.
- Do not use benchmark gold locations.

## Allowed tools

- `text_search`
- `symbol_search`
- `read_range`

## Provenance

Synthesized from design patterns in `agentless`, `aider`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.