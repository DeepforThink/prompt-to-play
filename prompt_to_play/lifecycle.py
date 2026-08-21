"""Deterministic request and revision lifecycle helpers for Prompt-to-Play.

The public CLI deliberately keeps generation inputs narrow: a natural-language
prompt and zero or more reference files.  Seeds are derived internally, while
timing, token usage, weights, and thresholds remain evaluation concerns.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # Support both ``python -m prompt_to_play.lifecycle`` and direct execution.
    from .contracts import canonical_json_bytes, derive_world_seed, document_sha256
except ImportError:  # pragma: no cover - exercised only by direct script use
    from contracts import canonical_json_bytes, derive_world_seed, document_sha256


REQUEST_SCHEMA = "prompt-to-play/request@1"
SELECTION_SCHEMA = "prompt-to-play/selection@1"
EVALUATION_SCHEMA = "prompt-to-play/evaluation@1"
SELECTION_STRATEGY = "hard-checks-then-weighted-score@1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class LifecycleError(ValueError):
    """Raised when request inputs or revision candidates are invalid."""


def _fail(path: str, message: str) -> None:
    raise LifecycleError(f"{path}: {message}")


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "expected an object")
    return value


def _nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        _fail(path, "expected a string")
    if not value.strip():
        _fail(path, "must not be empty")
    return value


def _integer(value: Any, path: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, "expected an integer")
    if value < minimum:
        _fail(path, f"must be >= {minimum}")
    return value


def _score(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "expected a number")
    score = float(value)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        _fail(path, "must be a finite number between 0 and 1")
    return score


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise LifecycleError(f"{path}: {exc}") from exc
    return digest.hexdigest()


def _display_name(path: Path, base_dir: Path) -> str:
    """Return a portable trace name without leaking an absolute machine path."""

    try:
        relative = path.resolve().relative_to(base_dir.resolve())
        name = relative.as_posix()
    except ValueError:
        name = path.name
    if not name or name in (".", ".."):  # pragma: no cover - guarded by is_file
        _fail(str(path), "cannot derive a reference name")
    return name


def build_request(
    prompt: str,
    reference_files: Sequence[str | Path] = (),
    *,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Build a canonical request from the complete external semantic input.

    Reference names are retained for provenance but are intentionally excluded
    from ``request_hash`` and seed derivation.  Thus moving or renaming an image
    without changing its bytes cannot change the generated world.
    """

    prompt = _nonempty_string(prompt, "prompt")
    root = Path.cwd() if base_dir is None else Path(base_dir)
    references: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for index, value in enumerate(reference_files):
        supplied = Path(value)
        file_path = supplied if supplied.is_absolute() else root / supplied
        if not file_path.is_file():
            _fail(f"references[{index}]", f"file does not exist: {supplied}")
        name = _display_name(file_path, root)
        if name in seen_names:
            _fail(f"references[{index}]", f"duplicate reference name {name!r}")
        seen_names.add(name)
        references.append({"name": name, "sha256": _sha256_file(file_path)})

    # Multi-view order can carry meaning (for example, "first image" in the
    # prompt), so preserve the exact user-supplied reference order.  Names are
    # provenance only and do not participate in the semantic hash.
    semantic_references = [{"sha256": item["sha256"]} for item in references]
    semantic_payload = {
        "prompt": prompt,
        "reference_sha256": [item["sha256"] for item in semantic_references],
    }
    request_hash = document_sha256(semantic_payload)
    seed = derive_world_seed(prompt, semantic_references)
    # This assertion couples the request hash definition to the WorldSpec seed
    # contract and detects accidental divergence during later maintenance.
    if seed != int(request_hash[:8], 16):  # pragma: no cover - contract invariant
        raise RuntimeError("request hash and WorldSpec seed derivation diverged")
    return {
        "schema": REQUEST_SCHEMA,
        "prompt": prompt,
        "references": references,
        "request_hash": request_hash,
        "seed": seed,
    }


def _evaluation_summary(document: Any, source: str) -> dict[str, Any]:
    report = _mapping(document, source)
    if report.get("schema") != EVALUATION_SCHEMA:
        _fail(f"{source}.schema", f"expected {EVALUATION_SCHEMA!r}")
    run_id = _nonempty_string(report.get("run_id"), f"{source}.run_id")
    world_id = _nonempty_string(report.get("world_id"), f"{source}.world_id")
    world_sha256 = _nonempty_string(
        report.get("world_sha256"), f"{source}.world_sha256"
    )
    if not SHA256_RE.fullmatch(world_sha256):
        _fail(f"{source}.world_sha256", "expected a lowercase SHA-256 digest")
    iteration = _integer(report.get("iteration"), f"{source}.iteration")

    checks = report.get("checks")
    if not isinstance(checks, list) or not checks:
        _fail(f"{source}.checks", "expected a non-empty array")
    hard_results: list[bool] = []
    seen_check_ids: set[str] = set()
    for index, value in enumerate(checks):
        path = f"{source}.checks[{index}]"
        check = _mapping(value, path)
        check_id = _nonempty_string(check.get("id"), f"{path}.id")
        if check_id in seen_check_ids:
            _fail(f"{path}.id", f"duplicate check ID {check_id!r}")
        seen_check_ids.add(check_id)
        kind = check.get("kind")
        if kind not in ("hard", "soft"):
            _fail(f"{path}.kind", "expected 'hard' or 'soft'")
        passed = check.get("passed")
        if not isinstance(passed, bool):
            _fail(f"{path}.passed", "expected a boolean")
        if kind == "hard":
            hard_results.append(passed)
    if not hard_results:
        _fail(f"{source}.checks", "at least one hard check is required")

    result = _mapping(report.get("result"), f"{source}.result")
    weighted_score = _score(
        result.get("weighted_score"), f"{source}.result.weighted_score"
    )
    try:
        evaluation_sha256 = document_sha256(document)
    except ValueError as exc:
        raise LifecycleError(f"{source}: evaluation is not canonical JSON: {exc}") from exc
    return {
        "source": source,
        "evaluation_sha256": evaluation_sha256,
        "run_id": run_id,
        "world_id": world_id,
        "world_sha256": world_sha256,
        "iteration": iteration,
        "all_hard_checks_passed": all(hard_results),
        "weighted_score": weighted_score,
    }


def _rank_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    """Lower tuple values are better; the last fields make ties deterministic."""

    return (
        not candidate["all_hard_checks_passed"],
        -candidate["weighted_score"],
        candidate["iteration"],
        candidate["evaluation_sha256"],
        candidate["source"],
    )


def select_best_revision(
    evaluations: Sequence[tuple[str, Any]],
) -> dict[str, Any]:
    """Select the best evaluation without treating runtime or tokens as gates."""

    if not evaluations:
        _fail("evaluations", "at least one evaluation is required")
    candidates = []
    for index, (source, document) in enumerate(evaluations):
        source = _nonempty_string(source, f"evaluations[{index}].source")
        candidates.append(_evaluation_summary(document, source))
    identities = {(item["run_id"], item["world_id"]) for item in candidates}
    if len(identities) != 1:
        _fail("evaluations", "all revisions must have the same run_id and world_id")

    ranked = sorted(candidates, key=_rank_key)
    selected = ranked[0]
    return {
        "schema": SELECTION_SCHEMA,
        "strategy": SELECTION_STRATEGY,
        "run_id": selected["run_id"],
        "world_id": selected["world_id"],
        "selected": selected,
        "candidates": [dict(item, rank=index + 1) for index, item in enumerate(ranked)],
    }


def _load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise LifecycleError(f"{path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise LifecycleError(f"{path}:{exc.lineno}:{exc.colno}: {exc.msg}") from exc


def _write_canonical_json(path: Path, document: Any) -> None:
    try:
        payload = canonical_json_bytes(document) + b"\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    except OSError as exc:
        raise LifecycleError(f"{path}: {exc}") from exc


def _source_name(path: Path, base_dir: Path) -> str:
    return _display_name(path, base_dir)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create Prompt-to-Play requests and select evaluated revisions"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    request_parser = subparsers.add_parser(
        "create-request", help="create a canonical request.json"
    )
    request_parser.add_argument("--prompt", required=True)
    request_parser.add_argument(
        "--reference",
        action="append",
        default=[],
        metavar="FILE",
        help="reference image/file; repeat for multiple references",
    )
    request_parser.add_argument("--output", required=True)

    selection_parser = subparsers.add_parser(
        "select-best", help="select the best evaluated revision"
    )
    selection_parser.add_argument("evaluations", nargs="+", metavar="EVALUATION")
    selection_parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "create-request":
            output = Path(args.output)
            request = build_request(args.prompt, args.reference)
            _write_canonical_json(output, request)
            print(f"ok: {output}")
        elif args.command == "select-best":
            base_dir = Path.cwd()
            inputs: list[tuple[str, Any]] = []
            for value in args.evaluations:
                path = Path(value)
                inputs.append((_source_name(path, base_dir), _load_json(path)))
            selection = select_best_revision(inputs)
            output = Path(args.output)
            _write_canonical_json(output, selection)
            print(f"ok: {output}")
        else:  # pragma: no cover - argparse prevents this branch
            raise AssertionError(f"unsupported command {args.command}")
    except LifecycleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
