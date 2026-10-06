"""
Receives webhook notifications from Onshape when the trebuchet document
changes, and records that a change happened so the main simulation loop
can pick it up (see `read_and_clear_change_flag()` below).

Onshape docs: https://onshape-public.github.io/docs/app-dev/webhook/

Notes on how Onshape calls this:
- Onshape sends a POST with a JSON body shaped like:
    {
      "timestamp": "...",
      "event": "onshape.model.lifecycle.changed",
      "documentId": "...",
      "workspaceId": "...",
      "elementId": "...",
      "webhookId": "...",
      "messageId": "..."
    }
- Right after you register a webhook, Onshape sends a "webhook.register"
  test notification to confirm your endpoint is reachable -- this endpoint
  must return HTTP 200 for the registration to succeed. Same applies to
  "webhook.ping" and "webhook.unregister".
- Onshape requires the endpoint be reachable via HTTPS with a CA-signed
  cert (no self-signed certs) once you're past local testing.

Optional signature verification:
Onshape only signs webhook payloads if you've set signing keys under your
company/enterprise account settings (Company Settings -> Webhooks). If
you're on a personal account or haven't set signing keys, this feature
isn't available -- ONSHAPE_SIGNING_KEY can just be left unset and
verification will be skipped.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import threading

from flask import Flask, request, jsonify

API_VERSION = "v16"

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("onshape_webhook")

# Set this only if you've configured a signing key in Onshape's
# Company Settings > Webhooks page. Leave unset to skip verification.
SIGNING_KEY = os.environ.get("ONSHAPE_SIGNING_KEY", "")

# File-based flag, NOT in-memory -- a plain Python module-level
# variable is only shared WITHIN one process. This Flask app and
# whatever separate script polls read_and_clear_change_flag() (e.g.
# test_webhook_only.py) are two different OS processes, each with its
# own independent copy of any in-memory state -- setting a variable
# here would never be visible over there. A file on disk is the
# simplest thing genuinely shared between them.
#
# Cross-process safety against a reader seeing a half-written file
# comes from os.replace() below being an atomic rename on both POSIX
# and Windows, not from _lock -- _lock only protects against Flask's
# own worker THREADS (still within this one process) racing each
# other if two webhook POSTs land close together.
_lock = threading.Lock()
FLAG_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".webhook_flag.json")


def verify_signature(timestamp: str, raw_body: bytes, signature_header: str) -> bool:
    """
    Per Onshape's docs, the expected signature is:
        Base64(HMAC-SHA256(<timestamp>.<raw request body>, signing_key))
    """
    print(f"verify_signature")
    if not SIGNING_KEY:
        return True  # no signing key configured, nothing to verify against

    if not timestamp or not signature_header:
        return False

    message = f"{timestamp}.".encode("utf-8") + raw_body
    digest = hmac.new(SIGNING_KEY.encode("utf-8"), message, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")

    return hmac.compare_digest(expected, signature_header)


def mark_design_changed(event_payload: dict):
    print(f"mark design changed")
    """Called whenever a real design-change event comes in. Writes the
    event to FLAG_FILE_PATH -- see the module-level comment above for
    why this can't just be an in-memory variable."""
    with _lock:
        tmp_path = FLAG_FILE_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(event_payload, f)
        os.replace(tmp_path, FLAG_FILE_PATH)  # atomic rename, both POSIX and Windows
    logger.info("Design change flagged (wrote %s): %s", FLAG_FILE_PATH, event_payload.get("event"))


def read_and_clear_change_flag():
    print(f"read and clear flag")
    """
    Call this from the main while loop, e.g.:

        while True:
            if read_and_clear_change_flag():
                # re-download STEP, re-upload to Omniverse, re-run sim, etc.
                ...
            time.sleep(1)

    Reads FLAG_FILE_PATH and deletes it if present -- this is what
    makes the flag visible to a SEPARATE process (e.g.
    test_webhook_only.py) polling this same function, unlike a plain
    in-memory variable which would only ever be visible within this
    Flask process itself.
    """
    with _lock:
        if not os.path.isfile(FLAG_FILE_PATH):
            return None
        try:
            with open(FLAG_FILE_PATH, "r", encoding="utf-8") as f:
                event = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Couldn't read/parse %s (%s) -- treating as no pending change.",
                            FLAG_FILE_PATH, e)
            return None
        try:
            os.remove(FLAG_FILE_PATH)
        except OSError:
            pass
    return event


@app.route("/onshape-webhook", methods=["POST"])
def onshape_webhook():
    print(f"onshape webhook")
    raw_body = request.get_data()
    timestamp = request.headers.get("X-onshape-webhook-timestamp", "")
    signature = (
        request.headers.get("X-onshape-webhook-signature-primary")
        or request.headers.get("X-onshape-webhook-signature-secondary")
        or ""
    )

    if not verify_signature(timestamp, raw_body, signature):
        logger.warning("Rejected webhook: signature verification failed")
        return jsonify({"error": "invalid signature"}), 401

    payload = request.get_json(silent=True)
    if payload is None:
        logger.warning("Rejected webhook: body was not valid JSON")
        return jsonify({"error": "invalid JSON"}), 400

    event_type = payload.get("event")
    logger.info("Received Onshape event: %s", event_type)

    # webhook.register / webhook.ping / webhook.unregister just need a 200
    # back to confirm this endpoint is alive -- no action needed for those.
    if event_type in ("webhook.register", "webhook.ping", "webhook.unregister"):
        return jsonify({"status": "ok"}), 200

    if event_type == "onshape.model.lifecycle.changed":
        mark_design_changed(payload)

    return jsonify({"status": "received"}), 200


if __name__ == "__main__":
    # For local testing. In production, run behind a proper WSGI server
    # (gunicorn, etc) with a valid HTTPS certificate in front of it.
    # use_reloader=False deliberately -- Flask's auto-reloader watches
    # EVERY .py file in this folder (confirmed: it was restarting this
    # process whenever rerun_local_test.py changed, a completely
    # unrelated script), and any webhook POST landing during a restart
    # gets dropped with no error shown anywhere. debug=True is kept
    # for its other benefits (error pages, etc), just not the
    # unstable auto-restart-on-any-file-change behavior.
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
