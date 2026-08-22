"""Role-isolated API agents and auditable call traces for Prompt-to-Play.

The agents share credentials and transport configuration, but never share an
unstructured conversation. Each role gets its own provider instance, system
prompt, JSON contract, usage accounting, and trace entries.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from . import direct_common
from .provider import ProviderUsage, create_provider_from_env


TRACE_SCHEMA = "prompt-to-play/agent-trace@1"


class AgentRole(str, Enum):
    PROJECT_GENERATOR = "project_generator"
    CODE_REPAIR = "code_repair"
    WORLD_PLANNER = "world_planner"
    VISUAL_EVALUATOR = "visual_evaluator"
    REPAIR = "repair"


ROLE_MODEL_ENV: Mapping[AgentRole, str] = {
    AgentRole.PROJECT_GENERATOR: "PROMPT_TO_PLAY_PLANNER_MODEL",
    AgentRole.CODE_REPAIR: "PROMPT_TO_PLAY_REPAIR_MODEL",
    AgentRole.WORLD_PLANNER: "PROMPT_TO_PLAY_PLANNER_MODEL",
    AgentRole.VISUAL_EVALUATOR: "PROMPT_TO_PLAY_EVALUATOR_MODEL",
    AgentRole.REPAIR: "PROMPT_TO_PLAY_REPAIR_MODEL",
}

ROLE_MAX_OUTPUT_TOKENS: Mapping[AgentRole, str] = {
    AgentRole.PROJECT_GENERATOR: "20000",
    AgentRole.CODE_REPAIR: "16000",
    AgentRole.VISUAL_EVALUATOR: "6000",
}


class StructuredProvider(Protocol):
    def generate_json(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        json_schema: Mapping[str, Any],
        schema_name: str,
        image_paths: Sequence[str | Path] = (),
    ) -> Mapping[str, Any]: ...


ProviderFactory = Callable[[AgentRole, Mapping[str, str], Path], StructuredProvider]


@dataclass(frozen=True)
class AgentCallTrace:
    sequence: int
    role: str
    schema_name: str
    status: str
    started_at_utc: str
    finished_at_utc: str
    duration_ms: int
    input_sha256: str
    output_sha256: str | None
    image_sha256: tuple[str, ...]
    backend: str
    model: str
    token_usage_exact: bool
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    total_tokens: int
    error_type: str | None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _image_digests(paths: Sequence[str | Path]) -> tuple[str, ...]:
    digests: list[str] = []
    for raw_path in paths:
        path = Path(raw_path).resolve(strict=True)
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digests.append(digest.hexdigest())
    return tuple(digests)


def _usage(provider: Any) -> ProviderUsage:
    summary = getattr(provider, "usage_summary", None)
    if callable(summary):
        value = summary()
        if isinstance(value, ProviderUsage):
            return value
    return ProviderUsage(backend=type(provider).__name__, model="unreported")


def _usage_delta(before: ProviderUsage, after: ProviderUsage) -> ProviderUsage:
    return ProviderUsage(
        backend=after.backend,
        model=after.model,
        calls=max(0, after.calls - before.calls),
        input_tokens=max(0, after.input_tokens - before.input_tokens),
        cached_input_tokens=max(
            0, after.cached_input_tokens - before.cached_input_tokens
        ),
        output_tokens=max(0, after.output_tokens - before.output_tokens),
        exact=after.exact and after.calls > before.calls,
    )


class TracingAgentProvider:
    """Wrap one structured provider as a named, independently traced agent."""

    def __init__(
        self,
        role: AgentRole,
        provider: StructuredProvider,
        record: Callable[[AgentCallTrace], None],
        next_sequence: Callable[[], int],
    ) -> None:
        self.role = role
        self.provider = provider
        self._record = record
        self._next_sequence = next_sequence

    def usage_summary(self) -> ProviderUsage:
        return _usage(self.provider)

    def generate_json(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        json_schema: Mapping[str, Any],
        schema_name: str,
        image_paths: Sequence[str | Path] = (),
    ) -> Mapping[str, Any]:
        sequence = self._next_sequence()
        started_at = _utc_now()
        started = time.perf_counter()
        image_sha256 = _image_digests(image_paths)
        request_document = {
            "role": self.role.value,
            "messages": [dict(message) for message in messages],
            "json_schema": json_schema,
            "schema_name": schema_name,
            "image_sha256": list(image_sha256),
        }
        input_sha256 = direct_common.document_sha256(request_document)
        before = _usage(self.provider)
        output_sha256: str | None = None
        error_type: str | None = None
        status = "success"
        try:
            document = self.provider.generate_json(
                messages,
                json_schema=json_schema,
                schema_name=schema_name,
                image_paths=image_paths,
            )
            output_sha256 = direct_common.document_sha256(document)
            return document
        except Exception as exc:
            status = "error"
            error_type = type(exc).__name__
            raise
        finally:
            after = _usage(self.provider)
            delta = _usage_delta(before, after)
            duration_ms = max(0, round((time.perf_counter() - started) * 1000))
            self._record(
                AgentCallTrace(
                    sequence=sequence,
                    role=self.role.value,
                    schema_name=schema_name,
                    status=status,
                    started_at_utc=started_at,
                    finished_at_utc=_utc_now(),
                    duration_ms=duration_ms,
                    input_sha256=input_sha256,
                    output_sha256=output_sha256,
                    image_sha256=image_sha256,
                    backend=delta.backend,
                    model=delta.model,
                    token_usage_exact=delta.exact,
                    input_tokens=delta.input_tokens,
                    cached_input_tokens=delta.cached_input_tokens,
                    output_tokens=delta.output_tokens,
                    total_tokens=delta.total_tokens,
                    error_type=error_type,
                )
            )


class MultiAgentRuntime:
    """Create isolated role providers and persist one deterministic trace."""

    def __init__(
        self,
        repo: str | Path,
        *,
        environment: Mapping[str, str] | None = None,
        provider_factory: ProviderFactory | None = None,
    ) -> None:
        self.repo = Path(repo).resolve(strict=True)
        self.environment = dict(os.environ if environment is None else environment)
        self._provider_factory = (
            self._default_provider_factory
            if provider_factory is None
            else provider_factory
        )
        self._agents: dict[AgentRole, TracingAgentProvider] = {}
        self._traces: list[AgentCallTrace] = []
        self._lock = threading.Lock()
        self._sequence = 0

    @staticmethod
    def _default_provider_factory(
        role: AgentRole,
        environment: Mapping[str, str],
        repo: Path,
    ) -> StructuredProvider:
        env = dict(environment)
        # Runtime generation is API-first. Codex CLI remains available only
        # when a developer explicitly selects it.
        env.setdefault("PROMPT_TO_PLAY_PROVIDER", "http")
        role_model = env.get(ROLE_MODEL_ENV[role], "").strip()
        if role_model:
            env["PROMPT_TO_PLAY_MODEL"] = role_model
        if not env.get("PROMPT_TO_PLAY_MAX_OUTPUT_TOKENS", "").strip():
            role_limit = ROLE_MAX_OUTPUT_TOKENS.get(role)
            if role_limit:
                env["PROMPT_TO_PLAY_MAX_OUTPUT_TOKENS"] = role_limit
        return create_provider_from_env(env, repo=repo)

    def _next_sequence(self) -> int:
        with self._lock:
            self._sequence += 1
            return self._sequence

    def _record(self, trace: AgentCallTrace) -> None:
        with self._lock:
            self._traces.append(trace)

    def agent(self, role: AgentRole) -> TracingAgentProvider:
        with self._lock:
            existing = self._agents.get(role)
        if existing is not None:
            return existing
        provider = self._provider_factory(role, self.environment, self.repo)
        created = TracingAgentProvider(
            role,
            provider,
            self._record,
            self._next_sequence,
        )
        with self._lock:
            return self._agents.setdefault(role, created)

    def traces(self) -> tuple[AgentCallTrace, ...]:
        with self._lock:
            return tuple(sorted(self._traces, key=lambda item: item.sequence))

    def aggregate_usage(self) -> ProviderUsage:
        traces = self.traces()
        models = sorted({trace.model for trace in traces if trace.model})
        backends = sorted({trace.backend for trace in traces if trace.backend})
        return ProviderUsage(
            backend="+".join(backends) or "unreported",
            model="+".join(models) or "unreported",
            calls=len(traces),
            input_tokens=sum(trace.input_tokens for trace in traces),
            cached_input_tokens=sum(trace.cached_input_tokens for trace in traces),
            output_tokens=sum(trace.output_tokens for trace in traces),
            exact=bool(traces) and all(trace.token_usage_exact for trace in traces),
        )

    def write_trace(self, path: str | Path, *, run_id: str) -> Path:
        output = Path(path)
        with self._lock:
            active_roles = [role.value for role in self._agents]
        document = {
            "schema": TRACE_SCHEMA,
            "run_id": run_id,
            "agents": active_roles,
            "calls": [asdict(trace) for trace in self.traces()],
            "usage": asdict(self.aggregate_usage()),
        }
        document["usage"]["total_tokens"] = self.aggregate_usage().total_tokens
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(direct_common.canonical_json_bytes(document) + b"\n")
        return output
