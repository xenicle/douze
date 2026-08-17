#!/usr/bin/env bash
# Installe les 4 sinks stéréo de la SSL 12 pour l'utilisateur courant.
#
# Le fichier de configuration est un MODÈLE : il désigne la carte par le nom de
# son nœud ALSA, qui contient son numéro de série — donc différent sur chaque
# machine. Ce script le trouve et écrit le fichier final.
#
#   tools/install-pipewire.sh
#
# La carte doit être branchée ET en profil « Pro Audio » : c'est le seul profil
# qui expose les 8 canaux de lecture (pro-output-0). Sans lui, il n'y a rien à
# découper, et le dire tout de suite évite de chercher la panne ailleurs.
set -euo pipefail

ici="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
modele="$ici/pipewire/99-ssl12-sinks.conf"
dest="${XDG_CONFIG_HOME:-$HOME/.config}/pipewire/pipewire.conf.d/99-ssl12-sinks.conf"

if [[ ! -r "$modele" ]]; then
  echo "modèle introuvable : $modele" >&2
  exit 1
fi

if ! command -v pw-link >/dev/null; then
  echo "pw-link introuvable — PipeWire est-il installé ?" >&2
  exit 1
fi

noeud="$(pw-link -o 2>/dev/null \
         | grep -oE 'alsa_output\.usb-Solid_State_Logic_SSL_12_[^:]*\.pro-output-0' \
         | head -1 || true)"

if [[ -z "$noeud" ]]; then
  echo "SSL 12 introuvable dans le graphe PipeWire." >&2
  echo >&2
  echo "À vérifier, dans cet ordre :" >&2
  echo "  1. la carte est branchée et allumée      : lsusb | grep 31e9" >&2
  echo "  2. son profil est « Pro Audio »          : pavucontrol → Configuration" >&2
  echo "     (les autres profils n'exposent pas les 8 canaux de lecture)" >&2
  echo "  3. PipeWire la voit                      : pw-link -o | grep -i solid" >&2
  exit 1
fi

echo "carte trouvée : $noeud"

mkdir -p "$(dirname "$dest")"
if [[ -e "$dest" ]]; then
  cp -a "$dest" "$dest.bak"
  echo "configuration existante sauvegardée : $dest.bak"
fi

sed "s|@SSL12_NODE@|$noeud|g" "$modele" > "$dest"
echo "écrit : $dest"

echo
echo "Reste à recharger PipeWire :"
echo "    systemctl --user restart pipewire"
echo
echo "⚠️ Un redémarrage de PipeWire coupe les applications audio en cours"
echo "   (clients Discord, Easy Effects…) : à faire hors session."
