"""Host-owned parallel repair fan-out and deterministic patch merging."""

from __future__ import annotations

import copy
import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import agents, contracts, evaluator, planner


REPAIR_ROUND_SCHEMA = "prompt-to-play/repair-round@1"
REPAIR_MAX_WORKERS_ENV = "PROMPT_TO_PLAY_REPAIR_MAX_WORKERS"
DEFAULT_REPAIR_MAX_WORKERS = 3
STRUCTURAL_CHECK_DOMAINS: Mapping[str, str] = {
    "world_graph_connected": "layout",
    "walkable_collision": "layout",
    "objectives_completable": "gameplay",
    "completion_reachable": "gameplay",
    "evaluation_cameras": "lighting_camera",
}

SUBAGENT_SYSTEM_PROMPT = """
You are one bounded Repair Subagent in a prompt-to-play system. Return one
complete corrected WorldSpec JSON object. Work only on the host-assigned entity
kinds and fields, using the assigned visual issues, structural failures,
reference images, and rendered screenshots. Keep all unowned values
byte-equivalent to the current world. Update existing stable IDs only; do not
add, remove, rename, or reorder entities. Preserve schema, world_id, seed,
brief, style, units, and player_spawn. It is valid to return the current world
unchanged when your assigned domain needs no repair. The host derives and
validates patches, merges non-overlapping writes, and rejects invalid output.
"""


@dataclass(frozen=True)
class RepairTaskOutcome:
    task: agents.AgentTask
    status: str
    duration_ms: int
    base_world_sha256: str
    assigned_issue_codes: tuple[str, ...]
    assigned_check_ids: tuple[str, ...]
    candidate_world_sha256: str | None = None
    patch: Mapping[str, Any] | None = None
    error_type: str | None = None
    rejection_reason: str | None = None


@dataclass(frozen=True)
class RepairRun:
    world: Mapping[str, Any]
    patch: Mapping[str, Any] | None
    record: Mapping[str, Any]


def repair_max_workers(environment: Mapping[str, str] | None = None) -> int:
    env = os.environ if environment is None else environment
    raw = env.get(REPAIR_MAX_WORKERS_ENV, str(DEFAULT_REPAIR_MAX_WORKERS)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{REPAIR_MAX_WORKERS_ENV} must be an integer") from exc
    if not 1 <= value <= agents.MAX_SUBAGENT_CONCURRENCY:
        raise ValueError(
            f"{REPAIR_MAX_WORKERS_ENV} must be between 1 and "
            f"{agents.MAX_SUBAGENT_CONCURRENCY}"
        )
    return value


def _owned_fields(*kinds: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple(
        (kind, tuple(sorted(contracts.PATCHABLE_FIELDS[kind]))) for kind in kinds
    )


def derive_repair_tasks(
    world_document: Any, *, iteration: int
) -> tuple[agents.AgentTask, ...]:
    contracts.validate_world(world_document)
    if isinstance(iteration, bool) or not isinstance(iteration, int):
        raise ValueError("iteration must be an integer")
    if not 1 <= iteration <= 10:
        raise ValueError("iteration must be between 1 and 10")
    prefix = f"repair_{iteration:02d}"
    return (
        agents.AgentTask(
            task_id=f"{prefix}_layout",
            role=agents.AgentRole.REPAIR,
            owned_fields=_owned_fields("region", "road", "building", "prop"),
        ),
        agents.AgentTask(
            task_id=f"{prefix}_gameplay",
            role=agents.AgentRole.REPAIR,
            owned_fields=_owned_fields("interactable", "objective", "exit"),
        ),
        agents.AgentTask(
            task_id=f"{prefix}_lighting_camera",
            role=agents.AgentRole.REPAIR,
            owned_fields=_owned_fields("light", "camera"),
        ),
    )


def _domain(task: agents.AgentTask) -> str:
    return task.task_id.split("_", 2)[2]


def _canonical_equal(first: Any, second: Any) -> bool:
    return contracts.canonical_json_bytes(first) == contracts.canonical_json_bytes(
        second
    )


def _bounded_reason(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    return (message or type(exc).__name__)[:500]


def _safe_rejection_reason(exc: Exception) -> str:
    message = str(exc)
    if isinstance(exc, evaluator.EvaluatorError):
        if message.startswith("RepairAgent provider failed"):
            return "repair provider failed"
        return _bounded_reason(exc)
    if isinstance(exc, contracts.ContractError):
        return _bounded_reason(exc)
    return "repair subagent execution failed"


def _issue_domain(issue: Mapping[str, Any]) -> str | None:
    direct = issue.get("domain")
    if isinstance(direct, str):
        return direct
    guidance = issue.get("repair_guidance")
    if isinstance(guidance, Mapping) and isinstance(guidance.get("domain"), str):
        return guidance["domain"]
    return None


def _assigned_issues(
    feedback: Mapping[str, Any], task: agents.AgentTask
) -> list[Mapping[str, Any]]:
    issues = feedback.get("issues")
    if not isinstance(issues, list):
        return []
    domain = _domain(task)
    assigned = [
        copy.deepcopy(issue)
        for issue in issues
        if isinstance(issue, Mapping)
        and (_issue_domain(issue) in (None, domain))
    ]
    return assigned


def _structural_failures(
    structural_report: Mapping[str, Any] | None,
) -> list[dict[str, str]]:
    if not isinstance(structural_report, Mapping):
        return []
    checks = structural_report.get("checks")
    if not isinstance(checks, list):
        return []
    failures: list[dict[str, str]] = []
    for check in checks:
        if (
            isinstance(check, Mapping)
            and check.get("passed") is False
            and isinstance(check.get("id"), str)
        ):
            failures.append(
                {
                    "id": check["id"],
                    "message": str(check.get("message", ""))[:500],
                }
            )
    return failures


def _assigned_structural_failures(
    failures: Sequence[Mapping[str, str]], task: agents.AgentTask
) -> list[Mapping[str, str]]:
    domain = _domain(task)
    return [
        copy.deepcopy(failure)
        for failure in failures
        if STRUCTURAL_CHECK_DOMAINS.get(failure["id"], domain) == domain
    ]


class _ScopedProvider:
    def __init__(
        self,
        provider: agents.StructuredProvider,
        task: agents.AgentTask,
        assigned_issues: Sequence[Mapping[str, Any]],
        assigned_structural_failures: Sequence[Mapping[str, str]],
    ) -> None:
        self.provider = provider
        self.task = task
        self.assigned_issues = assigned_issues
        self.assigned_structural_failures = assigned_structural_failures

    def generate_json(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        json_schema: Mapping[str, Any],
        schema_name: str,
        image_paths: Sequence[str | Path] = (),
    ) -> Mapping[str, Any]:
        payload = json.loads(messages[-1]["content"])
        payload["agent"] = "RepairSubagent"
        payload["task_id"] = self.task.task_id
        payload["domain"] = _domain(self.task)
        payload["ownership"] = {
            kind: list(fields) for kind, fields in self.task.owned_fields
        }
        payload["patchable_fields"] = copy.deepcopy(payload["ownership"])
        payload["assigned_visual_issues"] = copy.deepcopy(
            list(self.assigned_issues)
        )
        payload["structural_failures"] = copy.deepcopy(
            list(self.assigned_structural_failures)
        )
        scoped_feedback = copy.deepcopy(payload.get("visual_feedback", {}))
        if isinstance(scoped_feedback, dict):
            scoped_feedback["issues"] = copy.deepcopy(list(self.assigned_issues))
            payload["visual_feedback"] = scoped_feedback
        scoped_messages = [
            {"role": "system", "content": SUBAGENT_SYSTEM_PROMPT.strip()},
            {
                "role": "user",
                "content": json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":")
                ),
            },
        ]
        return self.provider.generate_json(
            scoped_messages,
            json_schema=json_schema,
            schema_name="prompt_to_play_repair_subagent_world_spec",
            image_paths=image_paths,
        )


def _scoped_patch(
    task: agents.AgentTask,
    current: Mapping[str, Any],
    candidate: Mapping[str, Any],
    iteration: int,
    assigned_issues: Sequence[Mapping[str, Any]],
    assigned_structural_failures: Sequence[Mapping[str, str]],
) -> dict[str, Any] | None:
    if _canonical_equal(current, candidate):
        return None
    patch = evaluator.diff_to_patch(current, candidate, iteration)
    ownership = {kind: set(fields) for kind, fields in task.owned_fields}
    guidance_targets = {
        (
            change["target_kind"],
            change["target_id"],
            change["field"],
        )
        for issue in assigned_issues
        for change in issue.get("suggested_changes", [])
        if isinstance(change, Mapping)
    }
    writes: set[tuple[str, str, str]] = set()
    for operation in patch["operations"]:
        if operation["op"] != "update":
            raise evaluator.EvaluatorError(
                f"task {task.task_id!r} may only update existing stable IDs"
            )
        target = operation["target"]
        allowed = ownership.get(target["kind"])
        if allowed is None:
            raise evaluator.EvaluatorError(
                f"task {task.task_id!r} does not own {target['kind']!r}"
            )
        forbidden = sorted(set(operation["changes"]) - allowed)
        if forbidden:
            raise evaluator.EvaluatorError(
                f"task {task.task_id!r} does not own fields: "
                + ", ".join(forbidden)
            )
        for field in operation["changes"]:
            write = (target["kind"], target["id"], field)
            if (
                not assigned_structural_failures
                and write not in guidance_targets
                and (target["kind"], None, field) not in guidance_targets
            ):
                raise evaluator.EvaluatorError(
                    f"task {task.task_id!r} write is not backed by assigned "
                    f"guidance: {write!r}"
                )
            if write in writes:
                raise evaluator.EvaluatorError(
                    f"task {task.task_id!r} contains a duplicate field write"
                )
            writes.add(write)
    scoped = copy.deepcopy(patch)
    identity = contracts.document_sha256(
        {
            "task_id": task.task_id,
            "base_world_sha256": scoped["base_world_sha256"],
            "operations": scoped["operations"],
        }
    )
    scoped["patch_id"] = f"{task.task_id}_{identity[:16]}"
    scoped["reasons"] = [f"Apply host-validated {task.task_id} repair."]
    contracts.validate_patch(scoped, current)
    return scoped


def _merged_patch(
    base: Mapping[str, Any],
    operations: Sequence[Mapping[str, Any]],
    iteration: int,
) -> dict[str, Any]:
    identity = contracts.document_sha256(
        {
            "base_world_sha256": contracts.document_sha256(base),
            "operations": list(operations),
            "iteration": iteration,
        }
    )
    return {
        "schema": contracts.PATCH_SCHEMA,
        "patch_id": f"repair_{iteration}_{identity[:16]}",
        "base_world_id": base["world_id"],
        "base_world_sha256": contracts.document_sha256(base),
        "iteration": iteration,
        "reasons": [
            f"Merge non-overlapping Repair Subagent tasks for revision {iteration}."
        ],
        "operations": copy.deepcopy(list(operations)),
        "expected_checks": [
            "world_contract",
            "semantic_connectivity",
            "visual_similarity",
        ],
    }


def _outcome_record(outcome: RepairTaskOutcome) -> dict[str, Any]:
    patch = None if outcome.patch is None else copy.deepcopy(dict(outcome.patch))
    return {
        "task_id": outcome.task.task_id,
        "role": outcome.task.role.value,
        "depends_on": list(outcome.task.depends_on),
        "domain": _domain(outcome.task),
        "ownership": {
            kind: list(fields) for kind, fields in outcome.task.owned_fields
        },
        "assigned_issue_codes": list(outcome.assigned_issue_codes),
        "assigned_check_ids": list(outcome.assigned_check_ids),
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


def repair_world(
    runtime: agents.MultiAgentRuntime,
    prompt: str,
    world_document: Any,
    feedback_document: Any,
    screenshot_image_paths: Sequence[str | Path],
    *,
    iteration: int,
    reference_image_paths: Sequence[str | Path] = (),
    structural_report: Mapping[str, Any] | None = None,
    project_root: str | Path,
    max_workers: int = DEFAULT_REPAIR_MAX_WORKERS,
) -> RepairRun:
    base = copy.deepcopy(contracts.validate_world(world_document))
    base_hash = contracts.document_sha256(base)
    tasks = derive_repair_tasks(base, iteration=iteration)
    structural_failures = _structural_failures(structural_report)

    def execute(task: agents.AgentTask) -> RepairTaskOutcome:
        started = time.perf_counter()
        assigned = _assigned_issues(feedback_document, task)
        assigned_structural = _assigned_structural_failures(
            structural_failures, task
        )
        issue_codes = tuple(
            str(issue["code"]) for issue in assigned if "code" in issue
        )
        check_ids = tuple(failure["id"] for failure in assigned_structural)
        if not assigned and not assigned_structural:
            return RepairTaskOutcome(
                task=task,
                status="skipped",
                duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
                base_world_sha256=base_hash,
                assigned_issue_codes=issue_codes,
                assigned_check_ids=check_ids,
                candidate_world_sha256=base_hash,
            )
        try:
            provider = _ScopedProvider(
                runtime.subagent(task.role, task.task_id),
                task,
                assigned,
                assigned_structural,
            )
            worker = evaluator.RepairAgent(provider, project_root=project_root)
            candidate = worker.revise_world(
                prompt,
                base,
                feedback_document,
                screenshot_image_paths,
                reference_image_paths=reference_image_paths,
                structural_report=structural_report,
            )
            patch = _scoped_patch(
                task,
                base,
                candidate,
                iteration,
                assigned,
                assigned_structural,
            )
            return RepairTaskOutcome(
                task=task,
                status="noop" if patch is None else "candidate",
                duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
                base_world_sha256=base_hash,
                assigned_issue_codes=issue_codes,
                assigned_check_ids=check_ids,
                candidate_world_sha256=contracts.document_sha256(candidate),
                patch=patch,
            )
        except Exception as exc:
            return RepairTaskOutcome(
                task=task,
                status="rejected",
                duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
                base_world_sha256=base_hash,
                assigned_issue_codes=issue_codes,
                assigned_check_ids=check_ids,
                error_type=type(exc).__name__,
                rejection_reason=_safe_rejection_reason(exc),
            )

    outcomes = list(agents.run_task_dag(tasks, execute, max_workers=max_workers))
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
                error_type="RepairConflict",
                rejection_reason=f"overlapping field writes: {overlap!r}"[:500],
            )
            continue
        proposed_operations = [*merged_operations, *outcome.patch["operations"]]
        try:
            proposed_patch = _merged_patch(base, proposed_operations, iteration)
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
    record = {
        "schema": REPAIR_ROUND_SCHEMA,
        "iteration": iteration,
        "max_workers": max_workers,
        "base_world_sha256": base_hash,
        "visual_feedback_sha256": contracts.document_sha256(feedback_document),
        "visual_guidance": copy.deepcopy(
            list(feedback_document.get("issues", []))
            if isinstance(feedback_document, Mapping)
            else []
        ),
        "tasks": [_outcome_record(outcome) for outcome in outcomes],
        "merged_patch": merged_patch,
        "merged_patch_sha256": (
            None
            if merged_patch is None
            else contracts.document_sha256(merged_patch)
        ),
        "result_world_sha256": contracts.document_sha256(merged_world),
        "status": "applied" if merged_patch is not None else "stalled",
    }
    return RepairRun(world=merged_world, patch=merged_patch, record=record)


__all__ = [
    "DEFAULT_REPAIR_MAX_WORKERS",
    "REPAIR_MAX_WORKERS_ENV",
    "REPAIR_ROUND_SCHEMA",
    "RepairRun",
    "RepairTaskOutcome",
    "derive_repair_tasks",
    "repair_max_workers",
    "repair_world",
]
