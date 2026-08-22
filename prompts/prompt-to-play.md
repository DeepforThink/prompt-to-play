# Build a Prompt-to-Play ${ENGINE_NAME} game

Turn the user's natural-language description and optional reference images
directly into a reproducible, playable Godot game. Follow
`${ENGINE_GUIDE_FILE}` for engine-specific build and capture details.

## Public input boundary

Accept only:

1. one natural-language game description; and
2. zero or more optional reference images.

Do not ask for a seed, time budget, Token budget, score weights, score
threshold, or correction count. Derive the seed from the canonical request.
Record timing, model calls, Token usage, scores, and output hashes after
execution. Load thresholds and the maximum correction count from system policy.

Treat each prompt independently. Never assume a mechanical city, three
regions, fixed entity counts, particular controls, or one gameplay genre. The
current request determines scene type, visual style, topology, camera,
mechanics, interactions, UI, win/lose states, and controls.

## Direct source-of-truth boundary

Implement the game directly as model-owned Godot files below `generated/`.
There is no WorldSpec or fixed semantic scene representation. The required
entry is `generated/GeneratedGame.tscn`; it may reference additional `.cs`,
`.gd`, `.tscn`, `.tres`, `.gdshader`, `.json`, or `.md` files below the same
directory.

Return only the strict `prompt-to-play/direct-files@1` JSON document expected by
the host. Do not return Markdown or an architectural description in place of
working files. Use Godot 4.7.x APIs and .NET 8 when writing C#.

`project.godot`, the `.csproj`, and `harness/**` are trusted host files. Never
attempt to modify or replace them. Do not add packages, invoke shell commands,
read environment variables, access the host filesystem or network, load native
code, use reflection, or circumvent the generated-directory boundary.

The generated entry must:

- instantiate playable content and add at least one node to `ptp_gameplay`;
- contain clearly visible 2D or 3D content rather than an empty scene;
- provide one or two current `Camera2D`/`Camera3D` nodes in the
  `ptp_capture_camera` group; and
- expose the controlled node, goal/progression, and HUD through `ptp_player`,
  `ptp_objective`, and `ptp_hud`;
- put a stateful node in `ptp_interaction_probe`, declare existing InputMap
  actions in its `ptp_probe_actions` metadata, and respond observably when the
  trusted host synthesizes those actions; and
- implement the prompt's actual mechanics, feedback, and completion/failure
  behavior, not merely a static visual mock-up.

Prefer coherent procedural geometry, materials, particles, lighting, shaders,
and reusable generated scenes when external assets are unavailable. Missing
optional visual detail may reduce fidelity, but must not make the game fail to
build or play.

## Mandatory generate -> verify -> evaluate -> repair loop

1. **Generate** — ProjectGeneratorAgent reads the original prompt and ordered
   reference images, then emits a complete direct-file package including the
   required entry scene.
2. **Safety check** — validate every operation, path, extension, byte limit,
   resource reference, and generated script. Apply the package atomically only
   after the entire response passes. Write a content-addressed direct manifest.
3. **Build** — compile the fixed Godot .NET project. A compiler failure is
   concrete repair evidence, never a successful result.
4. **Structural evaluation** — load the project headlessly and verify that the
   generated scene exists, instantiates, provides player/objective/HUD content,
   renders visible content, exposes valid evaluation cameras, responds to
   synthesized input, and exits verification cleanly.
5. **Capture and visual evaluation** — after a valid build, render one or two
   stable views. VisualEvaluationAgent compares them with the original prompt
   and optional references, independently scoring prompt fidelity, composition,
   coherence, detail density, lighting/materials, and gameplay readability. The
   host derives the weighted overall score and acceptance decision.
6. **Repair** — CodeRepairAgent receives the original prompt, bounded current
   `generated/**` source, compiler diagnostics, structural report, visual
   feedback, and relevant images. It returns only a safe direct-file patch.
   Fix build and scene-loading errors before gameplay and presentation issues.
7. **Repeat and select** — validate, rebuild, and reevaluate each patch up to the
   system correction limit (four repairs by default, hard ceiling six), stopping
   early only when every gate passes. Accepted revisions outrank rejected ones.
   Restore and rebuild the selected accepted revision before launch; if no
   revision passes, preserve evidence but do not claim or launch completion.

The evaluator observes and reports; it never edits files. The repair Agent
changes only `generated/**`. Never accept a response merely because JSON parsing
succeeded—the built and loaded Godot project is the product.

## Immutable evidence and delivery

Keep evidence under `artifacts/runs/<run_id>/rev_<n>/` without overwriting an
earlier revision. Record at least:

- canonical request and ordered reference hashes;
- direct-file application manifest and generated-project SHA-256;
- compiler log and structural report;
- capture manifest and screenshot hashes when rendering succeeds;
- visual feedback, correction decision, Agent timing/Token trace, and selection
  record.

The final game must load the selected files saved on disk, not unsaved editor
state. Preserve the complete project under
`../output/generated/<request-hash>/run-<id>/` so closing the game does not
delete it and teammates can reopen or edit it without an API key.

Before delivery, run at least two semantically different prompts through the
same direct pipeline, rerun one unchanged request to compare reproducibility
evidence, and exercise one real evaluation-to-code-patch-to-reevaluation
transition. Preserve the resulting build, interaction, visual, timing,
model-usage, and reproducibility evidence in the run artifacts.
