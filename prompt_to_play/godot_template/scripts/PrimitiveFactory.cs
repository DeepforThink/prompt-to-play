using System.Globalization;
using Godot;

namespace PromptToPlay;

public static class PrimitiveFactory
{
    public static Color ParseColor(string html, Color fallback)
    {
        string value = html.Trim().TrimStart('#');
        if (value.Length != 6 ||
            !byte.TryParse(value[..2], NumberStyles.HexNumber, null, out byte red) ||
            !byte.TryParse(value.Substring(2, 2), NumberStyles.HexNumber, null, out byte green) ||
            !byte.TryParse(value.Substring(4, 2), NumberStyles.HexNumber, null, out byte blue))
        {
            return fallback;
        }

        return new Color(red / 255.0f, green / 255.0f, blue / 255.0f);
    }

    public static StandardMaterial3D Material(
        Color albedo,
        float metallic = 0.0f,
        float roughness = 0.75f,
        Color? emission = null)
    {
        var material = new StandardMaterial3D
        {
            AlbedoColor = albedo,
            Metallic = metallic,
            Roughness = roughness,
        };

        if (emission is Color emissionColor)
        {
            material.EmissionEnabled = true;
            material.Emission = emissionColor;
            material.EmissionEnergyMultiplier = 2.5f;
        }

        return material;
    }

    public static StaticBody3D AddBoxBody(
        Node parent,
        string name,
        Vector3 position,
        Vector3 size,
        Material material)
    {
        var body = new StaticBody3D
        {
            Name = name,
            Position = position,
        };
        var mesh = new MeshInstance3D
        {
            Name = "Mesh",
            Mesh = new BoxMesh { Size = size },
            MaterialOverride = material,
        };
        var collision = new CollisionShape3D
        {
            Name = "Collision",
            Shape = new BoxShape3D { Size = size },
        };
        body.AddChild(mesh);
        body.AddChild(collision);
        parent.AddChild(body);
        return body;
    }

    public static StaticBody3D AddBoxCollision(
        Node parent,
        string name,
        Vector3 position,
        Vector3 size,
        bool addToWalkableGroup = false)
    {
        var body = new StaticBody3D
        {
            Name = name,
            Position = position,
        };
        body.AddChild(new CollisionShape3D
        {
            Name = "Collision",
            Shape = new BoxShape3D { Size = size },
        });
        if (addToWalkableGroup)
        {
            body.AddToGroup("ptp_walkable");
        }
        parent.AddChild(body);
        return body;
    }

    public static MeshInstance3D AddBoxVisual(
        Node parent,
        string name,
        Vector3 position,
        Vector3 size,
        Material material)
    {
        var mesh = new MeshInstance3D
        {
            Name = name,
            Position = position,
            Mesh = new BoxMesh { Size = size },
            MaterialOverride = material,
        };
        parent.AddChild(mesh);
        return mesh;
    }

    public static MeshInstance3D AddCylinderVisual(
        Node parent,
        string name,
        Vector3 position,
        float radius,
        float height,
        Material material)
    {
        var mesh = new MeshInstance3D
        {
            Name = name,
            Position = position,
            Mesh = new CylinderMesh
            {
                TopRadius = radius,
                BottomRadius = radius,
                Height = height,
                RadialSegments = 12,
            },
            MaterialOverride = material,
        };
        parent.AddChild(mesh);
        return mesh;
    }

    public static MeshInstance3D AddSphereVisual(
        Node parent,
        string name,
        Vector3 position,
        float radius,
        Material material)
    {
        var mesh = new MeshInstance3D
        {
            Name = name,
            Position = position,
            Mesh = new SphereMesh
            {
                Radius = radius,
                Height = radius * 2.0f,
                RadialSegments = 16,
                Rings = 8,
            },
            MaterialOverride = material,
        };
        parent.AddChild(mesh);
        return mesh;
    }
}
