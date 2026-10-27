"""
Lista ostatnich plików APP (karta „Pliki APP”) — zapis ustawień, scalanie wpisów.

Moduł czysty (bez Qt i bez QGIS), żeby dał się testować w zwykłym Pythonie.
Wpisy pamiętają ścieżkę pliku, wersję APP (rozpoznaną z przestrzeni nazw), wynik
ostatniej walidacji i datę dodania. Lista jest ograniczona do
:data:`MAX_RECENT` pozycji, najnowsza na górze.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

#: Ile plików pamięta wtyczka.
MAX_RECENT = 10

#: Status walidacji: nie sprawdzono.
STATUS_NONE = "none"
#: Status walidacji: bez błędów.
STATUS_OK = "ok"
#: Status walidacji: ostrzeżenia.
STATUS_WARN = "warn"
#: Status walidacji: błędy.
STATUS_ERROR = "err"

#: Kolejność statusów w ustawieniach — od najgorszego.
_ALL_STATUS = (STATUS_ERROR, STATUS_WARN, STATUS_OK, STATUS_NONE)


def status_from_counts(errors: int, warnings: int) -> str:
    """Status walidacji z liczby błędów i ostrzeżeń."""
    if errors:
        return STATUS_ERROR
    if warnings:
        return STATUS_WARN
    return STATUS_OK


def _plural(count: int, one: str, few: str, many: str) -> str:
    if count == 1:
        word = one
    elif count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        word = few
    else:
        word = many
    return f"{count} {word}"


def plural_errors(count: int) -> str:
    """``1 błąd``, ``2 błędy``, ``5 błędów``."""
    return _plural(count, "błąd", "błędy", "błędów")


def plural_warnings(count: int) -> str:
    """``1 ostrzeżenie``, ``2 ostrzeżenia``, ``5 ostrzeżeń``."""
    return _plural(count, "ostrzeżenie", "ostrzeżenia", "ostrzeżeń")


def plural_infos(count: int) -> str:
    """``1 informacja``, ``2 informacje``, ``5 informacji``."""
    return _plural(count, "informacja", "informacje", "informacji")


def plural_objects(count: int) -> str:
    """``1 obiekt``, ``2 obiekty``, ``5 obiektów``."""
    return _plural(count, "obiekt", "obiekty", "obiektów")


def normalize(entry: Optional[Dict]) -> Optional[Dict]:
    """Wpis w zapisanym formacie: tylko znane pola, poprawne typy."""
    if not entry or not isinstance(entry, dict):
        return None
    path = str(entry.get("path") or "").strip()
    if not path:
        return None
    status = str(entry.get("status") or STATUS_NONE)
    if status not in _ALL_STATUS:
        status = STATUS_NONE
    return {
        "path": path,
        "name": str(entry.get("name") or ""),
        "directory": str(entry.get("directory") or ""),
        "kind": str(entry.get("kind") or _kind_of(path)),
        "version": str(entry.get("version") or ""),
        "status": status,
        "errors": int(entry.get("errors") or 0),
        "warnings": int(entry.get("warnings") or 0),
        "added": str(entry.get("added") or ""),
    }


def normalize_all(entries) -> List[Dict]:
    """Lista wpisów w zapisanym formacie (bez duplikatów ścieżek)."""
    result: List[Dict] = []
    seen = set()
    for raw in entries or []:
        item = normalize(raw)
        if item is None or item["path"] in seen:
            continue
        seen.add(item["path"])
        result.append(item)
    return result


def upsert(entries, path: str, name: str = "", directory: str = "",
           version: Optional[str] = None,
           status: Optional[str] = None, errors: Optional[int] = None,
           warnings: Optional[int] = None, added: Optional[str] = None) -> List[Dict]:
    """
    Wstawia plik na początku listy (albo aktualizuje istniejący wpis).

    Podane pola nadpisują poprzednie; brakujące zostają bez zmian. Kolejność:
    najnowszy plik pierwszy, lista obcięta do :data:`MAX_RECENT`.
    """
    items = normalize_all(entries)
    path = str(path or "").strip()
    if not path:
        return items
    existing = next((item for item in items if item["path"] == path), None)
    item = dict(existing) if existing else {
        "path": path, "name": name, "directory": directory, "version": "", "status": STATUS_NONE,
        "errors": 0, "warnings": 0, "added": added or datetime.now().isoformat(timespec="seconds"),
    }
    if name:
        item["name"] = name
    if directory:
        item["directory"] = directory
    if version is not None:
        item["version"] = str(version)
    if status is not None:
        item["status"] = status
    if errors is not None:
        item["errors"] = int(errors)
    if warnings is not None:
        item["warnings"] = int(warnings)
    items = [i for i in items if i["path"] != path]
    items.insert(0, item)
    return items[:MAX_RECENT]


def remove(entries, path: str) -> List[Dict]:
    """Usuwa plik z listy (bez potwierdzenia — łatwe do cofnięcia)."""
    return [item for item in normalize_all(entries) if item["path"] != str(path or "")]


def find(entries, path: str) -> Optional[Dict]:
    """Wpis dla ścieżki albo ``None``."""
    return next((item for item in normalize_all(entries) if item["path"] == str(path or "")), None)


def _kind_of(path: str) -> str:
    """Rodzaj pliku: ``gml`` albo ``gpkg`` (na podstawie rozszerzenia)."""
    return "gpkg" if str(path or "").lower().endswith(".gpkg") else "gml"


def status_label(entry: Dict) -> str:
    """Tekst wyniku walidacji: „bez błędów”, „3 błędy, 1 ostrzeżenie”, „nie sprawdzono”."""
    status = entry.get("status", STATUS_NONE)
    errors = int(entry.get("errors") or 0)
    warnings = int(entry.get("warnings") or 0)
    if status == STATUS_NONE:
        return "nie sprawdzono"
    if status == STATUS_ERROR:
        return plural_errors(errors) + (", " + plural_warnings(warnings) if warnings else "")
    if status == STATUS_WARN:
        return plural_warnings(warnings)
    return "bez błędów"


def version_label(entry: Dict) -> str:
    """Wersja APP pliku (``""`` gdy nieznana — plik GPKG bez wersji)."""
    return str(entry.get("version") or "")


def kind_label(entry: Dict) -> str:
    """Etykieta wersji pliku na liście: „APP 3.0” albo „GeoPackage APP”."""
    version = version_label(entry)
    if version:
        return f"APP {version}"
    if entry.get("kind") == "gpkg":
        return "GeoPackage APP"
    return "APP"
