#!/usr/bin/env python3
"""
SQLite 数据层 —— 投递记录、聊天消息、设置、每日统计。
"""

import sqlite3
import threading
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

DB_PATH = Path(__file__).parent / ".boss_profile" / "boss_state.db"

_local = threading.local()


def get_db() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _local.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_title TEXT NOT NULL,
            company TEXT,
            salary TEXT,
            job_url TEXT UNIQUE NOT NULL,
            city TEXT,
            experience TEXT,
            education TEXT,
            hr_name TEXT,
            hr_title TEXT,
            description TEXT,
            status TEXT DEFAULT 'pending',
            greeting_text TEXT,
            greeting_sent_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER REFERENCES applications(id),
            hr_name TEXT NOT NULL,
            hr_company TEXT,
            job_title TEXT,
            last_message_text TEXT,
            last_message_from TEXT,
            last_message_at TIMESTAMP,
            unread_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            auto_reply_enabled INTEGER DEFAULT 1,
            interest_level TEXT,
            hr_wechat TEXT,
            wechat_shared_at TIMESTAMP,
            resume_sent INTEGER DEFAULT 0,
            phone_shared INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id),
            sender TEXT NOT NULL,
            content TEXT NOT NULL,
            delivery_status TEXT,
            ai_generated INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS daily_stats (
            date TEXT PRIMARY KEY,
            applications_sent INTEGER DEFAULT 0,
            messages_sent INTEGER DEFAULT 0,
            messages_received INTEGER DEFAULT 0,
            auto_replies_sent INTEGER DEFAULT 0
        );
    """)
    try:
        db.execute("ALTER TABLE messages ADD COLUMN delivery_status TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE conversations ADD COLUMN interest_level TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE conversations ADD COLUMN hr_wechat TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE conversations ADD COLUMN wechat_shared_at TIMESTAMP")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE conversations ADD COLUMN resume_sent INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE conversations ADD COLUMN phone_shared INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # 候选池表
    db.executescript("""
        CREATE TABLE IF NOT EXISTS shortlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_url TEXT UNIQUE NOT NULL,
            job_title TEXT NOT NULL,
            company TEXT,
            salary TEXT,
            city TEXT,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # 默认设置
    defaults = {
        "greeting_template": "您好！看到贵司在招{job_title}，很感兴趣。PS：正在和你聊天的这个AI工具是我自己开发的——就当是我的技术名片了",
        "greeting_enabled": "true",
        "ai_reply_style": "professional",
        "daily_apply_limit": "120",
        "auto_reply_enabled": "false",
        "min_reply_delay_sec": "15",
        "max_reply_delay_sec": "20",
        "batch_delay_min_sec": "30",
        "batch_delay_max_sec": "90",
        "resume_summary": "",
        "wechat_id": "",
        "search_keywords": "AI Agent,大模型开发,AI产品经理,RAG开发,大模型应用",
        "default_city": "淄博",
    }
    for k, v in defaults.items():
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    db.commit()


def _row_to_dict(row) -> Optional[dict]:
    return dict(row) if row else None


def _rows_to_list(rows) -> List[dict]:
    return [dict(r) for r in rows]


# ══════════════════════════════════════
#  Applications
# ══════════════════════════════════════


def add_application(job: dict) -> int:
    db = get_db()
    cur = db.execute(
        """INSERT OR IGNORE INTO applications
           (job_title, company, salary, job_url, city, experience, education, hr_name, hr_title, description, company_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job.get("title", ""),
            job.get("company", ""),
            job.get("salary", ""),
            job.get("url", ""),
            job.get("city", ""),
            job.get("experience", ""),
            job.get("education", ""),
            job.get("hr_name", ""),
            job.get("hr_title", ""),
            job.get("description", ""),
            job.get("company_id", ""),
        ),
    )
    db.commit()
    return cur.lastrowid if cur.lastrowid else 0


def get_application(app_id: int) -> Optional[dict]:
    return _row_to_dict(get_db().execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone())


def get_application_by_url(url: str) -> Optional[dict]:
    return _row_to_dict(get_db().execute("SELECT * FROM applications WHERE job_url=?", (url,)).fetchone())


def update_application_from_job(app_id: int, job: dict) -> Optional[dict]:
    """用本次搜索结果刷新已有岗位；空值不覆盖旧值。"""
    fields = {
        "job_title": job.get("title", ""),
        "company": job.get("company", ""),
        "salary": job.get("salary", ""),
        "city": job.get("city", ""),
        "experience": job.get("experience", ""),
        "education": job.get("education", ""),
        "hr_name": job.get("hr_name", ""),
        "hr_title": job.get("hr_title", ""),
        "description": job.get("description", ""),
    }
    params = []
    assignments = []
    for column, value in fields.items():
        value = (value or "").strip()
        assignments.append(f"{column}=CASE WHEN ?!='' THEN ? ELSE {column} END")
        params.extend([value, value])
    params.append(app_id)

    db = get_db()
    db.execute(
        f"""UPDATE applications SET {", ".join(assignments)},
            updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        params,
    )
    db.commit()
    return get_application(app_id)


def list_applications(status: Optional[str] = None, limit: int = 50) -> List[dict]:
    db = get_db()
    if status:
        rows = db.execute(
            "SELECT * FROM applications WHERE status=? ORDER BY updated_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM applications ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    return _rows_to_list(rows)


def update_application_status(app_id: int, status: str, greeting_text: Optional[str] = None):
    db = get_db()
    if greeting_text:
        db.execute(
            """UPDATE applications SET status=?, greeting_text=?, greeting_sent_at=CURRENT_TIMESTAMP,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (status, greeting_text, app_id),
        )
    else:
        db.execute(
            "UPDATE applications SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, app_id),
        )
    db.commit()


def get_today_application_count() -> int:
    row = (
        get_db()
        .execute("SELECT COUNT(*) as cnt FROM applications WHERE date(greeting_sent_at)=date('now','localtime')")
        .fetchone()
    )
    return row["cnt"] if row else 0


def get_today_pending_count() -> int:
    row = get_db().execute("SELECT COUNT(*) as cnt FROM applications WHERE status='pending'").fetchone()
    return row["cnt"] if row else 0


def count_hours_replied_in_range(hours: int) -> int:
    row = (
        get_db()
        .execute(
            "SELECT COUNT(*) as cnt FROM conversations WHERE last_message_from='hr' AND datetime(last_message_at) > datetime('now','localtime',? || ' hours')",
            (f"-{hours}",),
        )
        .fetchone()
    )
    return row["cnt"] if row else 0


def count_interest_level(level: str) -> int:
    row = get_db().execute("SELECT COUNT(*) as cnt FROM conversations WHERE interest_level=?", (level,)).fetchone()
    return row["cnt"] if row else 0


def get_pending_applications(limit: int = 50) -> List[dict]:
    return _rows_to_list(
        get_db()
        .execute(
            "SELECT * FROM applications WHERE status='pending' AND job_url!='' ORDER BY id LIMIT ?",
            (limit,),
        )
        .fetchall()
    )


# ══════════════════════════════════════
#  Conversations
# ══════════════════════════════════════


def get_or_create_conversation(application_id: int, hr_name: str, hr_company: str, job_title: str) -> int:
    db = get_db()
    if application_id:
        row = db.execute("SELECT id FROM conversations WHERE application_id=?", (application_id,)).fetchone()
        if row:
            return row["id"]
    # 按 HR 名字查重（精确匹配，去空白）
    name = hr_name.strip() if hr_name else ""
    if name:
        row = db.execute("SELECT id FROM conversations WHERE hr_name=? AND status!='closed'", (name,)).fetchone()
        if row:
            return row["id"]
    cur = db.execute(
        """INSERT INTO conversations (application_id, hr_name, hr_company, job_title)
           VALUES (?, ?, ?, ?)""",
        (application_id, name, hr_company, job_title),
    )
    db.commit()
    return cur.lastrowid


def get_conversation(conv_id: int) -> Optional[dict]:
    return _row_to_dict(get_db().execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone())


def list_active_conversations() -> List[dict]:
    return _rows_to_list(
        get_db().execute("SELECT * FROM conversations WHERE status!='closed' ORDER BY updated_at DESC").fetchall()
    )


def find_conversation_by_hr_name(hr_name: str) -> Optional[dict]:
    return _row_to_dict(
        get_db()
        .execute(
            "SELECT * FROM conversations WHERE hr_name=? ORDER BY updated_at DESC LIMIT 1",
            (hr_name,),
        )
        .fetchone()
    )


def update_conversation_last_message(conv_id: int, text: str, sender: str, unread_delta: int = 0):
    db = get_db()
    db.execute(
        """UPDATE conversations SET last_message_text=?, last_message_from=?,
           last_message_at=CURRENT_TIMESTAMP, unread_count=MAX(0, unread_count+?),
           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (text[:200], sender, unread_delta, conv_id),
    )
    db.commit()


def update_conversation_status(conv_id: int, status: str):
    get_db().execute(
        "UPDATE conversations SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (status, conv_id),
    )
    get_db().commit()


def update_conversation_interest(conv_id: int, level: str):
    get_db().execute(
        "UPDATE conversations SET interest_level=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (level, conv_id),
    )
    get_db().commit()


def update_conversation_wechat(conv_id: int, wechat_id: str):
    get_db().execute(
        "UPDATE conversations SET hr_wechat=?, wechat_shared_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (wechat_id, conv_id),
    )
    get_db().commit()


def mark_resume_sent(conv_id: int):
    get_db().execute("UPDATE conversations SET resume_sent=1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (conv_id,))
    get_db().commit()


def mark_phone_shared(conv_id: int):
    get_db().execute("UPDATE conversations SET phone_shared=1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (conv_id,))
    get_db().commit()


def get_wechat_exchanges() -> List[dict]:
    """返回所有已获取到微信号的会话，包含岗位详情。"""
    return _rows_to_list(
        get_db()
        .execute(
            """SELECT c.id, c.hr_name, c.hr_company, c.job_title, c.hr_wechat,
                      c.wechat_shared_at, c.interest_level,
                      a.city, a.salary, a.experience, a.education, a.description
               FROM conversations c
               LEFT JOIN applications a ON c.application_id = a.id
               WHERE c.hr_wechat IS NOT NULL AND c.hr_wechat != ''
               ORDER BY c.wechat_shared_at DESC"""
        )
        .fetchall()
    )


def set_auto_reply(conv_id: int, enabled: bool):
    get_db().execute(
        "UPDATE conversations SET auto_reply_enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (1 if enabled else 0, conv_id),
    )
    get_db().commit()


# ══════════════════════════════════════
#  Messages
# ══════════════════════════════════════


def add_message(
    conversation_id: int, sender: str, content: str, ai_generated: bool = False, delivery_status: str = ""
) -> int:
    db = get_db()
    cur = db.execute(
        "INSERT INTO messages (conversation_id, sender, content, delivery_status, ai_generated) VALUES (?, ?, ?, ?, ?)",
        (conversation_id, sender, content, delivery_status, 1 if ai_generated else 0),
    )
    db.commit()
    return cur.lastrowid


def get_messages(conversation_id: int, limit: int = 50) -> List[dict]:
    return _rows_to_list(
        get_db()
        .execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at ASC, id ASC LIMIT ?",
            (conversation_id, limit),
        )
        .fetchall()
    )


def get_recent_messages(conversation_id: int, limit: int = 5) -> List[dict]:
    return _rows_to_list(
        get_db()
        .execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (conversation_id, limit),
        )
        .fetchall()
    )


def replace_conversation_messages(conversation_id: int, messages: List[dict]):
    """用 BOSS 当前消息历史覆盖本地缓存，避免 Web 端展示过期或错会话内容。"""
    db = get_db()
    old_ai = {
        r["content"]
        for r in db.execute(
            "SELECT content FROM messages WHERE conversation_id=? AND ai_generated=1",
            (conversation_id,),
        ).fetchall()
    }
    db.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
    for msg in messages:
        sender = msg.get("sender", "hr")
        content = (msg.get("content") or "").strip()
        delivery_status = (msg.get("status") or msg.get("delivery_status") or "").strip()
        if not content:
            continue
        ai_generated = 1 if sender == "me" and content in old_ai else 0
        db.execute(
            "INSERT INTO messages (conversation_id, sender, content, delivery_status, ai_generated) VALUES (?, ?, ?, ?, ?)",
            (conversation_id, sender, content, delivery_status, ai_generated),
        )
    db.commit()


def get_last_hr_message(conversation_id: int) -> Optional[dict]:
    return _row_to_dict(
        get_db()
        .execute(
            "SELECT * FROM messages WHERE conversation_id=? AND sender='hr' ORDER BY created_at DESC LIMIT 1",
            (conversation_id,),
        )
        .fetchone()
    )


def message_exists(conversation_id: int, content: str, sender: str) -> bool:
    row = (
        get_db()
        .execute(
            "SELECT id FROM messages WHERE conversation_id=? AND content=? AND sender=? ORDER BY created_at DESC LIMIT 1",
            (conversation_id, content, sender),
        )
        .fetchone()
    )
    return row is not None


# ══════════════════════════════════════
#  Settings
# ══════════════════════════════════════


def get_setting(key: str, default: str = "") -> str:
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    get_db().execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
        (key, value),
    )
    get_db().commit()


def get_all_settings() -> dict:
    rows = get_db().execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


# ══════════════════════════════════════
#  Daily Stats
# ══════════════════════════════════════


def _today() -> str:
    return date.today().isoformat()


def _ensure_today():
    get_db().execute("INSERT OR IGNORE INTO daily_stats (date) VALUES (?)", (_today(),))
    get_db().commit()


def increment_daily_stat(field: str):
    _ensure_today()
    get_db().execute(
        f"UPDATE daily_stats SET {field} = {field} + 1 WHERE date=?",
        (_today(),),
    )
    get_db().commit()


def get_daily_stats(date_str: Optional[str] = None) -> dict:
    d = date_str or _today()
    row = get_db().execute("SELECT * FROM daily_stats WHERE date=?", (d,)).fetchone()
    return dict(row) if row else {}


def get_today_auto_reply_count() -> int:
    row = (
        get_db()
        .execute(
            "SELECT COUNT(*) as cnt FROM messages WHERE ai_generated=1 AND date(created_at)=date('now','localtime')"
        )
        .fetchone()
    )
    return row["cnt"] if row else 0


# ═══════════════════════
#  候选池
# ═══════════════════════
def add_to_shortlist(
    job_url: str, title: str, company: str = "", salary: str = "", city: str = "", note: str = ""
) -> int:
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO shortlists (job_url, job_title, company, salary, city, note) VALUES (?,?,?,?,?,?)",
            (job_url, title, company, salary, city, note),
        )
        db.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return 0


def remove_from_shortlist(shortlist_id: int):
    get_db().execute("DELETE FROM shortlists WHERE id=?", (shortlist_id,))
    get_db().commit()


def list_shortlists(limit: int = 100) -> list:
    rows = get_db().execute("SELECT * FROM shortlists ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return _rows_to_list(rows)


def is_in_shortlist(job_url: str) -> bool:
    row = get_db().execute("SELECT COUNT(*) as cnt FROM shortlists WHERE job_url=?", (job_url,)).fetchone()
    return row["cnt"] > 0 if row else False


# ══════════════════════════════════════
#  公司名归一化 & 去重
# ══════════════════════════════════════

_COMPANY_SUFFIXES = re.compile(
    r"(有限|有限责任|股份|集团|控股)?(公司|企业|厂|店|馆|所|中心|社)?$"
)
_COMPANY_BRACKETS = re.compile(r"[\(（][^)）]*[\)）]")
_COMPANY_PREFIX = re.compile(r"^[京津沪渝冀豫云辽黑湘皖苏浙赣鄂桂甘晋内蒙古陕吉闽贵粤青川藏琼宁新]{1,2}省?")


def _normalize_company_name(name: str) -> str:
    """归一化公司名：去前缀省份、去后缀(有限/股份/集团)公司、去括号尾注。"""
    name = (name or "").strip()
    if not name:
        return ""
    name = _COMPANY_PREFIX.sub("", name).strip()
    name = _COMPANY_BRACKETS.sub("", name).strip()
    name = _COMPANY_SUFFIXES.sub("", name).strip()
    return name if len(name) >= 2 else name


def has_company_been_applied(company: str, company_id: str = "") -> dict:
    """
    检查该公司是否已经投递过。
    规则：applications 中存在同公司名且状态为 applied 或 replied。
    :return: {"applied": bool, "count": int}
    """
    db = get_db()
    name = (company or "").strip()
    if not name:
        return {"applied": False, "count": 0}

    # 1) 优先用 company_id 精确匹配
    if company_id:
        row = db.execute(
            "SELECT COUNT(*) as cnt FROM applications WHERE company_id=? AND status IN ('applied','replied')",
            (company_id,),
        ).fetchone()
        if row and row["cnt"] > 0:
            return {"applied": True, "count": row["cnt"]}

    # 2) 精确公司名匹配
    row = db.execute(
        "SELECT COUNT(*) as cnt FROM applications WHERE company=? AND status IN ('applied','replied')",
        (name,),
    ).fetchone()
    if row and row["cnt"] > 0:
        return {"applied": True, "count": row["cnt"]}

    # 3) 归一化后模糊匹配：取出所有 applied/replied 的公司名，逐一比对
    rows = db.execute(
        "SELECT DISTINCT company FROM applications WHERE status IN ('applied','replied')"
    ).fetchall()
    norm_target = _normalize_company_name(name)
    if len(norm_target) < 2:
        return {"applied": False, "count": 0}

    matched = []
    for r in rows:
        existing = (r["company"] or "").strip()
        if not existing:
            continue
        norm_existing = _normalize_company_name(existing)
        # 双向包含：A 包含 B 或 B 包含 A（且较短名长度≥2）
        if (
            norm_existing
            and len(norm_existing) >= 2
            and (norm_existing in norm_target or norm_target in norm_existing)
        ):
            count_row = db.execute(
                "SELECT COUNT(*) as cnt FROM applications WHERE company=? AND status IN ('applied','replied')",
                (existing,),
            ).fetchone()
            if count_row and count_row["cnt"] > 0:
                matched.append(count_row["cnt"])

    total = sum(matched)
    return {"applied": total > 0, "count": total}


def clean_open_positions(raw: str, salary_filter: str = "") -> tuple:
    """
    清洗BOSS直聘"在招岗位"原始文本。
    返回: (清理后的岗位串, 岗位数)
    """
    text = (raw or "").strip()
    # 去掉薪资片段 如 "5-7K"、"10-15K"
    text = re.sub(r"\d+[-~]\d+K", "", text)
    # 去掉UI噪声词
    noise = re.compile(r"(职位搜索|更多|举报|收藏|分享|申请|投递|沟通)")
    text = noise.sub("", text)
    # 按分隔符拆开、去重、去空、去内部空格
    parts = re.split(r"[、，,;；\n]+", text)
    seen = set()
    cleaned = []
    for p in parts:
        p = p.strip().replace(" ", "").replace("　", "")
        if p and len(p) > 1 and p not in seen:
            seen.add(p)
            cleaned.append(p)
    return ("、".join(cleaned), len(cleaned))


# ── company_id 列迁移（兼容旧表）──
def _migrate_company_id_column():
    db = get_db()
    try:
        db.execute("ALTER TABLE applications ADD COLUMN company_id TEXT")
        db.commit()
    except sqlite3.OperationalError:
        pass


_migrate_company_id_column()

# 启动时初始化
init_db()
