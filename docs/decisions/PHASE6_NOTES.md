# Phase 6 notes: design decisions

Every decision made while building the `repo_index` (repository ZIP) workflow, in plain
language. Each entry has: the **decision**, the **alternatives** considered, and the **reason**.
The short version lives in `CLAUDE.md` ("Decisions made (Phase 6)"); the threat model is in
`docs/security.md`.

## How the ZIP is read

### 1. Extracted files go into one generated file, never under their own names
- **Decision:** `extract_files` copies the bytes of every extracted file, one after another, into
  `work/{job_id}/files.bin`, and writes each file's offset, size and SHA-256 to `extract.json`.
  No file or folder is ever created with a name that came from the archive.
- **Alternatives:** extract into a folder tree (`work/{job_id}/files/src/app.py`), which is
  what `unzip` does; or one file per entry with a generated name (`files/00017`).
- **Reason:** with a folder tree, every name has to be perfectly validated, because one mistake
  is a "zip slip" (writing to `../../somewhere`). With one blob, a bad name *cannot* reach the
  filesystem, even if a check were missed. It also avoids creating thousands of small files, and
  it fits the checkpoint system, which needs to know a stage's output files in advance (two fixed
  files instead of a variable number). Think of it like storing uploads in a database column
  keyed by id instead of trusting the user's file name.

### 2. Two passes in the sandbox: first the table of contents, then the bytes
- **Decision:** `validate_archive` reads only the ZIP's table of contents (the "central
  directory") and decides whether the archive is acceptable. `extract_files` then decompresses.
  Both run in the separate, limited child process (`zip_child.py`).
- **Alternatives:** one pass that checks and extracts together; or reading the table of
  contents in the worker itself (it's "just metadata").
- **Reason:** the whole archive must be rejected for one bad name, so names must all be checked
  *before* anything is decompressed. Two passes also map onto two checkpointed stages. The table
  of contents is still attacker-controlled (a crafted one can be enormous), so it's read in the
  sandbox too.

### 3. Path rules run before extraction
- **Decision:** files that will be skipped because of their path (inside `node_modules/`,
  hidden, nested archives, lockfiles, unknown extensions, too long, too deep) and files whose
  declared size is over the per-file limit are never decompressed. The total-size limit
  (500 MiB) therefore counts only the files that are extracted.
- **Alternatives:** extract everything, then filter (the order the stage names suggest).
- **Reason:** extracting `node_modules/` would waste time and disk, and a bomb hidden inside a
  skipped folder would still go off. The filter logic is one pure function
  (`path_skip_reason`), used by both `extract_files` (to choose what to extract) and
  `filter_files` (to record the reason), so the two can't disagree.

### 4. The ZIP child uses only the standard library and generic arguments
- **Decision:** `zip_child.py` imports only Python's standard library (a test enforces it), gets
  its limits as numbers and a small JSON request file, and runs with `python -I` and an
  environment with only `PATH` and `LANG`.
- **Alternatives:** import the app's settings or helpers in the child.
- **Reason:** the child processes hostile input; the less it can reach, the better. It can't
  read the database password because it never receives it, and it can't import app code.

### 5. One shared sandbox for PDFs and ZIPs
- **Decision:** the Phase 5 PDF sandbox runner became `app/workflows/sandbox.py`
  (`run_sandboxed`) and the launcher moved to `app/workflows/limited_launcher.py`. Each caller
  says which error classes its child may report (`allowed_errors`) and which class means "killed
  for using too much" (`pdf_too_complex` / `zip_too_complex`). `pdf/sandbox.py` is now a thin
  wrapper with the same behaviour.
- **Alternatives:** copy the PDF sandbox for ZIPs.
- **Reason:** the carry-forward list asked to reuse the sandbox pattern. A copy would mean two
  places to fix every future bug in security-critical code. Restricting each child to its own
  error classes is a small extra safety step (a PDF child can't claim `zip_bomb`).

### 6. The worker re-checks what the child wrote
- **Decision:** `archive.py` validates the child's output files: field types, indexes in order,
  exactly the requested entries, offsets that tile `files.bin` exactly, sizes within limits. A
  mismatch is a bug (`internal_error`).
- **Alternatives:** trust the child's files like the PDF parser's page text.
- **Reason:** cheap, and it stops a confused or compromised child from making the worker read
  outside the blob.

## Which archives are rejected

### 7. Any unsafe name rejects the whole archive
- **Decision:** `zip_unsafe_path` for: a NUL byte, any control character, a backslash, a
  leading `/`, a drive letter (`C:`), an empty, `.` or `..` path segment, a name longer than
  4,096 characters, a name that resolves (with `realpath`) outside the job's real work folder,
  duplicate names, names equal after Unicode normalization (NFC) and case-folding, and a path
  used both as a file and as a folder.
- **Alternatives:** skip only the bad entries; or allow `./src/x` and `src//x` by normalizing.
- **Reason:** the brief says reject the whole archive. An archive with even one crafted name was
  made by an attacker or a broken tool; normalizing would mean guessing what the creator meant.
  The `realpath` check resolves the final path for real, so it holds even if a string pattern
  were missed (tested with a planted symlink).

### 8. Both the raw and the decoded name are checked
- **Decision:** the check runs on `orig_filename` (which keeps a NUL byte that Python's zipfile
  cuts off) and on `filename` (which may come from a "Unicode path" extra field).
- **Alternatives:** check only `filename`.
- **Reason:** `evil.txt\0.md` looks like a Markdown file to some tools and opens as `evil.txt`
  in others; and two different names in one entry is a classic way to fool one tool and not
  another.

### 9. Very long or very deep paths are skipped, not rejected
- **Decision:** paths over 240 characters or 20 components are skipped (`path_too_long`,
  `path_too_deep`). Their names are cut to 240 characters in the outputs, and the expensive
  checks (`realpath`, folder prefixes) are skipped for them.
- **Alternatives:** reject the archive.
- **Reason:** deep paths are common in real repositories (deep `node_modules` trees, Java
  packages), so rejecting would fail legitimate uploads. Since names are never used as paths,
  skipping is safe. Cutting names keeps `archive.json` small; skipping the expensive checks stops
  a name like `a/a/a/...` (thousands of folders) from exhausting the child (found in review).

### 10. Encryption anywhere rejects the archive
- **Decision:** the encryption flag, or the AES method, on any entry gives `zip_encrypted`,
  even inside a skipped folder.
- **Alternatives:** reject only if an encrypted entry would be indexed.
- **Reason:** the brief says encrypted entries reject the archive, and the user gets a clear
  message instead of a silently incomplete index.

### 11. Every read error is `zip_corrupt`, except disk trouble
- **Decision:** truncated archives, bad checksums, mismatched headers, overlapping entries,
  unsupported compression and undecodable names all become `zip_corrupt`. `ENOSPC`, `EIO`,
  `EROFS`, `EDQUOT`, `EAGAIN` become the transient `storage_unavailable` (retried).
- **Alternatives:** separate classes for each kind of damage.
- **Reason:** the user can do the same thing about all of them (make a new ZIP). Disk errors are
  the server's problem, not the archive's, and Phase 5 already treats them as transient (found
  in review).

## Zip bombs

### 12. Sizes in the ZIP headers are never trusted for safety
- **Decision:** declared sizes are used only to fail fast (declared total over the limit,
  declared ratio over 100:1) or to skip early (declared size over the per-file limit). The real
  limits are enforced on the bytes actually produced, counted every 64 KiB while decompressing
  (`Budget` in `zip_child.py`). Reading a file stops one block after the per-file limit, and the
  rest is never decompressed.
- **Alternatives:** check declared sizes only (what naive code does); or decompress fully and
  check afterwards.
- **Reason:** headers can lie. Failing fast on declared values is safe because an honest archive
  never trips them; the counted bytes are what an attacker can't fake.

### 13. Compression ratios are checked only past 1 MiB
- **Decision:** an entry (or the whole archive) is a bomb if it produced more than 100 bytes per
  compressed byte, but only once it has produced more than `ZIP_RATIO_CHECK_MIN_BYTES` (1 MiB).
- **Alternatives:** check the ratio from the first byte.
- **Reason:** small, very repetitive files (a file of blank lines, a generated table) compress
  far better than 100:1 and are harmless. Below 1 MiB, the expansion can't hurt anything.

### 14. A header that understates a size ends as `zip_corrupt`
- **Decision:** no special code: Python's zipfile stops reading at the stated size, the checksum
  then fails, and that's reported as `zip_corrupt`.
- **Alternatives:** a dedicated `zip_lying_header` class.
- **Reason:** the effect for the user is the same (the archive is broken), and the counting in
  decision 12 still guards the case the library didn't. Tests cover both directions of lying.

### 15. Settings in bytes, with startup checks
- **Decision:** `ZIP_MAX_TOTAL_BYTES`, `ZIP_MAX_FILE_BYTES`, `ZIP_RATIO_CHECK_MIN_BYTES` are
  bytes. At startup the settings refuse: a per-file limit over the total, a sandbox memory below
  `128 MiB + 3 × the per-file limit` (the child holds one file in memory), a total over 64 GiB,
  more than 100,000 entries, an excluded-folder list containing a path, a Celery time limit not
  longer than the larger workflow's stage limits, and `STUB_DELAY_SECONDS` not below the provider
  timeout. The output allowance for the table of contents grows with `ZIP_MAX_ENTRIES`.
- **Alternatives:** megabyte settings like the PDF ones.
- **Reason:** tests need limits of a few KiB. The startup checks follow the Phase 5 rule: a
  misconfiguration should stop the services with a clear message, not fail jobs later with a
  misleading `zip_too_complex`.

## Which files are indexed

### 16. An allowlist with 14 skip reasons
- **Decision:** a file is indexed only if its extension (case-insensitive) is a known text/code
  type, or its exact name is `README`, `LICENSE`, `Makefile` or `Dockerfile`. Every other file
  gets one reason: `symlink`, `special_file`, `path_too_long`, `path_too_deep`, `excluded_dir`,
  `system_file`, `hidden`, `nested_archive`, `lockfile`, `minified`, `unsupported_type`,
  `too_large`, `binary`, `empty`. Path rules apply in that fixed order.
- **Alternatives:** a blocklist of known binary types.
- **Reason:** with a blocklist, anything new or unusual gets indexed by default. With an
  allowlist, the worst case is a skipped file, which the manifest explains.

### 17. Hidden files are skipped
- **Decision:** any file or folder whose name starts with `.` is skipped (`hidden`), unless an
  earlier rule already applies (`.git` is `excluded_dir`, `.DS_Store` is `system_file`).
- **Alternatives:** index dotfiles like `.github/workflows/ci.yml`.
- **Reason:** `.env` and similar files often hold secrets. Skipping them all is the safe default.

### 18. The default excluded folders
- **Decision:** `.git, .hg, .svn, node_modules, bower_components, dist, build, target,
  __pycache__, .venv, venv, .tox, .nox, .mypy_cache, .pytest_cache, .ruff_cache, .idea, .vscode,
  coverage, htmlcov, .next, .nuxt, .gradle, .terraform, .cache, site-packages, vendor`, matched
  case-insensitively at any depth. `REPO_EXCLUDED_DIRS` replaces the list; empty keeps it.
- **Alternatives:** also `env` and `out`.
- **Reason:** `env` and `out` are common names for real source folders, so excluding them would
  hide code. `vendor` holds copied dependencies in Go and PHP projects.

### 19. Content rules: binary, empty, minified
- **Decision:** `binary` if there's a NUL byte in the first 8 KiB or more than 10 % of the
  decoded characters are replacement characters (invalid UTF-8). A few invalid bytes are kept as
  replacement characters (e.g. a Latin-1 comment). `empty` if there's no visible character.
  `minified` for `*.min.*`, `*-min.*`, `*.bundle.*` names, or JS/TS/CSS/JSON/HTML files of at
  least 1,000 characters averaging more than 250 characters per line.
- **Alternatives:** apply the line-length rule to every file type.
- **Reason:** Markdown and text files often have one long line per paragraph and would be
  wrongly skipped. UTF-16 files contain NUL bytes and are treated as binary; that's rare in
  source repositories.

## Chunks and summaries

### 20. Chunks are whole lines, packed with the PDF chunker's code
- **Decision:** each file is split into lines, and lines are packed with the PDF chunker's
  packing function (`group_pieces`) and the same size settings (target 4,000, max 6,000, overlap
  400 characters). A chunk never spans two files. Chunk ids use the PDF scheme with this
  chunker's own version (`repo-lines-1`) and a repository-wide chunk number. A line longer than
  6,000 characters is cut into pieces that keep its line number.
- **Alternatives:** split code by functions/classes (needs a parser per language).
- **Reason:** lines are universal and give exact line ranges in `chunks.jsonl`. Reusing the
  packing code means one implementation to trust. (The refactor was checked: PDF chunks are
  byte-identical to Phase 5's.)

### 21. A summary per file, then an overview
- **Decision:** every chunk is summarized; a file with several chunks gets its chunk summaries
  combined; the file summaries, each written as `path: summary` and cut to 2,000 characters, are
  combined into the overview. All of it uses the Phase 5 map-reduce code.
- **Alternatives:** summarize the whole repository as one long document.
- **Reason:** per-file summaries are what a reader of a repository wants. Cutting each overview
  line keeps the guarantee that every combine round shrinks the work, so it always ends.

### 22. The call limit is checked before the first call
- **Decision:** `planned_calls` computes an upper bound on the summarizer calls (chunks, plus
  combine rounds per file, plus overview rounds); over `REPO_MAX_PROVIDER_CALLS` (1,000) the job
  fails with `repo_too_large` before any call. `filter_files` also stops early once the text
  can't possibly fit (more than `calls × chunk_max_chars` characters).
- **Alternatives:** count calls as they happen and stop at the limit.
- **Reason:** stopping halfway would waste the calls already made (real money with a real
  provider) and leave no result. An upper bound is easy to prove correct (a test checks it).

### 23. One shared "summary caller"
- **Decision:** the code around each provider call (fault injection, the per-chunk cache,
  recording calls, capping output, the stage deadline, progress) moved from the PDF summarize
  stage into `SummaryCaller` in `app/workflows/common.py`, used by both workflows. Small helpers
  (`Throttle`, `verified_input_path`, `register_artifact`) moved there too.
- **Alternatives:** copy it into the repo stage.
- **Reason:** same behaviour in both workflows, including retries reusing cached chunk
  summaries; one place to change for Phase 11.

## Outputs

### 24. Three artifacts, written deterministically
- **Decision:** `summary.md`, `chunks.jsonl` (`application/x-ndjson`) and `manifest.json`
  (`tasklane.repo_manifest/1`). The manifest lists every included file (language, bytes, lines,
  SHA-256) and every skipped file with its reason, counts per reason and per language, totals,
  chunking settings, provider and model, and every limit that applied. No timestamps.
- **Alternatives:** only counts in the manifest.
- **Reason:** "why wasn't my file indexed?" should be answerable from the manifest. The
  archive is capped at 5,000 entries, so the lists stay small. Determinism makes reruns
  byte-identical (tested for every stage).

### 25. Untrusted names and text are made inert
- **Decision:** in `summary.md`, paths have invisible/format characters written as visible
  `\uXXXX` codes and Markdown punctuation escaped (now including `&`, so `&#x202e;` stays
  literal); every summary sits in a fenced block whose fence is longer than any run of `~`
  inside it. `manifest.json` and `chunks.jsonl` are ASCII-only JSON. Error messages never
  include names from the archive.
- **Alternatives:** escape summaries with backslashes like file names.
- **Reason:** a right-to-left override can make `gnp.py` display as `yp.png`; Markdown links and
  HTML in a name or summary could become clickable in a viewer. A fenced block shows code-derived
  text exactly as it is, and can't be closed early.

### 26. Repository text is data, never instructions
- **Decision:** repository text and paths reach the summarizer only as the `text` argument;
  nothing from them goes into the call's options. The stub doesn't interpret text; summaries are
  only stored as text.
- **Alternatives:** wrap chunks in delimiters in the stage.
- **Reason:** delimiters chosen by the stage can be imitated by the content, so proper
  separation is the job of a real provider's prompt format (Phase 11 carry-forward). Tests run
  "ignore previous instructions" content through the pipeline and check it stays plain text.

### 27. A failed filter deletes `files.bin`
- **Decision:** when `filter_files` fails permanently (`repo_no_sources`, `repo_too_large`), it
  deletes `files.bin`.
- **Alternatives:** cap extraction at the text the call limit allows; delete work folders on
  every failure.
- **Reason:** found in review: a ZIP could make each job keep up to 500 MiB it could never use.
  A cap tied to the call limit would wrongly fail repositories with big minified files that the
  filter skips anyway, and general cleanup is Phase 12's retention work. Succeeded jobs still keep
  `files.bin` (carry-forward).

## Errors, retries, tests

### 28. Nine permanent input errors
- **Decision:** `zip_corrupt`, `zip_encrypted`, `zip_unsafe_path`, `zip_too_many_files`,
  `zip_too_large`, `zip_bomb`, `zip_too_complex`, `repo_no_sources`, `repo_too_large`, with
  number-only message templates, all in `INPUT_ERROR_CLASSES` (no manual retry).
- **Alternatives:** make `zip_too_complex` transient (a timeout might be luck).
- **Reason:** the same archive would fail the same way; retrying just wastes worker time. No
  migration was needed: error classes are stored as plain text, and `chunks` was already an
  artifact type.

### 29. Checkpoints and fingerprints
- **Decision:** the first five stages are checkpointed. `extract_files` and `filter_files`
  include every setting the path rules use (excluded folders, path length and depth, file limit)
  in their fingerprints. `REPO_MAX_PROVIDER_CALLS` is in none.
- **Alternatives:** fingerprint every setting.
- **Reason:** a fingerprint must change when a stage's *output* would change. The call limit can
  only make a stage fail, never change a successful output, and it's checked again each time
  `summarize` starts.

### 30. `STUB_DELAY_SECONDS` replaces the dummy stage
- **Decision:** `repo_index` no longer runs the placeholder stage; the class lives on as
  `tests/dummy_stage.py` for tests that need a do-nothing stage. The end-to-end timing tests
  (duplicate delivery, killed worker) instead slow the stub summarizer down with
  `STUB_DELAY_SECONDS` (default 0).
- **Alternatives:** keep `DUMMY_STAGE_SECONDS` as an extra stage in `repo_index`.
- **Reason:** a fake stage in a real workflow would show up in events and the dashboard. A
  simulated provider delay is realistic (real providers are slow) and also lets you watch a job
  in the walkthrough.

### 31. `repo_index` accepts fault injection
- **Decision:** the `fault_injection` option works for both workflows.
- **Reason:** needed to show retries and resume for repositories (scenario f of the walkthrough),
  with the same guards as Phase 5.

### 32. Malicious archives are built in code, and every rejection snapshots the disk
- **Decision:** `devtools/zip_samples.py` builds every archive, patching header bytes where
  zipfile refuses (encrypted flags, NUL names, lying sizes). Tests use small limits (50 entries,
  512 KiB total, 128 KiB per file, 16 KiB ratio floor). Each rejection test lists every file and
  folder under the storage folder's parent before and after the job and requires them to be
  identical outside `work/{job_id}/`.
- **Alternatives:** commit sample ZIPs; check only that `evil.txt` doesn't exist.
- **Reason:** no binary files in the repository (Phase 5 rule). A full snapshot catches *any*
  stray write, not just the one we thought of.

### 33. Left for later
- **Decision:** no end-of-central-directory pre-check: a ZIP with millions of entries is stopped
  by the sandbox's memory/time limits (`zip_too_complex`) rather than `zip_too_many_files`.
- **Reason:** safe as it is; a clearer error is Phase 12 polish (carry-forward).
