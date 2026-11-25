"""Web-Oberflaeche des Home Displays: http://<pi>:8080

Zeigt eine Vorschau des Displays und ein Formular fuer die Einstellungen.
Speichern schreibt config.json und startet das Programm neu (systemd startet
es nach rund 10 Sekunden wieder). Nur Python-Standardbibliothek.

Die Seite hat keine Anmeldung. Sie ist nur im Heimnetz erreichbar und kann nur
die Anzeige einstellen, nichts in openHAB schalten.
"""

import base64
import html
import io
import json
import logging
import os
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("openhab-display")

FELD_NAMEN = ["Feld oben links", "Feld oben rechts", "Feld unten links", "Feld unten rechts"]
LISTEN = [
    ("fenster", "Fenster", "Ist eines offen, wird die Fußzeile schwarz."),
    ("rauchmelder", "Rauchmelder", "Alles außer IDLE_OFF gilt als Alarm."),
    ("wassermelder_schalter", "Wassermelder (Schalter)", "ON gilt als Wasser."),
    ("wassermelder_text", "Wassermelder (Text)", "Alles außer DRY gilt als Wasser."),
]
EINZELNE = [
    ("wetter", "zustand", "Wetterlage heute", "Symbol (und Text) in der Fußzeile"),
    ("wetter", "min", "Tiefsttemperatur heute", ""),
    ("wetter", "max", "Höchsttemperatur heute", ""),
    ("wetter", "sonnenuntergang", "Sonnenuntergang", "Uhrzeit ganz rechts in der Fußzeile"),
    ("wetter", "jetzt", "Wetterlage jetzt", "Regen jetzt + Regen heute = Schirm-Symbol"),
    ("wetter", "regen_jetzt", "Regenmenge jetzt", "größer 0 = es regnet jetzt"),
    ("wetter", "regen_heute", "Regenmenge heute", "ab 1 mm = Regen hält an"),
    ("wetter", "aussen", "Außentemperatur (für Glatteis)", "höchstens 1° + Nässe = Glatteis-Warnung"),
    ("wetter", "feuchte", "Luftfeuchte (für Glatteis)", "ab 90 % zählt als nass (Reif)"),
    ("keller", "tuer", "Kellertür", "OPEN = Vollbild „ALARM KELLER“"),
    ("keller", "alarm", "Alarm Keller (Zone 2)", "ON = Vollbild „ALARM KELLER“"),
    ("alarmanlage", "scharf", "Alarmanlage scharf", "ON = „SCHARF“ in der Kopfzeile"),
    ("alarmanlage", "ausgeloest", "Alarm ausgelöst", "ON = Vollbild „ALARM“"),
    ("alarmanlage", "still", "Stiller Alarm", "ON = Vollbild „ALARM“"),
    ("alarmanlage", "gespeichert", "Gespeicherte Alarme (Anzahl)", "größer 0 = Hinweis in der Fußzeile"),
    ("anwesenheit", "vorzimmer", "Jemand im Vorzimmer", "ON = Display wird aktualisiert"),
    ("anwesenheit", "tuer", "Wohnungstür (Ankunft)",
     "OPEN, während niemand zu Hause ist = gilt 60 s als „jemand im Vorzimmer“ (der Melder reagiert erst rund 25 s später)"),
    ("anwesenheit", "personen", "Personen in der Wohnung", "0 = ABWESEND, mehr als 1 = Gäste"),
    ("anwesenheit", "ich", "Ich bin da", "ON = ich bin zu Hause (sonst Gäste)"),
    ("anwesenheit", "simulation", "Anwesenheitssimulation", "ON = Hinweis in der Fußzeile"),
    ("waesche", "item", "Waschmaschine (Status-Text)",
     "„Wäsche läuft (seit …)“ = Symbol + Zeit in der Fußzeile, „Wäsche fertig (…)“ = Fußzeile schwarz"),
]

CSS = """
:root { color-scheme: light dark; --rand: #8884; --fehler: #c62828; --ok: #2e7d32; }
* { box-sizing: border-box; }
body { font-family: -apple-system, system-ui, sans-serif; margin: 0 auto; padding: 16px;
       max-width: 760px; line-height: 1.4; }
h1 { font-size: 1.4em; margin: 0 0 12px; }
h2 { font-size: 1.1em; margin: 0 0 8px; }
fieldset { border: 1px solid var(--rand); border-radius: 8px; padding: 12px; margin: 0 0 16px; }
legend { font-weight: 600; padding: 0 4px; }
label { display: block; font-weight: 600; margin: 8px 0 2px; }
input[type=text], input[type=number], textarea { width: 100%; padding: 6px; font: inherit;
       border: 1px solid var(--rand); border-radius: 4px; }
textarea { font-family: ui-monospace, monospace; font-size: .9em; }
.hinweis { font-size: .85em; opacity: .75; margin: 2px 0 0; }
table.symbole { border-collapse: collapse; width: 100%; }
table.symbole td { padding: 6px 8px; border-top: 1px solid var(--rand); vertical-align: middle; }
td.sym { width: 1%; white-space: nowrap; }
td.sym img { display: block; image-rendering: pixelated; border: 1px solid var(--rand); }
.wert { font-size: .85em; color: var(--ok); }
.unbekannt { font-size: .85em; color: var(--fehler); font-weight: 600; }
.zeile { display: grid; grid-template-columns: 1fr 1fr; gap: 0 12px; }
.vorschau { text-align: center; margin: 0 0 16px; }
.vorschau img { width: 100%; max-width: 500px; image-rendering: pixelated;
       border: 6px solid #888; border-radius: 6px; background: #fff; }
.vorschau img + img { margin-top: 8px; }
.meldung { padding: 10px 12px; border-radius: 6px; margin: 0 0 16px; }
.meldung.ok { background: #2e7d3222; border: 1px solid var(--ok); }
.meldung.fehler { background: #c6282822; border: 1px solid var(--fehler); }
.knoepfe { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 16px; }
button { font: inherit; padding: 8px 14px; border-radius: 6px; border: 1px solid var(--rand);
       cursor: pointer; }
button.haupt { background: #1565c0; color: #fff; border-color: #1565c0; font-weight: 600; }
@media (max-width: 520px) { .zeile { grid-template-columns: 1fr; } }
select { width: 100%; padding: 6px; font: inherit; }
.abfahrt { border-top: 1px solid var(--rand); padding: 8px 0; }
.abfahrt:first-of-type { border-top: 0; }
.richtung { display: block; font-weight: normal; padding: 6px 0; }
a.knopf { display: inline-block; padding: 8px 14px; border-radius: 6px; border: 1px solid var(--rand);
       text-decoration: none; color: inherit; }
"""
CSS += """
.karte { background: #1565c00d; border: 1px solid #1565c033; border-radius: 8px; padding: 4px 12px 12px;
       margin: 12px 0; }
.karte h2 { margin: 8px 0 4px; color: #1565c0; }
@media (prefers-color-scheme: dark) { .karte { background: #64b5f614; border-color: #64b5f640; }
       .karte h2 { color: #90caf9; } }
.itembox { position: relative; }
.itemzeile { display: flex; gap: 6px; align-items: center; margin: 4px 0; }
.itemzeile .itembox { flex: 1; }
.itemzeile input.anzeigename { flex: 1; }
.itemzeile button { padding: 4px 10px; }
.vorschlaege { position: absolute; left: 0; right: 0; top: 100%; z-index: 10; max-height: 280px;
       overflow-y: auto; margin: 2px 0 0; padding: 0; list-style: none; background: Canvas; color: CanvasText;
       border: 1px solid var(--rand); border-radius: 6px; box-shadow: 0 4px 16px #0003; }
.vorschlaege li { padding: 6px 8px; cursor: pointer; border-bottom: 1px solid var(--rand); }
.vorschlaege li.aktiv, .vorschlaege li:hover { background: #1565c022; }
.vorschlaege li small { display: block; opacity: .7; }
.vorschlaege li.mehr { cursor: default; opacity: .7; font-size: .85em; }
nav.reiter { display: flex; gap: 4px; border-bottom: 2px solid var(--rand); margin: 0 0 16px;
       overflow-x: auto; }
nav.reiter a { padding: 8px 14px; text-decoration: none; color: inherit; border-radius: 6px 6px 0 0;
       white-space: nowrap; }
nav.reiter a.aktiv { font-weight: 700; border: 2px solid var(--rand); border-bottom: 2px solid Canvas;
       margin-bottom: -2px; }
"""
REITER = {
    "allgemein": ("Allgemein", "/"),
    "haupt": ("openHAB-Display", "/display"),
    "wl": ("Wiener Linien", "/wl"),
    "bt": ("Bluetooth", "/bluetooth"),
    "wlan": ("WLAN", "/wlan"),
    "symbole": ("Hilfe", "/hilfe"),
}
REITER_ZIEL = {name: pfad for name, (_, pfad) in REITER.items()}
# Auswahlliste mit Suche fuer Item-Felder (input.itemwahl), Daten in ITEMS = [[Name, Label, Wert], ...]
JS_ITEMWAHL = """
(function () {
  var MAX = 150;
  function suchen(text) {
    var w = text.toLowerCase().split(/\\s+/).filter(Boolean), aus = [];
    for (var i = 0; i < ITEMS.length; i++) {
      var h = (ITEMS[i][0] + ' ' + ITEMS[i][1]).toLowerCase();
      if (w.every(function (x) { return h.indexOf(x) >= 0; })) aus.push(ITEMS[i]);
    }
    return aus;
  }
  function esc(t) { var d = document.createElement('div'); d.textContent = t; return d.innerHTML; }
  function einrichten(eingabe) {
    if (eingabe.dataset.fertig) return;
    eingabe.dataset.fertig = '1';
    eingabe.setAttribute('autocomplete', 'off');
    var box = document.createElement('div'); box.className = 'itembox';
    eingabe.parentNode.insertBefore(box, eingabe); box.appendChild(eingabe);
    var liste = document.createElement('ul'); liste.className = 'vorschlaege'; liste.hidden = true;
    box.appendChild(liste);
    var treffer = [], aktiv = -1;
    function zeigen(text) {
      treffer = suchen(text); aktiv = -1;
      var html = treffer.slice(0, MAX).map(function (it, i) {
        return '<li data-i="' + i + '"><b>' + esc(it[0]) + '</b><small>' + esc(it[1] || '') +
               (it[2] ? ' · ' + esc(it[2]) : '') + '</small></li>';
      }).join('');
      if (!treffer.length) html = '<li class="mehr">Kein Item gefunden</li>';
      else if (treffer.length > MAX) html += '<li class="mehr">… ' + (treffer.length - MAX) +
               ' weitere, bitte weiter tippen</li>';
      liste.innerHTML = html; liste.hidden = false; liste.scrollTop = 0;
    }
    function waehlen(i) {
      eingabe.value = treffer[i][0]; liste.hidden = true;
      eingabe.dispatchEvent(new Event('change', {bubbles: true}));
    }
    eingabe.addEventListener('focus', function () { zeigen(''); });
    eingabe.addEventListener('input', function () { zeigen(eingabe.value); });
    eingabe.addEventListener('blur', function () { setTimeout(function () { liste.hidden = true; }, 150); });
    liste.addEventListener('mousedown', function (ev) {
      var li = ev.target.closest('li[data-i]'); if (li) { ev.preventDefault(); waehlen(+li.dataset.i); }
    });
    eingabe.addEventListener('keydown', function (ev) {
      if (liste.hidden) return;
      var n = Math.min(treffer.length, MAX);
      if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
        ev.preventDefault();
        aktiv = (aktiv + (ev.key === 'ArrowDown' ? 1 : n - 1)) % n;
        [].forEach.call(liste.children, function (li, i) { li.classList.toggle('aktiv', i === aktiv); });
        var li = liste.children[aktiv]; if (li) li.scrollIntoView({block: 'nearest'});
      } else if (ev.key === 'Enter' && aktiv >= 0) { ev.preventDefault(); waehlen(aktiv); }
      else if (ev.key === 'Escape') { liste.hidden = true; }
    });
  }
  window.itemwahlEinrichten = function () {
    [].forEach.call(document.querySelectorAll('input.itemwahl'), einrichten);
  };
  document.addEventListener('click', function (ev) {
    var k = ev.target.closest('button.item-plus');
    if (k) {
      var zeile = document.createElement('div'); zeile.className = 'itemzeile';
      zeile.innerHTML = '<input type="text" class="itemwahl" name="' + k.dataset.name + '" placeholder="Item">' +
        (k.dataset.liste ? '<input type="text" class="anzeigename" name="' + k.dataset.liste +
                           '_name" placeholder="Name am Display">' : '') +
        '<button type="button" class="item-weg" title="Entfernen">✕</button>';
      k.parentNode.insertBefore(zeile, k); itemwahlEinrichten(); zeile.querySelector('input').focus();
    }
    var w = ev.target.closest('button.item-weg');
    if (w) { w.closest('.itemzeile').remove(); }
  });
  document.addEventListener('change', function (ev) {
    var t = ev.target;
    if (!t.classList || !t.classList.contains('itemwahl')) return;
    var name = t.closest('.itemzeile') && t.closest('.itemzeile').querySelector('input.anzeigename');
    if (!name || name.value) return;
    for (var i = 0; i < ITEMS.length; i++) if (ITEMS[i][0] === t.value) { name.value = ITEMS[i][1]; break; }
  });
  itemwahlEinrichten();
})();
"""

MAX_ABFAHRTEN = 9          # je 3 auf einer Abfahrten-Seite (Wunsch des Nutzers)
_ORDNER = os.path.dirname(os.path.abspath(__file__))
# Downloads im Reiter Hilfe: Adresse -> (Datei am Pi, Name beim Herunterladen, Typ, Titel, Beschreibung).
# Fehlt eine Datei am Pi, erscheint ihr Link nicht.
DOWNLOADS = {
    "/anleitung.pdf": ("anleitung.pdf", "Home-Display-Bedienungsanleitung.pdf", "application/pdf",
                       "Bedienungsanleitung",
                       "Alles zu Anzeige, Einstellungen, WLAN, Ein- und Ausschalten und was tun, wenn etwas "
                       "nicht stimmt."),
    "/installation.pdf": ("installation.pdf", "Home-Display-Installationsanleitung.pdf", "application/pdf",
                          "Installationsanleitung",
                          "Ein eigenes Home Display einrichten, auch ohne openHAB (nur Wiener Linien)."),
    "/paket.tar.gz": ("paket.tar.gz", "Home-Display-Paket.tar.gz", "application/gzip",
                      "Installationspaket",
                      "Programm, Installationsskript und Start-Einstellung für ein neues Home Display "
                      "(siehe Installationsanleitung)."),
}
KONTAKT = ""   # Kontakt-Adresse fuer den Reiter Hilfe (leer = keine)
QUELLE = ("Abfahrtsdaten: Wiener Linien, Open Government Data (CC BY 4.0), "
          "data.wien.gv.at")


def png_daten(bild):
    puffer = io.BytesIO()
    bild.save(puffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(puffer.getvalue()).decode("ascii")



def aus_text(hub):
    """Was nach dem Herunterfahren zu tun ist, je nachdem ob der Hub mit Akku dran ist."""
    if hub:
        return ("Nach etwa 30 Sekunden den Taster am Hub lange drücken, damit er ganz ausgeht "
                "(der Akku versorgt den Pi sonst weiter). Zum Einschalten den Taster kurz drücken.")
    return ("Nach etwa 30 Sekunden kannst du den Strom trennen oder ausschalten. "
            "Zum Einschalten den Pi wieder mit Strom versorgen.")


def aus_rueckfrage(hub):
    if hub:
        return "Er startet erst wieder, wenn du den Taster am Hub lange und dann kurz drückst."
    return "Er startet erst wieder, wenn du den Strom aus- und wieder einschaltest."

class Web:
    def __init__(self, g, warteschlange, display, prog):
        self.g = g
        self.q = warteschlange
        self.display = display
        self.prog = prog
        self._items = None
        self._items_zeit = 0.0
        import wiener_linien
        self.wlmod = wiener_linien
        self.wl = wiener_linien.WienerLinien(prog.ORDNER)

    def abfahrten_vorschau(self):
        """Bilder der Abfahrten-Seiten. Holt frische Daten, wenn keine aktuellen da sind."""
        cfg = self.g.cfg
        eintraege = cfg["wiener_linien"]["abfahrten"]
        if not cfg["wiener_linien"]["aktiv"] or not eintraege:
            return None
        if not self.g.abfahrt_zeilen():
            try:
                abruf = (time.time(), self.wl.abfahrten(eintraege))
                with self.g.sperre:
                    self.g.oeffis = abruf
            except Exception as e:
                log.warning("Wiener Linien nicht abrufbar: %s", e)
                return None
        seiten = max(1, -(-len(self.g.abfahrt_zeilen()) // self.prog.PRO_SEITE))
        return [self.g.vorschau("abfahrten", n) for n in range(1, seiten + 1)]

    def items(self):
        """Item-Liste aus openHAB, 2 Minuten zwischengespeichert. None, wenn nicht erreichbar."""
        if self._items is None or time.time() - self._items_zeit > 120:
            try:
                self._items = self.prog.item_liste_holen()
                self._items_zeit = time.time()
            except Exception as e:
                log.warning("Item-Liste nicht abrufbar: %s", e)
                return self._items
        return self._items

    # --- Formular -> Einstellungen ---

    def _label(self, item):
        """Beschriftung des Items aus openHAB, sonst der Item-Name."""
        i = (self.items() or {}).get(item) or {}
        return i.get("label") or item

    def aus_formular(self, f, teil="haupt"):
        """Uebernimmt die Felder eines Reiters in die aktuellen Einstellungen.

        teil = "haupt" (openHAB-Display) oder "wl" (Wiener Linien). Der jeweils
        andere Teil bleibt, wie er gespeichert ist.
        """
        def t(name):
            return f.get(name, [""])[0].strip()

        cfg = self.prog.config_laden()
        fehler = []
        if teil == "wl":
            self._wl_aus_formular(f, t, cfg, fehler)
            return cfg, fehler
        if teil == "bt":
            self._bt_aus_formular(f, t, cfg, fehler)
            return cfg, fehler
        if teil == "wlan":
            self._wlan_aus_formular(f, t, cfg, fehler)
            return cfg, fehler
        if teil == "allgemein":
            cfg["openhab"] = t("openhab") or cfg["openhab"]
            if not cfg["openhab"].startswith(("http://", "https://")):
                fehler.append("Die openHAB-Adresse muss mit http:// oder https:// beginnen.")
            cfg["drehung"] = 180 if t("drehung") == "180" else 0
            try:
                cfg["messwerte_abstand_min"] = min(60, max(1, int(t("abstand"))))
            except ValueError:
                fehler.append("Der Abstand für Messwerte muss eine ganze Zahl (Minuten) sein.")
            try:
                cfg["leeren_nach_min"] = min(1440, max(0, int(t("leeren"))))
            except ValueError:
                fehler.append("Die Zeit bis zum Weißschalten muss eine ganze Zahl (Minuten) sein.")
            cfg["openhab_aktiv"] = t("openhab_aktiv") == "1"
            akt = cfg.setdefault("aktualisieren", {})
            if t("akt_modus") in ("anwesenheit", "zeiten", "immer"):
                akt["modus"] = t("akt_modus")
            if t("akt_ersatz") in ("zeiten", "immer"):
                akt["ersatz"] = t("akt_ersatz")
            zeiten = []
            for nr in range(4):
                von, bis = t(f"zt{nr}_von"), t(f"zt{nr}_bis")
                tage = sorted({int(x) for x in f.get(f"zt{nr}_tag", []) if x.isdigit() and int(x) < 7})
                if not von and not bis:
                    continue
                if not (len(von) == 5 and len(bis) == 5 and von[2] == bis[2] == ":") or not tage:
                    fehler.append(f"Zeitplan Zeile {nr + 1}: bitte Tage ankreuzen und von/bis als Uhrzeit angeben.")
                    continue
                zeiten.append({"tage": tage, "von": von, "bis": bis})
            akt["zeiten"] = zeiten
            if akt.get("modus") == "zeiten" and not zeiten:
                fehler.append("Für „Nach Zeitplan“ bitte mindestens ein Zeitfenster eintragen.")
            if akt.get("modus") == "anwesenheit" and not cfg["openhab_aktiv"]:
                fehler.append("Ohne openHAB gibt es keinen Bewegungsmelder: bitte „Nach Zeitplan“ oder "
                              "„Immer“ wählen.")
            return cfg, fehler

        cfg["haupt_aktiv"] = t("haupt_aktiv") == "1"
        if t("anzeige_s"):
            try:
                cfg["anzeige_s"] = min(120, max(3, int(t("anzeige_s"))))
            except ValueError:
                fehler.append("Die Anzeigedauer der Hauptansicht muss eine ganze Zahl (Sekunden) sein.")

        def zuordnung(text):
            ergebnis = {}
            for zeile in text.splitlines():
                if "=" in zeile:
                    item, name = zeile.split("=", 1)
                    if item.strip():
                        ergebnis[item.strip()] = name.strip() or item.strip()
                elif zeile.strip():
                    ergebnis[zeile.strip()] = zeile.strip()
            return ergebnis

        for i in range(4):
            feld = cfg["felder"][i]
            feld["titel"] = t(f"f{i}_titel")
            roh = "\n".join(f.get(f"f{i}_items", []))
            feld["items"] = [z.strip() for z in roh.replace(",", "\n").splitlines() if z.strip()]
            feld["einheit"] = f.get(f"f{i}_einheit", [""])[0]  # Leerzeichen am Anfang behalten
            try:
                feld["stellen"] = min(3, max(0, int(t(f"f{i}_stellen"))))
            except ValueError:
                fehler.append(f"{FELD_NAMEN[i]}: Nachkommastellen müssen eine Zahl von 0 bis 3 sein.")
            if not feld["items"]:
                fehler.append(f"{FELD_NAMEN[i]}: mindestens ein Item eintragen.")
        for schluessel, _, _ in LISTEN:
            if f"{schluessel}_item" in f:          # Zeilen (Auswahl)
                namen = f.get(f"{schluessel}_name", [])
                cfg[schluessel] = {}
                for nr, item in enumerate(f[f"{schluessel}_item"]):
                    item = item.strip()
                    if item:
                        name = namen[nr].strip() if nr < len(namen) else ""
                        cfg[schluessel][item] = name or self._label(item)
            else:                                   # altes Textfeld "Item = Name"
                cfg[schluessel] = zuordnung(f.get(schluessel, [""])[0])
        for gruppe, schluessel, _, _ in EINZELNE:
            cfg[gruppe][schluessel] = t(f"{gruppe}_{schluessel}")
        cfg["wetter_text"] = t("wetter_text") == "1"
        kl = cfg["kleidung"]
        kl["aktiv"] = t("kl_aktiv") == "1"
        kl["gefuehlt"] = t("kl_gefuehlt")
        try:
            kl["feld"] = min(3, max(0, int(t("kl_feld")) - 1))
            grenzen = [int(t(f"kl_grenze{i}")) for i in range(3)]
            if not grenzen[0] > grenzen[1] > grenzen[2]:
                raise ValueError
            kl["grenzen"] = grenzen
        except ValueError:
            fehler.append("Kleidung: die Grenzen müssen ganze Zahlen sein, von oben nach unten kleiner werdend.")
        sh = cfg["strom_hoch"]
        sh["aktiv"] = t("sh_aktiv") == "1"
        try:
            sh["feld"] = min(3, max(0, int(t("sh_feld")) - 1))
            sh["grenze"] = max(0, int(t("sh_grenze")))
        except ValueError:
            fehler.append("Hoher Verbrauch: die Grenze muss eine ganze Zahl sein.")
        sv = cfg["strom_vergleich"]
        sv["aktiv"] = t("sv_aktiv") == "1"
        sv["leistung"] = t("sv_leistung")
        try:
            sv["feld"] = min(3, max(0, int(t("sv_feld")) - 1))
            sv["tage"] = min(60, max(5, int(t("sv_tage"))))
        except ValueError:
            fehler.append("Vergleich: die Anzahl der Tage muss eine ganze Zahl sein (5 bis 60).")
        cfg["uv"]["aktiv"] = t("uv_aktiv") == "1"
        try:
            cfg["uv"]["grenze"] = min(11, max(1, round(float(t("uv_grenze").replace(",", ".")), 1)))
        except ValueError:
            fehler.append("UV-Grenze: bitte eine Zahl von 1 bis 11 eintragen.")

        bekannt = self.items()
        if bekannt is not None:
            fehlend = [n for n in self.prog.benutzte_items(cfg) if n not in bekannt]
            if fehlend:
                fehler.append("Diese Items gibt es in openHAB nicht: " + ", ".join(fehlend))
        return cfg, fehler

    def _bt_aus_formular(self, f, t, cfg, fehler):
        import bluetooth_naehe
        bt = cfg["bluetooth"]
        bt["aktiv"] = t("bt_aktiv") == "1"
        bt["nur_wenn_abwesend"] = t("bt_nur_weg") == "1"
        roh = t("bt_adresse")
        adresse = bluetooth_naehe.adresse_pruefen(roh) if roh else ""
        if roh and not adresse:
            fehler.append(f"„{roh}“ ist keine Bluetooth-Adresse (Form AA:BB:CC:11:22:33).")
        if adresse != bt["adresse"]:
            bt["geraetename"] = ""
        bt["adresse"] = adresse or ""
        bt["geraetename"] = t("bt_geraetename") or bt["geraetename"]
        bt["item"] = t("bt_item")
        try:
            bt["intervall_s"] = min(120, max(5, int(t("bt_intervall"))))
            bt["weg_nach_s"] = min(3600, max(bt["intervall_s"], int(t("bt_weg"))))
        except ValueError:
            fehler.append("Abfrage-Abstand und Zeit bis „weg“ müssen ganze Zahlen (Sekunden) sein.")
        if t("bt_pegel_an") == "1":
            try:
                bt["mindestpegel"] = min(0, max(-40, int(t("bt_pegel"))))
            except ValueError:
                fehler.append("Der Mindest-Signalpegel muss eine ganze Zahl zwischen −40 und 0 sein.")
        else:
            bt["mindestpegel"] = None
        if bt["aktiv"] and not bt["adresse"]:
            fehler.append("Zum Einschalten bitte zuerst die Bluetooth-Adresse des Handys eintragen.")
        if bt["aktiv"] and not bt["item"]:
            fehler.append("Zum Einschalten bitte das openHAB-Item eintragen.")
        bekannt = self.items()
        if bt["item"] and bekannt is not None and bt["item"] not in bekannt:
            fehler.append(f"Das Item {bt['item']} gibt es in openHAB nicht.")
        elif bt["item"] and bekannt and bekannt[bt["item"]].get("type") != "Switch":
            fehler.append(f"Das Item {bt['item']} muss ein Schalter (Switch) sein.")

    def _wl_aus_formular(self, f, t, cfg, fehler):
        wl = cfg["wiener_linien"]
        wl["aktiv"] = t("wl_aktiv") == "1"
        try:
            wl["wechsel_s"] = min(120, max(3, int(t("wl_wechsel"))))
        except ValueError:
            fehler.append("Wiener Linien: Die Anzeigedauer muss eine ganze Zahl (Sekunden) sein.")
        wl["laufen"] = t("wl_laufen") == "1"
        try:
            wl["laufen_min"] = min(10, max(1, int(t("wl_laufen_min"))))
        except ValueError:
            fehler.append("Wiener Linien: Die Minuten fürs Laufen müssen eine ganze Zahl sein.")
        wl["rollstuhl"] = t("wl_rollstuhl") == "1"
        wl["klima"] = t("wl_klima") == "1"
        try:
            wl["klima_ab"] = float(t("wl_klima_ab").replace(",", "."))
        except ValueError:
            fehler.append("Wiener Linien: Die Temperatur für die Klimaanlage muss eine Zahl sein.")
        wl["abfahrten"] = []
        for i in range(MAX_ABFAHRTEN):
            daten = t(f"wl{i}_daten")
            if not daten or t(f"wl{i}_weg") == "1":
                continue
            try:
                e = json.loads(daten)
            except ValueError:
                continue
            e["name"] = t(f"wl{i}_name") or self.wlmod.kurzname(e.get("ziel", ""))
            try:
                e["gehzeit_min"] = min(60, max(0, int(t(f"wl{i}_gehzeit"))))
            except ValueError:
                fehler.append(f"Wiener Linien: Gehzeit für {e['linie']} {e['name']} muss eine ganze Zahl sein.")
            wl["abfahrten"].append(e)

    # --- Seiten ---

    def kopf(self, reiter, titel="Home Display", meldung="", fehler=(), neustart=False):
        """Seitenanfang mit den drei Reitern."""
        e = html.escape
        ziel = REITER_ZIEL.get(reiter, "/")
        teile = ["<!doctype html><html lang='de'><head><meta charset='utf-8'>",
                 "<meta name='viewport' content='width=device-width, initial-scale=1'>",
                 f"<title>{e(titel)}</title>"]
        if neustart:
            teile.append(f"<meta http-equiv='refresh' content='25;url={ziel}'>")
        teile.append(f"<style>{CSS}</style></head><body><h1>Home Display</h1><nav class='reiter'>")
        for name, (text, pfad) in REITER.items():
            aktiv = " class='aktiv'" if name == reiter else ""
            teile.append(f"<a href='{pfad}'{aktiv}>{e(text)}</a>")
        teile.append("</nav>")
        if meldung:
            teile.append(f"<div class='meldung ok'>{e(meldung)}</div>")
        if fehler:
            teile.append("<div class='meldung fehler'><b>Nicht gespeichert:</b><ul>"
                         + "".join(f"<li>{e(x)}</li>" for x in fehler) + "</ul></div>")
        return teile

    def seite(self, cfg, meldung="", fehler=(), neustart=False, reiter="haupt"):
        if reiter == "wl":
            return self.seite_wl(cfg, meldung, fehler, neustart)
        if reiter == "bt":
            return self.seite_bluetooth(cfg, meldung, fehler, neustart)
        if reiter == "allgemein":
            return self.seite_allgemein(cfg, meldung, fehler, neustart)
        e = html.escape
        items = self.items() or {}

        def info(name):
            if not name:
                return ""
            i = items.get(name)
            if not items:
                return ""
            if i is None:
                return '<span class="unbekannt">unbekanntes Item</span>'
            label = i.get("label") or ""
            return f'<span class="wert">{e(label)}: {e(str(i.get("state")))}</span>'

        teile = self.kopf("haupt", meldung=meldung, fehler=fehler, neustart=neustart)
        if neustart:
            teile.append("</body></html>")
            return "".join(teile)
        if not cfg.get("openhab_aktiv", True):
            teile.append("<div class='meldung fehler'>openHAB ist im Reiter Allgemein ausgeschaltet. Die "
                         "Einstellungen hier gelten erst, wenn es wieder eingeschaltet ist.</div>")

        teile.append("<div class='vorschau'><img src='%s' alt='Vorschau Hauptansicht'>"
                     "<p class='hinweis'>So sähe die Hauptansicht jetzt aus (Werte von gerade eben).</p></div>"
                     % png_daten(self.g.vorschau()))

        teile.append("<form method='post' action='/speichern'>")
        an = " checked" if cfg.get("haupt_aktiv", True) else ""
        teile.append("<fieldset><legend>Anzeige</legend>"
                     f"<div><input type='checkbox' name='haupt_aktiv' id='haupt_aktiv' value='1'{an}> "
                     "<label for='haupt_aktiv' style='display:inline'>Hauptansicht anzeigen</label></div>"
                     "<p class='hinweis'>Ohne Hakerl zeigt das Display nur die Abfahrten der Wiener Linien "
                     "(gibt es keine, bleibt es weiß). Vollbild-Alarme kommen immer.</p>"
                     "<label>Hauptansicht anzeigen … Sekunden</label>"
                     f"<input type='number' name='anzeige_s' min='3' max='120' value='{cfg.get('anzeige_s', 7)}'>"
                     "<p class='hinweis'>Solange jemand im Vorzimmer ist und Abfahrten der Wiener Linien "
                     "eingestellt sind, wechselt das Display zwischen Hauptansicht und Abfahrten. So lange "
                     "bleibt die Hauptansicht stehen. Die Dauer der Abfahrten-Seite steht im Reiter Wiener "
                     f"Linien (jetzt {e(str(cfg['wiener_linien']['wechsel_s']))} Sekunden).</p></fieldset>")
        # Felder
        teile.append("<fieldset><legend>Die vier Messwert-Felder</legend>"
                     "<p class='hinweis'>Mehrere Items in einem Feld (ein Item pro Zeile) ergeben den Mittelwert.</p>")
        for i, feld in enumerate(cfg["felder"]):
            zeilen = "".join(
                f"<div class='itemzeile'><input type='text' class='itemwahl' name='f{i}_items' value='{e(n)}'>"
                "<button type='button' class='item-weg' title='Entfernen'>✕</button></div>"
                f"<p class='hinweis' style='margin:0 0 4px'>{info(n)}</p>" for n in (feld["items"] or [""]))
            teile.append(f"<div class='karte'><h2>{FELD_NAMEN[i]}</h2><div class='zeile'><div>"
                         f"<label>Überschrift</label><input type='text' name='f{i}_titel' value='{e(feld['titel'])}'>"
                         f"</div><div class='zeile'><div><label>Einheit</label>"
                         f"<input type='text' name='f{i}_einheit' value='{e(feld['einheit'])}'></div><div>"
                         f"<label>Nachkommastellen</label><input type='number' min='0' max='3' "
                         f"name='f{i}_stellen' value='{feld['stellen']}'></div></div></div>"
                         "<label>Item(s)</label>" + zeilen
                         + f"<button type='button' class='item-plus' data-name='f{i}_items'>+ weiteres Item "
                         "(Mittelwert)</button></div>")
        kl = cfg["kleidung"]
        an = " checked" if kl["aktiv"] else ""
        optionen = "".join(f"<option value='{i + 1}'{' selected' if kl['feld'] == i else ''}>{e(n)}</option>"
                           for i, n in enumerate(FELD_NAMEN))
        g0, g1, g2 = kl["grenzen"]
        zahlfeld = "<input type='number' name='kl_grenze{}' value='{}' style='width:4.5em'>"
        teile.append(
            "<div class='karte'><h2>Was anziehen?</h2>"
            f"<div><input type='checkbox' name='kl_aktiv' id='kl_aktiv' value='1'{an}> "
            "<label for='kl_aktiv' style='display:inline'>Kleidungs-Symbol und gefühlte Temperatur zeigen in</label> "
            f"<select name='kl_feld'>{optionen}</select></div>"
            "<label>Gefühlte Temperatur (Item)</label>"
            f"<input type='text' class='itemwahl' name='kl_gefuehlt' value='{e(kl['gefuehlt'])}'>"
            f"<p class='hinweis'>Steht als „gefühlt X°“ in der Überschrift und bestimmt das Symbol "
            f"(leer = gemessene Temperatur). {info(kl['gefuehlt'])}</p>"
            "<p>T-Shirt ab " + zahlfeld.format(0, g0) + "°, Pullover ab " + zahlfeld.format(1, g1)
            + "°, Jacke ab " + zahlfeld.format(2, g2) + "°, darunter Winterjacke</p></div>")
        sh = cfg["strom_hoch"]
        an = " checked" if sh["aktiv"] else ""
        optionen = "".join(f"<option value='{i + 1}'{' selected' if sh['feld'] == i else ''}>{e(n)}</option>"
                           for i, n in enumerate(FELD_NAMEN))
        teile.append(
            "<div class='karte'><h2>Hoher Verbrauch</h2>"
            f"<div><input type='checkbox' name='sh_aktiv' id='sh_aktiv' value='1'{an}> "
            "<label for='sh_aktiv' style='display:inline'>Blitz zeigen in</label> "
            f"<select name='sh_feld'>{optionen}</select> ab "
            f"<input type='number' name='sh_grenze' min='0' value='{sh['grenze']}' style='width:6em'></div>"
            "<p class='hinweis'>Gilt für den Wert im Feld (z. B. Watt bei „Strom jetzt“).</p></div>")
        sv = cfg["strom_vergleich"]
        an = " checked" if sv["aktiv"] else ""
        optionen = "".join(f"<option value='{i + 1}'{' selected' if sv['feld'] == i else ''}>{e(n)}</option>"
                           for i, n in enumerate(FELD_NAMEN))
        teile.append(
            "<div class='karte'><h2>Vergleich mit einem üblichen Tag</h2>"
            f"<div><input type='checkbox' name='sv_aktiv' id='sv_aktiv' value='1'{an}> "
            "<label for='sv_aktiv' style='display:inline'>Balken zeigen in</label> "
            f"<select name='sv_feld'>{optionen}</select>, üblicher Tag aus den letzten "
            f"<input type='number' name='sv_tage' min='5' max='60' value='{sv['tage']}' style='width:4.5em'> Tagen</div>"
            "<label>Leistung (Item mit gespeichertem Verlauf, in Watt)</label>"
            f"<input type='text' class='itemwahl' name='sv_leistung' value='{e(sv['leistung'])}'>"
            "<p class='hinweis'>Der Strich in der Mitte des Balkens ist, was an einem üblichen Tag bis zur "
            "jetzigen Uhrzeit verbraucht ist (Median der Tage, Ausreißer zählen kaum). Voller Balken = doppelt so "
            "viel. Wird beim Start und jede Nacht aus dem gespeicherten Verlauf in openHAB berechnet. "
            f"{info(sv['leistung'])}</p></div>")
        teile.append("</fieldset>")

        # Einzelne Items
        teile.append("<fieldset><legend>Wetter, Alarme und Anwesenheit</legend>"
                     "<p class='hinweis'>Feld leer lassen, um etwas abzuschalten.</p>")
        for gruppe, schluessel, titel, hinweis in EINZELNE:
            name = cfg[gruppe][schluessel]
            teile.append(f"<div class='karte'><label>{e(titel)}</label><input type='text' class='itemwahl' "
                         f"name='{gruppe}_{schluessel}' value='{e(name)}'>"
                         f"<p class='hinweis'>{e(hinweis)} {info(name)}</p>")
            if (gruppe, schluessel) == ("wetter", "zustand"):
                an = " checked" if cfg.get("wetter_text", True) else ""
                teile.append(f"<div><input type='checkbox' name='wetter_text' id='wetter_text' value='1'{an}> "
                             "<label for='wetter_text' style='display:inline'>Wetterlage neben dem Symbol "
                             "auch als Text (z. B. „Bedeckt“)</label></div>")
                uv = cfg["uv"]
                an = " checked" if uv["aktiv"] else ""
                teile.append(f"<div style='margin-top:8px'><input type='checkbox' name='uv_aktiv' id='uv_aktiv' "
                             f"value='1'{an}> <label for='uv_aktiv' style='display:inline'>Sonnenbrille zeigen, "
                             f"wenn der UV-Index heute erreicht:</label> <input type='number' name='uv_grenze' "
                             f"min='1' max='11' step='1' value='{uv['grenze']:g}' style='width:5em'></div>"
                             "<p class='hinweis'>UV-Index von Open-Meteo (frei, CC BY 4.0), stündlich, für Wien. "
                             "WHO-Skala: 3–5 mäßig, 6–7 hoch, 8–10 sehr hoch.</p>")
            teile.append("</div>")
        teile.append("</fieldset>")

        # Listen
        teile.append("<fieldset><legend>Fenster und Melder</legend>"
                     "<p class='hinweis'>Pro Zeile ein Item und der Name, der am Display erscheint "
                     "(leer = Beschriftung aus openHAB).</p>")
        for schluessel, titel, hinweis in LISTEN:
            zeilen = "".join(
                "<div class='itemzeile'>"
                f"<input type='text' class='itemwahl' name='{schluessel}_item' value='{e(k)}' placeholder='Item'>"
                f"<input type='text' class='anzeigename' name='{schluessel}_name' value='{e(v)}' "
                "placeholder='Name am Display'>"
                "<button type='button' class='item-weg' title='Entfernen'>✕</button></div>"
                f"<p class='hinweis' style='margin:0 0 4px'>{info(k)}</p>"
                for k, v in (list(cfg[schluessel].items()) or [("", "")]))
            teile.append(f"<div class='karte'><h2>{e(titel)}</h2><p class='hinweis'>{e(hinweis)}</p>{zeilen}"
                         f"<button type='button' class='item-plus' data-name='{schluessel}_item' "
                         f"data-liste='{schluessel}'>+ weiteres Item</button></div>")
        teile.append("</fieldset>")

        teile.append("<div class='knoepfe'><button class='haupt'>Speichern und neu starten</button></div></form>")
        if items:
            daten = [[n, i.get("label") or "", str(i.get("state") if i.get("state") not in (None, "NULL") else "")]
                     for n, i in sorted(items.items(), key=lambda x: x[0].lower())]
            teile.append("<script>var ITEMS = " + json.dumps(daten, ensure_ascii=False).replace("</", "<\\/")
                         + ";" + JS_ITEMWAHL + "</script>")
        else:
            teile.append("<p class='meldung fehler'>openHAB ist gerade nicht erreichbar, "
                         "Item-Namen können nicht geprüft werden.</p>")
        teile.append("</body></html>")
        return "".join(teile)

    def seite_allgemein(self, cfg, meldung="", fehler=(), neustart=False):
        e = html.escape
        teile = self.kopf("allgemein", "Allgemein", meldung, fehler, neustart)
        if neustart:
            teile.append("</body></html>")
            return "".join(teile)
        with self.g.sperre:
            zuletzt = self.g.letzte_zeichnung
        # Zustand: normal / Zeitplan / Autark
        with self.g.sperre:
            autark, ersatz = self.g.autark, self.g.ersatz
        if not cfg.get("openhab_aktiv", True):
            zustand = "Ohne openHAB: Das Display zeigt nur die Abfahrten der Wiener Linien."
        elif autark:
            zustand = ("<b>Autark-Modus:</b> openHAB ist gerade nicht erreichbar. Das Display zeigt nur die "
                       "Abfahrten und aktualisiert nach dem Zeitplan. Sobald openHAB wieder da ist, geht es "
                       "von selbst normal weiter.")
        elif ersatz:
            zustand = ("Der Bewegungsmelder ist nicht verfügbar (oder nicht gewählt): Das Display aktualisiert "
                       + ("immer." if ersatz == "immer" else "nach dem Zeitplan."))
        else:
            zustand = "Normaler Betrieb: openHAB läuft, das Display aktualisiert, wenn jemand im Vorzimmer ist."
        teile.append(f"<div class='meldung {'fehler' if autark and cfg.get('openhab_aktiv', True) else 'ok'}'>"
                     f"{zustand}</div>")
        teile.append("<form method='post' action='/allgemein/speichern'><fieldset><legend>Allgemein</legend>")
        an = " checked" if cfg.get("openhab_aktiv", True) else ""
        teile.append(f"<div><input type='checkbox' name='openhab_aktiv' id='openhab_aktiv' value='1'{an}> "
                     "<label for='openhab_aktiv' style='display:inline'>openHAB verwenden</label></div>"
                     "<p class='hinweis'>Ohne Hakerl zeigt das Display nur die Abfahrten der Wiener Linien "
                     "(für ein Zuhause ohne openHAB).</p>")
        teile.append(f"<label>openHAB-Adresse</label><input type='text' name='openhab' value='{e(cfg['openhab'])}'>")
        teile.append("<label>Ausrichtung</label>")
        for grad, text in ((0, "normal"), (180, "um 180 Grad gedreht")):
            an = " checked" if cfg["drehung"] == grad else ""
            teile.append(f"<div><input type='radio' name='drehung' id='d{grad}' value='{grad}'{an}> "
                         f"<label for='d{grad}' style='display:inline;font-weight:normal'>{text}</label></div>")
        teile.append(f"<label>Messwerte höchstens alle … Minuten neu zeichnen</label>"
                     f"<input type='number' name='abstand' min='1' max='60' value='{cfg['messwerte_abstand_min']}'>"
                     "<p class='hinweis'>Gilt nur, solange jemand im Vorzimmer ist. Alarme, Fenster und "
                     "Anwesenheit werden immer sofort gezeichnet.</p>"
                     "<label>Display weiß schalten, wenn die Anzeige älter ist als … Minuten</label>"
                     f"<input type='number' name='leeren' min='0' max='1440' value='{cfg.get('leeren_nach_min', 5)}'>"
                     "<p class='hinweis'>Ist niemand im Vorzimmer, wird nichts neu gezeichnet, und Werte und "
                     "Abfahrtszeiten würden veralten. Nach dieser Zeit wird das Display deshalb weiß. Kommt "
                     "jemand, zeichnet es sofort neu. Ein Alarmbild bleibt immer stehen. 0 = nie weiß "
                     "schalten.</p></fieldset>")
        # Wann aktualisieren?
        akt = cfg.get("aktualisieren", {})
        modus, ersatz_w = akt.get("modus", "anwesenheit"), akt.get("ersatz", "zeiten")

        def radio(name, wert, aktuell, text):
            an = " checked" if wert == aktuell else ""
            return (f"<div><input type='radio' name='{name}' id='{name}_{wert}' value='{wert}'{an}> "
                    f"<label for='{name}_{wert}' style='display:inline;font-weight:normal'>{text}</label></div>")
        teile.append("<fieldset><legend>Wann aktualisieren?</legend>"
                     + radio("akt_modus", "anwesenheit", modus, "Wenn jemand im Vorzimmer ist (Bewegungsmelder "
                             "über openHAB)")
                     + radio("akt_modus", "zeiten", modus, "Nach Zeitplan (unten)")
                     + radio("akt_modus", "immer", modus, "Immer")
                     + "<p class='hinweis'>„Immer“ fragt die Wiener Linien den ganzen Tag alle 30 Sekunden ab. "
                       "Außerhalb des Zeitplans wird das Display weiß.</p>"
                     "<label>Wenn der Bewegungsmelder nicht verfügbar ist (openHAB oder CCU aus)</label>"
                     + radio("akt_ersatz", "zeiten", ersatz_w, "Nach Zeitplan")
                     + radio("akt_ersatz", "immer", ersatz_w, "Immer")
                     + "<label>Zeitplan</label><p class='hinweis'>Bis zu vier Zeitfenster. Leere Zeilen werden "
                       "ignoriert. Geht ein Fenster über Mitternacht (z. B. 22:00–01:00), zählt der Rest zum "
                       "Vortag.</p>")
        tage_namen = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
        zeiten = list(akt.get("zeiten", []))[:4]
        zeiten += [{"tage": [], "von": "", "bis": ""}] * (4 - len(zeiten))
        for nr, f in enumerate(zeiten):
            kaestchen = "".join(
                f"<label style='display:inline;font-weight:normal;margin-right:6px'><input type='checkbox' "
                f"name='zt{nr}_tag' value='{t}'{' checked' if t in f.get('tage', []) else ''}> {n}</label>"
                for t, n in enumerate(tage_namen))
            teile.append(f"<div class='karte'><div>{kaestchen}</div><div style='margin-top:6px'>von "
                         f"<input type='time' name='zt{nr}_von' value='{e(f.get('von', ''))}'> bis "
                         f"<input type='time' name='zt{nr}_bis' value='{e(f.get('bis', ''))}'></div></div>")
        teile.append("</fieldset>"
                     "<div class='knoepfe'><button class='haupt'>Speichern und neu starten</button></div></form>")

        teile.append("<fieldset><legend>System</legend>"
                     f"<p>Display zuletzt gezeichnet: {e(zuletzt or '–')}</p>"
                     "<form method='post' action='/neuzeichnen'><button>Display jetzt neu zeichnen</button></form>"
                     "<p></p><form method='post' action='/herunterfahren' onsubmit=\"return confirm("
                     f"'Den Pi jetzt herunterfahren? {aus_rueckfrage(self.prog.hub_da())}')\"><button>Pi herunterfahren</button></form>"
                     f"<p class='hinweis'>Das Display zeigt dann „Ausgeschaltet“. {aus_text(self.prog.hub_da())}</p>"
                     "<form method='post' action='/standard' onsubmit=\"return confirm("
                     "'Alle Einstellungen (alle Reiter) auf den Standard zurücksetzen?')\">"
                     "<button>Alle Einstellungen auf Standard zurücksetzen</button></form>"
                     "</fieldset></body></html>")
        return "".join(teile)

    def seite_wl(self, cfg, meldung="", fehler=(), neustart=False):
        e = html.escape
        teile = self.kopf("wl", "Wiener Linien", meldung, fehler, neustart)
        if neustart:
            teile.append("</body></html>")
            return "".join(teile)
        ab = self.abfahrten_vorschau()
        if ab:
            bilder = "".join(f"<img src='{png_daten(b)}' alt='Vorschau Abfahrten Seite {i}'>"
                             for i, b in enumerate(ab, 1))
            mehr = (f" Es sind {len(ab)} Abfahrten-Seiten mit je {self.prog.PRO_SEITE} Abfahrten; das "
                    "Display zeigt sie nacheinander, dann wieder die Hauptansicht." if len(ab) > 1 else "")
            teile.append(f"<div class='vorschau'>{bilder}"
                         "<p class='hinweis'>So sähe die Abfahrten-Seite jetzt aus. Solange jemand im "
                         f"Vorzimmer ist, wechselt das Display zwischen ihr und der Hauptansicht.{mehr}</p></div>")
        teile.append("<form method='post' action='/wl/speichern'>")
        wl = cfg["wiener_linien"]
        an = " checked" if wl["aktiv"] else ""
        teile.append("<fieldset><legend>Wiener Linien</legend>"
                     f"<div><input type='checkbox' name='wl_aktiv' id='wl_aktiv' value='1'{an}> "
                     "<label for='wl_aktiv' style='display:inline'>Abfahrten anzeigen</label></div>"
                     "<label>Abfahrten anzeigen … Sekunden</label>"
                     f"<input type='number' name='wl_wechsel' min='3' max='120' value='{wl['wechsel_s']}'>"
                     "<p class='hinweis'>Solange jemand im Vorzimmer ist, wechselt das Display zwischen "
                     f"Hauptansicht und Abfahrten. So lange bleibt die Abfahrten-Seite stehen, danach "
                     f"kommt die Hauptansicht für {e(str(cfg.get('anzeige_s', 7)))} Sekunden (einstellbar im "
                     "Reiter openHAB-Display). Groß steht die Abfahrt in Minuten, darunter, wann du "
                     "losgehen musst (Abfahrt minus Gehzeit).</p>")
        lf = " checked" if wl.get("laufen", True) else ""
        teile.append("<div style='margin-top:10px'><input type='checkbox' name='wl_laufen' id='wl_laufen' "
                     f"value='1'{lf}> <label for='wl_laufen' style='display:inline'>Auch Abfahrten zeigen, die "
                     "man nur noch laufend erreicht</label></div>"
                     "<label>Laufen spart bis zu … Minuten</label>"
                     f"<input type='number' name='wl_laufen_min' min='1' max='10' value='{int(wl.get('laufen_min', 2))}'>"
                     "<p class='hinweis'>Beispiel: Gehzeit 5 Minuten, Laufen spart 2. Kommt die Bim in 3 oder "
                     "4 Minuten, steht rechts ein schwarzes Feld mit einem Läufer und der Zeit bis zur Abfahrt in Minuten und Sekunden (z. B. 4:10).</p>")
        rs = " checked" if wl.get("rollstuhl", True) else ""
        km = " checked" if wl.get("klima", True) else ""
        ab = wl.get("klima_ab", 25)
        ab = int(ab) if float(ab).is_integer() else ab
        teile.append("<div style='margin-top:10px'><input type='checkbox' name='wl_rollstuhl' id='wl_rollstuhl' "
                     f"value='1'{rs}> <label for='wl_rollstuhl' style='display:inline'>Rollstuhl-Symbol, wenn "
                     "der Wagen barrierefrei ist</label></div>"
                     "<div><input type='checkbox' name='wl_klima' id='wl_klima' value='1'"
                     f"{km}> <label for='wl_klima' style='display:inline'>Schneeflocke, wenn der Wagen "
                     "klimatisiert ist</label></div>"
                     "<label>… aber nur, wenn es draußen wärmer ist als (°C)</label>"
                     f"<input type='number' name='wl_klima_ab' step='0.5' min='-30' max='50' value='{ab}'>"
                     "<p class='hinweis'>Die Symbole gelten für den Wagen, den du erreichst (bei „fährt in "
                     "X Min“). Die Außentemperatur kommt von Open-Meteo (frei, ohne Schlüssel), auch ohne "
                     "openHAB.</p>")
        for i, ab in enumerate(wl["abfahrten"][:MAX_ABFAHRTEN]):
            daten = {k: ab[k] for k in ("linie", "station", "haltepunkt", "ziel") if k in ab}
            teile.append(
                f"<div class='abfahrt'><b>{e(ab['linie'])}</b> · {e(ab.get('station', ''))} → "
                f"{e(ab.get('ziel', ''))}"
                f"<input type='hidden' name='wl{i}_daten' value='{e(json.dumps(daten, ensure_ascii=False))}'>"
                "<div class='zeile'><div><label>Name am Display</label>"
                f"<input type='text' name='wl{i}_name' value='{e(ab['name'])}'></div><div>"
                "<label>Gehzeit zur Station (Minuten)</label>"
                f"<input type='number' min='0' max='60' name='wl{i}_gehzeit' value='{ab['gehzeit_min']}'>"
                f"</div></div><p class='knoepfe'>"
                + (f"<button formaction='/wl/verschieben' name='schieb' value='{i}:ganzhoch'>⤒ ganz nach oben</button>"
                   if i > 1 else "")
                + (f"<button formaction='/wl/verschieben' name='schieb' value='{i}:hoch'>▲ nach oben</button>"
                   if i > 0 else "")
                + (f"<button formaction='/wl/verschieben' name='schieb' value='{i}:runter'>▼ nach unten</button>"
                   if i < len(wl["abfahrten"]) - 1 else "")
                + f"<button formaction='/entfernen' name='weg' value='{i}' "
                f"onclick=\"return confirm('Abfahrt {e(ab['linie'])} {e(ab['name'])} entfernen?')\">"
                f"Entfernen</button></p></div>")
        if len(wl["abfahrten"]) < MAX_ABFAHRTEN:
            teile.append("<p><a class='knopf' href='/abfahrt'>+ Abfahrt hinzufügen</a></p>"
                         f"<p class='hinweis'>Bis zu {MAX_ABFAHRTEN} Abfahrten. Je {self.prog.PRO_SEITE} stehen auf "
                         "einer Seite des Displays; bei mehr blättert das Display weiter.</p>")
        else:
            teile.append(f"<p class='hinweis'>Höchstens {MAX_ABFAHRTEN} Abfahrten, je {self.prog.PRO_SEITE} auf einer Seite des Displays.</p>")
        teile.append(f"<p class='hinweis'>{e(QUELLE)}</p></fieldset>")
        teile.append("<div class='knoepfe'><button class='haupt'>Speichern und neu starten</button></div>"
                     "</form></body></html>")
        return "".join(teile)

    def seite_symbole(self, cfg):
        """Reiter Hilfe: alles, was das Display zeigen kann, mit Bedeutung."""
        import wetter_symbole as ws
        from PIL import Image, ImageDraw
        e = html.escape

        def gross(bild, rand=4):
            """1-Bit-Bild mit weissem Rand, dreifach vergroessert (Pixel bleiben scharf)."""
            b = bild.convert("1")
            leer = Image.new("1", (b.width + 2 * rand, b.height + 2 * rand), 255)
            leer.paste(b, (rand, rand))
            return leer.resize((leer.width * 3, leer.height * 3), Image.NEAREST)

        def zeile(bild, name, text):
            return (f"<tr><td class='sym'><img src='{png_daten(gross(bild))}' alt='{e(name)}'></td>"
                    f"<td><b>{e(name)}</b><br><span class='hinweis'>{text}</span></td></tr>")

        def tabelle(titel, erklaerung, zeilen):
            return (f"<fieldset><legend>{e(titel)}</legend><p class='hinweis'>{erklaerung}</p>"
                    "<table class='symbole'>" + "".join(zeilen) + "</table></fieldset>")

        teile = self.kopf("symbole", "Hilfe")
        links = []
        for pfad, (datei, name, art, titel, text) in DOWNLOADS.items():
            voll = os.path.join(_ORDNER, datei)
            if os.path.exists(voll):
                endung = "PDF" if art == "application/pdf" else "Archiv"
                links.append(f"<p style='margin:8px 0 0'><a href='{pfad}' download='{name}'><b>{e(titel)} "
                             f"herunterladen</b></a> <span class='hinweis'>({endung}, "
                             f"{os.path.getsize(voll) // 1024} KB)</span></p><p class='hinweis'>{e(text)}</p>")
        if links:
            teile.append("<fieldset><legend>Anleitungen und Downloads</legend>" + "".join(links) + "</fieldset>")
        teile.append("<p class='hinweis'>Alles, was das Display anzeigen kann, und was es bedeutet. "
                     "Die Bilder sind die echten Display-Pixel, dreifach vergrößert.</p>")

        namen = {"klar": "Klar", "wenig": "Leicht bewölkt", "maessig": "Mäßig bewölkt",
                 "ueberwiegend": "Überwiegend bewölkt", "bedeckt": "Bedeckt", "niesel": "Niesel",
                 "regen": "Regen", "starkregen": "Starkregen", "schauer": "Schauer", "gewitter": "Gewitter",
                 "schnee": "Schnee", "schneeregen": "Schneeregen, Graupel, Eisregen", "nebel": "Nebel, Dunst",
                 "sturm": "Sturm", "unbekannt": "Unbekannt"}
        teile.append(tabelle(
            "Wetter heute (Fußzeile links)",
            "Aus der Tages-Wetterlage von OpenWeatherMap. Daneben stehen der Text (abschaltbar) und "
            "Tiefst- bis Höchsttemperatur.",
            [zeile(ws.symbol(k), v, "OpenWeatherMap-Text: " + e(ws.BEISPIELE.get(k, "")))
             for k, v in namen.items()]))

        uv = cfg["uv"]
        teile.append(tabelle(
            "Zusätzliche Wetter-Hinweise (Fußzeile)",
            "Erscheinen neben oder statt dem Wettersymbol.",
            [zeile(ws.symbol("schirm"), "Schirm (weiß auf schwarz)",
                   "Statt des Wettersymbols, wenn es <b>jetzt regnet</b> und <b>heute weiter Regen</b> "
                   "angesagt ist (Regen-Wetterlage oder mindestens 1 mm). Also: Schirm mitnehmen."),
             zeile(ws.symbol("sonnenbrille"), "Sonnenbrille",
                   f"Zusätzlich, wenn der UV-Index heute <b>{e(str(uv['grenze']))} oder mehr</b> erreicht "
                   "(Open-Meteo, stündlich). Also: Sonnenschutz, auch Sonnencreme."
                   + ("" if uv["aktiv"] else " <i>Derzeit ausgeschaltet.</i>")),
             zeile(ws.symbol("glatteis"), "Glatteis (Warndreieck mit Schneeflocke)",
                   "Zusätzlich, wenn es <b>kalt</b> ist (jetzt höchstens 1° oder heute Frost) und <b>nass</b> "
                   "(Regen, Schnee, Graupel oder Luftfeuchte ab 90 %). Bei Eisregen immer."),
             zeile(ws.symbol("untergang"), "Sonnenuntergang (Fußzeile rechts)",
                   "Uhrzeit des Sonnenuntergangs heute (openHAB, Astro).")]))

        kl = cfg["kleidung"]
        g = kl["grenzen"]
        teile.append(tabelle(
            "Was anziehen? (Feld Außentemperatur, unten rechts)",
            "Nach der <b>gefühlten</b> Temperatur, die auch in der Überschrift des Feldes steht."
            + ("" if kl["aktiv"] else " <i>Derzeit ausgeschaltet.</i>"),
            [zeile(ws.kleidung("tshirt"), "T-Shirt", f"gefühlt ab {g[0]}°"),
             zeile(ws.kleidung("pullover"), "Pullover", f"gefühlt ab {g[1]}°"),
             zeile(ws.kleidung("jacke"), "Jacke", f"gefühlt ab {g[2]}°"),
             zeile(ws.kleidung("winterjacke"), "Winterjacke (schwarz)", f"gefühlt unter {g[2]}°")]))

        # Balken "Strom heute" nachzeichnen
        balken = Image.new("1", (48, 12), 255)
        d = ImageDraw.Draw(balken)
        x0, x1, y0 = 1, 46, 4
        d.rectangle((x0, y0, x1, y0 + 4), outline=0)
        d.rectangle((x0, y0, x0 + int((x1 - x0) * 0.65), y0 + 4), fill=0)
        m = x0 + (x1 - x0) // 2
        d.line((m, y0 - 2, m, y0 + 6), fill=0, width=2)
        sh, sv = cfg["strom_hoch"], cfg["strom_vergleich"]
        teile.append(tabelle(
            "Strom und Haushalt",
            "",
            [zeile(ws.symbol("blitz"), "Blitz (Feld „Strom jetzt“)",
                   f"Der Verbrauch liegt jetzt bei <b>{e(str(sh['grenze']))} W oder mehr</b>."
                   + ("" if sh["aktiv"] else " <i>Derzeit ausgeschaltet.</i>")),
             zeile(balken, "Balken (Feld „Strom heute“)",
                   "Vergleich mit einem üblichen Tag: Der <b>Strich in der Mitte</b> ist, was an einem "
                   f"üblichen Tag bis zur jetzigen Uhrzeit verbraucht ist (Median der letzten {e(str(sv['tage']))} "
                   "Tage). Balken über den Strich hinaus = mehr als sonst, voller Balken = doppelt so viel."
                   + ("" if sv["aktiv"] else " <i>Derzeit ausgeschaltet.</i>")),
             zeile(ws.symbol("waesche"), "Waschmaschine läuft",
                   "In der Fußzeile statt des Wettertexts, mit der Laufzeit („Laufzeit 1 Std 20 Min“)."),
             zeile(ws.symbol("waesche_fertig"), "Wäsche fertig",
                   "Fußzeile schwarz: „Wäsche fertig · Uhrzeit (Dauer)“. Verschwindet nach einem "
                   "Besuch im Bad, beim nächsten Waschgang oder wenn die Steckdose ausgeht.")]))

        wl = cfg["wiener_linien"]
        teile.append(tabelle(
            "Abfahrten",
            "Rollstuhl und Schneeflocke stehen hinter „fährt in X Min“ und gelten für den Wagen, den du "
            "noch erreichst. Angaben der Wiener Linien in Echtzeit.",
            [zeile(ws.laeufer(), "Läufer (weiß auf schwarz, rechts)",
                   "Mit normalem Gehen ist diese Abfahrt nicht mehr zu schaffen, laufend schon "
                   f"(Laufen spart bis zu {e(str(wl.get('laufen_min', 2)))} Minuten). Daneben steht, wann sie "
                   "abfährt, in Minuten und Sekunden (4:10 = in 4 Minuten 10 Sekunden, in 10-Sekunden-Schritten "
                   "abgerundet; gilt beim Anzeigen der Seite)." + ("" if wl.get("laufen", True) else " <i>Derzeit ausgeschaltet.</i>")),
             zeile(ws.wagen_symbol("rollstuhl"), "Rollstuhl",
                   "Der Wagen ist <b>barrierefrei</b> (Niederflur). Steht nichts da, hat er Stufen."
                   + ("" if wl.get("rollstuhl", True) else " <i>Derzeit ausgeschaltet.</i>")),
             zeile(ws.wagen_symbol("klima"), "Schneeflocke",
                   f"Der Wagen ist <b>klimatisiert</b>. Nur, wenn es draußen wärmer als "
                   f"<b>{e(str(wl.get('klima_ab', 25)))}°</b> ist (Open-Meteo), sonst ist es nicht wichtig."
                   + ("" if wl.get("klima", True) else " <i>Derzeit ausgeschaltet.</i>"))]))

        teile.append("<fieldset><legend>Weitere Anzeigen (ohne Symbol)</legend><table class='symbole'>"
                     + "".join(f"<tr><td><b>{e(a)}</b></td><td class='hinweis'>{b}</td></tr>" for a, b in [
                         ("Kopfzeile links", "Datum. Dazu „ABWESEND“ (niemand da), „Anwesend“ (nur du) oder "
                          "„Gäste (n)“; bei scharfer Alarmanlage „· SCHARF“."),
                         ("Kopfzeile rechts", "Uhrzeit, zu der das Bild gezeichnet wurde."),
                         ("Fußzeile schwarz", "In dieser Reihenfolge: keine Verbindung zu openHAB, Fenster offen "
                          "(mit Namen), gespeicherte Alarme, Wäsche fertig. Danach Wäsche läuft, "
                          "Anwesenheitssimulation, sonst das Wetter."),
                         ("Vollbild-Alarm", "Alarmanlage, stiller Alarm, Wassermelder, Rauchmelder, Kellertür "
                          "offen. Wird sofort gezeichnet und hat Vorrang vor allem."),
                         ("Abfahrten-Seite", "Wechselt mit der Hauptansicht, solange jemand im Vorzimmer ist. "
                          "Links die Linie, rechts groß „los in X′“ (Abfahrt minus Gehzeit), bei 0 „LOS!“, "
                          "nur noch laufend erreichbar: Läufer und Abfahrt in Minuten und Sekunden (4:10). "
                          "20 Sekunden nachdem niemand mehr da ist, wird sie weiß."),
                         ("Weißes Display", "Niemand im Vorzimmer und das Bild ist älter als die eingestellte "
                          "Zeit (Reiter Allgemein). So bleiben keine alten Werte stehen."),
                         ("WLAN-Startbild", "Eine Minute nach dem Hochfahren: WLAN, Signal und die Adresse "
                          "dieser Seite, dazu ein QR-Code mit der Adresse zum Scannen mit dem Handy."),
                         ("Hotspot „" + cfg["wlan"]["hotspot_ssid"] + "“", "Kein WLAN: Name und Passwort "
                          "des Hotspots, die Adresse für die Einrichtung (Reiter WLAN) und ein QR-Code. "
                          "Mit der Handy-Kamera scannen: Das Handy verbindet sich mit dem Hotspot und "
                          "öffnet die Einrichtung von selbst."),
                         ("Ausgeschaltet", "Nach „Pi herunterfahren“, mit Hinweis zum Aus- und Einschalten."),
                     ]) + "</table></fieldset>")
        teile.append("<fieldset><legend>Fragen und Hilfe</legend>"
                     + (f"<p>Bei Fragen oder Problemen: <a href='mailto:{KONTAKT}'>{KONTAKT}</a></p>"
                        if KONTAKT else "")
                     + "<p class='hinweis'>© 2026 Markus Nepp · Lizenz: MIT (siehe unten)</p></fieldset>")

        # Lizenz (Wunsch des Nutzers): eigener Code MIT, dazu Fremdteile und Datenquellen
        try:
            with open(os.path.join(_ORDNER, "LICENSE"), encoding="utf-8") as f:
                lizenz = f.read()
        except OSError:
            lizenz = "MIT License, Copyright (c) 2026 Markus Nepp"
        teile.append(
            "<fieldset><legend>Lizenz</legend>"
            "<p>Das Home Display ist freie Software unter der <b>MIT-Lizenz</b>: Jeder darf es verwenden, "
            "verändern und weitergeben, auch kommerziell, solange der Copyright-Hinweis und der Lizenztext "
            "dabei bleiben. Es gibt keine Gewährleistung.</p>"
            "<p><b>Fremdteile und Datenquellen</b></p><ul class='hinweis'>"
            "<li>Display-Treiber von Waveshare (MIT-Lizenz), leicht geändert</li>"
            "<li>Abfahrtsdaten: Wiener Linien, Open Government Data (CC BY 4.0), data.wien.gv.at</li>"
            "<li>UV-Index und Temperatur: Open-Meteo.com (CC BY 4.0)</li>"
            "<li>Python-Bibliotheken aus Raspberry Pi OS: Pillow, qrcode, spidev, lgpio; "
            "Schrift DejaVu Sans – jeweils unter ihren eigenen freien Lizenzen</li></ul>"
            f"<details><summary>Lizenztext</summary><pre style='white-space:pre-wrap;font-size:12px'>"
            f"{e(lizenz)}</pre></details></fieldset></body></html>")
        return "".join(teile)

    def seite_wlan(self, cfg, meldung="", fehler=(), neustart=False, gesucht=False):
        """Reiter WLAN: Zustand, WLANs in der Naehe, verbinden, Hotspot-Einstellungen."""
        e = html.escape
        teile = self.kopf("wlan", "WLAN", meldung, fehler, neustart)
        if neustart:
            teile.append("</body></html>")
            return "".join(teile)
        w = self.g.wlan
        if w is None:
            teile.append("<p>WLAN-Einrichtung läuft nicht.</p></body></html>")
            return "".join(teile)
        z = w.zustand()
        teile.append("<fieldset><legend>Zustand</legend>")
        if z["modus"] == "hotspot":
            teile.append(f"<p><b>Kein WLAN – der Pi hat einen eigenen Hotspot geöffnet</b> "
                         f"(„{e(z['hotspot_ssid'])}“, Adresse {e(z['hotspot_ip'])}).</p>"
                         "<p class='hinweis'>Wähle unten dein WLAN und gib das Passwort ein. Solange niemand "
                         "mit dem Hotspot verbunden ist, versucht der Pi zwischendurch selbst, wieder in ein "
                         "bekanntes WLAN zu kommen.</p>")
        elif z["modus"] == "verbinde":
            teile.append(f"<p><b>{e(z['meldung'])}</b></p>")
        else:
            info = self.prog.wlan_info() or {}
            sig = ""
            if info.get("prozent") is not None:
                sig = f", Signal {info['prozent']} %"
                if info.get("dbm") is not None:
                    sig += f" ({info['dbm']} dBm)"
            teile.append(f"<p>Verbunden mit <b>{e(info.get('ssid') or z['ssid'] or '?')}</b>{e(sig)}, "
                         f"Adresse {e(info.get('ip') or '?')}.</p>")
        if z["meldung"] and z["modus"] != "verbinde":
            teile.append(f"<p class='hinweis'>Zuletzt: {e(z['meldung'])}</p>")
        # Hotspot von Hand (Wunsch des Nutzers: zum Ausprobieren, und um nicht festzusitzen)
        if z["modus"] == "hotspot":
            teile.append("<form method='post' action='/wlan/hotspot_aus'><button>Hotspot beenden, zurück "
                         "ins WLAN</button></form><p class='hinweis'>Der Pi sucht ein bekanntes WLAN in "
                         "Reichweite und verbindet sich. Findet er keins, bleibt der Hotspot offen.</p>")
        elif z["modus"] == "wlan":
            minuten = max(1, int(cfg["wlan"]["erneut_s"]) // 60)
            frage = ("Hotspot jetzt öffnen? Der Pi verlässt dafür dein WLAN, diese Seite ist dann nicht mehr "
                     f"erreichbar. Ist nach {minuten} Minuten kein Gerät am Hotspot, kehrt er von selbst zurück.")
            teile.append(f"<form method='post' action='/wlan/hotspot_an' onsubmit=\"return confirm('{e(frage)}')\">"
                         "<button>Hotspot jetzt öffnen (zum Testen)</button></form>"
                         "<p class='hinweis'>Zum Ausprobieren der Einrichtung mit dem Handy: Das Display zeigt "
                         "dann den QR-Code des Hotspots. Zurück geht es über „Hotspot beenden“ auf der "
                         f"Einrichtungsseite, mit einem neuen WLAN oder nach {minuten} Minuten ohne verbundenes "
                         "Gerät von selbst.</p>")
        teile.append("</fieldset>")

        netze = w.netze
        teile.append("<form method='post' action='/wlan/verbinden'><fieldset><legend>WLAN wählen</legend>")
        if netze:
            for i, (ssid, signal, sicherheit) in enumerate(netze):
                aktiv = " checked" if ssid == (z["ssid"] or "") else ""
                schloss = "" if sicherheit.strip() in ("", "--") else " 🔒"
                teile.append(f"<div><input type='radio' name='ssid' id='n{i}' value='{e(ssid)}'{aktiv}> "
                             f"<label for='n{i}' style='display:inline'>{e(ssid)}{schloss} "
                             f"<span class='hinweis'>({signal} %)</span></label></div>")
        else:
            teile.append("<p>Noch keine WLANs gesucht.</p>")
        teile.append("<label>Oder Name eines versteckten WLANs</label>"
                     "<input type='text' name='ssid_frei' autocomplete='off'>"
                     "<label>Passwort</label>"
                     "<input type='password' name='passwort' autocomplete='new-password'>"
                     "<p class='hinweis'>Ist das WLAN schon bekannt und das Passwort gleich, kannst du das Feld "
                     "leer lassen. Beim Verbinden reißt die Verbindung zu dieser Seite ab. Klappt es, zeigt das "
                     "Display eine Minute lang die neue Adresse. Klappt es nicht, bleibt alles wie vorher "
                     "(bzw. der Hotspot geht wieder an).</p>"
                     "<div class='knoepfe'><button class='haupt'>Verbinden</button></div></fieldset></form>"
                     "<form method='post' action='/wlan/suchen'><button>WLANs neu suchen</button> "
                     "<span class='hinweis'>(dauert rund 10 Sekunden)</span></form><p></p>")

        wc = cfg["wlan"]
        an = " checked" if wc["hotspot"] else ""
        teile.append("<form method='post' action='/wlan/speichern'><fieldset><legend>Hotspot</legend>"
                     f"<div><input type='checkbox' name='hotspot' id='hotspot' value='1'{an}> "
                     "<label for='hotspot' style='display:inline'>Hotspot öffnen, wenn kein WLAN da ist</label></div>"
                     "<label>Name des Hotspots</label>"
                     f"<input type='text' name='hotspot_ssid' maxlength='32' value='{e(wc['hotspot_ssid'])}'>"
                     "<label>Hotspot nach … Minuten ohne WLAN</label>"
                     f"<input type='number' name='ohne_wlan_min' min='1' max='60' value='{int(wc['ohne_wlan_s']) // 60}'>"
                     f"<p class='hinweis'>Passwort des Hotspots: <code>{e(self.prog_hotspot_passwort())}</code> "
                     "(steht auch am Display, solange der Hotspot offen ist).</p>"
                     "<div class='knoepfe'><button class='haupt'>Speichern und neu starten</button></div>"
                     "</fieldset></form></body></html>")
        return "".join(teile)

    @staticmethod
    def prog_hotspot_passwort():
        import wlan_einrichtung
        return wlan_einrichtung.hotspot_passwort()

    def _wlan_aus_formular(self, f, t, cfg, fehler):
        wc = cfg["wlan"]
        wc["hotspot"] = t("hotspot") == "1"
        name = t("hotspot_ssid")
        if not name or len(name.encode("utf-8")) > 32:
            fehler.append("Der Name des Hotspots muss 1 bis 32 Zeichen lang sein.")
        else:
            wc["hotspot_ssid"] = name
        try:
            wc["ohne_wlan_s"] = min(60, max(1, int(t("ohne_wlan_min")))) * 60
        except ValueError:
            fehler.append("Die Minuten bis zum Hotspot müssen eine ganze Zahl sein.")

    def seite_bluetooth(self, cfg, meldung="", fehler=(), neustart=False, gefunden=None, test=None):
        e = html.escape
        teile = self.kopf("bt", "Bluetooth", meldung, fehler, neustart)
        if neustart:
            teile.append("</body></html>")
            return "".join(teile)
        bt = cfg["bluetooth"]
        items = self.items() or {}

        # Status
        n = self.g.naehe
        teile.append("<fieldset><legend>Status</legend>")
        if n is None:
            teile.append("<p class='meldung fehler'>Die Handy-Erkennung ist <b>aus</b>. Der Pi fragt das Handy "
                         "nicht ab, und das openHAB-Item behält seinen letzten Wert. Zum Einschalten unten bei "
                         "„Einstellungen“ den Haken „Handy-Erkennung einschalten“ setzen und speichern.</p>")
        elif n.pausiert:
            teile.append("<p>Handy-Suche <b>pausiert</b>: Du bist zu Hause (<code>"
                         + e(cfg["anwesenheit"]["ich"]) + "</code> = ON). Der Pi sucht erst wieder, wenn du "
                         "weg bist, und meldet solange „weg“ (OFF).</p>")
        else:
            zustand = {True: "<b>in der Nähe</b>", False: "<b>weg</b>", None: "wird gerade geprüft …"}[n.status]
            gesehen = time.strftime("%d.%m. %H:%M:%S", time.localtime(n.zuletzt_gesehen)) \
                if n.zuletzt_gesehen else "noch nie (seit dem letzten Start)"
            teile.append(f"<p>Handy {e(n.adresse)}{' („' + e(n.name) + '“)' if n.name else ''}: {zustand}<br>"
                         f"Zuletzt erkannt: {e(gesehen)}")
            if n.mindestpegel is not None:
                gemessen = (f"{n.pegel} (um {time.strftime('%H:%M:%S', time.localtime(n.pegel_um))})"
                            if n.pegel is not None else "noch keine Messung")
                teile.append(f"<br>Signalpegel zuletzt: <b>{e(gemessen)}</b>, Mindestpegel: {n.mindestpegel}")
            teile.append("</p>")
        i = items.get(bt["item"]) if bt["item"] else None
        if i:
            teile.append(f"<p class='hinweis'>openHAB-Item <code>{e(bt['item'])}</code>: "
                         f"<span class='wert'>{e(str(i.get('state')))}</span></p>")
        teile.append("<p class='hinweis'>Der Pi setzt nur den Zustand dieses Items (ON = in der Nähe, "
                     "OFF = weg). Was daraus folgt, zum Beispiel das Aufsperren der Tür, regelst du in "
                     "openHAB. Tipp für die Regel: zusätzlich prüfen, dass an der Wohnungstür geklingelt wurde "
                     "und du gerade heimkommst (vorher weg). Sonst geht die Tür auch auf, wenn du daheim bist "
                     "und ein Fremder klingelt.</p></fieldset>")

        # Suche
        teile.append("<fieldset><legend>Handy suchen</legend>"
                     "<p>Öffne am iPhone <b>Einstellungen → Bluetooth</b> und lass die Seite offen. "
                     "Nur dann ist das iPhone sichtbar. Dann hier auf „Suchen“ drücken. Die Suche dauert "
                     "rund 15 Sekunden.</p>"
                     "<form method='post' action='/bt/suchen'><button>Suchen</button></form>")
        if gefunden is not None:
            if not gefunden:
                teile.append("<p class='meldung fehler'>Kein sichtbares Gerät gefunden. Ist am iPhone "
                             "Einstellungen → Bluetooth offen?</p>")
            for adresse, name in gefunden:
                js = (f"document.getElementById('bt_adresse').value='{adresse}';"
                      f"document.getElementById('bt_geraetename').value={json.dumps(name)};"
                      "document.getElementById('bt_adresse').scrollIntoView();return false;")
                teile.append(f"<div class='abfahrt'><b>{e(name or 'Name unbekannt')}</b> · {e(adresse)} "
                             f"<button onclick=\"{e(js)}\">Übernehmen</button></div>")
        teile.append("</fieldset>")

        # Einstellungen
        an = " checked" if bt["aktiv"] else ""
        weg = " checked" if bt.get("nur_wenn_abwesend", True) else ""
        teile.append("<form method='post' action='/bt/speichern'><fieldset><legend>Einstellungen</legend>"
                     f"<div><input type='checkbox' name='bt_aktiv' id='bt_aktiv' value='1'{an}> "
                     "<label for='bt_aktiv' style='display:inline'>Handy-Erkennung einschalten</label></div>"
                     f"<div><input type='checkbox' name='bt_nur_weg' id='bt_nur_weg' value='1'{weg}> "
                     "<label for='bt_nur_weg' style='display:inline'>Nur suchen, wenn ich nicht zu Hause bin "
                     f"(<code>{e(cfg['anwesenheit']['ich'])}</code> = OFF), spart Strom und CPU</label></div>"
                     "<label>Bluetooth-Adresse des Handys</label>"
                     f"<input type='text' id='bt_adresse' name='bt_adresse' value='{e(bt['adresse'])}' "
                     "placeholder='AA:BB:CC:11:22:33'>"
                     f"<input type='hidden' id='bt_geraetename' name='bt_geraetename' value='{e(bt['geraetename'])}'>"
                     "<p class='hinweis'>Steht am iPhone auch unter Einstellungen → Allgemein → Info → Bluetooth.</p>")
        if test is not None:
            name, gemessen = test
            if name or gemessen is not None:
                text = "Das Handy hat geantwortet" + (f": „{name}“" if name else "") + "."
                text += (f" Signalpegel jetzt: {gemessen}." if gemessen is not None
                         else " Den Signalpegel konnte der Pi nicht messen.")
                klasse = "ok"
            else:
                klasse, text = "fehler", "Keine Antwort. Ist das Handy in der Nähe und Bluetooth eingeschaltet?"
            teile.append(f"<div class='meldung {klasse}'>{e(text)}</div>")
        pegel_an = " checked" if bt.get("mindestpegel") is not None else ""
        pegel_wert = bt.get("mindestpegel") if bt.get("mindestpegel") is not None else -5
        teile.append("<p><button formaction='/bt/testen'>Jetzt prüfen, ob das Handy antwortet</button></p>"
                     "<label>openHAB-Item (Schalter)</label>"
                     f"<input type='text' list='items' name='bt_item' value='{e(bt['item'])}'>"
                     "<div class='zeile'><div><label>Abfrage alle … Sekunden</label>"
                     f"<input type='number' name='bt_intervall' min='5' max='120' value='{bt['intervall_s']}'></div>"
                     "<div><label>„weg“ nach … Sekunden ohne Antwort</label>"
                     f"<input type='number' name='bt_weg' min='5' max='3600' value='{bt['weg_nach_s']}'></div></div>"
                     f"<div style='margin-top:12px'><input type='checkbox' name='bt_pegel_an' id='bt_pegel_an' "
                     f"value='1'{pegel_an}> <label for='bt_pegel_an' style='display:inline'>Mindest-Signalpegel "
                     "verwenden</label></div>"
                     "<label>Mindest-Signalpegel</label>"
                     f"<input type='number' name='bt_pegel' min='-40' max='0' value='{pegel_wert}'>"
                     "<p class='hinweis'>Das Handy zählt nur als „in der Nähe“, wenn sein Signal mindestens so "
                     "stark ist. 0 = sehr gutes Signal (nah), negative Zahlen = schwächer (weiter weg). Am besten "
                     "einmal vor der Tür und einmal drinnen „Jetzt prüfen“ drücken und den Wert dazwischen "
                     "wählen.</p>"
                     "</fieldset><div class='knoepfe'><button class='haupt'>Speichern und neu starten</button>"
                     "</div></form>")
        if items:
            teile.append("<datalist id='items'>" + "".join(
                f"<option value='{e(nm)}'>{e(it.get('label') or '')}</option>"
                for nm, it in sorted(items.items()) if it.get("type") == "Switch") + "</datalist>")
        teile.append("</body></html>")
        return "".join(teile)

    def abfahrt_seite(self, linie="", station="", fehler=""):
        """Neue Abfahrt in drei Schritten: Linie, Station, Richtung."""
        e = html.escape
        t = self.kopf("wl", "Abfahrt hinzufügen")
        t.append("<h2>Abfahrt hinzufügen</h2>")
        if fehler:
            t.append(f"<div class='meldung fehler'>{e(fehler)}</div>")
        try:
            index = self.wl.index()
        except Exception as x:
            t.append(f"<div class='meldung fehler'>Liniendaten der Wiener Linien nicht abrufbar: {e(str(x))}</div>"
                     "<p><a class='knopf' href='/wl'>Zurück</a></p></body></html>")
            return "".join(t)

        l = self.wl.linie(linie) if linie else None
        s = None
        if l and station:
            s = next((x for x in l["stationen"] if x["name"] == station), None)

        # Schritt 1: Linie
        t.append("<form method='get' action='/abfahrt'><fieldset><legend>1. Linie</legend><select name='linie' "
                 "onchange='this.form.submit()'><option value=''>– Linie wählen –</option>")
        gruppe = None
        for x in index["linien"]:
            typ = self.wlmod.TYPEN.get(x["typ"], x["typ"])
            if typ != gruppe:
                t.append(("</optgroup>" if gruppe else "") + f"<optgroup label='{e(typ)}'>")
                gruppe = typ
            gewaehlt = " selected" if l and x["name"] == l["name"] else ""
            t.append(f"<option value='{e(x['name'])}'{gewaehlt}>{e(x['name'])}</option>")
        t.append("</optgroup></select><noscript><button>Weiter</button></noscript></fieldset></form>")

        # Schritt 2: Station
        if l:
            t.append("<form method='get' action='/abfahrt'><fieldset><legend>2. Station</legend>"
                     f"<input type='hidden' name='linie' value='{e(l['name'])}'>"
                     "<select name='station' onchange='this.form.submit()'><option value=''>– Station wählen –</option>")
            for x in l["stationen"]:
                gewaehlt = " selected" if s and x["name"] == s["name"] else ""
                t.append(f"<option value='{e(x['name'])}'{gewaehlt}>{e(x['name'])}</option>")
            t.append("</select><p class='hinweis'>In Fahrtreihenfolge.</p>"
                     "<noscript><button>Weiter</button></noscript></fieldset></form>")

        # Schritt 3: Richtung, Name, Gehzeit
        if s:
            eintraege = [{"haltepunkt": r["haltepunkt"], "linie": l["name"]} for r in s["richtungen"]]
            try:
                live = self.wl.abfahrten(eintraege)
            except Exception:
                live = {}
            jetzt = time.time()
            t.append("<form method='post' action='/abfahrt'><fieldset><legend>3. Richtung</legend>"
                     f"<input type='hidden' name='linie' value='{e(l['name'])}'>"
                     f"<input type='hidden' name='station' value='{e(s['name'])}'>")
            meiste = s["richtungen"][0]["anzahl"]
            for k, r in enumerate(s["richtungen"]):
                zeiten = [int((z - jetzt) // 60) for z in live.get(f"{r['haltepunkt']}|{l['name']}", [])]
                zeiten = [m for m in zeiten if m >= 0][:3]
                naechste = ("nächste in " + ", ".join(str(m) for m in zeiten) + " Min") if zeiten else "gerade keine Abfahrt"
                selten = " (selten)" if r["anzahl"] < meiste / 4 else ""
                an = " checked" if k == 0 else ""
                t.append(f"<label class='richtung'><input type='radio' name='richtung' value='{r['haltepunkt']}|"
                         f"{e(r['ziel'])}'{an}> Richtung <b>{e(r['ziel'])}</b>{selten} "
                         f"<span class='hinweis'>– {e(naechste)}</span></label>")
            t.append("<label>Name am Display (leer = automatisch, z. B. „"
                     f"{e(self.wlmod.kurzname(s['richtungen'][0]['ziel']))}“)</label>"
                     "<input type='text' name='name' maxlength='30'>"
                     "<label>Gehzeit zur Station (Minuten)</label>"
                     "<input type='number' name='gehzeit' min='0' max='60' value='5'>"
                     "<p class='hinweis'>Abfahrten, die du mit dieser Gehzeit nicht mehr erreichst, "
                     "zeigt das Display nicht an.</p></fieldset>"
                     "<div class='knoepfe'><button class='haupt'>Hinzufügen und neu starten</button></div></form>")
        t.append(f"<p><a class='knopf' href='/wl'>Zurück</a></p><p class='hinweis'>{e(QUELLE)}</p></body></html>")
        return "".join(t)

    def abfahrt_hinzufuegen(self, f):
        """Liefert (Einstellungen, Fehlertext)."""
        def wert(name):
            return f.get(name, [""])[0].strip()
        cfg = self.prog.config_laden()
        if len(cfg["wiener_linien"]["abfahrten"]) >= MAX_ABFAHRTEN:
            return cfg, f"Es sind schon {MAX_ABFAHRTEN} Abfahrten eingetragen. Bitte zuerst eine entfernen."
        try:
            punkt, ziel = wert("richtung").split("|", 1)
            punkt = int(punkt)
            gehzeit = min(60, max(0, int(wert("gehzeit") or 0)))
        except ValueError:
            return cfg, "Bitte eine Richtung wählen und die Gehzeit als ganze Zahl eintragen."
        l = self.wl.linie(wert("linie"))
        s = next((x for x in l["stationen"] if x["name"] == wert("station")), None) if l else None
        if not s or punkt not in [r["haltepunkt"] for r in s["richtungen"]]:
            return cfg, "Diese Kombination aus Linie, Station und Richtung gibt es nicht."
        cfg["wiener_linien"]["abfahrten"].append({
            "linie": l["name"], "station": s["name"], "haltepunkt": punkt, "ziel": ziel,
            "name": wert("name") or self.wlmod.kurzname(ziel), "gehzeit_min": gehzeit})
        cfg["wiener_linien"]["aktiv"] = True
        return cfg, ""

    def herunterfahren(self):
        """Display auf 'Ausgeschaltet' zeichnen, dann den Pi herunterfahren."""
        self.g.aus = True  # Hauptschleife zeichnet ab jetzt nichts mehr

        def ende():
            time.sleep(1)
            try:
                self.display.zeigen(self.prog.aus_bild(), True, self.g.cfg["drehung"])
            except Exception:
                log.exception("Abschaltbild nicht gezeichnet")
            item = self.g.cfg["bluetooth"]["item"]
            if item:  # ohne laufenden Pi weiss niemand, ob das Handy da ist
                try:
                    self.prog.zustand_setzen(item, "OFF")
                except Exception as e:
                    log.warning("Handy-Zustand nicht auf OFF gesetzt: %s", e)
            with self.display.sperre:  # nicht mitten im Zeichnen abschalten
                log.info("Herunterfahren über die Web-Oberfläche")
                subprocess.run(["sudo", "-n", "systemctl", "poweroff"])
        threading.Thread(target=ende, daemon=True).start()

    def neu_starten(self):
        """Programm beenden, systemd startet es mit den neuen Einstellungen wieder."""
        def ende():
            time.sleep(1)
            with self.display.sperre:  # nicht mitten im Zeichnen beenden
                log.info("Einstellungen geändert, starte neu")
                os._exit(0)
        threading.Thread(target=ende, daemon=True).start()


def starten(port, g, warteschlange, display, prog):
    web = Web(g, warteschlange, display, prog)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # keine Zeile pro Seitenaufruf im Log

        def senden(self, code, inhalt, art="text/html; charset=utf-8"):
            daten = inhalt.encode("utf-8") if isinstance(inhalt, str) else inhalt
            self.send_response(code)
            self.send_header("Content-Type", art)
            self.send_header("Content-Length", str(len(daten)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(daten)

        def weiter(self, ziel="/"):
            self.send_response(303)
            self.send_header("Location", ziel)
            self.end_headers()

        def do_GET(self):
            pfad = urllib.parse.urlparse(self.path).path
            wz = g.wlan.zustand() if g.wlan else None
            if wz and wz["modus"] == "hotspot" and pfad not in ("/wlan", "/vorschau.png") and pfad not in DOWNLOADS:
                # Ohne WLAN gibt es nur die Einrichtung (kein openHAB). Das gilt auch fuer die
                # Internet-Pruefung der Handys (captive.apple.com, generate_204 ...): Im Hotspot
                # fuehrt jede Adresse zum Pi (dnsmasq), Port 80 hoert mit, und die Weiterleitung
                # laesst das Handy die Einrichtung als Anmeldeseite von selbst oeffnen.
                self.weiter(f"http://{wz.get('hotspot_ip', '10.42.0.1')}:{port}/wlan")
                return
            if pfad in DOWNLOADS:
                datei, name, art, _, _ = DOWNLOADS[pfad]
                try:
                    with open(os.path.join(_ORDNER, datei), "rb") as f:
                        daten = f.read()
                except OSError:
                    self.senden(404, "Datei nicht auf dem Pi", "text/plain; charset=utf-8")
                    return
                self.send_response(200)
                self.send_header("Content-Type", art)
                weise = "inline" if art == "application/pdf" else "attachment"
                self.send_header("Content-Disposition", f'{weise}; filename="{name}"')
                self.send_header("Content-Length", str(len(daten)))
                self.end_headers()
                self.wfile.write(daten)
            elif pfad in ("/hilfe", "/symbole"):
                self.senden(200, web.seite_symbole(prog.config_laden()))
            elif pfad == "/wlan":
                if g.wlan and not g.wlan.netze:
                    g.wlan.suchen()
                self.senden(200, web.seite_wlan(prog.config_laden()))
            elif pfad == "/":
                self.senden(200, web.seite(prog.config_laden(), reiter="allgemein"))
            elif pfad == "/display":
                self.senden(200, web.seite(prog.config_laden()))
            elif pfad == "/wl":
                self.senden(200, web.seite(prog.config_laden(), reiter="wl"))
            elif pfad == "/bluetooth":
                self.senden(200, web.seite(prog.config_laden(), reiter="bt"))
            elif pfad == "/abfahrt":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                self.senden(200, web.abfahrt_seite(q.get("linie", [""])[0], q.get("station", [""])[0]))
            elif pfad == "/vorschau.png":
                puffer = io.BytesIO()
                g.vorschau().save(puffer, "PNG")
                self.senden(200, puffer.getvalue(), "image/png")
            else:
                self.senden(404, "Nicht gefunden", "text/plain; charset=utf-8")

        def do_POST(self):
            pfad = urllib.parse.urlparse(self.path).path
            laenge = min(int(self.headers.get("Content-Length") or 0), 200_000)
            formular = urllib.parse.parse_qs(self.rfile.read(laenge).decode("utf-8"), keep_blank_values=True)
            if pfad in ("/speichern", "/allgemein/speichern"):
                reiter = "allgemein" if pfad == "/allgemein/speichern" else "haupt"
                cfg, fehler = web.aus_formular(formular, reiter)
                if fehler:
                    self.senden(200, web.seite(cfg, fehler=fehler, reiter=reiter))
                    return
                prog.config_speichern(cfg)
                self.senden(200, web.seite(cfg, "Gespeichert. Das Display startet neu, diese Seite lädt "
                                                "in etwa 25 Sekunden neu.", neustart=True, reiter=reiter))
                web.neu_starten()
            elif pfad == "/standard":
                prog.config_zuruecksetzen()
                self.senden(200, web.seite(prog.config_laden(), "Auf Standard zurückgesetzt. Das Display "
                                           "startet neu, diese Seite lädt in etwa 25 Sekunden neu.", neustart=True,
                                           reiter="allgemein"))
                web.neu_starten()
            elif pfad in ("/wl/speichern", "/entfernen", "/wl/verschieben"):
                # Nur die Wiener-Linien-Einstellungen; dazu eine Abfahrt streichen oder verschieben
                weg = formular.get("weg", [""])[0]
                if pfad == "/entfernen" and weg.isdigit():
                    formular[f"wl{weg}_weg"] = ["1"]
                cfg, fehler = web.aus_formular(formular, "wl")
                if fehler:
                    self.senden(200, web.seite(cfg, fehler=fehler, reiter="wl"))
                    return
                text = {"/entfernen": "Abfahrt entfernt.", "/wl/verschieben": "Reihenfolge geändert."}.get(
                    pfad, "Gespeichert.")
                if pfad == "/wl/verschieben":
                    liste = cfg["wiener_linien"]["abfahrten"]
                    try:
                        i, richtung = formular.get("schieb", [""])[0].split(":")
                        i = int(i)
                        j = {"hoch": i - 1, "runter": i + 1, "ganzhoch": 0}[richtung]
                        if not (0 <= i < len(liste) and 0 <= j < len(liste)):
                            raise ValueError
                    except (ValueError, KeyError):
                        self.senden(200, web.seite(cfg, fehler=["Verschieben nicht möglich."], reiter="wl"))
                        return
                    liste.insert(j, liste.pop(i))
                prog.config_speichern(cfg)
                self.senden(200, web.seite(cfg, text + " Das Display startet neu, diese Seite lädt "
                                                "in etwa 25 Sekunden neu.", neustart=True, reiter="wl"))
                web.neu_starten()
            elif pfad == "/abfahrt":
                cfg, fehler = web.abfahrt_hinzufuegen(formular)
                if fehler:
                    self.senden(200, web.abfahrt_seite(formular.get("linie", [""])[0],
                                                       formular.get("station", [""])[0], fehler))
                    return
                prog.config_speichern(cfg)
                self.senden(200, web.seite(cfg, "Abfahrt hinzugefügt. Das Display startet neu, "
                                                "diese Seite lädt in etwa 25 Sekunden neu.", neustart=True,
                                           reiter="wl"))
                web.neu_starten()
            elif pfad == "/bt/suchen":
                import bluetooth_naehe
                gefunden = bluetooth_naehe.suchen()
                self.senden(200, web.seite_bluetooth(prog.config_laden(), gefunden=gefunden))
            elif pfad == "/bt/testen":
                import bluetooth_naehe
                cfg, fehler = web.aus_formular(formular, "bt")
                adresse = cfg["bluetooth"]["adresse"]
                if not adresse:
                    self.senden(200, web.seite_bluetooth(cfg, fehler=fehler or ["Bitte zuerst eine Adresse eintragen."]))
                    return
                name = bluetooth_naehe.namensanfrage(adresse)
                gemessen = bluetooth_naehe.pegel(adresse) if name else None
                self.senden(200, web.seite_bluetooth(cfg, test=(name or "", gemessen)))
            elif pfad == "/wlan/suchen":
                if g.wlan:
                    g.wlan.suchen()
                self.senden(200, web.seite_wlan(prog.config_laden(), "Suche abgeschlossen."))
            elif pfad == "/wlan/hotspot_an" and g.wlan:
                teile = web.kopf("wlan", "WLAN", meldung=(
                    "Der Pi öffnet jetzt den Hotspot und verlässt dein WLAN, diese Seite ist gleich nicht mehr "
                    "erreichbar. Am Display erscheinen Name, Passwort und QR-Code des Hotspots. Ist nach "
                    "einigen Minuten kein Gerät verbunden, kehrt der Pi von selbst in dein WLAN zurück."))
                self.senden(200, "".join(teile) + "</body></html>")
                threading.Thread(target=g.wlan.hotspot_starten, args=("Hotspot von Hand geöffnet",),
                                 daemon=True).start()
            elif pfad == "/wlan/hotspot_aus" and g.wlan:
                teile = web.kopf("wlan", "WLAN", meldung=(
                    "Der Pi sucht jetzt ein bekanntes WLAN und verbindet sich. Der Hotspot geht dabei aus. "
                    "Klappt es, zeigt das Display eine Minute lang das WLAN und die Adresse. Findet er keins, "
                    "bleibt der Hotspot offen."))
                self.senden(200, "".join(teile) + "</body></html>")
                threading.Thread(target=g.wlan.bekannte_versuchen, daemon=True).start()
            elif pfad == "/wlan/verbinden":
                ssid = (formular.get("ssid_frei", [""])[0].strip() or formular.get("ssid", [""])[0])
                passwort = formular.get("passwort", [""])[0]
                fehler = []
                if not ssid or len(ssid.encode("utf-8")) > 32:
                    fehler.append("Bitte ein WLAN auswählen.")
                if passwort and not 8 <= len(passwort) <= 63:
                    fehler.append("Ein WLAN-Passwort hat 8 bis 63 Zeichen.")
                if fehler or not g.wlan:
                    self.senden(200, web.seite_wlan(prog.config_laden(), fehler=fehler or ["WLAN-Einrichtung läuft nicht."]))
                    return
                teile = web.kopf("wlan", "WLAN", meldung=(
                    f"Der Pi verbindet sich jetzt mit „{ssid}“. Diese Seite ist dann nicht mehr erreichbar. "
                    "Klappt es, zeigt das Display eine Minute lang die neue Adresse. Klappt es nicht, "
                    "bleibt alles wie vorher bzw. der Hotspot geht wieder an (das dauert bis zu 2 Minuten)."))
                self.senden(200, "".join(teile) + "</body></html>")
                g.wlan.verbinden(ssid, passwort)
            elif pfad == "/wlan/speichern":
                cfg, fehler = web.aus_formular(formular, "wlan")
                if fehler:
                    self.senden(200, web.seite_wlan(cfg, fehler=fehler))
                    return
                prog.config_speichern(cfg)
                self.senden(200, web.seite_wlan(cfg, "Gespeichert. Das Display startet neu, diese Seite "
                                                     "lädt in etwa 25 Sekunden neu.", neustart=True))
                web.neu_starten()
            elif pfad == "/bt/speichern":
                cfg, fehler = web.aus_formular(formular, "bt")
                if fehler:
                    self.senden(200, web.seite_bluetooth(cfg, fehler=fehler))
                    return
                prog.config_speichern(cfg)
                self.senden(200, web.seite(cfg, "Gespeichert. Das Display startet neu, diese Seite lädt "
                                                "in etwa 25 Sekunden neu.", neustart=True, reiter="bt"))
                web.neu_starten()
            elif pfad == "/herunterfahren":
                teile = web.kopf("allgemein", meldung="Der Pi fährt herunter. Das Display zeigt gleich "
                                 "„Ausgeschaltet“. " + aus_text(web.prog.hub_da()))
                self.senden(200, "".join(teile) + "</body></html>")
                web.herunterfahren()
            elif pfad == "/neuzeichnen":
                warteschlange.put(("neuzeichnen", None))
                self.weiter()
            else:
                self.senden(404, "Nicht gefunden", "text/plain; charset=utf-8")

    server = ThreadingHTTPServer(("", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # Zusaetzlich Port 80: dort fragen Handys nach, ob sie Internet haben (Anmeldeseite im
    # Hotspot). Braucht CAP_NET_BIND_SERVICE in der Dienst-Datei; ohne geht es nur auf `port`.
    try:
        server80 = ThreadingHTTPServer(("", 80), Handler)
        server80.daemon_threads = True
        threading.Thread(target=server80.serve_forever, daemon=True).start()
    except OSError as e:
        logging.getLogger(__name__).info("Port 80 nicht verfügbar (%s), nur Port %s", e, port)
    return server
