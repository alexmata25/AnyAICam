# AACO local AI/NLU layer — model/runtime audit and integration design (2026-09-19)

Phase 4 of AACO: a local natural-language layer sitting in front of the
existing, already-shipped deterministic command engine
(`app/aaco.py`, merged `deb302c4`; `app/main.py`'s `_ClassicAacoBoundary`,
including `unlock_door`, merged `2483bec4`). This document is the
audit and design the code in `app/aaco_llm.py` implements; see that
file's own docstring for the runtime safety argument in detail.

## Hardware/dependency audit

**AnyAiCam's existing Python ML footprint** (`requirements.txt` /
`requirements-cpu.txt`, both already shipped to every VMS
container — cloud portal and Ryzen edge alike): `torch==2.5.1+cpu`,
`torchvision==0.20.1+cpu` (CPU-only wheels, no CUDA anywhere in this
stack), `ultralytics==8.3.40` (YOLOv8, person/vehicle/PPE detection),
`opencv-python`, `pytesseract` (LPR OCR). This is a real, working
CPU-only inference precedent already proven in production on both
environments — but it is a *vision* stack (image tensors through a
CNN), not a text/LLM stack, and pulling a decoder-style LLM through
PyTorch would mean loading an entire second multi-hundred-MB framework
already resident just for one narrow text-classification-shaped task.

**Ryzen (the physical appliance)**: 8-core AMD CPU, 12GB RAM, **no
GPU** — confirmed by `requirements-cpu.txt`'s own CPU-only PyTorch
pin, which would be pointless if any AnyAiCam deployment target had a
CUDA-capable GPU to target instead. Real, live-measured utilization
across this project's own history (`docs/PROJECT_CHECKPOINT.md`,
`docs/checkpoints/RYZEN.md`): sustained **700–800% CPU** (of 800%
available across 8 cores) from five concurrent camera pipelines — RTSP
decode, motion detection, YOLO person/vehicle detection, Live Relay
`libx264` re-encode, and event-clip encoding — with **effectively zero
CPU headroom** at almost any point this appliance has been observed.
RAM is comparatively less pressured (typically 5–7GB of 12GB used) but
still not abundant.

**The staging cloud instance** (`i-0a082abd812929bb4`, where AACO
actually runs today): `t3.medium` — **2 vCPU (1 core/2 threads), 4GB
RAM**, burstable/shared. This project has already been burned twice by
this exact box's tight memory margin causing real OOM-killed containers
from nothing more than container sprawl, with as little as 2.2–3.7GB
"available" at times. This is meaningfully *smaller* than Ryzen, not
larger.

**Conclusion driving the model/runtime choice below**: neither
environment has GPU acceleration, Ryzen has no CPU headroom to spare,
and the cloud host has very little RAM margin to spare. "Smallest
practical" is not a preference here, it is close to a hard requirement
in either direction.

## Model/runtime recommendation

**Runtime: [llama.cpp](https://github.com/ggerganov/llama.cpp) via the
`llama-cpp-python` bindings.** Deliberately *not* PyTorch/`transformers`:
llama.cpp is a purpose-built, dependency-light, pure-CPU C++ inference
engine for quantized GGUF models, with no CUDA requirement and a far
smaller runtime memory footprint than loading an LLM through the
existing torch stack would need. It is the de facto standard smallest-
footprint way to run a local LLM on CPU-only hardware, which is what
both AnyAiCam environments are, categorically, with no exception
anywhere in this codebase's own dependency history.

**Model: a small instruction-tuned model in GGUF, `Q4_K_M` quantization
— recommend starting with Qwen2.5-1.5B-Instruct** (~1GB on disk,
roughly 1.5–2GB resident at inference, no GPU). This is a
*recommendation to validate against real traffic before finalizing*,
not a final decision made here, matching this project's own standing
practice for infrastructure choices:

- **Qwen2.5-0.5B-Instruct** (~350–400MB Q4_K_M) is the absolute floor
  of "smallest practical" and worth trying first if validation-pass-rate
  data (how often `_validate_ai_command()` actually accepts its output)
  turns out acceptable — but 0.5B models are meaningfully less reliable
  at consistent structured-JSON output, which just means more requests
  fall back to the deterministic grammar's own clarification, safe but
  a worse user experience.
- **Qwen2.5-1.5B-Instruct** is the recommended starting point: small
  enough to be a "smallest practical" answer, large enough to reliably
  hold the fixed JSON shape and few-shot pattern this narrow task needs.
- If 1.5B's real validation-pass rate is too low once measured, the
  next step up (not smaller) would be something like
  Llama-3.2-3B-Instruct — still CPU-only-plausible, still no GPU
  needed, just a bigger memory/latency cost.

**Why this fits Ryzen's hardware profile in principle, but is not
recommended to run there today.** llama.cpp + a ~1GB quantized model
needs no GPU and no framework Ryzen doesn't already conceptually support
(it is, if anything, a *lighter* dependency than the torch/YOLO stack
already running there) — so nothing about the *model choice itself* is
Ryzen-incompatible. But Ryzen has **~0% real CPU headroom** today, and
AACO is, and remains, a *cloud-side, customer-portal* feature — nothing
in this product runs it on the edge appliance. Recommending Ryzen as an
inference host would mean either genuinely new hardware or accepting
degraded camera-pipeline performance for a feature that doesn't need to
be there at all. **The correct read of "fits Ryzen's constraints" here
is: chosen specifically small enough that it *could* run there without
requiring new hardware if a future phase ever had a real reason to move
it edge-side — not a plan to actually put it there.**

**Why not the current cloud host either, unmodified.** `t3.medium`'s 4GB
RAM is already tight from container count alone. Loading a resident
~1.5–2GB model process into that same box, continuously, is a real risk
of recreating this project's own prior OOM incidents. **Recommendation:
if/when this is actually turned on, run inference either on a separate,
appropriately-sized instance, or only after resizing/isolating the
portal host — not colocated with the current burstable box as-is.**
This is a deployment decision for a later, separately gated pass, not
made here.

## Integration design

```
customer text  ->  NaturalAacoLanguageAdapter.parse()
                       |
                       |-- 1. try LocalNlpInterpreter.interpret() (if enabled+configured)
                       |         |
                       |         `-- LlamaCppInterpreter._generate()  (the ONLY place inference happens)
                       |                 -> raw text -> extract JSON -> _validate_ai_command()
                       |                                                     |
                       |                                     valid  --------+------->  AacoCommand  (used immediately)
                       |                                     invalid/unsafe -+------->  Clarification (discarded by the adapter)
                       |
                       `-- 2. aaco.DeterministicLanguageAdapter().parse()  (always tried; the ONLY source of
                                                                             any clarification text a customer
                                                                             ever actually sees)
                                   |
                                   `-->  AacoCommand  or  Clarification
```

`NaturalAacoLanguageAdapter` never calls `execute()`, never touches a
`VmsBoundary`, never imports `door_access`/`relay_control`/`partner_db`
— it has no way to reach a VMS action even in principle. Everything it
can ever produce is one of the two existing `aaco.py` types, handed to
the *unmodified* `execute()`/`_ClassicAacoBoundary` path, which still
performs every tenant/`can_live`/`can_playback`/`can_unlock` check it
already did before this phase existed.

**How AI output is constrained (`_validate_ai_command()`, the actual
security boundary, not the prompt)**: the model's raw text is treated
as untrusted input, identically to a hand-typed command. It is
re-parsed as JSON and accepted only if *every* field matches the exact
closed shape `AacoCommand` itself allows: `operation` must be one of
`aaco.Operation`'s own seven literal values (`typing.get_args`, not a
hand-maintained duplicate list); `camera_id` must match the identical
token grammar the regex grammar already produces (`camera-<n>` or
`camera-name:<text>`, via the same shape as `aaco_web.py`'s own
`_CONTEXT_CAMERA` pattern); `event_type` must be `person`/`vehicle`/`car`;
`offset_minutes` a plain positive int (not a bool, which is a `int`
subclass in Python — explicitly excluded); `start`/`end` real ISO
datetimes within a year-back/one-day-forward sanity window; every
operation's own specific requirement `execute()` itself enforces
(e.g. `unlock_door` needs a `camera_id`, `event_search` needs a
time range) is re-checked here too, before the model's output ever
reaches `execute()`. **Any unrecognized key, unknown operation, or
malformed value — including deliberately adversarial content like
`"camera_id": "'; DROP TABLE cameras; --"` or `"../../etc/passwd"` — is
rejected identically to a genuine parse failure**: `Clarification`,
never a best-effort guess, never an exception that could propagate
somewhere unexpected. No model-authored free text is ever shown to a
customer; a model output that fails validation is silently discarded,
and the customer sees the same fixed clarification message the
existing regex grammar already produces.

**Fallback behavior**: the deterministic regex grammar (`aaco.
DeterministicLanguageAdapter`, completely unmodified) is *always* also
tried — the local model is never removed from the equation, only ever
added on top. If local inference is disabled (`ANYAICAM_AACO_LOCAL_LLM_
ENABLED` unset/false, the default everywhere today), if no model file
is configured, if the package/model fails to load, if inference itself
raises or times out, or if the model's output fails strict validation —
every one of these is handled identically: fall straight through to the
regex grammar, with zero special-casing needed anywhere else in the
system. An exact fixed phrase like `"Show Camera 4"` parses exactly the
same whether or not local inference is enabled.

## Files changed this pass

- **`app/aaco_llm.py`** (new): `InterpreterUnavailable`,
  `NaturalLanguageInterpreter` (Protocol), `_validate_ai_command()` (the
  security boundary), `LlamaCppInterpreter` (the one real local-inference
  implementation, lazy-imports `llama_cpp` only when actually invoked),
  `NaturalAacoLanguageAdapter` (the orchestration layer), `default_interpreter()`
  (returns `None` — today's unchanged behavior — unless both
  `ANYAICAM_AACO_LOCAL_LLM_ENABLED=true` and `ANYAICAM_AACO_LLM_MODEL_PATH`
  point at a real file).
- **`app/aaco_web.py`**: `register_aaco_routes()` gained an optional
  `language_adapter_factory` parameter (default: the existing
  `DeterministicLanguageAdapter`, so every other caller/test is
  unaffected), constructed once at route-registration time rather than
  per-request — the correct lifetime for something that may load a
  model.
- **`app/main.py`**: one new `_aaco_language_adapter()` helper, passed
  into the existing `register_aaco_routes(...)` call — the only call
  site that decides real local inference is even attempted, and today
  it always resolves to `NaturalAacoLanguageAdapter(None)` (pure regex)
  since the env var is unset everywhere.
- **`app/tests/test_aaco_llm.py`** (new, 25 tests): the full validator
  test matrix (valid commands for every operation; rejection of unknown
  operations, extra keys, malformed/adversarial `camera_id` values,
  out-of-range timestamps, invalid `event_type`/`offset_minutes`,
  operation-specific missing-field cases); `NaturalAacoLanguageAdapter`
  behavior with a fake interpreter (valid output used directly,
  `InterpreterUnavailable` and `Clarification` both fall through to
  regex, an exact regex phrase still wins even with a broken fake
  interpreter installed); the five natural-language examples mapped
  through a fake interpreter onto the correct existing `AacoCommand`;
  `default_interpreter()`'s three on/off combinations; `LlamaCppInterpreter`
  plumbing (JSON extraction from chatter, `InterpreterUnavailable` for a
  missing model file, a real inference exception wrapped safely) — none
  of this requires the `llama-cpp-python` package or a real model file
  to be installed anywhere.

No changes to `app/aaco.py` or `app/main.py`'s `_ClassicAacoBoundary` —
this phase adds a layer in front of that boundary, it does not modify it.

## Not done this pass, on purpose

- `llama-cpp-python` is not added to any `requirements*.txt` — this
  stays a fully optional dependency (lazy-imported only inside
  `LlamaCppInterpreter._load()`) until real deployment is separately
  authorized, so nothing about this phase can affect any existing
  build.
- No GGUF model file was downloaded, bundled, or referenced by path
  anywhere real — `ANYAICAM_AACO_LLM_MODEL_PATH` stays unset.
- No staging or production deployment of this code.
- Ryzen untouched.

## Addendum (2026-09-19): real staging measurement

The above was a paper audit; this is what actually happened once it
was validated for real on the staging box (`i-0a082abd812929bb4`).

**Host decision, made with real current numbers, not the general audit
above.** Re-measured staging at validation time: `t3.medium`, 2 vCPU,
3.7GB RAM with only **2.3GB "available"** and disk at **86-89% full
(3.1-4.1GB free)** — tighter than the general audit assumed. Ryzen was
not re-touched (per its own 700-800%-of-800% CPU history, already
disqualifying, and out of scope for a cloud-side feature). A genuinely
separate inference host/container was rejected too: the shipped
`aaco_llm.py` design runs inference **in-process** inside the same
FastAPI worker that serves the whole portal — splitting it out would
mean building a new internal service this pass never asked for, and
would not even remove the real resource contention (same host, same
memory pool) without also requesting a second EC2 instance, i.e. a
new-host purchase. **Verdict: smallest safe option is in-process on
the existing staging EC2, using the smaller Qwen2.5-0.5B-Instruct
model (not the originally-recommended 1.5B)** — 0.5B's ~500-680MB
resident footprint leaves real margin on this box; 1.5B's ~1.5-2GB
would not.

**Real bug found and fixed: the interpreter was calling the wrong
llama.cpp API.** `LlamaCppInterpreter._generate()` called the model as
a bare single-string completion. Qwen2.5-Instruct GGUF models are
fine-tuned specifically for the ChatML template; measured result
before the fix was a **0% `_validate_ai_command()` pass rate** across
every test phrase (including trivial ones) and **38-77 second
latency** per request. Switching to `create_chat_completion()` (system
role = `_SYSTEM_PROMPT`, user role = the customer's text, prompt
content otherwise unchanged) measured correct output in **3.5-8.3
seconds** on the same hardware for the same phrases. Fixed in
`app/aaco_llm.py`, commit `38762ef`.

**Real measured accuracy on the requested example phrases** (post-fix,
Qwen2.5-0.5B-Instruct, isolated non-customer-facing test container on
the real staging host):

| Phrase | Result | Correct? | Latency |
|---|---|---|---|
| "Show me the front door" | `live_view`, `camera-name:front door` | yes | 19.5s |
| "Which cameras are down?" | `camera_status` | yes | 1.8s |
| "Open the front door for me" | `live_view` (should be `unlock_door`) | no -- wrong but still a validly authorized, non-dangerous action | 3.5s |
| "Take me back twenty minutes on the driveway" | fell back to regex/`Clarification` | ambiguous without prior playback context, arguably correct to decline | 10.9s |
| "Were there any people at the front entrance in the last hour?" | fell back to `Clarification` | no -- likely because the matching few-shot example's `start`/`end` are the literal placeholders `"<now-1h>"`/`"<now>"`, which a 0.5B model has no real pattern to compute a real ISO timestamp from | 14.9s |

Adversarial/out-of-domain fallback (the actual hard safety requirement)
was **100% correct**: "What's the weather like today?" and gibberish
input both safely fell through to the fixed clarification message in
1.4-9.0 seconds, with no exception, no bypass, no adversarial value
ever reaching `execute()`.

**Why this was not cut over to the real customer-facing staging
container.** Two independent findings argue for staying at
architecture-proof rather than customer-facing yet:
1. **Accuracy is partial** (2 of 5 example phrases fully correct; one
   wrong-but-safe; two safe fallbacks, one of which is a real miss on
   a phrase the system prompt's own few-shot example should cover).
2. **`LOCAL_LLM_TIMEOUT_SECONDS` (defined in `aaco_llm.py`) is not
   actually enforced anywhere** — inference is a synchronous, CPU-
   bound call inside an `async` route handler on a single-worker
   process (`web_concurrency: 1`, confirmed from this box's own
   startup log). A slow generation (measured up to 19.5s here) blocks
   that one worker for its entire duration, which on a single-worker
   process means **every other customer's request to this portal --
   typed AACO, Live View, anything -- would queue behind it**, not
   just the one AACO request. This is a real, pre-existing gap in the
   already-merged code, surfaced only by actually running it, and is
   the concrete prerequisite before any customer-facing enablement
   (even in staging): wrap the blocking call in a bounded thread/
   timeout so a slow or hung generation degrades only its own request.

**What was validated, concretely, on the real staging host:** a
throwaway, non-network-aliased sibling container (`portal-9cccbf4-
llmtest`) built from the same git-verified staging image, with
`llama-cpp-python` built from PyPI's official source distribution
(the project's third-party wheel-index install attempt was blocked by
this environment's own safety classifier and not pursued further) and
the real `Qwen2.5-0.5B-Instruct-GGUF` `Q4_K_M` file (491MB, from
`huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF`) downloaded onto a
dedicated host volume. `ANYAICAM_AACO_LOCAL_LLM_ENABLED` was never set
on the real customer-facing `portal-9cccbf4` container; that container
was never restarted, never modified, and served customer traffic
uninterrupted throughout. The test container was stopped (not
deleted) after measurement, releasing its ~680MB back to the host.
