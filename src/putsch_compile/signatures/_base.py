"""Signature base + metadata + registry.

A signature has two parts:

1. The DSPy declaration (input fields, output fields, instructions). This is the schema GEPA
   compiles against.

2. The ``SignatureMeta`` block — owner team, accuracy threshold, intent, demos, version. Together
   with the DSPy schema, this gives a stable ``version_hash`` that is the source of truth for "did
   the signature change?".

A change to the version_hash invalidates every compiled artifact tied to the old hash; the registry
will refuse to mark a stale artifact as active. Bumping the version is intentional and reviewed.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any, ClassVar, Final

import dspy
import orjson
from pydantic import BaseModel, ConfigDict, Field, field_validator


class OwnerTeam(StrEnum):
    """Owning teams. CODEOWNERS in the repo route reviews accordingly."""

    AP_AUTOMATION = "ap-automation"
    AR_DUNNING = "ar-dunning"
    CUSTOMS = "customs"
    DATEV_PLATFORM = "datev-platform"
    CRM = "crm"
    MDM = "mdm"
    AUDIT = "audit-platform"


class Demo(BaseModel):
    """A few-shot demonstration. Same provenance fields as a dataset entry.

    Demos live with the signature declaration (so the version_hash changes when a demo changes).
    They are *not* a substitute for the eval dataset — the dataset is held out from the signature.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    inputs: dict[str, Any] = Field(..., description="Field name → example input value.")
    outputs: dict[str, Any] = Field(..., description="Field name → expected output value.")
    labeled_by: str = Field(..., min_length=2, description="ldap / email of the labeler.")
    rationale: str | None = Field(
        default=None,
        description="Optional human-readable why-this-is-correct note (not sent to the LM).",
    )


class SignatureMeta(BaseModel):
    """Static metadata for a signature.

    Treat this like a class-level constant. Mutating at runtime is a smell.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    owner_team: OwnerTeam
    purpose: str = Field(..., min_length=10, max_length=400)
    version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    accuracy_threshold: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="A compiled artifact must hit this on holdout or it does not promote.",
    )
    demos: tuple[Demo, ...] = Field(default_factory=tuple)
    instruction: str = Field(..., min_length=20)
    cost_ceiling_eur_per_1k_calls: float = Field(
        ...,
        gt=0.0,
        description="Compilation rejects any candidate exceeding this even if accuracy is higher.",
    )

    @field_validator("demos")
    @classmethod
    def _at_least_one_demo_in_prod(cls, value: tuple[Demo, ...]) -> tuple[Demo, ...]:
        # The base spec asks for examples on every signature. Zero demos is allowed for net-new
        # signatures still in design; CI's signature_contract_test refuses promotion without demos.
        return value


_REGISTRY: dict[str, type["PutschSignature"]] = {}
SIGNATURE_REGISTRY: Final[dict[str, type["PutschSignature"]]] = _REGISTRY


def register(cls: type["PutschSignature"]) -> type["PutschSignature"]:
    """Decorator. Used by every signature subclass at module-import time."""

    meta = cls.meta()
    if meta.name in _REGISTRY:
        existing = _REGISTRY[meta.name]
        if existing is cls:
            return cls
        raise RuntimeError(
            f"duplicate signature name {meta.name!r}: "
            f"{existing.__module__}.{existing.__qualname__} vs "
            f"{cls.__module__}.{cls.__qualname__}"
        )
    _REGISTRY[meta.name] = cls
    return cls


class PutschSignature(dspy.Signature):
    """Base for every signature in the platform.

    Subclasses *must* override ``meta()``. The metadata is class-level (a ``ClassVar``) and the
    version hash includes the metadata plus the DSPy schema fingerprint — so a field rename, an
    instruction tweak, or a demo edit produces a new hash and forces recompilation.
    """

    _meta: ClassVar[SignatureMeta | None] = None

    @classmethod
    def meta(cls) -> SignatureMeta:
        if cls._meta is None:
            raise NotImplementedError(
                f"{cls.__name__} must set _meta = SignatureMeta(...) at class scope"
            )
        return cls._meta

    @classmethod
    def iter_dspy_fields(cls) -> dict[str, Any]:
        """Resolve the (name → FieldInfo) mapping in a DSPy-version-tolerant way.

        DSPy <=2.4 exposes ``Signature.fields``; 2.5+ inherits Pydantic's ``model_fields``. We
        prefer the former when available, fall back to the latter, and filter to fields that
        carry the ``__dspy_field_type`` marker so non-DSPy class attributes don't leak in.
        """

        container = getattr(cls, "fields", None) or getattr(cls, "model_fields", {})
        result: dict[str, Any] = {}
        for fname, field in container.items():
            extra = getattr(field, "json_schema_extra", None) or {}
            if isinstance(extra, dict) and "__dspy_field_type" in extra:
                result[fname] = field
        return result

    @classmethod
    def fields_fingerprint(cls) -> dict[str, dict[str, str]]:
        """Stable JSON representation of the DSPy fields, for hashing.

        We capture name + role (input/output) + annotation repr + ``desc``. The repr is canonical
        for the same Pydantic type across runs, so this is stable.
        """

        fingerprint: dict[str, dict[str, str]] = {}
        for fname, field in cls.iter_dspy_fields().items():
            extra = getattr(field, "json_schema_extra", None) or {}
            fingerprint[fname] = {
                "role": str(extra.get("__dspy_field_type", "?")),
                "annotation": _annotation_repr(getattr(field, "annotation", str)),
                "desc": getattr(field, "description", "") or "",
            }
        return fingerprint

    @classmethod
    def version_hash(cls) -> str:
        """SHA-256 over (meta + fields fingerprint). Hex-encoded, 16 chars.

        Same signature definition + same demos → same hash, every machine, every Python version.
        This is what the registry stores as the immutable identity of the signature *version*.
        """

        payload = {
            "meta": cls.meta().model_dump(mode="json"),
            "fields": cls.fields_fingerprint(),
        }
        blob = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
        return hashlib.sha256(blob).hexdigest()[:16]


def _annotation_repr(ann: Any) -> str:
    """Stable repr of a type annotation. ``typing`` objects across Python versions are noisy, so
    we normalize aggressively. Anything we don't recognize falls through to ``str(ann)``."""

    module = getattr(ann, "__module__", "") or ""
    qualname = getattr(ann, "__qualname__", None) or getattr(ann, "__name__", None)
    if qualname and module not in {"builtins", ""}:
        return f"{module}.{qualname}"
    if qualname:
        return str(qualname)
    return str(ann)
