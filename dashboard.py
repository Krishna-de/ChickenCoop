#!/usr/bin/env python3
""" 
dashboard.py — runs on the CAMERA Pi
Serves the unified coop dashboard:
  - Indoor camera (left) + Outside camera (right)
  - streams stay closed until you press Connect (keeps the Pi cool)
  - motor controls relayed to the motor Pi

Set MOTOR_PI_IP below to your motor Pi Tailscale or LAN IP.
"""

import subprocess, os, re, threading, queue, time, json, sqlite3
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.request import urlopen, Request
from urllib.error import URLError

# RealSense SDK — opens RGB+IR from ONE device handle, killing the two-ffmpeg
# USB negotiation race. If unavailable, we fall back to the staggered ffmpeg path.
try:
    import pyrealsense2 as rs
    import numpy as np
    REALSENSE_SDK = True
except Exception as _e:
    REALSENSE_SDK = False
    print(f"[realsense] SDK unavailable ({_e}); using ffmpeg fallback")

# JPEG encoder — pick the lightest backend available. Avoids the heavy
# python3-opencv apt chain; simplejpeg/Pillow both have aarch64 wheels.
_JPEG_BACKEND = None
if REALSENSE_SDK:
    try:
        import simplejpeg
        _JPEG_BACKEND = "simplejpeg"
    except Exception:
        try:
            from PIL import Image
            _JPEG_BACKEND = "pillow"
        except Exception:
            try:
                import cv2
                _JPEG_BACKEND = "cv2"
            except Exception:
                _JPEG_BACKEND = None
                REALSENSE_SDK = False
                print("[realsense] no JPEG backend (simplejpeg/Pillow/cv2); "
                      "using ffmpeg fallback")
    if _JPEG_BACKEND:
        print(f"[realsense] JPEG backend: {_JPEG_BACKEND}")


def encode_jpeg(arr, gray, quality=80):
    """Encode an RGB (HxWx3) or grayscale (HxW) numpy array to JPEG bytes."""
    if _JPEG_BACKEND == "simplejpeg":
        if gray:
            return simplejpeg.encode_jpeg(arr.reshape(arr.shape[0], arr.shape[1], 1),
                                          quality=quality, colorspace="GRAY")
        return simplejpeg.encode_jpeg(arr, quality=quality, colorspace="RGB")
    if _JPEG_BACKEND == "pillow":
        import io
        buf = io.BytesIO()
        Image.fromarray(arr, mode="L" if gray else "RGB").save(
            buf, format="JPEG", quality=quality)
        return buf.getvalue()
    if _JPEG_BACKEND == "cv2":
        # cv2 assumes BGR input; our color frames are RGB, so swap channels
        src = arr if gray else arr[:, :, ::-1]
        ok, jpg = cv2.imencode(".jpg", src, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return jpg.tobytes() if ok else None
    return None

# ── config ────────────────────────────────────────────────────────────────────
VERSION     = "1.9.0"
PORT        = 8080
FPS         = 10          # ffmpeg/UVC: lower FPS reduces USB bandwidth contention
REALSENSE_FPS = 15        # Indoor camera. 15 is verified working on this D4xx;
                          # 10 is NOT a valid rate. Every frame is JPEG-encoded
                          # in Python, so this is the main CPU/heat knob — try 6
                          # to cut load (falls back automatically if rejected).
JPEG_QUALITY  = 70        # Indoor encode quality (lower = less CPU + bandwidth)
MJPEG_QSCALE  = 7         # ffmpeg -q:v when a UVC cam can't do MJPEG passthrough
                          # (2=best/heaviest, 31=worst/lightest)
MOTOR_PI_IP = "YOUR_MOTOR_PI_IP"
MOTOR_PORT  = 8081

RES = {
    "uvc":   (1280, 720),    # Outside camera (1080p costs USB bandwidth for little gain)
    "intel": (640,  480),    # Indoor camera
}

# Indoor camera colour node (video0=Depth, video2=IR — both unused)
REALSENSE_RGB_NODE = "/dev/video4"

# Keep a UVC ffmpeg stream alive this long after the last viewer leaves, so a
# page refresh reuses it instead of racing a stop→start re-open (device busy).
IDLE_GRACE = 5.0

# CPU temperature logging for both Pis (camera + motor), graphed in the dashboard.
TEMP_SAMPLE_SEC    = 60      # how often to record a sample (RAM only, no disk)
TEMP_HISTORY_HOURS = 24      # rolling window kept in memory

ALLOWED_KEYWORDS = ["uvc", "intel", "realsense", "real sense",
                    "webcam", "usb camera", "usb2.0", "usb3", "general"]


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ── camera discovery ──────────────────────────────────────────────────────────

def is_allowed_camera(name):
    return any(kw in name.lower() for kw in ALLOWED_KEYWORDS)


def list_cameras():
    cameras = []
    try:
        result = subprocess.run(["v4l2-ctl", "--list-devices"],
                                capture_output=True, text=True, timeout=5)
        lines = result.stdout.splitlines()
        current_name, current_nodes = None, []

        def flush(name, nodes):
            if not name or not is_allowed_camera(name):
                return
            is_intel = any(k in name.lower() for k in ["intel", "realsense", "real sense"])
            if is_intel:
                for dev in nodes:
                    if dev == REALSENSE_RGB_NODE:
                        cameras.append({"index": int(re.search(r"\d+", dev).group()),
                                        "device": dev, "name": "Indoor",
                                        "kind": "intel", "auto": ""})
            else:
                for dev in nodes:
                    if re.match(r"/dev/video\d+$", dev):
                        cameras.append({"index": int(re.search(r"\d+", dev).group()),
                                        "device": dev, "name": "Outside",
                                        "kind": "uvc", "auto": ""})
                        break

        for line in lines:
            if not line.startswith("\t"):
                flush(current_name, current_nodes)
                current_name  = line.strip()
                current_nodes = []
            else:
                dev = line.strip()
                if re.match(r"/dev/video\d+$", dev):
                    current_nodes.append(dev)
        flush(current_name, current_nodes)
    except Exception:
        for i in range(8):
            dev = f"/dev/video{i}"
            if os.path.exists(dev):
                cameras.append({"index": i, "device": dev,
                                "name": f"Outside {i}", "kind": "uvc", "auto": ""})
    return cameras


# Camera hardware is static, but `v4l2-ctl --list-devices` returns nothing for a
# RealSense node once the SDK pipeline has claimed it over libusb. So a second
# browser loading the page mid-stream would see "no cameras". Cache the first
# good enumeration and reuse it for every page load.
_cameras_cache = []
_cameras_lock  = threading.Lock()


def get_cameras(force=False):
    global _cameras_cache
    with _cameras_lock:
        if _cameras_cache and not force:
            return _cameras_cache
    found = list_cameras()
    with _cameras_lock:
        # Only overwrite with a non-empty result, so a probe that races a busy
        # device (returns fewer/no cameras) can't clobber a known-good list.
        if found and (force or len(found) >= len(_cameras_cache)):
            _cameras_cache = found
        return _cameras_cache


# ── per-camera broadcaster ────────────────────────────────────────────────────

def _kill_proc(p, timeout=2):
    """Terminate an ffmpeg process and guarantee it dies, freeing the device.
    A plain terminate()+wait() can hang forever if ffmpeg is blocked on a wedged
    USB device and ignores SIGTERM — then the node stays busy and no new stream
    can open it. Escalate to SIGKILL so the device is always released."""
    if p is None:
        return
    try:
        p.terminate()
    except Exception:
        pass
    try:
        p.wait(timeout=timeout)
    except Exception:
        try:
            p.kill()
            p.wait(timeout=timeout)
        except Exception:
            pass


class CameraStream:
    def __init__(self, device, kind="uvc"):
        self.device  = device
        self.kind    = kind
        self.lock    = threading.Lock()
        self.clients = []
        self.running = False
        self.thread  = None
        self.proc    = None      # the live ffmpeg subprocess, for external stop
        # Most UVC webcams emit MJPEG natively, so we can copy frames instead of
        # decode+re-encode — that transcode is the single biggest CPU/heat cost.
        # Falls back to transcoding automatically if the camera can't do MJPEG.
        self.passthrough = (kind == "uvc")

    def stop(self):
        """Stop streaming and kill the ffmpeg child. Without this an abrupt
        process exit orphans ffmpeg, leaving /dev/videoN held."""
        with self.lock:
            self.running = False
            t = self.thread
            p = self.proc
        _kill_proc(p)              # force-free the device (SIGKILL fallback)
        if t and t.is_alive():
            t.join(timeout=4)

    def restart(self, delay=2.0):
        """Kill ffmpeg, wait `delay`s, then respawn it if viewers remain — the
        client queues are kept, so video resumes without a reconnect. Runs in a
        background thread so the HTTP request returns immediately."""
        def worker():
            self.stop()                       # keeps client queues intact
            if delay > 0:
                time.sleep(delay)
            with self.lock:
                if self.clients and not self.running:
                    self._start()
                    print(f"[cam:{self.kind}] restarted {self.device} after {delay}s")
        threading.Thread(target=worker, daemon=True).start()

    def add_client(self):
        q = queue.Queue(maxsize=5)
        with self.lock:
            self.clients.append(q)
            if not self.running:
                self._start()
        return q

    def remove_client(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def _start(self):
        self.running = True
        self.thread  = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _build_cmd(self, w, h):
        """Build the ffmpeg command list for this stream kind."""
        base = ["ffmpeg", "-loglevel", "error", "-f", "v4l2",
                "-framerate", str(FPS),
                "-video_size", f"{w}x{h}"]
        if self.passthrough:
            # Ask the camera for MJPEG and copy it straight through: no decode,
            # no encode, near-zero CPU.
            return base + ["-input_format", "mjpeg", "-i", self.device,
                           "-c:v", "copy", "-f", "mjpeg", "pipe:1"]
        # Fallback: camera can't do MJPEG, so we must transcode (expensive).
        return base + ["-i", self.device,
                       "-c:v", "mjpeg", "-q:v", str(MJPEG_QSCALE),
                       "-f", "mjpeg", "pipe:1"]

    def _run(self):
        print(f"[cam:{self.kind}] starting {self.device}")
        while self.running:
            w, h   = RES.get(self.kind, (640, 480))
            cmd    = self._build_cmd(w, h)
            ffmpeg = subprocess.Popen(cmd,
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL)
            self.proc = ffmpeg
            buf = b""
            frames_seen = 0
            idle_since = None          # when clients last dropped to zero
            try:
                while self.running:
                    with self.lock:
                        has_clients = bool(self.clients)
                    # Keep ffmpeg (and the device) open through a short idle grace
                    # so a page refresh reuses it instead of racing a stop→start
                    # re-open, which fails with "device busy" on a UVC node.
                    if has_clients:
                        idle_since = None
                    else:
                        if idle_since is None:
                            idle_since = time.time()
                        elif time.time() - idle_since > IDLE_GRACE:
                            break
                    chunk = ffmpeg.stdout.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        s = buf.find(b"\xff\xd8")
                        e = buf.find(b"\xff\xd9")
                        if s != -1 and e != -1 and e > s:
                            frame = buf[s:e+2]
                            buf   = buf[e+2:]
                            frames_seen += 1
                            with self.lock:
                                for q in list(self.clients):
                                    try:
                                        q.put_nowait(frame)
                                    except queue.Full:
                                        pass
                        else:
                            break
            finally:
                _kill_proc(ffmpeg)
                self.proc = None

            # If MJPEG passthrough yielded nothing, this camera can't do MJPEG —
            # drop to transcoding for subsequent attempts.
            if self.passthrough and frames_seen == 0:
                self.passthrough = False
                print(f"[cam:{self.kind}] {self.device}: no MJPEG from camera, "
                      "falling back to transcode (higher CPU)")

            with self.lock:
                if not self.clients:
                    self.running = False
                    return
            time.sleep(1)

        print(f"[cam:{self.kind}] stopped {self.device}")


_streams      = {}
_streams_lock = threading.Lock()


def get_stream(device, kind="uvc"):
    with _streams_lock:
        if device not in _streams:
            _streams[device] = CameraStream(device, kind)
        return _streams[device]


# ── RealSense hub — one pipeline, both streams, one device handle ───────────────

class RealSenseHub:
    """Drives the Indoor camera's colour stream from a single rs.pipeline.

    The pipeline only runs while someone is watching — frames are JPEG-encoded
    and fanned out to client queues, mirroring the CameraStream broadcaster
    contract. With no viewers the device is released and the Pi stays cool.
    """

    def __init__(self):
        self.lock     = threading.Lock()
        self.clients  = {"intel": []}   # kind -> [queue, ...]
        self.running  = False
        self.thread   = None
        self.pipeline = None

    def stop(self):
        """Release the device cleanly. Safe to call from a signal handler or
        atexit. Without this, an abrupt process exit leaks the USB handle and
        the next start fails with 'Couldn't resolve requests' / device busy."""
        with self.lock:
            self.running = False
            t = self.thread
        if t and t.is_alive():
            t.join(timeout=6)          # _run's finally stops the pipeline
        # Backstop: if the thread didn't stop it (e.g. crashed), stop directly.
        p = self.pipeline
        if p is not None:
            try:
                p.stop()
            except Exception:
                pass
            self.pipeline = None

    def is_running(self):
        with self.lock:
            return self.running

    def restart(self, delay=2.0):
        """Stop the pipeline, wait `delay`s, then restart it if viewers remain.
        Runs in a background thread so the HTTP request returns immediately."""
        def worker():
            self.stop()                       # releases the device
            if delay > 0:
                time.sleep(delay)
            with self.lock:
                if self.clients["intel"] and not self.running:
                    self._start()
                    print(f"[indoor] restarted after {delay}s")
        threading.Thread(target=worker, daemon=True).start()

    def add_client(self, kind):
        q = queue.Queue(maxsize=5)
        with self.lock:
            self.clients[kind].append(q)
            if not self.running:
                self._start()
        return q

    def remove_client(self, kind, q):
        with self.lock:
            if q in self.clients[kind]:
                self.clients[kind].remove(q)

    def _start(self):
        self.running = True
        self.thread  = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        w, h = RES["intel"]
        # Not every D4xx advertises every rate for the colour profile, so try the
        # configured FPS first and fall back to other valid rates rather than
        # failing outright with "Couldn't resolve requests".
        candidates = [REALSENSE_FPS] + [f for f in (15, 30, 6) if f != REALSENSE_FPS]
        pipeline = None
        for fps in candidates:
            p   = rs.pipeline()
            cfg = rs.config()
            cfg.enable_stream(rs.stream.color, w, h, rs.format.rgb8, fps)
            try:
                p.start(cfg)
                pipeline = p
                if fps != REALSENSE_FPS:
                    print(f"[indoor] {REALSENSE_FPS}fps unsupported, using {fps}fps")
                break
            except Exception as e:
                print(f"[indoor] {fps}fps rejected: {e}")
        if pipeline is None:
            print("[indoor] pipeline start failed at every rate")
            with self.lock:
                self.running = False
            return

        self.pipeline = pipeline
        print("[indoor] pipeline started")
        try:
            while self.running:
                with self.lock:
                    if not self.clients["intel"]:
                        break
                try:
                    frames = pipeline.wait_for_frames(5000)
                except Exception as e:
                    print(f"[indoor] wait_for_frames: {e}")
                    break
                self._fan("intel", frames.get_color_frame())
        finally:
            try:
                pipeline.stop()
            except Exception:
                pass
            self.pipeline = None
            with self.lock:
                self.running = False
            print("[indoor] pipeline stopped")

    def _fan(self, kind, frame):
        if not frame:
            return
        with self.lock:
            qs = list(self.clients[kind])
        if not qs:
            return
        img  = np.asanyarray(frame.get_data())
        data = encode_jpeg(img, gray=False, quality=JPEG_QUALITY)
        if not data:
            return
        for q in qs:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass


realsense_hub = RealSenseHub() if REALSENSE_SDK else None


def is_realsense_node(device):
    return REALSENSE_SDK and device == REALSENSE_RGB_NODE


# ── motor relay ───────────────────────────────────────────────────────────────

# Motor Pi IP is runtime-configurable from the dashboard and persisted here so
# it survives restarts. MOTOR_PI_IP above is only the first-boot default.
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "dashboard_config.json")

# Whole config lives in one dict so saving one setting can't clobber another.
_config = {
    "motor_pi_ip": MOTOR_PI_IP,
    # Remote egg store, e.g. "http://192.168.1.50:8090" (egg_collector.py).
    # Empty = keep eggs in a local file on the Pi.
    "egg_db_url":  "",
    "egg_db_token": "",
    "schedule": {
        "open_enabled":  True,
        "open_time":     "07:00",
        "close_enabled": False,
        "close_time":    "21:00",
    },
}
_config_lock = threading.Lock()

# Accept dotted IPv4 or a hostname/Tailscale name; reject anything with chars
# that could smuggle a port, path, or scheme into the URL.
_IP_RE   = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")     # 24h HH:MM


def _save_config_locked():
    """Write the whole config atomically. Caller holds _config_lock."""
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_config, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


def _load_config():
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        print(f"[config] load failed ({e}); using defaults")
        return
    with _config_lock:
        ip = data.get("motor_pi_ip")
        if ip and _IP_RE.match(ip):
            _config["motor_pi_ip"] = ip
        for k in ("egg_db_url", "egg_db_token"):
            if isinstance(data.get(k), str):
                _config[k] = data[k].strip()
        sch = data.get("schedule")
        if isinstance(sch, dict):
            for k in ("open_enabled", "close_enabled"):
                if k in sch:
                    _config["schedule"][k] = bool(sch[k])
            for k in ("open_time", "close_time"):
                if _TIME_RE.match(str(sch.get(k, ""))):
                    _config["schedule"][k] = sch[k]
    print(f"[config] motor Pi IP {_config['motor_pi_ip']}, "
          f"schedule {_config['schedule']}")


def get_motor_ip():
    with _config_lock:
        return _config["motor_pi_ip"]


def get_schedule():
    with _config_lock:
        return dict(_config["schedule"])


def get_egg_db():
    with _config_lock:
        return _config["egg_db_url"], _config["egg_db_token"]


def set_egg_db(url, token):
    """Point the egg log at a remote collector ('' = store locally)."""
    url = (url or "").strip().rstrip("/")
    if url and not re.match(r"^https?://[A-Za-z0-9.\-]+(:\d+)?$", url):
        return False, "URL must look like http://host:port"
    with _config_lock:
        _config["egg_db_url"]   = url
        _config["egg_db_token"] = (token or "").strip()
        try:
            _save_config_locked()
        except Exception as e:
            return False, f"saved in memory but write failed: {e}"
    print(f"[eggs] store = {url or 'local file'}")
    return True, url


def set_motor_ip(ip):
    """Validate, store, and persist a new motor Pi IP. Returns (ok, msg)."""
    ip = (ip or "").strip()
    if not _IP_RE.match(ip):
        return False, "invalid IP or hostname"
    with _config_lock:
        _config["motor_pi_ip"] = ip
        try:
            _save_config_locked()
        except Exception as e:
            return False, f"saved in memory but write failed: {e}"
    print(f"[config] motor Pi IP set to {ip}")
    return True, ip


def set_schedule(data):
    """Validate and persist the door schedule. Returns (ok, schedule|error)."""
    if not isinstance(data, dict):
        return False, "bad payload"
    for k in ("open_time", "close_time"):
        if k in data and not _TIME_RE.match(str(data[k])):
            return False, f"{k} must be HH:MM (24h)"
    with _config_lock:
        for k in ("open_enabled", "close_enabled"):
            if k in data:
                _config["schedule"][k] = bool(data[k])
        for k in ("open_time", "close_time"):
            if k in data:
                _config["schedule"][k] = data[k]
        try:
            _save_config_locked()
        except Exception as e:
            return False, f"saved in memory but write failed: {e}"
        sch = dict(_config["schedule"])
    print(f"[config] schedule set to {sch}")
    return True, sch


def motor_base():
    return f"http://{get_motor_ip()}:{MOTOR_PORT}/motor"


def motor_post(cmd):
    try:
        body = json.dumps({"cmd": cmd}).encode()
        req  = Request(motor_base(), data=body,
                       headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=3) as r:
            data = json.loads(r.read())
            return True, data.get("state", "unknown")
    except URLError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


def motor_get():
    """Return (ok, full status dict) — includes state, door_state, top, bottom."""
    try:
        with urlopen(motor_base(), timeout=3) as r:
            return True, json.loads(r.read())
    except Exception as e:
        # Log the real reason — a timeout (Pi busy/network blip) looks very
        # different from connection-refused (server actually down).
        print(f"[motor] poll failed: {type(e).__name__}: {e}")
        return False, str(e)


def motor_version():
    """Fetch the motor Pi's reported version. Returns the string or None."""
    try:
        with urlopen(motor_base(), timeout=3) as r:
            return json.loads(r.read()).get("version")
    except Exception:
        return None


# Recent door commands — in-memory only, deliberately never persisted.
# Cleared when the dashboard restarts.
_actions      = deque(maxlen=20)
_actions_lock = threading.Lock()


def log_action(cmd, ok, detail):
    with _actions_lock:
        _actions.appendleft({"t": int(time.time()), "cmd": cmd,
                             "ok": bool(ok), "detail": str(detail)})


# ── door schedule ─────────────────────────────────────────────────────────────
# Fires the configured open/close commands once per day. Runs on the camera Pi
# because that's where the config and UI live — note the door will NOT move on
# schedule if this Pi is down (the motor Pi has no clock-driven logic).
_sched_fired = {}      # "open"/"close" -> "YYYY-MM-DD" it last fired


def _scheduler():
    print("[sched] scheduler started")
    while True:
        try:
            sch   = get_schedule()
            now   = time.localtime()
            today = time.strftime("%Y-%m-%d", now)
            hhmm  = time.strftime("%H:%M", now)
            for action, cmd in (("open", "up"), ("close", "down")):
                if not sch.get(action + "_enabled"):
                    continue
                if sch.get(action + "_time") != hhmm:
                    continue
                if _sched_fired.get(action) == today:
                    continue                      # already ran today
                _sched_fired[action] = today
                ok, res = motor_post(cmd)
                log_action("schedule:" + action, ok, res)
                print(f"[sched] {action} fired at {hhmm} -> ok={ok} {res}")
        except Exception as e:
            print(f"[sched] error: {e}")
        time.sleep(20)          # 20s tick: never misses an HH:MM window


# ── egg log ───────────────────────────────────────────────────────────────────
# Lives in an in-memory SQLite database — nothing is ever written to the SD card.
# Pulled from the laptop's collector at startup; pushed back only when you press
# "Save to laptop". Unsaved taps are lost if the dashboard restarts, so the UI
# shows an unsaved-changes badge.

HENS = [
    {"id": "grun",   "name": "Miss Grün"},
    {"id": "koenig", "name": "Miss König"},
    {"id": "sus",    "name": "Miss Sus"},
]
_HEN_IDS   = {h["id"] for h in HENS}
_eggs_lock = threading.Lock()
_DATE_RE   = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_egg_db     = sqlite3.connect(":memory:", check_same_thread=False)
_egg_dirty  = 0                # unsaved changes since the last successful save
_egg_saved  = None             # epoch of last successful save
_egg_online = None             # last known reachability of the collector

with _eggs_lock:
    _egg_db.execute("""CREATE TABLE IF NOT EXISTS eggs (
                           date TEXT NOT NULL,
                           hen  TEXT NOT NULL,
                           PRIMARY KEY (date, hen))""")


def _egg_req(path, payload=None, timeout=8):
    """Call the remote collector. Returns parsed JSON, raises on failure."""
    url, token = get_egg_db()
    if not url:
        raise RuntimeError("no egg database configured")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Token"] = token
    body = json.dumps(payload).encode() if payload is not None else None
    req  = Request(url + path, data=body, headers=headers,
                   method="POST" if payload is not None else "GET")
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _eggs_snapshot():
    """Whole in-memory DB as {date: {hen: True}} — the payload we push."""
    out = {}
    with _eggs_lock:
        for date, hen in _egg_db.execute("SELECT date, hen FROM eggs"):
            out.setdefault(date, {})[hen] = True
    return out


def _load_eggs():
    """Seed the in-memory DB from the laptop's collector."""
    global _egg_online, _egg_dirty
    url, _ = get_egg_db()
    if not url:
        print("[eggs] no egg database configured — memory only")
        return
    try:
        data = _egg_req("/eggs").get("eggs", {})
        with _eggs_lock:
            _egg_db.execute("DELETE FROM eggs")
            for d, hens in data.items():
                if not _DATE_RE.match(d) or not isinstance(hens, dict):
                    continue
                for hen, laid in hens.items():
                    if laid and hen in _HEN_IDS:
                        _egg_db.execute("INSERT OR IGNORE INTO eggs VALUES(?,?)", (d, hen))
            n = _egg_db.execute("SELECT COUNT(DISTINCT date) FROM eggs").fetchone()[0]
        _egg_dirty  = 0
        _egg_online = True
        print(f"[eggs] loaded {n} days from {url}")
    except Exception as e:
        _egg_online = False
        print(f"[eggs] could not load from {url}: {e}")


def set_egg(date, hen, laid):
    """Record a hen laying, in memory only. Returns (ok, error)."""
    global _egg_dirty
    if not _DATE_RE.match(str(date)):
        return False, "date must be YYYY-MM-DD"
    if hen not in _HEN_IDS:
        return False, f"unknown hen: {hen}"
    with _eggs_lock:
        if laid:
            _egg_db.execute("INSERT OR IGNORE INTO eggs VALUES(?,?)", (date, hen))
        else:
            _egg_db.execute("DELETE FROM eggs WHERE date=? AND hen=?", (date, hen))
    _egg_dirty += 1
    return True, None


def save_eggs():
    """Push the whole in-memory DB to the laptop. Returns (ok, msg)."""
    global _egg_dirty, _egg_saved, _egg_online
    url, _ = get_egg_db()
    if not url:
        return False, "no egg database configured"
    snap = _eggs_snapshot()
    try:
        res = _egg_req("/eggs/bulk", {"eggs": snap}, timeout=20)
    except Exception as e:
        _egg_online = False
        return False, str(e)
    _egg_dirty  = 0
    _egg_saved  = time.time()
    _egg_online = True
    msg = f"{res.get('days', len(snap))} days, {res.get('rows', 0)} eggs"
    print(f"[eggs] saved to {url}: {msg}")
    return True, msg


def _ram_tmp(name):
    """A scratch path in RAM (/dev/shm) so nothing touches the SD card."""
    import tempfile
    d = "/dev/shm" if os.path.isdir("/dev/shm") else tempfile.gettempdir()
    return os.path.join(d, f"{name}-{os.getpid()}-{int(time.time()*1000)}.db")


def eggs_db_bytes():
    """The in-memory database as a real .db file, built entirely in RAM."""
    with _eggs_lock:
        try:
            return _egg_db.serialize()          # Python 3.11+
        except AttributeError:
            pass
        path = _ram_tmp("eggs-dl")
        dest = sqlite3.connect(path)
        try:
            _egg_db.backup(dest)
            dest.close()
            with open(path, "rb") as f:
                return f.read()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


def mark_eggs_downloaded():
    """The log has been exported off the Pi, so it's no longer 'unsaved'."""
    global _egg_dirty, _egg_saved
    _egg_dirty = 0
    _egg_saved = time.time()


def eggs_csv():
    names = {h["id"]: h["name"] for h in HENS}
    out = ["date,hen_id,hen_name"]
    with _eggs_lock:
        for date, hen in _egg_db.execute(
                "SELECT date, hen FROM eggs ORDER BY date, hen"):
            out.append(f'{date},{hen},"{names.get(hen, hen)}"')
    return "\n".join(out) + "\n"


def _rows_from_db_bytes(data):
    """Read (date, hen) rows out of a .db file's bytes."""
    try:                                    # Python 3.11+: straight from RAM
        con = sqlite3.connect(":memory:")
        con.deserialize(data)
        rows = con.execute("SELECT date, hen FROM eggs").fetchall()
        con.close()
        return rows
    except AttributeError:
        pass
    path = _ram_tmp("eggs-up")              # older Python: via a RAM-backed file
    try:
        with open(path, "wb") as f:
            f.write(data)
        src  = sqlite3.connect(path)
        rows = src.execute("SELECT date, hen FROM eggs").fetchall()
        src.close()
        return rows
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _rows_from_csv(text):
    """Read (date, hen) rows out of our CSV export."""
    rows = []
    for line in text.splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) >= 2 and _DATE_RE.match(parts[0]):
            rows.append((parts[0], parts[1]))
    return rows


def eggs_load_bytes(data):
    """Restore from an uploaded .db or .csv export. Returns (ok, msg)."""
    global _egg_dirty
    try:
        if data[:15] == b"SQLite format 3":
            rows = _rows_from_db_bytes(data)
        else:
            rows = _rows_from_csv(data.decode("utf-8", "replace"))
            if not rows:
                return False, "no egg rows found in file"
    except Exception as e:
        return False, f"could not read file: {e}"
    n = 0
    with _eggs_lock:
        _egg_db.execute("DELETE FROM eggs")
        for date, hen in rows:
            if _DATE_RE.match(str(date)) and hen in _HEN_IDS:
                _egg_db.execute("INSERT OR IGNORE INTO eggs VALUES(?,?)", (date, hen))
                n += 1
    _egg_dirty = 0
    print(f"[eggs] restored {n} eggs from upload")
    return True, f"restored {n} eggs"


def eggs_range(days=14):
    """Most recent `days` days, newest last, with per-hen booleans."""
    out   = []
    today = time.time()
    with _eggs_lock:
        for i in range(days - 1, -1, -1):
            d    = time.strftime("%Y-%m-%d", time.localtime(today - i * 86400))
            rows = {r[0] for r in
                    _egg_db.execute("SELECT hen FROM eggs WHERE date=?", (d,))}
            out.append({"date": d,
                        "hens": {h["id"]: (h["id"] in rows) for h in HENS}})
    return out


def motor_temp():
    """Fetch the motor Pi's CPU temperature (°C) or None."""
    try:
        with urlopen(motor_base(), timeout=3) as r:
            return json.loads(r.read()).get("temp")
    except Exception:
        return None


# ── temperature logging ─────────────────────────────────────────────────────────
# RAM only — nothing is written to the SD card. The graph is a rolling window of
# the last TEMP_HISTORY_HOURS and simply starts empty again after a restart.

_temps      = []                       # [[epoch:int, cam:float|None, motor:float|None], ...]
_temps_lock = threading.Lock()


def read_cpu_temp():
    """This Pi's CPU temperature in °C, or None."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        return None


def _temp_sampler():
    print(f"[temps] sampling every {TEMP_SAMPLE_SEC}s, "
          f"keeping {TEMP_HISTORY_HOURS}h in memory (no disk writes)")
    while True:
        ts  = int(time.time())
        cam = read_cpu_temp()
        mot = motor_temp()
        cutoff = ts - TEMP_HISTORY_HOURS * 3600
        with _temps_lock:
            _temps.append([ts, cam, mot])
            while _temps and _temps[0][0] < cutoff:
                _temps.pop(0)
        time.sleep(TEMP_SAMPLE_SEC)


# ── HTML ──────────────────────────────────────────────────────────────────────

def cam_json(cameras):
    items = []
    for c in cameras:
        name = c["name"].replace('"', '').replace("'", "")
        items.append('{"device":"%s","name":"%s","kind":"%s","index":%d,"auto":"%s"}'
                     % (c["device"], name, c["kind"], c["index"], c.get("auto", "")))
    return "[" + ",".join(items) + "]"


HTML_STYLE = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0c0e11;--surface:#13161b;--border:#1f2329;
  --green:#00c97d;--green2:#009960;--green-dim:rgba(0,201,125,.12);
  --blue:#3a8fff;--blue-dim:rgba(58,143,255,.12);
  --purple:#b57aff;--purple-dim:rgba(181,122,255,.12);
  --red:#ff4c4c;--red-dim:rgba(255,76,76,.12);
  --amber:#ffb340;--amber-dim:rgba(255,179,64,.12);
  --text:#dde1e7;--muted:#5a6070;--live:#ff3b3b;--r:10px;
}
body{background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  min-height:100vh;display:flex;flex-direction:column;
  align-items:center;padding:20px 16px 60px;gap:20px}
header{width:100%;max-width:1100px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.logo{font-family:monospace;font-size:1rem;font-weight:700;color:var(--green);
  letter-spacing:.07em;text-transform:uppercase}
.logo span{color:var(--muted)}
.pill{font-family:monospace;font-size:.6rem;font-weight:700;letter-spacing:.1em;
  padding:3px 8px;border-radius:20px;text-transform:uppercase}
.pill-on{background:var(--green-dim);color:var(--green);border:1px solid rgba(0,201,125,.3)}
.pill-off{background:var(--red-dim);color:var(--red);border:1px solid rgba(255,76,76,.3)}
.ver{font-family:monospace;font-size:.6rem;color:var(--muted);letter-spacing:.04em}
.section-label{width:100%;max-width:1100px;font-family:monospace;font-size:.65rem;
  font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);
  border-bottom:1px solid var(--border);padding-bottom:6px}
.tabs{width:100%;max-width:1100px;display:flex;gap:4px;border-bottom:1px solid var(--border)}
.tab{font-family:monospace;font-size:.72rem;font-weight:700;letter-spacing:.06em;
  text-transform:uppercase;padding:11px 20px;cursor:pointer;color:var(--muted);
  background:transparent;border:none;border-bottom:2px solid transparent;margin-bottom:-1px}
.tab:hover{color:var(--text)}
.tab.active{color:var(--green);border-bottom-color:var(--green)}
.tab-panel{display:none;width:100%;max-width:1100px;flex-direction:column;
  gap:20px;align-items:center}
.tab-panel.active{display:flex}
.temp-panel{width:100%;background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r);padding:18px 20px;display:flex;flex-direction:column;gap:14px}
.temp-head{display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.temp-now{font-family:monospace;font-size:.8rem;color:var(--text);display:flex;
  align-items:center;gap:7px}
.temp-now b{font-weight:700}
.temp-now .dot{width:10px;height:10px;border-radius:50%;display:inline-block}
.dot.cam{background:var(--green)}.dot.mot{background:var(--amber)}
.temp-range{font-family:monospace;font-size:.66rem;color:var(--muted);margin-left:auto}
.temp-chart{width:100%;height:320px;background:var(--bg);border:1px solid var(--border);
  border-radius:8px;overflow:hidden;display:flex;align-items:center;justify-content:center}
.temp-empty{font-family:monospace;font-size:.78rem;color:var(--muted)}
.tchart{width:100%;height:100%}
.tchart .grid{stroke:var(--border);stroke-width:1}
.tchart .ylab,.tchart .xlab{fill:var(--muted);font-family:monospace;font-size:11px}
.tchart .xlab{text-anchor:middle}
.tchart .ylab{text-anchor:end}
.tchart .lcam{fill:none;stroke:var(--green);stroke-width:2;
  vector-effect:non-scaling-stroke;stroke-linejoin:round}
.tchart .lmot{fill:none;stroke:var(--amber);stroke-width:2;
  vector-effect:non-scaling-stroke;stroke-linejoin:round}
.cam-grid{display:grid;gap:12px;width:100%;max-width:1100px;
  grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.panel{position:relative;background:#000;border:1px solid var(--border);
  border-radius:var(--r);overflow:hidden;aspect-ratio:4/3;
  display:flex;align-items:center;justify-content:center}
.panel.left{border-color:var(--green)}
.panel.right{border-color:var(--purple)}
.panel.third{border-color:var(--blue)}
.panel.right::after{content:'';position:absolute;inset:0;
  background:linear-gradient(135deg,rgba(181,122,255,.04) 0%,transparent 60%);
  pointer-events:none}
.panel.third::after{content:'';position:absolute;inset:0;
  background:linear-gradient(135deg,rgba(58,143,255,.04) 0%,transparent 60%);
  pointer-events:none}
.panel img{width:100%;height:100%;object-fit:contain;display:block}
.ph{color:var(--muted);font-family:monospace;font-size:.75rem;text-align:center;line-height:2}
.ph span{display:block;font-size:.58rem;opacity:.4;margin-bottom:4px;
  letter-spacing:.1em;text-transform:uppercase}
.badge{position:absolute;top:9px;right:9px;color:#fff;font-family:monospace;
  font-size:.58rem;font-weight:700;letter-spacing:.12em;padding:2px 7px;
  border-radius:4px;animation:pulse 2s infinite}
.badge.L{background:var(--live)}.badge.R{background:#7c3aed}.badge.C{background:var(--blue)}
.plbl{position:absolute;top:9px;left:9px;font-family:monospace;font-size:.58rem;
  font-weight:700;padding:2px 7px;border-radius:4px}
.plbl.L{background:rgba(0,201,125,.18);color:var(--green)}
.plbl.R{background:rgba(181,122,255,.18);color:var(--purple)}
.plbl.C{background:rgba(58,143,255,.18);color:var(--blue)}
.slbl{position:absolute;bottom:9px;left:9px;font-family:monospace;font-size:.6rem;
  padding:2px 8px;border-radius:4px;background:rgba(0,0,0,.55);color:var(--text);
  max-width:55%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.panel-ctrls{position:absolute;bottom:9px;right:9px;display:flex;gap:6px;align-items:center}
.disc{font-family:monospace;font-size:.62rem;
  font-weight:700;padding:4px 10px;border-radius:6px;cursor:pointer;
  border:1px solid rgba(255,76,76,.35);background:rgba(255,76,76,.1);color:var(--red)}
.disc:hover{background:rgba(255,76,76,.25)}
.rst{font-family:monospace;font-size:.62rem;font-weight:700;padding:4px 10px;
  border-radius:6px;cursor:pointer;border:1px solid rgba(58,143,255,.35);
  background:rgba(58,143,255,.1);color:var(--blue)}
.rst:hover{background:rgba(58,143,255,.25)}
.rst:disabled{opacity:.5;cursor:default}
.re-msg{display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:10px;width:100%;height:100%;color:var(--muted);font-family:monospace;font-size:.75rem}
.re-btn{font-family:monospace;font-size:.66rem;font-weight:700;padding:5px 14px;
  border-radius:6px;cursor:pointer}
.re-btn.L{background:var(--green-dim);color:var(--green);border:1px solid rgba(0,201,125,.35)}
.re-btn.R{background:var(--purple-dim);color:var(--purple);border:1px solid rgba(181,122,255,.4)}
.re-btn.C{background:var(--blue-dim);color:var(--blue);border:1px solid rgba(58,143,255,.4)}
.pipe-ctrl{display:flex;align-items:center;gap:12px;flex-wrap:wrap;width:100%;
  max-width:1100px;background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r);padding:10px 16px}
.pipe-title{font-family:monospace;font-size:.65rem;font-weight:700;letter-spacing:.1em;
  text-transform:uppercase;color:var(--muted)}
.pipe-badge{font-family:monospace;font-size:.6rem;font-weight:700;letter-spacing:.1em;
  padding:2px 8px;border-radius:20px;text-transform:uppercase}
.pipe-badge.on {background:var(--green-dim);color:var(--green);border:1px solid rgba(0,201,125,.3)}
.pipe-badge.off{background:var(--red-dim);color:var(--red);border:1px solid rgba(255,76,76,.3)}
.pc-btn{font-family:monospace;font-size:.68rem;font-weight:700;padding:6px 14px;
  border-radius:6px;cursor:pointer;border:1px solid}
.pc-stop{background:var(--red-dim);color:var(--red);border-color:rgba(255,76,76,.35)}
.pc-stop:hover{background:rgba(255,76,76,.25)}
.pc-restart{background:var(--blue-dim);color:var(--blue);border-color:rgba(58,143,255,.35)}
.pc-restart:hover{background:rgba(58,143,255,.25)}
.pc-delay{font-family:monospace;font-size:.66rem;color:var(--muted);display:flex;
  align-items:center;gap:5px}
.pc-delay input{width:56px;font-family:monospace;font-size:.72rem;color:var(--text);
  background:var(--bg);border:1px solid var(--border);border-radius:5px;padding:4px 6px}
.pipe-status{font-family:monospace;font-size:.7rem;margin-left:auto}
.motor-panel{width:100%;max-width:1100px;background:var(--surface);
  border:1px solid var(--border);border-radius:var(--r);padding:20px 24px;
  display:flex;flex-direction:column;gap:16px}
.motor-status-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.motor-title{font-family:monospace;font-size:.72rem;font-weight:700;
  letter-spacing:.1em;text-transform:uppercase;color:var(--muted);flex:1}
/* door control component */
.door-panel{width:100%;max-width:1100px;background:var(--surface);
  border:1px solid var(--border);border-radius:var(--r);padding:18px 22px;
  display:flex;flex-direction:column;gap:16px}
.door-head{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.door-title{font-family:monospace;font-size:.72rem;font-weight:700;
  letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.door-badge{font-family:monospace;font-size:.82rem;font-weight:700;letter-spacing:.08em;
  padding:5px 16px;border-radius:20px;text-transform:uppercase;transition:.2s}
.db-open   {background:var(--green-dim);color:var(--green);border:1px solid rgba(0,201,125,.4)}
.db-closed {background:var(--blue-dim);color:var(--blue);border:1px solid rgba(58,143,255,.4)}
.db-moving {background:var(--amber-dim);color:var(--amber);border:1px solid rgba(255,179,64,.4);
  animation:pulse 1.4s infinite}
.db-partial{background:rgba(90,96,112,.15);color:var(--text);border:1px solid var(--border)}
.db-unknown{background:rgba(90,96,112,.15);color:var(--muted);border:1px solid var(--border)}
.limits{display:flex;gap:12px;align-items:center}
.lim{display:flex;align-items:center;gap:6px;font-family:monospace;font-size:.6rem;
  font-weight:700;letter-spacing:.08em;color:var(--muted)}
.lim .ldot{width:9px;height:9px;border-radius:50%;background:var(--border);
  border:1px solid #2e3440;transition:.15s}
.lim.on{color:var(--green)}
.lim.on .ldot{background:var(--green);border-color:var(--green);box-shadow:0 0 6px var(--green)}
.mbtn:disabled{opacity:.35;cursor:not-allowed;filter:grayscale(.5)}
/* door schedule */
.sched-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.sw{display:flex;align-items:center;gap:6px;font-family:monospace;font-size:.68rem;
  color:var(--text);cursor:pointer}
.sw input{accent-color:var(--green);width:15px;height:15px;cursor:pointer}
.tm{font-family:monospace;font-size:.78rem;color:var(--text);background:var(--bg);
  border:1px solid var(--border);border-radius:6px;padding:6px 8px}
.tm:focus{outline:none;border-color:var(--green2)}
/* egg log */
.egg-panel{width:100%;background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r);padding:18px 20px;display:flex;flex-direction:column;gap:16px}
.egg-head{display:flex;align-items:center;gap:12px}
.egg-date{font-family:monospace;font-size:.68rem;color:var(--muted);margin-left:auto}
.egg-store{margin-left:8px;padding:2px 7px;border-radius:20px;font-size:.6rem;
  background:var(--green-dim);color:var(--green);border:1px solid rgba(0,201,125,.3)}
.egg-store.bad{background:var(--amber-dim);color:var(--amber);
  border-color:rgba(255,179,64,.35)}
.egg-save{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.egg-unsaved{font-family:monospace;font-size:.66rem;color:var(--muted)}
.egg-unsaved.on{color:var(--amber);font-weight:700}
.egg-today{display:flex;gap:10px;flex-wrap:wrap}
.egg-btn{flex:1 1 150px;display:flex;flex-direction:column;align-items:center;gap:6px;
  padding:14px 10px;border-radius:var(--r);cursor:pointer;font-family:monospace;
  background:rgba(90,96,112,.12);border:1px solid var(--border);color:var(--muted)}
.egg-btn .ei{font-size:1.7rem;line-height:1}
.egg-btn .en{font-size:.74rem;font-weight:700}
.egg-btn.on{background:var(--amber-dim);border-color:rgba(255,179,64,.5);color:var(--amber)}
.egg-btn:hover{border-color:var(--green2)}
.egg-grid-wrap{width:100%;overflow-x:auto}
.egg-grid{display:grid;gap:2px;min-width:520px}
.gh{font-family:monospace;font-size:.55rem;color:var(--muted);text-align:center;
  padding:3px 0}
.gn{font-family:monospace;font-size:.66rem;color:var(--text);padding:4px 8px 4px 0;
  white-space:nowrap}
.gc{display:flex;align-items:center;justify-content:center;font-size:.8rem;
  color:var(--border);background:var(--bg);border-radius:3px;padding:5px 0;cursor:pointer}
.gc:hover{outline:1px solid var(--green2)}
.gc.on{color:var(--amber);background:var(--amber-dim)}
.egg-totals{display:flex;gap:14px;flex-wrap:wrap;font-family:monospace;font-size:.68rem;
  color:var(--muted);border-top:1px solid var(--border);padding-top:12px}
.egg-tot b{color:var(--text);font-weight:700}
.egg-tot.all{margin-left:auto}
.egg-tot.all b{color:var(--amber)}
.acts{display:flex;flex-direction:column;gap:6px}
.acts-label{font-family:monospace;font-size:.6rem;font-weight:700;letter-spacing:.1em;
  text-transform:uppercase;color:var(--muted)}
.acts-label i{font-style:normal;opacity:.6;text-transform:none;letter-spacing:0}
.acts-list{list-style:none;display:flex;flex-direction:column;gap:2px;
  max-height:132px;overflow-y:auto}
.acts-none{font-family:monospace;font-size:.68rem;color:var(--muted);padding:3px 0}
.act{display:flex;align-items:center;gap:10px;font-family:monospace;font-size:.68rem;
  padding:3px 8px;border-radius:4px;background:var(--bg)}
.act .at{color:var(--muted)}
.act .an{font-weight:700;min-width:74px}
.act .ad{color:var(--muted);margin-left:auto}
.act.ok .an{color:var(--text)}
.act.bad .an,.act.bad .ad{color:var(--red)}
.state-badge{font-family:monospace;font-size:.72rem;font-weight:700;
  letter-spacing:.1em;padding:4px 12px;border-radius:6px;text-transform:uppercase;transition:.2s}
.state-stopped{background:rgba(90,96,112,.15);color:var(--muted);border:1px solid var(--border)}
.state-up  {background:var(--green-dim);color:var(--green);border:1px solid rgba(0,201,125,.35);animation:pulse 2s infinite}
.state-down{background:var(--amber-dim);color:var(--amber);border:1px solid rgba(255,179,64,.35);animation:pulse 2s infinite}
.motor-err{font-family:monospace;font-size:.68rem;color:var(--red);margin-left:auto}
.motor-btns{display:flex;gap:10px;flex-wrap:wrap}
.mbtn{display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:4px;padding:16px 28px;border-radius:var(--r);cursor:pointer;font-family:monospace;
  font-weight:700;font-size:.72rem;letter-spacing:.1em;text-transform:uppercase;
  transition:opacity .12s,transform .1s;border:1px solid;flex:1;min-width:100px}
.mbtn:hover{opacity:.85}.mbtn:active{transform:scale(.97)}
.mbtn-up  {background:var(--green-dim);color:var(--green);border-color:rgba(0,201,125,.35)}
.mbtn-up.active{background:rgba(0,201,125,.25);border-color:var(--green)}
.mbtn-down{background:var(--amber-dim);color:var(--amber);border-color:rgba(255,179,64,.35)}
.mbtn-down.active{background:rgba(255,179,64,.25);border-color:var(--amber)}
.mbtn-stop{background:var(--red-dim);color:var(--red);border-color:rgba(255,76,76,.35);
  flex:0 0 auto;min-width:80px}
.mbtn-stop:hover{background:rgba(255,76,76,.25)}
.mbtn-icon{font-size:1.5rem;line-height:1}
.motor-conn-warn{font-family:monospace;font-size:.7rem;color:var(--amber);
  background:var(--amber-dim);border:1px solid rgba(255,179,64,.3);
  border-radius:6px;padding:8px 12px;display:none}
.ip-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.ip-label{font-family:monospace;font-size:.65rem;font-weight:700;letter-spacing:.1em;
  text-transform:uppercase;color:var(--muted)}
.ip-input{font-family:monospace;font-size:.78rem;color:var(--text);
  background:var(--bg);border:1px solid var(--border);border-radius:6px;
  padding:7px 10px;min-width:180px;flex:0 1 220px}
.ip-input:focus{outline:none;border-color:var(--green2)}
.ip-save{font-family:monospace;font-size:.68rem;font-weight:700;padding:7px 16px;
  border-radius:6px;cursor:pointer;background:var(--green-dim);color:var(--green);
  border:1px solid rgba(0,201,125,.35)}
.ip-save:hover{background:rgba(0,201,125,.25)}
.ip-status{font-family:monospace;font-size:.7rem}
.kbd-hint{display:flex;gap:14px;flex-wrap:wrap}
.kh{display:flex;align-items:center;gap:6px;font-family:monospace;font-size:.65rem;color:var(--muted)}
kbd{background:var(--border);border:1px solid #2e3440;border-radius:4px;
  padding:2px 7px;font-size:.65rem;color:var(--text)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}

/* ── Mobile (phones / portrait) ─────────────────────────────────── */
@media(max-width:640px){
  body{padding:12px 10px 40px;gap:14px}
  .logo{font-size:.85rem}
  header{gap:8px}
  /* keyboard shortcut hints are desktop-only */
  .kbd-hint{display:none}
  /* full-width, bigger touch targets */
  .motor-panel{padding:16px}
  .motor-btns{gap:8px}
  .mbtn{padding:18px 14px;font-size:.8rem;min-width:0;flex:1 1 28%}
  .mbtn-stop{flex:1 1 28%}
  .mbtn-icon{font-size:1.6rem}
  .disc,.rst{font-size:.66rem;padding:7px 12px}
  .ip-row{gap:8px}
  .ip-input{flex:1 1 100%;min-width:0;padding:10px 12px;font-size:.85rem}
  .ip-save{flex:1 1 100%;padding:10px}
  .pipe-ctrl{gap:8px;padding:10px 12px}
  .pc-btn{padding:9px 16px;font-size:.72rem}
  .pipe-status{margin-left:0;flex:1 1 100%}
}
/* coarse pointers (touch): never rely on hover-only affordances */
@media(hover:none){
  .mbtn:hover,.disc:hover,.rst:hover,.ip-save:hover,
  .pc-stop:hover,.pc-restart:hover{opacity:1}
}
"""

HTML_JS = """
var CAMS  = CAMS_JSON;
var slots = {L: null, R: null};
var PANEL = {L: 'left', R: 'right'};
function kindLabel(kind){ return kind === 'uvc' ? 'Outside' : 'Indoor'; }

function showTab(name){
  ['live', 'eggs', 'temps'].forEach(function(t){
    document.getElementById('tab-' + t).classList.toggle('active', t === name);
    document.getElementById('tab-btn-' + t).classList.toggle('active', t === name);
  });
  if (name === 'temps') loadTemps();
  if (name === 'eggs')  loadEggs();
}

function assign(slot, cam) {
  slots[slot] = cam.device;
  startStream(slot, cam.device, cam.name, cam.kind);
}

function startStream(slot, device, name, kind) {
  var panel = document.getElementById('p' + slot);
  panel.className = 'panel ' + PANEL[slot];

  var img = document.createElement('img');
  img.id  = 'img' + slot;
  img.src = '/feed?device=' + device + '&t=' + Date.now();

  var lbl = document.createElement('div');
  lbl.className = 'plbl ' + slot;
  lbl.textContent = kindLabel(kind);

  var badge = document.createElement('div');
  badge.className = 'badge ' + slot;
  badge.textContent = 'LIVE';

  var slbl = document.createElement('div');
  slbl.className = 'slbl';
  slbl.textContent = name;

  var ctrls = document.createElement('div');
  ctrls.className = 'panel-ctrls';

  // Forced restart-with-delay for UVC (RealSense uses the pipeline bar instead).
  if (kind === 'uvc') {
    var rst = document.createElement('button');
    rst.className = 'rst';
    rst.textContent = '\\u21BB Restart';
    rst.onclick = function(){ restartFeed(device, rst); };
    ctrls.appendChild(rst);
  }

  var disc = document.createElement('button');
  disc.className = 'disc';
  disc.textContent = '\\u25A0 Disconnect';
  disc.onclick = function(){ disconnect(slot, device); };
  ctrls.appendChild(disc);

  panel.innerHTML = '';
  panel.appendChild(img); panel.appendChild(lbl);
  panel.appendChild(badge); panel.appendChild(slbl); panel.appendChild(ctrls);
}

function restartFeed(device, btn) {
  var old = btn ? btn.textContent : '';
  if (btn) { btn.textContent = '\\u21BB\\u2026'; btn.disabled = true; }
  fetch('/pipeline', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action:'restart', delay: 2, device: device})})
  .then(function(r){ return r.json(); })
  .catch(function(){})
  .then(function(){
    setTimeout(function(){ if (btn) { btn.textContent = old; btn.disabled = false; } }, 3000);
  });
}

function disconnect(slot, device) {
  var img = document.getElementById('img' + slot);
  if (img) img.src = '';          // drops the MJPEG connection -> stream stops
  showIdle(slot, CAMS.find(function(c){ return c.device === device; }));
}

// Fixed layout: Indoor -> left, Outside -> right. Nothing streams until the
// user presses Connect — an idle camera costs no USB bandwidth, CPU or heat.
function showIdle(slot, cam){
  var panel = document.getElementById('p' + slot);
  panel.className = 'panel';
  var msg = document.createElement('div'); msg.className = 're-msg';
  var txt = document.createElement('span');
  if (!cam) {
    txt.textContent = 'Not detected';
    msg.appendChild(txt);
  } else {
    txt.textContent = cam.name;
    var btn = document.createElement('button');
    btn.className = 're-btn ' + slot;
    btn.textContent = '\\u25B6 Connect';
    btn.onclick = function(){ assign(slot, cam); };
    msg.appendChild(txt); msg.appendChild(btn);
  }
  panel.innerHTML = ''; panel.appendChild(msg);
  slots[slot] = null;
}
var indoorCam  = CAMS.find(function(c){ return c.kind === 'intel'; });
var outsideCam = CAMS.find(function(c){ return c.kind === 'uvc'; });
showIdle('L', indoorCam);
showIdle('R', outsideCam);

// door / motor
// Combine motion (state) with door position (door_state + limit switches) into
// one label: Opening/Closing while moving, else Open/Closed/Partial.
var lastStatus  = null;
var cmdCooldown = false;
var CMD_COOLDOWN_MS = 1500;   // covers the motor's decel + reverse dwell

// Buttons are disabled by EITHER a limit switch or the anti-spam cooldown,
// so both inputs must be re-applied together whenever either changes.
function updateButtons() {
  var d = lastStatus || {};
  document.getElementById('btn-up').disabled   = !!d.top || cmdCooldown;
  document.getElementById('btn-down').disabled = !!d.bottom || cmdCooldown;
  // Force Stop is never disabled — it's the emergency path.
}
function renderDoor(d) {
  lastStatus = d;
  var badge = document.getElementById('door-badge');
  var state = d.state, door = d.door_state;
  var cls = 'unknown', txt = 'Unknown';
  if (state === 'up')        { cls = 'moving'; txt = 'Opening'; }
  else if (state === 'down') { cls = 'moving'; txt = 'Closing'; }
  else if (door === 'open')  { cls = 'open';   txt = 'Open'; }
  else if (door === 'closed'){ cls = 'closed'; txt = 'Closed'; }
  else                       { cls = 'partial';txt = 'Partial'; }
  badge.className = 'door-badge db-' + cls;
  badge.textContent = txt;
  document.getElementById('lim-top').classList.toggle('on', !!d.top);
  document.getElementById('lim-bot').classList.toggle('on', !!d.bottom);
  document.getElementById('btn-up').classList.toggle('active',   state === 'up');
  document.getElementById('btn-down').classList.toggle('active', state === 'down');
  updateButtons();
}
function fmtActTime(sec){
  var d = new Date(sec * 1000);
  return ('0'+d.getHours()).slice(-2) + ':' + ('0'+d.getMinutes()).slice(-2)
       + ':' + ('0'+d.getSeconds()).slice(-2);
}
var ACT_NAME = {up: 'Open', down: 'Close', stop: 'Force stop'};
function loadActions() {
  fetch('/actions').then(function(r){ return r.json(); }).then(function(d){
    var ul = document.getElementById('acts-list');
    var a  = d.actions || [];
    if (!a.length) { ul.innerHTML = '<li class="acts-none">None yet</li>'; return; }
    ul.innerHTML = '';
    a.forEach(function(x){
      var li = document.createElement('li');
      li.className = 'act ' + (x.ok ? 'ok' : 'bad');
      li.innerHTML = '<span class="at">' + fmtActTime(x.t) + '</span>'
                   + '<span class="an">' + (ACT_NAME[x.cmd] || x.cmd) + '</span>'
                   + '<span class="ad">' + (x.ok ? x.detail : 'failed') + '</span>';
      ul.appendChild(li);
    });
  }).catch(function(){});
}
function setErr(msg) {
  var el  = document.getElementById('motor-err');
  var cw  = document.getElementById('conn-warn');
  var pil = document.getElementById('conn-pill');
  if (msg) {
    el.textContent = msg; cw.style.display = 'block';
    pil.className = 'pill pill-off'; pil.textContent = 'Motor Pi offline';
  } else {
    el.textContent = ''; cw.style.display = 'none';
    pil.className = 'pill pill-on'; pil.textContent = 'Motor Pi';
  }
}
function motorCmd(cmd) {
  // Anti-spam: Open/Close are rate-limited so rapid clicks can't queue up
  // reversals. Force Stop always goes through.
  if (cmd !== 'stop') {
    if (cmdCooldown) return;
    cmdCooldown = true; updateButtons();
    setTimeout(function(){ cmdCooldown = false; updateButtons(); }, CMD_COOLDOWN_MS);
  }
  fetch('/motor', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({cmd: cmd})})
  .then(function(r){ return r.json(); })
  .then(function(d){ if (d.ok) { setErr(null); pollMotor(); } else { setErr(d.error||'error'); } })
  .catch(function(){ setErr('Network error'); })
  .then(function(){ loadActions(); });
}
// A single dropped/slow poll (network blip, Pi busy stepping) must not flap the
// UI to "offline" — only give up after several consecutive misses, and keep
// showing the last known door state until then.
var motorFails = 0;
var MOTOR_FAIL_LIMIT = 3;          // 3 x 4s poll => ~12s before declaring offline
function motorMissed(msg) {
  motorFails++;
  if (motorFails >= MOTOR_FAIL_LIMIT) setErr(msg);
}
function pollMotor() {
  fetch('/motor')
  .then(function(r){ return r.json(); })
  .then(function(d){
    if (d.state !== undefined){ motorFails = 0; renderDoor(d); setErr(null); }
    else { motorMissed(d.error || 'Motor Pi error'); }
  })
  .catch(function(){ motorMissed('Motor Pi unreachable'); });
}
setInterval(pollMotor, 4000);
pollMotor();
loadActions();

// motor Pi IP config
function loadConfig() {
  fetch('/config')
  .then(function(r){ return r.json(); })
  .then(function(d){
    document.getElementById('ip-input').value = d.motor_pi_ip || '';
    var u = document.getElementById('egg-url');   var t = document.getElementById('egg-token');
    if (u) u.value = d.egg_db_url || '';
    if (t) t.value = d.egg_db_token || '';
  })
  .catch(function(){});
}
function saveMotorIp() {
  var ip  = document.getElementById('ip-input').value.trim();
  var st  = document.getElementById('ip-status');
  fetch('/config', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({motor_pi_ip: ip})})
  .then(function(r){ return r.json(); })
  .then(function(d){
    if (d.ok) { st.textContent = 'Saved \\u2713'; st.style.color = 'var(--green)'; pollMotor(); loadVersions(); }
    else      { st.textContent = d.error || 'error'; st.style.color = 'var(--red)'; }
    setTimeout(function(){ st.textContent = ''; }, 3000);
  })
  .catch(function(){ st.textContent = 'Network error'; st.style.color = 'var(--red)'; });
}
loadConfig();

// ── door schedule ───────────────────────────────────────────────────────────
function loadSchedule() {
  fetch('/schedule').then(function(r){ return r.json(); }).then(function(s){
    document.getElementById('sch-open-en').checked  = !!s.open_enabled;
    document.getElementById('sch-open-tm').value    = s.open_time  || '07:00';
    document.getElementById('sch-close-en').checked = !!s.close_enabled;
    document.getElementById('sch-close-tm').value   = s.close_time || '21:00';
  }).catch(function(){});
}
function saveSchedule() {
  var st = document.getElementById('sch-status');
  fetch('/schedule', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      open_enabled:  document.getElementById('sch-open-en').checked,
      open_time:     document.getElementById('sch-open-tm').value,
      close_enabled: document.getElementById('sch-close-en').checked,
      close_time:    document.getElementById('sch-close-tm').value
    })})
  .then(function(r){ return r.json(); })
  .then(function(d){
    st.textContent = d.ok ? 'Saved \\u2713' : (d.error || 'error');
    st.style.color = d.ok ? 'var(--green)' : 'var(--red)';
    setTimeout(function(){ st.textContent = ''; }, 3000);
  })
  .catch(function(){ st.textContent = 'Network error'; st.style.color = 'var(--red)'; });
}
loadSchedule();

// ── egg log ─────────────────────────────────────────────────────────────────
var EGG_DAYS = 14;
function toggleEgg(date, hen, laid) {
  fetch('/eggs', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({date: date, hen: hen, laid: laid})})
  .then(function(){ loadEggs(); }).catch(function(){});
}
function loadEggs() {
  fetch('/eggs?days=' + EGG_DAYS).then(function(r){ return r.json(); })
  .then(function(d){
    var hens = d.hens || [], days = d.days || [], today = d.today;
    var store = !d.store ? 'in memory \\u2014 download to keep'
              : (d.online === false ? '\\u26A0 ' + d.store + ' unreachable'
                                    : '\\u2713 ' + d.store);
    document.getElementById('egg-today').innerHTML =
      today + ' <span class="egg-store' + (d.online === false ? ' bad' : '')
            + '">' + store + '</span>';
    var u = document.getElementById('egg-unsaved');
    if (d.unsaved) {
      u.textContent = d.unsaved + ' change' + (d.unsaved > 1 ? 's' : '')
                    + ' not downloaded';
      u.className = 'egg-unsaved on';
    } else {
      u.textContent = d.saved_at ? 'Downloaded \\u2713' : 'No changes';
      u.className = 'egg-unsaved';
    }
    var last = days.length ? days[days.length-1] : {hens:{}};

    // today's toggles
    var tg = document.getElementById('egg-toggles');
    tg.innerHTML = '';
    hens.forEach(function(h){
      var on = !!(last.hens && last.hens[h.id]);
      var b  = document.createElement('button');
      b.className = 'egg-btn' + (on ? ' on' : '');
      b.innerHTML = '<span class="ei">' + (on ? '\\uD83E\\uDD5A' : '\\u2014')
                  + '</span><span class="en">' + h.name + '</span>';
      b.onclick = function(){ toggleEgg(today, h.id, !on); };
      tg.appendChild(b);
    });

    // history grid: one row per hen, one cell per day
    var g = document.getElementById('egg-grid');
    g.innerHTML = '';
    g.style.gridTemplateColumns = 'minmax(90px,auto) repeat(' + days.length + ',1fr)';
    g.appendChild(cell('', 'gh'));
    days.forEach(function(x){
      g.appendChild(cell(x.date.slice(8) + '/' + x.date.slice(5,7), 'gh'));
    });
    hens.forEach(function(h){
      g.appendChild(cell(h.name, 'gn'));
      days.forEach(function(x){
        var on = !!x.hens[h.id];
        var c  = cell(on ? '\\u25CF' : '\\u00B7', 'gc' + (on ? ' on' : ''));
        c.title = h.name + ' \\u2014 ' + x.date + (on ? ': egg' : ': none');
        c.onclick = function(){ toggleEgg(x.date, h.id, !on); };
        g.appendChild(c);
      });
    });

    // totals
    var tot = document.getElementById('egg-totals');
    tot.innerHTML = '';
    hens.forEach(function(h){
      var n = days.filter(function(x){ return x.hens[h.id]; }).length;
      var s = document.createElement('span');
      s.className = 'egg-tot';
      s.innerHTML = '<b>' + h.name + '</b> ' + n + '/' + days.length + ' days';
      tot.appendChild(s);
    });
    var all = days.reduce(function(a,x){
      return a + hens.filter(function(h){ return x.hens[h.id]; }).length; }, 0);
    var s2 = document.createElement('span');
    s2.className = 'egg-tot all';
    s2.innerHTML = '<b>Total</b> ' + all + ' eggs / ' + days.length + ' days';
    tot.appendChild(s2);
  }).catch(function(){});
}
function cell(txt, cls) {
  var d = document.createElement('div');
  d.className = cls; d.textContent = txt;
  return d;
}
function uploadEggs(input) {
  var f = input.files && input.files[0];
  if (!f) return;
  if (!confirm('Replace the current egg log with "' + f.name + '"?')) {
    input.value = ''; return;
  }
  var st = document.getElementById('egg-save-status');
  st.textContent = 'Restoring\\u2026'; st.style.color = 'var(--muted)';
  f.arrayBuffer().then(function(buf){
    return fetch('/eggs/upload', {method:'POST',
      headers:{'Content-Type':'application/octet-stream'}, body: buf});
  })
  .then(function(r){ return r.json(); })
  .then(function(d){
    st.textContent = d.ok ? (d.msg || 'Restored \\u2713') : (d.error || 'failed');
    st.style.color = d.ok ? 'var(--green)' : 'var(--red)';
    setTimeout(function(){ st.textContent = ''; }, 5000);
    loadEggs();
  })
  .catch(function(){ st.textContent = 'Upload failed'; st.style.color = 'var(--red)'; })
  .then(function(){ input.value = ''; });
}
function saveEggDb() {
  var st = document.getElementById('egg-db-status');
  fetch('/config', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({egg_db_url: document.getElementById('egg-url').value,
                          egg_db_token: document.getElementById('egg-token').value})})
  .then(function(r){ return r.json(); })
  .then(function(d){
    st.textContent = d.ok ? 'Saved \\u2713' : (d.error || 'error');
    st.style.color = d.ok ? 'var(--green)' : 'var(--red)';
    setTimeout(function(){ st.textContent = ''; }, 3000);
    if (d.ok) loadEggs();
  })
  .catch(function(){ st.textContent = 'Network error'; st.style.color = 'var(--red)'; });
}

// dashboard + motor server versions
function loadVersions() {
  fetch('/version').then(function(r){ return r.json(); })
  .then(function(d){
    var el = document.getElementById('ver');
    if (el) el.innerHTML = 'dash v' + (d.dashboard || '?') +
                           ' \\u00B7 motor v' + (d.motor || '\\u2014');
  }).catch(function(){});
}
loadVersions();

// ── temperature graph ───────────────────────────────────────────────────────
function fmtClock(sec){
  var d = new Date(sec * 1000);
  return ('0'+d.getHours()).slice(-2) + ':' + ('0'+d.getMinutes()).slice(-2);
}
function buildTempSVG(samples){
  var W = 1000, H = 320, mL = 44, mR = 12, mT = 14, mB = 26;
  var temps = [];
  samples.forEach(function(s){ if (s[1]!=null) temps.push(s[1]); if (s[2]!=null) temps.push(s[2]); });
  if (!temps.length) return null;
  var lo = Math.min.apply(null, temps), hi = Math.max.apply(null, temps);
  lo = Math.floor((lo - 3) / 5) * 5; hi = Math.ceil((hi + 3) / 5) * 5;
  if (hi - lo < 10) hi = lo + 10;
  var t0 = samples[0][0], t1 = samples[samples.length-1][0];
  var span = Math.max(1, t1 - t0);
  function X(t){ return mL + (t - t0) / span * (W - mL - mR); }
  function Y(v){ return mT + (1 - (v - lo) / (hi - lo)) * (H - mT - mB); }
  var svg = '<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none" '
          + 'xmlns="http://www.w3.org/2000/svg" class="tchart">';
  for (var v = lo; v <= hi; v += 5){
    var y = Y(v);
    svg += '<line x1="'+mL+'" y1="'+y+'" x2="'+(W-mR)+'" y2="'+y+'" class="grid"/>';
    svg += '<text x="'+(mL-6)+'" y="'+(y+3)+'" class="ylab">'+v+'\\u00B0</text>';
  }
  [t0, t0+span/2, t1].forEach(function(t){
    svg += '<text x="'+X(t)+'" y="'+(H-8)+'" class="xlab">'+fmtClock(t)+'</text>';
  });
  function line(idx, cls){
    var pts = '', started = false;
    samples.forEach(function(s){
      var v = s[idx];
      if (v == null){ started = false; return; }
      pts += (started ? ' L' : 'M') + X(s[0]).toFixed(1) + ' ' + Y(v).toFixed(1);
      started = true;
    });
    return pts ? '<path d="'+pts+'" class="'+cls+'"/>' : '';
  }
  svg += line(1, 'lcam') + line(2, 'lmot') + '</svg>';
  return svg;
}
function loadTemps(){
  fetch('/temps').then(function(r){ return r.json(); }).then(function(d){
    var box = document.getElementById('temp-chart');
    var s = d.samples || [];
    var last = s.length ? s[s.length-1] : null;
    var cam = last && last[1]!=null ? last[1].toFixed(1)+'\\u00B0C' : '\\u2014';
    var mot = last && last[2]!=null ? last[2].toFixed(1)+'\\u00B0C' : '\\u2014';
    document.querySelector('#temp-cam b').textContent = cam;
    document.querySelector('#temp-mot b').textContent = mot;
    document.getElementById('temp-range').textContent =
      s.length ? (s.length + ' samples \\u00B7 last ' + d.hours + 'h') : '';
    var svg = buildTempSVG(s);
    box.innerHTML = svg || '<span class="temp-empty">No samples yet\\u2026</span>';
  }).catch(function(){});
}
setInterval(function(){
  if (document.getElementById('tab-temps').classList.contains('active')) loadTemps();
}, 30000);

// RealSense pipeline control (only present when SDK active)
function setPipeBadge(running) {
  var b = document.getElementById('pipe-badge');
  if (!b) return;
  b.className = 'pipe-badge ' + (running ? 'on' : 'off');
  b.textContent = running ? 'running' : 'stopped';
}
function pollPipeline() {
  if (!document.getElementById('pipe-badge')) return;
  fetch('/pipeline').then(function(r){ return r.json(); })
  .then(function(d){ setPipeBadge(d.running); }).catch(function(){});
}
function pipeStatus(msg, ok) {
  var s = document.getElementById('pipe-status');
  if (!s) return;
  s.textContent = msg; s.style.color = ok ? 'var(--green)' : 'var(--red)';
  setTimeout(function(){ s.textContent = ''; }, 4000);
}
function stopPipeline() {
  fetch('/pipeline', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action:'stop'})})
  .then(function(r){ return r.json(); })
  .then(function(d){ if (d.ok){ setPipeBadge(false); pipeStatus('Stopped \\u2713', true); }
                     else pipeStatus(d.error||'error', false); })
  .catch(function(){ pipeStatus('Network error', false); });
}
function restartPipeline() {
  var delay = parseFloat(document.getElementById('pipe-delay').value) || 0;
  pipeStatus('Restarting in ' + delay + 's\\u2026', true);
  fetch('/pipeline', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action:'restart', delay: delay})})
  .then(function(r){ return r.json(); })
  .then(function(d){
    if (!d.ok) { pipeStatus(d.error||'error', false); return; }
    // poll back to running after the delay completes
    setTimeout(pollPipeline, (delay + 1.5) * 1000);
  })
  .catch(function(){ pipeStatus('Network error', false); });
}
pollPipeline();
setInterval(pollPipeline, 5000);

document.addEventListener('keydown', function(e){
  if (['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) return;
  if (e.key==='u'||e.key==='U') motorCmd('up');
  if (e.key==='d'||e.key==='D') motorCmd('down');
  if (e.key==='s'||e.key==='S') motorCmd('stop');
});
"""


def build_dashboard(cameras):
    cjson   = cam_json(cameras)
    n       = len(cameras)
    js      = HTML_JS.replace("CAMS_JSON", cjson)

    # Pipeline controls only make sense when the SDK drives the indoor camera.
    pipe_ctrl = (
        '<div class="pipe-ctrl">\n'
        '  <span class="pipe-title">Indoor camera pipeline</span>\n'
        '  <span class="pipe-badge" id="pipe-badge">&hellip;</span>\n'
        '  <button class="pc-btn pc-stop" onclick="stopPipeline()">&#9632; Stop</button>\n'
        '  <span class="pc-delay">restart delay\n'
        '    <input id="pipe-delay" type="number" min="0" max="30" step="0.5" value="2"/>s\n'
        '  </span>\n'
        '  <button class="pc-btn pc-restart" onclick="restartPipeline()">&#8635; Restart</button>\n'
        '  <span class="pipe-status" id="pipe-status"></span>\n'
        '</div>\n'
    ) if REALSENSE_SDK else ''

    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="UTF-8"/>\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1"/>\n'
        '<title>Coop Dashboard</title>\n'
        '<style>' + HTML_STYLE + '</style>\n'
        '</head>\n<body>\n'
        '<header>\n'
        '  <span class="logo">&#127313; Coop <span>// dashboard</span></span>\n'
        '  <span class="pill pill-on" id="conn-pill">Motor Pi</span>\n'
        '  <span class="ver" id="ver" title="dashboard / motor server versions" '
        'style="margin-left:auto">dash v' + VERSION + ' &middot; motor v&hellip;</span>\n'
        '  <span style="font-family:monospace;font-size:.65rem;color:var(--muted)">'
        + str(n) + ' camera(s)</span>\n'
        '</header>\n'
        '<nav class="tabs" role="tablist">\n'
        '  <button class="tab active" id="tab-btn-live" onclick="showTab(\'live\')">'
        '&#128247; Live</button>\n'
        '  <button class="tab" id="tab-btn-eggs" onclick="showTab(\'eggs\')">'
        '&#129370; Eggs</button>\n'
        '  <button class="tab" id="tab-btn-temps" onclick="showTab(\'temps\')">'
        '&#127777; Temps</button>\n'
        '</nav>\n'
        '<section class="tab-panel active" id="tab-live">\n'
        '<div class="cam-grid">\n'
        '  <div class="panel" id="pL"><div class="ph"><span>Indoor</span>&hellip;</div></div>\n'
        '  <div class="panel" id="pR"><div class="ph"><span>Outside</span>&hellip;</div></div>\n'
        '</div>\n'
        + pipe_ctrl +
        # ── door control: single inline component ──
        '<div class="door-panel">\n'
        '  <div class="door-head">\n'
        '    <span class="door-title">&#9881; Coop door</span>\n'
        '    <span class="door-badge db-unknown" id="door-badge">&hellip;</span>\n'
        '    <span class="limits">\n'
        '      <span class="lim" id="lim-top"><i class="ldot"></i>TOP</span>\n'
        '      <span class="lim" id="lim-bot"><i class="ldot"></i>BOTTOM</span>\n'
        '    </span>\n'
        '    <span class="motor-err" id="motor-err"></span>\n'
        '  </div>\n'
        '  <div class="motor-conn-warn" id="conn-warn">Cannot reach the motor Pi on port '
        + str(MOTOR_PORT) + '. Check the IP below and that motor_server.py is running.</div>\n'
        '  <div class="motor-btns">\n'
        '    <button class="mbtn mbtn-up"   id="btn-up"   onclick="motorCmd(\'up\')">'
        '<span class="mbtn-icon">&#8593;</span>Open</button>\n'
        '    <button class="mbtn mbtn-stop" id="btn-stop" onclick="motorCmd(\'stop\')">'
        '<span class="mbtn-icon">&#9632;</span>Force Stop</button>\n'
        '    <button class="mbtn mbtn-down" id="btn-down" onclick="motorCmd(\'down\')">'
        '<span class="mbtn-icon">&#8595;</span>Close</button>\n'
        '  </div>\n'
        '  <div class="ip-row">\n'
        '    <label class="ip-label" for="ip-input">Motor Pi IP</label>\n'
        '    <input class="ip-input" id="ip-input" type="text" spellcheck="false"\n'
        '           autocomplete="off" placeholder="100.x.x.x or hostname"/>\n'
        '    <span style="color:var(--muted);font-family:monospace;font-size:.7rem">:'
        + str(MOTOR_PORT) + '</span>\n'
        '    <button class="ip-save" onclick="saveMotorIp()">Save</button>\n'
        '    <span class="ip-status" id="ip-status"></span>\n'
        '  </div>\n'
        '  <div class="sched-row">\n'
        '    <span class="ip-label">Schedule</span>\n'
        '    <label class="sw"><input type="checkbox" id="sch-open-en"/>'
        '<span>Open at</span></label>\n'
        '    <input class="tm" id="sch-open-tm" type="time" value="07:00"/>\n'
        '    <label class="sw"><input type="checkbox" id="sch-close-en"/>'
        '<span>Close at</span></label>\n'
        '    <input class="tm" id="sch-close-tm" type="time" value="21:00"/>\n'
        '    <button class="ip-save" onclick="saveSchedule()">Save</button>\n'
        '    <span class="ip-status" id="sch-status"></span>\n'
        '  </div>\n'
        '  <div class="acts">\n'
        '    <span class="acts-label">Recent actions <i>(this session only)</i></span>\n'
        '    <ul class="acts-list" id="acts-list"><li class="acts-none">None yet</li></ul>\n'
        '  </div>\n'
        '  <div class="kbd-hint">\n'
        '    <div class="kh"><kbd>U</kbd> Open</div>\n'
        '    <div class="kh"><kbd>D</kbd> Close</div>\n'
        '    <div class="kh"><kbd>S</kbd> Force stop</div>\n'
        '  </div>\n'
        '</div>\n'
        '</section>\n'
        '<section class="tab-panel" id="tab-eggs">\n'
        '  <div class="egg-panel">\n'
        '    <div class="egg-head">\n'
        '      <span class="door-title">&#129370; Egg log</span>\n'
        '      <span class="egg-date" id="egg-today"></span>\n'
        '    </div>\n'
        '    <div class="egg-save">\n'
        '      <a class="ip-save" href="/eggs/download" download>'
        '&#11015; Save .db to this device</a>\n'
        '      <a class="pc-btn pc-restart" href="/eggs/export.csv" download>'
        '&#11015; CSV</a>\n'
        '      <button class="pc-btn pc-restart" onclick="'
        'document.getElementById(\'egg-file\').click()">&#11014; Restore</button>\n'
        '      <input type="file" id="egg-file" accept=".db,.csv" style="display:none" '
        'onchange="uploadEggs(this)"/>\n'
        '      <span class="egg-unsaved" id="egg-unsaved"></span>\n'
        '      <span class="ip-status" id="egg-save-status"></span>\n'
        '    </div>\n'
        '    <div class="egg-today" id="egg-toggles"></div>\n'
        '    <div class="egg-grid-wrap"><div class="egg-grid" id="egg-grid"></div></div>\n'
        '    <div class="egg-totals" id="egg-totals"></div>\n'
        '    <div class="ip-row">\n'
        '      <label class="ip-label" for="egg-url">Egg database</label>\n'
        '      <input class="ip-input" id="egg-url" type="text" spellcheck="false"\n'
        '             autocomplete="off" placeholder="http://192.168.1.50:8090 '
        '(blank = store on Pi)"/>\n'
        '      <input class="ip-input" id="egg-token" type="password"\n'
        '             autocomplete="off" placeholder="token (optional)" style="flex:0 1 150px"/>\n'
        '      <button class="ip-save" onclick="saveEggDb()">Save</button>\n'
        '      <span class="ip-status" id="egg-db-status"></span>\n'
        '    </div>\n'
        '  </div>\n'
        '</section>\n'
        '<section class="tab-panel" id="tab-temps">\n'
        '  <div class="temp-panel">\n'
        '    <div class="temp-head">\n'
        '      <span class="temp-now" id="temp-cam"><i class="dot cam"></i>'
        'Camera Pi <b>&mdash;</b></span>\n'
        '      <span class="temp-now" id="temp-mot"><i class="dot mot"></i>'
        'Motor Pi <b>&mdash;</b></span>\n'
        '      <span class="temp-range" id="temp-range"></span>\n'
        '    </div>\n'
        '    <div class="temp-chart" id="temp-chart">'
        '<span class="temp-empty">No samples yet&hellip;</span></div>\n'
        '  </div>\n'
        '</section>\n'
        '<script>\n' + js + '\n</script>\n'
        '</body>\n</html>'
    )


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def parse_qs(self):
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = {}
        for part in qs.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k] = v
        return params

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _download(self, body, filename, ctype="application/octet-stream"):
        """Send bytes as a browser download (saves to the viewer's machine)."""
        if isinstance(body, str):
            body = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/":
            cameras = get_cameras()
            body    = build_dashboard(cameras).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/feed":
            device = self.parse_qs().get("device", "")
            # RealSense nodes are owned by the SDK (RSUSB backend detaches the
            # kernel uvc driver, so /dev/videoN vanishes while streaming). Only
            # filesystem-check UVC devices, which ffmpeg opens directly.
            if not device or (not is_realsense_node(device)
                              and not os.path.exists(device)):
                self.send_error(400, "Bad device")
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            peer = self.client_address[0]

            if is_realsense_node(device):
                # Indoor camera: SDK pipeline, not ffmpeg.
                hk = "intel"
                q  = realsense_hub.add_client(hk)
                print(f"[feed] +{peer} {device} ({hk}) viewers={len(realsense_hub.clients[hk])}")
                try:
                    while True:
                        frame = q.get(timeout=10)
                        self.wfile.write(
                            b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                        )
                        self.wfile.flush()
                except Exception as e:
                    print(f"[feed] -{peer} {device} ({hk}) closed: {e}")
                finally:
                    realsense_hub.remove_client(hk, q)
                return

            stream = get_stream(device, "uvc")
            q      = stream.add_client()
            print(f"[feed] +{peer} {device} (uvc) viewers={len(stream.clients)}")
            try:
                while True:
                    frame = q.get(timeout=10)
                    self.wfile.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                    )
                    self.wfile.flush()
            except Exception as e:
                print(f"[feed] -{peer} {device} (uvc) closed: {e}")
            finally:
                stream.remove_client(q)

        elif path == "/motor":
            ok, result = motor_get()
            if ok:
                self._json(200, result)          # full status: state/door_state/top/bottom
            else:
                self._json(502, {"error": result})

        elif path == "/config":
            url, token = get_egg_db()
            self._json(200, {"motor_pi_ip": get_motor_ip(), "motor_port": MOTOR_PORT,
                             "egg_db_url": url, "egg_db_token": token})

        elif path == "/pipeline":
            self._json(200, {"sdk": REALSENSE_SDK,
                             "running": bool(realsense_hub and realsense_hub.is_running())})

        elif path == "/version":
            self._json(200, {"dashboard": VERSION, "motor": motor_version()})

        elif path == "/schedule":
            self._json(200, get_schedule())

        elif path == "/eggs":
            try:
                days = max(1, min(int(self.parse_qs().get("days", 14)), 90))
            except ValueError:
                days = 14
            url, _ = get_egg_db()
            self._json(200, {"hens": HENS, "days": eggs_range(days),
                             "today": time.strftime("%Y-%m-%d"),
                             "store": url or "",
                             "online": _egg_online,
                             "unsaved": _egg_dirty,
                             "saved_at": _egg_saved})

        elif path == "/eggs/download":
            stamp = time.strftime("%Y-%m-%d")
            self._download(eggs_db_bytes(), f"eggs-{stamp}.db")
            mark_eggs_downloaded()          # downloading the .db counts as saving

        elif path == "/eggs/export.csv":
            stamp = time.strftime("%Y-%m-%d")
            self._download(eggs_csv(), f"eggs-{stamp}.csv", "text/csv; charset=utf-8")

        elif path == "/actions":
            with _actions_lock:
                self._json(200, {"actions": list(_actions)})

        elif path == "/temps":
            with _temps_lock:
                samples = list(_temps)
            self._json(200, {"samples": samples, "interval": TEMP_SAMPLE_SEC,
                             "hours": TEMP_HISTORY_HOURS})

        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/motor", "/config", "/pipeline", "/schedule",
                        "/eggs", "/eggs/save", "/eggs/reload", "/eggs/upload"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))

        if path == "/eggs/upload":          # raw .db bytes, not JSON
            if length <= 0 or length > 20 * 1024 * 1024:
                self._json(400, {"ok": False, "error": "bad upload size"})
                return
            ok, msg = eggs_load_bytes(self.rfile.read(length))
            self._json(200 if ok else 400,
                       {"ok": ok, "msg" if ok else "error": msg})
            return

        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            self._json(400, {"error": "bad json"})
            return

        if path == "/config":
            if "egg_db_url" in data:      # egg store settings
                ok, result = set_egg_db(data.get("egg_db_url", ""),
                                        data.get("egg_db_token", ""))
                if ok:
                    _load_eggs()          # re-read from the new store
                    self._json(200, {"ok": True, "egg_db_url": result})
                else:
                    self._json(400, {"ok": False, "error": result})
                return
            ok, result = set_motor_ip(data.get("motor_pi_ip", ""))
            if ok:
                self._json(200, {"ok": True, "motor_pi_ip": result})
            else:
                self._json(400, {"ok": False, "error": result})
            return

        if path == "/schedule":
            ok, result = set_schedule(data)
            if ok:
                self._json(200, {"ok": True, "schedule": result})
            else:
                self._json(400, {"ok": False, "error": result})
            return

        if path == "/eggs":
            ok, err = set_egg(data.get("date", ""), data.get("hen", ""),
                              bool(data.get("laid")))
            if ok:
                self._json(200, {"ok": True, "unsaved": _egg_dirty})
            else:
                self._json(400, {"ok": False, "error": err})
            return

        if path == "/eggs/save":
            ok, msg = save_eggs()
            self._json(200 if ok else 502, {"ok": ok, "msg" if ok else "error": msg})
            return

        if path == "/eggs/reload":
            _load_eggs()          # discard memory, re-pull from the laptop
            self._json(200, {"ok": _egg_online is True, "online": _egg_online})
            return

        if path == "/pipeline":
            action = data.get("action", "")
            device = data.get("device", "")

            # Pick the target: a specific UVC device, or the shared RealSense hub.
            if device and not is_realsense_node(device):
                with _streams_lock:
                    target = _streams.get(device)
                if target is None:
                    self._json(400, {"ok": False, "error": "no active stream for device"})
                    return
            else:
                target = realsense_hub
                if target is None:
                    self._json(400, {"ok": False, "error": "RealSense SDK not available"})
                    return

            if action == "stop":
                target.stop()
                self._json(200, {"ok": True, "running": False})
            elif action == "restart":
                try:
                    delay = float(data.get("delay", 2.0))
                except (TypeError, ValueError):
                    delay = 2.0
                delay = max(0.0, min(delay, 30.0))   # clamp to a sane range
                target.restart(delay)
                self._json(200, {"ok": True, "delay": delay})
            else:
                self._json(400, {"ok": False, "error": f"unknown action: {action}"})
            return

        cmd = data.get("cmd", "")
        if cmd not in ("up", "down", "stop"):
            self._json(400, {"error": f"unknown cmd: {cmd}"})
            return
        ok, result = motor_post(cmd)
        log_action(cmd, ok, result)
        if ok:
            self._json(200, {"ok": True, "state": result})
        else:
            self._json(502, {"ok": False, "error": result})


def _cleanup():
    """Release all camera devices on exit so the next start isn't blocked:
    stop the RealSense pipeline and kill every UVC ffmpeg subprocess."""
    if realsense_hub is not None:
        print("[dashboard] releasing RealSense device...")
        realsense_hub.stop()
    with _streams_lock:
        streams = list(_streams.values())
    for s in streams:
        print(f"[dashboard] stopping ffmpeg for {s.device}...")
        s.stop()


if __name__ == "__main__":
    import signal, atexit

    _load_config()
    _load_eggs()
    threading.Thread(target=_temp_sampler, daemon=True).start()
    threading.Thread(target=_scheduler, daemon=True).start()
    cams = get_cameras(force=True)     # warm the cache while the device is idle
    print(f"[dashboard] cameras: {[c['name'] for c in cams] or 'none'}")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"[dashboard] http://0.0.0.0:{PORT}")
    print(f"[dashboard] phone  -> http://100.86.37.14:{PORT}")
    print(f"[dashboard] motor  -> {motor_base()}")

    atexit.register(_cleanup)

    # systemd sends SIGTERM (not KeyboardInterrupt) on stop/restart. Translate it
    # to KeyboardInterrupt so it propagates out of serve_forever in THIS (main)
    # thread — calling server.shutdown() from the handler would deadlock, since
    # shutdown() blocks waiting on the very loop the handler interrupted.
    def _on_signal(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_signal)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] shutting down.")
    finally:
        _cleanup()                 # idempotent; atexit also covers hard paths
        print("[dashboard] shut down.")
