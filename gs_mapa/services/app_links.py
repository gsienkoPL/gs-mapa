"""Sprawdzanie łączy do dokumentów i rysunków aktu (HTTP, w tle).

Okno kreatora nie może czekać na sieć: sprawdzanie idzie przez ``QgsNetworkAccessManager``
(proxy i SSL z ustawień QGIS) po kolei, z limitem czasu na adres, a wynik wraca sygnałem.
Ocena odpowiedzi jest w ``core/app_links.py`` — tutaj tylko wysyłanie zapytań.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from qgis.core import QgsNetworkAccessManager
from qgis.PyQt.QtCore import QObject, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest

from ..core.app_links import LinkResult, check_result

#: Limit czasu na jedno zapytanie (sekundy × 1000).
TIMEOUT_MS = 5000
#: Ile bajtów pobieramy, gdy serwer nie obsługuje ``HEAD`` — dość, by rozpoznać nagłówek pliku.
PROBE_BYTES = 2048
#: Kody, po których warto ponowić zapytanie metodą ``GET`` (serwer nie obsługuje ``HEAD``).
_RETRY_STATUSES = (403, 405, 501)


class LinkChecker(QObject):
    """
    Sprawdza adresy plików: ``HEAD``, a gdy serwer go nie obsługuje — ``GET`` pierwszych bajtów.

    Wyniki pamięta przez czas życia obiektu (sesja okna), więc kolejne „Sprawdź dane” nie
    wysyła tych samych zapytań. Sygnał ``checked`` niesie listę ``LinkResult`` w kolejności
    zgłoszonych adresów.
    """

    #: Lista ``LinkResult`` po sprawdzeniu wszystkich adresów.
    checked = pyqtSignal(list)

    def __init__(self, parent=None, timeout_ms: int = TIMEOUT_MS, manager=None):
        super().__init__(parent)
        self._timeout_ms = timeout_ms
        self._manager = manager
        self._cache: Dict[str, LinkResult] = {}
        self._queue: List[Tuple[str, str]] = []
        self._order: List[str] = []
        self._labels: Dict[str, str] = {}
        self._results: Dict[str, LinkResult] = {}
        self._reply: Optional[QNetworkReply] = None
        self._current: Optional[Tuple[str, str]] = None
        self._timed_out = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timeout)

    # --- API ---------------------------------------------------------------------------

    def check(self, items: List[Tuple[str, str]]) -> None:
        """
        Sprawdza listę adresów.

        :param items: pary ``(adres, miejsce w formularzu)``, np. ``("https://…/uchwala.pdf", "Dokument 1")``.
        """
        self.cancel()
        self._order, self._labels, self._results = [], {}, {}
        pending: List[Tuple[str, str]] = []
        for url, where in items:
            url = (url or "").strip()
            if not url or url in self._labels:
                continue
            self._order.append(url)
            self._labels[url] = where
            if url in self._cache:
                self._results[url] = self._cache[url]
            else:
                pending.append((url, where))
        self._queue = pending
        self._next()

    def busy(self) -> bool:
        """Czy trwa sprawdzanie (kolejka albo odpowiedź w locie)."""
        return bool(self._queue) or self._current is not None

    def cancel(self) -> None:
        """Przerywa sprawdzanie (zamknięcie okna) — bez emisji wyniku."""
        self._timer.stop()
        if self._reply is not None:
            reply, self._reply = self._reply, None
            reply.finished.disconnect()
            reply.abort()
            reply.deleteLater()
        self._current = None
        self._queue = []

    def cache(self) -> Dict[str, LinkResult]:
        """Wyniki zapamiętane w tej sesji (adres → wynik)."""
        return dict(self._cache)

    # --- wysyłanie zapytań -------------------------------------------------------------

    def _session(self):
        return self._manager or QgsNetworkAccessManager.instance()

    def _next(self) -> None:
        if not self._queue:
            self._finish()
            return
        url, where = self._queue.pop(0)
        self._current = (url, where)
        self._send(url, head=True)

    def _send(self, url: str, head: bool) -> None:
        request = QNetworkRequest(QUrl(url))
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        if head:
            self._reply = self._session().head(request)
        else:
            request.setRawHeader(b"Range", b"bytes=0-%d" % (PROBE_BYTES - 1))
            self._reply = self._session().get(request)
        reply = self._reply
        if reply is None:
            return
        self._timed_out = False
        reply.finished.connect(self._on_finished)
        self._timer.start(self._timeout_ms)

    def _on_timeout(self) -> None:
        if self._reply is None:
            return
        self._timed_out = True
        self._reply.abort()

    def _status(self, reply: QNetworkReply) -> Optional[int]:
        code = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        return int(code) if code is not None else None

    def _content_type(self, reply: QNetworkReply) -> str:
        value = reply.header(QNetworkRequest.KnownHeaders.ContentTypeHeader)
        return str(value or "")

    def _on_finished(self) -> None:
        reply, self._reply = self._reply, None
        if reply is None:
            return
        self._timer.stop()
        reply.finished.disconnect()
        try:
            self._handle(reply)
        finally:
            reply.deleteLater()

    def _handle(self, reply: QNetworkReply) -> None:
        if self._current is None:
            return
        url, where = self._current
        status = self._status(reply)
        error = reply.error()
        # Metodę rozpoznajemy po nagłówku ``Range``, który dokładamy tylko do ``GET``.
        is_get = reply.request().rawHeader(b"Range") != b""
        if not is_get and (error == QNetworkReply.NetworkError.ProtocolInvalidOperationError
                           or status in _RETRY_STATUSES):
            # Serwer nie obsługuje ``HEAD`` — pobierz pierwsze bajty pliku.
            self._send(url, head=False)
            return
        head_bytes = b""
        if is_get and error == QNetworkReply.NetworkError.NoError:
            head_bytes = bytes(reply.read(PROBE_BYTES))
        text = "" if error == QNetworkReply.NetworkError.NoError else reply.errorString()
        if self._timed_out:
            text, status = "przekroczono czas oczekiwania", None
        result = check_result(url, where, status, self._content_type(reply), head_bytes, text)
        self._results[url] = result
        self._cache[url] = result
        self._current = None
        self._next()

    def _finish(self) -> None:
        self._current = None
        results = [self._results[url] for url in self._order if url in self._results]
        self.checked.emit(results)
