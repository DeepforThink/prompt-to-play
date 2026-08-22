"""LLM-authored Godot object code with a fixed host wrapper and fallback."""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import agents, contracts


CODE_OBJECT_SCHEMA = "prompt-to-play/code-objects@1"
CODE_OBJECT_MAX_ENTITIES_ENV = "PROMPT_TO_PLAY_CODE_OBJECT_MAX_ENTITIES"
DEFAULT_CODE_OBJECT_MAX_ENTITIES = 16
GENERATED_SOURCE_PATH = Path("scripts/GeneratedCodeObjects.cs")

SYSTEM_PROMPT = """
You are CodeObjectAgent. Write the body of one C# switch statement that builds
distinct Godot 4.7 C# geometry for the supplied WorldSpec entities. Return JSON
only. Emit exactly one `case "stable_id":` for every requested entity ID and
finish each case with `return true;`. The body runs inside a method with these
variables: stableId, prefab, root (Node3D), scale (Vector3), seed (uint),
primary/accent/ground/emissive (Color). Use only Godot node, mesh, shape, material,
Vector3, Color, Mathf, RandomNumberGenerator, and PromptToPlay.PrimitiveFactory
APIs. Add useful collision bodies for solid objects. Do not read files, load
resources, use networking, processes, reflection, environment variables,
signals, scene-tree traversal, or external assets. Keep all geometry below 96
nodes total and make silhouettes visibly specific to the prompt.
"""

RESPONSE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "entity_ids": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": DEFAULT_CODE_OBJECT_MAX_ENTITIES,
        },
        "csharp_switch_body": {"type": "string", "minLength": 1, "maxLength": 50000},
    },
    "required": ["entity_ids", "csharp_switch_body"],
    "additionalProperties": False,
}

_FORBIDDEN = re.compile(
    r"(?i)(system\.(io|net|diagnostics|reflection)|\b(file|directory|process|"
    r"environment|assembly|activator|marshal|dllimport|unsafe|dynamic)\b|"
    r"gd\.load|resourceloader|packedscene|http|\\|res://|user://|\.gettree\s*\()"
)


@dataclass(frozen=True)
class CodeObjectResult:
    status: str
    entity_ids: tuple[str, ...]
    source_path: Path
    record: Mapping[str, Any]


def max_entities(environment: Mapping[str, str] | None = None) -> int:
    env = os.environ if environment is None else environment
    raw = env.get(
        CODE_OBJECT_MAX_ENTITIES_ENV, str(DEFAULT_CODE_OBJECT_MAX_ENTITIES)
    ).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{CODE_OBJECT_MAX_ENTITIES_ENV} must be an integer") from exc
    if not 1 <= value <= DEFAULT_CODE_OBJECT_MAX_ENTITIES:
        raise ValueError(
            f"{CODE_OBJECT_MAX_ENTITIES_ENV} must be between 1 and "
            f"{DEFAULT_CODE_OBJECT_MAX_ENTITIES}"
        )
    return value


def _entities(world: Mapping[str, Any], limit: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for kind, values in (
        ("building", world["buildings"]),
        ("interactable", world["interactions"]["interactables"]),
        ("prop", world["props"]),
    ):
        for value in values:
            candidates.append(
                {
                    "kind": kind,
                    "id": value["id"],
                    "prefab": value["prefab"],
                    "scale": copy.deepcopy(value.get("scale", [1.0, 1.0, 1.0])),
                }
            )
    return candidates[:limit]


def default_source() -> str:
    return _wrap_source(None)


def _wrap_source(body: str | None) -> str:
    switch_body = "" if body is None else body.strip()
    if switch_body:
        switch_body = "\n".join(f"                {line}" for line in switch_body.splitlines())
        switch_body += "\n"
    return f"""using Godot;

namespace PromptToPlay;

public static class GeneratedCodeObjects
{{
    public static bool TryBuild(
        string stableId,
        string prefab,
        Node3D root,
        Vector3 scale,
        uint seed,
        Color primary,
        Color accent,
        Color ground,
        Color emissive,
        out string failure)
    {{
        failure = string.Empty;
        try
        {{
            switch (stableId)
            {{
{switch_body}                default:
                    return false;
            }}
        }}
        catch (Exception exception)
        {{
            failure = exception.GetType().Name;
            return false;
        }}
    }}
}}
"""


def _validate_response(document: Any, expected_ids: tuple[str, ...]) -> str:
    if not isinstance(document, Mapping) or set(document) != {
        "entity_ids",
        "csharp_switch_body",
    }:
        raise ValueError("CodeObjectAgent output does not match the host contract")
    entity_ids = document["entity_ids"]
    if not isinstance(entity_ids, list) or tuple(entity_ids) != expected_ids:
        raise ValueError("CodeObjectAgent entity_ids do not match the assigned entities")
    body = document["csharp_switch_body"]
    if not isinstance(body, str) or not body.strip() or len(body) > 50000:
        raise ValueError("CodeObjectAgent csharp_switch_body is invalid")
    if _FORBIDDEN.search(body):
        raise ValueError("CodeObjectAgent code uses a forbidden API")
    for entity_id in expected_ids:
        if body.count(f'case "{entity_id}":') != 1:
            raise ValueError(
                f"CodeObjectAgent code must contain exactly one case for {entity_id!r}"
            )
    if "return true;" not in body:
        raise ValueError("CodeObjectAgent code never reports a generated object")
    return body.strip()


def generate_code_objects(
    runtime: agents.MultiAgentRuntime,
    prompt: str,
    world_document: Any,
    project_root: str | Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> CodeObjectResult:
    world = contracts.validate_world(world_document)
    root = Path(project_root).resolve(strict=True)
    output = root / GENERATED_SOURCE_PATH
    output.parent.mkdir(parents=True, exist_ok=True)
    selected = _entities(world, max_entities(environment))
    expected_ids = tuple(item["id"] for item in selected)
    if not selected:
        output.write_text(default_source(), encoding="utf-8", newline="\n")
        return CodeObjectResult(
            status="empty",
            entity_ids=(),
            source_path=output,
            record={"schema": CODE_OBJECT_SCHEMA, "status": "empty", "entity_ids": []},
        )

    payload = {
        "original_prompt": prompt,
        "style": copy.deepcopy(world["style"]),
        "entities": selected,
    }
    try:
        provider = runtime.subagent(agents.AgentRole.CODE_OBJECT, "code_objects_01")
        response = provider.generate_json(
            [
                {"role": "system", "content": SYSTEM_PROMPT.strip()},
                {
                    "role": "user",
                    "content": json.dumps(
                        payload, ensure_ascii=False, separators=(",", ":")
                    ),
                },
            ],
            json_schema=RESPONSE_SCHEMA,
            schema_name="prompt_to_play_code_objects",
        )
        body = _validate_response(response, expected_ids)
        source = _wrap_source(body)
        status = "generated"
        error_type = None
    except Exception as exc:
        source = default_source()
        status = "fallback"
        error_type = type(exc).__name__

    output.write_text(source, encoding="utf-8", newline="\n")
    source_sha256 = contracts.document_sha256({"source": source})
    record = {
        "schema": CODE_OBJECT_SCHEMA,
        "status": status,
        "entity_ids": list(expected_ids if status == "generated" else ()),
        "source_sha256": source_sha256,
        "error_type": error_type,
    }
    return CodeObjectResult(status, expected_ids if status == "generated" else (), output, record)


__all__ = [
    "CODE_OBJECT_MAX_ENTITIES_ENV",
    "CODE_OBJECT_SCHEMA",
    "CodeObjectResult",
    "GENERATED_SOURCE_PATH",
    "default_source",
    "generate_code_objects",
    "max_entities",
]
