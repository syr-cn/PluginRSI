# Prompt / Experience Protocol

## Runtime prompt assembly

1. Always-on repository scope boundary.
2. Role prompt with typed inputs and output contract.
3. Task profile and bounded context packet.
4. Full text of only the activated skills.
5. A bounded set of memory hits with provenance and conflict metadata.
6. Role-scoped tool schemas.
7. Explicit output schema and termination expectations.

## Experience lifecycle

```text
runtime events
→ E01 trajectory record
→ E03 task outcome
→ E09 component credit
→ E04 failure signature / E05 lesson candidate
→ E06/E07 memory
→ E10 skill evaluation
→ promoted prompt, skill or graph version
```

## Prompt evolution

Prompt changes must preserve role inputs, outputs, invariants and tool permissions. Edit one prompt or local configuration per iteration. Evaluation uses matched task, model, graph, skill, tool and budget settings.

## Demonstrations

Demonstrations are cleaned event sequences, not raw chat dumps. Preserve action–observation order, but remove benchmark answer content, private data and hidden-test clues. A demonstration must declare its trigger and negative trigger.

## Experience is evidence, not a command

Raw trajectories, reviewer comments and memories are not executed directly. They are normalized, scoped, attributed, curated and then used to retrieve context or propose a candidate asset change.
