using Godot;

namespace PromptToPlay;

public partial class ExitGate : Area3D
{
    public event Action? Completed;

    public bool IsUnlocked { get; private set; }
    public IReadOnlyList<string> RequiredObjectives => _requiredObjectives;

    private CollisionShape3D _barrierCollision = null!;
    private StandardMaterial3D _indicatorMaterial = null!;
    private readonly List<string> _requiredObjectives = new();
    private bool _completed;

    public void Configure(
        IEnumerable<string> requiredObjectives,
        CollisionShape3D barrierCollision,
        StandardMaterial3D indicatorMaterial)
    {
        _requiredObjectives.Clear();
        _requiredObjectives.AddRange(requiredObjectives);
        _barrierCollision = barrierCollision;
        _indicatorMaterial = indicatorMaterial;
    }

    public void UpdateObjectiveState(IReadOnlySet<string> completedObjectives)
    {
        if (_requiredObjectives.All(completedObjectives.Contains))
        {
            Unlock();
        }
    }

    public override void _Ready()
    {
        BodyEntered += OnBodyEntered;
    }

    public void Unlock()
    {
        if (IsUnlocked)
        {
            return;
        }

        IsUnlocked = true;
        _indicatorMaterial.AlbedoColor = new Color(0.33f, 1.0f, 0.60f);
        _indicatorMaterial.Emission = new Color(0.27f, 1.0f, 0.53f);
        _barrierCollision.SetDeferred(CollisionShape3D.PropertyName.Disabled, true);

        foreach (Node3D body in GetOverlappingBodies())
        {
            if (body is PlayerController)
            {
                Complete();
                break;
            }
        }
    }

    private void OnBodyEntered(Node3D body)
    {
        if (IsUnlocked && body is PlayerController)
        {
            Complete();
        }
    }

    private void Complete()
    {
        if (_completed)
        {
            return;
        }

        _completed = true;
        Completed?.Invoke();
    }
}
