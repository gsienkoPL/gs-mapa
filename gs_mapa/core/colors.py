"""Kolory pochodne od palety motywu — bez zależności od Qt (testowalne bez QGIS)."""

from typing import Sequence

#: Udział koloru tekstu w tle wyróżnionej ramki (reszta to tło okna).
BOX_TEXT_SHARE = 0.08
#: Udział koloru tekstu w kolorze tekstu pomocniczego.
MUTED_TEXT_SHARE = 0.75


def mix_rgb(first: Sequence[int], second: Sequence[int], share: float) -> str:
    """
    Mieszanina dwóch kolorów RGB jako ``#rrggbb``.

    :param first: kolor ``(r, g, b)`` w zakresie 0–255.
    :param second: kolor ``(r, g, b)``.
    :param share: udział pierwszego koloru (0 — sam ``second``, 1 — sam ``first``).
    """
    share = min(1.0, max(0.0, share))
    mixed = [round(a * share + b * (1 - share)) for a, b in zip(first[:3], second[:3])]
    return "#{:02x}{:02x}{:02x}".format(*mixed)


def box_color(text: Sequence[int], base: Sequence[int]) -> str:
    """
    Tło wyróżnionej ramki: tło okna lekko przesunięte w stronę koloru tekstu.

    Nie używamy roli ``AlternateBase`` — w części motywów (np. ciemny tryb macOS)
    jest jasnoszara i biały tekst oraz tekst pomocniczy stają się na niej nieczytelne.
    Mieszanina z ``Text``/``Base`` zawsze zachowuje kontrast z kolorem tekstu.
    """
    return mix_rgb(text, base, BOX_TEXT_SHARE)


def muted_color(text: Sequence[int], base: Sequence[int]) -> str:
    """Kolor tekstu pomocniczego — czytelny na tle okna i na tle ramki (:func:`box_color`)."""
    return mix_rgb(text, base, MUTED_TEXT_SHARE)


def _luminance(rgb: Sequence[float]) -> float:
    def channel(value: float) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(first: Sequence[float], second: Sequence[float]) -> float:
    """Współczynnik kontrastu WCAG 2.x (1–21)."""
    hi, lo = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def hex_to_rgb(value: str) -> tuple:
    """``#rrggbb`` -> ``(r, g, b)``."""
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb: Sequence[float]) -> str:
    """``(r, g, b)`` -> ``#rrggbb``."""
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(round(v)))) for v in rgb[:3]))


#: Minimalny kontrast tekstu do tła (WCAG AA dla zwykłego tekstu).
MIN_CONTRAST = 4.5

#: Kolory semantyczne interfejsu (``UI_GUIDELINES.md``, tabela 3.1): rola -> (jasny, ciemny).
#: Nowy kolor dodawaj tylko tutaj i tylko po sprawdzeniu kontrastu w obu motywach
#: (``tests/test_theme.py``).
SEMANTIC_COLORS = {
    "ok": ("#17702f", "#4ac26b"),
    "warn": ("#8a5a00", "#e3b341"),
    "err": ("#c4222b", "#ff6b66"),
    "info": ("#0860c4", "#6cb6ff"),
    "pog": ("#6f3fc0", "#c39bff"),
    "mpzp": ("#0d7066", "#4fd1c0"),
    "zpi": ("#a24a00", "#f2a65a"),
    "pzpw": ("#0860c4", "#7ab8ff"),
}

#: Wygląd wtyczki (``UI_GUIDELINES.md``, pkt 3.2): tokeny kolorów z makiet ``docs/ui/`` — te same
#: wartości ma aplikacja przeglądarkowa. Wariant (jasny / ciemny) wybiera jasność okna QGIS,
#: ale same kolory nie zależą od stylu Qt ani systemu. Kontrasty pilnuje ``tests/test_theme.py``.
UI_TOKENS = {
    "light": {
        "window": "#ececec",         # tło panelu i okien
        "surface": "#ffffff",        # listy, drzewa, tabele, menu
        "alt": "#f5f5f5",            # co drugi wiersz listy
        "field": "#ffffff",          # pola tekstowe i combo
        "button": "#ffffff",         # zwykły przycisk
        "button_hover": "#f5f5f5",
        "button_pressed": "#e6e6e6",
        "hover": "#e0e0e0",          # przycisk płaski / karta pod kursorem
        "border": "#d1d1d1",         # ramki pól, list, przycisków; separatory
        "border_strong": "#b4b4b8",  # chip wyłączony, pole wyboru, uchwyt przewijania
        "text": "#1d1d1f",
        "muted": "#636366",          # tekst pomocniczy
        "disabled": "#a1a1a6",       # tekst nieaktywny
        "accent": "#0064d3",         # przycisk główny, zaznaczenie, aktywna karta
        "accent_hover": "#0057b8",
        "accent_text": "#ffffff",
        "link": "#0064d3",
    },
    "dark": {
        "window": "#1e1e1e",
        "surface": "#151515",
        "alt": "#1a1a1a",
        "field": "#262626",
        "button": "#323232",
        "button_hover": "#3a3a3a",
        "button_pressed": "#2a2a2a",
        "hover": "#2e2e2e",
        "border": "#3a3a3a",
        "border_strong": "#5a5a5e",
        "text": "#ececec",
        "muted": "#a1a1a6",
        "disabled": "#6e6e73",
        "accent": "#1f62c8",
        "accent_hover": "#2a6fd6",
        "accent_text": "#ffffff",
        "link": "#5fa8f4",
    },
}


def ui_tokens(dark: bool) -> dict:
    """Tokeny wyglądu wtyczki dla motywu ciemnego albo jasnego (kopia — można ją zmieniać)."""
    return dict(UI_TOKENS["dark" if dark else "light"])


def is_dark_rgb(window: Sequence[int]) -> bool:
    """Motyw ciemny: jasność (HSL lightness) koloru okna < 128 — jak ``QColor.lightness()``."""
    r, g, b = window[:3]
    return (max(r, g, b) + min(r, g, b)) / 2 < 128


def ensure_contrast(color: Sequence[int], backgrounds: Sequence[Sequence[int]],
                    minimum: float = MIN_CONTRAST) -> str:
    """
    Kolor ``color`` przesunięty w stronę bieli albo czerni, aż osiągnie kontrast ``minimum``
    do każdego tła z ``backgrounds``.

    Wartości z :data:`SEMANTIC_COLORS` mają kontrast ≥ 4.5:1 do typowych palet jasnych i ciemnych;
    nietypowa paleta (np. ciemnoszare okno ``#454545``) dostaje kolor rozjaśniony tylko tyle,
    ile trzeba. Odcień i nasycenie zostają rozpoznawalne.
    """
    if not backgrounds:
        return rgb_to_hex(color)
    darkest_bg = min(backgrounds, key=_luminance)
    lightest_bg = max(backgrounds, key=_luminance)
    # Tło ciemne -> rozjaśniamy kolor, jasne -> przyciemniamy.
    target = (255, 255, 255) if _luminance(lightest_bg) < 0.18 else (0, 0, 0)
    current = tuple(color[:3])
    for step in range(0, 101, 4):
        candidate = tuple(c + (t - c) * step / 100 for c, t in zip(current, target))
        if all(contrast_ratio(candidate, bg) >= minimum for bg in (darkest_bg, lightest_bg)):
            return rgb_to_hex(candidate)
    return rgb_to_hex(target)


def semantic_hex(role: str, window: Sequence[int], backgrounds: Sequence[Sequence[int]] = ()) -> str:
    """
    Kolor semantyczny roli dla motywu o kolorze okna ``window``.

    :param role: klucz z :data:`SEMANTIC_COLORS` (``ok``, ``warn``, ``err``, ``info``, ``pog``…).
    :param backgrounds: tła, na których kolor ma być czytelny (``Window``, ``Base``);
        domyślnie samo okno.
    """
    light, dark = SEMANTIC_COLORS[role]
    value = dark if is_dark_rgb(window) else light
    return ensure_contrast(hex_to_rgb(value), list(backgrounds) or [window])
