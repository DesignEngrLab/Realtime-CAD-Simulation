"""
onshape_urdf_export.py

Exports an Onshape ASSEMBLY as URDF (+ OBJ meshes) through the REST API and
unpacks it locally -- the API equivalent of Export -> URDF in the Onshape UI,
replacing the STEP export the pipeline used before.

Request/response flow matches pull_urdf_debug.py, which was confirmed
working against this assembly:

    1. POST /assemblies/d/{did}/w/{wid}/e/{eid}/translations
         formatName "URDF", urdfMeshFormat "OBJ", storeInDocument false,
         PLUS the tessellation/resolution parameters -- REQUIRED: without
         them Onshape rejects the export with "Invalid resolution
         parameters were specified" (the export tessellates a mesh per link)
       -> may already be requestState DONE for a small assembly; otherwise
          ACTIVE with an "href" to poll
    2. GET href (or /translations/{id}) until DONE / FAILED
    3. GET /documents/d/{did}/externaldata/{id} -- confirmed: ONE id, a zip
       holding the .urdf and every .obj/.mtl together

Same credentials as the rest of the project (File_Name._get_credentials:
ONSHAPE_ACCESS_KEY / ONSHAPE_SECRET_KEY environment variables).

Usage:
    python onshape_urdf_export.py <did> <wid> <assembly_eid> <out_dir>
"""

import base64
import glob
import io
import os
import sys
import time
import zipfile

import requests

from File_Name import _get_credentials, BASE_URL

API_VERSION = "v16"

# urdf_obj_asset.py reads OBJ + MTL, which is what your manual export
# produced. The export API also accepts other mesh formats (GLTF, STL), but
# the asset builder would need a matching reader for those.
URDF_MESH_FORMAT = "OBJ"

# Tessellation settings for the exported meshes -- same values as the
# confirmed-working pull_urdf_debug.py. "fine"/smaller tolerances give
# denser meshes (tighter collision hulls, slower convex decomposition).
MESH_RESOLUTION = {
    "resolution": "medium",
    "distanceTolerance": 0.0001,
    "angularTolerance": 0.1090830782496456,
    "maximumChordLength": 10,
}

POLL_INTERVAL_S = 2.0
EXPORT_TIMEOUT_S = 300.0


def _headers(accept="application/json;charset=UTF-8;qs=0.09"):
    access_key, secret_key = _get_credentials()
    token = base64.b64encode(f"{access_key}:{secret_key}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Accept": accept, "Content-Type": "application/json"}


def _raise_with_body(resp, what):
    if resp.status_code >= 400:
        raise requests.HTTPError(f"{what} failed: HTTP {resp.status_code}: {resp.text[:500]}", response=resp)


def start_urdf_translation(did, wid, eid, mesh_format=URDF_MESH_FORMAT, configuration=None) -> dict:
    url = f"{BASE_URL}/api/{API_VERSION}/assemblies/d/{did}/w/{wid}/e/{eid}/translations"
    body = {
        "formatName": "URDF",
        "storeInDocument": False,  # download it; don't add a blob tab to the document
        "urdfMeshFormat": mesh_format,
        **MESH_RESOLUTION,
    }
    if configuration:
        body["configuration"] = configuration
    resp = requests.post(url, headers=_headers(), json=body, timeout=30)
    _raise_with_body(resp, "Starting URDF export")
    return resp.json()


def wait_for_translation(initial: dict, timeout_s=EXPORT_TIMEOUT_S) -> dict:
    """Return the finished translation. Skips polling entirely if the
    initial response is already DONE (common for a small assembly)."""
    info = initial
    tid = initial.get("id") or initial.get("translationId")
    url = initial.get("href") or (f"{BASE_URL}/api/{API_VERSION}/translations/{tid}" if tid else None)
    deadline = time.time() + timeout_s
    while True:
        state = info.get("requestState")
        if state == "DONE":
            return info
        if state == "FAILED":
            raise RuntimeError(f"Onshape URDF export FAILED: {info.get('failureReason') or info}")
        if not url:
            raise RuntimeError(f"URDF export is {state!r} but the response has no 'href' or 'id' "
                               f"to poll: {info}")
        if time.time() > deadline:
            raise TimeoutError(f"URDF export still {state!r} after {timeout_s:.0f}s (translation {tid}).")
        time.sleep(POLL_INTERVAL_S)
        resp = requests.get(url, headers=_headers(), timeout=30)
        _raise_with_body(resp, "Polling URDF export")
        info = resp.json()


def download_external_data(did, fid) -> bytes:
    url = f"{BASE_URL}/api/{API_VERSION}/documents/d/{did}/externaldata/{fid}"
    # Same headers as the confirmed-working pull_urdf_debug.py download.
    resp = requests.get(url, headers=_headers(), timeout=120)
    _raise_with_body(resp, "Downloading URDF export")
    return resp.content


def locate_export(out_dir):
    """-> (urdf_path, mesh_dir). Searches the unpacked export at any depth,
    so it doesn't matter whether the zip has a top-level <name>/ folder."""
    urdfs = sorted(glob.glob(os.path.join(out_dir, "**", "*.urdf"), recursive=True))
    if not urdfs:
        raise FileNotFoundError(f"No .urdf found in the downloaded export under {out_dir!r}.")
    if len(urdfs) > 1:
        print(f"WARNING: {len(urdfs)} .urdf files in export, using {urdfs[0]}")
    urdf = urdfs[0]
    # Standard layout is <pkg>/urdf/x.urdf + <pkg>/meshes/*.obj; fall back to
    # wherever the OBJs actually are.
    mesh_dir = os.path.join(os.path.dirname(os.path.dirname(urdf)), "meshes")
    if not os.path.isdir(mesh_dir):
        objs = sorted(glob.glob(os.path.join(out_dir, "**", "*.obj"), recursive=True))
        mesh_dir = os.path.dirname(objs[0]) if objs else os.path.dirname(urdf)
    return urdf, mesh_dir


def export_assembly_urdf(did, wid, eid, out_dir, mesh_format=URDF_MESH_FORMAT, configuration=None):
    """Export + download + unpack. Returns (urdf_path, mesh_dir)."""
    os.makedirs(out_dir, exist_ok=True)
    t0 = time.time()
    print(f"Requesting URDF export from Onshape ({mesh_format} meshes)...")
    info = wait_for_translation(start_urdf_translation(did, wid, eid, mesh_format, configuration))
    ids = info.get("resultExternalDataIds") or []
    if not ids:
        raise RuntimeError(f"URDF export finished but returned no downloadable file: {info}")

    for n, fid in enumerate(ids):
        data = download_external_data(did, fid)
        if data[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for member in zf.namelist():  # refuse path traversal
                    target = os.path.abspath(os.path.join(out_dir, member))
                    if not target.startswith(os.path.abspath(out_dir) + os.sep) and target != os.path.abspath(out_dir):
                        raise RuntimeError(f"Unsafe path in export zip: {member!r}")
                zf.extractall(out_dir)
        else:
            # Not zipped -- save as-is (a lone URDF would have no meshes).
            with open(os.path.join(out_dir, f"export_{n}.urdf"), "wb") as f:
                f.write(data)

    urdf, mesh_dir = locate_export(out_dir)
    print(f"URDF export ready in {time.time() - t0:.1f}s: {urdf} (meshes: {mesh_dir})")
    return urdf, mesh_dir


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: python onshape_urdf_export.py <did> <wid> <assembly_eid> <out_dir>")
        sys.exit(1)
    export_assembly_urdf(*sys.argv[1:5])
