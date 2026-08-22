import base64
import copy
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib import error as urlerror
from unittest import mock

from prompt_to_play import contracts, planner, provider


ROOT = Path(__file__).resolve().parents[1]
FOREST = json.loads(
    (ROOT / "prompt_to_play" / "examples" / "forest_world.json").read_text(
        encoding="utf-8"
    )
)


class FakeProvider:
    def __init__(self, document):
        self.document = document
        self.calls = []

    def generate_json(
        self,
        messages,
        *,
        json_schema,
        schema_name,
        image_paths=(),
    ):
        self.calls.append(
            {
                "messages": copy.deepcopy(messages),
                "json_schema": copy.deepcopy(json_schema),
                "schema_name": schema_name,
                "image_paths": list(image_paths),
            }
        )
        return copy.deepcopy(self.document)


class SequenceProvider(FakeProvider):
    def __init__(self, documents):
        super().__init__(None)
        self.documents = list(documents)

    def generate_json(self, *args, **kwargs):
        index = len(self.calls)
        if index >= len(self.documents):
            raise AssertionError("planner called provider too many times")
        self.document = self.documents[index]
        return super().generate_json(*args, **kwargs)


class ProviderConfigTests(unittest.TestCase):
    def test_http_config_requires_a_key_and_is_environment_replaceable(self):
        with self.assertRaisesRegex(
            provider.ProviderConfigurationError, "missing API key"
        ):
            provider.ProviderConfig.from_env({})
        config = provider.ProviderConfig.from_env(
            {
                "PROMPT_TO_PLAY_API_KEY": "test-key",
                "PROMPT_TO_PLAY_MODEL": "local-model",
                "PROMPT_TO_PLAY_BASE_URL": "http://127.0.0.1:9000/v1/",
                "PROMPT_TO_PLAY_API_STYLE": "responses",
                "PROMPT_TO_PLAY_STRUCTURED_OUTPUT_MODE": "prompt",
                "PROMPT_TO_PLAY_STREAM_RESPONSES": "true",
                "PROMPT_TO_PLAY_TIMEOUT_SECONDS": "9.5",
                "PROMPT_TO_PLAY_MAX_OUTPUT_TOKENS": "2048",
                "PROMPT_TO_PLAY_USER_AGENT": "codex_cli_rs/0.77.0 test",
            }
        )
        self.assertEqual(config.model, "local-model")
        self.assertEqual(config.base_url, "http://127.0.0.1:9000/v1")
        self.assertEqual(config.api_style, "responses")
        self.assertEqual(config.structured_output_mode, "prompt")
        self.assertTrue(config.stream_responses)
        self.assertEqual(config.timeout_seconds, 9.5)
        self.assertEqual(config.max_output_tokens, 2048)
        self.assertEqual(config.user_agent, "codex_cli_rs/0.77.0 test")

    def test_http_config_rejects_header_control_characters(self):
        with self.assertRaisesRegex(
            provider.ProviderConfigurationError, "control characters"
        ):
            provider.ProviderConfig.from_env(
                {
                    "PROMPT_TO_PLAY_API_KEY": "test-key",
                    "PROMPT_TO_PLAY_USER_AGENT": "valid-prefix\r\ninjected: value",
                }
            )
        with self.assertRaisesRegex(
            provider.ProviderConfigurationError, "API key.*control characters"
        ):
            provider.ProviderConfig.from_env(
                {"PROMPT_TO_PLAY_API_KEY": "test-key\r\ninjected: value"}
            )

    def test_http_config_rejects_unsafe_remote_urls(self):
        unsafe_urls = (
            "http://example.test/v1",
            "https://user:password@example.test/v1",
            "https://example.test/v1;parameter",
            "https://example.test/v1?route=other",
            "https://example.test/v1#fragment",
            "https://:443/v1",
            "https://example.test:invalid/v1",
        )
        for base_url in unsafe_urls:
            with self.subTest(base_url=base_url):
                with self.assertRaises(provider.ProviderConfigurationError):
                    provider.ProviderConfig.from_env(
                        {
                            "PROMPT_TO_PLAY_API_KEY": "test-key",
                            "PROMPT_TO_PLAY_BASE_URL": base_url,
                        }
                    )

    def test_provider_config_repr_does_not_expose_api_key(self):
        config = provider.ProviderConfig(api_key="sk-sensitive", model="m")
        self.assertNotIn("sk-sensitive", repr(config))

    def test_auto_prefers_api_key_then_falls_back_to_codex(self):
        selected = provider.create_provider_from_env(
            {"OPENAI_API_KEY": "key", "PROMPT_TO_PLAY_MODEL": "m"}
        )
        self.assertIsInstance(selected, provider.OpenAICompatibleProvider)
        with mock.patch.object(provider.shutil, "which", return_value="codex"):
            selected = provider.create_provider_from_env(
                {"PROMPT_TO_PLAY_REPO": str(ROOT)}
            )
        self.assertIsInstance(selected, provider.CodexCliProvider)

    def test_http_mode_selects_compatible_provider(self):
        selected = provider.create_provider_from_env(
            {
                "PROMPT_TO_PLAY_PROVIDER": "http",
                "PROMPT_TO_PLAY_API_KEY": "key",
            }
        )
        self.assertIsInstance(selected, provider.OpenAICompatibleProvider)

    def test_auto_reports_when_neither_auth_path_exists(self):
        with mock.patch.object(provider.shutil, "which", return_value=None):
            with self.assertRaisesRegex(
                provider.ProviderConfigurationError,
                "no API key and no codex CLI",
            ):
                provider.create_provider_from_env(
                    {
                        "PROMPT_TO_PLAY_REPO": str(ROOT),
                        "PROMPT_TO_PLAY_CODEX_COMMAND": "definitely-missing-codex",
                    }
                )

    def test_workspace_codex_cmd_precedes_a_windowsapps_alias(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            repo = workspace / "prompt-to-play"
            repo.mkdir()
            local_codex = (
                workspace
                / ".tools"
                / "codex-cli"
                / "node_modules"
                / ".bin"
                / "codex.cmd"
            )
            local_codex.parent.mkdir(parents=True)
            local_codex.write_text("@echo off\n", encoding="ascii")

            with mock.patch.object(
                provider.shutil,
                "which",
                return_value=(
                    r"C:\Program Files\WindowsApps\OpenAI.Codex_1.0\codex.exe"
                ),
            ):
                selected = provider.create_provider_from_env(
                    {"PROMPT_TO_PLAY_REPO": str(repo)}
                )

            self.assertIsInstance(selected, provider.CodexCliProvider)
            self.assertEqual(selected.config.command, str(local_codex.resolve()))


class HttpProviderTests(unittest.TestCase):
    def test_http_error_body_is_never_exposed(self):
        failure = urlerror.HTTPError(
            "https://example.invalid/v1/responses",
            401,
            "unauthorized",
            {},
            io.BytesIO(b"echoed sk-sensitive-value and private prompt"),
        )
        opener = mock.Mock()
        opener.open.side_effect = failure
        with mock.patch.object(provider.request, "build_opener", return_value=opener):
            with self.assertRaises(provider.ProviderRequestError) as raised:
                provider._default_transport(
                    "https://example.invalid/v1/responses",
                    {"Authorization": "Bearer sk-sensitive-value"},
                    b"{}",
                    1,
                )
        self.assertEqual(str(raised.exception), "provider HTTP 401")
        self.assertIsNone(raised.exception.__cause__)

    def test_http_error_exposes_only_local_categories_and_field_hints(self):
        secret = "sk-this-must-never-appear"
        private_prompt = "private prompt must never appear"
        body = {
            "error": {
                "type": "invalid_request_error",
                "code": secret,
                "param": "text.format.schema",
                "message": (
                    "Unsupported json_schema anyOf; " + private_prompt + " " + secret
                ),
            }
        }
        failure = urlerror.HTTPError(
            "https://example.invalid/v1/responses",
            400,
            private_prompt,
            {"X-Debug": secret},
            io.BytesIO(json.dumps(body).encode("utf-8")),
        )
        opener = mock.Mock()
        opener.open.side_effect = failure
        with mock.patch.object(provider.request, "build_opener", return_value=opener):
            with self.assertRaises(provider.ProviderRequestError) as raised:
                provider._default_transport(
                    "https://example.invalid/v1/responses",
                    {"Authorization": f"Bearer {secret}"},
                    private_prompt.encode("utf-8"),
                    1,
                )
        diagnostic = str(raised.exception)
        self.assertIn("provider HTTP 400", diagnostic)
        self.assertIn("invalid request", diagnostic)
        self.assertIn("json_schema", diagnostic)
        self.assertIn("anyof", diagnostic)
        self.assertNotIn(secret, diagnostic)
        self.assertNotIn(private_prompt, diagnostic)
        self.assertIsNone(raised.exception.__cause__)

    def test_redirects_are_disabled_to_keep_authorization_on_one_origin(self):
        handler = provider._NoRedirectHandler()
        outgoing = provider.request.Request(
            "https://www.micuapi.ai/v1/responses",
            headers={"Authorization": "Bearer secret"},
        )
        redirected = handler.redirect_request(
            outgoing,
            None,
            302,
            "Found",
            {},
            "https://unexpected.example/v1/responses",
        )
        self.assertIsNone(redirected)

        failure = urlerror.HTTPError(
            "https://www.micuapi.ai/v1/responses",
            302,
            "Found",
            {"Location": "https://unexpected.example/v1/responses"},
            None,
        )
        opener = mock.Mock()
        opener.open.side_effect = failure
        with mock.patch.object(provider.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(provider.ProviderRequestError, "HTTP 302"):
                provider._default_transport(
                    "https://www.micuapi.ai/v1/responses",
                    {"Authorization": "Bearer secret"},
                    b"{}",
                    1,
                )

    def test_usage_summary_reads_chat_and_responses_token_fields(self):
        responses = [
            {
                "model": "served-model",
                "choices": [{"message": {"content": '{"ok":true}'}}],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "prompt_tokens_details": {"cached_tokens": 40},
                },
            },
            {
                "model": "served-model",
                "choices": [{"message": {"content": '{"ok":true}'}}],
                "usage": {
                    "input_tokens": 50,
                    "output_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 5},
                },
            },
        ]

        def transport(*_args):
            return json.dumps(responses.pop(0)).encode("utf-8")

        client = provider.OpenAICompatibleProvider(
            provider.ProviderConfig(api_key="secret", model="requested-model"),
            transport=transport,
        )
        for _ in range(2):
            client.generate_json(
                [{"role": "user", "content": "json"}],
                json_schema={"type": "object"},
                schema_name="result",
            )
        usage = client.usage_summary()
        self.assertEqual(usage.model, "served-model")
        self.assertEqual(usage.calls, 2)
        self.assertEqual(usage.input_tokens, 170)
        self.assertEqual(usage.cached_input_tokens, 45)
        self.assertEqual(usage.output_tokens, 40)
        self.assertEqual(usage.total_tokens, 210)
        self.assertTrue(usage.exact)

    def test_chat_completions_payload_and_structured_response(self):
        captured = {}

        def transport(url, headers, payload, timeout):
            captured.update(
                url=url,
                headers=headers,
                payload=json.loads(payload.decode("utf-8")),
                timeout=timeout,
            )
            response = {"choices": [{"message": {"content": '{"answer":42}'}}]}
            return json.dumps(response).encode("utf-8")

        client = provider.OpenAICompatibleProvider(
            provider.ProviderConfig(
                api_key="secret",
                model="replaceable-model",
                base_url="https://example.test/v1",
            ),
            transport=transport,
        )
        document = client.generate_json(
            [{"role": "user", "content": "make json"}],
            json_schema={"type": "object"},
            schema_name="answer",
        )
        self.assertEqual(document, {"answer": 42})
        self.assertEqual(captured["url"], "https://example.test/v1/chat/completions")
        self.assertEqual(captured["payload"]["model"], "replaceable-model")
        self.assertTrue(captured["payload"]["response_format"]["json_schema"]["strict"])
        self.assertEqual(captured["headers"]["Authorization"], "Bearer secret")

    def test_responses_api_payload_and_output_extraction(self):
        captured = {}

        def transport(url, headers, payload, timeout):
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = json.loads(payload.decode("utf-8"))
            response = {
                "output": [
                    {"content": [{"type": "output_text", "text": '{"ok":true}'}]}
                ]
            }
            return json.dumps(response).encode("utf-8")

        client = provider.OpenAICompatibleProvider(
            provider.ProviderConfig(
                api_key="secret",
                model="m",
                base_url="https://example.test/v1",
                api_style="responses",
                user_agent="codex_cli_rs/0.77.0 test",
            ),
            transport=transport,
        )
        document = client.generate_json(
            [{"role": "user", "content": "make json"}],
            json_schema={"type": "object"},
            schema_name="result",
        )
        self.assertEqual(document, {"ok": True})
        self.assertEqual(captured["url"], "https://example.test/v1/responses")
        self.assertEqual(captured["headers"]["User-Agent"], "codex_cli_rs/0.77.0 test")
        self.assertEqual(captured["payload"]["text"]["format"]["type"], "json_schema")

    def test_prompt_structured_responses_payload_uses_codex_style_subset(self):
        captured = {}

        def transport(_url, _headers, payload, _timeout):
            captured["payload"] = json.loads(payload.decode("utf-8"))
            return (
                "event: response.output_text.delta\n"
                'data: {"type":"response.output_text.delta","delta":"{\\"ok\\":"}\n\n'
                "event: response.output_text.delta\n"
                'data: {"type":"response.output_text.delta","delta":"true}"}\n\n'
                "event: response.completed\n"
                'data: {"type":"response.completed","response":{"status":"completed",'
                '"model":"gpt-5.6-sol","usage":{"input_tokens":10,'
                '"output_tokens":3,"input_tokens_details":{"cached_tokens":0}}}}\n\n'
                "data: [DONE]\n\n"
            ).encode("utf-8")

        client = provider.OpenAICompatibleProvider(
            provider.ProviderConfig(
                api_key="secret",
                model="gpt-5.6-sol",
                base_url="https://www.micuapi.ai/v1",
                api_style="responses",
                structured_output_mode="prompt",
                stream_responses=True,
                max_output_tokens=12000,
            ),
            transport=transport,
        )
        document = client.generate_json(
            [
                {"role": "system", "content": "Follow the host contract."},
                {"role": "user", "content": "make json"},
            ],
            json_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["ok"],
                "properties": {"ok": {"type": "boolean"}},
            },
            schema_name="result",
        )

        payload = captured["payload"]
        self.assertEqual(document, {"ok": True})
        self.assertEqual(payload["input"], "make json")
        self.assertIn("Follow the host contract.", payload["instructions"])
        self.assertIn('"required":["ok"]', payload["instructions"])
        self.assertEqual(payload["max_output_tokens"], 12000)
        self.assertFalse(payload["store"])
        self.assertTrue(payload["stream"])
        self.assertNotIn("text", payload)
        self.assertNotIn("temperature", payload)
        usage = client.usage_summary()
        self.assertEqual(usage.total_tokens, 13)
        self.assertTrue(usage.exact)

    def test_chat_and_responses_encode_local_images_as_data_urls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "参考 图.png"
            image.write_bytes(b"actual pixels")
            expected_url = "data:image/png;base64," + base64.b64encode(
                b"actual pixels"
            ).decode("ascii")

            for style in ("chat_completions", "responses"):
                with self.subTest(style=style):
                    captured = {}

                    def transport(_url, _headers, payload, _timeout):
                        captured["payload"] = json.loads(payload.decode("utf-8"))
                        if style == "chat_completions":
                            response = {
                                "choices": [{"message": {"content": '{"ok":true}'}}]
                            }
                        else:
                            response = {"output_text": '{"ok":true}'}
                        return json.dumps(response).encode("utf-8")

                    client = provider.OpenAICompatibleProvider(
                        provider.ProviderConfig(
                            api_key="secret",
                            model="m",
                            api_style=style,
                        ),
                        transport=transport,
                    )
                    document = client.generate_json(
                        [
                            {"role": "system", "content": "return json"},
                            {"role": "user", "content": "inspect the image"},
                        ],
                        json_schema={"type": "object"},
                        schema_name="result",
                        image_paths=[image],
                    )

                    self.assertEqual(document, {"ok": True})
                    messages_key = (
                        "messages" if style == "chat_completions" else "input"
                    )
                    messages = captured["payload"][messages_key]
                    self.assertEqual(messages[0]["content"], "return json")
                    content = messages[1]["content"]
                    if style == "chat_completions":
                        self.assertEqual(
                            content,
                            [
                                {"type": "text", "text": "inspect the image"},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": expected_url,
                                        "detail": "auto",
                                    },
                                },
                            ],
                        )
                    else:
                        self.assertEqual(
                            content,
                            [
                                {
                                    "type": "input_text",
                                    "text": "inspect the image",
                                },
                                {
                                    "type": "input_image",
                                    "image_url": expected_url,
                                    "detail": "auto",
                                },
                            ],
                        )

    def test_malformed_structured_output_is_rejected(self):
        response = {"choices": [{"message": {"content": "```json\n{}\n```"}}]}
        client = provider.OpenAICompatibleProvider(
            provider.ProviderConfig(api_key="secret", model="m"),
            transport=lambda *args: json.dumps(response).encode("utf-8"),
        )
        with self.assertRaisesRegex(provider.ProviderResponseError, "not valid JSON"):
            client.generate_json(
                [{"role": "user", "content": "json"}],
                json_schema={"type": "object"},
                schema_name="result",
            )


class CodexCliProviderTests(unittest.TestCase):
    def test_codex_usage_preserves_reported_total_without_claiming_exact_split(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def runner(command, **_kwargs):
                output_path = Path(command[command.index("-o") + 1])
                output_path.write_text('{"ok":true}', encoding="utf-8")
                return subprocess.CompletedProcess(
                    command,
                    0,
                    "",
                    "model: gpt-test\ntokens used: 1,234\n",
                )

            client = provider.CodexCliProvider(
                provider.CodexCliConfig(command="codex", repo=root),
                runner=runner,
            )
            client.generate_json(
                [{"role": "user", "content": "json"}],
                json_schema={"type": "object"},
                schema_name="result",
            )
            usage = client.usage_summary()
            self.assertEqual(usage.model, "gpt-test")
            self.assertEqual(usage.calls, 1)
            self.assertEqual(usage.total_tokens, 1234)
            self.assertFalse(usage.exact)

    def test_codex_command_is_ephemeral_read_only_structured_and_sends_each_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.png"
            second = root / "second.png"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            captured = {}

            def runner(command, **kwargs):
                captured["command"] = command
                captured["kwargs"] = kwargs
                workspace = Path(command[command.index("-C") + 1])
                captured["workspace"] = workspace
                captured["workspace_entries"] = list(workspace.iterdir())
                schema_path = Path(command[command.index("--output-schema") + 1])
                captured["schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
                output_path = Path(command[command.index("-o") + 1])
                output_path.write_text('{"planned":true}', encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            client = provider.CodexCliProvider(
                provider.CodexCliConfig(command="codex", repo=root),
                runner=runner,
            )
            with mock.patch.dict(
                provider.os.environ,
                {
                    "CODEX_HOME": "",
                    "AWS_SECRET_ACCESS_KEY": "must-not-leak",
                    "GITHUB_TOKEN": "must-not-leak",
                },
            ):
                document = client.generate_json(
                    [{"role": "user", "content": "plan"}],
                    json_schema={"type": "object"},
                    schema_name="world",
                    image_paths=[first, second],
                )
            self.assertEqual(document, {"planned": True})
            command = captured["command"]
            self.assertEqual(command[:4], ["codex", "-a", "never", "exec"])
            for flag in (
                "--ignore-user-config",
                "--ephemeral",
                "--skip-git-repo-check",
                "--output-schema",
            ):
                self.assertIn(flag, command)
            self.assertNotIn("--ignore-rules", command)
            self.assertEqual(command[command.index("-s") + 1], "read-only")
            self.assertNotEqual(command[command.index("-C") + 1], str(root))
            self.assertEqual(captured["workspace_entries"], [])
            image_arguments = [
                command[index + 1]
                for index, value in enumerate(command)
                if value == "-i"
            ]
            self.assertEqual(
                image_arguments, [str(first.resolve()), str(second.resolve())]
            )
            self.assertEqual(command[-1], "-")
            self.assertIn("[USER]\nplan", captured["kwargs"]["input"])
            self.assertNotIn("plan", command)
            self.assertEqual(
                captured["kwargs"]["env"]["CODEX_HOME"],
                str(Path.home() / ".codex"),
            )
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", captured["kwargs"]["env"])
            self.assertNotIn("GITHUB_TOKEN", captured["kwargs"]["env"])
            self.assertEqual(captured["schema"], {"type": "object"})
            self.assertFalse(captured["kwargs"]["check"])

    def test_windows_permission_error_falls_back_to_powershell_argv_forwarding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "项目 空间"
            root.mkdir()
            image = root / "参考 图.png"
            image.write_bytes(b"pixels")
            calls = []
            captured = {}

            def runner(command, **kwargs):
                calls.append((list(command), dict(kwargs)))
                if len(calls) == 1:
                    raise PermissionError(13, "Access is denied", "codex.exe")
                file_index = command.index("-File")
                wrapper_path = Path(command[file_index + 1])
                captured["wrapper"] = wrapper_path.read_text(encoding="ascii")
                forwarded = command[file_index + 2 :]
                output_path = Path(forwarded[forwarded.index("-o") + 1])
                output_path.write_text('{"fallback":true}', encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            client = provider.CodexCliProvider(
                provider.CodexCliConfig(command="codex", repo=root),
                runner=runner,
                powershell_finder=lambda name: (
                    r"C:\Tools\pwsh.exe" if name == "pwsh.exe" else None
                ),
                windows=True,
            )
            document = client.generate_json(
                [{"role": "user", "content": "生成一座雨夜城市"}],
                json_schema={"type": "object"},
                schema_name="世界",
                image_paths=[image],
            )

            self.assertEqual(document, {"fallback": True})
            self.assertEqual(len(calls), 2)
            direct_argv, direct_kwargs = calls[0]
            wrapper_argv, wrapper_kwargs = calls[1]
            self.assertEqual(direct_argv[:4], ["codex", "-a", "never", "exec"])
            self.assertIn(str(image.resolve()), direct_argv)
            self.assertEqual(direct_argv[-1], "-")
            self.assertIn("生成一座雨夜城市", direct_kwargs["input"])
            self.assertEqual(wrapper_argv[0], r"C:\Tools\pwsh.exe")
            self.assertEqual(
                wrapper_argv[1:7],
                [
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                ],
            )
            file_index = wrapper_argv.index("-File")
            self.assertEqual(wrapper_argv[file_index + 2 :], direct_argv)
            self.assertNotIn("shell", direct_kwargs)
            self.assertNotIn("shell", wrapper_kwargs)
            self.assertEqual(wrapper_kwargs["input"], direct_kwargs["input"])
            self.assertIn("$executable = $args[0]", captured["wrapper"])
            self.assertIn("& $executable @forwarded", captured["wrapper"])
            self.assertIn("$ErrorActionPreference = 'Stop'", captured["wrapper"])
            self.assertIn("if (-not $?) { exit 1 }", captured["wrapper"])
            self.assertIn(
                "if ($null -eq $LASTEXITCODE) { exit 1 }", captured["wrapper"]
            )
            self.assertNotIn("Invoke-Expression", captured["wrapper"])

    def test_codex_failure_filters_and_bounds_stderr(self):
        def runner(command, **kwargs):
            stderr = (
                "cache response body: "
                + ("x" * 10000)
                + "\nBearer secret-token\nsk-secretvalue"
            )
            return subprocess.CompletedProcess(command, 1, "", stderr)

        client = provider.CodexCliProvider(
            provider.CodexCliConfig(
                command="codex",
                repo=ROOT,
                max_stderr_chars=300,
            ),
            runner=runner,
        )
        with self.assertRaises(provider.ProviderRequestError) as raised:
            client.generate_json(
                [{"role": "user", "content": "plan"}],
                json_schema={"type": "object"},
                schema_name="world",
            )
        message = str(raised.exception)
        self.assertLessEqual(len(message), 380)
        self.assertNotIn("x" * 1000, message)
        self.assertNotIn("secret-token", message)
        self.assertNotIn("secretvalue", message)


class PlannerTests(unittest.TestCase):
    def test_arbitrary_prompt_and_summary_produce_valid_authoritative_world(self):
        fake = FakeProvider(FOREST)
        prompt = "在冰层下建造一座可探索的海洋档案馆"
        references = [
            {
                "name": "refs/front.png",
                "sha256": "a" * 64,
                "summary": "蓝绿色拱顶、玻璃隧道和远处鲸群",
            }
        ]
        world = planner.plan_world(prompt, references, provider=fake)
        contracts.validate_world(world)
        self.assertEqual(world["brief"]["text"], prompt)
        self.assertEqual(
            world["brief"]["references"],
            [{"path": "refs/front.png", "sha256": "a" * 64}],
        )
        self.assertEqual(
            world["seed"],
            contracts.derive_world_seed(prompt, world["brief"]["references"]),
        )
        call = fake.calls[0]
        self.assertIn(prompt, call["messages"][1]["content"])
        self.assertIn("玻璃隧道", call["messages"][1]["content"])
        self.assertEqual(
            call["json_schema"]["properties"]["seed"]["const"], world["seed"]
        )
        self.assertEqual(call["json_schema"]["properties"]["brief"]["type"], "object")
        self.assertNotIn("const", call["json_schema"]["properties"]["brief"])
        self.assertEqual(call["schema_name"], "prompt_to_play_world_spec")

    def test_image_files_are_hashed_recorded_and_sent_to_provider(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "refs" / "angle.png"
            image.parent.mkdir()
            image.write_bytes(b"actual image bytes")
            fake = FakeProvider(FOREST)
            world = planner.plan_world(
                "悬崖上的风暴观测站",
                reference_image_paths=[image],
                project_root=root,
                provider=fake,
            )
            self.assertEqual(
                world["brief"]["references"],
                [
                    {
                        "path": "refs/angle.png",
                        "sha256": hashlib.sha256(b"actual image bytes").hexdigest(),
                    }
                ],
            )
            self.assertEqual(fake.calls[0]["image_paths"], [image.resolve()])

    def test_supplied_reference_digest_must_match_image_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "view.png"
            image.write_bytes(b"pixels")
            fake = FakeProvider(FOREST)
            with self.assertRaisesRegex(
                planner.PlannerError, "content hash does not match"
            ):
                planner.plan_world(
                    "森林",
                    [{"path": "refs/view.png", "sha256": "0" * 64}],
                    reference_image_paths=[image],
                    project_root=root,
                    provider=fake,
                )
            self.assertEqual(fake.calls, [])

    def test_disconnected_first_plan_is_returned_for_model_correction(self):
        disconnected = copy.deepcopy(FOREST)
        disconnected["roads"] = []
        sequence = SequenceProvider([disconnected, FOREST])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "参考.png"
            image.write_bytes(b"reference pixels")
            world = planner.plan_world(
                "设计一片互相连通的森林遗迹",
                reference_image_paths=[image],
                project_root=root,
                provider=sequence,
            )

        self.assertEqual(world["roads"], FOREST["roads"])
        self.assertEqual(len(sequence.calls), 2)
        first, second = sequence.calls
        self.assertEqual(len(first["messages"]), 2)
        self.assertEqual(len(second["messages"]), 4)
        rejected = json.loads(second["messages"][-2]["content"])
        self.assertEqual(rejected["roads"], [])
        self.assertIn("does not reach regions", second["messages"][-1]["content"])
        self.assertIn("stone_ruins", second["messages"][-1]["content"])
        self.assertEqual(first["json_schema"], second["json_schema"])
        self.assertEqual(first["image_paths"], second["image_paths"])
        self.assertEqual(len(second["image_paths"]), 1)

    def test_three_disconnected_plans_fail_after_exactly_two_corrections(self):
        disconnected = copy.deepcopy(FOREST)
        disconnected["roads"] = []
        sequence = SequenceProvider([disconnected, disconnected, disconnected])
        with self.assertRaisesRegex(
            planner.PlannerError,
            "after 2 correction attempts.*does not reach regions",
        ):
            planner.plan_world("生成连通世界", provider=sequence)
        self.assertEqual(len(sequence.calls), 3)
        self.assertEqual([len(call["messages"]) for call in sequence.calls], [2, 4, 6])

    def test_player_only_camera_is_returned_for_model_correction(self):
        player_only = copy.deepcopy(FOREST)
        player_only["cameras"][0]["kind"] = "player"
        sequence = SequenceProvider([player_only, FOREST])
        world = planner.plan_world("生成可固定评测视角的场景", provider=sequence)
        self.assertEqual(world["cameras"][0]["kind"], "orbit")
        self.assertEqual(len(sequence.calls), 2)
        correction = sequence.calls[1]["messages"][-1]["content"]
        self.assertIn("'fixed' or 'orbit'", correction)
        self.assertIn("player-only cameras are insufficient", correction)

    def test_invalid_provider_world_is_rejected_by_contracts(self):
        invalid = copy.deepcopy(FOREST)
        invalid["roads"][0]["to"] = "missing_region"
        fake = FakeProvider(invalid)
        with self.assertRaisesRegex(planner.PlannerError, "invalid WorldSpec"):
            planner.plan_world("任意新场景", provider=fake)
        self.assertEqual(len(fake.calls), 3)

    def test_provider_configuration_failure_is_clear_and_wrapped(self):
        failure = provider.ProviderConfigurationError("missing API key and codex login")
        with mock.patch.object(
            planner, "create_provider_from_env", side_effect=failure
        ):
            with self.assertRaisesRegex(
                planner.PlannerError,
                "missing API key and codex login",
            ):
                planner.plan_world("任意场景")

    def test_invalid_inputs_never_call_provider(self):
        fake = FakeProvider(FOREST)
        with self.assertRaisesRegex(planner.PlannerError, "must not be empty"):
            planner.plan_world("   ", provider=fake)
        with self.assertRaisesRegex(planner.PlannerError, "repository-relative"):
            planner.plan_world(
                "forest",
                [{"path": "../outside.png", "sha256": "a" * 64}],
                provider=fake,
            )
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
