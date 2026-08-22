# Prompt-to-Play direct generation workflow

Prompt-to-Play turns a natural-language game request and optional reference
images directly into an editable, playable Godot project. The model authors
Godot scenes, scripts, resources, and shaders below `generated/`. There is no
WorldSpec, fixed entity graph, or scene-specific intermediate representation.

The prompt shown in class is an example input, not a template. A hidden test may
request a different setting, camera, mechanic, objective, art direction, or
game genre; the same pipeline must generate the corresponding Godot source.

## External interface

The user supplies only:

1. a non-empty natural-language game description; and
2. zero or more optional reference images.

The runtime hashes the prompt and ordered reference bytes and derives an
internal seed. Generation time, model calls, Token usage, scores, and output
hashes are measured after execution. Seed, time/Token budgets, evaluation
weights, thresholds, and correction count are not fields the user must enter.

Every run is written to a unique directory below
`../output/generated/<request-hash>/run-<id>/`. Closing Godot does not remove
the project. The selected revision can be reopened, edited, copied, and played
without another API call.

## Architecture

```text
prompt + ordered reference images
        |
        v
ProjectGeneratorAgent (structured model API)
        |
        v
direct-files JSON: generated/** scenes/scripts/resources/shaders
        |
        v
path + extension + size + code-capability safety gate
        |
        v
trusted Godot harness + dotnet build
        |
        v
headless structure + input probe -> rendered capture -> VisualEvaluationAgent
        |                                      |
        | accepted                             | evidence-backed issues
        v                                      v
candidate revision                    CodeRepairAgent
                                               |
                                      direct-files patch
                                               |
                                               +----> validate/build/capture

all candidates -> accepted-first selection -> rebuild -> quality-gated play
```

The three model roles use isolated sessions and strict JSON outputs:

- **ProjectGeneratorAgent** converts the current raw prompt into a complete
  `generated/**` file package. It must create
  `generated/GeneratedGame.tscn` and the gameplay the prompt actually asks for.
- **VisualEvaluationAgent** sees the original prompt, reference images, and
  generated screenshots. It independently scores prompt fidelity, composition,
  visual coherence, detail density, lighting/materials, and gameplay readability
  and reports concrete issues. The host, not the model, derives the weighted
  score and acceptance decision.
- **CodeRepairAgent** receives bounded current generated source plus compiler,
  structural, capture, and visual evidence. It returns a direct file patch,
  not prose and not a replacement host project.

The default run allows up to four correction revisions, with a hard internal
ceiling of six and early exit as soon as every gate passes. An accepted revision
always outranks a rejected revision, even when the rejected model score is
higher. If a patch regresses or cycles, the host restores the best snapshot and
verifies it with another build. If the correction ceiling is exhausted without
acceptance, the project and evidence are preserved for inspection but the
pipeline does not label or launch it as completed.

## Trust boundary

| Owner | Paths | Responsibility |
| --- | --- | --- |
| Repository host | `project.godot`, `.csproj`, `harness/**` | Stable entry point, build settings, structural evidence, screenshot capture |
| Model | `generated/**` | Prompt-specific scene, mechanics, UI, code, resources, shaders |
| Host evidence writer | `artifacts/runs/<run-id>/rev_<n>/**` | Immutable manifests, logs, reports, captures, Agent trace, selection |
| Host reference publisher | `references/**` | Ordered copies of user-supplied reference images with verified hashes |

The direct-files contract permits only normalized project-relative paths below
`generated/`, an allowlist of text-based Godot/source extensions, and bounded
file and total sizes. Before writing, it rejects path traversal, absolute or
case-ambiguous paths, reserved device names, links/junctions, unsupported
resource URLs, and generated code that attempts process execution, network or
host-filesystem access, environment access, reflection, native interop, or
unsafe C#. Application is atomic and followed by a source-tree hash check.

This is a conservative static safety gate, not a claim that arbitrary generated
code is harmless. Production deployment should also use an OS-level sandbox
and least-privilege account.

## Trusted harness contract

`project.godot` always launches `res://harness/Main.tscn`. The harness loads
`res://generated/GeneratedGame.tscn`, waits two frames for generated scripts to
create their nodes, and then checks:

- the generated entry exists and instantiates;
- at least one node is in the `ptp_gameplay` group;
- visible 2D or 3D renderable content exists; and
- one or two supported cameras are in `ptp_capture_camera`;
- player, objective/progression, and visible HUD nodes are respectively in
  `ptp_player`, `ptp_objective`, and `ptp_hud`; and
- at least one `ptp_interaction_probe` node declares existing InputMap actions
  in `ptp_probe_actions`, and synthesized input changes a trusted observable
  transform, velocity, control/value/text, or `ptp_probe_state` value.

A capture camera may set `capture_id` metadata. UI or debug overlays that
should not appear in evaluation images may join `ptp_capture_hidden`.

The Python host also appends a `host_process_completed` hard check after Godot
exits; a report written before a crash or timeout cannot pass. `PTP_RUN_ID`,
`PTP_REVISION`, and `PTP_PROJECT_SHA256` bind reports to an exact
candidate. `PTP_AUTOMATION=1` runs the headless check and exits;
`PTP_CAPTURE=1` runs under a rendering display driver, saves one or two PNGs,
rejects empty/near-uniform frames, and records their hashes. These environment
variables are operational evidence coordinates, not semantic generation
inputs.

## Evidence and rubric mapping

Each revision retains the exact generated source snapshot and machine-readable
evidence. Earlier revisions are never silently overwritten.

| Course metric | Direct-pipeline evidence |
| --- | --- |
| Scene similarity | Visual Agent comparison of prompt/references with hashed capture PNGs |
| Structural correctness | Build result plus harness entry, gameplay, renderable, and camera checks |
| Automation loop | Initial file manifest, per-revision patch/evaluation, immutable snapshots, and final selection |
| Generation speed | Measured planning, publishing, restore, build, structural, capture, evaluation, and repair time |
| Token efficiency | Per-role API input/output Token counts and model-call trace |
| Reproducibility | Canonical request/seed, exact per-file and project hashes, and repeated-run comparison |

An attractive screenshot cannot override a failed build or structural gate.
Original references are attached before generated screenshots with explicit
counts, so the visual and repair Agents compare actual image content rather
than filenames.

For reproducibility, rerun the same canonical request and compare structural
outcomes, visual scores, and source/capture hashes. Exact source identity is
strong evidence when the provider is deterministic; when it is not, report the
measured variation rather than claiming bit-for-bit equality.

## Acceptance demonstration

Demonstrate at least:

1. two semantically different prompts producing two distinct playable projects
   through the same direct pipeline;
2. one real compiler, structural, or visual issue causing a CodeRepairAgent
   patch and reevaluation;
3. best-revision restoration when a later candidate is worse; and
4. a repeated request with request, generated-project, capture, timing, and
   Token evidence available for comparison.

The generated project—not a JSON plan—is the deliverable. The strongest demo
opens the selected `project.godot`, plays the requested mechanic, shows the
revision evidence, closes Godot, and reopens the same persistent project
without calling the model again.
