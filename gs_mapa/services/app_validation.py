"""Walidacja pliku GML APP w tle: XSD, reguły APP i geometria (bez bazy danych).

Kolejne etapy zadania (``QgsTask``):

1. odczyt pliku i reguły APP (``core/app_gml.py``, ``core/app_rules.py``),
2. XSD urzędowego schematu wersji pliku (``core/app_xsd.py``, lxml),
3. geometria w QGIS/GEOS na warstwach OGR (``services/app_geometry.py``).

Wynik (:class:`ValidationReport`) trafia do okna sygnałem z wątku GUI.
"""

from __future__ import annotations

import csv
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.core import Qgis, QgsApplication, QgsTask, QgsVectorLayer

from ..core.app_gml import AppGmlError, read_app_gml
from ..core.app_rules import ERROR, RULES, Finding, check_document, summarize
from ..core.app_update import read_act_source
from ..core.app_versioning import compare_versions
from ..core.app_xsd import XsdResult, validate_xsd
from .app_geometry import Canceled, check_gml_geometry

logger = logging.getLogger("GSMapa.AppValidation")


@dataclass
class ValidationReport:
    path: str
    version: Optional[str] = None
    counts: Dict[str, int] = field(default_factory=dict)
    xsd: Optional[XsdResult] = None
    findings: List[Finding] = field(default_factory=list)
    measurements: Dict[str, float] = field(default_factory=dict)
    #: Układ współrzędnych WKT w ``findings`` (np. ``EPSG:2177``).
    crs: str = ""
    #: Błąd, który przerwał walidację (plik nieczytelny, przerwanie).
    fatal: Optional[str] = None
    #: Plik poprzedniej wersji, gdy raport jest porównaniem dwóch wersji (``compare_files``).
    previous: Optional[str] = None
    #: Etapy pominięte: (etap, powód).
    skipped: List[tuple] = field(default_factory=list)
    #: Wykonane etapy: ``REGULY``, ``XSD``, ``GEOMETRIA``.
    checked: List[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def summary(self) -> Dict[str, int]:
        return summarize(self.findings)

    @property
    def valid(self) -> bool:
        """Brak błędów (ostrzeżenia dopuszczalne) i wszystkie etapy wykonane."""
        return self.fatal is None and not self.skipped and self.summary[ERROR] == 0


def validate_file(path: str, xsd: bool = True, geometry: bool = True,
                  progress=None, is_canceled=None, stage=None) -> ValidationReport:
    """
    Pełna walidacja pliku GML APP (synchronicznie — w zadaniu albo w testach).

    :param progress: ``callable(0–100)``; :param is_canceled: ``callable() -> bool``;
    :param stage: ``callable(str)`` z nazwą bieżącego etapu.
    """
    started = time.monotonic()
    report = ValidationReport(path=str(path))

    def step(text, value):
        if stage is not None:
            stage(text)
        if progress is not None:
            progress(value)

    def canceled():
        return is_canceled is not None and is_canceled()

    try:
        step("Odczyt pliku i reguły APP", 0)
        doc = read_app_gml(path)
        report.version, report.counts = doc.version, doc.counts()
        report.findings.extend(check_document(doc))
        report.checked.append("REGULY")
        if report.version not in ("2.0", "3.0"):
            report.skipped.append(("XSD i geometria", "plik nie jest w APP 2.0 ani 3.0"))
            return report
        if canceled():
            raise Canceled()
        if xsd:
            step(f"Walidacja XSD APP {report.version}", 20)
            report.xsd = validate_xsd(path, report.version)
            if report.xsd.executed:
                report.checked.append("XSD")
            else:
                report.skipped.append(("XSD", report.xsd.skipped))
            for line, message in report.xsd.errors:
                report.findings.append(Finding("XSD", ERROR, message, line=line))
            hidden = report.xsd.error_count - len(report.xsd.errors)
            if hidden > 0:
                report.findings.append(Finding("XSD", ERROR, f"… i {hidden} kolejnych błędów XSD (pominięte w raporcie)."))
        if canceled():
            raise Canceled()
        if geometry:
            step("Geometria (QGIS)", 35)
            result = check_gml_geometry(
                str(path), set(report.counts),
                progress=(lambda v: progress(35 + 0.65 * v)) if progress is not None else None,
                is_canceled=is_canceled,
            )
            report.findings.extend(result.findings)
            report.measurements, report.crs = result.measurements, result.crs
            report.checked.append("GEOMETRIA")
        step("Gotowe", 100)
    except AppGmlError as e:
        report.fatal = str(e)
    except Canceled:
        report.fatal = "Walidację przerwano."
    finally:
        report.seconds = round(time.monotonic() - started, 1)
    return report


def write_csv(report: ValidationReport, path: str) -> None:
    """Raport do CSV (UTF-8 z BOM, separator ``;`` — otwiera się wprost w arkuszu)."""
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["poziom", "reguła", "klasa", "gml:id", "atrybut", "linia", "komunikat", "miara", "wkt"])
        for f in report.findings:
            writer.writerow([f.severity_label, f.rule, f.kind or "", f.object_id or "", f.field or "",
                             f.line or "", f.message, "" if f.measure is None else f.measure, f.wkt or ""])


def rules_text() -> str:
    """Lista sprawdzanych reguł (do okna i raportu)."""
    return "\n".join(f"{r.rule_id}: {r.title}" for r in RULES)


class _ValidationTask(QgsTask):
    def __init__(self, path: str, xsd: bool, geometry: bool):
        super().__init__(f"GS Mapa: walidacja {Path(path).name}", QgsTask.Flag.CanCancel)
        self.path, self.xsd, self.geometry = path, xsd, geometry
        self.report: Optional[ValidationReport] = None
        self.stage = ""
        self.done: Optional[Callable] = None

    def run(self) -> bool:
        try:
            self.report = validate_file(self.path, self.xsd, self.geometry, self.setProgress,
                                        self.isCanceled, self._set_stage)
        except Exception as e:  # noqa: BLE001 — każdy błąd trafia do raportu w oknie
            logger.exception("Walidacja nie powiodła się")
            self.report = ValidationReport(path=self.path, fatal=f"Nieoczekiwany błąd: {e}")
        return True

    def _set_stage(self, text: str) -> None:
        self.stage = text

    def finished(self, result: bool) -> None:
        if self.done is not None:
            self.done(self)


class AppValidationService(QObject):
    """Uruchamia walidację w menedżerze zadań QGIS; jedna walidacja naraz."""

    #: raport (:class:`ValidationReport`)
    finished = pyqtSignal(object)
    #: (postęp 0–100, etap)
    progress = pyqtSignal(float, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._task: Optional[_ValidationTask] = None

    def busy(self) -> bool:
        return self._task is not None

    def start(self, path: str, xsd: bool = True, geometry: bool = True) -> bool:
        if self._task is not None:
            return False
        task = _ValidationTask(path, xsd, geometry)
        task.done = self._on_done
        task.progressChanged.connect(lambda value: self.progress.emit(value, task.stage))
        self._task = task  # menedżer zadań nie trzyma referencji Pythona
        QgsApplication.taskManager().addTask(task)
        return True

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def _on_done(self, task: _ValidationTask) -> None:
        if task is not self._task:
            return
        self._task = None
        self.finished.emit(task.report or ValidationReport(path=task.path, fatal="Walidację przerwano."))


def compare_files(previous: str, current: str) -> ValidationReport:
    """
    Porównuje dwie wersje tego samego zbioru APP (dotychczasową i nową).

    Wynik jest w formacie raportu walidacji — te same drzewo, filtr i eksport CSV; etap
    ``WERSJE`` zamiast ``XSD`` i ``GEOMETRIA``.

    :param previous: plik poprzedniej wersji aktu.
    :param current: plik nowej wersji aktu.
    """
    started = time.monotonic()
    report = ValidationReport(path=str(current))
    try:
        old = read_act_source(previous)
        new = read_act_source(current)
    except (AppGmlError, OSError) as e:
        report.fatal = str(e)
        report.seconds = round(time.monotonic() - started, 2)
        return report
    report.version = new.app_version
    report.counts = dict(new.counts)
    report.previous = str(previous)
    report.findings = compare_versions(old, new)
    report.checked = ["WERSJE"]
    report.seconds = round(time.monotonic() - started, 2)
    return report


def style_error_layer(layer: QgsVectorLayer) -> None:
    """
    Symbolika warstwy z miejscami błędów walidacji: czerwony obrys i półprzezroczyste
    wypełnienie / czerwony punkt.

    To kartografia warstwy na mapie, nie kolor interfejsu — dlatego żyje w ``services/``,
    a nie w ``ui/theme.py``.
    """
    symbol = layer.renderer().symbol()
    symbol.setColor(QColor(230, 30, 30, 90))
    if layer.geometryType() == Qgis.GeometryType.Polygon:
        symbol.symbolLayer(0).setStrokeColor(QColor(230, 30, 30))
        symbol.symbolLayer(0).setStrokeWidth(0.6)
    else:
        symbol.setColor(QColor(230, 30, 30))
        symbol.setSize(3.0)
