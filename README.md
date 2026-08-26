# TUTORial

**The notebook that quizzes you back.**

A student photographs a problem they are stuck on. TUTORial does *not* solve it for
them. Instead Claude writes a **similar** problem, works that one on screen in six
honest steps, and after each step stops the video and asks the student to make that
same move on **their own** problem. Nothing unlocks until they produce the step
themselves. A wrong answer earns its own video — drawn on their figure — showing
where that answer came from, and then sends them back to try again.

The thesis the whole thing argues: asking a chatbot for "a similar problem" hands you
a finished solution to read, which feels like learning but isn't. Segmented video plus
forced retrieval is a different learning event. The **Why It Works** tab makes the
case; **Works Cited** carries the 30 sources in MLA 9th.

---

## Running it

No system dependencies — no ffmpeg, no local rendering, no GPU. Video generation
happens in Qwen, on Alibaba Cloud Model Studio.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# prototyping stage: both keys are literals in the source
#   app.py →  ANTHROPIC_API_KEY = "sk-ant-..."
#             DASHSCOPE_API_KEY = "sk-..."

python app.py            # http://127.0.0.1:5000
```

The first run downloads the three notebook typefaces (Lilita One, Patrick Hand,
Caveat) into `static/fonts/`. They are only a reference for the SVG Claude writes —
the frames themselves are rendered by Qwen, so an offline first run costs nothing.

Qwen knobs live at the top of `app.py`:

| | |
|---|---|
| `QWEN_MODEL` | `wan2.7-t2v` — generally available, 1080p, native synchronised audio, 15-second ceiling. `wan3.0-t2v` would double that to 30s but its Model Studio API is still in preview; raise `QWEN_MAX_SECONDS` to 30 if you switch. |
| `QWEN_MAX_SECONDS` | The clip ceiling, and therefore the narration budget. |
| `QWEN_SIZE` | `1920*1080`. DashScope spells sizes with a star, not an x. |
| `DASHSCOPE_REGION` | `intl` points the SDK at the Singapore host; use `cn` for a mainland account. The SDK ships pointed at mainland, so an international key fails until this is right. |
| `WORDS_PER_SECOND` / `BEAT_SECONDS` | The speaking-pace estimate that turns a narration into a duration. |

**Note:** the 15-second ceiling is what makes the narration budget tight — about 25
words for a whole step. If steps start reading clipped, either lower `FRAMES_PER_STEP`
(fewer keyframes means fewer inter-line beats eating the clip) or move to
`wan3.0-t2v` at `QWEN_MAX_SECONDS = 30` once it leaves preview on your account.

## The pipeline

Claude draws a flipbook; Qwen animates it. Nothing renders locally.

```
photo of the problem
        │
        ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ Claude (vision)                        build_plan()         │
  │  · reads the ORIGINAL problem                               │
  │  · invents a SIMILAR problem, solved in 6 steps             │
  │  · writes 6 checkpoint questions about the ORIGINAL problem │
  └─────────────────────────────────────────────────────────────┘
        │
        ▼   for each step i
  ┌─────────────────────────────────────────────────────────────┐
  │ Claude                            build_step_scenes()       │
  │  returns 3-5 flipbook keyframes, each:                      │
  │    svg        a complete SVG document for that beat         │
  │    narration  the line spoken over it                       │
  │                                                             │
  │  Every frame repeats the last one unchanged and ADDS one    │
  │  new mark. The page only ever accumulates — so the          │
  │  difference between frames IS the animation.                │
  └─────────────────────────────────────────────────────────────┘
        │
        ▼                                  create_qwen_video()
   the whole flipbook becomes one prompt: the animation and narration
   instructions, then KEYFRAME n OF N with its line and its raw SVG
        │
        ▼
   VideoSynthesis.async_call(model="wan2.7-t2v", prompt=..., size=...,
                             duration=<seconds the narration needs>)
   poll VideoSynthesis.fetch() through PENDING → RUNNING → SUCCEEDED
   download output.video_url → step_i.mp4
   mp4_duration() reads the real length out of the file's mvhd atom
        │
        ▼
   video plays → stops at the checkpoint → student answers
        │
        ├── correct   → generate step i+1 (prefetched while they read the ✓)
        └── incorrect → build_miss_scenes(): a second flipbook, on THEIR problem,
                        showing the wrong move and why it breaks → try again
```

The SVG goes into the Qwen prompt as plain text — Qwen reads it as the exact artwork
for that beat and animates between beats, so new labels and lines appear to be written
onto one continuous page rather than cutting between slides. `SVG_STYLE_RULES` pins
down the notebook substrate, the palette, the three type voices, and the accumulate-only
flipbook rule that makes the interpolation legible.

**Why the narration is budgeted.** DashScope has no equivalent of Sora's extensions
endpoint, so a step cannot be stretched across several clips without concatenating them
locally — which would drag ffmpeg back in. Instead the dependency runs the other way:
the narration is sized to the clip. `narration_word_budget()` turns `QWEN_MAX_SECONDS`
into a word count, both scene prompts state it as a hard limit, and `fit_narration()`
catches anything that still overruns and asks Claude once for a shorter cut of the same
lines. `fit_duration()` then requests exactly the seconds that narration needs, up to the
ceiling. A step therefore always fits inside one clip and is never cut off mid-sentence.

## Layout

```
app.py                 everything: config, Claude calls, the Qwen call, jobs, routes
templates/index.html   the notebook — the whole interface and its state machine
static/sample-problem.png
static/fonts/          fetched on first run
media/<session>/       generated videos and uploads (in-memory sessions, ephemeral)
```

### Routes

| | |
|---|---|
| `POST /api/upload` | takes the photo (or falls back to the sample), opens a session |
| `POST /api/start` | plan + first segment, as one background job |
| `POST /api/segment` | draw the flipbook for step *i* and have Qwen animate it |
| `POST /api/answer` | mark a checkpoint; on a miss, kicks off the correction video |
| `GET /api/job/<id>` | poll a background job: stage, label, detail, result |
| `GET /api/state/<sid>` | the whole lesson as the browser is allowed to see it |
| `POST /api/reset` | same lesson, scores cleared |

Correct answers, `target` values, affirmations and explanations never reach the
browser — `public_checkpoint()` strips them and `/api/answer` marks server-side.
Claude's SVG only ever travels inside the Qwen prompt on the server; it never enters
the page DOM.

## What is deliberately unfinished

- **Sessions live in memory.** A restart empties them; the media on disk is orphaned.
  Fine for the prototype, wrong for anything else.
- **No auth, no rate limiting, two hardcoded keys.** `app.run()` is the dev server.
- **Narration length is estimated, not measured.** `speaking_seconds()` counts words at
  `WORDS_PER_SECOND` and adds a beat per line. It has never heard the voice Qwen will
  use, so the budget is a guess — tune the two constants once you have seen real output.
- **A 15-second ceiling caps how much a step can say.** About 25 words for a whole
  step — roughly five per keyframe at the maximum of five. That is a real pedagogical
  constraint, not just a technical one: a step needing more gets tightened rather than
  split, so expect terse narration and watch whether it costs clarity on harder steps.
- **Qwen interprets, it does not rasterise.** The SVG is a very strong hint, not a
  guarantee — expect the wording and geometry to drift from what Claude drew. If a step
  needs exact figures, this is the wrong renderer for it.
- **Reasoning accuracy is validated on geometry only.** The pipeline is subject-agnostic
  by construction, but nothing checks Claude's arithmetic beyond the checkpoint
  disagreement itself.
