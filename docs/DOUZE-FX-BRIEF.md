# Douze FX — brief de passation (17/08/2026)

> **MAJ 17/08/2026 (session delestor-proto)** : vérifications JUCE 8→9 + licences
> faites (§ « Framework »), point bloquant JACK identifié et résolu
> (§ « I/O audio »), **portée élargie** — Douze FX ne traite plus seulement
> Analogue 1 mais *partout où c'est possible* dans Douze (§ « Points
> d'insertion ») — et **jalon 1 réalisé** : la cible `fx/` existe, build propre,
> chaîne {Clear, Mc} hébergée dans le graphe PipeWire (§ « Jalon 1 »).
>
> **MAJ 17/08/2026 (soir)** : le code a été RAPATRIÉ dans `douze/fx/` — ce brief
> et le moteur vivent désormais dans le même dépôt, les chemins vers
> `delestor-proto` ci-dessous ne valent plus que comme historique (seul le
> SCANNER `delestor_engine --scanone` reste emprunté). Jalons 2 et 3 largement
> faits. Un brief est un document de DÉCISIONS, pas un état des lieux : quand il
> contredit le code, c'est le code qui a raison — les décisions révisées depuis
> sont marquées comme telles à leur place plutôt que réécrites en silence.

## Quoi

**Douze FX** = mini-app standalone JUCE qui héberge une chaîne de VST3 pour
traiter le **micro** (SSL 12, entrée Analogue 1) dans le graphe PipeWire, en
remplacement d'Easy Effects. Compagnon de **Douze** (GUI du projet
`~/workspace/douze` : démon Python sur http://localhost:1212 qui pilote le
mixer hardware de la SSL 12 — protocole USB rétro-ingéniéré, voir
`douze/PROTOCOL.md`).

Nouvelle cible dans ce repo (ex. `fx/`), **réutilise l'engine JUCE existant**
(scan `KnownPluginList`, `AudioProcessorGraph`, yabridge validé) mais PAS le
SHM : ici l'I/O audio passe par **JACK** (émulation PipeWire, `pw-jack`) —
l'app apparaît comme un nœud du graphe.

## Chaîne cible du proto (demandée par Tony)

1. **« Clear »** — plugin Windows via **yabridge** (stubs dans `~/.vst3/yabridge`,
   `yabridgectl sync` déjà fonctionnel côté engine console)
2. **« Mc »** — un compresseur VST3 natif (développé à côté)

## Architecture décidée

- JUCE `AudioDeviceManager` backend **JACK** → dans PipeWire : capture SSL
  (source « SSL 12 Pro ») → **Douze FX** → source/sink virtuel que les applis
  utilisent. Câblage auto par l'app ou règles pw-link.
- **Rack JSON** : liste ordonnée de plugins + états (`getStateInformation`
  base64), chargé au boot, sauvé à chaud. Emplacement suggéré :
  `~/.config/douze-fx/rack.json`. (Structure élargie aux **bandes** : cf. §
  « Points d'insertion ».)
- **GUIs locales** : ouvrir les vrais éditeurs (`createEditorIfNeeded` dans une
  fenêtre JUCE) — trivial en local, PAS le problème GUI-distante de Delestor.
- Contrôle à distance simple (bypass, load rack) : HTTP local minimal ou OSC —
  pour intégration future dans les **profils** de Douze (le démon Douze
  saura démarrer/câbler Douze FX par profil).

## Points d'insertion — « FX partout où c'est possible » (17/08/2026)

Portée élargie par Tony : **à terme, pouvoir appliquer Douze FX partout où
c'est possible dans Douze**, pas seulement sur Analogue 1.

**La ligne de partage est nette** : la matrice de gains de la SSL 12 est
*dans le device* (crosspoint 8 couches × slots, cf. `douze/PROTOCOL.md`).
Tout ce qui reste hardware est in-FX-able. Donc **« possible » = « le signal
passe par le PC »**, soit exactement deux familles de points d'insertion
(relevé sur le graphe live le 17/08/2026) :

| Famille | Ports PipeWire | Ce qu'on peut y faire |
|---|---|---|
| **Capture (16 canaux)** | `alsa_input.…pro-input-0:capture_AUX0…15` (AUX0-3 = Analogue 1-4) | insérer une bande sur n'importe quel canal/paire → publier une source virtuelle `douze-fx.<nom>` (le cas micro→Discord n'est qu'une instance) |
| **Playback (4 paires)** | `ssl12.pb12 / pb34 / pb56 / pb78` (sinks du `99-ssl12-sinks.conf`) | insérer une bande devant n'importe quelle paire (`douze-fx.pb34` → `ssl12.pb34`) : EQ/limiteur de monitoring, correction de casque, FX sur la musique ou sur Discord = remplacement complet d'Easy Effects |

**Ce qui restera impossible, et qu'il faut assumer dans Douze** : le monitoring
direct hardware (Analogue N → HP A/B via la matrice, latence ~0). Activer une
bande FX sur une entrée implique de **couper la cellule de monitoring direct**
de ce canal (sinon on entend sec + traité) et de renvoyer le traité par une
paire playback → aller-retour USB (quelques ms). `sslctl` sait déjà faire les
deux gestes (`route <ch> <bus> off`, faders/cellules) : **c'est le démon Douze
qui doit orchestrer les deux moitiés** — créer/câbler la bande FX *et*
réécrire la matrice. C'est le vrai travail d'intégration, plus que
l'hébergement VST3 lui-même.

**Conséquence sur l'archi** : Douze FX n'est pas une app mono-chaîne mais
**N bandes**, et on recommande **1 process par bande** (= 1 client JACK
autonome, nom de nœud propre, câblage trivial, et un plugin qui crashe ne tue
que sa bande) — exactement le modèle d'isolation process déjà validé dans
Delestor (étape 5 bis), supervisé ici par le démon Douze au lieu du
superviseur SHM. `rack.json` devient une liste de bandes
`{id, source(s), chaîne + états, destination, bypass, cellules de matrice
associées}`, et un **profil Douze = un jeu de bandes**.

## I/O audio : JACK — point bloquant identifié (17/08/2026)

`pw-jack` et `libjack.so.0` sont **absents** de la machine de Tony →
`services.pipewire.jack.enable` n'est pas activé dans sa config NixOS. Or JUCE
fait un `dlopen("libjack.so.0")`, et le `libjack2` du devShell est le *vrai*
JACK (il chercherait un `jackd` inexistant).

**Solution sans toucher à NixOS ni redémarrer PipeWire** (contrainte Easy
Effects/Vesktop) : le PipeWire système est en **1.6.6** et
`nixpkgs#pipewire.jack` est **aussi en 1.6.6**
(`/nix/store/…-pipewire-1.6.6-jack`) → lancer `douze_fx` avec ce
`libjack.so.0` via `LD_LIBRARY_PATH` (c'est exactement ce que fait `pw-jack`).
À ajouter au devShell + un petit wrapper de lancement.

**Plan B** si ça coince : backend **ALSA** de JUCE sur le PCM `pipewire` (les
confs `50-pipewire.conf` / `99-pipewire-default.conf` sont bien installées) —
zéro dépendance, mais moins de contrôle sur le nommage des nœuds/ports.

⚠️ `JUCE_JACK` vaut **0 par défaut** dans `juce_audio_devices` → à passer à 1,
avec `JUCE_JACK_CLIENT_NAME` (utile : un nom par bande).

## Framework : JUCE 9 (⚠️ décision explicite de Tony) — VÉRIFIÉ 17/08/2026

**Douze FX utilise JUCE 9** — PAS le JUCE 8.0.12 que le build engine actuel
clone. Version courante : **9.0.1** (10/08/2026 ; 9.0.0 le 21/07/2026).

### Breaking changes 8 → 9 confrontés à notre code (`engine/`)

| Breaking change (9.0.0 / 9.0.1) | Impact chez nous |
|---|---|
| `Drawable` n'hérite plus de `Component` (→ `DrawableComponent`) | **aucun** — 0 occurrence de `Drawable` |
| `Drawable::createFromSVG(XmlElement&)` supprimée | **aucun** — 0 SVG |
| `DrawableShape::getStrokeType`/`getDashLengths` (types de retour) | **aucun** |
| Multi-touch désactivé par défaut sous Windows | **aucun** (Linux) |
| Linux OpenGL : **EGL au lieu de GLX** | **aucun sur le code** (on ne lie pas `juce_opengl` ; notre stack GL est Pugl/NanoVG côté plugin, hors JUCE) → ajouter EGL/`libglvnd` au devShell par sécurité |
| 9.0.1 : zlib/libpng/libjpeg/libflac compilés en **C** | risque théorique d'ODR si on lie aussi des copies système ; échappatoire `JUCE_INCLUDE_ZLIB_CODE=0` & co. |
| 9.0.1 : chemin du package `WebBrowserComponent` | **aucun** (`JUCE_WEB_BROWSER=0`) |
| `AudioChannelSet::create9point0point4` (→ layouts VST3 `k90_4_W`…) | **aucun** (on utilise `enableAllBuses` + layouts natifs) |

Vérifié aussi sur le tag 9.0.1 : **`juce_audio_processors_headless` existe
toujours** (l'include VST3_SDK de `engine/src/scanshell.cpp` en dépend),
**`juce_JackAudio.cpp` toujours présent**, CMake ≥ 3.22 / C++17 (on est déjà en
3.22 / C++20). → **rien à corriger dans le code existant pour compiler en 9.**

### Trouvé EN COMPILANT (non listé dans BREAKING_CHANGES.md)

Ces trois-là ne sont pas dans la doc officielle mais mordent à la compilation —
à connaître avant toute migration de Delestor :

| Découverte | Détail |
|---|---|
| `AudioPluginFormatManager::addDefaultFormats()` est **`= delete`** | remplacé par les fonctions libres `juce::addDefaultFormatsToManager()` (avec UI) ou `addHeadlessDefaultFormatsToManager()` (sans UI, build console plus léger). Delestor utilise DÉJÀ la forme libre → **non impacté**. |
| `createEditorIfNeeded()` **déprécié** → `createEditorAndMakeActive()` | même implémentation, nom moins trompeur ; renvoie **nullptr** si un éditeur est déjà actif → tester `getActiveEditor()` d'abord. `engine/src/main.cpp` l'utilise : compilera encore (dépréciation, pas suppression), à moderniser le jour de la migration. |
| `ArgumentList::getValueForOption()` n'accepte `--opt valeur` que pour les options **courtes** | les longues exigent `--opt=valeur`. Piège réel : les noms de clients JACK contiennent des espaces. Douze FX a son propre mini-parseur qui accepte les deux formes. |

### Décision : checkout séparé, pas de migration globale

`fx/` prend son propre `FetchContent` sur **9.0.1** ; **Delestor reste sur
8.0.12**. Raison **technique** : 16/16 tests validés contre ce tag, aucune
raison de les rejouer (deux arbres JUCE ne coûtent que du disque).
⚠️ L'argument « frontière de licence » ne tient PAS (cf. ci-dessous : mêmes
termes des deux côtés) — c'est bien la stabilité du build qui tranche.

Corollaire honnête : `engine/src/main.cpp` fait 2 847 lignes, couplé
SHM/registre/slots → **on ne réutilisera pas le code tel quel**. On réutilise
le *savoir-faire* (scan hors-process, `enableAllBuses`, auto-wake Acustica,
fenêtre d'éditeur, watchdogs) + éventuellement le cache de scan
`~/.cache/delestor/plugins.xml`.

## Licences — vérifié le 17/08/2026 (juce.com + forum + EULA)

- **JUCE 9 est en double licence AGPL v3 / commerciale** (confirmé dans le
  `LICENSE.md` du tag 9.0.1). Douze FX étant **open source AGPLv3**, JUCE est
  utilisable gratuitement, sans plafond de revenus ni écran de démarrage imposé.
  C'est ce qui FIXE la licence de ce dépôt : l'AGPL n'est pas un choix
  philosophique ici, c'est la condition de gratuité de JUCE.
- Le tier **Starter** existe toujours après la sortie de JUCE 9, et l'EULA 9
  (17/06/2026) ne change ni les tranches ni les prix : « *JUCE 9 will retain the
  same divisions between licensing tiers, and the price for each tier will remain
  the same* », « *The JUCE 9 EULA is also remaining unchanged from the last
  iteration of JUCE 8* ». Une licence JUCE 8 ne s'évapore donc pas parce que 9
  est sorti.
- **SDK VST3 Steinberg** : voie GPLv3, compatible AGPL.

⚠️ Ces notes valent pour CE projet, qui est open source. Quiconque veut vendre un
produit basé sur JUCE doit lire l'EULA lui-même : les seuils de revenus et ce
qu'ils englobent pour un particulier ne se déduisent pas d'un résumé.

Sources : <https://juce.com/get-juce/> ·
<https://forum.juce.com/t/juce-9-no-pricing-or-eula-changes/69000> ·
<https://juce.com/legal/juce-9-licence/> ·
<https://github.com/juce-framework/JUCE/blob/master/BREAKING_CHANGES.md>

## Contexte PipeWire chez Tony (repris du dépôt)

- Graphe en 44100. SSL 12 en profil Pro Audio : sink 8 ch (+ 4 sinks stéréo
  virtuels `ssl12.pb12/34/56/78` via loopbacks), source **16 ch** (vérifié :
  `capture_AUX0…15`).
- Un `virtual-mic` existe déjà (setup StreamMix) — Douze FX ne doit pas le
  casser : créer sa propre source, Tony choisira dans Discord.
- Easy Effects tourne encore (nœuds `easyeffects_sink`/`easyeffects_source`
  présents) : Douze FX le remplacera progressivement, pas d'un bloc.
- Attention : redémarrer PipeWire tue Easy Effects/Vesktop — éviter d'exiger
  des restarts (d'où la solution `LD_LIBRARY_PATH` pour JACK).

## Décisions produit — GUI Douze (17/08/2026, tranchées par Tony)

Maquette de l'onglet **FX** (reprend les tokens de `douze/tools/douze.html`) :
<https://claude.ai/code/artifact/b4ea0afc-28f6-46ef-92fe-ba0354ebbb0f>

| Point | Décision |
|---|---|
| Points d'insertion proposés | **Seulement ce qui est branché** (entrées avec signal, paires réellement utilisées) + lien « tout afficher » |
| Sortie d'une bande d'entrée | **Au choix bande par bande** : micro virtuel (`douze_fx_mic`) ou paire de lecture ; demandé à la création |
| Monitoring direct du canal traité | Douze **coupe ET rétablit**, et l'**affiche** (bandeau sur la bande + interrupteurs dans le rail) — jamais d'écriture silencieuse dans la matrice |
| Paramètres d'un plugin | **Recherche + favoris épinglés** (★ en tête, sauvegardés avec le preset) ; « Tous » reste accessible |
| Presets de rack | **Bibliothèque commune**, chargeable sur n'importe quelle bande |
| Panne d'un plugin | La bande **continue en sautant l'étage** (marqué en rouge + motif + « réessayer ») — on ne coupe jamais le son parce qu'un plugin a lâché |
| Taille de bloc / latence | ~~Par bande~~ → **GLOBAL** (décision révisée le 17/08/2026). Une bande qui « demande » un quantum tire TOUT le graphe avec elle : le réglage par bande était une illusion, qui produisait des « 256 demandé / 1024 obtenu » incompréhensibles. C'est donc l'**horloge du graphe** dans Douze (`set_graph`), déjà en place, et `Strip.start` ne pose plus aucun bloc |

Architecture de l'intégration : **Douze FX reste un moteur sans GUI propre** (hors éditeurs
natifs) ; **Douze garde toute l'interface** et son démon devient superviseur de bandes.
Douze FX expose une **API HTTP/JSON locale**, un port par bande. ⚠️ Les chemins
esquissés ici (`/strip/<id>/…`) n'ont PAS survécu à l'écriture : une instance = une bande,
donc l'identifiant de bande dans l'URL était redondant. L'API réelle est plus bas
(« API de contrôle locale ») et fait autorité. Le scan a aussi changé de camp : il vit
dans le SUPERVISEUR, pas dans le moteur, pour survivre à la mort d'une bande. Pas de
SSE côté moteur non plus — c'est Douze qui diffuse, en interrogeant une seule fois pour
tous ses clients.
Savoir-faire repris de Delestor (pas le code) : scan hors-process + cache + skip-on-freeze,
catalogue marque/type/erreurs, sauvegarde d'état par plugin, watchdogs.

## Jalons

1. **Jalon 1 — une bande, bout en bout — ✅ FAIT (17/08/2026), à valider à
   l'oreille.** Voir la section suivante.
2. **Jalon 2 — généralisation — ✅ FAIT (17/08/2026).** API de contrôle locale,
   N bandes supervisées (`tools/douzefx.py`, 1 process + 1 port par bande,
   ré-adoption par le port), auto-câblage, création du nœud virtuel par le
   superviseur (micro **et** puits), et réadoption des applications quand ce
   nœud renaît.
3. **Jalon 3 — intégration Douze — en grande partie FAIT.** Le démon lance et
   câble les bandes **et** réécrit la matrice hardware en cohérence :
   `_fx_direct()` coupe le monitoring direct des canaux traités au démarrage et
   le rétablit à l'arrêt, en mémorisant l'état d'avant. **Restent** : les
   profils (rappeler un jeu de bandes complet) et le remplacement d'Easy
   Effects côté sortie.

## Jalon 1 — ce qui existe (17/08/2026)

```
fx/CMakeLists.txt          FetchContent JUCE 9.0.1, JUCE_JACK=1, hosting VST3/LV2
fx/src/PluginScan.{h,cpp}  catalogue : cache XML + « chemin@0xUID » (shells VST3)
fx/src/Rack.{h,cpp}        chaîne en série, états, éditeurs natifs
fx/src/main.cpp            device JACK, callback audio, console de commandes
fx/tools/run-douze-fx.sh   pose le libjack PipeWire + nomme le nœud
```

```bash
cmake -S fx -B build-fx -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build-fx
./fx/tools/run-douze-fx.sh --list-devices
DOUZE_FX_NAME=douze-fx.mic ./fx/tools/run-douze-fx.sh \
    --in "SSL 12 Pro" --out "SSL 12 Playback 3-4"
```

Commandes de la console : `ls`, `find <texte>`, `add <chemin.vst3>`, `rm <n>`,
`e <n>` (ouvre / masque l'éditeur natif), `b <n>`, `bypass`, `scan`, `save`,
`dev`, `q`. Le rack vit dans `~/.config/douze-fx/rack.json`.

**Validé sur la machine (graphe PipeWire réel)** :
- backend **JACK** via `pipewire.jack` 1.6.6, **sans rebuild NixOS ni restart** ;
- nœud `douze-fx.<nom>` visible dans le graphe, câblé automatiquement
  (`capture_AUX0/1` → in_1/2, out_1/2 → `ssl12.pbXX`) ; renommage par
  `PIPEWIRE_PROPS` opérationnel ;
- **catalogue repris du cache Delestor : 1668 plugins** immédiatement
  disponibles (zéro scan à refaire) ;
- **la chaîne cible tourne** : `Clear` (Supertone, Windows/yabridge) → `Mc`
  (VST3 natif), instanciés et traités en série, ~1,2 % CPU, 0 xrun ;
- **éditeur natif de Clear** (fenêtre Wine) : ouvert / masqué / ré-affiché /
  re-masqué, 4 bascules d'affilée sans blocage ; bypass + `save` OK.

**Trois constats à retenir :**
- **Clear a 2148 samples de latence interne** (~49 ms à 44,1 kHz — c'est un
  débruiteur IA). Parfait pour le flux Discord, **injouable en monitoring
  direct** : sur une bande d'entrée, on écoute le hardware, pas le retour traité.
- Le nœud reçoit un **quantum de 1024** (~23 ms) du graphe. Pour descendre, c'est
  l'horloge du GRAPHE qu'on règle, pas la bande (cf. le tableau des décisions) —
  exposé dans Douze.
- **La sortie du process peut BLOQUER > 30 s** sur le teardown d'un plugin Wine
  (constaté avec Clear — exactement la leçon Acustica de Delestor). Corrigé par
  un **watchdog de sortie** (8 s puis `_Exit`) : sortie mesurée à **1 s**,
  éditeur Wine ouvert compris.

**Null-test ✅ (17/08/2026)** — `fx/tools/nulltest.py`. Une bande jetable en
passthrough (puits virtuel → micro virtuel), un signal connu injecté, réenregistré,
réaligné, soustrait :

```
canal 1 : retard 24576 éch. (557.28 ms) | résidu RMS -inf dBFS, crête -inf dBFS
          témoins à ±1 éch. : -12.2 / -12.2 dBFS
```

**Résidu exactement nul sur les deux canaux** : entre son entrée et sa sortie,
Douze FX n'ajoute rien, ne retranche rien, ne rééchantillonne pas. Les témoins
comptent autant que le résultat — désaligné d'UN échantillon, le résidu remonte
au niveau du signal, ce qui prouve que la mesure sait échouer. Le contrôle est
dans le script, donc il ne peut pas être oublié.

Le retard de 557 ms est celui du chemin de TEST (pw-play → puits → moteur →
micro virtuel → pw-record, chacun avec ses tampons), pas de la bande en usage :
pour ça, lire `latency` dans `/state`.

**Reste à faire côté jalon 1** : l'écoute réelle par Tony — un null-test prouve
que le chemin est honnête, pas que la chaîne SONNE bien. La paire de retour est
tranchée : `pb34` (les tests s'étaient faits sur `pb78`, fermée au mixer, pour ne
rien injecter dans le monitoring de Tony).

## API de contrôle locale ✅ (17/08/2026, testée sur la machine)

`fx/src/HttpApi.{h,cpp}` — serveur HTTP/1.1 maison (~200 lignes, `juce::StreamingSocket`,
pas de dépendance en plus), **lié à 127.0.0.1 uniquement**, une requête par connexion,
corps JSON, en-têtes CORS pour que la GUI web de Douze (autre port local) puisse appeler.
Activation : `--port N` ou `DOUZE_FX_PORT=N` (un port par bande).

Toute opération touchant un plugin (instancier, éditeur, preset) est renvoyée sur le
**message thread** via `onMessageThread()` (attente + timeout) — leçon Delestor.

| Méthode | Chemin | Effet |
|---|---|---|
| GET | `/state` | nom, backend, source/destination, SR, bloc, xruns, CPU, latence, et les étages (nom, chargé, bypass, latence, nb params, `error`) |
| GET | `/plugins?q=&limit=` | catalogue filtré (nom/marque) + total |
| GET | `/params?stage=N` | paramètres réels : nom, valeur normalisée, texte formaté |
| POST | `/chain/add` `{path}` | ajoute un étage (même s'il échoue → marqué, sauté) |
| POST | `/chain/remove` `{index}` · `/chain/move` `{from,to}` · `/chain/retry` | édition de la chaîne |
| POST | `/bypass` `{index?,on?}` | bypass d'un étage, ou de la bande entière |
| POST | `/editor` `{index}` | affiche/masque la GUI native de l'étage |
| POST | `/params` `{stage,index,value}` | règle un paramètre (`setValueNotifyingHost` → l'éditeur natif suit) |
| POST | `/preset/save` · `/preset/load` `{file?}` | presets de rack |
| POST | `/quit` | arrêt propre de la bande |

**Vérifié en vrai** (bande micro en marche, `curl`) : `/state` complet ; `/plugins?q=comp`
→ 1668 au total, résultats filtrés ; `/chain/add` d'un chemin faux → étage **en erreur,
sauté, la bande continue de sortir du son** (la décision produit, prouvée en direct) ;
`/chain/remove` puis ajout du bon chemin → CHOW chargé, latence 37, 54 paramètres ;
`/params` set `Output Gain` 0.5 → 0.85 (« 21.00 dB ») appliqué de bout en bout ;
`/bypass` par étage OK.

⚠️ **Taille de bloc** — c'est cette enquête qui a fait abandonner le réglage par
bande. `--block` seul ne suffit pas : le backend JACK ne choisit pas le quantum. Le levier
réel est `PIPEWIRE_LATENCY=<bloc>/<sr>`, et **ça reste une demande** : si un autre nœud du
graphe impose un quantum plus grand, PipeWire tranche et la bande l'obtient. Autrement dit
une bande ne peut pas avoir SA latence — elle négocie celle de tout le monde. D'où le
réglage global, et l'affichage du bloc RÉEL lu dans `/state` plutôt que de la valeur
demandée. Le lanceur sait toujours poser `DOUZE_FX_BLOCK` (utile à la main, pour une
expérience) ; c'est le SUPERVISEUR qui ne le fait plus.

⚠️ `tools/gcroots.sh` protège désormais aussi `douze_fx` **et** le libjack de
PipeWire (chargé par `dlopen` → invisible d'un `ldd`, donc effaçable par un GC).
À relancer après chaque rebuild.

## Robustesse du thread de contrôle ✅ (17/08/2026 — déclenché par un vrai clic)

Symptôme rapporté : clic sur l'icône GUI (▣) de **RDeEsser Stereo** → aucune fenêtre, et
« à la place le nom a été renommé » (le chip affichait `WaveShell1-VST3 16.7_x64@0xe39a6c6d`).
Reproduit à l'identique en `curl`. Un seul défaut à l'origine, trois conséquences :

`createEditorAndMakeActive()` sur un plugin **Waves via yabridge** ne rend JAMAIS la main
(la fenêtre est créée côté Wine — D3D11, dcomp, `EnableNonClientDpiScaling` dans le log —
puis l'appel reste pendu ; attendu 5 min, rien). Or :

1. `Rack::toggleEditor` tenait `lock_` pendant cet appel → `/state`, `/params`, tout ce qui
   lit la chaîne se figeait avec lui. L'audio survivait (le chemin audio est en `tryLock`)
   mais **la chaîne était court-circuitée** tout ce temps ;
2. le serveur HTTP ne traite **qu'une requête à la fois** → la requête pendue rendait
   **toute l'API muette** ;
3. côté Douze, `alive()` restait vrai (le process vivait) mais `/state` expirait → repli sur
   le rack, qui ne stockait que des **chemins** → les chips prenaient un nom de fichier.
   Le « renommage » n'était donc qu'un affichage de secours mal outillé.

Corrections :

- **verrou lâché** pendant l'appel au plugin (la chaîne n'est mutée que depuis ce thread) ;
- **`/editor` en fire-and-forget** : la requête répond immédiatement, l'API reste vivante ;
- **watchdog du thread de contrôle** (`main.cpp`, `namespace watchdog`) : battement de cœur
  émis par un `juce::Timer` (donc muet dès que le thread est bloqué), `_Exit(70)` au-delà du
  budget, avec la **phase** en clair dans le log. Budget élargi par RAII pour les opérations
  légitimement lentes (instancier 90 s, restaurer 90 s, éditeur 20 s, capture d'état 20 s) et
  **fenêtre de grâce** de 20 s après, pour couvrir le contrecoup — les deux leçons de Delestor ;
- **superviseur** (`douzefx.py`) : fil de surveillance toutes les 3 s, relance la bande dont
  le process est mort sans qu'on l'ait demandé, avec **frein** (3 relances / 120 s puis
  renoncement affiché dans la GUI) ;
- **mémoire des éditeurs bloquants** (pattern deadman de Delestor) : le chemin est écrit
  AVANT l'essai dans `~/.cache/douze-fx/editor_try_<bande>.txt`, effacé après (y compris sur
  échec PROPRE — une exception rattrapée n'est pas un blocage). S'il survit à un redémarrage,
  le plugin est inscrit dans `~/.cache/douze-fx/editor_hang.txt` et **on refuse de rouvrir**
  son éditeur : `/state` publie `editor_hangs`, la GUI grise le ▣ et renvoie vers ≡ ;
- le rack **sauve le nom** de chaque étage → l'affichage hors-ligne est correct.

**Vérifié bout en bout** : clic → API répond en 6 ms → l'éditeur fige → watchdog → 
`[fx] mic : relancée (thread de contrôle figé)` → ~6 s de coupure → chaîne rechargée ;
2ᵉ clic → refus instantané (`éditeur 2 (RDeEsser Stereo) : refusé`) ; **KStrip, lui,
s'ouvre et se masque normalement** (pas de régression) ; bypass ON → `out_peak == in_peak`
au chiffre près, 0 xrun.

⚠️ **Piège JUCE, corrigé au passage** (visible dans la GUI : « plugin tombÃ© pendant le
traitement ») : `juce::String (const char*)` lit les octets en **Latin-1**, un par
caractère. Un « é » littéral (2 octets UTF-8) devient deux caractères et ressort
double-encodé dans le JSON. Tout littéral accentué destiné à une `juce::String` passe
désormais par `utf8()` / `CharPointer_UTF8` (les `std::cout`, eux, n'ont jamais été touchés).

**Limite assumée** : on ne sait pas ouvrir l'UI native des Waves dans Douze FX. Le panneau
≡ (paramètres) les pilote entièrement.

**MAJ 17/08/2026 — le coût de cette limite est tombé, et 5 bis ne s'impose plus.**
En cherchant par où commencer l'isolation par process, on a trouvé que les six
secondes de silence n'étaient PAS le fait du plugin : le watchdog ne surveillait
que le battement du *message thread* et faisait `_Exit(70)` quand il se taisait —
alors que le **thread audio continuait de traiter**, imperturbable. On tuait donc
un process dont le son allait très bien, pour récupérer une fenêtre.

Le watchdog a maintenant **deux battements** (`audioBeat` incrémenté dans le
callback audio) et deux verdicts : contrôle figé + audio vivant → on garde tout et
on le DIT (`frozen` dans `/state`, pastille ambre dans la GUI, bouton « Relancer »
laissé à l'utilisateur) ; les deux figés → sortie forcée comme avant.

Vérifié en vrai sur l'éditeur Waves qui gèle (RDeEsser, `WaveShell…@0xe39a6c6d`) :
le process **survit**, `ready=true`, `frozen=true`, 0,41 % de CPU, l'audio passe
toujours. Seul l'éditeur natif est perdu. Autrement dit, ce que 5 bis devait
réparer coûte désormais une fenêtre, plus une coupure de son.

Ce qu'une vraie isolation par process apporterait ENCORE : récupérer l'éditeur
sans relancer la bande, et isoler un plugin qui plante — mais ce dernier cas est
déjà encaissé (l'étage mort est sauté, l'audio continue). À rouvrir seulement si
l'usage le réclame ; ce n'est plus un prérequis à la publication.

## Inventaire de ce qui manque (17/08/2026, vérifié dans le code)

Établi sur demande, en lisant le code et non de mémoire. Ordre = valeur / effort.

### Quasi gratuit

- **Tri et regroupement du picker.** `/plugins` renvoie DÉJÀ `manufacturer` et
  `category` ; le picker se contente de filtrer par nom. A→Z / marque / type avec
  en-têtes de groupe est du travail GUI pur (cf. `rebuildRows` de Delestor).
- **Changement de fréquence d'échantillonnage.** `Rack::prepare` n'instancie que
  les étages MANQUANTS : il ne rappelle jamais `prepareToPlay` sur ceux déjà
  chargés. Après un 44,1 → 48 kHz, les plugins tournent réglés pour l'ancienne
  fréquence (filtres décalés, temps de compresseur faux).

### Le gros morceau : le scan

Il n'existe **aucun endpoint de scan**. Les seuls sont `/state`, `/plugins`,
`/params`, `/chain/{add,remove,move,retry}`, `/bypass`, `/editor`,
`/preset/{save,load}`, `/quit`. La liste vient du cache de Delestor importé en
LECTURE SEULE (1668 entrées) → **un plugin fraîchement installé n'apparaît
jamais** depuis Douze, seulement via la console (`scan`) ou `--scan`.

Delestor a déjà résolu tout le difficile : scan hors-process par plugin, timeout
et skip sur plugin qui gèle, liste d'erreurs avec rescan par ligne, persistance
périodique du cache, repli factory-only pour les WaveShell. **Ne pas réécrire** :
piloter `delestor_engine --scanone <fichier> <sortie.xml>` (et `--scanshell` en
repli), qui est la primitive robuste et déjà éprouvée.

### Structurel — un seul chantier, deux symptômes

- **Un plugin qui meurt emporte toute la bande** (démontré : tuer l'host Wine
  d'un plugin, en SIGKILL comme en SIGTERM, fait avorter le process — SIGABRT,
  `code -6`. Le superviseur relance en ~9 s, mais c'est une coupure audible).
- **Éditeurs natifs Waves impossibles** (liste noire apprise par deadman).

Même réponse aux deux : **un process par plugin ou par chaîne**, c'est-à-dire
l'étape 5 bis de Delestor, déjà écrite et validée là-bas.

### Reste

- **Zéro test automatisé dans `fx/`** (2 295 lignes) contre 21 dans Delestor.
  Une revue de code sans tests ne peut que lire, pas vérifier.
- La **reprise d'étage** (réinstanciation d'un étage tombé) est écrite mais n'a
  jamais été éprouvée en conditions réelles : tuer un host Wine produit un
  SIGABRT, pas l'état « étage mort, bande vivante ».
- Pas de **presets de rack nommés**, pas d'A/B : « Enregistrer » écrase le rack.
- **Aucune sauvegarde automatique** : une modif de chaîne est perdue au
  redémarrage si l'utilisateur n'a pas cliqué.
- L'onglet FX **sonde toutes les 400 ms** alors que le SSE existe déjà dans Douze
  (`/events` + `EventSource`, utilisé par la mixette). Incohérence, pas manque.
- Pas de vumètre ENTRE les étages, pas de trim par étage.
- `prev_mute` (mémoire du « ce canal était-il déjà coupé ? ») vit en RAM du démon
  seulement : un redémarrage l'oublie, et une relance automatique de bande ne
  repasse pas par la coupure d'écoute directe.
- `PROTOCOL.md` annonce encore « transport identifié, contenu inconnu, en attente
  des premières captures » alors que l'implémentation a largement dépassé ça.

### Où en est cet inventaire (fin de journée, 17/08/2026)

Fait depuis :

- **Scan** ✅ — coordinateur dans le démon Douze, un process jetable par plugin
  (`delestor_engine --scanone`, repli `--scanshell`), délai maximum, « Passer »,
  cache écrit tous les 20 fichiers, liste d'erreurs persistée, CLAP exclu (ni le
  moteur ni le scanner ne savent l'héberger). Amorcé sur le cache Delestor : 53
  fichiers à regarder au lieu de 1085. Résultat réel : **242 plugins ajoutés**,
  catalogue 1668 → 1910, et le moteur relit son cache À CHAUD (`reloadCacheIfChanged`)
  donc sans couper le micro.
- **Tri du picker** ✅ — catalogue chargé en entier puis trié côté client : A→Z,
  marque (112 groupes), type (39 groupes après normalisation des catégories VST3).
- **Fréquence d'échantillonnage** ✅ — `prepare` re-prépare les instances déjà
  chargées quand le format change.
- **Sauvegarde automatique du rack** ✅ — différée de 2,5 s après une modification
  de chaîne OU de bypass.
- **SSE pour l'onglet FX** ✅ — le démon pousse l'état des bandes, gaté par la
  présence d'auditeurs. Supprime la multiplication par page des sollicitations du
  verrou du rack.
- **Tests** ✅ — `fx/tools/run_tests.sh` : 43 vérifications moteur (hors device) +
  99 côté superviseur/scan (142 en tout — le compte annoncé jusqu'ici, « 96 »,
  ne comptait que la moitié Python). Le harnais a été validé en cassant
  volontairement une attente (il sortait en 0 : `|| true` après un pipe réinitialise PIPESTATUS).
- **Détails** ✅ — bypass par étage visible et sauvegardé, vumètre par étage,
  `prev_mute` persisté, `PROTOCOL.md` remis à jour.

Correctifs de robustesse trouvés en chemin (tous nés d'un usage réel) :

- `SIGPIPE` ignoré — un plugin Wine dont l'host meurt tuait TOUT le process, et
  aucun try/catch ne peut s'y opposer (un signal n'est pas une exception) ;
- `instantiate()` enveloppé — l'exception traversait le `callAsync` du message
  thread et finissait en `std::terminate`, donc SIGABRT ;
- ne RIEN appeler sur une instance dont l'host est mort : `releaseResources()` ne
  jette pas, elle BLOQUE (mesuré 90 s, jusqu'au watchdog) ;
- verrou du rack jamais tenu pendant un aller-retour plugin (`params` : 30 ms pour
  216 paramètres, soit un bloc sec garanti pour un budget de 23 ms ; `saveFile` :
  pire encore).

**Reste pour après la revue** : isolation par process (referme d'un coup les
crashs de plugin et les éditeurs natifs Waves), presets de rack nommés, et le
premier scan complet du parc Windows.

### Piste notée : créer des sources/destinations virtuelles depuis Douze

Demande de Tony (17/08/2026). Aujourd'hui un nœud virtuel n'existe qu'en tant
qu'EFFET DE BORD d'une bande : `_start_virtual_node(role)` le crée au démarrage,
et il disparaît à l'arrêt. On voudrait pouvoir en créer et en nommer librement
depuis Douze, indépendamment des bandes.

C'est faisable sans mécanisme nouveau — c'est le même `support.null-audio-sink`,
déjà écrit et éprouvé dans les deux sens (Audio/Sink pour un puits,
Audio/Source/Virtual pour un micro). Ce qu'il faut trancher :

- **Qui possède la durée de vie ?** Le nœud vit tant que son `pw-cli` vit. Un
  nœud indépendant d'une bande doit donc être tenu par le démon (liste dans
  `strips.json` ou un fichier à part), et ressuscité à son démarrage — sinon il
  disparaît au moindre redémarrage de Douze, avec les liens que l'utilisateur
  aura faits dessus.
- **Que se passe-t-il si une bande le référence puis qu'on le supprime ?** Il
  faut refuser la suppression, ou l'assumer et laisser la bande en erreur
  visible plutôt qu'en silence.
- `object.linger=true` ferait survivre le nœud à `pw-cli`, mais alors plus rien
  ne le nettoie : à éviter tant qu'on n'a pas de recensement fiable.

Intérêt réel au-delà du confort : plusieurs applications pourraient partager un
même puits, et une destination virtuelle survivrait au redémarrage d'une bande —
donc les liens faits par l'utilisateur dans un patchbay ne sauteraient plus.
