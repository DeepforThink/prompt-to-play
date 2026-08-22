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
variables: stableId, entityKind, prefab, root (Node3D), scale (Vector3), path
(Vector3[]), seed (uint), primary/accent/ground/emissive (Color). For regions,
root is positioned at the region surface, scale is its full size, prefab is its
semantic kind, and geometry must use local coordinates. For roads, root is at
the world origin, scale.X is road width, path contains world-space waypoints,
and prefab is the road kind. The host already creates region and road collision;
generate their visible terrain and continuous path geometry. Use only Godot
node, mesh, shape, material, Vector3, Color, Mathf, RandomNumberGenerator, and
PromptToPlay.PrimitiveFactory APIs. Add useful collision bodies for other solid
objects. Do not read files, load resources, use networking, processes,
reflection, environment variables, signals, scene-tree traversal, or external
assets. Keep all geometry below 160 nodes total and make silhouettes visibly
specific to the prompt. Prioritize coherent terrain and readable roads over
small decorative detail.
"""

RESPONSE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "entity_ids": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 64,
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


def _entity_segments(world: Mapping[str, Any], limit: int) -> list[tuple[str, list[dict[str, Any]]]]:
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
    placed = candidates[:limit]
    regions = [
        {
            "kind": "region",
            "id": value["id"],
            "prefab": value["kind"],
            "scale": copy.deepcopy(value["size"]),
            "center": copy.deepcopy(value["center"]),
            "elevation": value["elevation"],
        }
        for value in world["regions"]
    ]
    roads = [
        {
            "kind": "road",
            "id": value["id"],
            "prefab": value["kind"],
            "scale": [value["width"], 1.0, 1.0],
            "from": value["from"],
            "to": value["to"],
            "waypoints": copy.deepcopy(value["waypoints"]),
        }
        for value in world["roads"]
    ]
    return [
        ("regions", regions),
        ("roads", roads),
        ("objects", placed),
    ]


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
        string entityKind,
        string prefab,
        Node3D root,
        Vector3 scale,
        Vector3[] path,
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


def _generate_segment(
    runtime: agents.MultiAgentRuntime,
    prompt: str,
    style: Mapping[str, Any],
    segment_name: str,
    selected: list[dict[str, Any]],
    *,
    revision: int,
    visual_feedback: Mapping[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    expected_ids = tuple(item["id"] for item in selected)
    if not expected_ids:
        return "", {
            "name": segment_name,
            "status": "empty",
            "entity_ids": [],
            "error_type": None,
        }

    payload = {
        "original_prompt": prompt,
        "style": copy.deepcopy(dict(style)),
        "segment": segment_name,
        "entities": selected,
        "revision": revision,
        "visual_feedback": (
            copy.deepcopy(dict(visual_feedback))
            if isinstance(visual_feedback, Mapping)
            else None
        ),
    }
    task_id = f"code_{segment_name}_{revision + 1:02d}"
    try:
        provider = runtime.subagent(agents.AgentRole.CODE_OBJECT, task_id)
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
            schema_name=f"prompt_to_play_code_{segment_name}",
        )
        return _validate_response(response, expected_ids), {
            "name": segment_name,
            "status": "generated",
            "entity_ids": list(expected_ids),
            "error_type": None,
        }
    except Exception as exc:
        return "", {
            "name": segment_name,
            "status": "fallback",
            "entity_ids": [],
            "error_type": type(exc).__name__,
        }


def generate_code_objects(
    runtime: agents.MultiAgentRuntime,
    prompt: str,
    world_document: Any,
    project_root: str | Path,
    *,
    environment: Mapping[str, str] | None = None,
    revision: int = 0,
    visual_feedback: Mapping[str, Any] | None = None,
) -> CodeObjectResult:
    world = contracts.validate_world(world_document)
    root = Path(project_root).resolve(strict=True)
    output = root / GENERATED_SOURCE_PATH
    output.parent.mkdir(parents=True, exist_ok=True)
    segments = _entity_segments(world, max_entities(environment))
    all_selected = [item for _name, values in segments for item in values]
    expected_ids = tuple(item["id"] for item in all_selected)
    if not all_selected:
        output.write_text(default_source(), encoding="utf-8", newline="\n")
        return CodeObjectResult(
            status="empty",
            entity_ids=(),
            source_path=output,
            record={
                "schema": CODE_OBJECT_SCHEMA,
                "status": "empty",
                "revision": revision,
                "entity_ids": [],
            },
        )

    bodies: list[str] = []
    segment_records: list[dict[str, Any]] = []
    for segment_name, selected in segments:
        body, segment_record = _generate_segment(
            runtime,
            prompt,
            world["style"],
            segment_name,
            selected,
            revision=revision,
            visual_feedback=visual_feedback,
        )
        if body:
            bodies.append(body)
        segment_records.append(segment_record)

    generated_ids = tuple(
        entity_id
        for segment in segment_records
        for entity_id in segment["entity_ids"]
    )
    source = _wrap_source("\n".join(bodies) if bodies else None)
    generated_count = sum(
        segment["status"] == "generated" for segment in segment_records
    )
    status = (
        "generated" if generated_count == len(segment_records)
        else "partial" if generated_count else "fallback"
    )
    error_types = [
        f"{segment['name']}:{segment['error_type']}"
        for segment in segment_records
        if segment["error_type"]
    ]
    error_type = ";".join(error_types) or None

    output.write_text(source, encoding="utf-8", newline="\n")
    source_sha256 = contracts.document_sha256({"source": source})
    record = {
        "schema": CODE_OBJECT_SCHEMA,
        "status": status,
        "revision": revision,
        "entity_ids": list(generated_ids),
        "source_sha256": source_sha256,
        "error_type": error_type,
        "segments": segment_records,
    }
    return CodeObjectResult(status, generated_ids, output, record)


__all__ = [
    "CODE_OBJECT_MAX_ENTITIES_ENV",
    "CODE_OBJECT_SCHEMA",
    "CodeObjectResult",
    "GENERATED_SOURCE_PATH",
    "default_source",
    "generate_code_objects",
    "max_entities",
]
