"""
graders.py — the Claude API sitting inside the exercises.

Every exercise in the curriculum is marked by a real model call. The learner
presses EXECUTE, their work goes to Claude with the rubric for that exercise,
and what comes back is a verdict per component. The page paints an approved
component green and an unapproved one red.

Two exercises are marked here in Python instead, on purpose. Matching a task to
a tool family and reordering the writing loop both have a single defensible
answer, and a course about not over using AI should not call a model to compare
two lists.

If no API key is configured, or a call fails, every grader falls back to a local
rubric so the whole course still works offline. The response says which engine
marked it, and the page says so too.
"""

from __future__ import annotations

import re
from typing import Any, Callable

import curriculum

# app.py injects its own Claude caller here at import time, which keeps this
# module importable and testable without Flask.
_asker: Callable[..., dict] | None = None
_key_ready: Callable[[], bool] = lambda: False


def wire(asker: Callable[..., dict], key_ready: Callable[[], bool]) -> None:
    global _asker, _key_ready
    _asker, _key_ready = asker, key_ready


VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "ok": {"type": "boolean"},
                    "note": {"type": "string"},
                },
                "required": ["key", "ok", "note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "verdicts"],
    "additionalProperties": False,
}

HOUSE_STYLE = (
    "Write every word of your summary and every note in plain sentences that contain "
    "no dash, no hyphen, no colon and no semicolon of any kind. Use full stops and "
    "commas instead. This is a hard formatting requirement and the interface will look "
    "broken if you ignore it. Keep each note under thirty words, address the learner as "
    "you, and when a component fails say the specific thing that would fix it."
)

TUTOR_SYSTEM = (
    "You are the marker for a free course that teaches college students how to use AI "
    "properly and ethically. The course paraphrases the Student Guide to Artificial "
    "Intelligence, AI U 2025, Elon Edition, published by Elon University and the "
    "American Association of Colleges and Universities. You mark generously on effort "
    "and strictly on substance. A component that is present but vague is not approved. "
    "Never rewrite the learner's work for them. Name what is missing and stop.\n\n"
    + HOUSE_STYLE
)

_FORBIDDEN = re.compile(r"[-:;‐‑‒–—―]")

# The engine line under every result already says who did the marking, so a local
# summary talks about the work rather than repeating the news about the key.
_LOCAL_SUMMARY = (
    "Read against a rubric rather than by a model, so treat a green box as the "
    "floor and not the ceiling."
)


def scrub(text: str) -> str:
    """Last line of defence for the house style, applied to anything a model wrote."""
    text = _FORBIDDEN.sub(" ", str(text or ""))
    return re.sub(r"\s{2,}", " ", text).strip()


def _ask(user_text: str, max_tokens: int = 3000) -> dict:
    if _asker is None:
        raise RuntimeError("no Claude caller has been wired in")
    return _asker(TUTOR_SYSTEM, [{"type": "text", "text": user_text}],
                  VERDICT_SCHEMA, max_tokens)


def _shape(raw: dict, expected: list[str], engine: str,
           outof: int | None = None) -> dict:
    """Normalise whatever came back into one verdict per expected key."""
    got = {}
    for verdict in raw.get("verdicts", []):
        key = str(verdict.get("key", "")).strip()
        if key in expected and key not in got:
            got[key] = {"key": key, "ok": bool(verdict.get("ok")),
                        "note": scrub(verdict.get("note", ""))}
    verdicts = [got.get(k, {"key": k, "ok": False,
                            "note": "The marker did not reach this one. Try again."})
                for k in expected]
    for verdict in verdicts:
        verdict.setdefault("state", "ok" if verdict["ok"] else "no")
    score = sum(1 for v in verdicts if v["ok"])
    return {
        "verdicts": verdicts,
        "score": score,
        "outof": outof if outof is not None else len(verdicts),
        "summary": scrub(raw.get("summary", "")),
        "engine": engine,
    }


def _finish(spec: dict, result: dict) -> dict:
    pass_mark = spec.get("pass_mark", result["outof"])
    result["passed"] = result["score"] >= pass_mark
    result["pass_mark"] = pass_mark
    result["reward"] = spec.get("reward", "")
    if not result["summary"]:
        result["summary"] = ("Every part held up." if result["passed"]
                             else "Some parts still need work. Read the red ones and go again.")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# KIND. FIELDS — many labelled boxes, one verdict each
# ─────────────────────────────────────────────────────────────────────────────

def grade_fields(spec: dict, payload: dict) -> dict:
    values = {f["key"]: str(payload.get("values", {}).get(f["key"], "")).strip()
              for f in spec["fields"]}

    # An optional box left empty is neither approved nor rejected.
    skipped = [f["key"] for f in spec["fields"]
               if f.get("optional") and not values[f["key"]]]
    judged = [f for f in spec["fields"] if f["key"] not in skipped]
    keys = [f["key"] for f in judged]

    if not any(values[k] for k in keys):
        return _finish(spec, {
            "verdicts": [{"key": f["key"], "ok": False, "state": "no",
                          "note": "This is still empty."} for f in spec["fields"]],
            "score": 0, "outof": len(judged), "engine": "local",
            "summary": "Nothing has been written yet.",
        })

    if _key_ready():
        try:
            lines = [f"EXERCISE. {spec['title']}", f"WHAT THE LEARNER WAS ASKED. {spec['intro']}"]
            if spec.get("scenario"):
                lines.append(f"THE ASSIGNMENT THEY ARE PROMPTING ABOUT. {spec['scenario']}")
            lines.append("")
            lines.append("Judge each component below on its own. Approve it only when it is "
                         "genuinely present and specific enough to change what a model would "
                         "produce. A component that restates the label, or that could be "
                         "pasted into any assignment in any subject, is not approved.")
            lines.append("")
            for field in judged:
                lines.append(f"COMPONENT key {field['key']}")
                lines.append(f"  what it must contain. {field['label']}. {field.get('hint','')}")
                lines.append(f"  what the learner wrote. {values[field['key']] or 'nothing at all'}")
                lines.append("")
            lines.append("Return one verdict object for every component key listed above and "
                         "no others.")
            raw = _ask("\n".join(lines))
            result = _shape(raw, keys, "claude", outof=len(judged))
        except Exception as exc:
            result = _local_fields(judged, values, note=str(exc))
    else:
        result = _local_fields(judged, values)

    for key in skipped:
        result["verdicts"].append({"key": key, "ok": False, "state": "skip",
                                   "note": "Optional, and you left it out. That is allowed."})
    return _finish(spec, result)


def _local_fields(fields: list[dict], values: dict, note: str = "") -> dict:
    verdicts = []
    for field in fields:
        text = values[field["key"]]
        words = len(text.split())
        ok = words >= 12 and len(set(text.lower().split())) >= 8
        verdicts.append({
            "key": field["key"], "ok": ok, "state": "ok" if ok else "no",
            "note": ("Long enough and varied enough to be doing real work."
                     if ok else "Too thin to change what a model would produce. Say more, and be specific."),
        })
    return {"verdicts": verdicts, "score": sum(1 for v in verdicts if v["ok"]),
            "outof": len(fields), "engine": "local",
            "summary": scrub(_LOCAL_SUMMARY + (" " + note if note else ""))}


# ─────────────────────────────────────────────────────────────────────────────
# KIND. SINGLE — one box, one verdict per component the marker finds inside it
# ─────────────────────────────────────────────────────────────────────────────

def grade_single(spec: dict, payload: dict) -> dict:
    text = str(payload.get("text", "")).strip()
    components = spec["components"]
    keys = [c["key"] for c in components]

    if not text:
        return _finish(spec, {
            "verdicts": [{"key": k, "ok": False, "state": "no", "note": "Nothing written yet."}
                         for k in keys],
            "score": 0, "outof": len(keys), "engine": "local",
            "summary": "The box is empty.",
        })

    if _key_ready():
        try:
            lines = [f"EXERCISE. {spec['title']}", f"WHAT THE LEARNER WAS ASKED. {spec['intro']}"]
            if spec.get("sample"):
                lines.append(f"THE MATERIAL THEY WERE GIVEN. {spec['sample']}")
            lines.append("")
            lines.append("The learner wrote one passage. Decide, for each component listed "
                         "below, whether that component is genuinely present inside their "
                         "passage and specific enough to matter. Presence of a related word is "
                         "not presence of the component.")
            lines.append("")
            lines.append("COMPONENTS TO LOOK FOR")
            for component in components:
                optional = " This one is optional." if component.get("optional") else ""
                lines.append(f"  key {component['key']}. {component['label']}.{optional}")
            lines.append("")
            lines.append("WHAT THE LEARNER WROTE")
            lines.append(text)
            lines.append("")
            lines.append("Return exactly one verdict for each component key.")
            raw = _ask("\n".join(lines))
            result = _shape(raw, keys, "claude")
        except Exception as exc:
            result = _local_single(components, text, str(exc))
    else:
        result = _local_single(components, text)
    return _finish(spec, result)


_CUE_WORDS = {
    "goal": ("goal", "need", "trying", "aim", "so that", "for my", "purpose"),
    "task": ("write", "list", "outline", "produce", "generate", "sections", "words", "bullet", "give me"),
    "constraints": ("only", "must", "do not", "cite", "source", "verify", "link", "peer reviewed", "flag"),
    "context": ("i have", "my class", "we have", "background", "already", "context", "audience"),
    "role": ("act as", "you are", "as a", "role"),
    "examples": ("for example", "here is", "attached", "like this", "sample"),
    "tools": ("chatgpt", "claude", "gemini", "copilot", "perplexity", "assistant", "model"),
    "purpose": ("brainstorm", "grammar", "edit", "analysis", "outline", "format", "citation"),
    "extent": ("minimal", "moderate", "extensive", "limited", "light", "heavy"),
    "oversight": ("verified", "reviewed", "checked", "responsib", "author", "i take"),
}


def _local_single(components: list[dict], text: str, note: str = "") -> dict:
    low = text.lower()
    verdicts = []
    for component in components:
        cues = _CUE_WORDS.get(component["key"], ())
        ok = any(cue in low for cue in cues) and len(text.split()) >= 15
        verdicts.append({
            "key": component["key"], "ok": ok, "state": "ok" if ok else "no",
            "note": ("Found language that does this job." if ok
                     else "Nothing in the passage does this job yet."),
        })
    return {"verdicts": verdicts, "score": sum(1 for v in verdicts if v["ok"]),
            "outof": len(components), "engine": "local",
            "summary": scrub(_LOCAL_SUMMARY + (" " + note if note else ""))}


# ─────────────────────────────────────────────────────────────────────────────
# KIND. CHECKS — a checklist over a sample, plus a written finding
# ─────────────────────────────────────────────────────────────────────────────

def grade_checks(spec: dict, payload: dict) -> dict:
    ticked = set(payload.get("ticked") or [])
    note = str(payload.get("note", "")).strip()
    items = spec["items"]
    keys = [i["key"] for i in items] + ["note"]

    if _key_ready():
        try:
            lines = [f"EXERCISE. {spec['title']}", f"WHAT THE LEARNER WAS ASKED. {spec['intro']}",
                     "", f"THE MATERIAL UNDER REVIEW.", spec["sample"], "",
                     "For each checklist statement below, decide whether the learner made the "
                     "right call. Set ok to true when their decision was correct, whether they "
                     "ticked a statement that is genuinely true of the material or correctly "
                     "left alone a statement that is not true of it. Set ok to false otherwise, "
                     "and in the note say plainly whether the statement is true of the material.",
                     ""]
            for item in items:
                decision = "ticked it" if item["key"] in ticked else "left it alone"
                lines.append(f"  key {item['key']}. Statement. {item['text']} The learner {decision}.")
            lines.append("")
            lines.append(f"FINALLY, key note. The learner was asked. {spec['note_label']}.")
            lines.append(f"They wrote. {note or 'nothing at all'}")
            lines.append("Approve the note only when it names something specific about this "
                         "material rather than a general principle.")
            lines.append("")
            lines.append("Return one verdict for every key listed, including note.")
            raw = _ask("\n".join(lines), max_tokens=5000)
            result = _shape(raw, keys, "claude")
        except Exception as exc:
            result = _local_checks(items, ticked, note, str(exc))
    else:
        result = _local_checks(items, ticked, note)
    return _finish(spec, result)


def _local_checks(items: list[dict], ticked: set, note: str, err: str = "") -> dict:
    verdicts = []
    for item in items:
        truth = bool(item.get("answer"))
        ok = (item["key"] in ticked) == truth
        verdicts.append({
            "key": item["key"], "ok": ok, "state": "ok" if ok else "no",
            "note": ("Right call." if ok else
                     ("That one is true of this material and you left it alone." if truth
                      else "That one is not true of this material.")),
        })
    note_ok = len(note.split()) >= 8
    verdicts.append({"key": "note", "ok": note_ok, "state": "ok" if note_ok else "no",
                     "note": "Specific enough to act on." if note_ok else "Say more here."})
    return {"verdicts": verdicts, "score": sum(1 for v in verdicts if v["ok"]),
            "outof": len(verdicts), "engine": "local",
            "summary": scrub(_LOCAL_SUMMARY + (" " + err if err else ""))}


# ─────────────────────────────────────────────────────────────────────────────
# KIND. HUNT — click the sentences that need verifying
# ─────────────────────────────────────────────────────────────────────────────

def grade_hunt(spec: dict, payload: dict) -> dict:
    picked = set(payload.get("picked") or [])
    note = str(payload.get("note", "")).strip()
    sentences = spec["sentences"]
    keys = [s["key"] for s in sentences] + ["note"]

    if _key_ready():
        try:
            lines = [f"EXERCISE. {spec['title']}", f"WHAT THE LEARNER WAS ASKED. {spec['intro']}",
                     "",
                     "A sentence needs verifying when it carries a specific checkable factual "
                     "claim, such as a precise date, a precise figure, a named study or a "
                     "strong assertion about what happened. A sentence that is general, hedged "
                     "or interpretive does not need verifying in the same way. For each "
                     "sentence, set ok to true when the learner made the right call.",
                     ""]
            for sentence in sentences:
                decision = "flagged it" if sentence["key"] in picked else "left it alone"
                lines.append(f"  key {sentence['key']}. Sentence. {sentence['text']} The learner {decision}.")
            lines.append("")
            lines.append(f"FINALLY, key note. The learner was asked. {spec['note_label']}.")
            lines.append(f"They wrote. {note or 'nothing at all'}")
            lines.append("")
            lines.append("Return one verdict for every key listed, including note.")
            raw = _ask("\n".join(lines), max_tokens=4000)
            result = _shape(raw, keys, "claude")
        except Exception as exc:
            result = _local_hunt(sentences, picked, note, str(exc))
    else:
        result = _local_hunt(sentences, picked, note)
    return _finish(spec, result)


def _local_hunt(sentences: list[dict], picked: set, note: str, err: str = "") -> dict:
    verdicts = []
    for sentence in sentences:
        truth = bool(sentence.get("answer"))
        ok = (sentence["key"] in picked) == truth
        verdicts.append({
            "key": sentence["key"], "ok": ok, "state": "ok" if ok else "no",
            "note": ("Right call." if ok else
                     ("That sentence carries a checkable claim and you passed over it." if truth
                      else "That sentence is general enough not to need its own check.")),
        })
    note_ok = len(note.split()) >= 8
    verdicts.append({"key": "note", "ok": note_ok, "state": "ok" if note_ok else "no",
                     "note": "Clear reasoning." if note_ok else "Say more here."})
    return {"verdicts": verdicts, "score": sum(1 for v in verdicts if v["ok"]),
            "outof": len(verdicts), "engine": "local",
            "summary": scrub(_LOCAL_SUMMARY + (" " + err if err else ""))}


# ─────────────────────────────────────────────────────────────────────────────
# KIND. SORTER — drop each item into a bucket
# ─────────────────────────────────────────────────────────────────────────────

def grade_sorter(spec: dict, payload: dict) -> dict:
    placement = payload.get("placement") or {}
    items = spec["items"]
    buckets = {b["key"]: b["label"] for b in spec["buckets"]}
    keys = [i["key"] for i in items]
    pass_mark = spec.get("pass_mark", max(1, len(items) - 1))

    if _key_ready():
        try:
            lines = [f"EXERCISE. {spec['title']}", f"WHAT THE LEARNER WAS ASKED. {spec['intro']}",
                     "", "THE BUCKETS AVAILABLE."]
            for bucket in spec["buckets"]:
                lines.append(f"  {bucket['key']}. {bucket['label']}")
            lines.append("")
            lines.append("For each situation below, set ok to true when the bucket the learner "
                         "chose is defensible, and false when it is not. In the note for a wrong "
                         "call, name the bucket it belongs in and give the one reason why.")
            lines.append("")
            for item in items:
                chosen = placement.get(item["key"])
                where = buckets.get(chosen, "nothing at all")
                lines.append(f"  key {item['key']}. Situation. {item['text']} The learner chose {where}.")
            lines.append("")
            lines.append("Return one verdict for every key listed.")
            raw = _ask("\n".join(lines), max_tokens=5000)
            result = _shape(raw, keys, "claude")
        except Exception as exc:
            result = _local_sorter(items, placement, buckets, str(exc))
    else:
        result = _local_sorter(items, placement, buckets)
    spec = dict(spec, pass_mark=pass_mark)
    return _finish(spec, result)


def _local_sorter(items: list[dict], placement: dict, buckets: dict, err: str = "") -> dict:
    verdicts = []
    for item in items:
        truth = item.get("answer")
        chosen = placement.get(item["key"])
        ok = chosen == truth
        verdicts.append({
            "key": item["key"], "ok": ok, "state": "ok" if ok else "no",
            "note": ("Right call." if ok
                     else f"That one belongs under {buckets.get(truth, 'another bucket')}."),
        })
    return {"verdicts": verdicts, "score": sum(1 for v in verdicts if v["ok"]),
            "outof": len(items), "engine": "local",
            "summary": scrub(_LOCAL_SUMMARY + (" " + err if err else ""))}


# ─────────────────────────────────────────────────────────────────────────────
# KIND. MATCH and KIND. ORDER — marked here, with no model involved
# ─────────────────────────────────────────────────────────────────────────────

def grade_match(spec: dict, payload: dict) -> dict:
    chosen = payload.get("chosen") or {}
    options = spec["options"]
    verdicts = []
    for item in spec["items"]:
        picked = chosen.get(item["key"])
        ok = picked is not None and int(picked) == item["answer"]
        verdicts.append({
            "key": item["key"], "ok": ok, "state": "ok" if ok else "no",
            "note": ("Right family." if ok
                     else f"That job wants {options[item['answer']].lower()}."),
        })
    score = sum(1 for v in verdicts if v["ok"])
    return _finish(spec, {
        "verdicts": verdicts, "score": score, "outof": len(verdicts), "engine": "rules",
        "summary": ("Every job matched." if score == len(verdicts)
                    else "Read the red rows and try those again."),
    })


def grade_order(spec: dict, payload: dict) -> dict:
    given = list(payload.get("order") or [])
    correct = spec["order"]
    by_key = {s["key"]: s for s in spec["steps"]}
    verdicts = []
    for position, key in enumerate(correct):
        ok = position < len(given) and given[position] == key
        step = by_key[key]
        verdicts.append({
            "key": key, "ok": ok, "state": "ok" if ok else "no",
            "note": (f"Turn {position + 1} is right." if ok
                     else f"Turn {position + 1} belongs to {step['who'].lower()}."),
        })
    score = sum(1 for v in verdicts if v["ok"])
    spec = dict(spec, pass_mark=len(correct))
    return _finish(spec, {
        "verdicts": verdicts, "score": score, "outof": len(correct), "engine": "rules",
        "summary": ("The whole loop is in order." if score == len(correct)
                    else "Some turns are in the wrong hands. Look at where your first draft sits."),
    })


# ─────────────────────────────────────────────────────────────────────────────
# DISPATCH
# ─────────────────────────────────────────────────────────────────────────────

BY_KIND = {
    "fields": grade_fields,
    "single": grade_single,
    "checks": grade_checks,
    "hunt": grade_hunt,
    "sorter": grade_sorter,
    "match": grade_match,
    "order": grade_order,
}


def grade(exercise_id: str, payload: dict[str, Any]) -> dict:
    spec = curriculum.EXERCISES.get(exercise_id)
    if not spec:
        raise KeyError(exercise_id)
    return BY_KIND[spec["kind"]](spec, payload or {})
