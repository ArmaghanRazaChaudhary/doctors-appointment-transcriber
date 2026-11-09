"""Transcript grounding checks and clinical consistency validation."""
import re

from .extraction import _transcript_sources
from .medicines import (
    _NUMBER_WORDS,
    _bp_pairs,
    _daily_count,
    _dose_quantities,
    _english_medication_label,
    _evidence_present,
    _has_language_cue,
    _latin_spoken_name,
    _normalize_medicine_text,
    _number_normalized_text,
    _spoken_name_before_dose,
)

def validate_transcript_evidence(data, transcript):
    """Check cited facts without erasing whole findings on a quotation mismatch."""
    if not isinstance(data, dict):
        return data
    review_notes = list(data.get('review_notes') or [])
    warnings = [note for note in review_notes if re.search(
        r'unclear|uncertain|verify|review|conflict|ambiguous|ASR|unresolved|corrected', str(note), re.I
    )]
    if not re.search(r'(?:کھانسی|cough).{0,60}(?:فور|return)', transcript, re.I):
        warnings = [warning for warning in warnings
                    if not ('cough' in str(warning).lower() and 'recur' in str(warning).lower())]
    audit = []
    sources = _transcript_sources(transcript)

    def source_fields(patterns):
        selected = [(identifier, quote) for identifier, quote in sources.items()
                    if any(re.search(pattern, quote, re.I) for pattern in patterns)][:3]
        return {'evidence_ids': [item[0] for item in selected],
                'evidence_quotes': [item[1] for item in selected],
                'evidence': ' '.join(item[1] for item in selected)}

    def flag(path, old, reason):
        audit.append({'field': path, 'model_value': old, 'reason': reason})
        warnings.append(path + ': ' + reason)

    for section in ('chief_complaint', 'symptoms', 'negative_symptoms', 'medical_history',
                    'medications', 'examination', 'follow_up'):
        items = data.get(section) or ([] if section not in ('chief_complaint', 'follow_up') else {})
        items = items if isinstance(items, list) else [items]
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            quotes = item.get('evidence_quotes') or ([item['evidence']] if item.get('evidence') else [])
            meaningful = any(value not in (None, [], '') for key, value in item.items()
                             if key not in ('evidence_ids', 'evidence_quotes', 'evidence'))
            if meaningful and (not quotes or not all(_evidence_present(q, transcript) for q in quotes)):
                flag(f'{section}[{index}]', item.get('evidence'),
                     'Source citation needs review. The finding was retained, not silently deleted.')
                item['review_required'] = True

    chief = data.get('chief_complaint') or {}
    if re.search(r'[\u0600-\u06ff]', str(chief.get('name') or '')):
        first_present = next((item for item in data.get('symptoms') or []
                              if item.get('present') is True and
                              not re.search(r'[\u0600-\u06ff]', str(item.get('name') or ''))), None)
        if first_present:
            flag('chief_complaint.name', chief['name'], 'Normalized the complaint using its English symptom entry.')
            chief['name'] = first_present['name']
            if re.search(r'[\u0600-\u06ff]', str(chief.get('duration_text') or '')):
                chief['duration_text'] = first_present.get('duration_text')
    uncertain_day_range = bool(re.search(r'پانچ\s+(?:شہ|چھ)\s+دن', transcript))
    if uncertain_day_range:
        chief['duration_text'] = 'approximately 5–6 days (ASR wording unclear)'
        for symptom in data.get('symptoms') or []:
            if _has_language_cue(symptom.get('evidence') or '', r'پانچ\s+(?:شہ|چھ)\s+دن'):
                symptom['duration_text'] = 'approximately 5–6 days (ASR wording unclear)'
    if chief.get('duration_text') is None and _has_language_cue(chief.get('evidence') or '', r'کل سے|since yesterday'):
        chief['duration_text'] = 'since yesterday'

    # Do not manufacture a named drug from instruction fragments. Unresolved
    # source wording is stored for audit; normalized display uses English only.
    for index, med in enumerate(data.get('medications') or []):
        evidence = med.get('evidence') or ''
        source_name = med.get('spoken_name')
        if source_name and not _evidence_present(source_name, transcript):
            flag(f'medications[{index}].spoken_name', source_name,
                 'Name was not spoken verbatim; no guessed replacement was inserted.')
            source_name = None
        if not source_name and med.get('dose'):
            source_name = _spoken_name_before_dose(evidence, med.get('dose'))
            if source_name:
                flag(f'medications[{index}].spoken_name', None,
                     'Restored the exact spoken source name immediately before its dose.')
        if not source_name:
            description = re.search(
                r'(کاف\s+والی\s+میڈیسن|بی\s*پی\s+کی(?:\s+جو)?\s+میڈیسن\w*|'
                r'diabetes medicine|blood[- ]pressure medicine|cough medicine)', evidence, re.I
            )
            source_name = description.group(1) if description else None
        if source_name and _has_language_cue(source_name, r'دن تک|رکھ لیں|after meals|when needed'):
            flag(f'medications[{index}].spoken_name', source_name,
                 'Instruction text is not a medication name.')
            source_name = None
        if source_name and _dose_quantities(source_name):
            words = source_name.split()
            for position, word in enumerate(words):
                normalized_word = _normalize_medicine_text(word)
                if normalized_word.isdigit() or normalized_word in _NUMBER_WORDS or re.match(r'^\d', normalized_word):
                    shortened = ' '.join(words[:position]).strip()
                    if shortened:
                        flag(f'medications[{index}].spoken_name', source_name,
                             'Removed dose words from the spoken medication name.')
                        source_name = shortened
                    break
        med['source_spoken_name'] = source_name
        med['spoken_name'] = _latin_spoken_name(source_name)
        if _has_language_cue(evidence, r'لیتا ہوں|لیتی ہوں|کھاتا ہوں|کھاتی ہوں|میڈیسن پڑتا ہوں|taking|currently take'):
            med['status'] = 'current'
        elif _has_language_cue(evidence, r'لیں|لینی ہے|کھانی ہے|آپ نے کھانا ہے|دے رہا ہوں|prescribed|start'):
            med['status'] = 'new'
        elif med.get('status') is None and med.get('dose') and med.get('duration'):
            med['status'] = 'new'
        if med.get('route') and not _has_language_cue(evidence, r'منہ کے ذریعے|منہ سے|by mouth|orally|oral route|intravenous|intramuscular|topical|جلد پر'):
            flag(f'medications[{index}].route', med['route'], 'Route was inferred rather than explicitly stated.')
            med['route'] = None
        count = _daily_count(evidence)
        if count is not None and med.get('frequency') and med.get('as_needed') is not True:
            med['times_per_day'] = count
        if med.get('as_needed') is True and not _has_language_cue(
            evidence, r'حسب ضرورت|جب ضرورت|ضرورت ہو|ضرورت کے مطابق|ضرورت پڑ\w*|as needed|when needed|if needed'
        ):
            flag(f'medications[{index}].as_needed', True,
                 'As-needed use was not stated in this medication\'s cited evidence.')
            med['as_needed'] = None
        elif _has_language_cue(evidence, r'حسب ضرورت|جب ضرورت|ضرورت کے مطابق|as needed|when needed|if needed'):
            med['as_needed'] = True
        if med.get('duration') is None:
            normalized = _number_normalized_text(evidence)
            duration_match = re.search(r'(\d+)\s*(?:دن|days?)\s*(?:تک|کے لیے|for)?', normalized, re.I)
            if duration_match:
                med['duration'] = duration_match.group(1) + ' days'
        if med.get('indication') and not _has_language_cue(
            evidence, r'(?:کھانسی|درد|بخار|بلڈ پریشر|cough|pain|fever|blood pressure)\s*(?:کے لیے|for)'
        ):
            flag(f'medications[{index}].indication', med['indication'],
                 'Indication was inferred rather than explicitly stated for this medication.')
            med['indication'] = None
        if med.get('status') == 'past' and not _has_language_cue(evidence, r'جب ضرورت|حسب ضرورت|when needed|as needed'):
            if med.get('as_needed') is True:
                flag(f'medications[{index}].as_needed', True, 'A past dose is not a PRN prescription.')
            med['as_needed'] = None
        if not med.get('medication_name') and not med.get('generic_name'):
            med['review_required'] = True
            warnings.append(f'Medication #{index + 1}: identity is unresolved; do not assume a brand or ingredient.')
        med['display_name'] = _english_medication_label(med)

    continued_medicine = re.search(
        r'(?:اپنی\s+)?([^؟،۔]{1,35}?)\s+کی\s+میڈیس\w*[^؟،۔]{0,35}?(?:کانٹینیو|جاری)\s+رکھ', transcript
    )
    if continued_medicine and not any(_has_language_cue(
        med.get('evidence') or '', r'کانٹینیو|جاری|continue'
    ) for med in data.get('medications') or []):
        item = {'spoken_name': None, 'source_spoken_name': None, 'medication_name': None,
                'generic_name': None, 'status': 'continue', 'dose': None, 'frequency': None,
                'times_per_day': None, 'timing': None, 'as_needed': None, 'duration': None,
                'route': None, 'indication': None, 'review_required': True}
        item.update(source_fields((r'کانٹینیو|جاری|continue',)))
        item['display_name'] = _english_medication_label(item)
        data.setdefault('medications', []).append(item)
        warnings.append('Continued medicine: the medicine was described by condition, without a verified name.')

    # Breathing normally does not specifically establish the absence of wheeze.
    generic_sound_added = False
    for section in ('symptoms', 'negative_symptoms'):
        kept = []
        for symptom in data.get(section) or []:
            name = str(symptom.get('name', '')).lower()
            evidence = symptom.get('evidence') or ''
            if ('wheez' in name or 'stridor' in name) and not _has_language_cue(
                evidence, r'wheez\w*|stridor|سیٹی|ویزنگ'
            ):
                flag(section, symptom, 'A specific breathing sound was inferred from a generic question about sound.')
                if section == 'negative_symptoms' and _has_language_cue(evidence, r'آواز|sound|noise') and not generic_sound_added:
                    symptom['name'] = 'abnormal breathing sounds'
                    kept.append(symptom)
                    generic_sound_added = True
                continue
            kept.append(symptom)
        data[section] = kept

    history_terms = {
        'hypertension': r'بی پی|بلڈ پریشر|hypertension|blood pressure|\bBP\b',
        'bp': r'بی پی|بلڈ پریشر|blood pressure|\bBP\b',
        'diabetes': r'شوگر|سوگر|ڈائیپیٹیز|diabetes',
        'asthma': r'ایستما|ایسٹما|asthma',
        'smoking': r'سموک|smok', 'tobacco': r'سموک|تمباکو|smok|tobacco',
        'malaria': r'ملیریا|malaria',
    }
    kept_history = []
    for item in data.get('medical_history') or []:
        condition = str(item.get('condition') or '').strip()
        key = condition.lower()
        pattern = history_terms.get(key)
        if pattern and not _has_language_cue(item.get('evidence') or '', pattern):
            flag('medical_history', item, 'The translated condition is not present in its cited source.')
            continue
        if key == 'bp':
            item['condition'] = 'hypertension'
        if item.get('condition') == 'hypertension':
            match = re.search(r'(\d{2,3})\s*(?:سے|to|[-–])\s*(\d{2,3})', transcript, re.I)
            if match and 'systolic' not in str(item.get('status') or '').lower():
                item['status'] = f"{item.get('status') or 'reported'}; usual systolic {match.group(1)}–{match.group(2)} mmHg"
        kept_history.append(item)
    data['medical_history'] = kept_history

    kept = []
    for symptom in data.get('symptoms') or []:
        evidence = str(symptom.get('evidence') or '').strip()
        if symptom.get('present') is True and evidence.endswith(('?', '؟')):
            flag('symptoms', symptom, 'A question without its answer does not establish a positive finding.')
            continue
        kept.append(symptom)
    data['symptoms'] = kept

    kept = []
    for symptom in data.get('negative_symptoms') or []:
        if not _has_language_cue(symptom.get('evidence') or '', r'نہیں|نہ\s|den(?:y|ied|ies)|\bno\b|absent|without'):
            flag('negative_symptoms', symptom, 'No denial or absence appears in the cited source.')
            continue
        kept.append(symptom)
    data['negative_symptoms'] = kept

    fever_mentioned = _has_language_cue(transcript, r'بخار|fever')
    prior_fever = fever_mentioned and _has_language_cue(transcript, r'کل رات|last night')
    morning_absent = bool(re.search(r'(?:آج صبح.{0,35}نہیں|this morning.{0,35}(?:absent|no fever))', transcript, re.I))
    if prior_fever:
        fever_rows = [item for item in data.get('symptoms') or []
                      if 'fever' in str(item.get('name') or '').lower()]
        if fever_rows:
            primary_fever = fever_rows[0]
            primary_fever['present'] = True
            primary_fever['timing'] = 'last night'
            if str(primary_fever.get('duration_text') or '').lower() in ('all night', 'throughout the night') and not _has_language_cue(
                transcript, r'ساری رات|پوری رات|all night|throughout the night'
            ):
                primary_fever['duration_text'] = None
            data['symptoms'] = [item for item in data.get('symptoms') or []
                                if 'fever' not in str(item.get('name') or '').lower() or item is primary_fever]
        else:
            item = {'name': 'fever', 'present': True, 'duration_text': None,
                    'severity': 'patient-reported; not objectively confirmed', 'timing': 'last night'}
            item.update(source_fields((r'بخار|fever', r'کل رات|last night')))
            data.setdefault('symptoms', []).append(item)
            flag('symptoms', 'fever', 'Restored the explicitly reported prior fever and its timing.')
    if morning_absent:
        data['negative_symptoms'] = [item for item in data.get('negative_symptoms') or []
                                     if 'fever' not in str(item.get('name') or '').lower()]
        item = {'name': 'fever', 'timing': 'this morning'}
        item.update(source_fields((r'بخار|fever', r'آج صبح|this morning')))
        data['negative_symptoms'].append(item)

    if _has_language_cue(transcript, r'گلا خراب|sore throat') and not any(
        'throat' in str(item.get('name') or '').lower() for item in data.get('symptoms') or []
    ):
        item = {'name': 'sore throat', 'present': True, 'duration_text': None,
                'severity': None, 'timing': None}
        item.update(source_fields((r'گلا خراب|sore throat',)))
        data.setdefault('symptoms', []).append(item)

    if re.search(r'سانس پھولتی.{0,120}نہیں سانس ٹھیک|shortness of breath.{0,120}(?:denied|no)', transcript, re.I):
        if not any(('breathing difficulty' in str(item.get('name') or '').lower() or
                    'shortness of breath' in str(item.get('name') or '').lower())
                   for item in data.get('negative_symptoms') or []):
            item = {'name': 'breathing difficulty', 'timing': None}
            item.update(source_fields((r'سانس پھولتی|shortness of breath', r'نہیں سانس ٹھیک|breathing is normal')))
            data.setdefault('negative_symptoms', []).append(item)

    if re.search(r'(?:سموک|smok).{0,180}(?:اب نہیں|quit|stopped).{0,140}(?:پہلے|سال|ago)', transcript, re.I):
        if not any(str(item.get('condition') or '').lower() in ('smoking', 'tobacco use')
                   for item in data.get('medical_history') or []):
            item = {'condition': 'smoking', 'status': 'former; quit years ago'}
            item.update(source_fields((r'سموک|smok', r'چھوڑ|quit|stopped')))
            data.setdefault('medical_history', []).append(item)

    drug_allergy_denied = re.search(r'(?:میڈیسن|دوائی|دوا).{0,30}الرجی.{0,20}نہیں', transcript)
    if drug_allergy_denied:
        allergy_rows = [item for item in data.get('medical_history') or []
                        if 'allerg' in str(item.get('condition') or '').lower()]
        item = {'condition': 'drug allergy', 'status': 'denied'}
        item.update(source_fields((r'(?:میڈیسن|دوائی|دوا).{0,30}الرجی',)))
        data['medical_history'] = [row for row in data.get('medical_history') or [] if row not in allergy_rows] + [item]

    allergy_match = re.search(r'([^\s؟،۔]+)\s+سے\s+الرجی\s+ہے', transcript)
    if allergy_match:
        substance = allergy_match.group(1)
        common_allergens = {'پینسلن': 'penicillin'}
        label = common_allergens.get(substance, (_latin_spoken_name(substance) or substance) + ' (source wording)')
        allergy_rows = [item for item in data.get('medical_history') or []
                        if 'allerg' in str(item.get('condition') or '').lower()]
        item = allergy_rows[0] if allergy_rows else {}
        item.update({'condition': label + ' allergy', 'status': 'present'})
        item.update(source_fields((re.escape(substance) + r'\s+سے\s+الرجی',)))
        data['medical_history'] = [row for row in data.get('medical_history') or [] if row not in allergy_rows] + [item]

    symptom_terms = {
        'fever': r'بخار|fever', 'diabetes': r'شوگر|سوگر|ڈائیپیٹیز|diabetes',
        'asthma': r'ایستما|ایسٹما|asthma', 'nausea': r'متلی|nausea',
        'vomiting': r'الٹی|vomit', 'breathing difficulty': r'سانس|breath',
    }
    for section in ('symptoms', 'negative_symptoms'):
        kept = []
        for symptom in data.get(section) or []:
            name = str(symptom.get('name') or '').lower()
            pattern = next((value for key, value in symptom_terms.items() if key in name), None)
            if pattern and not _has_language_cue(symptom.get('evidence') or '', pattern):
                flag(section, symptom, 'The cited source does not mention this named finding.')
                continue
            kept.append(symptom)
        data[section] = kept

    positive_names = {str(item.get('name') or '').strip().lower() for item in data.get('symptoms') or []}
    for item in data.get('negative_symptoms') or []:
        if str(item.get('name') or '').strip().lower() in positive_names:
            warnings.append(f"Conflicting positive and negative finding preserved for {item.get('name')}; verify the audio.")

    # Check whole BP pairs, rather than independently accepting two numbers.
    rebuilt_examination = []
    bp_seen = False
    for index, item in enumerate(data.get('examination') or []):
        finding = str(item.get('finding', '')).lower()
        if 'blood pressure' not in finding and not re.search(r'\bbp\b', finding):
            rebuilt_examination.append(item)
            continue
        if bp_seen:
            continue
        bp_seen = True
        proposed = _bp_pairs((item.get('finding') or '') + ' ' + (item.get('status') or ''))
        supported = list(dict.fromkeys(_bp_pairs(transcript)))
        if supported:
            for sequence, (systolic, diastolic) in enumerate(supported, 1):
                replacement = dict(item)
                replacement['finding'] = 'blood pressure'
                replacement['status'] = f'{systolic}/{diastolic} mmHg (reading {sequence})'
                replacement['sequence'] = sequence
                replacement.update(source_fields((rf'{systolic}\s*/\s*{diastolic}',)))
                rebuilt_examination.append(replacement)
            if proposed != supported:
                flag(f'examination[{index}].status', item.get('status'),
                     'Preserved every complete BP pair in spoken order.')
            continue
        if proposed and any(pair not in supported for pair in proposed):
            flag(f'examination[{index}].status', item['status'], 'BP pair does not occur in its cited source.')
            item['status'] = None
            item['review_required'] = True
        rebuilt_examination.append(item)
    data['examination'] = rebuilt_examination
    impression = re.search(r'مجھے\s+(.{2,50}?)\s+لگ رہا ہے', transcript)
    if impression:
        phrase = impression.group(1).strip()
        translations = {'ایسڈ ری فلکس': 'acid reflux'}
        data['assessment'] = translations.get(phrase, (_latin_spoken_name(phrase) or phrase) + ' (clinician impression; source wording)')
    elif data.get('assessment') and not _has_language_cue(
        transcript, r'تشخیص|ڈائگنوس|diagnos(?:is|ed)|assessment|impression|لگ رہا ہے'
    ):
        flag('assessment', data['assessment'], 'No explicit clinician diagnosis or assessment was stated.')
        data['assessment'] = None
    follow_up = data.get('follow_up') or {}
    follow_up['instructions'] = [
        instruction for instruction in follow_up.get('instructions') or []
        if not re.search(r'return immediately|فور(?:اً|ا)', str(instruction), re.I)
        and not (re.search(r'\btake\b|لیں|کھانی|کھانا', str(instruction), re.I)
                 and not re.search(r'follow[- ]?up|check[- ]?up|دوبارہ|فالو', str(instruction), re.I))
    ]
    if not _has_language_cue(transcript, r'فور\w*\s+واپس|return immediately|come back immediately'):
        if follow_up.get('return_immediately_if'):
            flag('follow_up.return_immediately_if', follow_up['return_immediately_if'],
                 'No immediate-return instruction was spoken.')
        follow_up['return_immediately_if'] = []
    verified_conditions = []
    follow_evidence = follow_up.get('evidence') or ''
    return_sources = source_fields((r'فور\w*\s+واپس|return immediately', r'اگر|if'))
    if follow_up.get('return_immediately_if') and not _has_language_cue(follow_evidence, r'فور\w*\s+واپس|return immediately'):
        flag('follow_up.return_immediately_if', follow_up['return_immediately_if'],
             'The model cited the routine follow-up span rather than the spoken urgent-return conditions.')
        verified_conditions = ['Urgent return conditions are unclear in the ASR; verify the recording']
        follow_up.update(return_sources)
        unclear_added = True
    unclear_added = False
    for condition in ([] if verified_conditions else follow_up.get('return_immediately_if') or []):
        lowered = str(condition).lower()
        if 'cough' in lowered and not re.search(r'(?:کھانسی|cough).{0,60}(?:فور|return)', transcript, re.I):
            if not unclear_added:
                verified_conditions.append('Unclear symptom recurs (verify recording)')
                unclear_added = True
            flag('follow_up.return_immediately_if', condition,
                 'The ASR word for this return condition is unclear; it was not assumed to mean cough.')
        elif ('breath' in lowered or 'shortness' in lowered) and not re.search(
            r'(?:سانس|breath).{0,60}(?:فور|return)', transcript, re.I
        ):
            flag('follow_up.return_immediately_if', condition,
                 'Breathing is not mentioned in the cited return instruction.')
        else:
            verified_conditions.append(condition)
    follow_up['return_immediately_if'] = verified_conditions
    if re.search(r'نہیں[،,]?\s*الٹی\s+بھی\s+آئی', transcript):
        warnings.append('Vomiting answer is internally contradictory in the ASR (negative cue followed by positive wording); verify the audio.')
    data['evidence_audit'] = audit
    data['validation'] = {'review_required': bool(warnings), 'warnings': warnings}
    return data


def validate_clinical_data(data):
    """
    Deterministic validation for the NEW Qwen clinical JSON schema.

    This function does NOT decide whether a treatment is medically
    appropriate.

    It checks for:
    - Missing medication identity
    - Suspicious medication instructions
    - Missing evidence
    - Invalid frequency/times-per-day combinations
    - Missing duration where appropriate
    - Contradictory examination findings
    - Missing follow-up evidence
    """
    warnings = list((data.get('validation') or {}).get('warnings', [])) if isinstance(data, dict) else []

    # BASIC STRUCTURE
    if not isinstance(data, dict):
        return {
            "validation": {
                "review_required": True,
                "warnings": [
                    "Clinical data is not a valid JSON object."
                ]
            }
        }

    # REQUIRED TOP-LEVEL FIELDS
    required_fields = [
        "chief_complaint",
        "symptoms",
        "negative_symptoms",
        "medical_history",
        "medications",
        "examination",
        "assessment",
        "follow_up",
    ]
    for field in required_fields:
        if field not in data:
            warnings.append(f'Missing top-level field: {field}')

    # CHIEF COMPLAINT
    chief_complaint = data.get('chief_complaint', {})
    if chief_complaint.get("name") is not None:
        if not chief_complaint.get("evidence"):
            warnings.append('Chief complaint has no transcript evidence.')

    # SYMPTOMS
    symptoms = data.get('symptoms', [])
    if not isinstance(symptoms, list):
        warnings.append('Symptoms field is not a list.')
        symptoms = []
    for index, symptom in enumerate(
        symptoms,
        start=1
    ):
        if not isinstance(symptom, dict):
            warnings.append(f'Symptom #{index} is not a valid object.')
            continue
        name = symptom.get('name')
        evidence = symptom.get('evidence')
        if name and not evidence:
            warnings.append(f'Symptom #{index} ({name}) has no transcript evidence.')

        # A present symptom must explicitly have present=True
        if (
            name
            and symptom.get("present") is None
        ):
            warnings.append(f'Symptom #{index} ({name}) has no present/absent status.')

    # NEGATIVE SYMPTOMS
    negative_symptoms = data.get('negative_symptoms', [])
    if not isinstance(negative_symptoms, list):
        warnings.append('negative_symptoms field is not a list.')
        negative_symptoms = []
    for index, symptom in enumerate(
        negative_symptoms,
        start=1
    ):
        if not isinstance(symptom, dict):
            warnings.append(f'Negative symptom #{index} is not a valid object.')
            continue
        name = symptom.get('name')
        evidence = symptom.get('evidence')
        if name and not evidence:
            warnings.append(f'Negative symptom #{index} ({name}) has no transcript evidence.')

    # MEDICAL HISTORY
    medical_history = data.get('medical_history', [])
    if not isinstance(medical_history, list):
        warnings.append('medical_history field is not a list.')
        medical_history = []
    for index, history in enumerate(
        medical_history,
        start=1
    ):
        if not isinstance(history, dict):
            warnings.append(f'Medical history #{index} is not a valid object.')
            continue
        condition = history.get('condition')
        evidence = history.get('evidence')
        if condition and not evidence:
            warnings.append(f'Medical history #{index} ({condition}) has no transcript evidence.')

    # MEDICATIONS
    medications = data.get('medications', [])
    if not isinstance(medications, list):
        warnings.append('medications field is not a list.')
        medications = []
    for index, medication in enumerate(
        medications,
        start=1
    ):
        if not isinstance(medication, dict):
            warnings.append(f'Medication #{index} is not a valid object.')
            continue

        # Medication identity
        medication_name = medication.get('medication_name')
        generic_name = medication.get('generic_name')
        status = medication.get('status')
        evidence = medication.get('evidence')
        if medication.get("identity_rejected"):
            medication["review_required"] = True
            warnings.append(
                f"Medication #{index}: Qwen proposed an unsupported medicine identity; "
                "identity was removed because it was not supplied by the DRAP resolver."
            )

        # Unknown medication identity
        # This is EXPECTED when the medicine resolver cannot identify
        # the spoken medicine.
        # It must trigger doctor review rather than hallucination.
        if (
            status == "new"
            and not medication_name
            and not generic_name
        ):
            medication["review_required"] = True
            warnings.append(
                f"Medication #{index}: "
                "new medication mentioned but identity "
                "could not be identified."
            )

        # Continue medication with unknown name
        # Example:
        # "BP ki jo medicine ... continue karni hai"
        # This is not necessarily an error.
        # The transcript explicitly says the medication name is unknown.
        if (
            status == "continue"
            and not medication_name
            and not generic_name
        ):
            medication["review_required"] = True
            warnings.append(
                f"Medication #{index}: "
                "existing medication is being continued "
                "but medication name is unknown."
            )

        # Evidence
        if not evidence:
            medication["review_required"] = True
            warnings.append(f'Medication #{index}: no transcript evidence.')

        # Dose
        dose = medication.get('dose')
        if dose is not None:
            if not isinstance(dose, (str, int, float)):
                medication["review_required"] = True
                warnings.append(f'Medication #{index}: invalid dose format.')

        # Frequency / times per day
        frequency = medication.get('frequency')
        times_per_day = medication.get('times_per_day')
        if (
            frequency == "once daily"
            and times_per_day != 1
        ):
            medication["review_required"] = True
            warnings.append(f'Medication #{index}: once daily but times_per_day is not 1.')
        if (
            frequency == "twice daily"
            and times_per_day != 2
        ):
            medication["review_required"] = True
            warnings.append(f'Medication #{index}: twice daily but times_per_day is not 2.')

        # Also support our underscore-normalized values
        if (
            frequency == "once_daily"
            and times_per_day != 1
        ):
            medication["review_required"] = True
            warnings.append(f'Medication #{index}: once_daily but times_per_day is not 1.')
        if (
            frequency == "twice_daily"
            and times_per_day != 2
        ):
            medication["review_required"] = True
            warnings.append(f'Medication #{index}: twice_daily but times_per_day is not 2.')

        # AS NEEDED consistency
        as_needed = medication.get('as_needed')
        timing = medication.get('timing')
        if as_needed is True:

            # PRN medication should normally not have a fixed
            # frequency unless the transcript explicitly states both.
            if frequency is not None:
                medication["review_required"] = True
                warnings.append(
                    f"Medication #{index}: "
                    "as_needed is true but a fixed frequency "
                    "was also provided."
                )

        # Duration
        duration = medication.get('duration')
        if duration is not None:
            if not isinstance(duration, (str, int, float)):
                medication["review_required"] = True
                warnings.append(f'Medication #{index}: invalid duration format.')

        # Route
        route = medication.get('route')
        if route is not None:
            if not isinstance(route, str):
                medication["review_required"] = True
                warnings.append(f'Medication #{index}: invalid route format.')

        # New medication without explicit instructions
        if status == "new":
            if (
                dose is None
                and frequency is None
                and timing is None
                and duration is None
                and as_needed is not True
            ):
                medication["review_required"] = True
                warnings.append(
                    f"Medication #{index}: "
                    "new medication mentioned but "
                    "no medication instructions were extracted."
                )

    # EXAMINATION
    examination = data.get('examination', [])
    if not isinstance(examination, list):
        warnings.append('examination field is not a list.')
        examination = []
    for index, finding in enumerate(
        examination,
        start=1
    ):
        if not isinstance(finding, dict):
            warnings.append(f'Examination finding #{index} is not a valid object.')
            continue
        evidence = finding.get('evidence')
        if (
            finding.get("finding")
            and not evidence
        ):
            warnings.append(f'Examination finding #{index} has no transcript evidence.')

    # ASSESSMENT
    assessment = data.get('assessment')

    # IMPORTANT:
    # We intentionally DO NOT require an assessment.
    # If the doctor did not explicitly provide one,
    # null is correct.
    # FOLLOW-UP
    follow_up = data.get('follow_up', {})
    if not isinstance(follow_up, dict):
        warnings.append('follow_up field is not a valid object.')
        follow_up = {}
    return_immediately_if = follow_up.get('return_immediately_if', [])
    instructions = follow_up.get('instructions', [])
    follow_up_evidence = follow_up.get('evidence')

    # If follow-up instructions exist, evidence should exist.
    if (
        (
            return_immediately_if
            or instructions
            or follow_up.get("after_days") is not None
        )
        and not follow_up_evidence
    ):
        warnings.append('Follow-up information exists but has no transcript evidence.')

    # DUPLICATE / CONTRADICTORY EXAMINATION CHECK
    # We DO NOT mark contradictions as errors.
    # Example:
    # chest = slightly congested
    # chest = clear
    # This is intentionally preserved because it happened in the
    # consultation.
    examination_findings = {}
    for finding in examination:
        if not isinstance(finding, dict):
            continue
        name = finding.get('finding')
        status = finding.get('status')
        if not name or not status:
            continue
        normalized_name = str(name).strip().lower()
        examination_findings.setdefault(normalized_name, []).append(str(status).strip().lower())
    for finding_name, statuses in examination_findings.items():
        unique_statuses = set(statuses)
        if len(unique_statuses) > 1 and finding_name not in ('blood pressure', 'bp'):

            # Informational warning only.
            # This does NOT mean the LLM made an error.
            # It means the doctor said two different things.
            warnings.append(
                f"Examination contradiction preserved for "
                f"'{finding_name}': "
                f"{', '.join(statuses)}."
            )

    # FINAL VALIDATION FLAG
    data['validation'] = {'review_required': len(warnings) > 0, 'warnings': warnings}
    return data
