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
| `QWEN_MODEL` | `wan3.0-video-prime` — Wan3.0 Prime, the high-speed Wan3.0 video model, with audio. Available on the free tier. **This is the ID the console shows; `wan3.0-t2v` is not a Model Studio ID and is refused with `AccessDenied.Unpurchased`.** |
| `QWEN_MAX_SECONDS` | How long ONE clip may run. Wan3.0 Prime takes an integer 2-30. |
| `MAX_STEP_SECONDS` | How long a whole step may run, which sets the narration budget. At 30s that is ~61 words and usually a single clip; a step needing more spills into a second. |
| `QWEN_RESOLUTION` / `QWEN_RATIO` | `720P` and `widescreen`. Note this model takes **`resolution` and `ratio`**, not `size` — passing `size` is the wrong shape. |
| `DASHSCOPE_REGION` | `intl` points the SDK at the Singapore host; use `cn` for a mainland account. The SDK ships pointed at mainland, so an international key fails until this is right. |
| `WORDS_PER_SECOND` / `BEAT_SECONDS` | The speaking-pace estimate that turns a narration into a duration. |

**Note:** output is billed **per second**, so `MAX_STEP_SECONDS` is the cost dial as
well as the pedagogy one. At the default 30s a six-checkpoint lesson is around seven
clips of ~30s. Raising it buys words at a linear cost; dropping `QWEN_RESOLUTION` to
`480P` is the cheaper lever if the notebook text still reads.

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
   plan_clips() splits the flipbook into clips that each fit the 30s
   ceiling. Clip k+1 opens on clip k's CLOSING keyframe — the page as
   the student last saw it — so the seam lands on identical artwork
        │
        ▼   for each clip k
   VideoSynthesis.async_call(model="wan3.0-video-prime", prompt=...,
                             resolution="720P", ratio="widescreen",
                             duration=<seconds that clip's lines need>)
   poll VideoSynthesis.fetch() through PENDING → RUNNING → SUCCEEDED
   download output.video_url → step_i_k.mp4
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

**How a step outruns the clip ceiling.** DashScope has no extensions endpoint, and
concatenating locally would drag ffmpeg back in — but neither is needed. Because the
flipbook only ever accumulates, the last keyframe of one clip is *already* a valid
opening frame for the next. So `plan_clips()` splits a step into N clips, hands each one
the previous clip's closing SVG as an `OPENING FRAME` it must start on without
re-narrating or re-establishing, and the player runs them back to back as a single
segment. The seam lands on identical artwork, and the checkpoint only fires when the
last clip ends.

Two guards keep anything from being cut off. `fit_narration()` shortens a step that runs
past `MAX_STEP_SECONDS` — and, separately, any *single* line longer than one clip, since
no amount of splitting saves a line that cannot fit in the clip it is spoken over.
`fit_duration()` then asks for exactly the seconds each clip's lines need, so short clips
stay short instead of padding to the ceiling.

## Layout

```
app.py                 everything: config, Claude calls, the Qwen call, jobs, routes
templates/index.html   the notebook — the whole interface and its state machine
static/sample-problem.png
static/fonts/          fetched on first run
media/<session>/       step_<i>_<k>.mp4 clips and uploads (in-memory sessions, ephemeral)
```

### Routes

| | |
|---|---|
| `POST /api/upload` | takes the photo (or falls back to the sample), opens a session |
| `POST /api/start` | plan + first segment, as one background job |
| `POST /api/segment` | draw the flipbook for step *i* and have Qwen animate it as N chained clips |
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
- **Output is billed per second, and a step is 30 of them.** `MAX_STEP_SECONDS` and
  `QWEN_RESOLUTION` are the two dials; a step that spills into a second clip doubles
  its cost.
- **The seams are only as good as Qwen's obedience.** Each clip is told to open on the
  previous one's closing frame without redrawing or re-establishing it. If it ignores
  that, a seam will show as a visible jump. Worth watching on the first real run.
- **Qwen interprets, it does not rasterise.** The SVG is a very strong hint, not a
  guarantee — expect the wording and geometry to drift from what Claude drew. If a step
  needs exact figures, this is the wrong renderer for it.
- **Reasoning accuracy is validated on geometry only.** The pipeline is subject-agnostic
  by construction, but nothing checks Claude's arithmetic beyond the checkpoint
  disagreement itself.
