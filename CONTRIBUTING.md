# Contributing

Patches, bug reports and protocol findings are all welcome. This is a small
project; there is no process to speak of.

## Before anything else

**Never send invented frames to the card.** Replay only byte sequences observed
in a capture. This is firmware nobody has a datasheet for, and a bricked SSL 12
is not a recoverable mistake.

`tools/sslctl.py listen` is passive and safe. Start there.

## Language

**The source comments are in French.** They carry most of the knowledge in this
repository — not what the code does, but why it is shaped that way and which
failure it prevents. Translating them hastily would degrade the part that
matters most, so they stay in the language they were written in.

Everything a user or a bug reporter sees is English or bilingual: the README,
program output, the GUI. Write new user-facing strings in English; write
comments in whichever of the two you are comfortable with. If a comment blocks
you, open an issue and ask — that is a perfectly good issue.

## Running the tests

```bash
python tools/test_douzefx.py   # supervisor, scanner, profiles — no hardware
fx/tools/run_tests.sh          # engine — no audio device
python fx/tools/nulltest.py    # audio path is bit-transparent (daemon must run)
```

The first one needs nothing but a checkout; the second needs Douze FX built.
Please run them before submitting.

If you touch anything in the audio path, run the null test too, and say so in
the pull request.

## Reporting a bug

Include:

- what the card is (`lsusb | grep 31e9`) and its firmware (`bcdDevice`),
- your PipeWire version and whether the card is in **Pro Audio** profile,
- `journalctl --user -u douze -b --no-pager` for daemon problems,
- `~/.cache/douze-fx/<strip>.log` for a plugin strip (and `.log.1`, which holds
  the session *before* the last restart — often the one that explains it).

The [troubleshooting section](README.md#troubleshooting) covers the failures we
already hit; each entry names a real one.

## Contributing protocol findings

This is the most valuable kind of contribution.

Capture SSL 360 talking to the card (see the README), then:

- **one capture per action** — it is what makes analysis tractable;
- log the session in `captures/JOURNAL.md`, including the USB address, which
  changes on every replug;
- ship the filtered `.ctl.pcapng`, never the raw one (the raw file is ~250 MB
  per 30 s because usbmon records the whole bus, audio included);
- write what you concluded in `PROTOCOL.md`. That file is the source of truth,
  not the code.

Negative results are worth recording too: "control X produces no traffic" saves
the next person the same afternoon.

## Licensing of contributions

- Code (`tools/`, `fx/`, `pipewire/`, `systemd/`, `udev/`) → **AGPL-3.0-or-later**.
- Protocol documentation and captures (`PROTOCOL.md`, `captures/`, `docs/`) →
  **CC0-1.0**, public domain.

By contributing you agree your work goes out under the licence of the area you
touched. See [COPYING.md](COPYING.md) for why the protocol is CC0 — the short
version is that a documented protocol is a fact about hardware, and it should be
free to end up in a kernel driver without anyone having to ask.
