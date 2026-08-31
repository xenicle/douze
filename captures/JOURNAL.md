# Journal des captures

## Modèle d'entrée (copier-coller)

```
## AAAA-MM-JJ — session N
- Version SSL 360 : x.y.z  (⚠️ à vérifier à chaque session, une MAJ peut changer le protocole)
- Firmware SSL 12 : bcdDevice x.yz (lsusb -v -d 31e9:0005 | grep bcdDevice)
- Adresse USB du Control I/F : Bus ___ Device ___ (lsusb | grep 31e9:0024)
- Interface de capture : usbmon___

### NN-description.pcapng
- t≈__s : action précise (ex. « fader monitor de -inf à 0 dB, lentement »)
- t≈__s : …
- Remarques :
```

---

(entrées ci-dessous)

## 2026-08-16 — session 1 (pré-captures)

- **MAJ faite ce jour, avant toute capture** : SSL 360 passé en **V2**,
  version **v2.1.12.72214** (affichée en bas de Home) + firmware SSL 12.
- Firmware SSL 12 : bcdDevice **1.44** (était 1.41 avant la MAJ)
- Control I/F : bcdDevice 10.00, endpoints inchangés (bulk 64 o, EP 0x81 IN /
  EP 0x02 OUT) → transport identique, on capture le protocole V2 directement.
- Adresse USB du Control I/F côté hôte : Bus 001 Device 015 (⚠️ changera au
  passthrough VM — re-relever au moment de la capture)
- SSL 360 V2 = nouveau mixer multi-bus (sends HP A / HP B / Line 3-4 par canal,
  direct-to-bus, monitoring DIM/MONO/ALT, profils…) → checklist étendue (18+)
  dans `README.md`, inventaire complet dans `PROTOCOL.md`.

### 01b-idle-ssl360core-running.pcapng (d'abord nommée 01, renommée)
- Adresse device : Bus 001 Device **011** (usbmon1)
- 30 s, VM démarrée + passthrough actif, SSL 360 (GUI) fermé, aucune action —
  mais **SSL360Core.exe tournait en arrière-plan** (découvert après coup).
- Du coup cette capture documente le dialogue de fond de SSL360Core : poll OUT
  `ff 1b 01 XX YY` toutes les 150 ms + heartbeat IN `31 60` (~2 ms) + trames
  longues ~25/s (vumètres probables). Détails dans `PROTOCOL.md`.
- Version filtrée device 11 : `01b-idle-ssl360core-running.ctl.pcapng`
  (3,8 Mo, 39 794 trames) ; brut de 256 Mo supprimé après vérification.

### 01-idle.pcapng (refaite, SSL360Core tué)
- Adresse device : Bus 001 Device **013** (usbmon1) — a changé depuis la 01b
  (l'audio est Device 012)
- 30 s, VM + passthrough actifs, SSL 360 GUI fermée **et** SSL360Core.exe tué.
- Résultat : **zéro trame sur le canal de contrôle** (ni 11 ni 13 dans la
  capture ; l'audio device 12 y est bien, 329k trames → la capture
  fonctionnait). Le firmware n'émet rien spontanément : tout le trafic de
  contrôle vient du logiciel. Pas de version .ctl (elle serait vide) ; brut de
  529 Mo supprimé, le résultat « zéro trame » est la seule info à retenir.

### 02-launch-ssl360.ctl.pcapng
- Adresse device : Bus 001 Device **013** (usbmon1)
- SSL360Core tué avant capture → démarrage à froid complet : ~5 s d'attente,
  lancement de SSL 360 dans la VM, mixer complètement affiché, ~10 s, stop.
- 35 282 trames de contrôle. Contient : init vendor control (t=0), handshake
  `ff 01/ff 02/ff 05`, grosse lecture d'état (~2 229 trames OUT `ff 6b …`),
  puis bruit de fond (heartbeat/vumètres/poll 150 ms). Analyse : `PROTOCOL.md`.
- Brut de 324 Mo supprimé après vérification de la version filtrée.

### 03-quit-ssl360+kill-ssl360core.ctl.pcapng
- Adresse device : Bus 001 Device **013**, puis **reset USB en fin de capture**
  → ré-énumérée en 014 (188 trames de ré-énumération incluses dans le .ctl).
- Scénario : SSL 360 ouvert et stable → quit GUI (~t inconnu) → kill
  SSL360Core → stop.
- Résultat : **aucun message de fermeture, ni au quit GUI ni au kill Core**.
  725 messages, tous du bruit de fond (keepalive 0x1b + vumètres 0x6c), arrêt
  brutal à t≈27,2 s puis reset USB (probablement le passthrough qui relâche le
  device à la mort du driver). Un futur sslctl peut donc se déconnecter sans
  cérémonie.
- Brut de 248 Mo supprimé après vérification.

### 04-volume-monitor-(master-fader).ctl.pcapng
- Adresse device : Bus 001 Device **014**
- Master fader du Monitor Bus (SSL 360, onglet SSL 12) : bas → haut, lentement.
- Résultat : 153 messages `ff 6b` sub 06 sur **param_id 9**, valeur u32 LE =
  gain linéaire, 0 dB = 2²⁵ (butée +12 dB vérifiée à 0,0003 %). Aucun ACK.
  Structure des messages paramètre décodée → `PROTOCOL.md`.
- Brut de 292 Mo supprimé après vérification (45 654 trames dans le .ctl).

### 05-volume-casque-A.ctl.pcapng / 06-volume-casque-B.ctl.pcapng
- Adresse device : Bus 001 Device **014**
- Geste (les deux) : fader casque de **off (-∞) → tout en haut → retour à 0 dB**.
- Résultat : même message que la 04 mais **instance 4** (HP A) et **6** (HP B)
  — l'adresse paramètre est en fait (contrôle u16, instance u16) : contrôle 9
  = fader master de bus, instance = bus. Le retour à 0 dB confirme
  l'unité 2²⁵ (+0,04 dB observé). Aucune valeur envoyée pour la position off
  de départ → encodage du -∞ pas encore observé (sans doute 0).
- Bruts (230 + 222 Mo) supprimés après vérification (35 925 / 34 689 trames).

### 07-volume-ch1-(Analogue-1).ctl.pcapng
- Adresse device : Bus 001 Device **014**
- **Volume** du canal Analogue 1 dans le mixer SSL 360, min → max. (Nommée
  « gain » au départ : le vrai gain de préampli n'existe pas dans SSL 360,
  il est uniquement sur le knob physique de la SSL 12.)
- Résultat : sub 06, contrôle 1, mais **chaque mouvement écrit DEUX instances
  (8 et 0x26=38) avec la même valeur** — à élucider (FOLLOW MIX ?). Plage GUI
  off / −84,8 → +12 dB, échelle standard 2²⁵. Aucun ACK.
- Brut de 261 Mo supprimé (41 214 trames dans le .ctl).

### 08-48v-(Analogue-1-2-3-4).ctl.pcapng
- Adresse device : Bus 001 Device **014**
- 48V ON puis OFF successivement sur Analogue 1, 2, 3, 4 (avec pauses).
- Résultat : **parfait** — sub 04 (set booléen), contrôle 1, instance 0–3 =
  canal, valeur 01/00. Le 48V est bien **par canal** en V2 (pas par paire).
  Chaque set reçoit un écho IN sub 05 en ~20 ms (premier ACK observé !).
- Brut de 267 Mo supprimé (41 492 trames dans le .ctl).

### 07b-volume-ch2-(Analogue-2).ctl.pcapng
- Adresse device : Bus 001 Device **014**
- Volume du canal Analogue 2, même geste que la 07.
- Résultat : instances **9 et 39** (ch1 : 8 et 38) → volume canal = instance
  8+N, double write vers 38+N confirmé systématique. NB : plage GUI des
  volumes = off / −84,8 → +12,0 dB → même échelle 2²⁵ que les masters.
- Brut de 252 Mo supprimé (39 752 trames). (Le brut avait été nommé `.ctl` par
  erreur — renommé avant filtrage.)

### 10-mix-fader-(Playback 1-2).ctl.pcapng
- Adresse device : Bus 001 Device **014**
- Fader de tranche Playback 1-2, bas → haut.
- Résultat : contrôle 1, instance **0** (+ double write instance **31**).
  L'espace d'instances des faders de tranche : 0 = Playback 1-2, 8+N =
  Analogue N. Miroirs : 31 (pb 1-2), 38/39 (ana 1/2) — cf. hypothèse
  FOLLOW MIX, à trancher avec les captures 18/20.
- Brut de 244 Mo supprimé (38 500 trames dans le .ctl).

### 11-mute(cut)-solo.ctl.pcapng
- Adresse device : Bus 001 Device **014**
- Canal Analogue 1 : SOLO on (t≈3,7) / off (t≈9,4), puis CUT on (t≈11,1) /
  off (t≈16,7). NB : « mute » s'appelle CUT dans SSL 360.
- Résultat majeur : **cut et solo sont émulés par le host** (aucun booléen
  device). Cut = écrire 0 dans les faders du canal ; solo = écrire 0 partout
  ailleurs (toutes couches de mix) puis restaurer. Confirme **0 = -∞** et
  révèle la structure multi-couches de l'espace d'instances (stride 30).
- ⚠️ Le `.ctl.pcapng` a été supprimé par erreur en même temps que le brut
  (glob trop large). L'analyse était finie et est archivée dans
  `11-mute(cut)-solo.decoded.txt` + `PROTOCOL.md`. À re-capturer uniquement si
  on a un jour besoin du binaire exact (5 min, non bloquant).

### 12-pan-(Analogue 1).ctl.pcapng
- Adresse device : Bus 001 Device **014**
- Pan Analogue 1 : gauche → droite, puis droite → centre.
- Résultat : pan émulé host — cellules L (inst 8) / R (inst 0x26) en courbes
  complémentaires, **loi puissance constante -3 dB au centre** (retour centre
  fini à -3,0/-3,0 dB). Capture qui a permis d'établir le **modèle matrice de
  gains** (instance = 30×couche + slot) → PROTOCOL.md.
- Brut supprimé (43 812 trames dans le .ctl).

### 18-sends-hpa-(analogue 1).ctl.pcapng
- Adresse device : Bus 001 Device **014**
- Send HP A du canal Analogue 1 : send on → niveau off → +12 dB → pan
  C/G/D/C → mute off → mute on → send off.
- Résultat : 1 208 écritures **toutes à 0** vers les cellules 128/158
  (= couches 4/5 → **HP A confirmé couches 4/5** de la matrice), aucun
  booléen. Interprétation corrigée après la capture 20 (qui montre que FOLLOW
  était déjà **off** ici) : le **MUTE du send était probablement enclenché
  pendant tout le geste** → gain effectif -∞ quel que soit niveau/pan. À
  refaire en **18b**, MUTE off dès le départ.
- Brut supprimé (56 760 trames dans le .ctl).

### 20-bus-modes.ctl.pcapng
- Adresse device : Bus 001 Device **014**
- Master HP A : FOLLOW MIX 1-2 on/off → SENDS POST on/off → AFL on/off →
  CUT on/off → MONO on/off.
- Résultat : FOLLOW MIX (ctrl 7), CUT (ctrl 4), MONO (ctrl 2) = booléens
  device **sub 07** (nouveau registre, sans ACK), instance 4 = HP A.
  SENDS POST et AFL = émulation host. L'AFL révèle les **slots sources 26/27
  = retour bus HP A** (routing bus-à-bus dans la matrice). Détails PROTOCOL.md.
- Brut supprimé (45 486 trames dans le .ctl).

### 18b-sends-hpa-(analogue 1).ctl.pcapng
- Adresse device : Bus 001 Device **014**
- Send HP A Analogue 1, MUTE off au départ : niveau off → +12 → pan (bouton
  pan resté désactivé → pas d'effet) → niveau off → mute on/off (invisible,
  niveau déjà off).
- Résultat : cellules 128/158 montent de −87,8 à **+8,99 dB = +12 − 3 (pan
  centre)** → confirme la règle cellule = niveau + pan, unité 2²⁵, et
  qu'aucun booléen device n'existe pour les sends. Encodage sends bouclé.
- Brut supprimé (55 436 trames dans le .ctl).

### 13-loopback.ctl.pcapng
- Adresse device : Bus 001 Device **014**
- LOOPBACK SOURCE (colonne droite) : None → source 1 → source 2 → None.
- Résultat : `sub 08` (set enum u16), contrôle **11**, instance 0, valeurs
  1 / 2 / 0 = Playback 1-2 / Playback 3-4 / None (menu dans l'ordre : None,
  Pb 1-2, Pb 3-4, Pb 5-6, Pb 7-8, Monitor Bus, Line 3-4, HP A, HP B).
- Brut supprimé (23 224 trames dans le .ctl).

### 21-monitoring.ctl.pcapng
- Adresse device : Bus 001 Device **014**
- Panneau MONITORING : DIM on/off → CUT on/off → MONO on/off → ØL on/off →
  ALT SPK ENABLE on + ALT on/off + enable off → DIM LEVEL sweep →
  ALT SPK TRIM sweep.
- Résultat : DIM/CUT/MONO/ØL/ALT = booléens sub 04 (contrôles 6/7/5/4/8,
  instance 0, ACK) ; CUT et ALT doublés d'un `0x13` (LED physiques probables).
  DIM LEVEL = sub 06 contrôle 3 ; ALT SPK TRIM = sub 06 contrôle 6 inst 2 ;
  ALT SPK ENABLE = sub 07 contrôle 5 + contrôle 0x20 (inst 2/3). Registres
  complets dans PROTOCOL.md.
- Brut supprimé (143 132 trames dans le .ctl).

### 19-direct-to-bus-(playback 1-2).ctl.pcapng
- Adresse device : Bus 001 Device **014**
- DIR de Playback 1-2 (« DIRECT TO BUS MON L-R »), off au départ : ON (t≈4,7)
  puis OFF (t≈12,7).
- Résultat : DIR = **émulation host** (aucun booléen). ON = tranche
  court-circuitée : pb 1-2 à 0 dB (unité) dans le mix, sends forcés -∞.
  OFF = restauration de l'état stocké de la tranche (fader + sends, d'où les
  0 dB réécrits en couches 4/5 et 6/7 — sends laissés à 0 dB par les captures
  précédentes). Réserve : fader déjà à 0 dB → « unité » vs « valeur fader »
  indistinguables ici (re-tester un jour avec fader à −20 dB).
- Brut supprimé (18 204 trames dans le .ctl).

### 17-buttons-hardware-(gain 0-max Channel 1).ctl.pcapng
- Adresse device : Bus 001 Device **014**
- Knob de gain **physique** du canal 1 : 0 → max, SSL 360 ouvert.
- Résultat : **zéro message de contrôle** — uniquement vumètres + keepalive.
  Le gain de préampli est interne au device, jamais exposé sur l'USB (ni
  set ni notification). → À refaire en **17b** avec les contrôles physiques
  qui, eux, apparaissent dans SSL 360 : gros knob monitor, CUT/ALT/TALK,
  volumes casques.
- Brut supprimé (21 254 trames dans le .ctl).

### 17b-buttons-hardware.ctl.pcapng
- Adresse device : Bus 001 Device **014**
- Contrôles physiques, SSL 360 ouvert : monitor level 0 → max, boutons CUT /
  ALT / TALK, volumes casques A et B 0 → max.
- Résultat : **les knobs de volume physiques (monitor, casques) sont muets sur
  l'USB** — internes au device, en série après la matrice (comme le gain).
  Les **boutons** notifient en IN sub 05 (CUT=ctrl 7, ALT=ctrl 8 — refusé car
  ALT SPK off, TALK=ctrl 9 + DIM auto) et le **host pilote les LED** via 0x13
  (0x0c/0x0d/0x0e). Premières notifications spontanées observées.
- Brut supprimé (49 482 trames dans le .ctl).

### 24-profile.ctl.pcapng
- Adresse device : Bus 001 Device **014**
- APPLY DEFAULTS (t≈10) puis LOAD du profil ssl12 (jusqu'à t≈25).
- Résultat : **validation finale** — 2 105 messages OUT, exclusivement les
  4 familles connues (sub 06 ×1938, 07 ×78, 04 ×65, 08 ×24), aucun type
  inconnu. Les profils sont purement host-side. Reste une poignée de contrôles
  anonymes (réglages SETTINGS/horloge, listés dans PROTOCOL.md).
- Brut supprimé (42 322 trames dans le .ctl).

### 23-user-buttons.ctl.pcapng
- Adresse device : Bus 001 Device **014**
- Réassignation des 3 boutons USER, un à la fois (menu : DIM, CUT, MONO SUM,
  ALT, INVERT PHASE LEFT, TALKBACK, 360° SSL12 GUI).
- Résultat : sub 08, contrôle **12**, instance = bouton (0/1/2), valeur =
  fonction (0–5 observés). Défauts : CUT/ALT/TALKBACK.
- **Correction du 31/08/2026** : la valeur n'est *pas* le rang dans le menu.
  Les six réassignations, prises dans l'ordre du menu, sortent
  `0, 1, 2, 4, 3, 5` — ALT et INVERT PHASE LEFT sont échangés sur le fil.
  L'entrée d'origine avait lu ces valeurs comme si elles étaient croissantes.
  Confirmé à l'oreille par un utilisateur : la valeur 3 inverse la phase du
  canal gauche. Ordre réel : 0=DIM, 1=CUT, 2=MONO SUM, 3=INVERT PHASE LEFT,
  4=ALT, 5=TALKBACK.
- Brut supprimé (37 780 trames dans le .ctl).

### 22-hpf-phase-(analogue 3).ctl.pcapng
- Adresse device : Bus 001 Device **014**
- Analogue 3 : passe-haut on/off, puis Ø (polarité) on/off.
- Résultat : HPF = sub 04 contrôle **2**, Ø = sub 04 contrôle **15**
  (instance 2 = canal 3), échos ACK habituels.
- Brut supprimé (15 532 trames dans le .ctl).

### 16b-talkback.ctl.pcapng
- Adresse device : Bus 001 Device **014**
- TALK on (t≈2,4) → TALKBACK TRIM off → max **pendant TALK actif** → TALK off
  (t≈21,7).
- Résultat : **aucun message pour le trim, même TALK actif** — le talkback
  (micro intégré, routage, niveau) est entièrement interne au firmware. Le
  trim de SSL 360 = réglage local/profil, jamais transmis sur ce canal.
- Brut supprimé (24 960 trames dans le .ctl).

### 16-talkback.ctl.pcapng
- Adresse device : Bus 001 Device **014**
- Bouton TALK de SSL 360 : on (t≈4,2) / off (t≈10,0), puis TALKBACK TRIM
  0 → off → max → 0 dB (t>10, TALK off).
- Résultat : TALK = sub 04 contrôle **9** (confirme la 17b), LED via 0x13
  groupe 0x0e écrite par le host, écho device TALK + DIM (auto-dim firmware).
  **Le TRIM n'a émis aucun message** (TALK étant off) — et TALK on n'écrit
  aucune cellule de matrice : routage talkback interne au firmware. À tester
  en 16b : bouger le trim **pendant** que TALK est actif.
- Brut supprimé (44 036 trames dans le .ctl).

### 09-line-inst.ctl.pcapng
- Adresse device : Bus 001 Device **014**
- Switch LINE/INST de l'entrée Analogue 3, 2 allers-retours.
- Résultat : sub 04, contrôle **3**, instance **2**, val 0/1, écho IN sub 05.
- Brut de 100 Mo supprimé (15 535 trames).
