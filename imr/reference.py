"""Image reference parsing: [registry/]name[:tag][@digest].

Grammar (simplified, local store semantics):

    reference  := name [ ":" tag ] [ "@" digest ]
    name       := path-component ("/" path-component)*   (may include host:port)
    tag        := [A-Za-z0-9_][A-Za-z0-9._-]{0,127}
    digest     := algorithm ":" hex

If a digest is present it identifies the target (tag stays informational /
None). Otherwise a missing tag defaults to ``latest``.
"""

from __future__ import annotations

import re

from .digest import parse_digest
from .errors import ReferenceInvalid
from .models import DEFAULT_TAG, ImageReference

_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
_NAME_COMPONENT_RE = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*$")
_HOST_LABEL = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
_HOSTNAME_RE = re.compile(
    rf"^{_HOST_LABEL}(?:\.{_HOST_LABEL})*(?::[0-9]{{1,5}})?$"
)


def _validate_name(name: str) -> None:
    if not name:
        raise ReferenceInvalid("empty image name")
    parts = name.split("/")
    # First component may be a host[:port] (contains . or : or is localhost).
    first = parts[0]
    if "." in first or ":" in first or first == "localhost":
        if not _HOSTNAME_RE.match(first):
            raise ReferenceInvalid(f"invalid registry host: {first!r}")
        rest = parts[1:]
    else:
        rest = parts
    if not rest:
        raise ReferenceInvalid(f"image name has no repository path: {name!r}")
    for component in rest:
        if not component or not _NAME_COMPONENT_RE.match(component):
            raise ReferenceInvalid(f"invalid name component: {component!r}")


def parse_reference(text: str) -> ImageReference:
    """Parse an image reference into name / tag / digest."""
    if not isinstance(text, str) or not text:
        raise ReferenceInvalid("reference must be a non-empty string")
    if any(ch.isspace() for ch in text):
        raise ReferenceInvalid(f"whitespace in reference: {text!r}")

    remainder = text
    digest: str | None = None
    if "@" in remainder:
        remainder, _, digest_part = remainder.rpartition("@")
        if not digest_part:
            raise ReferenceInvalid("empty digest after '@'")
        parse_digest(digest_part)  # raises on unsupported/malformed
        digest = digest_part

    tag: str | None = None
    name = remainder
    last_slash = remainder.rfind("/")
    tail = remainder[last_slash + 1 :] if last_slash >= 0 else remainder
    if ":" in tail:
        name_part, _, tag_part = remainder.rpartition(":")
        if not tag_part:
            raise ReferenceInvalid("empty tag after ':'")
        if not _TAG_RE.match(tag_part):
            raise ReferenceInvalid(f"invalid tag: {tag_part!r}")
        tag = tag_part
        name = name_part

    if not name:
        raise ReferenceInvalid("empty image name")
    _validate_name(name)

    if digest is None and tag is None:
        tag = DEFAULT_TAG

    return ImageReference(name=name, tag=tag, digest=digest)
