// Douze FX — serveur HTTP local minimal (l'API de contrôle d'UNE bande).
//
// Pourquoi maison plutôt qu'une lib : JUCE n'expose pas de serveur HTTP, et on
// ne veut pas d'une dépendance de plus pour ~200 lignes. Le besoin est étroit :
// HTTP/1.1, une requête par connexion, corps JSON, réponses JSON.
//
// SÉCURITÉ : on écoute UNIQUEMENT sur 127.0.0.1. C'est une télécommande locale
// (le démon Douze tourne sur la même machine), jamais un service réseau.
#pragma once

#include <juce_core/juce_core.h>

#include <functional>

namespace douze
{

class HttpApi final : private juce::Thread
{
public:
    /** Réponse d'un gestionnaire : code HTTP + corps (JSON par défaut). */
    struct Reply
    {
        int status = 200;
        juce::String body { "{}" };
        juce::String contentType { "application/json; charset=utf-8" };
    };

    /** method = "GET"/"POST", path = "/state", query = "q=comp&limit=20", body = JSON brut. */
    using Handler = std::function<Reply (const juce::String& method,
                                         const juce::String& path,
                                         const juce::String& query,
                                         const juce::String& body)>;

    HttpApi (int port, Handler handler);
    ~HttpApi() override;

    /** Démarre l'écoute. Renvoie false si le port est déjà pris. */
    bool start();

    int getPort() const noexcept { return port_; }

private:
    void run() override;
    void serveOne (juce::StreamingSocket& client);

    int port_;
    Handler handler_;
    std::unique_ptr<juce::StreamingSocket> listener_;
};

} // namespace douze
