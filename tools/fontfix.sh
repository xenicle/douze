#!/usr/bin/env bash
# ============================================================================
# fontfix.sh — répare les préfixes Wine après un `nix-collect-garbage`.
#
# POURQUOI : la clé registre
#   Software\Microsoft\Windows NT\CurrentVersion\Fonts
# de CHAQUE préfixe Wine liste les polices par chemin absolu `Z:\nix\store\…`.
# Une mise à jour nix crée de NOUVEAUX chemins, le GC efface les anciens — et
# les préfixes, eux, gardent les anciens. À chaque démarrage À FROID, Wine
# tente alors d'ouvrir des fichiers disparus en tenant la `loader_section` :
# PlugPlay, winebus, wineusb, winebth (et Bonjour / PACE là où ils sont
# installés) expirent chacun à 10 s (« failed to start: 1053 ») et le préfixe
# met 60 à 92 s à démarrer au lieu de 4.
#
# Vécu le 07/09/2026 : 347 des 465 polices listées avaient disparu (retrait des
# bureaux de la config NixOS + GC). La bande « mic » de Douze FX, qui a besoin
# de TROIS préfixes froids, dépassait le START_TIMEOUT de 90 s de douzefx.py et
# ne démarrait plus — sans qu'aucun message ne désigne les polices. La bande
# « retour », un seul préfixe, passait de justesse à 62 s : c'est cette
# asymétrie qui trahit la panne.
#
# CE QU'IL FAIT, en deux temps :
#   1. NETTOYAGE — retire des registres les SEULES entrées de police dont le
#      fichier n'existe plus. Les entrées valides sont conservées, et Wine
#      réenregistre ce qui lui manque. Rien n'est touché dans un préfixe déjà
#      sain, ni dans un préfixe dont un wineserver tourne (il réécrirait le
#      registre en se fermant, et on perdrait la correction).
#   2. PRÉCHAUFFAGE — un `wine cmd /c exit` dans chaque préfixe nettoyé, pour
#      que le PREMIER démarrage, celui qui coûte cher, soit payé ICI plutôt que
#      par l'autostart de Douze au prochain boot.
#
# ⚠️ HONNÊTETÉ SUR LA CAUSE : le lien « entrées mortes -> lenteur » est établi
# par un avant/après net sur les trois préfixes de Douze (92 s -> 4,2 s) et par
# une greffe de registre qui l'isolait sur cette seule clé (52 s contre 1 s).
# Mais quelques heures plus tard, en remettant 472 entrées mortes dans un autre
# préfixe — cache polices de `user.reg` purgé compris — la lenteur n'est PAS
# revenue. Un facteur global non identifié participe donc. C'est exactement
# pourquoi l'étape 2 existe : elle protège Douze même si le nettoyage seul ne
# suffit pas, puisqu'elle absorbe le premier démarrage quelle qu'en soit la
# cause. Ne pas retirer le préchauffage en croyant le nettoyage suffisant.
#
# À RELANCER APRÈS CHAQUE `nix-collect-garbage` (c'est le GC qui casse, pas le
# rebuild). Appelé automatiquement depuis ~/collect-garbage.sh.
#
# Usage : tools/fontfix.sh [--check] [--racine <dir>] [--sans-prechauffage]
#   (sans argument)      répare + préchauffe, en sauvegardant chaque registre
#   --check              n'écrit rien : liste les atteints (code 1 s'il y en a)
#   --racine <dir>       dossier des préfixes (défaut : ~/WinePrefix)
#   --sans-prechauffage  nettoie seulement (pour un diagnostic)
# ============================================================================
set -uo pipefail

RACINE="${WINEPREFIX_ROOT:-$HOME/WinePrefix}"
CHECK=0
PRECHAUFFE=1
BACKUP_DIR="$HOME/.cache/douze-fx/fontfix-backup-$(date +%Y%m%d-%H%M%S)"
NETTOYES=$(mktemp)
trap 'rm -f "$NETTOYES"' EXIT

while [ $# -gt 0 ]; do
  case "$1" in
    --check)  CHECK=1; shift ;;
    --sans-prechauffage) PRECHAUFFE=0; shift ;;
    --racine) RACINE="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,48p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "fontfix : argument inconnu « $1 » (voir --help)" >&2; exit 2 ;;
  esac
done

if [ ! -d "$RACINE" ]; then
  echo "fontfix : pas de dossier de préfixes ($RACINE), rien à faire."
  exit 0
fi

# --- préfixes à ne pas toucher ---------------------------------------------
# Un wineserver vivant garde le registre en mémoire et le réécrit en se
# fermant : corriger le fichier sous ses pieds ne servirait à rien. On les
# saute et on le DIT, pour que personne ne croie le parc entièrement traité.
occupes=""
for pid in $(pgrep -x wineserver 2>/dev/null); do
  # Le process peut disparaître entre le pgrep et la lecture : on met la
  # redirection DANS un groupe muet, sinon bash annonce lui-même l'échec.
  wp=$( { tr '\0' '\n' < "/proc/$pid/environ"; } 2>/dev/null | sed -n 's/^WINEPREFIX=//p')
  [ -n "$wp" ] && occupes="$occupes${wp%/}"$'\n'
done

# --- le travail lui-même ----------------------------------------------------
# En Python : un seul processus pour des dizaines de milliers de tests
# d'existence, là où une boucle shell prendrait des minutes.
export FONTFIX_RACINE="$RACINE" FONTFIX_CHECK="$CHECK" \
       FONTFIX_BACKUP="$BACKUP_DIR" FONTFIX_OCCUPES="$occupes" \
       FONTFIX_NETTOYES="$NETTOYES"

python3 - <<'PYEOF'
import os, re, shutil, sys

racine   = os.environ["FONTFIX_RACINE"]
check    = os.environ["FONTFIX_CHECK"] == "1"
backup   = os.environ["FONTFIX_BACKUP"]
occupes  = {p for p in os.environ["FONTFIX_OCCUPES"].splitlines() if p}
nettoyes = open(os.environ["FONTFIX_NETTOYES"], "w", encoding="utf-8")

CLE = "[Software\\\\Microsoft\\\\Windows NT\\\\CurrentVersion\\\\Fonts]"
VAL = re.compile(r'^"[^"]*"="(Z:\\\\[^"]+)"\s*$')

def unix(chemin_win):
    """« Z:\\\\nix\\\\store\\\\x\\\\y.otf » -> « /nix/store/x/y.otf »."""
    return "/" + chemin_win[len("Z:\\\\"):].replace("\\\\", "/")

atteints = touches = total_morts = 0
echecs = []

for nom in sorted(os.listdir(racine)):
    pfx = os.path.join(racine, nom)
    reg = os.path.join(pfx, "system.reg")
    if not os.path.isfile(reg):
        continue

    try:
        with open(reg, encoding="utf-8", errors="surrogateescape") as fh:
            lignes = fh.readlines()
    except OSError as e:
        echecs.append(f"{nom} : lecture impossible ({e})")
        continue

    # Repérer le bloc de la clé Fonts, puis les valeurs dont le fichier est mort.
    dans_bloc = False
    morts = []
    for i, ligne in enumerate(lignes):
        if ligne.startswith("["):
            dans_bloc = ligne.startswith(CLE)
            continue
        if not dans_bloc:
            continue
        m = VAL.match(ligne)
        if m and not os.path.exists(unix(m.group(1))):
            morts.append(i)

    if not morts:
        continue

    atteints += 1
    total_morts += len(morts)

    if pfx.rstrip("/") in occupes:
        echecs.append(f"{nom} : {len(morts)} polices mortes, mais un wineserver "
                      f"tourne — non corrigé (arrêter la bande puis relancer)")
        continue

    print(f"  {nom} : {len(morts)} polices disparues")
    if check:
        continue

    try:
        os.makedirs(backup, exist_ok=True)
        shutil.copy2(reg, os.path.join(backup, f"{nom}.system.reg"))
    except OSError as e:
        echecs.append(f"{nom} : sauvegarde impossible ({e}), NON modifié")
        continue

    # Écriture par fichier temporaire puis renommage : un registre à moitié
    # écrit (disque plein, coupure) rendrait le préfixe inutilisable.
    morts = set(morts)
    tmp = reg + ".fontfix.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as fh:
            fh.writelines(l for i, l in enumerate(lignes) if i not in morts)
        os.replace(tmp, reg)
    except OSError as e:
        echecs.append(f"{nom} : écriture impossible ({e})")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        continue

    touches += 1
    nettoyes.write(pfx + "\n")

# --- compte rendu -----------------------------------------------------------
nettoyes.close()

if atteints == 0:
    print("fontfix : tous les préfixes sont sains, rien à faire.")
else:
    print(f"fontfix : {atteints} préfixe(s) atteint(s), "
          f"{total_morts} entrée(s) de police disparue(s).")
    if not check:
        print(f"fontfix : {touches} corrigé(s), sauvegardes dans {backup}")

for e in echecs:
    print(f"  ! {e}", file=sys.stderr)

# --check sert de garde-fou dans un script : code 1 = il reste du travail.
if check and atteints:
    sys.exit(1)
sys.exit(1 if echecs else 0)
PYEOF
CODE=$?

# --- 2. préchauffage --------------------------------------------------------
# Le premier démarrage d'un préfixe après un GC est celui qui coûte cher. On le
# paie ici, une bonne fois, plutôt que de le laisser à l'autostart de Douze —
# où trois préfixes froids d'affilée dépassent son START_TIMEOUT de 90 s et
# laissent l'utilisateur sans micro traité, sans message qui désigne la cause.
# Seuls les préfixes NETTOYÉS sont préchauffés : après un GC, ce sont eux les
# suspects, et préchauffer les 86 coûterait des minutes pour rien.
if [ "$CHECK" -eq 0 ] && [ "$PRECHAUFFE" -eq 1 ] && [ -s "$NETTOYES" ]; then
  if ! command -v wine > /dev/null 2>&1; then
    echo "fontfix : wine introuvable, préchauffage sauté." >&2
  else
    n=$(wc -l < "$NETTOYES")
    echo "fontfix : préchauffage de $n préfixe(s)…"
    lents=0
    while read -r pfx; do
      [ -d "$pfx" ] || continue
      debut=$(date +%s)
      # 180 s : large devant les ~90 s du pire cas observé. Un préfixe qui
      # dépasse est signalé, pas retenté — le but est de ne bloquer personne.
      # `wine cmd /c exit` segfaute en SORTANT, y compris quand tout va bien
      # (observé sur des démarrages à 1 s). C'est bash qui l'annonce, pas wine :
      # on l'exécute dans un sous-shell muet pour ne pas alarmer dans un log de
      # GC. Le travail, lui, a bien eu lieu avant la sortie.
      # `timeout` se RETUE avec le signal de son enfant : c'est donc bash qui
      # annonce le segfault, et ni `2>/dev/null` ni un sous-shell ne le taisent.
      # Un shell intermédiaire, lui, l'annonce sur SA sortie d'erreur (muette)
      # et se termine par un code normal. Le « ; exit 0 » est INDISPENSABLE :
      # sans lui, bash optimise `bash -c 'une seule commande'` en exec() et se
      # fait remplacer par `timeout` — il n'y a alors plus de shell muet pour
      # encaisser le signal, et le message revient.
      WINEPREFIX="$pfx" WINEDEBUG=-all \
        bash -c 'timeout 180 wine cmd /c exit; exit 0' > /dev/null 2>&1
      duree=$(( $(date +%s) - debut ))
      if [ "$duree" -ge 20 ]; then
        echo "  lent : $(basename "$pfx") a mis ${duree} s (c'est ce démarrage-là qu'on vient d'absorber)"
        lents=$((lents + 1))
      fi
      # Ne pas laisser des dizaines de wineservers ouverts derrière soi — mais
      # ne tuer QUE celui qu'on vient de lancer. Un `wineserver -k` aveugle
      # couperait les plugins d'une bande Douze (ou d'un DAW) qui aurait
      # démarré sur ce préfixe entre le scan et maintenant : on vérifie donc
      # que le serveur est plus jeune que notre propre appel.
      for pid in $(pgrep -x wineserver 2>/dev/null); do
        wp=$( { tr '\0' '\n' < "/proc/$pid/environ"; } 2>/dev/null | sed -n 's/^WINEPREFIX=//p')
        [ "${wp%/}" = "${pfx%/}" ] || continue
        age=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ')
        [ -n "$age" ] && [ "$age" -le "$((duree + 5))" ] && kill "$pid" 2>/dev/null
      done
    done < "$NETTOYES"
    echo "fontfix : préchauffage terminé ($lents préfixe(s) lent(s) au premier démarrage)."
  fi
fi

exit $CODE
