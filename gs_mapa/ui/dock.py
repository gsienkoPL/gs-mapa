"""Panel (dock) wtyczki GS Mapa: pasek kontekstu jednostki i karty (Rejestr Urbanistyczny, GUGiK, Pliki APP)."""

import logging

from qgis.PyQt.QtCore import Qt, QCoreApplication, QTimer
from qgis.PyQt.QtGui import QKeySequence, QShortcut
from qgis.PyQt.QtWidgets import (
    QMessageBox,
    QDialog,
    QFrame,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from qgis.gui import QgsDockWidget, QgsScrollArea
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCsException,
    QgsMessageLog,
    QgsProject,
    QgsRectangle,
)

from ..core.act_index import ActIndexEntry, count_by_type, short_title
from ..services import act_files
from ..services.download import FileDownloader
from ..core.units import creator_unit
from ..core.config import GSMapaConfig, MANIFEST_URL
from ..core.labels import format_date, plural_layers
from ..services.manifest import ManifestService
from ..services.layers import LayerService
from ..services.ogr_reader import OGRReader
from ..services.acts import ActService
from ..services.act_index import ActIndexService
from ..services.basemaps import BasemapService
from ..services.eziudp import EziudpService
from ..services.opendata import OpendataService
from ..services.uldk import UldkService
from ..core.models import ManifestModel, LayerModel
from .dialogs import UpdateConfirmationDialog
from .eziudp_tab import EziudpTab
from .opendata_tab import OpendataTab
from .app_tab import AppTab
from .national_layers import NationalLayersPanel
from .plan_info import (
    ActHit, PlanInfoDialog, act_layers, acts_on_map, add_vector_layers, is_bucket_url, remove_act_layers,
    resolve_local_hits,
)
from .search_panel import ActSearchPanel
from .unit_context import ContextBar, UnitContext
from .widgets import Section
from . import theme

logger = logging.getLogger("GSMapa.Dock")

#: Nazwa zakładki wtyczki w panelu „Dziennik komunikatów” QGIS.
LOG_TAG = "GS Mapa"

#: Poziomy komunikatów: poziom QGIS i poziom logu Pythona.
_LEVELS = {
    "INFO": (Qgis.MessageLevel.Info, logging.INFO),
    "UWAGA": (Qgis.MessageLevel.Warning, logging.WARNING),
    "BŁĄD": (Qgis.MessageLevel.Critical, logging.ERROR),
}

#: Klucze kart doku w kolejności wyświetlania (zapis ostatnio otwartej karty).
TAB_KEYS = ("plany", "app", "gugik")

#: Kolejność typów w podpowiedzi indeksu.
_TYPE_SORT = {"pog": 0, "mpzp": 1, "zpi": 2, "pzpw": 3}


class GSMapaDock(QgsDockWidget):
    """Panel boczny GS Mapa: pasek kontekstu jednostki i karty Plany, Pliki APP, GUGiK."""

    def __init__(self, iface, config: GSMapaConfig,
                 manifest_service: ManifestService,
                 layer_service: LayerService,
                 ogr_reader: OGRReader,
                 parent=None,
                 act_service: ActService | None = None,
                 basemap_service: BasemapService | None = None,
                 act_index_service: ActIndexService | None = None,
                 eziudp_service: EziudpService | None = None,
                 uldk_service: UldkService | None = None,
                 opendata_service: OpendataService | None = None,
                 unit_context: UnitContext | None = None):
        super().__init__(self.tr("GS Mapa"), parent)
        # Własny wygląd wtyczki (makiety docs/ui), niezależny od stylu Qt w QGIS.
        theme.apply_plugin_style(self)
        self.setObjectName("GSMapaDock")
        self._iface = iface
        self._config = config
        self._manifest_service = manifest_service
        self._layer_service = layer_service
        self._ogr_reader = ogr_reader
        self._act_service = act_service if act_service is not None else ActService()
        # Podkłady obsługuje pasek narzędzi wtyczki (menu „Podkład”); parametr zostaje dla zgodności.
        self._basemap_service = basemap_service
        self._eziudp_service = eziudp_service if eziudp_service is not None else EziudpService()
        self._uldk_service = uldk_service if uldk_service is not None else UldkService()
        self._opendata_service = opendata_service if opendata_service is not None else OpendataService()
        # Osobna instancja ULDK: wynik kliknięcia w jednej zakładce nie zmienia filtrów drugiej.
        self._opendata_uldk_service = UldkService()
        self._act_index_service = act_index_service if act_index_service is not None else ActIndexService()
        # Jedna jednostka dla całej wtyczki (pasek kontekstu); własna instancja ULDK do „Wskaż na mapie”.
        self._unit_context = unit_context if unit_context is not None else UnitContext(config)
        self._context_uldk_service = UldkService()
        #: Ostatni komunikat o wyniku akcji (pasek komunikatów QGIS; w testach — do sprawdzenia).
        self._last_message = ""
        #: Akty, dla których „Dodaj warstwy planu” czeka na plik ``akty/{act_id}.json``.
        self._pending_act_layers: set = set()
        #: Pliki planów pobierane do bufora: adres w buckecie -> (nazwa grupy, ``act_id``).
        self._pending_act_files: dict = {}
        self._act_downloader = FileDownloader()
        self._act_downloader.finished.connect(self._on_act_file_ready)
        self._act_downloader.failed.connect(self._on_act_file_failed)
        #: Wpisy indeksu aktów — dopasowanie aktów z plików dodanych do mapy (typ + TERYT).
        self._act_entries: list = []

        self._current_manifest: ManifestModel | None = None
        self._plan_info_dialog: PlanInfoDialog | None = None

        self._setup_ui()
        self._connect_signals()
        self._on_unit_changed(self._unit_context.unit())

    def _setup_ui(self) -> None:
        """Buduj interfejs użytkownika."""
        # Karta „Plany”: sekcje „Akty planistyczne” i „Warstwy krajowe” w przewijanym obszarze.
        self._search_box = Section(self.tr("Akty planistyczne"), "acts", self._config)
        self._search_panel = ActSearchPanel()
        self._search_panel.set_selected_types(self._config.search_types)
        self._search_panel.set_selected_status(self._config.search_status)
        self._search_box.body_layout.addWidget(self._search_panel)

        # „Warstwy krajowe”: drzewo typ danych -> podwarstwy (archiwa PMTiles z manifestu).
        self._layers_box = Section(self.tr("Warstwy krajowe"), "layers", self._config)
        self._layers_box.set_context(self.tr("Rejestr Urbanistyczny"))
        self._layers_panel = NationalLayersPanel(self._layer_service, self._sublayers_of, self._config)
        self._layers_box.body_layout.addWidget(self._layers_panel)

        register_tab = self._scroll_tab(self._search_box, self._layers_box)
        # Mapa pobierana dopiero przy użyciu „Z mapy” (atrapy interfejsu w testach jej nie mają).
        self._eziudp_tab = EziudpTab(self._eziudp_service, self._config,
                                     uldk_service=self._uldk_service, canvas=self._canvas)
        self._opendata_tab = OpendataTab(self._opendata_service, self._config,
                                         uldk_service=self._opendata_uldk_service, canvas=self._canvas)
        self._app_tab = AppTab(self._config, canvas=self._canvas)

        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        self._tabs.addTab(register_tab, self.tr("Plany"))
        self._tabs.addTab(self._app_tab, self.tr("Pliki APP"))
        self._tabs.addTab(self._gugik_tab(), self.tr("GUGiK"))
        for index, key in enumerate(TAB_KEYS):
            self._tabs.widget(index).setProperty("gsmapa_tab", key)
        self._tabs.setCurrentIndex(self._initial_tab_index())

        # Pasek kontekstu (jednostka, indeks Rejestru) nad kartami — wspólny dla całego doku.
        self._context_bar = ContextBar(self._unit_context, uldk_service=self._context_uldk_service,
                                       canvas=self._canvas)
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)

        container = QWidget(self)
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)
        container_layout.addWidget(self._context_bar)
        container_layout.addWidget(line)
        container_layout.addWidget(self._tabs, 1)
        self.setWidget(container)

    @staticmethod
    def _scroll_tab(*sections) -> QWidget:
        """
        Zawartość karty w przewijanym obszarze (pkt 6.2): marginesy 12 px, odstęp między sekcjami 16 px;
        sekcje u góry, reszta miejsca pusta (listy nie rozciągają się, by ją wypełnić).
        """
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(16)
        for section in sections:
            layout.addWidget(section)
        layout.addStretch(1)
        scroll = QgsScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        return scroll

    def _gugik_tab(self) -> QWidget:
        """
        Zakładka „GUGiK”: usługi EZiUDP i pakiet danych powiatu w zwijanych sekcjach.

        Dawne osobne zakładki „Usługi EZiUDP” i „Pobieranie danych” dotyczyły
        usług GUGiK — teraz są jedną zakładką, a jej sekcje można zwijać
        (stan zwinięcia trzyma ustawienia wtyczki, jak sekcje karty „Plany”).
        Układ jak karta „Plany”: marginesy 12 px i przewijanie (pkt 5, 6.2).
        """
        sections = []
        # Kontekst sekcji po prawej (pkt 6.2): rodzaje usług i charakter danych.
        for title, key, context, widget in (
            (self.tr("Usługi EZiUDP"), "eziudp", "WMS · WFS", self._eziudp_tab),
            (self.tr("Pakiet danych powiatu"), "opendata", self.tr("otwarte dane GUGiK"), self._opendata_tab),
        ):
            if self._config is not None:
                self._config.migrate_collapsed(
                    f"gugik_{key}", f"QgsCollapsibleGroupBox/GSMapaDock/gsmapa_gugik_{key}")
            box = Section(title, f"gugik_{key}", self._config)
            box.set_context(context)
            box.body_layout.addWidget(widget)
            sections.append(box)
        return self._scroll_tab(*sections)

    def _initial_tab_index(self) -> int:
        """
        Ostatnio otwarta karta (klucz ``last_tab_key``).

        Migracja: dawny numer karty ``last_tab`` odnosił się do kart Rejestr, Mapy podkładowe,
        GUGiK, Pliki APP (przed złączeniem kart GUGiK — 3 = „Pobieranie danych”); karta
        „Mapy podkładowe” przeszła na pasek narzędzi, więc jej numer prowadzi do pierwszej karty.
        """
        key = self._config.last_tab_key
        if not key:
            old = self._config.last_tab
            if not self._config.last_tab_migrated and old == 3:
                old = 2
            key = {2: "gugik", 3: "app"}.get(old, "plany")
            self._config.last_tab_key = key
            self._config.last_tab_migrated = True
        return TAB_KEYS.index(key) if key in TAB_KEYS else 0

    def _connect_signals(self) -> None:
        """Łączy sygnały UI i usług."""
        self._context_bar.refresh_requested.connect(self._on_refresh_clicked)
        self._context_bar.message.connect(self.log)
        self._unit_context.changed.connect(self._on_unit_changed)
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._eziudp_tab.message.connect(self.log)
        self._opendata_tab.message.connect(self.log)
        self._app_tab.message.connect(self.log)
        self._manifest_service.manifest_updated.connect(self._on_manifest_updated)
        self._manifest_service.fetch_error.connect(self._on_manifest_error)

        self._layers_panel.message.connect(self.log)
        self._layers_panel.notify.connect(self._notify)
        self._layers_panel.remove_all_requested.connect(self._on_remove_plugin_clicked)
        self._layers_panel.update_requested.connect(self._on_update_selected_clicked)
        self._act_service.details_ready.connect(self._on_act_details_ready)
        self._act_service.details_error.connect(self._on_act_details_error)

        # Wyszukiwarka aktów: indeks z manifestu, akcje na wybranym akcie.
        self._act_index_service.index_ready.connect(self._on_act_index_ready)
        self._act_index_service.index_error.connect(self._on_act_index_error)
        self._search_panel.show_on_map.connect(self._on_search_show_on_map)
        self._search_panel.details_requested.connect(self._on_search_details)
        self._search_panel.filters_changed.connect(self._on_search_filters_changed)
        self._search_panel.add_layers_requested.connect(self.add_act_layers)
        self._search_panel.remove_layers_requested.connect(self.remove_act_layers)
        self._search_panel.new_version_requested.connect(self._app_tab.update_act)
        self._search_panel.validate_requested.connect(self._on_search_validate)
        self._search_panel.selection_changed.connect(self._app_tab.set_selected_act)
        self._search_panel.choose_unit_requested.connect(self._context_bar.choose)
        # Ctrl/⌘+F — pole szukania aktów (karta Plany).
        find = QShortcut(QKeySequence(QKeySequence.StandardKey.Find), self)
        find.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        find.activated.connect(self._focus_search)
        self._search_panel.set_on_map(acts_on_map())

        # Kolumna „w projekcie” i przyciski nadążają za zmianami w projekcie,
        # także gdy użytkownik usunie warstwę ręcznie.
        project = QgsProject.instance()
        project.layersAdded.connect(self._on_project_layers_changed)
        project.layersRemoved.connect(self._on_project_layers_changed)
        project.cleared.connect(self._on_project_layers_changed)

        # Automatyczne sprawdzanie aktualizacji
        self._update_timer = QTimer()
        self._update_timer.timeout.connect(self._on_refresh_clicked)
        self._update_timer.start(self._config.check_interval * 60 * 1000)

        # Lista warstw pochodzi z manifestu — pobierz go od razu po otwarciu
        # panelu, bez czekania na kliknięcie. Wersje stylów podkładów — też.
        QTimer.singleShot(0, self._on_refresh_clicked)

    def _on_tab_changed(self, index: int) -> None:
        if 0 <= index < len(TAB_KEYS):
            self._config.last_tab_key = TAB_KEYS[index]

    def log(self, message: str, level: str = "INFO") -> None:
        """
        Zapisuje komunikat w dzienniku komunikatów QGIS.

        Komunikat trafia do zakładki „Rejestr Urbanistyczny” w panelu
        „Dziennik komunikatów” oraz do logu Pythona. Błędy są dodatkowo
        pokazywane na pasku komunikatów QGIS, żeby nie przeszły niezauważone.

        :param message: treść komunikatu.
        :param level: poziom komunikatu: ``INFO``, ``UWAGA`` albo ``BŁĄD``.
        """
        qgis_level, py_level = _LEVELS.get(level, _LEVELS["INFO"])
        QgsMessageLog.logMessage(message, LOG_TAG, qgis_level)
        logger.log(py_level, message)

        if level == "BŁĄD" and hasattr(self._iface, "messageBar"):
            self._iface.messageBar().pushMessage(LOG_TAG, message, level=qgis_level, duration=10)

    def _notify(self, text: str, level: str = "info") -> None:
        """
        Wynik akcji użytkownika na pasku komunikatów QGIS (``info``, ``success``, ``warning``).

        Panel nie ma już własnej etykiety stanu — zdarzenia globalne idą przez ``iface.messageBar()``
        z czasem wyświetlania (pkt 6.7 wytycznych).
        """
        self._last_message = text
        if hasattr(self._iface, "messageBar"):
            bar = self._iface.messageBar()
            push = {"success": bar.pushSuccess, "warning": bar.pushWarning}.get(level, bar.pushInfo)
            push(LOG_TAG, text)

    def _update_index_state(self) -> None:
        """Druga linia paska kontekstu: data indeksu Rejestru, szczegóły w podpowiedzi."""
        manifest = self._current_manifest
        if manifest is None:
            return
        entries = self._act_index_service.entries
        lines = [
            f"{self.tr('Build')}: {manifest.build_id}",
            f"{self.tr('Utworzono')}: {manifest.created}",
            f"{self.tr('Typy')}: {', '.join(manifest.types)}",
            f"{self.tr('Archiwa')}: {len(manifest.datasets)}",
        ]
        if entries:
            counts = count_by_type(entries)
            lines.append(self.tr("W indeksie: ") + ", ".join(
                f"{typ.upper()} {counts[typ]}" for typ in sorted(counts, key=lambda t: _TYPE_SORT.get(t, 9))))
        state, text = "ok", f"{self.tr('Indeks Rejestru z')} {format_date(manifest.created)}"
        newer = self._newer_layers_count()
        if newer:
            state = "stale"
            lines.append(self.tr("Na mapie są warstwy starszego wydania: {0}").format(newer))
        self._context_bar.set_index_state(state, text, "\n".join(lines))

    def _newer_layers_count(self) -> int:
        if not self._current_manifest:
            return 0
        updates = self._layer_service.get_update_status(self._current_manifest)
        return sum(1 for u in updates.values() if u["status"] == self._layer_service.STATUS_NEWER)

    def _on_unit_changed(self, unit) -> None:
        """Nowa jednostka kontekstu — karty filtrują się nią domyślnie."""
        self._search_panel.set_unit(unit)
        self._app_tab.set_unit(creator_unit(unit))
        self._eziudp_tab.set_context_unit(unit)
        self._opendata_tab.set_context_unit(unit)

    def _on_refresh_clicked(self) -> None:
        """Obsługa kliknięcia przycisku odśwież."""
        self._context_bar.set_index_state("loading", self.tr("Pobieranie indeksu Rejestru…"))
        self.log(f"Pobieranie manifestu: {MANIFEST_URL}")
        self._manifest_service.fetch_manifest()

    def _on_manifest_updated(self, manifest: ManifestModel) -> None:
        """Aktualizuje UI po pobraniu manifestu."""
        build_changed = self._current_manifest is None or self._current_manifest.build_id != manifest.build_id
        if build_changed:
            self._act_service.clear_cache()
        self._current_manifest = manifest
        self._app_tab.set_acts_url_template(manifest.acts_url_template)
        self._update_index_state()

        self.log(
            f"Manifest pobrany: build {manifest.build_id}, "
            f"typy: {', '.join(manifest.types)}, archiwa: {len(manifest.datasets)}"
        )

        self._layers_panel.set_manifest(manifest)
        self._refresh_layer_states()

        # Indeks aktów do wyszukiwarki — pobierany raz na build.
        if manifest.acts_index_url and (build_changed or not self._act_index_service.entries):
            self._search_panel.set_loading()
        self._act_index_service.fetch(manifest.acts_index_url, force=build_changed)

    def _on_manifest_error(self, error_msg: str) -> None:
        """Obsługa błędów pobierania manifestu."""
        self._context_bar.set_index_state(
            "error", self.tr("Nie udało się pobrać indeksu — odśwież"),
            self.tr("Indeks Rejestru jest niedostępny: {0}").format(error_msg),
        )
        if self._current_manifest is None:
            self._layers_panel.set_placeholder(self.tr("Nie udało się pobrać listy warstw — użyj „Odśwież indeks”."))
        self.log(f"Nie udało się pobrać manifestu: {error_msg}", "BŁĄD")

    def _sublayers_of(self, ds: LayerModel) -> list[str]:
        """
        Podwarstwy archiwum: z manifestu, a gdy ich tam nie ma — z archiwum.

        :param ds: archiwum z manifestu.
        :return: nazwy podwarstw; pusta lista, gdy nie da się ich ustalić.
        """
        if ds.layers:
            return list(ds.layers)
        # Manifest nie podaje listy podwarstw — sięgnij po nią z archiwum
        # (wymaga bindingów GDAL).
        try:
            return self._ogr_reader.get_sublayers(ds.url)
        except Exception as e:
            self.log(f"Nie udało się pobrać podwarstw {ds.url}: {e}", "BŁĄD")
            return []

    def _on_project_layers_changed(self, *args) -> None:
        self._refresh_layer_states()
        self._search_panel.set_on_map(acts_on_map())

    def _focus_search(self) -> None:
        self._tabs.setCurrentIndex(TAB_KEYS.index("plany"))
        self._search_panel.focus_search()

    def _refresh_layer_states(self) -> None:
        """Stan warstw krajowych na mapie i odnośnik „Zaktualizuj N warstw…”."""
        self._layers_panel.refresh_states()
        newer = self._newer_layers_count()
        self._layers_panel.set_update_count(newer)
        self._update_index_state()

    def _plugin_layer_count(self) -> int:
        return len(self._layer_service.plugin_layers()) + sum(len(act_layers(a)) for a in acts_on_map())

    def _on_remove_plugin_clicked(self) -> None:
        """„Usuń z mapy wszystkie warstwy wtyczki…” — warstwy krajowe i warstwy planów, po potwierdzeniu."""
        national = len(self._layer_service.plugin_layers())
        plans = sorted(acts_on_map())
        count = national + sum(len(act_layers(a)) for a in plans)
        if count == 0:
            self._notify(self.tr("Na mapie nie ma warstw wtyczki"))
            return
        answer = QMessageBox.question(
            self,
            self.tr("Usuń z mapy warstwy wtyczki"),
            self.tr("Usunąć z mapy {0} dodane przez wtyczkę (warstwy krajowe i warstwy planów)? "
                    "Podkłady zostają.").format(plural_layers(count)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = self._layer_service.remove_plugin_layers()
        for act_id in plans:
            removed += remove_act_layers(act_id)
        self.log(f"Usunięto {removed} warstw wtyczki.")
        self._notify(f"{self.tr('Usunięto z mapy')} {plural_layers(removed)}", "success")
        self._refresh_layer_states()
        self._search_panel.set_on_map(acts_on_map())

    def _on_update_selected_clicked(self) -> None:
        """Aktualizuje zaznaczone warstwy do nowszej wersji."""
        if not self._current_manifest:
            return

        # Znajdź wszystkie warstwy wymagające aktualizacji
        all_updates = self._layer_service.get_update_status(self._current_manifest)

        # Filtruj tylko te, które mają status STATUS_NEWER
        to_update = []
        for lid, info in all_updates.items():
            if info["status"] == self._layer_service.STATUS_NEWER:
                # Pobierz nazwę warstwy dla dialogu
                layer = QgsProject.instance().mapLayer(lid)
                name = layer.name() if layer else "Nieznana"

                update_data = info.copy()
                update_data["id"] = lid
                update_data["name"] = name
                to_update.append(update_data)

        if not to_update:
            QMessageBox.information(self, self.tr("Aktualizacja"), self.tr("Brak warstw do aktualizacji."))
            return

        # Pokaż dialog potwierdzenia
        dialog = UpdateConfirmationDialog(to_update, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            count = 0
            for item in to_update:
                if self._layer_service.safe_update_layer(
                    item["id"],
                    item["new_model"],
                    self._current_manifest.build_id
                ):
                    count += 1
                    self.log(f"Zaktualizowano: {item['name']}")

            self._notify(f"{self.tr('Zaktualizowano')} {plural_layers(count)}", "success")
            self.log(f"Zaktualizowano warstw: {count}")
            self._refresh_layer_states()

    # --- Informacja o planie ---------------------------------------------

    def _canvas(self):
        """Mapa QGIS albo ``None`` (np. w testach bez interfejsu)."""
        return self._iface.mapCanvas() if hasattr(self._iface, "mapCanvas") else None

    def deactivate_tools(self) -> None:
        """Wyłącza narzędzia mapy wtyczki (przy rozładowaniu wtyczki)."""
        self._eziudp_tab.deactivate_tools()
        self._opendata_tab.deactivate_tools()
        self._opendata_service.cancel()
        self._app_tab.shutdown()
        self._context_bar.deactivate_tools()

    def show_plan_info(self, hits: list) -> None:
        """Akty z miejsca klikniętego narzędziem „Informacja o planie” (pasek narzędzi wtyczki)."""
        self._on_acts_found(hits)

    def _on_acts_found(self, hits: list) -> None:
        """Pokazuje akty z klikniętego miejsca i pobiera ich dokumenty."""
        self.log(f"Informacja o planie: {', '.join(h.act_id or f'{h.typ} z pliku' for h in hits) or 'brak planu w tym miejscu'}")
        self._show_acts(hits)

    def _show_acts(self, hits: list) -> None:
        """Otwiera okno „Informacja o planie” z aktami i pobiera ich pliki JSON."""
        # Akty z plików APP (GPKG, GML) nie mają identyfikatora z Rejestru — dopasowujemy je
        # do indeksu po typie i TERYT, żeby okno pokazało dokumenty i link do Rejestru.
        hits = resolve_local_hits(list(hits), self._act_entries)
        if self._plan_info_dialog is None:
            self._plan_info_dialog = PlanInfoDialog(self._iface, self, config=self._config)
        self._plan_info_dialog.show_acts(hits)
        self._plan_info_dialog.show()
        self._plan_info_dialog.raise_()

        template = self._current_manifest.acts_url_template if self._current_manifest else None
        for hit in hits:
            if hit.act_id:
                self._act_service.fetch(hit.act_id, template)

    def _on_act_details_ready(self, act_id: str, details) -> None:
        if self._plan_info_dialog is not None:
            self._plan_info_dialog.set_details(act_id, details)
        if act_id in self._pending_act_layers:
            self._pending_act_layers.discard(act_id)
            self._add_act_gpkg(details)

    def _on_act_details_error(self, act_id: str, message: str) -> None:
        self.log(f"Dokumenty aktu {act_id}: {message}", "UWAGA")
        if self._plan_info_dialog is not None:
            self._plan_info_dialog.set_error(act_id, message)
        if act_id in self._pending_act_layers:
            self._pending_act_layers.discard(act_id)
            self._notify(self.tr("Nie udało się pobrać danych aktu {0} ({1}). Spróbuj ponownie po „Odśwież indeks”.")
                         .format(act_id, message), "warning")

    # --- Warstwy planu (plik GPKG aktu) ---------------------------------------

    def add_act_layers(self, entry: ActIndexEntry) -> None:
        """
        „Dodaj warstwy planu”: GPKG aktu z bucketa (pole ``dane.gpkg`` pliku ``akty/{act_id}.json``),
        pobierany do bufora wtyczki i otwierany z dysku — ta sama droga co „Granica planu →
        dodaj do mapy” w oknie informacji.
        """
        if entry.act_id in acts_on_map():
            return
        self._pending_act_layers.add(entry.act_id)
        template = self._current_manifest.acts_url_template if self._current_manifest else None
        self._act_service.fetch(entry.act_id, template)

    def _add_act_gpkg(self, details) -> None:
        """
        Pobiera plik planu do folderu zapisu; warstwy dodaje :meth:`_on_act_file_ready`.

        Nie otwieramy pliku wprost z bucketa przez ``/vsicurl/``: QGIS dekoduje
        kodowanie procentowe w ścieżce, a libcurl odrzuca spację, więc akty z gmin
        o nazwie ze spacją (np. ``066101_Biała Podlaska``) w ogóle się nie otwierały
        (``querySublayers()`` → 0 warstw).
        """
        url = details.dane.get("gpkg")
        if not is_bucket_url(url):
            self._notify(self.tr("Akt {0} nie ma opublikowanego pliku GPKG — warstw planu nie da się dodać.")
                         .format(details.act_id), "warning")
            return
        folder = act_files.download_folder(self._config, self)
        if not folder:
            self.log(f"Pominięto dodanie warstw planu {details.act_id}: nie wybrano folderu zapisu", "UWAGA")
            return
        title = short_title(details.tytul) or details.act_id
        name = f"{details.typ.upper()} — {title}"
        self._pending_act_files[url] = (name, details.act_id)
        self.log(f"Pobieranie pliku planu {details.act_id}: {url}")
        self._act_downloader.download(url, str(act_files.act_folder(folder, details.act_id)))

    def _on_act_file_ready(self, url: str, path: str) -> None:
        """Pobrany plik planu: dodaje jego warstwy do mapy."""
        pending = self._pending_act_files.pop(url, None)
        if pending is None:
            return
        name, act_id = pending
        if add_vector_layers(path, name, include_tables=False, act_id=act_id) is None:
            self.log(f"Nie udało się otworzyć {path}", "BŁĄD")
            self._notify(self.tr("Nie udało się otworzyć warstw planu {0} — plik: {1}").format(act_id, path),
                         "warning")
            return
        self.log(f"Dodano warstwy planu {act_id}: {path}")
        self._notify(self.tr("Dodano warstwy planu: {0}").format(name), "success")
        self._search_panel.set_on_map(acts_on_map())

    def _on_act_file_failed(self, url: str, message: str) -> None:
        """Nieudane pobranie pliku planu — komunikat zamiast ciszy."""
        pending = self._pending_act_files.pop(url, None)
        if pending is None:
            return
        _name, act_id = pending
        self.log(f"Nie udało się pobrać pliku planu {act_id}: {message}", "BŁĄD")
        self._notify(self.tr("Nie udało się pobrać danych aktu {0} ({1}).").format(act_id, message),
                     "warning")

    def remove_act_layers(self, entry: ActIndexEntry) -> int:
        """„Usuń warstwy planu z mapy” — pojedyncza, łatwa do cofnięcia akcja (bez potwierdzenia)."""
        removed = remove_act_layers(entry.act_id)
        if removed:
            self.log(f"Usunięto warstwy planu {entry.act_id}: {removed}")
            self._notify(f"{self.tr('Usunięto z mapy')} {plural_layers(removed)}", "success")
        self._search_panel.set_on_map(acts_on_map())
        return removed

    # --- Wyszukiwanie aktów ----------------------------------------------

    def _on_act_index_ready(self, entries: list) -> None:
        self.log(f"Indeks aktów: {len(entries)} aktów")
        self._act_entries = list(entries)
        self._search_panel.set_entries(entries)
        self._app_tab.set_act_index(entries)
        self._update_index_state()

    def _on_act_index_error(self, message: str) -> None:
        self.log(f"Indeks aktów: {message}", "UWAGA")
        self._search_panel.set_unavailable(message)
        self._app_tab.set_acts_unavailable(message)

    def _on_search_filters_changed(self, *args) -> None:
        """Zapamiętuje filtry wyszukiwarki między sesjami."""
        self._config.search_types = self._search_panel.selected_types()
        self._config.search_status = self._search_panel.selected_status()

    def _on_search_details(self, entry: ActIndexEntry) -> None:
        """Okno „Informacja o planie” dla aktu z wyników wyszukiwania."""
        self.log(f"Wyszukiwanie: szczegóły aktu {entry.act_id}")
        self._show_acts([ActHit(act_id=entry.act_id, typ=entry.typ, teryt=entry.terc)])

    def _on_search_validate(self, entry: ActIndexEntry) -> None:
        """„Waliduj” z menu aktu — plik GML Rejestru do okna walidacji APP."""
        self.log(f"Wyszukiwanie: walidacja aktu {entry.act_id}")
        self._app_tab.validate_act(entry)

    def _on_search_show_on_map(self, entry: ActIndexEntry) -> None:
        """Przybliża mapę do aktu z wyników; dokłada warstwy jego typu, jeśli ich nie ma."""
        if self.zoom_to_act(entry):
            self.log(f"Wyszukiwanie: przybliżono do {entry.act_id} ({entry.gmina})")

    def _ensure_type_layers(self, layer_type: str) -> int:
        """
        Dodaje do projektu wszystkie podwarstwy typu, jeśli nie ma jeszcze żadnej.

        :param layer_type: kod typu (``POG``).
        :return: liczba dodanych warstw.
        """
        if not self._current_manifest:
            return 0
        if any(t == layer_type for t, _ in self._layer_service.added_sublayers()):
            return 0
        count = 0
        for ds in self._current_manifest.datasets:
            if ds.layer_type != layer_type:
                continue
            for name in self._sublayers_of(ds):
                if self._layer_service.add_sublayer(ds, name, self._current_manifest.build_id):
                    count += 1
                else:
                    self.log(f"Nie udało się dodać: {ds.layer_type} - {name}", "BŁĄD")
        if count:
            self.log(f"Dodano warstwy {layer_type} do pokazania aktu: {count}")
            self._refresh_layer_states()
        return count

    def zoom_to_act(self, entry: ActIndexEntry) -> bool:
        """
        Przybliża mapę do zasięgu aktu (bbox WGS84 z indeksu).

        :return: ``True``, gdy mapa została przesunięta.
        """
        canvas = self._canvas()
        if canvas is None or entry.bbox is None:
            return False
        self._ensure_type_layers(entry.typ.upper())
        rect = QgsRectangle(*entry.bbox)
        transform = QgsCoordinateTransform(
            QgsCoordinateReferenceSystem("EPSG:4326"), canvas.mapSettings().destinationCrs(), QgsProject.instance()
        )
        try:
            rect = transform.transformBoundingBox(rect)
        except QgsCsException as e:
            self.log(f"Nie udało się przeliczyć zasięgu aktu {entry.act_id}: {e}", "BŁĄD")
            return False
        rect.scale(1.15)
        canvas.setExtent(rect)
        canvas.refresh()
        return True

    def tr(self, text: str) -> str:
        """Przetłumacz tekst."""
        return QCoreApplication.translate("GSMapaDock", text)

    def dock_area(self):
        """Obszar dokowania."""
        return Qt.DockWidgetArea.RightDockWidgetArea
