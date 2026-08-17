#!/usr/bin/env python3
"""Décortique une capture usbmon (pcapng) : trafic bulk/control lisible.

Usage :
    python tools/usbdump.py captures/04-monitor-volume.pcapng
    python tools/usbdump.py captures/04-monitor-volume.pcapng --addr 11
    python tools/usbdump.py captures/02-launch-ssl360.pcapng --summary

Nécessite tshark (fourni par le devshell).
"""

import argparse
import shutil
import subprocess
import sys
from collections import Counter

FIELDS = [
    "frame.number",
    "frame.time_relative",
    "usb.device_address",
    "usb.endpoint_address",
    "usb.transfer_type",
    "usb.urb_type",
    "usb.bmRequestType",
    "usb.setup.bRequest",
    "usb.setup.wValue",
    "usb.setup.wIndex",
    "usb.capdata",
]

TRANSFER_TYPES = {"0": "iso", "1": "intr", "2": "ctrl", "3": "bulk",
                  "0x00": "iso", "0x01": "intr", "0x02": "ctrl", "0x03": "bulk"}


def run_tshark(path, addr):
    display = "(usb.transfer_type == 2 || usb.transfer_type == 3)"
    if addr is not None:
        display += f" && usb.device_address == {addr}"
    cmd = [
        "tshark", "-r", path, "-Y", display,
        "-T", "fields", "-E", "separator=\t",
    ]
    for f in FIELDS:
        cmd += ["-e", f]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"tshark a échoué :\n{proc.stderr.strip()}")
    for line in proc.stdout.splitlines():
        yield (line.split("\t") + [""] * len(FIELDS))[: len(FIELDS)]


def fmt_payload(capdata):
    if not capdata:
        return "", ""
    raw = bytes.fromhex(capdata.replace(":", ""))
    hexs = " ".join(f"{b:02x}" for b in raw)
    asc = "".join(chr(b) if 32 <= b < 127 else "." for b in raw)
    return hexs, asc


def direction(ep_addr):
    try:
        return "IN " if int(ep_addr, 0) & 0x80 else "OUT"
    except ValueError:
        return "?  "


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pcap")
    ap.add_argument("--addr", type=int, default=None,
                    help="ne garder que ce device_address (cf. JOURNAL.md)")
    ap.add_argument("--summary", action="store_true",
                    help="motifs récurrents plutôt que trame par trame")
    args = ap.parse_args()

    if shutil.which("tshark") is None:
        sys.exit("tshark introuvable — installer wireshark-cli "
                 "(ou lancer depuis `nix develop`).")

    rows = list(run_tshark(args.pcap, args.addr))
    if not rows:
        sys.exit("Aucune trame bulk/control ne correspond (mauvais --addr ?).")

    if args.summary:
        buckets = Counter()
        samples = {}
        for (_, _, dev, ep, ttype, urb, _, _, _, _, capdata) in rows:
            if not capdata:
                continue
            raw = bytes.fromhex(capdata.replace(":", ""))
            key = (dev, direction(ep).strip(), TRANSFER_TYPES.get(ttype, ttype),
                   raw[:2].hex(), len(raw))
            buckets[key] += 1
            samples.setdefault(key, raw)
        print(f"{'nb':>5}  {'dev':>3} {'dir':3} {'type':4} {'préfixe':7} {'len':>3}  exemple")
        for key, n in buckets.most_common():
            dev, d, t, prefix, ln = key
            hexs, _ = fmt_payload(samples[key].hex())
            print(f"{n:>5}  {dev:>3} {d:3} {t:4} {prefix:7} {ln:>3}  {hexs[:66]}")
        return

    print(f"{'frame':>6} {'t(s)':>9} {'dev':>3} {'ep':>4} {'dir':3} {'type':4}  payload")
    for (num, t, dev, ep, ttype, urb, bmrt, breq, wval, widx, capdata) in rows:
        hexs, asc = fmt_payload(capdata)
        setup = ""
        if ttype in ("2", "0x02") and breq:
            setup = f"[setup bmRT={bmrt} bReq={breq} wVal={wval} wIdx={widx}] "
        try:
            ts = f"{float(t):9.4f}"
        except ValueError:
            ts = f"{t:>9}"
        print(f"{num:>6} {ts} {dev:>3} {ep:>4} {direction(ep)} "
              f"{TRANSFER_TYPES.get(ttype, ttype):4}  {setup}{hexs}"
              + (f"  |{asc}|" if asc else ""))


if __name__ == "__main__":
    main()
