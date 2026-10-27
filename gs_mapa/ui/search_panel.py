"""
Sekcja „Akty planistyczne” karty Plany: jedna lista aktów z Rejestru Urbanistycznego.

Lista pokazuje akty jednostki z paska kontekstu (bez jednostki — wynik wyszukiwania w całym
indeksie). Pole szukania filtruje po tytule (z numerem uchwały), gminie i identyfikatorze aktu;
chipy POG / MPZP / ZPI / PZPW (wielokrotny wybór) i lista statusów zawężają wynik.

Wiersz (model + delegat): plakietka typu, tytuł, „status · data”, po prawej „na mapie”.
Dwuklik / Enter = „Przybliż do planu”; prawy klik = menu z pełną listą akcji; Ctrl+I =
„Informacja o planie”. Panel nie zna sieci ani mapy — akcje zgłasza sygnałami.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Set

from qgis.PyQt.QtCore import (
    QAbstractListModel, QCoreApplication, QModelIndex, QRect, QSize, Qt, QTimer, QUrl, pyqtSignal,
)
from qgis.PyQt.QtGui import (
    QDesktopServices, QFont, QFontMetrics, QGuiApplication, QKeySequence, QPainter, QPen, QShortcut,
)
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListView,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)
from qgis.gui import QgsFilterLineEdit

from ..core.act_index import ACT_STATUSES, ActIndexEntry, search_acts, short_title, status_label
from ..core.config import DATA_TYPES
from ..core.labels import format_date, is_inactive_status, plural_acts, type_description, type_label, type_role
from ..core.units import context_acts
from ..services.acts import registry_url
from . import theme
from .widgets import make_chip

logger = logging.getLogger("GSMapa.Search")

#: Rola danych wiersza: wpis indeksu.
ENTRY_ROLE = Qt.ItemDataRole.UserRole + 1
#: Rola danych wiersza: akt ma warstwy na mapie.
ON_MAP_ROLE = Qt.ItemDataRole.UserRole + 2

#: Maksymalna liczba wierszy na liście — powyżej prosimy o zawężenie zapytania.
MAX_RESULTS = 200
#: Najwięcej wierszy widocznych bez przewijania listy (lista nie wypełnia pustej przestrzeni).
VISIBLE_ROWS = 8

#: Opóźnienie między wpisaniem znaku a wyszukaniem (ms).
_DEBOUNCE_MS = 200


def _tr(text: str) -> str:
    return QCoreApplication.translate("ActSearchPanel", text)


def entry_title(entry: ActIndexEntry) -> str:
    return short_title(entry.tytul) or type_description(entry.typ)


def entry_subtitle(entry: ActIndexEntry) -> str:
    """Druga linia wiersza: ``obowiązujący · od 14.05.2024``, ``uchylony · do 12.01.2026``."""
    status = (status_label(entry.status) or entry.status or _tr("status nieznany")).lower()
    date = ""
    if is_inactive_status(entry.status) and entry.obowiazuje_do:
        date = f"{_tr('do')} {format_date(entry.obowiazuje_do)}"
    elif entry.obowiazuje_od:
        date = f"{_tr('od')} {format_date(entry.obowiazuje_od)}"
    return f"{status} · {date}" if date else status


def entry_tooltip(entry: ActIndexEntry) -> str:
    lines = [" ".join((entry.tytul or type_description(entry.typ)).split()),
             f"{entry.typ.upper()} — {type_label(entry.typ.upper())}"]
    place = ", ".join(x for x in (
        f"gm. {entry.gmina}" if entry.gmina else "",
        f"pow. {entry.powiat}" if entry.powiat else "",
        f"woj. {entry.wojewodztwo.lower()}" if entry.wojewodztwo else "",
    ) if x)
    if place:
        lines.append(place)
    if entry.terc:
        lines.append(f"TERYT {entry.terc}")
    if entry.status:
        lines.append(f"Status: {entry.status}")
    if entry.obowiazuje_od:
        validity = f"Obowiązuje od {format_date(entry.obowiazuje_od)}"
        if entry.obowiazuje_do:
            validity += f" do {format_date(entry.obowiazuje_do)}"
        lines.append(validity)
    lines.append(f"Identyfikator aktu: {entry.act_id}")
    return "\n".join(lines)


class ActListModel(QAbstractListModel):
    """Akty widoczne na liście; stan „na mapie” z zestawu identyfikatorów aktów."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: List[ActIndexEntry] = []
        self._on_map: Set[str] = set()

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._entries)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._entries):
            return None
        entry = self._entries[index.row()]
        if role == ENTRY_ROLE:
            return entry
        if role == ON_MAP_ROLE:
            return entry.act_id in self._on_map
        if role == Qt.ItemDataRole.DisplayRole:
            return entry_title(entry)
        if role == Qt.ItemDataRole.ToolTipRole:
            return entry_tooltip(entry)
        if role == Qt.ItemDataRole.AccessibleTextRole:
            on_map = f", {_tr('na mapie')}" if entry.act_id in self._on_map else ""
            return f"{entry.typ.upper()} {entry_title(entry)}, {entry_subtitle(entry)}{on_map}"
        return None

    def set_entries(self, entries: List[ActIndexEntry]) -> None:
        self.beginResetModel()
        self._entries = list(entries)
        self.endResetModel()

    def entries(self) -> List[ActIndexEntry]:
        return list(self._entries)

    def set_on_map(self, act_ids: Set[str]) -> None:
        self._on_map = set(act_ids)
        if self._entries:
            self.dataChanged.emit(self.index(0), self.index(len(self._entries) - 1), [ON_MAP_ROLE])

    def on_map(self) -> Set[str]:
        return set(self._on_map)


class ActDelegate(QStyledItemDelegate):
    """Wiersz aktu: plakietka typu (obrys + tekst w kolorze typu), tytuł, „status · data”, „na mapie”."""

    PADDING = 7
    SPACING = 8

    @staticmethod
    def _fonts(option):
        base = QFont(option.font)
        small = QFont(base)
        if small.pointSizeF() > 0:
            small.setPointSizeF(max(small.pointSizeF() - 1, 6))
        badge = QFont(small)
        badge.setBold(True)
        return base, small, badge

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        base, small, _ = self._fonts(option)
        height = QFontMetrics(base).height() + QFontMetrics(small).height() + 2 * self.PADDING + 2
        return QSize(200, height)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        entry: ActIndexEntry = index.data(ENTRY_ROLE)
        if entry is None:
            super().paint(painter, option, index)
            return
        widget = option.widget
        style = widget.style() if widget is not None else QApplication.style()
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        palette = option.palette
        text_color = palette.highlightedText().color() if selected else palette.text().color()
        inactive = is_inactive_status(entry.status)
        muted = text_color if selected else theme.muted_text(widget)
        base, small, badge_font = self._fonts(option)
        rect = option.rect.adjusted(self.PADDING + 1, self.PADDING, -self.PADDING - 1, -self.PADDING)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Plakietka typu — stała szerokość (najdłuższy kod), obrys i tekst w kolorze typu.
        badge_metrics = QFontMetrics(badge_font)
        badge_width = badge_metrics.horizontalAdvance("PZPW") + 12
        badge_height = badge_metrics.height() + 4
        badge_rect = QRect(rect.left(), rect.top() + (rect.height() - badge_height) // 2, badge_width, badge_height)
        if selected:
            badge_color = text_color
        elif inactive:
            badge_color = theme.muted_text(widget)
        else:
            badge_color = theme.semantic(type_role(entry.typ), widget)
        painter.setPen(QPen(badge_color, 1.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(badge_rect.adjusted(0, 0, -1, -1), 4, 4)
        painter.setFont(badge_font)
        painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, entry.typ.upper())

        # Po prawej: „na mapie” (ikona warstw + tekst w kolorze ok).
        right = rect.right()
        if index.data(ON_MAP_ROLE):
            ok = text_color if selected else theme.semantic("ok", widget)
            label = _tr("na mapie")
            metrics = QFontMetrics(small)
            label_width = metrics.horizontalAdvance(label)
            icon_size = theme.ROW_SIZE
            x = right - label_width
            painter.setFont(small)
            painter.setPen(ok)
            painter.drawText(QRect(x, rect.top(), label_width, rect.height()), Qt.AlignmentFlag.AlignVCenter, label)
            pixmap = theme.icon("layers", "HighlightedText" if selected else "ok").pixmap(icon_size, icon_size)
            painter.drawPixmap(x - icon_size - 4, rect.top() + (rect.height() - icon_size) // 2, pixmap)
            right = x - icon_size - 4 - self.SPACING

        # Tytuł (elidowany na końcu) i „status · data”.
        left = badge_rect.right() + self.SPACING + 2
        width = max(right - left, 10)
        base_metrics, small_metrics = QFontMetrics(base), QFontMetrics(small)
        title_rect = QRect(left, rect.top(), width, base_metrics.height())
        sub_rect = QRect(left, title_rect.bottom() + 2, width, small_metrics.height())
        painter.setFont(base)
        painter.setPen(muted if inactive and not selected else text_color)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignVCenter,
                         base_metrics.elidedText(entry_title(entry), Qt.TextElideMode.ElideRight, width))
        painter.setFont(small)
        painter.setPen(muted)
        painter.drawText(sub_rect, Qt.AlignmentFlag.AlignVCenter,
                         small_metrics.elidedText(entry_subtitle(entry), Qt.TextElideMode.ElideRight, width))
        painter.restore()


class ActListView(QListView):
    """Lista aktów: Enter = akcja domyślna; wysokość dopasowana do liczby wierszy (bez pustej przestrzeni)."""

    activated_default = pyqtSignal(QModelIndex)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.currentIndex().isValid():
            self.activated_default.emit(self.currentIndex())
            return
        super().keyPressEvent(event)

    def fit_height(self) -> None:
        rows = min(self.model().rowCount() if self.model() else 0, VISIBLE_ROWS)
        row_height = self.sizeHintForRow(0) if rows else 0
        self.setFixedHeight(rows * row_height + 2 * self.frameWidth() if rows else 0)


class ActSearchPanel(QWidget):
    """
    Akty planistyczne: pole szukania, chipy typu, status, lista z menu kontekstowym.

    Sygnały niosą wpis indeksu (``ActIndexEntry``).
    """

    #: Przybliż do planu (dwuklik, Enter, menu).
    show_on_map = pyqtSignal(object)
    #: Okno „Informacja o planie” (menu, Ctrl+I).
    details_requested = pyqtSignal(object)
    #: Dodaj / usuń warstwy planu (plik GPKG aktu z bucketa).
    add_layers_requested = pyqtSignal(object)
    remove_layers_requested = pyqtSignal(object)
    #: Nowa wersja aktu w kreatorze GML APP 3.0.
    new_version_requested = pyqtSignal(object)
    #: Zmieniły się filtry typu/statusu (do zapamiętania w ustawieniach).
    filters_changed = pyqtSignal()
    #: Zmieniło się zaznaczenie (akt albo ``None``) — „Nowa wersja aktu z Rejestru…” na karcie Pliki APP.
    selection_changed = pyqtSignal(object)
    #: Stan pusty: użytkownik chce wybrać jednostkę.
    choose_unit_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: List[ActIndexEntry] = []
        #: Jednostka paska kontekstu — lista pokazuje jej akty (``None`` = cały indeks, trzeba wpisać zapytanie).
        self._unit = None
        self._available = False
        self._setup_ui()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(_DEBOUNCE_MS)
        self._timer.timeout.connect(self._run_search)
        self.set_loading()

    # --- budowa ------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._query = QgsFilterLineEdit()
        self._query.setShowSearchIcon(True)
        self._query.setShowClearButton(True)
        self._query.setAccessibleName(self.tr("Szukaj aktów"))
        self._query.setToolTip(self.tr(
            "Tytuł (z numerem uchwały), gmina albo identyfikator aktu; bez jednostki — także kod TERYT"
        ))
        self._query.textChanged.connect(self._on_query_changed)
        self._query.returnPressed.connect(self._run_search)
        layout.addWidget(self._query)

        # Chipy typu w jednym wierszu.
        filters_widget = QWidget()
        filters = QHBoxLayout(filters_widget)
        filters.setContentsMargins(0, 0, 0, 0)
        filters.setSpacing(6)
        self._type_buttons = {}
        for layer_type in DATA_TYPES:
            button = make_chip(layer_type)
            button.setToolTip(type_description(layer_type))
            button.setAccessibleName(self.tr("Typ {0}").format(layer_type))
            button.setChecked(True)
            button.toggled.connect(self._on_filter_changed)
            filters.addWidget(button)
            self._type_buttons[layer_type.lower()] = button
        filters.addStretch(1)
        layout.addWidget(filters_widget)

        # Wymuś minimalną szerokość, aby wszystkie chipy mieściły się w jednej linii.
        min_width = sum(btn.minimumWidth() for btn in self._type_buttons.values())
        min_width += 6 * max(0, len(self._type_buttons) - 1)
        filters_widget.setMinimumWidth(min_width)

        # Status w osobnym wierszu, na całą szerokość.
        self._status = QComboBox()
        self._status.setToolTip(self.tr("Status aktu w Rejestrze Urbanistycznym"))
        self._status.setAccessibleName(self.tr("Status aktu"))
        self._status.addItem(self.tr("Każdy status"), "")
        for value, label in ACT_STATUSES:
            self._status.addItem(label, value)
        self._status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._status.currentIndexChanged.connect(self._on_filter_changed)
        layout.addWidget(self._status)

        self._model = ActListModel(self)
        self._results = ActListView()
        self._results.setModel(self._model)
        self._results.setItemDelegate(ActDelegate(self._results))
        self._results.setAlternatingRowColors(True)
        self._results.setUniformItemSizes(True)
        self._results.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._results.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._results.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._results.setAccessibleName(self.tr("Akty planistyczne"))
        self._results.doubleClicked.connect(self._on_default_action)
        self._results.activated_default.connect(self._on_default_action)
        self._results.customContextMenuRequested.connect(self._on_context_menu)
        self._results.selectionModel().currentChanged.connect(self._on_current_changed)
        layout.addWidget(self._results)

        # Stan pusty: jedna linia + jedna akcja (pkt 6.7).
        self._empty = QWidget()
        empty = QHBoxLayout(self._empty)
        empty.setContentsMargins(0, 0, 0, 0)
        self._empty_label = QLabel()
        self._empty_label.setWordWrap(True)
        theme.style_muted(self._empty_label)
        self._btn_choose_unit = QPushButton(self.tr("Wybierz jednostkę…"))
        self._btn_choose_unit.clicked.connect(self.choose_unit_requested)
        empty.addWidget(self._empty_label, 1)
        empty.addWidget(self._btn_choose_unit, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._empty)

        self._summary = QLabel()
        self._summary.setWordWrap(True)
        theme.style_muted(self._summary, small=True)
        layout.addWidget(self._summary)

        shortcut = QShortcut(QKeySequence("Ctrl+I"), self._results)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(self._details_for_current)
        self._update_placeholder()

    # --- stan indeksu ------------------------------------------------------

    def set_loading(self) -> None:
        """Indeks w trakcie pobierania."""
        self._available = False
        self._set_enabled(False)
        self._show_empty(self.tr("Pobieranie indeksu aktów…"))

    def set_unavailable(self, message: str) -> None:
        """Indeks niedostępny (niepublikowany albo błąd sieci)."""
        self._available = False
        self._entries = []
        self._set_enabled(False)
        self._show_empty(self.tr("Lista aktów niedostępna: {0} Spróbuj „Odśwież indeks”.").format(message))

    def set_entries(self, entries: List[ActIndexEntry]) -> None:
        """Nowy indeks — odświeża wyniki dla bieżącego zapytania."""
        self._entries = list(entries)
        self._available = True
        self._set_enabled(True)
        self._run_search()

    @property
    def entries(self) -> List[ActIndexEntry]:
        return self._entries

    def set_unit(self, unit) -> None:
        """Jednostka z paska kontekstu: lista zawęża się do jej aktów."""
        self._unit = unit
        self._update_placeholder()
        if self._available:
            self._run_search()

    def unit(self):
        return self._unit

    def set_on_map(self, act_ids) -> None:
        """Akty, których warstwy są na mapie (``gsmapa/idIIP`` warstw projektu)."""
        self._model.set_on_map(set(act_ids))

    def _update_placeholder(self) -> None:
        self._query.setPlaceholderText(
            self.tr("Tytuł, numer uchwały albo identyfikator") if self._unit is not None
            else self.tr("Gmina, tytuł albo kod TERYT")
        )

    # --- filtry ------------------------------------------------------------

    def selected_types(self) -> List[str]:
        """Zaznaczone typy aktów (małymi literami, jak w indeksie)."""
        return [typ for typ, button in self._type_buttons.items() if button.isChecked()]

    def set_selected_types(self, types: List[str]) -> None:
        wanted = {t.lower() for t in types}
        for typ, button in self._type_buttons.items():
            button.blockSignals(True)
            button.setChecked(typ in wanted)
            button.blockSignals(False)
            # Ikona ✓ chipa nadąża za stanem także przy zablokowanych sygnałach.
            button._on_toggled(button.isChecked())
        self._schedule()

    def selected_status(self) -> str:
        """Wybrany status aktu albo ``""`` (każdy)."""
        return self._status.currentData() or ""

    def set_selected_status(self, status: str) -> None:
        index = self._status.findData(status)
        self._status.setCurrentIndex(index if index >= 0 else 0)

    def query(self) -> str:
        return self._query.text()

    def set_query(self, text: str) -> None:
        self._query.setText(text)

    def focus_search(self) -> None:
        self._query.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self._query.selectAll()

    # --- wyniki ------------------------------------------------------------

    def results(self) -> List[ActIndexEntry]:
        """Akty aktualnie widoczne na liście."""
        return self._model.entries()

    def current_entry(self) -> Optional[ActIndexEntry]:
        """Zaznaczony akt albo ``None``."""
        index = self._results.currentIndex()
        if not index.isValid() or not self._results.selectionModel().isSelected(index):
            return None
        return index.data(ENTRY_ROLE)

    def select_entry(self, act_id: str) -> bool:
        for row, entry in enumerate(self._model.entries()):
            if entry.act_id == act_id:
                self._results.setCurrentIndex(self._model.index(row))
                return True
        return False

    def summary(self) -> str:
        return self._summary.text()

    def empty_text(self) -> str:
        return self._empty_label.text() if not self._empty.isHidden() else ""

    def _on_query_changed(self, text: str) -> None:
        self._schedule()

    def _on_filter_changed(self, *args) -> None:
        self.filters_changed.emit()
        self._schedule()

    def _schedule(self) -> None:
        if self._available:
            self._timer.start()

    def _run_search(self) -> None:
        self._timer.stop()
        if not self._available:
            return
        query = self._query.text().strip()
        status = self.selected_status()
        types = self.selected_types()
        if self._unit is None and not query and not status:
            self._show_empty(self.tr("Wybierz jednostkę albo wpisz nazwę gminy, aby zobaczyć akty."),
                             choose_unit=True)
            return
        pool = context_acts(self._entries, self._unit) if self._unit is not None else self._entries
        found = search_acts(pool, query, types=types, statuses=[status] if status else None, limit=0)
        if not found:
            text = (self.tr("Jednostka nie ma aktów w Rejestrze Urbanistycznym.") if not pool
                    else self.tr("Brak aktów spełniających kryteria — zmień filtry."))
            self._show_empty(text)
            return
        self._fill(found[:MAX_RESULTS])
        self._empty.setVisible(False)
        if len(found) > MAX_RESULTS:
            summary = self.tr("Pokazano {0} z {1} — zawęź wyszukiwanie").format(MAX_RESULTS, plural_acts(len(found)))
        else:
            summary = f"{plural_acts(len(found))} · {self.tr('dwuklik przybliża, prawy klik otwiera akcje')}"
        self._summary.setText(summary)
        self._summary.setVisible(True)

    def _fill(self, entries: List[ActIndexEntry]) -> None:
        current = self.current_entry()
        self._model.set_entries(entries)
        self._results.setVisible(bool(entries))
        self._results.fit_height()
        if current is not None:
            self.select_entry(current.act_id)
        self.selection_changed.emit(self.current_entry())

    def _show_empty(self, text: str, choose_unit: bool = False) -> None:
        self._fill([])
        self._empty_label.setText(text)
        self._btn_choose_unit.setVisible(choose_unit)
        self._empty.setVisible(True)
        self._summary.setVisible(False)
        self._summary.setText("")

    def _set_enabled(self, enabled: bool) -> None:
        self._query.setEnabled(enabled)
        self._status.setEnabled(enabled)
        for button in self._type_buttons.values():
            button.setEnabled(enabled)

    # --- akcje -----------------------------------------------------------

    def _on_current_changed(self, *args) -> None:
        self.selection_changed.emit(self.current_entry())

    def _on_default_action(self, index: QModelIndex) -> None:
        entry = index.data(ENTRY_ROLE)
        if entry is None:
            return
        if entry.bbox is not None:
            self.show_on_map.emit(entry)
        else:
            # Bez zasięgu w indeksie nie ma do czego przybliżyć — pokaż akt w oknie informacji.
            self.details_requested.emit(entry)

    def _details_for_current(self) -> None:
        entry = self.current_entry()
        if entry is not None:
            self.details_requested.emit(entry)

    def context_menu(self, entry: ActIndexEntry) -> QMenu:
        """Menu kontekstowe aktu z pełną listą akcji."""
        menu = QMenu(self._results)
        menu.setToolTipsVisible(True)
        zoom = menu.addAction(theme.icon("zoom"), self.tr("Przybliż do planu"))
        zoom.setShortcut(QKeySequence(Qt.Key.Key_Return))
        font = zoom.font()
        font.setBold(True)
        zoom.setFont(font)
        zoom.setEnabled(entry.bbox is not None)
        zoom.setToolTip("" if entry.bbox is not None else self.tr("Indeks nie podaje zasięgu tego aktu"))
        zoom.triggered.connect(lambda: self.show_on_map.emit(entry))
        info = menu.addAction(theme.icon("plan-info"), self.tr("Informacja o planie"))
        info.setShortcut(QKeySequence("Ctrl+I"))
        info.triggered.connect(lambda: self.details_requested.emit(entry))
        if entry.act_id in self._model.on_map():
            layers = menu.addAction(theme.icon("remove-layers"), self.tr("Usuń z mapy"))
            layers.setToolTip(self.tr("Usuń warstwy planu z mapy"))
            layers.triggered.connect(lambda: self.remove_layers_requested.emit(entry))
        else:
            layers = menu.addAction(theme.icon("layers"), self.tr("Dodaj do mapy"))
            layers.setToolTip(self.tr("Granica i obiekty aktu z pliku GPKG opublikowanego przez GS Mapa"))
            layers.triggered.connect(lambda: self.add_layers_requested.emit(entry))
        version = menu.addAction(theme.icon("wand"), self.tr("Nowa wersja w kreatorze…"))
        version.triggered.connect(lambda: self.new_version_requested.emit(entry))
        menu.addSeparator()
        copy = menu.addAction(theme.icon("copy"), self.tr("Kopiuj identyfikator aktu"))
        copy.setToolTip(entry.act_id)
        copy.triggered.connect(lambda: QGuiApplication.clipboard().setText(entry.act_id))
        url = registry_url(entry.act_id)
        registry = menu.addAction(theme.icon("external"), self.tr("Otwórz w Rejestrze Urbanistycznym"))
        registry.setEnabled(bool(url))
        registry.setToolTip(url or self.tr("Nieznany adres aktu w Rejestrze"))
        registry.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
        return menu

    def _on_context_menu(self, pos) -> None:
        index = self._results.indexAt(pos)
        entry = index.data(ENTRY_ROLE) if index.isValid() else None
        if entry is None:
            return
        self._results.setCurrentIndex(index)
        menu = self.context_menu(entry)
        menu.exec(self._results.viewport().mapToGlobal(pos))
        menu.deleteLater()

    def tr(self, text: str) -> str:
        return QCoreApplication.translate("ActSearchPanel", text)
