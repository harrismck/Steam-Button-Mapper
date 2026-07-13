# steam-button-mapper

Maps keyboard keys to SteamOS gamepad/keyboard signals, so a keyboard-only
setup (e.g. a handheld docked to a TV) can still open the Steam menu,
Quick Access Menu (QAM), and other shortcuts that otherwise require a controller.

## Why this exists

SteamOS's built in `Ctrl+1` / `Ctrl+2` keyboard shortcuts for the Steam menu and
QAM only work while Steam's own UI/overlay has keyboard focus, they don't
work once a game has taken over input. What *does* work everywhere, is the
literal gamepad Guide button signal (`BTN_MODE`in Linux terms). This is what a
real controller's dedicated Steam button (or xbox button, PS button etc), actually sends.

This tool grabs your keyboard, mirrors all its normal keys through a
virtual keyboard so nothing is lost, and lets you designate specific keys
to instead fire either:
- a **gamepad button/chord** via a virtual "Xbox 360 pad" (recognized
  system-wide, including in-game), or
- a **keyboard combo** via the virtual keyboard (a Steam UI-level
  shortcut -- works in Steam's own menus, not once a game has focus).

Bindings only take effect while SteamOS Gaming Mode (gamescope) is
detected running, or while manually overridden -- so the same keys behave
completely normally in Desktop Mode.

## Features

- Remap any key to: another key, a combo of keys, or a virtual gamepad button press!
- Already set up to map the buttons you'll most likely want in SteamOS
- Can toggle on/off the key mapping with a button combo 
  - In addition to automatically toggling off when in Desktop Mode
- Optional notifications when mapping is turned on/off
  - And/Or fire off a simple beep to notify mapping is on/off
  - Can also call an arbitrary script (hook) when toggled. 
    Useful if you want to indicate the state some other way.
- Easily customizable with a simple YAML file
## Known limitations

- Keyboard-combo bindings (QAM, Menu, etc.) work while Steam's own UI has
  focus, but not once you're actually inside a running game. This is a
  known Valve limitation on the keyboard-shortcut path itself, not
  something this tool can currently work around.
- Firmware-level keyboard actions (e.g. an RGB-lighting-cycle key bound in
  your keyboard's own configurator) can't be triggered by this tool's
  virtual keyboard.

## Install

Check whether `evdev` and `PyYAML` are already available -- on SteamOS
they often are, as system packages:

```
python3 -c "import evdev, yaml; print(evdev.__file__); print(yaml.__file__)"
```

If that prints two paths under `/usr/...` with no error, skip straight to
Configure below, and run everything with your system
`python3` directly. No install step/container needed.

If it errors with `ModuleNotFoundError`, install them for your user:

```
pip install --user -r requirements.txt
```

This doesn't need root and doesn't touch `/usr`, so it works fine on
SteamOS's read-only root as-is.

<details>
<summary>Advanced: if pip itself doesn't work on your system</summary>

If `pip install --user` fails outright (no pip, no compiler, an
"externally-managed-environment" error, etc.), you can try
[distrobox](https://github.com/89luca89/distrobox), which installs
software to your home directory and survives OS updates. Check first
whether you already have it -- **SteamOS has shipped Podman and Distrobox
pre-installed since SteamOS 3.5**:

```
which distrobox podman
```

If both are found:

```
distrobox create --name pyenv --image docker.io/library/archlinux:latest
distrobox enter pyenv
sudo pacman -Syu python python-pip
pip install --user -r requirements.txt
exit
```

If either is missing, install distrobox itself:

```
curl -s https://raw.githubusercontent.com/89luca89/distrobox/main/install | sh -s -- --prefix ~/.local
```

and podman via the current static-binary release (the old
`extras/install-podman` convenience script is deprecated -- don't use it):

```
curl -L -o ~/.local/bin/podman https://github.com/89luca89/podman-launcher/releases/latest/download/podman-launcher-amd64
chmod +x ~/.local/bin/podman
```

Add `~/.local/bin` to your PATH (in `~/.bashrc`) and repeat the
`distrobox create`/`enter`/`pacman`/`pip` steps above. Your home directory
and `/dev` are shared with the container automatically, so no copying
needed. If you go this route, your systemd `ExecStart` (below) needs to
invoke the script through the container -- see the commented example in
`steam-button-mapper.service`.

</details>

## Configure

```
mkdir -p ~/.config/steam-button-mapper
cp config.example.yaml ~/.config/steam-button-mapper/config.yaml
```

Edit the copy to your liking! It's got lots of explanatory comments.

### Finding your device name and mystery button codes

```
python3 -c "import evdev; print([evdev.InputDevice(p).name for p in evdev.list_devices()])"
```

If you're trying to figure out what a specific button sends (useful when
porting this to a different keyboard or handheld), run:

```
sudo python3 watch_all.py
```

and press the button in question. It prints every key/abs event from every
readable device, tagged by source device name. Note: if nothing appears at
all even with sudo across every device, it likely means the action is handled
entirely inside the keyboard/controller's own firmware and never reaches the OS.

## Run

```
python3 steam_button_mapper.py
```

Or with a config path in a non-default location:

```
python3 steam_button_mapper.py /path/to/config.yaml
# or
STEAM_BUTTON_MAPPER_CONFIG=/path/to/config.yaml python3 steam_button_mapper.py
```

## Run permanently (systemd user service)

First confirm the `ExecStart` path in the steam-button-mapper.service file
actually matches your system:

```
which python3
```

If it's not `/usr/bin/python3`, edit `ExecStart` in the copied service
file to match.

```
mkdir -p ~/.config/systemd/user
cp steam-button-mapper.service ~/.config/systemd/user/
```

Then:

```
systemctl --user daemon-reload
systemctl --user enable --now steam-button-mapper.service
systemctl --user status steam-button-mapper.service
journalctl --user -u steam-button-mapper -f
```

The service file has commented-out `ExecStart` alternatives for a venv or
distrobox setup, if you ended up needing one of those instead.

## Hooks

`on_state_change_hook` in your config runs a bash command any time the
effective remap state changes (toggled manually, or Gaming Mode
entered/exited), with `STATE=on` or `STATE=off` in its environment. Use
this for things like changing a status LED, playing a custom sound, or
calling out to a script that talks to your keyboard's hardware directly

## Troubleshooting

**`error: externally-managed-environment` from pip** (common inside a
distrobox Arch container, per [PEP 668](https://peps.python.org/pep-0668/)):
try installing via pacman instead --
```
sudo pacman -S python-evdev python-yaml
```
-- or use a venv, which bypasses the restriction entirely:
```
python -m venv ~/steam-button-mapper-venv
~/steam-button-mapper-venv/bin/pip install -r requirements.txt
```
(point systemd's `ExecStart` at that venv's `python` if you go this route
-- see the commented example in `steam-button-mapper.service`).

**Mapping seems off right after boot into Gaming Mode:** the service
starts as soon as `graphical-session.target` is reached, which may be a
moment before gamescope itself is actually running. The script fast-polls
(every 0.25s) for the first 5 seconds after startup specifically to
converge on the real state quickly, then settles into the normal 1-second
cache interval -- so this should self-correct almost immediately. If it's
still wrong well after that window, check `journalctl --user -u
steam-button-mapper` for what `is_gaming_mode()` is actually seeing.
