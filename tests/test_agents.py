from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from prompt_to_play import agents, provider


class FakeProvider:
    def __init__(self):
        self.calls = 0

    def generate_json(self, messages, *, json_schema, schema_name, image_paths=()):
        self.calls += 1
        return {"role": schema_name, "value": len(messages) + len(image_paths)}

    def usage_summary(self):
        return provider.ProviderUsage(
            backend="fake-api",
            model="fake-model",
            calls=self.calls,
            input_tokens=self.calls * 10,
            cached_input_tokens=self.calls * 2,
            output_tokens=self.calls * 3,
            exact=True,
        )


class MultiAgentRuntimeTests(unittest.TestCase):
    def test_roles_are_isolated_traced_and_never_persist_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "capture.png"
            image.write_bytes(b"pixels")
            created = {}

            def factory(role, environment, repo):
                self.assertEqual(repo, root.resolve())
                self.assertEqual(environment["PROMPT_TO_PLAY_API_KEY"], "secret")
                value = FakeProvider()
                created[role] = value
                return value

            runtime = agents.MultiAgentRuntime(
                root,
                environment={"PROMPT_TO_PLAY_API_KEY": "secret"},
                provider_factory=factory,
            )
            planner = runtime.agent(agents.AgentRole.WORLD_PLANNER)
            evaluator = runtime.agent(agents.AgentRole.VISUAL_EVALUATOR)
            self.assertIs(planner, runtime.agent(agents.AgentRole.WORLD_PLANNER))
            self.assertIsNot(planner, evaluator)

            planner.generate_json(
                [{"role": "user", "content": "plan"}],
                json_schema={"type": "object"},
                schema_name="world",
            )
            evaluator.generate_json(
                [{"role": "user", "content": "evaluate"}],
                json_schema={"type": "object"},
                schema_name="feedback",
                image_paths=[image],
            )

            trace_path = runtime.write_trace(root / "trace.json", run_id="run123")
            document = json.loads(trace_path.read_text(encoding="utf-8"))
            self.assertEqual(document["schema"], agents.TRACE_SCHEMA)
            self.assertEqual([call["role"] for call in document["calls"]], [
                "world_planner",
                "visual_evaluator",
            ])
            self.assertEqual(document["usage"]["calls"], 2)
            self.assertEqual(document["usage"]["total_tokens"], 26)
            self.assertNotIn("secret", trace_path.read_text(encoding="utf-8"))

    def test_failed_calls_are_traced_without_error_messages_or_prompt_text(self):
        class FailingProvider(FakeProvider):
            def generate_json(self, *args, **kwargs):
                self.calls += 1
                raise RuntimeError("secret detail")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = agents.MultiAgentRuntime(
                root,
                provider_factory=lambda *_args: FailingProvider(),
            )
            with self.assertRaisesRegex(RuntimeError, "secret detail"):
                runtime.agent(agents.AgentRole.REPAIR).generate_json(
                    [{"role": "user", "content": "sensitive prompt"}],
                    json_schema={"type": "object"},
                    schema_name="repair",
                )
            trace = runtime.traces()[0]
            self.assertEqual(trace.status, "error")
            self.assertEqual(trace.error_type, "RuntimeError")
            serialized = json.dumps(trace.__dict__)
            self.assertNotIn("secret detail", serialized)
            self.assertNotIn("sensitive prompt", serialized)


if __name__ == "__main__":
    unittest.main()
