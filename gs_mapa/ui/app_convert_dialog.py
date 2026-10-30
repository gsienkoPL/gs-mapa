"""Okno konwersji pliku GML APP 1.0 / 2.0 do APP 3.0."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from qgis.PyQt.QtCore import QDate, Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDateEdit,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)
from qgis.gui import QgsFileWidget

from ..core.app_convert import SOURCE_VERSIONS, TARGET_VERSION, ConvertOptions, default_target
from ..core.app_gml import AppGmlError, scan_app_classes
from ..services.app_convert import AppConvertService, ConvertOutcome
from . import theme

logger = logging.getLogger("GSMapa.AppConvertDialog")

COL_OBJECT, COL_FIELD, COL_MESSAGE = range(3)
#: Role kolorów wierszy raportu (``theme``).
_GREEN, _RED, _ORANGE = "ok", "err", "warn"


class AppConvertDialog(QDialog):
    """Konwersja GML APP do najnowszej specyfikacji (3.0): wybór plików, opcje, raport zmian."""

    def __init__(self, parent=None, convert_service: Optional[AppConvertService] = None,
                 open_validation: Optional[Callable[[str], None]] = None,
                 add_file: Optional[Callable[[str], None]] = None,
                 message: Optional[Callable[[str, str], None]] = None):
        super().__init__(parent)
        # Własny wygląd wtyczki (makiety docs/ui), niezależny od stylu Qt w QGIS.
        theme.apply_plugin_style(self)
        self.setWindowTitle(self.tr("Konwersja GML APP do APP 3.0"))
        self.setObjectName("GSMapaAppConvertDialog")
        self.resize(820, 560)
        self._service = convert_service or AppConvertService(self)
        self._open_validation = open_validation
        self._add_file = add_file
        self._message = message
        self._outcome: Optional[ConvertOutcome] = None
        self._source_version: Optional[str] = None

        layout = QVBoxLayout(self)
        intro = QLabel(self.tr(
            "Przepisuje plik GML aktu planowania przestrzennego z APP 1.0 albo 2.0 do APP 3.0: "
            "przestrzeń nazw i schemat 3.0, kolekcja wfs:FeatureCollection, wymagane w 3.0 daty "
            "obowiazujeOd aktu i rysunków. Identyfikatory i geometria pozostają bez zmian; każda "
            "zmiana treści jest wypisana w raporcie. Plik źródłowy nie jest modyfikowany."
        ))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.source_widget = QgsFileWidget()
        self.source_widget.setFilter(self.tr("GML APP (*.gml *.GML *.xml)"))
        self.source_widget.setDialogTitle(self.tr("Plik GML APP 1.0 / 2.0"))
        form.addRow(self.tr("Plik źródłowy:"), self.source_widget)
        self.version_label = QLabel("")
        form.addRow(self.tr("Wersja pliku:"), self.version_label)
        self.target_widget = QgsFileWidget()
        self.target_widget.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.target_widget.setFilter(self.tr("GML (*.gml)"))
        self.target_widget.setDialogTitle(self.tr("Zapisz plik APP 3.0"))
        form.addRow(self.tr("Plik wynikowy:"), self.target_widget)

        date_row = QHBoxLayout()
        self.check_date = QCheckBox(self.tr("Gdy brak obowiazujeOd i daty wejścia w życie uchwały:"))
        self.check_date.setToolTip(self.tr(
            "APP 3.0 wymaga obowiazujeOd w akcie i rysunku. Najpierw używana jest dataWejsciaWZycie "
            "dokumentu uchwalającego; ta data jest używana tylko, gdy jej brak (np. projekt aktu)."))
        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setEnabled(False)
        date_row.addWidget(self.check_date)
        date_row.addWidget(self.date_edit)
        date_row.addStretch(1)
        form.addRow(self.tr("Data obowiązywania:"), date_row)
        self.check_xsd = QCheckBox(self.tr("Sprawdź wynik ze schematem XSD APP 3.0"))
        self.check_xsd.setChecked(True)
        form.addRow("", self.check_xsd)
        layout.addLayout(form)

        run_row = QHBoxLayout()
        self.btn_convert = QPushButton(self.tr("Konwertuj"))
        self.btn_convert.setDefault(True)
        self.btn_cancel = QPushButton(self.tr("Przerwij"))
        run_row.addStretch(1)
        run_row.addWidget(self.btn_convert)
        run_row.addWidget(self.btn_cancel)
        layout.addLayout(run_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.summary = QLabel(self.tr("Wybierz plik GML APP 1.0 albo 2.0."))
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels([self.tr("Obiekt"), self.tr("Atrybut"), self.tr("Zmiana / uwaga")])
        self.tree.setRootIsDecorated(False)
        self.tree.setWordWrap(True)
        self.tree.setAlternatingRowColors(True)
        header = self.tree.header()
        header.setSectionResizeMode(COL_OBJECT, header.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_FIELD, header.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_MESSAGE, header.ResizeMode.Stretch)
        self.tree.setColumnWidth(COL_OBJECT, 260)
        theme.align_headers(self.tree)
        layout.addWidget(self.tree, 1)

        buttons = QHBoxLayout()
        self.btn_validate = QPushButton(self.tr("Waliduj wynik…"))
        self.btn_validate.setToolTip(self.tr("Okno walidacji: XSD, reguły APP 3.0 i geometria"))
        self.btn_add = QPushButton(self.tr("Dodaj wynik do projektu"))
        self.btn_close = QPushButton(self.tr("Zamknij"))
        buttons.addWidget(self.btn_validate)
        buttons.addWidget(self.btn_add)
        buttons.addStretch(1)
        buttons.addWidget(self.btn_close)
        layout.addLayout(buttons)

        self.source_widget.fileChanged.connect(self._on_source_changed)
        self.target_widget.fileChanged.connect(self._update_buttons)
        self.check_date.toggled.connect(self.date_edit.setEnabled)
        self.btn_convert.clicked.connect(self.start)
        self.btn_cancel.clicked.connect(self._service.cancel)
        self.btn_validate.clicked.connect(self.validate_result)
        self.btn_add.clicked.connect(self.add_result)
        self.btn_close.clicked.connect(self.close)
        self._service.progress.connect(self._on_progress)
        self._service.finished.connect(self.show_outcome)
        self._update_buttons()

    # --- stan -----------------------------------------------------------------

    def source(self) -> str:
        return self.source_widget.filePath().strip()

    def target(self) -> str:
        return self.target_widget.filePath().strip()

    def set_source(self, path: str) -> None:
        self.source_widget.setFilePath(path)
        self._on_source_changed(path)

    def options(self) -> ConvertOptions:
        date = self.date_edit.date().toString("yyyy-MM-dd") if self.check_date.isChecked() else None
        return ConvertOptions(effective_date=date)

    def _log(self, text: str, level: str = "INFO") -> None:
        if self._message is not None:
            self._message(text, level)

    def _on_source_changed(self, path: str = "") -> None:
        path = self.source()
        self._source_version = None
        if not path or not Path(path).is_file():
            theme.set_html(self.version_label, "")
        else:
            try:
                version, classes = scan_app_classes(path)
            except (AppGmlError, OSError) as e:
                theme.set_html(self.version_label, theme.span(str(e), "err"))
            else:
                self._source_version = version
                if version is None:
                    text = "<span style='color:{c:err}'>" + self.tr("brak obiektów APP") + "</span>"
                elif version == TARGET_VERSION:
                    text = "<span style='color:{c:ok}'>" + self.tr("APP 3.0 — konwersja niepotrzebna") + "</span>"
                elif version not in SOURCE_VERSIONS:
                    text = "<span style='color:{c:err}'>" + self.tr("APP {0} — nieobsługiwana").format(version) + "</span>"
                else:
                    text = self.tr("APP {0} (klasy: {1})").format(version, ", ".join(classes))
                theme.set_html(self.version_label, text)
                if version in SOURCE_VERSIONS and not self.target():
                    self.target_widget.setFilePath(str(default_target(path)))
        self._update_buttons()

    def _on_progress(self, value: float) -> None:
        self.progress.setValue(int(value))

    def _update_buttons(self, *args) -> None:
        busy = self._service.busy()
        ready = (self._source_version in SOURCE_VERSIONS and bool(self.target())
                 and Path(self.target()).resolve() != Path(self.source()).resolve())
        self.btn_convert.setEnabled(ready and not busy)
        self.btn_cancel.setEnabled(busy)
        done = self._outcome is not None and self._outcome.ok and Path(self._outcome.target).is_file()
        self.btn_validate.setEnabled(done and not busy and self._open_validation is not None)
        self.btn_add.setEnabled(done and not busy and self._add_file is not None)
        # Nieaktywny przycisk ma w podpowiedzi powód (checklista UI, pkt 9).
        reason = self.tr("Najpierw przekonwertuj plik („Konwertuj”)")
        self.btn_validate.setToolTip(self.tr("Okno walidacji: XSD, reguły APP 3.0 i geometria")
                                     if self.btn_validate.isEnabled() else reason)
        self.btn_add.setToolTip(self.tr("Dodaj tabele wyniku do projektu")
                                if self.btn_add.isEnabled() else reason)

    # --- konwersja ------------------------------------------------------------

    def start(self) -> bool:
        if not self.btn_convert.isEnabled():
            return False
        if not self._service.start(self.source(), self.target(), self.options(), self.check_xsd.isChecked()):
            return False
        self._outcome = None
        self.tree.clear()
        self.progress.setValue(0)
        self.progress.setVisible(True)
        theme.set_html(self.summary, self.tr("Konwersja {0}…").format(Path(self.source()).name))
        self._log(f"Konwersja do APP 3.0: {self.source()} → {self.target()}")
        self._update_buttons()
        return True

    def show_outcome(self, outcome: ConvertOutcome) -> None:
        self._outcome = outcome
        self.progress.setVisible(False)
        self.tree.clear()
        if not outcome.ok:
            theme.set_html(self.summary, f"<b>{Path(outcome.source).name}</b>: "
                           + theme.span(str(outcome.error), "err"))
            self._log(f"Konwersja {outcome.source} nie powiodła się: {outcome.error}", "BŁĄD")
            self._update_buttons()
            return
        result = outcome.result
        if result is None:  # ok bez wyniku nie powinno wystąpić — nie zgłaszaj pustego raportu
            self._update_buttons()
            return
        lines = [f"<b>{Path(outcome.target).name}</b> — {result.summary()} ({outcome.seconds} s)"]
        xsd = outcome.xsd
        if xsd is None:
            lines.append(self.tr("XSD wyniku nie był sprawdzany."))
        elif not xsd.executed:
            lines.append(theme.span(f"{self.tr('Pominięto XSD')}: {xsd.skipped}", "warn"))
        elif xsd.valid:
            lines.append("<b style='color:{c:ok}'>" + self.tr("Wynik zgodny z XSD APP 3.0.") + "</b>")
        else:
            lines.append("<b style='color:{c:err}'>" + self.tr("XSD APP 3.0: błędów {0}").format(xsd.error_count)
                         + "</b>")
        theme.set_html(self.summary, "<br>".join(lines))
        for change in result.changes:
            self._row(change.object_id, change.field, change.message, _ORANGE)
        for warning in result.warnings:
            self._row(self.tr("(plik)"), "", warning, _ORANGE)
        if xsd is not None and xsd.executed:
            for line, message in xsd.errors:
                self._row(self.tr("XSD, linia {0}").format(line or "?"), "", message, _RED)
        if not self.tree.topLevelItemCount():
            self._row(self.tr("(plik)"), "", self.tr("Bez zmian treści — zmieniono tylko przestrzeń nazw i schemat."),
                      _GREEN)
        level = "INFO" if xsd is None or xsd.valid or not xsd.executed else "UWAGA"
        self._log(f"Konwersja do APP 3.0: {outcome.target} — {result.summary()}", level)
        self._update_buttons()

    def _row(self, obj: str, field: str, message: str, color: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem(self.tree)
        item.setText(COL_OBJECT, obj)
        item.setToolTip(COL_OBJECT, obj)
        item.setText(COL_FIELD, field)
        item.setText(COL_MESSAGE, message)
        item.setToolTip(COL_MESSAGE, message)
        theme.set_item_role(item, COL_MESSAGE, color)
        return item

    def validate_result(self) -> bool:
        if self._outcome is None or not self._outcome.ok or self._open_validation is None:
            return False
        self._open_validation(self._outcome.target)
        return True

    def add_result(self) -> bool:
        if self._outcome is None or not self._outcome.ok or self._add_file is None:
            return False
        self._add_file(self._outcome.target)
        return True

    def closeEvent(self, event) -> None:  # noqa: N802 - nazwa z API Qt
        self._service.cancel()
        super().closeEvent(event)
