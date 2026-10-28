"""
Okno „Zbiór z katalogu”: analiza identyfikatorów i dokumentów aktów przed publikacją.

Akty w wybranym katalogu nie są jeszcze w Rejestrze Urbanistycznym — ich identyfikatory
można zmieniać. Po publikacji identyfikator staje się odwołaniem zewnętrznym, więc okno
oznacza akty już opublikowane i nie pozwala ich „poprawiać” (zmiana wymaga nowej wersji).

Układ wg ``docs/ui/UI_GUIDELINES.md``: pole katalogu z akcją, postęp, jedno zdanie
podsumowania, drzewo aktów i ustaleń, akcje na dole.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush
from qgis.PyQt.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)
from qgis.gui import QgsFileWidget

from ..core.app_rules import ERROR, INFO, WARNING
from ..core.app_set import (SetAct, SetReport, acts_of_unit, published_key, summary_text,
                            unit_folder, write_csv)
from ..services.app_set import AppSetService
from ..services.app_set_download import AppSetDownloadService, SetDownload
from . import theme

logger = logging.getLogger("GSMapa.AppSetDialog")

#: Kolumny drzewa: obiekt/akt, szczegóły, stan.
COL_NAME, COL_DETAILS, COL_STATE = range(3)
#: Kolory poziomów (``theme``).
_COLORS = {ERROR: "err", WARNING: "warn", INFO: "muted"}
_GROUP_NAMES = {ERROR: "Błędy", WARNING: "Ostrzeżenia", INFO: "Uwagi"}


class AppSetDialog(QDialog):
    """Analiza katalogu z aktami APP oczekującymi na publikację."""

    #: Komunikat do logu wtyczki: (treść, poziom).
    message = pyqtSignal(str, str)

    def __init__(self, parent=None, service: Optional[AppSetService] = None,
                 published: Optional[Dict[Tuple[str, str, str], str]] = None,
                 add_file: Optional[Callable[[str], bool]] = None,
                 download_service: Optional[AppSetDownloadService] = None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Zbiór z katalogu"))
        self.resize(760, 620)
        self._service = service or AppSetService(self)
        self._download = download_service or AppSetDownloadService(self)
        self._published = dict(published or {})
        self._add_file = add_file
        self._report: Optional[SetReport] = None
        #: Jednostka, indeks aktów i szablon ich opisów — do pobrania zbioru z Rejestru.
        self._unit = None
        self._entries: list = []
        self._acts_template = ""
        self._base_dir = ""
        self._download_acts: list = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        note = QLabel(self.tr("Akty spoza Rejestru: identyfikatory można jeszcze zmieniać. "
                              "Akty pobrane z Rejestru są tylko do odczytu — zmiana wymaga nowej wersji."))
        note.setWordWrap(True)
        theme.style_muted(note, small=True)
        layout.addWidget(note)

        source = QHBoxLayout()
        source.setSpacing(6)
        self.unit_info = QLabel("")
        self.unit_info.setWordWrap(True)
        theme.style_muted(self.unit_info, small=True)
        source.addWidget(self.unit_info, 1)
        self.btn_download = QPushButton(self.tr("Pobierz z Rejestru…"))
        self.btn_download.setToolTip(self.tr("Pobierz wszystkie akty jednostki z indeksu Rejestru "
                                             "do katalogu roboczego i przeanalizuj je jako zbiór"))
        theme.set_icon(self.btn_download, "refresh", "/mActionRefresh.svg")
        self.btn_download.setEnabled(False)
        self.btn_download.clicked.connect(self.download_unit)
        source.addWidget(self.btn_download)
        layout.addLayout(source)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.folder = QgsFileWidget()
        self.folder.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        self.folder.setToolTip(self.tr("Katalog z plikami .gml aktów oczekujących na publikację"))
        row.addWidget(self.folder, 1)
        self.btn_scan = QPushButton(self.tr("Analizuj"))
        self.btn_scan.setToolTip(self.tr("Przeczytaj wszystkie pliki .gml w katalogu i sprawdź "
                                         "identyfikatory, dokumenty i stan publikacji"))
        theme.set_icon(self.btn_scan, "validate", "/mActionCheckGeometry.svg")
        self.btn_scan.clicked.connect(self.start)
        row.addWidget(self.btn_scan)
        layout.addLayout(row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        theme.style_muted(self.summary, small=True)
        layout.addWidget(self.summary)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels([self.tr("Akt / obiekt"), self.tr("Szczegóły"), self.tr("Stan")])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.itemSelectionChanged.connect(self._update_actions)
        header = self.tree.header()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setColumnWidth(0, 190)
        theme.align_headers(self.tree)
        layout.addWidget(self.tree, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.btn_add = QPushButton(self.tr("Dodaj do mapy"))
        self.btn_add.setToolTip(self.tr("Dodaj warstwy zaznaczonego aktu do mapy"))
        theme.set_icon(self.btn_add, "layers", "/mActionAddLayer.svg")
        self.btn_add.setEnabled(False)
        self.btn_add.clicked.connect(self._add_selected)
        buttons.addWidget(self.btn_add)
        self.btn_csv = QPushButton(self.tr("Zapisz raport CSV…"))
        self.btn_csv.setToolTip(self.tr("Zapisz akty i ustalenia do pliku CSV"))
        self.btn_csv.setEnabled(False)
        self.btn_csv.clicked.connect(self._save_csv)
        buttons.addWidget(self.btn_csv)
        buttons.addStretch(1)
        self.btn_close = QPushButton(self.tr("Zamknij"))
        self.btn_close.clicked.connect(self.close)
        buttons.addWidget(self.btn_close)
        layout.addLayout(buttons)

        self._service.finished.connect(self._on_finished)
        self._service.failed.connect(self._on_failed)
        self._service.progress.connect(self._on_progress)
        self._download.finished.connect(self._on_downloaded)
        self._download.failed.connect(self._on_download_failed)
        self._download.progress.connect(self._on_progress)

    # --- jednostka i pobranie zbioru z Rejestru ---------------------------------------

    def set_unit(self, unit, entries, acts_template: str = "", base_dir: str = "") -> None:
        """
        Jednostka, z której bierze się zbiór z Rejestru (pasek kontekstu + indeks aktów).

        :param unit: jednostka (:class:`core.units.Unit`) albo ``None``.
        :param entries: wpisy indeksu aktów Rejestru.
        :param acts_template: szablon adresu opisów aktów (``akty/{act_id}.json``).
        :param base_dir: katalog pobranych danych (ustawienie ``download_dir``).
        """
        self._unit = unit
        self._entries = list(entries or [])
        self._acts_template = str(acts_template or "")
        self._base_dir = str(base_dir or "")
        acts = acts_of_unit(self._entries, getattr(unit, "jpt", "")) if unit is not None else []
        if unit is None:
            self.unit_info.setText(self.tr("Z Rejestru: wskaż jednostkę w pasku kontekstu."))
        elif not acts:
            self.unit_info.setText(self.tr("Z Rejestru: {0} ({1}) — brak aktów w indeksie.").format(
                unit.name, unit.kind_label))
        else:
            self.unit_info.setText(self.tr("Z Rejestru: {0} ({1}) — aktów w indeksie: {2}").format(
                unit.name, unit.kind_label, len(acts)))
        self.btn_download.setEnabled(bool(acts) and bool(self._acts_template) and not self._download.busy())
        self._update_actions()

    def download_unit(self) -> bool:
        """Pobiera wszystkie akty jednostki do katalogu roboczego i analizuje je jako zbiór."""
        if self._unit is None:
            return False
        acts = acts_of_unit(self._entries, self._unit.jpt)
        if not acts:
            self.summary.setText(self.tr("Indeks Rejestru nie ma aktów tej jednostki."))
            return False
        folder = unit_folder(self._base_dir or str(Path.home()), self._unit.jpt, self._unit.name)
        if not self._download.start(acts, str(folder), self._acts_template):
            return False
        self._download_acts = acts
        self.summary.setText(self.tr("Pobieranie {0} aktów: {1}…").format(len(acts), self._unit.name))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.btn_download.setEnabled(False)
        self.message.emit(f"Pobieranie zbioru {self._unit.name}: {len(acts)} aktów do {folder}", "INFO")
        return True

    def _on_downloaded(self, result: SetDownload) -> None:
        self.progress.setVisible(False)
        self.btn_download.setEnabled(True)
        self.set_folder(result.folder)
        # Adresy plików z Rejestru niosą tożsamość aktu — po pobraniu wiadomo, co jest
        # już opublikowane (starszy indeks nie ma pola ``dane``).
        published = dict(self._published)
        for act_id, url in result.urls.items():
            key = published_key(url)
            if key is not None:
                published[key] = act_id
        if len(published) != len(self._published):
            self.set_published(published)
        note = f"pobrano {result.files} plików ({result.bytes / 1e6:.1f} MB)"
        if result.skipped:
            note += f", w tym już obecnych: {result.skipped}"
        if result.errors:
            note += f", błędów: {len(result.errors)}"
        self.message.emit(f"Zbiór z Rejestru {result.folder}: {note}", "INFO")
        self.summary.setText(self.tr("Pobrano: {0}. Analiza…").format(note))
        self.start(result.folder)

    def _on_download_failed(self, message: str) -> None:
        self.progress.setVisible(False)
        self.btn_download.setEnabled(self._unit is not None)
        self.summary.setText(self.tr("Nie udało się pobrać zbioru: {0}").format(message))
        self.message.emit(f"Pobieranie zbioru z Rejestru nie powiodło się: {message}", "BŁĄD")

    # --- ustawienia ------------------------------------------------------------------

    def set_published(self, published: Dict[Tuple[str, str, str], str]) -> None:
        """Klucze aktów opublikowanych w Rejestrze (``core.app_set.published_keys``)."""
        self._published = dict(published or {})

    def set_folder(self, folder: str) -> None:
        self.folder.setFilePath(str(folder))

    def folder_path(self) -> str:
        return self.folder.filePath()

    # --- analiza ---------------------------------------------------------------------

    def start(self, folder: Optional[str] = None) -> bool:
        """Uruchamia analizę katalogu (z pola okna albo z argumentu)."""
        path = str(folder or self.folder_path() or "").strip()
        if not path:
            self.summary.setText(self.tr("Wskaż katalog z aktami."))
            return False
        self.set_folder(path)
        if not self._service.start(path, self._published):
            self.summary.setText(self.tr("Poprzednia analiza jeszcze trwa."))
            return False
        self.summary.setText(self.tr("Analiza {0}…").format(Path(path).name or path))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.btn_scan.setEnabled(False)
        self.message.emit(f"Analiza zbioru APP: {path}", "INFO")
        return True

    def _on_progress(self, value: float, text: str) -> None:
        self.progress.setValue(int(value))
        if text:
            self.summary.setText(self.tr("Analiza {0}…").format(Path(text).name))

    def _on_failed(self, message: str) -> None:
        self.progress.setVisible(False)
        self.btn_scan.setEnabled(True)
        self.summary.setText(self.tr("Nie udało się przeanalizować katalogu: {0}").format(message))
        self.message.emit(f"Analiza zbioru APP nie powiodła się: {message}", "BŁĄD")

    def _on_finished(self, report: SetReport) -> None:
        self._report = report
        self.progress.setVisible(False)
        self.btn_scan.setEnabled(True)
        self.btn_csv.setEnabled(True)
        self.summary.setText(summary_text(report))
        self._fill_tree(report)
        self.message.emit(f"Zbiór APP {report.folder}: {summary_text(report)}", "INFO")

    # --- drzewo ----------------------------------------------------------------------

    def _fill_tree(self, report: SetReport) -> None:
        self.tree.clear()
        acts_node = QTreeWidgetItem([self.tr("Akty w katalogu ({0})").format(len(report.acts)), "", ""])
        acts_node.setFirstColumnSpanned(True)
        self.tree.addTopLevelItem(acts_node)
        for act in report.acts:
            item = QTreeWidgetItem(acts_node, [act.name, self._act_details(act), self._act_state(act)])
            item.setData(0, Qt.ItemDataRole.UserRole, act.path)
            item.setToolTip(0, act.path)
        acts_node.setExpanded(False)

        expanded = False
        for severity in (ERROR, WARNING, INFO):
            findings = [finding for finding in report.findings if finding.severity == severity]
            if not findings:
                continue
            node = QTreeWidgetItem([self.tr("{0} ({1})").format(_GROUP_NAMES[severity], len(findings)), "", ""])
            node.setFirstColumnSpanned(True)
            self.tree.addTopLevelItem(node)
            for finding in findings:
                name = finding.object_id or (Path(finding.file).name if finding.file else "—")
                child = QTreeWidgetItem(node, [name, finding.message, ""])
                child.setToolTip(1, finding.message)
                if finding.file:
                    child.setData(0, Qt.ItemDataRole.UserRole, finding.file)
                role = _COLORS.get(severity)
                if role:
                    child.setForeground(0, QBrush(theme.color_of(role, self.tree)))
            # Pierwsza grupa z ustaleniami jest rozwinięta — od razu widać, co jest nie tak.
            node.setExpanded(not expanded)
            expanded = True
        self._update_actions()

    @staticmethod
    def _act_details(act: SetAct) -> str:
        parts = [act.namespace or "(bez przestrzeni nazw)"]
        if act.local_id:
            parts.append(act.local_id)
        if act.version_id:
            parts.append(f"wersja {act.version_id}")
        parts.append(f"obiektów: {act.objects}")
        return " · ".join(parts)

    @staticmethod
    def _act_state(act: SetAct) -> str:
        if act.error:
            return "nieczytelny"
        if act.published:
            return "opublikowany"
        if act.published_version:
            return f"nowa wersja ({act.published_version})"
        return "oczekuje na publikację"

    def _selected_path(self) -> str:
        item = self.tree.currentItem()
        if item is None:
            return ""
        return str(item.data(0, Qt.ItemDataRole.UserRole) or "")

    def _update_actions(self) -> None:
        path = self._selected_path()
        self.btn_add.setEnabled(bool(path) and Path(path).is_file() and self._add_file is not None)

    def _add_selected(self) -> None:
        path = self._selected_path()
        if path and self._add_file is not None:
            self._add_file(path)

    # --- raport ----------------------------------------------------------------------

    def _save_csv(self) -> None:
        if self._report is None:
            return
        start = self._report.folder or str(Path.home())
        path, _ = QFileDialog.getSaveFileName(
            self, self.tr("Raport zbioru APP"), str(Path(start, "zbior-app.csv")),
            self.tr("CSV (*.csv)"))
        if not path:
            return
        try:
            write_csv(self._report, path)
        except OSError as error:
            self.summary.setText(self.tr("Nie udało się zapisać raportu: {0}").format(error))
            return
        self.message.emit(f"Raport zbioru APP: {path}", "INFO")
        self.summary.setText(self.tr("Raport zapisany: {0}").format(path))
