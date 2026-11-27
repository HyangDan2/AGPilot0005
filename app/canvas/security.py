"""Canvas security helpers (WO-C14).

Responsibilities
----------------
* HTML preview sanitization with a strict allowlist (no script, no event handlers, no remote loads).
* The sandbox/CSP policy the frontend must apply when it renders a preview.
* Title and identifier hygiene (control characters, length, path-ish strings).
* A scope-override guard: a tool argument, patch or AI proposal may never carry
  ``tenant_id`` / ``owner_id`` / ``service_id`` / workspace access fields. Those come only from the
  verified ``ExecutionScope``.
* A conservative token estimate for the AI context budget (no tokenizer dependency is added).

Prompt-injection boundary: artifact content is data. ``edit_envelope`` (see ``app.canvas.editing``)
labels it as untrusted content and this module keeps scope keys out of every request payload, so
text inside an artifact cannot grant itself another tenant or workspace.
"""
from __future__ import annotations

import re
import unicodedata
from html import escape
from html.parser import HTMLParser
from typing import Any, Dict, List, Mapping, Set, Tuple

from app.canvas.errors import CanvasError, CanvasErrorCode

# --------------------------------------------------------------------------- HTML preview

#: Rendered (content kept) allowed tags.
ALLOWED_TAGS: Set[str] = {
    "p", "div", "span", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col",
    "pre", "code", "kbd", "samp", "blockquote", "q", "cite",
    "strong", "b", "em", "i", "u", "s", "del", "ins", "sub", "sup", "small", "mark",
    "figure", "figcaption", "section", "article", "header", "footer", "details", "summary",
    "img", "a",
}

VOID_TAGS: Set[str] = {"br", "hr", "img", "col"}

#: Dropped together with everything inside them.
DROP_WITH_CONTENT: Set[str] = {
    "script", "style", "iframe", "object", "embed", "applet", "frame", "frameset",
    "link", "meta", "base", "form", "input", "textarea", "select", "option", "button",
    "svg", "math", "template", "video", "audio", "source", "track", "canvas", "portal",
    "noscript",
}

ALLOWED_ATTRS: Set[str] = {
    "class", "title", "lang", "dir", "colspan", "rowspan", "span", "start", "reversed",
    "alt", "width", "height", "datetime", "cite", "open", "scope", "headers",
}

SAFE_DATA_IMAGE = re.compile(r"^data:image/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/=\s]+$", re.IGNORECASE)
SAFE_LINK = re.compile(r"^(https?://|mailto:|#|/(?!/))", re.IGNORECASE)

PREVIEW_CSP = (
    "default-src 'none'; img-src data:; style-src 'none'; script-src 'none'; "
    "connect-src 'none'; font-src 'none'; frame-src 'none'; media-src 'none'; "
    "form-action 'none'; base-uri 'none'; navigate-to 'none'"
)
PREVIEW_IFRAME_SANDBOX = "allow-same-origin"  # no allow-scripts, no allow-forms, no top navigation


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.stack: List[str] = []
        self.skip_depth = 0
        self.skip_tag: str | None = None
        self.removed_tags: List[str] = []
        self.blocked_attributes: List[str] = []
        self.blocked_urls: List[str] = []

    # -- helpers ---------------------------------------------------------
    def _note_removed(self, tag: str) -> None:
        if tag not in self.removed_tags:
            self.removed_tags.append(tag)

    def _clean_attrs(self, tag: str, attrs: List[Tuple[str, str | None]]) -> List[Tuple[str, str]]:
        cleaned: List[Tuple[str, str]] = []
        for raw_name, raw_value in attrs:
            name = (raw_name or "").strip().lower()
            value = raw_value if raw_value is not None else ""
            if not name:
                continue
            if name.startswith("on"):
                self.blocked_attributes.append(name)
                continue
            if name in ("style", "srcset", "formaction", "xlink:href", "background", "poster"):
                self.blocked_attributes.append(name)
                continue
            if name not in ALLOWED_ATTRS and name not in ("href", "src"):
                self.blocked_attributes.append(name)
                continue
            if name in ("href", "src"):
                candidate = value.strip()
                lowered = candidate.lower().replace("\t", "").replace("\n", "").replace("\r", "")
                if lowered.startswith(("javascript:", "vbscript:", "data:")) and not (
                    name == "src" and SAFE_DATA_IMAGE.match(lowered)
                ):
                    self.blocked_urls.append(name)
                    continue
                if name == "src" and not SAFE_DATA_IMAGE.match(lowered):
                    # remote image / any other resource would be an outbound network request
                    self.blocked_urls.append(name)
                    continue
                if name == "href":
                    # A backslash is normalised to "/" by browsers, so "\\\\evil.example" and
                    # "\\/evil.example" would become a protocol-relative external link.
                    if "\\" in candidate or not SAFE_LINK.match(candidate):
                        self.blocked_urls.append(name)
                        continue
                    cleaned.append(("href", candidate))
                    continue
                cleaned.append((name, candidate))
                continue
            cleaned.append((name, value))
        return cleaned

    # -- parser callbacks ------------------------------------------------
    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if self.skip_depth:
            # Audit fidelity: even inside a dropped subtree, a nested active element is recorded
            # (e.g. the <input> inside a dropped <form>) so warnings show everything removed.
            if tag in DROP_WITH_CONTENT:
                self._note_removed(tag)
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        if tag in DROP_WITH_CONTENT:
            self._note_removed(tag)
            self.skip_depth = 1
            self.skip_tag = tag
            return
        if tag not in ALLOWED_TAGS:
            self._note_removed(tag)
            return  # tag dropped, inner text is still kept (no skip depth)
        safe_attrs = self._clean_attrs(tag, attrs)
        rendered = "".join(f' {name}="{escape(value, quote=True)}"' for name, value in safe_attrs)
        if tag == "a":
            rendered += ' rel="noopener noreferrer nofollow" target="_blank"'
        if tag == "img":
            rendered += ' loading="lazy"'
        self.out.append(f"<{tag}{rendered}>")
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS and self.stack and self.stack[-1] == tag.lower():
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.skip_depth:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth == 0:
                    self.skip_tag = None
            return
        if tag in VOID_TAGS or tag not in ALLOWED_TAGS:
            return
        if tag in self.stack:
            while self.stack:
                open_tag = self.stack.pop()
                self.out.append(f"</{open_tag}>")
                if open_tag == tag:
                    break

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        self.out.append(escape(data, quote=False))

    def handle_comment(self, data: str) -> None:
        # comments can hide conditional-compilation scripts in old IE and confuse reviewers
        return

    def result(self) -> str:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def sanitize_html_for_preview(html: str) -> Dict[str, Any]:
    """Return sanitized HTML plus what was removed, for audit and UI warnings."""
    parser = _Sanitizer()
    parser.feed(html)
    sanitized = parser.result()
    return {
        "html": sanitized,
        "removed_tags": parser.removed_tags,
        "blocked_attributes": sorted(set(parser.blocked_attributes)),
        "blocked_urls": sorted(set(parser.blocked_urls)),
        "changed": sanitized != html,
    }


def preview_policy() -> Dict[str, Any]:
    return {
        "iframe_sandbox": PREVIEW_IFRAME_SANDBOX,
        "allow_scripts": False,
        "allow_forms": False,
        "allow_network": False,
        "allow_same_origin_scripts": False,
        "csp": PREVIEW_CSP,
        "notes": (
            "Renders inside an iframe with the sandbox attribute above and the CSP applied by the "
            "serving document. Scripts, event handlers, remote resources and form posts are removed "
            "by the backend sanitizer as well, so a sanitizer bypass still cannot execute."
        ),
    }


# --------------------------------------------------------------------------- titles / ids

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_TITLE_FORBIDDEN = re.compile(r"[\\/\x00]")
_ID_OK = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def sanitize_title(title: str, *, max_chars: int = 200) -> str:
    cleaned = unicodedata.normalize("NFC", (title or "")).strip()
    cleaned = _CONTROL_CHARS.sub(" ", cleaned)
    cleaned = _TITLE_FORBIDDEN.sub("-", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        raise CanvasError(CanvasErrorCode.INVALID_REQUEST, "title must not be empty")
    if len(cleaned) > max_chars:
        raise CanvasError(
            CanvasErrorCode.INVALID_REQUEST,
            "title is too long",
            context={"length": len(cleaned), "max_chars": max_chars},
        )
    return cleaned


def validate_identifier(value: str, field: str) -> str:
    cleaned = (value or "").strip()
    if not _ID_OK.match(cleaned):
        raise CanvasError(
            CanvasErrorCode.INVALID_REQUEST,
            f"{field} has an invalid format",
            context={"field": field},
        )
    return cleaned


def validate_artifact_id(value: str) -> str:
    return validate_identifier(value, "artifact_id")


# --------------------------------------------------------------------------- scope guard

SCOPE_OVERRIDE_KEYS = frozenset(
    {
        "tenant_id", "owner_id", "service_id", "scope", "execution_scope",
        "workspace_access", "access_level", "is_admin", "role",
    }
)


def reject_scope_override(payload: Mapping[str, Any], *, source: str) -> None:
    """Refuse payloads that try to carry identity/authorization fields.

    Applied to tool arguments and AI edit requests so an injected instruction cannot widen its own
    access. ``workspace_id`` is *not* in the list: linking an artifact to a workspace is a legitimate
    request that is still checked against the workspace provider.
    """
    offending = sorted(key for key in payload.keys() if str(key).lower() in SCOPE_OVERRIDE_KEYS)
    if offending:
        raise CanvasError(
            CanvasErrorCode.FORBIDDEN,
            "identity or authorization fields are not accepted from the client",
            context={"source": source, "fields": offending},
        )


# --------------------------------------------------------------------------- budget helpers

def estimate_tokens(text: str) -> int:
    """Conservative estimate without a tokenizer dependency (Korean is ~1 token/char).

    Dividing by 2.5 keeps the budget safe for mixed Korean/English text; over-estimating only costs
    a smaller excerpt, never a budget overflow at the provider.
    """
    if not text:
        return 0
    return max(1, int(len(text) / 2.5) + 1)
