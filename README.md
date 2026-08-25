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
happens in Sora.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# prototyping stage: both keys are literals in the source
#   app.py →  ANTHROPIC_API_KEY = "sk-ant-..."
#             OPENAI_API_KEY    = "sk-proj-..."

python app.py            # http://127.0.0.1:5000
```

The first run downloads the three notebook typefaces (Lilita One, Patrick Hand,
Caveat) into `static/fonts/`. They are only a reference for the SVG Claude writes —
the frames themselves are rendered by Sora, so an offline first run costs nothing.

Sora knobs live at the top of `app.py`: `SORA_MODEL` (`sora-2` or `sora-2-pro`),
`SORA_SECONDS` (the API accepts only `"4"`, `"8"` or `"12"`) and `SORA_SIZE`
(`1280x720` landscape, to match the taped-in player).

## The pipeline

Claude draws a flipbook; Sora animates it. Nothing renders locally.

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
        ▼                                  create_sora_video()
   build_sora_prompt() lays the whole flipbook out as text:
   the animation instructions, then KEYFRAME n OF N with its
   narration and its raw SVG, in order
        │
        ▼
   client.videos.create(model="sora-2", prompt=..., seconds="12", size="1280x720")
   poll retrieve() through queued → in_progress → completed
   download_content(id, variant="video").write_to_file(step_i.mp4)
        │
        ▼
   video plays → stops at the checkpoint → student answers
        │
        ├── correct   → generate step i+1 (prefetched while they read the ✓)
        └── incorrect → build_miss_scenes(): a second flipbook, on THEIR problem,
                        showing the wrong move and why it breaks → try again
```

The SVG goes into the Sora prompt as plain text — Sora reads it as the exact artwork
for that beat and animates between beats, so new labels and lines appear to be written
onto one continuous page rather than cutting between slides. `SVG_STYLE_RULES` pins
down the notebook substrate, the palette, the three type voices, and the accumulate-only
flipbook rule that makes the interpolation legible.

## Layout

```
app.py                 everything: config, Claude calls, the Sora call, jobs, routes
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
| `POST /api/segment` | draw the flipbook for step *i* and have Sora animate it |
| `POST /api/answer` | mark a checkpoint; on a miss, kicks off the correction video |
| `GET /api/job/<id>` | poll a background job: stage, label, detail, result |
| `GET /api/state/<sid>` | the whole lesson as the browser is allowed to see it |
| `POST /api/reset` | same lesson, scores cleared |

Correct answers, `target` values, affirmations and explanations never reach the
browser — `public_checkpoint()` strips them and `/api/answer` marks server-side.
Claude's SVG only ever travels inside the Sora prompt on the server; it never enters
the page DOM.

## What is deliberately unfinished

- **Sessions live in memory.** A restart empties them; the media on disk is orphaned.
  Fine for the prototype, wrong for anything else.
- **No auth, no rate limiting, two hardcoded keys.** `app.run()` is the dev server.
- **Segments are a fixed length.** Sora accepts only 4, 8 or 12 seconds, so every step
  gets the same budget regardless of how much it has to say, and the narration is paced
  evenly across it rather than timed to a real voice track.
- **Sora interprets, it does not rasterise.** The SVG is a very strong hint, not a
  guarantee — expect the wording and geometry to drift from what Claude drew. If a step
  needs exact figures, this is the wrong renderer for it.
- **Reasoning accuracy is validated on geometry only.** The pipeline is subject-agnostic
  by construction, but nothing checks Claude's arithmetic beyond the checkpoint
  disagreement itself.
