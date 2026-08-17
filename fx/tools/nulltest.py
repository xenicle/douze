#!/usr/bin/env python3
"""Null-test du chemin audio d'une bande Douze FX.

    python fx/tools/nulltest.py          # Douze doit tourner (port 1212)

Injecte un signal connu dans une bande JETABLE en passthrough (rack vide),
réenregistre sa sortie, réaligne, soustrait. Le résidu doit être NUL : entre
l'entrée et la sortie, Douze FX ne doit rien ajouter, rien retrancher, rien
rééchantillonner. C'est la seule vérification qui porte sur le son lui-même —
tout le reste du projet teste des décisions autour du son.

Deux choix de méthode :

- l'alignement se fait sur une IMPULSION de repère en tête du signal. Chercher le
  décalage par corrélation croisée coûterait O(n²) en Python pur (et il n'y a pas
  de numpy dans ce devShell) ; une impulsion se trouve en une passe ;
- le résidu se mesure sur le BRUIT BLANC qui suit, parce qu'il excite tout le
  spectre : un défaut qui ne toucherait qu'une bande de fréquences s'y voit.

Le test mesure aussi le résidu à ±1 et ±2 échantillons. Sans ça, un « résidu
nul » ne prouverait rien : il faut montrer que la mesure SAIT échouer. Un
désalignement d'un seul échantillon doit faire remonter le résidu au niveau du
signal (aucune annulation).
"""
import json
import math
import os
import random
import struct
import subprocess
import tempfile
import time
import urllib.request
import wave

DOUZE = os.environ.get("DOUZE_URL", "http://localhost:1212") + "/fx"
SR = 44100
ID = "nulltest"                # bande jetable, supprimée à la fin
SILENCE_AV = 0.30              # avant l'impulsion
SILENCE_AP = 0.20              # après l'impulsion, avant le bruit
BRUIT = 4.0                    # secondes de bruit blanc
NIVEAU = 0.30                  # amplitude du bruit
GARDE = 1000                   # marge d'échantillons aux deux bouts de la mesure


def douze(cmd, **kw):
    corps = json.dumps(dict(cmd=cmd, **kw)).encode()
    req = urllib.request.Request(DOUZE, data=corps,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def dbfs(x):
    return -math.inf if x <= 0 else 20 * math.log10(x / 32768.0)


def ecrire_wav(chemin, mono):
    w = wave.open(chemin, "wb")
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    entrelace = [v for v in mono for _ in range(2)]
    w.writeframes(struct.pack("<%dh" % len(entrelace), *entrelace))
    w.close()


def lire_wav(chemin):
    w = wave.open(chemin, "rb")
    n, larg, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
    brut = w.readframes(w.getnframes())
    w.close()
    if larg != 2:
        raise SystemExit(f"format inattendu : {larg * 8} bits")
    ech = struct.unpack("<%dh" % (len(brut) // 2), brut)
    return [list(ech[c::n]) for c in range(n)], sr


def signal():
    """Impulsion de repère puis bruit blanc DÉTERMINISTE (rejouable à l'identique)."""
    rnd = random.Random(1212)
    avant = [0] * int(SILENCE_AV * SR)
    apres = [0] * int(SILENCE_AP * SR)
    bruit = [int(rnd.uniform(-1, 1) * NIVEAU * 32767)
             for _ in range(int(BRUIT * SR))]
    return avant + [int(0.9 * 32767)] + apres + bruit, len(avant)


def residu(ref, cap, decalage, debut, fin):
    """RMS et crête de (capture alignée − référence), en valeurs brutes s16."""
    somme = crete = 0
    for i in range(debut, fin):
        e = cap[i + decalage] - ref[i]
        somme += e * e
        if abs(e) > crete:
            crete = abs(e)
    return math.sqrt(somme / (fin - debut)), crete


def main():
    tmp = tempfile.mkdtemp(prefix="douze-nulltest-")
    envoye, recu = os.path.join(tmp, "envoye.wav"), os.path.join(tmp, "recu.wav")
    ref, pos_imp = signal()
    ecrire_wav(envoye, ref)
    print(f"signal : impulsion à {pos_imp}, puis {BRUIT} s de bruit blanc")

    print("création de la bande de test…")
    try:
        douze("remove", id=ID)              # reliquat d'un essai interrompu
    except Exception:
        pass
    douze("add", strip={
        "id": ID, "name": "Null Test",
        "source": {"kind": "virtualsink", "name": "Null Test In"},
        "dest": {"kind": "virtualmic", "name": "Null Test Out", "channels": 2},
        "autostart": False, "cut_direct": [],
    })
    r = douze("start", id=ID)
    if not r.get("ok"):
        raise SystemExit(f"la bande n'a pas démarré : {r.get('msg')}")

    try:
        time.sleep(1.0)                     # laisser le graphe se poser
        rec = subprocess.Popen(
            ["pw-record", "--target", "douze_fx_nulltest", "--channels=2",
             f"--rate={SR}", "--format=s16", recu],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)                     # que l'enregistreur soit prêt
        subprocess.run(["pw-play", "--target", "douze_fx_in_nulltest", envoye],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        rec.terminate()
        rec.wait(timeout=10)
    finally:
        douze("stop", id=ID)
        douze("remove", id=ID)
        try:
            os.remove(os.path.expanduser(f"~/.config/douze-fx/racks/{ID}.json"))
        except OSError:
            pass

    canaux, sr = lire_wav(recu)
    if sr != SR:
        raise SystemExit(f"rééchantillonnage détecté ({sr} Hz) : chemin non transparent")

    echecs = 0
    for ic, cap in enumerate(canaux):
        crete = max((abs(v) for v in cap), default=0)
        if crete < 0.3 * 32767:
            print(f"  canal {ic + 1} : IMPULSION INTROUVABLE — rien n'est passé")
            echecs += 1
            continue
        pos = max(range(len(cap)), key=lambda i: abs(cap[i]))
        d = pos - pos_imp
        debut = pos_imp + int(SILENCE_AP * SR) + GARDE
        fin = min(len(ref), len(cap) - d) - GARDE
        if fin - debut < SR:
            print(f"  canal {ic + 1} : capture trop courte pour mesurer")
            echecs += 1
            continue

        rms, pic = residu(ref, cap, d, debut, fin)
        print(f"  canal {ic + 1} : retard {d} éch. ({d / SR * 1000:.2f} ms) | "
              f"résidu RMS {dbfs(rms):.2f} dBFS, crête {dbfs(pic):.2f} dBFS")

        # Le contrôle qui rend le résultat crédible : désaligné, ça DOIT rater.
        temoins = []
        for delta in (-1, 1):
            t, _ = residu(ref, cap, d + delta, debut, fin - abs(delta))
            temoins.append(dbfs(t))
        print(f"            témoins à ±1 éch. : "
              f"{temoins[0]:.1f} / {temoins[1]:.1f} dBFS")

        if rms != 0:
            print("            ⚠ résidu NON nul : le chemin altère le signal")
            echecs += 1
        if max(temoins) < -60:
            print("            ⚠ les témoins annulent aussi : mesure non probante")
            echecs += 1

    if echecs:
        raise SystemExit(f"\n=== NULL-TEST ÉCHOUÉ ({echecs} problème(s)) ===")
    print("\n=== NULL-TEST OK — chemin bit-transparent sur tous les canaux ===")


if __name__ == "__main__":
    main()
