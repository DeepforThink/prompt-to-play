from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from prompt_to_play.launcher import (
    AsyncPipelineController,
    EventKind,
    LaunchRequest,
    LauncherApp,
    PipelineCommands,
    PipelineConfigurationError,
    PipelineRunner,
    Stage,
)


def no_op(_state, _log):
    return None


class FakeRoot:
    def __init__(self):
        self.protocols = {}
        self.destroyed = False

    def title(self, _value):
        return None

    def geometry(self, _value):
        return None

    def minsize(self, _width, _height):
        return None

    def protocol(self, name, callback):
        self.protocols[name] = callback

    def after(self, _delay, _callback):
        return None

    def destroy(self):
        self.destroyed = True


class FakeMessageBox:
    def __init__(self):
        self.warnings = []

    def showwarning(self, title, message, *, parent):
        self.warnings.append((title, message, parent))


class FakeController:
    def __init__(self, *, running):
        self.running = running


class LaunchRequestTests(unittest.TestCase):
    def test_requires_a_non_empty_prompt(self):
        with self.assertRaisesRegex(ValueError, "游戏世界描述"):
            LaunchRequest.from_values(" \n ")

    def test_normalizes_and_deduplicates_reference_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "参考图.png"
            image.write_bytes(b"fixture")
            request = LaunchRequest.from_values("  雾林遗迹  ", [image, image])

            self.assertEqual(request.prompt, "雾林遗迹")
            self.assertEqual(request.reference_images, (image.resolve(),))

    def test_rejects_a_missing_reference_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.png"
            with self.assertRaisesRegex(ValueError, "参考图不存在"):
                LaunchRequest.from_values("world", [missing])


class PipelineRunnerTests(unittest.TestCase):
    def test_executes_all_stages_in_order_and_passes_state(self):
        calls: list[str] = []

        def command(name: str):
            def run(state, log):
                calls.append(name)
                previous = calls[-2] if len(calls) > 1 else "request"
                state.set_result(name, previous)
                log(f"{name} log")

            return run

        commands = PipelineCommands(
            plan=command("plan"),
            validate=command("validate"),
            publish=command("publish"),
            build=command("build"),
            launch=command("launch"),
        )
        events = []
        result = PipelineRunner(commands).run(
            LaunchRequest.from_values("dynamic world"), events.append
        )

        self.assertTrue(result.success)
        self.assertEqual(calls, [stage.value for stage in Stage])
        self.assertEqual(result.state.require_result("validate"), "plan")
        self.assertEqual(
            [event.stage for event in events if event.kind == EventKind.STAGE_STARTED],
            list(Stage),
        )
        self.assertEqual(
            [event.message for event in events if event.kind == EventKind.LOG],
            [f"{stage.value} log" for stage in Stage],
        )
        self.assertEqual(events[-1].kind, EventKind.FINISHED)

    def test_stops_at_the_failing_stage_and_reports_the_error(self):
        calls: list[str] = []

        def plan(_state, _log):
            calls.append("plan")

        def validate(_state, _log):
            calls.append("validate")
            raise ValueError("schema mismatch")

        def must_not_run(_state, _log):
            calls.append("unexpected")

        runner = PipelineRunner(
            PipelineCommands(plan, validate, must_not_run, must_not_run, must_not_run)
        )
        events = []
        result = runner.run(LaunchRequest.from_values("world"), events.append)

        self.assertFalse(result.success)
        self.assertEqual(result.failed_stage, Stage.VALIDATE)
        self.assertEqual(calls, ["plan", "validate"])
        errors = [event for event in events if event.kind == EventKind.ERROR]
        self.assertEqual(len(errors), 1)
        self.assertIn("schema mismatch", errors[0].message)
        self.assertEqual(events[-1].kind, EventKind.FINISHED)

    def test_unconfigured_pipeline_fails_with_an_integration_hint(self):
        result = PipelineRunner(PipelineCommands.unconfigured()).run(
            LaunchRequest.from_values("world"), lambda _event: None
        )

        self.assertFalse(result.success)
        self.assertEqual(result.failed_stage, Stage.PLAN)
        self.assertIn("run_launcher", result.error or "")
        self.assertTrue(issubclass(PipelineConfigurationError, RuntimeError))


class AsyncPipelineControllerTests(unittest.TestCase):
    def test_runs_off_the_calling_thread_and_rejects_duplicate_start(self):
        main_thread = threading.get_ident()
        worker_threads: list[int] = []
        entered = threading.Event()
        release = threading.Event()

        def plan(_state, log):
            worker_threads.append(threading.get_ident())
            entered.set()
            self.assertTrue(release.wait(timeout=2))
            log("worker continued")

        controller = AsyncPipelineController(
            PipelineRunner(PipelineCommands(plan, no_op, no_op, no_op, no_op))
        )
        request = LaunchRequest.from_values("world")

        self.assertTrue(controller.start(request))
        self.assertTrue(entered.wait(timeout=2))
        self.assertTrue(controller.running)
        self.assertFalse(controller.start(request))
        release.set()
        result = controller.wait(timeout=2)

        self.assertIsNotNone(result)
        self.assertTrue(result.success)
        self.assertFalse(controller.running)
        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], main_thread)
        events = controller.drain_events()
        self.assertTrue(any(event.kind == EventKind.LOG for event in events))
        self.assertEqual(events[-1].kind, EventKind.FINISHED)


class LauncherWindowLifecycleTests(unittest.TestCase):
    def make_app(self, *, running):
        root = FakeRoot()
        controller = FakeController(running=running)
        messagebox = FakeMessageBox()
        fake_tk = SimpleNamespace(StringVar=lambda **_kwargs: object())
        modules = (fake_tk, object(), object(), messagebox)
        with (
            patch("prompt_to_play.launcher._load_tk_modules", return_value=modules),
            patch.object(LauncherApp, "_build_widgets"),
        ):
            app = LauncherApp(root, controller)
        return app, root, controller, messagebox

    def test_registers_close_protocol_and_blocks_close_while_running(self):
        app, root, _controller, messagebox = self.make_app(running=True)

        self.assertEqual(root.protocols["WM_DELETE_WINDOW"], app._on_close_requested)
        root.protocols["WM_DELETE_WINDOW"]()

        self.assertFalse(root.destroyed)
        self.assertEqual(len(messagebox.warnings), 1)
        title, message, parent = messagebox.warnings[0]
        self.assertEqual(title, "任务正在运行")
        self.assertIn("等待", message)
        self.assertIs(parent, root)

    def test_close_is_allowed_after_pipeline_finishes(self):
        app, root, controller, messagebox = self.make_app(running=True)
        controller.running = False

        app._on_close_requested()

        self.assertTrue(root.destroyed)
        self.assertEqual(messagebox.warnings, [])


if __name__ == "__main__":
    unittest.main()
