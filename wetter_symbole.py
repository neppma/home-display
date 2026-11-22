"""Wettersymbole fuer die Fusszeile des openHAB-Displays.

openHAB (OpenWeatherMap-Binding, Sprache Deutsch) liefert die Wetterlage als
Text, z. B. "Bedeckt" oder "Leichter Regen". kategorie() ordnet den Text einer
von wenigen Gruppen zu, symbol() zeichnet dazu ein kleines Schwarz-Weiss-Bild
(Standard 20 x 20 Pixel, schwarz auf weiss).
"""

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

# Reihenfolge wichtig: die erste passende Gruppe gewinnt
# (z. B. "Gewitter mit Regen" ist Gewitter, nicht Regen).
REGELN = [
    ("gewitter", ("gewitter",)),
    ("sturm", ("tornado", "sturm", "böen", "orkan")),
    ("schneeregen", ("schneeregen", "regen und schnee", "graupel", "eisregen", "hagel")),
    ("schnee", ("schnee",)),
    ("schauer", ("schauer",)),
    ("starkregen", ("starker regen", "sehr starker", "extremer", "heftiger regen", "starkregen")),
    ("niesel", ("niesel",)),
    ("regen", ("regen",)),
    ("nebel", ("nebel", "dunst", "trüb", "rauch", "staub", "sand", "asche")),
    ("bedeckt", ("bedeckt",)),
    ("ueberwiegend", ("überwiegend", "stark bewölkt")),
    ("maessig", ("mäßig", "aufgelockert", "teilweise", "bewölkt")),
    ("wenig", ("ein paar wolken", "leicht bewölkt", "heiter", "vereinzelt")),
    ("klar", ("klar", "sonnig", "wolkenlos")),
]

# Fuer die Vorschlagsseite: welche OpenWeatherMap-Begriffe landen wo
BEISPIELE = {
    "klar": "Klarer Himmel",
    "wenig": "Ein paar Wolken",
    "maessig": "Mäßig bewölkt",
    "ueberwiegend": "Überwiegend bewölkt",
    "bedeckt": "Bedeckt",
    "niesel": "Nieselregen, leichter Nieselregen",
    "regen": "Leichter Regen, Mäßiger Regen",
    "starkregen": "Starker / Sehr starker / Extremer Regen",
    "schauer": "Regenschauer, leichte / heftige Regenschauer",
    "gewitter": "Gewitter (auch mit Regen / Niesel)",
    "schnee": "Schnee, leichter / heftiger Schneefall, Schneeschauer",
    "schneeregen": "Schneeregen, Graupel, Eisregen, Regen und Schnee",
    "nebel": "Nebel, Dunst, trüb, Rauch, Staub, Sand",
    "sturm": "Sturmböen, Tornado",
    "unbekannt": "alles andere",
    "untergang": "Uhrzeit aus openHAB (Astro)",
    "glatteis": "zusätzlich bei Kälte (≤ 1° jetzt oder Frost heute) und Nässe, Eisregen immer",
    "sonnenbrille": "zusätzlich, wenn der UV-Index heute 6 oder mehr erreicht (Open-Meteo)",
    "schirm": "statt Wettersymbol, wenn es jetzt regnet und heute weiter regnen soll",
    "waesche": "Wäsche läuft (Fußzeile: „läuft seit HH:MM“)",
    "waesche_fertig": "Wäsche fertig (Fußzeile schwarz)",
    "blitz": "im Strom-Feld ab 500 W (einstellbar)",
}


REGEN = {"gewitter", "schauer", "starkregen", "niesel", "regen", "schneeregen"}


def kategorie(text):
    t = (text or "").lower()
    for name, woerter in REGELN:
        if any(w in t for w in woerter):
            return name
    return "unbekannt"


# --- Bausteine ----------------------------------------------------------------
# Gezeichnet wird pixelgenau im 20er-Raster: Flaechen als Maske, Umrisse
# entstehen durch Abziehen der um 2 Pixel geschrumpften Maske (immer 2 px dick).

G = 20


def _maske():
    return Image.new("L", (G, G), 0)


def _umriss(maske, dicke=2):
    # mit Rand rechnen, damit Formen am Bildrand auch dort einen Umriss bekommen
    gross = ImageOps.expand(maske, dicke + 1, 0)
    innen = gross.filter(ImageFilter.MinFilter(2 * dicke + 1)).crop(
        (dicke + 1, dicke + 1, dicke + 1 + G, dicke + 1 + G))
    return ImageChops.subtract(maske, innen)


class _Bild:
    def __init__(self):
        self.b = Image.new("L", (G, G), 255)

    def frei(self, maske):        # Flaeche weiss machen (Vordergrund verdeckt Hintergrund)
        self.b.paste(255, (0, 0), maske)

    def schwarz(self, maske):
        self.b.paste(0, (0, 0), maske)

    def form(self, maske, voll=False):
        self.frei(maske.filter(ImageFilter.MaxFilter(3)))  # 1 px Luft zum Hintergrund
        self.schwarz(maske if voll else _umriss(maske))


def _wolke_maske(x, y, w):
    """Wolke: Unterkante y, links x, Breite w (Hoehe rund 0,6 w)."""
    m = _maske()
    d = ImageDraw.Draw(m)
    h = round(w * 0.36)
    d.rounded_rectangle((x, y - h, x + w - 1, y - 1), radius=h // 2, fill=255)
    r1 = round(w * 0.27)
    c1 = x + round(w * 0.58)
    d.ellipse((c1 - r1, y - h - r1, c1 + r1 - 1, y - h + r1 - 1), fill=255)
    r2 = round(w * 0.19)
    c2 = x + round(w * 0.30)
    d.ellipse((c2 - r2, y - h - r2 + 2, c2 + r2 - 1, y - h + r2 + 1), fill=255)
    return m


def _kreis_maske(cx, cy, r):
    m = _maske()
    ImageDraw.Draw(m).ellipse((cx - r, cy - r, cx + r - 1, cy + r - 1), fill=255)
    return m


def _sonne(bild, cx, cy, r, strahlen=True):
    if strahlen:
        m = _maske()
        d = ImageDraw.Draw(m)
        a = r + 2           # Abstand der Strahlen vom Rand
        d.rectangle((cx - 1, cy - r - a - 1, cx, cy - r - 2), fill=255)     # oben
        d.rectangle((cx - 1, cy + r + 1, cx, cy + r + a), fill=255)         # unten
        d.rectangle((cx - r - a - 1, cy - 1, cx - r - 2, cy), fill=255)     # links
        d.rectangle((cx + r + 1, cy - 1, cx + r + a, cy), fill=255)         # rechts
        e = round((r + 2) * 0.72)
        for sx, sy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
            px, py = cx + sx * (e + 1) - (1 if sx < 0 else 0), cy + sy * (e + 1) - (1 if sy < 0 else 0)
            d.rectangle((min(px, px - sx), min(py, py - sy), max(px, px - sx), max(py, py - sy)), fill=255)
        bild.schwarz(m)
    bild.form(_kreis_maske(cx, cy, r))


def _wolke(bild, x, y, w, voll=False):
    bild.form(_wolke_maske(x, y, w), voll)


def _striche(bild, punkte, lang=3):
    """Schraege Regenstriche, 2 px breit."""
    m = _maske()
    d = ImageDraw.Draw(m)
    for px, py in punkte:
        for i in range(lang):
            d.rectangle((px - i // 2, py + i, px - i // 2 + 1, py + i), fill=255)
    bild.schwarz(m)


def _punkte(bild, punkte):
    m = _maske()
    d = ImageDraw.Draw(m)
    for px, py in punkte:
        d.rectangle((px, py, px + 1, py + 1), fill=255)
    bild.schwarz(m)


def _flocken(bild, punkte):
    """Kleine Schneeflocke als Plus mit Mittelpunkt (5 x 5)."""
    m = _maske()
    d = ImageDraw.Draw(m)
    for cx, cy in punkte:
        d.line((cx - 2, cy, cx + 2, cy), fill=255)
        d.line((cx, cy - 2, cx, cy + 2), fill=255)
        d.point(((cx - 1, cy - 1), (cx + 1, cy - 1), (cx - 1, cy + 1), (cx + 1, cy + 1)), fill=255)
    bild.schwarz(m)


# --- Symbole ----------------------------------------------------------------

def _klar(b):
    _sonne(b, 10, 10, 5)


def _wenig(b):
    _sonne(b, 8, 8, 4)
    _wolke(b, 8, 20, 12)


def _maessig(b):
    _sonne(b, 7, 7, 4)
    _wolke(b, 3, 20, 17)


def _ueberwiegend(b):
    _sonne(b, 14, 6, 4, strahlen=False)
    _wolke(b, 0, 19, 19)


def _bedeckt(b):
    _wolke(b, 7, 12, 13)
    _wolke(b, 0, 19, 17, voll=True)


def _regenwolke(b, voll=False):
    _wolke(b, 0, 13, 20, voll=voll)


def _niesel(b):
    _regenwolke(b)
    _punkte(b, ((4, 15), (9, 16), (14, 15), (6, 18), (11, 18), (16, 18)))


def _regen(b):
    _regenwolke(b)
    _striche(b, ((5, 15), (10, 16), (15, 15)), lang=4)


def _starkregen(b):
    _regenwolke(b, voll=True)
    _striche(b, ((3, 14), (7, 15), (11, 14), (15, 15), (18, 14)), lang=5)


def _schauer(b):
    _sonne(b, 6, 6, 4)
    _wolke(b, 4, 15, 16)
    _striche(b, ((9, 16), (14, 16)), lang=4)


def _gewitter(b):
    _regenwolke(b)
    m = _maske()
    ImageDraw.Draw(m).polygon(((11, 7), (6, 14), (9, 14), (7, 20), (14, 12), (11, 12), (13, 7)), fill=255)
    rand = m.filter(ImageFilter.MaxFilter(3))
    b.frei(rand)
    b.schwarz(m)


def _schnee(b):
    _regenwolke(b)
    _flocken(b, ((4, 17), (10, 17), (16, 17)))


def _schneeregen(b):
    _regenwolke(b)
    _striche(b, ((5, 15), (14, 15)), lang=4)
    _flocken(b, ((9, 17), (17, 18)))


def _nebel(b):
    m = _maske()
    d = ImageDraw.Draw(m)
    for y, a, e in ((3, 3, 16), (7, 0, 14), (11, 5, 19), (15, 1, 13)):
        d.rectangle((a, y, e, y + 1), fill=255)
    b.schwarz(m)


def _sturm(b):
    m = _maske()
    d = ImageDraw.Draw(m)
    d.rectangle((0, 7, 12, 8), fill=255)
    d.arc((10, 1, 17, 8), 180, 100, fill=255, width=2)
    d.rectangle((0, 11, 17, 12), fill=255)
    d.rectangle((0, 15, 9, 16), fill=255)
    d.arc((7, 15, 14, 22), 260, 180, fill=255, width=2)
    b.schwarz(m)


def _unbekannt(b):
    m = _maske()
    d = ImageDraw.Draw(m)
    d.arc((5, 1, 14, 10), 180, 90, fill=255, width=2)
    d.rectangle((9, 10, 10, 13), fill=255)
    d.rectangle((9, 16, 10, 17), fill=255)
    b.schwarz(m)


def _untergang(b):
    """Halbe Sonne ueber dem Horizont, darunter Pfeil nach unten."""
    m = _maske()
    d = ImageDraw.Draw(m)
    d.ellipse((5, 5, 14, 14), fill=255)
    d.rectangle((0, 10, G - 1, G - 1), fill=0)          # untere Haelfte weg
    ring = _umriss(m)
    s = _maske()
    d = ImageDraw.Draw(s)
    d.rectangle((9, 0, 10, 2), fill=255)                  # Strahlen
    d.rectangle((1, 8, 3, 9), fill=255)
    d.rectangle((16, 8, 18, 9), fill=255)
    d.rectangle((3, 2, 4, 3), fill=255)
    d.rectangle((15, 2, 16, 3), fill=255)
    d.rectangle((0, 11, G - 1, 12), fill=255)            # Horizont
    d.rectangle((9, 14, 10, 18), fill=255)                # Pfeil
    d.polygon(((6, 16), (13, 16), (9.5, 19.5)), fill=255)
    b.schwarz(ring)
    b.schwarz(s)


def _schirm(b):
    """Regenschirm, weiss auf schwarzem Feld (faellt sofort auf)."""
    b.b.paste(0, (0, 0, G, G))
    m = _maske()
    d = ImageDraw.Draw(m)
    d.pieslice((1, 2, 18, 17), 180, 360, fill=255)            # Dach
    for x in (5, 10, 14):                                       # Bogen-Kanten unten
        d.ellipse((x - 2, 8, x + 2, 11), fill=0)
    d.rectangle((9, 9, 10, 16), fill=255)                      # Stiel
    d.arc((5, 13, 10, 18), 0, 180, fill=255, width=2)          # Griff
    d.rectangle((9, 0, 10, 2), fill=255)                       # Spitze
    b.b.paste(255, (0, 0), m)


def _sonnenbrille(b):
    """Sonnenbrille, weiss auf schwarzem Feld (wie der Schirm: Hinweis zum Mitnehmen)."""
    b.b.paste(0, (0, 0, G, G))
    m = _maske()
    d = ImageDraw.Draw(m)
    d.rectangle((0, 6, G - 1, 7), fill=255)                   # Buegel oben
    d.rounded_rectangle((1, 6, 8, 13), radius=3, fill=255)    # Glas links
    d.rounded_rectangle((11, 6, 18, 13), radius=3, fill=255)  # Glas rechts
    d.rectangle((8, 8, 11, 9), fill=255)                      # Steg
    b.b.paste(255, (0, 0), m)
    s = _maske()
    ImageDraw.Draw(s).line((3, 9, 5, 7), fill=255)            # Glanz
    ImageDraw.Draw(s).line((13, 9, 15, 7), fill=255)
    b.b.paste(0, (0, 0), s)


def _glatteis(b):
    """Warndreieck mit Schneeflocke, weiss auf schwarzem Feld."""
    b.b.paste(0, (0, 0, G, G))
    m = _maske()
    ImageDraw.Draw(m).polygon(((10, 0), (19.5, 18), (0.5, 18)), fill=255)
    b.b.paste(255, (0, 0), m)
    s = _maske()
    d = ImageDraw.Draw(s)
    cx, cy = 10, 12
    d.rectangle((cx - 4, cy, cx + 4, cy), fill=255)
    d.rectangle((cx, cy - 4, cx, cy + 4), fill=255)
    d.line((cx - 3, cy - 3, cx + 3, cy + 3), fill=255)
    d.line((cx - 3, cy + 3, cx + 3, cy - 3), fill=255)
    b.b.paste(0, (0, 0), s)


def _waschmaschine(b, invers=False, haken=False):
    """Frontlader. haken = Waesche fertig (Haken statt Trommel), invers = weiss auf schwarz."""
    vg, hg = (255, 0) if invers else (0, 255)
    b.b.paste(hg, (0, 0, G, G))
    d = ImageDraw.Draw(b.b)
    if invers:
        d.rounded_rectangle((3, 1, 16, 18), radius=2, outline=vg, width=2)
    else:
        d.rounded_rectangle((2, 0, 17, 19), radius=2, outline=vg, width=2)
    d.line((3, 5, 16, 5), fill=vg)                 # Bedienleiste
    d.rectangle((5, 2, 6, 3), fill=vg)             # Knopf
    d.rectangle((12, 2, 14, 3), fill=vg)           # Anzeige
    if haken:
        d.line((6, 12, 9, 15), fill=vg, width=2)
        d.line((9, 15, 14, 8), fill=vg, width=2)
    else:
        d.ellipse((5, 7, 14, 16), outline=vg, width=2)  # Tuer
        d.line((7, 12, 8, 11), fill=vg)                 # Wasser
        d.line((8, 11, 10, 13), fill=vg)
        d.line((10, 13, 12, 11), fill=vg)


def _blitz(b):
    """Blitz fuer hohen Stromverbrauch."""
    pts = ((12, 0), (3, 11), (9, 11), (7, 19), (16, 7), (10, 7))
    ImageDraw.Draw(b.b).polygon(pts, fill=0)


ZEICHNER = {
    "waesche": _waschmaschine,                                          # Waesche laeuft
    "waesche_fertig": lambda b: _waschmaschine(b, invers=True, haken=True),
    "waesche_haken": lambda b: _waschmaschine(b, haken=True),          # fertig, in schwarzer Zeile
    "blitz": _blitz,
    "glatteis": _glatteis,
    "sonnenbrille": _sonnenbrille,
    "schirm": _schirm,
    "untergang": _untergang,
    "klar": _klar, "wenig": _wenig, "maessig": _maessig, "ueberwiegend": _ueberwiegend,
    "bedeckt": _bedeckt, "niesel": _niesel, "regen": _regen, "starkregen": _starkregen,
    "schauer": _schauer, "gewitter": _gewitter, "schnee": _schnee, "schneeregen": _schneeregen,
    "nebel": _nebel, "sturm": _sturm, "unbekannt": _unbekannt,
}


def symbol(name):
    """Symbol als 1-Bit-Bild, 20 x 20 Pixel, schwarz auf weiss."""
    b = _Bild()
    ZEICHNER.get(name, _unbekannt)(b)
    return b.b.convert("1", dither=Image.NONE)


# --- Kleidung fuer das Aussentemperatur-Feld (32 x 34 Pixel) ----------------

KLEIDUNG = ("tshirt", "pullover", "jacke", "winterjacke")


def kleidung_fuer(temperatur, grenzen=(20, 12, 3)):
    """Temperatur -> Kleidungsstueck. grenzen = ab T-Shirt, ab Pullover, ab Jacke."""
    for name, grenze in zip(KLEIDUNG, grenzen):
        if temperatur >= grenze:
            return name
    return "winterjacke"


def _oberteil(d, x, y, lang, gefuellt=False, h=27):
    if lang:
        pts = [(x+8, y), (x+12, y+3), (x+16, y), (x+24, y+3), (x+28, y+22), (x+23, y+23), (x+21, y+11),
               (x+21, y+h), (x+3, y+h), (x+3, y+11), (x+1, y+23), (x-4, y+22), (x, y+3)]
    else:
        pts = [(x+8, y), (x+12, y+3), (x+16, y), (x+24, y+3), (x+28, y+10), (x+22, y+13), (x+21, y+11),
               (x+21, y+h), (x+3, y+h), (x+3, y+11), (x+2, y+13), (x-4, y+10), (x, y+3)]
    d.polygon(pts, fill=0 if gefuellt else 255)
    d.line(pts + [pts[0]], fill=0, width=2)


def kleidung(name):
    """Kleidungsstueck als 1-Bit-Bild (hoechstens 32 x 30), schwarz auf weiss,
    auf den Inhalt zugeschnitten."""
    bild = Image.new("1", (34, 40), 255)
    d = ImageDraw.Draw(bild)
    x, y = 5, 5
    if name == "tshirt":
        _oberteil(d, x, y, lang=False)
    elif name == "pullover":
        _oberteil(d, x, y, lang=True)
        d.line((x + 3, y + 23, x + 21, y + 23), fill=0, width=2)        # Bund
    elif name == "jacke":
        _oberteil(d, x, y, lang=True)
        d.line((x + 12, y + 3, x + 12, y + 27), fill=0, width=2)        # Reissverschluss
        d.line((x + 6, y + 15, x + 9, y + 15), fill=0, width=2)         # Taschen
        d.line((x + 15, y + 15, x + 18, y + 15), fill=0, width=2)
    else:
        _oberteil(d, x, y, lang=True, gefuellt=True)                    # dicke Winterjacke: schwarz
        d.line((x + 12, y + 4, x + 12, y + 25), fill=255, width=2)      # Reissverschluss hell
        d.line((x + 5, y + 15, x + 9, y + 15), fill=255, width=2)       # Taschen hell
        d.line((x + 15, y + 15, x + 19, y + 15), fill=255, width=2)
    schwarz = Image.eval(bild.convert("L"), lambda v: 255 - v)
    return bild.crop(schwarz.getbbox())


# --- Kleine Wagen-Symbole fuer die Abfahrten-Seite (12 x 12 Pixel) ----------

def wagen_symbol(name):
    """'rollstuhl' = Wagen ist barrierefrei, 'klima' = Wagen ist klimatisiert
    (Schneeflocke). Schwarz auf weiss, 1 Bit."""
    b = Image.new("1", (12, 12), 255)
    d = ImageDraw.Draw(b)
    if name == "rollstuhl":
        d.rectangle((4, 0, 5, 1), fill=0)                      # Kopf
        d.line(((4, 3), (4, 7), (8, 7), (10, 11)), fill=0, width=2)
        d.line(((4, 4), (7, 4)), fill=0)                       # Arm
        d.arc((0, 5, 7, 11), 90, 360, fill=0)                  # Rad
    else:
        d.line(((6, 0), (6, 11)), fill=0)
        d.line(((1, 3), (11, 9)), fill=0)
        d.line(((1, 9), (11, 3)), fill=0)
        d.point(((5, 1), (7, 1), (5, 10), (7, 10)), fill=0)
    return b


def laeufer():
    """Laufende Person (18 x 22), schwarz auf weiss: Abfahrt nur noch laufend erreichbar."""
    b = Image.new("1", (18, 22), 255)
    d = ImageDraw.Draw(b)
    d.ellipse((10, 0, 14, 4), fill=0)                              # Kopf
    d.line(((11, 6), (7, 13)), fill=0, width=3)                    # Rumpf, nach vorn geneigt
    d.line(((10, 7), (14, 10), (17, 8)), fill=0, width=2)          # vorderer Arm
    d.line(((10, 7), (5, 8), (2, 11)), fill=0, width=2)            # hinterer Arm
    d.line(((7, 13), (12, 16), (11, 21)), fill=0, width=2)         # vorderes Bein
    d.line(((7, 13), (4, 18), (0, 18)), fill=0, width=2)           # hinteres Bein
    return b
