from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "prompt_to_play" / "godot_template"
SCRIPTS = TEMPLATE / "scripts"


class GodotTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = (SCRIPTS / "WorldRuntime.cs").read_text(encoding="utf-8")
        cls.spec = (SCRIPTS / "WorldSpec.cs").read_text(encoding="utf-8")
        cls.artifacts = (SCRIPTS / "ArtifactWriter.cs").read_text(encoding="utf-8")

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
        self.assertIn("HasVisualVariation(image)", self.runtime)
        self.assertIn("SHA256.HashData", self.runtime)
        self.assertIn("capture_manifest.json", self.artifacts)
        self.assertIn("camera_id", self.artifacts)

    def test_artifacts_are_revision_scoped_and_run_id_is_path_safe(self):
        self.assertIn('OS.GetEnvironment("PTP_RUN_ID")', self.artifacts)
        self.assertIn('OS.GetEnvironment("PTP_REVISION")', self.artifacts)
        self.assertIn('rev_{Revision}', self.artifacts)
        self.assertIn("^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", self.artifacts)

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
