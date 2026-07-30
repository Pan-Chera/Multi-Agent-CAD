# build123d Algebra API Reference (v0.11.1)

This file is automatically provided to the code repair agent.
Use it as the authoritative reference for build123d API behavior.

## CRITICAL: Primitive Alignment

All primitives (`Cylinder`, `Box`, `Sphere`, `Cone`) default to
`align=(CENTER, CENTER, CENTER)` — the object is **centered at the origin**.

```python
Cylinder(radius=5, height=10)   # Z = -5 to +5  (centered!)
Box(10, 20, 30)                 # Z = -15 to +15 (centered!)
```

Use `align` to control which point of the bounding box sits at the origin:

```python
from build123d import Align

# Bottom-aligned: object starts at Z=0
Cylinder(radius=5, height=10, align=(Align.CENTER, Align.CENTER, Align.MIN))
# Z = 0 to 10  (bottom at origin)

Box(10, 20, 30, align=(Align.CENTER, Align.CENTER, Align.MIN))
# Z = 0 to 30  (bottom at origin)
```

**For Z-axis through-hole cutting tools, use `Align.MIN` on Z.**
This makes `Pos(x, y, z)` place the BOTTOM of the cylinder at Z=z:

```python
# Through-hole in Z through a 10mm base plate (Z=0..10)
tool = Pos(x, y, -1) * Cylinder(radius=3.5, height=14,
    align=(Align.CENTER, Align.CENTER, Align.MIN))
# Bottom Z=-1, top Z=13 → penetrates Z=0..10 with 1mm overshoot each side

# For Y/X-axis holes (rotated cylinders), keep DEFAULT alignment (CENTER):
tool = Pos(0, 0, 34) * Rot(X=90) * Cylinder(radius=7, height=60)
# Center at Z=34, Y range: -30..30 → covers lugs at Y=-26..26
```

## Pos and Rot Transforms

`Pos(x, y, z)` translates. `Rot(X=deg, Y=deg, Z=deg)` rotates.
**Order matters** — transforms apply right-to-left:

```python
Pos(0, 0, 34) * Rot(X=90) * Cylinder(radius=7, height=60, align=...)
# 1. Create cylinder (centered at origin, along Z)
# 2. Rot(X=90): rotate Z-axis to Y-axis
# 3. Pos(0,0,34): translate center to Z=34
```

## Extrude

```python
# Algebra API (no context manager)
sketch = Rectangle(50, 40)
solid = extrude(sketch, amount=10)         # extrude along sketch normal
solid = extrude(sketch, amount=10, dir=(0, 0, -1))  # negative direction
solid = extrude(sketch, amount=5, both=True)         # symmetric ±5mm
```

NOTE: parameter is `dir`, NOT `direction`.

## Sweep

`sweep(profile, path)` sweeps a 2D profile along a 3D path.

**CRITICAL: The profile MUST be perpendicular to the path tangent at the start point.**
If the profile is parallel to the path, the result is a flat ribbon (near-zero cross-section).

```python
# Helix handrail: path goes in Y direction at start, so profile must be in XZ plane
helix_path = Helix(pitch=116, height=116, radius=66)

# ❌ WRONG: Circle in XY plane (parallel to helix tangent) → flat ribbon
sk = Pos(66, 0, 0) * Circle(2.5)
rail = sweep(sk, helix_path)  # Volume ~2278 mm³ (ribbon)

# ✅ CORRECT: Circle in XZ plane (perpendicular to helix tangent) → tube
sk = Plane.XZ * Pos(66, 0, 0) * Circle(2.5)
rail = sweep(sk, helix_path)  # Volume ~8142 mm³ (proper tube)
```

**General rule**: Use `Plane.XZ` or `Plane.YZ` for profiles swept along paths that start in the XY plane. Use `Plane.XY` only when the path starts going vertically (along Z).

## Boolean Operations

```python
result = body_a + body_b                   # union
result = body_a - tool                     # cut (subtract)
result = body_a & body_b                   # intersect
```

**Use `_safe_cut(body, tool, label)` instead of `body - tool`.**
It detects missed cuts (no volume change) and logs them.

**Cut tools MUST overshoot the target by ≥1mm on each side.**
Coincident / coplanar faces cause OpenCascade kernel failures.

## Mirror for Symmetric Parts

When a part is symmetric (e.g., left/right lugs, left/right ribs), generate
ONLY the right half and use `mirror()` to create the left half. This is
far more reliable than re-generating mirrored geometry from scratch.

```python
# Generate right lug
solid_right = Pos(0, 26, 0) * Rot(X=90) * extrude(sk_lug, amount=18.0)

# Left lug = mirror of right lug across XZ plane (Y → -Y)
solid_left = mirror(solid_right, Plane.XZ)

# Union
body = body + solid_right + solid_left
```

**Mirror planes**:
- `Plane.XZ` — mirrors Y → -Y (left/right symmetry)
- `Plane.YZ` — mirrors X → -X (front/back symmetry)
- `Plane.XY` — mirrors Z → -Z (top/bottom symmetry)

**Do NOT re-generate mirrored geometry from scratch** — coordinate sign
errors are the most common source of misplaced features.

## Rib Perpendicularity (CRITICAL)

A rib's **sketch plane** MUST be **orthogonal** to the connection face plane
(they cannot be the same plane). The rib's **extrude direction** = the
connection face's normal axis.

Quick rule: connection face on XZ → rib sketch on YZ; on YZ → rib on XZ;
on XY → rib on XZ or YZ.

**Self-check**: if the rib's sketch plane == the connection face plane, switch
to the orthogonal plane. Do NOT fix a wrong-plane rib with Pos/Rot — switch
the plane and remap the local coordinates.

## Holes (Cylinder subtraction pattern)

```python
def gen_step():
    # ... build the part ...
    body = extrude(Rectangle(120, 60), amount=10)  # base plate Z=0..10

    # Z-axis through-hole: use Align.MIN so Pos_z = bottom position
    tool = Pos(45, 20, -1) * Cylinder(radius=3.5, height=14,
        align=(Align.CENTER, Align.CENTER, Align.MIN))
    body = _safe_cut(body, tool, 'hole-1')

    # Y-axis through-hole: rotate first, keep DEFAULT alignment (CENTER)
    tool = Pos(0, 0, 34) * Rot(X=90) * Cylinder(radius=7, height=60)
    body = _safe_cut(body, tool, 'hole-2')

    return {"shape": body}
```

## Fillet and Chamfer

Fillets/chamfers MUST come AFTER all boolean operations.
Every boolean invalidates all edge selectors.

**Edge selection is the most error-prone part of fillet/chamfer.** Use these
proven patterns:

### Base perimeter fillet (horizontal edges at top/bottom)

```python
# filter_by(Plane.XY) gets ALL horizontal edges (top + bottom perimeter)
xy_edges = solid.edges().filter_by(Plane.XY)
solid = fillet(xy_edges, radius=3.0)
```

### Vertical edges split by position

```python
# All vertical (Z-aligned) edges
z_edges = solid.edges().filter_by(Axis.Z)

# Split by X position: cutout edges vs. lug/base edges
cutout_edges = [e for e in z_edges if abs(e.center().X) > 20]   # outer edges
lug_edges = [e for e in z_edges if abs(e.center().X) <= 20]    # inner edges

solid = fillet(cutout_edges, radius=3.0)   # cutout vertical edges
solid = fillet(lug_edges, radius=2.0)      # lug-to-base transition edges
```

### Key rules

- Use `e.center().Z` (capital Z), NOT `e.center().z` — build123d uses capital axis names
- Use `e.length` to filter by edge length (avoid tiny edges from holes/cuts)
- Wrap each fillet in `try/except` — if one fillet fails, others can still apply
- If fillet fails with "try a smaller value", reduce radius by 0.5mm and retry

## Export

```python
export_step(solid, "output.step", unit=Unit.MM)
export_stl(solid, "output.stl", tolerance=0.01, angular_tolerance=0.1)
```

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| `Pos(x,y,-1) * Cylinder(h=12)` → blind hole (center at -1, range -7..5) | Add `align=(CENTER,CENTER,MIN)` → range -1..11 |
| `extrude(sk, direction=...)` → param is `dir`, not `direction` | Use `dir=(0,0,1)` |
| `with BuildPart():` → context manager forbidden in algebra API | Use top-level `extrude()`, `Box()`, etc. |
| `body - tool` → bypasses missed-cut detection | Use `_safe_cut(body, tool, label)` |
| Fillet before boolean → edge selectors invalidated | Move fillet to LAST step |
| `make_face(Line(...), Arc(...))` → wrong arg count | Use `make_face(wire.wire())` with BuildLine |
| `sweep(Circle(r), helix)` → flat ribbon (profile parallel to path) | Use `Plane.XZ * Circle(r)` (profile ⊥ path tangent) |
| Re-generating mirrored geometry from scratch → coordinate sign errors | Use `mirror(solid, Plane.XZ)` for left/right symmetry |
| `solid.edges().filter_by(Axis.Z)` for base perimeter fillet → wrong edges | Use `filter_by(Plane.XY)` for horizontal perimeter edges |
| `e.center().z` → AttributeError | Use `e.center().Z` (capital Z) |
| Fillet "try a smaller value" error | Reduce radius by 0.5mm and retry in try/except |
