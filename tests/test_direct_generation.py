import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from prompt_to_play import direct_generation as direct


def package(*files):
    return {"schema": direct.DIRECT_GENERATION_CONTRACT, "files": list(files)}


def file(path, content="content", action=None):
    result = {"path": path, "content": content}
    if action is not None:
        result["action"] = action
    return result


class DirectGenerationContractTests(unittest.TestCase):
    def test_schema_and_parser_default_to_upsert(self):
        schema = direct.DIRECT_GENERATION_SCHEMA
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["files"]["maxItems"], direct.MAX_FILES)
        plan = direct.parse_file_plan(
            package(file("generated/GeneratedGame.tscn", "[gd_scene format=3]"))
        )
        self.assertEqual(plan.files[0].action, "upsert")
        self.assertEqual(plan.files[0].path, "generated/GeneratedGame.tscn")

    def test_parser_accepts_utf8_json_bytes_and_rejects_duplicate_keys(self):
        raw = json.dumps(
            package(file("generated/说明.md", "你好")), ensure_ascii=False
        ).encode("utf-8")
        self.assertEqual(direct.parse_file_plan(raw).files[0].content, "你好")
        duplicate = (
            '{"schema":"prompt-to-play/direct-files@1",'
            '"schema":"prompt-to-play/direct-files@1","files":[]}'
        )
        with self.assertRaisesRegex(direct.DirectGenerationError, "duplicate JSON key"):
            direct.parse_file_plan(duplicate)

    def test_contract_rejects_unknown_or_missing_fields(self):
        with self.assertRaisesRegex(direct.DirectGenerationError, "unknown keys"):
            direct.parse_file_plan(
                {
                    **package(file("generated/a.cs", "class A {}")),
                    "prompt": "must not be persisted",
                }
            )
        with self.assertRaisesRegex(direct.DirectGenerationError, "missing keys"):
            direct.parse_file_plan({"files": []})
        with self.assertRaisesRegex(direct.DirectGenerationError, "unknown keys"):
            direct.parse_file_plan(
                package(
                    {
                        **file("generated/a.cs", "class A {}"),
                        "reasoning": "hidden chain",
                    }
                )
            )

    def test_action_content_rules_are_strict(self):
        direct.parse_file_plan(
            package({"path": "generated/old.cs", "action": "delete"})
        )
        with self.assertRaisesRegex(direct.DirectGenerationError, "omitted for delete"):
            direct.parse_file_plan(
                package(file("generated/old.cs", "", action="delete"))
            )
        with self.assertRaisesRegex(direct.DirectGenerationError, "is required"):
            direct.parse_file_plan(
                package({"path": "generated/new.cs", "action": "create"})
            )
        with self.assertRaisesRegex(direct.DirectGenerationError, "expected upsert"):
            direct.parse_file_plan(
                package(file("generated/new.cs", "class A {}", action="patch"))
            )

    def test_only_generated_text_game_files_are_allowed(self):
        allowed = (
            "generated/game.cs",
            "generated/game.gd",
            "generated/GeneratedGame.tscn",
            "generated/materials/ground.tres",
            "generated/shaders/terrain.gdshader",
            "generated/config/rules.json",
            "generated/README.md",
            "generated/subproject.godot",
        )
        for path in allowed:
            with self.subTest(path=path):
                direct.parse_file_plan(package(file(path)))

        forbidden = (
            "../outside.cs",
            "generated/../../outside.cs",
            "/tmp/outside.cs",
            "C:/outside.cs",
            "generated\\outside.cs",
            "generated//outside.cs",
            "generated/./outside.cs",
            "generated/.hidden.cs",
            "generated/CON.cs",
            "generated/file.exe",
            "scripts/host.cs",
            "artifacts/report.json",
            "project.godot",
        )
        for path in forbidden:
            with self.subTest(path=path):
                with self.assertRaises(direct.DirectGenerationError):
                    direct.parse_file_plan(package(file(path)))

    def test_paths_are_case_insensitively_unique(self):
        with self.assertRaisesRegex(
            direct.DirectGenerationError, "duplicate file path"
        ):
            direct.parse_file_plan(
                package(
                    file("generated/Game.cs", "class Game {}"),
                    file("generated/game.cs", "class Other {}"),
                )
            )

    def test_content_limits_and_nul_are_enforced(self):
        with self.assertRaisesRegex(direct.DirectGenerationError, "NUL"):
            direct.parse_file_plan(package(file("generated/a.md", "a\x00b")))
        with self.assertRaisesRegex(direct.DirectGenerationError, "exceeds"):
            direct.parse_file_plan(
                package(file("generated/large.md", "x" * (direct.MAX_FILE_BYTES + 1)))
            )
        files = [
            file(f"generated/part-{index}.md", "x" * direct.MAX_FILE_BYTES)
            for index in range(
                direct.MAX_TOTAL_CONTENT_BYTES // direct.MAX_FILE_BYTES + 1
            )
        ]
        with self.assertRaisesRegex(
            direct.DirectGenerationError, "total content exceeds"
        ):
            direct.parse_file_plan(package(*files))

    def test_obvious_dangerous_csharp_capabilities_are_rejected(self):
        dangerous = (
            'using System.Diagnostics; class A { void X() { Process.Start("cmd"); } }',
            'using System.Runtime.InteropServices; class A { [DllImport("x")] static extern void X(); }',
            "using System.Net.Http; class A { HttpClient c = new(); }",
            'using System.IO; class A { void X() { File.WriteAllText("x", "y"); } }',
            "using Microsoft.Win32; class A {}",
            'using System.Reflection; class A { void X() { Assembly.Load("x"); } }',
            "unsafe class A { int* p; }",
            'using Godot; class A { void X() { OS.Execute("x", []); } }',
            'using Godot; class A { void X() { FileAccess.Open("user://x", FileAccess.ModeFlags.Write); } }',
            'using Godot; class A { void X() { ResourceSaver.Save(new Resource(), "res://project.godot"); } }',
            'using Godot; class A { void X(Image image) { image.SavePng("user://x.png"); } }',
            "using Godot; class A { void X() { ProjectSettings.Save(); } }",
            'using Godot; class A { void X() { OS.SetEnvironment("X", "Y"); } }',
            (
                "using System; class A { void X() { "
                'var t = Type.GetType("System." + "IO.File"); '
                'var m = t!.GetMethod("ReadAllText"); '
                'm!.Invoke(null, new object[] { "secret.txt" }); } }'
            ),
            (
                "using Godot; using PromptToPlay.Direct; class A { void X() { "
                'DirectArtifactWriter.WriteStatus("pass", "forged"); } }'
            ),
        )
        for index, content in enumerate(dangerous):
            with self.subTest(index=index):
                with self.assertRaisesRegex(direct.DirectGenerationError, "forbidden"):
                    direct.parse_file_plan(
                        package(file(f"generated/Danger{index}.cs", content))
                    )

    def test_normal_godot_csharp_is_allowed(self):
        content = """using Godot;
public partial class Vehicle : CharacterBody3D
{
    [Export] public float Speed = 10f;
    public override void _PhysicsProcess(double delta)
    {
        Velocity = Vector3.Forward * Speed;
        MoveAndSlide();
    }
}
"""
        direct.parse_file_plan(package(file("generated/Vehicle.cs", content)))
        direct.parse_file_plan(
            package(
                file(
                    "generated/Lighting.cs",
                    "using Godot; public partial class Lighting : Node { "
                    "Godot.Environment.BGMode mode = Godot.Environment.BGMode.Color; }",
                )
            )
        )

    def test_dangerous_gdscript_is_rejected(self):
        for index, content in enumerate(
            (
                'extends Node\nfunc x(): OS.execute("cmd", [])',
                "extends Node\nvar request = HTTPRequest.new()",
                'extends Node\nvar f = FileAccess.open("user://x", FileAccess.WRITE)',
                'extends Node\nfunc x(): return OS.get_environment("SECRET")',
                'extends Node\nfunc x(r): ResourceSaver.save(r, "res://project.godot")',
                'extends Node\nfunc x(image): image.save_png("user://x.png")',
                "extends Node\nfunc x(): ProjectSettings.save()",
            )
        ):
            with self.subTest(index=index):
                with self.assertRaisesRegex(direct.DirectGenerationError, "forbidden"):
                    direct.parse_file_plan(
                        package(file(f"generated/danger_{index}.gd", content))
                    )

    def test_godot_resources_cannot_reference_external_or_traversal_paths(self):
        for value in (
            'path="file:///tmp/evil.cs"',
            'path="user://evil.tres"',
            'path="res://../host/Main.tscn"',
            'path="res://harness/Main.tscn"',
            'path="res://artifacts/runs/forged.json"',
            'path="C:/outside/file.tres"',
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(direct.DirectGenerationError, "must"):
                    direct.parse_file_plan(
                        package(file("generated/GeneratedGame.tscn", value))
                    )

        direct.parse_file_plan(
            package(
                file(
                    "generated/GeneratedGame.tscn",
                    'path="res://generated/Game.cs"\n'
                    'path="res://references/00_reference.png"\n',
                )
            )
        )


class DirectGenerationApplyTests(unittest.TestCase):
    def test_apply_upserts_files_and_writes_sanitized_hashed_manifest(self):
        secret = "sk-this-must-not-appear"
        prompt = "a private prompt that must not appear"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = package(
                file("generated/GeneratedGame.tscn", "[gd_scene format=3]\n"),
                file("generated/notes.md", f"{secret}\n{prompt}\n"),
            )
            manifest = direct.apply_file_plan(root, plan)

            self.assertEqual(
                (root / "generated/GeneratedGame.tscn").read_text(encoding="utf-8"),
                "[gd_scene format=3]\n",
            )
            manifest_path = root / "artifacts/direct-generation-manifest.json"
            persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted, manifest)
            serialized = json.dumps(manifest, sort_keys=True)
            self.assertNotIn(secret, serialized)
            self.assertNotIn(prompt, serialized)
            self.assertNotIn('"content":', serialized)
            base = dict(manifest)
            reported_hash = base.pop("manifest_sha256")
            canonical = json.dumps(
                base,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            self.assertEqual(reported_hash, hashlib.sha256(canonical).hexdigest())
            self.assertFalse(any(root.rglob("*.tmp")))

    def test_create_replace_and_delete_have_checked_preconditions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generated = root / "generated"
            generated.mkdir()
            target = generated / "game.md"
            target.write_text("old", encoding="utf-8")

            with self.assertRaisesRegex(direct.DirectGenerationError, "already exists"):
                direct.apply_file_plan(
                    root,
                    package(file("generated/game.md", "new", action="create")),
                )
            self.assertEqual(target.read_text(encoding="utf-8"), "old")

            manifest = direct.apply_file_plan(
                root,
                package(file("generated/game.md", "new", action="replace")),
            )
            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            self.assertEqual(manifest["files"][0]["applied_action"], "replace")

            direct.apply_file_plan(
                root,
                package({"path": "generated/game.md", "action": "delete"}),
            )
            self.assertFalse(target.exists())
            with self.assertRaisesRegex(direct.DirectGenerationError, "does not exist"):
                direct.apply_file_plan(
                    root,
                    package({"path": "generated/game.md", "action": "delete"}),
                )

    def test_all_state_preconditions_are_checked_before_first_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(direct.DirectGenerationError, "does not exist"):
                direct.apply_file_plan(
                    root,
                    package(
                        file("generated/first.md", "would be written"),
                        file("generated/missing.md", "x", action="replace"),
                    ),
                )
            self.assertFalse((root / "generated/first.md").exists())
            self.assertFalse((root / "artifacts").exists())

    def test_parent_file_is_rejected_before_first_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "generated").write_text("not a directory", encoding="utf-8")
            with self.assertRaisesRegex(direct.DirectGenerationError, "real directory"):
                direct.apply_file_plan(
                    root,
                    package(file("generated/new.md", "x")),
                )

    def test_reserved_file_cannot_be_modified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(direct.DirectGenerationError, "reserved"):
                direct.apply_file_plan(
                    root,
                    package(file("generated/Harness.cs", "class Attack {}")),
                    reserved_paths=("generated/Harness.cs",),
                )
            self.assertFalse((root / "generated/Harness.cs").exists())

    def test_manifest_must_be_host_owned_artifacts_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = package(file("generated/a.md", "x"))
            for invalid in (
                "generated/manifest.json",
                "manifest.json",
                "artifacts/manifest.md",
                "../artifacts/manifest.json",
            ):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(direct.DirectGenerationError):
                        direct.apply_file_plan(root, plan, manifest_path=invalid)
                    self.assertFalse((root / "generated/a.md").exists())

    def test_symlink_escape_is_rejected(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            tempfile.TemporaryDirectory() as outside,
        ):
            root = Path(temporary)
            link = root / "generated"
            try:
                os.symlink(outside, link, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")
            with self.assertRaisesRegex(
                direct.DirectGenerationError, "symbolic link or junction"
            ):
                direct.apply_file_plan(
                    root,
                    package(file("generated/escape.md", "blocked")),
                )
            self.assertFalse((Path(outside) / "escape.md").exists())

    def test_file_plan_instance_is_revalidated(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan = direct.FilePlan(
                schema=direct.DIRECT_GENERATION_CONTRACT,
                files=(direct.FileOperation("../escape.cs", "upsert", "class A {}"),),
            )
            with self.assertRaises(direct.DirectGenerationError):
                direct.apply_file_plan(temporary, plan)


if __name__ == "__main__":
    unittest.main()
