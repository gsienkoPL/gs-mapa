"""Czytelne nazwy typów danych i podwarstw oraz drobne pomoce tekstowe interfejsu."""

from datetime import datetime
from typing import List, Optional, Tuple

#: Krótkie nazwy typów danych (drzewo w panelu ma ok. 300 px szerokości).
TYPE_LABELS = {
    "POG": "Plan ogólny",
    "MPZP": "Plany miejscowe",
    "ZPI": "Plany zintegrowane",
    "PZPW": "Plany województw",
}

#: Nazwy typów aktu w liczbie pojedynczej (okno „Informacja o planie”).
TYPE_LABELS_SINGULAR = {
    "POG": "Plan ogólny gminy",
    "MPZP": "Miejscowy plan zagospodarowania przestrzennego",
    "ZPI": "Zintegrowany plan inwestycyjny",
    "PZPW": "Plan zagospodarowania przestrzennego województwa",
}

#: Nazwy podwarstw po odcięciu prefiksu typu (``pog_strefy`` -> ``strefy``): krótka
#: do drzewa w panelu i pełna do podpowiedzi.
SUBLAYER_LABELS = {
    "granica": ("Granica planu", "Granica obszaru objętego planem"),
    "strefy": ("Strefy planistyczne", "Strefy planistyczne"),
    "ouz": ("Uzupełnienie zabudowy", "Obszar uzupełnienia zabudowy"),
    "ozs": ("Zabudowa śródmiejska", "Obszar zabudowy śródmiejskiej"),
}

#: Przyrostek podwarstwy z obrysami wypełnienia (``pog_strefy_linie``).
LINES_SUFFIX = "_linie"


def type_label(layer_type: str) -> str:
    """Krótka nazwa typu danych, np. ``POG`` -> ``Plan ogólny``."""
    return TYPE_LABELS.get(layer_type.upper(), layer_type.upper())


def type_description(layer_type: str) -> str:
    """Pełna nazwa typu aktu, np. ``MPZP`` -> ``Miejscowy plan zagospodarowania przestrzennego``."""
    return TYPE_LABELS_SINGULAR.get(layer_type.upper(), layer_type.upper())


def sublayer_label(name: str, full: bool = False) -> str:
    """
    Czytelna nazwa podwarstwy, np. ``pog_strefy`` -> ``Strefy planistyczne``.

    Nieznane nazwy: bez prefiksu typu, podkreślenia zamienione na spacje.

    :param name: nazwa podwarstwy z manifestu.
    :param full: pełna nazwa (do podpowiedzi) zamiast krótkiej.
    """
    base = name[: -len(LINES_SUFFIX)] if name.endswith(LINES_SUFFIX) else name
    key = base.split("_", 1)[1] if "_" in base else base
    labels = SUBLAYER_LABELS.get(key)
    label = labels[1 if full else 0] if labels else key.replace("_", " ").capitalize()
    return f"{label} (linie)" if name.endswith(LINES_SUFFIX) else label


def group_sublayers(names: List[str]) -> List[Tuple[str, List[str]]]:
    """
    Łączy wypełnienie z jego obrysami w jedną pozycję.

    ``pog_strefy`` i ``pog_strefy_linie`` to jedna warstwa dla użytkownika — obrysy
    bez wypełnienia (i odwrotnie) wyglądają na mapie niekompletnie.

    :param names: nazwy podwarstw w kolejności z manifestu.
    :return: lista (nazwa główna, wszystkie podwarstwy pozycji).
    """
    present = set(names)
    groups = []
    for name in names:
        if name.endswith(LINES_SUFFIX) and name[: -len(LINES_SUFFIX)] in present:
            continue  # dołączona do swojego wypełnienia
        members = [name]
        if f"{name}{LINES_SUFFIX}" in present:
            members.append(f"{name}{LINES_SUFFIX}")
        groups.append((name, members))
    return groups


def plural_layers(count: int) -> str:
    """Liczba warstw z poprawną odmianą: 1 warstwę, 2 warstwy, 5 warstw."""
    if count == 1:
        word = "warstwę"
    elif count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        word = "warstwy"
    else:
        word = "warstw"
    return f"{count} {word}"


def plural_acts(count: int) -> str:
    """Liczba aktów z poprawną odmianą: 1 akt, 2 akty, 5 aktów."""
    if count == 1:
        word = "akt"
    elif count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        word = "akty"
    else:
        word = "aktów"
    return f"{count} {word}"


def format_date(value: Optional[str]) -> str:
    """Data ISO 8601 jako DD.MM.RRRR; nieparsowalna — bez zmian, pusta — ``""``."""
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return value


def type_role(layer_type: str) -> str:
    """Rola koloru semantycznego typu aktu (``gs_mapa.ui.theme``): ``pog``, ``mpzp``, ``zpi``, ``pzpw``; inne — ``info``."""
    role = (layer_type or "").lower()
    return role if role in ("pog", "mpzp", "zpi", "pzpw") else "info"


def is_inactive_status(status: str) -> bool:
    """Akt nieobowiązujący (uchylony, wygasły, nieważny, archiwalny) — na listach tekstem pomocniczym."""
    text = (status or "").lower()
    return any(word in text for word in ("uchylon", "nieobowiąz", "wygas", "nieważn", "nieaktualn", "archiw"))


def status_role(status: str) -> str:
    """
    Rola koloru semantycznego statusu aktu: ``err`` (nieobowiązujący), ``warn`` (w toku),
    ``ok`` (obowiązuje) albo ``""`` (status nieznany — kolor tekstu).
    """
    text = (status or "").lower()
    if is_inactive_status(text):
        return "err"
    if any(word in text for word in ("w trakcie", "opracowan", "przyjmowan", "projekt")):
        return "warn"
    if any(word in text for word in ("wiążąc", "obowiązuj", "realizowan")):
        return "ok"
    return ""
