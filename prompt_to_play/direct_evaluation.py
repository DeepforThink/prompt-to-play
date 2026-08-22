"""Strict visual-feedback contract for directly generated Godot projects.

This module deliberately has no dependency on the legacy WorldSpec evaluator.
Direct projects are repaired at the file level, so visual issues cannot name a
semantic entity and the trusted host exclusively owns the acceptance gate.
"""

from __future__ import annotations

import math
import re
from typing import Any, Mapping


DEFAULT_MIN_SCENE_SIMILARITY = 0.76
ISSUE_SEVERITIES = ("info", "minor", "major", "blocker")
BLOCKING_SEVERITIES = frozenset(("major", "blocker"))
STABLE_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
VISUAL_SCORE_DIMENSIONS = (
    "prompt_fidelity",
    "composition",
    "visual_coherence",
    "detail_density",
    "lighting_materials",
    "gameplay_readability",
)
VISUAL_SCORE_WEIGHTS: Mapping[str, float] = {
    "prompt_fidelity": 0.30,
    "composition": 0.15,
    "visual_coherence": 0.15,
    "detail_density": 0.15,
    "lighting_materials": 0.10,
    "gameplay_readability": 0.15,
}
VISUAL_SCORE_MINIMUMS: Mapping[str, float] = {
    "prompt_fidelity": 0.70,
    "composition": 0.60,
    "visual_coherence": 0.65,
    "detail_density": 0.58,
    "lighting_materials": 0.58,
    "gameplay_readability": 0.65,
}


class DirectEvaluationError(ValueError):
    """Raised when direct-project visual feedback violates its host contract."""


def _object(properties: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(properties),
        "additionalProperties": False,
    }


DIRECT_VISUAL_ISSUE_JSON_SCHEMA = _object(
    {
        "code": {
            "type": "string",
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
        },
        "severity": {"type": "string", "enum": list(ISSUE_SEVERITIES)},
        "entity_id": {"type": "null"},
        "message": {"type": "string", "minLength": 1},
        "suggested_fix": {"type": "string", "minLength": 1},
    }
)

DIRECT_VISUAL_FEEDBACK_JSON_SCHEMA = _object(
    {
        "scores": _object(
            {
                dimension: {"type": "number", "minimum": 0, "maximum": 1}
                for dimension in VISUAL_SCORE_DIMENSIONS
            }
        ),
        "issues": {
            "type": "array",
            "items": DIRECT_VISUAL_ISSUE_JSON_SCHEMA,
            "minItems": 0,
            "maxItems": 32,
        },
    }
)


def _nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise DirectEvaluationError(f"{path}: expected a string")
    if not value.strip():
        raise DirectEvaluationError(f"{path}: must not be empty")
    return value


def _similarity_threshold(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DirectEvaluationError("min_scene_similarity: expected a number")
    threshold = float(value)
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise DirectEvaluationError(
            "min_scene_similarity: expected a value from 0 to 1"
        )
    return threshold


def _unit_score(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DirectEvaluationError(f"{path}: expected a number")
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise DirectEvaluationError(f"{path}: expected a value from 0 to 1")
    return score


def validate_direct_visual_feedback(
    document: Any,
    *,
    min_scene_similarity: float = DEFAULT_MIN_SCENE_SIMILARITY,
) -> dict[str, Any]:
    """Validate feedback and enforce the trusted host's visual-only gate.

    The model reports six observable visual scores and actionable issues.  It
    never reports an overall score or acceptance decision.  The trusted host
    derives both from fixed weights, per-dimension floors, the configured
    aggregate threshold, and absence of major/blocker issues.
    """

    threshold = _similarity_threshold(min_scene_similarity)
    if not isinstance(document, Mapping):
        raise DirectEvaluationError("direct visual feedback: expected an object")
    required = {"scores", "issues"}
    missing = sorted(required - set(document))
    extra = sorted(set(document) - required)
    if missing:
        raise DirectEvaluationError(
            "direct visual feedback: missing keys: " + ", ".join(missing)
        )
    if extra:
        raise DirectEvaluationError(
            "direct visual feedback: unknown keys: " + ", ".join(extra)
        )

    raw_scores = document["scores"]
    if not isinstance(raw_scores, Mapping):
        raise DirectEvaluationError("direct visual feedback.scores: expected an object")
    missing_scores = sorted(set(VISUAL_SCORE_DIMENSIONS) - set(raw_scores))
    extra_scores = sorted(set(raw_scores) - set(VISUAL_SCORE_DIMENSIONS))
    if missing_scores:
        raise DirectEvaluationError(
            "direct visual feedback.scores: missing keys: " + ", ".join(missing_scores)
        )
    if extra_scores:
        raise DirectEvaluationError(
            "direct visual feedback.scores: unknown keys: " + ", ".join(extra_scores)
        )
    scores = {
        dimension: _unit_score(
            raw_scores[dimension], f"direct visual feedback.scores.{dimension}"
        )
        for dimension in VISUAL_SCORE_DIMENSIONS
    }
    if not isinstance(document["issues"], list):
        raise DirectEvaluationError("direct visual feedback.issues: expected an array")
    if len(document["issues"]) > 32:
        raise DirectEvaluationError(
            "direct visual feedback.issues: at most 32 issues are allowed"
        )

    issues: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    issue_keys = {"code", "severity", "entity_id", "message", "suggested_fix"}
    for index, value in enumerate(document["issues"]):
        path = f"direct visual feedback.issues[{index}]"
        if not isinstance(value, Mapping):
            raise DirectEvaluationError(f"{path}: expected an object")
        missing_issue = sorted(issue_keys - set(value))
        extra_issue = sorted(set(value) - issue_keys)
        if missing_issue:
            raise DirectEvaluationError(
                f"{path}: missing keys: {', '.join(missing_issue)}"
            )
        if extra_issue:
            raise DirectEvaluationError(
                f"{path}: unknown keys: {', '.join(extra_issue)}"
            )
        code = _nonempty_string(value["code"], f"{path}.code")
        if STABLE_CODE_RE.fullmatch(code) is None:
            raise DirectEvaluationError(f"{path}.code: invalid stable issue code")
        if code in seen_codes:
            raise DirectEvaluationError(f"{path}.code: duplicate issue code {code!r}")
        seen_codes.add(code)
        severity = value["severity"]
        if severity not in ISSUE_SEVERITIES:
            raise DirectEvaluationError(
                f"{path}.severity: expected one of: {', '.join(ISSUE_SEVERITIES)}"
            )
        if value["entity_id"] is not None:
            raise DirectEvaluationError(
                f"{path}.entity_id: must be null for direct file generation"
            )
        issues.append(
            {
                "code": code,
                "severity": severity,
                "entity_id": None,
                "message": _nonempty_string(value["message"], f"{path}.message"),
                "suggested_fix": _nonempty_string(
                    value["suggested_fix"], f"{path}.suggested_fix"
                ),
            }
        )

    scene_similarity = round(
        sum(
            scores[name] * VISUAL_SCORE_WEIGHTS[name]
            for name in VISUAL_SCORE_DIMENSIONS
        ),
        6,
    )
    floors_passed = all(
        scores[name] >= VISUAL_SCORE_MINIMUMS[name] for name in VISUAL_SCORE_DIMENSIONS
    )
    accepted = (
        scene_similarity >= threshold
        and floors_passed
        and not any(issue["severity"] in BLOCKING_SEVERITIES for issue in issues)
    )
    if not accepted and not issues:
        raise DirectEvaluationError(
            "direct visual feedback.issues: rejected feedback requires an "
            "actionable issue"
        )
    return {
        "scores": scores,
        "scene_similarity": scene_similarity,
        "accepted": accepted,
        "issues": issues,
    }


__all__ = [
    "BLOCKING_SEVERITIES",
    "DEFAULT_MIN_SCENE_SIMILARITY",
    "DIRECT_VISUAL_FEEDBACK_JSON_SCHEMA",
    "DIRECT_VISUAL_ISSUE_JSON_SCHEMA",
    "DirectEvaluationError",
    "ISSUE_SEVERITIES",
    "STABLE_CODE_RE",
    "VISUAL_SCORE_DIMENSIONS",
    "VISUAL_SCORE_MINIMUMS",
    "VISUAL_SCORE_WEIGHTS",
    "validate_direct_visual_feedback",
]
