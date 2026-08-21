"""LLM-backed planning from a prompt/reference summary into a valid WorldSpec."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Protocol, Sequence

try:  # Support package imports and direct local execution.
    from . import contracts
    from .provider import ProviderError, create_provider_from_env
except ImportError:  # pragma: no cover
    import contracts
    from provider import ProviderError, create_provider_from_env


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
COLOR_PATTERN = r"^#[0-9A-F]{6}$"
MAX_CORRECTION_ATTEMPTS = 2


class PlannerError(ValueError):
    """Raised when inputs or provider output cannot produce a valid WorldSpec."""


class _SemanticPreflightError(ValueError):
    """Internal signal for engine/evaluator invariants beyond contract shape."""


class StructuredProvider(Protocol):
    def generate_json(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        json_schema: Mapping[str, Any],
        schema_name: str,
        image_paths: Sequence[str | Path] = (),
    ) -> Mapping[str, Any]: ...


def _object(properties: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(properties),
        "additionalProperties": False,
    }


def _array(
    items: Mapping[str, Any], *, minimum: int = 0, maximum: int | None = None
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "array", "items": items, "minItems": minimum}
    if maximum is not None:
        schema["maxItems"] = maximum
    return schema


ID = {"type": "string", "pattern": ID_PATTERN}
TEXT = {"type": "string", "minLength": 1}
NUMBER = {"type": "number"}
NONNEGATIVE_NUMBER = {"type": "number", "minimum": 0}
VEC3 = _array(NUMBER, minimum=3, maximum=3)
POSITIVE_VEC3 = _array({"type": "number", "exclusiveMinimum": 0}, minimum=3, maximum=3)
COLOR = {"type": "string", "pattern": COLOR_PATTERN}
PATH = {"type": "string", "minLength": 1}


REGION_SCHEMA = _object(
    {
        "id": ID,
        "kind": TEXT,
        "center": VEC3,
        "size": POSITIVE_VEC3,
        "elevation": NUMBER,
    }
)
ROAD_SCHEMA = _object(
    {
        "id": ID,
        "from": ID,
        "to": ID,
        "kind": TEXT,
        "width": {"type": "number", "exclusiveMinimum": 0},
        "waypoints": _array(VEC3, minimum=2),
    }
)
PLACEMENT_SCHEMA = _object(
    {
        "id": ID,
        "region": ID,
        "prefab": PATH,
        "position": VEC3,
        "rotation_deg": VEC3,
        "scale": POSITIVE_VEC3,
    }
)
LIGHT_SCHEMA = _object(
    {
        "id": ID,
        "kind": {"type": "string", "enum": ["directional", "omni", "spot"]},
        "position": VEC3,
        "rotation_deg": VEC3,
        "color": COLOR,
        "energy": NONNEGATIVE_NUMBER,
        "range": NONNEGATIVE_NUMBER,
    }
)
INTERACTABLE_SCHEMA = _object(
    {
        "id": ID,
        "region": ID,
        "prefab": PATH,
        "position": VEC3,
        "action": {
            "type": "string",
            "enum": ["collect", "activate", "repair", "inspect"],
        },
        "label": TEXT,
        "duration_ms": {"type": "integer", "minimum": 0, "maximum": 10000},
    }
)
OBJECTIVE_SCHEMA = _object(
    {
        "id": ID,
        "rule": {"type": "string", "enum": ["all", "any", "sequence"]},
        "targets": _array(ID, minimum=1),
        "completion_text": TEXT,
    }
)
CAMERA_SCHEMA = _object(
    {
        "id": ID,
        "kind": {"type": "string", "enum": ["fixed", "player", "orbit"]},
        "position": VEC3,
        "look_at": VEC3,
        "fov_deg": {"type": "number", "minimum": 1, "maximum": 179},
        "resolution": _array(
            {"type": "integer", "minimum": 64, "maximum": 8192},
            minimum=2,
            maximum=2,
        ),
    }
)


WORLD_SPEC_JSON_SCHEMA = _object(
    {
        "schema": {"type": "string", "const": contracts.WORLD_SCHEMA},
        "world_id": ID,
        "seed": {"type": "integer", "minimum": 0, "maximum": contracts.UINT32_MAX},
        "brief": _object(
            {
                "text": TEXT,
                "references": _array(
                    _object(
                        {
                            "path": PATH,
                            "sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                        }
                    )
                ),
            }
        ),
        "style": _object(
            {
                "theme": TEXT,
                "palette": _object(
                    {
                        "sky": COLOR,
                        "ground": COLOR,
                        "primary": COLOR,
                        "accent": COLOR,
                        "emissive": COLOR,
                    }
                ),
                "fog_density": {"type": "number", "minimum": 0, "maximum": 1},
            }
        ),
        "units": _object(
            {
                "length": {"type": "string", "const": "m"},
                "rotation": {"type": "string", "const": "deg"},
                "up_axis": {"type": "string", "const": "Y"},
            }
        ),
        "regions": _array(REGION_SCHEMA, minimum=1, maximum=12),
        "roads": _array(ROAD_SCHEMA, maximum=32),
        "buildings": _array(PLACEMENT_SCHEMA, maximum=64),
        "props": _array(PLACEMENT_SCHEMA, maximum=128),
        "lights": _array(LIGHT_SCHEMA, maximum=32),
        "interactions": _object(
            {
                "player_spawn": _object({"region": ID, "position": VEC3}),
                "interactables": _array(INTERACTABLE_SCHEMA, minimum=1, maximum=32),
                "objectives": _array(OBJECTIVE_SCHEMA, minimum=1, maximum=16),
                "exit": _object(
                    {
                        "id": ID,
                        "region": ID,
                        "position": VEC3,
                        "requires": _array(ID, minimum=1),
                    }
                ),
            }
        ),
        "cameras": _array(CAMERA_SCHEMA, minimum=1, maximum=8),
    }
)


SYSTEM_PROMPT = """You are the planning component of a prompt-to-play pipeline.
Translate the user's arbitrary scene request and optional reference-image summaries into the supplied WorldSpec JSON Schema.

Rules:
- Plan the setting described by this request; never reuse a canned world, fixed theme, or fixed objective.
- Use metres, degrees, and Y-up coordinates. Keep the scene small enough for an interactive prototype.
- Every entity ID must be globally unique. All region, road, objective, interactable, and exit references must resolve.
- Treat roads as an undirected traversal graph: every region must be reachable from player_spawn.region. Do not leave decorative regions disconnected.
- Include at least one evaluation camera whose kind is fixed or orbit. A player-only camera is insufficient for capture and evaluation.
- An objective targets interactable IDs. exit.requires contains objective IDs, never interactable IDs.
- Use repository-relative prefab paths with forward slashes and no '..'.
- The caller owns schema, brief, and seed; reproduce the supplied authoritative values exactly.
- Do not add evaluation policy, token limits, time limits, prose, or Markdown.
"""


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise PlannerError(f"{path}: expected a string")
    if not value.strip():
        raise PlannerError(f"{path}: must not be empty")
    return value


def _safe_path(value: Any, path: str) -> str:
    text = _string(value, path)
    if "\\" in text or text.startswith(("/", "~")) or WINDOWS_DRIVE_RE.match(text):
        raise PlannerError(f"{path}: must be a normalized repository-relative path")
    parsed = PurePosixPath(text)
    if str(parsed) != text or ".." in parsed.parts or not parsed.parts:
        raise PlannerError(f"{path}: must be a normalized repository-relative path")
    return text


def normalize_references(
    references: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Return contract references and the richer summaries sent to the model."""

    contract_references: list[dict[str, str]] = []
    model_references: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for index, value in enumerate(references):
        if not isinstance(value, Mapping):
            raise PlannerError(f"references[{index}]: expected an object")
        unknown = set(value) - {"path", "name", "sha256", "summary"}
        if unknown:
            raise PlannerError(
                f"references[{index}]: unknown keys: {', '.join(sorted(unknown))}"
            )
        supplied_path = value.get("path")
        supplied_name = value.get("name")
        if (
            supplied_path is not None
            and supplied_name is not None
            and supplied_path != supplied_name
        ):
            raise PlannerError(f"references[{index}]: path and name disagree")
        path = _safe_path(
            supplied_path if supplied_path is not None else supplied_name,
            f"references[{index}].path",
        )
        if path in seen_paths:
            raise PlannerError(f"references[{index}].path: duplicate path {path!r}")
        seen_paths.add(path)
        sha256 = _string(value.get("sha256"), f"references[{index}].sha256")
        if not SHA256_RE.fullmatch(sha256):
            raise PlannerError(
                f"references[{index}].sha256: expected a lowercase SHA-256 digest"
            )
        contract_reference = {"path": path, "sha256": sha256}
        contract_references.append(contract_reference)
        model_reference = dict(contract_reference)
        if "summary" in value:
            model_reference["summary"] = _string(
                value["summary"], f"references[{index}].summary"
            )
        model_references.append(model_reference)
    return contract_references, model_references


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise PlannerError(f"reference image {path}: {exc}") from exc
    return digest.hexdigest()


def _project_reference_path(image: Path, project_root: Path) -> str:
    try:
        return image.relative_to(project_root).as_posix()
    except ValueError:
        return image.name


def _prepare_references(
    references: Sequence[Mapping[str, Any]],
    reference_image_paths: Sequence[str | Path],
    project_root: Path,
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[Path]]:
    contract_references, model_references = normalize_references(references)
    resolved_images: list[Path] = []
    image_digests: list[str] = []
    for index, value in enumerate(reference_image_paths):
        supplied = Path(value)
        image = supplied if supplied.is_absolute() else project_root / supplied
        image = image.resolve()
        if not image.is_file():
            raise PlannerError(
                f"reference_image_paths[{index}]: file does not exist: {supplied}"
            )
        resolved_images.append(image)
        image_digests.append(_sha256_file(image))

    if resolved_images and contract_references:
        if len(resolved_images) != len(contract_references):
            raise PlannerError(
                "references and reference_image_paths must have the same length"
            )
        for index, (digest, reference) in enumerate(
            zip(image_digests, contract_references)
        ):
            if digest != reference["sha256"]:
                raise PlannerError(
                    f"reference_image_paths[{index}]: content hash does not match references[{index}]"
                )
    elif resolved_images:
        seen_paths: set[str] = set()
        for index, (image, digest) in enumerate(zip(resolved_images, image_digests)):
            path = _safe_path(
                _project_reference_path(image, project_root),
                f"reference_image_paths[{index}]",
            )
            if path in seen_paths:
                raise PlannerError(
                    f"reference_image_paths[{index}]: duplicate project path {path!r}"
                )
            seen_paths.add(path)
            reference = {"path": path, "sha256": digest}
            contract_references.append(reference)
            model_references.append(dict(reference))
    return contract_references, model_references, resolved_images


def _request_schema(brief: Mapping[str, Any], seed: int) -> dict[str, Any]:
    schema = copy.deepcopy(WORLD_SPEC_JSON_SCHEMA)
    schema["properties"]["seed"] = {"type": "integer", "const": seed}
    # Some structured-output implementations reject object-valued ``const``.
    # The authoritative brief is included in the prompt and is overwritten by
    # the caller after generation, so retain the fully typed object schema here.
    return schema


def _semantic_preflight(world: Mapping[str, Any]) -> None:
    region_ids = {region["id"] for region in world["regions"]}
    adjacency = {region_id: set() for region_id in region_ids}
    for road in world["roads"]:
        adjacency[road["from"]].add(road["to"])
        adjacency[road["to"]].add(road["from"])
    start = world["interactions"]["player_spawn"]["region"]
    reached = {start}
    pending = [start]
    while pending:
        current = pending.pop()
        for neighbour in adjacency[current] - reached:
            reached.add(neighbour)
            pending.append(neighbour)
    unreachable = sorted(region_ids - reached)
    if unreachable:
        names = ", ".join(repr(region_id) for region_id in unreachable)
        raise _SemanticPreflightError(
            "semantic preflight: roads graph from player_spawn region "
            f"{start!r} does not reach regions: {names}"
        )
    if not any(camera["kind"] in ("fixed", "orbit") for camera in world["cameras"]):
        raise _SemanticPreflightError(
            "semantic preflight: at least one evaluation camera with kind "
            "'fixed' or 'orbit' is required; player-only cameras are insufficient"
        )


def _authoritative_world(
    planned: Mapping[str, Any],
    brief: Mapping[str, Any],
    seed: int,
) -> dict[str, Any]:
    world = dict(planned)
    world["schema"] = contracts.WORLD_SCHEMA
    world["seed"] = seed
    world["brief"] = dict(brief)
    return world


def _correction_messages(
    world: Mapping[str, Any], exact_error: str
) -> list[dict[str, str]]:
    try:
        rejected_json = json.dumps(
            world,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PlannerError(f"provider output is not canonical JSON: {exc}") from exc
    return [
        {"role": "assistant", "content": rejected_json},
        {
            "role": "user",
            "content": (
                "The previous WorldSpec failed validation. Correct the complete JSON object "
                "without changing the authoritative schema, brief, or seed. Do not explain.\n"
                f"Exact validation error:\n{exact_error}"
            ),
        },
    ]


def plan_world(
    prompt: str,
    references: Sequence[Mapping[str, Any]] = (),
    *,
    reference_image_paths: Sequence[str | Path] = (),
    project_root: str | Path | None = None,
    provider: StructuredProvider | None = None,
) -> dict[str, Any]:
    """Plan, validate, and return a WorldSpec for arbitrary semantic input."""

    prompt = _string(prompt, "prompt")
    root = (Path.cwd() if project_root is None else Path(project_root)).resolve()
    if not root.is_dir():
        raise PlannerError(f"project_root: not a directory: {root}")
    contract_references, model_references, resolved_images = _prepare_references(
        references,
        reference_image_paths,
        root,
    )
    brief = {"text": prompt, "references": contract_references}
    seed = contracts.derive_world_seed(prompt, contract_references)
    request_payload = {
        "prompt": prompt,
        "reference_image_summaries": model_references,
        "authoritative_world_fields": {
            "schema": contracts.WORLD_SCHEMA,
            "seed": seed,
            "brief": brief,
        },
    }
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.strip()},
        {
            "role": "user",
            "content": json.dumps(
                request_payload, ensure_ascii=False, separators=(",", ":")
            ),
        },
    ]
    try:
        active_provider = (
            create_provider_from_env(repo=root) if provider is None else provider
        )
    except ProviderError as exc:
        raise PlannerError(f"world planning provider failed: {exc}") from exc
    response_schema = _request_schema(brief, seed)
    for attempt in range(MAX_CORRECTION_ATTEMPTS + 1):
        try:
            planned = active_provider.generate_json(
                messages,
                json_schema=response_schema,
                schema_name="prompt_to_play_world_spec",
                image_paths=resolved_images,
            )
        except ProviderError as exc:
            raise PlannerError(f"world planning provider failed: {exc}") from exc
        except (OSError, RuntimeError) as exc:
            raise PlannerError(f"world planning provider failed: {exc}") from exc
        if not isinstance(planned, Mapping):
            raise PlannerError("provider output: expected a JSON object")

        # These fields are authoritative system state even if a compatible
        # endpoint does not fully enforce JSON Schema const constraints.
        world = _authoritative_world(planned, brief, seed)
        try:
            contracts.validate_world(world)
            _semantic_preflight(world)
        except (contracts.ContractError, _SemanticPreflightError) as exc:
            exact_error = str(exc)
            if attempt >= MAX_CORRECTION_ATTEMPTS:
                raise PlannerError(
                    "provider returned an invalid WorldSpec after "
                    f"{MAX_CORRECTION_ATTEMPTS} correction attempts: {exact_error}"
                ) from exc
            messages.extend(_correction_messages(world, exact_error))
            continue
        return world
    raise AssertionError("unreachable correction loop")  # pragma: no cover
