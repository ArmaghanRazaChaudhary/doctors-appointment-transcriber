"""Grounded Qwen extraction and medication identity safety gate."""
import json
import re

from .config import OLLAMA_URL, QWEN_OLLAMA_MODEL
from .medicines import _normalize_med_identity, _normalize_medicine_text

SYSTEM_PROMPT = "Create a complete clinical note from the numbered SOURCE SPANS. Return only JSON matching the schema.\n\nSTRICT GROUNDING\n- SOURCE SPANS are the only factual evidence. Cite evidence_ids for every nonempty item.\n- Cite the question and its answer when both are needed. Never cite a question alone as a denial.\n- Do not reconstruct quotes; the program attaches the cited source text.\n- Use null for anything unstated. Never infer a diagnosis, medication identity, indication, route, symptom, severity, measurement, or follow-up purpose.\n- assessment must be null unless the clinician explicitly states a diagnosis or assessment.\n\nLANGUAGE\n- Every normalized field must be English: complaint, symptom names, durations, timing, history, examination, instructions, and return conditions.\n- Only medication spoken_name may contain the exact original Urdu wording. It must be a real medication name or an explicit unnamed description, never dose or instruction words.\n- review_notes must be English and contain only genuine uncertainty requiring review. Do not use review_notes as a substitute for structured facts.\n\nCLINICAL FACTS\n- Capture every symptom, time change, relevant denial, history item, smoking status, measurement, examination correction, medication event, and follow-up instruction.\n- Keep a symptom present earlier and absent later as separate entries with their own timing.\n- A short answer inherits the subject of its question. Cite both spans if they are split.\n- Do not assign the complaint duration to phlegm, breathing, fever, or another finding unless that duration is explicitly stated for it.\n- Report an uncertain temperature as patient-reported and unconfirmed. Do not turn it into an objective measurement.\n- A usual systolic range is history, not a current BP reading. Preserve complete BP pairs and corrections in order.\n- Preserve corrected examinations in order. Do not use an earlier corrected finding as the assessment.\n- Normal breathing is not specifically a denial of wheezing. Reduced sleep is not fatigue.\n- Former smoking is history. A clear answer denying diabetes or asthma is a negative history item.\n\nMEDICATIONS\n- Create a separate entry for every past, current, new, and continued medication, including unnamed drugs.\n- medication_name and generic_name may use only values supplied in authoritative resolver identities. Otherwise both must be null.\n- status meanings: past = an earlier dose; current = ongoing use; new = newly prescribed; continue = explicit instruction to continue.\n- Keep each medication's dose, frequency, timing, PRN status, duration and route tied to that medication's own cited spans.\n- Do not copy “when needed” from a following medicine onto the preceding medicine.\n- “روزانہ ایک” means once daily and times_per_day 1. “At night when needed” is PRN timing, with frequency null.\n- Do not infer oral route from “take,” a tablet, dose units, or after-meal timing.\n- An earlier single dose is not continuation and is not automatically PRN.\n\nFOLLOW-UP\n- Preserve the routine interval and every clearly spoken immediate-return condition.\n- If an ASR word naming a return condition is unclear, use “Unclear symptom recurs (verify recording)” and add an English review note. Do not guess the symptom.\n- Keep medication instructions inside medications. Do not invent a test or purpose for a generic checkup.\n\nFINAL CHECK\nRead all spans again. Check that facts recorded in review_notes also appear in their correct structured section. Check every medication independently. Check English normalization, corrections, time changes, numbers, and follow-up before returning JSON."

MEDICATION_PROMPT = "Extract medication events only from the numbered source spans. Return JSON matching the schema. Create one row for each distinct medication mention or instruction. Never combine a dose from one medicine with the schedule, duration, or status of another. A later instruction to continue an unnamed disease medicine is its own row unless the source explicitly names it. Cite only the shortest spans that contain that medicine and its own instructions. medication_name and generic_name must be null unless the resolver supplies that exact identity. Keep spoken_name verbatim; all other text must be English. Do not infer route or indication. Use null where a detail is absent and record real ASR ambiguity in review_notes."


def _transcript_sources(transcript):
    """Small, ordered source spans; IDs avoid fabricated or spliced quotations."""
    # Resolver annotations are metadata, not words spoken in the consultation.
    clean_transcript = re.sub(r'\[[^\[\]]+\]', '', transcript)
    words = list(re.finditer(r'\S+', clean_transcript))
    sources = {}
    for offset in range(0, len(words), 12):
        group = words[offset:offset + 20]
        sources[f'S{len(sources) + 1:02d}'] = clean_transcript[group[0].start():group[-1].end()]
    return sources

def _clinical_output_schema(annotated_transcript):
    text = {"type": ["string", "null"]}
    integer = {"type": ["integer", "null"]}
    boolean = {"type": ["boolean", "null"]}
    citations = {"type": "array", "items": {"enum": list(_transcript_sources(annotated_transcript))},
                 "uniqueItems": True, "minItems": 1}

    def record(properties, cited=True):
        properties = dict(properties)
        if cited:
            properties['evidence_ids'] = citations
        return {"type": "object", "properties": properties,
                "required": list(properties), "additionalProperties": False}

    def rows(properties):
        return {"type": "array", "items": record(properties)}

    annotations = _authoritative_medicine_annotations(annotated_transcript)
    brands = sorted({item['brand'] for item in annotations if item.get('brand')})
    generics = sorted({name for item in annotations for name in item.get('generics', [])})
    return record({
        'chief_complaint': record({'name': text, 'duration_text': text}),
        'symptoms': rows({'name': text, 'present': boolean, 'duration_text': text,
                          'severity': text, 'timing': text}),
        'negative_symptoms': rows({'name': text, 'timing': text}),
        'medical_history': rows({'condition': text, 'status': text}),
        'medications': rows({
            'spoken_name': text, 'medication_name': {'enum': [None] + brands},
            'generic_name': {'enum': [None] + generics},
            'status': {'enum': [None, 'past', 'current', 'new', 'continue']},
            'dose': text, 'frequency': text, 'times_per_day': integer, 'timing': text,
            'as_needed': boolean, 'duration': text, 'route': text, 'indication': text,
        }),
        'examination': rows({'finding': text, 'status': text}),
        'assessment': text,
        'follow_up': record({'after_days': integer,
                             'return_immediately_if': {'type': 'array', 'items': {'type': 'string'}},
                             'instructions': {'type': 'array', 'items': {'type': 'string'}}}),
        'review_notes': {'type': 'array', 'items': {'type': 'string'}},
    }, cited=False)


def _medication_output_schema(annotated_transcript):
    full = _clinical_output_schema(annotated_transcript)
    properties = full['properties']
    return {"type": "object", "properties": {
        "medications": properties['medications'], "review_notes": properties['review_notes']
    }, "required": ["medications", "review_notes"], "additionalProperties": False}


def _check_response_shape(value, schema, path="note"):
    """Validate the small JSON Schema subset used above, without a dependency."""
    if "enum" in schema:
        if value not in schema["enum"]:
            raise ValueError(f"{path}: value is outside the allowed choices")
        return
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    actual = ("null" if value is None else "boolean" if isinstance(value, bool)
              else "integer" if isinstance(value, int) else "string" if isinstance(value, str)
              else "object" if isinstance(value, dict) else "array" if isinstance(value, list)
              else "unsupported")
    if actual not in kinds:
        raise ValueError(f"{path}: expected {kind}, got {actual}")
    if actual == "object":
        properties = schema["properties"]
        missing = set(schema.get("required", [])) - value.keys()
        extra = value.keys() - properties.keys()
        if missing or (extra and schema.get("additionalProperties") is False):
            raise ValueError(f"{path}: missing fields {sorted(missing)}, unexpected fields {sorted(extra)}")
        for key, child in value.items():
            if key in properties:
                _check_response_shape(child, properties[key], path + "." + key)
    elif actual == "array":
        for index, item in enumerate(value):
            _check_response_shape(item, schema["items"], f"{path}[{index}]")


def _extract_json_from_response(content):
    """Read a complete JSON object; never repair or silently trim partial output."""
    if not isinstance(content, str):
        raise ValueError("Qwen returned non-text content.")
    content = content.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        if lines and lines[-1].strip() == "```":
            content = "\n".join(lines[1:-1]).strip()
    start = content.find("{")
    if start < 0:
        raise ValueError("Qwen did not return a JSON object.")
    result = json.loads(content[start:])
    if not isinstance(result, dict):
        raise ValueError("Qwen's response must be a JSON object.")
    return result


def _request_grounded_note(messages, schema, label):
    import requests
    import time

    last_error = None
    for attempt, (context, limit) in enumerate(((12288, 4096), (24576, 8192)), 1):
        try:
            response = requests.post(
                OLLAMA_URL,
                json={'model': QWEN_OLLAMA_MODEL, 'messages': messages, 'stream': False,
                      'think': False, 'keep_alive': '10m', 'format': schema,
                      'options': {'temperature': 0, 'num_ctx': context, 'num_predict': limit}},
                timeout=300,
            )
        except requests.exceptions.Timeout as exc:
            raise RuntimeError(f'{label} timed out; no clinical note was saved.') from exc
        except requests.exceptions.ConnectionError as exc:
            raise RuntimeError('Cannot connect to Ollama; no clinical note was saved.') from exc
        if not response.ok:
            last_error = RuntimeError(f'Ollama returned {response.status_code}: {response.text}')
            if attempt == 1 and response.status_code >= 500:
                print(f'[QWEN] Ollama worker failed; retrying once: {response.status_code}', flush=True)
                time.sleep(2)
                continue
            raise last_error
        try:
            payload = response.json()
            reason = payload.get('done_reason', 'unknown')
            print(f"[QWEN] {label}, attempt {attempt}: finish={reason}, "
                  f"input={payload.get('prompt_eval_count')}, output={payload.get('eval_count')}", flush=True)
            if payload.get('done') is not True or reason == 'length':
                raise ValueError('generation ended before completion')
            note = _extract_json_from_response(payload.get('message', {}).get('content', ''))
            _check_response_shape(note, schema)
            return note
        except (ValueError, TypeError, AttributeError) as exc:
            last_error = exc
            if attempt == 1:
                print(f'[QWEN] Retrying incomplete or invalid output: {exc}', flush=True)
    raise RuntimeError(f'{label} failed after two attempts: {last_error}. No partial note was saved.')


def release_qwen_model():
    """Release Ollama GPU memory after all related extraction calls finish."""
    import requests

    try:
        requests.post(
            OLLAMA_URL,
            json={'model': QWEN_OLLAMA_MODEL, 'messages': [], 'stream': False, 'keep_alive': 0},
            timeout=30,
        )
    except requests.RequestException:
        pass

def _attach_source_evidence(note, sources):
    for section in ('chief_complaint', 'symptoms', 'negative_symptoms', 'medical_history',
                    'medications', 'examination', 'follow_up'):
        items = note[section] if isinstance(note[section], list) else [note[section]]
        for item in items:
            ids = sorted(set(item.get('evidence_ids', [])))
            item['evidence_ids'] = ids
            item['evidence_quotes'] = [sources[identifier] for identifier in ids]
            item['evidence'] = ' '.join(item['evidence_quotes'])
    return note

def generate_clinical_note(annotated_transcript):
    sources = _transcript_sources(annotated_transcript)
    source_text = '\n'.join(f'{key}: {value}' for key, value in sources.items())
    schema = _clinical_output_schema(annotated_transcript)
    print('\nQWEN CLINICAL EXTRACTION', flush=True)
    resolver_data = _authoritative_medicine_annotations(annotated_transcript)
    resolver_text = json.dumps(resolver_data, ensure_ascii=False)

    common = 'Resolver identities (authoritative):\n' + resolver_text + '\nSource spans:\n' + source_text
    draft = _request_grounded_note(
        [{'role': 'system', 'content': SYSTEM_PROMPT},
         {'role': 'user', 'content': 'Extract the complete consultation and check every source span.\n' + common}],
        schema, 'extraction',
    )
    audit_prompt = (
        'Return a corrected FULL note after checking the draft against every source span. The draft '
        'may omit facts or attach details to the wrong item. Check every symptom and time change; '
        'former smoking; denied history; reported measurement or range; examination correction; '
        'every medication with only its own dose, schedule, timing and duration; and every follow-up '
        'condition. A question alone does not establish a positive or negative finding. Preserve '
        'uncertain ASR wording as uncertainty. Use English for every normalized field; only medication '
        'spoken_name may remain verbatim. Never add a diagnosis, route or indication.\n\n' + common +
        '\n\nUNTRUSTED DRAFT:\n' + json.dumps(draft, ensure_ascii=False)
    )
    audited = _request_grounded_note(
        [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': audit_prompt}],
        schema, 'source audit',
    )
    medication_result = _request_grounded_note(
        [{'role': 'system', 'content': MEDICATION_PROMPT},
         {'role': 'user', 'content': common}],
        _medication_output_schema(annotated_transcript), 'medication extraction',
    )

    def coverage_score(note):
        cited = set()
        item_count = 0
        for section in ('chief_complaint', 'symptoms', 'negative_symptoms', 'medical_history',
                        'medications', 'examination', 'follow_up'):
            items = note.get(section) or []
            items = items if isinstance(items, list) else [items]
            for item in items:
                if isinstance(item, dict):
                    cited.update(item.get('evidence_ids') or [])
                    item_count += 1
        return len(cited) * 20 + item_count

    combined = audited if coverage_score(audited) >= coverage_score(draft) else draft
    print(f"[QWEN] retained {'audited' if combined is audited else 'initial'} extraction "
          f"(coverage score {coverage_score(combined)})", flush=True)
    combined['review_notes'] = list(dict.fromkeys(
        (draft.get('review_notes') or []) + (audited.get('review_notes') or [])
    ))
    combined['medications'] = medication_result['medications']
    translation_prompt = (
        'Translate the JSON string values into concise clinical English without changing, adding, '
        'removing or reclassifying any fact, number, null, list item or evidence ID. Preserve medication '
        'spoken_name exactly in its original language. Keep medication_name and generic_name unchanged. '
        'Translate review_notes into English. Return the identical schema as JSON only.\n\nJSON:\n' +
        json.dumps(combined, ensure_ascii=False)
    )
    normalized = _request_grounded_note(
        [{'role': 'system', 'content': 'You translate structured clinical JSON. Preserve its facts and structure exactly.'},
         {'role': 'user', 'content': translation_prompt}],
        schema, 'English normalization',
    )
    if len(normalized.get('medications') or []) == len(combined.get('medications') or []):
        for translated, original in zip(normalized['medications'], combined['medications']):
            translated['spoken_name'] = original.get('spoken_name')
            translated['medication_name'] = original.get('medication_name')
            translated['generic_name'] = original.get('generic_name')
    return _attach_source_evidence(normalized, sources)


def _authoritative_medicine_annotations(annotated_transcript):
    """
    Extract ONLY medicine identities inserted by annotate_medications().

    Current annotation format:
        spoken[Brand/Generic]
        spoken[Brand]

    These annotations are the only authority for medication identity.
    """
    annotations = []
    for match in re.finditer(r"\[([^\[\]/]+?)(?:/([^\[\]]+?))?\]", annotated_transcript or ""):
        brand = match.group(1).strip() if match.group(1) else None
        generic_text = match.group(2).strip() if match.group(2) else None
        generics = []
        if generic_text:
            generics = [item.strip() for item in generic_text.split(',') if item.strip()]
        annotations.append({'brand': brand, 'generics': generics})
    return annotations


def enforce_authoritative_medication_identities(data, annotated_transcript):
    """
    Hard safety boundary between Qwen and medication identity.

    Qwen may extract medication instructions, but medication_name/generic_name
    are accepted only if they exactly match an identity inserted by the DRAP
    resolver. Any unsupported identity is removed deterministically.
    """
    if not isinstance(data, dict):
        return data
    annotations = _authoritative_medicine_annotations(annotated_transcript)
    allowed_brands = {
        _normalize_med_identity(item["brand"]): item["brand"]
        for item in annotations
        if item.get("brand")
    }
    allowed_generics = {}
    for item in annotations:
        for generic in item.get("generics", []):
            allowed_generics[_normalize_med_identity(generic)] = generic
    rejected_names = set()
    medications = data.get("medications", [])
    if not isinstance(medications, list):
        return data
    for medication in medications:
        if not isinstance(medication, dict):
            continue

        # Normalize schema vocabulary before validation. This changes only
        # representation, not clinical meaning.
        status = medication.get("status")
        if isinstance(status, str):
            status_key = status.strip().lower().replace("-", "_").replace(" ", "_")
            if status_key in {"newly_prescribed", "new_prescription", "prescribed", "start", "started"}:
                medication["status"] = "new"
            elif status_key in {"continue", "continued", "continuing"}:
                medication["status"] = "continue"
        frequency = medication.get("frequency")
        times_per_day = medication.get("times_per_day")
        if times_per_day is None and isinstance(frequency, str):
            frequency_key = frequency.strip().lower().replace("_", " ")
            if frequency_key == "once daily":
                medication["times_per_day"] = 1
            elif frequency_key == "twice daily":
                medication["times_per_day"] = 2
            elif frequency_key == "three times daily":
                medication["times_per_day"] = 3
        med_name = medication.get("medication_name")
        generic_name = medication.get("generic_name")
        med_key = _normalize_med_identity(med_name)
        generic_key = _normalize_med_identity(generic_name)
        med_supported = bool(med_key and med_key in allowed_brands)
        generic_supported = bool(generic_key and generic_key in allowed_generics)

        # Canonicalize identities that really did come from the resolver.
        if med_supported:
            medication["medication_name"] = allowed_brands[med_key]
        if generic_supported:
            medication["generic_name"] = allowed_generics[generic_key]

        # Reject anything Qwen invented or normalized on its own.
        if med_name and not med_supported:
            rejected_names.add(str(med_name).strip())
            medication["medication_name"] = None
            medication["identity_rejected"] = True
            medication["review_required"] = True
        if generic_name and not generic_supported:
            rejected_names.add(str(generic_name).strip())
            medication["generic_name"] = None
            medication["identity_rejected"] = True
            medication["review_required"] = True

        # If there are no resolver annotations at all, no medication identity
        # from Qwen is allowed under any circumstance.
        if not annotations:
            medication["medication_name"] = None
            medication["generic_name"] = None

    # Qwen sometimes repeats a hallucinated medicine name inside follow-up.
    # Remove those duplicated unsafe instructions as well.
    follow_up = data.get("follow_up")
    if isinstance(follow_up, dict) and rejected_names:
        instructions = follow_up.get("instructions", [])
        if isinstance(instructions, list):
            safe_instructions = []
            for instruction in instructions:
                text = str(instruction)
                lowered = text.lower()
                if any(name.lower() in lowered for name in rejected_names if name):
                    continue
                safe_instructions.append(instruction)
            follow_up["instructions"] = safe_instructions
    return data
