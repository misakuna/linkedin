"""Reading LinkedIn's HTML email bodies.

What: pulls visible text and links out of an HTML email part.

Why: most LinkedIn notification emails ship a usable text/plain alternative,
but not all of them — `email_accept_invite_single_01` (the "X accepted your
invitation" email) has a plain part containing only the footer, with every
useful word locked in the HTML.

How: stdlib `html.parser`, skipping <style>/<script>/<head> so CSS doesn't end
up in the extracted text. Deliberately not BeautifulSoup: this is forty lines
of well-understood code, and keeping the fragile surface inside this repo means
a LinkedIn markup change is a fix we control rather than a dependency bump.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

# Tags whose contents are markup machinery, never words a human reads.
_SKIP_CONTENT = {"style", "script", "head", "title"}

# Tags that imply a line break in the rendered output.
_BREAKS = {"br", "p", "div", "tr", "td", "li", "h1", "h2", "h3", "h4", "table"}


@dataclass
class HtmlContent:
    """Visible text and the links found in an HTML email body."""

    text: str
    links: list[str] = field(default_factory=list)


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_CONTENT:
            self._skip_depth += 1
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    self.links.append(value)
        if tag in _BREAKS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_CONTENT and self._skip_depth:
            self._skip_depth -= 1
        if tag in _BREAKS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data)

    def result(self) -> str:
        joined = "".join(self._chunks)
        # Collapse runs of spaces, then runs of blank lines, so the output is
        # line-addressable the same way a text/plain part is.
        joined = re.sub(r"[ \t\r\f\v]+", " ", joined)
        joined = re.sub(r"\n\s*\n+", "\n", joined)
        return "\n".join(line.strip() for line in joined.splitlines() if line.strip())


def extract(html: str) -> HtmlContent:
    """Pull visible text and hrefs out of an HTML email body."""
    parser = _Extractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # Malformed markup shouldn't take down an inbox poll; whatever was
        # parsed before the failure is still useful.
        pass
    return HtmlContent(text=parser.result(), links=parser.links)
