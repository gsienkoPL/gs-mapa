"""Decyzje dla rysunków po wycięciu obszaru planu — czysty Python (bez QGIS).

Wycinanie obszaru w planie miejscowym zmienia granicę aktu, więc rysunki trzeba podzielić na trzy
grupy (APP 3.0: rysunek nie ma ``status``, więc wycofanie to ``koniecWersjiObiektu``
i ``obowiazujeDo``):

============================  ==============================================================
cały arkusz w wycięciu        **wycofanie bez nowej wersji** — ten sam ``wersjaId``,
                              ``koniecWersjiObiektu`` = początek nowej wersji aktu,
                              ``obowiazujeDo`` = data wycofania; arkusz zostaje w pliku
część arkusza w wycięciu      **nowa wersja** — nowe ``wersjaId``, nowe ``lacze``, ``opis``
                              z informacją o przycięciu, przycięty plik GeoTIFF
arkusz nietknięty             **bez zmian** — ta sama wersja, to samo łącze
============================  ==============================================================

Nowa wersja rysunku musi zmienić ``opis``: :func:`gs_mapa.core.app_versioning.same_drawing`
porównuje metadane, więc ten sam adres pliku z niezmienionym opisem znaczyłby „bez zmian”,
choć treść rastra się zmieniła.

Moduł nic nie zapisuje i nie zna QGIS — dostaje wynik ``classify_raster`` (``services/app_raster``)
i zwraca decyzje, które kreator zamienia na nowy plik APP.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Mapping, Optional, Sequence

#: Dopuszczalne czynności dla rysunku.
ACTIONS = ("unchanged", "new_version", "retire")
#: Klasy wycięcia z ``services/app_raster.classify_raster``.
CLASSES = ("outside", "partial", "inside")
#: Czynność dla klasy wycięcia.
ACTION_FOR_CLASS = {"outside": "unchanged", "partial": "new_version", "inside": "retire"}


@dataclass
class DrawingDecision:
    """Co zrobić z jednym rysunkiem po wycięciu obszaru planu."""

    local_id: str
    #: ``"unchanged"`` | ``"new_version"`` | ``"retire"``.
    action: str
    #: ``koniecWersjiObiektu`` (tylko wycofanie).
    version_end: Optional[str] = None
    #: ``obowiazujeDo`` (tylko wycofanie).
    effective_to: Optional[str] = None
    #: Opis rysunku do zapisu (``None`` — pole puste).
    description: Optional[str] = None
    #: Uzasadnienie dla raportu w oknie i w panelu uwag kreatora.
    reason: str = ""


def crop_note(act_title: str, version_id: str, removed_area_m2: float) -> str:
    """
    Nota do ``opis`` przyciętego rysunku.

    Przykład: „przycięty do granicy aktu po wycięciu obszaru planu Plan ogólny Testowo
    (wersja 20270101T000000), −12,4 ha”.

    :param act_title: tytuł aktu, w którym wycięto obszar.
    :param version_id: ``wersjaId`` nowej wersji aktu.
    :param removed_area_m2: pole wyciętego obszaru w m² (przeliczane na hektary).
    """
    title = (act_title or "").strip() or "bez tytułu"
    version = (version_id or "").strip()
    suffix = f" (wersja {version})" if version else ""
    return (f"przycięty do granicy aktu po wycięciu obszaru planu {title}{suffix}, "
            f"−{_hectares(removed_area_m2)} ha")


def decide_drawings(drawings: Sequence[dict], classes: Mapping[str, str], version_start: str,
                    retire_date: str, act_title: str, removed_area_m2: float,
                    version_id: Optional[str] = None) -> List[DrawingDecision]:
    """
    Decyzje dla rysunków na podstawie klasy wycięcia każdego arkusza.

    :param drawings: rysunki źródłowe (``read_act_source(...).drawings`` — słowniki z ``local_id``,
        ``version_id`` i ``description``).
    :param classes: ``lokalnyId`` → ``"outside"`` | ``"partial"`` | ``"inside"``
        (wynik ``classify_raster`` dla każdego rysunku).
    :param version_start: początek nowej wersji aktu (``koniecWersjiObiektu`` wycofanych arkuszy).
    :param retire_date: ``obowiazujeDo`` wycofanych arkuszy — data podana przez użytkownika
        (zwykle data wejścia w życie nowego planu).
    :param act_title: tytuł aktu do noty w ``opis``.
    :param removed_area_m2: pole wyciętego obszaru (m²) do noty w ``opis``.
    :param version_id: ``wersjaId`` nowej wersji aktu; bez niego brany z ``version_start``.
    :raises ValueError: brak klasy dla rysunku albo klasa spoza ``CLASSES`` — decyzji nie zgadujemy.
    """
    new_version = version_id or _version_from_start(version_start)
    note = crop_note(act_title, new_version, removed_area_m2)
    decisions: List[DrawingDecision] = []
    for drawing in drawings:
        local_id = str(drawing.get("local_id") or "")
        name = local_id or "(bez lokalnyId)"
        klass = classes.get(local_id)
        if klass is None:
            raise ValueError(f"brak klasy wycięcia dla rysunku {name} — nie wiadomo, czy go przyciąć")
        if klass not in CLASSES:
            raise ValueError(f"nieznana klasa wycięcia „{klass}” dla rysunku {name} "
                             f"(oczekiwano: {', '.join(CLASSES)})")
        description = str(drawing.get("description") or "").strip() or None
        action = ACTION_FOR_CLASS[klass]
        if action == "unchanged":
            decisions.append(DrawingDecision(local_id, action, description=description,
                                             reason="rysunek leży poza wyciętym obszarem — bez zmian"))
        elif action == "new_version":
            decisions.append(DrawingDecision(
                local_id, action, description=f"{description}; {note}" if description else note,
                reason="część arkusza w wyciętym obszarze — nowa wersja z przyciętym plikiem"))
        else:
            decisions.append(DrawingDecision(
                local_id, action, version_end=version_start, effective_to=retire_date,
                description=description,
                reason="cały arkusz w wyciętym obszarze — wycofanie bez nowej wersji"))
    return decisions


def summary(decisions: Sequence[DrawingDecision]) -> Dict[str, int]:
    """Liczby dla raportu: ``{"unchanged": n, "new_version": n, "retire": n}``."""
    counts = {action: 0 for action in ACTIONS}
    for decision in decisions:
        counts[decision.action] = counts.get(decision.action, 0) + 1
    return counts


def _version_from_start(version_start: str) -> str:
    """``wersjaId`` z początku wersji (``2027-01-01T00:00:00Z`` → ``20270101T000000``)."""
    text = (version_start or "").strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y%m%dT%H%M%S")
    except ValueError:
        return ""


def _hectares(area_m2: float) -> str:
    """Pole w hektarach z polskim separatorem: ``124000`` → ``12,4``, ``1234567`` → ``123,5``."""
    try:
        hectares = float(area_m2) / 10000.0
    except (TypeError, ValueError):
        return "0,0"
    text = f"{hectares:,.1f}".replace(",", " ").replace(".", ",")
    return text
