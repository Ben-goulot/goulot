"""Neutralize links in any text that came from an article or a model (docs/SECURITY.md, threat 8).

URLs become `hxxps://exemple[.]com/…`, bare domains `exemple[.]com`, e-mail addresses `nom[@]exemple[.]com`. Alerts
carry no link at all: the details are in the app.
"""

from __future__ import annotations

import re

URL = re.compile(r"(?i)\b(?:https?|ftp)://[^\s<>\"']+")
EMAIL = re.compile(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
DOMAIN = re.compile(r"(?i)\b(?:[a-z0-9-]+\.)+(?:[a-z]{2,24})\b")
SCHEMES = {"http": "hxxp", "https": "hxxps", "ftp": "fxp"}


def _dots(text: str) -> str:
    return text.replace(".", "[.]")


def defang(text: str) -> str:
    def url(m: re.Match[str]) -> str:
        scheme, _, rest = m.group(0).partition("://")
        scheme = SCHEMES[scheme.lower()]
        host, slash, path = rest.partition("/")
        return f"{scheme}://{_dots(host)}{slash}{path}"

    text = URL.sub(url, text)
    text = EMAIL.sub(lambda m: _dots(m.group(0).replace("@", "[@]")), text)
    # Bare domains, but not decimals (1.5) nor already-neutralized ones.
    return DOMAIN.sub(lambda m: m.group(0) if "[.]" in m.group(0) else _dots(m.group(0)), text)
