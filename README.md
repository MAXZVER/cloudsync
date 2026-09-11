# yasync — selective cloud sync for macOS that does not run all day

> 🇷🇺 [Русская версия](README.ru.md)

Pick folders from a cloud storage and keep them in sync, in three files, with a real three-way merge
for conflicts — including Office documents. No resident daemon and no polling loop: it sleeps until
the kernel wakes it.

**Any provider [rclone](https://rclone.org/overview/) supports** — Yandex.Disk, Google Drive,
Dropbox, OneDrive, S3, WebDAV, SFTP. The provider is not baked in: you pick one of your configured
rclone remotes from the menu. Only one line of the code knows about a specific backend, and it is a
`--yandex-upload-wait` quirk.

Works on any Mac. Built and tested against Yandex.Disk on macOS Sequoia 15.7.9.

| | |
|---|---|
| [`yasync.py`](yasync.py) | The engine: config, sync, conflict detection, ancestor snapshots |
| [`yadiff.py`](yadiff.py) | Three-pane merge UI, served on `127.0.0.1` |
| [`CloudSync.swift`](CloudSync.swift) | Menu-bar app: storage and folder picker, status, and the decision of *when* to sync |

Dependencies: [`rclone`](https://rclone.org/) — one static binary — and macOS itself. The Python side
is standard library only: no `pip`, no virtualenv. The Swift side is one file, built with the Command
Line Tools; Xcode is not needed.

---

## Install

```bash
# rclone, if you do not have it
mkdir -p ~/bin && curl -fsSL https://downloads.rclone.org/rclone-current-osx-amd64.zip -o /tmp/rc.zip
unzip -jo /tmp/rc.zip '*/rclone' -d ~/bin && chmod +x ~/bin/rclone

# set up a remote if you have none — rclone asks the questions and keeps the token
~/bin/rclone config

cp yasync.py yadiff.py ~/bin/ && chmod +x ~/bin/yasync.py
~/bin/yasync.py remotes          # what rclone knows about
~/bin/yasync.py init yandex:     # or drive:, dropbox:, s3:, …

swiftc -O CloudSync.swift -o ~/bin/CloudSync
~/bin/CloudSync &
```

Pick the storage and the folders from the menu-bar icon, and turn on **Запускать при входе** to
keep it there. The local root defaults to `~/<StorageName>` and is recorded in the config, so it
never moves under folders that are already syncing.

The OAuth token lives in rclone's own config file. Nothing here reads, copies or logs it.

> The interface is in Russian, because that is who it was written for. The code and this README are
> in English.

---

## When it syncs, and why that list is short

No timer, no polling loop. The app is idle until the kernel wakes it, and exactly four things do:

| Trigger | Mechanism | Why |
|---|---|---|
| A file changed locally | `FSEventStream` on the sync root, 2 s kernel coalescing + 10 s debounce | Push from the kernel. A burst of saves costs one sync |
| Finder became active | `NSWorkspace.didActivateApplicationNotification`, throttled to once per 2 min | The closest thing macOS has to "the user opened a folder" |
| Woke from sleep | `NSWorkspace.didWakeNotification` | The remote has probably moved on while the lid was shut |
| Network came back | `NWPathMonitor`, only after it had been down | Retry the sync that failed offline, once, when it can succeed |

**There is no "folder was opened" event in macOS.** No public API reports it and Finder does not
broadcast it. Anything claiming otherwise is a polling loop or an AppleScript asking Finder for its
front window. Finder activation is the honest approximation, which is why it is throttled.

Idle cost is what an idle `NSStatusItem` costs: no timers, no wakeups, no CPU.

---

## Sync itself: `rclone bisync`

Two-way sync is a solved problem, and this is not the place to re-solve it badly. `rclone bisync`
finds changes on both sides, including deletions and conflicts, and keeps state in a workdir.

The flags that matter:

```
--conflict-resolve none        # never pick a winner behind the user's back
--conflict-loser pathname      # keep BOTH versions, renamed
--conflict-suffix local,remote # → doc.txt.local and doc.txt.remote
--resilient --recover          # survive small interruptions without demanding a full resync
```

Those first two are the whole basis of the merge feature: when both sides changed, rclone leaves
`doc.txt.local` and `doc.txt.remote` side by side, and that is the input to the diff.

### The safety abort is never bypassed silently

`bisync` refuses to proceed when a suspiciously large share of files changed at once — that pattern
usually means a folder was moved or emptied by accident, not edited. `yasync.py` catches the abort,
records `lastResult: needs-confirm` with the reason, and stops. The menu then offers a confirmation
that shows what `bisync` actually said before `--force` is added.

Passing `--force` automatically would make the tool convenient and occasionally catastrophic.

---

## Conflicts: a real three-way merge

Open a conflict from the menu and a local HTTP server on `127.0.0.1` (random port) serves a merge
page: **left is the storage, right is this Mac, the middle pane is the live result.**

- The chevron in a gutter points **inward** to take a hunk into the result, and flips **outward**
  once it is there, so one click puts it back.
- When one side is already in, the other offers **add below** and **add above**.
- When both are in, ↑↓ reorder them, and a badge shows the order.
- The result pane **tints each line with where it came from** and says so on hover.
- Word-level highlighting inside changed lines, a change ruler down the right edge, undo/redo over
  every step, <kbd>F7</kbd> to walk the differences, and an overlay for editing the result by hand.

### Why two versions are not enough, and where the third comes from

A line present on the left and absent on the right is either *"the left side added it"* or *"the
right side deleted it"*. These are different facts, and **a diff between two versions cannot tell
them apart.** Neither can timestamps: a file has one `mtime` for the whole file, so it can say which
version was saved later but never which *line* changed later. Per-line history does not exist
anywhere on disk.

Only a third version — the common ancestor — separates those cases.

So `yasync.py` keeps one. After every successful sync, when both sides agree by definition, it
snapshots the folder into `~/Library/Application Support/CloudSync/base/`: gzipped, incremental (a
file is re-copied only if its size or mtime changed), skipping anything over 32 MB. Files currently
in conflict are deliberately *not* re-snapshotted — their old ancestor is exactly what the merge
needs.

With an ancestor, every hunk is classified:

| Both sides vs ancestor | Verdict |
|---|---|
| Only the left changed | Apply the left. Unambiguous |
| Only the right changed | Apply the right. Unambiguous — **including deletions**, which a two-way diff would have mistaken for an addition on the other side |
| Both changed, identically | Not a difference at all; not shown |
| Both changed, differently | A genuine conflict. Left for the human |

Unambiguous hunks are pre-applied when the page opens, and the toolbar button re-applies them after
you have undone things. Genuine conflicts are never auto-merged.

Without an ancestor — a file that appeared already in conflict, or one over the size limit — the page
says so, and auto-merge falls back to the only case that stays unambiguous with two versions: a
one-sided insertion. The status bar labels that count approximate rather than pretending otherwise.

### Office documents

`.docx`, `.doc`, `.rtf`, `.odt` are converted with `textutil`, which ships with macOS. `.xlsx` and
`.pptx` are unzipped and their XML read with the standard library — spreadsheets come out as
`Sheet!A1: value` lines, presentations as slide text. So you can *see* what differs inside an Office
document.

You cannot rebuild a `.docx` from text, so those merge per-file rather than per-hunk: pick a side.
The page says so instead of offering buttons that would produce a corrupt document.

`.xls` and `.ppt` — the old binary formats — are not read at all.

---

## Files and state

| Path | What |
|---|---|
| `~/<StorageName>/` | The synced folders themselves |
| `~/Library/Application Support/CloudSync/config.json` | Which storage, which folders, when each last synced |
| `~/Library/Application Support/CloudSync/bisync/` | `rclone bisync` state |
| `~/Library/Application Support/CloudSync/base/` | Gzipped ancestor snapshots |
| `~/Library/Logs/cloud-sync.log` | Log |
| `~/.config/rclone/rclone.conf` | The OAuth token — rclone's, not ours |

The engine is usable on its own:

```bash
yasync.py remotes             # storages rclone knows about
yasync.py init drive:         # choose one
yasync.py ls                  # folders in it
yasync.py add Документы       # take one under sync (first run does a resync)
yasync.py sync --all
yasync.py status              # or: state, for JSON
yasync.py conflicts
yasync.py resolve doc.txt     # opens the merge page
```

---

## Honest limits

- **The ancestor only starts existing after the first successful sync.** A conflict that predates the
  snapshot gets the two-way fallback.
- **Files over 32 MB have no ancestor** and never will — line-merging them is not a real workflow.
- **An edit made during a sync can be missed by the file watcher.** It ignores events while syncing
  and for 5 s after, because otherwise the tool's own writes retrigger it forever. Such an edit is
  picked up at the next trigger, not instantly.
- **Finder activation is a proxy**, not a real "folder opened" signal — see above.
- **Case sensitivity and extended attributes** are `rclone`'s business, not ours; Finder tags and
  resource forks do not survive a round trip.
- **Not affiliated with any of the storage providers.** All network work is `rclone`'s.
- **Only one backend quirk is handled** (`--yandex-upload-wait`). Another provider may need its own;
  that is one line in `BACKEND_FLAGS`.

## License

[MIT](LICENSE).
