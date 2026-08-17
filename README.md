# douze

**Solid State Logic SSL 12 on Linux, without SSL 360.**

SSL ships no Linux software for the SSL 12. The audio side works out of the box
(UAC2, `snd-usb-audio`), but everything the SSL 360 application controls — the
internal mixer, monitoring, phantom power, loopback, routing — is unreachable.
The card behaves like a fixed-function box.

This project reverse-engineers the control protocol and gives that back:

- **`sslctl`** — command-line control of the card: mixer matrix, monitoring,
  preamps, loopback, headphone buses.
- **Douze** — a local web GUI on `http://localhost:1212`: mixer matrix, live
  meters, monitoring section, persistent profiles.
- **Douze FX** — a standalone VST3 host that inserts plugin chains into the
  PipeWire graph (one process per strip), driven from the same GUI.

It follows the path traced by Geoffrey Bennett's Scarlett driver work.

> **Note for contributors:** source comments are in French. They carry most of
> the hard-won knowledge here — why a workaround exists, which failure it
> prevents — and were kept in the language they were written in rather than
> degraded by translation. Everything a user or bug reporter sees (this file,
> program output, the GUI) is English or bilingual. Ask if a comment blocks you.

---

## Status

The protocol is **mapped and in production use**. Full details in
[PROTOCOL.md](PROTOCOL.md).

| Area | State |
|---|---|
| Framing, handshake, checksums | done |
| Gain matrix (full map, dB formula) | done |
| Monitoring: dim / cut / mono / alt / talk, levels | done |
| Preamps: 48V, HPF, inst, polarity | done |
| Loopback, clock readback, hardware button notifications | done |
| Logical mixer: faders, pans, mutes, solos, profiles | done (emulated) |
| A few SETTINGS/clock controls | identified, not decoded |

The device itself knows nothing about faders, pans, mutes, solos or profiles.
SSL 360 emulates them and writes the resulting gain matrix; `sslctl` does the
same and keeps its state in `~/.config/sslctl/state.json`.

Verified against firmware `bcdDevice` 1.44, SSL 360 V2.

---

## Requirements

| | Needed for | Notes |
|---|---|---|
| Linux with PipeWire | everything | tested on PipeWire 1.6 |
| Python 3.11+ and **pyusb** | `sslctl`, Douze GUI | the only runtime dependency |
| The card in **Pro Audio** profile | multichannel routing | set it in `pavucontrol` / your sound settings |
| CMake, Ninja, a C++20 compiler | Douze FX only | JUCE 9 is fetched by CMake |
| PipeWire's `libjack.so.0` | Douze FX only | from `pipewire-jack`; see troubleshooting |

**Nix is not required.** The project was developed on NixOS and ships a flake
(`nix develop` gives you everything, including capture tooling), but nothing
depends on it — `sslctl` and the GUI need only Python and pyusb:

```bash
# Debian/Ubuntu
sudo apt install python3-usb pipewire-audio    # + cmake ninja-build g++ for Douze FX
# Fedora
sudo dnf install python3-pyusb pipewire-jack-audio-connection-kit
# Arch
sudo pacman -S python-pyusb pipewire-jack
# anywhere
pip install --user pyusb
```

Only two things are known to be Linux/PipeWire-specific by design: the USB
control protocol (it is the same on any OS, but this implementation uses
libusb/pyusb) and the routing, which is built on PipeWire. There is no
Windows/macOS target — SSL 360 already covers those.

---

## Install

### 1. Device access, without root

The control interface is a vendor-specific USB device with no kernel driver, so
it needs a udev rule.

```nix
# NixOS
services.udev.extraRules = ''
  SUBSYSTEM=="usb", ATTRS{idVendor}=="31e9", ATTRS{idProduct}=="0024", MODE="0660", GROUP="audio"
'';
```

Other distributions: copy `udev/99-douze.rules` to `/etc/udev/rules.d/`, then

```bash
sudo udevadm control --reload && sudo udevadm trigger
```

Make sure you are in the `audio` group (`id -nG`), then **replug the card**.

**Check it worked:**

```bash
python tools/sslctl.py info
```

It should print the control interface and its two endpoints. If it says
permission denied, see [troubleshooting](#troubleshooting).

### 2. Split the playback pairs (PipeWire)

Set the card's profile to **Pro Audio** first (`pavucontrol` → Configuration).
No other profile exposes the 8 playback channels, and without it there is
nothing to split.

```bash
tools/install-pipewire.sh
systemctl --user restart pipewire
```

This turns the single 8-channel sink into four stereo sinks, so each application
can target one playback pair.

The shipped config is a **template**: it addresses the card by its ALSA node
name, which contains the card's serial number and therefore differs on every
machine. The script finds yours and fills it in. To do it by hand:

```bash
pw-link -o | grep -oE 'alsa_output\.usb-Solid_State_Logic_SSL_12_[^:]*\.pro-output-0' | head -1
sed "s|@SSL12_NODE@|<that name>|g" pipewire/99-ssl12-sinks.conf \
  > ~/.config/pipewire/pipewire.conf.d/99-ssl12-sinks.conf
```

**Check it worked:**

```bash
pw-play --target ssl12.pb34 /usr/share/sounds/alsa/Front_Center.wav
```

> Restarting PipeWire drops the connections of apps that were using it (Discord
> clients and Easy Effects are the usual casualties). Do this before a session,
> not during one.

### 3. The GUI, as a user service

```bash
mkdir -p ~/.config/systemd/user
sed "s|%h/douze|$PWD|" systemd/douze.service > ~/.config/systemd/user/douze.service
systemctl --user daemon-reload && systemctl --user enable --now douze
```

(On Nix, point `ExecStart` at `nix develop … --command python tools/douze.py`
instead, so the daemon gets the devShell's dependencies — see the comments in
the unit file.)

Open <http://localhost:1212>. If the card is absent at login the unit retries
every 10 s.

**The daemon owns the USB device.** The `sslctl` CLI cannot talk to the card
while the daemon runs — stop the service first, or drive everything from the
GUI.

**Check it worked:**

```bash
systemctl --user status douze
journalctl --user -u douze -f
```

---

## Using `sslctl`

```bash
sslctl status                       # what the device itself reports
sslctl show                         # logical mixer state
sslctl master monitor -12           # monitor bus level, in dB
sslctl channel 1 --db -6 --pan L30  # a channel's level and pan
sslctl fader 1 -6                   # logical fader, compiled into the matrix
sslctl route 1 hpa -3               # send channel 1 to headphones A
sslctl 48v 1 on                     # phantom power, per channel
sslctl hpf 1 on / inst 3 on / phase 2 on
sslctl dim on / cut on / mono on / alt on / talk on
sslctl loopback pb34                # loopback source
sslctl bus hpa follow                # bus mode: follow mix 1-2 / cut / mono
sslctl user 1 talkback              # reassign a USER button
```

`sslctl --help` lists everything. For key bindings, use relative moves:
`sslctl master monitor --rel -3`.

`sslctl sync` pushes the whole logical state back to the card — useful after a
power cycle, since the device starts blank.

---

## Douze FX (plugin host)

One process per strip: a source, a chain of VST3 plugins, a destination. A strip
can create its own **virtual microphone** or **virtual sink**, so other
applications pick it like any device — a noise suppressor on a mic, an EQ on a
voice-chat return.

```bash
cmake -S fx -B build-fx -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build-fx
python tools/douzefx.py list|start <id>|stop <id>|state <id>
```

Strips are configured from the Douze GUI (create, wire, reorder plugins, save
sets of strips as profiles). Config lives in `~/.config/douze-fx/`.

Plugins are scanned **one throwaway process per file**, so a plugin that hangs
or crashes cannot take the scan — or your audio — with it. Windows VST3 through
[yabridge](https://github.com/robbert-vdh/yabridge) works.

Design decisions, and the failures behind them:
[docs/DOUZE-FX-BRIEF.md](docs/DOUZE-FX-BRIEF.md).

---

## Troubleshooting

### `sslctl` says the device is not found, or permission denied

1. `lsusb | grep 31e9` — you should see `31e9:0024 SSL Control I/F`. If only
   `31e9:0005` appears, the card is in a mode that hides the control interface;
   replug it.
2. `id -nG | grep audio` — you must be in the group named in the udev rule.
3. Rule not applied? Rules only run on **device events**: reload and replug.
4. **The uaccess trap (NixOS).** Do not put `TAG+="uaccess"` in
   `services.udev.extraRules`. That generates `99-local.rules`, but uaccess is
   applied by `73-seat-late.rules` — a uaccess rule numbered 99 is silently
   ignored, and you get a rule that looks right and does nothing. Use
   `MODE`/`GROUP` as shown above.
5. Is the Douze daemon running? It holds the device. `systemctl --user stop douze`.

### The four `ssl12.pbXX` sinks never appear

Almost always the card profile. Only **Pro Audio** exposes the 8 playback
channels; in the default duplex profile the ports the config targets do not
exist, so PipeWire loads four loopbacks that connect to nothing.

```bash
pw-link -o | grep -i solid          # should list …pro-output-0:playback_AUX0..7
```

If the node name changed (different card, or you re-ran with another unit),
re-run `tools/install-pipewire.sh` — the name is baked into the config.

### The card is silent, or an application plays into the void

The `ssl12.pbXX` sinks are loopbacks that must stay linked to the card. If the
ALSA node is recreated (boot, hot-plug, USB reset) they do **not** reconnect on
their own — the symptom is a card that reports "suspended" and total silence.
The daemon repairs this at startup; if you are not running it:

```bash
card=$(pw-link -o | grep -oE 'alsa_output\.usb-Solid_State_Logic_SSL_12_[^:]*\.pro-output-0' | head -1)
for i in 0 1 2 3 4 5 6 7; do
  pair=$(( i / 2 )); names=(pb12 pb34 pb56 pb78)
  pw-link "ssl12.${names[$pair]}.out:output_AUX$i" "$card:playback_AUX$i"
done
```

### An application lost its virtual microphone / virtual sink

A strip's virtual node is destroyed and recreated every time the strip starts.
Applications keep their `target.object` but lose the actual link, and the
session manager quietly relocates them to the default device. Nothing reports an
error: the app still plays, the strip still runs, only the meters stay at zero.

The daemon re-adopts those streams automatically at strip start (look for
`N application(s) rebranchée(s)` in the journal). If you wired something by
hand, note that **`pw-link -d` by name disconnects every stream sharing that
port name** — use port IDs (`pw-link -lI`) instead.

### A strip shows "Frozen — audio OK" in amber

Its control thread is stuck, almost always inside a plugin's native editor that
never returned (Waves plugins under Wine are the usual case). **Your audio is
still being processed** — that is why it is amber and not red. The strip's
editor is lost until you restart it, at a moment of your choosing.

Douze remembers editors that hung and greys them out afterwards, so you do not
walk into the same one twice.

### A strip will not start at all, and its log is empty

The launcher (`fx/tools/run-douze-fx.sh`) has to find PipeWire's
`libjack.so.0`. If it cannot, and falls back to querying Nix, a slow or blocked
Nix evaluation leaves the launcher hanging before it prints anything — the
supervisor then sees a live process that never answers.

Point it straight at the library:

```bash
DOUZE_FX_JACK_LIB=/path/to/pipewire-jack/lib python tools/douzefx.py start mic
```

The resolved path is cached in `~/.cache/douze-fx/jacklib` and reused.

### Windows plugins die as soon as they load

Almost always an inherited `LD_LIBRARY_PATH` — libraries from a different
toolchain than the system's, which kills yabridge's Wine host. The launcher
deliberately **replaces** the variable rather than extending it. If you wrapped
it in your own script or service, do not re-export a broader path.

Symptoms: strip dies with `code -6`, `terminate called without an active
exception` in the strip log, or *"The Wine host process has exited
unexpectedly"*.

### Meters do not move, although audio is flowing

The engine resets its peaks on every read — reading is consuming. The daemon
serves one shared reading to all clients, so the GUI is fine. But if you poll
`/fx` or a strip's `/state` yourself in a loop, you are stealing peaks from the
GUI. Read the daemon's event stream (`/events`) instead.

### Waves (or other "shell") plugins do not appear after a scan

One binary can hold hundreds of sub-plugins, and instantiating them one by one
overflows a Wine thread's stack. Douze detects this and retries with a
factory-only enumeration. If they are still missing, scan that file directly:

```bash
build-fx/douze_fx_artefacts/RelWithDebInfo/douze_fx --scanshell "/path/WaveShell….vst3" /tmp/out.txt
```

### LADSPA plugins are missing from the picker

Deliberate. The host does not load LADSPA, and LADSPA has no state saving, so a
saved chain could not restore its settings. Only formats the engine can actually
host are offered.

### The binary breaks after `nix-collect-garbage`

Douze FX loads `libjack` via `dlopen`, which is invisible to `ldd` and therefore
to Nix's dependency scan. Run `tools/gcroots.sh` after each rebuild.

---

## Tests

```bash
python tools/test_douzefx.py   # supervisor, scanner, profiles — no hardware needed
fx/tools/run_tests.sh          # engine checks, no audio device needed
python fx/tools/nulltest.py    # audio path is bit-transparent (daemon must run)
```

The null test is the one that matters for an audio tool: it injects a known
signal through a passthrough strip, re-records it, realigns and subtracts. The
residual must be **exactly zero** — and it also measures the residual one sample
off, to prove the measurement is capable of failing.

---

## Reverse engineering

[PROTOCOL.md](PROTOCOL.md) is the source of truth. To extend it, capture SSL 360
talking to the card: run it in a Windows VM with USB passthrough of **both**
devices, and capture from the host with `usbmon` — the host sees everything the
VM exchanges.

```bash
sudo modprobe usbmon
lsusb | grep 31e9        # note bus and address; it changes on every replug
tshark -r NN-x.pcapng -Y 'usb.device_address == ADDR' -w NN-x.ctl.pcapng
```

Then decode:

```bash
python tools/usbdump.py   captures/NN-x.ctl.pcapng --addr 13             # raw frames
python tools/ssldecode.py captures/NN-x.ctl.pcapng --addr 13 --no-noise  # SSL messages
python tools/ssldecode.py captures/NN-x.ctl.pcapng --addr 13 --summary
```

What made this tractable: **one capture per action**, log every session in
`captures/JOURNAL.md` (including the USB address), and leave ~5 s of silence
before and after so periodic traffic stands out. Raw captures are huge (~250 MB
per 30 s, audio included) — keep only the filtered `.ctl.pcapng`.

> Never send invented frames to this firmware. Replay only sequences observed in
> captures until the encoding is understood.

---

## License

Two licences, on purpose — see [COPYING.md](COPYING.md).

- **Code** (`tools/`, `fx/`, `pipewire/`, `systemd/`, `udev/`):
  **AGPL-3.0-or-later**. Douze FX embeds JUCE 9 under its AGPLv3 option, which
  is what fixes the choice.
- **Protocol documentation and captures** (`PROTOCOL.md`, `captures/`, `docs/`):
  **CC0-1.0**, public domain.

The second one matters more than the first. A documented protocol is a fact
about a piece of hardware, not a literary work, and the best outcome for it is
to end up in a kernel driver or someone else's tool. So it carries no
attribution requirement at all — take it, no need to ask.
