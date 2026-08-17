# claude-usage-widget

A small always-on-top desktop widget showing Claude Code usage. Windows, Python + PySide6.

## Features

- Compact frameless window, true rounded corners (no white edge - see [Known issues fixed](#known-issues-fixed-along-the-way))
- **5H** and **Weekly** rate-limit percentages, each with a reset time (local timezone) and a countdown to that reset
- Three-tier data priority for those percentages, each clearly labelled so a reading is never mistaken for a different kind of number:
  1. **OFFICIAL** - Anthropic's own `rate_limits` figures, captured from Claude Code's status line (see [Reading official rate limits](#reading-official-rate-limits)). Used whenever a fresh reading exists.
  2. **ESTIMATED** - a local estimate anchored on a previously-observed rate-limit hit, used only when no official reading is available. Only ever covers a 5-hour-shaped window.
  3. **LOCAL** - a plain token count summed from this machine's own Claude Code session transcripts, shown separately since it answers "how much was used," not "what fraction of a limit."
- Hover the local count for the exact number, split into **fresh** (new input/output/cache-writes) vs **cached** (replayed context) - see [why that split matters](#why-fresh-vs-cached)
- **Always on top** toggle
- **Opacity** slider, 30%–100%
- Drag anywhere on the window to move it
- Remembers window position, opacity, and always-on-top between runs
- Closing the window hides it instead of quitting, so the next wake-up skips Qt's process cold-start cost
- A global hotkey (`Ctrl+Shift+Alt+U` by default) brings the widget to the front instantly while it's running - see [Fast wake-up](#fast-wake-up)

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

Click the `✕` in the top-right corner to hide it (see [Fast wake-up](#fast-wake-up) for how to bring it back). Settings are saved on close.

## Reading official rate limits

Claude Code's status line payload includes real `rate_limits.five_hour` / `rate_limits.seven_day` figures (`used_percentage`, `resets_at`) when a `statusLine` hook is configured. `claude_usage_widget/statusline_hook.py` is a small script that captures those figures to `~/.claude-usage-widget/official_rate_limits.json` on every render; wire it into your global `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "pythonw /path/to/claude-usage-widget/claude_usage_widget/statusline_hook.py"
  }
}
```

`OfficialRateLimitProvider` (`claude_usage_widget/rate_limits.py`) reads that capture file and is used whenever it's fresh (see `DEFAULT_MAX_AGE`); without it, the widget falls back to the estimate/local tiers described above.

## Fast wake-up

The widget hides rather than quits on close, so most of the time it's already running in the background. Two ways to bring it back:

- **Global hotkey** - while running, the widget registers `Ctrl+Shift+Alt+U` itself (`RegisterHotKey`) and reacts to it directly, with no new process involved. This is near-instant (single-digit milliseconds measured).
- **`launcher/`** - a small precompiled C# launcher (`Launcher.cs`, built with `csc.exe` - see the comment at the top of that file for the exact command) that either wakes an already-running widget or cold-starts one if none is running. This is what a Start Menu shortcut or an autostart-at-login shortcut should point at, since neither of those can rely on the hotkey being registered yet.

  The launcher wakes the widget through a small loopback TCP listener (`WAKE_PORT` in `widget.py`) rather than calling `ShowWindow`/`SetForegroundWindow` on its native handle directly - see the comments around `WAKE_PORT` for why that distinction matters (a direct external `ShowWindow` call left Qt's own mouse-event routing desynced: painting and opacity kept working, but dragging, the close button, and the opacity slider all stopped responding).

## Tests

```powershell
python selftest.py
```

Builds the real widget and exercises it end to end - the official/estimated/local data priority chain, warning-level thresholds, the always-on-top flag, opacity limits, the settings round-trip, recovery from a saved position that is no longer on any connected monitor, and that closing hides rather than tears down the widget. All against real code paths - the transcript tests use throwaway files in a temp directory, not this machine's own history.

## Why the local count exists at all

Before official rate limits were confirmed reachable from the status line, no local source exposed anything shaped like a percentage. This was researched properly rather than guessed at - each row below was directly verified, not inferred from memory:

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

Deliberately out of scope: installer, notifications/alerts, tray icon, multi-window.
