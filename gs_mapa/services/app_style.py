"""Style QML tabel aktu planowania przestrzennego (APP) z oficjalnej wtyczki APP dla QGIS.

Pliki ``styles/app/*.qml`` pochodzą z repozytorium ``CyfryzacjaAPP/wtyczka_qgis_app_2``
(katalog ``QML``, licencja GPL-3.0) — szczegóły w ``styles/app/README.md``.

Styl dobierany jest po nazwie tabeli (klasie APP) w pliku GPKG/GML aktu. Wczytywane są tylko
kategorie wyglądu (symbolika, etykiety, renderowanie): style zawierają też konfigurację pól
i formularzy dla schematu edycyjnego wtyczki APP (np. pole ``status`` zamiast ``status_title``
w danych z Rejestru Urbanistycznego), która nie pasuje do importowanych tabel.
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("GSMapa.AppStyle")

#: Katalog z plikami QML.
STYLES_DIR = Path(__file__).resolve().parent.parent / "styles" / "app"

#: Klasa APP (nazwa tabeli) -> plik QML.
APP_STYLES = {
    "StrefaPlanistyczna": "StrefaPlanistyczna.qml",
    "AktPlanowaniaPrzestrzennego": "granice_app.qml",
    "ObszarUzupelnieniaZabudowy": "ObszarUzupelnieniaZabudowy.qml",
    "ObszarZabudowySrodmiejskiej": "ObszarZabudowySrodmiejskiej.qml",
    "ObszarStandardowDostepnosciInfrastrukturySpolecznej":
        "ObszarStandardowDostepnosciInfrastrukturySpolecznej.qml",
}


def app_style_path(table: str) -> Optional[Path]:
    """
    Plik QML dla tabeli aktu.

    :param table: nazwa tabeli (warstwy) w pliku, np. ``StrefaPlanistyczna``.
    :return: ścieżka pliku QML albo ``None``, gdy tabela nie ma stylu.
    """
    name = APP_STYLES.get(table)
    return STYLES_DIR / name if name else None


def style_categories():
    """Kategorie stylu wczytywane z QML: wygląd warstwy, bez pól i formularzy."""
    from qgis.core import QgsMapLayer

    return (
        QgsMapLayer.StyleCategory.Symbology
        | QgsMapLayer.StyleCategory.Labeling
        | QgsMapLayer.StyleCategory.Rendering
    )


def apply_app_style(layer, table: str) -> bool:
    """
    Nadaje warstwie styl APP, jeśli istnieje dla jej tabeli.

    :param layer: warstwa wektorowa z tabeli pliku GPKG/GML aktu.
    :param table: nazwa tabeli (klasa APP).
    :return: ``True``, gdy styl został wczytany.
    """
    from qgis.core import Qgis

    path = app_style_path(table)
    if path is None or layer.geometryType() == Qgis.GeometryType.Null:
        return False
    if not path.is_file():
        logger.warning(f"Brak pliku stylu {path}")
        return False
    from qgis.PyQt.QtXml import QDomDocument

    # importNamedStyle, nie loadNamedStyle: w QGIS 4.2 loadNamedStyle z pliku QML nakłada
    # konfigurację pól (np. domyślne „wybierz”) mimo ograniczenia kategorii.
    document = QDomDocument()
    parsed = document.setContent(path.read_bytes())
    if not (parsed[0] if isinstance(parsed, tuple) else parsed):
        logger.warning(f"Niepoprawny plik stylu {path}")
        return False
    ok, message = layer.importNamedStyle(document, style_categories())
    if not ok:
        logger.warning(f"Nie udało się wczytać stylu {path.name} dla {table}: {message}")
        return False
    layer.triggerRepaint()
    return True
