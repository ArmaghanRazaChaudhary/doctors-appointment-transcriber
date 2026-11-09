import unittest
from unittest.mock import patch

from doctor_assistant import main
from doctor_assistant.extraction import _request_grounded_note
from doctor_assistant.extraction import _transcript_sources
from doctor_assistant.medicines import _spoken_name_before_dose
from doctor_assistant.transcription import _merge_transcript_parts, _remove_repetition_loops
from doctor_assistant.validation import validate_transcript_evidence


class ArchitectureSmokeTests(unittest.TestCase):
    def test_entry_point_is_callable(self):
        self.assertTrue(callable(main))

    def test_overlapping_transcript_words_are_not_duplicated(self):
        parts = ["مریض کو تین دن سے کھانسی ہے", "کھانسی ہے اور بخار نہیں"]
        self.assertEqual(_merge_transcript_parts(parts), "مریض کو تین دن سے کھانسی ہے اور بخار نہیں")

    def test_spoken_medicine_name_uses_the_matching_dose(self):
        evidence = "ناشتے سے 30 منٹ پہلے لیں اینٹی ایسڈ سیروپ 10 ایمیل"
        self.assertEqual(_spoken_name_before_dose(evidence, "10 ml"), "اینٹی ایسڈ سیروپ")

    def test_silence_hallucination_loop_is_removed(self):
        text = "اصل بات اردو میں اردو میں اردو میں اردو میں اگلی بات"
        self.assertEqual(_remove_repetition_loops(text), "اصل بات اگلی بات")

    @patch("time.sleep")
    @patch("requests.post")
    def test_transient_ollama_worker_failure_is_retried(self, post, _sleep):
        class Response:
            def __init__(self, ok, status, payload=None):
                self.ok, self.status_code, self.text = ok, status, "worker failed"
                self._payload = payload

            def json(self):
                return self._payload

        post.side_effect = [
            Response(False, 500),
            Response(True, 200, {"done": True, "done_reason": "stop",
                                 "message": {"content": '{"value":"ok"}'}}),
        ]
        schema = {"type": "object", "properties": {"value": {"type": "string"}},
                  "required": ["value"], "additionalProperties": False}
        self.assertEqual(_request_grounded_note([], schema, "test"), {"value": "ok"})
        self.assertEqual(post.call_count, 2)

    def test_prescription_and_drug_allergy_polarity_are_grounded(self):
        transcript = ("کسی میڈیسن سے الرجی ہو؟ نہیں لیکن میں الرجی کے لیے ایک میڈیسن پڑتا ہوں "
                      "کون سی زائیڈ ہے اچھا میں آپ کو میڈیسن دے رہا ہوں آپ نے پینیڈول کھانی ہے")
        sources = _transcript_sources(transcript)
        ids = list(sources)
        evidence = {"evidence_ids": ids, "evidence_quotes": list(sources.values()),
                    "evidence": " ".join(sources.values())}
        prescription = "میں آپ کو میڈیسن دے رہا ہوں آپ نے پینیڈول کھانی ہے"
        medication_evidence = {"evidence_ids": ids[:1], "evidence_quotes": [prescription],
                               "evidence": prescription}
        data = {"chief_complaint": {}, "symptoms": [], "negative_symptoms": [],
                "medical_history": [{"condition": "Allergy", "status": "present", **evidence}],
                "medications": [
                    {"spoken_name": "پینیڈول", "medication_name": None, "generic_name": None,
                     "status": "continue", "dose": None, "frequency": None, "times_per_day": None,
                     "timing": None, "as_needed": False, "duration": None, "route": None,
                     "indication": None, **medication_evidence}
                ], "examination": [], "assessment": None,
                "follow_up": {"after_days": None, "return_immediately_if": [], "instructions": [], **evidence},
                "review_notes": []}
        result = validate_transcript_evidence(data, transcript)
        self.assertEqual(result["medical_history"][-1]["status"], "denied")
        self.assertEqual(result["medications"][0]["status"], "new")


if __name__ == "__main__":
    unittest.main()
