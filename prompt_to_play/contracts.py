"""Small, dependency-free contracts for Prompt-to-Play world generation.

The module deliberately uses explicit Python validation instead of JSON Schema so
published game repositories can validate and apply contracts with the Python
standard library alone.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


WORLD_SCHEMA = "prompt-to-play/world@1"
PATCH_SCHEMA = "prompt-to-play/patch@1"
EVALUATION_SCHEMA = "prompt-to-play/evaluation@1"
EVALUATION_POLICY_SCHEMA = "prompt-to-play/evaluation-policy@1"

UINT32_MAX = 2**32 - 1
ENTITY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
COLOR_RE = re.compile(r"^#[0-9A-F]{6}$")

METRIC_KEYS = (
    "scene_similarity",
    "structural_correctness",
    "automation_loop",
    "generation_speed",
    "token_efficiency",
    "reproducibility",
)
TARGET_KINDS = (
    "region",
    "road",
    "building",
    "prop",
    "light",
    "interactable",
    "objective",
    "exit",
    "camera",
)
PATCH_OPS = ("update", "upsert", "remove", "regenerate_region")

PATCHABLE_FIELDS: dict[str, frozenset[str]] = {
    "region": frozenset({"kind", "center", "size", "elevation", "generation_seed"}),
    "road": frozenset({"from", "to", "kind", "width", "waypoints"}),
    "building": frozenset({"region", "prefab", "position", "rotation_deg", "scale"}),
    "prop": frozenset({"region", "prefab", "position", "rotation_deg", "scale"}),
    "light": frozenset({"kind", "position", "rotation_deg", "color", "energy", "range"}),
    "interactable": frozenset(
        {"region", "prefab", "position", "action", "label", "duration_ms"}
    ),
    "objective": frozenset({"rule", "targets", "completion_text"}),
    "exit": frozenset({"region", "position", "requires"}),
    "camera": frozenset({"kind", "position", "look_at", "fov_deg", "resolution"}),
}


class ContractError(ValueError):
    """Raised when a contract is malformed or internally inconsistent."""


def _fail(path: str, message: str) -> None:
    raise ContractError(f"{path}: {message}")


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "expected an object")
    return value


def _object(
    value: Any,
    path: str,
    *,
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> Mapping[str, Any]:
    obj = _mapping(value, path)
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(obj))
    extra = sorted(set(obj) - allowed)
    if missing:
        _fail(path, f"missing keys: {', '.join(missing)}")
    if extra:
        _fail(path, f"unknown keys: {', '.join(extra)}")
    return obj


def _list(value: Any, path: str, *, min_length: int = 0) -> list[Any]:
    if not isinstance(value, list):
        _fail(path, "expected an array")
    if len(value) < min_length:
        _fail(path, f"expected at least {min_length} item(s)")
    return value


def _string(value: Any, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        _fail(path, "expected a string")
    if not allow_empty and not value.strip():
        _fail(path, "must not be empty")
    return value


def _identifier(value: Any, path: str) -> str:
    text = _string(value, path)
    if not ENTITY_ID_RE.fullmatch(text):
        _fail(path, "must match [A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
    return text


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        _fail(path, "expected a boolean")
    return value


def _integer(
    value: Any,
    path: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, "expected an integer")
    if minimum is not None and value < minimum:
        _fail(path, f"must be >= {minimum}")
    if maximum is not None and value > maximum:
        _fail(path, f"must be <= {maximum}")
    return value


def _number(
    value: Any,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "expected a number")
    result = float(value)
    if not math.isfinite(result):
        _fail(path, "must be finite")
    if minimum is not None and result < minimum:
        _fail(path, f"must be >= {minimum}")
    if maximum is not None and result > maximum:
        _fail(path, f"must be <= {maximum}")
    return result


def _vec(
    value: Any,
    path: str,
    length: int,
    *,
    positive: bool = False,
) -> list[Any]:
    values = _list(value, path)
    if len(values) != length:
        _fail(path, f"expected exactly {length} numbers")
    for index, item in enumerate(values):
        number = _number(item, f"{path}[{index}]")
        if positive and number <= 0:
            _fail(f"{path}[{index}]", "must be > 0")
    return values


def _enum(value: Any, path: str, choices: Sequence[str]) -> str:
    text = _string(value, path)
    if text not in choices:
        _fail(path, f"expected one of: {', '.join(choices)}")
    return text


def _sha256(value: Any, path: str) -> str:
    text = _string(value, path)
    if not SHA256_RE.fullmatch(text):
        _fail(path, "expected a lowercase 64-character SHA-256 digest")
    return text


def _color(value: Any, path: str) -> str:
    text = _string(value, path)
    if not COLOR_RE.fullmatch(text):
        _fail(path, "expected an uppercase #RRGGBB color")
    return text


def _relative_path(value: Any, path: str) -> str:
    text = _string(value, path)
    if "\\" in text:
        _fail(path, "must use forward slashes")
    if text.startswith(("/", "~")) or WINDOWS_DRIVE_RE.match(text):
        _fail(path, "must be a repository-relative path")
    parsed = PurePosixPath(text)
    if str(parsed) != text:
        _fail(path, "must be normalized")
    if parsed.is_absolute() or ".." in parsed.parts or not parsed.parts:
        _fail(path, "must not escape the repository")
    if any(part in ("", ".") for part in parsed.parts):
        _fail(path, "must be normalized")
    return text


def _unique_strings(values: Any, path: str, *, min_length: int = 0) -> list[str]:
    items = _list(values, path, min_length=min_length)
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        text = _identifier(item, f"{path}[{index}]")
        if text in seen:
            _fail(f"{path}[{index}]", f"duplicate value {text!r}")
        seen.add(text)
        result.append(text)
    return result


def _utc_timestamp(value: Any, path: str) -> datetime:
    text = _string(value, path)
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError as exc:
        _fail(path, f"invalid ISO-8601 timestamp: {exc}")
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        _fail(path, "must include a UTC timezone")
    return parsed


def load_json(path: str | Path) -> Any:
    """Load a UTF-8 JSON document and report useful parsing errors."""

    file_path = Path(path)
    try:
        with file_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise ContractError(f"{file_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"{file_path}:{exc.lineno}:{exc.colno}: {exc.msg}") from exc


def canonical_json_bytes(document: Any) -> bytes:
    """Return the canonical byte representation used by all contract hashes."""

    try:
        text = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ContractError(f"document is not canonical JSON: {exc}") from exc
    return text.encode("utf-8")


def document_sha256(document: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


def derive_seed(root_seed: int, stable_id: str) -> int:
    """Derive an iteration-order-independent uint32 seed for an entity."""

    _integer(root_seed, "root_seed", minimum=0, maximum=UINT32_MAX)
    _identifier(stable_id, "stable_id")
    digest = hashlib.sha256(f"{root_seed}:{stable_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def derive_world_seed(prompt: str, references: Sequence[Mapping[str, Any]]) -> int:
    """Derive the internal root seed from the only external inputs.

    Reference paths are intentionally excluded: the image content digest, not a
    machine-local filename, determines generation.
    """

    _string(prompt, "prompt")
    digests: list[str] = []
    for index, reference in enumerate(references):
        obj = _mapping(reference, f"references[{index}]")
        digests.append(_sha256(obj.get("sha256"), f"references[{index}].sha256"))
    payload = {"prompt": prompt, "reference_sha256": digests}
    digest = hashlib.sha256(canonical_json_bytes(payload)).digest()
    return int.from_bytes(digest[:4], "big")


def _validate_transform_entity(obj: Mapping[str, Any], path: str) -> None:
    _identifier(obj["id"], f"{path}.id")
    _identifier(obj["region"], f"{path}.region")
    _relative_path(obj["prefab"], f"{path}.prefab")
    _vec(obj["position"], f"{path}.position", 3)


def validate_world(document: Any) -> Mapping[str, Any]:
    """Validate a WorldSpec and return it unchanged on success."""

    world = _object(
        document,
        "$",
        required=(
            "schema",
            "world_id",
            "seed",
            "brief",
            "style",
            "units",
            "regions",
            "roads",
            "buildings",
            "props",
            "lights",
            "interactions",
            "cameras",
        ),
    )
    if world["schema"] != WORLD_SCHEMA:
        _fail("$.schema", f"expected {WORLD_SCHEMA!r}")
    _identifier(world["world_id"], "$.world_id")
    _integer(world["seed"], "$.seed", minimum=0, maximum=UINT32_MAX)

    brief = _object(world["brief"], "$.brief", required=("text", "references"))
    _string(brief["text"], "$.brief.text")
    references = _list(brief["references"], "$.brief.references")
    for index, value in enumerate(references):
        path = f"$.brief.references[{index}]"
        reference = _object(value, path, required=("path", "sha256"))
        _relative_path(reference["path"], f"{path}.path")
        _sha256(reference["sha256"], f"{path}.sha256")
    expected_seed = derive_world_seed(brief["text"], references)
    if world["seed"] != expected_seed:
        _fail("$.seed", f"expected system-derived seed {expected_seed}")

    style = _object(world["style"], "$.style", required=("theme", "palette", "fog_density"))
    _string(style["theme"], "$.style.theme")
    palette = _object(
        style["palette"],
        "$.style.palette",
        required=("sky", "ground", "primary", "accent", "emissive"),
    )
    for key in ("sky", "ground", "primary", "accent", "emissive"):
        _color(palette[key], f"$.style.palette.{key}")
    _number(style["fog_density"], "$.style.fog_density", minimum=0, maximum=1)

    units = _object(world["units"], "$.units", required=("length", "rotation", "up_axis"))
    if units["length"] != "m":
        _fail("$.units.length", "expected 'm'")
    if units["rotation"] != "deg":
        _fail("$.units.rotation", "expected 'deg'")
    if units["up_axis"] != "Y":
        _fail("$.units.up_axis", "expected 'Y'")

    regions = _list(world["regions"], "$.regions", min_length=1)
    if len(regions) > 12:
        _fail("$.regions", "at most 12 regions are allowed")
    region_ids: set[str] = set()
    all_entity_ids: set[str] = set()
    for index, value in enumerate(regions):
        path = f"$.regions[{index}]"
        region = _object(
            value,
            path,
            required=("id", "kind", "center", "size", "elevation"),
            optional=("generation_seed",),
        )
        region_id = _identifier(region["id"], f"{path}.id")
        if region_id in region_ids:
            _fail(f"{path}.id", f"duplicate region ID {region_id!r}")
        region_ids.add(region_id)
        all_entity_ids.add(region_id)
        _string(region["kind"], f"{path}.kind")
        _vec(region["center"], f"{path}.center", 3)
        _vec(region["size"], f"{path}.size", 3, positive=True)
        _number(region["elevation"], f"{path}.elevation")
        if "generation_seed" in region:
            _integer(
                region["generation_seed"],
                f"{path}.generation_seed",
                minimum=0,
                maximum=UINT32_MAX,
            )

    roads = _list(world["roads"], "$.roads")
    if len(roads) > 32:
        _fail("$.roads", "at most 32 roads are allowed")
    for index, value in enumerate(roads):
        path = f"$.roads[{index}]"
        road = _object(
            value,
            path,
            required=("id", "from", "to", "kind", "width", "waypoints"),
        )
        road_id = _identifier(road["id"], f"{path}.id")
        _register_global_id(road_id, all_entity_ids, f"{path}.id")
        source = _identifier(road["from"], f"{path}.from")
        target = _identifier(road["to"], f"{path}.to")
        if source not in region_ids:
            _fail(f"{path}.from", f"unknown region {source!r}")
        if target not in region_ids:
            _fail(f"{path}.to", f"unknown region {target!r}")
        if source == target:
            _fail(path, "road endpoints must be different regions")
        _string(road["kind"], f"{path}.kind")
        width = _number(road["width"], f"{path}.width")
        if width <= 0:
            _fail(f"{path}.width", "must be > 0")
        waypoints = _list(road["waypoints"], f"{path}.waypoints", min_length=2)
        for waypoint_index, waypoint in enumerate(waypoints):
            _vec(waypoint, f"{path}.waypoints[{waypoint_index}]", 3)

    buildings = _list(world["buildings"], "$.buildings")
    if len(buildings) > 64:
        _fail("$.buildings", "at most 64 buildings are allowed")
    for index, value in enumerate(buildings):
        path = f"$.buildings[{index}]"
        building = _object(
            value,
            path,
            required=("id", "region", "prefab", "position", "rotation_deg", "scale"),
        )
        _validate_transform_entity(building, path)
        _register_global_id(building["id"], all_entity_ids, f"{path}.id")
        if building["region"] not in region_ids:
            _fail(f"{path}.region", f"unknown region {building['region']!r}")
        _vec(building["rotation_deg"], f"{path}.rotation_deg", 3)
        _vec(building["scale"], f"{path}.scale", 3, positive=True)

    props = _list(world["props"], "$.props")
    if len(props) > 128:
        _fail("$.props", "at most 128 props are allowed")
    for index, value in enumerate(props):
        path = f"$.props[{index}]"
        prop = _object(
            value,
            path,
            required=("id", "region", "prefab", "position", "rotation_deg", "scale"),
        )
        _validate_transform_entity(prop, path)
        _register_global_id(prop["id"], all_entity_ids, f"{path}.id")
        if prop["region"] not in region_ids:
            _fail(f"{path}.region", f"unknown region {prop['region']!r}")
        _vec(prop["rotation_deg"], f"{path}.rotation_deg", 3)
        _vec(prop["scale"], f"{path}.scale", 3, positive=True)

    lights = _list(world["lights"], "$.lights")
    if len(lights) > 32:
        _fail("$.lights", "at most 32 lights are allowed")
    for index, value in enumerate(lights):
        path = f"$.lights[{index}]"
        light = _object(
            value,
            path,
            required=(
                "id",
                "kind",
                "position",
                "rotation_deg",
                "color",
                "energy",
                "range",
            ),
        )
        light_id = _identifier(light["id"], f"{path}.id")
        _register_global_id(light_id, all_entity_ids, f"{path}.id")
        _enum(light["kind"], f"{path}.kind", ("directional", "omni", "spot"))
        _vec(light["position"], f"{path}.position", 3)
        _vec(light["rotation_deg"], f"{path}.rotation_deg", 3)
        _color(light["color"], f"{path}.color")
        _number(light["energy"], f"{path}.energy", minimum=0)
        _number(light["range"], f"{path}.range", minimum=0)

    interactions = _object(
        world["interactions"],
        "$.interactions",
        required=("player_spawn", "interactables", "objectives", "exit"),
    )
    player_spawn = _object(
        interactions["player_spawn"],
        "$.interactions.player_spawn",
        required=("region", "position"),
    )
    spawn_region = _identifier(player_spawn["region"], "$.interactions.player_spawn.region")
    if spawn_region not in region_ids:
        _fail("$.interactions.player_spawn.region", f"unknown region {spawn_region!r}")
    _vec(player_spawn["position"], "$.interactions.player_spawn.position", 3)

    interactables = _list(
        interactions["interactables"],
        "$.interactions.interactables",
        min_length=1,
    )
    if len(interactables) > 32:
        _fail("$.interactions.interactables", "at most 32 interactables are allowed")
    interactable_ids: set[str] = set()
    for index, value in enumerate(interactables):
        path = f"$.interactions.interactables[{index}]"
        interactable = _object(
            value,
            path,
            required=(
                "id",
                "region",
                "prefab",
                "position",
                "action",
                "label",
                "duration_ms",
            ),
        )
        _validate_transform_entity(interactable, path)
        interactable_id = interactable["id"]
        _register_global_id(interactable_id, all_entity_ids, f"{path}.id")
        interactable_ids.add(interactable_id)
        if interactable["region"] not in region_ids:
            _fail(f"{path}.region", f"unknown region {interactable['region']!r}")
        _enum(
            interactable["action"],
            f"{path}.action",
            ("collect", "activate", "repair", "inspect"),
        )
        _string(interactable["label"], f"{path}.label")
        _integer(interactable["duration_ms"], f"{path}.duration_ms", minimum=0, maximum=10000)

    objectives = _list(interactions["objectives"], "$.interactions.objectives", min_length=1)
    if len(objectives) > 16:
        _fail("$.interactions.objectives", "at most 16 objectives are allowed")
    objective_ids: set[str] = set()
    for index, value in enumerate(objectives):
        path = f"$.interactions.objectives[{index}]"
        objective = _object(
            value,
            path,
            required=("id", "rule", "targets", "completion_text"),
        )
        objective_id = _identifier(objective["id"], f"{path}.id")
        _register_global_id(objective_id, all_entity_ids, f"{path}.id")
        objective_ids.add(objective_id)
        _enum(objective["rule"], f"{path}.rule", ("all", "any", "sequence"))
        targets = _unique_strings(objective["targets"], f"{path}.targets", min_length=1)
        for target_index, target_id in enumerate(targets):
            if target_id not in interactable_ids:
                _fail(f"{path}.targets[{target_index}]", f"unknown interactable {target_id!r}")
        _string(objective["completion_text"], f"{path}.completion_text")

    exit_path = "$.interactions.exit"
    exit_obj = _object(
        interactions["exit"],
        exit_path,
        required=("id", "region", "position", "requires"),
    )
    exit_id = _identifier(exit_obj["id"], f"{exit_path}.id")
    _register_global_id(exit_id, all_entity_ids, f"{exit_path}.id")
    exit_region = _identifier(exit_obj["region"], f"{exit_path}.region")
    if exit_region not in region_ids:
        _fail(f"{exit_path}.region", f"unknown region {exit_region!r}")
    _vec(exit_obj["position"], f"{exit_path}.position", 3)
    required_objectives = _unique_strings(exit_obj["requires"], f"{exit_path}.requires", min_length=1)
    for index, objective_id in enumerate(required_objectives):
        if objective_id not in objective_ids:
            _fail(f"{exit_path}.requires[{index}]", f"unknown objective {objective_id!r}")

    cameras = _list(world["cameras"], "$.cameras", min_length=1)
    if len(cameras) > 8:
        _fail("$.cameras", "at most 8 cameras are allowed")
    for index, value in enumerate(cameras):
        path = f"$.cameras[{index}]"
        camera = _object(
            value,
            path,
            required=("id", "kind", "position", "look_at", "fov_deg", "resolution"),
        )
        camera_id = _identifier(camera["id"], f"{path}.id")
        _register_global_id(camera_id, all_entity_ids, f"{path}.id")
        _enum(camera["kind"], f"{path}.kind", ("fixed", "player", "orbit"))
        _vec(camera["position"], f"{path}.position", 3)
        _vec(camera["look_at"], f"{path}.look_at", 3)
        _number(camera["fov_deg"], f"{path}.fov_deg", minimum=1, maximum=179)
        resolution = _list(camera["resolution"], f"{path}.resolution")
        if len(resolution) != 2:
            _fail(f"{path}.resolution", "expected [width, height]")
        for component, name in zip(resolution, ("width", "height")):
            _integer(component, f"{path}.resolution.{name}", minimum=64, maximum=8192)

    return world


def validate_evaluation_policy(document: Any) -> Mapping[str, Any]:
    """Validate the fixed, system-owned evaluation and correction policy."""

    policy = _object(
        document,
        "$",
        required=(
            "schema",
            "max_correction_iterations",
            "required_checks",
            "weights",
            "min_score",
            "normalization",
        ),
    )
    if policy["schema"] != EVALUATION_POLICY_SCHEMA:
        _fail("$.schema", f"expected {EVALUATION_POLICY_SCHEMA!r}")
    max_iterations = _integer(
        policy["max_correction_iterations"],
        "$.max_correction_iterations",
        minimum=0,
        maximum=10,
    )
    if max_iterations != 2:
        _fail("$.max_correction_iterations", "the fixed system policy requires 2")
    _unique_strings(policy["required_checks"], "$.required_checks", min_length=1)
    weights = _object(policy["weights"], "$.weights", required=METRIC_KEYS)
    total_weight = 0.0
    for key in METRIC_KEYS:
        total_weight += _number(weights[key], f"$.weights.{key}", minimum=0, maximum=1)
    if not math.isclose(total_weight, 1.0, rel_tol=0.0, abs_tol=1e-6):
        _fail("$.weights", f"weights must sum to 1, got {total_weight}")
    _number(policy["min_score"], "$.min_score", minimum=0, maximum=1)
    normalization = _object(
        policy["normalization"],
        "$.normalization",
        required=("reference_generation_ms", "reference_total_tokens"),
    )
    _integer(
        normalization["reference_generation_ms"],
        "$.normalization.reference_generation_ms",
        minimum=1,
    )
    _integer(
        normalization["reference_total_tokens"],
        "$.normalization.reference_total_tokens",
        minimum=1,
    )
    return policy


def _register_global_id(entity_id: str, known: set[str], path: str) -> None:
    if entity_id in known:
        _fail(path, f"duplicate global entity ID {entity_id!r}")
    known.add(entity_id)


def _validate_patch_shape(document: Any) -> Mapping[str, Any]:
    patch = _object(
        document,
        "$",
        required=(
            "schema",
            "patch_id",
            "base_world_id",
            "base_world_sha256",
            "iteration",
            "reasons",
            "operations",
            "expected_checks",
        ),
    )
    if patch["schema"] != PATCH_SCHEMA:
        _fail("$.schema", f"expected {PATCH_SCHEMA!r}")
    _identifier(patch["patch_id"], "$.patch_id")
    _identifier(patch["base_world_id"], "$.base_world_id")
    _sha256(patch["base_world_sha256"], "$.base_world_sha256")
    _integer(patch["iteration"], "$.iteration", minimum=1, maximum=10)
    reasons = _list(patch["reasons"], "$.reasons", min_length=1)
    for index, reason in enumerate(reasons):
        _string(reason, f"$.reasons[{index}]")
    _unique_strings(patch["expected_checks"], "$.expected_checks")

    operations = _list(patch["operations"], "$.operations", min_length=1)
    for index, value in enumerate(operations):
        path = f"$.operations[{index}]"
        operation = _mapping(value, path)
        op = _enum(operation.get("op"), f"{path}.op", PATCH_OPS)
        if op == "update":
            operation = _object(operation, path, required=("op", "target", "changes"))
        elif op == "upsert":
            operation = _object(operation, path, required=("op", "target", "value"))
        elif op == "remove":
            operation = _object(operation, path, required=("op", "target"))
        else:
            operation = _object(operation, path, required=("op", "target", "seed"))

        target = _object(operation["target"], f"{path}.target", required=("kind", "id"))
        kind = _enum(target["kind"], f"{path}.target.kind", TARGET_KINDS)
        target_id = _identifier(target["id"], f"{path}.target.id")

        if op == "update":
            changes = _mapping(operation["changes"], f"{path}.changes")
            if not changes:
                _fail(f"{path}.changes", "must not be empty")
            forbidden = sorted(set(changes) - PATCHABLE_FIELDS[kind])
            if forbidden:
                _fail(f"{path}.changes", f"fields are not patchable for {kind}: {', '.join(forbidden)}")
        elif op == "upsert":
            replacement = _mapping(operation["value"], f"{path}.value")
            if replacement.get("id") != target_id:
                _fail(f"{path}.value.id", "must equal target.id")
        elif op == "regenerate_region":
            if kind != "region":
                _fail(f"{path}.target.kind", "regenerate_region only accepts a region target")
            _integer(operation["seed"], f"{path}.seed", minimum=0, maximum=UINT32_MAX)
    return patch


def validate_patch(document: Any, world_document: Any) -> Mapping[str, Any]:
    """Validate a PatchSpec against the exact WorldSpec it targets."""

    world = validate_world(world_document)
    patch = _validate_patch_shape(document)
    if patch["base_world_id"] != world["world_id"]:
        _fail("$.base_world_id", "does not match the target world")
    actual_hash = document_sha256(world)
    if patch["base_world_sha256"] != actual_hash:
        _fail("$.base_world_sha256", f"stale patch; expected {actual_hash}")
    candidate = _apply_patch_unchecked(world, patch)
    validate_world(candidate)
    return patch


def _entity_collection(world: dict[str, Any], kind: str) -> tuple[list[dict[str, Any]], str] | None:
    if kind == "region":
        return world["regions"], "regions"
    if kind == "road":
        return world["roads"], "roads"
    if kind == "building":
        return world["buildings"], "buildings"
    if kind == "prop":
        return world["props"], "props"
    if kind == "light":
        return world["lights"], "lights"
    if kind == "interactable":
        return world["interactions"]["interactables"], "interactions.interactables"
    if kind == "objective":
        return world["interactions"]["objectives"], "interactions.objectives"
    if kind == "camera":
        return world["cameras"], "cameras"
    return None


def _find_entity(world: dict[str, Any], kind: str, entity_id: str) -> dict[str, Any] | None:
    if kind == "exit":
        candidate = world["interactions"].get("exit")
        return candidate if candidate and candidate.get("id") == entity_id else None
    collection_info = _entity_collection(world, kind)
    if collection_info is None:
        return None
    collection, _ = collection_info
    return next((entity for entity in collection if entity.get("id") == entity_id), None)


def _apply_patch_unchecked(world_document: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    world = copy.deepcopy(world_document)
    for operation in patch["operations"]:
        op = operation["op"]
        kind = operation["target"]["kind"]
        entity_id = operation["target"]["id"]
        current = _find_entity(world, kind, entity_id)

        if op == "update":
            if current is None:
                _fail("$.operations", f"cannot update missing {kind} {entity_id!r}")
            current.update(copy.deepcopy(operation["changes"]))
        elif op == "regenerate_region":
            if current is None:
                _fail("$.operations", f"cannot regenerate missing region {entity_id!r}")
            current["generation_seed"] = operation["seed"]
        elif kind == "exit":
            if op == "upsert":
                world["interactions"]["exit"] = copy.deepcopy(operation["value"])
            elif op == "remove":
                world["interactions"].pop("exit", None)
        else:
            collection_info = _entity_collection(world, kind)
            if collection_info is None:
                _fail("$.operations", f"unsupported target kind {kind!r}")
            collection, _ = collection_info
            if op == "upsert":
                replacement = copy.deepcopy(operation["value"])
                if current is None:
                    collection.append(replacement)
                else:
                    collection[collection.index(current)] = replacement
            elif op == "remove":
                if current is None:
                    _fail("$.operations", f"cannot remove missing {kind} {entity_id!r}")
                collection.remove(current)
    return world


def apply_patch(world_document: Any, patch_document: Any) -> dict[str, Any]:
    """Validate and immutably apply a PatchSpec, returning a new WorldSpec."""

    world = validate_world(world_document)
    patch = validate_patch(patch_document, world)
    result = _apply_patch_unchecked(world, patch)
    validate_world(result)
    return result


def validate_evaluation(
    document: Any,
    world_document: Any,
    policy_document: Any,
) -> Mapping[str, Any]:
    """Validate and independently recompute an Evaluation report."""

    world = validate_world(world_document)
    policy = validate_evaluation_policy(policy_document)
    report = _object(
        document,
        "$",
        required=(
            "schema",
            "run_id",
            "world_id",
            "world_sha256",
            "iteration",
            "status",
            "started_at_utc",
            "finished_at_utc",
            "timing_ms",
            "tokens",
            "checks",
            "metrics",
            "result",
            "issues",
            "artifacts",
            "next_action",
        ),
    )
    if report["schema"] != EVALUATION_SCHEMA:
        _fail("$.schema", f"expected {EVALUATION_SCHEMA!r}")
    _identifier(report["run_id"], "$.run_id")
    if report["world_id"] != world["world_id"]:
        _fail("$.world_id", "does not match evaluated world")
    expected_world_hash = document_sha256(world)
    _sha256(report["world_sha256"], "$.world_sha256")
    if report["world_sha256"] != expected_world_hash:
        _fail("$.world_sha256", f"expected {expected_world_hash}")
    iteration = _integer(report["iteration"], "$.iteration", minimum=0, maximum=10)
    if iteration > policy["max_correction_iterations"]:
        _fail("$.iteration", "exceeds policy.max_correction_iterations")
    status = _enum(report["status"], "$.status", ("pass", "fail", "error"))
    started = _utc_timestamp(report["started_at_utc"], "$.started_at_utc")
    finished = _utc_timestamp(report["finished_at_utc"], "$.finished_at_utc")
    if finished < started:
        _fail("$.finished_at_utc", "must not precede started_at_utc")

    timing = _object(
        report["timing_ms"],
        "$.timing_ms",
        required=("plan", "build", "import", "capture", "evaluate", "total"),
    )
    for key in ("plan", "build", "import", "capture", "evaluate", "total"):
        _integer(timing[key], f"$.timing_ms.{key}", minimum=0)
    component_total = sum(timing[key] for key in ("plan", "build", "import", "capture", "evaluate"))
    if timing["total"] != component_total:
        _fail("$.timing_ms.total", f"must equal phase sum {component_total}")

    tokens = _object(
        report["tokens"],
        "$.tokens",
        required=("model", "calls", "input", "cached_input", "output", "total"),
    )
    _string(tokens["model"], "$.tokens.model")
    _integer(tokens["calls"], "$.tokens.calls", minimum=0)
    input_tokens = _integer(tokens["input"], "$.tokens.input", minimum=0)
    cached_tokens = _integer(tokens["cached_input"], "$.tokens.cached_input", minimum=0)
    output_tokens = _integer(tokens["output"], "$.tokens.output", minimum=0)
    total_tokens = _integer(tokens["total"], "$.tokens.total", minimum=0)
    if cached_tokens > input_tokens:
        _fail("$.tokens.cached_input", "must be a subset of input tokens")
    if total_tokens != input_tokens + output_tokens:
        _fail("$.tokens.total", "must equal input + output; cached_input is not added again")

    checks = _list(report["checks"], "$.checks", min_length=1)
    check_by_id: dict[str, Mapping[str, Any]] = {}
    all_hard_passed = True
    for index, value in enumerate(checks):
        path = f"$.checks[{index}]"
        check = _object(
            value,
            path,
            required=("id", "kind", "passed", "score", "message", "evidence"),
        )
        check_id = _identifier(check["id"], f"{path}.id")
        if check_id in check_by_id:
            _fail(f"{path}.id", f"duplicate check ID {check_id!r}")
        check_by_id[check_id] = check
        kind = _enum(check["kind"], f"{path}.kind", ("hard", "soft"))
        passed = _boolean(check["passed"], f"{path}.passed")
        _number(check["score"], f"{path}.score", minimum=0, maximum=1)
        _string(check["message"], f"{path}.message")
        evidence = _list(check["evidence"], f"{path}.evidence")
        for evidence_index, evidence_path in enumerate(evidence):
            _relative_path(evidence_path, f"{path}.evidence[{evidence_index}]")
        if kind == "hard" and not passed:
            all_hard_passed = False

    for required_check in policy["required_checks"]:
        check = check_by_id.get(required_check)
        if check is None:
            _fail("$.checks", f"missing required check {required_check!r}")
        if check["kind"] != "hard":
            _fail("$.checks", f"required check {required_check!r} must be hard")

    metrics = _object(report["metrics"], "$.metrics", required=METRIC_KEYS)
    reference_generation_ms = policy["normalization"]["reference_generation_ms"]
    reference_total_tokens = policy["normalization"]["reference_total_tokens"]
    expected_generation_speed = min(1.0, reference_generation_ms / max(timing["total"], 1))
    expected_token_efficiency = min(1.0, reference_total_tokens / max(total_tokens, 1))
    reported_generation_speed = _number(
        metrics["generation_speed"],
        "$.metrics.generation_speed",
        minimum=0,
        maximum=1,
    )
    reported_token_efficiency = _number(
        metrics["token_efficiency"],
        "$.metrics.token_efficiency",
        minimum=0,
        maximum=1,
    )
    if not math.isclose(
        reported_generation_speed,
        expected_generation_speed,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        _fail(
            "$.metrics.generation_speed",
            f"expected normalized score {expected_generation_speed:.6f}",
        )
    if not math.isclose(
        reported_token_efficiency,
        expected_token_efficiency,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        _fail(
            "$.metrics.token_efficiency",
            f"expected normalized score {expected_token_efficiency:.6f}",
        )
    weighted_score = 0.0
    for key in METRIC_KEYS:
        metric = _number(metrics[key], f"$.metrics.{key}", minimum=0, maximum=1)
        weighted_score += metric * float(policy["weights"][key])

    result = _object(
        report["result"],
        "$.result",
        required=("weighted_score", "threshold", "passed"),
    )
    reported_score = _number(result["weighted_score"], "$.result.weighted_score", minimum=0, maximum=1)
    if not math.isclose(reported_score, weighted_score, rel_tol=0.0, abs_tol=1e-6):
        _fail("$.result.weighted_score", f"expected recomputed score {weighted_score:.6f}")
    threshold = _number(result["threshold"], "$.result.threshold", minimum=0, maximum=1)
    expected_threshold = float(policy["min_score"])
    if not math.isclose(threshold, expected_threshold, rel_tol=0.0, abs_tol=1e-6):
        _fail("$.result.threshold", f"expected world threshold {expected_threshold}")

    expected_passed = all_hard_passed and weighted_score >= expected_threshold
    reported_passed = _boolean(result["passed"], "$.result.passed")
    if reported_passed != expected_passed:
        reasons: list[str] = []
        if not all_hard_passed:
            reasons.append("a hard check failed")
        if weighted_score < expected_threshold:
            reasons.append("score is below threshold")
        _fail("$.result.passed", "does not match gates: " + ", ".join(reasons or ["all gates passed"]))
    if (status == "pass") != reported_passed:
        _fail("$.status", "must be 'pass' exactly when result.passed is true")

    issues = _list(report["issues"], "$.issues")
    for index, value in enumerate(issues):
        path = f"$.issues[{index}]"
        issue = _object(
            value,
            path,
            required=("code", "severity", "message"),
            optional=("entity_id", "suggested_op"),
        )
        _identifier(issue["code"], f"{path}.code")
        _enum(issue["severity"], f"{path}.severity", ("info", "minor", "major", "blocker"))
        _string(issue["message"], f"{path}.message")
        if "entity_id" in issue:
            _identifier(issue["entity_id"], f"{path}.entity_id")
        if "suggested_op" in issue:
            _enum(issue["suggested_op"], f"{path}.suggested_op", PATCH_OPS)

    artifacts = _object(
        report["artifacts"],
        "$.artifacts",
        required=("scene", "screenshots", "video"),
    )
    _relative_path(artifacts["scene"], "$.artifacts.scene")
    screenshots = _list(artifacts["screenshots"], "$.artifacts.screenshots", min_length=1)
    for index, screenshot in enumerate(screenshots):
        _relative_path(screenshot, f"$.artifacts.screenshots[{index}]")
    if artifacts["video"] is not None:
        _relative_path(artifacts["video"], "$.artifacts.video")

    next_action = _enum(report["next_action"], "$.next_action", ("accept", "patch", "abort"))
    if reported_passed and next_action != "accept":
        _fail("$.next_action", "a passing report must be accepted")
    if not reported_passed and next_action == "accept":
        _fail("$.next_action", "a failing report cannot be accepted")
    if (
        not reported_passed
        and next_action == "patch"
        and iteration >= policy["max_correction_iterations"]
    ):
        _fail("$.next_action", "correction limit reached; failing report must abort")
    return report


def _write_json(path: str | Path, document: Any) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate Prompt-to-Play JSON contracts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_world_parser = subparsers.add_parser("validate-world", help="validate a WorldSpec")
    validate_world_parser.add_argument("world")

    hash_parser = subparsers.add_parser("hash", help="print a validated WorldSpec SHA-256")
    hash_parser.add_argument("world")

    validate_patch_parser = subparsers.add_parser("validate-patch", help="validate a PatchSpec")
    validate_patch_parser.add_argument("patch")
    validate_patch_parser.add_argument("--world", required=True)

    apply_parser = subparsers.add_parser("apply", help="apply a PatchSpec to a WorldSpec")
    apply_parser.add_argument("world")
    apply_parser.add_argument("patch")
    apply_parser.add_argument("output")

    validate_eval_parser = subparsers.add_parser("validate-eval", help="validate an Evaluation report")
    validate_eval_parser.add_argument("evaluation")
    validate_eval_parser.add_argument("--world", required=True)
    validate_eval_parser.add_argument("--policy", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-world":
            validate_world(load_json(args.world))
            print(f"ok: {args.world}")
        elif args.command == "hash":
            world = load_json(args.world)
            validate_world(world)
            print(document_sha256(world))
        elif args.command == "validate-patch":
            validate_patch(load_json(args.patch), load_json(args.world))
            print(f"ok: {args.patch}")
        elif args.command == "apply":
            resolved = apply_patch(load_json(args.world), load_json(args.patch))
            _write_json(args.output, resolved)
            print(f"ok: {args.output}")
        elif args.command == "validate-eval":
            validate_evaluation(
                load_json(args.evaluation),
                load_json(args.world),
                load_json(args.policy),
            )
            print(f"ok: {args.evaluation}")
        else:  # pragma: no cover - argparse prevents this branch
            parser.error(f"unsupported command {args.command}")
    except ContractError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
