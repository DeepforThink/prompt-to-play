from __future__ import annotations

import ast
import hashlib
import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from prompt_to_play import (
    direct_common,
    direct_evaluation,
    direct_generation,
    direct_host,
    direct_pipeline,
)
from prompt_to_play.direct_host import PipelineIntegrationError, Toolchain
from prompt_to_play.launcher import LaunchRequest, PipelineState


def generated_file(path: str, content: str, *, action: str = "upsert"):
    return {"path": path, "action": action, "content": content}


def file_plan(*files):
    return {
        "schema": direct_generation.DIRECT_GENERATION_CONTRACT,
        "files": list(files),
    }


def initial_plan(marker: str = "BROKEN"):
    return file_plan(
        generated_file(
            "generated/GeneratedGame.tscn",
            '[gd_scene format=3]\n[node name="GeneratedGame" type="Node3D"]\n',
        ),
        generated_file(
            "generated/Game.cs",
            f"using Godot; public partial class Game : Node3D {{ /* {marker} */ }}\n",
        ),
    )


def visual_feedback(value: float = 0.85, *, issues=()):
    return {
        "scores": {
            dimension: value for dimension in direct_evaluation.VISUAL_SCORE_DIMENSIONS
        },
        "issues": list(issues),
    }


def passing_structural_report(run_id: str, revision: int, project_sha256: str):
    harness_ids = sorted(
        direct_pipeline.REQUIRED_STRUCTURAL_CHECK_IDS - {"host_process_completed"}
    )
    return {
        "schema": "prompt-to-play/direct-structural-report@1",
        "mode": "direct-project",
        "run_id": run_id,
        "revision": revision,
        "project_sha256": project_sha256,
        "status": "pass",
        "interaction_probe": {
            "required": True,
            "status": "pass",
            "target_count": 1,
            "declared_action_count": 1,
            "attempted_action_count": 1,
            "state_changed": True,
            "evidence": [
                "before_sha256="
                + "a" * 64
                + "; after_sha256="
                + "b" * 64
                + "; changed=true"
            ],
        },
        "checks": [
            {
                "id": check_id,
                "kind": "hard",
                "passed": True,
                "score": 1,
                "message": "test pass",
                "evidence": [],
            }
            for check_id in harness_ids
        ],
        "summary": {"passed": len(harness_ids), "failed": 0},
    }


def write_json(path: Path, document) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


@dataclass(frozen=True)
class Layout:
    workspace: Path
    source_repo: Path
    output_root: Path
    reference: Path
    toolchain: Toolchain


def make_layout(temporary: str) -> Layout:
    workspace = Path(temporary)
    source_repo = workspace / "prompt-to-play"
    template = source_repo / "prompt_to_play" / "direct_template"
    (template / "harness").mkdir(parents=True)
    (template / "generated").mkdir()
    (template / ".gitignore").write_text(".godot/\nartifacts/\n", encoding="utf-8")
    (template / "project.godot").write_text(
        '[application]\nrun/main_scene="res://harness/Main.tscn"\n',
        encoding="utf-8",
    )
    (template / "PromptToPlayDirect.csproj").write_text(
        '<Project Sdk="Godot.NET.Sdk/4.7.1"></Project>\n',
        encoding="utf-8",
    )
    (template / "harness" / "Main.tscn").write_text(
        '[gd_scene format=3]\n[node name="Host" type="Node"]\n',
        encoding="utf-8",
    )
    (template / "harness" / "Host.txt").write_text(
        "trusted template marker\n", encoding="utf-8"
    )
    (template / "generated" / "GeneratedGame.tscn").write_text(
        '[gd_scene format=3]\n[node name="Placeholder" type="Node3D"]\n',
        encoding="utf-8",
    )
    (template / "generated" / "TemplateOnly.cs").write_text(
        "// must not survive direct generation\n", encoding="utf-8"
    )

    dotnet = source_repo / ".tools" / "dotnet-sdk" / "dotnet.exe"
    godot = (
        source_repo / ".tools" / "godot" / "Godot_v4.7.1-stable_mono_win64_console.exe"
    )
    visible = godot.with_name("Godot_v4.7.1-stable_mono_win64.exe")
    nupkgs = godot.parent / "GodotSharp" / "Tools" / "nupkgs"
    dotnet.parent.mkdir(parents=True)
    nupkgs.mkdir(parents=True)
    dotnet.write_bytes(b"fake dotnet")
    godot.write_bytes(b"fake console godot")
    visible.write_bytes(b"fake visible godot")
    (nupkgs / "Godot.NET.Sdk.4.7.1.nupkg").write_bytes(b"fake package")

    reference = workspace / "inputs" / "reference.png"
    reference.parent.mkdir()
    reference.write_bytes(b"reference pixels")
    return Layout(
        workspace=workspace,
        source_repo=source_repo,
        output_root=workspace / "output" / "generated",
        reference=reference,
        toolchain=Toolchain(dotnet, godot, nupkgs, visible),
    )


def make_stages(layout: Layout, **overrides) -> direct_pipeline.DirectPipelineStages:
    values = {
        "source_repo_root": layout.source_repo,
        "output_root": layout.output_root,
        "environment": {"PATH": ""},
        "which": lambda _name: None,
        "launch_probe_seconds": 0,
    }
    values.update(overrides)
    return direct_pipeline.DirectPipelineStages(
        direct_pipeline.DirectPipelineDependencies(**values)
    )


def run_to_publish(
    stages: direct_pipeline.DirectPipelineStages,
    prompt: str,
    references=(),
) -> PipelineState:
    state = PipelineState(LaunchRequest.from_values(prompt, references))
    stages.plan(state, lambda _message: None)
    stages.validate(state, lambda _message: None)
    stages.publish(state, lambda _message: None)
    return state


class AliveProcess:
    pid = 4242

    @staticmethod
    def poll():
        return None


class PassingArtifactRunner:
    """Fail selected builds and emulate trusted Godot JSON/PNG artifacts."""

    def __init__(self, *, failed_build_revisions=()):
        self.failed_build_revisions = set(failed_build_revisions)
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
        self.build_revisions: list[int] = []

    def __call__(self, argv, cwd, environment, log):
        command = tuple(str(value) for value in argv)
        env = dict(environment)
        self.calls.append((command, env))
        if len(command) >= 2 and command[1] == "restore":
            log("restore succeeded")
            return
        if len(command) >= 2 and command[1] == "build":
            revision = int(env["PTP_REVISION"])
            self.build_revisions.append(revision)
            if revision in self.failed_build_revisions:
                log(f"CS1002: revision {revision} has a compiler error")
                raise RuntimeError("compiler failed")
            log(f"build revision {revision} succeeded")
            return

        run_id = env["PTP_RUN_ID"]
        revision = int(env["PTP_REVISION"])
        project_sha256 = env["PTP_PROJECT_SHA256"]
        revision_dir = cwd / "artifacts" / "runs" / run_id / f"rev_{revision}"
        if env.get("PTP_AUTOMATION") == "1":
            write_json(
                revision_dir / "direct_structural_report.json",
                passing_structural_report(run_id, revision, project_sha256),
            )
            return
        if env.get("PTP_CAPTURE") == "1":
            relative = f"artifacts/runs/{run_id}/rev_{revision}/captures/overview.png"
            image = cwd.joinpath(*relative.split("/"))
            image.parent.mkdir(parents=True, exist_ok=True)
            image_bytes = f"rendered revision {revision}".encode()
            image.write_bytes(image_bytes)
            write_json(
                revision_dir / "capture_manifest.json",
                {
                    "schema": "prompt-to-play/direct-capture-manifest@1",
                    "mode": "direct-project",
                    "run_id": run_id,
                    "revision": revision,
                    "project_sha256": project_sha256,
                    "status": "pass",
                    "captures": [
                        {
                            "camera_id": "overview",
                            "path": relative,
                            "sha256": hashlib.sha256(image_bytes).hexdigest(),
                        }
                    ],
                },
            )
            return
        raise AssertionError(f"unexpected command: {command}")


class DirectRuntimeIsolationTests(unittest.TestCase):
    def test_direct_pipeline_imports_no_legacy_world_runtime_modules(self):
        source = Path(direct_pipeline.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                if node.module:
                    imported.add(node.module)
                else:
                    imported.update(alias.name for alias in node.names)
        self.assertFalse(
            imported
            & {"contracts", "lifecycle", "pipeline", "planner", "evaluator", "assets"},
            imported,
        )
        self.assertTrue(
            {
                "agents",
                "direct_common",
                "direct_agents",
                "direct_generation",
                "direct_host",
                "launcher",
            }
            <= imported
        )

    def test_direct_request_hash_and_seed_depend_only_on_prompt_and_image_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "one" / "front.png"
            second = root / "two" / "renamed.png"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"same pixels")
            second.write_bytes(b"same pixels")
            request_a = direct_common.build_request("any playable game", [first])
            request_b = direct_common.build_request("any playable game", [second])
            self.assertEqual(request_a["request_hash"], request_b["request_hash"])
            self.assertEqual(request_a["seed"], request_b["seed"])
            self.assertEqual(request_a["seed"], int(request_a["request_hash"][:8], 16))
            self.assertNotEqual(
                request_a["references"][0]["name"],
                request_b["references"][0]["name"],
            )

    def test_direct_child_environment_does_not_forward_api_secrets(self):
        child = direct_host.child_process_environment(
            {
                "PATH": "tools",
                "TEMP": "tmp",
                "PROMPT_TO_PLAY_API_KEY": "secret",
                "OPENAI_API_KEY": "secret-too",
                "PTP_GODOT_EXE": "configured-before-child-launch",
            }
        )
        self.assertEqual(child, {"PATH": "tools", "TEMP": "tmp"})

    def test_direct_toolchain_discovers_workspace_local_real_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            layout = make_layout(temporary)
            discovered = direct_host.discover_toolchain(
                layout.source_repo, environment={"PATH": ""}, which=lambda _name: None
            )
            self.assertTrue(
                discovered.dotnet_exe.samefile(layout.toolchain.dotnet_exe)
            )
            self.assertTrue(
                discovered.godot_exe.samefile(layout.toolchain.godot_exe)
            )
            self.assertTrue(
                discovered.godot_nupkgs.samefile(layout.toolchain.godot_nupkgs)
            )
            self.assertTrue(
                discovered.visible_exe.samefile(layout.toolchain.visible_exe)
            )


class DirectPipelinePlanningAndPublishingTests(unittest.TestCase):
    def test_plan_sends_arbitrary_prompt_seed_and_reference_without_worldspec(self):
        with tempfile.TemporaryDirectory() as temporary:
            layout = make_layout(temporary)
            captured = {}
            package = initial_plan("ARBITRARY_PROMPT")

            def generate(prompt, *, seed, reference_image_paths):
                captured.update(
                    prompt=prompt,
                    seed=seed,
                    reference_image_paths=tuple(reference_image_paths),
                )
                return package

            stages = make_stages(layout, generate_project=generate)
            state = PipelineState(
                LaunchRequest.from_values(
                    "  海底玻璃穹顶中的零重力解谜游戏  ", [layout.reference]
                )
            )
            stages.plan(state, lambda _message: None)

            request = state.require_result(direct_pipeline.REQUEST_RESULT)
            self.assertEqual(captured["prompt"], state.request.prompt)
            self.assertEqual(captured["seed"], request["seed"])
            self.assertEqual(
                captured["reference_image_paths"], state.request.reference_images
            )
            self.assertEqual(
                state.require_result(direct_pipeline.FILE_PLAN_RESULT), package
            )
            self.assertTrue(
                all("world" not in key.casefold() for key in state.results),
                state.results.keys(),
            )
            self.assertNotIn("world_spec", json.dumps(package).casefold())

    def test_validate_rejects_missing_entry_and_out_of_scope_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            layout = make_layout(temporary)
            stages = make_stages(layout)
            state = PipelineState(LaunchRequest.from_values("any game"))
            state.set_result(
                direct_pipeline.FILE_PLAN_RESULT,
                file_plan(generated_file("generated/Game.cs", "class Game {}")),
            )
            with self.assertRaisesRegex(PipelineIntegrationError, "GeneratedGame.tscn"):
                stages.validate(state, lambda _message: None)

            state.set_result(
                direct_pipeline.FILE_PLAN_RESULT,
                file_plan(generated_file("../Host.cs", "class Host {}")),
            )
            with self.assertRaises(direct_generation.DirectGenerationError):
                stages.validate(state, lambda _message: None)

            state.set_result(
                direct_pipeline.FILE_PLAN_RESULT,
                file_plan(
                    generated_file(
                        "generated/GeneratedGame.tscn",
                        '[gd_scene format=3]\n[node name="Game" type="Node"]\n',
                        action="replace",
                    )
                ),
            )
            with self.assertRaisesRegex(PipelineIntegrationError, "upsert/create"):
                stages.validate(state, lambda _message: None)

    def test_publish_copies_template_reference_and_manifest_without_world_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            layout = make_layout(temporary)
            stages = make_stages(
                layout,
                generate_project=lambda *_args, **_kwargs: initial_plan("PUBLISHED"),
            )
            state = run_to_publish(
                stages,
                "procedural cliff village platformer",
                [layout.reference],
            )

            project = Path(state.require_result(direct_pipeline.PROJECT_RESULT))
            request = state.require_result(direct_pipeline.REQUEST_RESULT)
            run_id = request["request_hash"][:12]
            binding = state.require_result(direct_pipeline.REFERENCES_RESULT)[0]
            copied_reference = project.joinpath(*binding.project_path.split("/"))
            manifest_path = (
                project
                / "artifacts"
                / "runs"
                / run_id
                / "rev_0"
                / "direct_generation_manifest.json"
            )
            persisted_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(
                (project / "harness" / "Host.txt").read_text(encoding="utf-8"),
                "trusted template marker\n",
            )
            self.assertEqual(
                copied_reference.read_bytes(), layout.reference.read_bytes()
            )
            self.assertEqual(
                persisted_manifest,
                state.require_result(direct_pipeline.PROJECT_MANIFEST_RESULT),
            )
            self.assertEqual(
                persisted_manifest["project_sha256"],
                direct_pipeline._generated_digest(project)[0],
            )
            self.assertTrue(
                (
                    project
                    / "artifacts"
                    / "runs"
                    / run_id
                    / "rev_0"
                    / "initial_file_plan.json"
                ).is_file()
            )
            self.assertEqual(list(project.rglob("world.json")), [])
            self.assertFalse((project / "spec").exists())
            self.assertFalse((project / "generated" / "TemplateOnly.cs").exists())


class DirectPipelineBuildTests(unittest.TestCase):
    def test_accepted_revision_outranks_higher_scoring_blocked_revision(self):
        accepted = direct_pipeline.RevisionCandidate(
            revision=1,
            project_sha256="a" * 64,
            build_passed=True,
            structural_passed=True,
            capture_passed=True,
            scene_similarity=0.80,
            accepted=True,
            evaluation_path="rev_1/direct_evaluation.json",
        )
        blocked = direct_pipeline.RevisionCandidate(
            revision=0,
            project_sha256="b" * 64,
            build_passed=True,
            structural_passed=True,
            capture_passed=True,
            scene_similarity=0.99,
            accepted=False,
            evaluation_path="rev_0/direct_evaluation.json",
        )
        self.assertIs(
            max((blocked, accepted), key=direct_pipeline._candidate_key), accepted
        )

    def test_build_repairs_compiler_failure_then_evaluates_selects_and_launches_hash(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            layout = make_layout(temporary)
            runner = PassingArtifactRunner(failed_build_revisions={0})
            repairs = []
            evaluations = []
            launches = []

            def repair(
                prompt,
                current_files,
                *,
                diagnostics,
                structural_report,
                visual_feedback,
                screenshot_image_paths,
                reference_image_paths,
            ):
                repairs.append(
                    {
                        "prompt": prompt,
                        "files": dict(current_files),
                        "diagnostics": diagnostics,
                        "structural": dict(structural_report),
                        "visual": dict(visual_feedback),
                        "screenshots": list(screenshot_image_paths),
                        "references": list(reference_image_paths),
                    }
                )
                return file_plan(
                    generated_file(
                        "generated/Game.cs",
                        "using Godot; public partial class Game : Node3D "
                        "{ /* FIXED */ }\n",
                    )
                )

            def evaluate(
                prompt,
                screenshots,
                *,
                reference_image_paths,
                project_manifest,
            ):
                evaluations.append(
                    {
                        "prompt": prompt,
                        "screenshots": list(screenshots),
                        "references": list(reference_image_paths),
                        "manifest": dict(project_manifest),
                    }
                )
                return visual_feedback(0.91)

            def start_process(argv, cwd, environment, log_path):
                launches.append(
                    (
                        tuple(str(value) for value in argv),
                        cwd,
                        dict(environment),
                        log_path,
                    )
                )
                return AliveProcess()

            stages = make_stages(
                layout,
                generate_project=lambda *_args, **_kwargs: initial_plan(),
                repair_project=repair,
                evaluate_visual=evaluate,
                run_command=runner,
                start_process=start_process,
                max_correction_iterations=2,
            )
            state = run_to_publish(
                stages,
                "arbitrary controllable canyon glider",
                [layout.reference],
            )
            stages.build(state, lambda _message: None)

            self.assertEqual(runner.build_revisions, [0, 1])
            self.assertEqual(len(repairs), 1)
            self.assertIn("CS1002", repairs[0]["diagnostics"])
            self.assertIn("BROKEN", repairs[0]["files"]["generated/Game.cs"])
            self.assertEqual(repairs[0]["structural"]["status"], "not_run")
            self.assertEqual(
                repairs[0]["visual"]["issues"][0]["code"], "capture_missing"
            )
            self.assertEqual(repairs[0]["screenshots"], [])
            self.assertEqual(len(repairs[0]["references"]), 1)
            self.assertEqual(len(evaluations), 1)
            self.assertEqual(evaluations[0]["prompt"], state.request.prompt)
            self.assertEqual(len(evaluations[0]["screenshots"]), 1)
            self.assertEqual(len(evaluations[0]["references"]), 1)
            self.assertEqual(
                state.require_result(direct_pipeline.SELECTED_REVISION_RESULT), 1
            )
            reports = state.require_result(direct_pipeline.EVALUATIONS_RESULT)
            self.assertEqual([report["revision"] for report in reports], [0, 1])
            self.assertFalse(reports[0]["checks"]["build"])
            self.assertTrue(reports[1]["result"]["passed"])

            selected_manifest = state.require_result(
                direct_pipeline.PROJECT_MANIFEST_RESULT
            )
            selected_hash = selected_manifest["project_sha256"]
            self.assertEqual(
                evaluations[0]["manifest"]["project_sha256"], selected_hash
            )
            project = Path(state.require_result(direct_pipeline.PROJECT_RESULT))
            selection = json.loads(
                (
                    project
                    / "artifacts"
                    / "runs"
                    / state.require_result(direct_pipeline.REQUEST_RESULT)[
                        "request_hash"
                    ][:12]
                    / "selection.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(selection["selected_project_sha256"], selected_hash)
            artifact_root = project / "artifacts" / "runs" / selection["run_id"]
            for revision in (0, 1):
                self.assertTrue(
                    (
                        artifact_root / f"rev_{revision}" / "generated_snapshot.zip"
                    ).is_file()
                )
            self.assertEqual(list(artifact_root.rglob("*.cs")), [])

            stages.launch(state, lambda _message: None)
            self.assertEqual(len(launches), 1)
            _command, _cwd, launch_environment, _log_path = launches[0]
            self.assertEqual(launch_environment["PTP_REVISION"], "1")
            self.assertEqual(launch_environment["PTP_PROJECT_SHA256"], selected_hash)
            self.assertNotIn("PTP_AUTOMATION", launch_environment)
            self.assertNotIn("PTP_CAPTURE", launch_environment)

    def test_build_repeats_until_gate_but_never_exceeds_internal_cap(self):
        with tempfile.TemporaryDirectory() as temporary:
            layout = make_layout(temporary)
            with self.assertRaisesRegex(ValueError, "between 0 and 6"):
                make_stages(layout, max_correction_iterations=7)

            runner = PassingArtifactRunner(failed_build_revisions={0, 1, 2, 3, 4})
            repair_calls = []

            def repair(_prompt, _files, **_evidence):
                index = len(repair_calls) + 1
                repair_calls.append(index)
                return file_plan(
                    generated_file(
                        "generated/Game.cs",
                        "using Godot; public partial class Game : Node3D "
                        f"{{ /* repair {index} */ }}\n",
                    )
                )

            stages = make_stages(
                layout,
                generate_project=lambda *_args, **_kwargs: initial_plan(),
                repair_project=repair,
                run_command=runner,
                max_correction_iterations=4,
            )
            state = run_to_publish(stages, "game that remains broken")
            with self.assertRaisesRegex(PipelineIntegrationError, "没有得到可编译"):
                stages.build(state, lambda _message: None)

            self.assertEqual(runner.build_revisions, [0, 1, 2, 3, 4])
            self.assertEqual(repair_calls, [1, 2, 3, 4])
            self.assertEqual(
                len(state.require_result(direct_pipeline.EVALUATIONS_RESULT)), 5
            )
            project = Path(state.require_result(direct_pipeline.PROJECT_RESULT))
            run_id = state.require_result(direct_pipeline.REQUEST_RESULT)[
                "request_hash"
            ][:12]
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
            self.assertTrue(
                (
                    project
                    / "artifacts"
                    / "runs"
                    / run_id
                    / "rev_1"
                    / "patch_to_rev_2.json"
                ).is_file()
            )
            self.assertTrue(
                (
                    project
                    / "artifacts"
                    / "runs"
                    / run_id
                    / "rev_3"
                    / "patch_to_rev_4.json"
                ).is_file()
            )

    def test_visual_gate_exhaustion_preserves_best_but_does_not_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            layout = make_layout(temporary)
            runner = PassingArtifactRunner()
            repair_count = 0

            def repair(_prompt, _files, **_evidence):
                nonlocal repair_count
                repair_count += 1
                return file_plan(
                    generated_file(
                        "generated/Game.cs",
                        "using Godot; public partial class Game : Node3D "
                        f"{{ /* visual repair {repair_count} */ }}\n",
                    )
                )

            blocker = {
                "code": "primitive_blockout",
                "severity": "blocker",
                "entity_id": None,
                "message": "The scene is still a primitive blockout.",
                "suggested_fix": "Add authored terrain, set dressing, and lighting.",
            }
            stages = make_stages(
                layout,
                generate_project=lambda *_args, **_kwargs: initial_plan(),
                evaluate_visual=lambda *_args, **_kwargs: visual_feedback(
                    0.95, issues=[blocker]
                ),
                repair_project=repair,
                run_command=runner,
                max_correction_iterations=2,
            )
            state = run_to_publish(stages, "polished mountain driving game")
            with self.assertRaisesRegex(PipelineIntegrationError, "未达到视觉与交互"):
                stages.build(state, lambda _message: None)

            self.assertEqual(repair_count, 2)
            self.assertEqual(
                state.require_result(direct_pipeline.SELECTED_REVISION_RESULT), 0
            )
            project = Path(state.require_result(direct_pipeline.PROJECT_RESULT))
            run_id = state.require_result(direct_pipeline.REQUEST_RESULT)[
                "request_hash"
            ][:12]
            selection = json.loads(
                (project / "artifacts" / "runs" / run_id / "selection.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(selection["stop_reason"], "correction_limit")
            self.assertFalse(selection["candidates"][0]["accepted"])


class DirectPipelineEvidenceValidationTests(unittest.TestCase):
    def test_failed_godot_process_cannot_leave_a_passing_structural_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            layout = make_layout(temporary)
            project = layout.workspace / "project"
            project.mkdir()
            run_id = "f" * 12
            project_hash = "e" * 64

            def runner(_argv, cwd, environment, _log):
                revision = int(environment["PTP_REVISION"])
                write_json(
                    cwd
                    / "artifacts"
                    / "runs"
                    / run_id
                    / f"rev_{revision}"
                    / "direct_structural_report.json",
                    passing_structural_report(run_id, revision, project_hash),
                )
                raise RuntimeError("Godot crashed after writing a report")

            stages = make_stages(layout, run_command=runner)
            report, diagnostics = stages._run_structural(
                project,
                layout.toolchain,
                run_id,
                0,
                project_hash,
                lambda _message: None,
            )

            self.assertEqual(report["status"], "fail")
            self.assertFalse(direct_pipeline._structural_passed(report))
            host_check = next(
                check
                for check in report["checks"]
                if check["id"] == "host_process_completed"
            )
            self.assertFalse(host_check["passed"])
            self.assertIn("did not exit cleanly", diagnostics)

    def test_generated_snapshot_is_inert_and_round_trips_selected_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            generated = project / "generated"
            revision = project / "artifacts" / "runs" / "run" / "rev_0"
            generated.mkdir(parents=True)
            revision.mkdir(parents=True)
            original = "using Godot; public partial class Game : Node {}\n"
            (generated / "Game.cs").write_text(original, encoding="utf-8")
            (generated / "GeneratedGame.tscn").write_text(
                '[gd_scene format=3]\n[node name="Game" type="Node"]\n',
                encoding="utf-8",
            )
            digest = direct_pipeline._generated_digest(project)[0]

            direct_pipeline._snapshot_generated(project, revision, digest)
            self.assertTrue((revision / "generated_snapshot.zip").is_file())
            self.assertFalse((revision / "generated_snapshot").exists())
            self.assertEqual(list(revision.rglob("*.cs")), [])

            (generated / "Game.cs").write_text("broken\n", encoding="utf-8")
            direct_pipeline._restore_generated(project, revision, digest)
            self.assertEqual(
                (generated / "Game.cs").read_text(encoding="utf-8"), original
            )
            self.assertEqual(direct_pipeline._generated_digest(project)[0], digest)

    def test_structural_report_identity_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            layout = make_layout(temporary)
            project = layout.workspace / "project"
            project.mkdir()
            run_id = "a" * 12
            project_hash = "b" * 64

            def runner(_argv, cwd, environment, _log):
                revision = int(environment["PTP_REVISION"])
                write_json(
                    cwd
                    / "artifacts"
                    / "runs"
                    / run_id
                    / f"rev_{revision}"
                    / "direct_structural_report.json",
                    {
                        "schema": "prompt-to-play/direct-structural-report@1",
                        "run_id": "wrong-run-id",
                        "revision": revision,
                        "project_sha256": project_hash,
                        "status": "pass",
                        "checks": [],
                    },
                )

            stages = make_stages(layout, run_command=runner)
            with self.assertRaisesRegex(PipelineIntegrationError, "identity mismatch"):
                stages._run_structural(
                    project,
                    layout.toolchain,
                    run_id,
                    0,
                    project_hash,
                    lambda _message: None,
                )

    def test_capture_content_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            layout = make_layout(temporary)
            project = layout.workspace / "project"
            project.mkdir()
            run_id = "c" * 12
            project_hash = "d" * 64

            def runner(_argv, cwd, environment, _log):
                revision = int(environment["PTP_REVISION"])
                relative = f"artifacts/runs/{run_id}/rev_{revision}/captures/main.png"
                image = cwd.joinpath(*relative.split("/"))
                image.parent.mkdir(parents=True, exist_ok=True)
                image.write_bytes(b"actual rendered pixels")
                write_json(
                    cwd
                    / "artifacts"
                    / "runs"
                    / run_id
                    / f"rev_{revision}"
                    / "capture_manifest.json",
                    {
                        "schema": "prompt-to-play/direct-capture-manifest@1",
                        "mode": "direct-project",
                        "run_id": run_id,
                        "revision": revision,
                        "project_sha256": project_hash,
                        "status": "pass",
                        "captures": [
                            {
                                "path": relative,
                                "sha256": "0" * 64,
                            }
                        ],
                    },
                )

            stages = make_stages(layout, run_command=runner)
            with self.assertRaisesRegex(PipelineIntegrationError, "hash mismatch"):
                stages._capture(
                    project,
                    layout.toolchain,
                    run_id,
                    0,
                    project_hash,
                    lambda _message: None,
                )

    def test_failed_capture_process_cannot_leave_a_passing_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            layout = make_layout(temporary)
            project = layout.workspace / "project"
            project.mkdir()
            run_id = "9" * 12
            project_hash = "8" * 64

            def runner(_argv, cwd, environment, _log):
                revision = int(environment["PTP_REVISION"])
                relative = f"artifacts/runs/{run_id}/rev_{revision}/captures/main.png"
                image = cwd.joinpath(*relative.split("/"))
                image.parent.mkdir(parents=True, exist_ok=True)
                image.write_bytes(b"captured before crash")
                write_json(
                    cwd
                    / "artifacts"
                    / "runs"
                    / run_id
                    / f"rev_{revision}"
                    / "capture_manifest.json",
                    {
                        "schema": "prompt-to-play/direct-capture-manifest@1",
                        "mode": "direct-project",
                        "run_id": run_id,
                        "revision": revision,
                        "project_sha256": project_hash,
                        "status": "pass",
                        "captures": [
                            {
                                "camera_id": "main",
                                "path": relative,
                                "sha256": hashlib.sha256(
                                    b"captured before crash"
                                ).hexdigest(),
                            }
                        ],
                    },
                )
                raise RuntimeError("capture process crashed")

            stages = make_stages(layout, run_command=runner)
            manifest, images, paths, diagnostics = stages._capture(
                project,
                layout.toolchain,
                run_id,
                0,
                project_hash,
                lambda _message: None,
            )

            self.assertEqual(manifest["status"], "error")
            self.assertFalse(manifest["host_process_succeeded"])
            self.assertEqual(images, [])
            self.assertEqual(paths, [])
            self.assertIn("did not exit cleanly", diagnostics)


if __name__ == "__main__":
    unittest.main()
