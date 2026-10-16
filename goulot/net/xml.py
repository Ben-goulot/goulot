"""XML from sources: `defusedxml` only, no DTD, no entities, no external references, size limited
(docs/SECURITY.md, threat 11)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from defusedxml import ElementTree as SafeET

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element  # nosec B405 - type annotation only, parsing uses defusedxml

MAX_XML_BYTES = 10 * 1024 * 1024


class UnsafeXML(ValueError):
    pass


def parse_xml(data: bytes, *, max_bytes: int = MAX_XML_BYTES) -> Element:
    if len(data) > max_bytes:
        raise UnsafeXML("XML document too large")
    try:
        root: Element = SafeET.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except SafeET.ParseError as exc:
        raise UnsafeXML("malformed XML") from exc
    except Exception as exc:  # defusedxml raises DTDForbidden, EntitiesForbidden, ExternalReferenceForbidden
        raise UnsafeXML(f"XML refused: {type(exc).__name__}") from exc
    return root
