# Prior harness authoring guidance

Use when designing a free-form Python harness or selecting prior plugins.

Read resources/inventory.yaml for the asset-to-plugin mapping, then load only relevant resources/prior files. The retained workflow graphs, scaffold profiles, old configs and schemas are historical design guidance, not executable workflow plugins or current run configuration. Translate their behavior into harness Python using the four supported plugin interfaces. No DAG runtime is provided.

All prior source files are preserved as reference resources. Role plugins retain complete prompts and guardrails; skill plugins retain full SKILL.md bodies. Respect the evidence-only experience protocol and current split/contamination rules. Source names refer to projects; they are not verified paper titles.
