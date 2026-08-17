// Douze FX — catalogue de plugins (cache XML + résolution d'un étage de rack).
//
// Réutilise le savoir-faire Delestor plutôt que son code : mêmes conventions de
// cache (`KnownPluginList` sérialisée en XML) et même syntaxe d'étage
// « chemin@0xUID » pour désigner UN sous-plugin d'un shell VST3 (WaveShell).
// Bonus pratique : si le cache Delestor existe (~1670 entrées déjà scannées,
// yabridge compris), on l'IMPORTE au lieu de refaire des heures de scan.
#pragma once

#include <juce_audio_processors/juce_audio_processors.h>

namespace douze
{

class PluginScan
{
public:
    PluginScan();

    /** Formats disponibles (VST3, LV2…), prêts pour createPluginInstance. */
    juce::AudioPluginFormatManager& formats() noexcept { return formats_; }

    /** Copie du catalogue, prise sous verrou.

        ⚠️ On ne rend PLUS la `KnownPluginList` elle-même. Elle était mutée depuis
        DEUX threads : le thread HTTP via `reloadCacheIfChanged` (qui la reconstruit
        entièrement) et le message thread via `resolveStage` (qui y ajoute un type).
        Course de données franche sur le même objet — au mieux un catalogue
        incohérent, au pire un crash. Les appelants travaillent donc sur une copie ;
        elle fait ~1900 entrées, c'est le prix de la tranquillité. */
    juce::Array<juce::PluginDescription> types() const;

    int numTypes() const;

    /** Scanne les fichiers/dossiers donnés (bloquant, IN-PROCESS : réservé aux
        cibles connues — le scan robuste hors-process est une reprise du jalon 2). */
    int scanPaths (const juce::StringArray& paths);

    /** Scan des emplacements VST3 standard + VST3_PATH. */
    int scanDefaultPaths();

    /** Résout un étage de rack (« chemin » ou « chemin@0xUID ») en description.
        Cherche d'abord dans le cache (match par uid), sinon interroge le format. */
    bool resolveStage (const juce::String& stagePath, juce::PluginDescription& out);

    /** Nom lisible d'un étage, pour les logs et la GUI. */
    juce::String displayName (const juce::String& stagePath);

    bool saveCache() const;

    /** Relit le cache s'il a changé sur le disque. Renvoie true s'il a bougé.

        Le scan est piloté par Douze (coordinateur Python, hors-process) et écrit
        ce fichier : sans cette relecture, un plugin fraîchement scanné n'aurait
        existé qu'au prochain démarrage de la bande — c'est-à-dire après une
        coupure du micro, pour la seule raison qu'on a installé un plugin. */
    bool reloadCacheIfChanged();

    static juce::File cacheFile();
    static juce::File delestorCacheFile();

private:
    void loadCache();

    juce::AudioPluginFormatManager formats_;

    // Protège `known_` et `cacheStamp_` : lus et ÉCRITS depuis le thread HTTP
    // comme depuis le message thread.
    mutable juce::CriticalSection lock_;
    juce::KnownPluginList known_;
    juce::int64 cacheStamp_ = 0;          // date du cache tel qu'on l'a lu
};

} // namespace douze
