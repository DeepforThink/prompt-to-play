# Contributing to Prompt-to-Play

Prompt-to-Play turns an arbitrary natural-language game request and optional reference images into an editable Godot project, then proves and improves the result through an evidence-backed correction loop.

## Project Priorities

Contributions should improve at least one of:

- prompt generality across different genres, cameras, mechanics, and art directions;
- build, scene, interaction, capture, or visual reliability;
- measurable output quality;
- Token, latency, or API cost efficiency without hiding quality regressions;
- safety of the model-writable <code>generated/**</code> boundary;
- reproducibility and clarity of per-revision evidence.

Avoid fixed WorldSpec vocabularies, scene-specific shortcuts, or changes that let a model bypass host-owned acceptance gates.

## Before Opening a Pull Request

Open an issue for substantial architecture, contract, provider, or workflow changes. Include:

- the problem and a minimal reproduction;
- current compiler, structural, capture, or visual evidence;
- the proposed behavior;
- expected effects on quality, latency, Token use, and compatibility.

Small documentation and narrowly scoped bug fixes can go directly to a pull request.

## Development Requirements

Run:

~~~powershell
python -m pytest -q
python -m compileall -q prompt_to_play scripts tests
ruff check prompt_to_play scripts tests
~~~

Changes to the direct-file contract, Agent prompts, evaluation gate, trusted template, launcher, or selection logic should include focused tests. Pipeline changes should also include an end-to-end result or sanitized evidence summary.

## Security and Repository Hygiene

- Never commit API keys, <code>.env</code> files, personal paths, private reference images, generated projects, or run artifacts.
- Keep model writes below <code>generated/**</code>; do not expand capabilities without explicit tests and threat analysis.
- Use the masked launcher for credentials.
- Keep showcase media compressed, curated, and suitable for repository browsing.
- Preserve upstream attribution and the MIT license.

## Pull Request Scope

Keep changes focused and explain how they improve the supported direct Godot pipeline. Historical Godogen publishing, Bevy, Babylon.js, and asset-generation code remains for reference; do not refactor it incidentally when changing Prompt-to-Play.
