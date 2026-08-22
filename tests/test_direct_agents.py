from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from prompt_to_play import direct_agents, direct_evaluation, direct_generation


def direct_plan(*files: dict[str, str]) -> dict[str, object]:
    return {
        "schema": direct_generation.DIRECT_GENERATION_CONTRACT,
        "files": list(files),
    }


def generated_file(path: str, content: str) -> dict[str, str]:
    return {"path": path, "action": "upsert", "content": content}


def visual_issue(*, severity: str = "major") -> dict[str, object]:
    return {
        "code": "missing_subject",
        "severity": severity,
        "entity_id": None,
        "message": "The requested vehicle is not recognisable.",
        "suggested_fix": "Improve the generated vehicle mesh and camera framing.",
    }


def visual_scores(value: float = 0.8, **overrides: float) -> dict[str, float]:
    result = {name: value for name in direct_evaluation.VISUAL_SCORE_DIMENSIONS}
    result.update(overrides)
    return result


class FakeProvider:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

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


class ProjectGeneratorAgentTests(unittest.TestCase):
    def test_generator_sends_original_prompt_seed_and_references(self):
        response = direct_plan(
            generated_file(
                "generated/GeneratedGame.tscn",
                '[gd_scene format=3]\n[node name="GeneratedGame" type="Node3D"]\n',
            ),
            generated_file(
                "generated/Game.gd",
                "extends Node3D\n",
            ),
        )
        provider = FakeProvider(response)

        with tempfile.TemporaryDirectory() as temporary:
            reference = Path(temporary) / "reference.png"
            reference.write_bytes(b"reference pixels")
            result = direct_agents.ProjectGeneratorAgent(provider).generate(
                "  a playable forest racing game  ",
                seed=424242,
                reference_image_paths=[reference],
            )

        self.assertEqual(result, response)
        self.assertEqual(len(provider.calls), 1)
        call = provider.calls[0]
        self.assertEqual(call["schema_name"], "prompt_to_play_direct_project_files")
        self.assertEqual(call["image_paths"], [reference.resolve()])
        self.assertFalse(call["json_schema"]["additionalProperties"])
        request = json.loads(call["messages"][1]["content"])
        self.assertEqual(request["original_prompt"], "a playable forest racing game")
        self.assertEqual(request["internal_seed"], 424242)
        self.assertEqual(request["reference_image_count"], 1)
        self.assertEqual(
            request["required_entry_scene"], "generated/GeneratedGame.tscn"
        )
        self.assertNotIn("world_spec", request)
        self.assertNotIn("WorldSpec", request)

    def test_generator_rejects_plan_without_required_game_scene(self):
        provider = FakeProvider(
            direct_plan(
                generated_file(
                    "generated/Game.cs",
                    "using Godot; public partial class Game : Node3D {}",
                )
            )
        )

        with self.assertRaisesRegex(
            direct_agents.DirectAgentError, "GeneratedGame.tscn"
        ):
            direct_agents.ProjectGeneratorAgent(provider).generate(
                "make a small game",
                seed=7,
            )


class DirectVisualEvaluationAgentTests(unittest.TestCase):
    def test_visual_evaluation_needs_no_worldspec_and_orders_images(self):
        response = {
            "scores": visual_scores(0.55),
            "issues": [visual_issue()],
        }
        provider = FakeProvider(response)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.png"
            screenshot = root / "capture.png"
            reference.write_bytes(b"reference")
            screenshot.write_bytes(b"capture")
            result = direct_agents.DirectVisualEvaluationAgent(
                provider,
                min_scene_similarity=0.8,
            ).evaluate(
                "forest racer",
                [screenshot],
                reference_image_paths=[reference],
                project_manifest={"file_count": 3},
            )

        self.assertEqual(result["accepted"], False)
        call = provider.calls[0]
        self.assertEqual(call["schema_name"], "prompt_to_play_direct_visual_feedback")
        self.assertEqual(
            call["image_paths"], [reference.resolve(), screenshot.resolve()]
        )
        request = json.loads(call["messages"][1]["content"])
        self.assertEqual(request["original_prompt"], "forest racer")
        self.assertEqual(request["reference_image_count"], 1)
        self.assertEqual(request["screenshot_count"], 1)
        self.assertEqual(request["project_manifest"], {"file_count": 3})
        self.assertFalse(any("world" in key.casefold() for key in request))

    def test_host_computes_overall_score_and_acceptance(self):
        provider = FakeProvider(
            {
                "scores": visual_scores(0.9),
                "issues": [visual_issue(severity="minor")],
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            screenshot = Path(temporary) / "capture.png"
            screenshot.write_bytes(b"capture")
            agent = direct_agents.DirectVisualEvaluationAgent(
                provider,
                min_scene_similarity=0.8,
            )
            result = agent.evaluate("forest racer", [screenshot])
        self.assertEqual(result["scene_similarity"], 0.9)
        self.assertTrue(result["accepted"])
        self.assertNotIn("accepted", provider.calls[0]["json_schema"]["properties"])


class CodeRepairAgentTests(unittest.TestCase):
    def test_repair_receives_current_source_and_diagnostics_and_returns_safe_patch(
        self,
    ):
        response = direct_plan(
            generated_file(
                "generated/Game.cs",
                "using Godot; public partial class Game : Node3D {}",
            )
        )
        provider = FakeProvider(response)
        current_files = {
            "generated/Game.cs": (
                "using Godot; public partial class Game : Node3D "
                "{ public override void _Ready() { } }"
            ),
            "generated/GeneratedGame.tscn": (
                '[gd_scene format=3]\n[node name="Game" type="Node3D"]\n'
            ),
        }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.png"
            screenshot = root / "capture.png"
            reference.write_bytes(b"reference")
            screenshot.write_bytes(b"capture")
            result = direct_agents.CodeRepairAgent(provider).repair(
                "make the car controllable",
                current_files,
                diagnostics="CS0103: The name 'speed' does not exist",
                structural_report={"scene_load": False},
                visual_feedback={
                    "scores": visual_scores(0.4),
                    "scene_similarity": 0.4,
                    "accepted": False,
                    "issues": [visual_issue()],
                },
                screenshot_image_paths=[screenshot],
                reference_image_paths=[reference],
            )

        self.assertEqual(result, response)
        call = provider.calls[0]
        self.assertEqual(call["schema_name"], "prompt_to_play_direct_code_patch")
        self.assertEqual(
            call["image_paths"], [reference.resolve(), screenshot.resolve()]
        )
        request = json.loads(call["messages"][1]["content"])
        self.assertEqual(request["original_prompt"], "make the car controllable")
        self.assertEqual(
            request["diagnostics"], "CS0103: The name 'speed' does not exist"
        )
        self.assertEqual(request["structural_report"], {"scene_load": False})
        self.assertEqual(
            {
                item["path"]: item["content"]
                for item in request["current_generated_files"]
            },
            current_files,
        )
        self.assertTrue(request["source_context_is_complete"])
        self.assertEqual(
            [item["path"] for item in request["current_generated_file_inventory"]],
            sorted(current_files),
        )
        self.assertNotIn("world_spec", request)

    def test_repair_context_omits_whole_files_and_never_sends_partial_source(self):
        response = direct_plan(
            generated_file(
                "generated/Game.cs",
                "using Godot; public partial class Game : Node3D {}",
            )
        )
        provider = FakeProvider(response)
        large_a = "a" * 250_000
        large_b = "b" * 250_000
        files = {
            "generated/GeneratedGame.tscn": "[gd_scene format=3]\n",
            "generated/A.md": large_a,
            "generated/B.md": large_b,
        }

        direct_agents.CodeRepairAgent(provider).repair(
            "repair it",
            files,
            diagnostics="B.md needs work",
        )
        request = json.loads(provider.calls[0]["messages"][1]["content"])
        included = {
            item["path"]: item["content"] for item in request["current_generated_files"]
        }
        self.assertFalse(request["source_context_is_complete"])
        for path, content in included.items():
            self.assertEqual(content, files[path])
        self.assertIn("generated/B.md", included)
        self.assertNotIn("generated/A.md", included)

    def test_repair_rejects_patch_outside_generated_boundary(self):
        provider = FakeProvider(
            direct_plan(
                generated_file(
                    "host/TrustedHarness.cs",
                    "using Godot; public partial class TrustedHarness : Node {}",
                )
            )
        )
        with self.assertRaises(direct_generation.DirectGenerationError):
            direct_agents.CodeRepairAgent(provider).repair(
                "repair the game",
                {"generated/GeneratedGame.tscn": "[gd_scene format=3]\n"},
                diagnostics="scene failed to load",
            )


if __name__ == "__main__":
    unittest.main()
