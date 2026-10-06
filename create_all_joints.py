"""
create_all_joints.py

Reads joint_map.py and creates a physics joint (via the Kit app's
/create_joint endpoint) for every entry that has both body0_path and
body1_path filled in. Entries still set to None are skipped and
reported at the end, so you can see what's left to fill in.

Also finds any part under PRIM_PREFIX that ISN'T referenced by any
joint at all (e.g. a free-standing "Ball" that isn't connected to
anything by a mate) and gives it real physics (RigidBodyAPI + proper
collision) via /make_dynamic, so it isn't left as inert static
geometry -- see make_orphan_parts_dynamic() below.

Run this AFTER your normal pipeline has loaded the assembly into the
stage (so the prim paths in joint_map.py actually exist).
"""

import importlib

import physics_client
from physics_client import create_joint, MATE_TYPE_MAP


def find_orphan_parts(prim_entries: list, known_body_paths: set) -> list:
    """
    Find sibling prims of the already-known joint body paths that
    AREN'T themselves a joint body -- e.g. a free-standing "Ball" that
    sits alongside "ThrowingArm"/"CrossBar1"/etc under the same
    assembly-group parent, but has no mate connecting it to anything.

    Deliberately NOT reusing select_part_candidates() here: that
    function picks one candidate per DISTINCT NAME anywhere in the
    tree, which (correctly, for name-matching -- unused groups are
    just ignored there) also lets through the assembly-group prim
    itself and redundant generic "Mesh" leaves nested inside an
    already-selected part. Calling /make_dynamic on those would be
    wrong -- it would try to make the whole assembly group a rigid body
    right alongside its own already-dynamic children, or double up a
    single part's RigidBodyAPI on both its outer prim and an internal
    Mesh descendant that's already covered by that outer prim's own
    _ensure_rigid_body pass.

    Sibling-based lookup sidesteps that ambiguity entirely: every known
    joint body already correctly identifies ONE "part-level" prim path,
    so any OTHER direct child of that same parent is, by construction,
    also a part-level prim -- regardless of what's nested beneath it.
    """
    if not known_body_paths:
        return []
    parent_paths = {p.rsplit("/", 1)[0] for p in known_body_paths}
    prim_paths = {e["path"] for e in prim_entries}
    orphans = []
    for parent_path in parent_paths:
        prefix = parent_path + "/"
        for path in prim_paths:
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix):]
            if "/" in rest:
                continue  # not a DIRECT child of that parent
            if path in known_body_paths:
                continue
            orphans.append(path)
    return orphans


def make_orphan_parts_dynamic(joint_map_module) -> list:
    """
    Give RigidBodyAPI + proper convex-decomposition collision to any
    part under joint_map.PRIM_PREFIX that isn't body0_path/body1_path
    of any entry in JOINT_MAP. RigidBodyAPI only ever gets applied
    inside physics.create_joint_impl (per-body, via _ensure_rigid_body)
    -- a part with no mate at all, like a free ball meant to sit in a
    catch pocket and get thrown/caught, is never touched by that, so it
    just sits as static geometry with collision but nothing ever moves
    it. This finds those and calls /make_dynamic on each.
    """
    joint_body_paths = set()
    for entry in joint_map_module.JOINT_MAP:
        if entry["body0_path"]:
            joint_body_paths.add(entry["body0_path"])
        if entry["body1_path"]:
            joint_body_paths.add(entry["body1_path"])

    prim_root = joint_map_module.PRIM_PREFIX.rstrip("/")
    prim_result = physics_client.list_prims(root_path=prim_root)
    orphan_paths = find_orphan_parts(prim_result["prims"], joint_body_paths)

    made_dynamic = []
    failed = []
    for path in orphan_paths:
        result = physics_client.make_dynamic(path)
        if result.get("ok"):
            made_dynamic.append(path)
        else:
            failed.append((path, result.get("error")))

    print(f"\nMade {len(made_dynamic)} orphan part(s) dynamic (no joint, given RigidBodyAPI + collision directly): {made_dynamic}")
    if failed:
        print(f"Failed to make {len(failed)} orphan part(s) dynamic: {failed}")
    return made_dynamic


def main():
    import joint_map
    importlib.reload(joint_map)  # picks up whatever generate_joint_map.py just wrote

    created = []
    skipped = []

    for entry in joint_map.JOINT_MAP:
        if not entry["body0_path"] or not entry["body1_path"]:
            skipped.append(entry["mate_name"])
            continue

        joint_type = MATE_TYPE_MAP.get(entry["mate_type"])
        if joint_type is None:
            print(f"Skipping '{entry['mate_name']}': unrecognized mate_type {entry['mate_type']!r}")
            skipped.append(entry["mate_name"])
            continue

        try:
            create_joint(
                joint_type,
                entry["body0_path"],
                entry["body1_path"],
                # NOTE: previously these two weren't passed at all, so
                # every joint silently defaulted to (0,0,0) -- i.e. the
                # joint frame was placed at each body's origin instead
                # of the actual Onshape mate connector location. Now
                # sourced from joint_map.py, which generate_joint_map.py
                # fills in from each mate's matedCS origin.
                local_pos0=entry.get("local_pos0", (0, 0, 0)),
                local_pos1=entry.get("local_pos1", (0, 0, 0)),
                # Same story as local_pos0/1 above, one level further:
                # without these the joint frame's ORIENTATION silently
                # defaulted to identity (each body's own local axes),
                # which is only correct by coincidence. Revolute/
                # Prismatic/Spherical joints have rotational freedom to
                # mostly absorb that; FIXED joints don't, and PhysX
                # reports "disjointed body transforms" and can snap/
                # eject the bodies on the first sim step. .get(...) with
                # an identity default here is just a safety net for a
                # joint_map.py written before generate_joint_map.py
                # started emitting these -- a fresh --write run always
                # includes real values.
                local_rot0=entry.get("local_rot0", (1.0, 0.0, 0.0, 0.0)),
                local_rot1=entry.get("local_rot1", (1.0, 0.0, 0.0, 0.0)),
                axis=entry.get("axis", (0, 0, 1)),
                joint_name=entry["mate_name"].replace(" ", "_"),
                # The ORIGINAL Onshape mate type ("PLANAR"/"CYLINDRICAL"/
                # etc, before MATE_TYPE_MAP collapses it to "D6") --
                # without this, a D6 joint gets created with every one
                # of its 6 degrees of freedom left free, constraining
                # nothing at all. See physics._configure_d6_limits.
                onshape_mate_type=entry["mate_type"],
            )
            created.append(entry["mate_name"])
        except Exception as e:
            print(f"FAILED '{entry['mate_name']}': {e}")
            skipped.append(entry["mate_name"])

    print(f"\nCreated {len(created)} joints: {created}")
    if skipped:
        print(f"Skipped {len(skipped)} (missing body paths or errors): {skipped}")

    make_orphan_parts_dynamic(joint_map)


if __name__ == "__main__":
    main()
