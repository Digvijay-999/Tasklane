# Phase 9 notes: the dashboard

Each decision: **what** was decided, the **alternatives**, **why**, and a **plain-language**
version. The short form is in `CLAUDE.md` ("Decisions made (Phase 9)"); the visual design is in
`DESIGN.md`; the new routes are in `docs/api.md` ("Dashboard routes"); the UI threat model is in
`docs/security.md` ("The dashboard").

Node comparison for the whole phase: this is the part you know best. React + Vite is the usual
SPA setup; TanStack Query is the `useQuery` you may know from React projects; React Router is the
same library you'd use with Express backends. The new things are on the edges: Server-Sent Events
(`EventSource`), upload progress (XMLHttpRequest), and a strict Content-Security-Policy.

---

## 0. Skills and process

**Skills loaded** (before any design decision): `baseline-ui` (anti-slop UI rules),
`fixing-accessibility` (names, keyboard, focus, forms, announcements, contrast),
`fixing-motion-performance` (compositor-only animation, reduced motion), `improve-ui` (auditing
against the product's own design evidence), and `dataviz` (status colors, stat tiles, its palette
validator). **No skill named "taste" is installed** (`~/.claude/skills` and the plugin cache
were searched); `baseline-ui` and `improve-ui` were used for the taste critique instead.

**Process followed:** plan (the brief and the screens) → `DESIGN.md` written and checked against
the "Do NOT use" list before any screen was built ("How this avoids generic defaults" records
what that check changed) → build → screenshot critique with Playwright, three rounds (§11) →
final screenshots in `docs/screenshots/`.

**Where I deviated from a skill, and why.**
- `baseline-ui` says "MUST use Tailwind CSS" and `motion/react`. The brief asks for an explicit,
  named token system (hex values per theme, type, spacing and radius scales), and the only motion
  is three CSS fades/transforms. Plain CSS with custom properties expresses that directly, with no
  build plugin and two fewer dependencies. Its other rules were followed: tabular numbers,
  skeletons, no gradients, an AlertDialog for the destructive action, a fixed z-index scale,
  compositor-only animation, reduced motion, errors next to the action, one accent.
- `dataviz`'s validator fails the seven state colors *as a chart palette* (§4.3). That is
  accepted only because no state is ever shown by color alone.

---

## 1. Frontend versions (locked 2026-11-14, checked against npm that day)

Rule used: the latest stable release of each package on npm today, exact versions in
`package.json`, `package-lock.json` committed, installed with `npm ci`. `npm audit --omit=dev`:
0 vulnerabilities. Vite 8.3.7/8.3.8 were days old, but they arrived together with 8.1.7, 7.3.8 and
6.4.6 on the same day (a coordinated patch release), so staying on an older patch would have
been the riskier choice.

| Package | Version | Why this library |
|---|---|---|
| Node | 24.21.0 (local) / `node:24.21.0-bookworm-slim` (Docker) | LTS line already installed on the developer's machine; same major in both places, so the lockfile resolves the same. Debian slim (glibc) rather than Alpine (musl) because Vite 8's native parts (rolldown, lightningcss) are most widely tested on glibc. |
| react, react-dom | 19.3.1 | Required by the brief. |
| typescript | 7.0.2 | Current stable (the native compiler). Only used for type checking (`tsc --noEmit`); Vite transpiles with its own tools, so the compiler change can't affect the bundle. |
| vite, @vitejs/plugin-react | 8.3.8, 6.3.0 | Required by the brief; dev server with a proxy (one origin, no CORS) and `vite preview` for testing the production build. |
| react-router | 8.5.1 | **Routing.** Four routes plus URL search parameters for the job filters (`useSearchParams`); the standard library, works in memory for tests (`createMemoryRouter`). A hand-written router would need its own history and link handling. |
| @tanstack/react-query | 5.107.1 | **Data fetching and caching.** Polling (`refetchInterval`), cursor pages (`useInfiniteQuery`), and above all: when a refresh fails it *keeps the last data* and reports the error, which is exactly what "show the last known state, marked stale" needs. Hand-written `useEffect` fetching gets this wrong easily (races, flashes of empty state). |
| @base-ui/react | 1.9.0 | **Accessible headless primitives.** Used for the one widget with real focus behavior: the "Cancel this job?" AlertDialog (focus trap, Escape, focus return, inert page). Headless: it ships no look, so the design stays ours. Its `CSPProvider disableStyleElements` keeps it from injecting `<style>` tags under the CSP. Everything else is native HTML (buttons, radios, checkboxes, select, table), which needs no library. |
| @fontsource-variable/ibm-plex-sans, @fontsource/ibm-plex-mono | 5.3.0 | **Self-hosted fonts.** The font files are bundled by Vite and served from our own origin; no font CDN request. |
| vitest, jsdom | 5.1.0, 31.0.1 | **Unit/component tests**, Jest-compatible API, shares Vite's config. |
| @testing-library/react, dom, user-event, jest-dom | 16.3.3, 10.4.2, 14.6.10, 7.0.1 | Tests that use the page the way a person does (roles, labels, accessible descriptions). |
| @playwright/test | 1.65.0 | **End-to-end tests and screenshots** in a real Chromium against the real stack. |
| @types/node, @types/react, @types/react-dom | 24.19.2, 19.3.0, 19.3.0 | Types. |

Deliberately **not** added: a UI kit (it would dictate the look), Tailwind (above), a Markdown
renderer (the summary is shown as plain text, §8), an icon library (eight small SVG glyphs drawn
for their shapes), a date library (`Intl.DateTimeFormat`), ESLint (TypeScript strict mode plus the
tests are the check for now).

Plain language: every library does one job the browser doesn't do well by itself, and each is
pinned to an exact version so `npm ci` installs exactly what was tested.

---

## 2. Backend additions

### 2.1 `GET /v1/overview`

**Decision.** One read-only service (`services/overview.py`) with three queries
(`db/repositories/overview.py`): `SELECT status, count(*) ... GROUP BY status`; the oldest queued
job with `extract(epoch from now() - updated_at)`; and an outbox summary (pending, scheduled,
failing, max dispatch attempts, last error type, age of the oldest pending message). A message
whose publish failed counts as *pending* and its age runs from `created_at`: the dispatcher's
backoff moves `available_at` forward after each failure, so measuring from it would make an
hour-long Redis outage look like a few seconds (found in the code review, §13). The route adds a Redis
ping (the same `run_checks` as `/health/ready`, so the same 2 s deadline).

**Alternatives.** Count in the browser from `GET /v1/jobs` pages (wrong: the list is paged, so the
numbers would be partial); a new index for the counts (unneeded: `ix_jobs_status_created_at_id`
starts with `status`, and a test shows the planner can use it); measure "queued since" from
`created_at` (wrong for a job reopened by a manual retry days later).

**Why `updated_at` is "queued since".** Nothing updates a queued row except a status change into
`queued` (creation or manual retry): progress writes only touch running jobs, and a lost-message
requeue only appends an event. The recovery loop already relies on the same fact.

**Why the database clock.** The browser's clock (or the API container's) can be off; `now()` in
the same statement as the row is the one clock every component agrees on.

Plain language: the overview asks the database "how many jobs in each state, and how long has the
oldest one waited?", and the database answers with its own clock.

### 2.2 `GET /v1/meta`

**Decision.** Upload limits, extensions, content types, signature bytes, the stage list with
weights, the filename limit, the upload TTL, and whether fault injection is on, read from the
running server's settings. The stage list lives in `domain/workflows.py` (pure data); a unit test
asserts it equals `(name, weight)` of the stages the worker really builds.

**Alternatives.** Hardcode limits in the UI (forbidden by the brief, and wrong as soon as someone
changes `.env`); import the stage classes in the API (would load the PDF/ZIP workflow modules into
the API process just to read two strings each).

Plain language: the dashboard asks the server for the rules instead of guessing them, so changing
a limit in `.env` changes what the page accepts.

### 2.3 `upload_filename` in the job list

**Decision.** List items carry the upload's original filename, fetched with one primary-key query
for the page's upload ids (`repositories/uploads.original_filenames`). Duration needs nothing new:
`started_at` and `finished_at` were already in every item.

**Alternatives.** Join `uploads` into `list_jobs_statement` (would change the query whose index
use is tested, for a column that is display-only).

---

## 3. Layout, typography, and the accent color

(Full detail in `DESIGN.md`; reasons here.)

- **Top bar, not a sidebar.** Four destinations fit in a bar, and the timeline gets the full
  width.
- **The primary action is ink** (black on light, white on dark), not a brand hue. Every hue is
  already used by a job state; a blue "New job" button would read as "running". Engineering tools
  people trust do the same.
- **IBM Plex Sans + Plex Mono.** Drawn for technical interfaces, has true tabular figures (columns
  of numbers line up), and isn't the default font of every generated UI. Mono only for machine
  values (ids, hashes, seq, error classes, paths).
- **No cards.** Sections are a heading and a rule; the seven counts are one ruled strip.

Plain language: color means "job state" and nothing else, so you can scan the page for color and
find what needs attention.

---

## 4. Job states: glyph + label + color

### 4.1 Decision
Seven SVG glyphs whose *shape* differs (dashed ring, half-filled, circular arrow, ring with a
square, check, cross, filled square), always next to a text label, in the state's color.

### 4.2 Alternatives
Colored pills (color carries the meaning alone: fails color-blind users); emoji (banned, and render
differently per OS).

### 4.3 The color-blind check
The dataviz validator (`validate_palette.js`, all pairs) reports amber/red only 2.8 ΔE apart for
deuteranopes, and gray/warm-gray too close for everyone. Seven distinct, accessible hues for
seven states is not achievable; the design therefore never relies on hue: shape and label carry
the state. Text contrast of every state color was measured: 5.0–6.6:1 (light), 7.1–8.7:1 (dark).

---

## 5. The timeline (the signature element)

**Decision.** A table: rows are attempts, columns are the workflow's stages (from `GET /v1/meta`).
Cells: not reached, running (progress bar + elapsed), done (duration + a bar proportional to the
longest stage), reused (dashed outline: the checkpoint was verified, the stage didn't run),
failed (error class + transient/permanent), interrupted ("Worker lost": lease expired), not run
(cancelled). Between rows, one sentence for what happened: the retry wait (with the real wait),
the lease expiry, a manual retry with its new budget, a lost message. A final row states the
outcome. Built by a pure function `buildTimeline(events, stages)` (`timelineModel.ts`) with unit
tests per scenario.

**Alternatives.**
- *A vertical list of events* (that is the event history, which stays below it): it doesn't show
  where the retry resumed.
- *A Gantt chart on a time axis*: stage durations range from 10 ms to minutes, so short stages
  vanish, and wall-clock gaps (retry waits) dominate. Proportional bars inside equal columns keep
  every stage readable and still show where the time goes.
- *Rows = stages, columns = attempts*: grows sideways with every retry.

**Why columns.** Because each stage has a fixed column, a resumed attempt shows a vertical run of
"Reused" cells and the failed cell sits right above the stage that then succeeded. That is the
point of checkpointing, made visible.

**Live and no layout shift.** Every stage column exists from the start, cells have a fixed height,
and only the changed cell fades its content in (`key={state}`), so progress ticks don't animate.
A new attempt is appended at the bottom.

**Narrow widths.** A CSS container query turns the grid into a list per attempt under 720 px.

Plain language: read a row to see what one attempt did; read a column to see what happened to one
stage across attempts.

---

## 6. Live data (Phase 8 rules)

All in `src/lib/live/jobEventStream.ts` (a plain class, wrapped by `useJobEvents` through
`useSyncExternalStore`), unit-tested with a fake `EventSource`.

| Rule | How |
|---|---|
| Snapshot and history first | `GET /v1/jobs/{id}` (React Query) and every page of `/events/history` load first; then `EventSource('/v1/jobs/{id}/events?after=<last seq held>')`. |
| De-duplicate, order by seq | Events go into a `Map` by `seq`; only the contiguous prefix 1..N is shown. A replayed event (`seq <= N`) is ignored. |
| Gaps | Event 7 when we hold 5: 7 is held back, the history route is read from 5, and the prefix advances once 6 arrives. If the history itself lacks seqs (deleted, Phase 12), they are counted and shown ("N events are missing"), never silently skipped. Event types the client doesn't listen for are caught the same way: the next known event shows the gap. |
| `stream.end` | `close()`, mode "ended". The job snapshot is re-read. |
| Dropped connection | readyState CONNECTING: the browser reconnects itself with `Last-Event-ID`; the page says "Live updates paused, reconnecting". |
| Browser gave up (CLOSED) | EventSource can't tell why, so the class probes the same URL with `fetch`: `503 stream_limit_reached` → poll the history route every 2 s ("Checking for new events every 2 s instead"), retry the stream every 30 s, stop polling when the job is finished and caught up; `404 job_not_found` → stop; `200` → abort the probe (frees the server slot) and reconnect; anything else → reconnect with backoff 1, 2, 4, 8, 15 s, resuming with `?after=`. |
| Page refresh | The history is reloaded, so `?after=` starts from what the history returned (EventSource can't send `Last-Event-ID` after a refresh). The e2e test reloads mid-job and checks the seqs are exactly 1..last with no duplicates. |
| After Cancel / Retry | The job is re-fetched (and after Retry the stream reopens); nothing is changed optimistically. |
| Job snapshot freshness | Re-read about 150 ms after any non-progress event and at most once a second for progress, plus every 5 s while the job isn't finished (an expiring lease produces no event until recovery acts). |

**Stale data.** React Query keeps the last data when a refresh fails. Each section then shows
"Not current: last updated 12:03:44 (can't reach the API). Retrying." and dims its values, and a
banner under the top bar says the API is unreachable. A stale health check never wears the
green/red of a current one ("Last known: Reachable", found in critique round 1).

Plain language: the page first reads everything stored, then listens for new events starting
right after the last one it has. If anything is missing, it asks the history again. If it can't
listen, it says so and checks every 2 seconds instead.

---

## 7. New job

- **Validation first, from `GET /v1/meta`.** Extension for the chosen workflow, size limit, empty
  file, filename length, control characters and text-direction characters (the server's own
  rules), and the first bytes against the signature (`%PDF-`, `PK\x03\x04`). Errors are linked to
  the file input (`aria-describedby`, `aria-invalid`) and the button explains nothing is sent.
- **Upload with XMLHttpRequest** (fetch has no upload progress), with a "Cancel upload" button.
- **Idempotency.** One submission = one key from `crypto.randomUUID()`. The submission is
  fingerprinted (workflow, file name/size/modified time, fault option). If creating the job fails
  without a clear answer (network error, 5xx), "Try again" re-sends the same body with the same
  key and the already uploaded file; any change of file, workflow or option starts a new key.
  A `422 upload_*` keeps the key (validation errors don't use it up) but uploads again.
- **Replay shown clearly.** `200 + Idempotent-Replayed: true` stays on the page with "The server
  already had this submission, so it returned the existing job instead of creating a second one",
  and a link. A new job (201) opens its detail page directly.
- **Fault injection** controls appear only when `fault_injection_enabled` is true.

Plain language: the form checks the file against the server's rules before uploading, and if the
connection drops at the worst moment, trying again can't create a duplicate job.

---

## 8. Security in the UI

- **`SafeText`** renders every upload-derived string: React escapes markup, `<bdi>` isolates
  direction, and control/format/separator characters (`\p{Cc}\p{Cf}\p{Zl}\p{Zp}\p{Cs}`) become
  visible `‮` tokens. Never `dangerouslySetInnerHTML` (a test scans the source); no linkify
  anywhere.
- **`summary.md` is shown as plain preformatted text.** Alternative: render Markdown with raw HTML
  disabled and links inert. Rejected: a Markdown renderer is a dependency and an attack surface,
  and the repository summaries are already in fenced blocks meant to be read as text.
- **Manifest** parsed defensively (wrong types become empty), capped at 16 MiB.
- **CSP** for the production build: a `<meta>` tag injected only at build time (the dev server
  needs inline scripts for hot reload), and the same policy plus `frame-ancestors 'none'` as a
  header from `vite preview`. To make it strict: the theme script is an external file, assets are
  never inlined as `data:` URIs (`assetsInlineLimit: 0`), Base UI's style injection is disabled,
  and React's `style={{ }}` is set through the CSSOM, which `style-src 'self'` allows.
  Verified: every Playwright e2e test runs on the production build and fails on any console error.
- **One origin.** Vite proxies `/v1` and `/health` to the API; no CORS headers on the API.
- **127.0.0.1.** The dev server binds to 127.0.0.1 on Windows; in Docker it listens on the
  container's interface (it must, for Docker's port mapping) and Compose publishes
  `127.0.0.1:5173` only.

---

## 9. Compose and live reload

**Decision.** A `frontend` service (`node:24.21.0-bookworm-slim`, image built with `npm ci`)
running Vite with `server.watch.usePolling` (`TASKLANE_DEV_POLLING=true`). Only `src/`,
`public/`, `index.html` and `vite.config.ts` are bind-mounted; `node_modules` stays the one in the
image. The process runs as the image's unprivileged `node` user, which owns `/app` (Vite writes a
temporary bundle of its config file there; the first build failed with `EACCES` until that was
fixed).

**Alternatives.**
- *Run Vite on Windows with the installed Node 24 only.* Works (documented in the README:
  `npm ci; npm run dev`), and is what the e2e tests use. But the brief asked for the dashboard in
  Compose, and one `docker compose up` for everything is simpler for a reviewer.
- *Mount the whole `frontend/` folder.* Rejected: it would mount a Windows `node_modules` (with
  Windows builds of rolldown and lightningcss) into Linux, or need an anonymous volume that goes
  stale after dependency changes.

**Why polling.** The Phase 1 lesson: file-change events from the Windows filesystem don't reach
Linux containers. Verified: an edit made on Windows produced an `hmr update` in the container
within about 3 s.

Plain language: the dashboard runs in a container like everything else, and it checks your files
for changes every 300 ms because Windows doesn't tell the container when you save.

---

## 10. Tests

- **Vitest (40 tests, about 5 s):** the stream class with a fake EventSource (history then
  `?after=`, duplicates, `stream.end` closes and never reconnects, gap fill, browser reconnect,
  self-reconnect with backoff, 503 → polling and back, deleted job, reload without duplicates via
  the hook); the timeline model (retry with reused checkpoints, lease expiry → recovery,
  cancellation, manual retry, superseded cancel, cancelled while waiting); Cancel/Retry from
  `can_cancel`/`can_retry` and their reasons, server error shown inline, confirmation dialog,
  re-fetch instead of optimistic update; upload validation against server limits; idempotent
  replay display and key reuse/renewal; hostile file names inert (bidi, invisible, `javascript:`,
  HTML, URLs) in `SafeText`, the jobs table and the manifest parser; source scans for
  `dangerouslySetInnerHTML` and emoji (both checked to fail on a planted violation).
- **Playwright e2e (6 tests, about 50 s, real stack and worker, production build):** PDF to
  succeeded live + summary download; scanned PDF fails with `pdf_no_text` and Retry disabled with
  its reason; cancel a running job (stops before Write artifacts); reload mid-job (seqs exactly
  1..last); fault-injected job retries live (second attempt reuses 3 checkpoints); repo ZIP manifest
  with skip reasons and path filter. Every test fails on any browser console error.
- **Backend:** new integration tests for the overview (counts, oldest-queued age by the DB clock,
  dispatch failures, unreachable broker, index use), metadata (limits come from settings, fault
  flag) and the list's `upload_filename`; the stage-list sync test. Full suite after the last fix:
  see the final summary.

---

## 11. Critique rounds (screenshots reviewed against DESIGN.md and the "Do NOT use" list)

**Round 0 (first render, scratch screenshots).**
- Progress percentages wrapped ("100 / %") → one line, and `100%` (English style, no space).
- A green bar in every finished row repeated the state → bars only for unfinished jobs; finished
  rows show the number, muted.
- Very fast stages read "0.0 s" → durations under 1 s in milliseconds.
- Attempts crammed beside Outputs (Error class cut off) → full width, directly under the timeline.
- Native file button unstyled → styled like the other buttons.

**Round 1 (`docs/screenshots`, first full set).**
- Bug found by looking: screenshots came from a *stale* preview server, and helper-created jobs
  showed full Windows paths as names (a heredoc ate a backslash in a regex) → Playwright now
  always builds fresh (`reuseExistingServer: false`); the helper uses `path.basename`.
- The attempt duration was an unlabeled number under the trigger → "Ran 1.4 s" / "Running for".
- Event-log times were right-aligned, leaving a gap after Seq → left-aligned.
- The final row of a running job just repeated "Running" → "Running attempt 1, now in Summarize"
  / "Waiting to retry, next attempt in 3 s" / "Cancelling: the worker stops after ...".
- Detail page showed a progress bar for finished jobs → removed, like the table.
- **Stale overview showed Redis and Dispatcher in green** while the API was down: stale data
  presented as current → "Last known: Reachable", neutral.
- Phone width: the event table squeezed its details column into a 6,000 px tower → wide tables
  scroll sideways (`min-width`).

**Round 2.**
- Action reasons under the two header buttons wrapped raggedly and right-aligned → one
  left-aligned list under the buttons (each reason names its action; still linked to its button
  with `aria-describedby`).
- The failed job's final row repeated the full error sentence that "Why it failed" shows right
  below → "Failed in Extract text: pdf_no_text."
- "Started" and "Seq" values aligned differently from their headers → aligned.
- Seven counts at equal weight hid the non-zero ones → zero counts recede (faint, regular weight).

**Round 3.**
- Skip reasons wrapped ("Unsupported / type") → no wrap.
- The New job file row showed the file name twice (native control + hint) → the hint shows only
  what the control doesn't: the size.
- Language names looked amber in the dark manifest screenshot → checked the computed color
  (`#EDEDEF`, the text color): an artifact of the downscaled image, no change.

Checked every round against the list: no gradients, glows, glass, purple/blue palette, emoji,
hero, cards, single radius, eyebrows, middle-dot meta strings, arrow buttons, decorative charts,
invented numbers, lorem ipsum, spinners or toasts.

---

## 12. Carry-forward items from earlier phases (all Phase 9 items)

| Item | Where it is handled |
|---|---|
| Rendering `summary.md` without raw HTML, active links or auto-linked URLs; escape manifest paths | Plain `<pre>` with `SafeText` (§8); manifest paths through `SafeText`; tests in `SafeText.test.tsx` |
| Queue health: `dispatch_attempts`, `last_dispatch_error`, oldest queued job's age | `GET /v1/overview` → Overview "Queue" section (dispatcher failing messages with tries and error type, oldest queued job with link) |
| Actions and leases: use `can_cancel`/`can_retry` and reasons; show `lease.expired`; attempt outcomes `lease_expired`/`cancelled`, triggers `recovery`/`manual_retry` | `JobActions.tsx` (tested); "Worker not responding" notice from `lease.expired`; timeline "Worker lost" cells and lease-expiry sentence; Attempts table outcomes and triggers in words |
| Untrusted text: never auto-link file names, archive paths, manifest entries, event payloads | `SafeText` everywhere, event payloads rendered as text; no linkify; tests |
| CORS / dev proxy: Vite proxy `/v1` → API, no CORS | `vite.config.ts` proxy (dev and preview), Compose `TASKLANE_API_URL=http://api:8000` |
| EventSource usage: `addEventListener` per type; resume with `?after=` after reload; close on `stream.end`; on CLOSED reconnect with backoff and `?after=`; order by seq; de-duplicate | `jobEventStream.ts` (§6), unit and e2e tests |

---

## 13. Code review (code-reviewer agent, read-only) and what changed

No high-severity findings. Fixed:

1. **Medium: a reconnect timer could outlive `stop()`** and open an EventSource nobody closes (the
   gap-fill retry and the reconnect shared one timer field, so one handle overwrote the other).
   Now two timers, both cleared by `stop()`, and scheduled reconnects check the generation.
   Regression test: "opens nothing after stop(), even with a reconnect and a gap fill pending".
2. **Medium: outbox ages hid a Redis outage** (above, §2.1). Test: a row failing for an hour
   reports about 3,600 s and counts as pending.
3. Low: `stop()` during a gap fill left `filling` stuck, so later gaps stayed unfilled after a
   React StrictMode remount. Reset in `stop()`; regression test (checked to fail without the fix).
4. Low: a probe answering "ok" reconnected without delay (a loop if EventSource kept failing while
   `fetch` worked). Now it goes through the backoff, and "ok" requires `text/event-stream`.
5. Low: manifest rows used the path as React key; paths cut to the length limit can repeat. Index
   keys now.
6. Low: after a non-retryable error (4xx) the same idempotency key was kept for the next submit.
   Now only an unanswered request (network error, 5xx) keeps the key.

Not changed: "no test fails on a CSP violation". Every e2e test fails on any browser console
error (`e2e/helpers.ts`), and Chromium reports CSP violations as console errors.

## 14. Things to know

- **Demo settings for the e2e tests and the walkthrough** are shell variables only (never `.env`):
  `ENABLE_FAULT_INJECTION=true`, `STUB_DELAY_SECONDS=1`, `LEASE_SECONDS=20`,
  `PROVIDER_TIMEOUT_SECONDS=10`, `RECOVERY_INTERVAL_SECONDS=2`, `RETRY_BASE_SECONDS=2`. The e2e
  global setup refuses to run without fault injection on.
- **The e2e and screenshot runs create jobs in the development database** (they use the real
  stack). The screenshot run also kills and restarts the worker container once (for the recovery
  view) and cancels the long `large.pdf` job it starts.
- **Edge-state screenshots use route mocking** (empty database, API unreachable, stream limit,
  upload in progress, replay); everything else is real data.
- **The dev server has no CSP** (hot reload needs inline scripts); the build does.
- **A stage's percent and the job's percent differ on purpose**: a timeline cell shows the stage's
  own progress (5 % of Summarize), the header shows the whole job (43 %).
