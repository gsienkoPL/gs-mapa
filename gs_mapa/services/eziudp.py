"""EZiUDP: wyszukiwanie, GetCapabilities, dodawanie warstw usług do projektu i pobieranie WFS do GPKG.

Sieć idzie przez ``QgsNetworkAccessManager`` (ustawienia proxy, SSL i uwierzytelniania QGIS),
asynchronicznie — GUI nie jest blokowane. Reguły dla API EZiUDP: tylko HTTPS, bez
przekierowań, wymagany ``Content-Type: application/json``, limit czasu i rozmiaru odpowiedzi,
neutralne komunikaty błędów (bez treści odpowiedzi).
"""

import logging
import os
from typing import Callable, List, Optional, Sequence, Tuple

from qgis.PyQt.QtCore import QObject, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.core import (
    QgsApplication,
    QgsDataSourceUri,
    QgsNetworkAccessManager,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)

from ..core.eziudp import (
    API_URL,
    MAX_API_BYTES,
    MAX_CAPABILITIES_BYTES,
    SEARCH_LIMIT,
    WFS,
    Capabilities,
    DatasetRecord,
    EziudpError,
    OwsLayer,
    SearchQuery,
    SearchResult,
    ServiceEndpoint,
    WfsDownloadJob,
    WfsDownloadReport,
    build_search_urls,
    capabilities_url,
    filter_result,
    gpkg_table_name,
    merge_results,
    parse_capabilities,
    parse_search_response,
    wfs_params,
    wms_params,
)
from .wfs_download import WfsDownloadTask

logger = logging.getLogger("GSMapa.EZiUDP")

#: Grupa w drzewie warstw na usługi z EZiUDP (podgrupa na każdy zbiór).
GROUP_NAME = "Usługi EZiUDP"

#: Właściwości warstwy opisujące jej pochodzenie.
PROP_IDIIP = "gs_mapa_eziudp_idiip"
PROP_KIND = "gs_mapa_eziudp_kind"
PROP_URL = "gs_mapa_eziudp_url"
PROP_LAYER = "gs_mapa_eziudp_layer"

USER_AGENT = b"GS-Mapa-QGIS-EZiUDP/1"


class EziudpService(QObject):
    """Wyszukiwanie zbiorów w EZiUDP i dodawanie warstw ich usług WMS/WMTS/WFS."""

    #: Wynik wyszukiwania (``SearchResult``).
    search_finished = pyqtSignal(object)
    #: Błąd wyszukiwania — neutralny komunikat.
    search_failed = pyqtSignal(str)
    #: Pobrano GetCapabilities: (``ServiceEndpoint``, ``Capabilities``).
    capabilities_ready = pyqtSignal(object, object)
    #: Błąd GetCapabilities: (``ServiceEndpoint``, komunikat).
    capabilities_failed = pyqtSignal(object, str)
    #: Koniec pobierania WFS do GeoPackage (``WfsDownloadReport``).
    download_finished = pyqtSignal(object)

    def __init__(self, api_url: str = API_URL, timeout_ms: int = 15000,
                 limit: int = SEARCH_LIMIT, require_https: bool = True):
        super().__init__()
        if require_https and not api_url.lower().startswith("https://"):
            raise ValueError("Adres API EZiUDP musi używać HTTPS.")
        self._api_url = api_url
        self._timeout_ms = timeout_ms
        self._limit = limit
        self._search_replies: List[QNetworkReply] = []
        self._search_results: List[Optional[SearchResult]] = []
        self._search_query = SearchQuery()
        self._download_tasks: List[WfsDownloadTask] = []

    # --- sieć ---------------------------------------------------------------

    def _get(self, url: str, max_bytes: int, accept: bytes, follow_redirects: bool,
             on_done: Callable[[QNetworkReply, Optional[str]], None]) -> QNetworkReply:
        """
        Wysyła GET z limitem czasu i rozmiaru; ``on_done(reply, błąd)`` po zakończeniu.

        Przekroczenie rozmiaru przerywa transfer w trakcie (nie po pobraniu całości).
        """
        request = QNetworkRequest(QUrl(url))
        request.setTransferTimeout(self._timeout_ms)
        request.setRawHeader(b"Accept", accept)
        request.setRawHeader(b"User-Agent", USER_AGENT)
        policy = (QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy if follow_redirects
                  else QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute, policy)

        reply = QgsNetworkAccessManager.instance().get(request)
        state = {"too_large": False}

        def on_progress(received: int, _total: int) -> None:
            if received > max_bytes and not state["too_large"]:
                state["too_large"] = True
                reply.abort()

        def on_finished() -> None:
            try:
                error = None
                if state["too_large"]:
                    error = "too_large"
                elif reply.error() != QNetworkReply.NetworkError.NoError:
                    error = "network"
                on_done(reply, error)
            finally:
                reply.deleteLater()

        reply.downloadProgress.connect(on_progress)
        reply.finished.connect(on_finished)
        return reply

    @staticmethod
    def _status(reply: QNetworkReply) -> Optional[int]:
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        return status if isinstance(status, int) else None

    # --- wyszukiwanie ---------------------------------------------------------

    def search(self, query: SearchQuery) -> None:
        """Wyszukuje zbiory; wynik przez ``search_finished`` albo ``search_failed``."""
        try:
            urls = build_search_urls(query, self._limit, self._api_url)
        except EziudpError as e:
            self.search_failed.emit(str(e))
            return
        # Nowe wyszukiwanie unieważnia poprzednie — stary wynik nie nadpisze nowego.
        self._abort_search()
        self._search_query = query
        self._search_results = [None] * len(urls)
        for index, url in enumerate(urls):
            logger.info(f"EZiUDP: {url}")
            self._search_replies.append(self._get(
                url, MAX_API_BYTES, b"application/json", False,
                lambda reply, error, i=index: self._on_search_reply(i, reply, error),
            ))

    def cancel_search(self) -> None:
        """Przerywa trwające wyszukiwanie bez wyniku (np. przy zamykaniu okna, które je zleciło)."""
        self._abort_search()

    def _abort_search(self) -> None:
        """Przerywa trwające zapytania wyszukiwania (ich odpowiedzi są ignorowane)."""
        pending, self._search_replies = self._search_replies, []
        for reply in pending:
            reply.abort()

    def _on_search_reply(self, index: int, reply: QNetworkReply, error: Optional[str]) -> None:
        if not any(reply is r for r in self._search_replies):
            return  # odpowiedź na zapytanie zastąpione nowszym albo przerwane
        self._search_replies = [r for r in self._search_replies if r is not reply]
        try:
            if error == "too_large":
                raise EziudpError("Odpowiedź katalogu EZiUDP przekracza limit rozmiaru.")
            if error is not None:
                logger.warning(f"EZiUDP: błąd sieci: {reply.errorString()}")
                raise EziudpError("Katalog EZiUDP jest chwilowo niedostępny.")
            status = self._status(reply)
            if status is not None and status != 200:
                logger.warning(f"EZiUDP: HTTP {status}")
                raise EziudpError("Katalog EZiUDP jest chwilowo niedostępny.")
            content_type = reply.header(QNetworkRequest.KnownHeaders.ContentTypeHeader)
            if status is not None and "application/json" not in str(content_type or "").lower():
                raise EziudpError("Katalog EZiUDP zwrócił nieobsługiwany format.")
            self._search_results[index] = parse_search_response(bytes(reply.readAll()), self._limit)
        except EziudpError as e:
            self._abort_search()
            self.search_failed.emit(str(e))
            return
        if self._search_replies:
            return  # czekamy na pozostałe zapytania
        results = [r for r in self._search_results if r is not None]
        self.search_finished.emit(filter_result(merge_results(results), self._search_query))

    # --- GetCapabilities ------------------------------------------------------

    def fetch_capabilities(self, endpoint: ServiceEndpoint) -> None:
        """Pobiera GetCapabilities usługi; wynik przez ``capabilities_ready``/``capabilities_failed``."""
        try:
            url = capabilities_url(endpoint)
        except EziudpError as e:
            self.capabilities_failed.emit(endpoint, str(e))
            return
        logger.info(f"GetCapabilities: {url}")
        self._get(url, MAX_CAPABILITIES_BYTES, b"application/xml, text/xml, */*", True,
                  lambda reply, error: self._on_capabilities_reply(endpoint, reply, error))

    def _on_capabilities_reply(self, endpoint: ServiceEndpoint, reply: QNetworkReply,
                               error: Optional[str]) -> None:
        try:
            if error == "too_large":
                raise EziudpError("Dokument GetCapabilities przekracza limit rozmiaru.")
            if error is not None:
                raise EziudpError(f"Usługa nie odpowiada: {reply.errorString()}")
            status = self._status(reply)
            if status is not None and status != 200:
                raise EziudpError(f"Usługa zwróciła HTTP {status}.")
            caps = parse_capabilities(bytes(reply.readAll()))
        except EziudpError as e:
            self.capabilities_failed.emit(endpoint, str(e))
            return
        self.capabilities_ready.emit(endpoint, caps)

    # --- warstwy ----------------------------------------------------------------

    @staticmethod
    def _project_crs() -> str:
        crs = QgsProject.instance().crs()
        return crs.authid() if crs.isValid() else ""

    def create_layer(self, record: DatasetRecord, endpoint: ServiceEndpoint, caps: Capabilities,
                     layer: OwsLayer):
        """
        Tworzy warstwę QGIS usługi (bez dodawania do projektu).

        Uwaga: dostawcy ``wms``/``WFS`` QGIS odpytują usługę już przy tworzeniu warstwy.

        :return: warstwa albo ``None``, gdy QGIS nie potrafi jej otworzyć.
        """
        project_crs = self._project_crs()
        uri = QgsDataSourceUri()
        if caps.kind == WFS:
            for key, value in wfs_params(endpoint, layer, project_crs):
                uri.setParam(key, value)
            qgs_layer = QgsVectorLayer(uri.uri(False), layer.label, "WFS")
        else:
            for key, value in wms_params(endpoint, caps, layer, project_crs):
                uri.setParam(key, value)
            qgs_layer = QgsRasterLayer(bytes(uri.encodedUri()).decode(), layer.label, "wms")
        if not qgs_layer.isValid():
            logger.error(f"Nie udało się otworzyć warstwy {caps.kind} {layer.name} z {endpoint.url}: "
                         f"{qgs_layer.error().summary()}")
            return None

        qgs_layer.setCustomProperty(PROP_IDIIP, record.idiip)
        qgs_layer.setCustomProperty(PROP_KIND, caps.kind)
        qgs_layer.setCustomProperty(PROP_URL, endpoint.url)
        qgs_layer.setCustomProperty(PROP_LAYER, layer.name)
        metadata = qgs_layer.metadata()
        metadata.setIdentifier(record.idiip)
        abstract = f"{record.name} — {record.area}\nOrgan: {record.organ}\nEZiUDP: {record.idiip}"
        if layer.abstract:
            abstract = f"{layer.abstract}\n\n{abstract}"
        metadata.setAbstract(abstract)
        qgs_layer.setMetadata(metadata)
        return qgs_layer

    @staticmethod
    def find_layer(endpoint: ServiceEndpoint, kind: str, layer_name: str):
        """Warstwa tej usługi i tego identyfikatora warstwy w projekcie albo ``None``."""
        for qgs_layer in QgsProject.instance().mapLayers().values():
            if (qgs_layer.customProperty(PROP_URL) == endpoint.url
                    and qgs_layer.customProperty(PROP_KIND) == kind
                    and qgs_layer.customProperty(PROP_LAYER) == layer_name):
                return qgs_layer
        return None

    @staticmethod
    def present() -> set:
        """Warstwy usług w projekcie jako trójki (adres usługi, typ, identyfikator warstwy)."""
        return {
            (layer.customProperty(PROP_URL), layer.customProperty(PROP_KIND), layer.customProperty(PROP_LAYER))
            for layer in QgsProject.instance().mapLayers().values()
            if layer.customProperty(PROP_URL)
        }

    @staticmethod
    def _group(record: DatasetRecord):
        """Podgrupa zbioru w grupie „Usługi EZiUDP” (grupa nowa — na górze drzewa)."""
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(GROUP_NAME)
        if group is None:
            group = root.insertGroup(0, GROUP_NAME)
        name = f"{record.name} — {record.area}" if record.area else record.name
        subgroup = group.findGroup(name)
        if subgroup is None:
            subgroup = group.addGroup(name)
        return subgroup

    def add_layer(self, record: DatasetRecord, endpoint: ServiceEndpoint, caps: Capabilities,
                  layer: OwsLayer) -> Tuple[object, bool]:
        """
        Dodaje warstwę usługi do projektu; warstwa już obecna nie jest dodawana drugi raz.

        :return: para (warstwa albo ``None``, ``True`` gdy dodano nową warstwę).
        """
        existing = self.find_layer(endpoint, caps.kind, layer.name)
        if existing is not None:
            return existing, False
        qgs_layer = self.create_layer(record, endpoint, caps, layer)
        if qgs_layer is None:
            return None, False
        QgsProject.instance().addMapLayer(qgs_layer, False)
        self._group(record).insertLayer(0, qgs_layer)
        return qgs_layer, True

    # --- pobieranie WFS do GeoPackage -------------------------------------------------

    def wfs_jobs(self, selected: Sequence[Tuple[DatasetRecord, ServiceEndpoint, Capabilities, OwsLayer]]
                 ) -> List[WfsDownloadJob]:
        """Zadania pobierania dla zaznaczonych warstw WFS (pozostałe typy są pomijane)."""
        project_crs = self._project_crs()
        jobs, tables, seen = [], [], set()
        for record, endpoint, caps, layer in selected:
            if caps.kind != WFS or (endpoint.url, layer.name) in seen:
                continue
            seen.add((endpoint.url, layer.name))
            uri = QgsDataSourceUri()
            for key, value in wfs_params(endpoint, layer, project_crs):
                uri.setParam(key, value)
            table = gpkg_table_name(layer.name, tables)
            tables.append(table)
            jobs.append(WfsDownloadJob(title=layer.label, table=table, uri=uri.uri(False), idiip=record.idiip))
        return jobs

    def download_wfs(self, jobs: Sequence[WfsDownloadJob], path: str, extent=None, extent_crs=None):
        """
        Pobiera warstwy WFS do pliku GeoPackage w tle; wynik przez ``download_finished``.

        :param extent: zasięg (``QgsRectangle``) w układzie ``extent_crs``; ``None`` = cały zbiór.
        :return: uruchomione zadanie (``WfsDownloadTask``).
        """
        task = WfsDownloadTask(jobs, path, extent, extent_crs, QgsProject.instance().transformContext())
        # Menedżer zadań nie trzyma obiektu Pythona — bez referencji zadanie zostałoby usunięte.
        self._download_tasks.append(task)
        task.done.connect(lambda report, t=task: self._on_download_done(t, report))
        for job in jobs:
            logger.info(f"WFS -> GPKG: {job.title} ({job.uri}) -> {path}|{job.table}")
        QgsApplication.taskManager().addTask(task)
        return task

    def downloading(self, path: str) -> bool:
        """Czy trwa pobieranie do tego pliku."""
        target = os.path.normcase(os.path.abspath(path))
        return any(os.path.normcase(os.path.abspath(t.path)) == target for t in self._download_tasks)

    def _on_download_done(self, task, report: WfsDownloadReport) -> None:
        self._download_tasks = [t for t in self._download_tasks if t is not task]
        for job, message in report.failed:
            logger.warning(f"WFS -> GPKG: {job.title}: {message}")
        self.download_finished.emit(report)

    @staticmethod
    def add_downloaded_layers(report: WfsDownloadReport) -> List[QgsVectorLayer]:
        """
        Dodaje zapisane tabele GeoPackage do projektu (grupa z nazwą pliku na górze drzewa).

        Warstwa z tym samym źródłem już obecna w projekcie jest tylko przeładowywana.

        :return: nowo dodane warstwy.
        """
        project = QgsProject.instance()
        sources = {layer.source(): layer for layer in project.mapLayers().values()}
        added, group = [], None
        for job, _count in report.written:
            source = f"{report.path}|layername={job.table}"
            if source in sources:
                sources[source].dataProvider().reloadData()
                sources[source].triggerRepaint()
                continue
            layer = QgsVectorLayer(source, job.title, "ogr")
            if not layer.isValid():
                logger.error(f"Nie udało się otworzyć pobranej warstwy {source}")
                continue
            layer.setCustomProperty(PROP_IDIIP, job.idiip)
            if group is None:
                root = project.layerTreeRoot()
                name = os.path.basename(report.path)
                group = root.findGroup(name)
                if group is None:
                    group = root.insertGroup(0, name)
            project.addMapLayer(layer, False)
            group.addLayer(layer)
            added.append(layer)
        return added
