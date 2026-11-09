"""Check provenance only; do not rewrite model facts using sample-specific rules."""
import re


def normalized(text):
    return re.sub(r"\s+", " ", text).strip()


def check_evidence(note, transcript):
    source = normalized(transcript)
    findings = []

    def inspect(record, location):
        quotes = record.get("evidence_quotes", [])
        if not quotes:
            findings.append(f"{location}: no supporting source quote.")
        for quote in quotes:
            if not normalized(quote) or normalized(quote) not in source:
                findings.append(f"{location}: source quote is not present verbatim.")

    data = note.model_dump()
    for section, value in data.items():
        if section == "diagnostic_assistance":
            for index, suggestion in enumerate(value["suggestions"]):
                inspect(suggestion, f"diagnostic_assistance.suggestions[{index}]")
            if len(value["suggestions"]) > 3:
                findings.append("diagnostic_assistance: more than three suggestions; review required.")
            if not value["suggestions"] and not value["insufficient_information_reason"]:
                findings.append("diagnostic_assistance: no suggestions or explanation provided.")
            continue
        if section == "review_notes" or value is None:
            continue
        if isinstance(value, list):
            for index, record in enumerate(value):
                inspect(record, f"{section}[{index}]")
        else:
            inspect(value, section)
    return findings
