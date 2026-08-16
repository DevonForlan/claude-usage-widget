# claude-usage-widget

A small always-on-top desktop widget showing Claude Code usage. Windows, Python + PySide6.

> **This shows real token counts, not a quota percentage.** Claude Code does not expose your actual plan limit to local tools, so there is nothing to compute a percentage against — see [Why not a percentage](#why-not-a-percentage). The number is a genuine local measurement (summed from your own session transcripts), and the UI labels it `LOCAL` so it's never mistaken for an official reading.

## Features

- Compact frameless window (250×160) that sits in a screen corner, true rounded corners (no white edge - see [Known issues fixed](#known-issues-fixed-along-the-way))
- Total tokens used in the trailing 5 hours, summed from this machine's own Claude Code session transcripts
- Hover the number for the exact count, split into **fresh** (new input/output/cache-writes) vs **cached** (replayed context) - see [why that split matters](#why-fresh-vs-cached)
- **Always on top** toggle
- **Opacity** slider, 30%–100%
- Drag anywhere on the window to move it
- Remembers window position, opacity, and always-on-top between runs

## Requirements

- Windows
- Python 3.9+
- PySide6 (`pip install -r requirements.txt`)

## Running it

```powershell
cd claude-usage-widget
python -m claude_usage_widget
```

To launch without a console window lingering behind it, use `pythonw`:

```powershell
pythonw -m claude_usage_widget
```

Close it with the `✕` in the top-right corner. Settings are saved on close.

## Tests

```powershell
python selftest.py
```

Builds the real widget and exercises it: `format_token_count`, `LocalTranscriptUsageProvider` against synthetic transcripts (window filtering, the mtime pre-filter, the fresh/cached split, a missing projects directory), the always-on-top flag, opacity limits, the settings round-trip, and recovery from a saved position that is no longer on any connected monitor. 30 checks, all against real code paths - the transcript tests use throwaway files in a temp directory, not this machine's own history.

## Why not a percentage

This was researched properly rather than guessed at - each row below was directly verified, not inferred from memory:

| Source | Result |
|---|---|
| `claude` CLI subcommands | No usage/quota command exists. Confirmed by listing all 34 documented commands and separately trying `claude usage` / `claude cost` directly - both are treated as plain prompts, not recognised as subcommands |
| `/usage` slash command | Real, but confirmed TUI-only: running `claude -p "/usage"` (non-interactive print mode) does not trigger it either - the text is passed straight through as a prompt |
| `~/.claude/.credentials.json` | Has real `subscriptionType` and `rateLimitTier` fields (e.g. `"team"`, `"default_raven"`) - genuine data, but plan/tier *labels*, not a usage number |
| `~/.claude/stats-cache.json` | Real local aggregate of message/token counts by day - but its own file-modified date was 18 days stale against 19 newer session files when checked, so it isn't reliably kept current, and it has no field for what your limit actually is |
| Session transcripts (`~/.claude/projects/*.jsonl`) | Real per-message token counts - this is what the widget now uses - but no limit/quota field anywhere in them |
| `policy-limits.json` | Organisation policy toggles, unrelated to personal usage |
| Anthropic Rate Limits API / usage-cost admin API | Documented only for Console API-key organisations, confirmed not extended to consumer OAuth subscriptions |
| GitHub issue [anthropics/claude-code#13585](https://github.com/anthropics/claude-code/issues) "Add Quota Information Access to Claude Code CLI" | **Open and unresolved.** Explicitly states quota % is only shown in the Claude UI and is "not accessible to Claude Code CLI users or automation scripts." Proposes a `claude quota` command, `CLAUDE_QUOTA_*` env vars, and `~/.claude/quota.json` - **none of these exist yet**. Worth flagging: some web summaries describe this issue's *proposal* as if already shipped |
| Community tool `ccusage` | Confirmed it reads the same local transcript files this widget does and estimates cost from token counts + a pricing table - not a live quota query |

**Bottom line:** no source anywhere gives the denominator (your plan's actual limit), so no percentage can be honestly computed. Token counts summed from real transcripts are the most accurate thing available - real data, just a different kind of number than "% used."

## Why fresh vs cached

Claude Code resends prior conversation context on every turn; `cache_read_input_tokens` covers that replay and is far cheaper (and likely counted differently against any real limit) than newly generated tokens. On a long-running session the total can look alarmingly large - one real reading here was **183M tokens**, of which only ~9M were `input_tokens` + `output_tokens` + `cache_creation_input_tokens` ("fresh"); the rest was cache replay. Showing one combined number without that context would be misleading, so the breakdown is one hover away on the big number.

## Known issues fixed along the way

- **White corners.** `#root`'s rounded corners were drawn over an opaque top-level window, so the four corners outside the curve showed through as solid white. Fixed with `Qt.WA_TranslucentBackground`; verified by checking the actual alpha channel of a rendered frame (corner pixel alpha was 0 after the fix), not just eyeballing it.
- **Badge width broke the title.** Renaming the badge to `LOCAL` (from a longer draft label) after visual verification showed `CLAUDE CODE USAGE` clipped to `CLAUDE CODE USAG` at 250px wide.

## Swapping in a different source later

The UI never talks to a data source directly. Everything flows through one small interface in `claude_usage_widget/usage_source.py`:

```python
class UsageProvider(Protocol):
    name: str
    def fetch(self) -> Optional[UsageSnapshot]: ...
```

To use a different source (e.g. if Anthropic ships anything from issue #13585 above), add a class implementing `fetch()` and return it from `build_default_provider()`. No UI changes needed. `badge_text` controls the small tag next to the title - set it to `None` for a source you consider authoritative enough not to need a caveat label.

Returning `None` from `fetch()` is supported and renders as "Usage data unavailable" rather than a stale or fake number.

## Not in V1

Deliberately out of scope: installer, auto-start at login, notifications/alerts, tray icon, multi-window.
