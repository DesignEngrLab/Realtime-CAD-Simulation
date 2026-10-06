"""
urdf_obj_asset.py

Builds the assembly Kit loads from the Onshape URDF + OBJ/MTL export alone --
replaces the STEP download and STEP->USD conversion entirely.

Kit's stage is USD, and /load_asset takes a USD file path, so the OBJs are
wrapped in one small .usda text file (standard library only; no pxr, no
converter, no Python 3.12 subprocess). Nothing is reinterpreted:

    - every OBJ's vertices are copied AS-IS (they're already in Onshape
      part-studio coordinates, the same local frame the STEP parts had)
    - each part prim's transform is where the URDF places that part:
      link world pose @ <visual><origin>, zero joint configuration
    - colors come from the .mtl (falling back to the URDF <color>)

Because each part prim's local frame IS its part-studio frame, the joint
frames from urdf_joint_map.py line up with it exactly, the same way Onshape
matedCS lined up with the STEP parts.

Prim names = mesh file stem, _1/_2... for repeats ("Base", "Base_1",
"ThrowingArm", "Ball"), so generate_joint_map's name matching,
find_part_prim_path_offline("Ball"/"ThrowingArm") and
create_all_joints.find_orphan_parts all work unchanged.

Each part becomes ONE Mesh per material: Onshape writes every CAD face as a
separate OBJ object (many are two-triangle flat patches), and a collider
built per flat patch would be useless, so they're merged per part.

CLI (no Kit needed):
    python urdf_obj_asset.py                          # uses URDF_FOLDER/MESH_FOLDER
    python urdf_obj_asset.py --urdf <file|folder> --out C:\\tmp\\test.usda
"""

import argparse
import os
import re

import urdf_joint_map

# Unjointed parts whose prim origin is moved to the center of their mesh
# (geometry shifted to match, so nothing moves visually or physically).
# Onshape's part-studio origin can sit anywhere relative to the part; for
# the Ball that would make its reported position swing around as it tumbles
# in flight, skewing the measured throw distance. Only safe for parts with
# NO joints -- joint frames are expressed relative to the part-studio origin.
RECENTER_PART_NAMES = ["Ball"]


# --------------------------------------------------------------------------
# OBJ / MTL
# --------------------------------------------------------------------------
def parse_mtl(path):
    mats, cur = {}, None
    if not path or not os.path.isfile(path):
        return mats
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.split()
            if not p:
                continue
            if p[0] == "newmtl":
                cur = " ".join(p[1:])
                mats[cur] = {"Kd": (0.8, 0.8, 0.8), "d": 1.0}
            elif cur and p[0] == "Kd" and len(p) >= 4:
                mats[cur]["Kd"] = tuple(float(v) for v in p[1:4])
            elif cur and p[0] == "d" and len(p) >= 2:
                mats[cur]["d"] = float(p[1])
            elif cur and p[0] == "Tr" and len(p) >= 2:
                mats[cur]["d"] = 1.0 - float(p[1])
    return mats


def parse_obj(path):
    """-> {"v", "vn", "groups": {material: [face, ...]}, "mtl"}; each face is
    a list of (v_index, vn_index_or_None), 0-based."""
    v, vn, groups, mtl_files, cur = [], [], {}, [], None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.split()
            if not p or p[0].startswith("#"):
                continue
            if p[0] == "v":
                v.append((float(p[1]), float(p[2]), float(p[3])))
            elif p[0] == "vn":
                vn.append((float(p[1]), float(p[2]), float(p[3])))
            elif p[0] == "f":
                face = []
                for tok in p[1:]:
                    b = tok.split("/")
                    vi = int(b[0])
                    vi = vi - 1 if vi > 0 else len(v) + vi
                    ni = None
                    if len(b) >= 3 and b[2]:
                        ni = int(b[2])
                        ni = ni - 1 if ni > 0 else len(vn) + ni
                    face.append((vi, ni))
                if len(face) >= 3:
                    groups.setdefault(cur, []).append(face)
            elif p[0] == "usemtl":
                cur = " ".join(p[1:])
            elif p[0] == "mtllib":
                mtl_files.append(" ".join(p[1:]))
    mtl = {}
    for name in mtl_files:
        mtl.update(parse_mtl(os.path.join(os.path.dirname(path), name)))
    return {"v": v, "vn": vn, "groups": groups, "mtl": mtl}


def _mesh_subsets(obj, scale, fallback_rgba):
    sx, sy, sz = scale
    out = []
    for mat_name, faces in obj["groups"].items():
        remap, pts, counts, idx, normals = {}, [], [], [], []
        has_n = bool(obj["vn"]) and all(ni is not None for fc in faces for _, ni in fc)
        for fc in faces:
            counts.append(len(fc))
            for vi, ni in fc:
                if vi not in remap:
                    x, y, z = obj["v"][vi]
                    remap[vi] = len(pts)
                    pts.append((x * sx, y * sy, z * sz))
                idx.append(remap[vi])
                if has_n:
                    normals.append(obj["vn"][ni])
        mat = obj["mtl"].get(mat_name) if mat_name else None
        if mat:
            color, opacity = mat["Kd"], mat["d"]
        elif fallback_rgba:
            color, opacity = fallback_rgba[:3], fallback_rgba[3]
        else:
            color, opacity = (0.8, 0.8, 0.8), 1.0
        out.append({"points": pts, "counts": counts, "indices": idx,
                    "normals": normals if has_n else None, "color": tuple(color), "opacity": opacity})
    return out


# --------------------------------------------------------------------------
# USDA writer
# --------------------------------------------------------------------------
def _sanitize(name):
    s = re.sub(r"[^A-Za-z0-9_]", "_", name)
    return s if s and not s[0].isdigit() else f"_{s}"


def _f(x):
    return f"{x:.9g}"


def _vecs(vals):
    return "[" + ", ".join("(" + ", ".join(_f(c) for c in v) + ")" for v in vals) + "]"


def _usd_matrix(m):
    # USD matrices are row-vector (translation in the last row) = M transposed.
    return "( " + ", ".join("(" + ", ".join(_f(m[r][c]) for r in range(4)) + ")" for c in range(4)) + " )"


def build_asset(urdf_path, out_path, mesh_folder=None):
    """Write the .usda for this URDF. Returns (out_path, UrdfAssembly)."""
    asm = urdf_joint_map.UrdfAssembly(urdf_path, mesh_folder or urdf_joint_map.MESH_FOLDER)
    root = _sanitize(os.path.splitext(os.path.basename(urdf_path))[0])
    L = ["#usda 1.0", "(", f'    defaultPrim = "{root}"', "    metersPerUnit = 1",
         "    kilogramsPerUnit = 1", f'    upAxis = "{urdf_joint_map.STAGE_UP_AXIS}"',
         f'    doc = "Built by urdf_obj_asset.py from {os.path.basename(urdf_path)} + OBJ meshes"',
         ")", "", f'def Xform "{root}"', "{"]
    obj_cache = {}
    for n in asm.parts:
        link = asm.links[n]
        path = asm.mesh_path(n)
        if not path:
            raise FileNotFoundError(f"Mesh {link['mesh']!r} for URDF link {n!r} not found "
                                    f"(looked in {asm.mesh_folder!r} and next to the URDF).")
        if path not in obj_cache:
            obj_cache[path] = parse_obj(path)
        subsets = _mesh_subsets(obj_cache[path], link["mesh_scale"] or (1, 1, 1), link["rgba"])
        name = _sanitize(asm.instance_name[n])
        xform = asm.part_world[n]
        if name in RECENTER_PART_NAMES:
            pts_all = [p for s in subsets for p in s["points"]]
            c = tuple((min(p[a] for p in pts_all) + max(p[a] for p in pts_all)) / 2 for a in range(3))
            for s in subsets:
                s["points"] = [(p[0] - c[0], p[1] - c[1], p[2] - c[2]) for p in s["points"]]
            shift = urdf_joint_map._eye()
            shift[0][3], shift[1][3], shift[2][3] = c
            xform = urdf_joint_map._mul(xform, shift)
        L += [f'    def Xform "{name}"', "    {",
              f'        custom string urdf:link = "{n}"',
              f"        matrix4d xformOp:transform = {_usd_matrix(xform)}",
              '        uniform token[] xformOpOrder = ["xformOp:transform"]', "",
              '        def Scope "Looks"', "        {"]
        for i, s in enumerate(subsets):
            mp = f"/{root}/{name}/Looks/Mat_{i}"
            L += [f'            def Material "Mat_{i}"', "            {",
                  f"                token outputs:surface.connect = <{mp}/Shader.outputs:surface>",
                  '                def Shader "Shader"', "                {",
                  '                    uniform token info:id = "UsdPreviewSurface"',
                  f"                    color3f inputs:diffuseColor = ({', '.join(_f(c) for c in s['color'])})",
                  f"                    float inputs:opacity = {_f(s['opacity'])}",
                  "                    float inputs:roughness = 0.5",
                  "                    float inputs:metallic = 0",
                  "                    token outputs:surface", "                }", "            }"]
        L.append("        }")
        for i, s in enumerate(subsets):
            mesh = "Geom" if len(subsets) == 1 else f"Geom_{i}"
            L += ["", f'        def Mesh "{mesh}" (', '            prepend apiSchemas = ["MaterialBindingAPI"]',
                  "        )", "        {",
                  f"            int[] faceVertexCounts = [{', '.join(map(str, s['counts']))}]",
                  f"            int[] faceVertexIndices = [{', '.join(map(str, s['indices']))}]",
                  f"            point3f[] points = {_vecs(s['points'])}"]
            if s["normals"]:
                L += [f"            normal3f[] normals = {_vecs(s['normals'])} (",
                      '                interpolation = "faceVarying"', "            )"]
            L += [f"            color3f[] primvars:displayColor = [({', '.join(_f(c) for c in s['color'])})]",
                  f"            float[] primvars:displayOpacity = [{_f(s['opacity'])}]",
                  '            uniform token subdivisionScheme = "none"',
                  f"            rel material:binding = </{root}/{name}/Looks/Mat_{i}>", "        }"]
        L += ["    }", ""]
    L += ["}", ""]
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"Built {out_path}: {len(asm.parts)} parts "
          f"({', '.join(asm.instance_name[n] for n in asm.parts)})")
    return out_path, asm


def main():
    ap = argparse.ArgumentParser(description="Build the Kit asset from a URDF + OBJ export.")
    ap.add_argument("--urdf", default=urdf_joint_map.URDF_FOLDER, help="URDF file or folder")
    ap.add_argument("--mesh-dir", default=urdf_joint_map.MESH_FOLDER)
    ap.add_argument("--out", help="output .usda (default: next to the URDF)")
    a = ap.parse_args()
    urdf = urdf_joint_map.find_urdf_file(a.urdf)
    build_asset(urdf, a.out or os.path.splitext(urdf)[0] + ".usda", a.mesh_dir)


if __name__ == "__main__":
    main()
