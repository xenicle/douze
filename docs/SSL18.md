# SSL 18 on Linux — status and how to help

> **Public domain — [CC0 1.0](../LICENSES/CC0-1.0.txt).** *Domaine public.*
> Not affiliated with Solid State Logic.

## Where things stand

| | SSL 12 | SSL 18 |
|---|---|---|
| Audio (UAC2, `snd-usb-audio`) | works | works (reported) |
| Control interface | `31e9:0024` | `31e9:0026` (same `bcdDevice 0x1000`) |
| Control protocol decoded | **yes** (PROTOCOL.md) | **no — nothing captured yet** |
| Mixer / monitoring / 48 V from Linux | yes | **no** |
| **Douze FX** (VST3 EQ, compressor, gate… in PipeWire) | yes | **yes — needs no protocol** |

**You can have an EQ today.** Douze FX is a plugin host that sits in the PipeWire
graph; it does not talk to the card. Put an EQ (LSP, x42, Calf, any VST3…) on an
input and route the result to a virtual mic or to your DAW/apps. What the SSL 18
cannot do yet is what SSL 360 does *inside the hardware* (routing matrix, monitor
section, preamp switches).

Every frame Douze sends was observed from SSL 360 talking to an SSL 12. The SSL 18
likely shares the framing (`ff | opcode | len | payload | checksum`) but has more
channels, so its gain matrix and control numbers certainly differ. **Douze never
sends invented bytes to a firmware**, so with `SSL_MODEL=ssl18`:

- the daemon does **not touch the USB control interface at all**;
- `sslctl` refuses every write (read-only commands stay available);
- Douze FX, link repair and the anti-suspend rule keep working.

## Use Douze FX with an SSL 18 now

```bash
# 1. card in Pro Audio profile (pavucontrol → Configuration)
# 2. keep the card from suspending (SSL 18: no stereo sinks, only this rule)
SSL_MODEL=ssl18 tools/install-pipewire.sh
systemctl --user restart pipewire pipewire-pulse wireplumber
# 3. run the daemon in SSL 18 mode (effects only)
SSL_MODEL=ssl18 python tools/douze.py        # or add Environment=SSL_MODEL=ssl18
                                             # to the systemd unit
```

Then open <http://localhost:1212>, **FX** tab: create a strip, pick the SSL 18
input as source, add your EQ, send it to a virtual mic or sink.

If the strip cannot find your card, run the diagnostic below and send us the
output: the PipeWire node name of the SSL 18 has never been seen on a real
machine (the code *assumes* `alsa_output.usb-Solid_State_Logic_SSL_18…`).

## Step 1 — safe, read-only report (5 minutes, no risk)

None of these write anything to the card.

```bash
lsusb -v -d 31e9:0026 2>/dev/null | head -80      # descriptors
SSL_MODEL=ssl18 python tools/sslctl.py diag       # USB + PipeWire nodes, one report
SSL_MODEL=ssl18 python tools/sslctl.py info       # endpoints of the control interface
```

(`sslctl.py` only needs Python 3.11+ and `pyusb`. If you get *permission denied*,
install `udev/99-douze.rules` — it now covers `31e9:0026` — and replug the card.)

Paste the output in an issue.

## Step 2 — listen while the manufacturer's app talks (read-only for the card)

The firmware **never speaks first**: it only answers the host. So `listen` on an
idle card showing nothing is **normal** and proves nothing. We need the traffic of
SSL 360 itself.

You need SSL 360 (Windows or macOS only) talking to the SSL 18, and a way to see
the USB bus:

- **Linux host + Windows VM with USB passthrough of both SSL devices** (how the
  SSL 12 was mapped): capture on the host with `usbmon`, it sees everything the VM
  exchanges.
- **Native Windows**: Wireshark with USBPcap, same rules below.
- macOS: no easy way; if you only have a Mac, say so and we will find another route.

```bash
sudo modprobe usbmon
lsusb | grep 31e9              # note BUS and ADDRESS of 31e9:0026 (changes on replug)
sudo tshark -i usbmonN -w raw.pcapng     # N = bus number; Ctrl-C to stop
```

Rules that make a capture usable:

1. **One action per capture.** E.g. "move the monitor fader slowly from −∞ to 0 dB",
   then stop. Not five things at once.
2. Leave about **5 s of silence** before and after, so periodic traffic stands out.
3. **Write a log**: for each capture, what you did and at roughly which second.
   Without it the capture is useless. Copy the template in
   [`captures/JOURNAL.md`](../captures/JOURNAL.md) and add the **SSL 360 version**
   and the **firmware version**.
4. Filter before sharing — raw files are huge (audio included):
   `tshark -r raw.pcapng -Y 'usb.device_address == ADDR' -w NN-action.ctl.pcapng`
5. Share the `.ctl.pcapng` and the log (issue, or email).

Start with: (a) just launching SSL 360, ~30 s, no touching anything — this contains
the handshake; (b) one fader; (c) 48 V on channel 1; (d) one monitoring button.

## What we do not ask you to do

- Do **not** replay captures or `sslctl send` frames to the SSL 18. We have no idea
  what an SSL 12 frame does to that firmware. (`SSL_ALLOW_UNVALIDATED=1` exists to
  lift the safety, for people who accept the risk and say so.)
- Do not upgrade the firmware just before capturing; note the version instead.

## For maintainers: promoting the SSL 18

When captures have been analysed and written to `PROTOCOL.md`, and a frame set has
been **validated on a real SSL 18**: add `"ssl18"` to
`PROTOCOLE_VALIDE` in `tools/sslmodel.py`, adapt the channel map / sinks for its
channel count, and log it in `captures/JOURNAL.md`.
