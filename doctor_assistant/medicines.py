"""Conservative DRAP-backed medication candidate resolution."""
import os
import pickle
import re
import sqlite3
import unicodedata

from rapidfuzz import fuzz, process

from .config import (
    DB_PATH,
    FUZZY_MATCH_MARGIN,
    FUZZY_MATCH_THRESHOLD,
    FUZZY_PREFILTER_THRESHOLD,
    FUZZY_SHORTLIST_LIMIT,
    MEDICINE_FUZZY_INDEX_CACHE,
)


def _normalize_med_identity(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip().lower())

_MEDICINE_INDEX_MEMORY = None


def _normalize_medicine_text(text):
    """
    Lightweight Urdu/Latin normalization used only for medicine matching.
    """
    if not text:
        return ""
    text = str(text).lower().strip()
    replacements = {
        "ي": "ی",
        "ى": "ی",
        "ك": "ک",
        "ۀ": "ہ",
        "ة": "ہ",
        "ؤ": "و",
        "إ": "ا",
        "أ": "ا",
        "ٱ": "ا",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    # Remove Urdu/Arabic vowel marks.
    text = re.sub(r"[\u064B-\u065F\u0670]", "", text)
    text = re.sub('[^\\w\\s\\u0600-\\u06FF]', ' ', text)
    return re.sub(r"\s+", " ", text).strip()

# Conservative character-level Urdu -> Latin phonetic representation.
# This is NOT intended as linguistic transliteration.  It only creates a
# comparison key so Urdu ASR spellings can be compared with Latin DRAP brands.
_URDU_PHONETIC_MAP = {
    "ا": "a", "آ": "a",
    "ب": "b", "پ": "p",
    "ت": "t", "ٹ": "t", "ث": "s",
    "ج": "j", "چ": "ch",
    "ح": "h", "خ": "kh",
    "د": "d", "ڈ": "d", "ذ": "z",
    "ر": "r", "ڑ": "r",
    "ز": "z", "ژ": "zh",
    "س": "s", "ش": "sh",
    "ص": "s", "ض": "z",
    "ط": "t", "ظ": "z",
    "ع": "", "غ": "gh",
    "ف": "f", "ق": "k",
    "ک": "k", "گ": "g",
    "ل": "l", "م": "m",
    "ن": "n", "ں": "n",
    "و": "u", "ہ": "h", "ھ": "h",
    "ء": "", "ئ": "i",
    "ی": "i", "ے": "e",
}


def _medicine_phonetic_key(text):
    """
    Produce a language-agnostic-ish key for comparing Urdu ASR spellings
    against Latin medicine brands.

    Example:
        "زی میکس" -> "zimiks"
        "Zimax"    -> "zimaks"

    We normalize Latin 'x' to 'ks', which makes the two representations
    much closer without hardcoding any medicine name.
    """
    normalized = _normalize_medicine_text(text)
    if not normalized:
        return ""
    pieces = []
    for char in normalized:
        if char in _URDU_PHONETIC_MAP:
            pieces.append(_URDU_PHONETIC_MAP[char])
        else:
            pieces.append(char)
    value = "".join(pieces)

    # Strip accents from any Latin text.
    value = unicodedata.normalize("NFKD", value)
    value = ''.join((ch for ch in value if not unicodedata.combining(ch)))
    value = value.lower()

    # Common spelling variants that represent nearly the same sound.
    # These are language/phonetic rules, NOT medicine-specific aliases.
    value = value.replace("ph", "f")
    value = value.replace("ck", "k")
    value = value.replace("qu", "k")
    value = value.replace("q", "k")
    value = value.replace("c", "k")
    value = value.replace("x", "ks")

    # Remove separators/punctuation after transliteration.
    value = re.sub(r"[^a-z0-9]+", "", value)

    # Repeated Latin letters rarely carry useful identity information
    # in ASR medicine names (e.g. "tt" vs "t").
    value = re.sub(r"([a-z])\1+", r"\1", value)
    return value


def _medicine_consonant_skeleton(phonetic_key):
    """
    Secondary comparison that is less sensitive to uncertain Urdu vowels.
    """
    return re.sub(r"[aeiouy]", "", phonetic_key or "")


def _medicine_similarity(candidate_key, db_key):
    """
    Conservative combined score.

    Full phonetic spelling remains the main signal.  A consonant skeleton
    helps with Urdu vowel ambiguity, but cannot win by itself.
    """
    if not candidate_key or not db_key:
        return 0.0
    full_score = fuzz.ratio(candidate_key, db_key)
    candidate_skeleton = _medicine_consonant_skeleton(candidate_key)
    db_skeleton = _medicine_consonant_skeleton(db_key)
    if candidate_skeleton and db_skeleton:
        skeleton_score = fuzz.ratio(candidate_skeleton, db_skeleton)
    else:
        skeleton_score = 0.0

    # Urdu commonly omits or varies short vowels relative to Latin brand
    # spellings.  Treat all vowel runs as one generic vowel for a third,
    # weaker comparison signal.
    candidate_vowel_class = re.sub('[aeiouy]+', 'a', candidate_key)
    db_vowel_class = re.sub('[aeiouy]+', 'a', db_key)
    vowel_score = fuzz.ratio(candidate_vowel_class, db_vowel_class)
    score = 0.5 * full_score + 0.3 * skeleton_score + 0.2 * vowel_score

    # Penalize only major length mismatches.  Moderate length differences
    # are normal when Urdu drops vowels (e.g. اگمنٹن vs Augmentin).
    longer = max(len(candidate_key), len(db_key))
    shorter = min(len(candidate_key), len(db_key))
    if longer:
        length_ratio = shorter / longer
        if length_ratio < 0.55:
            score -= 10.0
        elif length_ratio < 0.65:
            score -= 4.0
    return max(0.0, min(100.0, score))


def _db_signature():
    """
    Signature used to invalidate the sidecar medicine-name cache whenever
    the DRAP database file changes.
    """
    try:
        stat = os.stat(DB_PATH)
        return (os.path.abspath(DB_PATH), int(stat.st_size), int(stat.st_mtime))
    except OSError:
        return (os.path.abspath(DB_PATH), None, None)


def _build_medicine_fuzzy_index(conn):
    """
    Build a compact searchable list from product_name + brand_name.

    This reads only medicine identity columns, not the large raw HTML fields.
    The result is cached on disk and in memory.
    """
    print("[MED] Building phonetic medicine index (first run only)...")
    rows = conn.execute(
        """
        SELECT
            p.registration_no,
            p.product_name,
            d.brand_name
        FROM products AS p
        LEFT JOIN product_details AS d
            ON d.registration_no = p.registration_no
        WHERE
            (p.product_name IS NOT NULL AND TRIM(p.product_name) != '')
            OR
            (d.brand_name IS NOT NULL AND TRIM(d.brand_name) != '')
        """
    ).fetchall()

    # key -> canonical-name -> representative record
    grouped = {}
    for registration_no, product_name, brand_name in rows:
        canonical_brand = (
            str(brand_name).strip()
            if brand_name
            else (
                str(product_name).strip()
                if product_name
                else None
            )
        )
        names_to_index = []
        if product_name:
            names_to_index.append(str(product_name).strip())
        if brand_name:
            brand_value = str(brand_name).strip()
            if brand_value and brand_value not in names_to_index:
                names_to_index.append(brand_value)
        for source_name in names_to_index:
            key = _medicine_phonetic_key(source_name)

            # Extremely short keys are too collision-prone for fuzzy identity.
            if len(key) < 4:
                continue
            canonical_norm = _normalize_med_identity(canonical_brand or source_name)
            bucket = grouped.setdefault(key, {})

            # Multiple registrations of the same brand are fine.
            # Keep one representative registration for later generic lookup.
            bucket.setdefault(
                canonical_norm,
                {
                    "registration_no": registration_no,
                    "product_name": (
                        str(product_name).strip()
                        if product_name
                        else source_name
                    ),
                    "brand_name": canonical_brand or source_name,
                    "indexed_name": source_name,
                    "phonetic_key": key,
                },
            )

    # RapidFuzz searches unique phonetic keys. Keeping ambiguity data for
    # each key lets us refuse keys shared by distinct medicine names.
    # We also cache two parallel representations used only for candidate
    # retrieval. This prevents a good Urdu/Latin match from being excluded
    # just because whole-string spelling similarity has many ties.
    keys = list(grouped.keys())
    skeletons = [_medicine_consonant_skeleton(key) for key in keys]
    vowel_classes = [re.sub('[aeiouy]+', 'a', key) for key in keys]
    index = {
        "signature": _db_signature(),
        "keys": keys,
        "skeletons": skeletons,
        "vowel_classes": vowel_classes,
        "records_by_key": grouped,
    }
    print(f'[MED] Phonetic index ready: {len(keys):,} unique medicine-name keys')
    return index


def _load_medicine_fuzzy_index(conn):
    global _MEDICINE_INDEX_MEMORY
    signature = _db_signature()
    if (
        isinstance(_MEDICINE_INDEX_MEMORY, dict)
        and _MEDICINE_INDEX_MEMORY.get("signature") == signature
    ):
        return _MEDICINE_INDEX_MEMORY

    # Try compact sidecar cache first.
    try:
        with open(MEDICINE_FUZZY_INDEX_CACHE, "rb") as f:
            cached = pickle.load(f)
        if (
            isinstance(cached, dict)
            and cached.get("signature") == signature
            and cached.get("keys")
            and cached.get("skeletons")
            and cached.get("vowel_classes")
            and cached.get("records_by_key")
        ):
            _MEDICINE_INDEX_MEMORY = cached
            print(f"[MED] Loaded phonetic index cache: {len(cached['keys']):,} keys")
            return cached
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f'[MED] Ignoring unusable fuzzy index cache: {e}')
    index = _build_medicine_fuzzy_index(conn)

    # Cache failure must never break clinical processing.
    try:
        with open(MEDICINE_FUZZY_INDEX_CACHE, "wb") as f:
            pickle.dump(index, f, protocol=pickle.HIGHEST_PROTOCOL)
        print(f'[MED] Saved phonetic index cache: {MEDICINE_FUZZY_INDEX_CACHE}')
    except Exception as e:
        print(f'[MED] Could not save phonetic index cache: {e}')
    _MEDICINE_INDEX_MEMORY = index
    return index


def _get_medicine_details(conn, registration_no, fallback_name):
    """
    Resolve canonical brand/product + generic composition for one registration.
    """
    product_row = conn.execute(
        """
        SELECT product_name
        FROM products
        WHERE registration_no = ?
        LIMIT 1
        """,
        (registration_no,),
    ).fetchone()
    product_name = str(product_row[0]).strip() if product_row and product_row[0] else fallback_name
    brand_row = conn.execute(
        """
        SELECT brand_name
        FROM product_details
        WHERE registration_no = ?
        LIMIT 1
        """,
        (registration_no,),
    ).fetchone()
    brand_name = str(brand_row[0]).strip() if brand_row and brand_row[0] else product_name
    generic_rows = conn.execute(
        """
        SELECT DISTINCT generic_name
        FROM product_compositions
        WHERE registration_no = ?
          AND generic_name IS NOT NULL
          AND TRIM(generic_name) != ''
        """,
        (registration_no,),
    ).fetchall()
    generics = [str(row[0]).strip() for row in generic_rows if row[0]]
    return product_name, brand_name, generics

# Language normalization rules, not medication aliases or clinical inference.
_NUMBER_WORDS = {}
for _number, _word in enumerate(
    (
        'صفر ایک دو تین چار پانچ چھ سات آٹھ نو دس گیارہ بارہ تیرہ چودہ پندرہ سولہ سترہ '
        'اٹھارہ انیس بیس اکیس بائیس تئیس چوبیس پچیس چھبیس ستائیس اٹھائیس انتیس تیس اکتیس '
        'بتیس تینتیس چونتیس پینتیس چھتیس سینتیس اڑتیس انتالیس چالیس اکتالیس بیالیس تینتالیس '
        'چوالیس پینتالیس چھیالیس سینتالیس اڑتالیس انچاس پچاس اکیاون باون ترپن چوّن پچپن چھپن '
        'ستاون اٹھاون انسٹھ ساٹھ اکسٹھ باسٹھ تریسٹھ چونسٹھ پینسٹھ چھیاسٹھ سڑسٹھ اڑسٹھ انہتر '
        'ستر اکہتر بہتر تہتر چوہتر پچھتر چھہتر ستتر اٹھہتر اناسی اسی اکیاسی بیاسی تراسی '
        'چوراسی پچاسی چھیاسی ستاسی اٹھاسی نواسی نوے اکانوے بانوے ترانوے چورانوے پچانوے '
        'چھیانوے ستانوے اٹھانوے ننانوے'
    ).split()
):
    _NUMBER_WORDS[_normalize_medicine_text(_word)] = _number
_NUMBER_WORDS.update(dict(zip(
    'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen'.split(),
    range(20),
)))
_NUMBER_WORDS.update(dict(zip(
    'twenty thirty forty fifty sixty seventy eighty ninety'.split(),
    range(20, 100, 10),
)))
_NUMBER_WORDS.update({'چھے': 6, 'چون': 54})
_NUMBER_SCALES = {'سو': 100, 'ہزار': 1000, 'hundred': 100, 'thousand': 1000}


def _number_normalized_text(text):
    """Normalize Urdu/English cardinals without assigning clinical meaning."""
    tokens = re.findall(r'\d+|[^\W\d_]+|[^\w\s]', str(text).lower(), re.UNICODE)
    result = []
    i = 0
    while i < len(tokens):
        word = _normalize_medicine_text(tokens[i])
        if tokens[i].isdigit():
            result.append(str(int(tokens[i])))
            i += 1
            continue
        if word not in _NUMBER_WORDS and word not in _NUMBER_SCALES:
            result.append(tokens[i])
            i += 1
            continue
        value = _NUMBER_WORDS.get(word, 1)
        if word in _NUMBER_SCALES:
            value = _NUMBER_SCALES[word]
        i += 1

        # Urdu non-round cardinals are single words. Only combine a following
        # scale, or tens + units in English, rather than merging repeated ASR words.
        if i < len(tokens) and tokens[i] in _NUMBER_SCALES:
            value *= _NUMBER_SCALES[tokens[i]]
            i += 1
            if i < len(tokens) and tokens[i] == 'and':
                i += 1
            if i < len(tokens) and _normalize_medicine_text(tokens[i]) in _NUMBER_WORDS:
                remainder_word = _normalize_medicine_text(tokens[i])
                value += _NUMBER_WORDS[remainder_word]
                i += 1
                if remainder_word in {'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety'}:
                    if i < len(tokens) and tokens[i] == '-':
                        i += 1
                    if i < len(tokens) and tokens[i] in _NUMBER_WORDS and 0 < _NUMBER_WORDS[tokens[i]] < 10:
                        value += _NUMBER_WORDS[tokens[i]]
                        i += 1
        elif word in {'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety'}:
            if i < len(tokens) and tokens[i] == '-':
                i += 1
            if i < len(tokens) and tokens[i] in _NUMBER_WORDS and 0 < _NUMBER_WORDS[tokens[i]] < 10:
                value += _NUMBER_WORDS[tokens[i]]
                i += 1
        result.append(str(value))
    return re.sub(r'(?<=\d)\s*\.\s*(?=\d)', '.', ' '.join(result))


def _evidence_text(text):
    return re.sub(r'[^\w]+', ' ', _normalize_medicine_text(text)).strip()


def _evidence_present(evidence, transcript):
    quote = _evidence_text(evidence)
    return bool(quote) and (' ' + quote + ' ') in (' ' + _evidence_text(transcript) + ' ')


def _has_language_cue(text, pattern):
    return bool(re.search(r'(?<!\w)(?:' + pattern + r')(?!\w)', _evidence_text(text), re.I))


def _positive_language_cue(text, pattern):
    normalized = _evidence_text(text)
    for match in re.finditer(r'(?<!\w)(?:' + pattern + r')(?!\w)', normalized, re.I):
        surrounding = normalized[:match.start()].split()[-3:] + normalized[match.end():].split()[:3]
        if not set(surrounding).intersection({'نہیں', 'نہ', 'مت', 'not', 'never', 'no'}):
            return True
    return False


def _dose_quantities(text):
    normalized = _number_normalized_text(text)
    units = {'mg': 'mg', 'milligram': 'mg', 'milligrams': 'mg', 'ملی گرام': 'mg', 'میلی گرام': 'mg', 'ملیگرام': 'mg', 'میلیگرام': 'mg', 'mcg': 'mcg', 'ml': 'ml', 'ملی لیٹر': 'ml', 'میلی لیٹر': 'ml', 'tablet': 'tablet', 'tablets': 'tablet', 'گولی': 'tablet', 'گولیاں': 'tablet', 'capsule': 'capsule', 'capsules': 'capsule', 'کیپسول': 'capsule'}
    pattern = r'(\d+(?:\.\d+)?)\s*(?:کی\s+)?(' + '|'.join(re.escape(u) for u in sorted(units, key=len, reverse=True)) + r')(?!\w)'
    return {(float(amount), units[unit]) for amount, unit in re.findall(pattern, normalized)}


def _bp_pairs(text):
    normalized = _number_normalized_text(text)
    return [(int(a), int(b)) for a, b in re.findall(
        r'(?<!\d)(\d{2,3})\s*(?:/|بٹا|بھٹا|over|bata)\s*(\d{2,3})(?!\d)', normalized, re.I
    )]


def _medication_candidate_phrases(normalized_tokens, cue_positions, minimum_key_length=5):
    """Names beside instructions or directly before explicit dose units.

    Preserve token offsets so annotations never consume the dose or neighboring
    words. Do not turn allergy questions or negated prescribing into candidates.
    """
    tokens = [re.sub(r'^[^\w]+|[^\w]+$', '', t) for t in normalized_tokens]
    grammar = set((
        'آپ اپ یہ وہ ایک اور تو بس پھر کی کے کا کو سے میں پر نے جو والی والا والے مجھے کسی '
        'کوئی نئی نیا نہیں نہ یا آج کل رات صبح شام تھا تھی تھے ہے ہیں جی صرف اس وقت بعد چیک '
        'اپ کروا the a an this that my your please any no new not yesterday today'
    ).split())
    descriptors = set('میڈیسن دوائی دوا دروا گولی کیپسول شربت کاف medicine medicines tablet tablets capsule capsules syrup injection cough'.split())
    units = {'mg', 'mcg', 'ml', 'milligram', 'milligrams', 'گرام', 'ملیگرام', 'میلیگرام'}
    starts = set()

    # A medication can precede its dose even when the taking verb is later.
    for i, token in enumerate(tokens):
        if token not in units and not re.fullmatch(r'\d+(?:mg|mcg|ml)', token):
            continue
        end = i
        if token == 'گرام' and i and tokens[i - 1] in {'ملی', 'میلی'}:
            end -= 1
        dose_end = end
        while end > 0 and (tokens[end - 1].isdigit() or tokens[end - 1] in _NUMBER_WORDS or tokens[end - 1] in _NUMBER_SCALES):
            end -= 1
        if end < dose_end or re.fullmatch(r'\d+(?:mg|mcg|ml)', token):
            starts.add(end)

    # Explicit taking instructions and historical taking verbs. Broad nouns
    # such as 'medicine' alone are not enough to justify fuzzy name lookup.
    verbs = {'لیں', 'لین', 'لینی', 'لینا', 'لے', 'لی', 'لیا', 'دیں', 'دینا', 'دینی', 'take', 'took', 'taking', 'prescribe', 'prescribed', 'continue'}
    for i, token in enumerate(tokens):
        if token not in verbs:
            continue
        end = i
        while end > 0 and (tokens[end - 1] in descriptors or tokens[end - 1] in {'کی', 'ایک', '1'}):
            end -= 1
        if end == i:
            starts.add(end)
    grammar.update(verbs)
    result = []
    seen = set()
    for end in sorted(starts):
        for length in (1, 2, 3):
            start = end - length
            if start < 0:
                continue
            phrase_tokens = tokens[start:end]
            if any(not t or re.search(r'[؟،۔]', t) or t in grammar or t in descriptors or t in units or t in _NUMBER_WORDS or t.isdigit() for t in phrase_tokens):
                continue
            phrase = ' '.join(phrase_tokens)
            key = _medicine_phonetic_key(phrase)
            if len(key) < minimum_key_length or (start, end) in seen:
                continue
            seen.add((start, end))
            result.append((start, end, phrase, key))
    return result


def _daily_count(evidence):
    text = _number_normalized_text(evidence)
    patterns = [r'(?:روزانہ|دن میں|روز)\s*(\d+)\s*(?:بار|دفعہ)?',
                r'(\d+)\s*(?:times|time)\s*(?:a|per)\s*day']
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    if re.search(r'once\s+(?:daily|a day)', text, re.I):
        return 1
    if re.search(r'twice\s+(?:daily|a day)', text, re.I):
        return 2
    return None

def _latin_spoken_name(value):
    """Transliterate source words; this is explicitly NOT a verified brand name."""
    if not value:
        return None
    if not re.search(r'[\u0600-\u06ff]', value):
        return value
    words = []
    for word in _normalize_medicine_text(value).split():
        latin = ''.join(_URDU_PHONETIC_MAP.get(char, char) for char in word)
        latin = re.sub(r'[^a-zA-Z0-9-]', '', latin)
        if latin:
            words.append(latin.capitalize())
    return ' '.join(words) or None

def _spoken_name_before_dose(evidence, dose=None):
    """Return the short source phrase immediately before a numeric dose."""
    words = evidence.split()
    stop = set(('میں مجھے آپ اسے یہ وہ اور پھر ہے ہیں تھا تھی تھے ہوں جی اچھا صرف لیں لے لیتا لیتی '
                'for take taking prescribed prescribe daily روزانہ دفعہ صبح شام').split())
    expected = re.search(r'\d+(?:\.\d+)?', str(dose or ''))
    expected = expected.group(0) if expected else None
    instruction_words = {'لیں', 'لین', 'لینی', 'لینا', 'لے', 'take', 'prescribe', 'prescribed'}
    for index, word in enumerate(words):
        number = _normalize_medicine_text(word)
        if not re.fullmatch(r'\d+(?:\.\d+)?', number) or (expected and number != expected):
            continue
        candidate = []
        for previous in reversed(words[max(0, index - 4):index]):
            clean = re.sub(r'^[^\w]+|[^\w]+$', '', previous)
            if not candidate and _normalize_medicine_text(clean) in instruction_words:
                continue
            if not clean or _normalize_medicine_text(clean) in stop:
                break
            candidate.append(clean)
            if len(candidate) == 3:
                break
        candidate.reverse()
        if candidate:
            return ' '.join(candidate)
    return None

def _english_medication_label(medication):
    verified = medication.get('medication_name') or medication.get('generic_name')
    if verified:
        return verified
    source = ' '.join(str(value or '') for value in (
        medication.get('source_spoken_name'), medication.get('spoken_name')
    )).strip()
    if _has_language_cue(source, r'کاف|کھانسی|cough'):
        return 'Unidentified cough medicine'
    if _has_language_cue(source, r'بی پی|بلڈ پریشر|blood pressure|bp'):
        return 'Unidentified blood-pressure medicine'
    if not source and _has_language_cue(medication.get('evidence') or '', r'ڈائیپیٹیز|شوگر|diabetes'):
        return 'Unidentified diabetes medicine'
    return 'Unidentified medication'


def _fuzzy_candidate_phrases(normalized_tokens, cue_positions):
    return _medication_candidate_phrases(normalized_tokens, cue_positions)


def _find_phonetic_fuzzy_match(conn, fuzzy_index, phrase, candidate_key):
    """
    Return one high-confidence unique medicine match or None.

    Candidate retrieval deliberately uses multiple phonetic views:
      1. whole phonetic spelling
      2. consonant skeleton (robust to Urdu vowel uncertainty)
      3. vowel-normalized spelling

    The union is then reranked with the conservative combined scorer.
    This is generic: no medicine-specific aliases are hardcoded.

    Acceptance still requires:
      - strong combined score
      - unique database identity for the winning phonetic key
      - a safe margin over a different medicine
    """
    if not candidate_key or len(candidate_key) < 5:
        return None
    keys = fuzzy_index["keys"]
    skeletons = fuzzy_index["skeletons"]
    vowel_classes = fuzzy_index["vowel_classes"]
    candidate_skeleton = _medicine_consonant_skeleton(candidate_key)
    candidate_vowel_class = re.sub('[aeiouy]+', 'a', candidate_key)

    # Collect candidate key indexes from three independent retrieval views.
    # A larger shortlist is cheap because only medicine-like transcript
    # phrases reach this function, not every word in the consultation.
    candidate_indexes = set()
    full_hits = process.extract(candidate_key, keys, scorer=fuzz.ratio, limit=32, score_cutoff=55.0)
    for _value, _score, idx in full_hits:
        candidate_indexes.add(idx)
    if candidate_skeleton:
        skeleton_hits = process.extract(
            candidate_skeleton,
            skeletons,
            scorer=fuzz.ratio,
            limit=32,
            score_cutoff=60.0,
        )
        for _value, _score, idx in skeleton_hits:
            candidate_indexes.add(idx)
    if candidate_vowel_class:
        vowel_hits = process.extract(
            candidate_vowel_class,
            vowel_classes,
            scorer=fuzz.ratio,
            limit=32,
            score_cutoff=60.0,
        )
        for _value, _score, idx in vowel_hits:
            candidate_indexes.add(idx)
    if not candidate_indexes:
        return None
    ranked = []
    for idx in candidate_indexes:
        db_key = keys[idx]
        score = _medicine_similarity(candidate_key, db_key)
        records_for_key = fuzzy_index['records_by_key'].get(db_key, {})

        # If the same phonetic key belongs to multiple distinct canonical
        # medicine names, it is intrinsically ambiguous -> do not auto-resolve.
        if len(records_for_key) != 1:
            continue
        record = next(iter(records_for_key.values()))
        ranked.append({'score': score, 'db_key': db_key, 'record': record})
    if not ranked:
        return None
    ranked.sort(key=lambda item: item['score'], reverse=True)
    best = ranked[0]

    # Runner-up must represent a different canonical medicine.
    best_name_key = _normalize_med_identity(
        best["record"].get("brand_name")
        or best["record"].get("product_name")
    )
    second = None
    for item in ranked[1:]:
        item_name_key = _normalize_med_identity(
            item["record"].get("brand_name")
            or item["record"].get("product_name")
        )
        if item_name_key != best_name_key:
            second = item
            break
    second_score = second["score"] if second else 0.0
    margin = best["score"] - second_score
    token_count = len(_normalize_medicine_text(phrase).split())
    required_score = FUZZY_MATCH_THRESHOLD
    if token_count == 1 and len(candidate_key) <= 4:
        required_score += 4.0
    best_display_name = (
        best["record"].get("brand_name")
        or best["record"].get("product_name")
        or "unknown"
    )

    # Diagnostic: show the top few plausible medicines for real-world tuning.
    plausible = []
    seen_names = set()
    for item in ranked:
        display = (
            item["record"].get("brand_name")
            or item["record"].get("product_name")
            or "unknown"
        )
        norm = _normalize_med_identity(display)
        if norm in seen_names:
            continue
        seen_names.add(norm)
        plausible.append((display, item["score"]))
        if len(plausible) == 5:
            break
    if plausible and plausible[0][1] >= 70.0:
        preview = ', '.join((f'{name}={score:.1f}' for name, score in plausible))
        print(f"[MED] Fuzzy ranking '{phrase}': {preview}")
    if best["score"] < required_score:
        if best["score"] >= 75.0:
            print(
                "[MED] Fuzzy rejected "
                f"'{phrase}': best='{best_display_name}', "
                f"score={best['score']:.1f} "
                f"(required={required_score:.1f})"
            )
        return None
    if second_score > 0 and margin < FUZZY_MATCH_MARGIN:
        second_display_name = (
            second["record"].get("brand_name")
            or second["record"].get("product_name")
            or "unknown"
        )
        print(
            "[MED] Fuzzy rejected as ambiguous "
            f"'{phrase}': best='{best_display_name}' "
            f"{best['score']:.1f}, "
            f"runner_up='{second_display_name}' "
            f"{second_score:.1f}, "
            f"margin={margin:.1f} "
            f"(required={FUZZY_MATCH_MARGIN:.1f})"
        )
        return None
    registration_no = best["record"]["registration_no"]
    product_name, brand_name, generics = _get_medicine_details(
        conn,
        registration_no,
        best["record"].get("indexed_name") or phrase,
    )
    return {
        "registration_no": registration_no,
        "product_name": product_name,
        "brand_name": brand_name,
        "generics": generics,
        "method": "phonetic_fuzzy",
        "score": round(best["score"], 1),
        "margin": round(margin, 1),
    }


def annotate_medications(transcript):
    """
    Conservative Pakistani medicine identification.

    Resolution order:
      1. Exact product_name / brand_name lookup.
      2. If exact lookup fails, phonetic/fuzzy matching ONLY for phrases
         immediately next to medication-related instruction cues.

    Safety:
      - Never fuzzy-searches the whole transcript.
      - Never hardcodes medicine aliases.
      - Requires a high score and a uniqueness margin.
      - Ambiguous matches remain unresolved for doctor review.
    """
    print()
    print("=" * 60)
    print("MEDICATION MATCHING")
    print("=" * 60)
    try:
        print("[MED] Opening DRAP database...")
        conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True, timeout=10)
        conn.execute("PRAGMA query_only = ON")
        print("[MED] SQLite DB opened")
    except Exception as e:
        print(f"[MED DB ERROR] {e}")
        return transcript
    original_tokens = transcript.split()
    if not original_tokens:
        conn.close()
        return transcript
    normalized_tokens = [_normalize_medicine_text(token) for token in original_tokens]
    medication_cues = {
        "لیں", "لین", "لینی", "لینا", "لے",
        "دیں", "دینا", "دینی",
        "دوائی", "دوا", "میڈیسن",
        "medicine", "medicines",
        "tablet", "tablets",
        "capsule", "capsules",
        "syrup", "injection",
        "گولی", "کیپسول", "شربت",
        "prescribe", "prescribed",
        "take", "continue", "شروع",
    }
    cue_positions = [
        index
        for index, token in enumerate(normalized_tokens)
        if token in medication_cues
    ]

    # Exact candidates
    candidate_phrases = [
        (start, end, phrase)
        for start, end, phrase, _key in _medication_candidate_phrases(
            normalized_tokens, cue_positions, minimum_key_length=3
        )
    ]
    unique_candidates = []
    seen = set()
    for start, end, phrase in candidate_phrases:
        key = (start, end, phrase)
        if key in seen:
            continue
        seen.add(key)
        unique_candidates.append((start, end, phrase))
    print(f'[MED] Exact candidate phrases: {len(unique_candidates)}')
    matches = []

    # Stage 1: exact DB lookup
    for start, end, phrase in unique_candidates:
        rows = conn.execute(
            """
            SELECT registration_no, product_name
            FROM products
            WHERE LOWER(product_name) = LOWER(?)
            LIMIT 10
            """,
            (phrase,),
        ).fetchall()
        if not rows:
            rows = conn.execute(
                """
                SELECT registration_no, brand_name
                FROM product_details
                WHERE LOWER(brand_name) = LOWER(?)
                LIMIT 10
                """,
                (phrase,),
            ).fetchall()
        if not rows:
            continue
        registration_no = rows[0][0]
        product_name, brand_name, generics = _get_medicine_details(conn, registration_no, phrase)
        matches.append(
            {
                "start": start,
                "end": end,
                "spoken_phrase": phrase,
                "registration_no": registration_no,
                "product_name": product_name,
                "brand_name": brand_name,
                "generics": generics,
                "method": "exact",
                "score": 100.0,
                "margin": 100.0,
            }
        )
    print(f'[MED] Exact matches found: {len(matches)}')

    # Stage 2: phonetic/fuzzy DB lookup
    fuzzy_candidates = _fuzzy_candidate_phrases(normalized_tokens, cue_positions)

    # Do not fuzz spans already resolved exactly.
    exact_occupied = set()
    for match in matches:
        exact_occupied.update(range(match['start'], match['end']))
    fuzzy_candidates = [
        item
        for item in fuzzy_candidates
        if not exact_occupied.intersection(
            range(item[0], item[1])
        )
    ]
    print(f'[MED] Fuzzy candidate phrases: {len(fuzzy_candidates)}')
    if fuzzy_candidates:
        fuzzy_index = _load_medicine_fuzzy_index(conn)
        for start, end, phrase, candidate_key in fuzzy_candidates:
            fuzzy_match = _find_phonetic_fuzzy_match(conn, fuzzy_index, phrase, candidate_key)
            if not fuzzy_match:
                continue
            fuzzy_match.update({'start': start, 'end': end, 'spoken_phrase': phrase})
            matches.append(fuzzy_match)
            print(
                "[MED] PHONETIC MATCH "
                f"'{phrase}' -> "
                f"'{fuzzy_match['brand_name']}' "
                f"(score={fuzzy_match['score']:.1f}, "
                f"margin={fuzzy_match['margin']:.1f})"
            )

    # Database work is complete.
    conn.close()
    if not matches:
        print('[MED] No safe medicine matches found. Medicine identity will remain unresolved.')
        return transcript

    # Resolve overlapping candidate spans.
    # Priority:
    #   exact > fuzzy
    #   higher score > lower score
    #   longer phrase > shorter phrase
    matches.sort(
        key=lambda item: (
            0 if item.get("method") == "exact" else 1,
            -float(item.get("score") or 0),
            -(item["end"] - item["start"]),
            item["start"],
        )
    )
    final_matches = []
    occupied = set()
    for match in matches:
        positions = set(range(match['start'], match['end']))
        if occupied.intersection(positions):
            continue
        final_matches.append(match)
        occupied.update(positions)

    # Put annotations back into transcript order.
    final_matches.sort(key=lambda item: item['start'])
    annotated_tokens = original_tokens[:]
    for match in sorted(
        final_matches,
        key=lambda item: item["start"],
        reverse=True,
    ):
        start = match["start"]
        end = match["end"]
        brand = match.get('brand_name') or match.get('product_name')
        generics = match.get("generics") or []
        if generics:
            annotation = f"[{brand}/{', '.join(generics)}]"
        else:
            annotation = f"[{brand}]"

        # Preserve ALL of the doctor's spoken words and append the
        # authoritative DRAP identity after the final token.
        # Example:
        #   زی میکس[Zimax/Azithromycin]
        # Do not delete "زی" from the spoken phrase.
        annotated_tokens[end - 1] += annotation
    result = ' '.join((token for token in annotated_tokens if token))
    print()
    print("[MED] Medication matching complete.")
    print(f'[MED] Medicines identified safely: {len(final_matches)}')
    for match in final_matches:
        print(
            f"[MED]   {match.get('spoken_phrase')} "
            f"=> {match.get('brand_name')} "
            f"[{match.get('method')}, "
            f"{float(match.get('score') or 0):.1f}]"
        )
    return result
