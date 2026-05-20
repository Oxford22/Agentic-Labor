"""Putsch business graph ontology — the constitution of the memory layer.

This module is the single source of truth for what entities and
relationships exist in the Putsch agent stack's memory. The Cypher
migrations are GENERATED from these Pydantic models (see migrations.py);
do not hand-write Cypher constraints anywhere else.

DESIGN PRINCIPLES (read before editing)

1.  **Justified, not speculative.** Every entity and relationship in this
    file maps to an actual Putsch business process. If you add a class,
    write the business-need sentence in its docstring. If you cannot
    write that sentence, do not add the class.

2.  **Temporal by default.** Every entity carries a `ValidityWindow`.
    Even "immutable" things like a vendor's HRB-Nummer are technically
    temporal (the company gets re-registered, merged, dissolved).

3.  **Bitemporal.** `business_time` ≠ `system_time`. A correction
    written on 2026-05-18 for a fact that was true on 2025-06-01 has
    `business_time_from = 2025-06-01` and `system_time_from = 2026-05-18`.
    Both are stored. Both matter for audit.

4.  **Identity before attributes.** Each entity has at least one
    *deterministic key* (USt-IdNr, IBAN, HRB-Nummer, SAP Lieferanten-Nr,
    etc.). Entity resolution at write time falls back to embedding
    similarity only when no deterministic key is available, and even
    then only as a human-confirmable suggestion.

5.  **Cardinality is encoded.** Where an attribute is 1:1-at-a-given-
    moment but 1:N-over-time (a vendor has one current address, but has
    had three), the model marks it as `current_unique=True`. The
    Graphiti client refuses writes that would create two concurrent
    "current" values.

6.  **Schema escape hatch.** `RawObservation` exists for facts the system
    hasn't decided how to model. Agents that hit an unmodelled situation
    SHOULD emit a RawObservation, not invent a malformed entity.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


def _normalise_identifier(v: object) -> object:
    """Strip ALL whitespace and uppercase a stringly-typed identifier.

    StringConstraints(strip_whitespace=True) only trims edge whitespace;
    real-world inputs (PDF extraction, hand-typed SAP entries) frequently
    contain internal spaces — e.g. 'DE 123 456 789' for a USt-IdNr. We
    normalise those at the boundary so downstream code can rely on the
    canonical form without re-cleaning on every read.
    """
    if isinstance(v, str):
        return "".join(v.split()).upper()
    return v

# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------

UStIdNr = Annotated[
    str,
    BeforeValidator(_normalise_identifier),
    StringConstraints(
        pattern=r"^[A-Z]{2}[0-9A-Z]{2,12}$",
        strip_whitespace=True,
        to_upper=True,
    ),
]
"""USt-Identifikationsnummer (EU VAT ID). DE123456789, ATU12345678, etc."""

HRBNummer = Annotated[
    str,
    StringConstraints(pattern=r"^HRB\s?\d{1,7}(\s?(B|HRB))?\s?[A-ZÄÖÜ-]+$", strip_whitespace=True),
]
"""German Handelsregister B number, e.g. 'HRB 12345 Hagen'."""

IBAN = Annotated[
    str,
    BeforeValidator(_normalise_identifier),
    StringConstraints(
        pattern=r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{1,30}$",
        strip_whitespace=True,
        to_upper=True,
    ),
]

DUNS = Annotated[str, StringConstraints(pattern=r"^\d{9}$")]

EUR = Annotated[float, Field(ge=0)]

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
"""0.0 = pure speculation, 1.0 = direct from a source-of-truth system."""


class SourceSystem(StrEnum):
    """Where a fact came from. Required on every write."""

    SAP_HAGEN = "sap:hagen"
    SAP_ASHEVILLE = "sap:asheville"
    SAP_POGGIBONSI = "sap:poggibonsi"
    SAP_VALLADOLID = "sap:valladolid"
    SAP_BERGEN = "sap:bergen"
    SAP_NOVE_MESTO = "sap:nove-mesto"
    DATEV = "datev"
    DOCLING = "docling"
    EMAIL = "email"
    MANUAL = "manual"
    AGENT_AP = "agent:ap-crew"
    AGENT_MAHNVERFAHREN = "agent:mahnverfahren-swarm"
    AGENT_STAMMDATEN = "agent:stammdaten-crew"
    AGENT_ZOLL = "agent:zoll-crew"
    AGENT_RECONCILIATION = "agent:reconciliation"
    RECONCILED_FACT = "reconciled"     # written by humans resolving a conflict


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


class ValidityWindow(BaseModel):
    """Bitemporal validity window.

    `business_time_*` = when the fact was true in the world.
    `system_time_*`   = when the fact was written to the graph.

    Both are stored so we can answer:
    * "what did we believe on date X" (`system_time_to >= X`)
    * "what was true on date X"       (`business_time_to >= X`)
    These differ for backdated corrections.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    business_time_from: datetime
    business_time_to: datetime | None = None     # None = currently valid
    system_time_from: datetime
    system_time_to: datetime | None = None
    superseded_by: str | None = Field(
        default=None,
        description="Fact ID of the newer fact that replaced this one. Set by the supersede() call.",
    )

    @field_validator("business_time_from", "business_time_to", "system_time_from", "system_time_to")
    @classmethod
    def _require_utc(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        if v.tzinfo is None:
            raise ValueError("validity-window datetimes must be timezone-aware (UTC).")
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _order(self) -> ValidityWindow:
        if self.business_time_to is not None and self.business_time_to < self.business_time_from:
            raise ValueError("business_time_to precedes business_time_from")
        if self.system_time_to is not None and self.system_time_to < self.system_time_from:
            raise ValueError("system_time_to precedes system_time_from")
        return self

    def is_open(self) -> bool:
        return self.business_time_to is None and self.system_time_to is None

    def is_active_at(self, business_time: datetime, *, system_time: datetime | None = None) -> bool:
        """True if the fact is in force *both* as of `business_time` and
        as known to the system at `system_time` (defaults to "now").
        """
        if business_time < self.business_time_from:
            return False
        if self.business_time_to is not None and business_time >= self.business_time_to:
            return False
        if system_time is not None:
            if system_time < self.system_time_from:
                return False
            if self.system_time_to is not None and system_time >= self.system_time_to:
                return False
        return True


# ---------------------------------------------------------------------------
# Provenance — mandatory on every fact
# ---------------------------------------------------------------------------


class Provenance(BaseModel):
    """Where a fact came from and who wrote it.

    The four fields below are non-negotiable; the SDK refuses anonymous
    writes. The `trace_id` ties this fact back to the Langfuse run that
    produced it, closing the audit loop.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_system: SourceSystem
    source_id: str = Field(min_length=1, max_length=256, description="ID in the upstream system, e.g. SAP doc number.")
    written_by_agent: str = Field(
        min_length=1,
        max_length=128,
        description="e.g. 'ap_crew/v3' or 'sap_sync/2026-05-18T03:00Z'.",
    )
    written_at_trace_id: str = Field(
        min_length=1, max_length=128, description="Langfuse trace ID for the run that produced this fact."
    )
    confidence: Confidence = Field(
        default=1.0,
        description="1.0 for system-of-record extraction; < 0.7 requires human confirmation before downstream use.",
    )
    justification: str | None = Field(
        default=None,
        max_length=2000,
        description="Required for manual corrections. Optional otherwise.",
    )


# ---------------------------------------------------------------------------
# Fact base
# ---------------------------------------------------------------------------


class Fact(BaseModel):
    """Base of every domain entity.

    Subclasses MUST set `__entity_label__` (the Neo4j label) and SHOULD
    declare `__deterministic_keys__` for entity resolution.
    """

    __entity_label__: ClassVar[str]
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ()
    __current_unique_attrs__: ClassVar[tuple[str, ...]] = ()
    """Attributes that must have exactly one *currently valid* value per entity
    (e.g. a vendor has one current address). Enforced at write time."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(
        min_length=1,
        max_length=128,
        description="Stable graph ID. Derived from deterministic keys via `make_id()`.",
    )
    validity: ValidityWindow
    provenance: Provenance
    tags: tuple[str, ...] = Field(default_factory=tuple)

    @classmethod
    def make_id(cls, **deterministic_values: str) -> str:
        """Build a stable graph ID from the entity's deterministic keys.

        Example: `Lieferant.make_id(ust_id_nr="DE123456789")`
                 → `"lieferant:DE123456789"`
        Multi-key entities: `make_id(key1="...", key2="...")`
                 → `"<label>:sha256(key1=...;key2=...)[:16]"`
        """
        missing = set(cls.__deterministic_keys__) - set(deterministic_values)
        if missing:
            raise ValueError(
                f"{cls.__name__}.make_id missing deterministic keys: {sorted(missing)}"
            )
        if len(cls.__deterministic_keys__) == 1:
            (k,) = cls.__deterministic_keys__
            v = deterministic_values[k]
            return f"{cls.__entity_label__.lower()}:{v}"
        ordered = ";".join(f"{k}={deterministic_values[k]}" for k in cls.__deterministic_keys__)
        digest = hashlib.sha256(ordered.encode("utf-8")).hexdigest()[:16]
        return f"{cls.__entity_label__.lower()}:{digest}"


# ---------------------------------------------------------------------------
# Domain entities — each docstring justifies the business need
# ---------------------------------------------------------------------------


class Lieferant(Fact):
    """A vendor / supplier.

    Business need: AP Crew must route invoices to the correct vendor,
    detect duplicate vendor master records across subsidiaries, and
    answer "what did this vendor's payment terms used to be" for audit.
    """

    __entity_label__: ClassVar[str] = "Lieferant"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("ust_id_nr",)
    __current_unique_attrs__: ClassVar[tuple[str, ...]] = ("primary_address", "payment_terms", "bank_iban")

    name: str = Field(min_length=1, max_length=256)
    legal_name: str | None = Field(default=None, max_length=256)
    ust_id_nr: UStIdNr
    hrb_nummer: HRBNummer | None = None
    duns: DUNS | None = None
    sap_vendor_numbers: dict[SourceSystem, str] = Field(
        default_factory=dict,
        description="The same vendor often has different SAP numbers per subsidiary; mapped here.",
    )
    primary_address: str | None = Field(default=None, max_length=512)
    bank_iban: IBAN | None = None
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    is_critical: bool = Field(default=False, description="Flagged for AP-Crew priority routing.")

    @model_validator(mode="after")
    def _id_matches_keys(self) -> Lieferant:
        expected = Lieferant.make_id(ust_id_nr=self.ust_id_nr)
        if self.id != expected:
            raise ValueError(f"Lieferant.id must equal {expected}, got {self.id}")
        return self


class Kunde(Fact):
    """A customer.

    Business need: Mahnverfahren swarm must read relationship history,
    payment behavior, and prior escalations before sending a dunning
    letter. "Has this customer paid late three quarters in a row" is
    a temporal query.
    """

    __entity_label__: ClassVar[str] = "Kunde"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("ust_id_nr",)
    __current_unique_attrs__: ClassVar[tuple[str, ...]] = ("primary_address", "credit_limit_eur", "owner_sachbearbeiter")

    name: str = Field(min_length=1, max_length=256)
    legal_name: str | None = Field(default=None, max_length=256)
    ust_id_nr: UStIdNr
    hrb_nummer: HRBNummer | None = None
    primary_address: str | None = Field(default=None, max_length=512)
    credit_limit_eur: EUR | None = None
    owner_sachbearbeiter: str | None = Field(default=None, description="Mitarbeiter ID; routed via personnel namespace.")
    escalation_level: Literal["none", "soft", "formal", "legal"] = "none"

    @model_validator(mode="after")
    def _id_matches_keys(self) -> Kunde:
        expected = Kunde.make_id(ust_id_nr=self.ust_id_nr)
        if self.id != expected:
            raise ValueError(f"Kunde.id must equal {expected}, got {self.id}")
        return self


class Material(Fact):
    """A material / part.

    Business need: linking Bestellung lines to current HS codes for
    customs and to current pricing for AP three-way matching.
    """

    __entity_label__: ClassVar[str] = "Material"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("sap_material_number",)
    __current_unique_attrs__: ClassVar[tuple[str, ...]] = ("hs_code", "list_price_eur")

    sap_material_number: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=512)
    hs_code: str | None = Field(default=None, pattern=r"^\d{6,10}$")
    unit_of_measure: str = Field(default="ST", max_length=8)
    list_price_eur: EUR | None = None


class Bestellung(Fact):
    """Purchase order.

    Business need: three-way match (Bestellung — Wareneingang — Rechnung)
    is the workhorse AP-Crew decision.
    """

    __entity_label__: ClassVar[str] = "Bestellung"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("po_number", "issuing_subsidiary")

    po_number: str = Field(min_length=1, max_length=32)
    issuing_subsidiary: SourceSystem
    vendor_id: str = Field(description="Lieferant.id")
    total_eur: EUR
    currency: Literal["EUR", "USD", "CHF", "GBP", "CZK", "NOK"] = "EUR"
    incoterm: str | None = Field(default=None, max_length=8)
    expected_delivery: datetime | None = None
    status: Literal["open", "partial", "delivered", "cancelled"] = "open"


class Wareneingang(Fact):
    """Goods receipt.

    Business need: closes the three-way match. Carries the actual
    received quantity, which can differ from the PO line and triggers
    a manual review.
    """

    __entity_label__: ClassVar[str] = "Wareneingang"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("gr_number", "receiving_subsidiary")

    gr_number: str = Field(min_length=1, max_length=32)
    receiving_subsidiary: SourceSystem
    bestellung_id: str
    received_at: datetime
    quantity: float = Field(gt=0)
    material_id: str


class Rechnung(Fact):
    """Invoice (incoming or outgoing).

    Business need: the AP and AR crews' core entity. Carries booking
    fate, posted period, and an audit-grade hash of the original PDF.
    """

    __entity_label__: ClassVar[str] = "Rechnung"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("invoice_number", "issuing_party")

    invoice_number: str = Field(min_length=1, max_length=64)
    issuing_party: str = Field(description="Lieferant.id (incoming) or Kunde.id (outgoing).")
    receiving_party: str
    direction: Literal["incoming", "outgoing"]
    issued_at: datetime
    due_at: datetime | None = None
    paid_at: datetime | None = None
    gross_eur: EUR
    net_eur: EUR
    vat_eur: EUR = 0.0
    currency: Literal["EUR", "USD", "CHF", "GBP", "CZK", "NOK"] = "EUR"
    posted_to_konto: str | None = None
    posted_to_period: str | None = None
    pdf_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class Buchung(Fact):
    """A DATEV booking line.

    Business need: every monetary flow ends as a booking; the audit
    replay starts here and walks back through Rechnung → Bestellung.
    """

    __entity_label__: ClassVar[str] = "Buchung"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("datev_doc_number", "period")

    datev_doc_number: str
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="YYYY-MM")
    debit_konto: str
    credit_konto: str
    amount_eur: EUR
    text: str = Field(max_length=512)
    rechnung_id: str | None = None


class Konto(Fact):
    """A DATEV chart-of-accounts account.

    Business need: routing decisions like "this invoice should post to
    1200" require the account to exist and be valid in the period.
    """

    __entity_label__: ClassVar[str] = "Konto"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("konto_number",)

    konto_number: str = Field(pattern=r"^\d{3,8}$")
    name: str = Field(max_length=256)
    account_type: Literal["aktiv", "passiv", "ertrag", "aufwand"]


class Buchungsperiode(Fact):
    """An accounting period (month).

    Business need: the period is the audit unit. A closed period is
    immutable; new facts that reference a closed period are routed to
    the Wirtschaftsprüfer queue instead of being booked.
    """

    __entity_label__: ClassVar[str] = "Buchungsperiode"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("period",)

    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    status: Literal["open", "soft_closed", "closed", "audited"]
    audited_by: str | None = Field(default=None, description="Wirtschaftsprüfer reference.")


class Mitarbeiter(Fact):
    """An employee. Lives in the personnel namespace ONLY.

    Business need: routing decisions reference "the responsible
    Sachbearbeiter". The Betriebsrat needs to audit those references.

    Compliance: Art. 9 GDPR–adjacent; §87 BetrVG governed. Reads route
    through the personnel_memory interface with audit logging; never
    co-located with vendor/customer facts.
    """

    __entity_label__: ClassVar[str] = "Mitarbeiter"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("personnel_id",)
    __current_unique_attrs__: ClassVar[tuple[str, ...]] = ("role", "site")

    personnel_id: str = Field(min_length=1, max_length=32)
    display_name: str = Field(max_length=128)  # Given name + surname initial; full name stays in HRIS.
    role: str = Field(max_length=128)
    site: str = Field(max_length=64)
    active: bool = True


class Standort(Fact):
    """A site / location.

    Business need: dispatch and customs logic varies by site (Hagen,
    Asheville, Poggibonsi, Valladolid, Bergen, Nové Město).
    """

    __entity_label__: ClassVar[str] = "Standort"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("code",)

    code: Literal["HAGEN", "ASHEVILLE", "POGGIBONSI", "VALLADOLID", "BERGEN", "NOVE_MESTO"]
    country: Literal["DE", "US", "IT", "ES", "NO", "CZ"]
    address: str = Field(max_length=512)


class Tochtergesellschaft(Fact):
    """A legal entity / subsidiary.

    Business need: cross-subsidiary reconciliation; a Hagen booking can
    affect Poggibonsi's intercompany position.
    """

    __entity_label__: ClassVar[str] = "Tochtergesellschaft"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("legal_id",)

    legal_id: str
    name: str = Field(max_length=256)
    site: str = Field(description="Standort.id")
    sap_instance: SourceSystem


class Zollvorgang(Fact):
    """A customs declaration / event.

    Business need: HS-code accuracy is auditable by Zoll authorities;
    the Zoll crew needs to know what HS code was used on a given date.
    """

    __entity_label__: ClassVar[str] = "Zollvorgang"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("declaration_id",)

    declaration_id: str
    direction: Literal["import", "export"]
    hs_code: str = Field(pattern=r"^\d{6,10}$")
    statistical_value_eur: EUR
    country_origin: str = Field(min_length=2, max_length=2)
    country_destination: str = Field(min_length=2, max_length=2)
    bestellung_id: str | None = None


class Mahnverfahren(Fact):
    """A dunning case.

    Business need: escalation tone must reflect prior history. "We have
    sent customer K three dunning letters in eighteen months" is a
    temporal query the Mahnverfahren swarm reads before drafting.
    """

    __entity_label__: ClassVar[str] = "Mahnverfahren"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("case_id",)

    case_id: str
    kunde_id: str
    opened_at: datetime
    closed_at: datetime | None = None
    stage: Literal["pre_dunning", "soft_reminder", "formal_dunning", "legal", "written_off"]
    outstanding_eur: EUR


class RawObservation(Fact):
    """Unmodelled fact — the schema escape hatch.

    Use when an agent encounters a fact it cannot classify under the
    typed entities above. The Stammdaten crew triages RawObservations
    weekly and either promotes them to a typed entity or rejects them.

    Do NOT use this as a lazy default. RawObservation should be rare;
    a growing RawObservation count is a signal that the ontology is
    missing an entity type.
    """

    __entity_label__: ClassVar[str] = "RawObservation"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("observation_id",)

    observation_id: str
    text: str = Field(max_length=8192)
    suggested_entity_type: str | None = None


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------


class RelationshipDescriptor(BaseModel):
    """Static description of a relationship type.

    Stored in BUSINESS_GRAPH below; consumed by the migrations module to
    emit the Cypher constraints. NOT a Pydantic schema for the edge
    payload itself — edge payloads live in episode types.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    from_label: str
    to_label: str
    cardinality: Literal["1:1", "1:N", "N:M"]
    description: str


# ---------------------------------------------------------------------------
# Business graph — the full registry, used for migrations + visualisation
# ---------------------------------------------------------------------------


ENTITIES: tuple[type[Fact], ...] = (
    Lieferant,
    Kunde,
    Material,
    Bestellung,
    Wareneingang,
    Rechnung,
    Buchung,
    Konto,
    Buchungsperiode,
    Mitarbeiter,
    Standort,
    Tochtergesellschaft,
    Zollvorgang,
    Mahnverfahren,
    RawObservation,
)

RELATIONSHIPS: tuple[RelationshipDescriptor, ...] = (
    RelationshipDescriptor(
        name="SUPPLIES",
        from_label="Lieferant",
        to_label="Material",
        cardinality="N:M",
        description="Vendor supplies material. Edge carries unit price, lead time.",
    ),
    RelationshipDescriptor(
        name="ORDERS",
        from_label="Bestellung",
        to_label="Material",
        cardinality="N:M",
        description="PO line orders material at a quantity and price.",
    ),
    RelationshipDescriptor(
        name="INVOICES",
        from_label="Rechnung",
        to_label="Bestellung",
        cardinality="N:M",
        description="Invoice line links back to PO line for three-way match.",
    ),
    RelationshipDescriptor(
        name="RECEIVES",
        from_label="Wareneingang",
        to_label="Bestellung",
        cardinality="N:M",
        description="Goods receipt against PO; carries received quantity.",
    ),
    RelationshipDescriptor(
        name="BELONGS_TO_PERIOD",
        from_label="Buchung",
        to_label="Buchungsperiode",
        cardinality="1:N",
        description="Every booking is in exactly one period; periods hold many.",
    ),
    RelationshipDescriptor(
        name="RESPONSIBLE_FOR",
        from_label="Mitarbeiter",
        to_label="Kunde",
        cardinality="1:N",
        description="Sachbearbeiter owns an account. Validity window encodes rotation.",
    ),
    RelationshipDescriptor(
        name="ESCALATES_TO",
        from_label="Mahnverfahren",
        to_label="Mitarbeiter",
        cardinality="1:1",
        description="Active dunning case is owned by one person at a time.",
    ),
    RelationshipDescriptor(
        name="RECONCILES_WITH",
        from_label="Lieferant",
        to_label="Lieferant",
        cardinality="N:M",
        description="Cross-subsidiary identity: same vendor seen as two SAP records.",
    ),
    RelationshipDescriptor(
        name="AUDITED_BY",
        from_label="Buchungsperiode",
        to_label="Mitarbeiter",
        cardinality="1:1",
        description="Wirtschaftsprüfer who signed off the period.",
    ),
    RelationshipDescriptor(
        name="DECLARED_IN",
        from_label="Bestellung",
        to_label="Zollvorgang",
        cardinality="1:N",
        description="PO line declared on customs declaration.",
    ),
    RelationshipDescriptor(
        name="LOCATED_AT",
        from_label="Tochtergesellschaft",
        to_label="Standort",
        cardinality="1:1",
        description="Legal entity has one primary site.",
    ),
    RelationshipDescriptor(
        name="SUPERSEDED_BY",
        from_label="Fact",
        to_label="Fact",
        cardinality="1:1",
        description="Versioning edge — old fact points forward to its successor.",
    ),
    RelationshipDescriptor(
        name="CORRECTION_OF",
        from_label="Fact",
        to_label="Fact",
        cardinality="1:1",
        description="Backdated correction — system_time differs from business_time.",
    ),
    RelationshipDescriptor(
        name="CONFLICTS_WITH",
        from_label="Fact",
        to_label="Fact",
        cardinality="N:M",
        description="Detected disagreement across source systems. See conflicts.py.",
    ),
)


class BusinessGraph(BaseModel):
    """Registry of entities + relationships. Used by:
    * `migrations.py` to generate Cypher constraints
    * `graphiti_client.py` for label validation
    * `eval/` to enumerate test surface
    * the README to render the ontology diagram
    """

    model_config = ConfigDict(frozen=True)

    entities: tuple[type[Fact], ...]
    relationships: tuple[RelationshipDescriptor, ...]

    def entity_by_label(self, label: str) -> type[Fact]:
        for e in self.entities:
            if e.__entity_label__ == label:
                return e
        raise KeyError(f"no entity with label {label!r}")

    def cypher_constraints(self) -> list[str]:
        """Emit the Cypher DDL that enforces the ontology in Neo4j.

        Generated, not handwritten. The migration runner diff-applies
        these against the live graph.
        """
        out: list[str] = []
        for e in self.entities:
            label = e.__entity_label__
            # ID uniqueness
            out.append(
                f"CREATE CONSTRAINT {label.lower()}_id_unique IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.id IS UNIQUE"
            )
            # Deterministic key uniqueness
            for k in e.__deterministic_keys__:
                out.append(
                    f"CREATE CONSTRAINT {label.lower()}_{k}_unique IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE n.{k} IS UNIQUE"
                )
            # Validity window index
            out.append(
                f"CREATE INDEX {label.lower()}_business_time_from IF NOT EXISTS "
                f"FOR (n:{label}) ON (n.business_time_from)"
            )
            out.append(
                f"CREATE INDEX {label.lower()}_business_time_to IF NOT EXISTS "
                f"FOR (n:{label}) ON (n.business_time_to)"
            )
        # Shared indexes
        out.append("CREATE INDEX ent_source_system IF NOT EXISTS FOR (n:Fact) ON (n.source_system)")
        out.append("CREATE INDEX ent_idempotency IF NOT EXISTS FOR (n:Fact) ON (n.idempotency_key)")
        out.append("CREATE INDEX ent_valid_window IF NOT EXISTS FOR (n:Fact) ON (n.business_time_from, n.business_time_to)")
        return out

    def to_diagram(self) -> str:
        """Render the ontology as a Mermaid graph for the README."""
        lines = ["graph LR"]
        for r in self.relationships:
            lines.append(f"  {r.from_label} -- {r.name} --> {r.to_label}")
        return "\n".join(lines)


BUSINESS_GRAPH: BusinessGraph = BusinessGraph(entities=ENTITIES, relationships=RELATIONSHIPS)


# ---------------------------------------------------------------------------
# Helper for hand-construction of facts in tests / writers
# ---------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def open_window(*, business_time_from: datetime | None = None) -> ValidityWindow:
    """Build a currently-open validity window. `now()` if not given."""
    now = now_utc()
    return ValidityWindow(
        business_time_from=business_time_from or now,
        business_time_to=None,
        system_time_from=now,
        system_time_to=None,
    )


def make_idempotency_key(*, source_system: SourceSystem, source_id: str, event_time: datetime) -> str:
    """Deterministic write-idempotency key. Episodes carrying the same
    (source, source_id, event_time) triple are *the same event* — duplicate
    writes are no-ops."""
    payload = f"{source_system.value}|{source_id}|{event_time.astimezone(timezone.utc).isoformat()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "BUSINESS_GRAPH",
    "Bestellung",
    "Buchung",
    "Buchungsperiode",
    "BusinessGraph",
    "Confidence",
    "DUNS",
    "EUR",
    "Fact",
    "HRBNummer",
    "IBAN",
    "Konto",
    "Kunde",
    "Lieferant",
    "Mahnverfahren",
    "Material",
    "Mitarbeiter",
    "Provenance",
    "RawObservation",
    "Rechnung",
    "RelationshipDescriptor",
    "SourceSystem",
    "Standort",
    "Tochtergesellschaft",
    "UStIdNr",
    "ValidityWindow",
    "Wareneingang",
    "Zollvorgang",
    "make_idempotency_key",
    "now_utc",
    "open_window",
]


"""Putsch business graph ontology — the constitution of the memory layer.

This module is the single source of truth for what entities and
relationships exist in the Putsch agent stack's memory. The Cypher
migrations are GENERATED from these Pydantic models (see migrations.py);
do not hand-write Cypher constraints anywhere else.

DESIGN PRINCIPLES (read before editing)

1.  **Justified, not speculative.** Every entity and relationship in this
    file maps to an actual Putsch business process. If you add a class,
    write the business-need sentence in its docstring. If you cannot
    write that sentence, do not add the class.

2.  **Temporal by default.** Every entity carries a `ValidityWindow`.
    Even "immutable" things like a vendor's HRB-Nummer are technically
    temporal (the company gets re-registered, merged, dissolved).

3.  **Bitemporal.** `business_time` ≠ `system_time`. A correction
    written on 2026-05-18 for a fact that was true on 2025-06-01 has
    `business_time_from = 2025-06-01` and `system_time_from = 2026-05-18`.
    Both are stored. Both matter for audit.

4.  **Identity before attributes.** Each entity has at least one
    *deterministic key* (USt-IdNr, IBAN, HRB-Nummer, SAP Lieferanten-Nr,
    etc.). Entity resolution at write time falls back to embedding
    similarity only when no deterministic key is available, and even
    then only as a human-confirmable suggestion.

5.  **Cardinality is encoded.** Where an attribute is 1:1-at-a-given-
    moment but 1:N-over-time (a vendor has one current address, but has
    had three), the model marks it as `current_unique=True`. The
    Graphiti client refuses writes that would create two concurrent
    "current" values.

6.  **Schema escape hatch.** `RawObservation` exists for facts the system
    hasn't decided how to model. Agents that hit an unmodelled situation
    SHOULD emit a RawObservation, not invent a malformed entity.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------

UStIdNr = Annotated[
    str,
    StringConstraints(
        pattern=r"^[A-Z]{2}[0-9A-Z]{2,12}$",
        strip_whitespace=True,
        to_upper=True,
    ),
]
"""USt-Identifikationsnummer (EU VAT ID). DE123456789, ATU12345678, etc."""

HRBNummer = Annotated[
    str,
    StringConstraints(pattern=r"^HRB\s?\d{1,7}(\s?(B|HRB))?\s?[A-ZÄÖÜ-]+$", strip_whitespace=True),
]
"""German Handelsregister B number, e.g. 'HRB 12345 Hagen'."""

IBAN = Annotated[
    str,
    StringConstraints(
        pattern=r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{1,30}$",
        strip_whitespace=True,
        to_upper=True,
    ),
]

DUNS = Annotated[str, StringConstraints(pattern=r"^\d{9}$")]

EUR = Annotated[float, Field(ge=0)]

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
"""0.0 = pure speculation, 1.0 = direct from a source-of-truth system."""


class SourceSystem(StrEnum):
    """Where a fact came from. Required on every write."""

    SAP_HAGEN = "sap:hagen"
    SAP_ASHEVILLE = "sap:asheville"
    SAP_POGGIBONSI = "sap:poggibonsi"
    SAP_VALLADOLID = "sap:valladolid"
    SAP_BERGEN = "sap:bergen"
    SAP_NOVE_MESTO = "sap:nove-mesto"
    DATEV = "datev"
    DOCLING = "docling"
    EMAIL = "email"
    MANUAL = "manual"
    AGENT_AP = "agent:ap-crew"
    AGENT_MAHNVERFAHREN = "agent:mahnverfahren-swarm"
    AGENT_STAMMDATEN = "agent:stammdaten-crew"
    AGENT_ZOLL = "agent:zoll-crew"
    AGENT_RECONCILIATION = "agent:reconciliation"
    RECONCILED_FACT = "reconciled"     # written by humans resolving a conflict


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


class ValidityWindow(BaseModel):
    """Bitemporal validity window.

    `business_time_*` = when the fact was true in the world.
    `system_time_*`   = when the fact was written to the graph.

    Both are stored so we can answer:
    * "what did we believe on date X" (`system_time_to >= X`)
    * "what was true on date X"       (`business_time_to >= X`)
    These differ for backdated corrections.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    business_time_from: datetime
    business_time_to: datetime | None = None     # None = currently valid
    system_time_from: datetime
    system_time_to: datetime | None = None
    superseded_by: str | None = Field(
        default=None,
        description="Fact ID of the newer fact that replaced this one. Set by the supersede() call.",
    )

    @field_validator("business_time_from", "business_time_to", "system_time_from", "system_time_to")
    @classmethod
    def _require_utc(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        if v.tzinfo is None:
            raise ValueError("validity-window datetimes must be timezone-aware (UTC).")
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _order(self) -> ValidityWindow:
        if self.business_time_to is not None and self.business_time_to < self.business_time_from:
            raise ValueError("business_time_to precedes business_time_from")
        if self.system_time_to is not None and self.system_time_to < self.system_time_from:
            raise ValueError("system_time_to precedes system_time_from")
        return self

    def is_open(self) -> bool:
        return self.business_time_to is None and self.system_time_to is None

    def is_active_at(self, business_time: datetime, *, system_time: datetime | None = None) -> bool:
        """True if the fact is in force *both* as of `business_time` and
        as known to the system at `system_time` (defaults to "now").
        """
        if business_time < self.business_time_from:
            return False
        if self.business_time_to is not None and business_time >= self.business_time_to:
            return False
        if system_time is not None:
            if system_time < self.system_time_from:
                return False
            if self.system_time_to is not None and system_time >= self.system_time_to:
                return False
        return True


# ---------------------------------------------------------------------------
# Provenance — mandatory on every fact
# ---------------------------------------------------------------------------


class Provenance(BaseModel):
    """Where a fact came from and who wrote it.

    The four fields below are non-negotiable; the SDK refuses anonymous
    writes. The `trace_id` ties this fact back to the Langfuse run that
    produced it, closing the audit loop.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_system: SourceSystem
    source_id: str = Field(min_length=1, max_length=256, description="ID in the upstream system, e.g. SAP doc number.")
    written_by_agent: str = Field(
        min_length=1,
        max_length=128,
        description="e.g. 'ap_crew/v3' or 'sap_sync/2026-05-18T03:00Z'.",
    )
    written_at_trace_id: str = Field(
        min_length=1, max_length=128, description="Langfuse trace ID for the run that produced this fact."
    )
    confidence: Confidence = Field(
        default=1.0,
        description="1.0 for system-of-record extraction; < 0.7 requires human confirmation before downstream use.",
    )
    justification: str | None = Field(
        default=None,
        max_length=2000,
        description="Required for manual corrections. Optional otherwise.",
    )


# ---------------------------------------------------------------------------
# Fact base
# ---------------------------------------------------------------------------


class Fact(BaseModel):
    """Base of every domain entity.

    Subclasses MUST set `__entity_label__` (the Neo4j label) and SHOULD
    declare `__deterministic_keys__` for entity resolution.
    """

    __entity_label__: ClassVar[str]
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ()
    __current_unique_attrs__: ClassVar[tuple[str, ...]] = ()
    """Attributes that must have exactly one *currently valid* value per entity
    (e.g. a vendor has one current address). Enforced at write time."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(
        min_length=1,
        max_length=128,
        description="Stable graph ID. Derived from deterministic keys via `make_id()`.",
    )
    validity: ValidityWindow
    provenance: Provenance
    tags: tuple[str, ...] = Field(default_factory=tuple)

    @classmethod
    def make_id(cls, **deterministic_values: str) -> str:
        """Build a stable graph ID from the entity's deterministic keys.

        Example: `Lieferant.make_id(ust_id_nr="DE123456789")`
                 → `"lieferant:DE123456789"`
        Multi-key entities: `make_id(key1="...", key2="...")`
                 → `"<label>:sha256(key1=...;key2=...)[:16]"`
        """
        missing = set(cls.__deterministic_keys__) - set(deterministic_values)
        if missing:
            raise ValueError(
                f"{cls.__name__}.make_id missing deterministic keys: {sorted(missing)}"
            )
        if len(cls.__deterministic_keys__) == 1:
            (k,) = cls.__deterministic_keys__
            v = deterministic_values[k]
            return f"{cls.__entity_label__.lower()}:{v}"
        ordered = ";".join(f"{k}={deterministic_values[k]}" for k in cls.__deterministic_keys__)
        digest = hashlib.sha256(ordered.encode("utf-8")).hexdigest()[:16]
        return f"{cls.__entity_label__.lower()}:{digest}"


# ---------------------------------------------------------------------------
# Domain entities — each docstring justifies the business need
# ---------------------------------------------------------------------------


class Lieferant(Fact):
    """A vendor / supplier.

    Business need: AP Crew must route invoices to the correct vendor,
    detect duplicate vendor master records across subsidiaries, and
    answer "what did this vendor's payment terms used to be" for audit.
    """

    __entity_label__: ClassVar[str] = "Lieferant"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("ust_id_nr",)
    __current_unique_attrs__: ClassVar[tuple[str, ...]] = ("primary_address", "payment_terms", "bank_iban")

    name: str = Field(min_length=1, max_length=256)
    legal_name: str | None = Field(default=None, max_length=256)
    ust_id_nr: UStIdNr
    hrb_nummer: HRBNummer | None = None
    duns: DUNS | None = None
    sap_vendor_numbers: dict[SourceSystem, str] = Field(
        default_factory=dict,
        description="The same vendor often has different SAP numbers per subsidiary; mapped here.",
    )
    primary_address: str | None = Field(default=None, max_length=512)
    bank_iban: IBAN | None = None
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    is_critical: bool = Field(default=False, description="Flagged for AP-Crew priority routing.")

    @model_validator(mode="after")
    def _id_matches_keys(self) -> Lieferant:
        expected = Lieferant.make_id(ust_id_nr=self.ust_id_nr)
        if self.id != expected:
            raise ValueError(f"Lieferant.id must equal {expected}, got {self.id}")
        return self


class Kunde(Fact):
    """A customer.

    Business need: Mahnverfahren swarm must read relationship history,
    payment behavior, and prior escalations before sending a dunning
    letter. "Has this customer paid late three quarters in a row" is
    a temporal query.
    """

    __entity_label__: ClassVar[str] = "Kunde"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("ust_id_nr",)
    __current_unique_attrs__: ClassVar[tuple[str, ...]] = ("primary_address", "credit_limit_eur", "owner_sachbearbeiter")

    name: str = Field(min_length=1, max_length=256)
    legal_name: str | None = Field(default=None, max_length=256)
    ust_id_nr: UStIdNr
    hrb_nummer: HRBNummer | None = None
    primary_address: str | None = Field(default=None, max_length=512)
    credit_limit_eur: EUR | None = None
    owner_sachbearbeiter: str | None = Field(default=None, description="Mitarbeiter ID; routed via personnel namespace.")
    escalation_level: Literal["none", "soft", "formal", "legal"] = "none"

    @model_validator(mode="after")
    def _id_matches_keys(self) -> Kunde:
        expected = Kunde.make_id(ust_id_nr=self.ust_id_nr)
        if self.id != expected:
            raise ValueError(f"Kunde.id must equal {expected}, got {self.id}")
        return self


class Material(Fact):
    """A material / part.

    Business need: linking Bestellung lines to current HS codes for
    customs and to current pricing for AP three-way matching.
    """

    __entity_label__: ClassVar[str] = "Material"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("sap_material_number",)
    __current_unique_attrs__: ClassVar[tuple[str, ...]] = ("hs_code", "list_price_eur")

    sap_material_number: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=512)
    hs_code: str | None = Field(default=None, pattern=r"^\d{6,10}$")
    unit_of_measure: str = Field(default="ST", max_length=8)
    list_price_eur: EUR | None = None


class Bestellung(Fact):
    """Purchase order.

    Business need: three-way match (Bestellung — Wareneingang — Rechnung)
    is the workhorse AP-Crew decision.
    """

    __entity_label__: ClassVar[str] = "Bestellung"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("po_number", "issuing_subsidiary")

    po_number: str = Field(min_length=1, max_length=32)
    issuing_subsidiary: SourceSystem
    vendor_id: str = Field(description="Lieferant.id")
    total_eur: EUR
    currency: Literal["EUR", "USD", "CHF", "GBP", "CZK", "NOK"] = "EUR"
    incoterm: str | None = Field(default=None, max_length=8)
    expected_delivery: datetime | None = None
    status: Literal["open", "partial", "delivered", "cancelled"] = "open"


class Wareneingang(Fact):
    """Goods receipt.

    Business need: closes the three-way match. Carries the actual
    received quantity, which can differ from the PO line and triggers
    a manual review.
    """

    __entity_label__: ClassVar[str] = "Wareneingang"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("gr_number", "receiving_subsidiary")

    gr_number: str = Field(min_length=1, max_length=32)
    receiving_subsidiary: SourceSystem
    bestellung_id: str
    received_at: datetime
    quantity: float = Field(gt=0)
    material_id: str


class Rechnung(Fact):
    """Invoice (incoming or outgoing).

    Business need: the AP and AR crews' core entity. Carries booking
    fate, posted period, and an audit-grade hash of the original PDF.
    """

    __entity_label__: ClassVar[str] = "Rechnung"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("invoice_number", "issuing_party")

    invoice_number: str = Field(min_length=1, max_length=64)
    issuing_party: str = Field(description="Lieferant.id (incoming) or Kunde.id (outgoing).")
    receiving_party: str
    direction: Literal["incoming", "outgoing"]
    issued_at: datetime
    due_at: datetime | None = None
    paid_at: datetime | None = None
    gross_eur: EUR
    net_eur: EUR
    vat_eur: EUR = 0.0
    currency: Literal["EUR", "USD", "CHF", "GBP", "CZK", "NOK"] = "EUR"
    posted_to_konto: str | None = None
    posted_to_period: str | None = None
    pdf_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class Buchung(Fact):
    """A DATEV booking line.

    Business need: every monetary flow ends as a booking; the audit
    replay starts here and walks back through Rechnung → Bestellung.
    """

    __entity_label__: ClassVar[str] = "Buchung"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("datev_doc_number", "period")

    datev_doc_number: str
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="YYYY-MM")
    debit_konto: str
    credit_konto: str
    amount_eur: EUR
    text: str = Field(max_length=512)
    rechnung_id: str | None = None


class Konto(Fact):
    """A DATEV chart-of-accounts account.

    Business need: routing decisions like "this invoice should post to
    1200" require the account to exist and be valid in the period.
    """

    __entity_label__: ClassVar[str] = "Konto"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("konto_number",)

    konto_number: str = Field(pattern=r"^\d{3,8}$")
    name: str = Field(max_length=256)
    account_type: Literal["aktiv", "passiv", "ertrag", "aufwand"]


class Buchungsperiode(Fact):
    """An accounting period (month).

    Business need: the period is the audit unit. A closed period is
    immutable; new facts that reference a closed period are routed to
    the Wirtschaftsprüfer queue instead of being booked.
    """

    __entity_label__: ClassVar[str] = "Buchungsperiode"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("period",)

    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    status: Literal["open", "soft_closed", "closed", "audited"]
    audited_by: str | None = Field(default=None, description="Wirtschaftsprüfer reference.")


class Mitarbeiter(Fact):
    """An employee. Lives in the personnel namespace ONLY.

    Business need: routing decisions reference "the responsible
    Sachbearbeiter". The Betriebsrat needs to audit those references.

    Compliance: Art. 9 GDPR–adjacent; §87 BetrVG governed. Reads route
    through the personnel_memory interface with audit logging; never
    co-located with vendor/customer facts.
    """

    __entity_label__: ClassVar[str] = "Mitarbeiter"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("personnel_id",)
    __current_unique_attrs__: ClassVar[tuple[str, ...]] = ("role", "site")

    personnel_id: str = Field(min_length=1, max_length=32)
    display_name: str = Field(max_length=128)  # Given name + surname initial; full name stays in HRIS.
    role: str = Field(max_length=128)
    site: str = Field(max_length=64)
    active: bool = True


class Standort(Fact):
    """A site / location.

    Business need: dispatch and customs logic varies by site (Hagen,
    Asheville, Poggibonsi, Valladolid, Bergen, Nové Město).
    """

    __entity_label__: ClassVar[str] = "Standort"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("code",)

    code: Literal["HAGEN", "ASHEVILLE", "POGGIBONSI", "VALLADOLID", "BERGEN", "NOVE_MESTO"]
    country: Literal["DE", "US", "IT", "ES", "NO", "CZ"]
    address: str = Field(max_length=512)


class Tochtergesellschaft(Fact):
    """A legal entity / subsidiary.

    Business need: cross-subsidiary reconciliation; a Hagen booking can
    affect Poggibonsi's intercompany position.
    """

    __entity_label__: ClassVar[str] = "Tochtergesellschaft"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("legal_id",)

    legal_id: str
    name: str = Field(max_length=256)
    site: str = Field(description="Standort.id")
    sap_instance: SourceSystem


class Zollvorgang(Fact):
    """A customs declaration / event.

    Business need: HS-code accuracy is auditable by Zoll authorities;
    the Zoll crew needs to know what HS code was used on a given date.
    """

    __entity_label__: ClassVar[str] = "Zollvorgang"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("declaration_id",)

    declaration_id: str
    direction: Literal["import", "export"]
    hs_code: str = Field(pattern=r"^\d{6,10}$")
    statistical_value_eur: EUR
    country_origin: str = Field(min_length=2, max_length=2)
    country_destination: str = Field(min_length=2, max_length=2)
    bestellung_id: str | None = None


class Mahnverfahren(Fact):
    """A dunning case.

    Business need: escalation tone must reflect prior history. "We have
    sent customer K three dunning letters in eighteen months" is a
    temporal query the Mahnverfahren swarm reads before drafting.
    """

    __entity_label__: ClassVar[str] = "Mahnverfahren"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("case_id",)

    case_id: str
    kunde_id: str
    opened_at: datetime
    closed_at: datetime | None = None
    stage: Literal["pre_dunning", "soft_reminder", "formal_dunning", "legal", "written_off"]
    outstanding_eur: EUR


class RawObservation(Fact):
    """Unmodelled fact — the schema escape hatch.

    Use when an agent encounters a fact it cannot classify under the
    typed entities above. The Stammdaten crew triages RawObservations
    weekly and either promotes them to a typed entity or rejects them.

    Do NOT use this as a lazy default. RawObservation should be rare;
    a growing RawObservation count is a signal that the ontology is
    missing an entity type.
    """

    __entity_label__: ClassVar[str] = "RawObservation"
    __deterministic_keys__: ClassVar[tuple[str, ...]] = ("observation_id",)

    observation_id: str
    text: str = Field(max_length=8192)
    suggested_entity_type: str | None = None


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------


class RelationshipDescriptor(BaseModel):
    """Static description of a relationship type.

    Stored in BUSINESS_GRAPH below; consumed by the migrations module to
    emit the Cypher constraints. NOT a Pydantic schema for the edge
    payload itself — edge payloads live in episode types.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    from_label: str
    to_label: str
    cardinality: Literal["1:1", "1:N", "N:M"]
    description: str


# ---------------------------------------------------------------------------
# Business graph — the full registry, used for migrations + visualisation
# ---------------------------------------------------------------------------


ENTITIES: tuple[type[Fact], ...] = (
    Lieferant,
    Kunde,
    Material,
    Bestellung,
    Wareneingang,
    Rechnung,
    Buchung,
    Konto,
    Buchungsperiode,
    Mitarbeiter,
    Standort,
    Tochtergesellschaft,
    Zollvorgang,
    Mahnverfahren,
    RawObservation,
)

RELATIONSHIPS: tuple[RelationshipDescriptor, ...] = (
    RelationshipDescriptor(
        name="SUPPLIES",
        from_label="Lieferant",
        to_label="Material",
        cardinality="N:M",
        description="Vendor supplies material. Edge carries unit price, lead time.",
    ),
    RelationshipDescriptor(
        name="ORDERS",
        from_label="Bestellung",
        to_label="Material",
        cardinality="N:M",
        description="PO line orders material at a quantity and price.",
    ),
    RelationshipDescriptor(
        name="INVOICES",
        from_label="Rechnung",
        to_label="Bestellung",
        cardinality="N:M",
        description="Invoice line links back to PO line for three-way match.",
    ),
    RelationshipDescriptor(
        name="RECEIVES",
        from_label="Wareneingang",
        to_label="Bestellung",
        cardinality="N:M",
        description="Goods receipt against PO; carries received quantity.",
    ),
    RelationshipDescriptor(
        name="BELONGS_TO_PERIOD",
        from_label="Buchung",
        to_label="Buchungsperiode",
        cardinality="1:N",
        description="Every booking is in exactly one period; periods hold many.",
    ),
    RelationshipDescriptor(
        name="RESPONSIBLE_FOR",
        from_label="Mitarbeiter",
        to_label="Kunde",
        cardinality="1:N",
        description="Sachbearbeiter owns an account. Validity window encodes rotation.",
    ),
    RelationshipDescriptor(
        name="ESCALATES_TO",
        from_label="Mahnverfahren",
        to_label="Mitarbeiter",
        cardinality="1:1",
        description="Active dunning case is owned by one person at a time.",
    ),
    RelationshipDescriptor(
        name="RECONCILES_WITH",
        from_label="Lieferant",
        to_label="Lieferant",
        cardinality="N:M",
        description="Cross-subsidiary identity: same vendor seen as two SAP records.",
    ),
    RelationshipDescriptor(
        name="AUDITED_BY",
        from_label="Buchungsperiode",
        to_label="Mitarbeiter",
        cardinality="1:1",
        description="Wirtschaftsprüfer who signed off the period.",
    ),
    RelationshipDescriptor(
        name="DECLARED_IN",
        from_label="Bestellung",
        to_label="Zollvorgang",
        cardinality="1:N",
        description="PO line declared on customs declaration.",
    ),
    RelationshipDescriptor(
        name="LOCATED_AT",
        from_label="Tochtergesellschaft",
        to_label="Standort",
        cardinality="1:1",
        description="Legal entity has one primary site.",
    ),
    RelationshipDescriptor(
        name="SUPERSEDED_BY",
        from_label="Fact",
        to_label="Fact",
        cardinality="1:1",
        description="Versioning edge — old fact points forward to its successor.",
    ),
    RelationshipDescriptor(
        name="CORRECTION_OF",
        from_label="Fact",
        to_label="Fact",
        cardinality="1:1",
        description="Backdated correction — system_time differs from business_time.",
    ),
    RelationshipDescriptor(
        name="CONFLICTS_WITH",
        from_label="Fact",
        to_label="Fact",
        cardinality="N:M",
        description="Detected disagreement across source systems. See conflicts.py.",
    ),
)


class BusinessGraph(BaseModel):
    """Registry of entities + relationships. Used by:
    * `migrations.py` to generate Cypher constraints
    * `graphiti_client.py` for label validation
    * `eval/` to enumerate test surface
    * the README to render the ontology diagram
    """

    model_config = ConfigDict(frozen=True)

    entities: tuple[type[Fact], ...]
    relationships: tuple[RelationshipDescriptor, ...]

    def entity_by_label(self, label: str) -> type[Fact]:
        for e in self.entities:
            if e.__entity_label__ == label:
                return e
        raise KeyError(f"no entity with label {label!r}")

    def cypher_constraints(self) -> list[str]:
        """Emit the Cypher DDL that enforces the ontology in Neo4j.

        Generated, not handwritten. The migration runner diff-applies
        these against the live graph.
        """
        out: list[str] = []
        for e in self.entities:
            label = e.__entity_label__
            # ID uniqueness
            out.append(
                f"CREATE CONSTRAINT {label.lower()}_id_unique IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.id IS UNIQUE"
            )
            # Deterministic key uniqueness
            for k in e.__deterministic_keys__:
                out.append(
                    f"CREATE CONSTRAINT {label.lower()}_{k}_unique IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE n.{k} IS UNIQUE"
                )
            # Validity window index
            out.append(
                f"CREATE INDEX {label.lower()}_business_time_from IF NOT EXISTS "
                f"FOR (n:{label}) ON (n.business_time_from)"
            )
            out.append(
                f"CREATE INDEX {label.lower()}_business_time_to IF NOT EXISTS "
                f"FOR (n:{label}) ON (n.business_time_to)"
            )
        # Shared indexes
        out.append("CREATE INDEX ent_source_system IF NOT EXISTS FOR (n:Fact) ON (n.source_system)")
        out.append("CREATE INDEX ent_idempotency IF NOT EXISTS FOR (n:Fact) ON (n.idempotency_key)")
        out.append("CREATE INDEX ent_valid_window IF NOT EXISTS FOR (n:Fact) ON (n.business_time_from, n.business_time_to)")
        return out

    def to_diagram(self) -> str:
        """Render the ontology as a Mermaid graph for the README."""
        lines = ["graph LR"]
        for r in self.relationships:
            lines.append(f"  {r.from_label} -- {r.name} --> {r.to_label}")
        return "\n".join(lines)


BUSINESS_GRAPH: BusinessGraph = BusinessGraph(entities=ENTITIES, relationships=RELATIONSHIPS)


# ---------------------------------------------------------------------------
# Helper for hand-construction of facts in tests / writers
# ---------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def open_window(*, business_time_from: datetime | None = None) -> ValidityWindow:
    """Build a currently-open validity window. `now()` if not given."""
    now = now_utc()
    return ValidityWindow(
        business_time_from=business_time_from or now,
        business_time_to=None,
        system_time_from=now,
        system_time_to=None,
    )


def make_idempotency_key(*, source_system: SourceSystem, source_id: str, event_time: datetime) -> str:
    """Deterministic write-idempotency key. Episodes carrying the same
    (source, source_id, event_time) triple are *the same event* — duplicate
    writes are no-ops."""
    payload = f"{source_system.value}|{source_id}|{event_time.astimezone(timezone.utc).isoformat()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "BUSINESS_GRAPH",
    "Bestellung",
    "Buchung",
    "Buchungsperiode",
    "BusinessGraph",
    "Confidence",
    "DUNS",
    "EUR",
    "Fact",
    "HRBNummer",
    "IBAN",
    "Konto",
    "Kunde",
    "Lieferant",
    "Mahnverfahren",
    "Material",
    "Mitarbeiter",
    "Provenance",
    "RawObservation",
    "Rechnung",
    "RelationshipDescriptor",
    "SourceSystem",
    "Standort",
    "Tochtergesellschaft",
    "UStIdNr",
    "ValidityWindow",
    "Wareneingang",
    "Zollvorgang",
    "make_idempotency_key",
    "now_utc",
    "open_window",
]


