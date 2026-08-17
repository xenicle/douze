#include "HttpApi.h"

#include <iostream>

namespace douze
{

HttpApi::HttpApi (int port, Handler handler)
    : juce::Thread ("douze-fx-api"), port_ (port), handler_ (std::move (handler)) {}

HttpApi::~HttpApi()
{
    signalThreadShouldExit();

    if (listener_ != nullptr)
        listener_->close();          // débloque le waitForNextConnection

    stopThread (2000);
}

bool HttpApi::start()
{
    listener_ = std::make_unique<juce::StreamingSocket>();

    // 127.0.0.1 explicite : jamais exposé au réseau.
    if (! listener_->createListener (port_, "127.0.0.1"))
    {
        std::cout << "[api] port " << port_ << " indisponible." << std::endl;
        listener_.reset();
        return false;
    }

    startThread();
    std::cout << "[api] http://127.0.0.1:" << port_ << std::endl;
    return true;
}

void HttpApi::run()
{
    while (! threadShouldExit())
    {
        std::unique_ptr<juce::StreamingSocket> client (listener_->waitForNextConnection());

        if (client == nullptr)
            continue;                // fermeture, ou erreur transitoire

        serveOne (*client);
    }
}

//==============================================================================
/** Lit jusqu'à la fin des en-têtes, puis le corps annoncé par Content-Length.

    Une requête par connexion (on renvoie `Connection: close`) : c'est amplement
    suffisant pour une télécommande, et ça évite toute gestion de keep-alive. */
void HttpApi::serveOne (juce::StreamingSocket& client)
{
    juce::String raw;
    char buf[2048];

    // --- en-têtes -----------------------------------------------------------
    while (! raw.contains ("\r\n\r\n") && raw.length() < 64 * 1024)
    {
        if (client.waitUntilReady (true, 3000) != 1)
            return;

        const int n = client.read (buf, sizeof (buf), false);

        if (n <= 0)
            return;

        raw += juce::String::fromUTF8 (buf, n);
    }

    const auto head = raw.upToFirstOccurrenceOf ("\r\n\r\n", false, false);
    juce::String body = raw.fromFirstOccurrenceOf ("\r\n\r\n", false, false);

    // --- corps (Content-Length) ---------------------------------------------
    int contentLength = 0;

    for (const auto& line : juce::StringArray::fromLines (head))
        if (line.startsWithIgnoreCase ("content-length:"))
            contentLength = line.fromFirstOccurrenceOf (":", false, false).trim().getIntValue();

    while (body.getNumBytesAsUTF8() < (size_t) contentLength)
    {
        if (client.waitUntilReady (true, 3000) != 1)
            break;

        const int n = client.read (buf, sizeof (buf), false);

        if (n <= 0)
            break;

        body += juce::String::fromUTF8 (buf, n);
    }

    // --- ligne de requête ----------------------------------------------------
    const auto requestLine = juce::StringArray::fromLines (head)[0];
    const auto method = requestLine.upToFirstOccurrenceOf (" ", false, false).trim();
    const auto target = requestLine.fromFirstOccurrenceOf (" ", false, false)
                                   .upToFirstOccurrenceOf (" ", false, false).trim();
    const auto path  = target.upToFirstOccurrenceOf ("?", false, false);
    const auto query = target.fromFirstOccurrenceOf ("?", false, false);

    Reply reply;

    if (method == "OPTIONS")                       // pré-vol CORS (GUI web locale)
        reply = { 204, {}, "text/plain" };
    else if (handler_ != nullptr)
        reply = handler_ (method, path, query, body);
    else
        reply = { 500, R"({"error":"pas de gestionnaire"})" };

    const auto payload = reply.body.toUTF8();
    juce::String out;
    out << "HTTP/1.1 " << reply.status << (reply.status == 200 ? " OK" : " ") << "\r\n"
        << "Content-Type: " << reply.contentType << "\r\n"
        << "Content-Length: " << (int) payload.sizeInBytes() - 1 << "\r\n"
        // PAS de CORS, volontairement.
        //
        // On avait mis `Access-Control-Allow-Origin: *` en croyant que la GUI
        // Douze appellerait cette API directement. Elle ne le fait pas : elle
        // passe par `douze.py` (`cmd: "api"`), qui relaie côté serveur. Le
        // joker ne servait donc à rien — et il autorisait N'IMPORTE QUEL site
        // web visité à parler à 127.0.0.1:1213, c'est-à-dire à retirer un
        // plugin ou à arrêter la bande micro pendant une conversation.
        << "Connection: close\r\n\r\n";

    const auto header = out.toUTF8();
    client.write (header.getAddress(), (int) header.sizeInBytes() - 1);

    if (payload.sizeInBytes() > 1)
        client.write (payload.getAddress(), (int) payload.sizeInBytes() - 1);

    client.close();
}

} // namespace douze
