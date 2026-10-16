from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from html.parser import HTMLParser

RENDERER_ATTRIBUTES_BY_PLATFORM: dict[
    str, dict[tuple[str, str], str | Callable[[str | None], bool] | None]
] = {
    "gitlab": {
        ("*", "data-sourcepos"): None,
        ("*", "dir"): "auto",
        ("a", "data-canonical-src"): None,
        ("pre", "data-canonical-lang"): None,
        ("pre", "data-lang-params"): None,
        ("pre", "v-pre"): "true",
        ("a", "rel"): "nofollow noreferrer noopener",
        ("a", "target"): "_blank",
        **{
            (f"h{level}", "id"): lambda value: (
                isinstance(value, str) and value.startswith("user-content-")
            )
            for level in range(1, 7)
        },
    },
    "github": {
        ("table", "role"): "table",
        ("pre", "data-meta"): None,
        ("pre", "lang"): None,
        ("a", "rel"): "nofollow",
    },
}


_RENDERER_CLASS_VALUES = {
    "gitlab": {
        ("a", "gfm"),
        ("pre", "code highlight js-syntax-highlight language-plaintext"),
    },
    "github": {("pre", "notranslate"), ("code", "notranslate")},
}


_COMMON_ELEMENTS = frozenset(
    {
        "a",
        "blockquote",
        "code",
        "h1",
        "h2",
        "li",
        "ol",
        "p",
        "pre",
        "strong",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
ELEMENT_VOCABULARIES = {
    "gitlab": _COMMON_ELEMENTS,
    "github": _COMMON_ELEMENTS | {"br"},
}


@dataclass
class _Node:
    tag: str
    attrs: tuple[tuple[str, str | None], ...] = ()
    children: list[_Node | str] = field(default_factory=list)


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.root = _Node("#root")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, tuple(attrs))
        self.stack[-1].children.append(node)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(_Node(tag, tuple(attrs)))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(html.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self.handle_data(html.unescape(f"&#{name};"))

    def handle_comment(self, data: str) -> None:
        self.stack[-1].children.append(_Node("#comment", (), [data]))


def _attrs(node: _Node) -> dict[str, str | None]:
    return dict(node.attrs)


def is_github_mention(node: _Node) -> bool:
    return node.tag == "a" and bool(
        {"user-mention", "team-mention"}
        & set((_attrs(node).get("class") or "").split())
    )


def is_github_reference(node: _Node) -> bool:
    return node.tag == "a" and "issue-link" in (_attrs(node).get("class") or "").split()


def _is_renderer_attribute(
    platform: str, tag: str, name: str, value: str | None
) -> bool:
    if name == "class":
        return (tag, value) in _RENDERER_CLASS_VALUES[platform]
    attributes = RENDERER_ATTRIBUTES_BY_PLATFORM[platform]
    for key in ((tag, name), ("*", name)):
        if key in attributes:
            expected = attributes[key]
            return (
                expected(value)
                if callable(expected)
                else expected is None or expected == value
            )
    return False


def _heading_anchor(node: _Node, parent: _Node | None) -> bool:
    attrs = _attrs(node)
    return (
        parent is not None
        and parent.tag in {f"h{number}" for number in range(1, 7)}
        and parent.children[-1] is node
        and not node.children
        and "anchor" in (attrs.get("class") or "").split()
        and (attrs.get("href") or "").startswith("#")
        and set(attrs) <= {"href", "class", "aria-label", "data-heading-content"}
    )


_CHROME_RULES: tuple[
    tuple[str, str, str, Callable[[_Node, _Node | None], bool]], ...
] = (
    (
        "gitlab",
        "div",
        "unwrap",
        lambda n, p: (
            _attrs(n) == {"class": "gl-relative markdown-code-block js-markdown-code"}
        ),
    ),
    (
        "github",
        "div",
        "unwrap",
        lambda n, p: _attrs(n) == {"class": "highlight highlight-text-adblock"},
    ),
    ("github", "markdown-accessiblity-table", "unwrap", lambda n, p: not n.attrs),
    (
        "gitlab",
        "span",
        "unwrap",
        lambda n, p: (
            _attrs(n).get("class") == "line"
            and re.fullmatch(r"LC[0-9]+", _attrs(n).get("id") or "") is not None
            and set(_attrs(n)) == {"id", "class", "data-lang"}
        ),
    ),
    (
        "gitlab",
        "span",
        "unwrap",
        lambda n, p: (
            _attrs(n).get("data-escaped-char") == ""
            and set(_attrs(n)) == {"data-escaped-char", "data-sourcepos"}
        ),
    ),
    ("gitlab", "copy-code", "drop", lambda n, p: not n.attrs),
    ("gitlab", "insert-code-snippet", "drop", lambda n, p: not n.attrs),
    ("gitlab", "a", "drop", _heading_anchor),
)


def _chrome_action(node: _Node, parent: _Node | None, platform: str) -> str | None:
    return next(
        (
            action
            for scope, tag, action, matches in _CHROME_RULES
            if scope in {"both", platform} and node.tag == tag and matches(node, parent)
        ),
        None,
    )


def _escape_text(text: str) -> str:
    return html.escape(text, quote=False)


def _serialize_parts(
    node: _Node, platform: str, blob_prefix: str | None, parent: _Node | None = None
) -> list[tuple[bool, str]]:
    if node.tag == "#root":
        return _children_parts(node, platform, blob_prefix)
    if node.tag == "#comment":
        data = (
            node.children[0]
            if node.children and isinstance(node.children[0], str)
            else ""
        )
        return [(False, "<!--" + data + "-->")]
    action = _chrome_action(node, parent, platform)
    if action == "drop":
        return []
    if action == "unwrap":
        return _children_parts(node, platform, blob_prefix)
    attrs = _attrs(node)
    if node.tag == "a" and blob_prefix:
        href = attrs.get("href")
        if href == blob_prefix or (href and href.startswith(blob_prefix)):
            attrs["href"] = href[len(blob_prefix) :]
    attrs = {
        key: value
        for key, value in attrs.items()
        if not _is_renderer_attribute(platform, node.tag, key, value)
    }
    rendered_attrs = "".join(
        f" {key}" if value is None else f' {key}="{html.escape(value, quote=True)}"'
        for key, value in sorted(attrs.items())
    )
    if node.tag == "br":
        if rendered_attrs:
            return [(True, " "), (False, f"<br{rendered_attrs}>")]
        return [(True, " ")]
    children = node.children
    if (
        node.tag == "pre"
        and len(children) == 1
        and isinstance(children[0], _Node)
        and children[0].tag == "code"
        and all(
            _is_renderer_attribute(platform, children[0].tag, name, value)
            for name, value in children[0].attrs
        )
    ):
        children = children[0].children
    body = _children_parts(node, platform, blob_prefix, children)
    if node.tag == "pre" and body and body[-1][0]:
        body[-1] = (True, body[-1][1].rstrip(" \t\n\r\f"))
    return [(False, f"<{node.tag}{rendered_attrs}>"), *body, (False, f"</{node.tag}>")]


def _children_parts(
    parent: _Node,
    platform: str,
    blob_prefix: str | None,
    children: Iterable[_Node | str] | None = None,
) -> list[tuple[bool, str]]:
    parts: list[tuple[bool, str]] = []
    for child in parent.children if children is None else children:
        if isinstance(child, _Node):
            parts.extend(_serialize_parts(child, platform, blob_prefix, parent))
        else:
            parts.append((True, _escape_text(child)))
    return parts


def _join_parts(parts: list[tuple[bool, str]], *, trim_root: bool = False) -> str:
    normalized: list[tuple[bool, str]] = []
    for is_text, value in parts:
        if is_text:
            if normalized and normalized[-1][0]:
                normalized[-1] = (
                    True,
                    re.sub(r"[ \t\n\r\f]+", " ", normalized[-1][1] + value),
                )
            elif value:
                normalized.append((True, re.sub(r"[ \t\n\r\f]+", " ", value)))
        else:
            normalized.append((False, value))
    if trim_root and normalized and normalized[0][0]:
        normalized[0] = (True, normalized[0][1].lstrip(" \t\n\r\f"))
    if trim_root and normalized and normalized[-1][0]:
        normalized[-1] = (True, normalized[-1][1].rstrip(" \t\n\r\f"))
    return "".join(value for _, value in normalized if value)


def structure(html_text: str, *, platform: str, blob_prefix: str | None = None) -> str:
    """Return normalized renderer structure while preserving meaningful markup."""
    parser = _TreeParser()
    parser.feed(html_text)
    parser.close()
    if platform not in ELEMENT_VOCABULARIES:
        raise ValueError(f"unknown render platform: {platform}")
    return _join_parts(
        _serialize_parts(parser.root, platform, blob_prefix), trim_root=True
    )


def _walk(node: _Node, parent: _Node | None = None) -> list[tuple[_Node, _Node | None]]:
    result = [(node, parent)]
    for child in node.children:
        if isinstance(child, _Node):
            result.extend(_walk(child, node))
    return result


def check_render(platform: str, html_text: str) -> None:
    """Raise ValueError when a render violates its platform containment rules."""
    if platform not in ELEMENT_VOCABULARIES:
        raise ValueError(f"unknown render platform: {platform}")
    parser = _TreeParser()
    parser.feed(html_text)
    parser.close()
    problems: list[str] = []
    for node, parent in _walk(parser.root):
        if node.tag == "#comment":
            problems.append("comment node")
            continue
        if node.tag == "#root":
            continue
        attrs = _attrs(node)
        if (
            node.tag not in ELEMENT_VOCABULARIES[platform]
            and _chrome_action(node, parent, platform) is None
        ):
            problems.append(f"unknown element <{node.tag}>")
        if platform == "gitlab" and "data-reference-type" in attrs:
            problems.append("GitLab reference link")
        if platform == "github" and is_github_mention(node):
            problems.append("GitHub mention anchor")
    if problems:
        raise ValueError("; ".join(problems))


def _reference_originals(html_text: str) -> list[str]:
    parser = _TreeParser()
    parser.feed(html_text)
    parser.close()
    originals = []
    for node, _ in _walk(parser.root):
        if node.tag == "a" and "data-reference-type" in _attrs(node):
            value = _attrs(node).get("data-original")
            if isinstance(value, str):
                originals.append(value)
    return sorted(originals)


class _TagCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tags: list[tuple[str, tuple[tuple[str, str | None], ...]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, tuple(sorted(attrs))))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, tuple(sorted(attrs))))

    def handle_endtag(self, tag: str) -> None:
        self.tags.append(("/" + tag, ()))


def _skeleton(
    html_text: str, *, platform: str, blob_prefix: str | None = None
) -> tuple[tuple[str, tuple[tuple[str, str | None], ...]], ...]:
    collector = _TagCollector()
    collector.feed(structure(html_text, platform=platform, blob_prefix=blob_prefix))
    collector.close()
    return tuple(collector.tags)


def pair_sha256(
    github_html: str, gitlab_html: str, *, blob_prefix: str | None = None
) -> str:
    pair = (
        structure(github_html, platform="github")
        + "\0"
        + structure(gitlab_html, platform="gitlab", blob_prefix=blob_prefix)
    )
    return hashlib.sha256(pair.encode("utf-8")).hexdigest()
