using Godot;

namespace PromptToPlay;

public partial class Interactable : Area3D
{
    public event Action<string>? Triggered;
    public event Action<string, string, bool>? ProximityChanged;

    public string StableId { get; private set; } = string.Empty;
    public string ActionKind { get; private set; } = string.Empty;
    public string InteractionLabel { get; private set; } = string.Empty;
    public int DurationMilliseconds { get; private set; }

    private StandardMaterial3D _material = null!;
    private bool _playerInside;
    private bool _consumed;
    private bool _busy;
    private float _baseY;
    private float _phase;

    public void Configure(InteractableSpec spec, uint seed, StandardMaterial3D material)
    {
        StableId = spec.Id;
        ActionKind = spec.Action;
        InteractionLabel = spec.Label;
        DurationMilliseconds = spec.DurationMilliseconds;
        _phase = (seed % 6283u) / 1000.0f;
        _material = material;
    }

    public override void _Ready()
    {
        _baseY = Position.Y;
        BodyEntered += OnBodyEntered;
        BodyExited += OnBodyExited;
    }

    public override void _Process(double deltaValue)
    {
        float delta = (float)deltaValue;
        RotateY(delta * 1.2f);

        Vector3 position = Position;
        position.Y = _baseY + Mathf.Sin((float)Time.GetTicksMsec() * 0.002f + _phase) * 0.12f;
        Position = position;

        if (_playerInside && !_busy && !_consumed && Input.IsActionJustPressed("interact"))
        {
            BeginInteraction();
        }
    }

    private async void BeginInteraction()
    {
        _busy = true;
        if (DurationMilliseconds > 0)
        {
            await ToSignal(
                GetTree().CreateTimer(DurationMilliseconds / 1000.0),
                SceneTreeTimer.SignalName.Timeout);
        }

        if (!IsInsideTree())
        {
            return;
        }

        Triggered?.Invoke(StableId);
        _material.AlbedoColor = new Color(0.35f, 1.0f, 0.58f);
        _material.Emission = new Color(0.25f, 1.0f, 0.48f);

        _consumed = ActionKind is "collect" or "repair";
        _busy = false;
        if (_consumed)
        {
            ProximityChanged?.Invoke(StableId, BuildPrompt(), false);
        }
        if (ActionKind == "collect")
        {
            QueueFree();
        }
    }

    private void OnBodyEntered(Node3D body)
    {
        if (body is PlayerController)
        {
            _playerInside = true;
            ProximityChanged?.Invoke(StableId, BuildPrompt(), true);
        }
    }

    private void OnBodyExited(Node3D body)
    {
        if (body is PlayerController)
        {
            _playerInside = false;
            ProximityChanged?.Invoke(StableId, BuildPrompt(), false);
        }
    }

    private string BuildPrompt()
    {
        string verb = ActionKind switch
        {
            "collect" => "收集",
            "activate" => "启动",
            "repair" => "修复",
            "inspect" => "查看",
            _ => "交互",
        };
        return $"按 E {verb}：{InteractionLabel}";
    }
}
