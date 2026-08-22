using System.Text.Json;
using System.Text.Json.Serialization;
using Godot;

namespace PromptToPlay;

public sealed class AssetCatalogDocument
{
    [JsonPropertyName("schema")]
    public string Schema { get; init; } = string.Empty;

    [JsonPropertyName("assets")]
    public List<AssetCatalogEntry> Assets { get; init; } = new();
}

public sealed class AssetCatalogEntry
{
    [JsonPropertyName("prefab")]
    public string Prefab { get; init; } = string.Empty;

    [JsonPropertyName("scene_path")]
    public string ScenePath { get; init; } = string.Empty;
}

public sealed class AssetCatalog
{
    public const string CatalogPath = "res://assets/catalog.json";
    public const string CatalogSchema = "prompt-to-play/asset-catalog@1";

    private readonly Dictionary<string, AssetCatalogEntry> _entries;

    public string LoadIssue { get; }

    private AssetCatalog(
        Dictionary<string, AssetCatalogEntry> entries,
        string loadIssue = "")
    {
        _entries = entries;
        LoadIssue = loadIssue;
    }

    public static AssetCatalog LoadDefault()
    {
        var entries = new Dictionary<string, AssetCatalogEntry>(StringComparer.Ordinal);
        try
        {
            if (!Godot.FileAccess.FileExists(CatalogPath))
            {
                const string issue = $"Asset catalog is missing: {CatalogPath}";
                GD.PushWarning(issue);
                return new AssetCatalog(entries, issue);
            }

            string source = Godot.FileAccess.GetFileAsString(CatalogPath);
            AssetCatalogDocument? document = JsonSerializer.Deserialize<AssetCatalogDocument>(
                source,
                new JsonSerializerOptions
                {
                    PropertyNameCaseInsensitive = false,
                    UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
                });
            if (document is null)
            {
                const string issue = "Asset catalog deserialized to null.";
                GD.PushWarning(issue);
                return new AssetCatalog(entries, issue);
            }
            if (document.Schema != CatalogSchema)
            {
                string issue = $"Expected asset catalog schema {CatalogSchema}.";
                GD.PushWarning(issue);
                return new AssetCatalog(entries, issue);
            }

            foreach (AssetCatalogEntry entry in document.Assets)
            {
                if (string.IsNullOrWhiteSpace(entry.Prefab) ||
                    string.IsNullOrWhiteSpace(entry.ScenePath))
                {
                    continue;
                }
                if (!entries.TryAdd(entry.Prefab, entry))
                {
                    GD.PushWarning(
                        $"Ignoring duplicate asset catalog prefab '{entry.Prefab}'. " +
                        "Prefab matching is exact and deterministic.");
                }
            }

            return new AssetCatalog(entries);
        }
        catch (Exception exception)
        {
            string issue = $"Asset catalog could not be read: {exception.Message}";
            GD.PushWarning(issue);
            return new AssetCatalog(entries, issue);
        }
    }

    public bool TryGetExact(string prefab, out AssetCatalogEntry entry)
    {
        if (_entries.TryGetValue(prefab, out AssetCatalogEntry? match))
        {
            entry = match;
            return true;
        }

        entry = null!;
        return false;
    }
}
