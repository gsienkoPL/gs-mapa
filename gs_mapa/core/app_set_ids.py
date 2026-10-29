"""Hurtowa zmiana identyfikatorów aktów APP spoza Rejestru (przestrzeń nazw, ``lokalnyId``, ``wersjaId``).

Akt, którego nie ma w Rejestrze Urbanistycznym, nie ma jeszcze odwołań z zewnątrz — jego
identyfikatory można poprawić w miejscu. Typowe przypadki: pliki przygotowane z testową albo
cudzą przestrzenią nazw, ``lokalnyId`` z prefiksem innej gminy, ``wersjaId`` z daty eksportu.

Tożsamość obiektu siedzi w pliku GML w czterech miejscach i wszystkie muszą się zgadzać
(reguła ``IDENTYFIKATOR`` walidatora):

1. ``idIIP/Identyfikator`` — ``przestrzenNazw``, ``lokalnyId``, ``wersjaId``;
2. ``gml:identifier`` — ``<codeSpace>/<klasa>/<przestrzeń>/<lokalnyId>[/<wersjaId>]``;
3. ``gml:id`` — ``<przestrzeń z „_” zamiast „/”>_<lokalnyId>[_<wersjaId>]``;
4. odwołania ``xlink:href`` w innych obiektach (ten sam adres co w pkt 2, także bez wersji).

Moduł przepisuje plik tekstowo (wyrażeniami regularnymi), więc układ, wcięcia i reszta treści
zostają bez zmian. Nie zależy od QGIS ani Qt.
"""

from __future__ import annotations

import bisect
import re
import shutil
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from .app_gml import CODE_SPACE
from .app_namespace import parse_namespace

#: Grupy klas obiektów, których ``lokalnyId`` zmienia się razem.
ACT = "akt"
DOCUMENT = "dokument"
DRAWING = "rysunek"
SPATIAL = "obiekt"
GROUPS: Dict[str, FrozenSet[str]] = {
    ACT: frozenset({"AktPlanowaniaPrzestrzennego"}),
    DOCUMENT: frozenset({"DokumentFormalny"}),
    DRAWING: frozenset({"RysunekAktuPlanowaniaPrzestrzennego"}),
    SPATIAL: frozenset({"StrefaPlanistyczna", "ObszarUzupelnieniaZabudowy",
                        "ObszarZabudowySrodmiejskiej",
                        "ObszarStandardowDostepnosciInfrastrukturySpolecznej"}),
}
GROUP_LABELS = {ACT: "akty", DOCUMENT: "dokumenty formalne", DRAWING: "rysunki",
                SPATIAL: "strefy i obszary"}
#: Wszystkie klasy z tożsamością IIP.
KINDS: FrozenSet[str] = frozenset().union(*GROUPS.values())
#: Wszystkie grupy — domyślny zakres zmiany ``lokalnyId``.
ALL_GROUPS: FrozenSet[str] = frozenset(GROUPS)

#: ``lokalnyId`` trafia do ``gml:id`` (typ ``xs:ID`` = NCName), więc dozwolone są litery,
#: cyfry, „_”, „-” i „.”; ukośnik rozbiłby adres ``gml:identifier``.
LOCAL_ID_RE = re.compile(r"^[^\W][\w.\-]*$")
VERSION_RE = re.compile(r"^\d{8}T\d{6}$")

_KIND_ALT = "|".join(sorted(KINDS, key=len, reverse=True))
_OBJECT_TAG = re.compile(r"<(?P<prefix>[\w.\-]+:)?(?P<kind>" + _KIND_ALT + r")(?P<attrs>\s[^>]*?)?/?>")
_GML_ID = re.compile(r'gml:id="(?P<value>[^"]*)"')
_IDENT_BLOCK = re.compile(
    r"<(?P<prefix>(?:[\w.\-]+:)?)Identyfikator\b[^>]*>(?P<body>.*?)</(?P=prefix)Identyfikator>", re.S)
_IDENT_PART = r"<(?P<p>(?:[\w.\-]+:)?){name}\b[^>]*>(?P<value>[^<]*)</(?P=p){name}>"
#: Wszystko, co przepisujemy, jednym przebiegiem (kolejność alternatyw = pierwszeństwo):
#: znacznik obiektu APP (z ``gml:id``), blok ``Identyfikator``, adres tożsamości (``gml:identifier``
#: i ``xlink:href``), pozostałe ``gml:id`` (geometria) i odwołania lokalne ``xlink:href="#…"``.
_REWRITE = re.compile(
    # Wyprzedzenie po pierwszym znaku: bez niego silnik próbuje wszystkich alternatyw na każdej
    # pozycji współrzędnych (plan ogólny 9 MB: 2,5 s zamiast ~0,3 s).
    r"(?=[<hgx])(?:"
    r"(?P<tag><(?P<prefix>[\w.\-]+:)?(?P<kind>" + _KIND_ALT + r")(?P<attrs>\s[^>]*?)?/?>)"
    r"|(?P<ident><(?P<iprefix>(?:[\w.\-]+:)?)Identyfikator\b[^>]*>.*?</(?P=iprefix)Identyfikator>)"
    r"|(?P<uri>" + re.escape(CODE_SPACE) + r"/(?P<ukind>[A-Za-z]+)/(?P<uns>PL\.ZIPPZP\.\d+/\d{2,7}-[A-Za-z]+)"
    r"/(?P<ulocal>[^/\"'<>\s]+)(?:/(?P<uversion>[^/\"'<>\s]+))?)"
    r'|(?P<gid>gml:id="(?P<gvalue>[^"]*)")'
    r'|(?P<lhref>xlink:href="#(?P<hvalue>[^"]*)"))',
    re.S)


@dataclass(frozen=True)
class Identity:
    """Tożsamość obiektu APP: klasa, przestrzeń nazw, ``lokalnyId`` i (bez dokumentów) ``wersjaId``."""

    kind: str
    namespace: str
    local_id: str
    version_id: str = ""

    @property
    def key(self) -> Tuple[str, str, str]:
        """Klucz unikalności w zbiorze: ``(klasa, przestrzeń, lokalnyId)`` — bez wersji."""
        return self.kind, self.namespace, self.local_id

    @property
    def uri(self) -> str:
        """Adres ``gml:identifier`` (ten sam trafia do odwołań ``xlink:href``)."""
        parts = [CODE_SPACE, self.kind, self.namespace, self.local_id]
        if self.version_id:
            parts.append(self.version_id)
        return "/".join(parts)

    @property
    def gml_id(self) -> str:
        """``gml:id`` zgodny z idIIP (jak w plikach Rejestru)."""
        parts = [self.namespace.replace("/", "_"), self.local_id]
        if self.version_id:
            parts.append(self.version_id)
        return "_".join(parts)

    def text(self) -> str:
        """Zapis do podglądu: ``przestrzeń/lokalnyId[/wersjaId]``."""
        return "/".join(part for part in (self.namespace, self.local_id, self.version_id) if part)


@dataclass(frozen=True)
class FileIdentities:
    """Obiekty z tożsamością IIP zapisane w jednym pliku (kolejność jak w pliku)."""

    path: str
    identities: Tuple[Identity, ...] = ()
    error: str = ""


@dataclass
class IdRules:
    """
    Reguły hurtowej zmiany identyfikatorów (puste pole = bez zmiany tej części).

    - ``namespace_from`` → ``namespace_to``: zmiana przestrzeni nazw (cały zbiór, także
      odwołania do obiektów, których nie ma w zmienianych plikach);
    - ``find`` → ``replace`` i ``prefix``: zmiana ``lokalnyId`` obiektów klas z ``groups``;
      prefiks nie jest dodawany drugi raz (ponowne uruchomienie nic nie psuje);
    - ``version``: nowy ``wersjaId`` wszystkich obiektów, które go mają (dokument formalny
      nie ma wersji — zostaje bez zmian).
    """

    namespace_from: str = ""
    namespace_to: str = ""
    find: str = ""
    replace: str = ""
    prefix: str = ""
    version: str = ""
    groups: FrozenSet[str] = ALL_GROUPS

    @property
    def changes_namespace(self) -> bool:
        return bool(self.namespace_from and self.namespace_to and self.namespace_from != self.namespace_to)

    @property
    def changes_local_id(self) -> bool:
        return bool((self.find or self.prefix) and self.groups)

    @property
    def active(self) -> bool:
        return self.changes_namespace or self.changes_local_id or bool(self.version)

    def errors(self) -> List[str]:
        """Błędy w samych regułach (zanim policzymy zmiany)."""
        found = []
        if self.namespace_to and parse_namespace(self.namespace_to) is None:
            found.append(f"Przestrzeń nazw „{self.namespace_to}” nie ma postaci "
                         "PL.ZIPPZP.<numer>/<JPT>-<rodzaj>, np. PL.ZIPPZP.2500/026501-MPZP.")
        if self.namespace_to and not self.namespace_from:
            found.append("Wybierz przestrzeń nazw, którą chcesz zmienić.")
        if self.prefix and not re.match(r"^[\w.\-]+$", self.prefix):
            found.append(f"Prefiks „{self.prefix}” może mieć tylko litery, cyfry, „_”, „-” i „.”.")
        if self.version and not VERSION_RE.match(self.version):
            found.append(f"wersjaId „{self.version}” nie ma postaci RRRRMMDDTGGMMSS, np. 20261029T120000.")
        return found

    def renamed(self, identity: Identity) -> Tuple[str, str]:
        """Nowa ``(przestrzeń, lokalnyId)`` obiektu (bez wersji)."""
        namespace = identity.namespace
        if self.changes_namespace and namespace == self.namespace_from:
            namespace = self.namespace_to
        local_id = identity.local_id
        if self.changes_local_id and any(identity.kind in GROUPS[group] for group in self.groups):
            if self.find:
                local_id = local_id.replace(self.find, self.replace)
            if self.prefix and not local_id.startswith(self.prefix):
                local_id = self.prefix + local_id
        return namespace, local_id

    def apply(self, identity: Identity) -> Identity:
        """Nowa tożsamość obiektu po zmianie."""
        namespace, local_id = self.renamed(identity)
        version = self.version if (self.version and identity.version_id) else identity.version_id
        return replace(identity, namespace=namespace, local_id=local_id, version_id=version)


@dataclass(frozen=True)
class IdChange:
    """Zmiana tożsamości jednego obiektu w jednym pliku."""

    path: str
    old: Identity
    new: Identity


@dataclass
class IdPlan:
    """Lista zmian i powody, dla których nie da się jej zastosować."""

    rules: IdRules
    changes: List[IdChange] = field(default_factory=list)
    #: Błędy blokujące zapis (reguły, niepoprawne identyfikatory, kolizje).
    errors: List[str] = field(default_factory=list)
    #: Uwagi, które nie blokują zapisu.
    warnings: List[str] = field(default_factory=list)
    #: Pliki, w których cokolwiek się zmieni (kolejność jak na liście plików).
    files: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.changes) and not self.errors

    @property
    def objects(self) -> int:
        """Liczba zmienianych obiektów (obiekt współdzielony przez kilka plików liczony raz)."""
        return len({change.old for change in self.changes})


@dataclass
class ApplyResult:
    """Wynik zapisu: zmienione pliki, liczba podmian, kopie i nowe nazwy plików."""

    files: List[str] = field(default_factory=list)
    replacements: int = 0
    backups: List[str] = field(default_factory=list)
    #: stara ścieżka → nowa (plik z nazwą w formacie Rejestru dostaje nową nazwę).
    renamed: Dict[str, str] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)


# --- odczyt -------------------------------------------------------------------------------


def _part(body: str, name: str) -> Optional[re.Match]:
    return re.search(_IDENT_PART.format(name=name), body)


def _read(path) -> str:
    """Treść pliku bez zamiany końców linii (plik zapisany z powrotem ma te same CRLF/LF)."""
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def read_identities(text: str) -> List[Tuple[Identity, str]]:
    """
    Obiekty z tożsamością IIP w treści GML: ``(tożsamość, gml:id)`` w kolejności z pliku.

    Właścicielem bloku ``idIIP`` jest ostatni otwarty obiekt klasy APP przed nim (``idIIP``
    stoi na początku obiektu, przed geometrią z własnymi ``gml:id``).
    """
    found = []
    tags = list(_OBJECT_TAG.finditer(text))
    if not tags:
        return found
    starts = [tag.start() for tag in tags]
    for block in _IDENT_BLOCK.finditer(text):
        index = bisect.bisect_right(starts, block.start()) - 1
        if index < 0:
            continue
        tag = tags[index]
        body = block.group("body")
        namespace, local_id, version = (_part(body, name) for name in ("przestrzenNazw", "lokalnyId", "wersjaId"))
        if namespace is None or local_id is None:
            continue
        gml_id = _GML_ID.search(tag.group("attrs") or "")
        found.append((Identity(tag.group("kind"), namespace.group("value").strip(),
                               local_id.group("value").strip(),
                               version.group("value").strip() if version else ""),
                      gml_id.group("value") if gml_id else ""))
    return found


def read_file(path) -> FileIdentities:
    """Tożsamości obiektów pliku (błąd odczytu w ``error``, nie wyjątek)."""
    try:
        text = _read(path)
    except (OSError, UnicodeDecodeError) as error:
        return FileIdentities(str(path), (), str(error))
    return FileIdentities(str(path), tuple(identity for identity, _ in read_identities(text)))


def read_files(paths: Iterable) -> List[FileIdentities]:
    return [read_file(path) for path in paths]


# --- plan ---------------------------------------------------------------------------------


def plan_changes(targets: Sequence[FileIdentities], rules: IdRules,
                 others: Sequence[FileIdentities] = (),
                 republished: Sequence[str] = ()) -> IdPlan:
    """
    Zmiany tożsamości w plikach ``targets`` i powody, dla których nie wolno ich zapisać.

    :param targets: pliki do zmiany (akty spoza Rejestru).
    :param rules: reguły zmiany.
    :param others: pozostałe pliki katalogu — do wykrycia kolizji z obiektami, które zostają.
    :param republished: pliki z nową wersją aktu opublikowanego w Rejestrze (zmiana przestrzeni
        albo ``lokalnyId`` zrywa ciągłość wersji — uwaga, nie błąd).
    """
    plan = IdPlan(rules=rules)
    plan.errors.extend(rules.errors())
    if not rules.active or plan.errors:
        return plan
    mapping: Dict[Identity, Identity] = {}
    for item in targets:
        if item.error:
            plan.warnings.append(f"{Path(item.path).name}: pominięty — {item.error}")
            continue
        changed = False
        for identity in item.identities:
            new = rules.apply(identity)
            if new == identity:
                continue
            mapping[identity] = new
            plan.changes.append(IdChange(item.path, identity, new))
            changed = True
        if changed:
            plan.files.append(item.path)

    invalid = sorted({change.new.local_id for change in plan.changes
                      if not LOCAL_ID_RE.match(change.new.local_id)})
    if invalid:
        listed = ", ".join(f"„{value}”" for value in invalid[:5]) + ("…" if len(invalid) > 5 else "")
        plan.errors.append(f"Niepoprawny lokalnyId po zmianie: {listed} — dozwolone są litery, cyfry, "
                           "„_”, „-” i „.”, pierwszy znak to litera albo cyfra.")

    # Kolizje: dwa różne obiekty z tym samym nowym kluczem albo nowy klucz zajęty przez obiekt,
    # który zostaje (w zmienianych lub pozostałych plikach).
    new_keys: Dict[Tuple[str, str, str], set] = {}
    for old, new in mapping.items():
        new_keys.setdefault(new.key, set()).add(old.key)
    staying = set()
    for item in list(targets) + list(others):
        for identity in item.identities:
            if identity not in mapping:
                staying.add(identity.key)
    collisions = []
    for key, sources in sorted(new_keys.items()):
        if len(sources) > 1:
            collisions.append(f"{key[0]} {key[1]}/{key[2]} — z {len(sources)} różnych obiektów")
        elif key in staying and key not in sources:
            collisions.append(f"{key[0]} {key[1]}/{key[2]} — identyfikator jest już zajęty")
    if collisions:
        listed = "; ".join(collisions[:3]) + (f" (i {len(collisions) - 3} więcej)" if len(collisions) > 3 else "")
        plan.errors.append(f"Kolizja identyfikatorów po zmianie: {listed}. Zmień reguły, żeby każdy obiekt "
                           "miał własny lokalnyId.")

    if rules.changes_namespace:
        old_kind, new_kind = (parse_namespace(value) for value in (rules.namespace_from, rules.namespace_to))
        if old_kind and new_kind and old_kind[2] != new_kind[2]:
            plan.warnings.append(f"Zmieniasz rodzaj zbioru ({old_kind[2]} → {new_kind[2]}) — sprawdź, czy "
                                 "typ aktu pasuje do nowej przestrzeni nazw.")
        left = sorted({Path(item.path).name for item in others
                       if any(identity.namespace == rules.namespace_from for identity in item.identities)})
        if left:
            listed = ", ".join(left[:3]) + ("…" if len(left) > 3 else "")
            plan.warnings.append(f"W katalogu zostają pliki ze starą przestrzenią nazw ({len(left)}: {listed}) — "
                                 "odwołania do ich obiektów też dostaną nową przestrzeń.")
    touched = sorted({Path(path).name for path in republished if path in plan.files})
    if touched and (rules.changes_namespace or rules.changes_local_id):
        plan.warnings.append(f"{', '.join(touched[:3])}: to nowa wersja aktu z Rejestru — po zmianie "
                             "przestrzeni nazw albo lokalnyId Rejestr nie połączy jej z poprzednią wersją.")
    if rules.version:
        plan.warnings.append("wersjaId zmienia tylko identyfikator — poczatekWersjiObiektu zostaje bez zmian.")
    return plan


# --- zapis --------------------------------------------------------------------------------


def rewrite_text(text: str, rules: IdRules, mapping: Optional[Dict[Identity, Identity]] = None) -> Tuple[str, int]:
    """
    Przepisuje identyfikatory w treści GML według reguł.

    Obiekty zdefiniowane w pliku dostają tożsamość z ``rules.apply``; odwołania do obiektów
    spoza pliku — z ``mapping`` (zmiany w pozostałych plikach zbioru), a gdy ich tam nie ma —
    samą zmianę przestrzeni nazw (cały zbiór przechodzi do nowej przestrzeni).

    :return: nowa treść i liczba podmian.
    """
    mapping = dict(mapping or {})
    defined = read_identities(text)
    for identity, _gml_id in defined:
        mapping.setdefault(identity, rules.apply(identity))
    by_key: Dict[Tuple[str, str, str], Tuple[str, str]] = {}
    for old, new in mapping.items():
        by_key.setdefault(old.key, (new.namespace, new.local_id))
    gml_ids: Dict[str, str] = {}
    for identity, gml_id in defined:
        new = mapping[identity]
        if not gml_id or new == identity:
            continue
        if gml_id == identity.gml_id:
            gml_ids[gml_id] = new.gml_id
        elif rules.changes_namespace and identity.namespace == rules.namespace_from:
            gml_ids[gml_id] = _ns_prefix(gml_id, rules)

    count = 0
    state = {"kind": ""}

    def new_identity(kind: str, namespace: str, local_id: str, version: str) -> Identity:
        old = Identity(kind, namespace, local_id, version)
        if old in mapping:
            return mapping[old]
        if old.key in by_key:
            # Odwołanie do innej wersji obiektu: przestrzeń i lokalnyId jak obiekt, wersja bez zmian.
            namespace, local_id = by_key[old.key]
            return Identity(kind, namespace, local_id, version)
        if rules.changes_namespace and namespace == rules.namespace_from:
            return Identity(kind, rules.namespace_to, local_id, version)
        return old

    def substitute(match: re.Match) -> str:
        nonlocal count
        whole = match.group(0)
        if match.group("tag") is not None:
            state["kind"] = match.group("kind")
            result = _GML_ID.sub(lambda m: f'gml:id="{gml_ids.get(m.group("value"), m.group("value"))}"', whole)
        elif match.group("ident") is not None:
            result = _rewrite_block(whole, state["kind"], new_identity)
        elif match.group("uri") is not None:
            result = new_identity(match.group("ukind"), match.group("uns"), match.group("ulocal"),
                                  match.group("uversion") or "").uri
        elif match.group("gid") is not None:
            value = match.group("gvalue")
            new_value = gml_ids.get(value) or (_ns_prefix(value, rules) if rules.changes_namespace else value)
            result = f'gml:id="{new_value}"'
        else:
            value = match.group("hvalue")
            result = f'xlink:href="#{gml_ids.get(value, value)}"'
        if result != whole:
            count += 1
        return result

    return _REWRITE.sub(substitute, text), count


def _ns_prefix(value: str, rules: IdRules) -> str:
    """``gml:id`` z prefiksem starej przestrzeni nazw → prefiks nowej (pozostałe bez zmian)."""
    old = rules.namespace_from.replace("/", "_") + "_"
    if value.startswith(old):
        return rules.namespace_to.replace("/", "_") + "_" + value[len(old):]
    return value


def _rewrite_block(block: str, kind: str, new_identity) -> str:
    namespace, local_id, version = (_part(block, name) for name in ("przestrzenNazw", "lokalnyId", "wersjaId"))
    if not kind or namespace is None or local_id is None:
        return block
    old = (namespace.group("value").strip(), local_id.group("value").strip(),
           version.group("value").strip() if version else "")
    new = new_identity(kind, *old)
    for name, match, value in (("przestrzenNazw", namespace, new.namespace), ("lokalnyId", local_id, new.local_id),
                               ("wersjaId", version, new.version_id)):
        if match is None or match.group("value").strip() == value:
            continue
        pattern = _IDENT_PART.format(name=name)
        block = re.sub(pattern, lambda m, v=value: m.group(0).replace(
            f">{m.group('value')}<", f">{v}<", 1), block, count=1)
    return block


#: Nazwa pliku w formacie Rejestru: ``PL.ZIPPZP.2500_026501-MPZP_<lokalnyId>_<wersjaId>.gml``.
_REGISTRY_NAME = re.compile(r"^(PL\.ZIPPZP\.\d+_\d{2,7}-[A-Za-z]+)_(.+)_(\d{8}T\d{6})\.gml$", re.I)


def registry_name(path: str, old: Identity, new: Identity) -> Optional[str]:
    """
    Nowa nazwa pliku w formacie Rejestru, gdy nazwa odpowiada staremu identyfikatorowi aktu.

    :return: nowa nazwa (bez katalogu) albo ``None`` — plik zostaje pod swoją nazwą.
    """
    name = Path(path).name
    if name.lower() != f"{old.gml_id}.gml".lower() or not _REGISTRY_NAME.match(name) or not new.version_id:
        return None
    return f"{new.gml_id}.gml"


def apply_plan(plan: IdPlan, backup: bool = True, rename_files: bool = True,
               stamp: Optional[str] = None) -> ApplyResult:
    """
    Zapisuje zmiany planu do plików (najpierw kopia ``<plik>.<znacznik>.bak``).

    :param backup: zostawić kopię każdego zmienianego pliku obok niego.
    :param rename_files: plik z nazwą w formacie Rejestru dostaje nazwę z nowym identyfikatorem aktu.
    :param stamp: znacznik kopii (domyślnie bieżąca data i godzina).
    """
    result = ApplyResult()
    if not plan.ok:
        result.errors.extend(plan.errors or ["Brak zmian do zapisania."])
        return result
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    mapping = {change.old: change.new for change in plan.changes}
    for path in plan.files:
        source = Path(path)
        try:
            text = _read(source)
            new_text, count = rewrite_text(text, plan.rules, mapping)
            if new_text == text:
                continue
            if backup:
                copy = source.with_name(f"{source.name}.{stamp}.bak")
                shutil.copy2(source, copy)
                result.backups.append(str(copy))
            tmp = source.with_name(source.name + ".tmp")
            with open(tmp, "w", encoding="utf-8", newline="") as handle:
                handle.write(new_text)
            tmp.replace(source)
            target = source
            if rename_files:
                act = next((change for change in plan.changes if change.path == path
                            and change.old.kind in GROUPS[ACT]), None)
                name = registry_name(path, act.old, act.new) if act else None
                if name and not source.with_name(name).exists():
                    target = source.rename(source.with_name(name))
                    result.renamed[str(source)] = str(target)
            result.files.append(str(target))
            result.replacements += count
        except (OSError, UnicodeDecodeError) as error:
            result.errors.append(f"{source.name}: {error}")
    return result


def now_version() -> str:
    """``wersjaId`` z bieżącej chwili (UTC), w formacie plików Rejestru."""
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime())
