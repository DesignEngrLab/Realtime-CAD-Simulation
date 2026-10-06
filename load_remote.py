"""
load_remote.py

Runs OUTSIDE Omniverse entirely - just a plain Python script (needs
only `requests`, pip install requests). It tells the already-running,
already-streaming Kit app (with the Remote Asset Loader extension
enabled) to load a converted USD file into its live stage.

Usage:
    python load_remote.py "C:/path/to/converted_part.usd"
"""

import sys
import requests

# Match these to the machine/port the Kit app is actually running on.
# The port is printed in the Kit app's log when omni.services.transport
# .server.http starts up (default is usually 8011, but check the log -
# it auto-increments if that port is already taken).
KIT_HOST = "127.0.0.1"
KIT_PORT = 8011


def load_asset(usd_path: str,
                prim_path: str = "/World/ImportedAsset",
                position=(0.0, 0.0, 0.0),
                host: str = KIT_HOST,
                port: int = KIT_PORT):
    url = f"http://{host}:{port}/load_asset"
    payload = {
        "usd_path": usd_path,
        "prim_path": prim_path,
        "position": list(position),
    }

    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    result = resp.json()

    if not result.get("ok"):
        raise RuntimeError(f"Kit app reported an error: {result.get('error')}")

    print(f"Loaded '{result['loaded']}' into stage at prim '{result['prim']}' "
          f"({result.get('convex_decomposition_colliders_applied', 0)} mesh(es) "
          "got convex-decomposition colliders)")
    return result


def create_ground_plane(size: float,
                         prim_path: str = "/World/GroundPlane",
                         position=(0.0, 0.0, 0.0),
                         host: str = KIT_HOST,
                         port: int = KIT_PORT):
    """Ask the Kit app to add a ground plane to the live stage.

    `size` has no default here on purpose: it used to be 1000.0 in
    this function's signature AND separately defaulted in the Kit-side
    extension's request model. Bumping only the extension's default
    silently did nothing, because this function's own default of
    1000.0 was still sent explicitly in every request, overriding it.
    Making it required forces the caller to be the single source of
    truth for the value -- see GROUND_PLANE_SIZE in
    ExperimentCodeComplete.py.

    Mirrors load_asset()'s request shape. Requires a matching
    `/create_ground_plane` route in the Remote Asset Loader extension
    on the Kit side (e.g. wrapping
    omni.kit.commands.execute("CreateMeshPrimWithDefaultXform", prim_type="Plane", ...)
    or authoring a UsdGeom.Plane/Mesh directly on the stage) --
    that server-side handler doesn't exist yet and needs to be added
    alongside /load_asset.
    """
    url = f"http://{host}:{port}/create_ground_plane"
    payload = {
        "prim_path": prim_path,
        "size": size,
        "position": list(position),
    }

    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    result = resp.json()

    if not result.get("ok"):
        raise RuntimeError(f"Kit app reported an error: {result.get('error')}")

    print(f"Created ground plane at prim '{result['prim']}'")
    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python load_remote.py <path_or_url_to_converted_usd>")
        sys.exit(1)

    load_asset(sys.argv[1])
