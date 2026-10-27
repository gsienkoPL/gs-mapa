"""EZiUDP (Ewidencja Zbiorów i Usług Danych Przestrzennych, GUGiK): wyszukiwanie zbiorów i ich usług.

Czysta logika bez zależności od QGIS/Qt — budowa zapytań do API, walidacja odpowiedzi,
sanityzacja zgłoszonych adresów usług, parsowanie dokumentów GetCapabilities (WMS, WMTS, WFS)
i parametry źródeł danych warstw. Sieć i warstwy QGIS obsługuje ``services/eziudp.py``.

Kontrakt API (sprawdzony na żywym serwisie):
``GET https://integracja.gugik.gov.pl/eziudp/api.php`` z parametrami filtrów ``zbior`` (fraza
w nazwie zbioru), ``nazwa`` (nazwa jednostki/organu), ``teryt`` (prefiks kodu), ``rodzaj``
(``centralne``/``wojewodztwa``/``powiaty``/``gminy``), ``usluga`` (rodzaj zgłoszonej usługi),
``result`` (lista pól) i ``count`` (limit). ``nazwa`` to dopasowanie podciągu w nazwie organu
i urzędu (nie w ``obszar``) — wynik zawęża ``filter_result`` do całych słów. Odpowiedź:
``{"metadata": {"count", "limit", ...}, "data": [...]}``; rekord ma pole ``uslugi`` z listami
adresów w kategoriach ``wyszukiwania``,
``przegladania``, ``pobierania``, ``przeksztalcania``, ``uruchamiania``. Adres bywa poprzedzony
jawnym typem usługi, np. ``WMTS=https://...``.
"""

import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .act_index import normalize_text

#: Publiczne API EZiUDP (tylko odczyt).
API_URL = "https://integracja.gugik.gov.pl/eziudp/api.php"
#: Katalog EZiUDP dla ludzi — link do karty zbioru: ``CATALOG_URL?idiip=...``.
CATALOG_URL = "https://integracja.gugik.gov.pl/eziudp/index.php"

#: Najwięcej rekordów w jednym wyszukiwaniu (cały rejestr ma ~12 tys. zbiorów).
SEARCH_LIMIT = 500
#: Limit rozmiaru odpowiedzi API.
MAX_API_BYTES = 8 * 1024 * 1024
#: Limit rozmiaru dokumentu GetCapabilities (krajowe usługi mają po kilka MB).
MAX_CAPABILITIES_BYTES = 20 * 1024 * 1024
#: Najwięcej adresów branych z jednej kategorii usług rekordu.
MAX_URLS_PER_CATEGORY = 20
MAX_URL_LENGTH = 2000

#: Pola rekordu, o które prosi wtyczka.
RESULT_FIELDS = "idiip,teryt,zbior,obszar,organ,uslugi"

#: Typ jednostki (parametr ``rodzaj``): wartość API -> etykieta.
UNIT_KINDS: List[Tuple[str, str]] = [
    ("", "wszystkie"),
    ("centralne", "jednostki centralne"),
    ("wojewodztwa", "województwa"),
    ("powiaty", "powiaty"),
    ("gminy", "gminy"),
]

#: Kategorie usług IIP czytane przez wtyczkę (pozostałe są pomijane).
BROWSE = "przegladania"
DOWNLOAD = "pobierania"
CATEGORIES = (BROWSE, DOWNLOAD)
CATEGORY_LABELS = {BROWSE: "przeglądania", DOWNLOAD: "pobierania"}

#: Filtr usługi (parametr ``usluga``): wartość API -> etykieta.
SERVICE_FILTERS: List[Tuple[str, str]] = [
    ("", "przeglądania lub pobierania"),
    (BROWSE, "przeglądania (WMS, WMTS)"),
    (DOWNLOAD, "pobierania (WFS)"),
]

WMS = "WMS"
WMTS = "WMTS"
WFS = "WFS"
WCS = "WCS"
ATOM = "ATOM"
#: Typy usług, które wtyczka dodaje jako warstwy.
SUPPORTED_KINDS = (WMS, WMTS, WFS)
_KNOWN_KINDS = (WMS, WMTS, WFS, WCS, ATOM)

_IDIIP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,159}$")
_TERYT_INPUT_RE = re.compile(r"^[0-9]{1,6}(_[0-9]?)?$|^[0-9]{7}$")
_COMMUNE_RE = re.compile(r"^[0-9]{6}_[0-9]$")
#: Kod TERYT rekordów jednostek centralnych.
COUNTRY_TERYT = "PL"
_KIND_PREFIX_RE = re.compile(r"^([A-Za-z]{3,5})=(.*)$", re.S)
_SECRET_PARAM_RE = re.compile(r"token|secret|password|passwd|api_?key|auth", re.I)
#: Parametry OGC, które wtyczka ustawia sama — usuwane z adresu bazowego.
_OWS_PARAMS = {"service", "request", "version", "acceptversions"}


class EziudpError(Exception):
    """Błąd integracji z neutralnym komunikatem dla użytkownika (bez treści odpowiedzi)."""


# --- wyszukiwanie -----------------------------------------------------------


@dataclass(frozen=True)
class SearchQuery:
    """
    Filtry wyszukiwania zbiorów (puste pole = bez filtra).

    ``location`` to kod gminy (``TERYT6_rodzaj``) punktu wskazanego na mapie: wynik obejmuje
    wtedy zbiory gminy, jej powiatu i województwa (a dla typu „jednostki centralne” — kraju),
    a pole ``teryt`` jest pomijane.
    """

    dataset: str = ""
    unit: str = ""
    teryt: str = ""
    unit_kind: str = ""
    service: str = ""
    location: str = ""

    def normalized_teryt(self) -> str:
        """
        Kod TERYT w postaci rejestru: prefiks cyfr; 7-cyfrowy kod gminy ``0411022`` -> ``041102_2``.

        :raises EziudpError: gdy kod nie jest poprawnym (prefiksem) kodu TERYT.
        """
        teryt = self.teryt.strip().replace(" ", "")
        if not teryt:
            return ""
        if not _TERYT_INPUT_RE.match(teryt):
            raise EziudpError("Nieprawidłowy kod TERYT — podaj cyfry, np. 12, 1206, 126101 albo 1261011.")
        if len(teryt) == 7 and "_" not in teryt:
            teryt = f"{teryt[:6]}_{teryt[6]}"
        return teryt

    def location_teryts(self) -> Tuple[str, ...]:
        """
        Kody jednostek obejmujących lokalizację: kraj, województwo, powiat, gmina.

        :raises EziudpError: gdy lokalizacja nie jest kodem gminy.
        """
        if not self.location:
            return ()
        if not _COMMUNE_RE.match(self.location):
            raise EziudpError("Nieprawidłowa lokalizacja — wskaż punkt na mapie ponownie.")
        return (COUNTRY_TERYT, self.location[:2], self.location[:4], self.location)

    def is_empty(self) -> bool:
        """Brak filtrów zawężających (sam filtr usługi nie zawęża wyniku)."""
        return not (self.dataset.strip() or self.unit.strip() or self.teryt.strip() or self.unit_kind
                    or self.location)

    def _params(self, teryt: str, unit_kind: str) -> List[Tuple[str, str]]:
        params = []
        for key, value in (
            ("zbior", self.dataset.strip()),
            ("nazwa", self.unit.strip()),
            ("teryt", teryt),
            ("rodzaj", unit_kind),
            ("usluga", self.service),
        ):
            if value:
                params.append((key, value))
        return params

    def _validate(self) -> None:
        if self.unit_kind and self.unit_kind not in dict(UNIT_KINDS):
            raise EziudpError("Nieznany typ jednostki.")
        if self.service and self.service not in dict(SERVICE_FILTERS):
            raise EziudpError("Nieznany rodzaj usługi.")

    def params(self) -> List[Tuple[str, str]]:
        """Parametry zapytania API odpowiadające filtrom (bez lokalizacji)."""
        self._validate()
        return self._params(self.normalized_teryt(), self.unit_kind)

    def requests(self) -> List[List[Tuple[str, str]]]:
        """
        Parametry kolejnych zapytań API (wyniki łączy ``merge_results``).

        Bez lokalizacji to jedno zapytanie. API filtruje TERYT prefiksem, więc lokalizacja to
        zapytanie o powiat (zwraca powiat i jego gminy — nadmiar odrzuca ``filter_result``)
        i osobne o województwo (prefiks województwa objąłby wszystkie jego jednostki).
        """
        self._validate()
        teryts = self.location_teryts()
        if not teryts:
            return [self.params()]
        _country, voivodeship, county, _commune = teryts
        kind = self.unit_kind
        if kind == "centralne":
            return [self._params("", kind)]
        requests = []
        if kind in ("", "powiaty", "gminy"):
            requests.append(self._params(county, kind))
        if kind in ("", "wojewodztwa"):
            requests.append(self._params(voivodeship, "wojewodztwa"))
        return requests


def split_unit_teryt(text: str) -> Tuple[str, str]:
    """
    Pole „Jednostka albo TERYT” → ``(jednostka, teryt)``.

    Same cyfry (z ewentualnym ``_`` rodzaju gminy, spacje pomijane) to kod TERYT, reszta to nazwa
    jednostki: ``"0663011"`` → ``("", "0663011")``, ``"Lublin"`` → ``("Lublin", "")``.
    """
    value = (text or "").strip()
    compact = value.replace(" ", "")
    if compact and re.fullmatch(r"[0-9][0-9_]*", compact):
        return "", compact
    return value, ""


def build_search_urls(query: SearchQuery, limit: int = SEARCH_LIMIT, api_url: str = API_URL) -> List[str]:
    """
    Adresy zapytań API EZiUDP dla filtrów (zwykle jeden, z lokalizacją — dwa).

    :raises EziudpError: gdy brak filtrów albo filtry są niepoprawne.
    """
    if query.is_empty():
        raise EziudpError("Podaj frazę, jednostkę, kod TERYT, typ jednostki albo wskaż miejsce na mapie.")
    return [f"{api_url}?{urlencode(params + [('result', RESULT_FIELDS), ('count', str(limit))])}"
            for params in query.requests()]


def build_search_url(query: SearchQuery, limit: int = SEARCH_LIMIT, api_url: str = API_URL) -> str:
    """
    Adres zapytania API EZiUDP dla filtrów bez lokalizacji.

    :raises EziudpError: gdy brak filtrów, filtry są niepoprawne albo wymagają kilku zapytań.
    """
    urls = build_search_urls(query, limit, api_url)
    if len(urls) != 1:
        raise EziudpError("Wyszukiwanie wymaga kilku zapytań — użyj build_search_urls().")
    return urls[0]


@dataclass(frozen=True)
class ServiceEndpoint:
    """Zgłoszona usługa zbioru."""

    #: Typ usługi: ``WMS``, ``WMTS``, ``WFS``, ``WCS``, ``ATOM``.
    kind: str
    #: Kategoria w EZiUDP: ``przegladania`` albo ``pobierania``.
    category: str
    #: Adres usługi (bez prefiksu typu), jak zgłoszono.
    url: str

    @property
    def supported(self) -> bool:
        return self.kind in SUPPORTED_KINDS

    @property
    def base_url(self) -> str:
        """Adres bez parametrów OGC (``service``/``request``/``version``), z pozostałymi parametrami."""
        return strip_ows_params(self.url)

    @property
    def secure(self) -> bool:
        return self.url.lower().startswith("https://")


@dataclass(frozen=True)
class DatasetRecord:
    """Zbiór danych przestrzennych z EZiUDP."""

    idiip: str
    teryt: str
    name: str
    area: str
    organ: str
    services: Tuple[ServiceEndpoint, ...] = ()

    @property
    def catalog_url(self) -> str:
        return f"{CATALOG_URL}?{urlencode({'idiip': self.idiip})}"

    def supported_services(self) -> List[ServiceEndpoint]:
        return [s for s in self.services if s.supported]


@dataclass(frozen=True)
class SearchResult:
    records: Tuple[DatasetRecord, ...]
    limit: int
    #: Rekordy z API odrzucone przez filtr po stronie wtyczki (np. dopasowanie części słowa).
    hidden: int = 0
    #: Odpowiedź API (któraś z odpowiedzi) osiągnęła limit — prawdopodobnie są kolejne rekordy.
    truncated: bool = False


def unit_matcher(phrase: str):
    """
    Predykat rekordu dla frazy jednostki: całe słowa w obszarze albo nazwie organu.

    API (parametr ``nazwa``) szuka podciągu w nazwie organu i urzędu, więc „Lublin” zwraca też
    „Starosta Lubliniecki” i „Starostwo Powiatowe w Lublinie” (powiat lubelski). Tu fraza musi
    wystąpić jako całe słowa (kolejne, bez względu na wielkość liter i znaki diakrytyczne)
    w ``obszar`` albo ``organ``: „Lublin” -> „miasto Lublin”, „Prezydent Miasta Lublin”.

    :return: funkcja ``DatasetRecord -> bool`` albo ``None``, gdy fraza jest pusta.
    """
    words = normalize_text(phrase).split()
    if not words:
        return None
    pattern = re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, words)) + r"(?!\w)")
    return lambda record: any(pattern.search(normalize_text(text)) for text in (record.area, record.organ))


def filter_result(result: SearchResult, query: SearchQuery) -> SearchResult:
    """
    Wynik API zawężony filtrami, których API nie realizuje dokładnie.

    Fraza jednostki — całe słowa (``unit_matcher``); lokalizacja — tylko jednostki obejmujące
    punkt (kraj, województwo, powiat, gmina), bez pozostałych gmin powiatu. ``hidden`` liczy
    tylko rekordy odrzucone przez frazę (pozostałe gminy powiatu to nie „trafienia” API).
    """
    records = result.records
    covering = set(query.location_teryts())
    if covering:
        records = tuple(r for r in records if r.teryt in covering)
    hidden = result.hidden
    matches = unit_matcher(query.unit)
    if matches is not None:
        kept = tuple(r for r in records if matches(r))
        hidden += len(records) - len(kept)
        records = kept
    if len(records) == len(result.records):  # filtry tylko usuwają rekordy
        return result
    return SearchResult(records=records, limit=result.limit, hidden=hidden, truncated=result.truncated)


def merge_results(results: Sequence[SearchResult]) -> SearchResult:
    """Wyniki kilku zapytań w kolejności zapytań, bez powtórzeń zbiorów."""
    seen, records = set(), []
    for result in results:
        for record in result.records:
            if record.idiip not in seen:
                seen.add(record.idiip)
                records.append(record)
    return SearchResult(
        records=tuple(records),
        limit=max((r.limit for r in results), default=SEARCH_LIMIT),
        hidden=sum(r.hidden for r in results),
        truncated=any(r.truncated for r in results),
    )


def _text(value, max_len: int) -> str:
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split())
    return value[:max_len]


def sanitize_url(raw) -> Optional[str]:
    """
    Zgłoszony adres usługi albo ``None``, gdy nie spełnia reguł bezpieczeństwa.

    Dozwolone: ``http``/``https`` z nazwą hosta, do 2000 znaków, bez danych logowania,
    fragmentu i parametrów wyglądających na sekret (``token``, ``password``, ``auth``…).
    """
    if not isinstance(raw, str):
        return None
    url = raw.strip()
    if not url or len(url) > MAX_URL_LENGTH or any(c.isspace() for c in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port  # noqa: F841 — rzuca ValueError dla niepoprawnego portu
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password or parts.fragment or "#" in url:
        return None
    if any(_SECRET_PARAM_RE.search(key) for key, _ in parse_qsl(parts.query, keep_blank_values=True)):
        return None
    return url


def classify_service(category: str, raw) -> Optional[ServiceEndpoint]:
    """
    Usługa z wpisu kategorii ``uslugi`` (z typem rozpoznanym z prefiksu albo adresu).

    Bez prefiksu: usługa przeglądania to WMTS, gdy adres zawiera ``wmts``, inaczej WMS;
    usługa pobierania to ATOM (``atom`` w adresie, ``.xml``), WCS (``wcs``) albo WFS.
    """
    if not isinstance(raw, str):
        return None
    kind = None
    match = _KIND_PREFIX_RE.match(raw.strip())
    if match and match.group(1).upper() in _KNOWN_KINDS:
        kind, raw = match.group(1).upper(), match.group(2)
    url = sanitize_url(raw)
    if url is None:
        return None
    if kind is None:
        lower = url.lower()
        path = urlsplit(lower).path
        if category == BROWSE:
            kind = WMTS if "wmts" in lower else WMS
        elif "atom" in lower or path.endswith((".xml", ".atom")):
            kind = ATOM
        elif re.search(r"(^|[/=_.-])wcs($|[/&_.-])", lower):
            kind = WCS
        else:
            kind = WFS
    return ServiceEndpoint(kind=kind, category=category, url=url)


def parse_services(uslugi) -> Tuple[ServiceEndpoint, ...]:
    """Usługi przeglądania i pobierania rekordu (bez duplikatów, w kolejności zgłoszenia)."""
    if not isinstance(uslugi, dict):
        return ()
    result: List[ServiceEndpoint] = []
    seen = set()
    for category in CATEGORIES:
        urls = uslugi.get(category)
        if not isinstance(urls, list):
            continue
        for raw in urls[:MAX_URLS_PER_CATEGORY]:
            endpoint = classify_service(category, raw)
            if endpoint is None or (endpoint.kind, endpoint.url) in seen:
                continue
            seen.add((endpoint.kind, endpoint.url))
            result.append(endpoint)
    return tuple(result)


def parse_search_response(data: bytes, limit: int = SEARCH_LIMIT) -> SearchResult:
    """
    Rekordy z odpowiedzi API (z pominięciem rekordów z niepoprawnym identyfikatorem).

    :raises EziudpError: gdy odpowiedź nie jest oczekiwanym JSON-em albo przekracza limit rekordów.
    """
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise EziudpError("Nieprawidłowa odpowiedź katalogu EZiUDP.") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise EziudpError("Nieprawidłowa odpowiedź katalogu EZiUDP.")
    rows = payload["data"]
    if len(rows) > limit:
        raise EziudpError("Odpowiedź katalogu EZiUDP przekracza limit rekordów.")

    records = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        idiip = row.get("idiip")
        if not isinstance(idiip, str) or not _IDIIP_RE.match(idiip):
            continue
        records.append(DatasetRecord(
            idiip=idiip,
            teryt=_text(row.get("teryt"), 20),
            name=_text(row.get("zbior"), 500),
            area=_text(row.get("obszar"), 300),
            organ=_text(row.get("organ"), 300),
            services=parse_services(row.get("uslugi")),
        ))
    return SearchResult(records=tuple(records), limit=limit, truncated=len(rows) >= limit)


# --- adresy usług -------------------------------------------------------------


def strip_ows_params(url: str) -> str:
    """Adres bez parametrów ``service``/``request``/``version`` (wielkość liter bez znaczenia)."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in _OWS_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def capabilities_url(endpoint: ServiceEndpoint) -> str:
    """Adres GetCapabilities usługi (wersja: WMS 1.3.0, WMTS 1.0.0, WFS 2.0.0 — serwer może zejść niżej)."""
    version = {WMS: "1.3.0", WMTS: "1.0.0", WFS: "2.0.0"}.get(endpoint.kind)
    if version is None:
        raise EziudpError(f"Usługa {endpoint.kind} nie jest obsługiwana przez wtyczkę.")
    base = endpoint.base_url
    params = urlencode([("SERVICE", endpoint.kind), ("REQUEST", "GetCapabilities"), ("VERSION", version)])
    return f"{base}{'&' if urlsplit(base).query else '?'}{params}"


# --- GetCapabilities ------------------------------------------------------------


@dataclass(frozen=True)
class OwsLayer:
    """Warstwa z dokumentu GetCapabilities."""

    #: Identyfikator warstwy (``Name`` / ``ows:Identifier``); pusty = warstwa-grupa bez nazwy.
    name: str
    title: str
    abstract: str = ""
    #: Układy współrzędnych (znormalizowane, np. ``EPSG:2180``).
    crs: Tuple[str, ...] = ()
    styles: Tuple[str, ...] = ()
    formats: Tuple[str, ...] = ()
    #: WMTS: pary (identyfikator TileMatrixSet, układ współrzędnych).
    tile_matrix_sets: Tuple[Tuple[str, str], ...] = ()
    #: Styl domyślny (WMTS).
    default_style: str = ""
    children: Tuple["OwsLayer", ...] = ()

    @property
    def label(self) -> str:
        return self.title or self.name

    def walk(self) -> Iterable["OwsLayer"]:
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass(frozen=True)
class Capabilities:
    #: Typ usługi rozpoznany z dokumentu (``WMS``/``WMTS``/``WFS``).
    kind: str
    title: str
    version: str
    layers: Tuple[OwsLayer, ...]
    #: WMS: formaty GetMap.
    formats: Tuple[str, ...] = ()

    def named_layers(self) -> List[OwsLayer]:
        return [layer for top in self.layers for layer in top.walk() if layer.name]


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _children(element, name: str) -> List[ET.Element]:
    return [c for c in element if _local(c.tag) == name]


def _child_text(element, *names: str) -> str:
    for name in names:
        for child in element:
            if _local(child.tag) == name and child.text and child.text.strip():
                return " ".join(child.text.split())
    return ""


def _find(element, *path: str) -> Optional[ET.Element]:
    for name in path:
        found = _children(element, name)
        if not found:
            return None
        element = found[0]
    return element


def normalize_crs(value: str) -> str:
    """``urn:ogc:def:crs:EPSG::2180`` / ``http://www.opengis.net/def/crs/EPSG/0/2180`` -> ``EPSG:2180``."""
    value = (value or "").strip()
    match = re.search(r"EPSG[:/](?:[0-9.]*[:/])*:?([0-9]+)$", value, re.I)
    if match:
        return f"EPSG:{match.group(1)}"
    if re.fullmatch(r"(urn:ogc:def:crs:OGC:[0-9.]*:|)CRS:?84", value, re.I):
        return "CRS:84"
    return value


def _unique(values: Iterable[str]) -> Tuple[str, ...]:
    seen, result = set(), []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _parse_wms_layer(element, inherited_crs: Tuple[str, ...]) -> OwsLayer:
    # WMS 1.1.1 dopuszcza kilka kodów w jednym elemencie SRS rozdzielonych spacją — dziel przed normalizacją.
    own_crs = [normalize_crs(part) for c in element if _local(c.tag) in ("CRS", "SRS")
               for part in (c.text or "").split()]
    crs = _unique(list(inherited_crs) + own_crs)
    styles = _unique(_child_text(s, "Name") for s in _children(element, "Style"))
    children = tuple(_parse_wms_layer(c, crs) for c in _children(element, "Layer"))
    return OwsLayer(
        name=_child_text(element, "Name"),
        title=_child_text(element, "Title"),
        abstract=_child_text(element, "Abstract"),
        crs=crs,
        styles=styles,
        children=children,
    )


def _parse_wms(root) -> Capabilities:
    capability = _find(root, "Capability")
    if capability is None:
        raise EziudpError("Dokument GetCapabilities WMS nie zawiera sekcji Capability.")
    getmap = _find(capability, "Request", "GetMap")
    formats = _unique((f.text or "").strip() for f in _children(getmap, "Format")) if getmap is not None else ()
    layers = tuple(_parse_wms_layer(layer, ()) for layer in _children(capability, "Layer"))
    service = _find(root, "Service")
    return Capabilities(
        kind=WMS,
        title=_child_text(service, "Title") if service is not None else "",
        version=root.get("version", ""),
        layers=layers,
        formats=formats,
    )


def _parse_wmts(root) -> Capabilities:
    contents = _find(root, "Contents")
    if contents is None:
        raise EziudpError("Dokument GetCapabilities WMTS nie zawiera sekcji Contents.")
    matrix_crs = {
        _child_text(tms, "Identifier"): normalize_crs(_child_text(tms, "SupportedCRS"))
        for tms in _children(contents, "TileMatrixSet")
    }
    layers = []
    for element in _children(contents, "Layer"):
        styles = _children(element, "Style")
        default = next((_child_text(s, "Identifier") for s in styles if s.get("isDefault") == "true"), "")
        style_ids = _unique(_child_text(s, "Identifier") for s in styles)
        tms_ids = _unique(_child_text(link, "TileMatrixSet") for link in _children(element, "TileMatrixSetLink"))
        tile_matrix_sets = tuple((tms, matrix_crs.get(tms, "")) for tms in tms_ids)
        layers.append(OwsLayer(
            name=_child_text(element, "Identifier"),
            title=_child_text(element, "Title"),
            abstract=_child_text(element, "Abstract"),
            crs=_unique(crs for _, crs in tile_matrix_sets),
            styles=style_ids,
            formats=_unique((f.text or "").strip() for f in _children(element, "Format")),
            tile_matrix_sets=tile_matrix_sets,
            default_style=default or (style_ids[0] if style_ids else ""),
        ))
    identification = _find(root, "ServiceIdentification")
    return Capabilities(
        kind=WMTS,
        title=_child_text(identification, "Title") if identification is not None else "",
        version=root.get("version", ""),
        layers=tuple(layers),
    )


def _parse_wfs(root) -> Capabilities:
    feature_types = _find(root, "FeatureTypeList")
    if feature_types is None:
        raise EziudpError("Dokument GetCapabilities WFS nie zawiera listy typów obiektów.")
    layers = []
    for element in _children(feature_types, "FeatureType"):
        crs = [normalize_crs(c.text or "") for c in element
               if _local(c.tag) in ("DefaultCRS", "DefaultSRS", "SRS", "OtherCRS", "OtherSRS")]
        layers.append(OwsLayer(
            name=_child_text(element, "Name"),
            title=_child_text(element, "Title"),
            abstract=_child_text(element, "Abstract"),
            crs=_unique(crs),
        ))
    # Element bez dzieci jest „fałszywy” — porównuj z None, nie przez ``or``.
    identification = _find(root, "ServiceIdentification")
    if identification is None:
        identification = _find(root, "Service")
    return Capabilities(
        kind=WFS,
        title=_child_text(identification, "Title") if identification is not None else "",
        version=root.get("version", ""),
        layers=tuple(layers),
    )


def parse_capabilities(data: bytes) -> Capabilities:
    """
    Dokument GetCapabilities WMS (1.1.1/1.3.0), WMTS (1.0.0) albo WFS (1.0.0/1.1.0/2.0.0).

    :raises EziudpError: gdy dokument nie jest XML-em, jest raportem wyjątku usługi albo ma inny format.
    """
    if len(data) > MAX_CAPABILITIES_BYTES:
        raise EziudpError("Dokument GetCapabilities przekracza limit rozmiaru.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise EziudpError("Usługa nie zwróciła poprawnego dokumentu GetCapabilities (XML).") from None
    tag = _local(root.tag)
    if tag in ("ServiceExceptionReport", "ExceptionReport"):
        detail = " ".join(" ".join(root.itertext()).split())[:200]
        raise EziudpError(f"Usługa zgłosiła wyjątek: {detail}" if detail else "Usługa zgłosiła wyjątek.")
    if tag in ("WMS_Capabilities", "WMT_MS_Capabilities"):
        return _parse_wms(root)
    if tag == "Capabilities" and "wmts" in root.tag.lower():
        return _parse_wmts(root)
    if tag == "WFS_Capabilities":
        return _parse_wfs(root)
    raise EziudpError("Usługa zwróciła nieobsługiwany dokument GetCapabilities.")


# --- parametry źródeł danych -----------------------------------------------------

#: Kolejność preferowanych układów, gdy projekt nie ma własnego (albo usługa go nie oferuje).
PREFERRED_CRS = ("EPSG:2180", "EPSG:3857", "EPSG:4326")


def choose_crs(available: Sequence[str], project_crs: str = "") -> str:
    """Układ warstwy: układ projektu, gdy usługa go oferuje, potem EPSG:2180/3857/4326, potem pierwszy."""
    available = [c for c in available if c]
    for candidate in ((project_crs,) if project_crs else ()) + PREFERRED_CRS:
        if candidate in available:
            return candidate
    return available[0] if available else (project_crs or PREFERRED_CRS[0])


def choose_format(formats: Sequence[str]) -> str:
    """Format obrazu: PNG (przezroczystość), potem JPEG, potem pierwszy dostępny."""
    for preferred in ("image/png", "image/png8", "image/png; mode=8bit", "image/jpeg"):
        if preferred in formats:
            return preferred
    return formats[0] if formats else "image/png"


def wms_params(endpoint: ServiceEndpoint, caps: Capabilities, layer: OwsLayer,
               project_crs: str = "") -> List[Tuple[str, str]]:
    """Parametry źródła ``wms`` QGIS dla warstwy WMS albo WMTS (kolejność jak w QGIS)."""
    if caps.kind == WMTS:
        crs = choose_crs([c for _, c in layer.tile_matrix_sets], project_crs)
        tms = next((t for t, c in layer.tile_matrix_sets if c == crs),
                   layer.tile_matrix_sets[0][0] if layer.tile_matrix_sets else "")
        return [
            ("contextualWMSLegend", "0"),
            ("crs", crs),
            ("dpiMode", "7"),
            ("format", choose_format(layer.formats)),
            ("layers", layer.name),
            ("styles", layer.default_style),
            ("tileMatrixSet", tms),
            # QGIS rozpoznaje WMTS po adresie GetCapabilities, nie po adresie bazowym.
            ("url", capabilities_url(ServiceEndpoint(WMTS, endpoint.category, endpoint.url))),
        ]
    return [
        ("contextualWMSLegend", "0"),
        ("crs", choose_crs(layer.crs, project_crs)),
        ("dpiMode", "7"),
        ("format", choose_format(caps.formats)),
        ("layers", layer.name),
        ("styles", ""),
        ("url", endpoint.base_url),
    ]


def wfs_params(endpoint: ServiceEndpoint, layer: OwsLayer, project_crs: str = "") -> List[Tuple[str, str]]:
    """Parametry źródła ``WFS`` QGIS (obiekty pobierane tylko dla widocznego zasięgu)."""
    params = [
        ("url", endpoint.base_url),
        ("typename", layer.name),
        ("version", "auto"),
        # Zbiory powiatowe mają setki tysięcy obiektów — pobieraj tylko widoczny fragment.
        ("restrictToRequestBBOX", "1"),
    ]
    if layer.crs:
        params.append(("srsname", choose_crs(layer.crs, project_crs)))
    return params


# --- pobieranie WFS do GeoPackage ----------------------------------------------------

#: Najdłuższa nazwa pliku i nazwa tabeli GeoPackage proponowane przez wtyczkę.
MAX_FILE_STEM = 80
MAX_TABLE_NAME = 60


def _slug(text: str, max_len: int) -> str:
    """ASCII: litery, cyfry i ``_`` (bez polskich znaków, bez powtórzonych ``_``)."""
    text = unicodedata.normalize("NFKD", str(text or "")).replace("ł", "l").replace("Ł", "L")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:max_len].strip("_")


def default_gpkg_filename(record: DatasetRecord) -> str:
    """Proponowana nazwa pliku: nazwa i obszar zbioru, np. ``EGiB_powiat_krakowski.gpkg``."""
    stem = _slug(f"{record.name} {record.area}", MAX_FILE_STEM) or _slug(record.idiip, MAX_FILE_STEM)
    return f"{stem or 'wfs'}.gpkg"


def gpkg_table_name(layer_name: str, used: Iterable[str] = ()) -> str:
    """
    Nazwa tabeli GeoPackage dla warstwy WFS: identyfikator bez prefiksu przestrzeni nazw.

    ``ms:dzialki`` -> ``dzialki``; nazwa zajęta (bez względu na wielkość liter, jak w SQLite)
    dostaje przyrostek ``_2``, ``_3``…; nazwa zaczynająca się od cyfry — przedrostek ``wfs_``.
    """
    name = _slug(layer_name.rsplit(":", 1)[-1], MAX_TABLE_NAME) or _slug(layer_name, MAX_TABLE_NAME) or "warstwa"
    if name[0].isdigit():
        name = f"wfs_{name}"[:MAX_TABLE_NAME]
    taken = {u.lower() for u in used}
    candidate, number = name, 2
    while candidate.lower() in taken:
        candidate, number = f"{name}_{number}", number + 1
    return candidate


@dataclass(frozen=True)
class WfsDownloadJob:
    """Warstwa WFS do zapisania w GeoPackage."""

    #: Nazwa warstwy dla użytkownika.
    title: str
    #: Nazwa tabeli w pliku GeoPackage.
    table: str
    #: Źródło danych dostawcy ``WFS`` QGIS.
    uri: str
    #: Identyfikator zbioru EZiUDP.
    idiip: str = ""


@dataclass(frozen=True)
class WfsDownloadReport:
    """Wynik pobierania warstw WFS do pliku GeoPackage."""

    path: str
    #: Zapisane warstwy: (zadanie, liczba obiektów).
    written: Tuple[Tuple[WfsDownloadJob, int], ...] = ()
    #: Warstwy, których nie udało się zapisać: (zadanie, komunikat).
    failed: Tuple[Tuple[WfsDownloadJob, str], ...] = ()
    canceled: bool = False
    #: Pobierano tylko obiekty z zasięgu widoku mapy.
    extent_only: bool = False

    @property
    def ok(self) -> bool:
        """Plik powstał (zapisano co najmniej jedną warstwę i nie przerwano pobierania)."""
        return bool(self.written) and not self.canceled

    def summary(self) -> str:
        """Jednozdaniowy opis wyniku dla paska stanu i logu."""
        if self.canceled:
            return "Pobieranie WFS przerwane — plik nie został zapisany."
        if not self.written:
            return "Nie udało się pobrać żadnej warstwy WFS."
        features = sum(count for _, count in self.written)
        scope = " z widoku mapy" if self.extent_only else ""
        text = f"Zapisano warstwy WFS: {len(self.written)} z {len(self.written) + len(self.failed)} " \
               f"({_plural(features, 'obiekt', 'obiekty', 'obiektów')}{scope}) do {self.path}"
        if self.failed:
            text += f"; błędy: {', '.join(job.title for job, _ in self.failed)}"
        return text


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Liczba z odmianą: 1 obiekt, 2 obiekty, 5 obiektów."""
    if count == 1:
        return f"1 {one}"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"{count} {few}"
    return f"{count} {many}"
