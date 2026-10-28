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
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)
from qgis.gui import QgsFileWidget

from ..core.app_codes import coded_values
from ..core.app_defaults import document_defaults
from ..core.app_gml import DOCUMENT
from ..core.app_forms import TemplateValues
from ..core.app_layers import LAYERS, layers_for, template_filename
from ..core.app_namespace import namespace_mismatch, parse_namespace
from ..core.units import Unit
from ..core.app_rules import SUPPORTED_EPSG
from ..core.app_writer import POG, load_codes
from ..services.app_template import (
    CRS_LABELS,
    RefreshResult,
    TemplateError,
    configure_project_layers,
    create_template,
    refresh_forms,
)
from . import theme

logger = logging.getLogger("GSMapa.AppTemplate")


def _tail(uri: str) -> str:
    return uri.rstrip("/").rsplit("/", 1)[-1]


class AppTemplateDialog(QDialog):
    """Pusty plik GPKG z tabelami i atrybutami APP 3.0 — do wypełnienia w QGIS."""

    def __init__(self, parent=None, config=None, add_file: Optional[Callable[[str], bool]] = None,
                 message: Optional[Callable[[str, str], None]] = None, codes: Optional[dict] = None,
                 unit: Optional[Unit] = None, eziudp_service=None):
        super().__init__(parent)
        # Własny wygląd wtyczki (makiety docs/ui), niezależny od stylu Qt w QGIS.
        theme.apply_plugin_style(self)
        self.setWindowTitle(self.tr("Nowy plik GeoPackage APP 3.0 (wzór)"))
        self.setObjectName("GSMapaAppTemplateDialog")
        self.resize(680, 380)
        self._config = config
        self._add_file = add_file
        self._message = message
        self.codes = codes or load_codes()
        self._unit: Optional[Unit] = None
        #: Usługa EZiUDP okna wyszukiwania przestrzeni nazw (``None`` — własna, tworzona przy otwarciu).
        self._eziudp_service = eziudp_service
        self._namespace_dialog = None

        layout = QVBoxLayout(self)
        intro = QLabel(self.tr(
            "Tworzy pusty plik GeoPackage z tabelami aktu APP 3.0: wszystkie atrybuty schematu "
            "są kolumnami do wypełnienia, a geometrię rysujesz w warstwie. Nazwy kolumn są nazwami "
            "atrybutów APP, więc kreator GML czyta plik bez mapowania pól („Dopasuj” wypełni się sam). "
            "Układ wybierasz tutaj, bo plik ma jedną kolumnę geometrii — APP wymaga jednego układu "
            "w całym akcie. Identyfikatory, wersje, symbole i oznaczenia nowych obiektów wypełniają się "
            "same; przestrzeń nazw i jednostka stąd są wartościami domyślnymi."))
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
        # Przestrzeń nazw i jednostka trafiają do pliku jako wartości domyślne: każdy nowy obiekt
        # dostaje przestrzeń nazw (dopóki tabela aktu jej nie poda), a dokument — organ i dziennik.
        self.namespace = QLineEdit()
        self.namespace.setPlaceholderText(self.tr("opcjonalnie, np. PL.ZIPPZP.9999/146501-POG"))
        self.namespace.setToolTip(self.tr(
            "Przestrzeń nazw zbioru danych — wartość domyślna pól „Przestrzeń nazw” wszystkich tabel. "
            "Można ją też wpisać później w tabeli aktu: obiekty wezmą ją stamtąd."))
        self.btn_namespace = QPushButton(self.tr("Szukaj w EZiUDP…"))
        self.btn_namespace.setToolTip(self.tr("Wyszukaj zbiór APP jednostki w Ewidencji Zbiorów i Usług Danych "
                                              "Przestrzennych"))
        namespace_row = QHBoxLayout()
        namespace_row.addWidget(self.namespace, 1)
        namespace_row.addWidget(self.btn_namespace)
        self.unit_label = QLabel("")
        self.unit_label.setWordWrap(True)
        theme.style_muted(self.unit_label)
        form.addRow(self.tr("Typ aktu *:"), self.plan_type)
        form.addRow(self.tr("Układ pliku *:"), self.epsg)
        form.addRow(self.tr("Przestrzeń nazw:"), namespace_row)
        form.addRow(self.tr("Jednostka:"), self.unit_label)
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
        # Wzór utworzony starszą wersją wtyczki dostaje nowe formularze bez tworzenia pliku od nowa.
        self.btn_refresh = buttons.addButton(self.tr("Odśwież formularze w pliku…"),
                                             QDialogButtonBox.ButtonRole.ActionRole)
        self.btn_refresh.setToolTip(self.tr(
            "Karty, nazwy pól, podpowiedzi, kalendarze i listy wyboru w istniejącym pliku wzoru. "
            "Dane zostają bez zmian."))
        buttons.accepted.connect(self.create)
        buttons.rejected.connect(self.reject)
        self.btn_refresh.clicked.connect(self._on_refresh_clicked)
        layout.addWidget(buttons)

        self.plan_type.currentIndexChanged.connect(self._refresh)
        self.epsg.currentIndexChanged.connect(self._refresh)
        self.namespace.textChanged.connect(self._refresh)
        self.btn_namespace.clicked.connect(self._on_find_namespace)
        self.set_unit(unit)
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

    def unit(self) -> Optional[Unit]:
        return self._unit

    def set_unit(self, unit: Optional[Unit]) -> None:
        """Jednostka z paska kontekstu panelu: organ i dziennik dokumentów, TERYT do wyszukiwania."""
        self._unit = unit
        if unit is None:
            self.unit_label.setText(self.tr("nie wybrano (pasek na górze panelu GS Mapa) — dokumenty bez "
                                            "domyślnego organu i dziennika"))
            return
        defaults = document_defaults(unit, self.codes)
        journals = coded_values(self.codes).get(DOCUMENT, {}).get("dziennikUrzedowy", {})
        details = [defaults["authority"]] if defaults.get("authority") else []
        if defaults.get("journal"):
            details.append(journals.get(defaults["journal"], defaults["journal"]))
        self.unit_label.setText(unit.label + (self.tr(" — dokumenty: {0}").format(", ".join(details))
                                              if details else ""))

    def template_values(self) -> TemplateValues:
        """Dane wzoru do wartości domyślnych pól (``core/app_forms.TemplateValues``)."""
        defaults = document_defaults(self._unit, self.codes)
        return TemplateValues(self.plan_code(), self.namespace.text().strip(), self.epsg_code(),
                              defaults.get("authority", ""), defaults.get("journal", ""))

    def _on_find_namespace(self) -> None:
        self.find_namespace()

    def find_namespace(self):
        """Okno wyszukiwania przestrzeni nazw w EZiUDP; wybrany zbiór trafia do pola."""
        from .app_namespace_dialog import AppNamespaceDialog

        if self._namespace_dialog is None:
            self._namespace_dialog = AppNamespaceDialog(self._eziudp_service, parent=self)
            self._namespace_dialog.accepted.connect(self._on_namespace_accepted)
        if self._unit is not None and not self.namespace.text().strip() and not self._namespace_dialog.teryt.text():
            self._namespace_dialog.teryt.setText(self._unit.jpt)
        self._namespace_dialog.prepare(self.plan_code(), self.namespace.text())
        self._namespace_dialog.show()
        self._namespace_dialog.raise_()
        self._namespace_dialog.activateWindow()
        return self._namespace_dialog

    def _on_namespace_accepted(self) -> None:
        namespace = self._namespace_dialog.selected_namespace() if self._namespace_dialog is not None else ""
        if namespace:
            self.namespace.setText(namespace)

    def closeEvent(self, event) -> None:  # noqa: N802 — nazwa z Qt
        if self._namespace_dialog is not None:
            self._namespace_dialog.close()
        super().closeEvent(event)

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
        namespace = self.namespace.text().strip()
        problem = ""
        if namespace and parse_namespace(namespace) is None:
            problem = self.tr("Przestrzeń nazw nie ma postaci PL.ZIPPZP.<numer>/<TERYT>-<TYP>.")
        elif namespace:
            mismatch = namespace_mismatch(namespace, self.plan_code()) or ""
            problem = mismatch[:1].upper() + mismatch[1:] + "." if mismatch else ""
        if problem:
            self.info.setText(self.info.text() + "\n" + problem)

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
            tables = create_template(str(target), self.plan_code(), self.epsg_code(), overwrite=overwrite,
                                     codes=self.codes, values=self.template_values())
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

    # --- odświeżenie formularzy w istniejącym pliku ------------------------------------

    def _on_refresh_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Plik wzoru GeoPackage APP 3.0"), str(self._last_dir()),
            self.tr("GeoPackage (*.gpkg *.GPKG)"))
        if path:
            self.refresh(path)

    def refresh(self, path: str, layers=None) -> Optional[RefreshResult]:
        """
        Odświeża formularze w pliku wzoru i w jego warstwach dodanych do projektu.

        :param path: plik GeoPackage z tabelami APP.
        :param layers: warstwy projektu (testy); domyślnie wszystkie warstwy bieżącego projektu.
        :return: wynik albo ``None`` przy błędzie (komunikat w oknie).
        """
        try:
            result = refresh_forms(path, self.codes)
        except TemplateError as e:
            self._fail(str(e), self.tr("Nie udało się odświeżyć formularzy"))
            return None
        except Exception as e:  # noqa: BLE001 — komunikat trafia do użytkownika
            logger.exception("Nie udało się odświeżyć formularzy wzoru APP")
            self._fail(str(e), self.tr("Nie udało się odświeżyć formularzy"))
            return None
        if layers is None:
            from qgis.core import QgsProject

            layers = QgsProject.instance().mapLayers().values()
        in_project = configure_project_layers(path, layers, self.codes)
        name = Path(path).name
        text = self.tr("Odświeżono formularze w {0}: tabel {1}").format(name, len(result.tables))
        if in_project:
            text += self.tr(", warstw na mapie {0}").format(in_project)
        if result.fixed_count:
            text += self.tr(". Poprawiono wartości list wyboru (etykieta zamiast kodu): {0}").format(
                result.fixed_count)
        self.status.setText(text + ".")
        fixed = ", ".join(f"{table}.{field}: {count}" for (table, field), count in result.fixed.items())
        self._log(f"Formularze wzoru APP odświeżone: {path} (tabele: {', '.join(result.tables)}"
                  f"{'; poprawione wartości: ' + fixed if fixed else ''})")
        return result

    def _fail(self, message: str, context: str = "") -> None:
        self.status.setText(message)
        self._log(f"{context or 'Nie udało się utworzyć wzorcowego pliku APP'}: {message}", "BŁĄD")

    def _log(self, text: str, level: str = "INFO") -> None:
        if self._message is not None:
            self._message(text, level)
