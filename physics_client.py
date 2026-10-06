"""
physics_client.py

Runs OUTSIDE Omniverse, same as load_remote.py. Sends requests to the
already-running Kit app to apply materials, create physics joints, and
start the simulation.

Assumes three NEW endpoints exist on the same Kit app HTTP server that
already serves /load_asset: /apply_material, /create_joint,
/start_simulation. The actual USD/PhysX work for those lives in your
Kit extension (server side) -- share that extension's source and I'll
wire the routing to match its framework exactly. The functions below
just define the request/response contract; adjust the routing
decorators on your end to match.
"""

import requests

# Mirror of the same map in physics.py (inside the Kit extension) -- kept
# here too since this script runs as a separate process and can't import
# from the extension directly. Keep both in sync if you change one.
MATE_TYPE_MAP = {
    "FASTENED": "FIXED",
    "REVOLUTE": "REVOLUTE",
    "SLIDER": "PRISMATIC",
    "BALL": "SPHERICAL",
    "CYLINDRICAL": "D6",   # NOTE: D6 limits not yet configured -- see joint_map.py
    "PLANAR": "D6",        # NOTE: D6 limits not yet configured -- see joint_map.py
    "PIN_SLOT": "D6",
    "PARALLEL": "D6",
}

KIT_HOST = "127.0.0.1"
KIT_PORT = 8011


def apply_material(prim_path: str, color=(0.8, 0.8, 0.8), opacity: float = 1.0,
                    roughness: float = 0.5, metallic: float = 0.0,
                    host: str = KIT_HOST, port: int = KIT_PORT) -> dict:
    """Create (or reuse) a material with the given params and bind it to prim_path."""
    url = f"http://{host}:{port}/apply_material"
    payload = {
        "prim_path": prim_path,
        "color": list(color),
        "opacity": opacity,
        "roughness": roughness,
        "metallic": metallic,
    }
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Kit app reported an error: {result.get('error')}")
    print(f"Applied material to '{prim_path}'")
    return result


def get_bounds(prim_path: str = "/World/ImportedAsset",
                host: str = KIT_HOST, port: int = KIT_PORT) -> dict:
    """Return the real-world-meter bounding box size of prim_path in the live stage."""
    url = f"http://{host}:{port}/get_bounds"
    resp = requests.post(url, json={"prim_path": prim_path}, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Kit app reported an error: {result.get('error')}")
    return result


def list_prims(root_path: str = "/World/ImportedAsset",
                host: str = KIT_HOST, port: int = KIT_PORT) -> dict:
    """
    Return {"prims": [{"path": ..., "world_translation": [x,y,z] or None}, ...],
    "meters_per_unit": float} for the live stage under root_path.
    """
    url = f"http://{host}:{port}/list_prims"
    resp = requests.post(url, json={"root_path": root_path}, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Kit app reported an error: {result.get('error')}")
    return {"prims": result["prims"], "meters_per_unit": result.get("meters_per_unit", 1.0)}


def create_joint(joint_type: str, body0_path: str, body1_path: str,
                  local_pos0=(0.0, 0.0, 0.0), local_pos1=(0.0, 0.0, 0.0),
                  local_rot0=(1.0, 0.0, 0.0, 0.0), local_rot1=(1.0, 0.0, 0.0, 0.0),
                  axis=(0.0, 0.0, 1.0), joint_name: str = None,
                  onshape_mate_type: str = None,
                  host: str = KIT_HOST, port: int = KIT_PORT) -> dict:
    """
    joint_type: "FIXED" | "REVOLUTE" | "PRISMATIC" | "SPHERICAL" | "D6"
    body0_path/body1_path: USD prim paths of the two connected parts.
    local_pos0/1: joint frame origin relative to each body, in that
        body's local space (from Onshape mate connector origin).
    local_rot0/1: joint frame ORIENTATION relative to each body, as a
        (w, x, y, z) quaternion, in that body's local space (from Onshape
        mate connector x_axis/z_axis -- see generate_joint_map.py's
        _quat_from_connector_basis). Defaults to identity for callers
        that don't have orientation data; identity is fine for joint
        types with rotational freedom (REVOLUTE/PRISMATIC/SPHERICAL) but
        will make FIXED joints unreliable if the two bodies' local frames
        aren't already co-aligned (PhysX will report "disjointed body
        transforms" and can snap/eject the bodies).
    axis: joint axis direction (for REVOLUTE/PRISMATIC/D6). Only used as
        a fallback when local_rot0/1 are left at identity -- once a real
        local_rot0/1 is supplied, the joint frame's own Z axis already
        equals the mate connector's z_axis, so the Kit-side code uses
        "Z" directly instead of guessing a dominant axis letter from
        this raw, unrotated vector.
    onshape_mate_type: REQUIRED (in practice) when joint_type=="D6" --
        the original Onshape mate type ("PLANAR"/"CYLINDRICAL") this
        joint was derived from. USD Physics has no dedicated Planar/
        Cylindrical joint schema, so both map to D6 (UsdPhysics.Joint);
        without this, a D6 joint is created with every one of its 6
        degrees of freedom left completely free -- it constrains
        NOTHING. See physics._configure_d6_limits.
    """
    url = f"http://{host}:{port}/create_joint"
    payload = {
        "joint_type": joint_type,
        "body0_path": body0_path,
        "body1_path": body1_path,
        "local_pos0": list(local_pos0),
        "local_pos1": list(local_pos1),
        "local_rot0": list(local_rot0),
        "local_rot1": list(local_rot1),
        "axis": list(axis),
        "joint_name": joint_name,
        "onshape_mate_type": onshape_mate_type,
    }
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Kit app reported an error: {result.get('error')}")
    print(f"Created {joint_type} joint between '{body0_path}' and '{body1_path}'")
    return result


def start_simulation(host: str = KIT_HOST, port: int = KIT_PORT) -> dict:
    """Ensure a PhysicsScene exists and start timeline playback (runs the simulation)."""
    url = f"http://{host}:{port}/start_simulation"
    resp = requests.post(url, json={}, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Kit app reported an error: {result.get('error')}")
    print("Simulation started")
    return result


def stop_simulation(host: str = KIT_HOST, port: int = KIT_PORT) -> dict:
    """
    Pause timeline playback. Not needed for the normal landing-detection
    flow -- physics.py's ball landing tracker now calls this on the Kit
    side automatically the instant it detects a landing -- this is here
    for manually pausing on demand instead (e.g. from a script, or an
    interactive session).
    """
    url = f"http://{host}:{port}/stop_simulation"
    resp = requests.post(url, json={}, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Kit app reported an error: {result.get('error')}")
    print("Simulation paused")
    return result


def clear_asset(prim_path: str = "/World/ImportedAsset", joints_root: str = "/World/Joints",
                 host: str = KIT_HOST, port: int = KIT_PORT) -> dict:
    """
    Remove the previous run's asset (every part/body under prim_path)
    AND every joint under joints_root, plus any stale landing-tracker
    state, and reset the timeline to frame 0. Call this BEFORE
    re-loading on a webhook-triggered reload.

    joints_root matters just as much as prim_path -- joints live at a
    completely separate prim path from the bodies they connect;
    clearing only prim_path leaves every old joint behind, still
    pointing at the now-deleted bodies, which produces an immediate
    flood of "Joint body relationship ... points to a non existent
    prim" errors and leaves the NEXT create_joint pass re-authoring
    already-broken prims rather than genuinely fresh ones.
    """
    url = f"http://{host}:{port}/clear_asset"
    resp = requests.post(url, json={"prim_path": prim_path, "joints_root": joints_root}, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Kit app reported an error: {result.get('error')}")
    print(f"Cleared '{prim_path}' (removed={result['removed']}) and '{joints_root}' "
          f"(removed={result.get('joints_removed')}); trackers_cleared={result['trackers_cleared']}")
    return result


def make_dynamic(prim_path: str, host: str = KIT_HOST, port: int = KIT_PORT) -> dict:
    """
    Give prim_path real physics (RigidBodyAPI + convex-hull collision
    on its actual geometry, the pipeline's current default) without
    creating any joint. Use this for free-standing parts that should
    fall/collide/get thrown but aren't connected to anything by a mate
    -- e.g. a ball sitting in a mechanism's catch pocket.
    """
    url = f"http://{host}:{port}/make_dynamic"
    resp = requests.post(url, json={"prim_path": prim_path}, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Kit app reported an error: {result.get('error')}")
    print(f"Made '{prim_path}' a dynamic rigid body")
    return result


def set_collision_approximation(prim_path: str, approximation: str = "convexDecomposition",
                                 host: str = KIT_HOST, port: int = KIT_PORT) -> dict:
    """
    Override one specific part's collision approximation, on top of
    whatever default the rest of the pipeline uses (currently
    convexHull). Use this for any part whose concave features matter
    for the simulation -- e.g. the throwing arm's ball-catching pocket,
    which a plain convex hull would round off into a solid block,
    ejecting anything meant to rest inside it.
    """
    url = f"http://{host}:{port}/set_collision_approximation"
    resp = requests.post(url, json={"prim_path": prim_path, "approximation": approximation}, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Kit app reported an error: {result.get('error')}")
    print(f"Set '{prim_path}' collision approximation to '{approximation}'")
    # For convexDecomposition specifically: the approximation TOKEN and
    # the FIDELITY TUNING (voxelResolution/maxConvexHulls/shrinkWrap)
    # are two separate things that can succeed/fail independently --
    # print this directly instead of requiring a separate check of
    # Kit's own console for a warning that's easy to miss.
    tuning_failed = result.get("tuning_failed") or []
    tuning_applied = result.get("tuning_applied") or []
    if tuning_failed:
        print(f"  WARNING: fidelity tuning FAILED for {len(tuning_failed)} mesh(es) -- "
              f"falling back to PhysX's default decomposition resolution, which may "
              f"round off shallow concave features: {tuning_failed}")
    elif tuning_applied:
        print(f"  Fidelity tuning (voxelResolution=500000, maxConvexHulls=64, "
              f"shrinkWrap=True) applied successfully to {len(tuning_applied)} mesh(es).")
    return result


def set_mass(prim_path: str, mass_kg: float, host: str = KIT_HOST, port: int = KIT_PORT) -> dict:
    """
    Override a body's mass with an absolute value in kg, sourced from
    Onshape's own computed mass properties (see
    onshape_metadata.get_part_mass_properties) rather than PhysX's
    default of collision-shape volume x a generic density. The body
    must already have RigidBodyAPI (from a joint or make_dynamic)
    before this can be applied.
    """
    url = f"http://{host}:{port}/set_mass"
    resp = requests.post(url, json={"prim_path": prim_path, "mass_kg": mass_kg}, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Kit app reported an error: {result.get('error')}")
    print(f"Set '{prim_path}' mass to {mass_kg:.4f} kg")
    return result


def align_to_ground(prim_path: str = "/World/ImportedAsset", ground_prim_path: str = "/World/GroundPlane",
                     clearance: float = 0.0, host: str = KIT_HOST, port: int = KIT_PORT) -> dict:
    """
    Shift prim_path straight up/down along the stage's up-axis so its
    lowest point rests exactly on ground_prim_path's top surface (plus
    an optional clearance gap, in stage units). Computed from both
    prims' real, current bounding boxes -- not a hardcoded offset --
    so it stays correct even if the CAD model's own origin placement
    changes. Call this AFTER both /load_asset and /create_ground_plane
    have run, and before /start_simulation.
    """
    url = f"http://{host}:{port}/align_to_ground"
    resp = requests.post(url, json={"prim_path": prim_path, "ground_prim_path": ground_prim_path,
                                     "clearance": clearance}, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Kit app reported an error: {result.get('error')}")
    print(f"Aligned '{prim_path}' to ground: shifted {result['delta_applied_world']:.4f} world "
          f"stage units ({result['local_delta_applied']:.4f} local, /{result['scale_factor_on_up_axis']:.1f}x "
          f"scale) along the {result['up_axis']} axis. Was resting at {result['asset_min_before']:.4f}, "
          f"ground top is at {result['ground_top']:.4f}, actually landed at {result['asset_min_after']:.4f}.")
    return result


def start_ball_landing_tracker(prim_path: str, ground_prim_path: str = "/World/GroundPlane",
                                host: str = KIT_HOST, port: int = KIT_PORT) -> dict:
    """
    Start watching prim_path inside Kit's own physics step loop. The
    first time its lowest point reaches ground_prim_path's top surface,
    Kit prints its X position to its own console and records it --
    entirely independent of this client script; it keeps working even
    if this process exits or stops polling. Call once, right after
    /start_simulation.
    """
    url = f"http://{host}:{port}/start_ball_landing_tracker"
    resp = requests.post(url, json={"prim_path": prim_path, "ground_prim_path": ground_prim_path}, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Kit app reported an error: {result.get('error')}")
    print(f"Started landing tracker for '{prim_path}' (ground top at "
          f"{result['ground_top']:.4f} along {result['up_axis']})")
    return result


def get_ball_landing_result(prim_path: str, host: str = KIT_HOST, port: int = KIT_PORT) -> dict:
    """
    Poll the result of a tracker started by start_ball_landing_tracker.
    Optional -- Kit already prints the result on its own the moment it
    lands, independent of anyone calling this.
    """
    url = f"http://{host}:{port}/get_ball_landing_result"
    resp = requests.post(url, json={"prim_path": prim_path}, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Kit app reported an error: {result.get('error')}")
    return result


def auto_filter_overlapping_pairs(part_paths: list, already_exempt_pairs: list = None,
                                   threshold: float = 0.15,
                                   host: str = KIT_HOST, port: int = KIT_PORT) -> dict:
    """
    For every pair among part_paths NOT already in already_exempt_pairs
    (typically the pairs already handled by a direct joint's
    collisionEnabled=False), disable collision for JUST the pairs whose
    world-space bounding boxes substantially overlap. Pairs that merely
    touch (a legitimate mechanical stop, e.g. a pin's shoulder resting
    against a neighboring part) are left alone.

    Replaces an earlier, cruder disable_self_collision() (a single
    self-filtered CollisionGroup covering every mechanism part) that
    was confirmed live to be too broad -- it also removed legitimate
    contact-based stops that CYLINDRICAL/PRISMATIC joints' free axis
    relies on, so pins slid straight through with nothing to stop them.
    This is per-pair and driven by actual geometry overlap instead.
    """
    url = f"http://{host}:{port}/auto_filter_overlapping_pairs"
    payload = {
        "part_paths": part_paths,
        "already_exempt_pairs": [list(p) for p in (already_exempt_pairs or [])],
        "threshold": threshold,
    }
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Kit app reported an error: {result.get('error')}")
    filtered = result.get("filtered_pairs", [])
    print(f"Filtered collision for {len(filtered)} overlapping pair(s) "
          f"(out of {len(part_paths) * (len(part_paths) - 1) // 2} checked): {filtered}")
    return result
