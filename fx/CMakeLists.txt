# Douze FX — host VST3 standalone branché en JACK (émulation PipeWire).
#
# BUILD SÉPARÉ du reste du repo (comme plugin/) : Douze FX est sur **JUCE 9**
# (décision Tony, licence AGPLv3) alors que l'engine Delestor reste sur
# JUCE 8.0.12 (16/16 tests validés contre ce tag — on ne le fait pas bouger).
# Deux FetchContent distincts = zéro collision de cibles.
#
#   cmake -S fx -B build-fx -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo
#   cmake --build build-fx
#   ./fx/tools/run-douze-fx.sh          (pose le libjack de PipeWire)
#
# Cf. docs/DOUZE-FX-BRIEF.md.

cmake_minimum_required(VERSION 3.22)

project(DouzeFx VERSION 0.1.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)   # clangd / Claude

# ---------------------------------------------------------------------------
# JUCE 9.0.1 (10/08/2026). Vérifié le 17/08/2026 : aucun des breaking changes
# 8 -> 9 ne touche notre code (cf. brief), `juce_JackAudio.cpp` toujours
# présent, `juce_audio_processors_headless` toujours là.
# ---------------------------------------------------------------------------
include(FetchContent)
FetchContent_Declare(JUCE
  GIT_REPOSITORY https://github.com/juce-framework/JUCE.git
  GIT_TAG        9.0.1
  GIT_SHALLOW    TRUE)
FetchContent_MakeAvailable(JUCE)

juce_add_console_app(douze_fx
  PRODUCT_NAME "Douze FX")

target_sources(douze_fx PRIVATE
  src/main.cpp
  src/HttpApi.cpp             # API de contrôle locale (127.0.0.1) pilotée par Douze
  src/PluginScan.cpp
  src/Rack.cpp
  src/ScanShell.cpp           # énumération factory-only des shells VST3
  src/SelfTest.cpp)

# ScanShell.cpp parle au SDK VST3 directement (headers vendorés par JUCE — dans
# `juce_audio_processors_headless` depuis JUCE 9, plus dans `juce_audio_processors`).
# L'include n'est ajouté que pour CE fichier : les macros du SDK et celles de JUCE
# se marchent dessus si on les mélange dans une même unité de compilation.
set_source_files_properties(src/ScanShell.cpp PROPERTIES
  INCLUDE_DIRECTORIES
    "${JUCE_SOURCE_DIR}/modules/juce_audio_processors_headless/format_types/VST3_SDK")

target_compile_definitions(douze_fx PRIVATE
  # --- I/O audio ---------------------------------------------------------
  JUCE_JACK=1                   # OFF par défaut dans juce_audio_devices !
  JUCE_ALSA=1                   # plan B (PCM `pipewire`) si JACK indisponible
  # Nom du client JACK : COMPILE-TIME dans JUCE (juce_JackAudio.cpp l.128).
  # Pour une bande nommée, on surcharge le nom du nœud À L'EXÉCUTION via
  # PIPEWIRE_PROPS (cf. tools/run-douze-fx.sh) — pipewire-jack le respecte.
  JUCE_JACK_CLIENT_NAME="douze-fx"

  # --- Hébergement de plugins -------------------------------------------
  JUCE_PLUGINHOST_VST3=1        # cible principale (yabridge expose les VST Windows ici)
  JUCE_PLUGINHOST_LV2=1         # SDK bundlé par JUCE, pas de dép système
  JUCE_PLUGINHOST_LADSPA=0      # éviterait d'exiger <ladspa.h> ici

  # --- Divers -------------------------------------------------------------
  JUCE_WEB_BROWSER=0            # évite webkitgtk
  JUCE_USE_CURL=0               # pas de réseau
  JUCE_MODAL_LOOPS_PERMITTED=1  # runDispatchLoopUntil() : pomper la message loop
                                # (plugins qui rappellent depuis un thread de fond)
  JUCE_STANDALONE_APPLICATION=1)

target_link_libraries(douze_fx
  PRIVATE
    juce::juce_audio_utils      # tire audio_processors, audio_devices, gui, events
  PUBLIC
    juce::juce_recommended_config_flags
    juce::juce_recommended_warning_flags)
