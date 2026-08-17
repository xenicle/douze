#!/usr/bin/env bash
# Batterie de tests Douze FX — moteur ET superviseur, sans carte ni JACK.
#
#   fx/tools/run_tests.sh            # tout
#   fx/tools/run_tests.sh rack       # ne garde que les tests du moteur dont le
#                                    # nom contient « rack »
#
# Rien ici ne touche à une bande en marche : c'est le point. Jusqu'à présent
# chaque correctif se vérifiait à la main sur le micro de l'utilisateur, en
# coupant son son — donc en pratique une fois, sans filet contre les régressions.
#
# Ce que ça N'ATTRAPE PAS, et qui reste à faire en vrai : latence, underruns,
# éditeurs natifs, hébergement d'un plugin Wine. Ces tests-là couvrent la LOGIQUE,
# qui est exactement ce qu'on casse en corrigeant le reste.
set -uo pipefail

racine="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
moteur="$racine/build-fx/douze_fx_artefacts/RelWithDebInfo/douze_fx"
filtre="${1:-}"
echecs=0

titre() { printf '\n\033[1m── %s ─────────────────────────────────\033[0m\n' "$1"; }

titre "moteur (Rack, catalogue, rack.json)"
if [[ ! -x "$moteur" ]]; then
  echo "moteur absent : $moteur"
  echo "  construis-le d'abord :  ninja -C build-fx"
  echecs=$((echecs + 1))
else
  # On CAPTURE d'abord, on filtre pour l'affichage ensuite.
  #
  # Première version : un pipe vers grep suivi de `|| true`, puis lecture de
  # PIPESTATUS. Piège — le `|| true` exécute une nouvelle commande, donc
  # PIPESTATUS ne décrit plus le pipeline du moteur mais celui de `true` : le
  # harnais affichait « ECHEC » et sortait quand même en 0. Un harnais qui ne
  # peut pas échouer est pire que pas de harnais.
  sortie="$("$moteur" --selftest "$filtre" 2>&1)"
  rc=$?
  # Le bruit du chargement de catalogue (lilv, LV2 absents) n'apprend rien ici.
  printf '%s\n' "$sortie" \
    | grep -viE "lilv|manifest\.ttl|^error: failed to open file" || true
  [[ $rc -eq 0 ]] || echecs=$((echecs + 1))
fi

titre "superviseur et scan (Python)"
py="$(command -v python3 || true)"
# Même dépôt depuis le rapatriement de fx/ : plus de chemin à devenir faux.
tests="$racine/tools/test_douzefx.py"

if [[ -z "$py" ]]; then
  # Le devShell de ce dépôt n'a pas forcément python ; celui de Douze, oui.
  py="$(ls -d /nix/store/*-python3-3.1*-env/bin/python 2>/dev/null | head -1 || true)"
fi

if [[ -z "$py" || ! -f "$tests" ]]; then
  echo "sautés (python: ${py:-absent}, tests: $tests)"
else
  "$py" "$tests" || echecs=$((echecs + 1))
fi

titre "bilan"
if [[ $echecs -eq 0 ]]; then
  echo "tout passe."
else
  echo "$echecs groupe(s) en échec."
fi
exit $echecs
