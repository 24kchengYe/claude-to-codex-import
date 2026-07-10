---
name: claude-to-codex-import
description: Import local Claude Code conversations into Codex on macOS or Windows, including repairing Codex Desktop imported timestamps, finding all Claude project JSONL sessions, backing up Codex state, updating state_5.sqlite metadata, compacting long imported rollouts into archive entries, and preserving original Claude JSONL files.
---

# Claude to Codex Import

Use this skill when a macOS or Windows user wants to migrate local Claude Code history into Codex, fix Codex Desktop's imported session timestamps, compact long imported conversations, or explain why the built-in importer missed older Claude sessions.

## What to Know

- Claude Code sessions normally live under `~/.claude/projects/**/<session-id>.jsonl`.
- Codex Desktop state normally lives under `~/.codex/state_5.sqlite`.
- Codex conversation files normally live under `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`.
- Codex Desktop's external import index is `~/.codex/external_agent_session_imports.json`.
- On Windows, `~` usually expands to `C:\Users\<user>` in PowerShell/Python, so the same defaults target `C:\Users\<user>\.claude` and `C:\Users\<user>\.codex`.
- The built-in Codex importer may import only recent Claude sessions. To import all local history, enumerate Claude JSONL files directly.
- Imported Codex timestamps should come from Claude JSONL line-level `timestamp` fields: earliest line for creation time, latest line for update/recency time.
- Very long imported Claude sessions are archives, not good continuation targets. Opening them as active Codex threads can exceed the model context window because Codex may load the full converted rollout.
- On Windows Codex Desktop, a Claude archive row may be readable but fail when the user sends a new message because the app-server tries to resume an old external-agent thread id. Treat archive rows as read-only history. For continuation, create a separate Codex-native thread through app-server `thread/start` + `thread/inject_items`; do not hand-write rows into `state_5.sqlite` and expect them to be resumable.
- Imported/read-only archive threads may also reject mid-conversation model changes with `thread/settings/update: thread not found`. This is expected for archive compatibility rows. New native continuation threads inherit the current Codex model unless the user later selects another model.
- Continuation threads can preserve the original Claude session timestamp, so migrated history stays under the right project and date instead of all appearing as new work today.
- A very large resumed rollout with repeated compactions or embedded images can repeatedly disconnect WebSocket transport before falling back to HTTP. Prefer a compact handoff into a new native thread instead of continuing a 100 MB-class archive.
- Codex Desktop resume/startup failures after migration are not always bad rollout data. On Windows, also inspect Desktop logs, config feature keys, custom MCP/app settings, and any local app-server wrapper before editing sessions.

## Workflow

1. Confirm the environment and inspect paths:
   - `~/.claude/projects`
   - `~/.codex/state_5.sqlite`
   - `~/.codex/external_agent_session_imports.json`
   - `~/.codex/sessions`
2. Run a dry run first:
   ```bash
   python3 scripts/import_claude_sessions_to_codex.py --dry-run
   ```
3. If Codex already imported sessions but gave them identical timestamps, repair existing imported thread timestamps:
   ```bash
   python3 scripts/import_claude_sessions_to_codex.py --fix-existing-timestamps
   ```
4. Import missing Claude sessions:
   ```bash
   python3 scripts/import_claude_sessions_to_codex.py --import-missing
   ```
5. For long imported Claude sessions, convert them into compact archive entries:
   ```bash
   python3 scripts/import_claude_sessions_to_codex.py --archive-and-compact-imports --dry-run
   python3 scripts/import_claude_sessions_to_codex.py --archive-and-compact-imports
   ```
   This keeps Codex's thread list clickable while storing full original JSONL files and Markdown summaries under `~/.codex/imported_claude_archive/`.
   Existing manually edited Codex thread titles are preserved by default. Use `--overwrite-manual-titles` only when the user explicitly wants regenerated titles to replace UI edits.
6. If the user wants to continue old Claude sessions from Codex Desktop on Windows, create Codex-native continuation threads from the archive summaries through app-server:
   ```bash
   python3 scripts/create_native_continuation_threads.py --dry-run
   python3 scripts/create_native_continuation_threads.py
   ```
   This uses Codex's own app-server to create threads and injects a compact prompt pointing to the Markdown summary and original Claude JSONL. It then patches display timestamps back to the original Claude session time.
   The script inherits existing proxy environment variables. Use `--proxy <url>` only when an explicit override is required, for example `--proxy http://127.0.0.1:2080` on a machine with that local proxy.
   To test one archive first:
   ```bash
   python3 scripts/create_native_continuation_threads.py --thread-id <archived-thread-id>
   ```
7. Legacy fallback only: if app-server creation is unavailable, create database-only continuation rows for inspection, but keep them archived or read-only because Windows Desktop may not resume them:
   ```bash
   python3 scripts/import_claude_sessions_to_codex.py --create-continuation-threads --dry-run
   python3 scripts/import_claude_sessions_to_codex.py --create-continuation-threads
   ```
8. Verify parity:
   ```bash
   codex doctor --json
   ```
   Check that rollout database/file parity is ok and no rollout files are missing.
9. If Codex Desktop still crashes, shows only a custom model selector, or old threads repeatedly disconnect after the import, run the Windows Desktop recovery checklist below before changing migrated session data.

## Windows Notes

- Run from PowerShell with Python 3 installed:
  ```powershell
  python .\scripts\import_claude_sessions_to_codex.py --dry-run
  python .\scripts\import_claude_sessions_to_codex.py --fix-existing-timestamps
  python .\scripts\import_claude_sessions_to_codex.py --import-missing
  python .\scripts\import_claude_sessions_to_codex.py --archive-and-compact-imports
  ```
- If Claude or Codex is installed in a nonstandard location, pass explicit paths:
  ```powershell
  python .\scripts\import_claude_sessions_to_codex.py --claude-projects "$env:USERPROFILE\.claude\projects" --codex-home "$env:USERPROFILE\.codex" --dry-run
  ```
- Close Codex Desktop before write operations on Windows. SQLite locks are more likely if the app is open.
- If previous Windows imports failed after opening old conversations, suspect long-rollout context loading. Use `--archive-and-compact-imports` so imported Claude sessions are clickable archive entries instead of full active threads.
- If imported archives are readable but cannot accept new messages, run `scripts/create_native_continuation_threads.py`. Continue work in the generated `续写: ...` Codex-native threads; keep the original archive rows for history.

## Windows Desktop Recovery Checklist

Use this checklist when Codex Desktop fails after import, cross-session resume fails, or the model picker unexpectedly shows only "custom" choices:

1. Read current Desktop logs first. On Windows they are normally under:
   ```powershell
   Get-ChildItem "$env:LOCALAPPDATA\Codex\Logs" -Recurse -File |
     Sort-Object LastWriteTime -Descending |
     Select-Object -First 10 FullName,LastWriteTime,Length
   ```
   Look for `unknown feature key`, `app-server is not available`, `stream disconnected`, websocket errors, or startup process exits.
2. Back up `~/.codex/config.toml`, then remove or comment unsupported feature keys that the installed app-server rejects. If errors mention `thread_tools`, `agent_todos`, `remote_plugin`, `enable_mcp_apps`, or app/MCP features, isolate those first instead of rewriting session files.
3. Temporarily disable custom MCP servers and app/plugin experiments in `config.toml`. Keep built-in runtime MCP entries only when the current Desktop build requires them.
4. Do not remove a top-level `model = ...` or `model_reasoning_effort = ...` merely because it exists: current Desktop builds write these keys for normal model selection. Remove them only when evidence shows they were test overrides or the user explicitly wants the recommended default. Re-test with:
   ```powershell
   codex doctor --summary
   ```
5. If the machine uses a local app-server wrapper/filter, inspect it for model-related rewriting. It should not synthesize `model/list`, and it should not inject `model` or `modelProvider` into `thread/start`, `thread/resume`, or `turn/start` unless the user explicitly wants a forced model. A wrapper that fakes `model/list` can make Desktop show "custom" even when `config.toml` is clean.
6. After a Store update, read `AppxManifest.xml` instead of hardcoding `app\Codex.exe`; newer Windows packages may launch `app\ChatGPT.exe`. A proxy launcher should resolve the manifest executable and dynamically locate the current CLI rather than retain a stale copied binary.
7. Relaunch Desktop from the known-good launcher or shortcut that preserves the required proxy/runtime environment. Verify the running app-server executable and command line; launching the plain Store entry may bypass a wrapper/filter even when the shortcut is correct.
8. When rolling back a bad custom MCP edit, compare against a timestamped pre-change backup. Remove the custom server without deleting the built-in `node_repl`; verify its command and Node paths still resolve because Desktop updates rotate runtime directories.
9. Run `codex doctor --summary`. Require config/auth/MCP/install health and inspect WebSocket/reachability separately. Remaining rollout/state parity warnings concern migrated thread inventory and should not be "fixed" by deleting source rollouts.
10. Only after Desktop config, app-server, websocket, and model-list health are verified should you modify imported sessions or create continuation threads.

## Safety Rules

- Always preserve a backup before writing to `~/.codex`.
- Do not delete Codex rows or rollout files during migration.
- Do not commit or copy `auth.json`, logs, caches, old config backups, or files that may contain API keys when publishing this skill or sharing recovery artifacts.
- Exclude `*/subagents/*` and `journal.jsonl` unless the user explicitly asks to include them.
- If `state_5.sqlite` schema differs, inspect `PRAGMA table_info(threads)` before writing and adapt conservatively.
- If `gh` or network commands are needed to publish a skill, verify authentication before assuming GitHub access works.
- For long imported sessions, prefer starting a fresh Codex thread and reading the Claude source JSONL selectively instead of continuing the imported rollout directly.
- Keep the Codex thread entry compact, but make the sidecar Markdown summary rich: preserve objectives, module/phase breakdowns, decisions, paths, tool usage, errors/fixes, first requests, recent requests, recent assistant outputs, and a user-prompt timeline where each user prompt is paired with the following assistant result.

## Script

Use `scripts/import_claude_sessions_to_codex.py` for repeatable migrations. It supports configurable home directories, dry runs, timestamp repair, missing-session import, and optional subagent inclusion.
