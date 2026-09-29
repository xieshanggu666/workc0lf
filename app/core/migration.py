# -*- coding: utf-8 -*-
"""轻量建表后迁移：为旧版数据库补齐新增列。

项目没有引入 Alembic，这里用 ADD COLUMN 做前向兼容：
- pending_crisis / last_resolution：旧档案没有待处理危机，补 NULL 即进入每日阶段
- row_version：乐观锁版本号，旧行统一从 1 开始
对已是最新结构的库为幂等无操作。
"""
from sqlalchemy import inspect, text


def _existing_columns(conn, table):
    try:
        return {c["name"] for c in inspect(conn).get_columns(table)}
    except Exception:
        return set()


def ensure_schema(engine):
    with engine.begin() as conn:
        columns = _existing_columns(conn, "game_sessions")
        if not columns:
            # 表尚未创建，create_all 会按最新模型建表，无需迁移
            return
        if "pending_crisis" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN pending_crisis JSON"))
        if "last_resolution" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN last_resolution JSON"))
        if "row_version" not in columns:
            # NOT NULL + 常量默认值，存量行全部初始化为 1
            conn.execute(
                text("ALTER TABLE game_sessions ADD COLUMN row_version INTEGER NOT NULL DEFAULT 1")
            )
