"""Modèles de cartes SSL connus de Douze (SSL_MODEL=ssl12|ssl18).

Un modèle dit trois choses : comment trouver la carte (PID USB du « SSL Control
I/F », nom du nœud ALSA dans PipeWire) et SURTOUT si son protocole de contrôle
est VALIDÉ.

Règle du projet : jamais d'octets inventés vers un firmware. Toutes les trames de
Douze viennent de captures de SSL 360 avec une SSL 12. Pour un modèle dont le
protocole n'est pas validé (`protocole_valide` faux) :
  - le démon ne touche PAS à l'USB (ni handshake, ni keepalive, ni état) ;
  - `sslctl` refuse toute écriture, sauf accord explicite de la personne avec
    SSL_ALLOW_UNVALIDATED=1 (lecture, `info`, `listen`, `diag` restent libres) ;
  - ce qui ne dépend pas du protocole marche quand même : Douze FX (effets
    VST3 dans le graphe PipeWire) et la règle anti-veille WirePlumber.

Pour promouvoir un modèle : l'ajouter à `PROTOCOLE_VALIDE` seulement après
captures de SSL 360 sur ce matériel, consignées dans captures/JOURNAL.md.
"""

import os

MODELES = {
    "ssl12": {
        "nom": "SSL 12",
        "pid_controle": 0x0024,
        "pid_audio": 0x0005,
        "noeud_alsa": r"^alsa_output\.usb-Solid_State_Logic_SSL_12_.*\.pro-output-0$",
        "sinks_stereo": True,
    },
    "ssl18": {
        "nom": "SSL 18",
        "pid_controle": 0x0026,
        "pid_audio": 0x000A,
        # Nom du nœud PAS encore vu sur une machine : `sslctl diag` l'affiche.
        "noeud_alsa": r"^alsa_output\.usb-Solid_State_Logic_SSL_18.*",
        "sinks_stereo": False,
    },
}

PROTOCOLE_VALIDE = {"ssl12"}


def choisir():
    """→ (clé, modèle). SSL_MODEL choisit ; défaut ssl12 ; inconnu = erreur."""
    cle = os.environ.get("SSL_MODEL", "ssl12").strip().lower()
    if cle not in MODELES:
        raise SystemExit(f"SSL_MODEL={cle!r} inconnu — modèles : "
                         f"{', '.join(sorted(MODELES))}")
    return cle, MODELES[cle]


CLE, MODELE = choisir()
NOM = MODELE["nom"]
# SSL_PID (ancienne variable) force un autre PID : le protocole n'est alors validé
# que s'il s'agit bien de celui du modèle.
PID_CONTROLE = int(os.environ.get("SSL_PID", hex(MODELE["pid_controle"])), 0)
VALIDE = CLE in PROTOCOLE_VALIDE and PID_CONTROLE == MODELE["pid_controle"]
ECRITURE_AUTORISEE = VALIDE or os.environ.get("SSL_ALLOW_UNVALIDATED") == "1"
