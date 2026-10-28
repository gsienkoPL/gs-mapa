"""Słownik pól APP 3.0 — podpowiedzi przy polach z urzędowego schematu.

Opisy pochodzą z elementów ``documentation`` w XSD APP 3.0; plik ``resources/app/field_help.json``
powstaje skryptem ``scripts/build_field_help.py``. Dzięki temu podpowiedź przy polu mówi to samo
co rozporządzenie i zmienia się razem z nim, a nie razem z pamięcią autora.

Moduł nie zależy od QGIS — treść słownika to dane, a HTML buduje okno pomocy.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

from .app_layers import LAYERS

HELP_PATH = Path(__file__).resolve().parent.parent / "resources" / "app" / "field_help.json"

#: Nazwy klas w kolejności tabel pliku (nagłówki słownika).
LAYER_ORDER = tuple(layer.name for layer in LAYERS)
#: Czytelne nazwy klas (``AktPlanowaniaPrzestrzennego`` → „Akt planowania przestrzennego”).
LAYER_LABELS = {layer.name: layer.label for layer in LAYERS}


@lru_cache(maxsize=1)
def _loaded(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load(path: Optional[Path] = None) -> dict:
    """
    Cały słownik: ``{"source": …, "layers": {klasa: {pole: opis}}}``.

    :param path: inny plik słownika (testy); domyślnie zasób wtyczki.
    """
    return _loaded(str(path or HELP_PATH))


def entry(layer: str, field: str) -> Optional[dict]:
    """
    Opis jednego pola.

    :param layer: klasa APP, np. ``StrefaPlanistyczna``.
    :param field: nazwa atrybutu APP, np. ``maksWysokoscZabudowy``.
    :return: ``{"label", "description", "type", "required", "from"}`` albo ``None``.
    """
    return (load().get("layers", {}).get(layer) or {}).get(field)


def label(layer: str, field: str) -> str:
    """Krótka nazwa pola po polsku (albo nazwa atrybutu, gdy słownik go nie zna)."""
    return (entry(layer, field) or {}).get("label") or field


def tooltip(layer: str, field: str) -> str:
    """
    Podpowiedź pola: nazwa, opis ze schematu i informacja o wymagalności.

    :return: tekst gotowy do ``setToolTip``; pusty, gdy pola nie ma w słowniku.
    """
    item = entry(layer, field)
    if not item:
        return ""
    lines: List[str] = []
    name = item.get("label") or ""
    if name:
        lines.append(name[:1].upper() + name[1:] + ".")
    description = item.get("description") or ""
    if description:
        lines.append(description)
    if item.get("required"):
        lines.append("Pole wymagane w schemacie APP 3.0.")
    return "\n".join(lines)


def rows(layer: str) -> List[Dict[str, object]]:
    """
    Pola jednej klasy w kolejności tabeli pliku — wiersze do tabeli okna pomocy.

    :param layer: klasa APP.
    :return: lista ``{"field", "label", "description", "required"}``.
    """
    fields = load().get("layers", {}).get(layer) or {}
    return [{"field": name, "label": item.get("label") or name,
             "description": item.get("description") or "", "required": bool(item.get("required"))}
            for name, item in fields.items()]
