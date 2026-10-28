"""Układ współrzędnych pliku aktu APP — wspólny dla wszystkich jego tabel.

GPKG zapisany z GML ma w ``gpkg_contents`` układ tylko dla tabel z geometrią.
Tabela bez geometrii (``DokumentFormalny``) dostaje ``srs_id = 0``, czyli brak
układu, i QGIS traktuje ją jako warstwę o nieznanym układzie — przy dodaniu do
projektu pyta wtedy użytkownika, w jakim układzie są dane.

APP wymaga jednego ``srsName`` w całym akcie, więc układ tabeli bez geometrii jest
z góry znany: taki sam jak reszty pliku. Wystarczy go podstawić.

Trzeci przypadek: warstwa dostaje **inny** układ niż zapisany w pliku (np. układ
projektu, gdy QGIS nie odczytał układu pliku, albo z wcześniejszej sesji). Wtedy
dane leżą daleko od swojego miejsca, a QGIS nie sygnalizuje błędu — dlatego układ
z pliku jest nadpisywany (``enforce_file_crs``).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from qgis.core import QgsCoordinateReferenceSystem

logger = logging.getLogger(__name__)

#: Wartości ``srs_id``, które w GeoPackage znaczą „brak układu”: 0/-1 to układ
#: niezdefiniowany, a 99999 tak GDAL zapisuje plik, w którym nie rozpoznał układu.
UNDEFINED_SRS_IDS = (0, -1, 99999)


def gpkg_srs_ids(path) -> Optional[Dict[str, int]]:
    """Układ tabel z geometrią zapisany w GeoPackage.

    :param path: plik GeoPackage.
    :return: ``tabela -> srs_id`` albo ``None``, gdy pliku nie da się odczytać.
    """
    try:
        # Tylko odczyt: plik może być otwarty w projekcie QGIS.
        with sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True) as db:
            return {name: int(srs) for name, srs in
                    db.execute("SELECT table_name, srs_id FROM gpkg_geometry_columns")}
    except (sqlite3.Error, OSError, ValueError):
        return None


def file_crs(layers: Iterable) -> Optional[QgsCoordinateReferenceSystem]:
    """Układ pliku — z pierwszej warstwy, która go ma.

    :param layers: warstwy pliku (kolejność bez znaczenia).
    :return: układ wspólny dla pliku albo ``None``, gdy żadna warstwa go nie ma.
    """
    for layer in layers:
        crs = layer.crs()
        if crs.isValid():
            return crs
    return None


def assign_file_crs(layers: Iterable, fallback: Optional[QgsCoordinateReferenceSystem] = None) -> int:
    """Nadaje warstwom bez układu układ pliku.

    :param layers: warstwy do poprawy (lista zmienianych w miejscu).
    :param fallback: układ do użycia, gdy żadna warstwa go nie ma (np. zadeklarowany w GML,
        z którego powstał plik) — pokrywa plik zapisany bez układu w całości.
    :return: liczba warstw, którym nadano układ.
    """
    items: List = list(layers)
    crs = file_crs(items) or fallback
    if crs is None or not crs.isValid():
        return 0
    changed = 0
    for layer in items:
        if not layer.crs().isValid():
            layer.setCrs(crs)
            if layer.crs().isValid():
                changed += 1
    return changed


def layer_table(layer) -> str:
    """Nazwa tabeli warstwy — z jej źródła OGR (``plik.gpkg|layername=Tabela``).

    :param layer: warstwa wektorowa.
    :return: nazwa tabeli albo nazwa warstwy, gdy źródło jej nie podaje.
    """
    for part in (layer.source() or "").split("|")[1:]:
        if part.startswith("layername="):
            return part[len("layername="):]
    return layer.name()


def file_srs_id(path) -> Optional[int]:
    """Układ pliku jako ``srs_id`` — tylko gdy wszystkie tabele z geometrią mają ten sam.

    :param path: plik GeoPackage (ścieżka lokalna — plik zdalny ``/vsicurl/`` nie jest czytany).
    :return: ``srs_id`` wspólny dla pliku albo ``None``, gdy plik go nie ma albo tabele się różnią.
    """
    ids = gpkg_srs_ids(path) if path else None
    if not ids:
        return None
    used = {int(value) for value in ids.values() if int(value) not in UNDEFINED_SRS_IDS}
    return used.pop() if len(used) == 1 else None


def enforce_file_crs(layers: Iterable, path=None) -> List[str]:
    """Nadaje warstwom układ zapisany w pliku, gdy OGR (albo projekt) dał inny.

    Warstwa z układem **projektu** zamiast układu pliku rysuje się daleko od swojego
    miejsca i nie zgłasza błędu — QGIS przelicza wtedy współrzędne z niewłaściwego
    układu. Układ z pliku jest źródłem prawdy (``gpkg_geometry_columns.srs_id``), bo
    to on opisuje zapisane współrzędne.

    :param layers: warstwy dodawane z jednego pliku (zmieniane w miejscu).
    :param path: plik GeoPackage, z którego warstwy powstały.
    :return: nazwy warstw, którym nadpisano układ (puste, gdy nic nie zmieniono).
    """
    items: List = list(layers)
    srs_id = file_srs_id(path)
    if srs_id is None:
        return []
    expected = QgsCoordinateReferenceSystem.fromEpsgId(srs_id)
    if not expected.isValid():
        # Układ bez kodu EPSG (srs_id 100000) — nie zgadujemy, tylko zostawiamy OGR.
        return []
    fixed = []
    name = Path(str(path)).name
    for layer in items:
        current = layer.crs()
        if current.isValid() and current.authid() == expected.authid():
            continue
        layer.setCrs(expected)
        fixed.append(layer.name())
        logger.warning("%s: układ %s zamiast %s z pliku %s — poprawiono",
                       layer.name(), current.authid() or "brak", expected.authid(), name)
    return fixed
