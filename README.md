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

## Archive Design

Each imported Claude session has three layers:

- Compact Codex thread entry: short enough to open without exhausting context.
- Rich Markdown sidecar summary: objectives, module/phase breakdowns, decisions, paths, tools, errors/fixes, first requests, recent requests, and recent assistant outputs.
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
