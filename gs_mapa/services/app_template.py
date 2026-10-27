"""Tworzenie wzorcowego (pustego) pliku GeoPackage dla aktu APP 3.0.

Plik powstaje z definicji w ``core/app_layers.py``: jedna tabela na klasę APP (akt, dokumenty,
rysunki, a w planie ogólnym — strefy i regulacje), wszystkie atrybuty schematu APP jako kolumny,
geometria w wybranym układzie PL-2000 / PL-1992. Dzięki temu plik można wypełnić w QGIS
(rysowanie i tabela atrybutów), a kreator GML czyta z niego geometrię i atrybuty bez ręcznego
mapowania pól — nazwy kolumn są nazwami atrybutów APP.

Układ współrzędnych jest zapisywany w pliku razem z geometrią (jedna kolumna geometrii = jeden
SRS), dlatego plik powstaje dla układu wskazanego przy tworzeniu, a nie „we wszystkich układach”:
APP wymaga jednego ``srsName`` w całym akcie (reguła CRS walidatora), a pięć układów w jednym
plikacie to pięć kopii każdej tabeli.

Zapis jest atomowy: tabele powstają w pliku tymczasowym obok, który zastępuje plik docelowy
dopiero po sukcesie — nieudany zapis nie zostawia okrojonego pliku.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, List, Optional

from qgis.PyQt.QtCore import QMetaType
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransformContext,
    QgsField,
    QgsFields,
    QgsVectorFileWriter,
)

from ..core.app_layers import LAYERS, Layer, layers_for
from ..core.app_rules import SUPPORTED_EPSG

#: Typ pola APP -> typ QMetaType (kolumna GPKG).
FIELD_TYPES = {
    "string": QMetaType.Type.QString,
    "integer": QMetaType.Type.Int,
    "double": QMetaType.Type.Double,
    "boolean": QMetaType.Type.Bool,
    "date": QMetaType.Type.QDate,
    "datetime": QMetaType.Type.QDateTime,
}
#: Typ geometrii tabeli -> typ WKB QGIS.
GEOMETRY_TYPES = {
    "MultiPolygon": Qgis.WkbType.MultiPolygon,
    "Polygon": Qgis.WkbType.Polygon,
    "": Qgis.WkbType.NoGeometry,
}
#: Nazwy układów do okna tworzenia pliku (kolejność rosnąca).
CRS_LABELS = {
    2176: "EPSG:2176 — PL-2000 strefa 5 (14°E)",
    2177: "EPSG:2177 — PL-2000 strefa 6 (16°E)",
    2178: "EPSG:2178 — PL-2000 strefa 7 (18°E)",
    2179: "EPSG:2179 — PL-2000 strefa 8 (20°E)",
    2180: "EPSG:2180 — PL-1992 (układ krajowy)",
}


class TemplateError(RuntimeError):
    """Nie udało się utworzyć pliku (np. plik istnieje, zły układ, błąd zapisu)."""


def template_fields(layer: Layer) -> QgsFields:
    """Pola tabeli jako ``QgsFields``: typy QGIS, alias i opis z etykiety APP."""
    fields = QgsFields()
    for field in layer.fields:
        qgs_field = QgsField(field.name, FIELD_TYPES[field.type], field.type, 0, 0, field.label)
        if field.label:
            qgs_field.setAlias(field.label)
        fields.append(qgs_field)
    return fields


def create_template(path: str, plan_type: str, epsg: int, overwrite: bool = False,
                    progress: Optional[Callable[[float], None]] = None) -> List[str]:
    """
    Tworzy pusty plik GPKG z tabelami aktu APP 3.0.

    :param path: plik docelowy (rozszerzenie ``.gpkg``).
    :param plan_type: typ aktu — decyduje o zestawie tabel (plan ogólny ma strefy i regulacje).
    :param epsg: układ współrzędnych geometrii (EPSG:2176–2180).
    :param overwrite: zastąp istniejący plik (inaczej :class:`TemplateError`).
    :param progress: wywoływane z postępem 0–100.
    :return: nazwy utworzonych tabel.
    :raises TemplateError: plik istnieje bez ``overwrite``, układ spoza EPSG:2176–2180,
        brak tabel dla typu aktu albo błąd zapisu OGR.
    """
    if epsg not in SUPPORTED_EPSG:
        raise TemplateError(f"EPSG:{epsg} — dozwolone układy to EPSG:2176–2180.")
    layers = layers_for(plan_type)
    if not layers:
        raise TemplateError(f"Nieznany typ aktu „{plan_type}” — brak tabel do utworzenia.")
    target = Path(path)
    if target.suffix.lower() != ".gpkg":
        raise TemplateError("Plik wzorcowy musi mieć rozszerzenie .gpkg.")
    if target.exists() and not overwrite:
        raise TemplateError(f"Plik {target.name} już istnieje.")
    target.parent.mkdir(parents=True, exist_ok=True)

    part = target.with_name(target.name + ".part.gpkg")
    part.unlink(missing_ok=True)
    crs = QgsCoordinateReferenceSystem.fromEpsgId(epsg)
    context = QgsCoordinateTransformContext()
    written: List[str] = []
    try:
        for index, layer in enumerate(layers):
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "GPKG"
            options.layerName = layer.name
            if layer.geometry_name:
                # Nazwa kolumny geometrii jak w schemacie APP (``zasiegPrzestrzenny`` / ``geometria``)
                # zamiast domyślnej ``geom`` — czytelniej w QGIS i w innych narzędziach.
                options.layerOptions = [f"GEOMETRY_NAME={layer.geometry_name}"]
            if written:
                options.actionOnExistingFile = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
            writer = QgsVectorFileWriter.create(str(part), template_fields(layer),
                                                GEOMETRY_TYPES[layer.geometry], crs, context, options)
            if writer.hasError() != QgsVectorFileWriter.WriterError.NoError:
                raise TemplateError(f"Nie udało się utworzyć tabeli {layer.name}: {writer.errorMessage()}")
            # Pusty plik: tabela powstaje bez obiektów, schemat pól i układ są już zapisane.
            del writer
            written.append(layer.name)
            if progress is not None:
                progress(100.0 * (index + 1) / len(layers))
        os.replace(part, target)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return written


def layer_names_for(plan_type: str) -> List[str]:
    """Nazwy tabel, które powstaną w pliku dla typu aktu (do opisu w oknie)."""
    return [layer.name for layer in layers_for(plan_type)]


def all_layer_names() -> List[str]:
    """Nazwy wszystkich tabel wzorcowego pliku (wszystkie klasy APP)."""
    return [layer.name for layer in LAYERS]
