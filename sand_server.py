#!/usr/bin/env python3
"""
sand_server.py — runs on the SAND Pi
Timed actuator control for sand-dumping. Drives an H-bridge / dual-relay
(IN1/IN2) one way to EXTEND (dump) and the other to RETRACT (home), each for a
fixed "duty cycle" period, then stops. No limit switches — travel is time-based.

Endpoints:
  POST /sand    body: {"cmd":"extend"|"dump"|"retract"|"home"|"stop", "duty":<sec?>}
  GET  /sand    -> {"state":"idle|extending|retracting", "duty_default":40, ...}

`duty` is optional per-command; if absent, DUTY_CYCLE_SEC is used. The dashboard
sends the admin-configured value with each command.

Start: python3 sand_server.py   (default port 8082)
"""

import json, threading, time, sys, signal, logging
import RPi.GPIO as GPIO
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

# ── config ──────────────────────────────────────────────────────────────────
IN1_PIN  = 20          # H-bridge / relay direction A  (board pin 38)
IN2_PIN  = 21          # H-bridge / relay direction B  (board pin 40)
PORT     = 8082
VERSION  = "1.0.0"

DUTY_CYCLE_SEC = 40    # default run time per extend/retract if none supplied
DUTY_MIN, DUTY_MAX = 1, 120   # clamp — never run the actuator longer than this

logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("sand")

# ── state ───────────────────────────────────────────────────────────────────
stop_evt    = threading.Event()
worker      = None
state_label = "idle"            # idle | extending | retracting
state_lock  = threading.Lock()
cmd_lock    = threading.Lock()  # serialize start/stop — one motion at a time


def setup():
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(IN1_PIN, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(IN2_PIN, GPIO.OUT, initial=GPIO.LOW)


def _coast():
    """Both low = motor off (never both high — that shorts an H-bridge)."""
    GPIO.output(IN1_PIN, GPIO.LOW)
    GPIO.output(IN2_PIN, GPIO.LOW)


def _run(direction, duty):
    """Drive one direction for `duty` seconds (or until stop), then coast."""
    global state_label
    a, b = (GPIO.HIGH, GPIO.LOW) if direction == "extend" else (GPIO.LOW, GPIO.HIGH)
    GPIO.output(IN1_PIN, a)
    GPIO.output(IN2_PIN, b)
    stop_evt.wait(timeout=duty)            # returns early if stop_evt is set
    _coast()
    with state_lock:
        state_label = "idle"
    log.info(f"{direction} done (coast)")


def _stop_locked():
    global worker, state_label
    stop_evt.set()
    if worker and worker.is_alive():
        worker.join(timeout=DUTY_MAX + 1)
    worker = None
    _coast()
    with state_lock:
        state_label = "idle"


def start(direction, duty):
    global worker, state_label
    duty = max(DUTY_MIN, min(int(duty), DUTY_MAX))
    with cmd_lock:
        _stop_locked()                     # cancel any current motion first
        stop_evt.clear()
        with state_lock:
            state_label = "extending" if direction == "extend" else "retracting"
        worker = threading.Thread(target=_run, args=(direction, duty), daemon=True)
        worker.start()
    log.info(f"start {direction} for {duty}s")


def stop():
    with cmd_lock:
        _stop_locked()
    log.info("stop")


def shutdown(sig=None, frame=None):
    stop()
    GPIO.cleanup()
    log.info("shutdown")
    sys.exit(0)


# ── HTTP ────────────────────────────────────────────────────────────────────
class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path.split("?")[0] == "/sand":
            with state_lock:
                st = state_label
            self._json(200, {"state": st, "duty_default": DUTY_CYCLE_SEC,
                             "version": VERSION})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/sand":
            self._json(404, {"error": "not found"}); return
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            self._json(400, {"error": "bad json"}); return
        cmd  = str(data.get("cmd", "")).lower()
        duty = data.get("duty", DUTY_CYCLE_SEC)
        log.info(f"POST cmd={cmd!r} duty={duty} from {self.client_address[0]}")
        if cmd in ("extend", "dump"):
            start("extend", duty)
        elif cmd in ("retract", "home"):
            start("retract", duty)
        elif cmd == "stop":
            stop()
        else:
            self._json(400, {"error": f"unknown cmd: {cmd}"}); return
        with state_lock:
            self._json(200, {"ok": True, "state": state_label})


if __name__ == "__main__":
    setup()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT,  shutdown)
    log.info(f"[sand_server] v{VERSION} listening on port {PORT} "
             f"(IN1={IN1_PIN}, IN2={IN2_PIN}, duty={DUTY_CYCLE_SEC}s)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
