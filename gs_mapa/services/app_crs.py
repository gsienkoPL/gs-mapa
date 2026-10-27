"""Układ współrzędnych pliku aktu APP — wspólny dla wszystkich jego tabel.

GPKG zapisany z GML ma w ``gpkg_contents`` układ tylko dla tabel z geometrią.
Tabela bez geometrii (``DokumentFormalny``) dostaje ``srs_id = 0``, czyli brak
układu, i QGIS traktuje ją jako warstwę o nieznanym układzie — przy dodaniu do
projektu pyta wtedy użytkownika, w jakim układzie są dane.

APP wymaga jednego ``srsName`` w całym akcie, więc układ tabeli bez geometrii jest
z góry znany: taki sam jak reszty pliku. Wystarczy go podstawić.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from qgis.core import QgsCoordinateReferenceSystem


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
