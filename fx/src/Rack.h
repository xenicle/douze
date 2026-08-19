// Douze FX — le rack : une chaîne ordonnée de plugins traitée EN SÉRIE.
//
// Reprend les leçons du proto Delestor (durement acquises, cf. CLAUDE.md) :
//   - `enableAllBuses` AVANT prepareToPlay  → sinon LALA / bx_console segfaultent
//     (bus Aux désactivé = pointeurs nuls lus par le plugin côté Wine) ;
//   - round-trip get/setStateInformation juste après l'instanciation → réveille
//     les Acustica (ASH/OAK) qui restent muets tant que leur kernel n'est pas
//     chargé, SANS ouvrir l'éditeur ;
//   - éditeurs natifs en fenêtre séparée, fermeture = MASQUER (garder le moteur
//     du plugin chargé, et ne pas déclencher les teardown bloquants).
//
// Différence avec Delestor : ici tout est LOCAL (pas de SHM, pas de slot, pas de
// process enfant) — le rack vit dans le process, une bande = un process.
#pragma once

#include <juce_audio_processors/juce_audio_processors.h>
#include <juce_audio_utils/juce_audio_utils.h>

#include "PluginScan.h"

namespace douze
{

/** Y a-t-il seulement un écran où poser une fenêtre ?

    Un service utilisateur lancé au BOOT démarre avant que la session graphique
    n'ait publié DISPLAY / WAYLAND_DISPLAY : l'audio, lui, n'en a pas besoin et
    marche parfaitement, mais Wine se rabat alors sur son pilote nul
    (« nodrv_CreateWindow : the explorer process failed to start ») et
    l'ouverture d'un éditeur Windows ne rend JAMAIS la main. */
bool hasDisplay();

class Rack
{
public:
    // ctor/dtor définis dans le .cpp : `Stage` y est complet (OwnedArray exige
    // le type complet pour détruire ses éléments).
    explicit Rack (PluginScan& scan);
    ~Rack();

    //== message thread ========================================================
    /** Prépare le rack pour ce format audio (instancie ce qui ne l'est pas). */
    void prepare (double sampleRate, int blockSize, int numChannels);

    /** Libère les instances (et ferme les éditeurs AVANT — ordre imposé). */
    void release();

    /** Ajoute un étage en fin de chaîne (« chemin » ou « chemin@0xUID »). */
    bool addStage (const juce::String& stagePath);

    bool removeStage (int index);

    /** Ouvre l'éditeur natif de l'étage, ou le masque s'il est déjà visible. */
    void toggleEditor (int index);

    bool loadFile (const juce::File& rackJson);
    bool saveFile (const juce::File& rackJson);

    void setBypass (int index, bool shouldBypass);

    /** Bascule le bypass d'un étage (A/B rapide) ; renvoie le nouvel état. */
    bool toggleBypass (int index);
    void setBypassAll (bool shouldBypass) noexcept { bypassAll_.store (shouldBypass); }
    bool bypassAll() const noexcept                { return bypassAll_.load(); }

    int getNumStages() const;
    juce::String describe() const;

    /** Ce que l'API expose d'un étage. `error` est vide tant que tout va bien ;
        un étage en erreur est SAUTÉ par le traitement, la bande continue. */
    struct StageInfo
    {
        juce::String path, name, error;
        bool bypass = false, loaded = false;
        // `editorHangs` : cet éditeur natif a DÉJÀ figé la bande, on refuse de le
        // rouvrir (la GUI le dit au lieu de proposer un bouton qui casse tout).
        bool editorHangs = false;
        // Crête en sortie de CET étage depuis la dernière lecture : c'est ce qui
        // dit lequel des plugins d'une chaîne perd ou sature le signal.
        float peak = 0.0f;
        int latency = 0, numParams = 0;
    };

    juce::Array<StageInfo> stageInfo() const;

    /** Déplace un étage (réordonne la chaîne). */
    bool moveStage (int from, int to);

    /** Retire cet étage de la liste des éditeurs bloquants — « je réessaie ».

        Sans porte de sortie, une inscription était DÉFINITIVE : un plugin sain
        condamné par un accident d'environnement (démon sans affichage) ne se
        rouvrait plus jamais, et rien dans l'interface ne permettait de revenir
        dessus. Écrit le fichier partagé, donc les autres bandes le verront à
        leur prochain clic (`toggleEditor` relit la liste). */
    bool forgetEditorHang (int index);

    /** Y a-t-il un étage tombé (ou en cours de reprise) ? Test bon marché,
        appelé souvent : il évite d'entrer dans la reprise pour rien. */
    bool needsRecovery() const;

    /** Réinstancie les étages tombés pendant le traitement. MESSAGE THREAD.

        Sans ça, un plugin dont l'host Wine meurt était simplement SAUTÉ jusqu'au
        prochain arrêt/démarrage manuel — la voix passait, mais sans compression
        ni dé-esseur, donc plus faible, et rien ne le disait à voix haute. */
    void superviseStages();

    struct ParamInfo { juce::String name, text; float value = 0.0f; };

    juce::Array<ParamInfo> params (int stageIndex) const;
    bool setParam (int stageIndex, int paramIndex, float normalised);

    /** Latence interne cumulée de la chaîne, en samples (informatif : sur une
        bande live il n'y a rien à réaligner, mais ça se paie à l'écoute). */
    int totalLatencySamples() const;

    //== thread audio ==========================================================
    /** Traite EN PLACE. Si le rack est en cours de modification, ne touche à
        rien (= passe le signal sec) plutôt que d'attendre un verrou. */
    void process (juce::AudioBuffer<float>& buffer, juce::MidiBuffer& midi);

private:
    struct Stage;

    /** Instancie un étage. Rattrape TOUTE exception : un plugin qui jette (via
        yabridge, quand son host Wine tombe) ne doit pas emporter la bande. */
    bool instantiate (Stage& s);
    bool instantiateUnguarded (Stage& s);
    void closeEditor (Stage& s);

    PluginScan& scan_;

    juce::CriticalSection lock_;          // protège la chaîne (tryEnter côté audio)
    juce::OwnedArray<Stage> stages_;

    // Chemins dont l'éditeur natif a figé le thread de contrôle (appris par
    // deadman, persisté dans ~/.cache/douze-fx/editor_hang.txt).
    juce::StringArray editorHangs_;

    double sampleRate_ = 0.0;
    int blockSize_ = 0;
    int numChannels_ = 0;

    juce::AudioBuffer<float> work_;       // buffer pleine largeur (canaux Aux inclus)
    std::atomic<bool> bypassAll_ { false };

    JUCE_DECLARE_NON_COPYABLE_WITH_LEAK_DETECTOR (Rack)
};

} // namespace douze
