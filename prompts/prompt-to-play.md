# Build a Prompt-to-Play ${ENGINE_NAME} world

Turn the user's natural-language world description and optional reference images into a reproducible, explorable 3D world. Follow `${ENGINE_GUIDE_FILE}` for engine-specific implementation and capture details.

## Public input boundary

Accept only:

1. one natural-language description; and
2. zero or more optional reference images.

Do not ask the user for a seed, time budget, token budget, score weights, score threshold, or correction count. Derive the seed from a canonical hash of the request. Treat timing, model calls, token usage, scores, and output hashes as observations recorded after execution. Load weights, thresholds, normalization references, and the maximum correction count from `prompt_to_play/evaluation_policy.json`.

The example WorldSpecs are acceptance fixtures, not templates to copy semantically. Never assume a mechanical city, three regions, three cores, a particular palette, or fixed entity counts. Infer theme, topology, counts, transforms, props, lighting, cameras, interactions, objectives, and completion rules from the current request.

## Supported product boundary

Generate prompt-conditioned, explorable 3D worlds with lightweight reusable interactions. The runtime vocabulary is `collect`, `activate`, `repair`, and `inspect`; objectives combine targets with `all`, `any`, or `sequence`; a reachable exit provides explicit completion. This workflow does not promise arbitrary game genres or photorealistic multi-view reconstruction. Reference images constrain style, palette, landmarks, and spatial relationships.

Unknown logical prefab IDs must receive a deterministic primitive fallback. Missing optional generated assets must reduce visual fidelity, not prevent a playable result. Ask before making a paid asset-generation call through `${ASSET_SKILL_COMMAND}`.

## Source of truth and artifacts

Represent the plan as a validated `WorldSpec`; do not build directly from prose. Use stable IDs and repository-relative logical prefab IDs. Validate with `prompt_to_play/contracts.py` before opening the engine.

Keep immutable evidence under `artifacts/runs/<run_id>/rev_<n>/`:

- `request.json`: prompt, reference descriptors and hashes, request hash, and derived seed;
- `world_spec.json`: the validated semantic source of truth;
- `build_manifest.json`: spec hash, seed, stable IDs, resolved transforms/prefabs, tool versions, timings, warnings, and errors;
- `structural_report.json`: hard checks and machine-readable issues;
- `evaluation.json`: the six rubric scores, weighted score, measurements, and evidence paths;
- `patch.json`: a validated allowlisted patch against an exact base spec hash, when correction is attempted;
- `captures/`: fixed-camera screenshots and the final proof recording when capture is available.

Never overwrite an earlier revision. The delivered project must load the selected on-disk revision, not unsaved editor state.

## Mandatory Plan -> Execute -> Evaluate -> Correct loop

1. **Plan** — interpret the prompt and references, derive request hash and seed, create a complete WorldSpec, then validate its schema, unique IDs, references, paths, objective graph, and completion path.
2. **Execute** — compile the WorldSpec deterministically. Generate regions, roads, buildings, props, lights, cameras, player spawn, interactables, objectives, and exit from their arrays rather than fixed names or counts. Save, reload, and verify the scene before declaring the build successful.
3. **Structural evaluation** — run headlessly and require at least `scene_loads`, `world_graph_connected`, `objectives_completable`, and `completion_reachable`. Also report duplicate IDs, dangling references, invalid transforms, missing collisions, runtime exceptions, and fallback assets. The evaluator observes; it never edits.
4. **Capture and score** — only after the structural gates pass, capture the declared fixed cameras with recorded renderer and resolution settings. Score exactly `scene_similarity`, `structural_correctness`, `automation_loop`, `generation_speed`, `token_efficiency`, and `reproducibility`. If a reference-dependent comparison is unavailable, report the limitation instead of inventing evidence. Time and tokens are measurements normalized by policy references; exceeding those references is not itself a hard failure.
5. **Correct** — give the feedback Agent only the original request, relevant reference summary, current WorldSpec, scores, hard-check results, and top evidence-backed issues. It must return one of `accept`, `patch`, `rollback`, or `stop`. A `patch` decision must contain only operations allowed by `contracts.py`, address stable IDs, and match `base_spec_hash`; arbitrary code, paths, shaders, or shell commands are forbidden.
6. **Repeat and select** — validate every patch, rebuild from the resulting complete WorldSpec, and reevaluate. Permit at most the policy's two correction rounds after the initial build. A hard-pass revision always outranks a hard-fail revision; among hard-pass revisions choose the highest weighted score. Roll back when a correction regresses the best result.

## Interaction and proof

The generated game must support WASD movement, mouse look, Space to jump, and E to interact. Show context-sensitive interaction text, live objective progress, and a clear completion message generated from the current WorldSpec rather than hardcoded story text.

Before delivery, run at least two semantically different WorldSpecs through the same compiler and runtime, rerun one unchanged spec to compare deterministic hashes, and exercise one real evaluation-to-patch-to-reevaluation transition. Update the generated game's `README.md` with the selected run and revision, request/spec hashes, derived seed, commands, engine/renderer versions, measured elapsed time and model/token usage when available, scores, evidence links, fallback assets, and remaining limitations.
