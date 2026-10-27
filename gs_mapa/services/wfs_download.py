"""Pobieranie warstw WFS do pliku GeoPackage w tle (``QgsTask``).

Dostawca ``WFS`` QGIS pobiera obiekty (ze stronicowaniem) w wątku zadania, a
``QgsVectorFileWriter`` zapisuje je w GeoPackage — GUI nie jest blokowane, a postęp
i przycisk przerwania widać w menedżerze zadań QGIS (pasek stanu).

Zapis idzie do pliku tymczasowego obok docelowego; dopiero po pomyślnym zapisie co
najmniej jednej warstwy zastępuje on plik docelowy. Przerwane albo całkowicie nieudane
pobieranie nie niszczy istniejącego pliku.
"""

import logging
import os
from typing import List, Optional, Sequence, Tuple

from qgis.PyQt.QtCore import pyqtSignal
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsCsException,
    QgsFeedback,
    QgsRectangle,
    QgsTask,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

from ..core.eziudp import WfsDownloadJob, WfsDownloadReport

logger = logging.getLogger("GSMapa.WfsDownload")


def temporary_path(path: str) -> str:
    """Plik roboczy obok docelowego: ``dane.gpkg`` -> ``dane.part.gpkg``."""
    stem, ext = os.path.splitext(path)
    return f"{stem}.part{ext or '.gpkg'}"


def _remove(path: str) -> None:
    for name in (path, f"{path}-wal", f"{path}-shm", f"{path}-journal"):
        try:
            os.remove(name)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning(f"Nie udało się usunąć pliku roboczego {name}: {e}")


class WfsDownloadTask(QgsTask):
    """Zapisuje warstwy WFS jako tabele jednego pliku GeoPackage."""

    #: Koniec zadania (w wątku GUI): ``WfsDownloadReport``.
    done = pyqtSignal(object)

    def __init__(self, jobs: Sequence[WfsDownloadJob], path: str,
                 extent: Optional[QgsRectangle] = None,
                 extent_crs: Optional[QgsCoordinateReferenceSystem] = None,
                 transform_context: Optional[QgsCoordinateTransformContext] = None):
        """
        :param jobs: warstwy do zapisania (nazwy tabel muszą być unikalne).
        :param path: docelowy plik ``.gpkg`` (istniejący zostanie zastąpiony).
        :param extent: zasięg obiektów (``None`` = cały zbiór) w układzie ``extent_crs``.
        :param transform_context: kontekst transformacji projektu (kopiowany w wątku GUI).
        """
        titles = ", ".join(job.title for job in jobs)
        super().__init__(f"GS Mapa: pobieranie WFS do GPKG ({titles})", QgsTask.Flag.CanCancel)
        self.jobs = list(jobs)
        self.path = path
        self.extent = QgsRectangle(extent) if extent is not None and not extent.isEmpty() else None
        self.extent_crs = QgsCoordinateReferenceSystem(extent_crs) if extent_crs is not None else None
        self._context = QgsCoordinateTransformContext(transform_context) if transform_context is not None \
            else QgsCoordinateTransformContext()
        self._feedback: Optional[QgsFeedback] = None
        self._written: List[Tuple[WfsDownloadJob, int]] = []
        self._failed: List[Tuple[WfsDownloadJob, str]] = []
        self.report: Optional[WfsDownloadReport] = None

    # --- wątek zadania -------------------------------------------------------------

    def cancel(self) -> None:
        if self._feedback is not None:
            self._feedback.cancel()
        super().cancel()

    def _layer_extent(self, layer: QgsVectorLayer) -> Optional[QgsRectangle]:
        """Zasięg w układzie warstwy (``None`` = bez ograniczenia)."""
        if self.extent is None:
            return None
        if self.extent_crs is None or not self.extent_crs.isValid() or self.extent_crs == layer.crs():
            return QgsRectangle(self.extent)
        transform = QgsCoordinateTransform(self.extent_crs, layer.crs(), self._context)
        transform.setBallparkTransformsAreAppropriate(True)
        return transform.transformBoundingBox(self.extent)

    def _write(self, job: WfsDownloadJob, target: str, first: bool) -> Optional[str]:
        """Zapisuje jedną warstwę; zwraca komunikat błędu albo ``None``."""
        layer = QgsVectorLayer(job.uri, job.title, "WFS")
        if not layer.isValid():
            return f"usługa nie udostępniła warstwy ({layer.error().summary() or 'brak szczegółów'})"
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = job.table
        options.fileEncoding = "UTF-8"
        options.actionOnExistingFile = (QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile if first
                                        else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer)
        options.feedback = self._feedback
        try:
            extent = self._layer_extent(layer)
        except QgsCsException as e:
            return f"nie można przeliczyć zasięgu mapy do układu warstwy: {e}"
        if extent is not None:
            options.filterExtent = extent
        error, message, _path, _layer = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, target, self._context, options)
        if self.isCanceled():
            return "przerwano"
        if error != QgsVectorFileWriter.WriterError.NoError:
            return message or str(error)
        return None

    def run(self) -> bool:
        target = temporary_path(self.path)
        _remove(target)
        self._feedback = QgsFeedback()
        total = len(self.jobs)
        for index, job in enumerate(self.jobs):
            if self.isCanceled():
                break
            self._feedback.progressChanged.connect(
                lambda value, i=index: self.setProgress((i + max(0.0, min(value, 100.0)) / 100.0) * 100.0 / total))
            try:
                error = self._write(job, target, first=not self._written)
            except Exception as e:  # noqa: BLE001 — błąd jednej warstwy nie przerywa pozostałych
                logger.exception(f"WFS -> GPKG: {job.title}")
                error = str(e) or e.__class__.__name__
            self._feedback.progressChanged.disconnect()
            if self.isCanceled():
                break
            if error is not None:
                logger.warning(f"WFS -> GPKG: {job.title}: {error}")
                self._failed.append((job, error))
                continue
            written = QgsVectorLayer(f"{target}|layername={job.table}", job.table, "ogr")
            self._written.append((job, max(written.featureCount(), 0) if written.isValid() else 0))
            del written  # zamyka plik przed zapisem kolejnej warstwy
            self.setProgress((index + 1) * 100.0 / total)

        canceled = self.isCanceled()
        if self._written and not canceled:
            try:
                os.replace(target, self.path)
            except OSError as e:
                self._failed.extend((job, f"nie można zapisać pliku: {e}") for job, _ in self._written)
                self._written = []
        if not self._written or canceled:
            _remove(target)
        self.report = WfsDownloadReport(
            path=self.path, written=tuple(self._written), failed=tuple(self._failed),
            canceled=canceled, extent_only=self.extent is not None,
        )
        return self.report.ok

    # --- wątek GUI -----------------------------------------------------------------

    def finished(self, _result: bool) -> None:
        if self.report is None:  # zadanie przerwane przed uruchomieniem
            self.report = WfsDownloadReport(path=self.path, canceled=True, extent_only=self.extent is not None)
        self.done.emit(self.report)
