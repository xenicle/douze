#!/usr/bin/env bash
# Lance Douze FX avec le libjack de PipeWire.
#
# JUCE fait un dlopen("libjack.so.0"). Chez Tony, `services.pipewire.jack.enable`
# n'est PAS activé → pas de libjack système, et le libjack2 du devShell est le
# VRAI JACK (il chercherait un jackd inexistant). PipeWire système et
# nixpkgs#pipewire.jack sont tous deux en 1.6.6 → on pose simplement le bon
# libjack dans LD_LIBRARY_PATH, sans rebuild NixOS ni restart PipeWire
# (contrainte : un restart tue Easy Effects et Vesktop).
#
#   ./fx/tools/run-douze-fx.sh --list-devices
#   ./fx/tools/run-douze-fx.sh --in "alsa_input.usb-…SSL_12…" --out ssl12.pb34
#
# Variables :
#   DOUZE_FX_JACK_LIB  répertoire contenant libjack.so.0 (sinon : détection)
#   DOUZE_FX_NAME      nom du nœud PipeWire de CETTE bande (défaut : douze-fx)
#   DOUZE_FX_BIN       binaire à lancer (sinon : build-fx/…)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# --- 1. binaire -------------------------------------------------------------
bin="${DOUZE_FX_BIN:-}"
if [[ -z "$bin" ]]; then
  for cfg in RelWithDebInfo Release Debug; do
    cand="$here/build-fx/fx/douze_fx_artefacts/$cfg/douze_fx"
    [[ -x "$cand" ]] && bin="$cand" && break
    cand="$here/build-fx/douze_fx_artefacts/$cfg/douze_fx"
    [[ -x "$cand" ]] && bin="$cand" && break
  done
fi
if [[ -z "$bin" || ! -x "$bin" ]]; then
  echo "douze_fx introuvable — build :" >&2
  echo "  cmake -S fx -B build-fx -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo && cmake --build build-fx" >&2
  exit 1
fi

# --- 2. libjack (PipeWire) --------------------------------------------------
# ⚠️ Ne JAMAIS mettre `nix eval` dans la liste d'un `for` : bash développe le mot
# entier AVANT d'itérer, donc l'évaluation partait à CHAQUE lancement de bande,
# même quand un chemin plus simple convenait. Le jour où nix s'est mis à bloquer
# (verrou du daemon, registre à rafraîchir), toutes les bandes ont cessé de
# démarrer — le lanceur restait pendu avant même son premier `echo`, et le
# superviseur ne voyait qu'un process vivant qui ne répondait jamais. Chaque
# relance en empilait une de plus. Panne vécue le 17/08/2026.
#
# D'où : on essaie du moins cher au plus cher, on s'arrête au premier qui marche,
# et on MÉMORISE le résultat — après le premier lancement, plus besoin de nix.
jacklib="${DOUZE_FX_JACK_LIB:-}"
cache="${XDG_CACHE_HOME:-$HOME/.cache}/douze-fx/jacklib"

if [[ -z "$jacklib" && -r "$cache" ]]; then
  d="$(cat "$cache" 2>/dev/null || true)"
  [[ -n "$d" && -e "$d/libjack.so.0" ]] && jacklib="$d"
fi

if [[ -z "$jacklib" ]]; then
  for d in /run/current-system/sw/lib/pipewire-0.3/jack /run/current-system/sw/lib; do
    if [[ -e "$d/libjack.so.0" ]]; then jacklib="$d"; break; fi
  done
fi

# Dernier recours, et BORNÉ dans le temps : mieux vaut une bande qui refuse de
# démarrer en disant pourquoi qu'une bande pendue pour toujours.
if [[ -z "$jacklib" ]]; then
  out="$(timeout 60 nix eval --raw nixpkgs#pipewire.jack.outPath 2>/dev/null || true)"
  [[ -n "$out" && -e "$out/lib/libjack.so.0" ]] && jacklib="$out/lib"
fi

if [[ -n "$jacklib" ]]; then
  mkdir -p "$(dirname "$cache")" 2>/dev/null || true
  printf '%s\n' "$jacklib" > "$cache" 2>/dev/null || true
fi
if [[ -z "$jacklib" ]]; then
  echo "libjack.so.0 (PipeWire) introuvable. Essaie :" >&2
  echo "  DOUZE_FX_JACK_LIB=\$(nix build --no-link --print-out-paths nixpkgs#pipewire.jack)/lib $0 …" >&2
  echo "  (ou --alsa pour le backend de repli)" >&2
  exit 1
fi
# On REMPLACE la variable au lieu de l'étendre, et on ne prend QUE deux chemins :
#
#   - le libjack de PipeWire (le backend audio) ;
#   - les bibliothèques du SYSTÈME (nix-ld), parce que des plugins natifs comme
#     RNNoise réclament libstdc++/libfreetype/libatomic, absentes du profil.
#
# Pourquoi remplacer : un LD_LIBRARY_PATH hérité (typiquement celui d'un
# devShell) injecte des bibliothèques d'un AUTRE nixpkgs que celui du système, et
# l'host Wine de yabridge meurt alors au chargement du premier plugin Windows.
# En prenant celles du système, on est cohérent avec yabridge, qui en vient aussi.
syslib="/run/current-system/sw/share/nix-ld/lib"
export LD_LIBRARY_PATH="$jacklib${LD_LIBRARY_PATH:+}"
[[ -d "$syslib" ]] && export LD_LIBRARY_PATH="$jacklib:$syslib"

# --- 3. nom du nœud dans le graphe -----------------------------------------
# Le nom du client JACK est figé à la COMPILATION dans JUCE
# (JUCE_JACK_CLIENT_NAME) → pour nommer chaque bande, on surcharge le nœud à
# l'exécution : pipewire-jack respecte PIPEWIRE_PROPS.
name="${DOUZE_FX_NAME:-douze-fx}"
props="node.name = \"$name\" node.description = \"Douze FX — $name\""

# --- 4. taille de bloc de CETTE bande ---------------------------------------
# Décision produit : le bloc se règle par bande, et se pose AU LANCEMENT (donc
# verrouillé tant que la bande tourne). node.latency est ce que PipeWire
# respecte ; --block le redemande côté JUCE pour que JACK ne négocie pas autre
# chose dans notre dos.
blockargs=()
if [[ -n "${DOUZE_FX_BLOCK:-}" ]]; then
  rate="${DOUZE_FX_RATE:-44100}"
  # PIPEWIRE_LATENCY est le levier qui fonctionne réellement : node.latency dans
  # PIPEWIRE_PROPS est ignoré par pipewire-jack (constaté). ⚠️ C'est une DEMANDE :
  # si un autre nœud du graphe impose un quantum plus grand, PipeWire tranche et
  # la bande obtient ce quantum-là — d'où la vérification dans /state.
  export PIPEWIRE_LATENCY="$DOUZE_FX_BLOCK/$rate"
  props="$props node.latency = $DOUZE_FX_BLOCK/$rate"
  blockargs=(--block "$DOUZE_FX_BLOCK")
fi
export PIPEWIRE_PROPS="{ $props }"

# --- 5. API de contrôle locale ----------------------------------------------
apiargs=()
[[ -n "${DOUZE_FX_PORT:-}" ]] && apiargs=(--port "$DOUZE_FX_PORT")

echo "[run] $bin"
echo "[run] libjack : $jacklib"
echo "[run] nœud    : $name${DOUZE_FX_BLOCK:+  (bloc $DOUZE_FX_BLOCK)}"
exec "$bin" "${blockargs[@]}" "${apiargs[@]}" "$@"
