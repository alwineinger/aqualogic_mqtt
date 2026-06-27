# aqualogic_mqtt/webapp.py
from __future__ import annotations
import os
import base64
import logging
from functools import wraps
from flask import Flask, jsonify, request, send_from_directory
from . import controls
from .vsp import VspBusyError, VspDisabledError, VspInterlockError
from .equipment import EquipmentBusyError, EquipmentError

def _basic_auth(user: str | None, pw: str | None):
    if not user or not pw:
        return lambda f: f  # no-op
    token = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
    def decorator(f):
        @wraps(f)
        def inner(*args, **kwargs):
            if request.headers.get("Authorization") == token:
                return f(*args, **kwargs)
            return ("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm=\"AquaLogic\"'})
        return inner
    return decorator

def _enable_flask_logging(app: Flask):
    # Ensure our app logger is at least INFO and has a handler
    if app.logger.level > logging.INFO:
        app.logger.setLevel(logging.INFO)
    if not app.logger.handlers:
        h = logging.StreamHandler()
        h.setLevel(logging.INFO)
        app.logger.addHandler(h)

def create_app(static_dir: str | None = None, basic_user: str | None = None, basic_pass: str | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    _enable_flask_logging(app)
    require_auth = _basic_auth(basic_user, basic_pass)

    # ---- API ----
    @app.get("/api/display")
    @require_auth
    def api_display():
        return jsonify(controls.get_display())

    @app.get("/api/default-menu")
    @require_auth
    def api_default_menu():
        return jsonify(controls.get_default_menu())

    @app.get("/api/vsp")
    @require_auth
    def api_vsp_status():
        return jsonify(controls.get_vsp_status())

    @app.post("/api/vsp/speed")
    @require_auth
    def api_vsp_speed():
        body = request.get_json(silent=True) or {}
        preset = body.get("preset")
        if not preset:
            return jsonify({"ok": False, "error": "JSON field 'preset' is required"}), 400
        try:
            status = controls.request_vsp_preset(preset, body.get("lease_seconds"))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except VspDisabledError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 503
        except VspInterlockError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
        except VspBusyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 503
        return jsonify({"ok": True, "status": status}), 202

    @app.delete("/api/vsp/speed")
    @require_auth
    def api_vsp_clear():
        try:
            status = controls.clear_vsp_target()
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 503
        return jsonify({"ok": True, "status": status})

    @app.get("/api/equipment")
    @require_auth
    def api_equipment_status():
        return jsonify(controls.get_equipment_status())

    @app.get("/api/automation")
    @require_auth
    def api_automation_status():
        return jsonify(controls.get_automation_status())

    @app.post("/api/automation/manual")
    @require_auth
    def api_automation_manual():
        body = request.get_json(silent=True) or {}
        try:
            status = controls.set_manual_override(body)
        except (ValueError, TypeError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 503
        return jsonify({"ok": True, "status": status}), 202

    @app.delete("/api/automation/manual")
    @require_auth
    def api_automation_manual_clear():
        body = request.get_json(silent=True) or {}
        try:
            status = controls.clear_manual_override(body.get("field"))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 503
        return jsonify({"ok": True, "status": status})

    @app.get("/api/openclaw/spa")
    @require_auth
    def api_openclaw_spa_status():
        status = controls.get_automation_status()
        session = status.get("openclaw_spa_session")
        desired = status.get("desired") or {}
        return jsonify({
            "ok": True,
            "active": bool(status.get("enabled")) and desired.get("source") == "calendar" and session is not None,
            "session": session,
            "desired": desired,
            "automation_enabled": status.get("enabled"),
        })

    @app.post("/api/openclaw/spa")
    @require_auth
    def api_openclaw_spa_start():
        body = request.get_json(silent=True) or {}
        try:
            status = controls.activate_openclaw_spa(body)
        except (ValueError, TypeError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 503
        return jsonify({"ok": True, "status": status}), 202

    @app.delete("/api/openclaw/spa")
    @require_auth
    def api_openclaw_spa_stop():
        body = request.get_json(silent=True) or {}
        try:
            status = controls.stop_openclaw_spa(body.get("session_id"))
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 503
        return jsonify({"ok": True, "status": status}), 202

    @app.post("/api/control/switch")
    @require_auth
    def api_control_switch():
        body = request.get_json(silent=True) or {}
        try:
            result = controls.set_equipment_switch(body.get("control"), body.get("target"))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except (EquipmentError, RuntimeError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
        return jsonify(result), 202

    @app.post("/api/control/mode")
    @require_auth
    def api_control_mode():
        body = request.get_json(silent=True) or {}
        try:
            status = controls.request_equipment_mode(body.get("target"))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except (EquipmentBusyError, EquipmentError, RuntimeError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
        return jsonify({"ok": True, "status": status}), 202

    @app.post("/api/control/pump-speed")
    @require_auth
    def api_control_pump_speed():
        body = request.get_json(silent=True) or {}
        try:
            status = controls.request_vsp_preset(body.get("target"), body.get("lease_seconds"))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except (VspBusyError, VspDisabledError, VspInterlockError, RuntimeError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
        return jsonify({"ok": True, "status": status}), 202

    @app.post("/api/key/<keyname>")
    @require_auth
    def api_keypress(keyname):
        ok = controls.enqueue_key(keyname)
        app.logger.info(f"POST /api/key/{keyname} -> queued={ok}")
        # Send immediately (don’t wait for next panel update)
        try:
            controls.drain_keypresses()
            app.logger.info("controls.drain_keypresses() invoked")
        except Exception as e:
            app.logger.info(f"controls.drain_keypresses() error: {e}")
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
