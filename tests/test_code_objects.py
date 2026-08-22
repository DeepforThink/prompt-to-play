import json
import tempfile
import unittest
from pathlib import Path

from prompt_to_play import agents, code_objects


ROOT = Path(__file__).resolve().parents[1]


def read_world():
    return json.loads(
        (ROOT / "prompt_to_play" / "examples" / "forest_world.json").read_text(
            encoding="utf-8"
        )
    )


class Provider:
    def __init__(self, response):
        self.response = response

    def generate_json(self, *_args, **_kwargs):
        return self.response


def expected_ids(world):
    return [
        *[item["id"] for item in world["buildings"]],
        *[item["id"] for item in world["interactions"]["interactables"]],
        *[item["id"] for item in world["props"]],
    ][:16]


class CodeObjectAgentTests(unittest.TestCase):
    def test_llm_csharp_is_wrapped_and_written_for_assigned_entities(self):
        world = read_world()
        ids = expected_ids(world)
        body = "\n".join(
            f'case "{entity_id}":\n    PrimitiveFactory.AddSphereVisual(root, '
            f'"LLM", Vector3.Zero, scale.X, PrimitiveFactory.Material(accent));\n'
            "    return true;"
            for entity_id in ids
        )
        provider = Provider({"entity_ids": ids, "csharp_switch_body": body})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = agents.MultiAgentRuntime(
                root, provider_factory=lambda *_args: provider
            )
            result = code_objects.generate_code_objects(
                runtime, world["brief"]["text"], world, root
            )
            source = result.source_path.read_text(encoding="utf-8")

        self.assertEqual(result.status, "generated")
        self.assertEqual(list(result.entity_ids), ids)
        self.assertIn('case "ancient_oak":', source)
        self.assertIn("PrimitiveFactory.AddSphereVisual", source)
        self.assertEqual(runtime.traces()[0].task_id, "code_objects_01")

    def test_forbidden_code_is_replaced_by_default(self):
        world = read_world()
        ids = expected_ids(world)
        body = "\n".join(
            f'case "{entity_id}": System.IO.File.ReadAllText("secret"); return true;'
            for entity_id in ids
        )
        provider = Provider({"entity_ids": ids, "csharp_switch_body": body})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = agents.MultiAgentRuntime(
                root, provider_factory=lambda *_args: provider
            )
            result = code_objects.generate_code_objects(
                runtime, world["brief"]["text"], world, root
            )
            source = result.source_path.read_text(encoding="utf-8")

        self.assertEqual(result.status, "fallback")
        self.assertEqual(result.entity_ids, ())
        self.assertNotIn("ReadAllText", source)
        self.assertEqual(result.record["error_type"], "ValueError")

    def test_entity_cap_is_host_bounded(self):
        self.assertEqual(code_objects.max_entities({}), 16)
        for raw in ("0", "17", "many"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                code_objects.max_entities(
                    {code_objects.CODE_OBJECT_MAX_ENTITIES_ENV: raw}
                )


if __name__ == "__main__":
    unittest.main()
