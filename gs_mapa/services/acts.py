"""Akty planistyczne: identyfikatory, link do rejestru i pliki JSON aktów.

Format pliku aktu opisuje ``docs/format-akty-json.md``.
"""

import json
import logging
import re
import urllib.parse
from typing import Any, Dict, Optional, Tuple

from qgis.PyQt.QtCore import QObject, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from ..core.models import ActDetails, ActDocument

logger = logging.getLogger("GSMapa.Acts")

#: Adres strony aktu w Rejestrze Urbanistycznym.
REGISTRY_URL = "https://rejestr-urbanistyczny.gov.pl/published/details/{typ}/{published_id}"

#: Grupa w ścieżce szczegółów. Routing SPA Rejestru (wtyczka "published-details-fe") ma
#: wyłącznie ``pog|mpzp|pzpw|uk|ak → :id`` — plany ZPI/MPR/MPO rejestr linkuje jako grupę
#: ``mpzp`` (``getNavigationElement`` w liście "all": INTEGRATED_INVESTMENT_PLAN → "mpzp").
#: Ścieżka ``/published/details/zpi/{id}`` nie istnieje w routerze → strona "Nie znaleziono
#: strony" (przeglądarka + bundle rejestru, 2026-10-25). Identyczna mapa jest w pipeline
#: publikacji (``ru_sync.registry_url``), który zasila ``url_rejestru`` w plikach aktów.
REGISTRY_URL_GROUPS = {
    "pog": "pog",
    "mpzp": "mpzp",
    "zpi": "mpzp",
    "mpr": "mpzp",
    "mpo": "mpzp",
    "pzpw": "pzpw",
    "uk": "uk",
    "ak": "ak",
}

#: Obsługiwana wersja formatu pliku aktu.
ACT_FORMAT_VERSION = 1

_ACT_ID_RE = re.compile(r"^([a-z]+)-(\d+)$")


def parse_act_id(act_id: str) -> Optional[Tuple[str, int]]:
    """
    Rozkłada identyfikator aktu ``{typ}-{publishedActId}``.

    :param act_id: identyfikator z atrybutu ``act_id`` kafli, np. ``mpzp-3003``.
    :return: para (typ, publishedActId) albo ``None`` dla niepoprawnego identyfikatora.
    """
    match = _ACT_ID_RE.match(str(act_id or "").strip().lower())
    if not match:
        return None
    return match.group(1), int(match.group(2))


def registry_url(act_id: str) -> Optional[str]:
    """
    Adres strony aktu w Rejestrze Urbanistycznym — da się go zbudować z samego ``act_id``.

    Grupa w ścieżce nie zawsze pokrywa się z typem (ZPI/MPR/MPO → "mpzp", patrz
    ``REGISTRY_URL_GROUPS``).

    :param act_id: identyfikator aktu.
    :return: adres strony albo ``None`` dla niepoprawnego identyfikatora.
    """
    parsed = parse_act_id(act_id)
    if parsed is None:
        return None
    typ, published_id = parsed
    return REGISTRY_URL.format(typ=REGISTRY_URL_GROUPS.get(typ, typ),
                               published_id=published_id)


def act_json_url(template: str, act_id: str) -> str:
    """Adres pliku JSON aktu z szablonu manifestu."""
    return template.replace("{act_id}", urllib.parse.quote(act_id))


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _optional_text(value: Any) -> Optional[str]:
    text = _text(value).strip()
    return text or None


def _optional_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_act_details(data: Dict[str, Any], expected_act_id: Optional[str] = None) -> ActDetails:
    """
    Parsuje treść pliku aktu.

    Nieznane pola są pomijane, brakujące opcjonalne pola dostają wartości domyślne.

    :param data: zdekodowany JSON pliku aktu.
    :param expected_act_id: identyfikator, o który pytano — plik o innym jest odrzucany.
    :return: szczegóły aktu.
    :raises ValueError: gdy plik ma nieobsługiwaną wersję lub brakuje pól wymaganych.
    """
    if not isinstance(data, dict):
        raise ValueError("Plik aktu nie jest obiektem JSON")
    if data.get("version") != ACT_FORMAT_VERSION:
        raise ValueError(f"Nieobsługiwana wersja pliku aktu: {data.get('version')!r}")

    act_id = _text(data.get("act_id")).strip().lower()
    parsed = parse_act_id(act_id)
    if parsed is None:
        raise ValueError(f"Niepoprawny act_id w pliku aktu: {act_id!r}")
    if expected_act_id is not None and act_id != expected_act_id.strip().lower():
        raise ValueError(f"Plik aktu dotyczy {act_id}, oczekiwano {expected_act_id}")

    documents = []
    for item in data.get("dokumenty") or []:
        if not isinstance(item, dict):
            continue
        documents.append(ActDocument(
            rodzaj=_text(item.get("rodzaj")) or "dokument",
            tytul=_text(item.get("tytul")),
            plik=_text(item.get("plik")),
            url_s3=_optional_text(item.get("url_s3")),
            url_zrodlowy=_optional_text(item.get("url_zrodlowy")),
            status=_text(item.get("status")) or "ok",
            rola=_text(item.get("rola")),
            data=_text(item.get("data")),
            rozmiar=_optional_int(item.get("rozmiar")),
            content_type=_text(item.get("content_type")),
        ))

    dane = data.get("dane") if isinstance(data.get("dane"), dict) else {}

    return ActDetails(
        act_id=act_id,
        typ=parsed[0],
        published_act_id=parsed[1],
        tytul=_text(data.get("tytul")),
        status=_text(data.get("status")),
        obowiazuje_od=_optional_text(data.get("obowiazuje_od")),
        obowiazuje_do=_optional_text(data.get("obowiazuje_do")),
        wojewodztwo=_text(data.get("wojewodztwo")),
        powiat=_text(data.get("powiat")),
        gmina=_text(data.get("gmina")),
        terc=_text(data.get("terc")),
        url_rejestru=_text(data.get("url_rejestru")) or registry_url(act_id) or "",
        dane={str(k): str(v) for k, v in dane.items() if v},
        dokumenty=documents,
    )


class ActService(QObject):
    """
    Pobiera pliki JSON aktów w sposób nieblokujący.

    Każde żądanie jest niezależne — odpowiedź trafia do sygnału razem z ``act_id``,
    więc kilka aktów (kliknięcie w miejsce, gdzie nakładają się plany) może być
    pobieranych równolegle.
    """

    details_ready = pyqtSignal(str, object)  # act_id, ActDetails
    details_error = pyqtSignal(str, str)  # act_id, komunikat

    def __init__(self, timeout_ms: int = 10000, max_size: int = 1024 * 1024):
        super().__init__()
        self._manager = QNetworkAccessManager()
        self._timeout_ms = timeout_ms
        self._max_size = max_size
        self._cache: Dict[str, ActDetails] = {}

    def fetch(self, act_id: str, url_template: Optional[str]) -> None:
        """
        Rozpoczyna pobieranie pliku aktu.

        :param act_id: identyfikator aktu.
        :param url_template: szablon adresu z manifestu; ``None`` = pliki aktów niepublikowane.
        """
        if not url_template:
            self.details_error.emit(act_id, "Lista dokumentów nie jest jeszcze publikowana.")
            return
        if act_id in self._cache:
            self.details_ready.emit(act_id, self._cache[act_id])
            return

        request = QNetworkRequest(QUrl(act_json_url(url_template, act_id)))
        request.setTransferTimeout(self._timeout_ms)
        reply = self._manager.get(request)
        reply.finished.connect(lambda: self._handle_reply(act_id, reply))

    def clear_cache(self) -> None:
        """Zapomina pobrane akty (np. po zmianie buildu w manifeście)."""
        self._cache.clear()

    def _handle_reply(self, act_id: str, reply: QNetworkReply) -> None:
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
                if status in (403, 404):
                    # S3 zwraca 403 dla nieistniejącego obiektu bez prawa listowania.
                    message = "Brak listy dokumentów dla tego aktu."
                else:
                    message = f"Błąd sieci: {reply.errorString()}"
                self.details_error.emit(act_id, message)
                return

            data = bytes(reply.readAll())
            if len(data) > self._max_size:
                self.details_error.emit(act_id, f"Plik aktu zbyt duży ({len(data)} bajtów)")
                return

            details = parse_act_details(json.loads(data.decode("utf-8")), act_id)
            self._cache[act_id] = details
            self.details_ready.emit(act_id, details)
        except (ValueError, UnicodeDecodeError) as e:
            logger.error(f"Niepoprawny plik aktu {act_id}: {e}")
            self.details_error.emit(act_id, f"Niepoprawny plik aktu: {e}")
        except Exception as e:
            logger.error(f"Błąd odczytu pliku aktu {act_id}: {e}")
            self.details_error.emit(act_id, f"Błąd odczytu pliku aktu: {e}")
        finally:
            reply.deleteLater()
