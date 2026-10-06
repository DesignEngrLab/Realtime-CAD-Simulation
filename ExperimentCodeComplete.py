#Combine several subprograms to be able to run full experiment
#
# CAD source: Onshape URDF export (+ OBJ/MTL meshes) -- no STEP anywhere.
# On start and on every debounced webhook change:
#   onshape_urdf_export  -> download the assembly as URDF + OBJ (1 export call)
#   urdf_obj_asset       -> wrap the OBJs in a .usda for /load_asset
#   urdf_joint_map       -> joint_map.py from the URDF (no mates API calls)
#   create_all_joints    -> joints in Kit (unchanged)

import load_remote
import File_Name
import onshape_urdf_export
import urdf_obj_asset
import urdf_joint_map
import onshape_webhook_listener
import generate_joint_map
import create_all_joints
import physics_client
import update_onshape_ids
import register_webhook
import os
import time
import subprocess
import requests
import openpyxl
from openpyxl.styles import Font
import glob
import math
import sys
from datetime import datetime

# Directory this script itself lives in -- used to build absolute paths
# to sibling scripts so subprocess calls work regardless of the
# terminal's current working directory.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))



#Section One, Set up folder for all data to be stored

# Base directory location on PC for python scripts
base_loc = r"E:/Onshape test"

# Pull current date/time
current_datetime = datetime.now()
current_datetime_str = current_datetime.strftime("%y_%m_%d_%H_%M")
# Set folder path to the base location with a new folder labelled
# with the date and time of the start of the experiment
folder = os.path.join(base_loc, current_datetime_str)

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

# NOTE: iteration counter, mass tracking, model number, and timer are
# initialized inside main() itself now, not here at module level.
# Assigning to a name anywhere inside a function makes Python treat it
# as local for the WHOLE function -- so if these were only set here,
# any `i += 1` etc. inside main() would shadow them with an
# uninitialized local, causing UnboundLocalError the moment the while
# loop's condition tried to read `i` before that function-local `i`
# had ever been assigned.
MassPrevious = None

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

# How long to wait, with NO further webhook flags seen, before actually
# starting the CAD download -- debounces rapid successive edits (e.g.
# collapseEvents batching, or just someone actively editing) into a
# single re-pull reflecting the FINAL state, instead of kicking off an
# expensive download+conversion+re-simulate cycle on every single
# flag, possibly mid-edit.
WEBHOOK_DEBOUNCE_SECONDS = 5.0

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
KIT_APP_ROOT = r"E:\OmniverseStream\kit-app-template"

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
    KIT_APP_ROOT, "my_demo.my_editor_streaming.kit.bat",
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


WEBHOOK_LISTENER_PORT = 5000
WEBHOOK_LISTENER_READY_TIMEOUT_S = 10.0


def start_webhook_listener_process():
    """
    Auto-starts onshape_webhook_listener.py as its own background
    process, instead of requiring it to be launched manually in a
    separate terminal. Uses subprocess.Popen (non-blocking) -- that
    script's Flask dev server blocks forever once started via
    app.run(), same reasoning as start_kit_app_process() above needing
    Popen instead of run()/check_call().

    If a listener is ALREADY running (left over from a previous run,
    or started manually), this detects that via a quick reachability
    check and skips launching a second one -- a second Flask process
    couldn't bind the same port anyway.

    Still requires your tunnel (ngrok etc.) to be running separately
    and register_webhook.py to have been run at least once against
    that tunnel's URL -- this only handles the LOCAL listener process
    itself, not the public-facing tunnel or the Onshape-side
    registration.
    """
    listener_path = os.path.join(SCRIPT_DIR, "onshape_webhook_listener.py")
    listener_url = f"http://127.0.0.1:{WEBHOOK_LISTENER_PORT}/"

    if not os.path.isfile(listener_path):
        print(f"WARNING: onshape_webhook_listener.py not found at {listener_path} -- "
              f"webhook-triggered updates won't work until it exists and either this "
              f"auto-start or a manual `python onshape_webhook_listener.py` run gets it going.")
        return None

    try:
        requests.get(listener_url, timeout=2)
        print("Webhook listener already running -- not starting a second one.")
        return None
    except requests.exceptions.RequestException:
        pass

    print(f"Starting webhook listener: {listener_path}")
    process = subprocess.Popen([sys.executable, listener_path])

    deadline = time.time() + WEBHOOK_LISTENER_READY_TIMEOUT_S
    while time.time() < deadline:
        if process.poll() is not None:
            print(f"WARNING: webhook listener process exited early (return code "
                  f"{process.returncode}) -- check its own output above for the actual error.")
            return process
        try:
            requests.get(listener_url, timeout=1)
            print("Webhook listener is up and responding.")
            return process
        except requests.exceptions.RequestException:
            time.sleep(0.5)

    print(f"WARNING: webhook listener didn't respond within {WEBHOOK_LISTENER_READY_TIMEOUT_S:.0f}s "
          f"of starting -- check for errors in its own output, and confirm nothing else is "
          f"already using port {WEBHOOK_LISTENER_PORT}.")
    return process


def ensure_webhook_registered(did: str, wid: str, eid: str):
    """
    Checks whether a webhook already exists for THIS document (the one
    from the popup-entered URLs, not whatever register_webhook.py's
    own hardcoded constants say) and only registers a new one if it
    doesn't -- avoids creating a duplicate registration every single
    run.

    "Already exists for this document" is checked via the `filter`
    field on each registered webhook -- confirmed (see
    register_webhook.py's own comments) that this is the field Onshape
    actually uses for document/workspace/element scoping, unlike the
    top-level documentId field which doesn't reliably come back in
    list responses. A match is only treated as "good" if its url ALSO
    matches the CURRENT tunnel URL -- a match against a stale url
    (e.g. from a previous ngrok session) gets cleaned up and replaced,
    since a dead URL there is functionally the same as no registration
    at all.

    Monkeypatches register_webhook's own DOCUMENT_ID/WORKSPACE_ID/
    ELEMENT_ID module attributes to these values before calling its
    register_webhook() function -- same pattern already used elsewhere
    in this file for generate_joint_map, since
    register_webhook.py's registration logic reads those as module-
    level globals rather than taking them as parameters.
    """
    document_filter_marker = f"{{$DocumentId}} = '{did}'"

    try:
        webhooks = register_webhook.list_webhooks()
    except requests.exceptions.RequestException as e:
        print(f"WARNING: couldn't check existing webhooks ({e}) -- skipping webhook "
              f"registration for this run. Webhook-triggered updates won't work until "
              f"this is checked manually (e.g. run register_webhook.py directly).")
        return

    items = webhooks.get("items", webhooks) if isinstance(webhooks, dict) else webhooks
    for wh in items or []:
        if document_filter_marker in (wh.get("filter") or ""):
            if wh.get("url") == register_webhook.CALLBACK_URL:
                print(f"Webhook already registered for this document at the current "
                      f"tunnel URL (id={wh.get('id')}) -- not creating another one.")
                return
            else:
                print(f"Found an existing webhook for this document, but pointed at a "
                      f"stale URL ({wh.get('url')!r} != current {register_webhook.CALLBACK_URL!r}) "
                      f"-- deleting it before registering a fresh one: id={wh.get('id')}")
                try:
                    register_webhook.delete_webhook(wh["id"])
                except requests.exceptions.RequestException as e:
                    print(f"WARNING: failed to delete stale webhook {wh.get('id')} ({e}) -- "
                          f"continuing to register a new one anyway.")

    if not register_webhook.check_callback_reachable():
        print("NOT registering a webhook -- CALLBACK_URL isn't reachable right now "
              "(tunnel down, or listener not actually up yet). Webhook-triggered "
              "updates won't work until this is fixed and register_webhook.py is run "
              "again (manually, or via this function on the next run).")
        return

    register_webhook.DOCUMENT_ID = did
    register_webhook.WORKSPACE_ID = wid
    register_webhook.ELEMENT_ID = eid
    register_webhook.register_webhook()


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


def check_for_onshape_update() -> bool:
    """
    Checks the REAL webhook flag -- requires
    `python onshape_webhook_listener.py` to be running as its own
    separate process, with your tunnel (ngrok etc.) forwarding to it
    and register_webhook.py already run once against that tunnel URL.

    This is a plain in-memory-then-file-based check
    (read_and_clear_change_flag() reads/deletes a small JSON flag file
    onshape_webhook_listener.py writes to on each real Onshape edit)
    -- NOT an Onshape API call itself. The actual API calls happen
    below in main()'s loop (cad_pull -> URDF export) only when this
    returns True.
    """
    event = onshape_webhook_listener.read_and_clear_change_flag()
    if event is not None:
        print(f"Webhook fired: {event.get('event')} on documentId={event.get('documentId')} "
              f"at {event.get('timestamp')}")
        return True
    return False


def main():

    # Auto-start the webhook listener first -- cheap and near-instant
    # (just opens a local Flask server), so no reason to delay it
    # behind the slower Kit/CAD work below. Still requires your tunnel
    # to be running separately -- this only handles starting the LOCAL
    # process.
    start_webhook_listener_process()

    # Now that the listener is (hopefully) up, check whether a webhook
    # already exists for THIS document/workspace/element -- the
    # popup-derived DID/WID/EIDa from earlier in this file, not
    # register_webhook.py's own separate hardcoded constants -- and
    # only register a fresh one if it doesn't (or if the existing one
    # points at a stale tunnel URL).
    ensure_webhook_registered(DID, WID, EIDa)

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

    # Loop counters, initialized here (not at module level) so Python
    # doesn't treat them as unbound locals -- see note above MassPrevious.
    i = 0
    model_number = 1
    timer = 0.0

    # Tracks when the LAST webhook flag was seen, so the actual CAD
    # download only starts after WEBHOOK_DEBOUNCE_SECONDS of quiet --
    # not on every single flag. A new flag arriving during the wait
    # RESETS this, extending the quiet period -- rapid successive
    # edits collapse into one re-pull reflecting the final state,
    # instead of one (expensive) re-pull attempt per edit.
    last_flag_time = None

    # Main loop
    while i <= 1 or timer <= 5.0:
        timer_current = time.time()

        if check_for_onshape_update():
            last_flag_time = time.time()
            print(f"Webhook flag seen -- waiting {WEBHOOK_DEBOUNCE_SECONDS:.0f}s of quiet "
                  f"before pulling, in case more edits are still coming in.")
        elif last_flag_time is not None and (time.time() - last_flag_time) >= WEBHOOK_DEBOUNCE_SECONDS:
            print(f"\n{WEBHOOK_DEBOUNCE_SECONDS:.0f}s since the last webhook flag -- "
                  f"starting CAD download now.")
            last_flag_time = None  # reset BEFORE the pull, not after -- a flag arriving
                                    # DURING the pull should start its own fresh debounce
                                    # window for the NEXT pull, not be silently absorbed.

            usd_file, urdf_file, mesh_dir = cad_pull(model_number)
            load_model(usd_file, urdf_file, mesh_dir)

            # Ball distance check on EVERY re-pull, not just the
            # initial load -- each new CAD version gets its own
            # simulation run and its own reported throw distance.
            # Uses model_number BEFORE incrementing it below -- that's
            # the number cad_pull() actually just downloaded, and what
            # the log should show this row is for.
            run_ball_landing_check(model_number)
            model_number += 1
        else:
            print("No Update")

        time.sleep(0.25)
        i += 1

        # Add elapsed loop time to timer
        timer += time.time() - timer_current


if __name__ == "__main__":
    main()