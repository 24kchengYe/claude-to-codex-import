#!/usr/bin/env python3
"""Move post-import dialogue from read-only Claude archive threads into native continuations."""

import argparse
import datetime as dt
import importlib.util
import json
import shutil
import sqlite3
from pathlib import Path


HERE = Path(__file__).resolve().parent
HELPER_PATH = HERE / "create_native_continuation_threads.py"
spec = importlib.util.spec_from_file_location("native_threads", HELPER_PATH)
native_threads = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native_threads)


def parse_iso(value):
    if not value:
        return None
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def unix_ms(ts):
    return int(ts.timestamp() * 1000)


def text_from_message(payload):
    return "".join(
        part.get("text", "")
        for part in payload.get("content", [])
        if isinstance(part, dict)
    )


def iter_post_messages(item):
    rollout_path = Path(item["rollout_path"])
    cutoff = parse_iso(item.get("last"))
    if not rollout_path.exists():
        return []

    messages = []
    for line in rollout_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        timestamp = parse_iso(row.get("timestamp")) if row.get("timestamp") else None
        if cutoff and timestamp and timestamp <= cutoff:
            continue
        if row.get("type") != "response_item":
            continue
        payload = row.get("payload") or {}
        if payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in ("user", "assistant"):
            continue
        text = text_from_message(payload).strip()
        if not text:
            continue
        messages.append({"timestamp": row.get("timestamp"), "role": role, "text": text})
    return messages


def clip(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


def transcript_markdown(item, messages):
    lines = [
        "# Post-import dialogue from old Claude archive",
        "",
        f"Old archive threadId: {item['thread_id']}",
        f"Native continuation threadId: {item.get('native_continuation_thread_id', '')}",
        f"Old archive title: {item.get('title') or item.get('purpose') or ''}",
        f"Original Claude last timestamp: {item.get('last', '')}",
        f"Post-import message count: {len(messages)}",
        f"Post-import first timestamp: {messages[0]['timestamp'] if messages else ''}",
        f"Post-import last timestamp: {messages[-1]['timestamp'] if messages else ''}",
        "",
        "This file preserves messages that were sent in the old read-only archive after the Claude import.",
        "Use it together with the structured archive summary when continuing the native Codex thread.",
        "",
        "## Messages",
        "",
    ]
    for index, msg in enumerate(messages, 1):
        lines.extend(
            [
                f"### {index}. {msg['timestamp']} {msg['role']}",
                "",
                msg["text"],
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def injection_prompt(item, transcript_path, messages):
    char_count = sum(len(message["text"]) for message in messages)
    recent = messages[-8:]
    lines = [
        "补入旧 Claude 归档线程中已经发生过的后续对话。",
        "",
        f"旧归档 threadId: {item['thread_id']}",
        f"旧归档标题: {item.get('title') or item.get('purpose') or ''}",
        f"补充转录路径: {transcript_path}",
        f"补充消息数: {len(messages)}",
        f"补充字符数: {char_count}",
        f"补充时间范围: {messages[0]['timestamp']} -> {messages[-1]['timestamp']}",
        "",
        "后续继续任务时，请把这个补充转录视为该续写线程的一部分历史；需要细节时先读取补充转录路径。",
        "",
        "最近几条补充消息摘录：",
    ]
    for msg in recent:
        lines.extend(
            [
                "",
                f"[{msg['timestamp']}] {msg['role']}:",
                clip(msg["text"], 1200),
            ]
        )
    return "\n".join(lines)


def resume_native_thread(client, con, thread_id):
    row = con.execute("SELECT rollout_path, cwd FROM threads WHERE id = ?", (thread_id,)).fetchone()
    if not row:
        raise RuntimeError(f"native continuation thread not found in state DB: {thread_id}")
    rollout_path, cwd = row
    client.request(
        "thread/resume",
        {
            "threadId": thread_id,
            "path": rollout_path,
            "cwd": native_threads.strip_extended_prefix(cwd),
            "history": None,
            "approvalPolicy": "never",
            "permissions": ":danger-full-access",
            "runtimeWorkspaceRoots": [native_threads.strip_extended_prefix(cwd)],
            "approvalsReviewer": "user",
            "excludeTurns": True,
            "initialTurnsPage": {"limit": 5, "itemsView": "full", "sortDirection": "desc"},
        },
        timeout=180,
    )


def backup_files(codex_home, state_db, index_path, impacted):
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = codex_home / "backups" / f"{stamp}_post_import_dialogues"
    backup_dir.mkdir(parents=True, exist_ok=False)
    for path in (state_db, Path(str(state_db) + "-wal"), Path(str(state_db) + "-shm"), index_path):
        if path.exists():
            shutil.copy2(path, backup_dir / path.name)
    rollout_dir = backup_dir / "rollouts"
    rollout_dir.mkdir()
    for item, _messages in impacted:
        old_path = Path(item["rollout_path"])
        if old_path.exists():
            shutil.copy2(old_path, rollout_dir / old_path.name)
        native_id = item.get("native_continuation_thread_id")
        if native_id:
            for native_path in (codex_home / "sessions").glob(f"**/*{native_id}.jsonl"):
                shutil.copy2(native_path, rollout_dir / native_path.name)
    return backup_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument("--archive-index", default=str(Path.home() / ".codex/imported_claude_archive/index.json"))
    parser.add_argument("--codex-exe", default=str(native_threads.DEFAULT_CLI_EXE))
    parser.add_argument("--proxy", help="Override HTTP/HTTPS/ALL_PROXY for the app-server process.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    codex_home = Path(args.codex_home).expanduser()
    index_path = Path(args.archive_index).expanduser()
    state_db = codex_home / "state_5.sqlite"
    transcript_root = codex_home / "imported_claude_archive" / "post_import_dialogues"
    index_rows = json.loads(index_path.read_text(encoding="utf-8"))

    impacted = []
    for item in index_rows:
        if item.get("post_import_dialogue_migrated_at"):
            continue
        if not item.get("native_continuation_thread_id"):
            continue
        messages = iter_post_messages(item)
        if messages:
            impacted.append((item, messages))

    print(f"impacted={len(impacted)}")
    for item, messages in impacted:
        print(
            "would_migrate",
            item["thread_id"],
            "->",
            item.get("native_continuation_thread_id"),
            "messages=",
            len(messages),
            "chars=",
            sum(len(message["text"]) for message in messages),
            "last=",
            messages[-1]["timestamp"],
        )
    if args.dry_run or not impacted:
        return

    backup_dir = backup_files(codex_home, state_db, index_path, impacted)
    print(f"backup_dir={backup_dir}")
    transcript_root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(state_db)
    try:
        for item, messages in impacted:
            old_id = item["thread_id"]
            native_id = item["native_continuation_thread_id"]
            day = (messages[0]["timestamp"] or item.get("last") or "unknown")[:10].replace("-", "\\")
            transcript_dir = transcript_root / day
            transcript_dir.mkdir(parents=True, exist_ok=True)
            transcript_path = transcript_dir / f"{old_id}-post-import-dialogue.md"
            transcript_path.write_text(transcript_markdown(item, messages), encoding="utf-8")
            prompt = injection_prompt(item, transcript_path, messages)
            client = native_threads.AppServerClient(Path(args.codex_exe), args.proxy)
            try:
                resume_native_thread(client, con, native_id)
                client.request(
                    "thread/inject_items",
                    {
                        "threadId": native_id,
                        "items": [
                            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]},
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": "已补入旧归档线程中的后续对话索引。后续继续时会把补充转录作为可读取历史。",
                                    }
                                ],
                            },
                        ],
                    },
                    timeout=180,
                )
            finally:
                client.close()
            last_ts = parse_iso(messages[-1]["timestamp"]) or dt.datetime.now(dt.timezone.utc)
            last_ms = unix_ms(last_ts)
            con.execute(
                """
                UPDATE threads
                   SET updated_at = ?,
                       updated_at_ms = ?,
                       recency_at = ?,
                       recency_at_ms = ?,
                       preview = ?
                 WHERE id = ?
                """,
                (
                    last_ms // 1000,
                    last_ms,
                    last_ms // 1000,
                    last_ms,
                    f"已补入旧归档后续对话：{len(messages)} 条。转录路径在会话中。"[:500],
                    native_id,
                ),
            )
            item["post_import_dialogue_path"] = str(transcript_path)
            item["post_import_dialogue_message_count"] = len(messages)
            item["post_import_dialogue_last"] = messages[-1]["timestamp"]
            item["post_import_dialogue_migrated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            con.commit()
            index_path.write_text(json.dumps(index_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"migrated_post_dialogue {old_id} -> {native_id} messages={len(messages)}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
