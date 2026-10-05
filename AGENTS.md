
## AI agent context discipline

- Avoid reading generated artifacts, historical diff dumps, logs, caches, virtual environments and dependency directories unless necessary.
- Prefer targeted search before opening files.
- Do not scan the entire repository when the task can be solved from a small set of files.
- Keep delegated work narrow and bounded.
- Avoid repeated inspection of already-understood files.
- Run only tests relevant to the current change unless a full suite is necessary.
