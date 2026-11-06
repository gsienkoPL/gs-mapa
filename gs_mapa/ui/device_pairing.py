"""Okno „Połącz z panelem” — parowanie urządzeniowe z panelem GS Mapa (klucz API).

Domyślna droga jest **automatyczna** (FAST PATH): wtyczka trzyma stały sekret
urządzenia (:meth:`gs_mapa.core.config.GSMapaConfig.ensure_device_secret`) i przy
otwarciu okna — gdy klucza jeszcze nie ma — wysyła ``POST /polacz/urzadzenie`` z
``deviceSecret``. Serwer tworzy/odnajduje konto typu „urządzenie” i klucz wraca
**od razu w odpowiedzi** (``{apiKey, tier, quota, keyId}``) — bez przeglądarki,
bez ``user_code``. Klucz wtyczka zapisuje do :attr:`GSMapaConfig.api_key`
(QSettings) i okno się domyka. Kontrakt serwera: gs-mapa-engine
``docs/to-do/2026-11-06-konta-urzadzen-parowanie.md``.

Droga zapasowa „Przez panel…” to przepływ z potwierdzeniem w przeglądarce:
``POST /polacz/urzadzenie`` (bez sekretu) dostaje ``userCode`` (duży, kopiowalny)
i adres karty potwierdzenia; użytkownik zatwierdza raz w przeglądarce, a wtyczka
pollingiem ``POST /polacz/klucz`` odbiera klucz wygenerowany przez panel. Jeżeli
serwer nie obsługuje jeszcze kont urządzeń (odpowiada grantem zamiast klucza),
połączenie automatyczne samo przechodzi w ten widok.

Polling biegnie w ``QTimer`` (UI płynne, bez blokowania wątku), transport HTTP
jest wstrzyknięty parametrem (wzór :mod:`gs_mapa.core.quota`). Błąd sieci
w pollingu to „próba N” i nasłuch trwa dalej; błąd łączenia automatycznego
kończy się komunikatem inline i dwiema akcjami: „Połącz automatycznie” (ponowi
próbę) i „Przez panel…” (droga z potwierdzeniem). Klucz zapisany → okno nie
uruchamia parowania samo. Sekret urządzenia, treść klucza i sekretny
``deviceCode`` nigdy nie trafiają do logów — do logu trafia tylko stan
(połączono, zapisano).
"""

from __future__ import annotations

import html
import json
import math
import platform
import time
from typing import Any, Optional
from urllib.parse import quote

from qgis.PyQt.QtCore import Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QGuiApplication
from qgis.PyQt.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                                 QPushButton, QToolButton, QVBoxLayout, QWidget)
from qgis.core import Qgis, QgsMessageLog

from ..core import quota
from ..core.config import GSMapaConfig
from ..version import VERSION
from . import theme

#: Domena panelu (jedno miejsce trzyma adres; karta potwierdzenia to ``/polacz``).
PANEL_BASE_URL = "https://panel.gs-mapa.cloud"
DEVICE_URL = PANEL_BASE_URL + "/polacz/urzadzenie"
KEY_URL = PANEL_BASE_URL + "/polacz/klucz"

#: Nazwa urządzenia w zamówieniu — widzi ją użytkownik na karcie potwierdzenia panelu.
DEVICE_NAME = "GS Mapa (wtyczka QGIS)"

#: Wartości zapasowe, gdyby serwer nie podał ``expiresIn``/``interval`` (kontrakt: 600 s).
DEFAULT_EXPIRES_IN_S = 600.0
DEFAULT_INTERVAL_S = 5.0
#: Timeout transportu (polling biegnie w ``QTimer``, a klik nie może wisieć na sieci).
REQUEST_TIMEOUT_S = 10.0

#: Statusy ``POST /polacz/klucz`` — dokładnie jak w kontrakcie serwera.
STATUS_PENDING = "oczekuje"
STATUS_APPROVED = "zatwierdzone"
STATUS_REJECTED = "odrzucone"
STATUS_EXPIRED = "wygasłe"
STATUS_DELIVERED = "dostarczone"

_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}


def _body_json(data: dict) -> bytes:
    """Treść żądania JSON (``deviceCode`` nigdy nie idzie do adresu ani logu)."""
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def _positive_number(value: Any, default: float) -> float:
    """Liczba dodatnia z odpowiedzi serwera albo wartość zapasowa (kontrakt)."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _retry_minutes(value: Any) -> Optional[int]:
    """:attr:``retryAfterSeconds`` jako pełne minuty (w górę, min. 1) albo ``None``."""
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return max(1, math.ceil(seconds / 60))


def _server_reason(data: Any) -> str:
    """:attr:``error`` z treści odpowiedzi serwera jako ``": powód"`` albo pusty."""
    if not isinstance(data, dict):
        return ""
    reason = str(data.get("error", "")).strip()
    return f": {reason}" if reason else ""


class DevicePairingDialog(QDialog):
    """Okno parowania: najpierw automatycznie (sekret → klucz), potem z paneli.

    ``transport`` jest wstrzykiwany parametrem (testy nie robią sieci); ``None``
    → :class:`quota.UrllibTransport` z krótkim timeoutem. Pierwsze pokazanie
    okna bez zapisanego klucza łączy się automatycznie; „Połącz automatycznie”
    ponawia tę próbę, a „Przez panel…” przełącza na przepływ z potwierdzeniem
    w przeglądarce — kod, „Otwórz panel”, polling (:meth:`begin`). Klucz już
    zapisany → okno czeka, parowanie nie uruchamia się samo.
    """

    def __init__(self, config: GSMapaConfig, parent=None, transport=None):
        super().__init__(parent)
        theme.apply_plugin_style(self)
        self._config = config
        self._transport = (transport if transport is not None
                           else quota.UrllibTransport(timeout_s=REQUEST_TIMEOUT_S))
        self._device_code = ""
        self._panel_url = ""
        self._deadline = 0.0
        self._attempts = 0
        self._started = False

        self.setWindowTitle(self.tr("Połącz z panelem"))
        self.setObjectName("GSMapaDevicePairingDialog")
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        intro = QLabel(self.tr("Klucz API dla podkładów wektorowych wydaje panel GS Mapa."))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # --- akcje trybu: automat (główna) i droga z potwierdzeniem (wtórna) ---------------
        choice_row = QWidget(self)
        choice_layout = QHBoxLayout(choice_row)
        choice_layout.setContentsMargins(0, 0, 0, 0)
        self.auto_button = QPushButton(self.tr("Połącz automatycznie"))
        self.auto_button.setToolTip(self.tr("Wysyła stały sekret urządzenia — klucz wraca w odpowiedzi"))
        theme.set_icon(self.auto_button, "panel")
        theme.primary(self.auto_button)
        self.auto_button.clicked.connect(self._begin_auto)
        choice_layout.addWidget(self.auto_button)
        self.panel_button = theme.link_button(QToolButton(), "panel")
        self.panel_button.setText(self.tr("Przez panel…"))
        self.panel_button.setToolTip(self.tr("Pokazuje tryb z potwierdzeniem kodu w przeglądarce"))
        self.panel_button.clicked.connect(self.begin)
        choice_layout.addWidget(self.panel_button)
        choice_layout.addStretch(1)
        layout.addWidget(choice_row)
        self.choice_row = choice_row

        # --- kod połączenia (schowany, dopóki serwer go nie wyda) -------------------------
        code_row = QWidget(self)
        code_layout = QHBoxLayout(code_row)
        code_layout.setContentsMargins(0, 0, 0, 0)
        self.code_label = QLabel()
        self.code_label.setTextFormat(Qt.TextFormat.PlainText)
        self.code_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.code_label.setAccessibleName(self.tr("Kod połączenia"))
        self.code_label.setToolTip(self.tr("Wpisz ten kod na karcie potwierdzenia w panelu"))
        code_layout.addWidget(self.code_label, 1)
        self.copy_button = theme.link_button(QToolButton(), "copy")
        self.copy_button.setText(self.tr("Kopiuj"))
        self.copy_button.setToolTip(self.tr("Kopiuje kod połączenia do schowka"))
        self.copy_button.clicked.connect(self._copy_code)
        code_layout.addWidget(self.copy_button)
        layout.addWidget(code_row)
        self.code_row = code_row

        self.open_panel_button = theme.link_button(QToolButton(), "external")
        self.open_panel_button.setText(self.tr("Otwórz panel"))
        self.open_panel_button.setToolTip(self.tr("Otwiera kartę potwierdzenia w panelu GS Mapa"))
        self.open_panel_button.clicked.connect(self._open_panel)
        layout.addWidget(self.open_panel_button)

        # --- stan inline -----------------------------------------------------------------
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.countdown_label = QLabel()
        self.countdown_label.setWordWrap(True)
        theme.style_muted(self.countdown_label, small=True)
        layout.addWidget(self.countdown_label)

        retry_row = QWidget(self)
        retry_layout = QHBoxLayout(retry_row)
        retry_layout.setContentsMargins(0, 0, 0, 0)
        self.retry_button = QPushButton(self.tr("Spróbuj ponownie"))
        self.retry_button.clicked.connect(self.begin)
        retry_layout.addWidget(self.retry_button)
        retry_layout.addStretch(1)
        layout.addWidget(retry_row)
        self.retry_row = retry_row

        layout.addStretch(1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_once)
        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._update_countdown)
        self.finished.connect(self._stop_polling)

        self._set_idle()
        if (self._config.api_key or "").strip():
            self._set_manual()

    # --- pokazanie / zamknięcie ------------------------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802 (sygnał Qt)
        """Pierwsze pokazanie: brak klucza → łączenie automatyczne, klucz jest → spoczynek."""
        super().showEvent(event)
        if not self._started:
            self._open_once()

    def _open_once(self) -> None:
        """Jedno działanie na pokazanie okna, zależne od zapisanego klucza."""
        if (self._config.api_key or "").strip():
            self._set_manual()
            return
        self._begin_auto()

    def reject(self) -> None:
        self._stop_polling()
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 (sygnał Qt)
        self._stop_polling()
        super().closeEvent(event)

    # --- Stan inline -----------------------------------------------------------------------

    def _set_status(self, text: str, role: str) -> None:
        """Komunikat w roli koloru motywu (``ok``/``err``/``info``/``warn``/``muted``)."""
        theme.set_html(self.status_label, theme.span(html.escape(text), role))

    def _refit(self) -> None:
        """Okno dopasowuje się do treści po dynamicznych stanach (status potrafi się wydłużyć)."""
        self.layout().activate()
        self.adjustSize()

    def _set_idle(self) -> None:
        self.choice_row.setVisible(False)
        self.code_row.setVisible(False)
        self.open_panel_button.setVisible(False)
        self.retry_row.setVisible(False)
        self.countdown_label.setVisible(False)
        self._set_status(self.tr("Łączę automatycznie…"), "muted")

    def _set_manual(self) -> None:
        """Klucz już zapisany: nic nie uruchamia się samo, akcje czekają na klik."""
        self.code_row.setVisible(False)
        self.open_panel_button.setVisible(False)
        self.retry_row.setVisible(False)
        self.countdown_label.setVisible(False)
        self.choice_row.setVisible(True)
        self._set_status(self.tr("Klucz API jest już zapisany — parowanie nie uruchamia się samo."),
                         "muted")
        self._refit()

    def _set_failure(self, message: str) -> None:
        """Zamawianie nie wyszło: komunikat + droga naprawy, bez kodu i bez klucza."""
        self._stop_polling()
        self._device_code = ""
        self.choice_row.setVisible(False)
        self.code_row.setVisible(False)
        self.open_panel_button.setVisible(False)
        self.countdown_label.setVisible(False)
        self.retry_row.setVisible(True)
        self._set_status(message, "err")
        self._refit()

    def _set_auto_failure(self, message: str, role: str) -> None:
        """Automat nie wyszedł: komunikat inline + dwie akcje (ponów albo przez panel)."""
        self._stop_polling()
        self._device_code = ""
        self.code_row.setVisible(False)
        self.open_panel_button.setVisible(False)
        self.countdown_label.setVisible(False)
        self.retry_row.setVisible(False)
        self.choice_row.setVisible(True)
        self._set_status(message, role)
        self._refit()

    # --- zamawianie ------------------------------------------------------------------------

    def begin(self) -> None:
        """Zamawia grant (``POST /polacz/urzadzenie``) i przygotowuje widok czekania.

        Błąd sieci albo odpowiedzi niezgodnej z kontraktem kończy się komunikatem
        inline i „Spróbuj ponownie”; klucza nie ma wtedy nigdzie. To droga
        „Przez panel…” — bez sekretu urządzenia w treści żądania.
        """
        self._stop_polling()
        self._started = True
        self._device_code = ""
        self._panel_url = ""
        self._attempts = 0
        self.choice_row.setVisible(False)
        self._set_status(self.tr("Zamawiam połączenie…"), "muted")
        body = _body_json({"name": DEVICE_NAME, "platform": platform.system(),
                           "version": VERSION})
        try:
            response = self._transport.request("POST", DEVICE_URL, headers=_HEADERS, body=body)
        except Exception as exc:  # transport może podnieść dowolny błąd (wzór core.quota)
            self._set_failure(self.tr("Nie udało się zamówić połączenia z panelem ({0}). "
                                      "Spróbuj ponownie.").format(_reason(exc)))
            return
        if response.status != 200:
            self._set_failure(self.tr("Nie udało się zamówić połączenia z panelem (HTTP {0}). "
                                      "Spróbuj ponownie.").format(response.status))
            return
        try:
            data = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            data = None
        if not isinstance(data, dict) or not str(data.get("deviceCode", "")) \
                or not str(data.get("userCode", "")):
            self._set_failure(self.tr("Nie udało się zamówić połączenia z panelem — nieczytelna "
                                      "odpowiedź. Spróbuj ponownie."))
            return
        self._fill_waiting(data)
        QgsMessageLog.logMessage(self.tr("Zamówiono połączenie z panelem — czekam na potwierdzenie."),
                                 "GS Mapa", Qgis.MessageLevel.Info)

    def _begin_auto(self) -> None:
        """FAST PATH: ``POST /polacz/urzadzenie`` ze stałym sekretem → klucz w odpowiedzi.

        Klucz zapisuje się od razu i okno się domyka — bez przeglądarki i kodu.
        Odpowiedź z grantem (serwer bez kont urządzeń) przechodzi w widok
        potwierdzenia; 429 i inne błędy kończą się komunikatem inline i akcjami
        „Połącz automatycznie” / „Przez panel…”, bez zapisu klucza.
        """
        self._stop_polling()
        self._started = True
        self._device_code = ""
        self._panel_url = ""
        self._attempts = 0
        self.choice_row.setVisible(False)
        self.code_row.setVisible(False)
        self.open_panel_button.setVisible(False)
        self.retry_row.setVisible(False)
        self.countdown_label.setVisible(False)
        self._set_status(self.tr("Łączę automatycznie…"), "muted")
        body = _body_json({"deviceSecret": self._config.ensure_device_secret(),
                           "name": DEVICE_NAME, "platform": platform.system(),
                           "version": Qgis.QGIS_VERSION})
        try:
            response = self._transport.request("POST", DEVICE_URL, headers=_HEADERS, body=body)
        except Exception as exc:  # transport może podnieść dowolny błąd (wzór core.quota)
            self._set_auto_failure(self.tr("Nie udało się połączyć automatycznie ({0}).")
                                   .format(_reason(exc)), "err")
            return
        try:
            data = json.loads(response.body.decode("utf-8")) if response.body else None
        except (UnicodeDecodeError, ValueError):
            data = None
        if response.status == 200 and isinstance(data, dict):
            key = str(data.get("apiKey", "")).strip()
            if key:
                self._approved(data)
                return
            if str(data.get("deviceCode", "")) and str(data.get("userCode", "")):
                # Serwer bez kont urządzeń odpowiada grantem — wchodzimy w widok potwierdzenia.
                self._fill_waiting(data)
                QgsMessageLog.logMessage(self.tr("Połączenie automatyczne nieobsługiwane na "
                                                 "serwerze — czekam na potwierdzenie w panelu."),
                                         "GS Mapa", Qgis.MessageLevel.Info)
                return
        if response.status == 429:
            minutes = _retry_minutes(data.get("retryAfterSeconds") if isinstance(data, dict) else None)
            if minutes is None:
                self._set_auto_failure(self.tr("Limit nowych połączeń z tej sieci — spróbuj "
                                               "ponownie później albo użyj trybu przez panel"), "warn")
            else:
                self._set_auto_failure(self.tr("Limit nowych połączeń z tej sieci — spróbuj "
                                               "ponownie za {0} min albo użyj trybu przez panel")
                                       .format(minutes), "warn")
            return
        self._set_auto_failure(self.tr("Nie udało się połączyć automatycznie (HTTP {0}{1}).")
                               .format(response.status, _server_reason(data)), "err")

    def _fill_waiting(self, data: dict) -> None:
        """Widok czekania: kod, odnośnik do karty potwierdzenia, polling i odliczanie."""
        self.choice_row.setVisible(False)
        self._device_code = str(data["deviceCode"])
        user_code = str(data["userCode"])
        self.code_label.setText(user_code)
        font = self.code_label.font()
        font.setBold(True)
        font.setPointSize(max(1, font.pointSize() + 6))
        self.code_label.setFont(font)
        panel_url = str(data.get("panelUrl", "")).strip()
        if not panel_url:
            panel_url = PANEL_BASE_URL + "/polacz?device=" + quote(user_code, safe="")
        self._panel_url = panel_url
        interval = _positive_number(data.get("interval"), DEFAULT_INTERVAL_S)
        self._deadline = self._now() + _positive_number(data.get("expiresIn"), DEFAULT_EXPIRES_IN_S)
        self.code_row.setVisible(True)
        self.open_panel_button.setVisible(True)
        self.retry_row.setVisible(False)
        self.countdown_label.setVisible(True)
        self._attempts = 0
        self._set_status(self.tr("Czekam na potwierdzenie…"), "info")
        self._poll_timer.start(int(round(interval * 1000)))
        self._clock.start()
        self._update_countdown()
        self._refit()

    # --- czekanie --------------------------------------------------------------------------

    def _now(self) -> float:
        return time.monotonic()

    def _update_countdown(self) -> None:
        """Odliczanie ważności kodu; wygenerowane też przed pierwszym tikem zegara."""
        if not self._device_code:
            return
        remaining = self._deadline - self._now()
        if remaining <= 0:
            self._expired()
            return
        minutes, seconds = divmod(int(remaining), 60)
        self.countdown_label.setText(self.tr("Kod ważny jeszcze {0}:{1:02d}.").format(minutes, seconds))

    def _expired(self) -> None:
        """Kod wygasł (limit czasu albo odpowiedź ``wygasłe``): koniec nasłuchu."""
        self._terminal(self.tr("Wygasło — spróbuj ponownie."), "err", retry=True)

    def _terminal(self, message: str, role: str, retry: bool) -> None:
        self._stop_polling()
        self.countdown_label.setVisible(False)
        self.retry_row.setVisible(retry)
        self._set_status(message, role)

    def _stop_polling(self) -> None:
        self._poll_timer.stop()
        self._clock.stop()

    # --- polling ---------------------------------------------------------------------------

    def _poll_once(self) -> None:
        """Jeden krok pollingowania ``POST /polacz/klucz``.

        Błąd sieci, HTTP != 200 i nieczytelna odpowiedź to „próba N” — nasłuch
        trwa; stany końcowe kontraktu zatrzymują timer. Treść klucza nie jest
        nigdzie drukowana.
        """
        if not self._device_code or not self._poll_timer.isActive():
            return
        if self._now() >= self._deadline:
            self._expired()
            return
        try:
            response = self._transport.request("POST", KEY_URL, headers=_HEADERS,
                                               body=_body_json({"deviceCode": self._device_code}))
        except Exception:  # nasłuch trwa — kolejny tik spróbuje ponownie
            self._attempt_failed()
            return
        if response.status != 200:
            self._attempt_failed()
            return
        try:
            data = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            data = None
        if not isinstance(data, dict):
            self._attempt_failed()
            return
        status = str(data.get("status", ""))
        if status == STATUS_PENDING:
            self._attempts = 0
            self._set_status(self.tr("Czekam na potwierdzenie…"), "info")
        elif status == STATUS_APPROVED:
            self._approved(data)
        elif status == STATUS_REJECTED:
            self._terminal(self.tr("Połączenie odrzucone w panelu — można zamówić ponownie."),
                           "err", retry=True)
        elif status == STATUS_EXPIRED:
            self._expired()
        elif status == STATUS_DELIVERED:
            self._terminal(self.tr("Klucz został już dostarczony — zamknij okno."), "info", retry=False)
        else:
            self._attempt_failed()

    def _attempt_failed(self) -> None:
        """Nieudana próba: komunikat wg kontraktu („próba N”), nasłuch i odliczanie trwają."""
        self._attempts += 1
        self._set_status(self.tr("Nie udało się połączyć z panelem (próba {0}).")
                         .format(self._attempts), "info")

    def _approved(self, data: dict) -> None:
        """Klucz z panelu zapisany do ustawień; okno domyka się z wynikiem kwoty."""
        key = str(data.get("apiKey", "")).strip()
        if not key:
            # Naruszenie kontraktu (zatwierdzone bez klucza): nic nie zapisujemy, można powtórzyć.
            self._terminal(self.tr("Panel nie przekazał klucza — spróbuj ponownie."), "err", retry=True)
            return
        self._config.api_key = key
        quota_value = data.get("quota")
        if quota_value is None or quota_value == "":
            self._set_status(self.tr("Połączono — kwota bez limitu."), "ok")
        else:
            self._set_status(self.tr("Połączono — kwota {0}/mies.").format(quota_value), "ok")
        QgsMessageLog.logMessage(self.tr("Klucz API z panelu zapisany w ustawieniach."),
                                 "GS Mapa", Qgis.MessageLevel.Info)
        self._stop_polling()
        self.accept()

    # --- schowek / przeglądarka ------------------------------------------------------------

    def _copy_code(self) -> None:
        """Kod połączenia do schowka (kod nie jest tajny — podaje go użytkownik)."""
        code = self.code_label.text().strip()
        if not code:
            return
        QGuiApplication.clipboard().setText(code)
        QgsMessageLog.logMessage(self.tr("Kod połączenia skopiowany do schowka."),
                                 "GS Mapa", Qgis.MessageLevel.Info)

    def _open_panel(self) -> None:
        """Karta potwierdzenia w przeglądarce (adres z serwera albo wartość zapasowa)."""
        if self._panel_url:
            QDesktopServices.openUrl(QUrl(self._panel_url))


def _reason(exc: Exception) -> str:
    """Krótka przyczyna do komunikatu: treść błędu albo jego typ."""
    return str(exc) or type(exc).__name__
