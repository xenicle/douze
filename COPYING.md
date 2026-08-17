# Licensing

This repository holds two different kinds of work, under two licences.

## Code — AGPL-3.0-or-later

Everything under `tools/`, `fx/`, `pipewire/`, `systemd/` and `udev/`.

Full text: [LICENSE](LICENSE).

Copyright (C) 2026 xenicle <tony.cuny@gmail.com>

Douze FX embeds [JUCE 9](https://juce.com), used under its AGPLv3 option (JUCE is
dual-licensed AGPLv3 / commercial). That is why the code is AGPL rather than a
more permissive licence: it is the condition under which JUCE may be used for
free. The AGPL's network clause is not a concern in practice here — Douze is a
tool you run on your own machine, and its web GUI binds to `127.0.0.1` only.

## Protocol documentation and captures — CC0-1.0

`PROTOCOL.md`, `captures/` and `docs/` are released into the **public domain**
under [CC0 1.0](LICENSES/CC0-1.0.txt).

This is deliberate, and it is the more important half of the decision.

A documented protocol is not a literary work — it is a **fact about a piece of
hardware**. The point of writing it down is that it should end up somewhere more
durable than this repository: a kernel driver, an ALSA control plugin, someone
else's tool, a wiki. An attribution clause would force a kernel developer to
carry a credit line for a *fact*, and that kind of requirement is exactly what
makes upstream maintainers hesitate to reuse third-party documentation.

So: take it. No permission needed, no credit needed, no need to ask. If it saves
you the weeks of captures it cost to produce, that is the whole point.

(Credit is still welcome — but the git history and a thank-you line do that
better than a legal clause ever will.)

## In short

| Path | Licence |
|---|---|
| `tools/`, `fx/`, `pipewire/`, `systemd/`, `udev/` | AGPL-3.0-or-later |
| `PROTOCOL.md`, `captures/`, `docs/` | CC0-1.0 (public domain) |
| `README.md`, this file | CC0-1.0 |
