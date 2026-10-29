"""
Okno „Zbiór z katalogu”: analiza identyfikatorów i dokumentów aktów przed publikacją.

Akty w wybranym katalogu, których nie ma w Rejestrze Urbanistycznym, mogą jeszcze zmienić
identyfikatory — okno pozwala to zrobić hurtowo („Zmień identyfikatory…”). Po publikacji
identyfikator staje się odwołaniem zewnętrznym, więc akty z Rejestru są tylko do odczytu
(zmiana wymaga nowej wersji).

Kolumna „Wersja APP” mówi, w którym schemacie zapisany jest plik: pliki z lustra Rejestru to
przeważnie APP 1.0 i 2.0, a do publikacji potrzebne jest 3.0 — akty starszych wersji można
przepisać hurtowo („Konwertuj do APP 3.0…”), z kopiami ``.bak`` i kontrolą XSD.

Układ wg ``docs/ui/UI_GUIDELINES.md``: źródło (katalog / Rejestr) → postęp → podsumowanie →
drzewo aktów (pogrupowane po zbiorach) i ustaleń → szczegóły zaznaczenia → akcje.
"""

from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QGuiApplication
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)
from qgis.gui import QgsFileWidget

from ..core.app_convert import TARGET_VERSION
from ..core.app_namespace import parse_namespace
from ..core.app_rules import ERROR, INFO, WARNING
from ..core.app_set import (SetAct, SetReport, acts_of_unit, published_key, summary_text,
                            unit_folder, version_counts, write_csv)
from ..core.app_set_convert import needs_conversion
from ..core.app_set_ids import read_files
from ..services.app_set import AppSetService
from ..services.app_set_download import AppSetDownloadService, SetDownload
from . import theme

logger = logging.getLogger("GSMapa.AppSetDialog")

#: Kolumny drzewa: plik, tytuł, lokalnyId, wersjaId, stan, wersja APP.
COL_NAME, COL_TITLE, COL_LOCAL, COL_VERSION, COL_STATE, COL_APP = range(6)
#: Rola danych wiersza: ścieżka pliku (akt) albo pliku ustalenia.
PATH_ROLE = Qt.ItemDataRole.UserRole
#: Rola danych wiersza: rodzaj wiersza (``act``, ``set``, ``finding``, ``group``).
KIND_ROLE = Qt.ItemDataRole.UserRole + 1
#: Kolory poziomów (``theme``).
_COLORS = {ERROR: "err", WARNING: "warn", INFO: "muted"}
_GROUP_NAMES = {ERROR: "Błędy", WARNING: "Ostrzeżenia", INFO: "Uwagi"}
#: Powyżej tylu aktów „Dodaj wszystkie do mapy” pyta o potwierdzenie (każdy akt to 2–4 warstwy).
CONFIRM_ADD_ABOVE = 20

#: Stan aktu: (tekst, rola koloru, podpowiedź).
STATE_UNREADABLE = ("nieczytelny", "err", "Pliku nie da się odczytać jako GML APP")
STATE_PUBLISHED = ("w Rejestrze", "info", "Akt jest opublikowany w Rejestrze — identyfikatory tylko do "
                                          "odczytu, zmiana wymaga nowej wersji")
STATE_LOCAL = ("spoza Rejestru", "ok", "Aktu nie ma w Rejestrze — identyfikatory można zmieniać")
STATE_UNCHECKED = ("nie sprawdzono", "muted", "Nie udało się sprawdzić aktu w Rejestrze (brak indeksu aktów "
                                              "albo połączenia) — odśwież indeks i przeanalizuj ponownie")


class AppSetDialog(QDialog):
    """Analiza katalogu z aktami APP: akty, zbiory, ustalenia, dodanie do mapy i zmiana identyfikatorów."""

    #: Komunikat do logu wtyczki: (treść, poziom).
    message = pyqtSignal(str, str)

    def __init__(self, parent=None, service: Optional[AppSetService] = None,
                 published: Optional[Dict[Tuple[str, str, str], str]] = None,
                 add_file: Optional[Callable[..., bool]] = None,
                 download_service: Optional[AppSetDownloadService] = None,
                 ids_service=None, convert_service=None):
        super().__init__(parent)
        theme.apply_plugin_style(self)
        self.setWindowTitle(self.tr("Zbiór z katalogu"))
        self.setObjectName("GSMapaAppSetDialog")
        self.resize(1000, 640)
        self._service = service or AppSetService(self)
        self._download = download_service or AppSetDownloadService(self)
        self._ids_service = ids_service
        self._convert_service = convert_service
        self._published = dict(published or {})
        self._add_file = add_file
        self._report: Optional[SetReport] = None
        #: Jednostka, indeks aktów i szablon ich opisów — do pobrania zbioru z Rejestru.
        self._unit = None
        self._entries: list = []
        self._acts_template = ""
        self._base_dir = ""
        self._download_acts: list = []
        self._ids_dialog = None
        self._convert_dialog = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # --- źródło: Rejestr albo własny katalog ---
        source = QHBoxLayout()
        source.setSpacing(6)
        self.unit_info = QLabel("")
        self.unit_info.setWordWrap(True)
        theme.style_muted(self.unit_info, small=True)
        source.addWidget(self.unit_info, 1)
        self.btn_download = QPushButton(self.tr("Pobierz z Rejestru…"))
        self.btn_download.setToolTip(self.tr("Pobierz wszystkie akty jednostki z indeksu Rejestru "
                                             "do katalogu roboczego i przeanalizuj je jako zbiór"))
        theme.set_icon(self.btn_download, "download", "/mActionRefresh.svg")
        self.btn_download.setEnabled(False)
        self.btn_download.clicked.connect(self.download_unit)
        source.addWidget(self.btn_download)
        layout.addLayout(source)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.folder = QgsFileWidget()
        self.folder.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        self.folder.setToolTip(self.tr("Katalog z plikami .gml aktów (także podkatalogi)"))
        self.folder.setAccessibleName(self.tr("Katalog z aktami"))
        row.addWidget(self.folder, 1)
        self.btn_scan = QPushButton(self.tr("Analizuj"))
        self.btn_scan.setToolTip(self.tr("Przeczytaj wszystkie pliki .gml w katalogu i sprawdź "
                                         "identyfikatory, dokumenty i stan publikacji"))
        theme.set_icon(self.btn_scan, "validate", "/mActionCheckGeometry.svg", role="HighlightedText")
        theme.primary(self.btn_scan)
        self.btn_scan.clicked.connect(lambda: self.start())
        row.addWidget(self.btn_scan)
        layout.addLayout(row)

        progress_row = QHBoxLayout()
        progress_row.setSpacing(6)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        progress_row.addWidget(self.progress, 1)
        self.btn_stop = QPushButton(self.tr("Przerwij"))
        self.btn_stop.setVisible(False)
        self.btn_stop.clicked.connect(self.cancel)
        progress_row.addWidget(self.btn_stop)
        layout.addLayout(progress_row)

        self.summary = QLabel(self.tr("Wskaż katalog z plikami GML albo pobierz zbiór jednostki z Rejestru."))
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.summary)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels([self.tr("Akt"), self.tr("Tytuł"), self.tr("lokalnyId"),
                                   self.tr("wersjaId"), self.tr("Stan"), self.tr("Wersja APP")])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemSelectionChanged.connect(self._update_actions)
        self.tree.itemDoubleClicked.connect(self._on_double_clicked)
        header = self.tree.header()
        header.setSectionResizeMode(COL_TITLE, QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(False)
        self.tree.setColumnWidth(COL_NAME, 200)
        self.tree.setColumnWidth(COL_LOCAL, 170)
        self.tree.setColumnWidth(COL_VERSION, 130)
        self.tree.setColumnWidth(COL_STATE, 110)
        self.tree.setColumnWidth(COL_APP, 100)
        theme.align_headers(self.tree)
        layout.addWidget(self.tree, 1)

        self.details = QLabel("")
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details.setProperty("gsmapa_card", True)
        self.details.setContentsMargins(8, 6, 8, 6)
        self.details.setVisible(False)
        layout.addWidget(self.details)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.btn_add = QPushButton(self.tr("Dodaj do mapy"))
        theme.set_icon(self.btn_add, "layers", "/mActionAddLayer.svg")
        self.btn_add.setEnabled(False)
        self.btn_add.clicked.connect(self._add_selected)
        buttons.addWidget(self.btn_add)
        self.btn_add_all = QPushButton(self.tr("Dodaj wszystkie do mapy"))
        self.btn_add_all.setEnabled(False)
        self.btn_add_all.clicked.connect(self.add_all)
        buttons.addWidget(self.btn_add_all)
        self.btn_ids = QPushButton(self.tr("Zmień identyfikatory…"))
        theme.set_icon(self.btn_ids, "wand")
        self.btn_ids.setEnabled(False)
        self.btn_ids.clicked.connect(self.open_ids)
        buttons.addWidget(self.btn_ids)
        self.btn_convert = QPushButton(self.tr("Konwertuj do APP 3.0…"))
        theme.set_icon(self.btn_convert, "convert")
        self.btn_convert.setEnabled(False)
        self.btn_convert.clicked.connect(self.open_convert)
        buttons.addWidget(self.btn_convert)
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
        self._download.progress.connect(self._on_download_progress)
        self._update_actions()

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
            self.unit_info.setText(self.tr("Rejestr: wskaż jednostkę w pasku kontekstu, żeby pobrać jej akty."))
        elif not acts:
            self.unit_info.setText(self.tr("Rejestr: {0} ({1}) — brak aktów w indeksie.").format(
                unit.name, unit.kind_label))
        else:
            self.unit_info.setText(self.tr("Rejestr: {0} ({1}) — aktów w indeksie: {2}").format(
                unit.name, unit.kind_label, len(acts)))
        can = bool(acts) and bool(self._acts_template) and not self._download.busy()
        self.btn_download.setEnabled(can)
        if unit is None:
            self.btn_download.setToolTip(self.tr("Wybierz jednostkę w pasku kontekstu"))
        elif not acts:
            self.btn_download.setToolTip(self.tr("Indeks Rejestru nie ma aktów tej jednostki"))
        elif not self._acts_template:
            self.btn_download.setToolTip(self.tr("Indeks Rejestru nie podaje adresów aktów — odśwież indeks"))
        else:
            self.btn_download.setToolTip(self.tr("Pobierz {0} aktów jednostki {1} do katalogu roboczego "
                                                 "i przeanalizuj je jako zbiór").format(len(acts), unit.name))
        self._update_actions()

    def download_unit(self) -> bool:
        """Pobiera wszystkie akty jednostki do katalogu roboczego i analizuje je jako zbiór."""
        if self._unit is None:
            return False
        acts = acts_of_unit(self._entries, self._unit.jpt)
        if not acts:
            self._set_summary(self.tr("Indeks Rejestru nie ma aktów tej jednostki."), "warn")
            return False
        folder = unit_folder(self._base_dir or str(Path.home()), self._unit.jpt, self._unit.name)
        if not self._download.start(acts, str(folder), self._acts_template):
            return False
        self._download_acts = acts
        self._set_summary(self.tr("Pobieranie {0} aktów: {1}…").format(len(acts), self._unit.name), "muted")
        self._set_busy(True)
        self.message.emit(f"Pobieranie zbioru {self._unit.name}: {len(acts)} aktów do {folder}", "INFO")
        return True

    def _on_download_progress(self, value: float, text: str) -> None:
        self.progress.setValue(int(value))

    def _on_downloaded(self, result: SetDownload) -> None:
        self._set_busy(False)
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
        self._set_summary(self.tr("Pobrano: {0}. Analiza…").format(note), "muted")
        self.start(result.folder)

    def _on_download_failed(self, message: str) -> None:
        self._set_busy(False)
        self._set_summary(self.tr("Nie udało się pobrać zbioru: {0}. Sprawdź połączenie i spróbuj "
                                  "ponownie.").format(message), "err")
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
            self._set_summary(self.tr("Wskaż katalog z aktami."), "warn")
            return False
        self.set_folder(path)
        if not self._service.start(path, self._published, entries=self._entries,
                                   template=self._acts_template):
            self._set_summary(self.tr("Poprzednia analiza jeszcze trwa."), "warn")
            return False
        self._set_summary(self.tr("Analiza {0}…").format(Path(path).name or path), "muted")
        self._set_busy(True)
        self.message.emit(f"Analiza zbioru APP: {path}", "INFO")
        return True

    def cancel(self) -> None:
        """Przerywa analizę albo pobieranie."""
        if self._service.busy():
            self._service.cancel()
        if self._download.busy():
            self._download.cancel()

    def _set_busy(self, busy: bool) -> None:
        self.progress.setValue(0)
        self.progress.setVisible(busy)
        self.btn_stop.setVisible(busy)
        self.btn_scan.setEnabled(not busy)
        self.btn_download.setEnabled(not busy and self._unit is not None and bool(self._acts_template)
                                     and bool(acts_of_unit(self._entries, getattr(self._unit, "jpt", ""))))
        self._update_actions()

    def _on_progress(self, value: float, text: str) -> None:
        self.progress.setValue(int(value))
        if text:
            self._set_summary(self.tr("Analiza {0}…").format(Path(text).name), "muted")

    def _on_failed(self, message: str) -> None:
        self._set_busy(False)
        self._set_summary(self.tr("Nie udało się przeanalizować katalogu: {0}. Sprawdź, czy katalog "
                                  "istnieje i masz do niego dostęp.").format(message), "err")
        self.message.emit(f"Analiza zbioru APP nie powiodła się: {message}", "BŁĄD")

    def _on_finished(self, report: SetReport) -> None:
        self._report = report
        if report.resolved:
            # Klucze z opisów aktów Rejestru — kolejna analiza nie pyta sieci drugi raz.
            self._published.update(report.resolved)
        self._set_busy(False)
        self.btn_csv.setEnabled(True)
        self._show_summary(report)
        self._fill_tree(report)
        self.message.emit(f"Zbiór APP {report.folder}: {summary_text(report)}", "INFO")

    # --- podsumowanie ----------------------------------------------------------------

    def _set_summary(self, text: str, role: str = "") -> None:
        escaped = html.escape(text)
        theme.set_html(self.summary, theme.span(escaped, role) if role else escaped)

    def _show_summary(self, report: SetReport) -> None:
        counts = report.counts()
        local = [act for act in report.acts if act.editable]
        if not report.acts:
            self._set_summary(self.tr("W katalogu nie ma plików .gml."), "warn")
            return
        if counts[ERROR]:
            status = theme.span(html.escape(self.tr("✕ Błędy: {0}").format(counts[ERROR])), "err", bold=True)
        elif counts[WARNING]:
            status = theme.span(html.escape(self.tr("⚠ Ostrzeżenia: {0}").format(counts[WARNING])), "warn", bold=True)
        else:
            status = theme.span(html.escape(self.tr("✓ Bez błędów")), "ok", bold=True)
        parts = [self.tr("aktów: {0}").format(counts["acts"]), self.tr("zbiorów: {0}").format(counts["namespaces"]),
                 self.tr("w Rejestrze: {0}").format(counts["published"]),
                 self.tr("spoza Rejestru: {0}").format(len(local))]
        versions = version_counts(report.acts)
        if versions:
            listed = ", ".join(f"{version or '?'} ×{count}"
                               for version, count in sorted(versions.items(), reverse=True))
            parts.append(self.tr("wersje APP: {0}").format(listed))
        if counts["unchecked"]:
            parts.append(self.tr("nie sprawdzono: {0}").format(counts["unchecked"]))
        if counts["unreadable"]:
            parts.append(self.tr("nieczytelnych: {0}").format(counts["unreadable"]))
        text = f"{status} &nbsp;·&nbsp; " + html.escape(" · ".join(parts))
        hints = []
        if local:
            hints.append(self.tr("Akty spoza Rejestru możesz poprawić hurtowo: Zmień identyfikatory."))
        older = [act for act in report.acts if needs_conversion(act)]
        if older:
            hints.append(self.tr("Akty starszych wersji APP przepiszesz na 3.0 hurtowo: "
                                 "Konwertuj do APP 3.0."))
        for hint in hints:
            text += "<br>" + theme.span(html.escape(hint), "muted")
        theme.set_html(self.summary, text)

    # --- drzewo ----------------------------------------------------------------------

    def _fill_tree(self, report: SetReport) -> None:
        self.tree.clear()
        by_namespace: Dict[str, List[SetAct]] = {}
        for act in report.acts:
            by_namespace.setdefault(act.namespace, []).append(act)
        for namespace in sorted(by_namespace, key=lambda value: (value == "", value)):
            acts = by_namespace[namespace]
            parts = parse_namespace(namespace)
            label = (f"{namespace} · {parts[2]}" if parts else namespace) if namespace else \
                self.tr("Pliki bez przestrzeni nazw")
            node = QTreeWidgetItem([self.tr("{0} — aktów: {1}").format(label, len(acts)), "", "", "", "", ""])
            node.setData(0, KIND_ROLE, "set")
            node.setFont(0, theme.bold_font(self.tree))
            node.setToolTip(0, namespace)
            self.tree.addTopLevelItem(node)
            # Rozciągnięcie na wszystkie kolumny działa dopiero, gdy element jest w drzewie.
            node.setFirstColumnSpanned(True)
            for act in acts:
                item = QTreeWidgetItem(node, [act.name, act.title or act.error, act.local_id,
                                              act.version_id, "", ""])
                item.setData(0, PATH_ROLE, act.path)
                item.setData(0, KIND_ROLE, "act")
                item.setToolTip(COL_NAME, act.path)
                item.setToolTip(COL_TITLE, act.title or act.error)
                text, role, tip = self._act_state(act)
                item.setText(COL_STATE, text)
                item.setToolTip(COL_STATE, tip)
                theme.set_item_role(item, COL_STATE, role)
                text, role, tip = self._app_version(act)
                item.setText(COL_APP, text)
                item.setToolTip(COL_APP, tip)
                theme.set_item_role(item, COL_APP, role)
            node.setExpanded(True)

        for severity in (ERROR, WARNING, INFO):
            findings = [finding for finding in report.findings if finding.severity == severity]
            if not findings:
                continue
            node = QTreeWidgetItem([self.tr("{0} ({1})").format(_GROUP_NAMES[severity], len(findings)),
                                    "", "", "", "", ""])
            node.setData(0, KIND_ROLE, "group")
            node.setFont(0, theme.bold_font(self.tree))
            self.tree.addTopLevelItem(node)
            node.setFirstColumnSpanned(True)
            theme.set_item_role(node, 0, _COLORS[severity])
            for finding in findings:
                name = Path(finding.file).name if finding.file else (finding.object_id or "—")
                child = QTreeWidgetItem(node, [name, finding.message, finding.object_id, "", "", ""])
                child.setToolTip(COL_TITLE, finding.message)
                child.setData(0, KIND_ROLE, "finding")
                if finding.file:
                    child.setData(0, PATH_ROLE, finding.file)
                theme.set_item_role(child, COL_NAME, _COLORS[severity])
            # Błędy i ostrzeżenia rozwinięte — od razu widać, co jest nie tak; uwagi zwinięte.
            node.setExpanded(severity != INFO)
        self._update_actions()

    @staticmethod
    def _act_state(act: SetAct) -> Tuple[str, str, str]:
        if act.error:
            return STATE_UNREADABLE
        if act.published:
            return STATE_PUBLISHED
        if act.published_version:
            return ("nowa wersja", "warn",
                    f"W Rejestrze jest wersja {act.published_version} tego aktu — plik to jej nowa wersja")
        if not act.registry_checked:
            return STATE_UNCHECKED
        return STATE_LOCAL

    @staticmethod
    def _app_version(act: SetAct) -> Tuple[str, str, str]:
        """Schemat APP pliku: (tekst, rola koloru, podpowiedź)."""
        if act.error:
            return "—", "muted", "Pliku nie da się odczytać — wersji APP nie można ustalić"
        if not act.app_version:
            return "brak", "err", "Plik nie zawiera obiektów APP — wersji schematu nie da się ustalić"
        if act.app_version == TARGET_VERSION:
            return (act.app_version, "ok",
                    f"Plik w schemacie APP {TARGET_VERSION} — wersja wymagana przy publikacji")
        return (act.app_version, "warn",
                f"Plik w schemacie APP {act.app_version} — do publikacji przepisz go na "
                f"APP {TARGET_VERSION}: „Konwertuj do APP 3.0…”")

    # --- zaznaczenie -----------------------------------------------------------------

    def _act_rows(self) -> List[QTreeWidgetItem]:
        rows = []
        for index in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(index)
            if node.data(0, KIND_ROLE) == "set":
                rows.extend(node.child(i) for i in range(node.childCount()))
        return rows

    def selected_paths(self) -> List[str]:
        """Pliki zaznaczonych aktów (zaznaczony zbiór = wszystkie jego akty), bez powtórzeń."""
        paths: List[str] = []
        for item in self.tree.selectedItems():
            kind = item.data(0, KIND_ROLE)
            if kind == "set":
                candidates = [item.child(i) for i in range(item.childCount())]
            elif kind == "act":
                candidates = [item]
            else:
                continue
            for row in candidates:
                path = str(row.data(0, PATH_ROLE) or "")
                if path and path not in paths:
                    paths.append(path)
        return paths

    def _acts(self, paths) -> List[SetAct]:
        if self._report is None:
            return []
        wanted = set(paths)
        return [act for act in self._report.acts if act.path in wanted]

    def _readable(self, acts: List[SetAct]) -> List[SetAct]:
        return [act for act in acts if not act.error and Path(act.path).is_file()]

    def _update_actions(self) -> None:
        busy = self._service.busy() or self._download.busy()
        selected = self._readable(self._acts(self.selected_paths()))
        everything = self._readable(self._report.acts) if self._report is not None else []
        can_add = self._add_file is not None and not busy
        self.btn_add.setEnabled(can_add and bool(selected))
        self.btn_add.setText(self.tr("Dodaj do mapy ({0})").format(len(selected)) if len(selected) > 1
                             else self.tr("Dodaj do mapy"))
        self.btn_add.setToolTip(self.tr("Dodaj warstwy zaznaczonych aktów do mapy") if selected
                                else self.tr("Zaznacz akt albo zbiór na liście"))
        self.btn_add_all.setEnabled(can_add and bool(everything))
        self.btn_add_all.setText(self.tr("Dodaj wszystkie do mapy ({0})").format(len(everything)) if everything
                                 else self.tr("Dodaj wszystkie do mapy"))
        self.btn_add_all.setToolTip(self.tr("Dodaj warstwy wszystkich aktów z katalogu do mapy, w jednej grupie")
                                    if everything else self.tr("Najpierw przeanalizuj katalog"))
        editable = [act for act in everything if act.editable]
        self.btn_ids.setEnabled(bool(editable) and not busy)
        if self._report is None:
            self.btn_ids.setToolTip(self.tr("Najpierw przeanalizuj katalog"))
        elif not editable and self._report.counts()["unchecked"]:
            self.btn_ids.setToolTip(self.tr("Stan publikacji aktów jest nieznany — odśwież indeks Rejestru "
                                            "i przeanalizuj katalog ponownie"))
        elif not editable:
            self.btn_ids.setToolTip(self.tr("Wszystkie akty są w Rejestrze — zmiana identyfikatorów wymaga "
                                            "nowej wersji aktu"))
        else:
            self.btn_ids.setToolTip(self.tr("Zmień hurtowo przestrzeń nazw, lokalnyId albo wersjaId aktów "
                                            "spoza Rejestru"))
        targets = [act for act in everything if needs_conversion(act)]
        self.btn_convert.setEnabled(bool(targets) and not busy)
        if self._report is None:
            self.btn_convert.setToolTip(self.tr("Najpierw przeanalizuj katalog"))
        elif not targets:
            self.btn_convert.setToolTip(self.tr("Wszystkie akty są już w APP {0} — konwersja nie jest "
                                                "potrzebna").format(TARGET_VERSION))
        else:
            self.btn_convert.setToolTip(self.tr("Przepisz {0} aktów starszych wersji APP na schemat {1} — "
                                                "z kopiami .bak i kontrolą XSD").format(
                len(targets), TARGET_VERSION))
        self.btn_convert.setText(self.tr("Konwertuj do APP 3.0 ({0})…").format(len(targets)) if len(targets) > 1
                                 else self.tr("Konwertuj do APP 3.0…"))
        self._show_details()

    def _show_details(self) -> None:
        items = self.tree.selectedItems()
        if len(items) != 1:
            self.details.setVisible(False)
            return
        item = items[0]
        kind = item.data(0, KIND_ROLE)
        if kind == "finding":
            text = html.escape(item.text(COL_TITLE))
        elif kind == "act":
            act = next(iter(self._acts([item.data(0, PATH_ROLE)])), None)
            if act is None:
                self.details.setVisible(False)
                return
            counts = ", ".join(f"{name}: {count}" for name, count in sorted(act.counts.items()))
            lines = [f"<b>{html.escape(act.title or act.name)}</b>",
                     html.escape(act.path),
                     html.escape(f"{act.namespace}/{act.local_id}/{act.version_id}".strip("/")),
                     html.escape(f"APP {act.app_version or '?'} · {counts}") if counts else "",
                     theme.span(html.escape(self._act_state(act)[2]), self._act_state(act)[1])]
            if act.error:
                lines.append(theme.span(html.escape(act.error), "err"))
            text = "<br>".join(line for line in lines if line)
        else:
            self.details.setVisible(False)
            return
        theme.set_html(self.details, text)
        self.details.setVisible(True)

    def _on_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        kind = item.data(0, KIND_ROLE)
        if kind == "act":
            self.add_paths([str(item.data(0, PATH_ROLE))])
        elif kind == "finding" and item.data(0, PATH_ROLE):
            # Ustalenie → akt, którego dotyczy.
            path = str(item.data(0, PATH_ROLE))
            for row in self._act_rows():
                if row.data(0, PATH_ROLE) == path:
                    self.tree.setCurrentItem(row)
                    self.tree.scrollToItem(row)
                    break

    def _context_menu(self, position) -> None:
        item = self.tree.itemAt(position)
        if item is None or item.data(0, KIND_ROLE) not in ("act", "set"):
            return
        if not item.isSelected():
            self.tree.setCurrentItem(item)
        menu = QMenu(self.tree)
        add = menu.addAction(self.btn_add.text())
        add.setEnabled(self.btn_add.isEnabled())
        add.triggered.connect(self._add_selected)
        editable = [act for act in self._acts(self.selected_paths()) if act.editable]
        ids = menu.addAction(self.tr("Zmień identyfikatory…"))
        ids.setEnabled(bool(editable))
        ids.triggered.connect(self.open_ids)
        convertible = [act for act in self._acts(self.selected_paths()) if needs_conversion(act)]
        convert = menu.addAction(self.tr("Konwertuj do APP 3.0…"))
        convert.setEnabled(bool(convertible))
        convert.triggered.connect(self.open_convert)
        if item.data(0, KIND_ROLE) == "act":
            menu.addSeparator()
            copy = menu.addAction(self.tr("Kopiuj identyfikator aktu"))
            act = next(iter(self._acts([item.data(0, PATH_ROLE)])), None)
            copy.setEnabled(act is not None and bool(act.local_id))
            if act is not None:
                copy.triggered.connect(lambda: QGuiApplication.clipboard().setText(
                    f"{act.namespace}/{act.local_id}/{act.version_id}".strip("/")))
        menu.exec(self.tree.viewport().mapToGlobal(position))

    # --- dodanie do mapy -------------------------------------------------------------

    def _group_name(self) -> str:
        folder = Path(self._report.folder).name if self._report is not None else ""
        return self.tr("Zbiór {0}").format(folder) if folder else self.tr("Zbiór z katalogu")

    def add_paths(self, paths: List[str]) -> int:
        """
        Dodaje akty do mapy (każdy akt to grupa warstw; wszystkie w grupie zbioru).

        :return: liczba aktów przekazanych do dodania.
        """
        if self._add_file is None:
            return 0
        acts = self._readable(self._acts(paths))
        group = self._group_name()
        added = 0
        for act in acts:
            if self._add_file(act.path, group):
                added += 1
        if acts:
            self.message.emit(f"Dodawanie do mapy: {len(acts)} aktów z {self._report.folder}", "INFO")
            self._set_summary(self.tr("Dodawanie do mapy: {0} aktów (grupa „{1}”). Pliki GML są zapisywane do "
                                      "GeoPackage w tle.").format(len(acts), group), "muted")
        return added

    def _add_selected(self) -> None:
        self.add_paths(self.selected_paths())

    def add_all(self) -> int:
        """„Dodaj wszystkie do mapy” — duży zbiór wymaga potwierdzenia (pkt 6.7 wytycznych)."""
        if self._report is None:
            return 0
        acts = self._readable(self._report.acts)
        if len(acts) > CONFIRM_ADD_ABOVE and not self._confirm_add_all(len(acts)):
            return 0
        return self.add_paths([act.path for act in acts])

    def _confirm_add_all(self, count: int) -> bool:
        answer = QMessageBox.question(
            self, self.tr("Dodaj wszystkie do mapy"),
            self.tr("Dodać do mapy {0} aktów? Każdy akt to grupa 2–4 warstw, a pliki GML są najpierw "
                    "zapisywane do GeoPackage — przy dużym zbiorze może to potrwać.").format(count))
        return answer == QMessageBox.StandardButton.Yes

    # --- zmiana identyfikatorów ------------------------------------------------------

    def ids_targets(self) -> List[SetAct]:
        """Akty do zmiany: zaznaczone spoza Rejestru, a bez zaznaczenia — wszystkie spoza Rejestru."""
        if self._report is None:
            return []
        selected = [act for act in self._readable(self._acts(self.selected_paths())) if act.editable]
        return selected or [act for act in self._readable(self._report.acts) if act.editable]

    def open_ids(self):
        """Okno „Zmień identyfikatory” dla aktów spoza Rejestru (zaznaczonych albo wszystkich)."""
        from qgis.PyQt.QtWidgets import QApplication

        from .app_set_ids_dialog import AppSetIdsDialog

        targets = self.ids_targets()
        if not targets or self._report is None:
            return None
        selected = bool([act for act in self._acts(self.selected_paths()) if act.editable])
        target_paths = {act.path for act in targets}
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            files = read_files([act.path for act in self._report.acts if not act.error])
        finally:
            QApplication.restoreOverrideCursor()
        if selected:
            scope = self.tr("Zaznaczone akty spoza Rejestru: {0} z {1} w katalogu. Żeby zmienić wszystkie, "
                            "zamknij okno i usuń zaznaczenie.").format(len(targets), len(self._report.acts))
        else:
            scope = self.tr("Wszystkie akty spoza Rejestru: {0} z {1} w katalogu.").format(
                len(targets), len(self._report.acts))
        dialog = AppSetIdsDialog(
            [item for item in files if item.path in target_paths],
            [item for item in files if item.path not in target_paths],
            [act.path for act in targets if act.published_version],
            scope=scope, service=self._ids_service, parent=self)
        dialog.applied.connect(self._on_ids_applied)
        self._ids_dialog = dialog
        dialog.open()
        return dialog

    def _on_ids_applied(self, result) -> None:
        note = f"zmieniono identyfikatory w {len(result.files)} plikach ({result.replacements} podmian)"
        if result.backups:
            note += f", kopie: {len(result.backups)} plików .bak"
        if result.renamed:
            note += f", nowe nazwy plików: {len(result.renamed)}"
        level = "BŁĄD" if result.errors else "INFO"
        if result.errors:
            note += f", błędy: {'; '.join(result.errors)}"
        self.message.emit(f"Zbiór APP {self._report.folder if self._report else ''}: {note}", level)
        if self._report is not None:
            self.start(self._report.folder)

    # --- konwersja na APP 3.0 ---------------------------------------------------------

    def convert_targets(self) -> List[SetAct]:
        """Akty do konwersji: zaznaczone starszych wersji APP, a bez zaznaczenia — wszystkie takie."""
        if self._report is None:
            return []
        selected = [act for act in self._readable(self._acts(self.selected_paths()))
                    if needs_conversion(act)]
        return selected or [act for act in self._readable(self._report.acts) if needs_conversion(act)]

    def open_convert(self):
        """Okno „Konwersja zbioru do APP 3.0” dla aktów starszych wersji (zaznaczonych albo wszystkich)."""
        from .app_set_convert_dialog import AppSetConvertDialog

        targets = self.convert_targets()
        if not targets or self._report is None:
            return None
        selected = bool([act for act in self._acts(self.selected_paths()) if needs_conversion(act)])
        if selected:
            scope = self.tr("Zaznaczone akty starszych wersji APP: {0} z {1} w katalogu. Żeby "
                            "przekonwertować wszystkie, zamknij okno i usuń zaznaczenie.").format(
                len(targets), len(self._report.acts))
        else:
            scope = self.tr("Wszystkie akty starszych wersji APP: {0} z {1} w katalogu.").format(
                len(targets), len(self._report.acts))
        dialog = AppSetConvertDialog(targets, scope=scope, service=self._convert_service, parent=self)
        dialog.converted.connect(self._on_converted)
        self._convert_dialog = dialog
        dialog.open()
        return dialog

    def _on_converted(self, batch) -> None:
        converted, failed = len(batch.converted), len(batch.failed)
        parts = [f"przekonwertowano {converted} z {len(batch.items)} aktów"]
        if failed:
            parts.append(f"błędów: {failed}")
        if batch.canceled:
            parts.append("przerwano")
        note = ", ".join(parts)
        level = "BŁĄD" if failed and not converted else "INFO"
        folder = self._report.folder if self._report else ""
        self.message.emit(f"Zbiór APP {folder}: konwersja do APP {TARGET_VERSION} — {note}", level)
        if converted and self._report is not None:
            # Pliki są już w 3.0 — analiza pokazuje nowe wersje i ewentualne ustalenia walidatora.
            self.start(self._report.folder)
        else:
            self._set_summary(self.tr("Żadnego aktu nie udało się przekonwertować — powód przy "
                                      "każdym pliku jest w oknie konwersji."), "warn")

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
            self._set_summary(self.tr("Nie udało się zapisać raportu: {0}").format(error), "err")
            return
        self.message.emit(f"Raport zbioru APP: {path}", "INFO")
        self._set_summary(self.tr("Raport zapisany: {0}").format(path), "ok")
