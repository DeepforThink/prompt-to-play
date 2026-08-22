from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "prompt_to_play" / "direct_template"
HARNESS = TEMPLATE / "harness"
GENERATED = TEMPLATE / "generated"


class DirectTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = (TEMPLATE / "project.godot").read_text(encoding="utf-8")
        cls.csproj = (TEMPLATE / "PromptToPlayDirect.csproj").read_text(
            encoding="utf-8"
        )
        cls.main_scene = (HARNESS / "Main.tscn").read_text(encoding="utf-8")
        cls.harness = (HARNESS / "DirectHarness.cs").read_text(encoding="utf-8")
        cls.writer = (HARNESS / "DirectArtifactWriter.cs").read_text(encoding="utf-8")
        cls.probe = (HARNESS / "DirectInteractionProbe.cs").read_text(
            encoding="utf-8"
        )
        cls.generated_scene = (GENERATED / "GeneratedGame.tscn").read_text(
            encoding="utf-8"
        )
        cls.generated_code = (GENERATED / "GeneratedGame.cs").read_text(
            encoding="utf-8"
        )
        cls.readme = (TEMPLATE / "README.md").read_text(encoding="utf-8")

    def test_template_has_stable_harness_and_model_owned_generated_surface(self):
        self.assertIn('run/main_scene="res://harness/Main.tscn"', self.project)
        self.assertIn("res://harness/DirectHarness.cs", self.main_scene)
        self.assertIn("res://generated/GeneratedGame.tscn", self.harness)
        self.assertIn("res://generated/GeneratedGame.cs", self.generated_scene)
        for path in (
            "project.godot",
            "PromptToPlayDirect.csproj",
            ".gitignore",
            "harness/Main.tscn",
            "harness/DirectHarness.cs",
            "harness/DirectArtifactWriter.cs",
            "harness/DirectInteractionProbe.cs",
            "generated/GeneratedGame.cs",
            "generated/GeneratedGame.tscn",
        ):
            with self.subTest(path=path):
                self.assertIn(path, self.readme)

    def test_template_does_not_depend_on_a_predeclared_world_document(self):
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in TEMPLATE.rglob("*")
            if path.is_file()
        )
        for forbidden in (
            "spec/world.json",
            "WorldRuntime",
            "WorldSpec.cs",
            "JsonSerializer.Deserialize",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, combined)

    def test_project_targets_required_godot_and_dotnet_versions(self):
        self.assertIn("Godot.NET.Sdk/4.7.1", self.csproj)
        self.assertIn("<TargetFramework>net8.0</TargetFramework>", self.csproj)
        self.assertIn('project/assembly_name="PromptToPlayDirect"', self.project)

    def test_headless_automation_writes_machine_readable_structural_report(self):
        self.assertIn('OS.GetEnvironment("PTP_AUTOMATION")', self.harness)
        self.assertIn('OS.GetEnvironment("PTP_VERIFY")', self.harness)
        self.assertIn("direct_structural_report.json", self.writer)
        self.assertIn("prompt-to-play/direct-structural-report@1", self.writer)
        self.assertLess(
            self.harness.index("WriteStructuralReport"),
            self.harness.index("await CaptureAsync"),
        )
        for check_id in (
            "harness_ready",
            "run_identifier_safe",
            "generated_entry_exists",
            "generated_entry_instantiates",
            "gameplay_root_declared",
            "renderable_content",
            "capture_camera_available",
            "player_declared",
            "objective_declared",
            "hud_declared",
            "interaction_responds_to_input",
        ):
            with self.subTest(check_id=check_id):
                self.assertIn(f'Id = "{check_id}"', self.harness)

    def test_automation_probes_declared_actions_before_writing_structure(self):
        self.assertLess(
            self.harness.index("DirectInteractionProbe.RunAsync"),
            self.harness.index("EvaluateStructure()"),
        )
        self.assertIn('OS.GetEnvironment("PTP_AUTOMATION")', self.harness)
        self.assertIn('OS.GetEnvironment("PTP_CAPTURE")', self.harness)
        self.assertIn('GetNodesInGroup(ProbeGroup)', self.probe)
        self.assertIn('target.GetMeta(ActionsMetadata)', self.probe)
        self.assertIn('InputMap.HasAction(action)', self.probe)
        self.assertIn('Input.ActionPress(action, 1.0f)', self.probe)
        self.assertIn('Input.ActionRelease(action)', self.probe)
        self.assertIn('SceneTree.SignalName.PhysicsFrame', self.probe)
        self.assertIn('before_sha256=', self.probe)
        self.assertIn('after_sha256=', self.probe)
        self.assertIn('interaction_probe = interactionProbe', self.writer)
        for genre_action in ('"move_forward"', '"fire"', '"collect"', '"race"'):
            with self.subTest(genre_action=genre_action):
                self.assertNotIn(genre_action, self.probe)

    def test_capture_writes_one_or_two_hashed_pngs_and_manifest(self):
        self.assertIn('OS.GetEnvironment("PTP_CAPTURE")', self.harness)
        self.assertIn(".Take(2)", self.harness)
        self.assertIn("image.SavePng", self.harness)
        self.assertIn("HasVisualVariation(image)", self.harness)
        self.assertIn("SHA256.HashData", self.writer)
        self.assertIn("capture_manifest.json", self.writer)
        self.assertIn("prompt-to-play/direct-capture-manifest@1", self.writer)

    def test_artifact_paths_reject_untrusted_run_and_camera_identifiers(self):
        self.assertIn("^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", self.writer)
        self.assertIn('OS.GetEnvironment("PTP_PROJECT_SHA256")', self.writer)
        self.assertIn("project_sha256 = NullIfEmpty(ProjectSha256)", self.writer)
        self.assertIn("^[a-f0-9]{64}$", self.writer)
        self.assertIn("revision is >= 0 and <= 6", self.writer)
        self.assertIn('Regex.Replace(cameraId, "[^A-Za-z0-9_.-]", "-")', self.writer)
        self.assertIn("rev_{Revision}", self.writer)

    def test_default_generated_game_satisfies_direct_contract(self):
        self.assertIn('AddToGroup("ptp_gameplay")', self.generated_code)
        self.assertEqual(
            self.generated_code.count('AddToGroup("ptp_capture_camera")'),
            2,
        )
        self.assertIn('AddToGroup("ptp_player")', self.generated_code)
        self.assertIn('AddToGroup("ptp_interaction_probe")', self.generated_code)
        self.assertIn('AddToGroup("ptp_objective")', self.generated_code)
        self.assertIn('AddToGroup("ptp_hud")', self.generated_code)
        self.assertIn('SetMeta("ptp_probe_actions", "move_forward")', self.generated_code)
        self.assertIn('Input.GetAxis("move_left", "move_right")', self.generated_code)
        self.assertIn('AddToGroup("ptp_goal")', self.generated_code)
        self.assertIn("MeshInstance3D", self.generated_code)

    def test_template_contains_no_generated_build_or_run_artifacts(self):
        forbidden_parts = {".godot", "bin", "obj", "artifacts"}
        leaked = [
            path.relative_to(TEMPLATE).as_posix()
            for path in TEMPLATE.rglob("*")
            if forbidden_parts.intersection(path.relative_to(TEMPLATE).parts)
        ]
        self.assertEqual(leaked, [])


if __name__ == "__main__":
    unittest.main()
