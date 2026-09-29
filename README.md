# Messenger analysis

Generate eight plots and a searchable PDF comparing Messenger conversation volume, directionality, and words per message.

**One full export is all you need.** By default, the script compares all available history with the past year, filtering the original messages with jq before computing either summary. It uses the most recent message's UTC date as its reference date, so rerunning an old export gives the same timeframes.

**Group chats are excluded by default.** Threads with more than two distinct participants are omitted from summaries, plots, and optional filtered exports. Add `--include-groups` to include them in volume, intensity, and activity analysis. Directionality and participant words/message always use valid 1:1 chats. Self-chats remain in volume totals.

## Download the export

**Use [Facebook's secure-storage download page](https://www.facebook.com/secure_storage/dyi). This is the only download link that worked properly for these encrypted Messenger exports.**

The script expects the conversation JSON format produced by that workflow, with `participants`, `threadName`, and `messages` at the top level. Other Facebook export schemas are not interchangeable.

## Setup

Requires **Python 3.10+** and **jq 1.6+**. On macOS:

```bash
brew install jq
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
mkdir -p data
```

Unzip the **full** export into `data/`. The script searches recursively, so either layout works:

```text
messenger-analysis/
  messenger_plots.py
  data/
    messages/
      Example Person_12.json
      Another Person_37.json
      media/...
  reports/                    # generated
```

Do not put multiple overlapping exports or filtered copies in the same input tree: each conversation must occur once. Media files are not opened. Existing jq summaries and unrelated JSON objects are skipped when scanning folders; malformed JSON is reported rather than silently ignored.

`data/`, `reports/`, Python environments, caches, and `chat_stats*.json` are excluded by `.gitignore`. The script never uploads messages. Keep raw exports and generated reports out of Git.

## Run the default comparison

From the repository folder:

```bash
python messenger_plots.py --me "Brogan Csinger"
```

This reads `data/` and writes `reports/`. The defaults are:

- **Window A:** all available messages.
- **Window B:** one calendar year ending on the latest exported message's UTC date.
- **Scope:** threads with at most two distinct participants. The reference date uses the latest message within this scope; `--include-groups` expands it.
- **You:** the exact participant/sender name passed with `--me`. If omitted, the script infers the most frequent participant and rejects ties.

To point directly at an export elsewhere:

```bash
python messenger_plots.py \
  --input "/path/to/full-export/messages" \
  --me "Brogan Csinger" \
  --out reports/default
```

Default `data/` and `reports/` paths are relative to the script itself. Explicit paths are relative to the working directory. Use `--no-recursive` to scan only the input folder's top level.

## Preset launchers

After setup and putting your full export in `data/`, double-click any `.command` file in `launchers/` on macOS, or run it from a terminal. These are ordinary shell scripts; each writes to its own folder:

| Launcher | Timeframes | Output folder |
|---|---|---|
| `full-vs-year.command` | Full history vs past calendar year | `reports/full-vs-year/` |
| `full-vs-6months.command` | Full history vs past six calendar months | `reports/full-vs-6months/` |
| `year-vs-6months.command` | Past calendar year vs past six calendar months | `reports/year-vs-6months/` |
| `year-vs-90days.command` | Past calendar year vs past 90 days | `reports/year-vs-90days/` |
| `90days-vs-30days.command` | Past 90 days vs past 30 days | `reports/90days-vs-30days/` |

All use the latest included message as the date anchor and exclude group chats by default. The change chart compares the non-overlapping portions and labels their dates. Rerunning a launcher refreshes its report folder.

The launchers find the repository regardless of your current directory. They use `.venv/bin/python` when available, then `python3` from your environment; `PYTHON_BIN` can select another interpreter. `run-preset.sh` is their shared helper. No virtual-environment activation is needed when using the repository's `.venv`.

Pass any plotter options from a terminal to override the preset or defaults:

```bash
./launchers/full-vs-year.command --input "/path/to/full-export/messages" --me "Brogan Csinger"
./launchers/year-vs-90days.command --scatter-labels 20 --include-groups
./launchers/90days-vs-30days.command --as-of 2026-06-30 --out reports/june-comparison
```

## Change either timeframe

### Rolling windows

```bash
python messenger_plots.py --window-a 2y --window-b 6m --out reports/2y-vs-6m
python messenger_plots.py --window-a all --window-b 90d --out reports/all-vs-90d
python messenger_plots.py --window-a 8w --window-b 2w --out reports/8w-vs-2w
```

Use `d` for days, `w` for weeks, `m` for calendar months, and `y` for calendar years. `all` means the available history; `--window-b none` makes a single-window report.

```bash
python messenger_plots.py --window-a all --window-b none
```

**Exact boundaries:** dates use UTC. A `1y` window anchored to 29 September 2026 starts at **00:00 on 29 September 2025** and includes the whole anchor date. Calendar month/year windows include both anniversary dates; leap-day and month-end dates clamp to the last valid date. A `30d` window contains exactly 30 calendar dates, including the anchor. These definitions are recorded in `analysis.json`.

Override the reference date with `--as-of`; this also caps the all-history window at that date:

```bash
python messenger_plots.py --as-of 2026-06-30 --window-b 1y
```

### Explicit date ranges

Start and end dates are inclusive. End dates are implemented as a strict cutoff at the following UTC midnight.

```bash
python messenger_plots.py \
  --start-a 2025-01-01 --end-a 2025-12-31 --label-a "2025" \
  --start-b 2026-01-01 --end-b 2026-06-30 --label-b "First half of 2026" \
  --out reports/2025-vs-2026-h1
```

An explicit start overrides a rolling window's calculated start. An explicit end overrides its reference date. To filter one unbounded side, use `--window-a all` or `--window-b all` with just the desired boundary. You can relabel either window with `--label-a` and `--label-b`.

The change plot labels both comparison periods with their actual dates. For example, a full export covering 27 September 2024 through 29 September 2026 with a past-year selection compares **27 September 2024–28 September 2025** with **29 September 2025–29 September 2026**. Messages in the past year are removed from the full history before calculating the earlier shares, so the comparison periods do not overlap. When the selected comparison sits in the middle of the primary timeframe, both remaining date ranges are shown. Disjoint selections are compared directly. Partly overlapping selections cannot produce this change plot; choose disjoint dates or put the broader timeframe in `--window-a`.

## Get only the one-year data

To create the two summary JSON files without plotting:

```bash
python messenger_plots.py --summarize-only --window-b 1y
```

The past-year summary is `reports/chat_stats_b.json`. No plotting libraries are imported in this mode, although jq is still needed for raw exports.

To also get **per-conversation JSON containing only that year's messages**:

```bash
python messenger_plots.py \
  --window-b 1y \
  --summarize-only \
  --export-b reports/raw_one_year
```

The filtered export preserves each selected conversation's metadata and relative filename. Empty threads are retained; group chats are omitted unless `--include-groups` is supplied. Only JSON is written: media assets are not copied, and their existing paths are unchanged. The export destination must be empty and outside the input tree, preventing filtered copies from being re-imported on the next run. Pick a new export destination when changing dates.

Both summary and raw filtering use jq. Inspect the exact embedded summary with:

```bash
python messenger_plots.py --print-jq
```

The accompanying `chat_summary.jq` is the same supplied summary program, formatted for `jq -s -f`. The Python script embeds it so the script can also be used as a single file; editing the standalone `.jq` file does not change the embedded program. The wrapper applies group and date filters before this program; running the standalone jq summary directly does not apply those filters.

For a manual raw-file filter, this selects 29 September 2025 through 29 September 2026 inclusive, using UTC regardless of your computer's timezone:

```bash
mkdir -p reports
jq --arg start '2025-09-29T00:00:00Z' \
   --arg end '2026-09-30T00:00:00Z' '
  (($start | fromdateiso8601) * 1000) as $start_ms |
  (($end | fromdateiso8601) * 1000) as $end_ms |
  .messages |= map(select(
    .timestamp >= $start_ms and .timestamp < $end_ms
  ))
' 'data/messages/Example Person_12.json' > reports/example_one_year.json
```

For routine use, prefer the `--export-b` command above; it handles all files and date calculations together.

## Existing summaries

You can reuse the summaries already generated by the original jq command:

```bash
python messenger_plots.py \
  --input chat_stats_alltime.json \
  --recent chat_stats.json \
  --me "Brogan Csinger" \
  --label-a "All-time export" --label-b "Past year"
```

jq is not needed when both inputs are summaries. A summary has no per-message timestamps, so **new timeframes cannot be recovered from summary totals**. To change dates, supply the full raw export. A single summary can be plotted with `--window-b none`.

Group exclusion also applies to supplied summaries: totals are rebuilt from retained threads, and the source files are unchanged. Date labels use observed message dates because summaries do not retain the original filter boundaries.

## Outputs

Every plotting run creates:

1. Top conversations by messages and words.
2. Each participant's message and word shares.
3. Message share versus word share.
4. Changes in contribution between non-overlapping portions.
5. Words versus messages per thread, on log scales.
6. Reaction and media intensity.
7. Observed conversation span versus activity.
8. Words per message, separately for each participant.

Files in the output folder:

- `01_...png` through `08_...png`: high-resolution charts.
- `Messenger_chat_report.pdf`: all eight charts, with searchable text and vector graphics.
- `Messenger_charts.zip`: the PNGs and analysis notes.
- `chat_stats_a.json`, `chat_stats_b.json`: summaries for the chosen windows; B is absent for a single-window run.
- `analysis.json`: window bounds, totals, per-thread derived metrics, comparison checks, and data notices.
- `chart_manifest.json`: the charts from this run.
- `README.md`: generated findings, definitions, and data notices.

Each run replaces files with these generated names. Use separate `--out` folders to retain different comparisons. `--summarize-only` refreshes summaries/analysis but does not refresh existing charts, so use a separate output folder if you want to keep those outputs clearly distinct.

Control selection and image size:

```bash
python messenger_plots.py --top 20 --scatter-labels 20 --min-messages 100 \
  --intensity-min 500 --change-min 250 --dpi 250
```

`--change-min` applies to **each** non-overlapping portion. Changing thresholds affects chart selection, not overall totals. Unsupported comparisons or empty selections produce explanatory chart panels rather than invented values. Names and captions come from the current input; no particular contact is required.

Log-scale scatter plots label up to **15 conversations per panel** by default (largest word totals on the words/messages chart; largest message totals on the activity chart). Set `--scatter-labels` from 0 to 40 to change this. All scatter name labels, including message-share versus word-share labels, sit upper-left or lower-right of their point, with a thin connector. Placement seeks to avoid every plotted marker and previously placed label; dense plots may still have overlaps, so reduce the label count if needed.

## Expected schema and interpretation

```json
{
  "participants": ["Your Name", "Other Person"],
  "threadName": "Other Person_12",
  "messages": [
    {
      "senderName": "Your Name",
      "timestamp": 1790640000000,
      "type": "text",
      "text": "Hello there",
      "isUnsent": false,
      "media": [],
      "reactions": [{"actor": "Other Person", "reaction": "❤"}]
    }
  ]
}
```

Timestamps must be numeric Unix **milliseconds**. The original jq word tokenizer (`[[:alnum:]'’]+`) is preserved. Words/message divides by **all messages**, including non-text messages; it is not mean text-message length. Reactions count events, and media means a nonempty media array, even if an attachment failed to download. Messages/day includes silent gaps between the first and last messages.

The original jq script can repeat a self-chat's person summary because its participant name occurs twice. The plotter retains the original summary, deduplicates identical person records for validation, and excludes the self-chat and groups from 1:1 directionality. Threads with unattributed sender counts remain in volume totals but are excluded from directionality. It never fixes this by guessing who wrote a message.

Thread matching removes only a final `_number` suffix and checks the participant set. Ambiguous keys fail explicitly. Aggregates and timestamps are validated before plotting. The script does not deduplicate raw messages or prove that an export contains your complete lifetime history.

The jq summary uses `-s` and therefore holds the input dataset in memory. It is not a constant-memory streaming pipeline. The Python wrapper spools input through a temporary file to avoid shell quoting and command-line length limits.

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests use synthetic messages only. They cover UTC cutoffs, leap/month boundaries, jq summaries, group selection, duplicate self-chat handling, validation failures, dated comparison periods, raw filtered exports, scatter label placement, and chart generation for sparse/empty datasets. No personal export is included in the repository.
