"""ULDK (Usługa Lokalizacji Działek Katastralnych, GUGiK): działka i jej jednostki w punkcie.

Czysta logika bez zależności od QGIS/Qt. Sieć obsługuje ``services/uldk.py``.

Kontrakt (sprawdzony na żywym serwisie):
``GET https://uldk.gugik.gov.pl/?request=GetParcelByXY&xy=X,Y,2180&result=id,voivodeship,county,commune``
-> ``text/plain``: pierwsza linia to status (``0`` = sukces, ``-1 brak wyników``), druga
``066301_1.0036.AR_4.22|lubelskie|powiat Lublin|Lublin (miasto)``. Identyfikator działki zaczyna
się od kodu TERYT jednostki ewidencyjnej (``TERYT6_rodzaj``); w Warszawie i Krakowie jest to
dzielnica / delegatura (rodzaj ``8`` / ``9``), a nie gmina.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlencode

#: Publiczna usługa ULDK (tylko odczyt).
ULDK_URL = "https://uldk.gugik.gov.pl/"
#: Układ współrzędnych zapytań (PUWG 1992).
ULDK_EPSG = 2180
#: Limit rozmiaru odpowiedzi (jedna działka, bez geometrii).
MAX_ULDK_BYTES = 64 * 1024

_PARCEL_ID_RE = re.compile(r"^([0-9]{6}_[0-9])\.[^\s|]{1,100}$")
#: Rodzaj jednostki ``8`` (dzielnica Warszawy) i ``9`` (delegatura) leży w gminie miejskiej ``TTPP01_1``.
_CITY_PART_KINDS = ("8", "9")


class UldkError(Exception):
    """Błąd ULDK z neutralnym komunikatem dla użytkownika."""


@dataclass(frozen=True)
class ParcelLocation:
    """Działka w klikniętym punkcie i jej jednostki administracyjne."""

    parcel_id: str
    voivodeship: str = ""
    county: str = ""
    commune: str = ""

    @property
    def unit_teryt(self) -> str:
        """Kod jednostki ewidencyjnej z identyfikatora działki, np. ``066301_1``, ``146510_8``."""
        return self.parcel_id.split(".", 1)[0]

    @property
    def commune_teryt(self) -> str:
        """Kod gminy (``TERYT6_rodzaj``): dzielnica/delegatura -> gmina miejska ``TTPP01_1``."""
        teryt = self.unit_teryt
        if teryt[-1] in _CITY_PART_KINDS:
            return f"{teryt[:4]}01_1"
        return teryt

    @property
    def label(self) -> str:
        """Opis do interfejsu, np. ``Lublin (miasto), powiat Lublin, lubelskie``."""
        return ", ".join(filter(None, (self.commune, self.county, self.voivodeship))) or self.commune_teryt


def parcel_by_xy_url(x: float, y: float, base_url: str = ULDK_URL) -> str:
    """Adres zapytania o działkę w punkcie (współrzędne w EPSG:2180)."""
    params = [
        ("request", "GetParcelByXY"),
        ("xy", f"{x:.2f},{y:.2f},{ULDK_EPSG}"),
        ("result", "id,voivodeship,county,commune"),
    ]
    return f"{base_url}?{urlencode(params)}"


def _clean(value: str) -> str:
    return " ".join(value.split())[:200]


def parse_parcel_response(data: bytes) -> ParcelLocation:
    """
    Działka z odpowiedzi ``GetParcelByXY``.

    :raises UldkError: brak działki w punkcie albo nieprawidłowa odpowiedź.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UldkError("Nieprawidłowa odpowiedź usługi ULDK.") from None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise UldkError("Nieprawidłowa odpowiedź usługi ULDK.")
    status = lines[0].split(None, 1)[0]
    if status == "-1":
        raise UldkError("W tym miejscu nie ma działki ewidencyjnej.")
    if status != "0" or len(lines) < 2:
        raise UldkError("Nieprawidłowa odpowiedź usługi ULDK.")
    fields = lines[1].split("|")
    if not _PARCEL_ID_RE.match(fields[0]):
        raise UldkError("Nieprawidłowa odpowiedź usługi ULDK.")
    fields += [""] * (4 - len(fields))
    return ParcelLocation(
        parcel_id=fields[0],
        voivodeship=_clean(fields[1]),
        county=_clean(fields[2]),
        commune=_clean(fields[3]),
    )
