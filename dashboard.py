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

# ── config ────────────────────────────────────────────────────────────────────
PORT        = 8080
FPS         = 10          # lower FPS reduces USB bandwidth contention on RealSense
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


# ── per-camera broadcaster ────────────────────────────────────────────────────

class CameraStream:
    def __init__(self, device, kind="uvc"):
        self.device  = device
        self.kind    = kind
        self.lock    = threading.Lock()
        self.clients = []
        self.running = False
        self.thread  = None

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
            buf = b""
            try:
                while True:
                    with self.lock:
                        if not self.clients:
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


# ── motor relay ───────────────────────────────────────────────────────────────

MOTOR_BASE = f"http://{MOTOR_PI_IP}:{MOTOR_PORT}/motor"


def motor_post(cmd):
    try:
        body = json.dumps({"cmd": cmd}).encode()
        req  = Request(MOTOR_BASE, data=body,
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
        with urlopen(MOTOR_BASE, timeout=3) as r:
            data = json.loads(r.read())
            return True, data.get("state", "unknown")
    except Exception as e:
        return False, str(e)


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
.section-label{width:100%;max-width:1100px;font-family:monospace;font-size:.65rem;
  font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);
  border-bottom:1px solid var(--border);padding-bottom:6px}
.cam-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;width:100%;max-width:1100px}
@media(max-width:600px){.cam-grid{grid-template-columns:1fr}}
.panel{position:relative;background:#000;border:1px solid var(--border);
  border-radius:var(--r);overflow:hidden;aspect-ratio:4/3;
  display:flex;align-items:center;justify-content:center}
.panel.left{border-color:var(--green)}
.panel.right{border-color:var(--purple)}
.panel.right::after{content:'';position:absolute;inset:0;
  background:linear-gradient(135deg,rgba(181,122,255,.04) 0%,transparent 60%);
  pointer-events:none}
.panel img{width:100%;height:100%;object-fit:contain;display:block}
.ph{color:var(--muted);font-family:monospace;font-size:.75rem;text-align:center;line-height:2}
.ph span{display:block;font-size:.58rem;opacity:.4;margin-bottom:4px;
  letter-spacing:.1em;text-transform:uppercase}
.badge{position:absolute;top:9px;right:9px;color:#fff;font-family:monospace;
  font-size:.58rem;font-weight:700;letter-spacing:.12em;padding:2px 7px;
  border-radius:4px;animation:pulse 2s infinite}
.badge.L{background:var(--live)}.badge.R{background:#7c3aed}
.plbl{position:absolute;top:9px;left:9px;font-family:monospace;font-size:.58rem;
  font-weight:700;padding:2px 7px;border-radius:4px}
.plbl.L{background:rgba(0,201,125,.18);color:var(--green)}
.plbl.R{background:rgba(181,122,255,.18);color:var(--purple)}
.slbl{position:absolute;bottom:9px;left:9px;font-family:monospace;font-size:.6rem;
  padding:2px 8px;border-radius:4px;background:rgba(0,0,0,.55);color:var(--text);
  max-width:55%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.disc{position:absolute;bottom:9px;right:9px;font-family:monospace;font-size:.62rem;
  font-weight:700;padding:4px 10px;border-radius:6px;cursor:pointer;
  border:1px solid rgba(255,76,76,.35);background:rgba(255,76,76,.1);color:var(--red)}
.disc:hover{background:rgba(255,76,76,.25)}
.re-msg{display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:10px;width:100%;height:100%;color:var(--muted);font-family:monospace;font-size:.75rem}
.re-btn{font-family:monospace;font-size:.66rem;font-weight:700;padding:5px 14px;
  border-radius:6px;cursor:pointer}
.re-btn.L{background:var(--green-dim);color:var(--green);border:1px solid rgba(0,201,125,.35)}
.re-btn.R{background:var(--purple-dim);color:var(--purple);border:1px solid rgba(181,122,255,.4)}
.cam-list{display:flex;flex-wrap:wrap;gap:10px;width:100%;max-width:1100px}
.cam-row{display:flex;align-items:center;gap:10px;padding:10px 14px;
  background:var(--surface);border:1px solid var(--border);border-radius:var(--r);flex:1 1 240px}
.cam-row.aL{border-color:var(--green);background:#0a1910}
.cam-row.aR{border-color:var(--purple);background:#120a1f}
.tag{font-family:monospace;font-size:.58rem;font-weight:700;letter-spacing:.1em;
  padding:2px 6px;border-radius:4px;text-transform:uppercase;flex-shrink:0}
.tag-uvc  {background:rgba(0,201,125,.15);color:var(--green);border:1px solid rgba(0,201,125,.3)}
.tag-intel{background:rgba(58,143,255,.15);color:var(--blue);border:1px solid rgba(58,143,255,.3)}
.tag-ir   {background:rgba(181,122,255,.15);color:var(--purple);border:1px solid rgba(181,122,255,.3)}
.ci{display:flex;flex-direction:column;gap:3px;flex:1}
.ci strong{font-size:.85rem}
.ci small{font-family:monospace;font-size:.66rem;color:var(--muted)}
.auto-badge{font-family:monospace;font-size:.56rem;font-weight:700;
  padding:1px 5px;border-radius:3px;letter-spacing:.08em;display:inline-block;margin-top:2px}
.auto-L{background:var(--green-dim);color:var(--green);border:1px solid rgba(0,201,125,.3)}
.auto-R{background:var(--purple-dim);color:var(--purple);border:1px solid rgba(181,122,255,.3)}
.sbtns{display:flex;gap:6px;margin-left:auto}
.sb{font-family:monospace;font-size:.68rem;font-weight:700;padding:4px 11px;
  border-radius:5px;cursor:pointer;border:1px solid var(--border);
  background:transparent;color:var(--muted)}
.sb:hover{border-color:var(--green2);color:var(--text)}
.sb.aL{border-color:rgba(0,201,125,.6);color:var(--green);background:var(--green-dim)}
.sb.aR{border-color:rgba(181,122,255,.6);color:var(--purple);background:var(--purple-dim)}
.no-cams{color:var(--muted);font-family:monospace;font-size:.82rem}
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
.kbd-hint{display:flex;gap:14px;flex-wrap:wrap}
.kh{display:flex;align-items:center;gap:6px;font-family:monospace;font-size:.65rem;color:var(--muted)}
kbd{background:var(--border);border:1px solid #2e3440;border-radius:4px;
  padding:2px 7px;font-size:.65rem;color:var(--text)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}
"""

HTML_JS = """
var CAMS  = CAMS_JSON;
var slots = {L: null, R: null};

var list = document.getElementById('cam-list');
CAMS.forEach(function(cam) {
  var row = document.createElement('div');
  row.className = 'cam-row';

  var tag = document.createElement('span');
  tag.className = 'tag tag-' + cam.kind;
  tag.textContent = cam.kind === 'ir' ? 'IR' : cam.kind.toUpperCase();

  var ci = document.createElement('div');
  ci.className = 'ci';
  var nameEl = document.createElement('strong');
  nameEl.textContent = cam.name;
  var devEl = document.createElement('small');
  devEl.textContent = cam.device;
  ci.appendChild(nameEl);
  ci.appendChild(devEl);
  if (cam.auto) {
    var ab = document.createElement('span');
    ab.className = 'auto-badge auto-' + cam.auto;
    ab.textContent = 'AUTO ' + cam.auto;
    ci.appendChild(ab);
  }

  var sbtns = document.createElement('div');
  sbtns.className = 'sbtns';
  var bL = document.createElement('button');
  bL.className = 'sb'; bL.textContent = 'L';
  bL.onclick = function(){ assign('L', cam); };
  var bR = document.createElement('button');
  bR.className = 'sb'; bR.textContent = 'R';
  bR.onclick = function(){ assign('R', cam); };
  sbtns.appendChild(bL); sbtns.appendChild(bR);

  row.appendChild(tag); row.appendChild(ci); row.appendChild(sbtns);
  list.appendChild(row);
  cam._row = row; cam._bL = bL; cam._bR = bR;
});

function assign(slot, cam) {
  CAMS.forEach(function(c) {
    if (slots[slot] === c.device) {
      c._row.classList.remove(slot === 'L' ? 'aL' : 'aR');
      (slot === 'L' ? c._bL : c._bR).classList.remove(slot === 'L' ? 'aL' : 'aR');
    }
  });
  slots[slot] = cam.device;
  cam._row.classList.add(slot === 'L' ? 'aL' : 'aR');
  (slot === 'L' ? cam._bL : cam._bR).classList.add(slot === 'L' ? 'aL' : 'aR');
  startStream(slot, cam.device, cam.name, cam.kind);
}

function startStream(slot, device, name, kind) {
  var panel = document.getElementById('p' + slot);
  panel.className = 'panel ' + (slot === 'L' ? 'left' : 'right');

  var img = document.createElement('img');
  img.id  = 'img' + slot;
  img.src = '/feed?device=' + device + '&t=' + Date.now();

  var lbl = document.createElement('div');
  lbl.className = 'plbl ' + slot;
  lbl.textContent = kind === 'ir' ? 'IR' : 'RGB';

  var badge = document.createElement('div');
  badge.className = 'badge ' + slot;
  badge.textContent = 'LIVE';

  var slbl = document.createElement('div');
  slbl.className = 'slbl';
  slbl.textContent = name;

  var disc = document.createElement('button');
  disc.className = 'disc';
  disc.textContent = '\\u25A0 Disconnect';
  disc.onclick = function(){ disconnect(slot, device); };

  panel.innerHTML = '';
  panel.appendChild(img); panel.appendChild(lbl);
  panel.appendChild(badge); panel.appendChild(slbl); panel.appendChild(disc);
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
  CAMS.forEach(function(c){
    if (c.device === device){
      c._row.classList.remove(slot === 'L' ? 'aL' : 'aR');
      (slot === 'L' ? c._bL : c._bR).classList.remove(slot === 'L' ? 'aL' : 'aR');
    }
  });
  slots[slot] = null;
}

// auto-assign RGB first, then IR after a short delay so the browser
// requests RGB before IR — matches the server-side stagger
var rgbCam = CAMS.find(function(c){ return c.auto === 'L'; });
var irCam  = CAMS.find(function(c){ return c.auto === 'R'; });
if (rgbCam) assign('L', rgbCam);
setTimeout(function(){
  if (irCam) assign('R', irCam);
}, 2500);
// assign any unassigned UVC to first free slot
CAMS.forEach(function(cam){
  if (cam.kind === 'uvc' && !slots.L) assign('L', cam);
  else if (cam.kind === 'uvc' && !slots.R) assign('R', cam);
});

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
    no_cams = "" if cameras else '<p class="no-cams">No cameras detected.</p>'
    js      = HTML_JS.replace("CAMS_JSON", cjson)

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
        '  <span style="font-family:monospace;font-size:.65rem;color:var(--muted);margin-left:auto">'
        + str(n) + ' camera(s)</span>\n'
        '</header>\n'
        '<div class="section-label">&#128247; Live cameras</div>\n'
        '<div class="cam-grid">\n'
        '  <div class="panel" id="pL"><div class="ph"><span>RGB</span>Loading&hellip;</div></div>\n'
        '  <div class="panel" id="pR"><div class="ph"><span>IR</span>Loading&hellip;</div></div>\n'
        '</div>\n'
        '<div class="cam-list" id="cam-list">' + no_cams + '</div>\n'
        '<div class="section-label">&#9881; Door motor</div>\n'
        '<div class="motor-panel">\n'
        '  <div class="motor-status-row">\n'
        '    <span class="motor-title">NEMA 17 // A4988</span>\n'
        '    <span class="state-badge state-stopped" id="state-badge">Stopped</span>\n'
        '    <span class="motor-err" id="motor-err"></span>\n'
        '  </div>\n'
        '  <div class="motor-conn-warn" id="conn-warn">Cannot reach motor Pi at '
        + MOTOR_PI_IP + ':' + str(MOTOR_PORT) + '. Check that motor_server.py is running.</div>\n'
        '  <div class="motor-btns">\n'
        '    <button class="mbtn mbtn-up"   id="btn-up"   onclick="motorCmd(\'up\')">'
        '<span class="mbtn-icon">&#8593;</span>UP</button>\n'
        '    <button class="mbtn mbtn-stop" id="btn-stop" onclick="motorCmd(\'stop\')">'
        '<span class="mbtn-icon">&#9632;</span>STOP</button>\n'
        '    <button class="mbtn mbtn-down" id="btn-down" onclick="motorCmd(\'down\')">'
        '<span class="mbtn-icon">&#8595;</span>DOWN</button>\n'
        '  </div>\n'
        '  <div class="kbd-hint">\n'
        '    <div class="kh"><kbd>U</kbd> Door up</div>\n'
        '    <div class="kh"><kbd>D</kbd> Door down</div>\n'
        '    <div class="kh"><kbd>S</kbd> Stop</div>\n'
        '  </div>\n'
        '</div>\n'
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
            cameras = list_cameras()
            body    = build_dashboard(cameras).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/feed":
            device = self.parse_qs().get("device", "")
            if not device or not os.path.exists(device):
                self.send_error(400, "Bad device")
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            cameras  = list_cameras()
            cam_info = next((c for c in cameras if c["device"] == device), {})
            kind     = cam_info.get("kind", "uvc")
            stream   = get_stream(device, kind)
            q        = stream.add_client()
            try:
                while True:
                    frame = q.get(timeout=10)
                    self.wfile.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                    )
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                stream.remove_client(q)

        elif path == "/motor":
            ok, result = motor_get()
            if ok:
                self._json(200, {"state": result})
            else:
                self._json(502, {"error": result})

        else:
            self.send_error(404)

    def do_POST(self):
        if self.path.split("?")[0] != "/motor":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            self._json(400, {"error": "bad json"})
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


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"[dashboard] http://0.0.0.0:{PORT}")
    print(f"[dashboard] phone  -> http://100.86.37.14:{PORT}")
    print(f"[dashboard] motor  -> {MOTOR_BASE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] shutting down.")
