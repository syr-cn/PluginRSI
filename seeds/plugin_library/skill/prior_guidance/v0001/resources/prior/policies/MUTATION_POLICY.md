# Alternating Mutation Policy

The search space is divided into three dimensions.

## Graph/scaffold phase

Freeze role definitions, prompt bodies, skill bodies, model, evaluator and memory. Allow one interpretable change to role selection, node count, edges, order, visibility, bounded loops or stop conditions.

## Prompt/experience phase

Freeze graph, skills, model and evaluator. Edit one role prompt, local configuration, retrieval parameter or example set. Preserve the typed role contract.

## Skill/tool phase

Freeze graph and role prompts. Propose one skill add/edit/merge/split/retire operation or one tool-permission/schema change. Require positive-trigger and negative-trigger evaluation before promotion.

## Memory phase

Start read-only. Introduce curated writes only after retrieval ablations. Promote procedural memory to a skill only after repeated evidence and held-out evaluation.

## Universal controls

- Pre-execution schema validation.
- One-change attribution whenever possible.
- Matched rollout, token, tool and test budgets.
- Rollback version for every promoted mutation.
- No mutation may weaken the global scope guard.
