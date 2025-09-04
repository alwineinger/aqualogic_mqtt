# aqualogic_mqtt/webapp.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import argparse
from functools import wraps
from typing import Optional

from flask import Flask, jsonify, request, send_from_directory, Response

from . import controls


def _basic_auth_middleware(app: Flask, user: Optional[str], passwd: Optional[str]):
    if not user:
        return  # disabled

    def check_auth(hdr: str) -> bool:
        try:
            scheme, b64 = hdr.split(" ", 1)
        except ValueError:
            return False
        if scheme.lower() != "basic":
            return False
        import base64
        try:
            decoded = base64.b64decode(b64).decode("utf-8")
        except Exception:
            return False
        u, p = decoded.split(":", 1)
        return (u == user) and (p == (passwd or ""))

    def require_auth(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            hdr = request.headers.get("Authorization")
            if not hdr or not check_auth(hdr):
                resp = Response(status=401)
                resp.headers["WWW-Authenticate"] = 'Basic realm="Aqualogic"'
                return resp
            return f(*args, **kwargs)
        return wrapper

    # Apply to all routes except static file serving
    for rule in list(app.url_map.iter_rules()):
        if rule.endpoint in ("static",):
            continue
        view = app.view_functions[rule.endpoint]
        app.view_functions[rule.endpoint] = require_auth(view)


def create_app(static_dir: Optional[str] = None,
               basic_user: Optional[str] = None,
               basic_pass: Optional[str] = None) -> Flask:
    app = Flask(__name__, static_folder=None)

    # Static single-page app index
    _static_dir = static_dir or os.path.join(os.path.dirname(__file__), "static")

    @app.get("/")
    def index():
        return send_from_directory(_static_dir, "index.html")

    @app.get("/assets/<path:filename>")
    def assets(filename):
        return send_from_directory(_static_dir, filename)

    # API: display state
    @app.get("/api/display")
    def api_display():
        return jsonify(controls.get_display())

    # API: keypress
    @app.post("/api/key/<key>")
    def api_key(key: str):
        try:
            controls.press(key)
            # We return immediately; reliability is ensured by draining within
            # the serial worker's write window.
            return jsonify({"ok": True})
        except KeyError:
            return jsonify({"ok": False, "error": f"unknown key: {key}"}), 400

    # Attach basic auth if configured
    _basic_auth_middleware(app, basic_user, basic_pass)

    return app


def main():  # pragma: no cover
    parser = argparse.ArgumentParser(description="Aqualogic Web UI server")
    parser.add_argument("--host", default=os.getenv("AQUALOGIC_HTTP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AQUALOGIC_HTTP_PORT", "8080")))
    parser.add_argument("--static-dir", default=os.getenv("AQUALOGIC_STATIC_DIR"))
    parser.add_argument("--basic-user", default=os.getenv("AQUALOGIC_HTTP_USER"))
    parser.add_argument("--basic-pass", default=os.getenv("AQUALOGIC_HTTP_PASS"))
    args = parser.parse_args()

    app = create_app(static_dir=args.static_dir, basic_user=args.basic_user, basic_pass=args.basic_pass)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":  # pragma: no cover
    main()


