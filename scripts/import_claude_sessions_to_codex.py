#!/usr/bin/env python3
"""Import local Claude Code JSONL sessions into Codex Desktop state on macOS or Windows."""

import argparse
import datetime as dt
import hashlib
import json
import shutil
import sqlite3
import re
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


def clean_text(value, limit=None):
    text = str(value or "")
    text = re.sub(
        r"(['\"]data['\"]\s*:\s*['\"])[A-Za-z0-9+/=\r\n]{200,}(['\"])",
        r"\1[base64 image data omitted]\2",
        text,
    )
    text = re.sub(
        r"(data:image/[A-Za-z0-9.+-]+;base64,)[A-Za-z0-9+/=\r\n]{200,}",
        r"\1[base64 image data omitted]",
        text,
    )
    text = re.sub(
        r"(/9j/|iVBOR)[A-Za-z0-9+/=\r\n]{200,}",
        r"\1[base64 image data omitted]",
        text,
    )
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        return text[: limit - 1].rstrip() + "..."
    return text


def unique_keep_order(items, limit):
    seen = set()
    out = []
    for item in items:
        key = str(item)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


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


def summarize_claude_session(path, fallback_home):
    info = analyze_claude_session(path, fallback_home)
    user_messages = []
    assistant_messages = []
    turns = []
    pending_user = None
    assistant_after_user = []
    all_texts = []
    summaries = []
    tool_names = {}
    for obj in read_jsonl(path):
        obj_type = obj.get("type")
        if obj_type == "summary" and obj.get("summary"):
            summary_text = clean_text(obj.get("summary"), 12000)
            summaries.append(summary_text)
            all_texts.append(summary_text)
        if obj_type == "user" and isinstance(obj.get("message"), dict):
            text = clean_text(text_from_content(obj["message"].get("content")), 6000)
            if text:
                if pending_user is not None:
                    turns.append({"user": pending_user, "assistant": " ".join(assistant_after_user)})
                pending_user = text
                assistant_after_user = []
                user_messages.append(text)
                all_texts.append(text)
        elif obj_type == "assistant" and isinstance(obj.get("message"), dict):
            content = obj["message"].get("content")
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        name = str(item.get("name") or "tool")
                        tool_names[name] = tool_names.get(name, 0) + 1
                    elif isinstance(item, dict) and item.get("type") == "text":
                        parts.append(str(item.get("text", "")))
                text = clean_text("\n".join(parts), 6000)
            else:
                text = clean_text(text_from_content(content), 6000)
            if text:
                assistant_messages.append(text)
                if pending_user is not None:
                    assistant_after_user.append(text)
                all_texts.append(text)
    if pending_user is not None:
        turns.append({"user": pending_user, "assistant": " ".join(assistant_after_user)})

    purpose = clean_text(info["title"], 28)
    if not purpose or purpose == path.stem:
        purpose = clean_text(user_messages[0] if user_messages else path.stem, 28)
    if purpose.startswith("<local-command-caveat>"):
        purpose = clean_text(user_messages[0].replace("<local-command-caveat>", "") if user_messages else "本地命令会话", 28)

    return {
        **info,
        "purpose": purpose,
        "detail": build_detail_summary(info["title"], summaries, user_messages),
        "modules": extract_modules(summaries + user_messages),
        "paths": extract_paths(all_texts),
        "turns": compact_turns(turns),
        "user_count": len(user_messages),
        "assistant_count": len(assistant_messages),
        "summary_blocks": summaries[:8],
        "first_user_messages": user_messages[:8],
        "recent_user_messages": user_messages[-12:],
        "recent_assistant_messages": assistant_messages[-8:],
        "tool_names": sorted(tool_names.items(), key=lambda item: (-item[1], item[0]))[:12],
    }


def build_detail_summary(title, summaries, user_messages):
    for text in summaries:
        if text:
            return clean_text(text, 5000)
    for text in user_messages:
        marker = "Primary Request and Intent:"
        if marker in text:
            tail = text.split(marker, 1)[1]
            next_marker = "2. Key Technical Concepts:"
            if next_marker in tail:
                tail = tail.split(next_marker, 1)[0]
            return clean_text(tail, 5000)
    if user_messages:
        return clean_text(user_messages[0], 3000)
    return clean_text(title, 3000)


def extract_modules(texts):
    modules = []
    section_re = re.compile(r"(?:^|\s)(\d{1,2})\.\s+([A-Z][A-Za-z0-9 /&()_-]{2,80}:)(.*?)(?=(?:\s\d{1,2}\.\s+[A-Z][A-Za-z0-9 /&()_-]{2,80}:)|$)", re.S)
    for text in texts:
        for match in section_re.finditer(text):
            title = clean_text(match.group(2).rstrip(":"), 100)
            body = clean_text(match.group(3), 2500)
            if title and body:
                modules.append({"title": title, "body": body})
    return unique_keep_order([json.dumps(item, ensure_ascii=False) for item in modules], 16)


def extract_paths(texts):
    path_re = re.compile(r"(?:(?:/[A-Za-z0-9._@%+=:,~ -]+)+|(?:[A-Za-z]:[\\/][^\s`'\"<>|]+))")
    paths = []
    for text in texts:
        for match in path_re.finditer(text):
            value = match.group(0).strip(".,);]")
            if len(value) >= 8:
                paths.append(value)
    return unique_keep_order(paths, 100)


def compact_turns(turns):
    out = []
    for index, turn in enumerate(turns, start=1):
        user = clean_text(turn.get("user"), 2200)
        assistant = clean_text(turn.get("assistant"), 1800)
        if not user:
            continue
        out.append(
            {
                "index": index,
                "user": user,
                "assistant": assistant or "这一轮没有可提取的助手文本回复，可能主要是工具结果、后台任务或会话中断。",
            }
        )
    return out


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


def safe_slug(text, limit=64):
    text = clean_text(text, limit)
    text = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", text, flags=re.UNICODE).strip("-._")
    return text[:limit] or "claude-session"


def md_path(path):
    return Path(path).expanduser().as_posix()


def context_reference_paths(source_path, summary, fallback_home):
    refs = []

    def add(label, path):
        if path is None:
            return
        p = Path(path).expanduser()
        key = (label, str(p))
        if key in {(item["label"], item["path"]) for item in refs}:
            return
        refs.append({"label": label, "path": str(p), "exists": p.exists()})

    source = Path(source_path).expanduser()
    add("Claude 原始会话所在目录", source.parent)
    add("Claude 项目 sessions-index.json", source.parent / "sessions-index.json")

    cwd_text = summary.get("cwd")
    if cwd_text:
        cwd = Path(cwd_text).expanduser()
        add("会话 CWD", cwd)
        current = cwd
        for _ in range(8):
            add("项目 CLAUDE.md", current / "CLAUDE.md")
            add("项目 AGENTS.md", current / "AGENTS.md")
            parent = current.parent
            if parent == current:
                break
            current = parent

    home = Path(fallback_home).expanduser()
    claude_home = home / ".claude"
    add("Claude 用户目录", claude_home)
    add("Claude 用户 memory: CLAUDE.md", claude_home / "CLAUDE.md")
    add("Claude settings.json", claude_home / "settings.json")
    add("Claude settings.local.json", claude_home / "settings.local.json")
    add("Claude skills 目录", claude_home / "skills")
    add("Claude commands 目录", claude_home / "commands")
    add("Claude projects 目录", claude_home / "projects")
    return refs


def context_reference_markdown(refs):
    if not refs:
        return []
    lines = ["## 可追溯环境入口", ""]
    lines.append("这些是 Claude 当时可能依赖的本地上下文入口；这里只保留路径，Codex 需要时再定向读取。")
    lines.append("")
    for item in refs:
        status = "存在" if item["exists"] else "未发现"
        lines.append(f"- {item['label']} ({status}): `{md_path(item['path'])}`")
    lines.append("")
    return lines


def markdown_summary(thread_id, source_path, original_copy, summary_path, thread_row, summary):
    tools = ", ".join(f"{name}({count})" for name, count in summary["tool_names"]) or "无明显工具记录"
    modules = [json.loads(item) for item in summary["modules"]]
    context_refs = context_reference_paths(source_path, summary, Path.home())
    lines = [
        f"# {summary['purpose']}",
        "",
        f"- Thread ID: `{thread_id}`",
        f"- 原 Claude JSONL: `{md_path(source_path)}`",
        f"- 归档副本: `{md_path(original_copy)}`",
        f"- Codex rollout: `{md_path(thread_row['rollout_path'])}`",
        f"- 时间: `{iso_z(summary['first'])}` 到 `{iso_z(summary['last'])}`",
        f"- CWD: `{summary['cwd']}`",
        f"- 消息统计: user {summary['user_count']} / assistant {summary['assistant_count']} / JSONL rows {summary['total']}",
        f"- 常见工具: {tools}",
        "",
        "## 这个会话主要在做什么",
        "",
        clean_text(summary["detail"], 1800) or summary["purpose"],
        "",
    ]
    if modules:
        lines += ["## 模块/阶段拆解", ""]
        for item in modules:
            lines += [f"### {item['title']}", "", item["body"], ""]
    if summary["summary_blocks"]:
        lines += ["## Claude 原始摘要片段", ""]
        for item in summary["summary_blocks"]:
            lines += [f"- {item}", ""]
    if summary["first_user_messages"]:
        lines += ["## 开始时的用户请求", ""]
        for item in summary["first_user_messages"]:
            lines += [f"- {item}", ""]
    if summary["recent_user_messages"]:
        lines += ["## 最近的用户请求", ""]
        for item in summary["recent_user_messages"]:
            lines += [f"- {item}", ""]
    if summary["recent_assistant_messages"]:
        lines += ["## 最近的助手回复", ""]
        for item in summary["recent_assistant_messages"]:
            lines += [f"- {item}", ""]
    if summary["turns"]:
        lines += ["## 用户 Prompt 时间线", ""]
        for item in summary["turns"]:
            lines += [
                f"### Turn {item['index']}",
                "",
                "**用户 prompt**",
                "",
                item["user"],
                "",
                "**助手完成/回复摘要**",
                "",
                item["assistant"],
                "",
            ]
    if summary["paths"]:
        lines += ["## 会话中提到的路径", ""]
        for item in summary["paths"]:
            lines += [f"- `{item}`"]
        lines += [""]
    lines += context_reference_markdown(context_refs)
    lines += [
        "## 使用建议",
        "",
        "这个 Codex 会话是 Claude 导入归档入口。需要完整上下文时，按上面的原始 JSONL 或归档副本路径定向读取，不要把整段历史一次性塞进新模型上下文。",
        "",
    ]
    return "\n".join(lines)


def compact_rollout_rows(thread_id, source_path, thread_row, summary, summary_path, original_copy, cli_version):
    first = summary["first"]
    last = summary["last"]
    meta = {
        "session_id": thread_id,
        "id": thread_id,
        "timestamp": iso_z(first),
        "cwd": summary["cwd"],
        "originator": "Codex Desktop",
        "cli_version": cli_version,
        "source": "vscode",
        "model_provider": "openai",
        "external_agent_source": "claude",
        "external_agent_source_path": str(source_path),
        "external_agent_archive_summary_path": str(summary_path),
        "external_agent_archive_original_copy": str(original_copy),
        "external_agent_compacted": True,
    }
    user_text = f"打开 Claude 导入会话归档：{summary['purpose']}"
    first_requests = "\n".join(f"- {item}" for item in summary["first_user_messages"][:2]) or "- 无"
    recent_requests = "\n".join(f"- {item}" for item in summary["recent_user_messages"][:4]) or "- 无"
    summary_blocks = "\n".join(f"- {item}" for item in summary["summary_blocks"][:2]) or "- 无 Claude summary 块，已从首尾用户请求抽取摘要"
    tools = ", ".join(f"{name}({count})" for name, count in summary["tool_names"][:8]) or "无明显工具记录"
    modules = [json.loads(item) for item in summary["modules"][:5]]
    module_text = "\n".join(f"- {item['title']}: {clean_text(item['body'], 500)}" for item in modules) or "- 无明确模块拆解"
    context_refs = context_reference_paths(source_path, summary, Path.home())
    context_text = "\n".join(
        f"- {item['label']} ({'存在' if item['exists'] else '未发现'}): {md_path(item['path'])}"
        for item in context_refs[:12]
    ) or "- 无"
    turn_preview = "\n".join(
        f"- Turn {item['index']}: {clean_text(item['user'], 260)} -> {clean_text(item['assistant'], 260)}"
        for item in summary["turns"][:8]
    ) or "- 无可提取 turn"
    assistant_text = "\n".join(
        [
            f"Claude 导入会话已整理为归档入口：{summary['purpose']}",
            "",
            "## 会话在做什么",
            clean_text(summary["detail"], 1800) or summary["purpose"],
            "",
            "## 原始/压缩摘要",
            summary_blocks,
            "",
            "## 模块/阶段",
            module_text,
            "",
            "## 开始时的用户请求",
            first_requests,
            "",
            "## 最近的用户请求",
            recent_requests,
            "",
            "## 用户 Prompt 时间线预览",
            turn_preview,
            "",
            "## 工具和规模",
            f"- 常见工具: {tools}",
            f"- 原消息规模: user {summary['user_count']} / assistant {summary['assistant_count']} / rows {summary['total']}",
            "",
            "## 文件路径",
            f"- [详细内容参考原 Claude JSONL]({md_path(source_path)})",
            f"- [归档副本]({md_path(original_copy)})",
            f"- [结构化摘要]({md_path(summary_path)})",
            f"- 时间: `{iso_z(first)}` 到 `{iso_z(last)}`",
            "",
            "## 可追溯环境入口",
            context_text,
            "",
            "为避免超出 Codex 上下文窗口，这里不再内联完整 Claude 历史。需要继续工作时，请新开 Codex 会话，并按上面的摘要或原始 JSONL 路径定向读取相关片段。",
        ]
    )
    return [
        event(first, "session_meta", meta),
        *user_text_lines(first, user_text),
        *agent_text_lines(last, assistant_text),
    ]


def strip_extended_prefix(path_text):
    text = str(path_text or "")
    if text.startswith("\\\\?\\"):
        return text[4:]
    return text


def codex_db_cwd(path_text):
    text = strip_extended_prefix(path_text)
    if re.match(r"^[A-Za-z]:\\", text):
        return "\\\\?\\" + text
    return text


def continuation_thread_id(old_thread_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"claude-to-codex-continuation-v1:{old_thread_id}"))


def continuation_rollout_path(codex_home, ts, thread_id):
    day_dir = codex_home / "sessions" / ts.strftime("%Y/%m/%d")
    name = f"rollout-{ts.strftime('%Y-%m-%dT%H-%M-%S')}-{thread_id}.jsonl"
    return day_dir / name


def continuation_prompt(old_thread_id, title, cwd, summary_path, original_copy, source_path):
    return "\n".join(
        [
            "继续一个从 Claude 迁移到 Codex 的会话。",
            "",
            f"旧归档 threadId: {old_thread_id}",
            f"旧归档标题: {title}",
            f"工作目录: {strip_extended_prefix(cwd)}",
            f"结构化摘要路径: {summary_path}",
            f"归档 Claude JSONL 副本: {original_copy}",
            f"原始 Claude JSONL: {source_path}",
            "",
            "请先读取结构化摘要路径，理解此前任务状态，再继续用户后续请求。",
            "不要把旧归档当作可直接 resume 的 Codex thread；旧归档只用于查看历史。",
        ]
    )


def continuation_rollout_rows(thread_id, old_thread_id, row, prompt, ts, cli_version):
    cwd = strip_extended_prefix(row["cwd"])
    title = row["title"]
    assistant_text = "\n".join(
        [
            "已创建 Claude 迁移会话的 Codex 原生续写入口。",
            "",
            f"- 旧归档 threadId: {old_thread_id}",
            f"- 旧归档标题: {title}",
            f"- 工作目录: {cwd}",
            "",
            "后续请在这个会话里继续；旧归档只用于查看历史。",
        ]
    )
    meta = {
        "session_id": thread_id,
        "id": thread_id,
        "timestamp": iso_z(ts),
        "cwd": cwd,
        "originator": "codex-tui",
        "cli_version": cli_version,
        "source": "cli",
        "thread_source": "user",
        "model_provider": "openai",
        "claude_archive_thread_id": old_thread_id,
    }
    return [
        event(ts, "session_meta", meta),
        *user_text_lines(ts, prompt),
        *agent_text_lines(ts, assistant_text),
    ]


def create_continuation_threads(con, index_rows, codex_home, cli_version, dry_run, overwrite, recency_mode, only_thread_id):
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
            "created_at_ms",
            "updated_at_ms",
            "thread_source",
            "preview",
            "recency_at",
            "recency_at_ms",
        ],
    )
    now = dt.datetime.now(dt.timezone.utc)
    planned = 0
    created = 0
    skipped_existing = 0
    skipped_missing_summary = 0
    for item in index_rows:
        old_thread_id = item.get("thread_id")
        if only_thread_id and old_thread_id != only_thread_id:
            continue
        summary_path = Path(str(item.get("summary_path") or ""))
        if not old_thread_id or not summary_path.exists():
            skipped_missing_summary += 1
            continue
        new_thread_id = continuation_thread_id(old_thread_id)
        exists = con.execute("SELECT 1 FROM threads WHERE id = ?", (new_thread_id,)).fetchone()
        if exists and not overwrite:
            skipped_existing += 1
            continue
        old_row = con.execute(
            "SELECT cwd, title, created_at, updated_at, created_at_ms, updated_at_ms, recency_at, recency_at_ms FROM threads WHERE id = ?",
            (old_thread_id,),
        ).fetchone()
        cwd = old_row[0] if old_row and old_row[0] else item.get("cwd") or str(Path.home())
        title = old_row[1] if old_row and old_row[1] else item.get("purpose") or item.get("title") or old_thread_id
        first = parse_iso(item.get("first")) or now
        last = parse_iso(item.get("last")) or first
        ts = now if recency_mode == "now" else last
        rollout_path = continuation_rollout_path(codex_home, ts, new_thread_id)
        prompt = continuation_prompt(
            old_thread_id,
            title,
            cwd,
            summary_path,
            item.get("original_copy") or "",
            item.get("source_path") or "",
        )
        planned += 1
        if dry_run:
            print(f"would_create_continuation {new_thread_id} <- {old_thread_id} {title} {rollout_path}")
            continue
        rollout_path.parent.mkdir(parents=True, exist_ok=True)
        rows = continuation_rollout_rows(
            new_thread_id,
            old_thread_id,
            {"cwd": cwd, "title": title},
            prompt,
            ts,
            cli_version,
        )
        with rollout_path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        created_ms = unix_ms(first)
        updated_ms = unix_ms(ts)
        new_title = f"续写: {title}"[:200]
        preview = f"Claude 迁移续写入口。先读摘要: {summary_path}"
        if exists:
            con.execute(
                """
                UPDATE threads
                   SET rollout_path = ?,
                       created_at = ?,
                       updated_at = ?,
                       source = 'cli',
                       model_provider = 'openai',
                       cwd = ?,
                       title = ?,
                       sandbox_policy = '{"type":"disabled"}',
                       approval_mode = 'never',
                       tokens_used = 0,
                       has_user_event = 1,
                       archived = 0,
                       archived_at = NULL,
                       cli_version = ?,
                       first_user_message = ?,
                       memory_mode = 'enabled',
                       model = NULL,
                       created_at_ms = ?,
                       updated_at_ms = ?,
                       thread_source = 'user',
                       preview = ?,
                       recency_at = ?,
                       recency_at_ms = ?
                 WHERE id = ?
                """,
                (
                    str(rollout_path),
                    created_ms // 1000,
                    updated_ms // 1000,
                    codex_db_cwd(cwd),
                    new_title,
                    cli_version,
                    prompt[:4000],
                    created_ms,
                    updated_ms,
                    preview[:4000],
                    updated_ms // 1000,
                    updated_ms,
                    new_thread_id,
                ),
            )
        else:
            con.execute(
                """
                INSERT INTO threads (
                id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                sandbox_policy, approval_mode, tokens_used, has_user_event, archived, archived_at,
                git_sha, git_branch, git_origin_url, cli_version, first_user_message,
                agent_nickname, agent_role, memory_mode, model, reasoning_effort, agent_path,
                created_at_ms, updated_at_ms, thread_source, preview, recency_at, recency_at_ms
                ) VALUES (
                ?, ?, ?, ?, 'cli', 'openai', ?, ?, '{"type":"disabled"}', 'never',
                0, 1, 0, NULL, NULL, NULL, NULL, ?, ?, NULL, NULL, 'enabled', NULL, NULL,
                NULL, ?, ?, 'user', ?, ?, ?
                )
                """,
                (
                    new_thread_id,
                    str(rollout_path),
                    created_ms // 1000,
                    updated_ms // 1000,
                    codex_db_cwd(cwd),
                    new_title,
                    cli_version,
                    prompt[:4000],
                    created_ms,
                    updated_ms,
                    preview[:4000],
                    updated_ms // 1000,
                    updated_ms,
                ),
            )
        created += 1
    return planned, created, skipped_existing, skipped_missing_summary


def archive_and_compact_imports(con, records, codex_home, archive_root, fallback_home, cli_version, dry_run, overwrite_manual_titles):
    require_threads_columns(con, ["id", "rollout_path", "title", "preview", "created_at", "updated_at", "recency_at"])
    originals_dir = archive_root / "originals"
    summaries_dir = archive_root / "summaries"
    rollout_backups_dir = archive_root / "rollout_backups"
    existing_index_path = archive_root / "index.json"
    existing_index = {}
    if existing_index_path.exists():
        existing_index = {item.get("thread_id"): item for item in json.loads(existing_index_path.read_text(encoding="utf-8"))}
    if not dry_run:
        archive_root.mkdir(parents=True, exist_ok=True)
        originals_dir.mkdir(parents=True, exist_ok=True)
        summaries_dir.mkdir(parents=True, exist_ok=True)
        rollout_backups_dir.mkdir(parents=True, exist_ok=True)

    index_rows = []
    changed = 0
    skipped_manual_titles = 0
    for record in records:
        thread_id = record.get("imported_thread_id")
        source_path = Path(str(record.get("source_path") or "")).expanduser()
        if not thread_id or not source_path.exists():
            continue
        row = con.execute(
            "SELECT id, rollout_path, title, preview FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
        if not row:
            continue
        thread_row = {"id": row[0], "rollout_path": row[1], "title": row[2], "preview": row[3]}
        old_index_item = existing_index.get(thread_id)
        manual_title = bool(old_index_item and thread_row["title"] != old_index_item.get("purpose"))
        if manual_title and not overwrite_manual_titles:
            skipped_manual_titles += 1
        rollout_path = Path(thread_row["rollout_path"])
        summary = summarize_claude_session(source_path, fallback_home)
        day = summary["first"].strftime("%Y/%m/%d")
        slug = safe_slug(summary["purpose"])
        original_copy = originals_dir / day / f"{thread_id}-{source_path.name}"
        summary_path = summaries_dir / day / f"{thread_id}-{slug}.md"
        rollout_backup = rollout_backups_dir / day / rollout_path.name
        index_row = {
            "thread_id": thread_id,
            "purpose": summary["purpose"],
            "title": summary["title"],
            "first": iso_z(summary["first"]),
            "last": iso_z(summary["last"]),
            "source_path": str(source_path),
            "original_copy": str(original_copy),
            "summary_path": str(summary_path),
            "rollout_path": str(rollout_path),
            "user_count": summary["user_count"],
            "assistant_count": summary["assistant_count"],
            "rows": summary["total"],
        }
        if manual_title and not overwrite_manual_titles:
            index_row["manual_title"] = thread_row["title"]
            index_row["title_update"] = "manual_title_preserved"
        index_rows.append(index_row)
        if dry_run:
            continue
        original_copy.parent.mkdir(parents=True, exist_ok=True)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        rollout_backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, original_copy)
        if rollout_path.exists() and not rollout_backup.exists():
            shutil.copy2(rollout_path, rollout_backup)
        summary_path.write_text(
            markdown_summary(thread_id, source_path, original_copy, summary_path, thread_row, summary),
            encoding="utf-8",
        )
        compact_rows = compact_rollout_rows(thread_id, source_path, thread_row, summary, summary_path, original_copy, cli_version)
        with rollout_path.open("w", encoding="utf-8") as handle:
            for compact_row in compact_rows:
                handle.write(json.dumps(compact_row, ensure_ascii=False, separators=(",", ":")) + "\n")
        first_ms = unix_ms(summary["first"])
        last_ms = unix_ms(summary["last"])
        cursor = con.execute(
            """
            UPDATE threads
               SET title = ?,
                   preview = ?,
                   created_at = ?,
                   updated_at = ?,
                   created_at_ms = ?,
                   updated_at_ms = ?,
                   recency_at = ?,
                   recency_at_ms = ?
             WHERE id = ?
            """,
            (
                thread_row["title"] if manual_title and not overwrite_manual_titles else summary["purpose"],
                f"Claude导入归档: {summary['purpose']}。详细内容参考 {summary_path}",
                first_ms // 1000,
                last_ms // 1000,
                first_ms,
                last_ms,
                last_ms // 1000,
                last_ms,
                thread_id,
            ),
        )
        changed += max(cursor.rowcount, 0)

    if not dry_run:
        index_path = archive_root / "index.json"
        index_path.write_text(json.dumps(index_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(index_rows), changed, skipped_manual_titles


def load_index(index_path):
    if not index_path.exists():
        return {"records": []}
    data = json.loads(index_path.read_text(encoding="utf-8"))
    data.setdefault("records", [])
    return data


def claude_session_paths(claude_projects, include_subagents):
    paths = sorted(claude_projects.rglob("*.jsonl"))
    if not include_subagents:
        paths = [path for path in paths if "subagents" not in path.parts]
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
    parser.add_argument("--archive-and-compact-imports", action="store_true")
    parser.add_argument("--create-continuation-threads", action="store_true")
    parser.add_argument("--archive-root", default="~/.codex/imported_claude_archive")
    parser.add_argument("--overwrite-manual-titles", action="store_true")
    parser.add_argument("--overwrite-continuation-threads", action="store_true")
    parser.add_argument("--continuation-recency", choices=["preserve", "now"], default="preserve")
    parser.add_argument("--thread-id", help="Limit continuation creation to one archived Claude thread id.")
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
        if args.archive_and_compact_imports:
            con = sqlite3.connect(state_db)
            try:
                archived, _, skipped_manual = archive_and_compact_imports(
                    con,
                    records,
                    codex_home,
                    expand(args.archive_root),
                    fallback_home,
                    args.cli_version,
                    True,
                    args.overwrite_manual_titles,
                )
            finally:
                con.close()
            print(f"would_archive_and_compact_imported_sessions={archived}")
            print(f"would_skip_manual_title_sessions={skipped_manual}")
            return
        if args.create_continuation_threads:
            archive_index_path = expand(args.archive_root) / "index.json"
            archive_index_rows = json.loads(archive_index_path.read_text(encoding="utf-8")) if archive_index_path.exists() else []
            con = sqlite3.connect(state_db)
            try:
                planned, _, skipped_existing, skipped_missing_summary = create_continuation_threads(
                    con,
                    archive_index_rows,
                    codex_home,
                    args.cli_version,
                    True,
                    args.overwrite_continuation_threads,
                    args.continuation_recency,
                    args.thread_id,
                )
            finally:
                con.close()
            print(f"would_create_continuation_threads={planned}")
            print(f"would_skip_existing_continuation_threads={skipped_existing}")
            print(f"would_skip_missing_summary_threads={skipped_missing_summary}")
            return
        for path in missing[:20]:
            info = analyze_claude_session(path, fallback_home)
            print(f"{iso_z(info['first'])} {iso_z(info['last'])} {info['title']} {path}")
        return

    if (
        not args.fix_existing_timestamps
        and not args.import_missing
        and not args.archive_and_compact_imports
        and not args.create_continuation_threads
    ):
        raise SystemExit(
            "Choose --dry-run, --fix-existing-timestamps, --import-missing, "
            "--archive-and-compact-imports, or --create-continuation-threads."
        )

    backup_dir = None
    if not args.no_backup:
        backup_dir = backup_codex(codex_home, state_db, index_path)
        print(f"backup_dir={backup_dir}")

    con = sqlite3.connect(state_db)
    try:
        con.execute("BEGIN IMMEDIATE")
        fixed = 0
        imported = 0
        archived = 0
        archive_updates = 0
        skipped_manual = 0
        continuation_planned = 0
        continuation_created = 0
        continuation_skipped_existing = 0
        continuation_skipped_missing_summary = 0
        if args.fix_existing_timestamps:
            fixed = fix_existing_timestamps(con, records, fallback_home)
        if args.import_missing:
            imported = import_missing_sessions(con, index, missing, sessions_dir, fallback_home, args.cli_version)
        if args.archive_and_compact_imports:
            archived, archive_updates, skipped_manual = archive_and_compact_imports(
                con,
                records,
                codex_home,
                expand(args.archive_root),
                fallback_home,
                args.cli_version,
                False,
                args.overwrite_manual_titles,
            )
        if args.create_continuation_threads:
            archive_index_path = expand(args.archive_root) / "index.json"
            archive_index_rows = json.loads(archive_index_path.read_text(encoding="utf-8")) if archive_index_path.exists() else []
            (
                continuation_planned,
                continuation_created,
                continuation_skipped_existing,
                continuation_skipped_missing_summary,
            ) = create_continuation_threads(
                con,
                archive_index_rows,
                codex_home,
                args.cli_version,
                False,
                args.overwrite_continuation_threads,
                args.continuation_recency,
                args.thread_id,
            )
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
    print(f"archived_imported_sessions={archived}")
    print(f"archive_thread_updates={archive_updates}")
    print(f"skipped_manual_title_sessions={skipped_manual}")
    print(f"continuation_threads_planned={continuation_planned}")
    print(f"continuation_threads_created={continuation_created}")
    print(f"continuation_threads_skipped_existing={continuation_skipped_existing}")
    print(f"continuation_threads_skipped_missing_summary={continuation_skipped_missing_summary}")


if __name__ == "__main__":
    main()
