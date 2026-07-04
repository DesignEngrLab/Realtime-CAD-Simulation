
"""
step_to_omniverse.py — standalone CLI

Converts a local STEP (.step/.stp) file to USD and uploads it to a Nucleus
server, entirely from the command line. This runs as its own process; it does
NOT need (or talk to) any already-open Omniverse application — it talks
directly to the Nucleus server, the same server your open app is connected
to. Once the upload finishes, refreshing the folder in your open app (or
the Content window's auto-refresh) will show the new file.

Setup (one time):
    python -m venv ov-env
    ov-env\\Scripts\\activate          (Windows)   or   source ov-env/bin/activate   (Linux)
    pip install omniverse-kit --extra-index-url https://pypi.nvidia.com

Usage:
    python step_to_omniverse.py "C:/cad/bracket.step" "omniverse://localhost/Users/me/parts/bracket.usd"

Optional flags:
    --keep-local        Keep the intermediate local USD file (default: deleted after upload)
    --message "text"    Checkpoint/comment attached to the upload, visible in Nucleus version history
    --no-optimize       Skip the scene-optimizer pass (bOptimize=false)
    --no-instancing     Don't instance repeated parts (instancing=false)
"""




""" Extra steps required. Only works on python 3.12 or earlier
run these lines in powershell

Remove-Item -Recurse -Force ov-env
py -3.12 -m venv ov-env
ov-env\Scripts\activate
pip install omniverse-kit --extra-index-url https://pypi.nvidia.com


Run program
step_to_omniverse.py "step file location" "where you are uploading it to"


Also has lots of extra steps as it was trying to upload directly to omniverse after conversion.
Doesn't work, but does create the file. At some point will go through and remove unnecessary code or 
modify to make work
"""

import argparse
import asyncio
import os
import sys
import tempfile

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

    os.makedirs(os.path.dirname(usd_output_path), exist_ok=True)

    converter = hoops_core.get_instance()
    await converter.create_converter_task(step_path, usd_output_path, options)

    if not os.path.exists(usd_output_path):
        raise RuntimeError(f"Conversion did not produce an output file: {usd_output_path}")


async def upload_to_nucleus(local_usd_path: str, nucleus_url: str, message: str, overwrite: bool = True) -> None:
    """Upload a local file to a Nucleus server path (e.g. omniverse://server/path/to/file.usd)."""
    import omni.client

    omni.client.initialize()

    behavior = omni.client.CopyBehavior.OVERWRITE if overwrite else omni.client.CopyBehavior.ERROR_IF_EXISTS

    result = await omni.client.copy_async(
        local_usd_path,
        nucleus_url,
        behavior=behavior,
        message=message,
    )

    if result != omni.client.Result.OK:
        raise RuntimeError(f"Upload to {nucleus_url} failed: {omni.client.get_result_string(result)}")


async def run(step_path: str, nucleus_dest: str, message: str, options: dict, keep_local_usd: bool) -> str:
    if not os.path.isfile(step_path):
        raise FileNotFoundError(step_path)

    base_name = os.path.splitext(os.path.basename(step_path))[0]
    work_dir = tempfile.mkdtemp(prefix="step2usd_")
    local_usd_path = os.path.join(work_dir, f"{base_name}.usd")

    print(f"[1/3] Converting {step_path}")
    print(f"      -> {local_usd_path}")
    await convert_step_to_usd(step_path, local_usd_path, options)

    print(f"[2/3] Uploading to {nucleus_dest}")
    await upload_to_nucleus(local_usd_path, nucleus_dest, message)

    print("[3/3] Done. Refresh the folder in your open Omniverse app to see it.")

    if not keep_local_usd:
        try:
            os.remove(local_usd_path)
            os.rmdir(work_dir)
        except OSError:
            pass

    return nucleus_dest


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert a STEP file to USD and upload it to Nucleus.")
    parser.add_argument("step_path", help="Path to the local .step/.stp file")
    parser.add_argument("nucleus_dest", help="Destination Nucleus URL, e.g. omniverse://localhost/Users/me/part.usd")
    parser.add_argument("--keep-local", action="store_true", help="Keep the intermediate local USD file")
    parser.add_argument("--message", default="Uploaded via step_to_omniverse.py", help="Checkpoint comment for the upload")
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
        "--enable", "omni.client",
        "--enable", "omni.kit.converter.hoops_core",
        "--/app/quitAfter=-1",  # don't auto-quit after N frames; we control shutdown ourselves
    ])

    # Drive our async pipeline as a task scheduled on Kit's own event loop,
    # and keep pumping kit_app.update() until it's done. A bare asyncio.run()
    # is not enough here because Kit's extensions (hoops_core's task system,
    # omni.client's async ops) are serviced by Kit's frame loop, not just by
    # the asyncio loop alone.
    result_holder = {"error": None}

    async def _wrapped():
        try:
            await run(args.step_path, args.nucleus_dest, args.message, options, args.keep_local)
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