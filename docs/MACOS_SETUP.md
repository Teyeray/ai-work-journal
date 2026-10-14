# macOS local setup

Create `config.local.json` and `webapp/config.local.json`; both are gitignored.
Never commit personal logs, screenshots, credentials, local paths or reports.

The report backend remains pi by default. Select a normal signed-in OpenCode CLI:

```json
{
  "llm_cli": {"backend": "opencode", "opencode_bin": "opencode", "timeout": 180},
  "codex_dir": "~/.codex/sessions",
  "opencode_db": "~/.local/share/opencode/opencode.db",
  "project_roots": ["~/Projects"]
}
```

`llm_cli.dir` may name an explicit existing workspace; otherwise it is this repository.
The current default model and ordinary plugins/authentication are reused. No model
is pinned or credential file copied. A unique report agent has native ask permissions
and runs without auto approval or interactive prompts; tool attempts and native errors
are rejected. Its sessions are excluded using the native IDs/title prefix.

Optional Codex/OpenCode sources are read-only. Message creation time determines the
day. Native parent links and fork metadata are retained separately; a user-channel
message does not prove physical typing or hand editing. File/session/message caps
limit scans. Claude/pi continue using their existing incremental adapters. The
optional readers rescan a bounded recent range; they do not imply full historical
coverage. dsh, web-chat message imports and document-edit authorship are not provided.

Local Chinese OCR uses Apple's Vision framework:

```json
{
  "host": "127.0.0.1", "port": 8799, "timezone": "Asia/Hong_Kong",
  "ocr": {"engine": "vision", "vision": {"languages": ["zh-Hans", "zh-Hant", "en-US"]}},
  "capture": {"enabled": true, "interval_sec": 60, "keep_days": 7},
  "llm": {"enabled": true}
}
```

Install the capture requirements and Xcode Command Line Tools. The bundled Swift
helper is compiled into the external user cache. Images and OCR stay local; no OCR
API key is needed. Report generation can send the selected digest/text to the user's
configured CLI provider. This is separate from screenshot transmission. Grant the
actual Python/terminal host Screen Recording when macOS requests it. ActivityWatch
provides foreground-window/AFK data and sensitive-app checks; install and run it.
Windows and OCR show observed activity, not proven authorship or completed tasks.

Build the frontend with `npm ci && npm run build`. If an npm lockfile generated on
another platform lacks native Rolldown/Tailwind optional packages, install the exact
matching platform package versions locally without modifying the dependency files.

Run `journal web`, then `journal daily YYYY-MM-DD`. Keep scheduled jobs uninstalled
until a real report is accepted; set `feishu_push` false in the external schedule file
when Feishu is not needed. Model failure preserves an existing good report; a new
fallback is marked clearly. The optional missing Feishu CLI must not block collection.

`llm.enabled` enables CLI session reviews. The separate tool-based “问 AI” feature
still needs its own OpenAI-compatible API configuration; OpenCode login is not copied
into API credentials. Leave API chat/classification disabled when it is not configured.
