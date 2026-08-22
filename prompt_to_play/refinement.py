"""Host-owned task fan-out and safe WorldSpec refinement merging."""

from __future__ import annotations

import copy
import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import agents, contracts, evaluator, planner


REFINEMENT_SCHEMA = "prompt-to-play/refinement@1"
REFINEMENT_MAX_WORKERS_ENV = "PROMPT_TO_PLAY_REFINEMENT_MAX_WORKERS"
REFINEMENT_MAX_ITERATIONS_ENV = "PROMPT_TO_PLAY_REFINEMENT_MAX_ITERATIONS"
DEFAULT_REFINEMENT_MAX_WORKERS = 4
DEFAULT_REFINEMENT_MAX_ITERATIONS = 3
HARD_MAX_REFINEMENT_ITERATIONS = 4

SYSTEM_PROMPT = """
You are one bounded WorldSpec refinement subagent. Review only the entity kinds
and fields assigned by the host. Return a complete WorldSpec, keeping all other
values byte-equivalent to the supplied base world. You may update existing
stable IDs only: do not add, remove, rename, or reorder entities. Preserve the
world schema, identity, seed, brief, style, units, and player spawn. The host
will reject stale, unauthorized, nonpatchable, or semantically invalid output.
"""


@dataclass(frozen=True)
class RefinementTaskOutcome:
    task: agents.AgentTask
    status: str
    duration_ms: int
    base_world_sha256: str
    candidate_world_sha256: str | None = None
    patch: Mapping[str, Any] | None = None
    error_type: str | None = None
    rejection_reason: str | None = None


@dataclass(frozen=True)
class RefinementRun:
    world: Mapping[str, Any]
    record: Mapping[str, Any]


def refinement_max_workers(environment: Mapping[str, str] | None = None) -> int:
    env = os.environ if environment is None else environment
    raw = env.get(
        REFINEMENT_MAX_WORKERS_ENV, str(DEFAULT_REFINEMENT_MAX_WORKERS)
    ).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{REFINEMENT_MAX_WORKERS_ENV} must be an integer") from exc
    if not 1 <= value <= agents.MAX_SUBAGENT_CONCURRENCY:
        raise ValueError(
            f"{REFINEMENT_MAX_WORKERS_ENV} must be between 1 and "
            f"{agents.MAX_SUBAGENT_CONCURRENCY}"
        )
    return value


def refinement_max_iterations(environment: Mapping[str, str] | None = None) -> int:
    env = os.environ if environment is None else environment
    raw = env.get(
        REFINEMENT_MAX_ITERATIONS_ENV, str(DEFAULT_REFINEMENT_MAX_ITERATIONS)
    ).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{REFINEMENT_MAX_ITERATIONS_ENV} must be an integer"
        ) from exc
    if not 1 <= value <= HARD_MAX_REFINEMENT_ITERATIONS:
        raise ValueError(
            f"{REFINEMENT_MAX_ITERATIONS_ENV} must be between 1 and "
            f"{HARD_MAX_REFINEMENT_ITERATIONS}"
        )
    return value


def _owned_fields(*kinds: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple(
        (kind, tuple(sorted(contracts.PATCHABLE_FIELDS[kind]))) for kind in kinds
    )


def derive_refinement_tasks(
    world_document: Any, *, iteration: int = 1
) -> tuple[agents.AgentTask, ...]:
    """Derive the fixed, bounded task graph from a validated WorldSpec."""

    contracts.validate_world(world_document)
    if isinstance(iteration, bool) or not isinstance(iteration, int) or iteration < 1:
        raise ValueError("iteration must be a positive integer")
    prefix = f"refine_{iteration:02d}"
    return (
        agents.AgentTask(
            task_id=f"{prefix}_layout",
            role=agents.AgentRole.WORLD_REFINER,
            owned_fields=_owned_fields("region", "road", "building", "prop"),
        ),
        agents.AgentTask(
            task_id=f"{prefix}_gameplay",
            role=agents.AgentRole.WORLD_REFINER,
            owned_fields=_owned_fields("interactable", "objective", "exit"),
        ),
        agents.AgentTask(
            task_id=f"{prefix}_lighting_camera",
            role=agents.AgentRole.WORLD_REFINER,
            owned_fields=_owned_fields("light", "camera"),
        ),
    )


def _canonical_equal(first: Any, second: Any) -> bool:
    return contracts.canonical_json_bytes(first) == contracts.canonical_json_bytes(second)


def _response_schema(current: Mapping[str, Any]) -> dict[str, Any]:
    schema = copy.deepcopy(planner.WORLD_SPEC_JSON_SCHEMA)
    for key in ("schema", "world_id", "seed", "brief", "style", "units"):
        schema["properties"][key] = {"const": copy.deepcopy(current[key])}
    return schema


def _bounded_reason(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    return (message or type(exc).__name__)[:500]


def _safe_worker_rejection_reason(exc: Exception) -> str:
    if isinstance(exc, (evaluator.EvaluatorError, contracts.ContractError, ValueError)):
        return _bounded_reason(exc)
    return "subagent execution failed"


def _validate_candidate(
    task: agents.AgentTask,
    current: Mapping[str, Any],
    response: Any,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not isinstance(response, Mapping):
        raise evaluator.EvaluatorError("refinement provider output: expected an object")
    candidate = copy.deepcopy(dict(response))
    for key in ("schema", "world_id", "seed", "brief", "style", "units"):
        if not _canonical_equal(candidate.get(key), current[key]):
            raise evaluator.EvaluatorError(
                f"refinement changed authoritative WorldSpec field {key!r}"
            )
    if not _canonical_equal(
        candidate.get("interactions", {}).get("player_spawn"),
        current["interactions"]["player_spawn"],
    ):
        raise evaluator.EvaluatorError("refinement changed authoritative player_spawn")
    try:
        contracts.validate_world(candidate)
        semantic_preflight = getattr(planner, "_semantic_preflight", None)
        if callable(semantic_preflight):
            semantic_preflight(candidate)
    except (contracts.ContractError, ValueError) as exc:
        raise evaluator.EvaluatorError(
            f"refinement returned an invalid WorldSpec: {exc}"
        ) from exc
    if _canonical_equal(candidate, current):
        return candidate, None

    iteration = int(task.task_id.split("_", 2)[1])
    patch = evaluator.diff_to_patch(current, candidate, iteration)
    ownership = {kind: set(fields) for kind, fields in task.owned_fields}
    writes: set[tuple[str, str, str]] = set()
    for operation in patch["operations"]:
        if operation["op"] != "update":
            raise evaluator.EvaluatorError(
                f"task {task.task_id!r} may only update existing stable IDs"
            )
        target = operation["target"]
        kind = target["kind"]
        allowed = ownership.get(kind)
        if allowed is None:
            raise evaluator.EvaluatorError(
                f"task {task.task_id!r} does not own {kind!r}"
            )
        forbidden = sorted(set(operation["changes"]) - allowed)
        if forbidden:
            raise evaluator.EvaluatorError(
                f"task {task.task_id!r} does not own fields: {', '.join(forbidden)}"
            )
        for field in operation["changes"]:
            write = (kind, target["id"], field)
            if write in writes:
                raise evaluator.EvaluatorError(
                    f"task {task.task_id!r} contains a duplicate field write"
                )
            writes.add(write)

    patch = copy.deepcopy(patch)
    identity = contracts.document_sha256(
        {
            "task_id": task.task_id,
            "base_world_sha256": patch["base_world_sha256"],
            "operations": patch["operations"],
        }
    )
    patch["patch_id"] = f"{task.task_id}_{identity[:16]}"
    patch["reasons"] = [f"Apply host-validated {task.task_id} refinement."]
    contracts.validate_patch(patch, current)
    return candidate, patch


class WorldRefinementAgent:
    """One isolated model worker restricted to a host-declared task."""

    def __init__(
        self,
        provider: agents.StructuredProvider,
        *,
        project_root: str | Path,
    ) -> None:
        self.provider = provider
        self.project_root = Path(project_root).resolve(strict=True)

    def refine(
        self,
        task: agents.AgentTask,
        prompt: str,
        world_document: Any,
        *,
        reference_image_paths: Sequence[str | Path] = (),
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        current = contracts.validate_world(world_document)
        if prompt != current["brief"]["text"]:
            raise evaluator.EvaluatorError(
                "prompt must equal current WorldSpec brief.text"
            )
        references: list[Path] = []
        for raw_path in reference_image_paths:
            path = Path(raw_path)
            resolved = (
                path if path.is_absolute() else self.project_root / path
            ).resolve(strict=True)
            references.append(resolved)
        payload = {
            "agent": "WorldRefinementAgent",
            "role": task.role.value,
            "task_id": task.task_id,
            "base_world_sha256": contracts.document_sha256(current),
            "original_prompt": prompt,
            "current_world": current,
            "ownership": {
                kind: list(fields) for kind, fields in task.owned_fields
            },
            "reference_image_count": len(references),
        }
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT.strip()},
            {
                "role": "user",
                "content": json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":")
                ),
            },
        ]
        response = self.provider.generate_json(
            messages,
            json_schema=_response_schema(current),
            schema_name="prompt_to_play_refined_world_spec",
            image_paths=references,
        )
        return _validate_candidate(task, current, response)


def _merged_patch(
    base: Mapping[str, Any],
    operations: Sequence[Mapping[str, Any]],
    iteration: int,
) -> dict[str, Any]:
    identity = contracts.document_sha256(
        {
            "base_world_sha256": contracts.document_sha256(base),
            "operations": list(operations),
        }
    )
    return {
        "schema": contracts.PATCH_SCHEMA,
        "patch_id": f"refinement_{iteration}_{identity[:16]}",
        "base_world_id": base["world_id"],
        "base_world_sha256": contracts.document_sha256(base),
        "iteration": iteration,
        "reasons": [
            f"Merge non-overlapping host-owned refinement tasks for round {iteration}."
        ],
        "operations": copy.deepcopy(list(operations)),
        "expected_checks": ["world_contract", "semantic_connectivity"],
    }


def _outcome_record(outcome: RefinementTaskOutcome) -> dict[str, Any]:
    patch = None if outcome.patch is None else copy.deepcopy(dict(outcome.patch))
    return {
        "task_id": outcome.task.task_id,
        "role": outcome.task.role.value,
        "depends_on": list(outcome.task.depends_on),
        "ownership": {
            kind: list(fields) for kind, fields in outcome.task.owned_fields
        },
        "status": outcome.status,
        "duration_ms": outcome.duration_ms,
        "base_world_sha256": outcome.base_world_sha256,
        "candidate_world_sha256": outcome.candidate_world_sha256,
        "patch_sha256": (
            None if patch is None else contracts.document_sha256(patch)
        ),
        "patch": patch,
        "error_type": outcome.error_type,
        "rejection_reason": outcome.rejection_reason,
    }


def refine_world(
    runtime: agents.MultiAgentRuntime,
    prompt: str,
    world_document: Any,
    *,
    reference_image_paths: Sequence[str | Path] = (),
    project_root: str | Path,
    max_workers: int = DEFAULT_REFINEMENT_MAX_WORKERS,
    max_iterations: int = DEFAULT_REFINEMENT_MAX_ITERATIONS,
) -> RefinementRun:
    """Repeat bounded task fan-out until convergence or the Host iteration cap."""

    if not 1 <= max_iterations <= HARD_MAX_REFINEMENT_ITERATIONS:
        raise ValueError(
            f"max_iterations must be between 1 and {HARD_MAX_REFINEMENT_ITERATIONS}"
        )
    planner_world = contracts.validate_world(world_document)
    planner_hash = contracts.document_sha256(planner_world)
    current_world = copy.deepcopy(planner_world)
    rounds: list[dict[str, Any]] = []
    termination_reason = "max_iterations"

    for iteration in range(1, max_iterations + 1):
        base = copy.deepcopy(current_world)
        base_hash = contracts.document_sha256(base)
        tasks = derive_refinement_tasks(base, iteration=iteration)

        def execute(task: agents.AgentTask) -> RefinementTaskOutcome:
            started = time.perf_counter()
            try:
                worker = WorldRefinementAgent(
                    runtime.subagent(task.role, task.task_id),
                    project_root=project_root,
                )
                candidate, patch = worker.refine(
                    task,
                    prompt,
                    base,
                    reference_image_paths=reference_image_paths,
                )
                return RefinementTaskOutcome(
                    task=task,
                    status="noop" if patch is None else "candidate",
                    duration_ms=max(
                        0, round((time.perf_counter() - started) * 1000)
                    ),
                    base_world_sha256=base_hash,
                    candidate_world_sha256=contracts.document_sha256(candidate),
                    patch=patch,
                )
            except Exception as exc:
                return RefinementTaskOutcome(
                    task=task,
                    status="rejected",
                    duration_ms=max(
                        0, round((time.perf_counter() - started) * 1000)
                    ),
                    base_world_sha256=base_hash,
                    error_type=type(exc).__name__,
                    rejection_reason=_safe_worker_rejection_reason(exc),
                )

        outcomes = list(
            agents.run_task_dag(tasks, execute, max_workers=max_workers)
        )
        merged_operations: list[Mapping[str, Any]] = []
        merged_writes: set[tuple[str, str, str]] = set()
        merged_world = copy.deepcopy(base)

        for index, outcome in enumerate(outcomes):
            if outcome.patch is None:
                continue
            task_writes = {
                (operation["target"]["kind"], operation["target"]["id"], field)
                for operation in outcome.patch["operations"]
                for field in operation["changes"]
            }
            overlap = sorted(task_writes & merged_writes)
            if overlap:
                outcomes[index] = replace(
                    outcome,
                    status="merge_rejected",
                    error_type="RefinementConflict",
                    rejection_reason=f"overlapping field writes: {overlap!r}"[:500],
                )
                continue
            proposed_operations = [
                *merged_operations,
                *outcome.patch["operations"],
            ]
            try:
                proposed_patch = _merged_patch(
                    base, proposed_operations, iteration
                )
                proposed_world = contracts.apply_patch(base, proposed_patch)
                semantic_preflight = getattr(planner, "_semantic_preflight", None)
                if callable(semantic_preflight):
                    semantic_preflight(proposed_world)
            except (contracts.ContractError, ValueError) as exc:
                outcomes[index] = replace(
                    outcome,
                    status="merge_rejected",
                    error_type=type(exc).__name__,
                    rejection_reason=_bounded_reason(exc),
                )
                continue
            merged_operations = proposed_operations
            merged_writes.update(task_writes)
            merged_world = proposed_world
            outcomes[index] = replace(outcome, status="applied")

        merged_patch = (
            _merged_patch(base, merged_operations, iteration)
            if merged_operations
            else None
        )
        rounds.append(
            {
                "iteration": iteration,
                "base_world_sha256": base_hash,
                "tasks": [_outcome_record(outcome) for outcome in outcomes],
                "merged_patch": merged_patch,
                "merged_patch_sha256": (
                    None
                    if merged_patch is None
                    else contracts.document_sha256(merged_patch)
                ),
                "result_world_sha256": contracts.document_sha256(merged_world),
            }
        )
        current_world = merged_world
        if not merged_operations:
            termination_reason = (
                "converged"
                if all(outcome.status == "noop" for outcome in outcomes)
                else "stalled"
            )
            break

    record = {
        "schema": REFINEMENT_SCHEMA,
        "planner": {"iteration": 0, "world_sha256": planner_hash},
        "max_workers": max_workers,
        "max_iterations": max_iterations,
        "termination_reason": termination_reason,
        "rounds": rounds,
        "result_world_sha256": contracts.document_sha256(current_world),
    }
    return RefinementRun(world=current_world, record=record)


__all__ = [
    "DEFAULT_REFINEMENT_MAX_WORKERS",
    "DEFAULT_REFINEMENT_MAX_ITERATIONS",
    "HARD_MAX_REFINEMENT_ITERATIONS",
    "REFINEMENT_MAX_ITERATIONS_ENV",
    "REFINEMENT_MAX_WORKERS_ENV",
    "REFINEMENT_SCHEMA",
    "RefinementRun",
    "RefinementTaskOutcome",
    "WorldRefinementAgent",
    "derive_refinement_tasks",
    "refine_world",
    "refinement_max_iterations",
    "refinement_max_workers",
]
