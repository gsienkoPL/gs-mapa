"""Suwak „Przezroczystość” osadzony pod warstwą w panelu Warstwy QGIS.

Wbudowany widżet QGIS („transparency”) ma stały podpis „Opacity”/„Krycie”. Ten widżet
pokazuje przezroczystość: 0% — warstwa pełna, 100% — niewidoczna (krycie = 1 − przezroczystość).
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QSlider, QWidget
from qgis.gui import QgsGui, QgsLayerTreeEmbeddedWidgetProvider

#: Identyfikator widżetu we właściwościach warstwy (``embeddedWidgets/0/id``).
WIDGET_ID = "gs_mapa_transparency"


class TransparencyWidget(QWidget):
    """Podpis i suwak przezroczystości warstwy (w %; wartość w podpowiedzi suwaka).

    Bez pola liczbowego — panel Warstwy bywa wąski, a pole by się ucinało.

    Szerokość: kolumna drzewa warstw dopasowuje się do najszerszego wiersza legendy, więc
    rozciągliwy suwak „nie kończyłby się”. Jak we wbudowanym suwaku QGIS
    (``QgsLayerTreeOpacityWidget``): elementy o stałej szerokości zależnej od czcionki
    i rozciągliwy odstęp na końcu wiersza.
    """

    def __init__(self, layer, parent=None):
        super().__init__(parent)
        self._layer = layer

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        label = QLabel(self.tr("Przezroczystość"))
        label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        layout.addWidget(label)
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 100)
        # Szerokość zależna od czcionki — skaluje się z ekranami HiDPI.
        self._slider.setFixedWidth(self.fontMetrics().horizontalAdvance("X") * 14)
        self._slider.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        layout.addWidget(self._slider)
        layout.addStretch(1)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        self._sync_from_layer()
        self._slider.valueChanged.connect(self._on_value_changed)
        layer.opacityChanged.connect(self._sync_from_layer)

    def transparency(self) -> int:
        """Przezroczystość w %."""
        return self._slider.value()

    def set_transparency(self, value: int) -> None:
        """Ustawia przezroczystość w % (jak przesunięcie suwaka)."""
        self._slider.setValue(value)

    def _sync_from_layer(self, *_args) -> None:
        value = round((1.0 - self._layer.opacity()) * 100)
        self._slider.blockSignals(True)
        self._slider.setValue(value)
        self._slider.blockSignals(False)
        self._slider.setToolTip(f"Przezroczystość: {value} %")

    def _on_value_changed(self, value: int) -> None:
        self._slider.setToolTip(f"Przezroczystość: {value} %")
        opacity = 1.0 - value / 100.0
        if abs(self._layer.opacity() - opacity) > 1e-9:
            self._layer.setOpacity(opacity)
            self._layer.triggerRepaint()


class TransparencyWidgetProvider(QgsLayerTreeEmbeddedWidgetProvider):
    """Dostawca widżetu dla rejestru osadzonych widżetów drzewa warstw."""

    def id(self) -> str:
        return WIDGET_ID

    def name(self) -> str:
        return "Przezroczystość (GS Mapa)"

    def createWidget(self, layer, widgetIndex):  # noqa: N802 - nazwa z API QGIS
        return TransparencyWidget(layer)

    def supportsLayer(self, layer) -> bool:  # noqa: N802 - nazwa z API QGIS
        return True


_provider = None


def register() -> None:
    """Rejestruje widżet w QGIS (raz; przy ponownym wczytaniu wtyczki — bez duplikatu)."""
    global _provider
    registry = QgsGui.layerTreeEmbeddedWidgetRegistry()
    if WIDGET_ID in registry.providers():
        return
    _provider = TransparencyWidgetProvider()
    registry.addProvider(_provider)


def unregister() -> None:
    """Wyrejestrowuje widżet (przy rozładowaniu wtyczki)."""
    global _provider
    registry = QgsGui.layerTreeEmbeddedWidgetRegistry()
    if WIDGET_ID in registry.providers():
        registry.removeProvider(WIDGET_ID)
    _provider = None
