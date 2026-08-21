from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from prompt_to_play import contracts, pipeline
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
                output_root / request_document["request_hash"][:12],
            )
            self.assertRegex(target.name, r"^run-[0-9a-f]{12}$")
            self.assertEqual(captured["prompt"], request.prompt)
            self.assertEqual(
                captured["reference_image_paths"], request.reference_images
            )
            self.assertEqual(captured["project_root"], source_repo)
            self.assertRegex(
                binding.project_path,
                r"^references/00_[0-9a-f]{16}\.png$",
            )
            self.assertEqual(
                captured["references"],
                [{"path": binding.project_path, "sha256": binding.sha256}],
            )
            self.assertNotIn(str(workspace), json.dumps(captured["references"]))

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
                output_root / request_document["request_hash"][:12],
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
                    environment={"PTP_CAPTURE": "1", "OPENAI_API_KEY": "must-not-leak"},
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
            stages.launch(state, lambda _message: None)

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
