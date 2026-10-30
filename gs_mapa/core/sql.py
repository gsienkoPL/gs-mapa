"""Nazwy w zapytaniach SQLite budowanych z nazw tabel i kolumn odczytanych z pliku."""

from __future__ import annotations


def quote_identifier(name: str) -> str:
    """
    Nazwa tabeli lub kolumny w cudzysłowie SQL (``"a""b"``).

    Nazw nie da się przekazać parametrem zapytania (``?``), a w plikach GeoPackage pochodzą
    z danych (``gpkg_geometry_columns``, ``gpkg_contents``). Podwojenie cudzysłowu sprawia, że
    nazwa zostaje jednym identyfikatorem, niezależnie od zawartych w niej znaków.

    :raises ValueError: nazwa zawiera znak NUL (SQLite i tak by jej nie przyjął).
    """
    text = str(name)
    if "\x00" in text:
        raise ValueError("Nazwa w zapytaniu SQL nie może zawierać znaku NUL.")
    return '"' + text.replace('"', '""') + '"'
