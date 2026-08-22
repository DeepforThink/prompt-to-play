from __future__ import annotations

import ast
import copy
import math
import unittest
from pathlib import Path

from prompt_to_play import direct_evaluation as direct


def issue(
    *,
    code: str = "missing_vehicle",
    severity: str = "major",
    entity_id=None,
):
    return {
        "code": code,
        "severity": severity,
        "entity_id": entity_id,
        "message": "The requested vehicle is not recognisable.",
        "suggested_fix": "Improve its generated mesh and camera framing.",
    }


def scores(value: float = 0.8, **overrides: float) -> dict[str, float]:
    result = {name: value for name in direct.VISUAL_SCORE_DIMENSIONS}
    result.update(overrides)
    return result


class DirectVisualFeedbackSchemaTests(unittest.TestCase):
    def test_schema_is_strict_and_has_no_semantic_entity_identifier(self):
        schema = direct.DIRECT_VISUAL_FEEDBACK_JSON_SCHEMA
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), {"scores", "issues"})
        self.assertEqual(
            set(schema["properties"]["scores"]["required"]),
            set(direct.VISUAL_SCORE_DIMENSIONS),
        )
        issue_schema = schema["properties"]["issues"]["items"]
        self.assertFalse(issue_schema["additionalProperties"])
        self.assertEqual(issue_schema["properties"]["entity_id"], {"type": "null"})

    def test_module_has_no_legacy_world_evaluation_imports(self):
        source_path = Path(direct.__file__)
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        for forbidden in ("evaluator", "contracts", "planner"):
            self.assertFalse(
                any(
                    name == forbidden or name.endswith(f".{forbidden}")
                    for name in imports
                ),
                imports,
            )


class DirectVisualFeedbackValidationTests(unittest.TestCase):
    def test_host_gate_accepts_only_threshold_without_blocking_issue(self):
        accepted = direct.validate_direct_visual_feedback(
            {
                "scores": scores(0.8),
                "issues": [issue(severity="minor")],
            },
            min_scene_similarity=0.8,
        )
        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["scene_similarity"], 0.8)

        blocked = direct.validate_direct_visual_feedback(
            {
                "scores": scores(0.99),
                "issues": [issue(severity="blocker")],
            },
            min_scene_similarity=0.8,
        )
        self.assertFalse(blocked["accepted"])

    def test_model_cannot_supply_host_owned_overall_or_acceptance(self):
        for field, value in (("accepted", True), ("scene_similarity", 0.99)):
            feedback = {
                "scores": scores(0.9),
                "issues": [],
                field: value,
            }
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    direct.DirectEvaluationError, "unknown keys"
                ):
                    direct.validate_direct_visual_feedback(feedback)

    def test_host_enforces_each_quality_floor_not_only_weighted_average(self):
        feedback = direct.validate_direct_visual_feedback(
            {
                "scores": scores(0.95, detail_density=0.2),
                "issues": [issue(severity="minor")],
            },
            min_scene_similarity=0.7,
        )
        self.assertGreater(feedback["scene_similarity"], 0.7)
        self.assertFalse(feedback["accepted"])

    def test_similarity_and_threshold_must_be_finite_numbers_in_unit_interval(self):
        base = {
            "scores": scores(0.5),
            "issues": [issue()],
        }
        for value in (-0.01, 1.01, math.nan, math.inf, True, "0.5"):
            feedback = copy.deepcopy(base)
            feedback["scores"]["composition"] = value
            with self.subTest(score=value):
                with self.assertRaises(direct.DirectEvaluationError):
                    direct.validate_direct_visual_feedback(feedback)
        for threshold in (-0.01, 1.01, math.nan, math.inf, True, "0.8"):
            with self.subTest(threshold=threshold):
                with self.assertRaises(direct.DirectEvaluationError):
                    direct.validate_direct_visual_feedback(
                        base, min_scene_similarity=threshold
                    )

    def test_issues_have_exact_fields_stable_unique_codes_and_null_entity(self):
        base = {
            "scores": scores(0.4),
            "issues": [issue()],
        }
        invalid_documents = []

        unknown = copy.deepcopy(base)
        unknown["issues"][0]["tokens"] = 10
        invalid_documents.append(unknown)

        unstable = copy.deepcopy(base)
        unstable["issues"][0]["code"] = "not stable!"
        invalid_documents.append(unstable)

        duplicate = copy.deepcopy(base)
        duplicate["issues"].append(issue())
        invalid_documents.append(duplicate)

        entity = copy.deepcopy(base)
        entity["issues"][0]["entity_id"] = "old_worldspec_entity"
        invalid_documents.append(entity)

        empty_fix = copy.deepcopy(base)
        empty_fix["issues"][0]["suggested_fix"] = "   "
        invalid_documents.append(empty_fix)

        for document in invalid_documents:
            with self.subTest(document=document):
                with self.assertRaises(direct.DirectEvaluationError):
                    direct.validate_direct_visual_feedback(document)

    def test_rejected_feedback_requires_an_actionable_issue(self):
        with self.assertRaisesRegex(
            direct.DirectEvaluationError, "requires an actionable issue"
        ):
            direct.validate_direct_visual_feedback(
                {"scores": scores(0.1), "issues": []},
                min_scene_similarity=0.8,
            )

    def test_top_level_contract_rejects_missing_and_unknown_fields(self):
        with self.assertRaisesRegex(direct.DirectEvaluationError, "missing keys"):
            direct.validate_direct_visual_feedback({"scores": scores(0.9)})
        with self.assertRaisesRegex(direct.DirectEvaluationError, "unknown keys"):
            direct.validate_direct_visual_feedback(
                {
                    "scores": scores(0.9),
                    "issues": [],
                    "world_spec": {},
                }
            )


if __name__ == "__main__":
    unittest.main()
