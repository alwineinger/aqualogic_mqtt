---
name: plplus-pool-spa
description: Operate Andy's PLPLUS pool/spa safely through aqualogic_mqtt. OpenClaw owns calendar timing, preheat calculations, weather checks, and approvals; PLPLUS only executes approved sessions.
---

# PLPLUS Pool/Spa Operations

## Ownership boundary

- OpenClaw owns calendar polling, event modifications, preheat planning,
  weather checks, and approval policy.
- Do not ask `aqualogic_mqtt` to poll or interpret a calendar.
- After an approved scheduled/manual spa decision, call the PLPLUS session API.
- Hubitat remains a HomeKit control surface; do not use its pool macros for
  OpenClaw automation.

## PLPLUS endpoint

Base URL: `http://127.0.0.1:8089`

Start an approved session:

```bash
curl -fsS -X POST http://127.0.0.1:8089/api/openclaw/spa \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"openclaw-<event-id>"}'
```

This requests Spa mode, Filter on, heater relay on, and Auto Heat. The API does
not run a software timer; PLPLUS Spa CountDn provides the hardware failsafe.

Stop when the event ends, is cancelled, or Andy says they are done:

```bash
curl -fsS -X DELETE http://127.0.0.1:8089/api/openclaw/spa \
  -H 'Content-Type: application/json' -d '{}'
```

Read status:

```bash
curl -fsS http://127.0.0.1:8089/api/openclaw/spa
curl -fsS http://127.0.0.1:8089/api/equipment
```

Wait and verify the returned desired state and equipment state. Calendar spa
sessions have priority over manual override, cleanout, and the normal schedule.

## Calendar behavior

- Continue using OpenClaw's existing iCloud calendar configuration.
- Only exact event names `Spa` and `spa` qualify.
- Apply existing weather/approval and preheat rules before activation.
- Event edits or cancellation must update/stop the PLPLUS session.

## Safety

- Hardware Service mode inhibits writes.
- Never bypass the session API with raw Pool/Spa key sequences.
- When stopping, verify Pool mode and that Auto Heat matches the saved
  `pool_heat_enabled` preference after valve settling.
