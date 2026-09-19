# G1 Audio Driver

PulseAudio bridge for the Unitree G1's onboard 4-mic array and head speaker. Creates standard Linux audio devices so any application (Chrome, Discord, OBS, `arecord`, etc.) can use the G1's audio hardware transparently.

## What It Does

Creates two virtual PulseAudio devices on PC2 (Jetson Orin):

| Device | PA Name | Type | Description |
|--------|---------|------|-------------|
| Microphone | `g1_microphone` | Source (input) | 4-mic MEMS array in the G1's head |
| Speaker | `g1_speaker` | Sink (output) | 8-ohm 3W speaker in the G1's head |

These appear in Ubuntu Settings, `pavucontrol`, and any app's audio device selector.

## Architecture

The G1's audio hardware lives on PC1 (the head unit). This driver runs on PC2 and bridges the two over the internal network:

```
PC1 (192.168.123.161)                  PC2 (192.168.123.164)
+-----------------------+              +--------------------------------------+
|                       |  multicast   |                                      |
|  4-mic array ---------+-- UDP -----> |  g1_audio_driver --> /tmp/g1_mic.pipe |
|  239.168.123.161:5555 |              |        |                             |
|                       |              |    PulseAudio                        |
|  Speaker <------------+-- DDS -----  |  g1_audio_driver <-- /tmp/g1_spk.pipe |
|  PlayStream API 1003  |              |                                      |
+-----------------------+              +--------------------------------------+
```

**Microphone path:** PC1's voice service streams raw PCM from the 4-mic array via multicast UDP (`239.168.123.161:5555`) — but only while mic mode is active. The driver first enables mic mode (voice API 1008, `{"mode": 1}`), then joins the multicast group on the auto-detected robot-network interface (override with `G1_LOCAL_IP`), receives packets, and writes them into a FIFO pipe. PulseAudio reads the pipe as a standard audio source.

**Speaker path:** PulseAudio writes audio to a FIFO pipe. The driver reads 200ms chunks (6400 bytes — the same chunk size as the proven `play_wav_request` script) from the pipe and sends them to PC1 via the DDS `PlayStream` RPC (API 1003), checking each RPC return code. The head speaker plays them in real-time.

## Audio Format

Both directions use the same format:

| Parameter | Value |
|-----------|-------|
| Sample rate | 16,000 Hz |
| Channels | 1 (mono) |
| Bit depth | 16-bit signed little-endian |
| Throughput | 32 KB/s |
| PA format string | `s16le` |

Multicast mic packets: 5,120 bytes each (2,560 samples = 160ms)
PlayStream chunks: 6,400 bytes each (3,200 samples = 200ms)

## Prerequisites

- **Ubuntu 20.04** on PC2 (Jetson Orin NX)
- **PulseAudio** with `module-pipe-source` and `module-pipe-sink` (included by default)
- **Unitree SDK2 Python** (`unitree_sdk2py`) — required for both directions (mic-mode activation + speaker PlayStream). Install at `~/unitree_sdk2_python` or set `UNITREE_SDK_PATH`
- **Network**: `eth0` on `192.168.123.0/24` with DDS and multicast enabled
- **PC1 voice service** running (provides mic stream and PlayStream endpoint)

## Quick Start

```bash
# On PC2 (Jetson Orin)
git clone https://github.com/experientialtech/g1-audio-driver.git ~/g1-audio-driver
cd ~/g1-audio-driver
./install.sh --start

# Test the mic (record 5 seconds)
# NOTE: parecord has no duration option — use timeout(1). "-d" means --device!
timeout 5 parecord --device=g1_microphone /tmp/test.wav

# Test the speaker (play it back through G1 head)
paplay --device=g1_speaker /tmp/test.wav
```

## Installation

```bash
cd ~/g1-audio-driver
./install.sh           # install and enable (auto-starts on login)
./install.sh --start   # install, enable, and start now
./install.sh --remove  # uninstall completely
```

The installer:
1. Checks PulseAudio is running with pipe modules available
2. Checks `unitree_sdk2py` is importable (required for mic mode and speaker)
3. Copies the systemd service to `~/.config/systemd/user/`
4. Enables auto-start on login

## Usage

### Service management

```bash
systemctl --user start g1-audio-driver
systemctl --user stop g1-audio-driver
systemctl --user restart g1-audio-driver
systemctl --user status g1-audio-driver

# Live logs
journalctl --user -u g1-audio-driver -f
```

### Manual run (foreground)

```bash
python3 g1_audio_driver.py                 # both mic and speaker
python3 g1_audio_driver.py --no-speaker    # mic only
python3 g1_audio_driver.py --no-mic        # speaker only
python3 g1_audio_driver.py --silence-gate  # pause speaker stream during silence
python3 g1_audio_driver.py --verbose       # debug logging
```

### Set as default device

```bash
pactl set-default-source g1_microphone
pactl set-default-sink g1_speaker
```

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `G1_LOCAL_IP` | auto-detect | Local IP used to join the mic multicast group (auto-detected from the `192.168.123.*` interface) |
| `G1_DDS_INTERFACE` | `eth0` | Network interface for DDS |
| `UNITREE_SDK_PATH` | auto-detect | Path to `unitree_sdk2_python` (checked: `~/unitree_sdk2_python`, `~/unitree-sdk2-python`) |

## How It Works

### Mic Activation

The G1's mic array only streams while mic mode is active. The driver enables it by calling voice service API 1008 with `{"mode": 1}` at startup. When the driver stops, it sends `{"mode": 2}` to disable the mic.

| Mode | Behavior |
|------|----------|
| 1 | Mic streaming active, ASR active |
| 2 | Off (default) |

### FIFO Pipes

PulseAudio's `module-pipe-source` and `module-pipe-sink` use Unix FIFO files as the bridge:

- `/tmp/g1_mic.pipe` — driver writes, PA reads (microphone)
- `/tmp/g1_spk.pipe` — PA writes, driver reads (speaker)

### Silence Gating (Speaker)

To avoid wasting DDS bandwidth during silence, pass `--silence-gate` and the driver monitors audio RMS. After 300ms of continuous silence, it stops sending data. Audio resumes instantly when non-silent data arrives. **Off by default** — without the flag, all audio (including silence) is sent to the speaker, matching the behavior of the proven `play_wav_request` script.

### Error Recovery

| Failure | Behavior |
|---------|----------|
| PA suspends source/sink | Pipe closes, driver retries pipe open |
| PA unloads module | Pipe EOF, driver retries pipe open |
| Multicast timeout | Keeps waiting (PC1 may not be streaming yet) |
| Pipe buffer full (mic, source idle) | Drains stale FIFO contents so a new recording starts with fresh audio |
| Partial pipe writes (mic) | Remainder kept and completed — no lost/misaligned bytes |
| DDS PlayStream rejected (non-zero RPC code) | Logs code + first 3, suppresses, continues |
| Thread crash | Main loop restarts with exponential backoff |
| Thread crash > 50 times | Gives up on that thread |
| PulseAudio restart | systemd restarts the driver service |

### Thread Health

The main loop checks thread liveness every 3 seconds. Dead threads are restarted with exponential backoff (1s, 2s, 4s, ... up to 30s max). After 50 restarts, the thread is abandoned.

## Latency

| Path | Latency |
|------|---------|
| Mic to PulseAudio | ~160ms (one multicast packet) |
| PulseAudio to Speaker | ~200ms (one PlayStream chunk) + ~5-10ms DDS overhead |
| Round-trip (mic to speaker) | ~350-400ms |

## Troubleshooting

### Devices don't appear in Settings

```bash
# Check if driver is running
systemctl --user status g1-audio-driver

# Check PA modules loaded
pactl list modules short | grep pipe

# Check pipes exist
ls -la /tmp/g1_*.pipe

# Restart everything
systemctl --user restart g1-audio-driver
```

### No mic audio

```bash
# Check the driver auto-detected the right multicast interface IP
journalctl --user -u g1-audio-driver -n 20 | grep multicast

# Check multicast data flowing (auto-detects the interface like the driver)
timeout 3 python3 -c "
import socket
ip = '0.0.0.0'
try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as p:
        p.connect(('192.168.123.161', 1))
        cand = p.getsockname()[0]
        if cand.startswith('192.168.123.'): ip = cand
except OSError: pass
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('', 5555))
s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
    socket.inet_aton('239.168.123.161') + socket.inet_aton(ip))
s.settimeout(3)
d, a = s.recvfrom(65535)
print(f'OK: {len(d)} bytes from {a} via {ip}')
"
```

### No speaker audio

```bash
# Quick test
paplay --device=g1_speaker /usr/share/sounds/freedesktop/stereo/bell.oga 2>/dev/null \
  || echo "Test file not found, try: speaker-test -D pulse -t sine -f 440 -l 1"

# Check driver logs for PlayStream rejections (non-zero RPC codes)
journalctl --user -u g1-audio-driver --since "5 min ago" | grep -iE "error|rejected"
```

## Logs

```bash
journalctl --user -u g1-audio-driver -f        # live
journalctl --user -u g1-audio-driver -n 50     # last 50 lines
journalctl --user -u g1-audio-driver -b         # since boot
```

Normal operation logs throughput stats every 60 seconds:
```
[g1audio] 14:30:00 INFO Mic stats: 31.3 KB/s, 0 stale bytes dropped
[g1audio] 14:30:00 INFO Speaker stats: 28.7 KB/s
```

## Files

```
g1-audio-driver/
  g1_audio_driver.py       # Main driver (run directly or via systemd)
  g1-audio-driver.service  # systemd user service unit
  install.sh               # Install/uninstall helper
  README.md                # This file
```

## Web Interface

A built-in web UI for managing the driver service from a browser. No external dependencies.

```bash
python3 g1_audio_web.py              # default port 8085
python3 g1_audio_web.py --port 9000  # custom port
```

Then open `http://192.168.123.164:8085` (or whatever PC2's address is).

### Features

- **Status dashboard** — live service state (running/stopped), PID, uptime, PulseAudio device status (mic and speaker loaded/not loaded)
- **Controls** — Start, Stop, Restart, and Force Kill buttons
- **Log viewer** — last 100 journal lines, auto-refreshes every 5 seconds
- **Auto-refresh** — status polls every 3 seconds

### Force Kill

The "Force Kill" button does more than a normal stop:
1. Stops the systemd service
2. Sends `SIGKILL` to any lingering driver processes
3. Unloads PulseAudio pipe modules
4. Cleans up FIFO pipes in `/tmp/`

Use this when the driver is stuck and a normal stop/restart doesn't work.
