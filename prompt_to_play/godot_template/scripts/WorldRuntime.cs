using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using Godot;

namespace PromptToPlay;

public partial class WorldRuntime : Node3D
{
    private const string SpecPath = "res://spec/world.json";
    private const string WorldSchema = "prompt-to-play/world@1";

    private readonly HashSet<string> _stableIds = new(StringComparer.Ordinal);
    private readonly List<ManifestEntity> _manifestEntities = new();
    private readonly HashSet<string> _primitiveFallbackIds = new(StringComparer.Ordinal);
    private readonly Dictionary<string, Camera3D> _evaluationCameras =
        new(StringComparer.Ordinal);
    private readonly SortedDictionary<string, string> _interactionPrompts =
        new(StringComparer.Ordinal);

    private WorldSpec _spec = null!;
    private string _sourceJson = string.Empty;
    private Node3D _generated = null!;
    private PrefabResolver _prefabResolver = null!;
    private ObjectiveManager _objectiveManager = null!;
    private ExitGate? _exitGate;
    private Label? _hud;
    private string _lastMessage = string.Empty;
    private int _exitCode;
    private bool _worldBuilt;

    private Color _skyColor;
    private Color _groundColor;
    private Color _primaryColor;
    private Color _accentColor;
    private Color _emissiveColor;

    public override void _Ready()
    {
        var stopwatch = Stopwatch.StartNew();
        try
        {
            EnsureInputActions();
            _spec = LoadAndValidateSpec();
            _prefabResolver = PrefabResolver.LoadDefault();
            ResolvePalette();

            _generated = new Node3D { Name = "Generated" };
            AddChild(_generated);
            BuildWorld();

            IReadOnlyList<StructuralCheck> checks = EvaluateStructure();
            stopwatch.Stop();
            ArtifactWriter.WriteSuccess(
                _spec,
                _sourceJson,
                stopwatch.ElapsedMilliseconds,
                _manifestEntities,
                _primitiveFallbackIds,
                checks);
            _worldBuilt = true;
            _exitCode = checks.All(check => check.Passed) ? 0 : 1;
        }
        catch (Exception exception)
        {
            stopwatch.Stop();
            _exitCode = 1;
            GD.PushError($"Prompt-to-Play build failed: {exception}");
            ArtifactWriter.WriteFailure(exception.Message);
        }

        bool captureRequested = OS.GetEnvironment("PTP_CAPTURE") == "1";
        bool headless = DisplayServer.GetName() == "headless";
        if (!_worldBuilt)
        {
            GetTree().Quit(_exitCode);
        }
        else if (captureRequested && headless)
        {
            GD.PushError("PTP_CAPTURE=1 requires a rendering display driver, not --headless.");
            GetTree().Quit(1);
        }
        else if (captureRequested)
        {
            _ = CaptureEvaluationCamerasAsync();
        }
        else if (headless)
        {
            GetTree().Quit(_exitCode);
        }
        else if (_exitCode != 0)
        {
            GetTree().Quit(_exitCode);
        }
    }

    private WorldSpec LoadAndValidateSpec()
    {
        if (!Godot.FileAccess.FileExists(SpecPath))
        {
            throw new InvalidDataException($"Missing WorldSpec: {SpecPath}");
        }

        _sourceJson = Godot.FileAccess.GetFileAsString(SpecPath);
        WorldSpec? spec = JsonSerializer.Deserialize<WorldSpec>(
            _sourceJson,
            new JsonSerializerOptions
            {
                PropertyNameCaseInsensitive = false,
                UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
            });
        if (spec is null)
        {
            throw new InvalidDataException("WorldSpec deserialized to null.");
        }

        ValidateSpec(spec);
        return spec;
    }

    private static void ValidateSpec(WorldSpec spec)
    {
        if (spec.Schema != WorldSchema)
        {
            throw new InvalidDataException($"Expected schema {WorldSchema}.");
        }

        RequireIdentifier(spec.WorldId, "world_id");
        if (string.IsNullOrWhiteSpace(spec.Brief.Text))
        {
            throw new InvalidDataException("brief.text must not be empty.");
        }
        foreach (ReferenceSpec reference in spec.Brief.References)
        {
            RequireRelativePath(reference.Path, "brief.references.path");
            if (!Regex.IsMatch(reference.Sha256, "^[0-9a-f]{64}$"))
            {
                throw new InvalidDataException(
                    "brief.references.sha256 must be a lowercase SHA-256 digest.");
            }
        }
        uint expectedSeed = StableSeed.DeriveWorld(
            spec.Brief.Text,
            spec.Brief.References.Select(reference => reference.Sha256));
        if (spec.Seed != expectedSeed)
        {
            throw new InvalidDataException(
                $"seed must be derived from prompt and references; expected {expectedSeed}.");
        }
        if (spec.Units.Length != "m" ||
            spec.Units.Rotation != "deg" ||
            spec.Units.UpAxis != "Y")
        {
            throw new InvalidDataException("units must be m/deg/Y.");
        }
        if (string.IsNullOrWhiteSpace(spec.Style.Theme))
        {
            throw new InvalidDataException("style.theme must not be empty.");
        }

        RequireHtmlColor(spec.Style.Palette.Sky, "style.palette.sky");
        RequireHtmlColor(spec.Style.Palette.Ground, "style.palette.ground");
        RequireHtmlColor(spec.Style.Palette.Primary, "style.palette.primary");
        RequireHtmlColor(spec.Style.Palette.Accent, "style.palette.accent");
        RequireHtmlColor(spec.Style.Palette.Emissive, "style.palette.emissive");
        if (!float.IsFinite(spec.Style.FogDensity) || spec.Style.FogDensity is < 0.0f or > 1.0f)
        {
            throw new InvalidDataException("style.fog_density must be in [0, 1].");
        }

        if (spec.Regions.Count == 0)
        {
            throw new InvalidDataException("At least one region is required.");
        }

        var allIds = new HashSet<string>(StringComparer.Ordinal);
        var regionIds = new HashSet<string>(StringComparer.Ordinal);
        foreach (RegionSpec region in spec.Regions)
        {
            RegisterSpecId(allIds, region.Id, $"region {region.Id}");
            regionIds.Add(region.Id);
            RequireVector3(region.Center, $"regions[{region.Id}].center");
            RequireVector3(region.Size, $"regions[{region.Id}].size", positive: true);
            if (!float.IsFinite(region.Elevation))
            {
                throw new InvalidDataException($"regions[{region.Id}].elevation must be finite.");
            }
        }

        foreach (RoadSpec road in spec.Roads)
        {
            RegisterSpecId(allIds, road.Id, $"road {road.Id}");
            RequireRegion(regionIds, road.From, $"roads[{road.Id}].from");
            RequireRegion(regionIds, road.To, $"roads[{road.Id}].to");
            if (road.From == road.To || !float.IsFinite(road.Width) || road.Width <= 0.0f)
            {
                throw new InvalidDataException($"roads[{road.Id}] has invalid endpoints or width.");
            }
            if (road.Waypoints.Count < 2)
            {
                throw new InvalidDataException($"roads[{road.Id}] needs at least two waypoints.");
            }
            for (int index = 0; index < road.Waypoints.Count; index++)
            {
                RequireVector3(road.Waypoints[index], $"roads[{road.Id}].waypoints[{index}]");
                if (index > 0 &&
                    ToVector3(road.Waypoints[index]).DistanceSquaredTo(
                        ToVector3(road.Waypoints[index - 1])) < 0.0001f)
                {
                    throw new InvalidDataException($"roads[{road.Id}] has a zero-length segment.");
                }
            }
        }

        foreach (BuildingSpec building in spec.Buildings)
        {
            ValidatePlacedEntity(
                allIds,
                regionIds,
                building.Id,
                building.Region,
                building.Prefab,
                building.Position,
                building.RotationDegrees,
                building.Scale,
                "building");
        }

        foreach (PropSpec prop in spec.Props)
        {
            ValidatePlacedEntity(
                allIds,
                regionIds,
                prop.Id,
                prop.Region,
                prop.Prefab,
                prop.Position,
                prop.RotationDegrees,
                prop.Scale,
                "prop");
        }

        foreach (LightSpec light in spec.Lights)
        {
            RegisterSpecId(allIds, light.Id, $"light {light.Id}");
            if (light.Kind is not ("directional" or "omni" or "spot"))
            {
                throw new InvalidDataException($"lights[{light.Id}].kind is unsupported.");
            }
            RequireVector3(light.Position, $"lights[{light.Id}].position");
            RequireVector3(light.RotationDegrees, $"lights[{light.Id}].rotation_deg");
            RequireHtmlColor(light.Color, $"lights[{light.Id}].color");
            bool invalidRange = !float.IsFinite(light.Range) || light.Range < 0.0f ||
                (light.Kind != "directional" && light.Range <= 0.0f);
            if (!float.IsFinite(light.Energy) || light.Energy < 0.0f || invalidRange)
            {
                throw new InvalidDataException($"lights[{light.Id}] has invalid energy or range.");
            }
        }

        RequireRegion(
            regionIds,
            spec.Interactions.PlayerSpawn.Region,
            "interactions.player_spawn.region");
        RequireVector3(spec.Interactions.PlayerSpawn.Position, "interactions.player_spawn.position");

        var interactableIds = new HashSet<string>(StringComparer.Ordinal);
        foreach (InteractableSpec interactable in spec.Interactions.Interactables)
        {
            RegisterSpecId(allIds, interactable.Id, $"interactable {interactable.Id}");
            interactableIds.Add(interactable.Id);
            RequireRegion(
                regionIds,
                interactable.Region,
                $"interactions.interactables[{interactable.Id}].region");
            RequireVector3(
                interactable.Position,
                $"interactions.interactables[{interactable.Id}].position");
            if (string.IsNullOrWhiteSpace(interactable.Prefab) ||
                string.IsNullOrWhiteSpace(interactable.Label) ||
                interactable.Action is not ("collect" or "activate" or "repair" or "inspect") ||
                interactable.DurationMilliseconds is < 0 or > 10000)
            {
                throw new InvalidDataException(
                    $"interactions.interactables[{interactable.Id}] is invalid.");
            }
        }

        var objectiveIds = new HashSet<string>(StringComparer.Ordinal);
        foreach (ObjectiveSpec objective in spec.Interactions.Objectives)
        {
            RegisterSpecId(allIds, objective.Id, $"objective {objective.Id}");
            objectiveIds.Add(objective.Id);
            if (objective.Rule is not ("all" or "any" or "sequence") ||
                objective.Targets.Count == 0 ||
                string.IsNullOrWhiteSpace(objective.CompletionText))
            {
                throw new InvalidDataException($"objectives[{objective.Id}] is invalid.");
            }
            foreach (string target in objective.Targets)
            {
                if (!interactableIds.Contains(target))
                {
                    throw new InvalidDataException(
                        $"objectives[{objective.Id}] references unknown interactable {target}.");
                }
            }
        }

        ExitSpec exit = spec.Interactions.Exit;
        RegisterSpecId(allIds, exit.Id, $"exit {exit.Id}");
        RequireRegion(regionIds, exit.Region, "interactions.exit.region");
        RequireVector3(exit.Position, "interactions.exit.position");
        foreach (string requirement in exit.Requires)
        {
            if (!objectiveIds.Contains(requirement))
            {
                throw new InvalidDataException(
                    $"interactions.exit requires unknown objective {requirement}.");
            }
        }

        if (spec.Cameras.Count == 0)
        {
            throw new InvalidDataException("At least one evaluation camera is required.");
        }
        foreach (CameraSpec camera in spec.Cameras)
        {
            RegisterSpecId(allIds, camera.Id, $"camera {camera.Id}");
            if (camera.Kind is not ("fixed" or "player" or "orbit"))
            {
                throw new InvalidDataException($"cameras[{camera.Id}].kind is unsupported.");
            }
            RequireVector3(camera.Position, $"cameras[{camera.Id}].position");
            RequireVector3(camera.LookAt, $"cameras[{camera.Id}].look_at");
            if (!float.IsFinite(camera.FovDegrees) || camera.FovDegrees is < 1.0f or > 179.0f ||
                camera.Resolution.Length != 2 || camera.Resolution.Any(value => value < 64))
            {
                throw new InvalidDataException($"cameras[{camera.Id}] is invalid.");
            }
        }
    }

    private void ResolvePalette()
    {
        PaletteSpec palette = _spec.Style.Palette;
        _skyColor = PrimitiveFactory.ParseColor(palette.Sky, new Color(0.08f, 0.12f, 0.22f));
        _groundColor = PrimitiveFactory.ParseColor(palette.Ground, new Color(0.28f, 0.32f, 0.36f));
        _primaryColor = PrimitiveFactory.ParseColor(palette.Primary, new Color(0.45f, 0.5f, 0.58f));
        _accentColor = PrimitiveFactory.ParseColor(palette.Accent, new Color(0.85f, 0.55f, 0.12f));
        _emissiveColor = PrimitiveFactory.ParseColor(palette.Emissive, new Color(0.3f, 0.75f, 1.0f));
    }

    private void BuildWorld()
    {
        BuildEnvironment();
        foreach (RegionSpec region in _spec.Regions.OrderBy(item => item.Id, StringComparer.Ordinal))
        {
            BuildRegion(region);
        }
        foreach (RoadSpec road in _spec.Roads.OrderBy(item => item.Id, StringComparer.Ordinal))
        {
            BuildRoad(road);
        }
        foreach (BuildingSpec building in _spec.Buildings.OrderBy(item => item.Id, StringComparer.Ordinal))
        {
            BuildPrefabEntity(
                building.Id,
                building.Prefab,
                building.Position,
                building.RotationDegrees,
                building.Scale,
                "building");
        }
        foreach (PropSpec prop in _spec.Props.OrderBy(item => item.Id, StringComparer.Ordinal))
        {
            BuildPrefabEntity(
                prop.Id,
                prop.Prefab,
                prop.Position,
                prop.RotationDegrees,
                prop.Scale,
                "prop");
        }
        BuildLights();
        BuildFixedCameras();
        BuildObjectives();
        BuildPlayer();
        BuildInteractables();
        BuildExit();
        BuildHud();
        UpdateHud();
    }

    private void BuildEnvironment()
    {
        var environment = new Godot.Environment
        {
            BackgroundMode = Godot.Environment.BGMode.Color,
            BackgroundColor = _skyColor,
            AmbientLightSource = Godot.Environment.AmbientSource.Color,
            AmbientLightColor = _skyColor.Lerp(Colors.White, 0.5f),
            AmbientLightEnergy = 0.65f,
            FogEnabled = _spec.Style.FogDensity > 0.0f,
            FogLightColor = _skyColor,
            // WorldSpec uses a normalized art-direction value. Godot's physical
            // density becomes opaque at values such as 0.2, so map [0, 1] onto
            // a useful scene-scale range instead of copying it verbatim.
            FogDensity = _spec.Style.FogDensity * 0.02f,
        };
        var worldEnvironment = new WorldEnvironment
        {
            Name = "WorldEnvironment",
            Environment = environment,
        };
        _generated.AddChild(worldEnvironment);
        RegisterStable(
            worldEnvironment,
            RuntimeId("environment"),
            "environment",
            StableSeed.Derive(_spec.Seed, RuntimeId("environment")),
            Vector3.Zero);
    }

    private void BuildRegion(RegionSpec region)
    {
        uint seed = region.GenerationSeed ?? StableSeed.Derive(_spec.Seed, region.Id);
        var rng = new RandomNumberGenerator { Seed = seed };
        Color surfaceColor = _groundColor.Lerp(_primaryColor, rng.RandfRange(0.08f, 0.25f));
        var surfaceMaterial = PrimitiveFactory.Material(surfaceColor, metallic: 0.18f);

        Vector3 size = ToVector3(region.Size);
        float thickness = Mathf.Clamp(size.Y * 0.2f, 1.0f, 3.0f);
        Vector3 center = new(region.Center[0], region.Elevation - thickness * 0.5f, region.Center[2]);
        StaticBody3D deck = PrimitiveFactory.AddBoxBody(
            _generated,
            SafeNodeName(region.Id),
            center,
            new Vector3(size.X, thickness, size.Z),
            surfaceMaterial);
        deck.AddToGroup("ptp_walkable");
        RegisterStable(deck, region.Id, "region", seed, center);

        var accentMaterial = PrimitiveFactory.Material(_accentColor, metallic: 0.35f);
        float pylonHeight = Mathf.Clamp(size.Y * 0.45f, 1.5f, 5.0f);
        float inset = 1.25f;
        Vector3[] corners =
        {
            new(-size.X * 0.5f + inset, thickness * 0.5f + pylonHeight * 0.5f, -size.Z * 0.5f + inset),
            new(size.X * 0.5f - inset, thickness * 0.5f + pylonHeight * 0.5f, -size.Z * 0.5f + inset),
            new(-size.X * 0.5f + inset, thickness * 0.5f + pylonHeight * 0.5f, size.Z * 0.5f - inset),
            new(size.X * 0.5f - inset, thickness * 0.5f + pylonHeight * 0.5f, size.Z * 0.5f - inset),
        };
        for (int index = 0; index < corners.Length; index++)
        {
            PrimitiveFactory.AddCylinderVisual(
                deck,
                $"Pylon{index + 1}",
                corners[index],
                0.24f,
                pylonHeight,
                accentMaterial);
        }

        BuildRegionDecoration(region, size, rng);
    }

    private void BuildRegionDecoration(
        RegionSpec region,
        Vector3 regionSize,
        RandomNumberGenerator rng)
    {
        string semantic = $"{region.Kind} {_spec.Style.Theme}".ToLowerInvariant();
        string category = ContainsAny(
            semantic,
            "forest", "grove", "jungle", "wood", "森林", "雨林", "树林", "丛林")
            ? "vegetation"
            : ContainsAny(
                semantic,
                "ruin", "temple", "ancient", "stone", "遗迹", "废墟", "古代", "石")
                ? "ruin"
                : ContainsAny(
                    semantic,
                    "industrial", "factory", "mechanical", "city", "urban",
                    "foundry", "工业", "工厂", "机械", "城市", "铸造")
                    ? "industrial"
                    : "generic";

        float area = regionSize.X * regionSize.Z;
        float divisor = category switch
        {
            "vegetation" => 75.0f,
            "ruin" => 120.0f,
            "industrial" => 130.0f,
            _ => 180.0f,
        };
        int count = Mathf.Clamp(Mathf.RoundToInt(area / divisor), 3, 18);
        var reserved = new List<Vector3>();
        if (_spec.Interactions.PlayerSpawn.Region == region.Id)
        {
            reserved.Add(ToVector3(_spec.Interactions.PlayerSpawn.Position));
        }
        reserved.AddRange(_spec.Interactions.Interactables
            .Where(item => item.Region == region.Id)
            .Select(item => ToVector3(item.Position)));
        if (_spec.Interactions.Exit.Region == region.Id)
        {
            reserved.Add(ToVector3(_spec.Interactions.Exit.Position));
        }
        reserved.AddRange(_spec.Buildings
            .Where(item => item.Region == region.Id)
            .Select(item => ToVector3(item.Position)));
        reserved.AddRange(_spec.Props
            .Where(item => item.Region == region.Id)
            .Select(item => ToVector3(item.Position)));
        reserved.AddRange(_spec.Roads
            .Where(road => road.From == region.Id || road.To == region.Id)
            .SelectMany(road => road.Waypoints)
            .Select(ToVector3));
        var placed = new List<Vector3>();
        float clearanceSquared = category == "vegetation" ? 10.24f : 6.25f;
        for (int index = 0; index < count; index++)
        {
            string stableId = RuntimeId($"decor.{region.Id}.{index:D2}");
            uint seed = StableSeed.Derive(_spec.Seed, stableId);
            Vector3? selectedPosition = null;
            for (int attempt = 0; attempt < 10; attempt++)
            {
                Vector3 candidate = new(
                    region.Center[0] + rng.RandfRange(-regionSize.X * 0.40f, regionSize.X * 0.40f),
                    region.Elevation,
                    region.Center[2] + rng.RandfRange(-regionSize.Z * 0.40f, regionSize.Z * 0.40f));
                if (reserved.All(point => point.DistanceSquaredTo(candidate) >= clearanceSquared) &&
                    placed.All(point => point.DistanceSquaredTo(candidate) >= clearanceSquared))
                {
                    selectedPosition = candidate;
                    break;
                }
            }
            if (selectedPosition is null)
            {
                continue;
            }
            Vector3 position = selectedPosition.Value;
            placed.Add(position);
            float uniformScale = rng.RandfRange(0.65f, 1.35f);
            var root = new Node3D
            {
                Name = SafeNodeName(stableId),
                Position = position,
                RotationDegrees = new Vector3(0.0f, rng.RandfRange(0.0f, 360.0f), 0.0f),
            };
            _generated.AddChild(root);

            Vector3 scale = Vector3.One * uniformScale;
            switch (category)
            {
                case "vegetation":
                    BuildTreePrimitive(root, scale, seed);
                    break;
                case "ruin" when index % 3 == 0:
                    BuildRuinPrimitive(root, scale * 0.55f, seed);
                    break;
                case "ruin":
                    BuildRockPrimitive(root, scale, seed);
                    break;
                case "industrial":
                    BuildBlockPrimitive(root, scale * 0.7f, seed, large: false);
                    break;
                default:
                    BuildRockPrimitive(root, scale * 0.75f, seed);
                    break;
            }

            RegisterStable(root, stableId, "decor", seed, position);
        }
    }

    private void BuildRoad(RoadSpec road)
    {
        uint roadSeed = StableSeed.Derive(_spec.Seed, road.Id);
        var root = new Node3D { Name = SafeNodeName(road.Id) };
        _generated.AddChild(root);
        RegisterStable(root, road.Id, "road", roadSeed, Vector3.Zero);

        var deckMaterial = PrimitiveFactory.Material(
            _primaryColor.Lerp(_groundColor, 0.35f),
            metallic: 0.3f);
        var railMaterial = PrimitiveFactory.Material(_accentColor, metallic: 0.55f);
        for (int index = 0; index < road.Waypoints.Count - 1; index++)
        {
            Vector3 from = ToVector3(road.Waypoints[index]);
            Vector3 to = ToVector3(road.Waypoints[index + 1]);
            Vector3 delta = to - from;
            float length = delta.Length();
            Vector3 center = (from + to) * 0.5f - Vector3.Up * 0.25f;
            string segmentId = RuntimeId($"road.{road.Id}.segment.{index:D2}");
            StaticBody3D segment = PrimitiveFactory.AddBoxBody(
                root,
                $"Segment{index:D2}",
                center,
                new Vector3(road.Width, 0.5f, length),
                deckMaterial);
            Vector3 up = Mathf.Abs(delta.Normalized().Dot(Vector3.Up)) > 0.98f
                ? Vector3.Forward
                : Vector3.Up;
            segment.LookAt(to - Vector3.Up * 0.25f, up);
            segment.AddToGroup("ptp_walkable");
            RegisterStable(
                segment,
                segmentId,
                "road_segment",
                StableSeed.Derive(_spec.Seed, segmentId),
                center);

            PrimitiveFactory.AddBoxVisual(
                segment,
                "RailLeft",
                new Vector3(-road.Width * 0.5f + 0.12f, 0.55f, 0.0f),
                new Vector3(0.18f, 0.8f, length),
                railMaterial);
            PrimitiveFactory.AddBoxVisual(
                segment,
                "RailRight",
                new Vector3(road.Width * 0.5f - 0.12f, 0.55f, 0.0f),
                new Vector3(0.18f, 0.8f, length),
                railMaterial);
        }
    }

    private void BuildPrefabEntity(
        string stableId,
        string prefab,
        float[] positionValues,
        float[] rotationValues,
        float[] scaleValues,
        string kind)
    {
        uint seed = StableSeed.Derive(_spec.Seed, stableId);
        Vector3 position = ToVector3(positionValues);
        Vector3 rotation = ToVector3(rotationValues);
        Vector3 scale = ToVector3(scaleValues);
        var root = new Node3D
        {
            Name = SafeNodeName(stableId),
            Position = position,
            RotationDegrees = rotation,
        };
        _generated.AddChild(root);
        root.SetMeta("prefab", prefab);
        RegisterStable(root, stableId, kind, seed, position);

        if (GeneratedCodeObjects.TryBuild(
                stableId,
                prefab,
                root,
                scale,
                seed,
                _primaryColor,
                _accentColor,
                _emissiveColor,
                out string codeFailure))
        {
            root.SetMeta("asset_resolution", "llm_code");
            return;
        }
        if (!string.IsNullOrEmpty(codeFailure))
        {
            root.SetMeta("code_generation_error", codeFailure);
        }

        if (_prefabResolver.TryInstantiate(
                prefab,
                root,
                scale,
                addCollision: true,
                out string assetFailure))
        {
            return;
        }

        root.SetMeta("asset_resolution", "primitive_fallback");
        root.SetMeta("asset_resolution_error", assetFailure);
        _primitiveFallbackIds.Add(stableId);

        string token = prefab.ToLowerInvariant();
        if (token.Contains("tree") || token.Contains("forest"))
        {
            BuildTreePrimitive(root, scale, seed);
        }
        else if (token.Contains("ruin") || token.Contains("arch") || token.Contains("temple"))
        {
            BuildRuinPrimitive(root, scale, seed);
        }
        else if (token.Contains("building") || token.Contains("factory") ||
                 token.Contains("tower") || token.Contains("house"))
        {
            BuildBlockPrimitive(root, scale, seed, large: true);
        }
        else if (token.Contains("rock") || token.Contains("boulder"))
        {
            BuildRockPrimitive(root, scale, seed);
        }
        else
        {
            switch (seed % 3u)
            {
                case 0:
                    BuildBlockPrimitive(root, scale, seed, large: kind == "building");
                    break;
                case 1:
                    BuildRockPrimitive(root, scale, seed);
                    break;
                default:
                    BuildMarkerPrimitive(root, scale, seed);
                    break;
            }
        }
    }

    private void BuildTreePrimitive(Node3D root, Vector3 scale, uint seed)
    {
        var rng = new RandomNumberGenerator { Seed = seed };
        float height = 4.0f * scale.Y * rng.RandfRange(0.85f, 1.2f);
        float radius = 0.45f * Mathf.Max(scale.X, scale.Z);
        var trunkMaterial = PrimitiveFactory.Material(_groundColor.Lerp(_accentColor, 0.25f));
        var crownMaterial = PrimitiveFactory.Material(_primaryColor.Lerp(_groundColor, 0.2f));
        StaticBody3D trunk = PrimitiveFactory.AddBoxBody(
            root,
            "Trunk",
            new Vector3(0.0f, height * 0.5f, 0.0f),
            new Vector3(radius * 1.2f, height, radius * 1.2f),
            trunkMaterial);
        trunk.AddToGroup("ptp_obstacle");
        PrimitiveFactory.AddSphereVisual(
            root,
            "CrownA",
            new Vector3(0.0f, height + radius, 0.0f),
            radius * 2.3f,
            crownMaterial);
        PrimitiveFactory.AddSphereVisual(
            root,
            "CrownB",
            new Vector3(radius, height + radius * 1.5f, 0.0f),
            radius * 1.5f,
            crownMaterial);
    }

    private void BuildRuinPrimitive(Node3D root, Vector3 scale, uint seed)
    {
        float width = 4.0f * scale.X;
        float height = 4.5f * scale.Y;
        float depth = 1.2f * scale.Z;
        var stone = PrimitiveFactory.Material(_primaryColor.Lerp(_groundColor, 0.38f), roughness: 0.95f);
        PrimitiveFactory.AddBoxBody(
            root,
            "ColumnLeft",
            new Vector3(-width * 0.42f, height * 0.5f, 0.0f),
            new Vector3(width * 0.18f, height, depth),
            stone).AddToGroup("ptp_obstacle");
        PrimitiveFactory.AddBoxBody(
            root,
            "ColumnRight",
            new Vector3(width * 0.42f, height * 0.5f, 0.0f),
            new Vector3(width * 0.18f, height, depth),
            stone).AddToGroup("ptp_obstacle");
        PrimitiveFactory.AddBoxBody(
            root,
            "Lintel",
            new Vector3(0.0f, height, 0.0f),
            new Vector3(width, height * 0.18f, depth),
            stone).AddToGroup("ptp_obstacle");
    }

    private void BuildBlockPrimitive(Node3D root, Vector3 scale, uint seed, bool large)
    {
        var rng = new RandomNumberGenerator { Seed = seed };
        float unit = large ? 1.0f : 0.55f;
        Vector3 size = new(
            5.0f * scale.X * unit,
            rng.RandfRange(3.0f, 6.0f) * scale.Y * unit,
            5.0f * scale.Z * unit);
        var bodyMaterial = PrimitiveFactory.Material(_primaryColor, metallic: 0.28f);
        var detailMaterial = PrimitiveFactory.Material(_accentColor, metallic: 0.62f);
        PrimitiveFactory.AddBoxBody(
            root,
            "Body",
            new Vector3(0.0f, size.Y * 0.5f, 0.0f),
            size,
            bodyMaterial).AddToGroup("ptp_obstacle");

        int detailCount = large ? 3 : 1;
        for (int index = 0; index < detailCount; index++)
        {
            float x = rng.RandfRange(-size.X * 0.32f, size.X * 0.32f);
            float z = rng.RandfRange(-size.Z * 0.32f, size.Z * 0.32f);
            float height = rng.RandfRange(1.0f, 2.6f) * unit;
            PrimitiveFactory.AddCylinderVisual(
                root,
                $"Detail{index + 1}",
                new Vector3(x, size.Y + height * 0.5f, z),
                0.25f * unit,
                height,
                detailMaterial);
        }
    }

    private void BuildRockPrimitive(Node3D root, Vector3 scale, uint seed)
    {
        var rng = new RandomNumberGenerator { Seed = seed };
        Vector3 size = new(
            rng.RandfRange(1.0f, 2.2f) * scale.X,
            rng.RandfRange(0.8f, 1.8f) * scale.Y,
            rng.RandfRange(1.0f, 2.2f) * scale.Z);
        var material = PrimitiveFactory.Material(_groundColor.Lerp(_primaryColor, 0.2f), roughness: 1.0f);
        StaticBody3D rock = PrimitiveFactory.AddBoxBody(
            root,
            "Rock",
            new Vector3(0.0f, size.Y * 0.5f, 0.0f),
            size,
            material);
        rock.RotationDegrees = new Vector3(
            rng.RandfRange(-10.0f, 10.0f),
            rng.RandfRange(0.0f, 180.0f),
            rng.RandfRange(-10.0f, 10.0f));
        rock.AddToGroup("ptp_obstacle");
    }

    private void BuildMarkerPrimitive(Node3D root, Vector3 scale, uint seed)
    {
        float height = Mathf.Max(1.0f, 2.5f * scale.Y);
        var baseMaterial = PrimitiveFactory.Material(_primaryColor, metallic: 0.22f);
        var glowMaterial = PrimitiveFactory.Material(
            _accentColor,
            metallic: 0.15f,
            emission: _emissiveColor);
        PrimitiveFactory.AddBoxBody(
            root,
            "Pedestal",
            new Vector3(0.0f, height * 0.3f, 0.0f),
            new Vector3(scale.X, height * 0.6f, scale.Z),
            baseMaterial).AddToGroup("ptp_obstacle");
        PrimitiveFactory.AddSphereVisual(
            root,
            "Marker",
            new Vector3(0.0f, height, 0.0f),
            0.5f * Mathf.Max(scale.X, scale.Z),
            glowMaterial);
    }

    private void BuildLights()
    {
        IEnumerable<LightSpec> lights = _spec.Lights;
        if (_spec.Lights.Count == 0)
        {
            lights = new[]
            {
                new LightSpec
                {
                    Id = RuntimeId("default_light"),
                    Kind = "directional",
                    Position = new[] { 0.0f, 20.0f, 0.0f },
                    RotationDegrees = new[] { -55.0f, -25.0f, 0.0f },
                    Color = _spec.Style.Palette.Sky,
                    Energy = 1.2f,
                    Range = 100.0f,
                },
            };
        }

        foreach (LightSpec lightSpec in lights.OrderBy(item => item.Id, StringComparer.Ordinal))
        {
            Light3D light = lightSpec.Kind switch
            {
                "directional" => new DirectionalLight3D(),
                "omni" => new OmniLight3D { OmniRange = lightSpec.Range },
                "spot" => new SpotLight3D { SpotRange = lightSpec.Range },
                _ => throw new InvalidDataException($"Unsupported light kind {lightSpec.Kind}."),
            };
            light.Name = SafeNodeName(lightSpec.Id);
            light.Position = ToVector3(lightSpec.Position);
            light.RotationDegrees = ToVector3(lightSpec.RotationDegrees);
            light.LightColor = PrimitiveFactory.ParseColor(lightSpec.Color, Colors.White);
            light.LightEnergy = lightSpec.Energy;
            light.ShadowEnabled = true;
            _generated.AddChild(light);
            RegisterStable(
                light,
                lightSpec.Id,
                "light",
                StableSeed.Derive(_spec.Seed, lightSpec.Id),
                light.Position);
        }
    }

    private void BuildFixedCameras()
    {
        foreach (CameraSpec cameraSpec in _spec.Cameras.OrderBy(item => item.Id, StringComparer.Ordinal))
        {
            var camera = new Camera3D
            {
                Name = SafeNodeName(cameraSpec.Id),
                Position = ToVector3(cameraSpec.Position),
                Fov = cameraSpec.FovDegrees,
                Current = false,
            };
            _generated.AddChild(camera);
            camera.LookAt(ToVector3(cameraSpec.LookAt), Vector3.Up);
            camera.SetMeta("camera_kind", cameraSpec.Kind);
            camera.SetMeta("resolution_width", cameraSpec.Resolution[0]);
            camera.SetMeta("resolution_height", cameraSpec.Resolution[1]);
            _evaluationCameras.Add(cameraSpec.Id, camera);
            RegisterStable(
                camera,
                cameraSpec.Id,
                "camera",
                StableSeed.Derive(_spec.Seed, cameraSpec.Id),
                camera.Position);
        }
    }

    private void BuildObjectives()
    {
        _objectiveManager = new ObjectiveManager { Name = "ObjectiveManager" };
        _generated.AddChild(_objectiveManager);
        _objectiveManager.Configure(_spec.Interactions.Objectives);
        _objectiveManager.ObjectiveCompleted += OnObjectiveCompleted;
        _objectiveManager.StateChanged += OnObjectiveStateChanged;
        string stableId = RuntimeId("objective_manager");
        RegisterStable(
            _objectiveManager,
            stableId,
            "objective_manager",
            StableSeed.Derive(_spec.Seed, stableId),
            Vector3.Zero);
    }

    private void BuildPlayer()
    {
        var player = new PlayerController
        {
            Name = "Player",
            Position = ToVector3(_spec.Interactions.PlayerSpawn.Position),
            CollisionLayer = 1,
            CollisionMask = 1,
        };
        var collision = new CollisionShape3D
        {
            Name = "Collision",
            Shape = new CapsuleShape3D { Radius = 0.45f, Height = 1.8f },
        };
        var head = new Node3D
        {
            Name = "Head",
            Position = new Vector3(0.0f, 0.62f, 0.0f),
        };
        var camera = new Camera3D
        {
            Name = "FpsCamera",
            Current = true,
            Fov = 75.0f,
            Near = 0.05f,
        };
        head.AddChild(camera);
        player.AddChild(collision);
        player.AddChild(head);
        _generated.AddChild(player);
        string stableId = RuntimeId("player");
        RegisterStable(
            player,
            stableId,
            "player",
            StableSeed.Derive(_spec.Seed, stableId),
            player.Position);
    }

    private void BuildInteractables()
    {
        foreach (InteractableSpec spec in _spec.Interactions.Interactables.OrderBy(
                     item => item.Id,
                     StringComparer.Ordinal))
        {
            uint seed = StableSeed.Derive(_spec.Seed, spec.Id);
            var material = PrimitiveFactory.Material(
                _accentColor,
                metallic: 0.35f,
                emission: _emissiveColor);
            var interactable = new Interactable
            {
                Name = SafeNodeName(spec.Id),
                Position = ToVector3(spec.Position),
                CollisionLayer = 2,
                CollisionMask = 1,
            };
            bool codeGenerated = GeneratedCodeObjects.TryBuild(
                spec.Id,
                spec.Prefab,
                interactable,
                Vector3.One,
                seed,
                _primaryColor,
                _accentColor,
                _emissiveColor,
                out string codeFailure);
            if (codeGenerated)
            {
                interactable.SetMeta("asset_resolution", "llm_code");
            }
            else if (!_prefabResolver.TryInstantiate(
                    spec.Prefab,
                    interactable,
                    Vector3.One,
                    addCollision: false,
                    out string assetFailure))
            {
                interactable.SetMeta("asset_resolution", "primitive_fallback");
                interactable.SetMeta("asset_resolution_error", assetFailure);
                _primitiveFallbackIds.Add(spec.Id);
                PrimitiveFactory.AddSphereVisual(
                    interactable,
                    "Visual",
                    Vector3.Zero,
                    0.65f,
                    material);
            }
            if (!string.IsNullOrEmpty(codeFailure))
            {
                interactable.SetMeta("code_generation_error", codeFailure);
            }
            interactable.AddChild(new CollisionShape3D
            {
                Name = "InteractionArea",
                Shape = new SphereShape3D { Radius = 1.35f },
            });
            interactable.AddChild(new OmniLight3D
            {
                Name = "Glow",
                LightColor = _emissiveColor,
                LightEnergy = 0.7f,
                OmniRange = 4.0f,
                ShadowEnabled = false,
            });
            interactable.Configure(spec, seed, material);
            interactable.Triggered += _objectiveManager.RecordInteraction;
            interactable.ProximityChanged += OnInteractionProximityChanged;
            _generated.AddChild(interactable);
            interactable.SetMeta("action", spec.Action);
            interactable.SetMeta("label", spec.Label);
            interactable.SetMeta("prefab", spec.Prefab);
            RegisterStable(interactable, spec.Id, "interactable", seed, interactable.Position);
        }
    }

    private void BuildExit()
    {
        ExitSpec spec = _spec.Interactions.Exit;
        uint seed = StableSeed.Derive(_spec.Seed, spec.Id);
        var gateMaterial = PrimitiveFactory.Material(
            _accentColor,
            metallic: 0.45f,
            emission: new Color(1.0f, 0.24f, 0.16f));
        var gate = new ExitGate
        {
            Name = SafeNodeName(spec.Id),
            Position = ToVector3(spec.Position),
            CollisionLayer = 2,
            CollisionMask = 1,
        };
        gate.AddChild(new CollisionShape3D
        {
            Name = "ExitArea",
            Position = new Vector3(0.0f, 1.5f, 0.0f),
            Shape = new BoxShape3D { Size = new Vector3(5.0f, 3.5f, 4.0f) },
        });
        StaticBody3D barrier = PrimitiveFactory.AddBoxBody(
            gate,
            "Barrier",
            new Vector3(0.0f, 1.6f, 0.0f),
            new Vector3(3.6f, 3.2f, 0.35f),
            gateMaterial);
        CollisionShape3D barrierCollision = barrier.GetNode<CollisionShape3D>("Collision");
        var frameMaterial = PrimitiveFactory.Material(_primaryColor, metallic: 0.55f);
        PrimitiveFactory.AddBoxVisual(
            gate,
            "FrameLeft",
            new Vector3(-2.1f, 1.8f, 0.0f),
            new Vector3(0.5f, 3.6f, 0.8f),
            frameMaterial);
        PrimitiveFactory.AddBoxVisual(
            gate,
            "FrameRight",
            new Vector3(2.1f, 1.8f, 0.0f),
            new Vector3(0.5f, 3.6f, 0.8f),
            frameMaterial);
        PrimitiveFactory.AddBoxVisual(
            gate,
            "FrameTop",
            new Vector3(0.0f, 3.55f, 0.0f),
            new Vector3(4.7f, 0.5f, 0.8f),
            frameMaterial);
        gate.Configure(spec.Requires, barrierCollision, gateMaterial);
        gate.Completed += OnExitCompleted;
        _generated.AddChild(gate);
        RegisterStable(gate, spec.Id, "exit", seed, gate.Position);
        _exitGate = gate;
        _exitGate.UpdateObjectiveState(_objectiveManager.CompletedObjectives);
    }

    private void BuildHud()
    {
        var canvas = new CanvasLayer { Name = "Hud" };
        _hud = new Label
        {
            Name = "Status",
            Position = new Vector2(18.0f, 18.0f),
        };
        _hud.AddThemeFontSizeOverride("font_size", 20);
        canvas.AddChild(_hud);
        AddChild(canvas);
        string stableId = RuntimeId("hud");
        RegisterStable(
            canvas,
            stableId,
            "hud",
            StableSeed.Derive(_spec.Seed, stableId),
            Vector3.Zero);
    }

    private void OnObjectiveCompleted(string objectiveId, string completionText)
    {
        _lastMessage = completionText;
        GD.Print($"Objective completed: {objectiveId} — {completionText}");
    }

    private void OnObjectiveStateChanged()
    {
        _exitGate?.UpdateObjectiveState(_objectiveManager.CompletedObjectives);
        UpdateHud();
    }

    private void OnInteractionProximityChanged(
        string stableId,
        string prompt,
        bool available)
    {
        if (available)
        {
            _interactionPrompts[stableId] = prompt;
        }
        else
        {
            _interactionPrompts.Remove(stableId);
        }
        UpdateHud();
    }

    private void OnExitCompleted()
    {
        _lastMessage = "出口已抵达，世界目标完成。";
        Input.MouseMode = Input.MouseModeEnum.Visible;
        UpdateHud();
        GD.Print("Prompt-to-Play world completed.");
    }

    private void UpdateHud()
    {
        if (_hud is null)
        {
            return;
        }

        string objectives = _objectiveManager.BuildStatusText();
        string gate = _exitGate?.IsUnlocked == true ? "出口：已解锁" : "出口：未解锁";
        string interaction = _interactionPrompts.Count == 0
            ? string.Empty
            : $"\n{_interactionPrompts.First().Value}";
        _hud.Text =
            $"{_spec.WorldId} · {_spec.Style.Theme}\n" +
            "WASD 移动 · 空格跳跃 · E 交互 · Esc 释放鼠标\n" +
            $"{objectives}\n{gate}{interaction}" +
            (string.IsNullOrEmpty(_lastMessage) ? string.Empty : $"\n{_lastMessage}");
    }

    private async Task CaptureEvaluationCamerasAsync()
    {
        var captures = new List<CaptureArtifact>();
        try
        {
            if (_hud is not null)
            {
                _hud.Visible = false;
            }

            foreach (CameraSpec cameraSpec in _spec.Cameras.OrderBy(
                         camera => camera.Id,
                         StringComparer.Ordinal))
            {
                Camera3D camera = _evaluationCameras[cameraSpec.Id];
                GetWindow().Size = new Vector2I(
                    cameraSpec.Resolution[0],
                    cameraSpec.Resolution[1]);
                camera.GlobalPosition = ToVector3(cameraSpec.Position);
                camera.LookAt(ToVector3(cameraSpec.LookAt), Vector3.Up);
                camera.MakeCurrent();

                for (int frame = 0; frame < 5; frame++)
                {
                    await ToSignal(GetTree(), SceneTree.SignalName.ProcessFrame);
                }
                await ToSignal(
                    RenderingServer.Singleton,
                    RenderingServer.SignalName.FramePostDraw);

                Vector3 target = ToVector3(cameraSpec.LookAt);
                GD.Print(
                    $"Capture camera {cameraSpec.Id}: position={camera.GlobalPosition}, " +
                    $"target={target}, forward={-camera.GlobalBasis.Z}, current={camera.Current}, " +
                    $"target_behind={camera.IsPositionBehind(target)}, " +
                    $"target_screen={camera.UnprojectPosition(target)}");

                Image image = GetViewport().GetTexture().GetImage();
                if (image.IsEmpty())
                {
                    throw new InvalidOperationException(
                        $"Camera {cameraSpec.Id} produced an empty image.");
                }
                if (!HasVisualVariation(image))
                {
                    throw new InvalidOperationException(
                        $"Camera {cameraSpec.Id} produced a near-uniform image.");
                }
                string absolutePath = ArtifactWriter.CaptureAbsolutePath(cameraSpec.Id);
                Error saveError = image.SavePng(absolutePath);
                if (saveError != Error.Ok)
                {
                    throw new IOException(
                        $"Could not save camera {cameraSpec.Id}: {saveError}.");
                }

                string sha256 = Convert.ToHexString(
                    System.Security.Cryptography.SHA256.HashData(
                        System.IO.File.ReadAllBytes(absolutePath)))
                    .ToLowerInvariant();
                captures.Add(new CaptureArtifact
                {
                    CameraId = cameraSpec.Id,
                    Path = ArtifactWriter.CaptureRelativePath(cameraSpec.Id),
                    Resolution = cameraSpec.Resolution,
                    Sha256 = sha256,
                });
                GD.Print($"Prompt-to-Play capture: {absolutePath}");
            }

            ArtifactWriter.WriteCaptureManifest(_spec, captures);
            // Preserve a structural failure exit code after writing visual
            // evidence so the host can route both reports to RepairAgent.
            GetTree().Quit(_exitCode);
        }
        catch (Exception exception)
        {
            GD.PushError($"Prompt-to-Play capture failed: {exception}");
            GetTree().Quit(1);
        }
    }

    private static bool HasVisualVariation(Image image)
    {
        float minimum = float.PositiveInfinity;
        float maximum = float.NegativeInfinity;
        int stepX = Math.Max(1, image.GetWidth() / 32);
        int stepY = Math.Max(1, image.GetHeight() / 18);
        for (int y = 0; y < image.GetHeight(); y += stepY)
        {
            for (int x = 0; x < image.GetWidth(); x += stepX)
            {
                Color color = image.GetPixel(x, y);
                float luminance = color.R * 0.2126f +
                    color.G * 0.7152f +
                    color.B * 0.0722f;
                minimum = Math.Min(minimum, luminance);
                maximum = Math.Max(maximum, luminance);
            }
        }
        return maximum - minimum >= 0.03f;
    }

    private IReadOnlyList<StructuralCheck> EvaluateStructure()
    {
        HashSet<string> reachableRegions = FindReachableRegions();
        bool graphConnected = _spec.Regions.All(region => reachableRegions.Contains(region.Id));
        int expectedWalkables = _spec.Regions.Count +
            _spec.Roads.Sum(road => Math.Max(0, road.Waypoints.Count - 1));
        int actualWalkables = GetTree().GetNodesInGroup("ptp_walkable").Count;
        bool stableIdsPass = _stableIds.Count == _manifestEntities.Count;
        bool objectiveReferencesPass = _spec.Interactions.Objectives.All(objective =>
            objective.Targets.All(target =>
                _spec.Interactions.Interactables.Any(item => item.Id == target)));
        bool objectiveRoutesPass = _spec.Interactions.Interactables
            .Where(item => _spec.Interactions.Objectives.Any(
                objective => objective.Targets.Contains(item.Id, StringComparer.Ordinal)))
            .All(item => reachableRegions.Contains(item.Region));
        bool objectivesCompletable = objectiveReferencesPass && objectiveRoutesPass;
        bool exitReferencesPass = _spec.Interactions.Exit.Requires.All(required =>
            _spec.Interactions.Objectives.Any(objective => objective.Id == required));
        bool completionReachable = objectivesCompletable &&
            exitReferencesPass &&
            reachableRegions.Contains(_spec.Interactions.Exit.Region);

        return new List<StructuralCheck>
        {
            new()
            {
                Id = "scene_loads",
                Passed = true,
                Message = "WorldSpec loaded and the runtime scene tree was generated.",
            },
            new()
            {
                Id = "world_graph_connected",
                Passed = graphConnected,
                Message = graphConnected
                    ? $"All {_spec.Regions.Count} regions are connected to the player spawn region."
                    : $"Only {reachableRegions.Count} of {_spec.Regions.Count} regions are connected to spawn.",
            },
            new()
            {
                Id = "objectives_completable",
                Passed = objectivesCompletable,
                Message = objectivesCompletable
                    ? $"All {_spec.Interactions.Objectives.Count} objectives reference reachable interactables."
                    : "At least one objective target is missing or disconnected from the spawn graph.",
            },
            new()
            {
                Id = "completion_reachable",
                Passed = completionReachable,
                Message = completionReachable
                    ? "The objective requirements and final exit are reachable from the player spawn."
                    : "The objective graph or final exit cannot be completed from the player spawn.",
            },
            new()
            {
                Id = "stable_ids_unique",
                Passed = stableIdsPass,
                Message = $"Registered {_stableIds.Count} unique stable IDs.",
            },
            new()
            {
                Id = "walkable_collision",
                Passed = actualWalkables >= expectedWalkables,
                Message = $"Found {actualWalkables} walkable colliders; expected at least {expectedWalkables}.",
            },
            new()
            {
                Id = "evaluation_cameras",
                Passed = _spec.Cameras.Count > 0,
                Message = $"Generated {_spec.Cameras.Count} fixed evaluation camera(s).",
            },
            new()
            {
                Id = "style_applied",
                Passed = true,
                Message = $"Applied theme '{_spec.Style.Theme}', palette, sky, and fog density {_spec.Style.FogDensity}.",
            },
        };
    }

    private HashSet<string> FindReachableRegions()
    {
        var graph = _spec.Regions.ToDictionary(
            region => region.Id,
            _ => new HashSet<string>(StringComparer.Ordinal),
            StringComparer.Ordinal);
        foreach (RoadSpec road in _spec.Roads)
        {
            graph[road.From].Add(road.To);
            graph[road.To].Add(road.From);
        }

        var reachable = new HashSet<string>(StringComparer.Ordinal);
        var queue = new Queue<string>();
        queue.Enqueue(_spec.Interactions.PlayerSpawn.Region);
        reachable.Add(_spec.Interactions.PlayerSpawn.Region);
        while (queue.Count > 0)
        {
            string current = queue.Dequeue();
            foreach (string neighbor in graph[current])
            {
                if (reachable.Add(neighbor))
                {
                    queue.Enqueue(neighbor);
                }
            }
        }
        return reachable;
    }

    private void RegisterStable(Node node, string stableId, string kind, uint seed, Vector3 position)
    {
        if (!_stableIds.Add(stableId))
        {
            throw new InvalidDataException($"Generated duplicate stable ID {stableId}.");
        }

        node.SetMeta("stable_id", stableId);
        node.SetMeta("stable_seed", (long)seed);
        node.AddToGroup("ptp_generated");
        node.AddToGroup($"ptp_{kind}");
        _manifestEntities.Add(new ManifestEntity
        {
            StableId = stableId,
            Kind = kind,
            NodePath = node.GetPath().ToString(),
            Seed = seed,
            Position = new[] { position.X, position.Y, position.Z },
        });
    }

    private static void EnsureInputActions()
    {
        EnsureKeyAction("move_forward", Key.W);
        EnsureKeyAction("move_back", Key.S);
        EnsureKeyAction("move_left", Key.A);
        EnsureKeyAction("move_right", Key.D);
        EnsureKeyAction("jump", Key.Space);
        EnsureKeyAction("interact", Key.E);
    }

    private static void EnsureKeyAction(string action, Key key)
    {
        if (InputMap.HasAction(action))
        {
            return;
        }

        InputMap.AddAction(action);
        InputMap.ActionAddEvent(action, new InputEventKey { PhysicalKeycode = key });
    }

    private string RuntimeId(string suffix)
    {
        return $"runtime.{_spec.WorldId}.{suffix}";
    }

    private static bool ContainsAny(string value, params string[] tokens)
    {
        return tokens.Any(token => value.Contains(token, StringComparison.Ordinal));
    }

    private static string SafeNodeName(string stableId)
    {
        var result = new StringBuilder(stableId.Length);
        foreach (char character in stableId)
        {
            result.Append(char.IsAsciiLetterOrDigit(character) || character is '_' or '-'
                ? character
                : '_');
        }
        return result.Length == 0 ? "Entity" : result.ToString();
    }

    private static Vector3 ToVector3(float[] values)
    {
        return new Vector3(values[0], values[1], values[2]);
    }

    private static void ValidatePlacedEntity(
        HashSet<string> allIds,
        HashSet<string> regionIds,
        string id,
        string region,
        string prefab,
        float[] position,
        float[] rotation,
        float[] scale,
        string kind)
    {
        RegisterSpecId(allIds, id, $"{kind} {id}");
        RequireRegion(regionIds, region, $"{kind}s[{id}].region");
        if (string.IsNullOrWhiteSpace(prefab))
        {
            throw new InvalidDataException($"{kind}s[{id}].prefab must not be empty.");
        }
        RequireVector3(position, $"{kind}s[{id}].position");
        RequireVector3(rotation, $"{kind}s[{id}].rotation_deg");
        RequireVector3(scale, $"{kind}s[{id}].scale", positive: true);
    }

    private static void RegisterSpecId(HashSet<string> ids, string id, string path)
    {
        RequireIdentifier(id, path);
        if (!ids.Add(id))
        {
            throw new InvalidDataException($"Duplicate stable ID {id} at {path}.");
        }
    }

    private static void RequireIdentifier(string value, string path)
    {
        if (!Regex.IsMatch(value, "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"))
        {
            throw new InvalidDataException($"{path} is not a valid stable identifier.");
        }
    }

    private static void RequireRegion(HashSet<string> regionIds, string region, string path)
    {
        if (!regionIds.Contains(region))
        {
            throw new InvalidDataException($"{path} references unknown region {region}.");
        }
    }

    private static void RequireHtmlColor(string value, string path)
    {
        if (!Regex.IsMatch(value, "^#[0-9A-F]{6}$"))
        {
            throw new InvalidDataException($"{path} must be #RRGGBB.");
        }
    }

    private static void RequireRelativePath(string value, string path)
    {
        string[] parts = value.Split('/');
        bool drivePath = value.Length >= 2 && char.IsAsciiLetter(value[0]) && value[1] == ':';
        if (string.IsNullOrWhiteSpace(value) ||
            value.Contains('\\') ||
            value.StartsWith('/') ||
            value.StartsWith('~') ||
            drivePath ||
            parts.Any(part => part is "" or "." or ".."))
        {
            throw new InvalidDataException($"{path} must be a normalized repository-relative path.");
        }
    }

    private static void RequireVector3(float[] values, string path, bool positive = false)
    {
        if (values.Length != 3 || values.Any(value => !float.IsFinite(value)) ||
            (positive && values.Any(value => value <= 0.0f)))
        {
            throw new InvalidDataException(
                $"{path} must contain three finite{(positive ? " positive" : string.Empty)} numbers.");
        }
    }
}
