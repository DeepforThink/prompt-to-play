from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import publish as publisher  # noqa: E402
from prompt_to_play import direct_generation, direct_pipeline  # noqa: E402
from prompt_to_play.launcher import LaunchRequest, PipelineState  # noqa: E402


class PublishArgumentTests(unittest.TestCase):
    def test_positional_and_out_interfaces_match(self) -> None:
        positional = publisher.parse_config(
            ["--engine", "godot", "--agent", "codex", "game"]
        )
        option = publisher.parse_config(
            ["--engine", "godot", "--agent", "codex", "--out", "game"]
        )

        self.assertEqual(positional, option)
        self.assertEqual(positional.workflow, "autonomous")

    def test_rejects_duplicate_target(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                publisher.parse_config(
                    [
                        "--engine",
                        "godot",
                        "--agent",
                        "codex",
                        "--out",
                        "one",
                        "two",
                    ]
                )

    def test_prompt_to_play_rejects_non_godot_engines(self) -> None:
        for engine in ("bevy", "babylon"):
            with self.subTest(engine=engine):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        publisher.parse_config(
                            [
                                "--engine",
                                engine,
                                "--agent",
                                "codex",
                                "--workflow",
                                "prompt-to-play",
                                "game",
                            ]
                        )


class PublishIntegrationTests(unittest.TestCase):
    def publish_quietly(
        self,
        target: Path,
        *,
        engine: str = "godot",
        agent: str = "codex",
        workflow: str = "autonomous",
        force: bool = False,
        repo_root: Path = REPO_ROOT,
    ) -> Path:
        config = publisher.PublishConfig(
            engine=engine,
            agent=agent,
            workflow=workflow,
            target=target,
            force=force,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            return publisher.publish(config, repo_root=repo_root)

    def test_autonomous_publish_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            for engine in publisher.ENGINES:
                for agent in publisher.AGENTS:
                    with self.subTest(engine=engine, agent=agent):
                        target = temp / f"{engine}-{agent}"
                        self.publish_quietly(target, engine=engine, agent=agent)

                        manifest = "CLAUDE.md" if agent == "claude" else "AGENTS.md"
                        skill_root = (
                            target
                            / (".claude" if agent == "claude" else ".agents")
                            / "skills"
                            / "asset-gen"
                        )
                        self.assertTrue((target / manifest).is_file())
                        self.assertTrue((target / f"{engine}.md").is_file())
                        self.assertTrue((skill_root / "SKILL.md").is_file())

                        skill_text = (skill_root / "SKILL.md").read_text(
                            encoding="utf-8"
                        )
                        self.assertNotIn("${", skill_text)
                        expected_command = (
                            "/asset-gen" if agent == "claude" else "$asset-gen"
                        )
                        self.assertIn(expected_command, skill_text)
                        expected_assets = (
                            "src/assets" if engine == "babylon" else "assets"
                        )
                        self.assertIn(expected_assets, skill_text)

                        metadata = skill_root / "agents" / "openai.yaml"
                        if agent == "codex":
                            self.assertTrue(metadata.is_file())
                            self.assertIn(
                                "$asset-gen",
                                metadata.read_text(encoding="utf-8"),
                            )
                        else:
                            self.assertFalse(metadata.exists())

    def test_prompt_to_play_uses_its_manifest_and_copies_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            target = Path(temp_name) / "game"
            self.publish_quietly(target, workflow="prompt-to-play")

            expected_manifest = (REPO_ROOT / "prompts" / "prompt-to-play.md").read_text(
                encoding="utf-8"
            )
            expected_manifest = (
                expected_manifest.replace("${ENGINE_NAME}", "Godot")
                .replace("${ENGINE_GUIDE_FILE}", "godot.md")
                .replace("${ASSET_SKILL_COMMAND}", "$asset-gen")
            )
            self.assertEqual(
                (target / "AGENTS.md").read_text(encoding="utf-8"),
                expected_manifest,
            )
            runtime_source = REPO_ROOT / "prompt_to_play"
            runtime_target = target / "prompt_to_play"
            for relative_text in publisher.PROMPT_TO_PLAY_RUNTIME_FILES:
                relative = Path(relative_text)
                copied = runtime_target / relative
                source = runtime_source / relative
                self.assertTrue(copied.is_file(), relative)
                self.assertEqual(copied.read_bytes(), source.read_bytes(), relative)

            published_runtime_files = {
                path.relative_to(runtime_target).as_posix()
                for path in runtime_target.rglob("*")
                if path.is_file()
            }
            self.assertEqual(
                published_runtime_files,
                set(publisher.PROMPT_TO_PLAY_RUNTIME_FILES),
            )
            self.assertFalse((target / "prompt_to_play" / "direct_template").exists())
            self.assertFalse((target / "prompt_to_play" / "godot_template").exists())
            for legacy in (
                "assets.py",
                "contracts.py",
                "evaluation_policy.json",
                "evaluator.py",
                "examples",
                "lifecycle.py",
                "pipeline.py",
                "planner.py",
            ):
                with self.subTest(legacy=legacy):
                    self.assertFalse((runtime_target / legacy).exists())

            template = REPO_ROOT / "prompt_to_play" / "direct_template"
            for source in template.rglob("*"):
                if (
                    source.is_file()
                    and "__pycache__" not in source.parts
                    and source.suffix != ".pyc"
                ):
                    relative = source.relative_to(template)
                    copied = target / relative
                    self.assertTrue(copied.is_file(), relative)
                    self.assertEqual(copied.read_bytes(), source.read_bytes(), relative)
            self.assertTrue((target / "project.godot").is_file())
            self.assertTrue((target / "PromptToPlayDirect.csproj").is_file())
            self.assertTrue((target / "harness" / "Main.tscn").is_file())
            self.assertTrue((target / "generated" / "GeneratedGame.cs").is_file())
            self.assertFalse((target / "spec" / "world.json").exists())
            self.assertTrue(
                (
                    target
                    / ".agents"
                    / "skills"
                    / "asset-gen"
                    / "agents"
                    / "openai.yaml"
                ).is_file()
            )

    def test_published_root_is_a_self_contained_direct_pipeline_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temporary = Path(temp_name)
            published_root = temporary / "published"
            self.publish_quietly(published_root, workflow="prompt-to-play")

            package = {
                "schema": direct_generation.DIRECT_GENERATION_CONTRACT,
                "files": [
                    {
                        "path": "generated/GeneratedGame.tscn",
                        "action": "upsert",
                        "content": (
                            "[gd_scene format=3]\n"
                            '[node name="GeneratedGame" type="Node3D"]\n'
                        ),
                    },
                    {
                        "path": "generated/Game.cs",
                        "action": "upsert",
                        "content": (
                            "using Godot; public partial class Game : Node3D {}\n"
                        ),
                    },
                ],
            }
            stages = direct_pipeline.DirectPipelineStages(
                direct_pipeline.DirectPipelineDependencies(
                    source_repo_root=published_root,
                    output_root=temporary / "generated-projects",
                    environment={"PATH": ""},
                    generate_project=lambda *_args, **_kwargs: package,
                    which=lambda _name: None,
                )
            )
            state = PipelineState(LaunchRequest.from_values("a direct test game"))
            stages.plan(state, lambda _message: None)
            stages.validate(state, lambda _message: None)
            stages.publish(state, lambda _message: None)

            generated_project = Path(
                state.require_result(direct_pipeline.PROJECT_RESULT)
            )
            self.assertEqual(
                direct_pipeline._direct_template_source(published_root),
                published_root,
            )
            trusted_files = [
                Path(".gitignore"),
                Path("project.godot"),
                Path("PromptToPlayDirect.csproj"),
                *[
                    path.relative_to(published_root)
                    for path in (published_root / "harness").rglob("*")
                    if path.is_file()
                ],
            ]
            for relative in trusted_files:
                with self.subTest(relative=relative):
                    self.assertEqual(
                        (generated_project / relative).read_bytes(),
                        (published_root / relative).read_bytes(),
                    )
            self.assertEqual(
                (generated_project / "generated" / "GeneratedGame.tscn").read_text(
                    encoding="utf-8"
                ),
                package["files"][0]["content"],
            )
            self.assertFalse(
                (published_root / "prompt_to_play" / "direct_template").exists()
            )

    def test_prompt_republish_preserves_user_scaffold_and_adds_missing_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            target = Path(temp_name) / "game"
            self.publish_quietly(target, workflow="prompt-to-play")

            template = REPO_ROOT / "prompt_to_play" / "direct_template"
            script_source = template / "generated" / "GeneratedGame.cs"
            script_target = target / script_source.relative_to(template)
            project_target = target / "project.godot"
            user_file = target / "generated" / "user_created.cs"
            project_target.write_text("user project settings\n", encoding="utf-8")
            script_target.write_text("// user script edit\n", encoding="utf-8")
            user_file.write_text("// keep me\n", encoding="utf-8")

            missing_source = next(
                path
                for path in sorted(template.rglob("*"))
                if (
                    path.is_file()
                    and path != script_source
                    and path.name != "project.godot"
                )
            )
            missing_target = target / missing_source.relative_to(template)
            missing_target.unlink()

            self.publish_quietly(target, workflow="prompt-to-play")

            self.assertEqual(
                project_target.read_text(encoding="utf-8"), "user project settings\n"
            )
            self.assertEqual(
                script_target.read_text(encoding="utf-8"), "// user script edit\n"
            )
            self.assertEqual(user_file.read_text(encoding="utf-8"), "// keep me\n")
            self.assertEqual(missing_target.read_bytes(), missing_source.read_bytes())
            self.assertFalse((target / "spec" / "world.json").exists())

    def test_prompt_force_rebuild_restores_the_scaffold(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            target = Path(temp_name) / "game"
            self.publish_quietly(target, workflow="prompt-to-play")

            template = REPO_ROOT / "prompt_to_play" / "direct_template"
            script_source = template / "generated" / "GeneratedGame.cs"
            script_target = target / script_source.relative_to(template)
            (target / "project.godot").write_text("modified\n", encoding="utf-8")
            script_target.write_text("modified\n", encoding="utf-8")
            spec_target = target / "spec" / "world.json"
            spec_target.parent.mkdir(parents=True)
            spec_target.write_text("{}\n", encoding="utf-8")
            extra = target / "user-only.txt"
            extra.write_text("remove on force\n", encoding="utf-8")

            self.publish_quietly(target, workflow="prompt-to-play", force=True)

            self.assertEqual(
                (target / "project.godot").read_bytes(),
                (template / "project.godot").read_bytes(),
            )
            self.assertEqual(script_target.read_bytes(), script_source.read_bytes())
            self.assertFalse(spec_target.exists())
            self.assertFalse(extra.exists())

    def test_republish_replaces_only_asset_gen(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            target = Path(temp_name) / "game"
            other_skill = target / ".agents" / "skills" / "keep-me" / "note.txt"
            old_asset = target / ".agents" / "skills" / "asset-gen" / "stale.txt"
            other_skill.parent.mkdir(parents=True)
            other_skill.write_text("preserve", encoding="utf-8")
            old_asset.parent.mkdir(parents=True)
            old_asset.write_text("remove", encoding="utf-8")
            custom_gitignore = target / ".gitignore"
            custom_gitignore.write_text("custom\n", encoding="utf-8")

            self.publish_quietly(target)

            self.assertEqual(other_skill.read_text(encoding="utf-8"), "preserve")
            self.assertFalse(old_asset.exists())
            self.assertEqual(custom_gitignore.read_text(encoding="utf-8"), "custom\n")

    def test_force_cleans_a_safe_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            target = Path(temp_name) / "game"
            target.mkdir()
            sentinel = target / "old-project-file.txt"
            sentinel.write_text("old", encoding="utf-8")

            self.publish_quietly(target, force=True)

            self.assertFalse(sentinel.exists())
            self.assertTrue((target / "AGENTS.md").is_file())

    def test_force_rejects_source_repo_and_ancestors(self) -> None:
        for unsafe in (
            REPO_ROOT,
            REPO_ROOT / "generated-game",
            REPO_ROOT.parent,
            Path(REPO_ROOT.anchor),
        ):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(ValueError):
                    publisher.validate_force_target(unsafe, REPO_ROOT)

    def test_missing_source_is_detected_before_force_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            target = temp / "target"
            incomplete_source = temp / "incomplete-source"
            target.mkdir()
            incomplete_source.mkdir()
            sentinel = target / "keep.txt"
            sentinel.write_text("still here", encoding="utf-8")

            with self.assertRaises(FileNotFoundError):
                self.publish_quietly(
                    target,
                    force=True,
                    repo_root=incomplete_source,
                )

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "still here")

    def test_prompt_source_layout_requires_the_direct_template(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source_root = Path(temp_name) / "source"
            config = publisher.PublishConfig(
                engine="godot",
                agent="codex",
                workflow="prompt-to-play",
                target=Path(temp_name) / "target",
            )
            for name, path in publisher._source_paths(config, source_root).items():
                if name == "workflow_template":
                    continue
                if name in {"asset_skill", "workflow_resources"}:
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("fixture\n", encoding="utf-8")

            with self.assertRaises(FileNotFoundError) as raised:
                publisher.validate_source_layout(config, source_root)

            self.assertIn("direct_template", str(raised.exception))


class WrapperTests(unittest.TestCase):
    def test_shell_wrapper_is_thin_and_uses_lf(self) -> None:
        wrapper = (REPO_ROOT / "publish.sh").read_bytes()
        self.assertTrue(wrapper.startswith(b"#!/usr/bin/env bash\n"))
        self.assertNotIn(b"\r\n", wrapper)
        self.assertIn(b"publish.py", wrapper)
        self.assertNotIn(b"rsync", wrapper)


if __name__ == "__main__":
    unittest.main()
