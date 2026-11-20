"""WLAN-Einrichtung: Hotspot, wenn der Pi nicht in sein WLAN kommt.

Ist das WLAN laenger als `ohne_wlan_s` weg (z. B. Passwort geaendert, neuer Standort),
oeffnet der Pi einen eigenen Hotspot (Standard "Home Display", WPA2, Passwort steht
am Display). Wer sich damit verbindet, findet die Web-Oberflaeche unter
http://10.42.0.1:8080/wlan, waehlt dort ein WLAN und gibt das Passwort ein.

Solange niemand mit dem Hotspot verbunden ist, versucht der Pi alle `erneut_s` Sekunden,
wieder in ein bekanntes WLAN zu kommen (z. B. nach einem Stromausfall startet der Pi
schneller als die Fritzbox).

Alles laeuft ueber NetworkManager (nmcli, per sudo). Passwoerter werden nie geloggt.
"""
import logging
import os
import random
import subprocess
import threading
import time

log = logging.getLogger("openhab-display")

HOTSPOT_NAME = "display-hotspot"     # Name der NetworkManager-Verbindung
HOTSPOT_IP = "10.42.0.1"             # NetworkManager-Standard fuer "shared"
ORDNER = os.path.dirname(os.path.abspath(__file__))
PASSWORT_DATEI = os.path.join(ORDNER, "hotspot_passwort")


def nmcli(*args, timeout=30, sudo=True):
    """nmcli aufrufen. Gibt (Rueckgabewert, Ausgabe) zurueck."""
    befehl = (["sudo", "-n"] if sudo else []) + ["nmcli"] + list(args)
    try:
        r = subprocess.run(befehl, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "Zeitüberschreitung"


def _felder(zeile):
    """Zeile aus 'nmcli -t' trennen (':' im Text ist als '\\:' geschuetzt)."""
    teile, aktuell, i = [], "", 0
    while i < len(zeile):
        c = zeile[i]
        if c == "\\" and i + 1 < len(zeile):
            aktuell += zeile[i + 1]
            i += 2
            continue
        if c == ":":
            teile.append(aktuell)
            aktuell = ""
        else:
            aktuell += c
        i += 1
    teile.append(aktuell)
    return teile


def hotspot_passwort():
    """Passwort des Hotspots, wird einmal zufaellig erzeugt und nur am Pi gespeichert."""
    try:
        with open(PASSWORT_DATEI, encoding="utf-8") as f:
            pw = f.read().strip()
        if len(pw) >= 8:
            return pw
    except FileNotFoundError:
        pass
    zeichen = "abcdefghkmnpqrstuvwxyz23456789"   # ohne leicht verwechselbare Zeichen
    pw = "".join(random.SystemRandom().choice(zeichen) for _ in range(10))
    neu = PASSWORT_DATEI + ".neu"
    with open(neu, "w", encoding="utf-8") as f:
        f.write(pw + "\n")
    os.chmod(neu, 0o600)
    os.replace(neu, PASSWORT_DATEI)
    return pw


class Wlan:
    def __init__(self, cfg, melden):
        """cfg = Abschnitt "wlan" der Einstellungen, melden(info) bekommt den neuen Zustand."""
        self.cfg = cfg
        self.melden = melden
        self.sperre = threading.Lock()
        self.modus = "start"         # start | wlan | hotspot | verbinde
        self.ssid = None             # verbundenes WLAN
        self.meldung = ""            # letzte Meldung fuer Web-Oberflaeche und Display
        self.netze = []              # zuletzt gefundene WLANs: [(ssid, signal, sicherheit)]
        self.netze_zeit = 0.0
        self.weg_seit = None
        self.hotspot_seit = 0.0
        self.letzter_versuch = 0.0
        self._arbeit = threading.Lock()   # nur ein Umschalten gleichzeitig

    # --- Zustand ------------------------------------------------------------
    def zustand(self):
        with self.sperre:
            return {"modus": self.modus, "ssid": self.ssid, "meldung": self.meldung,
                    "hotspot_ssid": self.cfg["hotspot_ssid"], "hotspot_ip": HOTSPOT_IP,
                    "hotspot_passwort": hotspot_passwort() if self.modus == "hotspot" else None}

    def _setzen(self, modus, ssid=None, meldung=None):
        with self.sperre:
            geaendert = modus != self.modus or ssid != self.ssid or (meldung is not None
                                                                     and meldung != self.meldung)
            self.modus, self.ssid = modus, ssid
            if meldung is not None:
                self.meldung = meldung
        if geaendert:
            self.melden(self.zustand())

    @staticmethod
    def aktive_verbindung():
        """(Verbindungsname, Zustand) von wlan0, z. B. ('preconfigured', 'connected')."""
        rc, aus = nmcli("-t", "-f", "DEVICE,STATE,CONNECTION", "dev", sudo=False)
        for zeile in aus.splitlines():
            f = _felder(zeile)
            if len(f) >= 3 and f[0] == "wlan0":
                return f[2], f[1]
        return "", "unbekannt"

    @staticmethod
    def verbundene_ssid():
        rc, aus = nmcli("-t", "-f", "ACTIVE,SSID", "dev", "wifi", "list", "--rescan", "no", sudo=False)
        for zeile in aus.splitlines():
            f = _felder(zeile)
            if len(f) >= 2 and f[0] == "yes":
                return f[1]
        return None

    @staticmethod
    def geraete_am_hotspot():
        """Ist gerade jemand mit dem Hotspot verbunden? (Nachbartabelle von wlan0)"""
        try:
            aus = subprocess.run(["ip", "neigh", "show", "dev", "wlan0"], capture_output=True,
                                 text=True, timeout=5).stdout
        except Exception:
            return False
        return any(("REACHABLE" in z or "DELAY" in z or "PROBE" in z) for z in aus.splitlines())

    # --- Netze suchen -------------------------------------------------------
    def suchen(self):
        """WLANs in der Naehe suchen (geht auch im Hotspot-Betrieb). Dauert rund 5-15 s."""
        rc, aus = nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "yes",
                        timeout=40)
        beste = {}
        for zeile in aus.splitlines():
            f = _felder(zeile)
            if len(f) < 3 or not f[0] or f[0] == self.cfg["hotspot_ssid"]:
                continue
            try:
                signal = int(f[1])
            except ValueError:
                continue
            if f[0] not in beste or signal > beste[f[0]][1]:
                beste[f[0]] = (f[0], signal, f[2])
        netze = sorted(beste.values(), key=lambda n: -n[1])
        with self.sperre:
            if netze or rc == 0:
                self.netze, self.netze_zeit = netze, time.time()
            return list(self.netze)

    # --- Hotspot ------------------------------------------------------------
    def _hotspot_anlegen(self):
        pw = hotspot_passwort()
        nmcli("con", "delete", HOTSPOT_NAME)
        rc, aus = nmcli("con", "add", "type", "wifi", "ifname", "wlan0", "con-name", HOTSPOT_NAME,
                        "autoconnect", "no", "ssid", self.cfg["hotspot_ssid"],
                        "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
                        "ipv4.method", "shared", "ipv6.method", "disabled",
                        "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", pw, "wifi-sec.proto", "rsn",
                        "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp", "wifi-sec.pmf", "disable")
        if rc != 0:
            log.error("Hotspot-Verbindung nicht angelegt: %s", aus.replace(pw, "***"))
        return rc == 0

    def hotspot_starten(self, meldung=None):
        with self._arbeit:
            return self._hotspot_starten(meldung)

    def _hotspot_starten(self, meldung=None):
        # immer neu anlegen, damit Name und Passwort zur Einstellung passen
        if not self._hotspot_anlegen():
            return False
        rc, aus = nmcli("con", "up", HOTSPOT_NAME, timeout=45)
        if rc != 0:
            log.error("Hotspot startet nicht: %s", aus)
            return False
        self.hotspot_seit = self.letzter_versuch = time.time()
        log.info("Hotspot %s gestartet (%s)", self.cfg["hotspot_ssid"], HOTSPOT_IP)
        self._setzen("hotspot", None, meldung)
        return True

    def _hotspot_stoppen(self):
        nmcli("con", "down", HOTSPOT_NAME, timeout=20)

    def _warten_auf_wlan(self, sekunden):
        """Wartet, bis wlan0 mit einem WLAN (nicht dem Hotspot) verbunden ist."""
        ende = time.time() + sekunden
        while time.time() < ende:
            name, stand = self.aktive_verbindung()
            if stand == "connected" and name and name != HOTSPOT_NAME:
                return True
            time.sleep(2)
        return False

    def bekannte_versuchen(self):
        """Hotspot aus, NetworkManager verbindet sich selbst mit einem bekannten WLAN in Reichweite."""
        with self._arbeit:
            self.letzter_versuch = time.time()
            sichtbar = {n[0] for n in self.suchen()}   # geht noch im Hotspot-Betrieb
            bekannte = [(n, ss) for n, ss in self._profile() if ss in sichtbar]
            if not bekannte:
                log.info("Kein bekanntes WLAN in Reichweite, Hotspot bleibt")
                self.letzter_versuch = time.time()
                return False
            log.info("Hotspot kurz aus, versuche %s", ", ".join(ss for _, ss in bekannte))
            self._hotspot_stoppen()
            if self._bekannte_verbinden(bekannte, ""):
                return True
            return self._hotspot_starten()

    def _bekannte_verbinden(self, bekannte, meldung):
        for name, ssid in bekannte:
            rc, aus = nmcli("con", "up", name, timeout=45)
            if rc == 0 and self._warten_auf_wlan(10):
                self.weg_seit = None
                log.info("Im WLAN %s", ssid)
                self._setzen("wlan", ssid, meldung)
                return True
        return False

    # --- Mit einem WLAN verbinden ----------------------------------------
    @staticmethod
    def _profile():
        """Gespeicherte WLAN-Verbindungen [(Name, SSID)], hoechste Prioritaet zuerst."""
        rc, aus = nmcli("-t", "-f", "NAME,TYPE,AUTOCONNECT-PRIORITY", "con", "show")
        liste = []
        for zeile in aus.splitlines():
            f = _felder(zeile)
            if len(f) >= 3 and f[1] == "802-11-wireless" and f[0] != HOTSPOT_NAME:
                rc2, ssid = nmcli("-g", "802-11-wireless.ssid", "con", "show", f[0])
                try:
                    prio = int(f[2])
                except ValueError:
                    prio = 0
                if rc2 == 0 and ssid.strip():
                    liste.append((prio, f[0], ssid.strip()))
        return [(n, ss) for _, n, ss in sorted(liste, key=lambda x: -x[0])]

    def _profil_zu_ssid(self, ssid):
        """Name der gespeicherten NetworkManager-Verbindung fuer dieses WLAN, sonst None."""
        for name, ss in self._profile():
            if ss == ssid:
                return name
        return None

    def verbinden(self, ssid, passwort):
        """Im Hintergrund: Verbindung anlegen bzw. Passwort aendern, dann verbinden.
        Klappt es nicht, bleibt das alte Passwort und der Hotspot geht wieder an."""
        threading.Thread(target=self._verbinden, args=(ssid, passwort), daemon=True).start()

    def _verbinden(self, ssid, passwort):
        with self._arbeit:
            vorher = self.modus
            self._setzen("verbinde", None, f"Verbinde mit {ssid} …")
            log.info("Verbinde mit WLAN %s (über die Web-Oberfläche)", ssid)
            time.sleep(3)  # damit die Antwortseite noch beim Browser ankommt
            profil = self._profil_zu_ssid(ssid)
            altes_pw = None
            neu_angelegt = not profil
            if profil:
                rc, altes_pw = nmcli("-s", "-g", "802-11-wireless-security.psk", "con", "show", profil)
                altes_pw = altes_pw if rc == 0 else None
                args = ["con", "modify", profil, "connection.autoconnect", "yes",
                        "connection.autoconnect-priority", "10"]
                if passwort:
                    args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", passwort]
                rc, aus = nmcli(*args)
            else:
                profil = "wlan-" + "".join(c if c.isalnum() else "-" for c in ssid)[:24]
                args = ["con", "add", "type", "wifi", "ifname", "wlan0", "con-name", profil, "ssid", ssid,
                        "connection.autoconnect", "yes", "connection.autoconnect-priority", "10"]
                if passwort:
                    args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", passwort]
                rc, aus = nmcli(*args)
            if rc != 0:
                fehler = aus.replace(passwort, "***") if passwort else aus
                log.error("WLAN-Verbindung nicht gespeichert: %s", fehler)
                self._nach_fehler(vorher, f"{ssid}: Einstellung nicht möglich")
                return
            if vorher == "hotspot":
                self._hotspot_stoppen()
            rc, aus = nmcli("con", "up", profil, timeout=60)
            if rc == 0 and self._warten_auf_wlan(15):
                self.weg_seit = None
                log.info("Mit WLAN %s verbunden", ssid)
                self._setzen("wlan", ssid, f"Mit {ssid} verbunden.")
                return
            log.warning("Verbindung mit WLAN %s fehlgeschlagen", ssid)
            # Aenderung zuruecknehmen: altes Passwort wieder, neue Verbindung weg
            if neu_angelegt:
                nmcli("con", "delete", profil)
            elif altes_pw and passwort:
                nmcli("con", "modify", profil, "wifi-sec.psk", altes_pw)
            self._nach_fehler(vorher, f"{ssid}: Verbindung fehlgeschlagen")

    def _nach_fehler(self, vorher, meldung):
        """Nach einem Fehlschlag (Sperre _arbeit ist gehalten): bekanntes WLAN, sonst Hotspot."""
        if self._warten_auf_wlan(15):
            self.weg_seit = None
            self._setzen("wlan", self.verbundene_ssid(), meldung)
            return
        sichtbar = {n[0] for n in self.suchen()}
        if self._bekannte_verbinden([(n, ss) for n, ss in self._profile() if ss in sichtbar], meldung):
            return
        self._hotspot_starten(meldung)

    # --- Ueberwachung -------------------------------------------------------
    def _schleife(self):
        start = time.time()
        while True:
            try:
                self._pruefen(start)
            except Exception:
                log.exception("WLAN-Überwachung")
            time.sleep(10)

    def _pruefen(self, start):
        if self._arbeit.locked():
            return
        name, stand = self.aktive_verbindung()
        jetzt = time.time()
        if name == HOTSPOT_NAME:
            if self.modus != "hotspot":   # z. B. nach Neustart des Programms
                self.hotspot_seit = self.letzter_versuch = jetzt
                self._setzen("hotspot")
            if (jetzt - self.letzter_versuch >= int(self.cfg["erneut_s"])
                    and not self.geraete_am_hotspot()):
                self.bekannte_versuchen()
            return
        if stand == "connected" and name:
            self.weg_seit = None
            if self.modus != "wlan":
                self._setzen("wlan", self.verbundene_ssid())
            return
        # nicht verbunden
        if self.weg_seit is None:
            self.weg_seit = jetzt if self.modus != "start" else start
            log.info("WLAN nicht verbunden (%s)", stand)
        if self.cfg["hotspot"] and jetzt - self.weg_seit >= int(self.cfg["ohne_wlan_s"]):
            log.warning("Seit %d s kein WLAN, starte Hotspot", jetzt - self.weg_seit)
            self.hotspot_starten("Kein WLAN gefunden.")

    def starten(self):
        threading.Thread(target=self._schleife, daemon=True).start()
