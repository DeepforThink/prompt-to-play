using Godot;

namespace PromptToPlay;

public partial class PlayerController : CharacterBody3D
{
    private const float MoveSpeed = 8.0f;
    private const float GroundAcceleration = 28.0f;
    private const float AirAcceleration = 8.0f;
    private const float JumpVelocity = 7.5f;
    private const float MouseSensitivity = 0.0022f;

    private Node3D _head = null!;
    private float _gravity;

    public override void _Ready()
    {
        _head = GetNode<Node3D>("Head");
        _gravity = ProjectSettings.GetSetting("physics/3d/default_gravity", 9.8f).AsSingle();
        if (DisplayServer.GetName() != "headless")
        {
            Input.MouseMode = Input.MouseModeEnum.Captured;
        }
    }

    public override void _UnhandledInput(InputEvent inputEvent)
    {
        if (inputEvent.IsActionPressed("ui_cancel"))
        {
            Input.MouseMode = Input.MouseMode == Input.MouseModeEnum.Captured
                ? Input.MouseModeEnum.Visible
                : Input.MouseModeEnum.Captured;
            GetViewport().SetInputAsHandled();
            return;
        }

        if (inputEvent is not InputEventMouseMotion mouseMotion ||
            Input.MouseMode != Input.MouseModeEnum.Captured)
        {
            return;
        }

        RotateY(-mouseMotion.Relative.X * MouseSensitivity);
        Vector3 headRotation = _head.Rotation;
        headRotation.X = Mathf.Clamp(
            headRotation.X - mouseMotion.Relative.Y * MouseSensitivity,
            -1.45f,
            1.45f);
        _head.Rotation = headRotation;
    }

    public override void _PhysicsProcess(double deltaValue)
    {
        float delta = (float)deltaValue;
        Vector3 velocity = Velocity;

        if (!IsOnFloor())
        {
            velocity.Y -= _gravity * delta;
        }
        else if (Input.IsActionJustPressed("jump"))
        {
            velocity.Y = JumpVelocity;
        }

        Vector2 input = Input.GetVector(
            "move_left",
            "move_right",
            "move_forward",
            "move_back");
        Vector3 direction = Transform.Basis * new Vector3(input.X, 0.0f, input.Y);
        direction.Y = 0.0f;
        direction = direction.Normalized();

        float acceleration = IsOnFloor() ? GroundAcceleration : AirAcceleration;
        velocity.X = Mathf.MoveToward(velocity.X, direction.X * MoveSpeed, acceleration * delta);
        velocity.Z = Mathf.MoveToward(velocity.Z, direction.Z * MoveSpeed, acceleration * delta);

        Velocity = velocity;
        MoveAndSlide();
    }
}
