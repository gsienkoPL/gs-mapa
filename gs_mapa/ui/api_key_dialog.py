"""Okno „Klucz API” — zapis klucza użytkownika i konta kafli (preflight ``/me``).

Klucz generuje panel GS Mapa (https://panel.gs-mapa.cloud/klucze); wtyczka tylko
go zapisuje (:attr:`GSMapaConfig.api_key`) i sprawdza kwotę przed dodaniem
podkładu. Transport preflight jest wstrzykiwany parametrem (testy nie robią sieci).

Sprawdzenie jest synchroniczne (jedno krótkie zapytanie, krótki timeout), bo okno
czeka na wynik; to zwykły klik, nie długie zadanie.
"""

from __future__ import annotations

import html

from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
                                 QLineEdit, QPushButton, QToolButton, QVBoxLayout)
from qgis.core import Qgis, QgsMessageLog

from ..core import quota
from ..core.config import GSMapaConfig
from . import theme
from .device_pairing import DevicePairingDialog

#: Panel, w którym użytkownik generuje i zarządza kluczami.
PANEL_URL = "https://panel.gs-mapa.cloud/klucze"


class ApiKeyDialog(QDialog):
    """Dialog klucza API: maskowane pole + łącze do panelu + „Sprawdź” (kwota).

    ``notice`` — powód otwarcia (odmowa dodania podkładu: brak/klucz zły/kwota);
    pokazuje go ``set_notice`` w roli ``warn`` nad treścią okna.
    """

    def __init__(self, config: GSMapaConfig, parent=None, transport=None, notice: str = ""):
        super().__init__(parent)
        theme.apply_plugin_style(self)
        self._config = config
        self._transport = transport

        self.setWindowTitle(self.tr("Klucz API"))
        self.setObjectName("GSMapaApiKeyDialog")
        # Wynik „Sprawdź” i odnośnik potrzebują kilku wierszy tekstu — bez tego okno zwęża się do pola.
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        # --- parowanie z panelem: klucz bez wklejania (zalecana droga) ---------------------
        pairing_title = QLabel(self.tr("Połącz z panelem (bez wpisywania klucza)"))
        pairing_title.setWordWrap(True)
        layout.addWidget(pairing_title)
        self.pairing_button = QPushButton(self.tr("Połącz z panelem…"))
        self.pairing_button.setToolTip(self.tr("Otwiera okno parowania — połączenie automatyczne "
                                               "albo potwierdzenie raz w przeglądarce"))
        theme.set_icon(self.pairing_button, "panel")
        pairing_row = QHBoxLayout()
        pairing_row.addWidget(self.pairing_button)
        pairing_row.addStretch(1)
        layout.addLayout(pairing_row)
        self._pairing_dialog = None
        self.pairing_button.clicked.connect(self._open_pairing)

        intro = QLabel(self.tr("Podkłady wektorowe GS Mapa wymagają klucza API. Klucz wygenerujesz w panelu:"))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        layout.addWidget(self._panel_link())

        form = QFormLayout()
        field_label = QLabel(self.tr("Klucz API"))
        self.field = QLineEdit()
        self.field.setEchoMode(QLineEdit.EchoMode.Password)
        self.field.setPlaceholderText(self.tr("Wklej klucz API"))
        field_label.setBuddy(self.field)
        form.addRow(field_label, self.field)
        layout.addLayout(form)

        self.check_button = QPushButton(self.tr("Sprawdź"))
        # Preflight /me nie zalicza żądania (nie podnosi kwoty), więc sprawdzanie jest darmowe.
        self.check_button.setToolTip(self.tr("Sprawdza konto kafli dla klucza z pola (/me nie nalicza kwoty)"))
        row = QHBoxLayout()
        row.addWidget(self.check_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.result_label = QLabel()
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)
        self.check_button.clicked.connect(self._on_check)

        layout.addStretch(1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                        | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        save = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        theme.primary(save)
        save.setDefault(True)
        layout.addWidget(self.buttons)

        key = (self._config.api_key or "").strip()
        if key:
            self.field.setText(key)
        self.set_notice(notice)

    # --- powód otwarcia (notice) ----------------------------------------------------------------

    def set_notice(self, text: str) -> None:
        """Uwaga nad treścią okna (powód odmowy); pusty tekst nic nie dodaje."""
        cleaned = (text or "").strip()
        if not cleaned:
            return
        if getattr(self, "_notice_label", None) is None:
            self._notice_label = QLabel()
            self._notice_label.setWordWrap(True)
            self.layout().insertWidget(0, self._notice_label)
        theme.set_html(self._notice_label, theme.span(html.escape(cleaned), "warn", True))

    # --- łącze do panelu --------------------------------------------------------------------------

    def _panel_link(self) -> QToolButton:
        """Odnośnik otwierający panel kluczy w przeglądarce."""
        button = theme.link_button(QToolButton(), "external")
        button.setText(self.tr("Klucze w panelu GS Mapa"))

        def _open() -> None:
            QDesktopServices.openUrl(QUrl(PANEL_URL))

        button.clicked.connect(_open)
        return button

    # --- parowanie z panelem ----------------------------------------------------------------------

    def _open_pairing(self) -> "DevicePairingDialog":
        """Okno „Połącz z panelem” (parowanie urządzeniowe — klucz bez wklejania).

        Okno jest niemodalne i jedno: drugi klik podnosi istniejące okno. Po
        akceptacji (klucz zapisany przez parowanie) pole pokazuje zapisany
        klucz — tak samo jak przy ponownym otwarciu okna.
        """
        existing = self._pairing_dialog
        if existing is not None and not existing.isHidden():
            existing.raise_()
            return existing
        dialog = DevicePairingDialog(self._config, parent=self, transport=self._transport)
        dialog.finished.connect(self._after_pairing)
        dialog.show()
        self._pairing_dialog = dialog
        return dialog

    def _after_pairing(self, result: int) -> None:
        """Okno parowania zamknięte; akceptacja = odświeżenie pola zapisanym kluczem."""
        if result == QDialog.DialogCode.Accepted:
            self.field.setText(self._config.api_key or "")

    # --- sprawdzenie ------------------------------------------------------------------------------

    def _on_check(self) -> None:
        """„Sprawdź”: kwota dla klucza z pola (nie to, co zapisane w konfiguracji)."""
        key = self.field.text().strip()
        self._show_result(quota.check_quota(key, transport=self._transport)
                          if key else quota.QuotaResult(state=quota.EMPTY))

    def _show_result(self, result: quota.QuotaResult) -> None:
        """Wynik preflight inline (rola koloru ok/err/info); czysta metoda — testy bez sieci."""
        if result.limit_exhausted:
            text, role = (self.tr("Wykorzystany limit miesięczny — "
                                  "większa liczba żądań: biuro@gs-mapa.cloud."), "err")
        elif result.state == quota.OK:
            if result.unlimited:
                text, role = self.tr("Klucz poprawny — bez limitu miesięcznego."), "ok"
            else:
                text, role = (
                    self.tr("Klucz poprawny — {0} z {1} żądań (pozostało {2}).")
                    .format(self._value(result.used), result.quota,
                            self._value(result.remaining)), "ok")
        elif result.state == quota.INVALID:
            text, role = self.tr("Klucz niepoprawny — sprawdź klucz w panelu GS Mapa."), "err"
        elif result.state == quota.DISABLED:
            text, role = self.tr("Klucz wyłączony — włącz albo wygeneruj nowy w panelu GS Mapa."), "err"
        elif result.state == quota.EMPTY:
            text, role = self.tr("Wpisz klucz, żeby go sprawdzić."), "muted"
        else:
            reason = f" ({result.error})" if result.error else ""
            text, role = (self.tr("Nie udało się sprawdzić{0} — dodanie podkładu "
                                  "nie będzie zablokowane.").format(reason), "info")
        theme.set_html(self.result_label, theme.span(text, role))

    @staticmethod
    def _value(value) -> str:
        """Kwota do komunikatu: ``None`` = „bez limitu”."""
        return "?" if value is None else str(value)

    # --- zapis ------------------------------------------------------------------------------------

    def save(self) -> None:
        """Zapis klucza do ustawień (pusty = usuwa klucz) i zamknięcie okna."""
        self._config.api_key = self.field.text().strip()
        QgsMessageLog.logMessage(self.tr("Klucz API zapisany."), "GS Mapa", Qgis.MessageLevel.Info)
        self.accept()
