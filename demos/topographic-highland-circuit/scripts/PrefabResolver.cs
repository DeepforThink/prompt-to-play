using Godot;

namespace PromptToPlay;

public sealed class PrefabResolver
{
    private const string AssetRoot = "res://assets/";
    private const int MaximumGeneratedCollisionMeshes = 64;

    private readonly AssetCatalog _catalog;

    public PrefabResolver(AssetCatalog catalog)
    {
        _catalog = catalog;
    }

    public static PrefabResolver LoadDefault()
    {
        return new PrefabResolver(AssetCatalog.LoadDefault());
    }

    public bool TryInstantiate(
        string prefab,
        Node3D entityRoot,
        Vector3 requestedScale,
        bool addCollision,
        out string failure)
    {
        failure = string.Empty;
        if (!_catalog.TryGetExact(prefab, out AssetCatalogEntry entry))
        {
            failure = string.IsNullOrEmpty(_catalog.LoadIssue)
                ? $"Prefab '{prefab}' is not present in the asset catalog."
                : _catalog.LoadIssue;
            return false;
        }
        if (!IsSafeGeneratedScenePath(entry.ScenePath))
        {
            failure =
                $"Prefab '{prefab}' has an unsafe or unsupported scene_path: {entry.ScenePath}";
            return false;
        }
        if (!IsFinitePositive(requestedScale))
        {
            failure = $"Prefab '{prefab}' received a non-positive or non-finite scale.";
            return false;
        }

        Node3D? instanceRoot = null;
        try
        {
            PackedScene? packedScene = GD.Load<PackedScene>(entry.ScenePath);
            if (packedScene is null)
            {
                failure = $"Prefab '{prefab}' could not load PackedScene {entry.ScenePath}.";
                return false;
            }

            Node instance = packedScene.Instantiate();
            if (instance is not Node3D sceneRoot)
            {
                instance.Free();
                failure = $"Prefab '{prefab}' scene root is not Node3D.";
                return false;
            }

            instanceRoot = sceneRoot;
            sceneRoot.Name = "ResolvedAsset";
            sceneRoot.Scale = Multiply(sceneRoot.Scale, requestedScale);
            entityRoot.AddChild(sceneRoot);
            EnableShadows(sceneRoot);

            int generatedCollisions = addCollision && !HasAuthoredCollision(sceneRoot)
                ? AddStaticMeshCollisions(sceneRoot)
                : 0;
            sceneRoot.SetMeta("asset_prefab", prefab);
            sceneRoot.SetMeta("asset_scene_path", entry.ScenePath);
            sceneRoot.SetMeta("generated_collision_count", generatedCollisions);
            entityRoot.SetMeta("asset_resolution", "catalog");
            entityRoot.SetMeta("asset_prefab", prefab);
            entityRoot.SetMeta("asset_scene_path", entry.ScenePath);
            return true;
        }
        catch (Exception exception)
        {
            if (instanceRoot is not null && GodotObject.IsInstanceValid(instanceRoot))
            {
                instanceRoot.QueueFree();
            }
            failure = $"Prefab '{prefab}' failed to instantiate: {exception.Message}";
            GD.PushWarning(failure);
            return false;
        }
    }

    private static bool IsSafeGeneratedScenePath(string path)
    {
        if (!path.StartsWith(AssetRoot, StringComparison.Ordinal) || path.Contains('\\'))
        {
            return false;
        }

        string relative = path[AssetRoot.Length..];
        string[] parts = relative.Split('/');
        if (parts.Any(part => part is "" or "." or ".."))
        {
            return false;
        }

        return path.EndsWith(".glb", StringComparison.OrdinalIgnoreCase) ||
            path.EndsWith(".gltf", StringComparison.OrdinalIgnoreCase);
    }

    private static bool IsFinitePositive(Vector3 value)
    {
        return float.IsFinite(value.X) && value.X > 0.0f &&
            float.IsFinite(value.Y) && value.Y > 0.0f &&
            float.IsFinite(value.Z) && value.Z > 0.0f;
    }

    private static Vector3 Multiply(Vector3 left, Vector3 right)
    {
        return new Vector3(left.X * right.X, left.Y * right.Y, left.Z * right.Z);
    }

    private static void EnableShadows(Node root)
    {
        foreach (Node node in DescendantsAndSelf(root))
        {
            if (node is GeometryInstance3D geometry)
            {
                geometry.CastShadow = GeometryInstance3D.ShadowCastingSetting.On;
            }
        }
    }

    private static bool HasAuthoredCollision(Node root)
    {
        return DescendantsAndSelf(root).Any(node => node is CollisionObject3D);
    }

    private static int AddStaticMeshCollisions(Node root)
    {
        MeshInstance3D[] meshes = DescendantsAndSelf(root)
            .OfType<MeshInstance3D>()
            .Where(mesh => mesh.Mesh is not null)
            .Take(MaximumGeneratedCollisionMeshes)
            .ToArray();
        int generated = 0;
        foreach (MeshInstance3D mesh in meshes)
        {
            try
            {
                Shape3D? shape = mesh.Mesh?.CreateTrimeshShape();
                if (shape is null)
                {
                    continue;
                }

                var body = new StaticBody3D
                {
                    Name = $"GeneratedCollision{generated + 1}",
                    CollisionLayer = 1,
                    CollisionMask = 1,
                };
                body.AddChild(new CollisionShape3D
                {
                    Name = "Shape",
                    Shape = shape,
                });
                mesh.AddChild(body);
                body.AddToGroup("ptp_obstacle");
                generated++;
            }
            catch (Exception exception)
            {
                GD.PushWarning(
                    $"Could not generate collision for mesh {mesh.GetPath()}: {exception.Message}");
            }
        }
        return generated;
    }

    private static IEnumerable<Node> DescendantsAndSelf(Node root)
    {
        yield return root;
        foreach (Node child in root.GetChildren())
        {
            foreach (Node descendant in DescendantsAndSelf(child))
            {
                yield return descendant;
            }
        }
    }
}
