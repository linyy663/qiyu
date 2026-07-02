"""
会话数据缓存模块
在凌晨2-6点期间调用七鱼批量导出接口，将会话列表缓存到 SQLite
白天查询时直接从缓存读取，避免"仅限凌晨2-6点调用"的限制
"""
import sqlite3
import json
import os
import threading
import time
import datetime
import sys

DB_PATH = os.path.join(os.path.dirname(__file__), 'session_cache.db')
# GitHub Actions 缓存的 JSON 文件目录（与 data/ 目录对应）
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
# 加载过一次 JSON 后自动同步到 SQLite（后续查询更快）
AUTO_SYNC_JSON = True


def _now_str():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """初始化数据库表"""
    conn = _get_conn()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS session_cache (
            date TEXT PRIMARY KEY,
            total_count INTEGER DEFAULT 0,
            raw_json TEXT,
            cached_at TEXT,
            status TEXT DEFAULT 'success'
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS session_cache_detail (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            session_id INTEGER NOT NULL,
            staff_id INTEGER DEFAULT 0,
            staff_name TEXT DEFAULT '',
            raw_json TEXT,
            UNIQUE(date, session_id)
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_detail_date ON session_cache_detail(date)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_detail_staff ON session_cache_detail(staff_id)')
    conn.commit()
    conn.close()


def get_cached_sessions(date_str: str, staff_id: int = None) -> dict:
    """从缓存获取指定日期的会话列表（优先 SQLite，fallback JSON 文件）
    返回: {date, sessions: [...], cached_at, total, cache_source}
    """
    conn = _get_conn()
    try:
        # 获取缓存元数据
        meta = conn.execute(
            'SELECT * FROM session_cache WHERE date = ?', (date_str,)
        ).fetchone()
        if not meta:
            # SQLite 无数据，尝试 JSON fallback
            sessions = _read_json_sessions(date_str)
            if sessions is not None:
                result_sessions = sessions
                if staff_id:
                    result_sessions = [s for s in sessions if s.get('staffId') == staff_id]
                return {
                    "date": date_str,
                    "sessions": result_sessions,
                    "cached_at": None,
                    "total": len(result_sessions),
                    "cached": True,
                    "status": "success (JSON)",
                    "cache_source": "json"
                }
            return {
                "date": date_str,
                "sessions": [],
                "cached_at": None,
                "total": 0,
                "cached": False,
                "cache_source": "none"
            }

        # 获取会话详情
        if staff_id:
            rows = conn.execute(
                'SELECT * FROM session_cache_detail WHERE date = ? AND staff_id = ? ORDER BY session_id',
                (date_str, staff_id)
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM session_cache_detail WHERE date = ? ORDER BY session_id',
                (date_str,)
            ).fetchall()

        sessions = []
        for row in rows:
            try:
                sessions.append(json.loads(row['raw_json']))
            except (json.JSONDecodeError, TypeError):
                pass

        return {
            "date": date_str,
            "sessions": sessions,
            "cached_at": meta['cached_at'],
            "total": len(sessions),
            "cached": True,
            "status": meta['status'],
            "cache_source": "sqlite"
        }
    finally:
        conn.close()


def store_sessions(date_str: str, sessions: list) -> int:
    """存储会话数据到缓存，返回存储条数"""
    conn = _get_conn()
    try:
        # 先清除旧数据
        conn.execute('DELETE FROM session_cache_detail WHERE date = ?', (date_str,))
        conn.execute('DELETE FROM session_cache WHERE date = ?', (date_str,))

        count = 0
        for s in sessions:
            if not isinstance(s, dict):
                continue
            session_id = s.get('id')
            if not session_id:
                continue
            staff_id = s.get('staffId', 0)
            staff_name = s.get('staffName', '')
            conn.execute(
                'INSERT OR REPLACE INTO session_cache_detail (date, session_id, staff_id, staff_name, raw_json) VALUES (?, ?, ?, ?, ?)',
                (date_str, session_id, staff_id, staff_name, json.dumps(s, ensure_ascii=False))
            )
            count += 1

        conn.execute(
            'INSERT OR REPLACE INTO session_cache (date, total_count, raw_json, cached_at, status) VALUES (?, ?, ?, ?, ?)',
            (date_str, count, json.dumps(sessions[:10], ensure_ascii=False), _now_str(), 'success')
        )

        conn.commit()
        return count
    except Exception as e:
        conn.rollback()
        # 记录失败状态
        try:
            conn.execute(
                'INSERT OR REPLACE INTO session_cache (date, total_count, raw_json, cached_at, status) VALUES (?, ?, ?, ?, ?)',
                (date_str, 0, '', _now_str(), f'error: {str(e)[:200]}')
            )
            conn.commit()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def get_cache_status(date_str: str = None) -> dict:
    """获取缓存状态（同时检查 SQLite 和 JSON 文件）"""
    conn = _get_conn()
    try:
        if date_str:
            meta = conn.execute('SELECT * FROM session_cache WHERE date = ?', (date_str,)).fetchone()
            if not meta:
                # SQLite 无数据，检查 JSON
                sessions = _read_json_sessions(date_str)
                if sessions is not None:
                    return {"date": date_str, "cached": True, "total": len(sessions), "cached_at": None, "status": "success (JSON)", "cache_source": "json"}
                return {"date": date_str, "cached": False, "total": 0, "status": "not_cached", "cache_source": "none"}
            return {
                "date": date_str,
                "cached": True,
                "total": meta['total_count'],
                "cached_at": meta['cached_at'],
                "status": meta['status'],
                "cache_source": "sqlite"
            }
        else:
            # 返回所有缓存日期（合并 SQLite + JSON）
            rows = conn.execute('SELECT date, total_count, cached_at, status FROM session_cache ORDER BY date DESC LIMIT 30').fetchall()
            sqlite_dates = {r['date']: {
                "date": r['date'], "total": r['total_count'], "cached_at": r['cached_at'], "status": r['status'], "cache_source": "sqlite"
            } for r in rows}

            # 检查 JSON 文件中是否有其他日期
            latest = _read_json_latest()
            if latest:
                json_dates = latest.get('sessionDates') or latest.get('dates') or []
                for d in json_dates:
                    if d not in sqlite_dates:
                        json_path = os.path.join(DATA_DIR, f'sessions-{d}.json')
                        if os.path.exists(json_path):
                            try:
                                with open(json_path, 'r', encoding='utf-8') as f:
                                    data = json.load(f)
                                total = len(data.get('sessions', []))
                                sqlite_dates[d] = {"date": d, "total": total, "cached_at": data.get('generatedAt'), "status": "success (JSON)", "cache_source": "json"}
                            except Exception:
                                pass

            return {
                "dates": sorted(sqlite_dates.values(), key=lambda x: x['date'], reverse=True)
            }
    finally:
        conn.close()


def is_export_window() -> bool:
    """判断当前是否在批量导出窗口（凌晨2:00-6:00）"""
    now = datetime.datetime.now()
    return 2 <= now.hour < 6


def _read_json_sessions(date_str: str) -> list | None:
    """从 JSON 文件读取会话数据（GitHub Actions 产出）"""
    json_path = os.path.join(DATA_DIR, f'sessions-{date_str}.json')
    if not os.path.exists(json_path):
        return None
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        sessions = data.get('sessions', [])
        if sessions and AUTO_SYNC_JSON:
            # 自动同步到 SQLite（后续查询更快）
            try:
                store_sessions(date_str, sessions)
            except Exception:
                pass  # 同步失败不影响读取
        return sessions
    except (json.JSONDecodeError, IOError):
        return None


def _read_json_latest() -> dict | None:
    """读取 latest.json 获取可用日期列表"""
    latest_path = os.path.join(DATA_DIR, 'latest.json')
    if not os.path.exists(latest_path):
        return None
    try:
        with open(latest_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None
