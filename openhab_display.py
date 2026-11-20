#!/usr/bin/env python3
"""openHAB-Display: zeigt Werte aus openHAB auf dem Waveshare 2,13" E-Paper V4.

Liest nur aus openHAB (REST-API und Event-Stream), schaltet nie etwas.
Regeln (siehe CLAUDE.md und docs/items.md):
- Alarme (Rauch, Wasser, Alarmanlage, stiller Alarm, Keller) fuellen den ganzen
  Bildschirm und werden sofort gezeichnet, auch wenn niemand da ist.
- Sonst wird nur gezeichnet, wenn jemand im Vorzimmer ist:
  beim Betreten sofort, wichtige Aenderungen (Kopf- und Fusszeile) sofort,
  Messwerte hoechstens alle paar Minuten.

Einstellungen: config.default.json (Standard), config.json (von der
Web-Oberflaeche geschrieben, ueberschreibt den Standard).
Web-Oberflaeche: http://<pi>:8080

Aufruf:
  python3 openhab_display.py            Dauerbetrieb (so laeuft der Dienst)
  python3 openhab_display.py --png X    aktuelles Bild nur als PNG speichern
"""

import datetime
import json
import logging
import os
import queue
import glob
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw, ImageFont

ORDNER = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(ORDNER, "lib"))
import wetter_symbole  # noqa: E402

CONFIG_STANDARD = os.path.join(ORDNER, "config.default.json")
CONFIG_DATEI = os.path.join(ORDNER, "config.json")
TOKEN_DATEI = os.path.join(ORDNER, "token")  # optional, Rechte 600
WEB_PORT = 8080

VOLL_NACH_TEILEN = 20        # nach so vielen Teilaktualisierungen eine volle ...
VOLL_NACH_TEILEN_DA = 60
PRO_SEITE = 3         # Abfahrten pro Abfahrten-Seite, weitere kommen auf weitere Seiten
AUTARK_NACH = 180     # s ohne openHAB: Autark-Modus (nur Abfahrten, Zeitplan statt Bewegungsmelder)
ANKUNFT_S = 60        # Tuer auf, waehrend niemand zu Hause ist: so lange gilt "jemand da"
ABFAHRTEN_WEG_S = 20  # Abfahrten-Seite so lange nach dem Gehen weiss schalten (ohne Uhrzeit = bald falsch)     # ... ausser es steht jemand davor: dann erst hier (Flackern vermeiden)
VOLL_SPAETESTENS = 6 * 3600  # s: spaetestens dann eine volle Aktualisierung
STILLE_MAX = 600             # s ohne Ereignis: Verbindung neu aufbauen
VERBINDUNG_WEG = 180         # s ohne Verbindung: Hinweis in der Fusszeile

BREITE, HOEHE = 250, 122
# Platzhalter fuer "Display ist weiss": unterscheidet sich von jeder echten Anzeige
LEER_ANZEIGE = {"alarme": (), "gespeichert": 0, "kopf": None, "fuss": None,
                "messwerte": None, "abfahrten": None, "leer": True}
SCHRIFT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
SCHRIFT_FETT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
WOCHENTAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
LEER = (None, "NULL", "UNDEF", "")

log = logging.getLogger("openhab-display")
OPENHAB = "http://openhab:8080"  # wird aus der Konfiguration gesetzt


# --- Einstellungen ---------------------------------------------------------

def config_standard():
    with open(CONFIG_STANDARD, encoding="utf-8") as f:
        return json.load(f)


def config_laden():
    cfg = config_standard()
    try:
        with open(CONFIG_DATEI, encoding="utf-8") as f:
            gespeichert = json.load(f)
        # Abschnitte zusammenfuehren, damit neue Einstellungen ihren Standardwert behalten
        for schluessel, wert in gespeichert.items():
            if isinstance(wert, dict) and isinstance(cfg.get(schluessel), dict) and schluessel not in (
                    "fenster", "rauchmelder", "wassermelder_schalter", "wassermelder_text"):
                cfg[schluessel].update(wert)
            else:
                cfg[schluessel] = wert
    except FileNotFoundError:
        pass
    except ValueError:
        log.error("config.json ist fehlerhaft, verwende den Standard")
    return cfg


def config_speichern(cfg):
    neu = CONFIG_DATEI + ".neu"
    with open(neu, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(neu, CONFIG_DATEI)


def config_zuruecksetzen():
    try:
        os.remove(CONFIG_DATEI)
    except FileNotFoundError:
        pass


def benutzte_items(cfg):
    namen = set()
    for feld in cfg["felder"]:
        namen.update(feld["items"])
    namen.update(cfg["wetter"].values())
    namen.add(cfg["kleidung"]["gefuehlt"])
    namen.add(cfg["strom_vergleich"]["leistung"])
    for schluessel in ("fenster", "rauchmelder", "wassermelder_schalter", "wassermelder_text"):
        namen.update(cfg[schluessel])
    for schluessel in ("keller", "alarmanlage", "anwesenheit", "waesche"):
        namen.update(cfg[schluessel].values())
    namen.discard("")
    return sorted(namen)


# --- openHAB lesen ---------------------------------------------------------

def anfrage(pfad, accept="text/plain"):
    req = urllib.request.Request(OPENHAB + pfad, headers={"Accept": accept})
    try:
        with open(TOKEN_DATEI) as f:
            token = f.read().strip()
        if token:
            req.add_header("Authorization", "Bearer " + token)
    except FileNotFoundError:
        pass
    return req


def zustaende_holen(items):
    zustaende = {}
    for name in items:
        try:
            with urllib.request.urlopen(anfrage(f"/rest/items/{name}/state"), timeout=15) as r:
                zustaende[name] = r.read().decode("utf-8").strip()
        except urllib.error.HTTPError as e:
            log.warning("Item %s: HTTP %s", name, e.code)
            zustaende[name] = None
    return zustaende


def zustand_setzen(item, wert):
    """Setzt den Zustand eines Items (kein Befehl). Nur fuer die Handy-Naehe freigegeben."""
    req = anfrage(f"/rest/items/{item}/state")
    req.method = "PUT"
    req.data = str(wert).encode("utf-8")
    req.add_header("Content-Type", "text/plain")
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def item_liste_holen():
    """Alle Items mit Beschriftung und Zustand (fuer die Web-Oberflaeche)."""
    with urllib.request.urlopen(anfrage("/rest/items?fields=name,label,state,type",
                                        "application/json"), timeout=20) as r:
        return {i["name"]: i for i in json.load(r)}


def ereignisse_lesen(items, warteschlange):
    """Laeuft im Hintergrund: Event-Stream lesen, Aenderungen einreihen."""
    themen = ",".join(f"openhab/items/{n}/statechanged" for n in items)
    pfad = "/rest/events?topics=" + urllib.parse.quote(themen, safe="/,*")
    pause = 5
    while True:
        try:
            # Nach jedem (Neu-)Verbinden alle Werte frisch holen
            warteschlange.put(("alle", zustaende_holen(items)))
            with urllib.request.urlopen(anfrage(pfad, "text/event-stream"), timeout=STILLE_MAX) as r:
                log.info("Event-Stream verbunden")
                warteschlange.put(("verbunden", True))
                pause = 5
                for zeile in r:
                    zeile = zeile.decode("utf-8").strip()
                    if not zeile.startswith("data:"):
                        continue
                    try:
                        daten = json.loads(zeile[5:])
                        name = daten["topic"].split("/")[2]
                        wert = json.loads(daten["payload"])["value"]
                    except (ValueError, KeyError, IndexError):
                        continue
                    warteschlange.put(("item", (name, wert)))
        except Exception as e:  # Netz weg, openHAB neu gestartet, Zeitueberschreitung ...
            log.warning("Event-Stream unterbrochen: %s", e)
        warteschlange.put(("verbunden", False))
        time.sleep(pause)
        pause = min(pause * 2, 120)


# --- Werte aufbereiten -----------------------------------------------------

def uhrzeit(text):
    """DateTime-Zustand aus openHAB ('2026-11-19T15:10:27.390Z') -> '16:10' (Ortszeit)."""
    try:
        return datetime.datetime.fromisoformat(text).astimezone().strftime("%H:%M")
    except (TypeError, ValueError):
        return None


def millimeter(text):
    """Niederschlag aus openHAB ('0.0012 m', '1.2 mm') in Millimeter, sonst None."""
    n = zahl(text)
    if n is None:
        return None
    t = str(text).strip()
    if t.endswith(" mm"):
        return n
    if t.endswith(" cm"):
        return n * 10
    if t.endswith(" m"):
        return n * 1000
    return n


def zahl(text):
    if text in LEER:
        return None
    try:
        return float(text.split()[0])
    except ValueError:
        return None


def komma(wert, stellen=1):
    return f"{wert:.{stellen}f}".replace(".", ",")


def in_zeiten(zeiten, t=None):
    """Liegt die Uhrzeit in einem der Zeitfenster? zeiten = [{"tage": [0..6], "von": "HH:MM",
    "bis": "HH:MM"}], 0 = Montag. Geht ein Fenster ueber Mitternacht (von > bis), zaehlt der
    Teil nach Mitternacht noch zum Vortag."""
    t = t or time.localtime()
    jetzt = t.tm_hour * 60 + t.tm_min
    for f in zeiten or []:
        try:
            vh, vm = map(int, f["von"].split(":"))
            bh, bm = map(int, f["bis"].split(":"))
        except (KeyError, ValueError):
            continue
        von, bis, tage = vh * 60 + vm, bh * 60 + bm, f.get("tage", range(7))
        if von <= bis:
            if t.tm_wday in tage and von <= jetzt < bis:
                return True
        elif (t.tm_wday in tage and jetzt >= von) or ((t.tm_wday - 1) % 7 in tage and jetzt < bis):
            return True
    return False


def dauer_text(minuten):
    """75 -> "1 Std 15 Min", 45 -> "45 Min", 120 -> "2 Std"."""
    minuten = max(0, int(minuten))
    std, mi = divmod(minuten, 60)
    if not std:
        return f"{mi} Min"
    return f"{std} Std" + (f" {mi} Min" if mi else "")


def minuten_seit(stunde, minute):
    """Minuten seit HH:MM (heute, oder gestern, wenn die Zeit noch in der Zukunft liegt)."""
    jetzt = datetime.datetime.now()
    start = jetzt.replace(hour=stunde, minute=minute, second=0, microsecond=0)
    if start > jetzt + datetime.timedelta(minutes=1):
        start -= datetime.timedelta(days=1)
    return int((jetzt - start).total_seconds() // 60)


def anzeige_berechnen(cfg, z, getrennt_seit, abfahrten=None):
    """Liefert, was angezeigt werden soll (ohne Uhrzeit der Zeichnung).

    abfahrten: Zeilen fuer die Abfahrten-Seite (wiener_linien.zeilen) oder None
    fuer die Hauptansicht.
    """
    def wert(item):
        return z.get(item) if item else None

    # Alarme
    alarme = []
    for item, raum in cfg["rauchmelder"].items():
        if wert(item) not in LEER + ("IDLE_OFF",):
            alarme.append(("RAUCH!", "Rauchmelder " + raum))
    for item, ort in cfg["wassermelder_schalter"].items():
        if wert(item) == "ON":
            alarme.append(("WASSER!", "Wassermelder " + ort))
    for item, ort in cfg["wassermelder_text"].items():
        if wert(item) not in LEER + ("DRY",):
            alarme.append(("WASSER!", "Wassermelder " + ort))
    aa = cfg["alarmanlage"]
    if wert(aa["ausgeloest"]) == "ON":
        alarme.append(("ALARM", "Alarmanlage ausgelöst"))
    if wert(aa["still"]) == "ON":
        alarme.append(("ALARM", "Stiller Alarm ausgelöst"))
    if wert(cfg["keller"]["alarm"]) == "ON":
        alarme.append(("ALARM KELLER", "Kellertür wurde geöffnet"))
    if wert(cfg["keller"]["tuer"]) == "OPEN":
        alarme.append(("ALARM KELLER", "Kellertür offen"))
    gespeichert = int(zahl(wert(aa["gespeichert"])) or 0)

    # Kopfzeile: Anwesenheit
    anw = cfg["anwesenheit"]
    personen = zahl(wert(anw["personen"]))
    scharf = wert(aa["scharf"]) == "ON"
    if personen is None:
        status, status_invers = "", False
    elif personen <= 0:
        status, status_invers = "ABWESEND", True
    elif personen == 1 and (not anw["ich"] or wert(anw["ich"]) == "ON"):
        status, status_invers = "Anwesend", False
    else:
        status, status_invers = f"Gäste ({int(personen)})", False
    if scharf:
        status = (status + " · SCHARF") if status else "SCHARF"
        status_invers = True

    # Messwerte (Mittelwert, wenn ein Feld mehrere Items hat)
    messwerte = []
    mittel = []
    for feld in cfg["felder"]:
        werte = [w for w in (zahl(wert(n)) for n in feld["items"]) if w is not None]
        if werte:
            mittel.append(sum(werte) / len(werte))
            text = komma(mittel[-1], feld["stellen"]) + feld["einheit"]
        else:
            mittel.append(None)
            text = "–"
        messwerte.append((feld["titel"], text, None))
    # Kleidungs-Symbol (nach gefuehlter Temperatur, sonst gemessener) und "gefuehlt" im Titel
    kl = cfg["kleidung"]
    i = kl["feld"]
    if kl["aktiv"] and 0 <= i < len(messwerte):
        gef = zahl(wert(kl["gefuehlt"])) if kl["gefuehlt"] else None
        titel, text, _ = messwerte[i]
        if gef is not None:
            titel += f" · gefühlt {int(round(gef))}°"
        basis = gef if gef is not None else mittel[i]
        teil = wetter_symbole.kleidung_fuer(basis, kl["grenzen"]) if basis is not None else None
        messwerte[i] = (titel, text, teil)

    # Hoher Stromverbrauch: Blitz im Strom-Feld (Vorschlag S1)
    sh = cfg["strom_hoch"]
    i = sh["feld"]
    if sh["aktiv"] and 0 <= i < len(messwerte) and mittel[i] is not None and mittel[i] >= sh["grenze"]:
        titel, text, _ = messwerte[i]
        messwerte[i] = (titel, text, "blitz")

    # Strom heute im Vergleich zum ueblichen Tag (Vorschlag D: Balken, Strich = ueblich)
    sv = cfg["strom_vergleich"]
    i = sv["feld"]
    profil = z.get(PROFIL_ITEM)
    if sv["aktiv"] and profil and 0 <= i < len(messwerte) and mittel[i] is not None:
        ueblich = ueblich_bis_jetzt(json.loads(profil))
        if ueblich >= 0.1:
            titel, text, _ = messwerte[i]
            # auf 2 % gerundet, damit nicht jede Kleinigkeit neu gezeichnet wird
            messwerte[i] = (titel, text, ("balken", round(mittel[i] / ueblich * 50) / 50))

    # Waesche (Vorschlag W2): "Wäsche läuft (seit HH:MM)" / "Wäsche fertig (HH:MM, NN min)"
    w_text = wert(cfg["waesche"]["item"]) or ""
    w_fertig = w_text.startswith("Wäsche fertig")
    w_laeuft = w_text.startswith("Wäsche läuft")
    w_klammer = re.search(r"\((.*)\)", w_text)
    w_klammer = w_klammer.group(1) if w_klammer else ""

    # Fusszeile
    offen = [name for item, name in cfg["fenster"].items() if wert(item) not in LEER + ("CLOSED",)]
    symbol = untergang = None  # nur in der Wetterzeile
    extra = ()
    # Bei schwarzen Meldungen (Fenster offen ...) zeigt ein Symbol links, dass Waesche laeuft/fertig ist
    w_symbol = ("waesche_haken",) if w_fertig else ("waesche",) if w_laeuft else ()
    if getrennt_seit and time.time() - getrennt_seit > VERBINDUNG_WEG:
        fuss, fuss_invers = "Keine Verbindung zu openHAB", True
    elif offen:
        fuss = ("Fenster offen: " + ", ".join(offen)) if len(offen) <= 2 else f"{len(offen)} Fenster offen"
        fuss_invers, extra = True, w_symbol
    elif gespeichert > 0:
        fuss = "1 Alarm gespeichert" if gespeichert == 1 else f"{gespeichert} Alarme gespeichert"
        fuss_invers, extra = True, w_symbol
    elif w_fertig:
        # "14:20, 75 min" -> "Wäsche fertig · 14:20 (1 Std 15 Min)"
        zeit, _, dauer = w_klammer.partition(",")
        m = re.search(r"(\d+)\s*min", dauer)
        dauer = dauer_text(int(m.group(1))) if m else dauer.strip()
        fuss = "Wäsche fertig" + (f" · {zeit.strip()}" if zeit.strip() else "") \
            + (f" ({dauer})" if dauer else "")
        fuss_invers, extra = True, ("waesche_haken",)
    elif w_laeuft:
        # "seit 12:18" -> "Laufzeit 45 Min" (Wunsch des Nutzers: Dauer statt Uhrzeit)
        m = re.search(r"(\d{1,2}):(\d{2})", w_klammer)
        if m:
            fuss = "Laufzeit " + dauer_text(minuten_seit(int(m.group(1)), int(m.group(2))))
        else:
            fuss = "Wäsche läuft"
        fuss_invers = False
        extra = ("waesche",)
        untergang = uhrzeit(wert(cfg["wetter"].get("sonnenuntergang", "")))
    elif wert(anw["simulation"]) == "ON":
        fuss, fuss_invers = "Anwesenheitssimulation läuft", False
    else:
        we = cfg["wetter"]
        bed = wert(we["zustand"])
        tmin, tmax = zahl(wert(we["min"])), zahl(wert(we["max"]))
        if bed not in LEER:
            symbol = wetter_symbole.kategorie(bed)
        # Schirm: es regnet jetzt UND fuer heute ist Regen vorhergesagt
        jetzt = wert(we.get("jetzt", ""))
        regnet = (jetzt not in LEER and wetter_symbole.kategorie(jetzt) in wetter_symbole.REGEN) \
            or (millimeter(wert(we.get("regen_jetzt", ""))) or 0) > 0
        haelt_an = (bed not in LEER and wetter_symbole.kategorie(bed) in wetter_symbole.REGEN) \
            or (millimeter(wert(we.get("regen_heute", ""))) or 0) >= 1
        if regnet and haelt_an:
            symbol = "schirm"
        # Glatteis: kalt (jetzt hoechstens 1 Grad oder heute Frost) UND nass
        # (Niederschlag jetzt oder heute, Schnee, oder sehr feucht = Reif); Eisregen immer
        aussen = zahl(wert(we.get("aussen", "")))
        feuchte = zahl(wert(we.get("feuchte", "")))
        if feuchte is not None and feuchte <= 1:
            feuchte *= 100  # openHAB liefert 0.79 statt 79 %
        texte = " ".join(x for x in (jetzt, bed) if x not in LEER).lower()
        kalt = (aussen is not None and aussen <= 1) or (tmin is not None and tmin <= 0)
        nass = regnet or haelt_an or "schnee" in texte or "graupel" in texte \
            or (millimeter(wert(we.get("regen_heute", ""))) or 0) > 0 \
            or (feuchte is not None and feuchte >= 90)
        if any(w in texte for w in ("eisregen", "gefrier", "glatt", "glätte")) or (kalt and nass):
            extra += ("glatteis",)
        uv = zahl(z.get(UV_ITEM))
        if cfg["uv"]["aktiv"] and uv is not None and uv >= cfg["uv"]["grenze"]:
            extra += ("sonnenbrille",)
        teile = [bed] if bed not in LEER and (cfg.get("wetter_text", True) or not symbol) else []
        if tmin is not None and tmax is not None:
            a, b = round(tmin), round(tmax)
            teile.append(f"{a}°" if a == b else f"{a}–{b}°")
        fuss, fuss_invers = "  ".join(teile), False
        untergang = uhrzeit(wert(we.get("sonnenuntergang", "")))

    # Abfahrten: Rollstuhl nur bei barrierefreien Wagen, Schneeflocke nur bei
    # klimatisierten und nur, wenn es draussen heiss ist (Wunsch des Nutzers)
    if abfahrten:
        wl = cfg["wiener_linien"]
        # Temperatur immer von Open-Meteo, damit es auch ohne openHAB gleich funktioniert
        aussen = zahl(z.get(TEMP_ITEM))
        heiss = bool(wl.get("klima")) and aussen is not None and aussen > wl.get("klima_ab", 25)
        abfahrten = [(*zl[:5], bool(wl.get("rollstuhl")) and zl[5], heiss and zl[6]) for zl in abfahrten]

    t = time.localtime()
    datum = f"{WOCHENTAGE[t.tm_wday]} {t.tm_mday:02d}.{t.tm_mon:02d}."

    return {
        "alarme": tuple(alarme),
        "gespeichert": gespeichert if alarme else 0,
        "kopf": (datum, status, status_invers),
        "fuss": (fuss, fuss_invers, symbol, untergang, extra),
        "messwerte": tuple(messwerte),
        "abfahrten": tuple(abfahrten[:PRO_SEITE]) if abfahrten else None,
    }


# --- Zeichnen --------------------------------------------------------------

_schriften = {}
BILD_SPERRE = threading.Lock()  # Pillow-Schriften nicht aus zwei Threads gleichzeitig


def schrift(groesse, fett=False):
    schluessel = (groesse, fett)
    if schluessel not in _schriften:
        _schriften[schluessel] = ImageFont.truetype(SCHRIFT_FETT if fett else SCHRIFT, groesse)
    return _schriften[schluessel]


def passend(d, text, max_breite, groesse, fett=True, minimum=9):
    """Groesste Schrift (bis 'groesse'), in der der Text in max_breite passt."""
    while groesse > minimum and d.textlength(text, font=schrift(groesse, fett)) > max_breite:
        groesse -= 1
    return schrift(groesse, fett)


def bild_zeichnen(a, uhrzeit, alarm_seit):
    with BILD_SPERRE:
        return _bild_zeichnen(a, uhrzeit, alarm_seit)


def _bild_zeichnen(a, uhrzeit, alarm_seit):
    bild = Image.new("1", (BREITE, HOEHE), 255)
    d = ImageDraw.Draw(bild)
    klein = schrift(11)
    if a["alarme"]:
        d.rectangle((0, 0, BREITE, HOEHE), fill=0)
        titel = a["alarme"][0][0]
        texte = []
        for _, t in a["alarme"]:
            if t not in texte:
                texte.append(t)
        if a["gespeichert"]:
            texte.append(f"{a['gespeichert']} Alarme gespeichert")
        if len(texte) > 2:
            texte = [texte[0], f"+ {len(texte) - 1} weitere"]
        d.text((BREITE // 2, 26), titel, font=passend(d, titel, BREITE - 10, 28), fill=255, anchor="mm")
        d.line((10, 48, BREITE - 10, 48), fill=255)
        for i, t in enumerate(texte):
            d.text((BREITE // 2, 68 + i * 22), t, font=passend(d, t, BREITE - 10, 16), fill=255, anchor="mm")
        unten = (f"seit {alarm_seit} · " if alarm_seit else "") + uhrzeit
        d.text((BREITE // 2, 112), unten, font=klein, fill=255, anchor="mm")
        return bild

    if a["abfahrten"]:
        _abfahrten_zeichnen(bild, d, a)
        return bild

    datum, status, status_invers = a["kopf"]
    d.text((4, 1), datum, font=klein, fill=0)
    d.text((BREITE - 4, 1), uhrzeit, font=klein, fill=0, anchor="ra")
    if status:
        f = passend(d, status, 140, 11)
        b = d.textlength(status, font=f) + 10
        x0 = (BREITE - b) / 2
        if status_invers:
            d.rectangle((x0, 0, x0 + b, 14), fill=0)
        d.text((BREITE / 2, 1), status, font=f, fill=255 if status_invers else 0, anchor="ma")
    d.line((0, 15, BREITE, 15), fill=0)

    # vier Felder: oben gross, unten mittel
    orte = [(4, 18, 24), (128, 18, 24), (4, 64, 18), (128, 64, 18)]
    for (titel, text, teil), (x, y, groesse) in zip(a["messwerte"], orte):
        d.text((x, y), titel, font=passend(d, titel, 118, 11, fett=False), fill=0)
        breite = 118
        if isinstance(teil, tuple) and teil[0] == "balken":
            # Wert etwas kleiner, darunter Balken: voll = doppelt so viel wie ueblich,
            # der Strich in der Mitte = ueblicher Tag bis jetzt
            unten = y + 41 if y < 60 else 96
            d.text((x, y + 10), text, font=passend(d, text, 118, groesse - 4), fill=0)
            x0, x1, y0 = x, x + 116, unten - 5
            d.rectangle((x0, y0, x1, y0 + 4), outline=0)
            anteil = max(0.0, min(1.0, teil[1] / 2))
            d.rectangle((x0, y0, x0 + int((x1 - x0) * anteil), y0 + 4), fill=0)
            m = (x0 + x1) // 2
            d.line((m, y0 - 2, m, y0 + 6), fill=0, width=2)
            continue
        if teil:
            # unten rechts im Feld, nur die schwarzen Pixel (die Ueberschrift bleibt lesbar)
            if teil in wetter_symbole.KLEIDUNG:
                k = wetter_symbole.kleidung(teil)
            else:
                k = wetter_symbole.symbol(teil)       # z. B. Blitz bei hohem Strom
            unten = y + 41 if y < 60 else 96           # Unterkante des Felds
            maske = Image.eval(k.convert("L"), lambda v: 255 - v)
            bild.paste(0, (x + 117 - k.width, unten - k.height), maske)
            breite = 116 - k.width - 4
        d.text((x, y + 12), text, font=passend(d, text, breite, groesse), fill=0)
    d.line((124, 18, 124, 58), fill=0)
    d.line((0, 61, BREITE, 61), fill=0)
    d.line((124, 64, 124, 96), fill=0)
    d.line((0, 99, BREITE, 99), fill=0)
    _fusszeile(d, a)
    return bild


def _fusszeile(d, a):
    fuss, fuss_invers, symbol, untergang, extra = a["fuss"]
    if fuss_invers:
        d.rectangle((0, 100, BREITE, HOEHE), fill=0)
    if not symbol and not untergang and not extra:
        if fuss:
            d.text((BREITE // 2, 111), fuss, font=passend(d, fuss, BREITE - 8, 14),
                   fill=255 if fuss_invers else 0, anchor="mm")
        return
    # Wetterzeile: links Symbol und Text, rechts Sonnenuntergang.
    # In einer schwarzen Zeile werden die Symbole weiss (nur ihre schwarzen Pixel).
    bild = d._image
    farbe = 255 if fuss_invers else 0

    def setzen(name, x):
        s = wetter_symbole.symbol(name)
        if fuss_invers:
            bild.paste(255, (x, 101), Image.eval(s.convert("L"), lambda v: 255 - v))
        else:
            bild.paste(s, (x, 101))

    links, rechts = 4, BREITE - 4
    if untergang:
        f = schrift(14, True)
        rechts -= d.textlength(untergang, font=f)
        d.text((rechts, 111), untergang, font=f, fill=farbe, anchor="lm")
        rechts -= 22
        setzen("untergang", int(rechts))
        rechts -= 6
    for s in (symbol,) + extra:
        if s:
            setzen(s, links)
            links += 23
    links += 3
    if fuss:
        d.text((links, 111), fuss, font=passend(d, fuss, rechts - links, 14), fill=farbe, anchor="lm")


WLAN_START_S = 60  # so lange zeigt das Display nach dem Start WLAN und Adresse


def wlan_info():
    """WLAN-Name, Signal (Prozent und dBm) und eigene IP-Adresse, None ohne WLAN."""
    import socket
    import subprocess
    ssid = prozent = dbm = None
    try:
        aus = subprocess.run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi", "list",
                              "--rescan", "no"], capture_output=True, text=True, timeout=10).stdout
        for zeile in aus.splitlines():
            if zeile.startswith("yes:"):
                rest, _, sig = zeile[4:].rpartition(":")
                ssid, prozent = rest.replace("\\:", ":"), int(sig)
                break
    except Exception as e:
        log.warning("WLAN-Name nicht lesbar: %s", e)
    try:
        with open("/proc/net/wireless") as f:
            for zeile in f:
                if zeile.strip().startswith("wlan0:"):
                    dbm = int(float(zeile.split()[3]))
    except (OSError, ValueError, IndexError):
        pass
    if not ssid:
        return None
    ip = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
            so.connect(("192.0.2.1", 9))  # schickt nichts, waehlt nur die Schnittstelle
            ip = so.getsockname()[0]
    except OSError:
        pass
    return {"ssid": ssid, "prozent": prozent, "dbm": dbm, "ip": ip}


def qr_bild(text, punkt=3, max_breite=None):
    """QR-Code als 1-Bit-Bild (ohne Rand, der weisse Rand kommt vom Platz drumherum),
    oder None, wenn python3-qrcode fehlt."""
    try:
        import qrcode
    except ImportError:
        return None
    q = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(text)
    q.make(fit=True)
    m = q.get_matrix()
    if max_breite and len(m) * punkt > max_breite:
        punkt = max(2, max_breite // len(m))
    b = Image.new("1", (len(m) * punkt, len(m) * punkt), 255)
    d = ImageDraw.Draw(b)
    for y, reihe in enumerate(m):
        for x, schwarz in enumerate(reihe):
            if schwarz:
                d.rectangle((x * punkt, y * punkt, x * punkt + punkt - 1, y * punkt + punkt - 1), fill=0)
    return b


def wlan_bild(info):
    """Startbild: WLAN, Signal und wo die Einstellungen zu finden sind, rechts ein
    QR-Code mit der Adresse (Wunsch des Nutzers: nicht abtippen muessen)."""
    with BILD_SPERRE:
        bild = Image.new("1", (BREITE, HOEHE), 255)
        d = ImageDraw.Draw(bild)
        d.rectangle((0, 0, BREITE - 1, 19), fill=0)
        d.text((BREITE // 2, 10), "Gestartet · WLAN verbunden", font=schrift(13, True), fill=255, anchor="mm")
        adresse = f"http://{info.get('ip') or 'display'}:{WEB_PORT}"
        qr = qr_bild(adresse)
        # Platz fuer den Text: ganze Breite oder links neben dem QR-Code (mit Ruhezone)
        rechts = BREITE - qr.width - 14 if qr else BREITE - 4
        if qr:
            bild.paste(qr, (BREITE - qr.width - 5, 20 + (HOEHE - 20 - qr.height) // 2))
        t = f"WLAN: {info['ssid']}"
        platz = rechts - 4 - (0 if qr else 40)
        f = passend(d, t, platz, 17, fett=True)
        d.text((4, 24), gekuerzt(d, t, platz, f), font=f, fill=0)
        dbm, prozent = info.get("dbm"), info.get("prozent")
        if dbm is not None:
            guete = "gut" if dbm >= -60 else "mittel" if dbm >= -70 else "schwach"
        elif prozent is not None:
            guete = "gut" if prozent >= 70 else "mittel" if prozent >= 45 else "schwach"
        else:
            guete = "?"
        teile = [f"{prozent} %" if prozent is not None else None,
                 f"{dbm} dBm" if dbm is not None else None]
        t = "Signal " + guete + " (" + ", ".join(x for x in teile if x) + ")"
        # Balken wie am Handy, 4 Stufen: ohne QR rechts oben, mit QR vor dem Signaltext
        stufen = {"gut": 4, "mittel": 2, "schwach": 1, "?": 0}[guete]
        bx, by = (4, 60) if qr else (214, 44)
        for i in range(4):
            x0, h = bx + i * 7 if qr else bx + i * 9, (4 + i * 4) if qr else (6 + i * 6)
            d.rectangle((x0, by - h, x0 + (4 if qr else 6), by), fill=0 if i < stufen else 255, outline=0)
        tx = 36 if qr else 4
        d.text((tx, 47), t, font=passend(d, t, rechts - tx, 13, fett=False, minimum=8), fill=0)
        d.line((0, 66, rechts, 66), fill=0)
        hinweis = "Einstellungen (QR-Code scannen):" if qr else "Einstellungen im Browser:"
        d.text((4, 72), hinweis, font=passend(d, hinweis, rechts - 4, 12, fett=False, minimum=8), fill=0)
        d.text(((4 + rechts) // 2, 102), adresse, font=passend(d, adresse, rechts - 6, 20, fett=True),
               fill=0, anchor="mm")
        return bild


def gekuerzt(d, text, max_breite, schriftart):
    """Text mit '…' kuerzen, bis er passt."""
    if d.textlength(text, font=schriftart) <= max_breite:
        return text
    while text and d.textlength(text + "…", font=schriftart) > max_breite:
        text = text[:-1]
    return text + "…"


def einrichtung_bild(wz):
    """Kein WLAN: wie man sich mit dem Hotspot verbindet. Beim Wechsel: 'Verbinde mit ...'."""
    with BILD_SPERRE:
        bild = Image.new("1", (BREITE, HOEHE), 255)
        d = ImageDraw.Draw(bild)
        d.rectangle((0, 0, BREITE - 1, 19), fill=0)
        if wz["modus"] == "verbinde":
            d.text((BREITE // 2, 10), "WLAN wird gewechselt", font=schrift(13, True), fill=255, anchor="mm")
            t = wz.get("meldung") or "Verbinde …"
            f = passend(d, t, BREITE - 8, 17, minimum=12)
            d.text((BREITE // 2, 55), gekuerzt(d, t, BREITE - 8, f), font=f, fill=0, anchor="mm")
            t = "Klappt es nicht, kommt der Hotspot wieder."
            d.text((BREITE // 2, 90), t, font=passend(d, t, BREITE - 8, 12, fett=False), fill=0, anchor="mm")
            return bild
        kopf = wz.get("meldung") or "Kein WLAN"
        kopf = kopf + " · Einrichtung" if len(kopf) < 16 else kopf
        f = passend(d, kopf, BREITE - 8, 13, minimum=11)
        d.text((BREITE // 2, 10), gekuerzt(d, kopf, BREITE - 8, f), font=f, fill=255, anchor="mm")
        ssid, pw = wz["hotspot_ssid"], wz.get("hotspot_passwort") or ""
        # QR-Code "mit diesem WLAN verbinden" (Wunsch des Nutzers); danach oeffnet das Handy
        # die Einrichtung von selbst (Anmeldeseite wie im Hotel-WLAN, siehe weboberflaeche)
        qr = qr_bild(wlan_qr_text(ssid, pw), max_breite=HOEHE - 30) if pw else None
        rechts = BREITE - qr.width - 12 if qr else BREITE - 4
        if qr:
            bild.paste(qr, (BREITE - qr.width - 4, 20 + (HOEHE - 20 - qr.height) // 2))
        for y, t in ((23, f"WLAN: {ssid}"), (43, f"Passwort: {pw or '?'}")):
            f = passend(d, t, rechts - 4, 16, minimum=8)
            d.text((4, y), gekuerzt(d, t, rechts - 4, f), font=f, fill=0)
        d.line((0, 64, rechts, 64), fill=0)
        hinweis = "QR-Code scannen oder öffnen:" if qr else "Dann im Browser öffnen:"
        d.text((4, 69), hinweis, font=passend(d, hinweis, rechts - 4, 12, fett=False, minimum=8), fill=0)
        adresse = f"http://{wz.get('hotspot_ip', '10.42.0.1')}:{WEB_PORT}"
        d.text(((4 + rechts) // 2, 101), adresse, font=passend(d, adresse, rechts - 6, 20, fett=True),
               fill=0, anchor="mm")
        return bild


def wlan_qr_text(ssid, passwort):
    """Inhalt eines WLAN-QR-Codes (versteht die Kamera von iPhone und Android)."""
    def schutz(s):
        return "".join("\\" + z if z in '\\;,:"' else z for z in s)
    return f"WIFI:T:WPA;S:{schutz(ssid)};P:{schutz(passwort)};;"


def aus_bild():
    """Bild beim Herunterfahren: bleibt ohne Strom stehen."""
    with BILD_SPERRE:
        bild = Image.new("1", (BREITE, HOEHE), 255)
        d = ImageDraw.Draw(bild)
        d.rectangle((0, 0, BREITE - 1, HOEHE - 1), outline=0)
        d.text((BREITE // 2, 34), "Ausgeschaltet", font=schrift(22, True), fill=0, anchor="mm")
        t = time.localtime()
        d.text((BREITE // 2, 60), f"seit {WOCHENTAGE[t.tm_wday]} {t.tm_mday:02d}.{t.tm_mon:02d}. "
               f"{t.tm_hour:02d}:{t.tm_min:02d}", font=schrift(12), fill=0, anchor="mm")
        for y, zeile in zip((86, 102), aus_hinweis()):
            d.text((BREITE // 2, y), zeile, font=passend(d, zeile, BREITE - 8, 11), fill=0, anchor="mm")
        return bild


HUB_KENNUNG = "1a40:0101"  # USB-Hub-HAT mit Akku (Chip FE1.1s)


def hub_da():
    """True, wenn der USB-Hub-HAT mit Akku am Pi steckt."""
    for d in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(d + "/idVendor") as f1, open(d + "/idProduct") as f2:
                if f"{f1.read().strip()}:{f2.read().strip()}" == HUB_KENNUNG:
                    return True
        except OSError:
            pass
    return False


def aus_hinweis():
    """Zwei Zeilen fuer das Abschaltbild. Mit Hub kann man den Strom nicht trennen:
    der Akku versorgt den Pi weiter, ausschalten geht nur ueber den Taster."""
    if hub_da():
        return ("Ganz aus: Taster am Hub lange drücken", "Einschalten: Taster kurz drücken")
    return ("Strom kann getrennt werden.", "Einschalten: Strom wieder anschließen")


def _abfahrten_zeichnen(bild, d, a):
    """Abfahrten-Seite (Vorschlag B des Nutzers): pro Abfahrt eine Zeile ohne Rahmen,
    links ein schwarzes Feld ueber die volle Zeilenhoehe mit grosser Liniennummer,
    in der Mitte Ziel und Abfahrt, rechts gross, wann man losgehen muss ("LOS!" als
    schwarzes Feld). Zwischen den Zeilen eine schmale Luecke. Keine Fusszeile: Wetter
    und Warnungen (z. B. Fenster offen) gehoeren auf die Hausseite (Wunsch des Nutzers)."""
    zeilen = a["abfahrten"]
    luecke = 4
    n = max(3, len(zeilen))
    zh = (HOEHE - luecke * (n - 1)) / n    # Zeilenhoehe
    lw = 44                                # Breite des Linienfelds
    spalte = BREITE - 66                   # linke Kante der Losgehen-Spalte
    r = 5                                  # Eckenradius
    for i, (linie, name, minuten, los, danach, rollstuhl, klima) in enumerate(zeilen):
        oben = round(i * (zh + luecke))
        ende = round(i * (zh + luecke) + zh) - 1
        mitte_y = (oben + ende) / 2
        # Linienfeld links (aussen rund, innen gerade)
        d.rounded_rectangle((0, oben, lw, ende), radius=r, fill=0)
        d.rectangle((lw - r, oben, lw, ende), fill=0)
        d.text((lw / 2 + 0.5, mitte_y + 1), linie, font=passend(d, linie, lw - 6, 24), fill=255, anchor="mm")
        # Losgehen rechts
        mitte_x = (spalte + BREITE) / 2
        if minuten is not None and los <= 0:
            d.rounded_rectangle((spalte, oben, BREITE - 1, ende), radius=r, fill=0)
            d.rectangle((spalte, oben, spalte + r, ende), fill=0)
            d.text((mitte_x, mitte_y + 1), "LOS!", font=schrift(22, True), fill=255, anchor="mm")
        elif minuten is not None:
            d.text((mitte_x, mitte_y - 8), "los in", font=schrift(10), fill=0, anchor="mm")
            d.text((mitte_x, mitte_y + 6), f"{los}′", font=schrift(20, True), fill=0, anchor="mm")
        # Mitte: Ziel und Abfahrt
        x = lw + 6
        d.text((x, mitte_y - 5), name, font=passend(d, name, spalte - x - 4, 16), fill=0, anchor="lm")
        if minuten is None:
            klein = "keine erreichbare Abfahrt"
        else:
            klein = f"fährt in {minuten} Min"
            if danach:
                klein += ", dann " + ", ".join(str(m) for m in danach)
        # Wagen-Symbole rechts hinter der Zeit (nur barrierefrei / klimatisiert bei Hitze)
        rechts = spalte - 4
        if minuten is not None:
            for an, sname in ((klima, "klima"), (rollstuhl, "rollstuhl")):
                if an:
                    s = wetter_symbole.wagen_symbol(sname)
                    bild.paste(s, (rechts - s.width, round(mitte_y + 11 - s.height / 2)))
                    rechts -= s.width + 3
        # Zu eng (Symbole)? Dann spaetere Abfahrten weglassen statt die Schrift winzig zu machen
        platz = rechts - x - 1
        if minuten is not None:
            for n in range(len(danach), -1, -1):
                rest = (", dann " + ", ".join(str(m) for m in danach[:n])) if n else ""
                for anfang in ("fährt in", "in"):
                    klein = f"{anfang} {minuten} Min{rest}"
                    if d.textlength(klein, font=schrift(9)) <= platz:
                        break
                else:
                    continue
                break
        d.text((x, mitte_y + 11), klein, font=passend(d, klein, platz, 11, fett=False, minimum=8),
               fill=0, anchor="lm")


class Display:
    """E-Paper ansteuern: volle oder Teilaktualisierung, danach schlafen."""

    def __init__(self):
        from waveshare_epd import epd2in13_V4
        self.epd = epd2in13_V4.EPD()
        self.letzter_puffer = None
        self.teile = 0
        self.letzte_volle = 0.0
        self.sperre = threading.Lock()  # waehrend des Zeichnens nicht beenden

    def zeigen(self, bild, voll=False, drehung=0, jemand_da=False):
        """jemand_da: die volle Aktualisierung (flackert schwarz) moeglichst aufschieben,
        bis niemand mehr hinschaut (Wunsch des Nutzers); sie kommt dann mit dem
        Weissschalten oder mit auffrischen()."""
        if drehung == 180:
            bild = bild.rotate(180)
        grenze = VOLL_NACH_TEILEN_DA if jemand_da else VOLL_NACH_TEILEN
        with self.sperre:
            puffer = self.epd.getbuffer(bild)
            self.epd.init()
            if (voll or self.letzter_puffer is None or self.teile >= grenze
                    or time.time() - self.letzte_volle > VOLL_SPAETESTENS):
                self.epd.displayPartBaseImage(puffer)  # volle Aktualisierung, merkt sich das Bild
                self.teile = 0
                self.letzte_volle = time.time()
                art = "voll"
            else:
                # Altes Bild als Vergleich in den Speicher des Displays, dann nur Unterschiede
                self.epd.send_command(0x26)
                self.epd.send_data2(self.letzter_puffer)
                self.epd.displayPartial(puffer)
                self.teile += 1
                art = "teil"
            self.epd.sleep()
            self.letzter_puffer = puffer
            return art


    def auffrischen(self):
        """Dasselbe Bild noch einmal voll zeichnen (gegen Geisterbilder)."""
        with self.sperre:
            if self.letzter_puffer is None:
                return
            self.epd.init()
            self.epd.displayPartBaseImage(self.letzter_puffer)
            self.teile = 0
            self.letzte_volle = time.time()
            self.epd.sleep()


class Gemeinsam:
    """Was Hauptschleife und Web-Oberflaeche gemeinsam sehen."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.sperre = threading.Lock()
        self.z = {}
        self.getrennt_seit = time.time()
        self.alarm_seit = None
        self.letzte_zeichnung = None  # Uhrzeit als Text
        self.da = False               # jemand im Vorzimmer
        self.oeffis = None            # (Abrufzeit, Abfahrtsdaten) der Wiener Linien
        self.naehe = None             # bluetooth_naehe.Naehe, wenn eingeschaltet
        self.aus = False              # True, sobald der Pi herunterfaehrt
        self.wlan = None              # wlan_einrichtung.Wlan
        self.wlan_zustand = None      # letzter gemeldeter Zustand (dict)
        self.autark = False           # openHAB aus oder nicht erreichbar
        self.ersatz = None            # None = Bewegungsmelder, sonst "zeiten" / "immer"

    def abfahrt_zeilen(self):
        wl = self.cfg["wiener_linien"]
        if not wl["aktiv"] or not wl["abfahrten"]:
            return []
        import wiener_linien
        with self.sperre:
            abruf = self.oeffis
        return wiener_linien.zeilen(wl["abfahrten"], abruf)

    def vorschau(self, seite="haupt", nummer=1):
        """Vorschau der Hauptansicht oder der Abfahrten-Seite Nummer `nummer` (ab 1)."""
        with self.sperre:
            z, getrennt, seit = dict(self.z), self.getrennt_seit, self.alarm_seit
        zeilen = None
        if seite == "abfahrten":
            zeilen = self.abfahrt_zeilen()[(nummer - 1) * PRO_SEITE:nummer * PRO_SEITE]
        a = anzeige_berechnen(self.cfg, z, getrennt, zeilen)
        return bild_zeichnen(a, time.strftime("%H:%M"), seit)


def abfahrten_lesen(cfg, g, warteschlange, wecker):
    """Laeuft im Hintergrund: Abfahrten holen, solange jemand im Vorzimmer ist."""
    import wiener_linien
    wl = wiener_linien.WienerLinien(ORDNER)
    eintraege = cfg["wiener_linien"]["abfahrten"]
    while True:
        wecker.wait()
        try:
            warteschlange.put(("oeffis", (time.time(), wl.abfahrten(eintraege))))
        except Exception as e:
            log.warning("Wiener Linien nicht abrufbar: %s", e)
        time.sleep(30)
        with g.sperre:
            if not g.da:
                wecker.clear()


UV_ITEM = "__uv"  # interner Name: UV-Index von Open-Meteo, liegt neben den openHAB-Werten
TEMP_ITEM = "__aussen"  # interner Name: Aussentemperatur jetzt von Open-Meteo


def open_meteo_noetig(cfg):
    wl = cfg["wiener_linien"]
    return cfg["uv"]["aktiv"] or (wl["aktiv"] and wl["abfahrten"] and wl.get("klima"))


def uv_lesen(cfg, warteschlange):
    """Laeuft im Hintergrund: hoechster UV-Index von heute und die Temperatur jetzt,
    halbstuendlich von Open-Meteo (frei, ohne Schluessel). Als Ort geht nur der grobe
    Ort aus der Einstellung mit. Die Temperatur entscheidet ueber die Schneeflocke
    bei den Abfahrten (bewusst nicht aus openHAB, damit es ueberall gleich geht)."""
    uv = cfg["uv"]
    url = ("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": uv["breite"], "longitude": uv["laenge"], "daily": "uv_index_max",
        "current": "temperature_2m", "timezone": "Europe/Vienna", "forecast_days": 1}))
    while True:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "openhab-display"})
            with urllib.request.urlopen(req, timeout=20) as r:
                daten = json.load(r)
            wert_heute = daten["daily"]["uv_index_max"][0]
            temp = daten.get("current", {}).get("temperature_2m")
            warteschlange.put(("item", (UV_ITEM, "" if wert_heute is None else str(wert_heute))))
            warteschlange.put(("item", (TEMP_ITEM, "" if temp is None else str(temp))))
            time.sleep(1800)
        except Exception as e:
            log.warning("UV-Index/Temperatur nicht abrufbar: %s", e)
            time.sleep(120)


PROFIL_ITEM = "__strom_profil"  # interner Name: ueblicher Tagesverlauf (kWh je Viertelstunde, JSON)


def strom_profil(punkte, heute):
    """Ueblicher Tagesverlauf aus gespeicherten Leistungswerten [(Sekunden, Watt), ...]:
    fuer jeden vergangenen Tag die bis zu jeder Viertelstunde verbrauchten kWh, davon
    je Viertelstunde der Median (einzelne Ausreisser-Tage verzerren ihn nicht).
    Liefert 96 Werte (Stand am Ende jeder Viertelstunde) oder None."""
    tage = {}
    abgedeckt = {}
    for (t, w), (t2, _) in zip(punkte, punkte[1:]):
        ende = min(t2, t + 900)  # Luecken hoechstens 15 Min als gleichbleibend werten
        while t < ende:
            lt = time.localtime(t)
            tag = (lt.tm_year, lt.tm_mon, lt.tm_mday)
            in_tag = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
            stueck = min(ende, t + 900 - in_tag % 900) - t
            if tag != heute:
                tage.setdefault(tag, [0.0] * 96)[min(95, in_tag // 900)] += max(0.0, w) * stueck / 3.6e6
                abgedeckt[tag] = abgedeckt.get(tag, 0) + stueck
            t += stueck
    kurven = []
    for tag, viertel in tage.items():
        if abgedeckt[tag] < 20 * 3600:
            continue  # zu wenig Daten (z. B. erster Tag)
        summe, kurve = 0.0, []
        for v in viertel:
            summe += v
            kurve.append(summe)
        kurven.append(kurve)
    if len(kurven) < 5:
        return None
    profil = []
    for i in range(96):
        werte = sorted(k[i] for k in kurven)
        n = len(werte)
        profil.append(werte[n // 2] if n % 2 else (werte[n // 2 - 1] + werte[n // 2]) / 2)
    return [round(x, 3) for x in profil]


def ueblich_bis_jetzt(profil):
    """kWh, die an einem ueblichen Tag bis jetzt verbraucht sind (zwischen den Viertelstunden linear)."""
    lt = time.localtime()
    sek = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
    i, rest = divmod(sek, 900)
    i = min(i, 95)
    vorher = profil[i - 1] if i else 0.0
    return vorher + (profil[i] - vorher) * rest / 900


def profil_holen(cfg):
    """Gespeicherte Leistungswerte der letzten Tage aus openHAB holen -> (Profil oder None, Anzahl Werte)."""
    sv = cfg["strom_vergleich"]
    beginn = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - sv["tage"] * 86400 - 3600))
    pfad = (f"/rest/persistence/items/{urllib.parse.quote(sv['leistung'])}?"
            + urllib.parse.urlencode({"starttime": beginn}))
    with urllib.request.urlopen(anfrage(pfad, "application/json"), timeout=60) as r:
        daten = json.load(r).get("data", [])
    punkte = []
    for p in daten:
        w = zahl(p.get("state"))
        if w is not None:
            punkte.append((p["time"] / 1000, w))
    punkte.sort()
    lt = time.localtime()
    return strom_profil(punkte, (lt.tm_year, lt.tm_mon, lt.tm_mday)), len(punkte)


def profil_lesen(cfg, warteschlange):
    """Laeuft im Hintergrund: holt beim Start und jede Nacht die gespeicherten Leistungswerte
    der letzten Tage aus openHAB und berechnet daraus den ueblichen Tagesverlauf."""
    while True:
        try:
            lt = time.localtime()
            profil, anzahl = profil_holen(cfg)
            warteschlange.put(("item", (PROFIL_ITEM, json.dumps(profil) if profil else "")))
            if profil:
                log.info("Üblicher Stromverbrauch berechnet (%d Werte, ganzer Tag %.1f kWh)",
                         anzahl, profil[-1])
            else:
                log.warning("Zu wenig gespeicherte Werte für den üblichen Stromverbrauch")
            # naechste Nacht kurz nach Mitternacht wieder
            morgen = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 10, 0, 0, 0, -1))
            time.sleep(max(600, morgen - time.time()))
        except Exception as e:
            log.warning("Üblicher Stromverbrauch nicht berechenbar: %s", e)
            time.sleep(120)  # z. B. kurz nach dem Hochfahren, WLAN noch nicht da


# --- Hauptschleife ---------------------------------------------------------

def main():
    global OPENHAB
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = config_laden()
    OPENHAB = cfg["openhab"].rstrip("/")
    items = benutzte_items(cfg)

    if "--png" in sys.argv:
        ziel = sys.argv[sys.argv.index("--png") + 1]
        zeilen = None
        if "--abfahrten" in sys.argv:
            import wiener_linien
            e = cfg["wiener_linien"]["abfahrten"]
            abruf = (time.time(), wiener_linien.WienerLinien(ORDNER).abfahrten(e))
            zeilen = wiener_linien.zeilen(e, abruf)
        mit_oh = cfg.get("openhab_aktiv", True)
        z = zustaende_holen(items) if mit_oh else {}
        if mit_oh and cfg["strom_vergleich"]["aktiv"]:
            profil, _ = profil_holen(cfg)
            z[PROFIL_ITEM] = json.dumps(profil) if profil else ""
        # Zum Ausprobieren: --setze Item=Wert (mehrfach moeglich) ueberschreibt Werte
        for i, arg in enumerate(sys.argv):
            if arg == "--setze" and i + 1 < len(sys.argv):
                k, _, v = sys.argv[i + 1].partition("=")
                z[k] = v
        a = anzeige_berechnen(cfg, z, None, zeilen)
        bild_zeichnen(a, time.strftime("%H:%M"), None).save(ziel)
        print(json.dumps(a, ensure_ascii=False, indent=1))
        return

    display = Display()
    g = Gemeinsam(cfg)
    warteschlange = queue.Queue()
    mit_openhab = bool(cfg.get("openhab_aktiv", True))
    if mit_openhab:
        threading.Thread(target=ereignisse_lesen, args=(items, warteschlange), daemon=True).start()
    else:
        log.info("openHAB ausgeschaltet: nur Wiener Linien, Aktualisieren nach Einstellung")
    if open_meteo_noetig(cfg):
        threading.Thread(target=uv_lesen, args=(cfg, warteschlange), daemon=True).start()
    if mit_openhab and cfg["strom_vergleich"]["aktiv"]:
        threading.Thread(target=profil_lesen, args=(cfg, warteschlange), daemon=True).start()
    wl = cfg["wiener_linien"]
    oeffi_wecker = threading.Event()
    if wl["aktiv"] and wl["abfahrten"]:
        threading.Thread(target=abfahrten_lesen, args=(cfg, g, warteschlange, oeffi_wecker),
                         daemon=True).start()
    bt = cfg["bluetooth"]
    if mit_openhab and bt["aktiv"] and bt["adresse"] and bt["item"]:
        import bluetooth_naehe
        g.naehe = bluetooth_naehe.Naehe(bt, lambda wert: zustand_setzen(bt["item"], wert))
        g.naehe.starten()
        log.info("Handy-Erkennung per Bluetooth läuft (%s -> %s)", bt["adresse"], bt["item"])
    elif bt["item"]:
        # Erkennung aus: niemand weiss, ob das Handy da ist, also sicherheitshalber "weg"
        def aus_melden():
            for _ in range(10):
                try:
                    zustand_setzen(bt["item"], "OFF")
                    log.info("Handy-Erkennung aus, %s auf OFF gesetzt", bt["item"])
                    return
                except Exception as e:
                    log.warning("Handy-Zustand nicht auf OFF gesetzt: %s", e)
                    time.sleep(30)
        threading.Thread(target=aus_melden, daemon=True).start()
    # Anzeigedauer je Seite, solange jemand da ist (Abfahrten kuerzer, weniger zu lesen)
    dauer_haupt = max(3, int(cfg.get("anzeige_s", 7)))
    dauer_ab = max(3, int(wl.get("wechsel_s", 5)))
    haupt_an = bool(cfg.get("haupt_aktiv", True))  # aus = nur Abfahrten (und Alarme)
    akt = cfg.get("aktualisieren", {})
    modus = akt.get("modus", "anwesenheit")
    if modus == "anwesenheit" and not mit_openhab:
        modus = akt.get("ersatz", "zeiten")   # ohne openHAB gibt es keinen Bewegungsmelder
    wl_da = bool(wl["aktiv"] and wl["abfahrten"])
    autark_vorher = None
    oh_jemals = False          # openHAB war seit dem Start schon einmal verbunden
    ersatz_vorher = None
    seite_abfahrten = False
    ab_seite = 0               # 0 = Hauptansicht, 1, 2, ... = Abfahrten-Seite (je PRO_SEITE Abfahrten)
    seite_seit = 0.0

    import wlan_einrichtung
    g.wlan = wlan_einrichtung.Wlan(cfg["wlan"], lambda info: warteschlange.put(("wlan", info)))
    g.wlan.starten()

    import weboberflaeche
    weboberflaeche.starten(WEB_PORT, g, warteschlange, display, sys.modules[__name__])
    log.info("Web-Oberfläche auf Port %s", WEB_PORT)

    gezeichnet = None          # zuletzt gezeichnete Anzeige
    letzte_zeichnung = 0.0
    da_vorher = False
    weg_seit = 0.0             # seit wann niemand mehr im Vorzimmer ist
    ankunft_bis = 0.0          # bis wann die geoeffnete Wohnungstuer als "jemand da" gilt
    tuer_vorher = None
    # Nach dem Hochfahren des Pi (nicht nach jedem Neustart des Programms, etwa beim
    # Speichern der Einstellungen) eine Minute WLAN und Adresse zeigen
    def seit_boot():
        try:
            with open("/proc/uptime") as f:
                return float(f.read().split()[0])
        except (OSError, ValueError):
            return 1e9
    wlan_warten = seit_boot() < 300   # bis das WLAN verbunden ist, alle 2 s nachsehen
    wlan_bis = 0.0
    abstand = max(1, int(cfg["messwerte_abstand_min"])) * 60
    leeren_nach = max(0, int(cfg.get("leeren_nach_min", 5))) * 60  # 0 = nie weiß schalten

    while True:
        neu_zeichnen = False
        try:
            # bis zum naechsten Seitenwechsel warten, nicht nach jeder Meldung wieder von vorn
            if da_vorher and seite_seit:
                dauer = dauer_ab if seite_abfahrten else dauer_haupt
                warten = min(30, max(0.2, seite_seit + dauer - time.time()))
            elif da_vorher and ankunft_bis > time.time():
                warten = max(0.2, ankunft_bis - time.time())
            elif gezeichnet is not None and gezeichnet.get("abfahrten") and not da_vorher:
                warten = min(30, max(0.2, weg_seit + ABFAHRTEN_WEG_S - time.time()))
            else:
                warten = 30
            if wlan_bis:
                warten = min(warten, max(0.2, wlan_bis - time.time()))
            if wlan_warten:
                warten = min(warten, 2)
            eintrag = warteschlange.get(timeout=warten)
            while True:
                art, inhalt = eintrag
                with g.sperre:
                    if art == "alle":
                        g.z.update(inhalt)
                    elif art == "item":
                        g.z[inhalt[0]] = inhalt[1]
                    elif art == "verbunden":
                        g.getrennt_seit = None if inhalt else (g.getrennt_seit or time.time())
                    elif art == "neuzeichnen":
                        neu_zeichnen = True
                    elif art == "wlan":
                        g.wlan_zustand = inhalt
                    elif art == "oeffis":
                        g.oeffis = inhalt
                eintrag = warteschlange.get_nowait()
        except queue.Empty:
            pass
        # Hotspot (kein WLAN) oder Wechsel des WLANs: Einrichtungsbild hat Vorrang vor allem
        with g.sperre:
            wz = g.wlan_zustand
        if wz and wz["modus"] in ("hotspot", "verbinde") and not g.aus:
            schluessel = (wz["modus"], wz.get("meldung"), wz.get("hotspot_ssid"))
            if not (gezeichnet and gezeichnet.get("einrichtung") == schluessel):
                try:
                    display.zeigen(einrichtung_bild(wz), True, cfg["drehung"])
                    log.info("Gezeichnet (WLAN-Einrichtung: %s)", wz["modus"])
                    with g.sperre:
                        g.letzte_zeichnung = time.strftime("%H:%M") + " (Hotspot)"
                except Exception:
                    log.exception("Einrichtungsbild nicht gezeichnet")
                gezeichnet = {"einrichtung": schluessel}
            wlan_warten = False
            continue
        if gezeichnet and gezeichnet.get("einrichtung") and wz and wz["modus"] == "wlan":
            wlan_warten = True    # wieder im WLAN: Startbild mit neuer Adresse zeigen
            gezeichnet = None
        if wlan_warten:
            wlan = wlan_info()
            if wlan and wlan["ip"] and wlan["ip"] != "10.42.0.1":
                wlan_warten = False
                log.info("WLAN %s, Signal %s %% / %s dBm, Adresse %s", wlan["ssid"], wlan["prozent"],
                         wlan["dbm"], wlan["ip"])
                try:  # sofort, auch wenn openHAB noch nicht erreichbar ist
                    display.zeigen(wlan_bild(wlan), True, cfg["drehung"])
                    log.info("Gezeichnet (WLAN-Startbild)")
                    gezeichnet, wlan_bis = {"wlan": True}, time.time() + WLAN_START_S
                    with g.sperre:
                        g.letzte_zeichnung = time.strftime("%H:%M") + " (WLAN)"
                except Exception:
                    log.exception("WLAN-Startbild nicht gezeichnet")
            elif seit_boot() >= 300:
                wlan_warten = False
        with g.sperre:
            z, getrennt_seit = dict(g.z), g.getrennt_seit
            # Autark: openHAB aus oder laenger nicht erreichbar -> nur Abfahrten, kein Melder
            if getrennt_seit is None:
                oh_jemals = True
            oh_ok = mit_openhab and oh_jemals and not (
                getrennt_seit and time.time() - getrennt_seit > AUTARK_NACH)
            if mit_openhab and not oh_jemals and time.time() - getrennt_seit <= AUTARK_NACH:
                continue   # kurz nach dem Start: auf openHAB warten
            if oh_ok != autark_vorher:
                if autark_vorher is not None or not oh_ok:
                    log.info("openHAB %s", "wieder da, normaler Betrieb" if oh_ok else
                             "nicht verfügbar: Autark-Modus (nur Abfahrten)")
                autark_vorher = oh_ok
            g.autark = not oh_ok
            anw = cfg["anwesenheit"]
            # Ankunft: Wohnungstuer geht auf, waehrend niemand zu Hause ist. Der Vorzimmer-
            # Melder meldet erst rund 25 s spaeter, so ist das Display schon fertig.
            tuer = z.get(anw.get("tuer", ""))
            if tuer == "OPEN" and tuer_vorher not in (None, "OPEN"):
                try:
                    niemand = float(z.get(anw["personen"]) or 1) == 0
                except ValueError:
                    niemand = False
                if niemand:
                    ankunft_bis = time.time() + ANKUNFT_S
                    log.info("Wohnungstür geöffnet, niemand zu Hause: Ankunft")
            tuer_vorher = tuer
            # Bewegungsmelder nur, wenn openHAB und CCU laufen und der Melder einen Wert hat
            melder_ok = (oh_ok and z.get(anw["vorzimmer"]) in ("ON", "OFF")
                         and (not anw.get("ccu_ok") or z.get(anw["ccu_ok"]) != "OFF"))
            if modus == "anwesenheit" and melder_ok:
                da = z.get(anw["vorzimmer"]) == "ON" or time.time() < ankunft_bis
                ersatz = None
            else:
                ersatz = modus if modus != "anwesenheit" else akt.get("ersatz", "zeiten")
                da = ersatz == "immer" or in_zeiten(akt.get("zeiten"))
            if ersatz != ersatz_vorher:
                log.info("Aktualisieren: %s", {None: "wenn jemand im Vorzimmer ist", "immer": "immer",
                                               "zeiten": "nach Zeitplan"}.get(ersatz, ersatz))
                ersatz_vorher = ersatz
            g.ersatz = ersatz
            g.da = da
        if g.naehe:
            ich = cfg["anwesenheit"]["ich"]
            g.naehe.pausieren(bool(ich) and z.get(ich) == "ON")
        if da:
            oeffi_wecker.set()

        jetzt = time.time()
        betreten = da and not da_vorher
        if da_vorher and not da:
            weg_seit = jetzt
        da_vorher = da

        # Seitenwechsel Hauptansicht <-> Abfahrten, nur solange jemand da ist
        zeilen = g.abfahrt_zeilen() if da else []
        ab_seiten = -(-len(zeilen) // PRO_SEITE)          # aufgerundet
        # Hauptansicht nur mit openHAB (im Autark-Modus nur Abfahrten, sofern eingestellt)
        haupt_jetzt = haupt_an and (oh_ok or not wl_da)
        if not zeilen:
            ab_seite = 0
        elif not haupt_jetzt:
            # nur Abfahrten: zwischen den Abfahrten-Seiten weiterblaettern
            if ab_seite == 0 or betreten or ab_seite > ab_seiten:
                ab_seite, seite_seit = 1, jetzt
            elif ab_seiten > 1 and jetzt - seite_seit >= dauer_ab:
                ab_seite, seite_seit = ab_seite % ab_seiten + 1, jetzt
        elif betreten:
            ab_seite, seite_seit = 0, jetzt
        elif jetzt - seite_seit >= (dauer_ab if ab_seite else dauer_haupt):
            # Hauptansicht -> Abfahrten 1 -> Abfahrten 2 -> ... -> Hauptansicht
            ab_seite, seite_seit = (ab_seite + 1) % (ab_seiten + 1), jetzt
        if ab_seite > ab_seiten:
            ab_seite = 0
        seite_abfahrten = ab_seite > 0
        teil = zeilen[(ab_seite - 1) * PRO_SEITE:ab_seite * PRO_SEITE] if seite_abfahrten else None
        a = anzeige_berechnen(cfg, z, getrennt_seit, teil)

        with g.sperre:
            if a["alarme"] and not g.alarm_seit:
                g.alarm_seit = time.strftime("%H:%M")
            elif not a["alarme"]:
                g.alarm_seit = None
            alarm_seit = g.alarm_seit

        if g.aus:
            continue  # Pi faehrt herunter, das Abschaltbild bleibt stehen

        # Nach dem Start: eine Minute WLAN-Name, Signal und Adresse der Web-Oberflaeche
        if wlan_bis and not a["alarme"] and not neu_zeichnen:
            if jetzt < wlan_bis:
                continue
        if wlan_bis and (jetzt >= wlan_bis or a["alarme"] or neu_zeichnen):
            wlan_bis = 0.0
            if gezeichnet and gezeichnet.get("wlan"):
                gezeichnet = None  # danach wie beim Start voll zeichnen

        # Hauptansicht ausgeschaltet und keine Abfahrten zu zeigen: Display weiss lassen
        if not haupt_jetzt and not seite_abfahrten and not a["alarme"]:
            if wl_da and da and g.oeffis is None:
                continue   # erste Abfahrten kommen gleich, nicht vorher weiss schalten
            if gezeichnet is None or not gezeichnet.get("leer"):
                try:
                    display.zeigen(Image.new("1", (BREITE, HOEHE), 255), gezeichnet is None,
                                   cfg["drehung"], jemand_da=da)
                    log.info("Anzeige weiß (Hauptansicht ausgeschaltet)")
                    gezeichnet, letzte_zeichnung = dict(LEER_ANZEIGE), jetzt
                    with g.sperre:
                        g.letzte_zeichnung = time.strftime("%H:%M") + " (weiß)"
                except Exception:
                    log.exception("Weißschalten fehlgeschlagen")
            continue

        # Veraltete Anzeige weiss schalten: niemand da, kein Alarm, letztes Bild zu alt.
        # Die Abfahrten-Seite (ohne Uhrzeit) schon kurz nach dem Gehen.
        abfahrten_alt = (gezeichnet is not None and gezeichnet.get("abfahrten")
                         and jetzt - weg_seit >= ABFAHRTEN_WEG_S)
        if (gezeichnet is not None and not gezeichnet.get("leer") and not da and not a["alarme"]
                and (abfahrten_alt or (leeren_nach and jetzt - letzte_zeichnung >= leeren_nach))):
            try:
                display.zeigen(Image.new("1", (BREITE, HOEHE), 255), True, cfg["drehung"])
                if abfahrten_alt:
                    log.info("Anzeige weiß geschaltet (Abfahrten, seit %d s niemand da)", ABFAHRTEN_WEG_S)
                else:
                    log.info("Anzeige weiß geschaltet (älter als %d Min, niemand da)", leeren_nach // 60)
                gezeichnet = dict(LEER_ANZEIGE)
                with g.sperre:
                    g.letzte_zeichnung = time.strftime("%H:%M") + " (weiß)"
            except Exception:
                log.exception("Weißschalten fehlgeschlagen")
            continue

        # Aufgeschobene volle Aktualisierung nachholen, wenn niemand mehr da ist (nur noetig,
        # wenn das Weissschalten aus ist, sonst erledigt das Weissschalten das)
        if (not leeren_nach and not da and gezeichnet is not None and not gezeichnet.get("leer")
                and not a["alarme"] and display.teile >= VOLL_NACH_TEILEN
                and jetzt - letzte_zeichnung >= 60):
            try:
                display.auffrischen()
                log.info("Volle Aktualisierung nachgeholt (niemand da)")
            except Exception:
                log.exception("Auffrischen fehlgeschlagen")
            continue

        grund = None
        if gezeichnet is None:
            grund = "Start"
        elif neu_zeichnen:
            grund = "Web-Oberfläche"
        elif (a["alarme"], a["gespeichert"]) != (gezeichnet["alarme"], gezeichnet["gespeichert"]):
            grund = "Alarm"
        elif a["alarme"]:
            pass  # Alarmbild steht schon, Messwerte sind dort nicht zu sehen
        elif da and a != gezeichnet:
            if betreten:
                grund = "jemand im Vorzimmer"
            elif a["abfahrten"] != gezeichnet["abfahrten"]:
                grund = "Abfahrten"
            elif a["abfahrten"]:
                pass  # Abfahrten-Seite zeigt weder Kopf-/Fusszeile noch Messwerte
            elif a["kopf"] != gezeichnet["kopf"] or a["fuss"] != gezeichnet["fuss"]:
                grund = "Kopf-/Fusszeile"
            elif jetzt - letzte_zeichnung >= abstand:
                grund = "Messwerte"
        if not grund:
            continue

        voll = (gezeichnet is None or neu_zeichnen
                or bool(a["alarme"]) != bool(gezeichnet["alarme"]))
        try:
            uhrzeit = time.strftime("%H:%M")
            wie = display.zeigen(bild_zeichnen(a, uhrzeit, alarm_seit), voll, cfg["drehung"], jemand_da=da)
            log.info("Gezeichnet (%s, %s)", grund, wie)
            gezeichnet = a
            letzte_zeichnung = jetzt
            with g.sperre:
                g.letzte_zeichnung = uhrzeit
        except Exception:
            log.exception("Zeichnen fehlgeschlagen")
            time.sleep(10)


if __name__ == "__main__":
    main()
