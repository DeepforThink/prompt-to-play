import copy
import json
import tempfile
import unittest
from pathlib import Path

from prompt_to_play import contracts, evaluator


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_WORLD = ROOT / "prompt_to_play" / "examples" / "world.json"


def read_world():
    return json.loads(EXAMPLE_WORLD.read_text(encoding="utf-8"))


def rejected_feedback(entity_id="pipe_cluster"):
    return {
        "scene_similarity": 0.52,
        "accepted": False,
        "issues": [
            {
                "code": "flat_landmark",
                "severity": "major",
                "entity_id": entity_id,
                "message": "The requested landmark is not visually recognisable.",
                "suggested_fix": "Move and enlarge the landmark near the camera.",
            }
        ],
    }


class FakeProvider:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def generate_json(
        self,
        messages,
        *,
        json_schema,
        schema_name,
        image_paths=(),
    ):
        self.calls.append(
            {
                "messages": copy.deepcopy(messages),
                "json_schema": copy.deepcopy(json_schema),
                "schema_name": schema_name,
                "image_paths": list(image_paths),
            }
        )
        return copy.deepcopy(self.response)


class VisualEvaluationAgentTests(unittest.TestCase):
    def test_agent_sends_screenshots_and_returns_only_strict_visual_feedback(self):
        world = read_world()
        response = {
            "scene_similarity": 0.86,
            "accepted": True,
            "issues": [
                {
                    "code": "minor_fog",
                    "severity": "minor",
                    "entity_id": None,
                    "message": "The skyline is slightly washed out.",
                    "suggested_fix": "Improve foreground contrast.",
                }
            ],
        }
        provider = FakeProvider(response)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.png"
            reference.write_bytes(b"reference pixels")
            screenshot = root / "overview.png"
            screenshot.write_bytes(b"rendered pixels")
            agent = evaluator.VisualEvaluationAgent(
                provider,
                project_root=root,
                min_scene_similarity=0.8,
            )
            result = agent.evaluate(
                world["brief"]["text"],
                world,
                ["overview.png"],
                reference_image_paths=["reference.png"],
            )

        self.assertEqual(agent.name, "VisualEvaluationAgent")
        self.assertEqual(agent.role, "visual_evaluator")
        self.assertEqual(result, response)
        self.assertEqual(len(provider.calls), 1)
        call = provider.calls[0]
        self.assertEqual(call["schema_name"], "prompt_to_play_visual_feedback")
        self.assertEqual(
            call["image_paths"], [reference.resolve(), screenshot.resolve()]
        )
        self.assertFalse(call["json_schema"]["additionalProperties"])
        self.assertEqual(
            set(call["json_schema"]["properties"]),
            {"scene_similarity", "accepted", "issues"},
        )
        request = json.loads(call["messages"][1]["content"])
        self.assertEqual(request["agent"], "VisualEvaluationAgent")
        self.assertEqual(request["role"], "visual_evaluator")
        self.assertEqual(request["original_prompt"], world["brief"]["text"])
        self.assertEqual(request["reference_image_count"], 1)
        self.assertEqual(request["screenshot_count"], 1)
        self.assertEqual(
            request["attached_image_order"],
            "reference_images_then_rendered_screenshots",
        )
        self.assertNotIn("timing_ms", request)
        self.assertNotIn("tokens", request)
        self.assertNotIn("weighted_score", request)

    def test_host_rejects_model_acceptance_that_disagrees_with_visual_gate(self):
        world = read_world()
        feedback = {
            "scene_similarity": 0.9,
            "accepted": False,
            "issues": [
                {
                    "code": "tiny_note",
                    "severity": "minor",
                    "entity_id": None,
                    "message": "A small detail could improve.",
                    "suggested_fix": "Add a decal.",
                }
            ],
        }
        with self.assertRaisesRegex(evaluator.EvaluatorError, "host visual gate"):
            evaluator.validate_visual_feedback(feedback, world)

    def test_strict_feedback_rejects_metrics_and_unknown_entity_ids(self):
        world = read_world()
        with_metric = copy.deepcopy(rejected_feedback())
        with_metric["tokens"] = {"total": 1}
        with self.assertRaisesRegex(evaluator.EvaluatorError, "unknown keys: tokens"):
            evaluator.validate_visual_feedback(with_metric, world)

        unknown_entity = rejected_feedback("invented_by_model")
        with self.assertRaisesRegex(evaluator.EvaluatorError, "unknown WorldSpec entity"):
            evaluator.validate_visual_feedback(unknown_entity, world)


class RepairAgentTests(unittest.TestCase):
    def test_revise_world_uses_world_schema_images_and_authoritative_fields(self):
        current = read_world()
        revised = copy.deepcopy(current)
        revised["props"][0]["position"] = [9, 1, -3]
        provider = FakeProvider(revised)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.png"
            reference.write_bytes(b"reference")
            screenshot = root / "overview.png"
            screenshot.write_bytes(b"render")
            agent = evaluator.RepairAgent(provider, project_root=root)
            result = agent.revise_world(
                current["brief"]["text"],
                current,
                rejected_feedback(),
                [screenshot],
                reference_image_paths=[reference],
                structural_report={
                    "checks": [
                        {
                            "id": "world_graph_connected",
                            "passed": False,
                            "message": "One region is unreachable.",
                        }
                    ]
                },
            )

        self.assertEqual(agent.name, "RepairAgent")
        self.assertEqual(agent.role, "repair")
        self.assertEqual(result, revised)
        call = provider.calls[0]
        self.assertEqual(call["schema_name"], "prompt_to_play_revised_world_spec")
        self.assertEqual(
            call["image_paths"], [reference.resolve(), screenshot.resolve()]
        )
        self.assertEqual(
            call["json_schema"]["properties"]["world_id"]["const"],
            current["world_id"],
        )
        self.assertEqual(
            call["json_schema"]["properties"]["seed"]["const"], current["seed"]
        )
        request = json.loads(call["messages"][1]["content"])
        self.assertEqual(request["agent"], "RepairAgent")
        self.assertEqual(request["role"], "repair")
        self.assertEqual(request["current_world"], current)
        self.assertEqual(request["visual_feedback"], rejected_feedback())
        self.assertEqual(
            request["structural_failures"],
            [
                {
                    "id": "world_graph_connected",
                    "message": "One region is unreachable.",
                }
            ],
        )
        self.assertEqual(request["reference_image_count"], 1)
        self.assertEqual(request["screenshot_count"], 1)
        self.assertNotIn("timing_ms", request)
        self.assertNotIn("tokens", request)

    def test_revise_world_rejects_authoritative_or_semantically_invalid_output(self):
        current = read_world()
        changed_id = copy.deepcopy(current)
        changed_id["world_id"] = "model_changed_identity"
        provider = FakeProvider(changed_id)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            screenshot = root / "shot.png"
            screenshot.write_bytes(b"render")
            with self.assertRaisesRegex(evaluator.EvaluatorError, "authoritative"):
                evaluator.revise_world(
                    current["brief"]["text"],
                    current,
                    rejected_feedback(),
                    [screenshot],
                    provider=provider,
                    project_root=root,
                )

            disconnected = copy.deepcopy(current)
            disconnected["roads"] = []
            with self.assertRaisesRegex(evaluator.EvaluatorError, "semantically invalid"):
                evaluator.revise_world(
                    current["brief"]["text"],
                    current,
                    rejected_feedback(),
                    [screenshot],
                    provider=FakeProvider(disconnected),
                    project_root=root,
                )

    def test_repair_returns_complete_revision_and_valid_patch(self):
        current = read_world()
        revised = copy.deepcopy(current)
        revised["lights"][1]["energy"] = 7
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            screenshot = root / "shot.png"
            screenshot.write_bytes(b"render")
            agent = evaluator.RepairAgent(FakeProvider(revised), project_root=root)
            result, patch = agent.repair(
                current["brief"]["text"],
                current,
                rejected_feedback(),
                [screenshot],
                iteration=1,
            )
        self.assertEqual(result, revised)
        self.assertEqual(contracts.apply_patch(current, patch), revised)


class DiffToPatchTests(unittest.TestCase):
    def test_diff_is_deterministic_allowlisted_and_reproduces_revision(self):
        current = read_world()
        revised = copy.deepcopy(current)
        revised["props"][0]["position"] = [8, 0, -2]
        revised["props"].pop(1)
        revised["props"].append(
            {
                "id": "warning_sign",
                "region": "dock",
                "prefab": "prop/warning_sign",
                "position": [71, 2, 7],
                "rotation_deg": [0, 15, 0],
                "scale": [1, 1, 1],
            }
        )
        revised["interactions"]["exit"]["position"] = [79, 3, 10]

        first = evaluator.diff_to_patch(current, revised, 1)
        second = evaluator.diff_to_patch(current, revised, 1)

        self.assertEqual(first, second)
        contracts.validate_patch(first, current)
        self.assertEqual(contracts.apply_patch(current, first), revised)
        operations = {(op["op"], op["target"]["id"]) for op in first["operations"]}
        self.assertIn(("update", "pipe_cluster"), operations)
        self.assertIn(("remove", "rusted_crates"), operations)
        self.assertIn(("upsert", "warning_sign"), operations)
        self.assertIn(("update", "exit_gate"), operations)

    def test_diff_rejects_nonpatchable_style_and_unrepresentable_reordering(self):
        current = read_world()
        style_change = copy.deepcopy(current)
        style_change["style"]["fog_density"] = 0.25
        with self.assertRaisesRegex(evaluator.EvaluatorError, "nonpatchable"):
            evaluator.diff_to_patch(current, style_change, 1)

        reordered = copy.deepcopy(current)
        reordered["props"].reverse()
        with self.assertRaisesRegex(evaluator.EvaluatorError, "no patchable changes"):
            evaluator.diff_to_patch(current, reordered, 1)

    def test_diff_rejects_empty_revision(self):
        world = read_world()
        with self.assertRaisesRegex(evaluator.EvaluatorError, "no patchable changes"):
            evaluator.diff_to_patch(world, copy.deepcopy(world), 1)


if __name__ == "__main__":
    unittest.main()
