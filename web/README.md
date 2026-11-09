# Suno local application

Start from PowerShell in this folder with `./start-local.ps1`. Open http://localhost:8766 in Chrome or Edge. Keep that console open. `./start-local.ps1 -Rebuild` rebuilds the frontend after changes. Needs Node 22+ on PATH; the Python dependencies come from the openai_trial environment (see the main README).

## Consultation flow

New consultation → patient name, patient ID and doctor → consent → Record or Upload → Generate clinical note → click clinical fields to edit → Confirm / Reviewed → Print report.

Allow the browser's microphone prompt when recording. If blocked, enable microphone for localhost in browser site settings and check Windows Settings > Privacy & security > Microphone. The server sends a microphone=(self) Permissions-Policy header. Localhost is required; this server is bound to 127.0.0.1 and is not exposed to the LAN or internet.

The existing API key is read at runtime from OPENAI_API_KEY or ../openai_trial/api-key.txt; it is never copied to the frontend. Audio generation uses paid OpenAI calls. Models are configured in backend/settings.json: gpt-transcribe and gpt-5.6-sol, low reasoning. The existing extraction prompt and usage logger are reused; a local schema extension adds the short AI diagnosis paragraph.

## Records and printing

local_data/consultations.db stores patient-scoped consultation state and reviewed records. Per-consultation directories retain uploaded audio, original note, transcript, API accounting and review revisions. Original evidence is not overwritten. Clinic and Hospital lists are separate workspaces. Imported old runs use explicit test identifiers because the source JSON has no patient identity.

Current/past/continue/stop/uncertain medications appear in history or medication changes. Only new medication rows appear in the prescription table. All clinical fields can be edited directly; more medication fields are available inline. Clinical edits are held in the page until Confirm / Reviewed. Navigation and reload warn about unsaved edits. Editing a reviewed note requires reconfirmation before printing.

The print document is separately constructed from clinical fields. AI diagnosis, AI differential lists, review flags and source evidence are excluded. A clinician-written assessment is included. Printing is offered only after a confirmed record is saved. Browser print dialog supports Save as PDF.

## Reliability and limits

Uploads are saved before queueing. Repeating an upload of the same file to the same consultation returns the existing job. Provider calls do not automatically retry. Paid retries are disabled, including the former manual structuring retry route. On restart, interrupted jobs become failed instead of silently spending credits again. Every save checks the record revision to prevent stale-tab overwrites.

This is a single-computer prototype, without production hospital authentication, shared patient registry, external HIMS delivery or encrypted clinical storage. Hospital mode organizes a local workspace; no external hospital is contacted. Do not expose this server publicly. Patient data is not embedded in the frontend build.


## Live audio

The browser captures continuous mono PCM using an AudioWorklet at 48 kHz. Transport blocks (250 ms) are saved to IndexedDB before upload, numbered within one consultation, and acknowledged only after SQLite persistence. Retries to localhost resend the same block and cannot duplicate a transcription job. If the connection fails, leave the page open and use End consultation or Resume saved upload. Following a reload, recovery finishes the saved portion; it does not silently restart the microphone. Interrupted capture is flagged on the clinical draft. A browser/OS crash can lose the final uncommitted audio; no software VAD guarantees every word.

The backend uses WebRTC VAD mode 0, ends speech chunks after 500 ms of detected silence, and retains 500 ms of leading/trailing padding. Continuous speech is split at 90 seconds with a 500 ms overlap. Only exact multiword text overlap is removed at these known boundaries; original individual transcripts remain stored. The fixed energy fallback was removed because ordinary room noise prevented silence cuts. Review uncertain boundaries against the recording. Silence trimming may reduce sent duration, but padding and short chunks can offset savings.

Live microphone consultations use one transcription-only OpenAI WebSocket session, opened with the first speech chunk and kept across pauses. Each chunk is appended and committed in order; the next commit waits for the previous final transcript so gpt-transcribe can use earlier transcribed turns as context. Session ID, item ID and previous-item ID link the OpenAI turn chain to the local consultation. The existing 500 ms pause setting, padding, models and prompts are unchanged. Raw audio stays at its capture rate; only the OpenAI input is converted to the required 24 kHz PCM format. Tiny final turns are zero-padded to the 100 ms API minimum.

There are no automatic WebSocket reconnections, file-transcription fallbacks or audio replays. A persisted sending marker prevents replay after an unknown outcome. A disconnected or restarted session becomes broken; queued/new chunks remain saved without opening a context-free replacement. If no OpenAI session had started before restart, the first unsent chunk can open it normally. Failed or unknown audio turns stop note generation with a consultation-level failure message. Doctors are never asked to review or transcribe chunks. No incomplete transcript is silently used for a final note. Once complete, the ordered transcript is structured exactly once per consultation. A failed or interrupted clinical request is retained as failed without paid retry.

The single complete recording is local_data/<consultation_id>/raw.wav, including silence. SQLite keeps durable transport blocks and an API usage ledger for recovery; chunks/*.wav and each chunk's calls.json preserve transcription inputs and accounting. Final transcript.txt and original_note.json are separate evidence artifacts. Usage, model and service tier are recorded when returned, with elapsed time for each committed turn. WebSocket request IDs identify the connection handshake, not independent HTTP requests per chunk; session/item/event IDs provide turn-level correlation. Missing API usage stays unknown rather than being inferred from local audio duration. Combined API call time is not the wall-clock consultation duration. Reported billing still requires reconciliation with OpenAI Costs.

Denied findings are hidden from the clinical sheet and print, but retained in original evidence. Uncertain findings show a plain parenthetical qualifier rather than a dropdown. Do not infer absence from a hidden finding.

Offline validation: openai_trial/.venv/Scripts/python.exe backend/test_flow.py and backend/test_streaming.py (from this folder, using the sibling Python path). Tests replace OpenAI with a fake client. The microphone/real API path must be tried by the user through localhost.

The VAD and WebSocket dependencies are isolated in backend/vendor (webrtcvad-wheels==2.0.14 and websockets==15.0.1). To reinstall with the sibling Python environment: python -m pip install --target backend/vendor -r backend/requirements.txt. It is not committed as application source.

Progress indicators distinguish recording, transcription, final saving, note preparation and failure. The doctor interface contains no chunk correction workflow. Usage JSON is a best-effort export of the SQLite ledger; export failure cannot discard a returned transcript.
