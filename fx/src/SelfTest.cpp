#include "SelfTest.h"

#include <iostream>
#include <string>

#include "Rack.h"

namespace douze
{

//==============================================================================
namespace
{

int gEchecs = 0;
int gTotal = 0;

void verifie (bool condition, const std::string& quoi)
{
    ++gTotal;

    if (condition)
    {
        std::cout << "  ok   " << quoi << std::endl;
    }
    else
    {
        ++gEchecs;
        std::cout << "  ECHEC " << quoi << std::endl;
    }
}

/** Buffer de test : une rampe non nulle sur tous les canaux.

    Pas du silence : un test qui passe sur du silence ne distingue pas « la chaîne
    a laissé passer » de « la chaîne a tout mangé ». */
juce::AudioBuffer<float> signal (int canaux, int frames, float amplitude = 0.5f)
{
    juce::AudioBuffer<float> b (canaux, frames);

    for (int ch = 0; ch < canaux; ++ch)
        for (int i = 0; i < frames; ++i)
            b.setSample (ch, i, amplitude * std::sin (juce::MathConstants<float>::twoPi
                                                        * 440.0f * (float) i / 48000.0f));

    return b;
}

float creteDe (const juce::AudioBuffer<float>& b)
{
    float p = 0.0f;

    for (int ch = 0; ch < b.getNumChannels(); ++ch)
        p = juce::jmax (p, b.getMagnitude (ch, 0, b.getNumSamples()));

    return p;
}

juce::File dossierTemporaire()
{
    auto d = juce::File::getSpecialLocation (juce::File::tempDirectory)
               .getChildFile ("douze-fx-selftest");
    d.createDirectory();
    return d;
}

//==============================================================================
/** Une chaîne VIDE laisse passer le signal intact.

    C'est le test le plus bête et le plus utile : il vérifie d'un coup le
    recopiage entrée → buffer de travail → sortie, là où une erreur de canal ou de
    taille passerait inaperçue derrière un plugin. */
void testPassthrough (PluginScan& scan)
{
    std::cout << "[test] chaîne vide = passthrough" << std::endl;

    Rack rack (scan);
    rack.prepare (48000.0, 512, 2);

    auto b = signal (2, 512);
    const auto avant = creteDe (b);
    juce::MidiBuffer midi;
    rack.process (b, midi);

    verifie (juce::approximatelyEqual (creteDe (b), avant),
             "le signal ressort inchangé (crête "
               + juce::String (avant, 4).toStdString() + ")");
    verifie (rack.getNumStages() == 0, "aucun étage");
}

/** Le bypass GLOBAL et le bypass PAR ÉTAGE n'altèrent pas le signal. */
void testBypass (PluginScan& scan)
{
    std::cout << "[test] bypass" << std::endl;

    Rack rack (scan);
    rack.prepare (48000.0, 512, 2);

    rack.setBypassAll (true);
    verifie (rack.bypassAll(), "le bypass global se retient");

    auto b = signal (2, 512);
    const auto avant = creteDe (b);
    juce::MidiBuffer midi;
    rack.process (b, midi);
    verifie (juce::approximatelyEqual (creteDe (b), avant),
             "bypass global : signal intact");

    rack.setBypassAll (false);
    verifie (! rack.bypassAll(), "et se relâche");
}

/** Un étage INTROUVABLE ne fait pas tomber la bande : il est marqué et sauté.

    C'est la décision produit du projet ; sans test, une exception mal placée la
    reprenait en silence (déjà arrivé : un `instantiate` non protégé faisait
    avorter tout le process). */
void testEtageIntrouvable (PluginScan& scan)
{
    std::cout << "[test] plugin introuvable = étage en erreur, chaîne vivante" << std::endl;

    Rack rack (scan);
    rack.prepare (48000.0, 512, 2);

    const bool ok = rack.addStage ("/chemin/qui/nexiste/pas.vst3");
    verifie (! ok, "addStage signale l'échec");
    verifie (rack.getNumStages() == 1, "l'étage est GARDÉ (visible, réessayable)");

    const auto infos = rack.stageInfo();
    verifie (infos.size() == 1 && ! infos[0].loaded, "il est marqué non chargé");
    verifie (infos.size() == 1 && infos[0].error.isNotEmpty(), "avec une raison");

    auto b = signal (2, 512);
    const auto avant = creteDe (b);
    juce::MidiBuffer midi;
    rack.process (b, midi);
    verifie (juce::approximatelyEqual (creteDe (b), avant),
             "le signal traverse quand même");
}

/** Ajouter, déplacer, retirer : l'ordre est ce qu'on a demandé.

    L'ordre s'entend (un débruiteur après un compresseur travaille sur un
    plancher de bruit remonté), donc une inversion silencieuse est un vrai bug. */
void testOrdreDeChaine (PluginScan& scan)
{
    std::cout << "[test] ordre de la chaîne" << std::endl;

    Rack rack (scan);
    rack.prepare (48000.0, 512, 2);

    for (const char* p : { "/a.vst3", "/b.vst3", "/c.vst3" })
        rack.addStage (p);

    verifie (rack.getNumStages() == 3, "trois étages");

    auto chemins = [&rack]
    {
        juce::String s;

        for (const auto& i : rack.stageInfo())
            s << juce::File (i.path).getFileName() << " ";

        return s.trim();
    };

    verifie (chemins() == "a.vst3 b.vst3 c.vst3", "ordre initial : " + chemins().toStdString());

    verifie (rack.moveStage (0, 2), "déplacement accepté");
    verifie (chemins() == "b.vst3 c.vst3 a.vst3", "après déplacement : " + chemins().toStdString());

    verifie (! rack.moveStage (0, 9), "déplacement hors bornes refusé");
    verifie (! rack.moveStage (-1, 0), "index négatif refusé");

    verifie (rack.removeStage (1), "retrait accepté");
    verifie (chemins() == "b.vst3 a.vst3", "après retrait : " + chemins().toStdString());
    verifie (! rack.removeStage (7), "retrait hors bornes refusé");
}

/** Le rack fait un aller-retour fidèle sur le disque.

    Le nom et le bypass DOIVENT survivre : le nom parce que Douze l'affiche quand
    la bande ne répond pas (sinon on voit un chemin de shell VST3), le bypass
    parce qu'il fait partie du réglage. */
void testRackAllerRetour (PluginScan& scan)
{
    std::cout << "[test] rack : écriture puis relecture" << std::endl;

    const auto fichier = dossierTemporaire().getChildFile ("rack.json");
    fichier.deleteFile();

    {
        Rack rack (scan);
        rack.prepare (48000.0, 512, 2);
        rack.addStage ("/un.vst3");
        rack.addStage ("/deux.vst3");
        rack.setBypass (1, true);
        rack.setBypassAll (true);
        verifie (rack.saveFile (fichier), "écrit");
    }

    verifie (fichier.existsAsFile() && fichier.getSize() > 0, "le fichier existe");

    {
        Rack rack (scan);
        verifie (rack.loadFile (fichier), "relu");
        verifie (rack.getNumStages() == 2, "deux étages retrouvés");
        verifie (rack.bypassAll(), "bypass global retrouvé");

        const auto infos = rack.stageInfo();
        verifie (infos.size() == 2 && ! infos[0].bypass && infos[1].bypass,
                 "bypass PAR ÉTAGE retrouvé (le 2e seulement)");
        verifie (infos.size() == 2
                   && juce::File (infos[0].path).getFileName() == "un.vst3",
                 "ordre conservé");
    }

    // Un rack ABSENT n'est pas une erreur fatale : on démarre vide.
    Rack vide (scan);
    verifie (! vide.loadFile (dossierTemporaire().getChildFile ("nexistepas.json")),
             "un rack absent est refusé proprement");
    verifie (vide.getNumStages() == 0, "et laisse la chaîne vide");

    fichier.deleteFile();
}

/** Changer de format re-prépare la chaîne (et ne casse pas le traitement).

    Le graphe PipeWire est global : passer de 44,1 à 48 kHz arrive sans qu'on
    touche à la bande. Les plugins déjà chargés restaient préparés pour l'ancienne
    fréquence — filtres décalés, temps de compresseur faux. */
void testChangementDeFormat (PluginScan& scan)
{
    std::cout << "[test] changement de fréquence et de bloc" << std::endl;

    Rack rack (scan);
    rack.prepare (44100.0, 1024, 2);

    auto b1 = signal (2, 1024);
    const auto avant1 = creteDe (b1);
    juce::MidiBuffer midi;
    rack.process (b1, midi);
    verifie (juce::approximatelyEqual (creteDe (b1), avant1), "44,1 kHz / 1024 : OK");

    rack.prepare (48000.0, 256, 2);

    auto b2 = signal (2, 256);
    const auto avant2 = creteDe (b2);
    rack.process (b2, midi);
    verifie (juce::approximatelyEqual (creteDe (b2), avant2), "48 kHz / 256 : OK");

    // Bloc PLUS GRAND que celui préparé : le rack doit refuser de traiter plutôt
    // que d'écrire hors de son buffer de travail.
    auto b3 = signal (2, 4096);
    const auto avant3 = creteDe (b3);
    rack.process (b3, midi);
    verifie (juce::approximatelyEqual (creteDe (b3), avant3),
             "bloc surdimensionné : passe sec au lieu de déborder");
}

/** Le catalogue résout un étage « chemin@0xUID » comme un chemin nu.

    Un shell VST3 (WaveShell : 200+ plugins Waves dans un fichier) se désigne par
    son UID ; confondre les deux chargeait aveuglément le premier sous-plugin. */
void testResolutionEtage (PluginScan& scan)
{
    std::cout << "[test] désignation d'un étage" << std::endl;

    const juce::String faux = "/pas/la/Shell.vst3@0x1a2b3c4d";
    juce::PluginDescription d;
    verifie (! scan.resolveStage (faux, d), "un étage inconnu n'est pas résolu");

    // Le nom de repli doit rester lisible : c'est ce que la GUI affiche.
    const auto nom = scan.displayName (faux);
    verifie (nom == "Shell", "nom de repli sans le suffixe UID : « " + nom.toStdString() + " »");
    verifie (scan.displayName ("/pas/la/Simple.vst3") == "Simple",
             "et sans l'extension pour un chemin nu");
}

} // namespace

//==============================================================================
int runSelfTests (const juce::String& filtre)
{
    gEchecs = gTotal = 0;

    // Un seul scan pour toute la batterie : c'est lent (il lit le cache) et rien
    // ici ne dépend de son contenu.
    PluginScan scan;

    struct Cas { const char* nom; void (*fn) (PluginScan&); };

    const Cas cas[] = {
        { "passthrough",  testPassthrough },
        { "bypass",       testBypass },
        { "introuvable",  testEtageIntrouvable },
        { "ordre",        testOrdreDeChaine },
        { "rack",         testRackAllerRetour },
        { "format",       testChangementDeFormat },
        { "resolution",   testResolutionEtage },
    };

    for (const auto& c : cas)
        if (filtre.isEmpty() || juce::String (c.nom).contains (filtre))
            c.fn (scan);

    std::cout << "\n=== " << (gTotal - gEchecs) << "/" << gTotal
              << " vérification(s) OK" << (gEchecs > 0 ? " — ÉCHEC" : " — tout passe")
              << " ===" << std::endl;
    return gEchecs;
}

} // namespace douze
