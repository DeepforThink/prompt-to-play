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
planner / reference interpreter
        |
        v
validated WorldSpec
        |
        v
deterministic Godot world compiler
        |
        v
structural evaluation -> fixed-camera capture -> six-metric evaluation
        |                                      |
        | pass                                 | issues
        v                                      v
      accept                         feedback Agent -> PatchSpec
                                                   |
                                                   +----> rebuild
```

The evaluator never edits the world. The feedback Agent receives stable issue
codes, affected IDs, the relevant screenshots, and the current WorldSpec. It
may return only allowlisted, stable-ID patch operations. A stale patch hash is
rejected. The workflow keeps the best revision and permits at most two
correction rounds after the initial build.

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
logical prefab IDs may resolve to authored assets; unknown IDs receive a
deterministic primitive fallback so a new prompt still produces a playable
world instead of failing on a missing model.

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
| Reproducibility | Request, WorldSpec, seed, manifest, and transform hashes |

Each run retains its request, WorldSpec, build manifest, structural report,
evaluation, patches, screenshots, proof video, tool versions, elapsed time, and
model usage. A good-looking screenshot cannot override a failed structural hard
gate.

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
