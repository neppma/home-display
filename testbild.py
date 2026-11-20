#!/usr/bin/env python3
"""Testbild fuer das Waveshare 2,13" E-Paper V4 am openHAB-Display.

Zeichnet Rahmen, Titel, Hostname, IP-Adresse, Datum und ein Schachbrett,
zeigt das Bild mit einer vollen Aktualisierung an und legt das Display
danach schlafen (es behaelt das Bild ohne Strom).

Aufruf am Pi:   python3 ~/openhab-display/testbild.py
Loeschen:       python3 ~/openhab-display/testbild.py --leeren
"""

import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "lib"))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from waveshare_epd import epd2in13_V4  # noqa: E402

BREITE, HOEHE = 250, 122  # Querformat
SCHRIFT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
SCHRIFT_FETT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def eigene_ip():
    """IP-Adresse, ueber die der Pi ins Netz geht (ohne etwas zu senden)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.1.1", 80))
        return s.getsockname()[0]
    except OSError:
        return "keine IP"
    finally:
        s.close()


def bild_zeichnen():
    bild = Image.new("1", (BREITE, HOEHE), 255)  # weiss
    d = ImageDraw.Draw(bild)
    gross = ImageFont.truetype(SCHRIFT_FETT, 20)
    klein = ImageFont.truetype(SCHRIFT, 13)

    d.rectangle((0, 0, BREITE - 1, HOEHE - 1), outline=0)
    d.rectangle((0, 0, BREITE - 1, 28), fill=0)
    d.text((8, 3), "openHAB-Display", font=gross, fill=255)

    d.text((8, 36), "Host: " + socket.gethostname(), font=klein, fill=0)
    d.text((8, 54), "IP:   " + eigene_ip(), font=klein, fill=0)
    d.text((8, 72), time.strftime("%d.%m.%Y %H:%M"), font=klein, fill=0)
    d.text((8, 96), "Testbild OK", font=klein, fill=0)

    # Schachbrett rechts unten: zeigt, ob Pixel sauber sitzen
    feld = 8
    x0, y0 = 170, 40
    for zeile in range(9):
        for spalte in range(9):
            if (zeile + spalte) % 2 == 0:
                x, y = x0 + spalte * feld, y0 + zeile * feld
                d.rectangle((x, y, x + feld - 1, y + feld - 1), fill=0)
    return bild


def main():
    epd = epd2in13_V4.EPD()
    epd.init()
    try:
        if "--leeren" in sys.argv:
            epd.Clear(0xFF)
        else:
            epd.display(epd.getbuffer(bild_zeichnen()))
    finally:
        epd.sleep()


if __name__ == "__main__":
    main()
