"""Install a built wheel outside the checkout and exercise the mesh QA runtime.

Run after ``pip wheel . --no-deps --wheel-dir dist``:
    python tests/release/check_wheel_install.py dist
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    wheels = list(Path(sys.argv[1]).glob("multi_agent_cad-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"Expected one built wheel; found {wheels}")
    with tempfile.TemporaryDirectory(prefix="mac-wheel-check-") as directory:
        root = Path(directory)
        target = root / "installed"
        subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--target",
                        str(target), str(wheels[0].resolve())], check=True)
        env = {**os.environ, "PYTHONPATH": str(target), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("MAC_CONFIG_FILE", None)
        env.pop("DASHSCOPE_API_KEY", None)
        probe = '''
from pathlib import Path
import sys
from build123d import Box, export_stl
import cadpy.generation
import multi_agent_cad.nodes as nodes
from multi_agent_cad.schemas import CADBrief
assert Path(nodes.__file__).is_relative_to(Path(sys.argv[1]))
assert nodes._CHECK_MESH_SCRIPT.is_file(), "wheel is missing the mesh engine"
export_stl(Box(10, 20, 30), "part.stl")
brief = CADBrief(part_name="box", part_category="flat-plate", user_request_raw="box")
report = nodes._run_engine_b_check_mesh(stl_path=str(Path("part.stl").resolve()), cad_brief=brief)
assert not report.errors, report.errors
assert report.raw_json, "installed mesh engine returned no analysis"
print("Installed wheel: CAD imports and mesh QA passed without credentials.")
'''
        subprocess.run([sys.executable, "-B", "-c", probe, str(target)],
                       cwd=root, env=env, check=True, timeout=180)


if __name__ == "__main__":
    main()
