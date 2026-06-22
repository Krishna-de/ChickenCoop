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

# ── motor state ───────────────────────────────────────────────────────────────
running      = False
direction    = True   # True = CW (up)
motor_thread = None
state_label  = "stopped"
state_lock   = threading.Lock()


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


def run_continuous():
    global running
    i = 0
    enable()
    while running:
        if i < RAMP_STEPS:
            t     = i / RAMP_STEPS
            t     = 0.5 - 0.5 * math.cos(math.pi * t)
            delay = DELAY_START + (DELAY_MIN - DELAY_START) * t
        else:
            delay = DELAY_MIN
        step_once(delay)
        i += 1
    disable()


def start_motor(cw=True):
    global running, direction, motor_thread, state_label
    if running:
        _stop()
    with state_lock:
        direction   = cw
        state_label = "up" if cw else "down"
    GPIO.output(DIR_PIN, GPIO.HIGH if cw else GPIO.LOW)
    running      = True
    motor_thread = threading.Thread(target=run_continuous, daemon=True)
    motor_thread.start()
    print(f"[motor] {'UP (CW)' if cw else 'DOWN (CCW)'}")


def _stop():
    global running, state_label
    running = False
    if motor_thread:
        motor_thread.join(timeout=2)
    with state_lock:
        state_label = "stopped"
    print("[motor] stopped")


def stop_motor():
    _stop()


def shutdown(sig=None, frame=None):
    _stop()
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
                self._json(200, {"state": state_label})
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
    print(f"[motor_server] listening on port {PORT}")
    server.serve_forever()
