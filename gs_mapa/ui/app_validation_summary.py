"""
Karta podsumowania walidacji pliku GML APP.

Układ wg ``docs/ui/UI_GUIDELINES.md`` (pkt 6.9) i makiety: ikona z werdyktem,
wiersz „APP x.y · EPSG · czas”, trzy liczniki (błędy, ostrzeżenia, informacje),
metryki geometrii (powierzchnia aktu, strefy, nakładające się pary, luki) oraz
rozwijany wiersz „Obiekty w pliku: N”.

Kolor nigdy nie jest jedynym nośnikiem informacji — obok ikony jest zawsze słowo.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.app_recent import plural_errors, plural_infos, plural_warnings
from ..core.app_rules import ERROR, INFO, WARNING
from . import theme

logger = logging.getLogger("GSMapa.ValidationSummary")

#: Nazwa klasy APP -> nazwa zbiorcza w polsku (wiersz „Obiekty w pliku”).
_KIND_NAMES = {
    "AktPlanowaniaPrzestrzennego": "akt",
    "DokumentFormalny": "dokumenty",
    "RysunekAktuPlanowaniaPrzestrzennego": "rysunki",
    "StrefaPlanistyczna": "strefy",
    "ObszarUzupelnieniaZabudowy": "obszary",
    "ObszarZabudowySrodmiejskiej": "obszary",
    "ObszarStandardowDostepnosciInfrastrukturySpolecznej": "obszary",
}

#: Ikony statusu wg poziomu.
_SEVERITY_ICONS = {ERROR: "status-err", WARNING: "status-warn", INFO: "status-info"}


def format_number(value: float, decimals: int = 0) -> str:
    """
    Liczba w zapisie polskim: ``14 583,02``.

    Separator tysięcy to twarda spacja (niełamliwa), dziesiętne — przecinek.
    """
    text = f"{value:,.{decimals}f}"
    return text.replace(",", " ").replace(".", ",")


def _kind_names(counts: Dict[str, int]) -> List[str]:
    """Nazwy zbiorcze obecnych w pliku klas, w stałej kolejności."""
    names = []
    for kind, count in counts.items():
        name = _KIND_NAMES.get(kind)
        if name and name not in names and count:
            names.append(name)
    return names


class ValidationSummary(QFrame):
    """Podsumowanie wyniku walidacji: werdykt, liczniki, metryki, liczba obiektów."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("gsmapa_validation_summary")
        # Karta z tłem powierzchni i obramowaniem, jak na makiecie (arkusz stylów wtyczki).
        self.setProperty("gsmapa_card", True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # --- werdykt: ikona + słowo + trzy liczniki -------------------------------
        top = QHBoxLayout()
        top.setSpacing(8)
        self._icon = QLabel()
        self._icon.setFixedWidth(theme.TOOLBAR_SIZE + 4)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        self._verdict = QLabel()
        self._verdict.setFont(theme.bold_font())
        self._verdict.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._counters = QHBoxLayout()
        self._counters.setSpacing(6)
        top.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignTop)
        top.addWidget(self._verdict, 1, Qt.AlignmentFlag.AlignTop)
        top.addLayout(self._counters)
        layout.addLayout(top)

        self._meta = QLabel()
        theme.style_muted(self._meta, small=True)
        self._meta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._meta)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(line)

        # --- metryki geometrii ------------------------------------------------------
        self._metrics = QGridLayout()
        self._metrics.setContentsMargins(0, 0, 0, 0)
        self._metrics.setHorizontalSpacing(16)
        self._metrics.setVerticalSpacing(0)
        self._metric_widgets: List[tuple] = []
        layout.addLayout(self._metrics)

        # --- rozwijany wiersz „Obiekty w pliku” -------------------------------------
        self._objects_btn = QToolButton()
        self._objects_btn.setText(self.tr("Obiekty w pliku: —"))
        self._objects_btn.setCheckable(True)
        self._objects_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._objects_btn.setAutoRaise(True)
        self._objects_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        theme.set_icon(self._objects_btn, "chevron-right", role="WindowText")  # szewron jak w sekcjach, nie strzałka stylu Qt
        self._objects_btn.setIconSize(QSize(theme.CHIP_SIZE, theme.CHIP_SIZE))
        theme.button_font(self._objects_btn)
        self._objects_btn.toggled.connect(self._on_objects_toggled)
        layout.addWidget(self._objects_btn, 0, Qt.AlignmentFlag.AlignLeft)

        self._objects_detail = QLabel()
        theme.style_muted(self._objects_detail, small=True)
        self._objects_detail.setWordWrap(True)
        self._objects_detail.setVisible(False)
        layout.addWidget(self._objects_detail)

        self.setVisible(False)

    # --- treść ------------------------------------------------------------------

    def clear(self) -> None:
        """Pusty stan (przed walidacją)."""
        self.setVisible(False)

    def set_report(self, report) -> None:
        """Pokazuje podsumowanie raportu walidacji."""
        counts = report.summary
        errors, warnings, infos = counts.get(ERROR, 0), counts.get(WARNING, 0), counts.get(INFO, 0)
        if report.fatal:
            self._set_verdict("status-err", "err", self.tr("Nie udało się sprawdzić pliku"))
            self._meta.setText(self.tr("{0} Sprawdź, czy plik to poprawny GML APP.").format(report.fatal))
        elif report.previous:
            self._set_verdict("status-info", "info", self.tr("Porównanie wersji"))
            self._meta.setText(self.tr("{0} → {1}").format(Path(report.previous).name,
                                                          Path(report.path).name))
        elif errors:
            self._set_verdict("status-err", "err", plural_errors(errors))
            self._meta.setText(self._meta_text(report))
        elif warnings:
            self._set_verdict("status-warn", "warn", plural_warnings(warnings))
            self._meta.setText(self._meta_text(report))
        else:
            self._set_verdict("status-ok", "ok", self.tr("Bez błędów"))
            self._meta.setText(self._meta_text(report))

        self._set_counters(errors, warnings, infos)
        self._set_metrics(report)
        self._set_objects(report)
        self.setVisible(True)

    def _set_verdict(self, icon: str, role: str, text: str) -> None:
        self._icon.setPixmap(theme.pixmap(icon, theme.TOOLBAR_SIZE, role, self))
        self._icon.setAccessibleName(text)
        self._verdict.setText(text)
        theme.set_text_role(self._verdict, role)

    def _meta_text(self, report) -> str:
        parts = [self.tr("APP {0}").format(report.version or "?")]
        if report.crs:
            parts.append(report.crs)
        if report.seconds:
            parts.append(self.tr("sprawdzono w {0} s").format(format_number(report.seconds, 1)))
        text = " · ".join(parts)
        for stage, reason in report.skipped:
            text += " · " + self.tr("pominięto {0}: {1}").format(stage, reason)
        return text

    def _set_counters(self, errors: int, warnings: int, infos: int) -> None:
        """Trzy liczniki: błędy, ostrzeżenia, informacje (ikona + liczba + słowo)."""
        while self._counters.count():
            item = self._counters.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        for text, icon, role, count in (
            (plural_errors(errors), "status-err", "err", errors),
            (plural_warnings(warnings), "status-warn", "warn", warnings),
            (plural_infos(infos), "status-info", "info", infos),
        ):
            # Licznik zerowy nie jest alarmem: tekst i ikona pomocnicze, kolor tylko przy uwagach.
            self._counters.addWidget(self._counter(text, icon, role if count else "muted"))

    def _counter(self, text: str, icon: str, role: str) -> QWidget:
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        pixmap = QLabel()
        pixmap.setPixmap(theme.pixmap(icon, theme.TOOLBAR_SIZE, role, widget))
        label = QLabel(text)
        theme.set_text_role(label, role)
        row.addWidget(pixmap, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(label, 0, Qt.AlignmentFlag.AlignVCenter)
        return widget

    def _set_metrics(self, report) -> None:
        """Metryki geometrii: powierzchnia aktu, strefy, nakładające się pary, luki."""
        while self._metrics.count():
            item = self._metrics.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        m = report.measurements
        if not m:
            self._metric_widgets = []
            return
        values = []
        if "act_area_m2" in m:
            values.append((self.tr("Powierzchnia aktu"),
                           f"{format_number(m['act_area_m2'] / 10000, 2)} ha"))
        if "zone_count" in m:
            values.append((self.tr("Strefy planistyczne"), format_number(m["zone_count"])))
        if "overlap_pairs" in m:
            values.append((self.tr("Nakładające się pary"), format_number(m["overlap_pairs"])))
        if "gap_area_m2" in m:
            values.append((self.tr("Luki między strefami"), f"{format_number(m['gap_area_m2'], 2)} m²"))
        for column, (label, value) in enumerate(values):
            caption = QLabel(label)
            theme.style_muted(caption, small=True)
            number = QLabel(value)
            number.setFont(theme.bold_font())
            self._metrics.addWidget(caption, 0, column)
            self._metrics.addWidget(number, 1, column)
        self._metric_widgets = values

    def _set_objects(self, report) -> None:
        """Rozwijany wiersz „Obiekty w pliku: N” z rozbiciem na klasy."""
        total = sum(report.counts.values()) if report.counts else 0
        names = _kind_names(report.counts)
        self._objects_btn.setText(self.tr("Obiekty w pliku: {0}").format(format_number(total)))
        self._objects_btn.setToolTip(
            self.tr("Klasy obiektów w pliku: {0}").format(", ".join(names)) if names else "")
        self._objects_detail.setText(
            "{0}: {1}".format(self.tr("Obiekty w pliku"), ", ".join(names)) if names else "")
        self._objects_btn.setChecked(False)
        self._objects_detail.setVisible(False)

    def _on_objects_toggled(self, checked: bool) -> None:
        theme.set_icon(self._objects_btn, "chevron-down" if checked else "chevron-right", role="WindowText")
        self._objects_detail.setVisible(checked)

    # --- testy -------------------------------------------------------------------

    def verdict(self) -> str:
        """Tekst werdyktu (np. „Bez błędów”)."""
        return self._verdict.text()

    def counters(self) -> List[str]:
        """Teksty trzech liczników."""
        result = []
        for index in range(self._counters.count()):
            item = self._counters.itemAt(index)
            widget = item.widget()
            if widget is not None:
                labels = widget.findChildren(QLabel)
                if len(labels) > 1:
                    result.append(labels[1].text())
        return result

    def metric_values(self) -> List[str]:
        """Wartości metryk geometrii."""
        return [value for _label, value in self._metric_widgets]

    def objects_text(self) -> str:
        """Tekst wiersza „Obiekty w pliku”."""
        return self._objects_btn.text()
