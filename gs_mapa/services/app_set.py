"""Analiza zbioru APP z katalogu w tle (``QgsTask``).

Skany katalogów z aktami oczekującymi na publikację bywają duże (plan ogólny miasta to
kilkadziesiąt MB), więc analiza idzie w tle, a okno dostaje postęp i wynik. Samą logikę
(bez QGIS) ma :mod:`gs_mapa.core.app_set` — tutaj tylko zadanie i sygnały.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Optional, Tuple

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import QgsApplication, QgsTask

from ..core.app_set import SetReport, scan

logger = logging.getLogger("GSMapa.AppSet")


class _ScanTask(QgsTask):
    """Zadanie w tle: analiza wszystkich plików GML w katalogu."""

    def __init__(self, folder: str, published: Dict[Tuple[str, str, str], str]):
        super().__init__(f"GS Mapa: analiza zbioru {folder}", QgsTask.Flag.CanCancel)
        self.folder = folder
        self.published = published
        self.report: Optional[SetReport] = None
        self.error: Optional[str] = None
        self.stage = ""
        self.done: Optional[Callable] = None

    def run(self) -> bool:
        try:
            self.report = scan(self.folder, self.published, progress=self._progress,
                               is_canceled=self.isCanceled)
            return True
        except Exception as error:  # noqa: BLE001 — komunikat trafia do użytkownika
            logger.exception("Analiza zbioru APP nie powiodła się")
            self.error = str(error)
            return False

    def _progress(self, value: float, text: str) -> None:
        self.stage = text
        self.setProgress(value)

    def finished(self, result: bool) -> None:
        if self.done is not None:
            self.done(self)


class AppSetService(QObject):
    """Analiza zbioru APP z katalogu; jedno zadanie naraz."""

    #: wynik (:class:`SetReport`)
    finished = pyqtSignal(object)
    #: (postęp 0–100, plik)
    progress = pyqtSignal(float, str)
    #: komunikat błędu — analiza nie wystartowała albo padła
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._task: Optional[_ScanTask] = None

    def busy(self) -> bool:
        return self._task is not None

    def start(self, folder: str, published: Optional[Dict[Tuple[str, str, str], str]] = None) -> bool:
        """
        Uruchamia analizę katalogu w tle.

        :param folder: katalog z plikami GML (także podkatalogi).
        :param published: klucze aktów opublikowanych (:func:`gs_mapa.core.app_set.published_keys`).
        :return: ``True``, gdy zadanie ruszyło.
        """
        if self._task is not None:
            return False
        task = _ScanTask(folder, dict(published or {}))
        task.done = self._on_done
        task.progressChanged.connect(lambda value: self.progress.emit(value, task.stage))
        self._task = task  # menedżer zadań nie trzyma referencji Pythona
        QgsApplication.taskManager().addTask(task)
        return True

    def scan_sync(self, folder: str, published: Optional[Dict[Tuple[str, str, str], str]] = None) -> SetReport:
        """Analiza w bieżącym wątku (testy i skrypty)."""
        return scan(folder, published)

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def _on_done(self, task: _ScanTask) -> None:
        if task is not self._task:
            return
        self._task = None
        if task.report is not None:
            self.finished.emit(task.report)
        else:
            self.failed.emit(task.error or "Nie udało się przeanalizować katalogu.")
