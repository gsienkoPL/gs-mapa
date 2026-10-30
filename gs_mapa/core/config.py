"""Ustawienia i stałe wtyczki Rejestr Urbanistyczny."""

import json

from qgis.PyQt.QtCore import QSettings

# Publiczny manifest wersji
MANIFEST_URL = "https://gs-mapa-public.s3.waw.io.cloud.ovh.net/planowanie/tiles/manifest.json"

# Obsługiwane typy danych
DATA_TYPES = ["POG", "MPZP", "ZPI", "PZPW"]


class GSMapaConfig:
    """Zarządzanie ustawieniami wtyczki."""

    def __init__(self, settings: QSettings | None = None):
        self._settings = settings if settings is not None else QSettings()

    @property
    def timeout(self) -> int:
        """Timeout dla operacji sieciowych (sekundy)."""
        return self._settings.value("network_timeout", 30, type=int)

    @timeout.setter
    def timeout(self, value: int) -> None:
        self._settings.setValue("network_timeout", value)

    @property
    def check_interval(self) -> int:
        """Interwał automatycznego sprawdzania aktualizacji (minuty)."""
        return self._settings.value("check_interval", 60, type=int)

    @check_interval.setter
    def check_interval(self, value: int) -> None:
        self._settings.setValue("check_interval", value)

    @property
    def last_used_type(self) -> str:
        """Ostatnio wybrany typ danych."""
        return self._settings.value("last_used_type", "POG", type=str)

    @last_used_type.setter
    def last_used_type(self, value: str) -> None:
        self._settings.setValue("last_used_type", value)

    @property
    def download_dir(self) -> str:
        """Ostatnio wybrany folder na pobierane pliki aktów (pusty = jeszcze nie wybrano)."""
        return self._settings.value("download_dir", "", type=str)

    @download_dir.setter
    def download_dir(self, value: str) -> None:
        self._settings.setValue("download_dir", value)

    @property
    def app_unit(self) -> str:
        """Jednostka zakładki „Pliki APP”: kod gminy (``143403_1``), województwa (``14``) albo pusty."""
        return self._settings.value("app_unit", "", type=str)

    @app_unit.setter
    def app_unit(self, value: str) -> None:
        self._settings.setValue("app_unit", value)

    @property
    def context_unit(self) -> str:
        """
        Ostatnio używana jednostka paska kontekstu: gmina (``066301_1``), powiat (``0663``),
        województwo (``06``) albo pusty.

        Migracja: bez zapisanej wartości odczytujemy jednostkę dawnej karty „Pliki APP” (``app_unit``);
        zapis idzie już tylko do nowego klucza.
        """
        if self._settings.contains("context_unit"):
            return self._settings.value("context_unit", "", type=str)
        return self.app_unit

    @context_unit.setter
    def context_unit(self, value: str) -> None:
        self._settings.setValue("context_unit", value)

    @property
    def plan_info_geometry(self):
        """Zapisane położenie i rozmiar okna „Informacja o planie” (``QByteArray`` albo ``None``)."""
        return self._settings.value("plan_info_geometry", None)

    @plan_info_geometry.setter
    def plan_info_geometry(self, value) -> None:
        self._settings.setValue("plan_info_geometry", value)

    @property
    def last_tab(self) -> int:
        """Ostatnio otwarta zakładka panelu."""
        return self._settings.value("last_tab", 0, type=int)

    @last_tab.setter
    def last_tab(self, value: int) -> None:
        self._settings.setValue("last_tab", value)

    @property
    def last_tab_key(self) -> str:
        """Ostatnio otwarta karta doku: ``plany``, ``app``, ``gugik`` (pusty = jeszcze nie zapisano)."""
        return self._settings.value("last_tab_key", "", type=str)

    @last_tab_key.setter
    def last_tab_key(self, value: str) -> None:
        self._settings.setValue("last_tab_key", value)

    @property
    def last_tab_migrated(self) -> bool:
        """Zapisany numer zakładki już przeniesiony po złączeniu zakładek w „GUGiK”."""
        return self._settings.value("last_tab_migrated", False, type=bool)

    @last_tab_migrated.setter
    def last_tab_migrated(self, value: bool) -> None:
        self._settings.setValue("last_tab_migrated", value)

    @property
    def eziudp_query(self) -> dict:
        """Ostatnie filtry wyszukiwania w EZiUDP (pusty słownik = brak)."""
        try:
            value = json.loads(self._settings.value("eziudp_query", "{}", type=str) or "{}")
        except ValueError:
            return {}
        return {k: v for k, v in value.items() if isinstance(v, str)} if isinstance(value, dict) else {}

    @eziudp_query.setter
    def eziudp_query(self, value: dict) -> None:
        self._settings.setValue("eziudp_query", json.dumps(value, ensure_ascii=False))

    @property
    def opendata_state(self) -> dict:
        """Ostatni wybór w zakładce „Pobieranie danych”: ``teryt`` (str) i ``datasets`` (lista kluczy)."""
        try:
            value = json.loads(self._settings.value("opendata_state", "{}", type=str) or "{}")
        except ValueError:
            return {}
        if not isinstance(value, dict):
            return {}
        result = {}
        if isinstance(value.get("teryt"), str):
            result["teryt"] = value["teryt"]
        if isinstance(value.get("datasets"), list):
            result["datasets"] = [k for k in value["datasets"] if isinstance(k, str)]
        return result

    @opendata_state.setter
    def opendata_state(self, value: dict) -> None:
        self._settings.setValue("opendata_state", json.dumps(value, ensure_ascii=False))

    @property
    def app_recent_files(self) -> list:
        """Ostatnie pliki APP (karta „Pliki APP”) — lista wpisów z :mod:`gs_mapa.core.app_recent`."""
        try:
            value = json.loads(self._settings.value("app_recent_files", "[]", type=str) or "[]")
        except ValueError:
            return []
        return value if isinstance(value, list) else []

    @app_recent_files.setter
    def app_recent_files(self, value: list) -> None:
        self._settings.setValue("app_recent_files", json.dumps(value, ensure_ascii=False))

    def is_collapsed(self, key: str) -> bool:
        """Sekcja panelu zwinięta (stan zapamiętywany między sesjami)."""
        return self._settings.value(f"collapsed/{key}", False, type=bool)

    def migrate_collapsed(self, key: str, legacy_key: str) -> None:
        """
        Przenosi stan zwinięcia ze starego klucza (``QgsCollapsibleGroupBox``) na nowy.

        Sekcje panelu są dziś zwijane przez :class:`gs_mapa.ui.widgets.Section`, które
        trzyma stan w kluczu ``collapsed/<klucz>``. Sekcje GUGiK do tej pory zapamiętywały
        stan w ``QgsSettings`` — stan przenosimy przy pierwszym uruchomieniu po zmianie.
        """
        from qgis.core import QgsSettings

        if self._settings.contains(f"collapsed/{key}"):
            return
        legacy = QgsSettings()
        if not legacy.contains(f"{legacy_key}/collapsed"):
            return
        self.set_collapsed(key, bool(legacy.value(f"{legacy_key}/collapsed", False, type=bool)))

    def set_collapsed(self, key: str, value: bool) -> None:
        self._settings.setValue(f"collapsed/{key}", bool(value))

    @property
    def search_types(self) -> list:
        """Typy aktów zaznaczone w wyszukiwarce (małymi literami); domyślnie wszystkie."""
        value = self._settings.value("search_types", ",".join(t.lower() for t in DATA_TYPES), type=str)
        return [t for t in value.split(",") if t]

    @search_types.setter
    def search_types(self, value: list) -> None:
        self._settings.setValue("search_types", ",".join(value))

    @property
    def search_status(self) -> str:
        """Status aktu wybrany w wyszukiwarce (pusty = każdy)."""
        return self._settings.value("search_status", "", type=str)

    @search_status.setter
    def search_status(self, value: str) -> None:
        self._settings.setValue("search_status", value)
