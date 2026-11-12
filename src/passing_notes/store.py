"""The private local store: root resolution, the store-in-repo guard and publication.

Layout (docs/protocol.md, "Store")::

    <root>/messages/<project>/<note id>.md
    <root>/acks/<project>/<note id>--<reader>.md
    <root>/cards/<project>/<lane>.md

Every path is built from ids that ``protocol`` has validated first, so no id can
reach outside the root, and no store folder may be a link (see ``_store_folder``).
Every file is first written to ``<name>.<random hex>.tmp``, created exclusively. The
random part is new for every attempt, so a leftover from an interrupted writer never
blocks a later one. Notes and acks are then renamed to ``<name>`` without ever
replacing an existing file. A lane card is the one file that is replaced: its
temporary file is moved over ``<lane>.md`` in one atomic replace.
"""

from __future__ import annotations

import enum
import os
import secrets
import stat
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Final, TypeVar

from passing_notes import protocol
from passing_notes.protocol import (
    ACK_MAX_BYTES,
    CARD_MAX_BYTES,
    NOTE_MAX_BYTES,
    NOTE_SUFFIX,
    PUBLISH_ATTEMPTS,
    Ack,
    Card,
    Draft,
    Note,
    OversizeError,
    ProtocolError,
)

_Parsed = TypeVar("_Parsed")

ROOT_ENV: Final = "PASSING_NOTES_ROOT"
LOCALAPPDATA_ENV: Final = "LOCALAPPDATA"
DEFAULT_ROOT_NAME: Final = "passing-notes"
MESSAGES_DIR: Final = "messages"
ACKS_DIR: Final = "acks"
CARDS_DIR: Final = "cards"
TMP_SUFFIX: Final = ".tmp"
# The random part of a temporary name: "<final name>.<TMP_HEX_CHARS hex>.tmp".
TMP_HEX_CHARS: Final = 8
GIT_MARKER: Final = ".git"

# Stable error codes (printed as "error: <code>: ...").
STORE_IN_REPO: Final = "store-in-repo"
STORE_LINK: Final = "store-link"
STORE_UNUSABLE: Final = "store-unusable"
PUBLISH_FAILED: Final = "publish-failed"
NOT_FOUND: Final = "not-found"
NOTE_INVALID: Final = "note-invalid"
CARD_INVALID: Final = "card-invalid"

# On Windows a file that another process holds open or is moving can refuse a rename,
# a replace or an open for a moment with a sharing violation (PermissionError). Each
# is tried up to its own number of times, _BUSY_WAIT_SECONDS apart.
_RENAME_BUSY_RETRIES: Final = 3
_BUSY_WAIT_SECONDS: Final = 0.05
# A card is replaced while readers may hold it open, and two lanes' windows may set
# the same card at once, so a busy replace is retried for longer than a busy rename.
REPLACE_BUSY_RETRIES: Final = 20
# The reader's side of that replace: opening a file in the instant it is replaced is
# refused, so a read is tried as often as a replace before it is reported unreadable.
READ_BUSY_RETRIES: Final = 20
_DISPLAY_NAME_MAX: Final = 120
# Diagnostics name the expected file shapes from the constants that define them.
_LEFTOVER_TMP: Final = f"leftover {TMP_SUFFIX} file; never read as {{what}}"
_NOT_A_NOTE_NAME: Final = f"ignored: not named <note id>{NOTE_SUFFIX}"
_NOT_AN_ACK_NAME: Final = f"ignored: not named {protocol.ACK_NAME_SHAPE}"
_NOT_A_CARD_NAME: Final = f"ignored: not named {protocol.CARD_NAME_SHAPE}"
_NOT_A_REGULAR_FILE: Final = "ignored: not a regular file"


class CodedError(Exception):
    """An error with a stable code, printed as ``error: <code>: <message>``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class StoreError(CodedError):
    """The store cannot be used (exit 4)."""


class NotFoundError(CodedError):
    """No valid note or card by that name in the project (exit 3)."""


class NoteNotFoundError(NotFoundError):
    """No valid note with that id in the project (exit 3)."""


class CardNotFoundError(NotFoundError):
    """No valid card for that lane in the project (exit 3)."""


@dataclass(frozen=True)
class Diagnostic:
    """One problem found in the store. ``path`` is root-relative with ``/``."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass(frozen=True)
class InboxEntry:
    note: Note
    acked: bool
    superseded: bool


@dataclass(frozen=True)
class InboxView:
    entries: tuple[InboxEntry, ...]
    diagnostics: tuple[Diagnostic, ...]


class _Outcome(enum.Enum):
    PUBLISHED = "published"
    TMP_EXISTS = "tmp-exists"
    TARGET_EXISTS = "target-exists"


def display_name(name: str) -> str:
    """A file name made safe to print: unsafe characters escaped, length bounded.

    A name read from the folder may hold any character, even a lone surrogate (an
    undecodable name on POSIX, unpaired UTF-16 on Windows); its escape is ASCII.
    """
    text = protocol.escape_unsafe(name)
    if len(text) > _DISPLAY_NAME_MAX:
        text = text[:_DISPLAY_NAME_MAX] + "..."
    return text


# --- Root resolution and the store-in-repo guard -------------------------------------


def resolve_root(environ: Mapping[str, str]) -> Path:
    """``PASSING_NOTES_ROOT`` if set, else ``%LOCALAPPDATA%\\passing-notes``."""
    override = environ.get(ROOT_ENV, "")
    if override:
        if not os.path.isabs(override):
            raise StoreError(STORE_UNUSABLE, f"{ROOT_ENV} must be an absolute path")
        return Path(os.path.abspath(override))
    base = environ.get(LOCALAPPDATA_ENV, "")
    if not base:
        raise StoreError(
            STORE_UNUSABLE,
            f"{LOCALAPPDATA_ENV} is not set; set {ROOT_ENV} to an absolute store folder",
        )
    if not os.path.isabs(base):
        raise StoreError(STORE_UNUSABLE, f"{LOCALAPPDATA_ENV} is not an absolute path")
    return Path(os.path.abspath(base)) / DEFAULT_ROOT_NAME


def find_git_worktree(path: Path) -> Path | None:
    """The nearest folder at or above ``path`` that holds a ``.git`` entry, if any.

    ``.git`` may be a folder (a normal clone) or a file (a worktree or submodule).
    Both the literal path and its symlink-resolved form are checked, and the path
    itself need not exist yet.
    """
    candidates = [Path(os.path.abspath(path))]
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        resolved = None
    if resolved is not None and resolved not in candidates:
        candidates.append(resolved)
    for base in candidates:
        for folder in (base, *base.parents):
            if os.path.lexists(folder / GIT_MARKER):
                return folder
    return None


def is_link_or_reparse_point(path: Path) -> bool:
    """True if ``path`` is a symbolic link, a junction or another reparse point.

    False if it does not exist. Python 3.11 has no ``os.path.isjunction``; on
    Windows ``lstat`` reports a junction (like every reparse point) through the
    ``FILE_ATTRIBUTE_REPARSE_POINT`` bit of ``st_file_attributes``.
    """
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    if sys.platform == "win32":
        return bool(info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    return False


# --- Low-level file helpers ----------------------------------------------------------


def _rename_windows(src: Path, dst: Path) -> None:
    # os.rename maps to MoveFileExW without MOVEFILE_REPLACE_EXISTING: it raises
    # FileExistsError rather than replace dst. A scanner briefly holding the new file
    # can cause a sharing violation (PermissionError), so retry that a few times.
    for attempt in range(_RENAME_BUSY_RETRIES):
        try:
            os.rename(src, dst)
            return
        except FileExistsError:
            raise
        except PermissionError:
            if attempt == _RENAME_BUSY_RETRIES - 1:
                raise
            time.sleep(_BUSY_WAIT_SECONDS)


def link_no_replace(src: Path, dst: Path) -> None:
    """POSIX form: ``os.rename`` would silently replace, so hard-link then unlink.

    ``os.link`` raises FileExistsError when ``dst`` exists, so this never replaces.
    """
    os.link(src, dst)
    try:
        os.unlink(src)
    except OSError:
        pass  # published; the leftover .tmp is reported by inbox diagnostics


def rename_no_replace(src: Path, dst: Path) -> None:
    """Move ``src`` to ``dst``; raise FileExistsError instead of replacing ``dst``."""
    if os.name == "nt":
        _rename_windows(src, dst)
    else:
        link_no_replace(src, dst)


def replace_file(src: Path, dst: Path) -> None:
    """Move ``src`` over ``dst`` in one atomic step, replacing ``dst`` if it exists.

    Only lane cards are ever replaced. On Windows, a reader holding ``dst`` open, a
    concurrent replace of the same card or a scanner holding ``src`` can briefly
    refuse the move with a sharing violation (PermissionError), so that is retried.
    """
    for attempt in range(REPLACE_BUSY_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or attempt == REPLACE_BUSY_RETRIES - 1:
                raise
            time.sleep(_BUSY_WAIT_SECONDS)


def _remove_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _read_bounded(path: Path, limit: int) -> bytes:
    """At most ``limit + 1`` bytes of ``path``.

    On Windows, opening a card in the instant replace_file moves a new card over it is
    refused with a sharing violation (PermissionError), so that is retried, up to
    READ_BUSY_RETRIES tries, as replace_file retries the writer's side. ``open`` reports
    no Windows error code, so the check is the platform, as in replace_file.
    """
    for attempt in range(READ_BUSY_RETRIES):
        try:
            with open(path, "rb") as handle:
                return handle.read(limit + 1)
        except PermissionError:
            if os.name != "nt" or attempt == READ_BUSY_RETRIES - 1:
                raise
            time.sleep(_BUSY_WAIT_SECONDS)
    raise AssertionError("unreachable: the last try returns or raises")


def os_reason(exc: OSError) -> str:
    """The short reason of an OSError, for one-line messages."""
    return exc.strerror or exc.__class__.__name__


def default_now() -> datetime:
    return datetime.now(UTC)


def default_hex() -> str:
    return secrets.token_hex(protocol.HEX_CHARS // 2)


def default_tmp_hex() -> str:
    return secrets.token_hex(TMP_HEX_CHARS // 2)


def temp_path(final: Path) -> Path:
    """A fresh temporary name for publishing ``final``: ``<name>.<random hex>.tmp``."""
    return final.with_name(f"{final.name}.{default_tmp_hex()}{TMP_SUFFIX}")


# --- The store -------------------------------------------------------------------------


class Store:
    """A store rooted at ``root``. Use :meth:`open` to apply the startup checks."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def open(cls, environ: Mapping[str, str] | None = None) -> Store:
        """Resolve the root; refuse one inside a Git working tree, a link or a non-folder."""
        env: Mapping[str, str] = os.environ if environ is None else environ
        root = resolve_root(env)
        repo = find_git_worktree(root)
        if repo is not None:
            raise StoreError(
                STORE_IN_REPO,
                f"store root {root} is inside the Git working tree {repo}; "
                "notes must stay outside repositories",
            )
        opened = cls(root)
        opened._store_folder()  # refuses a root that is a link
        if os.path.lexists(root) and not root.is_dir():
            raise StoreError(STORE_UNUSABLE, f"store root {root} exists but is not a folder")
        return opened

    # Paths. Each validates its ids before building or inspecting anything.

    def _store_folder(self, *parts: str) -> Path:
        """``root/parts...``, refusing any existing level that is a link.

        Reading or writing through a symbolic link, junction or other reparse point
        could reach outside the store, even into a Git working tree. So the root,
        ``messages``, ``acks`` and each project folder must be real folders.
        """
        folder = self.root
        for level in (None, *parts):
            if level is not None:
                folder = folder / level
            try:
                linked = is_link_or_reparse_point(folder)
            except OSError as exc:
                raise StoreError(
                    STORE_UNUSABLE, f"cannot inspect {folder}: {os_reason(exc)}"
                ) from None
            if linked:
                raise StoreError(
                    STORE_LINK,
                    f"store folder {folder} is a symbolic link, junction or other reparse "
                    "point; refusing to read or write through it",
                )
        return folder

    def _project_folder(self, kind: str, project: str) -> Path:
        protocol.check_project_id(project, "project")
        return self._store_folder(kind, project)

    def messages_dir(self, project: str) -> Path:
        return self._project_folder(MESSAGES_DIR, project)

    def acks_dir(self, project: str) -> Path:
        return self._project_folder(ACKS_DIR, project)

    def note_path(self, project: str, note_id: str) -> Path:
        protocol.check_note_id(note_id)
        return self.messages_dir(project) / f"{note_id}{NOTE_SUFFIX}"

    def ack_path(self, project: str, note_id: str, lane: str) -> Path:
        name = protocol.ack_file_name(note_id, lane)
        return self.acks_dir(project) / name

    def cards_dir(self, project: str) -> Path:
        return self._project_folder(CARDS_DIR, project)

    def card_path(self, project: str, lane: str) -> Path:
        name = protocol.card_file_name(lane)
        return self.cards_dir(project) / name

    def _make_project_folder(self, kind: str, project: str) -> Path:
        """Create ``<kind>/<project>`` if missing; links are refused before and after."""
        folder = self._project_folder(kind, project)
        # Default mode: the new folders inherit the parent's permissions (on Windows
        # the %LOCALAPPDATA% ACL). Nothing here ever loosens permissions.
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            raise StoreError(STORE_UNUSABLE, f"cannot create {folder}: {os_reason(exc)}") from None
        return self._project_folder(kind, project)

    def _list(
        self, folder: Path, rel: str, diagnostics: list[Diagnostic]
    ) -> list[os.DirEntry[str]]:
        try:
            with os.scandir(folder) as entries:
                return sorted(entries, key=lambda entry: entry.name)
        except FileNotFoundError:
            return []
        except NotADirectoryError:
            diagnostics.append(Diagnostic(rel, "expected a folder, found a file"))
            return []
        except OSError as exc:
            raise StoreError(STORE_UNUSABLE, f"cannot list {folder}: {os_reason(exc)}") from None

    # Reading.

    def _load(
        self,
        path: Path,
        max_bytes: int,
        parse: Callable[[bytes], _Parsed],
        what: str,
        project: str,
        missing: Callable[[str, str], NotFoundError],
        invalid: str,
    ) -> _Parsed:
        """The valid file at ``path``, parsed; ``missing(code, message)`` otherwise.

        ``what`` names the file in messages. A file that is absent raises it with
        NOT_FOUND; one that is not a regular file, oversize or invalid, with
        ``invalid``. A file that cannot be inspected or read is a StoreError.
        """
        try:
            info = os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            raise missing(NOT_FOUND, f"no {what} in project {project}") from None
        except OSError as exc:
            raise StoreError(STORE_UNUSABLE, f"cannot open {path}: {os_reason(exc)}") from None
        if not stat.S_ISREG(info.st_mode):
            raise missing(invalid, f"{what} is not a regular file")
        if info.st_size > max_bytes:
            raise missing(invalid, f"{what} is oversize ({info.st_size} bytes, limit {max_bytes})")
        try:
            data = _read_bounded(path, max_bytes)
        except OSError as exc:
            raise StoreError(STORE_UNUSABLE, f"cannot read {path}: {os_reason(exc)}") from None
        try:
            return parse(data)
        except ProtocolError as exc:
            raise missing(invalid, f"{what} is invalid: {exc}") from None

    def load_note(self, project: str, note_id: str) -> Note:
        """The valid note ``note_id`` in ``project``; NoteNotFoundError otherwise."""
        path = self.note_path(project, note_id)
        parse = partial(protocol.parse_note_bytes, file_stem=note_id, folder_project=project)
        return self._load(
            path,
            NOTE_MAX_BYTES,
            parse,
            f"note {note_id}",
            project,
            NoteNotFoundError,
            NOTE_INVALID,
        )

    def load_card(self, project: str, lane: str) -> Card:
        """The valid card of ``lane`` in ``project``; CardNotFoundError otherwise."""
        path = self.card_path(project, lane)
        parse = partial(protocol.parse_card_bytes, file_stem=lane, folder_project=project)
        return self._load(
            path,
            CARD_MAX_BYTES,
            parse,
            f"card for lane {lane}",
            project,
            CardNotFoundError,
            CARD_INVALID,
        )

    def scan_notes(self, project: str) -> tuple[list[Note], list[Diagnostic]]:
        """Valid notes in ``messages/<project>/``, plus a diagnostic per other entry."""
        folder = self.messages_dir(project)
        rel_dir = f"{MESSAGES_DIR}/{project}"
        diagnostics: list[Diagnostic] = []
        notes: list[Note] = []
        for entry in self._list(folder, rel_dir, diagnostics):
            rel = f"{rel_dir}/{display_name(entry.name)}"
            if entry.name.endswith(TMP_SUFFIX):
                diagnostics.append(Diagnostic(rel, _LEFTOVER_TMP.format(what="a note")))
                continue
            stem = entry.name.removesuffix(NOTE_SUFFIX)
            if stem == entry.name or not protocol.is_note_id(stem):
                diagnostics.append(Diagnostic(rel, _NOT_A_NOTE_NAME))
                continue
            parse = partial(protocol.parse_note_bytes, file_stem=stem, folder_project=project)
            note = self._read_entry(entry, rel, NOTE_MAX_BYTES, parse, diagnostics)
            if note is not None:
                notes.append(note)
        return notes, diagnostics

    def _read_entry(
        self,
        entry: os.DirEntry[str],
        rel: str,
        max_bytes: int,
        parse: Callable[[bytes], _Parsed],
        diagnostics: list[Diagnostic],
    ) -> _Parsed | None:
        """The listed file ``entry``, parsed; None, with a diagnostic, if it cannot be.

        That is a file that is not a regular file, oversize, unreadable or invalid.
        """
        try:
            if not entry.is_file(follow_symlinks=False):
                diagnostics.append(Diagnostic(rel, _NOT_A_REGULAR_FILE))
                return None
            size = entry.stat(follow_symlinks=False).st_size
            if size > max_bytes:
                diagnostics.append(Diagnostic(rel, f"oversize: {size} bytes (limit {max_bytes})"))
                return None
            data = _read_bounded(Path(entry.path), max_bytes)
        except OSError as exc:
            diagnostics.append(Diagnostic(rel, f"unreadable: {os_reason(exc)}"))
            return None
        try:
            return parse(data)
        except OversizeError as exc:
            diagnostics.append(Diagnostic(rel, f"oversize: {exc}"))
        except ProtocolError as exc:
            diagnostics.append(Diagnostic(rel, f"invalid: {exc}"))
        return None

    def scan_cards(self, project: str) -> tuple[list[Card], list[Diagnostic]]:
        """Valid cards in ``cards/<project>/`` in lane order, plus a diagnostic per other entry."""
        folder = self.cards_dir(project)
        rel_dir = f"{CARDS_DIR}/{project}"
        diagnostics: list[Diagnostic] = []
        cards: list[Card] = []
        for entry in self._list(folder, rel_dir, diagnostics):
            rel = f"{rel_dir}/{display_name(entry.name)}"
            if entry.name.endswith(TMP_SUFFIX):
                diagnostics.append(Diagnostic(rel, _LEFTOVER_TMP.format(what="a card")))
                continue
            try:
                lane = protocol.parse_card_file_name(entry.name)
            except ProtocolError:
                diagnostics.append(Diagnostic(rel, _NOT_A_CARD_NAME))
                continue
            parse = partial(protocol.parse_card_bytes, file_stem=lane, folder_project=project)
            card = self._read_entry(entry, rel, CARD_MAX_BYTES, parse, diagnostics)
            if card is not None:
                cards.append(card)
        return sorted(cards, key=lambda card: card.lane), diagnostics

    def scan_acks(self, project: str) -> tuple[set[tuple[str, str]], list[Diagnostic]]:
        """(note id, reader) keys of every ack file, plus diagnostics.

        An ack's key is its file name: a regular file there means acknowledged, as the
        ``ack`` command also treats it (``_ack_exists``), even when its contents are
        oversize, unreadable or invalid; those are still reported. Anything else at
        that name is ignored and reported, and ``ack`` refuses it.
        """
        folder = self.acks_dir(project)
        rel_dir = f"{ACKS_DIR}/{project}"
        diagnostics: list[Diagnostic] = []
        keys: set[tuple[str, str]] = set()
        for entry in self._list(folder, rel_dir, diagnostics):
            rel = f"{rel_dir}/{display_name(entry.name)}"
            if entry.name.endswith(TMP_SUFFIX):
                diagnostics.append(Diagnostic(rel, _LEFTOVER_TMP.format(what="an ack")))
                continue
            try:
                key = protocol.parse_ack_file_name(entry.name)
            except ProtocolError:
                diagnostics.append(Diagnostic(rel, _NOT_AN_ACK_NAME))
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    diagnostics.append(Diagnostic(rel, _NOT_A_REGULAR_FILE))
                    continue
                keys.add(key)
                size = entry.stat(follow_symlinks=False).st_size
                if size > ACK_MAX_BYTES:
                    diagnostics.append(
                        Diagnostic(rel, f"oversize: {size} bytes (limit {ACK_MAX_BYTES})")
                    )
                    continue
                data = _read_bounded(Path(entry.path), ACK_MAX_BYTES)
            except OSError as exc:
                diagnostics.append(Diagnostic(rel, f"unreadable: {os_reason(exc)}"))
                continue
            try:
                protocol.parse_ack_bytes(data, file_reader=key[1])
            except OversizeError as exc:
                diagnostics.append(Diagnostic(rel, f"oversize: {exc}"))
            except ProtocolError as exc:
                diagnostics.append(Diagnostic(rel, f"invalid: {exc}"))
        return keys, diagnostics

    def scan_flat(self) -> list[Diagnostic]:
        """Files left directly in ``messages/`` or ``acks/`` by the flat pilot layout.

        Only names are listed; such files are never read or dropped.
        """
        diagnostics: list[Diagnostic] = []
        for sub in (MESSAGES_DIR, ACKS_DIR):
            for entry in self._list(self._store_folder(sub), sub, diagnostics):
                try:
                    is_folder = entry.is_dir(follow_symlinks=False)
                except OSError:
                    is_folder = False
                if not is_folder:
                    diagnostics.append(
                        Diagnostic(
                            f"{sub}/{display_name(entry.name)}",
                            f"unmigrated flat-layout file; move it into {sub}/<project>/",
                        )
                    )
        return diagnostics

    def find_idempotent(self, project: str, sender: tuple[str, str], key: str) -> Note | None:
        """The earliest valid note in ``project`` from ``sender`` with ``key``, if any."""
        notes, _ = self.scan_notes(project)
        matches = sorted(
            (n for n in notes if n.sender == sender and n.idempotency_key == key),
            key=lambda n: n.id,
        )
        return matches[0] if matches else None

    def inbox(
        self,
        project: str,
        lane: str,
        *,
        include_acked: bool = False,
        include_superseded: bool = False,
    ) -> InboxView:
        """Notes for ``lane`` in ``project`` and every diagnostic for the project."""
        protocol.check_project_id(project, "project")
        protocol.check_lane_id(lane, "lane")
        diagnostics = self.scan_flat()
        notes, note_diagnostics = self.scan_notes(project)
        acked, ack_diagnostics = self.scan_acks(project)
        supersedes = protocol.resolve_supersedes(notes)
        supersedes_diagnostics = [
            Diagnostic(f"{MESSAGES_DIR}/{project}/{note_id}{NOTE_SUFFIX}", reason)
            for note_id, reason in supersedes.ignored
        ]
        entries: list[InboxEntry] = []
        for note in sorted(notes, key=lambda n: n.id):
            if not protocol.is_addressed_to(note, project, lane):
                continue
            is_acked = (note.id, lane) in acked
            is_superseded = note.id in supersedes.hidden
            if is_acked and not include_acked:
                continue
            if is_superseded and not include_superseded:
                continue
            entries.append(InboxEntry(note, is_acked, is_superseded))
        all_diagnostics = diagnostics + note_diagnostics + ack_diagnostics + supersedes_diagnostics
        return InboxView(tuple(entries), tuple(all_diagnostics))

    # Writing.

    def _write_temp(self, tmp: Path, data: bytes) -> bool:
        """Create ``tmp`` exclusively and write ``data`` to disk; False if ``tmp`` exists."""
        created = False
        try:
            with open(tmp, "xb") as handle:  # exclusive: fails if the .tmp exists
                created = True
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            if not created:
                return False
            _remove_quietly(tmp)
            raise StoreError(STORE_UNUSABLE, f"cannot write {tmp}") from None
        except OSError as exc:
            if created:
                _remove_quietly(tmp)
            raise StoreError(STORE_UNUSABLE, f"cannot write {tmp}: {os_reason(exc)}") from None
        return True

    def _publish(self, tmp: Path, final: Path, data: bytes) -> _Outcome:
        if not self._write_temp(tmp, data):
            return _Outcome.TMP_EXISTS
        try:
            rename_no_replace(tmp, final)
        except FileExistsError:
            _remove_quietly(tmp)
            return _Outcome.TARGET_EXISTS
        except OSError as exc:
            _remove_quietly(tmp)
            raise StoreError(STORE_UNUSABLE, f"cannot publish {final}: {os_reason(exc)}") from None
        return _Outcome.PUBLISHED

    def publish_note(
        self,
        draft: Draft,
        body: str,
        *,
        now: Callable[[], datetime] | None = None,
        hex_source: Callable[[], str] | None = None,
    ) -> Note:
        """Publish a new note; on a name clash pick new hex, up to PUBLISH_ATTEMPTS."""
        hex_source = hex_source or default_hex
        created = (now or default_now)()
        note, data = protocol.make_note(draft, body, created, hex_source())
        folder = self._make_project_folder(MESSAGES_DIR, draft.project)
        for attempt in range(PUBLISH_ATTEMPTS):
            if attempt:
                note, data = protocol.make_note(draft, body, created, hex_source())
            final = folder / f"{note.id}{NOTE_SUFFIX}"
            if self._publish(temp_path(final), final, data) is _Outcome.PUBLISHED:
                return note
        raise StoreError(
            PUBLISH_FAILED,
            f"could not publish the note: {PUBLISH_ATTEMPTS} attempts all clashed "
            "with existing names",
        )

    def _ack_exists(self, path: Path) -> bool:
        """True if the ack file at ``path`` exists; False if nothing is there.

        Only a regular file is an ack, as ``scan_acks`` counts it. Anything else at
        that name (a folder, a link) would never clear the note from the inbox, so it
        is a StoreError rather than "already acknowledged".
        """
        try:
            info = os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError as exc:
            raise StoreError(STORE_UNUSABLE, f"cannot inspect {path}: {os_reason(exc)}") from None
        if stat.S_ISREG(info.st_mode):
            return True
        raise StoreError(
            STORE_UNUSABLE,
            f"{path} is not a regular file, so it is not an ack; remove it to acknowledge",
        )

    def publish_ack(self, project: str, note_id: str, ack: Ack) -> bool:
        """Write the ack. False when it already exists (a successful no-op).

        Each attempt uses a fresh temporary name, so a leftover ``.tmp`` from an
        interrupted ack (or a concurrent one) never blocks this one. Two concurrent
        acks of the same key both try the no-replace rename; exactly one lands.
        Something other than a regular file at the ack's name is a StoreError.
        """
        existing = self.ack_path(project, note_id, ack.reader)
        data = protocol.encode_text(protocol.render_ack(ack), "ack")
        if self._ack_exists(existing):
            return False
        final = self._make_project_folder(ACKS_DIR, project) / existing.name
        for _ in range(PUBLISH_ATTEMPTS):
            outcome = self._publish(temp_path(final), final, data)
            if outcome is _Outcome.PUBLISHED:
                return True
            if outcome is _Outcome.TARGET_EXISTS and self._ack_exists(final):
                return False
            # TMP_EXISTS: this attempt's random temporary name was taken; draw anew.
            # (A clashing entry that has gone again by now is also retried.)
        if self._ack_exists(final):
            return False
        raise StoreError(
            PUBLISH_FAILED,
            f"cannot acknowledge {note_id} as {ack.reader}: {PUBLISH_ATTEMPTS} temporary "
            f"names in {ACKS_DIR}/{project} were all taken",
        )

    def _fresh_temp(self, final: Path, data: bytes) -> Path | None:
        """A new ``<final>.<random hex>.tmp`` holding ``data``; None if every name was taken.

        Each of the PUBLISH_ATTEMPTS attempts draws a fresh random name, so a leftover
        from an interrupted writer, or a concurrent writer's file, never blocks this one.
        """
        for _ in range(PUBLISH_ATTEMPTS):
            tmp = temp_path(final)
            if self._write_temp(tmp, data):
                return tmp
        return None

    def _check_card_target(self, path: Path) -> None:
        """Refuse to replace anything at a card's name but a regular file.

        Only a regular file is a card, as ``load_card`` and ``scan_cards`` read it. A
        folder or a link there is reported by them and left for its owner to remove.
        """
        try:
            info = os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return
        except OSError as exc:
            raise StoreError(STORE_UNUSABLE, f"cannot inspect {path}: {os_reason(exc)}") from None
        if not stat.S_ISREG(info.st_mode):
            raise StoreError(
                STORE_UNUSABLE,
                f"{path} is not a regular file, so it is not a card; remove it to set the card",
            )

    def publish_card(self, card: Card) -> None:
        """Replace the lane's card with ``card``: the one store file that is replaced.

        The card is written to a fresh ``<lane>.md.<random hex>.tmp``, created
        exclusively, and then moved over ``<lane>.md`` in one atomic replace. So a
        reader finds the old card or the new one, never part of one, and of two
        concurrent writers the later replace wins whole. Something other than a
        regular file at the card's name is a StoreError.
        """
        data = protocol.render_card_bytes(card)
        name = protocol.card_file_name(card.lane)
        final = self._make_project_folder(CARDS_DIR, card.project) / name
        self._check_card_target(final)
        tmp = self._fresh_temp(final, data)
        if tmp is None:
            raise StoreError(
                PUBLISH_FAILED,
                f"cannot set the card of {card.lane}: {PUBLISH_ATTEMPTS} temporary names in "
                f"{CARDS_DIR}/{card.project} were all taken",
            )
        try:
            replace_file(tmp, final)
        except OSError as exc:
            _remove_quietly(tmp)
            raise StoreError(STORE_UNUSABLE, f"cannot replace {final}: {os_reason(exc)}") from None
