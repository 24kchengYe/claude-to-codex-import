#!/usr/bin/env python3
"""Import local Claude Code JSONL sessions into Codex Desktop state on macOS."""

import argparse
import datetime as dt
import hashlib
import json
import shutil
import sqlite3
import uuid
from pathlib import Path


DEFAULT_CLI_VERSION = "0.142.5"


def expand(path_text):
    return Path(path_text).expanduser().resolve()


def parse_iso(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def iso_z(ts):
    return ts.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def unix_ms(ts):
    return int(ts.timestamp() * 1000)


def read_jsonl(path):
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def text_from_content(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            item_type = item.get("type")
            if item_type in {"text", "input_text", "output_text"}:
                parts.append(str(item.get("text", "")))
            elif item_type == "tool_result":
                parts.append(str(item.get("content", "")))
            elif item_type == "image":
                parts.append("[image]")
            else:
                parts.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
        return "\n".join(part for part in parts if part)
    return str(content)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def event(ts, event_type, payload):
    return {"timestamp": iso_z(ts), "type": event_type, "payload": payload}


def user_text_lines(ts, text):
    return [
        event(ts, "event_msg", {"type": "user_message", "message": text, "local_images": [], "text_elements": []}),
        event(ts, "response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}),
    ]


def agent_text_lines(ts, text):
    return [
        event(ts, "event_msg", {"type": "agent_message", "message": text, "phase": None, "memory_citation": None}),
        event(ts, "response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}),
    ]


def tool_call_text(item):
    name = item.get("name") or "tool"
    tool_input = item.get("input")
    lines = [f"[external_agent_tool_call: {name}]"]
    if isinstance(tool_input, dict):
        for key in ("description", "file", "command"):
            if key in tool_input:
                lines.append(f"{key}: {tool_input[key]}")
        rest = {key: value for key, value in tool_input.items() if key not in {"description", "file", "command"}}
        if rest:
            lines.append(json.dumps(rest, ensure_ascii=False, indent=2))
    elif tool_input is not None:
        lines.append(str(tool_input))
    lines.append("[/external_agent_tool_call]")
    return "\n".join(lines)


def tool_result_text(item):
    marker = "[external_agent_tool_result: error]" if item.get("is_error") else "[external_agent_tool_result]"
    return f"{marker}\n{text_from_content(item.get('content', ''))}\n[/external_agent_tool_result]"


def project_cwd(path, fallback_home):
    for obj in read_jsonl(path):
        cwd = obj.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
    return str(fallback_home)


def analyze_claude_session(path, fallback_home):
    title = ""
    first_user = ""
    preview = ""
    first = None
    last = None
    total = 0
    timestamped = 0
    for obj in read_jsonl(path):
        total += 1
        if obj.get("type") == "ai-title" and obj.get("aiTitle") and not title:
            title = str(obj["aiTitle"]).strip()
        ts = parse_iso(obj.get("timestamp"))
        if ts:
            timestamped += 1
            first = ts if first is None or ts < first else first
            last = ts if last is None or ts > last else last
        if obj.get("type") == "user" and isinstance(obj.get("message"), dict):
            text = text_from_content(obj["message"].get("content")).strip()
            if text and not first_user:
                first_user = text
            if text and not preview:
                preview = text
    if first is None:
        stat_time = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
        first = stat_time
    if last is None:
        last = first
    if not title:
        title = (first_user or path.stem).strip().splitlines()[0][:80]
    if not preview:
        preview = first_user or title
    return {
        "title": title[:200],
        "first_user": first_user[:4000],
        "preview": preview[:4000],
        "first": first,
        "last": last,
        "total": total,
        "timestamped": timestamped,
        "cwd": project_cwd(path, fallback_home),
    }


def convert_claude_to_rollout(path, thread_id, info, cli_version):
    rows = [
        event(
            info["first"],
            "session_meta",
            {
                "session_id": thread_id,
                "id": thread_id,
                "timestamp": iso_z(info["first"]),
                "cwd": info["cwd"],
                "originator": "Codex Desktop",
                "cli_version": cli_version,
                "source": "vscode",
                "model_provider": "openai",
                "external_agent_source": "claude",
                "external_agent_source_path": str(path),
            },
        )
    ]
    for obj in read_jsonl(path):
        ts = parse_iso(obj.get("timestamp")) or info["first"]
        obj_type = obj.get("type")
        if obj_type == "user" and isinstance(obj.get("message"), dict):
            content = obj["message"].get("content")
            if isinstance(content, list):
                normal_parts = []
                saw_tool_result = False
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        saw_tool_result = True
                        rows.extend(agent_text_lines(ts, tool_result_text(item)))
                    else:
                        normal_parts.append(item)
                text = text_from_content(normal_parts).strip()
                if text:
                    rows.extend(user_text_lines(ts, text))
                elif not saw_tool_result:
                    rows.extend(user_text_lines(ts, text_from_content(content).strip()))
            else:
                text = text_from_content(content).strip()
                if text:
                    rows.extend(user_text_lines(ts, text))
        elif obj_type == "assistant" and isinstance(obj.get("message"), dict):
            content = obj["message"].get("content")
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        rows.extend(agent_text_lines(ts, str(item)))
                    elif item.get("type") == "text":
                        text = str(item.get("text", "")).strip()
                        if text:
                            rows.extend(agent_text_lines(ts, text))
                    elif item.get("type") == "tool_use":
                        rows.extend(agent_text_lines(ts, tool_call_text(item)))
            else:
                text = text_from_content(content).strip()
                if text:
                    rows.extend(agent_text_lines(ts, text))
        elif obj_type == "summary" and obj.get("summary"):
            rows.extend(agent_text_lines(ts, str(obj["summary"]).strip()))
        elif obj_type == "queue-operation" and obj.get("content"):
            rows.extend(agent_text_lines(ts, str(obj["content"]).strip()))
    rows.extend(agent_text_lines(info["last"], "<EXTERNAL SESSION IMPORTED>"))
    return rows


def load_index(index_path):
    if not index_path.exists():
        return {"records": []}
    data = json.loads(index_path.read_text(encoding="utf-8"))
    data.setdefault("records", [])
    return data


def claude_session_paths(claude_projects, include_subagents):
    paths = sorted(claude_projects.rglob("*.jsonl"))
    if not include_subagents:
        paths = [path for path in paths if "/subagents/" not in str(path)]
    return [path for path in paths if path.name != "journal.jsonl"]


def backup_codex(codex_home, state_db, index_path):
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = codex_home / "backups" / f"{stamp}_claude_to_codex_import"
    backup_dir.mkdir(parents=True, exist_ok=False)
    for path in (state_db, Path(str(state_db) + "-wal"), Path(str(state_db) + "-shm"), index_path):
        if path.exists():
            shutil.copy2(path, backup_dir / path.name)
    return backup_dir


def require_threads_columns(con, columns):
    found = {row[1] for row in con.execute("PRAGMA table_info(threads)").fetchall()}
    missing = sorted(set(columns) - found)
    if missing:
        raise RuntimeError(f"state_5.sqlite threads table is missing columns: {', '.join(missing)}")


def fix_existing_timestamps(con, records, fallback_home):
    require_threads_columns(con, ["id", "created_at", "updated_at", "created_at_ms", "updated_at_ms", "recency_at", "recency_at_ms"])
    changed = 0
    for record in records:
        source_path = record.get("source_path")
        thread_id = record.get("imported_thread_id")
        if not source_path or not thread_id:
            continue
        path = Path(source_path).expanduser()
        if not path.exists():
            continue
        info = analyze_claude_session(path, fallback_home)
        first_ms = unix_ms(info["first"])
        last_ms = unix_ms(info["last"])
        cursor = con.execute(
            """
            UPDATE threads
               SET created_at = ?,
                   updated_at = ?,
                   created_at_ms = ?,
                   updated_at_ms = ?,
                   recency_at = ?,
                   recency_at_ms = ?
             WHERE id = ?
            """,
            (first_ms // 1000, last_ms // 1000, first_ms, last_ms, last_ms // 1000, last_ms, thread_id),
        )
        changed += max(cursor.rowcount, 0)
    return changed


def import_missing_sessions(con, index, missing, sessions_dir, fallback_home, cli_version):
    require_threads_columns(
        con,
        [
            "id",
            "rollout_path",
            "created_at",
            "updated_at",
            "source",
            "model_provider",
            "cwd",
            "title",
            "sandbox_policy",
            "approval_mode",
            "tokens_used",
            "has_user_event",
            "archived",
            "cli_version",
            "first_user_message",
            "memory_mode",
            "created_at_ms",
            "updated_at_ms",
            "preview",
            "recency_at",
            "recency_at_ms",
        ],
    )
    added = 0
    for path in missing:
        info = analyze_claude_session(path, fallback_home)
        thread_id = str(uuid.uuid4())
        day_dir = sessions_dir / info["first"].strftime("%Y/%m/%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        rollout_name = f"rollout-{info['first'].strftime('%Y-%m-%dT%H-%M-%S')}-{thread_id}.jsonl"
        rollout_path = day_dir / rollout_name
        rows = convert_claude_to_rollout(path, thread_id, info, cli_version)
        with rollout_path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        first_ms = unix_ms(info["first"])
        last_ms = unix_ms(info["last"])
        con.execute(
            """
            INSERT INTO threads (
                id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                sandbox_policy, approval_mode, tokens_used, has_user_event, archived, archived_at,
                git_sha, git_branch, git_origin_url, cli_version, first_user_message,
                agent_nickname, agent_role, memory_mode, model, reasoning_effort, agent_path,
                created_at_ms, updated_at_ms, thread_source, preview, recency_at, recency_at_ms
            ) VALUES (
                ?, ?, ?, ?, 'vscode', 'openai', ?, ?, '{"type":"read-only"}', 'on-request',
                0, 0, 0, NULL, NULL, NULL, NULL, ?, ?, NULL, NULL, 'enabled', NULL, NULL,
                NULL, ?, ?, NULL, ?, ?, ?
            )
            """,
            (
                thread_id,
                str(rollout_path),
                first_ms // 1000,
                last_ms // 1000,
                info["cwd"],
                info["title"],
                cli_version,
                info["first_user"],
                first_ms,
                last_ms,
                info["preview"],
                last_ms // 1000,
                last_ms,
            ),
        )
        index["records"].append(
            {
                "source_path": str(path),
                "content_sha256": sha256_file(path),
                "imported_thread_id": thread_id,
                "imported_at": int(dt.datetime.now(dt.timezone.utc).timestamp()),
                "source_modified_at": path.stat().st_mtime_ns,
            }
        )
        added += 1
    return added


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", default="~/.codex")
    parser.add_argument("--claude-projects", default="~/.claude/projects")
    parser.add_argument("--cli-version", default=DEFAULT_CLI_VERSION)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fix-existing-timestamps", action="store_true")
    parser.add_argument("--import-missing", action="store_true")
    parser.add_argument("--include-subagents", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    codex_home = expand(args.codex_home)
    claude_projects = expand(args.claude_projects)
    state_db = codex_home / "state_5.sqlite"
    index_path = codex_home / "external_agent_session_imports.json"
    sessions_dir = codex_home / "sessions"
    fallback_home = Path.home()

    if not claude_projects.exists():
        raise SystemExit(f"Claude projects directory not found: {claude_projects}")
    if not state_db.exists():
        raise SystemExit(f"Codex state database not found: {state_db}")

    index = load_index(index_path)
    records = index["records"]
    imported_paths = {record.get("source_path") for record in records if record.get("source_path")}
    all_paths = claude_session_paths(claude_projects, args.include_subagents)
    missing = [path for path in all_paths if str(path) not in imported_paths]

    print(f"candidate_sessions={len(all_paths)} already_indexed={len(imported_paths)} missing={len(missing)}")
    if args.dry_run:
        for path in missing[:20]:
            info = analyze_claude_session(path, fallback_home)
            print(f"{iso_z(info['first'])} {iso_z(info['last'])} {info['title']} {path}")
        return

    if not args.fix_existing_timestamps and not args.import_missing:
        raise SystemExit("Choose --dry-run, --fix-existing-timestamps, or --import-missing.")

    backup_dir = None
    if not args.no_backup:
        backup_dir = backup_codex(codex_home, state_db, index_path)
        print(f"backup_dir={backup_dir}")

    con = sqlite3.connect(state_db)
    try:
        con.execute("BEGIN IMMEDIATE")
        fixed = 0
        imported = 0
        if args.fix_existing_timestamps:
            fixed = fix_existing_timestamps(con, records, fallback_home)
        if args.import_missing:
            imported = import_missing_sessions(con, index, missing, sessions_dir, fallback_home, args.cli_version)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    if args.import_missing:
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"fixed_existing_thread_updates={fixed}")
    print(f"imported_missing_sessions={imported}")


if __name__ == "__main__":
    main()
