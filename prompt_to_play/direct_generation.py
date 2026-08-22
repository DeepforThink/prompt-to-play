"""Safe file-package contract for direct LLM-to-Godot generation.

The model is free to express game mechanics in Godot scenes, resources,
shaders, data, and C# rather than through a fixed world schema.  It is *not*
free to choose where those files are written or which host capabilities its
C# may call.  This module is the trust boundary between structured model
output and a generated project directory.

The checks here are intentionally dependency free.  They are a conservative
static gate, not a replacement for running generated games in an OS sandbox.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


DIRECT_GENERATION_CONTRACT = "prompt-to-play/direct-files@1"
DIRECT_MANIFEST_CONTRACT = "prompt-to-play/direct-manifest@1"

MAX_FILES = 96
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_CONTENT_BYTES = 2 * 1024 * 1024
MAX_JSON_BYTES = MAX_TOTAL_CONTENT_BYTES + 256 * 1024
MAX_PATH_CHARACTERS = 240
MAX_PATH_PARTS = 12

ALLOWED_EXTENSIONS = frozenset(
    {".godot", ".cs", ".gd", ".tscn", ".tres", ".gdshader", ".json", ".md"}
)
ALLOWED_TOP_LEVEL_DIRECTORIES = frozenset({"generated"})

_ACTIONS = frozenset({"upsert", "create", "replace", "delete"})
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WINDOWS_INVALID_CHARS_RE = re.compile(r'[<>:"|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

# These patterns deliberately inspect comments and string literals as well as
# executable tokens.  False positives are preferable to allowing a model to
# disguise a host-capability call behind an alias, reflection, or generated
# source string.
_DANGEROUS_CSHARP_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "process execution",
        re.compile(
            r"\b(?:System\s*\.\s*Diagnostics\s*\.\s*Process|Process\s*\.\s*Start|new\s+Process\b|OS\s*\.\s*(?:Execute|CreateProcess|CreateInstance|ShellOpen))",
            re.IGNORECASE,
        ),
    ),
    (
        "native interop",
        re.compile(
            r"\b(?:DllImport(?:Attribute)?|LibraryImport(?:Attribute)?|NativeLibrary|System\s*\.\s*Runtime\s*\.\s*InteropServices|UnmanagedCallersOnly|Marshal\s*\.\s*GetDelegateForFunctionPointer)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "network access",
        re.compile(
            r"\b(?:System\s*\.\s*Net\b|HttpClient\b|WebClient\b|WebRequest\b|HttpWebRequest\b|TcpClient\b|UdpClient\b|Socket\b|Dns\s*\.|HTTPRequest\b|HttpRequest\b|WebSocketPeer\b|StreamPeerTcp\b|PacketPeerUdp\b|UdpServer\b|ENetMultiplayerPeer\b|WebRtcMultiplayerPeer\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "host filesystem access",
        re.compile(
            r"\b(?:System\s*\.\s*IO\b|File\s*\.|Directory\s*\.|FileInfo\b|DirectoryInfo\b|DriveInfo\b|FileSystemWatcher\b|FileStream\b|StreamReader\b|StreamWriter\b|MemoryMappedFile\b|Path\s*\.|FileAccess\s*\.|DirAccess\s*\.|ProjectSettings\s*\.\s*(?:GlobalizePath|Save|SaveCustom)|ResourceSaver\s*\.\s*Save|ConfigFile\s*\.\s*Save|Image\s*\.\s*Save(?:Png|Jpg|Webp|Exr))",
            re.IGNORECASE,
        ),
    ),
    (
        "environment or registry access",
        re.compile(
            r"\b(?:System\s*\.\s*Environment\b|Environment\s*\.\s*(?:GetEnvironmentVariable|SetEnvironmentVariable|ExpandEnvironmentVariables|GetCommandLineArgs|CommandLine|CurrentDirectory|UserName|MachineName|UserDomainName|SystemDirectory)|OS\s*\.\s*(?:GetEnvironment|HasEnvironment|SetEnvironment|GetCmdlineArgs|GetExecutablePath|GetUserDataDir)|Microsoft\s*\.\s*Win32\b|Registry(?:Key)?\s*\.)",
            re.IGNORECASE,
        ),
    ),
    (
        "dynamic code loading",
        re.compile(
            r"\b(?:System\s*\.\s*Reflection\b|System\s*\.\s*Type\b|Type\s*\.\s*GetType|(?:GetType\s*\(\s*\)|typeof\s*\([^)]*\))\s*\.\s*(?:GetMethod|GetMethods|GetProperty|GetField|InvokeMember)|\w+\s*\.\s*(?:GetMethod|GetMethods|GetProperty|GetField)\s*\(|MethodInfo\b|PropertyInfo\b|FieldInfo\b|BindingFlags\b|\w+\s*\.\s*Invoke\s*\(|Delegate\s*\.\s*CreateDelegate|System\s*\.\s*Linq\s*\.\s*Expressions\b|Assembly\s*\.\s*(?:Load|LoadFrom|LoadFile)|Activator\s*\.\s*CreateInstance|AppDomain\b|CSharpCodeProvider\b|CSharpScript\b|GDScript\b|\w+\s*\.\s*SetScript\s*\(|Microsoft\s*\.\s*(?:CSharp|CodeAnalysis)\b|System\s*\.\s*Management\s*\.\s*Automation\b|\bdynamic\s+[A-Za-z_])",
            re.IGNORECASE,
        ),
    ),
    (
        "trusted host access",
        re.compile(
            r"\b(?:PromptToPlay\s*\.\s*Direct|DirectArtifactWriter|DirectHarness|DirectStructuralCheck|DirectCaptureArtifact|DirectInteractionProbeResult)\b|[\"']res://(?:harness|artifacts)/",
            re.IGNORECASE,
        ),
    ),
    ("unsafe code", re.compile(r"\bunsafe\b|\bstackalloc\b", re.IGNORECASE)),
)

_DANGEROUS_GDSCRIPT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "process execution",
        re.compile(
            r"\bOS\s*\.\s*(?:execute|create_process|create_instance|shell_open)\s*\(",
            re.IGNORECASE,
        ),
    ),
    (
        "network access",
        re.compile(
            r"\b(?:HTTPRequest|HTTPClient|TCPServer|StreamPeerTCP|UDPServer|PacketPeerUDP|WebSocketPeer|ENetMultiplayerPeer|WebRTCMultiplayerPeer)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "host filesystem access",
        re.compile(
            r"\b(?:FileAccess\s*\.|DirAccess\b|ProjectSettings\s*\.\s*(?:globalize_path|save|save_custom)|ResourceSaver\s*\.\s*save|ConfigFile\s*\.\s*save|[A-Za-z_][A-Za-z0-9_]*\s*\.\s*save_(?:png|jpg|webp|exr))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "environment access",
        re.compile(
            r"\bOS\s*\.\s*(?:get_environment|has_environment|set_environment|get_cmdline_args|get_executable_path|get_user_data_dir)\s*\(",
            re.IGNORECASE,
        ),
    ),
    (
        "native or dynamic code loading",
        re.compile(
            r"\b(?:JavaScriptBridge|GDExtension|NativeExtension|Engine\s*\.\s*get_singleton)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "trusted host access",
        re.compile(
            r"\b(?:PromptToPlay\s*\.\s*Direct|DirectArtifactWriter|DirectHarness|DirectInteractionProbe)\b|[\"']res://(?:harness|artifacts)/",
            re.IGNORECASE,
        ),
    ),
)

_GODOT_EXTERNAL_PATH_RE = re.compile(
    r"(?:path|config_file)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
)
_GODOT_RESOURCE_CALL_RE = re.compile(
    r"\b(?:load|preload|ResourceLoader\s*\.\s*load)\s*\(\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
_CSHARP_RESOURCE_CALL_RE = re.compile(
    r"\b(?:GD\s*\.\s*Load|ResourceLoader\s*\.\s*Load)(?:\s*<[^>]+>)?\s*\(\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)


DIRECT_GENERATION_SCHEMA: Mapping[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema", "files"],
    "properties": {
        "schema": {"const": DIRECT_GENERATION_CONTRACT},
        "files": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_FILES,
            "items": {
                "anyOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "content"],
                        "properties": {
                            "path": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": MAX_PATH_CHARACTERS,
                            },
                            "content": {
                                "type": "string",
                                "maxLength": MAX_FILE_BYTES,
                            },
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "action", "content"],
                        "properties": {
                            "path": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": MAX_PATH_CHARACTERS,
                            },
                            "action": {
                                "type": "string",
                                "enum": ["upsert", "create", "replace"],
                            },
                            "content": {
                                "type": "string",
                                "maxLength": MAX_FILE_BYTES,
                            },
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "action"],
                        "properties": {
                            "path": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": MAX_PATH_CHARACTERS,
                            },
                            "action": {"const": "delete"},
                        },
                    },
                ]
            },
        },
    },
}

# A descriptive alias makes call sites that pass this to a provider read well.
DIRECT_GENERATION_JSON_SCHEMA = DIRECT_GENERATION_SCHEMA


class DirectGenerationError(ValueError):
    """Raised before untrusted model output can mutate a generated project."""


@dataclass(frozen=True)
class FileOperation:
    path: str
    action: str
    content: str | None = None


@dataclass(frozen=True)
class FilePlan:
    schema: str
    files: tuple[FileOperation, ...]


def _fail(path: str, message: str) -> None:
    raise DirectGenerationError(f"{path}: {message}")


def _reject_json_constant(value: str) -> None:
    raise DirectGenerationError(f"$: non-finite JSON number {value!r} is not allowed")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DirectGenerationError(f"$: duplicate JSON key {key!r}")
        result[key] = value
    return result


def _load_document(document: Any) -> Any:
    if isinstance(document, bytes):
        if len(document) > MAX_JSON_BYTES:
            _fail("$", f"JSON document exceeds {MAX_JSON_BYTES} bytes")
        try:
            text = document.decode("utf-8")
        except UnicodeDecodeError as exc:
            _fail("$", f"JSON document must be UTF-8: {exc}")
    elif isinstance(document, str):
        try:
            size = len(document.encode("utf-8"))
        except UnicodeEncodeError as exc:
            _fail("$", f"JSON document must be valid UTF-8 text: {exc}")
        if size > MAX_JSON_BYTES:
            _fail("$", f"JSON document exceeds {MAX_JSON_BYTES} bytes")
        text = document
    else:
        return document
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except DirectGenerationError:
        raise
    except json.JSONDecodeError as exc:
        _fail("$", f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}")


def _validate_path(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(location, "expected a non-empty string")
    if len(value) > MAX_PATH_CHARACTERS:
        _fail(location, f"must be at most {MAX_PATH_CHARACTERS} characters")
    if "\\" in value:
        _fail(location, "must use forward slashes")
    if value.startswith(("/", "~")) or _WINDOWS_DRIVE_RE.match(value):
        _fail(location, "must be project-relative")
    path = PurePosixPath(value)
    if path.as_posix() != value or not path.parts:
        _fail(location, "must be a normalized project-relative path")
    if len(path.parts) > MAX_PATH_PARTS:
        _fail(location, f"must contain at most {MAX_PATH_PARTS} path components")
    if any(part in ("", ".", "..") for part in path.parts):
        _fail(location, "must not traverse outside the generated project")
    for part in path.parts:
        if part.startswith("."):
            _fail(location, "hidden path components are not allowed")
        if part.endswith((" ", ".")) or _WINDOWS_INVALID_CHARS_RE.search(part):
            _fail(location, "contains a platform-unsafe path component")
        stem = part.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED_NAMES:
            _fail(location, "contains a reserved Windows device name")
    suffix = path.suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        _fail(location, f"extension {suffix or '<none>'!r} is not allowed")
    if len(path.parts) == 1 or path.parts[0] not in ALLOWED_TOP_LEVEL_DIRECTORIES:
        _fail(location, "model-owned files must be inside generated/")
    return value


def _validate_manifest_relative_path(value: str) -> str:
    # Manifest output is host-owned and deliberately outside the model's path
    # allowlist.  It must remain beneath artifacts/ and be JSON.
    if not isinstance(value, str) or not value:
        _fail("manifest_path", "expected a non-empty path")
    if "\\" in value or value.startswith(("/", "~")) or _WINDOWS_DRIVE_RE.match(value):
        _fail("manifest_path", "must be project-relative")
    path = PurePosixPath(value)
    if path.as_posix() != value or any(part in ("", ".", "..") for part in path.parts):
        _fail("manifest_path", "must be a normalized project-relative path")
    if len(path.parts) < 2 or path.parts[0] != "artifacts":
        _fail("manifest_path", "must be inside artifacts/")
    if path.suffix.lower() != ".json":
        _fail("manifest_path", "must use the .json extension")
    for part in path.parts:
        if (
            part.startswith(".")
            or part.endswith((" ", "."))
            or _WINDOWS_INVALID_CHARS_RE.search(part)
        ):
            _fail("manifest_path", "contains an unsafe path component")
    return value


def _validate_csharp(content: str, location: str) -> None:
    for description, pattern in _DANGEROUS_CSHARP_PATTERNS:
        if pattern.search(content):
            _fail(location, f"C# contains forbidden {description}")
    for match in _CSHARP_RESOURCE_CALL_RE.finditer(content):
        _validate_resource_reference(match.group(1), location)


def _validate_gdscript(content: str, location: str) -> None:
    for description, pattern in _DANGEROUS_GDSCRIPT_PATTERNS:
        if pattern.search(content):
            _fail(location, f"GDScript contains forbidden {description}")
    for match in _GODOT_RESOURCE_CALL_RE.finditer(content):
        _validate_resource_reference(match.group(1), location)


def _validate_resource_reference(resource_path: str, location: str) -> None:
    resource_path = resource_path.strip()
    lower = resource_path.lower()
    if lower.startswith(("http://", "https://", "file://", "user://")):
        _fail(location, "Godot resource must not reference an external or user path")
    if lower.startswith("res://"):
        remainder = resource_path[6:]
        parsed = PurePosixPath(remainder)
        if (
            "\\" in remainder
            or parsed.is_absolute()
            or ".." in parsed.parts
            or not parsed.parts
        ):
            _fail(location, "Godot res:// reference must stay inside the project")
        if parsed.parts[0].casefold() not in {"generated", "references"}:
            _fail(
                location,
                "Godot res:// reference must stay inside generated/ or references/",
            )
    elif (
        "://" in lower
        or resource_path.startswith(("/", "~"))
        or _WINDOWS_DRIVE_RE.match(resource_path)
        or ".." in PurePosixPath(resource_path).parts
    ):
        _fail(location, "Godot resource path must stay inside the project")


def _validate_godot_resource_paths(content: str, location: str) -> None:
    for match in _GODOT_EXTERNAL_PATH_RE.finditer(content):
        _validate_resource_reference(match.group(1), location)


def _validate_file_content(path: str, data: bytes, location: str) -> str:
    if len(data) > MAX_FILE_BYTES:
        _fail(location, f"exceeds {MAX_FILE_BYTES} bytes")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail(location, f"must be valid UTF-8 text: {exc}")
    if "\x00" in content:
        _fail(location, "NUL bytes are not allowed")
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".cs":
        _validate_csharp(content, location)
    elif suffix == ".gd":
        _validate_gdscript(content, location)
    elif suffix in {".godot", ".tscn", ".tres"}:
        _validate_godot_resource_paths(content, location)
    return content


def parse_file_plan(document: Any) -> FilePlan:
    """Strictly parse and normalize one model-produced file package.

    ``action`` defaults to ``upsert``.  Explicit ``create``/``replace`` are
    supported when the caller wants optimistic state checks; ``delete`` never
    accepts content.
    """

    value = _load_document(document)
    if not isinstance(value, dict):
        _fail("$", "expected an object")
    allowed_root = {"schema", "files"}
    missing = allowed_root - set(value)
    extra = set(value) - allowed_root
    if missing:
        _fail("$", f"missing keys: {', '.join(sorted(missing))}")
    if extra:
        _fail("$", f"unknown keys: {', '.join(sorted(extra))}")
    if value["schema"] != DIRECT_GENERATION_CONTRACT:
        _fail("$.schema", f"expected {DIRECT_GENERATION_CONTRACT!r}")
    files = value["files"]
    if not isinstance(files, list):
        _fail("$.files", "expected an array")
    if not files:
        _fail("$.files", "expected at least one file operation")
    if len(files) > MAX_FILES:
        _fail("$.files", f"at most {MAX_FILES} file operations are allowed")

    operations: list[FileOperation] = []
    seen: set[str] = set()
    total_bytes = 0
    for index, raw_operation in enumerate(files):
        location = f"$.files[{index}]"
        if not isinstance(raw_operation, dict):
            _fail(location, "expected an object")
        allowed = {"path", "action", "content"}
        extra = set(raw_operation) - allowed
        if "path" not in raw_operation:
            _fail(location, "missing key: path")
        if extra:
            _fail(location, f"unknown keys: {', '.join(sorted(extra))}")
        path = _validate_path(raw_operation["path"], f"{location}.path")
        folded_path = path.casefold()
        if folded_path in seen:
            _fail(f"{location}.path", "duplicate file path")
        seen.add(folded_path)
        action = raw_operation.get("action", "upsert")
        if not isinstance(action, str) or action not in _ACTIONS:
            _fail(f"{location}.action", "expected upsert, create, replace, or delete")
        if action == "delete":
            if "content" in raw_operation:
                _fail(f"{location}.content", "must be omitted for delete")
            content = None
        else:
            if "content" not in raw_operation:
                _fail(f"{location}.content", "is required unless action is delete")
            content = raw_operation["content"]
            if not isinstance(content, str):
                _fail(f"{location}.content", "expected a string")
            try:
                content_bytes = content.encode("utf-8")
            except UnicodeEncodeError as exc:
                _fail(f"{location}.content", f"must be valid UTF-8 text: {exc}")
            _validate_file_content(path, content_bytes, f"{location}.content")
            total_bytes += len(content_bytes)
            if total_bytes > MAX_TOTAL_CONTENT_BYTES:
                _fail(
                    "$.files", f"total content exceeds {MAX_TOTAL_CONTENT_BYTES} bytes"
                )
        operations.append(FileOperation(path=path, action=action, content=content))
    return FilePlan(schema=DIRECT_GENERATION_CONTRACT, files=tuple(operations))


def _canonical_json_bytes(document: Any) -> bytes:
    try:
        text = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DirectGenerationError(f"cannot canonicalize manifest: {exc}") from exc
    return text.encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_link_or_junction(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction and is_junction())
    except OSError as exc:
        raise DirectGenerationError(
            f"cannot inspect project path {path}: {exc}"
        ) from exc


def _assert_no_links(root: Path, relative: PurePosixPath, location: str) -> None:
    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        if _is_link_or_junction(current):
            _fail(location, "must not pass through a symbolic link or junction")
        if (
            index < len(relative.parts) - 1
            and current.exists()
            and not current.is_dir()
        ):
            _fail(location, "parent path is not a directory")


def _ensure_safe_parent(root: Path, relative: PurePosixPath, location: str) -> Path:
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if _is_link_or_junction(current):
            _fail(location, "must not pass through a symbolic link or junction")
        try:
            current.mkdir()
        except FileExistsError:
            if not current.is_dir():
                _fail(location, "parent path is not a directory")
        except OSError as exc:
            raise DirectGenerationError(
                f"{location}: cannot create parent directory: {exc}"
            ) from exc
        if _is_link_or_junction(current) or not current.is_dir():
            _fail(location, "parent path is not a real directory")
    return current


def _atomic_write(
    root: Path, relative: PurePosixPath, data: bytes, *, create_only: bool
) -> None:
    location = relative.as_posix()
    parent = _ensure_safe_parent(root, relative, location)
    target = parent / relative.name
    _assert_no_links(root, relative, location)
    descriptor, raw_temporary = tempfile.mkstemp(
        dir=parent,
        prefix=f".{relative.name}.",
        suffix=".tmp",
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _assert_no_links(root, relative, location)
        if create_only:
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise DirectGenerationError(
                    f"{location}: create target already exists"
                ) from exc
            temporary.unlink()
        else:
            os.replace(temporary, target)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _relative_manifest_path(project_root: Path, manifest_path: str | Path) -> str:
    supplied = Path(manifest_path)
    if supplied.is_absolute():
        try:
            relative = (
                supplied.resolve(strict=False).relative_to(project_root).as_posix()
            )
        except (OSError, ValueError) as exc:
            raise DirectGenerationError(
                "manifest_path: must be inside the generated project"
            ) from exc
    else:
        relative = supplied.as_posix()
    return _validate_manifest_relative_path(relative)


def _normalized_plan_document(plan: FilePlan) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for operation in plan.files:
        item: dict[str, Any] = {"path": operation.path, "action": operation.action}
        if operation.content is not None:
            item["content"] = operation.content
        files.append(item)
    return {"schema": plan.schema, "files": files}


def _scan_generated_files(root: Path) -> dict[str, tuple[str, bytes]]:
    """Read and revalidate the complete model-owned tree without following links."""

    generated = root / "generated"
    if not generated.exists():
        return {}
    if _is_link_or_junction(generated) or not generated.is_dir():
        _fail("generated", "must be a real directory")
    result: dict[str, tuple[str, bytes]] = {}
    total_bytes = 0
    pending = [generated]
    while pending:
        directory = pending.pop()
        try:
            children = sorted(
                directory.iterdir(), key=lambda item: item.name.casefold()
            )
        except OSError as exc:
            raise DirectGenerationError(
                f"cannot inspect generated project: {exc}"
            ) from exc
        for child in children:
            if _is_link_or_junction(child):
                _fail("generated", "must not contain symbolic links or junctions")
            if child.is_dir():
                pending.append(child)
                continue
            if not child.is_file():
                _fail("generated", "must contain regular files only")
            path = child.relative_to(root).as_posix()
            _validate_path(path, "generated")
            folded = path.casefold()
            if folded in result:
                _fail("generated", "contains case-insensitively duplicate paths")
            try:
                data = child.read_bytes()
            except OSError as exc:
                raise DirectGenerationError(
                    f"cannot read generated file {path}: {exc}"
                ) from exc
            _validate_file_content(path, data, path)
            total_bytes += len(data)
            if total_bytes > MAX_TOTAL_CONTENT_BYTES:
                _fail(
                    "generated",
                    f"total content exceeds {MAX_TOTAL_CONTENT_BYTES} bytes",
                )
            result[folded] = (path, data)
            if len(result) > MAX_FILES:
                _fail("generated", f"at most {MAX_FILES} files are allowed")
    return result


def _project_digest(files: Mapping[str, tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for _, (path, data) in sorted(files.items(), key=lambda item: item[0]):
        encoded_path = path.encode("utf-8")
        digest.update(encoded_path)
        digest.update(b"\x00")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _normalize_reserved_paths(values: Iterable[str]) -> frozenset[str]:
    result: set[str] = set()
    for index, value in enumerate(values):
        normalized = _validate_path(value, f"reserved_paths[{index}]")
        folded = normalized.casefold()
        if folded in result:
            _fail(f"reserved_paths[{index}]", "duplicate reserved path")
        result.add(folded)
    return frozenset(result)


def apply_file_plan(
    project_root: str | Path,
    document: Any,
    *,
    reserved_paths: Iterable[str] = (),
    manifest_path: str | Path | None = "artifacts/direct-generation-manifest.json",
) -> Mapping[str, Any]:
    """Validate and atomically apply a model file package under ``project_root``.

    All validation and state preconditions happen before the first mutation.
    Each replacement is staged in its destination directory and committed with
    an atomic filesystem operation.  ``reserved_paths`` are exact, case-insensitive
    project-relative paths owned by the host pipeline.

    The returned manifest contains actions, byte counts, and SHA-256 digests,
    never prompts, API keys, or generated source text.  When ``manifest_path``
    is not ``None`` the same manifest is atomically written beneath
    ``artifacts/``; model plans can never write into that directory.
    """

    plan = parse_file_plan(
        _normalized_plan_document(document)
        if isinstance(document, FilePlan)
        else document
    )
    supplied_root = Path(project_root)
    if _is_link_or_junction(supplied_root):
        _fail("project_root", "must not be a symbolic link or junction")
    try:
        root = supplied_root.resolve(strict=True)
    except OSError as exc:
        raise DirectGenerationError(
            f"project_root: cannot resolve directory: {exc}"
        ) from exc
    if not root.is_dir():
        _fail("project_root", "must be an existing directory")

    reserved = _normalize_reserved_paths(reserved_paths)
    manifest_relative = (
        _relative_manifest_path(root, manifest_path)
        if manifest_path is not None
        else None
    )
    if manifest_relative is not None and manifest_relative.casefold() in {
        operation.path.casefold() for operation in plan.files
    }:
        _fail("manifest_path", "must not be overwritten by the file plan")

    current_files = _scan_generated_files(root)
    final_files = dict(current_files)
    prepared: list[tuple[FileOperation, PurePosixPath, Path, bool, bytes | None]] = []
    entries: list[dict[str, Any]] = []
    for index, operation in enumerate(plan.files):
        if operation.path.casefold() in reserved:
            _fail(f"$.files[{index}].path", "is reserved by the host pipeline")
        relative = PurePosixPath(operation.path)
        _assert_no_links(root, relative, f"$.files[{index}].path")
        target = root.joinpath(*relative.parts)
        try:
            target.relative_to(root)
        except ValueError:
            _fail(f"$.files[{index}].path", "escapes the generated project")
        folded_path = operation.path.casefold()
        existing = current_files.get(folded_path)
        exists = existing is not None
        if existing is not None and existing[0] != operation.path:
            _fail(
                f"$.files[{index}].path",
                f"path casing differs from existing file {existing[0]!r}",
            )
        if target.exists() != exists:
            _fail(f"$.files[{index}].path", "filesystem state is case-ambiguous")
        if target.exists() and not target.is_file():
            _fail(f"$.files[{index}].path", "target is not a regular file")
        if operation.action == "create" and exists:
            _fail(f"$.files[{index}].action", "create target already exists")
        if operation.action in {"replace", "delete"} and not exists:
            _fail(
                f"$.files[{index}].action", f"{operation.action} target does not exist"
            )
        if operation.action == "delete":
            data = None
            applied_action = "delete"
            digest = None
            size = 0
            final_files.pop(folded_path, None)
        else:
            assert operation.content is not None
            data = operation.content.encode("utf-8")
            applied_action = "replace" if exists else "create"
            digest = _sha256(data)
            size = len(data)
            final_files[folded_path] = (operation.path, data)
        prepared.append((operation, relative, target, exists, data))
        entries.append(
            {
                "path": operation.path,
                "requested_action": operation.action,
                "applied_action": applied_action,
                "bytes": size,
                "sha256": digest,
            }
        )

    if len(final_files) > MAX_FILES:
        _fail("$.files", f"resulting project exceeds {MAX_FILES} generated files")
    final_total_bytes = sum(len(item[1]) for item in final_files.values())
    if final_total_bytes > MAX_TOTAL_CONTENT_BYTES:
        _fail(
            "$.files",
            f"resulting project exceeds {MAX_TOTAL_CONTENT_BYTES} content bytes",
        )

    normalized_plan = _normalized_plan_document(plan)
    project_files = [
        {"path": path, "bytes": len(data), "sha256": _sha256(data)}
        for _, (path, data) in sorted(final_files.items(), key=lambda item: item[0])
    ]
    manifest_base: dict[str, Any] = {
        "schema": DIRECT_MANIFEST_CONTRACT,
        "plan_sha256": _sha256(_canonical_json_bytes(normalized_plan)),
        "file_count": len(entries),
        "content_bytes": sum(entry["bytes"] for entry in entries),
        "files": entries,
        "project_sha256": _project_digest(final_files),
        "project_file_count": len(project_files),
        "project_content_bytes": final_total_bytes,
        "project_files": project_files,
    }
    manifest = dict(manifest_base)
    manifest["manifest_sha256"] = _sha256(_canonical_json_bytes(manifest_base))

    # Preflight the host-owned manifest path before committing model files.
    if manifest_relative is not None:
        relative = PurePosixPath(manifest_relative)
        _assert_no_links(root, relative, "manifest_path")
        manifest_target = root.joinpath(*relative.parts)
        if manifest_target.exists() and not manifest_target.is_file():
            _fail("manifest_path", "target is not a regular file")

    for operation, relative, target, existed, data in prepared:
        _assert_no_links(root, relative, operation.path)
        if operation.action == "delete":
            try:
                target.unlink()
            except OSError as exc:
                raise DirectGenerationError(
                    f"{operation.path}: cannot delete target: {exc}"
                ) from exc
            continue
        assert data is not None
        # Explicit create uses a hard-link commit so another writer cannot be
        # silently overwritten between preflight and commit.  Upsert/replace
        # use replace(2)/MoveFileEx semantics after rechecking their state.
        if operation.action == "replace" and not target.is_file():
            _fail(operation.path, "replace target disappeared before commit")
        create_only = operation.action == "create" or (
            operation.action == "upsert" and not existed
        )
        _atomic_write(root, relative, data, create_only=create_only)

    applied_files = _scan_generated_files(root)
    if applied_files != final_files:
        _fail(
            "generated", "post-write verification did not match the validated project"
        )
    if manifest_relative is not None:
        payload = _canonical_json_bytes(manifest) + b"\n"
        _atomic_write(
            root, PurePosixPath(manifest_relative), payload, create_only=False
        )
    return manifest


__all__ = [
    "ALLOWED_EXTENSIONS",
    "DIRECT_GENERATION_CONTRACT",
    "DIRECT_GENERATION_JSON_SCHEMA",
    "DIRECT_GENERATION_SCHEMA",
    "DIRECT_MANIFEST_CONTRACT",
    "DirectGenerationError",
    "FileOperation",
    "FilePlan",
    "MAX_FILE_BYTES",
    "MAX_FILES",
    "MAX_TOTAL_CONTENT_BYTES",
    "apply_file_plan",
    "parse_file_plan",
]
