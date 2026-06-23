#!/usr/bin/env python3
"""
motor_server.py — runs on the MOTOR Pi
Exposes a tiny HTTP API so the camera Pi dashboard can control the NEMA 17.

Endpoints:
  POST /motor   body: {"cmd": "up"|"down"|"stop"}
  GET  /motor   returns {"state": "up"|"down"|"stopped"}

Start: python3 motor_server.py
Default port: 8081
"""

import json, math, threading, time, sys, signal
import RPi.GPIO as GPIO
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

# ── pin config ────────────────────────────────────────────────────────────────
STEP_PIN     = 17
DIR_PIN      = 27
EN_PIN       = 22

DELAY_START  = 0.012
DELAY_MIN    = 0.003
RAMP_STEPS   = 30

PORT         = 8081
VERSION      = "1.1.0"

# ── motor state ───────────────────────────────────────────────────────────────
stop_evt     = threading.Event()   # set => the stepping loop must exit now
motor_thread = None
state_label  = "stopped"
state_lock   = threading.Lock()    # guards state_label
cmd_lock     = threading.Lock()    # serializes start/stop so only one worker runs


def setup():
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(STEP_PIN, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(DIR_PIN,  GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(EN_PIN,   GPIO.OUT, initial=GPIO.HIGH)


def enable():
    GPIO.output(EN_PIN, GPIO.LOW)
    time.sleep(0.1)


def disable():
    GPIO.output(EN_PIN, GPIO.HIGH)


def step_once(delay):
    GPIO.output(STEP_PIN, GPIO.HIGH)
    time.sleep(delay)
    GPIO.output(STEP_PIN, GPIO.LOW)
    time.sleep(delay)


def run_continuous(cw):
    GPIO.output(DIR_PIN, GPIO.HIGH if cw else GPIO.LOW)
    enable()
    i = 0
    while not stop_evt.is_set():
        if i < RAMP_STEPS:
            t     = i / RAMP_STEPS
            t     = 0.5 - 0.5 * math.cos(math.pi * t)
            delay = DELAY_START + (DELAY_MIN - DELAY_START) * t
        else:
            delay = DELAY_MIN
        step_once(delay)
        i += 1
    disable()
    print("[motor] stepping thread exited")


def _stop_locked():
    """Stop the worker. Caller must hold cmd_lock."""
    global motor_thread, state_label
    stop_evt.set()
    if motor_thread and motor_thread.is_alive():
        motor_thread.join(timeout=2)
    motor_thread = None
    with state_lock:
        state_label = "stopped"


def start_motor(cw=True):
    global motor_thread, state_label
    with cmd_lock:
        _stop_locked()                 # ensure no existing worker
        stop_evt.clear()
        with state_lock:
            state_label = "up" if cw else "down"
        motor_thread = threading.Thread(target=run_continuous, args=(cw,), daemon=True)
        motor_thread.start()
    print(f"[motor] start {'UP (CW)' if cw else 'DOWN (CCW)'}")


def stop_motor():
    with cmd_lock:
        _stop_locked()
    print("[motor] stop")


def shutdown(sig=None, frame=None):
    stop_motor()
    disable()
    GPIO.cleanup()
    print("[motor] shutdown")
    sys.exit(0)


# ── HTTP server ───────────────────────────────────────────────────────────────

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
        if self.path.split("?")[0] == "/motor":
            with state_lock:
                self._json(200, {"state": state_label, "version": VERSION})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/motor":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            self._json(400, {"error": "bad json"})
            return
        cmd = data.get("cmd", "")
        print(f"[motor] POST cmd={cmd!r} from {self.client_address[0]}")
        if cmd == "up":
            start_motor(cw=True)
        elif cmd == "down":
            start_motor(cw=False)
        elif cmd == "stop":
            stop_motor()
        else:
            self._json(400, {"error": f"unknown cmd: {cmd}"})
            return
        with state_lock:
            self._json(200, {"ok": True, "state": state_label})


if __name__ == "__main__":
    setup()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT,  shutdown)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[motor_server] v{VERSION} listening on port {PORT}")
    server.serve_forever()
