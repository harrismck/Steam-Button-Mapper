#!/usr/bin/env python3
"""
watch_all.py

Prints every EV_KEY / EV_ABS event from every readable /dev/input device,
tagged with the source device name. Useful for hunting down which device
and event code a mystery button actually sends.

Non-exclusive: does NOT grab any device, so it won't interfere with
InputPlumber, gamescope, or anything else currently using your inputs;
it just listens alongside them.

Devices currently held by an exclusive grab (e.g. a handheld controller
daemon's grab on the raw controller device) will not show events here
either. That's expected, same limitation evtest has.

Usage: sudo python3 watch_all.py
(sudo is usually needed to read from devices you don't own outright, e.g.
system-owned inputs)
"""

import asyncio
from evdev import InputDevice, categorize, ecodes, list_devices

INTERESTING_TYPES = {ecodes.EV_KEY, ecodes.EV_ABS}


async def watch(dev):
    try:
        async for event in dev.async_read_loop():
            if event.type in INTERESTING_TYPES:
                print(f"[{dev.name:30s}] {categorize(event)}")
    except OSError as e:
        print(f"[{dev.name}] stopped watching: {e}")


async def main():
    devices = []
    for path in list_devices():
        try:
            devices.append(InputDevice(path))
        except OSError as e:
            print(f"Skipping {path}: {e}")

    print("Watching (Ctrl+C to stop):")
    for d in devices:
        print(f"  {d.name} ({d.path})")
    print()

    await asyncio.gather(*(watch(d) for d in devices), return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
