"""Parser manifestu publicznego dla Rejestru Urbanistycznego."""

import logging
import urllib.parse
from typing import Dict, List, Optional, Set, Any
from ..core.models import ManifestModel, LayerModel

logger = logging.getLogger("GSMapa.ManifestParser")

class ManifestParser:
    """
    Parsuje JSON manifestu, wyodrębniając pliki .pmtiles i normalizując adresy.
    """
    
    ALLOWED_HOST = "gs-mapa-public.s3.waw.io.cloud.ovh.net"
    S3_ROOT = f"https://{ALLOWED_HOST}/"

    def parse(self, data: Dict[str, Any]) -> ManifestModel:
        """
        Główna metoda parsowania manifestu.
        
        :param data: Słownik z treścią JSON manifestu.
        :raises ValueError: Jeśli manifest brakuje wymaganych pól lub zawiera niebezpieczne URL.
        """
        required_fields = ["version", "build_id", "created", "pipeline_version", "generator", "source", "types"]
        for field in required_fields:
            if field not in data:
                raise ValueError(f"Manifest brakuje wymaganego pola: {field}")

        # Wyodrębnianie warstw z zagnieżdżonego obiektu 'types'
        datasets = self._extract_datasets(data.get("types", {}))
        
        # Normalizacja listy typów (klucze z 'types') — wielkie litery, żeby
        # nazwy zgadzały się z listą typów w panelu (DATA_TYPES).
        types_list = [str(name).upper() for name in data["types"].keys()]

        return ManifestModel(
            version=str(data["version"]),
            build_id=str(data["build_id"]),
            created=str(data["created"]),
            pipeline_version=str(data["pipeline_version"]),
            generator=str(data["generator"]),
            source=str(data["source"]),
            types=types_list,
            datasets=datasets,
            acts_url_template=self._extract_acts_template(data.get("akty")),
            acts_index_url=self._extract_acts_index_url(data.get("akty")),
        )

    def _extract_acts_index_url(self, acts_data: Any) -> Optional[str]:
        """
        Adres indeksu aktów z sekcji ``akty`` manifestu (pole ``index_url``).

        Pole jest opcjonalne; niepoprawny adres (inny host, brak HTTPS) jest pomijany —
        panel pokazuje wtedy wyszukiwarkę jako niedostępną.
        """
        if not isinstance(acts_data, dict):
            return None
        url = acts_data.get("index_url")
        if not isinstance(url, str) or not url:
            return None
        try:
            return self._normalize_url(url)
        except ValueError as e:
            logger.warning(f"Niedozwolony adres indeksu aktów: {e}")
            return None

    def _extract_acts_template(self, acts_data: Any) -> Optional[str]:
        """
        Odczytuje szablon adresu plików aktów z sekcji ``akty`` manifestu.

        Sekcja jest opcjonalna; niepoprawna nie blokuje wczytania kafli — wtyczka
        pokazuje wtedy akty bez listy dokumentów.

        :param acts_data: zawartość sekcji ``akty`` (albo ``None``).
        :returns: szablon z ``{act_id}`` albo ``None``.
        """
        if not isinstance(acts_data, dict):
            return None
        template = acts_data.get("url_template")
        if not isinstance(template, str) or "{act_id}" not in template:
            logger.warning("Sekcja 'akty' manifestu bez poprawnego url_template. Pomijam.")
            return None
        try:
            # Host i protokół sprawdzamy na adresie z podstawionym identyfikatorem.
            self._normalize_url(template.replace("{act_id}", "x"))
        except ValueError as e:
            logger.warning(f"Niedozwolony adres plików aktów: {e}")
            return None
        return template

    def _extract_datasets(self, types_data: Dict[str, Any]) -> List[LayerModel]:
        """
        Wyodrębnia archiwa z sekcji 'types' manifestu.

        Manifest może opisywać typ na dwa sposoby:
          * obiekt — ``{"url": ..., "pmtiles": ..., "layers": [...]}`` (format produkcyjny);
          * listę — ``["https://.../archiwum.pmtiles", ...]`` (format starszy).

        :param types_data: zawartość sekcji ``types`` manifestu.
        :returns: lista modeli archiwów.
        """
        extracted_layers: List[LayerModel] = []
        seen_urls: Set[str] = set()

        for type_name, archives in types_data.items():
            if isinstance(archives, dict):
                # Format produkcyjny: typ opisuje jedno archiwum.
                candidates = [archives]
            elif isinstance(archives, list):
                # Format starszy: typ opisuje listę archiwów.
                candidates = archives
            else:
                logger.warning(f"Typ {type_name} ma nieobsługiwany format. Pomijam.")
                continue

            for item in candidates:
                url, sublayers = self._extract_url_and_layers(item)
                if not url:
                    continue

                if not url.endswith(".pmtiles"):
                    continue

                normalized_url = self._normalize_url(url)

                if normalized_url in seen_urls:
                    continue

                seen_urls.add(normalized_url)
                extracted_layers.append(LayerModel(
                    name=self._derive_name(url, type_name),
                    url=normalized_url,
                    layer_type=type_name.upper(),
                    layers=sublayers,
                ))

        return extracted_layers

    def _extract_url_and_layers(self, item: Any) -> tuple:
        """
        Wyciąga adres archiwum i listę podwarstw z pojedynczego wpisu manifestu.

        :param item: wpis archiwum (string, słownik albo lista).
        :returns: para (adres archiwum lub None, lista nazw podwarstw).
        """
        if isinstance(item, str):
            return item, []
        if isinstance(item, dict):
            url = item.get("url") or item.get("pmtiles")
            sublayers = item.get("layers") or []
            if not isinstance(sublayers, list):
                sublayers = []
            return url, [str(name) for name in sublayers]
        return None, []

    def _normalize_url(self, url: str) -> str:
        """
        Rozwiązuje ścieżki względne i weryfikuje hosta.
        """
        # Jeśli URL zaczyna się od 'planowanie/tiles/...', dołączamy korzeń bucketa
        if url.startswith("planowanie/tiles/"):
            url = f"{self.S3_ROOT}{url}"
        
        parsed = urllib.parse.urlparse(url)
        
        # Wymagamy HTTPS
        if parsed.scheme != "https":
            raise ValueError(f"Nieobsługiwany protokół {parsed.scheme} dla URL: {url}. Wymagany HTTPS.")
        
        # Weryfikacja hosta (allowlist)
        if parsed.hostname != self.ALLOWED_HOST:
            raise ValueError(f"Zakazany host {parsed.hostname} dla URL: {url}. Dozwolony tylko {self.ALLOWED_HOST}.")

        return url

    def _derive_name(self, url: str, type_name: str) -> str:
        """Wyodrębnia nazwę archiwum z URL pliku pmtiles.

        :param url: adres archiwum.
        :param type_name: nazwa typu — awaryjnie, gdy nazwa pliku jest pusta.
        :returns: nazwa archiwum, np. ``pog``.
        """
        name = urllib.parse.urlparse(url).path.split("/")[-1]
        if name.endswith(".pmtiles"):
            name = name[: -len(".pmtiles")]
        return name or type_name
