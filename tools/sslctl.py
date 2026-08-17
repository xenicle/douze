#!/usr/bin/env python3
"""Contrôle la SSL 12 sous Linux via son interface USB (31e9:0024).

Implémente le protocole rétro-ingéniéré documenté dans PROTOCOL.md :
messages `ff | opcode | len | payload | checksum`, matrice de gains
(30 × couche + slot, 0 dB = 2²⁵), registres booléens/enums.

Usage (exemples) :
    sslctl.py info
    sslctl.py init                       # handshake observé (capture 02)
    sslctl.py listen --secs 30           # écoute décodée (boutons physiques…)
    sslctl.py master monitor -20         # fader master Monitor à -20 dB
    sslctl.py master hp-a 0
    sslctl.py channel 1 -6 --pan 0       # volume canal Analogue 1 (mix principal)
    sslctl.py dim on / cut off / mono on / invert-l off / alt on / talk on
    sslctl.py 48v 1 on / hpf 3 on / inst 3 on / phase 2 off
    sslctl.py loopback pb12 / loopback none
    sslctl.py user 1 dim                 # assigne le bouton USER 1
    sslctl.py cell 4 8 -10               # accès brut : couche 4, slot 8, -10 dB
    sslctl.py send "ff 01 00 01"         # trame brute (séquences observées !)

Accès non-root : udev/99-douze.rules (TAG uaccess), sinon sudo.
"""

import argparse
import json
import math
import os
import sys
import time

import usb.core
import usb.util

VID, PID = 0x31E9, 0x0024
EP_IN, EP_OUT = 0x81, 0x02

UNITY = 1 << 25          # 0 dB (cf. PROTOCOL.md)
STRIDE = 30              # taille d'une couche de la matrice

# Registres (cf. PROTOCOL.md — « Registres identifiés »)
BOOL_CTRL = {"48v": 1, "hpf": 2, "inst": 3, "invert-l": 4, "mono": 5,
             "dim": 6, "cut": 7, "alt": 8, "talk": 9, "phase": 0x0F}
LED_GROUP = {"cut": 0x0C, "alt": 0x0D, "talk": 0x0E}   # LED pilotées par le host
MASTER_INST = {"monitor": 0, "line34": 2, "hp-a": 4, "hp-b": 6}
LOOPBACK = {"none": 0, "pb12": 1, "pb34": 2, "pb56": 3, "pb78": 4,
            "monitor": 5, "line34": 6, "hpa": 7, "hpb": 8}
USER_FN = {"dim": 0, "cut": 1, "mono-sum": 2, "alt": 3, "invert-l": 4,
           "talkback": 5, "gui": 6}
# Slots sources de la matrice (canaux mono Analogue 1-4)
ANALOGUE_SLOT = {1: 8, 2: 9, 3: 10, 4: 11}
# Paires de couches (L, R) par bus — carte complète (PROTOCOL.md)
BUS_LAYERS = {"mix": (0, 1), "line34": (2, 3), "hpa": (4, 5), "hpb": (6, 7)}


# ---------------------------------------------------------------- protocole

def frame(op, payload=b""):
    body = bytes([op, len(payload)]) + payload
    return b"\xff" + body + bytes([sum(body) & 0xFF])


def msg_gain(ctrl, inst, value):
    """sub 06 : set gain u32 (matrice si ctrl=1, DIM LEVEL=3, masters=9…)."""
    p = bytes([0x06, 0]) + ctrl.to_bytes(2, "little") + inst.to_bytes(2, "little") \
        + value.to_bytes(4, "little")
    return frame(0x6B, p)


def msg_bool(ctrl, inst, on, sub=0x04):
    """sub 04 (préampli/monitoring, avec ACK) ou 07 (modes bus, sans ACK)."""
    p = bytes([sub, 0]) + ctrl.to_bytes(2, "little") + inst.to_bytes(2, "little") \
        + bytes([1 if on else 0])
    return frame(0x6B, p)


def msg_get(ctrl, inst):
    """sub 03 : lecture d'un paramètre (réponse IN sub 05 ou 0x0d)."""
    p = bytes([0x03, 0]) + ctrl.to_bytes(2, "little") + inst.to_bytes(2, "little")
    return frame(0x6B, p)


def msg_enum(ctrl, inst, value):
    p = bytes([0x08, 0]) + ctrl.to_bytes(2, "little") + inst.to_bytes(2, "little") \
        + value.to_bytes(2, "little")
    return frame(0x6B, p)


def msg_led(group, on):
    return frame(0x13, bytes([0x01, group, 0x00, 1 if on else 0]))


def db_to_val(db):
    """'off'/'-inf' → 0 ; sinon dB → gain linéaire u32 (0 dB = 2²⁵)."""
    if isinstance(db, str) and db.lower() in ("off", "-inf", "inf-", "mute"):
        return 0
    v = round(UNITY * 10 ** (float(db) / 20))
    if not 0 <= v < 1 << 32:
        sys.exit(f"gain hors plage : {db} dB")
    return v


def val_to_db(v):
    return "-inf" if v == 0 else f"{20 * math.log10(v / UNITY):+.1f} dB"


def parse_in_stream(chunks):
    """Réassemble le flux IN (en-tête 31 xx par paquet 64 o) et itère les
    messages (opcode, payload)."""
    buf = bytearray()
    for raw in chunks:
        for off in range(0, len(raw), 64):
            c = raw[off:off + 64]
            if len(c) >= 2 and c[0] == 0x31:
                buf += c[2:]
    i = 0
    while i + 4 <= len(buf):
        if buf[i] != 0xFF:
            i += 1
            continue
        op, ln = buf[i + 1], buf[i + 2]
        end = i + 4 + ln
        if end > len(buf):
            break
        payload = bytes(buf[i + 3:end - 1])
        if (op + ln + sum(payload)) & 0xFF == buf[end - 1]:
            yield op, payload
            i = end
        else:
            i += 1


def describe(op, payload):
    """Décrit un message IN en clair quand on sait le faire."""
    if op == 0x6C and len(payload) >= 7 and payload[0] == 0x05:
        ctrl = int.from_bytes(payload[2:4], "little")
        inst = int.from_bytes(payload[4:6], "little")
        state = payload[6]
        name = next((k for k, v in BOOL_CTRL.items() if v == ctrl), f"ctrl {ctrl}")
        return f"état {name} (inst {inst}) = {'ON' if state else 'off'}"
    if op == 0x6C and len(payload) >= 6 and payload[0] == 0x11:
        rates = [int.from_bytes(payload[i:i + 4], "little")
                 for i in range(6, len(payload) - 2, 4)]
        return f"horloge/sample rates : {rates}"
    if op == 0x6C and payload[:1] == b"\x09":
        return "vumètres"
    if op == 0x06 and not payload:
        return "fin de réponse"
    return None


# ------------------------------------------------------- mixer logique (état)
#
# Le device ne stocke ni faders, ni pans, ni mutes, ni solos : SSL 360 les
# compile en cellules de matrice (PROTOCOL.md). sslctl fait pareil, avec un
# état persistant dans ~/.config/sslctl/state.json.

STATE_PATH = os.path.join(os.environ.get("XDG_CONFIG_HOME",
                          os.path.expanduser("~/.config")), "sslctl", "state.json")

# canaux du mixer logique : mono (slot) ou stéréo (slot L, slot R)
CHANNELS = {"1": {"slots": (8,)}, "2": {"slots": (9,)},
            "3": {"slots": (10,)}, "4": {"slots": (11,)},
            "pb12": {"slots": (0, 1)}, "pb34": {"slots": (2, 3)},
            "pb56": {"slots": (4, 5)}, "pb78": {"slots": (6, 7)}}
# défaut = profil par défaut de SSL 360 (capture 20) : analogues et pb12 à
# 0 dB centre dans le mix, les autres playbacks fermés
DEFAULT_CH = {"fader": 0.0, "pan": 0.0, "mute": False, "solo": False}
DEFAULT_OFF = {"pb34", "pb56", "pb78"}
SEND_BUSES = ("hpa", "hpb", "line34")   # cibles de `route` (mix = fader)


def default_channel(name):
    c = dict(DEFAULT_CH)
    if name in DEFAULT_OFF:
        c["fader"] = "off"
    return c


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"channels": {k: default_channel(k) for k in CHANNELS},
                "masters": {}, "sends": {}}


def save_state(st):
    # écriture atomique : un crash/écriture concurrente ne doit jamais laisser
    # un JSON tronqué (sinon load_state retombe sur les défauts → état perdu)
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_PATH)


def compile_mix(st):
    """État logique → liste de (instance, valeur) pour les couches Mix 1-2."""
    chans = st["channels"]
    any_solo = any(c["solo"] for c in chans.values())
    cells = []
    for name, geo in CHANNELS.items():
        c = chans.get(name, default_channel(name))
        audible = not c["mute"] and (not any_solo or c["solo"])
        fader, pan = c["fader"], float(c["pan"])
        if not audible or fader == "off":
            vL = vR = 0
        elif len(geo["slots"]) == 1:
            # mono : loi à puissance constante, -3 dB au centre (capture 12)
            theta = (pan + 100) / 200 * math.pi / 2
            vL = db_to_val(float(fader) + 20 * math.log10(max(math.cos(theta), 1e-9)))
            vR = db_to_val(float(fader) + 20 * math.log10(max(math.sin(theta), 1e-9)))
        else:
            # stéréo : balance, 0 dB au centre (capture 20) — loi présumée
            att = 20 * math.log10(max(math.cos(abs(pan) / 100 * math.pi / 2), 1e-9))
            vL = db_to_val(float(fader) + (att if pan > 0 else 0))
            vR = db_to_val(float(fader) + (att if pan < 0 else 0))
        if len(geo["slots"]) == 1:
            s = geo["slots"][0]
            cells += [(s, vL), (STRIDE + s, vR)]       # couche 0 L, couche 1 R
        else:
            sL, sR = geo["slots"]
            cells += [(sL, vL), (STRIDE + sR, vR)]
    return cells


def send_cells(ch, bus, level):
    """Cellules (instance, valeur) d'une route send canal → bus.

    Indépendant du mute/solo du canal (comportement SSL 360 observé :
    le cut n'écrit que les couches mix). Mono : -3 dB centre ; stéréo :
    niveau plein sur chaque côté (capture 19)."""
    slots = CHANNELS[ch]["slots"]
    lL, lR = BUS_LAYERS[bus]
    if level == "off":
        vL = vR = 0
    elif len(slots) == 1:
        v = db_to_val(float(level) + 20 * math.log10(math.cos(math.pi / 4)))
        vL = vR = v
    else:
        vL = vR = db_to_val(float(level))
    if len(slots) == 1:
        s = slots[0]
        return [(STRIDE * lL + s, vL), (STRIDE * lR + s, vR)]
    sL, sR = slots
    return [(STRIDE * lL + sL, vL), (STRIDE * lR + sR, vR)]


def compile_sends(st):
    cells = []
    for ch, buses in st.get("sends", {}).items():
        for bus, level in buses.items():
            cells += send_cells(ch, bus, level)
    return cells


def push_mix(st):
    d = SSL12()
    out = b"".join(msg_gain(1, inst, val) for inst, val in compile_mix(st))
    d.write(out)


def mutate_channel(ch, **updates):
    st = load_state()
    st["channels"].setdefault(ch, default_channel(ch)).update(updates)
    save_state(st)
    push_mix(st)
    return st


# ---------------------------------------------------------------- transport

class SSL12:
    def __init__(self):
        self.dev = usb.core.find(idVendor=VID, idProduct=PID)
        if self.dev is None:
            sys.exit("SSL Control I/F (31e9:0024) introuvable — SSL 12 branchée ?")
        # ⚠️ NE PAS appeler `set_configuration()` sans vérifier d'abord.
        #
        # C'est une requête au niveau du PÉRIPHÉRIQUE : elle réussit même quand
        # quelqu'un d'autre tient l'interface, et elle RÉINITIALISE le device. La
        # SSL 12 ayant un hub interne, la ré-énumération emporte aussi l'interface
        # AUDIO — les bandes perdent leur device JACK et s'arrêtent.
        #
        # Vécu le 17/08/2026 : un `sslctl status` lancé pendant que le démon
        # tournait a coupé le micro en pleine conversation, CINQ fois en 24 s. Et
        # le message affiché était « Accès refusé », donc on croyait qu'il ne
        # s'était rien passé. Un outil qui casse en annonçant qu'il n'a rien fait
        # est pire qu'un outil qui casse.
        try:
            self.dev.get_active_configuration()
        except usb.core.USBError:
            self.dev.set_configuration()      # device pas encore configuré
        try:
            usb.util.claim_interface(self.dev, 0)
        except usb.core.USBError as e:
            sys.exit(f"Interface occupée ou refusée ({e}).\n"
                     "  • le démon Douze la tient ? `systemctl --user stop douze`\n"
                     "  • sinon : règle udev/99-douze.rules, ou lancer avec sudo.")

    def write(self, data):
        self.dev.write(EP_OUT, data, timeout=1000)

    def read_chunks(self, secs):
        end = time.monotonic() + secs
        while time.monotonic() < end:
            try:
                yield bytes(self.dev.read(EP_IN, 512, timeout=200))
            except usb.core.USBTimeoutError:
                continue

    def transact(self, data, secs=0.3):
        self.write(data)
        return list(parse_in_stream(self.read_chunks(secs)))


def show_replies(msgs):
    for op, payload in msgs:
        desc = describe(op, payload)
        line = f"  IN 0x{op:02x} [{payload.hex(' ')}]"
        print(line + (f"  ← {desc}" if desc else ""))


# ---------------------------------------------------------------- commandes

def cmd_info(_a):
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        sys.exit("SSL Control I/F introuvable.")
    print(f"bus {dev.bus} addr {dev.address}  bcdDevice {dev.bcdDevice:#06x}")


def cmd_init(_a):
    """Handshake observé au lancement de SSL 360 (capture 02)."""
    d = SSL12()
    for op in (0x01, 0x02, 0x05, 0x4B, 0x4E):
        print(f"OUT {frame(op).hex(' ')}")
        show_replies(d.transact(frame(op)))


def cmd_listen(a):
    d = SSL12()
    print(f"écoute décodée pendant {a.secs}s (Ctrl-C pour arrêter)…")
    try:
        for op, payload in parse_in_stream(d.read_chunks(a.secs)):
            desc = describe(op, payload)
            if desc == "vumètres" and not a.meters:
                continue
            print(f"IN 0x{op:02x} [{payload.hex(' ')}]" + (f"  ← {desc}" if desc else ""))
    except KeyboardInterrupt:
        pass


def cmd_send(a):
    d = SSL12()
    raw = bytes.fromhex(a.frame.replace(":", " ").replace(",", " "))
    print(f"OUT {raw.hex(' ')}")
    show_replies(d.transact(raw, secs=a.secs))


def cmd_master(a):
    st = load_state()
    if a.rel:
        cur = st.get("masters", {}).get(a.bus, 0.0)
        if cur == "off":
            sys.exit(f"master {a.bus} est à off — mettre une valeur absolue d'abord")
        gain = min(float(cur) + float(a.gain), 12.0)
        v = db_to_val(gain)
    else:
        gain = a.gain
        v = db_to_val(gain)
    d = SSL12()
    d.write(msg_gain(9, MASTER_INST[a.bus], v))
    st.setdefault("masters", {})[a.bus] = "off" if v == 0 else float(gain)
    save_state(st)
    print(f"master {a.bus} → {val_to_db(v)}")


def cmd_channel(a):
    slot = ANALOGUE_SLOT[a.ch]
    lL, lR = BUS_LAYERS[a.bus]
    if db_to_val(a.gain) == 0:
        vL = vR = 0
    else:
        # loi de pan à puissance constante (-3 dB au centre), pan ∈ [-100, 100]
        theta = (a.pan + 100) / 200 * math.pi / 2
        fader = float(a.gain)
        vL = db_to_val(fader + 20 * math.log10(math.cos(theta) or 1e-9))
        vR = db_to_val(fader + 20 * math.log10(math.sin(theta) or 1e-9))
    d = SSL12()
    d.write(msg_gain(1, STRIDE * lL + slot, vL) + msg_gain(1, STRIDE * lR + slot, vR))
    print(f"canal {a.ch} ({a.bus}) → L {val_to_db(vL)} / R {val_to_db(vR)}")


def cmd_cell(a):
    v = db_to_val(a.gain)
    d = SSL12()
    d.write(msg_gain(1, STRIDE * a.layer + a.slot, v))
    print(f"cellule couche {a.layer} slot {a.slot} → {val_to_db(v)}")


def cmd_bool(name):
    def run(a):
        on = a.state == "on"
        d = SSL12()
        out = msg_bool(BOOL_CTRL[name], 0, on)
        if name in LED_GROUP:                 # parité SSL 360 : le host gère la LED
            out += msg_led(LED_GROUP[name], on)
        show_replies(d.transact(out))
        print(f"{name} → {a.state}")
    return run


def cmd_chan_bool(name):
    def run(a):
        d = SSL12()
        show_replies(d.transact(msg_bool(BOOL_CTRL[name], a.ch - 1, a.state == "on")))
        print(f"{name} canal {a.ch} → {a.state}")
    return run


def _fmt_state(ctrl, inst, val):
    if ctrl == 0x0B:
        src = next((k for k, v in LOOPBACK.items() if v == val), val)
        return f"loopback source        : {src}"
    return f"ctrl 0x{ctrl:02x} inst {inst}      : {val}"


def cmd_status(_a):
    """Lit les paramètres interrogeables (requêtes sub 03 observées capture 02).

    L'état du mixer (faders/pans/mutes) n'est PAS lisible : le device ne le
    stocke pas, il vit côté host (cf. PROTOCOL.md). NB : l'espace des gets est
    son propre registre (0x0b=loopback, 0x0c=horloge, 0x0a=blocs par bus…)."""
    d = SSL12()
    queries = [(0x0B, 0), (0x0C, 0), (0x0A, 0), (0x0A, 4), (0x0A, 6),
               (0x0D, 0), (0x0E, 0)]
    for ctrl, inst in queries:
        for op, payload in d.transact(msg_get(ctrl, inst), secs=0.3):
            if op == 0x06 or (op == 0x6C and payload[:1] == b"\x09"):
                continue
            if op == 0x6C and len(payload) >= 7 and payload[0] == 0x05:
                c = int.from_bytes(payload[2:4], "little")
                i = int.from_bytes(payload[4:6], "little")
                print(_fmt_state(c, i, payload[6]))
            elif op == 0x6C and len(payload) >= 6:
                desc = describe(op, payload)
                c = int.from_bytes(payload[2:4], "little")
                i = int.from_bytes(payload[4:6], "little")
                print(desc or f"ctrl 0x{c:02x} inst {i} (sub {payload[0]:#04x}) : "
                              f"{payload[6:].hex(' ')}")
    print("(faders/pans/mutes : état host-side — voir `sslctl show`)")


def cmd_get(a):
    d = SSL12()
    show_replies(d.transact(msg_get(a.ctrl, a.inst), secs=0.4))


def cmd_fader(a):
    g = a.gain if str(a.gain).lower() in ("off", "-inf") else float(a.gain)
    mutate_channel(a.ch, fader=g)
    print(f"fader {a.ch} → {g}")


def cmd_pan(a):
    mutate_channel(a.ch, pan=a.pos)
    print(f"pan {a.ch} → {a.pos:+.0f}")


def cmd_mute(a):
    mutate_channel(a.ch, mute=a.state == "on")
    print(f"mute {a.ch} → {a.state}")


def cmd_solo(a):
    st = mutate_channel(a.ch, solo=a.state == "on")
    actives = [k for k, c in st["channels"].items() if c["solo"]]
    print(f"solo {a.ch} → {a.state}" + (f" (solos actifs : {actives})" if actives else ""))


def cmd_show(_a):
    st = load_state()
    any_solo = any(c["solo"] for c in st["channels"].values())
    print(f"{'canal':6} {'fader':>7} {'pan':>5}  flags")
    for name in CHANNELS:
        c = st["channels"].get(name, default_channel(name))
        audible = not c["mute"] and (not any_solo or c["solo"])
        flags = ("M" if c["mute"] else "-") + ("S" if c["solo"] else "-") \
            + ("" if audible else "  (inaudible)")
        f = c["fader"] if c["fader"] == "off" else f"{float(c['fader']):+.1f}"
        print(f"{name:6} {f:>7} {float(c['pan']):+5.0f}  {flags}")
    for ch, buses in sorted(st.get("sends", {}).items()):
        for bus, level in buses.items():
            print(f"route {ch} → {bus} : {level}")
    for bus, g in st.get("masters", {}).items():
        print(f"master {bus} : {g}")


def cmd_route(a):
    st = load_state()
    if a.level == "off" and a.forget:
        st.get("sends", {}).get(a.ch, {}).pop(a.bus, None)
    else:
        st.setdefault("sends", {}).setdefault(a.ch, {})[a.bus] = \
            a.level if a.level == "off" else float(a.level)
    save_state(st)
    d = SSL12()
    d.write(b"".join(msg_gain(1, i, v) for i, v in send_cells(a.ch, a.bus, a.level)))
    print(f"route {a.ch} → {a.bus} : {a.level}")


def cmd_sync(_a):
    st = load_state()
    out = b"".join(msg_gain(1, i, v) for i, v in compile_mix(st))
    out += b"".join(msg_gain(1, i, v) for i, v in compile_sends(st))
    for bus, g in st.get("masters", {}).items():
        out += msg_gain(9, MASTER_INST[bus], db_to_val(g))
    SSL12().write(out)
    print("état complet renvoyé au device (mix + routes + masters)")


BUS_MODE_CTRL = {"mono": 2, "cut": 4, "follow": 7}   # sub 07 (capture 20)


def cmd_bus(a):
    d = SSL12()
    d.write(msg_bool(BUS_MODE_CTRL[a.mode], MASTER_INST[a.bus], a.state == "on",
                     sub=0x07))
    print(f"bus {a.bus} {a.mode} → {a.state}")


def cmd_loopback(a):
    d = SSL12()
    d.write(msg_enum(11, 0, LOOPBACK[a.source]))
    print(f"loopback → {a.source}")


def cmd_user(a):
    d = SSL12()
    d.write(msg_enum(12, a.button - 1, USER_FN[a.fn]))
    print(f"bouton USER {a.button} → {a.fn}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info").set_defaults(fn=cmd_info)
    sub.add_parser("init").set_defaults(fn=cmd_init)

    p = sub.add_parser("listen")
    p.add_argument("--secs", type=float, default=30)
    p.add_argument("--meters", action="store_true", help="afficher aussi les vumètres")
    p.set_defaults(fn=cmd_listen)

    p = sub.add_parser("send")
    p.add_argument("frame")
    p.add_argument("--secs", type=float, default=1.0)
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("master", help="fader master de bus")
    p.add_argument("bus", choices=MASTER_INST)
    p.add_argument("gain", help="dB (-inf → +12) ou 'off' ; avec --rel : delta en dB")
    p.add_argument("--rel", action="store_true",
                   help="ajustement relatif (pour raccourcis clavier)")
    p.set_defaults(fn=cmd_master)

    p = sub.add_parser("channel", help="volume+pan d'un canal Analogue")
    p.add_argument("ch", type=int, choices=ANALOGUE_SLOT)
    p.add_argument("gain", help="dB ou 'off'")
    p.add_argument("--pan", type=float, default=0, help="-100 (G) … 100 (D)")
    p.add_argument("--bus", choices=BUS_LAYERS, default="mix")
    p.set_defaults(fn=cmd_channel)

    p = sub.add_parser("cell", help="écriture brute d'une cellule de la matrice")
    p.add_argument("layer", type=int)
    p.add_argument("slot", type=int)
    p.add_argument("gain")
    p.set_defaults(fn=cmd_cell)

    for name in ("dim", "cut", "mono", "invert-l", "alt", "talk"):
        p = sub.add_parser(name, help=f"{name} monitoring on/off")
        p.add_argument("state", choices=("on", "off"))
        p.set_defaults(fn=cmd_bool(name))

    for name in ("48v", "hpf", "inst", "phase"):
        p = sub.add_parser(name, help=f"{name} par canal on/off")
        p.add_argument("ch", type=int, choices=(1, 2, 3, 4))
        p.add_argument("state", choices=("on", "off"))
        p.set_defaults(fn=cmd_chan_bool(name))

    sub.add_parser("status", help="lit les paramètres interrogeables").set_defaults(fn=cmd_status)
    sub.add_parser("show", help="état du mixer logique (fichier local)").set_defaults(fn=cmd_show)
    sub.add_parser("sync", help="renvoie tout l'état logique au device").set_defaults(fn=cmd_sync)

    p = sub.add_parser("fader", help="fader d'un canal du mixer logique")
    p.add_argument("ch", choices=CHANNELS)
    p.add_argument("gain", help="dB ou 'off'")
    p.set_defaults(fn=cmd_fader)

    p = sub.add_parser("pan", help="pan/balance d'un canal")
    p.add_argument("ch", choices=CHANNELS)
    p.add_argument("pos", type=float, help="-100 (G) … 100 (D)")
    p.set_defaults(fn=cmd_pan)

    p = sub.add_parser("route", help="send persistant canal → bus (ex. route pb34 hpa 0)")
    p.add_argument("ch", choices=CHANNELS)
    p.add_argument("bus", choices=SEND_BUSES)
    p.add_argument("level", help="dB ou 'off'")
    p.add_argument("--forget", action="store_true",
                   help="avec off : retire aussi la route de l'état")
    p.set_defaults(fn=cmd_route)

    for nm, fn in (("mute", cmd_mute), ("solo", cmd_solo)):
        p = sub.add_parser(nm, help=f"{nm} d'un canal (émulé host-side)")
        p.add_argument("ch", choices=CHANNELS)
        p.add_argument("state", choices=("on", "off"))
        p.set_defaults(fn=fn)

    p = sub.add_parser("get", help="lecture brute d'un paramètre (sub 03)")
    p.add_argument("ctrl", type=lambda s: int(s, 0))
    p.add_argument("inst", type=lambda s: int(s, 0), nargs="?", default=0)
    p.set_defaults(fn=cmd_get)

    p = sub.add_parser("bus", help="modes d'un bus (follow mix 1-2 / cut / mono)")
    p.add_argument("bus", choices=("hp-a", "hp-b"))
    p.add_argument("mode", choices=BUS_MODE_CTRL)
    p.add_argument("state", choices=("on", "off"))
    p.set_defaults(fn=cmd_bus)

    p = sub.add_parser("loopback")
    p.add_argument("source", choices=LOOPBACK)
    p.set_defaults(fn=cmd_loopback)

    p = sub.add_parser("user", help="assignation des boutons USER")
    p.add_argument("button", type=int, choices=(1, 2, 3))
    p.add_argument("fn", choices=USER_FN)
    p.set_defaults(fn=cmd_user)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
