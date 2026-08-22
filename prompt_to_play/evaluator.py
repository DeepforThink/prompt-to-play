"""API-backed visual evaluation and repair agents for Prompt-to-Play.

The model-facing boundary in this module is intentionally narrow.  The visual
agent may report only prompt similarity and actionable visual issues.  Timing,
token usage, structural checks, aggregate scores, and acceptance policy remain
system-owned observations handled by the orchestrator.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # Support package imports and direct local execution.
    from . import contracts, planner
    from .planner import StructuredProvider
    from .provider import ProviderError, create_provider_from_env
except ImportError:  # pragma: no cover
    import contracts
    import planner
    from planner import StructuredProvider
    from provider import ProviderError, create_provider_from_env


VISUAL_EVALUATION_AGENT_NAME = "VisualEvaluationAgent"
REPAIR_AGENT_NAME = "RepairAgent"
VISUAL_EVALUATION_AGENT_ROLE = "visual_evaluator"
REPAIR_AGENT_ROLE = "repair"
DEFAULT_MIN_SCENE_SIMILARITY = 0.75
ISSUE_SEVERITIES = ("info", "minor", "major", "blocker")
BLOCKING_SEVERITIES = frozenset(("major", "blocker"))


class EvaluatorError(ValueError):
    """Raised when visual feedback or an attempted repair is unsafe or invalid."""


def _object(properties: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(properties),
        "additionalProperties": False,
    }


VISUAL_ISSUE_JSON_SCHEMA = _object(
    {
        "code": {
            "type": "string",
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
        },
        "severity": {"type": "string", "enum": list(ISSUE_SEVERITIES)},
        "entity_id": {
            "anyOf": [
                {
                    "type": "string",
                    "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
                },
                {"type": "null"},
            ]
        },
        "message": {"type": "string", "minLength": 1},
        "suggested_fix": {"type": "string", "minLength": 1},
    }
)

VISUAL_FEEDBACK_JSON_SCHEMA = _object(
    {
        "scene_similarity": {"type": "number", "minimum": 0, "maximum": 1},
        "accepted": {"type": "boolean"},
        "issues": {
            "type": "array",
            "items": VISUAL_ISSUE_JSON_SCHEMA,
            "minItems": 0,
            "maxItems": 32,
        },
    }
)


VISUAL_EVALUATION_SYSTEM_PROMPT = """
You are the VisualEvaluationAgent in a prompt-to-play system. Compare the
rendered screenshots with the original scene prompt, any supplied reference
images, and the supplied WorldSpec. Reference images are attached first and
rendered screenshots second; their exact counts are included in the request.
Return only the strict visual-feedback JSON object described by the schema.

Judge composition, recognisable requested landmarks, atmosphere, materials,
lighting, visual coherence, and whether the screenshots visibly express the
prompt. Use entity_id only when a WorldSpec entity is responsible; otherwise
use null. Give stable machine-readable issue codes and concrete suggested fixes.

Do not report or infer generation time, token usage, cost, structural validity,
reproducibility, aggregate evaluation scores, correction limits, or system
policy. Those measurements belong exclusively to the host orchestrator.
"""


REPAIR_SYSTEM_PROMPT = """
You are the RepairAgent in a prompt-to-play system. Return one corrected,
complete WorldSpec JSON object using the supplied schema. Use the original
prompt, current complete WorldSpec, visual feedback, any supplied reference
images, rendered screenshots, and host-reported structural failures. Reference
images are attached first and rendered screenshots second; their exact counts
are included in the request.

Preserve schema, world_id, seed, brief, style, units, and player_spawn exactly.
Preserve the relative order of existing entities and append any new entities.
Only repair fields supported by the host PatchSpec allowlist. Keep every ID
globally unique, every reference resolvable, every region reachable from the
spawn through roads, and at least one fixed or orbit evaluation camera.

Do not output a patch, prose, Markdown, timing, token usage, budgets, evaluation
policy, or system metrics. The host validates the complete WorldSpec and derives
the deterministic PatchSpec itself.
"""


_COLLECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("region", ("regions",)),
    ("road", ("roads",)),
    ("building", ("buildings",)),
    ("prop", ("props",)),
    ("light", ("lights",)),
    ("interactable", ("interactions", "interactables")),
    ("objective", ("interactions", "objectives")),
    ("camera", ("cameras",)),
)


def _nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise EvaluatorError(f"{path}: expected a string")
    if not value.strip():
        raise EvaluatorError(f"{path}: must not be empty")
    return value


def _similarity_threshold(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluatorError("min_scene_similarity: expected a number")
    threshold = float(value)
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise EvaluatorError("min_scene_similarity: expected a value from 0 to 1")
    return threshold


def _known_entity_ids(world: Mapping[str, Any]) -> set[str]:
    entity_ids: set[str] = set()
    for _kind, path in _COLLECTIONS:
        collection: Any = world
        for key in path:
            collection = collection[key]
        entity_ids.update(entity["id"] for entity in collection)
    entity_ids.add(world["interactions"]["exit"]["id"])
    return entity_ids


def validate_visual_feedback(
    document: Any,
    world_document: Any | None = None,
    *,
    min_scene_similarity: float = DEFAULT_MIN_SCENE_SIMILARITY,
) -> dict[str, Any]:
    """Validate model feedback and recompute its visual-only acceptance gate.

    ``accepted`` is present in the structured model response for auditability,
    but it is not trusted: it must agree with the host-owned threshold and the
    absence of major/blocker issues.
    """

    threshold = _similarity_threshold(min_scene_similarity)
    if not isinstance(document, Mapping):
        raise EvaluatorError("visual feedback: expected an object")
    required = {"scene_similarity", "accepted", "issues"}
    missing = sorted(required - set(document))
    extra = sorted(set(document) - required)
    if missing:
        raise EvaluatorError("visual feedback: missing keys: " + ", ".join(missing))
    if extra:
        raise EvaluatorError("visual feedback: unknown keys: " + ", ".join(extra))

    score_value = document["scene_similarity"]
    if isinstance(score_value, bool) or not isinstance(score_value, (int, float)):
        raise EvaluatorError("visual feedback.scene_similarity: expected a number")
    score = float(score_value)
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise EvaluatorError(
            "visual feedback.scene_similarity: expected a value from 0 to 1"
        )
    if not isinstance(document["accepted"], bool):
        raise EvaluatorError("visual feedback.accepted: expected a boolean")
    if not isinstance(document["issues"], list):
        raise EvaluatorError("visual feedback.issues: expected an array")
    if len(document["issues"]) > 32:
        raise EvaluatorError("visual feedback.issues: at most 32 issues are allowed")

    known_ids: set[str] | None = None
    if world_document is not None:
        try:
            world = contracts.validate_world(world_document)
        except contracts.ContractError as exc:
            raise EvaluatorError(f"current WorldSpec is invalid: {exc}") from exc
        known_ids = _known_entity_ids(world)

    issues: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    for index, value in enumerate(document["issues"]):
        path = f"visual feedback.issues[{index}]"
        if not isinstance(value, Mapping):
            raise EvaluatorError(f"{path}: expected an object")
        issue_keys = {"code", "severity", "entity_id", "message", "suggested_fix"}
        missing_issue = sorted(issue_keys - set(value))
        extra_issue = sorted(set(value) - issue_keys)
        if missing_issue:
            raise EvaluatorError(f"{path}: missing keys: {', '.join(missing_issue)}")
        if extra_issue:
            raise EvaluatorError(f"{path}: unknown keys: {', '.join(extra_issue)}")
        code = _nonempty_string(value["code"], f"{path}.code")
        if not contracts.ENTITY_ID_RE.fullmatch(code):
            raise EvaluatorError(f"{path}.code: invalid stable issue code")
        if code in seen_codes:
            raise EvaluatorError(f"{path}.code: duplicate issue code {code!r}")
        seen_codes.add(code)
        severity = value["severity"]
        if severity not in ISSUE_SEVERITIES:
            raise EvaluatorError(
                f"{path}.severity: expected one of: {', '.join(ISSUE_SEVERITIES)}"
            )
        entity_id = value["entity_id"]
        if entity_id is not None:
            entity_id = _nonempty_string(entity_id, f"{path}.entity_id")
            if not contracts.ENTITY_ID_RE.fullmatch(entity_id):
                raise EvaluatorError(f"{path}.entity_id: invalid entity ID")
            if known_ids is not None and entity_id not in known_ids:
                raise EvaluatorError(
                    f"{path}.entity_id: unknown WorldSpec entity {entity_id!r}"
                )
        issues.append(
            {
                "code": code,
                "severity": severity,
                "entity_id": entity_id,
                "message": _nonempty_string(value["message"], f"{path}.message"),
                "suggested_fix": _nonempty_string(
                    value["suggested_fix"], f"{path}.suggested_fix"
                ),
            }
        )

    expected_accepted = score >= threshold and not any(
        issue["severity"] in BLOCKING_SEVERITIES for issue in issues
    )
    if document["accepted"] != expected_accepted:
        raise EvaluatorError(
            "visual feedback.accepted: must equal the host visual gate "
            f"({expected_accepted!r})"
        )
    if not expected_accepted and not issues:
        raise EvaluatorError(
            "visual feedback.issues: rejected feedback requires an actionable issue"
        )
    return {
        "scene_similarity": score,
        "accepted": expected_accepted,
        "issues": issues,
    }


def _resolve_images(
    image_paths: Sequence[str | Path],
    project_root: str | Path | None,
) -> tuple[Path, list[Path]]:
    root = (Path.cwd() if project_root is None else Path(project_root)).resolve()
    if not root.is_dir():
        raise EvaluatorError(f"project_root: not a directory: {root}")
    if not image_paths:
        raise EvaluatorError("screenshot_image_paths: at least one screenshot is required")
    resolved: list[Path] = []
    for index, supplied_value in enumerate(image_paths):
        supplied = Path(supplied_value)
        image = (supplied if supplied.is_absolute() else root / supplied).resolve()
        if not image.is_file():
            raise EvaluatorError(
                f"screenshot_image_paths[{index}]: file does not exist: {supplied}"
            )
        resolved.append(image)
    return root, resolved


def _resolve_reference_images(
    image_paths: Sequence[str | Path], root: Path
) -> list[Path]:
    resolved: list[Path] = []
    for index, supplied_value in enumerate(image_paths):
        supplied = Path(supplied_value)
        image = (supplied if supplied.is_absolute() else root / supplied).resolve()
        if not image.is_file():
            raise EvaluatorError(
                f"reference_image_paths[{index}]: file does not exist: {supplied}"
            )
        resolved.append(image)
    return resolved


def _structural_failures(
    structural_report: Mapping[str, Any] | None,
) -> list[dict[str, str]]:
    if structural_report is None:
        return []
    if not isinstance(structural_report, Mapping):
        raise EvaluatorError("structural_report: expected an object")
    checks = structural_report.get("checks")
    if not isinstance(checks, list):
        raise EvaluatorError("structural_report.checks: expected an array")
    failures: list[dict[str, str]] = []
    for index, value in enumerate(checks):
        if not isinstance(value, Mapping):
            raise EvaluatorError(
                f"structural_report.checks[{index}]: expected an object"
            )
        if value.get("passed") is not False:
            continue
        check_id = _nonempty_string(
            value.get("id"), f"structural_report.checks[{index}].id"
        )
        message = _nonempty_string(
            value.get("message"), f"structural_report.checks[{index}].message"
        )
        failures.append({"id": check_id, "message": message})
    return failures


def _active_provider(
    configured: StructuredProvider | None, root: Path
) -> StructuredProvider:
    if configured is not None:
        return configured
    try:
        return create_provider_from_env(repo=root)
    except ProviderError as exc:
        raise EvaluatorError(f"structured provider configuration failed: {exc}") from exc


class VisualEvaluationAgent:
    """Model-backed agent that emits only locally checked visual feedback."""

    name = VISUAL_EVALUATION_AGENT_NAME
    role = VISUAL_EVALUATION_AGENT_ROLE

    def __init__(
        self,
        provider: StructuredProvider | None = None,
        *,
        project_root: str | Path | None = None,
        min_scene_similarity: float = DEFAULT_MIN_SCENE_SIMILARITY,
    ) -> None:
        self.provider = provider
        self.project_root = project_root
        self.min_scene_similarity = _similarity_threshold(min_scene_similarity)

    def evaluate(
        self,
        prompt: str,
        world_document: Any,
        screenshot_image_paths: Sequence[str | Path],
        *,
        reference_image_paths: Sequence[str | Path] = (),
    ) -> dict[str, Any]:
        prompt = _nonempty_string(prompt, "prompt")
        try:
            world = contracts.validate_world(world_document)
        except contracts.ContractError as exc:
            raise EvaluatorError(f"current WorldSpec is invalid: {exc}") from exc
        if prompt != world["brief"]["text"]:
            raise EvaluatorError("prompt: must equal current WorldSpec brief.text")
        root, screenshots = _resolve_images(
            screenshot_image_paths, self.project_root
        )
        references = _resolve_reference_images(reference_image_paths, root)
        payload = {
            "agent": self.name,
            "role": self.role,
            "original_prompt": prompt,
            "current_world": world,
            "host_visual_gate": {
                "min_scene_similarity": self.min_scene_similarity,
                "blocking_severities": sorted(BLOCKING_SEVERITIES),
            },
            "reference_image_count": len(references),
            "screenshot_count": len(screenshots),
            "attached_image_order": "reference_images_then_rendered_screenshots",
        }
        messages = [
            {
                "role": "system",
                "content": VISUAL_EVALUATION_SYSTEM_PROMPT.strip(),
            },
            {
                "role": "user",
                "content": json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":")
                ),
            },
        ]
        try:
            response = _active_provider(self.provider, root).generate_json(
                messages,
                json_schema=copy.deepcopy(VISUAL_FEEDBACK_JSON_SCHEMA),
                schema_name="prompt_to_play_visual_feedback",
                image_paths=[*references, *screenshots],
            )
        except ProviderError as exc:
            raise EvaluatorError(f"VisualEvaluationAgent provider failed: {exc}") from exc
        except (OSError, RuntimeError) as exc:
            raise EvaluatorError(f"VisualEvaluationAgent provider failed: {exc}") from exc
        return validate_visual_feedback(
            response,
            world,
            min_scene_similarity=self.min_scene_similarity,
        )


class RepairAgent:
    """Model-backed repair planner with a deterministic PatchSpec boundary."""

    name = REPAIR_AGENT_NAME
    role = REPAIR_AGENT_ROLE

    def __init__(
        self,
        provider: StructuredProvider | None = None,
        *,
        project_root: str | Path | None = None,
        min_scene_similarity: float = DEFAULT_MIN_SCENE_SIMILARITY,
    ) -> None:
        self.provider = provider
        self.project_root = project_root
        self.min_scene_similarity = _similarity_threshold(min_scene_similarity)

    def revise_world(
        self,
        prompt: str,
        current_world_document: Any,
        feedback_document: Any,
        screenshot_image_paths: Sequence[str | Path],
        *,
        reference_image_paths: Sequence[str | Path] = (),
        structural_report: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        prompt = _nonempty_string(prompt, "prompt")
        try:
            current = contracts.validate_world(current_world_document)
        except contracts.ContractError as exc:
            raise EvaluatorError(f"current WorldSpec is invalid: {exc}") from exc
        if prompt != current["brief"]["text"]:
            raise EvaluatorError("prompt: must equal current WorldSpec brief.text")
        feedback = validate_visual_feedback(
            feedback_document,
            current,
            min_scene_similarity=self.min_scene_similarity,
        )
        root, screenshots = _resolve_images(
            screenshot_image_paths, self.project_root
        )
        references = _resolve_reference_images(reference_image_paths, root)
        structural_failures = _structural_failures(structural_report)

        response_schema = copy.deepcopy(planner.WORLD_SPEC_JSON_SCHEMA)
        response_schema["properties"]["world_id"] = {
            "type": "string",
            "const": current["world_id"],
        }
        response_schema["properties"]["seed"] = {
            "type": "integer",
            "const": current["seed"],
        }
        payload = {
            "agent": self.name,
            "role": self.role,
            "original_prompt": prompt,
            "current_world": current,
            "visual_feedback": feedback,
            "structural_failures": structural_failures,
            "reference_image_count": len(references),
            "screenshot_count": len(screenshots),
            "attached_image_order": "reference_images_then_rendered_screenshots",
            "authoritative_fields": {
                "schema": current["schema"],
                "world_id": current["world_id"],
                "seed": current["seed"],
                "brief": current["brief"],
            },
            "patchable_fields": {
                kind: sorted(fields)
                for kind, fields in contracts.PATCHABLE_FIELDS.items()
            },
        }
        messages = [
            {"role": "system", "content": REPAIR_SYSTEM_PROMPT.strip()},
            {
                "role": "user",
                "content": json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":")
                ),
            },
        ]
        try:
            response = _active_provider(self.provider, root).generate_json(
                messages,
                json_schema=response_schema,
                schema_name="prompt_to_play_revised_world_spec",
                image_paths=[*references, *screenshots],
            )
        except ProviderError as exc:
            raise EvaluatorError(f"RepairAgent provider failed: {exc}") from exc
        except (OSError, RuntimeError) as exc:
            raise EvaluatorError(f"RepairAgent provider failed: {exc}") from exc
        if not isinstance(response, Mapping):
            raise EvaluatorError("RepairAgent provider output: expected an object")
        revised = copy.deepcopy(dict(response))

        for key in ("schema", "world_id", "seed", "brief"):
            if contracts.canonical_json_bytes(revised.get(key)) != contracts.canonical_json_bytes(
                current[key]
            ):
                raise EvaluatorError(
                    f"RepairAgent changed authoritative WorldSpec field {key!r}"
                )
        try:
            contracts.validate_world(revised)
        except contracts.ContractError as exc:
            raise EvaluatorError(f"RepairAgent returned an invalid WorldSpec: {exc}") from exc
        semantic_preflight = getattr(planner, "_semantic_preflight", None)
        if callable(semantic_preflight):
            try:
                semantic_preflight(revised)
            except ValueError as exc:
                raise EvaluatorError(
                    f"RepairAgent returned a semantically invalid WorldSpec: {exc}"
                ) from exc
        return revised

    def create_patch(
        self,
        current_world_document: Any,
        revised_world_document: Any,
        iteration: int,
    ) -> dict[str, Any]:
        return diff_to_patch(
            current_world_document, revised_world_document, iteration
        )

    def repair(
        self,
        prompt: str,
        current_world_document: Any,
        feedback_document: Any,
        screenshot_image_paths: Sequence[str | Path],
        *,
        iteration: int,
        reference_image_paths: Sequence[str | Path] = (),
        structural_report: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return the validated complete revision and its deterministic patch."""

        revised = self.revise_world(
            prompt,
            current_world_document,
            feedback_document,
            screenshot_image_paths,
            reference_image_paths=reference_image_paths,
            structural_report=structural_report,
        )
        patch = self.create_patch(current_world_document, revised, iteration)
        return revised, patch


def evaluate_visual(
    prompt: str,
    world_document: Any,
    screenshot_image_paths: Sequence[str | Path],
    *,
    provider: StructuredProvider | None = None,
    project_root: str | Path | None = None,
    min_scene_similarity: float = DEFAULT_MIN_SCENE_SIMILARITY,
    reference_image_paths: Sequence[str | Path] = (),
) -> dict[str, Any]:
    """Convenience wrapper around :class:`VisualEvaluationAgent`."""

    return VisualEvaluationAgent(
        provider,
        project_root=project_root,
        min_scene_similarity=min_scene_similarity,
    ).evaluate(
        prompt,
        world_document,
        screenshot_image_paths,
        reference_image_paths=reference_image_paths,
    )


def revise_world(
    prompt: str,
    current_world_document: Any,
    feedback_document: Any,
    screenshot_image_paths: Sequence[str | Path],
    *,
    provider: StructuredProvider | None = None,
    project_root: str | Path | None = None,
    min_scene_similarity: float = DEFAULT_MIN_SCENE_SIMILARITY,
    reference_image_paths: Sequence[str | Path] = (),
    structural_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Convenience wrapper around :class:`RepairAgent`."""

    return RepairAgent(
        provider,
        project_root=project_root,
        min_scene_similarity=min_scene_similarity,
    ).revise_world(
        prompt,
        current_world_document,
        feedback_document,
        screenshot_image_paths,
        reference_image_paths=reference_image_paths,
        structural_report=structural_report,
    )


def _at_path(world: Mapping[str, Any], path: tuple[str, ...]) -> list[dict[str, Any]]:
    value: Any = world
    for key in path:
        value = value[key]
    return value


def _canonical_equal(first: Any, second: Any) -> bool:
    return contracts.canonical_json_bytes(first) == contracts.canonical_json_bytes(second)


def _entity_operations(
    current: Mapping[str, Any],
    revised: Mapping[str, Any],
    kind: str,
    path: tuple[str, ...],
) -> list[dict[str, Any]]:
    current_values = _at_path(current, path)
    revised_values = _at_path(revised, path)
    current_by_id = {value["id"]: value for value in current_values}
    revised_by_id = {value["id"]: value for value in revised_values}
    operations: list[dict[str, Any]] = []

    for entity in current_values:
        entity_id = entity["id"]
        if entity_id not in revised_by_id:
            operations.append(
                {"op": "remove", "target": {"kind": kind, "id": entity_id}}
            )

    for revised_entity in revised_values:
        entity_id = revised_entity["id"]
        current_entity = current_by_id.get(entity_id)
        target = {"kind": kind, "id": entity_id}
        if current_entity is None:
            operations.append(
                {"op": "upsert", "target": target, "value": copy.deepcopy(revised_entity)}
            )
            continue
        changed_fields = [
            key
            for key in sorted(set(current_entity) | set(revised_entity))
            if not _canonical_equal(current_entity.get(key), revised_entity.get(key))
        ]
        if not changed_fields:
            continue
        forbidden = sorted(set(changed_fields) - contracts.PATCHABLE_FIELDS[kind])
        if forbidden:
            raise EvaluatorError(
                f"nonpatchable change to {kind} {entity_id!r}: {', '.join(forbidden)}"
            )
        removed_fields = set(current_entity) - set(revised_entity)
        if removed_fields:
            operations.append(
                {"op": "upsert", "target": target, "value": copy.deepcopy(revised_entity)}
            )
        else:
            operations.append(
                {
                    "op": "update",
                    "target": target,
                    "changes": {
                        key: copy.deepcopy(revised_entity[key])
                        for key in changed_fields
                    },
                }
            )
    return operations


def diff_to_patch(
    current_world_document: Any,
    revised_world_document: Any,
    iteration: int,
) -> dict[str, Any]:
    """Create a deterministic, allowlisted PatchSpec from two valid worlds.

    Changes that the versioned PatchSpec cannot express are rejected instead of
    being smuggled through a broad model-generated replacement.
    """

    try:
        current = contracts.validate_world(current_world_document)
    except contracts.ContractError as exc:
        raise EvaluatorError(f"current WorldSpec is invalid: {exc}") from exc
    try:
        revised = contracts.validate_world(revised_world_document)
    except contracts.ContractError as exc:
        raise EvaluatorError(f"revised WorldSpec is invalid: {exc}") from exc
    if isinstance(iteration, bool) or not isinstance(iteration, int):
        raise EvaluatorError("iteration: expected an integer")
    if not 1 <= iteration <= 10:
        raise EvaluatorError("iteration: expected a value from 1 to 10")

    for key in ("schema", "world_id", "seed", "brief"):
        if not _canonical_equal(current[key], revised[key]):
            raise EvaluatorError(f"nonpatchable top-level change: {key}")
    for key in ("style", "units"):
        if not _canonical_equal(current[key], revised[key]):
            raise EvaluatorError(f"nonpatchable top-level change: {key}")
    if not _canonical_equal(
        current["interactions"]["player_spawn"],
        revised["interactions"]["player_spawn"],
    ):
        raise EvaluatorError("nonpatchable change: interactions.player_spawn")

    operations: list[dict[str, Any]] = []
    for kind, path in _COLLECTIONS:
        operations.extend(_entity_operations(current, revised, kind, path))

    current_exit = current["interactions"]["exit"]
    revised_exit = revised["interactions"]["exit"]
    if current_exit["id"] != revised_exit["id"]:
        operations.append(
            {
                "op": "upsert",
                "target": {"kind": "exit", "id": revised_exit["id"]},
                "value": copy.deepcopy(revised_exit),
            }
        )
    elif not _canonical_equal(current_exit, revised_exit):
        changed_fields = [
            key
            for key in sorted(set(current_exit) | set(revised_exit))
            if not _canonical_equal(current_exit.get(key), revised_exit.get(key))
        ]
        forbidden = sorted(set(changed_fields) - contracts.PATCHABLE_FIELDS["exit"])
        if forbidden:
            raise EvaluatorError(
                "nonpatchable change to exit "
                f"{current_exit['id']!r}: {', '.join(forbidden)}"
            )
        operations.append(
            {
                "op": "update",
                "target": {"kind": "exit", "id": current_exit["id"]},
                "changes": {
                    key: copy.deepcopy(revised_exit[key]) for key in changed_fields
                },
            }
        )

    if not operations:
        raise EvaluatorError("revised WorldSpec contains no patchable changes")

    base_hash = contracts.document_sha256(current)
    revision_hash = contracts.document_sha256(revised)
    identity_payload = {
        "base_world_sha256": base_hash,
        "revision_world_sha256": revision_hash,
        "iteration": iteration,
        "operations": operations,
    }
    identity_hash = contracts.document_sha256(identity_payload)
    patch = {
        "schema": contracts.PATCH_SCHEMA,
        "patch_id": f"visual_{iteration}_{identity_hash[:16]}",
        "base_world_id": current["world_id"],
        "base_world_sha256": base_hash,
        "iteration": iteration,
        "reasons": [
            f"Apply validated visual repair across {len(operations)} entity operation(s)."
        ],
        "operations": operations,
        "expected_checks": [
            "world_contract",
            "semantic_connectivity",
            "visual_similarity",
        ],
    }
    try:
        contracts.validate_patch(patch, current)
        applied = contracts.apply_patch(current, patch)
    except contracts.ContractError as exc:
        raise EvaluatorError(f"derived PatchSpec is invalid: {exc}") from exc
    if not _canonical_equal(applied, revised):
        raise EvaluatorError(
            "revised WorldSpec changes entity ordering or another value that "
            "PatchSpec cannot reproduce"
        )
    return patch


__all__ = [
    "DEFAULT_MIN_SCENE_SIMILARITY",
    "EvaluatorError",
    "REPAIR_AGENT_NAME",
    "REPAIR_AGENT_ROLE",
    "RepairAgent",
    "VISUAL_EVALUATION_AGENT_NAME",
    "VISUAL_EVALUATION_AGENT_ROLE",
    "VISUAL_FEEDBACK_JSON_SCHEMA",
    "VISUAL_ISSUE_JSON_SCHEMA",
    "VisualEvaluationAgent",
    "diff_to_patch",
    "evaluate_visual",
    "revise_world",
    "validate_visual_feedback",
]
