"""Stylizacja warstw planu ogólnego gminy (POG) zgodna z rozporządzeniem.

Źródło: rozporządzenie Ministra Rozwoju i Technologii z dnia 8 grudnia 2023 r. w sprawie
projektu planu ogólnego gminy, dokumentowania prac planistycznych w zakresie tego planu
oraz wydawania z niego wypisów i wyrysów (Dz. U. poz. 2758), załącznik nr 2 „Oznaczenia,
nazewnictwo, standardy oraz sposób prezentacji graficznej danych przestrzennych".
Nowelizacja z 22 listopada 2024 r. (Dz. U. poz. 1775) zmienia tylko załącznik nr 1.

Zasady z załącznika nr 2, ust. 1: wartości w milimetrach dotyczą skali 1:10 000 i większej;
w skalach mniejszych zmniejsza się je o 50% — z wyjątkiem odstępów i etykiet.

Obrysy stref, OZS i OUZ rysują osobne warstwy liniowe (``*_linie``): poligony w kaflach są
przycięte do granic kafli, więc obrys poligonu pokazywałby szwy między kaflami.
"""

from typing import Dict, Optional, Tuple

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsFillSymbol,
    QgsLineSymbol,
    QgsLinePatternFillSymbolLayer,
    QgsPalLayerSettings,
    QgsProperty,
    QgsRendererCategory,
    QgsSimpleFillSymbolLayer,
    QgsSimpleLineSymbolLayer,
    QgsSingleSymbolRenderer,
    QgsSymbolLayer,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsVectorLayerSimpleLabeling,
)

#: Skala, od której (dla skal mniejszych) wartości w mm zmniejsza się o 50%.
SCALE_THRESHOLD = 10000

#: Kolor linii i kreskowań: R083 G083 B083.
LINE_COLOR = QColor(83, 83, 83)

#: Strefy planistyczne (zał. 2, poz. 5.1–5.13): symbol -> (nazwa, kolor wypełnienia RGB).
ZONES: Dict[str, Tuple[str, Tuple[int, int, int]]] = {
    "SW": ("strefa wielofunkcyjna z zabudową mieszkaniową wielorodzinną", (154, 124, 100)),
    "SJ": ("strefa wielofunkcyjna z zabudową mieszkaniową jednorodzinną", (217, 180, 115)),
    "SZ": ("strefa wielofunkcyjna z zabudową zagrodową", (255, 230, 171)),
    "SU": ("strefa usługowa", (255, 108, 105)),
    "SH": ("strefa handlu wielkopowierzchniowego", (255, 132, 183)),
    "SP": ("strefa gospodarcza", (175, 136, 222)),
    "SR": ("strefa produkcji rolniczej", (254, 239, 100)),
    "SI": ("strefa infrastrukturalna", (204, 204, 204)),
    "SN": ("strefa zieleni i rekreacji", (187, 245, 139)),
    "SC": ("strefa cmentarzy", (148, 212, 196)),
    "SG": ("strefa górnictwa", (252, 212, 255)),
    "SO": ("strefa otwarta", (227, 255, 199)),
    "SK": ("strefa komunikacyjna", (242, 242, 242)),
}

#: Wypełnienie obszaru objętego planem (poligon ``pog_granica``). Rozporządzenie nie przewiduje
#: wypełnienia — to dodatek wtyczki: przy małych skalach kafle nie zawierają jeszcze stref,
#: a sama przerywana granica nie pokazuje, po której stronie leży obszar planu. Kolor linii
#: R083 G083 B083 z ok. 15% krycia — pod strefami jest niewidoczny.
BOUNDARY_FILL = QColor(83, 83, 83, 40)

#: Kąt kreskowania (od osi x): OZS 0°, OUZ 90° (zał. 2, poz. 3 i 4).
HATCH_ANGLES = {"ozs": 0.0, "ouz": 90.0}

#: Kolejność warstw POG w grupie, od góry: granica, OZS/OUZ, strefy. Linie nad wypełnieniami,
#: obszary OZS/OUZ (kreskowanie) nad kolorowymi strefami, żeby strefy ich nie zasłaniały.
POG_LAYER_ORDER = [
    "pog_granica_linie",
    "pog_ozs_linie",
    "pog_ouz_linie",
    "pog_ozs",
    "pog_ouz",
    "pog_strefy_linie",
    "pog_strefy",
    "pog_granica",
]

#: Najmniejsza skala (mianownik), przy której widać etykiety — przy mniejszych skalach
#: etykiety tysięcy stref zasłoniłyby mapę.
LABELS_MAX_SCALE = 25000


def _scaled(value_mm: float) -> QgsProperty:
    """Wartość w mm zmniejszana o 50% w skalach mniejszych niż 1:10 000."""
    return QgsProperty.fromExpression(
        f"if(@map_scale <= {SCALE_THRESHOLD}, {value_mm}, {value_mm / 2})"
    )


def _line_layer(width_mm: float, dash: Optional[Tuple[float, float]] = None) -> QgsSimpleLineSymbolLayer:
    """
    Linia w kolorze R083 G083 B083.

    :param width_mm: grubość w skali 1:10 000 i większej.
    :param dash: (długość kreski, odstęp) w mm dla linii przerywanej; ``None`` = ciągła.
    """
    layer = QgsSimpleLineSymbolLayer(LINE_COLOR, width_mm)
    layer.setWidthUnit(Qgis.RenderUnit.Millimeters)
    layer.setDataDefinedProperty(QgsSymbolLayer.Property.StrokeWidth, _scaled(width_mm))
    if dash:
        dash_mm, gap_mm = dash
        layer.setUseCustomDashPattern(True)
        layer.setCustomDashPatternUnit(Qgis.RenderUnit.Millimeters)
        layer.setCustomDashVector([dash_mm, gap_mm])
        # Kreska maleje o 50% w mniejszych skalach, odstęp — nie.
        layer.setDataDefinedProperty(
            QgsSymbolLayer.Property.CustomDash,
            QgsProperty.fromExpression(
                f"if(@map_scale <= {SCALE_THRESHOLD}, '{dash_mm};{gap_mm}', '{dash_mm / 2};{gap_mm}')"
            ),
        )
    layer.setPenCapStyle(Qt.PenCapStyle.FlatCap)
    return layer


def line_symbol(sublayer: str) -> Optional[QgsLineSymbol]:
    """Symbol warstwy liniowej POG albo ``None`` dla nieznanej warstwy."""
    if sublayer == "pog_granica_linie":
        # Zał. 2, poz. 1: linia przerywana 3 mm, kreska 14 mm, odstęp 10 mm.
        return QgsLineSymbol([_line_layer(3.0, (14.0, 10.0))])
    if sublayer == "pog_strefy_linie":
        # Zał. 2, poz. 5: obrys stref — linia ciągła 0,5 mm.
        return QgsLineSymbol([_line_layer(0.5)])
    if sublayer in ("pog_ozs_linie", "pog_ouz_linie"):
        # Zał. 2, poz. 3–4: linia przerywana 0,4 mm, kreska 2 mm, odstęp 1 mm.
        return QgsLineSymbol([_line_layer(0.4, (2.0, 1.0))])
    return None


def hatch_symbol(angle: float) -> QgsFillSymbol:
    """Kreskowanie: linie ciągłe 0,4 mm co 2 mm pod kątem ``angle`` (bez wypełnienia tła)."""
    pattern = QgsLinePatternFillSymbolLayer()
    pattern.setLineAngle(angle)
    pattern.setDistance(2.0)  # odstęp — nie maleje w mniejszych skalach
    pattern.setDistanceUnit(Qgis.RenderUnit.Millimeters)
    pattern.setSubSymbol(QgsLineSymbol([_line_layer(0.4)]))
    return QgsFillSymbol([pattern])


def zone_symbol(rgb: Tuple[int, int, int]) -> QgsFillSymbol:
    """Wypełnienie strefy bez obrysu (obrys rysuje warstwa ``pog_strefy_linie``)."""
    fill = QgsSimpleFillSymbolLayer(QColor(*rgb))
    fill.setStrokeStyle(Qt.PenStyle.NoPen)
    return QgsFillSymbol([fill])


def zones_renderer() -> QgsCategorizedSymbolRenderer:
    """Renderer stref planistycznych: kategoria na symbol strefy, kolory z rozporządzenia."""
    categories = [
        QgsRendererCategory(symbol, zone_symbol(rgb), f"{symbol} — {name}")
        for symbol, (name, rgb) in ZONES.items()
    ]
    # Wartości spoza rozporządzenia: białe wypełnienie, żeby były widoczne, ale neutralne.
    categories.append(QgsRendererCategory(None, zone_symbol((255, 255, 255)), "inna strefa"))
    return QgsCategorizedSymbolRenderer("symbol", categories)


def label_settings(field: str = "oznaczenie") -> QgsPalLayerSettings:
    """
    Etykiety oznaczeń (zał. 2, ust. 4): Arial 3 mm, czarne, z białą otoczką 0,5 mm.

    Etykiety nie maleją w mniejszych skalach — są tylko ukryte poniżej 1:25 000.
    """
    text = QgsTextFormat()
    font = text.font()
    font.setFamily("Arial")
    text.setFont(font)
    text.setSize(3.0)
    text.setSizeUnit(Qgis.RenderUnit.Millimeters)
    text.setColor(QColor(0, 0, 0))
    buffer = QgsTextBufferSettings()
    buffer.setEnabled(True)
    buffer.setSize(0.5)
    buffer.setSizeUnit(Qgis.RenderUnit.Millimeters)
    buffer.setColor(QColor(255, 255, 255))
    text.setBuffer(buffer)

    settings = QgsPalLayerSettings()
    settings.fieldName = field
    settings.setFormat(text)
    settings.placement = Qgis.LabelPlacement.Horizontal
    settings.scaleVisibility = True
    settings.minimumScale = LABELS_MAX_SCALE
    settings.maximumScale = 1
    # Kafle dzielą obszar na kawałki — bez tego ta sama etykieta pojawia się na każdym.
    thinning = settings.thinningSettings()
    thinning.setAllowDuplicateRemoval(True)
    thinning.setMinimumDistanceToDuplicate(40)
    thinning.setMinimumDistanceToDuplicateUnit(Qgis.RenderUnit.Millimeters)
    settings.setThinningSettings(thinning)
    return settings


def apply_pog_style(layer, sublayer: str) -> bool:
    """
    Nadaje warstwie POG styl z rozporządzenia.

    :param layer: warstwa wektorowa z kafli.
    :param sublayer: nazwa podwarstwy, np. ``pog_strefy``.
    :return: ``True``, gdy warstwa ma styl POG; ``False`` dla nieznanej podwarstwy.
    """
    symbol = line_symbol(sublayer)
    if symbol is not None:
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        layer.setLabelsEnabled(False)
        return True

    if sublayer == "pog_strefy":
        layer.setRenderer(zones_renderer())
    elif sublayer in ("pog_ozs", "pog_ouz"):
        layer.setRenderer(QgsSingleSymbolRenderer(hatch_symbol(HATCH_ANGLES[sublayer[4:]])))
    elif sublayer == "pog_granica":
        # Linię granicy rysuje ``pog_granica_linie``; poligon dostaje tylko lekkie wypełnienie,
        # bez obrysu (obrys przyciętych do kafli poligonów pokazywałby szwy).
        fill = QgsSimpleFillSymbolLayer(BOUNDARY_FILL)
        fill.setStrokeStyle(Qt.PenStyle.NoPen)
        layer.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol([fill])))
        layer.setLabelsEnabled(False)
        return True
    else:
        return False

    if layer.fields().indexOf("oznaczenie") >= 0:
        layer.setLabeling(QgsVectorLayerSimpleLabeling(label_settings()))
        layer.setLabelsEnabled(True)
    return True


def layer_rank(sublayer: str, is_line: bool) -> int:
    """
    Pozycja warstwy w grupie (mniejsza = wyżej w drzewie = rysowana na wierzchu).

    Znane warstwy POG — według ``POG_LAYER_ORDER``; pozostałe: linie nad wypełnieniami.
    """
    if sublayer in POG_LAYER_ORDER:
        return POG_LAYER_ORDER.index(sublayer) * 10
    return 35 if is_line else 60
