"""Handy-Naehe per Bluetooth fuer das openHAB-Display.

Der Pi fragt in festen Abstaenden die feste (oeffentliche) Bluetooth-Adresse des
Handys nach seinem Namen. Antwortet es, ist es in der Naehe. Antwortet es eine
einstellbare Zeit lang nicht, gilt es als weg. Das Ergebnis setzt der Pi als
Zustand (ON/OFF) eines openHAB-Items. Er schickt keinen Befehl; was daraus folgt
(z. B. Tuer aufsperren), entscheidet eine Regel in openHAB.

Werkzeug: hcitool aus BlueZ (laeuft ohne Root-Rechte).
- Namensanfrage: `hcitool name <Adresse>` (antwortet ein Geraet nicht, dauert es rund 5 s)
- Suche: `hcitool scan` findet nur sichtbare Geraete. Ein iPhone ist sichtbar,
  solange am iPhone Einstellungen -> Bluetooth offen ist.
"""

import logging
import re
import subprocess
import threading
import time

ADRESSE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
SPERRE = threading.Lock()  # Suche und Anfragen nicht gleichzeitig ueber die Funkschnittstelle

log = logging.getLogger("openhab-display")


def adresse_pruefen(text):
    """'aa-bb-cc-11-22-33' oder 'aabbcc112233' -> 'AA:BB:CC:11:22:33', sonst None."""
    roh = re.sub(r"[^0-9A-Fa-f]", "", text or "").upper()
    if len(roh) != 12:
        return None
    adresse = ":".join(roh[i:i + 2] for i in range(0, 12, 2))
    return adresse if ADRESSE.match(adresse) else None


def namensanfrage(adresse, zeitlimit=15):
    """Name des Geraets oder None, wenn es nicht antwortet."""
    with SPERRE:
        try:
            r = subprocess.run(["hcitool", "name", adresse], capture_output=True, text=True, timeout=zeitlimit)
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("Bluetooth-Anfrage fehlgeschlagen: %s", e)
            return None
    name = r.stdout.strip()
    return name or None


def pegel(adresse):
    """Wie _pegel_einmal, mit einem zweiten Versuch, falls der Funkchip noch mit
    dem Abbau der vorigen Verbindung beschaeftigt war (l2ping bricht dann sofort ab)."""
    beginn = time.time()
    p = _pegel_einmal(adresse)
    if p is None and time.time() - beginn < 2:
        time.sleep(2)
        p = _pegel_einmal(adresse)
    return p


def _pegel_einmal(adresse):
    """Signalpegel des Handys oder None, wenn es nicht erreichbar ist.

    Haelt mit `l2ping` (3 Pings, je 1 s Abstand) kurz eine Verbindung offen und
    liest waehrenddessen den Pegel (`hcitool rssi`). Mit `hcitool cc` allein kommt
    die Verbindung zum iPhone nicht zustande (bleibt im Aufbau haengen), mit
    l2ping schon, auch ohne Koppeln. Braucht CAP_NET_RAW/CAP_NET_ADMIN (im
    systemd-Dienst gesetzt).
    Klassisches Bluetooth meldet keinen dBm-Wert, sondern den Abstand zum
    "guten Empfangsbereich": 0 = gut/nah, negative Zahlen = schwaecher.
    Ergebnis ist der beste von bis zu drei Messwerten.
    """
    with SPERRE:
        ping = None
        try:
            ping = subprocess.Popen(["l2ping", "-c", "3", "-d", "1", adresse],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ende = time.time() + 8
            verbunden = False
            while time.time() < ende and ping.poll() is None:
                con = subprocess.run(["hcitool", "con"], capture_output=True, text=True, timeout=5).stdout
                if adresse in con:
                    verbunden = True
                    break
                time.sleep(0.25)
            werte = []
            if verbunden:
                time.sleep(0.5)
                for _ in range(3):
                    if ping.poll() is not None:
                        break
                    r = subprocess.run(["hcitool", "rssi", adresse], capture_output=True, text=True, timeout=5)
                    m = re.search(r"RSSI return value:\s*(-?\d+)", r.stdout)
                    if m:
                        werte.append(int(m.group(1)))
                    time.sleep(0.7)
            return max(werte) if werte else None
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("Bluetooth-Pegelmessung fehlgeschlagen: %s", e)
            return None
        finally:
            if ping is not None:
                try:
                    ping.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    ping.kill()
                    ping.wait()


def suchen(zeitlimit=40):
    """Sichtbare Geraete in der Naehe: Liste von (Adresse, Name)."""
    with SPERRE:
        try:
            r = subprocess.run(["hcitool", "scan", "--flush"], capture_output=True, text=True, timeout=zeitlimit)
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("Bluetooth-Suche fehlgeschlagen: %s", e)
            return []
    geraete = []
    for zeile in r.stdout.splitlines():
        teile = zeile.strip().split(None, 1)
        if teile and ADRESSE.match(teile[0].upper()):
            name = teile[1].strip() if len(teile) > 1 else ""
            geraete.append((teile[0].upper(), "" if name.lower() == "n/a" else name))
    return geraete


class Naehe:
    """Hintergrund-Thread: Handy anfragen und Aenderungen an openHAB melden."""

    def __init__(self, bt, melden):
        self.adresse = bt["adresse"]
        self.intervall = max(5, int(bt["intervall_s"]))
        self.weg_nach = max(self.intervall, int(bt["weg_nach_s"]))
        self.mindestpegel = bt.get("mindestpegel")  # None = jede Antwort zaehlt
        self.pegel = None             # zuletzt gemessener Pegel
        self.pegel_um = None          # Unix-Zeit dieser Messung
        self.melden = melden          # Funktion(wert) -> setzt den Item-Zustand
        self.zuletzt_gesehen = None   # Unix-Zeit der letzten Antwort
        self.name = None              # zuletzt gemeldeter Geraetename
        self.status = None            # True = in der Naehe, False = weg, None = noch unbekannt
        self.gemeldet = None          # zuletzt an openHAB gemeldeter Zustand
        self.gemeldet_um = 0.0
        # Nur suchen, solange der Nutzer nicht zu Hause ist (spart Strom und CPU).
        # Bis der erste Anwesenheitswert da ist, wird gewartet.
        self.nur_wenn_abwesend = bt.get("nur_wenn_abwesend", True)
        self.pausiert = self.nur_wenn_abwesend
        self._wecker = threading.Event()

    def pausieren(self, zu_hause):
        """Vom Hauptprogramm bei jeder Aenderung von 'anwesend' aufgerufen."""
        neu = self.nur_wenn_abwesend and zu_hause
        if neu != self.pausiert:
            log.info("Handy-Suche %s", "pausiert (du bist zu Hause)" if neu else "läuft (du bist weg)")
        self.pausiert = neu
        if not neu:
            self._wecker.set()

    def starten(self):
        threading.Thread(target=self._schleife, daemon=True).start()

    def _schleife(self):
        while True:
            if self.pausiert:
                # Waehrend der Pause sicherheitshalber "weg" melden: so bleibt kein altes ON
                # stehen, wenn du gehst und das WLAN dich noch kurz als anwesend fuehrt.
                if self.gemeldet is not False:
                    try:
                        self.melden("OFF")
                        self.gemeldet, self.gemeldet_um = False, time.time()
                    except Exception as e:
                        log.warning("Handy-Zustand nicht an openHAB gemeldet: %s", e)
                self.status, self.zuletzt_gesehen = None, None
                self._wecker.wait(60)
                self._wecker.clear()
                continue
            beginn = time.time()
            if self.mindestpegel is None:
                name = namensanfrage(self.adresse)
                if name:
                    self.name = name
                gesehen = bool(name)
            else:
                # Mit Mindestpegel: nur ein ausreichend starkes Signal zaehlt
                p = pegel(self.adresse)
                if p is not None:
                    self.pegel, self.pegel_um = p, time.time()
                gesehen = p is not None and p >= self.mindestpegel
            jetzt = time.time()
            if gesehen:
                self.zuletzt_gesehen = jetzt
            nah = self.zuletzt_gesehen is not None and jetzt - self.zuletzt_gesehen <= self.weg_nach
            if self.status is not None and nah != self.status:
                log.info("Handy %s", "in der Nähe" if nah else "weg")
            self.status = nah
            # Aenderungen sofort melden, sonst alle 10 Minuten zur Sicherheit wiederholen
            if nah != self.gemeldet or jetzt - self.gemeldet_um > 600:
                try:
                    self.melden("ON" if nah else "OFF")
                    self.gemeldet, self.gemeldet_um = nah, jetzt
                except Exception as e:
                    log.warning("Handy-Zustand nicht an openHAB gemeldet: %s", e)
            time.sleep(max(0.5, self.intervall - (time.time() - beginn)))
