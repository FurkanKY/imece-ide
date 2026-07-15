# Privacy

Imece IDE has no telemetry, analytics or automatic error reporting.

The application reads and writes only the local project the user opens. A model
request is made only after the user starts an AI run. The task and selected
project context are then sent to the provider assigned to that role — and to
no other provider in the catalog. The optional "Test & save" key check sends a
single authentication request to that provider's `/models` endpoint and
nothing else.

API keys are stored per provider and never leave the machine: in the packaged
Windows app they are stored with Windows DPAPI under the current user account;
when running from source they live in the local git-ignored `.env`. Keys are
not returned to the UI or sent to any Imece IDE service. Agent CLIs (Claude
Code, Gemini CLI, Codex CLI, Qwen Code) use their own login and are only
detected on `PATH`.

Running a project command and exporting a change receipt both require explicit
user action. A receipt is stored under the opened project's ignored `.imece/`
directory; Markdown export writes only to a folder chosen by the user.
