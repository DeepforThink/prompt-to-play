# Topographic Highland Circuit — development snapshot

This is a clean, editable Godot 4.7.1 .NET project produced by the
Prompt-to-Play pipeline from an arbitrary topographic racing-world prompt.
It is the selected revision from request `f22b5ced9ca9` and is committed as a
development artifact, not as a packaged executable.

The snapshot intentionally contains only the files needed to edit and run the
game:

- `project.godot` and `PromptToPlay.csproj`;
- `scenes/` and the generated C# runtime in `scripts/`;
- the selected `spec/world.json`;
- `assets/catalog.json`, ready for teammates to add GLB/GLTF prefabs.

Generated caches, nested Git metadata, API credentials, Agent traces, logs,
screenshots, and discarded revisions are not included.

## Open and develop

Install Godot 4.7.1 Mono and the .NET 8 SDK, then import this folder's
`project.godot` in Godot. No API key or Codex installation is needed to open,
edit, build, or play this existing project.

From a terminal at the repository root:

```powershell
dotnet build demos/topographic-highland-circuit/PromptToPlay.csproj
godot --editor --path demos/topographic-highland-circuit
```

Edit `spec/world.json` to change the existing world data. Edit the files under
`scripts/` to change runtime behavior. To regenerate a new project from a new
natural-language prompt, use the repository-level Prompt-to-Play launcher;
that regeneration path requires the configured model API.

This snapshot was produced with asset API mode off, so unresolved semantic
prefabs use the deterministic primitive fallback. Teammates can add real GLB
or GLTF files under `assets/` and register them in `assets/catalog.json`.

