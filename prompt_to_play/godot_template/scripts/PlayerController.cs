using Godot;

namespace PromptToPlay;

public partial class PlayerController : CharacterBody3D
{
    private const float MoveSpeed = 8.0f;
    private const float DriveForwardSpeed = 22.0f;
    private const float DriveReverseSpeed = 8.0f;
    private const float DriveAcceleration = 18.0f;
    private const float DriveBraking = 28.0f;
    private const float DriveCoastDeceleration = 7.0f;
    private const float DriveTurnRate = 1.65f;
    private const float GroundAcceleration = 28.0f;
    private const float AirAcceleration = 8.0f;
    private const float JumpVelocity = 7.5f;
    private const float MouseSensitivity = 0.0022f;

    private Node3D _head = null!;
    private float _gravity;
    private float _driveSpeed;
    private bool _drivingMode;

    public void Configure(bool drivingMode)
    {
        _drivingMode = drivingMode;
    }

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

        if (_drivingMode ||
            inputEvent is not InputEventMouseMotion mouseMotion ||
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
        if (_drivingMode)
        {
            Drive((float)deltaValue);
            return;
        }

        Walk((float)deltaValue);
    }

    private void Walk(float delta)
    {
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

    private void Drive(float delta)
    {
        Vector3 velocity = Velocity;
        if (!IsOnFloor())
        {
            velocity.Y -= _gravity * delta;
        }

        float throttle = Input.GetActionStrength("move_forward") -
            Input.GetActionStrength("move_back");
        float targetSpeed = throttle >= 0.0f
            ? throttle * DriveForwardSpeed
            : throttle * DriveReverseSpeed;
        float acceleration = Mathf.Abs(targetSpeed) < Mathf.Abs(_driveSpeed)
            ? DriveBraking
            : DriveAcceleration;
        if (Mathf.IsZeroApprox(throttle))
        {
            targetSpeed = 0.0f;
            acceleration = DriveCoastDeceleration;
        }
        _driveSpeed = Mathf.MoveToward(_driveSpeed, targetSpeed, acceleration * delta);

        float steering = Input.GetActionStrength("move_left") -
            Input.GetActionStrength("move_right");
        float speedRatio = Mathf.Clamp(Mathf.Abs(_driveSpeed) / DriveForwardSpeed, 0.18f, 1.0f);
        float directionSign = Mathf.IsZeroApprox(_driveSpeed) ? 1.0f : Mathf.Sign(_driveSpeed);
        RotateY(steering * DriveTurnRate * speedRatio * directionSign * delta);

        Vector3 forward = -GlobalTransform.Basis.Z;
        Vector3 planar = forward * _driveSpeed;
        velocity.X = planar.X;
        velocity.Z = planar.Z;
        Velocity = velocity;
        FloorSnapLength = 0.55f;
        FloorMaxAngle = Mathf.DegToRad(52.0f);
        MoveAndSlide();

        if (IsOnWall())
        {
            _driveSpeed *= 0.55f;
        }
    }
}
