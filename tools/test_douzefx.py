#!/usr/bin/env python3
"""Tests du superviseur de bandes et du scan — SANS carte, SANS bande lancée.

    python tools/test_douzefx.py

Ce qui est couvert : la logique pure, celle qu'on casse en corrigeant autre chose.
L'édition d'un rack HORS LIGNE compte parmi les plus utiles : c'est le seul chemin
disponible quand le moteur d'une bande est tombé, donc celui qui doit marcher
justement quand rien d'autre ne marche.

Pas couvert ici (il faut la carte et PipeWire) : le démarrage réel d'une bande, le
câblage du micro virtuel, l'horloge du graphe.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ECHECS = []
TOTAL = 0


def verifie(condition, quoi):
    global TOTAL
    TOTAL += 1
    if condition:
        print(f"  ok   {quoi}")
    else:
        ECHECS.append(quoi)
        print(f"  ECHEC {quoi}")


def test_slug():
    print("[test] identifiants de bande")
    import douzefx
    verifie(douzefx._slug("Écoute") == "ecoute",
            "les accents sont repliés (un nom de nœud PipeWire ne les supporte pas)")
    verifie(douzefx._slug("Micro Perche") == "micro_perche", "les espaces deviennent _")
    verifie(douzefx._slug("") == "bande", "un nom vide a un repli")
    verifie(douzefx._slug("###") == "bande", "un nom sans caractère utile aussi")


def test_nom_depuis_chemin():
    print("[test] nom de repli d'un plugin")
    import douzefx
    verifie(douzefx._name_from_path("/x/KStrip.vst3") == "KStrip", "extension retirée")
    verifie(douzefx._name_from_path("/x/Shell.vst3@0xdeadbeef") == "Shell",
            "suffixe d'UID retiré (shell VST3)")
    verifie(douzefx._name_from_path("") == "?", "chemin vide → repli lisible")


def test_rack_hors_ligne(tmp):
    print("[test] édition du rack bande ARRÊTÉE")
    import douzefx

    rack = os.path.join(tmp, "rack.json")
    with open(rack, "w") as f:
        json.dump({"version": 1, "bypass": False, "stages": []}, f)

    bande = douzefx.Strip({"id": "test", "name": "Test", "rack": rack,
                           "source": {}, "dest": {}}, 90)
    verifie(not bande.alive(), "la bande n'est pas vivante (aucun moteur)")

    verifie(bande.chain_add("/un.vst3"), "ajout hors ligne accepté")
    verifie(bande.chain_add("/deux.vst3"), "deuxième ajout")
    noms = [s["name"] for s in bande.chain()]
    verifie(noms == ["un", "deux"], f"ordre après ajouts : {noms}")

    verifie(bande.chain_move(0, 1), "déplacement accepté")
    noms = [s["name"] for s in bande.chain()]
    verifie(noms == ["deux", "un"], f"ordre après déplacement : {noms}")

    verifie(not bande.chain_move(0, 5), "déplacement hors bornes refusé")
    verifie(not bande.chain_move(0, 0), "déplacement sur place refusé")

    verifie(bande.chain_remove(0), "retrait accepté")
    verifie([s["name"] for s in bande.chain()] == ["un"], "il reste le bon")
    verifie(not bande.chain_remove(3), "retrait hors bornes refusé")

    # Le nom SAUVÉ par le moteur doit primer sur celui déduit du fichier : sur un
    # shell VST3 le chemin ne dit rien d'utile.
    with open(rack, "w") as f:
        json.dump({"stages": [{"path": "/x/Shell.vst3@0x1", "name": "RDeEsser Stereo"}]}, f)
    verifie(bande.chain()[0]["name"] == "RDeEsser Stereo",
            "le nom enregistré prime sur le nom de fichier")


def test_memoire_ecoute_directe(tmp):
    print("[test] mémoire de l'écoute directe (persistée)")
    import douzefx

    douzefx.LOG_DIR = tmp
    cfg = {"id": "mem", "name": "Mem", "rack": "", "source": {}, "dest": {}}

    a = douzefx.Strip(cfg, 91)
    verifie(a.prev_mute == {}, "vide au départ")
    a.prev_mute = {"1": True, "2": False}
    a.save_prev_mute()

    b = douzefx.Strip(cfg, 91)
    verifie(b.prev_mute == {"1": True, "2": False},
            "relue par une NOUVELLE instance (donc survit au redémarrage du démon)")


def test_config_bandes(tmp):
    print("[test] config des bandes : recâblage et fantômes")
    import douzefx

    # On détourne la config vers le dossier jetable : aucun risque de toucher
    # les vraies bandes de la machine.
    douzefx.CONFIG_DIR = tmp
    douzefx.STRIPS_PATH = os.path.join(tmp, "strips.json")
    douzefx.LOG_DIR = tmp
    with open(douzefx.STRIPS_PATH, "w") as f:
        json.dump({"strips": [
            {"id": "a", "name": "A", "rack": os.path.join(tmp, "a.json"),
             "source": {"client": "SSL 12 Pro", "channels": [1]},
             "dest": {"kind": "virtualmic", "name": "A"}, "cut_direct": ["1"]},
            {"id": "b", "name": "B", "rack": os.path.join(tmp, "b.json"),
             "source": {"client": "X", "channels": [1, 2]},
             "dest": {"kind": "jack", "client": "Y", "channels": [1, 2]}},
        ]}, f)

    sup = douzefx.Supervisor()
    verifie(sorted(sup.strips) == ["a", "b"], "les deux bandes sont chargées")

    # Bande à l'ARRÊT : rien à relancer, mais le changement doit être écrit.
    r = sup.update_strip("a", {"source": {"client": "Autre", "channels": [2]}})
    verifie(r["updated"] and not r["restarted"],
            "bande arrêtée : enregistrée sans relance")
    verifie(sup.get("a").cfg["source"]["client"] == "Autre",
            "le nouveau câblage est en mémoire")
    releve = [c for c in douzefx.load_strips()["strips"] if c["id"] == "a"][0]
    verifie(releve["source"]["client"] == "Autre",
            "et sur disque (donc il survit au redémarrage du démon)")

    # Un réglage qui ne touche PAS le câblage ne doit jamais relancer une bande :
    # cocher « démarrage auto » couperait le son pour rien.
    r = sup.update_strip("a", {"autostart": True})
    verifie(r["updated"] and not r["restarted"], "autostart ne recâble rien")

    verifie(not sup.update_strip("zzz", {"name": "?"})["updated"],
            "modifier une bande inconnue est refusé, pas planté")

    # Le rack ne doit PAS être touché par un recâblage : c'est ce qui sépare
    # « corriger la source » de « refaire la bande ».
    verifie(sup.get("a").cfg["rack"] == os.path.join(tmp, "a.json"),
            "le rack survit au recâblage")

    # strips.json édité à la main hors de Douze : la bande disparue ne doit pas
    # rester dans le superviseur, sinon plus rien ne peut la piloter.
    cfg = douzefx.load_strips()
    cfg["strips"] = [c for c in cfg["strips"] if c["id"] != "b"]
    douzefx.save_strips(cfg)
    sup.reload()
    verifie(list(sup.strips) == ["a"], "une bande retirée de la config disparaît")


def _graphe_factice():
    """Un graphe PipeWire minimal : un puits virtuel de bande, une application qui
    le VISE mais qui est retombée sur le sink par défaut, et une application qui ne
    vise rien (elle ne doit pas être touchée)."""
    noeud = lambda i, nom, cls, **kw: dict(
        id=i, type="PipeWire:Interface:Node",
        info={"props": dict(**{"node.name": nom, "media.class": cls}, **kw)})
    port = lambda i, nid, sens, num: dict(
        id=i, type="PipeWire:Interface:Port",
        info={"props": {"node.id": nid, "port.direction": sens, "port.id": num}})
    lien = lambda i, on, op, inn, ip: dict(
        id=i, type="PipeWire:Interface:Link",
        info={"output-node-id": on, "output-port-id": op,
              "input-node-id": inn, "input-port-id": ip})
    return [
        noeud(100, "douze_fx_in_test", "Audio/Sink", **{"object.serial": 500}),
        port(110, 100, "in", 0), port(111, 100, "in", 1),
        noeud(200, "vesktop", "Stream/Output/Audio",
              **{"target.object": "douze_fx_in_test"}),
        port(210, 200, "out", 0), port(211, 200, "out", 1),
        noeud(300, "ssl12.pb12", "Audio/Sink"),
        port(310, 300, "in", 0), port(311, 300, "in", 1),
        noeud(400, "autre-appli", "Stream/Output/Audio"),
        port(410, 400, "out", 0),
        lien(900, 200, 210, 300, 310), lien(901, 200, 211, 300, 311),
        lien(902, 400, 410, 300, 310),
    ]


def test_vue_partagee(tmp):
    print("[test] vue partagée : un seul interrogateur du moteur")
    import douzefx

    douzefx.CONFIG_DIR = tmp
    douzefx.STRIPS_PATH = os.path.join(tmp, "vue-strips.json")
    douzefx.LOG_DIR = tmp
    with open(douzefx.STRIPS_PATH, "w") as f:
        json.dump({"strips": [{"id": "v", "name": "V", "rack": "",
                               "source": {}, "dest": {}}]}, f)

    sup = douzefx.Supervisor()
    lectures = []

    # Le vrai moteur remet ses crêtes à zéro à chaque lecture : on l'imite en
    # comptant les lectures, puisque c'est exactement ce qui coûte un pic.
    def faux_snapshot():
        lectures.append(1)
        return {"id": "v", "live": {"in_peak": 0.5}}
    sup.strips["v"].snapshot = faux_snapshot

    a, b, c = sup.list(), sup.list(), sup.list()
    verifie(len(lectures) == 1,
            f"trois clients rapprochés = UNE lecture du moteur ({len(lectures)})")
    verifie(a == b == c, "et tous voient la même chose")

    verifie(sup.list(frais=True) and len(lectures) == 2,
            "on peut forcer une lecture fraîche")

    sup.oublier_vue()
    sup.list()
    verifie(len(lectures) == 3, "après une action, la vue périmée est relue")

    sup.vue_t -= sup.VUE_TTL + 1        # vieillit la vue
    sup.list()
    verifie(len(lectures) == 4, "et elle expire d'elle-même passé le TTL")


def test_profils(tmp):
    print("[test] profils de bandes")
    import douzefx

    douzefx.CONFIG_DIR = tmp
    douzefx.STRIPS_PATH = os.path.join(tmp, "prof-strips.json")
    douzefx.PROFILES_DIR = os.path.join(tmp, "profiles")
    douzefx.LOG_DIR = tmp

    rack_a = os.path.join(tmp, "pa.json")
    with open(rack_a, "w") as f:
        json.dump({"version": 1, "bypass": False,
                   "stages": [{"path": "/x/Comp.vst3", "name": "Comp"}]}, f)
    with open(douzefx.STRIPS_PATH, "w") as f:
        json.dump({"strips": [{"id": "a", "name": "A", "rack": rack_a,
                               "source": {"client": "X", "channels": [1]},
                               "dest": {"kind": "virtualmic", "name": "A"},
                               "autostart": True}]}, f)

    sup = douzefx.Supervisor()
    verifie(sup.save_profile("Stream") == 1, "profil enregistré (1 bande)")
    verifie(douzefx.list_profiles() == ["Stream"], "il apparaît dans la liste")
    verifie(sup.profil_courant() == "Stream", "et devient le rappel courant")

    # Le rack est EMBARQUÉ : un profil doit se rappeler même si le fichier de
    # rack a été écrasé entre-temps.
    with open(_profil_fichier(tmp, "Stream")) as f:
        prof = json.load(f)
    verifie([s["name"] for s in prof["racks"]["a"]["stages"]] == ["Comp"],
            "la chaîne est dans le fichier de profil, pas juste référencée")

    # On bricole après coup : le rappel doit disparaître, sinon une config
    # modifiée se ferait passer pour le profil enregistré.
    sup.update_strip("a", {"name": "A bis"})
    verifie(sup.profil_courant() is None,
            "une modification manuelle défait le rappel")

    # Deuxième profil, avec une bande DIFFÉRENTE : le rappel doit remplacer, pas
    # fusionner.
    rack_b = os.path.join(tmp, "pb.json")
    with open(rack_b, "w") as f:
        json.dump({"version": 1, "bypass": False, "stages": []}, f)
    sup.add_strip({"id": "b", "name": "B", "rack": rack_b,
                   "source": {}, "dest": {}, "autostart": False})
    sup.save_profile("Musique")

    a_lancer = sup.load_profile("Stream")
    verifie(list(sup.strips) == ["a"],
            f"rappeler « Stream » ne laisse que sa bande ({list(sup.strips)})")
    verifie(a_lancer == ["a"], "les bandes en démarrage auto sont annoncées")
    verifie(sup.profil_courant() == "Stream", "le rappel est de nouveau posé")
    verifie(sup.get("a").cfg["name"] == "A", "le nom d'origine est restauré")

    # Le rack écrasé doit être remis en place par le rappel.
    with open(rack_a, "w") as f:
        json.dump({"version": 1, "bypass": False, "stages": []}, f)
    sup.load_profile("Stream")
    with open(rack_a) as f:
        verifie([s["name"] for s in json.load(f)["stages"]] == ["Comp"],
                "un rack écrasé est restauré par le rappel")

    verifie(sorted(douzefx.list_profiles()) == ["Musique", "Stream"], "deux profils")
    douzefx.delete_profile("Musique")
    verifie(douzefx.list_profiles() == ["Stream"], "suppression d'un profil")

    for mauvais in ("", "   ", "///"):
        try:
            douzefx._profile_path(mauvais)
            verifie(False, f"nom de profil vide refusé ({mauvais!r})")
        except ValueError:
            verifie(True, f"nom de profil sans caractère utile refusé ({mauvais!r})")


def _profil_fichier(tmp, nom):
    return os.path.join(tmp, "profiles", nom + ".json")


def test_bande_figee(tmp):
    print("[test] bande figée : « démarre » ≠ « ne répond plus »")
    import douzefx

    douzefx.LOG_DIR = tmp
    cfg = {"id": "f", "name": "F", "rack": "", "source": {}, "dest": {}}
    s = douzefx.Strip(cfg, 95)

    vivante = [True]
    reponse = [None]
    s.alive = lambda: vivante[0]
    s.api = lambda *a, **k: reponse[0]

    # 1) Elle démarre : jamais répondu, donc surtout PAS « figée » — c'est ce
    #    mélange qui affichait « Démarrage… » à l'infini sur une bande morte.
    snap = s.snapshot()
    verifie(not snap.get("frozen") and not snap["ready"],
            "au démarrage : ni prête, ni figée")

    # 2) Elle répond : prête.
    reponse[0] = {"name": "F", "stages": []}
    snap = s.snapshot()
    verifie(snap["ready"] and not snap.get("frozen"), "elle répond : prête")

    # 3) Elle se tait alors qu'elle avait répondu : figée, et on le DIT.
    reponse[0] = None
    snap = s.snapshot()
    verifie(snap.get("frozen") is True, "muette après avoir répondu : figée")
    verifie("son continue" in snap.get("problem", ""),
            f"le message dit que le son passe : {snap.get('problem')!r}")

    # 3 bis) Le cas le PLUS courant : l'API répond encore (valeurs en cache) et
    #        c'est le moteur lui-même qui se déclare figé. Le silence ne le
    #        trahirait pas — seul son aveu le révèle.
    reponse[0] = {"name": "F", "stages": [],
                  "frozen": True, "frozen_phase": "ouverture de l'éditeur natif"}
    snap = s.snapshot()
    verifie(snap["ready"] and snap.get("frozen") is True,
            "moteur qui s'avoue figé : prête ET figée à la fois")
    verifie("éditeur est perdu" in snap.get("problem", ""),
            f"le message nomme ce qui est perdu : {snap.get('problem')!r}")

    # 4) Un nouveau démarrage remet le compteur à zéro : sinon une bande relancée
    #    serait annoncée « figée » pendant toute son instanciation.
    reponse[0] = None
    s.a_repondu = False
    verifie(not s.snapshot().get("frozen"), "après relance : elle démarre, elle ne gèle pas")

    # 5) Process mort : ce n'est pas un gel, c'est un arrêt.
    vivante[0] = False
    snap = s.snapshot()
    verifie(not snap["running"] and not snap.get("frozen"),
            "process mort : arrêtée, pas figée")


def test_capteurs_paralleles():
    """Une application qui capte l'entrée MATÉRIELLE court-circuite la bande.

    Vécu le 19/08/2026 : Discord avait deux flux de capture, l'un sur le micro
    traité, l'autre — sans cible, donc posé sur le périphérique par défaut —
    directement sur les entrées brutes de la carte. Les interlocuteurs
    entendaient le micro non traité, et régler les plugins ne changeait rien
    puisque ce flux ne les traversait pas. Rien, nulle part, ne le disait.
    """
    print("[test] applications qui captent l'entrée brute en parallèle")
    from douzefx import capteurs_paralleles

    def noeud(i, nom, classe, appli=None):
        p = {"node.name": nom, "media.class": classe}
        if appli:
            p["application.name"] = appli
        return {"id": i, "type": "PipeWire:Interface:Node", "info": {"props": p}}

    def lien(src, dst):
        return {"id": 900 + src * 10 + dst, "type": "PipeWire:Interface:Link",
                "info": {"output-node-id": src, "input-node-id": dst}}

    carte = noeud(1, "alsa_input.ssl12", "Audio/Source")
    moteur = noeud(2, "douze-fx.mic", "Stream/Input/Audio")
    micvirt = noeud(3, "douze_fx_mic", "Audio/Source/Virtual")
    propre = noeud(4, "vesktop", "Stream/Input/Audio", "vesktop")     # sur le traité
    voleur = noeud(5, "vesktop", "Stream/Input/Audio", "vesktop")     # sur le brut
    hp = noeud(6, "vesktop", "Stream/Output/Audio", "vesktop")        # lecture : hors sujet

    base = [carte, moteur, micvirt, propre, voleur, hp,
            lien(1, 2), lien(2, 3), lien(3, 4)]

    verifie(capteurs_paralleles(base, "douze-fx.mic") == [],
            "graphe sain : personne d'autre ne capte l'entrée matérielle")

    avec = base + [lien(1, 5)]
    verifie(capteurs_paralleles(avec, "douze-fx.mic") == ["vesktop"],
            f"le deuxième flux est nommé : {capteurs_paralleles(avec, 'douze-fx.mic')}")

    # Un flux de LECTURE branché sur la carte n'est pas une captation.
    verifie(capteurs_paralleles(base + [lien(1, 6)], "douze-fx.mic") == [],
            "un flux de lecture ne déclenche pas l'alerte")

    # Une bande alimentée par son PROPRE puits virtuel : les applications qui y
    # jouent sont ce qu'elle traite, pas des concurrentes.
    puits = noeud(7, "douze_fx_in_retour", "Audio/Sink")
    ret = noeud(8, "douze-fx.retour", "Stream/Input/Audio")
    app = noeud(9, "vesktop", "Stream/Input/Audio", "vesktop")
    verifie(capteurs_paralleles([puits, ret, app, lien(7, 8), lien(7, 9)],
                                "douze-fx.retour") == [],
            "une source non matérielle ne déclenche rien")

    verifie(capteurs_paralleles(base, "douze-fx.absente") == [],
            "bande arrêtée (absente du graphe) : rien à signaler")


def test_threads_temps_reel(tmp):
    """Les threads audio d'une bande tournent-ils en SCHED_FIFO / SCHED_RR ?

    Vécu le 20/08/2026 : TOUT le chemin audio de la machine était en
    SCHED_OTHER. Les bandes se déclaraient en marche, les plugins traitaient,
    les vumètres bougeaient — et le son décrochait en rafale dès un partage
    d'écran 4K (994 xruns sur l'entrée de la carte). Rien nulle part ne le
    disait, et la cause était hors de Douze.
    """
    print("[test] threads audio hors temps réel")
    from douzefx import _sched_thread, threads_audio_sans_rt

    def stat(pid, comm, politique, prio=0):
        # /proc/<pid>/stat : champ 40 = rt_priority, champ 41 = policy. Après la
        # DERNIÈRE `)`, reste[0] est le champ 3, donc le champ N est reste[N-3].
        bourre = " ".join(["0"] * 36)
        return f"{pid} ({comm}) S {bourre} {prio} {politique} 0 0 0"

    def faux_proc(racine, pid, threads):
        for tid, (comm, politique) in enumerate(threads.items(), start=pid + 1):
            d = os.path.join(racine, str(pid), "task", str(tid))
            os.makedirs(d)
            with open(os.path.join(d, "comm"), "w") as f:
                f.write(comm + "\n")
            with open(os.path.join(d, "stat"), "w") as f:
                f.write(stat(tid, comm, politique))

    # --- le piège du champ `comm` -------------------------------------------
    racine = os.path.join(tmp, "proc-parse")
    os.makedirs(racine)
    piege = os.path.join(racine, "stat")
    with open(piege, "w") as f:
        f.write(stat(42, "au (bon) endroit", 1, 88))
    verifie(_sched_thread(piege) == (1, 88),
            "un comm contenant espaces et parenthèses ne décale pas les champs")

    # --- une bande en bon état ----------------------------------------------
    # ⚠️ Les `pw-<nœud>` sont les boucles de CONTRÔLE de pipewire-jack : elles
    # sont en SCHED_OTHER même quand tout va bien (constaté sur les deux vraies
    # bandes, chemin audio en FF 83). Les compter allumait une alerte permanente
    # — le premier jet de ce relevé le faisait, et le vrai système l'a démenti.
    ok = os.path.join(tmp, "proc-ok")
    faux_proc(ok, 100, {"data-loop.0": 1, "data-loop.1": 1,
                        "pw-douze-fx": 0, "JUCE Timer": 0})
    sans, total = threads_audio_sans_rt(100, racine=ok)
    verifie((sans, total) == ([], 2),
            "deux data-loops en FIFO : rien à signaler")
    verifie(total == 2,
            "les boucles de contrôle pw-* ne sont PAS comptées (alerte permanente sinon)")
    verifie("JUCE Timer" not in sans,
            "les threads NON audio sont ignorés")

    # --- SCHED_RR compte aussi ----------------------------------------------
    rr = os.path.join(tmp, "proc-rr")
    faux_proc(rr, 200, {"data-loop.0": 2})
    verifie(threads_audio_sans_rt(200, racine=rr) == ([], 1),
            "SCHED_RR est du temps réel aussi (c'est ce qu'accorde RTKit)")

    # --- la panne du 20/08 ---------------------------------------------------
    ko = os.path.join(tmp, "proc-ko")
    faux_proc(ko, 300, {"data-loop.0": 0, "data-loop.1": 0, "wine-stdio": 0})
    sans, total = threads_audio_sans_rt(300, racine=ko)
    verifie((sans, total) == (["data-loop.0", "data-loop.1"], 2),
            "data-loops en SCHED_OTHER : les deux sont nommés")

    # --- moteur qui démarre encore -------------------------------------------
    vide = os.path.join(tmp, "proc-vide")
    faux_proc(vide, 400, {"douze_fx": 0, "pw-douze-fx": 0, "wine-stdio": 0})
    verifie(threads_audio_sans_rt(400, racine=vide) == ([], 0),
            "aucun thread audio trouvé : on ne conclut rien plutôt qu'alarmer")

    verifie(threads_audio_sans_rt(999, racine=vide) == ([], 0),
            "process disparu : pas d'exception, pas d'alerte")


def test_refus_transmis(tmp):
    """Un moteur qui dit NON n'est pas un moteur absent.

    `urllib.error.HTTPError` dérive de `URLError` : le refus argumenté du moteur
    (« aucun affichage », 409) tombait dans le même filet que « personne au bout
    du fil », et la GUI affichait « bande injoignable » pour une bande qui allait
    parfaitement bien. Le diagnostic devenait impossible à lire.
    """
    print("[test] refus du moteur transmis tel quel")
    import douzefx
    import http.server, threading

    douzefx.LOG_DIR = tmp

    class Faux(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            corps = b'{"ok": false, "error": "aucun affichage"}'
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            self.wfile.write(corps)

        def do_GET(self):
            self.do_POST()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Faux)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cfg = {"id": "r", "name": "R", "rack": "", "source": {}, "dest": {}}
        s = douzefx.Strip(cfg, 0)
        s.port = srv.server_address[1]

        out = s.api("/editor", {"index": 0}, timeout=5)
        verifie(isinstance(out, dict), f"un refus revient comme un objet, pas None : {out!r}")
        verifie(out.get("error") == "aucun affichage",
                f"et il porte la RAISON du moteur : {out!r}")

        # Corollaire : une bande qui refuse est vivante. C'était déjà vrai, mais
        # ça ne se voyait pas — elle passait pour morte.
        verifie(s.alive(), "une bande qui refuse reste comptée comme vivante")
    finally:
        srv.shutdown()
        srv.server_close()


class FauxProc:
    """Un process dont on décide la mort. `poll()` : None = vivant."""

    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code

    @property
    def returncode(self):
        return self.code

    def terminate(self):
        self.code = -15

    def kill(self):
        self.code = -9

    def wait(self, timeout=None):
        return self.code


def test_veille_pipewire(tmp):
    print("[test] veille : une bande dont le SON meurt sans que le process meure")
    import time

    import douzefx

    douzefx.LOG_DIR = tmp
    cfg = {"id": "v", "name": "V", "rack": "",
           "source": {}, "dest": {"kind": "virtualmic", "name": "V"}}

    def bande():
        s = douzefx.Strip(cfg, 96)
        s.proc = FauxProc()                     # lancée par NOUS (sinon pas surveillée)
        s.vnodes = {"mic": FauxProc()}
        s._stop_virtual_nodes = lambda: None    # pas de pkill dans un test
        s._pipewire_pret = lambda: True
        s.api = lambda *a, **k: None
        s.demarrages = []
        def start():
            s.demarrages.append(1)
            s.proc = FauxProc()
            s.vnodes = {"mic": FauxProc()}
            return True, "démarrée"
        s.start = start
        return s

    # 1) Tout va bien : la veille ne doit RIEN faire. Une veille qui relance à
    #    tort est pire que pas de veille.
    s = bande()
    s.audio_vu = True
    s.sante_t = time.time()
    verifie(s.supervise() is None and not s.demarrages,
            "bande saine : ni relance ni message")

    # 2) LA PANNE DU 18/08 : PipeWire redémarre, le `pw-cli` qui tenait le nœud
    #    meurt, le nœud disparaît — et le moteur, lui, reste vivant. Rien ne le
    #    voyait : la bande se disait « en marche », Discord n'avait plus de micro.
    s = bande()
    s.audio_vu = True
    s.sante_t = time.time()
    s.vnodes["mic"].code = 0                    # le pw-cli est mort (zombie récolté)
    msg = s.supervise()
    verifie(len(s.demarrages) == 1, "nœud virtuel disparu : la bande est relancée")
    verifie("nœud virtuel" in (msg or ""), f"et la raison est nommée : {msg!r}")
    verifie(s.snapshot().get("notice", "").startswith("relancée"),
            "la GUI reçoit un avis (une relance silencieuse ne s'explique pas)")

    # 3) Le serveur n'est pas encore revenu : on ne tente RIEN. Sans ce garde-fou,
    #    les trois relances du budget se consommaient pendant les deux secondes où
    #    PipeWire redémarre, et la bande était condamnée pour une panne déjà finie.
    s = bande()
    s.audio_vu = True
    s.sante_t = time.time()
    s.vnodes["mic"].code = 0
    s._pipewire_pret = lambda: False
    verifie(s.supervise() is None and not s.demarrages,
            "PipeWire absent : aucune tentative")
    verifie(not s.restarts, "et le budget de relances est intact (on repassera)")

    # 4) L'autre bout de la MÊME panne : pas de nœud virtuel (SSL → SSL), donc
    #    rien ne disparaît — mais le client audio du moteur est mort et
    #    `sampleRate` retombe à 0. Seul symptôme disponible.
    s = bande()
    s.vnodes = {}
    s.api = lambda *a, **k: {"stages": [], "sampleRate": 0.0}
    s.audio_vu = True                            # on l'a VUE jouer
    s.audio_ko_t = time.time() - 30              # muette depuis 30 s
    msg = s.supervise()
    verifie(len(s.demarrages) == 1, "plus de client audio : la bande est relancée")
    verifie("client audio" in (msg or ""), f"et la raison est nommée : {msg!r}")

    # 5) Une bande qui DÉMARRE annonce elle aussi sampleRate 0. La relancer
    #    l'empêcherait de jamais finir de démarrer.
    s = bande()
    s.vnodes = {}
    s.api = lambda *a, **k: {"stages": [], "sampleRate": 0.0}
    verifie(s.supervise() is None and not s.demarrages,
            "jamais vue jouer : c'est un démarrage, pas une panne")

    # 6) Budget : une panne qui revient sans cesse doit finir par s'arrêter et le
    #    DIRE, plutôt que de relancer la bande indéfiniment.
    s = bande()
    s.audio_vu = True
    s.sante_t = time.time()
    for _ in range(douzefx.Strip.RESTART_MAX + 1):
        s.sante_t = time.time()
        s.vnodes["mic"].code = 0
        dernier = s.supervise()
    verifie(len(s.demarrages) == douzefx.Strip.RESTART_MAX,
            f"au plus {douzefx.Strip.RESTART_MAX} relances dans la fenêtre")
    verifie("arrêtée après" in (dernier or ""), f"puis on renonce en le disant : {dernier!r}")
    verifie(s.snapshot().get("problem") == s.give_up,
            "et la GUI l'affiche comme un problème, pas comme un avis")

    # 7) Un arrêt VOULU efface l'ardoise ; une fermeture interne à la relance,
    #    non — sinon le budget se remettrait à zéro à chaque tour et ne
    #    plafonnerait jamais rien.
    s = bande()
    s.restarts = [time.time()]
    s._teardown = lambda: None
    s.stop()
    verifie(s.restarts == [] and s.give_up is None,
            "arrêt manuel : budget et renoncement remis à zéro")


def test_readoption():
    print("[test] réadoption des applications sur un nœud recréé")
    import douzefx

    verifie(douzefx._apparier([1, 2], [3, 4]) == [(1, 3), (2, 4)], "stéréo → stéréo")
    verifie(douzefx._apparier([1], [3, 4]) == [(1, 3), (1, 4)],
            "un micro MONO se dédouble (sinon un canal reste muet)")

    # Graphe VIVANT : `pw-link` y ajoute et retire vraiment des liens. Un faux
    # graphe figé ne pourrait pas prouver que la vérification converge — il
    # rapporterait un lien en trop même quand tout s'est bien passé.
    appels = []
    graphe = _graphe_factice()
    prochain = [1000]

    def faux_pw_link(*a):
        appels.append(a)
        if a[0] == "-d":
            sortie, entree = int(a[1]), int(a[2])
            for o in list(graphe):
                i = o.get("info") or {}
                if (str(o.get("type", "")).endswith("Link")
                        and i.get("output-port-id") == sortie
                        and i.get("input-port-id") == entree):
                    graphe.remove(o)
            return True
        sortie, entree = int(a[0]), int(a[1])
        noeud_de = {p["id"]: (p.get("info") or {})["props"]["node.id"]
                    for p in graphe if str(p.get("type", "")).endswith("Port")}
        prochain[0] += 1
        graphe.append({"id": prochain[0], "type": "PipeWire:Interface:Link",
                       "info": {"output-node-id": noeud_de[sortie],
                                "output-port-id": sortie,
                                "input-node-id": noeud_de[entree],
                                "input-port-id": entree}})
        return True

    douzefx._pw_objects = lambda: graphe
    douzefx._pw_link = faux_pw_link

    verifie(douzefx.readopt_streams("douze_fx_in_test", mic=False) == 1,
            "une application rebranchée")

    branches = [a for a in appels if a[0] != "-d"]
    debranches = [a for a in appels if a[0] == "-d"]
    verifie(branches == [("210", "110"), ("211", "111")],
            f"branché canal par canal sur le puits : {branches}")
    verifie(debranches == [("-d", "210", "310"), ("-d", "211", "311")],
            f"ancien lien coupé par IDENTIFIANT de port : {debranches}")
    # Le piège du 17/08 : couper par NOM aurait emporté le flux de l'autre appli,
    # qui partage les mêmes noms de ports.
    verifie(all("410" not in a and "902" not in a for a in appels),
            "l'application qui ne visait pas la bande n'est pas touchée")
    # L'ordre se lit dans la liste COMBINÉE : comparer des index de deux listes
    # séparées ne dit rien de la chronologie réelle.
    verifie(appels.index(("210", "110")) < appels.index(("-d", "210", "310")),
            "on branche AVANT de débrancher (jamais de trou sur un micro)")

    # LE point qui ferme la piste du « son doublé » : à l'arrivée, le flux ne doit
    # avoir QU'UNE source. Brancher-puis-débrancher ouvre une fenêtre à deux
    # sources ; si elle ne se refermait pas, l'application recevrait +6 dB et du
    # filtrage en peigne — une voix qui sature sans cause visible.
    def sources_du_flux():
        return [l["info"]["output-node-id"] for l in graphe
                if str(l.get("type", "")).endswith("Link")
                and l["info"]["output-node-id"] == 200] and [
            l["info"]["input-node-id"] for l in graphe
            if str(l.get("type", "")).endswith("Link")
            and l["info"]["output-node-id"] == 200]

    dest = sorted(set(sources_du_flux()))
    verifie(dest == [100],
            f"à l'arrivée le flux ne va QUE vers la bande ({dest}) — pas de son doublé")

    # Et le sink par défaut ne doit plus rien recevoir de CE flux.
    verifie(not douzefx._liens_indesirables(200, 100, "output-node-id", "input-node-id"),
            "aucun lien en trop ne subsiste")

    # Déjà bien branchée : ne rien faire du tout.
    appels.clear()
    dejala = graphe + [dict(id=903, type="PipeWire:Interface:Link",
                            info={"output-node-id": 200, "output-port-id": 210,
                                  "input-node-id": 100, "input-port-id": 110})]
    douzefx._pw_objects = lambda: dejala
    verifie(douzefx.readopt_streams("douze_fx_in_test", mic=False) == 0
            and not appels, "une application déjà branchée est laissée tranquille")

    # Le garde-fou lui-même : si `pw-link -d` ne fait RIEN (droits, course,
    # PipeWire qui refuse), l'ancien lien survit et l'application se retrouve avec
    # deux sources pour de bon. On vérifie que la fonction s'en aperçoit, qu'elle
    # insiste, et surtout qu'elle ne boucle pas à l'infini dessus.
    graphe2 = _graphe_factice()
    essais = []

    def pw_link_sourd(*a):
        essais.append(a)
        return True                       # ment : dit oui et ne change rien

    douzefx._pw_objects = lambda: graphe2
    douzefx._pw_link = pw_link_sourd
    verifie(douzefx.readopt_streams("douze_fx_in_test", mic=False) == 1,
            "débranchement qui échoue : la bande est quand même rebranchée")
    coupes = [a for a in essais if a[0] == "-d"]
    verifie(len(coupes) > 2, f"il a INSISTÉ sur le débranchement ({len(coupes)} tentatives)")
    verifie(len(coupes) < 20, "mais il a renoncé au lieu de boucler sans fin")
    verifie(douzefx._liens_indesirables(200, 100, "output-node-id", "input-node-id"),
            "et le lien en trop est bien détecté (c'est ce qui déclenche l'alerte)")

    # Nœud absent du graphe : on renonce sans rien casser (après la fenêtre
    # d'attente — en vrai le nœud vient d'être confirmé présent par `_wait_node`,
    # donc la première passe suffit).
    douzefx._pw_objects = lambda: []
    verifie(douzefx.readopt_streams("inexistant", mic=False) == 0,
            "nœud absent → aucun effet")


def test_scan_amorcage(tmp):
    print("[test] scan : amorçage du catalogue")
    import douzefx

    cache = os.path.join(tmp, "plugins.xml")
    amorce = os.path.join(tmp, "delestor.xml")
    # Entrées RÉALISTES : une entrée de `KnownPluginList` porte toujours son
    # `format`. On glisse un LADSPA et une entrée sans format, qui doivent
    # disparaître : le moteur ne sait charger ni l'un ni l'autre, et les
    # proposer reviendrait à offrir des plugins voués à l'échec.
    with open(amorce, "w") as f:
        f.write('<?xml version="1.0"?><KNOWNPLUGINS>'
                '<PLUGIN name="A" format="VST3" file="/x/a.vst3"/>'
                '<PLUGIN name="B" format="VST3" file="/x/b.vst3"/>'
                '<PLUGIN name="Vieux" format="LADSPA" file="/x/c.so"/>'
                '<PLUGIN name="Inconnu" file="/x/d.bin"/>'
                '</KNOWNPLUGINS>')

    douzefx.CACHE_PATH, douzefx.DELESTOR_CACHE = cache, amorce
    s = douzefx.Scanner()

    racine, connus = s._load_cache()
    verifie(len(racine.findall("PLUGIN")) == 2,
            "le cache de Delestor sert d'amorce (sinon on rescannerait tout le parc)")
    noms = sorted(p.get("name") for p in racine.findall("PLUGIN"))
    verifie(noms == ["A", "B"],
            f"les formats non hébergeables sont élagués (LADSPA, format absent) : {noms}")
    verifie(connus == {"/x/a.vst3", "/x/b.vst3"}, "les fichiers connus sont indexés")
    verifie(s._count_known() == 2, "le compteur affiché n'est pas à zéro avant le 1er scan")

    s._write_cache(racine)
    verifie(os.path.exists(cache), "écriture du cache")
    racine2, _ = s._load_cache()
    verifie(len(racine2.findall("PLUGIN")) == 2, "relecture fidèle")

    st = s.status()
    verifie(st["running"] is False and st["total"] == 0, "état initial au repos")
    ok, msg = s.skip()
    verifie(ok, f"« passer » est sans danger même sans scan en cours ({msg})")


def test_enumeration_plugins():
    print("[test] scan : énumération des fichiers")
    import douzefx
    fichiers = douzefx._plugin_files()
    verifie(all(f.endswith(".vst3") for f in fichiers),
            f"que du VST3 ({len(fichiers)} fichiers) — le CLAP n'est pas hébergeable")
    verifie(len(fichiers) == len(set(fichiers)), "aucun doublon")
    verifie(not any("/Contents/" in f for f in fichiers),
            "on ne descend pas DANS les bundles (sinon on trouverait le binaire deux fois)")


def main():
    import douzefx

    # ⚠️ LES PORTS D'ABORD, avant le moindre `Strip`.
    #
    # Les bandes de test s'appellent « a » et « b », donc index 0 et 1, donc
    # ports 1213 et 1214 — CEUX DES VRAIES BANDES. Et `Strip.alive()` se replie
    # sur « quelqu'un répond-il sur mon port ? » pour ré-adopter une bande
    # survivante : lancée pendant que Douze tourne, la batterie prenait le micro
    # de l'utilisateur pour sa propre bande de test, lui envoyait /quit, et le
    # relançait sous le nom « A » — micro coupé, nœud fantôme dans le graphe.
    # Une batterie de tests dont la promesse est « ne touche à rien » doit tenir
    # cette promesse même quand tout tourne.
    douzefx.PORT_BASE = 1400

    with tempfile.TemporaryDirectory() as tmp:
        test_slug()
        test_nom_depuis_chemin()
        test_rack_hors_ligne(tmp)
        test_memoire_ecoute_directe(tmp)
        test_config_bandes(tmp)
        test_vue_partagee(tmp)
        test_bande_figee(tmp)
        test_veille_pipewire(tmp)
        test_capteurs_paralleles()
        test_threads_temps_reel(tmp)
        test_refus_transmis(tmp)
        test_profils(tmp)
        test_readoption()
        test_scan_amorcage(tmp)
        test_enumeration_plugins()

    print(f"\n=== {TOTAL - len(ECHECS)}/{TOTAL} vérification(s) OK", end=" ")
    print("— tout passe ===" if not ECHECS else f"— ÉCHEC ===\n{chr(10).join(ECHECS)}")
    return 1 if ECHECS else 0


if __name__ == "__main__":
    sys.exit(main())
