"""Hurtowa konwersja zbioru do APP 3.0 w tle (``QgsTask``).

Konwersja planu ogólnego miasta (kilkadziesiąt MB) trwa kilka sekund na plik, a zbiór gminy
ma ich nawet kilkaset — praca musi iść w tle, z postępem i możliwością przerwania. Logikę
(bez QGIS) ma :mod:`gs_mapa.core.app_set_convert` — tutaj tylko zadanie i sygnały.
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable, List, Optional

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import QgsApplication, QgsTask

from ..core.app_convert import ConvertOptions
from ..core.app_set_convert import MODE_REPLACE, ConvertBatch, convert_batch

logger = logging.getLogger("GSMapa.AppSetConvert")


class _ConvertTask(QgsTask):
    def __init__(self, acts: List, mode: str, backup: bool, options: ConvertOptions, xsd: bool):
        super().__init__(f"GS Mapa: konwersja {len(acts)} aktów do APP 3.0", QgsTask.Flag.CanCancel)
        self.acts = acts
        self.mode = mode
        self.backup = backup
        self.options = options
        self.xsd = xsd
        self.batch: Optional[ConvertBatch] = None
        self.error: Optional[str] = None
        self.done: Optional[Callable] = None

    def run(self) -> bool:
        try:
            self.batch = convert_batch(self.acts, self.mode, self.backup, self.options, self.xsd,
                                       progress=self.setProgress, is_canceled=self.isCanceled)
            return True
        except Exception as error:  # noqa: BLE001 — komunikat trafia do okna
            logger.exception("Konwersja zbioru do APP 3.0 nie powiodła się")
            self.error = str(error)
            return False

    def finished(self, result: bool) -> None:
        if self.done is not None:
            self.done(self)


class AppSetConvertService(QObject):
    """Konwersja zbioru do APP 3.0; jedno zadanie naraz."""

    #: wynik (:class:`ConvertBatch`)
    finished = pyqtSignal(object)
    #: postęp 0–100
    progress = pyqtSignal(float)
    #: komunikat błędu — zadanie nie wystartowało albo padło
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._task: Optional[_ConvertTask] = None

    def busy(self) -> bool:
        return self._task is not None

    def start(self, acts: Iterable, mode: str = MODE_REPLACE, backup: bool = True,
              options: Optional[ConvertOptions] = None, xsd: bool = True) -> bool:
        """
        Uruchamia konwersję aktów w tle.

        :param acts: akty do konwersji (:class:`core.app_set.SetAct`).
        :param mode: ``replace`` (zastąp pliki, kopie ``.bak``) albo ``beside`` (zapisz obok).
        :param backup: kopiować źródło przed zastąpieniem.
        :param options: data ``obowiazujeOd`` dla aktów bez tej daty.
        :param xsd: sprawdzać wynik schematem XSD 3.0.
        :return: ``True``, gdy zadanie ruszyło.
        """
        if self._task is not None:
            return False
        task = _ConvertTask(list(acts), mode, backup, options or ConvertOptions(), xsd)
        task.done = self._on_done
        task.progressChanged.connect(self.progress.emit)
        self._task = task  # menedżer zadań nie trzyma referencji Pythona
        QgsApplication.taskManager().addTask(task)
        return True

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def _on_done(self, task: _ConvertTask) -> None:
        if task is not self._task:
            return
        self._task = None
        if task.batch is not None:
            self.finished.emit(task.batch)
        else:
            self.failed.emit(task.error or "Nie udało się przekonwertować zbioru.")
