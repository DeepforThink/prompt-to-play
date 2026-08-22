from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path

from prompt_to_play import agents, contracts, refinement


ROOT = Path(__file__).resolve().parents[1]


def read_world():
    return json.loads(
        (ROOT / "prompt_to_play" / "examples" / "forest_world.json").read_text(
            encoding="utf-8"
        )
    )


class RefinementProvider:
    def __init__(self, barrier=None, *, unauthorized_layout=False):
        self.barrier = barrier
        self.unauthorized_layout = unauthorized_layout

    def generate_json(self, messages, **_kwargs):
        if self.barrier is not None:
            self.barrier.wait(timeout=2)
        payload = json.loads(messages[1]["content"])
        world = copy.deepcopy(payload["current_world"])
        task_id = payload["task_id"]
        if task_id == "refine_01_layout":
            world["props"][0]["position"][0] += 1
            if self.unauthorized_layout:
                world["lights"][0]["energy"] += 1
        elif task_id == "refine_01_gameplay":
            world["interactions"]["interactables"][0]["label"] += "!"
        elif task_id == "refine_01_lighting_camera":
            world["lights"][0]["energy"] += 1
        return world


class NoopProvider(RefinementProvider):
    def generate_json(self, messages, **_kwargs):
        return copy.deepcopy(json.loads(messages[1]["content"])["current_world"])


class FailingProvider(RefinementProvider):
    def generate_json(self, *_args, **_kwargs):
        raise RuntimeError("secret-provider-detail")


class RefinementTests(unittest.TestCase):
    def test_fixed_subagents_merge_owned_updates_and_record_iteration(self):
        world = read_world()
        barrier = threading.Barrier(3)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = agents.MultiAgentRuntime(
                root,
                provider_factory=lambda *_args: RefinementProvider(barrier),
            )
            run = refinement.refine_world(
                runtime,
                world["brief"]["text"],
                world,
                project_root=root,
                max_workers=3,
            )

        self.assertEqual(
            [task["task_id"] for task in run.record["rounds"][0]["tasks"]],
            ["refine_01_layout", "refine_01_gameplay", "refine_01_lighting_camera"],
        )
        self.assertEqual(
            [task["status"] for task in run.record["rounds"][0]["tasks"]],
            ["applied", "applied", "applied"],
        )
        self.assertEqual(
            [task["status"] for task in run.record["rounds"][1]["tasks"]],
            ["noop", "noop", "noop"],
        )
        self.assertEqual(run.record["planner"]["iteration"], 0)
        self.assertEqual(run.record["termination_reason"], "converged")
        self.assertEqual(len(run.record["rounds"]), 2)
        self.assertEqual(run.record["max_workers"], 3)
        self.assertEqual(
            run.record["planner"]["world_sha256"], contracts.document_sha256(world)
        )
        self.assertEqual(
            run.record["result_world_sha256"],
            contracts.document_sha256(run.world),
        )
        self.assertEqual(
            run.world["props"][0]["position"][0],
            world["props"][0]["position"][0] + 1,
        )
        self.assertTrue(
            run.world["interactions"]["interactables"][0]["label"].endswith("!")
        )
        self.assertEqual(
            run.world["lights"][0]["energy"], world["lights"][0]["energy"] + 1
        )
        self.assertEqual(
            {trace.task_id for trace in runtime.traces()},
            {
                "refine_01_layout",
                "refine_01_gameplay",
                "refine_01_lighting_camera",
                "refine_02_layout",
                "refine_02_gameplay",
                "refine_02_lighting_camera",
            },
        )

    def test_unauthorized_candidate_is_rejected_without_losing_siblings(self):
        world = read_world()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = agents.MultiAgentRuntime(
                root,
                provider_factory=lambda *_args: RefinementProvider(
                    unauthorized_layout=True
                ),
            )
            run = refinement.refine_world(
                runtime,
                world["brief"]["text"],
                world,
                project_root=root,
            )

        by_id = {
            task["task_id"]: task for task in run.record["rounds"][0]["tasks"]
        }
        self.assertEqual(by_id["refine_01_layout"]["status"], "rejected")
        self.assertIn(
            "does not own 'light'",
            by_id["refine_01_layout"]["rejection_reason"],
        )
        self.assertEqual(run.world["props"], world["props"])
        self.assertNotEqual(
            run.world["interactions"]["interactables"],
            world["interactions"]["interactables"],
        )
        self.assertEqual(
            run.world["lights"][0]["energy"], world["lights"][0]["energy"] + 1
        )

    def test_noop_tasks_preserve_world_and_emit_no_merged_patch(self):
        world = read_world()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = agents.MultiAgentRuntime(
                root, provider_factory=lambda *_args: NoopProvider()
            )
            run = refinement.refine_world(
                runtime,
                world["brief"]["text"],
                world,
                project_root=root,
            )

        self.assertEqual(run.world, world)
        self.assertIsNone(run.record["rounds"][0]["merged_patch"])
        self.assertEqual(
            [task["status"] for task in run.record["rounds"][0]["tasks"]],
            ["noop", "noop", "noop"],
        )
        self.assertEqual(run.record["termination_reason"], "converged")

    def test_worker_configuration_is_host_bounded(self):
        self.assertEqual(refinement.refinement_max_workers({}), 4)
        for raw in ("0", "5", "many"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    refinement.refinement_max_workers(
                        {refinement.REFINEMENT_MAX_WORKERS_ENV: raw}
                    )
        self.assertEqual(refinement.refinement_max_iterations({}), 3)
        for raw in ("0", "5", "many"):
            with self.subTest(iterations=raw):
                with self.assertRaises(ValueError):
                    refinement.refinement_max_iterations(
                        {refinement.REFINEMENT_MAX_ITERATIONS_ENV: raw}
                    )

    def test_provider_failure_record_does_not_persist_error_details(self):
        world = read_world()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = agents.MultiAgentRuntime(
                root, provider_factory=lambda *_args: FailingProvider()
            )
            run = refinement.refine_world(
                runtime,
                world["brief"]["text"],
                world,
                project_root=root,
            )

        serialized = json.dumps(run.record)
        self.assertNotIn("secret-provider-detail", serialized)
        self.assertEqual(run.record["termination_reason"], "stalled")
        self.assertTrue(
            all(
                task["rejection_reason"] == "subagent execution failed"
                for task in run.record["rounds"][0]["tasks"]
            )
        )


if __name__ == "__main__":
    unittest.main()
