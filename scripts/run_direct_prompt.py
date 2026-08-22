"""Run the direct Prompt-to-Play pipeline without the Tk desktop wrapper."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from prompt_to_play.direct_pipeline import (  # noqa: E402
    PROJECT_RESULT,
    SELECTED_REVISION_RESULT,
    DirectPipelineDependencies,
    create_pipeline_commands,
)
from prompt_to_play.launcher import (  # noqa: E402
    LaunchRequest,
    PipelineEvent,
    PipelineRunner,
)


def _emit(event: PipelineEvent) -> None:
    stage = event.stage.value if event.stage is not None else "pipeline"
    print(f"[{event.kind.value}] {stage}: {event.message}", flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate, verify, repair, and launch one Godot game prompt."
    )
    parser.add_argument("prompt_file", type=Path)
    parser.add_argument(
        "--reference",
        action="append",
        default=[],
        type=Path,
        help="Optional reference image; repeat for multiple images.",
    )
    parser.add_argument(
        "--max-corrections",
        type=int,
        default=6,
        choices=range(0, 7),
        metavar="0..6",
        help="Maximum feedback repair rounds (default: 6).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    prompt_path = args.prompt_file.expanduser().resolve(strict=True)
    prompt = prompt_path.read_text(encoding="utf-8")
    references = [path.expanduser().resolve(strict=True) for path in args.reference]
    dependencies = DirectPipelineDependencies(
        max_correction_iterations=args.max_corrections
    )
    runner = PipelineRunner(create_pipeline_commands(dependencies))
    result = runner.run(LaunchRequest.from_values(prompt, references), _emit)
    print(f"FINAL_SUCCESS={result.success}", flush=True)
    print(f"PROJECT_DIR={result.state.results.get(PROJECT_RESULT, '')}", flush=True)
    print(
        f"SELECTED_REVISION={result.state.results.get(SELECTED_REVISION_RESULT, '')}",
        flush=True,
    )
    print(f"ERROR={result.error or ''}", flush=True)
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
