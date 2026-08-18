#!/usr/bin/env python3
"""Superviseur de bandes Douze FX.

Douze FX est un moteur : **une instance = une bande** (une source, une chaîne de
plugins, une destination), pilotable par une API HTTP locale. Ce module est le
superviseur côté Douze : il lance, arrête, câble et interroge les bandes.

Découpage volontaire :
  - Douze FX (C++) ne connaît QUE sa bande ;
  - ce module connaît la LISTE des bandes et le graphe PipeWire ;
  - il ne touche PAS à l'USB : couper le monitoring direct d'un canal reste
    l'affaire de douze.py, seul maître du device (il consomme `cut_direct`).

Config : ~/.config/douze-fx/strips.json
Test en CLI :
    python tools/douzefx.py list
    python tools/douzefx.py start mic
    python tools/douzefx.py state mic
    python tools/douzefx.py stop mic
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as _ET

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME",
                          os.path.expanduser("~/.config")), "douze-fx")
STRIPS_PATH = os.path.join(CONFIG_DIR, "strips.json")
LOG_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME",
                       os.path.expanduser("~/.cache")), "douze-fx")

# Le lanceur pose le libjack de PipeWire et nomme le nœud (cf. le dépôt du moteur).
# Le lanceur vit dans CE dépôt (fx/ a été rapatrié depuis delestor-proto) : on le
# résout relativement à ce fichier plutôt que par un chemin absolu, pour que le
# dépôt reste déplaçable.
RUNNER = os.environ.get("DOUZE_FX_RUNNER", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fx", "tools", "run-douze-fx.sh"))

PORT_BASE = 1213          # 1212 = Douze ; chaque bande prend le suivant
PW_CLI = shutil.which("pw-cli") or "/run/current-system/sw/bin/pw-cli"
PW_DUMP = shutil.which("pw-dump") or "/run/current-system/sw/bin/pw-dump"
PW_LINK = shutil.which("pw-link") or "/run/current-system/sw/bin/pw-link"

# Une chaîne yabridge/Wine peut mettre 30 s à s'instancier : on attend large.
START_TIMEOUT = 90.0

# ----------------------------------------------------- yabridge / NIX_PROFILES
#
# Les .vst3 posés par `yabridgectl sync` ne CONTIENNENT pas yabridge : juste un
# « chainloader » de 100 ko qui va chercher la vraie `libyabridge-vst3.so` à
# l'exécution. Sur NixOS il la cherche dans les répertoires listés par
# $NIX_PROFILES. Variable absente = « Could not find 'libyabridge-vst3.so' », que
# JUCE rapporte en « Unable to load VST-3 plug-in file » — exactement le message
# d'un plugin cassé, et SEULS les plugins Windows tombent (les natifs comme
# RNNoise chargent toujours), ce qui égare complètement le diagnostic.
#
# Or rien ne garantit la variable au démon : systemd --user ne l'a que si la
# session de bureau la lui a importée — et douze.service peut démarrer AVANT —,
# et `nix develop` ne la restitue pas. On la reconstruit donc ici, une fois, pour
# tout ce que ce module lance : les bandes ET le scanner (sinon un scan marque
# tous les plugins Windows comme cassés et pollue durablement le catalogue).
PROFILS_NIX = ("/run/current-system/sw",
               "/etc/profiles/per-user/" + (os.environ.get("USER") or ""),
               os.path.expanduser("~/.nix-profile"),
               "/nix/var/nix/profiles/default")
YABRIDGE_LIB = "lib/libyabridge-vst3.so"


def _reparer_nix_profiles():
    """Complète $NIX_PROFILES si yabridge n'y est pas trouvable. -> ce qu'on a ajouté."""
    for p in os.environ.get("NIX_PROFILES", "").split():
        if os.path.exists(os.path.join(p, YABRIDGE_LIB)):
            return ""                         # déjà bon : on ne touche à rien
    trouves = [p for p in PROFILS_NIX
               if p and os.path.exists(os.path.join(p, YABRIDGE_LIB))]
    if not trouves:
        return ""                             # pas de yabridge : rien à réparer
    os.environ["NIX_PROFILES"] = " ".join(dict.fromkeys(
        os.environ.get("NIX_PROFILES", "").split() + trouves))
    return " ".join(trouves)


_NIX_PROFILES_REPARE = _reparer_nix_profiles()
if _NIX_PROFILES_REPARE:
    print("[fx] NIX_PROFILES ne menait pas à yabridge : ajout de "
          + _NIX_PROFILES_REPARE, flush=True)

# ---------------------------------------------------------------------- scan
#
# Le catalogue de plugins est un fichier XML (`KnownPluginList` de JUCE) que le
# moteur relit quand il change. Le SCAN, lui, se fait ici : un process JETABLE
# par plugin, pour que celui qui gèle ou qui plante n'emporte rien.
#
# Le scanner est celui de Douze FX : `douze_fx --scanone <fichier> <sortie>`, plus
# son repli `--scanshell` (énumération factory-only) pour les shells VST3 — 245
# sous-plugins dans un seul binaire chez Waves, que le scan normal instancie un
# par un jusqu'à faire déborder la pile d'un thread Wine.
#
# Il a été EMPRUNTÉ à Delestor jusqu'au 17/08/2026. C'était intenable pour publier :
# le chemin pointait vers un dépôt personnel non publié, donc chez n'importe qui
# d'autre il n'y avait aucun scanner, donc aucun plugin, donc rien à héberger.
# `DOUZE_SCANNER` permet toujours de pointer ailleurs (l'ancien scanner reste
# compatible : même contrat `<mode> <fichier> <sortie>`).
def _scanner_par_defaut():
    racine = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for cfg in ("RelWithDebInfo", "Release", "Debug"):
        for sous in ("build-fx/fx", "build-fx"):
            c = os.path.join(racine, sous, "douze_fx_artefacts", cfg, "douze_fx")
            if os.path.isfile(c):
                return c
    return os.path.join(racine, "build-fx", "douze_fx_artefacts",
                        "RelWithDebInfo", "douze_fx")


SCANNER = os.environ.get("DOUZE_SCANNER") or _scanner_par_defaut()
CACHE_PATH = os.path.join(LOG_DIR, "plugins.xml")
# Cache de Delestor : sert d'AMORCE au premier scan (jamais réécrit — Delestor
# en reste propriétaire, comme côté moteur).
DELESTOR_CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME",
                              os.path.expanduser("~/.cache")),
                              "delestor", "plugins.xml")
SCAN_ERRORS_PATH = os.path.join(LOG_DIR, "scan-errors.txt")
SCAN_TIMEOUT = 90.0          # au-delà, le plugin est considéré comme figé
SCAN_PERSIST_EVERY = 20      # écriture du cache tous les N fichiers


# ------------------------------------------------ réadoption des applications
#
# Le nœud virtuel d'une bande est DÉTRUIT et RECRÉÉ à chaque démarrage. Les
# applications qui l'avaient choisi gardent leur `target.object` — mais le lien
# réel, lui, est perdu, et le gestionnaire de session les reloge en silence sur le
# périphérique par défaut.
#
# Personne ne se déclare en panne : l'appli joue toujours, la bande tourne
# toujours, le moteur n'a aucun xrun. Seuls les vumètres restent à zéro. Vécu le
# 17/08/2026 sur la bande de retour (Discord relogé sur ssl12.pb12), et c'est
# exactement le scénario que `ensure_pw_links` couvre déjà côté sinks ssl12.pbXX.

def _pw_objects():
    """Photo du graphe PipeWire, ou [] si pw-dump ne répond pas."""
    try:
        out = subprocess.run([PW_DUMP], capture_output=True, text=True,
                             timeout=10).stdout
        return json.loads(out)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def _apparier(a, b):
    """Apparie deux séries de ports. Un mono en face d'un stéréo se DÉDOUBLE :
    apparier bêtement laisserait un canal muet sur un micro virtuel mono."""
    if len(a) == 1 or len(b) == 1:
        return [(x, y) for x in a for y in b]
    return list(zip(a, b))


def _pw_link(*args):
    try:
        return subprocess.run([PW_LINK, *args], capture_output=True,
                              text=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _liens_indesirables(flux, cible_id, cle, autre):
    """Liens de `flux` qui ne vont PAS vers `cible_id`, RELUS dans le graphe.

    Relus, et pas déduits de ce qu'on croit avoir fait : c'est tout l'intérêt."""
    restants = []
    for o in _pw_objects():
        if not str(o.get("type", "")).endswith("Link"):
            continue
        i = o.get("info") or {}
        if i.get(cle) == flux and i.get(autre) != cible_id:
            restants.append(i)
    return restants


def readopt_streams(slug, mic):
    """Rebranche les applications qui VISENT `slug` sans y être reliées.

    `mic` dit de quel côté se trouve le nœud : un micro virtuel DONNE aux applis
    (elles enregistrent), un puits REÇOIT d'elles (elles jouent dedans).

    Tout se fait par IDENTIFIANT DE PORT et jamais par nom : deux flux d'une même
    application portent le MÊME nom de port, donc un `pw-link -d` par nom coupe
    les deux — dont celui qu'on ne visait pas (erreur commise à la main le
    17/08/2026 : le second flux de Discord s'est retrouvé sans aucun lien).

    Renvoie le nombre d'applications rebranchées.
    """
    props = lambda o: (o.get("info") or {}).get("props") or {}
    est = lambda o, quoi: str(o.get("type", "")).endswith(quoi)

    # Le nœud vient d'apparaître : ses ports peuvent suivre d'un souffle.
    for _ in range(6):
        objs = _pw_objects()
        cible = next((o for o in objs
                      if est(o, "Node") and props(o).get("node.name") == slug), None)
        ports = {}
        for o in objs:
            if est(o, "Port"):
                p = props(o)
                ports.setdefault(p.get("node.id"), {}).setdefault(
                    p.get("port.direction"), []).append((p.get("port.id", 0), o["id"]))
        cote = lambda nid, sens: [pid for _, pid in
                                  sorted(ports.get(nid, {}).get(sens, []))]
        if cible is not None and cote(cible["id"], "out" if mic else "in"):
            break
        time.sleep(0.5)
    else:
        return 0

    serial = str(props(cible).get("object.serial", ""))
    ses_ports = cote(cible["id"], "out" if mic else "in")
    liens = [o for o in objs if est(o, "Link")]

    # Côté du FLUX dans un lien, et côté d'en face.
    cle, autre = (("input-node-id", "output-node-id") if mic
                  else ("output-node-id", "input-node-id"))
    classe = "Stream/Input/Audio" if mic else "Stream/Output/Audio"

    n = 0
    for o in objs:
        if not est(o, "Node"):
            continue
        p = props(o)
        if p.get("media.class") != classe:
            continue
        if str(p.get("target.object") or p.get("node.target") or "") not in (slug, serial):
            continue

        flux = o["id"]
        siens = [l for l in liens if (l.get("info") or {}).get(cle) == flux]
        if any((l.get("info") or {}).get(autre) == cible["id"] for l in siens):
            continue                       # déjà branché où il faut

        a, b = ((ses_ports, cote(flux, "in")) if mic
                else (cote(flux, "out"), ses_ports))
        if not a or not b:
            continue

        # On BRANCHE avant de débrancher : un instant de doublon vaut mieux qu'un
        # trou, surtout sur un micro.
        for x, y in _apparier(a, b):
            _pw_link(str(x), str(y))
        for l in siens:
            i = l.get("info") or {}
            _pw_link("-d", str(i.get("output-port-id")), str(i.get("input-port-id")))

        # VÉRIFIER, pas faire confiance. Brancher d'abord évite un trou sur le
        # micro, mais ouvre une fenêtre où l'application reçoit DEUX sources
        # sommées : +6 dB et filtrage en peigne. Si un `pw-link -d` échoue, cette
        # fenêtre ne se referme JAMAIS — et une voix qui sature par intermittence
        # est exactement le genre de symptôme que personne ne relierait à un lien
        # PipeWire resté en trop. On relit donc le graphe et on insiste.
        restants = _liens_indesirables(flux, cible["id"], cle, autre)
        for _ in range(3):
            if not restants:
                break
            for i in restants:
                _pw_link("-d", str(i.get("output-port-id")), str(i.get("input-port-id")))
            time.sleep(0.2)
            restants = _liens_indesirables(flux, cible["id"], cle, autre)

        if restants:
            # On ne peut pas réparer, mais on refuse de le taire : sans ce
            # message, la panne serait muette côté machine et bien audible côté
            # interlocuteur.
            print(f"[fx] ⚠ {slug} : {len(restants)} lien(s) en trop sur le flux "
                  f"{flux} — l'application reçoit DEUX sources (son doublé)",
                  flush=True)
        n += 1
    return n


# Ce que le MOTEUR sait héberger (cf. les JUCE_PLUGINHOST_* de fx/CMakeLists.txt).
# Le catalogue amorcé depuis Delestor contient d'autres formats — 164 LADSPA
# notamment — que Douze FX ne peut pas charger : les proposer dans le picker
# revient à offrir des plugins dont on SAIT qu'ils échoueront. (LADSPA n'a en
# plus aucune sauvegarde d'état, donc un preset de rack ne pourrait pas les
# restaurer, même si on activait le format.)
FORMATS_HEBERGEABLES = ("VST3", "LV2")


def _elaguer_non_hebergeables(racine):
    """Retire du catalogue les formats que le moteur ne sait pas charger."""
    retires = 0
    for p in list(racine.findall("PLUGIN")):
        if (p.get("format") or "") not in FORMATS_HEBERGEABLES:
            racine.remove(p)
            retires += 1
    return retires


def _default_strips():
    return {"strips": [{
        "id": "mic",
        "name": "Micro",
        "source": {"client": "SSL 12 Pro", "channels": [1]},
        "dest": {"kind": "virtualmic", "name": "Douze FX Mic"},
        "rack": os.path.join(CONFIG_DIR, "racks", "mic.json"),
        "block": 256,
        "autostart": False,
        # Canaux du mixer dont le monitoring direct doit être coupé pendant que
        # la bande tourne (sinon on s'entend deux fois, sec et traité).
        "cut_direct": ["1"],
    }]}


def load_strips():
    try:
        with open(STRIPS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return _default_strips()


def save_strips(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = STRIPS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STRIPS_PATH)


# ------------------------------------------------------------------ profils
#
# Un profil = un JEU DE BANDES complet, câblage ET chaînes. « Stream » n'a pas
# les mêmes bandes que « musique » : ce n'est pas un réglage qu'on ajuste, c'est
# une configuration qu'on rappelle en bloc.
#
# Un SEUL fichier par profil, racks embarqués : un profil doit pouvoir se copier,
# s'envoyer et se supprimer d'un geste. Un arbre de fichiers se désynchronise
# (un rack effacé, un profil qui ne charge plus qu'à moitié).
PROFILES_DIR = os.path.join(CONFIG_DIR, "profiles")


def _profile_path(nom):
    sur = "".join(c for c in (nom or "") if c.isalnum() or c in " -_").strip()
    if not sur:
        raise ValueError("nom de profil vide")
    return os.path.join(PROFILES_DIR, sur + ".json")


def list_profiles():
    try:
        return sorted(f[:-5] for f in os.listdir(PROFILES_DIR)
                      if f.endswith(".json"))
    except OSError:
        return []


def delete_profile(nom):
    os.remove(_profile_path(nom))


def _slug(sid):
    """Identifiant sûr : ASCII, minuscules, sans espace.

    Sert de nom de nœud PipeWire, de nom de fichier de rack et de clé d'API :
    « Écoute » deviendrait `douze-fx.écoute`, ingérable dans un pw-link ou une
    URL. On replie donc les accents au lieu de les garder (str.isalnum() les
    accepte, ce qui est le piège)."""
    folded = unicodedata.normalize("NFKD", sid).encode("ascii", "ignore").decode()
    out = "".join(c if c.isalnum() else "_" for c in folded).strip("_").lower()
    return out or "bande"


def _name_from_path(path):
    """Nom de repli quand le rack ne porte pas le nom du plugin.

    Dernier recours seulement : le moteur SAUVE désormais le vrai nom dans le
    rack. Sur un shell VST3 (un binaire, des dizaines de sous-plugins) le chemin
    ne dit rien d'utile — « WaveShell1-VST3 16.7_x64@0xe39a6c6d » au lieu de
    « RDeEsser Stereo » — d'où le nom en clair côté rack."""
    base = os.path.basename(path or "").split("@")[0]
    for ext in (".vst3", ".so", ".clap"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
    return base or "?"


PW_METADATA = shutil.which("pw-metadata") or "/run/current-system/sw/bin/pw-metadata"


def graph_settings():
    """Réglages d'horloge du graphe PipeWire (quantum et taux d'échantillonnage).

    `clock.quantum` / `clock.rate` = ce qui tourne. `clock.force-*` = ce qu'une
    application impose au graphe (0 = rien d'imposé, PipeWire négocie).
    C'est le levier que prennent les DAW : tant qu'un force-quantum est posé,
    toute demande d'une bande est ignorée — d'où des « 256 demandé, 1024 obtenu »
    inexplicables autrement."""
    try:
        out = subprocess.run([PW_METADATA, "-n", "settings"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}

    vals = dict(re.findall(r"key:'([^']+)' value:'([^']*)'", out))
    num = lambda k: int(vals.get(k, "0") or 0)
    rates = [int(r) for r in re.findall(r"\d+", vals.get("clock.allowed-rates", ""))]

    # ⚠️ `clock.quantum` / `clock.rate` sont les valeurs de BASE négociées ; elles
    # ne bougent pas quand une application force le graphe. Ce qui tourne
    # réellement, c'est le forçage quand il existe — sinon l'interface affiche
    # 44,1 kHz alors que tout le monde travaille à 48, et le réglage a l'air de
    # « retomber » tout seul (vécu).
    out = {
        "quantum": num("clock.force-quantum") or num("clock.quantum"),
        "rate": num("clock.force-rate") or num("clock.rate"),
        "base_quantum": num("clock.quantum"),
        "base_rate": num("clock.rate"),
        "force_quantum": num("clock.force-quantum"),
        "force_rate": num("clock.force-rate"),
        "min_quantum": num("clock.min-quantum"),
        "max_quantum": num("clock.max-quantum"),
        "allowed_rates": rates or [num("clock.rate")],
    }
    out.update(_device_period())
    return out


def _device_period():
    """Période ALSA des cartes physiques — le PLANCHER réel de latence.

    En profil Pro Audio, PipeWire suit l'interruption ALSA (`disable-tsched`) :
    descendre le quantum du graphe sous cette période ne gagne rien. Ce réglage
    se change côté WirePlumber, pas ici — on l'affiche pour que le plafond soit
    visible plutôt que mystérieux."""
    try:
        nodes = json.loads(subprocess.run(["pw-dump"], capture_output=True,
                                          text=True, timeout=10).stdout or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}

    for n in nodes:
        props = ((n.get("info") or {}).get("props") or {})
        if props.get("api.alsa.period-size") and props.get("api.alsa.pcm.stream") == "capture":
            return {"device": props.get("api.alsa.card.name", "carte"),
                    "device_period": int(props["api.alsa.period-size"])}
    return {}


def set_graph(quantum=None, rate=None):
    """Impose (ou relâche, avec 0) le quantum et/ou le taux du graphe.

    ⚠️ C'est GLOBAL : toutes les applications audio de la machine suivent.
    Descendre le quantum réduit la latence de tout le monde et augmente la
    charge ; changer le taux fait renégocier chaque nœud."""
    done = {}
    for key, val in (("clock.force-quantum", quantum), ("clock.force-rate", rate)):
        if val is None:
            continue
        try:
            subprocess.run([PW_METADATA, "-n", "settings", "0", key, str(int(val))],
                           capture_output=True, timeout=5)
            done[key] = int(val)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return done


def insertion_points():
    """Points d'insertion RÉELLEMENT présents dans le graphe PipeWire.

    Décision produit : on ne propose que ce qui est branché. « Branché » = le
    nœud existe dans le graphe maintenant — détecter la présence d'un signal
    demanderait de mesurer, ce qu'on ne fait pas ici.

    On renvoie `node.description`, car c'est le nom sous lequel le moteur (JUCE,
    via pipewire-jack) voit les clients — pas `node.name`.
    """
    try:
        raw = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=10)
        nodes = json.loads(raw.stdout or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"sources": [], "destinations": []}

    sources, dests = [], []
    for n in nodes:
        if n.get("type") != "PipeWire:Interface:Node":
            continue
        info = n.get("info") or {}
        props = info.get("props") or {}
        cls = props.get("media.class", "")
        desc = props.get("node.description") or props.get("node.name")
        if not desc:
            continue
        entry = {"client": desc,
                 "node": props.get("node.name"),
                 "hardware": bool(props.get("device.id") is not None)}

        if cls in ("Audio/Source", "Audio/Source/Virtual"):
            entry["channels"] = info.get("n_output_ports", 0)
            sources.append(entry)
        elif cls == "Audio/Sink":
            entry["channels"] = info.get("n_input_ports", 0)
            dests.append(entry)

    key = lambda e: (not e["hardware"], e["client"].lower())
    return {"sources": sorted(sources, key=key),
            "destinations": sorted(dests, key=key)}


def _scan_dirs():
    """Emplacements où chercher des plugins, sans doublon.

    VST3 SEULEMENT. Le CLAP est volontairement exclu : ni le moteur ni le
    scanner ne savent l'héberger (JUCE n'a pas de format CLAP), donc l'inclure
    ne produirait pas des plugins mais 24 fausses erreurs permanentes. Vérifié :
    `--scanone` sur un .clap rend zéro description."""
    dirs = [os.path.expanduser("~/.vst3"),
            "/run/current-system/sw/lib/vst3"]
    dirs += [p for p in os.environ.get("VST3_PATH", "").split(":") if p]
    vus, out = set(), []
    for d in dirs:
        r = os.path.realpath(d)
        if r not in vus and os.path.isdir(r):
            vus.add(r)
            out.append(r)
    return out


def _plugin_files():
    """Bundles de plugins trouvés dans ces emplacements.

    Un plugin VST3 sous Linux est le plus souvent un DOSSIER `X.vst3` : on
    l'ajoute et on NE DESCEND PAS dedans (sinon on scannerait ses ressources
    internes et on trouverait le binaire deux fois)."""
    trouves = []
    for base in _scan_dirs():
        for racine, sous_dossiers, fichiers in os.walk(base):
            garde = []
            for d in sous_dossiers:
                if d.endswith(".vst3"):
                    trouves.append(os.path.join(racine, d))
                else:
                    garde.append(d)
            sous_dossiers[:] = garde
            for f in fichiers:
                if f.endswith(".vst3"):
                    trouves.append(os.path.join(racine, f))
    return sorted(set(trouves))


class Scanner:
    """Scan du parc de plugins, un process jetable par fichier.

    Ce qui compte ici, ce sont les modes de panne : un plugin peut GELER (Wine
    qui n'en finit pas, licence qui attend un serveur) ou PLANTER. Un scan
    in-process les subit tous les deux ; un process par plugin, avec délai
    maximum et repli `--scanshell`, permet de le tuer et de passer au suivant."""

    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.stop_flag = False
        self.skip_flag = False
        self.enfant = None                # process en cours (pour le tuer)
        self.etat = {"running": False, "done": 0, "total": 0, "current": "",
                     "added": 0, "errors": 0, "skipped": 0, "message": ""}
        self._nettoyer_cache_disque()

    def _nettoyer_cache_disque(self):
        """Élague le cache SUR LE DISQUE des formats non hébergeables.

        Indispensable : le catalogue du picker n'est pas servi par ce module mais
        par le MOTEUR, qui lit `plugins.xml` directement. Élaguer seulement en
        mémoire aurait laissé le picker proposer les 164 LADSPA hérités du cache
        de Delestor — des plugins dont on sait qu'ils ne se chargeront pas."""
        try:
            racine = _ET.parse(CACHE_PATH).getroot()
        except Exception:
            return
        if _elaguer_non_hebergeables(racine):
            self._write_cache(racine)

    # ------------------------------------------------------------------ lecture
    def status(self):
        with self.lock:
            out = dict(self.etat)
        out["known"] = self._count_known()
        return out

    def _count_known(self):
        # Même repli que `_load_cache` : avant le premier scan, le catalogue
        # affiché est celui de Delestor, pas un compteur à zéro trompeur.
        for source in (CACHE_PATH, DELESTOR_CACHE):
            try:
                racine = _ET.parse(source).getroot()
            except Exception:
                continue
            _elaguer_non_hebergeables(racine)
            return len(racine.findall("PLUGIN"))
        return 0

    # -------------------------------------------------------------------- cache
    def _load_cache(self):
        """Racine du cache + index des fichiers déjà connus.

        Au premier lancement, on AMORCE avec le cache de Delestor (~1670 entrées
        déjà scannées, yabridge compris). Sans ça la première passe incrémentale
        rescannerait les 1085 fichiers du parc, soit des heures pour redécouvrir
        ce qu'on savait déjà."""
        for source in (CACHE_PATH, DELESTOR_CACHE):
            try:
                racine = _ET.parse(source).getroot()
                break
            except Exception:
                racine = None
        if racine is None:
            racine = _ET.Element("KNOWNPLUGINS")
        _elaguer_non_hebergeables(racine)
        connus = {p.get("file") for p in racine.findall("PLUGIN") if p.get("file")}
        return racine, connus

    def _write_cache(self, racine):
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        _ET.ElementTree(racine).write(tmp, encoding="UTF-8", xml_declaration=True)
        os.replace(tmp, CACHE_PATH)       # atomique : jamais de cache tronqué

    # --------------------------------------------------------------- un fichier
    def _scan_one(self, chemin, mode):
        """Lance le scanner sur UN fichier. Renvoie (descriptions, raison).

        `raison` vaut "" si tout va bien, "frozen" si on a dû le tuer."""
        sortie = os.path.join(LOG_DIR, "scanone.xml")
        args = [SCANNER, mode, chemin, sortie]
        try:
            with self.lock:
                self.enfant = subprocess.Popen(
                    args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            enfant = self.enfant
            try:
                enfant.wait(timeout=SCAN_TIMEOUT)
                raison = ""
            except subprocess.TimeoutExpired:
                enfant.kill()
                enfant.wait(timeout=10)
                raison = "frozen"
        except OSError as e:
            return [], f"scanner introuvable ({e})"
        finally:
            with self.lock:
                self.enfant = None

        descs = []
        try:
            with open(sortie) as f:
                for ligne in f:
                    if ligne.startswith("DESC "):
                        try:
                            descs.append(_ET.fromstring(
                                ligne[5:].strip().split("?>", 1)[-1].strip()))
                        except _ET.ParseError:
                            pass
            os.remove(sortie)
        except OSError:
            pass
        return descs, raison

    # ----------------------------------------------------------------- la passe
    def _run(self, complet):
        racine, connus = self._load_cache()
        erreurs = set()
        try:
            with open(SCAN_ERRORS_PATH) as f:
                erreurs = {l.strip() for l in f if l.strip()}
        except OSError:
            pass

        fichiers = _plugin_files()
        if not complet:
            # Incrémental : on ne rescanne ni les connus, ni les échecs déjà
            # constatés — sinon chaque passe rebutait sur les mêmes gels.
            fichiers = [f for f in fichiers if f not in connus and f not in erreurs]

        with self.lock:
            self.etat.update(running=True, done=0, total=len(fichiers), current="",
                             added=0, errors=0, skipped=0, message="")

        ajoutes = depuis_ecriture = 0

        for i, chemin in enumerate(fichiers):
            with self.lock:
                if self.stop_flag:
                    break
                self.skip_flag = False
                self.etat.update(done=i, current=os.path.basename(chemin))

            descs, raison = self._scan_one(chemin, "--scanone")

            # Deuxième chance en énumération FACTORY-ONLY : c'est le cas des
            # shells Waves, que le scan normal fait déborder.
            if not descs and raison == "frozen":
                descs, raison = self._scan_one(chemin, "--scanshell")

            with self.lock:
                saute = self.skip_flag
            if saute:
                with self.lock:
                    self.etat["skipped"] += 1
                continue

            if descs:
                for d in descs:
                    racine.append(d)
                ajoutes += len(descs)
                erreurs.discard(chemin)
                with self.lock:
                    self.etat["added"] = ajoutes
            else:
                erreurs.add(chemin)
                with self.lock:
                    self.etat["errors"] = len(erreurs)

            depuis_ecriture += 1
            if depuis_ecriture >= SCAN_PERSIST_EVERY:
                depuis_ecriture = 0
                self._write_cache(racine)      # une passe interrompue REPREND
                self._write_errors(erreurs)

        self._write_cache(racine)
        self._write_errors(erreurs)

        with self.lock:
            # Un scan qui n'avait RIEN à faire doit le dire : sinon la GUI montre
            # une barre vide qui apparaît et disparaît, et on croit à une panne.
            if self.etat["total"] == 0:
                bilan = "aucun nouveau plugin"
            else:
                bilan = f"{ajoutes} ajouté(s), {len(erreurs)} en erreur"
            self.etat.update(running=False, current="",
                             done=self.etat["total"], message=bilan)
            self.stop_flag = False
            self.thread = None

    def _write_errors(self, erreurs):
        try:
            os.makedirs(os.path.dirname(SCAN_ERRORS_PATH), exist_ok=True)
            with open(SCAN_ERRORS_PATH, "w") as f:
                f.write("\n".join(sorted(erreurs)) + ("\n" if erreurs else ""))
        except OSError:
            pass

    # ------------------------------------------------------------------ actions
    def start(self, complet=False):
        with self.lock:
            if self.thread is not None:
                return False, "un scan tourne déjà"
            if not os.path.exists(SCANNER):
                return False, f"scanner absent : {SCANNER}"
            # `running` est levé ICI, pas dans le fil : énumérer le parc prend
            # une seconde, et la GUI qui sonde juste après aurait vu
            # « running: false » — donc conclu « scan terminé » avant qu'il ait
            # commencé, et cessé de suivre la progression.
            self.etat.update(running=True, done=0, total=0, current="",
                             added=0, errors=0, skipped=0, message="")
            self.thread = threading.Thread(target=self._run, args=(complet,),
                                           daemon=True)
        self.thread.start()
        return True, "scan lancé" + (" (complet)" if complet else " (nouveaux)")

    def skip(self):
        """Passe le plugin en cours : c'est l'échappatoire quand un plugin gèle
        et qu'on ne veut pas attendre le délai maximum."""
        with self.lock:
            self.skip_flag = True
            enfant = self.enfant
        if enfant is not None:
            enfant.kill()
        return True, "plugin sauté"

    def stop(self):
        with self.lock:
            self.stop_flag = True
            enfant = self.enfant
        if enfant is not None:
            enfant.kill()
        return True, "scan arrêté"

    def clear(self):
        """Vide le cache ET la liste d'erreurs. Un scan complet devient donc
        nécessaire — c'est le geste de dernier recours, pas une routine."""
        with self.lock:
            if self.thread is not None:
                return False, "un scan tourne : arrête-le d'abord"
        for p in (CACHE_PATH, SCAN_ERRORS_PATH):
            try:
                os.remove(p)
            except OSError:
                pass
        return True, "catalogue vidé"

    def errors(self):
        try:
            with open(SCAN_ERRORS_PATH) as f:
                return [l.strip() for l in f if l.strip()]
        except OSError:
            return []


class Strip:
    """Une bande : son process moteur, son micro virtuel, son port d'API."""

    def __init__(self, cfg, index):
        self.cfg = cfg
        self.id = cfg["id"]
        self.port = PORT_BASE + index
        self.node = f"douze-fx.{_slug(self.id)}"
        self.proc = None
        self.vnodes = {}          # nœuds virtuels vivants, par rôle
        # État du monitoring direct AVANT le démarrage de la bande, pour ne pas
        # rallumer à l'arrêt un canal que l'utilisateur avait coupé lui-même.
        # Rempli par douze.py, seul à posséder le device USB.
        #
        # PERSISTÉ sur disque : cette mémoire ne vivait qu'en RAM du démon, donc
        # un redémarrage de Douze l'oubliait — et l'arrêt suivant de la bande
        # rallumait l'écoute directe d'un canal que l'utilisateur avait coupé
        # lui-même (ou la laissait coupée à tort). Un état qu'on promet de
        # restaurer doit survivre au process qui l'a promis.
        self.prev_mute = self._load_prev_mute()
        # Relances automatiques : horodatages, pour ne pas boucler à l'infini sur
        # une chaîne qui tue le moteur à chaque essai (leçon Delestor : un
        # respawn sans frein produit vingt-cinq redémarrages en dix secondes).
        self.restarts = []
        self.give_up = None
        # A-t-elle répondu au moins une fois DEPUIS ce démarrage ? C'est ce qui
        # sépare « elle démarre encore » de « elle ne répond plus » : les deux se
        # présentent pareil de l'extérieur (process vivant, API muette), et les
        # confondre affichait « démarre… » indéfiniment sur une bande figée.
        self.a_repondu = False
        # Santé du chemin audio, indépendante de la vie du process (cf. `sante`).
        self.sante_t = 0.0        # dernier /state pris en compte
        self.audio_vu = False     # a-t-on VU cette bande jouer depuis ce démarrage ?
        self.audio_ko_t = 0.0     # depuis quand elle ne joue plus (0 = elle joue)
        # Avis TRANSITOIRE (« je me suis relancée toute seule, voilà pourquoi »).
        # La GUI ne savait afficher que les échecs : une bande rétablie sans un
        # mot laissait croire qu'il ne s'était rien passé, et la cause — un
        # redémarrage de PipeWire — restait invisible.
        self.avis = None
        self.avis_t = 0.0

    def _note(self, msg):
        self.avis, self.avis_t = msg, time.time()

    # ------------------------------------------- mémoire de l'écoute directe
    def _prev_mute_path(self):
        return os.path.join(LOG_DIR, f"{self.id}.prevmute.json")

    def _load_prev_mute(self):
        try:
            with open(self._prev_mute_path()) as f:
                d = json.load(f)
            return {str(k): bool(v) for k, v in d.items()}
        except (OSError, ValueError, AttributeError):
            return {}

    def save_prev_mute(self):
        """Appelé par douze.py après chaque coupure/rétablissement."""
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            tmp = self._prev_mute_path() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.prev_mute, f)
            os.replace(tmp, self._prev_mute_path())
        except OSError:
            pass

    # ---------------------------------------------------------------- API HTTP
    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def api(self, path, body=None, timeout=90):
        """Appelle l'API de la bande. Renvoie le JSON, ou None si injoignable."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self._url(path), data=data,
            headers={"Content-Type": "application/json"},
            method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def alive(self):
        """Vivante = l'API répond, pas « je possède le process ».

        Le superviseur peut être redémarré (relance du démon Douze) alors que les
        bandes tournent toujours : on les ré-adopte par leur port au lieu de les
        croire mortes et d'en relancer une deuxième sur le même nœud.
        """
        if self.proc is not None and self.proc.poll() is None:
            return True
        return self.api("/state", timeout=1) is not None

    # ------------------------------------------------------------- micro virtuel
    def _start_virtual_node(self, role):
        """Crée un nœud PipeWire virtuel pour cette bande, et renvoie le nom que
        le moteur doit viser (ou None en cas d'échec).

        UN SEUL mécanisme pour les deux besoins — `support.null-audio-sink` — dont
        seule la `media.class` change, et avec elle le côté que voient les applis :

          role="mic"  (DESTINATION) : Audio/Source/Virtual
                       → `input_*` où la bande écrit, `capture_*` où les applis
                         ENREGISTRENT. C'est un micro.
          role="sink" (SOURCE)      : Audio/Sink
                       → `playback_*` où les applis JOUENT, `monitor_*` où la
                         bande lit. C'est un puits : on y envoie Discord, un
                         navigateur, n'importe quoi, et la bande le traite.

        Le nœud vit tant que le process `pw-cli` vit, donc arrêter la bande le
        fait disparaître proprement.
        """
        mic = (role == "mic")
        cfg = self.cfg.get("dest" if mic else "source", {})
        name = cfg.get("name", "Douze FX Mic" if mic else "Douze FX Retour")
        slug = ("douze_fx_" if mic else "douze_fx_in_") + _slug(self.id)

        # Un nœud ORPHELIN peut traîner (moteur tombé, process tué) : l'appli
        # verrait alors deux micros identiques dont un seul est branché, et le
        # moteur se figerait sur ce nom ambigu.
        subprocess.run(["pkill", "-f", f"node.name={slug} "], capture_output=True)
        self._wait_node(slug, present=False, timeout=5)

        # 1 canal = vrai micro mono (ce qu'attend une appli de voix), 2 = stéréo.
        # Un puits reste STÉRÉO : les applis qu'on y envoie le sont.
        ch = 2 if not mic else int(cfg.get("channels", 2))
        chan_map = "[MONO]" if ch == 1 else "[FL,FR]"

        # Sortie du loopback dans un log : sans ça, un échec de création du micro
        # virtuel est muet et on cherche la panne du mauvais côté (vécu).
        os.makedirs(LOG_DIR, exist_ok=True)
        lb_log = open(os.path.join(LOG_DIR, f"{self.id}.{role}.log"), "w")

        # ⚠️ PAS pw-loopback : sur PipeWire 1.6, sa recette « capture=Audio/Sink
        # + playback=Audio/Source/Virtual » crée une source SANS PORT DE SORTIE.
        # Le nœud apparaît bien dans la liste des micros, mais aucune appli ne
        # peut l'enregistrer — vérifié : parecord n'en tire que l'en-tête WAV.
        # Un null-audio-sink déclaré Audio/Source/Virtual expose LES DEUX côtés :
        # input_* (où la bande écrit) et capture_* (où les applis lisent).
        media = "Audio/Source/Virtual" if mic else "Audio/Sink"
        node_props = (f'{{ factory.name=support.null-audio-sink '
                      f'node.name={slug} node.description="{name}" '
                      f'media.class={media} audio.position={chan_map} }}')
        self.vnodes[role] = subprocess.Popen(
            [PW_CLI, "-m", "create-node", "adapter", node_props],
            stdout=lb_log, stderr=lb_log)

        # Le nœud n'existe pas instantanément : le moteur échouerait à s'y
        # connecter s'il démarrait trop tôt. Et s'il n'apparaît JAMAIS, il faut
        # le dire — sinon le moteur se lance pour rien et meurt sur un « No such
        # device » que personne ne relie au loopback.
        if not self._wait_node(slug, present=True, timeout=10):
            self._stop_virtual_nodes()
            return None

        # Le nœud est neuf : les applis qui le visaient sont restées accrochées à
        # l'ancien. Jamais fatal — une bande qui tourne sans son appli vaut mieux
        # qu'une bande qui refuse de démarrer.
        try:
            repris = readopt_streams(slug, mic)
            if repris:
                print(f"[fx] {self.id} : {repris} application(s) rebranchée(s) "
                      f"sur {slug}", flush=True)
        except Exception as e:
            print(f"[fx] {self.id} : réadoption impossible ({e})", flush=True)

        # Le moteur (JUCE via pipewire-jack) désigne les clients par leur
        # DESCRIPTION, pas par node.name.
        return name

    def _wait_node(self, node_name, present, timeout=10):
        """Attend qu'un nœud apparaisse (present=True) ou disparaisse."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                out = subprocess.run(["pw-link", "-i"], capture_output=True,
                                     text=True, timeout=5).stdout
            except (OSError, subprocess.TimeoutExpired):
                out = ""
            if (node_name in out) == present:
                return True
            time.sleep(0.2)
        return False

    def _vnode_perdu(self):
        """Un de nos nœuds virtuels s'est-il évaporé ? Renvoie son rôle, ou None.

        Le nœud n'existe que tant que vit son `pw-cli -m create-node`. Un
        REDÉMARRAGE DE PIPEWIRE tue ce process (« connection error », relais
        brisé) : le nœud disparaît, le process passe zombie — et rien ne le
        remarquait. Le moteur, lui, survit et se déclare « en marche » ; vu de
        Discord, le micro et la destination avaient purement et simplement
        disparu (vécu le 18/08/2026, PipeWire relancé à 21:41).

        `poll()` fait d'une pierre deux coups : il constate la mort ET récolte
        le zombie."""
        for role, proc in self.vnodes.items():
            if proc.poll() is not None:
                return role
        return None

    def _pipewire_pret(self):
        """PipeWire répond-il ? Juste après son redémarrage il y a une fenêtre
        où TOUT échoue. Y brûler le budget de relances condamnerait la bande
        pour une panne déjà finie : on préfère repasser dans 3 s."""
        try:
            r = subprocess.run([PW_CLI, "info", "0"], capture_output=True,
                               timeout=5)
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _stop_virtual_nodes(self):
        for role, proc in list(self.vnodes.items()):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            self.vnodes.pop(role, None)

        # Une bande RÉ-ADOPTÉE par son port n'a pas de poignée sur les `pw-cli`
        # de ses nœuds : ils appartenaient au démon précédent. Sans ce balayage,
        # l'arrêter laissait derrière elle un micro virtuel orphelin — visible
        # dans la liste des micros, choisi par une appli, et relié à rien.
        # (Le démarrage s'en sortait déjà par son propre pkill ; l'ARRÊT, non.)
        for prefixe in ("douze_fx_", "douze_fx_in_"):
            subprocess.run(["pkill", "-f", f"node.name={prefixe}{_slug(self.id)} "],
                           capture_output=True)

    # --------------------------------------------------------------- santé audio
    #
    # Un moteur peut perdre son CLIENT AUDIO sans mourir : au redémarrage de
    # PipeWire, le process reste là, son API répond encore, mais son
    # périphérique JACK ne tourne plus — `sampleRate` retombe à 0 et plus un
    # échantillon ne passe. La bande s'affichait « en marche », et elle était
    # muette. C'est la même panne que `_vnode_perdu`, vue de l'autre bout : une
    # bande sans nœud virtuel (SSL → SSL) n'a que ce symptôme-là.
    SANTE_PERIODE = 15.0     # au plus une lecture /state par bande et par 15 s
    SANTE_GRACE = 8.0        # muette 8 s d'affilée = vraiment tombée

    def sante(self, live):
        """Prend en compte un /state DÉJÀ LU. Appelé par `snapshot`, qui en lit
        un de toute façon : lire /state consomme les crêtes du moteur, donc on
        ne s'en offre pas un deuxième quand la GUI en tire un toutes les 400 ms."""
        if live is None:
            return
        self.a_repondu = True
        if "sampleRate" not in live:      # moteur sans périphérique déclaré
            return
        self.sante_t = time.time()
        if (live.get("sampleRate") or 0) > 0:
            self.audio_vu = True
            self.audio_ko_t = 0.0
        elif self.audio_vu and not self.audio_ko_t:
            self.audio_ko_t = self.sante_t

    def _audio_mort(self):
        # Sans GUI ouverte, personne n'appelle `sante` : on va chercher l'état
        # nous-mêmes, mais rarement — une crête volée toutes les 15 s ne se voit
        # pas, une toutes les 3 s se verrait.
        if time.time() - self.sante_t > self.SANTE_PERIODE:
            self.sante(self.api("/state", timeout=2))
        # `audio_vu` est le garde-fou : tant qu'on ne l'a pas vue JOUER depuis ce
        # démarrage, un sampleRate à 0 veut seulement dire « elle démarre ».
        return bool(self.audio_vu and self.audio_ko_t
                    and time.time() - self.audio_ko_t >= self.SANTE_GRACE)

    # -------------------------------------------------------------- cycle de vie
    def start(self):
        if self.alive():
            return True, "déjà en marche"

        src = self.cfg.get("source", {})
        dest = self.cfg.get("dest", {})

        # Le puits d'abord : le moteur doit le trouver au moment de s'ouvrir.
        if src.get("kind") == "virtualsink":
            in_client = self._start_virtual_node("sink")
            if in_client is None:
                return False, "le puits virtuel n'a pas pu être créé"
            in_ch = "1,2"
        else:
            in_client = src.get("client", "")
            in_ch = ",".join(str(c) for c in src.get("channels", [1, 2]))

        if dest.get("kind") == "virtualmic":
            out_client = self._start_virtual_node("mic")
            if out_client is None:
                return False, "le micro virtuel n'a pas pu être créé"
            out_ch = "1" if int(dest.get("channels", 2)) == 1 else "1,2"
        else:
            out_client = dest.get("client", "")
            out_ch = ",".join(str(c) for c in dest.get("channels", [1, 2]))

        args = [RUNNER,
                "--in", in_client,
                "--in-ch", in_ch,
                "--out", out_client,
                "--out-ch", out_ch]

        rack = self.cfg.get("rack")
        if rack:
            args += ["--rack", os.path.expanduser(rack)]

        # PAS de bloc par bande : sous PipeWire, une bande qui « demande » un
        # quantum tire TOUT le graphe avec elle — ce n'est donc pas un réglage
        # de bande mais un réglage global (cf. l'horloge du graphe dans Douze).
        # Laisser croire le contraire produisait des « 256 demandé / 1024 obtenu »
        # incompréhensibles.
        env = dict(os.environ,
                   DOUZE_FX_NAME=self.node,
                   DOUZE_FX_PORT=str(self.port))

        os.makedirs(LOG_DIR, exist_ok=True)
        # Le log de la session PRÉCÉDENTE est conservé. Sans ça, le redémarrage
        # qui répare une panne effaçait la trace de cette panne : on ne pouvait
        # plus savoir pourquoi les plugins étaient tombés.
        chemin = os.path.join(LOG_DIR, f"{self.id}.log")
        try:
            if os.path.exists(chemin):
                os.replace(chemin, chemin + ".1")
        except OSError:
            pass
        log = open(chemin, "w")
        # stdin sur /dev/null : la console interactive du moteur ne sert pas ici,
        # tout passe par l'API.
        self.proc = subprocess.Popen(args, env=env, stdout=log, stderr=log,
                                     stdin=subprocess.DEVNULL)
        self.a_repondu = False          # nouveau process : elle DÉMARRE, elle ne gèle pas
        self.sante_t = self.audio_ko_t = 0.0
        self.audio_vu = False

        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if not self.alive():
                self._stop_virtual_nodes()
                return False, "le moteur s'est arrêté au démarrage (voir le log)"
            if self.api("/state", timeout=2) is not None:
                return True, "démarrée"
            time.sleep(0.3)

        self.stop()
        return False, "pas de réponse de la bande"

    def stop(self):
        self._teardown()
        self.restarts = []              # arrêt VOULU : on repart d'une ardoise nette
        self.give_up = None
        return True, "arrêtée"

    def _teardown(self):
        """Ferme le moteur et ses nœuds SANS toucher au budget de relances.

        Séparé de `stop()` parce qu'une relance automatique doit fermer la bande
        sans s'absoudre : remettre le compteur à zéro à chaque tour ferait boucler
        indéfiniment une bande qui retombe aussitôt."""
        # /quit d'abord : la bande se ferme proprement (elle a son propre
        # garde-fou de sortie si un plugin Wine bloque le teardown).
        self.api("/quit", body={}, timeout=5)

        if self.proc is not None:
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        else:
            # Bande ré-adoptée : on n'a pas de handle, on attend que le port se taise.
            deadline = time.time() + 15
            while time.time() < deadline and self.api("/state", timeout=1) is not None:
                time.sleep(0.3)

        self.proc = None
        self._stop_virtual_nodes()

    # ------------------------------------------------------------------ watchdog
    RESTART_WINDOW = 120.0              # secondes
    RESTART_MAX = 3                     # au-delà, on renonce et on le dit
    AVIS_TTL = 180.0                    # durée d'affichage d'un avis de relance

    def supervise(self):
        """Relance la bande si elle est tombée sans qu'on l'ait demandé.

        TROIS morts possibles, et une seule était surveillée :

        (1) SON PROCESS meurt. Le moteur se suicide (code 70) quand son thread de
            contrôle est figé par un plugin Wine — typiquement l'ouverture d'un
            éditeur Waves. Sans cette relance, l'utilisateur se retrouvait avec
            une bande vivante mais sourde à l'API : la GUI n'affichait plus ni les
            vrais noms ni les niveaux, et rien ne repartait tout seul.

        (2) SES NŒUDS VIRTUELS s'évaporent, le process survivant (`_vnode_perdu`).

        (3) SON CLIENT AUDIO meurt, le process ET les nœuds survivant
            (`_audio_mort`) — le cas des bandes sans nœud virtuel.

        (2) et (3) sont la même panne, vue des deux bouts : un redémarrage de
        PipeWire. Aucune des deux ne se voyait — la bande se déclarait « en
        marche » pendant que le son ne passait plus nulle part.

        On ne surveille QUE les bandes qu'on a lancées (`proc` non nul) : une
        bande ré-adoptée n'a pas de handle, et une bande arrêtée à la main doit
        rester arrêtée."""
        if self.proc is None:
            return None

        if self.proc.poll() is None:
            # Le process vit : reste à savoir si le SON vit avec lui.
            perdu = self._vnode_perdu()
            if perdu is None and not self._audio_mort():
                return None
            # Panne d'origine EXTERNE : inutile d'attaquer tant que le serveur
            # n'est pas revenu, et surtout pas d'y laisser le budget de relances.
            if not self._pipewire_pret():
                return None
            pourquoi = ("nœud virtuel disparu" if perdu else "plus de client audio")
            pourquoi += " (PipeWire a redémarré ?)"
            # Fermer AVANT de recréer les nœuds : le moteur resterait sinon
            # accroché à des fantômes, et deux nœuds du même nom coexisteraient.
            self._teardown()
        else:
            code = self.proc.returncode
            self.proc = None
            self._stop_virtual_nodes()    # le nœud du micro virtuel doit repartir avec
            pourquoi = ("thread de contrôle figé" if code == 70 else f"code {code}")

        now = time.time()
        self.restarts = [t for t in self.restarts if now - t < self.RESTART_WINDOW]

        if len(self.restarts) >= self.RESTART_MAX:
            self.give_up = (f"arrêtée après {len(self.restarts)} relances "
                            f"({pourquoi}) — voir {self.id}.log")
            return self.give_up

        self.restarts.append(now)
        ok, msg = self.start()
        if ok:
            self._note(f"relancée : {pourquoi}")
            return f"relancée ({pourquoi}) : {msg}"
        return f"relance échouée ({pourquoi}) : {msg}"

    # ------------------------------------------ chaîne (marche ET arrêt)
    #
    # Une bande arrêtée doit rester éditable : son rack n'est qu'un fichier.
    # Sans ça, une bande dont le moteur est tombé (plugin qui emporte le
    # process) devient une impasse — chaîne vide, aucun moyen d'ajouter.

    def _rack_path(self):
        return os.path.expanduser(self.cfg.get("rack") or "")

    def _rack_read(self):
        try:
            with open(self._rack_path()) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"version": 1, "bypass": False, "stages": []}

    def _rack_write(self, rack):
        p = self._rack_path()
        if not p:
            return False
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rack, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        return True

    def chain(self):
        """Étages de la bande : l'état live si elle tourne, sinon le rack."""
        if self.alive():
            live = self.api("/state", timeout=3)
            if live is not None:
                return live.get("stages", [])
        return [{"name": s.get("name") or _name_from_path(s.get("path", "")),
                 "path": s.get("path"), "loaded": False,
                 "bypass": s.get("bypass", False)}
                for s in self._rack_read().get("stages", [])]

    def chain_add(self, path):
        # `alive()` est vrai dès que le PROCESS tourne : pendant les ~10 s de
        # démarrage, l'API ne répond pas encore et `api()` rend None. On tombait
        # alors dans le trou — renvoyer False SANS écrire le rack — et l'ajout
        # disparaissait sans un mot. On retombe donc sur le fichier.
        if self.alive():
            rep = self.api("/chain/add", {"path": path})
            if rep is not None:
                return rep.get("ok", False)

        rack = self._rack_read()
        rack.setdefault("stages", []).append({"path": path, "bypass": False})
        return self._rack_write(rack)

    def chain_move(self, frm, to):
        """Réordonne un étage. L'ordre s'entend : un débruiteur après un
        compresseur travaille sur un plancher de bruit déjà remonté."""
        # Même repli que chain_add : une API muette ne doit pas perdre l'ordre.
        if self.alive():
            rep = self.api("/chain/move", {"from": frm, "to": to})
            if rep is not None:
                return rep.get("ok", False)
        rack = self._rack_read()
        stages = rack.setdefault("stages", [])
        if not (0 <= frm < len(stages) and 0 <= to < len(stages)) or frm == to:
            return False
        stages.insert(to, stages.pop(frm))
        return self._rack_write(rack)

    def chain_remove(self, index):
        if self.alive():
            rep = self.api("/chain/remove", {"index": index})
            if rep is not None:
                return rep.get("ok", False)
        rack = self._rack_read()
        stages = rack.setdefault("stages", [])
        if not 0 <= index < len(stages):
            return False
        stages.pop(index)
        return self._rack_write(rack)

    # ------------------------------------------------------------------- lecture
    def snapshot(self):
        """Config + état live (si la bande tourne)."""
        out = {
            "id": self.id,
            "name": self.cfg.get("name", self.id),
            "port": self.port,
            "node": self.node,
            "running": self.alive(),
            "source": self.cfg.get("source", {}),
            "dest": self.cfg.get("dest", {}),
            "cut_direct": self.cfg.get("cut_direct", []),
            # Publié pour que la GUI puisse l'AFFICHER et le basculer : sans ça le
            # réglage existait dans la config sans que rien ne le montre.
            "autostart": bool(self.cfg.get("autostart")),
        }
        if self.give_up:
            out["problem"] = self.give_up
        # Avis transitoire : la GUI doit pouvoir dire « ça s'est réparé tout
        # seul, et voici pourquoi », pas seulement « ça va » ou « c'est cassé ».
        elif self.avis and time.time() - self.avis_t < self.AVIS_TTL:
            out["notice"] = self.avis

        if self.alive():
            live = self.api("/state", timeout=3)
            self.sante(live)              # gratuit : ce /state est déjà payé
            if live is not None:
                out["live"] = live
                out["chain"] = live.get("stages", [])
                # Le moteur se DÉCLARE figé. C'est le cas le plus courant, et
                # celui qu'on ne pourrait pas deviner : son API répond encore
                # (elle est servie depuis des valeurs en cache), donc le silence
                # ne le trahit pas. Seul l'éditeur natif est perdu.
                if live.get("frozen"):
                    out["frozen"] = True
                    phase = live.get("frozen_phase") or "?"
                    out["problem"] = (f"figée sur « {phase} » — le son continue, "
                                      f"l'éditeur est perdu ; relancer quand tu veux")
            elif self.a_repondu and not self.give_up:
                # Elle a déjà répondu, donc elle ne « démarre » pas : son thread
                # de contrôle est figé (typiquement l'éditeur natif d'un plugin
                # Wine qui ne rend pas la main). Le moteur ne se tue plus dans ce
                # cas — son thread audio, lui, continue de traiter — mais elle
                # n'obéit plus à rien tant qu'on ne la relance pas.
                out["problem"] = ("ne répond plus (le son continue) — "
                                  "à relancer quand ça t'arrange")
                out["frozen"] = True
        # « ready » = l'API répond, pas juste « le process est lancé ». Une bande
        # qui démarre met des secondes à instancier sa chaîne (un shell Waves ~7 s)
        # et pendant ce temps la GUI ne doit pas croire tenir un état à jour.
        out["ready"] = "live" in out

        if "chain" not in out:
            out["chain"] = self.chain()      # bande arrêtée : la chaîne du rack
        return out


class Supervisor:
    """Toutes les bandes. Sérialisé : lancer deux bandes à la fois embrouille
    le câblage PipeWire (les nœuds apparaissent dans le désordre)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.strips = {}
        self.vue_lock = threading.Lock()
        self.vue = None               # dernier `list()`, partagé (cf. VUE_TTL)
        self.vue_t = 0.0
        self.reload()
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        """Relève les bandes tombées. Toutes les 3 s : assez pour que la coupure
        se compte en secondes, assez peu pour ne rien coûter."""
        while True:
            time.sleep(3.0)
            for s in list(self.strips.values()):
                try:
                    # Non bloquant : si une action utilisateur tient le verrou, on
                    # repassera dans 3 s. S'empiler derrière elle ne servirait qu'à
                    # retarder la relance.
                    if not self.lock.acquire(blocking=False):
                        continue
                    try:
                        msg = s.supervise()
                    finally:
                        self.lock.release()
                    if msg:
                        print(f"[fx] {s.id} : {msg}", flush=True)
                except Exception as e:                    # jamais fatal
                    print(f"[fx] surveillance {s.id} : {e}", flush=True)

    def reload(self):
        cfg = load_strips()
        voulues = {s["id"] for s in cfg.get("strips", [])}
        with self.lock:
            for i, s in enumerate(cfg.get("strips", [])):
                if s["id"] in self.strips:
                    self.strips[s["id"]].cfg = s      # garde le process en cours
                else:
                    self.strips[s["id"]] = Strip(s, i)
            # Une bande effacée de strips.json à la main laissait un FANTÔME :
            # plus rien pour la piloter (elle avait disparu de la GUI), mais son
            # process tournait toujours et tenait ses nœuds PipeWire. On la ferme.
            for sid in [k for k in self.strips if k not in voulues]:
                fantome = self.strips.pop(sid)
                if fantome.alive():
                    fantome.stop()
        self.oublier_vue()            # la liste des bandes a changé
        return cfg

    def get(self, sid):
        return self.strips.get(sid)

    # Le moteur REMET SES CRÊTES À ZÉRO à chaque lecture de `/state` : lire, c'est
    # consommer. Tant qu'il n'y avait qu'un lecteur (la boucle de diffusion) ça
    # marchait — mais l'application de bureau interroge `/fx` sur son propre
    # battement, et chacune de ses lectures VOLAIT un pic à la GUI. Symptôme :
    # des vumètres qui ne bougent presque plus, sans que rien ne soit en panne
    # (constaté le 17/08/2026 : 2 valeurs réelles sur 13 dans le flux).
    #
    # D'où une vue PARTAGÉE : le premier arrivé interroge les bandes, les autres
    # relisent son résultat pendant un court instant. La règle « un seul
    # interrogateur » cesse d'être une convention que chaque client doit respecter
    # pour devenir une propriété du démon, quel que soit le nombre de clients.
    #
    # Le TTL est un peu SOUS la période de diffusion (0,4 s) : la diffusion tombe
    # donc toujours sur une lecture fraîche, et tout lecteur supplémentaire se
    # greffe dessus au lieu de s'intercaler.
    VUE_TTL = 0.3

    def list(self, frais=False):
        with self.vue_lock:
            if (not frais and self.vue is not None
                    and time.time() - self.vue_t < self.VUE_TTL):
                return self.vue
        vue = [s.snapshot() for s in self.strips.values()]
        with self.vue_lock:
            self.vue, self.vue_t = vue, time.time()
        return vue

    def oublier_vue(self):
        """À appeler après toute action qui change l'état d'une bande : la vue
        d'avant mentirait (bande arrêtée encore affichée en marche)."""
        with self.vue_lock:
            self.vue = None

    # Le verrou sérialise les démarrages : lancer deux bandes à la fois embrouille
    # le câblage PipeWire. Mais un démarrage peut durer jusqu'à START_TIMEOUT
    # (90 s) — et le fil de surveillance en tient un lui aussi. Un clic de
    # l'utilisateur restait donc bloqué une minute et demie SANS un mot. On attend
    # désormais un temps borné, et on explique quand c'est occupé.
    BUSY_WAIT = 20.0

    def _serialise(self, quoi, action):
        if not self.lock.acquire(timeout=self.BUSY_WAIT):
            return False, f"occupé ({quoi} impossible : une bande démarre ou s'arrête)"
        try:
            return action()
        finally:
            self.oublier_vue()        # l'état d'avant vient de devenir faux
            self.lock.release()

    def start(self, sid):
        s = self.get(sid)
        if s is None:
            return False, f"bande inconnue : {sid}"
        return self._serialise("démarrage", s.start)

    def stop(self, sid):
        s = self.get(sid)
        if s is None:
            return False, f"bande inconnue : {sid}"
        return self._serialise("arrêt", s.stop)

    def stop_all(self):
        for s in self.strips.values():
            if s.alive():
                s.stop()

    def autostart(self, starter=None):
        """Relance les bandes marquées `autostart`.

        Nécessaire parce que les bandes sont des ENFANTS du démon : systemd tue
        tout le cgroup de l'unité à chaque redémarrage, donc elles ne survivent
        pas (la ré-adoption par port ne sauve que le cas d'un démon qui plante
        sans emporter ses enfants). Lancé en fond : instancier une chaîne
        yabridge prend des secondes, le démon ne doit pas attendre pour servir."""
        # `starter` permet à douze.py de passer par SON chemin de démarrage (qui
        # coupe aussi le monitoring direct) plutôt que par self.start seul.
        go = starter or (lambda sid: self.start(sid)[1])

        def run():
            for s in list(self.strips.values()):
                if s.cfg.get("autostart") and not s.alive():
                    try:
                        print(f"[fx] autostart {s.id} : {go(s.id)}", flush=True)
                    except Exception as e:                    # jamais bloquant
                        print(f"[fx] autostart {s.id} : {e}", flush=True)
        threading.Thread(target=run, daemon=True).start()

    # ---------------------------------------------------------------- profils
    def save_profile(self, nom):
        """Fige le jeu de bandes courant : câblage ET chaînes.

        Les bandes EN MARCHE sont priées d'écrire leur chaîne sur disque AVANT la
        photo. Une chaîne modifiée à chaud ne vit qu'en mémoire du moteur : sans
        ça, le profil aurait capturé un rack périmé, et le rappeler aurait rendu
        une configuration que l'utilisateur croyait avoir enregistrée."""
        for s in self.strips.values():
            if s.alive():
                s.api("/preset/save", body={}, timeout=20)

        cfg = load_strips()
        bandes = cfg.get("strips", [])
        racks = {}
        for c in bandes:
            try:
                with open(os.path.expanduser(c.get("rack") or "")) as f:
                    racks[c["id"]] = json.load(f)
            except (OSError, ValueError):
                # Une bande sans rack lisible entre quand même dans le profil, en
                # passthrough : mieux vaut un profil complet avec une chaîne vide
                # qu'un profil amputé d'une bande.
                racks[c["id"]] = {"version": 1, "bypass": False, "stages": []}

        os.makedirs(PROFILES_DIR, exist_ok=True)
        with open(_profile_path(nom), "w") as f:
            json.dump({"strips": bandes, "racks": racks}, f,
                      indent=2, ensure_ascii=False)

        cfg["profile"] = nom
        save_strips(cfg)
        self.oublier_vue()
        return len(bandes)

    def load_profile(self, nom):
        """Remplace le jeu de bandes par celui du profil. Renvoie les ids à lancer.

        ⚠️ L'appelant doit avoir ARRÊTÉ les bandes en marche par son propre chemin
        d'arrêt : c'est lui (douze.py) qui possède l'USB, et chaque bande doit
        rendre son écoute directe avant de disparaître. Le superviseur ne touche
        jamais au mixer."""
        with open(_profile_path(nom)) as f:
            prof = json.load(f)

        bandes = prof.get("strips", [])
        racks = prof.get("racks") or {}
        for c in bandes:
            rack = racks.get(c["id"])
            chemin = os.path.expanduser(c.get("rack") or "")
            if rack is None or not chemin:
                continue
            os.makedirs(os.path.dirname(chemin), exist_ok=True)
            tmp = chemin + ".tmp"
            with open(tmp, "w") as f:
                json.dump(rack, f, indent=2, ensure_ascii=False)
            os.replace(tmp, chemin)

        save_strips({"strips": bandes, "profile": nom})
        self.reload()          # ferme au passage les bandes absentes du profil
        return [c["id"] for c in bandes if c.get("autostart")]

    def profil_courant(self):
        return load_strips().get("profile")

    def _oublier_profil(self, cfg):
        """Toute modification manuelle défait le rappel : garder le nom
        affiché après coup ferait passer une configuration bricolée pour le
        profil enregistré."""
        cfg.pop("profile", None)
        return cfg

    # ------------------------------------------------------------ CRUD de bandes
    def add_strip(self, cfg):
        """Ajoute une bande à la config. `id` est dérivé du nom s'il manque."""
        cfg = dict(cfg)
        sid = _slug(cfg.get("id") or cfg.get("name") or "bande")
        base, i = sid, 2
        existing = {s["id"] for s in load_strips().get("strips", [])}
        while sid in existing:                    # jamais deux bandes du même id
            sid, i = f"{base}{i}", i + 1
        cfg["id"] = sid
        cfg.setdefault("name", sid)
        cfg.setdefault("rack", os.path.join(CONFIG_DIR, "racks", f"{sid}.json"))
        cfg.setdefault("cut_direct", [])

        # Un rack vide dès la création : la bande démarre en passthrough plutôt
        # que de râler sur un fichier absent.
        rack = os.path.expanduser(cfg["rack"])
        if not os.path.exists(rack):
            os.makedirs(os.path.dirname(rack), exist_ok=True)
            with open(rack, "w") as f:
                json.dump({"version": 1, "bypass": False, "stages": []}, f, indent=2)

        all_cfg = load_strips()
        all_cfg.setdefault("strips", []).append(cfg)
        save_strips(self._oublier_profil(all_cfg))
        self.reload()
        return sid

    def remove_strip(self, sid):
        """Retire une bande. On l'arrête d'abord : sinon son process survivrait
        sans plus rien pour le piloter."""
        s = self.get(sid)
        if s is not None and s.alive():
            s.stop()

        all_cfg = load_strips()
        before = len(all_cfg.get("strips", []))
        all_cfg["strips"] = [c for c in all_cfg.get("strips", []) if c["id"] != sid]
        save_strips(self._oublier_profil(all_cfg))
        self.strips.pop(sid, None)
        self.reload()
        return before != len(all_cfg["strips"])

    # Ce qui se pose au LANCEMENT du moteur (cf. `Strip.start`) : le changer sur
    # une bande en marche n'a aucun effet tant qu'elle n'est pas relancée.
    RECABLE_KEYS = ("source", "dest", "rack")

    def update_strip(self, sid, patch):
        """Modifie une bande (nom, source, destination, rack, autostart…).

        Une bande EN MARCHE dont on change le câblage est RELANCÉE. Sans ça le
        changement était accepté, écrit sur disque et affiché — mais l'audio
        continuait de suivre l'ancienne route jusqu'au prochain arrêt manuel :
        un réglage qui « ne marche pas » sans dire pourquoi."""
        all_cfg = load_strips()
        found = recable = False
        for c in all_cfg.get("strips", []):
            if c["id"] == sid:
                recable = any(k in patch and patch[k] != c.get(k)
                              for k in self.RECABLE_KEYS)
                c.update(patch)
                found = True
        if not found:
            return {"updated": False, "restarted": False,
                    "msg": f"bande inconnue : {sid}"}

        save_strips(self._oublier_profil(all_cfg))
        self.reload()

        s = self.get(sid)
        if not (recable and s is not None and s.alive()):
            return {"updated": True, "restarted": False, "msg": "enregistrée"}

        def relance():
            s.stop()
            return s.start()

        # Le verrou du superviseur : un recâblage est un arrêt + un démarrage,
        # il doit se sérialiser avec les autres comme n'importe quel démarrage.
        ok, msg = self._serialise("recâblage", relance)
        return {"updated": True, "restarted": bool(ok), "msg": msg}


# ---------------------------------------------------------------------- CLI
def _main(argv):
    sup = Supervisor()
    cmd = argv[1] if len(argv) > 1 else "list"
    sid = argv[2] if len(argv) > 2 else None

    if cmd == "list":
        for s in sup.list():
            mark = "●" if s["running"] else "○"
            live = s.get("live", {})
            extra = ""
            if live:
                stages = ", ".join(st["name"] for st in live.get("stages", [])) or "vide"
                extra = (f"  bloc {live.get('block')}"
                         f"  latence {live.get('latency')}  [{stages}]")
            print(f"{mark} {s['id']:<10} {s['name']:<16} port {s['port']}{extra}")
    elif cmd in ("start", "stop") and sid:
        ok, msg = getattr(sup, cmd)(sid)
        print(("OK  " if ok else "ÉCHEC  ") + msg)
        if cmd == "start" and ok:
            print(json.dumps(sup.get(sid).snapshot(), indent=2, ensure_ascii=False))
    elif cmd == "state" and sid:
        s = sup.get(sid)
        print(json.dumps(s.snapshot() if s else {"error": "inconnue"},
                         indent=2, ensure_ascii=False))
    elif cmd == "init":
        save_strips(load_strips())
        print(f"écrit : {STRIPS_PATH}")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv))
