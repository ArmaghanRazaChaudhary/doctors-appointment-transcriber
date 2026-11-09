"""Clinical note persistence and terminal presentation."""
import datetime
import json
import os

from .config import OUTPUT_DIR
from .medicines import _english_medication_label

def save_note(transcript, annotated_transcript, structured_data, audio_path):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    record = {
        "timestamp": timestamp,
        "audio_file": audio_path,
        "raw_transcript": transcript,
        "medication_annotated_transcript":
            annotated_transcript,
        "clinical_data":
            structured_data,
        "status":
            "pending_review",
    }
    output_path = os.path.join(OUTPUT_DIR, f'note_{timestamp}.json')
    with open(
        output_path,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return output_path


def display_clinical_note(data):
    chief_complaint = data.get('chief_complaint', {})
    symptoms = data.get('symptoms', [])
    negative_symptoms = data.get('negative_symptoms', [])
    medical_history = data.get('medical_history', [])
    medications = data.get('medications', [])
    examination = data.get('examination', [])
    assessment = data.get('assessment')
    follow_up = data.get('follow_up', {})
    validation = data.get('validation', {})
    print()
    print("=" * 60)
    print("STRUCTURED CLINICAL NOTE")
    print("=" * 60)

    # CHIEF COMPLAINT
    print()
    print("Chief Complaint:")
    if chief_complaint.get("name"):
        complaint = chief_complaint.get("name")
        duration = chief_complaint.get('duration_text')
        if duration:
            print(f'{complaint} ({duration})')
        else:
            print(complaint)
    else:
        print('Not mentioned')

    # SYMPTOMS
    print()
    print("Symptoms:")
    if symptoms:
        for symptom in symptoms:
            if not isinstance(symptom, dict):
                continue
            name = symptom.get('name')
            if not name:
                continue

            # Absent findings do not belong under positive Symptoms.
            # They are shown under Relevant Negatives below.
            if symptom.get("present") is False:
                continue
            details = []
            if symptom.get('duration_text'):
                details.append(symptom['duration_text'])
            if symptom.get('severity'):
                details.append(symptom['severity'])
            if symptom.get('timing'):
                details.append(symptom['timing'])
            if details:
                print(f" - {name}: {', '.join(details)}")
            else:
                print(f' - {name}')
    else:
        print('None mentioned')

    # NEGATIVE SYMPTOMS / RELEVANT NEGATIVES
    print()
    print("Relevant Negatives:")
    relevant_negatives = []
    seen_negative_names = set()

    # Symptoms explicitly extracted with present=false.
    for symptom in symptoms:
        if not isinstance(symptom, dict):
            continue
        if symptom.get("present") is not False:
            continue
        name = symptom.get("name")
        if not name:
            continue
        timing = symptom.get("timing")
        display_name = f"{name} ({timing})" if timing else name
        key = str(display_name).strip().lower()
        if key not in seen_negative_names:
            seen_negative_names.add(key)
            relevant_negatives.append(display_name)

    # Items Qwen placed directly in negative_symptoms.
    for symptom in negative_symptoms:
        if not isinstance(symptom, dict):
            continue
        name = symptom.get("name")
        if not name:
            continue
        timing = symptom.get("timing")
        display_name = f"{name} ({timing})" if timing else name
        key = str(display_name).strip().lower()
        if key not in seen_negative_names:
            seen_negative_names.add(key)
            relevant_negatives.append(display_name)
    if relevant_negatives:
        for name in relevant_negatives:
            print(f" - No {name}")
    else:
        print("None mentioned")

    # PAST MEDICAL HISTORY
    print()
    print("Past Medical History:")
    if medical_history:
        for history in medical_history:
            if not isinstance(history, dict):
                continue
            condition = history.get('condition')
            status = history.get('status')
            if not condition:
                continue
            if status:
                print(f' - {condition} ({status})')
            else:
                print(f' - {condition}')
    else:
        print('None mentioned')

    # EXAMINATION
    print()
    print("Examination Findings:")
    if examination:
        for finding in examination:
            if not isinstance(finding, dict):
                continue
            name = finding.get('finding')
            status = finding.get('status')
            if not name:
                continue
            if status:
                print(f' - {name}: {status}')
            else:
                print(f' - {name}')
    else:
        print('None mentioned')

    # ASSESSMENT
    print()
    print("Assessment:")
    if assessment:
        print(assessment)
    else:
        print('Not mentioned')

    # MEDICATIONS
    print()
    print("=" * 60)
    print("MEDICATIONS")
    print("=" * 60)
    if not medications:
        print('No medications mentioned.')
    else:
        for index, medication in enumerate(
            medications,
            start=1
        ):
            if not isinstance(medication, dict):
                continue
            medication_name = medication.get('medication_name')
            generic_name = medication.get('generic_name')
            spoken_name = medication.get('spoken_name')
            status = medication.get('status')
            dose = medication.get('dose')
            frequency = medication.get('frequency')
            times_per_day = medication.get('times_per_day')
            timing = medication.get('timing')
            duration = medication.get('duration')
            route = medication.get('route')
            as_needed = medication.get('as_needed')
            review_required = medication.get('review_required')

            # Medication identity
            display_name = medication.get('display_name') or _english_medication_label(medication)
            print()
            print(f'{index}. {display_name}')
            if spoken_name and not medication_name and not generic_name:
                print(f'   Spoken name (unverified transliteration): {spoken_name}')
            if generic_name:
                print(f'   Generic: {generic_name}')
            if status:
                print(f'   Status: {status}')

            # Dose
            if dose is not None:
                print(f'   Dose: {dose}')

            # Frequency
            if frequency:
                print(f'   Frequency: {frequency}')

            # Times per day
            if times_per_day is not None:
                print(f'   Times/day: {times_per_day}')

            # Timing
            if timing:
                print(f'   Timing: {timing}')

            # As needed
            if as_needed is True:
                print('   As needed: Yes')

            # Duration
            if duration:
                print(f'   Duration: {duration}')

            # Route
            if route:
                print(f'   Route: {route}')

            # Review flag
            if review_required:
                print('   ⚠ REVIEW REQUIRED')

    # FOLLOW-UP
    print()
    print("=" * 60)
    print("FOLLOW-UP")
    print("=" * 60)
    if follow_up:
        after_days = follow_up.get('after_days')
        instructions = follow_up.get('instructions', [])
        return_immediately_if = follow_up.get('return_immediately_if', [])
        if after_days is not None:
            print(f'Routine follow-up: {after_days} days')
        if instructions:
            print('Instructions:')
            for instruction in instructions:
                print(f' - {instruction}')
        if return_immediately_if:
            print('Return immediately if:')
            for condition in return_immediately_if:
                print(f' - {condition}')
        if (
            after_days is None
            and not instructions
            and not return_immediately_if
        ):
            print('No follow-up details mentioned.')
    else:
        print('No follow-up mentioned.')

    # VALIDATION
    print()
    print("=" * 60)
    print("VALIDATION")
    print("=" * 60)
    if validation.get('review_required'):
        print('⚠ REVIEW REQUIRED')
        for warning in validation.get(
            "warnings",
            []
        ):
            print(f' - {warning}')
    else:
        print('No structural warnings.')
