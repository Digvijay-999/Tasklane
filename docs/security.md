# Security: untrusted uploads

Tasklane runs on your own machine, but the files it processes (PDFs and repository ZIPs) are
treated as hostile: anyone who can upload can try to crash the worker, fill the disk, read files
they shouldn't, write outside the storage folder, or get code run. This page lists each attack,
the exact defense, **where it is enforced** (file), and **where it is tested** (test name; tests
live under `backend/tests/`).

Scope: the local, single-user deployment described in the README. There is no authentication
yet (all ports are bound to `127.0.0.1`), no antivirus scanning, and no secret scanning of
repository content (see "Not covered" at the end).

## Principles

1. **Parse untrusted files in a separate, limited process**, never in the worker itself. The
   worker kills it at a deadline; the operating system caps its memory, CPU time and output file
   size. Enforced by `app/workflows/sandbox.py` + `app/workflows/limited_launcher.py` for both
   workflows.
2. **The child gets nothing worth stealing**: `python -I` (application code isn't importable),
   an environment with only `PATH` and `LANG` (no database password, no API keys), no core dumps.
3. **Trust nothing the child says** beyond an allowed error class and integer parameters; check
   its output files again in the worker.
4. **Names from inside a file are data, never paths.** Every file Tasklane writes has a name it
   generated itself.
5. **Fixed, safe error messages**: templates with numbers only, never exception text, file
   content, or names from inside an archive. Details go to the worker log.
6. **Allowlists, not blocklists**, for what gets indexed.
7. **Nothing from an upload is ever executed, imported, built, or interpreted as an instruction.**

## Uploads (both kinds)

| Attack | Defense | Enforced in | Tested by |
|---|---|---|---|
| Huge upload fills the disk | Size counted on the bytes actually received (50 MiB PDF, 100 MiB ZIP); `Content-Length` only fails fast | `services/uploads.py` | `test_oversized_body_without_content_length_is_rejected_while_streaming`, `test_lying_content_length_does_not_bypass_the_limit` |
| Wrong file type disguised by name or `Content-Type` | Extension, declared type and file signature must all agree; checked while streaming | `domain/uploads.py`, `services/uploads.py` | `test_pdf_named_file_containing_zip_bytes_is_rejected`, `test_signature_mismatch_stops_reading_immediately` |
| Filename used as a path (`../../x`) | The `?filename=` value is display-only; files are stored at `uploads/{id}/source.{pdf,zip}` | `storage/local.py` | `test_filename_is_display_only_never_a_path` |
| Filename with control characters | Rejected | `domain/uploads.py` | `test_filename_with_control_characters_is_rejected` |
| Interrupted upload leaves junk | Partial file deleted, row marked `rejected` | `services/uploads.py` | `test_client_disconnect_mid_upload_rejects_and_cleans_up` |
| File swapped after upload | Stages check the SHA-256 recorded at upload time before parsing | `workflows/common.py` (`verified_input_path`) | `test_missing_or_changed_upload_file_fails_clearly` |

## The parser sandbox (PDF and ZIP)

| Attack | Defense | Enforced in | Tested by |
|---|---|---|---|
| Parser loops forever | Wall-clock deadline, then `SIGKILL` of the child's whole process group | `workflows/sandbox.py` | `test_a_hanging_parser_is_killed_at_the_deadline`, `test_processes_started_by_the_parser_are_killed_too` |
| Parser eats all memory | `RLIMIT_AS` (PDF 1 GiB, ZIP 512 MiB) | `workflows/limited_launcher.py` | `test_a_parser_that_uses_too_much_memory_is_stopped`, `test_limits_are_applied_inside_the_child` |
| Parser writes gigabytes | `RLIMIT_FSIZE` (PDF 64 MiB; ZIP 64 MiB for the table of contents, `ZIP_MAX_TOTAL_BYTES` + 16 MiB for extracted files) | `workflows/limited_launcher.py` | `test_a_parser_that_writes_too_much_output_is_stopped` |
| Parser steals secrets or app code | `python -I`, environment with only `PATH`/`LANG` | `workflows/sandbox.py` | `test_the_parser_gets_no_secrets_and_cannot_import_the_app` |
| Child script grows dangerous imports | The PDF child may import only the standard library and pypdf; the ZIP child and the launcher only the standard library (no app code, settings, or database) | `workflows/pdf/parse_child.py`, `workflows/repo/zip_child.py` | `test_sandboxed_scripts_import_only_the_standard_library` |
| Disk trouble while the child works | `ENOSPC`, `EIO`, `EROFS`, `EDQUOT` inside the ZIP child are reported as the transient `storage_unavailable` (retried), not blamed on the archive | `workflows/repo/zip_child.py` | (not reproducible in tests without a failing disk) |
| Compromised child lies to the worker | Only allowed error classes and integer parameters are trusted; anything else is `internal_error`. The ZIP child's output files are re-checked (exactly the requested entries, offsets that tile the blob, sizes within limits) | `workflows/sandbox.py`, `workflows/repo/archive.py` | `test_unknown_error_classes_from_the_child_are_not_trusted` |
| Worker loses the job while the child runs | The worker's heartbeat renews the lease; the sandbox checks for fencing about once a second and kills the child | `workflows/sandbox.py` | `test_the_lease_is_renewed_while_waiting`, `test_losing_the_job_kills_the_parser`, `test_a_fenced_heartbeat_stops_the_stage_and_kills_its_parser` |
| Worker process killed (kill -9, out of memory) while the child runs | `prctl(PR_SET_PDEATHSIG, SIGKILL)` in the child, plus an exit if its parent is already gone (Phase 7) | `workflows/limited_launcher.py` | `test_the_parser_dies_when_the_worker_process_is_killed`, `test_a_parser_whose_worker_died_before_it_started_exits_at_once` |

## PDFs

| Attack | Defense | Enforced in | Tested by |
|---|---|---|---|
| Encrypted / damaged PDF | `pdf_encrypted` / `pdf_corrupt`, permanent | `workflows/pdf/parse_child.py` | `test_password_protected_pdf_is_rejected`, `test_corrupt_pdf_is_rejected` |
| Thousands of pages, megabytes of text | 500 pages, 2,000,000 characters; fails, never truncates | `workflows/pdf/parse_child.py` | `test_page_limit`, `test_text_limit_fails_instead_of_truncating` |
| Error details leak internals | Fixed messages; tracebacks only in logs | `domain/errors.py` | `test_failure_details_stay_out_of_the_database` |
| PDF text renders as active Markdown/HTML | The display filename is escaped; the dashboard must render `summary.md` without raw HTML (Phase 9 carry-forward) | `workflows/pdf/report.py` | `test_markdown_escapes_the_display_filename` |

## Repository ZIPs

How the ZIP is read: `validate_archive` reads only the archive's table of contents (the "central
directory") in the sandbox and decides whether the whole archive is acceptable.
`extract_files` then decompresses, in the sandbox, only the entries that could be indexed, and
copies their bytes into one file with a generated name (`work/{job_id}/files.bin`) at offsets the
code chooses. **No file or folder is ever created under a name from the archive**, so even a name
that slipped past every check below could not reach the filesystem.

### Whole-archive rejections (permanent failures)

| Attack | Defense | Error | Enforced in | Tested by |
|---|---|---|---|---|
| Zip slip: `../../evil.txt`, `src/../../x` | Any `..` segment rejects the archive | `zip_unsafe_path` | `zip_child.py` (`unsafe_name`) | `test_unsafe_names_reject_the_whole_archive[zip slip]`, `[dot-dot in the middle]`; `test_malicious_archives_fail_permanently_and_write_nothing_outside_the_work_folder[zip slip]` |
| Absolute path `/etc/x` | Rejected | `zip_unsafe_path` | `zip_child.py` | `...[absolute path]` in both tests above |
| Windows drive letter `C:/x`, `C:x` | Rejected | `zip_unsafe_path` | `zip_child.py` | `...[drive letter]`, `[drive letter, relative]` |
| Backslash tricks `..\..\x` (a separator on Windows, a plain character on Linux) | Any backslash rejects the archive | `zip_unsafe_path` | `zip_child.py` | `...[backslash traversal]`, `[backslash inside]` |
| NUL byte in a name (`evil.txt\0.md` shows one name, opens another) | Checked on the raw name, before Python's zipfile cuts it at the NUL | `zip_unsafe_path` | `zip_child.py` | `test_unsafe_names_reject_the_whole_archive[NUL byte]` |
| Control characters, empty or `.` segments | Rejected | `zip_unsafe_path` | `zip_child.py` | `...[control character]`, `[empty segment]`, `[dot segment]` |
| A name longer than any real path (over 4,096 characters) | Rejected | `zip_unsafe_path` | `zip_child.py` | `test_a_name_longer_than_any_real_path_is_unsafe` |
| Names that differ only in Unicode form (`café` composed vs decomposed) | Same collision check | `zip_unsafe_path` | `zip_child.py` | `test_names_that_differ_only_in_unicode_form_collide` |
| A deep, long name built to make the checks slow (thousands of folders) | Names over `ZIP_MAX_PATH_LENGTH` skip the expensive checks (folder prefixes, `realpath`) and are skipped as `path_too_long` | skip | `zip_child.py` | `test_a_very_deep_long_name_is_cheap_to_inspect_and_flagged_too_long` |
| A name that resolves outside the folder by other means | The name is joined to the job's real work folder, resolved with `realpath` (following any links), and must stay inside it | `zip_unsafe_path` | `zip_child.py` | `test_names_are_resolved_against_the_real_folder` |
| Duplicate names (two entries, one path: which one wins?) | Rejected | `zip_unsafe_path` | `zip_child.py` | `...[duplicate names]` in both tests |
| Names that collide when case-folded (`README.md` / `readme.MD`), or differ only in Unicode form | NFC + casefold comparison; rejected | `zip_unsafe_path` | `zip_child.py` | `...[case collision]` in both tests |
| A path that is both a file and a folder (`README.md` and `README.md/x`) | Rejected | `zip_unsafe_path` | `zip_child.py` | `...[file and folder with one name]` |
| A misleading Unicode-path extra field | Both the raw name and the decoded name must be safe | `zip_unsafe_path` | `zip_child.py` | (covered by the same checks; no dedicated archive) |
| Encrypted entry (anywhere, even in a skipped folder) | Encryption flag or AES method rejects the archive | `zip_encrypted` | `zip_child.py` | `test_encrypted_or_corrupt_archives_are_rejected[encrypted]`; integration `[encrypted entry]` |
| Truncated or damaged archive, bad checksum, mismatched local header, overlapping entries | Any read error | `zip_corrupt` | `zip_child.py` | `...[truncated]`, `[garbage after signature]`; integration `[truncated]` |
| Too many entries | More than `ZIP_MAX_ENTRIES` (5,000) | `zip_too_many_files` | `zip_child.py` | `test_too_many_entries`; integration `[too many entries]` |
| A table of contents too big to read, or a reader that runs too long | The sandbox's memory and time limits | `zip_too_complex` | `sandbox.py` | `test_a_reader_that_runs_past_the_deadline_is_too_complex`, the sandbox tests above |

### Zip bombs and lying headers

The sizes written in a ZIP's headers can lie. They are used only to **fail fast** or to **skip
early** (both safe directions); every limit is enforced on the **real bytes** counted while
decompressing (`Budget` and `read_entry` in `zip_child.py`).

| Attack | Defense | Result | Tested by |
|---|---|---|---|
| High-ratio bomb (a tiny entry that expands enormously) | Ratio = real bytes out / compressed bytes in, per entry and for the whole archive, over 100:1 (`ZIP_MAX_COMPRESSION_RATIO`) once past 1 MiB (`ZIP_RATIO_CHECK_MIN_BYTES`). Declared ratios are checked first, before decompressing anything | `zip_bomb` | `test_a_high_ratio_bomb_is_rejected`; integration `[high-ratio bomb]`; `test_the_counter_enforces_total_and_ratio_on_real_bytes[ratio]` |
| Many moderate files adding up | Total real bytes over `ZIP_MAX_TOTAL_BYTES` (500 MiB) | `zip_too_large` | `test_contents_over_the_total_limit_are_rejected`; `...[total]` |
| Many small entries, each under the ratio floor | The overall ratio counts the whole archive | `zip_bomb` | `test_many_small_entries_can_add_up_to_a_bomb` |
| A header that understates the size | Python's reader stops at the stated size, and the checksum then fails | `zip_corrupt` | `test_a_header_that_understates_the_size_is_caught`; integration `[lying header]` |
| A header that overstates the size | Fails fast on the declared ratio, or the entry is skipped as `too_large` without being decompressed | `zip_bomb` / skip | `test_a_header_that_overstates_the_size_fails_fast` |
| A stream that produces more than any header says | Reading stops one block after `ZIP_MAX_FILE_BYTES`; the file is skipped as `too_large` and the rest is never decompressed | skip | `test_the_counter_stops_a_file_at_the_limit_whatever_the_header_says`, `test_an_entry_bigger_than_the_file_limit_is_skipped_not_copied` |
| A bomb hidden in `node_modules/`, a nested archive, or any skipped file | Skipped paths are never decompressed at all | skip | `test_nothing_skipped_was_ever_decompressed_or_written` |
| Small, very repetitive real files (e.g. blank lines) | Not a bomb: ratios are only checked past 1 MiB | indexed | `test_small_repetitive_files_are_not_bombs` |

### Entries that are skipped, not rejected

| Attack | Defense | Skip reason | Tested by |
|---|---|---|---|
| Symlink to a secret (`settings.py -> ../../../secret.txt`) | Detected from the entry's Unix mode bits; never created, never followed, its target never read | `symlink` | `test_symlinks_and_special_files_are_detected_from_their_mode_bits`; `test_a_symlink_to_a_secret_is_skipped_and_never_followed` (a real secret file next to the storage folder never appears in any output) |
| Device, pipe or socket entries | Mode bits | `special_file` | same unit test |
| Nested archives (`.zip`, `.tar`, `.gz`, `.7z`, `.rar`, `.jar`, ...) | Never extracted | `nested_archive` | `test_a_nested_archive_is_skipped_never_extracted` |
| Very long or very deep paths | More than 240 characters / 20 components; the name is cut in the outputs | `path_too_long`, `path_too_deep` | `test_a_very_long_name_is_cut_and_flagged`, `test_path_rules` |
| Secrets in dotfiles (`.env`), VCS internals (`.git/`), dependencies (`node_modules/`) | Hidden and excluded folders are skipped | `hidden`, `excluded_dir` | `test_path_rules`, `test_the_realistic_repository_gives_the_expected_manifest` |
| Binary or non-text files | Allowlist of extensions; NUL byte in the first 8 KiB or >10 % invalid UTF-8 | `unsupported_type`, `binary` | `test_content_rules` |

Every skipped file is listed in `manifest.json` with its reason
(`test_the_realistic_repository_gives_the_expected_manifest` checks all 14 reasons).

### Untrusted names and text in the outputs

| Attack | Defense | Enforced in | Tested by |
|---|---|---|---|
| A file name with Markdown/HTML (`[click](javascript:x)`) | Markdown punctuation escaped in `summary.md`, including `&`, so an entity like `&#x202e;` stays literal text | `workflows/repo/report.py`, `workflows/pdf/report.py` | `test_files_with_markdown_or_invisible_characters_in_their_names_are_escaped`, `test_html_entities_in_names_stay_literal_text` |
| A right-to-left override that disguises a name (`gnp.py` shown as `yp.png`), zero-width characters | Shown as visible `\u202e` codes in `summary.md`; `manifest.json` and `chunks.jsonl` are ASCII-only JSON | `workflows/repo/report.py`, `storage/workspace.py` | same test, `test_chunks_artifact` |
| Repository text (or an LLM's summary of it) that tries to inject Markdown, HTML or links | Summaries are in fenced blocks with a fence longer than anything inside, invisible characters made visible | `workflows/repo/report.py` | `test_summaries_cannot_break_out_of_their_block` |
| A name from inside the archive in the job's error message | Messages are fixed templates with numbers only | `domain/errors.py` | `test_malicious_archives_fail_permanently_and_write_nothing_outside_the_work_folder` (`"evil"` never in `error_summary`), `test_unsafe_names_reject_the_whole_archive` |
| Writing outside the job's folder | Generated names only; for every rejection, the whole storage folder and the folder around it are snapshotted before and after and must be identical outside `work/{job_id}/` | `workflows/repo/stages.py` | `test_malicious_archives_fail_permanently_and_write_nothing_outside_the_work_folder` |
| Prompt injection ("ignore previous instructions...") in repository text | Content is passed to the summarizer only as the data argument; nothing from it reaches the call's options, the stub doesn't interpret it, its output is only stored as text. A real LLM provider (Phase 11) must delimit it as data and offer no tools | `workflows/common.py`, `providers/base.py` | `test_stub_treats_instructions_in_the_text_as_plain_data`, `test_summaries_cannot_break_out_of_their_block` |
| Huge repositories run up summarization cost | At most `REPO_MAX_PROVIDER_CALLS` (1,000) calls, checked before the first one | `workflows/repo/stages.py` | `test_too_many_summarization_calls_fail_with_repo_too_large` |
| Extracted data piling up on disk after failures | When `filter_files` fails permanently (`repo_no_sources`, `repo_too_large`), it deletes `files.bin` | `workflows/repo/stages.py` | `test_a_permanent_filter_failure_does_not_keep_the_extracted_files` |

## The dashboard (Phase 9)

Everything the dashboard shows from an upload (file names, paths from inside a ZIP, languages,
summaries, event payloads) is untrusted text.

| Risk | Defense | Enforced in | Tested by |
|---|---|---|---|
| Markup or script in a name (`<img src=x onerror=...>.pdf`) | React renders strings as text; `dangerouslySetInnerHTML` is never used (a test scans the source for it) | everywhere; `frontend/src/components/SafeText.tsx` | `SafeText.test.tsx` ("renders hostile file names as inert text", "never uses dangerouslySetInnerHTML") |
| A name that becomes a link (`javascript:alert(1)`, `https://evil.example/x.pdf`) | No linkifying anywhere; the only links are the app's own routes and artifact downloads built from ids | `JobsTable.tsx`, `Manifest.tsx` | "keeps hostile names inert inside the jobs table" (every `href` is `/jobs/<id>`) |
| Right-to-left overrides and invisible characters (`invoice‮gpj.pdf` showing as `.jpg`) | Each untrusted string is wrapped in `<bdi>` (direction isolated) and every control, format (bidi, zero-width) and separator character is shown as a visible `‮` token | `SafeText.tsx` | `SafeText.test.tsx` |
| `summary.md` rendered as HTML/Markdown | Shown as plain preformatted text (no Markdown renderer at all), with the same escapes; downloads go through the artifact route, which sends `Content-Disposition: attachment` and `nosniff` | `JobDetailPage.tsx` (`SummaryPreview`) | e2e "a PDF job ... summary downloads" |
| A malformed or hostile `manifest.json` | Parsed defensively (wrong types become empty values, never markup); capped at 16 MiB, previews at 256 KiB | `Manifest.tsx` (`parseRepoManifest`), `api.artifactText` | "reads a manifest defensively" |
| Injected scripts or third-party requests | Production Content-Security-Policy: `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'` (plus `frame-ancestors 'none'` as a header from `vite preview`). No inline scripts (the theme script is a file), no `data:` assets, Base UI told not to inject `<style>` tags. Fonts are bundled; no CDN, no analytics | `frontend/vite.config.ts`, `index.html`, `main.tsx` | every Playwright e2e test runs against the production build and fails on any console error (CSP violations are reported there) |
| Cross-origin access to the API | None needed: the page, `fetch` and `EventSource` all go to the Vite server, which proxies `/v1` and `/health`. The API sends no CORS headers | `vite.config.ts` | e2e (same origin) |
| Exposure on the network | The dev server listens on 127.0.0.1 on Windows; in Docker it listens inside the container and Compose publishes it on `127.0.0.1:5173` only | `vite.config.ts`, `compose.yaml` | `docker compose ps` |

The dev server (`npm run dev` / the `frontend` container) has no CSP: Vite's hot reload needs
inline scripts. The CSP applies to the built files.

## Not covered (yet)

- **Secrets inside indexed files.** A source file that contains an API key is indexed like any
  other file. With the stub provider nothing leaves the machine; before a real provider sends
  repository text to a remote API (Phase 11), secret scanning or redaction is needed.
- **Disk use of successful jobs.** A succeeded job keeps `work/{job_id}/files.bin` (up to
  `ZIP_MAX_TOTAL_BYTES`), and one upload can feed many jobs. Retention is Phase 12.
- **Bare-URL file names** (`www.example.com/x.md`) can become links in renderers that auto-link
  (GitHub-flavoured Markdown) when you open a downloaded `summary.md` elsewhere. The dashboard
  never auto-links (see above).
- **Authentication.** Anyone who can reach `127.0.0.1:8000` can upload.
- **Malware.** Files are never executed, but they aren't scanned either; downloaded artifacts
  contain repository text.
