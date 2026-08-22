# Direct Godot template

This is the stable host for a Godot 4.7.1 .NET project authored directly by
the model. It starts without reading a scene-description schema.

## Stable files

- `project.godot` — engine settings and `res://harness/Main.tscn` entry point.
- `PromptToPlayDirect.csproj` — Godot .NET 4.7.1 / .NET 8 build definition.
- `.gitignore` — excludes Godot/.NET caches and generated run evidence.
- `harness/Main.tscn` — stable bootstrap scene.
- `harness/DirectHarness.cs` — loads the generated entry, checks it, captures it,
  and preserves a useful exit code.
- `harness/DirectArtifactWriter.cs` — writes machine-readable artifacts under
  `artifacts/runs/<run-id>/rev_<n>/`.
- `harness/DirectInteractionProbe.cs` — injects declared InputMap actions and
  verifies that trusted observable node state actually changes.

## Model-owned files

- `generated/GeneratedGame.cs` — primary implementation target.
- `generated/GeneratedGame.tscn` — stable generated entry scene; it may be
  replaced when a different root type or resource graph is needed.
- Additional `.cs`, `.gd`, `.tscn`, `.tres`, `.gdshader`, `.json`, and `.md`
  files may be added below `generated/`. User-supplied reference images are
  copied by the host below `references/` and may be used through their verified
  `res://references/...` paths.

The generated entry must add at least one node to `ptp_gameplay`, visible 2D or
3D content, and one or two `Camera2D`/`Camera3D` nodes to
`ptp_capture_camera`. A camera can set string metadata named `capture_id`;
capture IDs are sanitized before they become filenames. UI that should not
appear in evaluation screenshots can join `ptp_capture_hidden`.

Declare at least one player/controller node in `ptp_player`, one win/lose or
goal node in `ptp_objective`, and one player-facing UI node in `ptp_hud`. To
make gameplay verifiable without imposing a genre, also put each state-bearing
player/controller node to test in `ptp_interaction_probe` and set its
`ptp_probe_actions` metadata to a comma-separated list of InputMap action names.
The trusted host presses at most eight declared actions and observes standard
2D/3D transforms, physics velocity, Control geometry, Range values, Label text,
or optional primitive `ptp_probe_state` metadata. The host writes both
`player_declared`, `objective_declared`, `hud_declared`, and
`interaction_responds_to_input` checks plus hashed before/after evidence;
generated code does not write its own verdict.

## Automation

Run with `PTP_AUTOMATION=1` to write `direct_structural_report.json` and exit.
`PTP_VERIFY=1` is accepted as an alias.
Run with `PTP_CAPTURE=1` under a rendering display driver to additionally write
one or two PNG files and `capture_manifest.json`. Optional `PTP_RUN_ID` and
`PTP_REVISION` select the artifact folder. `PTP_PROJECT_SHA256` binds reports
and captures to the exact generated project snapshot. Unsafe values are never
used as paths or evidence identifiers.
