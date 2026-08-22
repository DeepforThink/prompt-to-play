using System.Text.Json.Serialization;

namespace PromptToPlay;

public sealed class WorldSpec
{
    [JsonPropertyName("schema")]
    public string Schema { get; init; } = string.Empty;

    [JsonPropertyName("world_id")]
    public string WorldId { get; init; } = string.Empty;

    [JsonPropertyName("seed")]
    public uint Seed { get; init; }

    [JsonPropertyName("brief")]
    public BriefSpec Brief { get; init; } = new();

    [JsonPropertyName("style")]
    public StyleSpec Style { get; init; } = new();

    [JsonPropertyName("units")]
    public UnitSpec Units { get; init; } = new();

    [JsonPropertyName("regions")]
    public List<RegionSpec> Regions { get; init; } = new();

    [JsonPropertyName("roads")]
    public List<RoadSpec> Roads { get; init; } = new();

    [JsonPropertyName("buildings")]
    public List<BuildingSpec> Buildings { get; init; } = new();

    [JsonPropertyName("props")]
    public List<PropSpec> Props { get; init; } = new();

    [JsonPropertyName("lights")]
    public List<LightSpec> Lights { get; init; } = new();

    [JsonPropertyName("interactions")]
    public InteractionSpec Interactions { get; init; } = new();

    [JsonPropertyName("cameras")]
    public List<CameraSpec> Cameras { get; init; } = new();
}

public sealed class BriefSpec
{
    [JsonPropertyName("text")]
    public string Text { get; init; } = string.Empty;

    [JsonPropertyName("references")]
    public List<ReferenceSpec> References { get; init; } = new();
}

public sealed class ReferenceSpec
{
    [JsonPropertyName("path")]
    public string Path { get; init; } = string.Empty;

    [JsonPropertyName("sha256")]
    public string Sha256 { get; init; } = string.Empty;
}

public sealed class UnitSpec
{
    [JsonPropertyName("length")]
    public string Length { get; init; } = string.Empty;

    [JsonPropertyName("rotation")]
    public string Rotation { get; init; } = string.Empty;

    [JsonPropertyName("up_axis")]
    public string UpAxis { get; init; } = string.Empty;
}

public sealed class StyleSpec
{
    [JsonPropertyName("theme")]
    public string Theme { get; init; } = string.Empty;

    [JsonPropertyName("palette")]
    public PaletteSpec Palette { get; init; } = new();

    [JsonPropertyName("fog_density")]
    public float FogDensity { get; init; }
}

public sealed class PaletteSpec
{
    [JsonPropertyName("sky")]
    public string Sky { get; init; } = "#18233a";

    [JsonPropertyName("ground")]
    public string Ground { get; init; } = "#4a5568";

    [JsonPropertyName("primary")]
    public string Primary { get; init; } = "#718096";

    [JsonPropertyName("accent")]
    public string Accent { get; init; } = "#d69e2e";

    [JsonPropertyName("emissive")]
    public string Emissive { get; init; } = "#63b3ed";
}

public sealed class RegionSpec
{
    [JsonPropertyName("id")]
    public string Id { get; init; } = string.Empty;

    [JsonPropertyName("kind")]
    public string Kind { get; init; } = string.Empty;

    [JsonPropertyName("center")]
    public float[] Center { get; init; } = Array.Empty<float>();

    [JsonPropertyName("size")]
    public float[] Size { get; init; } = Array.Empty<float>();

    [JsonPropertyName("elevation")]
    public float Elevation { get; init; }

    [JsonPropertyName("generation_seed")]
    public uint? GenerationSeed { get; init; }
}

public sealed class RoadSpec
{
    [JsonPropertyName("id")]
    public string Id { get; init; } = string.Empty;

    [JsonPropertyName("from")]
    public string From { get; init; } = string.Empty;

    [JsonPropertyName("to")]
    public string To { get; init; } = string.Empty;

    [JsonPropertyName("kind")]
    public string Kind { get; init; } = string.Empty;

    [JsonPropertyName("width")]
    public float Width { get; init; }

    [JsonPropertyName("waypoints")]
    public List<float[]> Waypoints { get; init; } = new();
}

public sealed class BuildingSpec
{
    [JsonPropertyName("id")]
    public string Id { get; init; } = string.Empty;

    [JsonPropertyName("region")]
    public string Region { get; init; } = string.Empty;

    [JsonPropertyName("prefab")]
    public string Prefab { get; init; } = string.Empty;

    [JsonPropertyName("position")]
    public float[] Position { get; init; } = Array.Empty<float>();

    [JsonPropertyName("rotation_deg")]
    public float[] RotationDegrees { get; init; } = Array.Empty<float>();

    [JsonPropertyName("scale")]
    public float[] Scale { get; init; } = Array.Empty<float>();
}

public sealed class PropSpec
{
    [JsonPropertyName("id")]
    public string Id { get; init; } = string.Empty;

    [JsonPropertyName("region")]
    public string Region { get; init; } = string.Empty;

    [JsonPropertyName("prefab")]
    public string Prefab { get; init; } = string.Empty;

    [JsonPropertyName("position")]
    public float[] Position { get; init; } = Array.Empty<float>();

    [JsonPropertyName("rotation_deg")]
    public float[] RotationDegrees { get; init; } = Array.Empty<float>();

    [JsonPropertyName("scale")]
    public float[] Scale { get; init; } = Array.Empty<float>();
}

public sealed class LightSpec
{
    [JsonPropertyName("id")]
    public string Id { get; init; } = string.Empty;

    [JsonPropertyName("kind")]
    public string Kind { get; init; } = string.Empty;

    [JsonPropertyName("position")]
    public float[] Position { get; init; } = Array.Empty<float>();

    [JsonPropertyName("rotation_deg")]
    public float[] RotationDegrees { get; init; } = Array.Empty<float>();

    [JsonPropertyName("color")]
    public string Color { get; init; } = "#ffffff";

    [JsonPropertyName("energy")]
    public float Energy { get; init; }

    [JsonPropertyName("range")]
    public float Range { get; init; }
}

public sealed class InteractionSpec
{
    [JsonPropertyName("player_spawn")]
    public PlayerSpawnSpec PlayerSpawn { get; init; } = new();

    [JsonPropertyName("interactables")]
    public List<InteractableSpec> Interactables { get; init; } = new();

    [JsonPropertyName("objectives")]
    public List<ObjectiveSpec> Objectives { get; init; } = new();

    [JsonPropertyName("exit")]
    public ExitSpec Exit { get; init; } = new();
}

public sealed class PlayerSpawnSpec
{
    [JsonPropertyName("region")]
    public string Region { get; init; } = string.Empty;

    [JsonPropertyName("position")]
    public float[] Position { get; init; } = Array.Empty<float>();
}

public sealed class InteractableSpec
{
    [JsonPropertyName("id")]
    public string Id { get; init; } = string.Empty;

    [JsonPropertyName("region")]
    public string Region { get; init; } = string.Empty;

    [JsonPropertyName("prefab")]
    public string Prefab { get; init; } = string.Empty;

    [JsonPropertyName("position")]
    public float[] Position { get; init; } = Array.Empty<float>();

    [JsonPropertyName("action")]
    public string Action { get; init; } = string.Empty;

    [JsonPropertyName("label")]
    public string Label { get; init; } = string.Empty;

    [JsonPropertyName("duration_ms")]
    public int DurationMilliseconds { get; init; }
}

public sealed class ObjectiveSpec
{
    [JsonPropertyName("id")]
    public string Id { get; init; } = string.Empty;

    [JsonPropertyName("rule")]
    public string Rule { get; init; } = string.Empty;

    [JsonPropertyName("targets")]
    public List<string> Targets { get; init; } = new();

    [JsonPropertyName("completion_text")]
    public string CompletionText { get; init; } = string.Empty;
}

public sealed class ExitSpec
{
    [JsonPropertyName("id")]
    public string Id { get; init; } = string.Empty;

    [JsonPropertyName("region")]
    public string Region { get; init; } = string.Empty;

    [JsonPropertyName("position")]
    public float[] Position { get; init; } = Array.Empty<float>();

    [JsonPropertyName("requires")]
    public List<string> Requires { get; init; } = new();
}

public sealed class CameraSpec
{
    [JsonPropertyName("id")]
    public string Id { get; init; } = string.Empty;

    [JsonPropertyName("kind")]
    public string Kind { get; init; } = string.Empty;

    [JsonPropertyName("position")]
    public float[] Position { get; init; } = Array.Empty<float>();

    [JsonPropertyName("look_at")]
    public float[] LookAt { get; init; } = Array.Empty<float>();

    [JsonPropertyName("fov_deg")]
    public float FovDegrees { get; init; }

    [JsonPropertyName("resolution")]
    public int[] Resolution { get; init; } = Array.Empty<int>();
}
