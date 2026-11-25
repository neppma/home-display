"""Abfahrten der Wiener Linien fuer das openHAB-Display.

Datenquelle: Wiener Linien, Open Government Data (CC BY 4.0),
https://www.wienerlinien.at/open-data
- Echtzeit: /ogd_realtime/monitor?stopId=...  (kein Schluessel noetig)
- Linien, Haltepunkte und Streckenverlaeufe als CSV. Daraus baut dieses Modul
  einmal einen kleinen Index (wl_index.json) fuer die Auswahl
  Linie -> Station -> Richtung in der Web-Oberflaeche. Er wird nach 30 Tagen
  neu gebaut.
"""

import collections
import csv
import io
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

BASIS = "https://www.wienerlinien.at/ogd_realtime"
CSV_URL = BASIS + "/doku/ogd/wienerlinien-ogd-{}.csv"
INDEX_ALTER = 30 * 24 * 3600
DATEN_ALTER = 180  # s: aeltere Abfahrtsdaten nicht mehr anzeigen

TYPEN = {
    "ptMetro": "U-Bahn",
    "ptTram": "Straßenbahn",
    "ptTramWLB": "Badner Bahn",
    "ptTrainS": "S-Bahn",
    "ptBusCity": "Bus",
    "ptBusNight": "Nachtbus",
    "ptRufBus": "Rufbus",
}
TYP_REIHENFOLGE = list(TYPEN)

log = logging.getLogger("openhab-display")


def kurzname(endstation):
    """'Stadion U, Handelskai' -> 'Stadion', 'Burggasse, Stadthalle U' -> 'Burggasse'."""
    name = endstation.split(",")[0].strip()
    for endung in (" S U", " U", " S"):
        if name.endswith(endung):
            name = name[: -len(endung)]
    return name.strip()


class WienerLinien:
    def __init__(self, ordner):
        self.index_datei = os.path.join(ordner, "wl_index.json")
        self._index = None
        self._sperre = threading.Lock()

    # --- Index fuer die Auswahl ---

    def _csv(self, name):
        with urllib.request.urlopen(CSV_URL.format(name), timeout=120) as r:
            text = r.read().decode("utf-8-sig")
        zeilen = csv.reader(io.StringIO(text), delimiter=";")
        next(zeilen)  # Kopfzeile
        return zeilen

    def _bauen(self):
        log.info("Baue Index der Wiener Linien ...")
        halt = {z[0]: z[2] for z in self._csv("haltepunkte") if len(z) > 2}
        linien = {}
        for z in self._csv("linien"):
            if len(z) >= 5:
                linien[z[0]] = {"name": z[1], "sort": int(z[2] or 0), "echtzeit": z[3] == "1", "typ": z[4]}
        muster = collections.defaultdict(list)
        for z in self._csv("fahrwegverlaeufe"):
            if len(z) >= 5 and z[0] in linien:
                muster[(z[0], z[1])].append((int(z[2]), z[3]))

        pro_linie = collections.defaultdict(list)
        for (linie_id, _), halte in muster.items():
            halte.sort()
            pro_linie[linie_id].append([h for _, h in halte])

        ergebnis = []
        for linie_id, info in linien.items():
            verlaeufe = sorted(pro_linie.get(linie_id, []), key=len, reverse=True)
            if not verlaeufe:
                continue
            ziele = collections.defaultdict(collections.Counter)  # Haltepunkt -> Endstationen
            haeufigkeit = collections.Counter()  # Station -> in wie vielen Verlaeufen
            for halte in verlaeufe:
                ende = halt.get(halte[-1], "?")
                for h in halte[:-1]:
                    ziele[h][ende] += 1
                haeufigkeit.update({halt[h] for h in halte if h in halt})
            # Selten angefahrene Stationen (Remisenfahrten, Umleitungen) weglassen
            grenze = 0.1 * len(verlaeufe)
            haupt = {n for n, a in haeufigkeit.items() if a >= grenze}
            # Reihenfolge nach dem Verlauf mit den meisten Hauptstationen
            bester = max(verlaeufe, key=lambda v: sum(1 for h in v if halt.get(h) in haupt))
            reihenfolge = []
            for halte in [bester] + verlaeufe:
                for h in halte:
                    name = halt.get(h)
                    if name in haupt and name not in reihenfolge:
                        reihenfolge.append(name)
            stationen = []
            for name in reihenfolge:
                je_ziel = {}
                for h, zaehler in ziele.items():
                    if halt.get(h) != name:
                        continue
                    ende, _ = zaehler.most_common(1)[0]
                    anzahl = sum(zaehler.values())
                    if ende not in je_ziel or anzahl > je_ziel[ende]["anzahl"]:
                        je_ziel[ende] = {"haltepunkt": int(h), "ziel": ende, "anzahl": anzahl}
                richtungen = sorted(je_ziel.values(), key=lambda r: -r["anzahl"])
                if richtungen:
                    stationen.append({"name": name, "richtungen": richtungen})
            ergebnis.append({"name": info["name"], "typ": info["typ"], "sort": info["sort"],
                             "echtzeit": info["echtzeit"], "stationen": stationen})
        ergebnis.sort(key=lambda l: (TYP_REIHENFOLGE.index(l["typ"]) if l["typ"] in TYP_REIHENFOLGE else 99,
                                     l["sort"], l["name"]))
        index = {"stand": time.time(), "linien": ergebnis}
        neu = self.index_datei + ".neu"
        with open(neu, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)
        os.replace(neu, self.index_datei)
        log.info("Index der Wiener Linien fertig: %d Linien", len(ergebnis))
        return index

    def index(self):
        with self._sperre:
            if self._index and time.time() - self._index["stand"] < INDEX_ALTER:
                return self._index
            try:
                with open(self.index_datei, encoding="utf-8") as f:
                    index = json.load(f)
                if time.time() - index["stand"] < INDEX_ALTER:
                    self._index = index
                    return index
            except (FileNotFoundError, ValueError, KeyError):
                pass
            try:
                self._index = self._bauen()
            except Exception:
                log.exception("Index der Wiener Linien konnte nicht gebaut werden")
                if self._index is None:
                    raise
            return self._index

    def linie(self, name):
        for l in self.index()["linien"]:
            if l["name"] == name:
                return l
        return None

    # --- Echtzeit ---

    def abfahrten(self, eintraege):
        """{'haltepunkt|linie': [(Unix-Zeit, barrierefrei, klimatisiert), ...]} fuer die Eintraege."""
        punkte = sorted({int(e["haltepunkt"]) for e in eintraege})
        if not punkte:
            return {}
        url = BASIS + "/monitor?" + urllib.parse.urlencode([("stopId", p) for p in punkte])
        with urllib.request.urlopen(url, timeout=20) as r:
            daten = json.load(r)
        ergebnis = {f"{e['haltepunkt']}|{e['linie']}": [] for e in eintraege}
        for monitor in daten.get("data", {}).get("monitors", []):
            punkt = monitor["locationStop"]["properties"]["attributes"].get("rbl")
            for linie in monitor.get("lines", []):
                schluessel = f"{punkt}|{linie.get('name')}"
                if schluessel not in ergebnis:
                    continue
                for ab in linie.get("departures", {}).get("departure", []):
                    zeit = ab.get("departureTime", {})
                    text = zeit.get("timeReal") or zeit.get("timePlanned")
                    if text:
                        # Angaben zum konkreten Wagen; fehlt "vehicle", gilt die Linie
                        wagen = ab.get("vehicle") or {}
                        bf = bool(wagen.get("barrierFree", linie.get("barrierFree", False)))
                        kl = bool(wagen.get("cooling", False))
                        ergebnis[schluessel].append((datetime.fromisoformat(text).timestamp(), bf, kl))
        for zeiten in ergebnis.values():
            zeiten.sort()
        return ergebnis


def zeilen(eintraege, abruf, jetzt=None, laufen_min=0):
    """Eine Zeile pro Abfahrt fuer die Abfahrten-Seite.

    Liefert Tupel (Linie, Name, Minuten bis zur Abfahrt, Losgehen-Minuten,
    weitere Abfahrten, barrierefrei, klimatisiert, Sekunden bis zur Abfahrt auf 10 s
    abgerundet, fuer die Anzeige "laufen"). Minuten sind None, wenn keine
    Abfahrt erreichbar ist. Barrierefrei/klimatisiert gelten fuer den erreichbaren Wagen.
    Beruecksichtigt die Gehzeit: es zaehlt die naechste Abfahrt, die man noch erreicht.
    laufen_min: so viele Minuten spart man, wenn man laeuft (Wunsch des Nutzers). Eine
    Abfahrt, die man nur laufend erreicht, kommt trotzdem; ihre Losgehen-Minuten sind
    dann negativ (Anzeige "laufen").
    Leere Liste, wenn keine frischen Daten da sind.
    """
    if not abruf:
        return []
    zeitpunkt, daten = abruf
    jetzt = jetzt or time.time()
    if jetzt - zeitpunkt > DATEN_ALTER:
        return []
    ergebnis = []
    for e in eintraege:
        zeiten = daten.get(f"{e['haltepunkt']}|{e['linie']}")
        if zeiten is None:
            continue
        geh = int(e.get("gehzeit_min", 0))
        erreichbar = [(int((t - jetzt) // 60), bf, kl) for t, bf, kl in zeiten]
        erreichbar = [x for x in erreichbar if x[0] >= max(1, geh - laufen_min) or x[0] >= geh]
        if not erreichbar:
            ergebnis.append((e["linie"], e["name"], None, None, (), False, False, None))
            continue
        m, bf, kl = erreichbar[0]
        t = next(t for t, _, _ in zeiten if int((t - jetzt) // 60) == m)
        sek = max(0, int(t - jetzt)) // 10 * 10   # 10er-Schritte: nicht jede Sekunde neu zeichnen
        ergebnis.append((e["linie"], e["name"], m, m - geh, tuple(x[0] for x in erreichbar[1:3]), bf, kl, sek))
    return ergebnis
