#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import re

import yaml
from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = ROOT / "ASSET_MANIFEST.yaml"
PROMPT_DIR = ROOT / "prompts"
WORKFLOW_DIR = ROOT / "scaffold/workflows"
SKILL_CATALOG_PATH = ROOT / "skills/catalog.yaml"
TOOL_REGISTRY_PATH = ROOT / "tools/tool_registry.yaml"
SCAFFOLD_PATH = ROOT / "scaffold/components.yaml"
EXPERIENCE_PATH = ROOT / "experience/record_types.yaml"
ROLE_MAP_PATH = ROOT / "mappings/role_asset_map.yaml"
CONFIG_PATH = ROOT / "configs/swebench_v1.yaml"

SCHEMA_FILES = {
    "prompt": "prompt_spec.schema.json",
    "experience": "experience_record.schema.json",
    "scaffold": "scaffold_component.schema.json",
    "workflow": "workflow_spec.schema.json",
    "tool": "tool_spec.schema.json",
}
SKILL_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
SKILL_TOOL_SECTION = re.compile(r"## Allowed tools\n\n(.*?)(?:\n## |\Z)", re.DOTALL)
BACKTICK_BULLET = re.compile(r"^- `([^`]+)`$", re.MULTILINE)


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def check_unique(items: list[dict], key: str, label: str, errors: list[str]) -> None:
    values = [item[key] for item in items]
    duplicates = sorted({value for value in values if values.count(value) > 1})
    require(not duplicates, f"duplicate {label} {key}: {duplicates}", errors)


def validate_schema(
    items: list[dict], schema_name: str, label: str, errors: list[str]
) -> None:
    schema = load_json(ROOT / "schemas" / SCHEMA_FILES[schema_name])
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    for item in items:
        for issue in validator.iter_errors(item):
            errors.append(f"{label} {item.get('id', item.get('name'))}: {issue.message}")


def main() -> None:
    errors: list[str] = []
    manifest = load_yaml(MANIFEST_PATH)
    prompts = [load_yaml(path) for path in sorted(PROMPT_DIR.glob("*.yaml"))]
    experiences = load_yaml(EXPERIENCE_PATH)["experience_types"]
    scaffold = load_yaml(SCAFFOLD_PATH)
    components = scaffold["components"]
    profiles = scaffold["profiles"]
    workflows = [load_yaml(path) for path in sorted(WORKFLOW_DIR.glob("*.yaml"))]
    skill_catalog = load_yaml(SKILL_CATALOG_PATH)
    skills = skill_catalog["skills"]
    tools = load_yaml(TOOL_REGISTRY_PATH)["tools"]
    role_map = load_yaml(ROLE_MAP_PATH)
    config = load_yaml(CONFIG_PATH)

    collections = [
        (prompts, "prompt"),
        (experiences, "experience"),
        (components, "scaffold component"),
        (profiles, "scaffold profile"),
        (workflows, "workflow"),
        (skills, "skill"),
        (tools, "tool"),
    ]
    for items, label in collections:
        check_unique(items, "id", label, errors)
        check_unique(items, "name", label, errors)

    validate_schema(prompts, "prompt", "prompt", errors)
    validate_schema(experiences, "experience", "experience", errors)
    validate_schema(components, "scaffold", "scaffold", errors)
    validate_schema(workflows, "workflow", "workflow", errors)
    validate_schema(tools, "tool", "tool", errors)

    prompt_names = {item["name"] for item in prompts}
    prompt_ids = {item["id"] for item in prompts}
    skill_names = {item["name"] for item in skills}
    skill_ids = {item["id"] for item in skills}
    tool_names = {item["name"] for item in tools}
    component_ids = {item["id"] for item in components}
    workflow_ids = {item["id"] for item in workflows}

    for prompt in prompts:
        expected_path = PROMPT_DIR / f"{prompt['name']}.yaml"
        require(expected_path.is_file(), f"missing prompt file: {expected_path}", errors)

    for skill in skills:
        skill_path = ROOT / skill["path"]
        require(skill_path.is_file(), f"missing skill body: {skill['path']}", errors)
        if not skill_path.is_file():
            continue
        text = skill_path.read_text(encoding="utf-8")
        frontmatter_match = SKILL_FRONTMATTER.match(text)
        require(frontmatter_match is not None, f"missing frontmatter: {skill['path']}", errors)
        if frontmatter_match:
            frontmatter = yaml.safe_load(frontmatter_match.group(1)) or {}
            require(
                frontmatter.get("name") == skill["name"],
                f"skill name mismatch: {skill['path']}",
                errors,
            )
        tool_section = SKILL_TOOL_SECTION.search(text)
        body_tools = set(BACKTICK_BULLET.findall(tool_section.group(1))) if tool_section else set()
        require(
            body_tools == set(skill["allowed_tools"]),
            f"skill tool mismatch {skill['name']}: catalog={skill['allowed_tools']} body={sorted(body_tools)}",
            errors,
        )
        unknown_tools = sorted(set(skill["allowed_tools"]) - tool_names)
        require(not unknown_tools, f"skill {skill['name']} has unknown tools: {unknown_tools}", errors)

    for workflow in workflows:
        node_ids = {node["id"] for node in workflow["nodes"]}
        for node in workflow["nodes"]:
            require(
                node["prompt"] in prompt_names,
                f"workflow {workflow['id']} references unknown prompt {node['prompt']}",
                errors,
            )
            unknown_skills = sorted(set(node["skills"]) - skill_names)
            require(
                not unknown_skills,
                f"workflow {workflow['id']} node {node['id']} has unknown skills: {unknown_skills}",
                errors,
            )
        for edge in workflow["edges"]:
            require(
                edge["from"] in node_ids and edge["to"] in node_ids,
                f"workflow {workflow['id']} has invalid edge {edge['from']} -> {edge['to']}",
                errors,
            )

    for profile in profiles:
        require(
            profile["workflow"] in workflow_ids,
            f"profile {profile['id']} references unknown workflow {profile['workflow']}",
            errors,
        )
        unknown_components = sorted(set(profile["components"]) - component_ids)
        require(
            not unknown_components,
            f"profile {profile['id']} has unknown components: {unknown_components}",
            errors,
        )

    for mapping in role_map["role_asset_map"]:
        require(
            set(mapping["prompts"]) <= prompt_ids,
            f"role {mapping['role_id']} has unknown prompt IDs",
            errors,
        )
        require(
            set(mapping["skills"]) <= skill_ids,
            f"role {mapping['role_id']} has unknown skill IDs",
            errors,
        )
        require(
            set(mapping["workflows"]) <= workflow_ids,
            f"role {mapping['role_id']} has unknown workflow IDs",
            errors,
        )

    configured_workflows = {
        config["baseline_workflow"],
        *config["fixed_workflow_baselines"],
        *config["evolution_workflows"].values(),
    }
    configured_skills = {
        *config["core_skills"],
        *config["control_skills"],
        *config["workflow_dependency_skills"],
        *config["meta_skills"],
    }
    require(config["base_scaffold_profile"] in {item["id"] for item in profiles}, "unknown base profile", errors)
    require(config["global_prompt"] in prompt_ids, "unknown global prompt", errors)
    require(configured_workflows <= workflow_ids, "config references unknown workflows", errors)
    require(configured_skills == skill_ids, "config skill selection differs from catalog", errors)

    inventory = manifest["inventory"]
    count_checks = {
        "prompts": len(prompts),
        "experience_record_types": len(experiences),
        "scaffold_components": len(components),
        "scaffold_profiles": len(profiles),
        "workflows": len(workflows),
        "skills": len(skills),
        "tools": len(tools),
    }
    for key, actual in count_checks.items():
        require(
            inventory[key]["count"] == actual,
            f"manifest count mismatch {key}: expected {inventory[key]['count']}, found {actual}",
            errors,
        )

    csv_files = list(ROOT.rglob("*.csv"))
    non_schema_json = [path for path in ROOT.rglob("*.json") if path.parent.name != "schemas"]
    require(not csv_files, f"CSV duplicates are not allowed: {csv_files}", errors)
    require(not non_schema_json, f"non-schema JSON files are not allowed: {non_schema_json}", errors)

    if errors:
        print("VALIDATION FAILED")
        for error in errors:
            print(f"- {error}")
        raise SystemExit(1)

    print(
        "VALID: "
        f"{len(prompts)} prompts, {len(experiences)} experience records, "
        f"{len(components)} scaffold components, {len(profiles)} profiles, "
        f"{len(workflows)} workflows, {len(skills)} skills, {len(tools)} tools"
    )


if __name__ == "__main__":
    main()
