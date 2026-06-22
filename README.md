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

```bash
# install on each Pi
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# get the Pi's IP
tailscale ip
```

Install the Tailscale app on your phone and sign in with the same account. Then open:
```
http://<camera-pi-tailscale-ip>:8080
```

For HTTPS with a real certificate:
```bash
sudo tailscale serve --bg https / http://localhost:8080
```

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

## Troubleshooting

**IR stream blank** — try increasing `IR_START_DELAY = 3.0` in `dashboard.py`

**RGB drops when IR starts** — same fix, increase the delay

**Motor Pi offline in dashboard** — check `sudo systemctl status motor-server` on the motor Pi and confirm `MOTOR_PI_IP` is correct in `dashboard.py`

**Wrong IR node** — run `v4l2-ctl --list-devices` and update `REALSENSE_IR_NODE` in `dashboard.py`

**Motor hums but doesn't rotate** — swap the middle two wires (BLU/GRN) and verify SLEEP+RESET are tied to 3.3V
