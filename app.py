"""
TUTORial — a notebook that quizzes you back.

A student photographs a problem they are stuck on. Claude reads it, invents a
*similar* problem, decomposes that similar problem into steps, and writes one
checkpoint question per step about the student's ORIGINAL problem. Each step is
then drawn as a short series of SVG scenes, rasterised, and stitched by
videopython into a video segment that plays up to the next checkpoint and stops.

The student cannot advance until they produce the step themselves. A wrong
answer generates a second video that diagnoses the mistake on their own problem
and sends them back to try again.

Everything lives in this one file: config, the Claude calls, the SVG renderer,
the videopython pipeline, the background job runner, and the Flask routes.

Run:
    pip install -r requirements.txt      # ffmpeg + ffprobe must be on PATH
    python app.py                        # http://127.0.0.1:5000
"""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — prototyping stage: the key is a literal, there is no cloud storage,
# and every session lives in memory until the process exits.
# ─────────────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = "sk-ant-REPLACE-WITH-YOUR-KEY"

MODEL = "claude-opus-5"

CHECKPOINT_COUNT = 6          # the video stops this many times
SCENES_PER_STEP = (2, 4)      # SVG scenes Claude draws per step
FRAME_W, FRAME_H = 1280, 720  # rasterised SVG size, and the video's resolution
FPS = 24
MIN_SCENE_SECONDS = 3.0
MAX_SCENE_SECONDS = 11.0
CHARS_PER_SECOND = 14.0       # narration pacing when no TTS voice is available

BASE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = BASE_DIR / "media"
UPLOAD_DIR = BASE_DIR / "media" / "_uploads"
FONT_DIR = BASE_DIR / "static" / "fonts"

# videopython's curated crossfade catalog. Claude picks from exactly this list
# when it says how one SVG scene should flow into the next.
TRANSITION_TYPES = [
    "fade", "dissolve", "wipeleft", "wiperight",
    "wipeup", "wipedown", "slideleft", "slideright",
]

# The notebook palette, handed to Claude so the generated frames sit on the same
# page as the interface around them.
PALETTE = {
    "paper": "#fffdf7", "ink": "#3d4663", "rule": "#d9edfa", "margin": "#f7c3d7",
    "lilac": "#c9b6e4", "lilac_dk": "#a98fd4", "blush": "#f7c8dc", "blush_dk": "#e79ec0",
    "sky": "#a8d8f0", "sky_dk": "#7fc4e8", "mint": "#86ddc0", "mint_dk": "#5cc4a0",
    "yellow": "#ffe08a", "yellow_dk": "#f0c85e", "lime": "#c8e06a", "muted": "#8b93ad",
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024


# ─────────────────────────────────────────────────────────────────────────────
# FONTS — the generated frames should be typeset in the notebook's own hands.
# Downloaded once into static/fonts and registered with fontconfig so cairosvg
# can find them. Offline, cairo falls back and the frames still render.
# ─────────────────────────────────────────────────────────────────────────────

GOOGLE_FONTS = {
    "LilitaOne.ttf": "https://fonts.gstatic.com/s/lilitaone/v17/i7dPIFZ9Zz-WBtRtedDbUEY.ttf",
    "PatrickHand.ttf": "https://fonts.gstatic.com/s/patrickhand/v25/LDI1apSQOAYtSuYWp8ZhfYeMWQ.ttf",
    "Caveat.ttf": "https://fonts.gstatic.com/s/caveat/v23/WnznHAc5bAfYB2QRah7pcpNvOx-pjcB9SII.ttf",
}


def ensure_fonts() -> None:
    """Fetch the three notebook faces and make fontconfig aware of them."""
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    missing = {n: u for n, u in GOOGLE_FONTS.items() if not (FONT_DIR / n).exists()}
    if missing:
        try:
            import requests
            for name, url in missing.items():
                resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                (FONT_DIR / name).write_bytes(resp.content)
        except Exception as exc:  # offline is survivable — cairo will substitute
            print(f"[fonts] could not fetch notebook fonts ({exc}); using fallbacks")

    # fontconfig reads ~/.fonts; symlinking is cheaper than a config file.
    try:
        home_fonts = Path.home() / ".fonts"
        home_fonts.mkdir(parents=True, exist_ok=True)
        for name in GOOGLE_FONTS:
            src, dst = FONT_DIR / name, home_fonts / name
            if src.exists() and not dst.exists():
                shutil.copyfile(src, dst)
        subprocess.run(["fc-cache", "-f"], capture_output=True, timeout=60)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# CLAUDE
# ─────────────────────────────────────────────────────────────────────────────

_client_lock = threading.Lock()
_client = None


def claude():
    global _client
    with _client_lock:
        if _client is None:
            import anthropic
            if "REPLACE-WITH-YOUR-KEY" in ANTHROPIC_API_KEY:
                raise RuntimeError(
                    "Set ANTHROPIC_API_KEY at the top of app.py to your Claude API key."
                )
            _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=600.0)
    return _client


def ask_json(system: str, content: list[dict], schema: dict, max_tokens: int = 32000) -> dict:
    """One structured Claude call. Streams so long SVG payloads can't time out."""
    with claude().messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        thinking={"type": "adaptive"},
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": content}],
    ) as stream:
        message = stream.get_final_message()

    if getattr(message, "stop_reason", None) == "refusal":
        raise RuntimeError("Claude declined to answer this request.")

    text = ""
    for block in message.content:
        if getattr(block, "type", None) == "text":
            text = block.text
    if not text.strip():
        raise RuntimeError("Claude returned an empty response.")
    return json.loads(strip_fences(text))


def strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text


def image_block(path: Path) -> dict:
    media_type = mimetypes.guess_type(path.name)[0] or "image/png"
    if media_type not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        media_type = "image/png"
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(path.read_bytes()).decode(),
        },
    }


# ── Prompts ──────────────────────────────────────────────────────────────────

SVG_STYLE_RULES = f"""
Every SVG you draw is a frame of a hand-kept school notebook, and the frames run
together as one continuous video. Follow these rules exactly.

CANVAS
- Root tag: <svg xmlns="http://www.w3.org/2000/svg" width="{FRAME_W}" height="{FRAME_H}"
  viewBox="0 0 {FRAME_W} {FRAME_H}"> ... </svg>
- Nothing outside the root tag. No markdown, no comments, no <script>, no <foreignObject>,
  no external images, no href to anything off-canvas. Self-contained vector shapes and text only.

SUBSTRATE (identical on every frame, so the page never jumps)
- Full-bleed cream sheet: <rect> filling the canvas in {PALETTE['paper']}.
- Ruled blue lines every 34px in {PALETTE['rule']}, starting at y=78.
- A double pink margin rule down the left at x=96 and x=102 in {PALETTE['margin']}.
- Three punched holes down the left gutter at x=48, y=120 / 360 / 600, r=13, fill {PALETTE['sky']} at low opacity.

PALETTE — use these and nothing else
- ink {PALETTE['ink']} · lilac {PALETTE['lilac']} / {PALETTE['lilac_dk']} · blush {PALETTE['blush']} / {PALETTE['blush_dk']}
- sky {PALETTE['sky']} / {PALETTE['sky_dk']} · mint {PALETTE['mint']} / {PALETTE['mint_dk']}
- highlighter {PALETTE['yellow']} / {PALETTE['yellow_dk']} · lime {PALETTE['lime']} · muted {PALETTE['muted']}

TYPE — three voices, never mixed up
- font-family="Lilita One" for headlines and stamped labels. ALL CAPS, chunky.
- font-family="Patrick Hand" for body text and worked math.
- font-family="Caveat" for margin asides and annotations.
- Always give a fallback: font-family="Lilita One, sans-serif".
- Body text is never smaller than 26px. Headlines are 44–64px.

PHYSICAL VOCABULARY — this is what makes it read as paper, not as a slide
- Cards are placed on the page: a hard offset shadow (a duplicate <rect> nudged 3px right
  and 4px down in a tint), never a blur filter.
- Everything sits at a sub-degree angle: transform="rotate(-0.6 640 360)" and similar.
- Highlighter swipes behind key phrases: a <rect> in {PALETTE['yellow']} behind the text, not a solid fill over it.
- Sticky notes get a torn corner: a <path> polygon with the top-right corner cut off.
- Washi tape holding a card down: a translucent <rect> rotated a few degrees across its edge.
- Labels are stamped in a flagged tag box: a filled rect with a triangle point on its right edge.

LAYOUT AND LEGIBILITY
- Keep all content inside x ∈ [132, 1180] and y ∈ [70, 660]. Nothing touches an edge.
- One idea per frame. Large, few elements, generous whitespace. This is a video, not a poster.
- Every frame carries the narration for that frame as a caption strip along the bottom
  (y ≈ 600–660) in Patrick Hand at 28px on a cream card, so the video is followable with the
  sound off. Wrap it yourself across <tspan x="..." dy="34"> lines — SVG does not wrap text.
- Math is drawn, not described: draw the triangle, label the sides, show the proportion as
  laid-out text with the numbers in place.

CONTINUITY BETWEEN FRAMES — this is what makes the video flow
- Frames within one step share a layout. Keep the diagram in the same place at the same size
  from frame to frame and change only what the step is adding: a new label, a highlighted side,
  a line of work appearing below the last. The crossfade then reads as annotation appearing on
  a page, not as a slide change.
- Because consecutive frames are near-identical apart from the new mark, pick "dissolve" for
  those. Reserve a wipe or slide for a genuine change of subject (e.g. leaving the diagram to
  do algebra). Never use a transition on the first scene of a segment.
"""

SCENE_SCHEMA = {
    "type": "object",
    "properties": {
        "theme_note": {
            "type": "string",
            "description": "One sentence: the shared visual layout these frames hold across the cut.",
        },
        "scenes": {
            "type": "array",
            "minItems": SCENES_PER_STEP[0],
            "maxItems": SCENES_PER_STEP[1],
            "items": {
                "type": "object",
                "properties": {
                    "svg": {"type": "string", "description": "A complete, self-contained SVG document."},
                    "narration": {
                        "type": "string",
                        "description": "What the voice says over this frame. 1-3 sentences, spoken register.",
                    },
                    "transition_in": {
                        "type": ["object", "null"],
                        "description": "How this frame arrives from the previous one. null on the first scene.",
                        "properties": {
                            "type": {"type": "string", "enum": TRANSITION_TYPES},
                            "duration": {"type": "number", "minimum": 0.3, "maximum": 2.0},
                        },
                        "required": ["type", "duration"],
                        "additionalProperties": False,
                    },
                },
                "required": ["svg", "narration", "transition_in"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["theme_note", "scenes"],
    "additionalProperties": False,
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "original": {
            "type": "object",
            "properties": {
                "statement": {"type": "string", "description": "The uploaded problem, restated in full."},
                "subject": {"type": "string", "description": "e.g. 'geometry — similar triangles'"},
                "givens": {"type": "string", "description": "The given quantities and relationships, compactly."},
                "final_answer": {"type": "string", "description": "The answer to the ORIGINAL problem, e.g. 'BD = 11.25'."},
            },
            "required": ["statement", "subject", "givens", "final_answer"],
            "additionalProperties": False,
        },
        "similar": {
            "type": "object",
            "properties": {
                "statement": {"type": "string", "description": "A NEW problem with the same structure and different numbers."},
                "givens": {"type": "string"},
                "final_answer": {"type": "string", "description": "The answer to the SIMILAR problem."},
            },
            "required": ["statement", "givens", "final_answer"],
            "additionalProperties": False,
        },
        "steps": {
            "type": "array",
            "minItems": CHECKPOINT_COUNT,
            "maxItems": CHECKPOINT_COUNT,
            "description": "The solution to the SIMILAR problem, one honest step at a time.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Imperative, <= 6 words. e.g. 'Set up the proportion'."},
                    "short_title": {"type": "string", "description": "1-2 words for the number line. e.g. 'proportion'."},
                    "teach": {"type": "string", "description": "What this step does and why, in a tutor's voice. 2-4 sentences."},
                    "work": {"type": "string", "description": "The actual work for this step in the similar problem, e.g. '4/9 = y/12'."},
                },
                "required": ["title", "short_title", "teach", "work"],
                "additionalProperties": False,
            },
        },
        "checkpoints": {
            "type": "array",
            "minItems": CHECKPOINT_COUNT,
            "maxItems": CHECKPOINT_COUNT,
            "description": "One question per step, each about the student's ORIGINAL problem.",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["mcq", "text"]},
                    "title": {"type": "string", "description": "Imperative, <= 6 words."},
                    "prompt": {"type": "string", "description": "The question, about the ORIGINAL problem."},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Exactly 4 options for mcq; an empty array for text.",
                    },
                    "correct_index": {"type": "integer", "description": "0-based index of the right option; -1 for text."},
                    "target": {"type": "string", "description": "The expected answer for text; empty string for mcq."},
                    "placeholder": {"type": "string", "description": "Input placeholder for text, e.g. '11.25'."},
                    "hint": {"type": "string", "description": "A margin aside nudging the method, never the answer."},
                    "affirm": {"type": "string", "description": "One sentence confirming a correct answer."},
                    "explanation": {"type": "string", "description": "Why the right answer is right, for a wrong attempt."},
                },
                "required": ["type", "title", "prompt", "options", "correct_index",
                             "target", "placeholder", "hint", "affirm", "explanation"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["original", "similar", "steps", "checkpoints"],
    "additionalProperties": False,
}

GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "correct": {"type": "boolean"},
        "feedback": {"type": "string", "description": "One or two sentences, addressed to the student."},
        "misconception": {
            "type": "string",
            "description": "If wrong: the specific wrong move that produces this answer. Empty if correct.",
        },
    },
    "required": ["correct", "feedback", "misconception"],
    "additionalProperties": False,
}


PLAN_SYSTEM = f"""You are the tutor behind TUTORial. A student has photographed a problem
they are stuck on. You do NOT solve their problem for them — that would hand them a finished
solution to read, which feels like learning and is not.

Instead you do two things at once.

1. You invent a SIMILAR problem: same underlying structure, same solution method, different
   numbers and different surface details. You solve THAT one out loud, in exactly
   {CHECKPOINT_COUNT} honest steps. No step may skip work or bundle two ideas together.

2. For each of those {CHECKPOINT_COUNT} steps you write ONE checkpoint question about the
   student's ORIGINAL problem — the transfer move. Checkpoint i asks the student to carry out
   step i on their own figure and their own numbers. Checkpoint {CHECKPOINT_COUNT} asks for the
   original problem's final answer.

Rules for the checkpoints:
- A checkpoint is answerable from the step just taught plus the original problem. Never ahead of it.
- Use "mcq" where the answer is a choice, a relationship, or a piece of notation. Use "text"
  where the answer is a number the student should actually compute. Aim for roughly two thirds
  mcq and the rest text, with the final checkpoint a "text" answer.
- For "mcq": exactly 4 options, one correct, and the three wrong ones must be real mistakes a
  student makes here — a flipped ratio, the wrong pair of corresponding sides, a stopped-too-early
  answer. Never filler.
- For "text": `target` is the expected value as a plain string ("11.25"), `options` is [] and
  `correct_index` is -1.
- Read every number off the student's image carefully. If the image is unreadable or is not a
  problem at all, still return the schema, using the original fields to say so plainly.

Write like a good tutor talking, not like a textbook."""


def build_plan(problem_image: Path) -> dict:
    return ask_json(
        system=PLAN_SYSTEM,
        content=[
            image_block(problem_image),
            {
                "type": "text",
                "text": (
                    "This is the problem my student is stuck on. Read it, work out what is "
                    "actually being asked, then build the lesson: a similar problem solved in "
                    f"{CHECKPOINT_COUNT} steps, and {CHECKPOINT_COUNT} checkpoint questions about "
                    "the problem in this image."
                ),
            },
        ],
        schema=PLAN_SCHEMA,
        max_tokens=32000,
    )


def build_step_scenes(plan: dict, index: int) -> dict:
    step = plan["steps"][index]
    checkpoint = plan["checkpoints"][index]
    prior = "\n".join(
        f"  step {i + 1}. {s['title']} — {s['work']}" for i, s in enumerate(plan["steps"][:index])
    ) or "  (none — this is the opening step)"

    system = (
        "You draw the frames of a TUTORial lesson video. You are given one step of a worked "
        "solution and you return a short run of SVG frames that teach exactly that step and then "
        "hand off to the student.\n\n" + SVG_STYLE_RULES
    )

    user = f"""THE SIMILAR PROBLEM being worked on screen
{plan['similar']['statement']}
Given: {plan['similar']['givens']}

STEPS ALREADY COVERED
{prior}

THIS SEGMENT TEACHES — step {index + 1} of {CHECKPOINT_COUNT}
Title: {step['title']}
Teach: {step['teach']}
Work:  {step['work']}

WHERE IT STOPS
The moment this step is done the video must stop and put the student to work on their OWN
problem. The question waiting for them is:
  "{checkpoint['prompt']}"
Do not answer that question, hint at its answer, or show their problem's numbers. The last frame
is the handoff: a stamped "YOUR TURN" card that tells them to take this same move over to their
own problem. It must not restate the question — the interface asks it.

Draw {SCENES_PER_STEP[0]}–{SCENES_PER_STEP[1]} frames: the step being made on the similar
problem, then the handoff. Give each frame its narration and its crossfade in."""

    return ask_json(system, [{"type": "text", "text": user}], SCENE_SCHEMA, max_tokens=32000)


def build_miss_scenes(plan: dict, index: int, given: str, misconception: str,
                      problem_image: Path) -> dict:
    checkpoint = plan["checkpoints"][index]

    system = (
        "You draw the frames of a TUTORial correction video. A student answered a checkpoint "
        "wrongly. You return a short run of SVG frames that walk back through THEIR OWN problem "
        "to show where that answer comes from and why it does not hold — then send them back to "
        "try again.\n\n" + SVG_STYLE_RULES
    )

    user = f"""THE STUDENT'S ORIGINAL PROBLEM (attached as an image, and restated here)
{plan['original']['statement']}
Given: {plan['original']['givens']}

THE CHECKPOINT THEY MISSED — step {index + 1}, "{checkpoint['title']}"
Question: {checkpoint['prompt']}
They answered: {given}
The wrong move that produces that answer: {misconception or 'not identified — diagnose it yourself'}
Why the right answer is right: {checkpoint['explanation']}

Draw {SCENES_PER_STEP[0]}–{SCENES_PER_STEP[1]} frames, working on THEIR problem, not the similar one:
1. Show their figure with the move they made drawn on it — make the wrong step visible, not just named.
2. Show the thing that breaks. Follow their move to where it contradicts something already on the page.
3. Point at the one idea that fixes it, WITHOUT completing the step for them.
4. Close on a stamped "TRY IT AGAIN" card.

Be kind and specific. Never say "you should know this", never call the mistake careless, and never
give away the answer — they are about to attempt it again."""

    return ask_json(
        system,
        [image_block(problem_image), {"type": "text", "text": user}],
        SCENE_SCHEMA,
        max_tokens=32000,
    )


def grade_free_text(plan: dict, index: int, given: str) -> dict:
    checkpoint = plan["checkpoints"][index]
    system = (
        "You mark one checkpoint answer for a student, generously but honestly. Accept any form "
        "of the right value — fractions, decimals, an equation with the value in it, extra units. "
        "Reject a right-looking answer that is arithmetically wrong. If it is wrong, name the "
        "specific wrong move that produces exactly that answer, so a correction video can show it."
    )
    user = f"""THE PROBLEM
{plan['original']['statement']}
Given: {plan['original']['givens']}

THE QUESTION
{checkpoint['prompt']}

EXPECTED ANSWER: {checkpoint['target']}
THE STUDENT WROTE: {given}"""
    return ask_json(system, [{"type": "text", "text": user}], GRADE_SCHEMA, max_tokens=8000)


# ─────────────────────────────────────────────────────────────────────────────
# SVG → FRAMES → VIDEO
# ─────────────────────────────────────────────────────────────────────────────

_STRIP_TAGS = re.compile(r"<\s*(script|foreignObject|iframe|use)\b.*?<\s*/\s*\1\s*>", re.I | re.S)
_STRIP_SELF = re.compile(r"<\s*(script|foreignObject|iframe|use)\b[^>]*/\s*>", re.I)
_REMOTE_HREF = re.compile(r"""\s(?:xlink:)?href\s*=\s*(['"])(?!#)[^'"]*\1""", re.I)
_ON_ATTR = re.compile(r"""\son[a-z]+\s*=\s*(['"]).*?\1""", re.I | re.S)


def sanitize_svg(svg: str) -> str:
    """Model output is rasterised server-side, never injected into the page — but
    strip scripting and off-canvas references anyway so nothing can reach out."""
    svg = strip_fences(svg)
    start = svg.find("<svg")
    if start > 0:
        svg = svg[start:]
    for pattern in (_STRIP_TAGS, _STRIP_SELF, _REMOTE_HREF, _ON_ATTR):
        svg = pattern.sub("", svg)
    if "<svg" not in svg:
        raise ValueError("model returned no SVG document")
    return svg


def rasterize(svg: str):
    """SVG text → an RGB numpy frame at the video's resolution."""
    import cairosvg
    import numpy as np
    from PIL import Image

    png = cairosvg.svg2png(
        bytestring=sanitize_svg(svg).encode("utf-8"),
        output_width=FRAME_W,
        output_height=FRAME_H,
        background_color=PALETTE["paper"],
    )
    image = Image.open(io.BytesIO(png)).convert("RGB")
    if image.size != (FRAME_W, FRAME_H):
        image = image.resize((FRAME_W, FRAME_H), Image.LANCZOS)
    return np.array(image)


def scene_seconds(narration: str) -> float:
    """How long a frame should hold when there is no spoken track to time it."""
    est = len(narration or "") / CHARS_PER_SECOND + 1.4
    return round(max(MIN_SCENE_SECONDS, min(MAX_SCENE_SECONDS, est)), 2)


_warned: set[str] = set()


def warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        print(f"[{key}] {message}")


_tts: Any = "unset"


def _speech():
    """videopython's TextToSpeech, if the heavy `videopython[ai]` extra is installed."""
    global _tts
    if _tts == "unset":
        try:
            from videopython.ai import TextToSpeech
            _tts = TextToSpeech()
        except Exception as exc:
            warn_once("tts", f"no spoken narration ({exc}); frames carry their captions instead")
            _tts = None
    return _tts


def render_scenes(scenes: list[dict], workdir: Path, out_path: Path,
                  on_progress=None) -> dict:
    """Claude's SVG scenes → one stitched MP4, exactly the sample pipeline:
    a clip per scene, then one VideoEdit with a crossfade into each follow-on.
    """
    from videopython.base.video import Video, VideoMetadata
    from videopython.editing import Resize, SegmentConfig, TransitionSpec, VideoEdit

    workdir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tts = _speech()

    clips: list[tuple[Path, float, dict | None, str]] = []
    for i, scene in enumerate(scenes):
        if on_progress:
            on_progress(i, len(scenes))

        narration = (scene.get("narration") or "").strip()
        frame = rasterize(scene["svg"])
        seconds = scene_seconds(narration)

        audio = None
        if tts is not None and narration:
            try:
                audio = tts.generate_audio(narration)
                seconds = max(MIN_SCENE_SECONDS, round(audio.total_seconds + 0.6, 2))
            except Exception as exc:
                warn_once("tts", f"no spoken narration ({exc}); frames carry their captions instead")

        clip = Video.from_image(frame, fps=FPS, length_seconds=seconds)
        if audio is not None:
            clip = clip.add_audio(audio)

        path = workdir / f"scene_{i}.mp4"
        clip.save(str(path))
        clips.append((path, seconds, scene.get("transition_in"), narration))

    # One segment per scene. Resize standardizes the frame size; the crossfade on
    # each follow-on scene is the one Claude chose to connect those two frames.
    segments = []
    for i, (path, _, transition, _) in enumerate(clips):
        meta = VideoMetadata.from_path(path)
        spec = None
        if i > 0:
            kind = (transition or {}).get("type", "dissolve")
            length = float((transition or {}).get("duration", 0.8) or 0.8)
            if kind not in TRANSITION_TYPES:
                kind = "dissolve"
            # A crossfade cannot be longer than either clip it overlaps.
            prev = VideoMetadata.from_path(clips[i - 1][0]).total_seconds
            length = max(0.3, min(length, prev - 0.3, meta.total_seconds - 0.3))
            spec = TransitionSpec(type=kind, duration=round(length, 2))
        segments.append(SegmentConfig(
            source=str(path),
            start=0,
            end=meta.total_seconds,
            operations=[Resize(width=FRAME_W, height=FRAME_H)],
            transition_in=spec,
        ))

    VideoEdit(segments=segments).run_to_file(str(out_path))

    # Where each narration line lands on the assembled timeline, so the page can
    # follow along in text as the video plays.
    transcript, cursor = [], 0.0
    for i, segment in enumerate(segments):
        overlap = segment.transition_in.duration if segment.transition_in else 0.0
        cursor = max(0.0, cursor - overlap)
        length = segment.end - segment.start
        transcript.append({
            "start": round(cursor, 2),
            "end": round(cursor + length, 2),
            "text": clips[i][3],
        })
        cursor += length

    for path, *_ in clips:
        path.unlink(missing_ok=True)

    duration = VideoMetadata.from_path(out_path).total_seconds
    return {"duration": round(duration, 2), "transcript": transcript}


# ─────────────────────────────────────────────────────────────────────────────
# SESSIONS AND JOBS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Lesson:
    sid: str
    image: Path
    image_url: str
    file_name: str
    plan: dict | None = None
    segments: dict[int, dict] = field(default_factory=dict)   # index -> video payload
    misses: dict[int, dict] = field(default_factory=dict)     # index -> latest correction video
    status: list[int] = field(default_factory=lambda: [0] * CHECKPOINT_COUNT)
    attempts: dict[int, int] = field(default_factory=dict)
    first_try: int = 0


@dataclass
class Job:
    jid: str
    labels: list[str]
    stage: int = 0
    detail: str = ""
    state: str = "running"          # running | done | error
    result: dict = field(default_factory=dict)
    error: str = ""

    def advance(self, stage: int, detail: str = "") -> None:
        self.stage, self.detail = stage, detail

    def payload(self) -> dict:
        return {
            "job_id": self.jid, "state": self.state, "stage": self.stage,
            "labels": self.labels, "detail": self.detail,
            "result": self.result, "error": self.error,
        }


LESSONS: dict[str, Lesson] = {}
JOBS: dict[str, Job] = {}
_store_lock = threading.Lock()


def run_job(labels: list[str], work) -> Job:
    """Start `work(job)` on a background thread and hand back its handle."""
    job = Job(jid=uuid.uuid4().hex, labels=labels)
    with _store_lock:
        JOBS[job.jid] = job

    def target():
        try:
            job.result = work(job) or {}
            job.state = "done"
            job.stage = len(job.labels)
        except Exception as exc:
            traceback.print_exc()
            job.state, job.error = "error", f"{type(exc).__name__}: {exc}"

    threading.Thread(target=target, daemon=True).start()
    return job


def public_checkpoint(checkpoint: dict, index: int) -> dict:
    """What the browser is allowed to see. Answers stay on the server."""
    return {
        "index": index,
        "type": checkpoint["type"],
        "title": checkpoint["title"],
        "prompt": checkpoint["prompt"],
        "options": checkpoint.get("options") or [],
        "placeholder": checkpoint.get("placeholder") or "",
        "hint": checkpoint.get("hint") or "",
    }


def public_plan(lesson: Lesson) -> dict:
    plan = lesson.plan or {}
    return {
        "subject": plan.get("original", {}).get("subject", ""),
        "similar": plan.get("similar", {}).get("statement", ""),
        "final_answer": plan.get("original", {}).get("final_answer", ""),
        "roadmap": [
            {"title": s["title"], "short": s["short_title"]} for s in plan.get("steps", [])
        ],
        "checkpoints": [
            public_checkpoint(c, i) for i, c in enumerate(plan.get("checkpoints", []))
        ],
        "count": len(plan.get("checkpoints", [])),
    }


def lesson_state(lesson: Lesson) -> dict:
    return {
        "session_id": lesson.sid,
        "image_url": lesson.image_url,
        "file_name": lesson.file_name,
        "status": lesson.status,
        "first_try": lesson.first_try,
        "attempts": sum(lesson.attempts.values()),
        "plan": public_plan(lesson) if lesson.plan else None,
        "segments": {str(k): v for k, v in lesson.segments.items()},
    }


SEGMENT_LABELS = ["Writing the next step", "Drawing an SVG frame per beat", "Stitching the frames into video"]


def generate_segment(job: Job, lesson: Lesson, index: int) -> dict:
    """Claude draws step `index` as SVG scenes; videopython stitches them."""
    job.advance(0, f"Step {index + 1} of {len(lesson.plan['steps'])}")
    scenes = build_step_scenes(lesson.plan, index)

    job.advance(1, "Frame 1")
    workdir = MEDIA_DIR / lesson.sid / f"step_{index}_work"
    out = MEDIA_DIR / lesson.sid / f"step_{index}.mp4"

    def progress(i: int, total: int) -> None:
        job.advance(1, f"Frame {i + 1} of {total}")

    rendered = render_scenes(scenes["scenes"], workdir, out, progress)
    job.advance(2, "Crossfading the frames together")

    payload = {
        "index": index,
        "video_url": f"/media/{lesson.sid}/step_{index}.mp4",
        "duration": rendered["duration"],
        "transcript": rendered["transcript"],
        "theme_note": scenes.get("theme_note", ""),
        "checkpoint": public_checkpoint(lesson.plan["checkpoints"][index], index),
    }
    lesson.segments[index] = payload
    shutil.rmtree(workdir, ignore_errors=True)
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return render_template(
        "index.html",
        checkpoint_count=CHECKPOINT_COUNT,
        key_missing="REPLACE-WITH-YOUR-KEY" in ANTHROPIC_API_KEY,
    )


@app.post("/api/upload")
def api_upload():
    sid = uuid.uuid4().hex
    folder = UPLOAD_DIR / sid
    folder.mkdir(parents=True, exist_ok=True)

    upload = request.files.get("image")
    if upload and upload.filename:
        name = secure_filename(upload.filename) or "problem.png"
        path = folder / name
        upload.save(path)
    else:
        name = "similar-triangles.png"
        path = folder / name
        shutil.copyfile(BASE_DIR / "static" / "sample-problem.png", path)

    lesson = Lesson(
        sid=sid,
        image=path,
        image_url=f"/uploads/{sid}/{name}",
        file_name=name,
    )
    with _store_lock:
        LESSONS[sid] = lesson
    return jsonify(lesson_state(lesson))


@app.post("/api/start")
def api_start():
    lesson = require_lesson(request.json.get("session_id"))

    labels = [
        "Reading your problem",
        "Writing a similar problem and its steps",
        "Drawing an SVG frame per beat",
        "Stitching the frames into video",
    ]

    def work(job: Job) -> dict:
        job.advance(0, "Looking at your image")
        lesson.plan = build_plan(lesson.image)
        lesson.status = [0] * len(lesson.plan["checkpoints"])

        job.advance(1, lesson.plan["original"]["subject"])
        time.sleep(0.2)  # let the checklist tick over visibly

        job.advance(2, "Frame 1")
        workdir = MEDIA_DIR / lesson.sid / "step_0_work"
        out = MEDIA_DIR / lesson.sid / "step_0.mp4"
        scenes = build_step_scenes(lesson.plan, 0)

        def progress(i: int, total: int) -> None:
            job.advance(2, f"Frame {i + 1} of {total}")

        rendered = render_scenes(scenes["scenes"], workdir, out, progress)
        job.advance(3, "Crossfading the frames together")
        shutil.rmtree(workdir, ignore_errors=True)

        lesson.segments[0] = {
            "index": 0,
            "video_url": f"/media/{lesson.sid}/step_0.mp4",
            "duration": rendered["duration"],
            "transcript": rendered["transcript"],
            "theme_note": scenes.get("theme_note", ""),
            "checkpoint": public_checkpoint(lesson.plan["checkpoints"][0], 0),
        }
        return {"state": lesson_state(lesson), "segment": lesson.segments[0]}

    return jsonify(run_job(labels, work).payload())


@app.post("/api/segment")
def api_segment():
    body = request.json or {}
    lesson = require_lesson(body.get("session_id"))
    index = int(body.get("index", 0))

    if not lesson.plan:
        abort(409, "lesson has no plan yet")
    if not 0 <= index < len(lesson.plan["steps"]):
        abort(404, "no such step")
    if index in lesson.segments:
        return jsonify({"state": "done", "stage": 3, "labels": SEGMENT_LABELS,
                        "result": {"segment": lesson.segments[index]}, "job_id": "", "detail": "", "error": ""})

    def work(job: Job) -> dict:
        return {"segment": generate_segment(job, lesson, index)}

    return jsonify(run_job(SEGMENT_LABELS, work).payload())


@app.post("/api/answer")
def api_answer():
    body = request.json or {}
    lesson = require_lesson(body.get("session_id"))
    index = int(body.get("index", 0))
    if not lesson.plan or not 0 <= index < len(lesson.plan["checkpoints"]):
        abort(404, "no such checkpoint")

    checkpoint = lesson.plan["checkpoints"][index]
    given = body.get("answer")

    if checkpoint["type"] == "mcq":
        try:
            chosen = int(given)
        except (TypeError, ValueError):
            abort(400, "pick an option")
        correct = chosen == int(checkpoint["correct_index"])
        given_text = (checkpoint.get("options") or ["", "", "", ""])[chosen] if 0 <= chosen < 4 else str(given)
        feedback = checkpoint["affirm"] if correct else checkpoint["explanation"]
        misconception = "" if correct else f"they chose: {given_text}"
    else:
        given_text = str(given or "").strip()
        if not given_text:
            abort(400, "write something in")
        correct = numbers_match(given_text, checkpoint.get("target", ""))
        if correct:
            feedback, misconception = checkpoint["affirm"], ""
        else:
            # Not a numeric match — it may still be right in another form.
            verdict = grade_free_text(lesson.plan, index, given_text)
            correct = bool(verdict["correct"])
            feedback = verdict["feedback"] or (
                checkpoint["affirm"] if correct else checkpoint["explanation"]
            )
            misconception = "" if correct else verdict.get("misconception", "")

    lesson.attempts[index] = lesson.attempts.get(index, 0) + 1
    if correct:
        if lesson.attempts[index] == 1:
            lesson.first_try += 1
        lesson.status[index] = 1

    response = {
        "correct": correct,
        "feedback": feedback,
        "state": lesson_state(lesson),
        "is_last": index == len(lesson.plan["checkpoints"]) - 1,
    }

    if not correct:
        # A wrong answer earns its own video: their problem, their wrong move.
        def work(job: Job) -> dict:
            job.advance(0, "Working out where that came from")
            scenes = build_miss_scenes(lesson.plan, index, given_text, misconception, lesson.image)

            stamp = lesson.attempts[index]
            workdir = MEDIA_DIR / lesson.sid / f"miss_{index}_{stamp}_work"
            out = MEDIA_DIR / lesson.sid / f"miss_{index}_{stamp}.mp4"

            def progress(i: int, total: int) -> None:
                job.advance(1, f"Frame {i + 1} of {total}")

            rendered = render_scenes(scenes["scenes"], workdir, out, progress)
            job.advance(2, "Crossfading the frames together")
            shutil.rmtree(workdir, ignore_errors=True)

            payload = {
                "index": index,
                "video_url": f"/media/{lesson.sid}/miss_{index}_{stamp}.mp4",
                "duration": rendered["duration"],
                "transcript": rendered["transcript"],
            }
            lesson.misses[index] = payload
            return {"correction": payload}

        labels = ["Working out where that came from", "Drawing it on your figure", "Stitching the frames into video"]
        response["correction_job"] = run_job(labels, work).payload()

    return jsonify(response)


@app.get("/api/job/<job_id>")
def api_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        abort(404, "no such job")
    return jsonify(job.payload())


@app.get("/api/state/<session_id>")
def api_state(session_id: str):
    return jsonify(lesson_state(require_lesson(session_id)))


@app.post("/api/reset")
def api_reset():
    """Same problem, same lesson, scores cleared — the REPLAY button."""
    lesson = require_lesson((request.json or {}).get("session_id"))
    lesson.status = [0] * len(lesson.plan["checkpoints"]) if lesson.plan else [0] * CHECKPOINT_COUNT
    lesson.attempts, lesson.first_try, lesson.misses = {}, 0, {}
    return jsonify(lesson_state(lesson))


@app.get("/media/<session_id>/<path:filename>")
def media(session_id: str, filename: str):
    return send_from_directory(MEDIA_DIR / secure_filename(session_id), filename)


@app.get("/uploads/<session_id>/<path:filename>")
def uploads(session_id: str, filename: str):
    return send_from_directory(UPLOAD_DIR / secure_filename(session_id), filename)


def require_lesson(session_id: Any) -> Lesson:
    lesson = LESSONS.get(str(session_id or ""))
    if not lesson:
        abort(404, "that lesson is not in memory — the server restarts empty")
    return lesson


def numbers_match(given: str, target: str) -> bool:
    """Fast local check for the common case: the same number, any notation."""
    a, b = parse_number(given), parse_number(target)
    if a is None or b is None:
        return given.strip().lower() == target.strip().lower() and bool(given.strip())
    return abs(a - b) < 1e-6


_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def parse_number(text: str) -> float | None:
    """Read a number out of '6.25', '25/4', 'x = 6.25', '6.25 cm', '6 1/4'."""
    if text is None:
        return None
    cleaned = str(text).strip().replace(",", "").replace("=", " ")
    cleaned = re.sub(r"[a-zA-Z°]+", " ", cleaned).strip()

    mixed = re.fullmatch(r"\s*(-?\d+)\s+(\d+)\s*/\s*(\d+)\s*", cleaned)
    if mixed:
        whole, num, den = (float(g) for g in mixed.groups())
        if den:
            return whole + (num / den if whole >= 0 else -num / den)

    fraction = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)\s*", cleaned)
    if fraction:
        num, den = float(fraction.group(1)), float(fraction.group(2))
        return num / den if den else None

    found = _NUM.findall(cleaned)
    if len(found) == 1:
        return float(found[0])
    return None


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(409)
@app.errorhandler(413)
def api_error(exc):
    return jsonify({"error": getattr(exc, "description", str(exc))}), exc.code


if __name__ == "__main__":
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ensure_fonts()
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("[warn] ffmpeg/ffprobe are not on PATH — videopython cannot stitch frames.")
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False, threaded=True)
