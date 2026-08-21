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
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Protocol, Sequence

try:  # Support package imports and direct local execution.
    from . import contracts, lifecycle, planner
    from .launcher import LogSink, PipelineCommands, PipelineState, run_launcher
except ImportError:  # pragma: no cover - direct script execution only
    import contracts
    import lifecycle
    import planner
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
        log(f"已规划 WorldSpec：{world.get('world_id', '<unknown>')}")

    def validate(self, state: PipelineState, log: LogSink) -> None:
        world = state.require_result(WORLD_RESULT)
        self.validate_world(world)
        log(f"WorldSpec 校验通过：{contracts.document_sha256(world)}")

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
        state.set_result(WORLD_RESULT, world)
        log(f"项目已发布：{project_dir}")
        log(f"WorldSpec 已写入：{spec_path}")

    def build(self, state: PipelineState, log: LogSink) -> None:
        project_dir = Path(state.require_result(PROJECT_RESULT)).resolve(strict=True)
        toolchain = discover_toolchain(
            self.source_repo_root,
            environment=self.environment,
            which=self.which,
        )
        state.set_result(TOOLCHAIN_RESULT, toolchain)
        run_environment = _child_process_environment(self.environment)
        request_document = state.require_result(REQUEST_RESULT)
        run_id = request_document["request_hash"][:12]
        run_environment.update(
            {
                "DOTNET_ROOT": os.fspath(toolchain.dotnet_exe.parent),
                "DOTNET_CLI_UI_LANGUAGE": "en-US",
                "VSLANG": "1033",
                "PTP_RUN_ID": run_id,
                "PTP_REVISION": "0",
            }
        )
        run_environment.pop("PTP_CAPTURE", None)

        commands = (
            (
                os.fspath(toolchain.dotnet_exe),
                "restore",
                "--source",
                os.fspath(toolchain.godot_nupkgs),
                "--ignore-failed-sources",
            ),
            (os.fspath(toolchain.dotnet_exe), "build", "--no-restore"),
            (
                os.fspath(toolchain.godot_exe),
                "--headless",
                "--path",
                os.fspath(project_dir),
                "--quit-after",
                "120",
            ),
        )
        for command in commands:
            self.run_command(command, project_dir, run_environment, log)

        report_path = (
            project_dir
            / "artifacts"
            / "runs"
            / run_id
            / "rev_0"
            / "structural_report.json"
        )
        if not report_path.is_file():
            raise PipelineIntegrationError(
                f"Godot did not write the structural report: {report_path}"
            )
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelineIntegrationError(
                f"cannot read structural report: {exc}"
            ) from exc
        if not isinstance(report, dict) or report.get("status") != "pass":
            status = report.get("status") if isinstance(report, dict) else "invalid"
            details: list[str] = []
            if isinstance(report, dict):
                checks = report.get("checks")
                if isinstance(checks, list):
                    for check in checks:
                        if isinstance(check, dict) and check.get("passed") is False:
                            check_id = check.get("id", "unknown_check")
                            message = check.get("message", "failed")
                            details.append(f"{check_id}: {message}")
                issues = report.get("issues")
                if isinstance(issues, list):
                    details.extend(str(issue) for issue in issues[:3])
            summary = "; ".join(details[:6]) or "no failure details were reported"
            raise PipelineIntegrationError(
                f"Godot structural checks did not pass ({status}): {summary}; "
                f"report: {report_path}"
            )
        state.set_result(STRUCTURAL_REPORT_RESULT, report)
        log(f"结构检查通过：{report_path}")

    def launch(self, state: PipelineState, log: LogSink) -> None:
        project_dir = Path(state.require_result(PROJECT_RESULT)).resolve(strict=True)
        toolchain: Toolchain = state.require_result(TOOLCHAIN_RESULT)
        request_document = state.require_result(REQUEST_RESULT)
        run_id = request_document["request_hash"][:12]
        run_environment = _child_process_environment(self.environment)
        run_environment.update(
            {
                "DOTNET_ROOT": os.fspath(toolchain.dotnet_exe.parent),
                "DOTNET_CLI_UI_LANGUAGE": "en-US",
                "VSLANG": "1033",
                "PTP_RUN_ID": run_id,
                "PTP_REVISION": "0",
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
        play_log = project_dir / "artifacts" / "runs" / run_id / "rev_0" / "play.log"
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
        log(f"Godot 已启动（PID {pid}）：{project_dir}")
        log(f"运行日志：{play_log}")


def create_pipeline_commands(
    dependencies: PipelineDependencies | None = None,
) -> PipelineCommands:
    return PipelineStages(dependencies).commands()


def main() -> int:
    return run_launcher(create_pipeline_commands())


if __name__ == "__main__":
    raise SystemExit(main())
