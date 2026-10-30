"""Narzędzie „Informacja o planie”: klik na mapie -> akt, link do rejestru, dokumenty."""

import html
import logging
import urllib.parse
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import QEvent, Qt, QUrl, QUrlQuery, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices, QPalette
from qgis.PyQt.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)
from qgis.core import (
    Qgis,
    QgsBilinearRasterResampler,
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsGeometry,
    QgsProject,
    QgsProviderRegistry,
    QgsProviderSublayerDetails,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.gui import QgsMapTool, QgsMapToolEmitPoint

from ..core.colors import box_color, muted_color
from ..core.act_index import ACT_STATUSES, normalize_text, short_title
from ..core.app_namespace import parse_namespace
from ..core.app_codes import coded_values
from ..core.app_gml import ZONE
from ..core.app_writer import STATUSES
from ..core.config import GSMapaConfig
from ..core.labels import format_date, plural_acts, status_role, type_description, type_role
from ..core.models import ActDetails, ActDocument
from ..services.download import FileDownloader
from ..services import act_files
from ..services.gdal_env import ensure_vsicurl_extensions
from ..services.acts import parse_act_id, registry_url
from ..services.layers import LayerService
from ..services.manifest_parser import ManifestParser
from ..services.app_style import apply_app_style
from ..services.app_crs import assign_file_crs, enforce_file_crs
from ..services.app_open_acts import ACT_PROPERTY as _ACT_PROPERTY
from . import theme
from ..services.pog_style import apply_pog_style

logger = logging.getLogger("GSMapa.PlanInfo")

#: Schemat odnośników wewnętrznych okna („Dodaj do mapy”).
_ADD_SCHEME = "gsmapa"

#: Kolejność i nagłówki grup dokumentów.
_DOCUMENT_GROUPS = [
    (("akt", "dokument"), "Uchwała i dokumenty"),
    (("rysunek",), "Rysunki"),
    (("legenda",), "Legendy"),
    (("mapa_podkladowa",), "Mapy podkładowe"),
]

#: Rodzaj odnośnika metadanego aktu: referencja mapy podkładowej (strona geoportalu/BIP).
#: Nie jest dokumentem, więc nie ma kopii w buckecie i nie pokazujemy przy niej „brak kopii”.
_KIND_MAPA_PODKLADOWA = "mapa_podkladowa"

#: Nazwy dokumentów bez nazwy pliku (brak kopii w buckecie).
_DOCUMENT_NAMES = {"akt": "Akt", "dokument": "Dokument", "rysunek": "Rysunek", "legenda": "Legenda",
                   _KIND_MAPA_PODKLADOWA: "Mapa podkładowa"}


def _badge(text: str, color: str) -> str:
    """Oznaczenie typu / statusu: pogrubiony tekst w kolorze semantycznym (kolor z ``theme`` — zawsze ze słowem)."""
    return f'<span style="color:{color}"><b>{html.escape(text)}</b></span>'


#: Atrybuty obiektu pokazywane przy kliknięciu (np. strefa POG).
_OBJECT_LABEL_FIELDS = ("oznaczenie", "nazwa")

#: Właściwość warstwy z plikiem aktu, z którego warstwa powstała
#: (``services/app_files.SOURCE_PROPERTY`` — pliki GPKG/GML dodane z dysku).
FILE_PROPERTY = "gs_mapa/app_source"

#: Pole APP z przestrzenią nazw ``idIIP`` obiektu (``PL.ZIPPZP.<nr>/<JPT>-<rodzaj>``).
NAMESPACE_FIELD = "przestrzenNazw"

#: Rodzaj zbioru APP (człon przestrzeni nazw) → typ aktu (``POG`` → ``pog``).
NAMESPACE_TYPES = {"POG": "pog", "MPZP": "mpzp", "SUIKZP": "suikzp", "PZPW": "pzpw", "UK": "uk"}


@dataclass
class ActHit:
    """Akt znaleziony w klikniętym miejscu."""
    #: Identyfikator aktu (``pog-342``); pusty dla pliku bez wpisu w indeksie aktów.
    act_id: str
    typ: str
    teryt: str = ""
    #: Opisy klikniętych obiektów aktu, np. ``12SN — strefa zieleni i rekreacji``.
    obiekty: List[str] = field(default_factory=list)
    #: Tytuł z pliku aktu (tabela ``AktPlanowaniaPrzestrzennego``) — gdy nie ma pliku JSON aktu.
    tytul: str = ""
    #: Status z pliku aktu jako etykieta (``prawnie wiążący lub realizowany``).
    status: str = ""
    #: Początek obowiązywania z pliku aktu (data ISO).
    obowiazuje_od: Optional[str] = None

    @property
    def z_pliku(self) -> bool:
        """Akt pochodzi z pliku dodanego do mapy, a nie z warstw Rejestru (kafle, GPKG aktu)."""
        return not self.act_id


def _text_value(value) -> str:
    """Wartość atrybutu jako tekst; ``QDateTime`` (pole datetime GPKG) jako data ISO."""
    if value is None:
        return ""
    if hasattr(value, "toString"):  # QDate / QDateTime
        return value.toString(Qt.DateFormat.ISODate)[:10]
    return str(value).strip()


def _status_label(value: str) -> str:
    """Status aktu z pliku APP jako etykieta (``…/legalForce`` → ``prawnie wiążący lub realizowany``)."""
    code = value.rsplit("/", 1)[-1].strip()
    return STATUSES.get(code, value)


@lru_cache(maxsize=1)
def _zone_labels() -> Dict[str, str]:
    """Rodzaj strefy planistycznej (kod listy) → etykieta; pusty słownik, gdy list kodowych nie ma."""
    try:
        return dict(coded_values()[ZONE]["nazwa"])
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _object_label(feature, names) -> str:
    """
    Opis klikniętego obiektu, np. ``12SN — strefa zieleni i rekreacji``.

    Rodzaj strefy z pliku APP jest kodem listy (``strefaZieleniIRekreacji``) — podmieniamy go
    na etykietę, tak jak w kaflach Rejestru.
    """
    parts = []
    for name in _OBJECT_LABEL_FIELDS:
        if name not in names or feature[name] in (None, ""):
            continue
        value = str(feature[name])
        parts.append(_zone_labels().get(value, value) if name == "nazwa" else value)
    return " — ".join(parts)


def _namespace_identity(feature, names) -> Optional[tuple]:
    """
    Typ aktu i kod TERYT z przestrzeni nazw ``idIIP`` obiektu pliku APP.

    :return: ``(typ, TERYT)`` albo ``None``, gdy obiekt nie ma rozpoznawalnej przestrzeni nazw.
    """
    if NAMESPACE_FIELD not in names:
        return None
    parts = parse_namespace(_text_value(feature[NAMESPACE_FIELD]))
    if parts is None:
        return None
    typ = NAMESPACE_TYPES.get(parts[2].upper())
    return (typ, parts[1]) if typ else None


def find_acts(layers, map_rect: QgsRectangle, map_crs) -> List[ActHit]:
    """
    Zbiera akty z obiektów warstw wtyczki przecinających prostokąt wyszukiwania.

    Akt obiektu wynika z (w tej kolejności): atrybutu ``act_id`` (kafle Rejestru),
    właściwości warstwy ``gsmapa/idIIP`` (plan dodany z Rejestru) albo przestrzeni nazw
    ``idIIP`` obiektu (pliki APP dodane z dysku — dają typ i TERYT, ale nie identyfikator
    aktu z Rejestru, dlatego ``act_id`` zostaje pusty do czasu dopasowania w indeksie).

    :param layers: warstwy wektorowe do przeszukania.
    :param map_rect: prostokąt wyszukiwania w układzie mapy.
    :param map_crs: układ współrzędnych mapy.
    :return: akty w kolejności znalezienia, bez powtórzeń.
    """
    hits: Dict[str, ActHit] = {}
    for layer in layers:
        if not isinstance(layer, QgsVectorLayer):
            continue
        names = layer.fields().names()
        fixed_act = str(layer.customProperty(ACT_PROPERTY) or "").strip().lower()
        has_act = "act_id" in names
        if not (fixed_act or has_act or NAMESPACE_FIELD in names):
            continue
        transform = QgsCoordinateTransform(map_crs, layer.crs(), QgsProject.instance())
        rect = transform.transformBoundingBox(map_rect)
        area = QgsGeometry.fromRect(rect)
        request = QgsFeatureRequest().setFilterRect(rect).setLimit(200)
        for feature in layer.getFeatures(request):
            if feature.hasGeometry() and not feature.geometry().intersects(area):
                continue
            act_id = (_text_value(feature["act_id"]) if has_act else "").lower() or fixed_act
            local = None if act_id else _namespace_identity(feature, names)
            if act_id:
                parsed = parse_act_id(act_id)
                if parsed is None:
                    continue
                key, typ = act_id, parsed[0]
                teryt = _text_value(feature["teryt"]) if "teryt" in names else ""
            elif local:
                typ, teryt = local
                key = f"{typ}:{teryt}"
            else:
                continue
            hit = hits.get(key)
            if hit is None:
                hit = hits[key] = ActHit(act_id=act_id, typ=typ, teryt=teryt)
            # Tabela aktu pliku APP ma tytuł, status i datę obowiązywania obiektu.
            if not hit.tytul and "tytul" in names:
                hit.tytul = _text_value(feature["tytul"])
            if not hit.status:
                # W plikach z Rejestru status jest odnośnikiem do listy kodowej (``status_href``).
                source = next((name for name in ("status", "status_href") if name in names), "")
                if source:
                    hit.status = _status_label(_text_value(feature[source]))
            if not hit.obowiazuje_od and "obowiazujeOd" in names:
                hit.obowiazuje_od = _text_value(feature["obowiazujeOd"]) or None
            label = _object_label(feature, names)
            if label and label not in hit.obiekty:
                hit.obiekty.append(label)
    return list(hits.values())


def _most_current(candidates: list) -> list:
    """Kandydaci o najlepszym statusie: obowiązujący przed przyjmowanym, opracowywanym i nieaktualnym."""
    order = [value for value, _label in ACT_STATUSES]

    def rank(entry) -> int:
        status = (entry.status or "").strip().lower()
        return order.index(status) if status in order else len(order)

    best = min(rank(entry) for entry in candidates)
    return [entry for entry in candidates if rank(entry) == best]


def resolve_local_hit(hit: ActHit, entries) -> ActHit:
    """
    Dopasowuje akt z pliku do wpisu indeksu aktów — po typie i kodzie TERYT.

    Plan ogólny jest jeden na gminę, więc para (typ, TERYT) wystarcza; przy kilku aktach
    tego samego typu w gminie (plany miejscowe) rozstrzyga tytuł, a potem status
    (obowiązujący przed projektem i aktem nieaktualnym). Bez jednoznacznego dopasowania
    akt zostaje bez ``act_id`` (okno pokazuje tylko dane z pliku).

    :param hit: akt znaleziony w warstwie pliku.
    :param entries: wpisy indeksu aktów (:class:`~gs_mapa.core.act_index.ActIndexEntry`).
    :return: akt z identyfikatorem z Rejestru albo ten sam akt bez zmian.
    """
    if hit.act_id or not hit.teryt:
        return hit
    candidates = [entry for entry in entries if entry.typ == hit.typ and entry.terc == hit.teryt]
    if len(candidates) > 1 and hit.tytul:
        title = normalize_text(short_title(hit.tytul))
        candidates = [entry for entry in candidates
                      if normalize_text(short_title(entry.tytul)) == title] or candidates
    if len(candidates) > 1:
        candidates = _most_current(candidates)
    if len(candidates) != 1:
        return hit
    return replace(hit, act_id=candidates[0].act_id)


def resolve_local_hits(hits: List[ActHit], entries) -> List[ActHit]:
    """Uzupełnia o identyfikator z indeksu akty pochodzące z plików (patrz :func:`resolve_local_hit`)."""
    return [resolve_local_hit(hit, entries) if hit.z_pliku else hit for hit in hits]


def _node_visible(node) -> bool:
    """Czy węzeł drzewa warstw jest widoczny razem ze swoimi grupami nadrzędnymi."""
    while node is not None:
        if not node.isVisible():
            return False
        node = node.parent()
    return True


def searchable_layers(project: Optional[QgsProject] = None) -> list:
    """
    Widoczne warstwy dodane przez wtyczkę, które mogą zawierać akty.

    Trzy drogi dodania: kafle Rejestru (``gs_mapa_plugin_id``), warstwy planu z Rejestru
    (``gsmapa/idIIP``) i pliki aktów GPKG/GML z dysku (``gs_mapa/app_source``).
    """
    target = project if project is not None else QgsProject.instance()
    if target is None:
        return []
    root = target.layerTreeRoot()
    found = []
    for layer in target.mapLayers().values():
        added = (
            layer.customProperty(f"{LayerService.PLUGIN_ID}_id") == LayerService.PLUGIN_ID
            or bool(layer.customProperty(ACT_PROPERTY))
            or bool(layer.customProperty(FILE_PROPERTY))
        )
        if not added:
            continue
        node = root.findLayer(layer.id())
        if node is not None and _node_visible(node):
            found.append(layer)
    return found


class PlanInfoMapTool(QgsMapToolEmitPoint):
    """Narzędzie mapy: kliknięcie wyszukuje akty w warstwach dodanych przez wtyczkę."""

    acts_found = pyqtSignal(list)  # list[ActHit]

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvasClicked.connect(self._on_clicked)

    def _plugin_layers(self):
        """Widoczne warstwy dodane przez wtyczkę."""
        return searchable_layers()

    def _on_clicked(self, point, button) -> None:
        radius = QgsMapTool.searchRadiusMU(self.canvas())
        rect = QgsRectangle(point.x() - radius, point.y() - radius, point.x() + radius, point.y() + radius)
        crs = self.canvas().mapSettings().destinationCrs()
        self.acts_found.emit(find_acts(self._plugin_layers(), rect, crs))


def is_bucket_url(url: Optional[str]) -> bool:
    """Do mapy dodajemy tylko pliki z bucketa wtyczki."""
    if not url:
        return False
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == ManifestParser.ALLOWED_HOST


def _is_geotiff(doc: ActDocument) -> bool:
    name = (doc.plik or doc.url_s3 or "").lower()
    return doc.content_type == "image/tiff" or name.endswith((".tif", ".tiff"))


def apply_drawing_resampling(layer: QgsRasterLayer) -> None:
    """
    Resampling biliniowy dla rysunku GeoTIFF — niezależnie dla zoomu w obie strony.

    Bez tego QGIS rysuje skan metodą nearest neighbour („pikseloza”) przy
    powiększeniu i gubi detale przy pomniejszeniu.
    """
    filt = layer.resampleFilter()
    filt.setZoomedInResampler(QgsBilinearRasterResampler())
    filt.setZoomedOutResampler(QgsBilinearRasterResampler())
    layer.setResamplingStage(Qgis.RasterResamplingStage.ResampleFilter)


def _format_size(size: Optional[int]) -> str:
    if not size:
        return ""
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{max(1, size // 1024)} kB"


def _add_link(url: str, name: str, kind: str, **extra) -> str:
    """Odnośnik wewnętrzny „Dodaj do mapy” / „Pobierz i dodaj”."""
    params = {"url": url, "name": name, "kind": kind}
    params.update({key: str(value) for key, value in extra.items() if value not in (None, "")})
    return f"{_ADD_SCHEME}://add?{urllib.parse.urlencode(params)}"


def _download_link(url: str, name: str, layer: str, act_id: str, size: Optional[int] = None) -> str:
    """Odnośnik „Pobierz i dodaj do projektu” (``layer``: ``vector`` albo ``raster``)."""
    return _add_link(url, name, "download", layer=layer, act=act_id, size=size)


def _link(href: str, text: str) -> str:
    return f'<a href="{html.escape(href, quote=True)}">{html.escape(text)}</a>'


#: Właściwość warstwy z identyfikatorem aktu, którego dane pokazuje (stan „na mapie” na karcie Plany).
#: (``services/app_open_acts.py`` — jedno źródło prawdy dla obu miejsc.)
ACT_PROPERTY = _ACT_PROPERTY


def add_vector_layers(source: str, name: str, include_tables: bool, act_id: str = ""):
    """
    Dodaje do projektu warstwy pliku wektorowego aktu w jednej grupie.

    GPKG/GML aktu ma kilka tabel: geometrię aktu oraz tabele bez geometrii
    (``DokumentFormalny``, ``RysunekAktuPlanowaniaPrzestrzennego``...).

    :param source: źródło OGR (ścieżka lub ``/vsicurl/...``).
    :param name: nazwa grupy.
    :param include_tables: czy dodać też tabele bez geometrii.
    :param act_id: identyfikator aktu — zapisywany we właściwości ``gsmapa/idIIP`` każdej warstwy.
    :return: pierwsza dodana warstwa albo ``None``.
    """
    options = QgsProviderSublayerDetails.LayerOptions(QgsProject.instance().transformContext())
    layers = []
    ogr = QgsProviderRegistry.instance().providerMetadata("ogr")
    for details in ogr.querySublayers(source):
        if details.type() != Qgis.LayerType.Vector:
            continue
        if not include_tables and details.wkbType() == Qgis.WkbType.NoGeometry:
            continue
        layer = details.toLayer(options)
        if layer is not None and layer.isValid():
            # Styl tabel APP (QML z oficjalnej wtyczki APP) albo stylizacja POG
            # z rozporządzenia (jeśli nazwa warstwy pasuje do podwarstwy kafli).
            if not apply_app_style(layer, details.name()):
                apply_pog_style(layer, details.name())
            if act_id:
                layer.setCustomProperty(ACT_PROPERTY, act_id)
            layers.append(layer)
    if not layers:
        return None
    # Tabele bez geometrii nie dostają układu z pliku GPKG (srs_id = 0) — bez tego
    # QGIS pyta użytkownika, w jakim układzie są dane.
    assign_file_crs(layers)
    # Układ inny niż w pliku (np. układ projektu) przestawia dane na mapie bez
    # komunikatu — po nadaniu brakujących układów plik jest źródłem prawdy.
    enforce_file_crs(layers, source.split("|")[0])
    group = QgsProject.instance().layerTreeRoot().insertGroup(0, name)
    for layer in layers:
        QgsProject.instance().addMapLayer(layer, False)
        group.addLayer(layer)
    return layers[0]


def act_layers(act_id: str) -> list:
    """Warstwy projektu z danymi aktu (właściwość ``gsmapa/idIIP``)."""
    return [layer for layer in QgsProject.instance().mapLayers().values()
            if layer.customProperty(ACT_PROPERTY) == act_id]


def acts_on_map() -> set:
    """Identyfikatory aktów, których warstwy są w projekcie."""
    return {layer.customProperty(ACT_PROPERTY) for layer in QgsProject.instance().mapLayers().values()
            if layer.customProperty(ACT_PROPERTY)}


def remove_act_layers(act_id: str) -> int:
    """Usuwa z projektu warstwy aktu i puste po nich grupy; zwraca liczbę usuniętych warstw."""
    project = QgsProject.instance()
    layers = act_layers(act_id)
    root = project.layerTreeRoot()
    parents = set()
    for layer in layers:
        node = root.findLayer(layer.id())
        if node is not None and node.parent() is not None and node.parent() is not root:
            parents.add(node.parent())
    project.removeMapLayers([layer.id() for layer in layers])
    for group in parents:
        if not group.children() and group.parent() is not None:
            group.parent().removeChildNode(group)
    return len(layers)


class PlanInfoDialog(QDialog):
    """Okno z aktami w klikniętym miejscu: dane aktu, link do rejestru, dokumenty."""

    def __init__(self, iface, parent=None, config: GSMapaConfig | None = None,
                 downloader: FileDownloader | None = None):
        super().__init__(parent)
        # Własny wygląd wtyczki (makiety docs/ui), niezależny od stylu Qt w QGIS.
        theme.apply_plugin_style(self)
        self._iface = iface
        self._config = config if config is not None else GSMapaConfig()
        self._downloader = downloader if downloader is not None else FileDownloader()
        self._downloader.finished.connect(self._on_download_finished)
        self._downloader.failed.connect(self._on_download_failed)
        self._downloader.progress.connect(self._on_download_progress)
        #: Pobierane pliki: adres -> (nazwa warstwy/grupy, ``vector`` albo ``raster``).
        self._pending_downloads: Dict[str, tuple] = {}
        self.setWindowTitle(self.tr("Informacja o planie"))
        self.setMinimumSize(380, 320)
        geometry = self._config.plan_info_geometry
        if geometry:
            self.restoreGeometry(geometry)
        else:
            self.resize(500, 640)

        layout = QVBoxLayout(self)
        self._browser = QTextBrowser()
        self._browser.setOpenLinks(False)
        self._browser.anchorClicked.connect(self._on_anchor_clicked)
        layout.addWidget(self._browser)

        self._download_label = QLabel()
        theme.style_muted(self._download_label)
        self._download_label.setVisible(False)
        layout.addWidget(self._download_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close_button = QPushButton(self.tr("Zamknij"))
        close_button.clicked.connect(self.close)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self._hits: List[ActHit] = []
        self._details: Dict[str, ActDetails] = {}
        self._errors: Dict[str, str] = {}

    # --- stan -------------------------------------------------------------

    def show_acts(self, hits: List[ActHit]) -> None:
        """Pokazuje akty z nowego kliknięcia; dokumenty dochodzą przez ``set_details``."""
        self._hits = list(hits)
        self._details.clear()
        self._errors.clear()
        self._render()

    def set_details(self, act_id: str, details: ActDetails) -> None:
        """Uzupełnia akt o dane z pliku JSON."""
        if any(hit.act_id == act_id for hit in self._hits):
            self._details[act_id] = details
            self._render()

    def set_error(self, act_id: str, message: str) -> None:
        """Zapisuje, dlaczego lista dokumentów aktu jest niedostępna."""
        if any(hit.act_id == act_id for hit in self._hits):
            self._errors[act_id] = message
            self._render()

    def html(self) -> str:
        """Aktualna treść okna (HTML)."""
        return self._browser.toHtml()

    # --- renderowanie -----------------------------------------------------

    def _render(self) -> None:
        if not self._hits:
            self.setWindowTitle(self.tr("Informacja o planie"))
            self._browser.setHtml(
                f"<p><b>{self.tr('W tym miejscu nie ma planu.')}</b></p>"
                f"<p style='color:{self._muted}'>{self.tr('Narzędzie przeszukuje widoczne warstwy dodane przez wtyczkę — kafle Rejestru, warstwy planów i pliki APP (GPKG, GML). Dodaj warstwy i kliknij w obszarze planu.')}</p>"
            )
            return
        count = len(self._hits)
        self.setWindowTitle(self.tr("Informacja o planie — {0}").format(plural_acts(count))
                            if count > 1 else self.tr("Informacja o planie"))
        parts = []
        if count > 1:
            jumps = " · ".join(
                f'<a href="#{self._anchor(hit)}">{html.escape(hit.typ.upper())}</a>' for hit in self._hits
            )
            parts.append(f"<p>{self.tr('W tym miejscu {0}: {1}').format(plural_acts(count), jumps)}</p>")
        parts.extend(self._render_act(hit) for hit in self._hits)
        self._browser.setHtml("".join(parts))

    @staticmethod
    def _anchor(hit: ActHit) -> str:
        """Znacznik aktu w oknie (akt z pliku nie ma ``act_id`` — rozróżnia go typ i TERYT)."""
        key = hit.act_id or f"{hit.typ}-{hit.teryt}"
        return f"akt-{key}"

    def _theme_rgb(self) -> tuple:
        """Kolory tekstu i tła pola treści (``Text``, ``Base``) jako ``(r, g, b)``."""
        palette = theme.palette()  # pod arkuszem stylów paleta widżetu to paleta aplikacji
        text = palette.color(QPalette.ColorRole.Text).getRgb()[:3]
        base = palette.color(QPalette.ColorRole.Base).getRgb()[:3]
        return text, base

    @property
    def _muted(self) -> str:
        """Kolor tekstu pomocniczego z palety (działa w jasnym i ciemnym motywie)."""
        return muted_color(*self._theme_rgb())

    @property
    def _box(self) -> str:
        """Tło ramki aktu — z ``Text``/``Base``, nie z ``AlternateBase`` (patrz :func:`box_color`)."""
        return box_color(*self._theme_rgb())

    def changeEvent(self, event) -> None:  # noqa: N802 - nazwa z API Qt
        """Zmiana motywu (jasny/ciemny) w trakcie pracy — kolory HTML liczymy od nowa."""
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange and hasattr(self, "_hits"):
            self._render()

    def _render_act(self, hit: ActHit) -> str:
        details = self._details.get(hit.act_id)
        muted = self._muted
        box = self._box

        badges = [_badge(hit.typ.upper(), theme.semantic(type_role(hit.typ), self._browser).name())]
        status = (details.status if details else "") or hit.status
        if status:
            role = status_role(status)
            color = theme.semantic(role, self._browser).name() if role else self._muted
            badges.append(_badge(status, color))

        title = (details.tytul if details and details.tytul else "") or hit.tytul or type_description(hit.typ)
        title = title[:1].upper() + title[1:]

        meta = []
        if details:
            meta.append(type_description(hit.typ))
            place = ", ".join(x for x in (f"gm. {details.gmina}" if details.gmina else "",
                                          f"pow. {details.powiat}" if details.powiat else "") if x)
            if place:
                meta.append(place)
            if details.obowiazuje_od:
                validity = f"obowiązuje od {format_date(details.obowiazuje_od)}"
                if details.obowiazuje_do:
                    validity += f" do {format_date(details.obowiazuje_do)}"
                meta.append(validity)
        else:
            # Akt bez pliku JSON: to, co jest w pliku dodanym do mapy.
            if hit.tytul:
                meta.append(type_description(hit.typ))
            if hit.teryt:
                meta.append(f"TERYT {hit.teryt}")
            if hit.obowiazuje_od:
                meta.append(f"obowiązuje od {format_date(hit.obowiazuje_od)}")

        head = [f'<a name="{self._anchor(hit)}"></a>',
                f'<table width="100%" cellspacing="0" cellpadding="8" bgcolor="{box}"><tr><td>',
                " · ".join(badges),
                f'<p style="margin-top:6px; margin-bottom:2px; font-size:large"><b>{html.escape(title)}</b></p>']
        if meta:
            head.append(f'<p style="margin-top:0; color:{muted}">{html.escape(" · ".join(meta))}</p>')
        for label in hit.obiekty[:5]:
            head.append(f"<p style='margin-top:0'>{self.tr('Wskazany obiekt: {0}').format(html.escape(label))}</p>")
        link = (details.url_rejestru if details else "") or registry_url(hit.act_id)
        if link:
            head.append(f"<p style='margin-bottom:0'>{_link(link, self.tr('Otwórz w Rejestrze Urbanistycznym ↗'))}</p>")
        head.append("</td></tr></table>")

        if details:
            body = self._render_documents(details)
        elif hit.act_id and hit.act_id in self._errors:
            body = f"<p style='color:{muted}'>{html.escape(self._errors[hit.act_id])}</p>"
        elif hit.act_id:
            body = f"<p style='color:{muted}'>{self.tr('Wczytywanie dokumentów...')}</p>"
        else:
            # Plik aktu bez wpisu w indeksie: dokumenty i link do Rejestru są w Rejestrze.
            body = (f"<p style='color:{muted}'>"
                    f"{self.tr('Akt wczytany z pliku. Dokumenty i link do Rejestru wymagają wpisu w indeksie aktów — odśwież indeks i kliknij ponownie.')}"
                    f"</p>")
        return "".join(head) + body + "<p></p>"

    def _section(self, title: str, rows: List[str]) -> str:
        """Sekcja okna: nagłówek i tabela wierszy (nazwa | akcje)."""
        return (
            f"<p style='margin-top:10px; margin-bottom:2px; color:{self._muted}'>"
            f"<b>{html.escape(title.upper())}</b></p>"
            '<table width="100%" cellspacing="0" cellpadding="3">' + "".join(rows) + "</table>"
        )

    @staticmethod
    def _row(name_html: str, actions: List[str]) -> str:
        return f'<tr><td>{name_html}</td><td align="right" nowrap>{" · ".join(actions)}</td></tr>'

    def _render_documents(self, details: ActDetails) -> str:
        out = []
        for kinds, header in _DOCUMENT_GROUPS:
            docs = [d for d in details.dokumenty if d.rodzaj in kinds]
            if docs:
                rows = [self._render_document(d, details) for d in docs]
                out.append(self._section(self.tr("{0} ({1})").format(self.tr(header), len(docs)), rows))
        others = [d for d in details.dokumenty if all(d.rodzaj not in k for k, _ in _DOCUMENT_GROUPS)]
        if others:
            out.append(self._section(self.tr("Inne ({0})").format(len(others)),
                                     [self._render_document(d, details) for d in others]))

        data_rows = []
        stream = [
            _link(_add_link(url, f"{details.act_id} — granica ({fmt.upper()})", "vector", act=details.act_id),
                  "dodaj do mapy" if fmt == "gpkg" else fmt.upper())
            for fmt, url in sorted(details.dane.items(), key=lambda kv: kv[0] != "gpkg") if is_bucket_url(url)
        ]
        if stream:
            data_rows.append(self._row(self.tr("Granica planu"), stream))
        gpkg = details.dane.get("gpkg")
        if is_bucket_url(gpkg):
            # Pobrany GPKG zawiera też tabele dokumentów i rysunków aktu (bez geometrii).
            data_rows.append(self._row(
                "GPKG z dokumentami",
                [_link(_download_link(gpkg, f"{details.act_id} (GPKG z dokumentami)", "vector", details.act_id),
                       "pobierz i dodaj")],
            ))
        if data_rows:
            out.append(self._section("Dane planu", data_rows))
        if not out:
            out.append(f"<p style='color:{self._muted}'>{self.tr('Akt nie ma dokumentów.')}</p>")
        return "".join(out)

    def _render_document(self, doc: ActDocument, details: ActDetails) -> str:
        muted = self._muted
        has_copy = doc.status == "ok" and doc.url_s3
        if doc.plik:
            name = html.escape(doc.plik)
        else:
            source = urllib.parse.urlparse(doc.url_zrodlowy or "").hostname or ""
            name = html.escape(self.tr(_DOCUMENT_NAMES.get(doc.rodzaj, doc.rodzaj.capitalize())))
            if source:
                name += f" <span style='color:{muted}'>({html.escape(source)})</span>"
        # Tytuł dokumentu pokazujemy tylko, gdy coś mówi (często to po prostu tytuł aktu).
        if doc.tytul and doc.tytul != details.tytul:
            name += f"<br><span style='color:{muted}'>{html.escape(doc.tytul)}</span>"
        extras = [x for x in (_format_size(doc.rozmiar) if has_copy else "", format_date(doc.data)) if x]
        if extras:
            name += f" <span style='color:{muted}'>({html.escape(', '.join(extras))})</span>"

        actions = []
        if has_copy:
            actions.append(_link(doc.url_s3, self.tr("otwórz")))
            # GeoTIFF z kopią w buckecie jest georeferencjonowany niezależnie od rodzaju wpisu
            # (magistraty wpisują rysunki także jako mapy podkładowe) — warto dać „dodaj do mapy”.
            if _is_geotiff(doc) and is_bucket_url(doc.url_s3):
                label = doc.plik or doc.rodzaj
                actions.append(_link(_add_link(doc.url_s3, label, "raster"), self.tr("dodaj do mapy")))
                actions.append(_link(
                    _download_link(doc.url_s3, label, "raster", details.act_id, doc.rozmiar),
                    self.tr("pobierz i dodaj")
                ))
        elif doc.url_zrodlowy:
            actions.append(_link(doc.url_zrodlowy, self.tr("źródło ↗")))
            # Referencja mapy podkładowej to odnośnik do strony źródłowej, a nie dokument,
            # którego kopii brakuje — „brak kopii” byłoby mylące.
            if doc.rodzaj != _KIND_MAPA_PODKLADOWA:
                actions.append(f"<span style='color:{muted}'>{self.tr('brak kopii')}</span>")
        else:
            actions.append(f"<span style='color:{muted}'>{self.tr('niedostępny')}</span>")
        return self._row(name, actions)

    # --- odnośniki --------------------------------------------------------

    def _on_anchor_clicked(self, url: QUrl) -> None:
        if url.scheme() == _ADD_SCHEME:
            query = QUrlQuery(url)
            file_url = query.queryItemValue("url", QUrl.ComponentFormattingOption.FullyDecoded)
            name = query.queryItemValue("name", QUrl.ComponentFormattingOption.FullyDecoded)
            kind = query.queryItemValue("kind")
            if kind == "download":
                size = query.queryItemValue("size")
                self.download_and_add(
                    file_url, name,
                    layer_type=query.queryItemValue("layer") or "vector",
                    act_id=query.queryItemValue("act"),
                    expected_size=int(size) if size.isdigit() else None,
                )
            else:
                self.add_to_map(file_url, name, kind, act_id=query.queryItemValue("act"))
        elif url.scheme() in ("http", "https"):
            QDesktopServices.openUrl(url)
        elif not url.scheme() and url.fragment():
            self._browser.scrollToAnchor(url.fragment())

    # --- okno -------------------------------------------------------------

    def hideEvent(self, event) -> None:  # noqa: N802 - nazwa z API Qt
        """Zapamiętuje położenie i rozmiar okna."""
        self._config.plan_info_geometry = self.saveGeometry()
        super().hideEvent(event)

    def add_to_map(self, url: str, name: str, kind: str, act_id: str = ""):
        """
        Dodaje plik z bucketa do mapy: rysunek (``raster``) przez ``/vsicurl/``,
        plik planu (``vector``) z folderu zapisu (pobrany na dysk).

        Rysunek czytamy strumieniowo (GeoTIFF obsługuje zakresy bajtów). Plik
        wektorowy pobieramy w całości: QGIS dekoduje kodowanie procentowe w ścieżce
        ``/vsicurl/``, a libcurl odrzuca spację, więc akty z gmin o nazwie ze spacją
        (np. ``066101_Biała Podlaska``) nie otwierały się wprost z bucketa.

        :param url: adres pliku w buckecie wtyczki.
        :param name: nazwa warstwy.
        :param kind: ``raster`` albo ``vector``.
        :return: dodana warstwa (raster) albo ``None`` — wektor dochodzi po pobraniu.
        """
        if not is_bucket_url(url):
            logger.warning(f"Pominięto dodanie pliku spoza bucketa: {url}")
            return None
        if kind == "raster":
            ensure_vsicurl_extensions()
            layer = QgsRasterLayer(f"/vsicurl/{url}", name, "gdal")
            if not layer.isValid():
                return self._open_failed(url, name)
            apply_drawing_resampling(layer)
            QgsProject.instance().addMapLayer(layer)
            return layer

        # Plik planu: folder zapisu, pobranie i otwarcie z dysku
        # (warstwy dodaje _on_download_finished).
        folder = self._choose_download_dir()
        if not folder:
            logger.info(f"Pominięto dodanie pliku {url}: nie wybrano folderu zapisu")
            return None
        self._pending_downloads[url] = (name, "vector", act_id)
        self._push("info", self.tr("Pobieranie {0}...").format(name))
        self._downloader.download(url, str(act_files.act_folder(folder, act_id)))
        return None

    def _add_vector_layers(self, source: str, name: str, include_tables: bool, act_id: str = ""):
        """Dodaje warstwy pliku wektorowego aktu (patrz :func:`add_vector_layers`)."""
        return add_vector_layers(source, name, include_tables, act_id)

    def _choose_download_dir(self, ask_always: bool = False) -> str:
        """
        Folder zapisu plików aktów: zapamiętany w ustawieniach albo wybrany teraz.

        :param ask_always: pytaj o folder nawet wtedy, gdy jest zapamiętany
            (akcja „pobierz i dodaj” — użytkownik może chcieć innego miejsca).
        :returns: ścieżka folderu; pusty łańcuch, gdy anulowano wybór.
        """
        return act_files.download_folder(self._config, self, ask_always=ask_always)

    def download_and_add(self, url: str, name: str, layer_type: str = "vector",
                         act_id: str = "", expected_size: Optional[int] = None) -> bool:
        """
        Pobiera plik aktu do wybranego folderu i dodaje go do projektu.

        Pliki trafiają do podfolderu aktu (``<folder>/<act_id>/``) — nazwy rysunków
        powtarzają się między aktami (np. ``rys_ZALACZNIK_GRAFICZNY.tif``).

        :param url: adres pliku w buckecie wtyczki.
        :param name: nazwa warstwy (raster) albo grupy (wektor) w projekcie.
        :param layer_type: ``vector`` — GPKG ze wszystkimi tabelami, ``raster`` — rysunek.
        :param act_id: identyfikator aktu — nazwa podfolderu.
        :param expected_size: rozmiar pliku z katalogu dokumentów; plik o innym rozmiarze
            na dysku jest pobierany ponownie.
        :return: ``True``, gdy pobieranie się rozpoczęło.
        """
        if not is_bucket_url(url):
            logger.warning(f"Pominięto pobranie pliku spoza bucketa: {url}")
            return False
        if act_id and parse_act_id(act_id) is None:
            logger.warning(f"Niepoprawny act_id: {act_id!r}")
            return False
        folder = self._choose_download_dir(ask_always=True)
        if not folder:
            return False
        target_dir = str(act_files.act_folder(folder, act_id))
        self._pending_downloads[url] = (name, layer_type, act_id)
        size = f" ({_format_size(expected_size)})" if expected_size else ""
        self._push("info", self.tr("Pobieranie {0}{1}...").format(name, size))
        self._downloader.download(url, target_dir, expected_size=expected_size)
        return True

    def _on_download_progress(self, url: str, received: int, total: int) -> None:
        if url not in self._pending_downloads:
            return
        name = self._pending_downloads[url][0]
        done = _format_size(received) or "0 kB"
        of = f" / {_format_size(total)}" if total > 0 else ""
        self._download_label.setText(self.tr("Pobieranie {0}: {1}{2}").format(name, done, of))
        self._download_label.setVisible(True)

    def _on_download_finished(self, url: str, path: str) -> None:
        pending = self._pending_downloads.pop(url, None)
        if pending is None:
            return
        self._download_label.setVisible(bool(self._pending_downloads))
        name, layer_type, act_id = pending
        if layer_type == "raster":
            layer = QgsRasterLayer(path, name, "gdal")
            if not layer.isValid():
                self._open_failed(path, name)
                return
            apply_drawing_resampling(layer)
            QgsProject.instance().addMapLayer(layer)
        elif self._add_vector_layers(path, name, include_tables=True, act_id=act_id) is None:
            self._open_failed(path, name)
            return
        self._push("success", self.tr("Dodano {0} — plik: {1}").format(name, path))

    def _on_download_failed(self, url: str, message: str) -> None:
        pending = self._pending_downloads.pop(url, None)
        if pending is not None:
            self._download_label.setVisible(bool(self._pending_downloads))
            logger.error(f"Nie udało się pobrać {url}: {message}")
            self._push("warning", self.tr("Nie udało się pobrać {0}: {1}").format(pending[0], message))

    def _push(self, level: str, message: str) -> None:
        """Komunikat na pasku QGIS (jeśli jest interfejs)."""
        if not hasattr(self._iface, "messageBar"):
            return
        bar = self._iface.messageBar()
        {"info": bar.pushInfo, "success": bar.pushSuccess, "warning": bar.pushWarning}[level](
            self.tr("Informacja o planie"), message
        )

    def _open_failed(self, url: str, name: str):
        logger.error(f"Nie udało się otworzyć {url}")
        if hasattr(self._iface, "messageBar"):
            self._iface.messageBar().pushWarning(self.tr("Informacja o planie"),
                                                 self.tr("Nie udało się otworzyć {0}").format(name))
        return None
