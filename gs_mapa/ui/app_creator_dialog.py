"""Kreator nowego pliku GML APP 3.0: formularz aktu, dokumentów, rysunków, stref i regulacji.

Geometrie pochodzą z warstw projektu (zasięg aktu — wskazane obiekty warstwy; strefy
i regulacje — obiekty warstw z mapowaniem pól na atrybuty APP). Stan formularza można
zapisać do pliku szkicu (JSON) i wczytać później.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from qgis.PyQt.QtCore import QDate, QDateTime, QTime, Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qgis.core import Qgis, QgsApplication, QgsDistanceArea, QgsProject, QgsVectorLayer
from qgis.gui import QgsFieldComboBox, QgsFileWidget, QgsMapLayerComboBox, QgsMapToolIdentifyFeature

from ..core.act_index import ActIndexEntry
from ..core import app_checklist
from ..core.app_gml import ACT, DOCUMENT, DRAWING, OSDIS, OUZ, OZS, ZONE, AppGmlError
from ..core.app_namespace import ACT_TYPE_KINDS, NamespaceCandidate, jpt_code, namespace_candidates, parse_namespace
from ..core.app_rules import SUPPORTED_EPSG
from ..core.app_update import (
    ActReferenceError, ActSource, act_from_values, document_from_values, drawing_from_values, file_draft,
    parse_act_reference, read_act_source, resolve_act_id, update_draft, update_filename,
)
from ..core.app_defaults import document_defaults, effective_from, is_adopting
from ..core.app_field_help import tooltip as field_tooltip
from ..core.app_links import is_web
from ..core.app_versioning import reconcile_drawings, reconcile_objects
from ..core.app_writer import (
    CL, DATE_TYPES, DEFAULT_LEVEL, DOCUMENT_ROLES, LEVELS, NATURES, POG, ROLE_LABELS, STATUSES, Act, AppPlan,
    AppWriteError, BaseMap, Document, Drawing, Identity, PlanIssue, check_plan, load_codes, version_id_now,
    write_app30,
)
from ..core.units import Unit, allowed_act_types, find_unit_by_jpt, jpt_query, unit_query
from ..services.app_creator import (
    CLASS_FIELDS, CLASS_LABELS, COMMON_FIELDS, CodeResolver, LayerSource, ObjectDefaults, act_geometry,
    auto_mapping, is_polygon_layer, objects_from_layer,
)
from ..services.app_files import SOURCE_PROPERTY
from .app_namespace_dialog import AppNamespaceDialog
from .app_unit_dialog import ActPickerDialog
from . import theme

logger = logging.getLogger("GSMapa.AppCreator")

DRAFT_VERSION = 1
SPATIAL_KINDS = (ZONE, OUZ, OZS, OSDIS)
TAB_ACT, TAB_DOCUMENTS, TAB_DRAWINGS, TAB_OBJECTS = range(4)
#: Kolumna „Łącze” (odnośnik „Pobierz”) w tabelach dokumentów i rysunków.
_DOCUMENTS_LINK_COLUMN, _DRAWINGS_LINK_COLUMN = 5, 2
#: Role kolorów poziomów uwag (``theme``).
_COLORS = {"error": "err", "warning": "warn"}
_SEVERITY = {"error": "błąd", "warning": "ostrzeżenie", "info": "informacja"}
#: Naprawy, które kreator potrafi wykonać jednym kliknięciem (``PlanIssue.fix``) — opis dla przycisku.
FIX_LABELS = {
    "obsolete": "Ustaw status „nieaktualny” (obsolete)",
    "effective-to": "Ustaw „obowiązuje do” — z daty uchylenia albo dziś",
    "modification": "Ustaw modyfikacja=tak (akt obowiązuje w części obszaru)",
    "change-count": "Podnieś liczbę zmian",
    "drawing": "Dodaj rysunek aktu…",
    "add-document": "Dodaj dokument uchwalający…",
}


def _tail(uri: str) -> str:
    return uri.rstrip("/").rsplit("/", 1)[-1]


def _combo(items, editable: bool = False) -> QComboBox:
    combo = QComboBox()
    combo.setEditable(editable)
    for code, label in items:
        combo.addItem(label, code)
    return combo


def _set_combo(combo: QComboBox, code) -> None:
    index = combo.findData(code)
    if index >= 0:
        combo.setCurrentIndex(index)


def _show_code(combo: QComboBox, code) -> None:
    """
    Pokazuje wartość z pliku, także wtedy, gdy jej nie ma na liście kodowej.

    Kody spoza listy (np. ``status`` z APP 1.0) są dopisywane do pola, żeby podgląd pokazywał
    treść pliku, a nie wartość domyślną listy. Dopisanie zdarza się wyłącznie w oknie
    „Szczegóły”, gdzie nic nie jest zapisywane.
    """
    if code in (None, ""):
        return
    index = combo.findData(code)
    if index < 0:
        combo.addItem(str(code), code)
        index = combo.count() - 1
    combo.setCurrentIndex(index)


def _field_tooltip(widget: QWidget, layer: str, field: str) -> None:
    """
    Dopisuje do podpowiedzi pola opis z urzędowego schematu APP 3.0.

    :param widget: pole formularza (istniejąca podpowiedź zostaje na początku).
    :param layer: klasa APP (``ACT``, ``DOCUMENT``…).
    :param field: nazwa atrybutu APP (``core/app_layers.py``).
    """
    text = field_tooltip(layer, field)
    if not text:
        return
    current = widget.toolTip()
    widget.setToolTip(f"{current}\n\n{text}" if current else text)


def _scroll(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setWidget(widget)
    return area


class OptionalDate(QWidget):
    """Data opcjonalna: pole wyboru + kalendarz (``None`` gdy nie zaznaczono)."""

    def __init__(self, label: str = "", parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.check = QCheckBox(label)
        self.edit = QDateEdit(QDate.currentDate())
        self.edit.setCalendarPopup(True)
        self.edit.setDisplayFormat("yyyy-MM-dd")
        self.edit.setEnabled(False)
        layout.addWidget(self.check)
        layout.addWidget(self.edit)
        layout.addStretch(1)
        self.check.toggled.connect(self.edit.setEnabled)

    def value(self) -> Optional[str]:
        return self.edit.date().toString("yyyy-MM-dd") if self.check.isChecked() else None

    def set_value(self, value: Optional[str]) -> None:
        self.check.setChecked(bool(value))
        if value:
            self.edit.setDate(QDate.fromString(value, "yyyy-MM-dd"))


class OptionalMoment(QWidget):
    """Data i godzina opcjonalna (``xs:dateTime``; ``None``, gdy nie zaznaczono)."""

    def __init__(self, label: str = "", parent=None, midnight: bool = False):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.check = QCheckBox(label)
        self.edit = QDateTimeEdit(QDateTime.currentDateTime())
        self.edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.edit.setCalendarPopup(True)
        self.edit.setEnabled(False)
        layout.addWidget(self.check)
        layout.addWidget(self.edit)
        layout.addStretch(1)
        self._midnight = midnight
        self._updating = False
        self.check.toggled.connect(self._on_toggled)
        if self._midnight:
            self.edit.dateChanged.connect(self._snap_midnight)

    def _on_toggled(self, checked: bool) -> None:
        self.edit.setEnabled(checked)
        if checked and self._midnight and not self._updating:
            self._snap_midnight()

    def _snap_midnight(self, *args) -> None:
        if self._updating:
            return
        moment = self.edit.dateTime()
        if moment.time() != QTime(0, 0, 0):
            moment.setTime(QTime(0, 0, 0))
            self.edit.setDateTime(moment)

    def value(self) -> Optional[str]:
        return self.edit.dateTime().toUTC().toString("yyyy-MM-dd'T'HH:mm:ss'Z'") if self.check.isChecked() else None

    def set_value(self, value: Optional[str]) -> None:
        self._updating = True
        try:
            self.check.setChecked(bool(value))
            if value:
                moment = QDateTime.fromString(value, Qt.DateFormat.ISODate)
                if not moment.isValid():
                    moment = QDateTime.fromString(value.replace("Z", ""), Qt.DateFormat.ISODate)
                if moment.isValid():
                    self.edit.setDateTime(moment.toLocalTime())
        finally:
            self._updating = False


def _date_edit(value: Optional[str] = None) -> QDateEdit:
    edit = QDateEdit(QDate.fromString(value, "yyyy-MM-dd") if value else QDate.currentDate())
    edit.setCalendarPopup(True)
    edit.setDisplayFormat("yyyy-MM-dd")
    return edit


def _format_size(size: Optional[int]) -> str:
    """Rozmiar pliku do komunikatu pobierania (pusty, gdy serwer go nie podał)."""
    if not size:
        return ""
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{max(1, size // 1024)} kB"


# --- okna pomocnicze: dokument i rysunek ------------------------------------------


class DocumentDialog(QDialog):
    """Formularz dokumentu formalnego (uchwała, rozstrzygnięcie, wyrok…)."""

    def __init__(self, codes: dict, value: Optional[dict] = None, parent=None,
                 defaults: Optional[dict] = None, read_only: bool = False):
        """
        :param defaults: propozycje z jednostki (organ, dziennik, źródło) — w podglądzie
            nieużywane, bo pola mają pokazywać wyłącznie zawartość pliku.
        :param read_only: podgląd dokumentu: pola czytelne, bez edycji i bez „OK”.
        """
        super().__init__(parent)
        # Własny wygląd wtyczki (makiety docs/ui), niezależny od stylu Qt w QGIS.
        theme.apply_plugin_style(self)
        self._attempted = False
        #: Podgląd: żadne pole nie przyjmuje zmian, wyjściem jest „Zamknij”.
        self._read_only = bool(read_only)
        #: Propozycje z jednostki (``core.app_defaults.document_defaults``): organ, dziennik, źródło.
        self._defaults = defaults or {}
        #: Data, którą „data wejścia w życie” powtarza za datą uchwały (``None`` = powiązanie zerwane).
        self._auto_entry: Optional[str] = None
        #: Ochrona przed potraktowaniem programowego wpisu daty jako decyzji użytkownika.
        self._filling = False
        self.setWindowTitle(self.tr("Dokument formalny"))
        self.resize(560, 520)
        form = QFormLayout(self)
        self.role = _combo([(code, ROLE_LABELS[code]) for code in DOCUMENT_ROLES])
        self.role.setToolTip(self.tr(
            "Rola dokumentu względem aktu (np. uchwała uchwalająca → dokumentUchwalajacy). "
            "Uchylenie wymaga statusu „nieaktualny” i daty „obowiązuje do”; unieważnienie — "
            "modyfikacja=true w planie ogólnym albo podniesionej liczby zmian w innych aktach."))
        self.local_id = QLineEdit()
        self.local_id.setPlaceholderText(self.tr("np. U1 — unikalny w przestrzeni nazw aktu"))
        self.title = QLineEdit()
        self.short_name = QLineEdit()
        self.number = QLineEdit()
        self.number.setPlaceholderText(self.tr("np. XXXIX/334/22"))
        self.authority = QLineEdit()
        self.authority.setPlaceholderText(self.tr("np. Rada Gminy Testowo"))
        self.date = _date_edit()
        self.date_type = _combo(DATE_TYPES.items())
        self.entry = OptionalDate(self.tr("podaj"))
        self.repeal = OptionalDate(self.tr("podaj"))
        self.reference = QLineEdit()
        self.journal = _combo([("", self.tr("(brak)"))] + sorted(
            ((_tail(uri), label) for uri, label in codes.get("journals", {}).get("values", {}).items()),
            key=lambda item: item[1]))
        self.links = QPlainTextEdit()
        self.links.setPlaceholderText(self.tr("adresy URI, po jednym w wierszu"))
        self.links.setMaximumHeight(70)
        form.addRow(self.tr("Rola:"), self.role)
        form.addRow(self.tr("lokalnyId *:"), self.local_id)
        form.addRow(self.tr("Tytuł *:"), self.title)
        form.addRow(self.tr("Nazwa skrócona:"), self.short_name)
        form.addRow(self.tr("Numer:"), self.number)
        form.addRow(self.tr("Organ ustanawiający:"), self.authority)
        date_row = QHBoxLayout()
        date_row.addWidget(self.date)
        date_row.addWidget(self.date_type)
        date_row.addStretch(1)
        form.addRow(self.tr("Data *:"), date_row)
        form.addRow(self.tr("Data wejścia w życie:"), self.entry)
        form.addRow(self.tr("Data uchylenia:"), self.repeal)
        form.addRow(self.tr("Szczegółowe odniesienie:"), self.reference)
        form.addRow(self.tr("Dziennik urzędowy:"), self.journal)
        form.addRow(self.tr("Łącza:"), self.links)
        # Skąd wzięły się wypełnione pola (pkt „pokazanie źródła”) — tylko gdy coś dopisano.
        self.source_hint = QLabel("")
        self.source_hint.setWordWrap(True)
        theme.style_muted(self.source_hint)
        self.source_hint.setVisible(False)
        form.addRow("", self.source_hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        #: Pasek przycisków — w podglądzie zostaje jedno wyjście („Zamknij”).
        self.buttons = buttons
        self.local_id.textChanged.connect(self._mark)
        self.title.textChanged.connect(self._mark)
        self.date.dateChanged.connect(self._follow_date)
        self.role.currentIndexChanged.connect(self._follow_date)
        self.entry.check.toggled.connect(self._break_follow)
        self.entry.edit.dateChanged.connect(self._break_follow)
        for widget, field in ((self.role, "rola"), (self.local_id, "lokalnyId"), (self.title, "tytul"),
                              (self.short_name, "nazwaSkrocona"), (self.number, "numerIdentyfikacyjny"),
                              (self.authority, "organUstanawiajacy"), (self.date, "data"),
                              (self.date_type, "typDaty"), (self.entry, "dataWejsciaWZycie"),
                              (self.repeal, "dataUchylenia"), (self.reference, "szczegoloweOdniesienie"),
                              (self.journal, "dziennikUrzedowy"), (self.links, "lacze")):
            _field_tooltip(widget, DOCUMENT, field)
        if value:
            # Data wejścia w życie z pliku jest wartością źródłową — w podglądzie jej nie „poprawiamy”.
            filling, self._filling = self._filling, self._read_only
            try:
                self.set_value(value)
            finally:
                self._filling = filling
        if self._read_only:
            self._make_read_only()
        else:
            self._apply_defaults()
        self._mark()

    def _make_read_only(self) -> None:
        """Podgląd dokumentu: pola zostają czytelne (można kopiować), ale bez edycji."""
        self.setWindowTitle(self.tr("Dokument formalny — podgląd"))
        for widget in (self.local_id, self.title, self.short_name, self.number, self.authority,
                       self.reference, self.links):
            widget.setReadOnly(True)
        for widget in (self.role, self.date, self.date_type, self.entry, self.repeal, self.journal):
            widget.setEnabled(False)
        self.buttons.setStandardButtons(QDialogButtonBox.StandardButton.Close)

    def _apply_defaults(self) -> None:
        """
        Organ, dziennik i data wejścia w życie z jednostki oraz z daty uchwały.

        Wypełniane są wyłącznie puste pola — wartości z pliku i wpisane przez użytkownika
        zostają bez zmian. Podpis pod formularzem mówi, co przyszło z jednostki.
        """
        filled = []
        proposed = self._defaults.get("authority")
        if proposed and not self.authority.text().strip():
            self.authority.setText(proposed)
            filled.append(self.tr("organ ustanawiający"))
        code = self._defaults.get("journal")
        if code and not (self.journal.currentData() or ""):
            _set_combo(self.journal, code)
            if self.journal.currentData() == code:
                filled.append(self.tr("dziennik urzędowy"))
        notes = []
        if filled:
            notes.append(self.tr("Z jednostki ({0}): {1}.").format(
                self._defaults.get("source") or self.tr("jednostka"), ", ".join(filled)))
        self._follow_date()
        if self._auto_entry and is_adopting({"role": self.role.currentData()}):
            notes.append(self.tr("Data wejścia w życie z daty uchwały — możesz ją zmienić."))
        self.source_hint.setText(" ".join(notes))
        self.source_hint.setVisible(bool(notes))

    def _follow_date(self, *args) -> None:
        """„Data wejścia w życie” idzie za datą uchwały, dopóki użytkownik jej nie zmieni."""
        if self._filling:
            return
        if not is_adopting({"role": self.role.currentData()}):
            self._auto_entry = None
            return
        date = self.date.date().toString("yyyy-MM-dd")
        current = self.entry.value()
        if current and current != self._auto_entry:
            # Własna data użytkownika — nie nadpisujemy.
            self._auto_entry = None
            return
        self._filling = True
        try:
            self.entry.set_value(date)
        finally:
            self._filling = False
        self._auto_entry = date

    def _break_follow(self, *args) -> None:
        """Ręczna zmiana (albo wyłączenie) daty wejścia w życie zrywa powiązanie z uchwałą."""
        if not self._filling:
            self._auto_entry = None

    def _mark(self, *args) -> None:
        # Ramka błędu dopiero po edycji pola albo próbie zapisu — nie przy otwarciu okna.
        for widget in (self.local_id, self.title):
            show = self._attempted or widget.isModified()
            theme.mark_invalid(widget, show and not widget.text().strip())

    def _accept(self) -> None:
        self._attempted = True
        self._mark()
        if self.local_id.text().strip() and self.title.text().strip():
            self.accept()

    def value(self) -> dict:
        return {
            "role": self.role.currentData(), "local_id": self.local_id.text().strip(),
            "title": self.title.text().strip(), "short_name": self.short_name.text().strip() or None,
            "number": self.number.text().strip() or None, "authority": self.authority.text().strip() or None,
            "date": self.date.date().toString("yyyy-MM-dd"), "date_type": self.date_type.currentData(),
            "entry_into_force": self.entry.value(), "repeal_date": self.repeal.value(),
            "detailed_reference": self.reference.text().strip() or None,
            "journal": self.journal.currentData() or None,
            "links": [line.strip() for line in self.links.toPlainText().splitlines() if line.strip()],
        }

    def set_value(self, value: dict) -> None:
        _set_combo(self.role, value.get("role", "uchwala"))
        self.local_id.setText(value.get("local_id") or "")
        self.title.setText(value.get("title") or "")
        self.short_name.setText(value.get("short_name") or "")
        self.number.setText(value.get("number") or "")
        self.authority.setText(value.get("authority") or "")
        if value.get("date"):
            self.date.setDate(QDate.fromString(value["date"], "yyyy-MM-dd"))
        _set_combo(self.date_type, value.get("date_type", "creation"))
        self.entry.set_value(value.get("entry_into_force"))
        self.repeal.set_value(value.get("repeal_date"))
        self.reference.setText(value.get("detailed_reference") or "")
        _set_combo(self.journal, value.get("journal") or "")
        self.links.setPlainText("\n".join(value.get("links") or []))


class DrawingDialog(QDialog):
    """Formularz rysunku aktu (GeoTIFF części graficznej)."""

    def __init__(self, value: Optional[dict] = None, parent=None, read_only: bool = False):
        """
        :param read_only: podgląd rysunku: pola czytelne, bez edycji i bez „OK”.
        """
        super().__init__(parent)
        # Własny wygląd wtyczki (makiety docs/ui), niezależny od stylu Qt w QGIS.
        theme.apply_plugin_style(self)
        self._attempted = False
        #: Podgląd: żadne pole nie przyjmuje zmian, wyjściem jest „Zamknij”.
        self._read_only = bool(read_only)
        self.setWindowTitle(self.tr("Rysunek aktu planowania przestrzennego"))
        self.resize(560, 420)
        form = QFormLayout(self)
        self.local_id = QLineEdit()
        self.local_id.setPlaceholderText(self.tr("np. R1"))
        self.title = QLineEdit()
        self.link = QLineEdit()
        self.link.setPlaceholderText(self.tr("https://…/rysunek.tif — GeoTIFF z georeferencją"))
        self.legend = QLineEdit()
        self.crs = QLineEdit()
        self.crs.setPlaceholderText(self.tr("domyślnie układ pliku, np. EPSG:2177"))
        self.scale = QSpinBox()
        self.scale.setRange(1, 10_000_000)
        self.scale.setValue(1000)
        self.scale.setPrefix("1:")
        self.description = QLineEdit()
        self.version_end = OptionalMoment(self.tr("rysunek wycofany"), midnight=True)
        self.version_end.setToolTip(self.tr("koniecWersjiObiektu — gdy ta wersja rysunku przestaje "
                                           "obowiązywać; puste pole = wersja otwarta"))
        self.effective_from = OptionalDate(self.tr("inna niż aktu"))
        self.effective_to = OptionalDate(self.tr("podaj"))
        form.addRow(self.tr("lokalnyId *:"), self.local_id)
        form.addRow(self.tr("Tytuł *:"), self.title)
        form.addRow(self.tr("Łącze (GeoTIFF) *:"), self.link)
        form.addRow(self.tr("Legenda (URI):"), self.legend)
        form.addRow(self.tr("Układ odniesienia:"), self.crs)
        form.addRow(self.tr("Rozdzielczość (skala) *:"), self.scale)
        form.addRow(self.tr("Opis:"), self.description)
        form.addRow(self.tr("Koniec wersji obiektu:"), self.version_end)
        form.addRow(self.tr("Obowiązuje od:"), self.effective_from)
        form.addRow(self.tr("Obowiązuje do:"), self.effective_to)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        #: Pasek przycisków — w podglądzie zostaje jedno wyjście („Zamknij”).
        self.buttons = buttons
        for widget in (self.local_id, self.title, self.link):
            widget.textChanged.connect(self._mark)
        for widget, field in ((self.local_id, "lokalnyId"), (self.title, "tytul"), (self.link, "lacze"),
                              (self.legend, "legenda"), (self.crs, "ukladOdniesieniaPrzestrzennego"),
                              (self.scale, "rozdzielczoscPrzestrzenna"), (self.description, "opis"),
                              (self.version_end, "koniecWersjiObiektu"), (self.effective_from, "obowiazujeOd"),
                              (self.effective_to, "obowiazujeDo")):
            _field_tooltip(widget, DRAWING, field)
        self.version_end.check.toggled.connect(self._on_version_end_changed)
        self.version_end.edit.dateTimeChanged.connect(self._on_version_end_changed)
        if value:
            self.set_value(value)
        if self._read_only:
            self._make_read_only()
        self._mark()

    def _make_read_only(self) -> None:
        """Podgląd rysunku: pola zostają czytelne (można kopiować), ale bez edycji."""
        self.setWindowTitle(self.tr("Rysunek aktu — podgląd"))
        for widget in (self.local_id, self.title, self.link, self.legend, self.crs, self.description):
            widget.setReadOnly(True)
        for widget in (self.scale, self.version_end, self.effective_from, self.effective_to):
            widget.setEnabled(False)
        self.buttons.setStandardButtons(QDialogButtonBox.StandardButton.Close)

    def _on_version_end_changed(self, *args) -> None:
        """Koniec wersji rysunku wyznacza koniec obowiązywania (dzień wcześniej)."""
        if self.version_end.check.isChecked():
            end_date = self.version_end.edit.date()
            self.effective_to.set_value(end_date.addDays(-1).toString("yyyy-MM-dd"))

    def _required(self):
        return (self.local_id, self.title, self.link)

    def _mark(self, *args) -> None:
        # Ramka błędu dopiero po edycji pola albo próbie zapisu — nie przy otwarciu okna.
        for widget in self._required():
            show = self._attempted or widget.isModified()
            theme.mark_invalid(widget, show and not widget.text().strip())

    def _accept(self) -> None:
        self._attempted = True
        self._mark()
        if all(w.text().strip() for w in self._required()):
            self.accept()

    def value(self) -> dict:
        return {
            "local_id": self.local_id.text().strip(), "title": self.title.text().strip(),
            "link": self.link.text().strip(), "legend": self.legend.text().strip() or None,
            "crs": self.crs.text().strip() or None, "scale": self.scale.value(),
            "description": self.description.text().strip() or None,
            "version_end": self.version_end.value(),
            "effective_from": self.effective_from.value(), "effective_to": self.effective_to.value(),
        }

    def set_value(self, value: dict) -> None:
        self.local_id.setText(value.get("local_id") or "")
        self.title.setText(value.get("title") or "")
        self.link.setText(value.get("link") or "")
        self.legend.setText(value.get("legend") or "")
        self.crs.setText(value.get("crs") or "")
        self.scale.setValue(int(value.get("scale") or 1000))
        self.description.setText(value.get("description") or "")
        self.version_end.set_value(value.get("version_end"))
        self.effective_from.set_value(value.get("effective_from"))
        self.effective_to.set_value(value.get("effective_to"))


class _ObjectSourceWidget(QGroupBox):
    """Źródło jednej klasy obiektów (strefy, OUZ, OZS, OSDIS): warstwa i mapowanie pól."""

    def __init__(self, kind: str, parent=None):
        super().__init__(CLASS_LABELS[kind], parent)
        self.kind = kind
        self.setCheckable(True)
        self.setChecked(kind == ZONE)
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(Qgis.LayerFilter.PolygonLayer)
        self.layer_combo.setAllowEmptyLayer(True)
        self.layer_combo.setLayer(None)
        self.selected_only = QCheckBox(self.tr("Tylko zaznaczone"))
        self.btn_auto = QPushButton(self.tr("Dopasuj pola"))
        self.btn_auto.setToolTip(self.tr("Dopasowanie pól warstwy do atrybutów APP po nazwie"))
        row.addWidget(QLabel(self.tr("Warstwa:")))
        row.addWidget(self.layer_combo, 1)
        row.addWidget(self.selected_only)
        row.addWidget(self.btn_auto)
        layout.addLayout(row)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels([self.tr("Atrybut APP"), self.tr("Pole warstwy")])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.combos: Dict[str, QgsFieldComboBox] = {}
        for attr, label in COMMON_FIELDS + CLASS_FIELDS[kind]:
            row_index = self.table.rowCount()
            self.table.insertRow(row_index)
            item = QTableWidgetItem(label)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            item.setData(Qt.ItemDataRole.UserRole, attr)
            self.table.setItem(row_index, 0, item)
            combo = QgsFieldComboBox()
            combo.setAllowEmptyFieldName(True)
            self.table.setCellWidget(row_index, 1, combo)
            self.combos[attr] = combo
        self.table.setMinimumHeight(220)
        layout.addWidget(self.table)
        hint = QLabel(self.tr(
            "Puste pole = wartość wyprowadzona: symbol z rodzaju strefy, oznaczenie z numeru i symbolu "
            "(1SJ, 2SJ…), lokalnyId = <lokalnyId aktu>-<oznaczenie>, status / charakter / daty — domyślne."))
        hint.setWordWrap(True)
        theme.style_muted(hint)
        layout.addWidget(hint)
        self.layer_combo.layerChanged.connect(self._on_layer_changed)
        self.btn_auto.clicked.connect(self.auto_map)

    def layer(self):
        return self.layer_combo.currentLayer()

    def _on_layer_changed(self, layer) -> None:
        for combo in self.combos.values():
            combo.setLayer(layer)
        if layer is not None:
            self.auto_map()

    def auto_map(self) -> Dict[str, str]:
        layer = self.layer()
        if layer is None:
            return {}
        mapping = auto_mapping(self.kind, layer.fields().names())
        for attr, combo in self.combos.items():
            combo.setField(mapping.get(attr, ""))
        return mapping

    def mapping(self) -> Dict[str, str]:
        return {attr: combo.currentField() for attr, combo in self.combos.items() if combo.currentField()}

    def state(self) -> dict:
        layer = self.layer()
        return {"enabled": self.isChecked(), "layer_id": layer.id() if layer else None,
                "layer_name": layer.name() if layer else None, "selected_only": self.selected_only.isChecked(),
                "mapping": self.mapping()}

    def set_state(self, state: dict) -> None:
        self.setChecked(bool(state.get("enabled")))
        layer = _find_layer(state.get("layer_id"), state.get("layer_name"))
        self.layer_combo.setLayer(layer)
        self.selected_only.setChecked(bool(state.get("selected_only")))
        for attr, combo in self.combos.items():
            combo.setField((state.get("mapping") or {}).get(attr, ""))


def epsg_of(crs) -> Optional[int]:
    """Kod EPSG układu (``postgisSrid()`` zwraca 0 dla układów z GPKG — liczy się ``authid``)."""
    authid = crs.authid() if crs is not None else ""
    if authid.upper().startswith("EPSG:"):
        try:
            return int(authid.split(":", 1)[1])
        except ValueError:
            return None
    srid = crs.postgisSrid() if crs is not None else 0
    return srid or None


def _find_layer(layer_id: Optional[str], name: Optional[str]):
    project = QgsProject.instance()
    layer = project.mapLayer(layer_id) if layer_id else None
    if layer is None and name:
        matches = project.mapLayersByName(name)
        layer = matches[0] if matches else None
    return layer if is_polygon_layer(layer) else None


# --- okno kreatora --------------------------------------------------------------


class AppCreatorDialog(QDialog):
    """Kreator nowego pliku GML APP 3.0."""

    def __init__(self, parent=None, canvas: Optional[Callable] = None,
                 open_validation: Optional[Callable[[str], None]] = None,
                 add_file: Optional[Callable[[str], None]] = None,
                 message: Optional[Callable[[str, str], None]] = None, codes: Optional[dict] = None,
                 eziudp_service=None, namespace_service=None, unit: Optional[Unit] = None,
                 file_service=None, act_service=None, downloader=None, config=None,
                 link_service=None):
        super().__init__(parent)
        # Własny wygląd wtyczki (makiety docs/ui), niezależny od stylu Qt w QGIS.
        theme.apply_plugin_style(self)
        #: Pokazuj ramki pustych pól wymaganych (po „Sprawdź dane” / „Generuj GML”).
        self._show_required = False
        self.setWindowTitle(self.tr("Nowy plik GML APP 3.0"))
        self.setObjectName("GSMapaAppCreatorDialog")
        self.resize(980, 760)
        self._canvas = canvas
        self._open_validation = open_validation
        self._add_file = add_file
        self._message = message
        #: Usługa EZiUDP dla wyszukiwarki przestrzeni nazw (``None`` = własna instancja okna).
        self._eziudp_service = eziudp_service
        #: Usługa EZiUDP do samoczynnego doboru przestrzeni nazw (``None`` = ``eziudp_service``
        #: albo własna instancja tworzona przy pierwszym użyciu).
        self._lookup_service = namespace_service or eziudp_service
        self._lookup_connected = False
        #: Zbiory APP znane z EZiUDP: (JPT, rodzaj) -> kandydat; JPT już sprawdzone w EZiUDP.
        self._ns_cache: Dict[tuple, NamespaceCandidate] = {}
        self._ns_checked: set = set()
        self._ns_pending: Optional[str] = None
        #: Wczytywanie stanu (szkic, aktualizacja) — bez samoczynnej zmiany przestrzeni nazw.
        self._loading = False
        self._unit: Optional[Unit] = None
        #: Aktualizacja aktu: usługi (``None`` = własne instancje przy pierwszym użyciu) i stan.
        self._file_service = file_service
        self._own_files = None
        self._files_connected = False
        self._act_service = act_service
        self._downloader = downloader
        self._config = config
        self._act_entries: List[ActIndexEntry] = []
        self._acts_url_template: Optional[str] = None
        self._update: Optional[dict] = None
        #: Usługa sprawdzania łączy plików (``None`` = własna instancja przy pierwszym użyciu).
        self._link_service = link_service
        self._links_connected = False
        #: Uwagi z „Sprawdź dane” i z kontroli łączy (łącza wracają później, więc osobno).
        self._base_issues: List[PlanIssue] = []
        self._link_issues: List[PlanIssue] = []
        self._summary_plan: Optional[AppPlan] = None
        self._summary_report = None
        #: Akt odczytany ze źródła przy wczytaniu do aktualizacji (wersje obiektów, geometria aktu).
        self._update_source: Optional[ActSource] = None
        #: Wycięcie obszaru nowego planu (``ui/app_cut_dialog.AppliedCut``): nowy zasięg aktu
        #: i decyzje dla rysunków; ``build_plan`` bierze je zamiast warstw i formularza rysunków.
        self._cut = None
        self._update_pending: Optional[str] = None
        self._update_url: Optional[str] = None
        self._update_import: Optional[str] = None
        self._act_picker = None
        self._namespace_dialog = None
        self.codes = codes or load_codes()
        self._resolver = CodeResolver(self.codes)
        self._documents: List[dict] = []
        self._drawings: List[dict] = []
        #: Pobieranie plików z łączy dokumentów i rysunków (własny obiekt, niezależny od pobierania GML aktu).
        self._link_downloader = None
        #: Trwające pobrania łączy: adres → (zakładka, nazwa pliku, czy wczytać rysunek do mapy).
        self._pending_links: Dict[str, tuple] = {}
        #: Plik GeoPackage wczytany jako źródło danych („Wczytaj z pliku”) — tryb edycji w tabelach.
        self._table_source: Optional[str] = None
        self._table_mode = False
        #: Pola zablokowane w trybie tabel: widget -> (enabled, read_only, edit_triggers).
        self._table_locked: Dict[QWidget, tuple] = {}
        #: Okno w trybie „Szczegóły”: te same zakładki, ale bez edycji i bez zapisu.
        self._read_only = False
        self._details_name = ""
        #: Uwagi w kolejności listy komunikatów (dla naprawy zaznaczonej pozycji).
        self._shown_issues: List[PlanIssue] = []
        self._result_path: Optional[str] = None
        self._pick_tool = None
        self._previous_tool = None
        self._watched_layer = None

        layout = QVBoxLayout(self)
        # Jedna linia u góry okna; pełny opis specyfiki pliku w podpowiedzi (pkt 6.7, miejsce na formularz).
        self.intro = QLabel(self.tr("Nowy plik GML APP 3.0: geometrie z warstw projektu, atrybuty z formularza. "
                                    "Pola z * są wymagane."))
        self.intro.setWordWrap(True)
        self.intro.setToolTip(self.tr(
            "Plik GML aktu planowania przestrzennego zgodny ze specyfikacją APP 3.0 — nowy akt albo nowa wersja "
            "aktu z Rejestru Urbanistycznego (sekcja „Aktualizacja istniejącego aktu”). Geometrie pochodzą "
            "z warstw projektu, atrybuty z formularza (listy kodowe jak w specyfikacji). Pola oznaczone * są "
            "wymagane; przed zapisem dane są sprawdzane, a wynik można od razu zwalidować (XSD, reguły APP, "
            "geometria)."))
        self._intro_text = self.intro.text()
        self._intro_tooltip = self.intro.toolTip()
        layout.addWidget(self.intro)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self._splitter = splitter
        self.tabs = QTabWidget()
        self.tabs.addTab(_scroll(self._build_act_tab()), self.tr("Akt"))
        self.tabs.addTab(self._build_list_tab("documents"), self.tr("Dokumenty"))
        self.tabs.addTab(self._build_list_tab("drawings"), self.tr("Rysunki"))
        #: Zakładka źródła obiektów — ustawienie kreatora, więc w podglądzie jest ukrywana.
        self._objects_page = _scroll(self._build_objects_tab())
        self.tabs.addTab(self._objects_page, self.tr("Strefy i regulacje"))
        #: Tytuły zakładek bez liczników — liczniki braków dokłada ``_update_tab_titles``.
        self._tab_titles = [self.tabs.tabText(index) for index in range(self.tabs.count())]
        splitter.addWidget(self.tabs)
        self.issues = QTreeWidget()
        self.issues.setColumnCount(3)
        self.issues.setHeaderLabels([self.tr("Poziom"), self.tr("Miejsce"), self.tr("Komunikat")])
        self.issues.setRootIsDecorated(False)
        self.issues.setWordWrap(True)
        self.issues.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.issues.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.issues.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.issues.setColumnWidth(1, 240)
        theme.align_headers(self.issues)
        self.issues.setToolTip(self.tr("Kliknij dwukrotnie, aby przejść do zakładki."))
        self.issues.itemSelectionChanged.connect(self._refresh_fix_button)
        # Panel komunikatów: lista uwag i naprawa wybranej jednym kliknięciem.
        issues_panel = QWidget()
        panel_layout = QVBoxLayout(issues_panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        fix_row = QHBoxLayout()
        self.fix_hint = QLabel(self.tr("Zaznacz uwagę, aby ją poprawić jednym kliknięciem."))
        self.fix_hint.setWordWrap(True)
        theme.style_muted(self.fix_hint)
        self.btn_fix = QPushButton(self.tr("Popraw"))
        self.btn_fix.setEnabled(False)
        fix_row.addWidget(self.fix_hint, 1)
        fix_row.addWidget(self.btn_fix)
        panel_layout.addLayout(fix_row)
        panel_layout.addWidget(self.issues, 1)
        # Pusta lista komunikatów zajmowała miejsce bez informacji — pokazuje się z pierwszym wynikiem.
        issues_panel.setVisible(False)
        self._issues_panel = issues_panel
        splitter.addWidget(issues_panel)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        out_row = QHBoxLayout()
        self._output_label = QLabel(self.tr("Plik wynikowy *:"))
        out_row.addWidget(self._output_label)
        self.output = QgsFileWidget()
        self.output.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.output.setFilter(self.tr("GML (*.gml)"))
        self.output.setDialogTitle(self.tr("Zapisz plik GML APP 3.0"))
        out_row.addWidget(self.output, 1)
        layout.addLayout(out_row)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary)

        buttons = QHBoxLayout()
        self.btn_load = QPushButton(self.tr("Wczytaj szkic…"))
        self.btn_save = QPushButton(self.tr("Zapisz szkic…"))
        self.btn_check = QPushButton(self.tr("Sprawdź dane"))
        self.btn_generate = QPushButton(self.tr("Generuj GML"))
        self.btn_generate.setDefault(True)
        self.btn_validate = QPushButton(self.tr("Waliduj wynik…"))
        self.btn_validate.setToolTip(self.tr("Okno walidacji: XSD, reguły APP 3.0 i geometria"))
        self.btn_add = QPushButton(self.tr("Dodaj wynik do projektu"))
        self.btn_add.setToolTip(self.tr("Tabele aktu ze stylami wtyczki APP (GML zapisywany do GeoPackage)"))
        self.btn_close = QPushButton(self.tr("Zamknij"))
        for button in (self.btn_load, self.btn_save):
            buttons.addWidget(button)
        buttons.addStretch(1)
        for button in (self.btn_check, self.btn_generate, self.btn_validate, self.btn_add, self.btn_close):
            buttons.addWidget(button)
        layout.addLayout(buttons)

        self.btn_load.clicked.connect(self._on_load_clicked)
        self.btn_save.clicked.connect(self._on_save_clicked)
        self.btn_check.clicked.connect(self.check)
        self.btn_generate.clicked.connect(self.generate)
        self.btn_validate.clicked.connect(self.validate_result)
        self.btn_add.clicked.connect(self.add_result)
        self.btn_close.clicked.connect(self.close)
        self.btn_fix.clicked.connect(self.fix_selected)
        self.check_table_mode.toggled.connect(self.set_table_mode)
        self.btn_reload.clicked.connect(self.refresh_from_file)
        self.issues.itemDoubleClicked.connect(self._on_issue_double_clicked)
        self.output.fileChanged.connect(self._refresh)
        self.new_version()
        self.set_unit(unit)
        self._on_type_changed()
        self._refresh()

    # --- budowa formularza ------------------------------------------------------

    def _build_act_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        update = QGroupBox(self.tr("Aktualizacja istniejącego aktu"))
        update_layout = QVBoxLayout(update)
        update_row = QHBoxLayout()
        self.update_ref = QLineEdit()
        self.update_ref.setPlaceholderText(self.tr("adres aktu w Rejestrze Urbanistycznym albo identyfikator, np. "
                                                   "https://rejestr-urbanistyczny.gov.pl/published/details/mpzp/10"))
        self.update_ref.setToolTip(self.tr("Akt zostanie pobrany z Rejestru (plik GML), formularz wypełniony jego "
                                           "danymi, a warstwy aktu dodane do projektu — zapis tworzy nową wersję "
                                           "aktu (ten sam lokalnyId, nowa wersjaId)"))
        self.btn_update_load = QPushButton(self.tr("Wczytaj"))
        self.btn_update_find = QPushButton(self.tr("Znajdź akt…"))
        self.btn_update_find.setToolTip(self.tr("Wyszukaj akt jednostki w indeksie aktów Rejestru Urbanistycznego"))
        self.btn_update_file = QPushButton(self.tr("Z pliku GML…"))
        self.btn_update_file.setToolTip(self.tr("Aktualizacja aktu z pliku GML APP na dysku"))
        update_row.addWidget(self.update_ref, 1)
        for button in (self.btn_update_load, self.btn_update_find, self.btn_update_file):
            update_row.addWidget(button)
        update_layout.addLayout(update_row)
        self.update_info = QLabel(self.tr("Nowy akt. Aby przygotować nową wersję aktu z Rejestru, wskaż go powyżej."))
        self.update_info.setWordWrap(True)
        self.update_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        theme.style_muted(self.update_info)
        update_layout.addWidget(self.update_info)
        previous_row = QHBoxLayout()
        self.previous_output = QLineEdit()
        self.previous_output.setToolTip(self.tr("Plik poprzedniej wersji aktu z domkniętymi wersjami obiektów "
                                                "(koniecWersjiObiektu) — pusty: nie zapisuj drugiego pliku"))
        self.previous_output.setPlaceholderText(self.tr("plik poprzedniej wersji (domkniętej) — nadpisywany przy "
                                                        "każdym zapisie"))
        previous_row.addWidget(QLabel(self.tr("Poprzednia wersja:")))
        previous_row.addWidget(self.previous_output, 1)
        update_layout.addLayout(previous_row)
        cut_row = QHBoxLayout()
        self.btn_cut = QPushButton(self.tr("Wytnij obszar nowego planu…"))
        self.btn_cut.setToolTip(self.tr(
            "Nowa granica aktu po wycięciu obszaru przejętego przez nowy plan: dziura, wnęka albo podział "
            "na części oraz przycięte rysunki (GeoTIFF). Wymaga wczytanego aktu do aktualizacji."))
        self.cut_info = QLabel("")
        self.cut_info.setWordWrap(True)
        theme.style_muted(self.cut_info)
        cut_row.addWidget(self.btn_cut)
        cut_row.addWidget(self.cut_info, 1)
        update_layout.addLayout(cut_row)
        self.btn_cut.clicked.connect(self._open_cut)
        #: Sekcja aktualizacji aktu — w podglądzie („Szczegóły”) ukrywana.
        self._update_box = update
        layout.addWidget(update)

        template = QGroupBox(self.tr("Wypełnij z pliku GeoPackage"))
        #: Sekcja z odczytem/wczytywaniem pliku — jedyne pole aktywne w trybie edycji w tabelach.
        self._template_box = template
        template_layout = QVBoxLayout(template)
        template_row = QHBoxLayout()
        template_hint = QLabel(self.tr(
            "Akt, dokumenty i rysunki z wypełnionego wzorcowego pliku GPKG — bez przepisywania. "
            "Strefy i regulacje biorą się z warstw projektu."))
        template_hint.setWordWrap(True)
        theme.style_muted(template_hint)
        self.btn_template = QPushButton(self.tr("Z pliku wzorcowego…"))
        self.btn_template.setToolTip(self.tr(
            "Wczytaj akt, dokumenty i rysunki z tabel APP w pliku GeoPackage; warstwy tego pliku, "
            "jeśli są w projekcie, zostaną podpięte jako zasięg i źródła stref."))
        template_row.addWidget(template_hint, 1)
        template_row.addWidget(self.btn_template)
        template_layout.addLayout(template_row)
        table_row = QHBoxLayout()
        self.check_table_mode = QCheckBox(self.tr("Edytuję w tabelach GeoPackage"))
        self.check_table_mode.setToolTip(self.tr(
            "Akt, dokumenty i rysunki są wtedy tylko do odczytu: zmieniasz je w tabelach QGIS, "
            "a „Odśwież z pliku” czyta je ponownie. Strefy i regulacje i tak pochodzą z warstw."))
        self.btn_reload = QPushButton(self.tr("Odśwież z pliku"))
        self.btn_reload.setToolTip(self.tr("Przeczytaj ponownie akt, dokumenty i rysunki z pliku GeoPackage"))
        self.btn_reload.setEnabled(False)
        table_row.addWidget(self.check_table_mode, 1)
        table_row.addWidget(self.btn_reload)
        template_layout.addLayout(table_row)
        self.template_info = QLabel("")
        self.template_info.setWordWrap(True)
        self.template_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        theme.style_muted(self.template_info)
        self.template_info.setVisible(False)
        template_layout.addWidget(self.template_info)
        layout.addWidget(template)

        ident = QGroupBox(self.tr("Identyfikator IIP"))
        form = QFormLayout(ident)
        self.unit_label = QLabel("")
        self.unit_label.setWordWrap(True)
        self.unit_label.setToolTip(self.tr(
            "Jednostkę wybiera się na górze panelu GS Mapa („Wybierz…” albo „Zmień…”). Gmina i "
            "województwo zawężają typy aktów i podpowiadają przestrzeń nazw; powiat nie jest tu używany."))
        form.addRow(self.tr("Jednostka:"), self.unit_label)
        self.namespace = QLineEdit()
        self.namespace.setPlaceholderText("PL.ZIPPZP.<numer>/<TERYT>-<POG|MPZP|PZPW…>, np. PL.ZIPPZP.9999/146501-POG")
        self.namespace.setToolTip(self.tr("Przestrzeń nazw zbioru danych (wspólna dla wszystkich obiektów pliku)"))
        namespace_row = QHBoxLayout()
        self.btn_namespace = QPushButton(self.tr("Szukaj w EZiUDP…"))
        self.btn_namespace.setToolTip(self.tr("Wyszukaj zbiór APP jednostki w Ewidencji Zbiorów i Usług Danych "
                                              "Przestrzennych i wstaw jego przestrzeń nazw"))
        namespace_row.addWidget(self.namespace, 1)
        namespace_row.addWidget(self.btn_namespace)
        self.local_id = QLineEdit()
        self.local_id.setPlaceholderText(self.tr("np. 1POG"))
        version_row = QHBoxLayout()
        self.version_id = QLineEdit()
        self.version_id.setToolTip(self.tr("wersjaId — wspólny dla aktu, rysunków, stref i regulacji"))
        self.btn_now = QPushButton(self.tr("Nowa wersja (teraz)"))
        self.btn_now.setToolTip(self.tr("wersjaId i początek wersji = bieżący czas (UTC)"))
        version_row.addWidget(self.version_id, 1)
        version_row.addWidget(self.btn_now)
        self.version_start = QDateTimeEdit(QDateTime.currentDateTime())
        self.version_start.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.version_start.setCalendarPopup(True)
        self.version_start.setToolTip(self.tr("Czas lokalny; w pliku zapisywany w UTC"))
        form.addRow(self.tr("Przestrzeń nazw *:"), namespace_row)
        self.namespace_hint = QLabel("")
        self.namespace_hint.setWordWrap(True)
        theme.style_muted(self.namespace_hint)
        self.namespace_hint.setVisible(False)
        form.addRow("", self.namespace_hint)
        form.addRow(self.tr("lokalnyId *:"), self.local_id)
        form.addRow(self.tr("wersjaId *:"), version_row)
        form.addRow(self.tr("Początek wersji obiektu *:"), self.version_start)
        self.version_end = OptionalMoment(self.tr("akt zamknięty"), midnight=True)
        self.version_end.setToolTip(self.tr(
            "koniecWersjiObiektu — wypełnij, gdy ta wersja aktu przestaje obowiązywać "
            "(uchylenie, wygaśnięcie); puste pole = wersja otwarta"))
        form.addRow(self.tr("Koniec wersji obiektu:"), self.version_end)
        layout.addWidget(ident)

        attrs = QGroupBox(self.tr("Atrybuty aktu"))
        form = QFormLayout(attrs)
        self.title = QLineEdit()
        self.alt_titles = QLineEdit()
        self.alt_titles.setPlaceholderText(self.tr("opcjonalnie; kilka rozdziel znakiem |"))
        types = self.codes["act_types"]["values"]
        #: Wszystkie typy aktów (kod, etykieta); lista w polu zależy od jednostki.
        self._all_types = sorted(((_tail(uri), label) for uri, label in types.items()), key=lambda i: i[1])
        self.plan_type = _combo(self._all_types)
        _set_combo(self.plan_type, POG)
        self.level = _combo(LEVELS.items())
        self.status = _combo(STATUSES.items())
        _set_combo(self.status, "legalForce")
        self.effective_from = _date_edit()
        #: Data, którą „Obowiązuje od” przyjmuje z uchwały; inna wartość = data użytkownika.
        self._auto_effective = self.effective_from.date().toString("yyyy-MM-dd")
        self.effective_to = OptionalDate(self.tr("podaj"))
        self.modification = _combo([(False, self.tr("nie — obowiązuje w całości")),
                                    (True, self.tr("tak — obowiązuje w części"))])
        self.modification.setToolTip(self.tr("Tylko plan ogólny: czy wersja obowiązuje w części obszaru (OCL ACT-06)"))
        change_row = QHBoxLayout()
        self.change_check = QCheckBox(self.tr("podaj"))
        self.change_count = QSpinBox()
        self.change_count.setRange(0, 999)
        self.change_count.setEnabled(False)
        change_row.addWidget(self.change_check)
        change_row.addWidget(self.change_count)
        change_row.addStretch(1)
        self.changed_plan = QLineEdit()
        self.changed_plan.setPlaceholderText(self.tr("URI aktu zmienianego, np. https://www.gov.pl/zagospodarowanieprzestrzenne/app/AktPlanowaniaPrzestrzennego/…"))
        self.changed_plan.setToolTip(self.tr(
            "Zmieniany plan (adres aktu, nie innego obiektu). Zmieniany plan nie może być w opracowaniu "
            "ani w trakcie przyjmowania — sprawdź jego status w Rejestrze Urbanistycznym."))
        form.addRow(self.tr("Tytuł *:"), self.title)
        form.addRow(self.tr("Tytuł alternatywny:"), self.alt_titles)
        form.addRow(self.tr("Typ aktu *:"), self.plan_type)
        form.addRow(self.tr("Poziom w hierarchii *:"), self.level)
        form.addRow(self.tr("Status *:"), self.status)
        form.addRow(self.tr("Obowiązuje od *:"), self.effective_from)
        # Skąd data, gdy przyszła z uchwały (pole zostaje edytowalne).
        self.effective_hint = QLabel("")
        self.effective_hint.setWordWrap(True)
        theme.style_muted(self.effective_hint)
        self.effective_hint.setVisible(False)
        form.addRow("", self.effective_hint)
        form.addRow(self.tr("Obowiązuje do:"), self.effective_to)
        self._modification_label = QLabel(self.tr("Modyfikacja *:"))
        form.addRow(self._modification_label, self.modification)
        self._change_label = QLabel(self.tr("Liczba zmian:"))
        form.addRow(self._change_label, change_row)
        form.addRow(self.tr("Zmieniany plan:"), self.changed_plan)
        layout.addWidget(attrs)

        maps = QGroupBox(self.tr("Mapy podkładowe"))
        maps_layout = QVBoxLayout(maps)
        self.base_maps = QTableWidget(0, 3)
        self.base_maps.setHorizontalHeaderLabels([self.tr("Data (RRRR-MM-DD)"), self.tr("Referencja"),
                                                  self.tr("Łącze")])
        self.base_maps.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.base_maps.setColumnWidth(0, 150)
        self.base_maps.setMaximumHeight(120)
        maps_layout.addWidget(self.base_maps)
        maps_buttons = QHBoxLayout()
        self.btn_map_add = QPushButton(self.tr("Dodaj mapę"))
        self.btn_map_remove = QPushButton(self.tr("Usuń"))
        maps_buttons.addWidget(self.btn_map_add)
        maps_buttons.addWidget(self.btn_map_remove)
        maps_buttons.addStretch(1)
        maps_layout.addLayout(maps_buttons)
        layout.addWidget(maps)

        extent = QGroupBox(self.tr("Zasięg przestrzenny (granica aktu)"))
        form = QFormLayout(extent)
        layer_row = QHBoxLayout()
        self.extent_layer = QgsMapLayerComboBox()
        self.extent_layer.setFilters(Qgis.LayerFilter.PolygonLayer)
        self.extent_layer.setAllowEmptyLayer(True)
        self.extent_layer.setLayer(None)
        self.btn_pick = QPushButton(self.tr("Wskaż na mapie"))
        self.btn_pick.setCheckable(True)
        self.btn_pick.setToolTip(self.tr("Klikaj obiekty warstwy na mapie, aby je zaznaczyć / odznaczyć"))
        self.btn_pick.setEnabled(self._canvas is not None)
        layer_row.addWidget(self.extent_layer, 1)
        layer_row.addWidget(self.btn_pick)
        self.extent_selected = QCheckBox(self.tr("Tylko zaznaczone obiekty"))
        self.extent_dissolve = QCheckBox(self.tr("Scal obiekty w jedną granicę"))
        self.extent_dissolve.setChecked(True)
        self.extent_info = QLabel("")
        crs_row = QHBoxLayout()
        self.crs = _combo([(epsg, f"EPSG:{epsg}") for epsg in SUPPORTED_EPSG])
        _set_combo(self.crs, 2180)
        self.precision = QSpinBox()
        self.precision.setRange(0, 9)
        self.precision.setValue(8)
        self.precision.setToolTip(self.tr("Zaokrąglenie do mniejszej liczby miejsc niż w danych źródłowych może "
                                          "zepsuć geometrię (samoprzecięcia, luki między strefami)"))
        self.precision.setSuffix(self.tr(" miejsc po przecinku"))
        crs_row.addWidget(self.crs)
        crs_row.addWidget(QLabel(self.tr("dokładność:")))
        crs_row.addWidget(self.precision)
        crs_row.addStretch(1)
        form.addRow(self.tr("Warstwa *:"), layer_row)
        form.addRow("", self.extent_selected)
        form.addRow("", self.extent_dissolve)
        form.addRow("", self.extent_info)
        form.addRow(self.tr("Układ pliku *:"), crs_row)
        layout.addWidget(extent)
        layout.addStretch(1)

        for widget in (self.namespace, self.local_id, self.version_id, self.title):
            widget.textChanged.connect(self._refresh)
        # Podpowiedzi z urzędowego schematu APP 3.0 (``core/app_field_help``).
        for widget, field in ((self.namespace, "przestrzenNazw"), (self.local_id, "lokalnyId"),
                              (self.version_id, "wersjaId"), (self.version_start, "poczatekWersjiObiektu"),
                              (self.version_end, "koniecWersjiObiektu"), (self.title, "tytul"),
                              (self.alt_titles, "tytulAlternatywny"), (self.plan_type, "typPlanu"),
                              (self.level, "poziomHierarchii"), (self.status, "status"),
                              (self.effective_from, "obowiazujeOd"), (self.effective_to, "obowiazujeDo"),
                              (self.modification, "modyfikacja"), (self.change_count, "zmiana")):
            _field_tooltip(widget, ACT, field)
        self.btn_now.clicked.connect(self.new_version)
        self.version_end.check.toggled.connect(self._on_version_end_changed)
        self.version_end.edit.dateTimeChanged.connect(self._on_version_end_changed)
        self.btn_namespace.clicked.connect(self.find_namespace)
        self.btn_update_load.clicked.connect(self._on_update_load_clicked)
        self.update_ref.returnPressed.connect(self._on_update_load_clicked)
        self.btn_update_find.clicked.connect(self.find_update_act)
        self.btn_update_file.clicked.connect(self._on_update_file_clicked)
        self.btn_template.clicked.connect(self._on_template_clicked)
        self.plan_type.currentIndexChanged.connect(self._on_type_changed)
        self.change_check.toggled.connect(self.change_count.setEnabled)
        self.btn_map_add.clicked.connect(self._add_base_map)
        self.btn_map_remove.clicked.connect(self._remove_base_map)
        self.extent_layer.layerChanged.connect(self._on_extent_layer_changed)
        self.extent_selected.toggled.connect(self._update_extent_info)
        self.btn_pick.toggled.connect(self._toggle_pick)
        return page

    def _build_list_tab(self, kind: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        if kind == "documents":
            headers = [self.tr("Rola"), self.tr("lokalnyId"), self.tr("Tytuł"), self.tr("Numer"),
                       self.tr("Data"), self.tr("Łącze")]
            hint = self.tr("Akt musi wskazywać dokument przystępujący albo uchwalający; akt obowiązujący — "
                           "dokładnie jeden dokument uchwalający (OCL ACT-01, ACT-02).")
            #: Kolumna rozciągana to „Tytuł” (adres łącza zostaje w wąskiej kolumnie z podpowiedzią).
            stretch = 2
        else:
            headers = [self.tr("lokalnyId"), self.tr("Tytuł"), self.tr("Łącze"), self.tr("Skala")]
            hint = self.tr("Rysunek (GeoTIFF z georeferencją) jest wymagany w obowiązującym akcie bez stref "
                           "i regulacji (OCL ACT-07). wersjaId i obowiązuje od — jak w akcie.")
            stretch = 1
        label = QLabel(hint)
        label.setWordWrap(True)
        theme.style_muted(label)
        layout.addWidget(label)
        #: Podpowiedź nad tabelą — w podglądzie („Szczegóły”) mówi, jak obejrzeć jeden dokument/rysunek.
        if kind == "documents":
            self._documents_hint = label
        else:
            self._drawings_hint = label
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
        theme.align_headers(table)
        layout.addWidget(table, 1)
        row = QHBoxLayout()
        add, edit, remove = (QPushButton(self.tr("Dodaj…")), QPushButton(self.tr("Edytuj…")),
                             QPushButton(self.tr("Usuń")))
        #: Pobranie pliku z łącza czyta tylko dane pliku, więc działa też w podglądzie („Szczegóły”).
        download = QPushButton(self.tr("Pobierz…"))
        for button in (add, edit, remove, download):
            row.addWidget(button)
        map_button = None
        if kind == "drawings":
            map_button = QPushButton(self.tr("Dodaj do mapy"))
            row.addWidget(map_button)
        row.addStretch(1)
        layout.addLayout(row)
        #: Komunikat pobierania pod przyciskami: postęp, zapisana ścieżka, błąd sieci.
        status = QLabel("")
        status.setWordWrap(True)
        status.setVisible(False)
        theme.style_muted(status)
        layout.addWidget(status)
        # „Edytuj…” i „Usuń” działają na zaznaczonym wierszu — bez zaznaczenia są nieaktywne z powodem.
        table.itemSelectionChanged.connect(self._refresh_list_buttons)
        if kind == "documents":
            self.documents_table = table
            self._documents_edit, self._documents_remove = edit, remove
            self._documents_download, self._documents_map = download, None
            self._documents_status = status
            add.clicked.connect(self._add_document)
            edit.clicked.connect(self._edit_document)
            remove.clicked.connect(self._remove_document)
            download.clicked.connect(self._on_documents_download_clicked)
            table.itemDoubleClicked.connect(self._edit_document)
        else:
            self.drawings_table = table
            self._drawings_edit, self._drawings_remove = edit, remove
            self._drawings_download, self._drawings_map = download, map_button
            self._drawings_status = status
            add.clicked.connect(self._add_drawing)
            edit.clicked.connect(self._edit_drawing)
            remove.clicked.connect(self._remove_drawing)
            download.clicked.connect(self._on_drawings_download_clicked)
            map_button.clicked.connect(self._on_drawings_map_clicked)
            table.itemDoubleClicked.connect(self._edit_drawing)
        self._refresh_list_buttons()
        return page

    def _build_objects_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.objects_hint = QLabel(self.tr(
            "Strefy planistyczne i regulacje (OUZ, OZS, OSDIS) występują w planie ogólnym gminy. "
            "Każdy obiekt warstwy staje się jednym obiektem APP; geometria musi być pojedynczym wielobokiem."))
        self.objects_hint.setWordWrap(True)
        layout.addWidget(self.objects_hint)
        numbering = QHBoxLayout()
        self.numbering_hint = QLabel(self.tr("Oznaczenia (np. 1SJ, 2SJ) muszą być unikalne w akcie."))
        self.numbering_hint.setWordWrap(True)
        theme.style_muted(self.numbering_hint)
        self.btn_numbering = QPushButton(self.tr("Przenumeruj oznaczenia…"))
        self.btn_numbering.setToolTip(self.tr(
            "Znajdź w warstwach powtórzone i puste oznaczenia i zaproponuj nowe numery (zapis w warstwie)"))
        self.btn_numbering.setEnabled(False)
        numbering.addWidget(self.numbering_hint, 1)
        numbering.addWidget(self.btn_numbering)
        layout.addLayout(numbering)
        defaults = QGroupBox(self.tr("Wartości domyślne (gdy pole nie jest wskazane)"))
        form = QFormLayout(defaults)
        self.objects_status = _combo(STATUSES.items())
        _set_combo(self.objects_status, "legalForce")
        self.objects_nature = _combo(NATURES.items())
        _set_combo(self.objects_nature, "generallyBinding")
        form.addRow(self.tr("Status:"), self.objects_status)
        form.addRow(self.tr("Charakter ustalenia:"), self.objects_nature)
        form.addRow(QLabel(self.tr("wersjaId, początek wersji i obowiązuje od/do — z aktu.")))
        layout.addWidget(defaults)
        self.sources: Dict[str, _ObjectSourceWidget] = {}
        for kind in SPATIAL_KINDS:
            widget = _ObjectSourceWidget(kind)
            self.sources[kind] = widget
            layout.addWidget(widget)
        layout.addStretch(1)
        self.btn_numbering.clicked.connect(self._on_numbering_clicked)
        return page

    # --- reakcje formularza ------------------------------------------------------

    def _log(self, text: str, level: str = "INFO") -> None:
        if self._message is not None:
            self._message(text, level)

    def find_namespace(self, *args):
        """
        Okno wyszukiwania przestrzeni nazw w EZiUDP (niemodalne); zatwierdzony wybór trafia do pola.

        :return: okno wyszukiwania (``AppNamespaceDialog``).
        """
        if self._namespace_dialog is None:
            self._namespace_dialog = AppNamespaceDialog(self._eziudp_service, parent=self)
            self._namespace_dialog.accepted.connect(self._on_namespace_accepted)
        if self._unit is not None and not self.namespace.text().strip() and not self._namespace_dialog.teryt.text():
            self._namespace_dialog.teryt.setText(self._unit.jpt)
        self._namespace_dialog.prepare(self.plan_type.currentData(), self.namespace.text())
        self._namespace_dialog.show()
        self._namespace_dialog.raise_()
        self._namespace_dialog.activateWindow()
        return self._namespace_dialog

    def _on_namespace_accepted(self) -> None:
        candidate = self._namespace_dialog.selected() if self._namespace_dialog is not None else None
        if candidate is None:
            return
        self.namespace.setText(candidate.namespace)
        self._remember(candidate)
        record = candidate.record
        self._log(f"Przestrzeń nazw z EZiUDP: {candidate.namespace} ({record.name} — {record.area})")

    # --- jednostka i samoczynna przestrzeń nazw ---------------------------------------

    def unit(self) -> Optional[Unit]:
        return self._unit

    def set_unit(self, unit: Optional[Unit]) -> None:
        """
        Jednostka kreatora (z paska kontekstu panelu): zawęża typy aktów (województwo — tylko PZPW)
        i wyznacza zbiór EZiUDP, z którego pochodzi przestrzeń nazw.
        """
        self._unit = unit
        if unit is None:
            self.unit_label.setText(self.tr("nie wybrano gminy ani województwa (pasek na górze panelu GS Mapa) — "
                                            "wszystkie typy aktów"))
        else:
            self.unit_label.setText(unit.label)
        allowed = allowed_act_types(unit, [code for code, _ in self._all_types])
        current = self.plan_type.currentData()
        self.plan_type.blockSignals(True)
        self.plan_type.clear()
        for code, label in self._all_types:
            if code in allowed:
                self.plan_type.addItem(label, code)
        index = self.plan_type.findData(current)
        if index < 0:
            index = self.plan_type.findData(POG)
        self.plan_type.setCurrentIndex(max(index, 0))
        self.plan_type.blockSignals(False)
        if self.plan_type.currentData() != current:
            self._on_type_changed()
        elif not self._loading:
            self.auto_namespace()

    def _lookup(self):
        """Usługa EZiUDP doboru przestrzeni nazw (tworzona i podłączana przy pierwszym użyciu)."""
        if self._lookup_service is None:
            from ..services.eziudp import EziudpService

            self._lookup_service = EziudpService()
            self._lookup_service.setParent(self)
        if not self._lookup_connected:
            self._lookup_service.search_finished.connect(self._on_lookup_finished)
            self._lookup_service.search_failed.connect(self._on_lookup_failed)
            self._lookup_connected = True
        return self._lookup_service

    def _remember(self, candidate) -> None:
        jpt = jpt_code(candidate.record.teryt)
        if jpt:
            self._ns_cache.setdefault((jpt, candidate.kind), candidate)

    def _set_namespace_hint(self, text: str, warning: bool = False) -> None:
        self.namespace_hint.setText(text)
        theme.set_text_role(self.namespace_hint, "warn" if warning else "muted")
        self.namespace_hint.setVisible(bool(text))

    def _target_jpt(self) -> Optional[str]:
        if self._unit is not None:
            return self._unit.jpt
        parts = parse_namespace(self.namespace.text())
        return parts[1] if parts else None

    def auto_namespace(self) -> Optional[str]:
        """
        Identyfikator zbioru (przestrzeń nazw) właściwy dla typu aktu i jednostki.

        Gdy bieżąca przestrzeń nazw należy do innego rodzaju zbioru (np. POG, a typ aktu to MPZP)
        albo do innej jednostki, jest zastępowana zbiorem jednostki z EZiUDP — z pamięci albo
        po zapytaniu (w tle). Jednostka: ustawiona w zakładce albo JPT z bieżącej przestrzeni nazw.

        :return: wstawiona przestrzeń nazw albo ``None`` (bez zmiany / trwa wyszukiwanie).
        """
        kind = ACT_TYPE_KINDS.get(self.plan_type.currentData())
        jpt = self._target_jpt()
        if not kind or not jpt:
            self._set_namespace_hint("")
            return None
        parts = parse_namespace(self.namespace.text())
        if parts and parts[1] == jpt and parts[2] == kind:
            return None
        candidate = self._ns_cache.get((jpt, kind))
        if candidate is not None:
            self.namespace.setText(candidate.namespace)
            self._set_namespace_hint(self.tr("Identyfikator zbioru {0} z EZiUDP ({1}).").format(
                kind, candidate.record.area))
            self._log(f"Kreator APP: przestrzeń nazw {candidate.namespace} dla typu {kind} (EZiUDP)")
            return candidate.namespace
        if jpt in self._ns_checked:
            self._set_namespace_hint(self.tr(
                "W EZiUDP nie ma zbioru {0} jednostki {1} — wpisz przestrzeń nazw ręcznie albo wyszukaj "
                "zbiór („Szukaj w EZiUDP…”).").format(kind, jpt), warning=True)
            return None
        query = unit_query(self._unit) if self._unit is not None else jpt_query(jpt)
        if query is None:
            return None
        if self._ns_pending != jpt:
            self._ns_pending = jpt
            self._lookup().search(query)
        self._set_namespace_hint(self.tr("Szukam zbioru {0} jednostki {1} w EZiUDP…").format(kind, jpt))
        return None

    def _on_lookup_finished(self, result) -> None:
        candidates, _ = namespace_candidates(result, None)
        for candidate in candidates:
            self._remember(candidate)
        if self._ns_pending is None:
            return
        self._ns_checked.add(self._ns_pending)
        self._ns_pending = None
        if not self._loading:
            self.auto_namespace()

    def _on_lookup_failed(self, message: str) -> None:
        if self._ns_pending is None:
            return
        self._ns_pending = None
        self._set_namespace_hint(self.tr("EZiUDP: {0}").format(message), warning=True)
        self._log(f"Kreator APP: przestrzeń nazw z EZiUDP — {message}", "UWAGA")

    def new_version(self) -> None:
        # Północ dnia lokalnego (aby uniknąć przesunięcia daty w wersjiId)
        local_now = datetime.now()
        midnight_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)

        # wersjaId jako lokalna północ (string)
        self.version_id.setText(midnight_local.strftime("%Y%m%dT%H%M%S"))

        # początek wersji w UTC (wymóg formatu pliku)
        midnight_aware = midnight_local.astimezone()
        utc_midnight = midnight_aware.astimezone(timezone.utc).replace(microsecond=0)

        self.version_start.setDateTime(QDateTime.fromString(utc_midnight.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                            Qt.DateFormat.ISODate).toLocalTime())
        self.version_end.set_value(None)

    def _on_version_end_changed(self, *args) -> None:
        """Koniec wersji aktu wyznacza koniec obowiązywania (dzień wcześniej)."""
        if self.version_end.check.isChecked():
            end_date = self.version_end.edit.date()
            self.effective_to.set_value(end_date.addDays(-1).toString("yyyy-MM-dd"))

    def _on_type_changed(self, *args) -> None:
        code = self.plan_type.currentData()
        _set_combo(self.level, DEFAULT_LEVEL.get(code, "local"))
        pog = code == POG
        for widget in (self.modification, self._modification_label):
            widget.setEnabled(pog)
        for widget in (self.change_check, self._change_label):
            widget.setEnabled(not pog)
        if pog:
            self.change_check.setChecked(False)
        self.tabs.setTabEnabled(TAB_OBJECTS, pog)
        self.objects_hint.setEnabled(pog)
        if not self._loading:
            self.auto_namespace()

    def _refresh(self, *args) -> None:
        # Puste pola wymagane dostają ramkę błędu dopiero po edycji pola albo po „Sprawdź dane” /
        # „Generuj GML” — świeżo otwarty formularz nie jest „pełen błędów” (pkt 6.7).
        show_all = self._show_required
        for widget in (self.namespace, self.local_id, self.version_id, self.title):
            show = show_all or widget.isModified()
            theme.mark_invalid(widget, show and not widget.text().strip())
        theme.mark_invalid(self.extent_layer, show_all and not self.extent_layer.currentLayer())
        has_output = bool(self.output.filePath().strip())
        # Ramka na polu tekstowym, nie na całym QgsFileWidget (inaczej czerwony jest też przycisk „…”).
        theme.mark_invalid(self.output.lineEdit(), show_all and not has_output)
        self.btn_generate.setEnabled(has_output)
        self.btn_generate.setToolTip("" if has_output else self.tr("Wskaż plik wynikowy"))
        done = bool(self._result_path) and Path(self._result_path).is_file()
        self.btn_validate.setEnabled(done and self._open_validation is not None)
        self.btn_add.setEnabled(done and self._add_file is not None)
        # Nieaktywny przycisk ma w podpowiedzi powód (checklista UI, pkt 9).
        self.btn_validate.setToolTip(self.tr("Okno walidacji: XSD, reguły APP 3.0 i geometria") if done and
                                     self._open_validation is not None
                                     else self.tr("Najpierw wygeneruj plik GML („Generuj GML”)"))
        self.btn_add.setToolTip(self.tr("Tabele aktu ze stylami wtyczki APP (GML zapisywany do GeoPackage)")
                                if done and self._add_file is not None
                                else self.tr("Najpierw wygeneruj plik GML („Generuj GML”)"))
        # „Przenumeruj oznaczenia…” działa, gdy w zakładce obiektów jest warstwa z mapowaniem.
        sources = getattr(self, "sources", {})
        if hasattr(self, "btn_cut"):
            ready = self._update_source is not None
            self.btn_cut.setEnabled(ready)
            self.btn_cut.setToolTip(
                self.tr("Nowa granica aktu po wycięciu obszaru przejętego przez nowy plan: dziura, wnęka "
                        "albo podział na części oraz przycięte rysunki (GeoTIFF)")
                if ready else
                self.tr("Najpierw wczytaj akt do aktualizacji (sekcja „Aktualizacja istniejącego aktu”)"))
            if self.plan_type.currentData() == POG:
                self.cut_info.setText(self.tr("wycięcie obszarów planu ogólnego — w przygotowaniu"))
            elif self._cut is not None:
                self.cut_info.setText(self._cut.report())
            else:
                self.cut_info.setText("")
        if hasattr(self, "btn_numbering"):
            has_layers = any(widget.isChecked() and widget.layer() is not None for widget in sources.values())
            self.btn_numbering.setEnabled(has_layers)
            self.btn_numbering.setToolTip(
                self.tr("Znajdź w warstwach powtórzone i puste oznaczenia i zaproponuj nowe numery "
                        "(zapis w warstwie)") if has_layers
                else self.tr("Najpierw wybierz warstwę stref albo regulacji"))
        self._refresh_list_buttons()

    def _add_base_map(self, *args, value: Optional[dict] = None) -> None:
        row = self.base_maps.rowCount()
        self.base_maps.insertRow(row)
        value = value or {"date": QDate.currentDate().toString("yyyy-MM-dd"), "reference": "", "link": ""}
        for column, key in enumerate(("date", "reference", "link")):
            self.base_maps.setItem(row, column, QTableWidgetItem(value.get(key) or ""))

    def _remove_base_map(self) -> None:
        rows = sorted({index.row() for index in self.base_maps.selectedIndexes()}, reverse=True)
        for row in rows or [self.base_maps.rowCount() - 1]:
            if row >= 0:
                self.base_maps.removeRow(row)

    def _base_maps(self) -> List[dict]:
        result = []
        for row in range(self.base_maps.rowCount()):
            cells = [(self.base_maps.item(row, column).text().strip() if self.base_maps.item(row, column) else "")
                     for column in range(3)]
            if any(cells):
                result.append({"date": cells[0], "reference": cells[1], "link": cells[2] or None})
        return result

    def _on_extent_layer_changed(self, layer) -> None:
        if self._watched_layer is not None:
            try:
                self._watched_layer.selectionChanged.disconnect(self._update_extent_info)
            except (TypeError, RuntimeError):
                pass
        self._watched_layer = layer
        if layer is not None:
            layer.selectionChanged.connect(self._update_extent_info)
            epsg = epsg_of(layer.crs())
            if epsg in SUPPORTED_EPSG:
                _set_combo(self.crs, epsg)
            if layer.selectedFeatureCount():
                self.extent_selected.setChecked(True)
        if self._pick_tool is not None:
            self._pick_tool.setLayer(layer)
        self._update_extent_info()
        self._refresh()

    def _update_extent_info(self, *args) -> None:
        layer = self.extent_layer.currentLayer()
        if layer is None:
            theme.set_html(self.extent_info, self.tr("Wybierz warstwę poligonową z granicą aktu."))
            return
        features = layer.getSelectedFeatures() if self.extent_selected.isChecked() else layer.getFeatures()
        area = QgsDistanceArea()
        area.setSourceCrs(layer.crs(), QgsProject.instance().transformContext())
        ellipsoid = QgsProject.instance().ellipsoid()
        area.setEllipsoid(ellipsoid if ellipsoid and ellipsoid != "NONE" else "GRS80")
        count, total = 0, 0.0
        for feature in features:
            count += 1
            if count <= 5000:
                total += area.measureArea(feature.geometry())
        text = self.tr("Obiektów: {0}, powierzchnia: {1:.2f} ha").format(count, total / 10000)
        if count == 0:
            text = "<span style='color:{c:err}'>" + self.tr("Brak obiektów — zaznacz granicę aktu.") + "</span>"
        theme.set_html(self.extent_info, text)

    def _toggle_pick(self, on: bool) -> None:
        canvas = self._canvas() if self._canvas else None
        if canvas is None:
            self.btn_pick.setChecked(False)
            return
        if on:
            layer = self.extent_layer.currentLayer()
            if layer is None:
                self.btn_pick.setChecked(False)
                return
            self._previous_tool = canvas.mapTool()
            self._pick_tool = QgsMapToolIdentifyFeature(canvas, layer)
            self._pick_tool.featureIdentified.connect(self._on_feature_picked)
            canvas.setMapTool(self._pick_tool)
            self.extent_selected.setChecked(True)
        else:
            self._release_pick_tool(canvas)

    def _release_pick_tool(self, canvas=None) -> None:
        canvas = canvas or (self._canvas() if self._canvas else None)
        if self._pick_tool is not None and canvas is not None and canvas.mapTool() is self._pick_tool:
            canvas.unsetMapTool(self._pick_tool)
            if self._previous_tool is not None:
                canvas.setMapTool(self._previous_tool)
        self._pick_tool = None

    def _on_feature_picked(self, feature) -> None:
        layer = self.extent_layer.currentLayer()
        if layer is None:
            return
        if feature.id() in layer.selectedFeatureIds():
            layer.deselect(feature.id())
        else:
            layer.select(feature.id())

    # --- aktualizacja istniejącego aktu --------------------------------------------------

    def set_act_index(self, entries, url_template: Optional[str]) -> None:
        """Indeks aktów Rejestru (wyszukiwarka aktu) i szablon adresu plików aktów z manifestu GS Mapa."""
        self._act_entries = list(entries or [])
        self._acts_url_template = url_template

    def _update_status(self, text: str, level: str = "INFO") -> None:
        theme.set_text_role(self.update_info, {"BŁĄD": "err", "UWAGA": "warn"}.get(level, ""))
        self.update_info.setText(text)
        if level != "INFO":
            self._log(f"Kreator APP: {text}", level)

    def _on_update_load_clicked(self, *args) -> None:
        self.load_update()

    def load_update(self, text: Optional[str] = None) -> Optional[str]:
        """
        Aktualizacja aktu wskazanego adresem strony w Rejestrze Urbanistycznym albo ``act_id``.

        :return: ``act_id`` pobieranego aktu albo ``None`` (błąd pokazany w oknie).
        """
        text = self.update_ref.text() if text is None else text
        try:
            reference = parse_act_reference(text)
        except ActReferenceError as e:
            self._update_status(str(e), "UWAGA")
            return None
        act_id = resolve_act_id(reference, [entry.act_id for entry in self._act_entries])
        if act_id is None:
            self._update_status(self.tr("Nie znaleziono aktu {0} w indeksie aktów — podaj typ i numer, "
                                        "np. mpzp-{0}.").format(reference[1]), "UWAGA")
            return None
        return self.start_update(act_id)

    def start_update(self, act_id: str) -> Optional[str]:
        """Pobiera opis aktu (``akty/<act_id>.json``), potem jego plik GML, i wczytuje go do formularza."""
        if not self._acts_url_template:
            self._update_status(self.tr("Adres plików aktów GS Mapa nie jest jeszcze znany (manifest) — spróbuj za "
                                        "chwilę albo wczytaj akt z pliku GML."), "UWAGA")
            return None
        self._update_pending = act_id
        self._update_url = None
        self.update_ref.setText(act_id)
        self._update_status(self.tr("Pobieranie opisu aktu {0} z GS Mapa…").format(act_id))
        self._log(f"Kreator APP: aktualizacja aktu {act_id}")
        self._acts().fetch(act_id, self._acts_url_template)
        return act_id

    def _acts(self):
        """Usługa plików aktów (tworzona i podłączana przy pierwszym użyciu)."""
        if self._act_service is None:
            from ..services.acts import ActService

            self._act_service = ActService()
            self._act_service.setParent(self)
        if not self._act_service.property("gs_mapa_creator"):
            self._act_service.details_ready.connect(self._on_update_details)
            self._act_service.details_error.connect(self._on_update_details_error)
            self._act_service.setProperty("gs_mapa_creator", True)
        return self._act_service

    def _downloads(self):
        """Pobieranie pliku GML aktu (tworzone i podłączane przy pierwszym użyciu)."""
        if self._downloader is None:
            from ..services.download import FileDownloader

            self._downloader = FileDownloader()
            self._downloader.setParent(self)
        if not self._downloader.property("gs_mapa_creator"):
            self._downloader.finished.connect(self._on_update_downloaded)
            self._downloader.failed.connect(self._on_update_download_failed)
            self._downloader.setProperty("gs_mapa_creator", True)
        return self._downloader

    def _on_update_details(self, act_id: str, details) -> None:
        if act_id != self._update_pending or self._update_url is not None:
            return
        url = (details.dane or {}).get("gml")
        if not url:
            self._update_pending = None
            self._update_status(self.tr("Akt {0} nie ma pliku GML w Rejestrze — wczytaj go z pliku na dysku.")
                                .format(act_id), "UWAGA")
            return
        self._update_url = url
        self._update_status(self.tr("Pobieranie pliku GML aktu {0}…").format(act_id))
        self._downloads().download(url, self._update_dir(act_id))

    def _on_update_details_error(self, act_id: str, message: str) -> None:
        if act_id != self._update_pending or self._update_url is not None:
            return
        self._update_pending = None
        self._update_status(self.tr("Akt {0}: {1}").format(act_id, message), "BŁĄD")

    def _update_dir(self, act_id: str) -> str:
        base = getattr(self._config, "download_dir", "") if self._config is not None else ""
        if not base:
            base = str(Path(QgsApplication.qgisSettingsDirPath()) / "cache" / "gs_mapa" / "akty")
        return str(Path(base) / act_id)

    def _on_update_downloaded(self, url: str, path: str) -> None:
        if url != self._update_url:
            return
        act_id, self._update_pending, self._update_url = self._update_pending, None, None
        self.load_update_file(path, act_id)

    def _on_update_download_failed(self, url: str, message: str) -> None:
        if url != self._update_url:
            return
        act_id, self._update_pending, self._update_url = self._update_pending, None, None
        self._update_status(self.tr("Nie pobrano pliku GML aktu {0}: {1}").format(act_id, message), "BŁĄD")

    def find_update_act(self, *args) -> ActPickerDialog:
        """Wyszukiwarka aktów jednostki w indeksie Rejestru (niemodalna); wybrany akt jest aktualizowany."""
        if self._act_picker is not None:
            self._act_picker.close()
            self._act_picker.deleteLater()
        self._act_picker = ActPickerDialog(self._act_entries, self._unit, parent=self)
        self._act_picker.accepted.connect(self._on_act_picked)
        self._act_picker.show()
        self._act_picker.raise_()
        self._act_picker.activateWindow()
        return self._act_picker

    def _on_act_picked(self) -> None:
        entry = self._act_picker.selected() if self._act_picker is not None else None
        if entry is not None:
            self.start_update(entry.act_id)

    def _on_update_file_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.tr("Plik GML aktu do aktualizacji"), "",
                                              self.tr("GML (*.gml *.GML)"))
        if path:
            self.load_update_file(path)

    # --- dane z pliku GeoPackage ---------------------------------------------------------

    def _on_template_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.tr("Plik GeoPackage APP"), "",
                                              self.tr("GeoPackage (*.gpkg)"))
        if path:
            self.load_template(path)

    def _template_status(self, text: str, role: str = "") -> None:
        self.template_info.setText(text)
        theme.set_text_role(self.template_info, role)
        self.template_info.setVisible(bool(text))

    def load_template(self, path: str) -> bool:
        """
        Wypełnia formularz z pliku GeoPackage: akt, dokumenty, rysunki i warstwy pliku.

        Strefy i regulacje nie są przepisywane z tabel — idą z warstw projektu, więc jeśli
        tabele pliku są w projekcie, są podpinane jako zasięg i źródła obiektów.

        :param path: plik ``.gpkg`` w układzie wzorcowego pliku wtyczki.
        :return: ``True``, gdy wczytano przynajmniej jedną rzecz.
        """
        from ..services.app_source import read_template

        try:
            source = read_template(path)
        except OSError as e:
            self._template_status(self.tr("Nie wczytano {0}: {1}").format(Path(path).name, e), "err")
            return False
        if not source.act and not source.documents and not source.drawings:
            text = self.tr("{0}: brak aktu, dokumentów i rysunków do wczytania.").format(Path(path).name)
            self._template_status(text, "warn")
            self._log(f"Kreator APP: {text}", "UWAGA")
            return False
        self._apply_template(source)
        bound = self.bind_template_layers(path)
        parts = []
        if source.act:
            parts.append(self.tr("akt {0}").format(source.act.get("local_id") or "?"))
        if DOCUMENT in source.counts:
            parts.append(self.tr("dokumentów: {0}").format(len(source.documents)))
        if DRAWING in source.counts:
            parts.append(self.tr("rysunków: {0}").format(len(source.drawings)))
        if bound:
            parts.append(self.tr("warstw podpiętych: {0}").format(bound))
        text = self.tr("Z pliku {0}: {1}.").format(Path(path).name, ", ".join(parts))
        if source.notes:
            text += " " + "; ".join(source.notes) + "."
        if not bound:
            text += " " + self.tr("Strefy i regulacje: dodaj tabele pliku do projektu (przeciągnij plik do QGIS) "
                                  "albo wskaż warstwy w zakładce „Strefy i regulacje”.")
        self._template_status(text, "warn" if source.notes or not bound else "")
        self._log(f"Kreator APP: wczytano z GeoPackage {path} — {', '.join(parts)}")
        #: Plik źródłowy dla „Odśwież z pliku” (także po zwykłym „Z pliku wzorcowego…”).
        self._table_source = str(Path(path))
        self.btn_reload.setEnabled(True)
        self.tabs.setCurrentIndex(TAB_ACT)
        return True

    def _apply_template(self, source) -> None:
        """Wstawia akt, dokumenty i rysunki z pliku GeoPackage do formularza."""
        act = source.act or {}
        loading, self._loading = self._loading, True
        try:
            plan_type = act.get("plan_type") or self.plan_type.currentData()
            if self.plan_type.findData(plan_type) < 0:
                # Typ spoza jednostki kreatora: jednostka z przestrzeni nazw pliku (albo żadna).
                self.set_unit(self._unit_for(act.get("namespace") or "", plan_type))
            _set_combo(self.plan_type, plan_type)
            self._on_type_changed()
            if act:
                self.namespace.setText(act.get("namespace") or "")
                self.local_id.setText(act.get("local_id") or "")
                self.version_id.setText(act.get("version_id") or "")
                if act.get("version_start"):
                    moment = QDateTime.fromString(str(act["version_start"]), Qt.DateFormat.ISODate)
                    if moment.isValid():
                        self.version_start.setDateTime(moment.toLocalTime())
                self.version_end.set_value(act.get("version_end"))
                self.title.setText(act.get("title") or "")
                self.alt_titles.setText(act.get("alt_titles") or "")
                _set_combo(self.level, act.get("level") or DEFAULT_LEVEL.get(plan_type, "local"))
                _set_combo(self.status, act.get("status") or "legalForce")
                if act.get("effective_from"):
                    self.effective_from.setDate(QDate.fromString(str(act["effective_from"]), "yyyy-MM-dd"))
                # Data z pliku jest wartością źródłową — uchwała jej nie nadpisze bez decyzji.
                self._auto_effective = self.effective_from.date().toString("yyyy-MM-dd")
                self._set_effective_hint("")
                self.effective_to.set_value(act.get("effective_to"))
                _set_combo(self.modification, bool(act.get("modification")))
                count = act.get("change_count")
                self.change_check.setChecked(count is not None)
                self.change_count.setValue(int(count or 0))
                self.changed_plan.setText(act.get("changed_plan") or "")
                self.base_maps.setRowCount(0)
                for value in act.get("base_maps") or []:
                    self._add_base_map(value=value)
            if DOCUMENT in source.counts:
                self._documents = list(source.documents)
                self._fill_documents()
            if DRAWING in source.counts:
                self._drawings = list(source.drawings)
                self._fill_drawings()
        finally:
            self._loading = loading
        self._refresh()

    def bind_template_layers(self, path: str) -> int:
        """
        Zasięg aktu i źródła stref z warstw pliku GeoPackage obecnych w projekcie.

        :return: liczba podpiętych warstw (0 — tabel pliku nie ma w projekcie).
        """
        target = str(Path(path))
        layers = {}
        for layer in QgsProject.instance().mapLayers().values():
            source = str(layer.source() or "")
            if source.split("|", 1)[0] == target:
                layers[layer.name()] = layer
        bound = 0
        act_layer = layers.get(ACT)
        if is_polygon_layer(act_layer):
            self.extent_layer.setLayer(act_layer)
            bound += 1
        for kind, widget in self.sources.items():
            layer = layers.get(kind)
            if is_polygon_layer(layer):
                widget.setChecked(True)
                widget.layer_combo.setLayer(layer)
                bound += 1
        return bound

    # --- tryb edycji w tabelach (GeoPackage jako źródło danych) -------------------------

    def load_table_source(self, path: str) -> bool:
        """
        Wczytuje wypełniony wzorcowy GeoPackage jako źródło danych i przełącza okno w tryb
        edycji w tabelach: akt, dokumenty i rysunki są tylko do odczytu (edycja w QGIS),
        a „Generuj GML” działa bez zmian.

        Warstwy pliku, których nie ma w projekcie, są dodawane — bez nich nie byłoby zasięgu
        aktu ani stref.

        :param path: plik ``.gpkg`` w układzie wzorcowego pliku wtyczki.
        :return: ``True``, gdy plik dał się wczytać.
        """
        target = str(Path(path))
        added = 0
        if not self._layers_of(target):
            from ..services.app_files import add_app_layers

            added = len(add_app_layers(target, Path(target).name))
        if not self.load_template(target):
            return False
        if added:
            self._template_status(self.template_info.text() + " "
                                  + self.tr("Dodano warstwy pliku do projektu: {0}.").format(added))
        self.check_table_mode.setChecked(True)
        self.set_table_mode(True)
        # Wczytanie pliku przestawia część pól (typy, daty) — blokada musi wrócić na wszystkie.
        self._lock_forms()
        self.check_table_source()
        self._refresh()
        return True

    def refresh_from_file(self) -> bool:
        """
        Czyta ponownie akt, dokumenty i rysunki z pliku wskazanego przy wczytaniu.

        Warstwy projektu nie są ruszane (strefy żyją w tabelach QGIS), więc zmiany w pliku
        widać dopiero po zapisaniu ich w QGIS.

        :return: ``True``, gdy plik dał się odczytać.
        """
        if not self._table_source:
            return False
        if not self.load_template(self._table_source):
            return False
        if self._table_mode:
            self._lock_forms()
        self.check_table_source()
        self._refresh()
        return True

    def set_table_mode(self, enabled: bool) -> None:
        """Tryb „edytuję w tabelach”: dane z pliku tylko do odczytu, edycja w QGIS."""
        enabled = bool(enabled)
        if enabled == self._table_mode:
            return
        self._table_mode = enabled
        self.check_table_mode.setChecked(enabled)
        if enabled:
            self._lock_forms()
            self.intro.setText(self.tr(
                "Tryb tabel: akt, dokumenty i rysunki czytane z pliku GeoPackage są tylko do odczytu. "
                "Zmieniaj je w tabelach QGIS i kliknij „Odśwież z pliku”, potem „Generuj GML”."))
        else:
            self._unlock_forms()
            self.intro.setText(self._intro_text)
            self.intro.setToolTip(self._intro_tooltip)
        self.btn_reload.setEnabled(bool(self._table_source))
        self._refresh()

    def check_table_source(self) -> List[PlanIssue]:
        """
        Braki danych z pliku bez odpytywania sieci: plan, ``check_plan``, liczniki zakładek
        i podsumowanie pod listą uwag.

        :return: uwagi (błędy blokują „Generuj GML”, ostrzeżenia nie).
        """
        try:
            plan, issues = self.build_plan()
        except (ValueError, KeyError, TypeError) as e:  # brak danych nie może wywalić okna
            logger.exception("Kreator APP: nie zbudowano planu z pliku")
            self._log(f"Kreator APP: nie zbudowano planu z pliku ({e})", "BŁĄD")
            return []
        issues = issues + check_plan(plan, self.codes)
        self._base_issues, self._link_issues = issues, []
        self._summary_plan, self._summary_report = plan, None
        self._show_issues(issues)
        self._show_summary(issues)
        errors = sum(i.severity == "error" for i in issues)
        self._log(f"Kreator APP: plik {Path(self._table_source or '').name} — błędów {errors}, "
                  f"uwag {len(issues) - errors}")
        return issues

    def _lock_forms(self) -> None:
        """
        Blokuje pola zakładek *Akt*, *Dokumenty* i *Rysunki* poza sekcją „Wypełnij z pliku
        GeoPackage” (tam zostaje „Z pliku wzorcowego…”, „Odśwież z pliku” i przełącznik).

        Pola tekstowe stają się tylko do odczytu (zostają czytelne), listy kodowe i przyciski
        są nieaktywne, a tabele przestają przyjmować edycję. Stan sprzed blokady jest
        zapamiętywany, więc wyłączenie trybu przywraca formularz bez zmian.
        """
        for index in (TAB_ACT, TAB_DOCUMENTS, TAB_DRAWINGS):
            page = self.tabs.widget(index)
            page = page.widget() if isinstance(page, QScrollArea) else page
            if page is None:
                continue
            for widget in page.findChildren(QWidget):
                if widget is self._template_box or self._template_box.isAncestorOf(widget):
                    continue
                self._lock_widget(widget)

    def _lock_widget(self, widget: QWidget) -> None:
        if widget not in self._table_locked:
            self._table_locked[widget] = (widget.isEnabled(),
                                          widget.isReadOnly() if hasattr(widget, "isReadOnly") else None,
                                          widget.editTriggers() if isinstance(widget, QAbstractItemView) else None)
        # Pola z wartością liczbową, datą i godziną: „tylko do odczytu” nie wystarcza — strzałki
        # i kalendarz nadal zmieniają wartość, więc pola są po prostu nieaktywne.
        if isinstance(widget, QAbstractSpinBox):
            widget.setEnabled(False)
            return
        if hasattr(widget, "setReadOnly"):
            widget.setReadOnly(True)
            return
        if isinstance(widget, QAbstractItemView):
            widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            return
        if isinstance(widget, (QComboBox, QCheckBox, QPushButton, QDateEdit, QDateTimeEdit, QSpinBox,
                               QPlainTextEdit, QgsFileWidget)):
            widget.setEnabled(False)

    def _unlock_forms(self) -> None:
        """Przywraca stan pól sprzed trybu tabel (włącznie z polami nieaktywnymi z założenia)."""
        for widget, (enabled, read_only, triggers) in self._table_locked.items():
            try:
                if read_only is not None:
                    widget.setReadOnly(read_only)
                if triggers is not None:
                    widget.setEditTriggers(triggers)
                widget.setEnabled(enabled)
            except RuntimeError:
                continue  # pole usunięte razem z oknem dokumentu/rysunku
        self._table_locked.clear()

    def _unit_for(self, namespace: str, plan_type: Optional[str]) -> Optional[Unit]:
        """Jednostka z przestrzeni nazw, o ile dopuszcza typ aktu."""
        parts = parse_namespace(namespace)
        unit = find_unit_by_jpt(parts[1]) if parts else None
        if unit is not None and plan_type and plan_type in allowed_act_types(unit, [plan_type]):
            return unit
        return None

    # --- tryb „Szczegóły” (podgląd pliku bez edycji) --------------------------------------

    def set_details_mode(self, enabled: bool = True) -> None:
        """
        Okno w trybie „Szczegóły”: te same zakładki co kreator, ale bez edycji i bez zapisu.

        Znikają sekcje wczytywania pliku i przyciski zapisu, walidacji i dodawania wyniku oraz
        zakładka *Strefy i regulacje* (to ustawienie kreatora, nie zawartość pliku). Formularz
        blokowany jest tak jak w trybie tabel: pola tekstowe zostają czytelne (można kopiować
        wartości), listy kodowe, daty i przyciski są nieaktywne, a tabele nie przyjmują edycji.
        """
        self._read_only = bool(enabled)
        if not self._read_only:
            return
        self.setObjectName("GSMapaAppDetailsDialog")
        self.setWindowTitle(self._details_title())
        self.intro.setText(self.tr(
            "Szczegóły pliku — dane tylko do odczytu. Nową wersję aktu przygotuj przez „Edytuj” "
            "na liście plików."))
        self.intro.setToolTip(self.tr(
            "Zawartość pliku APP: akt, dokumenty i rysunki. Nic nie jest tu liczone ani zapisywane; "
            "dwuklik na dokumencie albo rysunku pokazuje wszystkie jego pola."))
        for widget in (self._update_box, self._template_box, self._output_label, self.output,
                       self.btn_load, self.btn_save, self.btn_check, self.btn_generate,
                       self.btn_validate, self.btn_add):
            widget.setVisible(False)
        if hasattr(self.tabs, "setTabVisible"):
            self.tabs.setTabVisible(TAB_OBJECTS, False)
        else:  # starsze Qt: ukryta strona chowa też swój przycisk zakładki
            self._objects_page.setVisible(False)
        self._documents_hint.setText(self.tr(
            "Dokumenty formalne z pliku. Dwuklik na wierszu pokazuje wszystkie pola dokumentu "
            "(tytuł, numer, organ, dziennik, łącza). „Pobierz” przy łączu zapisuje plik "
            "w folderze pobrań wtyczki, a „Pobierz…” — w wybranym folderze."))
        self._drawings_hint.setText(self.tr(
            "Rysunki aktu z pliku. Dwuklik na wierszu pokazuje wszystkie pola rysunku "
            "(łącze GeoTIFF, legendę, układ, skalę). „Pobierz” przy łączu zapisuje rysunek "
            "na dysk, „Dodaj do mapy” wczytuje go do projektu."))
        self.btn_generate.setDefault(False)
        self._lock_forms()

    def load_details(self, path: str) -> bool:
        """
        Pokazuje w oknie zawartość pliku bez zmiany jego wersji i bez możliwości edycji.

        GML czytany jest odczytem aktu (:func:`core.app_update.read_act_source`, APP 1.0–3.0),
        GeoPackage — odczytem wzorcowego pliku (:func:`services.app_source.read_template`).
        Wartości list kodowych spoza listy wtyczki (np. ``status`` z APP 1.0) są dopisywane do pól,
        żeby podgląd pokazywał treść pliku, a nie wartość domyślną.

        :param path: plik ``.gml`` albo ``.gpkg`` z danymi APP.
        :return: ``True``, gdy plik dał się odczytać.
        """
        target = Path(path)
        if target.suffix.lower() == ".gml":
            try:
                source = read_act_source(str(target))
            except (AppGmlError, OSError) as e:
                self._details_failed(target, e)
                return False
            version = source.app_version
        else:
            from ..services.app_source import read_template

            try:
                source = read_template(str(target))
            except OSError as e:
                self._details_failed(target, e)
                return False
            version = None
        if not source.act and not source.documents and not source.drawings:
            self._details_failed(target, self.tr("brak aktu, dokumentów i rysunków"))
            return False
        self.set_details_mode(True)
        self.set_state(file_draft(source))
        for combo, code in ((self.plan_type, source.act.get("plan_type")),
                            (self.level, source.act.get("level")), (self.status, source.act.get("status"))):
            _show_code(combo, code)
        self._details_name = target.name
        self.setWindowTitle(self._details_title())
        self.unit_label.setText(self._details_unit(source.act))
        self.summary.setText(self._details_summary(version, source))
        #: ``set_state`` zdejmuje blokadę identyfikatora — w podglądzie pola zostają zablokowane.
        self._lock_forms()
        self.tabs.setCurrentIndex(TAB_ACT)
        self._log(f"Szczegóły APP: {target} — {version or 'GeoPackage'}")
        for note in source.notes:
            self._log(f"Szczegóły APP: {note}", "UWAGA")
        return True

    def _details_title(self) -> str:
        """Tytuł okna podglądu z nazwą pliku."""
        return (self.tr("Szczegóły pliku — {0}").format(self._details_name) if self._details_name
                else self.tr("Szczegóły pliku"))

    def _details_unit(self, act: dict) -> str:
        """
        Jednostka terytorialna aktu w podglądzie — z przestrzeni nazw pliku, a nie z panelu.

        W kreatorze jednostka pochodzi z paska na górze panelu (i zawęża typy aktów). Podgląd
        pokazuje jednostkę zapisaną w pliku, bo to ona jest treścią, którą się ogląda.
        """
        unit = self._unit_for(str(act.get("namespace") or ""), act.get("plan_type"))
        if unit is not None:
            return unit.label
        return self.tr("jednostka spoza listy wtyczki — przestrzeń nazw z pliku")

    def _details_summary(self, version: Optional[str], source) -> str:
        """
        Jedna linia opisu zawartości pliku.

        W podglądzie nie ma listy uwag ani pliku wynikowego, więc miejsce pod formularzem zajmuje
        to, ile czego w pliku jest (strefy i regulacje liczone z nagłówka pliku, bo ich zakładka
        jest w podglądzie ukryta).
        """
        zones = sum(count for kind, count in (source.counts or {}).items() if kind in SPATIAL_KINDS)
        parts = [self.tr("APP {0}").format(version)] if version else []
        parts += [self.tr("dokumentów: {0}").format(len(source.documents)),
                  self.tr("rysunków: {0}").format(len(source.drawings)),
                  self.tr("stref i regulacji: {0}").format(zones)]
        text = " · ".join(parts) + "."
        notes = list(source.notes or [])
        return text + (" " + " ".join(notes) if notes else "")

    def _details_failed(self, path: Path, error) -> None:
        """Komunikat o pliku, którego nie dało się pokazać (w oknie, bez modalnego błędu)."""
        text = self.tr("Nie wczytano {0}: {1}").format(path.name, error)
        self.summary.setText(text)
        self._log(f"Szczegóły APP: {text}", "BŁĄD")

    def load_update_file(self, path: str, act_id: Optional[str] = None) -> bool:
        """
        Wypełnia formularz aktem z pliku GML (nowa wersja: ten sam ``lokalnyId``, nowa ``wersjaId``)
        i dodaje warstwy pliku do projektu — zasięg aktu oraz strefy i regulacje są brane z nich.
        """
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            source = read_act_source(path)
        except (AppGmlError, OSError) as e:
            QApplication.restoreOverrideCursor()
            self._update_status(self.tr("Nie wczytano {0}: {1}").format(Path(path).name, e), "BŁĄD")
            return False
        QApplication.restoreOverrideCursor()
        act = source.act
        namespace, plan_type = str(act.get("namespace") or ""), str(act.get("plan_type") or "")
        unit = self._unit_for(namespace, plan_type)
        if unit != self._unit and (unit is not None or plan_type not in allowed_act_types(self._unit, [plan_type])):
            self._log(f"Kreator APP: jednostka kreatora — {unit.label if unit else 'bez jednostki'} (z aktu)")
            self._loading = True
            try:
                self.set_unit(unit)
            finally:
                self._loading = False
        now = datetime.now(timezone.utc).replace(microsecond=0)
        version_id = version_id_now(now)
        draft = update_draft(source, version_id, now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                             crs=source.crs if source.crs in SUPPORTED_EPSG else None)
        self.set_state(draft)
        self.output.setFilePath(str(Path(path).parent / update_filename(namespace, str(act.get("local_id")),
                                                                          version_id)))
        if act_id:
            self.update_ref.setText(act_id)
        self._update = {"act_id": act_id, "path": str(path), "previous_version": source.version_id,
                        "app_version": source.app_version, "namespace": namespace,
                        "local_id": str(act.get("local_id") or "")}
        self._update_source = source
        self._lock_identity(True)
        self.previous_output.setText(self._previous_path(str(path), namespace, str(act.get("local_id")),
                                                         source.version_id))
        self._show_update_info(source)
        if source.act.get("version_end"):
            self._log(f"Kreator APP: poprzednia wersja aktu była zamknięta ({source.act['version_end']}) "
                      "— nowa wersja jest otwarta", "UWAGA")
        if source.app_version and source.app_version != "3.0":
            self._log(f"Kreator APP: akt jest w APP {source.app_version} — zapis przygotuje plik APP 3.0",
                      "UWAGA")
        self._log(f"Kreator APP: wczytano akt {act_id or path} (wersja {source.version_id}) — nowa wersja {version_id}")
        for note in source.notes:
            self._log(f"Kreator APP: {note}", "UWAGA")
        self.tabs.setCurrentIndex(TAB_ACT)
        self._import_update_layers(str(path))
        self._refresh()
        return True

    def _lock_identity(self, locked: bool) -> None:
        """Przy aktualizacji aktu przestrzeń nazw i ``lokalnyId`` są niezmienne (zmienia się wersja)."""
        for widget in (self.namespace, self.local_id):
            widget.setReadOnly(locked)
        self.namespace.setToolTip(
            self.tr("Przestrzeń nazw zbioru jest niezmienna — przy aktualizacji zmienia się tylko wersja")
            if locked else
            self.tr("Przestrzeń nazw zbioru danych (wspólna dla wszystkich obiektów pliku)"))
        self.local_id.setToolTip(
            self.tr("lokalnyId aktu jest niezmienny — przy aktualizacji zmienia się tylko wersja")
            if locked else "")

    @staticmethod
    def _previous_path(source_path: str, namespace: str, local_id: str, version_id: Optional[str]) -> str:
        """Plik poprzedniej wersji obok nowego pliku; nigdy nie nadpisuje pliku, z którego czytamy."""
        if not version_id:
            return ""
        target = Path(source_path).parent / update_filename(namespace, local_id, version_id)
        if target == Path(source_path):
            target = target.with_name(f"{target.stem}-zamknieta{target.suffix}")
        return str(target)

    def _show_update_info(self, source=None) -> None:
        if not self._update:
            self._update_status(self.tr("Nowy akt. Aby przygotować nową wersję aktu z Rejestru, wskaż go powyżej."))
            return
        update = self._update
        name = update.get("act_id") or Path(update.get("path") or "").name
        version = str(update.get("app_version") or "")
        text = self.tr("Aktualizacja aktu {0}: poprzednia wersja {1} (APP {2}), nowa wersja {3}.").format(
            name, update.get("previous_version") or "?", version or "?",
            self.version_id.text().strip())
        if version and version != "3.0":
            text += " " + self.tr("Uwaga: akt jest w APP {0} — zapis przygotuje plik w APP 3.0 "
                                 "(nowa wersja schematu, nie tylko treści).").format(version)
        if source is not None:
            zones = sum(n for kind, n in source.counts.items() if kind in SPATIAL_KINDS)
            text += " " + self.tr("Dokumentów: {0}, rysunków: {1}, stref i regulacji: {2}.").format(
                len(source.documents), len(source.drawings), zones)
            text += " " + self.tr("Zapis tworzy dwa pliki: nową wersję (obiekty bez zmian zostają przy swojej "
                                  "wersji) i poprzednią wersję z domkniętymi wersjami.")
            if source.notes:
                text += " " + " ".join(source.notes)
        self._update_status(text)

    @staticmethod
    def _layers_of(path: str) -> list:
        target = str(Path(path))
        return [layer for layer in QgsProject.instance().mapLayers().values()
                if str(layer.customProperty(SOURCE_PROPERTY) or "") == target]

    def _files(self):
        """Dodawanie plików aktu do projektu (usługa zakładki albo własna) — podłączana raz."""
        if self._file_service is None and self._own_files is None:
            from ..services.app_files import AppFileService

            self._own_files = AppFileService(self)
        service = self._file_service or self._own_files
        if not self._files_connected:
            service.imported.connect(self._on_update_imported)
            service.failed.connect(self._on_update_import_failed)
            self._files_connected = True
        return service

    def _import_update_layers(self, path: str) -> None:
        """Warstwy pliku aktu w projekcie: istniejące są podpinane od razu, brakujące — po dodaniu pliku."""
        if self._layers_of(path):
            self.bind_update_layers(path)
            return
        service = self._files()
        self._update_import = path
        self.update_info.setText(self.update_info.text() + " " + self.tr("Dodawanie warstw aktu do projektu…"))
        if self._file_service is not None and self._add_file is not None:
            self._add_file(path)
        else:
            service.add_file(path)

    def _on_update_imported(self, path: str, count: int, source: str) -> None:
        if path != self._update_import:
            return
        self._update_import = None
        self.bind_update_layers(path)

    def _on_update_import_failed(self, path: str, message: str) -> None:
        if path != self._update_import:
            return
        self._update_import = None
        self._update_status(self.tr("Nie dodano warstw aktu: {0} — wskaż warstwy ręcznie.").format(message), "UWAGA")

    def bind_update_layers(self, path: str) -> int:
        """
        Zasięg aktu i źródła stref / regulacji z warstw pliku aktu (tabele o nazwach klas APP).

        :return: liczba podpiętych warstw.
        """
        layers = {layer.name(): layer for layer in self._layers_of(path)}
        bound = 0
        act_layer = layers.get(ACT)
        if act_layer is not None and is_polygon_layer(act_layer):
            self.extent_layer.setLayer(act_layer)
            self.extent_selected.setChecked(False)
            self.extent_dissolve.setChecked(True)
            bound += 1
        for kind, widget in self.sources.items():
            layer = layers.get(kind)
            layer = layer if is_polygon_layer(layer) else None
            widget.setChecked(layer is not None)
            widget.layer_combo.setLayer(layer)
            widget.selected_only.setChecked(False)
            bound += layer is not None
        self._show_update_info()
        self.update_info.setText(self.update_info.text() + " " + self.tr(
            "Warstwy aktu w projekcie (grupa {0}): zmień geometrię i atrybuty w QGIS, potem „Generuj GML”.").format(
            Path(path).name))
        self._log(f"Kreator APP: podpięto warstwy aktu z {path}: {bound}")
        return bound

    # --- dokumenty i rysunki -------------------------------------------------------

    def _fill_documents(self) -> None:
        table = self.documents_table
        table.setRowCount(0)
        for value in self._documents:
            row = table.rowCount()
            table.insertRow(row)
            links = [str(link).strip() for link in value.get("links") or [] if str(link or "").strip()]
            # Kolumna „Łącze” to odnośnik „Pobierz” (adres jest podpowiedzią), nie tekst adresu.
            cells = (ROLE_LABELS.get(value.get("role"), value.get("role")), value.get("local_id"), value.get("title"),
                     value.get("number") or "", value.get("date"), "")
            for column, text in enumerate(cells):
                table.setItem(row, column, QTableWidgetItem(text or ""))
            self._set_link_cell(table, "documents", row, _DOCUMENTS_LINK_COLUMN, links)

    def _fill_drawings(self) -> None:
        table = self.drawings_table
        table.setRowCount(0)
        for value in self._drawings:
            row = table.rowCount()
            table.insertRow(row)
            # Kolumna „Łącze” to odnośnik „Pobierz” (adres jest podpowiedzią), nie tekst adresu.
            cells = (value.get("local_id"), value.get("title"), "", f"1:{value.get('scale')}")
            for column, text in enumerate(cells):
                table.setItem(row, column, QTableWidgetItem(text or ""))
            links = [str(value.get("link") or "").strip()]
            self._set_link_cell(table, "drawings", row, _DRAWINGS_LINK_COLUMN, [link for link in links if link])

    def _set_link_cell(self, table: QTableWidget, kind: str, row: int, column: int,
                       links: List[str]) -> None:
        """
        Wstawia w komórkę „Łącze” odnośnik „Pobierz” (albo tekst mówiący, że łącza nie ma).

        Odnośnik pobiera plik z adresów wiersza do folderu pobrań wtyczki — bez pytania
        o katalog; cały adres (i katalog docelowy) jest w podpowiedzi, bo w kolumnie się nie mieści.

        :param table: tabela dokumentów albo rysunków.
        :param kind: ``documents`` albo ``drawings``.
        :param row: numer wiersza, którego dotyczy odnośnik.
        :param column: numer kolumny „Łącze”.
        :param links: adresy plików z wiersza (także spoza ``http``/``https`` — filtr jest w pobieraniu).
        """
        if not any(is_web(link) for link in links):
            item = table.item(row, column)
            item.setText(self.tr("brak łącza"))
            item.setToolTip(self.tr("Wiersz nie ma łącza http — nie ma czego pobrać."))
            return
        button = QToolButton()
        button.setText(self.tr("Pobierz"))
        button.setToolTip(self.tr("{0}\nZapisuje plik w folderze: {1}").format(
            "\n".join(str(link).strip() for link in links), self._link_base_dir()))
        theme.link_button(button, "download")
        button.clicked.connect(lambda _checked=False, target=row: self.download_link(kind, target))
        # Odnośnik wyrównany do lewej i tylko tak szeroki, jak tekst — kliknięcie w puste
        # miejsce komórki nie pobiera pliku (zaznacza wiersz, jak w pozostałych kolumnach).
        cell = QWidget()
        layout = QHBoxLayout(cell)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.addWidget(button)
        layout.addStretch(1)
        table.setCellWidget(row, column, cell)

    def _document_defaults(self) -> dict:
        """Propozycje pól dokumentu z jednostki: organ, dziennik urzędowy i podpis źródła."""
        return document_defaults(self._unit, self.codes)

    def _set_effective_hint(self, text: str, warning: bool = False) -> None:
        """Podpis pod „Obowiązuje od”: skąd data i czy różni się od daty uchwały."""
        self.effective_hint.setText(text)
        theme.set_text_role(self.effective_hint, "warn" if warning else "muted")
        self.effective_hint.setVisible(bool(text))

    def _sync_effective_from(self) -> None:
        """
        „Obowiązuje od” z daty dokumentu uchwalającego (uchwały).

        Data z uchwały zastępuje tylko wartość, która sama z wypełnienia pochodzi (albo domyślną
        datę otwarcia okna). Datę wpisaną przez użytkownika zostawiamy — rozbieżność pokazujemy
        pod polem jako uwagę, bo wybór należy do użytkownika.
        """
        if self._loading:
            return
        document = next((item for item in self._documents if is_adopting(item)), None)
        date = effective_from(document) if document is not None else None
        if document is None or not date:
            self._set_effective_hint("")
            return
        label = str(document.get("number") or document.get("local_id") or self.tr("uchwała"))
        current = self.effective_from.date().toString("yyyy-MM-dd")
        if current == self._auto_effective:
            self.effective_from.setDate(QDate.fromString(date, "yyyy-MM-dd"))
            self._auto_effective = date
            self._log(f"Kreator APP: obowiązuje od {date} — z uchwały {label}")
        elif current != date:
            self._set_effective_hint(
                self.tr("Uchwała {0} podaje datę {1} — w polu jest {2} (zostawiono wpisaną wartość).")
                .format(label, date, current), warning=True)
            return
        self._set_effective_hint(self.tr("Z uchwały {0}: obowiązuje od {1} — możesz zmienić.").format(label, date))

    def add_document(self, value: dict) -> None:
        self._documents.append(value)
        self._fill_documents()
        self._sync_effective_from()

    def add_drawing(self, value: dict) -> None:
        self._drawings.append(value)
        self._fill_drawings()

    def _refresh_list_buttons(self) -> None:
        """„Edytuj…” i „Usuń” aktywne tylko przy zaznaczonym wierszu; inaczej podpowiedź z powodem."""
        self._refresh_link_colors()
        for table, edit, remove, name in ((getattr(self, "documents_table", None), getattr(self, "_documents_edit", None),
                                           getattr(self, "_documents_remove", None), self.tr("dokument")),
                                          (getattr(self, "drawings_table", None), getattr(self, "_drawings_edit", None),
                                           getattr(self, "_drawings_remove", None), self.tr("rysunek"))):
            if table is None or edit is None or remove is None:
                continue
            selected = table.currentRow() >= 0 and bool(table.selectedItems())
            if self._read_only or self._table_mode:
                # Podgląd i tryb tabel: dokumenty i rysunki zmienia się poza tym oknem.
                for button in (edit, remove):
                    button.setEnabled(False)
                    button.setToolTip(self.tr("Podgląd: dane tylko do odczytu") if self._read_only
                                      else self.tr("Tryb tabel: zmień dane w pliku GeoPackage "
                                                   "i kliknij „Odśwież z pliku”"))
                continue
            for button in (edit, remove):
                button.setEnabled(selected)
                button.setToolTip("" if selected else self.tr("Zaznacz {0} na liście").format(name))
        # „Pobierz…” i „Dodaj do mapy” tylko czytają plik z łącza — działają też w podglądzie i trybie tabel.
        for kind, table, download, map_button, name in (
                ("documents", getattr(self, "documents_table", None), getattr(self, "_documents_download", None),
                 getattr(self, "_documents_map", None), self.tr("dokument")),
                ("drawings", getattr(self, "drawings_table", None), getattr(self, "_drawings_download", None),
                 getattr(self, "_drawings_map", None), self.tr("rysunek"))):
            if download is None:
                continue
            row = table.currentRow() if table is not None and bool(table.selectedItems()) else -1
            ready = bool(self._row_links(kind, row))
            reason = "" if ready else (
                self.tr("Zaznacz {0} na liście").format(name) if row < 0
                else self.tr("Wiersz nie ma łącza http — nie ma czego pobrać"))
            for button, tooltip in ((download, self.tr("Zapisz plik z łącza zaznaczonego wiersza w wybranym folderze")),
                                    (map_button, self.tr("Pobierz rysunek (GeoTIFF) i wczytaj go do projektu "
                                                         "jako warstwę rastrową"))):
                if button is None:
                    continue
                button.setEnabled(ready)
                button.setToolTip(reason or tooltip)

    def _refresh_link_colors(self) -> None:
        """
        Odnośniki „Pobierz” w zaznaczonych wierszach dostają kolor tekstu zaznaczenia.

        Komórka z odnośnikiem to widżet, więc arkusz stylów tabeli (``item:selected``) go nie
        przebarwia — na tle akcentu odnośnik w kolorze odnośnika był nieczytelny.
        """
        for table, column in ((getattr(self, "documents_table", None), _DOCUMENTS_LINK_COLUMN),
                              (getattr(self, "drawings_table", None), _DRAWINGS_LINK_COLUMN)):
            if table is None:
                continue
            selected = {item.row() for item in table.selectedItems()}
            for row in range(table.rowCount()):
                cell = table.cellWidget(row, column)
                button = cell.findChild(QToolButton) if cell is not None else None
                if button is not None:
                    theme.set_link_selected(button, row in selected)

    def _row_links(self, kind: str, row: int) -> List[str]:
        """
        Adresy plików z wiersza tabeli — tylko ``http``/``https`` (dokument może mieć kilka łączy).

        :param kind: ``documents`` albo ``drawings``.
        :param row: numer wiersza (``-1`` = brak zaznaczenia).
        :return: lista adresów do pobrania (pusta, gdy wiersz nie ma łącza do pliku).
        """
        values = self._documents if kind == "documents" else self._drawings
        if not (0 <= row < len(values)):
            return []
        value = values[row]
        links = value.get("links") if kind == "documents" else [value.get("link")]
        return [str(link).strip() for link in links or [] if is_web(link)]

    # --- pobieranie plików z łączy -----------------------------------------------------

    def _link_base_dir(self) -> str:
        """Katalog bazowy pobrań: wybrany w oknie „Informacja o planie”, a bez wyboru — cache QGIS."""
        base = getattr(self._config, "download_dir", "") if self._config is not None else ""
        if not base:
            base = str(Path(QgsApplication.qgisSettingsDirPath()) / "cache" / "gs_mapa")
        return base

    def _choose_download_dir(self, title: str) -> str:
        """Pyta o folder na pobierane pliki; wybór zostaje zapamiętany w ustawieniach wtyczki."""
        folder = QFileDialog.getExistingDirectory(self, title, self._link_base_dir())
        if folder and self._config is not None:
            self._config.download_dir = folder
        return folder or ""

    def _link_downloads(self):
        """Pobieranie plików z łączy (tworzone i podłączane przy pierwszym użyciu)."""
        if self._link_downloader is None:
            from ..services.download import FileDownloader

            self._link_downloader = FileDownloader()
            self._link_downloader.setParent(self)
            self._link_downloader.progress.connect(self._on_link_progress)
            self._link_downloader.finished.connect(self._on_link_downloaded)
            self._link_downloader.failed.connect(self._on_link_failed)
        return self._link_downloader

    def _link_status(self, kind: str, text: str, warning: bool = False) -> None:
        """Komunikat pod tabelą dokumentów albo rysunków (pusty tekst ukrywa etykietę)."""
        label = getattr(self, f"_{kind}_status", None)
        if label is None:
            return
        label.setText(text)
        theme.set_text_role(label, "warn" if warning else "muted")
        label.setVisible(bool(text))

    def download_link(self, kind: str, row: int) -> bool:
        """
        Pobiera plik z łącza wiersza — to robi odnośnik „Pobierz” w kolumnie „Łącze”.

        Katalog docelowy jest ten sam, co domyślny dla „Pobierz…” (``config.download_dir``,
        a bez wyboru folderu — cache QGIS), więc odnośnik nie zatrzymuje pracy oknem wyboru
        katalogu; ścieżkę zapisanego pliku pokazuje komunikat pod tabelą.

        :param kind: ``documents`` albo ``drawings``.
        :param row: numer wiersza w tabeli.
        :return: ``True``, gdy pobieranie się rozpoczęło.
        """
        folder = self._link_base_dir()
        if kind == "drawings":
            return self.download_drawing(row, folder)
        return self.download_document(row, folder)

    def download_document(self, row: int, folder: str) -> bool:
        """
        Pobiera łącza dokumentu z wiersza tabeli do wskazanego folderu.

        :param row: numer wiersza w tabeli dokumentów.
        :param folder: katalog docelowy.
        :return: ``True``, gdy pobieranie się rozpoczęło.
        """
        if not folder:
            return False
        links = self._row_links("documents", row)
        value = self._documents[row] if 0 <= row < len(self._documents) else {}
        name = str(value.get("title") or value.get("local_id") or self.tr("dokument"))
        if not links:
            self._link_status("documents", self.tr("Dokument {0} nie ma łącza http — nie ma czego pobrać.")
                              .format(name), warning=True)
            return False
        self._log(f"Kreator APP: pobieranie dokumentu {name} ({len(links)} łączy) do {folder}")
        for url in links:
            self._pending_links[url] = ("documents", name, False)
            self._link_status("documents", self.tr("Pobieranie {0}…").format(name))
            self._link_downloads().download(url, folder)
        return True

    def download_drawing(self, row: int, folder: str) -> bool:
        """
        Pobiera rysunek (GeoTIFF) z wiersza tabeli do wskazanego folderu.

        :param row: numer wiersza w tabeli rysunków.
        :param folder: katalog docelowy.
        :return: ``True``, gdy pobieranie się rozpoczęło.
        """
        if not folder:
            return False
        links = self._row_links("drawings", row)
        value = self._drawings[row] if 0 <= row < len(self._drawings) else {}
        name = str(value.get("title") or value.get("local_id") or self.tr("rysunek"))
        if not links:
            self._link_status("drawings", self.tr("Rysunek {0} nie ma łącza http — nie ma czego pobrać.")
                              .format(name), warning=True)
            return False
        self._log(f"Kreator APP: pobieranie rysunku {name} do {folder}")
        for url in links:
            self._pending_links[url] = ("drawings", name, False)
            self._link_status("drawings", self.tr("Pobieranie {0}…").format(name))
            self._link_downloads().download(url, folder)
        return True

    def drawing_to_map(self, row: int) -> bool:
        """
        Pobiera rysunek z wiersza (jeśli nie ma go jeszcze na dysku) i wczytuje go do projektu.

        Plik trafia do ``<katalog pobrań>/rysunki`` — bez pytania o folder, bo to ten sam wybór,
        co przy „Pobierz…”; istniejący plik nie jest pobierany ponownie.

        :param row: numer wiersza w tabeli rysunków.
        :return: ``True``, gdy pobieranie się rozpoczęło.
        """
        links = self._row_links("drawings", row)
        value = self._drawings[row] if 0 <= row < len(self._drawings) else {}
        name = str(value.get("title") or value.get("local_id") or self.tr("rysunek"))
        if not links:
            self._link_status("drawings", self.tr("Rysunek {0} nie ma łącza http — nie ma czego wczytać.")
                              .format(name), warning=True)
            return False
        folder = str(Path(self._link_base_dir()) / "rysunki")
        self._log(f"Kreator APP: rysunek {name} do mapy — pobieranie do {folder}")
        for url in links:
            self._pending_links[url] = ("drawings", name, True)
            self._link_status("drawings", self.tr("Pobieranie {0} do mapy…").format(name))
            self._link_downloads().download(url, folder)
        return True

    def _on_documents_download_clicked(self) -> None:
        folder = self._choose_download_dir(self.tr("Folder na pobrane dokumenty"))
        if folder:
            self.download_document(self.documents_table.currentRow(), folder)

    def _on_drawings_download_clicked(self) -> None:
        folder = self._choose_download_dir(self.tr("Folder na pobrane rysunki"))
        if folder:
            self.download_drawing(self.drawings_table.currentRow(), folder)

    def _on_drawings_map_clicked(self) -> None:
        self.drawing_to_map(self.drawings_table.currentRow())

    def _on_link_progress(self, url: str, received: int, total: int) -> None:
        pending = self._pending_links.get(url)
        if pending is None:
            return
        kind, name, _ = pending
        done = _format_size(received) or "0 kB"
        of = f" / {_format_size(total)}" if total > 0 else ""
        self._link_status(kind, self.tr("Pobieranie {0}: {1}{2}").format(name, done, of))

    def _on_link_downloaded(self, url: str, path: str) -> None:
        pending = self._pending_links.pop(url, None)
        if pending is None:
            return
        kind, name, to_map = pending
        if to_map:
            layer = self._add_drawing_layer(path, name)
            if layer is None:
                self._link_status(kind, self.tr("Nie udało się wczytać rysunku {0} z pliku {1}.")
                                  .format(name, path), warning=True)
                self._log(f"Kreator APP: nie wczytano rysunku {path}", "UWAGA")
                return
            self._link_status(kind, self.tr("Dodano do mapy: {0} — plik: {1}").format(name, path))
        else:
            self._link_status(kind, self.tr("Pobrano: {0}").format(path))
        self._log(f"Kreator APP: plik z łącza {url} — {path}")

    def _on_link_failed(self, url: str, message: str) -> None:
        pending = self._pending_links.pop(url, None)
        if pending is None:
            return
        kind, name, _ = pending
        self._link_status(kind, self.tr("Nie pobrano {0}: {1}").format(name, message), warning=True)
        self._log(f"Kreator APP: nie pobrano {url} — {message}", "UWAGA")

    def _add_drawing_layer(self, path: str, name: str):
        """
        Wczytuje pobrany rysunek jako warstwę rastrową projektu (resampling biliniowy jak w planie).

        :param path: plik GeoTIFF na dysku.
        :param name: nazwa warstwy w projekcie.
        :return: dodana warstwa albo ``None``, gdy plik nie jest rastrem z georeferencją.
        """
        from qgis.core import QgsRasterLayer

        from .plan_info import apply_drawing_resampling

        layer = QgsRasterLayer(path, name or Path(path).stem, "gdal")
        if not layer.isValid():
            return None
        apply_drawing_resampling(layer)
        QgsProject.instance().addMapLayer(layer)
        return layer

    def _add_document(self) -> None:
        if self._read_only:
            return
        default = {"role": "uchwala" if not self._documents else "dokument",
                   "local_id": f"D{len(self._documents) + 1}"}
        dialog = DocumentDialog(self.codes, default, self, defaults=self._document_defaults())
        if dialog.exec():
            self.add_document(dialog.value())

    def _edit_document(self, *args) -> None:
        """Formularz zaznaczonego dokumentu; w podglądzie („Szczegóły”) — tylko do odczytu."""
        row = self.documents_table.currentRow()
        if 0 <= row < len(self._documents):
            dialog = DocumentDialog(self.codes, self._documents[row], self,
                                    defaults=None if self._read_only else self._document_defaults(),
                                    read_only=self._read_only)
            if dialog.exec() and not self._read_only:
                self._documents[row] = dialog.value()
                self._fill_documents()
                self._sync_effective_from()

    def _remove_document(self) -> None:
        if self._read_only:
            return
        row = self.documents_table.currentRow()
        if 0 <= row < len(self._documents):
            del self._documents[row]
            self._fill_documents()
            self._sync_effective_from()

    def _add_drawing(self) -> None:
        if self._read_only:
            return
        dialog = DrawingDialog({"local_id": f"R{len(self._drawings) + 1}"}, self)
        if dialog.exec():
            self.add_drawing(dialog.value())

    def _edit_drawing(self, *args) -> None:
        """Formularz zaznaczonego rysunku; w podglądzie („Szczegóły”) — tylko do odczytu."""
        row = self.drawings_table.currentRow()
        if 0 <= row < len(self._drawings):
            dialog = DrawingDialog(self._drawings[row], self, read_only=self._read_only)
            if dialog.exec() and not self._read_only:
                self._drawings[row] = dialog.value()
                self._fill_drawings()

    def _remove_drawing(self) -> None:
        if self._read_only:
            return
        row = self.drawings_table.currentRow()
        if 0 <= row < len(self._drawings):
            del self._drawings[row]
            self._fill_drawings()

    # --- numeracja oznaczeń ------------------------------------------------------------

    def renumber_changes(self) -> list:
        """
        Propozycja nowych oznaczeń dla powtórzonych i pustych numerów w warstwach.

        Czyta tylko warstwy włączone w zakładce „Strefy i regulacje”, w których mapa pól
        wskazuje ``oznaczenie`` i ``symbol`` — bez tych kolumn numeracja jest wyprowadzana
        przy zapisie i duplikaty nie powstają.
        """
        from ..core.app_numbering import plan_renumbering

        records = []
        for widget in self.sources.values():
            if not widget.isChecked():
                continue
            layer = widget.layer()
            if layer is None:
                continue
            mapping = widget.mapping()
            designation, symbol = mapping.get("oznaczenie"), mapping.get("symbol")
            names = set(layer.fields().names())
            if not designation or not symbol or designation not in names or symbol not in names:
                continue
            for feature in layer.getFeatures():
                records.append((layer.name(), feature.id(), feature[symbol], feature[designation]))
        return plan_renumbering(records)

    def apply_renumbering(self, changes) -> int:
        """Zapisuje nowe oznaczenia w warstwach (okno podglądu otwiera ``_on_numbering_clicked``)."""
        from ..services.app_numbering import apply_changes

        layers = {widget.layer().name(): widget.layer() for widget in self.sources.values()
                  if widget.isChecked() and widget.layer() is not None}
        written = apply_changes(changes, layers, self.local_id.text().strip())
        if written:
            self.numbering_hint.setText(self.tr("Zapisano nowe oznaczenia: {0}.").format(written))
            self._log(f"Kreator APP: przenumerowano oznaczenia — {written}")
        self._refresh()
        return written

    def _on_numbering_clicked(self) -> None:
        changes = self.renumber_changes()
        if not changes:
            self.numbering_hint.setText(self.tr("Brak powtórzonych ani pustych oznaczeń w warstwach."))
            return
        from .app_numbering_dialog import NumberingDialog

        dialog = NumberingDialog(changes, self)
        if dialog.exec():
            self.apply_renumbering(changes)

    # --- model --------------------------------------------------------------------

    def _version_start(self) -> str:
        return self.version_start.dateTime().toUTC().toString("yyyy-MM-dd'T'HH:mm:ss'Z'")

    def build_plan(self) -> tuple:
        """Model pliku z formularza i warstw: ``(AppPlan, uwagi)``."""
        issues: List[PlanIssue] = []
        namespace = self.namespace.text().strip()
        version_id = self.version_id.text().strip()
        version_start = self._version_start()
        effective_from = self.effective_from.date().toString("yyyy-MM-dd")
        effective_to = self.effective_to.value()
        crs = int(self.crs.currentData())
        pog = self.plan_type.currentData() == POG
        geometry = []
        layer = self.extent_layer.currentLayer()
        used = [layer] + [w.layer() for w in self.sources.values() if pog and w.isChecked()]
        for source_layer in {lyr.id(): lyr for lyr in used if lyr is not None}.values():
            epsg = epsg_of(source_layer.crs())
            if epsg != crs:
                issues.append(PlanIssue("warning", "Układ", f"warstwa {source_layer.name()} "
                                        f"({source_layer.crs().authid() or '?'}) jest przeliczana do EPSG:{crs} — "
                                        "przeliczenie i zaokrąglenie może wprowadzić luki i nakładania"))
        if self._cut is not None:
            # Wycięcie obszaru nowego planu: granica pochodzi z okna, nie z warstwy projektu.
            geometry = [list(polygon) for polygon in self._cut.extent]
        elif layer is not None:
            geometry, found = act_geometry(layer, self.extent_selected.isChecked(), crs,
                                           self.extent_dissolve.isChecked())
            issues += found
        act = Act(
            identity=Identity(namespace, self.local_id.text().strip(), version_id),
            title=self.title.text().strip(),
            plan_type=self.plan_type.currentData(),
            level=self.level.currentData(),
            status=self.status.currentData(),
            version_start=version_start,
            version_end=self.version_end.value(),
            effective_from=effective_from,
            effective_to=effective_to,
            alt_titles=[t.strip() for t in self.alt_titles.text().split("|") if t.strip()],
            change_count=self.change_count.value() if (self.change_check.isChecked() and not pog) else None,
            modification=bool(self.modification.currentData()) if pog else None,
            base_maps=[BaseMap(m["date"], m["reference"], m["link"]) for m in self._base_maps()],
            changed_plan=self.changed_plan.text().strip() or None,
            geometry=geometry,
        )
        if self._update:
            expected_ns = str(self._update.get("namespace") or "")
            expected_id = str(self._update.get("local_id") or "")
            if expected_ns and namespace != expected_ns:
                issues.append(PlanIssue("error", "Akt", "przestrzeń nazw nie może się zmienić przy aktualizacji "
                                                      f"aktu (w źródle: {expected_ns})"))
            if expected_id and act.identity.local_id != expected_id:
                issues.append(PlanIssue("error", "Akt", "lokalnyId aktu nie może się zmienić przy aktualizacji "
                                                      f"(w źródle: {expected_id})"))
        documents = [Document(
            identity=Identity(namespace, d["local_id"]), title=d["title"], date=d["date"], role=d["role"],
            date_type=d.get("date_type") or "creation", short_name=d.get("short_name"), number=d.get("number"),
            authority=d.get("authority"), entry_into_force=d.get("entry_into_force"),
            repeal_date=d.get("repeal_date"), detailed_reference=d.get("detailed_reference"),
            journal=d.get("journal"), links=list(d.get("links") or []),
        ) for d in self._documents]
        drawings = [Drawing(
            identity=Identity(namespace, d["local_id"], version_id), title=d["title"], link=d["link"],
            scale=int(d.get("scale") or 0), version_start=version_start, version_end=d.get("version_end"),
            effective_from=d.get("effective_from") or effective_from, crs=d.get("crs"), legend=d.get("legend"),
            description=d.get("description"), effective_to=d.get("effective_to"),
        ) for d in self._drawings]
        drawings = self._cut_drawings(drawings)
        if self._cut is not None:
            source = self._source_for_update()
            if source is not None and source.documents:
                known = {str(document.get("local_id") or "") for document in source.documents}
                if not ({(d.get("local_id") or "") for d in self._documents} - known):
                    issues.append(PlanIssue(
                        "warning", "Dokumenty",
                        "Wycięcie obszaru: dodaj nowy dokument formalny (uchwała o zmianie planu albo "
                        "uchwalenie nowego planu) — nowa wersja aktu nie może opierać się wyłącznie na "
                        "dotychczasowej uchwale"))
        objects = []
        if pog:
            defaults = ObjectDefaults(
                act_local_id=act.identity.local_id, namespace=namespace, version_id=version_id,
                version_start=version_start, effective_from=effective_from, effective_to=effective_to,
                status=self.objects_status.currentData(), nature=self.objects_nature.currentData())
            for kind, widget in self.sources.items():
                if not widget.isChecked():
                    continue
                source_layer = widget.layer()
                if source_layer is None:
                    issues.append(PlanIssue("error", CLASS_LABELS[kind], "nie wybrano warstwy"))
                    continue
                found_objects, found = objects_from_layer(
                    LayerSource(kind, source_layer, widget.selected_only.isChecked(), widget.mapping()),
                    defaults, crs, self._resolver)
                objects += found_objects
                issues += found
        plan = AppPlan(act, crs, documents, drawings, objects, self.precision.value())
        return plan, issues

    def _version_report(self, plan: AppPlan):
        """Raport wersji dla bieżącego stanu formularza (``None``, gdy to nie aktualizacja aktu)."""
        source = self._source_for_update()
        if source is None or source.version_id == plan.act.identity.version_id:
            return None
        return self._versioned(plan, source)[2]

    @staticmethod
    def _version_issues(report) -> List[PlanIssue]:
        """Lista obiektów, które dostaną nową wersję, oraz wycofanych i przenumerowanych."""
        issues = [PlanIssue("info", name, "nowa wersja obiektu (zmieniona treść)") for name in report.changed]
        issues += [PlanIssue("info", name, "nowy obiekt") for name in report.added]
        issues += [PlanIssue("info", name, "obiekt wycofany — zostaje bez nowej wersji, z zamkniętym "
                                           "obowiązywaniem (obowiazujeDo) w pliku nowej wersji")
                   for name in report.retired]
        issues += [PlanIssue("info", "przenumerowanie", f"{line} — nowy obiekt, poprzedni wycofany")
                   for line in report.renamed]
        return issues

    def _geometry_issues(self, plan: AppPlan) -> List[PlanIssue]:
        """
        Kontrole geometrii planu przed zapisem: poprawność, wyjście poza akt, nakładanie, luki.

        Ten sam zestaw co walidator pliku, ale na danych z formularza i warstw — dzięki temu
        luki i nakładania wychodzą przy „Sprawdź dane”, a nie po zapisaniu pliku.
        """
        from ..services.app_geometry import check_plan_geometry

        try:
            result = check_plan_geometry(plan)
        except Exception as e:  # noqa: BLE001 - brak kontroli geometrii nie może psuć sprawdzania danych
            self._log(f"Kreator APP: kontroli geometrii nie wykonano ({e})", "UWAGA")
            return []
        issues: List[PlanIssue] = []
        for finding in result.findings:
            if finding.kind == ACT:
                where = "Akt"
            elif finding.kind:
                where = f"{CLASS_LABELS.get(finding.kind, finding.kind)} {finding.object_id or ''}".strip()
            else:
                where = "Strefy"
            issues.append(PlanIssue(finding.severity, where, finding.message))
        return issues

    def check(self) -> List[PlanIssue]:
        """Sprawdza dane formularza, pokazuje uwagi i uruchamia kontrolę łączy w tle."""
        self._show_required = True
        self._refresh()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        report = None
        plan = None
        try:
            plan, issues = self.build_plan()
            issues = issues + check_plan(plan, self.codes)
            if not any(i.severity == "error" for i in issues):
                report = self._version_report(plan)
                if report is not None:
                    issues = issues + self._version_issues(report)
                # Geometria (luki, nakładania, wyjście poza akt) przed zapisem — nie po nim.
                issues = issues + self._geometry_issues(plan)
        finally:
            QApplication.restoreOverrideCursor()
        # Uwagi z łączy wracają później (sieć), więc trzymamy osobno i pokazujemy razem.
        self._base_issues, self._link_issues = issues, []
        self._summary_plan, self._summary_report = plan, report
        self._show_issues(issues)
        self._show_summary(issues)
        self._check_links()
        return issues

    def _show_summary(self, issues: List[PlanIssue]) -> None:
        """Podsumowanie pod listą uwag: błędy i uwagi, braki w zakładkach, raport wersji."""
        errors = sum(i.severity == "error" for i in issues)
        notes = sum(i.severity == "warning" for i in issues)
        if errors:
            # „Gdzie brakuje danych” jednym zdaniem — zamiast liczenia uwag po całej liście.
            missing = app_checklist.summary(issues)
            theme.set_html(self.summary, theme.span(self.tr('Błędy: {0}').format(errors), "err", bold=True) + ", "
                                 + self.tr("uwagi: {0} — popraw dane przed zapisem.").format(notes)
                                 + ((" " + self.tr("Braki: {0}.").format(missing)) if missing else ""))
            return
        plan = self._summary_plan
        counts = (f"{len(plan.documents)} dok., {len(plan.drawings)} rys., {len(plan.objects)} stref/regulacji"
                  if plan is not None else "")
        text = ("<b style='color:{c:ok}'>" + self.tr("Dane kompletne") + "</b> ("
                + counts + (self.tr(", uwagi: {0}").format(notes) if notes else "") + ").")
        if self._summary_report is not None:
            text += " " + self.tr("Wersje obiektów: {0}.").format(self._summary_report.summary())
        theme.set_html(self.summary, text)

    def _links(self):
        """Usługa sprawdzania łączy (tworzona i podłączana przy pierwszym użyciu)."""
        if self._link_service is None:
            from ..services.app_links import LinkChecker

            self._link_service = LinkChecker(self)
        if not self._links_connected:
            self._link_service.checked.connect(self._on_links_checked)
            self._links_connected = True
        return self._link_service

    def _link_items(self) -> List[tuple]:
        """
        Adresy do sprawdzenia: łącza dokumentów, rysunków i legendy (tylko ``http``/``https``).

        :return: pary ``(adres, miejsce w formularzu)`` — miejsce trafia do listy uwag.
        """
        items = []
        for index, document in enumerate(self._documents, start=1):
            links = [url for url in document.get("links") or [] if is_web(url)]
            for number, url in enumerate(links, start=1):
                where = self.tr("Dokument {0}").format(index) if len(links) == 1 \
                    else self.tr("Dokument {0} (łącze {1})").format(index, number)
                items.append((url, where))
        for index, drawing in enumerate(self._drawings, start=1):
            if is_web(drawing.get("link")):
                items.append((drawing["link"], self.tr("Rysunek {0}").format(index)))
            if is_web(drawing.get("legend")):
                items.append((drawing["legend"], self.tr("Rysunek {0} (legenda)").format(index)))
        return items

    def _check_links(self) -> None:
        """Sprawdza łącza w tle — wynik jako uwagi, nigdy jako blokada zapisu."""
        items = self._link_items()
        if not items:
            return
        self._log(f"Kreator APP: sprawdzam łącza plików ({len(items)})")
        self._links().check(items)

    def _on_links_checked(self, results) -> None:
        """Wynik kontroli łączy: uwagi pod listą danych i wpis w logu okna."""
        self._link_issues = [PlanIssue("warning", result.where, result.message)
                             for result in results if not result.ok]
        for result in results:
            self._log(f"Kreator APP: {result.where} — {result.message}", "INFO" if result.ok else "UWAGA")
        issues = self._base_issues + self._link_issues
        self._show_issues(issues)
        self._show_summary(issues)

    def _show_issues(self, issues: List[PlanIssue]) -> None:
        self._update_tab_titles(issues)
        self.issues.clear()
        ordered = sorted(issues, key=lambda i: i.severity != "error")
        #: Uwagi w kolejności listy — z nich bierze się naprawa zaznaczonej pozycji.
        self._shown_issues = ordered
        self._issues_panel.setVisible(bool(issues))
        self._refresh_fix_button()
        if issues:
            # Ukryty panel nie dostał wysokości przy pokazaniu — oddaj mu ok. 1/3 okna.
            sizes = self._splitter.sizes()
            if len(sizes) < 2 or sizes[1] < 80:
                height = max(self.height(), 480)
                self._splitter.setSizes([height - max(160, height // 3), max(160, height // 3)])
        for issue in ordered:
            item = QTreeWidgetItem(self.issues)
            item.setText(0, _SEVERITY.get(issue.severity, issue.severity))
            theme.set_item_role(item, 0, _COLORS.get(issue.severity, _COLORS["warning"]))
            item.setText(1, issue.where)
            item.setToolTip(1, issue.where)
            item.setText(2, issue.message)
            # „Skąd wziąć wartość”: podpowiedź do komunikatu, nie ozdobnik listy.
            advice = app_checklist.hint(issue)
            item.setToolTip(2, issue.message + ("\n\n" + advice if advice else ""))

    def _update_tab_titles(self, issues: List[PlanIssue]) -> None:
        """Liczniki braków w tytułach zakładek („Akt (2)”) i podpowiedź, co tam poprawić."""
        found = app_checklist.counts(issues)
        keys = (app_checklist.ACT, app_checklist.DOCUMENTS, app_checklist.DRAWINGS, app_checklist.OBJECTS)
        for index, key in enumerate(keys):
            errors, notes = found.get(key, (0, 0))
            self.tabs.setTabText(index, app_checklist.tab_label(self._tab_titles[index], errors))
            self.tabs.setTabToolTip(index, app_checklist.tab_tooltip(errors, notes))

    def _on_issue_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        where = item.text(1)
        if where.startswith(("Akt", "Układ")):
            self.tabs.setCurrentIndex(TAB_ACT)
        elif where.startswith("Dokument"):
            self.tabs.setCurrentIndex(TAB_DOCUMENTS)
        elif where.lower().startswith("rysun"):
            self.tabs.setCurrentIndex(TAB_DRAWINGS)
        else:
            self.tabs.setCurrentIndex(TAB_OBJECTS)

    # --- naprawa zaznaczonej uwagi ----------------------------------------------------

    def _issue_of_selected(self) -> Optional[PlanIssue]:
        """Uwaga zaznaczona na liście komunikatów (``None`` bez zaznaczenia)."""
        item = self.issues.currentItem()
        if item is None:
            return None
        index = self.issues.indexOfTopLevelItem(item)
        if 0 <= index < len(self._shown_issues):
            return self._shown_issues[index]
        return None

    def _refresh_fix_button(self, *args) -> None:
        """„Popraw” aktywne tylko wtedy, gdy zaznaczona uwaga ma przygotowaną naprawę."""
        issue = self._issue_of_selected()
        fix = issue.fix if issue is not None else None
        if self._table_mode:
            # Naprawy zmieniają pola formularza — w trybie tabel formularz jest tylko do odczytu.
            self.btn_fix.setEnabled(False)
            self.btn_fix.setToolTip(self.tr("Tryb tabel: popraw dane w pliku GeoPackage"))
            self.fix_hint.setText(self.tr("Tryb tabel: popraw dane w pliku GeoPackage i kliknij "
                                          "„Odśwież z pliku”."))
            return
        self.btn_fix.setEnabled(bool(fix))
        if fix:
            label = FIX_LABELS.get(fix) or self.tr("Popraw dane aktu")
            self.btn_fix.setToolTip(label)
            self.fix_hint.setText(label + ".")
        else:
            self.btn_fix.setToolTip(self.tr("Zaznacz uwagę na liście"))
            self.fix_hint.setText(self.tr("Zaznacz uwagę, aby ją poprawić jednym kliknięciem."))

    def fix_selected(self) -> bool:
        """
        Wykonuje naprawę zaznaczonej uwagi i sprawdza dane ponownie.

        Naprawy są deterministyczne (status, data, modyfikacja, liczba zmian) albo otwierają
        formularz brakującego obiektu (dokument, rysunek) — nigdy nie zgadują ustaleń planu.

        :return: ``True``, gdy naprawa została wykonana.
        """
        issue = self._issue_of_selected()
        code = issue.fix if issue is not None else None
        handler = {
            "obsolete": self._fix_obsolete,
            "effective-to": self._fix_effective_to,
            "modification": self._fix_modification,
            "change-count": self._fix_change_count,
            "drawing": self._fix_drawing,
            "add-document": self._fix_add_document,
        }.get(code) if code else None
        if issue is None or handler is None:
            return False
        self._log(f"Kreator APP: poprawa uwagi „{issue.message}”")
        handler()
        self._refresh()
        self.check()
        return True

    def _fix_obsolete(self) -> None:
        """Dokument uchylający: status aktu musi być „nieaktualny”."""
        _set_combo(self.status, "obsolete")
        self._log("Kreator APP: status aktu → nieaktualny (obsolete)")

    def _fix_effective_to(self) -> None:
        """Dokument uchylający: „obowiązuje do” z daty uchylenia w dokumencie (albo dziś)."""
        repeal = next((value.get("repeal_date") for value in self._documents
                       if value.get("role") == "uchyla" and value.get("repeal_date")), None)
        self.effective_to.set_value(repeal or QDate.currentDate().toString("yyyy-MM-dd"))
        self._log(f"Kreator APP: obowiązuje do → {self.effective_to.value()}")

    def _fix_modification(self) -> None:
        """Dokument unieważniający w planie ogólnym: akt obowiązuje w części obszaru."""
        _set_combo(self.modification, True)
        self._log("Kreator APP: modyfikacja → tak (akt obowiązuje w części obszaru)")

    def _fix_change_count(self) -> None:
        """Dokument unieważniający w innych aktach: podniesiona liczba zmian."""
        self.change_check.setChecked(True)
        self.change_count.setValue(max(1, self.change_count.value()))
        self._log(f"Kreator APP: liczba zmian → {self.change_count.value()}")

    def _fix_drawing(self) -> None:
        """Brak rysunku w obowiązującym akcie: otwórz formularz rysunku."""
        self.tabs.setCurrentIndex(TAB_DRAWINGS)
        self._add_drawing()

    def _fix_add_document(self) -> None:
        """Brak dokumentu uchwalającego: otwórz formularz dokumentu."""
        self.tabs.setCurrentIndex(TAB_DOCUMENTS)
        self._add_document()

    def _source_for_update(self) -> Optional[ActSource]:
        """Akt źródłowy aktualizacji (przy wczytanym szkicu doczytywany z pliku — bez QGIS)."""
        if self._update is None:
            return None
        if self._update_source is None:
            path = str(self._update.get("path") or "")
            if not path or not Path(path).is_file():
                self._log(f"Kreator APP: brak pliku źródłowego aktualizacji ({path or '?'}) — "
                          "zapis bez porównania wersji", "UWAGA")
                return None
            try:
                self._update_source = read_act_source(path)
            except (AppGmlError, OSError) as e:
                self._log(f"Kreator APP: nie odczytano źródła aktualizacji ({e}) — zapis bez porównania wersji",
                          "UWAGA")
                return None
        return self._update_source

    def _versioned(self, plan: AppPlan, source: ActSource) -> tuple:
        """
        Dwa pliki zamiast jednego: nowa wersja (obiekty bez zmian przy swojej wersji, brakujące
        wycofane) i poprzednia wersja z domkniętymi wersjami aktu, rysunków oraz obiektów
        zmienionych (wycofanych nie domykamy — ich zamknięcie jest w nowej wersji).

        :return: ``(plan nowej wersji, plan poprzedniej wersji, raport)``.
        """
        namespace = str(source.act.get("namespace") or "")
        act_effective_from = str(source.act.get("effective_from") or "")
        objects = reconcile_objects(source.objects, plan.objects, plan.act.version_start)
        report = objects.report
        source_drawings = [drawing_from_values(values, namespace, effective_from=act_effective_from)
                           for values in source.drawings]
        current_drawings, previous_drawings, drawing_report = reconcile_drawings(
            source_drawings, plan.drawings, plan.act.version_start, plan.crs)
        report.merge(drawing_report)
        previous = AppPlan(
            act=act_from_values(source.act, source.geometry, version_end=plan.act.version_start),
            crs=source.crs or plan.crs,
            documents=[document_from_values(values, namespace) for values in source.documents],
            drawings=previous_drawings,
            objects=objects.previous,
            precision=plan.precision,
        )
        self._log(f"Kreator APP: wersje obiektów — {report.summary()}")
        for line in report.renamed:
            self._log(f"Kreator APP: przenumerowanie {line} — to nowy obiekt, poprzedni zostanie domknięty",
                      "UWAGA")
        return replace(plan, objects=objects.current, drawings=current_drawings), previous, report

    def _open_cut(self) -> None:
        """Okno „Wytnij obszar nowego planu…”: nowy zasięg aktu i przycięte rysunki."""
        source = self._source_for_update()
        if source is None:
            self._log("Kreator APP: wycięcie obszaru wymaga wczytanego aktu do aktualizacji", "UWAGA")
            return
        if self.plan_type.currentData() == POG:
            QMessageBox.information(
                self, self.tr("Wytnij obszar nowego planu"),
                self.tr("Wycięcie obszaru w planie ogólnym jest w przygotowaniu — obszary (OUZ, OZS, "
                        "OSDIS) opisuje dokument POG, a nie granica aktu."))
            return
        from .app_cut_dialog import AppCutDialog

        update_path = str((self._update or {}).get("path") or "")
        default_dir = str(Path(self.output.filePath() or update_path).parent) if self.output.filePath() \
            else (str(Path(update_path).parent) if update_path else "")
        dialog = AppCutDialog(self, source=source, crs=int(self.crs.currentData()),
                              version_id=self.version_id.text().strip(),
                              version_start=self._version_start(),
                              act_title=self.title.text().strip(),
                              drawings=source.drawings,
                              default_dir=default_dir,
                              act_path=update_path)
        dialog.set_layers([layer for layer in QgsProject.instance().mapLayers().values()
                           if isinstance(layer, QgsVectorLayer)])
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._cut = dialog.result_cut()
            if self._cut is not None:
                self._log(f"Kreator APP: wycięcie obszaru — {self._cut.report()}")
                for warning in self._cut.warnings:
                    self._log(f"Kreator APP: wycięcie obszaru — {warning}", "UWAGA")
                for decision in self._cut.decisions:
                    if decision.action == "new_version":
                        self._log(f"Kreator APP: rysunek {decision.local_id} — uzupełnij łącze do "
                                  f"przyciętego pliku przed zapisem", "UWAGA")
        self._refresh()

    def _cut_drawings(self, drawings: List[Drawing]) -> List[Drawing]:
        """Rysunki po wycięciu: przycięte w nowej wersji, wycofane, bez zmian (``opis``/``lacze``)."""
        if self._cut is None:
            return drawings
        by_id = {decision.local_id: decision for decision in self._cut.decisions}
        result: List[Drawing] = []
        for drawing in drawings:
            decision = by_id.get(drawing.identity.local_id)
            if decision is None or decision.action == "unchanged":
                result.append(drawing)
            elif decision.action == "retire":
                result.append(replace(drawing, version_end=decision.version_end,
                                      effective_to=decision.effective_to))
            else:
                # Nowy plik nie jest publikowany: łącze zostaje puste do czasu publikacji,
                # a check_plan słusznie wskaże brak adresu (użytkownik uzupełnia go po wgraniu).
                result.append(replace(drawing, description=decision.description, link=""))
        return result

    def generate(self) -> Optional[str]:
        self._show_required = True
        self._refresh()
        path = self.output.filePath().strip()
        if not path:
            return None
        if not path.lower().endswith(".gml"):
            path += ".gml"
            self.output.setFilePath(path)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        previous = None
        report = None
        previous_path = ""
        source = None
        try:
            plan, issues = self.build_plan()
            source = self._source_for_update()
            if source is not None and source.version_id != plan.act.identity.version_id:
                plan, previous, report = self._versioned(plan, source)
                previous_path = self.previous_output.text().strip()
                if previous_path and Path(previous_path) == Path(path):
                    self._log("Kreator APP: plik poprzedniej wersji wskazuje ten sam plik co nowa wersja "
                              "— nie zapisano poprzedniej wersji", "UWAGA")
                    previous = None
            if any(i.severity == "error" for i in issues):
                raise AppWriteError(issues + check_plan(plan, self.codes))
            geometry = self._geometry_issues(plan)
            result = write_app30(plan, path, codes=self.codes)
        except AppWriteError as e:
            QApplication.restoreOverrideCursor()
            self._show_issues(e.issues)
            errors = sum(i.severity == "error" for i in e.issues)
            missing = app_checklist.summary(e.issues)
            theme.set_html(self.summary, "<b style='color:{c:err}'>" + self.tr("Nie zapisano — błędów: {0}.").format(errors)
                                 + "</b> " + self.tr("Popraw dane (lista poniżej, dwuklik przenosi do zakładki).")
                                 + ((" " + self.tr("Braki: {0}.").format(missing)) if missing else ""))
            self._log(f"Kreator APP: nie zapisano {path} — błędów {errors}", "UWAGA")
            return None
        except OSError as e:
            QApplication.restoreOverrideCursor()
            theme.set_html(self.summary, theme.span(str(e), "err"))
            self._log(f"Kreator APP: błąd zapisu {path}: {e}", "BŁĄD")
            return None
        QApplication.restoreOverrideCursor()
        self._result_path = result.path
        shown = issues + result.issues + geometry
        if self._cut is not None:
            shown = shown + [PlanIssue("info", self.tr("Wycięcie obszaru"), self._cut.report())]
            shown = shown + [PlanIssue("info", self.tr("Wycięcie obszaru"), warning)
                             for warning in self._cut.warnings]
        if report is not None:
            shown = shown + self._version_issues(report)
        self._show_issues(shown)
        counts = ", ".join(f"{k}: {v}" for k, v in result.counts.items())
        text = (theme.span(self.tr('Zapisano'), "ok", bold=True) + f" <b>{Path(result.path).name}</b> "
                f"(APP 3.0) — {counts}.")
        if previous is not None and previous_path:
            try:
                old = write_app30(previous, previous_path, codes=self.codes)
            except (AppWriteError, OSError) as e:
                text += " " + self.tr("Nie zapisano poprzedniej wersji: {0}").format(e)
                self._log(f"Kreator APP: nie zapisano poprzedniej wersji {previous_path}: {e}", "UWAGA")
            else:
                text += " " + self.tr("Poprzednia wersja: {0} ({1}).").format(
                    Path(old.path).name, report.summary() if report else "")
                self._log(f"Kreator APP: zapisano poprzednią wersję {old.path} "
                          f"({report.summary() if report else ''})")
        theme.set_html(self.summary, text)
        self._log(f"Kreator APP: zapisano {result.path} ({counts})")
        self._refresh()
        return result.path

    def validate_result(self) -> bool:
        if not self._result_path or self._open_validation is None:
            return False
        self._open_validation(self._result_path)
        return True

    def add_result(self) -> bool:
        if not self._result_path or self._add_file is None:
            return False
        self._add_file(self._result_path)
        return True

    # --- szkic ----------------------------------------------------------------------

    def state(self) -> dict:
        layer = self.extent_layer.currentLayer()
        return {
            "format": "gs-mapa-app30-draft", "version": DRAFT_VERSION,
            "act": {
                "namespace": self.namespace.text().strip(), "local_id": self.local_id.text().strip(),
                "version_id": self.version_id.text().strip(), "version_start": self._version_start(),
                "version_end": self.version_end.value(),
                "title": self.title.text(), "alt_titles": self.alt_titles.text(),
                "plan_type": self.plan_type.currentData(), "level": self.level.currentData(),
                "status": self.status.currentData(),
                "effective_from": self.effective_from.date().toString("yyyy-MM-dd"),
                "effective_to": self.effective_to.value(), "modification": bool(self.modification.currentData()),
                "change_count": self.change_count.value() if self.change_check.isChecked() else None,
                "changed_plan": self.changed_plan.text().strip(), "base_maps": self._base_maps(),
            },
            "extent": {"layer_id": layer.id() if layer else None, "layer_name": layer.name() if layer else None,
                       "selected_only": self.extent_selected.isChecked(),
                       "dissolve": self.extent_dissolve.isChecked()},
            "crs": int(self.crs.currentData()), "precision": self.precision.value(),
            "documents": list(self._documents), "drawings": list(self._drawings),
            "objects": {"status": self.objects_status.currentData(), "nature": self.objects_nature.currentData(),
                        **{kind: widget.state() for kind, widget in self.sources.items()}},
            "output": self.output.filePath().strip(),
            "previous_output": self.previous_output.text().strip(),
            "update": dict(self._update) if self._update else None,
        }

    def set_state(self, state: dict) -> None:
        """Stan formularza ze szkicu (bez samoczynnej zmiany przestrzeni nazw)."""
        if state.get("format") != "gs-mapa-app30-draft":
            raise ValueError(self.tr("To nie jest szkic kreatora APP 3.0."))
        loading, self._loading = self._loading, True
        try:
            self._set_state(state)
        finally:
            self._loading = loading
        self._set_namespace_hint("")
        self._show_update_info()

    def _set_state(self, state: dict) -> None:
        act = state.get("act", {})
        plan_type = act.get("plan_type", POG)
        if self.plan_type.findData(plan_type) < 0:
            # Typ aktu spoza jednostki kreatora: jednostka z przestrzeni nazw szkicu (albo żadna).
            unit = self._unit_for(act.get("namespace") or "", plan_type)
            self._log(f"Kreator APP: typ aktu {plan_type} spoza jednostki — jednostka kreatora: "
                      f"{unit.label if unit else 'bez jednostki'}", "UWAGA")
            self.set_unit(unit)
        self._update = dict(state["update"]) if state.get("update") else None
        self._update_source = None
        self._cut = None
        self._lock_identity(self._update is not None)
        self.previous_output.setText(state.get("previous_output") or "")
        self.namespace.setText(act.get("namespace") or "")
        self.local_id.setText(act.get("local_id") or "")
        self.version_id.setText(act.get("version_id") or "")
        if act.get("version_start"):
            self.version_start.setDateTime(QDateTime.fromString(act["version_start"], Qt.DateFormat.ISODate)
                                           .toLocalTime())
        self.version_end.set_value(act.get("version_end"))
        self.title.setText(act.get("title") or "")
        self.alt_titles.setText(act.get("alt_titles") or "")
        _set_combo(self.plan_type, plan_type)
        self._on_type_changed()
        _set_combo(self.level, act.get("level"))
        _set_combo(self.status, act.get("status", "legalForce"))
        if act.get("effective_from"):
            self.effective_from.setDate(QDate.fromString(act["effective_from"], "yyyy-MM-dd"))
        # Data z pliku lub szkicu jest wartością źródłową — uchwała jej nie nadpisze bez decyzji.
        self._auto_effective = self.effective_from.date().toString("yyyy-MM-dd")
        self._set_effective_hint("")
        self.effective_to.set_value(act.get("effective_to"))
        _set_combo(self.modification, bool(act.get("modification")))
        self.change_check.setChecked(act.get("change_count") is not None)
        self.change_count.setValue(int(act.get("change_count") or 0))
        self.changed_plan.setText(act.get("changed_plan") or "")
        self.base_maps.setRowCount(0)
        for value in act.get("base_maps") or []:
            self._add_base_map(value=value)
        extent = state.get("extent", {})
        self.extent_layer.setLayer(_find_layer(extent.get("layer_id"), extent.get("layer_name")))
        self.extent_selected.setChecked(bool(extent.get("selected_only")))
        self.extent_dissolve.setChecked(extent.get("dissolve", True))
        _set_combo(self.crs, state.get("crs", 2180))
        self.precision.setValue(int(state.get("precision", 8)))
        self._documents = list(state.get("documents") or [])
        self._drawings = list(state.get("drawings") or [])
        self._fill_documents()
        self._fill_drawings()
        objects = state.get("objects", {})
        _set_combo(self.objects_status, objects.get("status", "legalForce"))
        _set_combo(self.objects_nature, objects.get("nature", "generallyBinding"))
        for kind, widget in self.sources.items():
            if kind in objects:
                widget.set_state(objects[kind])
        self.output.setFilePath(state.get("output") or "")
        self._refresh()

    def save_draft(self, path: str) -> str:
        Path(path).write_text(json.dumps(self.state(), ensure_ascii=False, indent=1), encoding="utf-8")
        self._log(f"Kreator APP: zapisano szkic {path}")
        return path

    def load_draft(self, path: str) -> None:
        self.set_state(json.loads(Path(path).read_text(encoding="utf-8")))
        self._log(f"Kreator APP: wczytano szkic {path}")

    def _on_save_clicked(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, self.tr("Zapisz szkic kreatora"), "",
                                              self.tr("Szkic APP (*.app30.json)"))
        if path:
            if not path.endswith(".json"):
                path += ".app30.json"
            self.save_draft(path)

    def _on_load_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.tr("Wczytaj szkic kreatora"), "",
                                              self.tr("Szkic APP (*.app30.json *.json)"))
        if not path:
            return
        try:
            self.load_draft(path)
        except (OSError, ValueError, KeyError) as e:
            QMessageBox.warning(self, self.tr("Szkic kreatora"), str(e))

    def closeEvent(self, event) -> None:  # noqa: N802 - nazwa z API Qt
        self.btn_pick.setChecked(False)
        self._release_pick_tool()
        if self._namespace_dialog is not None:
            self._namespace_dialog.close()
        if self._act_picker is not None:
            self._act_picker.close()
        if self._ns_pending is not None:
            # Odpowiedź EZiUDP po zamknięciu (i usunięciu) okna nie ma dokąd trafić.
            cancel = getattr(self._lookup_service, "cancel_search", None)
            if cancel is not None:
                cancel()
            self._ns_pending = None
        cancel_links = getattr(self._link_service, "cancel", None)
        if cancel_links is not None:
            # Odpowiedź HTTP po zamknięciu okna nie ma dokąd trafić.
            cancel_links()
        super().closeEvent(event)
