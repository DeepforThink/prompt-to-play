"""Concrete end-to-end stages for the Prompt-to-Play Windows launcher.

The launcher owns UI and threading; this module wires its five commands to the
planner, contracts, publisher, and Godot toolchain.  The only semantic inputs
remain the prompt and optional reference images supplied by ``LaunchRequest``.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Protocol, Sequence

try:  # Support package imports and direct local execution.
    from . import agents, assets, contracts, evaluator, lifecycle, planner, refinement
    from .launcher import LogSink, PipelineCommands, PipelineState, run_launcher
except ImportError:  # pragma: no cover - direct script execution only
    import agents
    import assets
    import contracts
    import evaluator
    import lifecycle
    import planner
    import refinement
    from launcher import LogSink, PipelineCommands, PipelineState, run_launcher


SOURCE_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMMAND_TIMEOUT_SECONDS = 240.0
REFERENCE_DIRECTORY = PurePosixPath("references")
REFERENCE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})

REQUEST_RESULT = "request"
REFERENCES_RESULT = "reference_bindings"
WORLD_RESULT = "world_spec"
PROJECT_RESULT = "project_dir"
TOOLCHAIN_RESULT = "toolchain"
STRUCTURAL_REPORT_RESULT = "structural_report"
PROCESS_RESULT = "godot_process"
AGENT_RUNTIME_RESULT = "multi_agent_runtime"
ASSET_RESULTS_RESULT = "asset_agent_results"
REFINEMENT_RESULT = "refinement"
EVALUATIONS_RESULT = "evaluations"
SELECTED_REVISION_RESULT = "selected_revision"
TIMING_RESULT = "timing_ms"


class PipelineIntegrationError(RuntimeError):
    """Raised when an external stage cannot satisfy the pipeline contract."""


@dataclass(frozen=True)
class ReferenceBinding:
    source: Path
    project_path: str
    sha256: str

    def contract_reference(self) -> dict[str, str]:
        return {"path": self.project_path, "sha256": self.sha256}


@dataclass(frozen=True)
class Toolchain:
    dotnet_exe: Path
    godot_exe: Path
    godot_nupkgs: Path
    godot_visible_exe: Path | None = None

    @property
    def visible_exe(self) -> Path:
        return (
            self.godot_exe if self.godot_visible_exe is None else self.godot_visible_exe
        )


class PlanWorld(Protocol):
    def __call__(
        self,
        prompt: str,
        references: Sequence[Mapping[str, Any]],
        *,
        reference_image_paths: Sequence[str | Path],
        project_root: str | Path | None,
    ) -> Mapping[str, Any]: ...


class PublishProject(Protocol):
    def __call__(self, target: Path) -> Path: ...


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        environment: Mapping[str, str],
        log: LogSink,
    ) -> None: ...


class ProcessStarter(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        environment: Mapping[str, str],
        log_path: Path,
    ) -> Any: ...


class AgentRuntimeFactory(Protocol):
    def __call__(
        self,
        repo: Path,
        environment: Mapping[str, str],
    ) -> agents.MultiAgentRuntime: ...


@dataclass
class PipelineDependencies:
    """Replaceable effects for tests or an alternate embedding host."""

    source_repo_root: Path = SOURCE_REPO_ROOT
    output_root: Path | None = None
    environment: Mapping[str, str] | None = None
    plan_world: PlanWorld | None = None
    validate_world: Callable[[Any], Mapping[str, Any]] | None = None
    publish_project: PublishProject | None = None
    run_command: CommandRunner | None = None
    start_process: ProcessStarter | None = None
    agent_runtime_factory: AgentRuntimeFactory | None = None
    orchestrate_assets: Callable[..., assets.AssetAgentResult] | None = None
    refine_world: Callable[..., refinement.RefinementRun] | None = None
    launch_probe_seconds: float = 1.25
    which: Callable[[str], str | None] = shutil.which


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_references(
    sources: Sequence[Path], request_document: Mapping[str, Any]
) -> tuple[ReferenceBinding, ...]:
    descriptors = request_document.get("references")
    if not isinstance(descriptors, list) or len(descriptors) != len(sources):
        raise PipelineIntegrationError(
            "request reference descriptors do not match input images"
        )

    bindings: list[ReferenceBinding] = []
    for index, (source, descriptor) in enumerate(
        zip(sources, descriptors, strict=True)
    ):
        if not isinstance(descriptor, Mapping) or not isinstance(
            descriptor.get("sha256"), str
        ):
            raise PipelineIntegrationError(f"request reference {index} has no SHA-256")
        suffix = source.suffix.lower()
        if suffix not in REFERENCE_SUFFIXES:
            raise PipelineIntegrationError(
                f"unsupported reference image type {source.suffix!r}: {source}"
            )
        sha256 = descriptor["sha256"]
        relative = REFERENCE_DIRECTORY / f"{index:02d}_{sha256[:16]}{suffix}"
        bindings.append(
            ReferenceBinding(
                source=source.resolve(strict=True),
                project_path=relative.as_posix(),
                sha256=sha256,
            )
        )
    return tuple(bindings)


def _default_agent_runtime_factory(
    repo: Path,
    environment: Mapping[str, str],
) -> agents.MultiAgentRuntime:
    return agents.MultiAgentRuntime(repo, environment=environment)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _timings(state: PipelineState) -> dict[str, int]:
    value = state.results.get(TIMING_RESULT)
    if isinstance(value, dict):
        return value
    created = {"plan": 0, "build": 0, "import": 0, "capture": 0, "evaluate": 0}
    state.set_result(TIMING_RESULT, created)
    return created


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineIntegrationError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineIntegrationError(f"{label} must contain a JSON object: {path}")
    return value


def _prior_world_hashes(project_dir: Path, request_hash: str) -> frozenset[str]:
    """Read only validated, selected sibling runs for this exact request."""

    hashes: set[str] = set()
    request_root = project_dir.parent
    if not request_root.is_dir() or re.fullmatch(r"[0-9a-f]{64}", request_hash) is None:
        return frozenset()
    run_id = request_hash[:12]
    try:
        candidates = tuple(request_root.iterdir())
    except OSError:
        return frozenset()
    for candidate in candidates:
        if (
            candidate == project_dir
            or not candidate.is_dir()
            or re.fullmatch(r"run-[0-9a-f]{12}", candidate.name) is None
        ):
            continue
        run_root = candidate / "artifacts" / "runs" / run_id
        request_path = run_root / "request.json"
        selection_path = run_root / "selection.json"
        world_path = candidate / "spec" / "world.json"
        if not all(path.is_file() for path in (request_path, selection_path, world_path)):
            continue
        try:
            prior_request = json.loads(request_path.read_text(encoding="utf-8"))
            selection = json.loads(selection_path.read_text(encoding="utf-8"))
            world = json.loads(world_path.read_text(encoding="utf-8"))
            if not all(
                isinstance(value, Mapping)
                for value in (prior_request, selection, world)
            ):
                continue
            if (
                prior_request.get("schema") != lifecycle.REQUEST_SCHEMA
                or prior_request.get("request_hash") != request_hash
                or selection.get("schema") != lifecycle.SELECTION_SCHEMA
                or selection.get("run_id") != run_id
            ):
                continue
            validated_world = contracts.validate_world(world)
            request_references = prior_request.get("references")
            if not isinstance(request_references, list):
                continue
            request_reference_hashes = [
                reference.get("sha256")
                for reference in request_references
                if isinstance(reference, Mapping)
            ]
            world_reference_hashes = [
                reference.get("sha256")
                for reference in validated_world["brief"]["references"]
            ]
            if (
                len(request_reference_hashes) != len(request_references)
                or validated_world["brief"]["text"] != prior_request.get("prompt")
                or world_reference_hashes != request_reference_hashes
            ):
                continue
            world_sha256 = contracts.document_sha256(validated_world)
            selected = selection.get("selected")
            if (
                not isinstance(selected, Mapping)
                or selected.get("world_sha256") != world_sha256
                or not isinstance(selected.get("source"), str)
                or not isinstance(selected.get("evaluation_sha256"), str)
            ):
                continue
            evaluation_relative = PurePosixPath(selected["source"])
            if evaluation_relative.is_absolute() or ".." in evaluation_relative.parts:
                continue
            evaluation_path = run_root.joinpath(*evaluation_relative.parts)
            if not evaluation_path.is_file():
                continue
            evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
            if (
                not isinstance(evaluation, Mapping)
                or evaluation.get("world_sha256") != world_sha256
                or contracts.document_sha256(evaluation)
                != selected["evaluation_sha256"]
            ):
                continue
        except (
            OSError,
            json.JSONDecodeError,
            contracts.ContractError,
            KeyError,
            TypeError,
            ValueError,
        ):
            continue
        hashes.add(world_sha256)
    return frozenset(hashes)


def _build_evaluation_report(
    *,
    run_id: str,
    iteration: int,
    world: Mapping[str, Any],
    structural_report: Mapping[str, Any],
    visual_feedback: Mapping[str, Any],
    screenshot_paths: Sequence[str],
    timing_ms: Mapping[str, int],
    usage: Any,
    policy: Mapping[str, Any],
    prior_world_hashes: frozenset[str],
    started_at_utc: str,
    finished_at_utc: str,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    raw_checks = structural_report.get("checks")
    if not isinstance(raw_checks, list):
        raise PipelineIntegrationError("structural report has no checks array")
    for index, raw_check in enumerate(raw_checks):
        if not isinstance(raw_check, Mapping):
            raise PipelineIntegrationError(
                f"structural report check {index} is not an object"
            )
        checks.append(
            {
                "id": raw_check.get("id"),
                "kind": raw_check.get("kind", "hard"),
                "passed": raw_check.get("passed"),
                "score": raw_check.get("score"),
                "message": raw_check.get("message"),
                "evidence": list(raw_check.get("evidence", [])),
            }
        )

    visual_passed = bool(visual_feedback["accepted"])
    similarity = float(visual_feedback["scene_similarity"])
    checks.append(
        {
            "id": "visual_prompt_gate",
            "kind": "hard",
            "passed": visual_passed,
            "score": similarity,
            "message": (
                "Rendered evaluation views satisfy the prompt similarity gate."
                if visual_passed
                else "Rendered evaluation views require another visual correction."
            ),
            "evidence": list(screenshot_paths),
        }
    )

    world_sha256 = contracts.document_sha256(world)
    source_hash_matches = (
        structural_report.get("world_source_sha256") == world_sha256
    )
    if prior_world_hashes:
        reproducibility_score = 1.0 if world_sha256 in prior_world_hashes else 0.0
        reproducibility_message = (
            "Current WorldSpec exactly matches a prior run for this request."
            if reproducibility_score == 1.0
            else "Current WorldSpec differs from prior runs for this request."
        )
    else:
        reproducibility_score = 0.5 if source_hash_matches else 0.0
        reproducibility_message = (
            "First-run evidence confirms Godot consumed the exact hashed WorldSpec; "
            "cross-run equality is not yet available."
            if reproducibility_score == 0.5
            else "No verified replay or exact Godot source-hash evidence is available."
        )
    checks.append(
        {
            "id": "reproducibility_evidence",
            "kind": "soft",
            "passed": reproducibility_score >= 0.5,
            "score": reproducibility_score,
            "message": reproducibility_message,
            "evidence": [],
        }
    )

    timing = {
        key: max(0, int(timing_ms.get(key, 0)))
        for key in ("plan", "build", "import", "capture", "evaluate")
    }
    timing["total"] = sum(timing.values())
    total_tokens = max(0, int(getattr(usage, "total_tokens", 0)))
    reference_generation_ms = policy["normalization"]["reference_generation_ms"]
    reference_total_tokens = policy["normalization"]["reference_total_tokens"]
    structural_scores = [
        float(check["score"])
        for check in checks
        if check["id"] != "visual_prompt_gate" and check["kind"] == "hard"
    ]
    structural_correctness = (
        sum(structural_scores) / len(structural_scores)
        if structural_scores
        else 0.0
    )
    metrics = {
        "scene_similarity": similarity,
        "structural_correctness": structural_correctness,
        "automation_loop": 1.0 if raw_checks and screenshot_paths else 0.0,
        "generation_speed": min(
            1.0, float(reference_generation_ms) / max(timing["total"], 1)
        ),
        "token_efficiency": min(
            1.0, float(reference_total_tokens) / max(total_tokens, 1)
        ),
        "reproducibility": reproducibility_score,
    }
    weighted_score = sum(
        metrics[key] * float(policy["weights"][key])
        for key in contracts.METRIC_KEYS
    )
    all_hard_passed = all(
        bool(check["passed"]) for check in checks if check["kind"] == "hard"
    )
    passed = all_hard_passed and weighted_score >= float(policy["min_score"])
    max_iterations = int(policy["max_correction_iterations"])
    if passed:
        next_action = "accept"
    elif iteration >= max_iterations:
        next_action = "abort"
    elif not all_hard_passed:
        # Scene/structure failures can be repaired. A score-only miss caused by
        # elapsed time or token use cannot be fixed by mutating the world after
        # those resources were already consumed, so do not perform a fake loop.
        next_action = "patch"
    else:
        next_action = "abort"
    issues: list[dict[str, Any]] = []
    for issue in visual_feedback["issues"]:
        converted = {
            "code": issue["code"],
            "severity": issue["severity"],
            "message": issue["message"],
        }
        if issue.get("entity_id") is not None:
            converted["entity_id"] = issue["entity_id"]
            converted["suggested_op"] = "update"
        issues.append(converted)

    report = {
        "schema": contracts.EVALUATION_SCHEMA,
        "run_id": run_id,
        "world_id": world["world_id"],
        "world_sha256": world_sha256,
        "iteration": iteration,
        "status": "pass" if passed else "fail",
        "started_at_utc": started_at_utc,
        "finished_at_utc": finished_at_utc,
        "timing_ms": timing,
        "tokens": {
            "model": str(getattr(usage, "model", "unreported")) or "unreported",
            "calls": max(0, int(getattr(usage, "calls", 0))),
            "input": max(0, int(getattr(usage, "input_tokens", 0))),
            "cached_input": max(
                0, int(getattr(usage, "cached_input_tokens", 0))
            ),
            "output": max(0, int(getattr(usage, "output_tokens", 0))),
            "total": total_tokens,
        },
        "checks": checks,
        "metrics": metrics,
        "result": {
            "weighted_score": weighted_score,
            "threshold": float(policy["min_score"]),
            "passed": passed,
        },
        "issues": issues,
        "artifacts": {
            "scene": "scenes/Main.tscn",
            "screenshots": list(screenshot_paths),
            "video": None,
        },
        "next_action": next_action,
    }
    contracts.validate_evaluation(report, world, policy)
    return report


def _require_passing_selection(
    evaluation: Mapping[str, Any],
    revision: int,
    selection_path: Path,
) -> None:
    result = evaluation.get("result")
    if not isinstance(result, Mapping) or result.get("passed") is not True:
        raise PipelineIntegrationError(
            "no revision passed all evaluation gates; refusing to launch the "
            f"best failing revision {revision}; selection: {selection_path}"
        )


def _load_source_publisher() -> Any:
    """Load the repository publisher by its anchored path, never by CWD name lookup."""

    publish_path = SOURCE_REPO_ROOT / "publish.py"
    module_name = "prompt_to_play_source_publisher"
    module_spec = importlib.util.spec_from_file_location(module_name, publish_path)
    if module_spec is None or module_spec.loader is None:
        raise PipelineIntegrationError(
            f"cannot load the project publisher: {publish_path}"
        )
    source_publisher = importlib.util.module_from_spec(module_spec)
    sys.modules[module_name] = source_publisher
    module_spec.loader.exec_module(source_publisher)
    return source_publisher


def _default_publish_project(target: Path) -> Path:
    # publish.py intentionally remains the source-repository entry point rather
    # than becoming part of the prompt_to_play package.
    source_publisher = _load_source_publisher()

    config = source_publisher.PublishConfig(
        engine="godot",
        agent="codex",
        workflow="prompt-to-play",
        target=target,
        force=True,
    )
    return source_publisher.publish(config)


def _default_run_command(
    argv: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    log: LogSink,
) -> None:
    command = [os.fspath(value) for value in argv]
    log("运行：" + " ".join(command))
    try:
        completed = subprocess.run(
            command,
            cwd=os.fspath(cwd),
            env=dict(environment),
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DEFAULT_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PipelineIntegrationError(
            f"command timed out after {DEFAULT_COMMAND_TIMEOUT_SECONDS:g}s: {command[0]}"
        ) from exc
    if completed.stdout and completed.stdout.strip():
        log(completed.stdout.rstrip())
    if completed.returncode != 0:
        raise PipelineIntegrationError(
            f"command exited with {completed.returncode}: {command[0]}"
        )


def _default_start_process(
    argv: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    log_path: Path,
) -> subprocess.Popen[bytes]:
    command = [os.fspath(value) for value in argv]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    creation_flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    )
    with log_path.open("ab", buffering=0) as log_handle:
        return subprocess.Popen(
            command,
            cwd=os.fspath(cwd),
            env=dict(environment),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
        )


def _read_log_tail(path: Path, maximum_bytes: int = 32_768) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - maximum_bytes))
            return handle.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def _probe_process(process: Any, timeout_seconds: float) -> int | None:
    """Return an early exit code, or None when the game remains alive."""

    wait = getattr(process, "wait", None)
    if callable(wait) and timeout_seconds > 0:
        try:
            return wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            return None
    poll = getattr(process, "poll", None)
    return poll() if callable(poll) else None


def _resolve_configured_executable(
    variable: str,
    environment: Mapping[str, str],
    which: Callable[[str], str | None],
) -> Path | None:
    configured = environment.get(variable, "").strip()
    if not configured:
        return None
    path = Path(configured).expanduser()
    if path.is_file():
        return path.resolve()
    located = which(configured)
    if located:
        return Path(located).resolve()
    raise PipelineIntegrationError(
        f"{variable} does not identify an executable: {configured}"
    )


def _child_process_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Pass only OS/runtime values needed by build tools and the generated game."""

    allowed = {
        "appdata",
        "comspec",
        "home",
        "homedrive",
        "homepath",
        "lang",
        "localappdata",
        "number_of_processors",
        "os",
        "path",
        "pathext",
        "processor_architecture",
        "programdata",
        "programfiles",
        "programfiles(x86)",
        "systemdrive",
        "systemroot",
        "temp",
        "tmp",
        "userprofile",
        "windir",
    }
    return {
        key: value
        for key, value in environment.items()
        if key.casefold() in allowed and isinstance(value, str)
    }


def _tool_roots(source_repo_root: Path) -> tuple[Path, ...]:
    candidates = (
        source_repo_root / ".tools",
        source_repo_root.parent / ".tools",
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        key = os.path.normcase(os.fspath(resolved))
        if key not in seen and resolved.is_dir():
            unique.append(resolved)
            seen.add(key)
    return tuple(unique)


def _find_in_tools(roots: Sequence[Path], patterns: Sequence[str]) -> Path | None:
    for pattern in patterns:
        matches: list[Path] = []
        for root in roots:
            matches.extend(path for path in root.rglob(pattern) if path.is_file())
        if matches:
            return sorted(
                matches,
                key=lambda path: (len(path.parts), os.path.normcase(os.fspath(path))),
            )[0].resolve()
    return None


def _find_nupkgs(godot_exe: Path) -> Path | None:
    adjacent = godot_exe.parent / "GodotSharp" / "Tools" / "nupkgs"
    packages = (
        sorted(adjacent.glob("Godot.NET.Sdk*.nupkg")) if adjacent.is_dir() else []
    )
    if not packages:
        return None

    version_match = re.search(r"(?<!\d)(\d+\.\d+(?:\.\d+)?)(?!\d)", godot_exe.name)
    if version_match is not None:
        version = version_match.group(1)
        if not any(f"Godot.NET.Sdk.{version}" in package.name for package in packages):
            return None
    return adjacent.resolve()


def _godot_executable_pair(discovered: Path) -> tuple[Path, Path]:
    """Return diagnostic console executable and clean visible executable."""

    marker = "_console"
    if marker in discovered.stem.lower():
        marker_index = discovered.stem.lower().rfind(marker)
        regular_stem = (
            discovered.stem[:marker_index]
            + discovered.stem[marker_index + len(marker) :]
        )
        regular = discovered.with_name(regular_stem + discovered.suffix)
        return discovered, regular.resolve() if regular.is_file() else discovered

    console = discovered.with_name(discovered.stem + marker + discovered.suffix)
    return (console.resolve() if console.is_file() else discovered, discovered)


def discover_toolchain(
    source_repo_root: str | Path = SOURCE_REPO_ROOT,
    *,
    environment: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> Toolchain:
    """Resolve explicit tool paths, workspace-local tools, then PATH."""

    repo_root = Path(source_repo_root).resolve(strict=False)
    env = os.environ if environment is None else environment
    roots = _tool_roots(repo_root)

    dotnet = _resolve_configured_executable("PTP_DOTNET_EXE", env, which)
    if dotnet is None:
        dotnet = _find_in_tools(roots, ("dotnet.exe", "dotnet"))
    if dotnet is None:
        located = which("dotnet")
        dotnet = Path(located).resolve() if located else None
    if dotnet is None:
        raise PipelineIntegrationError(
            "dotnet was not found; set PTP_DOTNET_EXE or install it under workspace .tools"
        )

    godot = _resolve_configured_executable("PTP_GODOT_EXE", env, which)
    if godot is None:
        godot = _find_in_tools(
            roots,
            ("Godot*_console.exe", "Godot*.exe", "godot.exe", "godot"),
        )
    if godot is None:
        located = which("godot")
        godot = Path(located).resolve() if located else None
    if godot is None:
        raise PipelineIntegrationError(
            "Godot .NET was not found; set PTP_GODOT_EXE or install it under workspace .tools"
        )

    godot_console, godot_visible = _godot_executable_pair(godot)
    nupkgs = _find_nupkgs(godot_console)
    if nupkgs is None:
        raise PipelineIntegrationError(
            "a matching Godot.NET.Sdk NuGet feed was not found beside the selected Godot executable"
        )
    return Toolchain(
        dotnet_exe=dotnet,
        godot_exe=godot_console,
        godot_nupkgs=nupkgs,
        godot_visible_exe=godot_visible,
    )


class PipelineStages:
    """Concrete implementations of the launcher's five stage callbacks."""

    def __init__(self, dependencies: PipelineDependencies | None = None) -> None:
        dependencies = PipelineDependencies() if dependencies is None else dependencies
        self.source_repo_root = dependencies.source_repo_root.resolve(strict=False)
        self.output_root = (
            self.source_repo_root.parent / "output" / "generated"
            if dependencies.output_root is None
            else dependencies.output_root.resolve(strict=False)
        )
        self.environment = dict(
            os.environ if dependencies.environment is None else dependencies.environment
        )
        self._uses_default_planner = dependencies.plan_world is None
        self.plan_world = (
            planner.plan_world
            if dependencies.plan_world is None
            else dependencies.plan_world
        )
        self.validate_world = (
            contracts.validate_world
            if dependencies.validate_world is None
            else dependencies.validate_world
        )
        self.publish_project = (
            _default_publish_project
            if dependencies.publish_project is None
            else dependencies.publish_project
        )
        self.run_command = (
            _default_run_command
            if dependencies.run_command is None
            else dependencies.run_command
        )
        self.start_process = (
            _default_start_process
            if dependencies.start_process is None
            else dependencies.start_process
        )
        self.agent_runtime_factory = (
            _default_agent_runtime_factory
            if dependencies.agent_runtime_factory is None
            else dependencies.agent_runtime_factory
        )
        self.orchestrate_assets = (
            assets.orchestrate_assets
            if dependencies.orchestrate_assets is None
            else dependencies.orchestrate_assets
        )
        self.refine_world = (
            refinement.refine_world
            if dependencies.refine_world is None
            else dependencies.refine_world
        )
        self.launch_probe_seconds = max(0.0, float(dependencies.launch_probe_seconds))
        self.which = dependencies.which

    def commands(self) -> PipelineCommands:
        return PipelineCommands(
            plan=self.plan,
            validate=self.validate,
            publish=self.publish,
            build=self.build,
            launch=self.launch,
        )

    def plan(self, state: PipelineState, log: LogSink) -> None:
        started = time.perf_counter()
        request = lifecycle.build_request(
            state.request.prompt,
            state.request.reference_images,
            base_dir=self.source_repo_root.parent,
        )
        bindings = _stable_references(state.request.reference_images, request)
        request_root = self.output_root / request["request_hash"][:12]
        project_dir = request_root / f"run-{uuid.uuid4().hex[:12]}"
        references = [binding.contract_reference() for binding in bindings]
        log(f"请求哈希：{request['request_hash']}")
        log(f"内部派生 seed：{request['seed']}")
        if self._uses_default_planner:
            runtime = self.agent_runtime_factory(
                self.source_repo_root,
                self.environment,
            )
            state.set_result(AGENT_RUNTIME_RESULT, runtime)
            log("WorldPlannerAgent：通过结构化 API 规划 WorldSpec")
            world = self.plan_world(
                state.request.prompt,
                references,
                reference_image_paths=state.request.reference_images,
                project_root=self.source_repo_root,
                provider=runtime.agent(agents.AgentRole.WORLD_PLANNER),
            )
            log("Host Orchestrator：分发 layout、gameplay、lighting/camera Subagent")
            try:
                refinement_run = self.refine_world(
                    runtime,
                    state.request.prompt,
                    world,
                    reference_image_paths=state.request.reference_images,
                    project_root=self.source_repo_root,
                    max_workers=refinement.refinement_max_workers(self.environment),
                    max_iterations=refinement.refinement_max_iterations(
                        self.environment
                    ),
                )
            except ValueError as exc:
                raise PipelineIntegrationError(
                    f"World refinement configuration failed: {exc}"
                ) from exc
            world = refinement_run.world
            state.set_result(
                REFINEMENT_RESULT, copy.deepcopy(dict(refinement_run.record))
            )
            round_statuses = []
            for round_record in refinement_run.record["rounds"]:
                task_statuses = ", ".join(
                    f"{task['task_id']}={task['status']}"
                    for task in round_record["tasks"]
                )
                round_statuses.append(
                    f"round {round_record['iteration']}: {task_statuses}"
                )
            log(
                "Host Orchestrator：Subagent 迭代完成（"
                + "; ".join(round_statuses)
                + f"；{refinement_run.record['termination_reason']}）"
            )
        else:
            world = self.plan_world(
                state.request.prompt,
                references,
                reference_image_paths=state.request.reference_images,
                project_root=self.source_repo_root,
            )
        if not isinstance(world, Mapping):
            raise PipelineIntegrationError("planner did not return a WorldSpec object")
        state.set_result(REQUEST_RESULT, request)
        state.set_result(REFERENCES_RESULT, bindings)
        state.set_result(WORLD_RESULT, copy.deepcopy(dict(world)))
        state.set_result(PROJECT_RESULT, project_dir)
        _timings(state)["plan"] += _elapsed_ms(started)
        log(f"已规划 WorldSpec：{world.get('world_id', '<unknown>')}")

    def validate(self, state: PipelineState, log: LogSink) -> None:
        world = state.require_result(WORLD_RESULT)
        self.validate_world(world)
        log(f"WorldSpec 校验通过：{contracts.document_sha256(world)}")

    def _asset_environment(self, state: PipelineState) -> dict[str, str]:
        """Apply one paid asset-attempt budget across every run revision."""

        environment = dict(self.environment)
        configured = assets.AssetAgentConfig.from_environment(environment)
        previous = state.results.get(ASSET_RESULTS_RESULT, [])
        used = 0
        if isinstance(previous, list):
            used = sum(
                max(0, int(getattr(result, "attempted_generations", 0)))
                for result in previous
            )
        remaining = max(0, configured.max_generations - used)
        environment[assets.ASSET_MAX_GENERATIONS_ENV] = str(remaining)
        return environment

    def publish(self, state: PipelineState, log: LogSink) -> None:
        project_dir = Path(state.require_result(PROJECT_RESULT)).resolve(strict=False)
        request_document = state.require_result(REQUEST_RESULT)
        expected_parent = (
            self.output_root / request_document["request_hash"][:12]
        ).resolve(strict=False)
        if (
            project_dir.parent != expected_parent
            or re.fullmatch(r"run-[0-9a-f]{12}", project_dir.name) is None
        ):
            raise PipelineIntegrationError(
                f"generated project must be a unique run child of {expected_parent}"
            )
        published_dir = Path(self.publish_project(project_dir)).resolve(strict=False)
        if published_dir != project_dir:
            raise PipelineIntegrationError(
                f"publisher returned an unexpected target: {published_dir}"
            )

        bindings: Sequence[ReferenceBinding] = state.require_result(REFERENCES_RESULT)
        world = copy.deepcopy(state.require_result(WORLD_RESULT))
        world["brief"] = {
            "text": state.request.prompt,
            "references": [binding.contract_reference() for binding in bindings],
        }
        self.validate_world(world)

        for binding in bindings:
            destination = project_dir.joinpath(
                *PurePosixPath(binding.project_path).parts
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(binding.source, destination)
            if _sha256_file(destination) != binding.sha256:
                raise PipelineIntegrationError(
                    f"copied reference hash mismatch: {binding.project_path}"
                )

        spec_path = project_dir / "spec" / "world.json"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_bytes(contracts.canonical_json_bytes(world) + b"\n")

        run_id = request_document["request_hash"][:12]
        request_path = project_dir / "artifacts" / "runs" / run_id / "request.json"
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_bytes(
            contracts.canonical_json_bytes(request_document) + b"\n"
        )
        refinement_record = state.results.get(REFINEMENT_RESULT)
        if isinstance(refinement_record, Mapping):
            persisted_refinement = copy.deepcopy(dict(refinement_record))
            persisted_refinement["run_id"] = run_id
            persisted_refinement["request_hash"] = request_document["request_hash"]
            (request_path.parent / "refinement.json").write_bytes(
                contracts.canonical_json_bytes(persisted_refinement) + b"\n"
            )
        runtime = state.results.get(AGENT_RUNTIME_RESULT)
        if isinstance(runtime, agents.MultiAgentRuntime):
            runtime.write_trace(request_path.parent / "agent_trace.json", run_id=run_id)

        asset_started = time.perf_counter()
        asset_result = self.orchestrate_assets(
            world,
            project_dir,
            source_repo_root=self.source_repo_root,
            cache_root=self.output_root.parent / "asset-cache",
            environment=self._asset_environment(state),
            log=log,
        )
        _timings(state)["import"] += _elapsed_ms(asset_started)
        state.set_result(ASSET_RESULTS_RESULT, [asset_result])

        revision_dir = request_path.parent / "rev_0"
        revision_dir.mkdir(parents=True, exist_ok=True)
        (revision_dir / "world.json").write_bytes(
            contracts.canonical_json_bytes(world) + b"\n"
        )
        shutil.copy2(asset_result.catalog_path, revision_dir / "asset_catalog.json")
        shutil.copy2(asset_result.manifest_path, revision_dir / "asset_manifest.json")
        state.set_result(WORLD_RESULT, world)
        log(f"项目已发布：{project_dir}")
        log(f"WorldSpec 已写入：{spec_path}")
        log(
            "AssetAgent："
            f"生成 {asset_result.generated_assets}，缓存 {asset_result.cache_hits}，"
            f"API 尝试 {asset_result.attempted_generations}"
        )

    @staticmethod
    def _revision_environment(
        toolchain: Toolchain,
        base_environment: Mapping[str, str],
        run_id: str,
        revision: int,
        *,
        capture: bool = False,
    ) -> dict[str, str]:
        environment = _child_process_environment(base_environment)
        environment.update(
            {
                "DOTNET_ROOT": os.fspath(toolchain.dotnet_exe.parent),
                "DOTNET_CLI_UI_LANGUAGE": "en-US",
                "VSLANG": "1033",
                "PTP_RUN_ID": run_id,
                "PTP_REVISION": str(revision),
            }
        )
        if capture:
            environment["PTP_CAPTURE"] = "1"
        else:
            environment.pop("PTP_CAPTURE", None)
        return environment

    @staticmethod
    def _structural_report_path(
        project_dir: Path, run_id: str, revision: int
    ) -> Path:
        return (
            project_dir
            / "artifacts"
            / "runs"
            / run_id
            / f"rev_{revision}"
            / "structural_report.json"
        )

    def _run_structural_revision(
        self,
        project_dir: Path,
        toolchain: Toolchain,
        run_id: str,
        revision: int,
        log: LogSink,
        *,
        allow_repairable_failure: bool = False,
    ) -> dict[str, Any]:
        environment = self._revision_environment(
            toolchain, self.environment, run_id, revision
        )
        command = (
            os.fspath(toolchain.godot_exe),
            "--headless",
            "--path",
            os.fspath(project_dir),
            "--quit-after",
            "120",
        )
        report_path = self._structural_report_path(project_dir, run_id, revision)
        try:
            report_path.unlink(missing_ok=True)
        except OSError as exc:
            raise PipelineIntegrationError(
                f"cannot clear stale structural report: {report_path}: {exc}"
            ) from exc
        command_failure: PipelineIntegrationError | None = None
        try:
            self.run_command(command, project_dir, environment, log)
        except PipelineIntegrationError as exc:
            # Godot intentionally exits non-zero when a structural hard check
            # fails. Read the fresh report before deciding whether the failure
            # is a repairable world issue or an engine/runtime failure.
            command_failure = exc
        if not report_path.is_file():
            if command_failure is not None:
                raise command_failure
            raise PipelineIntegrationError(
                f"Godot did not write the structural report: {report_path}"
            )
        report = _read_json_object(report_path, "structural report")
        if report.get("status") != "pass":
            status = report.get("status", "invalid")
            details: list[str] = []
            checks = report.get("checks")
            if isinstance(checks, list):
                for check in checks:
                    if isinstance(check, Mapping) and check.get("passed") is False:
                        details.append(
                            f"{check.get('id', 'unknown_check')}: "
                            f"{check.get('message', 'failed')}"
                        )
            summary = "; ".join(details[:6]) or "no failure details were reported"
            scene_failed = any(
                isinstance(check, Mapping)
                and check.get("id") == "scene_loads"
                and check.get("passed") is False
                for check in (checks if isinstance(checks, list) else [])
            )
            if not allow_repairable_failure or scene_failed or status == "error":
                raise PipelineIntegrationError(
                    f"Godot structural checks did not pass ({status}): {summary}; "
                    f"report: {report_path}"
                ) from command_failure
            log(
                f"修订 {revision} 存在可修复结构问题，将交给 RepairAgent：{summary}"
            )
            return report
        if command_failure is not None:
            raise command_failure
        log(f"修订 {revision} 结构检查通过：{report_path}")
        return report

    def _capture_revision(
        self,
        project_dir: Path,
        toolchain: Toolchain,
        run_id: str,
        revision: int,
        log: LogSink,
    ) -> tuple[dict[str, Any], list[Path], list[str]]:
        environment = self._revision_environment(
            toolchain,
            self.environment,
            run_id,
            revision,
            capture=True,
        )
        command = (
            os.fspath(toolchain.visible_exe),
            "--path",
            os.fspath(project_dir),
            "--rendering-method",
            "gl_compatibility",
            "--audio-driver",
            "Dummy",
        )
        manifest_path = (
            project_dir
            / "artifacts"
            / "runs"
            / run_id
            / f"rev_{revision}"
            / "capture_manifest.json"
        )
        try:
            manifest_path.unlink(missing_ok=True)
        except OSError as exc:
            raise PipelineIntegrationError(
                f"cannot clear stale capture manifest: {manifest_path}: {exc}"
            ) from exc
        command_failure: PipelineIntegrationError | None = None
        try:
            self.run_command(command, project_dir, environment, log)
        except PipelineIntegrationError as exc:
            # A repairable structural failure deliberately survives long
            # enough to write captures, then Godot preserves exit code 1. A
            # valid fresh manifest is the evidence boundary; missing evidence
            # still propagates the process failure.
            command_failure = exc
        if not manifest_path.is_file() and command_failure is not None:
            raise command_failure
        manifest = _read_json_object(manifest_path, "capture manifest")
        current_world = _read_json_object(
            project_dir / "spec" / "world.json", "current WorldSpec"
        )
        expected_identity = {
            "schema": "prompt-to-play/capture-manifest@1",
            "run_id": run_id,
            "revision": revision,
            "world_id": current_world.get("world_id"),
        }
        for key, expected in expected_identity.items():
            if manifest.get(key) != expected:
                raise PipelineIntegrationError(
                    f"capture manifest {key} mismatch: expected {expected!r}, "
                    f"got {manifest.get(key)!r}"
                )
        captures = manifest.get("captures")
        if not isinstance(captures, list) or not captures:
            raise PipelineIntegrationError(
                f"capture manifest contains no screenshots: {manifest_path}"
            )
        absolute_paths: list[Path] = []
        relative_paths: list[str] = []
        for index, capture in enumerate(captures):
            if not isinstance(capture, Mapping) or not isinstance(
                capture.get("path"), str
            ):
                raise PipelineIntegrationError(
                    f"capture manifest entry {index} has no relative path"
                )
            relative = capture["path"]
            parsed = PurePosixPath(relative)
            if parsed.is_absolute() or ".." in parsed.parts:
                raise PipelineIntegrationError(
                    f"capture manifest entry escapes the project: {relative}"
                )
            image_path = project_dir.joinpath(*parsed.parts).resolve(strict=True)
            try:
                image_path.relative_to(project_dir)
            except ValueError as exc:
                raise PipelineIntegrationError(
                    f"capture manifest entry escapes the project: {relative}"
                ) from exc
            if image_path.suffix.lower() != ".png":
                raise PipelineIntegrationError(
                    f"capture manifest entry is not a PNG: {relative}"
                )
            expected_sha256 = capture.get("sha256")
            if (
                not isinstance(expected_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
                or _sha256_file(image_path) != expected_sha256
            ):
                raise PipelineIntegrationError(
                    f"capture manifest entry hash mismatch: {relative}"
                )
            absolute_paths.append(image_path)
            relative_paths.append(parsed.as_posix())
        log(f"修订 {revision} 已捕获 {len(absolute_paths)} 个评测视角")
        if command_failure is not None:
            log(
                f"修订 {revision} 的截图已保留；Godot 结构退出码将交给 RepairAgent"
            )
        return manifest, absolute_paths, relative_paths

    def _snapshot_revision(
        self,
        project_dir: Path,
        run_id: str,
        revision: int,
        world: Mapping[str, Any],
        asset_result: assets.AssetAgentResult,
    ) -> None:
        revision_dir = (
            project_dir / "artifacts" / "runs" / run_id / f"rev_{revision}"
        )
        revision_dir.mkdir(parents=True, exist_ok=True)
        (revision_dir / "world.json").write_bytes(
            contracts.canonical_json_bytes(world) + b"\n"
        )
        shutil.copy2(asset_result.catalog_path, revision_dir / "asset_catalog.json")
        shutil.copy2(asset_result.manifest_path, revision_dir / "asset_manifest.json")

    def _refresh_assets(
        self,
        state: PipelineState,
        project_dir: Path,
        run_id: str,
        revision: int,
        world: Mapping[str, Any],
        log: LogSink,
    ) -> assets.AssetAgentResult:
        started = time.perf_counter()
        result = self.orchestrate_assets(
            world,
            project_dir,
            source_repo_root=self.source_repo_root,
            cache_root=self.output_root.parent / "asset-cache",
            environment=self._asset_environment(state),
            log=log,
        )
        _timings(state)["import"] += _elapsed_ms(started)
        results = state.results.setdefault(ASSET_RESULTS_RESULT, [])
        if isinstance(results, list):
            results.append(result)
        self._snapshot_revision(project_dir, run_id, revision, world, result)
        return result

    def build(self, state: PipelineState, log: LogSink) -> None:
        project_dir = Path(state.require_result(PROJECT_RESULT)).resolve(strict=True)
        toolchain = discover_toolchain(
            self.source_repo_root,
            environment=self.environment,
            which=self.which,
        )
        state.set_result(TOOLCHAIN_RESULT, toolchain)
        request_document = state.require_result(REQUEST_RESULT)
        run_id = request_document["request_hash"][:12]
        build_started = time.perf_counter()
        build_environment = self._revision_environment(
            toolchain, self.environment, run_id, 0
        )
        for command in (
            (
                os.fspath(toolchain.dotnet_exe),
                "restore",
                "--source",
                os.fspath(toolchain.godot_nupkgs),
                "--ignore-failed-sources",
            ),
            (os.fspath(toolchain.dotnet_exe), "build", "--no-restore"),
        ):
            self.run_command(command, project_dir, build_environment, log)
        _timings(state)["build"] += _elapsed_ms(build_started)

        runtime = state.results.get(AGENT_RUNTIME_RESULT)
        if not isinstance(runtime, agents.MultiAgentRuntime):
            structural_started = time.perf_counter()
            report = self._run_structural_revision(
                project_dir, toolchain, run_id, 0, log
            )
            _timings(state)["build"] += _elapsed_ms(structural_started)
            state.set_result(STRUCTURAL_REPORT_RESULT, report)
            state.set_result(SELECTED_REVISION_RESULT, 0)
            return

        policy = contracts.load_json(
            self.source_repo_root / "prompt_to_play" / "evaluation_policy.json"
        )
        contracts.validate_evaluation_policy(policy)
        world = copy.deepcopy(state.require_result(WORLD_RESULT))
        reference_bindings: Sequence[ReferenceBinding] = state.require_result(
            REFERENCES_RESULT
        )
        reference_image_paths: list[Path] = []
        for binding in reference_bindings:
            copied_reference = project_dir.joinpath(
                *PurePosixPath(binding.project_path).parts
            ).resolve(strict=True)
            try:
                copied_reference.relative_to(project_dir)
            except ValueError as exc:
                raise PipelineIntegrationError(
                    f"published reference escapes the project: {binding.project_path}"
                ) from exc
            if _sha256_file(copied_reference) != binding.sha256:
                raise PipelineIntegrationError(
                    f"published reference hash changed: {binding.project_path}"
                )
            reference_image_paths.append(copied_reference)
        evaluations: list[tuple[str, dict[str, Any]]] = []
        evaluation_worlds: dict[int, dict[str, Any]] = {}
        max_iterations = int(policy["max_correction_iterations"])
        prior_world_hashes = _prior_world_hashes(
            project_dir, request_document["request_hash"]
        )

        for revision in range(max_iterations + 1):
            iteration_started_utc = _utc_timestamp()
            structural_started = time.perf_counter()
            structural = self._run_structural_revision(
                project_dir,
                toolchain,
                run_id,
                revision,
                log,
                allow_repairable_failure=True,
            )
            _timings(state)["build"] += _elapsed_ms(structural_started)

            capture_started = time.perf_counter()
            _manifest, screenshots, screenshot_relatives = self._capture_revision(
                project_dir, toolchain, run_id, revision, log
            )
            _timings(state)["capture"] += _elapsed_ms(capture_started)

            evaluate_started = time.perf_counter()
            visual_agent = evaluator.VisualEvaluationAgent(
                runtime.agent(agents.AgentRole.VISUAL_EVALUATOR),
                project_root=project_dir,
            )
            feedback = visual_agent.evaluate(
                state.request.prompt,
                world,
                screenshots,
                reference_image_paths=reference_image_paths,
            )
            _timings(state)["evaluate"] += _elapsed_ms(evaluate_started)

            revision_dir = (
                project_dir / "artifacts" / "runs" / run_id / f"rev_{revision}"
            )
            feedback_path = revision_dir / "visual_feedback.json"
            feedback_path.write_bytes(
                contracts.canonical_json_bytes(feedback) + b"\n"
            )
            usage = runtime.aggregate_usage()
            report = _build_evaluation_report(
                run_id=run_id,
                iteration=revision,
                world=world,
                structural_report=structural,
                visual_feedback=feedback,
                screenshot_paths=screenshot_relatives,
                timing_ms=_timings(state),
                usage=usage,
                policy=policy,
                prior_world_hashes=prior_world_hashes,
                started_at_utc=iteration_started_utc,
                finished_at_utc=_utc_timestamp(),
            )
            evaluation_path = revision_dir / "evaluation.json"
            evaluation_path.write_bytes(
                contracts.canonical_json_bytes(report) + b"\n"
            )
            evaluations.append((f"rev_{revision}/evaluation.json", report))
            evaluation_worlds[revision] = copy.deepcopy(world)
            log(
                f"VisualEvaluationAgent：修订 {revision} 相似度 "
                f"{feedback['scene_similarity']:.2f}，"
                f"{'接受' if feedback['accepted'] else '需要修正'}"
            )
            runtime.write_trace(
                project_dir / "artifacts" / "runs" / run_id / "agent_trace.json",
                run_id=run_id,
            )

            if report["next_action"] != "patch":
                break

            repair_started = time.perf_counter()
            repair_agent = evaluator.RepairAgent(
                runtime.agent(agents.AgentRole.REPAIR),
                project_root=project_dir,
            )
            revised, patch = repair_agent.repair(
                state.request.prompt,
                world,
                feedback,
                screenshots,
                iteration=revision + 1,
                reference_image_paths=reference_image_paths,
                structural_report=structural,
            )
            _timings(state)["evaluate"] += _elapsed_ms(repair_started)
            patch_path = revision_dir / f"patch_to_rev_{revision + 1}.json"
            patch_path.write_bytes(contracts.canonical_json_bytes(patch) + b"\n")
            world = revised
            spec_path = project_dir / "spec" / "world.json"
            spec_path.write_bytes(contracts.canonical_json_bytes(world) + b"\n")
            self._refresh_assets(
                state,
                project_dir,
                run_id,
                revision + 1,
                world,
                log,
            )
            state.set_result(WORLD_RESULT, copy.deepcopy(world))
            log(f"RepairAgent：已生成并应用修订 {revision + 1}")

        selection = lifecycle.select_best_revision(evaluations)
        selection_path = (
            project_dir / "artifacts" / "runs" / run_id / "selection.json"
        )
        selection_path.write_bytes(
            contracts.canonical_json_bytes(selection) + b"\n"
        )
        selected_source = selection["selected"]["source"]
        match = re.fullmatch(r"rev_(\d+)/evaluation\.json", selected_source)
        if match is None:
            raise PipelineIntegrationError(
                f"best-revision selector returned an unexpected source: {selected_source}"
            )
        selected_revision = int(match.group(1))
        selected_world = evaluation_worlds[selected_revision]
        selected_dir = (
            project_dir
            / "artifacts"
            / "runs"
            / run_id
            / f"rev_{selected_revision}"
        )
        (project_dir / "spec" / "world.json").write_bytes(
            contracts.canonical_json_bytes(selected_world) + b"\n"
        )
        shutil.copy2(selected_dir / "asset_catalog.json", project_dir / "assets" / "catalog.json")
        shutil.copy2(selected_dir / "asset_manifest.json", project_dir / "assets" / "manifest.json")
        selected_structural = _read_json_object(
            selected_dir / "structural_report.json", "selected structural report"
        )
        state.set_result(WORLD_RESULT, selected_world)
        state.set_result(STRUCTURAL_REPORT_RESULT, selected_structural)
        state.set_result(EVALUATIONS_RESULT, [report for _source, report in evaluations])
        state.set_result(SELECTED_REVISION_RESULT, selected_revision)
        runtime.write_trace(
            project_dir / "artifacts" / "runs" / run_id / "agent_trace.json",
            run_id=run_id,
        )
        selected_evaluation = evaluations[selected_revision][1]
        _require_passing_selection(
            selected_evaluation, selected_revision, selection_path
        )
        log(f"已选择通过全部评价门槛的修订 {selected_revision}：{selection_path}")

    def launch(self, state: PipelineState, log: LogSink) -> None:
        project_dir = Path(state.require_result(PROJECT_RESULT)).resolve(strict=True)
        toolchain: Toolchain = state.require_result(TOOLCHAIN_RESULT)
        request_document = state.require_result(REQUEST_RESULT)
        run_id = request_document["request_hash"][:12]
        selected_revision = int(state.results.get(SELECTED_REVISION_RESULT, 0))
        run_environment = _child_process_environment(self.environment)
        run_environment.update(
            {
                "DOTNET_ROOT": os.fspath(toolchain.dotnet_exe.parent),
                "DOTNET_CLI_UI_LANGUAGE": "en-US",
                "VSLANG": "1033",
                "PTP_RUN_ID": run_id,
                "PTP_REVISION": str(selected_revision),
            }
        )
        run_environment.pop("PTP_CAPTURE", None)
        command = (
            os.fspath(toolchain.visible_exe),
            "--path",
            os.fspath(project_dir),
            "--rendering-method",
            "gl_compatibility",
            "--audio-driver",
            "Dummy",
        )
        play_log = (
            project_dir
            / "artifacts"
            / "runs"
            / run_id
            / f"rev_{selected_revision}"
            / "play.log"
        )
        process = self.start_process(command, project_dir, run_environment, play_log)
        state.set_result(PROCESS_RESULT, process)
        return_code = _probe_process(process, self.launch_probe_seconds)
        if return_code is not None:
            tail = _read_log_tail(play_log)
            diagnostic = f"\n{tail}" if tail else ""
            raise PipelineIntegrationError(
                f"Godot exited during startup with code {return_code}; "
                f"log: {play_log}{diagnostic}"
            )
        pid = getattr(process, "pid", "unknown")
        log(f"Godot 已启动（最佳修订 {selected_revision}，PID {pid}）：{project_dir}")
        log(f"运行日志：{play_log}")


def create_pipeline_commands(
    dependencies: PipelineDependencies | None = None,
) -> PipelineCommands:
    return PipelineStages(dependencies).commands()


def main() -> int:
    return run_launcher(create_pipeline_commands())


if __name__ == "__main__":
    raise SystemExit(main())
