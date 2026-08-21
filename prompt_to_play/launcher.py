"""Minimal Windows launcher for a Prompt-to-Play pipeline.

The UI deliberately knows nothing about planners, providers, Godot, or publish.py.
Callers inject five stage commands through :class:`PipelineCommands`; each command
runs on a worker thread and can store typed or untyped values in PipelineState for
the next stage. Tk widgets are touched only by the main thread.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence


class Stage(str, Enum):
    PLAN = "plan"
    VALIDATE = "validate"
    PUBLISH = "publish"
    BUILD = "build"
    LAUNCH = "launch"


STAGE_LABELS: Mapping[Stage, str] = MappingProxyType(
    {
        Stage.PLAN: "规划",
        Stage.VALIDATE: "校验",
        Stage.PUBLISH: "发布",
        Stage.BUILD: "构建",
        Stage.LAUNCH: "启动",
    }
)


class EventKind(str, Enum):
    STAGE_STARTED = "stage_started"
    LOG = "log"
    STAGE_COMPLETED = "stage_completed"
    ERROR = "error"
    FINISHED = "finished"


@dataclass(frozen=True)
class LaunchRequest:
    prompt: str
    reference_images: tuple[Path, ...] = ()

    @classmethod
    def from_values(
        cls,
        prompt: str,
        reference_images: Sequence[str | os.PathLike[str]] = (),
    ) -> "LaunchRequest":
        normalized_prompt = prompt.strip()
        if not normalized_prompt:
            raise ValueError("请输入游戏世界描述。")

        normalized_images: list[Path] = []
        seen: set[str] = set()
        for raw_path in reference_images:
            path = Path(raw_path).expanduser().resolve(strict=False)
            if not path.is_file():
                raise ValueError(f"参考图不存在或不是文件：{path}")
            key = os.path.normcase(str(path))
            if key not in seen:
                normalized_images.append(path)
                seen.add(key)
        return cls(normalized_prompt, tuple(normalized_images))


@dataclass
class PipelineState:
    """Shared state passed through the five injected stage commands."""

    request: LaunchRequest
    results: dict[str, Any] = field(default_factory=dict)

    def set_result(self, name: str, value: Any) -> None:
        if not name:
            raise ValueError("result name must not be empty")
        self.results[name] = value

    def require_result(self, name: str) -> Any:
        try:
            return self.results[name]
        except KeyError as exc:
            raise RuntimeError(f"流水线缺少阶段结果：{name}") from exc


LogSink = Callable[[str], None]
PipelineCommand = Callable[[PipelineState, LogSink], None]


@dataclass(frozen=True)
class PipelineCommands:
    """Injected commands; no stage is coupled to a concrete provider or CLI."""

    plan: PipelineCommand
    validate: PipelineCommand
    publish: PipelineCommand
    build: PipelineCommand
    launch: PipelineCommand

    def ordered(self) -> tuple[tuple[Stage, PipelineCommand], ...]:
        return (
            (Stage.PLAN, self.plan),
            (Stage.VALIDATE, self.validate),
            (Stage.PUBLISH, self.publish),
            (Stage.BUILD, self.build),
            (Stage.LAUNCH, self.launch),
        )

    @classmethod
    def unconfigured(cls) -> "PipelineCommands":
        def missing_adapter(_state: PipelineState, _log: LogSink) -> None:
            raise PipelineConfigurationError(
                "尚未注入 Prompt-to-Play 流水线命令；请由 planner/provider 入口调用 "
                "run_launcher(PipelineCommands(...))。"
            )

        return cls(
            plan=missing_adapter,
            validate=missing_adapter,
            publish=missing_adapter,
            build=missing_adapter,
            launch=missing_adapter,
        )


class PipelineConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PipelineEvent:
    kind: EventKind
    stage: Stage | None
    message: str


@dataclass(frozen=True)
class PipelineResult:
    success: bool
    state: PipelineState
    failed_stage: Stage | None = None
    error: str | None = None


class EventSink(Protocol):
    def __call__(self, event: PipelineEvent) -> None: ...


class PipelineRunner:
    """Synchronous runner. AsyncPipelineController supplies the worker thread."""

    def __init__(self, commands: PipelineCommands):
        self._commands = commands

    def run(self, request: LaunchRequest, emit: EventSink) -> PipelineResult:
        state = PipelineState(request=request)
        for stage, command in self._commands.ordered():
            label = STAGE_LABELS[stage]
            emit(PipelineEvent(EventKind.STAGE_STARTED, stage, f"{label}开始"))

            def log(message: str, *, _stage: Stage = stage) -> None:
                text = str(message).rstrip()
                if text:
                    emit(PipelineEvent(EventKind.LOG, _stage, text))

            try:
                command(state, log)
            except Exception as exc:
                message = f"{label}失败：{exc}"
                emit(PipelineEvent(EventKind.ERROR, stage, message))
                emit(PipelineEvent(EventKind.FINISHED, stage, "流水线失败"))
                return PipelineResult(
                    success=False,
                    state=state,
                    failed_stage=stage,
                    error=str(exc),
                )
            emit(PipelineEvent(EventKind.STAGE_COMPLETED, stage, f"{label}完成"))

        emit(PipelineEvent(EventKind.FINISHED, None, "游戏已启动"))
        return PipelineResult(success=True, state=state)


class AsyncPipelineController:
    """Owns one daemon worker and a thread-safe event queue for the UI."""

    def __init__(self, runner: PipelineRunner):
        self._runner = runner
        self._events: queue.Queue[PipelineEvent] = queue.Queue()
        self._lock = threading.Lock()
        self._running = False
        self._worker: threading.Thread | None = None
        self._last_result: PipelineResult | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def last_result(self) -> PipelineResult | None:
        with self._lock:
            return self._last_result

    def start(self, request: LaunchRequest) -> bool:
        """Start once; return False when another pipeline is still running."""

        with self._lock:
            if self._running:
                return False
            self._running = True
            self._last_result = None
            self._worker = threading.Thread(
                target=self._run_worker,
                args=(request,),
                name="prompt-to-play-pipeline",
                daemon=True,
            )
            worker = self._worker
        worker.start()
        return True

    def drain_events(self, maximum: int | None = None) -> list[PipelineEvent]:
        events: list[PipelineEvent] = []
        while maximum is None or len(events) < maximum:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
        return events

    def wait(self, timeout: float | None = None) -> PipelineResult | None:
        """Test/CLI helper; the Tk UI never blocks on this method."""

        with self._lock:
            worker = self._worker
        if worker is not None:
            worker.join(timeout)
        return self.last_result

    def _run_worker(self, request: LaunchRequest) -> None:
        try:
            result = self._runner.run(request, self._events.put)
        except Exception as exc:  # defensive boundary around custom runners
            message = f"流水线内部错误：{exc}"
            self._events.put(PipelineEvent(EventKind.ERROR, None, message))
            self._events.put(PipelineEvent(EventKind.FINISHED, None, "流水线失败"))
            result = PipelineResult(
                success=False,
                state=PipelineState(request),
                error=str(exc),
            )
        finally:
            with self._lock:
                self._last_result = result
                self._running = False


def _load_tk_modules() -> tuple[Any, Any, Any, Any]:
    """Import lazily so non-GUI tests do not require a display or Tcl session."""

    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    return tk, ttk, filedialog, messagebox


class LauncherApp:
    POLL_INTERVAL_MS = 50

    def __init__(self, root: Any, controller: AsyncPipelineController):
        self.root = root
        self.controller = controller
        self._tk, self._ttk, self._filedialog, self._messagebox = _load_tk_modules()
        self._reference_images: list[Path] = []
        self._failed = False

        root.title("Prompt-to-Play")
        root.geometry("820x650")
        root.minsize(680, 520)

        self._status = self._tk.StringVar(value="等待输入")
        self._build_widgets()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close_requested)
        self.root.after(self.POLL_INTERVAL_MS, self._poll_events)

    def _build_widgets(self) -> None:
        frame = self._ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(6, weight=1)

        self._ttk.Label(frame, text="描述你想生成的游戏世界").grid(
            row=0, column=0, sticky="w"
        )
        self.prompt_text = self._tk.Text(frame, height=7, wrap="word")
        self.prompt_text.grid(row=1, column=0, sticky="nsew", pady=(6, 12))

        reference_header = self._ttk.Frame(frame)
        reference_header.grid(row=2, column=0, sticky="ew")
        reference_header.columnconfigure(0, weight=1)
        self._ttk.Label(reference_header, text="参考图（可选）").grid(
            row=0, column=0, sticky="w"
        )
        self.add_button = self._ttk.Button(
            reference_header, text="添加图片", command=self._add_images
        )
        self.add_button.grid(row=0, column=1, padx=(8, 0))
        self.remove_button = self._ttk.Button(
            reference_header, text="移除所选", command=self._remove_images
        )
        self.remove_button.grid(row=0, column=2, padx=(8, 0))

        self.reference_list = self._tk.Listbox(frame, height=4)
        self.reference_list.grid(row=3, column=0, sticky="ew", pady=(6, 12))

        action_bar = self._ttk.Frame(frame)
        action_bar.grid(row=4, column=0, sticky="ew")
        action_bar.columnconfigure(1, weight=1)
        self.start_button = self._ttk.Button(
            action_bar, text="生成并启动", command=self._start_pipeline
        )
        self.start_button.grid(row=0, column=0, padx=(0, 12))
        self.progress = self._ttk.Progressbar(
            action_bar, mode="determinate", maximum=len(Stage)
        )
        self.progress.grid(row=0, column=1, sticky="ew")
        self._ttk.Label(action_bar, textvariable=self._status).grid(
            row=0, column=2, padx=(12, 0)
        )

        self._ttk.Label(frame, text="运行日志").grid(
            row=5, column=0, sticky="w", pady=(14, 6)
        )
        self.log_text = self._tk.Text(frame, wrap="word", state="disabled")
        self.log_text.grid(row=6, column=0, sticky="nsew")

    def _add_images(self) -> None:
        selected = self._filedialog.askopenfilenames(
            title="选择参考图",
            filetypes=(
                ("图片", "*.png *.jpg *.jpeg *.webp *.bmp"),
                ("所有文件", "*.*"),
            ),
        )
        known = {os.path.normcase(str(path)) for path in self._reference_images}
        for raw_path in selected:
            path = Path(raw_path).resolve(strict=False)
            key = os.path.normcase(str(path))
            if key not in known:
                self._reference_images.append(path)
                self.reference_list.insert("end", str(path))
                known.add(key)

    def _remove_images(self) -> None:
        selected = list(self.reference_list.curselection())
        for index in reversed(selected):
            self.reference_list.delete(index)
            del self._reference_images[index]

    def _start_pipeline(self) -> None:
        try:
            request = LaunchRequest.from_values(
                self.prompt_text.get("1.0", "end"), self._reference_images
            )
        except ValueError as exc:
            self._messagebox.showerror("无法开始", str(exc), parent=self.root)
            return

        if not self.controller.start(request):
            self._append_log("已有流水线正在运行。")
            return

        self._failed = False
        self.progress["value"] = 0
        self._clear_log()
        self._set_running_controls(True)
        self._status.set("准备运行")
        self._append_log("已提交生成请求。")

    def _poll_events(self) -> None:
        for event in self.controller.drain_events():
            self._handle_event(event)
        self.root.after(self.POLL_INTERVAL_MS, self._poll_events)

    def _on_close_requested(self) -> None:
        if self.controller.running:
            self._messagebox.showwarning(
                "任务正在运行",
                "规划或构建尚未完成，请等待流水线结束后再关闭窗口。",
                parent=self.root,
            )
            return
        self.root.destroy()

    def _handle_event(self, event: PipelineEvent) -> None:
        if event.kind == EventKind.STAGE_STARTED and event.stage is not None:
            stage_number = list(Stage).index(event.stage) + 1
            self.progress["value"] = stage_number - 1
            self._status.set(f"正在{STAGE_LABELS[event.stage]}")
            self._append_log(f"[{STAGE_LABELS[event.stage]}] 开始")
        elif event.kind == EventKind.LOG:
            self._append_log(event.message)
        elif event.kind == EventKind.STAGE_COMPLETED and event.stage is not None:
            stage_number = list(Stage).index(event.stage) + 1
            self.progress["value"] = stage_number
            self._append_log(f"[{STAGE_LABELS[event.stage]}] 完成")
        elif event.kind == EventKind.ERROR:
            self._failed = True
            self._status.set("运行失败")
            self._append_log(event.message)
        elif event.kind == EventKind.FINISHED:
            if not self._failed:
                self._status.set(event.message)
                self.progress["value"] = len(Stage)
            self._append_log(event.message)
            self._set_running_controls(False)

    def _set_running_controls(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.start_button.configure(state=state)
        self.add_button.configure(state=state)
        self.remove_button.configure(state=state)

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")


def run_launcher(commands: PipelineCommands) -> int:
    """Create the Windows UI with injected stage commands."""

    try:
        tk, _ttk, _filedialog, _messagebox = _load_tk_modules()
        root = tk.Tk()
    except Exception as exc:  # Tk raises TclError when no desktop session is available.
        print(f"无法启动图形界面：{exc}", file=sys.stderr)
        return 2

    LauncherApp(root, AsyncPipelineController(PipelineRunner(commands)))
    root.mainloop()
    return 0


def main() -> int:
    return run_launcher(PipelineCommands.unconfigured())


if __name__ == "__main__":
    raise SystemExit(main())
