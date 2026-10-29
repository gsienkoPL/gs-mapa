"""Zapis hurtowej zmiany identyfikatorów w tle (``QgsTask``).

Przepisanie planu ogólnego miasta (kilkadziesiąt MB, tysiące obiektów) trwa kilka sekund,
więc zapis idzie w tle, a okno dostaje wynik. Logikę (bez QGIS) ma
:mod:`gs_mapa.core.app_set_ids` — tutaj tylko zadanie i sygnały.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import QgsApplication, QgsTask

from ..core.app_set_ids import ApplyResult, IdPlan, apply_plan

logger = logging.getLogger("GSMapa.AppSetIds")


class _ApplyTask(QgsTask):
    def __init__(self, plan: IdPlan, backup: bool, rename_files: bool):
        super().__init__(f"GS Mapa: zmiana identyfikatorów ({len(plan.files)} plików)")
        self.plan = plan
        self.backup = backup
        self.rename_files = rename_files
        self.result: Optional[ApplyResult] = None
        self.error: Optional[str] = None
        self.done: Optional[Callable] = None

    def run(self) -> bool:
        try:
            self.result = apply_plan(self.plan, self.backup, self.rename_files)
            return True
        except Exception as error:  # noqa: BLE001 — komunikat trafia do użytkownika
            logger.exception("Zmiana identyfikatorów nie powiodła się")
            self.error = str(error)
            return False

    def finished(self, result: bool) -> None:
        if self.done is not None:
            self.done(self)


class AppSetIdsService(QObject):
    """Zapis zmiany identyfikatorów; jedno zadanie naraz."""

    #: wynik (:class:`ApplyResult`)
    finished = pyqtSignal(object)
    #: komunikat błędu
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._task: Optional[_ApplyTask] = None

    def busy(self) -> bool:
        return self._task is not None

    def start(self, plan: IdPlan, backup: bool = True, rename_files: bool = True) -> bool:
        """Zapisuje plan w tle; ``False``, gdy poprzedni zapis jeszcze trwa albo planu nie da się zapisać."""
        if self._task is not None or not plan.ok:
            return False
        task = _ApplyTask(plan, backup, rename_files)
        task.done = self._on_done
        self._task = task  # menedżer zadań nie trzyma referencji Pythona
        QgsApplication.taskManager().addTask(task)
        return True

    def _on_done(self, task: _ApplyTask) -> None:
        if task is not self._task:
            return
        self._task = None
        if task.result is not None:
            self.finished.emit(task.result)
        else:
            self.failed.emit(task.error or "Nie udało się zapisać zmian.")
