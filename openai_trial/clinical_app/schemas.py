from typing import Literal
from pydantic import BaseModel, ConfigDict


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fact(Record):
    description: str
    status: Literal["present", "denied", "uncertain", "historical", "corrected"]
    timing: str | None
    evidence_quotes: list[str]


class Medication(Record):
    spoken_name: str | None
    proposed_english_name: str | None
    name_uncertainty: str | None
    status: Literal["past", "current", "new", "continue", "stop", "uncertain"]
    dose: str | None
    strength: str | None
    formulation: str | None
    frequency: str | None
    times_per_day: int | None
    timing: str | None
    as_needed: bool | None
    duration: str | None
    route: str | None
    indication: str | None
    evidence_quotes: list[str]


class FollowUp(Record):
    interval: str | None
    instructions: list[str]
    urgent_return_conditions: list[str]
    evidence_quotes: list[str]


class DiagnosticSuggestion(Record):
    condition: str
    supporting_findings: list[str]
    limitations_or_conflicting_findings: list[str]
    questions_or_checks_for_clinician: list[str]
    evidence_quotes: list[str]


class DiagnosticAssistance(Record):
    source: Literal["ai_generated"]
    review_status: Literal["pending_doctor_review"]
    suggestions: list[DiagnosticSuggestion]
    insufficient_information_reason: str | None


class ClinicalNote(Record):
    chief_complaint: Fact | None
    symptoms: list[Fact]
    medical_history: list[Fact]
    allergies: list[Fact]
    examination: list[Fact]
    clinician_assessment: list[Fact]
    diagnostic_assistance: DiagnosticAssistance
    medications: list[Medication]
    follow_up: FollowUp | None
    review_notes: list[str]
