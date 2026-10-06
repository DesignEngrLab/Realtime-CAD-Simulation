
"""
step_to_usd.py — standalone CLI

Converts a local STEP (.step/.stp) file to USD. The output .usd file is
written to the SAME directory as the input STEP file, using the same base
name (e.g. bracket.step -> bracket.usd).

This runs as its own process; it does NOT need (or talk to) any already-open
Omniverse application, and it does NOT upload anywhere — it only performs
the local CAD -> USD conversion.

Setup (one time):
    python -m venv ov-env
    ov-env\\Scripts\\activate          (Windows)   or   source ov-env/bin/activate   (Linux)
    pip install omniverse-kit --extra-index-url https://pypi.nvidia.com

Note: only works on Python 3.12 or earlier. If you hit install issues on
Windows, try:
    Remove-Item -Recurse -Force ov-env
    py -3.12 -m venv ov-env
    ov-env\\Scripts\\activate
    pip install omniverse-kit --extra-index-url https://pypi.nvidia.com

Usage:
    python step_to_usd.py "C:/cad/bracket.step"

Optional flags:
    --no-optimize        Skip the scene-optimizer pass (bOptimize=false)
    --no-instancing      Don't instance repeated parts (instancing=false)
"""

import argparse
import asyncio
import os
import sys

# omni.kit_app.KitApp must be the FIRST Omniverse import in the process.
# Importing it triggers Kit's bootstrapping (Carbonite, sys.path setup, etc.)
from omni.kit_app import KitApp


async def convert_step_to_usd(step_path: str, usd_output_path: str, options: dict) -> None:
    """Convert a local STEP file to a local USD file using the HOOPS CAD core converter."""
    import omni.kit.app
    import omni.kit.converter.hoops_core as hoops_core

    # Make sure the extension is actually enabled before using it — registering
    # it in startup args is not always enough by itself for converter exts.
    ext_manager = omni.kit.app.get_app().get_extension_manager()
    ext_manager.set_extension_enabled_immediate("omni.kit.converter.hoops_core", True)

    os.makedirs(os.path.dirname(usd_output_path) or ".", exist_ok=True)

    converter = hoops_core.get_instance()
    await converter.create_converter_task(step_path, usd_output_path, options)

    if not os.path.exists(usd_output_path):
        raise RuntimeError(f"Conversion did not produce an output file: {usd_output_path}")


async def run(step_path: str, options: dict) -> str:
    if not os.path.isfile(step_path):
        raise FileNotFoundError(step_path)

    step_dir = os.path.dirname(os.path.abspath(step_path))
    base_name = os.path.splitext(os.path.basename(step_path))[0]
    usd_output_path = os.path.join(step_dir, f"{base_name}.usd")

    print(f"[1/2] Converting {step_path}")
    print(f"      -> {usd_output_path}")
    await convert_step_to_usd(step_path, usd_output_path, options)

    print("[2/2] Done.")

    return usd_output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert a STEP file to USD (saved next to the input file).")
    parser.add_argument("step_path", help="Path to the local .step/.stp file")
    parser.add_argument("--no-optimize", action="store_true", help="Disable the scene optimizer pass")
    parser.add_argument("--no-instancing", action="store_true", help="Disable mesh instancing of repeated parts")
    args = parser.parse_args()

    options = {
        "bOptimize": "false" if args.no_optimize else "true",
        "instancing": "false" if args.no_instancing else "true",
    }

    kit_app = KitApp()
    kit_app.startup([
        "--no-window",
        "--enable", "omni.kit.converter.hoops_core",
        "--/app/quitAfter=-1",  # don't auto-quit after N frames; we control shutdown ourselves
    ])

    # Drive our async pipeline as a task scheduled on Kit's own event loop,
    # and keep pumping kit_app.update() until it's done. A bare asyncio.run()
    # is not enough here because Kit's extensions (hoops_core's task system)
    # are serviced by Kit's frame loop, not just by the asyncio loop alone.
    result_holder = {"error": None}

    async def _wrapped():
        try:
            await run(args.step_path, options)
        except Exception as e:
            result_holder["error"] = e

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(_wrapped())

    try:
        while not task.done():
            kit_app.update()
            loop.stop()
            loop.run_forever()  # drain any callbacks asyncio scheduled this iteration
    finally:
        kit_app.shutdown()

    if result_holder["error"] is not None:
        print(f"ERROR: {result_holder['error']}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())