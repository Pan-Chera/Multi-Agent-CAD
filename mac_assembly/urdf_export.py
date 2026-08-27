"""URDF export for a finished mac_assembly job.

Emits a ``.urdf`` + ``meshes/<label>.stl`` directory from a successfully
placed assembly, suitable for grasp simulators (PyBullet / MuJoCo / MoveIt)
and structural validators (``yourdf``, ``check_urdf``).

Run after ``build_handoff`` from ``graph_assembly.main``. Best-effort:
returns ``None`` on failure (mirrors ``export_assembly_glb``).

Frame conventions (per ``skills/urdf/references/frame-semantics.md``):

URDF child link frame is coincident with the joint frame. The mate data
in ``assembly_mates.json`` carries resolved endpoint positions/orientations
in WORLD coords (computed in assembly_codegen as
``_parts[label].location × Location(resolved_point)`` -- the part's world
transform applied to the part-local resolved anchor). So:

- ``joint_world = Location(fixed_endpoint.position, fixed_endpoint.orientation)``
  -- the joint frame's pose in WORLD coords.
- ``<joint origin>`` (joint frame in PARENT link frame) =
  ``parent_world⁻¹ × joint_world`` where ``parent_world`` comes from
  ``assembly_manifest.json``.
- ``<visual>/<collision>/<inertial> origin`` of CHILD link =
  ``joint_world⁻¹ × child_world`` (mesh-local frame's pose in joint/child
  frame). For ROOT link (no parent mate), visual origin = ``root_world``
  (mesh-local frame's pose in world = root link frame, since URDF root
  link frame = world).
- ``<axis xyz>`` is in the joint frame; for revolute it's ``(0, 0, 1)``
  by URDF convention (the joint frame's Z = rotation axis, achieved by
  the ``<joint origin rpy>`` orienting the frame to align Z with the
  physical bore axis).
- ``<inertia>`` is around the CoM, in link-frame axes
  (``I_link = R(visual_origin) × I_mesh × R(visual_origin).T``).

Units: URDF uses metres / kg / radians. CAD is in millimetres. We scale
mesh geometry at the URDF ``<mesh scale="0.001 ...">`` level so the on-disk
STLs stay in millimetres; positions/translations are divided by 1000
explicitly.
"""

from __future__ import annotations

import json
import math
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

MM_TO_M = 0.001
DEG_TO_RAD = math.pi / 180.0
DEFAULT_DENSITY_KG_M3 = 1000.0


def export_urdf(work_dir: Path | str, brief=None) -> Path | None:
    """Emit ``<work_dir>/urdf/<assembly_name>.urdf`` + ``meshes/`` dir.

    Returns the URDF path on success, ``None`` on failure (best-effort,
    never raises -- mirrors ``export_assembly_glb``).
    """
    work_dir = Path(work_dir)
    mates_path = work_dir / "assembly_mates.json"
    manifest_path = work_dir / "assembly_manifest.json"
    if not mates_path.is_file() or not manifest_path.is_file():
        return None
    try:
        mates = _load_mates(work_dir)
        if not mates:
            return None
        world_locs = _load_world_locations(work_dir)
        root, tree_ok = _build_link_tree(mates)
        if not tree_ok:
            print("  [urdf] link tree not a single-root tree; skipping URDF")
            return None

        assembly_name = (
            getattr(brief, "assembly_name", None) or work_dir.name
        )
        urdf_dir = work_dir / "urdf"
        meshes_dir = urdf_dir / "meshes"
        meshes_dir.mkdir(parents=True, exist_ok=True)

        robot = ET.Element("robot", {"name": assembly_name})

        # Per-link visual offset (mesh-local frame's pose in link frame).
        # Root: link frame = world, so visual_offset = root_world.
        # Non-root: link frame = joint frame, so visual_offset =
        # joint_world.inverse * child_world (where joint_world is from the
        # mate where this link is the moving side).
        visual_offsets: dict[str, tuple[list, list]] = {}
        # Root's world transform.
        if root in world_locs:
            visual_offsets[root] = world_locs[root]
        # For each non-root link, find the mate where it's the moving side.
        for m in mates:
            child = m["moving_endpoint"]["part"]
            joint_world = _endpoint_to_loc(m["fixed_endpoint"])
            child_world = world_locs.get(child)
            if child_world is None:
                continue
            visual_offsets[child] = _compose_loc_inverse(
                joint_world, child_world
            )

        # Collect all labels (root + every child referenced by a mate).
        labels = {root}
        for m in mates:
            labels.add(m["moving_endpoint"]["part"])
        labels = sorted(labels)

        for label in labels:
            mesh_src = _find_part_stl(work_dir, label)
            if mesh_src is None:
                print(f"  [urdf] no STL for part {label!r}; skipping link")
                continue
            mesh_dst = meshes_dir / f"{label}.stl"
            shutil.copyfile(mesh_src, mesh_dst)
            voff = visual_offsets.get(label, ([0, 0, 0], [0, 0, 0]))
            _emit_link(robot, label, mesh_dst, voff, urdf_dir)

        for m in mates:
            parent = m["fixed_endpoint"]["part"]
            parent_world = world_locs.get(parent, ([0, 0, 0], [0, 0, 0]))
            _emit_joint(robot_elem=robot, mate=m, parent_world=parent_world)

        urdf_path = urdf_dir / f"{assembly_name}.urdf"
        ET.ElementTree(robot).write(
            urdf_path, encoding="utf-8", xml_declaration=True
        )

        ok, msg = _validate(urdf_path)
        if not ok:
            print(f"  [urdf] validation warning: {msg}")
        return urdf_path
    except Exception as exc:  # noqa: BLE001 - best-effort
        print(f"  [urdf] export failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def _load_mates(work_dir: Path) -> list[dict]:
    """Parse ``assembly_mates.json`` -> list of mate dicts.

    Each dict has ``label``, ``relation``, ``fixed_endpoint``,
    ``moving_endpoint``, ``parameters``. Endpoints have ``part``,
    ``position`` (3-list, WORLD mm), ``orientation`` (3-list, Euler XYZ
    deg, WORLD), ``axes`` (dict with x/y/z basis vectors).
    """
    p = work_dir / "assembly_mates.json"
    with p.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    out = []
    for r in raw:
        out.append({
            "label": r.get("label", ""),
            "relation": r.get("relation") or r.get("type", ""),
            "fixed_endpoint": (
                r.get("fixed_endpoint") or r.get("fixedEndpoint") or {}
            ),
            "moving_endpoint": (
                r.get("moving_endpoint") or r.get("movingEndpoint") or {}
            ),
            "parameters": r.get("parameters") or {},
        })
    return out


def _load_world_locations(work_dir: Path) -> dict[str, tuple[list, list]]:
    """Parse ``assembly_manifest.json`` -> {label: (translation_mm, euler_xyz_deg)}.

    Each value is the part's WORLD transform after placement.
    """
    p = work_dir / "assembly_manifest.json"
    with p.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    out: dict[str, tuple[list, list]] = {}
    for entry in raw:
        label = entry.get("label")
        if not label:
            continue
        loc = entry.get("location") or {}
        t = loc.get("translation") or [0, 0, 0]
        r = loc.get("rotation_euler_xyz_deg") or [0, 0, 0]
        out[label] = (list(t), list(r))
    return out


def _build_link_tree(mates: list[dict]) -> tuple[str | None, bool]:
    """Find root (part that is fixed but never moving) + verify tree."""
    parents = {m["fixed_endpoint"]["part"] for m in mates
               if m.get("fixed_endpoint", {}).get("part")}
    children = {m["moving_endpoint"]["part"] for m in mates
                if m.get("moving_endpoint", {}).get("part")}
    roots = parents - children
    if len(roots) != 1:
        return None, False
    return next(iter(roots)), True


def _find_part_stl(work_dir: Path, part_label: str) -> Path | None:
    """Find the link-local STL for a part.

    Looks in ``parts/<part_label>/temp_output_*.stl`` (mirrors
    ``part_generator._newest``). Returns the newest matching file.
    """
    part_dir = work_dir / "parts" / part_label
    if not part_dir.is_dir():
        return None
    cands = sorted(part_dir.glob("temp_output_*.stl"),
                   key=lambda p: p.stat().st_mtime)
    return cands[-1] if cands else None


# ---------------------------------------------------------------------------
# Geometry helpers (numpy-based, no build123d dependency for the math)
# ---------------------------------------------------------------------------


def _euler_xyz_deg_to_matrix(orient_deg) -> Any | None:
    """Euler XYZ (degrees) -> 3x3 rotation matrix (numpy array).

    Convention: extrinsic XYZ = Rz @ Ry @ Rx (URDF rpy convention, matches
    build123d's Location.orientation). Returns ``None`` if numpy
    unavailable; returns identity if orientation is all-zero.
    """
    try:
        import numpy as np
    except ImportError:
        return None
    rx, ry, rz = (float(orient_deg[i]) for i in range(3))
    if abs(rx) < 1e-9 and abs(ry) < 1e-9 and abs(rz) < 1e-9:
        return np.eye(3)
    cx, sx = math.cos(rx * DEG_TO_RAD), math.sin(rx * DEG_TO_RAD)
    cy, sy = math.cos(ry * DEG_TO_RAD), math.sin(ry * DEG_TO_RAD)
    cz, sz = math.cos(rz * DEG_TO_RAD), math.sin(rz * DEG_TO_RAD)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


def _matrix_to_euler_xyz_rad(R) -> list[float]:
    """3x3 rotation matrix -> Euler XYZ radians (extrinsic = URDF rpy)."""
    try:
        from scipy.spatial.transform import Rotation
        return list(Rotation.from_matrix(R).as_euler("xyz"))
    except ImportError:
        pass
    import math as _m
    ry = _m.asin(max(-1.0, min(1.0, -float(R[2, 0]))))
    if abs(abs(ry) - _m.pi / 2) < 1e-6:
        rx = 0.0
        rz = _m.atan2(-float(R[0, 1]), float(R[1, 1]))
    else:
        rx = _m.atan2(float(R[2, 1]), float(R[2, 2]))
        rz = _m.atan2(float(R[1, 0]), float(R[0, 0]))
    return [rx, ry, rz]


def _endpoint_to_loc(endpoint: dict) -> tuple[list, list]:
    """Extract (position_mm, orientation_deg) from a mate endpoint."""
    pos = list(endpoint.get("position", [0, 0, 0]))
    ori = list(endpoint.get("orientation", [0, 0, 0]))
    return pos, ori


def _compose_loc(loc_a_mm_deg: tuple[list, list],
                 loc_b_mm_deg: tuple[list, list]) -> tuple[list, list]:
    """Compute Location(A) × Location(B) -> (pos_mm, ori_deg).

    Used for ``visual_offset = joint_world⁻¹ × child_world`` etc.
    """
    import numpy as np
    pos_a, ori_a = loc_a_mm_deg
    pos_b, ori_b = loc_b_mm_deg
    Ra = _euler_xyz_deg_to_matrix(ori_a)
    if Ra is None:
        Ra = np.eye(3)
    Rb = _euler_xyz_deg_to_matrix(ori_b)
    if Rb is None:
        Rb = np.eye(3)
    ta = np.array([float(pos_a[0]), float(pos_a[1]), float(pos_a[2])])
    tb = np.array([float(pos_b[0]), float(pos_b[1]), float(pos_b[2])])
    # Compose: A * B -> rotation Ra @ Rb, translation Ra @ tb + ta.
    R_ab = Ra @ Rb
    t_ab = Ra @ tb + ta
    if np.allclose(R_ab, np.eye(3)):
        return t_ab.tolist(), [0.0, 0.0, 0.0]
    rpy_rad = _matrix_to_euler_xyz_rad(R_ab)
    return t_ab.tolist(), [r / DEG_TO_RAD for r in rpy_rad]


def _compose_loc_inverse(loc_a_mm_deg: tuple[list, list],
                         loc_b_mm_deg: tuple[list, list]) -> tuple[list, list]:
    """Compute Location(A)⁻¹ × Location(B) -> (pos_mm, ori_deg).

    Used for ``visual_offset = joint_world⁻¹ × child_world``.
    """
    import numpy as np
    pos_a, ori_a = loc_a_mm_deg
    pos_b, ori_b = loc_b_mm_deg
    Ra = _euler_xyz_deg_to_matrix(ori_a)
    if Ra is None:
        Ra = np.eye(3)
    Rb = _euler_xyz_deg_to_matrix(ori_b)
    if Rb is None:
        Rb = np.eye(3)
    ta = np.array([float(pos_a[0]), float(pos_a[1]), float(pos_a[2])])
    tb = np.array([float(pos_b[0]), float(pos_b[1]), float(pos_b[2])])
    # A.inverse: rotation Ra.T, translation -Ra.T @ ta.
    # (A.inv * B): rotation Ra.T @ Rb, translation Ra.T @ (tb - ta).
    R_ab = Ra.T @ Rb
    t_ab = Ra.T @ (tb - ta)
    if np.allclose(R_ab, np.eye(3)):
        return t_ab.tolist(), [0.0, 0.0, 0.0]
    rpy_rad = _matrix_to_euler_xyz_rad(R_ab)
    return t_ab.tolist(), [r / DEG_TO_RAD for r in rpy_rad]


def _loc_to_origin_strs(position_mm, orientation_deg) -> tuple[str, str]:
    """Convert (position_mm, orientation_deg) -> (xyz_str, rpy_str) for
    URDF origin attributes, with mm→m and deg→rad."""
    px, py, pz = (float(position_mm[i]) * MM_TO_M for i in range(3))
    rx, ry, rz = (float(orientation_deg[i]) * DEG_TO_RAD for i in range(3))
    return (
        f"{px:.6f} {py:.6f} {pz:.6f}",
        f"{rx:.6f} {ry:.6f} {rz:.6f}",
    )


# ---------------------------------------------------------------------------
# Per-link emission (visual + collision + inertial)
# ---------------------------------------------------------------------------


def _emit_link(robot_elem, label: str, mesh_path: Path,
               visual_offset_mm_deg: tuple[list, list],
               urdf_dir: Path) -> None:
    """Emit a <link> with visual + collision + inertial.

    ``visual_offset_mm_deg`` = (position_mm, orientation_deg) of the
    mesh-local frame in the LINK frame (= world for root, = joint frame
    for non-root). Both visual/collision origin AND inertial CoM/tensor
    rotation derive from this single offset.
    """
    link = ET.SubElement(robot_elem, "link", {"name": label})

    pos_mm, ori_deg = visual_offset_mm_deg
    xyz_str, rpy_str = _loc_to_origin_strs(pos_mm, ori_deg)

    mesh_rel = mesh_path.relative_to(urdf_dir).as_posix()

    visual = ET.SubElement(link, "visual")
    ET.SubElement(visual, "origin", {"xyz": xyz_str, "rpy": rpy_str})
    geom_v = ET.SubElement(visual, "geometry")
    ET.SubElement(geom_v, "mesh", {
        "filename": mesh_rel,
        "scale": f"{MM_TO_M} {MM_TO_M} {MM_TO_M}",
    })

    collision = ET.SubElement(link, "collision")
    ET.SubElement(collision, "origin", {"xyz": xyz_str, "rpy": rpy_str})
    geom_c = ET.SubElement(collision, "geometry")
    ET.SubElement(geom_c, "mesh", {
        "filename": mesh_rel,
        "scale": f"{MM_TO_M} {MM_TO_M} {MM_TO_M}",
    })

    com_link, mass, inertia_6 = _inertial_from_mesh(
        mesh_path, visual_offset_mm_deg
    )
    if mass > 0 and inertia_6 is not None:
        inertial = ET.SubElement(link, "inertial")
        ET.SubElement(inertial, "origin", {
            "xyz": f"{com_link[0]:.6f} {com_link[1]:.6f} {com_link[2]:.6f}",
            "rpy": "0 0 0",
        })
        ET.SubElement(inertial, "mass", {"value": f"{mass:.6e}"})
        ixx, ixy, ixz, iyy, iyz, izz = inertia_6
        ET.SubElement(inertial, "inertia", {
            "ixx": f"{ixx:.6e}", "ixy": f"{ixy:.6e}", "ixz": f"{ixz:.6e}",
            "iyy": f"{iyy:.6e}", "iyz": f"{iyz:.6e}", "izz": f"{izz:.6e}",
        })


def _inertial_from_mesh(mesh_path: Path,
                        visual_offset_mm_deg: tuple[list, list]
                        ) -> tuple[list, float, list | None]:
    """Compute (CoM in link-frame metres, mass kg, [ixx,ixy,ixz,iyy,iyz,izz])
    from a link-local STL.

    Steps:
    1. Load mesh (mm), scale to metres.
    2. com_mesh = mesh.center_mass (metres, mesh-local frame).
    3. trimesh 4.x: ``mesh.mass_properties.inertia`` is already around the
       CoM in mesh-local axes (verified by shift-then-recompute giving
       identical values).
    4. Rotate tensor to link-frame axes: I_link = R @ I_mesh @ R.T where R
       is the rotation part of ``visual_offset`` (mesh-local basis vectors
       in link frame). For zero rotation, no-op.
    5. CoM in link frame: com_link = R @ com_mesh + t (point transform
       through visual_offset), where t = visual_offset.translation (m).
    """
    try:
        import numpy as np
        import trimesh
    except ImportError:
        return [0, 0, 0], 0.0, None
    try:
        mesh = trimesh.load(str(mesh_path), force="mesh")
    except Exception:  # noqa: BLE001
        return [0, 0, 0], 0.0, None
    if mesh.is_empty or mesh.volume <= 0:
        return [0, 0, 0], 0.0, None

    mesh.apply_scale(MM_TO_M)  # mm -> m
    com_mesh = np.asarray(mesh.center_mass, dtype=float)

    props = mesh.mass_properties
    if callable(props):  # old trimesh API
        try:
            props = props(density=DEFAULT_DENSITY_KG_M3)
        except TypeError:
            props = props()
    if isinstance(props, dict):
        volume = float(props.get("volume", 0.0))
        mass = volume * DEFAULT_DENSITY_KG_M3
        inertia_mesh = np.asarray(props.get("inertia"), dtype=float)
    else:
        # trimesh 4.x: MassProperties object, density=1.0 by default.
        mass = float(props.mass) * DEFAULT_DENSITY_KG_M3
        inertia_mesh = np.asarray(props.inertia, dtype=float) * DEFAULT_DENSITY_KG_M3
    if mass <= 0 or inertia_mesh.shape != (3, 3):
        return [0, 0, 0], 0.0, None

    # Rotate tensor from mesh-local axes to link-frame axes, and transform
    # CoM from mesh-local to link frame, both via the visual_offset rotation.
    pos_mm, ori_deg = visual_offset_mm_deg
    R = _euler_xyz_deg_to_matrix(ori_deg)
    if R is None:
        R = np.eye(3)
    t_m = np.array([
        float(pos_mm[0]) * MM_TO_M,
        float(pos_mm[1]) * MM_TO_M,
        float(pos_mm[2]) * MM_TO_M,
    ])
    if not np.allclose(R, np.eye(3)):
        inertia_link = R @ inertia_mesh @ R.T
        com_link = R @ com_mesh + t_m
    else:
        inertia_link = inertia_mesh
        com_link = com_mesh + t_m

    ixx = float(inertia_link[0, 0])
    iyy = float(inertia_link[1, 1])
    izz = float(inertia_link[2, 2])
    ixy = float(inertia_link[0, 1])
    ixz = float(inertia_link[0, 2])
    iyz = float(inertia_link[1, 2])
    return [float(com_link[0]), float(com_link[1]), float(com_link[2])], mass, [
        ixx, ixy, ixz, iyy, iyz, izz,
    ]


# ---------------------------------------------------------------------------
# Per-joint emission
# ---------------------------------------------------------------------------


def _emit_ball_dummy_link(robot_elem, label: str) -> None:
    """Emit a kinematic-only dummy <link> for ball joint decomposition.

    No <visual>, no <collision> -- the dummy is purely a kinematic
    intermediate node. Always injects a tiny but legal <inertial>
    (mass=1e-6 kg, inertia=1e-9 identity) so strict URDF validators
    (check_urdf, MoveIt) that reject zero-mass or missing-inertia
    links don't fail. The mass is negligible for any practical
    dynamics sim (1e-6 kg vs typical finger link ~10 g).
    """
    link = ET.SubElement(robot_elem, "link", {"name": label})
    inertial = ET.SubElement(link, "inertial")
    ET.SubElement(inertial, "mass", {"value": "1e-6"})
    ET.SubElement(inertial, "inertia", {
        "ixx": "1e-9", "ixy": "0", "ixz": "0",
        "iyy": "1e-9", "iyz": "0", "izz": "1e-9",
    })


def _emit_joint(robot_elem, mate: dict,
                parent_world: tuple[list, list]) -> None:
    """Emit a <joint> from a mate dict.

    Origin: ``parent_world⁻¹ × joint_world`` where joint_world is the
    fixed_endpoint's WORLD pose (resolved bore centre + Euler XYZ).
    Axis: "0 0 1" for revolute/linear/continuous (URDF convention).

    Ball mates are decomposed into 2 orthogonal revolute joints + 1
    dummy link (standard URDF has no <joint type="ball">):
      socket --pitch (axis Y)--> dummy_link --yaw (axis Z)--> ball
    Cylindrical mates are decomposed into prismatic + continuous + 1
    dummy link (URDF has no <joint type="cylindrical">); both joints
    share the same Z axis (translation + unlimited rotation along it):
      parent --prismatic (axis Z)--> dummy --continuous (axis Z)--> child
    The dummy link is emitted inline via ``_emit_ball_dummy_link``
    before the two joints (reused for both decompositions).
    """
    rel = str(mate.get("relation", "")).lower()
    parent = mate["fixed_endpoint"]["part"]
    child = mate["moving_endpoint"]["part"]
    name = mate.get("label") or f"{parent}_to_{child}"

    if rel == "ball":
        # Decompose into 2 orthogonal revolute joints sharing a dummy
        # intermediate link. The dummy is purely kinematic (mass=1e-6 kg
        # placeholder so strict URDF validators don't reject the link).
        dummy_label = f"{name}_ball_dummy"

        # 1. Dummy link (no visual/collision, tiny inertial).
        _emit_ball_dummy_link(robot_elem, dummy_label)

        # Joint limits from KINEMATIC_SWEEP_DEG (same as revolute).
        try:
            from mac_assembly import config_assembly as cfg
            sweep_deg = float(cfg.KINEMATIC_SWEEP_DEG)
        except Exception:  # noqa: BLE001
            sweep_deg = 30.0
        lim = sweep_deg * DEG_TO_RAD

        # 2. Pitch joint: parent (socket) -> dummy, axis = Y.
        joint_world = _endpoint_to_loc(mate["fixed_endpoint"])
        joint_in_parent = _compose_loc_inverse(parent_world, joint_world)
        xyz_str, rpy_str = _loc_to_origin_strs(*joint_in_parent)
        pitch_joint = ET.SubElement(robot_elem, "joint", {
            "name": f"{name}_pitch", "type": "revolute",
        })
        ET.SubElement(pitch_joint, "parent", {"link": parent})
        ET.SubElement(pitch_joint, "child", {"link": dummy_label})
        ET.SubElement(pitch_joint, "origin", {"xyz": xyz_str, "rpy": rpy_str})
        ET.SubElement(pitch_joint, "axis", {"xyz": "0 1 0"})
        ET.SubElement(pitch_joint, "limit", {
            "lower": f"{-lim:.6f}", "upper": f"{lim:.6f}",
            "effort": "10.0", "velocity": "1.0",
        })

        # 3. Yaw joint: dummy -> child (ball), axis = Z, origin identity
        # (dummy is coincident with the ball center, so the yaw joint's
        # frame in the dummy is at the dummy's origin).
        yaw_joint = ET.SubElement(robot_elem, "joint", {
            "name": f"{name}_yaw", "type": "revolute",
        })
        ET.SubElement(yaw_joint, "parent", {"link": dummy_label})
        ET.SubElement(yaw_joint, "child", {"link": child})
        ET.SubElement(yaw_joint, "origin", {"xyz": "0 0 0", "rpy": "0 0 0"})
        ET.SubElement(yaw_joint, "axis", {"xyz": "0 0 1"})
        ET.SubElement(yaw_joint, "limit", {
            "lower": f"{-lim:.6f}", "upper": f"{lim:.6f}",
            "effort": "10.0", "velocity": "1.0",
        })
        return

    if rel == "cylindrical":
        # Decompose into prismatic + continuous sharing a dummy
        # intermediate link (URDF has no <joint type="cylindrical">).
        # Both joints share the same Z axis (joint frame's Z = physical
        # bore axis, set via the joint origin rpy). Prismatic translates
        # along Z; continuous rotates about Z (unlimited, no <limit>).
        # The dummy is coincident with the joint frame, so the second
        # joint's origin is identity. Mirrors the ball-joint pattern
        # (2 revolutes + dummy) above. Preserves both DOFs (the old
        # `continuous`-only degradation lost the translational DOF).
        dummy_label = f"{name}_cyl_dummy"
        _emit_ball_dummy_link(robot_elem, dummy_label)

        try:
            from mac_assembly import config_assembly as cfg
            sweep_mm = float(cfg.LINEAR_SWEEP_MM)
        except Exception:  # noqa: BLE001
            sweep_mm = 20.0
        lim_m = sweep_mm * MM_TO_M

        # 1. Prismatic: parent -> dummy, axis Z (translation along bore).
        joint_world = _endpoint_to_loc(mate["fixed_endpoint"])
        joint_in_parent = _compose_loc_inverse(parent_world, joint_world)
        xyz_str, rpy_str = _loc_to_origin_strs(*joint_in_parent)
        prismatic_joint = ET.SubElement(robot_elem, "joint", {
            "name": f"{name}_prismatic", "type": "prismatic",
        })
        ET.SubElement(prismatic_joint, "parent", {"link": parent})
        ET.SubElement(prismatic_joint, "child", {"link": dummy_label})
        ET.SubElement(prismatic_joint, "origin", {"xyz": xyz_str, "rpy": rpy_str})
        ET.SubElement(prismatic_joint, "axis", {"xyz": "0 0 1"})
        ET.SubElement(prismatic_joint, "limit", {
            "lower": f"{-lim_m:.6f}", "upper": f"{lim_m:.6f}",
            "effort": "10.0", "velocity": "0.1",
        })

        # 2. Continuous: dummy -> child, axis Z (unlimited rotation),
        # origin identity (dummy coincident with joint frame).
        continuous_joint = ET.SubElement(robot_elem, "joint", {
            "name": f"{name}_continuous", "type": "continuous",
        })
        ET.SubElement(continuous_joint, "parent", {"link": dummy_label})
        ET.SubElement(continuous_joint, "child", {"link": child})
        ET.SubElement(continuous_joint, "origin", {"xyz": "0 0 0", "rpy": "0 0 0"})
        ET.SubElement(continuous_joint, "axis", {"xyz": "0 0 1"})
        # continuous = unlimited rotation; no <limit> element required.
        return

    if rel in ("rigid", "face_to_face", "coaxial"):
        jtype = "fixed"
    elif rel == "revolute":
        jtype = "revolute"
    elif rel == "linear":
        jtype = "prismatic"
    else:
        jtype = "fixed"

    joint = ET.SubElement(robot_elem, "joint", {
        "name": name, "type": jtype,
    })
    ET.SubElement(joint, "parent", {"link": parent})
    ET.SubElement(joint, "child", {"link": child})

    joint_world = _endpoint_to_loc(mate["fixed_endpoint"])
    joint_in_parent = _compose_loc_inverse(parent_world, joint_world)
    xyz_str, rpy_str = _loc_to_origin_strs(*joint_in_parent)
    ET.SubElement(joint, "origin", {"xyz": xyz_str, "rpy": rpy_str})

    if jtype in ("revolute", "prismatic", "continuous"):
        ET.SubElement(joint, "axis", {"xyz": "0 0 1"})
        try:
            from mac_assembly import config_assembly as cfg
            sweep_deg = float(cfg.KINEMATIC_SWEEP_DEG)
            sweep_mm = float(cfg.LINEAR_SWEEP_MM)
        except Exception:  # noqa: BLE001
            sweep_deg, sweep_mm = 30.0, 20.0
        if jtype == "revolute":
            lim = sweep_deg * DEG_TO_RAD
            ET.SubElement(joint, "limit", {
                "lower": f"{-lim:.6f}", "upper": f"{lim:.6f}",
                "effort": "10.0", "velocity": "1.0",
            })
        elif jtype == "prismatic":
            lim = sweep_mm * MM_TO_M
            ET.SubElement(joint, "limit", {
                "lower": f"{-lim:.6f}", "upper": f"{lim:.6f}",
                "effort": "10.0", "velocity": "0.1",
            })


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate(urdf_path: Path) -> tuple[bool, str]:
    """Best-effort structural validation."""
    try:
        import yourdf  # type: ignore
        yourdf.URDF.load(str(urdf_path))
        return True, "yourdf"
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        return False, f"yourdf: {exc}"
    skill_src = Path(
        "/Users/puma/Desktop/text-to-cad-skill/skills/urdf/scripts/urdf"
    )
    if skill_src.is_dir():
        try:
            import sys
            sys.path.insert(0, str(skill_src))
            import source  # type: ignore
            source.read_urdf_source(urdf_path)  # Path, not str
            return True, "urdf-skill source.py"
        except Exception as exc:  # noqa: BLE001
            return False, f"urdf-skill: {exc}"
    return True, "no validator available (skipped)"
