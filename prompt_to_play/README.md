# Prompt-to-Play contracts

This directory defines small, versioned JSON contracts using only the Python
standard library. They are engine-independent: Godot or another builder consumes
the resolved `WorldSpec`.

## Contract boundary

The only external user input is a prompt plus zero or more reference images:

```json
{"text": "...", "references": [{"path": "refs/a.png", "sha256": "..."}]}
```

Everything else in WorldSpec is planned by the system. In particular, `seed` is
an internal field derived deterministically from the prompt and ordered reference
image content hashes. Local filenames do not affect it. A caller cannot put
budgets or evaluation weights in WorldSpec; those are rejected as unknown keys.

`lifecycle.py create-request` hashes reference bytes, not machine-local names,
and calls the same `derive_world_seed` function used by WorldSpec validation.
Reference order is preserved because a prompt may refer to the first or second
view explicitly.

## WorldSpec

Top-level fields are exactly:

- `schema`, `world_id`, system-derived `seed`, `brief`
- `style`, `units`
- `regions`, `roads`, `buildings`, `props`, `lights`
- `interactions`, `cameras`

`style` is `{theme, palette, fog_density}`. Its palette is exactly
`{sky, ground, primary, accent, emissive}`, with uppercase `#RRGGBB` colors.

Buildings and props share
`{id, region, prefab, position, rotation_deg, scale}`. Lights are
`{id, kind, position, rotation_deg, color, energy, range}`, where kind is
`directional`, `omni`, or `spot`.

Interactions contain:

- `player_spawn`: `{region, position}`
- `interactables`: `{id, region, prefab, position, action, label, duration_ms}`;
  action is `collect`, `activate`, `repair`, or `inspect`
- `objectives`: `{id, rule, targets, completion_text}`; rule is `all`, `any`,
  or `sequence`, and targets reference interactable IDs
- `exit`: `{id, region, position, requires}`, where requires references objective
  IDs rather than individual interactables

`examples/world.json` is a mechanical city. `examples/forest_world.json` uses
the same contract for a building-free luminous forest with inspect/repair
actions and a sequence objective, demonstrating that the schema has no fixed
three-core or mechanical-city assumption.

## PatchSpec

Patch fields are exactly `schema`, `patch_id`, `base_world_id`,
`base_world_sha256`, `iteration`, `reasons`, `operations`, and
`expected_checks`. Operations are `update`, `upsert`, `remove`, or
`regenerate_region`. Targets use stable `kind + id`, never array indexes.

Patchable kinds are `region`, `road`, `building`, `prop`, `light`,
`interactable`, `objective`, `exit`, and `camera`. Each kind has an explicit
field whitelist. Applying a patch verifies its canonical base hash, works on a
deep copy, and validates every cross-reference again.

## System evaluation policy

`evaluation_policy.json` is fixed system policy, not user input. It contains:

- `max_correction_iterations: 2`
- generic `required_checks`
- the six course metric `weights` and `min_score`
- `normalization.reference_generation_ms` and
  `normalization.reference_total_tokens`

The six exact metrics are `scene_similarity`, `structural_correctness`,
`automation_loop`, `generation_speed`, `token_efficiency`, and
`reproducibility`.

Generation time and tokens are recorded in Evaluation and converted to scores:

```text
generation_speed = min(1, reference_generation_ms / max(actual_total_ms, 1))
token_efficiency = min(1, reference_total_tokens / max(actual_total_tokens, 1))
```

They are not hard user-budget failures. Evaluation passes only when every
reported hard check and every policy-required hard check passes, and the
policy-weighted score reaches `min_score`. A failing report may request another
patch only while below `max_correction_iterations`.

Evaluation fields are exactly `schema`, `run_id`, `world_id`, `world_sha256`,
`iteration`, `status`, UTC timestamps, `timing_ms`, `tokens`, `checks`,
`metrics`, `result`, `issues`, `artifacts`, and `next_action`.

## CLI

From the repository root:

```bash
python prompt_to_play/contracts.py validate-world prompt_to_play/examples/world.json
python prompt_to_play/contracts.py hash prompt_to_play/examples/world.json
python prompt_to_play/contracts.py validate-patch prompt_to_play/examples/patch.json --world prompt_to_play/examples/world.json
python prompt_to_play/contracts.py apply prompt_to_play/examples/world.json prompt_to_play/examples/patch.json generated/world.resolved.json
python prompt_to_play/contracts.py validate-eval prompt_to_play/examples/evaluation.json --world generated/world.resolved.json --policy prompt_to_play/evaluation_policy.json
python prompt_to_play/lifecycle.py create-request --prompt "a luminous forest" --reference refs/front.png --output request.json
python prompt_to_play/lifecycle.py select-best artifacts/runs/r001/rev_0/evaluation.json artifacts/runs/r001/rev_1/evaluation.json --output artifacts/runs/r001/selection.json
```

Canonical hashes use SHA-256 over UTF-8 JSON with sorted keys and compact
separators. Contract paths are normalized repository-relative paths using `/`.

The Godot runtime reads `PTP_RUN_ID` and `PTP_REVISION` as operational evidence
coordinates; revision is restricted to `0`, `1`, or `2`. It writes manifests and
structural reports under `artifacts/runs/<run-id>/rev_<n>/`. With
`PTP_CAPTURE=1` in a rendered run, it visits all declared cameras, rejects empty
or near-uniform images, and records each PNG's resolution and SHA-256 in
`capture_manifest.json`. These environment variables do not alter WorldSpec or
become user-facing generation parameters.
