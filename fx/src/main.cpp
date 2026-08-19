// Douze FX — host VST3 standalone branché dans le graphe PipeWire via JACK.
//
// Compagnon de Douze (~/workspace/douze). Une INSTANCE = une BANDE : une
// source, une chaîne de plugins, une destination. Le multi-bande se fait par
// multi-process (1 client JACK par bande) — cf. docs/DOUZE-FX-BRIEF.md.
//
//   ./tools/run-douze-fx.sh --list-devices
//   ./tools/run-douze-fx.sh --in "SSL 12 Pro" --in-ch 1 --out "SSL 12 Playback 3-4"
//
// Options : --in/--out (clients JACK), --in-ch/--out-ch (canaux en base 1 dans
// l'ordre du client ; une seule valeur = source MONO, dupliquée au centre),
// --channels, --rack, --add, --alsa, --list-devices, --list-plugins, --scan.
//
// Jalon 1 : console + commandes clavier. GUI et contrôle HTTP = jalons suivants.

#include <juce_audio_devices/juce_audio_devices.h>
#include <juce_audio_utils/juce_audio_utils.h>

#include <chrono>
#include <csignal>
#include <cstdlib>
#include <iostream>
#include <thread>

#include "HttpApi.h"
#include "PluginScan.h"
#include "Rack.h"
#include "ScanShell.h"
#include "SelfTest.h"

namespace douze
{

static juce::File defaultRackFile()
{
    return juce::File::getSpecialLocation (juce::File::userHomeDirectory)
             .getChildFile (".config/douze-fx/rack.json");
}

//==============================================================================
// Battement du thread AUDIO. Déclaré ici parce que le callback audio est écrit
// avant le watchdog qui le lit ; le reste de la mécanique est plus bas, dans
// `namespace watchdog`.
//
// Un COMPTEUR et pas une horloge : le thread audio ne doit pas appeler l'heure à
// chaque bloc. Le watchdog, lui, regarde simplement s'il a bougé depuis sa
// dernière ronde.
namespace watchdog { static std::atomic<juce::uint64> audioBeat { 0 }; }

class Bande final : public juce::AudioIODeviceCallback
{
public:
    Bande() : rack_ (scan_) {}

    PluginScan& scan() noexcept { return scan_; }
    Rack& rack() noexcept       { return rack_; }

    //== thread audio ==========================================================
    void audioDeviceIOCallbackWithContext (const float* const* inputChannelData,
                                           int numInputChannels,
                                           float* const* outputChannelData,
                                           int numOutputChannels,
                                           int numSamples,
                                           const juce::AudioIODeviceCallbackContext&) override
    {
        // Tout premier geste, avant même le contrôle de format : un retour
        // anticipé signifie quand même que le thread audio VIT, et c'est
        // exactement ce que le watchdog a besoin de savoir.
        watchdog::audioBeat.fetch_add (1, std::memory_order_relaxed);

        const int n = juce::jmax (numInputChannels, numOutputChannels);

        if (io_.getNumChannels() < n || io_.getNumSamples() < numSamples)
            return;                                    // format inattendu : on ne bricole pas

        juce::AudioBuffer<float> view (io_.getArrayOfWritePointers(), n, numSamples);

        for (int ch = 0; ch < n; ++ch)
        {
            if (ch < numInputChannels && inputChannelData[ch] != nullptr)
                juce::FloatVectorOperations::copy (view.getWritePointer (ch),
                                                   inputChannelData[ch], numSamples);
            else
                view.clear (ch, 0, numSamples);
        }

        // mono d'entrée sur une bande stéréo (cas micro Analogue 1) : on
        // duplique, sinon la moitié de la chaîne travaillerait sur du silence.
        if (numInputChannels == 1 && n > 1)
            for (int ch = 1; ch < n; ++ch)
                juce::FloatVectorOperations::copy (view.getWritePointer (ch),
                                                   view.getReadPointer (0), numSamples);

        // Niveau d'ENTRÉE (avant la chaîne) : « est-ce que le signal arrive ? »
        accumulatePeak (inPeak_, view, juce::jmax (1, numInputChannels), numSamples);

        midi_.clear();

        // Charge DSP = temps passé dans la chaîne / budget temps réel du bloc.
        // C'est LE chiffre utile (« est-ce que la chaîne tient le temps réel ? »),
        // et il reste lisible même à 0,05 % — contrairement au CPU process, qui
        // noie le DSP dans le reste. Lissé (EMA ~20 blocs) pour ne pas clignoter.
        const auto t0 = juce::Time::getHighResolutionTicks();
        rack_.process (view, midi_);

        if (budgetSeconds_ > 0.0)
        {
            const double spent = juce::Time::highResolutionTicksToSeconds (
                                     juce::Time::getHighResolutionTicks() - t0);
            const float f = (float) (spent / budgetSeconds_);
            load_.store (load_.load() * 0.95f + f * 0.05f, std::memory_order_relaxed);
        }

        // Niveau de SORTIE (après la chaîne) : « est-ce que ça ressort ? »
        // Les deux ensemble disent d'un coup d'œil OÙ un signal disparaît —
        // à l'entrée, dans la chaîne, ou après.
        accumulatePeak (outPeak_, view, juce::jmax (1, numOutputChannels), numSamples);

        for (int ch = 0; ch < numOutputChannels; ++ch)
        {
            if (outputChannelData[ch] == nullptr)
                continue;

            if (ch < n)
                juce::FloatVectorOperations::copy (outputChannelData[ch],
                                                   view.getReadPointer (ch), numSamples);
            else
                juce::FloatVectorOperations::clear (outputChannelData[ch], numSamples);
        }
    }

    //== message thread ========================================================
    void audioDeviceAboutToStart (juce::AudioIODevice* device) override
    {
        const int numCh = juce::jmax (device->getActiveInputChannels().countNumberOfSetBits(),
                                      device->getActiveOutputChannels().countNumberOfSetBits(), 2);
        const int block = device->getCurrentBufferSizeSamples();

        io_.setSize (numCh, juce::jmax (block, 1), false, true, true);
        midi_.ensureSize (256);
        budgetSeconds_ = device->getCurrentSampleRate() > 0.0
                           ? block / device->getCurrentSampleRate() : 0.0;
        load_.store (0.0f);

        rack_.prepare (device->getCurrentSampleRate(), block, numCh);

        const int nIn = device->getActiveInputChannels().countNumberOfSetBits();

        std::cout << "[audio] " << device->getTypeName() << " / " << device->getName()
                  << " — " << device->getCurrentSampleRate() << " Hz, bloc " << block
                  << ", " << nIn << " entrée(s) → " << numCh << " canaux"
                  << (nIn == 1 ? " [source MONO → dupliquée au centre]" : "")
                  << " (latence chaîne " << rack_.totalLatencySamples() << " samples)"
                  << std::endl;
    }

    void audioDeviceStopped() override
    {
        std::cout << "[audio] arrêté." << std::endl;
    }

    /** Une bande est morte en `code 1` sans laisser un mot d'explication : ce
        code ne vient que de l'ouverture du device, donc quelque chose a fermé
        JACK sous nos pieds (serveur PipeWire qui redémarre, nœud du micro
        virtuel détruit…). On le NOMME désormais, au lieu de le déduire. */
    void audioDeviceError (const juce::String& message) override
    {
        std::cout << "[audio] ERREUR du device : " << message << std::endl;
        std::cout.flush();
    }

    /** Charge DSP de la chaîne, en % du temps réel (100 % = à la limite). */
    float dspLoadPercent() const noexcept { return load_.load (std::memory_order_relaxed) * 100.0f; }

    /** Crête depuis le DERNIER appel (et remise à zéro).

        « Depuis la dernière lecture » plutôt qu'une valeur instantanée : même
        interrogé deux fois par seconde, le vumètre ne rate aucun transitoire. */
    float takeInPeak()  noexcept { return inPeak_.exchange (0.0f); }
    float takeOutPeak() noexcept { return outPeak_.exchange (0.0f); }

private:
    /** Retient la crête la plus forte vue depuis la dernière lecture. */
    static void accumulatePeak (std::atomic<float>& slot,
                                const juce::AudioBuffer<float>& buf,
                                int numChannels, int numSamples) noexcept
    {
        float peak = 0.0f;

        for (int ch = 0; ch < juce::jmin (numChannels, buf.getNumChannels()); ++ch)
            peak = juce::jmax (peak, buf.getMagnitude (ch, 0, numSamples));

        float cur = slot.load (std::memory_order_relaxed);

        while (peak > cur && ! slot.compare_exchange_weak (cur, peak,
                                                           std::memory_order_relaxed))
        {}
    }

    PluginScan scan_;
    Rack rack_;
    juce::AudioBuffer<float> io_;
    juce::MidiBuffer midi_;
    std::atomic<float> load_ { 0.0f };
    std::atomic<float> inPeak_ { 0.0f }, outPeak_ { 0.0f };
    double budgetSeconds_ = 0.0;
};

//==============================================================================
/** Exécute une opération SUR LE MESSAGE THREAD et attend le résultat.

    Le serveur HTTP répond sur son propre thread, mais instancier un plugin ou
    ouvrir un éditeur n'est légal que sur le message thread (leçon Delestor :
    tout ce qui touche à un plugin hébergé passe par lui). */
static bool onMessageThread (std::function<void()> fn, int timeoutMs = 60000)
{
    if (juce::MessageManager::getInstance()->isThisTheMessageThread())
    {
        fn();
        return true;
    }

    juce::WaitableEvent done;
    juce::MessageManager::callAsync ([&fn, &done] { fn(); done.signal(); });
    return done.wait (timeoutMs);
}

//==============================================================================
// Watchdog du THREAD DE CONTRÔLE (leçon Delestor, payée deux fois).
//
// Certains plugins Wine ne rendent jamais la main : ouvrir l'éditeur natif d'un
// Waves fige le message thread pour de bon. Le process reste vivant, l'audio
// continue (le chemin audio ne prend jamais de verrou bloquant) mais plus rien
// n'est pilotable — et rien ne le remarque. On surveille donc un battement de
// cœur émis par le message thread lui-même : s'il s'arrête, on sort en force et
// le superviseur relance la bande (quelques secondes de son perdues, au lieu
// d'une bande définitivement muette pour le contrôle).
//
// Deux pièges appris sur Delestor :
//   - le budget doit s'ÉLARGIR pour les opérations légitimement lentes
//     (instancier un plugin yabridge prend des secondes, un Acustica ~25 s) ;
//   - il doit couvrir l'opération ET SON CONTRECOUP : le travail déclenché par
//     une restauration d'état se déroule APRÈS l'appel, dans la pompe. D'où la
//     fenêtre de grâce.
namespace watchdog
{
    static constexpr int kIdleBudgetMs  = 8000;    // au repos : 8 s sans battement = figé
    static constexpr int kGraceBudgetMs = 30000;   // contrecoup d'une opération lourde
    static constexpr int kGraceMs       = 20000;   // durée de la grâce

    static std::atomic<juce::int64> beat { 0 };    // 0 = pas encore armé (démarrage)
    static std::atomic<juce::int64> graceUntil { 0 };
    static std::atomic<int> budgetMs { kIdleBudgetMs };
    static std::atomic<const char*> phase { "repos" };
    // Publié dans `/state` : le thread de contrôle est pendu, mais l'audio et
    // l'API vont bien — donc autant le dire clairement à qui interroge.
    static std::atomic<bool> frozen { false };

    /** Élargit le budget le temps d'une opération lourde, puis laisse une grâce. */
    struct Phase
    {
        Phase (const char* what, int ms)
            : prevPhase (phase.exchange (what)), prevBudget (budgetMs.exchange (ms)) {}

        ~Phase()
        {
            phase.store (prevPhase);
            budgetMs.store (prevBudget);
            graceUntil.store (juce::Time::currentTimeMillis() + kGraceMs);
        }

        const char* prevPhase;
        int prevBudget;
    };

    /** Battement émis par le message thread (donc muet dès qu'il est bloqué). */
    class Heart final : private juce::Timer
    {
    public:
        Heart() { startTimer (250); }
        ~Heart() override { stopTimer(); }

    private:
        void timerCallback() override { beat.store (juce::Time::currentTimeMillis()); }
    };

    static void start()
    {
        std::thread ([]
        {
            juce::uint64 audioVuAvant = audioBeat.load (std::memory_order_relaxed);
            bool signale = false;      // le gel n'est annoncé qu'UNE fois

            for (;;)
            {
                std::this_thread::sleep_for (std::chrono::milliseconds (500));

                const auto audioVu = audioBeat.load (std::memory_order_relaxed);
                const bool audioVivant = (audioVu != audioVuAvant);
                audioVuAvant = audioVu;

                const auto last = beat.load();

                if (last == 0)                     // pas encore armé
                    continue;

                const auto now = juce::Time::currentTimeMillis();
                auto budget = budgetMs.load();

                if (now < graceUntil.load())
                    budget = juce::jmax (budget, kGraceBudgetMs);

                if (now - last <= budget)
                {
                    signale = false;               // revenu à lui : on réarme l'annonce
                    frozen.store (false);          // ... et il n'est plus figé
                    continue;
                }

                // DEUX battements, deux verdicts. Le thread de contrôle figé ne
                // dit RIEN de l'audio : quand un éditeur Wine ne rend pas la
                // main, c'est ce thread-là qui pend pendant que le thread audio
                // continue de traiter, imperturbable.
                //
                // On tuait quand même — et on coupait donc le son de quelqu'un
                // qui était peut-être en pleine conversation, pour réparer une
                // fenêtre. Le remède était pire que le mal : les six secondes de
                // silence étaient de NOTRE fait, pas de celui du plugin.
                if (audioVivant)
                {
                    frozen.store (true);

                    if (! signale)
                    {
                        signale = true;
                        std::cout << "[watchdog] thread de contrôle FIGÉ (> " << budget
                                  << " ms) dans la phase : << " << phase.load()
                                  << " >> — mais le thread audio TOURNE : on garde le son."
                                  << " La bande ne répond plus à son API ; à relancer"
                                  << " quand ça t'arrange." << std::endl;
                        std::cout.flush();
                    }
                    continue;
                }

                // L'audio s'est arrêté LUI AUSSI : plus rien à sauver, et rester
                // en vie n'offrirait qu'une bande muette et sourde.
                std::cout << "[watchdog] thread de contrôle ET thread audio figés (> "
                          << budget << " ms) dans la phase : << " << phase.load()
                          << " >> — sortie forcée, le superviseur relancera la bande."
                          << std::endl;
                std::cout.flush();
                std::_Exit (70);
            }
        }).detach();
    }
} // namespace watchdog

//==============================================================================
/** Écrit le rack tout seul, peu après une modification de chaîne.

    Sans ça, ajouter ou retirer un plugin ne survivait pas au redémarrage de la
    bande à moins de penser à cliquer « Enregistrer » — et une bande redémarre
    d'elle-même (crash d'un plugin, watchdog). Du travail perdu sans avertissement.

    DIFFÉRÉ, parce qu'une écriture capture l'état interne de chaque plugin (un
    aller-retour Wine chacun) : on ne veut pas le payer à chaque clic d'une rafale
    de réglages, mais une seule fois quand elle se calme. */
class AutoSave final : private juce::Timer
{
public:
    AutoSave (Rack& rack, juce::File file)
        : rack_ (rack), file_ (std::move (file)) {}

    ~AutoSave() override { stopTimer(); }

    void touch() { startTimer (kDelayMs); }

private:
    static constexpr int kDelayMs = 2500;

    void timerCallback() override
    {
        stopTimer();
        const watchdog::Phase ph ("sauvegarde automatique du rack", 20000);

        if (rack_.saveFile (file_))
            std::cout << "[rack] sauvegarde automatique." << std::endl;
    }

    Rack& rack_;
    juce::File file_;
};

//==============================================================================
/** Réinstancie, sur le message thread, les étages tombés pendant le traitement.

    Le sondage est bon marché (un booléen sous verrou) ; on n'élargit le budget du
    watchdog que quand il y a vraiment une reprise à tenter — sinon la fenêtre de
    grâce serait rouverte toutes les deux secondes et le watchdog ne détecterait
    plus jamais rien. */
class Repriseur final : private juce::Timer
{
public:
    explicit Repriseur (Rack& rack) : rack_ (rack) { startTimer (2000); }
    ~Repriseur() override { stopTimer(); }

private:
    void timerCallback() override
    {
        if (! rack_.needsRecovery())
            return;

        // 20 s et pas 90 : instancier un plugin yabridge prend ~7 s, donc c'est
        // large — et si quelque chose bloque malgré tout, la casse est bornée à
        // 20 s de contrôle figé au lieu d'une minute et demie.
        const watchdog::Phase ph ("reprise d'un étage tombé", 20000);
        rack_.superviseStages();
    }

    Rack& rack_;
};

static juce::var jsonBody (const juce::String& body)
{
    return juce::JSON::parse (body);
}

static juce::String jsonOut (juce::DynamicObject::Ptr o)
{
    return juce::JSON::toString (juce::var (o.get()), false);
}

static HttpApi::Reply okReply (juce::DynamicObject::Ptr o) { return { 200, jsonOut (o) }; }

static HttpApi::Reply errReply (int code, const juce::String& msg)
{
    juce::DynamicObject::Ptr o (new juce::DynamicObject());
    o->setProperty ("error", msg);
    return { code, jsonOut (o) };
}

//==============================================================================
static void printDevices (juce::AudioDeviceManager& adm)
{
    for (auto* type : adm.getAvailableDeviceTypes())
    {
        type->scanForDevices();
        std::cout << "== type « " << type->getTypeName() << " » ==" << std::endl;

        std::cout << "  entrées  : ";
        for (const auto& n : type->getDeviceNames (true))  std::cout << "[" << n << "] ";
        std::cout << std::endl;

        std::cout << "  sorties  : ";
        for (const auto& n : type->getDeviceNames (false)) std::cout << "[" << n << "] ";
        std::cout << std::endl;
    }
}

//==============================================================================
// Scan d'UN fichier, dans ce process jetable.
//
// Contrat (identique à celui du scanner emprunté jusqu'ici à Delestor, pour
// qu'on puisse basculer de l'un à l'autre par une variable d'environnement) :
//
//     douze_fx --scanone <fichier> <sortie>
//
// Une ligne « DESC <xml> » par type trouvé, dans <sortie>. Pourquoi un fichier
// et pas stdout : les plugins Windows écrivent leur propre bruit sur la sortie
// standard, et un host Wine bavard suffit à saturer le tube — le parent
// attendrait alors un enfant qui attend qu'on le lise.
//
// Codes de retour : 0 = au moins un type, 2 = aucun. Un gel ou un plantage ne
// rend rien du tout, et c'est voulu : le parent tranche par timeout.

static juce::String descsVersDesc (const juce::OwnedArray<juce::PluginDescription>& descs)
{
    juce::String out;

    for (auto* d : descs)
        out << "DESC "
            << d->createXml()->toString (juce::XmlElement::TextFormat().singleLine())
            << "\n";

    return out;
}

static int ecrireDescs (const juce::String& texte, const juce::String& sortie)
{
    if (sortie.isNotEmpty())
        juce::File (sortie).replaceWithText (texte);
    else
        std::cout << texte << std::flush;

    return 0;
}

static int runScanOne (const juce::String& fichier, const juce::String& sortie)
{
    juce::AudioPluginFormatManager fm;
    juce::addDefaultFormatsToManager (fm);

    juce::OwnedArray<juce::PluginDescription> descs;

    for (auto* fmt : fm.getFormats())
    {
        // C'est CET appel qui peut figer : il instancie le plugin pour compter
        // ses canaux. On l'isole dans ce process plutôt que de l'éviter.
        fmt->findAllTypesForFile (descs, fichier);

        if (! descs.isEmpty())
            break;
    }

    if (descs.isEmpty())
        return 2;

    return ecrireDescs (descsVersDesc (descs), sortie);
}

// Repêchage des shells : énumération de la factory, sans instancier. On
// reconstruit les descriptions à la main — les canaux restent à 0, comme le
// chemin rapide `moduleinfo.json` de JUCE, parce qu'on ne les connaît pas sans
// instancier. C'est exactement le compromis : un catalogue complet avec des
// canaux inconnus vaut mieux qu'un scan qui n'aboutit jamais.
static int runScanShell (const juce::String& fichier, const juce::String& sortie)
{
    ShellScanResult res;
    std::string err;

    if (! scanShellEnumerate (fichier.toStdString(), res, err) || res.classes.empty())
    {
        if (! err.empty())
            std::cerr << "[scanshell] " << err << std::endl;

        return 2;
    }

    const juce::File f (fichier);
    juce::String out;

    for (const auto& cls : res.classes)
    {
        juce::PluginDescription d;
        d.name = juce::String (juce::CharPointer_UTF8 (cls.name.c_str()));
        d.descriptiveName = d.name;
        d.pluginFormatName = "VST3";
        d.fileOrIdentifier = f.getFullPathName();
        d.lastFileModTime = f.getLastModificationTime();
        d.lastInfoUpdateTime = juce::Time::getCurrentTime();

        // Vendeur de la FACTORY d'abord, celui de la classe en repli : c'est la
        // règle que suit JUCE, et s'en écarter ferait apparaître les mêmes
        // plugins sous deux marques selon la façon dont ils ont été scannés.
        d.manufacturerName = juce::String (juce::CharPointer_UTF8 (res.factoryVendor.c_str())).trim();

        if (d.manufacturerName.isEmpty())
            d.manufacturerName = juce::String (juce::CharPointer_UTF8 (cls.vendor.c_str()));

        d.version = juce::String (juce::CharPointer_UTF8 (cls.version.c_str()));
        d.category = juce::String (juce::CharPointer_UTF8 (cls.subCategories.c_str()));
        d.isInstrument = d.category.containsIgnoreCase ("Instrument");
        d.numInputChannels = 0;
        d.numOutputChannels = 0;
        d.uniqueId = cls.uniqueId;
        d.deprecatedUid = cls.deprecatedUid;

        out << "DESC "
            << d.createXml()->toString (juce::XmlElement::TextFormat().singleLine())
            << "\n";
    }

    std::cerr << "[scanshell] " << fichier << " : " << (int) res.classes.size()
              << " classe(s) audio (factory seule)." << std::endl;

    return ecrireDescs (out, sortie);
}

static void printPlugins (const PluginScan& scan, const juce::String& filter)
{
    int shown = 0;

    for (const auto& d : scan.types())
    {
        if (filter.isNotEmpty() && ! d.name.containsIgnoreCase (filter)
            && ! d.manufacturerName.containsIgnoreCase (filter))
            continue;

        std::cout << "  " << d.name << "  [" << d.manufacturerName << "]  "
                  << d.fileOrIdentifier << std::endl;
        ++shown;
    }

    std::cout << "  → " << shown << " / " << scan.numTypes()
              << " plugin(s)." << std::endl;
}

//==============================================================================
//==============================================================================
/** Petit parseur d'arguments.

    juce::ArgumentList n'accepte « --opt valeur » que pour les options COURTES
    (les longues exigent « --opt=valeur ») — or les noms de clients JACK
    contiennent des espaces et se passent naturellement en argument séparé.
    On accepte donc les DEUX formes. */
struct Args
{
    Args (int argc, char* argv[])
    {
        for (int i = 1; i < argc; ++i)
        {
            const juce::String a { juce::CharPointer_UTF8 (argv[i]) };

            if (! a.startsWith ("--"))
            {
                // Argument LIBRE (non consommé comme valeur d'une option juste
                // avant). Sert au contrat du scanner — `--scanone <fichier>
                // <sortie>` — qui est celui du scanner de Delestor : le garder
                // identique permet de basculer de l'un à l'autre par une seule
                // variable d'environnement.
                libres.add (a);
                continue;
            }

            auto name = a.upToFirstOccurrenceOf ("=", false, false);
            juce::String val = a.contains ("=") ? a.fromFirstOccurrenceOf ("=", false, false)
                                                : juce::String();

            if (val.isEmpty() && i + 1 < argc)
            {
                const juce::String next { juce::CharPointer_UTF8 (argv[i + 1]) };

                if (! next.startsWith ("--"))
                {
                    val = next;
                    ++i;
                }
            }

            names.add (name);
            values.add (val);
        }
    }

    bool has (juce::StringRef n) const { return names.contains (n); }

    juce::String get (juce::StringRef n, const juce::String& def = {}) const
    {
        const int i = names.indexOf (n);
        return i >= 0 && values[i].isNotEmpty() ? values[i] : def;
    }

    /** Nᵉ argument libre, ou "" — jamais d'accès hors bornes chez l'appelant. */
    juce::String positional (int n) const
    {
        return juce::isPositiveAndBelow (n, libres.size()) ? libres[n] : juce::String();
    }

    juce::StringArray names, values, libres;
};

static void printHelp()
{
    std::cout <<
        "\ncommandes :\n"
        "  ls                  état du rack\n"
        "  find <texte>        cherche un plugin dans le catalogue\n"
        "  add <chemin.vst3>   ajoute un étage en fin de chaîne (« …vst3@0xUID » pour un shell)\n"
        "  rm <n>              retire l'étage n\n"
        "  e <n>               ouvre / masque l'éditeur natif de l'étage n\n"
        "  b <n>               bascule le bypass de l'étage n (A/B)\n"
        "  bypass              bypass global (toute la bande)\n"
        "  scan [chemin…]      scanne (défaut : emplacements VST3 standard)\n"
        "  save                sauve le rack (états des plugins compris)\n"
        "  dev                 liste les clients JACK visibles\n"
        "  q                   quitter\n" << std::endl;
}

} // namespace douze

//==============================================================================
int main (int argc, char* argv[])
{
    // ⚠️ SIGPIPE IGNORÉ, en toute première chose.
    //
    // Un plugin hébergé via yabridge vit dans un process Wine, au bout d'une
    // socket. Quand ce process meurt, la moindre écriture vers lui lève SIGPIPE
    // — dont l'action par défaut est de TUER le process. Toute la bande
    // disparaissait donc à cause d'un plugin, sans qu'aucun try/catch ne puisse
    // s'y opposer : un signal n'est pas une exception. Constaté sur SPL De-Esser
    // Dual-Band (mort en `code -13`), alors que le plugin s'héberge très bien
    // ailleurs. Ignoré, `write()` rend EPIPE, yabridge lève une exception que
    // l'on rattrape déjà, et l'étage est simplement marqué mort.
    std::signal (SIGPIPE, SIG_IGN);

    juce::ScopedJuceInitialiser_GUI juceInit;

    const douze::Args args (argc, argv);
    douze::Bande bande;

    const auto rackFile = args.has ("--rack") ? juce::File (args.get ("--rack"))
                                              : douze::defaultRackFile();

    // Autotests HORS device audio : ils doivent pouvoir tourner sans JACK, sans
    // carte, et sans couper le micro de personne.
    if (args.has ("--selftest"))
        return douze::runSelfTests (args.get ("--selftest"));

    if (args.has ("--list-plugins"))
    {
        douze::printPlugins (bande.scan(), args.get ("--list-plugins"));
        return 0;
    }

    // --- scan d'UN fichier, dans CE process jetable --------------------------
    //
    // C'est le mode que le superviseur (tools/douzefx.py) appelle en boucle, un
    // process par plugin. Tout l'intérêt est là : `findAllTypesForFile` INSTANCIE
    // le plugin pour compter ses canaux, donc un plugin qui gèle ou qui plante le
    // fait ICI — dans un enfant que le parent tue par timeout — au lieu
    // d'emporter le scan entier.
    //
    // Le résultat part dans un FICHIER, pas sur stdout : les plugins Windows
    // écrivent leur propre bruit sur la sortie standard, et un host Wine bavard
    // suffit à saturer le tube et à bloquer tout le monde.
    if (args.has ("--scanone"))
        return douze::runScanOne (args.get ("--scanone"), args.positional (0));

    // Repêchage des SHELLS VST3 (un binaire, N sous-plugins — WaveShell en a
    // 209) : le scan normal les instancie un par un jusqu'à faire déborder la
    // pile d'un thread Wine. Ici on énumère la factory SANS rien instancier.
    if (args.has ("--scanshell"))
        return douze::runScanShell (args.get ("--scanshell"), args.positional (0));

    if (args.has ("--scan"))
    {
        const auto paths = args.get ("--scan");
        const int added = paths.isNotEmpty()
                            ? bande.scan().scanPaths (juce::StringArray::fromTokens (paths, ":", {}))
                            : bande.scan().scanDefaultPaths();
        std::cout << "[scan] +" << added << " plugin(s)." << std::endl;
        return 0;
    }

    juce::AudioDeviceManager adm;

    if (args.has ("--list-devices"))
    {
        douze::printDevices (adm);
        return 0;
    }

    // --- ouverture du device --------------------------------------------------
    const juce::String wanted = args.has ("--alsa") ? "ALSA" : "JACK";
    const int numCh = juce::jmax (1, args.get ("--channels", "2").getIntValue());

    // Canaux SOURCE / DESTINATION, en base 1 dans l'ordre du client JACK visé.
    // Ex. sur « SSL 12 Pro » : 1 = Analogue 1 (capture_AUX0), 2 = Analogue 2, …
    // C'est CE réglage qui rend la bande applicable n'importe où (cf. brief,
    // § Points d'insertion) ; une seule valeur = source MONO.
    const auto parseChannels = [numCh] (const juce::String& spec)
    {
        juce::BigInteger bits;

        if (spec.isEmpty())
        {
            bits.setRange (0, numCh, true);
            return bits;
        }

        for (const auto& tok : juce::StringArray::fromTokens (spec, ",", {}))
            if (const int idx = tok.trim().getIntValue(); idx >= 1)
                bits.setBit (idx - 1);

        return bits;
    };

    juce::AudioDeviceManager::AudioDeviceSetup setup;
    setup.inputDeviceName  = args.get ("--in");
    setup.outputDeviceName = args.get ("--out");
    setup.useDefaultInputChannels  = false;
    setup.useDefaultOutputChannels = false;
    setup.inputChannels  = parseChannels (args.get ("--in-ch"));
    setup.outputChannels = parseChannels (args.get ("--out-ch"));

    // Taille de bloc PAR BANDE (décision produit) : posée au lancement, donc
    // figée tant que la bande tourne. Côté PipeWire c'est PIPEWIRE_LATENCY qui
    // fait foi (cf. tools/run-douze-fx.sh) ; on la redemande ici pour que le
    // backend JACK ne négocie pas autre chose dans notre dos.
    if (const int block = args.get ("--block", "0").getIntValue(); block > 0)
        setup.bufferSize = block;

    // ⚠️ NE PAS appeler initialise() : il ouvre un device du type PAR DÉFAUT
    // (ALSA en tête de liste) avant qu'on ait pu demander JACK. Cette ouverture
    // ALSA transitoire peut se FIGER quand PipeWire tient déjà la carte (thread
    // « alsa-pipewire » bloqué, thread principal en attente de verrou) — la
    // bande ne démarrait alors jamais, sans le moindre message.
    // On se contente de faire créer la liste des types, on choisit le nôtre,
    // puis on applique notre setup.
    adm.getAvailableDeviceTypes();
    adm.setCurrentAudioDeviceType (wanted, false);

    if (auto err = adm.setAudioDeviceSetup (setup, true); err.isNotEmpty())
    {
        std::cout << "[audio] ÉCHEC : " << err << std::endl;
        std::cout << "  (JACK absent ? lance via tools/run-douze-fx.sh, qui pose le "
                     "libjack de PipeWire ; ou --alsa en repli.)" << std::endl;
        douze::printDevices (adm);
        return 1;
    }

    auto* dev = adm.getCurrentAudioDevice();

    if (dev == nullptr)
    {
        std::cout << "[audio] ÉCHEC : aucun device ouvert (type " << wanted << ")."
                  << std::endl;
        douze::printDevices (adm);
        return 1;
    }

    if (dev->getTypeName() != wanted)
        std::cout << "[audio] ATTENTION : backend obtenu = " << dev->getTypeName()
                  << " (demandé : " << wanted << ")" << std::endl;

    {
        const auto s = adm.getAudioDeviceSetup();
        std::cout << "[audio] entrée « " << s.inputDeviceName << " » → sortie « "
                  << s.outputDeviceName << " »" << std::endl;
    }

    // --- rack -----------------------------------------------------------------
    bande.rack().loadFile (rackFile);

    if (args.has ("--add"))
        bande.rack().addStage (args.get ("--add"));

    bande.rack().prepare (dev->getCurrentSampleRate(),
                          dev->getCurrentBufferSizeSamples(),
                          juce::jmax (numCh, 2));

    adm.addAudioCallback (&bande);

    // --- API de contrôle locale (le démon Douze pilote la bande par là) -------
    const juce::String stripName = args.get ("--name",
        juce::SystemStats::getEnvironmentVariable ("DOUZE_FX_NAME", "douze-fx"));

    std::unique_ptr<douze::HttpApi> api;

    // Déclaré AVANT l'API : le gestionnaire le capture par référence.
    douze::AutoSave autoSave (bande.rack(), rackFile);

    if (const int port = args.get ("--port", "0").getIntValue(); port > 0)
    {
        api = std::make_unique<douze::HttpApi> (port,
            [&bande, &adm, &autoSave, rackFile, stripName] (const juce::String& method,
                                                 const juce::String& path,
                                                 const juce::String& query,
                                                 const juce::String& body) -> douze::HttpApi::Reply
        {
            auto& rack = bande.rack();

            // ---------------- lecture ----------------
            if (method == "GET" && path == "/state")
            {
                juce::DynamicObject::Ptr o (new juce::DynamicObject());
                o->setProperty ("name", stripName);
                o->setProperty ("bypass", rack.bypassAll());

                // Le moteur DIT lui-même qu'il est figé, plutôt que de laisser
                // deviner. C'est possible précisément parce que `/state` répond
                // encore : il est servi depuis des valeurs en cache, sans passer
                // par le message thread — celui-là même qui est pendu. Sans ça,
                // la seule façon de détecter un gel serait le silence de l'API,
                // qui n'arrive justement pas dans ce cas.
                if (douze::watchdog::frozen.load())
                {
                    o->setProperty ("frozen", true);
                    o->setProperty ("frozen_phase",
                                    juce::String (juce::CharPointer_UTF8 (douze::watchdog::phase.load())));
                }
                // Charge DSP de la chaîne (2 décimales : une chaîne légère vaut
                // 0,05 %, l'arrondi au dixième l'affichait « 0.0 » et donnait
                // l'impression d'une mesure morte).
                o->setProperty ("cpu", juce::roundToInt (bande.dspLoadPercent() * 100.0f) / 100.0);
                o->setProperty ("cpu_process",
                                juce::roundToInt (adm.getCpuUsage() * 10000.0) / 100.0);

                // Crêtes linéaires 0..1 (la conversion en dB est l'affaire de
                // l'affichage) — lues et remises à zéro à chaque interrogation.
                o->setProperty ("in_peak", juce::roundToInt (bande.takeInPeak() * 10000.0f) / 10000.0);
                o->setProperty ("out_peak", juce::roundToInt (bande.takeOutPeak() * 10000.0f) / 10000.0);

                if (auto* cur = adm.getCurrentAudioDevice())
                {
                    o->setProperty ("backend", cur->getTypeName());
                    o->setProperty ("source", adm.getAudioDeviceSetup().inputDeviceName);
                    o->setProperty ("destination", adm.getAudioDeviceSetup().outputDeviceName);
                    o->setProperty ("sampleRate", cur->getCurrentSampleRate());
                    o->setProperty ("block", cur->getCurrentBufferSizeSamples());
                    o->setProperty ("xruns", cur->getXRunCount());
                    o->setProperty ("running", cur->isPlaying());
                }

                o->setProperty ("latency", rack.totalLatencySamples());

                juce::Array<juce::var> stages;

                for (const auto& s : rack.stageInfo())
                {
                    juce::DynamicObject::Ptr st (new juce::DynamicObject());
                    st->setProperty ("name", s.name);
                    st->setProperty ("path", s.path);
                    st->setProperty ("loaded", s.loaded);
                    st->setProperty ("editor_hangs", s.editorHangs);
                    st->setProperty ("peak", s.peak);
                    st->setProperty ("bypass", s.bypass);
                    st->setProperty ("latency", s.latency);
                    st->setProperty ("params", s.numParams);

                    if (s.error.isNotEmpty())
                        st->setProperty ("error", s.error);

                    stages.add (juce::var (st.get()));
                }

                o->setProperty ("stages", stages);
                return douze::okReply (o);
            }

            if (method == "GET" && path == "/plugins")
            {
                // Le scan tourne HORS de ce process (coordinateur de Douze) et
                // écrit le cache : on le relit s'il a bougé, sinon la GUI
                // demanderait « quoi de neuf ? » à un catalogue figé au démarrage.
                bande.scan().reloadCacheIfChanged();

                juce::String q;
                int limit = 50;

                for (const auto& kv : juce::StringArray::fromTokens (query, "&", {}))
                {
                    const auto k = kv.upToFirstOccurrenceOf ("=", false, false);
                    const auto v = juce::URL::removeEscapeChars (kv.fromFirstOccurrenceOf ("=", false, false));

                    if (k == "q")     q = v;
                    if (k == "limit") limit = juce::jmax (1, v.getIntValue());
                }

                // Un « shell » VST3 (WaveShell : 200+ plugins Waves dans UN
                // fichier) doit être désigné par « chemin@0xUID », sinon on
                // charge aveuglément le premier sous-plugin du fichier au lieu
                // de celui que l'utilisateur a choisi.
                // UNE seule copie pour les deux passes : deux appels donneraient
                // deux instantanés, que le scan pourrait rendre différents.
                const auto catalogue = bande.scan().types();

                juce::StringArray multi;
                {
                    juce::StringArray seen;

                    for (const auto& d : catalogue)
                        if (! seen.addIfNotAlreadyThere (d.fileOrIdentifier))
                            multi.addIfNotAlreadyThere (d.fileOrIdentifier);
                }

                juce::Array<juce::var> list;

                for (const auto& d : catalogue)
                {
                    if (q.isNotEmpty() && ! d.name.containsIgnoreCase (q)
                        && ! d.manufacturerName.containsIgnoreCase (q))
                        continue;

                    juce::DynamicObject::Ptr p (new juce::DynamicObject());
                    p->setProperty ("name", d.name);
                    p->setProperty ("manufacturer", d.manufacturerName);
                    p->setProperty ("category", d.category);
                    p->setProperty ("path",
                                    multi.contains (d.fileOrIdentifier)
                                      ? d.fileOrIdentifier + "@0x"
                                          + juce::String::toHexString (d.uniqueId)
                                      : d.fileOrIdentifier);
                    list.add (juce::var (p.get()));

                    if (list.size() >= limit)
                        break;
                }

                juce::DynamicObject::Ptr o (new juce::DynamicObject());
                o->setProperty ("total", catalogue.size());
                o->setProperty ("plugins", list);
                return douze::okReply (o);
            }

            if (method == "GET" && path == "/params")
            {
                const int stage = query.fromFirstOccurrenceOf ("stage=", false, false).getIntValue();
                juce::Array<juce::var> list;

                // Sur le MESSAGE THREAD : `rack.params` interroge le plugin sans
                // tenir le verrou du rack, ce qui n'est sûr que si la lecture ne
                // peut pas s'entrelacer avec la destruction d'un étage — laquelle
                // se fait sur ce thread-là.
                juce::Array<douze::Rack::ParamInfo> infos;
                douze::onMessageThread ([&] { infos = rack.params (stage); });

                for (const auto& p : infos)
                {
                    juce::DynamicObject::Ptr o (new juce::DynamicObject());
                    o->setProperty ("name", p.name);
                    o->setProperty ("value", p.value);
                    o->setProperty ("text", p.text);
                    list.add (juce::var (o.get()));
                }

                juce::DynamicObject::Ptr o (new juce::DynamicObject());
                o->setProperty ("stage", stage);
                o->setProperty ("params", list);
                return douze::okReply (o);
            }

            // ---------------- écriture ----------------
            if (method != "POST")
                return douze::errReply (404, "chemin inconnu : " + path);

            const auto in = douze::jsonBody (body);
            const auto num = [&in] (const char* k, int def = 0)
            {
                return in.hasProperty (k) ? (int) in[k] : def;
            };

            bool ok = false;

            if (path == "/chain/add")
            {
                const auto p = in["path"].toString();

                if (p.isEmpty())
                    // CharPointer_UTF8 obligatoire : juce::String(const char*)
                    // lirait ces guillemets en Latin-1 (cf. Rack.cpp / utf8()).
                    return douze::errReply (400, juce::String (juce::CharPointer_UTF8 (
                                                     "champ « path » manquant")));

                douze::onMessageThread ([&]
                {
                    // Instancier via yabridge est légitimement lent (un shell
                    // Waves met ~7 s, un Acustica ~25 s) : le watchdog doit
                    // laisser faire.
                    const douze::watchdog::Phase ph ("chargement d'un plugin", 90000);
                    ok = rack.addStage (p);
                    autoSave.touch();          // la chaîne a changé : on l'écrira
                });
            }
            else if (path == "/chain/remove")
            {
                douze::onMessageThread ([&]
                {
                    // Le teardown d'un plugin Wine peut bloquer aussi (leçon
                    // Delestor : retirer un Acustica dont l'éditeur vit).
                    const douze::watchdog::Phase ph ("retrait d'un étage", 20000);
                    ok = rack.removeStage (num ("index", -1));
                    autoSave.touch();
                });
            }
            else if (path == "/chain/move")
            {
                douze::onMessageThread ([&]
                {
                    ok = rack.moveStage (num ("from", -1), num ("to", -1));
                    autoSave.touch();
                });
            }
            else if (path == "/chain/retry")
            {
                // prepare() ré-instancie tout étage encore vide : c'est le
                // « réessayer » du chip rouge.
                douze::onMessageThread ([&]
                {
                    const douze::watchdog::Phase ph ("chargement d'un plugin", 90000);

                    if (auto* cur = adm.getCurrentAudioDevice())
                        rack.prepare (cur->getCurrentSampleRate(),
                                      cur->getCurrentBufferSizeSamples(),
                                      juce::jmax (2, cur->getActiveOutputChannels().countNumberOfSetBits()));
                    ok = true;
                });
            }
            else if (path == "/bypass")
            {
                // Sur le MESSAGE THREAD : `autoSave.touch()` arme un juce::Timer,
                // et un Timer ne se manipule pas depuis un autre thread. C'était
                // le seul gestionnaire à ne pas passer par là.
                douze::onMessageThread ([&]
                {
                if (in.hasProperty ("index"))
                {
                    rack.setBypass (num ("index"), (bool) in["on"]);
                    ok = true;
                }
                else
                {
                    rack.setBypassAll (in.hasProperty ("on") ? (bool) in["on"] : ! rack.bypassAll());
                    ok = true;
                }

                // Le bypass fait PARTIE du rack (`saveFile` l'écrit) : ne pas le
                // sauver automatiquement laissait le fichier en désaccord avec ce
                // qu'on entend, et un étage bypassé pendant un essai revenait
                // bypassé au démarrage suivant.
                autoSave.touch();
                });
            }
            else if (path == "/editor")
            {
                // Sans écran, rien ne s'ouvrira — et l'essai coûterait la bande
                // (Wine sans pilote graphique ne rend pas la main), puis le
                // plugin serait inscrit « bloquant » à vie. On répond donc NON,
                // avec la raison ET la manœuvre : c'est le démon qu'il faut
                // relancer, pas le plugin qu'il faut changer.
                if (! douze::hasDisplay())
                    return douze::errReply (409, juce::String (juce::CharPointer_UTF8 (
                        "aucun affichage : cette bande a démarré avant ta session "
                        "graphique (DISPLAY / WAYLAND_DISPLAY absents). Relance le "
                        "démon : systemctl --user restart douze")));

                // FIRE-AND-FORGET, volontairement.
                //
                // Le serveur HTTP ne traite qu'une requête à la fois : attendre
                // ici le message thread suffisait à rendre TOUTE l'API muette dès
                // qu'un éditeur Wine ne rendait pas la main (Waves). On lance
                // l'ouverture et on répond tout de suite ; la fenêtre apparaît si
                // elle apparaît, et le watchdog rattrape le cas où le plugin
                // enferme le thread de contrôle.
                const int idx = num ("index");
                juce::MessageManager::callAsync ([&rack, idx]
                {
                    const douze::watchdog::Phase ph ("ouverture de l'éditeur natif", 20000);
                    rack.toggleEditor (idx);
                });
                ok = true;
            }
            else if (path == "/editor/unblock")
            {
                // « Réessayer » : la liste des éditeurs bloquants n'avait aucune
                // porte de sortie, alors qu'elle peut se tromper (un plugin sain
                // condamné parce que le démon tournait sans affichage). Retirer
                // l'inscription est une décision de l'utilisateur, pas du moteur.
                ok = rack.forgetEditorHang (num ("index"));
            }
            else if (path == "/params")
            {
                douze::onMessageThread ([&]
                {
                    ok = rack.setParam (num ("stage"), num ("index"),
                                        (float) (double) in["value"]);
                });
            }
            else if (path == "/preset/save")
            {
                const auto f = in["file"].toString();
                douze::onMessageThread ([&]
                {
                    // getStateInformation peut geler sur un plugin dont l'éditeur
                    // natif est vivant (leçon Delestor / Acustica).
                    const douze::watchdog::Phase ph ("capture de l'état des plugins", 20000);
                    ok = rack.saveFile (f.isEmpty() ? rackFile : juce::File (f));
                });
            }
            else if (path == "/preset/load")
            {
                const auto f = in["file"].toString();
                douze::onMessageThread ([&]
                {
                    // setStateInformation peut prendre très longtemps (mesuré
                    // 13 s sur Softube Flow, qui recharge son moteur).
                    const douze::watchdog::Phase ph ("restauration d'un rack", 90000);
                    ok = rack.loadFile (f.isEmpty() ? rackFile : juce::File (f));

                    if (ok)
                        if (auto* cur = adm.getCurrentAudioDevice())
                            rack.prepare (cur->getCurrentSampleRate(),
                                          cur->getCurrentBufferSizeSamples(),
                                          juce::jmax (2, cur->getActiveOutputChannels().countNumberOfSetBits()));
                });
            }
            else if (path == "/quit")
            {
                juce::MessageManager::callAsync ([]
                    { juce::MessageManager::getInstance()->stopDispatchLoop(); });
                ok = true;
            }
            else
            {
                return douze::errReply (404, "chemin inconnu : " + path);
            }

            juce::DynamicObject::Ptr o (new juce::DynamicObject());
            o->setProperty ("ok", ok);
            return douze::okReply (o);
        });

        if (! api->start())
            api.reset();
    }

    std::cout << "\nDouze FX — bande prête. `help` pour les commandes.\n" << std::endl;
    std::cout << bande.rack().describe() << std::endl;

    // --- console (thread lecteur -> message thread) ---------------------------
    std::atomic<bool> running { true };
    std::thread reader ([&]
    {
        std::string line;

        while (running.load() && std::getline (std::cin, line))
        {
            const juce::String cmd (line);

            juce::MessageManager::callAsync ([&bande, &adm, rackFile, cmd]
            {
                const auto verb = cmd.upToFirstOccurrenceOf (" ", false, false).trim();
                const auto rest = cmd.fromFirstOccurrenceOf (" ", false, false).trim();

                if (verb == "help" || verb.isEmpty())      douze::printHelp();
                else if (verb == "ls")                     std::cout << bande.rack().describe() << std::endl;
                else if (verb == "find")                   douze::printPlugins (bande.scan(), rest);
                else if (verb == "add")                    bande.rack().addStage (rest);
                else if (verb == "rm")                     bande.rack().removeStage (rest.getIntValue());
                else if (verb == "e")                      bande.rack().toggleEditor (rest.getIntValue());
                else if (verb == "b")                      bande.rack().toggleBypass (rest.getIntValue());
                else if (verb == "bypass")                 bande.rack().setBypassAll (! bande.rack().bypassAll());
                else if (verb == "scan")                   rest.isNotEmpty()
                                                             ? bande.scan().scanPaths (juce::StringArray::fromTokens (rest, ":", {}))
                                                             : bande.scan().scanDefaultPaths();
                else if (verb == "save")                   bande.rack().saveFile (rackFile);
                else if (verb == "dev")                    douze::printDevices (adm);
                else if (verb == "q" || verb == "quit")    juce::MessageManager::getInstance()->stopDispatchLoop();
                else                                       std::cout << "? " << verb << std::endl;
            });
        }
    });

    // Watchdog armé ICI, et pas plus tôt : le chargement initial du rack (qui
    // précède) est légitimement long et n'a pas encore de battement à surveiller.
    const douze::watchdog::Heart heart;
    douze::watchdog::start();

    // Reprise des étages tombés. Le budget de watchdog n'est ÉLARGI que quand il y
    // a réellement quelque chose à reprendre : l'élargir à chaque tour laisserait
    // une fenêtre de grâce permanente et le watchdog ne servirait plus à rien.
    const douze::Repriseur repriseur (bande.rack());

    juce::MessageManager::getInstance()->runDispatchLoop();

    // Trace explicite : une bande qui disparaît doit dire par où elle est sortie.
    // Sortir d'ici = arrêt demandé (/quit ou `q`) ; tout autre code de sortie
    // vient d'ailleurs (watchdog = 70, libjack qui appelle exit() = 1).
    std::cout << "[exit] boucle de messages terminée (arrêt demandé)." << std::endl;
    std::cout.flush();

    running.store (false);

    // Garde-fou de SORTIE (leçon Delestor) : détruire un plugin hébergé via Wine
    // (yabridge) peut BLOQUER indéfiniment — observé sur Clear, comme sur
    // Acustica dans Delestor. On laisse au teardown propre un budget, puis on
    // force la sortie : à la fermeture, il n'y a plus rien à préserver.
    std::thread ([]
    {
        std::this_thread::sleep_for (std::chrono::seconds (8));
        std::cout << "[douze-fx] teardown bloqué (>8 s) — sortie forcée." << std::endl;
        std::cout.flush();
        std::_Exit (70);
    }).detach();

    adm.removeAudioCallback (&bande);
    bande.rack().release();
    adm.closeAudioDevice();

    reader.detach();          // getline bloquant : on ne le join pas
    std::cout << "[douze-fx] au revoir." << std::endl;
    std::cout.flush();

    // _Exit : on saute les destructeurs statiques (mêmes hangs potentiels).
    std::_Exit (0);
}
