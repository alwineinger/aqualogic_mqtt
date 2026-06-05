# aqualogic_mqtt/webapp.py
from __future__ import annotations
import os
import base64
import logging
import time
from functools import wraps
from flask import Flask, jsonify, request, send_from_directory, current_app
from . import controls

# States mapping for /api/status
try:
    from aqualogic.states import States
    _STATE_MAP = [
        (States.POOL, "pool"),
        (States.SPA, "spa"),
        (States.FILTER, "filter"),
        (States.LIGHTS, "lights"),
        (States.AUX_1, "aux1"),
        (States.AUX_2, "aux2"),
        (States.AUX_3, "aux3"),
        (States.AUX_4, "aux4"),
        (States.AUX_5, "aux5"),
        (States.AUX_6, "aux6"),
        (States.AUX_7, "aux7"),
        (States.AUX_8, "aux8"),
        (States.AUX_9, "aux9"),
        (States.AUX_10, "aux10"),
        (States.AUX_11, "aux11"),
        (States.AUX_12, "aux12"),
        (States.AUX_13, "aux13"),
        (States.AUX_14, "aux14"),
        (States.HEATER_1, "heater_1"),
        (States.HEATER_AUTO_MODE, "heater_auto_mode"),
        (States.SUPER_CHLORINATE, "super_chlorinate"),
        (States.CHECK_SYSTEM, "check_system"),
    ]
    _svc = getattr(States, 'SERVICE', None)
    if _svc is not None:
        _STATE_MAP.append((_svc, "service"))
except ImportError:
    _STATE_MAP = []

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

    # ---- HAL structured API ----

    @app.get("/api/status")
    @require_auth
    def api_status():
        panel = current_app.config.get('PANEL')
        if panel is None:
            return jsonify({"error": "panel not available"}), 503

        # Build states list and LED mapping
        leds = {}
        active_states: list[str] = []
        for state, led_key in _STATE_MAP:
            try:
                val = bool(panel.get_state(state))
            except Exception:
                val = False
            leds[led_key] = val
            if val:
                active_states.append(state.name)

        # Include onish() style fallbacks for LEDs in case get_state differs from direct attrs (parity with client._panel_changed)
        def onish(v):
            return v in (True, 'ON', 'On', 'on', '1', 1)
        try:
            leds['pool'] = onish(getattr(panel, 'pool', None)) or leds.get('pool', False)
            leds['spa'] = onish(getattr(panel, 'spa', None)) or leds.get('spa', False)
            leds['filter'] = onish(getattr(panel, 'filter_pump', getattr(panel, 'f', None))) or leds.get('filter', False)
            leds['lights'] = onish(getattr(panel, 'lights', getattr(panel, 'l', None))) or leds.get('lights', False)
        except Exception:
            pass

        disp = controls.get_display()
        # Build the clean LED dict for the display sub-object (only keys with explicit LED relevance)
        display_leds = {k: v for k, v in leds.items() if k in {'pool','spa','filter','lights','aux1','aux2','aux3','aux4','aux5','aux6','aux7','aux8','aux9','aux10','aux11','aux12','aux13','aux14','heater_1','heater_auto_mode','super_chlorinate','check_system','service'}}

        now = time.time()
        return jsonify({
            "pool_temp": getattr(panel, 'pool_temp', None),
            "spa_temp": getattr(panel, 'spa_temp', None),
            "air_temp": getattr(panel, 'air_temp', None),
            "pool_chlorinator": getattr(panel, 'pool_chlorinator', None),
            "spa_chlorinator": getattr(panel, 'spa_chlorinator', None),
            "salt_level": getattr(panel, 'salt_level', None),
            "pump_speed": getattr(panel, 'pump_speed', None),
            "pump_power": getattr(panel, 'pump_power', None),
            "is_metric": getattr(panel, 'is_metric', False),
            "status": getattr(panel, 'status', 'OK'),
            "check_system_msg": getattr(panel, 'check_system_msg', None),
            "states": active_states,
            "display": {
                "lines": list(disp.get('lines', [])),
                "blink": list(disp.get('blink', [])),
                "leds": display_leds,
            },
            "updated_at": disp.get('updated_at', now),
        })

    @app.post("/api/keys")
    @require_auth
    def api_keys():
        key = None
        if request.is_json:
            data = request.get_json(silent=True) or {}
            if isinstance(data, dict):
                key = data.get('key')
        if not key:
            key = request.args.get('key', '')
        if not key and request.form:
            key = request.form.get('key', '')
        if not key:
            return jsonify({"error": "missing key"}), 400
        ok = controls.enqueue_key(key)
        try:
            controls.drain_keypresses()
        except Exception as e:
            app.logger.info(f"controls.drain_keypresses() error in /api/keys: {e}")
        app.logger.info(f"POST /api/keys key={key} queued={ok}")
        return jsonify({"ok": bool(ok), "key": key})

    @app.get("/api/health")
    @require_auth
    def api_health():
        pman = current_app.config.get('PANEL_MANAGER')
        if pman is None:
            return jsonify({"connected": False, "last_update_seconds_ago": None, "adapter": "source"})
        try:
            connected = bool(pman.is_updating())
            age = pman.get_last_update_age()
            age_f = round(float(age), 2) if age is not None else None
        except Exception:
            connected = False
            age_f = None
        return jsonify({"connected": connected, "last_update_seconds_ago": age_f, "adapter": "source"})

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