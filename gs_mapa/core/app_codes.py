"""Listy kodowe pól APP — jedna definicja dla wzorcowego GeoPackage i podpowiedzi w formularzu.

Wzorcowy plik ma kolumny o nazwach atrybutów APP, więc wartość wpisywana ręcznie musi być
kodem z listy (``legalForce``, ``strefaUslugowa``, ``dziennikUrzedowyWojMazowieckiego``…).
Ten moduł zamienia listy z ``codelists.json`` na słowniki ``kod -> etykieta`` przypisane do
tabel i pól, żeby plik GPKG mógł dostać listy wyboru (ValueMap) zamiast wolnego tekstu.

Moduł nie zależy od QGIS — listy wyboru zapisuje ``services/app_template.py``.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .app_gml import ACT, DOCUMENT, DRAWING, OSDIS, OUZ, OZS, ZONE
from .app_writer import DATE_TYPES, DOCUMENT_ROLES, LEVELS, NATURES, ROLE_LABELS, STATUSES, load_codes


def _tail(uri: str) -> str:
    """Kod z URI listy kodowej (``…/legalForce`` → ``legalForce``)."""
    return uri.rstrip("/").rsplit("/", 1)[-1]


def _sorted(values: Dict[str, str]) -> Dict[str, str]:
    """Kod i etykieta, alfabetycznie po etykiecie (długie listy: dzienniki, strefy, profile)."""
    return dict(sorted(values.items(), key=lambda item: (item[1] or item[0]).lower()))


def coded_values(codes: Optional[dict] = None) -> Dict[str, Dict[str, Dict[str, str]]]:
    """
    Pola z listami kodowymi wg tabel wzorcowego pliku: ``{tabela: {pole: {kod: etykieta}}}``.

    :param codes: listy kodowe (``load_codes()``); domyślnie z zasobów wtyczki.
    :return: tabela -> pole -> wartości; puste słowniki dla tabel bez list kodowych.
    """
    codes = codes or load_codes()
    act_types = _sorted({_tail(uri): label for uri, label in codes["act_types"]["values"].items()})
    journals = _sorted({_tail(uri): label
                        for uri, label in codes.get("journals", {}).get("values", {}).items()})
    zones = _sorted({_tail(uri): (entry.get("label") or _tail(uri))
                     for uri, entry in codes["zones"]["values"].items()})
    statuses = dict(STATUSES)
    natures = dict(NATURES)
    levels = dict(LEVELS)
    roles = {code: ROLE_LABELS[code] for code in DOCUMENT_ROLES}
    shared = {"status": statuses, "charakterUstalenia": natures}
    return {
        ACT: {"status": statuses, "poziomHierarchii": levels, "typPlanu": act_types},
        DOCUMENT: {"rola": roles, "typDaty": dict(DATE_TYPES), "dziennikUrzedowy": journals},
        DRAWING: {},
        ZONE: {**shared, "nazwa": zones},
        OUZ: shared,
        OZS: shared,
        OSDIS: shared,
    }


def zone_symbols(codes: Optional[dict] = None) -> Dict[str, str]:
    """
    Symbol rodzaju strefy planistycznej: ``{kod: symbol}`` (``strefaUslugowa`` → ``SU``).

    :param codes: listy kodowe (``load_codes()``); domyślnie z zasobów wtyczki.
    """
    codes = codes or load_codes()
    return {_tail(uri): entry.get("symbol") or ""
            for uri, entry in codes["zones"]["values"].items() if entry.get("symbol")}


def _profile_code(uri: str) -> str:
    """Kod profilu z URI ontologii KPT (``…/KPT#KPT-MPZP-MN`` → ``KPT-MPZP-MN``)."""
    return uri.rsplit("#", 1)[-1]


def zone_min_pbc(codes: Optional[dict] = None) -> Dict[str, Optional[int]]:
    """
    Minimalny udział powierzchni biologicznie czynnej w rodzaju strefy: ``{kod: wartość [%]}``.

    Wartość ustala rozporządzenie (zał. 1 do rozp. MRiT z 8.12.2023 w brzmieniu z 22.11.2024):
    plan nie może zejść poniżej niej w strefie danego rodzaju. ``None`` — rodzaj bez minimum
    (SG, SO, SK) i zarazem brak wartości domyślnej w formularzu.

    :param codes: listy kodowe (``load_codes()``); domyślnie z zasobów wtyczki.
    """
    codes = codes or load_codes()
    return {_tail(uri): entry.get("min_pbc") for uri, entry in codes["zones"]["values"].items()}


def zone_profiles(codes: Optional[dict] = None) -> Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]]:
    """
    Profile funkcjonalne dozwolone w rodzaju strefy: ``{kod strefy: (podstawowe, dodatkowe)}``.

    :param codes: listy kodowe (``load_codes()``); domyślnie z zasobów wtyczki.
    """
    codes = codes or load_codes()
    return {_tail(uri): (tuple(_profile_code(p) for p in entry.get("base_profiles", [])),
                         tuple(_profile_code(p) for p in entry.get("additional_profiles", [])))
            for uri, entry in codes["zones"]["values"].items()}


def profile_rows(codes: Optional[dict] = None) -> List[Tuple[str, str, str, str]]:
    """
    Wiersze tabeli słownikowej profili we wzorcowym pliku: ``(kod, etykieta, strefy, w których
    profil jest podstawowy, strefy, w których jest dodatkowy)`` — strefy jako kody po przecinku,
    etykieta z symbolem profilu na początku (``MN — teren zabudowy mieszkaniowej…``).

    Tabela zasila listę wyboru profili filtrowaną rodzajem strefy w formularzu.

    :param codes: listy kodowe (``load_codes()``); domyślnie z zasobów wtyczki.
    :return: wiersze posortowane po symbolu profilu.
    """
    codes = codes or load_codes()
    allowed = zone_profiles(codes)
    rows = []
    for uri, label in codes.get("profile_labels", {}).items():
        code = _profile_code(uri)
        base = sorted(zone for zone, (profiles, _) in allowed.items() if code in profiles)
        extra = sorted(zone for zone, (_, profiles) in allowed.items() if code in profiles)
        short = code.replace("KPT-MPZP-", "")
        rows.append((code, f"{short} — {label or code}", ",".join(base), ",".join(extra)))
    return sorted(rows, key=lambda row: row[1].lower())
