"""
onshape_metadata.py

Client-side (runs outside Omniverse) functions to pull two things from
Onshape needed for physics setup in Omniverse:

    1. get_assembly_mates()  -> mate list (joint type + connected parts)
    2. get_part_appearance() -> per-part color/appearance

UNVERIFIED WARNING: unlike File_Name.py and onshape_export.py (both
tested against your account), the exact JSON field names here are
based on Onshape's public API docs, not confirmed against a live
response. Test each function standalone (see bottom of file) and
inspect the raw JSON before trusting the parsed output -- field names
in mate/metadata responses are exactly the kind of thing that's
changed on us before (see the v6 -> v16 versioning surprise).
"""

import requests
from File_Name import _get_credentials, BASE_URL

API_VERSION = "v16"

# -----------------------------------------------------------------------
# WITHIN-A-RUN CACHING
# -----------------------------------------------------------------------
# get_assembly_definition() and get_instance_appearances() were each
# getting called multiple SEPARATE times per pipeline run (materials,
# joint generation, mass properties, and the Ball lookup all fetch
# appearances independently; assembly-mates and appearances each fetch
# the assembly definition too) -- all against the exact same,
# unchanged Onshape state within one pass. Caching their results here,
# at the source, fixes every call site at once with no changes needed
# in generate_material_map.py, generate_joint_map.py, or
# ExperimentCodeComplete.py.
#
# NOT a permanent/process-lifetime cache: Onshape workspaces are
# mutable, so (did, wid, eid) staying the same does NOT mean the data
# behind it hasn't changed -- ExperimentCodeComplete.py's own
# check_for_onshape_update() loop exists specifically to re-pull after
# a real edit. Call clear_cache() at the start of each fresh pass
# (ExperimentCodeComplete.py does this right after detecting an
# update, before re-running transfer_materials()/transfer_joints()) so
# a genuinely new pass doesn't serve stale data from the last one.
_assembly_definition_cache = {}
_instance_appearances_cache = {}
_instance_mass_cache = {}


def clear_cache():
    """
    Drop all cached assembly-definition / instance-appearance /
    instance-mass results. Call this at the start of any pass that
    should see FRESH Onshape data (e.g. after check_for_onshape_update()
    detects a real edit) -- without this, a second pass against the
    same (did, wid, eid) would silently keep serving data from the
    first pass forever, since the IDs alone don't change when a
    workspace's content does.
    """
    _assembly_definition_cache.clear()
    _instance_appearances_cache.clear()
    _instance_mass_cache.clear()


def _auth_headers() -> dict:
    import base64
    access_key, secret_key = _get_credentials()
    token = base64.b64encode(f"{access_key}:{secret_key}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json;charset=UTF-8;qs=0.09",
        "Content-Type": "application/json",
    }


def get_assembly_definition(did: str, wid: str, eid: str, include_mate_features: bool = True) -> dict:
    """
    Fetch the FULL assembly definition, including rootAssembly.instances,
    rootAssembly.occurrences, AND (with include_mate_features=True)
    rootAssembly.features in a RESOLVED form -- per confirmed Onshape
    forum guidance, passing includeMateFeatures=true returns mate
    features with a matedEntities array containing matedOccurrence
    (the actual occurrence path), unlike the raw BTM parameter blob
    from the plain /features endpoint we were parsing before.

    Memoized per (did, wid, eid, include_mate_features) for the
    lifetime of the current pass -- see clear_cache() above.
    """
    cache_key = (did, wid, eid, include_mate_features)
    if cache_key in _assembly_definition_cache:
        return _assembly_definition_cache[cache_key]

    url = f"{BASE_URL}/api/{API_VERSION}/assemblies/d/{did}/w/{wid}/e/{eid}"
    params = {"includeMateFeatures": "true"} if include_mate_features else {}
    resp = requests.get(url, headers=_auth_headers(), params=params, timeout=30)
    resp.raise_for_status()
    result = resp.json()

    _assembly_definition_cache[cache_key] = result
    return result


def _find_param(parameters: list, parameter_id: str):
    """Find a parameter dict by its parameterId within a BTM feature's parameter list."""
    for p in parameters:
        if p.get("parameterId") == parameter_id:
            return p
    return None


def get_assembly_mates(did: str, wid: str, eid: str, debug: bool = False) -> list:
    """
    Fetch the assembly's feature list and return only the mate features.

    CONFIRMED shape (via live test against this account): mates come back
    as full FeatureScript BTM features, not a simplified mate object.
    Relevant fields live inside feature["parameters"], keyed by
    "parameterId":
        - "mateType"            -> value is the mate type string
                                    (REVOLUTE, FASTENED, SLIDER, etc.)
        - "mateConnectorsQuery" -> "queries" list, each with a
                                    "featureId" pointing at a MATE
                                    CONNECTOR feature (not a part id
                                    directly -- see connector_feature_ids
                                    below and get_mate_connector_owner()).

    Returns:
        [{"name": ..., "mateType": ..., "connector_feature_ids": [id0, id1]}, ...]
    """
    url = f"{BASE_URL}/api/{API_VERSION}/assemblies/d/{did}/w/{wid}/e/{eid}/features"
    resp = requests.get(url, headers=_auth_headers(), timeout=30)
    resp.raise_for_status()
    data = resp.json()

    mates = []
    first_mate_dumped = False
    for feature in data.get("features", []):
        feature_data = feature.get("featureData", feature)
        feature_type = feature_data.get("featureType") or feature.get("featureType")
        if feature_type != "mate":
            continue

        if debug and not first_mate_dumped:
            import json
            print("=== RAW first mate feature (unfiltered) ===")
            print(json.dumps(feature, indent=2))
            print("=== END raw dump ===\n")
            first_mate_dumped = True

        parameters = feature_data.get("parameters", [])
        mate_type_param = _find_param(parameters, "mateType")
        connectors_param = _find_param(parameters, "mateConnectorsQuery")

        connector_feature_ids = []
        if connectors_param:
            for q in connectors_param.get("queries", []):
                fid = q.get("featureId")
                if fid:
                    connector_feature_ids.append(fid)

        mates.append({
            "name": feature_data.get("name"),
            "mateType": mate_type_param.get("value") if mate_type_param else None,
            "connector_feature_ids": connector_feature_ids,
        })
    return mates


def get_assembly_mates_resolved(did: str, wid: str, eid: str, debug: bool = False) -> list:
    """
    Fully-resolved mate list -- each mate's two mated parts (occurrence
    path + human-readable instance name + world position) AND each
    connector's local coordinate system (origin + axes), all from a
    single call to get_assembly_definition(..., include_mate_features=True).

    CONFIRMED (per Onshape's own engineering staff on their public
    forum): includeMateFeatures=true returns mate features with a
    RESOLVED matedEntities array -- each entry has "matedOccurrence"
    (occurrence path) and "matedCS" (origin + axes, in that part's own
    local space). This replaces the FeatureScript detour entirely.

    mate_type is NOT read from this resolved shape -- empirically (live
    test against this account) it doesn't come back on the resolved
    feature the way it does on the plain /features endpoint, and rather
    than keep guessing field names blind, we just pull it from
    get_assembly_mates() (confirmed working for that field) and merge
    the two by mate name.

    Also attaches each part's world_translation (meters, from
    rootAssembly.occurrences' "transform"), used downstream by
    generate_joint_map.py to disambiguate duplicate part instances
    (e.g. "Base <1>" vs "Base <2>") by POSITION rather than by naming
    convention -- Onshape's own duplicate-instance numbering has no
    guaranteed correspondence to whatever numbering the USD converter
    assigned, so matching by index is not reliable.

    Returns:
        [{
            "name": "Revolute 1",
            "mate_type": "REVOLUTE",
            "parts": [
                {"occurrence_path": [...], "instance_name": "ThrowingArm",
                 "origin": (x, y, z), "z_axis": (x, y, z), "x_axis": (x, y, z),
                 "world_translation": (x, y, z) or None},
                ...
            ],
        }, ...]
    """
    assembly_def = get_assembly_definition(did, wid, eid, include_mate_features=True)
    root = assembly_def.get("rootAssembly", {})

    instances_by_id = {inst.get("id"): inst for inst in root.get("instances", [])}

    def resolve_name(occurrence_path):
        if not occurrence_path:
            return None
        inst = instances_by_id.get(occurrence_path[-1])
        return inst.get("name") if inst else None

    # occurrence path -> world translation (meters). CONFIRMED via raw
    # debug dump: Onshape's "transform" is a 16-element ROW-MAJOR 4x4
    # homogeneous matrix (translation.x/y/z sit at indices [3, 7, 11] --
    # the last column of each row -- verified by matching a real
    # occurrence's transform against its already-known USD world
    # position). NOT column-major -- [12, 13, 14] is the bottom affine
    # row, which is always (0, 0, 0), which is exactly why every
    # occurrence was silently resolving to the origin before this fix.
    world_translation_by_path = {}
    if debug:
        import json
        print(f"=== RAW first 3 rootAssembly.occurrences entries "
              f"(of {len(root.get('occurrences', []))} total) ===")
        print(json.dumps(root.get("occurrences", [])[:3], indent=2))
        print("=== END raw occurrences dump ===\n")

    skipped_no_transform = 0
    for occ in root.get("occurrences", []):
        path = tuple(occ.get("path", []))
        transform = occ.get("transform")
        if transform and len(transform) >= 12:
            world_translation_by_path[path] = (transform[3], transform[7], transform[11])
        else:
            skipped_no_transform += 1

    if debug:
        print(f"Parsed {len(world_translation_by_path)} occurrence world "
              f"positions out of {len(root.get('occurrences', []))} occurrences "
              f"({skipped_no_transform} had no usable 'transform').")
        # Confirm matedOccurrence paths from mates actually show up as keys
        # here -- if this prints all False, the path tuples don't match
        # (wrong id, wrong nesting, or occurrences uses a different key
        # than "path" for the same concept) and that's the real bug.
        sample_paths = []
        for feature in root.get("features", []):
            fd = feature.get("featureData", feature)
            if (fd.get("featureType") or feature.get("featureType")) != "mate":
                continue
            for me in fd.get("matedEntities", []):
                sample_paths.append(tuple(me.get("matedOccurrence", [])))
            if len(sample_paths) >= 4:
                break
        print("Sample matedOccurrence path lookups against occurrences map:")
        for p in sample_paths:
            print(f"  {p} -> {'FOUND: ' + str(world_translation_by_path[p]) if p in world_translation_by_path else 'NOT FOUND'}")
        print()

    mates = []
    first_dumped = False
    for feature in root.get("features", []):
        feature_data = feature.get("featureData", feature)
        feature_type = feature_data.get("featureType") or feature.get("featureType")
        if feature_type != "mate":
            continue

        if debug and not first_dumped:
            import json
            print("=== RAW first resolved mate feature (includeMateFeatures=true) ===")
            print(json.dumps(feature, indent=2))
            print("=== END raw dump ===\n")
            first_dumped = True

        parts = []
        for me in feature_data.get("matedEntities", []):
            occ_path = me.get("matedOccurrence", [])
            cs = me.get("matedCS", {}) or {}

            if "xAxis" not in cs or "zAxis" not in cs:
                # Silently falling back to (1,0,0)/(0,0,1) here produces an
                # IDENTITY joint-frame rotation downstream (see
                # generate_joint_map.py's _quat_from_connector_basis) --
                # indistinguishable from "this mate genuinely has no
                # rotation", which is how a real missing-field problem
                # went unnoticed through several rounds of debugging. Flag
                # it loudly instead so it's obvious in the console.
                print(f"  WARNING: matedCS for mate {feature_data.get('name')!r} "
                      f"(occurrence {occ_path}) missing 'xAxis' and/or 'zAxis' -- "
                      f"keys actually present in matedCS: {sorted(cs.keys())!r}. "
                      f"Falling back to identity axes, which will silently produce "
                      f"an identity local_rot for this side of the joint.")

            parts.append({
                "occurrence_path": occ_path,
                "instance_name": resolve_name(occ_path),
                "origin": tuple(cs.get("origin", [0.0, 0.0, 0.0])),
                "z_axis": tuple(cs.get("zAxis", [0.0, 0.0, 1.0])),
                # matedCS carries a full local coordinate system (origin +
                # xAxis + yAxis + zAxis, all in the part's own local
                # space), not just an origin+zAxis pair -- xAxis was
                # previously read but never captured here. Without it,
                # downstream code (generate_joint_map.py) had no way to
                # build a joint's full orientation, only its position and
                # one axis direction. That's tolerable for single-axis
                # joint types (Revolute/Prismatic/Spherical still have
                # rotational freedom to absorb an unset orientation) but
                # not for FIXED joints (zero rotational DOF) -- PhysX
                # reported those as "disjointed body transforms" and
                # violently snapped them together on the first sim step.
                "x_axis": tuple(cs.get("xAxis", [1.0, 0.0, 0.0])),
                "world_translation": world_translation_by_path.get(tuple(occ_path)),
            })

        mates.append({
            "name": feature_data.get("name"),
            "mate_type": None,  # filled in below from get_assembly_mates()
            "parts": parts,
        })

    # mate_type: pull from the plain /features endpoint (CONFIRMED working
    # for this field) and merge by mate name, rather than guessing where
    # it lives in the resolved shape.
    raw_mates = get_assembly_mates(did, wid, eid)
    type_by_name = {m["name"]: m["mateType"] for m in raw_mates if m.get("name")}
    for m in mates:
        m["mate_type"] = type_by_name.get(m["name"])
        if m["mate_type"] is None:
            print(f"  WARNING: no mate_type found for '{m['name']}' even via "
                  f"get_assembly_mates() -- check that mate names match "
                  f"exactly between the two endpoints.")

    return mates


def get_part_appearance(did: str, wid: str, eid: str, part_id: str) -> dict:
    """
    Fetch a part's appearance (color) and material assignment, if any.

    Returns a dict like:
        {"color": (r, g, b), "opacity": 1.0, "material_name": "Aluminum, 6061" or None}

    r/g/b are floats 0.0-1.0. If Onshape hasn't set an explicit
    appearance/material, values may be None -- caller should fall back
    to a default material in that case.
    """
    url = f"{BASE_URL}/api/{API_VERSION}/metadata/d/{did}/w/{wid}/e/{eid}/p/{part_id}"
    resp = requests.get(url, headers=_auth_headers(), timeout=30)
    resp.raise_for_status()
    data = resp.json()

    color = None
    opacity = 1.0
    material_name = None

    for prop in data.get("properties", []):
        name = prop.get("name")
        value = prop.get("value")

        if name == "Appearance" and isinstance(value, dict):
            c = value.get("color", value)  # some responses nest under "color", some don't
            if isinstance(c, dict) and "red" in c:
                color = (c.get("red", 255) / 255.0, c.get("green", 255) / 255.0, c.get("blue", 255) / 255.0)
            opacity = value.get("opacity", opacity)

        if name == "Material" and isinstance(value, dict):
            material_name = value.get("displayName") or value.get("name")

    return {"color": color, "opacity": opacity, "material_name": material_name}


def get_part_mass_properties(did: str, wid: str, eid: str, part_id: str, debug: bool = False) -> dict:
    """
    Fetch a part's real, computed mass properties from Onshape -- based
    on whatever material is actually assigned to that part in the CAD
    document, not a name-to-density lookup table we'd have to maintain
    and keep accurate ourselves.

    UNVERIFIED WARNING (matching this file's established pattern for
    endpoints not yet confirmed against a live response): the exact
    response shape for /massproperties isn't confirmed here -- Onshape
    docs and forum posts describe properties that "come in sets of
    three" (value, upper tolerance, lower tolerance), so this defensively
    unwraps a list to its first element, but run with debug=True and
    inspect the raw response before trusting this on a new document.

    Mass is returned in KILOGRAMS -- Onshape's API responds in SI units
    regardless of the document's display units. This is only exact if
    the destination USD stage's kilogramsPerUnit is left at its default
    of 1.0 (as metersPerUnit needed explicit handling elsewhere in this
    pipeline -- see generate_joint_map.py's ASSET_METERS_PER_UNIT
    comments -- kilogramsPerUnit deserves the same scrutiny if this
    stage ever sets it to something else).

    Returns {"mass": float kg or None, "volume": float m^3 or None}.
    A part with no material assigned typically has no mass ("parts
    must have density in order to have mass" per Onshape's own docs) --
    caller should fall back to PhysX's own default density in that case
    rather than treating None as zero.
    """
    url = f"{BASE_URL}/api/{API_VERSION}/parts/d/{did}/w/{wid}/e/{eid}/partid/{part_id}/massproperties"
    resp = requests.get(url, headers=_auth_headers(), timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if debug:
        import json as _json
        print(f"=== RAW massproperties response for partId={part_id} ===")
        print(_json.dumps(data, indent=2))
        print("=== END raw dump ===\n")

    def _first(value):
        if isinstance(value, list):
            return value[0] if value else None
        return value

    # Response may be scoped by body (bodies: {"": {...}} or keyed by
    # partId) or flat at the top level, depending on endpoint/version --
    # check both shapes defensively.
    props = data
    bodies = data.get("bodies")
    if isinstance(bodies, dict) and bodies:
        props = next(iter(bodies.values()))

    mass = _first(props.get("mass"))
    volume = _first(props.get("volume"))
    has_mass = props.get("hasMass", mass is not None)

    return {"mass": float(mass) if has_mass and mass is not None else None,
            "volume": float(volume) if volume is not None else None}


# -----------------------------------------------------------------------
# BULK / ELEMENT-LEVEL FETCHES -- EXPERIMENTAL, NOT YET VERIFIED
# -----------------------------------------------------------------------
# get_part_appearance() and get_part_mass_properties() above are each
# called ONCE PER UNIQUE PART -- for an assembly with N unique parts,
# that's 2N calls just for materials/mass. Onshape exposes both of
# these at a BULK level instead:
#   - GET /api/{v}/metadata/d/{did}/w/{wid}/e/{eid}          (no /p/{partId})
#     returns metadata for EVERY part in that element in ONE call.
#   - GET /api/{v}/partstudios/d/{did}/w/{wid}/e/{eid}/massproperties
#     (no partid path segment) is documented as returning mass
#     properties "of a part studio OR PARTS" -- i.e. all parts in that
#     part studio in ONE call.
#
# UNVERIFIED WARNING (stronger than the usual caveat on this file's
# other endpoints): these two functions' PARSING is a best-effort
# extension of get_part_appearance()/get_part_mass_properties()'s own
# confirmed per-part parsing to a multi-part response shape -- it has
# NOT been checked against a real response from your account. Call
# either with debug=True FIRST and visually compare the raw dump
# against what's parsed out below before trusting these for real, the
# same way this file's own bottom __main__ block already recommends
# for every other endpoint here.
#
# NOT wired into get_instance_appearances() or any pipeline call site
# -- these are available to call directly (or via
# get_instance_appearances_bulk() below) once you've verified them,
# but the existing, already-working per-part path is untouched and
# stays the default.
#
# Also note: this only collapses calls per UNIQUE ELEMENT ID (part
# studio), not per assembly. If your parts are spread across multiple
# part studios, you still get one bulk call PER part studio involved
# -- still far fewer than one call per PART, just not down to exactly
# 2 total unless everything lives in a single part studio.

def get_bulk_element_metadata(did: str, wid: str, eid: str, debug: bool = False) -> dict:
    """
    EXPERIMENTAL -- see module-level warning above. Fetch appearance/
    material metadata for EVERY part under element `eid` in ONE call.

    Returns {part_id: {"color": (r,g,b) or None, "opacity": float,
                        "material_name": str or None}, ...}
    """
    url = f"{BASE_URL}/api/{API_VERSION}/metadata/d/{did}/w/{wid}/e/{eid}"
    resp = requests.get(url, headers=_auth_headers(), timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if debug:
        import json as _json
        print(f"=== RAW bulk element metadata response for eid={eid} ===")
        print(_json.dumps(data, indent=2))
        print("=== END raw dump -- compare this against the parsing below before trusting it ===\n")

    result = {}
    for part in data.get("parts", []):
        part_id = part.get("partId")
        if not part_id:
            continue
        color = None
        opacity = 1.0
        material_name = None
        for prop in part.get("properties", []):
            name = prop.get("name")
            value = prop.get("value")
            if name == "Appearance" and isinstance(value, dict):
                c = value.get("color", value)
                if isinstance(c, dict) and "red" in c:
                    color = (c.get("red", 255) / 255.0, c.get("green", 255) / 255.0, c.get("blue", 255) / 255.0)
                opacity = value.get("opacity", opacity)
            if name == "Material" and isinstance(value, dict):
                material_name = value.get("displayName") or value.get("name")
        result[part_id] = {"color": color, "opacity": opacity, "material_name": material_name}

    if debug and not result:
        print(f"  WARNING: parsed ZERO parts out of the bulk metadata response for eid={eid} -- "
              f"the response shape likely doesn't match this parsing. Check the raw dump above "
              f"and adjust the `parts` / `properties` field names accordingly.")
    return result


def get_bulk_mass_properties(did: str, wid: str, eid: str, part_ids: list, debug: bool = False) -> dict:
    """
    EXPERIMENTAL -- see module-level warning above. Fetch mass
    properties for the given parts in part studio `eid` in ONE call,
    instead of one get_part_mass_properties() call per part.

    part_ids is REQUIRED, not optional -- confirmed against a real
    response: calling this endpoint with no partIds filter returns a
    SINGLE aggregated body keyed "-all-" (combined mass/volume for the
    entire part studio at once), not one body per part. Individual
    per-part results only come back when specific part IDs are passed
    via the partIds query parameter.

    Returns {part_id: {"mass": float kg or None, "volume": float m^3 or None}, ...}
    """
    url = f"{BASE_URL}/api/{API_VERSION}/partstudios/d/{did}/w/{wid}/e/{eid}/massproperties"
    # CONFIRMED WRONG, then fixed: the parameter is `partId` (singular),
    # repeated once per part -- e.g. ?partId=JHD&partId=JaD&partId=JoD
    # -- NOT a single `partIds` key with a comma-joined value. requests
    # repeats a query key automatically when given a list as its value.
    # massAsGroup=false is required (confirmed via testing) to get
    # INDIVIDUAL per-part bodies back -- without it, the response
    # aggregates all requested parts into one combined "-all-" body
    # even with specific partId filters applied, which is useless for
    # per-part mass assignment.
    params = {"partId": part_ids, "massAsGroup": "false"}
    resp = requests.get(url, headers=_auth_headers(), params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if debug:
        import json as _json
        print(f"=== RAW bulk massproperties response for eid={eid}, partId={part_ids} ===")
        print(_json.dumps(data, indent=2))
        print("=== END raw dump -- compare this against the parsing below before trusting it ===\n")

    def _first(value):
        if isinstance(value, list):
            return value[0] if value else None
        return value

    result = {}
    bodies = data.get("bodies", {})
    if isinstance(bodies, dict):
        for part_id, props in bodies.items():
            if not part_id or part_id == "-all-":
                continue
            mass = _first(props.get("mass"))
            volume = _first(props.get("volume"))
            has_mass = props.get("hasMass", mass is not None)
            result[part_id] = {"mass": float(mass) if has_mass and mass is not None else None,
                                "volume": float(volume) if volume is not None else None}

    if debug and not result:
        print(f"  WARNING: parsed ZERO parts out of the bulk massproperties response for eid={eid} -- "
              f"the response shape likely doesn't match this parsing (e.g. 'bodies' may not be "
              f"keyed by partId the way this assumes). Check the raw dump above.")
    return result


def get_instance_appearances_bulk(did: str, wid: str, eid: str, debug: bool = False) -> dict:
    """
    EXPERIMENTAL drop-in alternative to get_instance_appearances() --
    same return shape, same caching behavior, but fetches appearance/
    mass data via get_bulk_element_metadata()/get_bulk_mass_properties()
    (grouped by each part's owning element/part-studio id) instead of
    one call per unique part. Verify with debug=True before switching
    any call site over from get_instance_appearances() to this.

    NOT automatically used anywhere in this pipeline yet -- switch a
    call site over explicitly (e.g. in generate_material_map.py or
    generate_joint_map.py) once you've confirmed the bulk parsing
    above is correct for your account.
    """
    cache_key = (did, wid, eid)
    if cache_key in _instance_appearances_cache:
        return _instance_appearances_cache[cache_key]

    assembly_def = get_assembly_definition(did, wid, eid, include_mate_features=False)
    root = assembly_def.get("rootAssembly", {})
    instances = root.get("instances", [])

    world_translation_by_path = {}
    for occ in root.get("occurrences", []):
        path = tuple(occ.get("path", []))
        transform = occ.get("transform")
        if transform and len(transform) >= 12:
            world_translation_by_path[path] = (transform[3], transform[7], transform[11])

    # Group instances by their OWNING element id -- parts can come
    # from different part studios within the same document, and each
    # bulk call only covers one element id at a time.
    part_ids_by_element = {}
    for inst in instances:
        if inst.get("type") != "Part":
            continue
        element_id = inst.get("elementId")
        part_id = inst.get("partId")
        if element_id and part_id:
            part_ids_by_element.setdefault(element_id, set()).add(part_id)

    appearance_by_element_part = {}
    mass_by_element_part = {}
    for element_id, part_id_set in part_ids_by_element.items():
        appearance_by_element_part[element_id] = get_bulk_element_metadata(did, wid, element_id, debug=debug)
        mass_by_element_part[element_id] = get_bulk_mass_properties(
            did, wid, element_id, list(part_id_set), debug=debug)

    result = {}
    for inst in instances:
        if inst.get("type") != "Part":
            continue
        name = inst.get("name")
        part_id = inst.get("partId")
        element_id = inst.get("elementId")
        inst_id = inst.get("id")
        if not name or not part_id or not element_id:
            continue

        appearance = appearance_by_element_part.get(element_id, {}).get(
            part_id, {"color": None, "opacity": 1.0, "material_name": None})
        mass_props = mass_by_element_part.get(element_id, {}).get(
            part_id, {"mass": None, "volume": None})

        result[name] = {
            **appearance,
            **mass_props,
            "world_translation": world_translation_by_path.get((inst_id,)),
        }

    _instance_appearances_cache[cache_key] = result
    return result


def get_instance_mass_bulk(did: str, wid: str, eid: str, debug: bool = False) -> dict:
    """
    VERIFIED against a real account (get_bulk_mass_properties with
    massAsGroup=false confirmed correct). Lean, MASS-ONLY alternative
    to get_instance_appearances_bulk() -- skips
    get_bulk_element_metadata() (appearance/material) entirely, since
    generate_joint_map.apply_onshape_mass_properties() only ever reads
    the "mass" key. One get_bulk_mass_properties() call per unique
    part studio (element id) involved, instead of one
    get_part_mass_properties() call per PART -- for this assembly
    (13 instances, 1 part studio), that's 1 call instead of 8.

    Returns {instance_name: {"mass": float kg or None,
                              "volume": float m^3 or None,
                              "world_translation": (x,y,z) or None}}
    -- same shape as get_instance_appearances() minus color/opacity/
    material_name, so callers that only ever read "mass"/
    "world_translation" (like apply_onshape_mass_properties) can
    switch over with no other code changes needed.
    """
    cache_key = (did, wid, eid)
    if cache_key in _instance_mass_cache:
        return _instance_mass_cache[cache_key]

    assembly_def = get_assembly_definition(did, wid, eid, include_mate_features=False)
    root = assembly_def.get("rootAssembly", {})
    instances = root.get("instances", [])

    world_translation_by_path = {}
    for occ in root.get("occurrences", []):
        path = tuple(occ.get("path", []))
        transform = occ.get("transform")
        if transform and len(transform) >= 12:
            world_translation_by_path[path] = (transform[3], transform[7], transform[11])

    # Group by owning part studio -- one bulk call per group, not per part.
    part_ids_by_element = {}
    for inst in instances:
        if inst.get("type") != "Part":
            continue
        element_id = inst.get("elementId")
        part_id = inst.get("partId")
        if element_id and part_id:
            part_ids_by_element.setdefault(element_id, set()).add(part_id)

    mass_by_element_part = {}
    for element_id, part_id_set in part_ids_by_element.items():
        mass_by_element_part[element_id] = get_bulk_mass_properties(
            did, wid, element_id, list(part_id_set), debug=debug)

    result = {}
    for inst in instances:
        if inst.get("type") != "Part":
            continue
        name = inst.get("name")
        part_id = inst.get("partId")
        element_id = inst.get("elementId")
        inst_id = inst.get("id")
        if not name or not part_id or not element_id:
            continue

        mass_props = mass_by_element_part.get(element_id, {}).get(
            part_id, {"mass": None, "volume": None})

        result[name] = {
            **mass_props,
            "world_translation": world_translation_by_path.get((inst_id,)),
        }

    _instance_mass_cache[cache_key] = result
    return result


def get_instance_appearances(did: str, wid: str, eid: str, debug: bool = False) -> dict:
    """
    Resolve appearance (color/opacity/material) + world position for every
    PART-type instance in the assembly, in one pass. This is the material
    counterpart to get_assembly_mates_resolved() -- same assembly-definition
    call, same row-major transform parsing for world position, just reading
    instances instead of mate features.

    UNVERIFIED WARNING: like get_part_appearance(), the instance fields
    relied on here (instance["type"] == "Part", instance["partId"],
    instance["elementId"]) are based on Onshape's public API docs, not
    confirmed against a live response -- run with debug=True and inspect
    the raw instance dump before trusting this. Sub-assembly instances
    (type != "Part") are skipped for now; their parts would need to be
    walked recursively, which isn't implemented here.

    Instances that share the same underlying part (same elementId +
    partId -- i.e. duplicate instances of one part) reuse a single
    get_part_appearance() call rather than hitting the API once per
    duplicate.

    Returns:
        {instance_name: {"color": (r,g,b) or None, "opacity": float,
                          "material_name": str or None,
                          "mass": float kg or None,
                          "volume": float m^3 or None,
                          "world_translation": (x,y,z) or None}}

    Memoized per (did, wid, eid) for the lifetime of the current pass
    -- see clear_cache() at the top of this file. This is what
    actually eliminates most of the redundant traffic: without this,
    materials, joint generation, mass-property application, and any
    find_part_prim_path() lookup each independently re-fetch the
    assembly definition AND every part's appearance/mass from
    scratch, even though all four happen back-to-back against the
    exact same unchanged Onshape state.
    """
    cache_key = (did, wid, eid)
    if cache_key in _instance_appearances_cache:
        return _instance_appearances_cache[cache_key]

    assembly_def = get_assembly_definition(did, wid, eid, include_mate_features=False)
    root = assembly_def.get("rootAssembly", {})
    instances = root.get("instances", [])

    if debug:
        import json
        print("=== RAW first 3 rootAssembly.instances entries ===")
        print(json.dumps(instances[:3], indent=2))
        print("=== END raw dump ===\n")

    # occurrence path -> world translation (meters). Same row-major
    # parsing as get_assembly_mates_resolved() above -- see the comment
    # there for why it's [3, 7, 11] and not [12, 13, 14]. Root-level
    # instances have a one-element occurrence path, [instance_id].
    world_translation_by_path = {}
    for occ in root.get("occurrences", []):
        path = tuple(occ.get("path", []))
        transform = occ.get("transform")
        if transform and len(transform) >= 12:
            world_translation_by_path[path] = (transform[3], transform[7], transform[11])

    result = {}
    skipped_subassembly = 0
    appearance_cache = {}  # (elementId, partId) -> appearance dict or None, avoids duplicate calls
    mass_cache = {}  # (elementId, partId) -> mass properties dict or None, same reasoning
    for inst in instances:
        if inst.get("type") != "Part":
            skipped_subassembly += 1
            continue

        name = inst.get("name")
        part_id = inst.get("partId")
        element_id = inst.get("elementId")
        inst_id = inst.get("id")

        if not name or not part_id or not element_id:
            print(f"  WARNING: instance {inst!r} missing name/partId/elementId, skipping")
            continue

        key = (element_id, part_id)
        if key not in appearance_cache:
            try:
                appearance_cache[key] = get_part_appearance(did, wid, element_id, part_id)
            except Exception as e:
                print(f"  WARNING: appearance lookup failed for {name!r} ({key}): {e}")
                appearance_cache[key] = None
        appearance = appearance_cache[key] or {"color": None, "opacity": 1.0, "material_name": None}

        if key not in mass_cache:
            try:
                mass_cache[key] = get_part_mass_properties(did, wid, element_id, part_id, debug=debug)
            except Exception as e:
                print(f"  WARNING: mass properties lookup failed for {name!r} ({key}): {e}")
                mass_cache[key] = None
        mass_props = mass_cache[key] or {"mass": None, "volume": None}

        result[name] = {
            **appearance,
            **mass_props,
            "world_translation": world_translation_by_path.get((inst_id,)),
        }

    if debug and skipped_subassembly:
        print(f"Skipped {skipped_subassembly} sub-assembly instance(s) "
              f"(not type=='Part' -- recursive walk not implemented).")

    _instance_appearances_cache[cache_key] = result
    return result


if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 4:
        print("Usage: python onshape_metadata.py <did> <wid> <eid> [part_id]")
        sys.exit(1)

    did_arg, wid_arg, eid_arg = sys.argv[1], sys.argv[2], sys.argv[3]

    print("=== Raw mates ===")
    mates = get_assembly_mates(did_arg, wid_arg, eid_arg, debug=True)
    print(json.dumps(mates, indent=2))

    # Debug: check whether the connector_feature_ids from the first mate
    # resolve to OTHER entries in the same features list (named Mate
    # Connector features), or point somewhere we can't see from here.
    if mates and mates[0]["connector_feature_ids"]:
        print("\n=== Checking if connector featureIds resolve within THIS ASSEMBLY's feature list (any type) ===")
        url = f"{BASE_URL}/api/{API_VERSION}/assemblies/d/{did_arg}/w/{wid_arg}/e/{eid_arg}/features"
        resp = requests.get(url, headers=_auth_headers(), timeout=30)
        all_features = resp.json().get("features", [])

        # Show every distinct featureType present, in case connectors
        # are a different type we were filtering out (not just "mate")
        types_seen = set()
        for f in all_features:
            fd = f.get("featureData", f)
            types_seen.add(fd.get("featureType") or f.get("featureType"))
        print(f"Feature types present in assembly's own /features: {types_seen}")

        all_ids = {f.get("featureId"): f for f in all_features}
        for fid in mates[0]["connector_feature_ids"]:
            match = all_ids.get(fid)
            if match:
                print(f"FOUND {fid} in assembly's own features list:")
                print(json.dumps(match, indent=2))
            else:
                print(f"NOT FOUND in assembly features: {fid}")

    # Reconnaissance: dump the full assembly definition's instances and
    # occurrences, to check whether occurrence-level disambiguation
    # info is available here (needed for full auto-resolution of which
    # specific part instance -- e.g. Base vs Base_1 -- each mate uses).
    print("\n=== Assembly definition: instances ===")
    assembly_def = get_assembly_definition(did_arg, wid_arg, eid_arg)
    root = assembly_def.get("rootAssembly", {})
    instances = root.get("instances", [])
    print(json.dumps(instances, indent=2))

    print("\n=== Assembly definition: occurrences (first 3) ===")
    print(json.dumps(root.get("occurrences", [])[:3], indent=2))

    # NEW: check the SOURCE PART STUDIO's own /features list for our
    # connector_feature_ids -- all instances share elementId
    # "045a083a25ff8005c749039f" per the instances dump, so mate
    # connectors may be defined there as named features, distinct from
    # both the assembly's mate list and the assembly's own feature list.
    if instances and mates and mates[0]["connector_feature_ids"]:
        part_studio_eid = instances[0]["elementId"]
        print(f"\n=== Checking PART STUDIO ({part_studio_eid}) features for connector IDs ===")
        ps_url = f"{BASE_URL}/api/{API_VERSION}/partstudios/d/{did_arg}/w/{wid_arg}/e/{part_studio_eid}/features"
        ps_resp = requests.get(ps_url, headers=_auth_headers(), timeout=30)
        if ps_resp.status_code != 200:
            print(f"Part studio features call failed: {ps_resp.status_code} {ps_resp.text}")
        else:
            ps_features = ps_resp.json().get("features", [])
            ps_types_seen = set()
            for f in ps_features:
                fd = f.get("featureData", f)
                ps_types_seen.add(fd.get("featureType") or f.get("featureType"))
            print(f"Feature types present in part studio's /features: {ps_types_seen}")

            ps_ids = {f.get("featureId"): f for f in ps_features}
            for fid in mates[0]["connector_feature_ids"]:
                match = ps_ids.get(fid)
                if match:
                    print(f"FOUND {fid} in PART STUDIO features list:")
                    print(json.dumps(match, indent=2))
                else:
                    print(f"NOT FOUND in part studio features: {fid}")

    # NEW: test the includeMateFeatures=true resolution path directly --
    # this is the one that should actually work, per the forum guidance.
    print("\n=== Resolved mates via includeMateFeatures=true (NEW approach) ===")
    resolved = get_assembly_mates_resolved(did_arg, wid_arg, eid_arg, debug=True)
    if not resolved:
        print("No mate features found. If mates[] above WAS non-empty, "
              "that means rootAssembly['features'] from the assembly-definition "
              "endpoint doesn't include mates the same way the /features "
              "endpoint does -- worth comparing the two raw feature lists.")
    for m in resolved:
        print(f"\n{m['name']} ({m['mate_type']}):")
        for i, p in enumerate(m["parts"]):
            print(f"  part{i}: instance_name={p['instance_name']!r} "
                  f"occurrence_path={p['occurrence_path']} "
                  f"origin={p['origin']} z_axis={p['z_axis']}")
