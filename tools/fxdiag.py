#!/usr/bin/env python3
"""Photographie l'état d'une bande AVANT de la relancer.

Raison d'être : le 19/08/2026, des bruits parasites sont apparus sur le micro
après plusieurs heures de marche, et un simple arrêt/relance de la bande les a
fait disparaître — deux fois. C'est le pire des symptômes : le geste qui répare
est aussi celui qui efface la preuve. Le moteur repart avec des plugins
réinstanciés et leur état RELU DU DISQUE, donc toute dérive accumulée en cours
de route (paramètre déplacé, auto-gain adaptatif, étage à moitié mort) part avec.

Ce script relève, sans rien perturber d'autre que les crêtes :

  - ce que le moteur dit de lui-même (bloc, xruns, CPU, crêtes par étage) ;
  - les compteurs d'erreur PipeWire, en DELTA (un compteur absolu ne dit pas si
    la panne est en cours ou date du démarrage de la machine) ;
  - l'horloge effective du graphe ;
  - la VALEUR DE TOUS LES PARAMÈTRES de chaque plugin.

Le dernier point est le plus important : relancer la bande puis relancer ce
script donne deux fichiers à comparer. S'ils diffèrent, la dérive est trouvée et
nommée ; s'ils sont identiques, la cause est ailleurs et on a éliminé une piste
au lieu de tourner en rond.

    python tools/fxdiag.py            # toutes les bandes, 20 s
    python tools/fxdiag.py mic 40     # une bande, 40 s
    diff ~/.cache/douze-fx/diag-*.txt
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

DOUZE = "http://localhost:1212"
CACHE = os.path.expanduser("~/.cache/douze-fx")


def douze(cmd, **kw):
    body = json.dumps(dict(cmd=cmd, **kw)).encode()
    req = urllib.request.Request(DOUZE + "/fx", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def pw_err():
    """Compteurs d'erreur par nœud. `-n 2` : la première passe de pw-top sort
    vide, seule la seconde porte des chiffres."""
    try:
        out = subprocess.run(["pw-top", "-b", "-n", "2"],
                             capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    res, bloc = {}, []
    for l in out.splitlines():
        if l.strip().startswith("S ") and "ID" in l:
            bloc = []
        else:
            bloc.append(l)
    for l in bloc:
        p = l.split()
        if len(p) < 9:
            continue
        try:
            res[p[-1]] = int(p[8])
        except ValueError:
            pass
    return res


def horloge():
    try:
        out = subprocess.run(["pw-metadata", "-n", "settings"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "(pw-metadata indisponible)"
    return " ".join(l.split("value:'")[1].split("'")[0].join(["", ""]) or l.strip()
                    for l in out.splitlines() if "clock." in l) or out.strip()


def main():
    cible = sys.argv[1] if len(sys.argv) > 1 else None
    duree = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0

    bandes = [s for s in douze("list")["strips"]
              if s.get("running") and (cible is None or s["id"] == cible)]
    if not bandes:
        print("aucune bande en marche" + (f" nommée « {cible} »" if cible else ""))
        return 1

    os.makedirs(CACHE, exist_ok=True)
    chemin = os.path.join(CACHE, time.strftime("diag-%Y%m%d-%H%M%S.txt"))
    sortie = open(chemin, "w")

    def dit(txt=""):
        print(txt)
        sortie.write(txt + "\n")

    dit(f"# Douze FX — relevé du {time.strftime('%d/%m/%Y %H:%M:%S')}")
    dit(f"# bandes : {', '.join(s['id'] for s in bandes)}   durée : {duree:.0f} s")
    dit()
    dit("## horloge du graphe")
    dit(horloge())
    dit()

    err0 = pw_err()
    suivi = {s["id"]: [] for s in bandes}

    t0 = time.time()
    while time.time() - t0 < duree:
        for s in bandes:
            d = douze("api", id=s["id"], path="/state")
            if isinstance(d, dict) and "sampleRate" in d:
                suivi[s["id"]].append(d)
        time.sleep(0.5)

    err1 = pw_err()

    dit("## erreurs PipeWire (delta = ce qui se passe MAINTENANT)")
    for k in sorted(set(err0) | set(err1)):
        a, b = err0.get(k, 0), err1.get(k, 0)
        if a or b:
            dit(f"  {k[:56]:56} {a:6} → {b:6}   delta {b - a:+}")
    dit()

    for s in bandes:
        rel = suivi[s["id"]]
        dit(f"## bande « {s['id']} » — {len(rel)} relevés")
        if not rel:
            dit("  (aucune réponse)")
            continue
        d = rel[-1]
        dit(f"  bloc {d.get('block')}   {d.get('sampleRate')} Hz   "
            f"backend {d.get('backend')}   xruns {d.get('xruns')}   "
            f"CPU {d.get('cpu')} %")
        xr = [r.get("xruns", 0) for r in rel]
        dit(f"  xruns pendant le relevé : {xr[0]} → {xr[-1]}   (delta {xr[-1] - xr[0]:+})")
        blocs = sorted({r.get("block") for r in rel})
        if len(blocs) > 1:
            dit(f"  ⚠ LE BLOC A CHANGÉ EN MARCHE : {blocs} — le graphe a renégocié "
                f"sous les pieds des plugins")

        def crete(f):
            v = [f(r) for r in rel if f(r)]
            return f"min {min(v):.5f} méd {sorted(v)[len(v) // 2]:.5f} max {max(v):.5f}" if v else "(silence)"

        dit(f"  entrée  : {crete(lambda r: r.get('in_peak', 0))}")
        dit(f"  sortie  : {crete(lambda r: r.get('out_peak', 0))}")
        for i, st in enumerate(d.get("stages", [])):
            dit(f"  étage {i} « {st['name']} » : "
                f"{crete(lambda r, i=i: (r.get('stages') or [{}] * (i + 1))[i].get('peak', 0))}"
                f"   chargé={st.get('loaded')} bypass={st.get('bypass')}"
                + (f"  ERREUR: {st['error']}" if st.get("error") else ""))
        dit()
        dit(f"### paramètres de « {s['id']} » (à comparer après relance)")
        for i, st in enumerate(d.get("stages", [])):
            dit(f"  --- étage {i} : {st['name']}")
            p = douze("api", id=s["id"], path=f"/params?stage={i}")
            for prm in (p.get("params") or []):
                dit(f"    {prm['name']:38} {prm['value']:.6f}  {prm['text']}")
        dit()

    sortie.close()
    print(f"\n→ écrit dans {chemin}")
    print("  Après relance de la bande, relance ce script et compare :")
    print(f"  diff {chemin} <le nouveau>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
