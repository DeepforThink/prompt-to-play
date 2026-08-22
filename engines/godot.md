# Godot engine guide

Stack: **Godot 4.7.1 .NET**, **.NET 8**, and prompt-specific **C# and/or
GDScript**. Every Godot C# class must be declared `partial`.

## Trusted host and generated source

The direct Prompt-to-Play project has two ownership domains.

Trusted repository files:

- `project.godot` fixes the main scene, window, renderer, and physics settings;
- `PromptToPlayDirect.csproj` pins `Godot.NET.Sdk/4.7.1`, `net8.0`, dynamic
  loading, nullable checks, assembly name, and root namespace;
- `harness/Main.tscn` and `harness/*.cs` load, inspect, capture, and report on
  the generated game.

Model-owned files live only below `generated/`. The required source of truth is
`generated/GeneratedGame.tscn`; it may reference additional generated C#,
GDScript, scenes, resources, and shaders. There is no `spec/world.json`,
`WorldRuntime`, fixed entity graph, or build-time semantic compiler.

Do not edit or shadow the trusted files from generated source. Do not add NuGet
packages. Create any prompt-specific InputMap actions synchronously in `_Ready`,
bind intuitive keys, and drive gameplay with `Input.IsActionPressed`,
`Input.GetAxis`, or `Input.GetVector`. Physical-key polling alone cannot be
synthesized by the trusted interaction probe and therefore fails automation.

Reference images are attached to the model and copied into verified
`references/**` project paths. Generated resources may use the supplied
`res://references/...` paths, but a request with no references must still build
without external texture, mesh, audio, or font files.

## Generated entry contract

The harness always loads:

```text
res://generated/GeneratedGame.tscn
```

That entry can have a `Node2D`, `Control`, or `Node3D` root. It must, within two
process frames:

- add at least one node to `ptp_gameplay`;
- expose visible content (`CanvasItem` for 2D/UI or `GeometryInstance3D` for
  3D);
- add one or two `Camera2D`/`Camera3D` nodes to `ptp_capture_camera`; and
- add the controlled node, goal/progression, and visible status UI to
  `ptp_player`, `ptp_objective`, and `ptp_hud`;
- add at least one stateful node to `ptp_interaction_probe`, set its string
  metadata `ptp_probe_actions` to comma-separated existing InputMap action
  names, and make those actions change its transform, velocity, Control/Range/
  Label state, or `ptp_probe_state` metadata; and
- make the requested mechanic playable with visible state and completion or
  failure feedback.

Set a stable string `capture_id` metadata value on each evaluation camera when
its node name is not descriptive. Put temporary debug overlays or UI that
should be absent from evidence in `ptp_capture_hidden`; the harness hides those
`CanvasItem`s before capture.

Create essential procedural content synchronously in `_Ready()`. The harness
waits two frames, not an unbounded loading screen. Long initialization should
present a valid initial game and camera before scheduling optional detail.

## Direct scene authoring

Prefer a small hand-authored `.tscn` entry that attaches one generated script,
then construct repeated geometry and gameplay nodes in code. This keeps model
output compact and avoids fragile, enormous scene text.

A minimal C# entry looks like:

```csharp
using Godot;

namespace PromptToPlay.Generated;

public partial class GeneratedGame : Node3D
{
    public override void _Ready()
    {
        AddToGroup("ptp_gameplay");
        BuildEnvironment();
        BuildPlayerAndCamera();
    }
}
```

The matching scene uses a project-relative resource path:

```ini
[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://generated/GeneratedGame.cs" id="1"]

[node name="GeneratedGame" type="Node3D"]
script = ExtResource("1")
```

Godot compiles every `.cs` file under the project, including helper files not
yet attached to a scene. A broken unused script still fails `dotnet build`.
Keep one public Godot class per file, keep filenames/class names aligned, and
avoid duplicate class names or namespaces across repair revisions.

For reusable pieces, generate another `.tscn` plus script and instantiate it
with `PackedScene`. Use `res://generated/...` paths. Never reference absolute,
`file://`, `user://`, HTTP, parent-traversal, or machine-specific locations.

The file safety gate also rejects generated code that attempts host filesystem,
process, network, environment, reflection, native-interop, or unsafe access.
Avoid even mentioning such API signatures in generated comments or strings,
because the conservative scanner intentionally checks the entire source text.
The trusted harness alone owns artifact I/O.

## C# and Godot traps

These failures often compile incompletely or appear only when the scene loads:

- Every Godot class is `public partial class ... : NodeType`; a missing
  `partial` produces a source-generator error.
- Training examples are often GDScript-biased. Verify C# enums and method
  signatures against the installed 4.7.1 assemblies. For example, mouse mode
  is `Input.MouseModeEnum.Captured`, and guessed enum suffixes are unreliable.
- Use Godot numeric types (`Vector2`, `Vector3`, `Color`, `Basis`, `Transform3D`)
  consistently; do not accidentally mix `System.Numerics` types.
- Call physics movement such as `CharacterBody3D.MoveAndSlide()` from
  `_PhysicsProcess`, scale acceleration/damping by `delta`, and use
  `speed *= Mathf.Exp(-rate * delta)` for frame-rate-independent decay.
- A visible `MeshInstance3D` is not collision. Add `StaticBody3D`/
  `CharacterBody3D` and primitive `CollisionShape3D` nodes where gameplay needs
  contact.
- Do not use imported mesh-derived trimesh/convex collision for ordinary props.
  Box, sphere, and capsule shapes are faster and more predictable.
- Procedural `ArrayMesh` geometry needs correct winding and generated normals
  to receive lighting and shadows. Disabling culling hides winding bugs but
  produces inconsistent lighting.
- Keep node references after `AddChild`, and use `IsInstanceValid` before
  accessing objects that may have been queued for deletion.
- Connect signals once. Rebuilding UI or levels without disconnecting old
  handlers causes duplicate scoring and state transitions.
- For 2D custom drawing, update state then call `QueueRedraw()`; for 3D material
  variations, duplicate mutable materials before changing per-instance values.
- Capture cameras must see a lit, non-uniform frame. A camera inside geometry,
  looking away from the scene, or rendering only the clear color fails capture
  even though the scene technically loads.

When writing `.tscn`/`.tres` text, keep `load_steps`, resource IDs, node parent
paths, and quoted values consistent. A parser error is a failed revision. Do
not attempt to hand-embed binary images or meshes into text resources; create
compact procedural visuals from supported primitives, surfaces, particles,
materials, and shaders.

## Visual quality without external assets

Primitive geometry can still read as an intentional game if it has a coherent
visual system. Prefer:

- a recognizable silhouette and focal landmark matching the prompt;
- layered forms rather than isolated cubes/cylinders;
- a small controlled palette with material roughness/metallic/emission chosen
  by function;
- directional key light, restrained fill/emission, sky/background, and fog
  where appropriate;
- decals or procedural line/shape detail, particles, and a generated shader
  used selectively;
- a camera composition that shows the mechanic and important spatial
  relationship, not only the player at ground level;
- readable UI with objective/state feedback and sufficient contrast.

For a 3D game, keep procedural instance counts bounded. Large numbers of nodes,
omni lights, transparent surfaces, particles, or collision bodies can turn a
valid generated project into an unusable one. Use `MultiMeshInstance3D` only
for simple generated meshes that do not need independent gameplay state; avoid
depending on serialization behavior of imported GLB internals.

## Build and load gates

Toolchain versions are discovered by the host. The checked-in template pins
Godot 4.7.1/.NET 8; do not silently change version-sensitive values based on
memory. Verify installations with `godot --version` and `dotnet --version`.

The pipeline restores from the discovered Godot NuGet package directory once,
then builds every candidate without restoring again:

```powershell
dotnet restore --source <godot-nupkgs> --ignore-failed-sources
dotnet build --no-restore
```

A successful C# compile is necessary but not sufficient. The host then starts
Godot headlessly from the actual project directory:

```powershell
$env:PTP_RUN_ID = "<safe-run-id>"
$env:PTP_REVISION = "0"
$env:PTP_PROJECT_SHA256 = "<64-hex-generated-source-hash>"
$env:PTP_AUTOMATION = "1"
godot --headless --path . --quit-after 120
```

This loads the saved `GeneratedGame.tscn`, instantiates its scripts, and writes
`artifacts/runs/<run-id>/rev_<n>/direct_structural_report.json`. Treat parser,
managed exception, startup exit, missing/mismatched report, or any failed hard
check as repair evidence. The host synthesizes declared actions and records
hashed before/after state, then appends `host_process_completed` only after a
clean Godot exit. Never replace a failed report with an invented pass.

Generated-file snapshots and their hashes are taken before every build. If a
later repair is worse, restore the selected snapshot and run
`dotnet build --no-restore` again before play.

## Screenshot capture

Capture requires a rendering display driver; do not pass `--headless`:

```powershell
Remove-Item Env:PTP_AUTOMATION -ErrorAction SilentlyContinue
$env:PTP_CAPTURE = "1"
godot --path . --rendering-method gl_compatibility --audio-driver Dummy
```

The trusted harness visits the one or two cameras in
`ptp_capture_camera`, waits for rendered frames, rejects empty or near-uniform
images, saves PNGs under the current revision, and writes
`capture_manifest.json` with resolution and SHA-256. The host verifies the
manifest identity, relative paths, extension, count, and image hashes before
sending screenshots to the VisualEvaluationAgent.

The compatibility renderer makes the demo portable. Hardware Vulkan or a
dedicated presentation build may improve final video quality, but optional
video must never replace the standard hashed still evidence. If making a video
after selection, use fixed FPS, scripted input, and a pre-positioned camera;
the first movie frame can render before normal per-frame logic has corrected
camera placement.

## Windows process and path hygiene

Run restore, build, structural evaluation, capture, and launch serially for one
generated project. Do not work around file contention by killing every Godot
process—the user may have another generated game open. Use unique run/revision
directories and bounded retries for transient editor or antivirus locks.

Pass paths containing spaces or non-ASCII characters as separate quoted
arguments. In PowerShell, inspect filesystem targets with `-LiteralPath`.
Keep artifact paths shallow to avoid legacy `MAX_PATH` failures. If a
third-party executable demonstrably cannot handle the workspace path, reproduce
the run from an explicitly chosen short ASCII path such as `D:\ptp\game`;
never silently move or rename the user's project.

The final visible launch clears automation/capture flags and opens the selected
on-disk project. Closing that process does not delete the source or its evidence.
