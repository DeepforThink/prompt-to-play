"""Trusted host/toolchain helpers for the WorldSpec-free direct runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from .direct_common import canonical_json_bytes


SOURCE_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMMAND_TIMEOUT_SECONDS = 240.0
REFERENCE_DIRECTORY = PurePosixPath("references")
REFERENCE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})


class PipelineIntegrationError(RuntimeError):
    """Raised when trusted local execution cannot satisfy its contract."""


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


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise PipelineIntegrationError(f"cannot hash file {path}: {exc}") from exc
    return digest.hexdigest()


def stable_references(
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
        digest = descriptor["sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise PipelineIntegrationError(
                f"request reference {index} has an invalid SHA-256"
            )
        if sha256_file(source) != digest:
            raise PipelineIntegrationError(
                f"request reference {index} content changed after binding"
            )
        relative = REFERENCE_DIRECTORY / f"{index:02d}_{digest[:16]}{suffix}"
        bindings.append(
            ReferenceBinding(
                source=source.resolve(strict=True),
                project_path=relative.as_posix(),
                sha256=digest,
            )
        )
    return tuple(bindings)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: str | Path, document: Any) -> Path:
    output = Path(path)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(canonical_json_bytes(document) + b"\n")
    except OSError as exc:
        raise PipelineIntegrationError(
            f"cannot write JSON artifact {output}: {exc}"
        ) from exc
    return output


def read_json_object(path: str | Path, label: str) -> dict[str, Any]:
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PipelineIntegrationError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineIntegrationError(f"{label} must contain a JSON object: {source}")
    return value


def read_log_tail(path: str | Path, maximum_bytes: int = 32_768) -> str:
    if maximum_bytes <= 0:
        return ""
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - maximum_bytes))
            return handle.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def child_process_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Copy runtime essentials while withholding API keys and unrelated secrets."""

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


def run_command(
    argv: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    log: Callable[[str], None],
) -> None:
    command = [os.fspath(value) for value in argv]
    if not command:
        raise PipelineIntegrationError("cannot run an empty command")
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
    except OSError as exc:
        raise PipelineIntegrationError(
            f"cannot run command {command[0]}: {exc}"
        ) from exc
    if completed.stdout and completed.stdout.strip():
        log(completed.stdout.rstrip())
    if completed.returncode != 0:
        raise PipelineIntegrationError(
            f"command exited with {completed.returncode}: {command[0]}"
        )


def start_process(
    argv: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    log_path: Path,
) -> subprocess.Popen[bytes]:
    command = [os.fspath(value) for value in argv]
    if not command:
        raise PipelineIntegrationError("cannot start an empty command")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    creation_flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    )
    try:
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
    except OSError as exc:
        raise PipelineIntegrationError(
            f"cannot start process {command[0]}: {exc}"
        ) from exc


def probe_process(process: Any, timeout_seconds: float) -> int | None:
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


def _tool_roots(source_repo_root: Path) -> tuple[Path, ...]:
    candidates = (source_repo_root / ".tools", source_repo_root.parent / ".tools")
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
    """Resolve configured tools, both workspace-local .tools roots, then PATH."""

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
            roots, ("Godot*_console.exe", "Godot*.exe", "godot.exe", "godot")
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
    return Toolchain(dotnet, godot_console, nupkgs, godot_visible)


__all__ = [
    "DEFAULT_COMMAND_TIMEOUT_SECONDS",
    "PipelineIntegrationError",
    "ReferenceBinding",
    "SOURCE_REPO_ROOT",
    "Toolchain",
    "child_process_environment",
    "discover_toolchain",
    "probe_process",
    "read_json_object",
    "read_log_tail",
    "run_command",
    "sha256_file",
    "stable_references",
    "start_process",
    "utc_timestamp",
    "write_json",
]
