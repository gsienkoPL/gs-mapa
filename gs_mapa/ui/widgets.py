"""Drobne widżety wspólne dla kart panelu: etykieta z elidowaniem, układ zawijany, sekcja zwijana."""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence, Tuple

from qgis.PyQt.QtCore import QPoint, QRect, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QFontMetrics, QTextLayout, QTextOption
from qgis.PyQt.QtWidgets import (
    QLabel,
    QLayout,
    QLineEdit,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
    QWidgetItem,
)


class ElidedLabel(QLabel):
    """
    Etykieta jednowierszowa, która przy braku miejsca elides tekst (``…``) i pokazuje pełny
    tekst w podpowiedzi — w wąskim doku nic nie jest ucięte bez możliwości odczytu (pkt 7).
    """

    def __init__(self, text: str = "", mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight, parent=None):
        super().__init__(parent)
        self._full = ""
        self._mode = mode
        self._own_tooltip = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(20)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - nazwa z API Qt
        self._full = text or ""
        self._update()

    def text(self) -> str:
        return self._full

    def setToolTip(self, text: str) -> None:  # noqa: N802 - nazwa z API Qt
        self._own_tooltip = text or ""
        self._update()

    def setElideMode(self, mode: Qt.TextElideMode) -> None:  # noqa: N802
        self._mode = mode
        self._update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        self._update()

    def sizeHint(self) -> QSize:  # noqa: N802
        # Z pełnego tekstu, nie z elidowanego — inaczej raz zwężona etykieta nie urośnie z powrotem
        # (ma znaczenie przy polityce innej niż ``Ignored``; ``Ignored`` pomija sizeHint).
        hint = super().sizeHint()
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + 4, hint.height())

    def _update(self) -> None:
        width = max(self.width(), 10)
        shown = self.fontMetrics().elidedText(self._full, self._mode, width)
        QLabel.setText(self, shown)
        tooltip = self._own_tooltip or (self._full if shown != self._full else "")
        QLabel.setToolTip(self, tooltip)


class FlowLayout(QLayout):
    """
    Układ zawijany (przyciski paska akcji w wąskim doku przechodzą do kolejnego wiersza).

    ``QgsFlowLayout`` nie jest dostępny z Pythona — to jego odpowiednik według przykładu Qt.
    """

    def __init__(self, parent: Optional[QWidget] = None, spacing: int = 6):
        super().__init__(parent)
        self._items: List = []
        self.setContentsMargins(0, 0, 0, 0)
        self._spacing = spacing

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def addWidget(self, widget: QWidget) -> None:  # noqa: N802
        self.addChildWidget(widget)
        self.addItem(QWidgetItem(widget))

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, line_height = effective.x(), effective.y(), 0
        for item in self._items:
            widget = item.widget()
            if widget is not None and widget.isHidden():
                continue
            hint = item.sizeHint()
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing > effective.right() + 1 and line_height > 0:
                x = effective.x()
                y = y + line_height + self._spacing
                next_x = x + hint.width() + self._spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


class Section(QWidget):
    """
    Zwijana sekcja karty (pkt 6.2): nagłówek z pogrubionym tytułem po lewej i kontekstem
    (tekst pomocniczy) po prawej; stan zwinięcia zapamiętywany w ustawieniach wtyczki.

    Odpowiednik ``QgsCollapsibleGroupBox`` z kontekstem w nagłówku (grupa QGIS ma tylko tytuł).
    """

    def __init__(self, title: str, key: str, config=None, parent=None):
        from qgis.PyQt.QtWidgets import QHBoxLayout, QToolButton, QVBoxLayout

        from . import theme

        super().__init__(parent)
        self.setObjectName(f"gsmapa_section_{key}")
        self._key = key
        self._config = config
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        header = QHBoxLayout()
        header.setSpacing(6)
        self.toggle = QToolButton()
        self.toggle.setAutoRaise(True)
        self.toggle.setText(title)
        self.toggle.setIconSize(QSize(theme.ROW_SIZE, theme.ROW_SIZE))
        self.toggle.setFont(theme.bold_font())
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setAccessibleName(title)
        self.context = ElidedLabel()
        self.context.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        theme.style_muted(self.context)
        header.addWidget(self.toggle)
        header.addWidget(self.context, 1)
        outer.addLayout(header)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(8)
        outer.addWidget(self.body)
        collapsed = bool(config.is_collapsed(key)) if config is not None else False
        self._expanded = not collapsed
        self.toggle.clicked.connect(self._on_clicked)
        self._on_toggled(self._expanded, save=False)

    def set_context(self, text: str, tooltip: str = "") -> None:
        self.context.setText(text)
        self.context.setToolTip(tooltip)

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        self._on_toggled(expanded)

    def _on_clicked(self, *_args) -> None:
        self._on_toggled(not self._expanded)

    def _on_toggled(self, expanded: bool, save: bool = True) -> None:
        self._expanded = expanded
        self.toggle.setAccessibleDescription("rozwinięta" if expanded else "zwinięta")
        self.body.setVisible(expanded)
        # Własny szewron (SVG) zamiast ``setArrowType`` — strzałki stylów Qt różnią się wielkością
        # i kształtem (macOS, Windows, Fusion), a panel ma wyglądać jak makieta w każdym stylu.
        from . import theme

        theme.set_icon(self.toggle, "chevron-down" if expanded else "chevron-right", role="WindowText")
        if save and self._config is not None:
            self._config.set_collapsed(self._key, not expanded)


def _apply_chip(button) -> None:
    from . import theme

    t = theme.tokens()
    button.setStyleSheet(
        "QToolButton { border: 1px solid %s; border-radius: 11px; padding: 2px 10px; background: %s;"
        " color: %s; }"
        "QToolButton:hover { background: %s; }"
        "QToolButton:checked { background-color: %s; color: %s; border-color: %s; }"
        % (t["border_strong"], t["field"], t["text"], t["button_hover"],
           t["accent"], t["accent_text"], t["accent"])
    )
    _apply_chip_icon(button)


def _apply_chip_icon(button) -> None:
    from qgis.PyQt.QtGui import QIcon

    from . import theme

    if button.property("gsmapa_chip_check"):
        button.setIcon(theme.icon("check", "HighlightedText") if button.isChecked() else QIcon())


def make_chip(text: str, checkable: bool = True, check_icon: bool = True, parent=None):
    """
    Chip filtra (pkt 6.5): zaokrąglony ``QToolButton``; włączony — wypełnienie akcentem, ikona ✓
    i pogrubienie, wyłączony — obrys. Stan jest czytelny bez koloru (ikona ✓).
    """
    from qgis.PyQt.QtWidgets import QToolButton

    from . import theme

    class ChipButton(QToolButton):
        def __init__(self):
            super().__init__(parent)
            self.toggled.connect(self._on_toggled)

        def _on_toggled(self, _checked: bool) -> None:
            _apply_chip_icon(self)

    button = ChipButton()
    button.setText(text)
    button.setCheckable(checkable)
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    button.setIconSize(QSize(theme.CHIP_SIZE, theme.CHIP_SIZE))
    button.setProperty("gsmapa_chip_check", bool(check_icon and checkable))
    if check_icon and checkable:
        # Rezerwacja miejsca na ikonę ✓ — przycisk nie zmienia szerokości przy zaznaczeniu
        # (zapobiega nieoczekiwanemu zawijaniu wiersza w FlowLayout).
        # Szerokość: tekst + ikona + odstęp + padding CSS (2×10) + obramowanie (2×1).
        button.setMinimumWidth(button.fontMetrics().horizontalAdvance(text) + theme.CHIP_SIZE + 26)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    theme.register(button, _apply_chip)
    return button


def make_badge(text: str, role: str = "info", parent=None):
    """
    Plakietka typu (pkt 6.3): obrys i tekst w kolorze typu, tło przezroczyste.

    Kolor pochodzi z :mod:`gs_mapa.ui.theme` (rola semantyczna), więc plakietka
    nadąża za zmianą motywu. Zwraca ``QLabel`` — do wstawienia jako widżet
    elementu drzewa albo w układzie.
    """
    from qgis.PyQt.QtWidgets import QLabel

    from . import theme

    label = QLabel(text, parent)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def apply(_obj=None) -> None:
        color = theme.semantic(role, label).name()
        label.setStyleSheet(
            "QLabel { border: 1px solid %s; border-radius: 3px; color: %s; background: transparent;"
            " padding: 0 5px; }" % (color, color)
        )

    apply()
    theme.register(label, apply)
    return label


class RemovableChip(QWidget):
    """
    Chip filtra z krzyżykiem (pkt 6.5): „Jednostka: Lublin ×”.

    Chip pokazuje filtr z kontekstu (jednostka z paska kontekstu) i pozwala go zdjąć
    kliknięciem w krzyżyk. Kolory z palety — patrz :mod:`gs_mapa.ui.theme`.
    """

    #: Użytkownik zdjął filtr (krzyżyk).
    removed = pyqtSignal()

    def __init__(self, text: str = "", parent=None):
        from qgis.PyQt.QtWidgets import QHBoxLayout, QToolButton

        from . import theme

        super().__init__(parent)
        self.setObjectName("gsmapa_removable_chip")
        # Ramka i tło z arkusza stylów — zwykły QWidget rysuje je tylko z tym atrybutem.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self._label = QLabel(text)
        self._close = QToolButton()
        self._close.setText("×")
        self._close.setToolTip(self.tr("Usuń filtr jednostki"))
        self._close.setAccessibleName(self.tr("Usuń filtr jednostki"))
        self._close.setFixedWidth(16)
        self._close.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close.clicked.connect(self.removed)
        layout.addWidget(self._label)
        layout.addWidget(self._close)
        self._apply_style()
        theme.register(self, self._apply_style)

    def text(self) -> str:
        return self._label.text()

    def setText(self, text: str) -> None:  # noqa: N802 - nazwa z API Qt
        self._label.setText(text)

    def _apply_style(self, _obj=None) -> None:
        from . import theme

        t = theme.tokens()
        self.setStyleSheet(
            "QWidget#gsmapa_removable_chip { border: 1px solid %s; border-radius: 11px; background: %s; }"
            "QLabel { border: none; background: transparent; padding: 2px 2px 2px 10px; }"
            "QToolButton { border: none; background: transparent; padding: 0 4px 0 0; }"
            % (t["border_strong"], t["field"])
        )


class SearchableCombo(QWidget):
    """
    Przeszukiwalne combo (nazwa albo TERYT) — pole tekstowe z listą podpowiedzi.

    Zastępuje parę „pole wyszukiwania + combo” (T6). Wybrana pozycja to TERYT
    (4 cyfry powiatu); tekst pola pokazuje nazwę powiatu z kodem. Dopasowanie
    ignoruje wielkość liter i polskie znaki (``boleslawiecki`` znajdzie
    „bolesławiecki”).
    """

    #: Wybrano pozycję (TERYT albo ``""``).
    selected = pyqtSignal(str)

    def __init__(self, parent=None):
        from qgis.PyQt.QtWidgets import QCompleter, QHBoxLayout, QLabel, QVBoxLayout

        from . import theme

        super().__init__(parent)
        self.setObjectName("gsmapa_searchable_combo")
        self._items: List[Tuple[str, str]] = []
        self._selected = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self._edit = QLineEdit()
        self._edit.setPlaceholderText(self.tr("nazwa albo TERYT, np. krakowski, Lublin, 1206"))
        self._edit.setAccessibleName(self.tr("Szukaj powiatu"))
        self._edit.setClearButtonEnabled(True)
        self._completer = QCompleter(self)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._edit.setCompleter(self._completer)
        self._completer.activated[str].connect(self._on_activated)
        self._edit.editingFinished.connect(self._on_editing_finished)
        row.addWidget(self._edit, 1)
        self._hint = QLabel("")
        theme.style_muted(self._hint, small=True)
        self._hint.setVisible(False)
        row.addWidget(self._hint, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(row)

    # --- dane ---------------------------------------------------------------

    def set_items(self, items: Sequence[Tuple[str, str]]) -> None:
        """Pozycje combo: (tekst na liście, TERYT)."""
        from qgis.PyQt.QtGui import QStandardItem, QStandardItemModel

        from ..core.act_index import normalize_text

        self._items = list(items)
        model = QStandardItemModel()
        for text, _data in self._items:
            model.appendRow([QStandardItem(text), QStandardItem(normalize_text(text))])
        self._completer.setModel(model)
        self._completer.setCompletionColumn(0)
        # Kolumna filtru (znormalizowana nazwa) — właściwość QCompleter, nie metoda.
        self._completer.setProperty("filterKeyColumn", 1)

    def set_hint(self, text: str) -> None:
        """Podpowiedź obok pola (np. „z jednostki”)."""
        self._hint.setText(text)
        self._hint.setVisible(bool(text))

    def current_teryt(self) -> str:
        """TERYT wybranej pozycji (``""`` gdy nic nie wybrano)."""
        return self._selected

    def set_current_teryt(self, teryt: str) -> None:
        """Wybiera pozycję po TERYT (pusty tekst czyści wybór)."""
        self._selected = str(teryt or "")
        self._edit.setText(self._label_of(self._selected))

    def _label_of(self, teryt: str) -> str:
        return next((text for text, data in self._items if data == teryt), "")

    def _on_activated(self, text: str) -> None:
        """Wybór z listy podpowiedzi — po tekście, nie po indeksie (filtrowa zmienia indeksy)."""
        match = next(((label, data) for label, data in self._items if label == text), None)
        if match is None:
            return
        self._selected = match[1]
        self._edit.setText(match[0])
        self.selected.emit(self._selected)

    def _on_editing_finished(self) -> None:
        """Wpisanie ręczne — dopasowanie do pozycji listy; brak dopasowania czyści wybór."""
        from ..core.act_index import normalize_text

        text = self._edit.text().strip()
        if not text:
            self._selected = ""
            self.selected.emit("")
            return
        query = normalize_text(text)
        match = next(((label, data) for label, data in self._items
                      if normalize_text(label) == query), None)
        if match is None:
            # Dopasowanie „zawiera” — użytkownik pisze fragment nazwy („krakow”, „1206”).
            match = next(((label, data) for label, data in self._items
                          if query and query in normalize_text(label)), None)
        if match is None:
            self._selected = ""
            self._edit.setText("")
            self.selected.emit("")
            return
        self._selected = match[1]
        self._edit.setText(match[0])
        self.selected.emit(self._selected)


class WrappingItemDelegate(QStyledItemDelegate):
    """
    Delegat zawijający tekst wybranych kolumn drzewa (pkt 6.9: komunikatów nie ucinamy „…”).

    ``QTreeView.setWordWrap(True)`` zawija tekst przy rysowaniu, ale domyślny delegat liczy
    wysokość wiersza bez znajomości szerokości kolumny — wiersz zostaje jednolinijkowy, a tekst
    jest ucięty. Ten delegat liczy wysokość z bieżącej szerokości kolumny (zawijanie na granicy
    słów, a długie identyfikatory bez spacji — w dowolnym miejscu). Po zmianie szerokości kolumny
    widok musi przeliczyć układ (:meth:`attach` podłącza to do nagłówka).
    """

    def __init__(self, view, columns: Sequence[int]):
        super().__init__(view)
        self._view = view
        self._columns = set(columns)

    @classmethod
    def attach(cls, view, columns: Sequence[int]) -> "WrappingItemDelegate":
        delegate = cls(view, columns)
        view.setItemDelegate(delegate)
        view.setWordWrap(True)
        view.setUniformRowHeights(False)
        view.header().sectionResized.connect(delegate._relayout)
        return delegate

    def _relayout(self, *_args) -> None:
        self._view.doItemsLayout()

    def _text_width(self, option, index) -> int:
        view = self._view
        column = index.column()
        width = view.header().sectionSize(column)
        if column == view.treePosition() or (view.treePosition() < 0 and column == 0):
            depth, parent = 0, index.parent()
            while parent.isValid():
                depth += 1
                parent = parent.parent()
            width -= view.indentation() * (depth + (1 if view.rootIsDecorated() else 0))
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.rect = QRect(0, 0, max(width, 1), 10000)
        style = view.style()
        text_rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, view)
        # Marginesy tekstu w komórce (jak w QCommonStyle: PM_FocusFrameHMargin + 1 z każdej strony).
        margin = style.pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin, None, view) + 1
        return max(text_rect.width() - 2 * margin, 10)

    @staticmethod
    def text_height(text: str, font, width: int) -> int:
        """Wysokość tekstu zawiniętego do ``width`` px (granica słów albo dowolne miejsce)."""
        layout = QTextLayout(text, font)
        text_option = QTextOption()
        text_option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(text_option)
        height = 0.0
        layout.beginLayout()
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            height += line.height()
        layout.endLayout()
        return int(height + 0.999)

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - nazwa z API Qt
        hint = super().sizeHint(option, index)
        if index.column() not in self._columns:
            return hint
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        if not text:
            return hint
        font = index.data(Qt.ItemDataRole.FontRole) or option.font
        line = QFontMetrics(font).height()
        padding = max(hint.height() - line, 0)
        height = self.text_height(text, font, self._text_width(option, index)) + padding
        return QSize(hint.width(), max(hint.height(), height))


class _ToggleLabel(QLabel):
    """Etykieta pola wyboru: kliknięcie przełącza pole (jak tekst samego ``QCheckBox``)."""

    def __init__(self, text: str, check, parent=None):
        super().__init__(text, parent)
        self._check = check

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - nazwa z API Qt
        if event.button() == Qt.MouseButton.LeftButton and self._check.isEnabled():
            self._check.toggle()
        super().mouseReleaseEvent(event)


class WrappingCheckBox(QWidget):
    """
    Pole wyboru z etykietą zawijaną w wąskim doku (pkt 7: przy 300 px nic nie jest ucięte).

    ``QCheckBox`` nie zawija tekstu, więc długa etykieta wyznacza minimalną szerokość całej karty.
    Tu tekst jest osobną etykietą (``wordWrap``), a kliknięcie w nią przełącza pole. Pole wyboru
    jest dostępne jako :attr:`check` (``isChecked``, ``toggled``…), tekst — jako nazwa dostępna.
    """

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        from qgis.PyQt.QtWidgets import QCheckBox, QHBoxLayout

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.check = QCheckBox()
        self.check.setAccessibleName(text)
        self.label = _ToggleLabel(text, self.check)
        self.label.setWordWrap(True)
        self.label.setBuddy(self.check)
        layout.addWidget(self.check, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.label, 1)

    def setToolTip(self, text: str) -> None:  # noqa: N802 - nazwa z API Qt
        super().setToolTip(text)
        self.check.setToolTip(text)
        self.label.setToolTip(text)

    def text(self) -> str:
        return self.label.text()
