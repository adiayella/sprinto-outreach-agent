"""Structured-output schemas the LLM must fill. Code validates everything it returns."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

FIELDS = [
    "company_name", "product", "business_model", "customer_segments", "named_customers",
    "funding_stage", "employee_count", "data_handled", "compliance_claims", "hiring_signals",
    "business_triggers", "existing_compliance_tools",
]
FieldName = Literal[
    "company_name", "product", "business_model", "customer_segments", "named_customers",
    "funding_stage", "employee_count", "data_handled", "compliance_claims", "hiring_signals",
    "business_triggers", "existing_compliance_tools",
]


class Citation(BaseModel):
    page: str = Field(description="Page id, e.g. P1 or S1")
    excerpt: str = Field(description="Exact words copied from that page, 5-40 words")


class FieldOut(BaseModel):
    field: FieldName
    status: Literal["observed", "inferred", "unknown", "conflicting"]
    value: str = Field(description="Short value in your words; empty if unknown")
    citations: list[Citation] = Field(default_factory=list)


class ExtractionOut(BaseModel):
    fields: list[FieldOut]


DimKey = Literal["fit", "trust_exposure", "workload", "reason_now", "complexity"]


class DimOut(BaseModel):
    key: DimKey
    level: int = Field(ge=0, le=2)
    evidence_ids: list[str] = Field(default_factory=list, description="Evidence ids like E3 that support this level")
    reason: str
    zero_reason: Literal["unknown", "explicit_negative", "outside_window", "not_applicable"] = "not_applicable"
    evidence_date: str = Field(default="", description="For reason_now: date stated in the evidence, YYYY-MM or YYYY-MM-DD")


class QualOut(BaseModel):
    dimensions: list[DimOut]
    unrelated_business: bool = Field(description="True only if evidence shows the company does not sell software/digital services")
    unrelated_evidence_ids: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list, description="Missing facts that would change the decision")
    next_pages: list[str] = Field(default_factory=list, description="Up to 2 URLs copied from the candidate link list")
    search_query: str = Field(default="", description="One web search query for the most important gap, or empty")


class PainOut(BaseModel):
    title: str
    statement: str = Field(description="One tentative sentence about this person's likely pain")
    evidence_ids: list[str]
    capability_id: str
    why_this_person: str


class PainsOut(BaseModel):
    hypotheses: list[PainOut]


class EmailParts(BaseModel):
    subject: str
    greeting: str
    opening_fact: str = Field(description="One sentence with a specific, evidence-backed observation about the company")
    opening_evidence_ids: list[str]
    paragraphs: list[str] = Field(description="1-2 short paragraphs: tentative pain + how Sprinto helps")
    question: str = Field(description="Exactly one low-pressure question")
    capability_ids: list[str] = Field(default_factory=list)


class EmailEval(BaseModel):
    hook: int = Field(ge=0, le=2)
    hypothesis: int = Field(ge=0, le=2)
    value: int = Field(ge=0, le=2)
    next_step: int = Field(ge=0, le=2)
    clarity: int = Field(ge=0, le=2)
    notes: str
