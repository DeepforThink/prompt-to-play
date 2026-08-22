# Upstream Godogen Notes

Prompt-to-Play began from the open-source [Godogen](https://github.com/htdt/godogen) project. Godogen's original source repository publishes lightweight runtime instructions for multiple engines and coding agents.

This repository now supports a different primary product path: direct Godot project generation from a raw Prompt and optional reference images, followed by host-controlled build, interaction, visual evaluation, repair, and best-revision selection.

The active architecture is documented in:

- [Project README](../README.md)
- [Prompt-to-Play Direct Workflow](PROMPT_TO_PLAY.md)
- [Tested Demo Prompts](demo_prompts.md)

The older publisher, multi-engine guides, asset-generation utilities, and schema-driven experiments remain in the source tree as implementation history and upstream reference. They are not used by the supported <code>python -m prompt_to_play.direct_pipeline</code> entry point.

For the original autonomous publishing architecture, refer to the upstream Godogen repository and its history. Upstream attribution and the MIT license are preserved.
