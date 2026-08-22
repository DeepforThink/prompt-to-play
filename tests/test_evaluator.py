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


def camera_evaluation(score, camera_id="overview"):
    return {
        "camera_id": camera_id,
        "prompt_alignment": score,
        "composition": score,
        "lighting_materials": score,
        "landmark_readability": score,
        "camera_coverage": score,
        "visible_defects": 1 - score,
    }


def suggested_change(
    target_kind="prop",
    target_id="pipe_cluster",
    field="scale",
    instruction="Increase the landmark scale to make it dominate the foreground.",
    expected_effect="The requested landmark becomes immediately recognisable.",
):
    return {
        "target_kind": target_kind,
        "target_id": target_id,
        "field": field,
        "instruction": instruction,
        "expected_effect": expected_effect,
    }


def rejected_observation(entity_id="pipe_cluster", camera_id="overview"):
    return {
        "camera_evaluations": [camera_evaluation(0.52, camera_id)],
        "issues": [
            {
                "code": "flat_landmark",
                "severity": "major",
                "entity_id": entity_id,
                "camera_id": camera_id,
                "message": "The requested landmark is not visually recognisable.",
                "suggested_fix": "Move and enlarge the landmark near the camera.",
                "domain": "layout",
                "suggested_changes": [
                    suggested_change(target_id=entity_id),
                ],
            }
        ],
    }


def rejected_feedback(entity_id="pipe_cluster", camera_id="overview"):
    return evaluator.validate_visual_feedback(
        rejected_observation(entity_id, camera_id),
        read_world(),
        expected_camera_ids=[camera_id],
    )


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


class SequenceProvider(FakeProvider):
    def __init__(self, responses):
        super().__init__(None)
        self.responses = list(responses)

    def generate_json(self, messages, **kwargs):
        self.response = self.responses[len(self.calls)]
        return super().generate_json(messages, **kwargs)


class VisualEvaluationAgentTests(unittest.TestCase):
    def test_agent_distinguishes_provider_failure_from_output_contract_failure(self):
        world = read_world()

        class FailingProvider:
            def generate_json(self, *_args, **_kwargs):
                raise RuntimeError("sk-sensitive-provider-detail")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            screenshot = root / "overview.png"
            screenshot.write_bytes(b"rendered pixels")

            provider_agent = evaluator.VisualEvaluationAgent(
                FailingProvider(), project_root=root
            )
            with self.assertRaises(
                evaluator.VisualEvaluationProviderError
            ) as provider_context:
                provider_agent.evaluate(
                    world["brief"]["text"], world, [screenshot]
                )
            self.assertNotIn(
                "sk-sensitive-provider-detail", str(provider_context.exception)
            )

            contract_agent = evaluator.VisualEvaluationAgent(
                FakeProvider({"provider_secret": "sk-sensitive-output"}),
                project_root=root,
            )
            with self.assertRaises(evaluator.VisualFeedbackContractError):
                contract_agent.evaluate(
                    world["brief"]["text"], world, [screenshot]
                )

    def test_agent_retries_one_invalid_contract_with_host_constraints(self):
        world = read_world()
        corrected = rejected_observation()
        provider = SequenceProvider(
            [
                rejected_observation("invented_by_model"),
                corrected,
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            screenshot = root / "overview.png"
            screenshot.write_bytes(b"rendered pixels")
            agent = evaluator.VisualEvaluationAgent(provider, project_root=root)
            result = agent.evaluate(world["brief"]["text"], world, [screenshot])

        self.assertEqual(result["issues"], corrected["issues"])
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(
            provider.calls[1]["schema_name"],
            "prompt_to_play_visual_feedback_correction",
        )
        correction = json.loads(provider.calls[1]["messages"][1]["content"])
        self.assertIn("unknown WorldSpec entity", correction["validation_error"])
        self.assertIn("pipe_cluster", correction["allowed_targets"]["prop"]["ids"])
        self.assertEqual(correction["required_camera_ids"], ["overview"])

    def test_agent_sends_screenshots_and_returns_only_strict_visual_feedback(self):
        world = read_world()
        response = {
            "camera_evaluations": [camera_evaluation(0.86)],
            "issues": [
                {
                    "code": "minor_fog",
                    "severity": "minor",
                    "entity_id": None,
                    "camera_id": "overview",
                    "message": "The skyline is slightly washed out.",
                    "suggested_fix": "Improve foreground contrast.",
                    "domain": "lighting_camera",
                    "suggested_changes": [
                        suggested_change(
                            "light",
                            "cloud_sun",
                            "energy",
                            "Reduce the directional light energy to restore foreground contrast.",
                            "Foreground silhouettes separate clearly from the skyline.",
                        )
                    ],
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
        self.assertEqual(result["camera_evaluations"], response["camera_evaluations"])
        self.assertEqual(result["scene_similarity"], 0.86)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["issues"], response["issues"])
        self.assertEqual(len(provider.calls), 1)
        call = provider.calls[0]
        self.assertEqual(call["schema_name"], "prompt_to_play_visual_feedback")
        self.assertEqual(
            call["image_paths"], [reference.resolve(), screenshot.resolve()]
        )
        self.assertFalse(call["json_schema"]["additionalProperties"])
        self.assertEqual(
            set(call["json_schema"]["properties"]),
            {"camera_evaluations", "issues"},
        )
        self.assertEqual(
            call["json_schema"]["properties"]["camera_evaluations"]["items"]
            ["properties"]["camera_id"]["enum"],
            ["overview"],
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
        self.assertEqual(
            request["screenshot_cameras"],
            [{"camera_id": "overview", "attached_image_index": 2}],
        )
        self.assertNotIn("host_visual_gate", request)
        self.assertNotIn("timing_ms", request)
        self.assertNotIn("tokens", request)
        self.assertNotIn("weighted_score", request)

    def test_model_cannot_submit_an_acceptance_decision(self):
        world = read_world()
        feedback = rejected_observation()
        feedback["accepted"] = True
        with self.assertRaisesRegex(evaluator.EvaluatorError, "unknown keys: accepted"):
            evaluator.validate_visual_feedback(feedback, world)

    def test_strict_feedback_rejects_metrics_and_unknown_entity_ids(self):
        world = read_world()
        with_metric = copy.deepcopy(rejected_observation())
        with_metric["tokens"] = {"total": 1}
        with self.assertRaisesRegex(evaluator.EvaluatorError, "unknown keys: tokens"):
            evaluator.validate_visual_feedback(with_metric, world)

        unknown_entity = rejected_observation("invented_by_model")
        with self.assertRaisesRegex(evaluator.EvaluatorError, "unknown WorldSpec entity"):
            evaluator.validate_visual_feedback(unknown_entity, world)

    def test_detailed_guidance_is_required_and_retained(self):
        world = read_world()
        observation = rejected_observation()
        result = evaluator.validate_visual_feedback(observation, world)

        issue = result["issues"][0]
        self.assertEqual(issue["domain"], "layout")
        self.assertEqual(
            issue["suggested_changes"],
            observation["issues"][0]["suggested_changes"],
        )

        missing = copy.deepcopy(observation)
        del missing["issues"][0]["suggested_changes"]
        with self.assertRaisesRegex(
            evaluator.EvaluatorError, "missing keys: suggested_changes"
        ):
            evaluator.validate_visual_feedback(missing, world)

        empty = copy.deepcopy(observation)
        empty["issues"][0]["suggested_changes"] = []
        with self.assertRaisesRegex(
            evaluator.EvaluatorError, "expected at least one item"
        ):
            evaluator.validate_visual_feedback(empty, world)

    def test_detailed_guidance_validates_domain_entity_kind_and_patchable_field(
        self,
    ):
        world = read_world()

        wrong_domain = rejected_observation()
        wrong_domain["issues"][0]["domain"] = "gameplay"
        with self.assertRaisesRegex(
            evaluator.EvaluatorError, "does not belong to domain"
        ):
            evaluator.validate_visual_feedback(wrong_domain, world)

        wrong_kind = rejected_observation()
        wrong_kind["issues"][0]["suggested_changes"][0]["target_kind"] = "building"
        with self.assertRaisesRegex(evaluator.EvaluatorError, "expected 'prop'"):
            evaluator.validate_visual_feedback(wrong_kind, world)

        unknown_target = rejected_observation()
        unknown_target["issues"][0]["suggested_changes"][0]["target_id"] = "unknown_prop"
        with self.assertRaisesRegex(evaluator.EvaluatorError, "unknown WorldSpec entity"):
            evaluator.validate_visual_feedback(unknown_target, world)

        forbidden_field = rejected_observation()
        forbidden_field["issues"][0]["suggested_changes"][0]["field"] = "energy"
        with self.assertRaisesRegex(evaluator.EvaluatorError, "not patchable for 'prop'"):
            evaluator.validate_visual_feedback(forbidden_field, world)

    def test_host_requires_every_camera_and_penalizes_the_worst_view(self):
        world = read_world()
        observations = {
            "camera_evaluations": [
                camera_evaluation(1.0, "overview"),
                camera_evaluation(0.5, "detail"),
            ],
            "issues": [
                {
                    "code": "detail_is_weak",
                    "severity": "minor",
                    "entity_id": None,
                    "camera_id": "detail",
                    "message": "The detail view is weak.",
                    "suggested_fix": "Reframe the detail camera.",
                    "domain": "lighting_camera",
                    "suggested_changes": [
                        suggested_change(
                            "camera",
                            None,
                            "fov_deg",
                            "Narrow the detail camera field of view around the landmark.",
                            "The landmark occupies more of the detail frame.",
                        )
                    ],
                }
            ],
        }
        result = evaluator.validate_visual_feedback(
            observations,
            world,
            min_scene_similarity=0.8,
            expected_camera_ids=["overview", "detail"],
        )
        self.assertEqual(result["scene_similarity"], 0.675)
        self.assertFalse(result["accepted"])

        missing = copy.deepcopy(observations)
        missing["camera_evaluations"].pop()
        with self.assertRaisesRegex(evaluator.EvaluatorError, "missing cameras: detail"):
            evaluator.validate_visual_feedback(
                missing,
                world,
                expected_camera_ids=["overview", "detail"],
            )


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
                    rejected_feedback(camera_id="shot"),
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
                    rejected_feedback(camera_id="shot"),
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
                rejected_feedback(camera_id="shot"),
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
