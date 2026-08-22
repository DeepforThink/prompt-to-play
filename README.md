# Prompt-to-Play on Godogen

This fork turns an arbitrary natural-language prompt and optional reference
images directly into a playable Godot project. The model writes real Godot
scenes, scripts, resources, and shaders below `generated/`; there is no
WorldSpec or other fixed scene vocabulary between the prompt and the game.

The pipeline is therefore not tied to the mechanical-city example, a fixed
number of regions, or one gameplay genre. A new test prompt can require a
different setting, layout, camera, rules, controls, objectives, or visual
style, and the ProjectGeneratorAgent implements those requirements in Godot
code and scene files.

## Architecture

```text
prompt + optional reference images
        -> ProjectGeneratorAgent emits generated/**
        -> strict path/content safety checks
        -> Godot .NET build + headless structural check
        -> rendered screenshots
        -> VisualEvaluationAgent compares prompt/references/renders
        -> CodeRepairAgent emits a generated/** file patch
        -> rebuild, reevaluate, and select the best revision
        -> launch the selected playable project
```

The model-owned and trusted portions are intentionally separated:

- `generated/**` is model-owned. The model must create
  `generated/GeneratedGame.tscn` and may add C#, GDScript, scenes, resources,
  shaders, JSON, or Markdown inside that directory.
- `project.godot`, the `.csproj`, and `harness/**` are repository-owned. The
  model cannot replace them. The harness loads the generated entry scene,
  synthesizes declared input actions, verifies observable player-state changes,
  captures one or two evaluation views, and writes evidence bound to the
  generated-project hash.
- Before a model response changes disk, the host rejects traversal, absolute
  paths, unsupported extensions, oversized packages, links/junctions, and
  generated code that attempts process, network, host-filesystem, environment,
  reflection, native-interop, or unsafe access.

The ProjectGeneratorAgent, VisualEvaluationAgent, and CodeRepairAgent use
separate model calls and auditable traces. Build errors are repair evidence as
well as visual feedback, so a correction can fix compilation, scene loading,
gameplay, or presentation. The visual Agent scores prompt fidelity,
composition, coherence, detail, lighting/materials, and gameplay readability;
the host owns the weighted acceptance gate. Earlier revisions remain immutable,
and an accepted revision always outranks a merely high-scoring rejected one.
The default loop permits four repair rounds (hard internal ceiling: six). If no
revision passes, the best project and evidence remain on disk but are not
reported or launched as a completed game.

## Requirements

- Windows with PowerShell for the masked convenience launcher
- Python 3.11 or newer
- .NET 8 SDK
- Godot 4.7.x .NET (console and rendered executables discoverable by the
  launcher)
- An API key for an OpenAI Responses-compatible endpoint

The runtime does **not** require the Codex CLI. CPU-only machines can run
generation, compilation, and structural checks; a GPU materially improves
rendering speed and visual evaluation but is not required for the pipeline.

## Run it

The recommended Windows entry point asks for the API key using masked local
input:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_prompt_to_play_api.ps1
```

Defaults are the Micu endpoint and `gpt-5.6-sol`. Other supported forms are:

```powershell
# Official OpenAI endpoint
powershell -ExecutionPolicy Bypass -File scripts/start_prompt_to_play_api.ps1 `
  -ApiProvider openai -Model <model-id>

# Another Responses-compatible endpoint
powershell -ExecutionPolicy Bypass -File scripts/start_prompt_to_play_api.ps1 `
  -ApiProvider custom -BaseUrl https://example.com/v1 -Model <model-id>
```

The script passes the key only through the launched process environment and
clears its own copy after startup. It does not put credentials in source files,
Git, logs, or command-line arguments. Do not paste API keys into this repository
or chat messages.

The Micu launcher uses its documented Codex-style Responses subset and a
10-minute per-call timeout because a complete Godot source package can take
several minutes to generate. Returned JSON is still validated locally against
the strict file, path, size, and host-capability contract before any file is
written.

If the environment is already configured, start the same GUI directly:

```powershell
$env:PROMPT_TO_PLAY_PROVIDER = "http"
$env:PROMPT_TO_PLAY_API_STYLE = "responses"
$env:PROMPT_TO_PLAY_BASE_URL = "https://www.micuapi.ai/v1"
$env:PROMPT_TO_PLAY_MODEL = "gpt-5.6-sol"
# Set PROMPT_TO_PLAY_API_KEY only in the current process, then:
python -m prompt_to_play.direct_pipeline
```

In the desktop launcher, enter any game prompt, optionally add reference
images, and choose `生成并启动`. Seed is derived internally. Time, Token use,
scores, and output hashes are recorded evaluation results rather than required
input fields.

## Generated output and replay

Each generation has a unique project directory under:

```text
../output/generated/<request-hash>/run-<id>/
```

Closing the game does not delete it. The directory contains a normal editable
Godot .NET project, the selected `generated/**` source, and run evidence under
`artifacts/runs/<run-id>/rev_<n>/`. Open its `project.godot` in Godot or run it
again without another model call. A teammate can play or edit a copied
generated project without an API key; an API key is needed only to generate or
repair a different game.

Evidence includes the original request and reference hashes, direct-file
manifests, compiler and structural results, screenshot manifests, visual
feedback, model timing/Token traces, and final revision selection. It does not
contain API credentials.

## Test

```powershell
python -m pytest -q
python -m py_compile start_prompt_to_play.py `
  prompt_to_play/direct_generation.py `
  prompt_to_play/direct_agents.py `
  prompt_to_play/direct_pipeline.py
```

The strongest demonstration is to run two semantically different prompts
through the same launcher, show that both produce playable projects, and then
show one evidence-backed repair iteration. The six course measurements remain
scene similarity, structural correctness, automation-loop completeness,
generation speed, Token efficiency, and reproducibility.

## Source layout

- `prompt_to_play/direct_generation.py` — strict direct-file schema, code/path
  safety checks, atomic patch application, and content-addressed manifests.
- `prompt_to_play/direct_common.py` and `direct_host.py` — request hashing,
  evidence serialization, toolchain discovery, and trusted process boundaries.
- `prompt_to_play/direct_evaluation.py` — direct-project visual feedback schema
  and host-owned acceptance gate.
- `prompt_to_play/direct_agents.py` — ProjectGenerator, visual-evaluation, and
  code-repair model roles.
- `prompt_to_play/direct_pipeline.py` — generation, build, verification,
  feedback, revision selection, and launch orchestration.
- `prompt_to_play/direct_template/` — trusted Godot host and generated entry
  boundary.
- `prompt_to_play/launcher.py` — responsive Tk desktop UI and stage runner.
- `scripts/start_prompt_to_play_api.ps1` — masked API launcher.
- `tests/` — safety, contract, Agent, template, and integration tests.

The older schema-driven modules remain only as archived implementation
reference for previous experiments. They are not imported by the direct
runtime, installed by the Prompt-to-Play publisher, or supported as a startup
path.

## Upstream and license

This remains a fork of [Godogen](https://github.com/htdt/godogen). See
[LICENSE.md](LICENSE.md) and the upstream project for attribution and licensing
details.
