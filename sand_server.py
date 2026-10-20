#!/usr/bin/env python3
"""
sand_server.py — runs on the SAND Pi (Pi 3 Model B)
HTTP control for the sand-dump linear actuator. Mirrors the bench-tested wiring
in sand_dump_test.py: a 2-channel relay H-bridge driving a 12V actuator.

  Relay 2 / EXTEND  -> GPIO 22 (board pin 15)  -> rod out  (dump)
  Relay 1 / RETRACT -> GPIO 17 (board pin 11)  -> rod in   (home)
  Both relays off -> both COMs to GND -> motor braked.

Safety, carried over from the bench script:
  - DEAD_TIME between reversing (never energise both relays).
  - MAX_RUN hard cap on any single run.
  - 20% DUTY_CYCLE: the actuator must rest ~4x its last run time. A command
    that arrives too soon is REJECTED with the remaining rest (protects the
    actuator from overheating on an unattended system).

Endpoints:
  POST /sand  {"cmd":"extend|dump|retract|home|stop", "duty":<sec?>}
  GET  /sand  -> {"state","duty_default","resting","rest_left","version"}

Start: python3 sand_server.py   (default port 8082)
"""

import json, threading, time, sys, signal, logging
from gpiozero import OutputDevice
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

# ── config (matches sand_dump_test.py) ───────────────────────────────────────
EXTEND_PIN  = 22       # relay 2 / IN2 / board pin 15
RETRACT_PIN = 17       # relay 1 / IN1 / board pin 11
PORT        = 8082
VERSION     = "1.1.0"

DEFAULT_TRAVEL = 40    # run time per command if none supplied (the "duty period")
DEAD_TIME   = 0.3      # both relays off this long before energising the other
MAX_RUN     = 90       # hard cap on any single run (seconds)
DUTY_CYCLE  = 0.20     # actuator manual: 20% -> rest = run * (1-0.2)/0.2 = run*4
RUN_MIN, RUN_MAX = 1, MAX_RUN

logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("sand")

extend_dev  = OutputDevice(EXTEND_PIN,  active_high=True, initial_value=False)
retract_dev = OutputDevice(RETRACT_PIN, active_high=True, initial_value=False)

# ── state ───────────────────────────────────────────────────────────────────
stop_evt    = threading.Event()
worker      = None
state_label = "idle"            # idle | extending | retracting
state_lock  = threading.Lock()
cmd_lock    = threading.Lock()
_last_run_s = 0.0               # duration of the last run (for duty rest calc)
_last_stop  = 0.0               # monotonic time the last run ended


def _coast():
    extend_dev.off()
    retract_dev.off()


def rest_left():
    """Seconds of rest still required before the actuator may run again."""
    if _last_run_s <= 0:
        return 0
    need = _last_run_s * (1 - DUTY_CYCLE) / DUTY_CYCLE
    return max(0, int(need - (time.monotonic() - _last_stop)))


def _run(direction, seconds):
    global state_label, _last_run_s, _last_stop
    _coast()
    time.sleep(DEAD_TIME)                 # guarantee no relay overlap
    if direction == "extend":
        retract_dev.off(); extend_dev.on()
    else:
        extend_dev.off(); retract_dev.on()
    t0 = time.monotonic()
    stop_evt.wait(timeout=seconds)        # returns early if stopped
    _coast()
    _last_run_s = time.monotonic() - t0
    _last_stop  = time.monotonic()
    with state_lock:
        state_label = "idle"
    log.info(f"{direction} done after {_last_run_s:.1f}s")


def _stop_locked():
    global worker, state_label
    stop_evt.set()
    if worker and worker.is_alive():
        worker.join(timeout=MAX_RUN + 1)
    worker = None
    _coast()
    with state_lock:
        state_label = "idle"


def start(direction, seconds):
    """Returns (ok, msg). Rejects if the actuator still needs to rest."""
    global worker, state_label
    seconds = max(RUN_MIN, min(int(seconds), RUN_MAX))
    rl = rest_left()
    if rl > 0:
        log.info(f"{direction} refused — resting {rl}s more (20% duty)")
        return False, f"resting {rl}s (20% duty cycle)"
    with cmd_lock:
        _stop_locked()
        stop_evt.clear()
        with state_lock:
            state_label = "extending" if direction == "extend" else "retracting"
        worker = threading.Thread(target=_run, args=(direction, seconds), daemon=True)
        worker.start()
    log.info(f"start {direction} for {seconds}s")
    return True, state_label


def stop():
    with cmd_lock:
        _stop_locked()
    log.info("stop")


def shutdown(sig=None, frame=None):
    stop()
    extend_dev.close()
    retract_dev.close()
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
            rl = rest_left()
            self._json(200, {"state": st, "duty_default": DEFAULT_TRAVEL,
                             "resting": rl > 0, "rest_left": rl, "version": VERSION})
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
        duty = data.get("duty", DEFAULT_TRAVEL)
        log.info(f"POST cmd={cmd!r} duty={duty} from {self.client_address[0]}")
        if cmd in ("extend", "dump"):
            ok, msg = start("extend", duty)
        elif cmd in ("retract", "home"):
            ok, msg = start("retract", duty)
        elif cmd == "stop":
            stop(); ok, msg = True, "idle"
        else:
            self._json(400, {"error": f"unknown cmd: {cmd}"}); return
        with state_lock:
            self._json(200 if ok else 429,
                       {"ok": ok, "state": state_label} if ok
                       else {"ok": False, "error": msg})


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT,  shutdown)
    log.info(f"[sand_server] v{VERSION} on port {PORT} "
             f"(EXTEND=GPIO{EXTEND_PIN}, RETRACT=GPIO{RETRACT_PIN}, "
             f"dead={DEAD_TIME}s, cap={MAX_RUN}s, duty={DUTY_CYCLE*100:.0f}%)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
