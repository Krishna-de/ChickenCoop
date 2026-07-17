# ChickenCoop 🐔

Automated chicken coop system built on two Raspberry Pis.

- **Camera Pi (Pi 5)** — live dual-camera dashboard (RealSense RGB + IR) served over HTTP
- **Motor Pi (Pi 4 B)** — NEMA 17 stepper motor control via A4988 driver for the coop door

Remote access via Tailscale from any device.

---

## Hardware

| Component | Details |
|---|---|
| Camera Pi | Raspberry Pi 5 |
| Motor Pi | Raspberry Pi 4 B |
| Cameras | Intel RealSense D4xx (RGB `/dev/video4` + IR `/dev/video2`) + UVC webcam |
| Stepper motor | NEMA 17 — 17HE15-1504S |
| Driver | A4988 |
| Power supply | 12V 2A barrel jack |
| Networking | Tailscale VPN |

### Motor wiring (A4988)

| Pin | GPIO | Board pin |
|---|---|---|
| STEP | GPIO 17 | Pin 11 |
| DIR | GPIO 27 | Pin 13 |
| EN | GPIO 22 | Pin 15 |
| SLEEP + RESET | 3.3V | Pin 17 |

Stepper wire order: **BLK, BLU, GRN, RED**
V_ref: ~0.8V

---

## Files

```
dashboard.py                    # Camera Pi — web dashboard + MJPEG streams + motor relay
motor_server.py                 # Motor Pi — HTTP API driving GPIO
systemd/coop-dashboard.service  # systemd unit for camera Pi
systemd/motor-server.service    # systemd unit for motor Pi
install.sh                      # deploy services in one command
```

---

## Setup

### Camera Pi

```bash
sudo apt install ffmpeg v4l-utils python3
git clone https://github.com/Krishna-de/ChickenCoop.git
cd ChickenCoop
```

Edit `dashboard.py` line 22 and set your motor Pi's Tailscale IP:
```python
MOTOR_PI_IP = "100.x.x.x"
```

Install and start the service:
```bash
./install.sh dashboard
```

Dashboard available at: `http://<camera-pi-tailscale-ip>:8080`

---

### Motor Pi

```bash
sudo apt install python3-rpi.gpio python3
git clone https://github.com/Krishna-de/ChickenCoop.git
cd ChickenCoop
./install.sh motor
```

---

## Remote access (Tailscale)

Tailscale puts both Pis and your phone on one private WireGuard network. **No port forwarding, nothing exposed to the internet**, and it works from behind CGNAT. The free personal tier covers this project (up to 100 devices).

### 1. Create an account

Sign up at [tailscale.com](https://tailscale.com) (Google/GitHub/Microsoft login). Use the **same account** on every device below — that's what puts them on one network.

### 2. Install on both Pis

Run on the **camera Pi** and the **motor Pi**:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

`tailscale up` prints an authentication URL. Open it in any browser and sign in — that links the Pi to your account. The `tailscaled` service is enabled on boot automatically, so this survives reboots.

### 3. Get each Pi's IP

```bash
tailscale ip -4        # this machine, e.g. 100.86.37.14
tailscale status       # every device on your network + its IP
```

Tailscale IPs always start with `100.` and are **stable** — they don't change on reboot or when you move networks.

### 4. Stop the keys from expiring

By default a device's auth key expires (~180 days) and the Pi silently drops off the network — bad for a headless coop. Fix it once:

**Admin console → Machines → ⋯ next to each Pi → Disable key expiry.**

### 5. Point the dashboard at the motor Pi

Open the dashboard and put the **motor Pi's** Tailscale IP in the *Motor Pi IP* box, then Save. It's persisted to `dashboard_config.json` — no code edit, no restart.

Verify the two Pis can see each other (run on the camera Pi):
```bash
ping -c 3 <motor-pi-tailscale-ip>
curl http://<motor-pi-tailscale-ip>:8081/motor    # should return JSON
```

### 6. Phone access

Install the Tailscale app (iOS/Android), sign in with the same account, toggle the VPN on. Then open:
```
http://<camera-pi-tailscale-ip>:8080
```

### Optional: nicer hostnames (MagicDNS)

Enable **MagicDNS** in the admin console (DNS tab) and use names instead of IPs:
```
http://camera-pi:8080
```

### Optional: real HTTPS

Gives a valid certificate (no browser warning) on a `*.ts.net` name:
```bash
sudo tailscale serve --bg https / http://localhost:8080
sudo tailscale serve status      # shows the public https URL
```

### Troubleshooting

| Symptom | Check |
|---|---|
| Pi not in `tailscale status` | `sudo systemctl status tailscaled`, re-run `sudo tailscale up` |
| Dashboard says "Motor Pi offline" | `ping` the motor Pi's Tailscale IP; confirm the IP saved in the dashboard matches `tailscale ip -4` on the motor Pi |
| Worked, then stopped after months | Key expiry — disable it (step 4) and re-authenticate |
| Phone can't connect | Tailscale VPN toggle actually on? Same account? |

---

## Camera streams

| Camera | Node | Resolution | Format |
|---|---|---|---|
| RealSense RGB | `/dev/video4` | 640×480 | Auto |
| RealSense IR | `/dev/video2` | 640×480 | UYVY |
| UVC webcam | auto-detected | 1920×1080 | Auto |

RGB and IR auto-assign to left/right slots on page load. IR stream is staggered by 2 seconds to avoid USB contention.

---

## Motor API

| Method | Endpoint | Body | Response |
|---|---|---|---|
| POST | `/motor` | `{"cmd": "up"}` | `{"ok": true, "state": "up"}` |
| POST | `/motor` | `{"cmd": "down"}` | `{"ok": true, "state": "down"}` |
| POST | `/motor` | `{"cmd": "stop"}` | `{"ok": true, "state": "stopped"}` |
| GET | `/motor` | — | `{"state": "up"\|"down"\|"stopped"}` |

---

## Dashboard controls

- **U** — door up
- **D** — door down  
- **S** — stop

Motor state polls every 4 seconds. Shows offline warning if motor Pi is unreachable.

---

## Service management

Both `dashboard.py` and `motor_server.py` run as systemd services (`coop-dashboard` on the camera Pi, `motor-server` on the motor Pi). Use the matching service name for the Pi you are on.

### Live logs

```bash
# follow logs in real time (Ctrl-C to stop following — does NOT stop the service)
journalctl -u coop-dashboard -f

# last 200 lines, then follow
journalctl -u coop-dashboard -n 200 -f

# logs since the last boot
journalctl -u coop-dashboard -b

# logs from the last 15 minutes
journalctl -u coop-dashboard --since "15 min ago"
```

These show the app's own output — camera enumeration, `[realsense] pipeline started`, per-viewer `[feed] +…` connects, and motor errors.

### Status, start/stop, restart

```bash
systemctl status coop-dashboard          # running? shows the loaded unit path + recent log lines
sudo systemctl restart coop-dashboard    # restart (SIGTERM releases the RealSense cleanly)
sudo systemctl stop coop-dashboard       # stop
sudo systemctl start coop-dashboard      # start
sudo systemctl enable coop-dashboard     # start automatically on boot
```

### Edit the service file

The installed unit (the one systemd runs) lives at `/etc/systemd/system/`. The repo keeps a source copy under `systemd/`.

```bash
# find the live unit path
systemctl status coop-dashboard          # see the "Loaded:" line

# edit it directly
sudo nano /etc/systemd/system/coop-dashboard.service

# OR edit the repo copy and redeploy
nano systemd/coop-dashboard.service
sudo ./install.sh dashboard
```

After **any** change to a `.service` file, reload systemd before restarting (it caches unit files):

```bash
sudo systemctl daemon-reload
sudo systemctl restart coop-dashboard
```

---

## Troubleshooting

**IR stream blank** — try increasing `IR_START_DELAY = 3.0` in `dashboard.py`

**RGB drops when IR starts** — same fix, increase the delay

**Motor Pi offline in dashboard** — check `sudo systemctl status motor-server` on the motor Pi and confirm `MOTOR_PI_IP` is correct in `dashboard.py`

**Wrong IR node** — run `v4l2-ctl --list-devices` and update `REALSENSE_IR_NODE` in `dashboard.py`

**Motor hums but doesn't rotate** — swap the middle two wires (BLU/GRN) and verify SLEEP+RESET are tied to 3.3V
