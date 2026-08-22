"""Deterministic, WorldSpec-free request primitives for direct generation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Sequence


REQUEST_SCHEMA = "prompt-to-play/request@1"


class DirectRequestError(ValueError):
    """Raised when prompt/reference inputs cannot form a direct request."""


def canonical_json_bytes(document: Any) -> bytes:
    """Encode canonical UTF-8 JSON used by direct-runtime evidence hashes."""

    try:
        text = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DirectRequestError(f"document is not canonical JSON: {exc}") from exc
    return text.encode("utf-8")


def document_sha256(document: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise DirectRequestError(f"cannot read reference image: {path}") from exc
    return digest.hexdigest()


def _display_name(path: Path, base_dir: Path) -> str:
    try:
        name = path.resolve().relative_to(base_dir.resolve()).as_posix()
    except ValueError:
        name = path.name
    if not name or name in (".", ".."):
        raise DirectRequestError(f"cannot derive a reference name: {path}")
    return name


def build_request(
    prompt: str,
    reference_files: Sequence[str | Path] = (),
    *,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Bind the only semantic inputs and derive a stable internal seed.

    Reference order is significant. File names are provenance only, so moving
    an unchanged image does not alter the request hash or seed.
    """

    if not isinstance(prompt, str) or not prompt.strip():
        raise DirectRequestError("prompt: must be a non-empty string")
    root = Path.cwd() if base_dir is None else Path(base_dir)
    references: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for index, value in enumerate(reference_files):
        supplied = Path(value)
        file_path = supplied if supplied.is_absolute() else root / supplied
        if not file_path.is_file():
            raise DirectRequestError(
                f"references[{index}]: file does not exist: {supplied}"
            )
        name = _display_name(file_path, root)
        if name in seen_names:
            raise DirectRequestError(
                f"references[{index}]: duplicate reference name {name!r}"
            )
        seen_names.add(name)
        references.append({"name": name, "sha256": _sha256_file(file_path)})

    semantic_payload = {
        "prompt": prompt,
        "reference_sha256": [item["sha256"] for item in references],
    }
    request_hash = document_sha256(semantic_payload)
    return {
        "schema": REQUEST_SCHEMA,
        "prompt": prompt,
        "references": references,
        "request_hash": request_hash,
        "seed": int(request_hash[:8], 16),
    }


__all__ = [
    "DirectRequestError",
    "REQUEST_SCHEMA",
    "build_request",
    "canonical_json_bytes",
    "document_sha256",
]
