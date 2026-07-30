#!/usr/bin/env python3
"""
3D支架模型分析工具 —— 将STL/OBJ/STEP等3D模型转换为AI可理解的自然语言报告。

功能模块：
  1. 精确尺寸分析 —— 包围盒、壁厚、截面分析
  2. 孔洞分析 —— 通孔/盲孔检测、孔距、边距
  3. 结构特征分析 —— 质心、惯性矩、锐边检测、加强筋
  4. 对称性与拓扑分析 —— 对称检测、结构类型推断
  5. 可制造性评估 —— 3D打印、CNC、材料适配
  6. 规格对比 —— 与目标尺寸/公差对比

用法:
  python check_mesh.py <模型文件>                    # 默认 JSON 输出
  python check_mesh.py <模型文件> --format markdown  # 详细中文报告
  python check_mesh.py <模型文件> --spec spec.json   # 规格对比
  python check_mesh.py --generate l_bracket -o test.stl  # 生成测试模型
"""

import trimesh
import numpy as np
import json
import sys
import os
import argparse
import warnings
from collections import defaultdict

# 屏蔽 trimesh 的冗余警告
warnings.filterwarnings("ignore", category=UserWarning)

# ============================================================================
#  工程标准常量
# ============================================================================

# 标准螺纹孔映射表（ISO公制粗牙）
# 格式: { 螺丝规格: (螺丝公称直径, 紧配孔径, 松配孔径, 沉头孔直径, 沉头角度) }
STANDARD_HOLE_CLEARANCES = {
    "M2":   {"screw_dia": 2.0, "tight_fit": 2.2, "loose_fit": 2.4, "countersink_dia": 4.0, "countersink_angle": 90},
    "M2.5": {"screw_dia": 2.5, "tight_fit": 2.7, "loose_fit": 3.0, "countersink_dia": 5.0, "countersink_angle": 90},
    "M3":   {"screw_dia": 3.0, "tight_fit": 3.2, "loose_fit": 3.4, "countersink_dia": 6.0, "countersink_angle": 90},
    "M4":   {"screw_dia": 4.0, "tight_fit": 4.2, "loose_fit": 4.5, "countersink_dia": 8.0, "countersink_angle": 90},
    "M5":   {"screw_dia": 5.0, "tight_fit": 5.3, "loose_fit": 5.5, "countersink_dia": 10.0, "countersink_angle": 90},
    "M6":   {"screw_dia": 6.0, "tight_fit": 6.4, "loose_fit": 6.6, "countersink_dia": 12.0, "countersink_angle": 90},
    "M8":   {"screw_dia": 8.0, "tight_fit": 8.4, "loose_fit": 9.0, "countersink_dia": 16.0, "countersink_angle": 90},
    "M10":  {"screw_dia": 10.0, "tight_fit": 10.5, "loose_fit": 11.0, "countersink_dia": 20.0, "countersink_angle": 90},
    "M12":  {"screw_dia": 12.0, "tight_fit": 13.0, "loose_fit": 13.5, "countersink_dia": 24.0, "countersink_angle": 90},
}


def lookup_standard_hole(screw_spec):
    """
    查询标准孔径。支持 "M3", "M4紧配", "M5松配" 等格式。
    返回建议的孔径和说明。
    """
    # 解析规格
    import re
    match = re.match(r'(M\d+(?:\.\d+)?)\s*(紧配|松配|沉头)?', screw_spec)
    if not match:
        return None
    screw_size = match.group(1)
    fit_type = match.group(2) or "默认"

    std = STANDARD_HOLE_CLEARANCES.get(screw_size)
    if not std:
        return None

    if fit_type in ("沉头",):
        return {
            "screw": screw_size,
            "recommended_hole_dia_mm": std["tight_fit"],
            "countersink_top_dia_mm": std["countersink_dia"],
            "countersink_angle_deg": std["countersink_angle"],
            "fit": "沉头孔",
            "note": f"通孔直径 {std['tight_fit']}mm + 沉头孔 {std['countersink_dia']}mm×{std['countersink_angle']}°",
        }
    if fit_type == "紧配" or fit_type == "默认":
        dia = std["tight_fit"]
        label = "紧配（定位孔）"
    else:
        dia = std["loose_fit"]
        label = "松配（自由孔）"

    return {
        "screw": screw_size,
        "recommended_hole_dia_mm": dia,
        "fit": label,
        "note": f"{screw_size}螺丝{label}建议孔径 {dia}mm",
    }


def load_feature_measurements(iteration=0, filename=None):
    """
    加载白盒插桩法生成的特征测量数据。

    这些测量是在布尔合并之前对独立特征的精确测量，
    避免了从合并后的 STEP 文件中逆向猜测特征的问题。

    Args:
        iteration: 迭代次数，用于定位 temp_measurements_{iteration}.json
        filename: 可选的自定义文件名

    Returns:
        dict: 特征测量数据，如果文件不存在则返回空字典
    """
    if filename is None:
        filename = f"temp_measurements_{iteration}.json"

    if not os.path.exists(filename):
        return {}

    try:
        with open(filename, 'r', encoding='utf-8') as f:
            measurements = json.load(f)
        return measurements
    except Exception as e:
        print(f"[WARNING] 无法加载特征测量文件 {filename}: {e}", file=sys.stderr)
        return {}


# ============================================================================
#  工具函数
# ============================================================================

def _load_mesh(file_path):
    """统一加载模型，处理 Scene 和单网格两种情况。"""
    obj = trimesh.load(file_path, force='mesh')
    if isinstance(obj, trimesh.Scene):
        geoms = list(obj.geometry.values())
        if not geoms:
            raise ValueError("场景为空或无法解析。")
        mesh = trimesh.util.concatenate(geoms)
    elif isinstance(obj, trimesh.Trimesh):
        mesh = obj
    else:
        raise TypeError(f"不支持的文件类型: {type(obj)}")
    # 合并重复顶点，修复法线
    mesh.merge_vertices()
    mesh.fix_normals()
    return mesh


def _safe_round(val, ndigits=2):
    """安全四舍五入，处理 None 和数组。"""
    if val is None:
        return None
    try:
        return round(float(val), ndigits)
    except (TypeError, ValueError):
        return val


def _vec3_to_list(v):
    """将 (3,) numpy 数组转为 [x, y, z] 列表。"""
    if v is None:
        return None
    return [_safe_round(x, 3) for x in np.asarray(v).flatten()[:3]]


def _estimate_mesh_resolution(mesh):
    """
    估算网格的实际分辨率（mm），即测量可靠性的下限。

    策略：分离"特征边"（短边，来自圆柱孔/圆角的多边形逼近）
    和"结构边"（长边，来自平面三角剖分）。用特征边的典型长度
    作为分辨率指标——因为孔、圆角等曲面特征决定了尺寸测量的精度瓶颈。

    返回: { resolution_mm, max_theoretical_error_mm, recommendation }
    """
    try:
        edges = mesh.edges_unique
        edge_lengths = np.linalg.norm(
            mesh.vertices[edges[:, 0]] - mesh.vertices[edges[:, 1]], axis=1
        )
        if len(edge_lengths) < 10:
            return {"resolution_mm": 0.1, "max_theoretical_error_mm": 0.15, "recommendation": "网格太简单，按默认精度处理。"}

        # 第5百分位：最短的5%边 → 特征分辨率（圆柱面、圆角等）
        p05 = float(np.percentile(edge_lengths, 5))
        # 第95百分位：最长的5%边 → 结构尺度（大平面三角剖分）
        p95 = float(np.percentile(edge_lengths, 95))
        # 中位数
        p50 = float(np.median(edge_lengths))

        # 用第5百分位作为真实特征分辨率
        feature_resolution = p05

        # 合理性检查：如果特征分辨率太接近结构尺度，说明模型可能没有曲面特征
        if feature_resolution > p50 * 0.5:
            feature_resolution = min(feature_resolution, 0.5)

        # 噪声阈值 = 特征边长（多边形逼近的最大理论偏差 ≈ 弦长本身）
        # 对于直径测量来说，实际偏差通常是特征边长的 0.5~1.5 倍
        if feature_resolution < 0.1:
            level = "fine"
            max_error = 0.10
            rec = "网格精度充足（angular_deflection<0.05 级别），测量值可信。"
        elif feature_resolution < 0.25:
            level = "good"
            max_error = 0.20
            rec = "网格精度良好（angular_deflection≈0.1 级别），直径测量误差约 ±0.15~0.2mm。"
        elif feature_resolution < 0.6:
            level = "coarse"
            max_error = 0.35
            rec = "网格偏粗（angular_deflection>0.5），直径测量误差可达 ±0.35mm。建议降低 angular_deflection。"
        else:
            level = "very_coarse"
            max_error = 0.5
            rec = "曲面特征分辨率 >0.6mm，圆孔测量值不精确，强烈建议设置 angular_deflection<=0.1。"

        return {
            "resolution_mm": _safe_round(feature_resolution, 3),
            "p50_edge_mm": _safe_round(p50, 3),
            "p95_edge_mm": _safe_round(p95, 3),
            "max_theoretical_error_mm": _safe_round(min(max_error, 0.5), 2),
            "level": level,
            "recommendation": rec,
        }
    except Exception:
        return {"resolution_mm": None, "max_theoretical_error_mm": None, "level": "unknown"}


def _check_connectivity(mesh):
    """
    拓扑连通性检查——检测STL是否包含多个分离的独立网格块。

    如果部件之间没有正确 union（如底板和侧板之间有缝隙），
    导出的STL会包含多个互不连接的网格块，切片打印时会散架。
    这是一个致命错误，必须反馈给AI。

    使用 Union-Find 算法检测连通分量，不依赖 networkx。

    返回: { is_single_body, body_count, body_volumes, is_fatal, message }
    """
    try:
        n_faces = len(mesh.faces)
        if n_faces == 0:
            return {"is_single_body": True, "body_count": 0,
                    "is_fatal": False, "message": "Empty mesh"}

        # --- Union-Find on face adjacency (no networkx needed) ---
        parent = list(range(n_faces))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for f1, f2 in mesh.face_adjacency:
            union(int(f1), int(f2))

        # Group faces by component root
        components = {}
        for i in range(n_faces):
            root = find(i)
            if root not in components:
                components[root] = []
            components[root].append(i)

        body_count = len(components)

        if body_count <= 1:
            return {
                "is_single_body": True,
                "body_count": 1,
                "is_fatal": False,
                "message": "拓扑正常：模型为单一连通实体。",
            }

        # Estimate volume per component from face area ratios
        face_areas = mesh.area_faces
        try:
            total_vol = float(mesh.volume)
        except Exception:
            total_vol = 0.0
        total_area = float(face_areas.sum()) if len(face_areas) > 0 else 1.0

        body_info = []
        for idx, (root, face_indices) in enumerate(
            sorted(components.items(), key=lambda x: -sum(face_areas[i] for i in x[1]))
        ):
            comp_area = sum(float(face_areas[i]) for i in face_indices)
            # Estimate volume proportional to surface area
            comp_vol = total_vol * (comp_area / total_area) if total_area > 0 else 0.0
            body_info.append({
                "body_index": idx + 1,
                "volume_mm3": _safe_round(comp_vol, 2),
                "vertex_count": len(set(
                    v for fi in face_indices for v in mesh.faces[fi]
                )),
            })

        total_vol_check = sum(b["volume_mm3"] for b in body_info) or 1.0

        # 过滤掉微小的碎块（体积 < 总体积的 0.1%），这些可能是布尔运算残留碎屑
        significant_bodies = [b for b in body_info if b["volume_mm3"] > total_vol_check * 0.001]
        tiny_fragments = [b for b in body_info if b["volume_mm3"] <= total_vol_check * 0.001]

        if len(significant_bodies) <= 1 and not tiny_fragments:
            return {
                "is_single_body": True,
                "body_count": 1,
                "is_fatal": False,
                "message": "拓扑正常：模型为单一连通实体。",
            }

        # 致命错误判定
        if len(significant_bodies) >= 2:
            fatal = True
            _body_desc = ", ".join(
                "#{}: {}mm³".format(b["body_index"], b["volume_mm3"])
                for b in significant_bodies
            )
            msg = (
                f"🔴 **致命错误**：模型包含 {len(significant_bodies)} 个相互分离的独立部件！"
                f"各部件体积：{_body_desc}。"
                f"这意味着 build123d 代码中的部件没有正确 union 在一起——"
                f"可能是位置偏移 (Pos/location) 有误导致部件之间存在缝隙。"
                f"请确保使用布尔运算 (+ 或 -=) 将所有部件合并为单一实体。"
                f"请检查所有 translate 偏移量，确保部件相互穿透（重叠至少 0.1mm），并用 union() 包裹所有部件。"
                f"（例外：如果用户明确要求多个独立实体如装配体/齿轮系统，可忽略此错误。）"
            )
        elif tiny_fragments:
            fatal = False
            msg = (
                f"🟡 检测到 {len(tiny_fragments)} 个微小碎块"
                f"（总体积 < 0.1%），这可能来自布尔运算残留，通常不影响打印。"
            )
        else:
            fatal = False
            msg = ""

        return {
            "is_single_body": len(significant_bodies) <= 1,
            "body_count": body_count,
            "significant_bodies": len(significant_bodies),
            "body_volumes": body_info,
            "is_fatal": fatal,
            "message": msg,
        }
    except Exception as e:
        return {
            "is_single_body": True,
            "body_count": 1,
            "is_fatal": False,
            "message": f"连通性检查失败: {str(e)}",
        }


def _deviation_is_noise(actual, target, mesh_resolution, tolerance=0.1):
    """
    判断测量偏差是否在网格噪声范围内（不是真正的设计偏差）。
    - mesh_resolution: _estimate_mesh_resolution 的返回值
    - tolerance: 用户额外指定的公差

    返回: {is_noise, explanation}
    """
    try:
        delta = abs(actual - target)
        noise_floor = mesh_resolution.get("max_theoretical_error_mm", 0.1)
        level = mesh_resolution.get("level", "good")

        if level == "fine":
            threshold = max(noise_floor, 0.05)
        elif level == "good":
            threshold = max(noise_floor, 0.15)
        elif level == "coarse":
            threshold = max(noise_floor, 0.3)
        else:
            threshold = max(noise_floor, 0.5)

        # 同时受限于用户公差
        effective_threshold = max(threshold, tolerance * 0.5)

        if delta <= effective_threshold:
            return {
                "is_noise": True,
                "explanation": (
                    f"偏差 {delta:.2f}mm 在网格噪声范围内"
                    f"（噪声阈值={effective_threshold:.2f}mm，网格={mesh_resolution.get('resolution_mm', '?')}mm边）。"
                    f"此偏差由多边形逼近产生，非设计错误，无需修改代码。"
                ),
            }
        else:
            return {
                "is_noise": False,
                "explanation": f"偏差 {delta:.2f}mm 超出网格噪声阈值 {effective_threshold:.2f}mm，需检查代码。",
            }
    except Exception:
        return {"is_noise": False, "explanation": "无法判断。"}


# ============================================================================
#  模块一：精确尺寸分析
# ============================================================================

def analyze_dimensions(mesh):
    """
    精确尺寸分析：
    - 包围盒尺寸与长宽比
    - 关键点坐标（质心、几何中心、各面心）
    - 壁厚射线采样
    - 截面分析
    """
    extents = mesh.extents  # [x, y, z]
    bounds = mesh.bounds     # [[min_x, min_y, min_z], [max_x, max_y, max_z]]
    center = mesh.centroid

    dims = {
        "X_width_mm":  _safe_round(extents[0], 2),
        "Y_depth_mm":  _safe_round(extents[1], 2),
        "Z_height_mm": _safe_round(extents[2], 2),
    }

    # 长宽比 —— 极端值表示细长件（宽度方向），容易折断
    # 排除板状结构（最小维度为厚度是正常的）
    ratios = {}
    arr = np.sort(extents)
    if arr[0] > 1e-6:
        ratios["aspect_max_min"] = _safe_round(arr[2] / arr[0], 2)
        # 仅当中间维度/最小维度也大时，才警告（即不是平板，而是细长杆）
        ratios["aspect_mid_min"] = _safe_round(arr[1] / arr[0], 2)
        # 真正的细长杆：max/min > 8 且 mid/min > 4（排除平板件）
        ratios["aspect_warning"] = (arr[2] / arr[0] > 8) and (arr[1] / arr[0] > 4)

    # 关键点
    key_points = {
        "centroid":        _vec3_to_list(center),
        "geometric_center": _vec3_to_list(bounds.mean(axis=0)),
        "bounds_min":      _vec3_to_list(bounds[0]),
        "bounds_max":      _vec3_to_list(bounds[1]),
    }

    # 壁厚射线采样
    thickness_samples = _sample_wall_thickness(mesh, extents, bounds)

    # 截面分析
    sections = _analyze_cross_sections(mesh, bounds, num_sections=7)

    return {
        "dimensions": dims,
        "aspect_ratios": ratios,
        "key_points_mm": key_points,
        "wall_thickness_samples_mm": thickness_samples,
        "cross_sections": sections,
    }


def _sample_wall_thickness(mesh, extents, bounds):
    """从包围盒各面中心向内发射射线，测量壁厚。"""
    samples = []
    # 6个面的中心点（从包围盒外侧稍偏内一点）
    cx, cy, cz = bounds.mean(axis=0)
    hx, hy, hz = extents / 2.0

    face_centers = {
        "+X": np.array([bounds[1, 0] + 0.01, cy, cz]),
        "-X": np.array([bounds[0, 0] - 0.01, cy, cz]),
        "+Y": np.array([cx, bounds[1, 1] + 0.01, cz]),
        "-Y": np.array([cx, bounds[0, 1] - 0.01, cz]),
        "+Z": np.array([cx, cy, bounds[1, 2] + 0.01]),
        "-Z": np.array([cx, cy, bounds[0, 2] - 0.01]),
    }
    directions = {
        "+X": np.array([-1, 0, 0]),
        "-X": np.array([1, 0, 0]),
        "+Y": np.array([0, -1, 0]),
        "-Y": np.array([0, 1, 0]),
        "+Z": np.array([0, 0, -1]),
        "-Z": np.array([0, 0, 1]),
    }

    # 使用纯 numpy 的射线相交器（无需 embreex/pyembree）
    try:
        from trimesh.ray.ray_pyembree import RayMeshIntersector as FastIntersector
        intersector = FastIntersector(mesh)
    except Exception:
        from trimesh.ray.ray_triangle import RayMeshIntersector
        intersector = RayMeshIntersector(mesh)

    for label, origin in face_centers.items():
        direction = directions[label]
        try:
            locations, index_ray, index_tri = intersector.intersects_location(
                ray_origins=[origin],
                ray_directions=[direction],
                multiple_hits=True,
            )
            if len(locations) >= 2:
                # 第一个交点 = 进入，第二个 = 穿出
                dist = np.linalg.norm(locations[0] - locations[1])
                samples.append({
                    "face": label,
                    "entry_point": _vec3_to_list(locations[0]),
                    "exit_point":  _vec3_to_list(locations[1]),
                    "thickness_mm": _safe_round(dist, 2),
                })
            elif len(locations) == 1:
                samples.append({
                    "face": label,
                    "entry_point": _vec3_to_list(locations[0]),
                    "exit_point": None,
                    "thickness_mm": None,
                    "note": "仅一个交点，可能为盲孔或实体区域",
                })
            else:
                samples.append({
                    "face": label,
                    "thickness_mm": None,
                    "note": "无交点，射线未命中网格",
                })
        except Exception:
            samples.append({"face": label, "thickness_mm": None, "note": "射线计算异常"})

    return samples


def _analyze_cross_sections(mesh, bounds, num_sections=7):
    """沿 Z 轴等距切 N 个截面，测量每个截面的最小宽度与面积。"""
    z_min, z_max = bounds[0, 2], bounds[1, 2]
    z_vals = np.linspace(z_min, z_max, num_sections + 2)[1:-1]  # 去掉首尾

    sections = []
    for z in z_vals:
        try:
            plane_origin = [0, 0, z]
            plane_normal = [0, 0, 1]
            slice_result = mesh.section(plane_origin=plane_origin, plane_normal=plane_normal)
            if slice_result is None:
                sections.append({"z_mm": _safe_round(z, 2), "cross_section_area_mm2": 0, "min_width_mm": 0})
                continue

            # 处理 trimesh 4.x 的 Path3D → Path2D 转换
            path_parts = []
            if hasattr(slice_result, 'to_2D'):
                result_2d = slice_result.to_2D()
                if isinstance(result_2d, tuple):
                    path_parts = list(result_2d)
                else:
                    path_parts = [result_2d]
            elif hasattr(slice_result, 'to_planar'):
                try:
                    planar = slice_result.to_planar()
                    if isinstance(planar, (list, tuple)):
                        path_parts = list(planar)
                    else:
                        path_parts = [planar]
                except Exception:
                    path_parts = [slice_result]

            # 汇总所有部分的面积
            total_area = 0
            min_width = float('inf')
            for part in path_parts:
                try:
                    if hasattr(part, 'area'):
                        total_area += float(part.area)
                    if hasattr(part, 'polygons_full') and part.polygons_full:
                        total_area += sum(p.area for p in part.polygons_full if p)
                    if hasattr(part, 'extents'):
                        part_width = float(np.min(part.extents[:2]))
                        min_width = min(min_width, part_width)
                except Exception:
                    pass

            if min_width == float('inf'):
                min_width = _estimate_min_width(slice_result)

            sections.append({
                "z_mm": _safe_round(z, 2),
                "cross_section_area_mm2": _safe_round(total_area, 2),
                "min_width_mm": _safe_round(min_width, 2),
            })
        except Exception:
            sections.append({"z_mm": _safe_round(z, 2), "cross_section_area_mm2": None, "min_width_mm": None, "note": "截面计算失败"})

    return sections


def _estimate_min_width(section):
    """估算2D截面的最小宽度（沿主轴的跨度）。"""
    try:
        if hasattr(section, 'vertices') and len(section.vertices) >= 3:
            pts = section.vertices[:, :2]  # 取 XY
            # 最小包围矩形宽度
            cov = np.cov(pts.T)
            if cov.shape == (2, 2):
                eigenvalues, _ = np.linalg.eigh(cov)
                if len(eigenvalues) >= 2 and eigenvalues[0] > 1e-12:
                    return 2 * np.sqrt(eigenvalues[0])  # 短轴长度近似
        if hasattr(section, 'extents'):
            return float(np.min(section.extents[:2]))
    except Exception:
        pass
    return 0


# ============================================================================
#  模块二：孔洞分析
# ============================================================================

def analyze_holes(mesh):
    """
    孔洞检测与分析：
    - 找到网格边界环
    - 判断每个环是否为圆形孔洞
    - 计算孔的位置、直径、间距、边距
    """
    holes, detection_warnings = _detect_circular_holes(mesh)
    bounds = mesh.bounds  # [[xmin,ymin,zmin], [xmax,ymax,zmax]]

    hole_data = []
    for h in holes:
        center = h["center"]
        hole_data.append({
            "center_mm": _vec3_to_list(center),
            "diameter_mm": _safe_round(h["diameter"], 2),
            "type": "通孔 (through-hole)" if h.get("is_through") else "盲孔 (blind hole)",
            "normal_axis": _vec3_to_list(h.get("normal", None)),
            "circularity": _safe_round(h.get("circularity", 1.0), 3),
            # ← 新增：孔心到包围盒各面的距离（便于AI用 translate 修正坐标）
            "distance_to_boundary_mm": {
                "to_X_minus": _safe_round(center[0] - bounds[0, 0], 2),
                "to_X_plus":  _safe_round(bounds[1, 0] - center[0], 2),
                "to_Y_minus": _safe_round(center[1] - bounds[0, 1], 2),
                "to_Y_plus":  _safe_round(bounds[1, 1] - center[1], 2),
                "to_Z_minus": _safe_round(center[2] - bounds[0, 2], 2),
                "to_Z_plus":  _safe_round(bounds[1, 2] - center[2], 2),
            },
        })

    # 孔间距矩阵
    hole_distances = []
    for i in range(len(hole_data)):
        for j in range(i + 1, len(hole_data)):
            c1 = np.array(holes[i]["center"])
            c2 = np.array(holes[j]["center"])
            d = np.linalg.norm(c1 - c2)
            hole_distances.append({
                "hole_pair": (i + 1, j + 1),
                "center_distance_mm": _safe_round(d, 2),
            })

    # 孔到边缘的最短距离
    edge_margins = []
    if hasattr(mesh, 'outline'):
        try:
            outline_pts = mesh.outline().vertices
            for i, h in enumerate(holes):
                center = np.array(h["center"])
                dists = np.linalg.norm(outline_pts[:, :2] - center[:2], axis=1)
                min_dist = np.min(dists) if len(dists) > 0 else None
                edge_margins.append({
                    "hole": i + 1,
                    "min_edge_distance_mm": _safe_round(min_dist, 2),
                    "diameter_ratio": _safe_round(min_dist / h["diameter"], 2) if min_dist and h["diameter"] > 0 else None,
                })
        except Exception:
            pass

    # 边距预警（根据孔径大小区分阈值）
    margin_warnings = []
    for i, m in enumerate(edge_margins):
        if not m.get("diameter_ratio"):
            continue

        # 获取孔径信息
        hole_diameter = hole_data[i]["diameter_mm"] if i < len(hole_data) else 0

        # 根据孔径选择阈值
        if hole_diameter >= 15.0:
            # 超大孔（≥15mm，如电机轴孔）：边距要求最宽松
            threshold = 0.15
            rule_desc = "0.15×孔径（超大孔）"
        elif hole_diameter >= 10.0:
            # 大孔（10-15mm）：边距要求放宽
            threshold = 0.25
            rule_desc = "0.25×孔径（大孔）"
        else:
            # 小孔（<10mm，如安装孔）：标准边距要求
            threshold = 1.0
            rule_desc = "1×孔径（小孔）"

        if m["diameter_ratio"] < threshold:
            margin_warnings.append(
                f"孔 #{m['hole']} 边距仅 {m['min_edge_distance_mm']}mm（{m['diameter_ratio']}×孔径），"
                f"小于安全值 {rule_desc}，存在破裂风险。"
            )

    return {
        "holes": hole_data,
        "hole_count": len(hole_data),
        "hole_distances_mm": hole_distances,
        "edge_margins": edge_margins,
        "margin_warnings": margin_warnings,
        "detection_warnings": detection_warnings,
    }


def _detect_circular_holes(mesh):
    """
    混合孔洞检测：
    1. 如果不是水密的，用边界环检测（孔 = 网格边界）
    2. 如果是水密的（CSG建模），用多截面扫描检测内部通孔

    返回: (holes, detection_warnings)
      - holes: 检测到的孔洞列表
      - detection_warnings: 检测过程中的警告信息列表
    """
    holes = []
    detection_warnings = []

    # 方法一：边界环检测（适用于非水密模型/开放边界的孔）
    try:
        from collections import Counter
        edge_counts = Counter(mesh.edges_unique_inverse)
        boundary_unique_indices = np.array(
            [idx for idx, cnt in edge_counts.items() if cnt == 1], dtype=np.int64
        )

        if len(boundary_unique_indices) > 0:
            boundary_edges = mesh.edges_unique[boundary_unique_indices]
            adjacency = defaultdict(set)
            for e in boundary_edges:
                adjacency[e[0]].add(e[1])
                adjacency[e[1]].add(e[0])

            visited = set()
            for start in list(adjacency.keys()):
                if start in visited or len(adjacency[start]) != 2:
                    continue
                loop_verts = [start]
                visited.add(start)
                prev, current = None, next(iter(adjacency[start] - {start}))
                while current not in visited:
                    visited.add(current)
                    loop_verts.append(current)
                    neighbors = list(adjacency[current] - {prev})
                    if len(neighbors) != 1:
                        break
                    prev, current = current, neighbors[0]

                if len(loop_verts) >= 8:
                    verts = mesh.vertices[loop_verts]
                    result = _fit_circle_to_boundary(verts)
                    if result and result.get("circularity", 0) > 0.7:
                        result["is_through"] = _check_through_hole(mesh, verts, result)
                        holes.append(result)
    except Exception as exc:
        detection_warnings.append(f"边界环检测异常: {exc}")

    # 方法二：截面扫描（适用于水密模型中的内部通孔）
    if len(holes) == 0:
        holes = _detect_holes_via_sections(mesh)
        if not holes:
            detection_warnings.append("两种孔洞检测方法均未发现孔洞（模型可能无孔或检测失败）")

    return holes, detection_warnings


def _fit_circle_to_boundary(vertices):
    """用最小二乘法拟合圆（3D→2D投影），返回圆心、直径、圆形度。"""
    try:
        # 用 PCA 投影到最佳拟合平面
        centroid = vertices.mean(axis=0)
        shifted = vertices - centroid
        _, _, vh = np.linalg.svd(shifted)
        normal = vh[2]  # 最小奇异值对应的法向量
        # 投影到前两个主成分
        proj_2d = shifted @ vh[:2].T

        # 最小二乘圆拟合
        x, y = proj_2d[:, 0], proj_2d[:, 1]
        A = np.column_stack([x, y, np.ones_like(x)])
        b = x**2 + y**2
        sol = np.linalg.lstsq(A, b, rcond=None)[0]
        circle_center_2d = np.array([sol[0] / 2, sol[1] / 2])
        radius = np.sqrt(sol[2] + (sol[0] / 2) ** 2 + (sol[1] / 2) ** 2)

        # 圆形度：所有点到拟合圆距离的std / radius（越小越圆）
        dists = np.abs(np.linalg.norm(proj_2d - circle_center_2d, axis=1) - radius)
        circularity = 1.0 - min(float(np.std(dists) / radius), 1.0)

        # 圆心 3D 坐标
        center_3d = centroid + circle_center_2d @ vh[:2]

        return {
            "center": center_3d,
            "diameter": float(radius) * 2,
            "normal": normal / np.linalg.norm(normal),
            "circularity": float(circularity),
            "vertex_count": len(vertices),
        }
    except Exception:
        return None


def _check_through_hole(mesh, boundary_verts, hole_info):
    """检查是否为通孔：在法线方向上找另一个相似圆形边界环。"""
    try:
        center = hole_info["center"]
        normal = hole_info["normal"]
        diameter = hole_info["diameter"]

        # 沿法线正方向移动，检查是否有另一个类似的孔
        step = max(diameter * 3, 5.0)
        for offset in [step, -step]:
            test_plane_origin = center + normal * offset
            section = mesh.section(plane_origin=test_plane_origin.tolist(),
                                    plane_normal=normal.tolist())
            if section is not None:
                if hasattr(section, 'vertices') and len(section.vertices) > 10:
                    # 检查截面是否近似圆环
                    result = _fit_circle_to_boundary(section.vertices)
                    if result and abs(result["diameter"] - diameter) / diameter < 0.3:
                        return True
        return False
    except Exception:
        return False


def _detect_holes_via_sections(mesh):
    """
    射线网格扫描法检测通孔：
    沿 Z 轴方向发射网格射线，射线穿透偶数次 = 存在通孔。
    将穿透点聚类，识别孔的位置和直径。

    依赖 scipy.spatial.cKDTree 进行聚类。如果 scipy 未安装，
    孔洞检测将跳过并输出警告。
    """
    holes = []
    try:
        extents = mesh.extents
        bounds = mesh.bounds
        x_range = bounds[1, 0] - bounds[0, 0]
        y_range = bounds[1, 1] - bounds[0, 1]

        # 网格分辨率：小件用1mm细网格，大件自适应
        grid_spacing = 1.0 if max(x_range, y_range) < 200 else max(1.0, min(x_range, y_range) / 40)
        nx = max(5, int(x_range / grid_spacing))
        ny = max(5, int(y_range / grid_spacing))

        # 射线起止
        z_start = bounds[0, 2] - 1.0
        z_end = bounds[1, 2] + 1.0
        direction = np.array([0, 0, 1])

        from trimesh.ray.ray_triangle import RayMeshIntersector
        intersector = RayMeshIntersector(mesh)

        # 收集所有穿透点
        through_points = []
        x_vals = np.linspace(bounds[0, 0] + 0.5, bounds[1, 0] - 0.5, nx)
        y_vals = np.linspace(bounds[0, 1] + 0.5, bounds[1, 1] - 0.5, ny)

        for x in x_vals:
            for y in y_vals:
                origin = np.array([x, y, z_start])
                try:
                    locs, _, _ = intersector.intersects_location(
                        ray_origins=[origin],
                        ray_directions=[direction],
                        multiple_hits=True,
                    )
                    # 0次命中 = 射线完全穿透（穿过通孔）
                    if len(locs) == 0:
                        through_points.append([x, y, (bounds[0, 2] + bounds[1, 2]) / 2])
                except Exception:
                    continue

        if not through_points:
            return holes

        through_pts = np.array(through_points)

        # 简单聚类：基于 XY 距离的连通分量
        from scipy.spatial import cKDTree
        tree = cKDTree(through_pts[:, :2])
        # 聚类距离阈值
        cluster_dist = 3.0  # mm
        visited = np.zeros(len(through_pts), dtype=bool)
        clusters = []

        for i in range(len(through_pts)):
            if visited[i]:
                continue
            # BFS 聚类
            cluster = []
            queue = [i]
            visited[i] = True
            while queue:
                idx = queue.pop(0)
                cluster.append(idx)
                neighbors = tree.query_ball_point(through_pts[idx, :2], cluster_dist)
                for nb in neighbors:
                    if not visited[nb]:
                        visited[nb] = True
                        queue.append(nb)
            clusters.append(cluster)

        # 对每个簇，估算孔参数
        for cluster in clusters:
            if len(cluster) < 3:  # 至少3个采样点
                continue
            pts = through_pts[cluster]
            center_xy = pts[:, :2].mean(axis=0)
            center_z = pts[:, 2].mean()
            center = np.array([center_xy[0], center_xy[1], center_z])

            # 估算直径：聚类点的最大XY距离
            distances = np.linalg.norm(pts[:, :2] - center_xy, axis=1)
            diameter = 2 * np.mean(distances) + grid_spacing  # 加网格间距补偿

            # 过滤噪声（太小的孔或过大的内部空腔）
            if diameter < 1.5 or diameter > max(extents[:2]) * 0.7:
                continue

            holes.append({
                "center": center,
                "diameter": float(diameter),
                "normal": np.array([0, 0, 1]),
                "circularity": 0.85,
                "vertex_count": len(cluster),
                "is_through": True,
            })

    except ImportError as imp_err:
        print(
            f"[WARNING] scipy 未安装 ({imp_err})，截面扫描法孔洞检测已跳过。"
            f"水密 CSG 模型的孔洞检测依赖 scipy.spatial.cKDTree，"
            f"请运行 pip install scipy 以启用完整检测。",
            file=sys.stderr,
        )
    except Exception as exc:
        print(
            f"[WARNING] 截面扫描法孔洞检测异常: {exc}",
            file=sys.stderr,
        )
    return holes


# ============================================================================
#  模块三：结构特征分析
# ============================================================================

def analyze_structure(mesh, dim_data, hole_data=None):
    """
    结构特征分析：
    - 体积/表面积比 → 等效壁厚
    - 质心偏移
    - 惯性矩估算
    - 锐边检测
    - 加强筋判断
    - 悬臂/力矩预警
    """
    volume = float(mesh.volume)
    area = float(mesh.area)
    center_mass = mesh.center_mass
    geometric_center = mesh.bounds.mean(axis=0)

    # V/SA 比 —— 等效壁厚估算
    # 对于薄壁件，V/A ≈ t/2（t为壁厚）
    if area > 0:
        equiv_wall_thickness = 2 * volume / area
    else:
        equiv_wall_thickness = 0

    # 质心偏移
    cent_offset = np.linalg.norm(center_mass - geometric_center)
    extents = mesh.extents
    max_dim = max(extents)
    offset_ratio = cent_offset / max_dim if max_dim > 1e-6 else 0

    # 惯性矩（绕质心的主惯性矩）
    try:
        inertia_tensor = mesh.moment_inertia
        # 主惯性矩（特征值）
        principal_inertia = np.linalg.eigvalsh(inertia_tensor)
        moi = {
            "I1": _safe_round(principal_inertia[0], 0),
            "I2": _safe_round(principal_inertia[1], 0),
            "I3": _safe_round(principal_inertia[2], 0),
        }
        # 抗弯能力指数（I_min / extents_max 的比值）
        moi["bending_resistance_index"] = _safe_round(
            min(principal_inertia) / (max_dim ** 3) * 1e6, 2
        ) if max_dim > 1e-6 else 0
    except Exception:
        moi = {"error": "惯性矩计算失败"}

    # 锐边检测（二面角 < 30° 或 > 150° 表示应力集中）
    sharp_edges = _detect_sharp_edges(mesh)

    # 加强筋判断（基于截面变化）
    sections = dim_data.get("cross_sections", [])
    has_ribs = _detect_ribs(sections)

    # 悬臂/力矩分析
    cantilever = _analyze_cantilever(mesh, hole_data, equiv_wall_thickness)

    # 强度评估
    strength_assessment = _assess_strength(
        volume, area, equiv_wall_thickness, offset_ratio,
        sharp_edges, has_ribs, extents, cantilever
    )

    return {
        "volume_mm3": _safe_round(volume, 2),
        "surface_area_mm2": _safe_round(area, 2),
        "is_watertight": bool(mesh.is_watertight),
        "equivalent_wall_thickness_mm": _safe_round(equiv_wall_thickness, 2),
        "center_of_mass_mm": _vec3_to_list(center_mass),
        "cantilever_analysis": cantilever,  # ← 悬臂/力矩分析
        "geometric_center_mm": _vec3_to_list(geometric_center),
        "centroid_offset_mm": _safe_round(cent_offset, 2),
        "centroid_offset_ratio": _safe_round(offset_ratio, 3),
        "principal_moments_of_inertia": moi,
        "sharp_edge_count": sharp_edges["count"],
        "sharp_edge_warning": sharp_edges["warning"],
        "has_ribs": has_ribs["detected"],
        "rib_details": has_ribs["details"],
        "strength_assessment": strength_assessment,
    }


def _detect_sharp_edges(mesh):
    """检测锐边（二面角远离 0°/180° 的边——真正的应力集中源）。"""
    try:
        angles = mesh.face_adjacency_angles
        # 二面角 = 0° → 共面同向；180° → 共面反向
        # 二面角在 30°~150° 之间 → 真正转角（可能为锐利边）
        angle_deg = np.degrees(angles)
        # 锐利边：转角 > 30° 且 < 150°（排除共面）
        sharp_mask = (angle_deg > 30) & (angle_deg < 150)
        sharp_count = int(np.sum(sharp_mask))
        total_edges = len(angles)

        # 进一步筛选"应力集中型"锐边：转角 < 60° 或 > 120°（大转角）
        stress_riser_mask = (angle_deg > 30) & (angle_deg < 60) | \
                             (angle_deg > 120) & (angle_deg < 150)
        stress_riser_count = int(np.sum(stress_riser_mask))

        warning = stress_riser_count > total_edges * 0.05 if total_edges > 0 else False

        return {
            "count": sharp_count,
            "stress_riser_count": stress_riser_count,
            "total_edges": total_edges,
            "ratio": _safe_round(sharp_count / total_edges, 3) if total_edges > 0 else 0,
            "warning": warning,
            "message": (
                f"检测到 {stress_riser_count} 条应力集中锐边（{stress_riser_count/total_edges*100:.1f}%），"
                f"这些区域可能在受载时优先失效。"
            ) if warning else "锐边比例正常。",
        }
    except Exception:
        return {"count": 0, "stress_riser_count": 0, "warning": False, "message": "锐边检测不可用。"}


def _detect_ribs(sections):
    """通过截面面积变化检测加强筋/肋板。"""
    areas = [s.get("cross_section_area_mm2", 0) for s in sections if s.get("cross_section_area_mm2")]
    if len(areas) < 3:
        return {"detected": False, "details": "截面数量不足，无法判断。"}

    areas = np.array(areas)
    mean_area = np.mean(areas)
    if mean_area < 1e-6:
        return {"detected": False, "details": ""}

    # 截面面积变异系数
    cv = np.std(areas) / mean_area
    has_ribs = cv > 0.15

    return {
        "detected": has_ribs,
        "section_area_variation_cv": _safe_round(cv, 3),
        "details": (
            f"截面面积变异系数 CV={cv:.2f}，{'存在' if has_ribs else '无明显'}加强筋/肋板特征。"
            if has_ribs else f"截面面积变化小（CV={cv:.3f}），无明显加强筋特征。"
        ),
    }


def _analyze_cantilever(mesh, hole_data, equiv_thickness):
    """
    悬臂/力矩分析——利用安装孔位置和质心计算最大悬臂距离。

    对于支架来说，安装孔是固定端，载荷通常施加在远离安装面的位置。
    悬臂距离过长而连接处过薄 = 杠杆效应 → 断裂风险。
    """
    try:
        if not hole_data or not hole_data.get("holes"):
            return {"max_cantilever_mm": None, "warning": None}

        holes = hole_data["holes"]
        center_mass = mesh.center_mass

        # 计算每个孔到质心的距离
        cantilever_dists = []
        furthest_hole_idx = 0
        max_dist = 0
        for i, h in enumerate(holes):
            h_center = np.array(h.get("center_mm", [0, 0, 0]))
            d = np.linalg.norm(h_center[:2] - center_mass[:2])  # XY平面距离
            cantilever_dists.append(d)
            if d > max_dist:
                max_dist = d
                furthest_hole_idx = i

        max_cantilever = max_dist

        # 悬臂比 = 最远安装孔距 / 等效壁厚
        # > 15: 严重杠杆效应
        # > 8: 需要关注
        if equiv_thickness > 1e-6:
            leverage_ratio = max_cantilever / equiv_thickness
        else:
            leverage_ratio = 999

        warning = None
        if leverage_ratio > 15:
            warning = (
                f"🔴 **悬臂过长警告**：最远安装孔距质心 {max_cantilever:.1f}mm，"
                f"悬臂比（距离/壁厚）={leverage_ratio:.0f}:1。"
                f"杠杆效应严重，安装面连接处容易断裂。"
                f"建议：①增加连接处板厚（目标：将悬臂比降到 ≤10）；"
                f"②在远离安装孔的一侧添加加强筋；"
                f"③重新排布安装孔位置使其包围质心。"
            )
        elif leverage_ratio > 8:
            warning = (
                f"🟡 悬臂比 {leverage_ratio:.0f}:1（距离 {max_cantilever:.1f}mm / 壁厚 {equiv_thickness:.1f}mm），"
                f"存在杠杆效应，高载荷下建议加强连接处。"
            )

        return {
            "max_cantilever_distance_mm": _safe_round(max_cantilever, 1),
            "furthest_hole_index": furthest_hole_idx + 1,
            "leverage_ratio": _safe_round(leverage_ratio, 1),
            "warning": warning,
            "is_severe": leverage_ratio > 15,
        }
    except Exception:
        return {"max_cantilever_mm": None, "warning": None}


def _assess_strength(volume, area, equiv_thickness, offset_ratio, sharp_edges, has_ribs, extents, cantilever=None):
    """综合强度评估。"""
    issues = []
    score = 100

    # 壁厚评估
    if equiv_thickness < 0.5:
        score -= 30
        issues.append("等效壁厚 < 0.5mm，整体过薄，不适合承力场景。")
    elif equiv_thickness < 1.0:
        score -= 15
        issues.append("等效壁厚 < 1.0mm，仅适合轻载荷场景。")
    elif equiv_thickness < 2.0:
        score -= 5
        issues.append("等效壁厚 1-2mm，中等载荷可用；若为主承力件，建议加厚至 ≥2mm。")

    # 质心偏移
    if offset_ratio > 0.2:
        score -= 10
        issues.append(f"质心偏移几何中心 {offset_ratio*100:.1f}%，可能导致安装应力不均。")

    # 锐边
    if sharp_edges.get("warning"):
        score -= 15
        issues.append(sharp_edges.get("message", "存在大量锐边。"))

    # 长宽比
    arr = np.sort(extents)
    if arr[2] / max(arr[0], 1e-6) > 8:
        score -= 10
        issues.append("长宽比 > 8，细长结构抗弯能力弱，建议增加横向支撑。")

    # 悬臂杠杆
    if cantilever and cantilever.get("is_severe"):
        score -= 20
        issues.append(cantilever.get("warning", ""))
    elif cantilever and cantilever.get("warning"):
        score -= 8
        issues.append(cantilever.get("warning", ""))

    # 加强筋加分
    if has_ribs.get("detected"):
        score += 10

    score = max(0, min(100, score))

    if score >= 80:
        grade = "优秀 (Excellent)"
    elif score >= 60:
        grade = "良好 (Good)"
    elif score >= 40:
        grade = "一般 (Fair)"
    else:
        grade = "差 (Poor)"

    return {
        "strength_score": score,
        "grade": grade,
        "issues": issues,
    }


# ============================================================================
#  模块四：对称性与拓扑分析
# ============================================================================

def analyze_symmetry(mesh, dim_data):
    """
    对称性与拓扑分析：
    - 对称性检测（XZ, YZ, XY 平面）
    - 结构类型推断
    - 特征计数
    """
    bounds = mesh.bounds
    extents = mesh.extents
    center = bounds.mean(axis=0)

    # 沿三个主平面镜像检测
    symmetry = {}
    for axis, plane_name in enumerate(["YZ", "XZ", "XY"]):
        try:
            score = _check_mirror_symmetry(mesh, center, axis)
            symmetry[plane_name] = {
                "symmetry_score": _safe_round(score, 3),
                "is_symmetric": score > 0.85,
            }
        except Exception:
            symmetry[plane_name] = {"symmetry_score": None, "is_symmetric": False}

    # 结构类型推断
    bracket_type = _infer_bracket_type(extents, symmetry, dim_data)

    # 特征计数
    feature_count = {
        "mounting_flanges": 0,
        "reinforcement_ribs": 0,
    }

    # 安装法兰：一侧有较大平面 + 孔
    # 简化：如果检测到孔，估算安装耳数量
    z_sections = dim_data.get("cross_sections", [])
    if z_sections:
        areas = [s.get("cross_section_area_mm2", 0) for s in z_sections]
        if len(areas) >= 3:
            # 面积突然变大的截面 → 可能是法兰
            areas_arr = np.array(areas)
            mean_a = np.mean(areas_arr)
            if mean_a > 1e-6:
                flange_count = int(np.sum(areas_arr > mean_a * 1.3))
                feature_count["mounting_flanges"] = min(flange_count, 4)

    return {
        "symmetry": symmetry,
        "bracket_type": bracket_type,
        "feature_count": feature_count,
    }


def _check_mirror_symmetry(mesh, center, axis):
    """通过采样点云检测镜像对称性（简化方法）。"""
    try:
        # 均匀采样点
        n_samples = min(500, len(mesh.vertices))
        idx = np.random.choice(len(mesh.vertices), n_samples, replace=False)
        pts = mesh.vertices[idx].copy()
        pts_centered = pts - center

        # 镜像
        mirrored = pts_centered.copy()
        mirrored[:, axis] *= -1

        # 找最近点距离
        from sklearn.neighbors import NearestNeighbors
        nn = NearestNeighbors(n_neighbors=1).fit(pts_centered)
        dists, _ = nn.kneighbors(mirrored)
        mean_dist = np.mean(dists)

        extents = mesh.extents
        norm_dist = mean_dist / max(extents) if max(extents) > 1e-6 else mean_dist
        return max(0, 1 - min(norm_dist, 1))
    except ImportError:
        # 无 sklearn 时用简化方法
        return _check_symmetry_simple(mesh, center, axis)
    except Exception:
        return 0


def _check_symmetry_simple(mesh, center, axis):
    """无 sklearn 时的简化对称性检测。"""
    try:
        pts = mesh.vertices.copy()
        pts_centered = pts - center
        mirrored = pts_centered.copy()
        mirrored[:, axis] *= -1
        # 随机采样对比
        n = min(100, len(pts))
        idx = np.random.choice(len(pts), n, replace=False)
        dists = []
        for i in idx:
            d = np.min(np.linalg.norm(pts_centered - mirrored[i], axis=1))
            dists.append(d)
        mean_dist = np.mean(dists)
        extents = mesh.extents
        norm_dist = mean_dist / max(extents) if max(extents) > 1e-6 else mean_dist
        return max(0, 1 - min(norm_dist, 1))
    except Exception:
        return 0


def _infer_bracket_type(extents, symmetry, dim_data):
    """推断支架结构类型。"""
    x, y, z = extents
    # 特征判断
    thin_z = z < max(x, y) * 0.3  # Z方向薄 → 板状
    thin_x = x < max(y, z) * 0.3  # X方向薄
    thin_y = y < max(x, z) * 0.3  # Y方向薄

    yz_sym = symmetry.get("YZ", {}).get("is_symmetric", False)
    xz_sym = symmetry.get("XZ", {}).get("is_symmetric", False)
    xy_sym = symmetry.get("XY", {}).get("is_symmetric", False)

    types = []
    confidence = 0

    if thin_z and xy_sym:
        types.append("平板安装支架 (Flat mounting plate)")
        confidence += 0.3
    if thin_x and yz_sym:
        types.append("L型 / V型支架 (L/V-bracket)")
        confidence += 0.4
    if thin_y:
        types.append("U型支架 (U-bracket/channel)")
        confidence += 0.4
    if not thin_x and not thin_y and not thin_z:
        types.append("块状结构 (Block/monolithic)")
        confidence += 0.2
    if symmetry.get("YZ", {}).get("is_symmetric") and symmetry.get("XZ", {}).get("is_symmetric"):
        types.append("对称双耳支架 (Symmetric dual-flange bracket)")
        confidence += 0.3

    # 选择最可能的类型
    if not types:
        types.append("自定义/不规则支架 (Custom/irregular bracket)")
        confidence = 0.5

    return {
        "inferred_types": list(set(types)),
        "confidence": round(min(confidence, 1.0), 2),
    }


# ============================================================================
#  模块五：可制造性评估
# ============================================================================

def _evaluate_print_orientation(mesh):
    """
    简化版打印方向评估——比较 6 个轴对齐方向，找出最佳打印底面。

    检查每个包围盒面朝下时的：底面接触面积、悬垂面比例。
    FDM打印中，Z轴层间结合最弱；如果支架的承力方向与Z轴平行，有断裂风险。

    返回: { best_orientation, orientations, z_weakness_warning }
    """
    try:
        extents = mesh.extents
        bounds = mesh.bounds
        normals_all = mesh.face_normals

        # 6个候选方向：±X, ±Y, ±Z 作为"朝下"方向
        candidates = [
            {"name": "+Z朝下（默认姿态）", "up": np.array([0, 0, 1]), "base_area": extents[0] * extents[1]},
            {"name": "-Z朝下（倒置）",       "up": np.array([0, 0, -1]), "base_area": extents[0] * extents[1]},
            {"name": "+Y朝下（侧放）",       "up": np.array([0, 1, 0]), "base_area": extents[0] * extents[2]},
            {"name": "-Y朝下（侧放）",       "up": np.array([0, -1, 0]), "base_area": extents[0] * extents[2]},
            {"name": "+X朝下（侧放）",       "up": np.array([1, 0, 0]), "base_area": extents[1] * extents[2]},
            {"name": "-X朝下（侧放）",       "up": np.array([-1, 0, 0]), "base_area": extents[1] * extents[2]},
        ]

        for cand in candidates:
            up = cand["up"]
            # 计算每个面的法线与"上"方向的夹角
            dot = np.dot(normals_all, up)
            # 面朝下的面（dot < 0 表示法线朝下）且倾斜过头（|dot| < cos45°）→ 悬垂
            overhang_mask = (dot < 0) & (np.abs(dot) < np.cos(np.radians(45)))
            cand["overhang_count"] = int(np.sum(overhang_mask))
            cand["overhang_ratio"] = _safe_round(
                cand["overhang_count"] / len(normals_all), 3
            ) if len(normals_all) > 0 else 0
            # 综合评分：底面越大越好，悬垂越少越好
            base_score = cand["base_area"] / (extents[0] * extents[1] + extents[1] * extents[2] + extents[0] * extents[2])
            overhang_penalty = cand["overhang_ratio"]
            cand["score"] = _safe_round(base_score * (1 - overhang_penalty), 3)

        # 找最佳方向
        best = max(candidates, key=lambda c: c["score"])

        # Z轴薄弱警告：默认姿态下，检查支架是否有Z方向的薄截面
        # 如果Z方向是最小尺寸（薄板水平打印），FDM层间强度弱
        z_is_shortest = extents[2] <= min(extents[0], extents[1])
        better_orientation_exists = best["name"] != "+Z朝下（默认姿态）" and best["score"] > candidates[0]["score"] * 1.2

        warning = None
        if z_is_shortest:
            warning = (
                f"⚠️ Z轴（打印层叠方向）是零件的最短尺寸（{extents[2]:.1f}mm），"
                f"FDM打印中Z向层间结合力最弱。如果支架承受Z向拉力/弯矩，"
                f"建议在 build123d 中调整构建方向，使承力面平行于 XY 平面。"
            )
        if better_orientation_exists:
            alt = best["name"]
            warning = (warning or "") + (
                f"\n💡 检测到更好的打印姿态：**{alt}**（底面={best['base_area']:.0f}mm²，"
                f"悬垂={best['overhang_ratio']*100:.0f}%），"
                f"相比默认姿态可减少支撑并提高强度。"
            )

        return {
            "best_orientation": best["name"],
            "best_base_area_mm2": _safe_round(best["base_area"], 0),
            "best_overhang_ratio": best["overhang_ratio"],
            "orientations_evaluated": [
                {"name": c["name"], "base_area": c["base_area"], "overhang_ratio": c["overhang_ratio"], "score": c["score"]}
                for c in candidates
            ],
            "z_layer_weakness_warning": warning,
        }
    except Exception:
        return {
            "best_orientation": "未知",
            "orientations_evaluated": [],
            "z_layer_weakness_warning": None,
        }


def assess_manufacturability(mesh, struct_data, dim_data, skip_print_orientation=False):
    """
    可制造性评估：
    - 3D打印（FDM）
    - CNC加工
    - 材料适配
    - 打印方向评估（各向异性警告）—— 可通过 skip_print_orientation 跳过
    """
    # 打印方向评估（迭代过程中跳过，最后自动旋转）
    if skip_print_orientation:
        print_orientation = {
            "best_orientation": "跳过（迭代中）",
            "orientations_evaluated": [],
            "z_layer_weakness_warning": None,
        }
    else:
        print_orientation = _evaluate_print_orientation(mesh)
    # CNC底切检测
    undercut_info = _detect_undercuts(mesh)

    is_watertight = struct_data.get("is_watertight", False)
    wall_thickness = struct_data.get("equivalent_wall_thickness_mm", 0)
    sections = dim_data.get("cross_sections", [])

    # 悬垂角评估（简化：检查是否有 >45° 的悬垂）
    overhang_risk = _check_overhangs(mesh)

    # 最小特征尺寸
    min_feature_size = _estimate_min_feature(mesh)

    # 3D打印评估
    print_issues = []
    if not is_watertight:
        print_issues.append("模型非水密，需修复后方可3D打印。")
    if overhang_risk["severe_overhangs"]:
        print_issues.append(
            f"存在{overhang_risk['true_overhang_count']}处真实悬垂（需支撑）"
            f"和{overhang_risk['bridge_count']}处桥接（无需支撑）。"
        )
    # Disabled: this check triggers on mesh tessellation edges from fillets
    # and curved surfaces, not actual printable features.  It confuses the
    # repair agent into trying to "fix" non-issues.
    # if min_feature_size and min_feature_size < 0.4:
    #     print_issues.append(f"最小特征尺寸 {min_feature_size:.2f}mm，低于FDM喷嘴分辨率（0.4mm）。")

    print_feasible = len(print_issues) == 0

    # CNC加工评估
    cnc_issues = []
    if min_feature_size and min_feature_size < 1.0:
        cnc_issues.append(f"最小特征尺寸 {min_feature_size:.2f}mm，小于常规铣刀直径（1mm）。")

    # 检查是否有内直角（CNC无法加工）
    sharp_corners = struct_data.get("sharp_edge_count", 0)
    if sharp_corners > 50:
        cnc_issues.append("大量内锐角，CNC需多工序或放电加工。")

    # 底切检测
    if undercut_info.get("severe"):
        cnc_issues.append(undercut_info["message"])
    elif undercut_info.get("has_undercuts"):
        cnc_issues.append(undercut_info["message"])

    cnc_feasible = len(cnc_issues) == 0

    # 材料适配
    material_compatibility = []
    materials = [
        {"name": "PLA (3D打印)", "min_wall": 1.2, "notes": "最常用3D打印材料，强度中等"},
        {"name": "ABS (3D打印)", "min_wall": 1.5, "notes": "韧性好于PLA，耐热但易翘曲"},
        {"name": "PETG (3D打印)", "min_wall": 1.2, "notes": "兼有PLA易打印和ABS强度"},
        {"name": "铝合金6061 (CNC)", "min_wall": 0.8, "notes": "轻质高强，适合结构件"},
        {"name": "钢 Q235 (CNC)", "min_wall": 1.5, "notes": "高强度，重载支架首选"},
        {"name": "尼龙 PA12 (SLS)", "min_wall": 1.0, "notes": "韧性极佳，无支撑打印"},
    ]
    for mat in materials:
        compatible = wall_thickness >= mat["min_wall"]
        material_compatibility.append({
            "material": mat["name"],
            "compatible": compatible,
            "required_min_wall_mm": mat["min_wall"],
            "actual_wall_mm": wall_thickness,
            "notes": mat["notes"],
        })

    return {
        "3d_print": {
            "feasible": print_feasible,
            "issues": print_issues,
        },
        "cnc_machining": {
            "feasible": cnc_feasible,
            "issues": cnc_issues,
        },
        "overhang_analysis": overhang_risk,
        "print_orientation": print_orientation,  # ← 打印方向与层间强度评估
        "cnc_undercut": undercut_info,            # ← CNC底切/刀具干涉检测
        "min_feature_size_mm": _safe_round(min_feature_size, 2) if min_feature_size else None,
        "material_compatibility": material_compatibility,
    }


def _check_overhangs(mesh):
    """
    增强版悬垂检测——区分"桥接"与"真实悬垂"。

    FDM打印中：
    - 真实悬垂 (true overhang)：两端无支撑的朝下面 → 需要支撑
    - 桥接 (bridge)：两端连接竖直墙壁的水平面（如U型槽顶部）→ 通常无需支撑

    检测方法：检查每个悬垂面是否通过边连接到竖直面（法线水平）。
    若至少2条边连接到竖直面 → 桥接；否则 → 真实悬垂。
    """
    try:
        normals = mesh.face_normals
        z_axis = np.array([0, 0, 1])
        z_dot = np.dot(normals, z_axis)

        # 朝下面：法线Z分量 < 0 且不够垂直（倾角 > 45°）
        overhang_mask = (z_dot < 0) & (np.abs(z_dot) < np.cos(np.radians(45)))
        overhang_face_indices = np.where(overhang_mask)[0]
        total = len(normals)

        # 竖直面：法线接近水平（Z分量 ≈ 0）
        vertical_mask = np.abs(z_dot) < np.sin(np.radians(20))

        # 区分桥接与真实悬垂
        face_adj = mesh.face_adjacency  # [N, 2] 相邻面对
        bridge_count = 0
        true_overhang_count = 0

        # 构建每个面的邻居集合
        neighbor_map = defaultdict(set)
        for a, b in face_adj:
            neighbor_map[a].add(b)
            neighbor_map[b].add(a)

        for fi in overhang_face_indices:
            neighbors = neighbor_map.get(fi, set())
            # 统计邻居中的竖直面数量
            vertical_neighbors = sum(1 for n in neighbors if vertical_mask[n] if n < len(vertical_mask))
            if vertical_neighbors >= 2:
                bridge_count += 1
            else:
                true_overhang_count += 1

        return {
            "severe_overhangs": true_overhang_count > total * 0.05,
            "true_overhang_count": true_overhang_count,
            "bridge_count": bridge_count,
            "total_overhang_faces": true_overhang_count + bridge_count,
            "true_overhang_ratio": _safe_round(true_overhang_count / total, 2) if total > 0 else 0,
            "message": (
                f"检测到 {true_overhang_count} 处真实悬垂（需支撑）和 {bridge_count} 处桥接（通常无需支撑）。"
                if true_overhang_count + bridge_count > 0 else "无明显悬垂。"
            ),
        }
    except Exception:
        return {"severe_overhangs": False, "true_overhang_count": 0, "bridge_count": 0, "total_overhang_faces": 0, "message": "悬垂分析异常。"}


def _detect_undercuts(mesh):
    """
    CNC三轴底切检测——从+Z和-Z方向投射射线，检查是否有表面被上方结构遮挡。

    三轴CNC刀具只能从Z轴接近工件。如果模型某表面在Z方向上被上方几何体遮挡，
    即存在"底切"(undercut)，需要翻面装夹或改用五轴CNC。

    返回: { has_undercuts, count, message }
    """
    try:
        bounds = mesh.bounds
        # 从+Z方向（上方）投射射线
        x_range = bounds[1, 0] - bounds[0, 0]
        y_range = bounds[1, 1] - bounds[0, 1]
        grid = max(1.0, min(x_range, y_range) / 20)

        nx = max(3, int(x_range / grid))
        ny = max(3, int(y_range / grid))
        x_vals = np.linspace(bounds[0, 0] + grid/2, bounds[1, 0] - grid/2, nx)
        y_vals = np.linspace(bounds[0, 1] + grid/2, bounds[1, 1] - grid/2, ny)

        from trimesh.ray.ray_triangle import RayMeshIntersector
        intersector = RayMeshIntersector(mesh)

        undercut_score = 0
        total_rays = 0

        for x in x_vals:
            for y in y_vals:
                total_rays += 1
                # 从上方照射
                origin_top = np.array([x, y, bounds[1, 2] + 1.0])
                dir_down = np.array([0, 0, -1])
                locs_top, _, _ = intersector.intersects_location(
                    ray_origins=[origin_top], ray_directions=[dir_down], multiple_hits=True
                )
                # 如果命中次数 ≥ 3（即穿过 ≥2 层材料）→ 有遮挡/底切
                if len(locs_top) >= 3:
                    undercut_score += 1

        undercut_ratio = undercut_score / total_rays if total_rays > 0 else 0

        if undercut_ratio > 0.2:
            message = (
                f"检测到严重的底切问题（{undercut_ratio*100:.0f}%区域Z向不可见），"
                f"三轴CNC无法一次装夹完成加工。需要多次翻面或改用五轴CNC。"
            )
        elif undercut_ratio > 0.05:
            message = (
                f"检测到底切区域（{undercut_ratio*100:.0f}%），部分表面需翻面加工。"
            )
        else:
            message = "Z向可见性良好，三轴CNC可一次装夹加工。"

        return {
            "has_undercuts": undercut_ratio > 0.05,
            "undercut_ratio": _safe_round(undercut_ratio, 3),
            "severe": undercut_ratio > 0.2,
            "message": message,
        }
    except Exception:
        return {"has_undercuts": False, "undercut_ratio": 0, "severe": False, "message": "底切检测异常。"}


def _estimate_min_feature(mesh):
    """估算最小特征尺寸（最近顶点间距的中位数）。"""
    try:
        edges = mesh.edges_unique
        edge_lengths = np.linalg.norm(
            mesh.vertices[edges[:, 0]] - mesh.vertices[edges[:, 1]], axis=1
        )
        if len(edge_lengths) > 0:
            return float(np.percentile(edge_lengths, 5))  # 5分位数
    except Exception:
        pass
    return None


# ============================================================================
#  模块六：规格对比
# ============================================================================

def compare_to_spec(analysis, spec):
    """
    规格对比：将分析结果与规格文件进行逐项对比。
    自动区分"网格噪声"和"真实设计偏差"——避免因多边形逼近误差而无效迭代。
    """
    comparisons = []
    mesh_res = analysis.get("mesh_resolution", {})
    noise_threshold = mesh_res.get("max_theoretical_error_mm", 0.15)

    dims = analysis.get("dimensions", {}).get("dimensions", {})
    target_dims = spec.get("target_dimensions", {})
    tolerances = spec.get("tolerances", {})
    for key, label in [("X_width_mm", "X"), ("Y_depth_mm", "Y"), ("Z_height_mm", "Z")]:
        target = target_dims.get(label)
        tol = tolerances.get(label, 0)
        actual = dims.get(key)
        if target is not None and actual is not None:
            delta = actual - target
            passed = abs(delta) <= tol
            noise_info = _deviation_is_noise(actual, target, mesh_res, tol)
            comparisons.append({
                "parameter": f"外形尺寸 {label}",
                "target_mm": target,
                "actual_mm": actual,
                "delta_mm": _safe_round(delta, 2),
                "tolerance_mm": tol,
                "pass": passed,
                "is_mesh_noise": noise_info["is_noise"],
                "noise_note": noise_info["explanation"] if noise_info["is_noise"] else "",
            })

    # 孔直径对比
    target_holes = spec.get("target_hole_diameters", [])
    hole_tol = spec.get("hole_tolerance", 0.1)
    actual_holes = analysis.get("holes", {}).get("holes", [])
    for i, (t_dia, a_hole) in enumerate(zip(target_holes, actual_holes)):
        a_dia = a_hole.get("diameter_mm")
        if a_dia:
            delta = a_dia - t_dia
            passed = abs(delta) <= hole_tol
            noise_info = _deviation_is_noise(a_dia, t_dia, mesh_res, hole_tol)
            comparisons.append({
                "parameter": f"孔 #{i+1} 直径",
                "target_mm": t_dia,
                "actual_mm": a_dia,
                "delta_mm": _safe_round(delta, 2),
                "tolerance_mm": hole_tol,
                "pass": passed,
                "is_mesh_noise": noise_info["is_noise"],
                "noise_note": noise_info["explanation"] if noise_info["is_noise"] else "",
            })

    # 壁厚对比
    target_wall = spec.get("min_wall_thickness")
    if target_wall is not None:
        actual_wall = analysis.get("structure", {}).get("equivalent_wall_thickness_mm")
        if actual_wall is not None:
            passed = actual_wall >= target_wall
            comparisons.append({
                "parameter": "最小壁厚",
                "target_mm": f"≥ {target_wall}",
                "actual_mm": actual_wall,
                "delta_mm": _safe_round(actual_wall - target_wall, 2),
                "tolerance_mm": "--",
                "pass": passed,
                "is_mesh_noise": False,
                "noise_note": "",
            })

    # 体积对比
    target_vol = spec.get("target_volume_mm3")
    if target_vol is not None:
        actual_vol = analysis.get("structure", {}).get("volume_mm3")
        vol_tol = spec.get("volume_tolerance", target_vol * 0.05)
        if actual_vol is not None:
            delta = actual_vol - target_vol
            passed = abs(delta) <= vol_tol
            comparisons.append({
                "parameter": "体积",
                "target_mm": target_vol,
                "actual_mm": actual_vol,
                "delta_mm": _safe_round(delta, 2),
                "tolerance_mm": _safe_round(vol_tol, 0),
                "pass": passed,
                "is_mesh_noise": False,
                "noise_note": "",
            })

    passed_count = sum(1 for c in comparisons if c["pass"])
    noise_only_fails = sum(1 for c in comparisons if not c["pass"] and c.get("is_mesh_noise"))
    real_fails = sum(1 for c in comparisons if not c["pass"] and not c.get("is_mesh_noise"))
    total_count = len(comparisons)

    return {
        "comparisons": comparisons,
        "passed": passed_count,
        "total": total_count,
        "pass_rate": _safe_round(passed_count / total_count, 2) if total_count > 0 else 0,
        "all_passed": passed_count == total_count,
        "noise_only_fails": noise_only_fails,  # ← 这些FAIL不是真正的设计错误
        "real_fails": real_fails,              # ← 只有这些需要修改代码
        "mesh_noise_threshold_mm": noise_threshold,
        "convergence_advice": (
            f"共 {noise_only_fails} 项偏差属于网格噪声（≤{noise_threshold:.2f}mm），无需修改代码。"
            if noise_only_fails > 0 else
            "所有偏差均为真实设计偏差。"
        ),
    }


# ============================================================================
#  输出模块：自然语言报告生成
# ============================================================================

class _NumpyEncoder(json.JSONEncoder):
    """处理 numpy 类型到 JSON 原生类型的转换。"""
    def default(self, obj):
        if isinstance(obj, (np.bool_,)):
            return bool(obj)
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


def _to_json_safe(obj):
    """递归转换数据结构中的 numpy 类型为 Python 原生类型。"""
    if isinstance(obj, dict):
        return {k: _to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_json_safe(v) for v in obj]
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj


def generate_json_report(analysis):
    """生成完整 JSON 报告。"""
    safe = _to_json_safe(analysis)
    return json.dumps(safe, ensure_ascii=False, indent=2, cls=_NumpyEncoder)


def generate_markdown_report(analysis, spec_result=None, terse=False):
    """生成详细的中文 Markdown 报告，供 AI 阅读理解。

    委托给 ``scripts/report_markdown.py`` 中的实现。
    该模块在 v2.0 中被提取以保持 check_mesh.py 精简。
    """
    _ensure_report_module_available()
    from scripts.report_markdown import generate_markdown_report as _gen
    return _gen(analysis, spec_result, terse=terse)


def _ensure_report_module_available():
    """Ensure the report module is importable."""
    _repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _repo not in sys.path:
        sys.path.insert(0, _repo)


#  测试模型生成
# ============================================================================

def generate_test_bracket(bracket_type="l_bracket"):
    """生成用于测试的支架STL模型。

    委托给 ``tests/fixtures/generate_test_meshes.py`` 中的实现。
    该模块在 v2.0 中被提取以保持 check_mesh.py 精简。
    """
    _ensure_test_fixtures_available()
    from tests.fixtures.generate_test_meshes import generate_test_bracket as _gen
    return _gen(bracket_type)


def _ensure_test_fixtures_available():
    """Ensure the test fixtures module is importable."""
    _repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _repo not in sys.path:
        sys.path.insert(0, _repo)


# ============================================================================
#  主分析流程
# ============================================================================

# ---------------------------------------------------------------------------
# Schema version — bump on breaking changes to the JSON output format.
# Callers (multi_agent_cad) should check this against their expected version.
# ---------------------------------------------------------------------------
SCHEMA_VERSION = "2.0.0"


def analyze_stl(file_path, skip_print_orientation=False, iteration=0):
    """对3D模型文件执行全量分析，返回结构化字典。"""
    mesh = _load_mesh(file_path)

    # ── 水密性前置阻断 ──────────────────────────────────────────────────
    # 如果网格非水密，后续所有分析（体积、孔检测、壁厚、截面）均不可靠。
    # 立即返回错误，避免浪费计算资源并产生误导性结果。
    if not mesh.is_watertight:
        return {
            "status": "error",
            "schema_version": SCHEMA_VERSION,
            "message": (
                "STL mesh is not watertight — cannot perform reliable analysis. "
                "Volume, hole detection, wall thickness, and cross-section "
                "measurements are all invalid on non-watertight meshes. "
                "Fix: ensure the build123d script exports a closed, manifold solid."
            ),
            "file_analyzed": file_path,
            "mesh_info": {
                "vertex_count": len(mesh.vertices),
                "face_count": len(mesh.faces),
                "is_watertight": False,
            },
        }

    # 拓扑连通性（致命错误检测——部件是否断开）
    connectivity = _check_connectivity(mesh)
    # 网格分辨率（所有测量的可靠性基线）
    mesh_res = _estimate_mesh_resolution(mesh)

    # 加载白盒插桩法生成的特征测量数据（如果存在）
    feature_measurements = load_feature_measurements(iteration=iteration)

    # 模块一
    dim_data = analyze_dimensions(mesh)
    # 模块二
    hole_data = analyze_holes(mesh)
    # 模块三（传入孔数据以进行悬臂分析）
    struct_data = analyze_structure(mesh, dim_data, hole_data)
    # 模块四
    sym_data = analyze_symmetry(mesh, dim_data)
    # 模块五（依赖结构数据和尺寸数据）
    mfg_data = assess_manufacturability(mesh, struct_data, dim_data, skip_print_orientation)

    result = {
        "status": "success",
        "schema_version": SCHEMA_VERSION,
        "file_analyzed": file_path,
        "mesh_info": {
            "vertex_count": len(mesh.vertices),
            "face_count": len(mesh.faces),
            "is_watertight": True,
        },
        "connectivity": connectivity,  # ← 拓扑连通性（致命错误检测）
        "mesh_resolution": mesh_res,  # ← 测量噪声基线
        "dimensions": dim_data,
        "holes": hole_data,
        "structure": struct_data,
        "symmetry": sym_data,
        "manufacturability": mfg_data,
    }

    # 如果有特征测量数据，添加到结果中
    if feature_measurements:
        result["feature_measurements"] = feature_measurements
        print(f"[INFO] 已加载 {len(feature_measurements)} 个特征测量数据（白盒插桩法）")

    return result


# ============================================================================
#  CLI 入口
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="3D支架模型分析工具 —— 将3D模型转换为AI可理解的结构化报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python check_mesh.py bracket.stl                        # 默认JSON输出
  python check_mesh.py bracket.stl --format markdown      # 详细中文报告
  python check_mesh.py bracket.stl --spec spec.json        # 规格对比
  python check_mesh.py bracket.stl --spec spec.json -f markdown  # 规格对比+中文报告
  python check_mesh.py --generate l_bracket -o test.stl    # 生成测试模型
  python check_mesh.py --generate u_bracket -o u_test.stl  # 生成U型支架
  python check_mesh.py --generate flat_bracket             # 生成平板支架(默认名)
  python check_mesh.py --generate tube_clamp -o clamp.stl  # 生成管夹支架
        """,
    )
    parser.add_argument("file", nargs="?", help="3D模型文件路径（STL/OBJ/STEP等）")
    parser.add_argument("--format", "-f", choices=["json", "markdown"], default="json",
                        help="输出格式: json 或 markdown (默认json)")
    parser.add_argument("--terse", "-t", action="store_true",
                        help="精简模式：仅输出 FAIL/警告项，折叠已通过项（适合第2轮+迭代）")
    parser.add_argument("--spec", "-s", help="规格对比文件路径 (JSON格式)")
    parser.add_argument("--generate", "-g", choices=["l_bracket", "u_bracket", "flat_bracket", "tube_clamp"],
                        help="生成测试模型而非分析")
    parser.add_argument("--output", "-o", help="输出文件路径 (用于 --generate 或重定向报告)")
    parser.add_argument("--skip-print-orientation", action="store_true",
                        help="跳过打印方向评估（迭代过程中使用，最后自动旋转）")
    parser.add_argument("--iteration", type=int, default=0,
                        help="当前迭代编号，用于加载对应的特征测量文件 temp_measurements_{iteration}.json")
    args = parser.parse_args()

    # 生成模式
    if args.generate:
        print(f"[生成] 测试模型: {args.generate} ...")
        mesh = generate_test_bracket(args.generate)
        out_path = args.output or f"{args.generate}.stl"
        # 尝试修复法线（需 scipy）
        try:
            mesh.fix_normals()
        except Exception:
            pass
        mesh.export(out_path)
        print(f"[OK] 已保存: {os.path.abspath(out_path)}")
        print(f"     顶点数: {len(mesh.vertices)}, 面片数: {len(mesh.faces)}")
        print(f"     水密: {mesh.is_watertight}, 体积: {mesh.volume:.2f} mm^3")
        return

    # 分析模式
    if not args.file:
        parser.print_help()
        sys.exit(1)

    if not os.path.exists(args.file):
        print(json.dumps({"status": "error", "message": f"文件不存在: {args.file}"}, ensure_ascii=False))
        sys.exit(1)

    # 执行分析
    try:
        analysis = analyze_stl(args.file, skip_print_orientation=args.skip_print_orientation, iteration=args.iteration)
    except Exception as e:
        print(json.dumps({"status": "error", "message": f"分析失败: {str(e)}"}, ensure_ascii=False))
        sys.exit(1)

    # 规格对比
    spec_result = None
    if args.spec:
        if not os.path.exists(args.spec):
            print(json.dumps({"status": "error", "message": f"规格文件不存在: {args.spec}"}, ensure_ascii=False))
            sys.exit(1)
        try:
            with open(args.spec, 'r', encoding='utf-8') as f:
                spec = json.load(f)
            spec_result = compare_to_spec(analysis, spec)
            analysis["spec_comparison"] = spec_result
        except json.JSONDecodeError as e:
            print(json.dumps({"status": "error", "message": f"规格文件JSON解析错误: {str(e)}"}, ensure_ascii=False))
            sys.exit(1)

    # 输出
    if args.format == "markdown":
        output = generate_markdown_report(analysis, spec_result, terse=args.terse)
    else:
        output = generate_json_report(analysis)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(output)
        print(f"[OK] 报告已保存: {os.path.abspath(args.output)}")
    else:
        # Force UTF-8 encoding for stdout to avoid GBK encoding errors
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        print(output)


if __name__ == "__main__":
    main()
