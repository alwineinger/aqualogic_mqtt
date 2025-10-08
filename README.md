# AquaLogic MQTT + Web UI (Enhanced)

## System Architecture

```text
 ┌───────────────────────┐
 │   Hayward PL-PLUS     │
 │  (RS-485 Controller)  │
 └─────────┬─────────────┘
           │ RS-485 (via TCP/serial adapter)
           ▼
 ┌───────────────────────┐
 │  aqualogic_mqtt       │
 │  (Python client)      │
 │                       │
 │ - Parses RS-485 data  │
 │ - Publishes MQTT      │
 │ - Hosts Web UI (Flask)│
 └───────┬─────┬─────────┘
         │     │
   MQTT  │     │  HTTP (Web UI + API)
         ▼     ▼
 ┌─────────────┐     ┌─────────────────────┐
 │ Mosquitto   │     │ Web Browser (UI)    │
 │ (Broker)    │     │ - LCD Display       │
 │             │     │ - Menu/Arrow/+/–    │
 │             │     │ - Filter button     │
 └─────────────┘     └─────────────────────┘
         │
         ▼
 ┌─────────────────────┐
 │ Home Assistant /    │
 │ Other MQTT Clients  │
 └─────────────────────┘
```

---

## Overview
This fork extends the original [`swilson/aqualogic`](https://github.com/swilson/aqualogic) project with:

- Embedded **Flask web UI** (`webapp.py`) for live panel display and keypress control.
- **Five navigation buttons** (Menu, Left, Right, Plus, Minus) in the UI + new **Filter** button.
- Mobile-optimized web UI (`static/index.html`) — designed for iPhone 16 Pro and responsive devices.
- Real **RS-485 LCD display text** streamed into the web UI via `panelmanager.text_updated`.
- Cleaned up **fallback suppression** in `client.py` so only live panel content shows.
- MQTT integration (unchanged from original) for state publishing and command control.

---

## File Changes

### `aqualogic_mqtt/client.py`
- Hooks into `controls.register_with_panel(self._panel)`.
- Updated `_panel_changed`:
  - Suppresses synthetic fallback display (POOL/TA/Salt).
  - Only updates UI when real LCD lines are present.
- Improved debug logging for LCD attributes.
- Fixed `logger.debug` bug (`name 'k' is not defined`).

### `aqualogic_mqtt/panelmanager.py`
- `text_updated()` now:
  - Forwards actual RS-485 display frames directly to the web UI.
  - Collapses each frame into a **single line string**.
- Keeps last-seen LCD line so UI always reflects what the panel is cycling.

### `aqualogic_mqtt/controls.py`
- Added centralized **DisplayState** and API helpers (`update_display`, `get_display`).
- Implemented **keypress queue** (`enqueue_key`, `drain_keypresses`).
- Added `_KEY_MAP` with support for:
  - `menu`, `left`, `right`, `plus`, `minus`
  - **`filter`** → `Keys.FILTER`
- Backend now recognizes `/api/key/filter` requests.

### `aqualogic_mqtt/webapp.py`
- Flask app serves:
  - `/api/display` → JSON LCD state.
  - `/api/key/<key>` → queues + drains keypress immediately.
- Logs POSTs clearly so you can confirm in `journalctl`.
- Static files (web UI) served from `/`.

### `aqualogic_mqtt/static/index.html`
- Rebuilt to mimic the original AquaLogic web panel look.
- Layout:
  - LCD-style green display window.
  - Navigation pad (Menu + arrows + +/-) **left aligned**.
  - **Filter** button added on the right.
- Responsive/mobile tweaks:
  - `viewport-fit=cover` and safe-area padding (handles iPhone Dynamic Island).
  - `100svh` + JS fallback to handle iOS Safari viewport height bugs.
  - Buttons sized ≥44×44px (Apple HIG).
  - Collapses display lines into one string, collapses extra spaces.
  - On very narrow screens, stacks Filter button under nav pad.

---

## Running as a Service

Systemd unit (`/etc/systemd/system/aqualogic_mqtt.service`):

```ini
[Service]
ExecStart=/home/andy/aqualogic_mqtt/venv-pool/bin/python   -m aqualogic_mqtt.client   -t 10.40.1.78:26   -m 10.40.1.61:1883   -e t_a t_p t_s cl_p cl_s salt s_p p_p l f aux1 aux2 aux3 aux4 spill v3 v4 h1 hauto sc pool spa   --http-host 0.0.0.0 --http-port 8089 -vvv
WorkingDirectory=/home/andy/aqualogic_mqtt
Restart=always
```

Reload + restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart aqualogic_mqtt
```

Check logs:

```bash
journalctl -u aqualogic_mqtt -f
```

---

## MQTT Broker (Mosquitto)

`/etc/mosquitto/mosquitto.conf`:

```
listener 1883 0.0.0.0
allow_anonymous true
log_dest file /var/log/mosquitto/mosquitto.log
persistence true
persistence_location /var/lib/mosquitto/
```

Sample subscription:

```bash
mosquitto_sub -h 10.40.1.61 -p 1883 -v -t 'homeassistant/#' -t 'aqualogic/#'
```

---

## Troubleshooting

### Web UI
- **Blank display** → Check `/api/display` directly:
  ```bash
  curl http://10.40.1.61:8089/api/display
  ```
  If `lines` are empty, ensure `panelmanager.text_updated` is firing.

- **Buttons do nothing**:
  - Confirm POST works:
    ```bash
    curl -X POST http://10.40.1.61:8089/api/key/menu
    ```
    Should return `{"ok":true,"key":"menu"}`.
  - Tail logs:
    ```bash
    journalctl -u aqualogic_mqtt -f | grep "queued key"
    ```
    You should see `queued key menu` then `sent 1 key(s)`.

- **Filter button not working**:
  - Ensure `_KEY_MAP` includes `"filter": Keys.FILTER`.
  - Hardware may expect `Keys.FILTER_PUMP` instead. If so, update mapping.

### Service won’t start
- Run manually in venv for immediate feedback:
  ```bash
  source ~/aqualogic_mqtt/venv-pool/bin/activate
  python -m aqualogic_mqtt.client -t 10.40.1.78:26 -m 10.40.1.61:1883 --http-port 8089 -vvv
  ```

- Look for:
  - Serial connection errors (`tcp host:port` mismatch).
  - MQTT broker connection failures.

### Display truncated horizontally
- The JS `collapseLines` function strips double-spaces:
  ```js
  const text = parts.join(' ').replace(/\s{2,}/g, ' ');
  ```
  If content still looks cut off, tweak `.display { min-width: … }` in CSS.

---

## Quick Test Checklist
1. `curl /api/display` shows live controller text (not synthetic fallback).
2. Web UI display matches pool panel LCD.
3. Buttons (`Menu, Left, Right, Plus, Minus, Filter`) send keys — confirmed in logs.
4. MQTT broker publishes state to `homeassistant/device/aqualogic/state`.
5. Service restarts cleanly via systemd.
