"""
Okno walidacji pliku GML APP (2.0 / 3.0).

Układ wg ``docs/ui/UI_GUIDELINES.md`` (pkt 6.9) i makiety: pole pliku z nazwą
i katalogiem, opcje XSD i geometrii w jednym wierszu, karta podsumowania wyniku
(werdykt, liczniki, metryki, liczba obiektów) i wyniki pogrupowane według ważności.
Walidacja działa w ``QgsTask`` ( mechanizm bez zmian — zmieniona jest prezentacja).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Callable, Optional

from qgis.PyQt.QtCore import QSize, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.gui import QgsFileWidget

from ..core.app_recent import plural_objects
from ..core.app_rules import ERROR, INFO, RULES, WARNING, group_identical
from ..services.app_files import SOURCE_PROPERTY, AppFileService
from ..services.app_open_acts import KIND_FILE, KIND_REGISTRY, OpenAct
from ..services.app_validation import (
    AppValidationService, ValidationReport, compare_files, rules_text, style_error_layer, write_csv,
)
from . import theme
from .app_open_acts_dialog import OpenActsDialog
from .app_validation_summary import ValidationSummary
from .widgets import ElidedLabel, WrappingItemDelegate

logger = logging.getLogger("GSMapa.AppValidationDialog")

#: Kolumny listy wyników.
COL_SEVERITY, COL_OBJECT, COL_MESSAGE, COL_ACTION, COL_LINE = range(5)
#: Rola danych elementu: obiekt :class:`Finding`.
_FINDING_ROLE = Qt.ItemDataRole.UserRole
#: Role kolorów poziomów (``theme``).
_COLORS = {ERROR: "err", WARNING: "warn", INFO: "muted"}
_RULE_TITLES = {r.rule_id: r.title for r in RULES}
#: Krótkie nazwy reguł — na listy w interfejsie (pełne tytuły są zbyt długie).
_RULE_SHORT = {r.rule_id: (r.short or r.rule_id) for r in RULES}
#: Kolejność grup wyników (od najpoważniejszych).
_GROUPS = (ERROR, WARNING, INFO)
#: Nazwy grup.
_GROUP_NAMES = {ERROR: "Błędy", WARNING: "Ostrzeżenia", INFO: "Uwagi"}
#: Ile identyfikatorów obiektów pokazać w podpowiedzi wiersza zgrupowanej uwagi.
_TOOLTIP_IDS = 20


class _FileField(QFrame):
    """Ramka pola pliku — kliknięcie (np. w napis „Upuść plik GML albo wybierz…”) otwiera wybór pliku."""

    clicked = pyqtSignal()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - nazwa z API Qt
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


def _apply_file_icon(label: QLabel) -> None:
    """Ikona pliku w polu pliku (tekst pomocniczy; odświeżana przy zmianie motywu)."""
    label.setPixmap(theme.pixmap("file", theme.TOOLBAR_SIZE, "muted"))


def _capitalize(text: str) -> str:
    return text[:1].upper() + text[1:]


def _short_dir(path: Path) -> str:
    """Katalog pliku do pola pliku — katalog domowy jako ``~`` (jak na makiecie: „· ~/Downloads”)."""
    try:
        return str(Path("~") / path.relative_to(Path.home()))
    except ValueError:
        return str(path)


class AppValidationDialog(QDialog):
    """Walidacja pliku GML APP: XSD, reguły APP i geometria, wynik w drzewie reguł."""

    def __init__(self, parent=None, canvas: Optional[Callable] = None,
                 validation_service: Optional[AppValidationService] = None,
                 file_service: Optional[AppFileService] = None, message: Optional[Callable] = None,
                 acts_provider: Optional[Callable[[], list]] = None,
                 registry_loader: Optional[Callable[[str], bool]] = None):
        super().__init__(parent)
        # Własny wygląd wtyczki (makiety docs/ui), niezależny od stylu Qt w QGIS.
        theme.apply_plugin_style(self)
        self.setWindowTitle(self.tr("Walidacja pliku GML APP"))
        self.setObjectName("GSMapaAppValidationDialog")
        self.resize(900, 620)
        self.setAcceptDrops(True)
        self._canvas = canvas
        self._service = validation_service or AppValidationService(self)
        self._files = file_service or AppFileService(self)
        self._message = message
        #: Lista aktów i plików APP otwartych w QGIS (dostarcza karta „Pliki APP”).
        self._acts_provider = acts_provider
        #: Pobranie pliku GML aktu z Rejestru (dostarcza karta „Pliki APP”).
        self._registry_loader = registry_loader
        self._report: Optional[ValidationReport] = None
        self._error_layers: list = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # --- plik: nazwa + katalog, „Wybierz…”, „Waliduj” --------------------------
        file_row = QHBoxLayout()
        file_row.setSpacing(6)
        # Pole pliku jak na makiecie: ikona, nazwa i zaraz za nią „· katalog” w jednej ramce;
        # bez pliku — napis „Upuść plik GML albo wybierz…” (klik otwiera wybór pliku).
        self._file_field = _FileField()
        self._file_field.setProperty("gsmapa_field", True)
        self._file_field.setCursor(Qt.CursorShape.PointingHandCursor)
        self._file_field.setToolTip(self.tr("Wybierz plik GML APP albo upuść go na okno"))
        field_layout = QHBoxLayout(self._file_field)
        field_layout.setContentsMargins(8, 4, 8, 4)
        field_layout.setSpacing(6)
        self._file_icon = QLabel()
        theme.register(self._file_icon, _apply_file_icon)
        _apply_file_icon(self._file_icon)
        self._file_label = ElidedLabel(mode=Qt.TextElideMode.ElideMiddle)
        self._file_label.setAccessibleName(self.tr("Plik do walidacji"))
        self._file_dir = ElidedLabel(mode=Qt.TextElideMode.ElideMiddle)
        theme.style_muted(self._file_dir)
        for label in (self._file_label, self._file_dir):
            # Preferowana szerokość = cały tekst, przy braku miejsca zwęża się do minimum (elidowanie).
            label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        field_layout.addWidget(self._file_icon, 0)
        field_layout.addWidget(self._file_label, 0)
        field_layout.addWidget(self._file_dir, 0)
        field_layout.addStretch(1)
        self.btn_choose = QPushButton(self.tr("Wybierz…"))
        self.btn_choose.setToolTip(self.tr("Wybierz plik GML APP do sprawdzenia"))
        self.btn_open = QPushButton(self.tr("Z aktów w QGIS…"))
        self.btn_open.setToolTip(self.tr("Sprawdź akt albo plik APP, który jest już otwarty w QGIS"))
        self.btn_validate = QPushButton(self.tr("Waliduj"))
        theme.primary(self.btn_validate)
        file_row.addWidget(self._file_field, 1)
        file_row.addWidget(self.btn_open, 0)
        file_row.addWidget(self.btn_choose, 0)
        file_row.addWidget(self.btn_validate, 0)
        layout.addLayout(file_row)

        # Pole wyboru pliku (niewidoczne — dostęp do pliku z „Wybierz…” i z upuszczania).
        self.file_widget = QgsFileWidget(self)
        self.file_widget.setFilter(self.tr("GML APP (*.gml *.GML)"))
        self.file_widget.setDialogTitle(self.tr("Plik GML APP"))
        self.file_widget.setVisible(False)
        layout.addWidget(self.file_widget)

        # --- opcje: XSD i geometria w jednym wierszu -----------------------------
        options = QHBoxLayout()
        options.setSpacing(12)
        self.check_xsd = QCheckBox(self.tr("Schemat XSD"))
        self.check_xsd.setChecked(True)
        self.check_geometry = QCheckBox(self.tr("Geometria: poprawność, granica aktu, nakładanie, luki"))
        self.check_geometry.setChecked(True)
        self.check_geometry.setToolTip(rules_text())
        options.addWidget(self.check_xsd)
        options.addWidget(self.check_geometry)
        options.addStretch(1)
        # Przy wybranym pliku — podpowiedź o upuszczaniu (bez pliku mówi o tym samo pole pliku).
        self._drop_hint = QLabel(self.tr("albo upuść plik na to okno"))
        theme.style_muted(self._drop_hint, small=True)
        self._drop_hint.setVisible(False)
        options.addWidget(self._drop_hint, 0)
        layout.addLayout(options)

        # --- porównanie wersji (zwijany wiersz) -----------------------------------
        self._btn_previous = QToolButton()
        self._btn_previous.setText(self.tr("Porównaj z poprzednią wersją…"))
        self._btn_previous.setCheckable(True)
        self._btn_previous.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._btn_previous.setAutoRaise(True)
        self._btn_previous.setCursor(Qt.CursorShape.PointingHandCursor)
        theme.set_icon(self._btn_previous, "chevron-right", role="WindowText")  # szewron jak w sekcjach, nie strzałka stylu Qt
        self._btn_previous.setIconSize(QSize(theme.CHIP_SIZE, theme.CHIP_SIZE))
        theme.button_font(self._btn_previous)
        self._btn_previous.setToolTip(self.tr("Sprawdza dwie wersje tego samego zbioru: domknięcia wersji, "
                                            "treść wersji o tym samym wersjaId i zbędne wersje obiektów"))
        layout.addWidget(self._btn_previous, 0, Qt.AlignmentFlag.AlignLeft)
        self._btn_previous.toggled.connect(self._on_previous_toggled)

        previous_row = QHBoxLayout()
        previous_row.addWidget(QLabel(self.tr("Poprzednia wersja:")))
        self.previous_widget = QgsFileWidget(self)
        self.previous_widget.setFilter(self.tr("GML APP (*.gml *.GML)"))
        self.previous_widget.setDialogTitle(self.tr("Plik poprzedniej wersji GML APP"))
        self.btn_compare = QPushButton(self.tr("Porównaj wersje"))
        self.btn_compare.setToolTip(self.tr("Sprawdza dwie wersje tego samego zbioru: domknięcia wersji, "
                                           "treść wersji o tym samym wersjaId i zbędne wersje obiektów"))
        previous_row.addWidget(self.previous_widget, 1)
        previous_row.addWidget(self.btn_compare)
        self._previous_box = QWidget()
        previous_box_layout = QVBoxLayout(self._previous_box)
        previous_box_layout.setContentsMargins(0, 0, 0, 0)
        previous_box_layout.addLayout(previous_row)
        self._previous_box.setVisible(False)
        layout.addWidget(self._previous_box)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.btn_cancel = QPushButton(self.tr("Przerwij"))
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setVisible(False)
        layout.addWidget(self.btn_cancel, 0, Qt.AlignmentFlag.AlignLeft)

        # --- karta podsumowania ----------------------------------------------------
        self.summary_card = ValidationSummary()
        layout.addWidget(self.summary_card)
        # Tekst podsumowania (pod karty) — zostaje dla komunikatów i testów.
        self.summary = QLabel(self.tr("Wybierz plik GML i kliknij „Waliduj”."))
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary.setVisible(False)
        layout.addWidget(self.summary)

        # --- wyniki ----------------------------------------------------------------
        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        # Kolumna „Kontrola”: nazwa reguły — poziom niesie już grupa (Błędy, Ostrzeżenia, Uwagi) i kolor.
        self.tree.setHeaderLabels([self.tr("Kontrola"), self.tr("Obiekt"), self.tr("Komunikat"),
                                   self.tr("Mapa"), self.tr("Linia")])
        self.tree.setRootIsDecorated(True)
        self.tree.setWordWrap(True)
        self.tree.setAlternatingRowColors(True)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(COL_SEVERITY, header.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_OBJECT, header.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_MESSAGE, header.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_ACTION, header.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_LINE, header.ResizeMode.ResizeToContents)
        self.tree.setColumnWidth(COL_OBJECT, 220)
        theme.align_headers(self.tree)
        # Komunikaty i identyfikatory zawijane — bez ucinania „…” (pkt 6.9, T8).
        self._wrap_delegate = WrappingItemDelegate.attach(self.tree, (COL_OBJECT, COL_MESSAGE))
        self.tree.setToolTip(self.tr("Kliknij dwukrotnie, aby pokazać miejsce na mapie."))
        # Lista wyników niewidoczna do pierwszego wyniku; do tego czasu jej miejsce zajmuje
        # pusty obszar (przyciski zostają na dole okna).
        self.tree.setVisible(False)
        layout.addWidget(self.tree, 1)
        self._results_placeholder = QWidget()
        layout.addWidget(self._results_placeholder, 1)

        # --- przyciski: raport i mapa po lewej, zamknijcie i dodanie po prawej -----
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.btn_save = QPushButton(self.tr("Zapisz raport CSV…"))
        self.btn_errors = QPushButton(self.tr("Pokaż uwagi na mapie"))
        self.btn_errors.setToolTip(self.tr("Warstwa tymczasowa z miejscami problemów geometrii"))
        self.btn_close = QPushButton(self.tr("Zamknij"))
        self.btn_add = QPushButton(self.tr("Dodaj do projektu"))
        self.btn_add.setToolTip(self.tr("Tabele aktu ze stylami wtyczki APP (GML zapisywany do GeoPackage)"))
        theme.primary(self.btn_add)
        for button in (self.btn_save, self.btn_errors):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.btn_close)
        buttons.addWidget(self.btn_add)
        layout.addLayout(buttons)

        self._file_field.clicked.connect(self._choose_file)
        self.file_widget.fileChanged.connect(self._on_file_changed)
        self.previous_widget.fileChanged.connect(self._update_buttons)
        self.btn_choose.clicked.connect(self._choose_file)
        self.btn_open.clicked.connect(self._choose_open_act)
        self.btn_validate.clicked.connect(self.start)
        self.btn_compare.clicked.connect(self.compare)
        self.btn_cancel.clicked.connect(self._service.cancel)
        self.btn_add.clicked.connect(self.add_to_project)
        self.btn_errors.clicked.connect(self.show_errors_layer)
        self.btn_save.clicked.connect(self.save_report)
        self.btn_close.clicked.connect(self.close)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._service.progress.connect(self._on_progress)
        self._service.finished.connect(self.show_report)
        self._update_file_label()
        self._update_buttons()

    # --- stan ---------------------------------------------------------------

    def path(self) -> str:
        return self.file_widget.filePath().strip()

    def set_path(self, path: str) -> None:
        self.file_widget.setFilePath(path)
        self._update_buttons()

    def previous_path(self) -> str:
        return self.previous_widget.filePath().strip()

    def set_previous_path(self, path: str) -> None:
        self.previous_widget.setFilePath(path)
        self._update_buttons()

    def _on_file_changed(self, path: str) -> None:
        """Aktualizuje etykiety pliku i stan przycisków."""
        self._update_file_label()
        self._update_buttons()

    def _update_file_label(self) -> None:
        """Nazwa pliku (elidowana w środku) + katalog; pełna ścieżka w podpowiedzi."""
        path = self.path()
        self._drop_hint.setVisible(bool(path))
        if not path:
            # Stan pusty: w miejscu nazwy pliku — co można zrobić (pkt 6.9, makieta).
            self._file_label.setText(self.tr("Upuść plik GML albo wybierz…"))
            theme.set_text_role(self._file_label, "muted")
            self._file_dir.setText("")
            self._file_dir.setVisible(False)
            self._file_label.setToolTip("")
            return
        file_path = Path(path)
        theme.set_text_role(self._file_label, "")
        self._file_label.setText(file_path.name)
        self._file_dir.setText(self.tr("· {0}").format(_short_dir(file_path.parent)))
        self._file_dir.setVisible(True)
        self._file_label.setToolTip(path)
        self._file_dir.setToolTip(path)

    def _choose_file(self) -> None:
        """„Wybierz…” — plik GML APP do sprawdzenia."""
        start = str(Path(self.path()).parent) if self.path() else ""
        path, _ = QFileDialog.getOpenFileName(self, self.tr("Plik GML APP"), start,
                                              self.tr("GML APP (*.gml *.GML)"))
        if path:
            self.set_path(path)

    # --- akty otwarte w QGIS -------------------------------------------------

    def _choose_open_act(self) -> None:
        """„Z aktów w QGIS…” — wybór aktu albo pliku APP już wczytanego do QGIS."""
        provider = self._acts_provider
        acts = list(provider()) if provider is not None else []
        picker = OpenActsDialog(acts, self)
        if picker.exec() != QDialog.DialogCode.Accepted:
            return
        act = picker.selected_act()
        if act is None:
            return
        self.open_act(act)

    def open_act(self, act: OpenAct) -> bool:
        """
        Sprawdza akt wskazany spośród tych, które są już otwarte w QGIS.

        Plik GML z dysku walidujemy wprost; akt z Rejestru wymaga pobrania pliku
        GML — to robi karta „Pliki APP” (``registry_loader``).

        :param act: wybrany akt.
        :return: ``True``, gdy walidacja została rozpoczęta.
        """
        if act.kind == KIND_FILE and act.path:
            self.set_path(act.path)
            return self.start()
        if act.kind == KIND_REGISTRY:
            loader = self._registry_loader
            if loader is None:
                return False
            self.progress.setRange(0, 0)
            self.progress.setFormat(self.tr("Pobieranie pliku GML aktu {0}…").format(act.act_id))
            self.progress.setVisible(True)
            if not loader(act.act_id):
                self.progress.setRange(0, 100)
                self.progress.setFormat("")
                self.progress.setVisible(False)
                self._log(self.tr("Nie udało się pobrać pliku GML aktu {0}.").format(act.act_id), "BŁĄD")
            return True
        return False

    def _on_previous_toggled(self, checked: bool) -> None:
        """Rozwija zwinięty wiersz porównania wersji."""
        theme.set_icon(self._btn_previous, "chevron-down" if checked else "chevron-right", role="WindowText")
        self._previous_box.setVisible(checked)

    def _update_buttons(self, *args) -> None:
        busy = self._service.busy()
        has_file = bool(self.path()) and Path(self.path()).is_file()
        self.btn_validate.setEnabled(has_file and not busy)
        self.btn_validate.setText(self.tr("Waliduj ponownie") if self._report is not None
                                  else self.tr("Waliduj"))
        has_previous = bool(self.previous_path()) and Path(self.previous_path()).is_file()
        self.btn_compare.setEnabled(has_file and has_previous and not busy)
        self.btn_cancel.setEnabled(busy)
        self.btn_cancel.setVisible(busy)
        self.btn_add.setEnabled(has_file and not self._files.busy())
        report = self._report
        self.btn_save.setEnabled(report is not None and not busy)
        has_geometry = report is not None and any(f.wkt for f in report.findings)
        self.btn_errors.setEnabled(has_geometry and not busy)
        self.btn_errors.setToolTip("" if has_geometry else self.tr("Brak uwag z geometrią do pokazania"))

    def _log(self, text: str, level: str = "INFO") -> None:
        if self._message is not None:
            self._message(text, level)

    # --- upuszczanie pliku na okno ------------------------------------------------

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - nazwa z API Qt
        if self._dropped_path(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        event.accept()

    def dropEvent(self, event) -> None:  # noqa: N802
        path = self._dropped_path(event)
        if not path:
            event.ignore()
            return
        self.set_path(path)
        event.acceptProposedAction()
        self.start()

    @staticmethod
    def _dropped_path(event) -> str:
        """Ścieżka pliku GML upuszczonego na okno (``""`` gdy to nie plik APP)."""
        mime = event.mimeData()
        if not mime.hasUrls():
            return ""
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            path = url.toLocalFile()
            if Path(path).suffix.lower() == ".gml":
                return path
        return ""

    # --- walidacja ------------------------------------------------------------

    def start(self) -> bool:
        path = self.path()
        if not path or not Path(path).is_file():
            return False
        if not self._service.start(path, self.check_xsd.isChecked(), self.check_geometry.isChecked()):
            return False
        self._report = None
        self.tree.clear()
        self.summary_card.clear()
        self.progress.setValue(0)
        self.progress.setVisible(True)
        theme.set_html(self.summary, self.tr("Walidacja {0}…").format(Path(path).name))
        self._log(f"Walidacja APP: {path}")
        self._update_buttons()
        return True

    def compare(self) -> bool:
        """Porównuje plik z polem „Poprzednia wersja” (dwie wersje tego samego zbioru)."""
        path, previous = self.path(), self.previous_path()
        if not (path and previous) or not Path(path).is_file() or not Path(previous).is_file():
            return False
        self.tree.clear()
        self.progress.setVisible(False)
        theme.set_html(self.summary, self.tr("Porównanie {0} → {1}…").format(Path(previous).name, Path(path).name))
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            report = compare_files(previous, path)
        finally:
            QApplication.restoreOverrideCursor()
        self.show_report(report)
        s = report.summary
        self._log(f"Porównanie wersji {Path(previous).name} → {Path(path).name}: błędy {s[ERROR]}, "
                  f"ostrzeżenia {s[WARNING]}" + (f" — {report.fatal}" if report.fatal else ""),
                  "BŁĄD" if report.fatal or s[ERROR] else "INFO")
        return True

    def _on_progress(self, value: float, stage: str) -> None:
        self.progress.setValue(int(value))
        if stage:
            theme.set_html(self.summary, f"{stage}…")

    def show_report(self, report: ValidationReport) -> None:
        """Pokazuje raport: podsumowanie i wyniki pogrupowane według ważności."""
        self._report = report
        self.progress.setVisible(False)
        self._results_placeholder.setVisible(False)
        self.tree.setVisible(True)
        self.tree.clear()
        self.summary_card.set_report(report)
        # Tekst podsumowania — ta sama informacja zwykłym tekstem (karta niesie ją wizualnie).
        self.summary.setText(self._summary_text(report))
        self.summary.setVisible(False)
        self._fill_results(report)
        s = report.summary
        level = "BŁĄD" if report.fatal else ("UWAGA" if s[ERROR] or report.skipped else "INFO")
        self._log(f"Walidacja APP {Path(report.path).name}: błędy {s[ERROR]}, ostrzeżenia {s[WARNING]}"
                  + (f" — {report.fatal}" if report.fatal else ""), level)
        self._update_buttons()

    @staticmethod
    def _summary_text(report: ValidationReport) -> str:
        """Podsumowanie zwykłym tekstem (werdykt, wersja, liczniki, czas)."""
        name = Path(report.path).name
        if report.fatal:
            return f"{name}: {report.fatal}"
        s = report.summary
        verdict = "bez błędów" if s[ERROR] == 0 else f"błędy: {s[ERROR]}"
        head = "Porównanie wersji" if report.previous else name
        text = (f"{head} — APP {report.version or '?'} — {verdict}, "
                f"ostrzeżenia: {s[WARNING]}, informacje: {s[INFO]} ({report.seconds} s)")
        if report.previous:
            text += f" ({Path(report.previous).name} → {name})"
        return text

    def _fill_results(self, report: ValidationReport) -> None:
        """
        Wyniki pogrupowane według ważności (Błędy / Ostrzeżenia / Uwagi).

        Grupy są rozwinięte, komunikaty zawijane (bez „…”), a wiersze z geometrią
        mają przycisk „Pokaż na mapie”. Kontrole sprawdzone bez uwag trafiają do
        jednego zwiniętego wiersza „Bez uwag (N)” z listą nazw.
        """
        groups = defaultdict(list)
        for finding in report.findings:
            groups[finding.severity].append(finding)

        has_lines = any(finding.line for finding in report.findings)
        self.tree.setColumnHidden(COL_LINE, not has_lines)
        # Kolumna „Mapa” tylko wtedy, gdy któraś uwaga ma geometrię (puste kolumny ukrywamy, pkt 6.9).
        self.tree.setColumnHidden(COL_ACTION, not any(finding.wkt for finding in report.findings))

        for severity in _GROUPS:
            findings = groups.get(severity, [])
            if not findings:
                continue
            group = QTreeWidgetItem(self.tree)
            group.setText(COL_SEVERITY, self.tr("{0} ({1})").format(_GROUP_NAMES[severity], len(findings)))
            theme.set_item_role(group, COL_SEVERITY, _COLORS[severity])
            for same in group_identical(findings):
                if len(same) == 1:
                    self._finding_item(group, same[0])
                    continue
                # Ta sama uwaga dla wielu obiektów — jeden wiersz z liczbą obiektów, obiekty pod nim.
                first = same[0]
                item = QTreeWidgetItem(group)
                self._set_rule_cell(item, first)
                item.setText(COL_OBJECT, plural_objects(len(same)))
                ids = [f.object_id or f.kind or "" for f in same]
                shown = ids[:_TOOLTIP_IDS] + ([f"… (+{len(ids) - _TOOLTIP_IDS})"] if len(ids) > _TOOLTIP_IDS else [])
                item.setToolTip(COL_OBJECT, "\n".join(shown))
                item.setText(COL_MESSAGE, first.message)
                item.setToolTip(COL_MESSAGE, first.message)
                for finding in same:
                    self._finding_item(item, finding, message=False)
                item.setExpanded(False)
            group.setExpanded(True)

        # Kontrole sprawdzone bez uwag — jeden zwinięty wiersz z listą nazw.
        reported = {finding.rule for finding in report.findings}
        clean = [rule for rule in self._checked_rules(report) if rule not in reported]
        if clean:
            group = QTreeWidgetItem(self.tree)
            group.setText(COL_SEVERITY, self.tr("Bez uwag ({0})").format(len(clean)))
            theme.set_item_role(group, COL_SEVERITY, "ok")
            names = ", ".join(_RULE_SHORT.get(rule, rule) for rule in clean)
            group.setText(COL_MESSAGE, names)
            group.setToolTip(COL_MESSAGE, names)
            group.setExpanded(False)

    def _set_rule_cell(self, item: QTreeWidgetItem, finding) -> None:
        """Kolumna „Kontrola”: krótka nazwa reguły w kolorze poziomu, pełny opis w podpowiedzi."""
        item.setText(COL_SEVERITY, _capitalize(_RULE_SHORT.get(finding.rule, finding.rule)))
        item.setToolTip(COL_SEVERITY, self.tr("{0} · {1}").format(
            _capitalize(finding.severity_label), _RULE_TITLES.get(finding.rule, finding.rule)))
        theme.set_item_role(item, COL_SEVERITY, _COLORS.get(finding.severity, _COLORS[INFO]))

    def _finding_item(self, parent: QTreeWidgetItem, finding, message: bool = True) -> QTreeWidgetItem:
        """Wiersz jednej uwagi; ``message=False`` — wiersz obiektu pod uwagą zgrupowaną (treść w rodzicu)."""
        item = QTreeWidgetItem(parent)
        if message:
            self._set_rule_cell(item, finding)
            item.setText(COL_MESSAGE, finding.message)
        item.setToolTip(COL_MESSAGE, finding.message)
        item.setText(COL_OBJECT, finding.object_id or finding.kind or "")
        item.setToolTip(COL_OBJECT, finding.object_id or "")
        item.setText(COL_LINE, str(finding.line) if finding.line else "")
        item.setData(COL_SEVERITY, _FINDING_ROLE, finding)
        if finding.wkt:
            self.tree.setItemWidget(item, COL_ACTION, self._map_button(finding))
        return item

    def _map_button(self, finding) -> QToolButton:
        """Przycisk „Pokaż na mapie” przy wierszu z geometrią problemu."""
        button = QToolButton()
        button.setText(self.tr("Pokaż na mapie"))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        button.setAutoRaise(False)  # przycisk z obrysem, jak na makiecie
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        theme.button_font(button)
        button.setToolTip(self.tr("Przybliż mapę do miejsca tej uwagi"))
        button.clicked.connect(lambda: self.zoom_to(finding))
        return button

    @staticmethod
    def _checked_rules(report: ValidationReport) -> list:
        """
        Reguły faktycznie sprawdzone w tym raporcie.

        Etap „REGULY” obejmuje wszystkie reguły poza XSD, geometrią i wersjami
        (te sprawdzane są osobno).
        """
        rules = [r.rule_id for r in RULES if r.rule_id not in ("XSD", "GEOMETRIA", "WERSJE")]
        if "XSD" in report.checked:
            rules.append("XSD")
        if "GEOMETRIA" in report.checked:
            rules.append("GEOMETRIA")
        if "WERSJE" in report.checked:
            rules.append("WERSJE")
        return rules

    @staticmethod
    def _rule_state(report: ValidationReport, rule: str) -> str:
        """Stan reguły bez naruszeń: OK albo „nie sprawdzono” (etap pominięty lub przerwany)."""
        stage = rule if rule in ("XSD", "GEOMETRIA", "WERSJE") else "REGULY"
        return "OK" if stage in report.checked else "nie sprawdzono"

    # --- akcje ----------------------------------------------------------------

    def add_to_project(self) -> bool:
        path = self.path()
        if not path:
            return False
        self._log(f"Dodawanie pliku aktu do projektu: {path}")
        started = self._files.add_file(path)
        self._update_buttons()
        return started

    def save_report(self, path: Optional[str] = None) -> Optional[str]:
        if self._report is None:
            return None
        if not path:
            default = str(Path(self._report.path).with_suffix(".walidacja.csv"))
            path, _ = QFileDialog.getSaveFileName(self, self.tr("Zapisz raport walidacji"), default,
                                                  self.tr("CSV (*.csv)"))
        if not path:
            return None
        write_csv(self._report, path)
        self._log(f"Zapisano raport walidacji: {path}")
        return path

    def show_errors_layer(self) -> list:
        """Warstwy tymczasowe z miejscami problemów (powierzchnie i punkty) w grupie na górze projektu."""
        report = self._report
        if report is None:
            return []
        crs = report.crs or "EPSG:2180"
        polygons = QgsVectorLayer(f"MultiPolygon?crs={crs}&field=poziom:string&field=regula:string"
                                  "&field=obiekt:string&field=komunikat:string&field=m2:double",
                                  self.tr("Uwagi APP — powierzchnie"), "memory")
        points = QgsVectorLayer(f"Point?crs={crs}&field=poziom:string&field=regula:string"
                                "&field=obiekt:string&field=komunikat:string",
                                self.tr("Uwagi APP — punkty"), "memory")
        poly_features, point_features = [], []
        for finding in report.findings:
            if not finding.wkt:
                continue
            geometry = QgsGeometry.fromWkt(finding.wkt)
            if geometry.isNull():
                continue
            if geometry.type() == Qgis.GeometryType.Point:
                feature = QgsFeature(points.fields())
                feature.setGeometry(geometry)
                feature.setAttributes([finding.severity_label, finding.rule, finding.object_id, finding.message])
                point_features.append(feature)
                continue
            area = geometry.convertToType(Qgis.GeometryType.Polygon, True)
            if area.isNull():
                continue
            feature = QgsFeature(polygons.fields())
            feature.setGeometry(area)
            feature.setAttributes([finding.severity_label, finding.rule, finding.object_id, finding.message,
                                   finding.measure])
            poly_features.append(feature)
        layers = []
        for layer, features in ((points, point_features), (polygons, poly_features)):
            if features:
                layer.dataProvider().addFeatures(features)
                layer.updateExtents()
                style_error_layer(layer)
                layers.append(layer)
        if not layers:
            return []
        project = QgsProject.instance()
        group = project.layerTreeRoot().insertGroup(0, self.tr("Walidacja APP — {0}").format(Path(report.path).name))
        for layer in layers:
            project.addMapLayer(layer, False)
            group.addLayer(layer)
        self._error_layers = layers
        return layers

    # --- nawigacja --------------------------------------------------------------

    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        finding = item.data(COL_SEVERITY, _FINDING_ROLE)
        if finding is not None:
            self.zoom_to(finding)

    def zoom_to(self, finding) -> bool:
        """Przybliża mapę do miejsca problemu (WKT) albo obiektu z warstwy dodanej z tego pliku."""
        canvas = self._canvas() if callable(self._canvas) else self._canvas
        if canvas is None or self._report is None:
            return False
        extent, crs = None, None
        if finding.wkt:
            geometry = QgsGeometry.fromWkt(finding.wkt)
            extent, crs = geometry.boundingBox(), QgsCoordinateReferenceSystem(self._report.crs)
        elif finding.object_id:
            for layer in QgsProject.instance().mapLayers().values():
                if (layer.customProperty(SOURCE_PROPERTY) != self._report.path
                        or layer.fields().indexOf("gml_id") < 0 or not layer.isSpatial()):
                    continue
                request = QgsFeatureRequest().setFilterExpression(f"\"gml_id\" = '{finding.object_id}'")
                ids = [f.id() for f in layer.getFeatures(request)]
                if ids:
                    layer.selectByIds(ids)
                    extent, crs = layer.boundingBoxOfSelected(), layer.crs()
                    break
        if extent is None or extent.isNull():
            return False
        destination = canvas.mapSettings().destinationCrs()
        if crs is not None and crs.isValid() and crs != destination:
            extent = QgsCoordinateTransform(crs, destination, QgsProject.instance()).transformBoundingBox(extent)
        margin = max(extent.width(), extent.height(), 20.0) * 0.5
        canvas.setExtent(QgsRectangle(extent.xMinimum() - margin, extent.yMinimum() - margin,
                                      extent.xMaximum() + margin, extent.yMaximum() + margin))
        canvas.refresh()
        return True

    def closeEvent(self, event) -> None:  # noqa: N802 - nazwa z API Qt
        self._service.cancel()
        super().closeEvent(event)
