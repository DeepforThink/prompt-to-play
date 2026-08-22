from pathlib import Path
import json
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "prompt_to_play" / "godot_template"
SCRIPTS = TEMPLATE / "scripts"


class GodotTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = (SCRIPTS / "WorldRuntime.cs").read_text(encoding="utf-8")
        cls.generated_objects = (SCRIPTS / "GeneratedCodeObjects.cs").read_text(
            encoding="utf-8"
        )
        cls.spec = (SCRIPTS / "WorldSpec.cs").read_text(encoding="utf-8")
        cls.artifacts = (SCRIPTS / "ArtifactWriter.cs").read_text(encoding="utf-8")
        cls.asset_catalog = (SCRIPTS / "AssetCatalog.cs").read_text(encoding="utf-8")
        cls.prefab_resolver = (SCRIPTS / "PrefabResolver.cs").read_text(encoding="utf-8")

    def test_runtime_reports_the_four_policy_hard_checks(self):
        for check_id in (
            "scene_loads",
            "world_graph_connected",
            "objectives_completable",
            "completion_reachable",
        ):
            with self.subTest(check_id=check_id):
                self.assertIn(f'Id = "{check_id}"', self.runtime)

    def test_runtime_has_no_three_core_or_collectible_contract(self):
        combined = self.runtime + self.spec
        for forbidden in (
            "CoreCollectible",
            "RequiredCoreCount",
            "Collectibles",
            "Count == 3",
            "Count is 3",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, combined)
        self.assertIn("spec.Interactions.Interactables", self.runtime)
        self.assertIn("spec.Interactions.Objectives", self.runtime)

    def test_runtime_covers_generic_interactions_and_objective_rules(self):
        interactions = (SCRIPTS / "Interactable.cs").read_text(encoding="utf-8")
        objectives = (SCRIPTS / "ObjectiveManager.cs").read_text(encoding="utf-8")
        for action in ("collect", "activate", "repair", "inspect"):
            self.assertIn(f'"{action}"', interactions + self.runtime)
        for rule in ("all", "any", "sequence"):
            self.assertIn(f'"{rule}"', objectives)

    def test_capture_is_fixed_camera_hashed_and_rejects_uniform_frames(self):
        self.assertIn('OS.GetEnvironment("PTP_CAPTURE")', self.runtime)
        self.assertIn("if (!_worldBuilt)", self.runtime)
        self.assertIn("GetTree().Quit(_exitCode);", self.runtime)
        self.assertLess(
            self.runtime.index("else if (captureRequested)"),
            self.runtime.index("else if (_exitCode != 0)"),
        )
        self.assertIn("HasVisualVariation(image)", self.runtime)
        self.assertIn("SHA256.HashData", self.runtime)
        self.assertIn("capture_manifest.json", self.artifacts)
        self.assertIn("camera_id", self.artifacts)

    def test_artifacts_are_revision_scoped_and_run_id_is_path_safe(self):
        self.assertIn('OS.GetEnvironment("PTP_RUN_ID")', self.artifacts)
        self.assertIn('OS.GetEnvironment("PTP_REVISION")', self.artifacts)
        self.assertIn('rev_{Revision}', self.artifacts)
        self.assertIn("^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", self.artifacts)

    def test_asset_catalog_template_is_empty_valid_and_versioned(self):
        catalog = json.loads(
            (TEMPLATE / "assets" / "catalog.json").read_text(encoding="utf-8")
        )
        self.assertEqual(catalog["schema"], "prompt-to-play/asset-catalog@1")
        self.assertEqual(catalog["assets"], [])
        self.assertIn('CatalogPath = "res://assets/catalog.json"', self.asset_catalog)
        self.assertIn("StringComparer.Ordinal", self.asset_catalog)

    def test_prefab_resolver_safely_loads_glb_before_primitive_fallback(self):
        self.assertIn("GD.Load<PackedScene>(entry.ScenePath)", self.prefab_resolver)
        self.assertIn('AssetRoot = "res://assets/"', self.prefab_resolver)
        self.assertIn('EndsWith(".glb"', self.prefab_resolver)
        self.assertIn("sceneRoot.Scale = Multiply(sceneRoot.Scale, requestedScale)", self.prefab_resolver)
        self.assertIn("ShadowCastingSetting.On", self.prefab_resolver)
        self.assertIn("CreateTrimeshShape", self.prefab_resolver)
        self.assertIn('entityRoot.SetMeta("asset_scene_path"', self.prefab_resolver)
        resolver_call = self.runtime.index("_prefabResolver.TryInstantiate(")
        primitive_branch = self.runtime.index('token.Contains("tree")', resolver_call)
        self.assertLess(resolver_call, primitive_branch)
        self.assertIn('_primitiveFallbackIds.Add(stableId)', self.runtime)

    def test_runtime_prefers_llm_generated_object_code_with_fallback(self):
        self.assertIn("GeneratedCodeObjects.TryBuild(", self.runtime)
        self.assertLess(
            self.runtime.index("GeneratedCodeObjects.TryBuild("),
            self.runtime.index("_prefabResolver.TryInstantiate("),
        )
        self.assertIn("return false;", self.generated_objects)
        self.assertIn("Color ground", self.generated_objects)
        self.assertIn('"region",\n            region.Kind', self.runtime)
        self.assertIn('"road",\n            road.Kind', self.runtime)
        self.assertIn("Vector3[] path", self.generated_objects)

    def test_runtime_has_coherent_visual_fallbacks_without_random_clutter(self):
        self.assertIn("BuildRegionFallback(root, region, size, rng)", self.runtime)
        self.assertIn('OS.GetEnvironment("PROMPT_TO_PLAY_RUNTIME_DECORATION") == "on"', self.runtime)
        self.assertIn('"CenterMarking"', self.runtime)
        self.assertIn('cameraSpec.Kind != "orbit"', self.runtime)

    def test_interactables_try_catalog_assets_before_glow_primitive(self):
        start = self.runtime.index("private void BuildInteractables()")
        section = self.runtime[start:self.runtime.index("private void BuildExit()", start)]
        self.assertLess(
            section.index("_prefabResolver.TryInstantiate("),
            section.index("PrimitiveFactory.AddSphereVisual("),
        )
        self.assertIn('_primitiveFallbackIds.Add(spec.Id)', section)

    def test_template_contains_no_local_build_cache(self):
        forbidden_parts = {".godot", "bin", "obj", "artifacts"}
        leaked = [
            path.relative_to(TEMPLATE).as_posix()
            for path in TEMPLATE.rglob("*")
            if forbidden_parts.intersection(path.relative_to(TEMPLATE).parts)
        ]
        self.assertEqual(leaked, [])


if __name__ == "__main__":
    unittest.main()
