#include "Rack.h"

#include <iostream>
#include <thread>

namespace douze
{

/** Littéral UTF-8 → juce::String.

    ⚠️ `juce::String (const char*)` lit les octets en LATIN-1, un par caractère :
    un « é » littéral (2 octets en UTF-8) devient DEUX caractères, et ressort
    double-encodé une fois réémis en UTF-8. C'est comme ça que la GUI affichait
    « plugin tombÃ© pendant le traitement ». Tout littéral accentué destiné à une
    juce::String (donc à l'API, donc à la GUI) passe par ici. */
static juce::String utf8 (const char* literal)
{
    return juce::String (juce::CharPointer_UTF8 (literal));
}

//==============================================================================
/** Fenêtre flottante qui accueille l'éditeur natif d'un étage.

    Fermer = MASQUER, jamais détruire : détruire l'éditeur d'un Acustica
    décharge ses kernels (retour au silence) et, côté Wine, peut bloquer le
    message thread. Local ou pas, la leçon Delestor s'applique. */
class StageEditorWindow final : public juce::DocumentWindow
{
public:
    StageEditorWindow (const juce::String& title, juce::AudioProcessorEditor* ed)
        : juce::DocumentWindow (title, juce::Colours::darkgrey,
                                juce::DocumentWindow::closeButton)
    {
        setUsingNativeTitleBar (true);
        setContentOwned (ed, true);
        setResizable (ed->isResizable(), false);
        centreWithSize (getWidth(), getHeight());
        setVisible (true);
    }

    void closeButtonPressed() override { setVisible (false); }
};

//==============================================================================
// Éditeurs qui NE S'OUVRENT PAS : mémoire des blocages (pattern « deadman » de
// Delestor).
//
// Certains plugins Wine — les Waves, constaté sur RDeEsser Stereo — ne rendent
// JAMAIS la main sur createEditor : la fenêtre est créée côté Wine puis l'appel
// reste pendu, et seul le watchdog en sort (relance de la bande, quelques
// secondes de son perdues). Rejouer ça à chaque clic serait absurde. On écrit
// donc le chemin AVANT d'essayer et on l'effface après : si le fichier survit à
// un redémarrage, c'est que cet éditeur a emporté la bande → on le liste, et on
// refuse désormais de l'ouvrir en le disant à l'utilisateur (qui a le panneau
// « Paramètres » pour régler le plugin).
static juce::File fxCacheDir()
{
    return juce::File::getSpecialLocation (juce::File::userHomeDirectory)
             .getChildFile (".cache/douze-fx");
}

static juce::File editorDeadmanFile()
{
    const auto who = juce::SystemStats::getEnvironmentVariable ("DOUZE_FX_NAME", "bande");
    return fxCacheDir().getChildFile ("editor_try_"
                                        + juce::File::createLegalFileName (who) + ".txt");
}

static juce::File editorHangListFile()
{
    return fxCacheDir().getChildFile ("editor_hang.txt");
}

static juce::StringArray readEditorHangList()
{
    juce::StringArray out;

    if (auto f = editorHangListFile(); f.existsAsFile())
        out.addLines (f.loadFileAsString());

    out.removeEmptyStrings();
    return out;
}

//==============================================================================
struct Rack::Stage
{
    juce::String path;                       // « chemin » ou « chemin@0xUID »
    juce::String name;
    juce::String error;                      // vide = OK ; sinon l'étage est sauté
    juce::MemoryBlock savedState;            // état relu du rack.json (appliqué à l'instanciation)
    bool forceWake = false;                  // « wake »: true dans le rack
    // Levé par le thread audio quand l'étage a jeté : on ne le rappelle plus.
    std::atomic<bool> dead { false };
    // Crête en sortie de cet étage depuis la dernière lecture (vumètre par étage).
    std::atomic<float> peak { 0.0f };
    // Relevés à l'INSTANCIATION, pas redemandés ensuite.
    //
    // `getLatencySamples()` et `getParameters()` sont des allers-retours socket
    // sur un plugin Wine. Les appeler depuis `stageInfo` — la fonction la plus
    // sollicitée du programme, 2,5 fois par seconde — verrou du rack en main,
    // faisait passer du signal non traité. Ces deux valeurs ne changent qu'avec
    // la chaîne : on les note une fois. (Un plugin PEUT changer sa latence en
    // cours de route ; sur une bande live on ne compense rien, c'est informatif.)
    int latency = 0;
    int numParams = 0;
    bool recovering = false;                 // reprise en cours (message thread)
    int attempts = 0;
    juce::int64 nextAttemptMs = 0;
    std::unique_ptr<juce::AudioPluginInstance> inst;
    std::unique_ptr<StageEditorWindow> editor;
    std::atomic<bool> bypass { false };
};

Rack::Rack (PluginScan& scan) : scan_ (scan)
{
    // Le deadman a survécu à un redémarrage : l'éditeur de ce plugin a figé la
    // bande au coup précédent. On l'apprend une fois pour toutes.
    if (auto dm = editorDeadmanFile(); dm.existsAsFile())
    {
        const auto culprit = dm.loadFileAsString().trim();
        dm.deleteFile();

        if (culprit.isNotEmpty())
        {
            auto list = readEditorHangList();

            if (! list.contains (culprit))
            {
                list.add (culprit);
                editorHangListFile().getParentDirectory().createDirectory();
                editorHangListFile().replaceWithText (list.joinIntoString ("\n") + "\n");
                std::cout << "[rack] éditeur bloquant retenu : " << culprit << std::endl;
            }
        }
    }

    editorHangs_ = readEditorHangList();
}

Rack::~Rack()
{
    release();
}

//==============================================================================
int Rack::getNumStages() const
{
    const juce::ScopedLock sl (lock_);
    return stages_.size();
}

int Rack::totalLatencySamples() const
{
    const juce::ScopedLock sl (lock_);
    int total = 0;

    for (auto* s : stages_)
        total += s->latency;          // caché : pas d'appel plugin sous verrou

    return total;
}

juce::String Rack::describe() const
{
    const juce::ScopedLock sl (lock_);
    juce::String out;

    for (int i = 0; i < stages_.size(); ++i)
    {
        auto* s = stages_.getUnchecked (i);
        out << "  [" << i << "] " << s->name
            << (s->inst != nullptr ? juce::String() : utf8 ("  (NON CHARGÉ)"))
            << (s->bypass.load() ? "  (bypass)" : "")
            << (s->latency > 0 ? "  latence " + juce::String (s->latency)
                               : juce::String())
            << "\n";
    }

    if (stages_.isEmpty())
        out << utf8 ("  (rack vide — passthrough)\n");

    return out;
}

//==============================================================================
/** Active TOUS les bus (y compris les Aux/sidechain désactivés).

    Sans ça, LALA / bx_console reçoivent un AudioBusBuffers à 0 canal pour leur
    bus Aux et segfaultent en lisant quand même leur entrée côté Wine. On élargit
    donc l'arrangement et on nourrit ces canaux de silence à chaque étage. */
static void enableAllBuses (juce::AudioPluginInstance& inst)
{
    for (bool isInput : { true, false })
        for (int b = 0; b < inst.getBusCount (isInput); ++b)
            if (auto* bus = inst.getBus (isInput, b))
                if (! bus->isEnabled())
                    bus->enable (true);
}

/** Réveille les plugins qui ne chargent leur moteur qu'à la restauration d'un
    état (Acustica : ASH/OAK restent muets sinon).

    ⚠️ RÉSERVÉ à Acustica. On l'appliquait à tout le monde — « set(get()) est
    l'identité, donc inoffensif » — et c'était faux : KStrip (Kiive, via
    yabridge) meurt sur ce round-trip alors qu'il s'héberge parfaitement sans.
    Un plugin qui tombe emporte toute la bande, donc on ne prend ce risque que
    là où il sert vraiment. `wake: true` dans le rack force le comportement. */
static bool needsStateWake (const juce::PluginDescription& d)
{
    return d.manufacturerName.containsIgnoreCase ("acustica")
        || d.name.startsWithIgnoreCase ("ASH")
        || d.name.startsWithIgnoreCase ("OAK");
}

static void wakeByStateRoundTrip (juce::AudioPluginInstance& inst)
{
    juce::MemoryBlock mb;
    inst.getStateInformation (mb);

    if (mb.getSize() > 0)
        inst.setStateInformation (mb.getData(), (int) mb.getSize());
}

bool Rack::instantiate (Stage& s)
{
    // Enveloppe de sûreté. `instantiateUnguarded` appelle du code de plugin
    // (createPluginInstance, prepareToPlay…) qui, via yabridge, JETTE quand son
    // host Wine tombe. Personne ne rattrapait : l'exception traversait
    // `prepare` puis le `callAsync` du message thread et finissait en
    // `std::terminate` — donc SIGABRT, toute la bande emportée par un seul
    // plugin. C'est contraire à la décision affichée juste en dessous (« un
    // étage qui ne charge pas ne fait PAS tomber la bande »).
    try
    {
        return instantiateUnguarded (s);
    }
    catch (const std::exception& e)
    {
        s.error = juce::String ("exception au chargement : ") + e.what();
    }
    catch (...)
    {
        s.error = utf8 ("exception au chargement (inconnue)");
    }

    // L'instance est peut-être à demi construite et son host mort : on ne la
    // touche pas (l'y appeler bloquerait), on l'abandonne à un fil détaché.
    if (auto* morte = s.inst.release())
        std::thread ([morte] { delete morte; }).detach();

    std::cout << "[rack] " << s.name << " : " << s.error << std::endl;
    return false;
}

bool Rack::instantiateUnguarded (Stage& s)
{
    juce::PluginDescription desc;

    if (! scan_.resolveStage (s.path, desc))
    {
        s.error = "plugin introuvable";
        std::cout << "[rack] introuvable : " << s.path << std::endl;
        return false;
    }

    juce::String err;
    auto inst = scan_.formats().createPluginInstance (desc, sampleRate_, blockSize_, err);

    if (inst == nullptr)
    {
        // Décision produit : un étage qui ne charge pas ne fait PAS tomber la
        // bande — il est marqué, sauté, et reste réessayable.
        s.error = err.isNotEmpty() ? err : utf8 ("échec de chargement");
        s.name  = desc.name;
        std::cout << "[rack] échec de chargement : " << desc.name << " — " << err << std::endl;
        return false;
    }

    enableAllBuses (*inst);

    // ⚠️ PAS de setPlayConfigDetails ici. Sur un plugin multi-bus (Weiss Deess :
    // stéréo + sidechain mono en entrée, stéréo en sortie), redemander « 3 in /
    // 2 out » fait ré-arranger les bus par JUCE et l'arrangement annoncé au
    // plugin ne correspond plus au buffer : yabridge segfaute dans son memcpy
    // de recopie des canaux (pile relevée sur un core dump). On garde le layout
    // natif, exactement comme le probe de l'engine Delestor qui, lui, passait.
    inst->prepareToPlay (sampleRate_, blockSize_);

    if (s.forceWake || needsStateWake (desc))
        wakeByStateRoundTrip (*inst);

    if (s.savedState.getSize() > 0)
        inst->setStateInformation (s.savedState.getData(), (int) s.savedState.getSize());

    s.name  = desc.name;
    s.error = {};
    // Relevés MAINTENANT, pendant qu'on parle déjà au plugin : `stageInfo` n'aura
    // plus à le rappeler (cf. le commentaire sur ces champs).
    s.latency   = inst->getLatencySamples();
    s.numParams = inst->getParameters().size();
    s.inst  = std::move (inst);

    std::cout << "[rack] chargé : " << s.name
              << "  (in " << s.inst->getTotalNumInputChannels()
              << " / out " << s.inst->getTotalNumOutputChannels()
              << ", latence " << s.latency << ")" << std::endl;
    return true;
}

//==============================================================================
void Rack::prepare (double sampleRate, int blockSize, int numChannels)
{
    // ⚠️ RÈGLE DU RACK : jamais un appel de plugin le verrou en main.
    //
    // Instancier un plugin yabridge prend ~7 s (25 s pour un Acustica). Le verrou
    // tenu pendant ce temps, le thread audio — qui ne l'attend jamais, par
    // conception — laissait passer du signal NON TRAITÉ pendant toute la durée.
    // Autrement dit : ajouter un plugin à une bande en marche coûtait plusieurs
    // secondes de voix sans compresseur ni débruiteur.
    //
    // D'où trois temps : (1) sous verrou, on note QUOI faire ; (2) hors verrou,
    // on parle aux plugins ; (3) sous verrou, on publie. Lâcher le verrou entre
    // les deux est sûr parce que la chaîne n'est mutée que depuis ce thread — le
    // message thread — qui est celui qui exécute cette fonction.
    struct AFaire
    {
        int index = 0;
        bool aCreer = false;              // sinon : re-préparer l'existant
        juce::String path;
        bool forceWake = false;
        juce::MemoryBlock savedState;
        juce::AudioPluginInstance* existante = nullptr;
    };

    juce::Array<AFaire> travail;
    bool formatChanged = false;

    // --- (1) sous verrou : le format, et la liste du travail ------------------
    {
        const juce::ScopedLock sl (lock_);

        // Le FORMAT a-t-il changé ? À comparer AVANT d'écraser les champs.
        //
        // Sans ça, `prepare` n'instanciait que les étages manquants et laissait
        // les autres tels quels : après un passage 44,1 → 48 kHz (le graphe
        // PipeWire est global, donc ça arrive sans qu'on touche à la bande), les
        // plugins continuaient de tourner PRÉPARÉS POUR L'ANCIENNE FRÉQUENCE —
        // filtres décalés, temps de compresseur faux, et rien pour le signaler.
        formatChanged = (! juce::approximatelyEqual (sampleRate, sampleRate_)
                         || blockSize != blockSize_);

        sampleRate_  = sampleRate;
        blockSize_   = blockSize;
        numChannels_ = numChannels;

        for (int i = 0; i < stages_.size(); ++i)
        {
            auto* s = stages_.getUnchecked (i);

            if (s->inst == nullptr)
                travail.add ({ i, true, s->path, s->forceWake, s->savedState, nullptr });
            else if (formatChanged)
                travail.add ({ i, false, s->path, s->forceWake, {}, s->inst.get() });
        }
    }

    // --- (2) hors verrou : on parle aux plugins (des secondes) ----------------
    //
    // Les étages À CRÉER sont traités en (2b) : `instantiate` écrit dans le Stage,
    // et le faire hors verrou est sûr pour la même raison — seul ce thread mute la
    // chaîne. Le thread audio ne lit `inst` que sous tryLock, et verra soit
    // l'ancien nullptr (étage sauté), soit la nouvelle instance : la publication
    // est une écriture de pointeur, jamais un état intermédiaire.
    for (const auto& t : travail)
    {
        if (t.aCreer)
            continue;

        // Re-préparation d'une instance existante. Un plugin Wine peut jeter :
        // on marque l'étage plutôt que de laisser l'exception emporter la bande.
        try
        {
            t.existante->releaseResources();
            t.existante->prepareToPlay (sampleRate, blockSize);
            std::cout << "[rack] " << t.path << " re-préparé pour "
                      << sampleRate << " Hz / bloc " << blockSize << std::endl;
        }
        catch (...)
        {
            const juce::ScopedLock sl (lock_);

            if (juce::isPositiveAndBelow (t.index, stages_.size()))
                stages_.getUnchecked (t.index)->dead.store (true);

            std::cout << "[rack] " << t.path
                      << " a jeté à la re-préparation." << std::endl;
        }
    }

    // (2b) créations : `instantiate` a besoin du Stage, et lui seul écrit dedans.
    for (const auto& t : travail)
        if (t.aCreer && juce::isPositiveAndBelow (t.index, stages_.size()))
            instantiate (*stages_.getUnchecked (t.index));

    // --- (3) sous verrou : largeur du buffer de travail -----------------------
    const juce::ScopedLock sl (lock_);
    int widest = numChannels;

    for (auto* s : stages_)
        if (s->inst != nullptr)
            widest = juce::jmax (widest,
                                 s->inst->getTotalNumInputChannels(),
                                 s->inst->getTotalNumOutputChannels());

    // buffer de travail pleine largeur : les canaux au-delà de la bande (Aux,
    // sidechain) existent et sont silencés à chaque étage.
    work_.setSize (widest, blockSize, false, true, true);
}

void Rack::release()
{
    const juce::ScopedLock sl (lock_);

    for (auto* s : stages_)
    {
        closeEditor (*s);          // TOUJOURS avant de lâcher l'instance

        if (s->inst != nullptr)
            s->inst->releaseResources();

        s->inst.reset();
    }
}

void Rack::closeEditor (Stage& s)
{
    if (s.editor != nullptr)
    {
        s.editor->clearContentComponent();
        s.editor.reset();
    }
}

//==============================================================================
bool Rack::addStage (const juce::String& stagePath)
{
    auto s = std::make_unique<Stage>();
    s->path = stagePath;
    s->name = scan_.displayName (stagePath);

    // On instancie AVANT de prendre le verrou, et avant même que l'étage entre
    // dans la chaîne : personne d'autre ne peut le voir, donc les ~7 s d'un plugin
    // yabridge ne bloquent plus le thread audio (règle du rack : jamais un appel
    // de plugin le verrou en main). Le format courant est lu sous verrou, mais lui
    // seul.
    double sr;
    int bs, nc;

    {
        const juce::ScopedLock sl (lock_);
        sr = sampleRate_; bs = blockSize_; nc = numChannels_;
    }

    // On ajoute l'étage MÊME s'il ne charge pas : il apparaît en erreur, il est
    // sauté par le traitement, et il reste réessayable. Une chaîne qui refuse
    // silencieusement un plugin serait plus déroutante qu'un étage rouge.
    const bool ok = sr <= 0.0 || instantiate (*s);

    {
        const juce::ScopedLock sl (lock_);
        stages_.add (s.release());
    }

    if (sr > 0.0)
        prepare (sr, bs, nc);              // re-calcule la largeur du buffer

    return ok;
}

bool Rack::removeStage (int index)
{
    const juce::ScopedLock sl (lock_);

    if (! juce::isPositiveAndBelow (index, stages_.size()))
        return false;

    auto* s = stages_.getUnchecked (index);

    // Même règle que pour la reprise : sur un étage MORT (host Wine disparu),
    // tout appel vers l'instance bloque au lieu d'échouer. Retirer un chip rouge
    // ne doit pas figer la bande.
    if (s->dead.load())
    {
        if (s->editor != nullptr)
            (void) s->editor.release();

        if (auto* morte = s->inst.release())
            std::thread ([morte] { delete morte; }).detach();

        std::cout << "[rack] étage " << index << " (mort) abandonné sans le toucher."
                  << std::endl;
    }
    else
    {
        closeEditor (*s);

        if (s->inst != nullptr)
            s->inst->releaseResources();
    }

    stages_.remove (index);
    return true;
}

void Rack::setBypass (int index, bool shouldBypass)
{
    const juce::ScopedLock sl (lock_);

    if (juce::isPositiveAndBelow (index, stages_.size()))
        stages_.getUnchecked (index)->bypass.store (shouldBypass);
}

juce::Array<Rack::StageInfo> Rack::stageInfo() const
{
    const juce::ScopedLock sl (lock_);
    juce::Array<StageInfo> out;

    for (auto* s : stages_)
    {
        StageInfo i;
        i.path      = s->path;
        i.name      = s->name;
        i.error     = s->dead.load() ? utf8 ("plugin tombé pendant le traitement") : s->error;
        i.bypass    = s->bypass.load();
        i.loaded    = s->inst != nullptr;
        i.editorHangs = s->editor == nullptr && editorHangs_.contains (s->path);
        // Lecture DESTRUCTIVE (« depuis la dernière fois ») : même interrogé
        // deux fois par seconde, le vumètre ne rate aucun transitoire.
        i.peak      = s->peak.exchange (0.0f, std::memory_order_relaxed);
        // Valeurs CACHÉES : plus aucun appel plugin ici (cf. Stage::latency).
        i.latency   = s->latency;
        i.numParams = s->numParams;
        out.add (i);
    }

    return out;
}

//==============================================================================
// Reprise d'un étage tombé.
//
// Vécu : les deux derniers plugins d'une chaîne (KStrip, RDeEsser) sont morts en
// direct ; la voix passait toujours — les étages morts sont sautés — mais sans
// compression ni dé-esseur, donc plus faible. Rien ne l'annonçait et rien ne le
// réparait : il a fallu arrêter puis redémarrer la bande à la main.
static constexpr int kMaxRecoveryAttempts = 3;
static constexpr int kRecoveryDelayMs = 4000;

bool Rack::needsRecovery() const
{
    const juce::ScopedLock sl (lock_);

    for (auto* s : stages_)
        if (s->dead.load() || s->recovering)
            return true;

    return false;
}

void Rack::superviseStages()
{
    // Même règle que `prepare` : on DÉCIDE sous verrou, on agit en dehors. Une
    // réinstanciation prend des secondes ; la faire verrou en main revenait à
    // court-circuiter le traitement pendant tout ce temps — au moment précis où
    // l'utilisateur attend qu'on répare quelque chose.
    juce::Array<int> aReprendre;
    const auto now = juce::Time::currentTimeMillis();

    {
        const juce::ScopedLock sl (lock_);

    for (int i = 0; i < stages_.size(); ++i)
    {
        auto* s = stages_.getUnchecked (i);

        if (s->dead.load() && ! s->recovering)
        {
            // Le thread audio ne loge rien (il n'a pas le droit) : c'est ici que
            // la panne devient visible dans le journal.
            std::cout << "[rack] étage " << i << " (" << s->name
                      << ") est TOMBÉ pendant le traitement — reprise dans "
                      << kRecoveryDelayMs / 1000 << " s." << std::endl;
            s->recovering = true;
            s->attempts = 0;
            s->nextAttemptMs = now + kRecoveryDelayMs;
        }

        if (! s->recovering || now < s->nextAttemptMs)
            continue;

        if (s->attempts >= kMaxRecoveryAttempts)
        {
            // On renonce : le chip reste rouge, l'utilisateur décide. Réessayer
            // sans fin sur un plugin qui meurt à chaque coup ferait pire.
            std::cout << "[rack] étage " << i << " (" << s->name << ") : "
                      << kMaxRecoveryAttempts << " reprises sans succès, abandon."
                      << std::endl;
            s->recovering = false;
            continue;
        }

        ++s->attempts;
        s->nextAttemptMs = now + kRecoveryDelayMs;
        std::cout << "[rack] reprise " << s->attempts << "/" << kMaxRecoveryAttempts
                  << " de l'étage " << i << " (" << s->name << ")…" << std::endl;

        // ⚠️ NE RIEN APPELER sur l'ancienne instance.
        //
        // Première version : `releaseResources()` puis destruction, sous
        // try/catch. Ça ne jette pas — ça BLOQUE : l'host Wine est mort, donc
        // l'appel attend une réponse qui ne viendra jamais. Mesuré en direct sur
        // RDeEsser : 90 s de thread de contrôle figé, jusqu'au force-exit du
        // watchdog et à la relance de la bande par le superviseur. La « reprise »
        // coûtait donc plus cher que la panne qu'elle prétendait réparer (un
        // étage mort est simplement sauté, la bande reste réactive).
        //
        // On confie donc la destruction à un fil DÉTACHÉ : s'il se fige, il se
        // fige tout seul. La fenêtre d'éditeur, elle, ne peut être détruite que
        // sur le message thread (règle JUCE) et c'est justement ce qui bloque :
        // on l'abandonne franchement plutôt que de rendre la bande muette.
        if (s->editor != nullptr)
        {
            (void) s->editor.release();
            std::cout << "[rack] éditeur de l'étage " << i
                      << " abandonné (le détruire bloquerait)." << std::endl;
        }

        if (auto* morte = s->inst.release())
            std::thread ([morte] { delete morte; }).detach();

        s->dead.store (false);
        s->latency = s->numParams = 0;
        aReprendre.add (i);
    }
    }   // fin du verrou : la suite parle aux plugins

    // --- hors verrou : réinstanciation (des secondes) -------------------------
    for (const int i : aReprendre)
    {
        Stage* s = nullptr;

        {
            const juce::ScopedLock sl (lock_);

            if (juce::isPositiveAndBelow (i, stages_.size()))
                s = stages_.getUnchecked (i);
        }

        if (s == nullptr || sampleRate_ <= 0.0 || ! instantiate (*s))
            continue;

        {
            const juce::ScopedLock sl (lock_);
            s->recovering = false;
            s->attempts = 0;
        }

        std::cout << "[rack] étage " << i << " (" << s->name
                  << ") RÉCUPÉRÉ." << std::endl;
        prepare (sampleRate_, blockSize_, numChannels_);   // largeur du buffer
    }
}

bool Rack::moveStage (int from, int to)
{
    const juce::ScopedLock sl (lock_);

    if (! juce::isPositiveAndBelow (from, stages_.size())
        || ! juce::isPositiveAndBelow (to, stages_.size()) || from == to)
        return false;

    stages_.move (from, to);
    std::cout << "[rack] étage " << from << " déplacé en " << to << std::endl;
    return true;
}

juce::Array<Rack::ParamInfo> Rack::params (int stageIndex) const
{
    // ⚠️ MESSAGE THREAD, et SANS tenir le verrou pendant l'interrogation.
    //
    // Chaque `getCurrentValueAsText()` d'un plugin Wine est un aller-retour sur
    // sa socket. Mesuré : les 216 paramètres de KStrip = 30 ms. Le verrou étant
    // tenu tout ce temps, le thread audio — qui ne l'attend jamais (tryLock) —
    // laissait passer du signal NON TRAITÉ : 30 ms pour un budget de bloc de
    // 23 ms, donc au moins un bloc sec garanti à chaque lecture du panneau.
    // On ne prend donc le verrou que pour récupérer le pointeur. C'est sûr parce
    // que la destruction d'un étage se fait sur CE thread : les deux ne peuvent
    // pas s'entrelacer.
    juce::AudioPluginInstance* inst = nullptr;
    juce::Array<ParamInfo> out;

    {
        const juce::ScopedLock sl (lock_);

        if (! juce::isPositiveAndBelow (stageIndex, stages_.size()))
            return out;

        inst = stages_.getUnchecked (stageIndex)->inst.get();
    }

    if (inst == nullptr)
        return out;

    for (auto* p : inst->getParameters())
    {
        ParamInfo i;
        i.name  = p->getName (64);
        i.value = p->getValue();
        i.text  = p->getCurrentValueAsText();
        out.add (i);
    }

    return out;
}

bool Rack::setParam (int stageIndex, int paramIndex, float normalised)
{
    // Même règle que `params()` : le verrou juste pour le pointeur, jamais
    // pendant l'aller-retour vers le plugin (message thread).
    juce::AudioPluginInstance* inst = nullptr;

    {
        const juce::ScopedLock sl (lock_);

        if (! juce::isPositiveAndBelow (stageIndex, stages_.size()))
            return false;

        inst = stages_.getUnchecked (stageIndex)->inst.get();
    }

    if (inst == nullptr || ! juce::isPositiveAndBelow (paramIndex, inst->getParameters().size()))
        return false;

    // setValueNotifyingHost : l'éditeur natif du plugin doit suivre le réglage
    // fait depuis la GUI web, sinon les deux affichages divergent.
    inst->getParameters()[paramIndex]->setValueNotifyingHost (juce::jlimit (0.0f, 1.0f, normalised));
    return true;
}

bool Rack::toggleBypass (int index)
{
    const juce::ScopedLock sl (lock_);

    if (! juce::isPositiveAndBelow (index, stages_.size()))
        return false;

    auto* s = stages_.getUnchecked (index);
    const bool now = ! s->bypass.load();
    s->bypass.store (now);

    std::cout << "[rack] étage " << index << " (" << s->name << ") : "
              << (now ? "BYPASS" : "actif") << std::endl;
    return now;
}

void Rack::toggleEditor (int index)
{
    // ⚠️ On ne tient PAS `lock_` pendant l'appel au plugin.
    //
    // Ouvrir l'éditeur natif d'un plugin Wine peut ne JAMAIS rendre la main
    // (constaté sur les Waves : la fenêtre est bien créée côté Wine — D3D11,
    // dcomp — puis l'appel reste pendu). Le verrou en main, TOUT se figeait avec
    // lui : /state, /params, la description de la chaîne… La GUI de Douze, ne
    // recevant plus rien, retombait alors sur le rack (qui ne stocke que des
    // chemins) et les chips prenaient un nom de FICHIER — le fameux « le plugin
    // s'est renommé tout seul ». Lâcher le verrou est sûr : la chaîne n'est mutée
    // que depuis ce thread, et le thread audio, seul en face, ne l'attend jamais
    // (tryLock). Un éditeur pendu ne coûte plus alors qu'un éditeur pendu — et le
    // watchdog du thread de contrôle (main.cpp) finit par relancer la bande.
    juce::AudioPluginInstance* inst = nullptr;
    StageEditorWindow* open = nullptr;
    juce::String name, path;

    {
        const juce::ScopedLock sl (lock_);

        if (! juce::isPositiveAndBelow (index, stages_.size()))
            return;

        auto* s = stages_.getUnchecked (index);
        inst = s->inst.get();
        open = s->editor.get();
        name = s->name;
        path = s->path;
    }

    if (inst == nullptr)
        return;

    // Déjà vu figer : on ne rejoue pas la panne (l'utilisateur garde le panneau
    // « Paramètres », qui pilote le plugin sans passer par son UI).
    if (open == nullptr && editorHangs_.contains (path))
    {
        std::cout << "[rack] éditeur " << index << " (" << name
                  << ") : refusé — cet éditeur a déjà figé la bande." << std::endl;
        return;
    }

    if (open != nullptr)
    {
        // déjà ouverte : on bascule visible/masquée (on ne détruit pas)
        const bool show = ! open->isVisible();
        open->setVisible (show);

        if (show)
            open->toFront (true);

        std::cout << "[rack] éditeur " << index << (show ? " affiché" : " masqué") << std::endl;
        return;
    }

    // JUCE 9 : createEditorIfNeeded() est DÉPRÉCIÉ au profit de
    // createEditorAndMakeActive() (même implémentation, nom moins trompeur —
    // il renvoie nullptr si un éditeur est DÉJÀ actif, d'où le getActiveEditor).
    //
    // Ouvrir l'éditeur d'un plugin Wine peut aussi faire mourir son host — et
    // l'exception que yabridge lève alors emportait toute la bande. On la
    // rattrape : au pire l'utilisateur n'a pas de fenêtre, il garde son audio.
    std::unique_ptr<StageEditorWindow> win;

    // Deadman : si l'appel ci-dessous ne revient jamais, ce fichier survit au
    // force-exit du watchdog et le prochain démarrage saura qui blâmer.
    {
        auto dm = editorDeadmanFile();
        dm.getParentDirectory().createDirectory();
        dm.replaceWithText (path);
    }

    try
    {
        auto* ed = inst->getActiveEditor();

        if (ed == nullptr)
            ed = inst->createEditorAndMakeActive();

        if (ed == nullptr)                              // JS Inflator & co.
            ed = new juce::GenericAudioProcessorEditor (*inst);

        win = std::make_unique<StageEditorWindow> (name, ed);
        editorDeadmanFile().deleteFile();               // rendu la main : innocent
        std::cout << "[rack] éditeur " << index << " ouvert (" << name << ")" << std::endl;
    }
    // Un échec PROPRE (exception rattrapée) n'est PAS un blocage : on effface le
    // deadman, sinon on blacklisterait un plugin qui a juste refusé poliment.
    catch (const std::exception& e)
    {
        editorDeadmanFile().deleteFile();
        std::cout << "[rack] éditeur " << index << " : ouverture impossible — "
                  << e.what() << std::endl;
        return;
    }
    catch (...)
    {
        editorDeadmanFile().deleteFile();
        std::cout << "[rack] éditeur " << index << " : ouverture impossible." << std::endl;
        return;
    }

    // La chaîne a pu bouger pendant l'ouverture (elle est longue) : on ne
    // rattache la fenêtre que si l'étage est toujours le même.
    const juce::ScopedLock sl (lock_);

    if (juce::isPositiveAndBelow (index, stages_.size())
        && stages_.getUnchecked (index)->inst.get() == inst)
        stages_.getUnchecked (index)->editor = std::move (win);
}

//==============================================================================
bool Rack::loadFile (const juce::File& rackJson)
{
    if (! rackJson.existsAsFile())
    {
        std::cout << "[rack] pas de rack à " << rackJson.getFullPathName()
                  << " — on démarre vide." << std::endl;
        return false;
    }

    auto parsed = juce::JSON::parse (rackJson.loadFileAsString());

    if (! parsed.isObject())
    {
        std::cout << "[rack] rack.json illisible." << std::endl;
        return false;
    }

    const juce::ScopedLock sl (lock_);
    stages_.clear();

    if (auto* arr = parsed.getProperty ("stages", {}).getArray())
    {
        for (const auto& item : *arr)
        {
            auto s = std::make_unique<Stage>();
            s->path = item.getProperty ("path", {}).toString();
            s->bypass.store ((bool) item.getProperty ("bypass", false));
            s->forceWake = (bool) item.getProperty ("wake", false);

            const auto b64 = item.getProperty ("state", {}).toString();

            if (b64.isNotEmpty())
                s->savedState.fromBase64Encoding (b64);

            s->name = item.getProperty ("name", {}).toString();

            if (s->name.isEmpty())
                s->name = scan_.displayName (s->path);

            stages_.add (s.release());
        }
    }

    bypassAll_.store ((bool) parsed.getProperty ("bypass", false));

    std::cout << "[rack] " << stages_.size() << " étage(s) depuis "
              << rackJson.getFullPathName() << std::endl;
    return true;
}

bool Rack::saveFile (const juce::File& rackJson)
{
    juce::DynamicObject::Ptr root (new juce::DynamicObject());
    juce::Array<juce::var> arr;

    // Photographie de la chaîne SOUS le verrou, capture des états EN DEHORS.
    //
    // `getStateInformation` d'un plugin Wine est un aller-retour sur sa socket,
    // et bien plus lourd qu'une lecture de paramètre. Le tenir sous le verrou
    // faisait passer du signal NON TRAITÉ (le thread audio n'attend jamais le
    // verrou), et l'écriture automatique du rack rendrait ça fréquent. Sûr parce
    // que la chaîne n'est mutée que depuis CE thread.
    struct Photo
    {
        juce::String path, name;
        bool bypass = false;
        juce::AudioPluginInstance* inst = nullptr;
        juce::MemoryBlock saved;                 // état relu du rack, si non instancié
    };

    juce::Array<Photo> photos;

    {
        const juce::ScopedLock sl (lock_);

        for (auto* s : stages_)
            photos.add ({ s->path, s->name, s->bypass.load(), s->inst.get(),
                          s->inst != nullptr ? juce::MemoryBlock() : s->savedState });
    }

    {
        for (const auto& ph : photos)
        {
            juce::DynamicObject::Ptr o (new juce::DynamicObject());
            o->setProperty ("path", ph.path);
            // Le nom est SAUVÉ, pas seulement déduit : c'est ce que Douze affiche
            // quand la bande est arrêtée ou ne répond pas encore. Sans lui, un
            // chemin de shell VST3 (« WaveShell1-VST3 16.7_x64@0xe39a6c6d »)
            // s'affichait à la place de « RDeEsser Stereo ».
            o->setProperty ("name", ph.name);
            o->setProperty ("bypass", ph.bypass);

            juce::MemoryBlock mb;

            if (ph.inst != nullptr)
            {
                // Un plugin Wine peut jeter ici (host mort) : on préfère un rack
                // sans son état qu'une bande emportée par une sauvegarde.
                try                     { ph.inst->getStateInformation (mb); }
                catch (...)             { mb = {}; }
            }
            else
            {
                mb = ph.saved;                           // jamais instancié : on re-sauve tel quel
            }

            if (mb.getSize() > 0)
                o->setProperty ("state", mb.toBase64Encoding());

            arr.add (juce::var (o.get()));
        }
    }

    root->setProperty ("version", 1);
    root->setProperty ("bypass", bypassAll_.load());
    root->setProperty ("stages", arr);

    rackJson.getParentDirectory().createDirectory();

    // écriture atomique : un crash ne doit pas laisser un rack tronqué
    juce::TemporaryFile tmp (rackJson);

    if (! tmp.getFile().replaceWithText (juce::JSON::toString (juce::var (root.get()), false)))
        return false;

    if (! tmp.overwriteTargetFileWithTemporary())
        return false;

    std::cout << "[rack] sauvé : " << rackJson.getFullPathName() << std::endl;
    return true;
}

//==============================================================================
void Rack::process (juce::AudioBuffer<float>& buffer, juce::MidiBuffer& midi)
{
    if (bypassAll_.load())
        return;

    // Le thread audio ne DOIT PAS attendre : si le rack est en cours de
    // modification (ajout d'étage, éditeur…), on laisse passer le signal sec.
    const juce::ScopedTryLock stl (lock_);

    if (! stl.isLocked())
        return;

    const int numSamples = buffer.getNumSamples();
    const int inCh       = buffer.getNumChannels();

    if (stages_.isEmpty() || work_.getNumChannels() < inCh || numSamples > work_.getNumSamples())
        return;

    // entrée -> buffer de travail (canaux surnuméraires = silence)
    for (int ch = 0; ch < work_.getNumChannels(); ++ch)
    {
        if (ch < inCh)
            work_.copyFrom (ch, 0, buffer, ch, 0, numSamples);
        else
            work_.clear (ch, 0, numSamples);
    }

    juce::AudioBuffer<float> view (work_.getArrayOfWritePointers(),
                                   work_.getNumChannels(), numSamples);

    for (auto* s : stages_)
    {
        if (s->inst == nullptr || s->bypass.load() || s->dead.load())
            continue;

        // re-silencer les canaux au-delà de la bande : un étage précédent a pu
        // y écrire, et le suivant les prendrait pour un vrai sidechain.
        for (int ch = inCh; ch < view.getNumChannels(); ++ch)
            view.clear (ch, 0, numSamples);

        // Un plugin hébergé via Wine JETTE une exception depuis le thread audio
        // quand son host Wine meurt (yabridge : asio lève sur socket fermée).
        // Non rattrapée, elle abattait TOUTE la bande — micro coupé en pleine
        // conversation. On la rattrape, on marque l'étage mort, et le reste de
        // la chaîne continue de sortir du son.
        try
        {
            s->inst->processBlock (view, midi);
        }
        catch (...)
        {
            s->dead.store (true);   // pas de log ici : on est sur le thread audio
        }

        // Crête EN SORTIE de cet étage, cumulée jusqu'à la prochaine lecture.
        // C'est ce qui permet de voir OÙ le signal se perd ou sature dans une
        // chaîne : les niveaux d'entrée et de sortie de la bande ne disent que
        // « ça rentre » et « ça sort », jamais lequel des plugins est en cause.
        float crete = 0.0f;

        for (int ch = 0; ch < inCh; ++ch)
            crete = juce::jmax (crete, view.getMagnitude (ch, 0, numSamples));

        float vu = s->peak.load (std::memory_order_relaxed);

        while (crete > vu
               && ! s->peak.compare_exchange_weak (vu, crete, std::memory_order_relaxed))
        {}
    }

    for (int ch = 0; ch < inCh; ++ch)
        buffer.copyFrom (ch, 0, view, ch, 0, numSamples);
}

} // namespace douze
