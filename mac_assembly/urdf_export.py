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
  ``parent_link_world⁻¹ × joint_world``. A non-root parent link frame is
  its incoming joint frame, NOT the part mesh frame in the manifest.
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

Zero-pose and joint-limit conventions (D3):

- **URDF joint zero == the CAD static pose.** The mate's static pose
  (``angle_deg`` / ``position_mm`` / ``ball_pitch_deg`` / ``ball_yaw_deg``)
  is baked into the endpoint transforms at assembly time, so a simulator
  opens the assembly exactly as CAD placed it; joint coordinates measure
  excursion FROM that pose.
- **Limits come from MateSpec, not from QA config.** ``limit_lower`` /
  ``limit_upper`` (deg for revolute, mm for linear, relative to joint
  zero) are design-level bounds the Mating Architect may set. Without
  them: revolute exports as ``continuous`` (honestly unlimited) and
  prismatic falls back to +/- ``LINEAR_SWEEP_MM`` -- a PLACEHOLDER only,
  because URDF requires a ``<limit>`` on prismatic joints. Ball /
  cylindrical decompositions keep sweep-based placeholder limits (their
  real bounds are 2-DOF cone angles, not expressible as a scalar pair).
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
import sys
import warnings
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

MM_TO_M = 0.001
DEG_TO_RAD = math.pi / 180.0
DEFAULT_DENSITY_KG_M3 = 1000.0

# Volume below which a link mesh is treated as zero-volume (BUG-008).
# ``_inertial_from_mesh`` already returns mass=0 for ``mesh.volume <= 0``;
# the preflight uses the same threshold (with a small epsilon to catch
# near-degenerate shells) so a zero-volume link never reaches _emit_link
# -- a URDF missing <inertial> blocks lets PyBullet silently assign
# mass=1.0 and MuJoCo reject outright. Kept local rather than imported
# from assembly_qa to avoid coupling the two modules' volume contracts.
_ZERO_VOLUME_EPS_MM3 = 1e-9

# Principal axis name -> URDF <axis xyz> string (ball decomposition).
_AXIS_STRS = {"x": "1 0 0", "y": "0 1 0", "z": "0 0 1"}


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
        labels = {root} | {m["moving_endpoint"]["part"] for m in mates}
        missing_locs = sorted(labels - world_locs.keys())
        if missing_locs:
            print(f"  [urdf] no world location for part(s) {missing_locs}; skipping URDF")
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
        link_world: dict[str, tuple[list, list]] = {
            root: ([0, 0, 0], [0, 0, 0])
        }
        # Root's world transform.
        if root in world_locs:
            visual_offsets[root] = world_locs[root]
        # For each non-root link, find the mate where it's the moving side.
        for m in mates:
            child = m["moving_endpoint"]["part"]
            joint_world = _endpoint_to_loc(m["fixed_endpoint"])
            link_world[child] = joint_world
            child_world = world_locs[child]
            visual_offsets[child] = _compose_loc_inverse(
                joint_world, child_world
            )

        # Collect all labels (root + every child referenced by a mate).
        labels = sorted(labels)

        # Pre-flight: every link needs its mesh. Emitting joints that
        # reference a link whose mesh was skipped produces a URDF with
        # dangling references that only fails when a downstream simulator
        # (PyBullet / MoveIt) loads it -- while the handoff manifest still
        # lists the file as a deliverable. Returning None (no URDF at all,
        # manifest marks it missing) is more honest than a broken file (D4).
        mesh_by_label = {l: _find_part_stl(work_dir, l) for l in labels}
        missing = sorted(l for l, p in mesh_by_label.items() if p is None)
        if missing:
            print(f"  [urdf] no STL for part(s) {missing}; skipping URDF export")
            return None

        # Zero-volume mesh preflight (BUG-008): a zero-volume link emits
        # <visual> + <collision> but no <inertial> -- _inertial_from_mesh
        # returns mass=0 for ``mesh.volume <= 0`` and _emit_link then skips
        # the <inertial> block. PyBullet silently assigns mass=1.0 (with a
        # printed warning); MuJoCo rejects the URDF outright. Fail closed
        # at preflight instead of emitting a URDF with wrong dynamics.
        # Runs BEFORE any link is emitted so no half-URDF reaches disk.
        try:
            import trimesh  # noqa: F401
        except ImportError:
            print("  [urdf] trimesh unavailable; cannot preflight mesh volumes; skipping URDF export")
            return None
        zero_vol_links = []
        for label in labels:
            stl_path = mesh_by_label[label]
            # Load + volume probe in one try: ``getattr`` only defaults when
            # the attribute is ABSENT, not when the property RAISES, so a
            # raising ``volume`` property (malformed mesh) must be caught
            # here rather than escape to the outer handler and print a
            # generic "export failed". NaN/inf volume (``nan <= eps`` is
            # False) would otherwise pass the preflight and let
            # ``_inertial_from_mesh`` compute ``mass = nan`` (skipping
            # ``<inertial>`` -- the exact BUG-008 regression) or
            # ``mass = inf`` (emitting ``<inertial>`` with non-finite
            # dynamics that PyBullet/MuJoCo reject).
            try:
                _preflight_mesh = trimesh.load(str(stl_path), force="mesh")
                preflight_empty = bool(getattr(_preflight_mesh, "is_empty", False))
                preflight_vol = float(getattr(_preflight_mesh, "volume", 0.0))
            except Exception as exc:  # noqa: BLE001 - load/volume failure = unverifiable
                print(f"  [urdf] mesh load or volume probe failed for {label!r}: {exc}; skipping URDF export")
                return None
            if (
                preflight_empty
                or not math.isfinite(preflight_vol)
                or preflight_vol <= _ZERO_VOLUME_EPS_MM3
            ):
                zero_vol_links.append(label)
        if zero_vol_links:
            print(
                f"  [urdf] zero-volume mesh for link(s) {sorted(zero_vol_links)}; "
                f"refusing to emit URDF with wrong dynamics (fix the part geometry)"
            )
            return None

        for label in labels:
            mesh_dst = meshes_dir / f"{label}.stl"
            shutil.copyfile(mesh_by_label[label], mesh_dst)
            voff = visual_offsets.get(label, ([0, 0, 0], [0, 0, 0]))
            _emit_link(robot, label, mesh_dst, voff, urdf_dir)

        for m in mates:
            parent = m["fixed_endpoint"]["part"]
            parent_world = link_world[parent]
            _emit_joint(robot_elem=robot, mate=m, parent_world=parent_world)

        pose_error = _zero_pose_error(robot, world_locs)
        if pose_error:
            print(f"  [urdf] zero-pose mismatch: {pose_error}; skipping URDF")
            return None

        urdf_path = urdf_dir / f"{assembly_name}.urdf"
        candidate_path = urdf_dir / f".{assembly_name}.candidate.urdf"
        ET.ElementTree(robot).write(
            candidate_path, encoding="utf-8", xml_declaration=True
        )

        status, msg = _validate(candidate_path)
        if status == "failed":
            print(f"  [urdf] validation failed: {msg}; skipping URDF")
            candidate_path.unlink(missing_ok=True)
            return None
        elif status == "unvalidated":
            # Honest status (D4): no validator was importable, so "no
            # error found" must NOT be reported as "validated".
            print(f"  [urdf] unvalidated: {msg}")
        candidate_path.replace(urdf_path)
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
    ``moving_endpoint``. Endpoints have ``part``,
    ``position`` (3-list, WORLD mm), ``orientation`` (3-list, Euler XYZ
    deg, WORLD), ``axes`` (dict with x/y/z basis vectors).

    The mates file's ``parameters`` block (static pose angle/position) is
    deliberately NOT carried through: nothing in the URDF path consumes
    it (the static pose is already baked into the endpoint transforms).
    ``limit_lower`` / ``limit_upper`` (merged in by the codegen footer
    from MateSpec, D3) ARE carried: they are the design-level joint
    travel limits -- degrees for revolute, mm for linear.
    ``ball_axis_1`` / ``ball_axis_2`` (also merged in by the codegen
    footer) are carried for the ball decomposition's joint axes.
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
            "limit_lower": r.get("limit_lower"),
            "limit_upper": r.get("limit_upper"),
            "ball_axis_1": r.get("ball_axis_1"),
            "ball_axis_2": r.get("ball_axis_2"),
            "ball_pitch_limit_lower": r.get("ball_pitch_limit_lower"),
            "ball_pitch_limit_upper": r.get("ball_pitch_limit_upper"),
            "ball_yaw_limit_lower": r.get("ball_yaw_limit_lower"),
            "ball_yaw_limit_upper": r.get("ball_yaw_limit_upper"),
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
        out[label] = (list(t), _cad_xyz_to_urdf_rpy(r))
    return out


def _build_link_tree(mates: list[dict]) -> tuple[str | None, bool]:
    """Find root (part that is fixed but never moving) + verify tree.

    A valid URDF link tree requires:
      * exactly one root (a part that is never a moving endpoint)
      * every non-root part has exactly ONE parent (URDF forbids
        multi-parent links -- a diamond DAG A->B, A->C, B->D, C->D would
        emit two <joint> parents for D and break Gazebo/PyBullet)
      * no cycles
      * every node reachable from the root

    The previous implementation used set difference to find the root,
    which silently collapsed duplicates and missed multi-parent children
    (BUG-005).
    """
    parent_count: dict[str, int] = {}
    parents_of: dict[str, list[str]] = {}
    all_nodes: set[str] = set()
    for m in mates:
        f = (m.get("fixed_endpoint") or {}).get("part")
        c = (m.get("moving_endpoint") or {}).get("part")
        if f:
            all_nodes.add(f)
        if c:
            all_nodes.add(c)
            parent_count[c] = parent_count.get(c, 0) + 1
            parents_of.setdefault(c, []).append(f or "")
    # Multi-parent check (URDF DAG-vs-tree): a child with >1 parent is
    # invalid even if the root count is 1 (the diamond case).
    multi_parent = [c for c, n in parent_count.items() if n > 1]
    if multi_parent:
        return None, False
    # Find root(s): nodes that appear as parent but never as child.
    roots = all_nodes - set(parent_count.keys())
    if len(roots) != 1:
        return None, False
    root = next(iter(roots))
    # Reachability + cycle check via BFS from the root. parent_count==1
    # already rules out multi-parent, but a cycle that includes the root
    # or a self-loop still needs graph traversal to detect.
    seen: set[str] = set()
    frontier = [root]
    while frontier:
        cur = frontier.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for m in mates:
            f = (m.get("fixed_endpoint") or {}).get("part")
            c = (m.get("moving_endpoint") or {}).get("part")
            if f == cur and c and c not in seen:
                # Cycle guard: if c == cur, this mate is a self-loop;
                # if c has already been seen via a different path, we
                # already caught multi-parent above. A self-loop here
                # would not have been in parent_count>1 unless the same
                # parent appears twice -- still flag as invalid.
                if c == cur:
                    return None, False
                frontier.append(c)
    if seen != all_nodes:
        # Unreachable nodes exist (e.g. self-loop on a non-root, or a
        # disconnected sub-tree).
        return None, False
    return root, True


def _find_part_stl(work_dir: Path, part_label: str) -> Path | None:
    """Find the link-local STL for a part.

    Looks in ``parts/<part_label>/temp_output_*.stl``; delegates the
    "newest" pick to ``file_utils.newest_file`` (single source of truth,
    R3) so it can never disagree with part_generator's harvest.
    """
    from mac_assembly.file_utils import newest_file

    part_dir = work_dir / "parts" / part_label
    if not part_dir.is_dir():
        return None
    return newest_file(part_dir, "temp_output_*.stl")


# ---------------------------------------------------------------------------
# Geometry helpers (numpy-based, no build123d dependency for the math)
# ---------------------------------------------------------------------------


def _euler_xyz_deg_to_matrix(orient_deg) -> Any | None:
    """Euler XYZ (degrees) -> 3x3 rotation matrix (numpy array).

    Convention: URDF RPY = Rz @ Ry @ Rx. Raw build123d intrinsic XYZ
    values are converted at the JSON loading boundary. Returns ``None`` if numpy
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


def _cad_xyz_to_urdf_rpy(orient_deg) -> list[float]:
    """Convert build123d intrinsic XYZ (Rx @ Ry @ Rz) to URDF RPY degrees."""
    import numpy as np

    rx, ry, rz = (float(v) * DEG_TO_RAD for v in orient_deg)
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return [v / DEG_TO_RAD for v in _matrix_to_euler_xyz_rad(Rx @ Ry @ Rz)]


def _matrix_to_euler_xyz_rad(R) -> list[float]:
    """3x3 rotation matrix -> Euler XYZ radians (extrinsic = URDF rpy)."""
    try:
        from scipy.spatial.transform import Rotation
        # At gimbal lock the Euler triple is non-unique, but the resulting
        # matrix is exact. Repeated warnings for ordinary 90-degree joints
        # obscure actionable exporter diagnostics.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Gimbal lock detected")
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
    """Extract (position_mm, URDF RPY degrees) from a CAD mate endpoint."""
    pos = list(endpoint.get("position", [0, 0, 0]))
    ori = _cad_xyz_to_urdf_rpy(endpoint.get("orientation", [0, 0, 0]))
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


def _zero_pose_error(robot_elem, world_locs: dict) -> str | None:
    """Check every visual's zero-q world pose against the CAD manifest.

    XML parsing alone cannot detect a valid URDF whose links are displaced or
    rotated. Compute FK over the generated tree before delivering the file.
    """
    import numpy as np

    def transform(origin):
        xyz = [float(v) / MM_TO_M for v in origin.get("xyz", "0 0 0").split()]
        rpy = [float(v) / DEG_TO_RAD for v in origin.get("rpy", "0 0 0").split()]
        result = np.eye(4)
        result[:3, :3] = _euler_xyz_deg_to_matrix(rpy)
        result[:3, 3] = xyz
        return result

    joints = robot_elem.findall("joint")
    children = {j.find("child").get("link") for j in joints}
    roots = {l.get("name") for l in robot_elem.findall("link")} - children
    if len(roots) != 1:
        return "URDF has no unique root"
    link_world = {roots.pop(): np.eye(4)}
    pending = list(joints)
    while pending:
        ready = [j for j in pending if j.find("parent").get("link") in link_world]
        if not ready:
            return "URDF joint tree is disconnected"
        for joint in ready:
            parent = joint.find("parent").get("link")
            child = joint.find("child").get("link")
            link_world[child] = link_world[parent] @ transform(joint.find("origin"))
            pending.remove(joint)

    for link in robot_elem.findall("link"):
        label = link.get("name")
        if label not in world_locs:
            continue  # ball/cylindrical kinematic dummy links have no CAD mesh
        visual = link.find("visual")
        if visual is None:
            return f"{label}: missing visual"
        actual = link_world[label] @ transform(visual.find("origin"))
        pos_mm, rpy_deg = world_locs[label]
        expected = np.eye(4)
        expected[:3, :3] = _euler_xyz_deg_to_matrix(rpy_deg)
        expected[:3, 3] = pos_mm
        position_error = float(np.linalg.norm(actual[:3, 3] - expected[:3, 3]))
        relative = actual[:3, :3].T @ expected[:3, :3]
        angle_error = math.degrees(math.acos(max(-1.0, min(1.0,
            (float(np.trace(relative)) - 1.0) / 2.0))))
        if position_error > 0.1 or angle_error > 0.1:
            return (f"{label}: {position_error:.3f} mm, "
                    f"{angle_error:.3f} deg")
    return None


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

    Ball mates are decomposed into 2 revolute joints + 1 dummy link
    (standard URDF has no <joint type="ball">):
      socket --pitch (axis ball_axis_1)--> dummy_link
      --yaw (axis ball_axis_2)--> ball
    with ball_axis_1/2 from the MateSpec (default y/z -- the two axes
    should be the ones perpendicular to the limb direction; rotation
    about the limb's own long axis is twist, not bending).
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
        # Decompose into 2 revolute joints sharing a dummy intermediate
        # link, one per DOF axis from the MateSpec (ball_axis_1/2,
        # defaulting to the legacy y/z). The axes are the same principal
        # axes the CAD static pose was composed from (codegen emits the
        # equivalent Euler triple); URDF joint coordinates then measure
        # excursion about the same axes. The dummy is purely kinematic
        # (mass=1e-6 kg placeholder so strict URDF validators don't
        # reject the link).
        dummy_label = f"{name}_ball_dummy"
        axis1 = str(mate.get("ball_axis_1") or "y").lower()
        axis2 = str(mate.get("ball_axis_2") or "z").lower()

        # 1. Dummy link (no visual/collision, tiny inertial).
        _emit_ball_dummy_link(robot_elem, dummy_label)

        # Per-DOF ball joint limits (degrees, optional). A ball mate's real
        # limit is a 2-DOF cone angle, which the MateSpec limit_lower/
        # limit_upper pair cannot express; MateSpec validators reject
        # limit_* on ball mates. For joints that need design-faithful
        # per-axis limits (e.g. xhand thumb_bend 0~105 deg about Z, rota1
        # -40~100 deg about X), callers merge these per-DOF fields into
        # assembly_mates.json; absent them, fall back to the QA sweep
        # range (a sampling parameter, NOT a design limit -- D3).
        try:
            from mac_assembly import config_assembly as cfg
            sweep_deg = float(cfg.KINEMATIC_SWEEP_DEG)
        except Exception:  # noqa: BLE001
            sweep_deg = 30.0

        def _ball_lim(lo_field: str, hi_field: str) -> tuple[float, float]:
            lo = mate.get(lo_field)
            hi = mate.get(hi_field)
            if lo is None or hi is None:
                return -sweep_deg, sweep_deg
            return float(lo), float(hi)

        pitch_lo, pitch_hi = _ball_lim("ball_pitch_limit_lower", "ball_pitch_limit_upper")
        yaw_lo, yaw_hi = _ball_lim("ball_yaw_limit_lower", "ball_yaw_limit_upper")

        # 2. Pitch joint: parent (socket) -> dummy, axis = ball_axis_1.
        joint_world = _endpoint_to_loc(mate["fixed_endpoint"])
        joint_in_parent = _compose_loc_inverse(parent_world, joint_world)
        xyz_str, rpy_str = _loc_to_origin_strs(*joint_in_parent)
        pitch_joint = ET.SubElement(robot_elem, "joint", {
            "name": f"{name}_pitch", "type": "revolute",
        })
        ET.SubElement(pitch_joint, "parent", {"link": parent})
        ET.SubElement(pitch_joint, "child", {"link": dummy_label})
        ET.SubElement(pitch_joint, "origin", {"xyz": xyz_str, "rpy": rpy_str})
        ET.SubElement(pitch_joint, "axis", {"xyz": _AXIS_STRS[axis1]})
        ET.SubElement(pitch_joint, "limit", {
            "lower": f"{pitch_lo * DEG_TO_RAD:.6f}",
            "upper": f"{pitch_hi * DEG_TO_RAD:.6f}",
            "effort": "10.0", "velocity": "1.0",
        })

        # 3. Yaw joint: dummy -> child (ball), axis = ball_axis_2, origin
        # identity (dummy is coincident with the ball center, so the yaw
        # joint's frame in the dummy is at the dummy's origin). The dummy
        # frame rotates with the pitch joint, so axis2 acts in the
        # post-pitch frame -- intrinsic composition, same as codegen.
        yaw_joint = ET.SubElement(robot_elem, "joint", {
            "name": f"{name}_yaw", "type": "revolute",
        })
        ET.SubElement(yaw_joint, "parent", {"link": dummy_label})
        ET.SubElement(yaw_joint, "child", {"link": child})
        ET.SubElement(yaw_joint, "origin", {"xyz": "0 0 0", "rpy": "0 0 0"})
        ET.SubElement(yaw_joint, "axis", {"xyz": _AXIS_STRS[axis2]})
        ET.SubElement(yaw_joint, "limit", {
            "lower": f"{yaw_lo * DEG_TO_RAD:.6f}",
            "upper": f"{yaw_hi * DEG_TO_RAD:.6f}",
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

        # PLACEHOLDER (D3): +/- LINEAR_SWEEP_MM is a QA sampling range,
        # not a design limit. A cylindrical mate's travel isn't
        # expressible in the revolute/linear limit_lower/limit_upper
        # pair, so the decomposition keeps this fallback.
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

    if rel in ("rigid", "face_to_face"):
        jtype = "fixed"
    elif rel == "coaxial":
        jtype = "continuous"
    elif rel == "revolute":
        # Explicit MateSpec limits -> bounded revolute; no limits ->
        # continuous (unlimited rotation). Borrowing the QA sweep range
        # here would pass a sampling parameter off as a design limit (D3).
        lo, hi = mate.get("limit_lower"), mate.get("limit_upper")
        jtype = "revolute" if (lo is not None and hi is not None) else "continuous"
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

    # ball and cylindrical relations returned early (decomposed above);
    # jtype here is one of revolute / continuous / prismatic / fixed.
    if jtype == "continuous":
        ET.SubElement(joint, "axis", {"xyz": "0 0 1"})
        # continuous = unlimited rotation; URDF requires no <limit>.
    elif jtype in ("revolute", "prismatic"):
        ET.SubElement(joint, "axis", {"xyz": "0 0 1"})
        lo, hi = mate.get("limit_lower"), mate.get("limit_upper")
        if lo is not None and hi is not None:
            # Explicit design limits from MateSpec: degrees for revolute,
            # mm for linear (schema-enforced pairing, lower < upper).
            scale = DEG_TO_RAD if jtype == "revolute" else MM_TO_M
            lower, upper = float(lo) * scale, float(hi) * scale
        else:
            # prismatic ONLY (an unlimited revolute is "continuous"
            # above). PLACEHOLDER: URDF requires a <limit> on prismatic
            # joints, so fall back to the QA linear sweep range -- a
            # sampling parameter, NOT a design limit (D3).
            try:
                from mac_assembly import config_assembly as cfg
                sweep_mm = float(cfg.LINEAR_SWEEP_MM)
            except Exception:  # noqa: BLE001
                sweep_mm = 20.0
            lower = -sweep_mm * MM_TO_M
            upper = sweep_mm * MM_TO_M
        ET.SubElement(joint, "limit", {
            "lower": f"{lower:.6f}", "upper": f"{upper:.6f}",
            "effort": "10.0",
            "velocity": "1.0" if jtype == "revolute" else "0.1",
        })


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate(urdf_path: Path) -> tuple[str, str]:
    """Best-effort structural validation.

    Returns ``(status, validator_msg)`` where status is one of:
      * ``"ok"``          -- a real validator parsed the file successfully.
      * ``"failed"``      -- a validator ran and rejected the file.
      * ``"unvalidated"`` -- no validator was importable; the file was NOT
        checked. Callers must surface this state instead of reporting a
        pass (D4).
    """
    try:
        import yourdf  # type: ignore
        yourdf.URDF.load(str(urdf_path))
        return "ok", "yourdf"
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        return "failed", f"yourdf: {exc}"
    # Fallback: load the urdf skill's source.py validator without polluting
    # sys.modules (avoids clobbering an unrelated 'source' module). Resolve
    # from $MAC_URDF_SKILL_PATH first, then the sibling text-to-cad-skill
    # repo next to this one; falls through to "no validator" if neither exists.
    env_path = os.environ.get("MAC_URDF_SKILL_PATH", "").strip()
    skill_dir = Path(env_path) if env_path else (
        Path(__file__).resolve().parents[2]
        / "text-to-cad-skill" / "skills" / "urdf" / "scripts" / "urdf"
    )
    source_path = skill_dir / "source.py"
    if source_path.is_file():
        try:
            module_name = "_mac_urdf_skill_source"
            spec = importlib.util.spec_from_file_location(
                module_name, source_path
            )
            if spec is None or spec.loader is None:
                return "failed", "urdf-skill: spec load failed"
            module = importlib.util.module_from_spec(spec)
            # source.py declares dataclasses with postponed annotations;
            # dataclasses needs the executing module in sys.modules. Keep
            # registration scoped so the optional validator cannot shadow
            # another module named 'source'.
            previous = sys.modules.get(module_name)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            finally:
                if previous is None:
                    sys.modules.pop(module_name, None)
                else:
                    sys.modules[module_name] = previous
            module.read_urdf_source(urdf_path)  # Path, not str
            return "ok", "urdf-skill source.py"
        except Exception as exc:  # noqa: BLE001
            return "failed", f"urdf-skill: {exc}"
    return "unvalidated", "no validator available (yourdf / urdf-skill not importable)"
