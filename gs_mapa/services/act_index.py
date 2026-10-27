"""Pobieranie indeksu aktów (``akty/index.json``) w sposób nieblokujący."""

import json
import logging
from typing import List, Optional

from qgis.PyQt.QtCore import QObject, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from ..core.act_index import ActIndexEntry, parse_act_index

logger = logging.getLogger("GSMapa.ActIndex")


class ActIndexService(QObject):
    """
    Pobiera indeks aktów i trzyma ostatnią poprawną wersję.

    Indeks ma ~2 MB, więc jest pobierany raz na adres — ponowne ``fetch`` z tym samym
    adresem kończy się natychmiast sygnałem z danymi z pamięci. Zmiana buildu w manifeście
    daje nowy adres tylko wtedy, gdy pipeline go zmieni; dlatego ``fetch(url, force=True)``
    wymusza pobranie po zmianie buildu.
    """

    index_ready = pyqtSignal(list)  # list[ActIndexEntry]
    index_error = pyqtSignal(str)

    def __init__(self, timeout_ms: int = 30000, max_size: int = 16 * 1024 * 1024):
        super().__init__()
        self._manager = QNetworkAccessManager()
        self._timeout_ms = timeout_ms
        self._max_size = max_size
        self._url: Optional[str] = None
        self._entries: List[ActIndexEntry] = []
        self._reply: Optional[QNetworkReply] = None

    @property
    def entries(self) -> List[ActIndexEntry]:
        """Ostatni poprawnie pobrany indeks (pusta lista, gdy jeszcze nie ma)."""
        return self._entries

    @property
    def loading(self) -> bool:
        return self._reply is not None

    def fetch(self, url: Optional[str], force: bool = False) -> None:
        """
        Rozpoczyna pobieranie indeksu.

        :param url: adres indeksu z manifestu; ``None`` = indeks niepublikowany.
        :param force: pobierz ponownie, nawet gdy adres się nie zmienił.
        """
        if not url:
            self._entries = []
            self._url = None
            self.index_error.emit("Indeks aktów nie jest jeszcze publikowany.")
            return
        if url == self._url and self._entries and not force:
            self.index_ready.emit(self._entries)
            return
        if self._reply is not None:
            # Trwające pobieranie — nowe żądanie zastępuje poprzednie.
            self._reply.abort()
            self._reply = None

        self._url = url
        request = QNetworkRequest(QUrl(url))
        request.setTransferTimeout(self._timeout_ms)
        reply = self._manager.get(request)
        self._reply = reply
        reply.finished.connect(lambda: self._handle_reply(reply))

    def _handle_reply(self, reply: QNetworkReply) -> None:
        if reply is not self._reply:
            reply.deleteLater()
            return
        self._reply = None
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                if reply.error() == QNetworkReply.NetworkError.OperationCanceledError:
                    return
                status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
                if status in (403, 404):
                    self.index_error.emit("Indeks aktów nie jest jeszcze publikowany.")
                else:
                    self.index_error.emit(f"Błąd sieci: {reply.errorString()}")
                return
            data = bytes(reply.readAll())
            if len(data) > self._max_size:
                self.index_error.emit(f"Indeks aktów zbyt duży ({len(data)} bajtów)")
                return
            self._entries = parse_act_index(json.loads(data.decode("utf-8")))
            logger.info(f"Indeks aktów: {len(self._entries)} aktów z {self._url}")
            self.index_ready.emit(self._entries)
        except (ValueError, UnicodeDecodeError) as e:
            logger.error(f"Niepoprawny indeks aktów: {e}")
            self.index_error.emit(f"Niepoprawny indeks aktów: {e}")
        except Exception as e:
            logger.exception("Błąd odczytu indeksu aktów")
            self.index_error.emit(f"Błąd odczytu indeksu aktów: {e}")
        finally:
            reply.deleteLater()
