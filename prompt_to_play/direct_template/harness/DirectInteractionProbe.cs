using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using Godot;

namespace PromptToPlay.Direct;

internal sealed class DirectInteractionProbeResult
{
    [JsonPropertyName("required")]
    public bool Required { get; init; }

    [JsonPropertyName("status")]
    public string Status { get; init; } = "not_run";

    [JsonPropertyName("target_count")]
    public int TargetCount { get; init; }

    [JsonPropertyName("declared_action_count")]
    public int DeclaredActionCount { get; init; }

    [JsonPropertyName("attempted_action_count")]
    public int AttemptedActionCount { get; init; }

    [JsonPropertyName("state_changed")]
    public bool StateChanged { get; init; }

    [JsonPropertyName("evidence")]
    public string[] Evidence { get; init; } = Array.Empty<string>();

    public static DirectInteractionProbeResult NotRequested() => new()
    {
        Required = false,
        Status = "not_run",
        Evidence = new[] { "Interaction probing is enabled only for verification or capture runs." },
    };
}

internal static class DirectInteractionProbe
{
    public const string ProbeGroup = "ptp_interaction_probe";
    public const string ActionsMetadata = "ptp_probe_actions";

    private const int MaximumTargets = 8;
    private const int MaximumActions = 8;
    private const int SettlePhysicsFrames = 24;
    private const int PressPhysicsFrames = 10;
    private static readonly Regex SafeActionName = new(
        "^[A-Za-z0-9_.:-]{1,64}$",
        RegexOptions.CultureInvariant);

    public static async Task<DirectInteractionProbeResult> RunAsync(
        Node host,
        Node? generatedRoot)
    {
        var evidence = new List<string>();
        Node[] targets = host.GetTree().GetNodesInGroup(ProbeGroup)
            .Where(target => IsGeneratedDescendant(generatedRoot, target))
            .OrderBy(target => target.GetPath().ToString(), StringComparer.Ordinal)
            .Take(MaximumTargets)
            .ToArray();

        for (int frame = 0; frame < SettlePhysicsFrames; frame++)
        {
            await host.ToSignal(host.GetTree(), SceneTree.SignalName.PhysicsFrame);
        }

        int declaredActionCount = 0;
        int attemptedActionCount = 0;
        bool stateChanged = false;

        foreach (Node target in targets)
        {
            string[] actions = DeclaredActions(target);
            declaredActionCount += actions.Length;
            if (actions.Length == 0)
            {
                evidence.Add($"target={target.GetPath()}; declaration=missing");
                continue;
            }

            TargetSnapshot initial = TargetSnapshot.Capture(target);
            if (!initial.Observable)
            {
                evidence.Add($"target={target.GetPath()}; observable_state=missing");
                continue;
            }

            foreach (string action in actions)
            {
                if (attemptedActionCount >= MaximumActions)
                {
                    break;
                }
                if (!SafeActionName.IsMatch(action) || !InputMap.HasAction(action))
                {
                    evidence.Add(
                        $"target={target.GetPath()}; action={action}; input_map=missing");
                    continue;
                }

                attemptedActionCount++;
                initial.Restore();
                await host.ToSignal(host.GetTree(), SceneTree.SignalName.PhysicsFrame);
                TargetSnapshot before = TargetSnapshot.Capture(target);
                try
                {
                    Input.ParseInputEvent(new InputEventAction
                    {
                        Action = action,
                        Pressed = true,
                        Strength = 1.0f,
                    });
                    Input.ActionPress(action, 1.0f);
                    for (int frame = 0; frame < PressPhysicsFrames; frame++)
                    {
                        await host.ToSignal(
                            host.GetTree(),
                            SceneTree.SignalName.PhysicsFrame);
                    }
                }
                finally
                {
                    Input.ActionRelease(action);
                    Input.ParseInputEvent(new InputEventAction
                    {
                        Action = action,
                        Pressed = false,
                        Strength = 0.0f,
                    });
                }

                TargetSnapshot after = TargetSnapshot.Capture(target);
                bool changed = before.Fingerprint != after.Fingerprint;
                evidence.Add(
                    $"target={target.GetPath()}; action={action}; " +
                    $"before_sha256={FingerprintHash(before.Fingerprint)}; " +
                    $"after_sha256={FingerprintHash(after.Fingerprint)}; " +
                    $"changed={changed.ToString().ToLowerInvariant()}");
                initial.Restore();
                stateChanged |= changed;
                if (stateChanged)
                {
                    break;
                }
            }
            if (stateChanged || attemptedActionCount >= MaximumActions)
            {
                break;
            }
        }

        bool declared = targets.Length > 0 && declaredActionCount > 0;
        return new DirectInteractionProbeResult
        {
            Required = true,
            Status = declared && stateChanged ? "pass" : "fail",
            TargetCount = targets.Length,
            DeclaredActionCount = declaredActionCount,
            AttemptedActionCount = attemptedActionCount,
            StateChanged = stateChanged,
            Evidence = evidence.ToArray(),
        };
    }

    private static bool IsGeneratedDescendant(Node? generatedRoot, Node target) =>
        generatedRoot is not null &&
        (generatedRoot == target || generatedRoot.IsAncestorOf(target));

    private static string[] DeclaredActions(Node target)
    {
        if (!target.HasMeta(ActionsMetadata))
        {
            return Array.Empty<string>();
        }
        return target.GetMeta(ActionsMetadata).AsString()
            .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Where(action => !string.IsNullOrWhiteSpace(action))
            .Distinct(StringComparer.Ordinal)
            .Take(MaximumActions)
            .ToArray();
    }

    private static string FingerprintHash(string fingerprint)
    {
        byte[] digest = SHA256.HashData(Encoding.UTF8.GetBytes(fingerprint));
        return Convert.ToHexString(digest).ToLowerInvariant();
    }

    private sealed class TargetSnapshot
    {
        private readonly Action _restore;

        private TargetSnapshot(bool observable, string fingerprint, Action restore)
        {
            Observable = observable;
            Fingerprint = fingerprint;
            _restore = restore;
        }

        public bool Observable { get; }
        public string Fingerprint { get; }

        public void Restore()
        {
            _restore();
        }

        public static TargetSnapshot Capture(Node target)
        {
            if (!GodotObject.IsInstanceValid(target))
            {
                return new TargetSnapshot(true, "freed", () => { });
            }

            var values = new List<string>();
            var restorers = new List<Action>();
            if (target is Node3D node3D)
            {
                Transform3D transform = node3D.GlobalTransform;
                values.Add($"transform3d={transform}");
                restorers.Add(() => node3D.GlobalTransform = transform);
            }
            if (target is CharacterBody3D character3D)
            {
                Vector3 velocity = character3D.Velocity;
                values.Add($"character_velocity3d={velocity}");
                restorers.Add(() => character3D.Velocity = velocity);
            }
            else if (target is RigidBody3D rigid3D)
            {
                Vector3 linear = rigid3D.LinearVelocity;
                Vector3 angular = rigid3D.AngularVelocity;
                values.Add($"rigid_velocity3d={linear};{angular}");
                restorers.Add(() =>
                {
                    rigid3D.LinearVelocity = linear;
                    rigid3D.AngularVelocity = angular;
                });
            }
            if (target is Node2D node2D)
            {
                Transform2D transform = node2D.GlobalTransform;
                values.Add($"transform2d={transform}");
                restorers.Add(() => node2D.GlobalTransform = transform);
            }
            if (target is CharacterBody2D character2D)
            {
                Vector2 velocity = character2D.Velocity;
                values.Add($"character_velocity2d={velocity}");
                restorers.Add(() => character2D.Velocity = velocity);
            }
            else if (target is RigidBody2D rigid2D)
            {
                Vector2 linear = rigid2D.LinearVelocity;
                float angular = rigid2D.AngularVelocity;
                values.Add($"rigid_velocity2d={linear};{angular:R}");
                restorers.Add(() =>
                {
                    rigid2D.LinearVelocity = linear;
                    rigid2D.AngularVelocity = angular;
                });
            }
            if (target is Control control)
            {
                Vector2 position = control.Position;
                Vector2 size = control.Size;
                values.Add($"control={position};{size}");
                restorers.Add(() =>
                {
                    control.Position = position;
                    control.Size = size;
                });
            }
            if (target is Godot.Range range)
            {
                double value = range.Value;
                values.Add($"range={value:R}");
                restorers.Add(() => range.Value = value);
            }
            if (target is Label label)
            {
                string text = label.Text;
                values.Add($"label={text}");
                restorers.Add(() => label.Text = text);
            }
            if (target.HasMeta("ptp_probe_state"))
            {
                Variant state = target.GetMeta("ptp_probe_state");
                values.Add($"metadata={state}");
                restorers.Add(() => target.SetMeta("ptp_probe_state", state));
            }

            return new TargetSnapshot(
                values.Count > 0,
                string.Join("|", values),
                () =>
                {
                    if (!GodotObject.IsInstanceValid(target))
                    {
                        return;
                    }
                    foreach (Action restore in restorers)
                    {
                        restore();
                    }
                });
        }
    }
}
