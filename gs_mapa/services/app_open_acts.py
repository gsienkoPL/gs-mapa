"""
Akty i pliki APP dostępne w bieżącym QGIS — źródło walidacji „z aktów w QGIS”.

Wtyczka dodaje do QGIS dwa rodzaje danych: pliki APP z dysku (właściwość warstwy
``gs_mapa/app_source`` wskazuje plik źródłowy) oraz akty z Rejestru Urbanistycznego
(właściwość ``gsmapa/idIIP``). Ten moduł zbiera je z projektu, żeby okno walidacji
mogło sprawdzić akt, który jest już otwarty — bez wskazywania pliku z dysku.

Właściwości warstw sprawdzane są tu, a nie w ``ui/plan_info.py``, bo korzysta z nich
także serwis (``services/`` nie importuje ``ui/``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from qgis.core import QgsProject

from ..core.act_index import short_title
from .app_files import SOURCE_PROPERTY

#: Właściwość warstwy z identyfikatorem aktu w Rejestrze Urbanistycznym.
#: (``ui/plan_info.py`` importuje ją stąd — jedno źródło prawdy.)
ACT_PROPERTY = "gsmapa/idIIP"
#: Akt z Rejestru — plik GML trzeba pobrać, zanim da się go sprawdzić.
KIND_REGISTRY = "registry"
#: Plik APP na dysku.
KIND_FILE = "file"


@dataclass
class OpenAct:
    """Akt (albo plik) dostępny w QGIS — kandydat do walidacji."""

    #: ``registry`` (akt z Rejestru) albo ``file`` (plik na dysku).
    kind: str
    #: Etykieta na liście.
    label: str = ""
    #: Ścieżka pliku GML na dysku (tylko dla ``file``).
    path: str = ""
    #: Identyfikator aktu w Rejestrze (tylko dla ``registry``).
    act_id: str = ""
    #: Nazwa grupy w drzewie warstw — mówi, skąd akt pochodzi.
    group: str = ""
    #: Opis wiersza (typ aktu, tytuł, katalog) — w podpowiedzi.
    detail: str = ""
    #: Czy akt da się teraz sprawdzić (GeoPackage wymagałby osobnych reguł).
    validatable: bool = True


def _label_for(act_id: str, entries: Optional[Iterable]) -> str:
    """Etykieta aktu z Rejestru: typ i skrócony tytuł z indeksu (albo identyfikator)."""
    for entry in entries or ():
        if getattr(entry, "act_id", "") == act_id:
            title = short_title(entry.tytul) if entry.tytul else ""
            typ = (entry.typ or "").upper()
            if title:
                return f"{typ} — {title}" if typ else title
            return act_id
    return act_id


def _group_name(project: QgsProject, layer) -> str:
    """Nazwa grupy w drzewie warstw, w której leży warstwa (pusta dla warstwy bez grupy)."""
    node = project.layerTreeRoot().findLayer(layer.id())
    if node is None or node.parent() is None:
        return ""
    parent = node.parent()
    if parent is project.layerTreeRoot():
        return ""
    return parent.name()


def open_acts(project: Optional[QgsProject] = None,
              entries: Optional[Iterable] = None) -> List[OpenAct]:
    """
    Akty i pliki APP dodane do QGIS przez wtyczkę (bez powtórzeń).

    :param project: projekt QGIS (domyślnie bieżący).
    :param entries: wpisy indeksu aktów — używane tylko do etykiet (typ i tytuł).
    :return: lista kandydatów do walidacji: akty z Rejestru i pliki APP na dysku.
    """
    target = project if project is not None else QgsProject.instance()
    if target is None:
        return []
    registry: dict = {}
    files: dict = {}
    for layer in target.mapLayers().values():
        group = _group_name(target, layer)
        act_id = str(layer.customProperty(ACT_PROPERTY) or "").strip()
        if act_id and act_id not in registry:
            registry[act_id] = OpenAct(kind=KIND_REGISTRY, act_id=act_id, group=group,
                                       label=_label_for(act_id, entries), detail=act_id)
        origin = str(layer.customProperty(SOURCE_PROPERTY) or "").strip()
        if not origin or origin in files:
            continue
        path = Path(origin)
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix == ".gml":
            files[origin] = OpenAct(kind=KIND_FILE, label=path.name, path=origin, group=group,
                                    detail=origin)
        elif suffix == ".gpkg":
            files[origin] = OpenAct(kind=KIND_FILE, label=path.name, path=origin, group=group,
                                    detail=origin, validatable=False)
    result = sorted(registry.values(), key=lambda item: item.label.lower())
    result += sorted(files.values(), key=lambda item: item.label.lower())
    return result
