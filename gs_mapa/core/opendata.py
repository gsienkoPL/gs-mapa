"""Dane powiatu z otwartych danych GUGiK (publikowane w dane.gov.pl): działki, budynki, transakcje RCN.

Czysta logika bez zależności od QGIS/Qt — adresy plików, wybór zbiorów, bezpieczne
rozpakowanie archiwum i poprawka typów geometrii w GeoPackage. Sieć, zadania w tle
i warstwy obsługuje ``services/opendata.py``.

Pliki (sprawdzone na żywym serwerze, ``latest_exports`` = najnowszy eksport):

- ``.../eziudp_wfs/GPKG/<TERYT>.gpkg.zip`` — EGiB powiatu pobrane z usług WFS zgłoszonych
  w EZiUDP: tabele ``dzialki`` i ``budynki`` (EPSG:2180), do ~175 MB archiwum;
- ``.../rcn_transakcje_ceny/GPKG/<TERYT>_transakcje_ceny.gpkg.zip`` — Rejestr Cen
  Nieruchomości: ``transakcje_dzialki``, ``transakcje_budynki``, ``transakcje_lokale``
  (większość transakcji nie ma geometrii — to wiersze bez położenia na mapie).

Tabele mają w ``gpkg_geometry_columns`` typ ogólny ``GEOMETRY``. QGIS zgaduje wtedy typ
z pierwszych obiektów: tabela z pustą geometrią na początku staje się tabelą bez
geometrii (znikają obiekty z położeniem), a mieszanka Polygon/MultiPolygon — warstwą
Polygon. ``fix_geometry_types`` wpisuje faktyczny typ z danych.
"""

import os
import re
import sqlite3
import struct
import zipfile
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .powiaty import powiat
from .sql import quote_identifier

OPENDATA_HOST = "opendata.geoportal.gov.pl"
OPENDATA_BASE = f"https://{OPENDATA_HOST}/InneDane/latest_exports"

#: Limit rozmiaru archiwum i rozpakowanego pliku (największy powiat: ~175 MB / ~0,5 GB).
MAX_ZIP_BYTES = 2 * 1024 * 1024 * 1024
MAX_GPKG_BYTES = 8 * 1024 * 1024 * 1024

_TERYT_RE = re.compile(r"^[0-9]{4}$")


class OpendataError(Exception):
    """Błąd z komunikatem dla użytkownika."""


@dataclass(frozen=True)
class Product:
    """Plik do pobrania (jedno archiwum ZIP z jednym GeoPackage)."""

    key: str
    label: str
    #: Ścieżka względem ``OPENDATA_BASE`` z ``{teryt}``.
    path: str
    #: Tabele pliku: (nazwa tabeli, nazwa warstwy dla użytkownika).
    tables: Tuple[Tuple[str, str], ...]

    def url(self, teryt: str) -> str:
        return f"{OPENDATA_BASE}/{self.path.format(teryt=teryt)}"

    def zip_name(self, teryt: str) -> str:
        return os.path.basename(self.path.format(teryt=teryt))

    def gpkg_name(self, teryt: str) -> str:
        return self.zip_name(teryt)[: -len(".zip")]

    def table_label(self, table: str) -> str:
        return dict(self.tables).get(table, table)


EGIB = Product(
    key="egib",
    label="EGiB (działki i budynki)",
    path="eziudp_wfs/GPKG/{teryt}.gpkg.zip",
    tables=(("dzialki", "Działki ewidencyjne"), ("budynki", "Budynki")),
)
RCN = Product(
    key="rcn",
    label="Rejestr Cen Nieruchomości",
    path="rcn_transakcje_ceny/GPKG/{teryt}_transakcje_ceny.gpkg.zip",
    tables=(
        ("transakcje_dzialki", "Transakcje — działki"),
        ("transakcje_budynki", "Transakcje — budynki"),
        ("transakcje_lokale", "Transakcje — lokale"),
    ),
)
PRODUCTS: Dict[str, Product] = {p.key: p for p in (EGIB, RCN)}


@dataclass(frozen=True)
class Dataset:
    """Pozycja do zaznaczenia w zakładce: tabele jednego pliku."""

    key: str
    label: str
    product: str
    tables: Tuple[str, ...]
    tooltip: str = ""


DATASETS: Tuple[Dataset, ...] = (
    Dataset("dzialki", "Działki ewidencyjne", EGIB.key, ("dzialki",),
            "EGiB powiatu z usług WFS zgłoszonych w EZiUDP (plik wspólny z budynkami)."),
    Dataset("budynki", "Budynki", EGIB.key, ("budynki",),
            "EGiB powiatu z usług WFS zgłoszonych w EZiUDP (plik wspólny z działkami)."),
    Dataset("rcn", "Transakcje i ceny (RCN)", RCN.key,
            ("transakcje_dzialki", "transakcje_budynki", "transakcje_lokale"),
            "Rejestr Cen Nieruchomości: transakcje działek, budynków i lokali. Większość "
            "transakcji nie ma położenia — są w tabeli atrybutów, nie na mapie."),
)
_DATASETS = {d.key: d for d in DATASETS}


def validate_teryt(teryt: str) -> str:
    """
    Kod TERYT powiatu (4 cyfry z listy powiatów).

    :raises OpendataError: gdy kod nie jest kodem powiatu.
    """
    teryt = (teryt or "").strip()
    if not _TERYT_RE.match(teryt) or powiat(teryt) is None:
        raise OpendataError("Wybierz powiat z listy albo podaj 4-cyfrowy kod TERYT powiatu, np. 1206.")
    return teryt


def plan(keys: Iterable[str]) -> List[Tuple[Product, Tuple[str, ...]]]:
    """
    Pliki do pobrania dla zaznaczonych pozycji: (plik, tabele do dodania) w kolejności ``PRODUCTS``.

    :raises OpendataError: gdy nic nie zaznaczono albo klucz jest nieznany.
    """
    tables: Dict[str, List[str]] = {}
    for key in keys:
        dataset = _DATASETS.get(key)
        if dataset is None:
            raise OpendataError(f"Nieznany zbiór danych: {key}")
        tables.setdefault(dataset.product, [])
        tables[dataset.product].extend(t for t in dataset.tables if t not in tables[dataset.product])
    if not tables:
        raise OpendataError("Zaznacz co najmniej jeden zbiór danych.")
    result = []
    for product in PRODUCTS.values():
        if product.key in tables:
            order = [t for t, _ in product.tables]
            result.append((product, tuple(sorted(tables[product.key], key=order.index))))
    return result


# --- archiwum ------------------------------------------------------------------------


def extract_gpkg(zip_path: str, member: str, target: str,
                 is_canceled: Callable[[], bool] = lambda: False,
                 progress: Callable[[float], None] = lambda _value: None,
                 free_bytes: Optional[Callable[[str], int]] = None) -> None:
    """
    Rozpakowuje z archiwum jeden plik ``member`` do ``target`` (przez ``target.part``).

    Nazwa pliku w archiwum musi być dokładnie ``member`` (bez katalogów) — archiwum nie może
    zapisać niczego poza ``target``. Sprawdzany jest rozmiar i wolne miejsce na dysku.

    :raises OpendataError: gdy archiwum jest uszkodzone, nie ma pliku, plik jest za duży albo przerwano.
    """
    part = f"{target}.part"
    try:
        with zipfile.ZipFile(zip_path) as archive:
            info = next((i for i in archive.infolist() if i.filename == member), None)
            if info is None:
                raise OpendataError(f"Archiwum nie zawiera pliku {member}.")
            if info.file_size > MAX_GPKG_BYTES:
                raise OpendataError(f"Plik {member} przekracza limit rozmiaru.")
            free = (free_bytes or _free_bytes)(os.path.dirname(os.path.abspath(target)))
            if info.file_size + 64 * 1024 * 1024 > free:
                raise OpendataError(f"Za mało miejsca na dysku: {member} zajmie "
                                    f"{info.file_size // (1024 * 1024)} MB.")
            written = 0
            with archive.open(info) as source, open(part, "wb") as out:
                while True:
                    if is_canceled():
                        raise OpendataError("Przerwano.")
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > info.file_size:
                        raise OpendataError(f"Plik {member} w archiwum jest uszkodzony.")
                    out.write(chunk)
                    progress(100.0 * written / max(info.file_size, 1))
        os.replace(part, target)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError):
        _unlink(part)
        raise OpendataError("Pobrane archiwum jest uszkodzone.") from None
    except OSError as e:
        _unlink(part)
        raise OpendataError(f"Nie można zapisać pliku {target}: {e}") from None
    except OpendataError:
        _unlink(part)
        raise


def _free_bytes(folder: str) -> int:
    import shutil

    return shutil.disk_usage(folder).free


def _unlink(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# --- typy geometrii w GeoPackage -----------------------------------------------------------

#: Kody WKB (2D) -> nazwa typu w GeoPackage.
_WKB_NAMES = {1: "POINT", 2: "LINESTRING", 3: "POLYGON", 4: "MULTIPOINT", 5: "MULTILINESTRING",
              6: "MULTIPOLYGON"}
_ENVELOPE_BYTES = (0, 32, 48, 48, 64)


def gpkg_blob_wkb_type(blob: Optional[bytes]) -> Optional[int]:
    """
    Kod typu WKB geometrii GeoPackage (np. 3 = Polygon) z nagłówka blobu.

    :return: ``None`` dla braku geometrii albo geometrii pustej; ``-1`` dla nieczytelnego blobu.
    """
    if blob is None:
        return None
    blob = bytes(blob)
    if len(blob) < 8 or blob[:2] != b"GP":
        return -1
    flags = blob[3]
    if flags & 0b10000:  # geometria pusta
        return None
    envelope = (flags >> 1) & 0b111
    if envelope >= len(_ENVELOPE_BYTES):
        return -1
    offset = 8 + _ENVELOPE_BYTES[envelope]
    if len(blob) < offset + 5:
        return -1
    order = "<" if blob[offset] == 1 else ">"
    return struct.unpack(f"{order}I", blob[offset + 1: offset + 5])[0]


def declared_type(codes: Iterable[Optional[int]]) -> Optional[str]:
    """
    Typ do wpisania w ``gpkg_geometry_columns`` dla kodów WKB obiektów tabeli.

    Jeden rodzaj geometrii (punkty, linie albo poligony) 2D: typ wielokrotny, gdy choć jeden
    obiekt jest wielokrotny, inaczej pojedynczy. Mieszanka rodzajów, geometrie 3D/M, kolekcje
    albo brak geometrii — ``None`` (bez zmiany).
    """
    kinds = {c for c in codes if c is not None}
    if not kinds or not kinds <= set(_WKB_NAMES):
        return None
    families = {code if code <= 3 else code - 3 for code in kinds}
    if len(families) != 1:
        return None
    family = families.pop()
    return _WKB_NAMES[family + 3 if any(code > 3 for code in kinds) else family]


def fix_geometry_types(gpkg_path: str, is_canceled: Callable[[], bool] = lambda: False) -> Dict[str, str]:
    """
    Wpisuje w ``gpkg_geometry_columns`` faktyczny typ geometrii tabel z typem ``GEOMETRY``.

    :return: tabela -> nowy typ (tylko zmienione).
    """
    changed = {}
    try:
        connection = sqlite3.connect(gpkg_path)
    except sqlite3.Error as e:
        raise OpendataError(f"Nie można otworzyć pliku GeoPackage {os.path.basename(gpkg_path)}: {e}") from None
    try:
        rows = connection.execute(
            "SELECT table_name, column_name FROM gpkg_geometry_columns "
            "WHERE upper(geometry_type_name) = 'GEOMETRY'").fetchall()
        for table, column in rows:
            codes = set()
            # Wystarczy nagłówek: 8 bajtów + koperta (do 64) + bajt kolejności + typ (4).
            # Nazwy pochodzą z pliku — jako identyfikatory w cudzysłowie (quote_identifier).
            query = f"SELECT substr({quote_identifier(column)}, 1, 80) FROM {quote_identifier(table)}"  # nosec B608
            for (head,) in connection.execute(query):
                codes.add(gpkg_blob_wkb_type(head))
                if is_canceled():
                    return changed
            new_type = declared_type(codes)
            if new_type is not None:
                connection.execute("UPDATE gpkg_geometry_columns SET geometry_type_name = ? WHERE table_name = ?",
                                   (new_type, table))
                changed[table] = new_type
        connection.commit()
    except sqlite3.Error as e:
        raise OpendataError(f"Nie można odczytać pliku GeoPackage {os.path.basename(gpkg_path)}: {e}") from None
    finally:
        connection.close()
    return changed


@dataclass(frozen=True)
class FetchResult:
    """Wynik pobierania plików powiatu."""

    teryt: str
    #: Gotowe pliki: (plik, tabele do dodania, ścieżka GPKG, ``True`` gdy użyto pliku z dysku).
    ready: Tuple[Tuple[Product, Tuple[str, ...], str, bool], ...] = ()
    #: Błędy: (plik, komunikat).
    failed: Tuple[Tuple[Product, str], ...] = ()
    canceled: bool = False

    def summary(self, label: str) -> str:
        if self.canceled:
            return f"Przerwano pobieranie danych: {label}."
        if not self.ready:
            return f"Nie udało się pobrać danych: {label}."
        parts = [f"{product.label}{' (z dysku)' if reused else ''}" for product, _t, _p, reused in self.ready]
        text = f"{label}: {', '.join(parts)}"
        if self.failed:
            text += f"; błędy: {', '.join(product.label for product, _ in self.failed)}"
        return text


def sizes_text(received: int, total: int) -> str:
    """Postęp pobierania w MB, np. ``12,5 / 40,0 MB``."""
    def mb(value: int) -> str:
        return f"{value / (1024 * 1024):.1f}".replace(".", ",")
    return f"{mb(received)} / {mb(total)} MB" if total > 0 else f"{mb(received)} MB"

