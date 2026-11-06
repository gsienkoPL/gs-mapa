"""Konto kafli podkładów: preflight ``GET /me`` (kwota miesięczna klucza API).

Podkłady (``osm``/``bdot10k`` na gs-mapa.cloud — kafle wektorowe i raster WMTS) są za kluczem API; kafle
nie raportują błędów HTTP w warstwie, więc przed dodaniem podkładu sprawdzamy
kontrakt :func:`check_quota` — ``200`` → kwota (``quota`` null = „bez limitu”),
``401`` → klucz niepoprawny, ``403`` → klucz wyłączony. Endpoint nie nalicza kwoty.

Sieć jest **wstrzykiwana parametrem** (wzór ``PracaZbiorowaDialog`` wtyczki Pro):
moduł jest czystym stdlibem, nie dotyka QGIS i sam nie otwiera połączenia — testy
pracują na atrapie transportu. Błąd sieci ``/me`` nie może zablokować legalnego
dodania podkładu (blokujemy tylko wtedy, gdy klucz jest na pewno zły albo kwota
jest pewnie wykorzystana).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional, Protocol
from urllib.parse import quote, urlsplit

from .basemaps import ME_URL

#: Stany wyniku preflight.
OK = "ok"                       # klucz rozpoznany — kwota albo „bez limitu”
INVALID = "invalid"             # 401: klucz niepoprawny
DISABLED = "disabled"           # 403: klucz wyłączony
UNREACHABLE = "unreachable"     # błąd sieci albo odpowiedź nieczytelna — „nie sprawdzono”
EMPTY = "empty"                 # brak klucza — nie ma czego sprawdzać

#: Timeout preflight (sekundy) — kontrola przed dodaniem podkładu nie może wisieć na sieci.
PREFLIGHT_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class HttpResponse:
    """Minimalna odpowiedź transportu."""

    status: int
    body: bytes = b""


class Transport(Protocol):
    """Transport HTTP: ``request`` → :class:`HttpResponse` albo :class:`TransportError`."""

    def request(self, method: str, url: str, headers: dict[str, str],
                body: bytes | None = None) -> HttpResponse: ...


class TransportError(Exception):
    """Nie udało się połączyć z serwerem (DNS, timeout, odrzucone połączenie)."""


class UrllibTransport:
    """Transport na :mod:`urllib.request` — tylko http/https; odpowiedziało to w Pro.

    Błędy HTTP (4xx/5xx) ZWRACAMY jako :class:`HttpResponse`, bo status i treść
    rozstrzygają preflight; połączenie nieudane podnosi :class:`TransportError`.
    """

    def __init__(self, timeout_s: float = PREFLIGHT_TIMEOUT_S) -> None:
        self.timeout_s = timeout_s

    def request(self, method: str, url: str, headers: dict[str, str],
                body: bytes | None = None) -> HttpResponse:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            raise TransportError("Transport obsługuje wyłącznie adresy HTTP i HTTPS")
        # Klucz nie trafia do logów; transport nie drukuje adresu.
        prepared = urllib.request.Request(url, data=body, method=method,
                                          headers={**headers, "User-Agent": "gs_mapa"})
        try:
            # B310: adres wyżej ogranicza schemat do http/https; klucz API jedzie tylko na nie.
            with urllib.request.urlopen(prepared, timeout=self.timeout_s) as response:  # nosec B310
                return HttpResponse(response.status, response.read())
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, exc.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"serwer kafli nie odpowiada ({type(exc).__name__})") from exc


@dataclass(frozen=True)
class QuotaResult:
    """Wynik preflight ``/me``: stan + kwota (``None`` = wartość „bez limitu”)."""

    state: str
    #: Limit miesięczny (requests): ``None`` = serwer nie narzuca limitu.
    quota: Optional[int] = None
    used: Optional[int] = None
    remaining: Optional[int] = None
    tier: str = ""
    month: str = ""
    error: str = ""

    @property
    def limit_exhausted(self) -> bool:
        """Kwota wyczerpana: ``remaining`` zero (``None`` = bez limitu, nie wyczerpana)."""
        return self.state == OK and self.remaining == 0

    @property
    def blocks_layer(self) -> bool:
        """Czy wynik ma NIE pozwolić na dodanie podkładu. Błąd sieci nigdy nie blokuje."""
        return self.state in (INVALID, DISABLED) or self.limit_exhausted

    @property
    def unlimited(self) -> bool:
        return self.state == OK and self.quota is None


def request_url(key: str, url: str = ME_URL) -> str:
    """Adres preflight: ``<url>?key=`` z zakodowanym kluczem (urlenc)."""
    return f"{url.rstrip('/')}?key={quote(key, safe='')}"


def check_quota(key: str, transport: Optional[Transport] = None,
                url: str = ME_URL) -> QuotaResult:
    """Preflight ``GET /me?key=…`` — konto kafli dla klucza użytkownika.

    :param key: klucz API (pusty → stan ``EMPTY`` bez zapytania).
    :param transport: transport HTTP; ``None`` → :class:`UrllibTransport`.
    :param url: baza endpointu (stała ``ME_URL`` z :mod:`core.basemaps`).
    """
    clean = (key or "").strip()
    if not clean:
        return QuotaResult(state=EMPTY, error="brak klucza")
    client = transport if transport is not None else UrllibTransport()
    try:
        response = client.request("GET", request_url(clean, url),
                                  headers={"Accept": "application/json"}, body=None)
    except Exception as exc:  # transport może podnieść wszystko — zawsze zamiana na stan
        return QuotaResult(state=UNREACHABLE, error=str(exc) or type(exc).__name__)
    if response.status == 401:
        return QuotaResult(state=INVALID, error="klucz niepoprawny")
    if response.status == 403:
        return QuotaResult(state=DISABLED, error="klucz wyłączony")
    if response.status != 200:
        return QuotaResult(state=UNREACHABLE, error=f"HTTP {response.status}")
    return _parse_ok(response)


def _parse_ok(response: HttpResponse) -> QuotaResult:
    """Treść ``200`` → :class:`QuotaResult`; nieczytelna treść = „nie sprawdzono”."""
    try:
        data = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return QuotaResult(state=UNREACHABLE, error="niepoprawna odpowiedź serwera /me")
    if not isinstance(data, dict):
        return QuotaResult(state=UNREACHABLE, error="niepoprawna odpowiedź serwera /me")
    try:
        return QuotaResult(
            state=OK, tier=str(data.get("tier", "")), month=str(data.get("month", "")),
            quota=_number(data.get("quota")), used=_number(data.get("used")),
            remaining=_number(data.get("remaining")))
    except ValueError:
        return QuotaResult(state=UNREACHABLE, error="niepoprawna odpowiedź serwera /me")


def _number(value) -> Optional[int]:
    """Pole kwoty: ``None`` albo liczba całkowita (nieczytelne pole → ValueError)."""
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"pole kwoty nie jest liczbą: {type(value).__name__}")
    return int(value)  # „123”/1.0/123 → 123; tekst nie-liczbowy podnosi ValueError


def problem_message(result: QuotaResult) -> Optional[str]:
    """Komunikat dla stanu, który blokuje dodanie podkładu; ``None`` — wolno dodać.

    Komunikaty mówią, co się stało i gdzie jest wyjście (panel albo e-mail).
    """
    if result.state == INVALID:
        return ("Klucz API niepoprawny — sprawdź klucz w panelu GS Mapa "
                "(panel.gs-mapa.cloud/klucze).")
    if result.state == DISABLED:
        return "Klucz API wyłączony — włącz albo wygeneruj nowy klucz w panelu GS Mapa."
    if result.limit_exhausted:
        return "Wykorzystany limit miesięczny — większa liczba żądań: biuro@gs-mapa.cloud."
    return None
