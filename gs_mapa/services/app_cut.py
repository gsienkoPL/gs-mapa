"""Wycięcie obszaru z zasięgu aktu APP (QGIS/GEOS) — geometria bez zapisu plików.

Plan miejscowy publikuje granicę aktu (``zasiegPrzestrzenny``) i rysunki. Gdy część obszaru
przechodzi do nowego planu, granica się zmniejsza: powstaje dziura (obszar wewnątrz), wnęka
(wcięcie od granicy) albo akt dzieli się na dwie części. Ten moduł liczy wyłącznie nową
geometrię (``E′ = E − W``); decyzje o wersjach i zapisie plików podejmują ``core/app_versioning.py``
i kreator.

Zasady:

- wierzchołki granicy biorą się z pliku 1:1 — bez zaokrąglania i bez bufferowania linii cięcia
  (``inset_m`` to świadome „cofnięcie granicy o X m”, domyślnie 0);
- pierścienie wewnętrzne (dziury) i wiele części są zachowywane — ``zasiegPrzestrzenny``
  to ``gml:MultiSurface``;
- kawałki mniejsze niż ``min_piece_m2`` to szum numeryczny po różnicy geometrii i są odrzucane
  (ten sam próg co ``GAP_PIECE_MIN_M2`` w ``services/app_geometry.py``);
- wycięcie całego obszaru to nie „nowa wersja aktu”, tylko jego wygaszenie — dlatego kończy się
  wyjątkiem :class:`EmptyCutError`, a nie pustą geometrią.

Pola i progi liczone są w metrach, więc układ pliku musi być metryczny (EPSG:2176–2180);
dla układu geograficznego moduł zwraca ostrzeżenie, bo metry przestają mieć sens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
)

from ..core.app_writer import Polygon
from .app_creator import polygons_of
from .app_geometry import GAP_PIECE_MIN_M2

#: Komunikat pustego wycięcia — wspólny dla usługi, okna i testów.
EMPTY_CUT_MESSAGE = "wycięcie obejmuje cały obszar aktu — to wygaszenie aktu, nie nowa wersja"
#: Udział pola, od którego ubytek jest wart ostrzeżenia (60%).
BIG_LOSS_SHARE = 0.6
#: Dokładność ``QgsGeometry.buffer`` przy cofaniu granicy (segmenty na ćwiartkę okręgu).
BUFFER_SEGMENTS = 8


class EmptyCutError(ValueError):
    """Po wycięciu nie zostaje obszar — to wygaszenie aktu, a nie jego nowa wersja."""


@dataclass
class ExtentCut:
    """Wynik wycięcia zasięgu aktu: nowa granica i liczby do raportu w oknie."""

    #: Nowy zasięg: lista wieloboków, każdy jako lista pierścieni w układzie pliku.
    extent: List[Polygon]
    before_area: float
    after_area: float
    removed_area: float
    parts_before: int
    parts_after: int
    #: Liczba pierścieni wewnętrznych (dziur) w nowym zasięgu.
    holes: int
    warnings: List[str] = field(default_factory=list)


def cut_extent(extent: Sequence[Polygon], cut: Sequence[Polygon], crs: int,
               inset_m: float = 0.0, min_piece_m2: float = GAP_PIECE_MIN_M2,
               cut_crs: Optional[int] = None) -> ExtentCut:
    """
    Odejmuje wycięcie od zasięgu aktu i zwraca nową granicę.

    :param extent: zasięg aktu (lista wieloboków jako listy pierścieni, układ ``crs``).
    :param cut: wycięcie (lista wieloboków jako listy pierścieni; układ ``cut_crs`` albo ``crs``).
    :param crs: układ pliku aktu (EPSG:2176–2180).
    :param inset_m: cofnięcie granicy wycięcia o tyle metrów (0 = dokładnie po wierzchołkach).
    :param min_piece_m2: kawałki mniejsze niż tyle m² są odrzucane jako szum numeryczny.
    :param cut_crs: układ wycięcia, gdy różni się od układu aktu (np. warstwa projektu).
    :raises EmptyCutError: po wycięciu nie zostaje żaden obszar.
    """
    target = QgsCoordinateReferenceSystem.fromEpsgId(int(crs))
    if not target.isValid():
        raise ValueError(f"nieznany układ odniesienia EPSG:{crs}")
    warnings: List[str] = []
    if target.isGeographic():
        warnings.append("układ odniesienia jest geograficzny — pola i progi liczone są w stopniach, nie w metrach")

    source = _geometry_of(extent)
    if source.isEmpty():
        raise ValueError("zasięg aktu jest pusty — nie ma czego wycinać")
    source = _valid(QgsGeometry(source), "zasięg aktu", warnings)

    cutter = _geometry_of(cut)
    if cutter.isEmpty():
        raise ValueError("wycięcie nie ma geometrii")
    if cut_crs is not None and int(cut_crs) != int(crs):
        cutter.transform(QgsCoordinateTransform(QgsCoordinateReferenceSystem.fromEpsgId(int(cut_crs)),
                                                target, QgsProject.instance()))
    cutter = _valid(cutter, "wycięcie", warnings)

    if inset_m and float(inset_m) > 0:
        inset = cutter.buffer(-float(inset_m), BUFFER_SEGMENTS)
        if inset.isEmpty() or inset.area() <= 0:
            raise ValueError(f"cofnięcie granicy o {float(inset_m):g} m usuwa całe wycięcie")
        cutter = inset

    before_area = source.area()
    parts_before, _ = _count(source)

    result = source.difference(cutter)
    if result.isEmpty() or result.area() <= 0:
        raise EmptyCutError(EMPTY_CUT_MESSAGE)
    result = _valid(result, "wynik wycięcia", warnings)
    result = _drop_slivers(result, float(min_piece_m2), warnings)

    after_area = result.area()
    removed_area = before_area - after_area
    parts_after, holes = _count(result)
    if before_area > 0 and removed_area / before_area > BIG_LOSS_SHARE:
        warnings.append(f"wycięcie zabiera {removed_area / before_area:.0%} obszaru aktu "
                        f"({removed_area:,.1f} m² z {before_area:,.1f} m²) — sprawdź geometrię wycięcia")
    return ExtentCut(extent=polygons_of(result), before_area=before_area, after_area=after_area,
                     removed_area=removed_area, parts_before=parts_before, parts_after=parts_after,
                     holes=holes, warnings=warnings)


def _geometry_of(polygons: Sequence[Polygon]) -> QgsGeometry:
    """
    Wieloboki (listy pierścieni) jako jedna geometria QGIS.

    Części są scalane ``unaryUnion``, bo wielopowierzchnia ze stykającymi się albo zachodzącymi
    częściami (tak wygląda kilka wycięć z jednej warstwy) nie jest poprawna dla GEOS.
    """
    parts: List[QgsGeometry] = []
    for polygon in polygons:
        rings = [[QgsPointXY(float(x), float(y)) for x, y in ring] for ring in polygon if len(ring) >= 3]
        if not rings:
            continue
        geometry = QgsGeometry.fromPolygonXY(rings)
        if not geometry.isEmpty():
            parts.append(geometry)
    if not parts:
        return QgsGeometry()
    if len(parts) == 1:
        return parts[0]
    return QgsGeometry.unaryUnion(parts)


def _valid(geometry: QgsGeometry, what: str, warnings: List[str]) -> QgsGeometry:
    """Geometria poprawna dla GEOS; niepoprawną naprawia ``makeValid`` i o tym mówi."""
    if geometry.isGeosValid():
        return geometry
    repaired = geometry.makeValid()
    warnings.append(f"{what}: geometria nie jest poprawna (GEOS) — naprawiono makeValid, sprawdź wynik "
                    f"przed zapisem")
    if repaired is not None and not repaired.isEmpty():
        return repaired
    return geometry


def _parts(geometry: QgsGeometry) -> List[QgsGeometry]:
    """Części geometrii wielobokowej (każda z własnymi pierścieniami wewnętrznymi)."""
    if geometry is None or geometry.isEmpty():
        return []
    if geometry.isMultipart():
        return [QgsGeometry(part) for part in geometry.asGeometryCollection()]
    return [geometry]


def _count(geometry: QgsGeometry) -> Tuple[int, int]:
    """``(liczba części, liczba pierścieni wewnętrznych)``."""
    parts = _parts(geometry)
    holes = 0
    for part in parts:
        polygon = part.asPolygon()
        if polygon:
            holes += max(0, len(polygon) - 1)
    return len(parts), holes


def _drop_slivers(geometry: QgsGeometry, min_piece_m2: float, warnings: List[str]) -> QgsGeometry:
    """Odrzuca kawałki mniejsze niż ``min_piece_m2`` (szum po różnicy geometrii)."""
    if min_piece_m2 <= 0:
        return geometry
    parts = _parts(geometry)
    kept = [part for part in parts if part.area() >= min_piece_m2]
    if len(kept) == len(parts):
        return geometry
    dropped = len(parts) - len(kept)
    warnings.append(f"pominięto {dropped} kawałek/kawałki mniejsze niż {min_piece_m2:g} m² "
                    f"(szum numeryczny po różnicy geometrii)")
    if not kept:
        raise EmptyCutError(EMPTY_CUT_MESSAGE)
    return QgsGeometry(kept[0]) if len(kept) == 1 else QgsGeometry.unaryUnion(kept)
