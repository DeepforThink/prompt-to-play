# Godot engine guide

Stack: **Godot 4 (.NET / Mono build)**, **C#**. All Godot C# classes must be `partial`.

## Project shape

- `project.godot` — config, input actions, display, physics. **Match version-sensitive fields to the installed toolchain** (`config_version`, and in `.csproj` the `Godot.NET.Sdk/...` version + `TargetFramework`) — run `godot --version` / `dotnet --version` and don't hardcode values from memory; on an existing project preserve them. For 3D, set `3d/physics_engine="Jolt Physics"` and a fixed `physics_ticks_per_second`.
- `{ProjectName}.csproj` — name must match `assembly_name`; `<EnableDynamicLoading>true</EnableDynamicLoading>`.
- `scripts/*.cs` runtime behavior · `scenes/*.tscn` scenes · `assets/` **only** files the running game loads (keep generation inputs/refs outside it).
- Build gate: `dotnet build`, then `godot --headless --import` after asset changes, then `godot --headless --quit` (RID-leak warnings on headless exit are benign).

The user watches by running the project themselves (`godot --path .` or the editor) — keep it building and importing cleanly so each run reflects current state.

## Scenes are generated at build time, not by hand

The published Prompt-to-Play scaffold starts with a deterministic runtime
compiler: `WorldRuntime.cs` validates `spec/world.json`, builds the scene tree,
and writes a stable manifest on startup. The validated on-disk WorldSpec is the
winning revision in this mode. If a run promotes that tree to a cached `.tscn`,
use the packing and reload rules below; never make the cached scene a second
semantic source of truth.

Write scenes as **C# `SceneTree` scripts** that run once headless and emit a `.tscn`: `godot --headless --script scenes/BuildX.cs`. A builder builds the node hierarchy, sets properties, attaches scripts, packs, and `Quit()`s — it contains **no** runtime logic (no `_Ready`/`_Process`, signals, or game state). Build **leaf scenes first**, parents after.

Treat a validated `world_spec.json` as the source of truth, not the generated node tree. Every region, road, building, prop, light, interactable, objective, exit, and camera gets an immutable stable `id`; copy that ID to generated nodes as metadata and use logical asset-catalog IDs instead of machine-specific paths. Write the build manifest in stable-ID order so a rerun can be compared byte-for-byte.

Seed every procedural choice from the WorldSpec. Derive an independent child seed from the root seed and stable ID with SHA-256; never use C# `string.GetHashCode()` (it is not stable across processes) or consume one shared RNG whose result changes when entity order changes:

```csharp
using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text;

static ulong DeriveSeed(ulong rootSeed, string stableId) {
    byte[] bytes = Encoding.UTF8.GetBytes($"{rootSeed}:{stableId}");
    byte[] digest = SHA256.HashData(bytes);
    return BinaryPrimitives.ReadUInt64LittleEndian(digest);
}

var rng = new RandomNumberGenerator { Seed = DeriveSeed(spec.Seed, entity.WorldId) };
```

The serialization rules below are silent-failure — they pass compilation and drop nodes or bloat files only in the saved `.tscn`:

- **Owner chain:** every node must have `Owner` set to the scene root or it won't serialize. After building, walk the tree and set `child.Owner = root` on all descendants — but **do not recurse into instantiated GLB/`.tscn` nodes** (those have a non-empty `SceneFilePath`). Recursing into a GLB inlines all its meshes as text → 100MB+ `.tscn`.
- **Validate pack and disk state:** compare node counts and the exact set of stable IDs after `Pack()`/`Instantiate()`, check the return value of `ResourceSaver.Save()`, then reload with the resource cache bypassed and repeat the checks. Only a verified on-disk scene is a successful build; an in-memory instance is not proof that saving worked.
- **`SetScript()` disposes the C# wrapper** — set scripts *last*, after the hierarchy is built. For the root, add it under a temp `Node`, set the script, then re-fetch it via `temp.GetChild(0)` before packing.

Sketch of the shared save path:

```csharp
void PackAndSave(Node root, string path) {
    SetOwnerRecursive(root, root); // Skip descendants of nodes with SceneFilePath set.
    int expectedCount = CountNodes(root);
    var expectedIds = CollectWorldIds(root); // This helper rejects duplicate IDs.

    var packed = new PackedScene();
    if (packed.Pack(root) != Error.Ok) { Fail("Pack failed"); return; }

    var memoryCopy = packed.Instantiate();
    bool memoryOk = CountNodes(memoryCopy) == expectedCount
        && CollectWorldIds(memoryCopy).SetEquals(expectedIds);
    memoryCopy.Free();
    if (!memoryOk) { Fail("PackedScene dropped or changed nodes/IDs"); return; }

    Error saveError = ResourceSaver.Save(packed, path);
    if (saveError != Error.Ok) { Fail($"Save failed: {saveError}"); return; }

    var diskScene = ResourceLoader.Load<PackedScene>(
        path, "", ResourceLoader.CacheMode.Ignore);
    if (diskScene is null) { Fail("Saved scene could not be reloaded from disk"); return; }

    var diskCopy = diskScene.Instantiate();
    bool diskOk = CountNodes(diskCopy) == expectedCount
        && CollectWorldIds(diskCopy).SetEquals(expectedIds);
    diskCopy.Free();
    if (!diskOk) { Fail("On-disk scene differs from the build"); return; }

    Quit(0);
}
```

`path` should be a revision-specific `res://...` path. `Fail(...)` must log the error and `Quit(1)`. Do not publish a build manifest until this function exits successfully; record the WorldSpec hash, root seed, stable IDs, logical asset IDs, scene path, and generated file hashes in that manifest.

GLB models: instantiate the `PackedScene`, measure the `MeshInstance3D` AABB to scale, and use a **primitive** collision shape (Box/Sphere/Capsule) from the AABB — never `CreateTrimeshShape()`/`CreateConvexShape()` on imported meshes (drops to <1 FPS).

## Quirks worth knowing (silent-failure)

Most Godot behavior the model already knows; these few fail with no error:

- **`ArrayMesh.GenerateNormals()`** is required for a procedural mesh to *receive* shadows. Without it (or with `CullMode.Disabled` as a "safety net"), shadows silently vanish — fix winding instead.
- **MultiMeshInstance3D + GLB** loses the mesh on pack/save; use individual instances. `MaterialOverride` on GLB-internal nodes also won't serialize (owner is skipped) — use a procedural `ArrayMesh` when a custom material is needed.
- **Raycasts don't reliably hit `ConcavePolygonShape3D`** (trimesh) — use a shape query or sample terrain height analytically.
- **`.gdignore`** in a directory makes the importer skip it silently — only `screenshots/` should have one, never `assets/`.
- **C# enum names:** training data is GDScript-biased, so guessed C# enum names are often wrong (`BGMode.Sky`, not `BGModeEnum.Sky`). Verify against the installed Godot — read the C# API in the Godot docs/assemblies rather than guessing.
- Frame-rate-independent damping: `speed *= Mathf.Exp(-rate * delta)`, not `speed *= (1 - drag)` per tick.

## Structural evaluation is a separate, read-only stage

Run a dedicated C# `SceneTree` evaluator after the verified save and before any visual capture. It loads the generated scene **from disk** and writes a machine-readable report; it does not repair nodes, rewrite the WorldSpec, or reuse the builder's in-memory tree. Exit nonzero when a hard gate fails.

At minimum, the evaluator owns these checks:

- `scene_loads`: the scene loads without parser, script, or runtime errors, and all required stable IDs and catalog references exist exactly once;
- `world_graph_connected`: all required regions are connected from the player spawn through declared roads;
- `objectives_completable`: every objective references existing interactables in reachable regions and its `all`, `any`, or `sequence` rule can complete;
- `completion_reachable`: the exit region is reachable and its required objective IDs can all become complete;
- nodes and colliders stay inside WorldSpec bounds; required colliders exist and forbidden overlaps are absent;
- manifest hashes, stable-ID ordering, and a second same-seed build agree, with nondeterministic fields explicitly excluded;
- every failure has a stable issue code, severity, affected IDs, and an evidence path in `eval_report.json`.

The reference scaffold proves reachability over the region/road graph and checks
generated collision counts. A run that adds NavMesh geometry should strengthen
the same four check IDs with synchronized navigation-path queries; it must not
silently rename the rubric gates.

Only revisions passing all structural hard gates may enter screenshot/video evaluation. This keeps rendering and vision evaluation from hiding a broken scene behind a good-looking frame.

## Capture (proof video)

For fixed-camera still evidence, set safe `PTP_RUN_ID`, set `PTP_REVISION` to
`0`, `1`, or `2`, and launch the rendered project with `PTP_CAPTURE=1`. The
scaffold visits every WorldSpec camera, waits for rendered frames, rejects empty
or near-uniform output, saves PNGs below the revision directory, records their
SHA-256 digests, and exits. Do not pass `--headless` for this capture run.

Hardware **Vulkan** gives correct rendering and is required for video; software Vulkan (`llvmpipe`/`lavapipe`) can still do stills but skip video and report it.

Capture deterministically with Godot's movie writer from a dedicated capture `SceneTree` script under `test/`:

```bash
# under xvfb-run -a -s '-screen 0 1920x1080x24' on a headless Linux box; prefer the hardware Vulkan ICD
godot --headless --import
godot --write-movie screenshots/result/frame.png --fixed-fps 30 --quit-after 450 --script test/Presentation.cs
ffmpeg -y -framerate 30 -i 'screenshots/result/frame%08d.png' \
  -c:v libx264 -pix_fmt yuv420p -movflags +faststart screenshots/result/video.mp4
```

On Windows, run import, builders, evaluators, and capture serially from PowerShell. Use the installed GPU renderer for the final capture; `--headless` is appropriate for import and structural evaluation, not for evidence frames:

```powershell
$GodotBin = $env:GODOT4_BIN
if ([string]::IsNullOrWhiteSpace($GodotBin) -or
    -not (Test-Path -LiteralPath $GodotBin)) {
    throw 'Set GODOT4_BIN to the Godot 4 .NET executable.'
}

$RunId = 'r001'
$FramesDir = Join-Path 'screenshots\result' $RunId
New-Item -ItemType Directory -Force -Path $FramesDir | Out-Null

dotnet build
if ($LASTEXITCODE -ne 0) { throw 'dotnet build failed' }
& $GodotBin --headless --path . --import
if ($LASTEXITCODE -ne 0) { throw 'Godot import failed' }
& $GodotBin --path . --write-movie "$FramesDir/frame.png" `
    --fixed-fps 30 --quit-after 450 --script test/Presentation.cs
if ($LASTEXITCODE -ne 0) { throw 'Godot capture failed' }
ffmpeg -y -framerate 30 -i "$FramesDir/frame%08d.png" `
    -c:v libx264 -pix_fmt yuv420p -movflags +faststart "$FramesDir/video.mp4"
if ($LASTEXITCODE -ne 0) { throw 'ffmpeg encoding failed' }
```

Use a per-workspace lock around the complete build/import/evaluate/capture sequence. Store the owner PID and run ID, refuse a live lock, clear a stale lock only after verifying that PID is gone, and release it in `finally`; never fix contention by killing every Godot process. Use unique revision output paths and bounded retries for transient editor/antivirus file locks.

Windows paths containing spaces or non-ASCII characters must be passed as quoted arguments and inspected with `-LiteralPath`. Keep run IDs and artifact paths shallow to avoid legacy `MAX_PATH` failures. If a third-party tool still mishandles the workspace path, reproduce the run from an explicitly chosen short ASCII path such as `D:\ptp\game`; do not silently rename or move the user's project.

`--fixed-fps` makes motion deterministic (450 frames @30fps = 15s). **Pre-position the camera** in the builder/`_Initialize` (the first movie frame renders before `_Process`). Drive capture-time input from the script, not live keys. The clip must show the behavior progressing across the whole window — no dead time, no single looped frame.
