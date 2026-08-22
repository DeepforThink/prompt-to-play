from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from prompt_to_play import agents, contracts, pipeline
from prompt_to_play.launcher import LaunchRequest, PipelineState


ROOT = Path(__file__).resolve().parents[1]


def read_fixture_world():
    with (ROOT / "prompt_to_play" / "examples" / "forest_world.json").open(
        "r", encoding="utf-8"
    ) as handle:
        return json.load(handle)


def planned_world(prompt, references):
    world = copy.deepcopy(read_fixture_world())
    world["world_id"] = "pipeline_fixture"
    world["brief"] = {"text": prompt, "references": [dict(item) for item in references]}
    world["seed"] = contracts.derive_world_seed(prompt, references)
    return world


def delivery_evaluation(
    *,
    passed: bool,
    visual_passed: bool = True,
    failed_structural: str | None = None,
):
    checks = [
        {
            "id": check_id,
            "kind": "hard",
            "passed": check_id != failed_structural,
            "score": 0 if check_id == failed_structural else 1,
            "message": f"{check_id} result",
            "evidence": [],
        }
        for check_id in sorted(pipeline.REQUIRED_DELIVERY_CHECKS)
    ]
    checks.append(
        {
            "id": pipeline.VISUAL_CHECK_ID,
            "kind": "hard",
            "passed": visual_passed,
            "score": 0.4 if not visual_passed else 1,
            "message": "visual result",
            "evidence": ["captures/overview.png"],
        }
    )
    return {
        "schema": contracts.EVALUATION_SCHEMA,
        "run_id": "run",
        "world_id": "world",
        "world_sha256": "a" * 64,
        "checks": checks,
        "result": {
            "passed": passed,
            "weighted_score": 0.8 if passed else 0.4,
            "threshold": 0.7,
        },
        "issues": [],
    }


class FakeProcess:
    pid = 4242


class PipelineStageTests(unittest.TestCase):
    def make_layout(self, temporary):
        workspace = Path(temporary)
        source_repo = workspace / "prompt-to-play"
        source_repo.mkdir()
        output_root = workspace / "output" / "generated"
        image = workspace / "inputs" / "参考.png"
        image.parent.mkdir()
        image.write_bytes(b"reference pixels")
        return workspace, source_repo, output_root, image

    def test_plan_passes_raw_images_and_stable_project_references(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace, source_repo, output_root, image = self.make_layout(temporary)
            captured = {}

            def fake_plan(
                prompt,
                references,
                *,
                reference_image_paths,
                project_root,
            ):
                captured.update(
                    prompt=prompt,
                    references=references,
                    reference_image_paths=reference_image_paths,
                    project_root=project_root,
                )
                return planned_world(prompt, references)

            stages = pipeline.PipelineStages(
                pipeline.PipelineDependencies(
                    source_repo_root=source_repo,
                    output_root=output_root,
                    plan_world=fake_plan,
                )
            )
            request = LaunchRequest.from_values("雾中的水晶花园", [image])
            state = PipelineState(request)
            stages.plan(state, lambda _message: None)

            request_document = state.require_result(pipeline.REQUEST_RESULT)
            binding = state.require_result(pipeline.REFERENCES_RESULT)[0]
            target = state.require_result(pipeline.PROJECT_RESULT)
            self.assertEqual(
                target.parent,
                (output_root / request_document["request_hash"][:12]).resolve(
                    strict=False
                ),
            )
            self.assertRegex(target.name, r"^run-[0-9a-f]{12}$")
            self.assertEqual(captured["prompt"], request.prompt)
            self.assertEqual(
                captured["reference_image_paths"], request.reference_images
            )
            self.assertEqual(
                captured["project_root"], source_repo.resolve(strict=False)
            )
            self.assertRegex(
                binding.project_path,
                r"^references/00_[0-9a-f]{16}\.png$",
            )
            self.assertEqual(
                captured["references"],
                [{"path": binding.project_path, "sha256": binding.sha256}],
            )
            self.assertNotIn(str(workspace), json.dumps(captured["references"]))

    def test_default_planner_fans_out_refiners_and_persists_iteration_record(self):
        class PlannerProvider:
            def generate_json(self, *_args, **_kwargs):
                return {}

        class RefinementProvider:
            def generate_json(self, messages, **_kwargs):
                payload = json.loads(messages[-1]["content"])
                return copy.deepcopy(payload["current_world"])

        with tempfile.TemporaryDirectory() as temporary:
            workspace, source_repo, output_root, _image = self.make_layout(temporary)

            def fake_default_plan(
                prompt,
                references,
                *,
                provider,
                **_kwargs,
            ):
                provider.generate_json(
                    [{"role": "user", "content": "plan"}],
                    json_schema={"type": "object"},
                    schema_name="world",
                )
                return planned_world(prompt, references)

            runtime_factory = lambda repo, _environment: agents.MultiAgentRuntime(
                repo,
                provider_factory=lambda role, *_args: (
                    RefinementProvider()
                    if role == agents.AgentRole.WORLD_REFINER
                    else PlannerProvider()
                ),
            )

            def fake_publish(target):
                (target / "spec").mkdir(parents=True)
                return target

            with patch.object(pipeline.planner, "plan_world", fake_default_plan):
                stages = pipeline.PipelineStages(
                    pipeline.PipelineDependencies(
                        source_repo_root=source_repo,
                        output_root=output_root,
                        environment={"PROMPT_TO_PLAY_ASSET_MODE": "off"},
                        publish_project=fake_publish,
                        agent_runtime_factory=runtime_factory,
                    )
                )
            state = PipelineState(LaunchRequest.from_values("并行规划的森林遗迹"))
            messages = []
            stages.plan(state, messages.append)
            stages.validate(state, lambda _message: None)
            stages.publish(state, lambda _message: None)

            record = state.require_result(pipeline.REFINEMENT_RESULT)
            first_round = record["rounds"][0]
            self.assertEqual(
                [task["task_id"] for task in first_round["tasks"]],
                [
                    "refine_01_layout",
                    "refine_01_gameplay",
                    "refine_01_lighting_camera",
                ],
            )
            self.assertEqual(
                [task["status"] for task in first_round["tasks"]],
                ["noop", "noop", "noop"],
            )
            project = state.require_result(pipeline.PROJECT_RESULT)
            run_id = state.require_result(pipeline.REQUEST_RESULT)["request_hash"][:12]
            artifact_root = project / "artifacts" / "runs" / run_id
            persisted = json.loads(
                (artifact_root / "refinement.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["run_id"], run_id)
            self.assertEqual(persisted["planner"]["iteration"], 0)
            self.assertEqual(persisted["termination_reason"], "converged")
            trace = json.loads(
                (artifact_root / "agent_trace.json").read_text(encoding="utf-8")
            )
            self.assertEqual(trace["schema"], agents.TRACE_SCHEMA)
            self.assertEqual(trace["calls"][0]["role"], "world_planner")
            self.assertIn(
                "模型后端：http -> https://api.openai.com/v1",
                messages,
            )
            self.assertEqual(
                {call["task_id"] for call in trace["calls"][1:]},
                {
                    "refine_01_layout",
                    "refine_01_gameplay",
                    "refine_01_lighting_camera",
                },
            )

    def test_validate_uses_the_contract_dependency(self):
        checked = []

        def fake_validate(document):
            checked.append(document)
            return document

        stages = pipeline.PipelineStages(
            pipeline.PipelineDependencies(validate_world=fake_validate)
        )
        state = PipelineState(LaunchRequest.from_values("different arbitrary prompt"))
        world = {"world_id": "not_a_fixed_scene"}
        state.set_result(pipeline.WORLD_RESULT, world)
        stages.validate(state, lambda _message: None)
        self.assertEqual(checked, [world])

    def test_asset_api_budget_is_shared_across_revisions(self):
        stages = pipeline.PipelineStages(
            pipeline.PipelineDependencies(
                environment={
                    "PROMPT_TO_PLAY_ASSET_MODE": "auto",
                    "PROMPT_TO_PLAY_ASSET_MAX_GENERATIONS": "3",
                }
            )
        )
        state = PipelineState(LaunchRequest.from_values("world"))
        self.assertEqual(
            stages._asset_environment(state)[
                "PROMPT_TO_PLAY_ASSET_MAX_GENERATIONS"
            ],
            "3",
        )
        state.set_result(
            pipeline.ASSET_RESULTS_RESULT,
            [
                SimpleNamespace(attempted_generations=2),
                SimpleNamespace(attempted_generations=1),
            ],
        )
        self.assertEqual(
            stages._asset_environment(state)[
                "PROMPT_TO_PLAY_ASSET_MAX_GENERATIONS"
            ],
            "0",
        )

    def test_publish_uses_hash_target_copies_references_and_writes_world(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace, source_repo, output_root, image = self.make_layout(temporary)
            published_targets = []

            def fake_plan(prompt, references, **_kwargs):
                return planned_world(prompt, references)

            def fake_publish(target):
                published_targets.append(target)
                (target / "spec").mkdir(parents=True)
                return target

            stages = pipeline.PipelineStages(
                pipeline.PipelineDependencies(
                    source_repo_root=source_repo,
                    output_root=output_root,
                    plan_world=fake_plan,
                    publish_project=fake_publish,
                )
            )
            state = PipelineState(LaunchRequest.from_values("海边观测站", [image]))
            stages.plan(state, lambda _message: None)
            stages.validate(state, lambda _message: None)
            stages.publish(state, lambda _message: None)

            request_document = state.require_result(pipeline.REQUEST_RESULT)
            expected_target = state.require_result(pipeline.PROJECT_RESULT)
            self.assertEqual(
                expected_target.parent,
                (output_root / request_document["request_hash"][:12]).resolve(
                    strict=False
                ),
            )
            self.assertEqual(published_targets, [expected_target])
            world = json.loads(
                (expected_target / "spec" / "world.json").read_text(encoding="utf-8")
            )
            reference = world["brief"]["references"][0]
            copied = expected_target.joinpath(*reference["path"].split("/"))
            self.assertEqual(copied.read_bytes(), image.read_bytes())
            self.assertEqual(reference["sha256"], pipeline._sha256_file(copied))
            self.assertEqual(world["brief"]["text"], "海边观测站")
            request_path = (
                expected_target
                / "artifacts"
                / "runs"
                / request_document["request_hash"][:12]
                / "request.json"
            )
            self.assertTrue(request_path.is_file())
            contracts.validate_world(world)

    def test_publish_rejects_a_target_outside_its_exact_hash_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace, source_repo, output_root, _image = self.make_layout(temporary)
            publisher_called = []
            stages = pipeline.PipelineStages(
                pipeline.PipelineDependencies(
                    source_repo_root=source_repo,
                    output_root=output_root,
                    publish_project=lambda target: publisher_called.append(target)
                    or target,
                )
            )
            state = PipelineState(LaunchRequest.from_values("world"))
            state.set_result(pipeline.REQUEST_RESULT, {"request_hash": "d" * 64})
            state.set_result(pipeline.PROJECT_RESULT, workspace / "outside")
            with self.assertRaisesRegex(
                pipeline.PipelineIntegrationError, "unique run child"
            ):
                stages.publish(state, lambda _message: None)
            self.assertEqual(publisher_called, [])

    def test_build_discovers_workspace_tools_and_runs_all_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace, source_repo, output_root, _image = self.make_layout(temporary)
            dotnet = workspace / ".tools" / "dotnet-sdk" / "dotnet.exe"
            godot = workspace / ".tools" / "godot" / "Godot_v4_console.exe"
            godot_visible = workspace / ".tools" / "godot" / "Godot_v4.exe"
            feed = godot.parent / "GodotSharp" / "Tools" / "nupkgs"
            dotnet.parent.mkdir(parents=True)
            feed.mkdir(parents=True)
            dotnet.write_bytes(b"exe")
            godot.write_bytes(b"exe")
            godot_visible.write_bytes(b"exe")
            (feed / "Godot.NET.Sdk.4.7.1.nupkg").write_bytes(b"package")

            request_hash = "a" * 64
            project_dir = output_root / request_hash[:12]
            project_dir.mkdir(parents=True)
            calls = []

            def fake_run(argv, cwd, environment, _log):
                calls.append((tuple(argv), cwd, dict(environment)))
                if "--quit-after" in argv:
                    report = (
                        project_dir
                        / "artifacts"
                        / "runs"
                        / request_hash[:12]
                        / "rev_0"
                        / "structural_report.json"
                    )
                    report.parent.mkdir(parents=True)
                    report.write_text('{"status":"pass"}', encoding="utf-8")

            stages = pipeline.PipelineStages(
                pipeline.PipelineDependencies(
                    source_repo_root=source_repo,
                    output_root=output_root,
                    environment={
                        "PTP_CAPTURE": "1",
                        "OPENAI_API_KEY": "must-not-leak",
                        "NUGET_FALLBACK_PACKAGES": "must-not-control-build",
                    },
                    run_command=fake_run,
                    which=lambda _name: None,
                )
            )
            state = PipelineState(LaunchRequest.from_values("world"))
            state.set_result(pipeline.REQUEST_RESULT, {"request_hash": request_hash})
            state.set_result(pipeline.PROJECT_RESULT, project_dir)
            stages.build(state, lambda _message: None)

            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[0][0][:2], (str(dotnet.resolve()), "restore"))
            self.assertEqual(
                calls[0][0][2:],
                ("--source", str(feed.resolve()), "--ignore-failed-sources"),
            )
            self.assertEqual(
                calls[1][0], (str(dotnet.resolve()), "build", "--no-restore")
            )
            self.assertIn("--headless", calls[2][0])
            for _argv, cwd, environment in calls:
                self.assertEqual(cwd, project_dir.resolve())
                self.assertEqual(environment["PTP_RUN_ID"], request_hash[:12])
                self.assertEqual(environment["PTP_REVISION"], "0")
                self.assertEqual(
                    environment["DOTNET_ROOT"], str(dotnet.parent.resolve())
                )
                self.assertNotIn("PTP_CAPTURE", environment)
                self.assertNotIn("OPENAI_API_KEY", environment)
            for _argv, _cwd, environment in calls[:2]:
                self.assertEqual(
                    environment["NUGET_FALLBACK_PACKAGES"], str(feed.resolve())
                )
            self.assertNotIn("NUGET_FALLBACK_PACKAGES", calls[2][2])
            self.assertEqual(
                state.require_result(pipeline.TOOLCHAIN_RESULT),
                pipeline.Toolchain(
                    dotnet.resolve(),
                    godot.resolve(),
                    feed.resolve(),
                    godot_visible.resolve(),
                ),
            )
            self.assertEqual(
                state.require_result(pipeline.STRUCTURAL_REPORT_RESULT)["status"],
                "pass",
            )

    def test_explicit_tool_environment_takes_priority(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_repo = root / "repo"
            source_repo.mkdir()
            dotnet = root / "custom" / "dotnet.exe"
            godot = root / "custom" / "Godot.exe"
            godot_console = root / "custom" / "Godot_console.exe"
            feed = godot.parent / "GodotSharp" / "Tools" / "nupkgs"
            dotnet.parent.mkdir(parents=True)
            feed.mkdir(parents=True)
            dotnet.write_bytes(b"exe")
            godot.write_bytes(b"exe")
            godot_console.write_bytes(b"exe")
            (feed / "Godot.NET.Sdk.test.nupkg").write_bytes(b"package")
            toolchain = pipeline.discover_toolchain(
                source_repo,
                environment={
                    "PTP_DOTNET_EXE": str(dotnet),
                    "PTP_GODOT_EXE": str(godot),
                },
                which=lambda _name: None,
            )
            self.assertEqual(toolchain.dotnet_exe, dotnet.resolve())
            self.assertEqual(toolchain.godot_exe, godot_console.resolve())
            self.assertEqual(toolchain.visible_exe, godot.resolve())
            self.assertEqual(toolchain.godot_nupkgs, feed.resolve())

    def test_launch_starts_visible_godot_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            project_dir = Path(temporary).resolve()
            godot = project_dir / "Godot.exe"
            godot.write_bytes(b"exe")
            captured = []

            def fake_start(argv, cwd, environment, log_path):
                captured.append((tuple(argv), cwd, dict(environment), log_path))
                return FakeProcess()

            stages = pipeline.PipelineStages(
                pipeline.PipelineDependencies(
                    environment={"PTP_CAPTURE": "1", "OPENAI_API_KEY": "must-not-leak"},
                    start_process=fake_start,
                    launch_probe_seconds=0,
                )
            )
            state = PipelineState(LaunchRequest.from_values("world"))
            state.set_result(pipeline.PROJECT_RESULT, project_dir)
            state.set_result(pipeline.REQUEST_RESULT, {"request_hash": "b" * 64})
            state.set_result(
                pipeline.TOOLCHAIN_RESULT,
                pipeline.Toolchain(godot, godot, project_dir),
            )
            state.set_result(pipeline.DELIVERY_RESULT, {"mode": "best_effort"})
            messages = []
            stages.launch(state, messages.append)

            argv, cwd, environment, log_path = captured[0]
            self.assertEqual(argv[0], str(godot))
            self.assertNotIn("--headless", argv)
            self.assertEqual(cwd, project_dir)
            self.assertNotIn("PTP_CAPTURE", environment)
            self.assertNotIn("OPENAI_API_KEY", environment)
            self.assertEqual(environment["PTP_RUN_ID"], "b" * 12)
            self.assertEqual(environment["PTP_REVISION"], "0")
            self.assertEqual(environment["DOTNET_ROOT"], str(project_dir))
            self.assertEqual(
                log_path,
                project_dir / "artifacts" / "runs" / ("b" * 12) / "rev_0" / "play.log",
            )
            self.assertIsInstance(
                state.require_result(pipeline.PROCESS_RESULT), FakeProcess
            )
            self.assertTrue(any("best_effort" in message for message in messages))

    def test_launch_reports_an_early_godot_exit_and_log_tail(self):
        class ExitedProcess:
            pid = 31337

            def wait(self, timeout):
                self.timeout = timeout
                return 7

        with tempfile.TemporaryDirectory() as temporary:
            project_dir = Path(temporary).resolve()
            godot = project_dir / "Godot.exe"
            godot.write_bytes(b"exe")

            def fake_start(_argv, _cwd, _environment, log_path):
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("assembly load failed", encoding="utf-8")
                return ExitedProcess()

            stages = pipeline.PipelineStages(
                pipeline.PipelineDependencies(
                    start_process=fake_start,
                    launch_probe_seconds=0.01,
                )
            )
            state = PipelineState(LaunchRequest.from_values("world"))
            state.set_result(pipeline.PROJECT_RESULT, project_dir)
            state.set_result(pipeline.REQUEST_RESULT, {"request_hash": "e" * 64})
            state.set_result(
                pipeline.TOOLCHAIN_RESULT,
                pipeline.Toolchain(godot, godot, project_dir),
            )

            with self.assertRaisesRegex(
                pipeline.PipelineIntegrationError,
                "(?s)code 7.*assembly load failed",
            ):
                stages.launch(state, lambda _message: None)

    def test_godot_feed_must_be_adjacent_and_match_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            godot = root / "godot" / "Godot_v4.7.1_console.exe"
            godot.parent.mkdir()
            godot.write_bytes(b"exe")
            unrelated = root / "other" / "GodotSharp" / "Tools" / "nupkgs"
            unrelated.mkdir(parents=True)
            (unrelated / "Godot.NET.Sdk.4.7.1.nupkg").write_bytes(b"package")
            self.assertIsNone(pipeline._find_nupkgs(godot))

            adjacent = godot.parent / "GodotSharp" / "Tools" / "nupkgs"
            adjacent.mkdir(parents=True)
            (adjacent / "Godot.NET.Sdk.4.6.0.nupkg").write_bytes(b"package")
            self.assertIsNone(pipeline._find_nupkgs(godot))
            (adjacent / "Godot.NET.Sdk.4.7.1.nupkg").write_bytes(b"package")
            self.assertEqual(pipeline._find_nupkgs(godot), adjacent.resolve())

    def test_api_loop_isolates_an_invalid_repair_subagent(self):
        class PlannerProvider:
            def generate_json(self, *_args, **_kwargs):
                return copy.deepcopy(read_fixture_world())

        class VisualProvider:
            def __init__(self):
                self.calls = 0
                self.image_paths = []

            def generate_json(self, *_args, **kwargs):
                self.calls += 1
                self.image_paths.append(list(kwargs["image_paths"]))
                if self.calls == 1:
                    return {
                        "camera_evaluations": [
                            {
                                "camera_id": "overview",
                                "prompt_alignment": 0.5,
                                "composition": 0.5,
                                "lighting_materials": 0.5,
                                "landmark_readability": 0.5,
                                "camera_coverage": 0.5,
                                "visible_defects": 0.5,
                            }
                        ],
                        "issues": [
                            {
                                "code": "landmark_too_small",
                                "severity": "major",
                                "entity_id": None,
                                "camera_id": "overview",
                                "message": "The main landmark is visually weak.",
                                "suggested_fix": "Move the first building closer to the overview camera.",
                                "domain": "layout",
                                "suggested_changes": [
                                    {
                                        "target_kind": "prop",
                                        "target_id": "ancient_oak",
                                        "field": "position",
                                        "instruction": "Move the landmark into a stronger foreground position.",
                                        "expected_effect": "The landmark becomes immediately readable.",
                                    }
                                ],
                            }
                        ],
                    }
                if self.calls == 3:
                    return {
                        "provider_secret": "sk-sensitive-evaluator-output",
                        "issues": "not-an-array",
                    }
                return {
                    "camera_evaluations": [
                        {
                            "camera_id": "overview",
                            "prompt_alignment": 0.5,
                            "composition": 0.5,
                            "lighting_materials": 0.5,
                            "landmark_readability": 0.5,
                            "camera_coverage": 0.5,
                            "visible_defects": 0.5,
                        }
                    ],
                    "issues": [
                        {
                            "code": "landmark_still_weak",
                            "severity": "major",
                            "entity_id": None,
                            "camera_id": "overview",
                            "message": "The repaired landmark is still visually weak.",
                            "suggested_fix": "Increase its visual prominence.",
                            "domain": "layout",
                            "suggested_changes": [
                                {
                                    "target_kind": "prop",
                                    "target_id": "ancient_oak",
                                    "field": "scale",
                                    "instruction": "Increase the landmark scale in the overview composition.",
                                    "expected_effect": "The landmark dominates the intended focal area.",
                                }
                            ],
                        },
                        {
                            "code": "interaction_label_unclear",
                            "severity": "minor",
                            "entity_id": "mural_clue",
                            "camera_id": "overview",
                            "message": "The interaction purpose is unclear.",
                            "suggested_fix": "Clarify the interaction label.",
                            "domain": "gameplay",
                            "suggested_changes": [
                                {
                                    "target_kind": "interactable",
                                    "target_id": "mural_clue",
                                    "field": "label",
                                    "instruction": "Clarify the interaction label.",
                                    "expected_effect": "The interaction purpose becomes readable.",
                                }
                            ],
                        },
                        {
                            "code": "flat_key_light",
                            "severity": "major",
                            "entity_id": "moonlight",
                            "camera_id": "overview",
                            "message": "The focal lighting is too flat.",
                            "suggested_fix": "Strengthen the key light.",
                            "domain": "lighting_camera",
                            "suggested_changes": [
                                {
                                    "target_kind": "light",
                                    "target_id": "moonlight",
                                    "field": "energy",
                                    "instruction": "Increase the key light energy.",
                                    "expected_effect": "The focal region gains contrast.",
                                }
                            ],
                        },
                    ],
                }

        class RefinementProvider:
            def generate_json(self, messages, **_kwargs):
                payload = json.loads(messages[-1]["content"])
                return copy.deepcopy(payload["current_world"])

        class RepairProvider:
            payloads = []

            def generate_json(self, messages, **_kwargs):
                payload = json.loads(messages[-1]["content"])
                self.payloads.append(payload)
                revised = copy.deepcopy(payload["current_world"])
                task_id = payload["task_id"]
                if task_id == "repair_01_layout":
                    revised["props"][0]["position"][0] += 1
                elif task_id == "repair_01_gameplay":
                    revised["interactions"]["interactables"][0]["label"] += "!"
                elif task_id == "repair_01_lighting_camera":
                    revised["lights"][0]["energy"] += 1
                elif task_id == "repair_02_layout":
                    revised["roads"][0]["to"] = revised["roads"][0]["from"]
                elif task_id == "repair_02_gameplay":
                    revised["interactions"]["interactables"][0]["label"] += "?"
                elif task_id == "repair_02_lighting_camera":
                    revised["lights"][0]["energy"] += 1
                return revised

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source_repo = workspace / "prompt-to-play"
            (source_repo / "prompt_to_play").mkdir(parents=True)
            (source_repo / "prompt_to_play" / "evaluation_policy.json").write_text(
                (ROOT / "prompt_to_play" / "evaluation_policy.json").read_text(
                    encoding="utf-8"
                ),
                encoding="utf-8",
            )
            output_root = workspace / "output" / "generated"
            reference = workspace / "reference.png"
            reference.write_bytes(b"reference pixels")

            dotnet = workspace / ".tools" / "dotnet-sdk" / "dotnet.exe"
            godot = workspace / ".tools" / "godot" / "Godot_v4_console.exe"
            godot_visible = workspace / ".tools" / "godot" / "Godot_v4.exe"
            feed = godot.parent / "GodotSharp" / "Tools" / "nupkgs"
            dotnet.parent.mkdir(parents=True)
            feed.mkdir(parents=True)
            dotnet.write_bytes(b"exe")
            godot.write_bytes(b"exe")
            godot_visible.write_bytes(b"exe")
            (feed / "Godot.NET.Sdk.4.7.1.nupkg").write_bytes(b"package")

            role_providers = {
                agents.AgentRole.WORLD_PLANNER: PlannerProvider(),
                agents.AgentRole.VISUAL_EVALUATOR: VisualProvider(),
                agents.AgentRole.REPAIR: RepairProvider(),
            }
            refinement_providers = []

            def provider_factory(role, _environment, _repo):
                if role == agents.AgentRole.WORLD_REFINER:
                    provider = RefinementProvider()
                    refinement_providers.append(provider)
                    return provider
                return role_providers[role]

            def runtime_factory(repo, environment):
                return agents.MultiAgentRuntime(
                    repo,
                    environment=environment,
                    provider_factory=provider_factory,
                )

            def fake_publish(target):
                (target / "spec").mkdir(parents=True)
                (target / "assets").mkdir()
                (target / "scenes").mkdir()
                (target / "scenes" / "Main.tscn").write_text(
                    "[gd_scene format=3]\n", encoding="utf-8"
                )
                return target

            initial_prop_x = read_fixture_world()["props"][0]["position"][0]

            def fake_run(argv, cwd, environment, _log):
                revision = int(environment["PTP_REVISION"])
                revision_dir = (
                    cwd
                    / "artifacts"
                    / "runs"
                    / environment["PTP_RUN_ID"]
                    / f"rev_{revision}"
                )
                if "--headless" in argv:
                    revision_dir.mkdir(parents=True, exist_ok=True)
                    current_world = json.loads(
                        (cwd / "spec" / "world.json").read_text(encoding="utf-8")
                    )
                    collision_repaired = (
                        current_world["props"][0]["position"][0] > initial_prop_x
                    )
                    checks = [
                        {
                            "id": check_id,
                            "kind": "hard",
                            "passed": collision_repaired
                            if check_id == "walkable_collision"
                            else True,
                            "score": (
                                1
                                if check_id != "walkable_collision"
                                or collision_repaired
                                else 0
                            ),
                            "message": (
                                "Move the blocking prop away from the walkway"
                                if check_id == "walkable_collision"
                                and not collision_repaired
                                else f"{check_id} passed"
                            ),
                            "evidence": [],
                        }
                        for check_id in (
                            "scene_loads",
                            "world_graph_connected",
                            "objectives_completable",
                            "completion_reachable",
                            "walkable_collision",
                        )
                    ]
                    structural_passed = all(check["passed"] for check in checks)
                    (revision_dir / "structural_report.json").write_text(
                        json.dumps(
                            {
                                "status": "pass" if structural_passed else "fail",
                                "world_source_sha256": contracts.document_sha256(
                                    current_world
                                ),
                                "checks": checks,
                            }
                        ),
                        encoding="utf-8",
                    )
                    if not structural_passed:
                        raise pipeline.PipelineIntegrationError(
                            "simulated Godot structural exit"
                        )
                elif environment.get("PTP_CAPTURE") == "1":
                    capture = revision_dir / "captures" / "overview.png"
                    capture.parent.mkdir(parents=True, exist_ok=True)
                    capture.write_bytes(b"fake png")
                    relative = capture.relative_to(cwd).as_posix()
                    (revision_dir / "capture_manifest.json").write_text(
                        json.dumps(
                            {
                                "schema": "prompt-to-play/capture-manifest@1",
                                "run_id": environment["PTP_RUN_ID"],
                                "revision": revision,
                                "world_id": json.loads(
                                    (cwd / "spec" / "world.json").read_text(
                                        encoding="utf-8"
                                    )
                                )["world_id"],
                                "captures": [
                                    {
                                        "path": relative,
                                        "sha256": pipeline._sha256_file(capture),
                                    }
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )
                    current_world = json.loads(
                        (cwd / "spec" / "world.json").read_text(encoding="utf-8")
                    )
                    if current_world["props"][0]["position"][0] <= initial_prop_x:
                        raise pipeline.PipelineIntegrationError(
                            "simulated structural exit after successful capture"
                        )

            stages = pipeline.PipelineStages(
                pipeline.PipelineDependencies(
                    source_repo_root=source_repo,
                    output_root=output_root,
                    environment={"PROMPT_TO_PLAY_ASSET_MODE": "off"},
                    publish_project=fake_publish,
                    run_command=fake_run,
                    agent_runtime_factory=runtime_factory,
                    which=lambda _name: None,
                )
            )
            state = PipelineState(
                LaunchRequest.from_values("生成一座可修复的森林遗迹", [reference])
            )
            stages.plan(state, lambda _message: None)
            stages.validate(state, lambda _message: None)
            stages.publish(state, lambda _message: None)
            original_x = state.require_result(pipeline.WORLD_RESULT)["props"][0][
                "position"
            ][0]
            stages.build(state, lambda _message: None)

            project = state.require_result(pipeline.PROJECT_RESULT)
            request_hash = state.require_result(pipeline.REQUEST_RESULT)["request_hash"]
            run_id = request_hash[:12]
            self.assertEqual(
                state.require_result(pipeline.SELECTED_REVISION_RESULT), 1
            )
            self.assertEqual(
                state.require_result(pipeline.WORLD_RESULT)["props"][0][
                    "position"
                ][0],
                original_x + 1,
            )
            self.assertTrue(
                (
                    project
                    / "artifacts"
                    / "runs"
                    / run_id
                    / "rev_0"
                    / "patch_to_rev_1.json"
                ).is_file()
            )
            second_repair_record = json.loads(
                (
                    project
                    / "artifacts"
                    / "runs"
                    / run_id
                    / "rev_1"
                    / "repair_to_rev_2.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                [task["status"] for task in second_repair_record["tasks"]],
                ["rejected", "applied", "applied"],
            )
            self.assertIn(
                "road endpoints",
                second_repair_record["tasks"][0]["rejection_reason"],
            )
            evaluations = state.require_result(pipeline.EVALUATIONS_RESULT)
            self.assertEqual(
                [item["iteration"] for item in evaluations], [0, 1]
            )
            self.assertEqual(
                [item["status"] for item in evaluations],
                ["fail", "fail"],
            )
            self.assertEqual(evaluations[1]["metrics"]["reproducibility"], 0.5)
            delivery = json.loads(
                (project / "artifacts" / "runs" / run_id / "delivery.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(delivery["mode"], "best_effort")
            self.assertFalse(delivery["evaluation_passed"])
            self.assertEqual(delivery["revision"], 1)
            self.assertEqual(
                delivery["correction_stop"]["reason"],
                "visual_evaluation_rejected",
            )
            self.assertEqual(
                delivery["visual_guidance"][0]["suggested_changes"][0]["field"],
                "scale",
            )
            evaluation_error_path = (
                project
                / "artifacts"
                / "runs"
                / run_id
                / "rev_2"
                / "visual_evaluation_error.json"
            )
            evaluation_error = json.loads(
                evaluation_error_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                evaluation_error["schema"],
                "prompt-to-play/visual-evaluation-error@1",
            )
            self.assertEqual(evaluation_error["status"], "rejected")
            self.assertEqual(
                evaluation_error["error_type"], "VisualFeedbackContractError"
            )
            self.assertEqual(
                evaluation_error["measurement_status"], "not_measured"
            )
            self.assertEqual(
                delivery["visual_evaluation_error"], evaluation_error
            )
            self.assertFalse(
                (evaluation_error_path.with_name("evaluation.json")).exists()
            )
            self.assertFalse(
                (
                    project / "artifacts" / "runs" / run_id / "rev_2"
                    / "visual_feedback.json"
                ).exists()
            )
            persisted_error = evaluation_error_path.read_text(encoding="utf-8")
            persisted_delivery = (
                project / "artifacts" / "runs" / run_id / "delivery.json"
            ).read_text(encoding="utf-8")
            self.assertNotIn("sk-sensitive-evaluator-output", persisted_error)
            self.assertNotIn("sk-sensitive-evaluator-output", persisted_delivery)
            self.assertNotIn(
                "repair_03",
                [payload["task_id"] for payload in RepairProvider.payloads],
            )
            visual_provider = role_providers[agents.AgentRole.VISUAL_EVALUATOR]
            published_reference = next((project / "references").iterdir()).resolve()
            self.assertEqual(
                visual_provider.image_paths[0][0], published_reference
            )
            self.assertEqual(len(visual_provider.image_paths[0]), 2)
            repair_payload = next(
                payload
                for payload in RepairProvider.payloads
                if payload["task_id"] == "repair_01_layout"
            )
            self.assertEqual(
                repair_payload["structural_failures"],
                [
                    {
                        "id": "walkable_collision",
                        "message": "Move the blocking prop away from the walkway",
                    }
                ],
            )
            self.assertEqual(repair_payload["reference_image_count"], 1)
            self.assertEqual(repair_payload["domain"], "layout")
            self.assertEqual(
                repair_payload["assigned_visual_issues"][0]["suggested_changes"][0][
                    "field"
                ],
                "position",
            )
            self.assertEqual(len(refinement_providers), 3)
            trace = json.loads(
                (project / "artifacts" / "runs" / run_id / "agent_trace.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                [call["role"] for call in trace["calls"]].count("repair"), 4
            )
            self.assertEqual(
                {
                    call["task_id"]
                    for call in trace["calls"]
                    if call["role"] == "repair"
                },
                {
                    "repair_01_layout",
                    "repair_02_layout",
                    "repair_02_gameplay",
                    "repair_02_lighting_camera",
                },
            )
            future_project = project.parent / "run-ffffffffffff"
            future_project.mkdir()
            self.assertEqual(
                pipeline._prior_world_hashes(future_project, request_hash),
                frozenset(
                    {contracts.document_sha256(state.require_result(pipeline.WORLD_RESULT))}
                ),
            )

    def test_delivery_gate_accepts_passed_and_best_effort_revisions(self):
        selection_path = Path("artifacts/runs/run/selection.json")
        self.assertEqual(
            pipeline._classify_delivery(
                delivery_evaluation(passed=True), 1, selection_path
            ),
            "passed",
        )
        self.assertEqual(
            pipeline._classify_delivery(
                delivery_evaluation(passed=False, visual_passed=False),
                2,
                selection_path,
            ),
            "best_effort",
        )
        self.assertEqual(
            pipeline._classify_delivery(
                delivery_evaluation(passed=False), 2, selection_path
            ),
            "best_effort",
        )

    def test_delivery_gate_rejects_structural_and_malformed_revisions(self):
        selection_path = Path("artifacts/runs/run/selection.json")
        with self.assertRaisesRegex(
            pipeline.PipelineIntegrationError,
            "failed delivery-blocking checks: scene_loads",
        ):
            pipeline._classify_delivery(
                delivery_evaluation(
                    passed=False, failed_structural="scene_loads"
                ),
                2,
                selection_path,
            )
        missing = delivery_evaluation(passed=False)
        missing["checks"] = [
            check for check in missing["checks"] if check["id"] != "scene_loads"
        ]
        with self.assertRaisesRegex(
            pipeline.PipelineIntegrationError, "missing scene_loads"
        ):
            pipeline._classify_delivery(missing, 2, selection_path)
        with self.assertRaisesRegex(
            pipeline.PipelineIntegrationError, "no valid evaluation result"
        ):
            pipeline._classify_delivery({"result": {"passed": "no"}}, 2, selection_path)


class SubprocessBoundaryTests(unittest.TestCase):
    def test_default_publisher_force_refreshes_the_derived_target(self):
        target = Path("derived-target")
        mocked = Mock(return_value=target)
        fake_module = SimpleNamespace(
            PublishConfig=lambda **values: SimpleNamespace(**values),
            publish=mocked,
        )
        with patch.object(pipeline, "_load_source_publisher", return_value=fake_module):
            self.assertEqual(pipeline._default_publish_project(target), target)
        config = mocked.call_args.args[0]
        self.assertTrue(config.force)
        self.assertEqual(config.target, target)
        self.assertEqual(config.workflow, "prompt-to-play")

    def test_command_runner_uses_argv_and_shell_false(self):
        completed = subprocess.CompletedProcess(["tool", "arg"], 0, "ok\n")
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch(
                "prompt_to_play.pipeline.subprocess.run", return_value=completed
            ) as mocked,
        ):
            messages = []
            pipeline._default_run_command(
                ["tool", "arg"], Path(temporary), {"A": "B"}, messages.append
            )
        args, kwargs = mocked.call_args
        self.assertEqual(args[0], ["tool", "arg"])
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["timeout"], pipeline.DEFAULT_COMMAND_TIMEOUT_SECONDS)
        self.assertIn("ok", messages[-1])

    def test_child_environment_excludes_credentials(self):
        child = pipeline._child_process_environment(
            {
                "PATH": "tools",
                "SystemRoot": r"C:\Windows",
                "OPENAI_API_KEY": "openai-secret",
                "AWS_SECRET_ACCESS_KEY": "aws-secret",
                "GITHUB_TOKEN": "github-secret",
                "PROMPT_TO_PLAY_API_KEY": "planner-secret",
            }
        )
        self.assertEqual(child, {"PATH": "tools", "SystemRoot": r"C:\Windows"})

    def test_process_starter_uses_argv_and_shell_false(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch(
                "prompt_to_play.pipeline.subprocess.Popen", return_value=FakeProcess()
            ) as mocked,
        ):
            result = pipeline._default_start_process(
                ["godot", "--path", temporary],
                Path(temporary),
                {"A": "B"},
                Path(temporary) / "play.log",
            )
        self.assertIsInstance(result, FakeProcess)
        args, kwargs = mocked.call_args
        self.assertEqual(args[0], ["godot", "--path", temporary])
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["stderr"], subprocess.STDOUT)

    def test_main_passes_concrete_commands_to_launcher(self):
        with patch("prompt_to_play.pipeline.run_launcher", return_value=17) as mocked:
            self.assertEqual(pipeline.main(), 17)
        commands = mocked.call_args.args[0]
        self.assertEqual(len(commands.ordered()), 5)


if __name__ == "__main__":
    unittest.main()
