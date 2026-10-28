"""Kompletność danych kreatora APP: gdzie brakuje danych i skąd wziąć wartość.

``check_plan`` zwraca płaską listę uwag; użytkownik dostaje z niej listę, w której nie widać,
która zakładka jest niekompletna. Ten moduł grupuje uwagi po miejscu (zakładka kreatora),
liczy błędy i uwagi oraz podaje krótką podpowiedź „skąd wziąć wartość” dla najczęstszych
braków. Bez QGIS — teksty i liczby, żeby dało się to sprawdzić testem jednostkowym.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

#: Klucze zakładek kreatora (kolejność jak w oknie).
ACT = "act"
DOCUMENTS = "documents"
DRAWINGS = "drawings"
OBJECTS = "objects"
ORDER = (ACT, DOCUMENTS, DRAWINGS, OBJECTS)
GROUP_LABELS = {ACT: "Akt", DOCUMENTS: "Dokumenty", DRAWINGS: "Rysunki", OBJECTS: "Strefy i regulacje"}

#: Podpowiedzi „skąd wziąć wartość” — fragment komunikatu -> podpowiedź.
_HINTS = (
    ("brak przestrzeni nazw", "Przestrzeń nazw zbioru: przycisk „Szukaj w EZiUDP…” albo jednostka "
                              "wybrana na pasku panelu (zbioru APP szuka się po TERYT)."),
    ("brak wersjaId", "Przycisk „Nowa wersja (teraz)” wpisuje bieżący czas w formacie plików APP."),
    ("brak lokalnyId", "lokalnyId aktu to oznaczenie z Rejestru, np. 1POG; przy aktualizacji aktu "
                       "jest niezmienne."),
    ("brak tytułu", "Tytuł bierz z uchwały (albo z dziennika urzędowego) — dokładnie jak w akcie."),
    ("brak geometrii zasięgu", "Wskaż warstwę z granicą aktu; „Tylko zaznaczone obiekty” zawęża "
                               "zasięg do zaznaczenia na mapie."),
    ("brak mapy podkładowej", "Mapa podkładowa to data i referencja (np. „kopia mapy zasadniczej”); "
                              "dla planu miejscowego jest wymagana (OCL ACT-04)."),
    ("brak referencji", "Referencja mapy podkładowej: nazwa albo numer arkusza/zbioru, z którego "
                        "powstała mapa."),
    ("OCL ACT-01", "Dodaj dokument w zakładce Dokumenty: rolę „uchwalenie” (akt obowiązujący) "
                   "albo „przystąpienie do sporządzania”."),
    ("OCL ACT-02", "Akt obowiązujący albo archiwalny musi mieć dokładnie jeden dokument uchwalający."),
    ("OCL ACT-07", "Akt bez stref i regulacji musi mieć rysunek — dodaj go w zakładce Rysunki."),
    ("brak łącza do pliku GeoTIFF", "Łącze to adres GeoTIFF-a z georeferencją (BIP, chmura gminy); "
                                    "bez publikacji pliku walidacja słusznie zgłasza brak."),
    ("brak profilu podstawowego", "Profil podstawowy z macierzy MRiT (np. MN, U, ZP) — w warstwie "
                                  "kolumna profilPodstawowy albo pole w mapowaniu."),
    ("nieznany rodzaj strefy", "Rodzaj strefy z listy kodowej: kod (strefaUslugowa), symbol (SU) "
                               "albo nazwa — sprawdź pisownię."),
    ("profil", "Profil musi być dozwolony dla rodzaju strefy (macierz MRiT); lista jest w podpowiedzi "
               "pola mapowania."),
    ("powtarza się", "Oznaczenia muszą być unikalne w akcie: kolejne obiekty numeruj 1SJ, 2SJ… "
                     "(„Przenumeruj…” w warstwie)."),
    ("nie jest datą", "Format daty to RRRR-MM-DD (pole z kalendarzem przyjmuje tylko poprawną datę)."),
    ("nie odpowiada strefie", "Symbol musi odpowiadać rodzajowi strefy (np. SU ↔ strefaUslugowa); "
                              "przy pustym polu symbol jest wyprowadzany z rodzaju."),
    ("modyfikacja", "Plan ogólny wymaga atrybutu modyfikacja: „nie” — obowiązuje w całości, "
                    "„tak” — w części obszaru (OCL ACT-06)."),
    ("dokument uchylający", "Uchylenie wymaga statusu „nieaktualny” i daty „obowiązuje do” — "
                            "użyj „Popraw” przy wybranej uwadze."),
    ("dokument unieważniający", "Unieważnienie zmienia akt: plan ogólny — modyfikacja=tak, "
                                "inne akty — podniesiona liczba zmian albo status „nieaktualny”."),
)


def group_of(where: str) -> str:
    """
    Zakładka kreatora, której dotyczy uwaga (na podstawie pola ``where`` z ``check_plan``).

    :param where: miejsce uwagi, np. ``„Akt”``, ``„Dokument 2”``, ``„Rysunek 1”``, ``„Strefy”``,
        ``„Obszary uzupełnienia zabudowy (OUZ)”``.
    :return: klucz zakładki (``ACT``, ``DOCUMENTS``, ``DRAWINGS``, ``OBJECTS``).
    """
    text = (where or "").strip()
    if text.startswith(("Akt", "Układ")):
        return ACT
    if text.startswith("Dokument"):
        return DOCUMENTS
    if text.lower().startswith("rysun"):
        return DRAWINGS
    return OBJECTS


def counts(issues: Iterable) -> Dict[str, Tuple[int, int]]:
    """
    Liczba błędów i uwag w każdej zakładce.

    :param issues: obiekty z polami ``severity`` i ``where`` (``PlanIssue`` albo ``Finding``).
    :return: ``{klucz zakładki: (błędy, uwagi)}`` — tylko zakładki z uwagami.
    """
    result: Dict[str, List[int]] = {}
    for issue in issues:
        key = group_of(issue.where)
        pair = result.setdefault(key, [0, 0])
        if issue.severity == "error":
            pair[0] += 1
        elif issue.severity == "warning":
            pair[1] += 1
    return {key: (pair[0], pair[1]) for key, pair in result.items() if any(pair)}


def _plural(number: int, one: str, few: str, many: str) -> str:
    """Odmiana rzeczownika po liczbie (1 błąd, 2 błędy, 5 błędów)."""
    if number == 1:
        return one
    if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        return few
    return many


def tab_label(base: str, errors: int) -> str:
    """Tytuł zakładki z licznikiem błędów (``„Akt (2)”``) albo bez, gdy brak błędów."""
    return f"{base} ({errors})" if errors else base


def tab_tooltip(errors: int, notes: int) -> str:
    """Podpowiedź zakładki: co jest do zrobienia i że dwuklik na liście tu przenosi."""
    if not errors and not notes:
        return ""
    parts = []
    if errors:
        parts.append(f"{errors} " + _plural(errors, "błąd", "błędy", "błędów"))
    if notes:
        parts.append(f"{notes} " + _plural(notes, "uwaga", "uwagi", "uwag"))
    return ", ".join(parts) + " — dwuklik na liście uwag przenosi tutaj"


def summary(issues: Iterable) -> str:
    """
    Jedna linia „gdzie brakuje danych”: ``„Akt — 2 błędy, 1 uwaga; Dokumenty — 1 błąd”``.

    :return: tekst gotowy do podsumowania okna; pusty, gdy nie ma uwag.
    """
    found = counts(issues)
    parts = []
    for key in ORDER:
        if key not in found:
            continue
        errors, notes = found[key]
        pieces = []
        if errors:
            pieces.append(f"{errors} " + _plural(errors, "błąd", "błędy", "błędów"))
        if notes:
            pieces.append(f"{notes} " + _plural(notes, "uwaga", "uwagi", "uwag"))
        parts.append(f"{GROUP_LABELS[key]} — " + ", ".join(pieces))
    return "; ".join(parts)


def hint(issue) -> Optional[str]:
    """
    Podpowiedź „skąd wziąć wartość” dla komunikatu uwagi.

    :param issue: ``PlanIssue`` albo ``Finding`` (liczy się ``message``).
    :return: podpowiedź albo ``None``, gdy komunikat nie ma przygotowanej rady.
    """
    message = str(getattr(issue, "message", "") or "")
    for marker, text in _HINTS:
        if marker.lower() in message.lower():
            return text
    return None
