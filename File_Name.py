"""
onshape_api.py

Python translation of FileName.jl. Talks to the Onshape REST API to
fetch a part studio's `name` field.

SECURITY NOTE: the original .jl file had live ONSHAPE_ACCESS_KEY /
ONSHAPE_SECRET_KEY values hardcoded as fallback defaults. Those keys
should be rotated in your Onshape account and never committed to a
file again -- set them as real environment variables instead:

    Windows (PowerShell):
        setx ONSHAPE_ACCESS_KEY "your_key_here"
        setx ONSHAPE_SECRET_KEY "your_secret_here"

    macOS/Linux:
        export ONSHAPE_ACCESS_KEY="your_key_here"
        export ONSHAPE_SECRET_KEY="your_secret_here"

No fallback defaults are included here on purpose -- if the env vars
aren't set, this raises immediately instead of silently using a stale
or exposed key.
"""

import os
import base64
import requests

BASE_URL = "https://cad.onshape.com"


def _get_credentials():
    access_key = os.environ.get("ONSHAPE_ACCESS_KEY")
    secret_key = os.environ.get("ONSHAPE_SECRET_KEY")

    if not access_key or not secret_key:
        raise EnvironmentError(
            "ONSHAPE_ACCESS_KEY and ONSHAPE_SECRET_KEY must be set as "
            "environment variables."
        )
    return access_key, secret_key


def get_partstudio_name(did: str, wid: str = None, eid: str = None) -> str:
    """
    Fetch the `name` field for a document from the Onshape API.
    """
    access_key, secret_key = _get_credentials()

    #May need to update v16 to the latest api version as it updates
    url = f"{BASE_URL}/api/v16/documents/{did}"

    token = base64.b64encode(f"{access_key}:{secret_key}".encode()).decode()
    headers = {
        "Authorization": f"Basic {token}",
        "Accept": "application/json;charset=UTF-8;qs=0.09",
        "Content-Type": "application/json",
    }

    params = {}  # optional query parameters, mirrors the Julia `params = Dict()`

    response = requests.get(url, params=params, headers=headers, timeout=15)
    response.raise_for_status()

    data = response.json()
    print(data["name"])
    return data["name"]


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python File_Name.py <document_id> [workspace_id] [element_id]")
        sys.exit(1)

    did_arg = sys.argv[1]
    wid_arg = sys.argv[2] if len(sys.argv) > 2 else None
    eid_arg = sys.argv[3] if len(sys.argv) > 3 else None

    get_partstudio_name(did_arg, wid_arg, eid_arg)