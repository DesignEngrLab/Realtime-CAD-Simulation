"""
Registers an Onshape webhook that notifies our listener whenever the
trebuchet document is changed.

Docs: https://onshape-public.github.io/docs/app-dev/webhook/

How to get what you need:
- ACCESS_KEY / SECRET_KEY: Onshape Developer Portal
  (https://cad.onshape.com/appstore/dev-portal) -> API Keys -> Create new API key.
  These act as a username/password pair for Basic Auth.
- DOCUMENT_ID: found in the document's Onshape URL:
  https://cad.onshape.com/documents/<DOCUMENT_ID>/w/<workspaceId>/e/<elementId>
- CALLBACK_URL: the public HTTPS URL where onshape_webhook_listener.py is
  reachable (e.g. via ngrok while testing, or wherever it's deployed).
  Onshape requires a valid CA-signed HTTPS cert -- self-signed certs are
  rejected, and http:// URLs will only work if Onshape explicitly allows
  http for your account type (generally not recommended past testing).

Run:
    pip install requests
    python register_webhook.py
"""

import os
import requests


API_VERSION = "v16"

ONSHAPE_BASE_URL = "https://cad.onshape.com"  # swap "cad" for your company domain if on Enterprise

ACCESS_KEY = os.environ["ONSHAPE_ACCESS_KEY"]
SECRET_KEY = os.environ["ONSHAPE_SECRET_KEY"]

#DOCUMENT_ID = os.environ["ONSHAPE_DOCUMENT_ID"]
DOCUMENT_ID = "6c0d2b9b726f93f6e30525f2"

# Onshape's own TypeScript client source lists workspaceId/elementId as
# valid additional scoping fields alongside documentId (not just
# documentId alone) -- and multiple developers on Onshape's own forum
# report the EXACT symptom seen here (webhook registers fine, the
# webhook.register handshake succeeds, but zero real notifications
# ever arrive) traced back to relying on documentId alone. Same
# workspace/assembly IDs already used elsewhere in this pipeline
# (ExperimentCodeComplete.py's WID/EIDa).
WORKSPACE_ID = "e60b006b496816beb3fba67d"
ELEMENT_ID = "9524668d3f639b4055de0e2b"

#CALLBACK_URL = os.environ["WEBHOOK_CALLBACK_URL"]  # e.g. "https://your-server.com/onshape-webhook"
CALLBACK_URL = "https://gender-freckled-constant.ngrok-free.dev/onshape-webhook"

# onshape.model.lifecycle.changed fires on essentially any change to the model
# (edits, new features, etc). If you only care about new versions being
# published rather than every edit, use "onshape.model.lifecycle.createversion"
# instead.
EVENT_TYPE = "onshape.model.lifecycle.changed"


def check_callback_reachable() -> bool:
    """
    Hits CALLBACK_URL directly, the same way Onshape would, BEFORE
    registering -- catches a dead/wrong tunnel URL immediately with a
    clear error, instead of registering against something unreachable
    and finding out later via silence (which is what "no request ever
    shows up in the Flask log" looks like from the outside).

    NOTE: ngrok's free tier without a reserved/static domain assigns a
    NEW random subdomain every time the tunnel restarts -- if you've
    restarted ngrok since CALLBACK_URL was last set/registered, this
    URL is almost certainly stale even if it looks otherwise normal.
    """
    try:
        resp = requests.post(CALLBACK_URL, json={"event": "manual-preflight-check"}, timeout=10)
        print(f"Reachability check: POST {CALLBACK_URL} -> HTTP {resp.status_code}")
        return resp.status_code < 500
    except requests.exceptions.RequestException as e:
        print(f"Reachability check FAILED: could not reach {CALLBACK_URL} at all ({e})")
        print("This means Onshape wouldn't be able to reach it either -- check that:")
        print("  1. onshape_webhook_listener.py is actually running")
        print("  2. Your tunnel (ngrok etc.) is actually running and forwarding to port 5000")
        print("  3. CALLBACK_URL above matches the CURRENT tunnel URL exactly (ngrok's free")
        print("     tier without a reserved domain generates a NEW URL every restart)")
        return False


def register_webhook():
    resp = requests.post(
        f"{ONSHAPE_BASE_URL}/api/{API_VERSION}/webhooks",
        auth=(ACCESS_KEY, SECRET_KEY),
        headers={
            "Accept": "application/json;charset=UTF-8; qs=0.09",
            "Content-Type": "application/json;charset=UTF-8; qs=0.09",
        },
        json={
            "documentId": DOCUMENT_ID,
            # The actual scoping mechanism -- confirmed against a real,
            # working example from Onshape's own forum for this exact
            # event type. Top-level workspaceId/elementId fields (what
            # was here before) get silently accepted but dropped --
            # this "filter" string expression is what Onshape actually
            # uses to decide whether an event matches this webhook.
            "filter": (
                f"{{$DocumentId}} = '{DOCUMENT_ID}' && "
                f"{{$WorkspaceId}} = '{WORKSPACE_ID}' && "
                f"{{$ElementId}} = '{ELEMENT_ID}'"
            ),
            "events": [EVENT_TYPE],
            "options": {"collapseEvents": True},  # collapse rapid-fire edits into one notification
            "url": CALLBACK_URL,
            "isTransient": False,  # prevents Onshape auto-deleting this webhook after inactivity
        },
    )

    if resp.status_code >= 300:
        print(f"Failed to register webhook: {resp.status_code}")
        print(resp.text)
        return None

    data = resp.json()
    print("Webhook registered successfully.")
    print(f"  webhookId: {data.get('id')}")
    print("Save this webhookId if you want to update or delete this webhook later.")
    return data


def print_registered_webhooks():
    """
    Prints what Onshape ACTUALLY currently has on file for this
    account -- the fastest way to catch a stale CALLBACK_URL: if the
    "url" field printed here doesn't match your current, live tunnel
    URL, that's exactly why nothing is arriving (Onshape is faithfully
    POSTing to whatever it has registered, which just isn't your
    tunnel anymore).

    Prints the FULL raw item (not just a few picked fields) so a field
    like documentId showing up as null/missing here is visible for
    real, not hidden behind a summary line that might be looking in
    the wrong place.
    """
    import json as _json
    webhooks = list_webhooks()
    items = webhooks.get("items", webhooks) if isinstance(webhooks, dict) else webhooks
    if not items:
        print("No webhooks currently registered for this account.")
        return
    print(f"{len(items)} webhook(s) currently registered:")
    for wh in items:
        print(_json.dumps(wh, indent=2))


def delete_stale_webhooks_for_this_document():
    """
    Deletes any EXISTING webhook already registered for DOCUMENT_ID
    before creating a new one -- without this, re-running
    register_webhook.py every time the tunnel/listener gets restarted
    during testing just accumulates dead registrations pointed at
    expired URLs, rather than replacing them.
    """
    webhooks = list_webhooks()
    items = webhooks.get("items", webhooks) if isinstance(webhooks, dict) else webhooks
    if not items:
        return
    for wh in items:
        if wh.get("documentId") == DOCUMENT_ID:
            print(f"Deleting stale existing webhook for this document: id={wh.get('id')}")
            delete_webhook(wh["id"])


def list_webhooks():
    """Handy for confirming what's currently registered for your account."""
    resp = requests.get(
        f"{ONSHAPE_BASE_URL}/api/{API_VERSION}/webhooks",
        auth=(ACCESS_KEY, SECRET_KEY),
        headers={"Accept": "application/json;charset=UTF-8; qs=0.09"},
    )
    resp.raise_for_status()
    return resp.json()


def delete_webhook(webhook_id: str):
    resp = requests.delete(
        f"{ONSHAPE_BASE_URL}/api/{API_VERSION}/webhooks/{webhook_id}",
        auth=(ACCESS_KEY, SECRET_KEY),
        headers={"Accept": "application/json;charset=UTF-8; qs=0.09"},
    )
    resp.raise_for_status()
    print(f"Deleted webhook {webhook_id}")


if __name__ == "__main__":
    print("=== What's currently registered (before doing anything) ===")
    print_registered_webhooks()

    print("\n=== Checking CALLBACK_URL is actually reachable right now ===")
    if not check_callback_reachable():
        print("\nNOT registering -- fix reachability first, or this will just silently "
              "fail the same way as before.")
    else:
        print("\n=== Cleaning up any stale existing registration for this document ===")
        delete_stale_webhooks_for_this_document()

        print("\n=== Registering ===")
        register_webhook()

        print("\n=== Confirming what's registered now ===")
        print_registered_webhooks()
