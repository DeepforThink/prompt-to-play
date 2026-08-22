"""Deterministic AssetAgent for Prompt-to-Play.

The agent converts semantic prefab references in a validated ``WorldSpec`` into
an explicit asset catalog.  It may reuse a content-addressed cache or, when the
host opts in and provides both required credentials, call the repository's
two-stage image -> GLB tool.  Primitive geometry remains the runtime fallback;
an unresolved entry is never represented as a successful generated asset.

This module intentionally owns no UI controls.  Paid generation is controlled
only by host environment policy:

``PROMPT_TO_PLAY_ASSET_MODE``
    ``off`` (default), ``auto``, or ``required``.
``PROMPT_TO_PLAY_ASSET_MAX_GENERATIONS``
    System-owned per-run API attempt budget (default 3, hard-capped at 12).
``PROMPT_TO_PLAY_ASSET_MAX_WORKERS``
    Maximum concurrent unique-prefab generations (default and hard cap 4).
``PROMPT_TO_PLAY_ASSET_IMAGE_MODEL``
    Optional ``grok`` or ``gemini`` override.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from prompt_to_play import contracts


CATALOG_SCHEMA = "prompt-to-play/asset-catalog@1"
MANIFEST_SCHEMA = "prompt-to-play/asset-manifest@1"
AGENT_NAME = "AssetAgent"
AGENT_ROLE = "resolve-or-generate-semantic-3d-assets"
AGENT_VERSION = "1"
REQUEST_RECIPE_VERSION = "prompt-to-play/asset-request@1"

ASSET_MODE_ENV = "PROMPT_TO_PLAY_ASSET_MODE"
ASSET_MAX_GENERATIONS_ENV = "PROMPT_TO_PLAY_ASSET_MAX_GENERATIONS"
ASSET_MAX_WORKERS_ENV = "PROMPT_TO_PLAY_ASSET_MAX_WORKERS"
ASSET_IMAGE_MODEL_ENV = "PROMPT_TO_PLAY_ASSET_IMAGE_MODEL"
ASSET_PYTHON_ENV = "PROMPT_TO_PLAY_ASSET_PYTHON"

DEFAULT_ASSET_MODE = "off"
DEFAULT_MAX_GENERATIONS = 3
DEFAULT_MAX_WORKERS = 4
HARD_MAX_GENERATIONS = 12
HARD_MAX_WORKERS = 4
VALID_ASSET_MODES = frozenset({"off", "auto", "required"})
VALID_IMAGE_MODELS = frozenset({"grok", "gemini"})

_ROLE_ORDER = {"building": 0, "interactable": 1, "prop": 2}
_SAFE_CHILD_ENVIRONMENT = frozenset(
    {
        "ALL_PROXY",
        "CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LOCALAPPDATA",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "PYTHONPATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
)
_CREDENTIAL_KEYS = frozenset(
    {"XAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "TRIPO3D_API_KEY"}
)


class AssetConfigurationError(ValueError):
    """Raised when host-owned asset generation settings are invalid."""


class AssetResolutionError(RuntimeError):
    """Raised after catalog emission when ``required`` mode is unresolved."""

    def __init__(self, manifest_path: Path, unresolved_prefabs: Sequence[str]) -> None:
        self.manifest_path = manifest_path
        self.unresolved_prefabs = tuple(unresolved_prefabs)
        joined = ", ".join(self.unresolved_prefabs)
        super().__init__(
            f"AssetAgent required mode could not resolve {len(self.unresolved_prefabs)} "
            f"asset(s): {joined}; see {manifest_path}"
        )


@dataclass(frozen=True)
class AssetCommandResult:
    """Sanitized process result returned by an injectable command runner."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class AssetCommandRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        environment: Mapping[str, str],
    ) -> AssetCommandResult: ...


LogSink = Callable[[str], None]


@dataclass(frozen=True)
class AssetAgentConfig:
    """Host-owned policy; no field is derived from the user's prompt."""

    mode: str
    max_generations: int
    max_workers: int
    image_model: str
    python_executable: str

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "AssetAgentConfig":
        env = os.environ if environment is None else environment
        mode = env.get(ASSET_MODE_ENV, DEFAULT_ASSET_MODE).strip().lower()
        if mode not in VALID_ASSET_MODES:
            raise AssetConfigurationError(
                f"{ASSET_MODE_ENV} must be 'off', 'auto', or 'required'"
            )

        raw_max = env.get(
            ASSET_MAX_GENERATIONS_ENV, str(DEFAULT_MAX_GENERATIONS)
        ).strip()
        try:
            max_generations = int(raw_max)
        except ValueError as exc:
            raise AssetConfigurationError(
                f"{ASSET_MAX_GENERATIONS_ENV} must be an integer"
            ) from exc
        if not 0 <= max_generations <= HARD_MAX_GENERATIONS:
            raise AssetConfigurationError(
                f"{ASSET_MAX_GENERATIONS_ENV} must be between 0 and "
                f"{HARD_MAX_GENERATIONS}"
            )

        raw_workers = env.get(
            ASSET_MAX_WORKERS_ENV, str(DEFAULT_MAX_WORKERS)
        ).strip()
        try:
            max_workers = int(raw_workers)
        except ValueError as exc:
            raise AssetConfigurationError(
                f"{ASSET_MAX_WORKERS_ENV} must be an integer"
            ) from exc
        if not 1 <= max_workers <= HARD_MAX_WORKERS:
            raise AssetConfigurationError(
                f"{ASSET_MAX_WORKERS_ENV} must be between 1 and "
                f"{HARD_MAX_WORKERS}"
            )

        configured_model = env.get(ASSET_IMAGE_MODEL_ENV, "").strip().lower()
        if configured_model and configured_model not in VALID_IMAGE_MODELS:
            raise AssetConfigurationError(
                f"{ASSET_IMAGE_MODEL_ENV} must be 'grok' or 'gemini'"
            )
        image_model = configured_model or _select_image_model(env)

        python_executable = env.get(ASSET_PYTHON_ENV, sys.executable).strip()
        if not python_executable:
            raise AssetConfigurationError(f"{ASSET_PYTHON_ENV} must not be empty")
        return cls(
            mode=mode,
            max_generations=max_generations,
            max_workers=max_workers,
            image_model=image_model,
            python_executable=python_executable,
        )


@dataclass(frozen=True)
class AssetRequest:
    """One unique semantic prefab request shared by every matching entity."""

    prefab: str
    roles: tuple[str, ...]
    prompt: str
    cache_key: str
    priority: int


@dataclass(frozen=True)
class _AssetWorkItem:
    request: AssetRequest
    action: str
    model_path: Path | None = None
    error: str | None = None
    attempt: int | None = None


@dataclass(frozen=True)
class AssetAgentResult:
    catalog_path: Path
    manifest_path: Path
    catalog: Mapping[str, Any]
    manifest: Mapping[str, Any]
    attempted_generations: int
    generated_assets: int
    cache_hits: int


def _select_image_model(environment: Mapping[str, str]) -> str:
    if environment.get("XAI_API_KEY", "").strip():
        return "grok"
    if (
        environment.get("GEMINI_API_KEY", "").strip()
        or environment.get("GOOGLE_API_KEY", "").strip()
    ):
        return "gemini"
    return "grok"


def _semantic_name(prefab: str) -> str:
    text = prefab.replace("/", " ").replace("_", " ").replace("-", " ")
    return " ".join(text.split())


def _asset_prompt(
    prefab: str,
    roles: Sequence[str],
    theme: str,
    interaction_cues: Sequence[str],
) -> str:
    role_text = ", ".join(roles)
    cue_text = ""
    if interaction_cues:
        cue_text = " Interaction semantics: " + "; ".join(interaction_cues) + "."
    return (
        "Create one realistic, game-ready 3D asset reference image for "
        "image-to-3D conversion. "
        f"Subject: {_semantic_name(prefab)} (logical prefab: {prefab}). "
        f"Usage roles: {role_text}. World art direction: {theme}."
        f"{cue_text} Show exactly one complete object, centered in a three-quarter "
        "elevated view on a solid light-gray background. Use physically plausible "
        "materials, coherent scale, clean silhouette, no text, no people, no border, "
        "and no surrounding scene."
    )


def derive_asset_requests(
    world_spec: Mapping[str, Any], *, image_model: str = "grok"
) -> tuple[AssetRequest, ...]:
    """Validate a WorldSpec and return stable, semantically deduplicated requests."""

    contracts.validate_world(world_spec)
    if image_model not in VALID_IMAGE_MODELS:
        raise AssetConfigurationError("image_model must be 'grok' or 'gemini'")

    observations: dict[str, dict[str, set[str]]] = {}

    def observe(prefab: str, role: str, cue: str | None = None) -> None:
        record = observations.setdefault(prefab, {"roles": set(), "cues": set()})
        record["roles"].add(role)
        if cue:
            record["cues"].add(cue)

    for building in world_spec["buildings"]:
        observe(building["prefab"], "building")
    for interactable in world_spec["interactions"]["interactables"]:
        cue = f"{interactable['action']}: {interactable['label']}"
        observe(interactable["prefab"], "interactable", cue)
    for prop in world_spec["props"]:
        observe(prop["prefab"], "prop")

    theme = world_spec["style"]["theme"]
    requests: list[AssetRequest] = []
    for prefab, observation in observations.items():
        roles = tuple(sorted(observation["roles"], key=_ROLE_ORDER.__getitem__))
        cues = tuple(sorted(observation["cues"]))
        prompt = _asset_prompt(prefab, roles, theme, cues)
        recipe = {
            "schema": REQUEST_RECIPE_VERSION,
            "prefab": prefab,
            "roles": list(roles),
            "prompt": prompt,
            "image_model": image_model,
            "image_size": "1K",
            "aspect_ratio": "1:1",
            "glb_quality": "default",
            "pbr": True,
        }
        cache_key = contracts.document_sha256(recipe)
        priority = 0 if {"building", "interactable"} & set(roles) else 1
        requests.append(
            AssetRequest(
                prefab=prefab,
                roles=roles,
                prompt=prompt,
                cache_key=cache_key,
                priority=priority,
            )
        )
    return tuple(sorted(requests, key=lambda request: (request.priority, request.prefab)))


def _default_command_runner(
    argv: Sequence[str], cwd: Path, environment: Mapping[str, str]
) -> AssetCommandResult:
    try:
        completed = subprocess.run(
            [os.fspath(value) for value in argv],
            cwd=os.fspath(cwd),
            env=dict(environment),
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20 * 60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return AssetCommandResult(returncode=124, stderr=f"command timed out: {exc}")
    return AssetCommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _child_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Pass runtime essentials and only the credentials this agent owns."""

    allowed = _SAFE_CHILD_ENVIRONMENT | _CREDENTIAL_KEYS
    return {
        key.upper(): value
        for key, value in environment.items()
        if key.upper() in allowed and isinstance(value, str)
    }


def _secret_values(environment: Mapping[str, str]) -> tuple[str, ...]:
    values = {
        environment.get(key, "").strip()
        for key in _CREDENTIAL_KEYS
        if environment.get(key, "").strip()
    }
    return tuple(sorted(values, key=len, reverse=True))


def _redact(message: Any, environment: Mapping[str, str]) -> str:
    text = str(message)
    for value in _secret_values(environment):
        text = text.replace(value, "[REDACTED]")
    return text


def _image_credential_name(config: AssetAgentConfig, environment: Mapping[str, str]) -> str | None:
    if config.image_model == "grok":
        return "XAI_API_KEY" if environment.get("XAI_API_KEY", "").strip() else None
    if environment.get("GEMINI_API_KEY", "").strip():
        return "GEMINI_API_KEY"
    if environment.get("GOOGLE_API_KEY", "").strip():
        return "GOOGLE_API_KEY"
    return None


def _missing_credentials(
    config: AssetAgentConfig, environment: Mapping[str, str]
) -> tuple[str, ...]:
    missing: list[str] = []
    if _image_credential_name(config, environment) is None:
        missing.append("XAI_API_KEY" if config.image_model == "grok" else "GEMINI_API_KEY/GOOGLE_API_KEY")
    if not environment.get("TRIPO3D_API_KEY", "").strip():
        missing.append("TRIPO3D_API_KEY")
    return tuple(missing)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _usable_glb(path: Path) -> bool:
    try:
        if path.stat().st_size < 12:
            return False
        with path.open("rb") as handle:
            return handle.read(4) == b"glTF"
    except OSError:
        return False


def _usable_png(path: Path) -> bool:
    try:
        if path.stat().st_size < 8:
            return False
        with path.open("rb") as handle:
            return handle.read(8) == b"\x89PNG\r\n\x1a\n"
    except OSError:
        return False


def _result_error(result: AssetCommandResult) -> str:
    for line in reversed(result.stdout.splitlines()):
        try:
            document = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(document, dict) and isinstance(document.get("error"), str):
            return document["error"]
    tail = result.stderr.strip().splitlines()
    if tail:
        return tail[-1]
    return f"asset_gen.py exited with {result.returncode}"


def _run_checked(
    runner: AssetCommandRunner,
    argv: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
) -> None:
    result = runner(argv, cwd, environment)
    if not isinstance(result, AssetCommandResult):
        raise TypeError("asset command runner must return AssetCommandResult")
    if result.returncode != 0:
        raise RuntimeError(_result_error(result))


def _generate_to_cache(
    request: AssetRequest,
    *,
    config: AssetAgentConfig,
    source_repo_root: Path,
    cache_root: Path,
    environment: Mapping[str, str],
    runner: AssetCommandRunner,
) -> Path:
    tool = source_repo_root / "asset-gen" / "tools" / "asset_gen.py"
    if not tool.is_file():
        raise FileNotFoundError(f"asset generation tool not found: {tool}")

    request_cache = cache_root / request.cache_key
    request_cache.mkdir(parents=True, exist_ok=True)
    image_path = request_cache / "reference.png"
    model_path = request_cache / "model.glb"
    child_env = _child_environment(environment)

    if not _usable_png(image_path):
        image_command = [
            config.python_executable,
            os.fspath(tool),
            "image",
            "--prompt",
            request.prompt,
            "--model",
            config.image_model,
            "--size",
            "1K",
            "--aspect-ratio",
            "1:1",
            "-o",
            os.fspath(image_path),
        ]
        _run_checked(runner, image_command, source_repo_root, child_env)
        if not _usable_png(image_path):
            raise RuntimeError("image stage reported success but produced no valid PNG")

    sidecar = model_path.with_suffix(model_path.suffix + ".tripo.json")
    if sidecar.is_file() and not _usable_glb(model_path):
        model_command = [
            config.python_executable,
            os.fspath(tool),
            "resume",
            "-o",
            os.fspath(model_path),
        ]
    else:
        model_command = [
            config.python_executable,
            os.fspath(tool),
            "glb",
            "--image",
            os.fspath(image_path),
            "--quality",
            "default",
            "-o",
            os.fspath(model_path),
        ]
    _run_checked(runner, model_command, source_repo_root, child_env)
    if not _usable_glb(model_path):
        raise RuntimeError("GLB stage reported success but produced no valid GLB")
    return model_path


def _publish_cached_asset(model_path: Path, project_root: Path, cache_key: str) -> tuple[str, str]:
    relative = Path("assets") / "generated" / f"{cache_key}.glb"
    destination = project_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(model_path, destination)
    source_hash = _sha256_file(model_path)
    if _sha256_file(destination) != source_hash:
        raise OSError("published GLB hash does not match the cached source")
    return "res://" + relative.as_posix(), source_hash


def _catalog_entry(
    request: AssetRequest,
    *,
    scene_path: str | None,
    sha256: str | None,
    source: str,
    status: str,
    error: str | None,
) -> dict[str, Any]:
    # ``prefab`` and ``scene_path`` are the exact keys consumed by PrefabResolver.
    return {
        "prefab": request.prefab,
        "scene_path": scene_path,
        "sha256": sha256,
        "source": source,
        "status": status,
        "roles": list(request.roles),
        "prompt": request.prompt,
        "cache_key": request.cache_key,
        "error": error,
    }


def _plan_asset_work(
    requests: Sequence[AssetRequest],
    *,
    config: AssetAgentConfig,
    cache_root: Path,
    missing_credentials: Sequence[str],
) -> tuple[_AssetWorkItem, ...]:
    """Classify requests and reserve the paid-attempt budget in stable order."""

    work: list[_AssetWorkItem] = []
    reserved = 0
    for request in requests:
        cached_model = cache_root / request.cache_key / "model.glb"
        if _usable_glb(cached_model):
            work.append(
                _AssetWorkItem(request, "publish-cache", model_path=cached_model)
            )
        elif config.mode == "off":
            work.append(
                _AssetWorkItem(
                    request,
                    "unresolved",
                    error="asset API mode is off and no cached GLB exists",
                )
            )
        elif missing_credentials:
            work.append(
                _AssetWorkItem(
                    request,
                    "unresolved",
                    error="missing credentials: " + ", ".join(missing_credentials),
                )
            )
        elif reserved < config.max_generations:
            reserved += 1
            work.append(_AssetWorkItem(request, "generate", attempt=reserved))
        else:
            work.append(
                _AssetWorkItem(
                    request,
                    "unresolved",
                    error=(
                        "system API generation limit reached "
                        f"({config.max_generations})"
                    ),
                )
            )
    return tuple(work)


class AssetAgent:
    """Resolve semantic asset requests and emit the PrefabResolver catalog."""

    def __init__(
        self,
        *,
        source_repo_root: str | Path,
        cache_root: str | Path,
        environment: Mapping[str, str] | None = None,
        command_runner: AssetCommandRunner | None = None,
        log: LogSink | None = None,
    ) -> None:
        self.source_repo_root = Path(source_repo_root).resolve(strict=False)
        self.cache_root = Path(cache_root).resolve(strict=False)
        self.environment = dict(
            os.environ if environment is None else environment
        )
        self.config = AssetAgentConfig.from_environment(self.environment)
        self.command_runner = command_runner or _default_command_runner
        self.log = log or (lambda _message: None)

    def run(
        self, world_spec: Mapping[str, Any], project_root: str | Path
    ) -> AssetAgentResult:
        project = Path(project_root).resolve(strict=False)
        project.mkdir(parents=True, exist_ok=True)
        requests = derive_asset_requests(
            world_spec, image_model=self.config.image_model
        )
        missing_credentials = _missing_credentials(self.config, self.environment)
        work = _plan_asset_work(
            requests,
            config=self.config,
            cache_root=self.cache_root,
            missing_credentials=missing_credentials,
        )
        attempted = sum(item.action == "generate" for item in work)
        generated = 0
        cache_hits = 0
        entries: list[dict[str, Any]] = []

        generation_futures: dict[str, Future[Path]] = {}
        generation_work = [item for item in work if item.action == "generate"]
        if generation_work:
            with ThreadPoolExecutor(
                max_workers=min(self.config.max_workers, len(generation_work)),
                thread_name_prefix="asset-subagent",
            ) as executor:
                for item in generation_work:
                    request = item.request
                    self.log(
                        f"AssetAgent API request {item.attempt}/"
                        f"{self.config.max_generations}: {request.prefab}"
                    )
                    generation_futures[request.cache_key] = executor.submit(
                        _generate_to_cache,
                        request,
                        config=self.config,
                        source_repo_root=self.source_repo_root,
                        cache_root=self.cache_root,
                        environment=self.environment,
                        runner=self.command_runner,
                    )

        # Workers only populate isolated content-cache directories. Publishing,
        # counters, logging, and catalog construction remain host-owned and stable.
        for item in work:
            request = item.request
            if item.action == "publish-cache":
                if item.model_path is None:
                    raise RuntimeError("cached asset work item has no model path")
                try:
                    scene_path, digest = _publish_cached_asset(
                        item.model_path, project, request.cache_key
                    )
                    entries.append(
                        _catalog_entry(
                            request,
                            scene_path=scene_path,
                            sha256=digest,
                            source="content-cache",
                            status="cached",
                            error=None,
                        )
                    )
                    cache_hits += 1
                    self.log(f"AssetAgent cache hit: {request.prefab}")
                except Exception as exc:  # catalog every per-asset failure
                    entries.append(
                        _catalog_entry(
                            request,
                            scene_path=None,
                            sha256=None,
                            source="content-cache",
                            status="failed",
                            error=_redact(exc, self.environment),
                        )
                    )
                continue

            if item.action == "unresolved":
                entries.append(
                    _catalog_entry(
                        request,
                        scene_path=None,
                        sha256=None,
                        source="none",
                        status="unresolved",
                        error=item.error,
                    )
                )
                continue

            try:
                generated_model = generation_futures[request.cache_key].result()
                scene_path, digest = _publish_cached_asset(
                    generated_model, project, request.cache_key
                )
                entries.append(
                    _catalog_entry(
                        request,
                        scene_path=scene_path,
                        sha256=digest,
                        source="asset-gen:image+tripo3d",
                        status="generated",
                        error=None,
                    )
                )
                generated += 1
            except Exception as exc:  # continue so every request receives a status
                safe_error = _redact(exc, self.environment)
                entries.append(
                    _catalog_entry(
                        request,
                        scene_path=None,
                        sha256=None,
                        source="asset-gen:image+tripo3d",
                        status="failed",
                        error=safe_error,
                    )
                )
                self.log(f"AssetAgent failed for {request.prefab}: {safe_error}")

        # The Godot reader rejects unknown members, so this runtime catalog must
        # remain deliberately tiny.  Auditable Agent output belongs in the
        # adjacent manifest rather than weakening the reader's strict contract.
        catalog: dict[str, Any] = {
            "schema": CATALOG_SCHEMA,
            "assets": [
                {
                    "prefab": entry["prefab"],
                    "scene_path": entry["scene_path"],
                }
                for entry in entries
            ],
        }
        manifest: dict[str, Any] = {
            "schema": MANIFEST_SCHEMA,
            "agent": {
                "name": AGENT_NAME,
                "role": AGENT_ROLE,
                "version": AGENT_VERSION,
            },
            "policy": {
                "mode": self.config.mode,
                "image_model": self.config.image_model,
                "max_api_generations": self.config.max_generations,
                "max_parallel_workers": self.config.max_workers,
                "attempted_api_generations": attempted,
            },
            "assets": entries,
        }
        catalog_path = project / "assets" / "catalog.json"
        manifest_path = project / "assets" / "manifest.json"
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        catalog_path.write_bytes(contracts.canonical_json_bytes(catalog) + b"\n")
        manifest_path.write_bytes(contracts.canonical_json_bytes(manifest) + b"\n")

        unresolved = [
            entry["prefab"]
            for entry in entries
            if entry["status"] not in {"cached", "generated"}
        ]
        result = AssetAgentResult(
            catalog_path=catalog_path,
            manifest_path=manifest_path,
            catalog=catalog,
            manifest=manifest,
            attempted_generations=attempted,
            generated_assets=generated,
            cache_hits=cache_hits,
        )
        if self.config.mode == "required" and unresolved:
            raise AssetResolutionError(manifest_path, unresolved)
        return result


def orchestrate_assets(
    world_spec: Mapping[str, Any],
    project_root: str | Path,
    *,
    source_repo_root: str | Path,
    cache_root: str | Path,
    environment: Mapping[str, str] | None = None,
    command_runner: AssetCommandRunner | None = None,
    log: LogSink | None = None,
) -> AssetAgentResult:
    """Functional adapter for the central multi-agent orchestrator."""

    return AssetAgent(
        source_repo_root=source_repo_root,
        cache_root=cache_root,
        environment=environment,
        command_runner=command_runner,
        log=log,
    ).run(world_spec, project_root)


__all__ = [
    "AGENT_NAME",
    "AGENT_ROLE",
    "AGENT_VERSION",
    "ASSET_MAX_GENERATIONS_ENV",
    "ASSET_MAX_WORKERS_ENV",
    "ASSET_MODE_ENV",
    "AssetAgent",
    "AssetAgentConfig",
    "AssetAgentResult",
    "AssetCommandResult",
    "AssetConfigurationError",
    "AssetRequest",
    "AssetResolutionError",
    "derive_asset_requests",
    "orchestrate_assets",
]
