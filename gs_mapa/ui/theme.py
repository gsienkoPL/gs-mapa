"""
Motyw interfejsu wtyczki: własny wygląd z tokenów makiet, kolory semantyczne, ikony.

Panel i okna wtyczki wyglądają jak makiety ``docs/ui/`` (i aplikacja przeglądarkowa) niezależnie
od stylu Qt wybranego w QGIS (macOS, Windows, Fusion): :func:`apply_plugin_style` nakłada na korzeń
(dok, okno) paletę z tokenów (``core.colors.UI_TOKENS``) i arkusz stylów (:func:`stylesheet`)
nadpisujący rysowanie kontrolek. Jasny albo ciemny wariant wybiera jasność okna QGIS.

Jedyne miejsce, w którym interfejs wtyczki ustala kolory (``docs/ui/UI_GUIDELINES.md``, pkt 3).
Moduły ``ui/`` nie wpisują stałych kolorów — pytają tutaj o rolę:

- tekst pomocniczy: :func:`muted_text`, :func:`style_muted`;
- statusy i typy aktów: :func:`semantic` (``ok``, ``warn``, ``err``, ``info``, ``pog``, ``mpzp``,
  ``zpi``, ``pzpw``), w HTML — znaczniki ``{c:ok}`` itd. w :func:`set_html`;
- akcent i linki: :func:`accent`, :func:`link`;
- ikony: :func:`icon` (monochromatyczne SVG wtyczki barwione kolorem palety), :func:`theme_icon`
  (ikona QGIS z zamiennikiem).

Zmiana motywu w locie: :class:`_ThemeWatcher` łapie ``ApplicationPaletteChange`` aplikacji
i wywołuje funkcje odświeżania zarejestrowane przez :func:`register` (widżety z kolorami
semantycznymi i ikonami wtyczki). Pomocnicze funkcje (``style_muted``, ``set_html``,
``set_item_role``…) rejestrują się same.
"""

from __future__ import annotations

import logging
import tempfile
import weakref
from string import Template
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import QByteArray, QCoreApplication, QEvent, QObject, QRectF, QSize, Qt
from qgis.PyQt.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPalette, QPixmap
from qgis.PyQt.QtWidgets import QApplication, QTreeWidget, QTreeWidgetItem, QWidget

from ..core.colors import (
    MIN_CONTRAST, SEMANTIC_COLORS, contrast_ratio, ensure_contrast, mix_rgb, semantic_hex, ui_tokens,
)

logger = logging.getLogger("GSMapa.Theme")

#: Katalog monochromatycznych ikon wtyczki (obrys ``currentColor``, siatka 24 px).
ICON_DIR = Path(__file__).resolve().parent.parent / "icons" / "ui"

#: Rozmiary ikon (pkt 4 wytycznych): pasek narzędzi i przyciski, wiersze list, chipy i linki.
TOOLBAR_SIZE, ROW_SIZE, CHIP_SIZE = 16, 14, 12

#: Rozmiary bitmap generowanych z SVG (z zapasem na ekrany HiDPI).
_PIXMAP_SIZES = (12, 14, 16, 20, 24, 28, 32, 48, 64)

#: Udział koloru tekstu w kolorze tekstu pomocniczego (``Text`` z kanałem alfa ~0.65).
MUTED_SHARE = 0.65

#: Rola danych elementu drzewa z nazwą roli koloru (:func:`set_item_role`).
_ITEM_ROLE = Qt.ItemDataRole.UserRole + 731

#: Ikony: (nazwa, kolor) -> QIcon. Kolor w kluczu = osobny cache dla jasnego i ciemnego motywu.
_icon_cache: Dict[Tuple[str, str], QIcon] = {}


# --- paleta -------------------------------------------------------------------------------


def system_dark() -> bool:
    """Ciemny motyw QGIS: jasność ``Window`` palety aplikacji < 128 (pkt 3.1.4 wytycznych)."""
    app = QApplication.instance()
    pal = app.palette() if app is not None else QPalette()
    return pal.color(QPalette.ColorRole.Window).lightness() < 128


def tokens() -> Dict[str, str]:
    """Tokeny wyglądu wtyczki (``core.colors.UI_TOKENS``) dla bieżącego motywu QGIS."""
    return ui_tokens(system_dark())


def plugin_palette() -> QPalette:
    """
    Paleta wtyczki z tokenów: tło okna, pola, tekst, akcent… niezależnie od stylu Qt i systemu.

    Nakładana na korzenie (dok, okna) przez :func:`apply_plugin_style`; widżety dziedziczą ją
    po rodzicu, więc także funkcje liczące kolory z palety widżetu dostają kolory wtyczki.
    """
    t = tokens()
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: t["window"], QPalette.ColorRole.WindowText: t["text"],
        QPalette.ColorRole.Base: t["surface"], QPalette.ColorRole.AlternateBase: t["alt"],
        QPalette.ColorRole.Text: t["text"], QPalette.ColorRole.Button: t["button"],
        QPalette.ColorRole.ButtonText: t["text"], QPalette.ColorRole.BrightText: t["accent_text"],
        QPalette.ColorRole.Highlight: t["accent"], QPalette.ColorRole.HighlightedText: t["accent_text"],
        QPalette.ColorRole.PlaceholderText: t["muted"], QPalette.ColorRole.Link: t["link"],
        QPalette.ColorRole.LinkVisited: t["link"], QPalette.ColorRole.ToolTipBase: t["surface"],
        QPalette.ColorRole.ToolTipText: t["text"], QPalette.ColorRole.Light: t["surface"],
        QPalette.ColorRole.Midlight: t["button_hover"], QPalette.ColorRole.Mid: t["border"],
        QPalette.ColorRole.Dark: t["border_strong"], QPalette.ColorRole.Shadow: t["border_strong"],
    }
    accent_role = getattr(QPalette.ColorRole, "Accent", None)
    if accent_role is not None:
        roles[accent_role] = t["accent"]
    for role, value in roles.items():
        pal.setColor(role, QColor(value))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(t["disabled"]))
    return pal


def palette(widget: Optional[QWidget] = None) -> QPalette:
    """
    Paleta wtyczki (z tokenów) — także dla konkretnego widżetu.

    Widżety pod arkuszem stylów nie dziedziczą palety rodzica (Qt bierze wtedy paletę aplikacji),
    więc paleta widżetu nie mówi nic o wyglądzie wtyczki. Parametr ``widget`` zostaje dla zgodności.
    """
    return plugin_palette()


def _rgb(color: QColor) -> Tuple[int, int, int]:
    return color.red(), color.green(), color.blue()


def is_dark(widget: Optional[QWidget] = None) -> bool:
    """Ciemny motyw: jasność koloru ``Window`` < 128 (pkt 3.1.4 wytycznych)."""
    return palette(widget).color(QPalette.ColorRole.Window).lightness() < 128


def _backgrounds(widget: Optional[QWidget] = None) -> List[Tuple[int, int, int]]:
    pal = palette(widget)
    return [_rgb(pal.color(QPalette.ColorRole.Window)), _rgb(pal.color(QPalette.ColorRole.Base))]


def semantic(role: str, widget: Optional[QWidget] = None) -> QColor:
    """
    Kolor semantyczny w wariancie bieżącego motywu (``UI_GUIDELINES.md``, tabela 3.1).

    Kontrast do ``Window`` i ``Base`` jest co najmniej 4.5:1 — przy nietypowej palecie kolor
    jest rozjaśniany albo przyciemniany (``core.colors.ensure_contrast``).

    :param role: ``ok``, ``warn``, ``err``, ``info``, ``pog``, ``mpzp``, ``zpi`` albo ``pzpw``.
    """
    if role not in SEMANTIC_COLORS:
        raise KeyError(f"Nieznana rola koloru: {role}")
    window = _rgb(palette(widget).color(QPalette.ColorRole.Window))
    return QColor(semantic_hex(role, window, _backgrounds(widget)))


def muted_text(widget: Optional[QWidget] = None) -> QColor:
    """
    Kolor tekstu pomocniczego: ``PlaceholderText``, gdy ma kontrast ≥ 4.5:1 do tła okna i pola,
    w przeciwnym razie ``Text`` zmieszany z tłem (odpowiednik alfy ~0.65) — rozjaśniany
    albo przyciemniany do kontrastu 4.5:1.
    """
    pal = palette(widget)
    backgrounds = _backgrounds(widget)
    placeholder = pal.color(QPalette.ColorRole.PlaceholderText)
    if placeholder.alpha() == 255 and all(
            contrast_ratio(_rgb(placeholder), bg) >= MIN_CONTRAST for bg in backgrounds):
        return QColor(placeholder)
    text = _rgb(pal.color(QPalette.ColorRole.Text))
    window = backgrounds[0]
    mixed = QColor(mix_rgb(text, window, MUTED_SHARE))
    return QColor(ensure_contrast(_rgb(mixed), backgrounds))


def accent(widget: Optional[QWidget] = None) -> QColor:
    """Kolor akcentu: rola ``Accent`` (Qt ≥ 6.6), inaczej ``Highlight``."""
    pal = palette(widget)
    role = getattr(QPalette.ColorRole, "Accent", None)
    if role is not None:
        color = pal.color(role)
        if color.isValid():
            return color
    return pal.color(QPalette.ColorRole.Highlight)


def accent_text(widget: Optional[QWidget] = None) -> QColor:
    """Tekst na tle akcentu (``HighlightedText``)."""
    return palette(widget).color(QPalette.ColorRole.HighlightedText)


def link(widget: Optional[QWidget] = None) -> QColor:
    """Kolor odnośnika (``Link``)."""
    return palette(widget).color(QPalette.ColorRole.Link)


def color_of(role: str, widget: Optional[QWidget] = None) -> QColor:
    """
    Kolor po nazwie roli: semantyczna (``ok``…), ``muted``, ``accent``, ``link`` albo rola palety
    (``WindowText``, ``Text``, ``ButtonText``, ``HighlightedText``…).
    """
    if role in SEMANTIC_COLORS:
        return semantic(role, widget)
    if role == "muted":
        return muted_text(widget)
    if role == "accent":
        return accent(widget)
    if role == "link":
        return link(widget)
    return palette(widget).color(getattr(QPalette.ColorRole, role))


# --- typografia -------------------------------------------------------------------------------


def small_font(widget: Optional[QWidget] = None) -> QFont:
    """Stopień pomocniczy: czcionka aplikacji − 1 pt (pkt 5 wytycznych)."""
    font = QFont(widget.font() if widget is not None else QApplication.font())
    size = font.pointSizeF()
    if size > 0:
        font.setPointSizeF(max(size - 1, 6))
    return font


def button_font(button):
    """
    Czcionka aplikacji na ``QToolButton`` z tekstem (pkt 5: dwa stopnie pisma).

    Styl macOS daje klasie ``QToolButton`` własną, mniejszą czcionkę (``ToolButtonFont``, ok. −2 pt),
    więc przycisk narzędziowy obok ``QPushButton`` wygląda na „drobniejszy”. Jawna czcionka
    aplikacji wyrównuje stopień z pozostałymi przyciskami; odnośniki dostają stopień pomocniczy
    (:func:`link_button`).
    """
    button.setFont(QFont(QApplication.font()))
    return button


def bold_font(widget: Optional[QWidget] = None) -> QFont:
    font = QFont(widget.font() if widget is not None else QApplication.font())
    font.setBold(True)
    return font


# --- ikony -------------------------------------------------------------------------------------


def icon_path(name: str) -> Path:
    return ICON_DIR / f"{name}.svg"


def icon(name: str, color_role: str = "WindowText", widget: Optional[QWidget] = None) -> QIcon:
    """
    Monochromatyczna ikona wtyczki (``icons/ui/<name>.svg``) w kolorze roli.

    ``currentColor`` w SVG jest zastępowany kolorem palety (albo semantycznym, patrz
    :func:`color_of`). Ikony są trzymane w pamięci osobno dla każdego koloru, więc jasny
    i ciemny motyw mają własne wpisy; zmiana motywu czyści pamięć (:func:`refresh_all`).

    :return: ikona; pusta (``isNull()``), gdy pliku nie ma.
    """
    color = color_of(color_role, widget)
    key = (name, color.name(QColor.NameFormat.HexArgb))
    cached = _icon_cache.get(key)
    if cached is not None:
        return cached
    path = icon_path(name)
    if not path.is_file():
        logger.warning(f"Brak ikony {path}")
        return QIcon()
    svg = path.read_text(encoding="utf-8").replace("currentColor", color.name())
    result = _svg_icon(svg.encode("utf-8"), color.alphaF())
    _icon_cache[key] = result
    return result


def _svg_icon(data: bytes, opacity: float = 1.0) -> QIcon:
    from qgis.PyQt.QtSvg import QSvgRenderer  # QtSvg tylko tutaj — reszta modułu bez niego

    renderer = QSvgRenderer(QByteArray(data))
    result = QIcon()
    if not renderer.isValid():
        return result
    for size in _PIXMAP_SIZES:
        image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(opacity)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
        result.addPixmap(QPixmap.fromImage(image))
    return result


def theme_icon(qgis_name: str, fallback: Optional[str] = None, color_role: str = "WindowText",
               widget: Optional[QWidget] = None) -> QIcon:
    """
    Ikona QGIS (``/mActionIdentify.svg``) — z zamiennikiem z ikon wtyczki, gdy QGIS jej nie ma.

    :param fallback: nazwa ikony wtyczki (``icons/ui``) używanej, gdy ikona QGIS jest pusta.
    """
    from qgis.core import QgsApplication

    result = QgsApplication.getThemeIcon(qgis_name)
    if (result is None or result.isNull() or not result.availableSizes()) and fallback:
        return icon(fallback, color_role, widget)
    return result


def blank_pixmap(size: int = ROW_SIZE) -> QPixmap:
    """Przezroczysta bitmapa — wyrównuje wiersze bez ikony do wierszy z ikoną."""
    result = QPixmap(size, size)
    result.fill(Qt.GlobalColor.transparent)
    return result


def pixmap(name: str, size: int = ROW_SIZE, color_role: str = "WindowText",
           widget: Optional[QWidget] = None) -> QPixmap:
    """Bitmapa ikony wtyczki w rozmiarze ``size`` (do etykiet i delegatów)."""
    return icon(name, color_role, widget).pixmap(QSize(size, size))


# --- odświeżanie przy zmianie motywu -------------------------------------------------------------


#: Zarejestrowane obiekty -> funkcje odświeżania ``fn(obiekt)``.
_registry: "weakref.WeakKeyDictionary[QObject, List[Callable]]" = weakref.WeakKeyDictionary()


class _ThemeWatcher(QObject):
    """Filtr zdarzeń aplikacji: zmiana palety (motyw systemu, styl QGIS) odświeża widżety wtyczki."""

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - nazwa z API Qt
        if obj is QCoreApplication.instance() and event.type() == QEvent.Type.ApplicationPaletteChange:
            # Aplikacja dostaje zdarzenie przed widżetami, ale ma już nową paletę — dlatego
            # funkcje odświeżania liczą kolory z palety aplikacji, nie widżetu.
            refresh_all()
        return False


_watcher: Optional[_ThemeWatcher] = None


def install() -> None:
    """Instaluje filtr zdarzeń zmiany motywu (raz na proces; wołane przy pierwszej rejestracji)."""
    global _watcher
    app = QCoreApplication.instance()
    if _watcher is not None or app is None:
        return
    _watcher = _ThemeWatcher()
    app.installEventFilter(_watcher)
    # Bitmapy ikon nie mogą przeżyć aplikacji (zniszczenie QPixmap po QGuiApplication = awaria).
    app.aboutToQuit.connect(shutdown)


def shutdown() -> None:
    """Zdejmuje filtr zdarzeń i czyści rejestr (rozładowanie wtyczki)."""
    global _watcher
    app = QCoreApplication.instance()
    if _watcher is not None and app is not None:
        app.removeEventFilter(_watcher)
    _watcher = None
    _registry.clear()
    _icon_cache.clear()


def register(obj: QObject, fn: Optional[Callable] = None) -> None:
    """
    Rejestruje odświeżanie obiektu przy zmianie motywu i od razu je wywołuje.

    :param fn: funkcja ``fn(obj)``; domyślnie metoda ``obj.refresh_theme()``. Funkcja nie może
        trzymać silnej referencji do ``obj`` (np. lambdy domykającej obiekt) — rejestr trzyma
        obiekty słabo, żeby nie przedłużać życia widżetów.
    """
    install()
    if fn is None:
        fn = _call_refresh_theme
    fns = _registry.setdefault(obj, [])
    if fn not in fns:
        fns.append(fn)
    fn(obj)


def _call_refresh_theme(obj) -> None:
    obj.refresh_theme()


def refresh_all() -> None:
    """Czyści ikony i odświeża wszystkie zarejestrowane obiekty (zmiana motywu)."""
    _icon_cache.clear()
    # Najpierw korzenie wtyczki (paleta z tokenów), potem widżety liczące kolory z palety rodzica.
    items = sorted(_registry.items(), key=lambda pair: _apply_root not in pair[1])
    for obj, fns in items:
        for fn in list(fns):
            try:
                fn(obj)
            except RuntimeError:  # obiekt C++ już usunięty
                _registry.pop(obj, None)
                break
            except Exception:  # noqa: BLE001 - odświeżenie jednego widżetu nie może zatrzymać reszty
                logger.exception("Odświeżenie motywu nie powiodło się")


# --- własny wygląd: arkusz stylów i styl bazowy -----------------------------------------------

#: Katalog plików SVG przebarwionych do arkusza stylów (``url(...)`` wymaga pliku).
_QSS_ICON_DIR = Path(tempfile.gettempdir()) / "gs_mapa_qss"

#: Arkusz stylów wtyczki (``$token`` — kolor z :func:`tokens`, ``$icon_*`` — ścieżki ikon).
#: Kolory tekstu etykiet nie są tu ustawiane: etykiety biorą je z palety (tekst pomocniczy,
#: kolory semantyczne), a arkusz nadpisałby paletę.
_QSS = Template("""
QDialog, QDockWidget { background: $window; }
QAbstractScrollArea { background: transparent; }
QAbstractScrollArea > QWidget#qt_scrollarea_viewport { background: transparent; }
QScrollArea > QWidget#qt_scrollarea_viewport > QWidget { background: $window; }

QLabel, QCheckBox, QRadioButton, QGroupBox { color: $text; }
*[gsmapa_role="muted"] { color: $muted; }
*[gsmapa_role="ok"] { color: $ok; }
*[gsmapa_role="warn"] { color: $warn; }
*[gsmapa_role="err"] { color: $err; }
*[gsmapa_role="info"] { color: $info; }
*[gsmapa_role="accent"] { color: $accent; }
*[gsmapa_role="link"] { color: $link; }

QLineEdit, QAbstractSpinBox, QComboBox, QPlainTextEdit, QTextEdit, QTextBrowser {
    background: $field; color: $text; border: 1px solid $border; border-radius: 6px;
    padding: 4px 8px; selection-background-color: $accent; selection-color: $accent_text;
}
QPlainTextEdit, QTextEdit, QTextBrowser { padding: 4px; }
QTextBrowser { background: $surface; }
QLineEdit:focus, QAbstractSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {
    border-color: $accent;
}
QLineEdit:disabled, QAbstractSpinBox:disabled, QComboBox:disabled { color: $disabled; background: $window; }
QComboBox { padding-right: 24px; }
QComboBox::drop-down, QDateTimeEdit::drop-down {
    subcontrol-origin: padding; subcontrol-position: center right; width: 22px; border: none;
}
QComboBox::down-arrow, QDateTimeEdit::down-arrow { image: url($icon_down); width: 12px; height: 12px; }
QComboBox QAbstractItemView {
    background: $surface; color: $text; border: 1px solid $border; outline: 0; padding: 2px;
    selection-background-color: $accent; selection-color: $accent_text;
}
QAbstractSpinBox { padding-right: 18px; }
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {
    subcontrol-origin: border; width: 16px; border: none; background: transparent;
}
QAbstractSpinBox::up-button { subcontrol-position: top right; }
QAbstractSpinBox::down-button { subcontrol-position: bottom right; }
QAbstractSpinBox::up-arrow { image: url($icon_up); width: 10px; height: 10px; }
QAbstractSpinBox::down-arrow { image: url($icon_down); width: 10px; height: 10px; }

QPushButton {
    background: $button; color: $text; border: 1px solid $border; border-radius: 6px;
    padding: 4px 12px; min-height: 18px;
}
QPushButton:hover { background: $button_hover; }
QPushButton:pressed { background: $button_pressed; }
QPushButton:focus { border-color: $accent; outline: none; }
QToolButton:focus, QCheckBox:focus, QRadioButton:focus, QTabBar:focus { outline: none; }
QPushButton:disabled { color: $disabled; background: $window; border-color: $border; }
QPushButton[gsmapa_primary="true"] {
    background: $accent; color: $accent_text; border-color: $accent; font-weight: 600;
}
QPushButton[gsmapa_primary="true"]:hover { background: $accent_hover; border-color: $accent_hover; }
QPushButton[gsmapa_primary="true"]:disabled { background: $button_pressed; border-color: $border; color: $disabled; }
QPushButton::menu-indicator {
    image: url($icon_down); subcontrol-origin: padding; subcontrol-position: center right;
    width: 10px; height: 10px; right: 2px;
}
QPushButton[gsmapa_primary="true"]::menu-indicator { image: url($icon_down_accent); }
QPushButton[gsmapa_menu="true"] { padding-right: 22px; }

QToolButton {
    background: $button; color: $text; border: 1px solid $border; border-radius: 6px; padding: 3px 6px;
}
QToolButton:hover { background: $button_hover; }
QToolButton:pressed, QToolButton:checked { background: $button_pressed; }
QToolButton:disabled { color: $disabled; background: $window; }
QToolButton[autoRaise="true"] { background: transparent; border-color: transparent; }
QToolButton[autoRaise="true"]:hover { background: $hover; }
QToolButton[gsmapa_link="true"] {
    background: transparent; border: none; padding: 0; color: $link;
}
QToolButton[gsmapa_link="true"]:hover { text-decoration: underline; background: transparent; }
QToolButton::menu-indicator { image: none; width: 0; }

QTabWidget::pane { border: none; border-top: 1px solid $border; top: -1px; background: $window; }
QStackedWidget { background: $window; }
QTabWidget::tab-bar { left: 12px; }
QTabBar { qproperty-drawBase: 0; background: transparent; }
QTabBar::tab {
    background: transparent; color: $muted; border: none; border-bottom: 2px solid transparent;
    padding: 6px 4px; margin-right: 18px;
}
QTabBar::tab:hover { color: $text; }
QTabBar::tab:selected { color: $text; font-weight: 600; border-bottom-color: $accent; }

QAbstractItemView {
    background: $surface; alternate-background-color: $alt; color: $text;
    border: 1px solid $border; border-radius: 6px; outline: 0;
    selection-background-color: $accent; selection-color: $accent_text;
}
QAbstractItemView::item { padding: 2px 0; }
QAbstractItemView::item:selected { background: $accent; color: $accent_text; }
QTreeView::branch { background: transparent; }
QTreeView::branch:has-children:closed { image: url($icon_right); }
QTreeView::branch:has-children:open { image: url($icon_down); }
QHeaderView { background: transparent; border: none; }
QHeaderView::section {
    background: $window; color: $muted; border: none; border-bottom: 1px solid $border;
    border-right: 1px solid $border; padding: 4px 6px;
}
QTableCornerButton::section { background: $window; border: none; }

QCheckBox, QRadioButton { spacing: 6px; }
QCheckBox::indicator, QRadioButton::indicator {
    width: 14px; height: 14px; border: 1px solid $border_strong; background: $field;
}
QCheckBox::indicator { border-radius: 4px; }
QRadioButton::indicator { border-radius: 8px; }
QCheckBox::indicator:checked { background: $accent; border-color: $accent; image: url($icon_check); }
QRadioButton::indicator:checked { background: $accent; border: 4px solid $field; }
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled { background: $window; border-color: $border; }

QGroupBox {
    border: 1px solid $border; border-radius: 6px; margin-top: 16px; padding: 10px 8px 8px 8px;
}
QGroupBox::title {
    subcontrol-origin: margin; subcontrol-position: top left; left: 8px; padding: 0 4px;
    font-weight: 600;
}

QProgressBar {
    background: $alt; border: 1px solid $border; border-radius: 4px; text-align: center;
    color: $text; max-height: 14px;
}
QProgressBar::chunk { background: $accent; border-radius: 3px; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle:vertical { background: $border_strong; border-radius: 4px; min-height: 24px; margin: 1px; }
QScrollBar::handle:horizontal { background: $border_strong; border-radius: 4px; min-width: 24px; margin: 1px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

QMenu {
    background: $surface; color: $text; border: 1px solid $border; border-radius: 8px; padding: 4px;
}
QMenu::item { padding: 5px 18px 5px 8px; border-radius: 5px; }
QMenu::item:selected { background: $accent; color: $accent_text; }
QMenu::item:disabled { color: $disabled; }
QMenu::separator { height: 1px; background: $border; margin: 4px 8px; }
QMenu::icon { padding-left: 6px; }

QSplitter::handle { background: $border; }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical { height: 1px; }
QFrame[frameShape="4"], QFrame[frameShape="5"] { background: $border; border: none; max-height: 1px; }
QFrame[frameShape="5"] { max-width: 1px; max-height: 16777215px; }

*[gsmapa_invalid="true"] { border: 1px solid $err; }

*[gsmapa_card="true"] { background: $surface; border: 1px solid $border; border-radius: 8px; }
*[gsmapa_field="true"] { background: $field; border: 1px solid $border; border-radius: 6px; }
""")


def _qss_icon(name: str, color: str, size: int = 16) -> str:
    """
    Plik PNG ikony ``name`` w kolorze ``color`` (ścieżka do ``url()`` arkusza stylów).

    PNG, nie SVG: arkusz stylów wczytuje obrazy przez wtyczki formatów Qt, a format ``svg`` nie
    zawsze jest zainstalowany. Obok zapisywana jest wersja ``@2x`` (ekrany HiDPI).
    """
    from qgis.PyQt.QtSvg import QSvgRenderer  # QtSvg tylko tutaj i w _svg_icon

    _QSS_ICON_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{name}-{color.lstrip('#')}-{size}"
    target = _QSS_ICON_DIR / f"{stem}.png"
    if not target.is_file():
        svg = icon_path(name).read_text(encoding="utf-8").replace("currentColor", color)
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        for scale, path in ((1, target), (2, _QSS_ICON_DIR / f"{stem}@2x.png")):
            image = QImage(size * scale, size * scale, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            renderer.render(painter, QRectF(0, 0, size * scale, size * scale))
            painter.end()
            image.save(str(path), "PNG")
    return target.as_posix()


def stylesheet() -> str:
    """Arkusz stylów wtyczki dla bieżącego motywu (kolory z :func:`tokens`)."""
    t = tokens()
    values = dict(t)
    for role in ("ok", "warn", "err", "info"):
        values[role] = semantic(role).name()
    values.update(
        icon_down=_qss_icon("chevron-down", t["muted"]),
        icon_down_accent=_qss_icon("chevron-down", t["accent_text"]),
        icon_up=_qss_icon("chevron-up", t["muted"]),
        icon_right=_qss_icon("chevron-right", t["muted"]),
        icon_check=_qss_icon("check", t["accent_text"]),
    )
    return _QSS.substitute(values)


def _apply_root(root: QWidget) -> None:
    root.setPalette(plugin_palette())
    root.setStyleSheet(stylesheet())


def apply_plugin_style(root: QWidget) -> QWidget:
    """
    Własny wygląd wtyczki na korzeniu (dok, okno): paleta i arkusz stylów z tokenów.

    Arkusz stylów określa tło i obramowanie każdej kontrolki, więc Qt rysuje je sam (styl
    arkusza), a nie styl systemu — wygląd nie zależy od stylu wybranego w QGIS (macOS, Windows,
    Fusion). Wariant jasny/ciemny nadąża za motywem (odświeżanie przy ``ApplicationPaletteChange``,
    przed pozostałymi widżetami).

    Nie nadajemy stylu Fusion widżetom (``setStyle``): połączenie stylu ustawionego na widżecie
    z arkuszem stylów rodzica prowadziło do awarii procesu przy niszczeniu okien.
    """
    root.setProperty("gsmapa_root", True)
    register(root, _apply_root)
    return root


def align_headers(widget) -> None:
    """
    Nagłówki kolumn do lewej — jednakowo w tabelach i drzewach.

    Qt ustawia wyrównanie nagłówka w zależności od stylu i platformy (raz do lewej, raz wyśrodkowane),
    więc tabele obok siebie w jednym oknie wyglądały niespójnie.
    """
    header = widget.horizontalHeader() if hasattr(widget, "horizontalHeader") else widget.header()
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


def repolish(widget: QWidget) -> None:
    """Ponowne dopasowanie arkusza stylów po zmianie właściwości używanej w selektorze."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


# --- pomocnicze: style widżetów ---------------------------------------------------------------
#
# Funkcje ``_apply_*`` liczą kolory z palety aplikacji: przy zmianie motywu aplikacja ma nową
# paletę, zanim rozejdzie się ona po widżetach (zdarzenie trafia najpierw do aplikacji).


def _apply_muted(widget: QWidget) -> None:
    color = muted_text()
    # Pusta paleta (bez ustawionych ról) + dwie role: reszta dziedziczy po rodzicu i nadąża za motywem.
    pal = QPalette()
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text):
        pal.setColor(role, color)
    widget.setPalette(pal)
    if widget.property("gsmapa_small"):
        widget.setFont(small_font())


def style_muted(widget: QWidget, small: bool = False) -> QWidget:
    """
    Tekst pomocniczy (kolor :func:`muted_text`); ``small`` — stopień pomocniczy (−1 pt).

    Pod korzeniem wtyczki kolor nadaje arkusz stylów (``[gsmapa_role="muted"]``); paleta widżetu
    jest ustawiana też — dla widżetów poza korzeniem (np. panel Warstwy QGIS).
    """
    widget.setProperty("gsmapa_small", bool(small))
    widget.setProperty("gsmapa_role", "muted")
    repolish(widget)
    register(widget, _apply_muted)
    return widget


def _apply_role(widget: QWidget) -> None:
    role = widget.property("gsmapa_color_role") or ""
    pal = QPalette()
    if role:
        color = color_of(role)
        for palette_role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text):
            pal.setColor(palette_role, color)
    widget.setPalette(pal)


def set_text_role(widget: QWidget, role: str) -> None:
    """Kolor tekstu widżetu z roli (``ok``, ``err``, ``muted``…); ``""`` — kolor domyślny."""
    widget.setProperty("gsmapa_color_role", role)
    # Selektor arkusza stylów zna role statusów; pozostałe (typy aktów) — przez paletę.
    widget.setProperty("gsmapa_role", role if role in ("muted", "ok", "warn", "err", "info", "accent", "link") else "")
    repolish(widget)
    register(widget, _apply_role)


def _apply_html(label) -> None:
    template = label.property("gsmapa_html") or ""
    html = template
    for role in (*SEMANTIC_COLORS, "muted", "accent", "link"):
        token = "{c:" + role + "}"
        if token in html:
            html = html.replace(token, color_of(role).name())
    label.setText(html)


def set_html(label, html: str) -> None:
    """
    Tekst sformatowany (HTML) z kolorami ról: ``<b style='color:{c:ok}'>…</b>``.

    Znaczniki ``{c:ok}``, ``{c:warn}``, ``{c:err}``, ``{c:info}``, ``{c:muted}``, ``{c:accent}``,
    ``{c:link}`` (i typy aktów) są zastępowane kolorem bieżącego motywu — także po jego zmianie.
    """
    label.setProperty("gsmapa_html", html)
    register(label, _apply_html)


def span(text: str, role: str, bold: bool = False) -> str:
    """Fragment HTML w kolorze roli (do :func:`set_html`); ``text`` musi być już escapowany."""
    tag = "b" if bold else "span"
    return f"<{tag} style='color:{{c:{role}}}'>{text}</{tag}>"


def mark_invalid(widget: QWidget, invalid: bool) -> None:
    """
    Ramka błędu wokół pola (wymagane pole jest puste) w kolorze roli ``err``.

    Ramkę rysuje arkusz stylów wtyczki (selektor ``[gsmapa_invalid="true"]``), więc pole zachowuje
    zaokrąglenie i wypełnienie pozostałych pól.
    """
    if bool(widget.property("gsmapa_invalid")) == bool(invalid):
        return
    widget.setProperty("gsmapa_invalid", bool(invalid))
    repolish(widget)


def _apply_tree_roles(tree: QTreeWidget) -> None:
    stack = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    while stack:
        item = stack.pop()
        for column in range(tree.columnCount()):
            role = item.data(column, _ITEM_ROLE)
            if role:
                item.setForeground(column, color_of(role))
        stack.extend(item.child(i) for i in range(item.childCount()))


def set_item_role(item: QTreeWidgetItem, column: int, role: str) -> None:
    """
    Kolor tekstu komórki drzewa z roli; drzewo odświeża kolory przy zmianie motywu.

    Element musi już należeć do drzewa (``treeWidget()``) — inaczej kolor zostanie ustawiony
    dopiero przy najbliższym odświeżeniu.
    """
    item.setData(column, _ITEM_ROLE, role)
    tree = item.treeWidget()
    if tree is not None:
        item.setForeground(column, color_of(role))
        if _apply_tree_roles not in _registry.get(tree, []):
            register(tree, _apply_tree_roles)


def _apply_icon(widget) -> None:
    name = widget.property("gsmapa_icon")
    qgis_name = widget.property("gsmapa_qgis_icon") or ""
    role = widget.property("gsmapa_icon_role") or "ButtonText"
    widget.setIcon(theme_icon(qgis_name, name, role) if qgis_name else icon(name, role))


def set_icon(widget, name: str, qgis_name: str = "", role: str = "ButtonText") -> None:
    """
    Ikona przycisku / akcji: ikona QGIS ``qgis_name`` (gdy jest) albo monochromatyczna ikona
    wtyczki ``name`` w kolorze roli; ikona wtyczki jest przebarwiana przy zmianie motywu.

    :param widget: ``QAbstractButton`` albo ``QAction`` (cokolwiek z ``setIcon``).
    """
    widget.setProperty("gsmapa_icon", name)
    widget.setProperty("gsmapa_qgis_icon", qgis_name)
    widget.setProperty("gsmapa_icon_role", role)
    register(widget, _apply_icon)


def primary(button):
    """
    Przycisk główny obszaru (pkt 6.6): wypełnienie akcentem (arkusz stylów, selektor
    ``[gsmapa_primary="true"]``) i ``setDefault(True)``.
    """
    if hasattr(button, "setDefault"):
        button.setDefault(True)
    button.setProperty("gsmapa_primary", True)
    repolish(button)
    return button


def _apply_link(button) -> None:
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.ButtonText, link())
    pal.setColor(QPalette.ColorRole.WindowText, link())
    button.setPalette(pal)
    name = button.property("gsmapa_icon")
    if name:
        button.setIcon(icon(name, "link"))


def link_button(button, icon_name: str = ""):
    """``QToolButton`` w stylu odnośnika: bez ramki, tekst (i ikona) w kolorze ``Link``."""
    from qgis.PyQt.QtWidgets import QToolButton

    if isinstance(button, QToolButton):
        button.setAutoRaise(True)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon if icon_name
                                  else Qt.ToolButtonStyle.ToolButtonTextOnly)
        button.setIconSize(QSize(CHIP_SIZE, CHIP_SIZE))
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    # Stopień pomocniczy (czcionka aplikacji − 1 pt), a nie ``ToolButtonFont`` stylu macOS (− 2 pt).
    button.setFont(small_font())
    button.setProperty("gsmapa_link", True)
    button.setProperty("gsmapa_icon", icon_name)
    register(button, _apply_link)
    return button
