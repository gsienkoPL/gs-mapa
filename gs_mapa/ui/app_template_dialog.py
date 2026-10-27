"""Okno tworzenia wzorcowego (pustego) pliku GeoPackage dla aktu APP 3.0."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
)
from qgis.gui import QgsFileWidget

from ..core.app_layers import LAYERS, layers_for, template_filename
from ..core.app_rules import SUPPORTED_EPSG
from ..core.app_writer import POG, load_codes
from ..services.app_template import CRS_LABELS, TemplateError, create_template
from . import theme

logger = logging.getLogger("GSMapa.AppTemplate")


def _tail(uri: str) -> str:
    return uri.rstrip("/").rsplit("/", 1)[-1]


class AppTemplateDialog(QDialog):
    """Pusty plik GPKG z tabelami i atrybutami APP 3.0 — do wypełnienia w QGIS."""

    def __init__(self, parent=None, config=None, add_file: Optional[Callable[[str], bool]] = None,
                 message: Optional[Callable[[str, str], None]] = None, codes: Optional[dict] = None):
        super().__init__(parent)
        # Własny wygląd wtyczki (makiety docs/ui), niezależny od stylu Qt w QGIS.
        theme.apply_plugin_style(self)
        self.setWindowTitle(self.tr("Nowy plik GeoPackage APP 3.0 (wzór)"))
        self.setObjectName("GSMapaAppTemplateDialog")
        self.resize(620, 380)
        self._config = config
        self._add_file = add_file
        self._message = message
        self.codes = codes or load_codes()

        layout = QVBoxLayout(self)
        intro = QLabel(self.tr(
            "Tworzy pusty plik GeoPackage z tabelami aktu APP 3.0: wszystkie atrybuty schematu "
            "są kolumnami do wypełnienia, a geometrię rysujesz w warstwie. Nazwy kolumn są nazwami "
            "atrybutów APP, więc kreator GML czyta plik bez mapowania pól („Dopasuj” wypełni się sam). "
            "Układ wybierasz tutaj, bo plik ma jedną kolumnę geometrii — APP wymaga jednego układu "
            "w całym akcie."))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        types = self.codes["act_types"]["values"]
        self.plan_type = QComboBox()
        for code, label in sorted(((_tail(uri), label) for uri, label in types.items()), key=lambda i: i[1]):
            self.plan_type.addItem(label, code)
        index = self.plan_type.findData(POG)
        if index >= 0:
            self.plan_type.setCurrentIndex(index)
        self.plan_type.setToolTip(self.tr("Plan ogólny gminy ma strefy planistyczne i regulacje (OUZ, OZS, "
                                          "OSDIS); pozostałe akty — akt, dokumenty i rysunki."))
        self.epsg = QComboBox()
        for epsg in SUPPORTED_EPSG:
            self.epsg.addItem(CRS_LABELS.get(epsg, f"EPSG:{epsg}"), epsg)
        self.epsg.setCurrentIndex(self.epsg.findData(2180))
        self.path = QgsFileWidget()
        self.path.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.path.setFilter(self.tr("GeoPackage (*.gpkg)"))
        self.path.setDialogTitle(self.tr("Zapisz wzorcowy plik GeoPackage APP 3.0"))
        self.add_layers = QCheckBox(self.tr("Dodaj tabele do projektu"))
        self.add_layers.setChecked(True)
        self.add_layers.setToolTip(self.tr("Tabele dostaną style oficjalnej wtyczki APP"))
        form.addRow(self.tr("Typ aktu *:"), self.plan_type)
        form.addRow(self.tr("Układ pliku *:"), self.epsg)
        form.addRow(self.tr("Plik *:"), self.path)
        form.addRow("", self.add_layers)
        layout.addLayout(form)

        self.info = QLabel("")
        self.info.setWordWrap(True)
        theme.style_muted(self.info)
        layout.addWidget(self.info)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self.tr("Utwórz plik"))
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(self.tr("Zamknij"))
        buttons.accepted.connect(self.create)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.plan_type.currentIndexChanged.connect(self._refresh)
        self.epsg.currentIndexChanged.connect(self._refresh)
        self._refresh()

    # --- stan formularza ---------------------------------------------------------

    def _last_dir(self) -> Path:
        directory = getattr(self._config, "download_dir", "") if self._config else ""
        return Path(directory) if directory else Path.home()

    def plan_code(self) -> str:
        return self.plan_type.currentData()

    def epsg_code(self) -> int:
        return int(self.epsg.currentData())

    def _suggested_path(self) -> str:
        name = template_filename(self.plan_code(), self.epsg_code())
        return str(self._last_dir() / name)

    def _refresh(self, *args) -> None:
        """Nazwa pliku i lista tabel zależą od typu aktu i układu; nie nadpisuj wybranej ścieżki."""
        current = self.path.filePath().strip()
        if not current or Path(current).name.startswith("wzor-app-"):
            self.path.setFilePath(self._suggested_path())
        layers = layers_for(self.plan_code())
        fields = sum(len(layer.fields) for layer in layers)
        self.info.setText(self.tr("Tabele ({0}), atrybuty: {1} — {2}").format(
            len(layers), fields, ", ".join(layer.name for layer in layers)))
        if len(layers) < len(LAYERS):
            self.info.setText(self.info.text() + self.tr(
                "\nStrefy i regulacje są tylko w planie ogólnym gminy — dla tego aktu ustalenia opisuje rysunek."))

    # --- utworzenie pliku -------------------------------------------------------

    def create(self) -> None:
        path = self.path.filePath().strip()
        if not path:
            self._fail(self.tr("Wskaż plik wynikowy."))
            return
        if not path.lower().endswith(".gpkg"):
            path += ".gpkg"
        target = Path(path)
        overwrite = False
        if target.exists():
            from qgis.PyQt.QtWidgets import QMessageBox

            answer = QMessageBox.question(
                self, self.tr("Plik istnieje"),
                self.tr("Plik {0} już istnieje. Zastąpić go nowym wzorem (dotychczasowa zawartość "
                        "zostanie utracona)?").format(target.name))
            if answer != QMessageBox.StandardButton.Yes:
                return
            overwrite = True
        try:
            tables = create_template(str(target), self.plan_code(), self.epsg_code(), overwrite=overwrite)
        except TemplateError as e:
            self._fail(str(e))
            return
        except Exception as e:  # noqa: BLE001 — komunikat trafia do użytkownika
            logger.exception("Nie udało się utworzyć wzorcowego pliku APP")
            self._fail(str(e))
            return
        text = self.tr("Utworzono {0}: tabel {1}").format(target.name, len(tables))
        self.status.setText(text)
        self._log(f"Wzorcowy plik APP: {target} (tabele: {', '.join(tables)})")
        if self.add_layers.isChecked() and self._add_file is not None:
            self._add_file(str(target))
        self.accept()

    def _fail(self, message: str) -> None:
        self.status.setText(message)
        self._log(f"Nie udało się utworzyć wzorcowego pliku APP: {message}", "BŁĄD")

    def _log(self, text: str, level: str = "INFO") -> None:
        if self._message is not None:
            self._message(text, level)
