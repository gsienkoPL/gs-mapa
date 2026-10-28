"""Numeracja oznaczeń obiektów APP: znajdowanie duplikatów i propozycja nowych numerów.

Oznaczenie (``1SJ``, ``2SJ``…) musi być unikalne w obrębie klasy obiektów aktu — duplikat
blokuje zapis pliku. Zamiast szukać go ręcznie w tabeli atrybutów, wtyczka pokazuje listę
zmian: które obiekty mają powtórzone albo puste oznaczenie i jaki numer dostaną.

Moduł nie zależy od QGIS — zapis do warstwy robi ``services/app_numbering.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class Change:
    """Zmiana oznaczenia jednego obiektu warstwy."""

    layer: str
    feature_id: int
    symbol: str
    #: Dotychczasowe oznaczenie (pusty tekst, gdy go nie było).
    old: str
    #: Nowe oznaczenie, np. ``3SJ``.
    new: str

    def __str__(self) -> str:
        return f"{self.layer}: {self.old or '(brak)'} → {self.new}"


def number_of(designation: Optional[str], symbol: str) -> Optional[int]:
    """Numer z oznaczenia tej samej klasy (``12SJ`` + ``SJ`` → 12; inny symbol → ``None``)."""
    text = (designation or "").strip()
    symbol = (symbol or "").strip()
    if not text or not symbol or not text.endswith(symbol):
        return None
    head = text[: -len(symbol)].strip()
    return int(head) if head.isdigit() else None


def plan_renumbering(records: Iterable[Tuple[str, int, str, Optional[str]]]) -> List[Change]:
    """
    Propozycja nowych oznaczeń dla powtórzonych i pustych numerów.

    Zasady:

    - obiekty rozpatrujemy osobno dla każdej warstwy i każdego symbolu (unikalność jest
      w obrębie klasy obiektów APP),
    - pierwsze wystąpienie oznaczenia zostaje bez zmian (kolejność: numer obiektu w warstwie),
    - kolejne wystąpienia i obiekty bez oznaczenia dostają kolejny wolny numer za najwyższym
      użytym w grupie (``1SJ``, ``2SJ`` → następny ``3SJ``),
    - obiekty bez symbolu są pomijane (symbol jest wymagany przez schemat — zgłasza go walidacja).

    :param records: ``(warstwa, numer obiektu, symbol, oznaczenie)``.
    :return: lista zmian w kolejności warstw i numerów obiektów.
    """
    groups: Dict[Tuple[str, str], List[Tuple[int, str]]] = {}
    for layer, feature_id, symbol, designation in records:
        symbol = (symbol or "").strip()
        if not symbol:
            continue
        groups.setdefault((layer, symbol), []).append((int(feature_id), (designation or "").strip()))

    changes: List[Change] = []
    for (layer, symbol), items in sorted(groups.items()):
        items.sort(key=lambda item: item[0])
        used = set()
        for _, designation in items:
            number = number_of(designation, symbol)
            if number is not None:
                used.add(number)
        next_number = max(used) + 1 if used else 1
        seen = set()
        for feature_id, designation in items:
            if designation and designation not in seen:
                seen.add(designation)
                continue
            while next_number in used:
                next_number += 1
            new = f"{next_number}{symbol}"
            used.add(next_number)
            next_number += 1
            changes.append(Change(layer, feature_id, symbol, designation, new))
            seen.add(new)
    return changes
