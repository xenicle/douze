// Douze FX — autotests HORS DEVICE AUDIO.
//
// Pourquoi un mode dédié : tout ce qui compte ici (une chaîne qui survit à un
// plugin mort, un rack qui se relit, une fréquence qui change) se vérifiait
// jusqu'à présent à la main, sur la bande micro de l'utilisateur, en coupant son
// son. Ce mode fabrique un signal, le fait traverser un vrai `Rack`, et compare
// ce qui sort — sans JACK, sans PipeWire, sans carte.
//
// Ce qu'il ne remplace PAS : les tests en vrai DAW / vrai graphe (latence,
// underruns, éditeurs natifs). Il attrape les régressions de LOGIQUE, qui sont
// exactement celles qu'on a introduites en corrigeant les autres.
#pragma once

#include <juce_core/juce_core.h>

namespace douze
{

/** Lance la batterie. `filtre` ne garde que les tests dont le nom le contient.
    Renvoie 0 si tout passe, le nombre d'échecs sinon. */
int runSelfTests (const juce::String& filtre);

} // namespace douze
