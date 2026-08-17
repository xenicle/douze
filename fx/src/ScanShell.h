#pragma once

#include <cstdint>
#include <string>
#include <vector>

/*  Énumération FACTORY-ONLY d'un binaire VST3.

    Pourquoi ça existe : un « shell » VST3 est un seul binaire qui expose N
    sous-plugins (WaveShell en annonce plus de 200). Le scan normal de JUCE
    (`findAllTypesForFile`) INSTANCIE chaque classe pour compter ses canaux — et
    deux cents instanciations dans un host Wine font déborder la pile de son
    thread, ce qui se voit de l'extérieur comme un plugin « figé ». Ici on lit la
    factory et rien d'autre : aucun sous-plugin n'est créé.

    Le prix à payer : on ne connaît pas les canaux (laissés à 0, exactement comme
    le chemin rapide `moduleinfo.json` de JUCE). L'hébergement, lui, retrouve la
    classe par nom + identifiants, pas par nombre de canaux.

    Ce fichier est une unité de compilation ISOLÉE : headers VST3 + dl/std,
    AUCUN header JUCE. Les deux mondes définissent des macros qui se marchent
    dessus, et l'include du SDK n'est ajouté que pour ce fichier (cf. CMakeLists).
*/

namespace douze
{

struct ShellClass
{
    std::string name, vendor, version, subCategories;
    std::int32_t uniqueId = 0;        // hash JUCE du TUID normalisé
    std::int32_t deprecatedUid = 0;   // hash JUCE des 16 octets bruts
};

struct ShellScanResult
{
    std::string factoryVendor;
    std::vector<ShellClass> classes;
};

/** Énumère les classes audio d'un binaire VST3 sans en instancier aucune.
    Renvoie false et remplit `err` si le module ne s'ouvre pas. */
bool scanShellEnumerate (const std::string& bundleOrFile,
                         ShellScanResult& out,
                         std::string& err);

}   // namespace douze
