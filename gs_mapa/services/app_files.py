"""Dodawanie plików aktu APP (GPKG, GML) do projektu QGIS ze stylami QML wtyczki APP.

GML jest najpierw zapisywany do GeoPackage (w tle, ``QgsTask``): sterownik GML czyta plik
sekwencyjnie, więc warstwa wprost z GML przelicza cały plik przy każdym odświeżeniu mapy
(plan ogólny dużego miasta — kilkadziesiąt sekund). GeoPackage ``<plik>.gml.gpkg`` powstaje
obok GML (inna nazwa niż ``<plik>.gpkg`` publikowany w Rejestrze), a gdy folder jest tylko
do odczytu — w katalogu podręcznym profilu QGIS. Aktualny plik jest używany ponownie.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Callable, List, Optional

from qgis.PyQt.QtCore import QMetaType, QObject, pyqtSignal
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransformContext,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsProject,
    QgsProviderRegistry,
    QgsProviderSublayerDetails,
    QgsTask,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)

from ..core.app_gml import ACT, APP_CLASSES, AppGmlError, declared_srs, scan_app_classes
from .app_crs import assign_file_crs, enforce_file_crs, gpkg_srs_ids
from .app_geometry import gml_layer_uri
from .app_style import apply_app_style

logger = logging.getLogger("GSMapa.AppFiles")

#: Własność warstwy: ścieżka pliku aktu, z którego pochodzi (do odszukania obiektów z raportu).
SOURCE_PROPERTY = "gs_mapa/app_source"

#: Kolejność tabel w grupie (od góry): granica aktu, regulacje, strefy, tabele bez geometrii.
_ORDER = [ACT, "ObszarStandardowDostepnosciInfrastrukturySpolecznej", "ObszarZabudowySrodmiejskiej",
          "ObszarUzupelnieniaZabudowy", "StrefaPlanistyczna", "RysunekAktuPlanowaniaPrzestrzennego",
          "DokumentFormalny"]


class ImportCanceled(Exception):
    pass


def gpkg_target(gml_path: str) -> Path:
    """Docelowy GeoPackage dla pliku GML: obok niego albo (folder tylko do odczytu) w katalogu podręcznym."""
    gml = Path(gml_path)
    beside = gml.with_name(gml.name + ".gpkg")
    if os.access(gml.parent, os.W_OK):
        return beside
    digest = hashlib.sha1(str(gml.resolve()).encode()).hexdigest()[:10]
    cache = Path(QgsApplication.qgisSettingsDirPath(), "cache", "gs_mapa", "app")
    return cache / f"{gml.stem}-{digest}.gml.gpkg"


def gpkg_is_current(gml_path: str, gpkg: Path) -> bool:
    """Czy plik GeoPackage jest aktualny i ma układ, który deklaruje GML."""
    if not (gpkg.is_file() and gpkg.stat().st_mtime >= Path(gml_path).stat().st_mtime):
        return False
    declared = declared_srs(gml_path)
    if declared is None:
        return True
    ids = gpkg_srs_ids(gpkg)
    if ids is None:  # nieczytelny plik — zapisz go jeszcze raz
        return False
    return all(str(value) == declared.partition(":")[2] for value in ids.values())


def declared_file_crs(path: Optional[str]) -> Optional[QgsCoordinateReferenceSystem]:
    """
    Układ zadeklarowany w pliku GML (``srsName``) jako obiekt QGIS.

    :param path: ścieżka pliku (``None``, gdy nieznana); pliki inne niż GML są pomijane.
    :return: układ albo ``None``, gdy plik go nie deklaruje.
    """
    if not path or Path(path).suffix.lower() != ".gml":
        return None
    epsg = declared_srs(path)
    if not epsg:
        return None
    crs = QgsCoordinateReferenceSystem(epsg)
    return crs if crs.isValid() else None


def _write_crs(declared: Optional[str], layer: QgsVectorLayer) -> QgsCoordinateReferenceSystem:
    """
    Układ, w którym zapisywana jest tabela.

    Najpierw ten zadeklarowany w GML: sterownik GML rozpoznaje ``srsName`` różnie w zależności
    od wersji GDAL i potrafi zwrócić warstwę bez układu, a wtedy GeoPackage powstaje bez CRS
    (QGIS pyta wtedy o układ przy dodawaniu pliku). Układ wykryty przez OGR jest zapasowy.
    """
    if declared:
        crs = QgsCoordinateReferenceSystem(declared)
        if crs.isValid():
            return crs
    return layer.crs()


def _warn_missing_crs(gpkg: Path, declared: Optional[str]) -> None:
    """Ostrzeżenie, gdy GeoPackage wyszedł bez układu — QGIS zapyta wtedy o CRS."""
    ids = gpkg_srs_ids(gpkg)
    if not ids:
        return
    expected = int(declared.partition(":")[2]) if declared else None
    if expected is not None and all(value == expected for value in ids.values()):
        return
    logger.warning("%s: tabele z geometrią bez układu %s (srs_id: %s)",
                   gpkg, declared or "EPSG", ids)


def gml_to_gpkg(gml_path: str, gpkg: Path, progress: Optional[Callable[[float], None]] = None,
                is_canceled: Optional[Callable[[], bool]] = None) -> List[str]:
    """
    Zapisuje tabele (klasy APP) pliku GML do GeoPackage.

    Pola tekstowe dostają nieograniczoną długość (sterownik GML zgaduje szerokość z próbki
    obiektów), wartości przepisywane są po nazwie pola, a geometria — jako wieloczęściowa.
    Zapis trafia do pliku tymczasowego, przemianowanego dopiero po sukcesie.

    :return: nazwy zapisanych tabel.
    :raises AppGmlError: plik nie jest GML APP; :raises RuntimeError: błąd zapisu.
    """
    _version, classes = scan_app_classes(gml_path)
    classes = [c for c in classes if c in APP_CLASSES]
    if not classes:
        raise AppGmlError("Plik nie zawiera obiektów APP.")
    declared = declared_srs(gml_path)
    gpkg.parent.mkdir(parents=True, exist_ok=True)
    part = gpkg.with_name(gpkg.name + ".part.gpkg")
    part.unlink(missing_ok=True)
    written = []
    context = QgsCoordinateTransformContext()
    try:
        for i, table in enumerate(classes):
            if is_canceled is not None and is_canceled():
                raise ImportCanceled()
            source = QgsVectorLayer(gml_layer_uri(gml_path, table), table, "ogr")
            if not source.isValid():
                raise RuntimeError(f"OGR nie odczytał tabeli {table} z {gml_path}")
            fields = QgsFields()
            for f in source.fields():
                copy = QgsField(f)
                if copy.type() == QMetaType.Type.QString:
                    copy.setLength(0)
                fields.append(copy)
            wkb = source.wkbType()
            geometry_type = QgsWkbTypes.promoteNonPointTypesToMulti(wkb) if wkb != Qgis.WkbType.NoGeometry else wkb
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "GPKG"
            options.layerName = table
            if written:
                options.actionOnExistingFile = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
            writer = QgsVectorFileWriter.create(str(part), fields, geometry_type,
                                                _write_crs(declared, source), context, options)
            if writer.hasError() != QgsVectorFileWriter.WriterError.NoError:
                raise RuntimeError(f"Zapis {table}: {writer.errorMessage()}")
            names = source.fields().names()
            for feature in source.getFeatures():
                out = QgsFeature(fields)
                out.setAttributes([feature.attribute(name) for name in names])
                geometry = feature.geometry()
                if not geometry.isNull() and geometry_type != Qgis.WkbType.NoGeometry:
                    geometry.convertToMultiType()
                    out.setGeometry(geometry)
                if not writer.addFeature(out):
                    raise RuntimeError(f"Zapis obiektu {table}: {writer.errorMessage()}")
            del writer
            written.append(table)
            if progress is not None:
                progress(100.0 * (i + 1) / len(classes))
        os.replace(part, gpkg)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    _warn_missing_crs(gpkg, declared)
    return written


def add_app_layers(path: str, group_name: Optional[str] = None, project: Optional[QgsProject] = None,
                   origin: Optional[str] = None) -> list:
    """
    Dodaje do projektu wszystkie tabele pliku aktu (GPKG lub inny plik OGR) w jednej grupie.

    Tabele APP dostają style QML wtyczki APP; kolejność w grupie: granica aktu na wierzchu,
    strefy pod regulacjami, tabele bez geometrii na końcu.

    :param origin: plik wskazany przez użytkownika (np. GML, z którego powstał GPKG) —
        zapisywany we własności warstwy ``gs_mapa/app_source``.
    :return: dodane warstwy (pusta lista, gdy pliku nie da się odczytać).
    """
    project = project or QgsProject.instance()
    options = QgsProviderSublayerDetails.LayerOptions(project.transformContext())
    layers = []
    for details in QgsProviderRegistry.instance().providerMetadata("ogr").querySublayers(path):
        if details.type() != Qgis.LayerType.Vector:
            continue
        layer = details.toLayer(options)
        if layer is None or not layer.isValid():
            continue
        apply_app_style(layer, details.name())
        layer.setCustomProperty(SOURCE_PROPERTY, str(origin or path))
        layers.append(layer)
    if not layers:
        return []
    # Tabele bez geometrii nie dostają układu z pliku GPKG (srs_id = 0) — bez tego
    # QGIS pyta użytkownika, w jakim układzie są dane. Gdy plik nie ma układu wcale
    # (GPKG zapisany z GML, w którym OGR nie rozpoznał ``srsName``), bierzemy układ
    # z deklaracji GML, z którego plik powstał.
    assign_file_crs(layers, declared_file_crs(origin))
    # Warstwa, której OGR dał inny układ niż plik (np. układ projektu zamiast 2178),
    # rysuje się w innym miejscu mapy bez żadnego komunikatu — układ pliku wygrywa.
    enforce_file_crs(layers, path)
    layers.sort(key=lambda l: _ORDER.index(l.name()) if l.name() in _ORDER else len(_ORDER))
    group = project.layerTreeRoot().insertGroup(0, group_name or Path(path).name)
    for layer in layers:
        project.addMapLayer(layer, False)
        group.addLayer(layer)
    return layers


class _GmlImportTask(QgsTask):
    def __init__(self, gml_path: str, gpkg: Path):
        super().__init__(f"GS Mapa: zapis {Path(gml_path).name} do GeoPackage", QgsTask.Flag.CanCancel)
        self.gml_path = gml_path
        self.gpkg = gpkg
        self.error: Optional[str] = None
        self.done: Optional[Callable] = None  # ustawiany przez serwis

    def run(self) -> bool:
        try:
            gml_to_gpkg(self.gml_path, self.gpkg, self.setProgress, self.isCanceled)
            return True
        except ImportCanceled:
            self.error = "Przerwano."
        except Exception as e:  # noqa: BLE001 — komunikat trafia do użytkownika
            logger.exception("Zapis GML do GeoPackage nie powiódł się")
            self.error = str(e)
        return False

    def finished(self, result: bool) -> None:
        if self.done is not None:
            self.done(self, result)


class AppFileService(QObject):
    """Dodawanie plików aktu do projektu; GML zapisywany w tle do GeoPackage."""

    #: (ścieżka wybrana przez użytkownika, liczba dodanych warstw, plik dodany do projektu)
    imported = pyqtSignal(str, int, str)
    #: (ścieżka, komunikat)
    failed = pyqtSignal(str, str)
    #: (ścieżka, postęp 0–100)
    progress = pyqtSignal(str, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: List[_GmlImportTask] = []

    def busy(self) -> bool:
        return bool(self._tasks)

    def add_file(self, path: str) -> bool:
        """
        Dodaje plik aktu: GPKG od razu, GML po zapisie do GeoPackage (w tle).

        :return: ``True``, gdy plik został dodany albo zapis ruszył.
        """
        if not Path(path).is_file():
            self.failed.emit(path, "Plik nie istnieje.")
            return False
        if Path(path).suffix.lower() != ".gml":
            return self._finish(path, path)
        gpkg = gpkg_target(path)
        if gpkg_is_current(path, gpkg):
            return self._finish(path, str(gpkg))
        task = _GmlImportTask(path, gpkg)
        task.done = self._on_task_done
        task.progressChanged.connect(lambda value, p=path: self.progress.emit(p, value))
        self._tasks.append(task)
        QgsApplication.taskManager().addTask(task)
        return True

    def run_sync(self, path: str) -> bool:
        """Jak :meth:`add_file`, ale zapis GML w bieżącym wątku (testy, skrypty)."""
        if Path(path).suffix.lower() != ".gml":
            return self.add_file(path)
        gpkg = gpkg_target(path)
        if not gpkg_is_current(path, gpkg):
            try:
                gml_to_gpkg(path, gpkg)
            except Exception as e:  # noqa: BLE001
                self.failed.emit(path, str(e))
                return False
        return self._finish(path, str(gpkg))

    def cancel(self) -> None:
        for task in list(self._tasks):
            task.cancel()

    def _on_task_done(self, task: _GmlImportTask, ok: bool) -> None:
        if task in self._tasks:
            self._tasks.remove(task)
        if ok:
            self._finish(task.gml_path, str(task.gpkg))
        else:
            self.failed.emit(task.gml_path, task.error or "Nie udało się zapisać GML do GeoPackage.")

    def _finish(self, path: str, source: str) -> bool:
        layers = add_app_layers(source, Path(path).name, origin=path)
        if not layers:
            self.failed.emit(path, "QGIS nie odczytał żadnej tabeli z pliku.")
            return False
        self.imported.emit(path, len(layers), source)
        return True
