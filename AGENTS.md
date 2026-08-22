# Prompt-to-Play Source Repository

This repository contains the active Prompt-to-Play direct Godot generation pipeline. It is derived from Godogen, but the supported product path is the desktop Prompt/optional-reference input followed by direct model-authored Godot files and an evidence-backed correction loop.

## Active Runtime

- <code>prompt_to_play/direct_pipeline.py</code> orchestrates generation, build, structural checks, captures, evaluation, repair, selection, and launch.
- <code>prompt_to_play/direct_agents.py</code> defines the ProjectGenerator, VisualEvaluation, and CodeRepair roles.
- <code>prompt_to_play/direct_generation.py</code> owns the strict direct-file contract and atomic patch application.
- <code>prompt_to_play/direct_template/</code> is the trusted Godot host. Model-authored content is restricted to <code>generated/**</code>.
- <code>scripts/start_prompt_to_play_api.ps1</code> is the supported masked Windows launcher.
- <code>README.md</code> is the public project page; <code>docs/PROMPT_TO_PLAY.md</code> is the detailed protocol.

## Editing Rules

- Do not reintroduce WorldSpec, a fixed entity vocabulary, or scene-specific generation assumptions into the direct runtime.
- Preserve the trust boundary: repository-owned host files are not model-writable; model changes remain below <code>generated/**</code>.
- Treat build, structural, interaction, capture, and visual results as evidence. The host, not a model response, owns acceptance.
- Never commit API keys, environment files, generated output directories, run artifacts, local toolchains, caches, or private reference images.
- Keep repository media small and web-friendly. Curated demo assets belong in <code>media/demos/</code>.
- Run the complete test suite after contract, Agent, pipeline, launcher, or template changes.
- Preserve upstream attribution and the MIT license.

Older publishing, multi-engine, and schema-driven modules remain historical reference unless a task explicitly targets them. Do not route the supported launcher back through those modules.
