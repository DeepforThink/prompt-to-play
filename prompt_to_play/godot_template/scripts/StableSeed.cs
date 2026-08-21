using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;

namespace PromptToPlay;

public static class StableSeed
{
    /// <summary>
    /// Matches prompt_to_play.contracts.derive_world_seed. Reference order is
    /// semantic; machine-local filenames are intentionally excluded.
    /// </summary>
    public static uint DeriveWorld(string prompt, IEnumerable<string> referenceSha256)
    {
        using var payload = new MemoryStream();
        using (var writer = new Utf8JsonWriter(
                   payload,
                   new JsonWriterOptions
                   {
                       Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
                       Indented = false,
                   }))
        {
            writer.WriteStartObject();
            writer.WriteString("prompt", prompt);
            writer.WritePropertyName("reference_sha256");
            writer.WriteStartArray();
            foreach (string digest in referenceSha256)
            {
                writer.WriteStringValue(digest);
            }
            writer.WriteEndArray();
            writer.WriteEndObject();
        }

        byte[] digestBytes = SHA256.HashData(payload.ToArray());
        return BinaryPrimitives.ReadUInt32BigEndian(digestBytes);
    }

    /// <summary>
    /// Matches prompt_to_play.contracts.derive_seed: the first four SHA-256
    /// bytes of "&lt;root seed&gt;:&lt;stable id&gt;", interpreted big-endian.
    /// </summary>
    public static uint Derive(uint rootSeed, string stableId)
    {
        byte[] payload = Encoding.UTF8.GetBytes($"{rootSeed}:{stableId}");
        byte[] digest = SHA256.HashData(payload);
        return BinaryPrimitives.ReadUInt32BigEndian(digest);
    }
}
