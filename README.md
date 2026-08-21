# Prompt-to-Play on Godogen

This fork adds a spec-driven Godot workflow to [Godogen](https://github.com/htdt/godogen): a user gives a natural-language world description and optional reference images, an Agent plans a validated world, and the same deterministic compiler turns different plans into explorable 3D scenes with lightweight objectives.

The floating mechanical city in `prompt_to_play/examples/` is only one acceptance fixture. It is not embedded in the compiler. A second foggy-forest fixture exercises the same generation and interaction code with a different theme, topology, entity set, and objective graph.

## What is implemented

- A versioned, engine-independent `WorldSpec`, `PatchSpec`, evaluation policy, and validator.
- A reusable Godot 4.7 C# scaffold that builds regions, roads, buildings, props, lights, fixed cameras, generic interactables, objectives, and a reachable exit from data.
- A desktop launcher where the player enters an arbitrary prompt and optional reference images, then watches Plan -> Validate -> Publish -> Build -> Play run off the UI thread.
- A structured planner with two interchangeable backends: an OpenAI-compatible API or the user's existing Codex CLI login (the zero-key default on this Windows setup).
- Deterministic primitive fallbacks for unknown logical prefab IDs, so unfamiliar prompts remain playable without paid assets.
- WASD and mouse exploration, Space to jump, E to interact, contextual prompts, objective progress, and completion feedback.
- Machine-readable build and structural evidence for `scene_loads`, `world_graph_connected`, `objectives_completable`, and `completion_reachable`.
- A constrained feedback contract: stable-ID patches, exact base hashes, allowlisted fields, at most two correction rounds, and best-revision rollback.
- A cross-platform publisher and Python CI matrix for Windows and Linux.

The host Agent performs prompt/reference interpretation and visual judgement. The repository supplies its runtime protocol, deterministic execution layer, contracts, evidence format, and correction guardrails. The scope is prompt-conditioned explorable 3D worlds, not arbitrary game genres or photorealistic reconstruction.

## Public input and internal measurements

The public request contains only:

1. a natural-language description; and
2. optional reference images.

The request hash and seed are derived internally. Evaluation weights, thresholds, normalization references, and correction limit come from the versioned policy. Generation time, model calls, Token usage, scores, and reproducibility hashes are recorded outputs—not parameters that the user must provide.

## Quick start

Prerequisites are Python 3.11+, the .NET 8 SDK, and the .NET build of Godot 4.7.x.

### Enter a prompt and play

On Windows, the shortest complete path is:

```powershell
python -m prompt_to_play.pipeline
```

Enter any world description, optionally add reference images, and choose
`生成并启动`. The launcher performs structured planning, contract validation,
publishing, .NET compilation, a headless Godot structural check, and only then
opens the playable game. Every run gets its own project under
`../output/generated/<request-hash>/run-<id>/`, so regenerating the same prompt
cannot overwrite a game that is still open. The example worlds are never used
as runtime fallbacks.

The default provider mode is `auto`: use `OPENAI_API_KEY` (or
`PROMPT_TO_PLAY_API_KEY`) when present, otherwise reuse a signed-in `codex` CLI.
Set `PROMPT_TO_PLAY_PROVIDER=openai` or `codex` to force a backend. The Codex
backend attaches every selected image to the planning request; the generic HTTP
adapter requires textual image summaries rather than pretending it inspected
image bytes.

Player controls are WASD + mouse, Space to jump, and E to interact. Seed, time,
Token use, thresholds, and correction limits are not fields in this launcher:
the seed is derived internally, while time and Token use remain evaluation
outputs.

### Publish for a host Agent

Publish a Codex-ready Godot project:

```powershell
python publish.py --engine godot --agent codex --workflow prompt-to-play --out D:\ptp-game
Set-Location D:\ptp-game
python prompt_to_play/lifecycle.py create-request --prompt "your world description" --output request.json
python prompt_to_play/contracts.py validate-world spec/world.json
dotnet build
$env:PTP_RUN_ID = "acceptance-001"
$env:PTP_REVISION = "0"
godot --headless --path . --quit-after 5
$env:PTP_CAPTURE = "1"
godot --path . --rendering-method gl_compatibility --audio-driver Dummy
Remove-Item Env:PTP_CAPTURE
godot --path .
```

For Claude Code, change `--agent codex` to `--agent claude`. Re-publishing fills missing scaffold files without overwriting project scripts or `spec/world.json`; `--force` intentionally recreates a safe target from scratch.

Then give the host Agent the actual world prompt and any references. Its published `AGENTS.md` or `CLAUDE.md` requires this loop:

```text
prompt + optional references
        -> Plan and validate WorldSpec
        -> Execute deterministic Godot compiler
        -> Evaluate structure and fixed-camera evidence
        -> Feedback Agent emits accept/patch/rollback/stop
        -> Rebuild and select best revision
```

Headless execution writes the build manifest and structural report to
`artifacts/runs/<run-id>/rev_<n>/`. Capture mode visits every WorldSpec camera,
rejects empty or near-uniform frames, writes PNGs, and records their SHA-256
digests in `capture_manifest.json`. Set `PTP_REVISION` to `1` or `2` only after
a validated feedback patch; earlier evidence remains untouched.

## Contracts and tests

```powershell
python prompt_to_play/contracts.py validate-world prompt_to_play/examples/world.json
python prompt_to_play/contracts.py validate-world prompt_to_play/examples/forest_world.json
python -m unittest discover -s tests -v
```

See [the architecture and rubric mapping](docs/PROMPT_TO_PLAY.md) for the system boundary and evaluation evidence. The six recorded metrics are scene similarity, structural correctness, automation-loop completeness, generation speed, Token efficiency, and reproducibility.

CPU-only execution is sufficient for contract validation, compilation, headless structural checks, and basic compatibility rendering. A GPU is useful for faster high-quality screenshots, video, or local generative models, but it is not required for this scaffold.

## Source layout

- `prompts/prompt-to-play.md` — Agent runtime protocol and stopping rules.
- `prompt_to_play/contracts.py` — validation, canonical hashes, policy scoring, and safe patch application.
- `prompt_to_play/lifecycle.py` — canonical request creation and deterministic best-revision selection.
- `prompt_to_play/provider.py` — Codex CLI and OpenAI-compatible structured-output adapters.
- `prompt_to_play/planner.py` — arbitrary prompt/reference planning into a validated WorldSpec.
- `prompt_to_play/launcher.py` — responsive Tk desktop UI and stage runner.
- `prompt_to_play/pipeline.py` — concrete planning, publishing, build, structural-check, and launch stages.
- `prompt_to_play/evaluation_policy.json` — internal evaluation configuration.
- `prompt_to_play/godot_template/` — reusable data-driven Godot project.
- `engines/godot.md` — Godot generation, verification, and capture guidance.
- `publish.py` / `publish.sh` — cross-platform project publisher.
- `tests/` — contract, generality, patch-safety, and publishing tests.

## Upstream and license

This work remains a fork of Godogen and preserves its multi-engine autonomous workflow. Use `--workflow autonomous` (the default) for the original thin publisher behavior. See [LICENSE.md](LICENSE.md) and the upstream project for attribution and licensing details.
