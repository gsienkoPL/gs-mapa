"""Podsumowanie braków w tabelach wzorcowego pliku APP po zapisaniu zmian.

Wzór wypełnia się w tabeli atrybutów albo w formularzu, a kontrolę kompletności robi dopiero
„Sprawdź dane” przy generowaniu GML — czyli po wielu obiektach. Ten moduł pilnuje tabel wzoru
w projekcie: po zatwierdzeniu zmian liczy obiekty bez pól wymaganych i podaje, czego brakuje.

Moduł nie zmienia danych i nie blokuje zapisu — tylko informuje. Obliczenia są w
``core/app_forms.missing_required`` (bez QGIS), tutaj jest obsługa projektu i sygnałów.
"""

from __future__ import annotations

import os
from functools import partial
from typing import Dict, Optional

from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal
from qgis.PyQt import sip

from ..core.app_forms import missing_required, missing_summary
from ..core.app_gml import ACT
from .app_template import VALUES_PROPERTY, table_of

#: Opóźnienie podsumowania po zatwierdzeniu zmian — jeden komunikat na serię zapisów.
DELAY_MS = 400


class TemplateCheck(QObject):
    """Braki danych w tabelach wzoru: po każdym zatwierdzeniu zmian jedno zdanie do paska."""

    #: (treść komunikatu, poziom ``INFO``/``UWAGA``)
    report = pyqtSignal(str, str)

    def __init__(self, project: Optional[QgsProject] = None, parent=None):
        super().__init__(parent)
        self._project = project or QgsProject.instance()
        self._layers: Dict[str, QgsVectorLayer] = {}
        self._timers: Dict[str, QTimer] = {}
        self._connected = False

    # --- podłączenie ---------------------------------------------------------------

    def attach(self) -> None:
        """Podłącza podsumowanie do tabel wzoru już w projekcie i dodawanych później."""
        if self._connected:
            return
        self._project.layerWasAdded.connect(self._on_layer_added)
        # ``layerWillBeRemoved`` przed zniszczeniem obiektu C++ — po ``clear()`` projektu
        # odpinanie sygnałów martwej warstwy kończy proces (wyjątek w slocie z C++).
        self._project.layerWillBeRemoved.connect(self._on_layer_will_be_removed)
        self._connected = True
        for layer in self._project.mapLayers().values():
            self._watch(layer)

    def detach(self) -> None:
        """Odpina sygnały i zatrzymuje odliczanie (przy rozładowaniu wtyczki)."""
        if not self._connected:
            return
        for signal, slot in ((self._project.layerWasAdded, self._on_layer_added),
                             (self._project.layerWillBeRemoved, self._on_layer_will_be_removed)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        for layer_id in list(self._layers):
            self._unwatch(layer_id)
        self._connected = False

    # --- tabele wzoru --------------------------------------------------------------

    @staticmethod
    def _act_source(path: str) -> Optional[str]:
        """Źródło tabeli aktu tego samego pliku w projekcie (``None``, gdy jej nie ma)."""
        wanted = os.path.normcase(os.path.abspath(path))
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsVectorLayer) and layer.providerType() == "ogr":
                source, table = table_of(layer)
                if table == ACT and os.path.normcase(os.path.abspath(source)) == wanted:
                    return layer.id()
        return None

    def template_table(self, layer) -> str:
        """
        Tabela wzoru, którą jest warstwa.

        Tabelę wzoru poznaje się po danych wzoru zapisanych na tabeli aktu tego samego pliku
        (``VALUES_PROPERTY``) albo po samej tabeli aktu, która je ma.

        :return: nazwa tabeli (pusta, gdy warstwa nie jest tabelą wzoru).
        """
        if not isinstance(layer, QgsVectorLayer) or layer.providerType() != "ogr":
            return ""
        path, table = table_of(layer)
        if not table or not path:
            return ""
        if layer.customProperty(VALUES_PROPERTY):
            return table
        return table if self._act_source(path) else ""

    def _watch(self, layer) -> None:
        if not isinstance(layer, QgsVectorLayer) or layer.id() in self._layers:
            return
        if not self.template_table(layer):
            return
        self._layers[layer.id()] = layer
        for signal in self._signals(layer):
            signal.connect(self._on_commit)

    @staticmethod
    def _signals(layer: QgsVectorLayer):
        return (layer.committedFeaturesAdded, layer.committedFeaturesRemoved,
                layer.committedAttributeValuesChanges)

    def _unwatch(self, layer_id: str) -> None:
        layer = self._layers.pop(layer_id, None)
        if layer is not None and sip.isdeleted(layer):
            layer = None
        timer = self._timers.pop(layer_id, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        if layer is None:
            return
        for signal in self._signals(layer):
            try:
                signal.disconnect(self._on_commit)
            except (TypeError, RuntimeError):
                pass

    def _on_layer_added(self, layer) -> None:
        self._watch(layer)

    def _on_layer_will_be_removed(self, layer_id: str) -> None:
        self._unwatch(layer_id)

    # --- podsumowanie --------------------------------------------------------------

    def _on_commit(self, *args) -> None:
        layer = self.sender()
        if not isinstance(layer, QgsVectorLayer) or layer.id() not in self._layers:
            return
        timer = self._timers.get(layer.id())
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(DELAY_MS)
            timer.timeout.connect(partial(self.summarize_id, layer.id()))
            self._timers[layer.id()] = timer
        timer.start()

    def summarize_id(self, layer_id: str) -> str:
        """Podsumowanie dla warstwy o identyfikatorze (slot odliczania)."""
        return self.summarize(self._layers.get(layer_id))

    def summarize(self, layer) -> str:
        """
        Liczy braki w tabeli i emituje komunikat (pusty — tabela kompletna).

        :param layer: warstwa tabeli wzoru.
        :return: treść komunikatu (do testów i dziennika).
        """
        if layer is None or not layer.isValid():
            return ""
        table = self.template_table(layer)
        if not table:
            return ""
        names = layer.fields().names()
        rows = [{name: feature[name] for name in names} for feature in layer.getFeatures()]
        incomplete, counts = missing_required(table, rows)
        text = missing_summary(table, incomplete, len(rows), counts)
        if text:
            self.report.emit(text, "UWAGA")
        return text
