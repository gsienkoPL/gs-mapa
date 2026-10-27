"""Konwersja GML APP 1.0 / 2.0 → APP 3.0 w tle (``QgsTask``) ze sprawdzeniem wyniku XSD 3.0."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import QgsApplication, QgsTask

from ..core.app_convert import AppConvertError, ConvertOptions, ConvertResult, TARGET_VERSION, convert_to_app30
from ..core.app_gml import AppGmlError
from ..core.app_xsd import XsdResult, validate_xsd

logger = logging.getLogger("GSMapa.AppConvert")


@dataclass
class ConvertOutcome:
    source: str
    target: str
    result: Optional[ConvertResult] = None
    #: Wynik XSD APP 3.0 pliku wynikowego (``None``: nie sprawdzano).
    xsd: Optional[XsdResult] = None
    error: Optional[str] = None
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None and self.result is not None


def convert_file(source: str, target: str, options: Optional[ConvertOptions] = None, xsd: bool = True,
                 progress: Optional[Callable[[float], None]] = None,
                 is_canceled: Optional[Callable[[], bool]] = None) -> ConvertOutcome:
    """Konwersja (0–90 % postępu) i XSD wyniku (90–100 %) — synchronicznie (zadanie, testy)."""
    started = time.monotonic()
    outcome = ConvertOutcome(source=str(source), target=str(target))
    try:
        scaled = (lambda value: progress(value * 0.9)) if progress else None
        outcome.result = convert_to_app30(source, target, options, scaled, is_canceled)
        if xsd:
            outcome.xsd = validate_xsd(target, TARGET_VERSION)
        if progress:
            progress(100.0)
    except (AppConvertError, AppGmlError, OSError) as e:
        outcome.error = str(e)
    except Exception as e:  # noqa: BLE001 — błąd trafia do okna, nie przerywa QGIS
        logger.exception("Konwersja nie powiodła się")
        outcome.error = f"Nieoczekiwany błąd: {e}"
    outcome.seconds = round(time.monotonic() - started, 1)
    return outcome


class _ConvertTask(QgsTask):
    def __init__(self, source: str, target: str, options: ConvertOptions, xsd: bool):
        super().__init__(f"GS Mapa: konwersja {Path(source).name} do APP {TARGET_VERSION}", QgsTask.Flag.CanCancel)
        self.source, self.target, self.options, self.xsd = source, target, options, xsd
        self.outcome: Optional[ConvertOutcome] = None
        self.done: Optional[Callable] = None

    def run(self) -> bool:
        self.outcome = convert_file(self.source, self.target, self.options, self.xsd,
                                    self.setProgress, self.isCanceled)
        return True

    def finished(self, result: bool) -> None:
        if self.done is not None:
            self.done(self)


class AppConvertService(QObject):
    """Uruchamia konwersję w menedżerze zadań QGIS; jedna konwersja naraz."""

    #: :class:`ConvertOutcome`
    finished = pyqtSignal(object)
    #: postęp 0–100
    progress = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._task: Optional[_ConvertTask] = None

    def busy(self) -> bool:
        return self._task is not None

    def start(self, source: str, target: str, options: Optional[ConvertOptions] = None, xsd: bool = True) -> bool:
        if self._task is not None:
            return False
        task = _ConvertTask(source, target, options or ConvertOptions(), xsd)
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
        self.finished.emit(task.outcome or ConvertOutcome(task.source, task.target, error="Konwersję przerwano."))
