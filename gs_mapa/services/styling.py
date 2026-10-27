"""Service for applying QGIS styles to Urban Register layers."""

import logging
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsSymbol,
    QgsFillSymbol,
    QgsLineSymbol,
    QgsSingleSymbolRenderer,
)

from .pog_style import apply_pog_style

logger = logging.getLogger("GSMapa.Styling")

class StylingService:
    """
    Provides default styles for different types of urban register layers.
    """

    # Default colors
    COLOR_POG_BOUNDARY = QColor("#E6194B")  # Red
    COLOR_POG_ZONE = QColor(200, 200, 200, 100)  # Semi-transparent gray
    COLOR_OUZ_OZS = QColor("#3CB44B")  # Green
    COLOR_NEUTRAL = QColor(220, 220, 220, 150)  # Light gray
    COLOR_LINE = QColor("#000000")  # Black

    def __init__(self):
        pass

    def get_default_symbol(self, layer_type: str, sublayer_name: str) -> QgsSymbol:
        """
        Determines and creates the appropriate symbol based on layer type and name.
        """
        is_line = "_linie" in sublayer_name.lower() or "line" in sublayer_name.lower()
        
        if layer_type == "POG":
            return self._create_pog_symbol(is_line, sublayer_name)
        
        # Neutral styles for MPZP, ZPI, PZPW
        if is_line:
            return self._create_simple_line(self.COLOR_LINE, 0.3)
        return self._create_simple_fill(self.COLOR_NEUTRAL, None, 0.2)

    def _create_pog_symbol(self, is_line: bool, name: str) -> QgsSymbol:
        name_lower = name.lower()
        
        if is_line:
            # POG Boundaries / Lines
            color = self.COLOR_POG_BOUNDARY if "granica" in name_lower else self.COLOR_LINE
            return self._create_simple_line(color, 0.4)
        
        # POG Fills
        if "ouz" in name_lower or "ozs" in name_lower:
            return self._create_simple_fill(self.COLOR_OUZ_OZS, None, 0.5)
        if "strefa" in name_lower or "stref" in name_lower:
            return self._create_simple_fill(self.COLOR_POG_ZONE, self.COLOR_LINE, 0.1)
            
        return self._create_simple_fill(self.COLOR_POG_ZONE, None, 0.3)

    def _create_simple_fill(self, fill_color: QColor, stroke_color: QColor | None, stroke_width: float) -> QgsSymbol:
        symbol = QgsFillSymbol()
        symbol.setColor(fill_color)
        # Obrys i jego grubość należą do warstwy symbolu (QgsSimpleFillSymbolLayer),
        # a nie do QgsSymbol — metody setStrokeColor/setStrokeWidth/setStrokeStyle
        # istnieją tylko na warstwie.
        symbol_layer = symbol.symbolLayer(0)
        if stroke_color:
            symbol_layer.setStrokeColor(stroke_color)
            symbol_layer.setStrokeWidth(stroke_width)
        else:
            symbol_layer.setStrokeStyle(Qt.PenStyle.NoPen)
        return symbol

    def _create_simple_line(self, color: QColor, width: float) -> QgsSymbol:
        symbol = QgsLineSymbol()
        symbol.setColor(color)
        symbol.setWidth(width)
        return symbol

    def apply_style(self, layer):
        """
        Applies the default style to a QgsVectorLayer.
        """
        try:
            # Extract info from custom properties set by LayerService
            layer_type = layer.customProperty("gs_mapa_plugin_type")
            sublayer_name = layer.customProperty("gs_mapa_plugin_sublayer")
            
            if not layer_type or not sublayer_name:
                return

            # POG: styl z rozporządzenia (kolory stref, kreskowania OZS/OUZ, granica, etykiety).
            if layer_type == "POG" and apply_pog_style(layer, sublayer_name):
                layer.triggerRepaint()
                return

            symbol = self.get_default_symbol(layer_type, sublayer_name)
            renderer = QgsSingleSymbolRenderer(symbol)
            layer.setRenderer(renderer)
            
            # Trigger redraw
            layer.triggerRepaint()
            
        except Exception as e:
            logger.error(f"Failed to apply style to layer {layer.name()}: {e}")
