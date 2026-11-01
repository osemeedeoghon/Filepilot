# FilePilot

A local-first file-management assistant for macOS.

- **Stage 1: safe local file scanner.** You choose a folder, and FilePilot lists its files and subfolders with their metadata.
- **Stage 2: smart organization preview.** FilePilot proposes how those files could be sorted into category folders, and explains why for each file.
- **Stage 3: exact duplicate detection.** FilePilot finds files with identical contents (SHA-256) and estimates the space that removing extra copies could free.

All stages are **strictly read-only**. FilePilot never creates, modifies, renames, moves or deletes files or folders. There is no apply or delete button and no endpoint that changes files, and nothing leaves your Mac. Stages 1 and 2 read only metadata. Stage 3 opens files read-only, solely to compute fingerprints.

## Prerequisites

- macOS. The native folder picker uses AppleScript. The scanner itself is plain Python, and on other systems you type the folder path instead.
- Python 3.10 or newer (developed and tested with Python 3.14).
- No accounts, API keys, paid services or AI models are needed.

## Installation

```bash
git clone https://github.com/osemeedeoghon/FilePilot.git
cd FilePilot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt        # runtime only
pip install -r requirements-dev.txt    # adds the test tools
```

## Launch

```bash
source .venv/bin/activate
python -m filepilot
```

Then open http://127.0.0.1:8765, or run `open http://127.0.0.1:8765` in another terminal. Press `Ctrl+C` to stop the server. The server always binds to `127.0.0.1`. Set `FILEPILOT_PORT` to use another port.

## Testing

```bash
source .venv/bin/activate
python -m pytest -q
```

## Architecture

FilePilot is a small local web app: a **FastAPI** backend and a **vanilla HTML/CSS/JavaScript** frontend, with no build step and no external requests.

- The backend exposes a few JSON endpoints on `127.0.0.1` and serves the static UI.
- `scanner.py` is the shared foundation. It validates the chosen folder and walks it read-only, using `lstat` metadata and never following symbolic links.
- `organizer.py` (Stage 2) turns scan metadata into a proposed plan with a pure, deterministic rules function.
- `duplicates.py` (Stage 3) groups files by size and confirms matches with chunked SHA-256 reads. `jobs.py` runs it in a background thread so the UI can poll progress and cancel.
- Request guards (allowed Host and Origin, JSON-only bodies, strict validation) and security headers live in `app.py`.

## Project layout

```
filepilot/
  __main__.py    # `python -m filepilot` starts uvicorn on 127.0.0.1
  app.py         # FastAPI app: routes, request guards, security headers
  config.py      # limits and settings (env-overridable)
  scanner.py     # read-only, bounded folder scanner and path validation
  organizer.py   # Stage 2: deterministic, read-only organization planner
  duplicates.py  # Stage 3: size -> SHA-256 duplicate detection, safe chunked reads
  jobs.py        # Stage 3: background job with progress/cancel for duplicate scans
  picker.py      # native macOS folder picker (AppleScript `choose folder`)
  static/        # index.html, styles.css, app.js, favicon.svg (no build step)
tests/
  conftest.py        # temporary folder trees used by the tests
  test_scanner.py    # Stage 1 scanner
  test_api.py        # HTTP API and security guards for all stages
  test_organizer.py  # Stage 2 rules, conflicts, boundaries
  test_duplicates.py # Stage 3 matching, unreadable/vanishing files, symlinks, limits
requirements.txt      # runtime: fastapi, uvicorn
requirements-dev.txt  # adds pytest, httpx
```

## Stage 1: scanner

- **Folder selection.** A **Choose folder…** button opens the native macOS dialog via `osascript`, with a fixed script and no shell. A path text field is always available as a fallback.
- **Recursive scan** of the selected folder only. Each entry shows name, type, size, last-modified date and relative path.
- **Summary metrics:** files, folders, total size and skipped items.
- **Browsing:** search, a file-type filter, sortable columns, paging, and an optional "Include hidden files" checkbox.
- **Clear states:** ready, scanning, complete, empty, error and partial.

## Stage 2: organization preview

After a scan, open the **Organize preview** tab.

- **Choose categories** (Documents, Images, Videos, Audio, Spreadsheets, Presentations, Archives, Code, Other). Each chip shows how many files fall into that category. Unselected categories are left in place.
- **Choose which files:**
  - *Top-level files only* (default): only loose files directly in the folder. `photo.jpg` goes to `Images/photo.jpg`.
  - *Include subfolders*: subfolder paths are kept inside the category. `Trip/beach.png` goes to `Images/Trip/beach.png`.
- **For every file**, the plan shows the current relative path, suggested category, proposed destination, a status and the reason.
- **Statuses:**
  - **Ready to move**: no rule found any reason for doubt.
  - **Name conflict**: the destination name is already taken. The proposal gets a free alternative such as `photo (2).jpg`, and is never an overwrite.
  - **Needs review**: something about the file is uncertain (see below).
  - **Left in place**: a rule says the file should not be moved.
- **Summary counts:** the four status cards also act as filters. There is search across current and proposed paths, plus status and category filters.
- **Clear about safety:** a banner states that nothing has been changed. There is no apply button and no endpoint that changes files.

### The rules (deterministic, no AI or network)

The category comes from the file extension. A plain **move** is proposed only when no other rule objects.

| Result | When |
|---|---|
| **Left in place** | Symbolic links (never moved or followed) · hidden files and anything in hidden folders · files inside macOS packages (`.app`, `.photoslibrary`, `.xcodeproj`, `.rtfd`…) · files inside software projects (a folder containing `.git`, `package.json`, `pyproject.toml`, `Makefile`, `Cargo.toml`…) · temporary or partial downloads (`.crdownload`, `.part`, `~$…`) · likely credentials (`.pem`, `.p12`, `id_rsa`…) · apps and executables (`.exe`, `.command`) · files already in the matching category folder · categories you didn't select · subfolder files in top-level mode |
| **Needs review** | Unknown extension or no extension · ambiguous extensions (`.ts` = TypeScript or video, `.key` = Keynote or crypto key, `.svg`, `.html`, `.json`, `.xml`, `.raw`) · double extensions such as `invoice.pdf.zip` (`.tar.gz` is allowed) · executable permission bit set · empty (0-byte) file · modified in the last 10 minutes · filed in a *different* category folder (e.g. `Images/notes.txt`) · destination folder name taken by a file or symlink · destination folder unreadable or not scanned · the scan hit a limit (then every move is marked for review) |
| **Name conflict** | The destination already exists, or two files would get the same destination. Names are compared case-insensitively and Unicode-normalised, like APFS. A same-size match is flagged as a *possible duplicate*. |

Further guarantees:

- Every proposed destination is a relative path that is checked to stay inside the selected folder (`ensure_within_root`).
- Every proposed destination is unique among proposals and doesn't match any existing file, folder or symlink. Hidden items are scanned for this check even when they aren't shown.
- An alternative name never takes a name another file wants.
- An existing category folder's spelling is reused (e.g. an existing `images/`).
- If the selected folder itself is a software project, no moves are proposed and a warning is shown.

## Stage 3: duplicate detection

After a scan, open the **Duplicates** tab and click **Find duplicates**. The check runs in the background with a live progress bar, showing files and bytes read, elapsed time and the current file. **Cancel** stops it and keeps the results found so far.

**How files are compared:**

1. Only regular, non-empty files are considered. Folders are never treated as files. Symbolic links are counted but never followed or read. Empty files are reported separately because they take no space.
2. Files are grouped by **size**. A file with a unique size cannot have a duplicate and is never opened.
3. For files over 1 MB, the first 64 KB is hashed first. Files whose beginnings differ are ruled out without reading the rest.
4. The remaining candidates are confirmed with a **SHA-256 of their full contents**, read in 1 MB chunks so large files are never loaded into memory. File names play no part.

**Each group shows:**

- the number of copies, the size of each, and the potential space saved
- a short SHA-256 (hover to see the full hash)
- every path and its modified date

Summary cards show groups, extra copies, recoverable space (estimated) and files compared. You can search paths and sort by recoverable space, size, number of copies or path.

**Recoverable space is an estimate:** `size × (distinct copies − 1)` per group, assuming you keep one copy. Paths that are hard links to the same data are detected (same device and inode). They're counted once and the group is labelled, since removing one frees nothing. The estimate uses each file's logical size, so APFS clones, snapshots and sparse files may mean less space is actually freed.

**Skipped safely:**

- unreadable files and folders (permission denied)
- files that disappear, change size or change while being read
- items that are no longer regular files (e.g. a FIFO swapped in)
- read errors

Each skipped file is listed with its reason, and the scan continues. App packages (`.app`, `.photoslibrary`…), version-control folders (`.git`…) and dependency folders (`node_modules`…) are not opened, because their internal copies are not yours to clean up. Hidden files follow the "Include hidden files" checkbox.

**How files are opened.** Every file is opened read-only, starting from the selected folder and going one path component at a time with `O_NOFOLLOW`. A symbolic link anywhere in the path, even one swapped in after the listing, makes the open fail instead of escaping the folder. Relative paths are also checked with `ensure_within_root`.

**Limits:**

- the file listing has the same limits as Stage 1 (20,000 entries, depth 16)
- hashing stops after 10 minutes or 100 GB read (`FILEPILOT_DUP_MAX_SECONDS`, `FILEPILOT_DUP_MAX_GB`)
- one duplicate check runs at a time
- the five most recent results are kept in memory

When a limit stops the check, results are marked partial and the number of unchecked files is shown. Candidates with the largest potential savings are checked first, so a partial result still finds the most space.

## Safety measures (all stages)

| Concern | How it's handled |
|---|---|
| Network exposure | Always binds to `127.0.0.1`; `TrustedHostMiddleware` rejects non-loopback `Host` headers. |
| Other websites calling the API | A foreign `Origin` gets a 403 on POST, and non-JSON bodies get a 415. |
| Path traversal | `..` segments, relative paths, control or NUL characters and over-long paths are refused. Plan paths are re-validated with `ensure_within_root`. |
| Symbolic links | A symlink chosen as the folder is rejected. Symlinks inside are listed but never followed, moved or read. A symlinked category folder is never used as a destination, and duplicate hashing opens files component by component with `O_NOFOLLOW`. |
| Whole-Mac scans | Never automatic. `/`, `/System`, `/Library`, `/Users`, `/Applications`, `/Volumes`, `/usr`, `/private`, `/var`, `/etc`… are refused. |
| Unreadable items | They are skipped and reported. Destinations inside unreadable folders are marked for review. |
| Resource limits | 20,000 entries, depth 16, 30 seconds per scan, 2 concurrent scans or previews. Duplicate hashing is capped at 10 minutes or 100 GB, one job at a time, and can be cancelled. Unknown request fields are rejected. Booleans must be real JSON booleans, categories and scope are checked against fixed lists, and job IDs must match a strict pattern. |
| File modification | No code path writes to the filesystem, and files are only ever opened with `O_RDONLY`. The API routes are: `GET /api/health`, `POST /api/scan`, `POST /api/pick-folder`, `POST /api/organize/preview`, `POST /api/duplicates/start`, `GET /api/duplicates/{id}` and `POST /api/duplicates/{id}/cancel` (cancel only stops FilePilot's own background check). Tests confirm that contents, sizes, modes and mtimes are unchanged after a preview or a duplicate check. |
| Front-end injection | A strict CSP allows no inline scripts. All file names are rendered with `textContent`. |
| Privacy | No external requests, CDNs or analytics. File contents, hashes, names and paths stay on this Mac. They go only to the browser page on `127.0.0.1`. |

Limits can be tuned with `FILEPILOT_PORT`, `FILEPILOT_MAX_ENTRIES` and `FILEPILOT_MAX_DEPTH`. The host cannot be changed.

## What was tested

There are 197 automated tests (`python -m pytest -q`), all passing. To run one stage's tests, use `python -m pytest -q tests/test_duplicates.py` (or another test file).

- **Stage 1 (71 tests):**
  - valid scans and file metadata
  - invalid paths and blocked locations
  - `..` traversal, symlinks, link loops and root-boundary checks
  - unreadable folders and scan limits
  - API guards (Origin, Host, content type, malformed bodies, no write routes, generic 500s)
  - the folder picker, with `osascript` mocked
- **Stage 2 (74 tests):**
  - **Categorization:** every category, unselected or empty selections, invalid category or scope, and the summary counts.
  - **Proposed paths:** top-level vs. subfolder scope, already-organized files, reuse of an existing folder's spelling, files filed in the wrong category folder.
  - **Duplicate names:**
    - existing destination (renamed, never overwritten)
    - same-size possible duplicates
    - two files wanting one name
    - case- and Unicode-insensitive clashes
    - `.tar.gz` alternatives
    - alternatives never taking another file's name
    - a category folder name blocked by a file
  - **Hidden files:** excluded and counted, shown but never moved, hidden folders not opened.
  - **Unknown and uncertain files:** unknown or missing extension, ambiguous extensions, double extensions, executable bit, empty files, recently modified files.
  - **Left in place:** executables, temp downloads, credential files, macOS packages, software projects, a project as root.
  - **Symbolic links:** link files and folders kept and not followed, symlinked category folder rejected, symlink root rejected.
  - **Traversal and root boundary:** `ensure_within_root` against 10 escape patterns, `..` in the root path, malicious names injected into scan data, and every destination staying inside the root.
  - **Limits:** a truncated scan marks everything for review, and an unreadable destination folder is not trusted.
  - **Read-only:** a before/after snapshot of the tree (no category folders created).
  - **API:** valid preview, malformed requests (including an `apply` field), invalid paths, traversal, symlink root, cross-origin, read-only check, and the Stage 1 `/api/scan` response shape unchanged.

- **Stage 3 (52 tests):**
  - **Matching:** identical files with different names are grouped, same-name files with different contents are not, different sizes are never hashed, and empty files are skipped. Also covered: multiple groups and totals, and hard links not counted as recoverable.
  - **Large files:** the prefix pass then full hash (with exact byte counts), and chunked reads (a spy confirms no read exceeds the chunk size).
  - **Unsafe states:** unreadable files and folders, a file deleted mid-scan, a file changing size, a FIFO that must not block, and read errors.
  - **Symbolic links:** link files and folders ignored, a symlink as the final path component refused, a symlink swapped into a parent folder refused, symlink root rejected.
  - **Root boundary:** `open_beneath` escape attempts, a `../` file path, the reused root validation (invalid, missing, `/`, traversal), and every reported path staying inside the root.
  - **Protected and hidden:** packages, `.git` and `node_modules` not opened; the hidden-file toggle.
  - **Limits:** byte limit, time limit, cancellation, progress reaching completion.
  - **Read-only:** a before/after snapshot of contents, sizes, modes, mtimes and inodes.
  - **API:** job start, poll and result; the hidden toggle; fast failure for invalid, blocked and traversal paths and symlink roots; malformed requests; one job at a time (409); cancel; unknown or malformed job IDs; cross-origin; read-only; safe internal errors.

Manual checks in the browser:

- Scanned a demo folder and opened the preview. Conflict detection and renaming, the double extension, unknown type, temp download, symlink, executable, software project and macOS package all behaved as expected.
- Switched scope, used the status-card filters, and switched back to the Files tab.
- Checked desktop and narrow layouts with no horizontal scroll, and found no new console errors.
- Confirmed the demo folder had no new files or folders afterwards.
- **Stage 3:** ran a demo folder with copies under different names, same-name files with different contents, hard links, empty files, a symlink, an `.app` package and an unreadable copy. The groups, the recoverable estimate (17.6 MB), the hard-link note and the skip reasons were all correct.
- **Stage 3, large files:** ran 6 × 3 GB sparse files and watched the live progress bar track bytes read. Cancel returned partial results.
- **Stage 3, layout:** checked desktop and phone layouts with no horizontal scroll.

## Changes to Stage 1 code

- `scanner.scan_folder` gained optional `collect_folders` and `prune` arguments, and `FileEntry` gained an internal `executable` flag. These stay out of `to_dict()`, so the `/api/scan` response is unchanged (a test checks this). Stage 1 behaviour is otherwise the same.
- `/api/health` also lists `organize_categories` and the duplicate limits.
- Stage 3: the UI tab switching now handles three tabs. The `include_hidden` field in every request now requires a real boolean, which tightens validation; the UI already sent booleans.
- The UI shows the file list and the preview as two tabs. The Stage 1 elements and behaviour are unchanged.

## Known limitations

- **Organizer: types come from extensions plus metadata only** (size, permissions, mtime, location). File contents are never read, so a mislabelled file with no other warning signs will still be proposed as a move. This is why there is a "Needs review" status and why nothing can be applied yet.
- **Conflicts are checked against the folder as it was scanned.** Files changed after the preview aren't reflected until you refresh the preview.
- **Conservative by design.** The plan doesn't count on names freed by moving other files away; it renames instead. Some files you'd happily move will show as "review" (e.g. any `.json` or `.html`, or files copied from a USB drive where every file has the executable bit).
- **Top-level only by default.** In subfolder mode, a mixed folder like `Trip/` gets split across categories (`Images/Trip/…`, `Documents/Trip/…`). The note on each item says so.
- **Bundle and project detection uses names** (e.g. `.photoslibrary`, `package.json`). Unusual project layouts may not be recognised.
- **macOS privacy (TCC).** Desktop, Documents, Downloads and iCloud Drive may need *Files and Folders* or *Full Disk Access* for your terminal app, or their items show as skipped.
- **The native folder picker** may open behind the browser, and its real dialog was only tested with mocks.
- **Hard links** count once per path.
- **Large folders** stop at 20,000 entries or 30 seconds, and the preview is then marked partial. The Stage 1 file list still opens macOS packages (as before); the preview does not.
- **Duplicates: estimates, not guarantees.** The space figure uses logical file sizes, so APFS clones, snapshots, sparse files and iCloud "optimised storage" placeholders can make the real saving smaller. Hard links are detected, but APFS clones are not.
- **Duplicates: reading updates access times.** Opening files may update their "last opened" time (atime) on some volumes. Contents, sizes, permissions and modified times are never changed.
- **Duplicates: limits.** The check covers what the listing found (up to 20,000 entries). Very large folders may hit the 10-minute or 100 GB limit and return partial results. Progress lives in memory, so restarting the server forgets running and finished checks.
- **Duplicates: what's skipped.** Files inside app packages, `.git` and `node_modules` are deliberately skipped. Duplicates there won't be reported.
- **Duplicates: speed.** Hashing speed depends on the disk. iCloud files that aren't downloaded may be fetched by macOS when read, or fail and be listed as skipped.
- A deprecation warning from Starlette's test client appears in test output. It does not affect the app.
