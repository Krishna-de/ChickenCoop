#!/usr/bin/env python3
""" 
dashboard.py — runs on the CAMERA Pi
Serves the unified coop dashboard:
  - RealSense RGB (left) + IR (right) auto-assigned on load
  - UVC webcam assignable to either slot
  - motor controls relayed to the motor Pi

Set MOTOR_PI_IP below to your motor Pi Tailscale or LAN IP.
"""

import subprocess, os, re, threading, queue, time, json
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
VERSION     = "1.1.0"
PORT        = 8080
FPS         = 10          # ffmpeg/UVC: lower FPS reduces USB bandwidth contention
REALSENSE_FPS = 15        # SDK serves both streams from one handle — no bandwidth race,
                          # so use a valid D4xx framerate (6/15/30/60; 10 is NOT valid)
MOTOR_PI_IP = "YOUR_MOTOR_PI_IP"
MOTOR_PORT  = 8081

RES = {
    "uvc":   (1920, 1080),
    "intel": (640,  480),
    "ir":    (640,  480),
}

# RealSense node map — video0=Depth, video2=IR, video4=RGB
REALSENSE_RGB_NODE = "/dev/video4"
REALSENSE_IR_NODE  = "/dev/video2"

# Delay before IR stream opens — gives RGB time to negotiate the device first
IR_START_DELAY = 2.0

# Keep a UVC ffmpeg stream alive this long after the last viewer leaves, so a
# page refresh reuses it instead of racing a stop→start re-open (device busy).
IDLE_GRACE = 5.0

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
                                        "device": dev, "name": "RealSense RGB",
                                        "kind": "intel", "auto": "L"})
                    elif dev == REALSENSE_IR_NODE:
                        cameras.append({"index": int(re.search(r"\d+", dev).group()),
                                        "device": dev, "name": "RealSense IR",
                                        "kind": "ir", "auto": "R"})
            else:
                for dev in nodes:
                    if re.match(r"/dev/video\d+$", dev):
                        label = name.split("(")[0].strip() or "USB Camera"
                        cameras.append({"index": int(re.search(r"\d+", dev).group()),
                                        "device": dev, "name": label,
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
                                "name": f"USB Camera {i}", "kind": "uvc", "auto": ""})
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

class CameraStream:
    def __init__(self, device, kind="uvc"):
        self.device  = device
        self.kind    = kind
        self.lock    = threading.Lock()
        self.clients = []
        self.running = False
        self.thread  = None
        self.proc    = None      # the live ffmpeg subprocess, for external stop

    def stop(self):
        """Stop streaming and kill the ffmpeg child. Without this an abrupt
        process exit orphans ffmpeg, leaving /dev/videoN held."""
        with self.lock:
            self.running = False
            t = self.thread
            p = self.proc
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass
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
        base = ["ffmpeg", "-f", "v4l2",
                "-framerate", str(FPS),
                "-video_size", f"{w}x{h}"]

        if self.kind == "ir":
            # IR node exports UYVY alongside GREY — uyvy422 converts cleanly to MJPEG
            base += ["-input_format", "uyvy422"]
        elif self.kind == "intel":
            # RGB node: let ffmpeg auto-negotiate, no format override needed
            pass

        base += ["-i", self.device,
                 "-c:v", "mjpeg", "-q:v", "5", "-f", "mjpeg", "pipe:1"]
        return base

    def _run(self):
        # Stagger IR open so RGB gets the USB bus first
        if self.kind == "ir":
            print(f"[cam:ir] waiting {IR_START_DELAY}s before opening {self.device}")
            time.sleep(IR_START_DELAY)

        print(f"[cam:{self.kind}] starting {self.device}")
        while self.running:
            w, h   = RES.get(self.kind, (640, 480))
            cmd    = self._build_cmd(w, h)
            ffmpeg = subprocess.Popen(cmd,
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL)
            self.proc = ffmpeg
            buf = b""
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
                            with self.lock:
                                for q in list(self.clients):
                                    try:
                                        q.put_nowait(frame)
                                    except queue.Full:
                                        pass
                        else:
                            break
            finally:
                ffmpeg.terminate()
                ffmpeg.wait()
                self.proc = None

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
    """Drives RGB + IR from a single rs.pipeline.

    Both streams share one USB device context, so there is no concurrent-open
    race — the bandwidth conflict that the ffmpeg stagger worked around cannot
    occur here. Frames are JPEG-encoded and fanned out to per-kind client
    queues, mirroring the CameraStream broadcaster contract.
    """

    def __init__(self):
        self.lock     = threading.Lock()
        self.clients  = {"intel": [], "ir": []}   # kind -> [queue, ...]
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
                has_clients = self.clients["intel"] or self.clients["ir"]
                if has_clients and not self.running:
                    self._start()
                    print(f"[realsense] restarted after {delay}s")
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
        w, h     = RES["intel"]
        pipeline = rs.pipeline()
        config   = rs.config()
        config.enable_stream(rs.stream.color, w, h, rs.format.rgb8, REALSENSE_FPS)
        config.enable_stream(rs.stream.infrared, 1, w, h, rs.format.y8, REALSENSE_FPS)  # index 1 = left IR
        try:
            pipeline.start(config)
        except Exception as e:
            print(f"[realsense] pipeline start failed: {e}")
            with self.lock:
                self.running = False
            return

        self.pipeline = pipeline
        print("[realsense] pipeline started — RGB+IR from one device handle")
        try:
            while self.running:
                with self.lock:
                    if not self.clients["intel"] and not self.clients["ir"]:
                        break
                try:
                    frames = pipeline.wait_for_frames(5000)
                except Exception as e:
                    print(f"[realsense] wait_for_frames: {e}")
                    break
                self._fan("intel", frames.get_color_frame())
                self._fan("ir",    frames.get_infrared_frame(1))
        finally:
            try:
                pipeline.stop()
            except Exception:
                pass
            self.pipeline = None
            with self.lock:
                self.running = False
            print("[realsense] pipeline stopped")

    def _fan(self, kind, frame):
        if not frame:
            return
        with self.lock:
            qs = list(self.clients[kind])
        if not qs:
            return
        img  = np.asanyarray(frame.get_data())
        data = encode_jpeg(img, gray=(kind == "ir"))
        if not data:
            return
        for q in qs:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass


realsense_hub = RealSenseHub() if REALSENSE_SDK else None


def is_realsense_node(device):
    return REALSENSE_SDK and device in (REALSENSE_RGB_NODE, REALSENSE_IR_NODE)


# ── motor relay ───────────────────────────────────────────────────────────────

# Motor Pi IP is runtime-configurable from the dashboard and persisted here so
# it survives restarts. MOTOR_PI_IP above is only the first-boot default.
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "dashboard_config.json")

_motor_ip   = MOTOR_PI_IP
_config_lock = threading.Lock()

# Accept dotted IPv4 or a hostname/Tailscale name; reject anything with chars
# that could smuggle a port, path, or scheme into the URL.
_IP_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")


def _load_config():
    global _motor_ip
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
        ip = data.get("motor_pi_ip")
        if ip and _IP_RE.match(ip):
            _motor_ip = ip
            print(f"[config] loaded motor Pi IP: {_motor_ip}")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[config] load failed ({e}); using default {_motor_ip}")


def get_motor_ip():
    with _config_lock:
        return _motor_ip


def set_motor_ip(ip):
    """Validate, store, and persist a new motor Pi IP. Returns (ok, msg)."""
    ip = (ip or "").strip()
    if not _IP_RE.match(ip):
        return False, "invalid IP or hostname"
    global _motor_ip
    with _config_lock:
        _motor_ip = ip
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump({"motor_pi_ip": ip}, f)
        except Exception as e:
            return False, f"saved in memory but write failed: {e}"
    print(f"[config] motor Pi IP set to {ip}")
    return True, ip


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
    try:
        with urlopen(motor_base(), timeout=3) as r:
            data = json.loads(r.read())
            return True, data.get("state", "unknown")
    except Exception as e:
        return False, str(e)


def motor_version():
    """Fetch the motor Pi's reported version. Returns the string or None."""
    try:
        with urlopen(motor_base(), timeout=3) as r:
            return json.loads(r.read()).get("version")
    except Exception:
        return None


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
var slots = {L: null, R: null, C: null};
var PANEL = {L: 'left', R: 'right', C: 'third'};
function kindLabel(kind){ return kind === 'ir' ? 'IR' : kind === 'uvc' ? 'UVC' : 'RGB'; }

function showTab(name){
  ['live', 'door'].forEach(function(t){
    document.getElementById('tab-' + t).classList.toggle('active', t === name);
    document.getElementById('tab-btn-' + t).classList.toggle('active', t === name);
  });
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
  if (img) img.src = '';
  var panel = document.getElementById('p' + slot);
  panel.className = 'panel';
  var msg = document.createElement('div'); msg.className = 're-msg';
  var txt = document.createElement('span'); txt.textContent = 'Stream stopped';
  var btn = document.createElement('button');
  btn.className = 're-btn ' + slot; btn.textContent = '\\u25B6 Reconnect';
  btn.onclick = function() {
    var cam = CAMS.find(function(c){ return c.device === device; });
    if (cam) assign(slot, cam);
  };
  msg.appendChild(txt); msg.appendChild(btn);
  panel.innerHTML = ''; panel.appendChild(msg);
  slots[slot] = null;
}

// Deterministic layout by kind: RGB -> Left, IR -> Right, UVC -> Center (third).
// (The 'auto' field and node order are not trusted here.) Extra UVC cams fall
// back to any leftover slot, placed only AFTER IR so they can't steal a panel.
var rgbCam = CAMS.find(function(c){ return c.kind === 'intel'; });
var irCam  = CAMS.find(function(c){ return c.kind === 'ir'; });
function fillUVC(){
  CAMS.forEach(function(cam){
    if (cam.kind !== 'uvc') return;
    if (!slots.C) assign('C', cam);          // UVC -> third pane
    else if (!slots.L) assign('L', cam);
    else if (!slots.R) assign('R', cam);
  });
}
if (rgbCam) assign('L', rgbCam);          // RGB always left
function placeIR(){ if (irCam) assign('R', irCam); fillUVC(); }
// SDK: instant (IR_DELAY_MS=0). ffmpeg fallback: stagger so RGB opens first.
if (IR_DELAY_MS > 0) setTimeout(placeIR, IR_DELAY_MS); else placeIR();
fillUVC();   // place UVC immediately when no RealSense is present

// motor
function setStateBadge(state) {
  var el = document.getElementById('state-badge');
  el.className = 'state-badge state-' + (state || 'stopped');
  el.textContent = state.charAt(0).toUpperCase() + state.slice(1);
  document.getElementById('btn-up').classList.toggle('active',   state === 'up');
  document.getElementById('btn-down').classList.toggle('active', state === 'down');
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
  fetch('/motor', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({cmd: cmd})})
  .then(function(r){ return r.json(); })
  .then(function(d){ if (d.ok) { setStateBadge(d.state); setErr(null); } else { setErr(d.error||'error'); } })
  .catch(function(){ setErr('Network error'); });
}
function pollMotor() {
  fetch('/motor')
  .then(function(r){ return r.json(); })
  .then(function(d){ if (d.state !== undefined){ setStateBadge(d.state); setErr(null); } else { setErr(d.error||'?'); } })
  .catch(function(){ setErr('Motor Pi unreachable'); });
}
setInterval(pollMotor, 4000);
pollMotor();

// motor Pi IP config
function loadConfig() {
  fetch('/config')
  .then(function(r){ return r.json(); })
  .then(function(d){ document.getElementById('ip-input').value = d.motor_pi_ip || ''; })
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
    # SDK serves both streams from one handle, so the IR open can be instant;
    # the ffmpeg fallback still needs the stagger to dodge the USB race.
    ir_delay = "0" if REALSENSE_SDK else "2500"
    js       = HTML_JS.replace("CAMS_JSON", cjson).replace("IR_DELAY_MS", ir_delay)

    # RealSense pipeline controls only make sense when the SDK drives the device.
    pipe_ctrl = (
        '<div class="pipe-ctrl">\n'
        '  <span class="pipe-title">RealSense pipeline</span>\n'
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
        '  <button class="tab" id="tab-btn-door" onclick="showTab(\'door\')">'
        '&#9881; Door control</button>\n'
        '</nav>\n'
        '<section class="tab-panel active" id="tab-live">\n'
        '<div class="cam-grid">\n'
        '  <div class="panel" id="pL"><div class="ph"><span>RGB</span>Loading&hellip;</div></div>\n'
        '  <div class="panel" id="pR"><div class="ph"><span>IR</span>Loading&hellip;</div></div>\n'
        '  <div class="panel" id="pC"><div class="ph"><span>UVC</span>Loading&hellip;</div></div>\n'
        '</div>\n'
        + pipe_ctrl +
        '</section>\n'
        '<section class="tab-panel" id="tab-door">\n'
        '<div class="motor-panel">\n'
        '  <div class="motor-status-row">\n'
        '    <span class="motor-title">NEMA 17 // A4988</span>\n'
        '    <span class="state-badge state-stopped" id="state-badge">Stopped</span>\n'
        '    <span class="motor-err" id="motor-err"></span>\n'
        '  </div>\n'
        '  <div class="motor-conn-warn" id="conn-warn">Cannot reach the motor Pi on port '
        + str(MOTOR_PORT) + '. Check the IP below and that motor_server.py is running.</div>\n'
        '  <div class="motor-btns">\n'
        '    <button class="mbtn mbtn-up"   id="btn-up"   onclick="motorCmd(\'up\')">'
        '<span class="mbtn-icon">&#8593;</span>UP</button>\n'
        '    <button class="mbtn mbtn-stop" id="btn-stop" onclick="motorCmd(\'stop\')">'
        '<span class="mbtn-icon">&#9632;</span>STOP</button>\n'
        '    <button class="mbtn mbtn-down" id="btn-down" onclick="motorCmd(\'down\')">'
        '<span class="mbtn-icon">&#8595;</span>DOWN</button>\n'
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
        '  <div class="kbd-hint">\n'
        '    <div class="kh"><kbd>U</kbd> Door up</div>\n'
        '    <div class="kh"><kbd>D</kbd> Door down</div>\n'
        '    <div class="kh"><kbd>S</kbd> Stop</div>\n'
        '  </div>\n'
        '</div>\n'
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
                # Single shared pipeline — no ffmpeg, no two-process race.
                # kind is derived from the device, so no v4l2-ctl call needed.
                hk = "intel" if device == REALSENSE_RGB_NODE else "ir"
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
                self._json(200, {"state": result})
            else:
                self._json(502, {"error": result})

        elif path == "/config":
            self._json(200, {"motor_pi_ip": get_motor_ip(), "motor_port": MOTOR_PORT})

        elif path == "/pipeline":
            self._json(200, {"sdk": REALSENSE_SDK,
                             "running": bool(realsense_hub and realsense_hub.is_running())})

        elif path == "/version":
            self._json(200, {"dashboard": VERSION, "motor": motor_version()})

        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/motor", "/config", "/pipeline"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            self._json(400, {"error": "bad json"})
            return

        if path == "/config":
            ok, result = set_motor_ip(data.get("motor_pi_ip", ""))
            if ok:
                self._json(200, {"ok": True, "motor_pi_ip": result})
            else:
                self._json(400, {"ok": False, "error": result})
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
