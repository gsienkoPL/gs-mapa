"""
Karta „Pliki APP” panelu GS Mapa: pasek akcji, strefa upuszczania i ostatnie pliki.

Układ wg ``docs/ui/UI_GUIDELINES.md`` (pkt 6.8): pasek akcji (Nowy ▾ / Otwórz… /
Waliduj… / Konwertuj…), strefa upuszczania plików ``.gml`` i ``.gpkg``, lista
„Ostatnie pliki” oraz jedno zdanie opisu z linkiem do pomocy. Karta nie ma własnego
wyboru jednostki ani listy aktów — jednostkę i akty obsługuje pasek kontekstu i karta
„Plany” (zadania T2 i T3).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from qgis.PyQt.QtCore import Qt, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices, QPainter, QPalette, QPen
from qgis.PyQt.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core import app_recent
from ..core.act_index import ActIndexEntry
from ..core.app_gml import peek_version
from ..core.app_rules import ERROR, WARNING
from ..core.app_set import published_keys
from ..core.units import Unit
from ..services.app_convert import AppConvertService
from ..services.app_files import AppFileService
from ..services.app_template_check import TemplateCheck
from ..services.app_validation import AppValidationService
from . import theme
from .app_convert_dialog import AppConvertDialog
from .app_creator_dialog import AppCreatorDialog
from .app_recent import RecentFilesPanel
from .app_set_dialog import AppSetDialog
from .app_template_dialog import AppTemplateDialog
from .app_validation_dialog import AppValidationDialog
from .widgets import FlowLayout

logger = logging.getLogger("GSMapa.AppTab")

#: Rozszerzenia plików akceptowane w strefie upuszczania.
DROP_SUFFIXES = (".gml", ".gpkg")


class DropZone(QFrame):
    """
    Strefa upuszczania plików ``.gml`` / ``.gpkg`` (pkt 6.8 wytycznych).

    Widoczna, gdy lista ostatnich plików jest pusta; podświetla się w trakcie
    przeciągania. Ramka rysowana pędzlem palety (bez stałych kolorów).
    """

    #: Pliki upuszczone (ścieżki).
    files_dropped = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("gsmapa_app_dropzone")
        self.setAcceptDrops(True)
        self._active = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 16, 12, 16)
        label = QLabel(self.tr("Upuść plik .gml albo .gpkg, aby dodać go do projektu"))
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        theme.style_muted(label)
        layout.addWidget(label)

    # --- przeciąganie --------------------------------------------------------

    @staticmethod
    def _paths(event) -> List[str]:
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        return [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - nazwa z API Qt
        paths = self._paths(event)
        acceptable = [p for p in paths if Path(p).suffix.lower() in DROP_SUFFIXES]
        if acceptable:
            self._active = True
            self.update()
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._active = False
        self.update()

    def dropEvent(self, event) -> None:  # noqa: N802
        self._active = False
        self.update()
        paths = [p for p in self._paths(event) if Path(p).suffix.lower() in DROP_SUFFIXES]
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        pen = QPen(theme.palette().color(QPalette.ColorRole.Mid))
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setWidth(1)
        if self._active:
            pen.setColor(theme.accent(self))
        painter = QPainter(self)
        painter.setPen(pen)
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))


class AppTab(QWidget):
    """Pliki aktów planowania przestrzennego (APP) z dysku."""

    #: Komunikat do logu wtyczki: (treść, poziom).
    message = pyqtSignal(str, str)

    def __init__(self, config=None, parent=None, canvas=None,
                 file_service: Optional[AppFileService] = None,
                 validation_service: Optional[AppValidationService] = None,
                 convert_service: Optional[AppConvertService] = None, namespace_service=None,
                 act_service=None, downloader=None):
        super().__init__(parent)
        self._config = config
        self._canvas = canvas
        self._files = file_service or AppFileService(self)
        self._validation = validation_service or AppValidationService(self)
        self._convert = convert_service or AppConvertService(self)
        #: Osobna usługa walidacji dla plików upuszczonych (nie koliduje z oknem walidacji).
        self._background_validation = AppValidationService(self)
        #: Braki danych w tabelach wzoru po zapisaniu zmian (pasek komunikatów).
        self._template_check = TemplateCheck(parent=self)
        #: Usługa EZiUDP kreatora (dobór przestrzeni nazw); ``None`` = własna instancja kreatora.
        self._namespace_service = namespace_service
        #: Usługi pobierania aktu z Rejestru do walidacji (``None`` = własne przy pierwszym użyciu).
        self._act_service = act_service
        self._downloader = downloader
        #: Akt oczekujący na plik GML do walidacji — filtr odpowiedzi sieciowych.
        self._validation_pending: Optional[str] = None
        self._validation_url: Optional[str] = None
        self._dialog: Optional[AppValidationDialog] = None
        self._convert_dialog: Optional[AppConvertDialog] = None
        self._creator_dialog: Optional[AppCreatorDialog] = None
        #: Okno „Szczegóły” — osobna instancja kreatora bez edycji (nie rusza stanu kreatora).
        self._details_dialog: Optional[AppCreatorDialog] = None
        self._template_dialog: Optional[AppTemplateDialog] = None
        self._set_dialog: Optional[AppSetDialog] = None
        self._unit: Optional[Unit] = None
        self._selected_act: Optional[ActIndexEntry] = None
        self._act_entries: List[ActIndexEntry] = []
        self._acts_url_template: Optional[str] = None
        self._recent: List[dict] = []
        if config is not None:
            self._recent = app_recent.normalize_all(getattr(config, "app_recent_files", []))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        layout.addWidget(self._build_actions())

        self._template_check.report.connect(self._on_template_report)
        self._template_check.attach()

        self.drop_zone = DropZone()
        self.drop_zone.files_dropped.connect(self._on_files_dropped)
        layout.addWidget(self.drop_zone)

        layout.addWidget(self._build_recent_section())

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        theme.style_muted(self.status, small=True)
        layout.addWidget(self.status)

        layout.addWidget(self._build_description())
        layout.addStretch(1)

        self._files.imported.connect(self._on_imported)
        self._files.failed.connect(self._on_failed)
        self._files.progress.connect(self._on_progress)
        self._validation.finished.connect(self._on_validation_finished)
        self._background_validation.finished.connect(self._on_validation_finished)
        self._convert.finished.connect(self._on_convert_finished)
        self.set_unit(None)
        self._update_new_menu()
        self._refresh_recent()
        # Cała karta przyjmuje upuszczanie plików, także gdy lista ostatnich nie jest pusta.
        self.setAcceptDrops(True)

    # --- upuszczanie plików (cała karta) -------------------------------------

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - nazwa z API Qt
        if self._dropped_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        event.accept()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = self._dropped_paths(event)
        if paths:
            self._on_files_dropped(paths)
            event.acceptProposedAction()
        else:
            event.ignore()

    @staticmethod
    def _dropped_paths(event) -> List[str]:
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        return [url.toLocalFile() for url in mime.urls()
                if url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in DROP_SUFFIXES]

    # --- budowa ------------------------------------------------------------

    def _build_actions(self) -> QWidget:
        """Pasek akcji: Nowy ▾ (menu), Otwórz…, Waliduj…, Konwertuj… (zawija się w wąskim doku)."""
        container = QWidget()
        row = FlowLayout(container, spacing=6)

        # „Nowy ▾” to QPushButton z menu, nie QToolButton: ta sama czcionka i wysokość co „Otwórz…”
        # obok, a strzałka menu rysowana przez styl po prawej (na macOS przycisk rozwijany),
        # zamiast wskaźnika nachodzącego na tekst w rogu przycisku narzędziowego.
        self.btn_new = QPushButton(self.tr("Nowy"))
        self.btn_new.setProperty("gsmapa_menu", True)  # miejsce na strzałkę menu (arkusz stylów)
        self.btn_new.setAccessibleName(self.tr("Nowy plik APP"))
        self.btn_new.setToolTip(self.tr("Nowy akt, wzorcowy GeoPackage, wczytanie wypełnionego pliku "
                                        "albo nowa wersja aktu z Rejestru"))
        theme.set_icon(self.btn_new, "plus")
        self._new_menu = QMenu(self.btn_new)
        self._new_menu.setToolTipsVisible(True)
        self._act_create = self._new_menu.addAction(
            theme.icon("wand", "WindowText"), self.tr("Kreator GML APP 3.0…"))
        self._act_create.setToolTip(self.tr("Nowy akt z warstw projektu"))
        self._act_create.triggered.connect(self._on_create_clicked)
        self._act_template = self._new_menu.addAction(
            theme.icon("file", "WindowText"), self.tr("Pusty GeoPackage APP…"))
        self._act_template.setToolTip(self.tr("Wzór: geometria w QGIS, atrybuty w tabeli"))
        self._act_template.triggered.connect(self._on_template_clicked)
        self._act_load = self._new_menu.addAction(
            theme.icon("folder", "WindowText"), self.tr("Wczytaj z pliku…"))
        self._act_load.setToolTip(self.tr("Wypełniony wzorcowy GeoPackage: dane tylko do odczytu, "
                                          "edycja w tabelach QGIS, wynik — GML"))
        self._act_load.triggered.connect(self._on_load_file_clicked)
        self._new_menu.addSeparator()
        self._act_update = self._new_menu.addAction(
            theme.icon("refresh", "WindowText"), self.tr("Nowa wersja aktu z Rejestru…"))
        self._act_update.setToolTip(self.tr("Akt wybrany na karcie Plany"))
        self._act_update.triggered.connect(self._on_update_selected_clicked)
        self.btn_new.setMenu(self._new_menu)
        theme.primary(self.btn_new)
        row.addWidget(self.btn_new)

        self.btn_add = QPushButton(self.tr("Otwórz…"))
        theme.set_icon(self.btn_add, "folder", "/mActionAddLayer.svg")
        self.btn_add.setToolTip(self.tr("Otwórz plik aktu (.gml albo .gpkg) i dodaj go do projektu"))
        row.addWidget(self.btn_add)

        self.btn_validate = QPushButton(self.tr("Waliduj…"))
        theme.set_icon(self.btn_validate, "validate", "/mActionCheckGeometry.svg")
        self.btn_validate.setToolTip(self.tr("Sprawdź plik GML APP: schemat XSD, reguły APP i geometria"))
        row.addWidget(self.btn_validate)

        self.btn_convert = QPushButton(self.tr("Konwertuj…"))
        theme.set_icon(self.btn_convert, "convert", "/mActionRefresh.svg")
        self.btn_convert.setToolTip(self.tr("Przepisz plik GML APP 1.0 / 2.0 na APP 3.0"))
        row.addWidget(self.btn_convert)

        self.btn_set = QPushButton(self.tr("Zbiór z katalogu…"))
        theme.set_icon(self.btn_set, "folder", "/mActionFolder.svg")
        self.btn_set.setToolTip(self.tr("Wskaż katalog z aktami jeszcze nieopublikowanymi: analiza "
                                        "identyfikatorów i dokumentów w całym zbiorze"))
        row.addWidget(self.btn_set)

        self.btn_add.clicked.connect(self.choose_and_add)
        self.btn_validate.clicked.connect(self._on_validate_clicked)
        self.btn_convert.clicked.connect(self._on_convert_clicked)
        self.btn_set.clicked.connect(self._on_set_clicked)
        return container

    def _build_recent_section(self) -> QWidget:
        """Sekcja „Ostatnie pliki” z odnośnikiem „Wyczyść listę” w nagłówku."""
        section = QWidget()
        section.setObjectName("gsmapa_recent_section")
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel(self.tr("Ostatnie pliki"))
        title.setFont(theme.bold_font())
        header.addWidget(title)
        header.addStretch(1)
        self.btn_clear_recent = QToolButton()
        self.btn_clear_recent.setText(self.tr("Wyczyść listę"))
        self.btn_clear_recent.setToolTip(self.tr("Usuń wszystkie pliki z listy ostatnich"))
        theme.link_button(self.btn_clear_recent)
        self.btn_clear_recent.clicked.connect(self._clear_recent)
        header.addWidget(self.btn_clear_recent, 0)
        layout.addLayout(header)

        self.recent_panel = RecentFilesPanel()
        self.recent_panel.action_requested.connect(self._on_recent_action)
        layout.addWidget(self.recent_panel)
        return section

    def _build_description(self) -> QWidget:
        """Jedno zdanie o pliku ``.gml.gpkg`` i link do pomocy (długi opis w oknie pomocy)."""
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        text = QLabel(self.tr("Plik GML zapisujemy obok jako .gml.gpkg, więc mapa nie przelicza go "
                              "przy każdym odświeżeniu."))
        text.setWordWrap(True)
        theme.style_muted(text, small=True)
        layout.addWidget(text)
        link = QToolButton()
        link.setText(self.tr("Jak przygotować plik APP 3.0"))
        link.setToolTip(self.tr("Krok po kroku: kreator, wzorcowy GeoPackage, walidacja"))
        theme.link_button(link, "help")
        link.clicked.connect(self._open_help)
        layout.addWidget(link, 0, Qt.AlignmentFlag.AlignLeft)
        return box

    # --- jednostka i jej akty (dla kreatora) --------------------------------

    def unit(self) -> Optional[Unit]:
        return self._unit

    def set_unit(self, unit: Optional[Unit]) -> None:
        """Jednostka z paska kontekstu (gmina albo województwo) dla kreatora: typy aktów, przestrzeń nazw."""
        self._unit = unit
        if self._creator_dialog is not None:
            self._creator_dialog.set_unit(unit)
        if self._template_dialog is not None:
            self._template_dialog.set_unit(unit)
        self._sync_set_dialog()

    def set_act_index(self, entries) -> None:
        """Indeks aktów Rejestru (z manifestu GS Mapa) — wyszukiwarka aktu w kreatorze."""
        self._act_entries = list(entries or [])
        self._sync_creator_index()
        self._sync_set_dialog()

    def set_acts_unavailable(self, message: str) -> None:
        """Indeks niedostępny — lista aktów jest na karcie Plany, tu nic do pokazania."""

    def set_acts_url_template(self, template: Optional[str]) -> None:
        """Szablon adresu plików aktów (``akty/{act_id}.json``) z manifestu — do pobrania aktu w kreatorze."""
        self._acts_url_template = template
        self._sync_creator_index()
        self._sync_set_dialog()

    def _sync_creator_index(self) -> None:
        if self._creator_dialog is not None:
            self._creator_dialog.set_act_index(self._act_entries, self._acts_url_template)

    def _sync_set_dialog(self) -> None:
        """Okno zbioru dostaje jednostkę, indeks aktów i katalog pobranych danych."""
        if self._set_dialog is not None:
            self._set_dialog.set_published(published_keys(self._act_entries))
            self._set_dialog.set_unit(self._unit, self._act_entries, self._acts_url_template or "",
                                      self._download_base())

    def _download_base(self) -> str:
        """Katalog pobranych danych (ustawienie wtyczki); bez niego — katalog profilu QGIS."""
        base = getattr(self._config, "download_dir", "") if self._config is not None else ""
        if base:
            return str(base)
        from qgis.core import QgsApplication

        return str(Path(QgsApplication.qgisSettingsDirPath()) / "cache" / "gs_mapa" / "akty")

    def set_selected_act(self, entry: Optional[ActIndexEntry]) -> None:
        """Akt zaznaczony na karcie Plany (jedna lista aktów) — „Nowa wersja aktu z Rejestru…”."""
        self._selected_act = entry
        self._update_new_menu()

    def selected_act(self) -> Optional[ActIndexEntry]:
        return self._selected_act

    def update_selected_act(self) -> Optional[AppCreatorDialog]:
        """Kreator z nową wersją aktu zaznaczonego na karcie Plany."""
        return self.update_act(self._selected_act) if self._selected_act is not None else None

    def update_act(self, entry: ActIndexEntry) -> AppCreatorDialog:
        """Kreator z aktualizacją aktu (pobranie GML z Rejestru, warstwy aktu w projekcie)."""
        creator = self.open_creator()
        creator.start_update(entry.act_id)
        return creator

    def _update_new_menu(self) -> None:
        """„Nowa wersja aktu z Rejestru…” — aktywna tylko z zaznaczonym aktem (nieaktywna ma tooltip)."""
        has_act = self._selected_act is not None
        self._act_update.setEnabled(has_act)
        self._act_update.setToolTip(self.tr("Akt wybrany na karcie Plany") if has_act
                                     else self.tr("Zaznacz akt na karcie Plany"))

    def _on_update_selected_clicked(self) -> None:
        if self._selected_act is None:
            return
        self.update_act(self._selected_act)

    # --- walidacja pliku GML aktu z Rejestru (karta Plany) ---------------------------

    def validate_act(self, entry: ActIndexEntry) -> bool:
        """
        „Waliduj” z menu aktu na karcie Plany: pobiera plik GML aktu z GS Mapa i otwiera
        okno walidacji z tym plikiem. Plik ląduje w katalogu współdzielonym z kreatorem.

        :return: ``True``, gdy rozpoczęto pobieranie opisu aktu.
        """
        if not self._acts_url_template:
            self.status.setText(self.tr("Adres plików aktów GS Mapa nie jest jeszcze znany (manifest) — "
                                        "spróbuj za chwilę."))
            self.message.emit("Walidacja: adres plików aktów GS Mapa nie jest jeszcze znany (manifest)", "UWAGA")
            return False
        self._validation_pending, self._validation_url = entry.act_id, None
        self.status.setText(self.tr("Pobieranie opisu aktu {0} z GS Mapa…").format(entry.act_id))
        self.message.emit(f"Walidacja {entry.act_id}: pobieranie opisu", "INFO")
        self._validation_acts().fetch(entry.act_id, self._acts_url_template)
        return True

    def _validation_note(self, text: str, level: str = "BŁĄD") -> None:
        """Nieudana walidacja aktu z Rejestru: komunikat na karcie i w logu wtyczki."""
        self.status.setText(text)
        self.message.emit(text, level)

    def _validation_acts(self):
        """Usługa plików aktów (tworzona i podłączana przy pierwszym użyciu)."""
        if self._act_service is None:
            from ..services.acts import ActService

            self._act_service = ActService()
            self._act_service.setParent(self)
        if not self._act_service.property("gs_mapa_validate"):
            self._act_service.details_ready.connect(self._on_validation_details)
            self._act_service.details_error.connect(self._on_validation_details_error)
            self._act_service.setProperty("gs_mapa_validate", True)
        return self._act_service

    def _validation_downloads(self):
        """Pobieranie pliku GML aktu (tworzone i podłączane przy pierwszym użyciu)."""
        if self._downloader is None:
            from ..services.download import FileDownloader

            self._downloader = FileDownloader()
            self._downloader.setParent(self)
        if not self._downloader.property("gs_mapa_validate"):
            self._downloader.finished.connect(self._on_validation_downloaded)
            self._downloader.failed.connect(self._on_validation_download_failed)
            self._downloader.setProperty("gs_mapa_validate", True)
        return self._downloader

    def _on_validation_details(self, act_id: str, details) -> None:
        """Opis aktu pobrany — walidacja czeka na plik GML z pola ``dane.gml``."""
        if act_id != self._validation_pending or self._validation_url is not None:
            return
        url = details.dane.get("gml")
        if not url:
            self._validation_pending = None
            self._validation_note(self.tr("Akt {0} nie ma pliku GML w Rejestrze — nie można go zwalidować.")
                                  .format(act_id), "UWAGA")
            return
        self._validation_url = url
        self.status.setText(self.tr("Pobieranie pliku GML aktu {0}…").format(act_id))
        # force=True: plik w katalogu pobranych może pochodzić ze starszej wersji aktu.
        self._validation_downloads().download(url, self._update_dir(act_id), force=True)

    def _on_validation_details_error(self, act_id: str, message: str) -> None:
        if act_id != self._validation_pending or self._validation_url is not None:
            return
        self._validation_pending = None
        self._validation_note(self.tr("Nie udało się pobrać opisu aktu {0}: {1}").format(act_id, message))

    def _on_validation_downloaded(self, url: str, path: str) -> None:
        """Plik GML na dysku — okno walidacji z tym plikiem."""
        if url != self._validation_url:
            return
        self._validation_pending, self._validation_url = None, None
        if self._validation.busy():
            self.status.setText(self.tr("Poprzednia walidacja jeszcze trwa — plik {0} jest w oknie walidacji.")
                                .format(Path(path).name))
        else:
            self.status.setText(self.tr("Walidacja {0}…").format(Path(path).name))
        self.open_validation(path)

    def _on_validation_download_failed(self, url: str, message: str) -> None:
        if url != self._validation_url:
            return
        self._validation_pending, self._validation_url = None, None
        self._validation_note(self.tr("Nie udało się pobrać pliku GML aktu: {0}").format(message))

    def _update_dir(self, act_id: str) -> str:
        """Katalog plików pobranych aktów — jak ``_update_dir`` kreatora (wspólna pamięć podręczna)."""
        base = getattr(self._config, "download_dir", "") if self._config is not None else ""
        if not base:
            from qgis.core import QgsApplication

            base = str(Path(QgsApplication.qgisSettingsDirPath()) / "cache" / "gs_mapa" / "akty")
        return str(Path(base) / act_id)

    # --- pliki ------------------------------------------------------------------------

    def _last_dir(self) -> str:
        return (getattr(self._config, "download_dir", "") or str(Path.home())) if self._config else str(Path.home())

    def choose_and_add(self) -> bool:
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Plik aktu APP"), self._last_dir(),
            self.tr("Pliki aktu (*.gpkg *.gml *.GPKG *.GML);;GeoPackage (*.gpkg);;GML (*.gml)"),
        )
        return self.add_file(path) if path else False

    def add_file(self, path: str, group: str = "") -> bool:
        """
        Dodaje plik aktu do mapy (GML przez GeoPackage w tle).

        :param group: grupa nadrzędna w drzewie warstw — akty jednego zbioru w jednej grupie.
        """
        if Path(path).suffix.lower() == ".gml":
            self.status.setText(self.tr("Zapis {0} do GeoPackage…").format(Path(path).name))
            self.progress.setValue(0)
            self.progress.setVisible(True)
        self.message.emit(f"Dodawanie pliku aktu: {path}", "INFO")
        return self._files.add_file(path, group) if group else self._files.add_file(path)

    def _on_template_report(self, text: str, level: str) -> None:
        """Braki w tabeli wzoru po zapisaniu zmian — jedno zdanie w pasku komunikatów panelu."""
        self.message.emit(text, level)

    def _on_progress(self, path: str, value: float) -> None:
        self.progress.setValue(int(value))

    def _on_imported(self, path: str, count: int, source: str) -> None:
        self.progress.setVisible(self._files.busy())
        note = "" if source == path else self.tr(" (z pliku {0})").format(source)
        self.status.setText(self.tr("Dodano {0}: tabel {1}{2}").format(Path(path).name, count, note))
        self.message.emit(f"Dodano plik aktu {path}: tabel {count}" + (f", GeoPackage {source}" if source != path else ""),
                          "INFO")
        self._remember(path)

    def _on_failed(self, path: str, message: str) -> None:
        self.progress.setVisible(self._files.busy())
        self.status.setText(self.tr("Nie udało się dodać {0}: {1} Sprawdź, czy plik istnieje, "
                                    "i spróbuj ponownie.").format(Path(path).name, message))
        self.message.emit(f"Nie udało się dodać pliku {path}: {message}", "BŁĄD")

    def _on_validate_clicked(self) -> None:
        self.open_validation()

    def _on_set_clicked(self) -> None:
        self.open_set()

    def open_set(self, folder: Optional[str] = None) -> AppSetDialog:
        """
        Okno „Zbiór z katalogu”: analiza identyfikatorów i dokumentów aktów z katalogu.

        :param folder: katalog do przeanalizowania (``None`` = okno bez analizy, wybór w oknie).
        :return: okno (jedno na kartę, stan zostaje do końca sesji).
        """
        if self._set_dialog is None:
            self._set_dialog = AppSetDialog(
                self.window(), published=published_keys(self._act_entries),
                add_file=self.add_file,
            )
            self._set_dialog.message.connect(self.message.emit)
            self._sync_set_dialog()
        dialog = self._set_dialog
        if folder:
            dialog.start(folder)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def _on_convert_clicked(self) -> None:
        self.open_conversion()

    def _on_create_clicked(self) -> None:
        self.open_creator()

    def _on_template_clicked(self) -> None:
        self.open_template()

    def _on_load_file_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Plik GeoPackage APP"), self._last_dir(),
            self.tr("GeoPackage (*.gpkg *.GPKG);;Wszystkie pliki (*)"),
        )
        if path:
            self.load_file(path)

    def load_file(self, path: str) -> bool:
        """
        „Wczytaj z pliku…”: wypełniony wzorcowy GeoPackage w kreatorze w trybie edycji w tabelach.

        Kreator czyta z pliku akt, dokumenty i rysunki (formularz tylko do odczytu), dodaje
        warstwy pliku do projektu i pokazuje braki; wynik zapisuje „Generuj GML”.

        :param path: plik ``.gpkg`` albo inny plik OGR z tabelami APP.
        :return: ``True``, gdy kreator wczytał plik.
        """
        creator = self.open_creator()
        if not creator.load_table_source(path):
            self.message.emit(f"Nie wczytano {path}: brak aktu, dokumentów i rysunków w tabelach", "UWAGA")
            return False
        self._remember(path)
        self.message.emit(f"Wczytano GeoPackage {path}: dane tylko do odczytu, GML z „Generuj GML”", "INFO")
        return True

    def edit_file(self, path: str) -> bool:
        """
        „Edytuj”: plik z listy ostatnich w kreatorze, w trybie edycji.

        GML otwiera się jako **nowa wersja aktu** (ten sam ``lokalnyId``, nowa ``wersjaId``, zapis
        tworzy dwa pliki), GeoPackage — w trybie edycji w tabelach (dane tylko do odczytu
        w formularzu, zmiany w tabelach QGIS).

        :param path: plik ``.gml`` albo ``.gpkg`` z listy ostatnich.
        :return: ``True``, gdy kreator wczytał plik.
        """
        target = Path(path)
        creator = self.open_creator()
        ok = (creator.load_update_file(str(target)) if target.suffix.lower() == ".gml"
              else creator.load_table_source(str(target)))
        if not ok:
            self.message.emit(f"Nie wczytano {target.name}: brak danych APP w pliku", "UWAGA")
            return False
        self._remember(str(target))
        return True

    def show_details(self, path: str) -> Optional[AppCreatorDialog]:
        """
        „Szczegóły”: okno jak kreator, ale tylko do odczytu — akt, dokumenty i rysunki z pliku.

        Okno ma własną instancję kreatora, więc otwarcie podglądu nie kasuje tego, co użytkownik
        ma w kreatorze (np. przygotowywana nowa wersja aktu). Warstwy nie są ruszane, a plik
        nie jest zapisywany.

        :param path: plik ``.gml`` albo ``.gpkg`` z listy ostatnich.
        :return: okno podglądu; ``None``, gdy pliku nie dało się odczytać.
        """
        if self._details_dialog is None:
            self._details_dialog = AppCreatorDialog(
                self.window(), canvas=None, message=self.message.emit, unit=self._unit,
                file_service=self._files, config=self._config,
            )
        dialog = self._details_dialog
        dialog.set_details_mode(True)
        if not dialog.load_details(path):
            self.message.emit(f"Nie wczytano {Path(path).name}: brak aktu, dokumentów i rysunków", "UWAGA")
            return None
        self._remember(path)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    # --- ostatnie pliki ------------------------------------------------------------------

    def recent_entries(self) -> List[dict]:
        """Wpisy listy ostatnich plików."""
        return list(self._recent)

    def _remember(self, path: str) -> None:
        """Dodaje plik do listy ostatnich (z wersją APP rozpoznaną z pliku)."""
        if not path:
            return
        file_path = Path(path)
        self._recent = app_recent.upsert(
            self._recent, str(file_path), name=file_path.name, directory=str(file_path.parent),
            version=peek_version(file_path) or "",
        )
        self._save_recent()

    def _save_recent(self) -> None:
        if self._config is not None:
            self._config.app_recent_files = self._recent
        self._refresh_recent()

    def _refresh_recent(self) -> None:
        self.recent_panel.set_entries(self._recent)
        has_files = bool(self._recent)
        self.recent_panel.setVisible(has_files)
        self.drop_zone.setVisible(not has_files)
        self.btn_clear_recent.setVisible(has_files)

    def _clear_recent(self) -> None:
        """Czyści listę ostatnich plików (pliki zostają na dysku)."""
        self._recent = []
        self._save_recent()

    def _remove_recent(self, path: str) -> None:
        self._recent = app_recent.remove(self._recent, path)
        self._save_recent()

    def _on_recent_action(self, action: str, path: str) -> None:
        """Akcja na wierszu listy ostatnich plików."""
        if action == "edit":
            self.edit_file(path)
        elif action == "details":
            self.show_details(path)
        elif action == "add":
            self.add_file(path)
        elif action == "validate":
            self.open_validation(path)
        elif action == "convert":
            self.open_conversion(path)
        elif action == "show":
            folder = str(Path(path).parent)
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        elif action == "remove":
            self._remove_recent(path)

    def _on_files_dropped(self, paths: List[str]) -> None:
        """Upuszczone pliki: dodanie do projektu; GML — dodatkowo walidacja w tle."""
        for path in paths:
            self.add_file(path)
            self._remember(path)
            if Path(path).suffix.lower() == ".gml":
                self._validate_in_background(path)

    def _validate_in_background(self, path: str) -> None:
        """Waliduje plik w tle — ikona statusu na liście ostatnich."""
        if not self._background_validation.start(path):
            return
        self.status.setText(self.tr("Walidacja {0}…").format(Path(path).name))

    def _on_validation_finished(self, report) -> None:
        """Zapisuje wynik walidacji w liście ostatnich plików (ikona statusu)."""
        path = str(getattr(report, "path", "") or "")
        if not path:
            return
        counts = getattr(report, "summary", {}) or {}
        errors = int(counts.get(ERROR, 0) or 0)
        warnings = int(counts.get(WARNING, 0) or 0)
        if getattr(report, "fatal", None):
            errors = max(errors, 1)
        file_path = Path(path)
        self._recent = app_recent.upsert(
            self._recent, path, name=file_path.name, directory=str(file_path.parent),
            version=getattr(report, "version", None),
            status=app_recent.status_from_counts(errors, warnings),
            errors=errors, warnings=warnings,
        )
        self._save_recent()

    def _on_convert_finished(self, outcome) -> None:
        """Wynik konwersji — plik APP 3.0 trafia na listę ostatnich."""
        target = str(getattr(outcome, "target", "") or "")
        if target and getattr(outcome, "ok", False):
            file_path = Path(target)
            self._recent = app_recent.upsert(
                self._recent, target, name=file_path.name, directory=str(file_path.parent),
                version="3.0",
            )
            self._save_recent()

    def _open_help(self) -> None:
        """Okno pomocy „Jak przygotować plik APP 3.0”."""
        from .app_help_dialog import AppHelpDialog

        dialog = AppHelpDialog(self.window())
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    # --- okna ------------------------------------------------------------------------

    def open_validation(self, path: Optional[str] = None) -> AppValidationDialog:
        """Okno walidacji; z podaną ścieżką od razu ją sprawdza (np. wynik konwersji)."""
        if self._dialog is None:
            self._dialog = AppValidationDialog(
                self.window(), canvas=self._canvas, validation_service=self._validation,
                file_service=self._files, message=lambda text, level: self.message.emit(text, level),
            )
        self._dialog.show()
        self._dialog.raise_()
        self._dialog.activateWindow()
        if path:
            self._dialog.set_path(path)
            self._dialog.start()
        return self._dialog

    def open_conversion(self, path: Optional[str] = None) -> AppConvertDialog:
        """Okno konwersji GML APP 1.0 / 2.0 do APP 3.0."""
        if self._convert_dialog is None:
            self._convert_dialog = AppConvertDialog(
                self.window(), convert_service=self._convert,
                open_validation=self.open_validation, add_file=self.add_file, message=self.message.emit,
            )
        if path:
            self._convert_dialog.set_source(path)
        self._convert_dialog.show()
        self._convert_dialog.raise_()
        self._convert_dialog.activateWindow()
        return self._convert_dialog

    def open_creator(self) -> AppCreatorDialog:
        """Kreator nowego pliku GML APP 3.0 (stan formularza zostaje do końca sesji)."""
        if self._creator_dialog is None:
            self._creator_dialog = AppCreatorDialog(
                self.window(), canvas=self._canvas, open_validation=self.open_validation, add_file=self.add_file,
                message=self.message.emit, unit=self._unit, file_service=self._files, config=self._config,
                namespace_service=self._namespace_service,
            )
            self._sync_creator_index()
        else:
            self._creator_dialog.auto_namespace()
        self._creator_dialog.show()
        self._creator_dialog.raise_()
        self._creator_dialog.activateWindow()
        return self._creator_dialog

    def open_template(self) -> AppTemplateDialog:
        """Okno wzorcowego (pustego) pliku GeoPackage APP 3.0."""
        if self._template_dialog is None:
            self._template_dialog = AppTemplateDialog(
                self.window(), config=self._config, add_file=self.add_file, message=self.message.emit,
                unit=self._unit,
            )
        self._template_dialog.show()
        self._template_dialog.raise_()
        self._template_dialog.activateWindow()
        return self._template_dialog

    def shutdown(self) -> None:
        """Przerywa zadania w tle i zamyka okna walidacji, konwersji, wzoru i kreatora (przy rozładowaniu wtyczki)."""
        self._template_check.detach()
        self._validation.cancel()
        self._background_validation.cancel()
        self._convert.cancel()
        self._files.cancel()
        for name in ("_dialog", "_convert_dialog", "_creator_dialog", "_details_dialog", "_template_dialog",
                     "_set_dialog"):
            dialog = getattr(self, name)
            if dialog is not None:
                dialog.close()
                dialog.deleteLater()
                setattr(self, name, None)
