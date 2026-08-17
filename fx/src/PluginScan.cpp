#include "PluginScan.h"

namespace douze
{

//==============================================================================
// Un étage de rack s'écrit « /chemin/Plug.vst3 » ou, pour un shell qui contient
// plusieurs sous-plugins (WaveShell), « /chemin/Shell.vst3@0x1a2b3c4d ».
static juce::String stageFileOf (const juce::String& stagePath)
{
    const auto at = stagePath.lastIndexOf ("@0x");
    return at > 0 ? stagePath.substring (0, at) : stagePath;
}

static bool stageUidOf (const juce::String& stagePath, int& uid)
{
    const auto at = stagePath.lastIndexOf ("@0x");
    if (at <= 0)
        return false;

    uid = (int) stagePath.substring (at + 3).getHexValue64();
    return true;
}

//==============================================================================
juce::File PluginScan::cacheFile()
{
    return juce::File::getSpecialLocation (juce::File::userHomeDirectory)
             .getChildFile (".cache/douze-fx/plugins.xml");
}

juce::File PluginScan::delestorCacheFile()
{
    return juce::File::getSpecialLocation (juce::File::userHomeDirectory)
             .getChildFile (".cache/delestor/plugins.xml");
}

PluginScan::PluginScan()
{
    // NB : `AudioPluginFormatManager::addDefaultFormats()` est SUPPRIMÉ (=delete)
    // dans le module headless. Version AVEC support des éditeurs (on en a besoin
    // pour ouvrir les GUI natives) ; la variante sans UI est
    // addHeadlessDefaultFormatsToManager().
    juce::addDefaultFormatsToManager (formats_);
    loadCache();
}

juce::Array<juce::PluginDescription> PluginScan::types() const
{
    const juce::ScopedLock sl (lock_);
    return known_.getTypes();
}

int PluginScan::numTypes() const
{
    const juce::ScopedLock sl (lock_);
    return known_.getNumTypes();
}

void PluginScan::loadCache()
{
    const juce::ScopedLock sl (lock_);

    // Notre cache d'abord ; à défaut celui de Delestor (déjà peuplé) — on ne
    // l'écrit JAMAIS en retour, Delestor en reste propriétaire.
    for (auto f : { cacheFile(), delestorCacheFile() })
    {
        if (! f.existsAsFile())
            continue;

        if (auto xml = juce::XmlDocument::parse (f))
        {
            known_.recreateFromXml (*xml);

            if (f == cacheFile())
                cacheStamp_ = f.getLastModificationTime().toMilliseconds();

            std::cout << "[scan] cache : " << known_.getNumTypes() << " plugin(s) depuis "
                      << f.getFullPathName() << std::endl;
            return;
        }
    }

    std::cout << "[scan] aucun cache — utilise `scan` pour peupler le catalogue." << std::endl;
}

bool PluginScan::reloadCacheIfChanged()
{
    const juce::ScopedLock sl (lock_);
    auto f = cacheFile();

    if (! f.existsAsFile())
        return false;

    const auto stamp = f.getLastModificationTime().toMilliseconds();

    if (stamp == cacheStamp_)
        return false;

    if (auto xml = juce::XmlDocument::parse (f))
    {
        const int before = known_.getNumTypes();
        known_.recreateFromXml (*xml);
        cacheStamp_ = stamp;
        std::cout << "[scan] cache rechargé : " << known_.getNumTypes()
                  << " plugin(s) (" << (known_.getNumTypes() - before) << " de plus)"
                  << std::endl;
        return true;
    }

    return false;
}

bool PluginScan::saveCache() const
{
    const juce::ScopedLock sl (lock_);
    auto f = cacheFile();
    f.getParentDirectory().createDirectory();

    if (auto xml = known_.createXml())
        return xml->writeTo (f, {});

    return false;
}

//==============================================================================
int PluginScan::scanPaths (const juce::StringArray& paths)
{
    // Le scanner de JUCE écrit directement dans `known_` : on garde le verrou sur
    // toute la passe. Ce chemin n'est utilisé qu'en ligne de commande (`--scan`),
    // jamais pendant qu'une bande joue.
    const juce::ScopedLock sl (lock_);
    const int before = known_.getNumTypes();

    for (auto* format : formats_.getFormats())
    {
        for (const auto& p : paths)
        {
            juce::PluginDirectoryScanner scanner (known_, *format,
                                                  juce::FileSearchPath (p),
                                                  true,   // récursif
                                                  {},     // pas de dead-man's pedal ici
                                                  false); // pas de scan async
            juce::String name;

            while (scanner.scanNextFile (true, name))
                std::cout << "[scan] " << name << std::endl;
        }
    }

    if (auto xml = known_.createXml())
    {
        auto f = cacheFile();
        f.getParentDirectory().createDirectory();
        xml->writeTo (f, {});
    }

    return known_.getNumTypes() - before;
}

int PluginScan::scanDefaultPaths()
{
    juce::StringArray paths;

    for (auto* format : formats_.getFormats())
    {
        const auto locations = format->getDefaultLocationsToSearch();

        for (int i = 0; i < locations.getNumPaths(); ++i)
            paths.addIfNotAlreadyThere (locations[i].getFullPathName());
    }

    const auto env = juce::SystemStats::getEnvironmentVariable ("VST3_PATH", {});

    if (env.isNotEmpty())
        for (const auto& p : juce::StringArray::fromTokens (env, ":", {}))
            paths.addIfNotAlreadyThere (p);

    return scanPaths (paths);
}

//==============================================================================
bool PluginScan::resolveStage (const juce::String& stagePath, juce::PluginDescription& out)
{
    const auto file = stageFileOf (stagePath);
    int uid = 0;
    const bool hasUid = stageUidOf (stagePath, uid);

    // 1) le cache : match par fichier (+ uid si l'étage en précise un). Sur une
    //    COPIE : `known_` peut être reconstruite par le thread HTTP au même moment.
    for (const auto& d : types())
    {
        if (d.fileOrIdentifier != file)
            continue;

        if (! hasUid || d.uniqueId == uid || d.deprecatedUid == uid)
        {
            out = d;
            return true;
        }
    }

    // 2) pas dans le cache : on demande au format (scan ciblé du seul fichier)
    for (auto* format : formats_.getFormats())
    {
        if (! format->fileMightContainThisPluginType (file))
            continue;

        juce::OwnedArray<juce::PluginDescription> descs;
        format->findAllTypesForFile (descs, file);

        for (auto* d : descs)
        {
            if (! hasUid || d->uniqueId == uid || d->deprecatedUid == uid)
            {
                out = *d;
                {
                    const juce::ScopedLock sl (lock_);
                    known_.addType (*d);
                }
                saveCache();
                return true;
            }
        }
    }

    return false;
}

juce::String PluginScan::displayName (const juce::String& stagePath)
{
    juce::PluginDescription d;

    if (resolveStage (stagePath, d))
        return d.name;

    return juce::File (stageFileOf (stagePath)).getFileNameWithoutExtension();
}

} // namespace douze
