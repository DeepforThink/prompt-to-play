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
        *[item["id"] for item in world["regions"]],
        *[item["id"] for item in world["roads"]],
        *[item["id"] for item in world["buildings"]],
        *[item["id"] for item in world["interactions"]["interactables"]],
        *[item["id"] for item in world["props"]],
    ][: len(world["regions"]) + len(world["roads"]) + 16]


def segment_ids(world, segment):
    if segment == "regions":
        return [item["id"] for item in world["regions"]]
    if segment == "roads":
        return [item["id"] for item in world["roads"]]
    return expected_ids(world)[len(world["regions"]) + len(world["roads"]):]


class CodeObjectAgentTests(unittest.TestCase):
    def test_llm_csharp_is_wrapped_and_written_for_assigned_entities(self):
        world = read_world()
        class SegmentProvider:
            def generate_json(self, messages, **_kwargs):
                payload = json.loads(messages[-1]["content"])
                ids = [item["id"] for item in payload["entities"]]
                body = "\n".join(
                    f'case "{entity_id}":\n    PrimitiveFactory.AddSphereVisual(root, '
                    f'"LLM", Vector3.Zero, scale.X, PrimitiveFactory.Material(accent));\n'
                    "    return true;"
                    for entity_id in ids
                )
                return {"entity_ids": ids, "csharp_switch_body": body}

        provider = SegmentProvider()
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
        self.assertEqual(
            list(result.entity_ids),
            [
                *segment_ids(world, "regions"),
                *segment_ids(world, "roads"),
                *segment_ids(world, "objects"),
            ],
        )
        self.assertIn(f'case "{world["regions"][0]["id"]}":', source)
        self.assertIn('case "ancient_oak":', source)
        self.assertIn("PrimitiveFactory.AddSphereVisual", source)
        self.assertIn("string entityKind", source)
        self.assertIn("Vector3[] path", source)
        self.assertEqual(
            [trace.task_id for trace in runtime.traces()],
            ["code_regions_01", "code_roads_01", "code_objects_01"],
        )

    def test_forbidden_code_is_replaced_by_default(self):
        world = read_world()
        class ForbiddenProvider:
            def generate_json(self, messages, **_kwargs):
                payload = json.loads(messages[-1]["content"])
                ids = [item["id"] for item in payload["entities"]]
                body = "\n".join(
                    f'case "{entity_id}": System.IO.File.ReadAllText("secret"); return true;'
                    for entity_id in ids
                )
                return {"entity_ids": ids, "csharp_switch_body": body}

        provider = ForbiddenProvider()
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
        self.assertIn("ValueError", result.record["error_type"])
        self.assertEqual(
            [segment["status"] for segment in result.record["segments"]],
            ["fallback", "fallback", "fallback"],
        )

    def test_failed_object_segment_preserves_generated_world_segments(self):
        world = read_world()

        class PartialProvider:
            def generate_json(self, messages, **_kwargs):
                payload = json.loads(messages[-1]["content"])
                segment = payload["segment"]
                if segment == "objects":
                    raise RuntimeError("objects unavailable")
                ids = [item["id"] for item in payload["entities"]]
                return {
                    "entity_ids": ids,
                    "csharp_switch_body": "\n".join(
                        f'case "{entity_id}": return true;' for entity_id in ids
                    ),
                }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = agents.MultiAgentRuntime(
                root, provider_factory=lambda *_args: PartialProvider()
            )
            result = code_objects.generate_code_objects(
                runtime, world["brief"]["text"], world, root
            )
            source = result.source_path.read_text(encoding="utf-8")

        self.assertEqual(result.status, "partial")
        self.assertIn(f'case "{world["regions"][0]["id"]}":', source)
        self.assertIn(f'case "{world["roads"][0]["id"]}":', source)
        self.assertNotIn("ancient_oak", source)
        self.assertEqual(
            [segment["status"] for segment in result.record["segments"]],
            ["generated", "generated", "fallback"],
        )

    def test_entity_cap_is_host_bounded(self):
        self.assertEqual(code_objects.max_entities({}), 16)
        for raw in ("0", "17", "many"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                code_objects.max_entities(
                    {code_objects.CODE_OBJECT_MAX_ENTITIES_ENV: raw}
                )


if __name__ == "__main__":
    unittest.main()
