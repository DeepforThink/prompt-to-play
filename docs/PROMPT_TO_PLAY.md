# Prompt-to-Play workflow

Prompt-to-Play is the Godot-specific, spec-driven workflow in this Godogen fork.
It turns a natural-language description and optional reference images into a
playable, explorable 3D world, then evaluates and selectively corrects that
world. The floating mechanical city is a test fixture, not a built-in scene.

## External interface

The user supplies only:

1. a natural-language world description; and
2. optional reference images.

The runtime derives the request hash and procedural seed. Generation time,
model calls, token usage, scores, and reproducibility hashes are observations
written after the run. Evaluation weights, score thresholds, and the correction
limit belong to the versioned evaluation policy; they are not user inputs.

## Pipeline

```text
prompt + references
        |
        v
WorldPlannerAgent (structured model API)
        |
        v
validated WorldSpec
        |
        v
Host refinement DAG
  |-- layout Subagent ---------|
  |-- gameplay Subagent -------|--> owned PatchSpec merge --> repeat to convergence
  +-- lighting/camera Subagent-|
        |
        v
bounded Asset Subagents -> image API -> Tripo3D API -> content-addressed GLB cache
        |
        v
Godot BuildExecutor (catalog assets first, primitive last fallback)
        |
        v
structural checks -> fixed-camera capture -> VisualEvaluationAgent
                                               |
                                  per-camera observations
                                               |
                                  host score and acceptance gate
                                     | pass              | fail
                                     v                   v
                           best-revision selection   RepairAgent
                                     |                   |
                                     |        host derives PatchSpec
                                     |                   |
                                     +<------ assets/build/capture
```

The four model roles have isolated contexts, role-specific model settings,
strict JSON schemas, and trace entries. After WorldPlanner returns one complete
WorldSpec, the host derives a fixed three-task DAG; the model cannot amplify the
task count. Each refinement worker receives an independent provider instance
and an explicit kind/field ownership set. Workers return complete candidates,
while the host derives PatchSpec operations, rejects stale or unauthorized
writes, and repeats until all workers return no change or the bounded iteration
limit is reached. AssetAgent uses separate bounded workers for unique prefabs
and writes one host-ordered asset manifest. The visual evaluator cannot edit the world or
decide acceptance, Token, timing, or aggregate metrics. It scores every capture
camera across the six visual dimensions; the host combines the mean and worst
camera and derives `scene_similarity` and `accepted`. RepairAgent returns a complete
candidate WorldSpec; the host validates it and deterministically derives only
allowlisted stable-ID PatchSpec operations. A stale patch hash is rejected. The
workflow keeps the best revision and permits at most two correction rounds
after the initial build. Launch requires the selected revision to pass all
gates; when every revision fails, evidence and selection are retained and the
pipeline stops before launch.

## Generality boundary

The compiler supports different themes, layouts, region counts, roads,
buildings, props, lights, cameras, and objective graphs. Interaction verbs are
mapped onto a small reusable runtime vocabulary:

- `collect`
- `activate`
- `repair`
- `inspect`
- a final reachable completion area

Objectives combine interactables with `all`, `any`, or `sequence`. Known
logical prefab IDs are sent through AssetAgent. It first reuses the
content-addressed cache and, when the host has explicitly enabled paid API
generation, creates a reference image and converts it to a PBR GLB. Godot loads
the strict catalog entry, while unresolved IDs receive a deterministic
primitive fallback so a new prompt still produces a playable world instead of
failing on a missing model. Its paid-attempt limit is shared by the full run,
not reset for each correction revision. Up to four unique-prefab workers run in
parallel, while budget reservation and final manifest ordering remain host-owned.

Regions also drive a deterministic PCG decoration pass. Semantic region and
theme tokens select vegetation, ruin, industrial, or generic primitive
grammars; region area controls bounded density; and every derived item receives
a child seed and stable manifest ID. Authored or generated prefabs can replace
these fallbacks later without changing the interaction contract.

This workflow targets prompt-conditioned, explorable 3D worlds with lightweight
objectives. It does not claim to synthesize an arbitrary game genre or perform
photorealistic multi-view 3D reconstruction. Reference images constrain style,
palette, landmarks, and spatial relationships.

## Evaluation evidence

The six course metrics are represented directly:

| Metric | Evidence |
| --- | --- |
| Scene similarity | Fixed-camera images compared with the prompt/references |
| Structural correctness | Load, graph, collision, navigation, and objective checks |
| Automation loop | Versioned plan, evaluation, patch, rebuild, and selection records |
| Generation speed | Measured stage and total elapsed time |
| Token efficiency | Recorded model calls and input/output token counts |
| Reproducibility | Exact Godot source hash on the first run; exact WorldSpec comparison with prior runs of the same request thereafter |

Each run retains its request, the Planner baseline, every refinement round and
task outcome, every WorldSpec revision, asset catalog and rich
asset manifest, build manifest, structural report, visual feedback, scored
evaluation, patches, screenshots, best-revision selection, elapsed time, and
per-agent model/Token trace. A good-looking screenshot cannot override a failed
structural hard gate. Original reference images are attached before generated
screenshots for both visual evaluation and repair, with explicit image counts
so the evaluator can compare against the actual references rather than path
names alone.

Operational `PTP_RUN_ID` and `PTP_REVISION` values only select immutable
evidence directories; revision is restricted to the initial build plus two
corrections. `PTP_CAPTURE=1` is likewise an execution switch rather than a
semantic input. Capture mode visits every declared camera, rejects empty or
near-uniform frames, and records resolution, renderer, and image hashes.

## Demonstrating that the compiler is not hardcoded

The acceptance suite uses at least two semantically and visually different
WorldSpecs, such as a floating mechanical city and a foggy forest ruin. Both
must pass through the same compiler and interaction runtime. The same request
and references must derive the same seed and stable manifest, while changed
prompts must produce meaningfully different layouts and style parameters.
