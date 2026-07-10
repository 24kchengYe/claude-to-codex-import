#!/usr/bin/env python3
"""Create Codex-native continuation threads for archived Claude summaries.

This uses Codex app-server's own thread/start + thread/inject_items flow, then
patches display timestamps back to the original Claude session time.
"""

import argparse
import datetime as dt
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path


DEFAULT_CLI_EXE = (
    Path.home()
    / "AppData/Roaming/npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64"
    / "vendor/x86_64-pc-windows-msvc/bin/codex.exe"
)


def parse_iso(value):
    if not value:
        return None
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def unix_ms(ts):
    return int(ts.timestamp() * 1000)


def strip_extended_prefix(text):
    text = str(text or "")
    return text[4:] if text.startswith("\\\\?\\") else text


def codex_db_cwd(text):
    text = strip_extended_prefix(text)
    if len(text) >= 3 and text[1:3] == ":\\":
        return "\\\\?\\" + text
    return text


class AppServerClient:
    def __init__(self, codex_exe, proxy):
        env = os.environ.copy()
        if proxy:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                env[key] = proxy
            env["NO_PROXY"] = "localhost,127.0.0.1,::1,*.local"
            env["no_proxy"] = env["NO_PROXY"]
        self.proc = subprocess.Popen(
            [str(codex_exe), "app-server", "--analytics-default-enabled"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        self.queue = queue.Queue()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self.request(
            "initialize",
            {"clientInfo": {"name": "claude-to-codex-import", "version": "1.0"}, "capabilities": {"experimentalApi": True}},
            request_id="__codex_initialize__",
            timeout=20,
        )

    def _read_stdout(self):
        for line in self.proc.stdout:
            self.queue.put(("out", line.rstrip("\n")))

    def _read_stderr(self):
        for line in self.proc.stderr:
            self.queue.put(("err", line.rstrip("\n")))

    def request(self, method, params, request_id=None, timeout=30):
        request_id = request_id or str(uuid.uuid4())
        payload = {"id": request_id, "method": method, "params": params}
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                kind, line = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if kind == "err":
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("id") == request_id:
                if "error" in obj:
                    raise RuntimeError(f"{method} failed: {obj['error']}")
                return obj.get("result")
        raise TimeoutError(method)

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass
        time.sleep(1)
        if self.proc.poll() is None:
            self.proc.kill()


def backup_state(codex_home):
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = codex_home / "backups" / f"{stamp}_native_continuation_threads"
    backup_dir.mkdir(parents=True, exist_ok=False)
    state_db = codex_home / "state_5.sqlite"
    for path in (state_db, Path(str(state_db) + "-wal"), Path(str(state_db) + "-shm")):
        if path.exists():
            shutil.copy2(path, backup_dir / path.name)
    return backup_dir


def continuation_prompt(item, title, cwd):
    return "\n".join(
        [
            "继续一个从 Claude 迁移到 Codex 的会话。",
            "",
            f"旧归档 threadId: {item['thread_id']}",
            f"旧归档标题: {title}",
            f"工作目录: {strip_extended_prefix(cwd)}",
            f"结构化摘要路径: {item.get('summary_path', '')}",
            f"归档 Claude JSONL 副本: {item.get('original_copy', '')}",
            f"原始 Claude JSONL: {item.get('source_path', '')}",
            "",
            "请先读取结构化摘要路径，理解此前任务状态，再继续用户后续请求。",
            "不要把旧归档当作可直接 resume 的 Codex thread；旧归档只用于查看历史。",
        ]
    )


def patch_thread_time(con, thread_id, title, cwd, first, last):
    first_ms = unix_ms(first)
    last_ms = unix_ms(last)
    con.execute(
        """
        UPDATE threads
           SET title = ?,
               cwd = ?,
               created_at = ?,
               updated_at = ?,
               created_at_ms = ?,
               updated_at_ms = ?,
               recency_at = ?,
               recency_at_ms = ?
         WHERE id = ?
        """,
        (
            title[:200],
            codex_db_cwd(cwd),
            first_ms // 1000,
            last_ms // 1000,
            first_ms,
            last_ms,
            last_ms // 1000,
            last_ms,
            thread_id,
        ),
    )


def repair_rollout_events(codex_home, thread_id, prompt):
    """Make injected continuation rollouts look like Desktop-created user turns."""
    matches = list((codex_home / "sessions").glob(f"**/*{thread_id}.jsonl"))
    if not matches:
        return None
    rollout_path = matches[0]
    rows = [json.loads(line) for line in rollout_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    turn_id = "auto-compact-0"
    for row in rows:
        if row.get("type") == "turn_context":
            turn_id = row.get("payload", {}).get("turn_id") or turn_id
            break
    has_task = any(row.get("type") == "event_msg" and row.get("payload", {}).get("type") == "task_started" for row in rows)
    has_user_event = any(row.get("type") == "event_msg" and row.get("payload", {}).get("type") == "user_message" for row in rows)
    output = []
    for index, row in enumerate(rows):
        output.append(row)
        if index == 0 and not has_task:
            output.append(
                {
                    "timestamp": row.get("timestamp") or dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "type": "event_msg",
                    "payload": {
                        "type": "task_started",
                        "turn_id": turn_id,
                        "started_at": int(time.time()),
                        "model_context_window": 258400,
                        "collaboration_mode_kind": "default",
                    },
                }
            )
        if (
            not has_user_event
            and row.get("type") == "response_item"
            and row.get("payload", {}).get("type") == "message"
            and row.get("payload", {}).get("role") == "user"
        ):
            text = "".join(part.get("text", "") for part in row.get("payload", {}).get("content", []) if isinstance(part, dict))
            if "继续一个从 Claude 迁移到 Codex 的会话" in text:
                output.append(
                    {
                        "timestamp": row.get("timestamp") or dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "client_id": "claude-to-codex-import",
                            "message": prompt,
                            "images": [],
                            "local_images": [],
                            "text_elements": [],
                        },
                    }
                )
                has_user_event = True
    rollout_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in output) + "\n",
        encoding="utf-8",
    )
    return rollout_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument("--archive-index", default=str(Path.home() / ".codex/imported_claude_archive/index.json"))
    parser.add_argument("--codex-exe", default=str(DEFAULT_CLI_EXE))
    parser.add_argument("--proxy", help="Override HTTP/HTTPS/ALL_PROXY for the app-server process.")
    parser.add_argument("--thread-id")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    codex_home = Path(args.codex_home).expanduser()
    state_db = codex_home / "state_5.sqlite"
    index_path = Path(args.archive_index).expanduser()
    index_rows = json.loads(index_path.read_text(encoding="utf-8"))
    con = sqlite3.connect(state_db)
    created = 0
    skipped = 0
    planned = 0
    backup_dir = None

    try:
        if not args.dry_run and not args.no_backup:
            backup_dir = backup_state(codex_home)
            print(f"backup_dir={backup_dir}")
        for item in index_rows:
            old_id = item.get("thread_id")
            if args.thread_id and old_id != args.thread_id:
                continue
            summary_path = Path(str(item.get("summary_path") or ""))
            if not old_id or not summary_path.exists():
                skipped += 1
                continue
            if item.get("native_continuation_thread_id") and not args.overwrite:
                skipped += 1
                continue
            old_row = con.execute("SELECT cwd, title FROM threads WHERE id = ?", (old_id,)).fetchone()
            cwd = old_row[0] if old_row and old_row[0] else str(Path.home())
            old_title = old_row[1] if old_row and old_row[1] else item.get("purpose") or item.get("title") or old_id
            title = f"续写: {old_title}"[:200]
            first = parse_iso(item.get("first")) or dt.datetime.now(dt.timezone.utc)
            last = parse_iso(item.get("last")) or first
            planned += 1
            if args.dry_run:
                print(f"would_create_native_continuation old={old_id} title={title} cwd={cwd}")
            else:
                client = AppServerClient(Path(args.codex_exe), args.proxy)
                try:
                    start = client.request(
                        "thread/start",
                        {
                            "cwd": strip_extended_prefix(cwd),
                            "approvalPolicy": "never",
                            "sandbox": "danger-full-access",
                            "approvalsReviewer": "user",
                            "threadSource": "user",
                            "ephemeral": False,
                        },
                    )
                    new_id = start["thread"]["id"]
                    client.request("thread/name/set", {"threadId": new_id, "name": title}, timeout=120)
                    prompt = continuation_prompt(item, old_title, cwd)
                    client.request(
                        "thread/inject_items",
                        {
                            "threadId": new_id,
                            "items": [
                                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]},
                                {
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "output_text",
                                            "text": "已创建 Claude 迁移会话的 Codex 原生续写入口。后续请在这个会话里继续；旧归档只用于查看历史。",
                                        }
                                    ],
                                },
                            ],
                        },
                        timeout=120,
                    )
                    rollout_path = repair_rollout_events(codex_home, new_id, prompt)
                    patch_thread_time(con, new_id, title, cwd, first, last)
                    con.execute(
                        """
                        UPDATE threads
                           SET rollout_path = COALESCE(?, rollout_path),
                               first_user_message = ?,
                               preview = ?,
                               has_user_event = 1,
                               reasoning_effort = COALESCE(reasoning_effort, 'medium')
                         WHERE id = ?
                        """,
                        (
                            str(rollout_path) if rollout_path else None,
                            prompt,
                            f"Claude 迁移续写入口：{old_title}。摘要路径在首条消息中。"[:500],
                            new_id,
                        ),
                    )
                    item["native_continuation_thread_id"] = new_id
                    item["native_continuation_created_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                    con.commit()
                    index_path.write_text(json.dumps(index_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    created += 1
                    print(f"created_native_continuation {new_id} <- {old_id} {title}")
                    if args.limit and created >= args.limit:
                        break
                finally:
                    client.close()
        if not args.dry_run:
            con.commit()
            index_path.write_text(json.dumps(index_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    print(f"planned={planned} created={created} skipped={skipped}")


if __name__ == "__main__":
    main()
