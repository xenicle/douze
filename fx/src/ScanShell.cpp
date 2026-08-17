// Voir ScanShell.h. Unité isolée : headers VST3 + dl/std, aucun header JUCE.
#include "ScanShell.h"

#include <cstring>
#include <dlfcn.h>
#include <sys/stat.h>
#include <sys/utsname.h>

#include "pluginterfaces/base/ipluginbase.h"

using namespace Steinberg;

namespace douze
{

// --- identité : les deux hashes de JUCE, à reproduire À L'IDENTIQUE ----------
//
// C'est LE point délicat de tout ce fichier. L'hébergement retrouve une classe
// par son nom ET ses identifiants (`uniqueId` / `deprecatedUid`) ; si nos hashes
// divergeaient de ceux que JUCE calcule lors d'un scan normal, le catalogue
// paraîtrait correct mais aucun sous-plugin ne se chargerait — une panne qui ne
// se voit qu'à l'usage, et qui ne dit pas son nom.
//
// JUCE fait `value = value * 31 + item` sur :
//   - `deprecatedUid` : les 16 octets BRUTS du TUID, lus en char SIGNÉS ;
//   - `uniqueId`      : le TUID « normalisé », soit quatre entiers 32 bits lus
//                       en BIG-ENDIAN (sur Linux, COM_COMPATIBLE vaut 0).

static std::int32_t hashRawTuid (const char* cid)
{
    std::uint32_t value = 0;

    for (int i = 0; i < 16; ++i)
        // `char` est signé ici : l'extension de signe fait partie du calcul de
        // JUCE, la retirer donnerait un autre hash.
        value = (value * 31u) + static_cast<std::uint32_t> (cid[i]);

    return static_cast<std::int32_t> (value);
}

static std::int32_t hashNormalisedTuid (const char* cid)
{
    const auto* d = reinterpret_cast<const std::uint8_t*> (cid);
    std::uint32_t value = 0;

    for (int g = 0; g < 4; ++g)
    {
        const std::uint32_t groupe = (static_cast<std::uint32_t> (d[g * 4 + 0]) << 24)
                                   | (static_cast<std::uint32_t> (d[g * 4 + 1]) << 16)
                                   | (static_cast<std::uint32_t> (d[g * 4 + 2]) << 8)
                                   |  static_cast<std::uint32_t> (d[g * 4 + 3]);
        value = (value * 31u) + groupe;
    }

    return static_cast<std::int32_t> (value);
}

// Les champs `char[]` du SDK ne sont pas garantis terminés par un NUL : on borne
// à la capacité déclarée avant de construire la chaîne, sinon on lit à côté.
static std::string champ (const char* s, std::size_t cap)
{
    std::size_t n = 0;
    while (n < cap && s[n] != '\0')
        ++n;

    std::string out (s, n);

    while (! out.empty() && (out.back() == ' ' || out.back() == '\t'))
        out.pop_back();

    std::size_t debut = 0;
    while (debut < out.size() && (out[debut] == ' ' || out[debut] == '\t'))
        ++debut;

    return out.substr (debut);
}

// Un VST3 Linux est un BUNDLE (un répertoire) : le binaire vit dans
// Contents/<machine>-linux/<nom-sans-extension>.so — même règle que JUCE. Un
// chemin qui désigne déjà un fichier est pris tel quel.
static std::string resoudreModule (const std::string& chemin)
{
    struct stat st {};

    if (::stat (chemin.c_str(), &st) != 0 || ! S_ISDIR (st.st_mode))
        return chemin;

    struct utsname un {};
    const std::string machine = (::uname (&un) == 0) ? un.machine : "x86_64";

    const auto slash = chemin.find_last_of ('/');
    std::string nom = (slash == std::string::npos) ? chemin : chemin.substr (slash + 1);

    const auto point = nom.find_last_of ('.');
    if (point != std::string::npos)
        nom = nom.substr (0, point);

    return chemin + "/Contents/" + machine + "-linux/" + nom + ".so";
}

bool scanShellEnumerate (const std::string& bundleOrFile,
                         ShellScanResult& out,
                         std::string& err)
{
    const std::string so = resoudreModule (bundleOrFile);

    void* module = ::dlopen (so.c_str(), RTLD_NOW | RTLD_LOCAL);

    if (module == nullptr)
    {
        const char* e = ::dlerror();
        err = "dlopen : " + std::string (e != nullptr ? e : "échec");
        return false;
    }

    // Certains modules exigent ModuleEntry avant toute chose. Son absence n'est
    // pas une erreur : beaucoup de plugins ne l'exportent pas.
    if (auto* entry = reinterpret_cast<bool (*) (void*)> (::dlsym (module, "ModuleEntry")))
        entry (module);

    auto* getFactory = reinterpret_cast<IPluginFactory* (*) ()> (
                           ::dlsym (module, "GetPluginFactory"));

    if (getFactory == nullptr)
    {
        err = "GetPluginFactory absent (pas un VST3 ?)";
        return false;
    }

    IPluginFactory* factory = getFactory();

    if (factory == nullptr)
    {
        err = "GetPluginFactory a rendu nullptr";
        return false;
    }

    PFactoryInfo fi {};
    if (factory->getFactoryInfo (&fi) == kResultOk)
        out.factoryVendor = champ (fi.vendor, sizeof (fi.vendor));

    // getClassInfo2 porte le vendeur, la version et les sous-catégories ;
    // getClassInfo (v1) ne donne que le nom et la catégorie. On prend le plus
    // riche quand il est là.
    IPluginFactory2* f2 = nullptr;
    factory->queryInterface (IPluginFactory2::iid, reinterpret_cast<void**> (&f2));

    const std::int32_t total = factory->countClasses();

    for (std::int32_t i = 0; i < total; ++i)
    {
        PClassInfo info {};
        if (factory->getClassInfo (i, &info) != kResultOk)
            continue;

        // Seules les classes AUDIO nous intéressent : une factory expose aussi
        // des contrôleurs d'édition, qui ne sont pas des plugins hébergeables.
        if (champ (info.category, sizeof (info.category)) != "Audio Module Class")
            continue;

        ShellClass c;
        c.name = champ (info.name, sizeof (info.name));
        c.uniqueId = hashNormalisedTuid (info.cid);
        c.deprecatedUid = hashRawTuid (info.cid);

        if (f2 != nullptr)
        {
            PClassInfo2 info2 {};
            if (f2->getClassInfo2 (i, &info2) == kResultOk)
            {
                c.vendor = champ (info2.vendor, sizeof (info2.vendor));
                c.version = champ (info2.version, sizeof (info2.version));
                c.subCategories = champ (info2.subCategories, sizeof (info2.subCategories));
            }
        }

        out.classes.push_back (std::move (c));
    }

    if (f2 != nullptr)
        f2->release();

    // PAS de ModuleExit ni de dlclose : ce process est JETABLE et s'arrête juste
    // après. Fermer proprement un module Wine peut bloquer (leçon des teardowns
    // à plus de 30 s), et bloquer ici transformerait un scan réussi en « figé ».
    return true;
}

}   // namespace douze
