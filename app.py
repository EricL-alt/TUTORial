"""
TUTORial — a notebook that quizzes you back.

A student photographs a problem they are stuck on. Claude reads it, invents a
*similar* problem, decomposes that similar problem into steps, and writes one
checkpoint question per step about the student's ORIGINAL problem. Each step is
then drawn as a flipbook of SVG keyframes, which Sora animates into one seamless
video segment that plays up to the next checkpoint and stops.

The student cannot advance until they produce the step themselves. A wrong
answer generates a second video that diagnoses the mistake on their own problem
and sends them back to try again.

Everything lives in this one file: config, the Claude calls, the Sora call, the
background job runner, and the Flask routes.

Run:
    pip install -r requirements.txt
    python app.py                        # http://127.0.0.1:5000
"""

from __future__ import annotations

import base64
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
OPENAI_API_KEY = "sk-proj-REPLACE-WITH-YOUR-KEY"

MODEL = "claude-opus-5"

SORA_MODEL = "sora-2"        # or "sora-2-pro"
SORA_SIZE = "1280x720"       # landscape, to match the taped-in player

# One Sora clip cannot outlast SORA_MAX_SECONDS, which is regularly shorter than the
# narration needs — so a step is split across clips and chained with the extensions
# endpoint. These are the lengths the API accepts: the SDK's VideoSeconds alias is
# stale at ("4", "8", "12"), but VideoExtendParams documents 16 and 20 and Python does
# not enforce a Literal at runtime, so the wider set goes through.
SORA_ALLOWED_SECONDS = (4, 8, 12, 16, 20)
SORA_MAX_SECONDS = SORA_ALLOWED_SECONDS[-1]
WORDS_PER_SECOND = 2.4       # unhurried explaining pace
BEAT_SECONDS = 0.9           # the pause after a line lands

CHECKPOINT_COUNT = 6          # the video stops this many times
FRAMES_PER_STEP = (3, 5)      # flipbook keyframes Claude draws per step

BASE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = BASE_DIR / "media"
UPLOAD_DIR = BASE_DIR / "media" / "_uploads"
FONT_DIR = BASE_DIR / "static" / "fonts"

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
# Downloaded once into static/fonts and registered with fontconfig so the SVG
# in each image_prompt can name them. Offline, the renderer substitutes.
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


# Structured outputs accept only a subset of JSON Schema — no array-length bounds, no
# numeric or string constraints. The SDK strips these automatically, but only when it
# builds the schema itself from a Pydantic model; a hand-written dict goes to the wire
# verbatim and the server answers 400. Ours are hand-written, so we prune them here.
_UNSUPPORTED_SCHEMA_KEYS = frozenset({
    "minItems", "maxItems", "uniqueItems", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "minProperties", "maxProperties",
})


def prune_schema(node: Any) -> Any:
    """Drop every keyword structured outputs will not accept, at any depth."""
    if isinstance(node, dict):
        return {k: prune_schema(v) for k, v in node.items() if k not in _UNSUPPORTED_SCHEMA_KEYS}
    if isinstance(node, list):
        return [prune_schema(v) for v in node]
    return node


def ask_json(system: str, content: list[dict], schema: dict, max_tokens: int = 32000) -> dict:
    """One structured Claude call. Streams so long SVG payloads can't time out."""
    with claude().messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        thinking={"type": "adaptive"},
        output_config={"format": {"type": "json_schema", "schema": prune_schema(schema)}},
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
You draw a FLIPBOOK. Each frame is a complete SVG document — the exact artwork for one
moment of the page — and consecutive frames are the SAME page with one thing added.
Those frames are handed to a video model that animates between them, so the difference
between one frame and the next IS the animation.

THE SVG
- Root tag: <svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720"
  viewBox="0 0 1280 720"> ... </svg>
- Self-contained vector shapes and text only. No <script>, no <foreignObject>, no
  external images, no href to anything off-canvas, no markdown fences around it.
- Keep each frame COMPACT. Every frame is sent as text in one prompt, so a wall of
  needless elements crowds out the ones that matter. Aim for well under 2000 characters
  per frame: the diagram, the labels, the line of work, the caption. Nothing decorative
  that does not carry meaning.

SUBSTRATE (byte-for-byte identical on every frame, so the page never jumps)
- Full-bleed cream sheet: <rect> filling the canvas in {PALETTE['paper']}.
- Ruled blue lines every 34px in {PALETTE['rule']}, starting at y=78.
- A double pink margin rule down the left at x=96 and x=102 in {PALETTE['margin']}.
- Three punched holes down the left gutter at x=48, y=120 / 360 / 600, r=13,
  fill {PALETTE['sky']} at low opacity.

PALETTE — use these and nothing else
- ink {PALETTE['ink']} · lilac {PALETTE['lilac']} / {PALETTE['lilac_dk']} · blush {PALETTE['blush']} / {PALETTE['blush_dk']}
- sky {PALETTE['sky']} / {PALETTE['sky_dk']} · mint {PALETTE['mint']} / {PALETTE['mint_dk']}
- highlighter {PALETTE['yellow']} / {PALETTE['yellow_dk']} · lime {PALETTE['lime']} · muted {PALETTE['muted']}

TYPE — three voices, never mixed up
- font-family="Lilita One, sans-serif" for headlines and stamped labels. ALL CAPS, chunky.
- font-family="Patrick Hand, cursive" for body text and worked math.
- font-family="Caveat, cursive" for margin asides and annotations.
- Body text is never smaller than 26px. Headlines are 44-64px.

PHYSICAL VOCABULARY — this is what makes it read as paper, not as a slide
- Cards are placed on the page: a hard offset shadow (a duplicate <rect> nudged 3px right
  and 4px down in a tint), never a blur filter.
- Everything sits at a sub-degree angle: transform="rotate(-0.6 640 360)" and similar.
- Highlighter swipes behind key phrases: a <rect> in {PALETTE['yellow']} behind the text,
  not a solid fill over it.
- Labels are stamped in a flagged tag box: a filled rect with a triangle point on its right edge.

LAYOUT AND LEGIBILITY
- Keep all content inside x from 132 to 1180 and y from 70 to 660. Nothing touches an edge.
- One idea per frame. Large, few elements, generous whitespace.
- Every frame carries its own narration as a caption strip along the bottom (y around
  600-660) in Patrick Hand at 28px on a cream card, so the video is followable with the
  sound off. Wrap it yourself across <tspan x="..." dy="34"> lines — SVG does not wrap text.
- Math is drawn, not described: draw the triangle, label the sides, show the proportion as
  laid-out text with the numbers in place.

THE FLIPBOOK RULE — the most important one
- Frame 1 establishes the page. Every later frame REPEATS frame 1's elements unchanged,
  at identical coordinates, and ADDS the one new mark that this beat is about — a label,
  a highlighted side, the next line of work appearing below the last.
- Never move, resize or restyle something that is already on the page. Never remove
  anything. The page only ever accumulates.
- That way the animator has exactly one difference to draw per beat, and the result reads
  as a hand writing on one continuous page.
"""

SCENE_SCHEMA = {
    "type": "object",
    "properties": {
        "frames": {
            "type": "array",
            "description": (
                f"{FRAMES_PER_STEP[0]}-{FRAMES_PER_STEP[1]} flipbook keyframes, in order. "
                "Structured outputs cannot bound array length, so hold to that range yourself."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "svg": {
                        "type": "string",
                        "description": "A complete, self-contained SVG document for this keyframe.",
                    },
                    "narration": {
                        "type": "string",
                        "description": "What the voice says over this frame. 1-2 sentences, spoken register.",
                    },
                },
                "required": ["svg", "narration"],
                "additionalProperties": False,
            },
        },
        "closing": {
            "type": "string",
            "description": (
                "One sentence for the video model about how the final beat should land, "
                "e.g. 'the YOUR TURN card settles onto the page and everything holds still'."
            ),
        },
    },
    "required": ["frames", "closing"],
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
            "description": (
                "The solution to the SIMILAR problem, one honest step at a time. "
                f"Exactly {CHECKPOINT_COUNT} entries — structured outputs cannot bound "
                "array length, so hold to that count yourself."
            ),
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
            "description": (
                "One question per step, each about the student's ORIGINAL problem. "
                f"Exactly {CHECKPOINT_COUNT} entries, index-aligned with `steps`."
            ),
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


# ── Normalizing what comes back ──────────────────────────────────────────────
# Structured outputs cannot bound array length, so the counts asked for in the
# prompts are not enforced on the wire. The routes consume `steps` and
# `checkpoints` independently, so a length mismatch would raise IndexError several
# minutes and several video renders into a lesson. Reconcile it up front instead:
# run the lesson at whatever length actually came back.

_CHECKPOINT_TEXT_FIELDS = ("title", "prompt", "target", "placeholder", "hint", "affirm", "explanation")


def normalize_plan(plan: dict) -> dict:
    """Make the plan safe for every route to index, and self-consistent in length."""
    steps = list(plan.get("steps") or [])
    checkpoints = list(plan.get("checkpoints") or [])
    n = min(len(steps), len(checkpoints))
    if n == 0:
        raise ValueError(
            "The lesson came back with no steps or no checkpoints. "
            "Try again, or upload a clearer photo of the problem."
        )
    if len(steps) != n or len(checkpoints) != n:
        warn_once("plan", f"asked for {CHECKPOINT_COUNT} steps, got "
                          f"{len(steps)} steps / {len(checkpoints)} checkpoints — running {n}")
    plan["steps"], plan["checkpoints"] = steps[:n], checkpoints[:n]

    for i, step in enumerate(plan["steps"]):
        step.setdefault("title", f"Step {i + 1}")
        step.setdefault("short_title", f"step {i + 1}")
        for field_name in ("teach", "work"):
            step.setdefault(field_name, "")

    for checkpoint in plan["checkpoints"]:
        for field_name in _CHECKPOINT_TEXT_FIELDS:
            checkpoint[field_name] = str(checkpoint.get(field_name) or "")

        if checkpoint.get("type") not in ("mcq", "text"):
            checkpoint["type"] = "text"

        if checkpoint["type"] == "mcq":
            # api_answer indexes options by the chosen letter, so there must be four.
            options = [str(o) for o in (checkpoint.get("options") or [])][:4]
            options += [""] * (4 - len(options))
            checkpoint["options"] = options
            try:
                correct = int(checkpoint.get("correct_index", 0))
            except (TypeError, ValueError):
                correct = 0
            checkpoint["correct_index"] = correct if 0 <= correct < 4 else 0
        else:
            checkpoint["options"] = []
            checkpoint["correct_index"] = -1

    for key in ("original", "similar"):
        plan.setdefault(key, {})
    return plan


def normalize_frames(payload: dict) -> dict:
    """Drop anything unusable; the array length is not enforced on the wire."""
    frames = []
    for frame in payload.get("frames") or []:
        svg = str(frame.get("svg") or "").strip()
        if "<svg" in svg:
            frames.append({"svg": svg, "narration": str(frame.get("narration") or "")})
    if not frames:
        raise ValueError("Claude returned no usable keyframes for this step.")
    payload["frames"] = frames
    payload["closing"] = str(payload.get("closing") or "the page holds still on the final frame.")
    return payload


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
        "You write the scenes of a TUTORial lesson video. You are given one step of a worked "
        "solution and you return a short run of scenes that teach exactly that step and then "
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

Write {FRAMES_PER_STEP[0]}–{FRAMES_PER_STEP[1]} scenes: the step being made on the similar
problem, then the handoff. Each scene is one `image_prompt` — the SVG for that frame followed by
its TRANSITION TO NEXT SCENE line — and one `narration`."""

    return normalize_frames(ask_json(system, [{"type": "text", "text": user}], SCENE_SCHEMA, max_tokens=32000))


def build_miss_scenes(plan: dict, index: int, given: str, misconception: str,
                      problem_image: Path) -> dict:
    checkpoint = plan["checkpoints"][index]

    system = (
        "You write the scenes of a TUTORial correction video. A student answered a checkpoint "
        "wrongly. You return a short run of scenes that walk back through THEIR OWN problem to "
        "show where that answer comes from and why it does not hold — then send them back to "
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

Write {FRAMES_PER_STEP[0]}–{FRAMES_PER_STEP[1]} scenes, working on THEIR problem, not the similar one:
1. Show their figure with the move they made drawn on it — make the wrong step visible, not just named.
2. Show the thing that breaks. Follow their move to where it contradicts something already on the page.
3. Point at the one idea that fixes it, WITHOUT completing the step for them.
4. Close on a stamped "TRY IT AGAIN" card.

Be kind and specific. Never say "you should know this", never call the mistake careless, and never
give away the answer — they are about to attempt it again."""

    return normalize_frames(ask_json(
        system,
        [image_block(problem_image), {"type": "text", "text": user}],
        SCENE_SCHEMA,
        max_tokens=32000,
    ))


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
# SCENES → VIDEO (Sora)
#
# Claude draws the step as a flipbook: SVG keyframes, each with the narration
# spoken over it. Sora renders a clip of at most SORA_MAX_SECONDS, which is
# routinely shorter than the narration needs — so the flipbook is split into
# chunks that each fit one clip, and every chunk after the first is appended with
# POST /v1/videos/extensions until every line has been covered.
# ─────────────────────────────────────────────────────────────────────────────

_warned: set[str] = set()


def warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        print(f"[{key}] {message}")


_openai_lock = threading.Lock()
_openai = None


def sora():
    global _openai
    with _openai_lock:
        if _openai is None:
            import openai
            if "REPLACE-WITH-YOUR-KEY" in OPENAI_API_KEY:
                raise RuntimeError(
                    "Set OPENAI_API_KEY at the top of app.py to your OpenAI API key."
                )
            _openai = openai.OpenAI(api_key=OPENAI_API_KEY, timeout=900.0)
    return _openai


def speaking_seconds(narration: str) -> float:
    """How long this line takes to say, plus a beat to land on."""
    return len(narration.split()) / WORDS_PER_SECOND + BEAT_SECONDS


def fit_seconds(needed: float) -> str:
    """The shortest clip length Sora offers that still covers `needed`."""
    for allowed in SORA_ALLOWED_SECONDS:
        if allowed >= needed:
            return str(allowed)
    return str(SORA_ALLOWED_SECONDS[-1])


def plan_chunks(frames: list[dict]) -> list[dict]:
    """Split the flipbook so each chunk's narration fits inside one Sora clip.

    A frame whose line is longer than a whole clip still gets its own chunk —
    it is the most we can give it — but nothing is ever dropped.
    """
    chunks: list[dict] = []
    current: list[dict] = []
    running = 0.0

    for frame in frames:
        needed = speaking_seconds(frame["narration"])
        if current and running + needed > SORA_MAX_SECONDS:
            chunks.append({"frames": current, "seconds": fit_seconds(running)})
            current, running = [], 0.0
        current.append(frame)
        running += needed

    if current:
        chunks.append({"frames": current, "seconds": fit_seconds(running)})
    return chunks


def _animation_rules(total_frames: int, seconds: str) -> list[str]:
    return [
        "HOW TO ANIMATE",
        "- The subject is a single sheet of cream school-notebook paper, ruled in pale "
        "blue, seen straight on and filling the frame. The camera never moves.",
        "- Hold each keyframe long enough to read it, then move to the next.",
        "- Consecutive keyframes are the SAME page with something added. Never cut and "
        "never slide the page. Animate the difference only: new strokes, labels and "
        "numbers draw themselves onto the page as if a hand were writing them, and "
        "highlighter sweeps across in one motion. Everything already on the page stays "
        "exactly where it is, at the same size.",
        "- Speak each keyframe's narration in full, at an unhurried explaining pace, "
        "and do not move on to the next keyframe until its line has finished. Every "
        "line must be spoken completely — never trail off or cut a sentence short.",
        f"- This clip is {seconds} seconds long and carries {total_frames} keyframe(s). "
        "Pace them so the narration fits comfortably inside it.",
        "",
        "DO NOT add photographic detail, hands, faces, desks, rooms, logos, watermarks or "
        "any text that is not in the SVGs. Flat vector artwork on paper, nothing else.",
    ]


def _keyframe_block(frames: list[dict], offset: int, grand_total: int) -> list[str]:
    parts = ["", "=" * 60]
    for i, frame in enumerate(frames, offset + 1):
        parts += [
            "",
            f"KEYFRAME {i} OF {grand_total}",
            f'NARRATION: "{frame["narration"]}"',
            "SVG:",
            frame["svg"],
            "",
            "-" * 60,
        ]
    return parts


def build_sora_prompt(frames: list[dict], seconds: str, grand_total: int, closing: str) -> str:
    """The opening clip: establish the page and work through the first keyframes."""
    parts = [
        "Animate this hand-drawn flipbook into ONE seamless, continuous video.",
        "",
        f"You are given {len(frames)} keyframes in order, of {grand_total} in the full "
        "lesson. Each keyframe is a complete SVG document given as text, together with the "
        "narration spoken over it. The SVG is the exact artwork for that moment — reproduce "
        "its layout, its shapes, its wording and its colours faithfully.",
        "",
    ] + _animation_rules(len(frames), seconds)
    if len(frames) == grand_total:
        parts += ["", f"CLOSING BEAT: {closing}"]
    else:
        parts += ["", "This clip is the OPENING of a longer take. End it mid-work, with the "
                      "page settled and the camera still, so the next part continues cleanly."]
    return "\n".join(parts + _keyframe_block(frames, 0, grand_total))


def build_extension_prompt(frames: list[dict], seconds: str, offset: int,
                           grand_total: int, closing: str, is_last: bool) -> str:
    """A continuation clip: same page, next keyframes written onto it."""
    parts = [
        "Continue the video exactly where it left off. This is the SAME sheet of "
        "notebook paper, in the same position, with everything already written on it "
        "still there and unchanged. Do not restart, do not re-establish the page, do "
        "not cut to a new shot.",
        "",
        f"Carry on with keyframes {offset + 1} to {offset + len(frames)} of {grand_total}. "
        "Each is a complete SVG document given as text, together with the narration spoken "
        "over it. The SVG shows the whole page as it should look at that moment — the marks "
        "already present from earlier plus the new one this beat adds. Draw only the new "
        "marks; the rest is already on the page.",
        "",
    ] + _animation_rules(len(frames), seconds)
    if is_last:
        parts += ["", f"CLOSING BEAT: {closing}"]
    else:
        parts += ["", "More follows after this, so end with the page settled and the camera "
                      "still rather than on a flourish."]
    return "\n".join(parts + _keyframe_block(frames, offset, grand_total))


def _await_video(video, on_progress=None, label=""):
    """Poll one Sora job to completion."""
    while video.status in ("queued", "in_progress"):
        if on_progress:
            on_progress(f"{label}{video.status.replace('_', ' ')}", video.progress or 0)
        time.sleep(4)
        video = sora().videos.retrieve(video.id)
    if video.status != "completed":
        detail = getattr(video.error, "message", None) or video.status
        raise RuntimeError(f"Sora could not render this step: {detail}")
    return video


def create_sora_video(frames: list[dict], output_path: str, closing: str,
                      on_progress=None) -> dict:
    """Render the whole flipbook, extending the clip until every line is spoken."""
    import warnings

    chunks = plan_chunks(frames)
    spoken = sum(speaking_seconds(f["narration"]) for f in frames)
    budget = sum(int(c["seconds"]) for c in chunks)
    warn_once(
        "sora",
        f"{len(frames)} keyframes, ~{spoken:.0f}s of narration -> "
        f"{len(chunks)} clip(s) totalling {budget}s "
        f"(1 create + {len(chunks) - 1} extension(s))",
    )

    with warnings.catch_warnings():   # the SDK flags the Sora endpoints as deprecated
        warnings.simplefilter("ignore", DeprecationWarning)

        video, offset = None, 0
        for n, chunk in enumerate(chunks):
            tag = f"clip {n + 1}/{len(chunks)} "
            if n == 0:
                video = sora().videos.create(
                    model=SORA_MODEL,
                    prompt=build_sora_prompt(chunk["frames"], chunk["seconds"], len(frames), closing),
                    seconds=chunk["seconds"],
                    size=SORA_SIZE,
                )
            else:
                video = sora().videos.extend(
                    video={"id": video.id},
                    prompt=build_extension_prompt(
                        chunk["frames"], chunk["seconds"], offset, len(frames),
                        closing, is_last=(n == len(chunks) - 1),
                    ),
                    seconds=chunk["seconds"],
                )
            video = _await_video(video, on_progress, tag)
            offset += len(chunk["frames"])

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        sora().videos.download_content(video.id, variant="video").write_to_file(output_path)

    # Trust the file over the plan: an extension chain may or may not hand back the
    # whole take, and the page needs the real length either way.
    duration = mp4_duration(output_path) or float(budget)
    if duration + 1.0 < budget:
        warn_once("sora-short", (
            f"the downloaded clip is {duration:.1f}s but {budget}s was generated — this "
            "build of the extensions endpoint returns only the final segment. The earlier "
            "chunks are still on your OpenAI account by video id."
        ))

    # Narration is paced across the clip in proportion to how long each line takes.
    transcript, cursor = [], 0.0
    scale = duration / max(spoken, 0.001)
    for frame in frames:
        span = speaking_seconds(frame["narration"]) * scale
        transcript.append({
            "start": round(cursor, 2),
            "end": round(min(cursor + span, duration), 2),
            "text": frame["narration"],
        })
        cursor += span

    return {"duration": round(duration, 2), "transcript": transcript, "video_id": video.id}


def mp4_duration(path: str) -> float | None:
    """Read an MP4's length out of its `mvhd` atom. Pure stdlib, no ffprobe."""
    try:
        blob = Path(path).read_bytes()
        if b"ftyp" not in blob[:64]:      # every MP4 opens with an ftyp box
            return None
        marker = blob.find(b"mvhd")
        if marker < 0:
            return None
        # Offsets from the 'mvhd' fourcc: version(1) flags(3), then created and
        # modified — 4 bytes each in version 0, 8 each in version 1 — then timescale
        # and duration.
        version = blob[marker + 4]
        if version == 1:
            timescale = int.from_bytes(blob[marker + 24:marker + 28], "big")
            units = int.from_bytes(blob[marker + 28:marker + 36], "big")
        else:
            timescale = int.from_bytes(blob[marker + 16:marker + 20], "big")
            units = int.from_bytes(blob[marker + 20:marker + 24], "big")
        if not timescale:
            return None
        seconds = units / timescale
        return seconds if 0 < seconds < 3600 else None
    except Exception:
        return None


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


SEGMENT_LABELS = ["Writing the next step", "Drawing the flipbook", "Sora animating the page"]


def generate_segment(job: Job, lesson: Lesson, index: int) -> dict:
    """Claude draws step `index` as SVG scenes; videopython stitches them."""
    job.advance(0, f"Step {index + 1} of {len(lesson.plan['steps'])}")
    scenes = build_step_scenes(lesson.plan, index)

    out = MEDIA_DIR / lesson.sid / f"step_{index}.mp4"
    job.advance(1, f"{len(scenes['frames'])} keyframes drawn")
    rendered = create_sora_video(
        scenes["frames"], str(out), scenes["closing"],
        lambda status, pct: job.advance(2, f"Sora {status.replace('_', ' ')} — {pct}%"),
    )

    payload = {
        "index": index,
        "video_url": f"/media/{lesson.sid}/step_{index}.mp4",
        "duration": rendered["duration"],
        "transcript": rendered["transcript"],
        "checkpoint": public_checkpoint(lesson.plan["checkpoints"][index], index),
    }
    lesson.segments[index] = payload
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
        "Drawing the flipbook",
        "Sora animating the page",
    ]

    def work(job: Job) -> dict:
        job.advance(0, "Looking at your image")
        lesson.plan = normalize_plan(build_plan(lesson.image))
        lesson.status = [0] * len(lesson.plan["checkpoints"])

        job.advance(1, lesson.plan["original"]["subject"])
        time.sleep(0.2)  # let the checklist tick over visibly

        out = MEDIA_DIR / lesson.sid / "step_0.mp4"
        scenes = build_step_scenes(lesson.plan, 0)
        job.advance(2, f"{len(scenes['frames'])} keyframes drawn")
        rendered = create_sora_video(
            scenes["frames"], str(out), scenes["closing"],
            lambda status, pct: job.advance(3, f"Sora {status.replace('_', ' ')} — {pct}%"),
        )

        lesson.segments[0] = {
            "index": 0,
            "video_url": f"/media/{lesson.sid}/step_0.mp4",
            "duration": rendered["duration"],
            "transcript": rendered["transcript"],
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
        options = checkpoint.get("options") or []
        given_text = options[chosen] if 0 <= chosen < len(options) else str(given)
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
            out = MEDIA_DIR / lesson.sid / f"miss_{index}_{stamp}.mp4"
            job.advance(1, f"{len(scenes['frames'])} keyframes drawn")
            rendered = create_sora_video(
                scenes["frames"], str(out), scenes["closing"],
                lambda status, pct: job.advance(2, f"Sora {status.replace('_', ' ')} — {pct}%"),
            )

            payload = {
                "index": index,
                "video_url": f"/media/{lesson.sid}/miss_{index}_{stamp}.mp4",
                "duration": rendered["duration"],
                "transcript": rendered["transcript"],
            }
            lesson.misses[index] = payload
            return {"correction": payload}

        labels = ["Working out where that came from", "Drawing it on your figure", "Sora animating the page"]
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
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False, threaded=True)
