# TUTORial

**Teaching students to use AI properly. TUTORial itself is only the worked example.**

The site now has two halves.

**The mission and the curriculum** are the point. TUTORial is not the best use anyone
has found for artificial intelligence, and the front page says so in as many words. It
is *an example* of a use that satisfies four properties the course teaches: it augments
rather than replaces, it keeps a human accountable, it is honest about being wrong, and
it can be checked. Behind a sign up gate sits a free eight unit curriculum, paraphrased
from the *Student Guide to Artificial Intelligence, AI U 2025, Elon Edition*, with
eleven interactive exercises that Claude marks component by component and eighteen
achievements kept in a SQLite file.

**The worked example** is the original prototype, described below.

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

The first run downloads the three notebook typefaces (Over the Rainbow for titles,
Sue Ellen Francisco for paragraphs, Caveat for margin asides) into `static/fonts/`. They are only a reference for the SVG Claude writes —
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

---

# TUTORial Academy

The free curriculum, the accounts behind it, and the badge wall.

## The pages

| Route | What it is | Gated |
|---|---|---|
| `/` | The mission, the four properties, the worked example, the research, the works cited | no |
| `/join` | Sign up or log in, with the same form switching between the two | no |
| `/curriculum` | Eight units and eleven exercises | yes |
| `/achievements` | Eighteen badges, when each was earned, and how every one is won | yes |
| `/logout` | Clears the session cookie and returns to the mission | no |

## The database

Everything a learner is lives in one SQLite file, `tutorial.db`, created on first import
and git-ignored.

```
users        id · email (unique, lowercased) · display_name
             password_hash · password_salt · rounds · created_at · last_seen_at
progress     user_id · item_key · score · finished_at        (unit1 … unit8, ex.<id>)
achievements user_id · code · earned_at
```

Passwords are never stored. `store.hash_password` runs PBKDF2 HMAC SHA256 at 240,000
rounds over a fresh 16 byte salt, and `verify_user` compares with
`secrets.compare_digest` and returns one identical message for a bad email and a bad
password, so the endpoint cannot be used to discover which addresses are registered.
The Flask cookie signing key is generated once into `.flask-secret` beside the database,
which is also git-ignored, so sessions survive a restart without a key in the source.

`store.py` has no Flask import, so the whole storage layer can be exercised on its own.

## The curriculum

`curriculum.py` holds eight units, each a paraphrase of one stretch of the guide with
the source pages named on the unit itself.

1. Where AI already lives · pages 6 to 7
2. What it means for your education · pages 7 and 24
3. The anatomy of a prompt · page 8
4. Read it like an editor · pages 9 to 10
5. AI across your work · pages 11 through 15
6. Integrity and attribution · pages 16 to 17
7. The ethics you inherit · page 18
8. A two part career plan · pages 19 through 23

The guide is published under a Creative Commons Attribution NonCommercial ShareAlike
licence, which is what makes a paraphrase inside a free course the intended use. The
full entry is on the works cited page.

## The exercises

Eleven exercises across seven interaction kinds, so the course never becomes one form
repeated. Nine of them are marked by a real Claude call. Two are marked in Python on
purpose, because a course about not over using AI should not call a model to compare two
lists.

| Kind | Interaction | Marked by |
|---|---|---|
| `fields` | Several labelled boxes, one verdict each | Claude |
| `single` | One box, one verdict per component found inside it | Claude |
| `checks` | A checklist over a sample, plus a written finding | Claude |
| `hunt` | Click the sentences in a passage that need verifying | Claude |
| `sorter` | Drop each situation into one of several buckets | Claude |
| `match` | Pair each job with the right family of tool | rules |
| `order` | Put the seven turns of the writing loop back in sequence | rules |

The flagship is **The Prompt Forge** in unit 3. The learner fills the six key elements
of an effective prompt from page 8 of the guide, one box at a time, and presses
**EXECUTE**. Each box goes to Claude with its own rubric and comes back green when the
component is genuinely present and specific enough to change what a model would produce,
red when it is not, and grey when an optional part was deliberately left out. Every
verdict carries one sentence naming the specific thing that would fix it.

**The Four Checks** in unit 4 is the second requested shape. A passage a model actually
wrote sits above the guide's four families of check, accuracy and sources, bias, logical
consistency, and emotional and manipulative language. The learner ticks every statement
genuinely true of the passage, writes the one question they would send back, and Claude
marks every tick and every deliberate non tick.

The rest are Prompt Rescue, Augment or Replace, Claim Hunt, Rebuild the Loop, Where Is
the Red Line, Write the Attribution, Ethics Triage, Your Two Part Plan and Pick the Right
Family.

**Without an API key the whole course still works.** Every grader falls back to a local
rubric, the result says which engine marked it, and the page says so too.

## Achievements

Eighteen badges in `curriculum.ACHIEVEMENTS`, each with a small declarative rule the
evaluator understands: making an account, finishing a named unit, clearing a named
exercise, reaching four units, clearing every exercise, and finishing the lot. After any
progress is recorded the server recomputes what the learner has earned and returns only
what is new, which the page raises as a toast in the corner.

## House style

Every string a learner can read, across all four templates and `curriculum.py`, is
written without a dash, a colon or a semicolon. `graders.py` puts the same requirement in
the marker's system prompt and runs `scrub` over everything a model writes, so a verdict
cannot break the rule either. The only exception is a web address inside a citation,
which is printed exactly as it resolves because an edited address is a broken one, and
the works cited page says so.

## The files

```
app.py           the lesson prototype, plus accounts, pages and the exercise endpoint
store.py         tutorial.db, hashing, progress and badges. No Flask import.
curriculum.py    eight units, eleven exercises, eighteen badges, and the answer keys
graders.py       one grader per interaction kind, Claude first with a local fallback
static/notebook.css   the shared sheet of ruled paper
templates/       index · join · curriculum · achievements
```
