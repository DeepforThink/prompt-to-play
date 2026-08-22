import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path

from prompt_to_play import agents, contracts, repair


ROOT = Path(__file__).resolve().parents[1]


def read_world():
    return json.loads(
        (ROOT / "prompt_to_play" / "examples" / "forest_world.json").read_text(
            encoding="utf-8"
        )
    )


def feedback(camera_id="overview", *, all_domains=True):
    document = {
        "camera_evaluations": [
            {
                "camera_id": camera_id,
                "prompt_alignment": 0.5,
                "composition": 0.5,
                "lighting_materials": 0.5,
                "landmark_readability": 0.5,
                "camera_coverage": 0.5,
                "visible_defects": 0.5,
            }
        ],
        "scene_similarity": 0.5,
        "accepted": False,
        "issues": [
            {
                "code": "weak_scene",
                "severity": "major",
                "entity_id": None,
                "camera_id": camera_id,
                "message": "The scene needs stronger composition.",
                "suggested_fix": "Improve owned scene elements.",
                "domain": "layout",
                "suggested_changes": [
                    {
                        "target_kind": "prop",
                        "target_id": "ancient_oak",
                        "field": "position",
                        "instruction": "Move the landmark into a stronger composition.",
                        "expected_effect": "The landmark becomes clearly readable.",
                    }
                ],
            }
        ],
    }
    if all_domains:
        document["issues"].extend(
            [
                {
                    "code": "weak_gameplay_readability",
                    "severity": "minor",
                    "entity_id": "mural_clue",
                    "camera_id": camera_id,
                    "message": "The interaction purpose is visually unclear.",
                    "suggested_fix": "Clarify the interaction label.",
                    "domain": "gameplay",
                    "suggested_changes": [
                        {
                            "target_kind": "interactable",
                            "target_id": "mural_clue",
                            "field": "label",
                            "instruction": "Clarify the interaction label.",
                            "expected_effect": "The interaction purpose is readable.",
                        }
                    ],
                },
                {
                    "code": "flat_lighting",
                    "severity": "major",
                    "entity_id": "moonlight",
                    "camera_id": camera_id,
                    "message": "The lighting lacks focal contrast.",
                    "suggested_fix": "Strengthen the key light.",
                    "domain": "lighting_camera",
                    "suggested_changes": [
                        {
                            "target_kind": "light",
                            "target_id": "moonlight",
                            "field": "energy",
                            "instruction": "Increase the key light energy.",
                            "expected_effect": "The focal area gains contrast.",
                        }
                    ],
                },
            ]
        )
    return document


class RepairProvider:
    def __init__(self, barrier=None, *, invalid_layout=False, unauthorized=False):
        self.barrier = barrier
        self.invalid_layout = invalid_layout
        self.unauthorized = unauthorized
        self.payloads = []

    def generate_json(self, messages, **_kwargs):
        if self.barrier is not None:
            self.barrier.wait(timeout=2)
        payload = json.loads(messages[-1]["content"])
        self.payloads.append(payload)
        world = copy.deepcopy(payload["current_world"])
        task_id = payload["task_id"]
        if task_id.endswith("_layout"):
            if self.invalid_layout:
                world["roads"][0]["to"] = world["roads"][0]["from"]
            else:
                world["props"][0]["position"][0] += 1
            if self.unauthorized:
                world["lights"][0]["energy"] += 1
        elif task_id.endswith("_gameplay"):
            world["interactions"]["interactables"][0]["label"] += "!"
        elif task_id.endswith("_lighting_camera"):
            world["lights"][0]["energy"] += 1
        return world


class NoopProvider(RepairProvider):
    def generate_json(self, messages, **_kwargs):
        payload = json.loads(messages[-1]["content"])
        return copy.deepcopy(payload["current_world"])


class FailingProvider(RepairProvider):
    def generate_json(self, *_args, **_kwargs):
        raise RuntimeError("secret-provider-detail")


class ValueErrorProvider(RepairProvider):
    def generate_json(self, *_args, **_kwargs):
        raise ValueError("secret-provider-value-detail")


class RepairOrchestratorTests(unittest.TestCase):
    def run_repair(self, provider_factory, *, max_workers=3, feedback_document=None):
        world = read_world()
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        screenshot = root / "overview.png"
        screenshot.write_bytes(b"render")
        runtime = agents.MultiAgentRuntime(
            root, provider_factory=lambda *_args: provider_factory()
        )
        run = repair.repair_world(
            runtime,
            world["brief"]["text"],
            world,
            feedback() if feedback_document is None else feedback_document,
            [screenshot],
            iteration=1,
            project_root=root,
            max_workers=max_workers,
        )
        return temporary, world, runtime, run

    def test_fixed_tasks_are_concurrent_owned_and_deterministically_merged(self):
        barrier = threading.Barrier(3)
        temporary, world, runtime, run = self.run_repair(
            lambda: RepairProvider(barrier)
        )
        self.addCleanup(temporary.cleanup)

        self.assertEqual(
            [task["task_id"] for task in run.record["tasks"]],
            [
                "repair_01_layout",
                "repair_01_gameplay",
                "repair_01_lighting_camera",
            ],
        )
        self.assertEqual(
            [task["status"] for task in run.record["tasks"]],
            ["applied", "applied", "applied"],
        )
        self.assertEqual(
            {trace.task_id for trace in runtime.traces()},
            {
                "repair_01_layout",
                "repair_01_gameplay",
                "repair_01_lighting_camera",
            },
        )
        self.assertEqual(
            run.record["visual_guidance"][0]["suggested_changes"][0]["field"],
            "position",
        )
        self.assertEqual(contracts.apply_patch(world, run.patch), run.world)
        self.assertEqual(
            run.world["props"][0]["position"][0],
            world["props"][0]["position"][0] + 1,
        )

    def test_invalid_layout_is_rejected_without_losing_siblings(self):
        temporary, world, _runtime, run = self.run_repair(
            lambda: RepairProvider(invalid_layout=True)
        )
        self.addCleanup(temporary.cleanup)
        tasks = {task["domain"]: task for task in run.record["tasks"]}

        self.assertEqual(tasks["layout"]["status"], "rejected")
        self.assertIn("road endpoints", tasks["layout"]["rejection_reason"])
        self.assertEqual(tasks["gameplay"]["status"], "applied")
        self.assertEqual(tasks["lighting_camera"]["status"], "applied")
        self.assertEqual(run.world["roads"], world["roads"])
        self.assertNotEqual(
            run.world["interactions"]["interactables"],
            world["interactions"]["interactables"],
        )
        self.assertNotEqual(run.world["lights"], world["lights"])

    def test_unauthorized_layout_write_is_rejected_atomically(self):
        temporary, world, _runtime, run = self.run_repair(
            lambda: RepairProvider(unauthorized=True)
        )
        self.addCleanup(temporary.cleanup)
        tasks = {task["domain"]: task for task in run.record["tasks"]}
        self.assertEqual(tasks["layout"]["status"], "rejected")
        self.assertIn("does not own 'light'", tasks["layout"]["rejection_reason"])
        self.assertEqual(run.world["props"], world["props"])

    def test_all_noop_or_failed_workers_stall_without_a_patch(self):
        temporary, world, _runtime, noop = self.run_repair(NoopProvider)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(noop.record["status"], "stalled")
        self.assertIsNone(noop.patch)
        self.assertEqual(noop.world, world)

        failed_temp, _world, _runtime, failed = self.run_repair(FailingProvider)
        self.addCleanup(failed_temp.cleanup)
        serialized = json.dumps(failed.record)
        self.assertNotIn("secret-provider-detail", serialized)
        self.assertTrue(
            all(task["status"] == "rejected" for task in failed.record["tasks"])
        )
        self.assertIsNone(failed.patch)

        value_temp, _world, _runtime, value_failed = self.run_repair(
            ValueErrorProvider
        )
        self.addCleanup(value_temp.cleanup)
        value_serialized = json.dumps(value_failed.record)
        self.assertNotIn("secret-provider-value-detail", value_serialized)
        self.assertTrue(
            all(
                task["rejection_reason"] == "repair subagent execution failed"
                for task in value_failed.record["tasks"]
            )
        )

    def test_unassigned_domains_are_skipped_and_cannot_change_the_world(self):
        temporary, world, runtime, run = self.run_repair(
            RepairProvider,
            feedback_document=feedback(all_domains=False),
        )
        self.addCleanup(temporary.cleanup)
        self.assertEqual(
            [task["status"] for task in run.record["tasks"]],
            ["applied", "skipped", "skipped"],
        )
        self.assertEqual(
            {trace.task_id for trace in runtime.traces()}, {"repair_01_layout"}
        )
        self.assertEqual(
            run.world["interactions"], world["interactions"]
        )
        self.assertEqual(run.world["lights"], world["lights"])

    def test_worker_configuration_is_host_bounded(self):
        self.assertEqual(repair.repair_max_workers({}), 3)
        for raw in ("0", "5", "many"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                repair.repair_max_workers({repair.REPAIR_MAX_WORKERS_ENV: raw})


if __name__ == "__main__":
    unittest.main()
