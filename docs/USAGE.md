# Usage

> The application UI is currently Turkish. This guide names UI actions in
> English with the Turkish label in parentheses where it helps you find the
> control.

All three interfaces share the same engine; pick the one that fits the job.

| Interface | Best for |
|-----------|----------|
| **Desktop IDE** (`shell.py`) ★ | working on an existing project — the full experience |
| **Web** (`app.py`) | generating a single file from scratch, watching the flow live |
| **CLI** (`orchestrator.py`) | quick, automation-friendly single-file generation |

---

## Desktop IDE (`shell.py`) ★

```bash
# build the frontend once (see SETUP.md §3):
cd web/ui && npm ci && npm run build && cd ../..

python shell.py            # loads web/ui/dist via app://
python shell.py --dev      # Vite dev server (HMR) + F12 DevTools — development
```

Layout: a web **titlebar** on top (frameless — drag / double-click to
maximize, its own min/max/close), the **activity bar** and **explorer** on the
left, the multi-tab **Monaco editor** in the center, the **AI team panel** on
the right, and the **status bar** at the bottom.

### Working with an AI team

1. Type a task in the composer (e.g. *"convert the date format in utils.py to
   ISO 8601"*), optionally change the role→model assignments, press **▶** (or
   Enter).
2. The **team pipeline** runs live: the active agent pulses; finished stages
   show model · time · tokens · cost. The flow tab shows stage cards with
   rendered markdown output; errors appear as red cards.
3. When a proposal arrives, the **Changes** tab opens together with the center
   **inline diff**. Review file by file; untick anything you don't want.
4. **Apply (Uygula)** writes the files — a checkpoint is taken first, and the
   toast offers one-click **Undo**. **Reject (Vazgeç)** writes nothing. Stop a
   run any time with **■**.
5. The status bar streams total tokens/cost; the clock icon opens run history —
   click an entry to bring its task back into the composer. `Ctrl+J` toggles
   the panel.

**Safety:** agents can never write outside the folder you opened
(path safety in `project.py`).

### Change receipts

After each run, pick **Receipt (Makbuz)** from the history drawer. A receipt
stores the task, plan/scope, proposed diff, reviewer verdict, cost, and
apply/checkpoint state. If no test or command was executed, the receipt says
so explicitly instead of implying proof. Receipts live in the project's
ignored `.imece/` directory and can be exported as Markdown to a folder you
choose.

### Editor and workspace

- **Open Folder** from the welcome screen (or recent projects) → lazy explorer
  tree. Click a file → it opens in a tab with syntax highlighting. `Ctrl+S`
  saves (• marks unsaved), `Ctrl+W` closes.
- **Right-click** in the explorer: New File/Folder, Rename, Delete, Copy Path,
  Reveal in Explorer. **Drag and drop** moves files between folders; tabs
  reorder by drag, and tab right-click offers Close Others/Right/All.
- `Ctrl+P` go to file (fuzzy), `Ctrl+K` command center, `Ctrl+B` toggle
  explorer. `Ctrl+=` / `Ctrl+-` / `Ctrl+0` zoom the UI; `Alt+Z` toggles word
  wrap; the diff tab has a side-by-side ↔ inline toggle.
- Unsaved tabs are protected by a close confirmation; open tabs, the active
  tab and panel layout are restored per project (`.imece/session.json`).
- External changes (another editor, git) refresh the tree automatically.

### Language intelligence

- **Python** — the basedpyright language server starts when a project opens
  (status bar shows it becoming ready). Completions (`Ctrl+Space`),
  diagnostics with red underlines, **F12 / Ctrl+click** go-to-definition
  across files, hover signatures/docstrings, parameter help on `(`.
- **TS/JS** — the same features from Monaco's built-in language service.

### Run and debug

- **F5** follows the VS Code convention: continue if a debug session is
  paused; start debugging if the active file is `.py`; otherwise run the file.
  **Ctrl+F5** runs the project without debugging (npm dev/start, cargo, go, or
  main/app.py — heuristic; override via the command palette entry "Change Run
  Command…" → `.imece/run.json`). **Shift+F5** or ■ stops.
- A project-provided command is shown for **approval before its first run**,
  including its source and working directory; approval is remembered per
  project and re-requested when the command changes.
- Output (run or debug) streams into the **OUTPUT** tab of the bottom panel,
  with a green/red exit-code badge and duration.
- **Debugging** — click left of a line number (or **F9**) for a breakpoint
  (persisted per project). The bug icon in the activity bar opens the Run and
  Debug view: start, then when paused you get the control strip (continue ·
  **F10** step over · **F11** step into · **Shift+F11** step out · stop), the
  call stack (click → jump to line), a lazy variables tree and the breakpoint
  list. The paused line is highlighted amber.

### Terminal

`Ctrl+\`` toggles the panel, `Ctrl+Shift+\`` opens a new terminal (tabbed).
Real ConPTY PowerShell: arrow keys, colors, `python` REPL and interactive
programs all work. Opens at the project root, UTF-8.

### Search and source control

- `Ctrl+Shift+F` — project-wide search with case and regex toggles; results
  grouped by file; click a line to jump. Uses ripgrep when installed, a Python
  scan otherwise.
- `Ctrl+Shift+G` — source control view: branch + ahead/behind counters, change
  lists with status letters (M/A/D/R/U). Click a row for the center diff;
  hover actions stage (+), unstage (−) or discard (↩, confirmed). Write a
  message and **Commit** (`Ctrl+Enter`). Git status is visible everywhere:
  changed files are colored in the explorer and counted in the status bar.
  Remote operations (push/pull/branch) are not included in this beta.

### Settings

Gear icon or `Ctrl+K` → "Settings": accent color (applied live), density,
Enter behavior, animations. Motion respects the OS reduced-motion preference.

---

## Web interface — generate from scratch

```bash
python app.py        # → http://127.0.0.1:5000
```

Enter a task, assign a model to each role, set rounds and the "run the code"
option. Each step appears as a card with **model · time · tokens · cost**. The
result is written to `output/result.py`. With "run the code" enabled
(execution grounding), the generated code is actually executed and any
output/error is fed back to the Coder.

## CLI

```bash
python orchestrator.py "code that prints the primes from 1 to 10"
python orchestrator.py "..." --run      # actually execute the generated code
```

The flow prints to the terminal (PLAN → CODE → run → REVIEW → fix), the result
is saved to `output/result.py`, and totals (time/tokens/cost) are shown.

---

## Changing roles and models

- From the UI: the Planner/Coder/Reviewer dropdowns.
- Permanently in code: `DEFAULT_ROUTING` in `agents.py`.
- To change a role's instructions: `ROLE_PROMPTS` in `agents.py`.

## Reducing cost

Most of the cost typically comes from the Planner when it runs on a premium
model. Try routing the Planner to a cheaper provider and compare — per-step
metrics are shown on every run.
