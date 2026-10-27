"""Interfejs do pobierania i parsowania manifestu."""

import logging
import json
from typing import Optional, Callable
from qgis.PyQt.QtCore import QObject, pyqtSignal

from ..core.config import MANIFEST_URL
from ..core.models import ManifestModel
from .manifest_parser import ManifestParser
from .manifest_fetcher import ManifestFetcher

logger = logging.getLogger("GSMapa.Manifest")

class ManifestService(QObject):
    """
    Usługa obsługująca manifest publiczny.
    Zarządza pobieraniem, parsowaniem i cache'owaniem ostatniego poprawnego manifestu.
    """
    # Sygnał informujący o pomyślnym pobraniu i sparsowaniu manifestu
    manifest_updated = pyqtSignal(ManifestModel)
    # Sygnał informujący o błędzie pobierania/parsowania
    fetch_error = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._parser = ManifestParser()
        self._fetcher = ManifestFetcher()
        self._last_valid_manifest: Optional[ManifestModel] = None
        
        # Połączenie sygnałów fetchera
        self._fetcher.finished.connect(self._on_fetch_finished)
        self._fetcher.error.connect(self._on_fetch_error)

    def fetch_manifest(self, url: str = MANIFEST_URL):
        """
        Rozpoczyna proces pobierania i aktualizacji manifestu.
        """
        logger.info(f"Rozpoczynam pobieranie manifestu z: {url}")
        self._fetcher.fetch(url)

    def _on_fetch_finished(self, data: bytes):
        """
        Obsługuje pomyślne pobranie danych.
        """
        try:
            json_text = data.decode("utf-8")
            json_data = json.loads(json_text)
            
            manifest = self._parser.parse(json_data)
            self._last_valid_manifest = manifest
            
            logger.info(f"Manifest pomyślnie zaktualizowany. Build ID: {manifest.build_id}")
            self.manifest_updated.emit(manifest)
            
        except json.JSONDecodeError as e:
            self._handle_error(f"Błąd formatu JSON: {str(e)}")
        except ValueError as e:
            self._handle_error(f"Błąd walidacji manifestu: {str(e)}")
        except Exception as e:
            self._handle_error(f"Nieoczekiwany błąd podczas przetwarzania manifestu: {str(e)}")

    def _on_fetch_error(self, error_msg: str):
        """Obsługuje błędy sieciowe z fetchera."""
        self._handle_error(error_msg)

    def _handle_error(self, error_msg: str):
        """Loguje błąd i emituje sygnał, zachowując ostatni poprawny manifest."""
        logger.error(error_msg)
        self.fetch_error.emit(error_msg)

    @property
    def last_valid_manifest(self) -> Optional[ManifestModel]:
        """Zwraca ostatni poprawny manifest (cache)."""
        return self._last_valid_manifest
