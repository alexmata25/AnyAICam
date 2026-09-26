# AACO Phase 1 Handoff

Base: `2672fb48a5fe78978ee3b6d6307e172327361195` on `feature/aaco-command-engine` in `C:\Users\Alejandro Mata\OneDrive\Desktop\AnyAiCam-AACO-command-engine`.

Created `app/aaco.py`: a provider-independent strict command schema, deterministic safe language adapter, authorization-aware VMS boundary, and storage-neutral `RecordingResolver` protocol. Five command families: live view, camera/time playback, event search, camera status, and playback context navigation. Unknown/destructive/ambiguous requests return clarification. Execution cannot access SQL, shell, S3, camera credentials, or arbitrary APIs.

Tests: `app/tests/test_aaco.py` covers the five families, ambiguity/destructive rejection, and cross-customer camera denial. Classic VMS is untouched.

Not deployed. Ryzen remains `48992a5`; Samsung unchanged; AWS unchanged; PR #15 remains open/unmerged.

Phase 2: bind the VMS boundary to existing authorized VMS service APIs, add an LLM provider adapter that may only emit the schema, and validate through staging before considering an appliance-relevant delta. Do not blindly deploy cloud-only AACO code to Ryzen.
