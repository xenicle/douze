# Protocole de contrôle SSL 12 — notes de rétro-ingénierie

> **Domaine public — [CC0 1.0](LICENSES/CC0-1.0.txt).** Ce fichier décrit des
> FAITS sur un appareil : reprenez-les sans permission, sans crédit et sans rien
> demander — driver noyau, plugin ALSA, autre outil, wiki. C'est le but. Le CODE
> de ce dépôt, lui, est sous AGPLv3 (cf. [COPYING.md](COPYING.md)).
>
> *Public domain (CC0 1.0). This documents facts about a device: reuse it freely,
> no attribution required. The repository's code is AGPLv3.*
>
> Projet **sans aucun lien avec Solid State Logic**. Ces notes décrivent ce qui a
> été OBSERVÉ sur le bus USB entre l'application du fabricant et une carte
> possédée par l'auteur ; elles ne contiennent ni code ni firmware du fabricant,
> et rien n'a été décompilé. « SSL » et « SSL 12 » appartiennent à leurs
> propriétaires.

État : **protocole exploité en production.** `tools/sslctl.py` et le démon
`tools/douze.py` pilotent réellement la carte — mixette, sends, monitoring,
préamplis, loopback, horloge, vumètres, profils. Ce fichier documente le
transport et les trames identifiées ; il ne décrit PAS tout ce que
l'implémentation sait faire, et la référence à jour est `sslctl.py`.

Les zones encore notées « inconnu » plus bas le sont vraiment : elles
n'apparaissent dans aucune commande utile observée à ce jour.

## Topologie USB (relevé 16/08/2026)

La SSL 12 embarque un hub Microchip 0424:2422 (2 ports) :

```
SSL 12 (câble unique)
└── Hub interne 0424:2422
    ├── Port 1 : 31e9:0024 « SSL Control I/F »  ← canal SSL 360
    └── Port 2 : 31e9:0005 « SSL 12 »           ← audio UAC2 (snd-usb-audio)
```

- Numéro de série commun : `S12-xxxxxx` (le même sur les deux périphériques)
- Firmware audio observé : `bcdDevice 1.44` (MAJ du 16/08/2026, avec SSL 360 V2 ;
  était 1.41 avant) ; control I/F : `bcdDevice 10.00` (inchangé par la MAJ, tout
  comme les endpoints)

## SSL Control I/F (31e9:0024)

- Full Speed (12 Mbps), 1 configuration, 1 interface (classe/sous-classe/protocole 255)
- `bMaxPacketSize0` = 8
- Endpoints :
  - `EP 0x81 IN`, bulk, 64 octets — notifications / réponses device → host
  - `EP 0x02 OUT`, bulk, 64 octets — commandes host → device
- Aucun driver noyau ne s'y attache → accès direct libusb sans detach

## Format des messages — VALIDÉ sur la capture 02 (2 988 messages, 0 erreur)

### Couche transport

- **OUT (EP 0x02)** : les messages `ff …` sont écrits tels quels (une ou
  plusieurs trames par transfert bulk). Au tout début, le driver envoie un
  transfert de **256 octets à zéro** (flush/resynchronisation ?), seul trafic
  qui ne suit pas le framing.
- **IN (EP 0x81)** : chaque **paquet USB de 64 octets** commence par un en-tête
  de 2 octets `31 xx` (`xx` = `0x60` en général, `0x00` parfois — rôle à
  préciser), suivi d'un **flux d'octets continu** : les messages `ff …`
  enjambent librement les frontières de paquets. Un paquet réduit à `31 60`
  seul = « rien à dire » (heartbeat). → Pour décoder l'IN il faut retirer
  l'en-tête de chaque tranche de 64 o puis réassembler le flux.

### Couche message (identique dans les deux sens)

```
ff | opcode (1 o) | len (1 o) | payload (len octets) | checksum (1 o)
```

- `checksum = (opcode + len + somme(payload)) & 0xff`
- Les réponses reprennent l'opcode de la requête ; exception : les requêtes
  `0x6b` reçoivent leurs réponses/notifications en `0x6c` (opcode+1).

### Séquence d'init de SSL 360 (capture 02, t≈2,07 s)

| t (s) | Sens | Message | Note |
|---|---|---|---|
| 2.073 | OUT | 256 octets `00` | flush |
| 2.074 | IN | 18 × `ff 04 02 {9d\|a0} 01` | en alternance 9d/a0 — vidage de tampon ? à recouper |
| 2.091 | OUT→IN | `ff 01 00 01` → rép. `34 12` | magic/identité ? (0x1234 LE) |
| 2.102 | OUT→IN | `ff 02 00 02` → rép. `12 a1` | ? |
| 2.113 | OUT→IN | `ff 05 00 05` → rép. `00` | ? |
| 2.150 | OUT→IN | `ff 4b 00 4b` → rép. `51 81 00 00` | version fw ? à recouper avec 1.44 |
| 2.168 | OUT→IN | `ff 4e 00 4e` → rép. `04 00` | ? |
| 2.181 | OUT | 36 × `ff 13 04 01 NN 00 00`, NN = 0x00…0x23 | souscription/activation par groupe ? |
| ensuite | OUT | 2 408 × `0x6b` (payloads variés) | lecture/écriture de l'état complet |

### Opcodes observés (capture 02)

| Opcode | Sens | Occurrences | Rôle supposé |
|---|---|---|---|
| `0x01`, `0x02`, `0x05`, `0x4b`, `0x4e` | OUT (rép. IN même opcode) | 1 chacun | handshake / identification |
| `0x13` | OUT | 36 | init par groupe (payload `01 NN 00 00`) |
| `0x1b` | OUT | 193 | keepalive toutes les 150 ms (payload = seq 0–3) |
| `0x14`, `0x1f` | OUT | 1 chacun | fin d'init ? |
| `0x6b` | OUT | 2 408 | **opération sur paramètre** (get/set — cœur du protocole) |
| `0x6c` | IN | 321 | réponse/notification paramètre (dont flux vumètres) |
| `0x04` | IN | 18 | inconnu (paires `9d 01`/`a0 01`) |

### Structure des messages paramètre `0x6b` (OUT) / `0x6c` (IN)

Payload : `sous-op (1 o) | 00 | contrôle (2 o LE) | instance (2 o LE) | valeur`

L'adresse d'un paramètre est un couple (contrôle, instance) : le contrôle dit
« quel type de réglage » (ex. 9 = fader master de bus), l'instance dit lequel
(ex. bus 0 = Monitor, 4 = HP A, 6 = HP B — indexation par paires stéréo
apparente ; captures 04/05/06).

| Sous-op | Sens | Valeur | Rôle (hypothèse) | Exemple |
|---|---|---|---|---|
| `0x06` | OUT | u32 LE | **set** paramètre continu — pas d'ACK | `06 00 09 00 00 00 VV VV VV VV` (capture 04) |
| `0x04` | OUT | u8 | **set booléen** — ACK via IN sub 05 | `04 00 01 00 00 00 01` = 48V canal 1 ON (capture 08) |
| `0x05` | IN | u8 + `01 01` | **notification/écho d'état booléen** | `05 00 01 00 00 00 01 01 01` |
| `0x07` | OUT | u8 | **set booléen « modes »** (3ᵉ registre, sans ACK) : contrôle 2 = MONO bus, 4 = CUT bus, 7 = FOLLOW MIX 1-2 ; instance = bus (4 = HP A) — capture 20 | `07 00 07 00 04 00 01` |
| `0x08` | OUT | u16 LE | **set enum** (sélecteurs) : contrôle 11 = LOOPBACK SOURCE (0 = None, 1/2/… = position dans le menu) — capture 13 | `08 00 0b 00 00 00 01 00` |
| `0x03` | OUT | — | **get** (lecture) ? | `03 00 0c 00 00 00` |
| `0x01` | OUT | — | ? (payload `01 00` seul, sans param) | |
| `0x09`, `0x11` | IN | variable | notifs (09 = vumètres, 11 = infos horloge : `44 ac`=44100, `80 bb`=48000, `00 ee 02`=192000 vus) | |

**Vumètres (0x6c sub 09)** — payload `09 00 01 00 00 00 | count u16 | count×u16`
(peak s16, 32767 = 0 dBFS ; ~20 trames/s ; le flux démarre sur un **second**
`ff 05` après le handshake, et s'arrête si le host ne lit pas l'EP IN).
Index calibrés les 17/08/2026 (tests aux tonalités, v2 corrigée) :
0–3 = Analogue 1–4 ; 12/13 = Pb 1-2 ; **bus contigus 14–21** : 14/15 = Line
3-4, 16/17 = HP A, 18/19 = HP B, 20/21 = Monitor (post-master : le meter suit
le master fader du bus) ; 22/23 = Pb 3-4, 24/25 = Pb 5-6, 26/27 = Pb 7-8
(présumé) ; 28 = ? ; 4–11 = silencieux dans tous les tests (loopback compris),
probablement réservés.

- **ACK asymétrique** : les sets continus (sub 06) ne déclenchent aucune
  réponse (capture 04 : 153 sets, rien) ; les sets booléens (sub 04) reçoivent
  un écho d'état IN sub 05 en ~15-25 ms (capture 08).
- **Encodage des volumes/faders** (capture 04, param 9 = master fader monitor) :
  **gain linéaire u32 LE, 0 dB = 2²⁵ = 0x0200_0000**, soit
  `valeur = 2²⁵ × 10^(dB/20)`. Vérifié : +12 dB (butée haute) → 133 582 600
  observé vs 133 583 059 théorique (0,0003 %). Bas de course ≈ -85 dB avant
  le décrochage -∞ (probablement valeur 0).

**Prochaine étape** : cartographier les `param_id` (captures 05+ : un contrôle
à la fois) et préciser les sous-ops. Pas de protobuf à ce niveau : framing
maison compact ; la mention protobuf de l'About concerne sans doute le lien
GUI ↔ SSL360Core.

## Faits établis par les captures 01/01b/03

- **Pas de séquence de fermeture** (capture 03) : ni le quit de la GUI ni le
  kill de SSL360Core n'envoient quoi que ce soit — le trafic s'arrête net
  (suivi d'un reset USB dû au relâchement du passthrough). `sslctl` pourra se
  déconnecter sans cérémonie ; le quit de la GUI ne génère aucun trafic USB
  (le dialogue GUI ↔ Core est purement local).

- **Le firmware n'initie jamais rien** : SSL360Core tué, zéro trame de contrôle
  (capture 01). Tout part du host ; pour recevoir les notifications, le host
  garde des lectures bulk IN en attente permanente.
- **C'est `SSL360Core.exe`** (service d'arrière-plan, tourne même GUI fermée)
  qui tient le dialogue : keepalive `0x1b` toutes les 150 ms + flux de
  vumètres `0x6c` ~25×/s (mots 16 bits little-endian répétés par canal).
  C'est le **bruit de fond à soustraire** dans toutes les captures.

## Piste protobuf (non confirmée à ce stade)

Le framing observé est maison et compact — pas de protobuf apparent dans les
trames USB. La mention protobuf de l'About concerne peut-être la communication
SSL 360 (GUI) ↔ SSL360Core. Plan B toujours valable si un encodage résiste : les binaires .NET de SSL 360 (install Windows de la
VM) sont lisibles avec `ILSpy`/`dnSpy` — les contrats
`[ProtoContract]`/`[ProtoMember]` (protobuf-net v2.4.1, crédité dans l'About)
et plus généralement le code qui construit les trames `ff …` donneraient les
opcodes et la carte des paramètres sans deviner.
- Trafic périodique (keepalive ?) :
- Séquence d'init de SSL 360 (capture 02) :

## Surface de contrôle SSL 360 V2 (relevé écran 16/08/2026)

Inventaire de l'onglet SSL 12 de SSL 360 V2 — c'est l'ensemble des paramètres
que le protocole doit pouvoir encoder. Le mixer V2 est multi-bus : chaque canal
alimente 4 bus (Monitor via le fader principal, plus 3 sends HP A / HP B /
Line 3-4).

**Canaux** (vues ANALOGUE IN / DIGITAL IN / PLAYBACK RTNS / AUX MASTERS) :

- Entrées : Analogue 1–4, Talkback, Playback 1-2 … 7-8 (returns inactifs grisés)
- Bus masters : Headphone A, Headphone B, Line 3-4, Monitor Bus

**Par entrée analogique** :

| Groupe | Contrôles |
|---|---|
| Préampli | 48V (par canal à l'écran), LINE/INST, filtre passe-haut, Ø (polarité), gain (meter 0→40) |
| Send HP A | on/off (case), PAN, niveau (−∞ → +12 dB), MUTE |
| Send HP B | idem |
| Send Line 3-4 | idem |
| Tranche | PAN L/R, SOLO, CUT, fader (+12 → −∞), lien stéréo (icône ∞ en bas de tranche) |

**Talkback** : bouton TALK, TALKBACK TRIM (fader dédié), pas de sortie vers le
master (« NO OUTPUT TO MASTER »), sends HP A / HP B / Line 3-4.

**Playback returns** : DIRECT TO BUS avec destination fixe par return
(1-2 → MON L-R, 3-4 → LINE 3-4, 5-6 → HP A, 7-8 → HP B) + bouton DIR ;
Playback 1-2 a une tranche complète (sends, pan, solo, cut, fader).

**Bus masters HP A / HP B / Line 3-4** : SENDS POST, FOLLOW MIX 1-2, AFL, CUT,
MONO, fader, meters. **Monitor Bus** : MASTER FADER, meters.

**Colonne monitoring / device** :

- MONITORING : DIM, CUT, MONO, ØL, ALT, ALT SPK ENABLE, DIM LEVEL (knob),
  ALT SPK TRIM (−12 → +12)
- USER : assignation des boutons physiques CUT / ALT / TALK
- CONTROL : SAMPLE RATE, CLOCK, LOOPBACK SOURCE (menu, « None » par défaut)
- PROFILE : LOAD / SAVE / SAVE AS / APPLY DEFAULTS (à déterminer : stockés
  côté device ou fichiers côté host ?)
- DEVICE : SETTINGS, I/O Mode
- MOUSE WHEEL (Scroll Mixer / Scroll Controls) : réglage purement UI,
  probablement aucun trafic USB — utile comme « contrôle témoin »

## Carte des contrôles

| Contrôle | Trame(s) OUT | Réponse / notif IN | Encodage valeur | Capture source |
|---|---|---|---|---|
| Volume monitor (master fader) | `0x6b` sub 06, contrôle **9**, instance **0** | aucune | u32 LE gain linéaire `2²⁵ × 10^(dB/20)`, -∞ → +12 dB | 04 |
| Volume casque A | idem, contrôle **9**, instance **4** | aucune | idem | 05 |
| Volume casque B | idem, contrôle **9**, instance **6** | aucune | idem | 06 |
| Volume canal Analogue N (« gain » à l'écran) | `0x6b` sub 06, contrôle **1**, instance **8+N** ; **double write** simultané vers instance **38+N** (rôle à élucider — FOLLOW MIX ?) | aucune | u32 LE gain linéaire std, GUI : off / −84,8 → +12 dB | 07, 07b |
| 48V (par canal !) | `0x6b` sub 04, contrôle **1**, instance **N** (0–3 = Analogue 1–4), val 0/1 | écho IN sub 05 | booléen u8 | 08 |
| Line/Inst (Hi-Z) | `0x6b` sub 04, contrôle **3**, instance **N** (2 = Analogue 3), val 0/1 | écho IN sub 05 | booléen u8 | 09 |

**Registres identifiés** (contrôle → fonction, par famille) :

| Famille | Contrôle | Fonction | Instance |
|---|---|---|---|
| sub 06 (u32 gain) | 1 | matrice de gains | 30×couche + slot |
| | 3 | DIM LEVEL | 0 |
| | 6 | ALT SPK TRIM | 2 |
| | 9 | fader master de bus | bus : 0=Monitor, 2=Line 3-4 (confirmé au meter 17/08), 4=HP A, 6=HP B |
| sub 04 (bool, ACK) | 1 | 48V | canal 0–3 |
| | 2 | filtre passe-haut | canal 0–3 |
| | 3 | Line/Inst | canal (2–3) |
| | 0x0f | polarité Ø | canal 0–3 |
| | 4 | ØL (polarité monitoring) | 0 |
| | 5 | MONO monitoring | 0 |
| | 6 | DIM | 0 |
| | 7 | CUT monitoring | 0 |
| | 8 | ALT | 0 |
| sub 07 (bool, sans ACK) | 2 | MONO bus | bus |
| | 4 | CUT bus | bus |
| | 5 | ALT SPK ENABLE | 2 |
| | 7 | FOLLOW MIX 1-2 | bus |
| | 0x20 | lié à ALT SPK (sorties 3/4 ?) | 2, 3 |
| sub 08 (enum u16) | 11 | LOOPBACK SOURCE | 0 |
| | 12 | boutons USER (fonction) | bouton 0–2 |
| *(non identifiés, vus dans le dump profil — capture 24)* | sub 07 : 0x0d, 0x0e, 0x1f ; sub 08 : 0x0a, 0x10, 0x1a ; sub 06 : 0x08, 0x1c | réglages SETTINGS / horloge / I/O mode probables | |

L'opcode `0x13` (payload `01 groupe état`) double certains booléens : CUT →
groupe 0x0c, ALT → groupe 0x0d — vraisemblablement les **LED des boutons
physiques** (les 36 messages `0x13` de l'init = synchro complète des LED/HW).

Les groupes LED sont donc indexés par **bouton de façade** (0x0c = CUT,
0x0d = ALT, 0x0e = TALK), pas par fonction — ça ne se voit pas tant que les
boutons portent leur fonction d'usine. Sur un bouton réassigné (sub 08
contrôle 12, cf. ci-dessous), le host doit allumer la LED du bouton qui porte
la fonction notifiée : `sslctl.led_group_for()`. **Vérifié sur le matériel** le
31/08/2026 (retour d'un utilisateur avec boutons réassignés : chaque LED
s'allume bien sous le bouton pressé), ce qui confirme que le device notifie la
fonction appliquée et non le bouton — comme le montrait déjà TALK, qui remonte
aussi DIM (capture 17b).

Reproduit le même jour sur la carte de référence : bouton ALT réassigné en
INVERT PHASE LEFT, l'appui remonte `invert-l` (sub 05, contrôle 4) et rien
d'autre — ni `alt`, ni le rang du bouton. C'est ce qui rend `led_group_for()`
nécessaire : la notification ne dit pas quel bouton a été pressé.

⚠️ **Un bouton portant ALT ne notifie RIEN tant qu'ALT SPK ENABLE est éteint**
(retour utilisateur du 31/08/2026, boutons par ailleurs tous fonctionnels) : le
firmware n'a pas de sorties à commuter, il n'émet donc aucun écho sub 05 — et
la LED reste éteinte, le host n'ayant rien à allumer. Ce n'est pas un défaut du
host : c'est le prérequis que SSL 360 impose aussi (capture 21, où l'enable est
activé avant de jouer avec ALT). Exposé depuis dans `sslctl altspk` et la GUI.

Note : le champ « contrôle » semble être un id de fonction **par famille** (les
booléens sub 04 ont leur numérotation : 1 = 48V, 3 = Line/Inst ; les continus
sub 06 la leur : 1 = volume canal, 9 = master bus). Les instances indexent
canal ou bus. À raffiner au fil des captures.

### Modèle central : la matrice de gains (contrôle 1) — établi captures 04–12

Le device n'a **ni faders, ni pans, ni mutes, ni solos** : il expose une
**matrice de gains crosspoint** source → bus. SSL 360 traduit chaque geste de
mixer en écritures de cellules (sub 06, contrôle 1) :

```
instance = 30 × couche + slot_source        (valeur = gain linéaire, 0 dB = 2²⁵, 0 = -∞)

couches (0–7) : 0 = Mix 1-2 L   1 = Mix 1-2 R      (carte COMPLÈTE —
                2 = Line 3-4 L  3 = Line 3-4 R      6/7 = HP B validé à
                4 = HP A L      5 = HP A R          l'oreille le 17/08/2026,
                6 = HP B L      7 = HP B R          2/3 par élimination)
— HP A = couches 4/5 confirmé (captures 18/18b). **Sends = pures écritures de
matrice** (aucun booléen device) : cellule = niveau × pan, mute = 0. Vérif
numérique (18b) : knob +12 dB → cellule +8,99 dB = +12 − 3 (pan centre) ;
plancher GUI −84,8 → −87,8 dB en cellule. La règle générale : **valeur de
cellule (dB) = fader/niveau (dB) + pan (dB, centre = −3)**, 0 dB = 2²⁵.
(Dans la 18 d'origine, tout restait à 0 : niveau à off puis mute enclenché.)

slots sources : 0/1 = Playback 1-2 L/R   2/3 = Pb 3-4   4/5 = Pb 5-6
                6/7 = Pb 7-8             8–11 = Analogue 1–4
                14–21 = ? (mono, restaurés à -3 dB par le dé-AFL)
                24–28 = **retours de bus comme sources** (26/27 = HP A L/R,
                confirmé par l'AFL qui les route à 0 dB vers le monitor) —
                le device fait donc aussi du routing bus-à-bus via la matrice
```

Traductions observées :
- **Fader mono** (Analogue N) : écrit la même valeur en couche L et R du bus
  (cellules `8+N` et `38+N` pour Mix 1-2).
- **Fader stéréo** (Playback 1-2) : slot L → couche L, slot R → couche R
  (cellules 0 et 31).
- **Pan** (capture 12) : loi à **puissance constante, −3 dB au centre** —
  L et R complémentaires, extrêmes = 0 dB / -∞.
- **Cut** : 0 dans les cellules du canal ; **Solo** : 0 dans toutes les
  cellules des autres canaux (toutes couches) ; restauration ensuite.
- Le gain affiché « fader + pan » = produit des deux, calculé par le host.

Conséquence sslctl : il faudra maintenir l'état logique (fader/pan/mute/solo)
côté outil et compiler vers la matrice, comme SSL 360.

Acquis de la capture 11 (solo/cut) :

- **Valeur 0 = -∞ (off)** — confirmé par le CUT (écrit 0, restaure ensuite).
- **Cut et solo n'existent pas côté device** : émulation host par réécriture
  de gains. L'état mute/solo vit dans SSL 360 (profil), le device ne stocke
  que les gains résultants.
- L'espace d'instances du contrôle 1 est **multi-couches** : le balayage du
  solo écrit aussi les instances 0x44, 0x62, 0x80, 0x9e, 0xbc, 0xda (stride
  30 : slot canal 1 dans ~8 couches de mix) et des plages 0x1f–0x3b (couche
  « miroir » du double write). Hypothèse : couche 0 = mix principal,
  couches suivantes = sends HP A / HP B / Line 3-4 (voire L/R séparés).
  Cartographie précise à faire avec les captures 18–20.
| Fader Playback 1-2 | `0x6b` sub 06, contrôle **1**, instance **0** ; double write instance **31** | aucune | u32 LE gain linéaire std | 10 |
| Cut (mute) | **pas de paramètre device** : le host écrit 0 (= -∞) dans les instances fader du canal, puis restaure | aucune | émulation host-side | 11 |
| Solo | **pas de paramètre device** : le host écrit 0 dans les faders de tous les *autres* canaux, sur toutes les couches de mix, puis restaure | aucune | émulation host-side | 11 |
| Pan | **pas de paramètre device** : réécrit les cellules L/R du canal (loi -3 dB centre) | aucune | émulation host-side | 12 |
| Loopback source | `0x6b` sub 08, contrôle **11**, instance 0 | aucune | enum u16, ordre du menu : 0=None, 1=Pb 1-2, 2=Pb 3-4 (vérifiés) ; 3=Pb 5-6, 4=Pb 7-8, 5=Monitor Bus, 6=Line 3-4, 7=HP A, 8=HP B (présumés) | 13 |
| Routing sorties | | | | 14 |
| Mode 4K | | | | 15 |
| Contrôles physiques — knobs (gain, monitor, casques A/B) | — | **rien** : gérés en interne, invisibles sur l'USB (atténuateurs en série après la matrice) | — | 17, 17b |
| Contrôles physiques — boutons CUT/ALT/TALK | LED pilotées par le host : `0x13` groupes 0x0c/0x0d/0x0e | notif IN sub 05 (contrôles 7/8/9) ; TALK déclenche aussi DIM (auto-dim device) | booléens | 17b |
| Talkback (TALK) | `0x6b` sub 04, contrôle **9**, instance 0 (+ LED `0x13` groupe 0x0e) | écho IN sub 05 : TALK **et** DIM (auto-dim firmware) | booléen u8 | 16, 17b |
| Send HP A (niveau, pan, mute) | pures écritures matrice couches 4/5 (cellule = niveau × pan, mute = 0) ; aucun booléen | aucune | u32 LE gain linéaire std | 18, 18b |
| Direct-to-bus (DIR) | pas de paramètre device : ON = source à 0 dB dans le bus cible + sends -∞ ; OFF = restauration de la tranche | aucune | émulation host-side | 19 |
| FOLLOW MIX 1-2 (bus) | `0x6b` sub 07, contrôle **7**, instance = bus | aucune | booléen u8 | 20 |
| CUT bus master | `0x6b` sub 07, contrôle **4**, instance = bus | aucune | booléen u8 | 20 |
| MONO bus master | `0x6b` sub 07, contrôle **2**, instance = bus | aucune | booléen u8 | 20 |
| SENDS POST | pas de paramètre device : recompilation des cellules de send par le host | aucune | émulation host-side | 20 |
| AFL | pas de paramètre device : mix principal → -∞ + retour bus (slots 26/27) → 0 dB, puis restauration | aucune | émulation host-side | 20 |
| Monitoring DIM/CUT/MONO/ØL/ALT | `0x6b` sub 04, contrôles 6/7/5/4/8, instance 0 (+ `0x13` LED pour CUT/ALT) | écho IN sub 05 | booléens u8 | 21 |
| DIM LEVEL | `0x6b` sub 06, contrôle **3**, instance 0 | aucune | u32 LE gain linéaire std | 21 |
| ALT SPK ENABLE / TRIM | enable : sub 07 ctrl 5 + ctrl 0x20 (inst 2/3), **les trois dans cet ordre** ; trim : sub 06 ctrl **6** inst 2 | aucune | bool / u32 gain | 21 |
| Passe-haut / polarité Ø | `0x6b` sub 04, contrôles **2** / **15**, instance = canal | écho IN sub 05 | booléens u8 | 22 |
| Boutons USER (assignation) | `0x6b` sub 08, contrôle **12**, instance = bouton (0/1/2) | aucune | enum : 0=DIM, 1=CUT, 2=MONO SUM, 3=INVERT PHASE LEFT, 4=ALT, 5=TALKBACK, 6=360° GUI (présumé) — **pas** l'ordre du menu, qui affiche ALT avant INVERT | 23 |
| Profils (LOAD / APPLY DEFAULTS) | **côté host uniquement** : rejoue l'état complet via les 4 familles connues (~2 100 messages) — aucun stockage device | échos habituels | — | 24 |
