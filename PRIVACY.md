# Privacy

Imece IDE has no telemetry, analytics or automatic error reporting.

The application reads and writes only the local project the user opens. A model
request is made only after the user starts an AI run. The task and selected
project context are then sent to the provider assigned to that role.

In the packaged Windows app, API keys are stored with Windows DPAPI under the
current user account. They are not returned to the UI or sent to any Imece
IDE service. The source-development `.env` workflow remains a local convenience
and should never be committed.

Running a project command and exporting a change receipt both require explicit
user action. A receipt is stored under the opened project's ignored `.imece/`
directory; Markdown export writes only to a folder chosen by the user.
