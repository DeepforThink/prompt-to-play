using System.Security.Cryptography;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using Godot;

namespace PromptToPlay;

public sealed class ManifestEntity
{
    [JsonPropertyName("stable_id")]
    public string StableId { get; init; } = string.Empty;

    [JsonPropertyName("kind")]
    public string Kind { get; init; } = string.Empty;

    [JsonPropertyName("node_path")]
    public string NodePath { get; init; } = string.Empty;

    [JsonPropertyName("seed")]
    public uint Seed { get; init; }

    [JsonPropertyName("position")]
    public float[] Position { get; init; } = Array.Empty<float>();
}

public sealed class StructuralCheck
{
    [JsonPropertyName("id")]
    public string Id { get; init; } = string.Empty;

    [JsonPropertyName("kind")]
    public string Kind { get; init; } = "hard";

    [JsonPropertyName("passed")]
    public bool Passed { get; init; }

    [JsonPropertyName("score")]
    public int Score => Passed ? 1 : 0;

    [JsonPropertyName("message")]
    public string Message { get; init; } = string.Empty;

    [JsonPropertyName("evidence")]
    public string[] Evidence { get; init; } = Array.Empty<string>();
}

public sealed class CaptureArtifact
{
    [JsonPropertyName("camera_id")]
    public string CameraId { get; init; } = string.Empty;

    [JsonPropertyName("path")]
    public string Path { get; init; } = string.Empty;

    [JsonPropertyName("resolution")]
    public int[] Resolution { get; init; } = Array.Empty<int>();

    [JsonPropertyName("sha256")]
    public string Sha256 { get; init; } = string.Empty;
}

public static class ArtifactWriter
{
    public static string RunId { get; } = ResolveRunId();
    public static int Revision { get; } = ResolveRevision();
    public static string ArtifactDirectory =>
        $"res://artifacts/runs/{RunId}/rev_{Revision}";

    public static string CaptureRelativePath(string cameraId) =>
        $"artifacts/runs/{RunId}/rev_{Revision}/captures/{cameraId}.png";

    public static string CaptureAbsolutePath(string cameraId)
    {
        string directory = ProjectSettings.GlobalizePath($"{ArtifactDirectory}/captures");
        Directory.CreateDirectory(directory);
        return Path.Combine(directory, $"{cameraId}.png");
    }

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true,
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    public static string SourceSha256(string sourceJson)
    {
        byte[] digest = SHA256.HashData(Encoding.UTF8.GetBytes(sourceJson));
        return Convert.ToHexString(digest).ToLowerInvariant();
    }

    public static void WriteSuccess(
        WorldSpec spec,
        string sourceJson,
        long buildMilliseconds,
        IEnumerable<ManifestEntity> entities,
        IEnumerable<string> primitiveFallbackIds,
        IReadOnlyList<StructuralCheck> checks)
    {
        ManifestEntity[] sortedEntities = entities
            .OrderBy(entity => entity.StableId, StringComparer.Ordinal)
            .ToArray();
        string[] sortedFallbacks = primitiveFallbackIds
            .OrderBy(id => id, StringComparer.Ordinal)
            .ToArray();

        var manifest = new
        {
            schema = "prompt-to-play/build-manifest@1",
            run_id = RunId,
            revision = Revision,
            world_id = spec.WorldId,
            world_source_sha256 = SourceSha256(sourceJson),
            root_seed = spec.Seed,
            engine_target = "Godot 4.7.1 .NET",
            spec_path = "spec/world.json",
            scene_path = "scenes/Main.tscn",
            build_ms = buildMilliseconds,
            entity_count = sortedEntities.Length,
            primitive_fallback_ids = sortedFallbacks,
            entities = sortedEntities,
        };

        bool passed = checks.All(check => check.Passed);
        var report = new
        {
            schema = "prompt-to-play/structural-report@1",
            run_id = RunId,
            revision = Revision,
            world_id = spec.WorldId,
            world_source_sha256 = SourceSha256(sourceJson),
            status = passed ? "pass" : "fail",
            checks,
            summary = new
            {
                passed = checks.Count(check => check.Passed),
                failed = checks.Count(check => !check.Passed),
                generated_entities = sortedEntities.Length,
            },
        };

        WriteJson("build_manifest.json", manifest);
        WriteJson("structural_report.json", report);
    }

    public static void WriteFailure(string message)
    {
        var report = new
        {
            schema = "prompt-to-play/structural-report@1",
            run_id = RunId,
            revision = Revision,
            status = "error",
            checks = new[]
            {
                new StructuralCheck
                {
                    Id = "scene_loads",
                    Passed = false,
                    Message = message,
                },
            },
        };
        WriteJson("structural_report.json", report);
    }

    public static void WriteCaptureManifest(
        WorldSpec spec,
        IReadOnlyCollection<CaptureArtifact> captures)
    {
        var manifest = new
        {
            schema = "prompt-to-play/capture-manifest@1",
            run_id = RunId,
            revision = Revision,
            world_id = spec.WorldId,
            renderer = RenderingServer.GetCurrentRenderingMethod(),
            display_driver = DisplayServer.GetName(),
            captures = captures.OrderBy(
                capture => capture.CameraId,
                StringComparer.Ordinal),
        };
        WriteJson("capture_manifest.json", manifest);
    }

    private static string ResolveRunId()
    {
        string value = OS.GetEnvironment("PTP_RUN_ID");
        if (string.IsNullOrWhiteSpace(value))
        {
            return "local-smoke";
        }
        if (!Regex.IsMatch(value, "^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"))
        {
            throw new InvalidDataException("PTP_RUN_ID is not a safe identifier.");
        }
        return value;
    }

    private static int ResolveRevision()
    {
        string value = OS.GetEnvironment("PTP_REVISION");
        if (string.IsNullOrWhiteSpace(value))
        {
            return 0;
        }
        if (!int.TryParse(value, out int revision) || revision is < 0 or > 2)
        {
            throw new InvalidDataException("PTP_REVISION must be 0, 1, or 2.");
        }
        return revision;
    }

    private static void WriteJson(string fileName, object document)
    {
        string directory = ProjectSettings.GlobalizePath(ArtifactDirectory);
        Directory.CreateDirectory(directory);
        string path = Path.Combine(directory, fileName);
        System.IO.File.WriteAllText(
            path,
            JsonSerializer.Serialize(document, JsonOptions) + "\n",
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
        GD.Print($"Prompt-to-Play artifact: {path}");
    }
}
