"""把 MiniMax Code 正在做的事情，翻译成桌宠能演的一个状态。

替代 codex_state.py 的数据源，输出结构完全一致。数据全部来自
MiniMax Code 本机落盘的只读记录，不联网、不碰密钥：

* ``~/.minimax/v2/sqlite/runtime-state.sqlite``
  - ``local_runtime_sessions``        会话列表：标题、工作目录、状态（started/idle/error/aborted）
  - ``local_runtime_query_view_states``  每个回合的处理起止时间（进行中 = finished 为空）
  - ``local_runtime_message_rows``    逐条消息：含 tool_calls（工具名、参数、结果状态）
  - ``local_runtime_token_usage``     每回合 token 用量

支持多个会话并发：所有进行中的回合会被聚合到同一张卡片上
（标题标注"N 个任务并行"，工具数 / tokens 汇总，活动显示最新动的那个）。
"""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import time

DATA_HOME = os.environ.get("MINIMAX_HOME") or os.path.expanduser("~/.minimax")
STATE_DB = os.path.join(DATA_HOME, "v2", "sqlite", "runtime-state.sqlite")

SOURCE_NAME = "MiniMax Code"

DONE_GRACE_SECONDS = 90      # 回合完成后，"完成了"表情保留多久
ERROR_GRACE_SECONDS = 180    # 会话出错/被打断后，保留多久
QUIET_SECONDS = 60           # 进行中的回合这么久没新消息，就认为在"等待你"
STALE_SECONDS = 900          # 回合"进行中"但超过这么久没有任何消息，视为残留（进程崩了）
MAX_RUNNING_SHOWN = 3        # 并行标题里最多列几个任务名

TOOL_LABELS = {
    "bash": "运行命令",
    "write": "修改文件",
    "edit": "修改文件",
    "read": "读文件",
    "glob": "找文件",
    "grep": "搜内容",
    "web_search": "搜索网页",
    "web_fetch": "抓网页",
    "webSearch": "搜索网页",
    "webFetch": "抓网页",
    "ask_user": "等你回答",
    "skill": "调用技能",
    "task": "委派子任务",
    "task_append": "继续子任务",
    "task_output": "查看子任务",
    "task_query": "查后台任务",
    "task_stop": "停止子任务",
    "todowrite": "规划步骤",
    "update_goal": "更新目标",
    "get_goal": "查看目标",
    "website_deploy": "发布网站",
}

ARG_HINT_KEYS = (
    "description",
    "command",
    "query",
    "pattern",
    "file_path",
    "path",
    "url",
    "skill",
    "name",
    "project_name",
)

SECRET_HINTS = ("key", "token", "secret", "password", "authorization", "cookie")


def available() -> bool:
    return os.path.exists(STATE_DB)


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect("file:%s?mode=ro" % STATE_DB, uri=True, timeout=1.5)
    con.row_factory = sqlite3.Row
    return con


def _shorten(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _compact(value: int) -> str:
    if value >= 1_000_000:
        return "%.1fM" % (value / 1_000_000.0)
    if value >= 1_000:
        return "%dk" % round(value / 1000.0)
    return str(value)


def _thread_label(con: sqlite3.Connection, session: dict) -> str:
    title = html.unescape(session.get("title") or "").strip()
    if title:
        return title
    row = con.execute(
        "select data_json from local_runtime_message_rows "
        "where session_id = ? and role = 'user' order by created_at_ms asc limit 1",
        (session["session_id"],),
    ).fetchone()
    if row:
        try:
            return html.unescape(str(json.loads(row[0]).get("msg_content") or "")).strip()
        except Exception:
            return ""
    return ""


def _arg_hint(arguments) -> str:
    """从工具参数里挑一个安全的、能看懂的提示词（和 codex_state 一样防泄密）。"""
    if isinstance(arguments, str):
        try:
            args = json.loads(arguments)
        except Exception:
            return ""
    elif isinstance(arguments, dict):
        args = arguments
    else:
        return ""
    if not isinstance(args, dict):
        return ""
    for key in ARG_HINT_KEYS:
        value = args.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        text = re.sub(r"\s+", " ", value).strip()
        lowered = (key + " " + text).lower()
        if any(hint in lowered for hint in SECRET_HINTS):
            return ""
        if key == "command":
            first = text.split("&&")[0].strip("\"'")
            return os.path.basename(first.split(" ")[0])[:24]
        return _shorten(text, 26)
    return ""


def _tool_activity(call: dict) -> str:
    name = str(call.get("tool_name") or "")
    label = TOOL_LABELS.get(name, "调用工具")
    hint = _arg_hint(call.get("tool_call_args"))
    if name == "ask_user":
        return label
    return f"{label} · {hint}" if hint else label


def _turn_messages(con: sqlite3.Connection, session_id: str, turn_id: str) -> list[dict]:
    rows = con.execute(
        "select role, created_at_ms, data_json from local_runtime_message_rows "
        "where session_id = ? and turn_id = ? order by created_at_ms asc",
        (session_id, turn_id),
    ).fetchall()
    out = []
    for row in rows:
        try:
            data = json.loads(row["data_json"])
        except Exception:
            data = {}
        data["_role"] = row["role"]
        data["_created_ms"] = row["created_at_ms"] or 0
        out.append(data)
    return out


def _summarize_turn(messages: list[dict]) -> dict:
    """扫一遍回合内的消息，统计工具数、找最新活动、最后发言。"""
    tools = 0
    latest_activity = ""
    last_say = ""
    for msg in messages:
        calls = msg.get("tool_calls") or []
        if calls:
            tools += len(calls)
            latest_activity = _tool_activity(calls[-1])
        content = msg.get("msg_content")
        if isinstance(content, str) and content.strip() and msg["_role"] == "assistant":
            last_say = content
    return {
        "tools": tools,
        "activity": latest_activity,
        "say": last_say,
    }


def _running_turns(con: sqlite3.Connection, now: float) -> list[dict]:
    """所有进行中的回合，附带所属会话信息和消息摘要。

    只认 ``processing_finished_at_ms IS NULL`` 的回合；最后一
    条消息太久的按残留数据丢弃（会话进程崩溃时 finished 不会被写上）。
    返回按"最新动静"降序排列。
    """
    rows = con.execute(
        "select q.session_id, q.current_turn_id, q.processing_started_at_ms, "
        "       s.title, s.status, s.workspace_dir, s.updated_at_ms "
        "from local_runtime_query_view_states q "
        "join local_runtime_sessions s on s.session_id = q.session_id "
        "where q.processing_finished_at_ms is null and s.archived = 0 "
        "order by q.updated_at_ms desc limit 12"
    ).fetchall()

    running = []
    for row in rows:
        session_id = row["session_id"]
        turn_id = row["current_turn_id"] or ""
        started_ms = row["processing_started_at_ms"] or 0
        messages = _turn_messages(con, session_id, turn_id)
        info = _summarize_turn(messages)
        msg_times = [m["_created_ms"] for m in messages]
        last_ms = max(msg_times) if msg_times else started_ms
        quiet_for = now - last_ms / 1000.0
        if quiet_for > STALE_SECONDS:
            continue  # 崩溃残留，不算在跑
        running.append({
            "session_id": session_id,
            "turn_id": turn_id,
            "started_ms": started_ms,
            "title": _thread_label(con, dict(row)),
            "cwd": row["workspace_dir"] or "",
            "status": row["status"] or "",
            "messages": messages,
            "info": info,
            "last_ms": last_ms,
            "quiet_for": quiet_for,
        })
    running.sort(key=lambda item: item["last_ms"], reverse=True)
    return running


def _tokens_for_sessions(con: sqlite3.Connection, session_ids: list[str]) -> str:
    if not session_ids:
        return ""
    marks = ",".join("?" * len(session_ids))
    row = con.execute(
        "select coalesce(sum(input_tokens + output_tokens), 0) total "
        "from local_runtime_token_usage where session_id in (%s)" % marks,
        session_ids,
    ).fetchone()
    total = int(row["total"] or 0) if row else 0
    return "%s tok" % _compact(total) if total else ""


def _task_payload(con: sqlite3.Connection, item: dict, now: float) -> dict:
    """把一个进行中的回合整理成 `tasks` 数组里的单个任务。"""
    last_calls = item["messages"][-1].get("tool_calls") if item["messages"] else None
    if last_calls and any(c.get("tool_name") == "ask_user" for c in last_calls):
        state, state_label = "waiting", "等待你"
    elif item["quiet_for"] > QUIET_SECONDS:
        state, state_label = "waiting", "等待你"
    elif item["info"]["activity"]:
        state, state_label = "working", "执行中"
    else:
        state, state_label = "thinking", "思考中"

    activity = item["info"]["activity"]
    if state == "thinking":
        activity = activity or "正在思考下一步"

    return {
        "title": _shorten(item["title"] or "MiniMax 任务", 60),
        "state": state,
        "label": state_label,
        "activity": _shorten(activity, 80),
        "elapsed": max(0, int(now - item["started_ms"] / 1000.0)) if item["started_ms"] else 0,
        "tools": item["info"]["tools"],
        "tokens": _tokens_for_sessions(con, [item["session_id"]]),
        "threadId": item["session_id"],
        "cwd": item["cwd"],
    }


def _parallel_title(running: list[dict]) -> str:
    names = [item["title"] for item in running if item["title"]]
    if not names:
        names = ["MiniMax 任务"]
    if len(names) == 1:
        return names[0]
    shown = "、".join(_shorten(n, 16) for n in names[:MAX_RUNNING_SHOWN])
    extra = len(names) - MAX_RUNNING_SHOWN
    if extra > 0:
        shown += " 等"
    return "%d 个任务并行：%s" % (len(names), shown)


def _latest_finished(con: sqlite3.Connection) -> dict:
    """没有进行中的回合时，取最近结束的一个回合 + 所属会话。"""
    row = con.execute(
        "select q.session_id, q.current_turn_id, q.processing_started_at_ms, "
        "       q.processing_finished_at_ms, s.title, s.status, s.workspace_dir, "
        "       s.updated_at_ms, s.error_message "
        "from local_runtime_query_view_states q "
        "join local_runtime_sessions s on s.session_id = q.session_id "
        "where q.processing_finished_at_ms is not null and s.archived = 0 "
        "order by q.processing_finished_at_ms desc limit 1"
    ).fetchone()
    return dict(row) if row else {}


def snapshot() -> dict:
    """当前桌宠状态。没有 MiniMax Code 在跑也总能返回可用结果。"""
    now = time.time()
    try:
        con = _connect()
    except sqlite3.Error:
        return _idle("")
    try:
        running = _running_turns(con, now)

        # ---- 有并发进行中的回合：聚合展示 ----------------------------------
        if running:
            tasks = [_task_payload(con, item, now) for item in running]
            newest = running[0]
            newest_payload = tasks[0]
            session_ids = [item["session_id"] for item in running]
            tokens = _tokens_for_sessions(con, session_ids)
            elapsed = (
                max(0, int(now - newest["started_ms"] / 1000.0))
                if newest["started_ms"] else 0
            )

            state = newest_payload["state"]
            state_label = newest_payload["label"]

            activity = newest_payload["activity"]
            if state == "waiting":
                activity = activity or "等你确认或回复"
            if len(running) > 1:
                prefix = "并行%d · " % len(running)
                activity = prefix + (activity or "执行中")

            return {
                "state": state,
                "label": state_label,
                "title": _shorten(_parallel_title(running), 60),
                "activity": _shorten(activity, 80),
                "say": _shorten(newest["info"]["say"], 110),
                "elapsed": elapsed,
                "tools": sum(item["info"]["tools"] for item in running),
                "tokens": tokens,
                "source": SOURCE_NAME,
                "threadId": newest["session_id"],
                "cwd": newest["cwd"],
                # 并行时携带任务列表；单任务/空闲时不带（前端按无数组处理）
                **({"tasks": tasks} if len(running) > 1 else {}),
            }

        # ---- 没有进行中的回合：看最近结束的回合 ----------------------------
        finished = _latest_finished(con)
        if not finished:
            return _idle("")

        session_id = finished["session_id"]
        label = _thread_label(con, finished)
        cwd = finished.get("workspace_dir") or ""
        since_finish = now - (finished["processing_finished_at_ms"] or 0) / 1000.0
        since_touch = now - (finished.get("updated_at_ms") or 0) / 1000.0
        status = finished.get("status") or ""

        if status == "error" and since_touch < ERROR_GRACE_SECONDS:
            return {
                "state": "error",
                "label": "出错了",
                "title": _shorten(label or "MiniMax 任务", 60),
                "activity": _shorten(str(finished.get("error_message") or "任务出错了"), 80),
                "say": "",
                "elapsed": 0,
                "tools": 0,
                "tokens": _tokens_for_sessions(con, [session_id]),
                "source": SOURCE_NAME,
                "threadId": session_id,
                "cwd": cwd,
            }
        if status == "aborted" and since_touch < ERROR_GRACE_SECONDS:
            return {
                "state": "interrupted",
                "label": "被打断",
                "title": _shorten(label or "MiniMax 任务", 60),
                "activity": "",
                "say": "",
                "elapsed": 0,
                "tools": 0,
                "tokens": "",
                "source": SOURCE_NAME,
                "threadId": session_id,
                "cwd": cwd,
            }
        if since_finish < DONE_GRACE_SECONDS:
            started_ms = finished.get("processing_started_at_ms") or 0
            finished_ms = finished.get("processing_finished_at_ms") or 0
            return {
                "state": "done",
                "label": "完成了",
                "title": _shorten(label or "MiniMax 任务", 60),
                "activity": "",
                "say": "",
                "elapsed": max(0, int((finished_ms - started_ms) / 1000.0)) if started_ms else 0,
                "tools": 0,
                "tokens": _tokens_for_sessions(con, [session_id]),
                "source": SOURCE_NAME,
                "threadId": session_id,
                "cwd": cwd,
            }
        return _idle(session_id)
    except sqlite3.Error:
        return _idle("")
    finally:
        con.close()


def _idle(session_id: str) -> dict:
    return {
        "state": "idle",
        "label": "空闲",
        "title": "",
        "activity": "",
        "say": "",
        "elapsed": 0,
        "tools": 0,
        "tokens": "",
        "source": SOURCE_NAME,
        "threadId": session_id or "",
    }


if __name__ == "__main__":
    print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
