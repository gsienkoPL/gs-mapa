"""Domyślne wartości aktu i dokumentów: jednostka oraz dokument uchwalający.

Wartości, które kreator może wyprowadzić sam, bo wynikają wprost z danych, które już ma:
rodzaj jednostki (organ stanowiący), województwo (dziennik urzędowy) i data uchwały
(początek obowiązywania aktu oraz data wejścia w życie dokumentu).

Każda z nich jest deterministyczna — nie jest ustaleniem planistycznym — więc kreator
wypełnia ją jako propozycję: pokazuje źródło, a użytkownik może wartość zmienić.
Moduł nie zależy od QGIS, żeby dał się sprawdzić testem jednostkowym bez PyQGIS.
"""

from __future__ import annotations

from typing import Dict, Mapping, Optional

from .act_index import normalize_text
from .units import GMINA, WOJEWODZTWO, Unit

#: Rola dokumentu, którego data wyznacza ``obowiazujeOd`` aktu (uchwalenie).
ADOPTING_ROLE = "uchwala"

#: Województwo w mianowniku (``resources/units.json``) -> dopełniacz do nazwy organu i dziennika.
VOIVODESHIPS: Dict[str, str] = {
    "dolnośląskie": "Dolnośląskiego",
    "kujawsko-pomorskie": "Kujawsko-Pomorskiego",
    "lubelskie": "Lubelskiego",
    "lubuskie": "Lubuskiego",
    "łódzkie": "Łódzkiego",
    "małopolskie": "Małopolskiego",
    "mazowieckie": "Mazowieckiego",
    "opolskie": "Opolskiego",
    "podkarpackie": "Podkarpackiego",
    "podlaskie": "Podlaskiego",
    "pomorskie": "Pomorskiego",
    "śląskie": "Śląskiego",
    "świętokrzyskie": "Świętokrzyskiego",
    "warmińsko-mazurskie": "Warmińsko-Mazurskiego",
    "wielkopolskie": "Wielkopolskiego",
    "zachodniopomorskie": "Zachodniopomorskiego",
}

#: Rodzaj gminy (ostatnia cyfra TERYT) -> nazwa organu stanowiącego.
COMMUNE_ORGAN: Dict[str, str] = {"1": "Rada Miasta", "2": "Rada Gminy", "3": "Rada Miejsko-Gminna"}

#: Organ stanowiący województwa (dopełnienie dokładane z tabeli ``VOIVODESHIPS``).
VOIVODESHIP_ORGAN = "Sejmik Województwa"

#: Dopełniacze po kluczu znormalizowanym (szukanie dziennika po etykiecie listy kodowej).
_GENITIVE = {normalize_text(name): genitive for name, genitive in VOIVODESHIPS.items()}


def _tail(uri: str) -> str:
    """Kod z URI listy kodowej (``…/dziennikUrzedowyWojMazowieckiego`` → ten kod)."""
    return uri.rstrip("/").rsplit("/", 1)[-1]


def voivodeship_name(unit: Optional[Unit]) -> Optional[str]:
    """
    Województwo jednostki w mianowniku (``mazowieckie``).

    Dla województwa jest to nazwa samej jednostki; dla gminy — województwo, w którym leży.

    :param unit: jednostka z paska kontekstu panelu (albo ``None``).
    :return: nazwa województwa albo ``None`` (brak jednostki, powiat).
    """
    if unit is None:
        return None
    if unit.kind == WOJEWODZTWO:
        return unit.name or None
    return unit.wojewodztwo or None


def organ(unit: Optional[Unit]) -> Optional[str]:
    """
    Organ stanowiący jednostki w formie do pola ``organUstanawiajacy``.

    Nazwę jednostki dokładamy w mianowniku (jak w podpowiedzi pola: „Rada Gminy Ząbki”) —
    odmiana nazw miejscowości zależy od uchwały, więc użytkownik może ją poprawić.

    :param unit: gmina albo województwo (powiat nie stanowi aktów APP).
    :return: np. ``„Rada Gminy Ząbki”``, ``„Sejmik Województwa Mazowieckiego”``; ``None`` bez jednostki.
    """
    if unit is None:
        return None
    if unit.kind == WOJEWODZTWO:
        genitive = _GENITIVE.get(normalize_text(unit.name or ""))
        return f"{VOIVODESHIP_ORGAN} {genitive}".strip() if genitive else VOIVODESHIP_ORGAN
    if unit.kind == GMINA:
        prefix = COMMUNE_ORGAN.get((unit.teryt or "")[6:7], "Rada Gminy")
        return f"{prefix} {(unit.name or '').strip()}".strip()
    return None


def journal(unit: Optional[Unit], values: Mapping[str, str]) -> Optional[str]:
    """
    Kod dziennika urzędowego województwa jednostki (``dziennikUrzedowyWoj…``).

    Lista kodowa ma etykiety w dopełniaczu („Dziennik Urzędowy Woj. Mazowieckiego”),
    więc dopasowanie idzie po końcu etykiety, bez diakrytyków.

    :param unit: jednostka z paska kontekstu.
    :param values: ``codes["journals"]["values"]`` — URI listy -> etykieta.
    :return: kod z listy kodowej albo ``None`` (brak jednostki / dziennika dla województwa).
    """
    genitive = _GENITIVE.get(normalize_text(voivodeship_name(unit)))
    if not genitive:
        return None
    wanted = normalize_text(genitive)
    for uri, label in (values or {}).items():
        if normalize_text(label).endswith(wanted):
            return _tail(uri)
    return None


def document_defaults(unit: Optional[Unit], codes: Optional[dict]) -> Dict[str, str]:
    """
    Wartości dokumentu, które wynikają z jednostki: organ, dziennik i podpis źródła.

    :param unit: jednostka kreatora.
    :param codes: listy kodowe (``load_codes()``).
    :return: ``{"authority": …, "journal": …, "source": …}`` — tylko to, co dało się ustalić.
    """
    result: Dict[str, str] = {}
    name = organ(unit)
    if name:
        result["authority"] = name
    code = journal(unit, ((codes or {}).get("journals") or {}).get("values") or {})
    if code:
        result["journal"] = code
    if result and unit is not None:
        result["source"] = unit.label
    return result


def is_adopting(document: Optional[dict]) -> bool:
    """Czy dokument jest uchwałą uchwalającą akt (jego data wyznacza ``obowiazujeOd``)."""
    return str((document or {}).get("role") or "") == ADOPTING_ROLE


def effective_from(document: Optional[dict]) -> Optional[str]:
    """
    ``obowiazujeOd`` aktu z dokumentu uchwalającego.

    Kolejność: data wejścia w życie dokumentu, a gdy jej nie ma — data uchwały. Uchwała
    wchodzi w życie zwykle w dniu publikacji w dzienniku urzędowym, dlatego data dokumentu
    jest dopiero zapasem.

    :param document: dokument uchwalający (``value()`` z formularza dokumentu).
    :return: data ``RRRR-MM-DD`` albo ``None``.
    """
    document = document or {}
    return document.get("entry_into_force") or document.get("date") or None


def entry_into_force(document: Optional[dict]) -> Optional[str]:
    """
    ``dataWejsciaWZycie`` dokumentu: podana przez użytkownika, a bez niej data dokumentu.

    :param document: dokument (``value()`` z formularza dokumentu).
    :return: data ``RRRR-MM-DD`` albo ``None``.
    """
    return effective_from(document)
