#!/usr/bin/env python3
"""Summarize Messenger exports with jq and create eight comparative charts.

Python 3.10+; plotting requires matplotlib and numpy. jq is needed only for raw
exports. Run --help for usage, or --print-jq to inspect the embedded summary.
No message text is sent to an external service.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import calendar
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile

VERSION = "1.1.0"

# The supplied jq program, with shell quoting removed. The apostrophe in the
# regex is literal: Python invokes jq directly, never through a shell.
JQ_PROGRAM = r'''def words:
  [.messages[]?.text // ""
    | [scan("[[:alnum:]'’]+")] | length
  ] | add // 0;

def person_stats($p):
  .messages as $m |
  [$m[] | select(.senderName == $p)] as $pm |
  {
    name: $p,
    messages: ($pm | length),
    text_messages: ([$pm[] | select(.type == "text")] | length),
    media_messages: ([$pm[] | select(.type == "media")] | length),
    words: ([$pm[].text // "" | [scan("[[:alnum:]'’]+")] | length] | add // 0),
    characters: ([$pm[].text // "" | length] | add // 0),
    reactions_given: ([$m[].reactions[]? | select(.actor == $p)] | length)
  };

def thread_summary:
  .messages as $m |
  {
    thread_name: .threadName,
    participants: .participants,
    total_messages: ($m | length),
    first_timestamp: ($m | map(.timestamp) | min // null),
    last_timestamp: ($m | map(.timestamp) | max // null),
    first_date: (if ($m | length) > 0 then (($m | map(.timestamp) | min) / 1000 | strftime("%Y-%m-%d")) else null end),
    last_date: (if ($m | length) > 0 then (($m | map(.timestamp) | max) / 1000 | strftime("%Y-%m-%d")) else null end),
    span_days: (if ($m | length) > 1 then ((($m | map(.timestamp) | max) - ($m | map(.timestamp) | min)) / 86400000) else 0 end),
    messages_per_day: (if ($m | length) > 1 then (($m | length) / (((($m | map(.timestamp) | max) - ($m | map(.timestamp) | min)) / 86400000) + 1)) else ($m | length) end),
    text_messages: ([$m[] | select(.type == "text")] | length),
    media_messages: ([$m[] | select(.type == "media")] | length),
    messages_with_media: ([$m[] | select((.media // [] | length) > 0)] | length),
    unsent_messages: ([$m[] | select(.isUnsent == true)] | length),
    total_reactions: ([$m[].reactions[]?] | length),
    total_words: ([$m[].text // "" | [scan("[[:alnum:]'’]+")] | length] | add // 0),
    total_characters: ([$m[].text // "" | length] | add // 0),
    people: [.participants[] as $p | person_stats($p)],
    reaction_types: ([$m[].reactions[]?.reaction] | sort | group_by(.) | map({reaction: .[0], count: length}) | sort_by(-.count)),
    message_types: ([$m[].type] | sort | group_by(.) | map({type: .[0], count: length}) | sort_by(-.count))
  };

map(thread_summary) as $threads |
{
  overall: {
    conversations: ($threads | length),
    total_messages: ([$threads[].total_messages] | add // 0),
    total_words: ([$threads[].total_words] | add // 0),
    total_media_messages: ([$threads[].media_messages] | add // 0),
    total_reactions: ([$threads[].total_reactions] | add // 0),
    total_unsent_messages: ([$threads[].unsent_messages] | add // 0),
    earliest_timestamp: ([$threads[].first_timestamp | select(. != null)] | min // null),
    latest_timestamp: ([$threads[].last_timestamp | select(. != null)] | max // null),
    earliest_date: ([$threads[].first_timestamp | select(. != null)] | if length > 0 then (min / 1000 | strftime("%Y-%m-%d")) else null end),
    latest_date: ([$threads[].last_timestamp | select(. != null)] | if length > 0 then (max / 1000 | strftime("%Y-%m-%d")) else null end),
    messages_by_sender: ([$threads[].people[]] | sort_by(.name) | group_by(.name) | map({name: .[0].name, messages: ([.[].messages] | add), words: ([.[].words] | add), media_messages: ([.[].media_messages] | add), reactions_given: ([.[].reactions_given] | add)}) | sort_by(-.messages)),
    largest_conversations: ($threads | map({thread_name, participants, total_messages, total_words, first_date, last_date, span_days, messages_per_day}) | sort_by(-.total_messages))
  },
  threads: ($threads | sort_by(-.total_messages))
}
'''

class InputError(ValueError):
    """A user-correctable input or configuration problem."""


def read_json(path: Path):
    try:
        with path.open(encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise InputError(f"Cannot read JSON in {path}: {e}") from e


def is_summary(obj):
    return isinstance(obj, dict) and isinstance(obj.get("overall"), dict) and isinstance(obj.get("threads"), list)


def raw_shape(obj):
    return isinstance(obj, dict) and "messages" in obj and "participants" in obj and "threadName" in obj


def validate_raw(obj, path):
    if not isinstance(obj["threadName"], str) or not isinstance(obj["participants"], list) or not all(isinstance(p, str) for p in obj["participants"]):
        raise InputError(f"{path}: expected a threadName string and a participants list of names.")
    if not isinstance(obj["messages"], list):
        raise InputError(f"{path}: messages must be a list.")
    for i, m in enumerate(obj["messages"]):
        if not isinstance(m, dict) or not finite(m.get("timestamp")):
            raise InputError(f"{path}: message {i} needs a numeric millisecond timestamp.")
        if m.get("text") is not None and not isinstance(m["text"], str):
            raise InputError(f"{path}: message {i} text must be a string or null.")
        if not isinstance(m.get("senderName"), str):
            raise InputError(f"{path}: message {i} needs a senderName string.")
        for field in ("media", "reactions"):
            if m.get(field) is not None and not isinstance(m[field], list):
                raise InputError(f"{path}: message {i} {field} must be a list or null.")


def discover(path: Path, recursive=False, exclude_dir=None):
    """Return either a summary object or validated raw file paths, not both."""
    if not path.exists():
        raise InputError(f"Input does not exist: {path}")
    if path.is_file():
        obj = read_json(path)
        if is_summary(obj):
            return obj, [], []
        if not raw_shape(obj):
            raise InputError(f"{path}: not a Messenger thread or jq summary file.")
        validate_raw(obj, path)
        return None, [path], []
    raw, skipped = [], []
    candidates = path.rglob("*.json") if recursive else path.glob("*.json")
    for candidate in sorted(candidates):
        resolved = candidate.resolve()
        if exclude_dir and exclude_dir != path and resolved.is_relative_to(exclude_dir):
            continue
        obj = read_json(candidate)
        if raw_shape(obj):
            validate_raw(obj, candidate)
            raw.append(candidate)
        else:
            skipped.append(str(candidate))
    if not raw:
        raise InputError(f"No raw conversation JSON files found in {path}. To plot a summary, pass its file path instead of the folder.")
    return None, raw, skipped


FILTER_PROGRAM = 'map(.messages |= map(select(($start == null or .timestamp >= $start) and ($end == null or .timestamp < $end)))) |\n'


def summarize(raw_files, jq="jq", start_ms=None, end_ms=None, include_groups=True):
    exe = shutil.which(jq)
    if not exe:
        raise InputError("jq was not found. Install jq (macOS: brew install jq), or pass an existing chat_stats JSON file.")
    program = JQ_PROGRAM
    command = [exe, "-s"]
    if start_ms is not None or end_ms is not None:
        command += ["--argjson", "start", json.dumps(start_ms), "--argjson", "end", json.dumps(end_ms)]
        program = FILTER_PROGRAM + program
    if not include_groups:
        program = "map(select((.participants | unique | length) <= 2)) |\n" + program
    # Spooling avoids command-line length limits and stderr/stdout pipe deadlocks.
    # jq -s still holds the complete dataset in memory, as in the supplied script.
    with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as output:
        for path in raw_files:
            with path.open("rb") as f:
                # Strip a possible UTF-8 BOM without otherwise changing input.
                prefix = f.read(3)
                source.write(prefix[3:] if prefix == b"\xef\xbb\xbf" else prefix)
                shutil.copyfileobj(f, source)
            source.write(b"\n")
        source.seek(0)
        proc = subprocess.run(command + [program], stdin=source, stdout=output, stderr=subprocess.PIPE, check=False)
        if proc.returncode:
            raise InputError("jq summary failed:\n" + proc.stderr.decode("utf-8", "replace").strip())
        output.seek(0)
        return json.load(output)


def scope_summary(summary, include_groups):
    """Apply the same thread selection to cached summaries without needing jq."""
    if include_groups:
        return summary
    # Validate the original totals before rebuilding them, so filtering cannot
    # conceal an inconsistent input summary. A sentinel owner needs no inference.
    prepare(summary, "Input summary", "")
    kept = [t for t in summary["threads"] if len(set(t["participants"])) <= 2]
    if len(kept) == len(summary["threads"]):
        return summary
    overall = dict(summary["overall"])
    overall["conversations"] = len(kept)
    for total, field in [("total_messages","total_messages"),("total_words","total_words"),("total_media_messages","media_messages"),("total_reactions","total_reactions"),("total_unsent_messages","unsent_messages")]:
        overall[total] = sum(t[field] for t in kept)
    for prefix, field, fn in [("earliest","first_timestamp",min),("latest","last_timestamp",max)]:
        values = [t[field] for t in kept if t[field] is not None]
        stamp = fn(values) if values else None
        overall[prefix+"_timestamp"] = stamp
        overall[prefix+"_date"] = datetime.fromtimestamp(stamp/1000,timezone.utc).strftime("%Y-%m-%d") if stamp is not None else None
    people = {}
    for t in kept:
        for p in t["people"]:
            person = people.setdefault(p["name"],dict(name=p["name"],messages=0,words=0,media_messages=0,reactions_given=0))
            for field in ("messages","words","media_messages","reactions_given"):
                person[field] += p[field]
    overall["messages_by_sender"] = sorted(people.values(),key=lambda p:(-p["messages"],p["name"]))
    fields = ("thread_name","participants","total_messages","total_words","first_date","last_date","span_days","messages_per_day")
    overall["largest_conversations"] = [{f:t[f] for f in fields} for t in kept]
    return dict(overall=overall,threads=kept)


def finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def count(x, context):
    if not finite(x) or x < 0 or int(x) != x:
        raise InputError(f"{context}: expected a nonnegative integer, got {x!r}.")
    return int(x)


def ratio(n, d, scale=1):
    return scale * n / d if d else None


def fmt(value, digits=1, suffix=""):
    return "n/a" if value is None else f"{value:.{digits}f}{suffix}"


def normalize_key(t):
    return re.sub(r"_\d+$", "", t["thread_name"]), tuple(sorted(set(t["participants"])))


def infer_owner(summaries):
    names = Counter()
    for summary in summaries:
        for t in summary.get("threads", []):
            if t.get("total_messages", 0) > 0:
                names.update(set(t.get("participants", [])))
    ranked = names.most_common()
    if not ranked or (len(ranked) > 1 and ranked[0][1] == ranked[1][1]):
        raise InputError("Could not unambiguously identify your name. Supply --me 'Your Exact Name'.")
    return ranked[0][0]


@dataclass
class Dataset:
    label: str
    summary: dict
    rows: list
    by_key: dict
    direct: list
    aggregate: dict
    warnings: list
    timeframe: str = ""

    @property
    def active(self):
        return [r for r in self.rows if r["total_messages"]]

    @property
    def dates(self):
        values = [r for r in self.active if r.get("first_timestamp") is not None]
        if not values:
            return "no dated messages"
        def date(ms):
            return datetime.fromtimestamp(ms/1000, timezone.utc).strftime("%d %b %Y")
        return f"{date(min(r['first_timestamp'] for r in values))} - {date(max(r['last_timestamp'] for r in values))} UTC"


def prepare(summary, label, owner):
    if not is_summary(summary):
        raise InputError(f"{label}: expected a jq summary with overall and threads.")
    rows, warnings, by_key = [], [], {}
    required_counts = ("total_messages", "total_words", "media_messages", "messages_with_media", "total_reactions", "unsent_messages", "text_messages", "total_characters")
    for t in summary["threads"]:
        if not isinstance(t, dict) or not isinstance(t.get("thread_name"), str):
            raise InputError(f"{label}: every thread needs a thread_name string.")
        name = t["thread_name"]
        if not isinstance(t.get("participants"), list) or not all(isinstance(p, str) for p in t["participants"]):
            raise InputError(f"{label} / {name}: participants must be a list of names.")
        r = dict(t)
        for f in required_counts:
            r[f] = count(t.get(f), f"{label} / {name} / {f}")
        k = normalize_key(t)
        if k in by_key:
            raise InputError(f"{label}: ambiguous thread identity {k[0]!r}. Two threads have the same normalized title and participants; rename them distinctly before comparing.")
        r.update(key=k, name=k[0], direct=False)
        n, w = r["total_messages"], r["total_words"]
        if not n and any(r[f] for f in required_counts if f != "total_messages"):
            raise InputError(f"{label} / {name}: an empty thread has nonzero content counts.")
        for f in ("media_messages", "messages_with_media", "unsent_messages", "text_messages"):
            if r[f] > n:
                raise InputError(f"{label} / {name}: {f} exceeds total_messages.")
        if n:
            first, last = t.get("first_timestamp"), t.get("last_timestamp")
            if not finite(first) or not finite(last) or last < first:
                raise InputError(f"{label} / {name}: invalid first/last timestamps.")
            span = (last-first)/86400000
            if not finite(t.get("span_days")) or not math.isclose(t["span_days"], span, abs_tol=1e-5):
                raise InputError(f"{label} / {name}: span_days disagrees with timestamps.")
            r["span_days"] = span
            r["messages_per_day"] = n/(span+1)
            if not finite(t.get("messages_per_day")) or not math.isclose(t["messages_per_day"], r["messages_per_day"], rel_tol=1e-5):
                raise InputError(f"{label} / {name}: messages_per_day disagrees with count and span.")
        else:
            r.update(span_days=0, messages_per_day=0)
        for f, target in (("message_types", "total_messages"), ("reaction_types", "total_reactions")):
            if f in t:
                if not isinstance(t[f], list) or any(not isinstance(x, dict) for x in t[f]):
                    raise InputError(f"{label} / {name}: {f} must be a list of count records.")
                if sum(count(x.get("count"), f"{name} / {f}") for x in t[f]) != r[target]:
                    raise InputError(f"{label} / {name}: {f} does not reconcile with {target}.")
        r.update(reaction_rate=ratio(r["total_reactions"], n, 100), media_rate=ratio(r["messages_with_media"], n, 100), wpm=ratio(w, n))
        if not isinstance(t.get("people"), list):
            raise InputError(f"{label} / {name}: people must be a list.")
        people = {}
        for person in t["people"]:
            if not isinstance(person, dict) or not isinstance(person.get("name"), str):
                raise InputError(f"{label} / {name}: invalid people record.")
            for f in ("messages", "words", "media_messages", "reactions_given"):
                count(person.get(f), f"{label} / {name} / {person['name']} / {f}")
            if person["name"] in people:
                if person != people[person["name"]]:
                    raise InputError(f"{label} / {name}: conflicting duplicate person records.")
                warnings.append(f"{name}: duplicate participant summary ignored for analysis (original jq output retained).")
            people[person["name"]] = person
        accounted = sum(p["messages"] for p in people.values()) == n and sum(p["words"] for p in people.values()) == w
        if not accounted:
            warnings.append(f"{name}: participant counts do not reconcile; excluded from directionality, retained in thread volume.")
        if len(set(t["participants"])) == 2 and owner in t["participants"] and n and accounted and set(people) == set(t["participants"]):
            me = people[owner]
            other = next(p for name, p in people.items() if name != owner)
            r.update(direct=True, my_messages=me["messages"], other_messages=other["messages"], my_words=me["words"], other_words=other["words"])
            r.update(message_share=ratio(me["messages"], n, 100), word_share=ratio(me["words"], w, 100), my_wpm=ratio(me["words"], me["messages"]), other_wpm=ratio(other["words"], other["messages"]))
        rows.append(r)
        by_key[k] = r
    rows.sort(key=lambda r: (-r["total_messages"], r["name"]))
    fields = {"total_messages":"total_messages", "total_words":"total_words", "total_media_messages":"media_messages", "total_reactions":"total_reactions", "total_unsent_messages":"unsent_messages"}
    if count(summary["overall"].get("conversations"), f"{label} / conversations") != len(rows):
        raise InputError(f"{label}: overall conversation count does not match threads.")
    for overall, field in fields.items():
        if count(summary["overall"].get(overall), f"{label} / {overall}") != sum(r[field] for r in rows):
            raise InputError(f"{label}: overall {overall} does not reconcile with threads.")
    direct = [r for r in rows if r["direct"]]
    agg = {f:sum(r[f] for r in direct) for f in ("total_messages", "total_words", "my_messages", "other_messages", "my_words", "other_words")}
    agg.update(message_share=ratio(agg["my_messages"], agg["total_messages"], 100), word_share=ratio(agg["my_words"], agg["total_words"], 100), my_wpm=ratio(agg["my_words"], agg["my_messages"]), other_wpm=ratio(agg["other_words"], agg["other_messages"]))
    return Dataset(label, summary, rows, by_key, direct, agg, warnings)


def check_nested(all_time, recent):
    """Conservative summary-level checks before taking differences."""
    if recent is None:
        return False, ["No comparison dataset supplied."]
    if not recent.active:
        return False, ["The comparison dataset has no messages."]
    reasons = []
    fields = ("total_messages", "total_words", "total_reactions", "messages_with_media", "media_messages", "unsent_messages", "text_messages", "total_characters")
    for y in recent.active:
        a = all_time.by_key.get(y["key"])
        if a is None:
            reasons.append(f"{y['name']}: no matching all-time thread.")
            continue
        if any(y[f] > a[f] for f in fields):
            reasons.append(f"{y['name']}: comparison counts exceed all-time counts.")
        if y["first_timestamp"] < (a.get("first_timestamp") or y["first_timestamp"]) or y["last_timestamp"] != a["last_timestamp"]:
            reasons.append(f"{y['name']}: timestamps do not describe a nested window with the same endpoint.")
        if y["direct"] and a["direct"] and any(y[f] > a[f] for f in ("my_messages", "other_messages", "my_words", "other_words")):
            reasons.append(f"{y['name']}: participant counts exceed all-time counts.")
    cutoff = min(r["first_timestamp"] for r in recent.active)
    for a in all_time.active:
        y = recent.by_key.get(a["key"])
        if (y is None or not y["total_messages"]) and a["last_timestamp"] >= cutoff:
            reasons.append(f"{a['name']}: all-time messages fall inside the comparison window but are absent there.")
    return not reasons, reasons


def changes(all_time, recent, mode, minimum):
    rows = []
    if mode not in ("nested", "disjoint"):
        return rows
    for a in all_time.direct:
        y = recent.by_key.get(a["key"])
        if not y or not y["direct"]:
            continue
        em = a["total_messages"]-y["total_messages"] if mode == "nested" else a["total_messages"]
        ew = a["total_words"]-y["total_words"] if mode == "nested" else a["total_words"]
        if em < minimum or y["total_messages"] < minimum or not ew or not y["total_words"]:
            continue
        mm = a["my_messages"]-y["my_messages"] if mode == "nested" else a["my_messages"]
        mw = a["my_words"]-y["my_words"] if mode == "nested" else a["my_words"]
        row = dict(name=a["name"], earlier_messages=em, recent_messages=y["total_messages"], old_m=ratio(mm, em, 100), new_m=y["message_share"], old_w=ratio(mw, ew, 100), new_w=y["word_share"])
        row["delta_w"] = row["new_w"]-row["old_w"]
        rows.append(row)
    return sorted(rows, key=lambda r: -abs(r["delta_w"]))


def select_rows(datasets, top, direct=False):
    selected, values = set(), {}
    for d in datasets:
        rows = d.direct if direct else d.active
        selected.update(r["key"] for r in rows[:top])
        for r in rows:
            values.setdefault(r["key"], r)
    return sorted((values[k] for k in selected), key=lambda r: (-r["total_messages"], r["name"]))


def date_ms(value):
    try:
        dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise argparse.ArgumentTypeError("Use a UTC date in YYYY-MM-DD format.") from e
    return int(dt.timestamp()*1000)


@dataclass
class Window:
    label: str
    start: int | None
    end: int | None  # Exclusive, always UTC midnight when specified.

    def description(self):
        def d(ms):
            return datetime.fromtimestamp(ms/1000, timezone.utc).strftime("%Y-%m-%d")
        return f"{d(self.start) if self.start is not None else 'beginning'} through {d(self.end-1) if self.end is not None else 'latest message'} (UTC)"


def period_text(start, end):
    """Format a half-open period as inclusive calendar dates for chart text."""
    if start is None or end is None or start >= end:
        return "no dated messages"
    date = lambda ms: datetime.fromtimestamp(ms/1000,timezone.utc).strftime("%d %b %Y")
    first, last = date(start), date(end-1)
    return first if first == last else f"{first} - {last}"


def period_bounds(dataset, window):
    # Explicit user-selected dates are retained even if no messages occur at
    # their edges. A completely unbounded export uses its observed day range.
    active = dataset.active
    start = window.start
    end = window.end
    if start is None and active:
        start = min(r['first_timestamp'] for r in active)//86400000*86400000
    if end is None and active:
        end = (max(r['last_timestamp'] for r in active)//86400000+1)*86400000
    return start, end


def comparison_dates(datasets, windows, mode):
    for d,w in zip(datasets,windows):
        d.timeframe = period_text(*period_bounds(d,w))
    if len(datasets)<2:
        return "no second timeframe"
    if mode == "disjoint":
        return datasets[0].timeframe
    if mode != "nested":
        return "no separate comparison period"
    start,end = period_bounds(datasets[0],windows[0])
    recent_start,recent_end = period_bounds(datasets[1],windows[1])
    if any(v is None for v in (start,end,recent_start,recent_end)):
        return "no dated comparison period"
    periods=[]
    if start < recent_start:periods.append(period_text(start,min(end,recent_start)))
    if recent_end < end:periods.append(period_text(max(start,recent_end),end))
    return " and ".join(periods) if periods else "no messages outside the selected comparison dates"


def resolve_window(spec, anchor_ms, start=None, end=None, label=None):
    if spec == "none":
        if start is not None or end is not None:
            raise InputError("A 'none' window cannot have date bounds.")
        return None
    if spec != "all" and not re.fullmatch(r"([1-9][0-9]*)([dwmy])", spec):
        raise InputError(f"Invalid window {spec!r}; use all, none, or a positive duration such as 30d, 8w, 6m, 1y.")
    if anchor_ms is None and spec != "all" and start is None:
        # An empty export has no meaningful relative date window.
        return Window(label or spec, None, None)
    anchor = datetime.fromtimestamp((end if end is not None else anchor_ms or 0)/1000, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    upper = end+86400000 if end is not None else (int((anchor+timedelta(days=1)).timestamp()*1000) if spec != "all" else None)
    lower = start
    default_label = "All-time export"
    if spec != "all":
        m = re.fullmatch(r"([1-9][0-9]*)([dwmy])", spec)
        if not m:
            raise InputError(f"Invalid window {spec!r}; use all, none, or a positive duration such as 30d, 8w, 6m, 1y.")
        n, unit = int(m[1]), m[2]
        if n > 10000:
            raise InputError("Window duration is too large.")
        if lower is None:
            if unit in "dw":
                first = anchor-timedelta(days=n*(7 if unit == "w" else 1)-1)
            else:
                months = n*(12 if unit == "y" else 1)
                index = anchor.year*12+anchor.month-1-months
                year, month0 = divmod(index, 12)
                if year < 1:
                    raise InputError("Window starts before year 1.")
                first = anchor.replace(year=year, month=month0+1, day=min(anchor.day, calendar.monthrange(year, month0+1)[1]))
            lower = int(first.timestamp()*1000)
        units = {"d":"day", "w":"week", "m":"month", "y":"year"}
        default_label = f"Past {n} {units[unit]}{'s' if n != 1 else ''}"
    if start is not None or end is not None:
        default_label = "Custom window"
    if lower is not None and upper is not None and lower >= upper:
        raise InputError("Window start must be on or before its end date.")
    return Window(label or default_label, lower, upper)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", "--all-time", dest="all_time", type=Path, default=Path(__file__).resolve().parent/"data", help="Full raw export folder, single thread, or jq summary (default: script's data/ folder).")
    p.add_argument("--recent", "--year", dest="recent", type=Path, help="Optional separate comparison summary/export; normally both windows come from --input.")
    p.add_argument("--window-a", default="all", help="Primary duration: all, 30d, 8w, 6m, 2y, etc. (default: all).")
    p.add_argument("--window-b", default="1y", help="Comparison duration; none disables it (default: 1y).")
    p.add_argument("--start-a", type=date_ms, metavar="YYYY-MM-DD")
    p.add_argument("--end-a", type=date_ms, metavar="YYYY-MM-DD", help="Inclusive UTC end date for A.")
    p.add_argument("--start-b", "--since", dest="start_b", type=date_ms, metavar="YYYY-MM-DD")
    p.add_argument("--end-b", type=date_ms, metavar="YYYY-MM-DD", help="Inclusive UTC end date for B.")
    p.add_argument("--as-of", type=date_ms, metavar="YYYY-MM-DD", help="Anchor rolling windows to this UTC date instead of the latest exported message.")
    p.add_argument("--me", help="Your exact sender/participant name. Default: infer the most frequent participant.")
    p.add_argument("--include-groups", action="store_true", help="Include threads with more than two distinct participants (excluded by default).")
    p.add_argument("--out", type=Path, default=Path(__file__).resolve().parent/"reports", help="Output folder (default: script's reports/ folder).")
    p.add_argument("--all-label", "--label-a", dest="all_label", help="Override A's generated label.")
    p.add_argument("--recent-label", "--label-b", dest="recent_label", help="Override B's generated label.")
    p.add_argument("--top", type=int, default=12, help="Top conversations per dataset for volume/share/length plots (default: 12).")
    p.add_argument("--scatter-labels", type=int, default=15, help="Names on each log-scale scatter plot (default: 15; 0 hides them).")
    p.add_argument("--min-messages", type=int, default=100, help="Minimum messages for directionality scatter (default: 100).")
    p.add_argument("--intensity-min", type=int, default=500, help="Minimum messages for media/reaction scatter (default: 500).")
    p.add_argument("--change-min", type=int, default=250, help="Minimum messages in EACH portion for change analysis (default: 250).")
    p.add_argument("--dpi", type=int, default=200, help="PNG resolution (default: 200).")
    p.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=True, help="Search input folders recursively (default: on; --no-recursive disables).")
    p.add_argument("--jq", default="jq", help="jq executable name or path.")
    p.add_argument("--summarize-only", action="store_true", help="Write summaries and analysis without importing plotting libraries.")
    p.add_argument("--export-b", type=Path, help="Also write B's filtered per-conversation JSON files to this separate folder (raw input only).")
    p.add_argument("--print-jq", action="store_true", help="Print the embedded jq program and exit.")
    p.add_argument("--version", action="version", version=VERSION)
    args = p.parse_args(argv)
    if args.print_jq:
        return args
    if not 1 <= args.top <= 30:
        p.error("--top must be between 1 and 30")
    if not 0 <= args.scatter_labels <= 40:
        p.error("--scatter-labels must be between 0 and 40")
    if any(getattr(args, k) < 1 for k in ("min_messages", "intensity_min", "change_min", "dpi")):
        p.error("thresholds and --dpi must be positive integers")
    if args.dpi > 600:
        p.error("--dpi must be at most 600")
    return args


# Plotting and the CLI entry point follow below. Importing this file is side-effect free.

def short(s, width=27):
    return textwrap.shorten(s, width=width, placeholder="...") if " " in s else (s if len(s)<=width else s[:width-3]+"...")


def describe(d):
    o=d.summary["overall"]
    return f"{d.label}: {o['total_messages']:,} messages / {o['total_words']:,} words / {len(d.active)} nonempty threads"


class Plotter:
    """Keep styling consistent and use only values from the current datasets."""
    INK="#192F40"; MUTED="#607381"; TEAL="#087F8C"; PALE="#DCE5EB"
    EARLIER="#A6B6C3"; GRID="#E5EBEF"; OTHER="#B56A3C"

    def __init__(self, datasets, out, args, mode, comparison_name, shift_rows):
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import numpy as np
            from matplotlib.backends.backend_pdf import PdfPages
            from matplotlib.ticker import FuncFormatter, NullFormatter
            from matplotlib.lines import Line2D
            from matplotlib.patches import Patch
        except ImportError as e:
            raise InputError("Plotting dependencies are missing. Run: python3 -m pip install matplotlib numpy") from e
        self.plt=plt;self.np=np;self.Line=Line2D;self.Patch=Patch
        self.Formatter=FuncFormatter;self.NullFormatter=NullFormatter
        self.datasets=datasets;self.a=datasets[0];self.b=datasets[1] if len(datasets)>1 else None
        self.out=out;self.args=args;self.mode=mode;self.comparison_name=comparison_name;self.shifts=shift_rows;self.manifest=[]
        self.pdf=PdfPages(out/"Messenger_chat_report.pdf",metadata={"Title":"Messenger chat statistics", "Author":"messenger_plots.py", "Subject":"Message and word directionality, volume, and activity"})
        self.colors={r["key"]:c for r,c in zip(select_rows(datasets,3),["#2866B1","#B96A36","#8155A4"])}
        plt.rcParams.update({"font.family":"DejaVu Sans","font.size":10.5,"text.color":self.INK,"axes.labelcolor":self.INK,"xtick.color":self.MUTED,"ytick.color":self.INK,"axes.spines.top":False,"axes.spines.right":False,"axes.spines.left":False,"axes.spines.bottom":False,"axes.axisbelow":True,"figure.facecolor":"white","axes.facecolor":"white","pdf.fonttype":42})

    def frame(self, number, title, subtitle, insight, note, metric="", rows=0):
        fig=self.plt.figure(figsize=(15,max(9.4,4.8+.30*rows)))
        fig.text(.045,.967,f"MESSENGER / {number:02d}",fontsize=10,fontweight="bold",color=self.TEAL,va="top")
        fig.text(.96,.967," / ".join(short(d.label,22) for d in self.datasets),fontsize=9.5,color=self.MUTED,ha="right",va="top")
        fig.text(.045,.917,title,fontsize=23,fontweight="bold",va="top")
        fig.text(.045,.870,textwrap.fill(subtitle,145),fontsize=10.5,color=self.MUTED,va="top",linespacing=1.4)
        if metric:fig.text(.045,.814,textwrap.fill(metric,145),fontsize=10.5,fontweight="bold",color=self.TEAL,va="top",linespacing=1.4)
        fig.add_artist(self.Line([.045,.96],[.164,.164],transform=fig.transFigure,color=self.GRID,lw=1))
        fig.text(.045,.139,textwrap.fill(insight,145),fontsize=10.5,va="top",linespacing=1.5)
        fig.text(.045,.073,textwrap.fill(note,162),fontsize=8.5,color=self.MUTED,va="top",linespacing=1.4)
        fig.text(.96,.018,f"{number} / 8",fontsize=8.5,color=self.MUTED,ha="right")
        return fig

    def save(self, fig, number, slug, title):
        name=f"{number:02d}_{slug}.png"
        fig.savefig(self.out/name,dpi=self.args.dpi,facecolor="white")
        self.pdf.savefig(fig)
        self.plt.close(fig)
        self.manifest.append({"number":number,"file":name,"title":title})

    def grid(self, ax, axis="x"):
        ax.grid(axis=axis,color=self.GRID,lw=.7);ax.tick_params(length=0,pad=6)

    def compact(self,x,pos=None):
        return f"{x/1000000:g}m" if abs(x)>=1e6 else f"{x/1000:g}k" if abs(x)>=1000 else f"{x:g}"

    def panels(self,fig,left=.083,bottom=.24,top=.745):
        if len(self.datasets)==1:return [fig.add_axes([left,bottom,.95-left,top-bottom])]
        return [fig.add_axes([left,bottom,.48-left,top-bottom]),fig.add_axes([.565,bottom,.395,top-bottom])]

    def empty(self,ax,text="No eligible conversations in this window."):
        ax.set_axis_off();ax.text(.5,.5,textwrap.fill(text,55),ha="center",va="center",transform=ax.transAxes,color=self.MUTED,fontsize=12)

    def panel_title(self,ax,d):
        ax.set_title(short(d.label,40),loc="left",fontweight="bold",fontsize=13,pad=21)
        ax.text(0,1.015,d.timeframe+" UTC" if d.timeframe else d.dates,transform=ax.transAxes,fontsize=8,color=self.MUTED,va="bottom")

    def annotations(self,ax,rows,x,y,limit=4):
        from matplotlib.transforms import Bbox
        seen=set();boxes=[];labels=[];points=[]
        ax.figure.canvas.draw();renderer=ax.figure.canvas.get_renderer()
        bounds=ax.get_window_extent(renderer)
        # Protect every scatter marker, including unlabelled points and the
        # aggregate diamond. Work in display coordinates for log and linear axes.
        for collection in ax.collections:
            if not hasattr(collection,"get_sizes"):continue
            offsets=collection.get_offset_transform().transform(collection.get_offsets())
            sizes=collection.get_sizes()
            paths=collection.get_paths()
            for i,(px,py) in enumerate(offsets):
                if not (self.np.isfinite(px) and self.np.isfinite(py)):continue
                shape=max(abs(v) for v in paths[i%len(paths)].get_extents().extents) if paths else .5
                radius=shape*math.sqrt(sizes[i%len(sizes)] if len(sizes) else 36)*ax.figure.dpi/72+3
                points.append(Bbox.from_extents(px-radius,py-radius,px+radius,py+radius))
        distances=sorted(((dx,dy) for dx in (10,18,30,46,66,90,120,160,210) for dy in (10,18,30,46,66,90,120,160)),key=lambda p:p[0]**2+p[1]**2)
        candidates=[offset for dx,dy in distances for offset in ((-dx,dy),(dx,-dy))]
        for r in rows:
            if r["key"] in seen:continue
            seen.add(r["key"])
            if len(seen)>limit:break
            best=None
            ann=ax.annotate(short(r["name"],23),(r[x],r[y]),xytext=(-10,10),textcoords="offset points",fontsize=8.5,color=self.colors.get(r["key"],self.INK),arrowprops={"arrowstyle":"-","lw":.5,"color":self.MUTED},bbox={"facecolor":"white","edgecolor":"none","alpha":.94,"pad":1},zorder=6)
            for dx,dy in candidates:
                ann.set_position((dx,dy));ann.set_ha("right" if dx<0 else "left");ann.set_va("bottom" if dy>0 else "top")
                ann.update_positions(renderer);ann.update_bbox_position_size(renderer)
                rect=ann.get_bbox_patch().get_window_extent(renderer).expanded(1.05,1.2)
                collisions=10000*sum(rect.overlaps(p) for p in points)+2000*sum(rect.overlaps(b) for b in boxes)
                collisions+=50000*int(rect.x0<bounds.x0 or rect.x1>bounds.x1 or rect.y0<bounds.y0 or rect.y1>bounds.y1)
                score=collisions+math.hypot(dx,dy)*.01
                if best is None or score<best[0]:
                    best=(score,dx,dy,rect)
                if collisions==0:break
            _,dx,dy,rect=best
            ann.set_position((dx,dy));ann.set_ha("right" if dx<0 else "left");ann.set_va("bottom" if dy>0 else "top")
            ann.update_positions(renderer);ann.update_bbox_position_size(renderer)
            boxes.append(rect);labels.append(ann)
        return labels

    def volume(self):
        rows=select_rows(self.datasets,self.args.top)
        insight=" | ".join(f"{d.label}: top {min(10,len(d.active))} threads contain {fmt(ratio(sum(r['total_messages'] for r in d.active[:10]),d.summary['overall']['total_messages'],100))}% of messages" for d in self.datasets)
        scope="Groups are included." if self.args.include_groups else "Group chats are excluded."
        fig=self.frame(1,"Conversation volume: messages and words",f"Union of the top {self.args.top} threads in each window; ordered by primary-window volume. {scope}",insight,"The windows may overlap: do not add their totals. Titles have only the final export-number suffix removed."," | ".join(describe(d) for d in self.datasets),len(rows))
        axes=[fig.add_axes([.19,.24,.335,.505]),fig.add_axes([.615,.24,.345,.505])]
        for ax,field,title in zip(axes,["total_messages","total_words"],["Messages","Words"]):
            if not rows:self.empty(ax);continue
            av=[self.a.by_key.get(r["key"],{}).get(field,0) for r in rows]
            bv=[self.b.by_key.get(r["key"],{}).get(field,0) for r in rows] if self.b else None
            maximum=max(av+(bv or [])+[1]);yy=self.np.arange(len(rows))
            if self.mode=="nested":
                ax.barh(yy,bv,height=.64,color=self.TEAL)
                ax.barh(yy,[a-b for a,b in zip(av,bv)],left=bv,height=.64,color=self.EARLIER)
            elif self.b:
                ax.barh(yy-.18,av,height=.31,color=self.EARLIER)
                ax.barh(yy+.18,bv,height=.31,color=self.TEAL)
            else:ax.barh(yy,av,height=.64,color=self.TEAL)
            for i,a in enumerate(av):
                ax.text(a+maximum*.02,i-.18 if self.b and self.mode!="nested" else i,f"{a:,}",fontsize=8.5,va="center",fontweight="bold")
            if bv:
                for i,b in enumerate(bv):
                    if self.mode!="nested":ax.text(b+maximum*.02,i+.18,f"{b:,}",fontsize=8,va="center",color=self.TEAL)
                    elif b>maximum*.15:ax.text(b/2,i,f"{b:,}",fontsize=8,color="white",ha="center",va="center")
            ax.set_xlim(0,maximum*1.25);ax.set_ylim(len(rows)-.4,-.65);ax.set_yticks(yy,[short(r['name']) for r in rows] if ax is axes[0] else [])
            ax.tick_params(axis="y",labelsize=9.5);ax.xaxis.set_major_formatter(self.Formatter(self.compact));self.grid(ax)
            ax.set_title(title,loc="left",fontsize=13,fontweight="bold",pad=14);ax.set_xlabel(f"Total {title.lower()}")
        if self.b:
            label_a=self.comparison_name if self.mode=="nested" else self.a.timeframe
            fig.legend(handles=[self.Patch(color=self.TEAL,label=self.b.timeframe),self.Patch(color=self.EARLIER,label=textwrap.fill(label_a,54))],loc="upper right",bbox_to_anchor=(.96,.79),ncol=2,frameon=False,fontsize=8)
        self.save(fig,1,"conversation_volume","Conversation volume")

    def shares(self):
        rows=select_rows(self.datasets,self.args.top,True)
        metric=" | ".join(f"{d.label}: your share {fmt(d.aggregate['message_share'])}% messages / {fmt(d.aggregate['word_share'])}% words" for d in self.datasets)
        fig=self.frame(2,"Message balance and word balance",f"Your share is teal; the other participant is grey. The same {len(rows)} 1:1 conversations are aligned across windows.","A message majority can accompany a word minority when the other person writes more words per message.","Shares use the thread's totals. The aggregate shares are weighted across all valid 1:1 chats. Group chats and the self-chat are excluded.",metric,len(rows))
        n=len(self.datasets)*2;left=.20;gap=.026;width=(.76-(n-1)*gap)/n
        for i,(d,field) in enumerate((d,f) for d in self.datasets for f in ["message_share","word_share"]):
            ax=fig.add_axes([left+i*(width+gap),.23,width,.50])
            for y,ref in enumerate(rows):
                r=d.by_key.get(ref["key"]);v=r.get(field) if r and r["direct"] else None
                if v is None:ax.text(50,y,"not available",ha="center",va="center",fontsize=8,color=self.MUTED);continue
                ax.barh(y,v,color=self.TEAL,height=.7);ax.barh(y,100-v,left=v,color=self.PALE,height=.7)
                if v>=12:ax.text(v/2,y,f"{v:.1f}",color="white",ha="center",va="center",fontsize=8.5,fontweight="bold")
                if 100-v>=12:ax.text(v+(100-v)/2,y,f"{100-v:.1f}",ha="center",va="center",fontsize=8.5)
            ax.axvline(50,color=self.MUTED,lw=.8,ls=":");ax.set_xlim(0,100);ax.set_ylim(max(len(rows)-.4,.6),-.7)
            ax.set_yticks(range(len(rows)),[short(r['name']) for r in rows] if i==0 else []);ax.set_xticks([0,50,100],["0%","50%","100%"])
            ax.get_xticklabels()[0].set_ha("left");ax.get_xticklabels()[-1].set_ha("right");ax.tick_params(length=0,labelsize=9)
            ax.set_title("Messages" if field=="message_share" else "Words",fontsize=11,fontweight="bold",pad=14)
            if i%2==0:fig.text(left+i*(width+gap)+width+gap/2,.775,short(d.label,28),ha="center",fontsize=10,fontweight="bold",color=self.MUTED)
            if not rows:self.empty(ax)
        self.save(fig,2,"message_and_word_directionality","Message and word directionality")

    def direction(self):
        threshold=self.args.min_messages
        fig=self.frame(3,"More messages can still mean fewer words",f"One point per 1:1 chat with at least {threshold} messages and at least one word. Both axes show your share.","Below the diagonal: the other person averages more words per message. Shaded areas: the message majority and word majority belong to different people.","Diamonds show weighted aggregate shares across all valid 1:1 chats. Contributions do not reveal who initiates contact or how interested someone is.")
        eligible=[r for d in self.datasets for r in d.direct if r['total_messages']>=threshold and r['total_words']]
        values=[v for r in eligible for v in (r['message_share'],r['word_share'])]
        lower=max(0,math.floor((min(values+[50])-7)/5)*5);upper=min(100,math.ceil((max(values+[50])+7)/5)*5)
        mid=(50-lower)/(upper-lower)
        for ax,d in zip(self.panels(fig),self.datasets):
            rows=[r for r in d.direct if r['total_messages']>=threshold and r['total_words']]
            if not rows:self.empty(ax);continue
            ax.axvspan(50,upper,ymin=0,ymax=mid,color="#F2EEF7");ax.axvspan(lower,50,ymin=mid,ymax=1,color="#F2EEF7")
            ax.plot([0,100],[0,100],ls="--",color=self.EARLIER,lw=1);ax.axvline(50,color=self.MUTED,lw=.8);ax.axhline(50,color=self.MUTED,lw=.8)
            for r in rows:ax.scatter(r['message_share'],r['word_share'],s=25+math.sqrt(r['total_messages'])*.7,color=self.colors.get(r['key'],self.TEAL),alpha=.8,edgecolor="white",lw=.5,zorder=3)
            agg=d.aggregate
            if agg['message_share'] is not None and agg['word_share'] is not None:ax.scatter(agg['message_share'],agg['word_share'],s=55,color=self.INK,marker="D",zorder=5)
            ax.set_xlim(lower,upper);ax.set_ylim(lower,upper);ax.xaxis.set_major_formatter(self.Formatter(lambda x,p:f"{x:g}%"));ax.yaxis.set_major_formatter(self.Formatter(lambda x,p:f"{x:g}%"))
            ax.set_xlabel("Your share of messages");ax.set_ylabel("Your share of words");self.grid(ax,"both");self.panel_title(ax,d)
            self.annotations(ax,rows[:3]+sorted(rows,key=lambda r:-abs(r['word_share']-r['message_share']))[:2],"message_share","word_share",5)
        self.save(fig,3,"directionality_and_message_length","Message share versus word share")

    def shifts_plot(self):
        rows=self.shifts[:10]
        insight="No eligible non-overlapping comparison is available. Other charts still compare the supplied windows directly."
        if rows:
            r=rows[0];insight=f"Largest absolute word-share change: {r['name']}, {r['old_w']:.1f}% to {r['new_w']:.1f}% ({r['delta_w']:+.1f} percentage points)."
        fig=self.frame(4,"Changes in your share of the conversation",f"{self.comparison_name} versus {self.b.timeframe if self.b else 'no second timeframe'}. Minimum {self.args.change_min} messages in each period.",insight,"The two comparison periods do not overlap. When one timeframe contains the other, messages within the smaller timeframe are removed from the broader one before comparing shares. All dates are UTC.")
        axes=[fig.add_axes([.205,.24,.315,.50]),fig.add_axes([.62,.24,.315,.50])]
        for ax,f,title in zip(axes,["m","w"],["Messages","Words"]):
            if not rows:self.empty(ax,"No eligible chats. Adjust dates or --change-min; partly overlapping windows are not subtracted.");continue
            for y,r in enumerate(rows):
                ax.plot([r['old_'+f],r['new_'+f]],[y,y],color=self.EARLIER,lw=2)
                ax.scatter(r['old_'+f],y,s=55,facecolor="white",edgecolor=self.MUTED,zorder=3)
                ax.scatter(r['new_'+f],y,s=65,color=self.TEAL,edgecolor="white",lw=.5,zorder=4)
                ax.text(105,y,f"{r['new_'+f]-r['old_'+f]:+.1f}",va="center",fontsize=9.5,color=self.TEAL,fontweight="bold")
            ax.axvline(50,color=self.MUTED,lw=.8,ls="--");ax.set_xlim(0,116);ax.set_xticks([0,25,50,75,100]);ax.xaxis.set_major_formatter(self.Formatter(lambda x,p:f"{x:g}%"))
            ax.set_ylim(len(rows)-.4,-.7);ax.set_yticks(range(len(rows)),[short(r['name']) for r in rows] if f=='m' else []);self.grid(ax)
            ax.set_xlabel("Your share");ax.set_title(title,loc="left",fontsize=13,fontweight="bold",pad=16)
            ax.text(1,1.02,"Change (pp)",ha="right",transform=ax.transAxes,fontsize=8,color=self.MUTED)
        if self.b and self.mode in ("nested","disjoint"):fig.legend(handles=[self.Line([],[],marker="o",ls="",markerfacecolor="white",markeredgecolor=self.MUTED,label=textwrap.fill(self.comparison_name,54)),self.Line([],[],marker="o",ls="",color=self.TEAL,label=self.b.timeframe)],loc="upper right",bbox_to_anchor=(.95,.80),ncol=2,frameon=False,fontsize=8)
        self.save(fig,4,"changes_in_directionality","Changes in contribution")

    def word_volume(self):
        fig=self.frame(5,"Words versus messages in each conversation","Log scales reveal both small and large chats. Dashed lines mark 5 and 10 words per message.","At the same message count, a higher point means more words per message. A high-volume thread need not lead in total words.","Only threads with positive message and word counts are plotted. Word density divides by all messages, including media and other non-text messages.")
        eligible=[r for d in self.datasets for r in d.active if r['total_words']]
        xmax=max([r['total_messages'] for r in eligible]+[10])*2.5;ymax=max([r['total_words'] for r in eligible]+[10])*3
        for ax,d in zip(self.panels(fig),self.datasets):
            rows=[r for r in d.active if r['total_words']]
            if not rows:self.empty(ax);continue
            ax.set_xscale("log");ax.set_yscale("log")
            xs=self.np.geomspace(1,xmax,100)
            for f in (5,10):ax.plot(xs,f*xs,color=self.EARLIER,lw=1,ls="--")
            for r in rows:ax.scatter(r['total_messages'],r['total_words'],s=35,color=self.colors.get(r['key'],self.TEAL),alpha=.65,edgecolor="white",lw=.4,zorder=3)
            ax.set_xlim(.8,xmax);ax.set_ylim(.8,ymax);ax.xaxis.set_major_formatter(self.Formatter(self.compact));ax.yaxis.set_major_formatter(self.Formatter(self.compact));ax.xaxis.set_minor_formatter(self.NullFormatter());ax.yaxis.set_minor_formatter(self.NullFormatter());ax.tick_params(which="minor",length=0)
            ax.set_xlabel("Messages (log scale)");ax.set_ylabel("Words (log scale)");self.grid(ax,"both");self.panel_title(ax,d)
            self.annotations(ax,sorted(rows,key=lambda r:-r['total_words']),"total_messages","total_words",self.args.scatter_labels)
        self.save(fig,5,"words_versus_messages","Words versus messages")

    def intensity(self):
        minimum=self.args.intensity_min
        scope="Squares denote groups." if self.args.include_groups else "Group chats are excluded."
        fig=self.frame(6,"Reactions and media, adjusted for conversation size",f"Each point is a thread with at least {minimum} messages. {scope} Dashed lines are dataset-wide rates.","Reaction events and media-containing messages describe different habits; rates allow conversations of different sizes to be compared.","Media counts nonempty media arrays, including failed downloads. Reactions are events, not the percentage of messages receiving a reaction.")
        eligible=[r for d in self.datasets for r in d.active if r['total_messages']>=minimum]
        xm=max([r['media_rate'] for r in eligible]+[1])*1.3;ym=max([r['reaction_rate'] for r in eligible]+[1])*1.3
        for ax,d in zip(self.panels(fig),self.datasets):
            rows=[r for r in d.active if r['total_messages']>=minimum]
            if not rows:self.empty(ax);continue
            total=sum(r['total_messages'] for r in d.rows)
            mr=100*sum(r['messages_with_media'] for r in d.rows)/total;rr=100*sum(r['total_reactions'] for r in d.rows)/total
            ax.axvline(mr,color=self.MUTED,ls="--",lw=.8);ax.axhline(rr,color=self.MUTED,ls="--",lw=.8)
            for r in rows:ax.scatter(r['media_rate'],r['reaction_rate'],s=55,color=self.colors.get(r['key'],self.TEAL),marker="s" if len(set(r['participants']))>2 else "o",edgecolor="white",lw=.5,zorder=3)
            ax.set_xlim(0,max(xm,mr*1.15));ax.set_ylim(0,max(ym,rr*1.15));ax.xaxis.set_major_formatter(self.Formatter(lambda x,p:f"{x:g}%"))
            ax.set_xlabel("Messages containing media");ax.set_ylabel("Reactions per 100 messages");self.grid(ax,"both");self.panel_title(ax,d)
            self.annotations(ax,sorted(rows,key=lambda r:-r['reaction_rate'])[:2]+sorted(rows,key=lambda r:-r['media_rate'])[:2],"media_rate","reaction_rate",4)
        self.save(fig,6,"reactions_and_media_intensity","Reaction and media intensity")

    def activity(self):
        fig=self.frame(7,"Conversation span versus activity","Observed span runs from the first to the last message inside each selected window. Larger points mean more messages.","High rates can come from brief bursts. Long spans include quiet gaps, so this is not the average on days with messages.","Activity = messages / (span in days + 1). Grey points have fewer than 100 messages. Export and date-filter boundaries limit spans; they are not relationship ages.")
        allrows=[r for d in self.datasets for r in d.active];xm=max([r['span_days'] for r in allrows]+[1])*1.10
        ymin=min([r['messages_per_day'] for r in allrows]+[1])*.5;ymax=max([r['messages_per_day'] for r in allrows]+[1])*2
        for ax,d in zip(self.panels(fig),self.datasets):
            if not d.active:self.empty(ax);continue
            for r in reversed(d.active):ax.scatter(r['span_days'],r['messages_per_day'],s=20+math.sqrt(r['total_messages'])*.7,color=self.colors.get(r['key'],self.TEAL if r['total_messages']>=100 else self.EARLIER),alpha=.8,edgecolor="white",lw=.4,zorder=3)
            ax.set_yscale("log");ax.set_xlim(-xm*.02,xm);ax.set_ylim(ymin,ymax);ax.yaxis.set_major_formatter(self.Formatter(self.compact));ax.yaxis.set_minor_formatter(self.NullFormatter());ax.tick_params(which="minor",length=0)
            ax.set_xlabel("Observed conversation span (days)");ax.set_ylabel("Messages per day (log scale)");self.grid(ax,"both");self.panel_title(ax,d)
            self.annotations(ax,d.active,"span_days","messages_per_day",self.args.scatter_labels)
        self.save(fig,7,"duration_versus_activity","Observed span and activity")

    def word_length(self):
        rows=select_rows(self.datasets,self.args.top,True)
        selected={r['key'] for r in rows}
        for d in self.datasets:
            eligible=[r for r in d.direct if r['total_messages']>=self.args.intensity_min and r['my_wpm'] is not None and r['other_wpm'] is not None]
            for r in sorted(eligible,key=lambda r:-abs(r['my_wpm']-r['other_wpm']))[:3]:
                if r['key'] not in selected:rows.append(r);selected.add(r['key'])
        rows.sort(key=lambda r:-self.a.by_key.get(r['key'],r)['total_messages'])
        metric=" | ".join(f"{d.label}: you {fmt(d.aggregate['my_wpm'],2)} / others {fmt(d.aggregate['other_wpm'],2)} words per message" for d in self.datasets)
        fig=self.frame(8,"Words per message, by participant",f"Top {self.args.top} direct chats per window, plus up to three large message-length gaps among substantial chats.","The two markers show each person's mean. You can send more messages while writing fewer words if your individual messages contain fewer words.","Mean = each person's words / their messages, including non-text messages. Averages above use all valid 1:1 chats and are weighted by message counts.",metric,len(rows))
        axes=([fig.add_axes([.20,.235,.61,.49])] if not self.b else [fig.add_axes([.20,.235,.26,.49]),fig.add_axes([.625,.235,.26,.49])])
        vals=[v for d in self.datasets for ref in rows for r in [d.by_key.get(ref['key'],{})] if r.get('direct') for v in (r['my_wpm'],r['other_wpm']) if v is not None]
        xmax=max(vals+[1])*1.14
        for ax,d in zip(axes,self.datasets):
            for y,ref in enumerate(rows):
                r=d.by_key.get(ref['key'])
                if not r or not r['direct']:ax.text(xmax*.5,y,"not available",ha="center",va="center",fontsize=8,color=self.MUTED);continue
                me,other=r['my_wpm'],r['other_wpm']
                if me is not None and other is not None:ax.plot([me,other],[y,y],color=self.EARLIER,lw=2)
                columns=(1.07,1.18) if not self.b else (1.12,1.29)
                for v,color,column in [(me,self.TEAL,columns[0]),(other,self.OTHER,columns[1])]:
                    if v is not None:ax.scatter(v,y,s=40,color=color,edgecolor="white",lw=.5,zorder=3)
                    ax.text(column,y,fmt(v,2),transform=ax.get_yaxis_transform(),ha="right",va="center",fontsize=8.8,color=color,fontweight="bold")
            ax.set_xlim(0,xmax);ax.set_ylim(max(len(rows)-.4,.6),-.7);ax.set_yticks(range(len(rows)),[short(r['name']) for r in rows] if ax is axes[0] else []);ax.tick_params(axis="y",labelsize=9.5)
            ax.set_xlabel("Words per message");self.grid(ax);ax.set_title(short(d.label,30),loc="left",fontsize=13,fontweight="bold",pad=14)
            columns=(1.07,1.18) if not self.b else (1.12,1.29)
            for column,label,color in [(columns[0],"You",self.TEAL),(columns[1],"Other",self.OTHER)]:ax.text(column,1.025,label,transform=ax.transAxes,ha="right",fontsize=8.5,color=color,fontweight="bold")
            if not rows:self.empty(ax)
        fig.legend(handles=[self.Line([],[],marker="o",ls="",color=self.TEAL,label="You"),self.Line([],[],marker="o",ls="",color=self.OTHER,label="Other participant")],loc="upper right",bbox_to_anchor=(.96,.785),ncol=2,frameon=False,fontsize=9.5)
        self.save(fig,8,"words_per_message_by_person","Words per message by person")

    def run(self):
        try:
            for method in (self.volume,self.shares,self.direction,self.shifts_plot,self.word_volume,self.intensity,self.activity,self.word_length):method()
        finally:
            self.pdf.close()
            self.plt.close("all")
        return self.manifest


def write_json(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")


def export_filtered(raw_files, source_root, target, window, jq, include_groups=False):
    """Optionally export filtered conversation JSON; do not copy media assets."""
    source_root=source_root.resolve();target=target.resolve()
    base=source_root if source_root.is_dir() else source_root.parent
    if target==base or target.is_relative_to(base):
        raise InputError("--export-b must be outside the raw input folder to avoid re-importing its filtered copies on the next run. Use reports/raw_window_b.")
    exe=shutil.which(jq)
    if not exe:raise InputError("jq is required for --export-b.")
    if not include_groups:
        raw_files=[p for p in raw_files if len(set(read_json(p)["participants"]))<=2]
    target.parent.mkdir(parents=True,exist_ok=True)
    program='.messages |= map(select(($start == null or .timestamp >= $start) and ($end == null or .timestamp < $end)))'
    with tempfile.TemporaryDirectory(prefix=".filtered-",dir=target.parent) as temp:
        temp=Path(temp)
        for path in raw_files:
            path=path.resolve()
            try:rel=path.relative_to(base)
            except ValueError as e:raise InputError(f"Cannot preserve the relative filename of linked input outside the export folder: {path}") from e
            dest=temp/rel;dest.parent.mkdir(parents=True,exist_ok=True)
            with dest.open("wb") as out:
                result=subprocess.run([exe,"--argjson","start",json.dumps(window.start),"--argjson","end",json.dumps(window.end),program,str(path)],stdout=out,stderr=subprocess.PIPE,check=False)
            if result.returncode:raise InputError(f"Could not filter {path}: {result.stderr.decode('utf-8','replace')}")
        # Avoid leaving old conversation files mixed into a new filtered export.
        if target.exists() and any(target.iterdir()):
            raise InputError(f"Filtered export folder is not empty: {target}. Choose a new folder or empty it first.")
        target.mkdir(parents=True,exist_ok=True)
        for path in temp.rglob("*.json"):
            dest=target/path.relative_to(temp);dest.parent.mkdir(parents=True,exist_ok=True);shutil.move(str(path),str(dest))
    return len(raw_files)


def write_notes(path, datasets, owner, windows, mode, comparison_name, shift_rows, warnings, manifest, include_groups=False):
    lines=["# Messenger analysis", "",f"Participant treated as 'you': **{owner}**.","", "| Metric | "+" | ".join(d.label.replace('|','/') for d in datasets)+" |", "|---|"+"---:|"*len(datasets)]
    specs=[("Messages",lambda d:f"{d.summary['overall']['total_messages']:,}"),("Words",lambda d:f"{d.summary['overall']['total_words']:,}"),("Threads in file",lambda d:str(len(d.rows))),("Nonempty threads",lambda d:str(len(d.active))),("Valid 1:1 chats",lambda d:str(len(d.direct))),("Your message share",lambda d:fmt(d.aggregate['message_share'],1,'%')),("Your word share",lambda d:fmt(d.aggregate['word_share'],1,'%')),("Your words/message",lambda d:fmt(d.aggregate['my_wpm'],2)),("Others' words/message",lambda d:fmt(d.aggregate['other_wpm'],2))]
    for name,fn in specs:lines.append("| "+name+" | "+" | ".join(fn(d) for d in datasets)+" |")
    lines += ["", "## Windows", ""]
    for i,d in enumerate(datasets):
        lines.append(f"- **{d.label}**: {d.timeframe} UTC; observed {d.dates}. Requested filter: {windows[i].description()}.")
    lines.append("- Group chats are included in volume and intensity totals." if include_groups else "- Group chats (more than two distinct participants) are excluded from all summaries, plots, and filtered exports.")
    lines += ["", "## Findings", ""]
    for d in datasets:
        if not d.active:
            lines.append(f"- {d.label}: no messages in the selected window.");continue
        m=max(d.active,key=lambda r:r['total_messages']);w=max(d.active,key=lambda r:r['total_words'])
        lines.append(f"- {d.label}: {m['name']} leads message volume ({m['total_messages']:,}); {w['name']} leads word volume ({w['total_words']:,}).")
        agg=d.aggregate;gap=ratio(agg['other_wpm'],agg['my_wpm']) if agg['other_wpm'] is not None and agg['my_wpm'] else None
        if gap is not None:lines.append(f"- {d.label}: other participants average {(gap-1)*100:+.1f}% words per message relative to you (weighted across 1:1 chats).")
    if shift_rows:
        r=shift_rows[0];lines.append(f"- Largest eligible absolute word-share change, {comparison_name} compared with {datasets[-1].timeframe}: {r['name']}, {r['old_w']:.1f}% to {r['new_w']:.1f}% ({r['delta_w']:+.1f} percentage points).")
    comparison=f"Share changes compare {comparison_name} with {datasets[-1].timeframe} (UTC), using non-overlapping messages." if mode in ("nested","disjoint") else "The supplied timeframes do not support a separate, non-overlapping share-change comparison."
    lines += ["", "## Charts", ""]
    lines += [f"{x['number']}. {x['title']} - `{x['file']}`" for x in manifest]
    lines += ["", "## Definitions and validation", "",
        "- The original supplied jq summary is embedded in the Python script. Time filtering is applied to message timestamps before that summary; the regex word-count rule is unchanged.",
        "- All dates are UTC. Explicit start/end dates are inclusive. The default anchor is the latest message's UTC date, not today's date. A 1y or 12m window starts at UTC midnight on the same calendar date one year earlier and includes the anchor date. Leap/month-end dates clamp to the last valid day. A 30d window contains 30 calendar dates, including the anchor. Use explicit dates to control exact boundaries.",
        "- Filtered summaries retain threads with zero messages, so the thread count in a filtered summary can equal the full export's count. Nonempty counts are reported separately.",
        "- Thread identity uses its title without the final numeric export suffix plus its exact set of participant names. Ambiguous normalized identities are rejected rather than merged.",
        "- Directionality requires exactly two distinct participants, including your name, and reconciled participant totals. Groups and the self-chat are excluded. Duplicate identical people records are ignored for analysis while the original jq output is retained.",
        "- Shares are counts divided by thread totals. Global shares and words/message are weighted by total counts, not averages of per-thread percentages.",
        "- Words/message includes media and other non-text messages in its denominator. It is not the mean text-message length or a median.",
        "- Reactions are recorded events, not unique messages receiving a reaction. Media intensity counts messages with a nonempty media array; it does not verify attachment downloads.",
        "- Activity is messages / (span days + 1), including quiet gaps. Spans are bounded by the export/filter and are not relationship ages.",
        f"- {comparison} Summary-only comparisons cannot prove message-level deduplication or recover the exact requested date boundaries; their labels use observed dates.",
        "- Totals, message-type counts, reaction-type counts, spans, and activity rates are validated. The data cannot establish initiation, reply times, active-day averages, sentiment, or completeness of lifetime history.",
        "- Optional raw filtered exports contain JSON only. Media folders/files are not copied, and existing media paths are left unchanged."]
    if warnings:lines += ["", "## Data notices", ""]+["- "+w for w in warnings]
    path.write_text("\n".join(lines)+"\n",encoding="utf-8")


def main(argv=None):
    args=parse_args(argv)
    if args.print_jq:
        print(JQ_PROGRAM,end="");return 0
    try:
        source=args.all_time.expanduser().resolve();out=args.out.expanduser().resolve()
        summary,raw,skipped=discover(source,args.recursive,out)
        protected={p.resolve() for p in raw} | ({source} if source.is_file() else set())
        notices=[]
        if skipped:notices.append(f"Ignored {len(skipped)} non-conversation JSON files (including existing summaries) in the input folder.")
        print(f"Reading {len(raw)} raw conversation files." if raw else "Reading supplied summary.",file=sys.stderr)
        full=scope_summary(summary,args.include_groups) if summary is not None else summarize(raw,args.jq,include_groups=args.include_groups)
        anchor=args.as_of if args.as_of is not None else full['overall'].get('latest_timestamp')
        end_a=args.end_a if args.end_a is not None else args.as_of
        end_b=args.end_b if args.end_b is not None else args.as_of
        wa=resolve_window(args.window_a,anchor,args.start_a,end_a,args.all_label)
        wb=resolve_window(args.window_b,anchor,args.start_b,end_b,args.recent_label)
        if wa is None:raise InputError("Window A cannot be 'none'.")
        if wa.label=="Custom window":wa.label=period_text(wa.start,wa.end) if wa.start is not None and wa.end is not None else "Selected primary dates"
        if wb and wb.label=="Custom window":wb.label=period_text(wb.start,wb.end) if wb.start is not None and wb.end is not None else "Selected comparison dates"
        if summary is not None and (wa.start is not None or wa.end is not None):
            raise InputError("A summary has no per-message timestamps. Use the full raw export to change timeframes.")
        sa=full if wa.start is None and wa.end is None else summarize(raw,args.jq,wa.start,wa.end,args.include_groups)
        sb=None;separate=False
        if args.recent:
            if args.window_b=="none":raise InputError("--recent cannot be combined with --window-b none.")
            if args.start_b is not None or args.end_b is not None or args.window_b!="1y" or args.as_of is not None:
                raise InputError("Do not combine a separate --recent input with B's window filters. Use one full raw export for tunable windows.")
            other=args.recent.expanduser().resolve();existing,other_raw,other_skipped=discover(other,args.recursive,out)
            protected.update(p.resolve() for p in other_raw)
            if other.is_file():protected.add(other)
            sb=scope_summary(existing,args.include_groups) if existing is not None else summarize(other_raw,args.jq,include_groups=args.include_groups)
            wb=Window(args.recent_label or "Comparison export",None,None);separate=True
            if other_skipped:notices.append(f"Ignored {len(other_skipped)} non-conversation JSON files in the separate comparison input.")
        elif wb:
            if summary is not None:
                raise InputError("Cannot derive a year from summary totals. Provide the full raw export, supply --recent with another summary, or choose --window-b none.")
            print(f"Filtering B: {wb.description()}.",file=sys.stderr)
            sb=full if wb.start is None and wb.end is None else summarize(raw,args.jq,wb.start,wb.end,args.include_groups)
        if args.export_b and (not raw or wb is None or separate):
            raise InputError("--export-b requires one raw full-export input and an enabled window B.")
        owner=args.me or infer_owner([full])
        a=prepare(sa,wa.label,owner);b=prepare(sb,wb.label,owner) if sb is not None else None
        datasets=[a]+([b] if b else []);windows=[wa]+([wb] if b else [])
        if a.active and not any(owner in r['participants'] for r in a.rows):notices.append(f"Participant {owner!r} is absent from window A; directionality charts will be empty.")
        mode="unavailable"
        if b and separate:
            nested,reasons=check_nested(a,b)
            if nested:mode="nested"
            else:
                # Disjoint observed ranges can still be compared without subtraction.
                if a.active and b.active and (max(r['last_timestamp'] for r in a.active)<min(r['first_timestamp'] for r in b.active) or max(r['last_timestamp'] for r in b.active)<min(r['first_timestamp'] for r in a.active)):
                    mode="disjoint"
                else:notices += ["Separate-period comparison unavailable: "+r for r in reasons]
        elif b:
            lo=lambda w:float('-inf') if w.start is None else w.start
            hi=lambda w:float('inf') if w.end is None else w.end
            if lo(wa)<=lo(wb) and hi(wb)<=hi(wa):
                mode="nested"
            elif hi(wa)<=lo(wb) or hi(wb)<=lo(wa):mode="disjoint"
        comparison_name=comparison_dates(datasets,windows,mode)
        if b and mode=="unavailable":notices.append(f"{a.timeframe} and {b.timeframe} cannot be separated into comparison periods from these inputs. Select disjoint dates, or use the broader timeframe as the primary selection.")
        for d in datasets:notices += [f"{d.label}: {w}" for w in d.warnings]
        shift_rows=changes(a,b,mode,args.change_min)
        analysis={"version":VERSION,"owner":owner,"include_groups":args.include_groups,"comparison_mode":mode,"comparison_base":comparison_name,"anchor_utc_date":datetime.fromtimestamp(anchor/1000,timezone.utc).strftime('%Y-%m-%d') if anchor is not None else None,"warnings":notices,"changes":shift_rows,"datasets":[]}
        for d,w in zip(datasets,windows):
            metrics=[{k:v for k,v in r.items() if k not in ('people','reaction_types','message_types','key')} for r in d.rows]
            analysis['datasets'].append({"label":d.label,"timeframe":d.timeframe,"requested_window":{"start_ms":w.start,"end_exclusive_ms":w.end,"description":w.description()},"observed_dates":d.dates,"overall":d.summary['overall'],"one_to_one":d.aggregate,"threads":metrics})
        out.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".messenger-build-",dir=out.parent) as stage_dir:
            stage=Path(stage_dir)
            write_json(stage/'chat_stats_a.json',sa)
            if sb is not None:write_json(stage/'chat_stats_b.json',sb)
            write_json(stage/'analysis.json',analysis)
            manifest=[]
            if not args.summarize_only:
                print("Rendering eight charts and the PDF report...",file=sys.stderr)
                manifest=Plotter(datasets,stage,args,mode,comparison_name,shift_rows).run()
            write_json(stage/'chart_manifest.json',manifest)
            write_notes(stage/'README.md',datasets,owner,windows,mode,comparison_name,shift_rows,notices,manifest,args.include_groups)
            if manifest:
                with zipfile.ZipFile(stage/'Messenger_charts.zip','w',compression=zipfile.ZIP_DEFLATED) as z:
                    for item in manifest:z.write(stage/item['file'],item['file'])
                    z.write(stage/'README.md','README.md')
            for p in stage.iterdir():
                if (out/p.name).resolve() in protected:raise InputError(f"Output would overwrite an input: {out/p.name}. Choose another --out folder.")
            if args.export_b:
                export_target=args.export_b.expanduser().resolve()
                n=export_filtered(raw,source,export_target,wb,args.jq,args.include_groups)
                print(f"Exported {n} filtered conversation JSON files to {export_target}",file=sys.stderr)
            out.mkdir(parents=True,exist_ok=True)
            # Remove only our stale B summary after a previous two-window run.
            # Never remove an input or unrelated files in the output directory.
            stale_b=out/'chat_stats_b.json'
            prior_analysis=out/'analysis.json'
            if sb is None and stale_b.is_file() and stale_b.resolve() not in protected and prior_analysis.is_file():
                try:owned=read_json(prior_analysis).get('version') in ("1.0.0",VERSION)
                except (InputError,AttributeError):owned=False
                if owned:stale_b.unlink()
            for p in stage.iterdir():os.replace(p,out/p.name)
        for d in datasets:print(describe(d)+f"; {d.dates}")
        for warning in notices:print("Notice: "+warning,file=sys.stderr)
        print(f"Saved {'summaries and analysis' if args.summarize_only else '8 PNGs, PDF, ZIP, summaries and analysis'} to {out}")
        return 0
    except (InputError,OSError,KeyError,TypeError,OverflowError) as e:
        print(f"Error: {e}",file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())
