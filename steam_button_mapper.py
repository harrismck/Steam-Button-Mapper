#!/usr/bin/env python3
"""
steam_button_mapper.py

Maps keyboard keys to SteamOS gamepad/keyboard signals. e.g. a "Steam
button" that opens the Steam menu from anywhere (including in-game), and
QAM/Menu shortcuts that work while Steam's own UI has focus. Config lives
in config.yaml (see config.example.yaml for a documented starting point).

A lightweight Supervisor watches for the configured keyboard connecting or
disconnecting (polling, not udev -- simpler, no extra dependency, and a
few seconds of latency doesn't matter for this use case) and starts/stops
the actual Mapper accordingly, so plugging the keyboard in later, or
switching between Bluetooth and the 2.4GHz dongle, just works without a
restart.

Mechanism notes:
- Steam button = BTN_MODE (Guide) on a recognized gamepad-class device.
  Works via a virtual "Xbox 360 pad" identity (045e:028e), everywhere,
  including in-game.
- QAM ("...") and Menu are Steam's own UI-level keyboard shortcuts
  (Ctrl+2 / Ctrl+0 by default here). These work while Steam's own
  window/overlay has keyboard focus, but do NOT work once a game has taken
  over input focus. Known Valve limitation :(
- Desktop notifications (notify-send) are reliable in Desktop Mode but may
  not render at all in Gaming Mode/gamescope, which typically doesn't run
  a full desktop notification daemon. The audio indicator (below) is the
  more dependable signal while actually in Gaming Mode.

Requires: python-evdev, PyYAML  (pip install --user evdev pyyaml, or via
distrobox if pip/build tools aren't available on the host -- see README)

Run as your normal user. Needs read/write access to /dev/input/eventX for
your keyboard and to /dev/uinput. SteamOS/Steam Input already relies on
uinput for its own virtual controllers, so the permissions are usually
already in place.
"""

import asyncio
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import yaml
from evdev import InputDevice, UInput, ecodes, list_devices


# ---------------- fire targets ----------------

class PadChord:
    """One or more gamepad button codes, pressed together then released, via the virtual gamepad."""
    def __init__(self, *codes):
        self.codes = codes


class KeyCombo:
    """One or more keyboard key codes, pressed together then released, via the virtual keyboard."""
    def __init__(self, *codes):
        self.codes = codes


# ---------------- config loading ----------------

def resolve_code(name):
    code = ecodes.ecodes.get(name)
    if code is None:
        raise ValueError(
            f"Unknown ecode name: {name!r}. See config.yaml comments for how "
            f"to list valid names."
        )
    if isinstance(code, list):  # a few names alias to the same int
        code = code[0]
    return code


def parse_bindings(raw_bindings):
    bindings = {}
    for key_name, spec in (raw_bindings or {}).items():
        trigger = resolve_code(key_name)
        codes = [resolve_code(c) for c in spec["codes"]]
        gaming_only = spec.get("gaming_only", True)
        kind = spec.get("type", "keys")
        if kind == "pad":
            target = PadChord(*codes)
        elif kind == "keys":
            target = KeyCombo(*codes)
        else:
            raise ValueError(f"Unknown binding type {kind!r} for {key_name}")
        bindings[trigger] = (target, gaming_only)
    return bindings


class Config:
    def __init__(self, raw, path):
        self.path = path
        self.device_name_match = raw.get("device_name_match", "Node 75")
        self.device_poll_seconds = float(raw.get("device_poll_seconds", 3.0))
        self.toggle_modifier = resolve_code(raw.get("toggle_modifier", "KEY_LEFTALT"))
        self.toggle_key = resolve_code(raw.get("toggle_key", "KEY_LEFTMETA"))
        self.manual_override_default = bool(raw.get("manual_override_default", False))
        self.gaming_mode_cache_ttl = float(raw.get("gaming_mode_cache_ttl", 1.0))
        self.chord_gap_seconds = float(raw.get("chord_gap_seconds", 0.05))

        notif = raw.get("notifications") or {}
        self.notify_enabled = bool(notif.get("enabled", True))
        self.notify_title = notif.get("title", "Steam Button Mapper")

        sound = raw.get("sound") or {}
        self.sound_enabled = bool(sound.get("enabled", True))
        self.sound_on_hz = float(sound.get("on_hz", 880))
        self.sound_off_hz = float(sound.get("off_hz", 440))
        self.sound_duration = float(sound.get("duration_seconds", 0.15))

        # A plain bash command, run via `bash -c` with STATE=on/off in its
        # environment, any time the effective remap state changes. This is
        # for anything shell-scriptable (LEDs, external tools) -- it can
        # NOT trigger firmware-level keyboard actions (see module docstring).
        self.on_state_change_hook = raw.get("on_state_change_hook") or None

        self.bindings = parse_bindings(raw.get("bindings", {}))


def resolve_config_path():
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    env_path = os.environ.get("STEAM_BUTTON_MAPPER_CONFIG")
    if env_path:
        return Path(env_path)
    default = Path.home() / ".config" / "steam-button-mapper" / "config.yaml"
    if default.exists():
        return default
    local = Path(__file__).with_name("config.yaml")
    if local.exists():
        return local
    raise FileNotFoundError(
        "No config.yaml found. Copy config.example.yaml to "
        f"{default} and edit it, set STEAM_BUTTON_MAPPER_CONFIG, or pass a "
        "path as the first argument."
    )


def load_config():
    path = resolve_config_path()
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    return Config(raw, path)


# ---------------- device discovery ----------------

def find_devices(name_match, required=True):
    matches = []
    for path in list_devices():
        try:
            dev = InputDevice(path)
        except OSError:
            continue  # disappeared mid-scan, e.g. mid-unplug
        if name_match.lower() in dev.name.lower():
            if dev.capabilities().get(ecodes.EV_KEY):
                matches.append(dev)
    if not matches and required:
        raise RuntimeError(
            f"No input device matching '{name_match}' with key events found. "
            f"Run `python3 -c \"import evdev; print([evdev.InputDevice(p).name for p in evdev.list_devices()])\"` "
            f"to see connected device names and adjust device_name_match in your config."
        )
    return matches


def build_combined_caps(devices):
    caps = {ecodes.EV_KEY: set(), ecodes.EV_REL: set(), ecodes.EV_MSC: set()}
    for d in devices:
        dcaps = d.capabilities()
        for etype in caps:
            caps[etype].update(dcaps.get(etype, []))
    return {k: sorted(v) for k, v in caps.items() if v}


# ---------------- audio indicator ----------------

def _write_tone_wav(path, frequency_hz, duration_s, volume=0.3, sample_rate=44100):
    n_samples = max(1, int(duration_s * sample_rate))
    fade_samples = max(1, int(sample_rate * 0.01))  # 10ms fade in/out, avoids clicks
    with wave.open(str(path), "w") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # 16-bit
        wav.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_samples):
            t = i / sample_rate
            fade = min(1.0, i / fade_samples, (n_samples - i) / fade_samples)
            sample = volume * fade * math.sin(2 * math.pi * frequency_hz * t)
            frames += struct.pack("<h", int(sample * 32767))
        wav.writeframesraw(bytes(frames))


def _find_audio_player():
    for player in ("paplay", "aplay", "afplay"):
        path = shutil.which(player)
        if path:
            return path
    return None


# ---------------- mapper (bound to one live set of devices) ----------------

class Mapper:
    def __init__(self, cfg, devices, audio_player):
        self.cfg = cfg
        self.devices = devices
        self.held = set()
        self.manual_override = cfg.manual_override_default
        self._gaming_mode_cache = {"value": False, "checked_at": 0.0}
        self.last_known_state = None
        self._tone_paths = {}
        self._audio_player = audio_player

        for d in devices:
            d.grab()

        self.virtual_kb = UInput(build_combined_caps(devices), name="steam-button-mapper-kbd")

        pad_caps = {
            ecodes.EV_KEY: [
                ecodes.BTN_MODE, ecodes.BTN_START, ecodes.BTN_SELECT,
                ecodes.BTN_SOUTH, ecodes.BTN_EAST, ecodes.BTN_WEST, ecodes.BTN_NORTH,
                ecodes.BTN_TL, ecodes.BTN_TR,
            ]
        }
        self.virtual_pad = UInput(
            pad_caps, name="Steam Button Emulator",
            vendor=0x045E, product=0x028E, version=0x0110, bustype=ecodes.BUS_USB,
        )

    # ---- gaming mode detection ----

    def _scan_gamescope(self):
        try:
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                try:
                    with open(f"/proc/{pid}/comm") as f:
                        if f.read().strip() == "gamescope":
                            return True
                except OSError:
                    continue
        except OSError:
            pass
        return False

    def is_gaming_mode(self):
        now = time.monotonic()
        cache = self._gaming_mode_cache
        if now - cache["checked_at"] < self.cfg.gaming_mode_cache_ttl:
            return cache["value"]
        result = self._scan_gamescope()
        cache["value"] = result
        cache["checked_at"] = now
        return result

    def effective_state(self):
        return self.is_gaming_mode() or self.manual_override

    # ---- device liveness (for the Supervisor) ----

    def devices_alive(self):
        for d in self.devices:
            try:
                d.capabilities()
            except OSError:
                return False
        return True

    # ---- side effects ----

    def notify(self, message):
        if not self.cfg.notify_enabled:
            return
        try:
            subprocess.run(["notify-send", self.cfg.notify_title, message], check=False, timeout=1)
        except FileNotFoundError:
            pass

    def play_indicator_sound(self, state_on):
        if not self.cfg.sound_enabled or not self._audio_player:
            return
        freq = self.cfg.sound_on_hz if state_on else self.cfg.sound_off_hz
        path = self._tone_paths.get(freq)
        if path is None:
            path = Path(tempfile.gettempdir()) / f"steam-button-mapper-tone-{int(freq)}.wav"
            if not path.exists():
                try:
                    _write_tone_wav(path, freq, self.cfg.sound_duration)
                except Exception as e:
                    print(f"Could not generate indicator tone: {e}")
                    return
            self._tone_paths[freq] = path
        try:
            subprocess.Popen(
                [self._audio_player, str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            print(f"Could not play indicator tone: {e}")

    def run_hook(self, state_on):
        if not self.cfg.on_state_change_hook:
            return
        env = {**os.environ, "STATE": "on" if state_on else "off"}
        try:
            subprocess.Popen(["bash", "-c", self.cfg.on_state_change_hook], env=env)
        except Exception as e:
            print(f"on_state_change_hook failed to launch: {e}")

    def apply_state_change(self, new_state):
        verb = "on" if new_state else "off"
        print(f"Remap state: {verb}")
        self.notify(f"Remap state: {verb}")
        self.play_indicator_sound(new_state)
        self.run_hook(new_state)

    # ---- firing ----

    def fire(self, target):
        if isinstance(target, PadChord):
            device = self.virtual_pad
        elif isinstance(target, KeyCombo):
            device = self.virtual_kb
        else:
            raise TypeError(f"Unknown fire target: {target!r}")

        gap = self.cfg.chord_gap_seconds
        for code in target.codes:
            device.write(ecodes.EV_KEY, code, 1)
            device.syn()
            time.sleep(gap)
        for code in reversed(target.codes):
            device.write(ecodes.EV_KEY, code, 0)
            device.syn()
            time.sleep(gap)

    # ---- event handling ----

    def handle_event(self, event):
        if event.type == ecodes.EV_KEY:
            if event.value == 1:
                self.held.add(event.code)
            elif event.value == 0:
                self.held.discard(event.code)

        # toggle_modifier + toggle_key: flip manual override, don't also
        # fire any binding on this particular press.
        if (
            event.type == ecodes.EV_KEY
            and event.code == self.cfg.toggle_key
            and event.value == 1
            and self.cfg.toggle_modifier in self.held
        ):
            before = self.effective_state()
            self.manual_override = not self.manual_override
            after = self.effective_state()
            if after != before:
                self.apply_state_change(after)
            self.last_known_state = after
            return

        if event.type != ecodes.EV_KEY or event.code not in self.cfg.bindings:
            self.virtual_kb.write_event(event)
            return

        target, gaming_only = self.cfg.bindings[event.code]
        if gaming_only and not self.effective_state():
            self.virtual_kb.write_event(event)  # Desktop Mode, no override: behave normally
            return

        if event.value == 1:  # key-down only -- ignore repeat (2) and up (0)
            self.fire(target)
        # swallow key-down/up/repeat here otherwise

    async def run_device(self, device):
        async for event in device.async_read_loop():
            self.handle_event(event)

    async def watch_state(self):
        # Fast-poll for the first few seconds so we converge quickly on the
        # real state right after startup (e.g. gamescope still launching at
        # boot), rather than waiting up to gaming_mode_cache_ttl. The very
        # first iteration always announces whatever state it finds, since
        # last_known_state starts as None.
        fast_poll_until = time.monotonic() + 5.0
        fast_poll_interval = 0.25

        while True:
            gamescope_running = self._scan_gamescope()
            self._gaming_mode_cache["value"] = gamescope_running
            self._gaming_mode_cache["checked_at"] = time.monotonic()

            current = gamescope_running or self.manual_override
            if current != self.last_known_state:
                self.apply_state_change(current)
                self.last_known_state = current

            interval = (
                fast_poll_interval
                if time.monotonic() < fast_poll_until
                else self.cfg.gaming_mode_cache_ttl
            )
            await asyncio.sleep(interval)

    async def run(self):
        tasks = [self.run_device(d) for d in self.devices]
        tasks.append(self.watch_state())
        await asyncio.gather(*tasks)

    def close(self):
        for d in self.devices:
            try:
                d.ungrab()
            except OSError:
                pass


# ---------------- supervisor (watches for the keyboard connecting/disconnecting) ----------------

class Supervisor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.audio_player = _find_audio_player()
        self.mapper = None
        self.mapper_task = None

    def _mapper_alive(self):
        if self.mapper is None or self.mapper_task is None:
            return False
        if self.mapper_task.done():
            return False
        return self.mapper.devices_alive()

    async def _start_mapper(self, devices):
        print("Device found, starting mapper:")
        for d in devices:
            print(f"  {d.name} ({d.path})")
        self.mapper = Mapper(self.cfg, devices, self.audio_player)
        self.mapper_task = asyncio.create_task(self.mapper.run())

    async def _stop_mapper(self, reason):
        if self.mapper is None:
            return
        print(f"Stopping mapper: {reason}")
        self.mapper_task.cancel()
        try:
            await self.mapper_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            self.mapper.close()
        except Exception:
            pass
        self.mapper = None
        self.mapper_task = None

    async def run(self):
        poll_interval = self.cfg.device_poll_seconds
        printed_waiting = False

        while True:
            if self.mapper is not None and not self._mapper_alive():
                await self._stop_mapper("device disconnected or mapper crashed")

            if self.mapper is None:
                devices = find_devices(self.cfg.device_name_match, required=False)
                if devices:
                    # brief settle so sibling interfaces of a composite
                    # device (e.g. a dongle splitting into several nodes)
                    # have time to enumerate together
                    await asyncio.sleep(0.5)
                    devices = find_devices(self.cfg.device_name_match, required=False)
                if devices:
                    await self._start_mapper(devices)
                    printed_waiting = False
                elif not printed_waiting:
                    print(f"Waiting for a device matching '{self.cfg.device_name_match}'...")
                    printed_waiting = True

            await asyncio.sleep(poll_interval)

    async def shutdown(self):
        await self._stop_mapper("shutting down")


def main():
    cfg = load_config()
    print(f"Loaded config: {cfg.path}")

    supervisor = Supervisor(cfg)
    print("Steam button mapper supervisor running. Ctrl+C to stop.")
    try:
        asyncio.run(supervisor.run())
    except KeyboardInterrupt:
        asyncio.run(supervisor.shutdown())


if __name__ == "__main__":
    main()
