# Threshold Alerts Implementation Plan

> Execute inline in the current feature branch, using test-driven-development and verification-before-completion. User approved the design in chat; no further design approval is required.

**Goal:** Add persistent background threshold alerts with WeCom, ServerChan and HTTPS Webhook notifications.

**Architecture:** `AlertService` owns configuration, rule state and delivery logs in the existing SQLite database. Its independent worker reads monitor snapshots without blocking metric collection. Admin APIs and a native standalone sidebar page exposes configuration and explicit test sends.

**Tech Stack:** Python 3.9+, standard library, existing psutil, native HTML/CSS/JavaScript.

**Spec:** `docs/superpowers/specs/2026-10-12-threshold-alerts-design.md`

## Constraints

- No new runtime dependencies; default notifications disabled.
- Strictly greater than threshold; independently evaluate every reported disk.
- Duration 0–3600 seconds; repeat interval 60–86400 seconds; stale sample cutoff 10 seconds.
- Credentials never returned by APIs, stored in browser preferences, or included in error logs.
- Administrator-only routes; mutation routes retain origin and CSRF checks.

## Task 1: Alert service

Files: create `alerts.py`, `tests/test_alerts.py`.

Interfaces: `AlertService(auth, sender=None)`, `status()`, `update(data)`, `evaluate(snapshot, now=None)`, `test_notification(hostname)`, `start(monitor)`.

- [x] Write failing tests for defaults, validation, persistence/redaction, strict threshold timing, repeat throttling, recovery, independent disks, stale data, disabled mode and failure records.
- [x] Run `python3 -m unittest discover -s tests -p test_alerts.py -v` and confirm the service is missing.
- [x] Implement configuration transaction in `auth.connection`, persistent bounded logs, monotonic state machine and worker lifecycle.
- [x] Add channel transport tests checking outgoing WeCom JSON, ServerChan form data, generic JSON, timeouts, redirects and business errors; implement the transport.
- [x] Run the alert tests to green.

## Task 2: APIs and deployment

Files: modify `server.py`, `Dockerfile`; create `tests/test_alert_api.py`.

Interfaces: `make_handler(monitor, auth, storage=None, alerts=None)`; alert admin GET/PATCH and POST test routes.

- [x] Write failing real HTTP tests for unauthenticated/member access, admin persistence, masked credentials, invalid configuration and missing CSRF; test sends use a local recording sender.
- [x] Implement routes, worker startup/stop, generic admin denial copy, and copy `alerts.py` into Docker image.
- [x] Run alert API tests and existing server/auth tests.

## Task 3: Settings and end-to-end verification

Files: create `static/alerts.js`, `tests/browser_alerts.py`; modify `static/auth.js`, `static/index.html`, `static/auth.css`, `static/app.js`, `README.md`.

Interfaces: `alertSettingsPanel()`, `bindAlertSettings()`, `resetAlertSettings()` invoked by standalone page lifecycle; administrator-only alerts navigation.

- [x] Write a browser test that configures generic HTTPS Webhook in a temporary service with a recording sender, saves, reloads, sends test notification, inspects log, and verifies member access and mobile overflow. Run to demonstrate absent alerts tab.
- [x] Implement form with independent rule toggles, duration and interval, credential replacement/clearing, channel guidance, explicit save/test actions and recent delivery records; keep async replies scoped to session/page.
- [x] Document concrete setup instructions, APIs, duration/repeat semantics and deployment updates.
- [x] Run unit suite and browser alerts/smoke tests; inspect screenshots and diff.
- [x] Request code review, address material findings, rerun affected verification.

## Completed verification

- 61 Python unit/integration tests passed.
- Alert browser QA passed, including password failure and delayed old-session response regressions, credential handling, responsive and dark layouts.
- Existing browser smoke suite passed without JavaScript errors.
- Docker image built using equivalent public ECR base image sources because the configured Docker Hub mirror returned HTTP 502. Project Dockerfile base sources remain unchanged.
- Disposable Docker alert check passed: real background threshold trigger, safe failure record, read-only root, settings/history and session persistence after restart.
- Independent review findings fixed and verified; no remaining findings.
- No real third-party messages were sent; production notification credentials remain to be configured by the administrator.

## Navigation and running-service correction

The user requested investigating inert controls and moving alerts to sidebar navigation. The running legacy-named `kong.service` was still executing the old backend and returned 404 for the alert route while serving new static files. The service was restarted after verification, preserving account and session data; its alert route now requires login (401), health is OK, and served scripts match the working tree. Alerts now live at `#alerts`, independently of monitoring data. Browser regressions cover missing-route recovery, metrics failure and recovery without overwriting unsaved inputs, member navigation restrictions and session races. All 61 backend tests, alert browser checks and existing browser smoke checks passed; the focused review found no remaining issues.
