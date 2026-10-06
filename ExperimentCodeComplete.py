#Combine several subprograms to be able to run full experiment
#
# CAD source: Onshape URDF export (+ OBJ/MTL meshes) -- no STEP anywhere.
# On start and on every debounced webhook change:
#   onshape_urdf_export  -> download the assembly as URDF + OBJ (1 export call)
#   urdf_obj_asset       -> wrap the OBJs in a .usda for /load_asset
#   urdf_joint_map       -> joint_map.py from the URDF (no mates API calls)
#   create_all_joints    -> joints in Kit (unchanged)

import os
import sys

# BASE LOCATIONS for scripts and where it will upload data. Update for current PC
base_loc_script = r"C:/Users/cmoss/RealTimeCADSim/Realtime-CAD-Simulation"                  
base_loc_data = r"F:/RealTimeSimData"    

if base_loc_script not in sys.path:                  
    sys.path.insert(0, base_loc_script)


# -----------------------------------------------------------------------
# ITERATION COUNT
# -----------------------------------------------------------------------
# Max number of iterations (URDF re-pulls) for this run, AFTER the
# original model. Before each one a popup shows "Iteration X of
# MAX_ITERATIONS" -- press OK to pull the current CAD from Onshape and
# re-simulate, or Cancel to end the run early.
MAX_ITERATIONS = 10

# Set to 0 for no delay between participant pressing ok and when it starts pulling file from Onshape.
PULL_DELAY_SECONDS = 5.0

# Make sure the sibling scripts below are importable from
# base_loc_script no matter what directory this is launched from.
if base_loc_script not in sys.path:
    sys.path.insert(0, base_loc_script)


import load_remote
import File_Name
import onshape_urdf_export
import urdf_obj_asset
import urdf_joint_map
import generate_joint_map
import create_all_joints
import physics_client
import update_onshape_ids
import tkinter as tk
import time
import subprocess
import requests
import openpyxl
from openpyxl.styles import Font
import glob
import math
from datetime import datetime

#Section One, Set up folder for all data to be stored
# Base directory location on PC for python scripts
SCRIPT_DIR = base_loc_script

# Pull current date/time
current_datetime = datetime.now()
current_datetime_str = current_datetime.strftime("%y_%m_%d_%H_%M")
# Set folder path to the base location with a new folder labelled
# with the date and time of the start of the experiment
folder = os.path.join(base_loc_data, current_datetime_str)

# If folder doesn't exist, create it
if not os.path.isdir(folder):
    os.makedirs(folder)

# One row per throw this run -- the original load AND every
# webhook-triggered re-pull get their own row, so the log covers this
# whole run's history of model numbers and their thrown distances.
BALL_DISTANCE_LOG_PATH = os.path.join(folder, "ball_distance_log.xlsx")


#Onshape Setup
# Setup variables for Onshape CAD.
#
# These fallback values are what's used if PROMPT_FOR_ONSHAPE_IDS is
# False below, or if the popup is cancelled -- update_onshape_ids.py
# keeps these (and the matching copies in generate_joint_map.py) in
# sync on disk every time you DO provide fresh URLs, so these stay
# current between runs even with prompting off.
# Document ID
DID = "6c0d2b9b726f93f6e30525f2"

# Workspace ID
WID = "e60b006b496816beb3fba67d"

# Assembly ID for studio
EIDs = "045a083a25ff8005c749039f"

# Assembly ID for Assembly
EIDa = "9524668d3f639b4055de0e2b"

# Set to False to skip the popup and just use whatever's hardcoded
# above (e.g. for an unattended/scheduled run where no one's there to
# click through a dialog).
PROMPT_FOR_ONSHAPE_IDS = True

if PROMPT_FOR_ONSHAPE_IDS:
    print("Prompting for the current Onshape document -- this is a REAL run "
          "against the live Onshape API using whatever IDs come out of this, "
          "not the file-only dry run that update_onshape_ids.py does on its own.")
    _studio_url, _assembly_url = update_onshape_ids.prompt_for_urls()

    if _studio_url and _assembly_url:
        _studio_parsed = update_onshape_ids.parse_onshape_url(_studio_url)
        _assembly_parsed = update_onshape_ids.parse_onshape_url(_assembly_url)

        if _studio_parsed["did"] != _assembly_parsed["did"] or _studio_parsed["wid"] != _assembly_parsed["wid"]:
            raise ValueError(
                "The two URLs point at DIFFERENT documents or workspaces -- refusing "
                "to proceed with a mismatched DID/WID for a real API run. Re-run and "
                "make sure both URLs are tabs of the SAME document and workspace."
            )

        DID = _studio_parsed["did"]
        WID = _studio_parsed["wid"]
        EIDs = _studio_parsed["eid"]
        EIDa = _assembly_parsed["eid"]

        print("\nUsing freshly-parsed IDs for this run:")
        for _var_name, _var_value in (("DID", DID), ("WID", WID), ("EIDs", EIDs), ("EIDa", EIDa)):
            print(f"  {_var_name} = {_var_value}")

        # Persist to disk (this file + generate_joint_map.py, same
        # TARGET_FILES list and same write+verify functions
        # update_onshape_ids.py uses on its own) so the NEXT run of
        # ANY script in this project starts from these same IDs too,
        # not just this one.
        print("\nSyncing IDs to disk across all subscripts...")
        _new_values = {"DID": DID, "WID": WID, "EIDs": EIDs, "EIDa": EIDa}
        for _file_path, _var_flags in update_onshape_ids.TARGET_FILES:
            for _var_name, _required in _var_flags.items():
                _new_value = _new_values[_var_name]
                _found = update_onshape_ids.update_variable_in_file(_file_path, _var_name, _new_value)
                if _found:
                    _verified = update_onshape_ids.verify_variable_in_file(_file_path, _var_name, _new_value)
                    print(f"  {'OK' if _verified else 'MISMATCH AFTER WRITE'}  {_file_path}: "
                          f"{_var_name} = {_new_value}")
                elif _required:
                    print(f"  WARNING: {_var_name} not found in {_file_path} -- couldn't sync it there.")

        # generate_joint_map was imported above with whatever IDs were on
        # disk at import time; the disk sync just above doesn't change an
        # already-imported module, so patch it directly for this run.
        generate_joint_map.DID = DID
        generate_joint_map.WID = WID
        generate_joint_map.EIDa = EIDa
    else:
        print("Popup cancelled/empty -- continuing with the existing hardcoded IDs above.")

# Pull the part studio name from Onshape (see onshape_api.py).
# NOTE: this only fills in `wid`/`eid` if get_partstudio_name needs them --
# confirm against the Onshape API docs whether the /d/{did} route alone
# is sufficient for your document, or whether you need to pass WID/EIDa too.
name = File_Name.get_partstudio_name(DID)

# Ground plane size (stage units, same convention as the asset scale
# handled in the Kit extension's /load_asset route). This is the ONE
# place this value should be set -- load_remote.create_ground_plane()
# requires it explicitly rather than defaulting it, so there's no
# second copy of this number to fall out of sync.
GROUND_PLANE_SIZE = 6000.0

# How long (and how often) this script polls /get_ball_landing_result
# after starting the simulation, waiting for the ball to land. Raise
# LANDING_TRACKER_TIMEOUT_S if a real throw genuinely takes longer than
# this. Kit itself pauses the sim and pops its own OK-button dialog the
# instant it detects the landing (see physics.py) independent of this
# polling loop -- this is just this script's OWN terminal-side report
# of the same result, same as rerun_local_test.py.
LANDING_TRACKER_TIMEOUT_S = 30.0
LANDING_POLL_INTERVAL_S = 0.5

# Per-part mass from the URDF <inertial> blocks (Onshape's own values for
# the assigned materials -- already in the export, so no extra API calls).
# False = PhysX volume x default density, which is what the confirmed-
# working rerun_local_test_urdf.py used. Flip to True for real masses.
APPLY_URDF_MASS = False

# Frame members get collision disabled between each other wherever their
# bounding boxes touch -- every frame connection is a pinned/fixed joint,
# so contact between members only adds artifacts (e.g. Base pushing the
# unmated VerticalBar). These moving parts keep full collision.
FILTER_FRAME_SELF_COLLISION = True

# "Z" = Kit shows the model in Onshape's exact coordinates (X, Y, Z all
# match Onshape, Z up). REQUIRES Kit's stage to be Z-up: Edit > Preferences
# > Stage > Default Up Axis = Z, then restart Kit (the startup stage is
# created with that setting). load_model() checks this and stops with
# instructions if Kit's stage disagrees. "Y" = previous behavior (Onshape
# Z shown as Kit's Y).
STAGE_UP_AXIS = "Z"
urdf_joint_map.STAGE_UP_AXIS = STAGE_UP_AXIS
MOVING_PART_NAMES = ["ThrowingArm", "Ball"]


# Function to pull cad from Onshape
def cad_pull(iteration):
    """
    Export the current assembly from Onshape as URDF + OBJ meshes, unpack
    it into this run's folder, and wrap the OBJs in a .usda that
    /load_asset can load. Returns (usd_path, urdf_path, mesh_dir).

    One Onshape export per call (plus the status polls and the download);
    everything after that is local file work.
    """
    export_dir = os.path.join(folder, f"{name}_{iteration}_urdf")
    urdf_path, mesh_dir = onshape_urdf_export.export_assembly_urdf(DID, WID, EIDa, export_dir)
    usd_path = os.path.join(folder, f"{name}_{iteration}.usda")
    urdf_obj_asset.build_asset(urdf_path, usd_path, mesh_dir)
    return usd_path, urdf_path, mesh_dir


# -----------------------------------------------------------------------
# KIT APP AUTO-LAUNCH
# -----------------------------------------------------------------------
# Replaces manually cd-ing into the kit-app-template project directory
# and running `.\repo.bat launch` (then picking your app from its
# interactive arrow-key prompt) before running this script.

# Root of the kit-app-template checkout for this project. NOT
# SCRIPT_DIR -- confirmed this script and the kit-app-template project
# live in different locations (E:\Onshape test\ vs
# C:\OmniverseStream\kit-app-template), not nested inside each other.
#Update to where omniverse BAT files live
KIT_APP_ROOT = r"C:\OmniverseStream\kit-app-template"

# The GENERATED per-app launcher .bat under _build/<platform>/release --
# deliberately NOT `.\repo.bat launch` itself. repo.bat launch opens an
# interactive "? Select with arrow keys which App you would like to
# launch:" prompt whenever a project has more than one .kit app, which
# just hangs forever waiting for keyboard input when run as a
# subprocess with no attached terminal. The generated launcher for one
# SPECIFIC app skips that prompt entirely.
#
# This is a best-guess filename (same base name as the .kit app
# selected at the repo.bat launch prompt, ".bat" instead of ".kit") --
# repo.bat build's exact naming can differ slightly. If it's wrong,
# _resolve_kit_app_launcher() below falls back to scanning the release
# folder itself instead of just failing on it.
KIT_APP_LAUNCHER = os.path.join(
    KIT_APP_ROOT, "_build", "windows-x86_64", "release",
    "my_demo.my_editor_streaming.kit.bat",
)

# Matches physics_client.KIT_HOST/KIT_PORT -- this is the same HTTP
# server every other function in this pipeline (load_remote,
# physics_client, etc.) already talks to.
KIT_READY_HOST = physics_client.KIT_HOST
KIT_READY_PORT = physics_client.KIT_PORT

# Kit's FIRST boot after a build (cold shader cache, cold extension
# cache) can take minutes; later boots are much faster. This timeout
# covers the slow case -- raise it further if your first boot is
# consistently slower than this.
KIT_BOOT_TIMEOUT_S = 180.0
KIT_READY_POLL_INTERVAL_S = 2.0


def _resolve_kit_app_launcher() -> str:
    """
    Resolve the actual generated launcher .bat under
    _build\\windows-x86_64\\release. Tries the KIT_APP_LAUNCHER guess
    above first; repo.bat build's exact naming can differ slightly
    from the .kit filename (extra suffixes, different casing, etc.),
    so if that guess doesn't exist, this scans the release folder
    itself instead of just failing on a wrong hardcoded name.
    """
    if os.path.isfile(KIT_APP_LAUNCHER):
        return KIT_APP_LAUNCHER

    release_dir = os.path.dirname(KIT_APP_LAUNCHER)
    if not os.path.isdir(release_dir):
        raise FileNotFoundError(
            f"Release folder not found at {release_dir}. Build the app first "
            f"(.\\repo.bat build from {KIT_APP_ROOT})."
        )

    # Filter to actual app launchers: pattern is always "<name>.kit.bat".
    # Excludes noise seen in this project's own release folder --
    # tests-*.bat (test runners, one per app), and one-off utility
    # scripts like kit.bat/pull_kit_sdk.bat/setup_python_env.bat that
    # aren't app launchers at all.
    all_bats = sorted(
        p for p in glob.glob(os.path.join(release_dir, "*.kit.bat"))
        if not os.path.basename(p).lower().startswith("tests-")
    )
    if not all_bats:
        raise FileNotFoundError(
            f"No .bat launcher found in {release_dir} at all. Build the app first "
            f"(.\\repo.bat build from {KIT_APP_ROOT})."
        )

    if len(all_bats) == 1:
        print(f"KIT_APP_LAUNCHER guess ({os.path.basename(KIT_APP_LAUNCHER)}) wasn't found, "
              f"but exactly one .bat exists in {release_dir} -- using it: "
              f"{os.path.basename(all_bats[0])}")
        return all_bats[0]

    raise FileNotFoundError(
        f"Kit app launcher not found at {KIT_APP_LAUNCHER}, and multiple .bat files "
        f"exist in {release_dir}, so this can't guess which one is right:\n  "
        + "\n  ".join(os.path.basename(p) for p in all_bats)
        + f"\n\nUpdate KIT_APP_LAUNCHER above to the correct one from that list."
    )


def start_kit_app_process():
    """
    Launch the Kit app in the background and return IMMEDIATELY (does
    NOT wait for it to finish booting) -- see wait_for_kit_app_ready()
    for the blocking half. Split into two functions specifically so
    main() below can call this FIRST, then do the URDF export + asset
    build while Kit boots in the background, and only block on
    Kit actually being ready right before it's needed
    (load_remote.load_asset()). Kit's own boot time (extension
    loading, shader cache warmup on a cold cache) can take a while, so
    overlapping it with the download/conversion work saves real wall
    time instead of doing everything strictly in sequence.

    Uses subprocess.Popen, NOT .run()/check_call(): Kit is a
    long-running server process, not a script that's supposed to exit
    on its own. .run() would block this entire script forever waiting
    for Kit to close.

    Returns None WITHOUT launching if Kit is already up and answering on
    KIT_READY_PORT (e.g. left open from a previous run). Launching a
    second copy anyway is what crashed the run: the old instance kept
    serving 8011, the new one fell back to another port, and the old one
    dropped its connection as soon as the second finished booting.
    """
    try:
        resp = requests.get(f"http://{KIT_READY_HOST}:{KIT_READY_PORT}/docs", timeout=2)
        if resp.status_code == 200:
            print(f"Kit app is already running on {KIT_READY_HOST}:{KIT_READY_PORT} -- "
                  f"reusing it, NOT launching a second instance.")
            return None
    except requests.exceptions.RequestException:
        pass

    launcher_path = _resolve_kit_app_launcher()

    print(f"Launching Kit app: {launcher_path}")
    return subprocess.Popen(
        [launcher_path],
        cwd=os.path.dirname(launcher_path),
    )


def wait_for_kit_app_ready(process):
    """
    Block until the Kit app's HTTP server (the same one physics_client
    talks to) is actually accepting requests, or raise if it takes too
    long / the process dies first.
    """
    print(f"Waiting up to {KIT_BOOT_TIMEOUT_S:.0f}s for Kit's HTTP server on "
          f"{KIT_READY_HOST}:{KIT_READY_PORT} to come up...")
    deadline = time.time() + KIT_BOOT_TIMEOUT_S
    while time.time() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(
                f"Kit app process exited early (return code {process.returncode}) "
                f"before its HTTP server ever came up -- check the Kit app's own "
                f"console/log window for the actual error (a bad build, a port "
                f"already in use, a missing extension, etc.)."
            )
        try:
            # /docs is FastAPI's auto-generated docs page -- exists the
            # moment uvicorn is listening, regardless of whether a
            # stage/asset has been loaded yet. A plain reachability
            # check, not a call into any of this pipeline's own routes.
            resp = requests.get(f"http://{KIT_READY_HOST}:{KIT_READY_PORT}/docs", timeout=2)
            if resp.status_code == 200:
                print("Kit app is up and responding.")
                return
        except requests.exceptions.RequestException:
            pass
        time.sleep(KIT_READY_POLL_INTERVAL_S)

    raise TimeoutError(
        f"Kit app didn't respond on {KIT_READY_HOST}:{KIT_READY_PORT} within "
        f"{KIT_BOOT_TIMEOUT_S:.0f}s. It may still be booting (a cold first launch "
        f"after a build/cache-clear can take several minutes) -- check the Kit "
        f"window/log directly, or raise KIT_BOOT_TIMEOUT_S."
    )


def launch_kit_app():
    """Convenience wrapper: start Kit AND block until it's ready, in one call.

    Use this (instead of the two functions above separately) for a
    simpler script -- e.g. rerun_local_test.py, which has no CAD
    download/conversion step to overlap the boot time with anyway.
    """
    process = start_kit_app_process()
    wait_for_kit_app_ready(process)
    return process





def filter_frame_self_collision(entries):
    """Disable collision for every pair of frame members whose bounding
    boxes touch at all (threshold 0.0), leaving MOVING_PART_NAMES alone."""
    moving = {generate_joint_map._group_key(n) for n in MOVING_PART_NAMES}
    frame = sorted({p for e in entries for p in (e["body0_path"], e["body1_path"])
                    if p and generate_joint_map._group_key(p.rsplit("/", 1)[-1]) not in moving})
    exempt = [(e["body0_path"], e["body1_path"]) for e in entries
              if e["body0_path"] and e["body1_path"]]
    print(f"Disabling collision between touching frame members ({len(frame)} parts; "
          f"keeping full collision for {MOVING_PART_NAMES})...")
    physics_client.auto_filter_overlapping_pairs(frame, exempt, threshold=0.0)


def transfer_joints(urdf_path, mesh_dir):
    """
    Joints (and optionally masses) from the downloaded URDF -- replaces the
    Onshape mates API + mass-properties API calls. Must run AFTER the asset
    is loaded (it matches URDF parts to live prims).
    """
    urdf_joint_map.MESH_FOLDER = mesh_dir
    entries = urdf_joint_map.run(write=True, urdf_path=urdf_path)
    create_all_joints.main()  # joints + makes the unjointed Ball dynamic

    generate_joint_map.disable_mechanism_self_collision(entries)
    if FILTER_FRAME_SELF_COLLISION:
        filter_frame_self_collision(entries)

    # Must run AFTER create_all_joints.main(): joint creation resets
    # collision approximation to the convexHull default on every joint
    # body. Local-only (list_prims + name matching), no Onshape calls.
    generate_joint_map.apply_concave_collision_overrides()

    # Also after joints: set_mass needs RigidBodyAPI already on the part.
    if APPLY_URDF_MASS:
        asm = urdf_joint_map.UrdfAssembly(urdf_path, mesh_dir)
        by_name = {asm.instance_name[n]: asm.links[n]["mass"] for n in asm.parts}
        for e in physics_client.list_prims(root_path=generate_joint_map.PRIM_ROOT)["prims"]:
            mass = by_name.get(e["path"].rsplit("/", 1)[-1])
            if mass and mass > 1e-6:
                physics_client.set_mass(e["path"], mass)


def load_model(usd_file, urdf_path, mesh_dir, create_ground=False):
    """clear -> load -> (ground plane, first time only) -> align -> joints.

    clear_asset() is REQUIRED on reloads: loading on top of the previous
    run's simulated instance leaves the old ball tracker's has_landed=True
    state (and old joints) in place. align_to_ground() now runs on reloads
    too -- a changed model can have a different lowest point."""
    physics_client.clear_asset()
    load_remote.load_asset(usd_file)
    if create_ground:
        load_remote.create_ground_plane(size=GROUND_PLANE_SIZE)
    aligned = physics_client.align_to_ground() or {}
    stage_up = str(aligned.get("up_axis", STAGE_UP_AXIS)).upper()
    if stage_up != STAGE_UP_AXIS:
        raise RuntimeError(
            f"Kit's stage is {stage_up}-up, but the asset was built for a {STAGE_UP_AXIS}-up stage "
            f"(STAGE_UP_AXIS = {STAGE_UP_AXIS!r}) -- the trebuchet would be lying on its side. "
            f"Either set Kit's stage to {STAGE_UP_AXIS}-up (Edit > Preferences > Stage > "
            f"Default Up Axis, then restart Kit) or set STAGE_UP_AXIS = {stage_up!r} in "
            f"ExperimentCodeComplete.py.")
    transfer_joints(urdf_path, mesh_dir)


def log_ball_distance(model_number, distance_meters):
    """
    Append one row (model number, distance thrown, timestamp) to
    BALL_DISTANCE_LOG_PATH. Creates the file with a header row the
    first time it's called this run; every call after just appends.

    distance_meters=None logs "N/A" -- used when the ball never landed
    within LANDING_TRACKER_TIMEOUT_S, so that model number still gets
    a row (the log stays a complete record of every run, not just the
    successful ones) rather than silently having a gap.
    """
    if not os.path.isfile(BALL_DISTANCE_LOG_PATH):
        wb = openpyxl.Workbook()
        sheet = wb.active
        sheet.title = "Ball Distance Log"
        headers = ["Model Number", "Distance Thrown (m)", "Timestamp"]
        for col, header in enumerate(headers, start=1):
            cell = sheet.cell(row=1, column=col, value=header)
            cell.font = Font(name="Arial", bold=True)
        sheet.column_dimensions["A"].width = 16
        sheet.column_dimensions["B"].width = 20
        sheet.column_dimensions["C"].width = 20
        wb.save(BALL_DISTANCE_LOG_PATH)

    wb = openpyxl.load_workbook(BALL_DISTANCE_LOG_PATH)
    sheet = wb["Ball Distance Log"]
    next_row = sheet.max_row + 1

    value = round(distance_meters, 4) if distance_meters is not None else "N/A"
    sheet.cell(row=next_row, column=1, value=model_number).font = Font(name="Arial")
    sheet.cell(row=next_row, column=2, value=value).font = Font(name="Arial")
    sheet.cell(row=next_row, column=3,
               value=datetime.now().strftime("%Y-%m-%d %H:%M:%S")).font = Font(name="Arial")
    wb.save(BALL_DISTANCE_LOG_PATH)
    print(f"Logged to {BALL_DISTANCE_LOG_PATH}: model_number={model_number}, distance={value}")


def _ball_world_position(ball_path):
    """(world position in stage units, meters_per_unit) of the Ball prim's
    origin -- its geometric center, see urdf_obj_asset.RECENTER_PART_NAMES."""
    result = physics_client.list_prims(root_path=ball_path)
    for e in result["prims"]:
        if e["path"] == ball_path and e.get("world_translation") is not None:
            return tuple(e["world_translation"]), result.get("meters_per_unit", 1.0)
    return None, result.get("meters_per_unit", 1.0)


def throw_distance_meters(start, end, up_axis, meters_per_unit):
    """Straight-line distance ALONG THE GROUND between the ball's start and
    landing positions (the up-axis component is ignored), always >= 0 --
    independent of which direction the arm throws."""
    up = "XYZ".index(str(up_axis).upper()) if str(up_axis).upper() in ("X", "Y", "Z") else 1
    horiz = [a for a in range(3) if a != up]
    return math.sqrt(sum((end[a] - start[a]) ** 2 for a in horiz)) * meters_per_unit


def run_ball_landing_check(model_number):
    """
    Resolves the Ball part, starts the landing tracker, starts the
    simulation, and polls/reports the thrown distance -- extracted out
    of main()'s initial setup so it can ALSO run after every
    webhook-triggered re-pull in the update loop below, not just once
    for the very first CAD load. Distance gets reported for every run
    now, not only the first.

    Tracker started BEFORE start_simulation() on purpose -- starting
    it after leaves a window where a fast throw could already be
    airborne or landed before the subscription actually exists on the
    Kit side.
    """
    # Offline lookup (live prims only) -- the URDF asset names the part
    # exactly "Ball", so no Onshape instance data is needed.
    ball_path = generate_joint_map.find_part_prim_path_offline("Ball")
    if not ball_path:
        print("WARNING: could not resolve a 'Ball' part in this assembly -- "
              "landing won't be tracked.")
        physics_client.start_simulation()
        log_ball_distance(model_number, None)
        return

    tracker = physics_client.start_ball_landing_tracker(ball_path)
    up_axis = tracker.get("up_axis", "Y")

    # Ball's resting position BEFORE the throw -- the start point of the
    # distance measurement.
    start_pos, meters_per_unit = _ball_world_position(ball_path)

    # Explicitly ensure /World/PhysicsScene exists and start playback,
    # rather than relying on pressing Kit's native Play button (which
    # leaves /World/PhysicsScene undefined -- PhysX then silently
    # auto-creates its OWN fallback scene at simulation start). Calling
    # this ourselves means gravity/scene settings are the ones this
    # pipeline actually intends, rather than whatever PhysX's fallback
    # happens to default to.
    physics_client.start_simulation()

    # Kit itself already pauses the sim (physics.stop_simulation_impl)
    # and pops its own native OK-button dialog with the distance the
    # instant it detects the landing. This poll is just this
    # terminal's OWN echo of that same result, so the distance also
    # shows up wherever this script's output is being logged (e.g. a
    # run log file), not only on the Kit machine's desktop.
    print(f"\nWaiting up to {LANDING_TRACKER_TIMEOUT_S:.0f}s for the ball to land "
          f"(polling /get_ball_landing_result every {LANDING_POLL_INTERVAL_S:.1f}s)...")
    deadline = time.time() + LANDING_TRACKER_TIMEOUT_S
    landed = False
    while time.time() < deadline:
        result = physics_client.get_ball_landing_result(ball_path)
        if result.get("has_landed"):
            landed = True
            # Kit pauses the sim the instant it detects the landing, so the
            # ball's current position IS the landing position.
            end_pos, _ = _ball_world_position(ball_path)
            distance = None
            if start_pos and end_pos and math.dist(start_pos, end_pos) > 1e-6:
                distance = throw_distance_meters(start_pos, end_pos, up_axis, meters_per_unit)
                print(f"\nBall started at {tuple(round(v, 3) for v in start_pos)} and landed at "
                      f"{tuple(round(v, 3) for v in end_pos)} (stage units).")
            elif start_pos:
                # Fallback if the stage position didn't update (physics
                # results not written back to USD): X-only, from Kit's
                # own landing_x.
                distance = abs(result["landing_x"] - start_pos[0]) * meters_per_unit
                print("\n(Ball's stage position didn't update -- using Kit's landing X only.)")
            if distance is None:
                distance = abs(result["distance_meters"])
            print(f"Distance thrown: {distance:.4f} m  "
                  f"(Kit's own signed value was {result['distance_meters']:.4f} m)")
            log_ball_distance(model_number, distance)
            break
        time.sleep(LANDING_POLL_INTERVAL_S)
    if not landed:
        print(f"\nBall hadn't landed after {LANDING_TRACKER_TIMEOUT_S:.0f}s of polling -- "
              f"either the throw takes longer than that, the mechanism didn't launch it, "
              f"or it's not crossing back down to the ground plane's height at all. Check "
              f"Kit's own console for '[ball_landing_tracker]' output, and consider raising "
              f"LANDING_TRACKER_TIMEOUT_S if the throw is just slow.")
        log_ball_distance(model_number, None)


def prompt_next_iteration(iteration: int, max_iterations: int) -> bool:
    """
    Pops up a dialog in the TOP-RIGHT corner of the screen asking whether
    to start the next iteration (pull the current URDF from Onshape and re-simulate). Shows the iteration
    number and the max for this run. Returns True on OK, False on
    Cancel / closing the window.
 
    Built as a small custom window instead of messagebox.askokcancel(),
    because messagebox dialogs can't be positioned -- the OS always
    centers them. Forced topmost so it appears in front of Kit's window
    instead of hiding behind it.
    """
    margin = 20  # pixels from the top and right edges of the screen
    result = {"ok": False}
 
    root = tk.Tk()
    root.title("Next Iteration")
    root.attributes("-topmost", True)
    root.resizable(False, False)
 
    def on_ok(event=None):
        result["ok"] = True
        root.destroy()
 
    def on_cancel(event=None):
        result["ok"] = False
        root.destroy()
 
    frame = tk.Frame(root, padx=16, pady=12)
    frame.pack()
    tk.Label(frame, text=f"Iteration {iteration} of {max_iterations}",
             font=("Arial", 12, "bold")).pack(anchor="w")
    tk.Label(frame,
             text="Make your Onshape changes, then press OK to pull the URDF and run this iteration.\n\nPress Cancel to end the run.",
             font=("Arial", 10), justify="left", wraplength=280).pack(anchor="w", pady=(8, 12))
 
    buttons = tk.Frame(frame)
    buttons.pack(anchor="e")
    tk.Button(buttons, text="OK", width=10, command=on_ok).pack(side="left", padx=(0, 6))
    tk.Button(buttons, text="Cancel", width=10, command=on_cancel).pack(side="left")
 
    root.bind("<Return>", on_ok)
    root.bind("<Escape>", on_cancel)
    root.protocol("WM_DELETE_WINDOW", on_cancel)
 
    # Measure the finished window, then place it in the top-right corner.
    root.update_idletasks()
    width = root.winfo_reqwidth()
    x = root.winfo_screenwidth() - width - margin
    root.geometry(f"+{x}+{margin}")
 
    root.lift()
    root.focus_force()
    root.mainloop()
    return result["ok"]

def main():

    # Start Kit booting in the BACKGROUND immediately -- don't block
    # here. cad_pull() (URDF export + download + asset build) takes
    # real time too; overlapping Kit's own
    # boot with that work, instead of waiting for Kit first and only
    # then starting the download, saves real wall time.
    kit_process = start_kit_app_process()

    # Download the original model as URDF + OBJ and build the stage asset
    # while Kit is still booting in the background.
    usd_file, urdf_file, mesh_dir = cad_pull("original")

    # Only block here, right before Kit is actually needed for the
    # first time -- by now it's had the whole export/download time to boot.
    wait_for_kit_app_ready(kit_process)

    # Ground plane only once (create_ground=True), not on every reload.
    # align_to_ground() inside load_model measures the real bounding boxes
    # and drops the assembly onto the plane.
    load_model(usd_file, urdf_file, mesh_dir, create_ground=True)

    run_ball_landing_check("original")

    # Main loop
    for iteration in range(1, MAX_ITERATIONS + 1):
        if not prompt_next_iteration(iteration, MAX_ITERATIONS):
            print(f"\nRun ended by user before iteration {iteration}.")
            break

        if PULL_DELAY_SECONDS > 0:
            print(f"\nOK pressed -- waiting {PULL_DELAY_SECONDS:g}s before pulling URDF...")
            time.sleep(PULL_DELAY_SECONDS)
            
        print(f"\n--- Iteration {iteration} of {MAX_ITERATIONS}: pulling URDF ---")
        usd_file, urdf_file, mesh_dir = cad_pull(iteration)
        load_model(usd_file, urdf_file, mesh_dir)
 
        # Ball distance check on EVERY re-pull, not just the initial
        # load -- each new CAD version gets its own simulation run and
        # its own reported throw distance.
        run_ball_landing_check(iteration)
    else:
        print(f"\nReached MAX_ITERATIONS ({MAX_ITERATIONS}) -- run complete.")


if __name__ == "__main__":
    main()