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
video frames are typeset in the same hands as the interface. Offline, the renderer
substitutes and everything still renders.

The `videopython[ai]` extra is **required**, not optional — the scene loop calls
`TextToImage`, `ImageToVideo` and `TextToSpeech` from `videopython.ai`. It pulls in
torch and friends (several GB) and wants a GPU to be tolerable.

---

## The pipeline

Claude produces the `scenes` list directly; the sample `create_ai_video` code then runs
as given, with nothing in between.

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
  │  returns the scenes list, each item exactly two keys:       │
  │                                                             │
  │    image_prompt  a complete SVG document written out as     │
  │                  text, then a line starting                 │
  │                  "TRANSITION TO NEXT SCENE:" saying how it  │
  │                  should flow into the next scene's SVG      │
  │    narration     what the voice says over that frame        │
  └─────────────────────────────────────────────────────────────┘
        │
        ▼                                  create_ai_video()
   TextToImage().generate_image(scene["image_prompt"])
   ImageToVideo().generate_video(image=image)
   TextToSpeech().generate_audio(scene["narration"])
   video.add_audio(audio).save(f"{workdir}/scene_{i}.mp4")
        │
        ▼
   VideoEdit(segments=[SegmentConfig(source, start, end,
                operations=[Resize(width=1920, height=1080)],
                transition_in=None if i == 0
                              else TransitionSpec(type="dissolve", duration=1.0))])
        .run_to_file("step_i.mp4")
        │
        ▼
   video plays → stops at the checkpoint → student answers
        │
        ├── correct   → generate step i+1 (prefetched while they read the ✓)
        └── incorrect → build_miss_scenes(): a second video, on THEIR problem,
                        showing the wrong move and why it breaks → try again
```

The SVG lives inside `image_prompt` rather than being rasterised separately, so the
frames are legible diagrams and each one carries its own instructions for how it should
become the next. `SVG_STYLE_RULES` pins down the notebook substrate, the palette, the
three type voices, and that transition contract.

## Layout

```
app.py                 everything: config, Claude calls, the scene pipeline, jobs, routes
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
Claude's SVG only ever travels inside an `image_prompt` on the server; it never enters
the page DOM.

## What is deliberately unfinished

- **Sessions live in memory.** A restart empties them; the media on disk is orphaned.
  Fine for the prototype, wrong for anything else.
- **No auth, no rate limiting, one hardcoded key.** `app.run()` is the dev server.
- **Generation is slow.** Each scene runs a diffusion image model, an image-to-video
  model and a TTS model, and `create_ai_video` constructs all three per segment exactly
  as the sample does — so the models reload every time. Hoisting them to module scope is
  a one-line change if the reload cost bites.
- **Reasoning accuracy is validated on geometry only.** The pipeline is subject-agnostic
  by construction, but nothing checks Claude's arithmetic beyond the checkpoint
  disagreement itself.
