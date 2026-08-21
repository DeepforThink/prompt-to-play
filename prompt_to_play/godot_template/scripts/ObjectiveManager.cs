using Godot;

namespace PromptToPlay;

public partial class ObjectiveManager : Node
{
    public event Action<string, string>? ObjectiveCompleted;
    public event Action? StateChanged;

    public IReadOnlySet<string> CompletedObjectives => _completedObjectives;
    public IReadOnlyCollection<string> CompletedInteractables => _completedInteractables;

    private readonly Dictionary<string, ObjectiveSpec> _objectives =
        new(StringComparer.Ordinal);
    private readonly HashSet<string> _completedObjectives =
        new(StringComparer.Ordinal);
    private readonly HashSet<string> _completedInteractables =
        new(StringComparer.Ordinal);
    private readonly List<string> _history = new();

    public void Configure(IEnumerable<ObjectiveSpec> objectives)
    {
        _objectives.Clear();
        _completedObjectives.Clear();
        _completedInteractables.Clear();
        _history.Clear();

        foreach (ObjectiveSpec objective in objectives)
        {
            _objectives.Add(objective.Id, objective);
        }
    }

    public void RecordInteraction(string stableId)
    {
        _history.Add(stableId);
        _completedInteractables.Add(stableId);

        foreach (ObjectiveSpec objective in _objectives.Values.OrderBy(
                     objective => objective.Id,
                     StringComparer.Ordinal))
        {
            if (_completedObjectives.Contains(objective.Id) || !IsSatisfied(objective))
            {
                continue;
            }

            _completedObjectives.Add(objective.Id);
            ObjectiveCompleted?.Invoke(objective.Id, objective.CompletionText);
        }

        StateChanged?.Invoke();
    }

    public bool AreCompleted(IEnumerable<string> objectiveIds)
    {
        return objectiveIds.All(_completedObjectives.Contains);
    }

    public string BuildStatusText()
    {
        IEnumerable<string> lines = _objectives.Values
            .OrderBy(objective => objective.Id, StringComparer.Ordinal)
            .Select(objective =>
                $"{(_completedObjectives.Contains(objective.Id) ? "[完成]" : "[待办]")} " +
                $"{objective.CompletionText}");
        return string.Join("\n", lines);
    }

    private bool IsSatisfied(ObjectiveSpec objective)
    {
        return objective.Rule switch
        {
            "all" => objective.Targets.All(_completedInteractables.Contains),
            "any" => objective.Targets.Any(_completedInteractables.Contains),
            "sequence" => ContainsOrderedSubsequence(_history, objective.Targets),
            _ => false,
        };
    }

    private static bool ContainsOrderedSubsequence(
        IReadOnlyList<string> history,
        IReadOnlyList<string> targets)
    {
        int targetIndex = 0;
        foreach (string interaction in history)
        {
            if (targetIndex < targets.Count && interaction == targets[targetIndex])
            {
                targetIndex++;
            }
        }

        return targetIndex == targets.Count;
    }
}
