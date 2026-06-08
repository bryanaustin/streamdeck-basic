# Stream Deck Basic

Drive an Elgato Stream Deck from a simple YAML file on **Linux**. It is fully
**headless** — there is no GUI, tray app, or configuration window. The entire
layout is **declared in one YAML file** and applied directly to the device, so it
runs happily on a server, a kiosk, or any always-on box with no desktop attached.
Each button can show an image and a text label, and run a bash command when
pressed. Buttons can also switch between **pages**, so you get folder-style
navigation. The app is built to **survive disconnects gracefully** — unplug the
deck, suspend/resume the machine, replug it, and it just reconnects and carries on.

Built on the [`python-elgato-streamdeck`](https://github.com/abcminiuser/python-elgato-streamdeck)
library (`pip install streamdeck`).

## Features

- **Headless, YAML-driven** — no GUI; the whole layout lives in one declarative
  file, ideal for servers and kiosks. Edit the file, restart, done.
- YAML configuration with multiple pages and `goto` navigation.
- Per-button **image** (auto-scaled), **text label**, and **bash command**.
- **Execution states** — command buttons show a spinner while running, then a
  success/error image; a second press stops a running command.
- **Animated keys** — drop in a GIF/APNG/animated-WebP and it plays automatically.
- Press or release triggers (`trigger: press|release`).
- Resilient to USB disconnects and suspend/resume — automatic reconnect that
  re-applies the whole layout.
- Runs in the foreground or as a systemd **user** service.

## Requirements

- Linux, Python 3.9+.
- A HID backend system library — on Debian/Ubuntu:

  ```sh
  sudo apt install libhidapi-libusb0      # or: libhidapi-hidraw0
  ```

## Install

```sh
# from the project directory
python3 -m venv .venv && . .venv/bin/activate
pip install .                # or: pip install -r requirements.txt
```

This installs the `streamdeck-basic` console command.

### udev rule (non-root access)

Without this you would have to run as root. Install the bundled rule for Elgato
devices (USB vendor `0fd9`):

```sh
sudo cp udev/70-streamdeck.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Then unplug and replug the Stream Deck.

## Configure

Start from the example and edit it:

```sh
mkdir -p ~/.config/streamdeck
cp config/example.yaml ~/.config/streamdeck/config.yaml
# generate the placeholder icons referenced by the example (optional):
python assets/generate_placeholders.py
```

### Config reference

```yaml
brightness: 50              # 0-100
device:
  serial: null             # null = first deck found; or a specific serial string
timing:
  poll_interval: 1.0       # connection health-check cadence (seconds)
  reconnect_interval: 2.0  # how often to look for the deck while disconnected
defaults:                  # appearance defaults, overridable per button later
  font: /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf
  font_size: 14
  text_color: white
  background: black
  margins: [0, 0, 20, 0]   # top, right, bottom, left (bottom gap leaves room for the label)
start_page: main
pages:
  main:
    - key: 0
      label: Apps
      action: {goto: apps}        # navigate to another page
    - key: 1
      image: ../assets/terminal.png
      label: Terminal
      command: x-terminal-emulator
  apps:
    - key: 5
      label: Back
      action: {goto: main}
```

**Button fields:** `key` (required, 0-based), `image` (optional path, resolved
relative to the config file), `label` (optional text), `command` (optional bash,
run via the shell), `action: {goto: <page>}` (optional navigation), and `trigger`
(`press` default, or `release`). A button may have both `command` and a `goto`.
Keys not listed on a page are left blank; keys beyond your device's range are
skipped with a warning.

**Animated keys:** if `image` points to a multi-frame file (animated GIF, APNG,
or animated WebP) the key plays the animation while that page is visible — looping
from the start each time you navigate to it. By default it uses the file's own
per-frame timing; tune it with an `animation` block, or freeze the key to its
first frame with `animate: false`:

```yaml
- key: 5
  image: ../assets/spinner.gif
  label: Busy
  animation:
    fps: 15      # override the embedded frame rate (optional)
    loop: true   # repeat forever (default); false stops on the last frame
```

**Execution states:** a button with a `command` reflects the command's progress.
Pressing it starts the command and shows a **running** image (a built-in animated
spinner unless you supply one); when it finishes the key shows a **completed**
(exit 0) or **errored** (non-zero) image and stays there until pressed again, which
re-runs the command. Pressing a button *while it is running* stops the command (and
its child processes) and returns the key to its idle `image`. Each state image is
optional — `running` defaults to the spinner, and `errored`/`completed` fall back to
the idle `image` when omitted:

```yaml
- key: 2
  label: Build
  command: make -C "$HOME/project"
  image: ../assets/terminal.png    # idle / default
  states:
    running:   {image: ../assets/spinner.gif}   # optional; omit for the built-in spinner
    errored:   {image: ../assets/error.png}
    completed: {image: ../assets/ok.png}
```

## Run

```sh
streamdeck-basic --config ~/.config/streamdeck/config.yaml
```

Without `--config` it looks at `$STREAMDECK_CONFIG`, then `./config.yaml`, then
`~/.config/streamdeck/config.yaml`. Use `--log-level DEBUG` for more detail.
Press `Ctrl-C` to stop; the deck is reset on exit.

## Run as a systemd user service

```sh
pip install --user .
mkdir -p ~/.config/systemd/user
cp systemd/streamdeck.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now streamdeck.service
systemctl --user status streamdeck.service
```

On a headless/always-on box also run `sudo loginctl enable-linger "$USER"`.
`Restart=on-failure` is just an outer safety net — the app reconnects on its own.

## How disconnect handling works

The Stream Deck library has no hotplug support: when a device is unplugged its
internal read thread stops silently while the object still *looks* alive, and the
next write raises `TransportError`. Stream Deck Basic handles all of this itself:

- a **supervisor loop** re-enumerates devices forever and re-applies the full
  layout on every (re)connect;
- a **health loop** polls `connected()` to detect silent disconnects;
- every device write is guarded, so a disconnect mid-update drops cleanly back to
  reconnecting instead of crashing;
- the button callback is fully guarded so a disconnect during a press can't kill
  input handling.

The same path covers laptop **suspend/resume**.

## Troubleshooting

- **No device found / permission denied:** confirm the udev rule is installed and
  you replugged; check `lsusb | grep -i elgato`. Without the rule, try `sudo` once
  to confirm the device otherwise works.
- **GUI commands don't launch under systemd:** GUI apps need the graphical
  session environment (`DISPLAY`/`WAYLAND_DISPLAY`). Running as a *user* service
  inside your graphical session is the simplest fix.
- **Labels render in a fallback font:** install the DejaVu fonts
  (`sudo apt install fonts-dejavu-core`) or point `defaults.font` at a TTF you have.

## Development

```sh
pip install -e ".[dev]"
pytest                      # config loader/validator tests (no hardware needed)
```

## Scope

Targets the standard key grid (Original/MK.2/Mini/XL — auto-adapts to key count).
Stream Deck **Plus** dials/touchscreen and **Neo** extra controls are not handled
in this version, and the layout is read once at startup (no live config reload).
