"""Kontrole geometrii pliku APP w QGIS (GEOS) — zamiast PostGIS walidatora CPI-ADM.

Odpowiada raportowi GML CPI-ADM (``app_gml_validator.py``):

- poprawność geometrii (``ST_IsValid``/``ST_IsValidReason`` → ``isGeosValid``/``validateGeometry``),
- obiekty wychodzące poza granicę aktu ponad tolerancję (``ST_Difference``),
- nakładanie się stref planistycznych ponad tolerancję (``ST_Intersection``),
- luki między strefami a granicą aktu (``ST_Difference(akt, ST_Union(strefy))``).

Tolerancje jak w technicznym profilu POG CPI-ADM (m²): poza aktem 1, nakładanie 0,1, luki 1;
kawałki mniejsze niż 0,01 m² to szum numeryczny i nie są wypisywane.

Szczeliny: kawałek luki, nakładania lub wyjścia poza akt o średniej szerokości (2·pole/obwód)
poniżej 2 cm nie jest zgłaszany osobno, tylko zbiorczo jako informacja. W plikach z Rejestru
współrzędne mają przeważnie 2 miejsca po przecinku, a część wierzchołków 8 (punkty wyliczone
przy cięciu) — sąsiednie strefy nie trafiają w siebie co do milimetra i powstają paski szerokości
ok. 2 mm wzdłuż granic. Ich łączne pole przekracza tolerancję na długich granicach, choć nie są
ani luką, ani nakładaniem w sensie planistycznym. Tolerancja jest jednak liczona z całego pola
przecięcia, nie tylko z jego części szerokich: nakładanie 0,6 m² jest nakładaniem niezależnie
od tego, jak wąski jest pasek.

Warstwy czytane są z pliku GML przez dostawcę OGR. Moduł nie tworzy widżetów ani nie dodaje
nic do projektu, więc może działać w wątku zadania (``QgsTask``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from qgis.core import Qgis, QgsGeometry, QgsSpatialIndex, QgsVectorLayer

from ..core.app_gml import ACT, SPATIAL_CLASSES, ZONE
from ..core.app_rules import ERROR, INFO, WARNING, Finding

#: Opcje sterownika GML: bez zapisu pliku ``.gfs`` obok GML, bez pobierania XSD z sieci,
#: z atrybutami ``xlink:href``/``xlink:title`` jako polami (``status_href``, ``nazwa_title``...).
OGR_GML_OPTIONS = "|option:WRITE_GFS=NO|option:DOWNLOAD_SCHEMA=NO|option:GML_ATTRIBUTES_TO_OGR_FIELDS=YES"

DEFAULT_LIMITS = {"outside_m2": 1.0, "overlap_m2": 0.1, "gap_m2": 1.0, "sliver_width_m": 0.02}
GAP_PIECE_MIN_M2 = 0.01

RULE = "GEOMETRIA"


def mean_width(geometry: QgsGeometry) -> float:
    """Średnia szerokość powierzchni: 2·pole/obwód (dla wąskiego paska ≈ jego szerokość)."""
    perimeter = geometry.length()
    return 2.0 * geometry.area() / perimeter if perimeter > 0 else 0.0


def _polygons(geometry: QgsGeometry) -> QgsGeometry:
    """
    Tylko część powierzchniowa wyniku operacji (bez linii i punktów styku).

    Nie używać ``convertToType(Polygon)`` na całym wyniku: z ``MultiPoint`` (strefy stykające
    się w kilku punktach) QGIS buduje wielokąt rozpięty na tych punktach — fałszywe „nakładanie”.
    """
    if geometry.isNull() or geometry.isEmpty():
        return QgsGeometry()
    if geometry.type() == Qgis.GeometryType.Polygon:
        return geometry
    parts = [part for part in geometry.asGeometryCollection() if part.type() == Qgis.GeometryType.Polygon]
    return QgsGeometry.collectGeometry(parts) if parts else QgsGeometry()


def _split(geometry: QgsGeometry, limits: dict) -> tuple:
    """
    Rozkład wyniku operacji na części: pole całkowite oraz kawałki szerokie i wąskie.

    Kawałki poniżej 0,01 m² są szumem numerycznym i nie są liczone. Wąskie
    (średnia szerokość poniżej ``sliver_width_m``) to paski niedokładności
    wierzchołków — osobno nie są zgłaszane, ale zliczane.

    :return: (pole całkowite, kawałki szerokie, kawałki wąskie).
    """
    polygons = _polygons(geometry)
    if polygons.isEmpty():
        return 0.0, [], []
    total = 0.0
    wide = []
    narrow = []
    for part in polygons.asGeometryCollection():
        if part.area() <= GAP_PIECE_MIN_M2:
            continue
        total += part.area()
        if mean_width(part) < limits["sliver_width_m"]:
            narrow.append(part)
        else:
            wide.append(part)
    return total, wide, narrow


def _m2(area: float) -> str:
    """Pole do komunikatu: 3 miejsca dla małych wartości (0,104 m² vs tolerancja 0,1 m²)."""
    return f"{area:.3f} m²" if area < 10 else f"{area:.2f} m²"


class _Slivers:
    """Zbiorcze szczeliny (wąskie paski) wg rodzaju: liczba, pole, geometria do warstwy błędów."""

    def __init__(self):
        self._parts: Dict[str, List[QgsGeometry]] = {}

    def add(self, key: str, geometry: QgsGeometry) -> None:
        self._parts.setdefault(key, []).append(geometry)

    def count(self, key: str) -> int:
        return len(self._parts.get(key, []))

    def area(self, key: str) -> float:
        return sum(g.area() for g in self._parts.get(key, []))

    def wkt(self, key: str) -> str:
        return QgsGeometry.collectGeometry(self._parts.get(key, [])).asWkt(3)


def gml_layer_uri(path: str, table: str) -> str:
    """Źródło OGR tabeli (klasy APP) z pliku GML."""
    return f"{path}|layername={table}{OGR_GML_OPTIONS}"


@dataclass
class _Item:
    gml_id: Optional[str]
    kind: str
    label: str
    geometry: QgsGeometry
    valid: bool = True


@dataclass
class GeometryResult:
    findings: List[Finding] = field(default_factory=list)
    measurements: Dict[str, float] = field(default_factory=dict)
    #: Układ warstw (np. ``EPSG:2177``) — w nim są WKT w ``findings``.
    crs: str = ""


class Canceled(Exception):
    """Przerwano na żądanie użytkownika."""


def _read(layer: QgsVectorLayer, kind: str, check: Callable[[], None]) -> List[_Item]:
    items = []
    names = layer.fields().names()
    has_id = "gml_id" in names
    has_designation = "oznaczenie" in names
    for feature in layer.getFeatures():
        check()
        gml_id = feature["gml_id"] if has_id else None
        designation = feature["oznaczenie"] if has_designation else None
        label = f"{designation} ({gml_id})" if designation else (gml_id or f"{kind} #{feature.id()}")
        items.append(_Item(gml_id, kind, label, QgsGeometry(feature.geometry())))
    return items


def check_layers(layers: Dict[str, QgsVectorLayer], limits: Optional[dict] = None,
                 progress: Optional[Callable[[float], None]] = None,
                 is_canceled: Optional[Callable[[], bool]] = None) -> GeometryResult:
    """
    Sprawdza geometrię warstw aktu.

    :param layers: klasa APP -> warstwa (``AktPlanowaniaPrzestrzennego`` i klasy z geometrią).
    :param limits: tolerancje (``outside_m2``, ``overlap_m2``, ``gap_m2``).
    :param progress: wywoływane z postępem 0–100.
    :param is_canceled: zwraca ``True``, gdy trzeba przerwać (rzuca :class:`Canceled`).
    """
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    result = GeometryResult()
    out = result.findings

    def check():
        if is_canceled is not None and is_canceled():
            raise Canceled()

    def report(value):
        if progress is not None:
            progress(value)

    def add(severity, message, item: Optional[_Item] = None, wkt=None, measure=None, field_name="geometria"):
        out.append(Finding(RULE, severity, message, object_id=item.gml_id if item else None,
                           kind=item.kind if item else None, field=field_name, wkt=wkt, measure=measure))

    act_layer = layers.get(ACT)
    if act_layer is not None:
        result.crs = act_layer.crs().authid()
    items: Dict[str, List[_Item]] = {}
    kinds = [k for k in (ACT,) + SPATIAL_CLASSES if k in layers]
    for i, kind in enumerate(kinds):
        items[kind] = _read(layers[kind], kind, check)
        if not result.crs:
            result.crs = layers[kind].crs().authid()
        report(30.0 * (i + 1) / max(len(kinds), 1))

    # 1. Poprawność geometrii.
    every = [item for kind in kinds for item in items[kind]]
    for n, item in enumerate(every):
        check()
        geometry = item.geometry
        if geometry.isNull() or geometry.isEmpty():
            item.valid = False
            add(ERROR, "Obiekt nie ma geometrii.", item)
            continue
        if geometry.type() != Qgis.GeometryType.Polygon:
            item.valid = False
            add(ERROR, "Geometria nie jest powierzchnią.", item)
            continue
        if not geometry.isGeosValid():
            item.valid = False
            errors = geometry.validateGeometry(Qgis.GeometryValidationEngine.Geos)
            reason = "; ".join(e.what() for e in errors) or "niepoprawna geometria"
            where = next((e.where() for e in errors if e.hasWhere()), None)
            wkt = QgsGeometry.fromPointXY(where).asWkt() if where is not None else geometry.pointOnSurface().asWkt()
            add(ERROR, f"Geometria jest niepoprawna: {reason}.", item, wkt=wkt)
        if n % 500 == 0:
            report(30 + 15.0 * n / max(len(every), 1))
    report(45)

    acts = [item for item in items.get(ACT, []) if item.valid]
    if not acts:
        if ACT in layers:
            add(ERROR, "Brak poprawnej geometrii aktu — pominięto kontrolę położenia, nakładania i luk.")
        return result
    act = acts[0]
    result.measurements["act_area_m2"] = round(act.geometry.area(), 2)
    engine = QgsGeometry.createGeometryEngine(act.geometry.constGet())
    engine.prepareGeometry()

    # 2. Obiekty poza granicą aktu.
    spatial = [item for kind in SPATIAL_CLASSES for item in items.get(kind, []) if item.valid]
    outside = 0
    slivers = _Slivers()
    for n, item in enumerate(spatial):
        check()
        if engine.contains(item.geometry.constGet()):
            continue
        total, wide, narrow = _split(item.geometry.difference(act.geometry), limits)
        if total > limits["outside_m2"]:
            outside += 1
            shape = QgsGeometry.collectGeometry(wide) if wide else item.geometry.difference(act.geometry)
            add(WARNING, f"Obiekt wychodzi poza granicę aktu: {_m2(total)} "
                         f"(tolerancja {limits['outside_m2']} m²).",
                item, wkt=shape.asWkt(3), measure=round(total, 4))
        else:
            for part in narrow:
                slivers.add("outside", part)
        if n % 500 == 0:
            report(45 + 15.0 * n / max(len(spatial), 1))
    result.measurements["outside_objects"] = outside
    report(60)

    # 3. Nakładanie się stref (indeks przestrzenny + geometria przygotowana).
    zones = [item for item in items.get(ZONE, []) if item.valid]
    result.measurements["zone_count"] = len(zones)
    index = QgsSpatialIndex()
    for i, item in enumerate(zones):
        index.addFeature(i, item.geometry.boundingBox())
    pairs = 0
    for i, item in enumerate(zones):
        check()
        zone_engine = None
        for j in index.intersects(item.geometry.boundingBox()):
            if j <= i:
                continue
            if zone_engine is None:
                zone_engine = QgsGeometry.createGeometryEngine(item.geometry.constGet())
                zone_engine.prepareGeometry()
            other = zones[j]
            if not zone_engine.intersects(other.geometry.constGet()):
                continue
            total, wide, narrow = _split(item.geometry.intersection(other.geometry), limits)
            if total > limits["overlap_m2"]:
                pairs += 1
                shape = QgsGeometry.collectGeometry(wide) if wide else item.geometry.intersection(other.geometry)
                add(WARNING, f"Strefa nakłada się na {other.label}: {_m2(total)} "
                             f"(tolerancja {limits['overlap_m2']} m²).", item, wkt=shape.asWkt(3),
                    measure=round(total, 4))
            else:
                for part in narrow:
                    slivers.add("overlap", part)
        if i % 200 == 0:
            report(60 + 25.0 * i / max(len(zones), 1))
    result.measurements["overlap_pairs"] = pairs
    report(85)

    # 4. Luki między strefami a granicą aktu (tylko gdy plik ma strefy — plan ogólny).
    if zones:
        check()
        covered = QgsGeometry.unaryUnion([item.geometry for item in zones])
        gap = act.geometry.difference(covered)
        total, wide, narrow = _split(gap, limits)
        result.measurements["gap_area_m2"] = round(total, 4)
        result.measurements["gap_pieces"] = len(wide) + len(narrow)
        severity = WARNING if total > limits["gap_m2"] else INFO
        for part in sorted(wide, key=lambda p: -p.area()):
            add(severity, f"Luka między strefami a granicą aktu: {_m2(part.area())} "
                          f"(tolerancja łączna {limits['gap_m2']} m², łącznie {_m2(total)}).",
                wkt=part.asWkt(3), measure=round(part.area(), 4))
        if narrow and total > limits["gap_m2"]:
            # Luka złożona z samych wąskich pasków — pole przekracza tolerancję, więc jest błędem.
            shape = QgsGeometry.collectGeometry(narrow)
            add(WARNING, f"Luka między strefami a granicą aktu: {_m2(total)} "
                         f"(tolerancja łączna {limits['gap_m2']} m², {len(narrow)} wąskich pasków).",
                wkt=shape.asWkt(3), measure=round(total, 4))
        else:
            for part in narrow:
                slivers.add("gap", part)
    width_cm = round(limits["sliver_width_m"] * 100, 1)
    for key, label in (("gap", "luk między strefami a granicą aktu"),
                       ("overlap", "nakładania się stref"),
                       ("outside", "wyjść obiektów poza granicę aktu")):
        if slivers.count(key):
            result.measurements[f"sliver_{key}_m2"] = round(slivers.area(key), 4)
            add(INFO, f"{slivers.count(key)} szczelin {label} o średniej szerokości poniżej {width_cm:g} cm, "
                      f"łącznie {_m2(slivers.area(key))} — niedokładność dopasowania wierzchołków "
                      f"(różna liczba miejsc po przecinku), nie ustalenie planu.",
                wkt=slivers.wkt(key), measure=round(slivers.area(key), 4))
    report(100)
    return result


def check_gml_geometry(path: str, classes, limits: Optional[dict] = None,
                       progress: Optional[Callable[[float], None]] = None,
                       is_canceled: Optional[Callable[[], bool]] = None) -> GeometryResult:
    """
    Kontrole geometrii pliku GML APP (warstwy czytane przez OGR).

    :param path: plik GML.
    :param classes: klasy APP obecne w pliku (z odczytu dokumentu).
    """
    layers = {}
    for kind in (ACT,) + SPATIAL_CLASSES:
        if kind in classes:
            layer = QgsVectorLayer(gml_layer_uri(path, kind), kind, "ogr")
            if layer.isValid():
                layers[kind] = layer
    if ACT in classes and ACT not in layers:
        result = GeometryResult()
        result.findings.append(Finding(RULE, ERROR, "QGIS nie odczytał geometrii aktu z pliku GML."))
        return result
    return check_layers(layers, limits, progress, is_canceled)
