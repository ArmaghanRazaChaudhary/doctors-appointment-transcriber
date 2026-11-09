# OpenAI-only clinical note experiment

Prepared only: not executed, dependencies not installed, no API calls made.
Independent of the parent local application. No DRAP, Ollama, local Whisper, GPU,
or parent validation rules are used. Existing recordings remain in ../recordings.

Flow: existing audio -> OpenAI transcription -> English structured clinical note
-> source-quote checks -> local files pending doctor review.

## Setup later

Use Python 3.11 or newer. These commands are instructions, not already executed:

```powershell
cd openai_trial
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:OPENAI_API_KEY = 'your-api-key'
```

Keep the key private. The .env.example file is a template only and is not loaded.
Use a separate OpenAI API project and monitor its spending in the API dashboard.

## Run later, after approving the first test

```powershell
.\.venv\Scripts\python.exe app.py --audio ..\recordings\consult_20260904_160940.wav
```

Select any existing recording with --audio. There is no hardcoded recording,
automatic microphone capture, background execution or batch upload.
Each run sends the selected audio and resulting transcript to OpenAI and incurs API charges.
Inputs over 25 MB are rejected locally; automatic chunking is intentionally not implemented.

To reuse a saved transcript after extraction fails or to compare another text model:

```powershell
.\.venv\Scripts\python.exe app.py --transcript .\outputs\YOUR_RUN\transcript.txt
```

## Models and outputs

Defaults: gpt-4o-transcribe for audio; gpt-4.1 for clinical extraction.
Override OPENAI_TRANSCRIPTION_MODEL / OPENAI_CLINICAL_MODEL in the environment.
Use a compatible transcription model supporting JSON and prompting; the text model
must support Responses API Structured Outputs. Account model access is unverified.

The app makes one transcription request and one structuring request, with SDK retries
disabled to avoid hidden repeat charges. It uses store=False for the Responses call.
This does not replace OpenAI's endpoint-specific data-retention policies.

Each unique outputs folder contains transcript.txt, transcription.json (audio runs),
note.json, note.md, and run.json with model identifiers, source hash, usage and status.
Intermediate transcription is preserved if structuring fails. There is no diagnosis
or prescription approval: every saved note remains pending doctor review.

Medicine names are English proposals from the model, with the exact original wording
and evidence in JSON. Missing doses, strengths and ingredients are not filled from
brand knowledge. Source-quote checks flag provenance errors without rewriting facts.
Schema conformance and quote checks do not guarantee clinical correctness.

## Initial test budget

Suggested starting balance: US$5, if that purchase amount is offered in your account.
At the published rates checked on September 5, 2026, gpt-4o-transcribe is estimated
at $0.006/minute. GPT-4.1 is $2/million input tokens and $8/million output tokens.
Example: a 5-minute recording plus 5,000 input and 2,000 output text tokens costs
approximately $0.056 before tax. Twenty such runs would be about $1.12.
Actual cost varies with audio duration, token counts and model selection.

Sources:
- https://developers.openai.com/api/docs/models/gpt-4.1
- https://developers.openai.com/api/docs/pricing
- https://developers.openai.com/api/docs/guides/speech-to-text
- https://developers.openai.com/api/docs/guides/structured-outputs
- https://developers.openai.com/api/docs/guides/your-data

## Usage, timing and billing

Each future run saves `calls.json` beside its note. Each call includes UTC timestamps,
elapsed seconds, requested/returned model, reported service tier, HTTP request ID,
response ID when available, raw usage, and estimated USD cost with dated pricing.
Missing provider fields stay null. Output tokens include reasoning tokens already.
The console prints stage times, estimated costs, and total processing time.
Unknown usage/model/tier pricing produces an unavailable estimate, never a zero charge.
Pricing is a 2026-09-05 standard-rate snapshot; estimates exclude taxes and account adjustments.
The existing model defaults and prompts are unchanged.

Reported billing is separate. Export a complete JSON response from OpenAI's
GET /v1/organization/costs, grouped by project_id and line_item, for a complete UTC day.
That endpoint requires an Admin API key; do not replace the app key with an Admin key.
The reconciliation command reads the saved JSON offline and makes no API requests:

```powershell
.\.venv\Scripts\python.exe reconcile_costs.py --costs-json "C:\path\costs.json" --date 2026-09-05 --project-id "proj_YOUR_PROJECT"
```

It saves reported line items, the local known estimated subtotal, the difference,
and missing-usage/older-run warnings to outputs/reconciliation_DATE.json.
Confirm that the local runs belong to the selected project and account for other activity.
This is an aggregate comparison, not an assignment of actual dollars to each request.
Provider Costs uses daily buckets; it does not give actual cost by request ID.
Older runs are not retroactively assigned missing request IDs or elapsed times.

Implementation added without executing the app, tests, or billing/API requests.

## AI diagnostic assistance (note schema version 2)

New notes include note.diagnostic_assistance, separate from clinician_assessment.
It contains AI-generated differential suggestions, supporting findings and source
quotes, limitations, and questions/checks for the clinician. It may return no
suggestions when the available information is insufficient. The source is fixed to
ai_generated and review_status to pending_doctor_review. Suggestions are not final
diagnoses, prescriptions, or HIMS orders. The current app has no doctor-approval UI;
a future client must record the doctor's decision separately before any promotion
into confirmed diagnoses. No previous patient chart is fetched: only history in the
supplied consultation is available. Existing saved notes are not migrated.

The same clinical API call generates the suggestions; output token usage may increase.
Quote checks verify source text presence, not diagnostic correctness. This feature
was added without executing the app, tests, or an API request and is not clinically validated.

## JSON console output

The app now prints one JSON object on completion, containing note, status, metrics,
validation warnings and output_dir. Stage progress messages and the Markdown report
are no longer printed; new runs no longer generate note.md. note.json keeps the full
clinical schema and calls.json keeps detailed accounting. Model text is requested as
brief phrases, with quotes only in evidence_quotes and no repeated disclaimers.
Existing output files are unchanged. Argument/help errors still use standard CLI stderr.
To capture JSON, redirect the app's stdout; omit the external PowerShell Write-Host
timer line if you need the entire console output to be machine-readable JSON.
No API test was run for this change; token savings have not been measured.
