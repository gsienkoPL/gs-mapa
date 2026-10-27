"""Zakładka „Pakiet danych powiatu” panelu GS Mapa: pliki powiatu z otwartych danych GUGiK.

Układ wg ``docs/ui/UI_GUIDELINES.md``: przeszukiwalne combo powiatu (domyślnie powiat
jednostki z paska kontekstu), dane do pobrania, folder, przycisk główny „Pobierz i dodaj (N)”
i w miejscu przycisku — karta postępu plik po pliku.
"""

import logging
import os
from typing import Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import QTimer, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qgis.gui import QgsFileWidget, QgsMapToolEmitPoint

from ..core.labels import plural_layers
from ..core.opendata import DATASETS, FetchResult, OpendataError, plan, sizes_text
from ..core.powiaty import POWIATY, powiat, powiat_label
from . import theme
from .widgets import SearchableCombo, WrappingCheckBox

logger = logging.getLogger("GSMapa.OpendataTab")

#: Ile plików w pakiecie danych jednego powiatu (dla komunikatu „Plik 2 z 2”).
_MAX_FILES = 4


def _county_label(name: str) -> str:
    """Nazwa powiatu do combo: „powiat krakowski” albo „m. Lublin”."""
    return name if name.startswith("m. ") else f"powiat {name}"


class OpendataTab(QWidget):
    """Wybór powiatu i zbiorów, pobranie plików GPKG i dodanie ich tabel do projektu."""

    #: Komunikat do logu wtyczki: (treść, poziom).
    message = pyqtSignal(str, str)

    def __init__(self, service, config=None, parent=None, uldk_service=None, canvas=None):
        super().__init__(parent)
        self._service = service
        self._config = config
        self._uldk = uldk_service
        self._canvas = canvas
        self._map_tool: Optional[QgsMapToolEmitPoint] = None
        self._previous_tool = None
        self._add_after = True
        self._selected_teryt = ""
        #: Powiat z jednostki kontekstu (pokazuje „z jednostki” przy combo).
        self._context_teryt = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(6)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)

        # Powiat: jedno przeszukiwalne combo (nazwa albo TERYT) + celownik.
        self._powiat_combo = SearchableCombo()
        self._powiat_combo.set_items([(f"{_county_label(name)} · {teryt}", teryt)
                                      for teryt, name, _woj in POWIATY])
        self._powiat_combo.setAccessibleName(self.tr("Powiat"))
        self._powiat_combo.selected.connect(self._on_powiat_selected)
        self._btn_from_map = QToolButton()
        self._btn_from_map.setIcon(theme.icon("crosshair", "ButtonText"))
        self._btn_from_map.setCheckable(True)
        self._btn_from_map.setToolTip(self.tr("Wskaż na mapie"))
        self._btn_from_map.setAccessibleName(self.tr("Wskaż na mapie"))
        if self._uldk is None:
            self._btn_from_map.setEnabled(False)
            self._btn_from_map.setToolTip(self.tr("Usługa ULDK jest niedostępna."))
        else:
            self._btn_from_map.setToolTip(self.tr(
                "Wskaż na mapie: kliknij miejsce, a wtyczka znajdzie powiat z działki ewidencyjnej."
            ))
        powiat_row = QWidget()
        powiat_layout = QHBoxLayout(powiat_row)
        powiat_layout.setContentsMargins(0, 0, 0, 0)
        powiat_layout.setSpacing(4)
        powiat_layout.addWidget(self._powiat_combo, 1)
        powiat_layout.addWidget(self._btn_from_map, 0)
        form.addRow(self.tr("Powiat"), powiat_row)

        datasets_box = QWidget()
        datasets_layout = QVBoxLayout(datasets_box)
        datasets_layout.setContentsMargins(0, 0, 0, 0)
        datasets_layout.setSpacing(2)
        self._dataset_checks = {}
        for dataset in DATASETS:
            check = QCheckBox(dataset.label)
            check.setToolTip(dataset.tooltip)
            self._dataset_checks[dataset.key] = check
            datasets_layout.addWidget(check)
        form.addRow(self.tr("Dane"), datasets_box)

        self._folder_widget = QgsFileWidget()
        self._folder_widget.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        self._folder_widget.setDialogTitle(self.tr("Folder na pobrane pliki"))
        form.addRow(self.tr("Folder"), self._folder_widget)
        layout.addLayout(form)

        # Etykieta zawijana — przy 300 px długi tekst pola wyboru nie poszerza karty (pkt 7).
        force_row = WrappingCheckBox(self.tr("Pobierz ponownie pliki, które już są w folderze"))
        force_row.setToolTip(self.tr(
            "Bez zaznaczenia plik już pobrany do folderu jest używany od razu, bez pobierania. "
            "Serwer udostępnia zawsze najnowszy eksport."
        ))
        self._force_check = force_row.check
        layout.addWidget(force_row)

        # Przycisk główny i karta postępu zajmują to samo miejsce.
        self._action_row = QHBoxLayout()
        self._action_row.setSpacing(6)
        self._btn_fetch = QPushButton(self.tr("Pobierz i dodaj"))
        theme.set_icon(self._btn_fetch, "download")
        theme.primary(self._btn_fetch)
        self._action_row.addWidget(self._btn_fetch, 1)
        layout.addLayout(self._action_row)

        self._progress_card = QWidget()
        progress_layout = QVBoxLayout(self._progress_card)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(4)
        self._files_box = QWidget()
        self._files_layout = QVBoxLayout(self._files_box)
        self._files_layout.setContentsMargins(0, 0, 0, 0)
        self._files_layout.setSpacing(2)
        progress_layout.addWidget(self._files_box)
        self._file_rows: List[Tuple[str, QLabel]] = []
        self._progress = QProgressBar()
        self._progress.setTextVisible(True)
        self._progress.setVisible(False)
        progress_layout.addWidget(self._progress)
        self._progress_files = QLabel()
        self._progress_files.setWordWrap(True)
        theme.style_muted(self._progress_files, small=True)
        progress_layout.addWidget(self._progress_files)
        self._btn_cancel = QPushButton(self.tr("Przerwij"))
        self._btn_cancel.setEnabled(False)
        progress_layout.addWidget(self._btn_cancel, 0, Qt.AlignmentFlag.AlignRight)
        self._progress_card.setVisible(False)
        layout.addWidget(self._progress_card)

        self._status = QLabel()
        self._status.setWordWrap(True)
        theme.style_muted(self._status, small=True)
        layout.addWidget(self._status)
        layout.addStretch(1)

        self._restore()

        self._btn_from_map.toggled.connect(self._on_from_map_toggled)
        self._btn_fetch.clicked.connect(self.fetch)
        self._btn_cancel.clicked.connect(self._service.cancel)
        for check in self._dataset_checks.values():
            check.toggled.connect(lambda _checked: self._update_buttons())
        self._folder_widget.fileChanged.connect(lambda _path: self._update_buttons())
        self._service.progress.connect(self._on_progress)
        self._service.finished.connect(self._on_finished)
        if self._uldk is not None:
            self._uldk.parcel_found.connect(self._on_parcel_found)
            self._uldk.parcel_failed.connect(self._on_parcel_failed)
        self._update_buttons()

    # --- stan ---------------------------------------------------------------------

    def _restore(self) -> None:
        state = self._config.opendata_state if self._config is not None else {}
        self._selected_teryt = state.get("teryt", "")
        keys = state.get("datasets", ["dzialki", "budynki"])
        for key, check in self._dataset_checks.items():
            check.setChecked(key in keys)
        folder = self._config.download_dir if self._config is not None else ""
        self._folder_widget.setFilePath(folder if folder and os.path.isdir(folder) else os.path.expanduser("~"))
        if self._selected_teryt:
            self._powiat_combo.set_current_teryt(self._selected_teryt)

    def _save(self) -> None:
        if self._config is None:
            return
        self._config.opendata_state = {"teryt": self.teryt(), "datasets": self.selected_keys()}
        self._config.download_dir = self.folder()

    def teryt(self) -> str:
        """TERYT wybranego powiatu (``""`` gdy nic nie wybrano)."""
        return self._powiat_combo.current_teryt()

    def set_teryt(self, teryt: str) -> None:
        """Wybiera powiat (dla powiatu spoza listy nic nie robi)."""
        self._selected_teryt = teryt
        self._powiat_combo.set_current_teryt(teryt)
        self._update_hint()

    def _on_powiat_selected(self, teryt: str) -> None:
        """Zmiana powiatu w combo — aktualizuje podpowiedź i przyciski."""
        self._selected_teryt = teryt
        self._update_hint()
        self._update_buttons()

    def _update_hint(self) -> None:
        """Podpowiedź „z jednostki”, gdy wybrany powiat pochodzi z paska kontekstu."""
        if self._context_teryt and self.teryt() == self._context_teryt:
            self._powiat_combo.set_hint(self.tr("z jednostki"))
        else:
            self._powiat_combo.set_hint("")

    def set_context_unit(self, unit) -> None:
        """Powiat jednostki z paska kontekstu (gmina — jej powiat); w trakcie pobierania bez zmian."""
        from ..core.units import county_of

        county = county_of(unit) if unit is not None else None
        self._context_teryt = county.teryt if county is not None else ""
        if county is not None and not self._service.busy():
            self.set_teryt(county.teryt)
        else:
            self._update_hint()

    def selected_keys(self) -> list:
        return [key for key, check in self._dataset_checks.items() if check.isChecked()]

    def folder(self) -> str:
        return self._folder_widget.filePath().strip()

    def _update_buttons(self) -> None:
        busy = self._service.busy()
        count = len(self.selected_keys())
        ready = bool(self.teryt()) and count > 0 and os.path.isdir(self.folder())
        self._btn_fetch.setText(self.tr("Pobierz i dodaj ({0})").format(count) if count
                                else self.tr("Pobierz i dodaj"))
        self._btn_fetch.setEnabled(ready and not busy)
        self._btn_fetch.setToolTip("" if ready else self.tr(
            "Wybierz powiat, dane i istniejący folder na pobrane pliki"))
        self._btn_cancel.setEnabled(busy)
        for widget in (self._powiat_combo, self._folder_widget, self._force_check,
                       *self._dataset_checks.values()):
            widget.setEnabled(not busy)
        self._btn_from_map.setEnabled(not busy and self._uldk is not None)
        self._btn_fetch.setVisible(not busy)
        self._progress_card.setVisible(busy)

    # --- pobieranie -------------------------------------------------------------------

    def fetch(self) -> bool:
        """Pobiera zaznaczone dane wybranego powiatu; ``True``, gdy uruchomiono pobieranie."""
        teryt, keys, folder = self.teryt(), self.selected_keys(), self.folder()
        try:
            items = plan(keys)
            self._service.fetch(teryt, keys, folder, force=self._force_check.isChecked())
        except OpendataError as e:
            self._status.setText(str(e))
            self.message.emit(f"Pobieranie danych: {e}", "UWAGA")
            return False
        self._save()
        files = ", ".join(product.gpkg_name(teryt) for product, _tables in items)
        self._status.setText(f"{self.tr('Pobieranie')}: {powiat_label(teryt)} ({files})...")
        self.message.emit(f"Pobieranie danych powiatu {powiat_label(teryt)} ({teryt}): {files} -> {folder}", "INFO")
        self._build_progress_rows(items)
        self._progress.setRange(0, 0)
        self._progress.setFormat("")
        self._progress.setVisible(True)
        self._update_buttons()
        return True

    def _build_progress_rows(self, items) -> None:
        """Karta postępu: wiersz na plik (nazwa pliku, stan i procent)."""
        while self._files_layout.count():
            widget = self._files_layout.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._file_rows = []
        for product, _tables in items:
            label = QLabel(product.label)
            theme.style_muted(label, small=True)
            label.setVisible(False)
            self._files_layout.addWidget(label)
            self._file_rows.append((product.label, label))
        self._progress_files.setText(self.tr("Plik 0 z {0}").format(len(self._file_rows)))
        self._update_progress_files(0)

    def _update_progress_files(self, index: int) -> None:
        """Aktualizuje licznik „Plik N z M” i widoczność wierszy plików."""
        total = len(self._file_rows)
        self._progress_files.setText(self.tr("Plik {0} z {1}").format(min(index, total), total))
        for position, (_label, row) in enumerate(self._file_rows):
            row.setVisible(position < index)

    def _on_progress(self, label: str, received: int, total: int) -> None:
        if total > 0:
            self._progress.setRange(0, 1000)
            self._progress.setValue(int(1000 * received / total))
            self._progress.setFormat(sizes_text(received, total))
        else:
            self._progress.setRange(0, 0)  # nieznany postęp: pasek ruchomy
            self._progress.setFormat(sizes_text(received, -1) if received > 0 else "")
        self._status.setText(label)
        # Postęp przypisujemy do pliku po jego nazwie w komunikacie usługi.
        for position, (name, row) in enumerate(self._file_rows):
            if not label.startswith(name):
                continue
            percent = int(100 * received / total) if total > 0 else 0
            row.setText(f"{name} — {percent}%" if total > 0 else name)
            row.setVisible(True)
            self._update_progress_files(position + 1)
            break

    def _on_finished(self, result: FetchResult) -> None:
        self._progress.setVisible(False)
        self._update_progress_files(len(self._file_rows))
        label = powiat_label(result.teryt)
        for product, message in result.failed:
            self.message.emit(f"{product.label}, {label} ({result.teryt}): {message}", "BŁĄD")
        summary = result.summary(label)
        added = self._service.add_layers(result) if result.ready and not result.canceled else []
        if added:
            summary += f" — {self.tr('dodano')} {plural_layers(len(added))}"
        elif result.ready and not result.canceled:
            summary += f" — {self.tr('warstwy są już na mapie')}"
        if result.failed:
            summary += f" ({'; '.join(message for _product, message in result.failed)})"
        self._status.setText(summary)
        level = "UWAGA" if result.canceled or (result.failed and result.ready) else ("BŁĄD" if result.failed else "INFO")
        self.message.emit(summary, level)
        self._update_buttons()

    # --- powiat z mapy (ULDK) -------------------------------------------------------------

    def _map_canvas(self):
        return self._canvas() if callable(self._canvas) else self._canvas

    def _on_from_map_toggled(self, checked: bool) -> None:
        if not checked:
            self.deactivate_tools()
            return
        if self._map_tool is None:
            canvas = self._map_canvas()
            if canvas is None:
                self._status.setText(self.tr("Mapa QGIS jest niedostępna."))
                QTimer.singleShot(0, lambda: self._btn_from_map.setChecked(False))
                return
            self._map_tool = QgsMapToolEmitPoint(canvas)
            self._map_tool.canvasClicked.connect(self._on_map_clicked)
            self._map_tool.deactivated.connect(lambda: self._btn_from_map.setChecked(False))
        canvas = self._map_tool.canvas()
        if canvas.mapTool() is not self._map_tool:
            self._previous_tool = canvas.mapTool()
        canvas.setMapTool(self._map_tool)
        self._status.setText(self.tr("Kliknij miejsce na mapie."))

    def deactivate_tools(self) -> None:
        """Wyłącza narzędzie mapy zakładki i przywraca poprzednie narzędzie QGIS."""
        if self._map_tool is None or self._map_tool.canvas().mapTool() is not self._map_tool:
            return
        canvas = self._map_tool.canvas()
        previous, self._previous_tool = self._previous_tool, None
        try:
            if previous is not None:
                canvas.setMapTool(previous)
                return
        except RuntimeError:
            pass  # poprzednie narzędzie zostało już usunięte
        canvas.unsetMapTool(self._map_tool)

    def _on_map_clicked(self, point, _button=None) -> None:
        QTimer.singleShot(0, lambda: self._btn_from_map.setChecked(False))
        self._status.setText(self.tr("Wyszukiwanie działki w ULDK..."))
        self._uldk.locate(point, self._map_tool.canvas().mapSettings().destinationCrs())

    def _on_parcel_found(self, location) -> None:
        teryt = location.commune_teryt[:4]
        if powiat(teryt) is None:
            self._status.setText(f"{self.tr('Nieznany powiat')}: {teryt}")
            return
        self.set_teryt(teryt)
        self._status.setText(f"{powiat_label(teryt)} — {self.tr('działka')} {location.parcel_id}")
        self.message.emit(f"ULDK: działka {location.parcel_id} — {powiat_label(teryt)} ({teryt})", "INFO")

    def _on_parcel_failed(self, message: str) -> None:
        self._status.setText(message)
        self.message.emit(f"ULDK: {message}", "UWAGA")
