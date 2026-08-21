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
                        expected_command = "/asset-gen" if agent == "claude" else "$asset-gen"
                        self.assertIn(expected_command, skill_text)
                        expected_assets = "src/assets" if engine == "babylon" else "assets"
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
            for source in (REPO_ROOT / "prompt_to_play").rglob("*"):
                relative = source.relative_to(REPO_ROOT / "prompt_to_play")
                if (
                    source.is_file()
                    and "__pycache__" not in source.parts
                    and source.suffix != ".pyc"
                    and relative.parts[0] != "godot_template"
                ):
                    copied = target / "prompt_to_play" / relative
                    self.assertTrue(copied.is_file(), relative)
                    self.assertEqual(copied.read_bytes(), source.read_bytes(), relative)
            self.assertFalse((target / "prompt_to_play" / "godot_template").exists())

            template = REPO_ROOT / "prompt_to_play" / "godot_template"
            for source in template.rglob("*"):
                if (
                    source.is_file()
                    and "__pycache__" not in source.parts
                    and source.suffix != ".pyc"
                ):
                    relative = source.relative_to(template)
                    if relative == Path("spec/world.json"):
                        continue
                    copied = target / relative
                    self.assertTrue(copied.is_file(), relative)
                    self.assertEqual(copied.read_bytes(), source.read_bytes(), relative)
            self.assertEqual(
                (target / "spec" / "world.json").read_bytes(),
                (REPO_ROOT / "prompt_to_play" / "examples" / "world.json").read_bytes(),
            )
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

    def test_prompt_republish_preserves_user_scaffold_and_adds_missing_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            target = Path(temp_name) / "game"
            self.publish_quietly(target, workflow="prompt-to-play")

            template = REPO_ROOT / "prompt_to_play" / "godot_template"
            script_source = next(
                path for path in sorted((template / "scripts").rglob("*")) if path.is_file()
            )
            script_target = target / script_source.relative_to(template)
            project_target = target / "project.godot"
            spec_target = target / "spec" / "world.json"
            user_file = target / "scripts" / "user_created.cs"
            project_target.write_text("user project settings\n", encoding="utf-8")
            script_target.write_text("// user script edit\n", encoding="utf-8")
            spec_target.write_text('{"user": "world"}\n', encoding="utf-8")
            user_file.write_text("// keep me\n", encoding="utf-8")

            missing_source = next(
                path
                for path in sorted(template.rglob("*"))
                if (
                    path.is_file()
                    and path != script_source
                    and path.name != "project.godot"
                    and path.relative_to(template) != Path("spec/world.json")
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
            self.assertEqual(
                spec_target.read_text(encoding="utf-8"), '{"user": "world"}\n'
            )
            self.assertEqual(user_file.read_text(encoding="utf-8"), "// keep me\n")
            self.assertEqual(missing_target.read_bytes(), missing_source.read_bytes())

    def test_prompt_force_rebuild_restores_the_scaffold(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            target = Path(temp_name) / "game"
            self.publish_quietly(target, workflow="prompt-to-play")

            template = REPO_ROOT / "prompt_to_play" / "godot_template"
            script_source = next(
                path for path in sorted((template / "scripts").rglob("*")) if path.is_file()
            )
            script_target = target / script_source.relative_to(template)
            (target / "project.godot").write_text("modified\n", encoding="utf-8")
            script_target.write_text("modified\n", encoding="utf-8")
            (target / "spec" / "world.json").write_text("{}\n", encoding="utf-8")
            extra = target / "user-only.txt"
            extra.write_text("remove on force\n", encoding="utf-8")

            self.publish_quietly(target, workflow="prompt-to-play", force=True)

            self.assertEqual(
                (target / "project.godot").read_bytes(),
                (template / "project.godot").read_bytes(),
            )
            self.assertEqual(script_target.read_bytes(), script_source.read_bytes())
            self.assertEqual(
                (target / "spec" / "world.json").read_bytes(),
                (REPO_ROOT / "prompt_to_play" / "examples" / "world.json").read_bytes(),
            )
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

    def test_prompt_source_layout_requires_the_godot_template(self) -> None:
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

            self.assertIn("godot_template", str(raised.exception))


class WrapperTests(unittest.TestCase):
    def test_shell_wrapper_is_thin_and_uses_lf(self) -> None:
        wrapper = (REPO_ROOT / "publish.sh").read_bytes()
        self.assertTrue(wrapper.startswith(b"#!/usr/bin/env bash\n"))
        self.assertNotIn(b"\r\n", wrapper)
        self.assertIn(b"publish.py", wrapper)
        self.assertNotIn(b"rsync", wrapper)


if __name__ == "__main__":
    unittest.main()
