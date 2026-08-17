#!/usr/bin/env bash
# ============================================================================
# gcroots.sh — protège Douze FX d'un `nix-collect-garbage`.
#
# POURQUOI : `douze_fx` est un binaire HORS du store, mais ses dépendances
# (interpréteur ELF glibc, X11, freetype, ALSA…) sont DANS /nix/store. Un GC
# efface celles que plus aucune racine ne retient -> la bande ne démarre plus
# (« No such file or directory » sur ld-linux) -> plus de micro traité, et un
# symptôme qui ne désigne pas sa cause. Vécu sur le moteur de Delestor, où l'on
# a cru avoir perdu le cache de scan alors qu'il était intact.
#
# À RELANCER APRÈS CHAQUE REBUILD : un rebuild peut tirer de NOUVEAUX chemins de
# store, que les anciennes racines ne couvrent pas.
#
# Usage : tools/gcroots.sh [--check]
#   (sans argument) régénère les racines dans .gcroots/
#   --check        n'écrit rien : liste ce qui n'est PAS protégé (code 1 si trou)
# ============================================================================
set -uo pipefail

cd "$(dirname "$0")/.."
ROOTDIR="$PWD/.gcroots"
CHECK=0
[[ "${1:-}" == "--check" ]] && CHECK=1

# Binaires à protéger. L'application de bureau n'est PAS ici : elle est
# construite par le flake et `.gcroots/douze-app` (créé par `nix build
# --out-link`) la retient déjà.
BINS=(
  "build-fx/douze_fx_artefacts/RelWithDebInfo/douze_fx"
  "build-fx/fx/douze_fx_artefacts/RelWithDebInfo/douze_fx"   # selon la disposition CMake
)

# Chemins protégés EN PLUS de la closure des binaires : le libjack de PipeWire
# est chargé par dlopen() à l'exécution -> il n'apparaît dans AUCUN ldd, donc un
# GC l'effacerait sans que rien ne le retienne, et Douze FX perdrait JACK.
EXTRA_PATHS=()
if jack_lib=$(nix eval --raw nixpkgs#pipewire.jack.outPath 2>/dev/null); then
  EXTRA_PATHS+=("$jack_lib")
fi

# Closure DIRECTE : bibliothèques résolues (ldd) + interpréteur ELF (le piège :
# ld-linux n'apparaît pas toujours dans ldd, et c'est LUI qui casse le lancement).
collect_paths() {
  local bin="$1"
  { ldd "$bin" 2>/dev/null | grep -o '/nix/store/[^ ]*'
    patchelf --print-interpreter "$bin" 2>/dev/null
  } | grep -o '^/nix/store/[^/]*' | sort -u
}

declare -A WANT=()
FOUND_BIN=0
for b in "${BINS[@]}"; do
  [[ -f "$b" ]] || continue
  FOUND_BIN=1
  while read -r p; do [[ -n "$p" ]] && WANT["$p"]=1; done < <(collect_paths "$b")
done
if [[ $FOUND_BIN -eq 0 ]]; then
  echo "gcroots: aucun binaire trouvé (builder l'engine d'abord)." >&2; exit 2
fi
for p in "${EXTRA_PATHS[@]:-}"; do
  [[ -n "$p" ]] && WANT["$p"]=1
done

# --check : compare aux racines existantes, ne touche à rien.
if [[ $CHECK -eq 1 ]]; then
  missing=0
  for p in "${!WANT[@]}"; do
    protected=0
    for l in "$ROOTDIR"/gcroot-*; do
      [[ -L "$l" && "$(readlink "$l")" == "$p" ]] && { protected=1; break; }
    done
    [[ $protected -eq 0 ]] && { echo "NON PROTÉGÉ: $p"; missing=1; }
  done
  [[ $missing -eq 0 ]] && echo "gcroots: closure complète (${#WANT[@]} chemins protégés)."
  exit $missing
fi

# Régénération : on repart à zéro (les racines périmées retiendraient d'anciens
# paquets pour rien ; Nix ignore les auto-roots dont le lien indirect a disparu).
mkdir -p "$ROOTDIR"
rm -f "$ROOTDIR"/gcroot-* "$ROOTDIR/closure.txt"

n=0
for p in "${!WANT[@]}"; do
  name="gcroot-$(basename "$p" | cut -c1-8)"
  # --indirect = racine ENREGISTRÉE côté Nix (/nix/var/nix/gcroots/auto) ; un
  # simple symlink dans le dépôt ne protégerait RIEN. -r réalise si nécessaire.
  if nix-store --add-root "$ROOTDIR/$name" --indirect -r "$p" >/dev/null 2>&1; then
    echo "$p" >> "$ROOTDIR/closure.txt"
    n=$((n+1))
  else
    echo "gcroots: échec sur $p" >&2
  fi
done

sort -o "$ROOTDIR/closure.txt" "$ROOTDIR/closure.txt" 2>/dev/null
echo "gcroots: $n chemins protégés dans $ROOTDIR (relancer après chaque rebuild)."
