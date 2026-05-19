"""Wrap untrusted content in <external_content> envelopes.

The envelope is the canonical signal to the LLM, at every reasoning step,
that the contained text is DATA rather than a directive. Nested closing
tags are defanged so the envelope cannot be forged from within the
payload.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Union


class Source(str, Enum):
    """Source taxonomy from the AP/Stammdaten threat model.

    Surfaces 1-6 from the model plus a cross-cutting WORKER surface used by
    the swarm to mark a specialist's reply when it is read back into the
    orchestrator's prompt on the next inner-loop step.
    """

    OCR = "ocr"
    DATEV = "datev"
    GITHUB = "github"
    SEARCH = "search"
    LANGFUSE = "langfuse"
    GIT = "git"
    ENV = "env"
    WORKER = "worker"


_OPEN_TAG_RE = re.compile(r"<\s*external_content\b[^>]*>", re.IGNORECASE)
_CLOSE_TAG_RE = re.compile(r"<\s*/\s*external_content\s*>", re.IGNORECASE)


def wrap_external(source: Union[Source, str], content: str) -> str:
    """Wrap `content` in an `<external_content source="...">` envelope.

    Any nested `<external_content>` or `</external_content>` tags inside
    `content` are defanged to `_NESTED` variants so an attacker cannot
    close the envelope mid-payload and smuggle directives after it.
    """

    safe = _OPEN_TAG_RE.sub("<external_content_NESTED>", content)
    safe = _CLOSE_TAG_RE.sub("</external_content_NESTED>", safe)
    src = source.value if isinstance(source, Source) else str(source)
    return f'<external_content source="{src}">{safe}</external_content>'


def contains_external_envelope(text: str) -> bool:
    """True iff `text` looks like it already carries an envelope.

    Used by tests and the NodeAdapter to assert that crew outputs are
    wrapped before they cross a trust boundary.
    """

    return bool(_OPEN_TAG_RE.search(text) and _CLOSE_TAG_RE.search(text))
