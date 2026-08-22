using System.Diagnostics;
using Godot;

namespace PromptToPlay.Direct;

public partial class DirectHarness : Node
{
    public const string GeneratedEntryScene = "res://generated/GeneratedGame.tscn";

    private readonly Stopwatch _startupClock = Stopwatch.StartNew();
    private Node? _generatedRoot;
    private bool _entryExists;
    private bool _entryInstantiated;
    private string _startupError = string.Empty;
    private DirectInteractionProbeResult _interactionProbe =
        DirectInteractionProbeResult.NotRequested();

    public override void _Ready()
    {
        DirectArtifactWriter.WriteStatus(
            "starting",
            $"Loading {GeneratedEntryScene}.");

        try
        {
            _entryExists = ResourceLoader.Exists(GeneratedEntryScene, "PackedScene");
            if (!_entryExists)
            {
                throw new FileNotFoundException(
                    $"Generated entry scene is missing: {GeneratedEntryScene}");
            }

            PackedScene? scene = ResourceLoader.Load<PackedScene>(GeneratedEntryScene);
            if (scene is null)
            {
                throw new InvalidDataException(
                    $"Generated entry is not a PackedScene: {GeneratedEntryScene}");
            }

            _generatedRoot = scene.Instantiate();
            _generatedRoot.Name = "GeneratedGame";
            AddChild(_generatedRoot);
            _entryInstantiated = _generatedRoot.IsInsideTree();
        }
        catch (Exception exception)
        {
            _startupError = exception.GetBaseException().Message;
            GD.PushError($"Direct project startup failed: {exception}");
        }

        CallDeferred(nameof(FinalizeStartup));
    }

    private async void FinalizeStartup()
    {
        // Give generated scripts two frames to create their scene tree before
        // the stable harness inspects it or renders a capture.
        await ToSignal(GetTree(), SceneTree.SignalName.ProcessFrame);
        await ToSignal(GetTree(), SceneTree.SignalName.ProcessFrame);

        bool probeRequested = OS.GetEnvironment("PTP_AUTOMATION") == "1" ||
            OS.GetEnvironment("PTP_VERIFY") == "1" ||
            OS.GetEnvironment("PTP_CAPTURE") == "1";
        if (probeRequested)
        {
            _interactionProbe = await DirectInteractionProbe.RunAsync(
                this,
                _generatedRoot);
        }

        IReadOnlyCollection<DirectStructuralCheck> checks = EvaluateStructure();
        DirectArtifactWriter.WriteStructuralReport(
            checks,
            GeneratedEntryScene,
            _startupClock.ElapsedMilliseconds,
            _interactionProbe);

        bool passed = checks.All(check => check.Passed);
        int exitCode = passed ? 0 : 1;
        DirectArtifactWriter.WriteStatus(
            passed ? "running" : "failed",
            passed
                ? "Generated project started and passed direct structural checks."
                : "Generated project failed one or more direct structural checks.",
            passed ? null : exitCode);

        if (OS.GetEnvironment("PTP_CAPTURE") == "1")
        {
            await CaptureAsync(exitCode);
            return;
        }

        if (OS.GetEnvironment("PTP_AUTOMATION") == "1" ||
            OS.GetEnvironment("PTP_VERIFY") == "1" ||
            !passed)
        {
            GetTree().Quit(exitCode);
        }
    }

    private IReadOnlyCollection<DirectStructuralCheck> EvaluateStructure()
    {
        int cameraCount = GetTree().GetNodesInGroup("ptp_capture_camera")
            .Count(IsSupportedCamera);
        int renderableCount = CountRenderableDescendants(_generatedRoot);
        int gameplayRootCount = GetTree().GetNodesInGroup("ptp_gameplay").Count;
        int playerCount = GetTree().GetNodesInGroup("ptp_player").Count;
        int objectiveCount = GetTree().GetNodesInGroup("ptp_objective").Count;
        int hudCount = GetTree().GetNodesInGroup("ptp_hud").Count;
        bool safeConfiguration = DirectArtifactWriter.ConfigurationIsSafe;

        return new DirectStructuralCheck[]
        {
            new()
            {
                Id = "harness_ready",
                Passed = true,
                Message = "The stable direct-project harness entered the scene tree.",
            },
            new()
            {
                Id = "run_identifier_safe",
                Passed = safeConfiguration,
                Message = safeConfiguration
                    ? "The artifact run identifier is path-safe."
                    : "PTP_RUN_ID was rejected; artifacts used the safe local fallback.",
            },
            new()
            {
                Id = "generated_entry_exists",
                Passed = _entryExists,
                Message = _entryExists
                    ? $"Found {GeneratedEntryScene}."
                    : $"Missing {GeneratedEntryScene}.",
            },
            new()
            {
                Id = "generated_entry_instantiates",
                Passed = _entryInstantiated,
                Message = _entryInstantiated
                    ? "The generated entry scene instantiated successfully."
                    : $"The generated entry did not instantiate. {_startupError}".Trim(),
            },
            new()
            {
                Id = "gameplay_root_declared",
                Passed = gameplayRootCount > 0,
                Message = $"Found {gameplayRootCount} node(s) in ptp_gameplay.",
            },
            new()
            {
                Id = "renderable_content",
                Passed = renderableCount > 0,
                Message = $"Found {renderableCount} visible 2D/3D render node(s).",
            },
            new()
            {
                Id = "capture_camera_available",
                Passed = cameraCount is >= 1 and <= 2,
                Message = $"Found {cameraCount} capture camera(s); expected one or two.",
            },
            new()
            {
                Id = "player_declared",
                Passed = playerCount > 0,
                Message = $"Found {playerCount} node(s) in ptp_player.",
            },
            new()
            {
                Id = "objective_declared",
                Passed = objectiveCount > 0,
                Message = $"Found {objectiveCount} node(s) in ptp_objective.",
            },
            new()
            {
                Id = "hud_declared",
                Passed = hudCount > 0,
                Message = $"Found {hudCount} node(s) in ptp_hud.",
            },
            new()
            {
                Id = "interaction_responds_to_input",
                Passed = !_interactionProbe.Required ||
                    (_interactionProbe.TargetCount > 0 &&
                     _interactionProbe.DeclaredActionCount > 0 &&
                     _interactionProbe.AttemptedActionCount > 0 &&
                     _interactionProbe.StateChanged),
                Message = !_interactionProbe.Required
                    ? "Interaction probe was not requested for this interactive launch."
                    : _interactionProbe.StateChanged
                        ? "A declared input action changed trusted observable player state."
                        : "No declared input action changed trusted observable player state.",
                Evidence = _interactionProbe.Evidence,
            },
        };
    }

    private async Task CaptureAsync(int structuralExitCode)
    {
        var captures = new List<DirectCaptureArtifact>();
        try
        {
            if (DisplayServer.GetName().Equals("headless", StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidOperationException(
                    "PTP_CAPTURE=1 requires a rendering display driver.");
            }

            Node[] cameras = GetTree().GetNodesInGroup("ptp_capture_camera")
                .Where(IsSupportedCamera)
                .OrderBy(CameraId, StringComparer.Ordinal)
                .Take(2)
                .ToArray();
            if (cameras.Length == 0)
            {
                throw new InvalidOperationException(
                    "The generated project did not register a capture camera.");
            }

            foreach (CanvasItem hidden in GetTree().GetNodesInGroup("ptp_capture_hidden")
                         .OfType<CanvasItem>())
            {
                hidden.Visible = false;
            }

            foreach (Node camera in cameras)
            {
                string cameraId = CameraId(camera);
                if (camera is Camera3D camera3D)
                {
                    camera3D.MakeCurrent();
                }
                else if (camera is Camera2D camera2D)
                {
                    camera2D.MakeCurrent();
                }
                for (int frame = 0; frame < 4; frame++)
                {
                    await ToSignal(GetTree(), SceneTree.SignalName.ProcessFrame);
                }
                await ToSignal(
                    RenderingServer.Singleton,
                    RenderingServer.SignalName.FramePostDraw);

                Image image = GetViewport().GetTexture().GetImage();
                if (image.IsEmpty() || !HasVisualVariation(image))
                {
                    throw new InvalidOperationException(
                        $"Capture camera {cameraId} produced an empty or near-uniform frame.");
                }

                string absolutePath = DirectArtifactWriter.CaptureAbsolutePath(cameraId);
                Error saveError = image.SavePng(absolutePath);
                if (saveError != Error.Ok)
                {
                    throw new IOException(
                        $"Could not save capture {cameraId}: {saveError}.");
                }

                captures.Add(new DirectCaptureArtifact
                {
                    CameraId = cameraId,
                    Path = DirectArtifactWriter.CaptureRelativePath(cameraId),
                    Resolution = new[] { image.GetWidth(), image.GetHeight() },
                    Sha256 = DirectArtifactWriter.FileSha256(absolutePath),
                });
            }

            DirectArtifactWriter.WriteCaptureManifest(captures, "pass");
            DirectArtifactWriter.WriteStatus(
                "captured",
                $"Saved {captures.Count} deterministic evaluation capture(s).",
                structuralExitCode);
            GetTree().Quit(structuralExitCode);
        }
        catch (Exception exception)
        {
            DirectArtifactWriter.WriteCaptureManifest(
                captures,
                "error",
                exception.GetBaseException().Message);
            DirectArtifactWriter.WriteStatus(
                "capture_failed",
                exception.GetBaseException().Message,
                1);
            GD.PushError($"Direct project capture failed: {exception}");
            GetTree().Quit(1);
        }
    }

    private static bool IsSupportedCamera(Node node) =>
        node is Camera3D or Camera2D;

    private static string CameraId(Node camera)
    {
        Variant value = camera.GetMeta("capture_id", camera.Name.ToString());
        string id = value.AsString();
        return string.IsNullOrWhiteSpace(id) ? camera.Name.ToString() : id;
    }

    private static int CountRenderableDescendants(Node? root)
    {
        if (root is null)
        {
            return 0;
        }

        int count = root is GeometryInstance3D or CanvasItem ? 1 : 0;
        foreach (Node child in root.GetChildren())
        {
            count += CountRenderableDescendants(child);
        }
        return count;
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
}
