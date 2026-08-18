{
  description = "douze — contrôle de la SSL 12 sous Linux (rétro-ingénierie du protocole SSL 360)";

  # ⚠️ Révision ÉPINGLÉE, pas « nixos-unstable ».
  #
  # Le devShell exporte un LD_LIBRARY_PATH dont héritent les bandes (le démon est
  # lancé par ce shell). Avec un nixpkgs plus récent que celui du système, la
  # libstdc++/glibc injectée ne correspond plus à celle contre laquelle yabridge
  # est construit, et TOUS les plugins Windows meurent au chargement (« The Wine
  # host process has exited unexpectedly »). Cette révision est celle de
  # delestor-proto, éprouvée avec le yabridge de cette machine.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/331800de5053fcebacf6813adb5db9c9dca22a0c";

  # Pin SÉPARÉ pour l'application de bureau. Sur nixos-unstable (gobject-
  # introspection 1.86 + PyGObject 3.56), l'introspection de classe est cassée :
  # les méthodes passent, mais les PROPRIÉTÉS et les SIGNAUX non
  # (« GtkMenuItem doesn't support property `label' », « unknown signal name:
  # delete-event » sur une GtkApplicationWindow). Sans signaux, pas d'app.
  inputs.nixpkgs-gtk.url = "github:NixOS/nixpkgs/nixos-25.05";

  outputs = { self, nixpkgs, nixpkgs-gtk }:
    let
      forAllSystems = f: nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ]
        (system: f nixpkgs.legacyPackages.${system});
      forAllSystemsGtk = f: nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ]
        (system: f nixpkgs-gtk.legacyPackages.${system});
    in
    {
      # Application de bureau (fenêtre + icône de notification). Empaquetée
      # plutôt que lancée par `nix develop` : le service, lui, passe par
      # `nix develop` à chaque redémarrage, ce qui recopie le dépôt dans le store
      # à chaque fois. Une icône de bureau ne doit pas faire ça.
      #
      # `DOUZE_APP_PY` permet de faire tourner le script DU DÉPÔT sans rebuild :
      # confort d'itération, l'environnement GTK restant celui du store.
      packages = forAllSystemsGtk (pkgs: rec {
        default = douze-app;

        douze-app =
          let python = pkgs.python3.withPackages (ps: [ ps.pygobject3 ]);
          in
          pkgs.stdenv.mkDerivation {
            pname = "douze-app";
            version = "0.1";
            src = ./tools;

            # wrapGAppsHook3 + gobject-introspection : ce duo pose
            # GI_TYPELIB_PATH, XDG_DATA_DIRS et les modules gdk-pixbuf (pour les
            # icônes SVG). À la main, ça se rate.
            nativeBuildInputs = [ pkgs.wrapGAppsHook3 pkgs.gobject-introspection ];
            buildInputs = [
              python
              pkgs.gtk3
              pkgs.webkitgtk_4_1
              pkgs.libayatana-appindicator
              pkgs.librsvg
              pkgs.adwaita-icon-theme
            ];

            dontBuild = true;

            installPhase = ''
              runHook preInstall
              mkdir -p $out/bin $out/share/douze \
                       $out/share/icons/hicolor/scalable/apps
              cp douze-app.py $out/share/douze/
              cp icons/douze.svg icons/douze-off.svg \
                 $out/share/icons/hicolor/scalable/apps/

              # PNG en plus du SVG : certaines barres d'état (Qt/quickshell) ne
              # résolvent pas un SVG et affichent le carré « icône introuvable ».
              for t in 16 22 24 32 48 64 128 256; do
                d=$out/share/icons/hicolor/''${t}x''${t}/apps
                mkdir -p $d
                cp icons/douze-$t.png     $d/douze.png
                cp icons/douze-off-$t.png $d/douze-off.png
              done

              cat > $out/bin/douze-app <<EOF
              #!${pkgs.runtimeShell}
              exec ${python}/bin/python3 \
                "\''${DOUZE_APP_PY:-$out/share/douze/douze-app.py}" "\$@"
              EOF
              chmod +x $out/bin/douze-app
              runHook postInstall
            '';

            meta.description =
              "Douze — fenêtre et icône de notification pour la SSL 12";
          };
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          name = "douze-dev";

          # Outils de build : Douze FX (fx/) vit désormais ICI, et c'est un
          # projet CMake/JUCE. cmake et ninja ne sont pas système, ils viennent
          # du shell.
          nativeBuildInputs = with pkgs; [
            cmake
            ninja
            pkg-config
            gcc
            gdb
            curl          # FetchContent clone JUCE au 1er configure
            git
          ];

          buildInputs = with pkgs; [
            # --- ce que JUCE réclame pour un host audio -------------------
            alsa-lib
            libjack2      # en-têtes JACK ; à l'exécution c'est le libjack de
                          # PipeWire que pose fx/tools/run-douze-fx.sh
            libpulseaudio
            libGL
            libGLU
            mesa
            libX11
            libxcb
            libXext
            libXrandr
            libXinerama
            libXcursor
            libXrender
            libXcomposite
            libXi
            libXfixes
            freetype
            fontconfig
            expat
            libxkbcommon
            dbus
            # <ladspa.h>, exigé par JUCE quand JUCE_PLUGINHOST_LADSPA=1.
            ladspa-header
            stdenv.cc.cc.lib
          ];

          packages = with pkgs; [
            wireshark # GUI + dissecteurs USB
            wireshark-cli # tshark, utilisé par tools/usbdump.py
            usbutils # lsusb
            nodejs    # `node --check` sur le JS de douze.html
            (python3.withPackages (ps: with ps; [ pyusb ]))
          ];

          # ⚠️ SURTOUT PAS de `export LD_LIBRARY_PATH` ici.
          #
          # `douze.service` lance le démon PAR ce shell (`nix develop --command
          # python tools/douze.py`), donc tout ce qu'il spawne en hérite : les
          # bandes, et à travers elles yabridge et Wine. Des bibliothèques venues
          # d'un nixpkgs différent de celui du SYSTÈME suffisent à faire mourir
          # l'host Wine au chargement du premier plugin Windows
          # (« terminate called without an active exception », bande en code -6).
          #
          # Diagnostic long à établir, donc écrit ici : le même binaire lancé À LA
          # MAIN depuis un autre shell marchait parfaitement — la faute était
          # l'environnement, pas le code ni la glibc du binaire.
          #
          # Les bibliothèques dont les plugins ont besoin sont posées par leur
          # lanceur (`fx/tools/run-douze-fx.sh`), et prises dans le profil du
          # système pour rester cohérentes avec yabridge.
          shellHook = ''
            echo "── douze ──────────────────────────────────────────"
            echo "capture : sudo modprobe usbmon ; sudo wireshark (interface usbmonN)"
            echo "analyse : python tools/usbdump.py captures/XX-....pcapng"
            echo "device  : $(${pkgs.usbutils}/bin/lsusb -d 31e9:0024 || echo '31e9:0024 SSL Control I/F NON DÉTECTÉE')"
            echo ""
            echo "Douze FX (host de plugins, JUCE 9) :"
            echo "  cmake -S fx -B build-fx -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo"
            echo "  cmake --build build-fx        # 1er build = clone JUCE 9.0.1"
            echo "  fx/tools/run_tests.sh         # 96 vérifications, sans carte"
            echo "─────────────────────────────────────────────────────"
          '';
        };
      });
    };
}
