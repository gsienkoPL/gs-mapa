"""Główna klasa wtyczki GS Mapa."""

import logging

from qgis.PyQt.QtCore import QCoreApplication, QSettings, QSize, Qt, QTimer
from qgis.PyQt.QtGui import QAction
from qgis.PyQt.QtWidgets import QToolButton
from qgis.core import Qgis, QgsMessageLog

from .core.config import GSMapaConfig
from .services.basemaps import BasemapService
from .services.layers import LayerService
from .services.manifest import ManifestService
from .services.ogr_reader import OGRReader
from .ui import theme, transparency_widget
from .ui.basemap_menu import BasemapMenu
from .ui.dock import GSMapaDock
from .ui.plan_info import PlanInfoMapTool

# Konfiguracja logowania
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("GSMapa.Plugin")

MENU_TITLE = "&GS Mapa"
#: Klucz ustawień z czasów, gdy wtyczka nazywała się „Rejestr Urbanistyczny” — bez zmian,
#: żeby nie zgubić zapisanych ustawień użytkowników.
SETTINGS_GROUP = "RejestrUrbanistyczny"
#: Nazwa paska narzędzi wtyczki.
TOOLBAR_NAME = "GS Mapa"


class GSMapaPlugin:
    """
    Wtyczka GS Mapa.

    Pasek narzędzi „GS Mapa” (pkt 1 wytycznych): przełącznik panelu, narzędzie mapy
    „Informacja o planie” i menu „Podkład ▾”; te same akcje w menu wtyczki.
    """

    def __init__(self, iface):
        self.iface = iface
        logger.info("Inicjalizacja wtyczki GS Mapa.")

        try:
            self.config = GSMapaConfig(QSettings(SETTINGS_GROUP, "Rejestr Urbanistyczny"))
        except Exception as e:
            logger.error(f"Błąd podczas ładowania konfiguracji: {e}")
            self.config = GSMapaConfig()

        #: Przełącznik panelu (dawna akcja „GS Mapa” — ta sama nazwa obiektu).
        self.action: QAction | None = None
        self.plan_info_action: QAction | None = None
        self.toolbar = None
        self.basemap_menu: BasemapMenu | None = None
        self.basemap_button: QToolButton | None = None
        self._plan_info_tool: PlanInfoMapTool | None = None
        self._previous_tool = None
        self.dock: GSMapaDock | None = None

        # Inicjalizacja usług
        self.manifest_service = ManifestService()
        self.ogr_reader = OGRReader()
        self.layer_service = LayerService(self.ogr_reader)
        self.basemap_service = BasemapService()

    def initGui(self) -> None:  # noqa: N802 - nazwa z API QGIS
        """Pasek narzędzi, menu wtyczki i suwak przezroczystości."""
        try:
            # Suwak „Przezroczystość” pod warstwami w panelu Warstwy.
            transparency_widget.register()
            parent = self.iface.mainWindow()

            action = QAction(self.tr("Panel GS Mapa"), parent)
            action.setObjectName("GSMapaAction")
            action.setCheckable(True)
            action.setToolTip(self.tr("Panel GS Mapa: plany, pliki APP i dane GUGiK"))
            action.setStatusTip(self.tr("Pokaż albo ukryj panel wtyczki GS Mapa"))
            theme.set_icon(action, "panel", role="WindowText")
            action.toggled.connect(self._on_panel_toggled)
            self.action = action

            info = QAction(self.tr("Informacja o planie"), parent)
            info.setObjectName("GSMapaPlanInfoAction")
            info.setCheckable(True)
            info.setToolTip(self.tr("Informacja o planie — kliknij plan, by zobaczyć jego akt, status i dokumenty"))
            theme.set_icon(info, "plan-info", role="WindowText")
            info.triggered.connect(self._on_plan_info_triggered)
            self.plan_info_action = info

            self.basemap_menu = BasemapMenu(self.basemap_service, parent)
            self.basemap_menu.message.connect(self._log)
            self.basemap_menu.menuAction().setObjectName("GSMapaBasemapMenu")
            theme.set_icon(self.basemap_menu.menuAction(), "map", role="WindowText")

            self.toolbar = self.iface.addToolBar(TOOLBAR_NAME)
            self.toolbar.setObjectName("GSMapaToolbar")
            self.toolbar.addAction(action)
            self.toolbar.addAction(info)
            button = QToolButton(self.toolbar)
            button.setObjectName("GSMapaBasemapButton")
            button.setText(self.tr("Podkład"))
            button.setToolTip(self.tr("Podkład pod danymi z rejestru"))
            button.setAccessibleName(self.tr("Podkład"))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            button.setIconSize(QSize(theme.TOOLBAR_SIZE, theme.TOOLBAR_SIZE))
            button.setMenu(self.basemap_menu)
            theme.set_icon(button, "map", role="ButtonText")
            self.toolbar.addWidget(button)
            self.basemap_button = button

            for item in (action, info, self.basemap_menu.menuAction()):
                self.iface.addPluginToMenu(MENU_TITLE, item)

            QTimer.singleShot(0, self.basemap_service.fetch_versions)
            QgsMessageLog.logMessage(self.tr("Wtyczka GS Mapa zainicjalizowana."), "GS Mapa", Qgis.MessageLevel.Info)
            logger.info("initGui zakończone sukcesem.")
        except Exception as e:
            logger.error(f"Błąd podczas initGui: {e}", exc_info=True)

    def unload(self) -> None:
        """Sprzątanie zasobów wtyczki."""
        logger.info("Wywoływanie unload().")
        self._deactivate_plan_info()
        if self.basemap_menu is not None:
            for item in (self.action, self.plan_info_action, self.basemap_menu.menuAction()):
                if item is not None:
                    self.iface.removePluginMenu(MENU_TITLE, item)
            self.basemap_menu.shutdown()
        if self.toolbar is not None:
            self.toolbar.clear()
            self.toolbar.hide()
            self.toolbar.deleteLater()
            self.toolbar = None
        self.action = self.plan_info_action = self.basemap_menu = self.basemap_button = None

        transparency_widget.unregister()

        if self.dock is not None:
            self.dock.deactivate_tools()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        theme.shutdown()
        logger.info("unload() zakończony.")

    # --- panel ------------------------------------------------------------------------------

    def _ensure_dock(self) -> GSMapaDock:
        """Panel (tworzony raz); nowy panel trafia do interfejsu QGIS."""
        if self.dock is None:
            self.dock = GSMapaDock(self.iface, self.config, self.manifest_service, self.layer_service,
                                   self.ogr_reader, basemap_service=self.basemap_service)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
            self.dock.visibilityChanged.connect(self._on_dock_visibility)
        return self.dock

    def run(self) -> None:
        """Pokazuje panel."""
        try:
            dock = self._ensure_dock()
            dock.setVisible(True)
            dock.raise_()
            self._on_dock_visibility(True)
            logger.info("Panel GS Mapa wyświetlony.")
        except Exception as e:
            logger.error(f"Błąd podczas uruchamiania panelu: {e}", exc_info=True)

    def _on_panel_toggled(self, checked: bool) -> None:
        if checked:
            self.run()
        elif self.dock is not None:
            self.dock.setVisible(False)

    def _on_dock_visibility(self, visible: bool) -> None:
        """Przełącznik na pasku nadąża za panelem (zamknięcie krzyżykiem, menu Widok → Panele)."""
        if self.action is not None and self.action.isChecked() != visible:
            self.action.blockSignals(True)
            self.action.setChecked(visible)
            self.action.blockSignals(False)

    # --- Informacja o planie -------------------------------------------------------------------

    def _on_plan_info_triggered(self, checked: bool) -> None:
        """Narzędzie mapy „Informacja o planie” — jak standardowe Identify: wyłącza je inne narzędzie."""
        if not checked:
            self._deactivate_plan_info()
            return
        canvas = self.iface.mapCanvas()
        if self._plan_info_tool is None:
            self._plan_info_tool = PlanInfoMapTool(canvas)
            # setAction: QGIS sam odznacza akcję, gdy użytkownik wybierze inne narzędzie mapy.
            self._plan_info_tool.setAction(self.plan_info_action)
            self._plan_info_tool.acts_found.connect(self._on_acts_found)
        if canvas.mapTool() is not self._plan_info_tool:
            self._previous_tool = canvas.mapTool()
        canvas.setMapTool(self._plan_info_tool)
        # Panel (indeks, dokumenty aktu) musi istnieć, ale nie musi być widoczny.
        if self.dock is None:
            self._ensure_dock().setVisible(False)

    def _deactivate_plan_info(self) -> None:
        tool = self._plan_info_tool
        if tool is None or tool.canvas().mapTool() is not tool:
            return
        canvas = tool.canvas()
        previous, self._previous_tool = self._previous_tool, None
        try:
            if previous is not None:
                canvas.setMapTool(previous)
                return
        except RuntimeError:
            pass  # poprzednie narzędzie zostało już usunięte
        canvas.unsetMapTool(tool)

    def _on_acts_found(self, hits: list) -> None:
        self._ensure_dock().show_plan_info(hits)

    # --- pomocnicze ----------------------------------------------------------------------------

    def _log(self, message: str, level: str = "INFO") -> None:
        if self.dock is not None:
            self.dock.log(message, level)
            return
        qgis_level = {"UWAGA": Qgis.MessageLevel.Warning, "BŁĄD": Qgis.MessageLevel.Critical}.get(
            level, Qgis.MessageLevel.Info)
        QgsMessageLog.logMessage(message, "GS Mapa", qgis_level)
        if level == "BŁĄD" and hasattr(self.iface, "messageBar"):
            self.iface.messageBar().pushMessage("GS Mapa", message, level=qgis_level, duration=10)

    def tr(self, text: str) -> str:
        return QCoreApplication.translate("GSMapaPlugin", text)

    @staticmethod
    def icon_path():
        from pathlib import Path
        return Path(__file__).parent / "icons" / "gs_mapa.svg"
