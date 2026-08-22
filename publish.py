#!/usr/bin/env python3
"""Publish Godogen runtime files into a target game repository."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
ENGINES = {
    "godot": "Godot",
    "bevy": "Bevy",
    "babylon": "Babylon.js",
}
AGENTS = {"claude", "codex"}
WORKFLOWS = {"autonomous", "prompt-to-play"}
PROMPT_TO_PLAY_RUNTIME_FILES = (
    "README.md",
    "agents.py",
    "direct_agents.py",
    "direct_common.py",
    "direct_evaluation.py",
    "direct_generation.py",
    "direct_host.py",
    "direct_pipeline.py",
    "launcher.py",
    "provider.py",
)


@dataclass(frozen=True)
class PublishConfig:
    engine: str
    agent: str
    workflow: str
    target: Path
    force: bool = False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish Godogen runtime files into a target game repo."
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="target directory (an alternative to --out)",
    )
    parser.add_argument("--engine", required=True, choices=sorted(ENGINES))
    parser.add_argument("--agent", required=True, choices=sorted(AGENTS))
    parser.add_argument("--out", dest="output", help="target directory")
    parser.add_argument(
        "--workflow",
        default="autonomous",
        choices=sorted(WORKFLOWS),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="remove existing target contents before publishing",
    )
    return parser


def parse_config(argv: list[str] | None = None) -> PublishConfig:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.target and args.output:
        parser.error("target specified more than once")
    output = args.output or args.target
    if not output:
        parser.error("--out <target_dir> or a positional target is required")
    if args.workflow == "prompt-to-play" and args.engine != "godot":
        parser.error("the prompt-to-play workflow only supports Godot")

    return PublishConfig(
        engine=args.engine,
        agent=args.agent,
        workflow=args.workflow,
        target=Path(output).expanduser(),
        force=args.force,
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_force_target(target: Path, repo_root: Path = REPO_ROOT) -> None:
    """Reject broad or source-containing targets before recursive deletion."""

    resolved = target.resolve(strict=False)
    repo = repo_root.resolve(strict=False)
    home = Path.home().resolve(strict=False)
    cwd = Path.cwd().resolve(strict=False)

    if resolved == Path(resolved.anchor):
        raise ValueError(f"refusing to force-publish to filesystem root: {resolved}")
    if _is_relative_to(cwd, resolved):
        raise ValueError(
            f"refusing to delete the current directory or its ancestor: {resolved}"
        )
    if _is_relative_to(repo, resolved) or _is_relative_to(resolved, repo):
        raise ValueError(
            f"refusing to delete the source repo, its ancestor, or its contents: {resolved}"
        )
    if _is_relative_to(home, resolved):
        raise ValueError(
            f"refusing to delete the home directory or its ancestor: {resolved}"
        )
    if target.is_symlink():
        raise ValueError(f"refusing to force-publish through a symlink: {target}")
    is_junction = getattr(target, "is_junction", None)
    if is_junction is not None and is_junction():
        raise ValueError(f"refusing to force-publish through a junction: {target}")


def _source_paths(config: PublishConfig, repo_root: Path) -> dict[str, Path]:
    prompt_name = (
        "prompt-to-play.md" if config.workflow == "prompt-to-play" else "runtime.md"
    )
    paths = {
        "asset_skill": repo_root / "asset-gen",
        "manifest": repo_root / "prompts" / prompt_name,
        "engine_guide": repo_root / "engines" / f"{config.engine}.md",
        "render_helper": repo_root / "scripts" / "render_dir.py",
        "metadata_helper": repo_root / "scripts" / "generate_codex_metadata.py",
    }
    if config.workflow == "prompt-to-play":
        paths["workflow_resources"] = repo_root / "prompt_to_play"
        paths["workflow_template"] = repo_root / "prompt_to_play" / "direct_template"
        for relative in PROMPT_TO_PLAY_RUNTIME_FILES:
            paths[f"workflow_runtime:{relative}"] = (
                paths["workflow_resources"] / relative
            )
    return paths


def validate_source_layout(config: PublishConfig, repo_root: Path = REPO_ROOT) -> None:
    missing = [
        str(path)
        for path in _source_paths(config, repo_root).values()
        if not path.exists()
    ]
    if missing:
        joined = "\n  - ".join(missing)
        raise FileNotFoundError(f"required publish source is missing:\n  - {joined}")


def _run_helper(script: Path, *args: str) -> None:
    subprocess.run(
        [sys.executable, "-X", "utf8", str(script), *args],
        check=True,
    )


def _copy_tree(
    source: Path,
    destination: Path,
    *,
    exclude: tuple[str, ...] = (),
) -> None:
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            ".godot",
            "bin",
            "obj",
            "artifacts",
            *exclude,
        ),
    )


def _copy_selected_files(
    source: Path,
    destination: Path,
    relative_paths: tuple[str, ...],
) -> None:
    """Copy an explicit runtime allowlist without carrying source-only modules."""

    destination.mkdir(parents=True, exist_ok=True)
    for relative_text in relative_paths:
        relative = Path(relative_text)
        copied = destination / relative
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, copied)


def _copy_missing_tree(source: Path, destination: Path) -> None:
    """Add missing scaffold files without replacing anything user-owned."""

    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        return
    destination.mkdir(parents=True, exist_ok=True)

    for child in source.iterdir():
        if (
            child.name in {"__pycache__", ".godot", "bin", "obj", "artifacts"}
            or child.suffix == ".pyc"
        ):
            continue
        copied = destination / child.name
        copied_exists = copied.exists() or copied.is_symlink()
        child_is_directory = child.is_dir() and not child.is_symlink()
        if copied_exists:
            if child_is_directory and copied.is_dir() and not copied.is_symlink():
                _copy_missing_tree(child, copied)
            continue
        if child_is_directory:
            _copy_tree(child, copied)
        else:
            shutil.copy2(child, copied, follow_symlinks=False)


def _replace_tree(source: Path, destination: Path) -> None:
    if destination.is_symlink() or destination.is_file():
        destination.unlink()
    elif destination.is_dir():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _copy_tree(source, destination)


def _remove_target(target: Path) -> None:
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)


def _gitignore_text(config: PublishConfig, engine_guide_file: str) -> str:
    lines = (
        [".claude", "CLAUDE.md"]
        if config.agent == "claude"
        else [".agents", "AGENTS.md", ".codex"]
    )
    lines.append(engine_guide_file)
    if config.engine == "godot":
        lines.extend(["assets", "screenshots", ".godot", "*.import", "bin/", "obj/"])
    elif config.engine == "bevy":
        lines.extend(["/target", "/screenshots"])
    else:
        lines.extend(["/node_modules", "/dist", "screenshots"])
    return "\n".join(lines) + "\n"


def _init_git(target: Path) -> None:
    try:
        subprocess.run(
            ["git", "-C", str(target), "init", "-q"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass


def publish(config: PublishConfig, repo_root: Path = REPO_ROOT) -> Path:
    repo_root = repo_root.resolve(strict=False)
    target = config.target.resolve(strict=False)

    if config.workflow == "prompt-to-play" and config.engine != "godot":
        raise ValueError("the prompt-to-play workflow only supports Godot")
    if config.force:
        validate_force_target(config.target, repo_root)
    validate_source_layout(config, repo_root)

    engine_display = ENGINES[config.engine]
    engine_guide_file = f"{config.engine}.md"
    manifest = "CLAUDE.md" if config.agent == "claude" else "AGENTS.md"
    skills_dir_rel = (
        Path(".claude") / "skills"
        if config.agent == "claude"
        else Path(".agents") / "skills"
    )
    agent_name = "Claude" if config.agent == "claude" else "Codex"
    asset_skill_command = "/asset-gen" if config.agent == "claude" else "$asset-gen"
    asset_skill_dir = (skills_dir_rel / "asset-gen").as_posix()
    runtime_asset_dir = "src/assets" if config.engine == "babylon" else "assets"
    source = _source_paths(config, repo_root)

    with tempfile.TemporaryDirectory(prefix="godogen-publish-") as temp_name:
        stage = Path(temp_name)
        staged_skill_root = stage / "skills"
        staged_skill = staged_skill_root / "asset-gen"
        _copy_tree(source["asset_skill"], staged_skill)
        _run_helper(
            source["render_helper"],
            str(staged_skill_root),
            f"AGENT_NAME={agent_name}",
            f"ASSET_GEN_SKILL_DIR={asset_skill_dir}",
            f"ASSET_SKILL_COMMAND={asset_skill_command}",
            f"RUNTIME_ASSET_DIR={runtime_asset_dir}",
        )
        if config.agent == "codex":
            _run_helper(source["metadata_helper"], str(staged_skill_root))

        staged_manifest = stage / manifest
        shutil.copy2(source["manifest"], staged_manifest)
        _run_helper(
            source["render_helper"],
            str(stage),
            f"ENGINE_NAME={engine_display}",
            f"ENGINE_GUIDE_FILE={engine_guide_file}",
            f"ASSET_SKILL_COMMAND={asset_skill_command}",
        )

        staged_guide = stage / engine_guide_file
        shutil.copy2(source["engine_guide"], staged_guide)

        staged_workflow = stage / "prompt_to_play"
        staged_scaffold = stage / "direct_template"
        if config.workflow == "prompt-to-play":
            _copy_selected_files(
                source["workflow_resources"],
                staged_workflow,
                PROMPT_TO_PLAY_RUNTIME_FILES,
            )
            _copy_tree(source["workflow_template"], staged_scaffold)

        if config.force and target.exists():
            print(f"Force: cleaning {target}")
            _remove_target(target)
        target.mkdir(parents=True, exist_ok=True)

        if config.workflow == "prompt-to-play":
            _copy_missing_tree(staged_scaffold, target)
            print("Installed missing Godot workflow scaffold files")

        destination_skill = target / skills_dir_rel / "asset-gen"
        _replace_tree(staged_skill, destination_skill)
        print("Installed asset-gen skill")

        shutil.copy2(staged_manifest, target / manifest)
        print(f"Created {manifest}")
        shutil.copy2(staged_guide, target / engine_guide_file)
        print(f"Created {engine_guide_file}")

        if config.workflow == "prompt-to-play":
            _replace_tree(staged_workflow, target / "prompt_to_play")
            print("Installed prompt-to-play workflow resources")

        gitignore = target / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(
                _gitignore_text(config, engine_guide_file),
                encoding="utf-8",
                newline="\n",
            )
            print("Created .gitignore")

        _init_git(target)

    print("Done.")
    return target


def main(argv: list[str] | None = None) -> int:
    try:
        config = parse_config(argv)
        target = config.target.resolve(strict=False)
        print(
            f"Publishing {config.engine}/{config.agent} "
            f"({config.workflow}) to: {target}"
        )
        publish(config)
    except (
        FileNotFoundError,
        OSError,
        ValueError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
