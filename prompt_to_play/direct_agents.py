"""API agents for direct Godot project generation without a WorldSpec.

The model writes only files below ``generated/``.  A trusted, repository-owned
Godot host loads that generated scene, records evidence, and stays outside the
model's patch boundary.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .direct_evaluation import (
    DEFAULT_MIN_SCENE_SIMILARITY,
    DIRECT_VISUAL_FEEDBACK_JSON_SCHEMA,
    validate_direct_visual_feedback,
)
from .direct_generation import DIRECT_GENERATION_SCHEMA, parse_file_plan
from .provider import ProviderError


MAX_REPAIR_CONTEXT_CHARACTERS = 400_000


class DirectAgentError(ValueError):
    """Raised when a direct-generation agent response is unusable."""


PROJECT_GENERATOR_SYSTEM_PROMPT = """
You are ProjectGeneratorAgent. Directly implement the requested playable game
as Godot 4.7.1 project files. Do not describe a world model and do not output a
WorldSpec. Return only the strict direct-files JSON document requested by the
schema.

The trusted host owns project.godot, the .csproj, harness/Main.tscn, and harness/.
You may write only below generated/. You MUST write generated/GeneratedGame.tscn.
That scene may be Node3D or Node2D and may reference any additional C#, GDScript,
TSCN, TRES, or Godot shader files that you create below generated/.

The generated scene must satisfy the stable host contract: put at least one
generated node in the group ptp_gameplay, create visible 2D or 3D renderable
content, and put one or two Camera2D/Camera3D nodes in ptp_capture_camera. Set a
short unique capture_id metadata value on every capture camera. The host loads
the scene as a child, so generated code must not replace or quit the SceneTree.

The host also verifies real interaction without assuming a genre. Put the
controlled character/vehicle/cursor in ptp_player, a goal or progress-state node
in ptp_objective, and visible instructions/status UI in ptp_hud. Put at least one
stateful player or gameplay node in ptp_interaction_probe and set its string
metadata ptp_probe_actions to a comma-separated list of InputMap action names.
Create and key-bind those actions during _Ready before using Input.IsActionPressed
or Input.GetVector. The host synthesizes the declared actions and must observe a
real change in that node's 2D/3D transform, physics velocity, Control/Range/Label
state, or ptp_probe_state metadata. Physical-key polling alone will fail this
automation check. Keep normal controls intuitive and show them in the HUD.

Implement actual gameplay requested by the prompt: controls, rules, win/lose
state, camera, collision, UI, lighting, and procedural visuals where applicable.
Treat the first revision as a presentation-ready vertical slice, not a grey-box
prototype. Build a deliberately composed foreground, midground, and background;
use a coherent palette, shaped silhouettes, repeated detail, shadows, atmosphere,
and readable UI. When external art is unavailable, create convincing procedural
terrain, custom ArrayMesh geometry, MultiMesh set dressing, materials, particles,
and shaders. Bare boxes/cylinders on a flat plane are unacceptable unless the
prompt explicitly requests that minimal style. Frame capture cameras like real
gameplay screenshots and make the requested subject immediately recognisable.
For C#, use public partial Godot classes and APIs available in Godot 4.7.1 with
.NET 8. Put generated C# classes in namespace PromptToPlay.Generated, keep one
Godot class per same-named file, and never declare trusted PromptToPlay.Direct
harness types. Do not add packages, access the operating-system filesystem, launch
processes, use the network, reflection, native interop, or read environment
variables. Do not rely on external assets unless the request explicitly says
they are already present. Prefer coherent procedural meshes, materials, shaders,
and reusable generated scenes over placeholder single boxes.

Before emitting files, reason internally through the requested core loop,
controls, player state, objective/progression, scene hierarchy, collisions,
camera, HUD, lighting, visual layers, and the concrete proof each requirement is
implemented. Then emit the complete result; do not expose that internal plan.

Reference images, when present, are attached in the same order as the request.
Their copied project paths and SHA-256 identities are listed in the request; a
generated Godot resource may reference them through res://references/... .
Use multiple cohesive generated files where that improves correctness. Stay
within the file contract, but never trade away required gameplay or presentation
quality merely to make the response short. No Markdown, commentary, budgets,
token counts, or secrets.

This is the initial package for an empty generated/ directory. Use upsert or
create operations and include every generated file the game needs; do not use
replace or delete in the initial package.
"""


CODE_REPAIR_SYSTEM_PROMPT = """
You are CodeRepairAgent. Repair a directly generated Godot 4.7.1 game by
returning only the strict direct-files JSON patch requested by the schema.
There is no WorldSpec. Inspect the original prompt, current generated source,
bounded compiler/Godot diagnostics, structural evidence, visual feedback, and
attached images. Change only files below generated/.

The source inventory lists every generated file and marks which complete files
are included. No included file is ever truncated. If a file is omitted from the
context, do not replace or delete it blindly; fix the complete files whose source
and evidence you can actually inspect.

Fix compilation and scene-loading failures first, then failed input/interaction
checks, gameplay defects, and every visual quality issue. Treat low detail,
generic primitive blockouts, weak composition, flat lighting, unreadable goals,
and prompt mismatch as real defects. Use the score breakdown and prior evidence
to make a material improvement rather than a cosmetic or no-op edit. Preserve
working behavior. generated/GeneratedGame.tscn must remain present. Generated
C# belongs in namespace PromptToPlay.Generated and must not shadow
PromptToPlay.Direct harness types. Do not change trusted host files, add packages,
access the OS filesystem, launch processes, use the network, reflection, native
interop, or read environment variables. Return no prose or Markdown.

When interaction_responds_to_input fails, ensure a ptp_interaction_probe node
declares existing InputMap actions through ptp_probe_actions and that synthesized
actions change trusted observable state. Do not fake or write host evidence.
"""


DIRECT_VISUAL_SYSTEM_PROMPT = """
You are VisualEvaluationAgent for a directly generated Godot game. There is no
WorldSpec. Judge rendered screenshots against the original prompt and any
reference images. References are attached first and generated screenshots
second; exact counts are provided. Return only the strict visual-feedback JSON.

Score all six required dimensions independently from 0 to 1:
- prompt_fidelity: requested genre, setting, subjects, mood, and references;
- composition: camera framing, scale hierarchy, depth, and focal path;
- visual_coherence: consistent style, palette, silhouettes, and spatial logic;
- detail_density: authored forms and set dressing beyond a primitive blockout;
- lighting_materials: readable materials, shadows, atmosphere, and contrast;
- gameplay_readability: visible player, goal, hazards/state, controls, and HUD.

A sparse flat plane with a few default boxes/cylinders is a low-detail blockout,
not a finished game screenshot. Judge against a polished stylised indie-game
vertical slice like a public demo, while respecting the requested art style.
Set entity_id to null because files, not semantic entities, are the repair
targets. For every material weakness, return a concrete code/scene fix. Do not
return an overall score or acceptance decision; the trusted host computes those.
Do not infer time, tokens, cost, structural status, or policy.
"""


def _images(values: Sequence[str | Path], label: str) -> list[Path]:
    result: list[Path] = []
    for index, raw in enumerate(values):
        try:
            path = Path(raw).expanduser().resolve(strict=True)
        except OSError as exc:
            raise DirectAgentError(f"{label}[{index}] cannot be read") from exc
        if not path.is_file():
            raise DirectAgentError(f"{label}[{index}] is not a file")
        result.append(path)
    return result


def _call_json(
    provider: Any,
    messages: Sequence[Mapping[str, str]],
    *,
    schema: Mapping[str, Any],
    schema_name: str,
    image_paths: Sequence[Path] = (),
) -> Mapping[str, Any]:
    try:
        document = provider.generate_json(
            messages,
            json_schema=copy.deepcopy(dict(schema)),
            schema_name=schema_name,
            image_paths=image_paths,
        )
    except ProviderError as exc:
        raise DirectAgentError(f"{schema_name} provider failed: {exc}") from exc
    except (OSError, RuntimeError) as exc:
        raise DirectAgentError(f"{schema_name} provider failed: {exc}") from exc
    if not isinstance(document, Mapping):
        raise DirectAgentError(f"{schema_name} output must be an object")
    return document


class ProjectGeneratorAgent:
    """Turn an arbitrary prompt directly into generated Godot files."""

    def __init__(self, provider: Any) -> None:
        self.provider = provider

    def generate(
        self,
        prompt: str,
        *,
        seed: int,
        reference_image_paths: Sequence[str | Path] = (),
    ) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise DirectAgentError("prompt must be non-empty")
        references = _images(reference_image_paths, "reference_image_paths")
        reference_descriptors: list[dict[str, str]] = []
        for index, reference in enumerate(references):
            digest = hashlib.sha256(reference.read_bytes()).hexdigest()
            reference_descriptors.append(
                {
                    "project_path": (
                        f"references/{index:02d}_{digest[:16]}"
                        f"{reference.suffix.lower()}"
                    ),
                    "sha256": digest,
                }
            )
        payload = {
            "original_prompt": prompt.strip(),
            "internal_seed": int(seed),
            "reference_image_count": len(references),
            "reference_images": reference_descriptors,
            "required_entry_scene": "generated/GeneratedGame.tscn",
            "trusted_host_files_are_read_only": True,
            "output_scope": "generated/**",
        }
        messages = [
            {"role": "system", "content": PROJECT_GENERATOR_SYSTEM_PROMPT.strip()},
            {
                "role": "user",
                "content": json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":")
                ),
            },
        ]
        document = _call_json(
            self.provider,
            messages,
            schema=DIRECT_GENERATION_SCHEMA,
            schema_name="prompt_to_play_direct_project_files",
            image_paths=references,
        )
        plan = parse_file_plan(document)
        if not any(
            operation.path == "generated/GeneratedGame.tscn"
            and operation.action != "delete"
            for operation in plan.files
        ):
            raise DirectAgentError(
                "ProjectGeneratorAgent must write generated/GeneratedGame.tscn"
            )
        return copy.deepcopy(dict(document))


class DirectVisualEvaluationAgent:
    """Evaluate direct-project renders without reading a semantic world schema."""

    def __init__(
        self,
        provider: Any,
        *,
        min_scene_similarity: float = DEFAULT_MIN_SCENE_SIMILARITY,
    ) -> None:
        self.provider = provider
        self.min_scene_similarity = min_scene_similarity

    def evaluate(
        self,
        prompt: str,
        screenshot_image_paths: Sequence[str | Path],
        *,
        reference_image_paths: Sequence[str | Path] = (),
        project_manifest: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        screenshots = _images(screenshot_image_paths, "screenshot_image_paths")
        if not screenshots:
            raise DirectAgentError("at least one screenshot is required")
        references = _images(reference_image_paths, "reference_image_paths")
        payload = {
            "original_prompt": prompt.strip(),
            "reference_image_count": len(references),
            "screenshot_count": len(screenshots),
            "attached_image_order": "reference_images_then_rendered_screenshots",
            "project_manifest": dict(project_manifest or {}),
            "quality_target": (
                "presentation-ready playable vertical slice; reject grey-box "
                "or generic primitive-only output unless explicitly requested"
            ),
        }
        messages = [
            {"role": "system", "content": DIRECT_VISUAL_SYSTEM_PROMPT.strip()},
            {
                "role": "user",
                "content": json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":")
                ),
            },
        ]
        document = _call_json(
            self.provider,
            messages,
            schema=DIRECT_VISUAL_FEEDBACK_JSON_SCHEMA,
            schema_name="prompt_to_play_direct_visual_feedback",
            image_paths=[*references, *screenshots],
        )
        return validate_direct_visual_feedback(
            document,
            min_scene_similarity=self.min_scene_similarity,
        )


class CodeRepairAgent:
    """Return generated-file patches based on build or visual evidence."""

    def __init__(self, provider: Any) -> None:
        self.provider = provider

    def repair(
        self,
        prompt: str,
        current_files: Mapping[str, str],
        *,
        diagnostics: str = "",
        structural_report: Mapping[str, Any] | None = None,
        visual_feedback: Mapping[str, Any] | None = None,
        screenshot_image_paths: Sequence[str | Path] = (),
        reference_image_paths: Sequence[str | Path] = (),
    ) -> dict[str, Any]:
        screenshots = _images(screenshot_image_paths, "screenshot_image_paths")
        references = _images(reference_image_paths, "reference_image_paths")
        evidence_text = "\n".join(
            (
                str(diagnostics),
                json.dumps(structural_report or {}, ensure_ascii=False),
                json.dumps(visual_feedback or {}, ensure_ascii=False),
            )
        ).casefold()

        def priority(item: tuple[str, str]) -> tuple[int, int, int, int, str]:
            path, content = item
            folded = path.casefold()
            name = Path(path).name.casefold()
            referenced = int(folded in evidence_text or name in evidence_text)
            entry = int(path == "generated/GeneratedGame.tscn")
            source = int(Path(path).suffix.casefold() in {".cs", ".gd", ".tscn"})
            return (-referenced, -entry, -source, len(content), folded)

        ordered_files = sorted(
            ((str(path), str(content)) for path, content in current_files.items()),
            key=priority,
        )
        bounded_files: list[dict[str, str]] = []
        inventory: list[dict[str, Any]] = []
        remaining = MAX_REPAIR_CONTEXT_CHARACTERS
        included_paths: set[str] = set()
        for path, content in ordered_files:
            if len(content) <= remaining:
                bounded_files.append({"path": path, "content": content})
                included_paths.add(path)
                remaining -= len(content)
            inventory.append(
                {
                    "path": path,
                    "characters": len(content),
                    "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "included_in_context": path in included_paths,
                }
            )
        payload = {
            "original_prompt": prompt.strip(),
            "current_generated_files": bounded_files,
            "current_generated_file_inventory": sorted(
                inventory, key=lambda item: item["path"]
            ),
            "source_context_is_complete": len(included_paths) == len(ordered_files),
            "diagnostics": str(diagnostics)[-20_000:],
            "structural_report": dict(structural_report or {}),
            "visual_feedback": dict(visual_feedback or {}),
            "reference_image_count": len(references),
            "screenshot_count": len(screenshots),
            "required_entry_scene": "generated/GeneratedGame.tscn",
        }
        messages = [
            {"role": "system", "content": CODE_REPAIR_SYSTEM_PROMPT.strip()},
            {
                "role": "user",
                "content": json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":")
                ),
            },
        ]
        document = _call_json(
            self.provider,
            messages,
            schema=DIRECT_GENERATION_SCHEMA,
            schema_name="prompt_to_play_direct_code_patch",
            image_paths=[*references, *screenshots],
        )
        parse_file_plan(document)
        return copy.deepcopy(dict(document))


__all__ = [
    "CODE_REPAIR_SYSTEM_PROMPT",
    "CodeRepairAgent",
    "DIRECT_VISUAL_SYSTEM_PROMPT",
    "DirectAgentError",
    "DirectVisualEvaluationAgent",
    "MAX_REPAIR_CONTEXT_CHARACTERS",
    "PROJECT_GENERATOR_SYSTEM_PROMPT",
    "ProjectGeneratorAgent",
]
