"""
update_onshape_ids.py

Standalone utility -- deliberately NOT part of the normal pipeline.
Makes NO calls to Onshape's API, does NOT launch or talk to Omniverse
Kit, does NOT download or convert any CAD. Its only job:

    1. Pop up asking for two Onshape tab URLs (Part Studio + Assembly).
    2. Parse the document/workspace/element IDs out of them.
    3. Write DID/WID/EIDs/EIDa into every script that currently
       hardcodes these constants.
    4. Re-read each file afterward and VERIFY the write actually
       landed correctly, reporting OK/FAILED per variable per file.

Use this to confirm the "update every subscript's Onshape IDs" step
works correctly, without spending a single real Onshape API call or
CAD download on it. Run generate_joint_map.py / ExperimentCodeComplete.py
separately afterward, once you've confirmed the IDs are consistent
everywhere.

WHY TWO URLS, NOT ONE: a single Onshape tab URL only ever encodes ONE
element id (whichever tab was open when you copied it). This pipeline
needs two different element ids -- EIDs (the Part Studio) and EIDa
(the Assembly) -- so there's no way around asking for both tabs'
URLs separately. DID and WID are shared between them (same document,
same workspace); this script cross-checks that both URLs actually
agree on those before writing anything.

FILES UPDATED (add more entries to TARGET_FILES below if a new script
starts hardcoding these constants too):
    - ExperimentCodeComplete.py  (DID, WID, EIDs, EIDa)
    - generate_joint_map.py      (DID, WID, EIDa -- no EIDs there;
                                   generate_material_map.py imports
                                   these three FROM generate_joint_map
                                   rather than hardcoding its own
                                   copy, so it's covered indirectly --
                                   confirmed via
                                   `from generate_joint_map import
                                   build_name_to_prim_map, DID, WID,
                                   EIDa` at the top of that file)
"""

import os
import re
import sys

# Directory this script lives in -- same convention as
# ExperimentCodeComplete.py's SCRIPT_DIR, so this works regardless of
# the terminal's current working directory. Assumes this script sits
# in the SAME folder as the files it updates; change the paths in
# TARGET_FILES below if that's not the case.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Each entry: (file path, {var_name: is_required}).
#   is_required=True  -> verification FAILS loudly if that variable
#                         isn't found in the file (catches a future
#                         rename silently going unnoticed).
#   is_required=False -> silently skipped if absent (expected case,
#                         e.g. EIDs not being in generate_joint_map.py).
TARGET_FILES = [
    (os.path.join(SCRIPT_DIR, "ExperimentCodeComplete.py"),
     {"DID": True, "WID": True, "EIDs": True, "EIDa": True}),
    (os.path.join(SCRIPT_DIR, "generate_joint_map.py"),
     {"DID": True, "WID": True, "EIDa": True}),
]

# Onshape object ids (document/workspace/version/element) are all
# 24-character hex strings.
_HEX24 = r"[0-9a-fA-F]{24}"

# Matches an Onshape tab URL, e.g.:
#   https://cad.onshape.com/documents/<did>/w/<wid>/e/<eid>?...
#   https://cad.onshape.com/documents/<did>/v/<vid>/e/<eid>?...
_URL_RE = re.compile(
    rf"onshape\.com/documents/(?P<did>{_HEX24})/(?P<wv>[wv])/(?P<wid>{_HEX24})/e/(?P<eid>{_HEX24})"
)


def parse_onshape_url(url: str) -> dict:
    """
    Extract {did, wv, wid, eid} from a single Onshape tab URL.
    Raises ValueError with a clear message if the URL doesn't match
    the expected pattern at all -- this is pure string parsing, no
    network request involved.
    """
    match = _URL_RE.search(url.strip())
    if not match:
        raise ValueError(
            f"Couldn't find a document/workspace-or-version/element pattern in "
            f"this URL: {url!r}\nExpected something like "
            f"https://cad.onshape.com/documents/<did>/w/<wid>/e/<eid>"
        )
    parsed = match.groupdict()
    if parsed["wv"] == "v":
        print("WARNING: this URL points at a VERSION (an immutable snapshot), not "
              "a live workspace -- WID will be set to that version's id. That's "
              "fine for reproducing one specific past state, but it won't pick up "
              "further edits made in Onshape the way a workspace URL would. Switch "
              "to the workspace tab in Onshape first if that's not what you want.")
    return parsed


def prompt_for_urls():
    """
    Show a popup asking for the Part Studio tab URL and the Assembly
    tab URL. Falls back to plain console input() if no GUI toolkit /
    desktop session is available (e.g. run from a true headless
    terminal with no display) -- same fallback pattern used for the
    ball-landed popup in physics.py.

    Returns (studio_url, assembly_url); either can be None/empty if
    the dialog was cancelled or closed without submitting.
    """
    try:
        import tkinter as tk

        result = {"studio": None, "assembly": None}

        root = tk.Tk()
        root.title("Update Onshape IDs")
        root.attributes("-topmost", True)

        tk.Label(
            root,
            text="Paste the URL from each tab (Part Studio and Assembly) of the "
                 "SAME document/workspace:",
            wraplength=420, justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 6))

        tk.Label(root, text="Part Studio tab URL:").grid(row=1, column=0, sticky="w", padx=10)
        studio_entry = tk.Entry(root, width=70)
        studio_entry.grid(row=2, column=0, columnspan=2, padx=10)

        tk.Label(root, text="Assembly tab URL:").grid(row=3, column=0, sticky="w", padx=10, pady=(10, 0))
        assembly_entry = tk.Entry(root, width=70)
        assembly_entry.grid(row=4, column=0, columnspan=2, padx=10)

        def on_ok():
            result["studio"] = studio_entry.get()
            result["assembly"] = assembly_entry.get()
            root.destroy()

        def on_cancel():
            root.destroy()

        button_frame = tk.Frame(root)
        button_frame.grid(row=5, column=0, columnspan=2, pady=14)
        tk.Button(button_frame, text="OK", width=10, command=on_ok).pack(side="left", padx=6)
        tk.Button(button_frame, text="Cancel", width=10, command=on_cancel).pack(side="left", padx=6)

        studio_entry.focus_set()
        root.mainloop()

        return result["studio"], result["assembly"]

    except Exception as e:
        print(f"Could not show GUI popup ({e}) -- falling back to console input.")
        studio_url = input("Part Studio tab URL: ").strip()
        assembly_url = input("Assembly tab URL: ").strip()
        return studio_url, assembly_url


def update_variable_in_file(file_path: str, var_name: str, new_value: str) -> bool:
    """
    Replace `VAR_NAME = "..."` with the new value in file_path, in
    place, preserving everything else in the file untouched. Returns
    True if a matching assignment was found and replaced, False if
    var_name doesn't appear in this file at all.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()

    pattern = re.compile(rf'^({re.escape(var_name)}\s*=\s*)"[^"]*"', re.MULTILINE)
    new_text, count = pattern.subn(rf'\g<1>"{new_value}"', text)

    if count == 0:
        return False
    if count > 1:
        print(f"  WARNING: '{var_name} = \"...\"' matched {count} times in this "
              f"file -- ALL of them were updated. If that's more than expected, "
              f"check for an accidental duplicate definition.")

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(new_text)
    return True


def verify_variable_in_file(file_path: str, var_name: str, expected_value: str) -> bool:
    """Re-read file_path from disk and confirm var_name now equals expected_value."""
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()
    match = re.search(rf'^{re.escape(var_name)}\s*=\s*"([^"]*)"', text, re.MULTILINE)
    if not match:
        return False
    return match.group(1) == expected_value


def main():
    studio_url, assembly_url = prompt_for_urls()
    if not studio_url or not assembly_url:
        print("Cancelled -- no URLs provided, nothing changed.")
        return

    studio_parsed = parse_onshape_url(studio_url)
    assembly_parsed = parse_onshape_url(assembly_url)

    if studio_parsed["did"] != assembly_parsed["did"] or studio_parsed["wid"] != assembly_parsed["wid"]:
        print("ERROR: the two URLs point at DIFFERENT documents or workspaces -- "
              "NOT updating anything. Make sure both URLs are tabs of the SAME "
              "document and the SAME workspace (or version), then retry:")
        print(f"  Part Studio -> did={studio_parsed['did']} wid={studio_parsed['wid']}")
        print(f"  Assembly    -> did={assembly_parsed['did']} wid={assembly_parsed['wid']}")
        sys.exit(1)

    new_values = {
        "DID": studio_parsed["did"],
        "WID": studio_parsed["wid"],
        "EIDs": studio_parsed["eid"],
        "EIDa": assembly_parsed["eid"],
    }

    print("\nParsed IDs:")
    for k, v in new_values.items():
        print(f"  {k} = {v}")

    print(f"\nUpdating {len(TARGET_FILES)} file(s) -- no Onshape API calls, no Kit "
          f"launch, no CAD download involved in any of this, just local text edits:")

    all_ok = True
    for file_path, var_flags in TARGET_FILES:
        print(f"\n{file_path}")
        if not os.path.isfile(file_path):
            print("  SKIPPED -- file not found.")
            if any(var_flags.values()):
                all_ok = False
            continue

        for var_name, required in var_flags.items():
            new_value = new_values[var_name]
            found = update_variable_in_file(file_path, var_name, new_value)
            if not found:
                if required:
                    print(f"  FAILED   {var_name}: not found in this file (expected it to be)")
                    all_ok = False
                else:
                    print(f"  skipped  {var_name}: not present in this file (expected)")
                continue

            verified = verify_variable_in_file(file_path, var_name, new_value)
            status = "OK" if verified else "MISMATCH AFTER WRITE"
            print(f"  {status:20} {var_name} = {new_value}")
            if not verified:
                all_ok = False

    print()
    if all_ok:
        print("All target files updated and verified successfully.")
    else:
        print("One or more updates FAILED or didn't verify -- see FAILED/MISMATCH lines above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
