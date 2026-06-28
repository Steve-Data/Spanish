#!/usr/bin/env python3
"""Generate a daily Spanish reinforcement list from Anki review history."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import random
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ANKI_URL = "http://raspberrypi:8765"
DECK = "1. Sentence Mining"
TODAY_FILE = "today.md"
HISTORY_FILE = "data/reinforcement-history.json"
ARCHIVE_DIR = "archive"


TAG_RE = re.compile(r"<[^>]+>")
SOUND_RE = re.compile(r"\[sound:[^\]]+\]", re.IGNORECASE)
SPACE_RE = re.compile(r"\s+")
BRACKET_RE = re.compile(r"^(.*?)\s*\[([^\]]+)\]\s*$")
MISS_REASON_RE = re.compile(r"^(\d+) lifetime misses?$")
LAPSE_REASON_RE = re.compile(r"^(\d+) lapses?$")
SHORT_INTERVAL_REASON_RE = re.compile(r"^short (\d+)d interval$")
INTERVAL_REASON_RE = re.compile(r"^(\d+)d interval$")
DAY_RE = re.compile(r"(\d+)d")


def anki(action: str, params: dict[str, Any] | None = None, url: str = ANKI_URL) -> Any:
    payload = {"action": action, "version": 6}
    if params is not None:
        payload["params"] = params

    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not connect to AnkiConnect at {url}: {exc}") from exc

    if result.get("error"):
        raise RuntimeError(f"AnkiConnect error for {action}: {result['error']}")
    return result.get("result")


def chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = SOUND_RE.sub("", text)
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    return SPACE_RE.sub(" ", text).strip()


def field_value(fields: dict[str, Any], names: list[str]) -> str:
    lower_map = {name.lower(): value for name, value in fields.items()}
    for name in names:
        value = lower_map.get(name.lower())
        if isinstance(value, dict):
            return clean_text(value.get("value", ""))
        if value is not None:
            return clean_text(value)
    return ""


def display_target(raw_target: str) -> tuple[str, str]:
    match = BRACKET_RE.match(raw_target)
    if not match:
        return raw_target.strip(), ""
    target = match.group(1).strip()
    lemma = match.group(2).strip()
    return target or raw_target.strip(), lemma


def recent_days(review_id_ms: int, today: dt.date) -> int:
    review_date = dt.datetime.fromtimestamp(review_id_ms / 1000).date()
    return max(0, (today - review_date).days)


def score_card(card: dict[str, Any], reviews: list[dict[str, Any]], today: dt.date) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    relevant_reviews = [
        review
        for review in reviews
        if review.get("type") != 4 and review.get("ease") is not None
    ]
    misses = [review for review in relevant_reviews if review.get("ease") == 1]

    if not relevant_reviews and int(card.get("reps") or 0) == 0:
        return 0.0, ["new or unreviewed"]

    if misses:
        score += min(len(misses) * 2.0, 10.0)
        reasons.append(f"{len(misses)} lifetime miss{'es' if len(misses) != 1 else ''}")

    recent_misses = []
    for review in misses:
        days = recent_days(int(review["id"]), today)
        if days <= 7:
            score += 12.0
            recent_misses.append(f"{days}d")
        elif days <= 30:
            score += 7.0
            recent_misses.append(f"{days}d")
        elif days <= 90:
            score += 3.0
    if recent_misses:
        reasons.append("recent miss " + ", ".join(recent_misses[:3]))

    lapses = int(card.get("lapses") or 0)
    if lapses:
        score += min(lapses * 3.0, 12.0)
        reasons.append(f"{lapses} lapse{'s' if lapses != 1 else ''}")

    interval = int(card.get("interval") or 0)
    reps = int(card.get("reps") or 0)
    if reps >= 4 and 0 < interval <= 14:
        score += 5.0
        reasons.append(f"short {interval}d interval")
    elif reps >= 4 and interval <= 30:
        score += 3.0
        reasons.append(f"{interval}d interval")
    elif reps >= 8 and interval <= 90:
        score += 1.5

    if reps:
        score += min(math.log1p(reps), 3.0)

    if int(card.get("queue") or 0) in (1, 3):
        score += 2.0
        reasons.append("learning/relearning")

    if not reasons:
        reasons.append("moderate review history")

    return score, reasons


def target_from_card(card: dict[str, Any]) -> tuple[str, str, str]:
    fields = card.get("fields") or {}
    raw_target = field_value(fields, ["Word", "Front"])
    definition = field_value(fields, ["Word Definition", "Back"])
    sentence = field_value(fields, ["Sentence"])
    return raw_target, definition, sentence


def summarize_reasons(reasons: list[str]) -> list[str]:
    misses = 0
    lapses = 0
    recent_days_seen: list[int] = []
    short_intervals: list[int] = []
    intervals: list[int] = []
    learning = False
    moderate = False
    other: list[str] = []

    for reason in reasons:
        if match := MISS_REASON_RE.match(reason):
            misses += int(match.group(1))
        elif reason.startswith("recent miss"):
            recent_days_seen.extend(int(day) for day in DAY_RE.findall(reason))
        elif match := LAPSE_REASON_RE.match(reason):
            lapses += int(match.group(1))
        elif match := SHORT_INTERVAL_REASON_RE.match(reason):
            short_intervals.append(int(match.group(1)))
        elif match := INTERVAL_REASON_RE.match(reason):
            intervals.append(int(match.group(1)))
        elif reason == "learning/relearning":
            learning = True
        elif reason == "moderate review history":
            moderate = True
        elif reason not in other:
            other.append(reason)

    summary: list[str] = []
    if misses:
        summary.append(f"{misses} lifetime miss{'es' if misses != 1 else ''}")
    if recent_days_seen:
        recent = ", ".join(f"{day}d" for day in sorted(recent_days_seen)[:3])
        summary.append(f"recent miss {recent}")
    if lapses:
        summary.append(f"{lapses} lapse{'s' if lapses != 1 else ''}")
    if short_intervals:
        summary.append(f"short {min(short_intervals)}d interval")
    elif intervals:
        summary.append(f"{min(intervals)}d interval")
    if learning:
        summary.append("learning/relearning")
    summary.extend(other)
    if not summary and moderate:
        summary.append("moderate review history")
    return summary[:4]


def load_history(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"days": []}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"days": []}
    if not isinstance(data, dict):
        return {"days": []}
    data.setdefault("days", [])
    return data


def recent_history_counts(history: dict[str, Any], today: dt.date) -> dict[str, int]:
    counts: dict[str, int] = {}
    for day in history.get("days", []):
        try:
            day_date = dt.date.fromisoformat(str(day.get("date")))
        except ValueError:
            continue
        age = (today - day_date).days
        if age <= 0 or age > 7:
            continue
        for item in day.get("targets", []):
            target = str(item).strip()
            if target:
                counts[target.lower()] = max(counts.get(target.lower(), 0), age)
    return counts


def pick_candidates(
    candidates: list[dict[str, Any]],
    count: int,
    today: dt.date,
    history: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates = sorted(candidates, key=lambda item: item["score"], reverse=True)
    pool = candidates[: max(count * 8, 60)]
    if len(pool) <= count:
        return pool

    recent_counts = recent_history_counts(history, today)
    rng = random.Random(today.isoformat())
    selected: list[dict[str, Any]] = []
    selected_notes: set[int] = set()

    recent_miss_items = [
        item
        for item in pool
        if any("recent miss" in reason for reason in item.get("reasons", []))
    ]
    for item in sorted(recent_miss_items, key=lambda row: row["score"], reverse=True)[:3]:
        selected.append(item)
        selected_notes.add(item["note_id"])

    while len(selected) < count:
        weighted: list[tuple[dict[str, Any], float]] = []
        for item in pool:
            if item["note_id"] in selected_notes:
                continue
            weight = max(item["score"], 1.0)
            recent_age = recent_counts.get(item["target"].lower())
            if recent_age is not None:
                if recent_age <= 1:
                    weight *= 0.20
                elif recent_age <= 3:
                    weight *= 0.35
                else:
                    weight *= 0.60
            weighted.append((item, weight))

        if not weighted:
            break

        total = sum(weight for _, weight in weighted)
        draw = rng.uniform(0, total)
        cumulative = 0.0
        choice = weighted[-1][0]
        for item, weight in weighted:
            cumulative += weight
            if cumulative >= draw:
                choice = item
                break
        selected.append(choice)
        selected_notes.add(choice["note_id"])

    return sorted(selected, key=lambda item: item["score"], reverse=True)


def build_candidates(anki_url: str, deck: str, today: dt.date) -> list[dict[str, Any]]:
    query = f'deck:"{deck}" -is:suspended'
    card_ids = anki("findCards", {"query": query}, anki_url)
    if not card_ids:
        return []

    cards: list[dict[str, Any]] = []
    for chunk in chunks(card_ids, 500):
        cards.extend(anki("cardsInfo", {"cards": chunk}, anki_url))

    reviews_by_card: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks(card_ids, 500):
        chunk_reviews = anki("getReviewsOfCards", {"cards": chunk}, anki_url)
        reviews_by_card.update({str(key): value for key, value in chunk_reviews.items()})

    notes: dict[int, dict[str, Any]] = {}
    for card in cards:
        note_id = int(card["note"])
        raw_target, definition, sentence = target_from_card(card)
        target, lemma = display_target(raw_target)
        if not target or not definition:
            continue

        score, reasons = score_card(card, reviews_by_card.get(str(card["cardId"]), []), today)
        if score <= 0:
            continue

        note = notes.setdefault(
            note_id,
            {
                "note_id": note_id,
                "target": target,
                "lemma": lemma,
                "raw_target": raw_target,
                "definition": definition,
                "sentence": sentence,
                "score": 0.0,
                "reasons": [],
                "cards": 0,
            },
        )
        note["score"] += score
        note["cards"] += 1
        note["reasons"].extend(reasons)
        if len(definition) > len(note.get("definition", "")):
            note["definition"] = definition
        if sentence and not note.get("sentence"):
            note["sentence"] = sentence

    candidates = list(notes.values())
    for item in candidates:
        item["score"] = round(float(item["score"]), 2)
        item["reasons"] = summarize_reasons(item["reasons"])
    return candidates


def render_markdown(selected: list[dict[str, Any]], today: dt.date) -> str:
    lines = [
        "# Spanish Reinforcement Words",
        "",
        f"Updated: {today.isoformat()}",
        "",
        "Use these exact Spanish targets in today's Spanish practice. Work them naturally into questions, mini-dialogues, and correction exercises.",
        "",
    ]

    for index, item in enumerate(selected, start=1):
        definition = item["definition"].rstrip(".")
        lines.append(f"{index}. **{item['target']}** - {definition}")

    lines.extend(
        [
            "",
            "## Context",
            "",
            "These were selected from Anki review history, with priority for recent misses, repeated lapses, and short intervals.",
            "",
        ]
    )

    for index, item in enumerate(selected, start=1):
        reasons = "; ".join(item.get("reasons", []))
        lines.append(f"{index}. **{item['target']}**")
        if item.get("lemma"):
            lines.append(f"   - Base form: {item['lemma']}")
        lines.append(f"   - Why selected: {reasons}")
        if item.get("sentence"):
            lines.append(f"   - Anki sentence: {item['sentence']}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def update_history(history: dict[str, Any], selected: list[dict[str, Any]], today: dt.date) -> dict[str, Any]:
    days = [
        day
        for day in history.get("days", [])
        if str(day.get("date")) != today.isoformat()
    ]
    days.append(
        {
            "date": today.isoformat(),
            "targets": [item["target"] for item in selected],
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
    )
    days = sorted(days, key=lambda day: str(day.get("date", "")))[-60:]
    history["days"] = days
    return history


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--anki-url", default=ANKI_URL)
    parser.add_argument("--deck", default=DECK)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--date", default=dt.date.today().isoformat())
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    today = dt.date.fromisoformat(args.date)
    history_path = root / HISTORY_FILE
    archive_dir = root / ARCHIVE_DIR
    today_path = root / TODAY_FILE

    history = load_history(history_path)
    candidates = build_candidates(args.anki_url, args.deck, today)
    if not candidates:
        raise RuntimeError("No eligible Anki candidates found.")

    selected = pick_candidates(candidates, args.count, today, history)
    if not selected:
        raise RuntimeError("No reinforcement words selected.")

    markdown = render_markdown(selected, today)

    archive_dir.mkdir(parents=True, exist_ok=True)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    today_path.write_text(markdown, encoding="utf-8")
    (archive_dir / f"{today.isoformat()}.md").write_text(markdown, encoding="utf-8")

    history = update_history(history, selected, today)
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Wrote {today_path}")
    print(f"Selected: {', '.join(item['target'] for item in selected)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
