#!/usr/bin/env python3
"""Décode le protocole SSL 12 dans une capture (couche message, cf. PROTOCOL.md).

Framing : ff | opcode | len | payload[len] | checksum(somme opcode..payload).
Côté IN, chaque paquet USB de 64 o porte un en-tête `31 xx` et les messages
forment un flux continu — réassemblé ici avant décodage.

Usage :
    python tools/ssldecode.py captures/02-launch-ssl360.ctl.pcapng --addr 13
    python tools/ssldecode.py captures/04-xxx.ctl.pcapng --addr 13 --no-noise
    python tools/ssldecode.py captures/02-launch-ssl360.ctl.pcapng --addr 13 --summary

--no-noise masque le bruit de fond connu (keepalive 0x1b, vumètres 0x6c
longs, heartbeats) pour ne laisser que les échanges intéressants.
"""

import argparse
import shutil
import subprocess
import sys
from collections import Counter

NOISE_OUT = {0x1B}


def read_bulk(pcap, addr):
    cmd = ["tshark", "-r", pcap, "-Y",
           f"usb.device_address == {addr} && usb.capdata && usb.transfer_type == 3",
           "-T", "fields", "-e", "frame.time_relative",
           "-e", "usb.endpoint_address", "-e", "usb.capdata"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"tshark a échoué :\n{proc.stderr.strip()}")
    for line in proc.stdout.splitlines():
        t, ep, cap = line.split("\t")
        yield float(t), int(ep, 16), bytes.fromhex(cap.replace(":", ""))


def parse(pcap, addr):
    """Retourne (messages, anomalies, compteur d'en-têtes IN).

    message = (t, "IN "/"OUT", opcode, payload, cksum_ok)
    """
    msgs, bad = [], []
    inhdr = Counter()
    instream = bytearray()
    stream_t = []  # (offset, timestamp) pour dater les messages IN

    for t, ep, raw in read_bulk(pcap, addr):
        if ep & 0x80:
            for off in range(0, len(raw), 64):
                chunk = raw[off:off + 64]
                if len(chunk) < 2 or chunk[0] != 0x31:
                    bad.append((t, "IN ", "en-tête ≠ 31 xx : " + chunk[:8].hex()))
                    continue
                inhdr[chunk[1]] += 1
                if len(chunk) > 2:
                    stream_t.append((len(instream), t))
                    instream += chunk[2:]
        else:
            if raw and set(raw) == {0}:
                bad.append((t, "OUT", f"{len(raw)} octets à zéro (flush)"))
                continue
            i = 0
            while i < len(raw):
                if raw[i] != 0xFF or i + 3 > len(raw):
                    bad.append((t, "OUT", "désync : " + raw[i:i + 8].hex()))
                    break
                op, ln = raw[i + 1], raw[i + 2]
                end = i + 4 + ln
                if end > len(raw):
                    bad.append((t, "OUT", "tronqué : " + raw[i:].hex()))
                    break
                payload = raw[i + 3:end - 1]
                ok = (op + ln + sum(payload)) & 0xFF == raw[end - 1]
                msgs.append((t, "OUT", op, payload, ok))
                i = end

    i, ti = 0, 0
    while i + 4 <= len(instream):
        while ti + 1 < len(stream_t) and stream_t[ti + 1][0] <= i:
            ti += 1
        t = stream_t[ti][1]
        if instream[i] != 0xFF:
            bad.append((t, "IN ", "désync : " + bytes(instream[i:i + 8]).hex()))
            i += 1
            continue
        op, ln = instream[i + 1], instream[i + 2]
        end = i + 4 + ln
        if end > len(instream):
            break  # flux tronqué en fin de capture
        payload = bytes(instream[i + 3:end - 1])
        if (op + ln + sum(payload)) & 0xFF == instream[end - 1]:
            msgs.append((t, "IN ", op, payload, True))
            i = end
        else:
            bad.append((t, "IN ", "cksum KO : " + bytes(instream[i:end]).hex()))
            i += 1

    msgs.sort(key=lambda m: m[0])
    return msgs, bad, inhdr


def is_noise(d, op, payload):
    if d == "OUT" and op in NOISE_OUT:
        return True
    if d == "IN " and op == 0x6C and len(payload) > 40:  # vumètres
        return True
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pcap")
    ap.add_argument("--addr", type=int, required=True,
                    help="device_address du Control I/F (cf. JOURNAL.md)")
    ap.add_argument("--summary", action="store_true",
                    help="inventaire des opcodes plutôt que chronologie")
    ap.add_argument("--no-noise", action="store_true",
                    help="masquer keepalive 0x1b et vumètres 0x6c")
    args = ap.parse_args()

    if shutil.which("tshark") is None:
        sys.exit("tshark introuvable — installer wireshark-cli "
                 "(ou lancer depuis `nix develop`).")

    msgs, bad, inhdr = parse(args.pcap, args.addr)
    if not msgs:
        sys.exit("Aucun message décodé (mauvais --addr ?).")

    ok = sum(1 for m in msgs if m[4])
    print(f"# {len(msgs)} messages ({ok} checksum OK), "
          f"{len(bad)} anomalies, en-têtes IN : "
          + ", ".join(f"31 {h:02x} ×{n}" for h, n in inhdr.most_common()))
    for b in bad[:8]:
        print(f"#   anomalie t={b[0]:.3f} {b[1]} {b[2]}")

    if args.summary:
        c = Counter((m[1], m[2], len(m[3])) for m in msgs)
        print(f"{'nb':>6}  dir op    len  exemple")
        for (d, op, ln), n in c.most_common():
            ex = next(m[3] for m in msgs if m[1] == d and m[2] == op
                      and len(m[3]) == ln)
            print(f"{n:>6}  {d} 0x{op:02x} {ln:>4}  {ex[:24].hex(' ')}")
        return

    for t, d, op, payload, ok in msgs:
        if args.no_noise and is_noise(d, op, payload):
            continue
        flag = "" if ok else "  !CKSUM"
        print(f"{t:9.4f} {d} 0x{op:02x} [{len(payload):3}] "
              f"{payload.hex(' ')}{flag}")


if __name__ == "__main__":
    main()
