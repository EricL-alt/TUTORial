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

```bash
# ffmpeg AND ffprobe must both be on PATH — videopython calls them as bare names
# and offers no path override. Video.save() encodes with one, VideoMetadata.from_path()
# probes with the other, so half a pair is not enough.
brew install ffmpeg                                        # macOS
sudo apt-get install -y --no-install-recommends ffmpeg      # Debian/Ubuntu

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# prototyping stage: the key is a literal in the source
#   app.py →  ANTHROPIC_API_KEY = "sk-ant-..."

python app.py            # http://127.0.0.1:5000
```

**Running from an IDE?** PyCharm and friends often launch Python without your login
shell's `PATH`, so an ffmpeg you already installed can still be invisible. On startup
the app checks the usual install directories (`/opt/homebrew/bin`, `/usr/local/bin`,
…) and repairs `PATH` for its own process if it finds both binaries there. If it
can't, it prints an unmissable banner with the install command, and every lesson
fails fast — before spending a Claude call — with that same message on the page.

The first run downloads the three notebook typefaces (Lilita One, Patrick Hand,
Caveat) into `static/fonts/` and registers them with fontconfig, so the generated
video frames are typeset in the same hands as the interface. Offline, cairo
substitutes and everything still renders.

Optional: `pip install "videopython[ai]"` (several GB — torch and friends) gives every
scene a spoken narration track through videopython's `TextToSpeech`. Without it each
frame carries its narration as an on-screen caption and the video is silent; the page
shows the same lines as a live transcript beside the video either way.

---

## The pipeline

Everything runs through `videopython` — the SVG frames are rasterised, turned into
clips, and stitched by `VideoEdit` with the crossfades Claude picked.

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
  │  returns 2–4 scenes, each:                                  │
  │    svg           a complete notebook frame                  │
  │    narration     what the voice says over it                │
  │    transition_in {type, duration} from videopython's        │
  │                  curated xfade catalog — how this frame      │
  │                  should flow out of the last one            │
  └─────────────────────────────────────────────────────────────┘
        │
        ▼                                    render_scenes()
   sanitize_svg → cairosvg → numpy frame
        │
        ▼
   Video.from_image(...).add_audio(tts)  →  scene_i.mp4
        │
        ▼
   VideoEdit(segments=[SegmentConfig(source, start, end,
                operations=[Resize(1280,720)],
                transition_in=TransitionSpec(type, duration))])
        .run_to_file("step_i.mp4")
        │
        ▼
   video plays → stops at the checkpoint → student answers
        │
        ├── correct   → generate step i+1 (prefetched while they read the ✓)
        └── incorrect → build_miss_scenes(): a second video, on THEIR problem,
                        showing the wrong move and why it breaks → try again
```

Claude is asked for SVG rather than image prompts because the frames need to be
*legible diagrams*, and because consecutive frames can then be near-identical apart
from the new mark — which is exactly what makes a `dissolve` read as annotation
appearing on a page rather than a slide change. The style rules in `SVG_STYLE_RULES`
pin down the notebook substrate, the palette, the three type voices, and the
continuity contract that the transition choice depends on.

## Layout

```
app.py                 everything: config, Claude calls, SVG → video, jobs, routes
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
| `POST /api/segment` | draw and stitch step *i* |
| `POST /api/answer` | mark a checkpoint; on a miss, kicks off the correction video |
| `GET /api/job/<id>` | poll a background job: stage, label, detail, result |
| `GET /api/state/<sid>` | the whole lesson as the browser is allowed to see it |
| `POST /api/reset` | same lesson, scores cleared |

Correct answers, `target` values, affirmations and explanations never reach the
browser — `public_checkpoint()` strips them and `/api/answer` marks server-side.
Claude's SVG is rasterised server-side and never enters the page DOM; `sanitize_svg()`
strips scripting and off-canvas references before cairosvg sees it.

## What is deliberately unfinished

- **Sessions live in memory.** A restart empties them; the media on disk is orphaned.
  Fine for the prototype, wrong for anything else.
- **Reasoning accuracy is validated on geometry only.** The pipeline is
  subject-agnostic by construction, but nothing checks Claude's arithmetic beyond
  the checkpoint disagreement itself.
- **No auth, no rate limiting, one hardcoded key.** `app.run()` is the dev server.
- **Generation is slow** — roughly 20–60s for the plan plus the first segment,
  then 15–40s per subsequent step. The next step is prefetched while the student
  reads their ✓, so most of that is hidden; the first build is not.
