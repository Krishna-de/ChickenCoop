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

import json, math, threading, time, sys, signal, logging
import RPi.GPIO as GPIO
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

# ── logging ─────────────────────────────────────────────────────────────────
# Baseline INFO logs are few (commands, limits, errors). The verbose switch-edge
# trace is behind `debug_enabled`, toggled at runtime via POST /log — so after
# diagnosing you turn it off and the motor Pi stops writing high-frequency logs
# to disk. Default off.
logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("motor")
debug_enabled = False

# ── pin config ────────────────────────────────────────────────────────────────
STEP_PIN     = 17
DIR_PIN      = 27
EN_PIN       = 22

# End-switches (limit switches) with internal pull-up.
TOP_PIN      = 5    # board pin 29 — stops UP travel
BOTTOM_PIN   = 6    # board pin 31 — stops DOWN travel
# How the switches are wired:
#   True  = NC (normally-closed): contact closed at rest -> pin LOW when NOT at
#           the limit, HIGH when the switch opens at the limit. Fail-safe: a
#           broken wire reads as "triggered". This matches the current wiring.
#   False = NO (normally-open): pin HIGH at rest, LOW at the limit.
SWITCHES_NC  = True

DELAY_START  = 0.012   # slowest step delay (start/end of a move)
DELAY_MIN    = 0.003   # fastest step delay (cruise)
RAMP_STEPS   = 30      # accel/decel ramp length
REVERSE_DWELL = 0.4    # seconds to pause after a decel before driving the other way
# The motor has no brake. When it stops off the bottom, gravity pulls the door
# down a little. If that slip is small and self-limiting (~cm) leave this False —
# coils release, no heat/power. Set True to keep the coils energized as a holding
# brake (prevents any slip, but draws motor current continuously = heat).
HOLD_OPEN    = False

PORT         = 8081
VERSION      = "1.7.0"

# ── motor state ───────────────────────────────────────────────────────────────
stop_evt     = threading.Event()   # HARD stop (Force Stop / limit) — halt now
soft_evt     = threading.Event()   # SOFT stop — decelerate smoothly, then halt
motor_thread = None
current_dir  = None                # True=up/open, False=down/close, None=stopped
state_label  = "stopped"
state_lock   = threading.Lock()    # guards state_label
cmd_lock     = threading.Lock()    # serializes start/stop so only one worker runs


def read_temp():
    """CPU temperature in °C, or None if unreadable."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        return None


def setup():
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(STEP_PIN, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(DIR_PIN,  GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(EN_PIN,   GPIO.OUT, initial=GPIO.HIGH)
    GPIO.setup(TOP_PIN,    GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(BOTTOM_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)


def _switch_active(pin):
    # NC: actuated when the contact OPENS -> pin pulled HIGH.
    # NO: actuated when the contact CLOSES to GND -> pin LOW.
    level = GPIO.input(pin)
    return level == (GPIO.HIGH if SWITCHES_NC else GPIO.LOW)


def top_hit():
    return _switch_active(TOP_PIN)


def bottom_hit():
    return _switch_active(BOTTOM_PIN)


def _watch_switches():
    """Log every switch edge with RAW level vs interpreted state — the trace that
    makes wiring problems (NC/NO, shorts, floats) obvious. Only runs while
    debug_enabled is set: when off it does no GPIO reads and no logging at all."""
    prev = None
    while True:
        if not debug_enabled:
            prev = None
            time.sleep(0.5)          # idle: no reads, no writes
            continue
        cur = (GPIO.input(TOP_PIN), GPIO.input(BOTTOM_PIN))
        if cur != prev:
            log.info("switch edge: TOP raw=%s active=%s | BOTTOM raw=%s active=%s | door=%s",
                     "HI" if cur[0] else "LO", top_hit(),
                     "HI" if cur[1] else "LO", bottom_hit(), door_position())
            prev = cur
        time.sleep(0.05)             # 20 Hz while debugging


last_limit = None   # "top" | "bottom" | None — which end-switch was last seen closed


def door_position():
    """Where the door is per the end-switches: top / bottom / partial."""
    if top_hit():
        return "top"
    if bottom_hit():
        return "bottom"
    return "partial"


def door_state():
    """Open (at/last-from top), closed (bottom), or unknown until a limit is seen.
    Top switch => door open; bottom switch => door closed."""
    global last_limit
    if top_hit():
        last_limit = "top"
        return "open"
    if bottom_hit():
        last_limit = "bottom"
        return "closed"
    if last_limit == "top":
        return "open"
    if last_limit == "bottom":
        return "closed"
    return "unknown"


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


def _ramp_delay(i):
    """Step delay during acceleration: eases from DELAY_START down to DELAY_MIN."""
    if i >= RAMP_STEPS:
        return DELAY_MIN
    t = 0.5 - 0.5 * math.cos(math.pi * (i / RAMP_STEPS))
    return DELAY_START + (DELAY_MIN - DELAY_START) * t


def _decelerate():
    """Ease from cruise speed back down to a stop — avoids a jarring hard halt
    (the mechanical shock that abrupt direction reversal was causing)."""
    for j in range(RAMP_STEPS):
        if stop_evt.is_set():          # a hard stop preempts the smooth ramp
            return
        t = 0.5 - 0.5 * math.cos(math.pi * (j / RAMP_STEPS))
        step_once(DELAY_MIN + (DELAY_START - DELAY_MIN) * t)


def run_continuous(cw):
    global state_label, last_limit
    GPIO.output(DIR_PIN, GPIO.HIGH if cw else GPIO.LOW)
    enable()
    # Only the limit in the travel direction stops us: UP -> TOP, DOWN -> BOTTOM.
    at_limit = top_hit if cw else bottom_hit
    i = 0
    while not stop_evt.is_set() and not soft_evt.is_set():
        if at_limit():
            log.info(f"{'TOP' if cw else 'BOTTOM'} limit hit — stopping")
            last_limit = "top" if cw else "bottom"
            break
        step_once(_ramp_delay(i))
        i += 1
    # Smoothly decelerate on a soft stop; hard stop and limits halt immediately.
    if soft_evt.is_set() and not stop_evt.is_set():
        _decelerate()
    # HOLD_OPEN keeps the coils energized off the bottom as a holding brake.
    # Default off: the door slips a small, self-limiting amount and the software
    # tracks it via last_limit, so no continuous holding current is needed.
    if HOLD_OPEN and not bottom_hit():
        log.info("holding position (coils energized)")
    else:
        disable()
    with state_lock:
        state_label = "stopped"
    log.info("stepping thread exited")


def _hard_stop_locked():
    """Immediate halt. Caller holds cmd_lock."""
    global motor_thread, state_label, current_dir
    stop_evt.set()
    if motor_thread and motor_thread.is_alive():
        motor_thread.join(timeout=3)
    motor_thread = None
    current_dir  = None
    with state_lock:
        state_label = "stopped"


def _soft_stop_locked():
    """Decelerate the current move to a stop. Caller holds cmd_lock."""
    global motor_thread, current_dir
    if motor_thread and motor_thread.is_alive():
        soft_evt.set()
        motor_thread.join(timeout=5)   # waits out the decel ramp
    motor_thread = None
    current_dir  = None
    soft_evt.clear()


def start_motor(cw=True):
    global motor_thread, state_label, current_dir
    # Refuse to drive INTO a pressed limit — but only when that switch alone is
    # active. If both read active it's a sensor fault, so don't lock the door up;
    # allow the move and let run_continuous stop at the real limit.
    both = top_hit() and bottom_hit()
    target_hit = top_hit() if cw else bottom_hit()
    if target_hit and not both:
        with cmd_lock:
            _hard_stop_locked()
        log.info(f"already at {'TOP' if cw else 'BOTTOM'} limit; not moving")
        return
    with cmd_lock:
        if motor_thread and motor_thread.is_alive() and current_dir == cw:
            log.info("already moving that way; ignoring")
            return
        reversing = motor_thread and motor_thread.is_alive()
        _soft_stop_locked()            # decelerate any current motion smoothly
        if reversing:
            time.sleep(REVERSE_DWELL)  # let the mechanism settle before reversing
        stop_evt.clear()
        soft_evt.clear()
        current_dir = cw
        with state_lock:
            state_label = "up" if cw else "down"
        motor_thread = threading.Thread(target=run_continuous, args=(cw,), daemon=True)
        motor_thread.start()
    log.info(f"start {'UP (CW)' if cw else 'DOWN (CCW)'}")


def stop_motor():
    """Force Stop — immediate hard halt."""
    with cmd_lock:
        _hard_stop_locked()
    log.info("force stop")


def shutdown(sig=None, frame=None):
    stop_motor()
    disable()
    GPIO.cleanup()
    log.info("shutdown")
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
        path = self.path.split("?")[0]
        if path == "/motor":
            with state_lock:
                st = state_label
            self._json(200, {"state": st, "version": VERSION, "temp": read_temp(),
                             "door": door_position(), "door_state": door_state(),
                             "top": top_hit(), "bottom": bottom_hit()})
        elif path == "/log":
            self._json(200, {"debug": debug_enabled})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/motor", "/log"):
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            self._json(400, {"error": "bad json"})
            return

        if path == "/log":
            global debug_enabled
            debug_enabled = bool(data.get("debug"))
            log.info(f"debug logging {'ON' if debug_enabled else 'OFF'}")
            self._json(200, {"ok": True, "debug": debug_enabled})
            return

        cmd = data.get("cmd", "")
        log.info(f"POST cmd={cmd!r} from {self.client_address[0]}")
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
    threading.Thread(target=_watch_switches, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info(f"v{VERSION} listening on port {PORT}  (POST /log to toggle debug)")
    server.serve_forever()
