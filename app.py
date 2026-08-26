"""
TUTORial — a notebook that quizzes you back.

A student photographs a problem they are stuck on. Claude reads it, invents a
*similar* problem, decomposes that similar problem into steps, and writes one
checkpoint question per step about the student's ORIGINAL problem. Each step is
then drawn as a flipbook of SVG keyframes, which Qwen animates into one seamless
video segment that plays up to the next checkpoint and stops.

The student cannot advance until they produce the step themselves. A wrong
answer generates a second video that diagnoses the mistake on their own problem
and sends them back to try again.

Everything lives in this one file: config, the Claude calls, the Qwen call, the
background job runner, and the Flask routes.

Run:
    pip install -r requirements.txt
    python app.py                        # http://127.0.0.1:5000
"""

from __future__ import annotations

import base64
import json
import math
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
DASHSCOPE_API_KEY = "sk-REPLACE-WITH-YOUR-KEY"

MODEL = "claude-opus-5"

QWEN_MODEL = "wan3.0-video-prime"   # Wan3.0 Prime — 2-30s per clip, with audio
QWEN_RESOLUTION = "720P"     # "480P" | "720P" | "1080P" — price is per second of output
QWEN_RATIO = "widescreen"    # "widescreen" | "adaptive" | "vertical" | "square" | "classic"
DASHSCOPE_REGION = "intl"    # "intl" for the Singapore host, "cn" for mainland

# QWEN_MAX_SECONDS is the ceiling on ONE clip; MAX_STEP_SECONDS is how long a whole
# step may run, and sets the narration budget. When a step needs more than one clip,
# it gets one: because the flipbook only ever accumulates, the LAST keyframe of a clip
# is already a valid opening frame for the next, so a step is rendered as N clips that
# each open on the previous clip's closing frame and the player runs them back to back.
QWEN_MAX_SECONDS = 30        # wan3.0-video-prime accepts an integer 2-30
QWEN_MIN_SECONDS = 2
MAX_STEP_SECONDS = 30        # ~61 words a step, and usually one clip. Raising this buys
                             # words at a linear cost — output is billed per second.
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


TIGHTEN_SCHEMA = {
    "type": "object",
    "properties": {
        "narrations": {
            "type": "array",
            "description": "The rewritten lines, one per keyframe, in the same order.",
            "items": {"type": "string"},
        },
    },
    "required": ["narrations"],
    "additionalProperties": False,
}


def fit_narration(payload: dict) -> dict:
    """Shorten over-long narration rather than let the video model truncate it.

    Two ways a step can overrun. It can be too long overall, past what
    MAX_STEP_SECONDS allows. Or one single line can be longer than a whole
    clip, in which case no amount of splitting saves it — a clip cannot be stretched
    past QWEN_MAX_SECONDS, so that line would be cut off mid-sentence. Either is worth
    one extra call to avoid.
    """
    frames = payload["frames"]
    spoken = flipbook_seconds(frames)
    ceiling = float(MAX_STEP_SECONDS)
    per_line = QWEN_MAX_SECONDS - BEAT_SECONDS      # one line alone in its own clip
    longest = max(speaking_seconds(f["narration"]) for f in frames)
    if spoken <= ceiling and longest <= per_line:
        return payload

    budget = narration_word_budget()
    if longest > per_line:
        warn_once("tighten", f"one line runs ~{longest:.0f}s, past the {per_line:.0f}s a single "
                             "clip can hold; asking for a shorter cut")
    else:
        warn_once("tighten", f"narration ran to ~{spoken:.0f}s (cap {ceiling:.0f}s); asking for a shorter cut")
    lines = "\n".join(f'{i + 1}. "{f["narration"]}"' for i, f in enumerate(frames))
    try:
        result = ask_json(
            system=(
                "You tighten narration for a short explainer video. Return the same number of "
                "lines in the same order, saying the same things in fewer words. Keep every "
                "idea and every number; drop only padding, restatement and throat-clearing. "
                "Plain spoken sentences."
            ),
            content=[{"type": "text", "text":
                      f"These {len(frames)} lines must be speakable in under "
                      f"{ceiling:.0f} seconds — about {budget} words in total, "
                      f"currently {sum(len(f['narration'].split()) for f in frames)}. "
                      f"No single line may run past {int(per_line * WORDS_PER_SECOND)} words, "
                      f"because each is spoken over one short clip that cannot be "
                      f"stretched.\n\n{lines}"}],
            schema=TIGHTEN_SCHEMA,
            max_tokens=8000,
        )
        tightened = [str(n) for n in result.get("narrations") or []]
        if len(tightened) == len(frames) and all(t.strip() for t in tightened):
            for frame, line in zip(frames, tightened):
                frame["narration"] = line.strip()
            warn_once("tighten", f"rewritten to ~{flipbook_seconds(frames):.0f}s, "
                                 f"longest line ~{max(speaking_seconds(f['narration']) for f in frames):.0f}s")
    except Exception as exc:
        warn_once("tighten", f"could not shorten the narration ({exc}); sending it as written")
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
    budget, clip_budget = narration_word_budget(), clip_word_budget()
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

Draw {FRAMES_PER_STEP[0]}–{FRAMES_PER_STEP[1]} keyframes: the step being made on the similar
problem, then the handoff. Each keyframe is one SVG plus the line spoken over it.

LENGTH
The step is rendered as a run of short clips played back to back, so you are not writing to a
single short window — say what the step actually needs. Keep the whole
step under {budget} words across all keyframes combined, in plain spoken sentences, and no
SINGLE keyframe's line past {clip_budget} words — each line is spoken over one short clip that
cannot be stretched. Anything past that gets sent back to be cut, so stop when the step is
taught rather than padding."""

    return fit_narration(normalize_frames(
        ask_json(system, [{"type": "text", "text": user}], SCENE_SCHEMA, max_tokens=32000)))


def build_miss_scenes(plan: dict, index: int, given: str, misconception: str,
                      problem_image: Path) -> dict:
    checkpoint = plan["checkpoints"][index]
    budget, clip_budget = narration_word_budget(), clip_word_budget()

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
give away the answer — they are about to attempt it again.

LENGTH
The correction is rendered as a run of short clips played back to back, so you are not writing
to a single short window. Keep the whole correction under {budget} words
across all keyframes combined, and no SINGLE keyframe's line past {clip_budget} words."""

    return fit_narration(normalize_frames(ask_json(
        system,
        [image_block(problem_image), {"type": "text", "text": user}],
        SCENE_SCHEMA,
        max_tokens=32000,
    )))


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
# SCENES → VIDEO (Qwen / Wan on DashScope)
#
# Claude draws the step as a flipbook: SVG keyframes, each with the narration
# spoken over it. The whole flipbook goes into one DashScope video-synthesis
# call and comes back as a single clip.
#
# There is no extensions endpoint here, so instead of stretching the video to
# fit the narration we size the narration to fit the video: Claude is given a
# word budget derived from MAX_STEP_SECONDS, and anything that still overruns is
# sent back once to be tightened. A step therefore always fits inside one clip
# and is never cut off mid-sentence.
# ─────────────────────────────────────────────────────────────────────────────

_warned: set[str] = set()


def warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        print(f"[{key}] {message}")


_dashscope_lock = threading.Lock()
_dashscope_ready = False


def dashscope_api():
    """The DashScope SDK, pointed at the right region and holding the key."""
    global _dashscope_ready
    import dashscope
    with _dashscope_lock:
        if not _dashscope_ready:
            if "REPLACE-WITH-YOUR-KEY" in DASHSCOPE_API_KEY:
                raise RuntimeError(
                    "Set DASHSCOPE_API_KEY at the top of app.py to your Alibaba Cloud "
                    "Model Studio (DashScope) key."
                )
            dashscope.api_key = DASHSCOPE_API_KEY
            # The SDK ships pointed at the mainland endpoint; an international
            # Model Studio account must be sent to the Singapore host instead.
            if DASHSCOPE_REGION == "intl":
                dashscope.base_http_api_url = "https://dashscope-intl.aliyuncs.com/api/v1"
            _dashscope_ready = True
    return dashscope


def speaking_seconds(narration: str) -> float:
    """How long this line takes to say, plus a beat to land on."""
    return len(narration.split()) / WORDS_PER_SECOND + BEAT_SECONDS


def flipbook_seconds(frames: list[dict]) -> float:
    return sum(speaking_seconds(f["narration"]) for f in frames)


def clip_word_budget() -> int:
    """Words that fit in ONE clip, once the pauses between lines are paid for."""
    speakable = QWEN_MAX_SECONDS - BEAT_SECONDS * FRAMES_PER_STEP[1]
    return int(speakable * WORDS_PER_SECOND)


def narration_word_budget() -> int:
    """Words a whole step may use, however many clips it ends up spanning."""
    speakable = MAX_STEP_SECONDS - BEAT_SECONDS * FRAMES_PER_STEP[1]
    return int(max(speakable, 1) * WORDS_PER_SECOND)


def max_clips_per_step() -> int:
    """How many clips MAX_STEP_SECONDS can spill into, at this clip ceiling."""
    return max(1, math.ceil(MAX_STEP_SECONDS / QWEN_MAX_SECONDS))


def plan_clips(frames: list[dict]) -> list[dict]:
    """Split the flipbook into clips that each fit inside QWEN_MAX_SECONDS.

    Each clip after the first opens on the previous clip's closing keyframe — the
    page as the student last saw it — so the seam between two files lands on
    identical artwork. That carried frame is scenery, not narration: it has already
    been spoken over, and the prompt says so.
    """
    clips: list[dict] = []
    current: list[dict] = []
    running = 0.0

    for frame in frames:
        needed = speaking_seconds(frame["narration"])
        # A carried opening frame costs a little screen time before the first line.
        overhead = BEAT_SECONDS if (clips or current) and not current else 0.0
        if current and running + needed > QWEN_MAX_SECONDS:
            clips.append({"frames": current, "opening": current[-1]["svg"]})
            current, running = [], 0.0
            overhead = BEAT_SECONDS
        current.append(frame)
        running += needed + overhead

    if current:
        clips.append({"frames": current, "opening": current[-1]["svg"]})

    # `opening` is what this clip HANDS ON; shift it so each clip carries what it
    # RECEIVES, and the first clip receives nothing.
    handed = None
    for clip in clips:
        closing, clip["opening"] = clip["opening"], handed
        handed = closing

    # MAX_STEP_SECONDS bounds the word budget handed to Claude, and fit_narration
    # enforces it upstream. If something still lands over, render the extra clip:
    # dropping a keyframe would lose teaching, which is the whole point of splitting.
    if len(clips) > max_clips_per_step():
        warn_once("clips", f"a step needed {len(clips)} clips, over the {max_clips_per_step()} "
                           "budgeted — rendering them all rather than dropping a keyframe")
    return clips


def fit_duration(needed: float) -> int:
    """A whole number of seconds that covers `needed`, inside the model's range."""
    return int(max(QWEN_MIN_SECONDS, min(QWEN_MAX_SECONDS, math.ceil(needed))))


def build_qwen_prompt(clip: dict, index: int, total_clips: int, duration: int,
                      closing: str) -> str:
    """One clip's prompt: what it opens on, its keyframes, and how to animate them."""
    frames = clip["frames"]
    is_last = index == total_clips - 1
    parts = [
        "Animate this hand-drawn flipbook into ONE seamless, continuous video with "
        "spoken narration.",
        "",
    ]

    if clip["opening"] is not None:
        parts += [
            f"This is part {index + 1} of {total_clips} of one continuous take. It opens "
            "on the OPENING FRAME below — the page exactly as the previous part left it, "
            "already written on. Start there, held still, and carry straight on.",
            "",
            "OPENING FRAME — already on the page. Do NOT narrate it, do NOT redraw it, "
            "and do NOT re-establish the shot. It is where you begin.",
            clip["opening"],
            "",
            "-" * 60,
            "",
        ]

    parts += [
        f"You are given {len(frames)} keyframes in order. Each keyframe is a complete SVG "
        "document given as text, together with the narration spoken over it. The SVG is "
        "the exact artwork for that moment — reproduce its layout, its shapes, its "
        "wording and its colours faithfully.",
        "",
        "HOW TO ANIMATE",
        "- The subject is a single sheet of cream school-notebook paper, ruled in pale "
        "blue, seen straight on and filling the frame. The camera never moves.",
        "- Hold each keyframe long enough to read it, then move to the next.",
        "- Consecutive keyframes are the SAME page with something added. Never cut and "
        "never slide the page. Animate the difference only: new strokes, labels and "
        "numbers draw themselves onto the page as if a hand were writing them, and "
        "highlighter sweeps across in one motion. Everything already on the page stays "
        "exactly where it is, at the same size.",
        "- The result must read as one continuous take of a page being worked on, not as "
        "a slideshow of separate images.",
        "",
        "NARRATION",
        "- Speak every keyframe's line in full, in order, in a warm unhurried teaching "
        "voice. Do not paraphrase, do not skip a line, and never trail off or cut a "
        "sentence short.",
        f"- The clip is {duration} seconds and the narration has been written to fit it "
        "with room to spare. Pace it evenly and let it breathe.",
        "",
        "DO NOT add photographic detail, hands, faces, desks, rooms, logos, watermarks or "
        "any text that is not in the SVGs. Flat vector artwork on paper, nothing else.",
        "",
    ]

    if is_last:
        parts += [f"CLOSING BEAT: {closing}"]
    else:
        parts += ["END STILL: the next part opens on this clip's final frame, so finish "
                  "with the page settled and the camera still — no flourish, no fade out."]

    parts += ["", "=" * 60]
    for i, frame in enumerate(frames, 1):
        parts += [
            "",
            f"KEYFRAME {i} OF {len(frames)}",
            f'NARRATION: "{frame["narration"]}"',
            "SVG:",
            frame["svg"],
            "",
            "-" * 60,
        ]
    return "\n".join(parts)


def render_one_clip(clip: dict, index: int, total_clips: int, closing: str,
                    output_path: str, on_progress=None) -> float:
    """Submit one clip, poll it, download it. Returns its measured length."""
    import requests

    dashscope_api()
    from dashscope import VideoSynthesis
    from dashscope.common.constants import TaskStatus

    needed = sum(speaking_seconds(f["narration"]) for f in clip["frames"])
    if clip["opening"] is not None:
        needed += BEAT_SECONDS
    duration = fit_duration(needed)
    prompt = build_qwen_prompt(clip, index, total_clips, duration, closing)

    started = VideoSynthesis.async_call(
        model=QWEN_MODEL,
        prompt=prompt,
        resolution=QWEN_RESOLUTION,
        ratio=QWEN_RATIO,
        duration=duration,
    )
    if started.status_code != 200 or not started.output:
        raise RuntimeError(
            f"DashScope refused the job ({started.code or started.status_code}): "
            f"{started.message or 'no detail'}"
        )

    task, label = started, f"part {index + 1}/{total_clips} "
    while True:
        status = task.output.task_status
        if status in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELED, TaskStatus.UNKNOWN):
            break
        if on_progress:
            on_progress(f"{label}{status.lower()}", 0)
        time.sleep(5)
        task = VideoSynthesis.fetch(started)

    if task.output.task_status != TaskStatus.SUCCEEDED:
        detail = task.message or task.output.task_status
        raise RuntimeError(f"Qwen could not render part {index + 1}: {detail}")
    if not task.output.video_url:
        raise RuntimeError(f"Qwen reported success on part {index + 1} but returned no video URL.")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with requests.get(task.output.video_url, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        with open(output_path, "wb") as handle:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                handle.write(chunk)

    return mp4_duration(output_path) or float(duration)


def create_qwen_video(frames: list[dict], out_stem: Path, url_stem: str, closing: str,
                      on_progress=None) -> dict:
    """Render a whole step as N chained clips the player runs back to back."""
    clips = plan_clips(frames)
    spoken = flipbook_seconds(frames)
    warn_once(
        "qwen",
        f"{len(frames)} keyframes, ~{spoken:.0f}s of narration -> {len(clips)} clip(s) "
        f"of at most {QWEN_MAX_SECONDS}s, each opening on the last frame of the one before",
    )

    rendered, total = [], 0.0
    for i, clip in enumerate(clips):
        path = f"{out_stem}_{i}.mp4"
        seconds = render_one_clip(clip, i, len(clips), closing, path, on_progress)
        rendered.append({"video_url": f"{url_stem}_{i}.mp4", "duration": round(seconds, 2)})
        total += seconds

    # Narration is paced across the whole segment in proportion to how long each line
    # takes, so the transcript follows the student across the seams.
    transcript, cursor = [], 0.0
    scale = total / max(spoken, 0.001)
    for frame in frames:
        span = speaking_seconds(frame["narration"]) * scale
        transcript.append({
            "start": round(cursor, 2),
            "end": round(min(cursor + span, total), 2),
            "text": frame["narration"],
        })
        cursor += span

    return {"clips": rendered, "duration": round(total, 2), "transcript": transcript}


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


SEGMENT_LABELS = ["Writing the next step", "Drawing the flipbook", "Qwen animating the page"]


def generate_segment(job: Job, lesson: Lesson, index: int) -> dict:
    """Claude draws step `index` as SVG scenes; videopython stitches them."""
    job.advance(0, f"Step {index + 1} of {len(lesson.plan['steps'])}")
    scenes = build_step_scenes(lesson.plan, index)

    job.advance(1, f"{len(scenes['frames'])} keyframes drawn")
    rendered = create_qwen_video(
        scenes["frames"],
        MEDIA_DIR / lesson.sid / f"step_{index}",
        f"/media/{lesson.sid}/step_{index}",
        scenes["closing"],
        lambda status, pct: job.advance(2, f"Qwen is rendering {status}"),
    )

    payload = {
        "index": index,
        "clips": rendered["clips"],
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
        "Qwen animating the page",
    ]

    def work(job: Job) -> dict:
        job.advance(0, "Looking at your image")
        lesson.plan = normalize_plan(build_plan(lesson.image))
        lesson.status = [0] * len(lesson.plan["checkpoints"])

        job.advance(1, lesson.plan["original"]["subject"])
        time.sleep(0.2)  # let the checklist tick over visibly

        scenes = build_step_scenes(lesson.plan, 0)
        job.advance(2, f"{len(scenes['frames'])} keyframes drawn")
        rendered = create_qwen_video(
            scenes["frames"],
            MEDIA_DIR / lesson.sid / "step_0",
            f"/media/{lesson.sid}/step_0",
            scenes["closing"],
            lambda status, pct: job.advance(3, f"Qwen is rendering {status}"),
        )

        lesson.segments[0] = {
            "index": 0,
            "clips": rendered["clips"],
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
            job.advance(1, f"{len(scenes['frames'])} keyframes drawn")
            rendered = create_qwen_video(
                scenes["frames"],
                MEDIA_DIR / lesson.sid / f"miss_{index}_{stamp}",
                f"/media/{lesson.sid}/miss_{index}_{stamp}",
                scenes["closing"],
                lambda status, pct: job.advance(2, f"Qwen is rendering {status}"),
            )

            payload = {
                "index": index,
                "clips": rendered["clips"],
                "duration": rendered["duration"],
                "transcript": rendered["transcript"],
            }
            lesson.misses[index] = payload
            return {"correction": payload}

        labels = ["Working out where that came from", "Drawing it on your figure", "Qwen animating the page"]
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
