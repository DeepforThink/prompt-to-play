using System.Security.Cryptography;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using Godot;

namespace PromptToPlay.Direct;

internal sealed class DirectStructuralCheck
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

internal sealed class DirectCaptureArtifact
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

internal static class DirectArtifactWriter
{
    private const string DefaultRunId = "local-smoke";
    private static readonly Regex SafeIdentifier = new(
        "^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$",
        RegexOptions.CultureInvariant);

    public static string RequestedRunId { get; } = OS.GetEnvironment("PTP_RUN_ID");
    public static string RequestedRevision { get; } = OS.GetEnvironment("PTP_REVISION");
    public static string RequestedProjectSha256 { get; } =
        OS.GetEnvironment("PTP_PROJECT_SHA256").ToLowerInvariant();
    public static string RunId { get; } = ResolveRunId();
    public static int Revision { get; } = ResolveRevision();
    public static string ProjectSha256 { get; } = ResolveProjectSha256();
    public static bool ConfigurationIsSafe =>
        (string.IsNullOrWhiteSpace(RequestedRunId) ||
         SafeIdentifier.IsMatch(RequestedRunId)) &&
        (string.IsNullOrWhiteSpace(RequestedRevision) ||
         int.TryParse(RequestedRevision, out int revision) && revision is >= 0 and <= 6) &&
        (string.IsNullOrWhiteSpace(RequestedProjectSha256) ||
         Regex.IsMatch(RequestedProjectSha256, "^[a-f0-9]{64}$"));

    public static string ArtifactDirectory =>
        $"res://artifacts/runs/{RunId}/rev_{Revision}";

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true,
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    public static void WriteStatus(string phase, string message, int? exitCode = null)
    {
        var document = new
        {
            schema = "prompt-to-play/direct-status@1",
            run_id = RunId,
            revision = Revision,
            project_sha256 = NullIfEmpty(ProjectSha256),
            phase,
            message,
            exit_code = exitCode,
            updated_at_utc = DateTimeOffset.UtcNow.ToString("O"),
        };
        WriteJson("direct_status.json", document);
    }

    public static void WriteStructuralReport(
        IReadOnlyCollection<DirectStructuralCheck> checks,
        string entryScene,
        long startupMilliseconds,
        DirectInteractionProbeResult interactionProbe)
    {
        DirectStructuralCheck[] orderedChecks = checks
            .OrderBy(check => check.Id, StringComparer.Ordinal)
            .ToArray();
        bool passed = orderedChecks.All(check => check.Passed);
        var document = new
        {
            schema = "prompt-to-play/direct-structural-report@1",
            mode = "direct-project",
            run_id = RunId,
            revision = Revision,
            project_sha256 = NullIfEmpty(ProjectSha256),
            status = passed ? "pass" : "fail",
            engine_target = "Godot 4.7.1 .NET",
            entry_scene = entryScene,
            startup_ms = startupMilliseconds,
            interaction_probe = interactionProbe,
            checks = orderedChecks,
            summary = new
            {
                passed = orderedChecks.Count(check => check.Passed),
                failed = orderedChecks.Count(check => !check.Passed),
            },
        };
        WriteJson("direct_structural_report.json", document);
    }

    public static void WriteCaptureManifest(
        IReadOnlyCollection<DirectCaptureArtifact> captures,
        string status,
        string? error = null)
    {
        var document = new
        {
            schema = "prompt-to-play/direct-capture-manifest@1",
            mode = "direct-project",
            run_id = RunId,
            revision = Revision,
            project_sha256 = NullIfEmpty(ProjectSha256),
            status,
            renderer = RenderingServer.GetCurrentRenderingMethod(),
            display_driver = DisplayServer.GetName(),
            error,
            captures = captures.OrderBy(
                capture => capture.CameraId,
                StringComparer.Ordinal),
        };
        WriteJson("capture_manifest.json", document);
    }

    public static string CaptureRelativePath(string cameraId) =>
        $"artifacts/runs/{RunId}/rev_{Revision}/captures/{SafeCameraId(cameraId)}.png";

    public static string CaptureAbsolutePath(string cameraId)
    {
        string directory = ProjectSettings.GlobalizePath($"{ArtifactDirectory}/captures");
        Directory.CreateDirectory(directory);
        return Path.Combine(directory, $"{SafeCameraId(cameraId)}.png");
    }

    public static string FileSha256(string path)
    {
        byte[] digest = SHA256.HashData(System.IO.File.ReadAllBytes(path));
        return Convert.ToHexString(digest).ToLowerInvariant();
    }

    private static string ResolveRunId()
    {
        string requested = RequestedRunId;
        return !string.IsNullOrWhiteSpace(requested) && SafeIdentifier.IsMatch(requested)
            ? requested
            : DefaultRunId;
    }

    private static int ResolveRevision()
    {
        string requested = RequestedRevision;
        return int.TryParse(requested, out int revision) && revision is >= 0 and <= 6
            ? revision
            : 0;
    }

    private static string ResolveProjectSha256() =>
        !string.IsNullOrWhiteSpace(RequestedProjectSha256) &&
        Regex.IsMatch(RequestedProjectSha256, "^[a-f0-9]{64}$")
            ? RequestedProjectSha256
            : string.Empty;

    private static string? NullIfEmpty(string value) =>
        string.IsNullOrWhiteSpace(value) ? null : value;

    private static string SafeCameraId(string cameraId)
    {
        string value = Regex.Replace(cameraId, "[^A-Za-z0-9_.-]", "-");
        if (string.IsNullOrWhiteSpace(value))
        {
            return "camera";
        }
        return value.Length <= 48 ? value : value[..48];
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
        GD.Print($"Prompt-to-Play direct artifact: {path}");
    }
}
