#!/usr/bin/env python3
"""
G1 Audio PulseAudio Driver
============================
Bridges the Unitree G1's onboard audio hardware (4-mic array + head speaker)
to standard PulseAudio virtual devices so any Linux application can use them.

Creates two virtual devices:
  - "G1 Onboard Microphone"  (PA source)  ← multicast UDP from PC1 mic array
  - "G1 Onboard Speaker"     (PA sink)    → DDS PlayStream to PC1 speaker

Architecture:
  ┌──────────┐  multicast UDP    ┌────────────┐  FIFO pipe   ┌────────────┐
  │ PC1 head │ ──────────────→  │ this driver │ ──────────→  │ PulseAudio │
  │ mic array│  239.168.123.161  │             │  /tmp/g1_mic │  source    │
  └──────────┘  :5555            │             │              └────────────┘
                                 │             │
  ┌──────────┐  DDS PlayStream   │             │  FIFO pipe   ┌────────────┐
  │ PC1 head │ ←──────────────  │             │ ←──────────  │ PulseAudio │
  │ speaker  │  RPC API 1003    │             │  /tmp/g1_spk │  sink      │
  └──────────┘                   └────────────┘              └────────────┘

Audio format (both directions): 16-bit signed LE, mono, 16 kHz (32 KB/s)

Requirements:
  - PulseAudio with module-pipe-source and module-pipe-sink
  - Unitree SDK2 Python (unitree_sdk2py) accessible on PYTHONPATH
    (needed by both directions: mic-mode activation and speaker PlayStream)
  - Network: DDS interface connected to 192.168.123.0/24 (set G1_DDS_INTERFACE if not eth0)
  - PC1 voice service running (provides mic multicast + PlayStream)

Usage:
  python3 g1_audio_driver.py                 # both mic and speaker
  python3 g1_audio_driver.py --no-mic        # speaker only
  python3 g1_audio_driver.py --no-speaker    # mic only
  python3 g1_audio_driver.py --silence-gate  # pause speaker stream during silence
  python3 g1_audio_driver.py --verbose       # extra debug logging

Environment:
  G1_LOCAL_IP        Local IP used to join the mic multicast group
                     (default: auto-detect the 192.168.123.* interface)
  G1_DDS_INTERFACE   Network interface for DDS (default: eth0)
  UNITREE_SDK_PATH   Path to unitree_sdk2_python (default: auto-detect)

Systemd:
  systemctl --user start g1-audio-driver
  systemctl --user stop g1-audio-driver
  journalctl --user -u g1-audio-driver -f
"""

import argparse
import array
import fcntl
import json
import logging
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Optional, Tuple

# ─── SDK path ─────────────────────────────────────────────────────────────────
def _default_sdk_path() -> str:
    """Pick the first SDK location that actually contains unitree_sdk2py."""
    for candidate in ("~/unitree_sdk2_python", "~/unitree-sdk2-python"):
        path = os.path.expanduser(candidate)
        if os.path.isdir(os.path.join(path, "unitree_sdk2py")):
            return path
    return os.path.expanduser("~/unitree_sdk2_python")


SDK_PATH = os.environ.get("UNITREE_SDK_PATH", _default_sdk_path())
if SDK_PATH not in sys.path:
    sys.path.insert(0, SDK_PATH)

# ─── Logging ──────────────────────────────────────────────────────────────────
LOG_FMT = "[g1audio] %(asctime)s %(levelname)s %(message)s"
LOG_DATEFMT = "%H:%M:%S"
logger = logging.getLogger("g1audio")


# ─── Audio constants ──────────────────────────────────────────────────────────
# Network
MULTICAST_GROUP = "239.168.123.161"     # PC1 streams mic audio here
MULTICAST_PORT = 5555                   # UDP port for multicast mic stream
DDS_INTERFACE = os.environ.get("G1_DDS_INTERFACE", "eth0")
# Local IP used to join the multicast group. Auto-detected at runtime by
# probing the route toward PC1 (192.168.123.161); override with G1_LOCAL_IP.
LOCAL_IP_OVERRIDE = os.environ.get("G1_LOCAL_IP")

# PCM format — same in both directions
SAMPLE_RATE = 16000                     # Hz
CHANNELS = 1                            # mono
SAMPLE_WIDTH = 2                        # bytes (16-bit signed LE)
BYTES_PER_SEC = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH  # 32000

# Multicast packets from PC1 are 5120 bytes each (2560 samples = 160ms)
MULTICAST_PACKET_SIZE = 5120

# PlayStream chunking — 6400 bytes (3200 samples = 200ms at 16 kHz).
# This matches the chunk size used by the proven play_wav_request script;
# smaller chunks (e.g. 50ms) are not reliably played by PC1.
PLAYSTREAM_CHUNK_BYTES = 6400           # 3200 samples = 200ms at 16 kHz
PLAYSTREAM_CHUNK_SECS = PLAYSTREAM_CHUNK_BYTES / BYTES_PER_SEC

# Silence detection for speaker (avoids sending silence to DDS endlessly).
# Disabled by default — only active with --silence-gate.
SILENCE_RMS_THRESHOLD = 100            # below this RMS, audio is considered silence
SILENCE_GATE_SECS = 0.3               # stop sending after this many seconds of silence

# Pipe buffer size — smaller = less latency on pause, but too small causes drops
# Default Linux pipe is 64KB (~2s at 32KB/s). We use 8KB (~250ms).
SPK_PIPE_BUF_SIZE = 8192

# PulseAudio
PA_SOURCE_NAME = "g1_microphone"
PA_SINK_NAME = "g1_speaker"
PA_SOURCE_DESC = "G1 Onboard Microphone"
PA_SINK_DESC = "G1 Onboard Speaker"
MIC_PIPE = "/tmp/g1_mic.pipe"
SPK_PIPE = "/tmp/g1_spk.pipe"

# Voice service DDS APIs
API_TTS = 1001
API_PLAY_STREAM = 1003
API_PLAY_STOP = 1004
API_GET_VOLUME = 1005
API_SET_VOLUME = 1006
API_GET_MODE = 1007
API_SET_MODE = 1008

# Thread health check
HEALTH_CHECK_INTERVAL = 3.0            # seconds between thread liveness checks
MAX_THREAD_RESTARTS = 50               # give up after this many restarts

# Stats reporting
STATS_INTERVAL = 60.0                  # seconds between throughput log lines


# ─── DDS layer ────────────────────────────────────────────────────────────────
class DDSAudio:
    """Thread-safe wrapper around the Unitree DDS audio clients."""

    def __init__(self):
        self._inited = False
        self._lock = threading.Lock()
        self._voice = None
        self._audio = None

    def init(self):
        """Initialize DDS transport and audio clients. Safe to call repeatedly."""
        with self._lock:
            if self._inited:
                return
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize
            from unitree_sdk2py.rpc.client import Client
            from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

            logger.info("Initializing DDS on %s...", DDS_INTERFACE)
            ChannelFactoryInitialize(0, DDS_INTERFACE)
            time.sleep(0.5)

            self._voice = Client("voice", False)
            self._voice.SetTimeout(5.0)
            self._voice._SetApiVerson("1.0.0.0")
            for api_id in range(1001, 1020):
                self._voice._RegistApi(api_id, 0)

            self._audio = AudioClient()
            self._audio.SetTimeout(10.0)
            self._audio.Init()

            self._inited = True
            logger.info("DDS initialized successfully")

    @property
    def ready(self) -> bool:
        return self._inited

    def set_mode(self, mode: int) -> int:
        """Set voice mode. 1=mic active, 2=off. Returns API code (0=success)."""
        self.init()
        code, _ = self._voice._Call(API_SET_MODE, json.dumps({"mode": mode}))
        return code

    def get_mode(self) -> Tuple[int, str]:
        """Get current voice mode. Returns (code, data_json)."""
        self.init()
        return self._voice._Call(API_GET_MODE, json.dumps({}))

    def play_stream(self, app_name: str, stream_id: str, pcm_data: bytes) -> int:
        """Send a PCM chunk to the G1 speaker. Returns the RPC code (0=success)."""
        self.init()
        try:
            code, _ = self._audio.PlayStream(app_name, stream_id, pcm_data)
        except TypeError:
            # Some SDK versions need list instead of bytes
            code, _ = self._audio.PlayStream(app_name, stream_id, list(pcm_data))
        return code

    def play_stop(self, app_name: str):
        """Stop an active PlayStream."""
        self.init()
        try:
            self._audio.PlayStop(app_name)
        except Exception as e:
            logger.debug("PlayStop error (usually harmless): %s", e)


dds = DDSAudio()


# ─── PulseAudio helpers ──────────────────────────────────────────────────────
def pa_run(cmd: list) -> Tuple[bool, str]:
    """Run a pactl/pacmd command. Returns (success, stdout)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return r.returncode == 0, r.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.warning("PA command timed out: %s", " ".join(cmd))
        return False, "timeout"
    except FileNotFoundError:
        logger.error("Command not found: %s", cmd[0])
        return False, "not found"
    except Exception as e:
        return False, str(e)


def pa_unload_by_pipe(pipe_path: str):
    """Unload any PA module that references the given pipe path."""
    ok, out = pa_run(["pactl", "list", "modules", "short"])
    if not ok:
        return
    for line in out.splitlines():
        if pipe_path in line:
            idx = line.split()[0]
            success, _ = pa_run(["pactl", "unload-module", idx])
            if success:
                logger.info("Unloaded stale PA module %s (referenced %s)", idx, pipe_path)
            else:
                logger.warning("Failed to unload PA module %s", idx)


def pa_set_description(kind: str, name: str, desc: str):
    """Set the human-readable description for a PA source or sink via pacmd.

    PA's module-load doesn't support spaces in property values, so we set
    descriptions after loading via pacmd update-*-proplist.
    """
    cmd = "update-source-proplist" if kind == "source" else "update-sink-proplist"
    ok, _ = pa_run(["pacmd", cmd, name, f'device.description="{desc}"'])
    if not ok:
        logger.warning("Could not set %s description for %s", kind, name)


def cleanup_pipe(path: str):
    """Remove an existing pipe/file so PA can create a fresh FIFO."""
    if os.path.exists(path):
        try:
            os.remove(path)
            logger.debug("Removed stale pipe: %s", path)
        except OSError as e:
            logger.warning("Could not remove %s: %s", path, e)


def setup_pa_source() -> bool:
    """Load module-pipe-source for the G1 virtual microphone.

    Returns True on success. Cleans up any stale modules/pipes first.
    """
    pa_unload_by_pipe(MIC_PIPE)
    cleanup_pipe(MIC_PIPE)

    ok, out = pa_run([
        "pactl", "load-module", "module-pipe-source",
        f"source_name={PA_SOURCE_NAME}",
        f"file={MIC_PIPE}",
        "format=s16le",
        f"rate={SAMPLE_RATE}",
        f"channels={CHANNELS}",
    ])
    if not ok:
        logger.error("Failed to load PA source module: %s", out)
        return False

    logger.info("PA source loaded: %s (module %s)", PA_SOURCE_NAME, out)
    pa_set_description("source", PA_SOURCE_NAME, PA_SOURCE_DESC)
    return True


def setup_pa_sink() -> bool:
    """Load module-pipe-sink for the G1 virtual speaker.

    Returns True on success. Cleans up any stale modules/pipes first.
    """
    pa_unload_by_pipe(SPK_PIPE)
    cleanup_pipe(SPK_PIPE)

    ok, out = pa_run([
        "pactl", "load-module", "module-pipe-sink",
        f"sink_name={PA_SINK_NAME}",
        f"file={SPK_PIPE}",
        "format=s16le",
        f"rate={SAMPLE_RATE}",
        f"channels={CHANNELS}",
    ])
    if not ok:
        logger.error("Failed to load PA sink module: %s", out)
        return False

    logger.info("PA sink loaded: %s (module %s)", PA_SINK_NAME, out)
    pa_set_description("sink", PA_SINK_NAME, PA_SINK_DESC)
    # Also rename the auto-created monitor source so it doesn't show as "Unix FIFO"
    pa_set_description("source", f"{PA_SINK_NAME}.monitor", f"{PA_SINK_DESC} Monitor")
    return True


def teardown_pa():
    """Unload our PA modules and clean up pipes."""
    pa_unload_by_pipe(MIC_PIPE)
    pa_unload_by_pipe(SPK_PIPE)
    cleanup_pipe(MIC_PIPE)
    cleanup_pipe(SPK_PIPE)
    logger.info("PA modules unloaded and pipes cleaned up")


# ─── Mic thread: multicast UDP → FIFO → PulseAudio source ────────────────────
def mic_thread(shutdown_event: threading.Event):
    """Capture multicast mic audio and feed it into the PulseAudio source pipe.

    Lifecycle:
      1. Enable mic mode on PC1 (voice API 1008 mode=1) — required: PC1 only
         streams the mic array while this mode is active
      2. Auto-detect the local IP on the robot network (or use G1_LOCAL_IP)
      3. Join multicast group 239.168.123.161:5555 on that interface
      4. Open the FIFO pipe for writing (non-blocking)
      5. Loop: recv UDP packet → write to pipe
      6. On shutdown: close pipe, leave multicast, disable mic mode

    Error handling:
      - BrokenPipeError: PA suspended the source → close and retry pipe
      - BlockingIOError: PA pipe buffer full → drop packet (not harmful)
      - socket.timeout: no data from PC1 → keep waiting
      - Any other error → log, close resources, retry after backoff
    """
    logger.info("Mic thread starting")

    # Activate mic streaming on PC1 — without this, no multicast data flows
    code = dds.set_mode(1)
    if code != 0:
        logger.warning("Could not enable mic mode (code=%d) — mic may not stream", code)
    else:
        logger.info("Mic mode enabled (mode=1)")

    # Resolve the interface IP for the multicast join
    if LOCAL_IP_OVERRIDE:
        local_ip = LOCAL_IP_OVERRIDE
        logger.info("Using G1_LOCAL_IP=%s for multicast join", local_ip)
    else:
        local_ip = get_local_ip_for_multicast()
        if local_ip:
            logger.info("Auto-detected multicast interface IP: %s", local_ip)
        else:
            local_ip = "0.0.0.0"
            logger.warning("No 192.168.123.* interface found — OS will pick the "
                           "multicast interface. Set G1_LOCAL_IP if no mic audio.")

    time.sleep(1)  # give PC1 time to start streaming

    sock = None
    pipe_fd = None
    bytes_written = 0
    packets_dropped = 0
    last_stats = time.time()

    try:
        # Create and configure multicast socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        # Increase receive buffer to reduce drops during pipe-reopen gaps
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
        sock.bind(("", MULTICAST_PORT))
        mreq = socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton(local_ip)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.settimeout(2.0)
        logger.info("Joined multicast %s:%d via %s", MULTICAST_GROUP, MULTICAST_PORT, local_ip)

        while not shutdown_event.is_set():
            # (Re)open pipe if needed — non-blocking so we don't hang if PA isn't reading
            if pipe_fd is None:
                try:
                    pipe_fd = os.open(MIC_PIPE, os.O_WRONLY | os.O_NONBLOCK)
                    logger.info("Mic pipe opened for writing")
                except OSError:
                    # PA hasn't opened read end yet (source suspended) — keep trying
                    time.sleep(0.5)
                    # Still drain UDP so the socket buffer doesn't overflow
                    try:
                        sock.recvfrom(65535)
                    except socket.timeout:
                        pass
                    continue

            # Receive a multicast packet
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError as e:
                logger.warning("Multicast recv error: %s", e)
                time.sleep(0.1)
                continue

            # Write to PA pipe
            try:
                os.write(pipe_fd, data)
                bytes_written += len(data)
            except BrokenPipeError:
                # PA closed the read end (source suspended/removed)
                logger.debug("Mic pipe broken — PA suspended source, will retry")
                _close_fd(pipe_fd)
                pipe_fd = None
                time.sleep(0.2)
                continue
            except BlockingIOError:
                # Pipe buffer full — drop this packet. Not harmful; PA will catch up.
                packets_dropped += 1
                continue
            except OSError as e:
                logger.warning("Mic pipe write error: %s", e)
                _close_fd(pipe_fd)
                pipe_fd = None
                time.sleep(0.2)
                continue

            # Periodic stats
            now = time.time()
            if now - last_stats >= STATS_INTERVAL:
                elapsed = now - last_stats
                kbps = bytes_written / 1024 / elapsed if elapsed > 0 else 0
                logger.info("Mic stats: %.1f KB/s, %d packets dropped", kbps, packets_dropped)
                bytes_written = 0
                packets_dropped = 0
                last_stats = now

    except Exception as e:
        logger.error("Mic thread unexpected error: %s", e, exc_info=True)
    finally:
        _close_fd(pipe_fd)
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        # Disable mic mode to save power
        try:
            dds.set_mode(2)
            logger.info("Mic mode disabled (mode=2)")
        except Exception:
            pass
        logger.info("Mic thread stopped")


# ─── Speaker thread: PulseAudio sink → FIFO → DDS PlayStream ─────────────────
def speaker_thread(shutdown_event: threading.Event, silence_gate: bool = False):
    """Read PCM from the PulseAudio sink pipe and send to the G1 speaker.

    Lifecycle:
      1. Open the FIFO pipe for reading (blocks until PA writes)
      2. Read PLAYSTREAM_CHUNK_BYTES (200ms) at a time
      3. Send each chunk via DDS PlayStream, checking the RPC return code
      4. Pace sends at real-time rate (200ms per 6400-byte chunk)
      5. When pipe EOF (PA removed sink), close and retry

    Chunking matches the proven play_wav_request script: 6400-byte chunks
    (200ms at 16 kHz) with wall-clock pacing.

    Silence gating (opt-in via --silence-gate):
      By default every chunk is sent, exactly like play_wav_request. With
      --silence-gate, chunks below the RMS threshold stop being sent after
      SILENCE_GATE_SECS of continuous silence, and PlayStop is issued.

    Error handling:
      - EOF on pipe: PA suspended/removed sink → close, PlayStop, retry
      - PlayStream non-zero RPC code or exception: logged, counted, continues
      - Any other error: log, clean up, retry with backoff
    """
    logger.info("Speaker thread starting")
    dds.init()

    APP_NAME = "g1drv"
    stream_id = str(int(time.time() * 1000))
    bytes_read = 0
    last_stats = time.time()
    play_errors = 0

    while not shutdown_event.is_set():
        pipe_fd = None
        idle_since = None
        playing = False

        try:
            # Open pipe — non-blocking so we can check shutdown_event
            while not shutdown_event.is_set():
                try:
                    pipe_fd = os.open(SPK_PIPE, os.O_RDONLY | os.O_NONBLOCK)
                    # Shrink pipe buffer to reduce latency on pause/stop
                    try:
                        fcntl.fcntl(pipe_fd, 1031, SPK_PIPE_BUF_SIZE)  # F_SETPIPE_SZ
                        actual = fcntl.fcntl(pipe_fd, 1032)  # F_GETPIPE_SZ
                        logger.info("Speaker pipe opened, buffer=%d bytes", actual)
                    except OSError:
                        logger.info("Speaker pipe opened for reading")
                    break
                except OSError:
                    time.sleep(0.5)

            if shutdown_event.is_set():
                break

            # Wall-clock pacer: track when we "should" send the next chunk
            next_send = time.monotonic()

            # Accumulate PCM into full PLAYSTREAM_CHUNK_BYTES chunks.
            # Partial reads are kept across select() rounds — never discarded,
            # because PulseAudio trickles data at real-time rate (~32 KB/s).
            buf = bytearray()

            while not shutdown_event.is_set():
                # Use select() to wait for data with a timeout (for shutdown checks)
                try:
                    ready, _, _ = select.select([pipe_fd], [], [], 0.25)
                except (ValueError, OSError):
                    break
                if not ready:
                    continue

                # Read whatever is available (up to the rest of the chunk)
                try:
                    chunk = os.read(pipe_fd, PLAYSTREAM_CHUNK_BYTES - len(buf))
                    if not chunk:
                        break  # EOF: PA suspended/removed the sink
                    buf.extend(chunk)
                except BlockingIOError:
                    continue

                if len(buf) < PLAYSTREAM_CHUNK_BYTES:
                    continue  # keep accumulating across rounds

                data = bytes(buf)
                buf.clear()

                if silence_gate:
                    # Fast silence detection using array (avoids struct.unpack + Python sum)
                    samples = array.array('h', data)
                    # Check a subset for speed (every 4th sample)
                    energy = sum(s * s for s in samples[::4])
                    is_silence = energy < SILENCE_RMS_THRESHOLD * (len(samples) // 4)

                    if is_silence:
                        if idle_since is None:
                            idle_since = time.time()
                        if time.time() - idle_since > SILENCE_GATE_SECS:
                            if playing:
                                dds.play_stop(APP_NAME)
                                playing = False
                                logger.debug("Speaker idle — silence gate active")
                            # Reset pacer so we don't burst when audio resumes
                            next_send = time.monotonic()
                            continue
                    else:
                        idle_since = None

                # Wall-clock pacing: sleep only the remaining time until next_send
                now = time.monotonic()
                if next_send > now:
                    time.sleep(next_send - now)
                next_send = max(time.monotonic(), next_send + PLAYSTREAM_CHUNK_SECS)

                # Send to G1 speaker and check the RPC return code
                try:
                    code = dds.play_stream(APP_NAME, stream_id, data)
                    if code != 0:
                        play_errors += 1
                        if play_errors <= 3:
                            logger.warning("PlayStream rejected (code=%d, error %d)",
                                           code, play_errors)
                        elif play_errors == 4:
                            logger.warning("PlayStream rejections continuing — "
                                           "suppressing further logs")
                    else:
                        playing = True
                        play_errors = 0
                except Exception as e:
                    play_errors += 1
                    if play_errors <= 3:
                        logger.warning("PlayStream error (%d): %s", play_errors, e)
                    elif play_errors == 4:
                        logger.warning("PlayStream errors continuing — suppressing further logs")

                bytes_read += len(data)

                # Periodic stats
                now_wall = time.time()
                if now_wall - last_stats >= STATS_INTERVAL:
                    elapsed = now_wall - last_stats
                    kbps = bytes_read / 1024 / elapsed if elapsed > 0 else 0
                    logger.info("Speaker stats: %.1f KB/s", kbps)
                    bytes_read = 0
                    last_stats = now_wall

        except Exception as e:
            logger.error("Speaker thread unexpected error: %s", e, exc_info=True)
        finally:
            _close_fd(pipe_fd)
            if playing:
                dds.play_stop(APP_NAME)
                playing = False

        # New stream ID for next pipe open
        stream_id = str(int(time.time() * 1000))
        if not shutdown_event.is_set():
            time.sleep(0.5)

    logger.info("Speaker thread stopped")


# ─── Utilities ────────────────────────────────────────────────────────────────
def get_local_ip_for_multicast() -> Optional[str]:
    """Auto-detect the local IP on the 192.168.123.0/24 (robot) network.

    Opens a UDP socket "toward" PC1 so the OS picks the route, then reads the
    local endpoint. No packets are actually sent. Same technique as the
    proven record_wav script. Returns None if no such interface exists.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.123.161", 1))
            ip = s.getsockname()[0]
    except OSError:
        return None
    if ip.startswith("192.168.123."):
        return ip
    return None


def _close_fd(fd: Optional[int]):
    """Safely close a file descriptor."""
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def _read_exact(fd: int, nbytes: int, shutdown_event: threading.Event) -> Optional[bytes]:
    """Read exactly nbytes from fd, handling partial reads.

    Returns None on EOF or if shutdown_event is set.
    Uses non-blocking reads with short sleeps to stay responsive to shutdown.
    """
    buf = bytearray()
    while len(buf) < nbytes:
        if shutdown_event.is_set():
            return None
        try:
            chunk = os.read(fd, nbytes - len(buf))
            if not chunk:
                # EOF
                return None
            buf.extend(chunk)
        except BlockingIOError:
            # No data available yet — brief sleep to avoid busy-waiting
            time.sleep(0.005)
        except OSError:
            return None
    return bytes(buf)


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="G1 Audio PulseAudio Driver — virtual mic and speaker for the Unitree G1",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                  Start both mic and speaker
  %(prog)s --no-speaker     Mic only (e.g. for recording)
  %(prog)s --no-mic         Speaker only (e.g. for playback)
  %(prog)s --silence-gate   Pause speaker stream during silence
  %(prog)s --verbose        Extra debug logging

Systemd service:
  systemctl --user start g1-audio-driver
  systemctl --user stop g1-audio-driver
  journalctl --user -u g1-audio-driver -f
""")
    parser.add_argument("--no-mic", action="store_true",
                        help="Disable virtual microphone (no G1 mic → PA source)")
    parser.add_argument("--no-speaker", action="store_true",
                        help="Disable virtual speaker (no PA sink → G1 speaker)")
    parser.add_argument("--silence-gate", action="store_true",
                        help="Stop sending audio to the speaker during silence "
                             "(off by default — all audio is sent)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug-level logging")
    args = parser.parse_args()

    # Configure logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(format=LOG_FMT, datefmt=LOG_DATEFMT, level=level, stream=sys.stdout)

    # Shutdown coordination
    shutdown_event = threading.Event()

    def handle_signal(signum, frame):
        signame = signal.Signals(signum).name
        logger.info("Received %s — shutting down", signame)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    logger.info("=" * 56)
    logger.info("G1 Audio PulseAudio Driver")
    logger.info("  Microphone: %s", "disabled" if args.no_mic else "enabled")
    logger.info("  Speaker:    %s", "disabled" if args.no_speaker else "enabled")
    logger.info("  SDK:        %s", SDK_PATH)
    logger.info("  PID:        %d", os.getpid())
    logger.info("=" * 56)

    # Initialize DDS (needed by both threads: mic-mode RPC + PlayStream)
    try:
        dds.init()
    except Exception as e:
        logger.error("Failed to initialize DDS: %s", e)
        logger.error("Is %s up? Is unitree_sdk2py installed?", DDS_INTERFACE)
        sys.exit(1)

    threads = []
    restart_counts = {}
    # Extra keyword args passed to each worker thread (used on start and restart)
    thread_params = {
        "mic": {},
        "speaker": {"silence_gate": args.silence_gate},
    }

    # Set up virtual microphone
    if not args.no_mic:
        if setup_pa_source():
            t = threading.Thread(target=mic_thread, args=(shutdown_event,),
                                 kwargs=thread_params["mic"],
                                 name="mic", daemon=True)
            t.start()
            threads.append(t)
            restart_counts["mic"] = 0
            logger.info("Microphone: active (%s)", PA_SOURCE_NAME)
        else:
            logger.error("Could not set up PA source — microphone disabled")
            logger.error("Is PulseAudio running? (pactl info)")

    # Set up virtual speaker
    if not args.no_speaker:
        if setup_pa_sink():
            t = threading.Thread(target=speaker_thread, args=(shutdown_event,),
                                 kwargs=thread_params["speaker"],
                                 name="speaker", daemon=True)
            t.start()
            threads.append(t)
            restart_counts["speaker"] = 0
            logger.info("Speaker: active (%s)", PA_SINK_NAME)
        else:
            logger.error("Could not set up PA sink — speaker disabled")
            logger.error("Is PulseAudio running? (pactl info)")

    if not threads:
        logger.error("No devices could be set up — exiting")
        sys.exit(1)

    logger.info("Driver running — devices are now visible in Ubuntu Settings")

    # Main loop: monitor thread health and restart if needed
    try:
        while not shutdown_event.is_set():
            shutdown_event.wait(HEALTH_CHECK_INTERVAL)
            if shutdown_event.is_set():
                break

            for i, t in enumerate(threads):
                if t.is_alive():
                    continue

                name = t.name
                restart_counts[name] = restart_counts.get(name, 0) + 1
                count = restart_counts[name]

                if count > MAX_THREAD_RESTARTS:
                    logger.error("%s thread died %d times — giving up", name, count)
                    continue

                logger.warning("%s thread died (restart %d/%d) — restarting",
                               name, count, MAX_THREAD_RESTARTS)

                # Backoff: 1s, 2s, 4s, ... up to 30s
                backoff = min(30, 2 ** (count - 1))
                time.sleep(backoff)

                if name == "mic":
                    target = mic_thread
                elif name == "speaker":
                    target = speaker_thread
                else:
                    continue

                new_t = threading.Thread(target=target, args=(shutdown_event,),
                                         kwargs=thread_params[name],
                                         name=name, daemon=True)
                new_t.start()
                threads[i] = new_t

    except KeyboardInterrupt:
        logger.info("Interrupted")
        shutdown_event.set()

    # Clean shutdown
    logger.info("Waiting for threads to finish...")
    shutdown_event.set()
    for t in threads:
        t.join(timeout=5)
        if t.is_alive():
            logger.warning("%s thread did not stop cleanly", t.name)

    teardown_pa()
    logger.info("Goodbye")


if __name__ == "__main__":
    main()
