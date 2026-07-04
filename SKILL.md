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
6. Verify parity:
   ```bash
   codex doctor --json
   ```
   Check that rollout database/file parity is ok and no rollout files are missing.

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

## Safety Rules

- Always preserve a backup before writing to `~/.codex`.
- Do not delete Codex rows or rollout files during migration.
- Exclude `*/subagents/*` and `journal.jsonl` unless the user explicitly asks to include them.
- If `state_5.sqlite` schema differs, inspect `PRAGMA table_info(threads)` before writing and adapt conservatively.
- If `gh` or network commands are needed to publish a skill, verify authentication before assuming GitHub access works.
- For long imported sessions, prefer starting a fresh Codex thread and reading the Claude source JSONL selectively instead of continuing the imported rollout directly.
- Keep the Codex thread entry compact, but make the sidecar Markdown summary rich: preserve objectives, module/phase breakdowns, decisions, paths, tool usage, errors/fixes, first requests, recent requests, recent assistant outputs, and a user-prompt timeline where each user prompt is paired with the following assistant result.

## Script

Use `scripts/import_claude_sessions_to_codex.py` for repeatable migrations. It supports configurable home directories, dry runs, timestamp repair, missing-session import, and optional subagent inclusion.
