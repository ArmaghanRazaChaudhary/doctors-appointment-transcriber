import json


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def render_note(note, warnings):
    data = note.model_dump()
    lines = ["# Clinical note — pending doctor review", ""]
    for section in ("chief_complaint", "symptoms", "medical_history", "allergies",
                    "examination", "clinician_assessment"):
        lines.extend(["## " + section.replace("_", " ").title(), ""])
        rows = data[section]
        rows = rows if isinstance(rows, list) else ([rows] if rows else [])
        for row in rows:
            timing = f"; {row['timing']}" if row['timing'] else ""
            lines.append(f"- {row['description']} ({row['status']}{timing})")
        if not rows:
            lines.append("Not stated.")
        lines.append("")
    assistance = data.get("diagnostic_assistance")
    if assistance:
        lines.extend(["## AI diagnostic suggestions — pending doctor review", "",
                      "Possible explanations for clinician consideration; not confirmed diagnoses.", ""])
        for suggestion in assistance["suggestions"]:
            lines.extend(["### " + suggestion["condition"], ""])
            for field, label in (("supporting_findings", "Supporting finding"),
                                 ("limitations_or_conflicting_findings", "Limitation"),
                                 ("questions_or_checks_for_clinician", "For the clinician to check")):
                lines.extend(f"- {label}: {item}" for item in suggestion[field])
            lines.append("")
        if assistance["insufficient_information_reason"]:
            lines.extend([assistance["insufficient_information_reason"], ""])
    lines.extend(["## Medications", ""])
    for number, med in enumerate(data["medications"], 1):
        name = med["proposed_english_name"] or "Unresolved medication"
        lines.append(f"### {number}. {name} (unverified)")
        lines.append("")
        for field in ("status", "dose", "strength", "formulation", "frequency", "times_per_day",
                      "timing", "as_needed", "duration", "route", "indication", "name_uncertainty"):
            if med[field] is not None:
                lines.append(f"- {field.replace('_', ' ').capitalize()}: {med[field]}")
        lines.append("")
    if not data["medications"]:
        lines.extend(["None stated.", ""])
    lines.extend(["## Follow-up", ""])
    follow = data["follow_up"]
    if follow:
        if follow["interval"]:
            lines.append(f"- Interval: {follow['interval']}")
        lines.extend(f"- {item}" for item in follow["instructions"])
        lines.extend(f"- Return urgently: {item}" for item in follow["urgent_return_conditions"])
    else:
        lines.append("Not stated.")
    lines.extend(["", "## Review", "", "Medication names are model interpretations, not verified identities."])
    lines.extend(f"- {item}" for item in data["review_notes"] + warnings)
    return "\n".join(lines) + "\n"
