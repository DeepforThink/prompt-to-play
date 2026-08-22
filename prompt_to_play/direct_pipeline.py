"""Direct prompt-to-play orchestration for Godot projects.

The default pipeline intentionally has no semantic world intermediate.  A
project-generation agent returns a bounded bundle of real Godot scene/script
files, the trusted host builds and runs those files, and a code-repair agent
patches the same files using compiler, structural, and rendered evidence.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import shutil
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Protocol, Sequence

from . import agents
from .direct_common import build_request
from .direct_agents import (
    CodeRepairAgent,
    DirectVisualEvaluationAgent,
    ProjectGeneratorAgent,
)
from .direct_generation import (
    FilePlan,
    apply_file_plan,
    parse_file_plan,
)
from .direct_evaluation import (
    DEFAULT_MIN_SCENE_SIMILARITY,
    VISUAL_SCORE_DIMENSIONS,
    validate_direct_visual_feedback,
)
from .direct_host import (
    SOURCE_REPO_ROOT,
    PipelineIntegrationError,
    ReferenceBinding,
    Toolchain,
    child_process_environment as _child_process_environment,
    discover_toolchain,
    probe_process as _probe_process,
    read_json_object as _read_json_object,
    read_log_tail as _read_log_tail,
    run_command as _default_run_command,
    sha256_file as _sha256_file,
    stable_references as _stable_references,
    start_process as _default_start_process,
    utc_timestamp as _utc_timestamp,
    write_json as _write_json,
)
from .launcher import LogSink, PipelineCommands, PipelineState, run_launcher


DIRECT_EVALUATION_CONTRACT = "prompt-to-play/direct-evaluation@2"
DIRECT_SELECTION_CONTRACT = "prompt-to-play/direct-selection@2"

REQUEST_RESULT = "request"
REFERENCES_RESULT = "reference_bindings"
FILE_PLAN_RESULT = "direct_file_plan"
PROJECT_RESULT = "project_dir"
PROJECT_MANIFEST_RESULT = "direct_project_manifest"
TOOLCHAIN_RESULT = "toolchain"
STRUCTURAL_REPORT_RESULT = "direct_structural_report"
PROCESS_RESULT = "godot_process"
AGENT_RUNTIME_RESULT = "multi_agent_runtime"
EVALUATIONS_RESULT = "direct_evaluations"
SELECTED_REVISION_RESULT = "selected_revision"
TIMING_RESULT = "timing_ms"

DEFAULT_CORRECTION_ITERATIONS = 4
MAX_CORRECTION_ITERATIONS = 6
MIN_SCENE_SIMILARITY = DEFAULT_MIN_SCENE_SIMILARITY
RUN_DIRECTORY_RE = re.compile(r"^run-[0-9a-f]{12}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_STRUCTURAL_CHECK_IDS = frozenset(
    {
        "harness_ready",
        "run_identifier_safe",
        "generated_entry_exists",
        "generated_entry_instantiates",
        "gameplay_root_declared",
        "renderable_content",
        "capture_camera_available",
        "player_declared",
        "objective_declared",
        "hud_declared",
        "interaction_responds_to_input",
        "host_process_completed",
    }
)


class GenerateProject(Protocol):
    def __call__(
        self,
        prompt: str,
        *,
        seed: int,
        reference_image_paths: Sequence[str | Path],
    ) -> Mapping[str, Any]: ...


class EvaluateVisual(Protocol):
    def __call__(
        self,
        prompt: str,
        screenshot_image_paths: Sequence[str | Path],
        *,
        reference_image_paths: Sequence[str | Path],
        project_manifest: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class RepairProject(Protocol):
    def __call__(
        self,
        prompt: str,
        current_files: Mapping[str, str],
        *,
        diagnostics: str,
        structural_report: Mapping[str, Any],
        visual_feedback: Mapping[str, Any],
        screenshot_image_paths: Sequence[str | Path],
        reference_image_paths: Sequence[str | Path],
    ) -> Mapping[str, Any]: ...


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
        self, repo: Path, environment: Mapping[str, str]
    ) -> agents.MultiAgentRuntime: ...


@dataclass
class DirectPipelineDependencies:
    """Replaceable effects used by tests and alternate launcher embeddings."""

    source_repo_root: Path = SOURCE_REPO_ROOT
    output_root: Path | None = None
    environment: Mapping[str, str] | None = None
    generate_project: GenerateProject | None = None
    evaluate_visual: EvaluateVisual | None = None
    repair_project: RepairProject | None = None
    publish_project: Callable[[Path], Path] | None = None
    run_command: CommandRunner | None = None
    start_process: ProcessStarter | None = None
    agent_runtime_factory: AgentRuntimeFactory | None = None
    max_correction_iterations: int = DEFAULT_CORRECTION_ITERATIONS
    min_scene_similarity: float = MIN_SCENE_SIMILARITY
    launch_probe_seconds: float = 1.25
    which: Callable[[str], str | None] = shutil.which


@dataclass(frozen=True)
class RevisionCandidate:
    revision: int
    project_sha256: str
    build_passed: bool
    structural_passed: bool
    capture_passed: bool
    scene_similarity: float
    accepted: bool
    evaluation_path: str

    @property
    def playable(self) -> bool:
        return self.build_passed and self.structural_passed


def _default_agent_runtime_factory(
    repo: Path, environment: Mapping[str, str]
) -> agents.MultiAgentRuntime:
    return agents.MultiAgentRuntime(repo, environment=environment)


def _timings(state: PipelineState) -> dict[str, int]:
    value = state.results.get(TIMING_RESULT)
    if isinstance(value, dict):
        return value
    created = {
        "generate": 0,
        "publish": 0,
        "restore": 0,
        "build": 0,
        "structural": 0,
        "capture": 0,
        "evaluate": 0,
        "repair": 0,
    }
    state.set_result(TIMING_RESULT, created)
    return created


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def _publish_template(template_root: Path, target: Path) -> Path:
    source = template_root.resolve(strict=True)
    if not source.is_dir():
        raise PipelineIntegrationError(f"direct template is not a directory: {source}")
    if target.exists():
        raise PipelineIntegrationError(f"generated project already exists: {target}")
    required_files = (
        ".gitignore",
        "project.godot",
        "PromptToPlayDirect.csproj",
    )
    required_directories = ("harness", "generated")
    missing = [name for name in required_files if not (source / name).is_file()]
    missing.extend(
        name for name in required_directories if not (source / name).is_dir()
    )
    if missing:
        raise PipelineIntegrationError(
            "direct template is incomplete: " + ", ".join(sorted(missing))
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    for name in required_files:
        shutil.copy2(source / name, target / name)
    for name in required_directories:
        shutil.copytree(source / name, target / name, symlinks=False)
    return target


def _direct_template_source(source_repo_root: Path) -> Path:
    packaged = source_repo_root / "prompt_to_play" / "direct_template"
    if packaged.is_dir():
        return packaged
    # A project produced by publish.py already has the trusted template at its
    # repository root. Copy only the explicit template allowlist above.
    return source_repo_root


def _generated_files(project_dir: Path) -> dict[str, str]:
    generated = project_dir / "generated"
    if not generated.is_dir() or generated.is_symlink():
        raise PipelineIntegrationError("generated/ must be a real directory")
    result: dict[str, str] = {}
    for path in sorted(generated.rglob("*"), key=lambda value: value.as_posix()):
        if path.is_symlink():
            raise PipelineIntegrationError("generated/ must not contain links")
        if not path.is_file():
            continue
        relative = path.relative_to(project_dir).as_posix()
        try:
            result[relative] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise PipelineIntegrationError(
                f"cannot read generated source {relative}: {exc}"
            ) from exc
    if not result:
        raise PipelineIntegrationError("generated/ contains no project files")
    return result


def _generated_digest(project_dir: Path) -> tuple[str, list[dict[str, Any]]]:
    files = _generated_files(project_dir)
    digest = hashlib.sha256()
    manifest_files: list[dict[str, Any]] = []
    for path, text in sorted(files.items(), key=lambda item: item[0].casefold()):
        data = text.encode("utf-8")
        digest.update(path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
        manifest_files.append(
            {
                "path": path,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return digest.hexdigest(), manifest_files


def _assert_required_entry(plan: FilePlan, *, initial: bool) -> None:
    entry = "generated/GeneratedGame.tscn"
    operations = [operation for operation in plan.files if operation.path == entry]
    if initial and not any(operation.action != "delete" for operation in operations):
        raise PipelineIntegrationError(
            "direct generator must write generated/GeneratedGame.tscn"
        )
    if initial and any(
        operation.action in {"replace", "delete"} for operation in plan.files
    ):
        raise PipelineIntegrationError(
            "initial direct package must use upsert/create against empty generated/"
        )
    if any(operation.action == "delete" for operation in operations):
        raise PipelineIntegrationError(
            "direct patch must not delete generated/GeneratedGame.tscn"
        )


def _reset_generated_directory(project_dir: Path) -> None:
    root = project_dir.resolve(strict=True)
    generated = (root / "generated").resolve(strict=True)
    if generated.parent != root or generated.name != "generated":
        raise PipelineIntegrationError("refusing to reset an unexpected directory")
    if generated.is_symlink() or not generated.is_dir():
        raise PipelineIntegrationError("generated/ must be a real directory")
    is_junction = getattr(generated, "is_junction", None)
    if callable(is_junction) and is_junction():
        raise PipelineIntegrationError("generated/ must not be a junction")
    shutil.rmtree(generated)
    generated.mkdir()


def _snapshot_generated(
    project_dir: Path,
    revision_dir: Path,
    expected_sha256: str,
) -> Mapping[str, Any]:
    digest, files = _generated_digest(project_dir)
    if digest != expected_sha256:
        raise PipelineIntegrationError(
            "generated project hash changed outside the validated file-plan boundary"
        )
    archive = revision_dir / "generated_snapshot.zip"
    if archive.exists():
        raise PipelineIntegrationError(f"revision snapshot already exists: {archive}")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as output:
        for item in files:
            relative = str(item["path"])
            data = project_dir.joinpath(*PurePosixPath(relative).parts).read_bytes()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            output.writestr(info, data, compresslevel=9)
    document = {
        "schema": "prompt-to-play/direct-snapshot@1",
        "project_sha256": digest,
        "archive": "generated_snapshot.zip",
        "archive_sha256": _sha256_file(archive),
        "files": files,
    }
    _write_json(revision_dir / "generated_snapshot_manifest.json", document)
    return document


def _restore_generated(
    project_dir: Path,
    revision_dir: Path,
    expected_sha256: str,
) -> None:
    generated = (project_dir / "generated").resolve(strict=True)
    expected_parent = project_dir.resolve(strict=True)
    if generated.parent != expected_parent or generated.name != "generated":
        raise PipelineIntegrationError("refusing to replace an unexpected directory")
    snapshot = (revision_dir / "generated_snapshot.zip").resolve(strict=True)
    expected_revision = revision_dir.resolve(strict=True)
    if (
        snapshot.parent != expected_revision
        or snapshot.name != "generated_snapshot.zip"
    ):
        raise PipelineIntegrationError("invalid generated snapshot path")
    snapshot_manifest = _read_json_object(
        revision_dir / "generated_snapshot_manifest.json",
        "generated snapshot manifest",
    )
    if (
        snapshot_manifest.get("schema") != "prompt-to-play/direct-snapshot@1"
        or snapshot_manifest.get("project_sha256") != expected_sha256
        or snapshot_manifest.get("archive") != snapshot.name
        or snapshot_manifest.get("archive_sha256") != _sha256_file(snapshot)
    ):
        raise PipelineIntegrationError("generated snapshot manifest mismatch")
    manifest_files = snapshot_manifest.get("files")
    if not isinstance(manifest_files, list) or not manifest_files:
        raise PipelineIntegrationError("generated snapshot manifest has no files")
    expected_paths = {
        item.get("path")
        for item in manifest_files
        if isinstance(item, Mapping) and isinstance(item.get("path"), str)
    }
    if len(expected_paths) != len(manifest_files):
        raise PipelineIntegrationError("generated snapshot manifest paths are invalid")

    restored: dict[str, bytes] = {}
    with zipfile.ZipFile(snapshot, "r") as archive:
        for info in archive.infolist():
            relative = PurePosixPath(info.filename)
            if (
                info.is_dir()
                or relative.is_absolute()
                or ".." in relative.parts
                or len(relative.parts) < 2
                or relative.parts[0] != "generated"
                or info.filename in restored
            ):
                raise PipelineIntegrationError(
                    "generated snapshot contains an unsafe path"
                )
            restored[info.filename] = archive.read(info)
    if set(restored) != expected_paths:
        raise PipelineIntegrationError("generated snapshot file list mismatch")

    shutil.rmtree(generated)
    generated.mkdir()
    for relative, data in sorted(restored.items()):
        target = project_dir.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    actual, _files = _generated_digest(project_dir)
    if actual != expected_sha256:
        raise PipelineIntegrationError("restored generated snapshot hash mismatch")


def _host_visual_failure(code: str, message: str) -> dict[str, Any]:
    return {
        "scores": {dimension: 0.0 for dimension in VISUAL_SCORE_DIMENSIONS},
        "scene_similarity": 0.0,
        "accepted": False,
        "issues": [
            {
                "code": code,
                "severity": "blocker",
                "entity_id": None,
                "message": message,
                "suggested_fix": "修复运行或相机输出，使可信宿主能够获得非空游戏截图。",
            }
        ],
    }


def _structural_passed(report: Mapping[str, Any]) -> bool:
    if report.get("status") != "pass" or report.get("mode") != "direct-project":
        return False
    checks = report.get("checks")
    if not isinstance(checks, list) or not checks:
        return False
    ids: list[str] = []
    for check in checks:
        if (
            not isinstance(check, Mapping)
            or not isinstance(check.get("id"), str)
            or check.get("kind") != "hard"
            or check.get("passed") is not True
            or check.get("score") != 1
        ):
            return False
        ids.append(check["id"])
    if len(ids) != len(set(ids)) or not REQUIRED_STRUCTURAL_CHECK_IDS.issubset(ids):
        return False
    summary = report.get("summary")
    if (
        not isinstance(summary, Mapping)
        or summary.get("passed") != len(checks)
        or summary.get("failed") != 0
    ):
        return False
    interaction = report.get("interaction_probe")
    if not isinstance(interaction, Mapping):
        return False
    return (
        interaction.get("required") is True
        and interaction.get("status") == "pass"
        and isinstance(interaction.get("target_count"), int)
        and interaction["target_count"] > 0
        and isinstance(interaction.get("declared_action_count"), int)
        and interaction["declared_action_count"] > 0
        and isinstance(interaction.get("attempted_action_count"), int)
        and interaction["attempted_action_count"] > 0
        and interaction.get("state_changed") is True
        and isinstance(interaction.get("evidence"), list)
        and any("changed=true" in str(item) for item in interaction["evidence"])
    )


def _candidate_key(
    candidate: RevisionCandidate,
) -> tuple[int, int, int, int, float, int]:
    return (
        int(candidate.accepted),
        int(candidate.playable),
        int(candidate.build_passed),
        int(candidate.capture_passed),
        candidate.scene_similarity,
        -candidate.revision,
    )


class DirectPipelineStages:
    """The launcher's five stages backed by direct project generation."""

    def __init__(self, dependencies: DirectPipelineDependencies | None = None) -> None:
        dependencies = (
            DirectPipelineDependencies() if dependencies is None else dependencies
        )
        self.source_repo_root = dependencies.source_repo_root.resolve(strict=False)
        self.output_root = (
            self.source_repo_root.parent / "output" / "generated"
            if dependencies.output_root is None
            else dependencies.output_root.resolve(strict=False)
        )
        self.environment = dict(
            os.environ if dependencies.environment is None else dependencies.environment
        )
        self.generate_project = dependencies.generate_project
        self.evaluate_visual = dependencies.evaluate_visual
        self.repair_project = dependencies.repair_project
        self.publish_project = dependencies.publish_project or (
            lambda target: _publish_template(
                _direct_template_source(self.source_repo_root), target
            )
        )
        self.run_command = dependencies.run_command or _default_run_command
        self.start_process = dependencies.start_process or _default_start_process
        self.agent_runtime_factory = (
            dependencies.agent_runtime_factory or _default_agent_runtime_factory
        )
        self.max_correction_iterations = int(dependencies.max_correction_iterations)
        if not 0 <= self.max_correction_iterations <= MAX_CORRECTION_ITERATIONS:
            raise ValueError(
                f"max_correction_iterations must be between 0 and {MAX_CORRECTION_ITERATIONS}"
            )
        self.min_scene_similarity = float(dependencies.min_scene_similarity)
        if not 0.0 <= self.min_scene_similarity <= 1.0:
            raise ValueError("min_scene_similarity must be between 0 and 1")
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

    def _runtime(self, state: PipelineState) -> agents.MultiAgentRuntime:
        runtime = state.results.get(AGENT_RUNTIME_RESULT)
        if runtime is None:
            runtime = self.agent_runtime_factory(
                self.source_repo_root, self.environment
            )
            state.set_result(AGENT_RUNTIME_RESULT, runtime)
        return runtime

    def plan(self, state: PipelineState, log: LogSink) -> None:
        started = time.perf_counter()
        request = build_request(
            state.request.prompt,
            state.request.reference_images,
            base_dir=self.source_repo_root.parent,
        )
        bindings = _stable_references(state.request.reference_images, request)
        request_root = self.output_root / request["request_hash"][:12]
        project_dir = request_root / f"run-{uuid.uuid4().hex[:12]}"

        if self.generate_project is None:
            generator = ProjectGeneratorAgent(
                self._runtime(state).agent(agents.AgentRole.PROJECT_GENERATOR)
            )
            document = generator.generate(
                request["prompt"],
                seed=request["seed"],
                reference_image_paths=state.request.reference_images,
            )
        else:
            document = self.generate_project(
                request["prompt"],
                seed=request["seed"],
                reference_image_paths=state.request.reference_images,
            )

        state.set_result(REQUEST_RESULT, request)
        state.set_result(REFERENCES_RESULT, bindings)
        state.set_result(FILE_PLAN_RESULT, copy.deepcopy(dict(document)))
        state.set_result(PROJECT_RESULT, project_dir.resolve(strict=False))
        _timings(state)["generate"] += _elapsed_ms(started)
        log("ProjectGeneratorAgent：已将原始 Prompt 直接生成 Godot 文件包。")

    def validate(self, state: PipelineState, log: LogSink) -> None:
        plan = parse_file_plan(state.require_result(FILE_PLAN_RESULT))
        _assert_required_entry(plan, initial=True)
        log(f"安全文件协议通过：{len(plan.files)} 个操作，范围仅限 generated/**。")

    def publish(self, state: PipelineState, log: LogSink) -> None:
        started = time.perf_counter()
        request = state.require_result(REQUEST_RESULT)
        project_dir = Path(state.require_result(PROJECT_RESULT)).resolve(strict=False)
        expected_parent = (self.output_root / request["request_hash"][:12]).resolve(
            strict=False
        )
        if (
            project_dir.parent != expected_parent
            or RUN_DIRECTORY_RE.fullmatch(project_dir.name) is None
        ):
            raise PipelineIntegrationError(
                "direct publish target must be a unique run child of its request hash"
            )

        published = Path(self.publish_project(project_dir)).resolve(strict=True)
        if published != project_dir.resolve(strict=True):
            raise PipelineIntegrationError(
                f"publisher returned an unexpected project path: {published}"
            )

        bindings: Sequence[ReferenceBinding] = state.require_result(REFERENCES_RESULT)
        for binding in bindings:
            relative = PurePosixPath(binding.project_path)
            target = published.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(binding.source, target)
            if _sha256_file(target) != binding.sha256:
                raise PipelineIntegrationError(
                    f"copied reference hash mismatch: {binding.project_path}"
                )

        run_id = request["request_hash"][:12]
        revision_dir = published / "artifacts" / "runs" / run_id / "rev_0"
        _write_json(published / "artifacts" / "runs" / run_id / "request.json", request)
        file_document = state.require_result(FILE_PLAN_RESULT)
        _write_json(revision_dir / "initial_file_plan.json", file_document)
        _reset_generated_directory(published)
        manifest = apply_file_plan(
            published,
            file_document,
            manifest_path=(
                f"artifacts/runs/{run_id}/rev_0/direct_generation_manifest.json"
            ),
        )
        state.set_result(PROJECT_MANIFEST_RESULT, copy.deepcopy(dict(manifest)))
        _timings(state)["publish"] += _elapsed_ms(started)
        log(f"直接生成项目已持久化：{published}")
        log(f"当前 generated/** 哈希：{manifest['project_sha256']}")

    @staticmethod
    def _revision_dir(project_dir: Path, run_id: str, revision: int) -> Path:
        return project_dir / "artifacts" / "runs" / run_id / f"rev_{revision}"

    def _revision_environment(
        self,
        toolchain: Toolchain,
        run_id: str,
        revision: int,
        project_sha256: str,
        *,
        capture: bool = False,
        automation: bool = False,
    ) -> dict[str, str]:
        environment = _child_process_environment(self.environment)
        environment.update(
            {
                "DOTNET_ROOT": os.fspath(toolchain.dotnet_exe.parent),
                "DOTNET_CLI_UI_LANGUAGE": "en-US",
                "VSLANG": "1033",
                "PTP_RUN_ID": run_id,
                "PTP_REVISION": str(revision),
                "PTP_PROJECT_SHA256": project_sha256,
            }
        )
        if capture:
            environment["PTP_CAPTURE"] = "1"
        if automation:
            environment["PTP_AUTOMATION"] = "1"
        return environment

    def _run_logged(
        self,
        command: Sequence[str],
        project_dir: Path,
        environment: Mapping[str, str],
        log: LogSink,
    ) -> tuple[bool, str]:
        lines: list[str] = []

        def collect(message: str) -> None:
            text = str(message).rstrip()
            if text:
                lines.append(text)
                log(text)

        try:
            self.run_command(command, project_dir, environment, collect)
            return True, "\n".join(lines)[-40_000:]
        except Exception as exc:
            lines.append(f"{type(exc).__name__}: {exc}")
            return False, "\n".join(lines)[-40_000:]

    def _run_structural(
        self,
        project_dir: Path,
        toolchain: Toolchain,
        run_id: str,
        revision: int,
        project_sha256: str,
        log: LogSink,
    ) -> tuple[dict[str, Any], str]:
        revision_dir = self._revision_dir(project_dir, run_id, revision)
        report_path = revision_dir / "direct_structural_report.json"
        report_path.unlink(missing_ok=True)
        environment = self._revision_environment(
            toolchain,
            run_id,
            revision,
            project_sha256,
            automation=True,
        )
        command = (
            os.fspath(toolchain.godot_exe),
            "--headless",
            "--path",
            os.fspath(project_dir),
            "--quit-after",
            "120",
        )
        succeeded, diagnostic = self._run_logged(command, project_dir, environment, log)
        if not report_path.is_file():
            report = {
                "schema": "prompt-to-play/direct-structural-report@1",
                "mode": "direct-project",
                "run_id": run_id,
                "revision": revision,
                "project_sha256": project_sha256,
                "status": "error",
                "checks": [
                    {
                        "id": "report_written",
                        "kind": "hard",
                        "passed": False,
                        "score": 0,
                        "message": "Godot did not write a structural report.",
                        "evidence": [],
                    }
                ],
                "summary": {"passed": 0, "failed": 1},
            }
            _write_json(report_path, report)
            return report, diagnostic
        report = _read_json_object(report_path, "direct structural report")
        expected = {
            "schema": "prompt-to-play/direct-structural-report@1",
            "mode": "direct-project",
            "run_id": run_id,
            "revision": revision,
            "project_sha256": project_sha256,
        }
        if any(report.get(key) != value for key, value in expected.items()):
            raise PipelineIntegrationError(
                f"direct structural report identity mismatch: {report_path}"
            )
        raw_checks = report.get("checks")
        checks = (
            [
                copy.deepcopy(dict(check))
                for check in raw_checks
                if isinstance(check, Mapping)
            ]
            if isinstance(raw_checks, list)
            else []
        )
        checks = [
            check for check in checks if check.get("id") != "host_process_completed"
        ]
        checks.append(
            {
                "id": "host_process_completed",
                "kind": "hard",
                "passed": succeeded,
                "score": 1 if succeeded else 0,
                "message": (
                    "Godot headless verification exited successfully."
                    if succeeded
                    else "Godot headless verification failed, crashed, or timed out."
                ),
                "evidence": [],
            }
        )
        normalized = copy.deepcopy(dict(report))
        normalized["checks"] = sorted(
            checks, key=lambda check: str(check.get("id", ""))
        )
        normalized["status"] = (
            "pass"
            if succeeded
            and all(check.get("passed") is True for check in normalized["checks"])
            else "fail"
        )
        normalized["summary"] = {
            "passed": sum(
                check.get("passed") is True for check in normalized["checks"]
            ),
            "failed": sum(
                check.get("passed") is not True for check in normalized["checks"]
            ),
        }
        report = normalized
        _write_json(report_path, report)
        if not succeeded:
            diagnostic += "\nGodot headless verification process did not exit cleanly."
        elif not _structural_passed(report):
            diagnostic += "\nGodot reported failing direct structural checks."
        return report, diagnostic

    def _capture(
        self,
        project_dir: Path,
        toolchain: Toolchain,
        run_id: str,
        revision: int,
        project_sha256: str,
        log: LogSink,
    ) -> tuple[dict[str, Any] | None, list[Path], list[str], str]:
        revision_dir = self._revision_dir(project_dir, run_id, revision)
        manifest_path = revision_dir / "capture_manifest.json"
        manifest_path.unlink(missing_ok=True)
        environment = self._revision_environment(
            toolchain,
            run_id,
            revision,
            project_sha256,
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
        succeeded, diagnostic = self._run_logged(command, project_dir, environment, log)
        if not manifest_path.is_file():
            return None, [], [], diagnostic
        manifest = _read_json_object(manifest_path, "direct capture manifest")
        expected = {
            "schema": "prompt-to-play/direct-capture-manifest@1",
            "mode": "direct-project",
            "run_id": run_id,
            "revision": revision,
            "project_sha256": project_sha256,
        }
        if any(manifest.get(key) != value for key, value in expected.items()):
            raise PipelineIntegrationError(
                f"direct capture manifest identity mismatch: {manifest_path}"
            )
        manifest = copy.deepcopy(dict(manifest))
        manifest["host_process_succeeded"] = succeeded
        if not succeeded:
            manifest["status"] = "error"
            diagnostic += "\nGodot capture process did not exit cleanly."
        _write_json(manifest_path, manifest)
        captures = manifest.get("captures")
        if manifest.get("status") != "pass" or not isinstance(captures, list):
            return manifest, [], [], diagnostic
        project_root = project_dir.resolve(strict=True)
        absolute_paths: list[Path] = []
        relative_paths: list[str] = []
        for index, capture in enumerate(captures):
            if not isinstance(capture, Mapping) or not isinstance(
                capture.get("path"), str
            ):
                raise PipelineIntegrationError(
                    f"direct capture {index} has no relative path"
                )
            relative = PurePosixPath(capture["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise PipelineIntegrationError("capture path escapes the project")
            expected_parent = PurePosixPath(
                "artifacts", "runs", run_id, f"rev_{revision}", "captures"
            )
            if relative.parent != expected_parent:
                raise PipelineIntegrationError(
                    "direct capture path must stay in its revision capture directory"
                )
            image = project_root.joinpath(*relative.parts).resolve(strict=True)
            try:
                image.relative_to(project_root)
            except ValueError as exc:
                raise PipelineIntegrationError(
                    "capture path escapes the project"
                ) from exc
            if image.suffix.lower() != ".png":
                raise PipelineIntegrationError("direct capture must be a PNG")
            digest = capture.get("sha256")
            if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                raise PipelineIntegrationError("direct capture has an invalid hash")
            if _sha256_file(image) != digest:
                raise PipelineIntegrationError("direct capture hash mismatch")
            absolute_paths.append(image)
            relative_paths.append(relative.as_posix())
        if not 1 <= len(absolute_paths) <= 2:
            raise PipelineIntegrationError(
                "direct capture must contain one or two images"
            )
        return manifest, absolute_paths, relative_paths, diagnostic

    def _published_references(
        self, state: PipelineState, project_dir: Path
    ) -> list[Path]:
        result: list[Path] = []
        project_root = project_dir.resolve(strict=True)
        bindings: Sequence[ReferenceBinding] = state.require_result(REFERENCES_RESULT)
        for binding in bindings:
            path = project_root.joinpath(*PurePosixPath(binding.project_path).parts)
            resolved = path.resolve(strict=True)
            try:
                resolved.relative_to(project_root)
            except ValueError as exc:
                raise PipelineIntegrationError(
                    f"published reference escapes project: {binding.project_path}"
                ) from exc
            if _sha256_file(resolved) != binding.sha256:
                raise PipelineIntegrationError(
                    f"published reference hash changed: {binding.project_path}"
                )
            result.append(resolved)
        return result

    def _evaluate_visual(
        self,
        state: PipelineState,
        screenshots: Sequence[Path],
        references: Sequence[Path],
        project_manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not screenshots:
            return _host_visual_failure(
                "capture_missing", "可信宿主未获得可用于视觉评测的截图。"
            )
        if self.evaluate_visual is not None:
            document = self.evaluate_visual(
                state.request.prompt,
                screenshots,
                reference_image_paths=references,
                project_manifest=project_manifest,
            )
            return validate_direct_visual_feedback(
                document, min_scene_similarity=self.min_scene_similarity
            )
        evaluator = DirectVisualEvaluationAgent(
            self._runtime(state).agent(agents.AgentRole.VISUAL_EVALUATOR),
            min_scene_similarity=self.min_scene_similarity,
        )
        return evaluator.evaluate(
            state.request.prompt,
            screenshots,
            reference_image_paths=references,
            project_manifest=project_manifest,
        )

    def _repair(
        self,
        state: PipelineState,
        project_dir: Path,
        references: Sequence[Path],
        screenshots: Sequence[Path],
        diagnostics: str,
        structural: Mapping[str, Any],
        visual: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        current_files = _generated_files(project_dir)
        if self.repair_project is not None:
            return self.repair_project(
                state.request.prompt,
                current_files,
                diagnostics=diagnostics,
                structural_report=structural,
                visual_feedback=visual,
                screenshot_image_paths=screenshots,
                reference_image_paths=references,
            )
        repairer = CodeRepairAgent(
            self._runtime(state).agent(agents.AgentRole.CODE_REPAIR)
        )
        return repairer.repair(
            state.request.prompt,
            current_files,
            diagnostics=diagnostics,
            structural_report=structural,
            visual_feedback=visual,
            screenshot_image_paths=screenshots,
            reference_image_paths=references,
        )

    def _write_trace(self, state: PipelineState, path: Path, run_id: str) -> None:
        runtime = state.results.get(AGENT_RUNTIME_RESULT)
        writer = getattr(runtime, "write_trace", None)
        if callable(writer):
            writer(path, run_id=run_id)

    def _usage(self, state: PipelineState) -> Mapping[str, Any]:
        runtime = state.results.get(AGENT_RUNTIME_RESULT)
        aggregate = getattr(runtime, "aggregate_usage", None)
        if not callable(aggregate):
            return {
                "backend": "injected",
                "model": "injected",
                "calls": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "exact": False,
            }
        usage = aggregate()
        result = asdict(usage)
        result["total_tokens"] = usage.total_tokens
        return result

    def _evaluation(
        self,
        *,
        run_id: str,
        revision: int,
        project_sha256: str,
        build_passed: bool,
        structural: Mapping[str, Any],
        capture_passed: bool,
        screenshots: Sequence[str],
        visual: Mapping[str, Any],
        diagnostics: str,
        timing_ms: Mapping[str, int],
        usage: Mapping[str, Any],
    ) -> dict[str, Any]:
        structural_passed = _structural_passed(structural)
        visual_accepted = bool(visual.get("accepted"))
        passed = (
            build_passed and structural_passed and capture_passed and visual_accepted
        )
        return {
            "schema": DIRECT_EVALUATION_CONTRACT,
            "mode": "direct-project",
            "run_id": run_id,
            "revision": revision,
            "project_sha256": project_sha256,
            "checks": {
                "build": build_passed,
                "structural": structural_passed,
                "capture": capture_passed,
                "visual": visual_accepted,
            },
            "scene_similarity": float(visual.get("scene_similarity", 0.0)),
            "visual_feedback": copy.deepcopy(dict(visual)),
            "screenshots": list(screenshots),
            "diagnostics_tail": diagnostics[-20_000:],
            "timing_ms": dict(timing_ms),
            "token_usage": dict(usage),
            "result": {"passed": passed},
            "next_action": (
                "accept"
                if passed
                else "patch"
                if revision < self.max_correction_iterations
                else "stop_quality_gate"
            ),
            "finished_at_utc": _utc_timestamp(),
        }

    def build(self, state: PipelineState, log: LogSink) -> None:
        project_dir = Path(state.require_result(PROJECT_RESULT)).resolve(strict=True)
        request = state.require_result(REQUEST_RESULT)
        run_id = request["request_hash"][:12]
        references = self._published_references(state, project_dir)
        toolchain = discover_toolchain(
            self.source_repo_root,
            environment=self.environment,
            which=self.which,
        )
        state.set_result(TOOLCHAIN_RESULT, toolchain)
        current_manifest = copy.deepcopy(
            dict(state.require_result(PROJECT_MANIFEST_RESULT))
        )

        restore_started = time.perf_counter()
        restore_environment = self._revision_environment(
            toolchain,
            run_id,
            0,
            current_manifest["project_sha256"],
        )
        restore_command = (
            os.fspath(toolchain.dotnet_exe),
            "restore",
            "--source",
            os.fspath(toolchain.godot_nupkgs),
            "--ignore-failed-sources",
        )
        restored, restore_diagnostic = self._run_logged(
            restore_command, project_dir, restore_environment, log
        )
        _timings(state)["restore"] += _elapsed_ms(restore_started)
        if not restored:
            raise PipelineIntegrationError(
                "Godot .NET dependency restore failed; this is a host/toolchain error:\n"
                + restore_diagnostic[-4000:]
            )

        candidates: list[RevisionCandidate] = []
        reports: list[dict[str, Any]] = []
        manifests: dict[int, Mapping[str, Any]] = {}
        seen_project_hashes = {str(current_manifest["project_sha256"])}
        loop_stop_reason = "correction_limit"

        for revision in range(self.max_correction_iterations + 1):
            revision_dir = self._revision_dir(project_dir, run_id, revision)
            revision_dir.mkdir(parents=True, exist_ok=True)
            project_sha256 = str(current_manifest["project_sha256"])
            manifests[revision] = copy.deepcopy(current_manifest)
            _snapshot_generated(project_dir, revision_dir, project_sha256)

            build_started = time.perf_counter()
            environment = self._revision_environment(
                toolchain, run_id, revision, project_sha256
            )
            build_passed, build_diagnostic = self._run_logged(
                (os.fspath(toolchain.dotnet_exe), "build", "--no-restore"),
                project_dir,
                environment,
                log,
            )
            (revision_dir / "build.log").write_text(
                build_diagnostic + ("\n" if build_diagnostic else ""),
                encoding="utf-8",
            )
            _timings(state)["build"] += _elapsed_ms(build_started)

            structural: dict[str, Any]
            structural_diagnostic = ""
            screenshots: list[Path] = []
            screenshot_relatives: list[str] = []
            capture_diagnostic = ""
            capture_passed = False

            if build_passed:
                structural_started = time.perf_counter()
                structural, structural_diagnostic = self._run_structural(
                    project_dir,
                    toolchain,
                    run_id,
                    revision,
                    project_sha256,
                    log,
                )
                _timings(state)["structural"] += _elapsed_ms(structural_started)

                capture_started = time.perf_counter()
                (
                    _capture_manifest,
                    screenshots,
                    screenshot_relatives,
                    capture_diagnostic,
                ) = self._capture(
                    project_dir,
                    toolchain,
                    run_id,
                    revision,
                    project_sha256,
                    log,
                )
                _timings(state)["capture"] += _elapsed_ms(capture_started)
                capture_passed = bool(screenshots)
                # Capture launches a fresh game process whose harness also writes
                # the Godot-side report. Restore the Python-normalized report so
                # the host-owned process-exit check remains authoritative.
                _write_json(revision_dir / "direct_structural_report.json", structural)
            else:
                structural = {
                    "schema": "prompt-to-play/direct-structural-report@1",
                    "mode": "direct-project",
                    "run_id": run_id,
                    "revision": revision,
                    "project_sha256": project_sha256,
                    "status": "not_run",
                    "checks": [],
                    "summary": {"passed": 0, "failed": 0},
                }
                _write_json(revision_dir / "direct_structural_report.json", structural)

            evaluate_started = time.perf_counter()
            visual = self._evaluate_visual(
                state, screenshots, references, current_manifest
            )
            _timings(state)["evaluate"] += _elapsed_ms(evaluate_started)
            _write_json(revision_dir / "direct_visual_feedback.json", visual)

            diagnostics = "\n".join(
                value
                for value in (
                    build_diagnostic,
                    structural_diagnostic,
                    capture_diagnostic,
                )
                if value
            )[-40_000:]
            evaluation = self._evaluation(
                run_id=run_id,
                revision=revision,
                project_sha256=project_sha256,
                build_passed=build_passed,
                structural=structural,
                capture_passed=capture_passed,
                screenshots=screenshot_relatives,
                visual=visual,
                diagnostics=diagnostics,
                timing_ms=_timings(state),
                usage=self._usage(state),
            )
            evaluation_path = revision_dir / "direct_evaluation.json"
            _write_json(evaluation_path, evaluation)
            reports.append(evaluation)
            candidate = RevisionCandidate(
                revision=revision,
                project_sha256=project_sha256,
                build_passed=build_passed,
                structural_passed=_structural_passed(structural),
                capture_passed=capture_passed,
                scene_similarity=float(visual.get("scene_similarity", 0.0)),
                accepted=bool(evaluation["result"]["passed"]),
                evaluation_path=(f"rev_{revision}/direct_evaluation.json"),
            )
            candidates.append(candidate)
            log(
                f"修订 {revision}：编译={'通过' if build_passed else '失败'}，"
                f"结构={'通过' if candidate.structural_passed else '失败'}，"
                f"视觉相似度={candidate.scene_similarity:.2f}。"
            )
            self._write_trace(
                state,
                project_dir / "artifacts" / "runs" / run_id / "agent_trace.json",
                run_id,
            )

            if candidate.accepted:
                loop_stop_reason = "quality_gate_passed"
                break
            if revision >= self.max_correction_iterations:
                break

            repair_started = time.perf_counter()
            patch_document = self._repair(
                state,
                project_dir,
                references,
                screenshots,
                diagnostics,
                structural,
                visual,
            )
            patch_plan = parse_file_plan(patch_document)
            _assert_required_entry(patch_plan, initial=False)
            patch_path = revision_dir / f"patch_to_rev_{revision + 1}.json"
            _write_json(patch_path, patch_document)
            next_manifest_path = (
                f"artifacts/runs/{run_id}/rev_{revision + 1}/"
                "direct_generation_manifest.json"
            )
            current_manifest = copy.deepcopy(
                dict(
                    apply_file_plan(
                        project_dir,
                        patch_document,
                        manifest_path=next_manifest_path,
                    )
                )
            )
            next_project_sha256 = str(current_manifest["project_sha256"])
            if next_project_sha256 in seen_project_hashes:
                loop_stop_reason = (
                    "repair_no_content_change"
                    if next_project_sha256 == project_sha256
                    else "repair_revision_cycle"
                )
                log("CodeRepairAgent 未产生新的项目状态；停止重复循环并保留最佳证据。")
                break
            seen_project_hashes.add(next_project_sha256)
            if not (project_dir / "generated" / "GeneratedGame.tscn").is_file():
                raise PipelineIntegrationError(
                    "repair removed generated/GeneratedGame.tscn"
                )
            _timings(state)["repair"] += _elapsed_ms(repair_started)
            log(f"CodeRepairAgent：已直接修改 generated/**，进入修订 {revision + 1}。")

        selected = max(candidates, key=_candidate_key)
        selection = {
            "schema": DIRECT_SELECTION_CONTRACT,
            "strategy": "accepted-first-playable-then-visual-score@2",
            "run_id": run_id,
            "stop_reason": loop_stop_reason,
            "selected_revision": selected.revision,
            "selected_project_sha256": selected.project_sha256,
            "candidates": [
                asdict(candidate) | {"playable": candidate.playable}
                for candidate in candidates
            ],
        }
        selection_path = project_dir / "artifacts" / "runs" / run_id / "selection.json"
        _write_json(selection_path, selection)
        self._write_trace(
            state,
            project_dir / "artifacts" / "runs" / run_id / "agent_trace.json",
            run_id,
        )
        if not selected.playable:
            state.set_result(EVALUATIONS_RESULT, reports)
            raise PipelineIntegrationError(
                "自动修复次数内没有得到可编译且通过结构检查的游戏；"
                f"诊断已保存在 {selection_path}"
            )

        selected_dir = self._revision_dir(project_dir, run_id, selected.revision)
        current_project_sha256, _current_files = _generated_digest(project_dir)
        if current_project_sha256 != selected.project_sha256:
            _restore_generated(project_dir, selected_dir, selected.project_sha256)
            environment = self._revision_environment(
                toolchain,
                run_id,
                selected.revision,
                selected.project_sha256,
            )
            rebuilt, diagnostic = self._run_logged(
                (os.fspath(toolchain.dotnet_exe), "build", "--no-restore"),
                project_dir,
                environment,
                log,
            )
            if not rebuilt:
                raise PipelineIntegrationError(
                    "restored best revision failed its verification rebuild:\n"
                    + diagnostic[-4000:]
                )

        selected_structural = _read_json_object(
            selected_dir / "direct_structural_report.json",
            "selected direct structural report",
        )
        state.set_result(
            PROJECT_MANIFEST_RESULT, copy.deepcopy(dict(manifests[selected.revision]))
        )
        state.set_result(STRUCTURAL_REPORT_RESULT, selected_structural)
        state.set_result(EVALUATIONS_RESULT, reports)
        state.set_result(SELECTED_REVISION_RESULT, selected.revision)
        if not selected.accepted:
            raise PipelineIntegrationError(
                "自动闭环未达到视觉与交互质量门槛，已停止而不是把未达标版本标记为完成；"
                f"最佳可玩修订 {selected.revision} 保存在 {project_dir}，"
                f"证据保存在 {selection_path}"
            )
        log(f"质量门槛通过，已选择修订 {selected.revision}；项目保留在 {project_dir}。")

    def launch(self, state: PipelineState, log: LogSink) -> None:
        project_dir = Path(state.require_result(PROJECT_RESULT)).resolve(strict=True)
        toolchain: Toolchain = state.require_result(TOOLCHAIN_RESULT)
        request = state.require_result(REQUEST_RESULT)
        manifest = state.require_result(PROJECT_MANIFEST_RESULT)
        run_id = request["request_hash"][:12]
        revision = int(state.require_result(SELECTED_REVISION_RESULT))
        environment = self._revision_environment(
            toolchain,
            run_id,
            revision,
            manifest["project_sha256"],
        )
        environment.pop("PTP_AUTOMATION", None)
        environment.pop("PTP_CAPTURE", None)
        command = (
            os.fspath(toolchain.visible_exe),
            "--path",
            os.fspath(project_dir),
            "--rendering-method",
            "gl_compatibility",
            "--audio-driver",
            "Dummy",
        )
        play_log = self._revision_dir(project_dir, run_id, revision) / "play.log"
        process = self.start_process(command, project_dir, environment, play_log)
        state.set_result(PROCESS_RESULT, process)
        return_code = _probe_process(process, self.launch_probe_seconds)
        if return_code is not None:
            tail = _read_log_tail(play_log)
            detail = f"\n{tail}" if tail else ""
            raise PipelineIntegrationError(
                f"Godot exited during startup with code {return_code}; "
                f"log: {play_log}{detail}"
            )
        pid = getattr(process, "pid", "unknown")
        log(f"Godot 已启动（直接生成修订 {revision}，PID {pid}）：{project_dir}")


def create_pipeline_commands(
    dependencies: DirectPipelineDependencies | None = None,
) -> PipelineCommands:
    return DirectPipelineStages(dependencies).commands()


def main() -> int:
    return run_launcher(create_pipeline_commands())


__all__ = [
    "AGENT_RUNTIME_RESULT",
    "DIRECT_EVALUATION_CONTRACT",
    "DIRECT_SELECTION_CONTRACT",
    "DirectPipelineDependencies",
    "DirectPipelineStages",
    "EVALUATIONS_RESULT",
    "FILE_PLAN_RESULT",
    "PROCESS_RESULT",
    "PROJECT_MANIFEST_RESULT",
    "PROJECT_RESULT",
    "REFERENCES_RESULT",
    "REQUEST_RESULT",
    "SELECTED_REVISION_RESULT",
    "STRUCTURAL_REPORT_RESULT",
    "TIMING_RESULT",
    "TOOLCHAIN_RESULT",
    "create_pipeline_commands",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
