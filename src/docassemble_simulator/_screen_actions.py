"""Discover native interview links without evaluating authored text again."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.href: str | None = None
        self.label: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.label = []

    def handle_data(self, data):
        if self.href is not None:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href is not None:
            self.links.append((self.href, " ".join("".join(self.label).split())))
            self.href = None


def _decode(href: str, interview: str | None) -> tuple[dict | None, str | None]:
    try:
        url = urlsplit(href)
        if url.scheme or url.netloc:
            return None, "JavaScript and external links are not supported"
        if url.path not in ("", "/interview", "/dasimulator/flex_interview"):
            return None, "not a supported interview action URL"
        query = parse_qs(url.query, keep_blank_values=True)
        if query.keys() - {"action", "i"} or url.fragment:
            return None, "additional URL navigation parameters are not supported"
        if "i" in query and (len(query["i"]) != 1 or query["i"][0] != interview):
            return None, "action links to a different interview are not supported"
        values = query.get("action", [])
        if len(values) != 1:
            return None, "link must contain one native action payload"
        encoded = values[0]
        payload = json.loads(
            base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
        )
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("action"), str)
            or not payload["action"].strip()
            or not isinstance(payload.get("arguments"), dict)
        ):
            return None, "malformed native action payload"
        return payload, None
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        return None, "malformed native action URL"


def _markdown_html(text: str) -> str:
    """Use docassemble's configured Markdown engine, not another render pipeline.

    Pure description callers without a loaded runtime can still inspect HTML.
    Execution captures the resulting controls while its native context is live.
    """
    try:
        from docassemble.base.functions import this_thread
    except ImportError:
        return text
    markdown = getattr(this_thread, "markdown", None)
    return markdown.reset().convert(text) if markdown is not None else text


def screen_actions(screen: dict) -> list[dict]:
    """Return screen-local controls, preserving native wrappers and arguments."""
    sources = [(key, screen.get(key)) for key in ("question_text", "subquestion_text")]
    sources.extend(
        (field["source"], field["content"])
        for field in screen.get("fields", [])
        if field.get("visible") is True and "content" in field
    )
    actions = []
    for source, text in sources:
        parser = _Links()
        text = str(text or "")
        if source in ("question_text", "subquestion_text") or source.endswith(":note"):
            text = _markdown_html(text)
        parser.feed(text)
        for index, (href, label) in enumerate(parser.links):
            payload, reason = _decode(href, screen.get("interview"))
            identity = json.dumps(
                [
                    screen.get("question_name"),
                    screen.get("sought"),
                    screen.get("orig_sought"),
                    screen.get("interview"),
                    source,
                    index,
                    href,
                ],
                sort_keys=True,
            )
            action = {
                "id": hashlib.sha256(identity.encode()).hexdigest()[:20],
                "label": label,
                "source": source,
            }
            if reason:
                action["unsupported"] = reason
            else:
                action["payload"] = payload
            actions.append(action)
    return actions
