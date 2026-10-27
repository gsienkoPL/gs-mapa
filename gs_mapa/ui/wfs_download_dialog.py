"""Okno „Pobierz WFS do GeoPackage”: plik docelowy, zakres obiektów i dodanie wyniku do projektu."""

import os
from typing import Sequence

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QRadioButton,
    QVBoxLayout,
)
from qgis.gui import QgsFileWidget

from ..core.labels import plural_layers
from . import theme


class WfsDownloadDialog(QDialog):
    """Wybór pliku GPKG i zakresu pobierania warstw WFS."""

    def __init__(self, titles: Sequence[str], default_path: str, extent_available: bool, parent=None):
        super().__init__(parent)
        # Własny wygląd wtyczki (makiety docs/ui), niezależny od stylu Qt w QGIS.
        theme.apply_plugin_style(self)
        self.setWindowTitle(self.tr("Pobierz WFS do GeoPackage"))
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)

        names = ", ".join(titles[:5]) + (" …" if len(titles) > 5 else "")
        intro = QLabel(f"{self.tr('Pobierz')} {plural_layers(len(titles))} WFS: {names}")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(QLabel(self.tr("Plik GeoPackage (każda warstwa w osobnej tabeli):")))
        self.file_widget = QgsFileWidget()
        self.file_widget.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.file_widget.setFilter(self.tr("GeoPackage (*.gpkg)"))
        self.file_widget.setDialogTitle(self.tr("Zapisz jako GeoPackage"))
        self.file_widget.setConfirmOverwrite(True)
        self.file_widget.setFilePath(default_path)
        layout.addWidget(self.file_widget)

        self.extent_radio = QRadioButton(self.tr("Obiekty z widocznego zasięgu mapy"))
        self.all_radio = QRadioButton(self.tr("Cały zbiór"))
        self.extent_radio.setEnabled(extent_available)
        (self.extent_radio if extent_available else self.all_radio).setChecked(True)
        layout.addWidget(self.extent_radio)
        layout.addWidget(self.all_radio)
        warning = QLabel(self.tr(
            "Cały zbiór (np. EGiB powiatu) może mieć setki tysięcy obiektów — pobieranie trwa wtedy "
            "długo. Postęp i przerwanie: menedżer zadań na pasku stanu QGIS."
        ))
        warning.setWordWrap(True)
        theme.style_muted(warning, small=True)
        layout.addWidget(warning)

        self.add_check = QCheckBox(self.tr("Dodaj pobrane warstwy do projektu"))
        self.add_check.setChecked(True)
        layout.addWidget(self.add_check)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self.tr("Pobierz"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(self.tr("Anuluj"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.file_widget.fileChanged.connect(lambda _path: self._update_ok())
        self._update_ok()

    def path(self) -> str:
        """Ścieżka pliku z rozszerzeniem ``.gpkg`` (pusta, gdy nie wybrano)."""
        path = self.file_widget.filePath().strip()
        if path and not path.lower().endswith(".gpkg"):
            path += ".gpkg"
        return os.path.abspath(os.path.expanduser(path)) if path else ""

    def extent_only(self) -> bool:
        return self.extent_radio.isChecked()

    def add_to_project(self) -> bool:
        return self.add_check.isChecked()

    def _update_ok(self) -> None:
        path = self.path()
        self._ok_button.setEnabled(bool(path) and os.path.isdir(os.path.dirname(path)))
