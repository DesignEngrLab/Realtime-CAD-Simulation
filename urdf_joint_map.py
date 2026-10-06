"""
urdf_joint_map.py

Drop-in replacement for generate_joint_map.generate() that reads the joints
from the Onshape URDF export instead of the Onshape mates API. Geometry
still comes from your existing STEP->USD asset -- NOTHING is converted to
USD here. Output is the same joint_map.py create_all_joints.py already reads.

Frames
------
The old pipeline gave /create_joint each mate connector in the PART's own
local frame (Onshape matedCS = part-studio coordinates), because that's the
local frame of each part prim in the STEP->USD asset. The URDF carries the
same frame: the OBJ meshes are in part-studio coordinates and each link's
<visual><origin> maps them into the link, so

    part frame (world) = link frame (world) @ visual origin

Every joint frame is computed in URDF world at the zero configuration (the
assembled pose Onshape exported) and then expressed relative to each part
frame -- i.e. exactly what matedCS used to supply. Units are meters, same as
the old pipeline (ASSET_METERS_PER_UNIT = 1.0).

Matching URDF parts to live prims
---------------------------------
Reuses generate_joint_map.build_name_to_prim_map unchanged: group by
normalized part name ("Base", "Base_1", "tn__Base_1_k9" -> "base"), then
disambiguate duplicates by world position. The URDF part-frame origin plays
the role Onshape's occurrence transform used to. The pairing is unaffected
by any overall shift (e.g. align_to_ground), since it minimizes TOTAL
squared distance within each group.

Mates
-----
Onshape's URDF export chains 1-DOF joints through massless dummy links
(cylindrical = prismatic + continuous, planar = prismatic + prismatic +
continuous) and closes kinematic loops with "<part>__<n>__loop_closure"
dummy links. Both are collapsed back to single Onshape-style mates; see
extract_mates() and _resolve_loop_closures().

Usage (Kit running, asset already loaded):
    python urdf_joint_map.py              # preview only
    python urdf_joint_map.py --write      # also write joint_map.py
"""

import argparse
import glob
import importlib.util
import math
import os
import re
import xml.etree.ElementTree as ET

import physics_client
import generate_joint_map  # only for its local name/position matching helpers

URDF_FOLDER = r"E:\Onshape test\extracted\assembly_1\urdf"
# Optional -- only used to read OBJ vertex positions for loop-closure
# assignment. If missing, falls back to center-of-mass distance.
MESH_FOLDER = r"E:\Onshape test\extracted\assembly_1\meshes"

PRIM_ROOT = generate_joint_map.PRIM_ROOT
JOINT_MAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "joint_map.py")

# Exporter artifact: an unmated part (the Ball) becomes the URDF root and
# the rest of the assembly is welded to it. Skipped, so the Ball stays free
# and create_all_joints.make_orphan_parts_dynamic() handles it as before.
SKIP_JOINT_PATTERNS = [r"^hanging_node"]

# PLANAR/CYLINDRICAL mates in this assembly sit at pin locations; in CAD the
# pin-in-hole contact does the holding, but in PhysX jointed bodies don't
# collide, so bars slide in-plane through their pins (PLANAR) or off the
# pin (CYLINDRICAL). REVOLUTE about the mate axis models the pin. Set to {}
# to get Onshape's original mate types exactly.
MATE_TYPE_OVERRIDES = {"PLANAR": "REVOLUTE", "CYLINDRICAL": "REVOLUTE"}
# Per-mate exceptions by URDF joint name, e.g. {"planar_10": "PLANAR"}.
PER_MATE_OVERRIDES = {}

# Which axis is "up" in the Kit stage the asset is built for.
#   "Z": parts are placed in Onshape's own coordinates, X/Y/Z identical to
#        Onshape (Z up). Kit's stage must be Z-up with gravity along -Z.
#   "Y": the standard Z-up -> Y-up mapping (Onshape X -> X, Y -> -Z,
#        Z -> Y); the default for Kit's Y-up stage. Kept as the module
#        default so older scripts (rerun_local_test_urdf.py) are unchanged;
#        ExperimentCodeComplete.py sets this itself.
STAGE_UP_AXIS = "Y"

# Rotation from the URDF's world frame to Onshape's assembly frame
# (3x3, row-major), or None to detect it. Onshape's exporter doesn't write
# the URDF in Onshape's own frame -- for assembly_1 it's turned -90 deg about
# X (Onshape Z along URDF +Y) because the root link (the Ball) sets the
# frame. Detection: parts inserted without rotation in Onshape all share the
# SAME orientation in the URDF -- that shared orientation is the
# URDF-vs-Onshape rotation itself. (10 of 13 parts agree in assembly_1.)
# Set this explicitly if an assembly has most of its parts rotated.
ONSHAPE_FRAME_ROTATION = None

LOOP_CLOSURE_RE = re.compile(r"^(?P<base>.+?)__\d+__loop_closure$")


# --------------------------------------------------------------------------
# 4x4 math (column vectors: p_world = M @ p_local)
# --------------------------------------------------------------------------
def _eye():
    return [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]


def _mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _inv(m):
    out = _eye()
    for i in range(3):
        for j in range(3):
            out[i][j] = m[j][i]
        out[i][3] = -sum(m[k][i] * m[k][3] for k in range(3))
    return out


def _rot(m, v):
    return tuple(sum(m[i][k] * v[k] for k in range(3)) for i in range(3))


def _pt(m, p):
    return tuple(sum(m[i][k] * p[k] for k in range(3)) + m[i][3] for i in range(3))


def _origin(elem):
    m = _eye()
    if elem is None:
        return m
    x, y, z = (float(v) for v in elem.get("xyz", "0 0 0").split())
    r, p, w = (float(v) for v in elem.get("rpy", "0 0 0").split())
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(w), math.sin(w)
    R = [[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
         [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
         [-sp, cp * sr, cp * cr]]
    for i in range(3):
        m[i][:3] = R[i]
    m[0][3], m[1][3], m[2][3] = x, y, z
    return m


def _norm(v):
    n = math.sqrt(sum(c * c for c in v))
    return tuple(c / n for c in v) if n > 1e-12 else tuple(v)


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _basis_with_z(z, x_hint=None):
    z = _norm(z)
    for h in ([x_hint] if x_hint else []) + [(1, 0, 0), (0, 1, 0), (0, 0, 1)]:
        h = _norm(h)
        if abs(_dot(h, z)) < 0.9:
            y = _norm(_cross(z, h))
            x = _norm(_cross(y, z))
            return [[x[i], y[i], z[i]] for i in range(3)]
    raise ValueError("degenerate axis")


def _quat(m4):
    q = generate_joint_map._quat_from_matrix([row[:3] for row in m4[:3]])
    n = math.sqrt(sum(c * c for c in q))
    q = tuple(c / n for c in q)
    return q if q[0] >= 0 else tuple(-c for c in q)


# --------------------------------------------------------------------------
# URDF
# --------------------------------------------------------------------------
def find_urdf_file(folder=URDF_FOLDER):
    if os.path.isfile(folder):
        return folder
    found = sorted(glob.glob(os.path.join(folder, "*.urdf")))
    if not found:
        raise FileNotFoundError(f"No .urdf file in {folder!r}")
    if len(found) > 1:
        print(f"WARNING: multiple .urdf files in {folder!r}, using {found[0]}")
    return found[0]


class UrdfAssembly:
    def __init__(self, urdf_path, mesh_folder=MESH_FOLDER):
        root = ET.parse(urdf_path).getroot()
        self.urdf_path = urdf_path
        self.links, self.joints = {}, []
        for l in root.findall("link"):
            vis = l.find("visual")
            mesh = vis.find("geometry/mesh") if vis is not None else None
            ine = l.find("inertial")
            col = vis.find("material/color") if vis is not None else None
            self.links[l.get("name")] = {
                "vis_origin": _origin(vis.find("origin")) if mesh is not None else None,
                "mesh": mesh.get("filename") if mesh is not None else None,
                "mesh_scale": tuple(float(v) for v in mesh.get("scale", "1 1 1").split()) if mesh is not None else None,
                "rgba": tuple(float(v) for v in col.get("rgba").split()) if col is not None else None,
                "mass": float(ine.find("mass").get("value")) if ine is not None and ine.find("mass") is not None else None,
                "com": tuple(_origin(ine.find("origin"))[i][3] for i in range(3)) if ine is not None else None,
            }
        for j in root.findall("joint"):
            a = j.find("axis")
            self.joints.append({
                "name": j.get("name"), "type": j.get("type"),
                "parent": j.find("parent").get("link"), "child": j.find("child").get("link"),
                "origin": _origin(j.find("origin")),
                "axis": _norm(tuple(float(v) for v in a.get("xyz").split())) if a is not None else (1, 0, 0),
            })
        self.by_child = {j["child"]: j for j in self.joints}
        self.by_parent = {}
        for j in self.joints:
            self.by_parent.setdefault(j["parent"], []).append(j)

        fk = {n: self._fk(n) for n in self.links}
        self.parts = [n for n, l in self.links.items() if l["mesh"]]
        to_stage = _mul(self._onshape_to_stage(), self._urdf_to_onshape(fk))
        self.world = {n: _mul(to_stage, m) for n, m in fk.items()}
        # Part-studio frame of each part in world = what the USD prim's local frame is.
        self.part_world = {n: _mul(self.world[n], self.links[n]["vis_origin"]) for n in self.parts}
        self.instance_name = self._instance_names()
        self.mesh_folder = mesh_folder
        self.loop_owner = self._resolve_loop_closures()

    def _urdf_to_onshape(self, fk):
        if ONSHAPE_FRAME_ROTATION is not None:
            R = ONSHAPE_FRAME_ROTATION
        else:
            counts = {}
            for n in self.parts:
                m = _mul(fk[n], self.links[n]["vis_origin"])
                key = tuple(tuple(int(round(m[i][j])) for j in range(3)) for i in range(3))
                if all(abs(m[i][j] - key[i][j]) < 1e-4 for i in range(3) for j in range(3)):
                    counts[key] = counts.get(key, 0) + 1
            ranked = sorted(counts.items(), key=lambda kv: -kv[1])
            if not ranked or ranked[0][1] < 2 or (len(ranked) > 1 and ranked[1][1] == ranked[0][1]):
                print("  WARNING: couldn't detect Onshape's frame from part orientations -- "
                      "using the URDF frame as-is. Set urdf_joint_map.ONSHAPE_FRAME_ROTATION.")
                return _eye()
            shared, count = ranked[0]
            R = [[shared[j][i] for j in range(3)] for i in range(3)]  # transpose = inverse
            if count != getattr(UrdfAssembly, "_last_frame_note", None):
                print(f"  Onshape frame: {count} of {len(self.parts)} parts share one orientation "
                      f"in the URDF -> URDF-to-Onshape rotation {tuple(tuple(r) for r in R)}")
                UrdfAssembly._last_frame_note = count
        m = _eye()
        for i in range(3):
            m[i][:3] = [float(v) for v in R[i]]
        return m

    @staticmethod
    def _onshape_to_stage():
        if STAGE_UP_AXIS == "Z":
            return _eye()
        if STAGE_UP_AXIS == "Y":  # Rx(-90): Onshape +Z -> +Y, +Y -> -Z
            m = _eye()
            m[1][:3], m[2][:3] = [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]
            return m
        raise ValueError(f"STAGE_UP_AXIS must be 'Y' or 'Z', not {STAGE_UP_AXIS!r}")

    def _fk(self, name):
        j = self.by_child.get(name)
        return _eye() if j is None else _mul(self._fk(j["parent"]), j["origin"])

    def _instance_names(self):
        """Mesh stem + _1, _2 for repeats ("root" -> "Ball"). Only needs to
        normalize via _group_key to the same key as the USD prim names --
        the index itself is ignored (duplicates are matched by position)."""
        counts, out = {}, {}
        for n in self.parts:
            stem = os.path.splitext(os.path.basename(self.links[n]["mesh"]))[0]
            k = counts.get(stem, 0)
            out[n] = stem if k == 0 else f"{stem}_{k}"
            counts[stem] = k + 1
        return out

    def mesh_path(self, n):
        """Resolve a link's mesh file: MESH_FOLDER/<file name> first, then
        "package://<pkg>/<rest>" relative to the folder above urdf/."""
        fn = self.links[n]["mesh"]
        tried = [os.path.join(self.mesh_folder or "", os.path.basename(fn))]
        if fn.startswith("package://"):
            rest = fn[len("package://"):].split("/", 1)[-1]
            tried.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(self.urdf_path))),
                                      *rest.split("/")))
        elif not fn.startswith("file://"):
            tried.append(os.path.join(os.path.dirname(os.path.abspath(self.urdf_path)), fn))
        for p in tried:
            if os.path.isfile(p):
                return p
        return None

    def _world_aabb(self, n):
        path = self.mesh_path(n)
        if not path:
            return None
        lo, hi = [math.inf] * 3, [-math.inf] * 3
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("v "):
                    p = _pt(self.part_world[n], tuple(float(v) for v in line.split()[1:4]))
                    for a in range(3):
                        lo[a], hi[a] = min(lo[a], p[a]), max(hi[a], p[a])
        return lo, hi

    def _resolve_loop_closures(self):
        """"base__2__loop_closure" -> which real "base*" link? The number is
        an enumeration, not an instance index, so pick the candidate whose
        mesh (world bounding box) the dummy frame sits on; fall back to
        nearest center of mass if the OBJ isn't available."""
        owners, aabb_cache = {}, {}
        for name, link in self.links.items():
            m = LOOP_CLOSURE_RE.match(name)
            if not m or link["mesh"]:
                continue
            base = m.group("base")
            cands = [n for n in self.parts if n == base or re.fullmatch(re.escape(base) + r"_\d+", n)]
            if not cands:
                print(f"  WARNING: loop-closure link {name!r} matches no part named like {base!r}")
                continue
            p = tuple(self.world[name][i][3] for i in range(3))

            def score(c):
                if c not in aabb_cache:
                    aabb_cache[c] = self._world_aabb(c)
                box = aabb_cache[c]
                d_box = (math.sqrt(sum(max(0.0, box[0][a] - p[a], p[a] - box[1][a]) ** 2 for a in range(3)))
                         if box else math.inf)
                com = self.links[c]["com"]
                d_com = math.dist(_pt(self.world[c], com), p) if com else math.inf
                return (round(d_box, 4), d_com)

            owners[name] = min(cands, key=score)
        return owners

    def _classify(self, chain):
        sig = "".join("R" if j["type"] in ("revolute", "continuous") else
                      "P" if j["type"] == "prismatic" else
                      "F" if j["type"] == "fixed" else "?" for j in chain)
        ax = [j["axis"] for j in chain]
        par = lambda a, b: abs(abs(_dot(a, b)) - 1) < 1e-3
        perp = lambda a, b: abs(_dot(a, b)) < 1e-3
        if sig == "F":
            return "FASTENED", None, None
        if sig == "R":
            return "REVOLUTE", 0, None
        if sig == "P":
            return "SLIDER", 0, None
        if sig == "PR" and par(ax[0], ax[1]):
            return "CYLINDRICAL", 1, None
        if sig == "RP" and par(ax[0], ax[1]):
            return "CYLINDRICAL", 0, None
        if sig == "PPR" and perp(ax[0], ax[2]) and perp(ax[1], ax[2]):
            return "PLANAR", 2, 0
        if sig == "RRR":
            return "BALL", None, None
        return None

    def extract_mates(self):
        mates = []
        for start in self.joints:
            if start["parent"] not in self.parts:
                continue
            if any(re.search(p, start["name"]) for p in SKIP_JOINT_PATTERNS):
                print(f"  Skipping exporter-artifact joint {start['name']!r}")
                continue
            chain, cur, ok = [start], start, True
            while not self.links[cur["child"]]["mesh"] and cur["child"] not in self.loop_owner:
                nxt = self.by_parent.get(cur["child"], [])
                if len(nxt) != 1:
                    print(f"  WARNING: chain from {start['name']!r} branches at {cur['child']!r} -- skipped")
                    ok = False
                    break
                cur = nxt[0]
                chain.append(cur)
            if not ok:
                continue
            child = self.loop_owner.get(chain[-1]["child"], chain[-1]["child"])
            if child not in self.parts:
                print(f"  WARNING: {start['name']!r} ends at non-part {child!r} -- skipped")
                continue
            cls = self._classify(chain)
            if cls is None:
                print(f"  WARNING: {[j['name'] for j in chain]} ({[j['type'] for j in chain]}) "
                      f"isn't a recognized mate -- skipped")
                continue
            urdf_type, z_idx, x_idx = cls
            mate_type = PER_MATE_OVERRIDES.get(start["name"]) or MATE_TYPE_OVERRIDES.get(urdf_type) or urdf_type

            # Joint frame in world, Z = mate axis (rotation axis / plane normal / slide axis).
            parent = start["parent"]
            w, frames = self.world[parent], []
            for j in chain:
                w = _mul(w, j["origin"])
                frames.append(w)
            wj = frames[-1]
            to_j = lambda i: _norm(_rot(_inv(wj), _rot(frames[i], chain[i]["axis"])))
            align = _basis_with_z(to_j(z_idx), to_j(x_idx) if x_idx is not None else None) \
                if z_idx is not None else [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
            joint_world = _eye()
            for r in range(3):
                for c in range(3):
                    joint_world[r][c] = sum(wj[r][k] * align[k][c] for k in range(3))
                joint_world[r][3] = wj[r][3]

            l0 = _mul(_inv(self.part_world[parent]), joint_world)
            l1 = _mul(_inv(self.part_world[child]), joint_world)
            mates.append({
                "mate_name": start["name"], "mate_type": mate_type, "urdf_mate_type": urdf_type,
                "link0": parent, "link1": child, "urdf_chain": [j["name"] for j in chain],
                "local_pos0": tuple(l0[i][3] / generate_joint_map.ASSET_METERS_PER_UNIT for i in range(3)),
                "local_pos1": tuple(l1[i][3] / generate_joint_map.ASSET_METERS_PER_UNIT for i in range(3)),
                "local_rot0": _quat(l0), "local_rot1": _quat(l1),
                "axis": (0.0, 0.0, 1.0),
            })
        return mates


# --------------------------------------------------------------------------
# Pipeline entry points
# --------------------------------------------------------------------------
def generate(urdf_path=None, prim_root=PRIM_ROOT, debug=False):
    urdf_path = urdf_path or find_urdf_file()
    print(f"Reading joints from URDF: {urdf_path}")
    asm = UrdfAssembly(urdf_path)
    mates = asm.extract_mates()

    prim_result = physics_client.list_prims(root_path=prim_root)
    mpu = prim_result.get("meters_per_unit", 1.0)
    names = {asm.instance_name[n] for n in asm.parts}
    world = {asm.instance_name[n]: tuple(asm.part_world[n][i][3] for i in range(3)) for n in asm.parts}
    print(f"\nMatching {len(names)} URDF parts to live prims under '{prim_root}' (by name + position):")
    name_to_prim = generate_joint_map.build_name_to_prim_map(names, prim_result["prims"], world, mpu, debug=debug)
    for n in asm.parts:
        print(f"  {n:16s} ({asm.instance_name[n]:14s}) -> {name_to_prim.get(asm.instance_name[n])}")
    if asm.loop_owner:
        print("Loop closures:", {k: v for k, v in sorted(asm.loop_owner.items())})

    entries = []
    for m in mates:
        e = dict(m)
        e["body0_path"] = name_to_prim.get(asm.instance_name[m["link0"]])
        e["body1_path"] = name_to_prim.get(asm.instance_name[m["link1"]])
        entries.append(e)

    print(f"\n{len(entries)} mates:")
    for e in entries:
        t = e["mate_type"] if e["mate_type"] == e["urdf_mate_type"] else f"{e['urdf_mate_type']}->{e['mate_type']}"
        print(f"  {t:22s} {e['mate_name']:26s} {e['body0_path']}  <->  {e['body1_path']}")
    missing = [e["mate_name"] for e in entries if not e["body0_path"] or not e["body1_path"]]
    if missing:
        print(f"\nWARNING: {len(missing)} mate(s) have an unresolved body path: {missing}")
    return entries


def build_output(entries, prim_root=PRIM_ROOT, source=""):
    r = lambda t: tuple(round(v, 6) for v in t)
    lines = ['"""', "joint_map.py", "",
             f"AUTO-GENERATED by urdf_joint_map.py from {source}.",
             "Re-run `python urdf_joint_map.py --write` instead of hand-editing.", '"""', "",
             f'PRIM_PREFIX = "{prim_root.rstrip("/")}/"', "", "JOINT_MAP = ["]
    for e in entries:
        b0 = f'"{e["body0_path"]}"' if e["body0_path"] else "None"
        b1 = f'"{e["body1_path"]}"' if e["body1_path"] else "None"
        lines.append(
            f'    {{"mate_name": "{e["mate_name"]}", "mate_type": "{e["mate_type"]}", '
            f'"body0_path": {b0}, "body1_path": {b1}, '
            f'"local_pos0": {r(e["local_pos0"])}, "local_pos1": {r(e["local_pos1"])}, '
            f'"local_rot0": {r(e["local_rot0"])}, "local_rot1": {r(e["local_rot1"])}, '
            f'"axis": {r(e["axis"])}}},')
    lines += ["]", ""]
    return "\n".join(lines)


def run(write=True, urdf_path=None, prim_root=PRIM_ROOT, debug=False):
    """generate() + write joint_map.py. Also deletes the cached .pyc so
    create_all_joints' importlib.reload can't pick up a stale copy written
    within the same second."""
    urdf_path = urdf_path or find_urdf_file()
    entries = generate(urdf_path, prim_root, debug)
    if write:
        with open(JOINT_MAP_PATH, "w", encoding="utf-8") as f:
            f.write(build_output(entries, prim_root, os.path.basename(urdf_path)))
        try:
            os.remove(importlib.util.cache_from_source(JOINT_MAP_PATH))
        except OSError:
            pass
        print(f"\nWrote {JOINT_MAP_PATH}")
    return entries


def main():
    ap = argparse.ArgumentParser(description="Generate joint_map.py from an Onshape URDF export.")
    ap.add_argument("--write", action="store_true", help="write joint_map.py (default: preview only)")
    ap.add_argument("--urdf", default=URDF_FOLDER, help="URDF file or folder")
    ap.add_argument("--prim-root", default=PRIM_ROOT)
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    run(write=a.write, urdf_path=find_urdf_file(a.urdf), prim_root=a.prim_root, debug=a.debug)


if __name__ == "__main__":
    main()
