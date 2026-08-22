# Direct Prompt-to-Play runtime

This package implements the default prompt-to-Godot pipeline. Its external
input is one natural-language prompt plus zero or more reference images. The
model directly authors Godot project files; no WorldSpec, fixed entity schema,
or scene-specific intermediate representation is involved.

## Request boundary

The canonical request records:

```json
{"prompt":"...","references":[{"name":"reference.png","sha256":"..."}],"request_hash":"...","seed":123}
```

Reference order is preserved because a prompt may explicitly refer to the
first or second image. Local filenames do not affect the request hash or
internally derived seed. Users are not asked for seed, time budget, Token
budget, evaluation weights, threshold, or correction count. Timing, model and
Token usage, scores, and hashes are measured outputs.

## Direct file contract

ProjectGeneratorAgent and CodeRepairAgent return
`prompt-to-play/direct-files@1` JSON:

```json
{
  "schema": "prompt-to-play/direct-files@1",
  "files": [
    {
      "path": "generated/GeneratedGame.tscn",
      "action": "upsert",
      "content": "[gd_scene ...]"
    },
    {
      "path": "generated/GeneratedGame.cs",
      "action": "upsert",
      "content": "using Godot; ..."
    }
  ]
}
```

`action` is `upsert`, `create`, `replace`, or `delete`; omitted action means
`upsert`. Generation must create `generated/GeneratedGame.tscn`. Additional
Godot scenes, C# or GDScript, resources, shaders, JSON, and Markdown may express
whatever mechanics and visuals the current prompt requires.

The host validates the complete response before applying it:

- every path must be normalized, project-relative, and below `generated/`;
- only `.godot`, `.cs`, `.gd`, `.tscn`, `.tres`, `.gdshader`, `.json`, and
  `.md` are accepted;
- file count, individual size, and total response size are bounded;
- duplicate/case-ambiguous paths, traversal, absolute paths, reserved device
  names, unsupported resource URLs, symlinks, and junctions are rejected;
- generated scripts cannot execute processes, access the network or host
  filesystem/environment, use reflection/native interop, or use unsafe C#;
- writes are atomic and checked again after application.

`apply_file_plan` produces a host-owned
`prompt-to-play/direct-manifest@1` containing per-file hashes and a hash of the
complete generated source tree. The manifest contains no prompt, credentials,
or source text.

This static gate is a deliberately conservative trust boundary. It does not
claim to replace operating-system sandboxing for hostile code.

## Trusted Godot host

`direct_template/` is a Godot 4.7.x .NET project with a stable boundary:

- `project.godot`, `PromptToPlayDirect.csproj`, and `harness/**` are trusted
  files copied from the repository and never exposed to model patches;
- `harness/Main.tscn` loads `res://generated/GeneratedGame.tscn`;
- the generated scene marks playable content with the `ptp_gameplay` group and
  provides one or two `Camera2D`/`Camera3D` nodes in
  `ptp_capture_camera` for consistent evaluation;
- player, objective/progression, and HUD nodes use `ptp_player`,
  `ptp_objective`, and `ptp_hud`; a `ptp_interaction_probe` node declares
  InputMap actions through `ptp_probe_actions`, allowing the host to verify a
  real observable response to synthesized input;
- the harness writes structure and capture evidence bound to `run_id`,
  revision, and generated-project hash.

Automation environment variables are operational evidence coordinates, not
generation inputs:

- `PTP_AUTOMATION=1` (or `PTP_VERIFY=1`) runs structural checks and exits;
- `PTP_CAPTURE=1` captures evaluation screenshots;
- `PTP_RUN_ID`, `PTP_REVISION`, and `PTP_PROJECT_SHA256` select and bind the
  artifact directory.

Artifacts are written below
`artifacts/runs/<run-id>/rev_<n>/`. Previous revisions are not overwritten.

## Agent loop

1. ProjectGeneratorAgent receives the raw prompt, derived seed, references,
   trusted-host contract, and direct-file JSON schema.
2. The host validates and atomically applies `generated/**`.
3. The host restores/builds the fixed .NET project and runs a headless Godot
   structural check.
4. On a valid build it verifies interaction and captures rendered views.
   VisualEvaluationAgent scores prompt fidelity, composition, coherence,
   detail, lighting/materials, and gameplay readability; host code computes the
   weighted gate.
5. CodeRepairAgent receives bounded generated source plus compiler, structural,
   and visual evidence and returns another direct-file patch.
6. The host repeats up to four repairs by default (hard ceiling six), stops
   early on acceptance, restores and rebuilds the best accepted revision, and
   launches it. If no revision passes, it preserves the best project and
   evidence but reports failure instead of launching an unfinished game.

The three roles have isolated model sessions and auditable timing/Token traces.
Neither the runtime nor a generated project needs the Codex CLI.

## Entry points

From the repository root:

```powershell
# Recommended: masked local API-key prompt
powershell -ExecutionPolicy Bypass -File scripts/start_prompt_to_play_api.ps1

# Environment is already configured
python -m prompt_to_play.direct_pipeline

# Static checks
python -m pytest -q
```

Every successful run is persisted under
`../output/generated/<request-hash>/run-<id>/`. Closing Godot does not remove
the generated project. It can be reopened, edited, copied, or played without a
new API call.

Older schema-driven modules are retained only as archived implementation
reference. They are not dependencies of `direct_pipeline.py` and are not
installed into newly published Prompt-to-Play projects.
