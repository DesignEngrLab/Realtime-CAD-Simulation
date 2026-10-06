"""
create_all_materials.py

Reads material_map.py and applies each entry's Onshape appearance to the
matching prim in the live USD stage, via the Kit app's /apply_material
endpoint (physics_client.apply_material()).

Entries with color=None (no explicit Onshape appearance found) are
skipped -- left as whatever default material the STEP->USD converter
assigned, rather than stomping it with a fabricated color.

Run this AFTER your normal pipeline has loaded the assembly into the
stage (so the prim paths in material_map.py actually exist), and after
generate_material_map.py has produced material_map.py.
"""

import importlib

from physics_client import apply_material


def main():
    import material_map
    importlib.reload(material_map)  # picks up whatever generate_material_map.py just wrote

    applied = []
    skipped = []

    for entry in material_map.MATERIAL_MAP:
        if entry["color"] is None:
            skipped.append((entry["instance_name"], "no Onshape appearance set"))
            continue

        try:
            apply_material(
                entry["prim_path"],
                color=entry["color"],
                opacity=entry.get("opacity", 1.0),
            )
            applied.append(entry["instance_name"])
        except Exception as e:
            print(f"FAILED '{entry['instance_name']}': {e}")
            skipped.append((entry["instance_name"], str(e)))

    print(f"\nApplied materials to {len(applied)} part(s): {applied}")
    if skipped:
        print(f"Skipped {len(skipped)}: {skipped}")


if __name__ == "__main__":
    main()
