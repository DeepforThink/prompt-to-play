# Prompt-to-Play on Godogen

This fork adds a spec-driven Godot workflow to [Godogen](https://github.com/htdt/godogen): a user gives a natural-language world description and optional reference images, an Agent plans a validated world, and the same deterministic compiler turns different plans into explorable 3D scenes with lightweight objectives.

The floating mechanical city in `prompt_to_play/examples/` is only one acceptance fixture. It is not embedded in the compiler. A second foggy-forest fixture exercises the same generation and interaction code with a different theme, topology, entity set, and objective graph.

## What is implemented

- A versioned, engine-independent `WorldSpec`, `PatchSpec`, evaluation policy, and validator.
- A reusable Godot 4.7 C# scaffold that builds regions, roads, buildings, props, lights, fixed cameras, generic interactables, objectives, and a reachable exit from data.
- A desktop launcher where the player enters an arbitrary prompt and optional reference images, then watches Plan -> Validate -> Publish -> Build -> Play run off the UI thread.
- An API-first multi-agent runtime with isolated WorldPlanner, WorldRefinement, VisualEvaluation, and Repair roles, including per-task provider instances, strict JSON hand-offs, hashes, timing, model, and Token traces.
- A host-derived refinement DAG that repeatedly fans out layout, gameplay, and lighting/camera Subagents, merges only owned stable-ID updates, and stops on convergence or a bounded iteration cap.
- Bounded AssetAgent workers that resolve unique semantic prefabs concurrently into cached API-generated GLBs, followed by host-ordered catalog publication and deterministic primitive fallback when an asset is unresolved.
- A Godot PrefabResolver that loads real `.glb`/`.gltf` scenes, enables shadows and collision, and records whether each entity used a catalog asset or fallback geometry.
- WASD and mouse exploration, Space to jump, E to interact, contextual prompts, objective progress, and completion feedback.
- Machine-readable build and structural evidence for `scene_loads`, `world_graph_connected`, `objectives_completable`, and `completion_reachable`.
- An implemented screenshot feedback loop: VisualEvaluationAgent returns validated entity/field-level guidance, the host routes it to isolated layout, gameplay, and lighting/camera Repair Subagents, merges only owned stable-ID updates, and performs at most two correction rounds before selecting the best revision.
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

On Windows, the complete path with realistic API-generated assets is the masked
launcher (it prompts locally for the three required credentials):

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_prompt_to_play_api.ps1 -ImageProvider gemini
```

The launcher defaults to Micu's OpenAI Responses-compatible endpoint and all
model roles use `gpt-5.6-sol`; override the model with `-Model <model-id>`.
Use `-ApiProvider openai` only when the official OpenAI endpoint is intended.
To run with only the model API and accept primitive asset fallbacks, add
`-AssetMode off`; that mode prompts for only the selected provider's key.

Enter any world description, optionally add reference images, and choose
`生成并启动`. The launcher performs structured planning, contract validation,
publishing, .NET compilation, a headless Godot structural check, and only then
opens the playable game. Every run gets its own project under
`../output/generated/<request-hash>/run-<id>/`, so regenerating the same prompt
cannot overwrite a game that is still open. The example worlds are never used
as runtime fallbacks.

If the required environment variables are already configured, the GUI can also
be started with `python -m prompt_to_play.pipeline`. The interactive multi-agent runtime is API-first and requires
`PROMPT_TO_PLAY_API_KEY` (or `OPENAI_API_KEY`). It sends selected reference
images and generated screenshots as real image inputs. A signed-in Codex CLI is
kept only as an explicitly selected development fallback by setting
`PROMPT_TO_PLAY_PROVIDER=codex`.

For the complete realistic-asset path, configure an image provider key
(`GEMINI_API_KEY`/`GOOGLE_API_KEY` or `XAI_API_KEY`) and
`TRIPO3D_API_KEY`, then set `PROMPT_TO_PLAY_ASSET_MODE=auto`. Asset generation
is paid and therefore defaults to `off`; cache hits remain available in every
mode. The Windows launcher above does not write keys to disk, logs,
command-line arguments, or Git.

For Micu, the launcher fixes the base URL to `https://www.micuapi.ai/v1`, uses
the Responses protocol, and supplies the Codex-style User-Agent required by its
compatibility gateway. Direct environment-based startup can configure the same
header with `PROMPT_TO_PLAY_USER_AGENT`.

Do not paste API keys into source files or chat messages. The script places
them only in the launched process environment and removes its own copies after
startup.

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
        -> WorldPlanner produces one complete WorldSpec
        -> Host repeatedly fans out bounded refinement tasks until convergence
        -> Execute deterministic Godot compiler
        -> Evaluate structure and fixed-camera evidence
        -> Visual Agent emits per-camera observations and entity/field guidance
        -> Host scores, fans out three Repair Subagents, and merges valid patches
        -> Rebuild and select a passing or structurally valid best-effort revision
```

The run root records `refinement.json` with the Planner hash, every refinement
round, task ownership, status, duration, candidate/patch hashes, merged patch,
and convergence reason. `agent_trace.json` identifies every model call by role,
instance, and task. Headless execution writes the build manifest and structural report to
`artifacts/runs/<run-id>/rev_<n>/`. Capture mode visits every WorldSpec camera,
rejects empty or near-uniform frames, writes PNGs, and records their SHA-256
digests in `capture_manifest.json`. Each correction source revision records
`repair_to_rev_<n>.json` with detailed visual guidance, task ownership/status,
candidate and patch hashes, rejected tasks, and the merged patch. Set
`PTP_REVISION` to `1` or `2` only after a validated merged patch; earlier
evidence remains untouched. A generated
project launches when the selected revision passes every structural delivery
gate. A fully passing evaluation is preferred; otherwise `delivery.json` marks
the highest-ranked structurally valid revision as `best_effort` and retains its
visual failures, score gap, detailed guidance, remaining issues, and
correction-stop evidence.

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
- `prompt_to_play/agents.py` — isolated API-agent sessions, role-specific models, Token accounting, and auditable traces.
- `prompt_to_play/planner.py` — arbitrary prompt/reference planning into a validated WorldSpec.
- `prompt_to_play/refinement.py` — fixed refinement DAG, ownership enforcement, convergence, and iteration records.
- `prompt_to_play/assets.py` — AssetAgent requests, paid API opt-in, content-addressed GLB cache, catalog, and manifest.
- `prompt_to_play/evaluator.py` — VisualEvaluationAgent guidance contracts, bounded Repair worker, and deterministic WorldSpec-to-PatchSpec diff.
- `prompt_to_play/repair.py` — fixed Repair Subagent fan-out, ownership validation, deterministic merge, and round evidence.
- `prompt_to_play/launcher.py` — responsive Tk desktop UI and stage runner.
- `prompt_to_play/pipeline.py` — concrete planning, publishing, build, structural-check, and launch stages.
- `prompt_to_play/evaluation_policy.json` — internal evaluation configuration.
- `prompt_to_play/godot_template/` — reusable data-driven Godot project.
- `engines/godot.md` — Godot generation, verification, and capture guidance.
- `publish.py` / `publish.sh` — cross-platform project publisher.
- `tests/` — contract, generality, patch-safety, and publishing tests.

## Upstream and license

This work remains a fork of Godogen and preserves its multi-engine autonomous workflow. Use `--workflow autonomous` (the default) for the original thin publisher behavior. See [LICENSE.md](LICENSE.md) and the upstream project for attribution and licensing details.
