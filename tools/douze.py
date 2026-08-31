#!/usr/bin/env python3
"""Douze — GUI web locale pour la SSL 12 (branche la maquette sur le device).

    python tools/douze.py            # puis ouvrir http://localhost:1212

Le démon possède la connexion USB (le CLI sslctl ne peut pas parler en même
temps) : handshake, keepalive 150 ms (comme SSL360Core), lecture continue de
l'EP IN (vumètres sub 09, échos/notifications sub 05 — boutons physiques
compris), commandes de la GUI en POST /api, événements en SSE sur /events.
"""

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import usb.core
import usb.util

import douzefx
import sslctl
from sslctl import (BOOL_CTRL, LOOPBACK, MASTER_INST, BUS_LAYERS,
                    USER_BUTTONS, USER_FN, user_msgs, led_group_for,
                    CHANNELS, STRIDE, compile_mix, compile_sends, send_cells,
                    db_to_val, default_channel, frame, load_state, msg_bool,
                    msg_enum, msg_gain, msg_get, msg_led, save_state)

PORT = 1212
HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "douze.html")
PROFILES_DIR = os.path.join(os.path.dirname(sslctl.STATE_PATH), "profiles")

MANIFEST = {
    "name": "Douze", "short_name": "Douze",
    "description": "Mixer SSL 12 (Douze)",
    "start_url": "/", "display": "standalone",
    "background_color": "#211F1C", "theme_color": "#211F1C",
    "icons": [{
        "src": "data:image/svg+xml," + (
            "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 96 96'%3E"
            "%3Crect width='96' height='96' rx='18' fill='%23211F1C'/%3E"
            "%3Crect x='22' y='16' width='6' height='64' rx='3' fill='%233A3733'/%3E"
            "%3Crect x='45' y='16' width='6' height='64' rx='3' fill='%233A3733'/%3E"
            "%3Crect x='68' y='16' width='6' height='64' rx='3' fill='%233A3733'/%3E"
            "%3Crect x='14' y='52' width='22' height='12' rx='4' fill='%236D93E8'/%3E"
            "%3Crect x='37' y='30' width='22' height='12' rx='4' fill='%236D93E8'/%3E"
            "%3Crect x='60' y='42' width='22' height='12' rx='4' fill='%234FBF77'/%3E"
            "%3C/svg%3E"),
        "sizes": "96x96", "type": "image/svg+xml", "purpose": "any"}],
}

# instances des booléens DIR par paire playback (bus de destination fixe)
DIR_DEST = {"pb12": "mix", "pb34": "line34", "pb56": "hpa", "pb78": "hpb"}

# --- auto-réparation des liens PipeWire -------------------------------------
# Les sinks ssl12.pbXX sont en node.dont-reconnect : si le nœud ALSA de la
# SSL est recréé (boot, hot-plug, reset USB), les liens ne reviennent pas
# seuls (panne vécue le 17/08/2026 : carte « suspended », silence total).
#
# ⚠️ Le nom du nœud ALSA porte le NUMÉRO DE SÉRIE de la carte. Il a longtemps été
# écrit en dur ici, ce qui ne pouvait marcher que sur une seule machine au monde.
# On le cherche donc dans le graphe.
PW_LINK = shutil.which("pw-link") or "/run/current-system/sw/bin/pw-link"
SSL_ALSA_MOTIF = re.compile(
    r"^alsa_output\.usb-Solid_State_Logic_SSL_12_.*\.pro-output-0$")
LOOPBACK_PORTS = [("pb12", 0), ("pb12", 1), ("pb34", 2), ("pb34", 3),
                  ("pb56", 4), ("pb56", 5), ("pb78", 6), ("pb78", 7)]


def trouver_noeud_ssl():
    """Nom du nœud ALSA de la SSL 12 en profil Pro Audio, ou None.

    Le profil compte : seul « pro-output-0 » expose les 8 canaux de lecture. Une
    carte laissée en profil duplex stéréo n'a pas ces ports du tout, et c'est un
    cas qu'on veut pouvoir NOMMER plutôt que d'échouer en silence."""
    try:
        sortie = subprocess.run([PW_LINK, "-o"], capture_output=True,
                                text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for ligne in sortie.splitlines():
        nom = ligne.strip().split(":", 1)[0]
        if SSL_ALSA_MOTIF.match(nom):
            return nom
    return None


def ensure_pw_links():
    """Recrée les liens loopback → carte (idempotent, réessaie 60 s)."""
    deadline = time.time() + 60
    prevenu = False
    while time.time() < deadline:
        cible = trouver_noeud_ssl()
        if cible is None:
            # La carte peut arriver après le démon (branchement à chaud) : on
            # réessaie. Mais on le dit une fois, sinon un profil mal réglé
            # ressemble à une panne inexplicable.
            if not prevenu:
                prevenu = True
                print("carte non trouvée dans le graphe (profil « Pro Audio » "
                      "sélectionné ?) — nouvelle tentative", flush=True)
            time.sleep(2)
            continue

        missing = 0
        for name, ch in LOOPBACK_PORTS:
            r = subprocess.run(
                [PW_LINK, f"ssl12.{name}.out:output_AUX{ch}",
                 f"{cible}:playback_AUX{ch}"],
                capture_output=True, text=True)
            if r.returncode != 0 and "exists" not in (r.stderr or ""):
                missing += 1
        if missing == 0:
            return
        time.sleep(2)


class Device(threading.Thread):
    """Possède l'USB : écritures sérialisées, lecture continue, keepalive."""

    def __init__(self, bus):
        super().__init__(daemon=True)
        self.bus = bus          # EventBus
        self.lock = threading.Lock()
        self.dev = usb.core.find(idVendor=sslctl.VID, idProduct=sslctl.PID)
        if self.dev is None:
            raise SystemExit("SSL Control I/F introuvable — SSL 12 branchée ?")
        self.dev.set_configuration()
        usb.util.claim_interface(self.dev, 0)
        self.buf = bytearray()
        self.seq = 0
        self.stats = {"reads": 0, "bytes": 0, "frames": 0, "ops": {}, "err": None}
        self.last_push = 0.0
        self.clock = None    # liste de rates lue via get 0x0c (sub 0x11)

    def write(self, data):
        with self.lock:
            self.dev.write(sslctl.EP_OUT, data, timeout=1000)

    def handshake(self):
        for op in (0x01, 0x02, 0x05, 0x4B, 0x4E):   # capture 02
            self.write(frame(op))
            time.sleep(0.02)
        time.sleep(0.2)
        self.write(frame(0x05))   # 2e ff 05 = démarre le flux vumètres (observé)
        self.write(msg_get(0x0C, 0))   # bloc horloge/sample rates

    def keepalive(self):
        while True:
            self.write(frame(0x1B, bytes([self.seq])))
            self.seq = (self.seq + 1) % 4
            time.sleep(0.15)

    def run(self):
        threading.Thread(target=self.keepalive, daemon=True).start()
        while True:
            try:
                raw = bytes(self.dev.read(sslctl.EP_IN, 512, timeout=200))
            except usb.core.USBTimeoutError:
                continue
            except usb.core.USBError as e:
                # hot-plug : on quitte, systemd relance (retry 10 s) et le
                # nouveau démon restaure l'état sur le device revenu
                self.stats["err"] = str(e)
                self.bus.push({"ev": "disconnected"})
                time.sleep(0.5)
                os._exit(1)
            self.stats["reads"] += 1
            self.stats["bytes"] += len(raw)
            for off in range(0, len(raw), 64):
                c = raw[off:off + 64]
                if len(c) >= 2 and c[0] == 0x31:
                    self.buf += c[2:]
            for op, payload in self.drain_frames():
                self.stats["frames"] += 1
                key = f"{op:#04x}/{payload[0]:#04x}" if op == 0x6C and payload else f"{op:#04x}"
                self.stats["ops"][key] = self.stats["ops"].get(key, 0) + 1
                self.dispatch(op, payload)

    def drain_frames(self):
        b = self.buf
        while len(b) >= 4:
            if b[0] != 0xFF:
                del b[0]
                continue
            ln = b[2]
            end = 4 + ln
            if len(b) < end:
                break
            op, payload = b[1], bytes(b[3:end - 1])
            if (op + ln + sum(payload)) & 0xFF == b[end - 1]:
                del b[:end]
                yield op, payload
            else:
                del b[0]

    def dispatch(self, op, payload):
        if op != 0x6C or len(payload) < 6:
            return
        sub = payload[0]
        if sub == 0x09 and len(payload) >= 8:
            # vumètres : `09 00 01 00 00 00 | count u16 | count × u16`
            now = time.monotonic()
            if now - self.last_push < 0.06:            # ~15 Hz vers la GUI
                return
            self.last_push = now
            n = int.from_bytes(payload[6:8], "little")
            vals = [int.from_bytes(payload[8 + 2 * i:10 + 2 * i], "little")
                    for i in range(min(n, (len(payload) - 8) // 2))]
            self.bus.push({"ev": "meters", "v": vals})
        elif sub == 0x11:
            self.clock = [int.from_bytes(payload[6 + 4 * i:10 + 4 * i], "little")
                          for i in range((len(payload) - 8) // 4)]
            self.bus.push({"ev": "clock", "rates": self.clock})
        elif sub == 0x05:
            # écho/notification d'état booléen (GUI *et* boutons physiques)
            ctrl = int.from_bytes(payload[2:4], "little")
            inst = int.from_bytes(payload[4:6], "little")
            val = bool(payload[6])
            name = next((k for k, v in BOOL_CTRL.items() if v == ctrl), None)
            if name:
              with STATE_LOCK:
                st = load_state()
                if inst == 0 and name in ("dim", "cut", "mono", "invert-l",
                                          "alt", "talk"):
                    st.setdefault("monitoring", {})[name] = val
                    # boutons physiques : le host doit rallumer la LED (17b) —
                    # celle du bouton qui porte cette fonction, pas celle qui
                    # porte ce nom (elles diffèrent dès qu'on réassigne)
                    grp = led_group_for(st, name)
                    if grp is not None:
                        self.write(msg_led(grp, val))
                else:
                    st.setdefault("preamp", {}).setdefault(name, {})[str(inst)] = val
                save_state(st)
                self.bus.push({"ev": "bool", "name": name, "inst": inst, "on": val})


def push_full_state(dev):
    """Renvoie tout l'état sauvegardé au device (qui perd tout à l'extinction —
    c'est le host qui possède l'état, comme avec SSL 360)."""
    st = load_state()
    out = b"".join(msg_gain(1, i, v) for i, v in compile_mix(st))
    out += b"".join(msg_gain(1, i, v) for i, v in compile_sends(st))
    for bus, g in st.get("masters", {}).items():
        out += msg_gain(9, MASTER_INST[bus], db_to_val(g))
    for name, on in st.get("monitoring", {}).items():
        if name in BOOL_CTRL and name != "talk":   # talk = momentané, pas restauré
            out += msg_bool(BOOL_CTRL[name], 0, on)
            grp = led_group_for(st, name)
            if grp is not None:
                out += msg_led(grp, on)
    for name, insts in st.get("preamp", {}).items():
        if name in BOOL_CTRL:
            for inst, on in insts.items():
                out += msg_bool(BOOL_CTRL[name], int(inst), on)
    if st.get("loopback"):
        out += msg_enum(11, 0, LOOPBACK[st["loopback"]])
    out += user_msgs(st)
    lv = st.get("monitoring_levels", {})
    if "dimlevel" in lv:
        out += msg_gain(3, 0, db_to_val(lv["dimlevel"]))
    if "alttrim" in lv:
        out += msg_gain(6, 2, db_to_val(lv["alttrim"]))
    dev.write(out)


class EventBus:
    def __init__(self):
        self.clients = []
        self.lock = threading.Lock()

    def push(self, obj):
        data = json.dumps(obj)
        with self.lock:
            for q in self.clients:
                if q.qsize() < 50:
                    q.put(data)

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.clients.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def has_clients(self):
        with self.lock:
            return bool(self.clients)


def fx_broadcast_loop():
    """Diffuse l'état des bandes sur le bus d'événements, 2,5 fois par seconde.

    Avant, CHAQUE page ouverte interrogeait `/fx` toutes les 400 ms, ce qui
    faisait interroger l'API de chaque bande autant de fois — et l'API d'une bande
    lit sa chaîne, donc prend le verrou du rack que le thread audio essaie
    d'acquérir. Deux pages ouvertes = deux fois la sollicitation, pour la même
    information. Ici on interroge UNE fois et on pousse à tout le monde.

    Rien n'est interrogé si personne n'écoute : une page fermée (ou en veille,
    qui ferme son flux) ne coûte plus rien du tout."""
    while True:
        time.sleep(0.4)
        if not BUS.has_clients():
            continue
        try:
            BUS.push({"ev": "fx", "strips": FX.list()})
        except Exception:
            pass                      # jamais fatal : la GUI se rattrapera


BUS = EventBus()
DEV = None
STATE_LOCK = threading.Lock()   # sérialise les load-modify-save de state.json
FX = douzefx.Supervisor()       # bandes d'effets (processus Douze FX)
SCAN = douzefx.Scanner()        # catalogue de plugins (scan hors-process)


# --- bandes d'effets ---------------------------------------------------------
#
# Le superviseur (douzefx.py) ne touche jamais à l'USB : c'est ici, seul endroit
# qui possède le device, qu'on coupe le monitoring direct des canaux traités —
# sinon on s'entend deux fois, sec et traité.

def _fx_direct(strip, cut):
    """Coupe (ou rétablit) le monitoring direct des canaux d'une bande.

    On MÉMORISE l'état précédent : si un canal était déjà coupé à la main, on ne
    le rallume pas en arrêtant la bande."""
    for ch in strip.cfg.get("cut_direct", []):
        if cut:
            before = load_state()["channels"].get(ch, {}).get("mute", False)
            strip.prev_mute[ch] = before
            if not before:
                apply_cmd({"cmd": "mute", "ch": ch, "on": True})
        else:
            if not strip.prev_mute.pop(ch, False):
                apply_cmd({"cmd": "mute", "ch": ch, "on": False})

    # Écrit sur disque : sans ça un redémarrage du démon perdait la mémoire et
    # l'arrêt suivant de la bande rallumait une écoute directe que l'utilisateur
    # avait coupée lui-même.
    strip.save_prev_mute()


def fx_cmd(c):
    """Commandes de bandes venues de la GUI (POST /fx)."""
    kind = c.get("cmd")

    if kind == "list":
        return {"strips": FX.list()}

    if kind == "reload":
        FX.reload()
        return {"strips": FX.list()}

    if kind == "points":
        return douzefx.insertion_points()

    if kind == "graph":
        return douzefx.graph_settings()

    if kind == "set_graph":
        # Réglage GLOBAL du graphe : toutes les applis audio suivent.
        douzefx.set_graph(quantum=c.get("quantum"), rate=c.get("rate"))
        return douzefx.graph_settings()

    if kind == "add":
        sid = FX.add_strip(c.get("strip") or {})
        return {"id": sid, "strips": FX.list()}

    if kind == "remove":
        strip = FX.get(c.get("id"))
        if strip is not None:
            _fx_direct(strip, False)          # ne pas laisser un canal muet derrière soi
        return {"removed": FX.remove_strip(c.get("id")), "strips": FX.list()}

    if kind == "update":
        strip, patch = FX.get(c.get("id")), c.get("patch") or {}
        # `cut_direct` est le seul réglage d'une bande qui touche l'USB, donc il
        # se règle ICI. Si la liste change pendant que la bande tourne, il faut
        # RENDRE son écoute directe à l'ancien canal avant de couper le nouveau —
        # sinon on laisse derrière soi un canal muet que plus rien ne rallume.
        recut = (strip is not None and strip.alive()
                 and "cut_direct" in patch
                 and patch["cut_direct"] != strip.cfg.get("cut_direct"))
        if recut:
            _fx_direct(strip, False)
        res = FX.update_strip(c.get("id"), patch)
        if recut:
            # `reload` garde l'objet Strip et remplace son `cfg` : c'est bien la
            # NOUVELLE liste de canaux qu'on coupe ici.
            _fx_direct(FX.get(c.get("id")) or strip, True)
        return {**res, "strips": FX.list()}

    # --- profils de bandes ---------------------------------------------------
    if kind == "profiles":
        return {"profiles": douzefx.list_profiles(), "profile": FX.profil_courant()}

    if kind == "profile_save":
        n = FX.save_profile(c.get("name", ""))
        return {"saved": c.get("name"), "bandes": n,
                "profiles": douzefx.list_profiles(), "profile": FX.profil_courant()}

    if kind == "profile_delete":
        douzefx.delete_profile(c.get("name", ""))
        return {"profiles": douzefx.list_profiles(), "profile": FX.profil_courant()}

    if kind == "profile_load":
        # Arrêt de TOUT avant la bascule, par le chemin d'arrêt normal : c'est lui
        # qui rend son écoute directe à chaque canal traité. Une bande du profil
        # précédent qui survivrait tiendrait en plus son port et ses nœuds.
        for s in list(FX.strips.values()):
            if s.alive():
                fx_cmd({"cmd": "stop", "id": s.id})
        FX.load_profile(c.get("name", ""))
        # Relance EN FOND, par le même chemin que le démarrage du démon : une
        # chaîne yabridge met des secondes à s'instancier, la requête ne doit pas
        # attendre. La GUI verra les bandes arriver sur le flux d'événements.
        FX.autostart(lambda sid: fx_cmd({"cmd": "start", "id": sid}).get("msg"))
        return {"loaded": c.get("name"), "strips": FX.list(),
                "profiles": douzefx.list_profiles(), "profile": FX.profil_courant()}

    # --- catalogue de plugins ------------------------------------------------
    # Le scan vit ICI et pas dans le moteur : il doit survivre au redémarrage
    # d'une bande, et un plugin qui gèle pendant le scan ne doit surtout pas
    # figer l'audio en cours.
    if kind == "scan":
        ok, msg = SCAN.start(complet=bool(c.get("full")))
        return {"ok": ok, "msg": msg, "scan": SCAN.status()}

    if kind == "scan_status":
        return {"scan": SCAN.status()}

    if kind == "scan_skip":
        ok, msg = SCAN.skip()
        return {"ok": ok, "msg": msg, "scan": SCAN.status()}

    if kind == "scan_stop":
        ok, msg = SCAN.stop()
        return {"ok": ok, "msg": msg, "scan": SCAN.status()}

    if kind == "scan_clear":
        ok, msg = SCAN.clear()
        return {"ok": ok, "msg": msg, "scan": SCAN.status()}

    if kind == "scan_errors":
        return {"errors": SCAN.errors()}

    if kind == "plugins":
        # Catalogue : servi par n'importe quelle bande VIVANTE — une bande
        # arrêtée n'a pas de moteur pour répondre, mais le catalogue est commun.
        for s in FX.strips.values():
            if s.alive():
                out = s.api("/plugins?" + c.get("query", "limit=40"))
                if out is not None:
                    return out
        raise ValueError("aucune bande en marche pour lire le catalogue")

    sid = c.get("id")
    strip = FX.get(sid)
    if strip is None:
        raise ValueError(f"bande inconnue : {sid}")

    if kind == "start":
        ok, msg = FX.start(sid)
        if ok:
            _fx_direct(strip, True)
        return {"ok": ok, "msg": msg, "strips": FX.list()}

    if kind == "stop":
        ok, msg = FX.stop(sid)
        _fx_direct(strip, False)
        return {"ok": ok, "msg": msg, "strips": FX.list()}

    # La chaîne vient de changer : la vue partagée d'avant montrerait encore
    # l'ancienne, et la GUI croirait son clic sans effet.
    if kind == "chain_add":
        ok = strip.chain_add(c.get("path", ""))
        FX.oublier_vue()
        return {"ok": ok, "strips": FX.list()}

    if kind == "chain_move":
        ok = strip.chain_move(int(c.get("from", -1)), int(c.get("to", -1)))
        FX.oublier_vue()
        return {"ok": ok, "strips": FX.list()}

    if kind == "chain_remove":
        ok = strip.chain_remove(int(c.get("index", -1)))
        FX.oublier_vue()
        return {"ok": ok, "strips": FX.list()}

    if kind == "api":
        # Relais vers l'API de la bande : la GUI garde une seule origine et n'a
        # pas à connaître les ports.
        out = strip.api(c.get("path", "/state"), c.get("body"))
        if out is None:
            raise ValueError("bande injoignable")
        return out

    raise ValueError(f"commande fx inconnue : {kind}")


# Clés de RAPPEL (quel profil est chargé, et a-t-il dérivé) : elles vivent dans
# l'état persistant, jamais dans un fichier de profil.
PROFILE_KEYS = ("profile", "profile_dirty")


def _profile_path(name):
    safe = "".join(ch for ch in name if ch.isalnum() or ch in " -_").strip()
    if not safe:
        raise ValueError("nom de profil vide")
    return os.path.join(PROFILES_DIR, safe + ".json")


def list_profiles():
    try:
        return sorted(f[:-5] for f in os.listdir(PROFILES_DIR)
                      if f.endswith(".json"))
    except OSError:
        return []


def apply_cmd(c):
    with STATE_LOCK:
        st = _apply_cmd(c)
    st["_clock"] = DEV.clock
    st["_profiles"] = list_profiles()
    BUS.push({"ev": "state", "state": st})   # sync live de toutes les pages
    return st


def _apply_cmd(c):
    """Applique une commande GUI (mêmes règles que le CLI sslctl)."""
    st = load_state()
    kind = c["cmd"]
    if kind in ("fader", "pan", "mute", "solo"):
        ch = c["ch"]
        upd = {"fader": ("fader", c.get("db")), "pan": ("pan", c.get("pos")),
               "mute": ("mute", c.get("on")), "solo": ("solo", c.get("on"))}[kind]
        st["channels"].setdefault(ch, default_channel(ch))[upd[0]] = upd[1]
        save_state(st)
        DEV.write(b"".join(msg_gain(1, i, v) for i, v in compile_mix(st)))
    elif kind == "route":
        st.setdefault("sends", {}).setdefault(c["ch"], {})[c["bus"]] = c["level"]
        save_state(st)
        DEV.write(b"".join(msg_gain(1, i, v)
                           for i, v in send_cells(c["ch"], c["bus"], c["level"])))
    elif kind == "dir":
        bus, ch = DIR_DEST[c["ch"]], c["ch"]
        st.setdefault("dir", {})[ch] = c["on"]
        if bus == "mix":
            st["channels"].setdefault(ch, default_channel(ch))["fader"] = \
                0.0 if c["on"] else "off"
            save_state(st)
            DEV.write(b"".join(msg_gain(1, i, v) for i, v in compile_mix(st)))
        else:
            st.setdefault("sends", {}).setdefault(ch, {})[bus] = \
                0.0 if c["on"] else "off"
            save_state(st)
            DEV.write(b"".join(msg_gain(1, i, v)
                               for i, v in send_cells(ch, bus,
                                                      0.0 if c["on"] else "off")))
    elif kind == "master":
        st.setdefault("masters", {})[c["bus"]] = c["db"]
        save_state(st)
        DEV.write(msg_gain(9, MASTER_INST[c["bus"]], db_to_val(c["db"])))
    elif kind == "mon":
        name, on = c["name"], c["on"]
        out = msg_bool(BOOL_CTRL[name], 0, on)
        grp = led_group_for(st, name)
        if grp is not None:
            out += msg_led(grp, on)
        DEV.write(out)
        st.setdefault("monitoring", {})[name] = on
        save_state(st)
    elif kind == "preamp":
        DEV.write(msg_bool(BOOL_CTRL[c["name"]], c["ch"], c["on"]))
        st.setdefault("preamp", {}).setdefault(c["name"], {})[str(c["ch"])] = c["on"]
        save_state(st)
    elif kind == "loopback":
        DEV.write(msg_enum(11, 0, LOOPBACK[c["source"]]))
        st["loopback"] = c["source"]
        save_state(st)
    elif kind == "user":
        # assignation d'un bouton de façade (CUT / ALT / TALK), page USER de
        # SSL 360 : sub 08, contrôle 12, instance = rang du bouton (capture 23)
        DEV.write(msg_enum(12, USER_BUTTONS.index(c["button"]), USER_FN[c["fn"]]))
        st.setdefault("user_buttons", {})[c["button"]] = c["fn"]
        save_state(st)
    elif kind == "dimlevel":
        DEV.write(msg_gain(3, 0, db_to_val(c["db"])))
        st.setdefault("monitoring_levels", {})["dimlevel"] = c["db"]
        save_state(st)
    elif kind == "alttrim":
        DEV.write(msg_gain(6, 2, db_to_val(c["db"])))
        st.setdefault("monitoring_levels", {})["alttrim"] = c["db"]
        save_state(st)
    elif kind == "profile-save":
        os.makedirs(PROFILES_DIR, exist_ok=True)
        with open(_profile_path(c["name"]), "w") as f:
            # Un profil ne porte PAS de rappel : sinon le fichier prétendrait
            # « je suis le profil X » et le charger sous un autre nom mentirait.
            json.dump({k: v for k, v in st.items() if k not in PROFILE_KEYS},
                      f, indent=2)
        st["profile"], st["profile_dirty"] = c["name"], False
        save_state(st)
    elif kind == "profile-load":
        with open(_profile_path(c["name"])) as f:
            st = json.load(f)
        # Le profil CHARGÉ est mémorisé dans l'état : sans ça, recharger la page
        # de Douze repartait sur « — » alors que la console était bien réglée.
        st["profile"], st["profile_dirty"] = c["name"], False
        save_state(st)
        push_full_state(DEV)
    elif kind == "profile-delete":
        os.remove(_profile_path(c["name"]))
        if st.get("profile") == c["name"]:      # plus de rappel vers un disparu
            st.pop("profile", None)
            st.pop("profile_dirty", None)
            save_state(st)
    elif kind == "raw":
        # debug : trame brute (séquences observées uniquement !)
        DEV.write(bytes.fromhex(c["hex"]))
    else:
        raise ValueError(f"commande inconnue : {kind}")

    # Toute commande qui n'est PAS une opération de profil fait dériver l'état du
    # profil rappelé (y compris la coupure d'écoute directe d'une bande FX). On
    # le marque, sinon la GUI afficherait « Studio » sur des réglages qui n'ont
    # plus rien à voir — un rappel qui ment est pire que pas de rappel.
    final = load_state()

    if (kind not in ("profile-save", "profile-load", "profile-delete")
            and final.get("profile") and not final.get("profile_dirty")):
        final["profile_dirty"] = True
        save_state(final)

    return final


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/":
            with open(HTML, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/state":
            st = load_state()
            st["_clock"] = DEV.clock
            st["_profiles"] = list_profiles()
            self.send_json(st)
        elif self.path == "/manifest.json":
            self.send_json(MANIFEST)
        elif self.path == "/fx":
            self.send_json({"strips": FX.list()})
        elif self.path == "/debug":
            self.send_json({**DEV.stats, "alive": DEV.is_alive(),
                            "buf": len(DEV.buf)})
        elif self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            q = BUS.subscribe()
            try:
                while True:
                    try:
                        data = q.get(timeout=15)
                        self.wfile.write(f"data: {data}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                BUS.unsubscribe(q)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path not in ("/api", "/fx"):
            return self.send_error(404)
        n = int(self.headers.get("Content-Length", 0))
        try:
            cmd = json.loads(self.rfile.read(n))
            if self.path == "/fx":
                self.send_json({"ok": True, **fx_cmd(cmd)})
            else:
                state = apply_cmd(cmd)
                self.send_json({"ok": True, "state": state})
        except Exception as e:  # renvoyé à la GUI, pas de crash serveur
            self.send_json({"ok": False, "error": str(e)}, code=400)

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


def main():
    global DEV
    DEV = Device(BUS)
    DEV.start()          # lire d'abord : le device coupe les vumètres si l'IN stalle
    time.sleep(0.1)
    DEV.handshake()
    time.sleep(0.2)
    push_full_state(DEV)   # le device démarre vierge : restaurer l'état sauvegardé
    threading.Thread(target=ensure_pw_links, daemon=True).start()
    print("état restauré sur le device")
    # Les bandes d'effets sont des enfants du démon : systemd tue tout le cgroup
    # au redémarrage de l'unité, donc elles ne survivent pas. On relance celles
    # marquées `autostart` (par fx_cmd, qui coupe aussi le monitoring direct).
    FX.autostart(lambda sid: fx_cmd({"cmd": "start", "id": sid}).get("msg"))
    threading.Thread(target=fx_broadcast_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    # ARRÊT PROPRE sur SIGTERM, c'est-à-dire sur `systemctl restart`.
    #
    # Sans ça, systemd envoyait SIGTERM à tout le cgroup — dont les moteurs de
    # bandes, qui peuvent BLOQUER plus de 30 s sur le teardown d'un plugin Wine.
    # Le cgroup ne se vidait pas, systemd attendait son TimeoutStopSec entier,
    # puis tuait de force. Vu de l'utilisateur : il clique « redémarrer » et le
    # démon « ne revient jamais » — il revenait après une minute et demie.
    #
    # On demande donc leur arrêt aux bandes NOUS-MÊMES : `/quit` déclenche leur
    # propre garde-fou de sortie (8 s puis `_Exit`), qui sait justement se
    # dépêtrer d'un plugin Wine récalcitrant.
    def arret(signum, _frame):
        print(f"signal {signum} : arrêt des bandes avant de rendre la main…",
              flush=True)
        try:
            FX.stop_all()
        except Exception as e:
            print(f"  arrêt des bandes : {e}", flush=True)
        # ⚠️ `shutdown()` DOIT être appelé depuis un AUTRE fil que celui qui
        # exécute `serve_forever()` — sinon il attend une boucle qui ne peut plus
        # tourner. Or un gestionnaire de signal s'exécute dans le fil PRINCIPAL,
        # c'est-à-dire précisément celui-là. L'appeler directement fige le
        # process (constaté : SIGKILL de systemd 20 s plus tard, et un
        # redémarrage qui a l'air de ne jamais revenir — la panne même qu'on
        # cherchait à corriger).
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, arret)
    signal.signal(signal.SIGINT, arret)

    print(f"Douze prêt → http://localhost:{PORT}  (Ctrl-C pour arrêter)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
