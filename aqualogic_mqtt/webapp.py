# aqualogic_mqtt/webapp.py
from __future__ import annotations
import os
import base64
from functools import wraps
from flask import Flask, jsonify, request, send_from_directory
from . import controls

def _basic_auth(user: str | None, pw: str | None):
    if not user or not pw:
        return lambda f: f  # no-op
    token = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
    def decorator(f):
        @wraps(f)
        def inner(*args, **kwargs):
            if request.headers.get("Authorization") == token:
                return f(*args, **kwargs)
            return ("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="AquaLogic"'})
        return inner
    return decorator

def create_app(static_dir: str | None = None, basic_user: str | None = None, basic_pass: str | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    require_auth = _basic_auth(basic_user, basic_pass)

    # ---- API ----
    @app.get("/api/display")
    @require_auth
    def api_display():
        return jsonify(controls.get_display())

    @app.post("/api/key/<keyname>")
    @require_auth
    def api_keypress(keyname):
        ok = controls.enqueue_key(keyname)
        return jsonify({"ok": bool(ok), "key": keyname})

    # ---- Static UI ----
    _static_dir = (static_dir or os.path.join(os.path.dirname(__file__), "static"))
    @app.get("/")
    @require_auth
    def index():
        return send_from_directory(_static_dir, "index.html")

    @app.get("/<path:path>")
    @require_auth
    def static_passthrough(path):
        return send_from_directory(_static_dir, path)

    return app