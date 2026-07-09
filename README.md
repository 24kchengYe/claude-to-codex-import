# Claude to Codex Import

Import local Claude Code conversations into Codex Desktop while preserving accurate timestamps and avoiding context-window failures from very long Claude sessions.

## What This Does

- Finds Claude JSONL sessions under `~/.claude/projects`.
- Adds missing sessions to Codex's `~/.codex/state_5.sqlite` and `~/.codex/sessions`.
- Repairs imported thread timestamps from Claude line-level `timestamp` fields.
- Converts long imported Claude rollouts into compact Codex archive entries so they remain clickable but do not load the full old conversation into context.
- Stores full original JSONL copies and rich Markdown summaries under `~/.codex/imported_claude_archive`.

## Recommended Flow

```bash
python3 scripts/import_claude_sessions_to_codex.py --dry-run
python3 scripts/import_claude_sessions_to_codex.py --fix-existing-timestamps
python3 scripts/import_claude_sessions_to_codex.py --import-missing
python3 scripts/import_claude_sessions_to_codex.py --archive-and-compact-imports --dry-run
python3 scripts/import_claude_sessions_to_codex.py --archive-and-compact-imports
codex doctor --json
```

On Windows PowerShell:

```powershell
python .\scripts\import_claude_sessions_to_codex.py --dry-run
python .\scripts\import_claude_sessions_to_codex.py --fix-existing-timestamps
python .\scripts\import_claude_sessions_to_codex.py --import-missing
python .\scripts\import_claude_sessions_to_codex.py --archive-and-compact-imports
```

Close Codex Desktop before write operations, especially on Windows, to avoid SQLite locks.

## Windows Desktop Recovery

If Codex Desktop breaks after import, do not assume the migrated sessions are corrupt. First check the Desktop/app-server layer:

- Inspect `%LOCALAPPDATA%\Codex\Logs` for `unknown feature key`, websocket disconnects, or app-server exits.
- Back up `~/.codex/config.toml`, then disable unsupported feature keys and custom MCP/app/plugin experiments.
- Remove test-only top-level `model` and `model_reasoning_effort` entries to return to Codex defaults.
- If the machine uses a local app-server wrapper/filter, make sure it forwards `model/list` to the real app-server and does not inject `model` or `modelProvider` into normal thread requests.
- Relaunch through the known-good Desktop launcher/proxy shortcut, then run `codex doctor --summary`.

Only edit migrated session data after Desktop config, app-server, websocket, and model-list health are verified.

## Archive Design

Each imported Claude session has three layers:

- Compact Codex thread entry: short enough to open without exhausting context.
- Rich Markdown sidecar summary: objectives, module/phase breakdowns, decisions, paths, tools, errors/fixes, first requests, recent requests, recent assistant outputs, and a turn-by-turn user prompt timeline.
- Original JSONL copy: complete raw Claude session for exact recovery.

The archive lives at:

```text
~/.codex/imported_claude_archive/
  index.json
  originals/YYYY/MM/DD/*.jsonl
  summaries/YYYY/MM/DD/*.md
  rollout_backups/YYYY/MM/DD/*.jsonl
```

Manually edited Codex thread titles are preserved by default. Use `--overwrite-manual-titles` only when regenerated titles should replace UI edits.

## Summary Detail

The Markdown summary is intentionally much longer than the compact Codex thread. It includes a `用户 Prompt 时间线` section:

- each user prompt becomes a numbered turn,
- the assistant text that follows that prompt is attached as the completion/result summary,
- tool-heavy or interrupted turns are still retained with a placeholder when no assistant prose is available.

This gives later Codex sessions enough context to understand what happened without loading the entire raw Claude JSONL into the active model context.
