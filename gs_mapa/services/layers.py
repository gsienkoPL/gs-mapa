"""Service for managing QGIS layers for the Urban Register."""

import logging
from typing import Dict, List, Any
from qgis.core import QgsProject, QgsVectorLayer
from ..core.models import LayerModel, ManifestModel
from .ogr_reader import OGRReader
from .styling import StylingService
from .pog_style import layer_rank

logger = logging.getLogger("GSMapa.Layers")

class LayerService:
    """
    Service responsible for adding and removing PMTiles layers in QGIS.
    Ensures no duplicates and tracks layers via custom properties.
    """
    
    PLUGIN_ID = "gs_mapa_plugin"
    
    # Statusy aktualizacji
    STATUS_CURRENT = "Aktualna"
    STATUS_NEWER = "Dostępna nowsza wersja"
    STATUS_UNAVAILABLE = "Źródło niedostępne"
    STATUS_UNKNOWN = "Nieznany"

    def __init__(self, ogr_reader: OGRReader):

        self._ogr_reader = ogr_reader
        self._styling_service = StylingService()

    def _get_or_create_group(self, type_name: str):
        """Returns the root group for a specific data type, creating it if necessary."""
        root = QgsProject.instance().layerTreeRoot()
        group_name = f"Rejestr Urbanistyczny / {type_name}"
        
        # Find existing group
        group = root.findGroup(group_name)
        if group:
            return group
        
        # Nowa grupa na górze drzewa — nad mapami podkładowymi, które leżą na spodzie.
        group = root.insertGroup(0, group_name)
        return group

    @staticmethod
    def is_line_layer(sublayer_name: str) -> bool:
        """
        Sprawdza, czy podwarstwa jest warstwą liniową (na podstawie nazwy).

        Dopasowanie odporne na polskie odmiany: „linia”, „linie”, „liniowy”
        (rdzeń "lini") oraz angielskie „line”/„lines”.

        :param sublayer_name: nazwa podwarstwy.
        :return: True jeśli podwarstwa jest liniowa.
        """
        name = sublayer_name.lower()
        return "lini" in name or "line" in name

    #: Widżet „Przezroczystość” wtyczki (``gs_mapa.ui.transparency_widget.WIDGET_ID``).
    TRANSPARENCY_WIDGET_ID = "gs_mapa_transparency"

    @classmethod
    def enable_opacity_slider(cls, layer) -> None:
        """
        Włącza suwak „Przezroczystość” pod warstwą w panelu Warstwy.

        Widżet rejestruje wtyczka (``transparency_widget.register()``). Właściwości muszą być
        ustawione przed wstawieniem warstwy do drzewa — QGIS czyta je przy tworzeniu węzła.
        """
        layer.setCustomProperty("embeddedWidgets/count", 1)
        layer.setCustomProperty("embeddedWidgets/0/id", cls.TRANSPARENCY_WIDGET_ID)

    def _add_layer_to_group(self, layer, group, sublayer_name: str) -> None:
        """
        Dodaje warstwę do grupy w drzewie warstw na pozycji wynikającej z jej rodzaju.

        Warstwa trafia do projektu bez legendy (addToLegend=False), a następnie jest
        wstawiana w grupę według rangi (``pog_style.layer_rank``): w QGIS warstwa wyżej
        w drzewie rysuje się na wierzchu, więc kolejność w grupie nie zależy od kolejności
        dodawania. Dla POG: granica, obszary OZS/OUZ, linie stref, wypełnienia stref;
        dla pozostałych typów linie nad wypełnieniami.

        :param layer: warstwa QgsMapLayer do dodania.
        :param group: grupa QgsLayerTreeGroup docelowa.
        :param sublayer_name: nazwa podwarstwy — na jej podstawie ustalana jest pozycja.
        """
        # Wypełnienia (np. strefy) zasłaniają podkład — suwak krycia pod ręką.
        # Linie go nie dostają: zmniejszenie krycia linii niewiele daje.
        if not self.is_line_layer(sublayer_name):
            self.enable_opacity_slider(layer)
        QgsProject.instance().addMapLayer(layer, False)
        rank = layer_rank(sublayer_name, self.is_line_layer(sublayer_name))
        index = len(group.children())
        for i, node in enumerate(group.children()):
            other = node.layer() if hasattr(node, "layer") else None
            other_name = other.customProperty(f"{self.PLUGIN_ID}_sublayer") if other is not None else None
            if other_name and layer_rank(other_name, self.is_line_layer(other_name)) > rank:
                index = i
                break
        node = group.insertLayer(index, layer)
        # Strefy POG rozwinięte — legenda stref i suwak przezroczystości od razu widoczne.
        if node is not None and sublayer_name == "pog_strefy":
            node.setExpanded(True)
            group.setExpanded(True)

    def add_sublayer(self, layer_model: LayerModel, sublayer_name: str, build_id: str) -> bool:
        """
        Adds a specific sub-layer from a PMTiles archive to the QGIS project.
        
        :param layer_model: Model containing the archive URL and type.
        :param sublayer_name: The OGR layer name.
        :param build_id: The manifest build ID.
        :return: True if successfully added or already exists.
        """
        # 1. Check for existing layer with same source
        # Porównujemy źródło bez opcji otwarcia — warstwy dodane starszą wersją
        # wtyczki nie mają opcji ZOOM_LEVEL_AUTO, a to wciąż ta sama podwarstwa.
        source = OGRReader.base_source(layer_model.url, sublayer_name)
        for layer in QgsProject.instance().mapLayers().values():
            if layer.source().split("|option:")[0] == source:
                logger.info(f"Layer {sublayer_name} already exists in project. Skipping.")
                return True

        # 2. Create the layer
        label = f"{layer_model.layer_type} - {sublayer_name}"
        vlayer = self._ogr_reader.create_vector_layer(layer_model.url, sublayer_name, label)
        
        if not vlayer.isValid():
            logger.error(f"Layer {sublayer_name} from {layer_model.url} is invalid.")
            return False

        # 3. Set custom properties for tracking
        vlayer.setCustomProperty(f"{self.PLUGIN_ID}_id", self.PLUGIN_ID)
        vlayer.setCustomProperty(f"{self.PLUGIN_ID}_url", layer_model.url)
        vlayer.setCustomProperty(f"{self.PLUGIN_ID}_type", layer_model.layer_type)
        vlayer.setCustomProperty(f"{self.PLUGIN_ID}_sublayer", sublayer_name)
        vlayer.setCustomProperty(f"{self.PLUGIN_ID}_build", build_id)

        # 4. Apply default styling
        self._styling_service.apply_style(vlayer)

        # 5. Add to project in correct group
        group = self._get_or_create_group(layer_model.layer_type)
        self._add_layer_to_group(vlayer, group, sublayer_name)

        logger.info(f"Successfully added layer: {label} to group {group.name()}")
        return True

    def plugin_layers(self) -> list:
        """Warstwy projektu dodane przez wtyczkę."""
        return [
            layer for layer in QgsProject.instance().mapLayers().values()
            if layer.customProperty(f"{self.PLUGIN_ID}_id") == self.PLUGIN_ID
        ]

    def added_sublayers(self) -> set:
        """Podwarstwy obecne w projekcie jako pary (typ, nazwa podwarstwy)."""
        return {
            (layer.customProperty(f"{self.PLUGIN_ID}_type"), layer.customProperty(f"{self.PLUGIN_ID}_sublayer"))
            for layer in self.plugin_layers()
        }

    def remove_sublayers(self, layer_type: str, names) -> int:
        """
        Usuwa z projektu warstwy wtyczki danego typu o nazwach podwarstw z ``names``;
        pusta po nich grupa „Rejestr Urbanistyczny / typ” też znika.

        :return: liczba usuniętych warstw.
        """
        wanted = set(names)
        layers = [layer for layer in self.plugin_layers()
                  if layer.customProperty(f"{self.PLUGIN_ID}_type") == layer_type
                  and layer.customProperty(f"{self.PLUGIN_ID}_sublayer") in wanted]
        QgsProject.instance().removeMapLayers([layer.id() for layer in layers])
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(f"Rejestr Urbanistyczny / {layer_type}")
        if group is not None and not group.children():
            root.removeChildNode(group)
        return len(layers)

    def remove_plugin_layers(self) -> int:
        """
        Removes all layers added by this plugin.
        
        :return: Number of layers removed.
        """
        layers_to_remove = []
        for layer in QgsProject.instance().mapLayers().values():
            if layer.customProperty(f"{self.PLUGIN_ID}_id") == self.PLUGIN_ID:
                layers_to_remove.append(layer)
        
        count = len(layers_to_remove)
        for layer in layers_to_remove:
            QgsProject.instance().removeMapLayer(layer.id())
            
        logger.info(f"Removed {count} plugin layers from project.")
        return count

    def _get_sublayers(self, layer_model: LayerModel, cache: Dict[str, List[str]]) -> List[str]:
        """
        Zwraca nazwy podwarstw archiwum.

        Lista pochodzi z manifestu. Archiwum jest otwierane przez GDAL tylko wtedy,
        gdy manifest jej nie podaje — wymaga to bindingów ``osgeo`` (brak np. na
        macOS) i pobierania nagłówków zdalnego pliku, więc wynik trafia do ``cache``.

        :param layer_model: archiwum z manifestu.
        :param cache: wyniki odczytu archiwów (url -> podwarstwy) w obrębie jednego sprawdzenia.
        :return: nazwy podwarstw; pusta lista, gdy nie da się ich ustalić.
        """
        if layer_model.layers:
            return list(layer_model.layers)
        if layer_model.url not in cache:
            try:
                cache[layer_model.url] = self._ogr_reader.get_sublayers(layer_model.url)
            except Exception as e:
                logger.warning(f"Cannot read sublayers of {layer_model.url}: {e}")
                cache[layer_model.url] = []
        return cache[layer_model.url]

    def get_update_status(self, manifest: ManifestModel) -> Dict[str, Dict[str, Any]]:
        """
        Porównuje warstwy w projekcie z aktualnym manifestem.
        """
        updates = {}
        plugin_layers = [lyr for lyr in QgsProject.instance().mapLayers().values()
                         if lyr.customProperty(f"{self.PLUGIN_ID}_id") == self.PLUGIN_ID]
        sublayers_cache: Dict[str, List[str]] = {}

        for layer in plugin_layers:
            layer_id = layer.id()
            l_type = layer.customProperty(f"{self.PLUGIN_ID}_type")
            l_sub = layer.customProperty(f"{self.PLUGIN_ID}_sublayer")
            l_build = layer.customProperty(f"{self.PLUGIN_ID}_build")
            l_url = layer.customProperty(f"{self.PLUGIN_ID}_url")
            
            # Szukaj dopasowania w manifeście po typie i nazwie podwarstwy
            # (URL może się zmienić między buildami)
            matched_model = None
            for ds in manifest.datasets:
                if ds.layer_type == l_type:
                    # Sprawdzamy czy podwarstwa istnieje w tym archiwum
                    if l_sub in self._get_sublayers(ds, sublayers_cache):
                        matched_model = ds
                        break
            
            if not matched_model:
                updates[layer_id] = {
                    "status": self.STATUS_UNAVAILABLE,
                    "current_build": l_build, "new_build": None,
                    "current_url": l_url, "new_url": None,
                    "new_model": None
                }
                continue
            
            if l_build == manifest.build_id:
                updates[layer_id] = {
                    "status": self.STATUS_CURRENT,
                    "current_build": l_build, "new_build": manifest.build_id,
                    "current_url": l_url, "new_url": matched_model.url,
                    "new_model": matched_model
                }
            else:
                updates[layer_id] = {
                    "status": self.STATUS_NEWER,
                    "current_build": l_build, "new_build": manifest.build_id,
                    "current_url": l_url, "new_url": matched_model.url,
                    "new_model": matched_model
                }
        
        return updates

    def safe_update_layer(self, layer_id: str, new_model: LayerModel, build_id: str) -> bool:
        """
        Bezpiecznie aktualizuje źródło warstwy, zachowując jej właściwości.
        """
        old_layer = QgsProject.instance().mapLayer(layer_id)
        if not old_layer:
            logger.error(f"Layer {layer_id} not found in project.")
            return False
            
        sublayer_name = old_layer.customProperty(f"{self.PLUGIN_ID}_sublayer")
        if not sublayer_name:
            logger.error(f"Layer {layer_id} is missing sublayer metadata.")
            return False
            
        # 1. Utwórz nową warstwę (tymczasową)
        label = old_layer.name()
        new_layer = self._ogr_reader.create_vector_layer(new_model.url, sublayer_name, label)
        
        if not new_layer or not new_layer.isValid():
            logger.error(f"New version of layer {sublayer_name} is invalid. Aborting update.")
            return False
            
        # 2. Kopiuj właściwości
        new_layer.setCustomProperty(f"{self.PLUGIN_ID}_id", self.PLUGIN_ID)
        new_layer.setCustomProperty(f"{self.PLUGIN_ID}_url", new_model.url)
        new_layer.setCustomProperty(f"{self.PLUGIN_ID}_type", new_model.layer_type)
        new_layer.setCustomProperty(f"{self.PLUGIN_ID}_sublayer", sublayer_name)
        new_layer.setCustomProperty(f"{self.PLUGIN_ID}_build", build_id)
        
        # Stylizacja
        self._styling_service.apply_style(new_layer)
        
        # Nazwa i krycie ustawione suwakiem
        new_layer.setName(old_layer.name())
        new_layer.setOpacity(old_layer.opacity())
        
        # 3. Dodaj do projektu i do grupy
        group = self._get_or_create_group(new_model.layer_type)
        self._add_layer_to_group(new_layer, group, sublayer_name)
            
        # 4. Usuń starą warstwę
        QgsProject.instance().removeMapLayer(layer_id)
        
        logger.info(f"Successfully updated layer {sublayer_name} to build {build_id}")
        return True

