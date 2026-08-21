"""Dependency-free OpenAI-compatible structured-output provider.

Configuration is read from environment variables so the planner is not tied to
one vendor or model:

``PROMPT_TO_PLAY_API_KEY`` (or ``OPENAI_API_KEY``)
``PROMPT_TO_PLAY_MODEL`` (or ``OPENAI_MODEL``)
``PROMPT_TO_PLAY_BASE_URL`` (or ``OPENAI_BASE_URL``)
``PROMPT_TO_PLAY_API_STYLE`` (``chat_completions`` or ``responses``)
``PROMPT_TO_PLAY_TIMEOUT_SECONDS`` and ``PROMPT_TO_PLAY_MAX_OUTPUT_TOKENS``

``PROMPT_TO_PLAY_PROVIDER`` defaults to ``auto``: use the HTTP provider when
an API key exists, otherwise reuse the signed-in ``codex`` CLI. Set it to
``openai`` or ``codex`` to force a backend. ``PROMPT_TO_PLAY_CODEX_COMMAND``
and ``PROMPT_TO_PLAY_REPO`` override the CLI executable and read-only workspace.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib import error, parse, request


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-5-mini"
API_STYLES = ("chat_completions", "responses")
PROVIDER_MODES = ("auto", "openai", "codex")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
IMAGE_MIME_TYPES = {
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class ProviderError(RuntimeError):
    """Base class for configuration, transport, and response failures."""


class ProviderConfigurationError(ProviderError):
    """Raised before a request when provider configuration is unusable."""


class ProviderCapabilityError(ProviderError):
    """Raised when a selected backend cannot consume a requested modality."""


class ProviderRequestError(ProviderError):
    """Raised when the remote endpoint cannot complete a request."""


class ProviderResponseError(ProviderError):
    """Raised when the endpoint returns a malformed structured response."""


Transport = Callable[[str, Mapping[str, str], bytes, float], bytes]


def _positive_number(value: str, name: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise ProviderConfigurationError(f"{name} must be a number") from exc
    if number <= 0:
        raise ProviderConfigurationError(f"{name} must be > 0")
    return number


def _positive_integer(value: str, name: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise ProviderConfigurationError(f"{name} must be an integer") from exc
    if number <= 0:
        raise ProviderConfigurationError(f"{name} must be > 0")
    return number


@dataclass(frozen=True)
class ProviderConfig:
    api_key: str
    model: str
    base_url: str = DEFAULT_BASE_URL
    api_style: str = "chat_completions"
    timeout_seconds: float = 120.0
    max_output_tokens: int = 12000

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ProviderConfig":
        env = os.environ if environ is None else environ
        api_key = (
            env.get("PROMPT_TO_PLAY_API_KEY") or env.get("OPENAI_API_KEY") or ""
        ).strip()
        if not api_key:
            raise ProviderConfigurationError(
                "missing API key; set PROMPT_TO_PLAY_API_KEY or OPENAI_API_KEY"
            )
        model = (
            env.get("PROMPT_TO_PLAY_MODEL") or env.get("OPENAI_MODEL") or DEFAULT_MODEL
        ).strip()
        if not model:
            raise ProviderConfigurationError("provider model must not be empty")
        base_url = (
            env.get("PROMPT_TO_PLAY_BASE_URL")
            or env.get("OPENAI_BASE_URL")
            or DEFAULT_BASE_URL
        ).strip()
        parsed = parse.urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ProviderConfigurationError(
                "PROMPT_TO_PLAY_BASE_URL must be an absolute http(s) URL"
            )
        api_style = env.get("PROMPT_TO_PLAY_API_STYLE", "chat_completions").strip()
        if api_style not in API_STYLES:
            raise ProviderConfigurationError(
                "PROMPT_TO_PLAY_API_STYLE must be 'chat_completions' or 'responses'"
            )
        timeout = _positive_number(
            env.get("PROMPT_TO_PLAY_TIMEOUT_SECONDS", "120"),
            "PROMPT_TO_PLAY_TIMEOUT_SECONDS",
        )
        max_output_tokens = _positive_integer(
            env.get("PROMPT_TO_PLAY_MAX_OUTPUT_TOKENS", "12000"),
            "PROMPT_TO_PLAY_MAX_OUTPUT_TOKENS",
        )
        return cls(
            api_key=api_key,
            model=model,
            base_url=base_url.rstrip("/"),
            api_style=api_style,
            timeout_seconds=timeout,
            max_output_tokens=max_output_tokens,
        )


def _default_transport(
    url: str,
    headers: Mapping[str, str],
    payload: bytes,
    timeout_seconds: float,
) -> bytes:
    outgoing = request.Request(url, data=payload, headers=dict(headers), method="POST")
    try:
        with request.urlopen(outgoing, timeout=timeout_seconds) as response:
            return response.read()
    except error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise ProviderRequestError(f"provider HTTP {exc.code}{suffix}") from exc
    except error.URLError as exc:
        raise ProviderRequestError(f"provider request failed: {exc.reason}") from exc
    except OSError as exc:
        raise ProviderRequestError(f"provider request failed: {exc}") from exc


def _message_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
        elif isinstance(text, dict) and isinstance(text.get("value"), str):
            parts.append(text["value"])
    return "".join(parts) if parts else None


class OpenAICompatibleProvider:
    """Call Chat Completions or Responses with strict JSON Schema output."""

    def __init__(
        self,
        config: ProviderConfig | None = None,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.config = ProviderConfig.from_env() if config is None else config
        self._transport = _default_transport if transport is None else transport

    def _endpoint(self) -> str:
        suffix = (
            "/chat/completions"
            if self.config.api_style == "chat_completions"
            else "/responses"
        )
        base = self.config.base_url.rstrip("/")
        if base.endswith(suffix):
            return base
        if base.endswith(("/chat/completions", "/responses")):
            raise ProviderConfigurationError(
                f"base URL endpoint does not match API style {self.config.api_style!r}"
            )
        return base + suffix

    def _payload(
        self,
        messages: Sequence[Mapping[str, str]],
        json_schema: Mapping[str, Any],
        schema_name: str,
        image_data_urls: Sequence[str] = (),
    ) -> dict[str, Any]:
        request_messages = self._messages_with_images(messages, image_data_urls)
        if self.config.api_style == "chat_completions":
            return {
                "model": self.config.model,
                "messages": request_messages,
                "temperature": 0,
                "max_tokens": self.config.max_output_tokens,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "strict": True,
                        "schema": json_schema,
                    },
                },
            }
        return {
            "model": self.config.model,
            "input": request_messages,
            "temperature": 0,
            "max_output_tokens": self.config.max_output_tokens,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": json_schema,
                }
            },
        }

    def _messages_with_images(
        self,
        messages: Sequence[Mapping[str, str]],
        image_data_urls: Sequence[str],
    ) -> list[dict[str, Any]]:
        request_messages: list[dict[str, Any]] = [dict(message) for message in messages]
        if not image_data_urls:
            return request_messages

        user_index = next(
            (
                index
                for index in range(len(request_messages) - 1, -1, -1)
                if request_messages[index].get("role") == "user"
            ),
            None,
        )
        if user_index is None:
            raise ProviderConfigurationError(
                "reference images require at least one user message"
            )
        text = request_messages[user_index]["content"]
        if self.config.api_style == "chat_completions":
            content: list[dict[str, Any]] = [{"type": "text", "text": text}]
            content.extend(
                {
                    "type": "image_url",
                    "image_url": {"url": data_url, "detail": "auto"},
                }
                for data_url in image_data_urls
            )
        else:
            content = [{"type": "input_text", "text": text}]
            content.extend(
                {
                    "type": "input_image",
                    "image_url": data_url,
                    "detail": "auto",
                }
                for data_url in image_data_urls
            )
        request_messages[user_index]["content"] = content
        return request_messages

    @staticmethod
    def _image_data_url(value: str | Path, index: int) -> str:
        supplied = Path(value).expanduser()
        try:
            image = supplied.resolve(strict=True)
        except OSError as exc:
            raise ProviderConfigurationError(
                f"image_paths[{index}] does not exist or cannot be read: {supplied}"
            ) from exc
        if not image.is_file():
            raise ProviderConfigurationError(
                f"image_paths[{index}] does not exist or is not a file: {supplied}"
            )
        media_type = IMAGE_MIME_TYPES.get(image.suffix.lower())
        if media_type is None:
            raise ProviderCapabilityError(
                f"image_paths[{index}] has an unsupported image type: {image.suffix!r}"
            )
        try:
            encoded = base64.b64encode(image.read_bytes()).decode("ascii")
        except OSError as exc:
            raise ProviderConfigurationError(
                f"image_paths[{index}] cannot be read: {supplied}"
            ) from exc
        return f"data:{media_type};base64,{encoded}"

    @staticmethod
    def _extract_structured(
        response: Mapping[str, Any], api_style: str
    ) -> Mapping[str, Any]:
        parsed: Any = None
        text: str | None = None
        if api_style == "chat_completions":
            choices = response.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ProviderResponseError("chat response has no choices")
            choice = choices[0]
            message = choice.get("message") if isinstance(choice, dict) else None
            if not isinstance(message, dict):
                raise ProviderResponseError("chat response has no assistant message")
            parsed = message.get("parsed")
            text = _message_text(message.get("content"))
        else:
            parsed = response.get("parsed")
            if isinstance(response.get("output_text"), str):
                text = response["output_text"]
            if text is None:
                output = response.get("output")
                if isinstance(output, list):
                    for item in output:
                        if not isinstance(item, dict):
                            continue
                        candidate = _message_text(item.get("content"))
                        if candidate:
                            text = candidate
                            break
        if isinstance(parsed, dict):
            return parsed
        if text is None or not text.strip():
            raise ProviderResponseError("provider returned no structured output text")
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderResponseError(
                f"structured output is not valid JSON at line {exc.lineno}, column {exc.colno}"
            ) from exc
        if not isinstance(document, dict):
            raise ProviderResponseError("structured output must be a JSON object")
        return document

    def generate_json(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        json_schema: Mapping[str, Any],
        schema_name: str,
        image_paths: Sequence[str | Path] = (),
    ) -> Mapping[str, Any]:
        if not messages:
            raise ProviderConfigurationError("at least one message is required")
        for index, message in enumerate(messages):
            if message.get("role") not in ("system", "user", "assistant"):
                raise ProviderConfigurationError(f"messages[{index}].role is invalid")
            if (
                not isinstance(message.get("content"), str)
                or not message["content"].strip()
            ):
                raise ProviderConfigurationError(
                    f"messages[{index}].content must be non-empty"
                )
        image_data_urls = [
            self._image_data_url(value, index)
            for index, value in enumerate(image_paths)
        ]
        payload = self._payload(messages, json_schema, schema_name, image_data_urls)
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(
            "utf-8"
        )
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        raw = self._transport(
            self._endpoint(), headers, encoded, self.config.timeout_seconds
        )
        if not isinstance(raw, bytes):
            raise ProviderResponseError("provider transport must return bytes")
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderResponseError(
                "provider response is not valid UTF-8 JSON"
            ) from exc
        if not isinstance(response, dict):
            raise ProviderResponseError("provider response must be a JSON object")
        return self._extract_structured(response, self.config.api_style)


@dataclass(frozen=True)
class CodexCliConfig:
    command: str = "codex"
    repo: Path = Path.cwd()
    timeout_seconds: float = 300.0
    max_stderr_chars: int = 2000

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "CodexCliConfig":
        env = os.environ if environ is None else environ
        command = env.get("PROMPT_TO_PLAY_CODEX_COMMAND", "codex").strip()
        if not command:
            raise ProviderConfigurationError(
                "PROMPT_TO_PLAY_CODEX_COMMAND must not be empty"
            )
        repo = Path(env.get("PROMPT_TO_PLAY_REPO", str(Path.cwd()))).resolve()
        if not repo.is_dir():
            raise ProviderConfigurationError(
                f"PROMPT_TO_PLAY_REPO is not a directory: {repo}"
            )
        timeout = _positive_number(
            env.get("PROMPT_TO_PLAY_TIMEOUT_SECONDS", "300"),
            "PROMPT_TO_PLAY_TIMEOUT_SECONDS",
        )
        max_stderr = _positive_integer(
            env.get("PROMPT_TO_PLAY_MAX_STDERR_CHARS", "2000"),
            "PROMPT_TO_PLAY_MAX_STDERR_CHARS",
        )
        return cls(
            command=command,
            repo=repo,
            timeout_seconds=timeout,
            max_stderr_chars=max_stderr,
        )


def _filtered_stderr(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else ""
    text = ANSI_RE.sub("", text).replace("\x00", "")
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "<redacted-key>", text)
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if len(line) > 500 and any(
            marker in lower
            for marker in (
                "response body",
                "cached body",
                "cache body",
                '"body"',
                "body:",
            )
        ):
            line = line[:160] + " … <large body omitted>"
        elif len(line) > 500:
            line = line[:500] + " … <line truncated>"
        lines.append(line)
    filtered = "\n".join(lines) or "no diagnostic output"
    if len(filtered) > limit:
        head_size = max(1, limit // 2)
        tail_size = max(1, limit - head_size - 24)
        filtered = (
            filtered[:head_size] + "\n… <stderr truncated> …\n" + filtered[-tail_size:]
        )
    return filtered[:limit]


def _codex_child_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Keep Codex authentication/network settings without exposing unrelated secrets."""

    allowed = {
        "all_proxy",
        "appdata",
        "codex_home",
        "comspec",
        "home",
        "http_proxy",
        "https_proxy",
        "lang",
        "localappdata",
        "no_proxy",
        "openai_api_key",
        "path",
        "pathext",
        "requests_ca_bundle",
        "ssl_cert_file",
        "systemroot",
        "temp",
        "tmp",
        "userprofile",
        "windir",
        "xdg_config_home",
    }
    child = {
        key: value
        for key, value in environment.items()
        if key.casefold() in allowed and isinstance(value, str)
    }
    has_codex_home = any(
        key.casefold() == "codex_home" and bool(value) for key, value in child.items()
    )
    if not has_codex_home:
        child["CODEX_HOME"] = str(Path.home() / ".codex")
    return child


class CodexCliProvider:
    """Reuse the local Codex login through a sandboxed, ephemeral CLI call."""

    _POWERSHELL_FORWARD_SCRIPT = (
        "$ErrorActionPreference = 'Stop'\n"
        "$executable = $args[0]\n"
        "$forwarded = @($args | Select-Object -Skip 1)\n"
        "try {\n"
        "  & $executable @forwarded\n"
        "  if (-not $?) { exit 1 }\n"
        "  if ($null -eq $LASTEXITCODE) { exit 1 }\n"
        "  exit [int]$LASTEXITCODE\n"
        "} catch {\n"
        "  [Console]::Error.WriteLine($_)\n"
        "  exit 1\n"
        "}\n"
    )

    def __init__(
        self,
        config: CodexCliConfig | None = None,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        powershell_finder: Callable[[str], str | None] | None = None,
        windows: bool | None = None,
    ) -> None:
        self.config = CodexCliConfig.from_env() if config is None else config
        self._runner = subprocess.run if runner is None else runner
        self._powershell_finder = (
            shutil.which if powershell_finder is None else powershell_finder
        )
        self._is_windows = os.name == "nt" if windows is None else windows

    def _run(
        self, argv: Sequence[str], prompt: str
    ) -> subprocess.CompletedProcess[str]:
        environment = _codex_child_environment(os.environ)
        return self._runner(
            list(argv),
            input=prompt,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.config.timeout_seconds,
            check=False,
        )

    def _powershell_argv(
        self,
        direct_argv: Sequence[str],
        wrapper_script: Path,
    ) -> list[str]:
        powershell = self._powershell_finder("pwsh.exe")
        if powershell is None:
            powershell = self._powershell_finder("powershell.exe")
        if powershell is None:
            raise ProviderRequestError(
                "direct codex launch was denied and no pwsh.exe or powershell.exe was found"
            )
        return [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(wrapper_script),
            *direct_argv,
        ]

    @staticmethod
    def _prompt(
        messages: Sequence[Mapping[str, str]],
        json_schema: Mapping[str, Any],
        schema_name: str,
    ) -> str:
        conversation = "\n\n".join(
            f"[{message['role'].upper()}]\n{message['content']}" for message in messages
        )
        schema = json.dumps(json_schema, ensure_ascii=False, separators=(",", ":"))
        return (
            "Act only as a structured JSON generation backend. Do not edit files or run commands. "
            f"Return exactly one JSON object conforming to schema {schema_name!r}; no Markdown.\n\n"
            f"JSON SCHEMA\n{schema}\n\nCONVERSATION\n{conversation}"
        )

    def generate_json(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        json_schema: Mapping[str, Any],
        schema_name: str,
        image_paths: Sequence[str | Path] = (),
    ) -> Mapping[str, Any]:
        if not messages:
            raise ProviderConfigurationError("at least one message is required")
        for index, message in enumerate(messages):
            if message.get("role") not in ("system", "user", "assistant"):
                raise ProviderConfigurationError(f"messages[{index}].role is invalid")
            if (
                not isinstance(message.get("content"), str)
                or not message["content"].strip()
            ):
                raise ProviderConfigurationError(
                    f"messages[{index}].content must be non-empty"
                )

        resolved_images: list[Path] = []
        for index, value in enumerate(image_paths):
            supplied = Path(value)
            image = supplied if supplied.is_absolute() else self.config.repo / supplied
            image = image.resolve()
            if not image.is_file():
                raise ProviderConfigurationError(
                    f"image_paths[{index}] does not exist or is not a file: {supplied}"
                )
            resolved_images.append(image)

        with tempfile.TemporaryDirectory(prefix="prompt-to-play-codex-") as temporary:
            temp = Path(temporary)
            workspace = temp / "workspace"
            workspace.mkdir()
            output_path = temp / "world.json"
            schema_path = temp / "world-schema.json"
            try:
                schema_path.write_text(
                    json.dumps(json_schema, ensure_ascii=False, allow_nan=False),
                    encoding="utf-8",
                )
            except (OSError, TypeError, ValueError) as exc:
                raise ProviderConfigurationError(
                    f"cannot serialize output schema: {exc}"
                ) from exc
            command = [
                self.config.command,
                "-a",
                "never",
                "exec",
                "--ignore-user-config",
                "--ephemeral",
                "--skip-git-repo-check",
                "-s",
                "read-only",
                "-C",
                str(workspace),
                "--output-schema",
                str(schema_path),
                "-o",
                str(output_path),
            ]
            for image in resolved_images:
                command.extend(("-i", str(image)))
            prompt = self._prompt(messages, json_schema, schema_name)
            command.append("-")
            try:
                completed = self._run(command, prompt)
            except PermissionError as direct_error:
                if not self._is_windows:
                    raise ProviderRequestError(
                        f"cannot launch codex CLI: {direct_error}"
                    ) from direct_error
                wrapper_script = temp / "invoke-codex.ps1"
                try:
                    wrapper_script.write_text(
                        self._POWERSHELL_FORWARD_SCRIPT,
                        encoding="ascii",
                        newline="\n",
                    )
                except OSError as exc:
                    raise ProviderRequestError(
                        f"cannot create the PowerShell Codex launcher: {exc}"
                    ) from exc
                wrapper_command = self._powershell_argv(command, wrapper_script)
                try:
                    completed = self._run(wrapper_command, prompt)
                except subprocess.TimeoutExpired as exc:
                    raise ProviderRequestError(
                        f"codex CLI timed out after {self.config.timeout_seconds:g}s"
                    ) from exc
                except OSError as exc:
                    raise ProviderRequestError(
                        f"cannot launch codex through PowerShell: {exc}"
                    ) from exc
            except subprocess.TimeoutExpired as exc:
                raise ProviderRequestError(
                    f"codex CLI timed out after {self.config.timeout_seconds:g}s"
                ) from exc
            except OSError as exc:
                raise ProviderRequestError(f"cannot launch codex CLI: {exc}") from exc
            if completed.returncode != 0:
                diagnostic = _filtered_stderr(
                    completed.stderr,
                    self.config.max_stderr_chars,
                )
                raise ProviderRequestError(
                    f"codex CLI exited with status {completed.returncode}: {diagnostic}"
                )
            try:
                raw = output_path.read_text(encoding="utf-8")
            except OSError as exc:
                diagnostic = _filtered_stderr(
                    completed.stderr,
                    self.config.max_stderr_chars,
                )
                raise ProviderResponseError(
                    f"codex CLI produced no readable output: {diagnostic}"
                ) from exc
            try:
                document = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ProviderResponseError(
                    f"codex structured output is invalid JSON at line {exc.lineno}, "
                    f"column {exc.colno}"
                ) from exc
            if not isinstance(document, dict):
                raise ProviderResponseError(
                    "codex structured output must be a JSON object"
                )
            return document


def _workspace_codex_command(repo: Path) -> Path | None:
    roots: list[Path] = []
    for candidate in (repo, repo.parent):
        resolved = candidate.resolve(strict=False)
        if resolved not in roots:
            roots.append(resolved)
    for root in roots:
        bin_directory = root / ".tools" / "codex-cli" / "node_modules" / ".bin"
        for name in ("codex.cmd", "codex"):
            candidate = bin_directory / name
            if candidate.is_file():
                return candidate.resolve()
    return None


def create_provider_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    repo: str | Path | None = None,
) -> OpenAICompatibleProvider | CodexCliProvider:
    """Select HTTP or Codex CLI without silently pretending images were read."""

    source_env = os.environ if environ is None else environ
    env = dict(source_env)
    if repo is not None and not env.get("PROMPT_TO_PLAY_REPO"):
        env["PROMPT_TO_PLAY_REPO"] = str(Path(repo).resolve())
    mode = env.get("PROMPT_TO_PLAY_PROVIDER", "auto").strip().lower()
    if mode not in PROVIDER_MODES:
        raise ProviderConfigurationError(
            "PROMPT_TO_PLAY_PROVIDER must be 'auto', 'openai', or 'codex'"
        )
    has_key = bool(
        (env.get("PROMPT_TO_PLAY_API_KEY") or env.get("OPENAI_API_KEY") or "").strip()
    )
    if mode == "openai" or (mode == "auto" and has_key):
        return OpenAICompatibleProvider(ProviderConfig.from_env(env))

    config = CodexCliConfig.from_env(env)
    if not env.get("PROMPT_TO_PLAY_CODEX_COMMAND", "").strip():
        workspace_command = _workspace_codex_command(config.repo)
        if workspace_command is not None:
            config = replace(config, command=str(workspace_command))
    command_available = bool(shutil.which(config.command))
    if not command_available:
        supplied = Path(config.command)
        command_available = supplied.is_file()
    if not command_available:
        if mode == "auto":
            raise ProviderConfigurationError(
                "no API key and no codex CLI found; set OPENAI_API_KEY or install/sign in to codex"
            )
        raise ProviderConfigurationError(
            f"codex CLI executable not found: {config.command}"
        )
    return CodexCliProvider(config)
