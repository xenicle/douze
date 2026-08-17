#!/usr/bin/env bash
# Installe l'application de bureau Douze : icône de lanceur + icônes de thème.
#
# Ne copie PAS le code : le lanceur pointe vers `.gcroots/douze-app` (construit
# depuis le flake, donc protégé du ramasse-miettes Nix) et exécute le script du
# DÉPÔT via DOUZE_APP_PY. Éditer tools/douze-app.py suffit donc, sans rebuild —
# seul un changement de dépendances en demande un.
set -euo pipefail

racine="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
lien="$racine/.gcroots/douze-app"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
theme="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
icones="$theme/scalable/apps"
# Tailles PNG : le SVG seul suffit à GTK, mais PAS forcément à la barre d'état.
# Chez Tony (quickshell/Qt), un SVG seul donnait le carré magenta « icône
# introuvable » — d'où les PNG, que tout le monde sait lire.
TAILLES="16 22 24 32 48 64 128 256"

if [[ ! -x "$lien/bin/douze-app" ]]; then
  echo "install: construction du paquet (première fois, quelques minutes)…"
  nix build "$racine#douze-app" --out-link "$lien"
fi

mkdir -p "$apps" "$icones"
install -m644 "$racine/tools/icons/douze.svg"     "$icones/douze.svg"
install -m644 "$racine/tools/icons/douze-off.svg" "$icones/douze-off.svg"

for t in $TAILLES; do
  mkdir -p "$theme/${t}x${t}/apps"
  for n in douze douze-off; do
    install -m644 "$racine/tools/icons/$n-$t.png" "$theme/${t}x${t}/apps/$n.png"
  done
done

# ⚠️ `env -u LD_LIBRARY_PATH` : lancée depuis un shell de développement (Nix
# devShell), l'application chargerait des bibliothèques d'un AUTRE nixpkgs que le
# sien et Pango s'effondrerait à l'import. Depuis le bureau l'environnement est
# propre, mais le lanceur doit marcher dans les deux cas.
cat > "$apps/douze.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Douze
GenericName=Console SSL 12
Comment=Mixette, effets et micro de la SSL 12
Exec=env -u LD_LIBRARY_PATH -u PYTHONPATH -u GI_TYPELIB_PATH DOUZE_APP_PY=$racine/tools/douze-app.py $lien/bin/douze-app
Icon=douze
Terminal=false
Categories=AudioVideo;Audio;Mixer;
Keywords=SSL;audio;mixer;micro;
StartupNotify=true
StartupWMClass=douze-app
EOF

command -v update-desktop-database >/dev/null && \
  update-desktop-database "$apps" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && \
  gtk-update-icon-cache -f -t "$theme" 2>/dev/null || true

echo "install: OK"
echo "  lanceur : $apps/douze.desktop"
echo "  icônes  : $icones/douze{,-off}.svg"
echo "  binaire : $lien/bin/douze-app"
