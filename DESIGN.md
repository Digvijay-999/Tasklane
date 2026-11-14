# Tasklane dashboard: design

Written before the screens were built (Phase 9) and revised after each screenshot critique round
(see `PHASE9_NOTES.md`, "Critique rounds"). The code follows this file; when they disagree, fix
one of them.

## 1. Subject and audience

**Subject:** an operations console for long-running AI processing jobs (PDF ingestion, repository
ZIP indexing) on one developer's machine. Every number on screen comes from the Tasklane API;
PostgreSQL is the source of truth.

**Audience:** engineers. They read dense tables, know what a lease or a retry budget is, and come
to the dashboard with one of three questions:

1. *Is the system healthy?* (Overview: counts, queue readiness, oldest queued job.)
2. *What is running?* (Overview, Jobs, and the live timeline.)
3. *What failed, why, and what can I do about it?* (Job detail: timeline, error, the one allowed
   action and the server's reason when it is not allowed.)

Answering those fast matters more than anything decorative. The dashboard is single-user,
local-first and never contacts another host.

## 2. References studied, and what was taken

| Reference | Taken | Not taken |
|---|---|---|
| **GitHub Actions run view** | A run is a sequence of steps, each with a state glyph and a duration aligned on the right; failures expand in place. Used for the timeline's stage cells. | The full log viewer; collapsible step logs. |
| **Temporal UI (workflow history)** | Event history is a numbered, filterable list with the raw event type visible, next to a higher-level view. Used for the event log under the timeline. | The compact/timeline "bubble" chart. |
| **Linear** | Status is a small glyph whose *shape* changes with the state (empty circle, half, check, cross), so it reads without color. Dense lists with quiet row separators. | Keyboard command palette (out of scope). |
| **Vercel dashboard** | The primary button is ink (black on light, white on dark), not a brand hue; hierarchy from type weight and spacing, not cards. | Large marketing-style headers. |
| **Sentry issue page** | The error is stated first, in the product's own words, with the next action right next to it. Used for the job error block. | Breadcrumb charts, stack traces (Tasklane keeps tracebacks in logs by design). |

## 3. Design tokens

All tokens are CSS custom properties in `frontend/src/styles/tokens.css`. Both themes are complete
sets: dark is chosen value by value, not inverted.

### 3.1 Color

Neutral base. Color is spent only on the seven job states. The primary action is **ink**: every
hue is already taken by a state, and a blue "primary" button would read as "running".

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | `#FFFFFF` | `#111214` | Page |
| `--bg-subtle` | `#F4F4F5` | `#1A1B1E` | Table headers, timeline track, `<pre>` |
| `--bg-hover` | `#ECECEE` | `#222428` | Row hover, pressed controls |
| `--border` | `#E3E3E6` | `#2A2C31` | Row rules, section rules |
| `--border-strong` | `#C9C9CF` | `#3B3E45` | Inputs, buttons, focus-less outlines |
| `--text` | `#17171A` | `#EDEDEF` | Primary text |
| `--text-muted` | `#575761` | `#A6A8B0` | Labels, secondary values (7.1:1 / 7.9:1) |
| `--text-faint` | `#6E6E79` | `#8C8F98` | Hints, "not reached" (5.0:1 / 5.8:1, AA) |
| `--ink` | `#17171A` | `#EDEDEF` | Primary button fill, focus ring |
| `--ink-contrast` | `#FFFFFF` | `#111214` | Text on ink |

**State colors** (text and glyph color; each ≥ 4.5:1 on `--bg` and `--bg-subtle` in its theme):

| State | Label | Glyph (shape carries the meaning) | Light | Dark |
|---|---|---|---|---|
| `queued` | Queued | dashed empty circle | `#5E6675` | `#A0A8B8` |
| `running` | Running | circle, right half filled | `#1E5BD6` | `#76A5FF` |
| `retry_wait` | Waiting to retry | open circular arrow | `#985700` | `#E3A13B` |
| `cancel_requested` | Cancelling | circle with a small square inside | `#A3238F` | `#E27BD2` |
| `succeeded` | Succeeded | filled circle, check cut out | `#17784C` | `#4CC38A` |
| `failed` | Failed | filled circle, cross cut out | `#C42B30` | `#FF7A75` |
| `cancelled` | Cancelled | filled rounded square | `#6B5E54` | `#BBAEA2` |

Measured contrast (script in the Phase 9 notes): light 5.0–6.6:1, dark 7.1–8.7:1.

**Color-blind check.** The dataviz skill's validator (`validate_palette.js`) was run on both sets.
It *fails* them as a categorical chart palette: amber/red are 2.8 ΔE apart under deuteranopia,
and queued/cancelled are deliberately low-chroma grays. That is acceptable here only because a
state is **never shown by color alone**: every badge and timeline mark pairs a distinct glyph
*shape* with a text label. Tests assert the label is present on every badge.

Tints: badges use no background fill. Timeline cells for failed/interrupted use the state color at
10 % opacity (`color-mix(in srgb, <state> 10%, transparent)`), never a gradient.

### 3.2 Typography

**IBM Plex Sans (variable)** for the interface, **IBM Plex Mono** for machine values only.
Self-hosted from `@fontsource` packages (bundled by Vite; no font CDN).

Why Plex: drawn for technical interfaces, with true tabular figures (`tnum`) and slashed-zero
support, so columns of durations, sizes and counts line up. Its slightly squared curves look
engineered without being a novelty face, and it is not the default sans of every generated UI.
The mono sibling shares metrics and tone, so a job id inside a sentence doesn't jump.

Mono is used for: job ids, upload ids, SHA-256 hashes, event `seq`, error classes, worker ids,
stage machine names in the event log, and file paths from manifests. Never for labels or buttons.

All numbers use `font-variant-numeric: tabular-nums` (global on `body`).

| Token | Size / line height | Weight | Use |
|---|---|---|---|
| `--text-xs` | 12 / 16 px | 400–500 | Table headers, hints, timeline durations |
| `--text-sm` | 13 / 18 px | 400–500 | Tables, event log, secondary text |
| `--text-md` | 14 / 20 px | 400 | Body default |
| `--text-lg` | 16 / 24 px | 600 | Section headings (h2) |
| `--text-xl` | 20 / 28 px | 600 | Page titles (h1) |
| `--text-2xl` | 28 / 32 px | 500 | The state counts on Overview (the only big numbers) |

Headings are sentence case and never preceded by an eyebrow label.

### 3.3 Spacing

4 px base: `--space-1` 4, `--space-2` 8, `--space-3` 12, `--space-4` 16, `--space-5` 24,
`--space-6` 32, `--space-7` 48. Table rows: 36 px (compact, readable). Page gutter 24 px
(16 px below 720 px). Sections are separated by 32 px and a 1 px rule, not by boxes.

### 3.4 Radii (deliberately not one value)

| Token | Value | Applies to |
|---|---|---|
| `--radius-0` | 0 | Tables, rows, section rules, the timeline grid |
| `--radius-1` | 2 px | Progress bars, timeline cells, duration bars, `kbd`-like tags |
| `--radius-2` | 4 px | Buttons, inputs, selects, filter chips |
| `--radius-3` | 8 px | The confirmation dialog only |
| full | 50 % | Status glyphs (drawn in SVG, not CSS) |

### 3.5 Borders, elevation, z-index

1 px `--border` rules separate rows and sections. Inputs and buttons use `--border-strong`.
One shadow exists, on the dialog (`0 8px 24px rgb(0 0 0 / 0.18)`), because it floats over
content. z-index scale: `--z-sticky` 10 (table header), `--z-banner` 20 (stale banner),
`--z-dialog` 30.

### 3.6 Motion

Only three things move, each answering a change:

1. A **new event row** fades in (opacity 0 → 1, 160 ms, ease-out).
2. **Progress** bars move with `transform: scaleX()` (200 ms, ease-out) when a new value arrives.
3. A timeline cell that **changes state** fades its new content in (160 ms).

No looping animation. `prefers-reduced-motion: reduce` removes all three (instant updates).

### 3.7 Focus and contrast

Focus ring: `2px solid var(--ink)` with `2px` offset, on `:focus-visible` only. All text pairs
meet WCAG AA (4.5:1) in both themes; numbers above.

## 4. Layout shell

```
┌───────────────────────────────────────────────────────────────────────────────────────────┐
│ Tasklane    Overview   Jobs                          API reachable    Theme ▾   [New job] │
├───────────────────────────────────────────────────────────────────────────────────────────┤
│ (stale banner here when the API is unreachable: "Can't reach the API. Showing data from   │
│  12:03:44. Retrying every few seconds.")                                                  │
│                                                                                           │
│ <page>                                                    max width 1280 px, 24 px gutter │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

- Top bar, not a sidebar: four destinations fit, and it leaves the full width for the timeline.
- "API reachable / unreachable" is a text status (glyph + words), driven by the last request.
- Theme control: a select with System, Light, Dark (default System, remembered locally).

## 5. Screens (wireframes)

### 5.1 Overview

```
Overview                                                              [New job]

Jobs by state                                                     142 jobs in total
 ┌─────────┬─────────┬──────────────────┬────────────┬───────────┬────────┬───────────┐
 │ ◌ Queued│◐ Running│↻ Waiting to retry│⊡ Cancelling│✓ Succeeded│⊗ Failed│■ Cancelled│  ← each a link
 │   2     │   1     │   0              │   0        │ 121       │  14    │   4       │    to Jobs?status=
 └─────────┴─────────┴──────────────────┴────────────┴───────────┴────────┴───────────┘
 (one row of columns divided by 1px rules; numbers 28 px tabular; no cards)

Queue ───────────────────────────────────────────────────────────────────────────────────
 Database        ✓ Reachable                     Oldest queued job   12 s   3f2a9c0e
 Redis broker    ✓ Reachable                     Messages waiting    2
 Dispatcher      ✓ Publishing normally           Scheduled retries   1
                 (or: ⊗ 2 messages failing to publish: ConnectionError, 5 tries)

Recent jobs ─────────────────────────────────────────────────────────── View all jobs
 State        File                 Workflow        Stage / progress         Created    Duration
 ◐ Running    report.pdf           PDF ingestion   Summarize  ████░░ 62 %   12:00:01   8 s
 ⊗ Failed     scanned.pdf          PDF ingestion   Extract text             11:58:40   1 s
```

### 5.2 New job

```
New job

Workflow
 (•) PDF ingestion              Extract text from a PDF and summarize it.  .pdf up to 50 MiB
 ( ) Repository ZIP indexing    Index a source archive's text files.       .zip up to 100 MiB

File
 [ Choose a PDF file ]  report.pdf   18.2 KiB                  ← drag and drop onto this row too
 ⊗ This file is 72.4 MiB; PDF uploads are limited to 50 MiB.   ← inline, aria-describedby

Fault injection (shown only when the server enables it)
 (•) Off   ( ) Fail the first N attempts  [1]   ( ) Fail permanently

[ Upload and start job ]   [Cancel upload] (while uploading)

Uploading report.pdf   ████████████░░░░░░░░  61 %   11.1 of 18.2 KiB
Creating job…
(network error) ⊗ The job request didn't reach the server. [Try again] reuses the same upload
                and idempotency key, so it can't create a second job.
```

### 5.3 Jobs (history)

```
Jobs                                                                  [New job]

Workflow  [All] [PDF ingestion] [Repository ZIP]          (segmented, radio semantics)
State     [◌ Queued] [◐ Running] [↻ Waiting to retry] [⊡ Cancelling] [✓ Succeeded] ...
          (toggle chips = checkboxes)                                       Clear filters
URL: /jobs?workflow=pdf_ingest&status=failed&status=running

 State      File            Workflow        Stage            Progress   Attempts  Created          Duration
 ✓ Succ...  report.pdf      PDF ingestion   —                100 %      1 of 3    14 Nov, 12:00    9.8 s
 ...
                                  [ Load more jobs ]   (cursor pagination; hidden on last page)
```

### 5.4 Job detail

```
Jobs / report.pdf
report.pdf                                                  [Cancel job]  [Retry job]
◐ Running   PDF ingestion   Job 3f2a9c0e…  ⧉   Created 12:00:01   Running for 8 s   Attempt 2 of 3
(Retry button disabled? the server's reason is printed under it: "Not retryable: the job failed
 because of its input. Upload a corrected file and create a new job.")

┌ Live updates: ● Live / Reconnecting… / Polling every 2 s (stream limit reached) / Finished ┐

Timeline                                                       (the signature element, §6)
 ...

┌ Error (failed jobs) ───────────────────────────────────────────────────────────────────────┐
 ⊗ pdf_no_text
 The PDF contains little or no extractable text (it may be scanned images). Scanned PDFs
 aren't supported yet.
 What you can do: this failure comes from the file itself, so retrying would fail the same way.
 Upload a text PDF and create a new job.   [New job]
(a left rule in the failed color, not a box)

Outputs                                      Attempts
 summary.md   2.1 KiB  [Download summary]     #  Trigger        Worker           Outcome   Error class  Duration
 manifest.json 1.0 KiB [Download manifest]    1  First run      worker@a1b2:63   Failed…   injected_…    0.4 s
 Summary preview (plain text <pre>)           2  Automatic retry ...

Manifest (repo jobs)
 Totals: 214 files, 120 indexed, 94 skipped, 1.4 MiB, 31,200 lines
 Languages table | Skip reasons table | Included files (filter, 100 at a time) | Skipped files

Event history                                  [Filter events…]  [ ] Show progress events
 seq  Time       Attempt  Event               Details
 1    12:00:01   —        job.created          workflow_type pdf_ingest
 ...  (34 progress events hidden)
```

## 6. The timeline (signature element)

The one place with visual boldness. Rows are **attempts** (lanes); columns are the workflow's
**stages**, in order, from `GET /v1/meta` (never hardcoded). Because columns line up across
attempts, a retry that reused checkpoints shows a column of "Reused" cells, and the attempt where
something broke is obvious at a glance.

```
                 Validate   Extract text  Chunk     Summarize        Write artifacts
Attempt 1        ✓ 0.7 s    ✓ 1.9 s       ✓ 0.1 s   ⊗ 0.3 s          ·
First run        ▬          ▬▬▬▬▬         ▏         injected_transient
worker@a1b2:63                                      transient
3.0 s
  │ ↻ Waited 4.1 s, then retried (injected_transient is a transient error).
Attempt 2        ┆↷ Reused┆ ┆↷ Reused ┆   ┆↷ Reused┆ ◐ 62 %  6.2 s     ·
Automatic retry                                    ████████░░░░
  │ (live: the running cell's bar and elapsed time move; others stay still)

Final row:  ✓ Succeeded in 12.9 s        (or ⊗ Failed: <server message>; ■ Cancelled before
                                          Chunk; ⊡ Cancelling: stops after Summarize)
```

**Cell states** (glyph + words + one visual treatment each, never color alone):

| Cell | Glyph | Text | Treatment |
|---|---|---|---|
| Not reached | small dot | none (screen readers: "not reached") | faint |
| Running | half circle | `62 %` and elapsed `6.2 s` (ticks every second) | running-blue progress bar inside the cell |
| Done | check | duration | neutral ink; a 3 px duration bar under it, width ∝ this stage's share of the longest stage in the job |
| Reused | curved arrow | "Reused" | dashed outline (no fill): the stage did not run in this attempt |
| Failed | cross | duration, `error_class` (mono), "transient" or "permanent" | failed-red text, 10 % red tint |
| Interrupted | broken circle | "Worker lost" | retry-amber text, 10 % amber tint (the lease expired mid-stage) |
| Not run (cancelled) | square | "Not run" | faint, cancelled glyph |

**Lane header:** "Attempt N", the trigger in words (First run, Automatic retry, Recovery,
Manual retry), worker id (mono, truncated with the full value in `title`), and the attempt's
duration.

**Between lanes** (a 1 px vertical connector with one sentence):

- retry wait: "Waited 4.1 s, then retried (`injected_transient` is a transient error)." While
  still waiting: "Retrying in 3 s." (counts down from `retry_at`).
- lease expired: "The worker stopped renewing its lease (last heartbeat 12:00:31). Recovery took
  the job back at 12:01:32."
- manual retry: "Retried by hand (manual retry 1). New budget: attempts 4 to 6."
- lost message: "The queue message was lost; recovery sent a new one."
- cancel requested (inside the lane): "Cancel requested at 12:00:05. The worker stops at the next
  stage boundary."

**Before the first attempt:** "Queued, waiting for a worker" with the wait time.

**Final row:** the job's terminal state glyph and label in its state color, with total duration,
the server's `error_summary` for failures, and "cancellation was requested but the job finished
first" when a `job.cancel_superseded` event exists.

**Live behavior:** the grid has a fixed row height per lane and every stage column exists from
the start (pending cells), so arriving events change cell contents, never the layout above them.
A new lane is appended at the bottom. The cell that changed fades its content in (160 ms;
disabled with reduced motion). Ordering is by `seq`, never `created_at`.

**Narrow widths:** below 720 px of container width the grid becomes a list per attempt (stage
name, then the cell), using a CSS container query.

**Accessibility:** the timeline is a `<table>` (attempts = rows, stages = column headers) with a
caption; each cell has full text ("Summarize: failed after 0.3 s, injected_transient,
transient"). A polite live region announces job status changes only (not every progress tick).

## 7. States

| State | Where | What the user sees |
|---|---|---|
| Loading | every screen | Skeleton rows/blocks with the final layout's dimensions (no spinners, no layout shift) |
| Empty | Overview | "No jobs yet. Create one to see it here." + [New job] |
| Empty | Jobs | "No jobs match these filters." + [Clear filters]; with no filters: "No jobs yet." + [New job] |
| Empty | Event history filter | "No events match "x"." + [Clear filter] |
| API unreachable | everywhere | Banner: "Can't reach the API. Showing data from 12:03:44. Retrying every few seconds." Values stay visible, dimmed, with "stale" in the section heading. Never blanked. |
| Database down (503) | Overview, Jobs | Same banner, wording from the server's safe message |
| Stream connecting | Job detail | "Connecting to live updates…" |
| Live | Job detail | "● Live" (running color dot + text) |
| Stream disconnected | Job detail | "Live updates paused, reconnecting…" (+ attempt count); resumes from the last seq |
| Stream limit reached | Job detail | "The server's live-stream limit is reached. Checking for new events every 2 s instead." (polling the history route; SSE retried every 30 s) |
| Finished | Job detail | "Job finished. Live updates stopped." (EventSource closed on `stream.end`) |
| Job not found | Job detail | "No job with this id." + [Back to jobs] |
| Error from an action | Cancel / Retry | Inline under the button: server `message` and `code` (mono) |
| Upload / create error | New job | Inline under the button, server `message` and `code`; [Try again] when retryable |
| Idempotent replay | New job → Job detail | Notice on the job page: "The server already had this submission, so it returned the existing job instead of creating another (Idempotent-Replayed)." |

## 8. How this avoids generic defaults

Checked item by item against the "Do NOT use" list in the Phase 9 brief.

| Generic tell | This design | Changed from my first idea |
|---|---|---|
| Gradients or glows | None. Tints are flat `color-mix`; the "reused" cell is a dashed outline. | First idea hatched reused cells with a `repeating-linear-gradient`; replaced by a dashed outline so no gradient exists anywhere. |
| Glassmorphism | No blur, no translucency over content. | — |
| Purple/blue "AI" palette | Neutral grays; blue appears only as the *running* state. | Accent was going to be blue; made it ink so blue keeps one meaning. |
| Cream + terracotta / near-black + acid green | Light is plain white/zinc; dark is a neutral #111214 with no neon. | — |
| Sparkle, robot, magic icons; emoji | Seven geometric state glyphs plus a few UI glyphs, all simple SVG. No emoji anywhere (a test greps the source). | — |
| Hero banners, "Welcome back" | The Overview opens on the numbers. | — |
| Identical rounded cards with soft shadows | Counts are one row of columns split by rules; sections are headings + rules. One shadow exists (dialog). | First wireframe had seven count cards; collapsed into one ruled strip. |
| One border radius on everything | Four radii with stated uses (§3.4). | — |
| ALL-CAPS eyebrow labels | None. Table headers are sentence case, `--text-xs`, muted. | — |
| Meta strings joined with middle dots | Metadata is a definition list / spaced columns with labels. | Header line was "Running · PDF · 3f2a…"; became labelled fields. |
| "→" on buttons and links | None. Links say where they go ("View all jobs"). | — |
| Decorative charts | No charts. The only bars are progress and stage durations, both real values. | — |
| Invented or placeholder metrics | Every number comes from the API; nothing is shown when the API has no value ("—" means "not applicable", e.g. no duration before a job starts). | Dropped a "success rate" idea: the API doesn't provide it and it would be computed from a partial page. |
| Lorem ipsum | Real copy; empty states say what to do next. | — |
| Spinners where a skeleton fits | Skeletons sized like the content. | — |
| Toasts for inline things | No toast system at all; action results and errors appear next to the control. | — |
