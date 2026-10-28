"""Odczyt aktu, dokumentów i rysunków ze wzorcowego pliku GeoPackage (GPKG → formularz kreatora).

Wzorcowy plik APP (`Pliki APP → Nowy ▫ → Pusty GeoPackage APP`) ma te same atrybuty co schemat
APP 3.0 w kolumnach tabel. Do tej pory kreator czytał z niego tylko strefy i regulacje (przez
warstwy) — akt, dokumenty i rysunki trzeba było wpisywać drugi raz w formularzu. Ten moduł
czyta je z tabel, więc ścieżka „wypełnij GeoPackage w QGIS → wygeneruj GML” zamyka się bez
przepisywania danych.

Nazwy kolumn są nazwami atrybutów APP (`core/app_layers.py`); moduł czyta też warianty
z plików zapisanych z GML (np. `status_href`) i wartości z list kodowych w postaci URI —
zostaje z nich kod (``legalForce``), tak jak w polach formularza.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from qgis.core import QgsVectorLayer

from ..core.app_gml import ACT, DOCUMENT, DRAWING
from ..core.app_writer import DOCUMENT_ROLES, ROLE_LABELS

#: Tabele czytane do formularza (strefy i regulacje idą przez warstwy projektu).
TABLES = (ACT, DOCUMENT, DRAWING)
_TABLE_LABELS = {ACT: "akt", DOCUMENT: "dokumenty", DRAWING: "rysunki"}


def _layer(path: str, table: str) -> Optional[QgsVectorLayer]:
    layer = QgsVectorLayer(f"{path}|layername={table}", table, "ogr")
    return layer if layer.isValid() else None


def _empty(value) -> bool:
    if value is None:
        return True
    if hasattr(value, "isNull") and value.isNull():
        return True
    return isinstance(value, str) and not value.strip()


def _text(value) -> Optional[str]:
    if _empty(value):
        return None
    if hasattr(value, "toString") and not isinstance(value, str):
        name = value.__class__.__name__
        # QDate: data dzienna; QDateTime: moment w UTC (jak w pliku GML).
        if name == "QDateTime":
            return value.toUTC().toString("yyyy-MM-dd'T'HH:mm:ss'Z'") or None
        return value.toString("yyyy-MM-dd") or None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _date(value) -> Optional[str]:
    text = _text(value)
    if text is None:
        return None
    return text.split("T", 1)[0].split(" ", 1)[0]


def _int(value) -> Optional[int]:
    text = _text(value)
    if text is None:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _bool(value) -> Optional[bool]:
    if _empty(value):
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "tak", "t", "yes", "y"):
        return True
    if text in ("false", "0", "nie", "n", "no", "f"):
        return False
    return None


def _values(value) -> List[str]:
    """Wartość wielokrotna z jednej kolumny: lista, JSON ``["…"]`` albo tekst z przecinkami."""
    if _empty(value):
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if text.startswith("["):
        try:
            return [str(item).strip() for item in json.loads(text) if str(item).strip()]
        except ValueError:
            pass
    for separator in (";", ","):
        if separator in text:
            return [part.strip() for part in text.split(separator) if part.strip()]
    return [text]


def _tail(value) -> Optional[str]:
    """Kod z URI listy kodowej (``…/legalForce``) albo sama wartość."""
    text = _text(value)
    return text.rstrip("/").rsplit("/", 1)[-1] if text else None


def _role(value) -> str:
    """Rola dokumentu: kod, etykieta polska albo nazwa atrybutu relacji z GML."""
    text = (_tail(value) or "").strip()
    if not text:
        return "uchwala"
    if text in DOCUMENT_ROLES:
        return text
    for code, label in ROLE_LABELS.items():
        if text.lower() == label.lower():
            return code
    aliases = {"dokumentUchwalajacy": "uchwala", "dokumentPrzystepujacy": "przystapienie",
               "dokumentZmieniajacy": "zmienia", "dokumentUchylajacy": "uchyla",
               "dokumentUniewazniajacy": "uniewaznia", "dokument": "dokument"}
    return aliases.get(text, text)


def act_values(feature) -> dict:
    """Pola zakładki „Akt” z wiersza tabeli aktu (postać jak w szkicu kreatora)."""
    names = set(feature.fields().names())

    def value(name, default=None):
        return feature[name] if name in names else default

    base_maps = []
    date = _date(value("mapaPodkladowa_data"))
    reference = _text(value("mapaPodkladowa_referencja"))
    link = _text(value("mapaPodkladowa_lacze"))
    if date or reference or link:
        base_maps.append({"date": date or "", "reference": reference or "", "link": link or ""})
    return {
        "namespace": _text(value("przestrzenNazw")) or "",
        "local_id": _text(value("lokalnyId")) or "",
        "version_id": _text(value("wersjaId")) or "",
        "version_start": _text(value("poczatekWersjiObiektu")),
        "version_end": _text(value("koniecWersjiObiektu")),
        "title": _text(value("tytul")) or "",
        "alt_titles": " | ".join(_values(value("tytulAlternatywny"))),
        "plan_type": _tail(value("typPlanu")) or "",
        "level": _tail(value("poziomHierarchii")),
        "status": _tail(value("status")) or "legalForce",
        "effective_from": _date(value("obowiazujeOd")) or "",
        "effective_to": _date(value("obowiazujeDo")),
        "modification": _bool(value("modyfikacja")),
        "change_count": _int(value("zmiana")),
        "changed_plan": _text(value("zmienianyPlan")),
        "base_maps": base_maps,
    }


def document_values(feature) -> dict:
    """Pola formularza dokumentu z wiersza tabeli dokumentów."""
    names = set(feature.fields().names())

    def value(name, default=None):
        return feature[name] if name in names else default

    return {
        "role": _role(value("rola")),
        "local_id": _text(value("lokalnyId")) or "",
        "title": _text(value("tytul")) or "",
        "short_name": _text(value("nazwaSkrocona")),
        "number": _text(value("numerIdentyfikacyjny")),
        "authority": _text(value("organUstanawiajacy")),
        "date": _date(value("data")) or "",
        "date_type": _tail(value("typDaty")) or "creation",
        "entry_into_force": _date(value("dataWejsciaWZycie")),
        "repeal_date": _date(value("dataUchylenia")),
        "detailed_reference": _text(value("szczegoloweOdniesienie")),
        "journal": _tail(value("dziennikUrzedowy")),
        "links": _values(value("lacze")),
    }


def drawing_values(feature) -> dict:
    """Pola formularza rysunku z wiersza tabeli rysunków."""
    names = set(feature.fields().names())

    def value(name, default=None):
        return feature[name] if name in names else default

    return {
        "local_id": _text(value("lokalnyId")) or "",
        "title": _text(value("tytul")) or "",
        "link": _text(value("lacze")) or "",
        "legend": _text(value("legenda")),
        "crs": _text(value("ukladOdniesieniaPrzestrzennego")),
        "scale": _int(value("rozdzielczoscPrzestrzenna")) or 0,
        "description": _text(value("opis")),
        "version_end": _text(value("koniecWersjiObiektu")),
        "effective_from": _date(value("obowiazujeOd")),
        "effective_to": _date(value("obowiazujeDo")),
    }


@dataclass
class TemplateSource:
    """Dane wczytane z pliku GeoPackage (akt, dokumenty, rysunki)."""

    path: str
    act: dict = field(default_factory=dict)
    documents: List[dict] = field(default_factory=list)
    drawings: List[dict] = field(default_factory=list)
    #: Liczba wierszy w każdej tabeli (także gdy ich nie użyto — np. drugi akt).
    counts: Dict[str, int] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


def read_template(path) -> TemplateSource:
    """
    Czyta akt, dokumenty i rysunki z pliku GeoPackage APP.

    Bierze pierwszy wiersz tabeli aktu (kolejne wiersze to zwykle pomyłka — o tym mówi uwaga)
    oraz wszystkie wiersze dokumentów i rysunków. Brak tabeli nie jest błędem: plik może być
    wypełniony częściowo.

    :param path: plik ``.gpkg`` w układzie wzorcowego pliku wtyczki.
    :return: dane gotowe do wstawienia w formularz.
    :raises OSError: plik nie istnieje.
    """
    target = Path(path)
    if not target.is_file():
        raise OSError(f"Nie ma pliku {target.name}")
    source = TemplateSource(str(target))
    for table in TABLES:
        layer = _layer(str(target), table)
        if layer is None:
            source.notes.append(f"plik nie ma tabeli {table} ({_TABLE_LABELS[table]}) — pominięto")
            continue
        rows = list(layer.getFeatures())
        source.counts[table] = len(rows)
        if not rows:
            source.notes.append(f"tabela {_TABLE_LABELS[table]} jest pusta")
            continue
        if table == ACT:
            source.act = act_values(rows[0])
            if len(rows) > 1:
                source.notes.append(f"tabela aktu ma {len(rows)} wiersze — wczytano pierwszy")
        elif table == DOCUMENT:
            source.documents = [document_values(feature) for feature in rows]
        else:
            source.drawings = [drawing_values(feature) for feature in rows]
    return source
