"""Render STL meshes to multi-angle PNGs for multimodal LLM input.

Used by ``node_judge_qa`` (Phase 2.5 of the autonomous skill loop) to give
the QA Judge agent actual visual evidence of the current model geometry —
mitigating the "spatial-imagination hallucination" risk where the LLM,
seeing only structured text (feature_measurements, error_details), generates
plausible-sounding but physically-wrong ACCEPT reasons.

Renders 4 isometric views via matplotlib's ``Poly3DCollection`` — headless
(no pyglet / OpenGL context required), no new deps beyond matplotlib + PIL
which are already in the environment.

Usage::

    from multi_agent_cad.render_views import (
        _render_isometric_views, _encode_png_data_url, _save_views_to_disk,
    )
    pngs = _render_isometric_views(stl_path, n_views=4, size=512)
    data_url = _encode_png_data_url(pngs[0])
    # → "data:image/png;base64,iVBORw0KG..."
"""
from __future__ import annotations

import base64
import io
from pathlib import Path


# 4 isometric view angles — covers all 8 octants of the bbox.
# Each tuple is (elevation, azimuth) in degrees.
_ISOMETRIC_VIEWS_4 = [
    (+30, +45),   # front-left-top
    (+30, +135),  # back-right-top
    (-30, +45),   # front-right-bottom
    (-30, +135),  # back-left-bottom
]


def _render_one_view(
    mesh,
    elev: float,
    azim: float,
    size: int = 512,
) -> bytes:
    """Render one isometric view of the mesh to PNG bytes.

    Parameters
    ----------
    mesh : trimesh.Trimesh
        Loaded STL mesh (already normalized to bbox-center origin, max-extent=1).
    elev : float
        Elevation angle in degrees (matplotlib convention).
    azim : float
        Azimuth angle in degrees (matplotlib convention).
    size : int
        Output image resolution (square, PNG). Default 512.

    Returns
    -------
    bytes
        PNG image bytes.
    """
    # Lazy imports — matplotlib is heavy (~200ms); only paid when multimodal is on.
    import matplotlib
    matplotlib.use("Agg")  # headless backend, no display required
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection, Line3DCollection
    import math
    import numpy as np

    dpi = 100
    figsize = (size / dpi, size / dpi)
    fig = plt.figure(figsize=figsize, dpi=dpi)
    try:
        ax = fig.add_subplot(111, projection="3d")

        # Convert mesh.faces (vertex indices) → polygon vertices (F, 3, 3).
        # Poly3DCollection expects actual vertex coordinates, not indices.
        verts = np.asarray(mesh.vertices)  # (V, 3)
        faces = np.asarray(mesh.faces)     # (F, 3) integer indices
        polygons = verts[faces]            # (F, 3, 3) — list of triangles as vertex coords

        # Filter edges: only draw true geometric feature edges (棱线、孔缘、
        # 倒角过渡)，skip STL triangulation edges on smooth surfaces.
        # trimesh.face_adjacency_angles gives the dihedral angle between
        # each pair of adjacent faces (radians). Threshold of 10° filters
        # out edges where faces are nearly coplanar (smooth surface) while
        # keeping real geometric edges (curvature discontinuities).
        feature_edges = None
        try:
            angles = np.asarray(mesh.face_adjacency_angles)  # (E,) radians
            # face_adjacency_edges returns (E, 2) vertex INDICES — must convert
            # to (E, 2, 3) vertex coordinates for Line3DCollection.
            edge_pair_indices = np.asarray(mesh.face_adjacency_edges)  # (E, 2) int
            threshold = math.radians(10)  # 10° dihedral angle
            feature_mask = angles > threshold
            if np.any(feature_mask):
                feature_edge_indices = edge_pair_indices[feature_mask]  # (E', 2) int
                feature_edges = mesh.vertices[feature_edge_indices]  # (E', 2, 3) coords
        except Exception as exc:
            # If feature-edge detection fails, fall back to drawing all edges
            # (better noisy edges than no edges).
            print(f"[render_views] feature-edge detection failed: {exc}")

        # Poly3DCollection: faces only, no edges (edgecolors='none').
        # Drawing STL triangulation edges on smooth surfaces creates visual
        # noise that distracts the multimodal LLM and can be misread as
        # surface texture. Real geometric edges are drawn separately below.
        collection = Poly3DCollection(
            polygons,
            edgecolors="none",      # no triangle edges — smooth surfaces stay smooth
            alpha=0.9,
            facecolors="#A0A0A0",   # neutral gray — LLM focuses on shape, not color cues
        )
        ax.add_collection3d(collection)

        # Line3DCollection: real geometric feature edges only (孔缘、棱线、
        # 面与面的交线、倒角过渡). 0.8px black — thicker than the previous
        # 0.2px because we now draw far fewer lines (only true feature edges),
        # and the increased thickness survives the 512px downscale while
        # making the contours instantly visible against the gray faces.
        if feature_edges is not None and len(feature_edges) > 0:
            line_collection = Line3DCollection(
                feature_edges,
                colors="black",
                linewidths=0.8,
            )
            ax.add_collection3d(line_collection)

        # Set axis limits based on the (normalized) mesh extents.
        # After normalization max-extent = 1, so range is roughly [-0.5, 0.5].
        extents = mesh.extents  # [dx, dy, dz]
        max_extent = float(extents.max()) if len(extents) else 1.0
        if max_extent < 1e-6:
            max_extent = 1.0
        half = max_extent / 2.0 * 1.1  # 10% padding
        ax.set_xlim(-half, half)
        ax.set_ylim(-half, half)
        ax.set_zlim(-half, half)

        # Preserve aspect ratio so z-axis doesn't get stretched.
        try:
            ax.set_box_aspect([1, 1, 1])
        except AttributeError:
            pass  # older matplotlib lacks set_box_aspect

        # Apply view angle.
        ax.view_init(elev=elev, azim=azim)

        # Strip axis labels / ticks / panes for a clean image.
        ax.set_axis_off()
        try:
            # Hide the gray panes (x/y/z back walls) — leaves just the mesh.
            ax.xaxis.pane.set_visible(False)
            ax.yaxis.pane.set_visible(False)
            ax.zaxis.pane.set_visible(False)
            ax.grid(False)
        except AttributeError:
            pass

        # Tight bbox, transparent background, no padding.
        buf = io.BytesIO()
        fig.savefig(
            buf,
            format="png",
            bbox_inches="tight",
            pad_inches=0,
            transparent=False,
            facecolor="white",
        )
        return buf.getvalue()
    finally:
        plt.close(fig)


def _render_isometric_views(
    stl_path: Path,
    n_views: int = 4,
    size: int = 512,
) -> list[bytes]:
    """Render N isometric views of the STL to PNG bytes.

    Parameters
    ----------
    stl_path : Path
        Path to the .stl file.
    n_views : int
        Number of views (currently 4 supported; n_views > 4 truncates, < 4 returns fewer).
    size : int
        Image resolution (square, PNG).

    Returns
    -------
    list[bytes]
        List of PNG image bytes (one per view). Empty list on failure.
    """
    try:
        import trimesh
        import numpy as np
    except ImportError as exc:
        print(f"[render_views] trimesh/numpy not available: {exc}")
        return []

    try:
        mesh = trimesh.load(str(stl_path), force="mesh")
        if mesh is None or len(getattr(mesh, "faces", [])) == 0:
            print(f"[render_views] empty mesh loaded from {stl_path}")
            return []
    except Exception as exc:
        print(f"[render_views] failed to load {stl_path}: {exc}")
        return []

    # Normalize: translate to bbox center, scale so max-extent = 1.
    try:
        bbox_min = mesh.vertices.min(axis=0)
        bbox_max = mesh.vertices.max(axis=0)
        center = (bbox_min + bbox_max) / 2.0
        extents = bbox_max - bbox_min
        max_extent = float(extents.max())
        if max_extent < 1e-9:
            print(f"[render_views] degenerate mesh (max_extent={max_extent})")
            return []
        scale = 1.0 / max_extent
        # Apply transform: translate to origin, then scale
        mesh.apply_translation(-center)
        mesh.apply_scale(scale)
    except Exception as exc:
        print(f"[render_views] normalization failed: {exc}")
        return []

    # Render each view.
    views: list[bytes] = []
    angles = _ISOMETRIC_VIEWS_4[:n_views]
    for i, (elev, azim) in enumerate(angles):
        try:
            png = _render_one_view(mesh, elev=elev, azim=azim, size=size)
            if png:
                views.append(png)
            else:
                print(f"[render_views] view {i} (elev={elev}, azim={azim}) returned empty bytes")
        except Exception as exc:
            print(f"[render_views] view {i} (elev={elev}, azim={azim}) failed: {exc}")
            continue

    return views


def _save_views_to_disk(
    views: list[bytes],
    out_dir: Path,
    prefix: str = "view",
) -> list[Path]:
    """Save rendered PNG bytes to disk for audit / debugging.

    Parameters
    ----------
    views : list[bytes]
        PNG image bytes (one per view).
    out_dir : Path
        Directory to write to. Created if it doesn't exist.
    prefix : str
        Filename prefix; files named ``{prefix}_{i}.png`` (0-indexed).

    Returns
    -------
    list[Path]
        Paths of saved files.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, png in enumerate(views):
        p = out_dir / f"{prefix}_{i}.png"
        try:
            p.write_bytes(png)
            paths.append(p)
        except OSError as exc:
            print(f"[render_views] failed to write {p}: {exc}")
    return paths


def _encode_png_data_url(png_bytes: bytes) -> str:
    """Base64-encode PNG bytes and wrap as a data URL for OpenAI image_url format.

    Returns ``"data:image/png;base64,<base64-encoded-bytes>"``.
    """
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"
