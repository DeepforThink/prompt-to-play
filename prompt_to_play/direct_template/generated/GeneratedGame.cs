using Godot;

namespace PromptToPlay.Generated;

// This file is the primary LLM-owned implementation surface. The harness only
// requires the matching scene, one ptp_gameplay node, visible 3D content, and
// one or two Camera3D nodes in ptp_capture_camera.
public partial class GeneratedGame : Node3D
{
    private CharacterBody3D? _player;
    private float _pitch;

    public override void _Ready()
    {
        AddToGroup("ptp_gameplay");
        EnsureInputAction("move_forward", Key.W);
        EnsureInputAction("move_back", Key.S);
        EnsureInputAction("move_left", Key.A);
        EnsureInputAction("move_right", Key.D);
        EnsureInputAction("jump", Key.Space);
        BuildEnvironment();
        BuildGround();
        BuildLandmarks();
        BuildPlayer();
        BuildOverviewCamera();
        BuildHud();
    }

    public override void _PhysicsProcess(double delta)
    {
        if (_player is null)
        {
            return;
        }

        float horizontal = Input.GetAxis("move_left", "move_right");
        float forward = Input.GetAxis("move_back", "move_forward");
        Vector3 input = new(horizontal, 0.0f, -forward);
        Vector3 direction = (_player.Basis * input).Normalized();
        Vector3 velocity = _player.Velocity;
        velocity.X = direction.X * 7.0f;
        velocity.Z = direction.Z * 7.0f;
        velocity.Y = _player.IsOnFloor()
            ? (Input.IsActionPressed("jump") ? 6.0f : 0.0f)
            : velocity.Y - 18.0f * (float)delta;
        _player.Velocity = velocity;
        _player.MoveAndSlide();
    }

    public override void _UnhandledInput(InputEvent @event)
    {
        if (_player is null)
        {
            return;
        }

        if (@event is InputEventKey key && key.Keycode == Key.Escape && key.Pressed)
        {
            Input.MouseMode = Input.MouseModeEnum.Visible;
        }
        else if (@event is InputEventMouseButton button && button.Pressed)
        {
            Input.MouseMode = Input.MouseModeEnum.Captured;
        }
        else if (@event is InputEventMouseMotion motion &&
                 Input.MouseMode == Input.MouseModeEnum.Captured)
        {
            _player.RotateY(-motion.Relative.X * 0.0025f);
            _pitch = Mathf.Clamp(_pitch - motion.Relative.Y * 0.0025f, -1.2f, 1.2f);
            Camera3D? camera = _player.GetNodeOrNull<Camera3D>("PlayerCamera");
            if (camera is not null)
            {
                camera.Rotation = new Vector3(_pitch, 0.0f, 0.0f);
            }
        }
    }

    private void BuildEnvironment()
    {
        var environment = new Godot.Environment
        {
            BackgroundMode = Godot.Environment.BGMode.Color,
            BackgroundColor = new Color("17213b"),
            AmbientLightSource = Godot.Environment.AmbientSource.Color,
            AmbientLightColor = new Color("9fb8dc"),
            AmbientLightEnergy = 0.65f,
        };
        AddChild(new WorldEnvironment
        {
            Name = "WorldEnvironment",
            Environment = environment,
        });

        AddChild(new DirectionalLight3D
        {
            Name = "Sun",
            RotationDegrees = new Vector3(-58.0f, -34.0f, 0.0f),
            LightColor = new Color("ffe3bd"),
            LightEnergy = 1.25f,
            ShadowEnabled = true,
        });
    }

    private void BuildGround()
    {
        var ground = new StaticBody3D { Name = "Ground" };
        ground.AddToGroup("ptp_walkable");
        ground.AddChild(new MeshInstance3D
        {
            Name = "GroundMesh",
            Mesh = new BoxMesh { Size = new Vector3(40.0f, 0.5f, 40.0f) },
            MaterialOverride = Material(new Color("273f52"), 0.05f, 0.75f),
        });
        ground.AddChild(new CollisionShape3D
        {
            Name = "GroundCollision",
            Shape = new BoxShape3D { Size = new Vector3(40.0f, 0.5f, 40.0f) },
        });
        ground.Position = new Vector3(0.0f, -0.25f, 0.0f);
        AddChild(ground);
    }

    private void BuildLandmarks()
    {
        Color[] colors =
        {
            new("e06c75"), new("61afef"), new("98c379"), new("e5c07b"),
        };
        Vector3[] positions =
        {
            new(-7.0f, 1.0f, -6.0f), new(7.0f, 1.5f, -5.0f),
            new(-6.0f, 2.0f, 5.0f), new(6.0f, 1.25f, 6.0f),
        };
        for (int index = 0; index < positions.Length; index++)
        {
            var landmark = new MeshInstance3D
            {
                Name = $"Landmark{index + 1}",
                Position = positions[index],
                Mesh = new CylinderMesh
                {
                    TopRadius = 0.9f,
                    BottomRadius = 1.4f,
                    Height = positions[index].Y * 2.0f,
                },
                MaterialOverride = Material(colors[index], 0.35f, 0.35f),
            };
            AddChild(landmark);
        }

        var goal = new MeshInstance3D
        {
            Name = "Goal",
            Position = new Vector3(0.0f, 1.25f, -10.0f),
            Mesh = new SphereMesh { Radius = 1.25f, Height = 2.5f },
            MaterialOverride = Material(
                new Color("56d6c9"),
                0.15f,
                0.2f,
                new Color("2aa198")),
        };
        goal.AddToGroup("ptp_goal");
        goal.AddToGroup("ptp_objective");
        AddChild(goal);
    }

    private void BuildPlayer()
    {
        _player = new CharacterBody3D
        {
            Name = "Player",
            Position = new Vector3(0.0f, 1.2f, 10.0f),
            CollisionLayer = 1,
            CollisionMask = 1,
        };
        _player.AddToGroup("ptp_player");
        _player.AddToGroup("ptp_interaction_probe");
        _player.SetMeta("ptp_probe_actions", "move_forward");
        _player.AddChild(new CollisionShape3D
        {
            Name = "Collision",
            Shape = new CapsuleShape3D { Radius = 0.45f, Height = 1.8f },
        });

        var camera = new Camera3D
        {
            Name = "PlayerCamera",
            Position = new Vector3(0.0f, 0.65f, 0.0f),
            Current = true,
            Fov = 72.0f,
        };
        camera.AddToGroup("ptp_capture_camera");
        camera.SetMeta("capture_id", "player");
        _player.AddChild(camera);
        AddChild(_player);
    }

    private void BuildOverviewCamera()
    {
        var camera = new Camera3D
        {
            Name = "OverviewCamera",
            Position = new Vector3(18.0f, 17.0f, 20.0f),
            Fov = 58.0f,
        };
        camera.LookAtFromPosition(camera.Position, Vector3.Zero, Vector3.Up);
        camera.AddToGroup("ptp_capture_camera");
        camera.SetMeta("capture_id", "overview");
        AddChild(camera);
    }

    private void BuildHud()
    {
        var canvas = new CanvasLayer { Name = "Hud" };
        canvas.AddToGroup("ptp_capture_hidden");
        canvas.AddToGroup("ptp_hud");
        var label = new Label
        {
            Name = "Instructions",
            Position = new Vector2(20.0f, 18.0f),
            Text = "DIRECT PROMPT GAME  ·  WASD move  ·  SPACE jump  ·  mouse look",
        };
        label.AddThemeFontSizeOverride("font_size", 18);
        canvas.AddChild(label);
        AddChild(canvas);
    }

    private static StandardMaterial3D Material(
        Color color,
        float metallic,
        float roughness,
        Color? emission = null)
    {
        var material = new StandardMaterial3D
        {
            AlbedoColor = color,
            Metallic = metallic,
            Roughness = roughness,
        };
        if (emission is Color glow)
        {
            material.EmissionEnabled = true;
            material.Emission = glow;
            material.EmissionEnergyMultiplier = 2.0f;
        }
        return material;
    }

    private static void EnsureInputAction(StringName action, Key key)
    {
        if (!InputMap.HasAction(action))
        {
            InputMap.AddAction(action);
        }
        bool alreadyBound = InputMap.ActionGetEvents(action)
            .OfType<InputEventKey>()
            .Any(input => input.PhysicalKeycode == key);
        if (!alreadyBound)
        {
            InputMap.ActionAddEvent(action, new InputEventKey
            {
                PhysicalKeycode = key,
            });
        }
    }
}
