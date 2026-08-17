#!/usr/bin/env python3
"""Douze — application de bureau : une fenêtre + une icône de notification.

Ce n'est **qu'un client**. Le démon (`douze.service`) garde la SSL 12 et les
bandes FX, qui sont ses enfants : si cette application le possédait, fermer la
fenêtre couperait le micro en pleine conversation. Elle se contente donc de
l'afficher, de le démarrer s'il dort, et de le piloter par HTTP.

  - la croix RÉDUIT dans la barre (l'icône reste, un clic ramène la fenêtre) ;
  - « Quitter » ferme l'application, jamais le démon ;
  - l'icône change d'état quand le démon ne répond plus.

Lancement : `tools/douze-app.sh` (qui pose l'environnement GTK/WebKit).
"""

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")

# L'indicateur est optionnel : sans hôte de barre (ou sans la lib), l'application
# doit rester utilisable en simple fenêtre plutôt que refuser de démarrer.
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except (ValueError, ImportError):
    AppIndicator = None

from gi.repository import Gdk, Gio, GLib, Gtk, WebKit2

# Sous Wayland, GTK tire l'app-id de la fenêtre du `prgname`, qui vaudrait sinon
# « douze-app.py » : le compositeur ne saurait pas rattacher la fenêtre au
# lanceur (`douze.desktop`) et l'afficherait sans son icône. À poser AVANT toute
# création de fenêtre.
GLib.set_prgname("douze")

URL = os.environ.get("DOUZE_URL", "http://127.0.0.1:1212")
UNIT = os.environ.get("DOUZE_UNIT", "douze.service")
APP_ID = "audio.xeni.douze"
POLL_S = 2
CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME",
                                    os.path.expanduser("~/.config")),
                      "douze", "app.json")


# --------------------------------------------------------------------- réseau
def _get(path, timeout=1.5):
    """GET JSON, ou None si le démon ne répond pas. Jamais appelé depuis la
    boucle GTK : 1,5 s d'attente y figerait la fenêtre."""
    try:
        with urllib.request.urlopen(URL + path, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _post_fx(body, timeout=90):
    try:
        req = urllib.request.Request(
            URL + "/fx", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _systemctl(*args):
    """systemctl --user, sans attendre : démarrer le démon prend des secondes et
    la barre ne doit pas se figer pendant."""
    subprocess.Popen(["systemctl", "--user", *args],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --------------------------------------------------------------------- fenêtre
class Fenetre(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Douze")
        self.set_default_size(*self._taille_sauvee())
        self.set_icon_name("douze")

        self.vue = WebKit2.WebView()
        self.vue.load_uri(URL)
        self.add(self.vue)
        self.vue.show()

        # La croix masque au lieu de détruire : recréer une WebView à chaque
        # ouverture rechargerait tout le graphe et perdrait le défilement.
        self.connect("delete-event", self._sur_fermeture)
        # F5 / Ctrl-R : une fenêtre sans barre d'adresse n'a aucun autre moyen de
        # recharger, ce qui est pénible dès qu'on retouche la page.
        self.connect("key-press-event", self._sur_touche)

    def _sur_touche(self, _w, ev):
        ctrl = bool(ev.state & Gdk.ModifierType.CONTROL_MASK)

        if ev.keyval == Gdk.KEY_F5 or (ctrl and ev.keyval in (Gdk.KEY_r, Gdk.KEY_R)):
            self.vue.reload_bypass_cache()
            return True

        return False

    def _taille_sauvee(self):
        try:
            with open(CONFIG) as f:
                d = json.load(f)
            return int(d.get("w", 1280)), int(d.get("h", 860))
        except (OSError, ValueError, TypeError):
            return 1280, 860

    def _sur_fermeture(self, *_):
        w, h = self.get_size()
        try:
            os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
            with open(CONFIG, "w") as f:
                json.dump({"w": w, "h": h}, f)
        except OSError:
            pass                      # une géométrie non sauvée n'est pas grave
        self.hide()
        return True                   # True = on NE détruit pas la fenêtre

    def montrer(self):
        self.show()
        self.present()

        # Le démon a pu démarrer entre-temps : une page en erreur doit se
        # recharger toute seule, sinon l'utilisateur voit un message de WebKit.
        if not (self.vue.get_uri() or "").startswith(URL) or self.vue.is_loading():
            return
        if self.vue.get_estimated_load_progress() == 0:
            self.vue.load_uri(URL)

    def recharger(self):
        self.vue.load_uri(URL)


# ------------------------------------------------------------------ application
class Douze(Gtk.Application):
    def __init__(self):
        # Pas de NON_UNIQUE : un deuxième lancement (double-clic sur l'icône de
        # bureau) ne crée pas un second processus, il réveille celui-ci — sinon on
        # aurait deux icônes dans la barre.
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.fenetre = None
        self.indicateur = None
        self.menu = None
        self.vivant = None            # None = pas encore su
        self.bandes = []

    # -- cycle de vie ------------------------------------------------------
    def do_startup(self):
        Gtk.Application.do_startup(self)
        # Sans `hold`, masquer la fenêtre (la croix) ferait quitter GTK, et
        # l'icône de barre disparaîtrait avec.
        self.hold()
        self._icones_locales()
        self._construire_indicateur()
        GLib.timeout_add_seconds(POLL_S, self._battement)
        self._sonder()

    def do_activate(self):
        if self.fenetre is None:
            self.fenetre = Fenetre(self)
        self.fenetre.montrer()

    def _icone(self, nom):
        """Chemin ABSOLU du PNG, sinon le nom nu.

        Les barres d'état ne résolvent pas toutes un nom d'icône : dms/quickshell
        (chez Tony) renvoie le nom tel quel à QML, qui ne sait qu'en faire — d'où
        le carré magenta « image cassée ». Un chemin absolu, lui, est
        explicitement géré (`file://…`), et c'est aussi le cas de waybar et des
        barres KDE. On garde le nom nu en dernier recours, pour les hôtes qui
        font une vraie recherche de thème."""
        for base in (os.path.join(os.environ.get(
                         "XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
                         "icons/hicolor/48x48/apps", nom + ".png"),
                     os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "icons", nom + "-48.png")):
            if os.path.isfile(base):
                return base
        return nom

    def _icones_locales(self):
        """Permet de tourner depuis le dépôt sans rien installer : GTK trouve
        `douze.svg` dans tools/icons/. (L'hôte de barre, lui, résout le nom via
        le thème système — d'où l'installation dans hicolor.)"""
        ici = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
        if os.path.isdir(ici):
            Gtk.IconTheme.get_default().append_search_path(ici)

    # -- icône de barre ----------------------------------------------------
    def _construire_indicateur(self):
        if AppIndicator is None:
            print("[douze-app] pas d'AyatanaAppIndicator : fenêtre seule.",
                  file=sys.stderr)
            return

        self.indicateur = AppIndicator.Indicator.new(
            APP_ID, self._icone("douze-off"),
            AppIndicator.IndicatorCategory.HARDWARE)
        self.indicateur.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicateur.set_title("Douze")
        self.menu = Gtk.Menu()
        self.indicateur.set_menu(self.menu)
        self._remplir_menu()

    def _item(self, libelle, action=None, actif=True):
        it = Gtk.MenuItem(label=libelle)
        it.set_sensitive(actif and action is not None)
        if action is not None:
            it.connect("activate", lambda *_: action())
        it.show()
        self.menu.append(it)
        return it

    def _remplir_menu(self):
        """Menu reconstruit à chaque sondage : les bandes vont et viennent."""
        if self.menu is None:
            return

        for vieux in self.menu.get_children():
            self.menu.remove(vieux)

        self._item("Ouvrir Douze", self.do_activate)

        sep = Gtk.SeparatorMenuItem(); sep.show(); self.menu.append(sep)

        if self.vivant is None:
            self._item("Connexion…", None)
        elif not self.vivant:
            self._item("Démon arrêté", None)
            self._item("Démarrer le démon", lambda: _systemctl("start", UNIT))
        else:
            if not self.bandes:
                self._item("Aucune bande d'effets", None)

            for b in self.bandes:
                sid = b.get("id")
                nom = b.get("name") or sid
                en_marche = bool(b.get("running"))
                prete = bool(b.get("ready"))
                etat = "● en marche" if prete else ("◐ démarrage…" if en_marche
                                                    else "○ arrêtée")
                self._item(f"Bande {nom} : {etat}", None)
                self._item("    Arrêter" if en_marche else "    Démarrer",
                           lambda s=sid, m=en_marche: self._bande(s, m))

            sep2 = Gtk.SeparatorMenuItem(); sep2.show(); self.menu.append(sep2)
            self._item("Redémarrer le démon",
                       lambda: _systemctl("restart", UNIT))

        sep3 = Gtk.SeparatorMenuItem(); sep3.show(); self.menu.append(sep3)
        # Dit explicitement ce que ça NE fait pas : la crainte légitime, avec un
        # micro en direct, c'est de tout couper en fermant une fenêtre.
        self._item("Quitter (le démon continue)", self._quitter)

    # -- sondage -----------------------------------------------------------
    def _battement(self):
        self._sonder()
        return True                   # True = on se rappelle

    def _sonder(self):
        """Interroge le démon dans un THREAD, applique le résultat dans la boucle
        GTK. Un urlopen sur le thread principal figerait fenêtre et menu."""
        def travail():
            etat = _get("/state")
            fx = _get("/fx") if etat is not None else None
            GLib.idle_add(self._appliquer, etat is not None,
                          (fx or {}).get("strips", []))

        threading.Thread(target=travail, daemon=True).start()

    def _appliquer(self, vivant, bandes):
        change = (vivant != self.vivant)
        self.vivant, self.bandes = vivant, bandes

        if self.indicateur is not None:
            self.indicateur.set_icon_full(
                self._icone("douze" if vivant else "douze-off"), "Douze")
            self.indicateur.set_title("Douze" if vivant
                                      else "Douze — démon arrêté")

        self._remplir_menu()

        # Le démon vient de revenir : la page affichait une erreur de connexion.
        if change and vivant and self.fenetre is not None:
            self.fenetre.recharger()

        return False                  # idle_add : ne pas répéter

    # -- actions -----------------------------------------------------------
    def _bande(self, sid, en_marche):
        def travail():
            _post_fx({"cmd": "stop" if en_marche else "start", "id": sid})
            GLib.idle_add(self._sonder)

        threading.Thread(target=travail, daemon=True).start()

    def _quitter(self):
        if self.fenetre is not None:
            self.fenetre.destroy()
        self.release()
        self.quit()


if __name__ == "__main__":
    sys.exit(Douze().run(sys.argv))
