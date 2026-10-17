"""Run a generated gen_step() script through cadpy in a separate process.

Invoked by absolute filename so a source checkout works from any job directory,
without importing the agent package or its provider configuration at startup.
The caller supplies the filtered environment; this is not a security sandbox.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", required=True, type=Path)
    parser.add_argument("--step", required=True, type=Path)
    parser.add_argument("--stl", required=True, type=Path)
    parser.add_argument("--iteration", required=True, type=int)
    parser.add_argument("--watch-parent", action="store_true")
    args = parser.parse_args()

    if args.watch_parent:
        # The parent keeps this pipe open without sending data. EOF means the
        # parent was cancelled or died; stop CAD work instead of orphaning it.
        # A pipe works on Windows too, where parent PID reparenting differs.
        def stop_on_parent_exit() -> None:
            # Avoid a buffered stdin lock held by a daemon during shutdown.
            os.read(sys.stdin.fileno(), 1)
            os._exit(1)

        threading.Thread(target=stop_on_parent_exit, daemon=True).start()

    repo_root = Path(__file__).resolve().parent.parent
    # Match the parent runner's source-checkout imports. Installed packages
    # still resolve through site-packages when the vendored source is absent.
    sys.path.insert(0, str(repo_root))
    sys.path.insert(0, str(repo_root / "packages" / "cadpy" / "src"))
    from cadpy.generation import EntrySpec, run_script_generator
    from cadpy.metadata import GeneratorMetadata

    script_path = args.script.resolve()
    step_path = args.step.resolve()
    stl_path = args.stl.resolve()
    name = f"temp_{args.iteration}"
    meta = GeneratorMetadata(
        script_path=script_path,
        kind="part",
        display_name=name,
        generator_names=("gen_step",),
        has_gen_step=True,
        has_gen_dxf=False,
        has_gen_urdf=False,
        has_gen_sdf=False,
        stl=str(stl_path) if stl_path.is_file() else None,
        three_mf=None,
        mesh_tolerance=None,
        mesh_angular_tolerance=None,
    )
    spec = EntrySpec(
        source_ref=name,
        cad_ref=name,
        kind="part",
        source_path=script_path,
        display_name=name,
        source="generated",
        step_path=step_path,
        script_path=script_path,
        stl_path=stl_path,
        generator_metadata=meta,
    )
    run_script_generator(spec, "gen_step")


if __name__ == "__main__":
    main()
