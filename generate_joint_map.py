"""
generate_joint_map.py

Auto-generates joint_map.py by combining:

    1. onshape_metadata.get_assembly_mates_resolved() -- mate name/type,
       WHICH two part instances each mate connects, and each connector's
       origin/z-axis, all read directly via REST (includeMateFeatures=true).
    2. physics_client.list_prims() -- the ACTUAL prim paths in the
       already-loaded USD stage (queried live from the running Kit app,
       via the /list_prims endpoint).

Matching Onshape instance names to USD prim names is done by NORMALIZING
both sides (stripping the "tn__" prefix and Onshape/USD's appended
name-mangling hash, e.g. "tn__CrossBar1_k9" -> "crossbar1") rather than
trying to reverse-engineer the exact mangling algorithm -- that's a
heuristic, not a guarantee, so this script prints every match (with an
AMBIGUOUS/NO MATCH flag where relevant) for you to eyeball before
anything is written to disk. Nothing is overwritten unless you pass
--write.

Run this AFTER your normal pipeline has loaded the assembly into the
stage (list_prims reads the live stage, same requirement as
create_all_joints.py).

Usage:
    python generate_joint_map.py                 # preview only
    python generate_joint_map.py --write          # also write joint_map.py
"""

import re
import sys
import math
import argparse
import itertools

import onshape_metadata
import physics_client

# Same IDs used elsewhere in this project (ExperimentCodeComplete.py).
DID = "e8c7844adc5a5adebdfaf453"
WID = "d8f6208d9ffc29e8942099d9"
EIDa = "df78440df4dc821dd2e3bed5"

PRIM_ROOT = "/World/ImportedAsset"

# Part-type names (matched via _group_key, so "ThrowingArm <1>",
# "ThrowingArm_1", etc. all match "ThrowingArm" the same way instance
# names are matched to prims elsewhere in this file) whose concave
# features matter for the simulation and must NOT be rounded off by
# the pipeline's default convexHull collision approximation. Confirmed
# live: with the arm's ball-catching pocket flattened into a solid
# convex hull, the ball was technically INSIDE solid geometry from
# PhysX's perspective and got violently ejected sideways. Add other
# part names here (e.g. a slot something slides through) if the same
# symptom shows up elsewhere.
CONCAVE_COLLISION_PARTS = ["ThrowingArm"]

# Must match the `asset_meters_per_unit` value passed to /load_asset for
# this same asset (see extension.py's LoadAssetRequest / how
# ExperimentCodeComplete.py calls load_remote.load_asset() -- it currently
# relies on that field's default of 1.0, i.e. the STEP->USD conversion
# authors the asset at 1 unit = 1 meter, same as Onshape's own API units).
#
# This is DELIBERATELY separate from the live stage's own metersPerUnit
# (fetched at runtime as `meters_per_unit` below from list_prims). Local
# joint offsets (local_pos0/local_pos1) are expressed in each body's own
# local frame, BEFORE any ancestor transform is applied -- and the 100x
# corrective scale extension.py's /load_asset puts on the ImportedAsset
# Xform (to reconcile the asset's meters against the stage's own, unrelated
# metersPerUnit) is exactly such an ancestor transform. That correction is
# already baked into a body's WORLD transform; applying it a second time to
# a LOCAL offset overshoots the pivot by that same factor. Local offsets
# need dividing by the ASSET's own meters_per_unit, not the stage's.
ASSET_METERS_PER_UNIT = 1.0


def _group_key(name: str) -> str:
    """
    Normalize a name (Onshape instance name OR USD prim leaf name) down
    to a base "part type" key for GROUPING only -- e.g. "Base <1>",
    "Base <2>", "Base", "Base_1" all collapse to "base". Any trailing
    duplicate-instance index or name-mangling hash is stripped and
    discarded here (not used for matching -- see build_name_to_prim_map
    for why).
    """
    s = name.lower()
    s = re.sub(r"^tn__", "", s)
    s = re.sub(r"[\s_<\-]\d+>?$", "", s)  # strip a trailing dup-looking index, any value
    m2 = re.match(r"^(.*)_([a-z0-9]{1,3})$", s)
    if m2 and not m2.group(2).isdigit():
        s = m2.group(1)
    return re.sub(r"[^a-z0-9]", "", s)


def _dist2(a, b) -> float:
    return sum((a[i] - b[i]) ** 2 for i in range(3))


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _normalize(v):
    n = math.sqrt(sum(c * c for c in v))
    return tuple(c / n for c in v) if n else v


def _quat_from_matrix(m):
    """
    Standard (Shepperd's method) 3x3 rotation-matrix -> quaternion
    conversion, returned as (w, x, y, z). m is row-major: m[row][col].
    """
    trace = m[0][0] + m[1][1] + m[2][2]
    if trace > 0:
        s = 0.5 / math.sqrt(trace + 1.0)
        w = 0.25 / s
        x = (m[2][1] - m[1][2]) * s
        y = (m[0][2] - m[2][0]) * s
        z = (m[1][0] - m[0][1]) * s
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = 2.0 * math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2])
        w = (m[2][1] - m[1][2]) / s
        x = 0.25 * s
        y = (m[0][1] + m[1][0]) / s
        z = (m[0][2] + m[2][0]) / s
    elif m[1][1] > m[2][2]:
        s = 2.0 * math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2])
        w = (m[0][2] - m[2][0]) / s
        x = (m[0][1] + m[1][0]) / s
        y = 0.25 * s
        z = (m[1][2] + m[2][1]) / s
    else:
        s = 2.0 * math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1])
        w = (m[1][0] - m[0][1]) / s
        x = (m[0][2] + m[2][0]) / s
        y = (m[1][2] + m[2][1]) / s
        z = 0.25 * s
    return (w, x, y, z)


def _quat_from_connector_basis(x_axis, z_axis):
    """
    Build the joint-frame rotation (as a quaternion, w,x,y,z) for one side
    of a mate, from that mate connector's x_axis and z_axis -- both
    already expressed in the OWNING BODY's own local space (per
    onshape_metadata.py's matedCS parsing).

    y_axis is deliberately NOT read from Onshape (even where present) --
    it's re-derived here as z (cross) x, then x is re-derived as y
    (cross) z, to guarantee an orthonormal, right-handed basis even if
    Onshape's own x/z weren't already perfectly perpendicular (mate
    connectors are usually exact, but this costs nothing and removes a
    class of numerical-drift bugs).

    The resulting rotation maps the JOINT frame's standard axes (as
    UsdPhysics.*Joint's own "axis" token, e.g. "Z", means them) into
    this body's local frame -- i.e. exactly what UsdPhysics.Joint's
    CreateLocalRot0Attr/CreateLocalRot1Attr expects. Because the joint
    frame's Z is constructed to equal this connector's own z_axis,
    create_joint_impl (physics.py) no longer needs to guess a
    "dominant axis letter" from a raw, unrotated vector -- once
    local_rot0/1 are set, the joint's constrained axis is always "Z".
    """
    z = _normalize(z_axis)
    x = _normalize(x_axis)
    y = _normalize(_cross(z, x))
    x = _normalize(_cross(y, z))  # re-orthogonalize against the final y/z

    # Columns of the rotation matrix are the joint frame's basis vectors
    # (x, y, z), expressed in the body's local coordinates.
    m = (
        (x[0], y[0], z[0]),
        (x[1], y[1], z[1]),
        (x[2], y[2], z[2]),
    )
    return _quat_from_matrix(m)


def _best_assignment(onshape_items, prim_items):
    """
    onshape_items / prim_items: list of (label, (x, y, z)).

    Pairs onshape_items to prim_items by minimizing TOTAL squared
    distance across the whole group (brute-force over permutations --
    fine given these groups are just a handful of duplicate part
    instances, not hundreds). Falls back to greedy nearest-neighbor if
    the group is larger than is sane to brute-force, or if the two
    sides have different counts (in which case leftover items are
    unmatched -- printed for manual follow-up).

    Returns {onshape_label: prim_path_or_None}.
    """
    labels = [n for n, _ in onshape_items]
    pts = [p for _, p in onshape_items]
    prim_paths = [p for p, _ in prim_items]
    prim_pts = [p for _, p in prim_items]

    if len(pts) == len(prim_pts) and 0 < len(pts) <= 8:
        best_perm, best_cost = None, None
        for perm in itertools.permutations(range(len(prim_pts))):
            cost = sum(_dist2(pts[i], prim_pts[perm[i]]) for i in range(len(pts)))
            if best_cost is None or cost < best_cost:
                best_cost, best_perm = cost, perm
        return {labels[i]: prim_paths[best_perm[i]] for i in range(len(pts))}

    # Greedy fallback: unequal counts, or a group too large to brute-force.
    remaining = list(range(len(prim_pts)))
    result = {}
    for i, pt in enumerate(pts):
        if not remaining:
            result[labels[i]] = None
            continue
        j = min(remaining, key=lambda j: _dist2(pt, prim_pts[j]))
        result[labels[i]] = prim_paths[j]
        remaining.remove(j)
    return result


def select_part_candidates(prim_entries: list) -> list:
    """
    From the full set of prims under prim_root (every intermediate
    Xform AND every leaf Mesh, at whatever depth this particular STEP
    export produced), select one candidate prim per physical part --
    the shallowest prim at which each part's name first appears.

    Blindly picking the DEEPEST leaf breaks on a STEP export where the
    converter wraps a part's geometry as an extra self-nested layer
    (".../ThrowingArm/ThrowingArm/Mesh" instead of a flat
    ".../ThrowingArm"): the actual leaf is named the generic "Mesh" for
    literally every part, collapsing every part into one
    indistinguishable group. CAD/STEP->USD converters aren't guaranteed
    to produce the same nesting depth between exports of the same
    assembly (e.g. after any edit upstream in Onshape), so relying on a
    fixed depth is fragile either way.

    For each prim, walk up to its immediate parent. If the parent's own
    name normalizes (via _group_key) to the SAME key as this prim's,
    this prim is a redundant self-nested wrapper (or the generic Mesh
    leaf inside one) -- its parent is the real "part" prim, so skip it.
    Otherwise this is the shallowest point this part's name appears,
    and becomes the candidate -- usable directly as a joint's body path
    OR as a target for physics.make_dynamic_impl for a part with no
    joint at all -- regardless of what's further nested below it
    (physics.py's create_joint_impl/_ensure_rigid_body/make_dynamic_impl
    already descend from a body path to find whatever Mesh children
    exist beneath it, however deep).
    """
    by_path = {e["path"]: e for e in prim_entries}
    candidates = []
    for e in prim_entries:
        path = e["path"]
        own_key = _group_key(path.rsplit("/", 1)[-1])
        if not own_key:
            continue
        parent_path = path.rsplit("/", 1)[0]
        parent_entry = by_path.get(parent_path)
        if parent_entry is not None:
            parent_key = _group_key(parent_path.rsplit("/", 1)[-1])
            if parent_key == own_key:
                continue  # redundant self-nested wrapper -- parent is the real candidate
        candidates.append(e)
    return candidates


def build_name_to_prim_map(instance_names: set, prim_entries: list, instance_world: dict,
                            meters_per_unit: float, debug: bool = True) -> dict:
    """
    Match each Onshape instance_name to the single best USD prim path.

    Onshape's own duplicate-instance numbering ("Base <1>"/"Base <2>")
    has NO reliable correspondence to whatever suffix the STEP->USD
    conversion assigned -- confirmed live: "Base <1>" landed on
    "Base_1" while "Base <2>" matched nothing, i.e. not even a
    consistent offset-by-one. So instead of trusting either side's
    numbering: group both sides by base part-type name only (ignoring
    the index entirely), then within each group pair items by their
    REAL WORLD POSITION -- unambiguous, since two different physical
    instances of the same part can't occupy the same place.

    Onshape positions come back in meters (rootAssembly.occurrences'
    transform); prim positions come back in the stage's native units --
    meters_per_unit converts between them before comparing.

    See select_part_candidates() for how candidate prims are chosen.
    """
    leaf_entries = select_part_candidates(prim_entries)

    if debug:
        print(f"\n  (full candidate prim list under the searched root, {len(leaf_entries)} total, for reference:)")
        for e in leaf_entries:
            print(f"    {e['path']}  world={e['world_translation']}")
        print()

    prim_groups = {}
    for e in leaf_entries:
        leaf_name = e["path"].rsplit("/", 1)[-1]
        prim_groups.setdefault(_group_key(leaf_name), []).append(e)

    name_groups = {}
    for name in instance_names:
        name_groups.setdefault(_group_key(name), []).append(name)

    result = {}
    for base_key, names in sorted(name_groups.items()):
        candidates = prim_groups.get(base_key, [])

        if not candidates:
            for name in names:
                result[name] = None
            if debug:
                print(f"  NO MATCH  {names!r:30s} -> (key={base_key!r}) no leaf prim group matched at all")
            continue

        if len(names) == 1 and len(candidates) == 1:
            result[names[0]] = candidates[0]["path"]
            if debug:
                print(f"  OK        {names[0]!r:30s} -> {candidates[0]['path']}")
            continue

        # Multiple instances of the same part -- disambiguate by position.
        onshape_pts = []
        missing_pos = []
        for name in names:
            w = instance_world.get(name)
            if w is None:
                missing_pos.append(name)
            else:
                onshape_pts.append((name, tuple(v / meters_per_unit for v in w)))

        prim_pts = []
        for e in candidates:
            if e["world_translation"] is None:
                missing_pos.append(e["path"])
            else:
                prim_pts.append((e["path"], tuple(e["world_translation"])))

        if missing_pos or not onshape_pts or not prim_pts:
            for name in names:
                result[name] = None
            if debug:
                print(f"  AMBIGUOUS {names!r:30s} -> (key={base_key!r}) missing world-position "
                      f"data, can't disambiguate: {missing_pos}")
            continue

        if len(names) != len(candidates) and debug:
            print(f"  WARNING (key={base_key!r}) count mismatch: {len(names)} Onshape instance(s) "
                  f"{names} vs {len(candidates)} prim(s) {[e['path'] for e in candidates]} -- "
                  f"pairing what we can by nearest position, rest left unmatched")

        if debug:
            print(f"  (key={base_key!r}) raw onshape world (÷meters_per_unit) vs raw prim world, "
                  f"for a direct scale sanity-check:")
            for name, pt in onshape_pts:
                raw = instance_world[name]
                print(f"    onshape {name!r}: raw_meters={raw} "
                      f"scaled(÷{meters_per_unit})={tuple(round(v, 4) for v in pt)}")
            for path, pt in prim_pts:
                print(f"    prim    {path!r}: world={tuple(round(v, 4) for v in pt)}")

        assignment = _best_assignment(onshape_pts, prim_pts)
        for name in names:
            path = assignment.get(name)
            result[name] = path
            if debug:
                if path:
                    print(f"  OK (pos)  {name!r:30s} -> {path}")
                else:
                    print(f"  NO MATCH  {name!r:30s} -> (key={base_key!r}) no prim left after assignment")

    return result


def generate(prim_root: str = PRIM_ROOT, debug: bool = False) -> list:
    """Fetch resolved mates + live prims and return the list of resolved joint entries."""
    print(f"Fetching resolved mates from Onshape ({DID[:8]}.../{WID[:8]}.../{EIDa[:8]}...)...")
    mates = onshape_metadata.get_assembly_mates_resolved(DID, WID, EIDa, debug=debug)
    if not mates:
        print("No mates found -- nothing to generate. Check the assembly IDs above.")
        return []
    print(f"Found {len(mates)} mates.")

    print(f"\nFetching live prim list from Kit app under '{prim_root}'...")
    try:
        prim_result = physics_client.list_prims(root_path=prim_root)
    except Exception as e:
        print(f"Could not reach the Kit app / list_prims failed: {e}")
        print("Trying '/World' instead, to see what's actually in the stage right now...")
        try:
            world_result = physics_client.list_prims(root_path="/World")
            print(f"\nFound {len(world_result['prims'])} prims under /World:")
            for e in world_result["prims"]:
                print(f"  {e['path']}  world={e['world_translation']}")
            print(
                "\nIf '/World/ImportedAsset' isn't in that list, either the "
                "pipeline hasn't loaded the assembly into THIS Kit session yet "
                "(re-run ExperimentCodeComplete.py / load_remote.load_asset "
                "first), or load_remote.py used a different prim_path -- "
                "check its call to /load_asset and re-run this script with "
                "--prim-root <that path>."
            )
        except Exception as e2:
            print(f"'/World' lookup also failed: {e2}")
            print(
                "That means the Kit app itself isn't reachable at "
                f"{physics_client.KIT_HOST}:{physics_client.KIT_PORT}, or has "
                "no stage open at all -- confirm the Kit app is running and "
                "the Remote Asset Loader extension is enabled "
                "(http://localhost:8011/docs should list /list_prims)."
            )
        return []

    prim_entries = prim_result["prims"]
    meters_per_unit = prim_result.get("meters_per_unit", 1.0)
    print(f"Found {len(prim_entries)} prims under '{prim_root}' "
          f"(stage meters_per_unit={meters_per_unit}).")

    instance_names = set()
    instance_world = {}
    for m in mates:
        for p in m["parts"]:
            if p["instance_name"]:
                instance_names.add(p["instance_name"])
                if p["instance_name"] not in instance_world and p.get("world_translation") is not None:
                    instance_world[p["instance_name"]] = p["world_translation"]

    print(f"\nMatching {len(instance_names)} Onshape instance names to USD prims (by position):")
    name_to_prim = build_name_to_prim_map(instance_names, prim_entries, instance_world, meters_per_unit, debug=debug)

    unresolved = [n for n, p in name_to_prim.items() if p is None]
    if unresolved:
        print(f"\n{len(unresolved)} instance name(s) did NOT resolve automatically: {unresolved}")
        print("Those mates will be left with body path = None below -- fill them "
              "in by hand the same way joint_map.py's docstring describes.")

    no_type = [m["name"] for m in mates if not m["mate_type"]]
    if no_type:
        print(f"\n{len(no_type)} mate(s) had no mate_type resolved (see "
              f"'[mate_type NOT FOUND ...]' lines above for the raw featureData "
              f"keys -- report those back so the field name lookup can be fixed): "
              f"{no_type}")

    entries = []
    skipped = []
    for m in mates:
        if len(m["parts"]) != 2:
            skipped.append((m["name"], f"expected 2 mated parts, got {len(m['parts'])}"))
            continue

        p0, p1 = m["parts"]
        path0 = name_to_prim.get(p0["instance_name"]) if p0["instance_name"] else None
        path1 = name_to_prim.get(p1["instance_name"]) if p1["instance_name"] else None

        # Onshape reports mate connector origins in meters. Unlike the
        # world-space matching above (onshape_pts = w / meters_per_unit,
        # correct there because world positions already carry the
        # ImportedAsset Xform's 100x unit-corrective scale), local_pos0/1
        # are a joint's pivot expressed in EACH BODY'S OWN LOCAL FRAME --
        # i.e. BEFORE that same ancestor scale is applied. Dividing by the
        # stage's metersPerUnit here double-applies the correction the
        # ancestor Xform already carries, overshooting every joint pivot
        # by that same factor (confirmed live: with a 0.01 stage
        # metersPerUnit this put pivots ~100x too far from their bodies,
        # and PhysX reported "disjointed body transforms" and violently
        # snapped/ejected parts on the first sim step). The correct
        # divisor is the ASSET's own meters_per_unit (see
        # ASSET_METERS_PER_UNIT above), not the stage's -- with the
        # default of 1.0 this is a no-op, since Onshape's origins are
        # already in meters and the asset is authored 1 unit = 1 meter.
        local_pos0 = tuple(v / ASSET_METERS_PER_UNIT for v in p0["origin"])
        local_pos1 = tuple(v / ASSET_METERS_PER_UNIT for v in p1["origin"])

        # Full joint-frame orientation per side, built from that side's
        # own (x_axis, z_axis) -- see _quat_from_connector_basis. This is
        # what was missing before: only a position and a bare z_axis were
        # ever computed, so every joint's localRot0/1 silently defaulted
        # to identity. Revolute/Prismatic/Spherical joints have enough
        # rotational freedom to mostly absorb that; FIXED joints (zero
        # rotational DOF) can't, which is why only Fastened_1 was ever
        # flagged by PhysX as having "disjointed body transforms".
        local_rot0 = _quat_from_connector_basis(p0["x_axis"], p0["z_axis"])
        local_rot1 = _quat_from_connector_basis(p1["x_axis"], p1["z_axis"])

        entries.append({
            "mate_name": m["name"],
            "mate_type": m["mate_type"],
            "body0_path": path0,
            "body1_path": path1,
            "local_pos0": local_pos0,
            "local_pos1": local_pos1,
            "local_rot0": local_rot0,
            "local_rot1": local_rot1,
            # Kept for reference/debugging only -- now that local_rot0/1
            # orient the joint frame so its own Z equals this connector's
            # z_axis, create_joint_impl no longer needs to derive an axis
            # letter from this raw vector (see physics.py).
            "axis": p0["z_axis"],
        })

    print(f"\nGenerated {len(entries)} entries ({len(skipped)} skipped: {skipped}).")
    return entries


def build_output(entries: list, prim_root: str = PRIM_ROOT) -> str:
    """Render entries as the joint_map.py file contents (does not write to disk)."""
    lines = []
    lines.append('"""')
    lines.append("joint_map.py")
    lines.append("")
    lines.append("AUTO-GENERATED by generate_joint_map.py from resolved Onshape mate")
    lines.append("data (via includeMateFeatures=true) + a live match against prims in")
    lines.append("the loaded USD stage. Re-run generate_joint_map.py --write any time")
    lines.append("the CAD assembly's parts or mates change, instead of hand-editing")
    lines.append("this file directly -- your edits would be overwritten.")
    lines.append("")
    lines.append("Any entry with body0_path/body1_path still set to None didn't")
    lines.append("auto-resolve (name-matching heuristic missed it, or Onshape")
    lines.append("didn't report an instance_name for that side) -- fill those in by")
    lines.append("hand per the original manual-mapping instructions.")
    lines.append('"""')
    lines.append("")
    lines.append(f'PRIM_PREFIX = "{prim_root}/"')
    lines.append("")
    lines.append("JOINT_MAP = [")
    for e in entries:
        b0 = f'"{e["body0_path"]}"' if e["body0_path"] else "None"
        b1 = f'"{e["body1_path"]}"' if e["body1_path"] else "None"
        mtype = f'"{e["mate_type"]}"' if e["mate_type"] else "None"
        lines.append(
            f'    {{"mate_name": "{e["mate_name"]}", "mate_type": {mtype}, '
            f'"body0_path": {b0}, "body1_path": {b1}, '
            f'"local_pos0": {tuple(round(v, 6) for v in e["local_pos0"])}, '
            f'"local_pos1": {tuple(round(v, 6) for v in e["local_pos1"])}, '
            f'"local_rot0": {tuple(round(v, 6) for v in e["local_rot0"])}, '
            f'"local_rot1": {tuple(round(v, 6) for v in e["local_rot1"])}, '
            f'"axis": {tuple(round(v, 6) for v in e["axis"])}}},'
        )
    lines.append("]")
    lines.append("")
    return "\n".join(lines)


def disable_mechanism_self_collision(entries: list) -> dict:
    """
    For every part that's actually used as a joint body, disable
    collision for the SPECIFIC pairs that substantially overlap
    without a direct joint between them -- while leaving pairs that
    merely touch (legitimate mechanical stops, e.g. a pin's shoulder
    resting against a neighboring part) fully collidable.

    Deliberately restricted to the parts joints actually use (not
    "every part in the assembly"), which would also sweep in unjointed
    parts like Ball that we WANT colliding against the mechanism --
    that's the whole point of a ball resting in a catch pocket.

    An earlier version of this put every jointed part into one single
    self-filtered collision group (disable ALL self-collision at once).
    Confirmed live that was too broad: some joints here are
    CYLINDRICAL/PRISMATIC (rotate + slide along one free axis), and
    something other than that joint -- a shoulder/stop making contact
    with a DIFFERENT, non-jointed neighboring part -- is what actually
    keeps the pin from sliding out in the real assembly. Disabling all
    self-collision removed that stop too, so pins slid straight
    through with nothing left to arrest that motion. This version only
    filters pairs confirmed (by bounding-box overlap) to be genuinely
    interpenetrating, via physics_client.auto_filter_overlapping_pairs.
    """
    jointed_paths = sorted({
        p for e in entries for p in (e["body0_path"], e["body1_path"]) if p
    })
    if not jointed_paths:
        return {"ok": True, "filtered_pairs": []}

    # Pairs already handled by a direct joint's own collisionEnabled=False
    # -- skip re-checking these; a joint's own two bodies are EXPECTED
    # to overlap at their shared pivot, and that's already accounted for.
    already_exempt_pairs = [
        (e["body0_path"], e["body1_path"])
        for e in entries if e["body0_path"] and e["body1_path"]
    ]

    return physics_client.auto_filter_overlapping_pairs(jointed_paths, already_exempt_pairs)


def apply_concave_collision_overrides(part_names: list = None, prim_root: str = PRIM_ROOT,
                                       debug: bool = False) -> list:
    """
    Override collision approximation back to convexDecomposition for
    specific named parts whose concave features matter for the
    simulation (see CONCAVE_COLLISION_PARTS) -- everything else in the
    pipeline defaults to convexHull.

    Tried "sdf" instead as a potentially more accurate option for a
    shallow pocket feature, but confirmed live that's not a valid
    token in this Kit/USD schema version ("Unknown approximation
    token: 'sdf'"). Sticking with convexDecomposition + the
    voxelResolution/maxConvexHulls tuning below.

    Finds each requested part the SAME way joint matching does
    (select_part_candidates + name normalization via _group_key), so
    "ThrowingArm" matches whatever the actual prim path turns out to be
    ("ThrowingArm <1>", "ThrowingArm_1", nested or flat) rather than
    depending on a hardcoded full path.

    Returns the list of prim paths that got the override applied.
    """
    if part_names is None:
        part_names = CONCAVE_COLLISION_PARTS
    if not part_names:
        return []

    wanted_keys = {_group_key(name) for name in part_names}

    print(f"\nFetching live prim list from Kit app under '{prim_root}' "
          f"to apply concave collision overrides for {part_names}...")
    prim_result = physics_client.list_prims(root_path=prim_root)
    prim_entries = prim_result["prims"]
    part_candidates = select_part_candidates(prim_entries)

    matched = []
    for e in part_candidates:
        own_name = e["path"].rsplit("/", 1)[-1]
        if _group_key(own_name) in wanted_keys:
            physics_client.set_collision_approximation(e["path"], "convexDecomposition")
            matched.append(e["path"])

    if debug:
        found_names = {p.rsplit("/", 1)[-1] for p in matched}
        missing = wanted_keys - {_group_key(n) for n in found_names}
        if missing:
            print(f"  WARNING: requested concave-collision part(s) not found "
                  f"among live prims: {missing}")

    return matched


def find_part_prim_path_offline(part_name: str, prim_root: str = PRIM_ROOT, debug: bool = False) -> str:
    """
    Resolve a single part's name to its live prim path using ONLY
    already-live Kit prims (physics_client.list_prims +
    select_part_candidates) -- no Onshape call of any kind, and no
    dependency on onshape_metadata/File_Name credentials being
    configured at all, unlike find_part_prim_path (which cross-
    references Onshape's own instance names + world positions to
    disambiguate DUPLICATE-named parts).

    Only safe for a part name known to have exactly ONE instance in
    the assembly (e.g. "ThrowingArm", "Ball", "CenterPin" -- all
    single-instance in this mechanism). For a name with duplicate
    instances (e.g. "Base <1>"/"Base <2>"), this has no way to tell
    them apart without Onshape's position data, and just returns
    whichever match select_part_candidates lists first, with a loud
    warning -- use find_part_prim_path (Onshape-backed) instead for a
    reliable match in that case.
    """
    prim_result = physics_client.list_prims(root_path=prim_root)
    prim_entries = prim_result["prims"]
    candidates = select_part_candidates(prim_entries)
    wanted_key = _group_key(part_name)
    matches = [e["path"] for e in candidates if _group_key(e["path"].rsplit("/", 1)[-1]) == wanted_key]

    if debug:
        print(f"  find_part_prim_path_offline({part_name!r}): {len(matches)} match(es): {matches}")
    if len(matches) > 1:
        print(f"  WARNING: {len(matches)} live prims matched {part_name!r} without Onshape "
              f"disambiguation -- returning the first ({matches[0]}). If this part actually "
              f"has duplicate instances in the assembly, use find_part_prim_path (Onshape-"
              f"backed) instead for a reliable match.")
    return matches[0] if matches else None


def find_part_prim_path(part_name: str, prim_root: str = PRIM_ROOT, debug: bool = False) -> str:
    """
    Resolve a single real Onshape part instance's name (e.g. "Ball") to
    its actual live prim path, using the same onshape_metadata.
    get_instance_appearances + build_name_to_prim_map resolution
    everything else in this file relies on -- so it stays correct
    across whatever nesting depth a given STEP export happens to
    produce, rather than hardcoding a full path that would break the
    moment the CAD model or its conversion changes.

    part_name is matched via the same _group_key normalization used
    throughout this file, so "Ball" matches "Ball <1>", "Ball_1", etc.
    the same way joint/material matching already does.

    Returns the resolved prim path, or None if no live prim matched.
    """
    appearances = onshape_metadata.get_instance_appearances(DID, WID, EIDa, debug=debug)
    wanted_key = _group_key(part_name)
    matching_names = {name for name in appearances if _group_key(name) == wanted_key}
    if not matching_names:
        return None

    instance_world = {
        name: data["world_translation"]
        for name, data in appearances.items()
        if name in matching_names and data.get("world_translation") is not None
    }

    prim_result = physics_client.list_prims(root_path=prim_root)
    prim_entries = prim_result["prims"]
    meters_per_unit = prim_result.get("meters_per_unit", 1.0)

    name_to_prim = build_name_to_prim_map(matching_names, prim_entries, instance_world, meters_per_unit, debug=debug)
    resolved = [p for p in name_to_prim.values() if p]
    return resolved[0] if resolved else None


def apply_onshape_mass_properties(prim_root: str = PRIM_ROOT, debug: bool = False) -> list:
    """
    Override every real part's mass with Onshape's own computed value
    (from whatever material is actually assigned in the CAD document),
    instead of leaving PhysX to derive mass from collision-shape volume
    times a generic default density -- which knows nothing about the
    part's real material, and isn't even working from the same volume
    as Onshape's exact CAD geometry (our collision shapes are
    convexHull by default, convexDecomposition for a few overridden
    parts).

    MUST run after every part already has RigidBodyAPI -- i.e. after
    BOTH make_unjointed_parts_dynamic AND create_all_joints.main() have
    completed (see ExperimentCodeComplete.py's transfer_joints(), which
    calls this last, same ordering requirement as
    apply_concave_collision_overrides and for the same reason:
    set_mass_impl requires RigidBodyAPI to already be present on the
    target prim).

    Uses onshape_metadata.get_instance_mass_bulk() -- VERIFIED against
    a real account (confirmed massAsGroup=false gives correct
    individual per-part results, not one aggregated "-all-" body) --
    one bulk mass call per part studio instead of one
    get_part_mass_properties() call per part. Position-matching
    against live Kit prims (build_name_to_prim_map) is unchanged; only
    the Onshape-side fetch changed.

    Parts with no material assigned in Onshape report mass=None ("parts
    must have density in order to have mass" per Onshape's own docs) --
    those are left alone rather than treated as zero mass, so PhysX's
    own volume x default-density fallback still applies to them.

    Returns the list of (prim_path, mass_kg) pairs that were applied.
    """
    print(f"\nFetching Onshape mass properties for all part instances (bulk)...")
    mass_data = onshape_metadata.get_instance_mass_bulk(DID, WID, EIDa, debug=debug)
    instance_names = set(mass_data.keys())
    instance_world = {
        name: data["world_translation"]
        for name, data in mass_data.items() if data.get("world_translation") is not None
    }

    print(f"Fetching live prim list from Kit app under '{prim_root}'...")
    prim_result = physics_client.list_prims(root_path=prim_root)
    prim_entries = prim_result["prims"]
    meters_per_unit = prim_result.get("meters_per_unit", 1.0)

    name_to_prim = build_name_to_prim_map(instance_names, prim_entries, instance_world, meters_per_unit, debug=debug)

    applied = []
    no_material = []
    for name, prim_path in name_to_prim.items():
        if not prim_path:
            continue
        mass_kg = mass_data.get(name, {}).get("mass")
        if mass_kg is None:
            no_material.append(name)
            continue
        try:
            physics_client.set_mass(prim_path, mass_kg)
            applied.append((prim_path, mass_kg))
        except Exception as e:
            print(f"  WARNING: could not set mass for '{prim_path}' ({name}): {e}")

    if no_material:
        print(f"  {len(no_material)} part(s) have no Onshape material assigned, "
              f"left at PhysX's default density: {no_material}")

    print(f"\nApplied Onshape mass to {len(applied)} part(s): "
          f"{[(p, round(m, 4)) for p, m in applied]}")
    return applied


def make_unjointed_parts_dynamic(entries: list, prim_root: str = PRIM_ROOT, debug: bool = False) -> list:
    """
    Make every REAL Onshape part instance that ISN'T referenced by any
    joint into its own dynamic rigid body via physics_client.make_dynamic().

    create_all_joints.py only ever touches the body0_path/body1_path
    prims that show up in JOINT_MAP -- anything not mated to another
    part in the Onshape assembly (e.g. a free projectile like "Ball")
    never gets RigidBodyAPI applied at all, and just sits as a static,
    gravity-ignoring collider forever. This sweeps up everything joints
    didn't cover.

    CRITICAL: this must be built from a real, name-matched list of
    Onshape part instances -- NOT raw select_part_candidates() output.
    select_part_candidates() is a generic "shallowest distinctly-named
    prim" heuristic; on its own (without cross-referencing against
    actual instance names, the way build_name_to_prim_map/generate()
    do for joints) it also matches non-part prims that happen to have
    a locally-unique name: Looks folders, Shader/PreviewSurface prims,
    and -- critically -- the assembly GROUP prim itself
    ("tn__Assembly1_n9"), an ANCESTOR of every real part.

    Confirmed live: an earlier version of this function used raw
    select_part_candidates() output directly, and made 72 "unjointed
    parts" dynamic, including bare shader prims (PhysX rejected those
    outright: "RigidBodyAPI applied to a non-xformable primitive") AND
    the assembly group Xform itself, which PhysX did NOT reject -- so
    it became a valid dynamic rigid body containing every other real
    dynamic rigid body (every joint body) nested inside it. USD Physics
    does not support nested rigid bodies; this is almost certainly what
    was actually behind the mechanism "exploding"/"clipping"/"falling
    apart" across several earlier rounds of what looked like unrelated
    collision-shape and joint-orientation issues.

    Uses onshape_metadata.get_instance_appearances() (the SAME per-
    instance fetch generate_material_map.py's materials step already
    uses successfully for all 13 parts, Ball included) rather than
    generate()'s own instance_names set, which is built only from
    MATE data and would miss Ball entirely (it has no mate to appear
    in). Then reuses build_name_to_prim_map -- the exact same position-
    based matching + filtering joints already trust -- to turn those
    real instance names into real, individual part prim paths.

    Returns the list of prim paths that were made dynamic.
    """
    jointed_paths = set()
    for e in entries:
        if e["body0_path"]:
            jointed_paths.add(e["body0_path"])
        if e["body1_path"]:
            jointed_paths.add(e["body1_path"])

    print(f"\nFetching ALL Onshape part instances (not just mated ones) "
          f"to find parts with no joint...")
    appearances = onshape_metadata.get_instance_appearances(DID, WID, EIDa, debug=debug)
    instance_names = set(appearances.keys())
    instance_world = {
        name: data["world_translation"]
        for name, data in appearances.items() if data.get("world_translation") is not None
    }

    print(f"Fetching live prim list from Kit app under '{prim_root}'...")
    prim_result = physics_client.list_prims(root_path=prim_root)
    prim_entries = prim_result["prims"]
    meters_per_unit = prim_result.get("meters_per_unit", 1.0)

    name_to_prim = build_name_to_prim_map(instance_names, prim_entries, instance_world, meters_per_unit, debug=debug)
    all_real_part_paths = {p for p in name_to_prim.values() if p}

    unjointed = sorted(all_real_part_paths - jointed_paths)

    if debug:
        print(f"  {len(all_real_part_paths)} real part(s) found total, "
              f"{len(jointed_paths)} already covered by a joint, "
              f"{len(unjointed)} with no joint at all.")

    made_dynamic = []
    for path in unjointed:
        try:
            physics_client.make_dynamic(path)
            made_dynamic.append(path)
        except Exception as e:
            print(f"  WARNING: could not make '{path}' dynamic: {e}")

    print(f"\nMade {len(made_dynamic)} unjointed part(s) dynamic: {made_dynamic}")
    return made_dynamic


def run(write: bool = True, prim_root: str = PRIM_ROOT, debug: bool = False) -> list:
    """
    Programmatic entry point (used by ExperimentCodeComplete.py). Generates
    the map and, if write=True, saves it to joint_map.py so
    create_all_joints.py can import it fresh. Also:
      - makes every part NOT covered by any joint (e.g. a free "Ball")
        into its own dynamic rigid body (make_unjointed_parts_dynamic)
      - stops every JOINTED part from colliding with every OTHER
        jointed part (disable_mechanism_self_collision), since parts
        converging at a shared pivot commonly touch without a direct
        joint between that specific pair

    Deliberately does NOT apply concave-collision overrides here (see
    apply_concave_collision_overrides) -- create_all_joints.main() runs
    AFTER this, and calls _ensure_rigid_body again for every joint body
    (including any part with a concave override), which unconditionally
    resets approximation back to the convexHull default. The override
    must run AFTER create_all_joints.main(), not here -- see
    ExperimentCodeComplete.py's transfer_joints().
    """
    entries = generate(prim_root=prim_root, debug=debug)
    if entries and write:
        with open("joint_map.py", "w") as f:
            f.write(build_output(entries, prim_root=prim_root))
        print("\nWrote joint_map.py")
    if entries:
        make_unjointed_parts_dynamic(entries, prim_root=prim_root, debug=debug)
        disable_mechanism_self_collision(entries)
    return entries


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true",
                         help="Overwrite joint_map.py with the generated JOINT_MAP")
    parser.add_argument("--prim-root", default=PRIM_ROOT,
                         help=f"Root prim to search for parts (default: {PRIM_ROOT})")
    parser.add_argument("--debug", action="store_true",
                         help="Dump the raw first resolved-mate JSON and the full "
                              "live prim list, instead of guessing at their shape")
    args = parser.parse_args()

    entries = generate(prim_root=args.prim_root, debug=args.debug)
    if not entries:
        sys.exit(1)

    output = build_output(entries, prim_root=args.prim_root)
    if args.write:
        with open("joint_map.py", "w") as f:
            f.write(output)
        print("\nWrote joint_map.py")
    else:
        print("\n--- Preview (rerun with --write to save as joint_map.py) ---\n")
        print(output)


if __name__ == "__main__":
    main()
