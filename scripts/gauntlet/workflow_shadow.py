"""Refuse local definitions that can replace the plugin's named pipeline."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from gauntlet.cli import CliError

PIPELINE_NAME = "code-gauntlet:code-gauntlet-pipeline"
WORKFLOW_SCRIPT_CAP = 524_288
_SPACE = re.compile(
    r"(?:[\s\ufeff]|//[^\n\r\u2028\u2029]*(?:[\n\r\u2028\u2029]|$)|/\*.*?\*/)*",
    re.DOTALL,
)
_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?")
_ESCAPES = dict(zip("nrtbfv", "\n\r\t\b\f\v", strict=True))
_ASCII_DIGITS = frozenset("0123456789")
_LINE_TERMINATOR = re.compile(r"[\n\r\u2028\u2029]")


class MetadataError(ValueError):
    pass


class NotWorkflow(ValueError):
    """An explicit host rejection, never unsupported or partially parsed syntax."""


class LiteralMetadata:
    """Read a conservative subset of the host's static metadata, without executing JS."""

    def __init__(self, source: str):
        self.source = source
        self.offset = 0

    def space(self) -> None:
        match = _SPACE.match(self.source, self.offset)
        assert match is not None
        self.offset = match.end()

    def take(self, token: str) -> bool:
        self.space()
        if not self.source.startswith(token, self.offset):
            return False
        self.offset += len(token)
        return True

    def require(self, token: str) -> None:
        if not self.take(token):
            raise MetadataError("unsupported or incomplete literal metadata")

    def identifier(self) -> str:
        self.space()
        match = _IDENTIFIER.match(self.source, self.offset)
        if match is None:
            raise MetadataError("expected a literal metadata key")
        self.offset = match.end()
        if self.source[self.offset : self.offset + 1] == "\\":
            raise MetadataError("escaped identifier needs the host parser")
        return match[0]

    def string(self) -> str:
        self.space()
        quote = self.source[self.offset]
        self.offset += 1
        value = ""
        while self.offset < len(self.source):
            char = self.source[self.offset]
            self.offset += 1
            if char == quote:
                return value
            if char == "\\":
                if self.offset == len(self.source):
                    break
                char = self.source[self.offset]
                self.offset += 1
                if char in "xu":
                    size = 2 if char == "x" else 4
                    braced = (
                        char == "u"
                        and self.source[self.offset : self.offset + 1] == "{"
                    )
                    if braced:
                        self.offset += 1
                        end = self.source.find("}", self.offset)
                        if end < 0:
                            raise NotWorkflow("unterminated string escape")
                    else:
                        end = self.offset + size
                    digits = self.source[self.offset : end]
                    if not re.fullmatch(r"[0-9a-fA-F]+", digits):
                        raise NotWorkflow("invalid string escape")
                    point = int(digits, 16)
                    if point > 0x10FFFF:
                        raise NotWorkflow("invalid Unicode code point")
                    value += chr(point)
                    self.offset = end + braced
                elif (
                    char == "0"
                    and self.source[self.offset : self.offset + 1] not in _ASCII_DIGITS
                ):
                    value += "\0"
                elif char in _ASCII_DIGITS:
                    raise NotWorkflow("numeric string escape is invalid in a module")
                elif char in "\n\r\u2028\u2029":
                    if (
                        char == "\r"
                        and self.source[self.offset : self.offset + 1] == "\n"
                    ):
                        self.offset += 1
                else:
                    value += _ESCAPES.get(char, char)
            elif (
                quote == "`"
                and char == "$"
                and self.source[self.offset : self.offset + 1] == "{"
            ):
                raise NotWorkflow("template interpolation is not static")
            elif char in "\n\r" and quote != "`":
                raise NotWorkflow("newline in string")
            else:
                value += char
        raise NotWorkflow("unterminated string")

    def value(self) -> object:
        self.space()
        char = self.source[self.offset : self.offset + 1]
        if char and char in "\"'`":
            return self.string()
        if self.take("{"):
            obj: dict[str, object] = {}
            if self.take("}"):
                return obj
            while True:
                self.space()
                char = self.source[self.offset : self.offset + 1]
                if char == "[" or self.source.startswith("...", self.offset):
                    raise NotWorkflow("computed and spread metadata is not literal")
                key = self.string() if char and char in "\"'" else self.identifier()
                if key in {"__proto__", "constructor", "prototype"}:
                    raise NotWorkflow("reserved metadata key")
                self.require(":")
                obj[key] = self.value()
                if self.take("}"):
                    return obj
                self.require(",")
                if self.take("}"):
                    return obj
        if self.take("["):
            items: list[object] = []
            if self.take("]"):
                return items
            while True:
                items.append(self.value())
                if self.take("]"):
                    return items
                self.require(",")
                if self.take("]"):
                    return items
        if char == "+":
            raise NotWorkflow("positive unary expressions are not literal metadata")
        match = _NUMBER.match(self.source, self.offset)
        if match:
            self.offset = match.end()
            return float(match[0])
        keyword = self.identifier()
        if keyword not in {"true", "false", "null"}:
            raise NotWorkflow("metadata must contain only literals")
        return {"true": True, "false": False, "null": None}[keyword]


def workflow_name(source: str) -> str | None:
    parser = LiteralMetadata(source)
    if source.startswith("#!"):
        terminator = _LINE_TERMINATOR.search(source)
        if terminator is None:
            return None
        parser.offset = terminator.start()
    for word in ("export", "const", "meta"):
        parser.space()
        char = source[parser.offset : parser.offset + 1]
        prefix = _IDENTIFIER.match(source, parser.offset)
        if prefix and not word.startswith(prefix[0]):
            return None
        if not char or (
            char != "\\" and _IDENTIFIER.match(source, parser.offset) is None
        ):
            return None
        if parser.identifier() != word:
            return None
    if not parser.take("="):
        return None
    metadata = parser.value()
    end = parser.offset
    if not parser.take(";") and parser.offset != len(source):
        char = source[parser.offset]
        # These tokens extend the initializer or add another declarator, so the
        # host's AST fallback cannot see one pure literal meta declaration.
        newline = _LINE_TERMINATOR.search(source[end : parser.offset])
        prefix_update = newline and source[parser.offset : parser.offset + 2] in {
            "++",
            "--",
        }
        if (char in ".,([`+-*/%&|^<>=?:" and not prefix_update) or source.startswith(
            "!=", parser.offset
        ):
            return None
        if not newline:
            return None
        next_word = _IDENTIFIER.match(source, parser.offset)
        if next_word and next_word[0] in {"in", "instanceof"}:
            following = source[next_word.end() : next_word.end() + 1]
            if following and (following == "\\" or ord(following) > 127):
                raise MetadataError("ambiguous declaration boundary")
            return None
    if not isinstance(metadata, dict):
        return None
    for key in ("name", "description"):
        if not isinstance(metadata.get(key), str) or not metadata[key]:
            return None
    return str(metadata["name"])


def workflow_directories(cwd: Path, config_dir: Path) -> list[Path]:
    # A superset avoids trusting repository-controlled git boundaries or trying to
    # infer the host's launch options and setting-source filters from a child shell.
    roots = [cwd, *cwd.parents]
    directories = [root / ".claude" / "workflows" for root in roots]
    directories.append(config_dir / "workflows")
    return list(dict.fromkeys(directories))


def check_directory(directory: Path) -> None:
    try:
        directory.stat()
    except FileNotFoundError:
        return
    for path in sorted(directory.iterdir()):
        if not path.name.endswith(".js"):
            continue
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        # The host rejects file bytes before decoding; its later UTF-16 cap is
        # not the registration limit for local workflow files.
        if not stat.S_ISREG(info.st_mode) or info.st_size > WORKFLOW_SCRIPT_CAP:
            continue
        try:
            raw = path.read_bytes()
            # Registration uses exact string equality and accepts only literals;
            # without either spelling, no string in this file can name the pipeline.
            if PIPELINE_NAME.encode() not in raw and b"\\" not in raw:
                continue
            name = workflow_name(
                raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
            )
        except NotWorkflow:
            continue
        except (OSError, ValueError, IndexError, RecursionError) as exc:
            raise CliError(
                f"{path}: cannot parse workflow metadata; possible pipeline shadow"
            ) from exc
        if name == PIPELINE_NAME:
            raise CliError(
                f"{path}: declares {PIPELINE_NAME} and shadows the plugin pipeline"
            )


def check_workflow_shadow() -> None:
    cwd = Path.cwd()
    config_dir = Path(
        os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
    ).absolute()
    directories = workflow_directories(cwd, config_dir)
    # The host resolves paths lexically; Bash may report a physical cwd instead.
    logical = os.environ.get("PWD")
    if logical and os.path.isabs(logical):
        try:
            if os.path.samefile(logical, cwd):
                directories.extend(workflow_directories(Path(logical), config_dir))
        except (OSError, ValueError):
            # PWD is only a hint: an unverified alias cannot add a workflow source.
            pass
    for directory in dict.fromkeys(directories):
        try:
            check_directory(directory)
        except (OSError, UnicodeError) as exc:
            raise CliError(
                f"{directory}: cannot inspect workflows; possible pipeline shadow"
            ) from exc
