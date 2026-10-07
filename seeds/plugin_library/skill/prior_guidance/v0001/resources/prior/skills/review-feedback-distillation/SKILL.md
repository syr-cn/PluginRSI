---
name: review-feedback-distillation
description: Convert repeated code-review or evaluator feedback into reusable guidelines or skill edits.
---

# Review Feedback Distillation

**Internal ID:** `S32`  
**Category:** `learning`  
**Tier:** `meta`  
**Output:** `DistilledReviewLesson`

## Activate when

- The workflow reaches the `learning` stage and the required inputs are present.

## Applicable roles

- Reflection Agent
- Skill Compiler

## Required inputs

- `review_feedback`
- `patches`
- `outcomes`

## Optional inputs

- `existing_skills`

## Procedure

1. Group feedback by underlying failure mechanism rather than wording.
2. Separate repository-specific conventions from general procedures.
3. Identify positive and negative triggers.
4. Propose memory or skill updates with supporting examples.

## Verification

- Each proposed update has repeated or strong evidence.
- Duplicate existing skills are merged rather than cloned.

## Stop conditions

- Feedback is rejected, stored as memory, or proposed as a tested skill change.

## Constraints

- Do not convert reviewer taste into a universal rule.
- Preserve counterexamples.

## Allowed tools

- `read_file`
- `text_search`

## Provenance

Synthesized from design patterns in `openhands-extensions`, `anthropic-skills`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.