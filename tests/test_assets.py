from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from prompt_to_play import assets, contracts


ROOT = Path(__file__).resolve().parents[1]
WORLD = json.loads(
    (ROOT / "prompt_to_play" / "examples" / "forest_world.json").read_text(
        encoding="utf-8"
    )
)
PNG_BYTES = b"\x89PNG\r\n\x1a\n"
GLB_BYTES = b"glTF\x02\x00\x00\x00\x0c\x00\x00\x00"


class FakeAssetRunner:
    def __init__(self, *, fail_prefab_text: str | None = None, secret: str = ""):
        self.calls = []
        self.fail_prefab_text = fail_prefab_text
        self.secret = secret

    def __call__(self, argv, cwd, environment):
        command = list(argv)
        self.calls.append((command, Path(cwd), dict(environment)))
        if self.fail_prefab_text and any(
            self.fail_prefab_text in value for value in command
        ):
            return assets.AssetCommandResult(
                1,
                stdout=json.dumps(
                    {"ok": False, "error": f"provider rejected {self.secret}"}
                ),
            )

        output = Path(command[command.index("-o") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        if "image" in command:
            output.write_bytes(PNG_BYTES)
        elif "glb" in command or "resume" in command:
            output.write_bytes(GLB_BYTES)
        else:
            raise AssertionError(f"unexpected asset command: {command}")
        return assets.AssetCommandResult(
            0, stdout=json.dumps({"ok": True, "path": str(output)})
        )


class AssetRequestTests(unittest.TestCase):
    def test_requests_are_semantic_unique_and_prioritized(self):
        world = copy.deepcopy(WORLD)
        shared = copy.deepcopy(world["props"][0])
        shared["id"] = "shared_gate_crank_prop"
        shared["prefab"] = "interaction/gate_crank"
        world["props"].append(shared)

        requests = assets.derive_asset_requests(world, image_model="grok")
        by_prefab = {request.prefab: request for request in requests}

        self.assertEqual(len(requests), 4)
        self.assertEqual(
            by_prefab["interaction/gate_crank"].roles,
            ("interactable", "prop"),
        )
        self.assertIn("repair: 修复石门绞盘", by_prefab["interaction/gate_crank"].prompt)
        self.assertEqual(
            [request.priority for request in requests],
            sorted(request.priority for request in requests),
        )
        self.assertTrue(
            all(request.priority == 0 for request in requests[:2]), requests
        )

    def test_cache_keys_ignore_entity_order_and_placement_but_track_recipe(self):
        moved = copy.deepcopy(WORLD)
        moved["props"].reverse()
        moved["interactions"]["interactables"].reverse()
        moved["props"][0]["position"] = [100, 2, -40]

        original = {
            request.prefab: request.cache_key
            for request in assets.derive_asset_requests(WORLD, image_model="grok")
        }
        reordered = {
            request.prefab: request.cache_key
            for request in assets.derive_asset_requests(moved, image_model="grok")
        }
        gemini = {
            request.prefab: request.cache_key
            for request in assets.derive_asset_requests(WORLD, image_model="gemini")
        }

        self.assertEqual(original, reordered)
        self.assertNotEqual(original, gemini)
        self.assertTrue(all(len(value) == 64 for value in original.values()))


class AssetConfigurationTests(unittest.TestCase):
    def test_safe_defaults_and_credential_based_model_selection(self):
        default = assets.AssetAgentConfig.from_environment({})
        self.assertEqual(default.mode, "off")
        self.assertEqual(default.max_generations, 3)
        self.assertEqual(default.image_model, "grok")

        gemini = assets.AssetAgentConfig.from_environment(
            {"GOOGLE_API_KEY": "present"}
        )
        self.assertEqual(gemini.image_model, "gemini")
        explicit = assets.AssetAgentConfig.from_environment(
            {
                "XAI_API_KEY": "present",
                "PROMPT_TO_PLAY_ASSET_IMAGE_MODEL": "gemini",
            }
        )
        self.assertEqual(explicit.image_model, "gemini")

    def test_rejects_invalid_host_policy(self):
        bad_environments = (
            {"PROMPT_TO_PLAY_ASSET_MODE": "sometimes"},
            {"PROMPT_TO_PLAY_ASSET_MAX_GENERATIONS": "many"},
            {"PROMPT_TO_PLAY_ASSET_MAX_GENERATIONS": "13"},
            {"PROMPT_TO_PLAY_ASSET_IMAGE_MODEL": "unknown"},
        )
        for environment in bad_environments:
            with self.subTest(environment=environment):
                with self.assertRaises(assets.AssetConfigurationError):
                    assets.AssetAgentConfig.from_environment(environment)


class AssetAgentTests(unittest.TestCase):
    def make_layout(self, temporary):
        root = Path(temporary)
        source = root / "source"
        tool = source / "asset-gen" / "tools" / "asset_gen.py"
        tool.parent.mkdir(parents=True)
        tool.write_text("# injectable test tool\n", encoding="utf-8")
        cache = root / "cache"
        project = root / "project"
        return source, cache, project

    def make_agent(
        self,
        source,
        cache,
        *,
        environment=None,
        runner=None,
        messages=None,
    ):
        sink = [] if messages is None else messages
        agent = assets.AssetAgent(
            source_repo_root=source,
            cache_root=cache,
            environment={} if environment is None else environment,
            command_runner=runner,
            log=sink.append,
        )
        return agent, sink

    def test_off_mode_emits_strict_runtime_catalog_and_rich_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, cache, project = self.make_layout(temporary)
            runner = FakeAssetRunner()
            agent, _messages = self.make_agent(source, cache, runner=runner)

            result = agent.run(WORLD, project)

            self.assertEqual(runner.calls, [])
            self.assertEqual(
                set(result.catalog),
                {"schema", "assets"},
            )
            self.assertTrue(
                all(set(item) == {"prefab", "scene_path"} for item in result.catalog["assets"])
            )
            self.assertTrue(
                all(item["scene_path"] is None for item in result.catalog["assets"])
            )
            self.assertEqual(result.manifest["agent"]["name"], "AssetAgent")
            self.assertEqual(
                {item["status"] for item in result.manifest["assets"]},
                {"unresolved"},
            )
            self.assertEqual(
                result.catalog_path.read_bytes(),
                contracts.canonical_json_bytes(result.catalog) + b"\n",
            )
            self.assertEqual(
                result.manifest_path.read_bytes(),
                contracts.canonical_json_bytes(result.manifest) + b"\n",
            )

    def test_off_mode_still_publishes_verified_content_cache_hits(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, cache, project = self.make_layout(temporary)
            request = assets.derive_asset_requests(WORLD)[0]
            cached = cache / request.cache_key / "model.glb"
            cached.parent.mkdir(parents=True)
            cached.write_bytes(GLB_BYTES)
            agent, _messages = self.make_agent(source, cache)

            result = agent.run(WORLD, project)
            manifest_entry = next(
                item
                for item in result.manifest["assets"]
                if item["prefab"] == request.prefab
            )

            self.assertEqual(manifest_entry["status"], "cached")
            self.assertEqual(manifest_entry["source"], "content-cache")
            self.assertEqual(
                manifest_entry["sha256"], hashlib.sha256(GLB_BYTES).hexdigest()
            )
            self.assertTrue(manifest_entry["scene_path"].startswith("res://assets/"))
            published = project / manifest_entry["scene_path"].removeprefix("res://")
            self.assertEqual(published.read_bytes(), GLB_BYTES)

    def test_auto_without_both_credentials_is_explicitly_unresolved(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, cache, project = self.make_layout(temporary)
            runner = FakeAssetRunner()
            agent, _messages = self.make_agent(
                source,
                cache,
                environment={
                    "PROMPT_TO_PLAY_ASSET_MODE": "auto",
                    "XAI_API_KEY": "image-only",
                },
                runner=runner,
            )

            result = agent.run(WORLD, project)

            self.assertEqual(runner.calls, [])
            self.assertTrue(
                all(item["status"] == "unresolved" for item in result.manifest["assets"])
            )
            self.assertTrue(
                all("TRIPO3D_API_KEY" in item["error"] for item in result.manifest["assets"])
            )

    def test_two_stage_generation_is_capped_and_prioritizes_interactables(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, cache, project = self.make_layout(temporary)
            runner = FakeAssetRunner()
            logs = []
            environment = {
                "PROMPT_TO_PLAY_ASSET_MODE": "auto",
                "PROMPT_TO_PLAY_ASSET_MAX_GENERATIONS": "2",
                "XAI_API_KEY": "xai-secret",
                "TRIPO3D_API_KEY": "tripo-secret",
                "OPENAI_API_KEY": "must-not-be-forwarded",
            }
            agent, _messages = self.make_agent(
                source,
                cache,
                environment=environment,
                runner=runner,
                messages=logs,
            )

            result = agent.run(WORLD, project)

            self.assertEqual(result.attempted_generations, 2)
            self.assertEqual(result.generated_assets, 2)
            self.assertEqual(len(runner.calls), 4)
            self.assertEqual(
                [call[0][2] for call in runner.calls],
                ["image", "glb", "image", "glb"],
            )
            for _command, _cwd, child_environment in runner.calls:
                self.assertNotIn("OPENAI_API_KEY", child_environment)
                self.assertEqual(child_environment["XAI_API_KEY"], "xai-secret")
                self.assertEqual(
                    child_environment["TRIPO3D_API_KEY"], "tripo-secret"
                )
            generated = [
                item for item in result.manifest["assets"] if item["status"] == "generated"
            ]
            self.assertEqual(
                {item["roles"][0] for item in generated}, {"interactable"}
            )
            unresolved = [
                item for item in result.manifest["assets"] if item["status"] == "unresolved"
            ]
            self.assertTrue(
                all("generation limit" in item["error"] for item in unresolved)
            )
            self.assertNotIn("xai-secret", "\n".join(logs))
            self.assertNotIn("tripo-secret", "\n".join(logs))

    def test_generated_cache_is_reused_without_credentials_or_api_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, cache, first_project = self.make_layout(temporary)
            first_runner = FakeAssetRunner()
            first, _messages = self.make_agent(
                source,
                cache,
                environment={
                    "PROMPT_TO_PLAY_ASSET_MODE": "auto",
                    "PROMPT_TO_PLAY_ASSET_MAX_GENERATIONS": "1",
                    "XAI_API_KEY": "x",
                    "TRIPO3D_API_KEY": "t",
                },
                runner=first_runner,
            )
            first.run(WORLD, first_project)

            second_runner = FakeAssetRunner()
            second, _messages = self.make_agent(
                source, cache, runner=second_runner
            )
            result = second.run(WORLD, Path(temporary) / "second-project")

            self.assertEqual(second_runner.calls, [])
            self.assertEqual(result.cache_hits, 1)
            self.assertEqual(
                sum(item["status"] == "cached" for item in result.manifest["assets"]),
                1,
            )

    def test_failures_are_redacted_recorded_and_do_not_abort_auto_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, cache, project = self.make_layout(temporary)
            secret = "super-secret-token"
            runner = FakeAssetRunner(
                fail_prefab_text="interaction/gate_crank", secret=secret
            )
            logs = []
            agent, _messages = self.make_agent(
                source,
                cache,
                environment={
                    "PROMPT_TO_PLAY_ASSET_MODE": "auto",
                    "PROMPT_TO_PLAY_ASSET_MAX_GENERATIONS": "1",
                    "XAI_API_KEY": secret,
                    "TRIPO3D_API_KEY": "tripo-secret",
                },
                runner=runner,
                messages=logs,
            )

            result = agent.run(WORLD, project)
            failed = [
                item for item in result.manifest["assets"] if item["status"] == "failed"
            ]

            self.assertEqual(len(failed), 1)
            self.assertIn("[REDACTED]", failed[0]["error"])
            self.assertNotIn(secret, result.manifest_path.read_text(encoding="utf-8"))
            self.assertNotIn(secret, "\n".join(logs))
            self.assertEqual(len(result.manifest["assets"]), 4)

    def test_required_mode_writes_all_evidence_then_raises(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, cache, project = self.make_layout(temporary)
            agent, _messages = self.make_agent(
                source,
                cache,
                environment={"PROMPT_TO_PLAY_ASSET_MODE": "required"},
                runner=FakeAssetRunner(),
            )

            with self.assertRaises(assets.AssetResolutionError) as raised:
                agent.run(WORLD, project)

            self.assertTrue((project / "assets" / "catalog.json").is_file())
            self.assertTrue((project / "assets" / "manifest.json").is_file())
            manifest = json.loads(
                (project / "assets" / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(manifest["assets"]), 4)
            self.assertEqual(len(raised.exception.unresolved_prefabs), 4)
            self.assertEqual(
                raised.exception.manifest_path,
                project.resolve() / "assets" / "manifest.json",
            )

    def test_existing_tripo_sidecar_uses_resume_instead_of_resubmitting(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, cache, project = self.make_layout(temporary)
            request = assets.derive_asset_requests(WORLD)[0]
            request_cache = cache / request.cache_key
            request_cache.mkdir(parents=True)
            (request_cache / "reference.png").write_bytes(PNG_BYTES)
            (request_cache / "model.glb.tripo.json").write_text(
                '{"status":"pending"}\n', encoding="utf-8"
            )
            runner = FakeAssetRunner()
            agent, _messages = self.make_agent(
                source,
                cache,
                environment={
                    "PROMPT_TO_PLAY_ASSET_MODE": "auto",
                    "PROMPT_TO_PLAY_ASSET_MAX_GENERATIONS": "1",
                    "XAI_API_KEY": "x",
                    "TRIPO3D_API_KEY": "t",
                },
                runner=runner,
            )

            agent.run(WORLD, project)

            self.assertEqual(len(runner.calls), 1)
            self.assertIn("resume", runner.calls[0][0])
            self.assertNotIn("glb", runner.calls[0][0][2:3])


if __name__ == "__main__":
    unittest.main()
