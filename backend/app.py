"""
网易七鱼客服工单统计与质检工具 - 后端服务
提供数据统计、会话分析、质量报告等功能
"""
import datetime
import json
import sys
import time as time_mod
import re
from collections import Counter
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from config_manager import load_config, save_config, get_config_status
from qiyu_api import QiyuAPI
from session_cache import init_db, get_cached_sessions, store_sessions, get_cache_status, is_export_window

app = Flask(__name__, static_folder='../frontend', static_url_path='')
CORS(app)

# 内存缓存（简单实现）
cache = {}
raw_cache = {}  # 原始API结果缓存，供agents和report共享


def get_timestamp_range(date_str: str = None, days: int = None) -> tuple:
    """获取指定日期的毫秒时间戳范围（中国时区 UTC+8）
    如果指定 days，返回 date_str 到 date_str+days 的范围
    时间范围自动截断到「当前时刻」，避免传入未来时间戳被七鱼API拒绝
    """
    if date_str:
        dt = datetime.datetime.strptime(date_str, '%Y-%m-%d')
    else:
        # 默认前一天
        dt = datetime.datetime.now() - datetime.timedelta(days=1)
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)

    # 中国时区 UTC+8
    china_tz = datetime.timezone(datetime.timedelta(hours=8))
    start = dt.replace(tzinfo=china_tz)
    span = days if days and days > 1 else 1
    end = start + datetime.timedelta(days=span)

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    # 截断：七鱼API不允许 end_time 超过当前时间
    now_ms = int(datetime.datetime.now(china_tz).timestamp() * 1000)
    if end_ms > now_ms:
        end_ms = now_ms

    if span > 1:
        label = f"{dt.strftime('%Y-%m-%d')} ~ {(start + datetime.timedelta(days=span - 1)).strftime('%Y-%m-%d')}"
    else:
        label = dt.strftime('%Y-%m-%d')
    return start_ms, end_ms, label


def get_cache_key(prefix: str, date_str: str) -> str:
    return f"{prefix}_{date_str}"


# ==================== 静态页面 ====================

@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


# ==================== 配置管理 ====================

@app.route('/api/config', methods=['GET'])
def api_get_config():
    """获取配置状态"""
    return jsonify({"code": 200, "data": get_config_status()})


@app.route('/api/config', methods=['POST'])
def api_save_config():
    """保存配置"""
    data = request.json or {}
    config = load_config()

    if 'appKey' in data:
        config['appKey'] = data['appKey']
    if 'appSecret' in data:
        config['appSecret'] = data['appSecret']
    if 'baseUrl' in data:
        config['baseUrl'] = data['baseUrl']
    if 'autoRefresh' in data:
        config['autoRefresh'] = data['autoRefresh']
    if 'refreshInterval' in data:
        config['refreshInterval'] = data['refreshInterval']

    save_config(config)
    return jsonify({"code": 200, "message": "配置保存成功", "data": get_config_status()})


# ==================== 工具函数 ====================

def api_call_with_retry(fn, *args, max_retries=4):
    """带指数退避重试的API调用，处理14009频率限制和OSError"""
    import time as time_mod
    delays = [1, 2, 4, 8]  # 指数退避
    for attempt in range(max_retries + 1):
        try:
            result = fn(*args)
        except OSError:
            if attempt < max_retries:
                time_mod.sleep(delays[min(attempt, len(delays)-1)])
                continue
            return {"code": -1, "message": "系统错误（OSError）"}
        if result.get('code') != 14009:
            return result
        if attempt < max_retries:
            wait = delays[min(attempt, len(delays)-1)]
            time_mod.sleep(wait)
    return result


# ==================== 1. 每日数据总览 ====================

@app.route('/api/stats/overview', methods=['GET'])
def api_overview():
    """获取数据总览。多天时逐天查询后聚合，同时返回 daily 数组供趋势图直接使用"""
    date_str = request.args.get('date')
    days = request.args.get('days', type=int)

    api = QiyuAPI()
    if not api.app_key or not api.app_secret:
        return jsonify({"code": 401, "message": "请先配置 AppKey 和 AppSecret"})

    import time as time_mod

    # ---- 多天聚合模式 ----
    if days and days > 1:
        china_tz = datetime.timezone(datetime.timedelta(hours=8))
        base_dt = datetime.datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=china_tz)
        total_days = days

        daily_data = []
        errors = []

        for i in range(total_days):
            day_dt = base_dt + datetime.timedelta(days=i)
            day_str = day_dt.strftime('%Y-%m-%d')
            day_start_ms, day_end_ms, _ = get_timestamp_range(day_str, days=1)

            # 检查缓存（10分钟有效）
            cache_key = get_cache_key('overview', day_str)
            cached = cache.get(cache_key)
            if cached and (time_mod.time() - cached[0] < 600):
                cached_point = cached[1].get('data', {})
                daily_data.append(_extract_daily_point(day_str, cached_point))
                continue

            # 首日加初始延迟，避免紧贴上一个API请求
            if i == 0:
                time_mod.sleep(2)

            result = api_call_with_retry(api.get_overview, day_start_ms, day_end_ms, max_retries=5)

            if result.get('code') == 200:
                raw = result.get('message', {})
                point = _extract_daily_point(day_str, raw)
                daily_data.append(point)

                # 写缓存
                cache_data = {"code": 200, "data": {"date": day_str, **point}}
                cache[cache_key] = (time_mod.time(), cache_data)
            else:
                errors.append({
                    "date": day_str,
                    "code": result.get('code'),
                    "message": result.get('message', 'API调用失败')
                })
                daily_data.append(None)

            # 日间间隔（加大到 2 秒，减少 14009 频率限制）
            if i < total_days - 1:
                time_mod.sleep(2)

        # 过滤出有效天数据
        valid_days = [d for d in daily_data if d is not None]

        if not valid_days:
            return jsonify({
                "code": 500,
                "message": f"所有 {total_days} 天数据获取均失败，可能是API频率限制（14009）",
                "data": {"errors": errors, "daily": [], "days": total_days}
            })

        # ---- 聚合统计 ----
        # 累加字段
        total_sessions = sum(d.get('totalSessions', 0) for d in valid_days)
        effective_sessions = sum(d.get('effectiveSessions', 0) for d in valid_days)
        total_messages = sum(d.get('totalMessages', 0) for d in valid_days)
        queue_count = sum(d.get('queueCount', 0) for d in valid_days)
        visit_count = sum(d.get('visitCount', 0) for d in valid_days)
        total_consult = sum(d.get('totalConsultCount', 0) for d in valid_days)

        # 取最大值的字段
        login_staff = max(d.get('loginStaffCount', 0) for d in valid_days)
        max_queue = max(d.get('maxQueueTime', 0) for d in valid_days)

        # 比率字段按总会话量加权平均
        def weighted_avg(field):
            total_w = sum(d.get('totalSessions', 0) for d in valid_days)
            if total_w == 0:
                return 0
            return round(sum(d.get(field, 0) * d.get('totalSessions', 0) for d in valid_days) / total_w, 1)

        # 时间类字段按总会话量加权平均（保持 ms）
        def weighted_avg_ms(field):
            total_w = sum(d.get('totalSessions', 0) for d in valid_days)
            if total_w == 0:
                return 0
            return round(sum(d.get(field, 0) * d.get('totalSessions', 0) for d in valid_days) / total_w)

        overview = {
            "date": f"{date_str} ~ {valid_days[-1]['date']}",
            "days": total_days,
            "totalSessions": total_sessions,
            "effectiveSessions": effective_sessions,
            "assignedRatio": weighted_avg('assignedRatio'),
            "totalMessages": total_messages,
            "answerRatio": weighted_avg('answerRatio'),
            "satisfactionRatio": weighted_avg('satisfactionRatio'),
            "avgFirstRespTime": weighted_avg_ms('avgFirstRespTime'),
            "avgRespTime": weighted_avg_ms('avgRespTime'),
            "avgSessionTime": weighted_avg_ms('avgSessionTime'),
            "evaRatio": weighted_avg('evaRatio'),
            "queueCount": queue_count,
            "maxQueueTime": max_queue,
            "oneOffRatio": weighted_avg('oneOffRatio'),
            "loginStaffCount": login_staff,
            "visitCount": visit_count,
            "totalConsultCount": total_consult,
            # 每日明细供趋势图使用
            "daily": valid_days,
            "errors": errors if errors else None,
            "validDays": len(valid_days),
            "totalDays": total_days,
        }

        return jsonify({"code": 200, "data": overview})

    # ---- 单天模式（原逻辑）----
    query_date = date_str
    start_ms, end_ms, actual_date = get_timestamp_range(query_date)

    cache_key = get_cache_key('overview', actual_date)
    cache_ttl = 300
    if cache_key in cache:
        cached_time, cached_data = cache[cache_key]
        if time_mod.time() - cached_time < cache_ttl:
            return jsonify(cached_data)

    result = api_call_with_retry(api.get_overview, start_ms, end_ms)

    if result.get('code') == 200:
        data = result.get('message', {})
        point = _extract_daily_point(actual_date, data)

        overview = {
            "date": actual_date,
            "days": days or 1,
            **point,
            "raw": data,
            "daily": [point],  # 单天也提供 daily，保持一致性
        }
        result_data = {"code": 200, "data": overview}
        cache[cache_key] = (time_mod.time(), result_data)
        return jsonify(result_data)
    else:
        return jsonify({"code": result.get('code', 500),
                        "message": result.get('message', 'API调用失败')})


def _extract_daily_point(date_str, raw_or_data):
    """从 API 原始数据或已解析数据中提取每日指标点"""
    # 兼容 raw 和已解析的 data
    if isinstance(raw_or_data, dict):
        d = raw_or_data
    else:
        d = {}

    def to_pct(v):
        if v is None or v < 0:
            return 0
        return round(v * 100, 1) if isinstance(v, float) and v <= 1 else v

    return {
        "date": date_str,
        "totalSessions": d.get('sessions', d.get('totalSessions', 0)),
        "effectiveSessions": d.get('effectSessions', d.get('effectiveSessions', 0)),
        "assignedRatio": to_pct(d.get('assignedRatio', 0)),
        "totalMessages": d.get('messages', d.get('totalMessages', 0)),
        "answerRatio": to_pct(d.get('answerRatio', 0)),
        "satisfactionRatio": to_pct(d.get('satisfactionRatio', 0)),
        "avgFirstRespTime": d.get('avgFirstRespTime', 0),
        "avgRespTime": d.get('avgRespTime', 0),
        "avgSessionTime": d.get('avgTime', d.get('avgSessionTime', 0)),
        "evaRatio": to_pct(d.get('evaRatio', 0)),
        "queueCount": d.get('queueCount', 0),
        "maxQueueTime": d.get('maxQueueTime', 0),
        "oneOffRatio": to_pct(d.get('oneOffRatio', 0)),
        "loginStaffCount": d.get('loginStaffCount', 0),
        "visitCount": d.get('visit', d.get('visitCount', 0)),
        "totalConsultCount": d.get('totalConsultCount', 0),
    }


@app.route('/api/stats/overview/trend', methods=['GET'])
def api_overview_trend():
    """获取指定时间段的每日指标趋势数据"""
    import time as time_mod
    date_str = request.args.get('date')
    days = request.args.get('days', type=int)
    if not days or days <= 1:
        return jsonify({"code": 400, "message": "days参数必须大于1"})

    api = QiyuAPI()
    if not api.app_key or not api.app_secret:
        return jsonify({"code": 401, "message": "请先配置 AppKey 和 AppSecret"})

    china_tz = datetime.timezone(datetime.timedelta(hours=8))
    base_dt = datetime.datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=china_tz)

    trend_points = []
    for i in range(days):
        day_dt = base_dt + datetime.timedelta(days=i)
        day_str = day_dt.strftime('%Y-%m-%d')
        day_start_ms, day_end_ms, _ = get_timestamp_range(day_str, days=1)

        # 检查缓存（扩展至10分钟）
        cache_key = get_cache_key('overview', day_str)
        cached = cache.get(cache_key)
        if cached and (time_mod.time() - cached[0] < 600):
            overview_data = cached[1].get('data', {})
            trend_points.append(_extract_trend_point(day_str, overview_data))
            continue

        # 首次API调用前加较长间隔，避免紧跟前一个请求触发 14009
        if i == 0:
            time_mod.sleep(2)

        # 指数退避重试（最多5次重试）
        result = api_call_with_retry(api.get_overview, day_start_ms, day_end_ms, max_retries=5)

        if result.get('code') == 200:
            raw = result.get('message', {})
            def to_pct(v):
                if v is None or v < 0:
                    return 0
                return round(v * 100, 1) if isinstance(v, float) and v <= 1 else v

            point = {
                "date": day_str,
                "totalSessions": raw.get('sessions', 0),
                "effectiveSessions": raw.get('effectSessions', 0),
                "answerRatio": to_pct(raw.get('answerRatio', 0)),
                "satisfactionRatio": to_pct(raw.get('satisfactionRatio', 0)),
                "oneOffRatio": to_pct(raw.get('oneOffRatio', 0)),
                "evaRatio": to_pct(raw.get('evaRatio', 0)),
                "totalMessages": raw.get('messages', 0),
                "avgFirstRespTime": raw.get('avgFirstRespTime', 0),
                "avgRespTime": raw.get('avgRespTime', 0),
                "queueCount": raw.get('queueCount', 0),
            }
            # 写入缓存供后续复用
            cache_data = {"code": 200, "data": {"date": day_str, **point, "raw": raw}}
            cache[cache_key] = (time_mod.time(), cache_data)
            trend_points.append(_extract_trend_point(day_str, cache_data['data']))
        else:
            trend_points.append({"date": day_str, "_error": True, "_errorCode": result.get('code'),
                                 "_errorMsg": result.get('message', 'API调用失败')})

        # 日间间隔 2 秒（减少 14009 频率限制触发概率）
        if i < days - 1:
            time_mod.sleep(2)

    return jsonify({"code": 200, "data": {
        "startDate": date_str,
        "days": days,
        "points": trend_points
    }})


def _extract_trend_point(date_str, data):
    """从 overview 数据中提取趋势点"""
    return {
        "date": date_str,
        "totalSessions": data.get('totalSessions', 0),
        "effectiveSessions": data.get('effectiveSessions', 0),
        "answerRatio": data.get('answerRatio', 0),
        "satisfactionRatio": data.get('satisfactionRatio', 0),
        "oneOffRatio": data.get('oneOffRatio', 0),
        "evaRatio": data.get('evaRatio', 0),
        "totalMessages": data.get('totalMessages', 0),
        "avgFirstRespTime": data.get('avgFirstRespTime', 0),
        "avgRespTime": data.get('avgRespTime', 0),
        "queueCount": data.get('queueCount', 0),
    }


# ==================== 2. 坐席绩效统计 ====================

@app.route('/api/stats/agents', methods=['GET'])
def api_agent_stats():
    """获取每个坐席的工作量和绩效数据"""
    date_str = request.args.get('date')
    days = request.args.get('days', type=int)
    start_ms, end_ms, actual_date = get_timestamp_range(date_str, days=days)

    api = QiyuAPI()
    if not api.app_key or not api.app_secret:
        return jsonify({"code": 401, "message": "请先配置 AppKey 和 AppSecret"})

    # 使用缓存避免频繁API调用（缓存5分钟，周缓存10分钟）
    import time as time_mod
    cache_key = get_cache_key('agents', actual_date.replace(' ~ ', '_to_'))
    cache_ttl = 600 if days and days > 1 else 300
    if cache_key in cache:
        cached_time, cached_data = cache[cache_key]
        if time_mod.time() - cached_time < cache_ttl:
            print(f"[CACHE] Returning cached agents data for {actual_date}")
            return jsonify(cached_data)

  # 顺序调用三个API，model=1返回全部坐席数据，间隔1秒避免频率限制
    workload_result = api_call_with_retry(api.get_staff_workload, start_ms, end_ms)
    time_mod.sleep(2)
    
    quality_result = api_call_with_retry(api.get_staff_quality, start_ms, end_ms)
    time_mod.sleep(2)
    
    satisfaction_result = api_call_with_retry(api.get_staff_satisfaction, start_ms, end_ms)
    time_mod.sleep(2)

    attendance_result = api_call_with_retry(api.get_staff_attendance, start_ms, end_ms)

    # 解析结果 - 使用 normalize_to_list 处理各种响应格式
    workload_data = normalize_to_list(workload_result.get('message', workload_result.get('data'))) if workload_result.get('code') == 200 else []
    quality_data = normalize_to_list(quality_result.get('message', quality_result.get('data'))) if quality_result.get('code') == 200 else []
    satisfaction_data = normalize_to_list(satisfaction_result.get('message', satisfaction_result.get('data'))) if satisfaction_result.get('code') == 200 else []
    attendance_data = normalize_to_list(attendance_result.get('message', attendance_result.get('data'))) if attendance_result.get('code') == 200 else []
    
    # 合并数据
    agents_map = {}

    # 处理工作量数据
    for item in (workload_data if isinstance(workload_data, list) else []):
        agent_id = item.get('id')
        if agent_id:
            agents_map[agent_id] = {
                "id": agent_id,
                "name": item.get('name', ''),
                "account": item.get('account', ''),
                "totalSessions": item.get('totalSessionCount', 0),
                "incomingSessions": item.get('sessionsCount', 0),
                "initiativeSessions": item.get('initiativeSessionsCount', 0),
                "redirectSessions": item.get('redirectSessionsCount', 0),
                "validSessions": item.get('validSessionCount', 0),
                "invalidSessions": item.get('uselessSessionsCount', 0),
                "noReplySessions": item.get('noReplySessionsCount', 0),
                "noReplyRatio": item.get('noReplyRatio', 0),
                "messageDealCount": item.get('messageDealCount', 0),
            }

    # 处理质量数据
    for item in (quality_data if isinstance(quality_data, list) else []):
        agent_id = item.get('id')
        if agent_id:
            if agent_id not in agents_map:
                agents_map[agent_id] = {"id": agent_id, "name": item.get('name', '')}
            # 七鱼quality接口比率是0-1小数，-1表示无数据
            def _to_pct_q(v):
                if v is None: return -1
                f = float(v)
                if f < 0: return -1
                return round(f * 100, 2) if f <= 1.0 else round(f, 2)
            agents_map[agent_id].update({
                "avgFirstRespTime": item.get('avgFirstRespTime', 0),
                "avgRespTime": item.get('avgRespTime', 0),
                "replyRatio": _to_pct_q(item.get('replyRatio', -1)),
                "avgSessionTime": item.get('avgTime', 0),
                "evaRatio": _to_pct_q(item.get('evaRatio', -1)),
                "satisfactionRatio": _to_pct_q(item.get('satisfactionRatio', -1)),
                "evaluationDetail": item.get('evaluationDetail', ''),
                "answerReplyRatio": _to_pct_q(item.get('answerReplyRatio', -1)),
                "oneOffRatio": _to_pct_q(item.get('oneOffRatio', -1)),
            })

    # 处理满意度数据
    for item in (satisfaction_data if isinstance(satisfaction_data, list) else []):
        agent_id = item.get('id')
        if agent_id:
            if agent_id not in agents_map:
                agents_map[agent_id] = {"id": agent_id, "name": item.get('name', '')}
            agents_map[agent_id].update({
                "inviteCount": item.get('inviteCount', 0),
                "evaCount": item.get('evaCount', 0),
                "verySatisfiedCount": item.get('verySatisfiedCount', 0),
                "satisfiedCount": item.get('satisfiedCount', 0),
                "normalCount": item.get('normalSatisfiedCount', 0),
                "notSatisfiedCount": item.get('notSatisfiedCount', 0),
                "veryNotSatisfiedCount": item.get('veryNotSatisfiedCount', 0),
                "staffInviteRatio": item.get('staffInviteRatio', 0),
                "satisfactionScore": item.get('satisfactionRatio', 0),
                "validSatisfactionScore": item.get('validSatisfactionRatio', 0),
            })

    # 处理考勤数据 — 按坐席聚合多天数据
    attendance_map = {}  # {agent_id: {firstLoginTs, firstOnlineTs, lastLogoutTs, loginDuration, onlineDuration, workDays, ...}}
    for item in (attendance_data if isinstance(attendance_data, list) else []):
        agent_id = item.get('id')
        if not agent_id:
            continue
        if agent_id not in attendance_map:
            attendance_map[agent_id] = {
                'firstLoginTs': None,
                'firstOnlineTs': None,
                'lastLogoutTs': None,
                'loginDuration': 0,
                'onlineDuration': 0,
                'pcOnlineDuration': 0,
                'mobileOnlineDuration': 0,
                'workDays': 0,
                'loginDays': 0,
            }
        am = attendance_map[agent_id]
        # 上岗时间：取最早的有效firstOnlineTs，没有则用firstLoginTs
        ft_login = item.get('firstLoginTs', 0) or 0
        ft_online = item.get('firstOnlineTs', 0) or 0
        lt_logout = item.get('lastLogoutTs', 0) or 0

        if ft_login > 0:
            if am['firstLoginTs'] is None or ft_login < am['firstLoginTs']:
                am['firstLoginTs'] = ft_login
            am['loginDays'] += 1
        if ft_online > 0:
            if am['firstOnlineTs'] is None or ft_online < am['firstOnlineTs']:
                am['firstOnlineTs'] = ft_online
        if lt_logout > 0:
            if am['lastLogoutTs'] is None or lt_logout > am['lastLogoutTs']:
                am['lastLogoutTs'] = lt_logout
        am['loginDuration'] += (item.get('loginDuration', 0) or 0)
        am['onlineDuration'] += (item.get('onlineDuration', 0) or 0)
        am['pcOnlineDuration'] += (item.get('pcOnlineDuration', 0) or 0)
        am['mobileOnlineDuration'] += (item.get('mobileOnlineDuration', 0) or 0)
        if (item.get('onlineDuration', 0) or 0) > 0:
            am['workDays'] += 1

    # 把考勤数据合并到每个坐席
    for agent_id, agent in agents_map.items():
        am = attendance_map.get(agent_id, {})
        # 上岗时间：优先用首次在线时间，没有则用首次登录时间
        first_online = am.get('firstOnlineTs')
        first_login = am.get('firstLoginTs')
        agent['firstOnlineTime'] = first_online if first_online else first_login
        agent['lastLogoutTime'] = am.get('lastLogoutTs')
        agent['loginDuration'] = am.get('loginDuration', 0)  # ms
        agent['onlineDuration'] = am.get('onlineDuration', 0)  # ms
        agent['pcOnlineDuration'] = am.get('pcOnlineDuration', 0)
        agent['mobileOnlineDuration'] = am.get('mobileOnlineDuration', 0)
        agent['workDays'] = am.get('workDays', 0)
        agent['loginDays'] = am.get('loginDays', 0)

    agents_list = list(agents_map.values())

    # 计算综合评分
    for agent in agents_list:
        agent['score'] = calculate_agent_score(agent)
        agent['grade'] = get_score_grade(agent['score'])

    # 按综合评分排序
    agents_list.sort(key=lambda x: x['score'], reverse=True)

    result_data = {
        "code": 200,
        "data": {
            "date": actual_date,
            "days": days or 1,
            "agents": agents_list,
            "total": len(agents_list),
            "workloadRaw": workload_data,
            "qualityRaw": quality_data,
            "satisfactionRaw": satisfaction_data
        }
    }

    # 缓存结果
    cache[cache_key] = (time_mod.time(), result_data)

    # 同时写入共享原始数据缓存，供report端点复用
    raw_cache[cache_key] = {
        'ts': time_mod.time(),
        'workload': workload_result,
        'quality': quality_result,
        'satisfaction': satisfaction_result,
        'attendance': attendance_result,
    }

    return jsonify(result_data)


def normalize_to_list(data, key_field='id') -> list:
    """将API返回的数据标准化为列表
    有些接口返回单个对象（model=1），有些返回数组（model=3）
    有些接口包装在 result 键里
    """
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # 如果包含 result 嵌套数组键（quality/satisfaction API常见格式）
        for k in ['result', 'staffs', 'list', 'items', 'data', 'records']:
            if k in data and isinstance(data[k], list):
                return data[k]
        # 如果是包含 id/name 的对象，说明是单条记录，包装成列表
        if key_field in data or 'name' in data:
            return [data]
    return []


def calculate_agent_score(agent: dict) -> float:
    """计算坐席综合评分（百分制）
    -1 表示该指标无数据，视为 0
    比率字段已在 agents_map 构建时转换为百分制(0-100)，-1=无数据
    """
    def safe_float(v, default=0):
        try:
            f = float(v)
            return max(f, 0)  # 负值(-1)当0处理
        except (ValueError, TypeError):
            return default

    score = 50  # 基础分

    # 应答率 (权重15) - 0-100百分制
    reply_ratio = safe_float(agent.get('replyRatio', 0))
    score += min(reply_ratio, 100) * 0.15

    # 满意度 (权重20) - 0-100百分制
    sat_ratio = safe_float(agent.get('satisfactionRatio', 0))
    score += min(sat_ratio, 100) * 0.20

    # 有效会话率 (权重10)
    total = agent.get('totalSessions', 0) or 1
    valid = agent.get('validSessions', 0)
    valid_ratio = (valid / total * 100) if total > 0 else 0
    score += min(valid_ratio, 100) * 0.10

    # 未回复率反向 (权重10) - noReplyRatio已是百分制
    no_reply = safe_float(agent.get('noReplyRatio', 0))
    # 七鱼的noReplyRatio有时是0-1小数
    if 0 < no_reply <= 1:
        no_reply = no_reply * 100
    score += max(0, 100 - no_reply) * 0.10

    # 平均响应速度 (权重15) - avgRespTime单位是毫秒
    avg_resp_ms = safe_float(agent.get('avgRespTime', 0))
    avg_resp_sec = avg_resp_ms / 1000 if avg_resp_ms > 0 else 0
    resp_score = max(0, 100 - avg_resp_sec / 3)  # 每多3秒扣1分
    score += min(resp_score, 100) * 0.15

    # 一次性解决率 (权重10) - 0-100百分制
    one_off = safe_float(agent.get('oneOffRatio', 0))
    score += min(one_off, 100) * 0.10

    # 参评率 (权重10) - 0-100百分制
    eva_r = safe_float(agent.get('evaRatio', 0))
    score += min(eva_r, 100) * 0.10

    # 接入量加分 (权重10)
    incoming = agent.get('incomingSessions', 0) or 0
    volume_score = min(incoming / 2, 100)
    score += volume_score * 0.10

    return round(min(score, 100), 1)


def get_score_grade(score: float) -> str:
    """根据评分返回等级"""
    if score >= 90:
        return '优秀'
    elif score >= 80:
        return '良好'
    elif score >= 70:
        return '合格'
    elif score >= 60:
        return '待改进'
    else:
        return '不合格'


# ==================== 3. 坐席会话分析 ====================

@app.route('/api/sessions/agent/<int:agent_id>', methods=['GET'])
def api_agent_sessions(agent_id):
    """获取指定坐席的会话列表（需要先通过export接口获取）"""
    date_str = request.args.get('date')
    start_ms, end_ms, actual_date = get_timestamp_range(date_str)

    api = QiyuAPI()
    if not api.app_key or not api.app_secret:
        return jsonify({"code": 401, "message": "请先配置 AppKey 和 AppSecret"})

    # 导出会话列表
    result = api.export_sessions(start_ms, end_ms)

    if result.get('code') != 200:
        # 如果导出接口不在可用时间，返回提示
        return jsonify({
            "code": result.get('code', 500),
            "message": result.get('message', '会话导出失败。注意：批量导出接口仅限凌晨2:00-6:00调用'),
            "hint": "如需查看会话详情，请提供具体的 session_id 进行单条查询"
        })

    sessions = result.get('data', result.get('message', []))
    if not isinstance(sessions, list):
        sessions = []

    # 过滤该坐席的会话
    agent_sessions = [s for s in sessions if s.get('staffId') == agent_id]

    return jsonify({
        "code": 200,
        "data": {
            "agentId": agent_id,
            "date": actual_date,
            "total": len(agent_sessions),
            "sessions": agent_sessions
        }
    })


@app.route('/api/sessions/<int:session_id>', methods=['GET'])
def api_session_detail(session_id):
    """获取单个会话详情（含消息内容）"""
    api = QiyuAPI()
    if not api.app_key or not api.app_secret:
        return jsonify({"code": 401, "message": "请先配置 AppKey 和 AppSecret"})

    # 获取会话详情（数据在 data 字段）
    detail = api.get_session_detail(session_id)
    if detail.get('code') != 200:
        return jsonify({"code": detail.get('code', 500),
                        "message": detail.get('message', '获取会话详情失败')})

    # 获取会话消息（数据在 data 字段，是数组）
    messages_result = api.get_session_messages(session_id)
    messages = []
    if messages_result.get('code') == 200:
        messages = messages_result.get('data', messages_result.get('message', []))
        if not isinstance(messages, list):
            messages = []

    # 会话详情数据在 data 字段
    session_data = detail.get('data', detail.get('message'))
    if not isinstance(session_data, dict):
        session_data = {}

    # 生成会话摘要
    summary = generate_session_summary(session_data, messages)

    # 格式化消息
    formatted_messages = []
    for msg in messages:
        m_type = msg.get('mType', 0)
        msg_type_name = {
            0: '系统消息', 1: '文本', 2: '图片', 3: '语音',
            4: '文件', 5: '视频', 6: '系统提示', 100: '自定义消息',
            110: '机器人答案', 111: '机器人反馈', 115: '富文本'
        }.get(m_type, f'类型{m_type}')

        formatted_messages.append({
            "id": msg.get('id'),
            "time": msg.get('time'),
            "from": '访客' if msg.get('from') == 1 else '客服',
            "staffName": msg.get('staffName', ''),
            "userName": msg.get('userName', ''),
            "msgType": m_type,
            "msgTypeName": msg_type_name,
            "content": msg.get('msg', ''),
            "isAutoReply": msg.get('autoReply') == 1,
            "status": msg.get('status', 1)
        })

    return jsonify({
        "code": 200,
        "data": {
            "session": session_data if session_data else detail.get('message'),
            "messages": formatted_messages,
            "summary": summary,
            "messageCount": len(formatted_messages)
        }
    })


# ==================== 3.5 会话缓存（凌晨自动抓取） ====================

@app.route('/api/sessions/cached', methods=['GET'])
def api_cached_sessions():
    """从缓存查询指定日期/坐席的会话列表"""
    date_str = request.args.get('date')
    staff_id = request.args.get('staffId', type=int)
    if not date_str:
        date_str = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')

    result = get_cached_sessions(date_str, staff_id)

    # 如果缓存不存在，返回提示
    if not result.get('cached'):
        return jsonify({
            "code": 404,
            "message": f"日期 {date_str} 的会话数据尚未缓存。缓存任务在每天凌晨2:00-6:00自动执行，也可手动触发。",
            "data": result
        })

    return jsonify({
        "code": 200,
        "data": result
    })


@app.route('/api/sessions/agent/<string:agent_id_str>/summary', methods=['GET'])
def api_agent_summary(agent_id_str):
    """生成指定坐席的会话概括总结，支持 deep 模式获取实际消息内容"""
    try:
        agent_id = int(agent_id_str)
    except ValueError:
        return jsonify({"code": 400, "message": "无效的坐席ID", "data": None})
    date_str = request.args.get('date')
    if not date_str:
        date_str = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    deep = request.args.get('deep', 'false').lower() == 'true'

    # 从缓存获取该坐席所有会话
    result = get_cached_sessions(date_str, agent_id)
    if not result.get('cached'):
        return jsonify({
            "code": 404,
            "message": f"日期 {date_str} 的会话数据尚未缓存",
            "data": None
        })

    sessions = result.get('sessions', [])
    if not sessions:
        return jsonify({
            "code": 200,
            "data": {"summary": "该坐席在 {date_str} 没有会话记录".format(date_str=date_str), "stats": {}, "topics": []}
        })

    # 基础元数据分析（总是执行）
    summary_data = generate_agent_summary(sessions, agent_id, date_str)

    # 深度分析：采样会话获取消息内容
    if deep:
        api = QiyuAPI()
        if api.app_key and api.app_secret:
            deep_data = analyze_agent_content_deeply(sessions, agent_id, api)
            summary_data['deepAnalysis'] = deep_data
            # 将内容发现融入总结（追加为新的 section）
            if deep_data.get('analyzedCount', 0) > 0:
                deep_parts = []
                if deep_data.get('commonIssues'):
                    issues_text = '、'.join([i['issue'] for i in deep_data['commonIssues'][:5]])
                    deep_parts.append(f"会话内容涉及的常见问题包括：{issues_text}")
                if deep_data.get('userComplaints'):
                    complaints_text = '、'.join(deep_data['userComplaints'][:3])
                    deep_parts.append(f"用户高频反馈：{complaints_text}")
                if deep_data.get('resolutionPatterns'):
                    patterns_text = '、'.join(deep_data['resolutionPatterns'][:3])
                    deep_parts.append(f"常见解决方案：{patterns_text}")
                if deep_parts:
                    deep_section = {"title": "📝 会话内容分析", "icon": "deep", "content": "；".join(deep_parts) + "。"}
                    summary_data.setdefault('sections', [])
                    summary_data['sections'].append(deep_section)
                    summary_data['summary'] += "\n\n📝 **会话内容分析**：" + "；".join(deep_parts) + "。"
        else:
            summary_data['deepAnalysis'] = {
                "analyzedCount": 0,
                "totalSampled": 0,
                "commonIssues": [],
                "topPhrases": [],
                "userComplaints": [],
                "resolutionPatterns": ["深度分析不可用：缺少七鱼API凭证配置"],
                "sampleSessions": [],
                "fetchErrors": ["AppKey或AppSecret未配置"],
                "analysisMethod": "深度分析（不可用）"
            }

    return jsonify({"code": 200, "data": summary_data})


def generate_agent_summary(sessions: list, agent_id: int, date_str: str) -> dict:
    """基于会话元数据生成坐席级别的概括总结"""
    total = len(sessions)
    if total == 0:
        return {"summary": "无会话数据", "stats": {}, "topics": []}

    # ---- 基础统计 ----
    valid_count = sum(1 for s in sessions if s.get('isValid') == 1)
    invalid_count = total - valid_count
    valid_rate = round(valid_count / total * 100, 1) if total > 0 else 0

    # 会话类型
    stype_map = {0: '正常', 1: '留言', 2: '留言', 3: '超时'}
    stypes = {}
    for s in sessions:
        t = stype_map.get(s.get('sType', 0), '未知')
        stypes[t] = stypes.get(t, 0) + 1

    # ---- 解决率 ----
    resolved = sum(1 for s in sessions if s.get('status') == 1)
    unresolved = sum(1 for s in sessions if s.get('status') == 0)
    solving = sum(1 for s in sessions if s.get('status') == 2)
    resolve_rate = round(resolved / total * 100, 1) if total > 0 else 0

    # 访客自主评价解决率
    user_resolved = sum(1 for s in sessions if s.get('userResolvedStatus') == 1)

    # ---- 满意度 ----
    evaluated = [s for s in sessions if isinstance(s.get('evaluation'), (int, float)) and s.get('evaluation', 0) > 0]
    satis_text = {5: '非常满意', 4: '满意', 3: '一般', 2: '不满意', 1: '非常不满意'}
    satis_count = {}
    for s in evaluated:
        raw_score = int(s.get('evaluation', 0))
        # 归一化：七鱼 API 可能返回 0-5 或 0-100 两种尺度
        if raw_score > 5:
            raw_score = max(1, min(5, round(raw_score / 20)))
        satis_count[raw_score] = satis_count.get(raw_score, 0) + 1
    # 用归一化后的分数计算均值（保持 0-5 尺度用于显示）
    total_satis = 0
    for s in evaluated:
        raw_score = int(s.get('evaluation', 0))
        if raw_score > 5:
            raw_score = max(1, min(5, round(raw_score / 20)))
        total_satis += raw_score
    avg_satisfaction = round(total_satis / len(evaluated), 2) if evaluated else 0
    satisfaction_rate = round(len([s for s in evaluated if (s.get('evaluation', 0) > 5 and round(s.get('evaluation', 0) / 20) >= 4) or (s.get('evaluation', 0) <= 5 and s.get('evaluation', 0) >= 4)]) / len(evaluated) * 100, 1) if evaluated else 0

    # ---- 消息量 ----
    staff_msgs = sum(s.get('staffMessageCount', 0) or 0 for s in sessions)
    user_msgs = sum(s.get('userMessageCount', 0) or 0 for s in sessions)
    avg_staff_msgs = round(staff_msgs / total, 1) if total > 0 else 0
    avg_user_msgs = round(user_msgs / total, 1) if total > 0 else 0

    # ---- 响应速度 ----
    first_reply_costs = [s.get('firstReplyCost', 0) or 0 for s in sessions if s.get('firstReplyCost', 0) and s.get('firstReplyCost', 0) > 0]
    avg_first_reply = round(sum(first_reply_costs) / len(first_reply_costs) / 1000, 1) if first_reply_costs else 0

    # ---- 会话时长 ----
    durations = []
    for s in sessions:
        d = s.get('sessionDuration')
        if d and d != '':
            try:
                durations.append(int(d) / 1000)
            except (ValueError, TypeError):
                pass
    avg_duration = round(sum(durations) / len(durations) / 60, 1) if durations else 0  # 分钟

    # ---- 分类分析 ----
    category_count = {}
    for s in sessions:
        cat = s.get('category', '') or '未分类'
        if cat:
            category_count[cat] = category_count.get(cat, 0) + 1
    top_categories = sorted(category_count.items(), key=lambda x: -x[1])[:10]
    # 将"未分类"和"未知"合并去重
    merged_cats = {}
    for cat, cnt in top_categories:
        if cat in ('未分类', '未知'):
            merged_cats['未分类'] = merged_cats.get('未分类', 0) + cnt
        else:
            merged_cats[cat] = cnt

    # ---- 时段分析 ----
    hour_dist = {}
    for s in sessions:
        st = s.get('startTime', 0)
        if st:
            try:
                hour = int((datetime.datetime.fromtimestamp(st / 1000)).hour)
                hour_dist[hour] = hour_dist.get(hour, 0) + 1
            except Exception:
                pass
    peak_hours = sorted(hour_dist.items(), key=lambda x: -x[1])[:3]

    # ---- 常见问题关键词 ----
    # 从 categoryDetail、transferHumanFailReason、startReason 提取关键词
    keyword_freq = {}
    for s in sessions:
        detail = s.get('categoryDetail', '') or ''
        fail_reason = s.get('transferHumanFailReason', '') or ''
        start_reason = s.get('startReason', '') or ''
        combined = f"{detail} {fail_reason} {start_reason}"

        for keyword in ['转人工失败', '无效会话', '账号问题', '充值', 'BUG', '排队', '超时',
                         '不在线', '机器人', '直接进入', '转出', '接管', '举报', '投诉']:
            if keyword in combined:
                keyword_freq[keyword] = keyword_freq.get(keyword, 0) + 1
    top_keywords = sorted(keyword_freq.items(), key=lambda x: -x[1])[:8]

    # ---- 细粒度问题标签（从 categoryDetail 拆分）----
    tag_freq = {}
    for s in sessions:
        detail = s.get('categoryDetail', '') or ''
        if detail:
            # categoryDetail 格式如 "新城堡4 / 游戏攻略 / 据点查询"
            tags = [t.strip() for t in detail.split('/') if t.strip()]
            for tag in tags:
                if len(tag) >= 2:
                    tag_freq[tag] = tag_freq.get(tag, 0) + 1

    # 合并相似标签
    tag_aliases = {
        '账号问题': ['账号', '账号查询', '账号找回', '账号注销', '账号换绑'],
        '游戏攻略': ['游戏攻略', '攻略', '玩法'],
        '充值问题': ['充值', '充值问题', '付费'],
        'BUG反馈': ['BUG', 'BUG反馈', 'bug'],
        '数据异常': ['数据查询', '数据问题'],
        '账号注册': ['账号注册', '注册', '绑定失败'],
        '举报投诉': ['举报', '投诉', '举报投诉'],
    }
    merged_tags = {}
    unmapped = set(tag_freq.keys())
    for main_tag, aliases in tag_aliases.items():
        tag_total = 0
        for alias in aliases:
            if alias in tag_freq:
                tag_total += tag_freq[alias]
                unmapped.discard(alias)
            # 部分匹配
            for t in list(unmapped):
                if alias in t:
                    tag_total += tag_freq[t]
                    unmapped.discard(t)
        if tag_total > 0:
            merged_tags[main_tag] = tag_total
    for t in unmapped:
        merged_tags[t] = tag_freq[t]

    top_tags = sorted(merged_tags.items(), key=lambda x: -x[1])[:12]

    # ---- 生成结构化总结段落 ----
    agent_name = sessions[0].get('staffName', f'坐席{agent_id}') if sessions else f'坐席{agent_id}'
    if not agent_name:
        agent_name = f'坐席{agent_id}'

    summary_sections = []

    # 段落1：概览
    overview_items = [f"<strong>{agent_name}</strong> 在 {date_str} 共处理 <strong>{total} 通</strong>会话"]
    if valid_count > 0:
        overview_items.append(f"其中有效会话 <strong>{valid_count} 通</strong>（{valid_rate}%）")
    if resolved > 0:
        overview_items.append(f"已解决 <strong>{resolved} 通</strong>")
    if unresolved > 0:
        overview_items.append(f"未解决 {unresolved} 通")
    if solving > 0:
        overview_items.append(f"解决中 {solving} 通")
    summary_sections.append({"title": "📋 会话概览", "icon": "overview", "content": "，".join(overview_items) + "。"})

    # 段落2：满意度与效率
    perf_items = []
    if evaluated:
        perf_items.append(f"收到 <strong>{len(evaluated)} 条</strong>评价，平均满意度 <strong>{avg_satisfaction} 分</strong>，好评率 <strong>{satisfaction_rate}%</strong>")
    else:
        perf_items.append("该时段未收到用户评价")
    if avg_first_reply > 0:
        perf_items.append(f"平均首次响应 <strong>{avg_first_reply} 秒</strong>")
    if avg_duration > 0:
        perf_items.append(f"平均会话时长 <strong>{avg_duration} 分钟</strong>")
    if avg_staff_msgs > 0:
        perf_items.append(f"平均客服回复 <strong>{avg_staff_msgs} 条</strong>，访客发言 <strong>{avg_user_msgs} 条</strong>")
    if perf_items:
        summary_sections.append({"title": "⏱ 服务效率", "icon": "efficiency", "content": "，".join(perf_items) + "。"})

    # 段落3：高峰时段
    if peak_hours:
        peak_text = '、'.join([f"<strong>{h}:00-{h+1}:00</strong>（{c}通）" for h, c in peak_hours])
        summary_sections.append({"title": "🔥 高峰时段", "icon": "peak", "content": f"会话主要集中在 {peak_text}。"})

    # 段落4：问题类型
    if merged_cats:
        top_cat_text = '、'.join([f"<strong>{cat}</strong>（{cnt}通）" for cat, cnt in list(merged_cats.items())[:5]])
        summary_sections.append({"title": "📂 主要问题类型", "icon": "category", "content": f"涉及 {top_cat_text}。"})

    # 段落5：细粒度标签
    if top_tags:
        top_tag_text = '、'.join([f"<strong>{tag}</strong>（{cnt}次）" for tag, cnt in top_tags[:6]])
        summary_sections.append({"title": "🏷️ 高频问题标签", "icon": "tag", "content": f"{top_tag_text}。"})

    # 突出问题
    flags = []
    if unresolved > valid_count * 0.5 and valid_count > 3:
        flags.append(f"⚠️ 解决率偏低（{resolve_rate}%）")
    if evaluated and avg_satisfaction < 3:
        flags.append(f"⚠️ 满意度偏低（{avg_satisfaction} 分）")
    if avg_first_reply > 60:
        flags.append(f"⏱ 平均首响超过 1 分钟（{avg_first_reply} 秒）")
    if keyword_freq.get('转人工失败', 0) > total * 0.3:
        flags.append(f"⚠️ 转人工失败比例较高（{keyword_freq.get('转人工失败', 0)} 通）")

    # 兼容旧版 summary 字段
    summary_text = "；".join([s["content"] for s in summary_sections])

    return {
        "agentName": agent_name,
        "agentId": agent_id,
        "date": date_str,
        "summary": summary_text,
        "sections": summary_sections,
        "flags": flags,
        "stats": {
            "total": total,
            "validCount": valid_count,
            "invalidCount": invalid_count,
            "validRate": valid_rate,
            "resolved": resolved,
            "unresolved": unresolved,
            "solving": solving,
            "resolveRate": resolve_rate,
            "userResolvedCount": user_resolved,
            "evaluationCount": len(evaluated),
            "avgSatisfaction": avg_satisfaction,
            "satisfactionRate": satisfaction_rate,
            "satisfactionDist": satis_count,
            "avgStaffMsgs": avg_staff_msgs,
            "avgUserMsgs": avg_user_msgs,
            "avgFirstReplySec": avg_first_reply,
            "avgDurationMin": avg_duration,
            "sessionTypes": stypes,
            "topCategories": [{"name": c, "count": n} for c, n in list(merged_cats.items())[:8]],
            "peakHours": [{"hour": h, "count": c} for h, c in peak_hours[:6]],
            "topKeywords": [{"keyword": k, "count": c} for k, c in top_keywords],
            "topTags": [{"tag": t, "count": c} for t, c in top_tags[:12]],
        }
    }


def analyze_agent_content_deeply(sessions: list, agent_id: int, api) -> dict:
    """采样坐席的会话，获取消息内容进行深度分析"""
    import time as time_mod
    import re
    from collections import Counter

    # 选择代表性样本：优先选有分类的、有消息的、已解决的
    scored = []
    for s in sessions:
        score = 0
        if s.get('category') and s.get('category') not in ('未知', '未分类'):
            score += 3
        msg_count = (s.get('staffMessageCount', 0) or 0) + (s.get('userMessageCount', 0) or 0)
        if msg_count > 5:
            score += min(msg_count // 5, 5)  # 消息多 => 含金量高
        if s.get('isValid') == 1:
            score += 2
        if s.get('status') == 1:  # 已解决
            score += 1
        scored.append((score, s))

    # 按得分排序，取前8条 + 随机2条作为样本
    scored.sort(key=lambda x: -x[0])
    sample_sessions = [s for _, s in scored[:8]]
    # 额外加2条低分样本以覆盖多样性
    if len(scored) > 8:
        low_scored = scored[8:]
        # 每隔N条取1条
        step = max(1, len(low_scored) // 2)
        for i in range(0, len(low_scored), step):
            if len(sample_sessions) >= 10:
                break
            sample_sessions.append(low_scored[i][1])

    all_user_msgs = []  # 用户消息内容
    all_session_summaries = []  # 每个会话的简短摘要
    analyzed_count = 0

    fetch_errors = []  # 记录获取消息过程中的错误
    for s in sample_sessions:
        sid = s.get('id')
        if not sid:
            continue
        try:
            time_mod.sleep(0.5)  # 控制 API 频率
            msg_result = api.get_session_messages(sid)
            if msg_result.get('code') != 200:
                err_msg = msg_result.get('message', '未知错误')
                fetch_errors.append(f"会话{sid}: API返回code={msg_result.get('code')}, {err_msg}")
                continue

            messages = msg_result.get('data', [])
            if not messages:
                fetch_errors.append(f"会话{sid}: 无消息内容(可能是纯图片/系统消息)")
                continue

            analyzed_count += 1

            # 提取用户消息（from=1 是访客）
            user_msgs = [m.get('msg', '') for m in messages
                        if m.get('from') == 1 and isinstance(m.get('msg'), str) and len(m.get('msg', '').strip()) > 2]
            if user_msgs:
                all_user_msgs.extend(user_msgs)

            # 提取客服消息用作解决模式分析
            staff_msgs = [m.get('msg', '') for m in messages
                         if m.get('from') != 1 and isinstance(m.get('msg'), str) and len(m.get('msg', '').strip()) > 5]

            # 为该会话生成简短摘要
            user_text = ' '.join(user_msgs[:3])[:150]  # 前3条用户消息
            cat = s.get('category', '') or ''
            cd = s.get('categoryDetail', '') or ''
            cat_text = f"{cat} - {cd}" if cat and cat not in ('未知', '未分类') else '未分类'
            all_session_summaries.append({
                "sessionId": sid,
                "category": cat_text,
                "userSample": user_text if user_text else '（无文本消息）',
                "msgCount": len(messages),
                "userMsgCount": len(user_msgs),
                "staffMsgCount": len(staff_msgs),
                "resolved": s.get('status') == 1
            })

        except Exception as e:
            fetch_errors.append(f"会话{sid}: 异常 - {str(e)[:100]}")
            continue

    if analyzed_count == 0:
        # 返回错误信息而不是 None，让前端能看到问题
        err_summary = '; '.join(fetch_errors[:3]) if fetch_errors else '未能获取任何会话消息内容'
        return {
            "analyzedCount": 0,
            "totalSampled": len(sample_sessions),
            "commonIssues": [],
            "topPhrases": [],
            "userComplaints": [],
            "resolutionPatterns": [f"深度分析失败：{err_summary}"],
            "sampleSessions": [],
            "fetchErrors": fetch_errors[:5],
            "analysisMethod": "基于会话消息内容的深度分析（失败）"
        }

    # ---- 分析用户消息，提取常见问题 ----
    # 使用关键词匹配识别问题类型
    issue_patterns = [
        ("账号找回/换绑", ['账号找', '密码', '换绑', '绑定', '手机号', '验证码', '注销']),
        ("充值/付费问题", ['充值', '付费', '扣款', '支付', '退款', '购买', '订单']),
        ("游戏攻略/玩法咨询", ['攻略', '怎么玩', '怎么打', '怎么过', '教程', '新手', '指南', '技巧']),
        ("数据/道具问题", ['数据', '道具', '装备', '角色', '丢失', '没收到', '不见了', '消失']),
        ("BUG/异常反馈", ['bug', '错误', '异常', '闪退', '卡', '掉线', '无法', '报错', '白屏', '黑屏']),
        ("举报/投诉", ['举报', '投诉', '外挂', '作弊', '挂机', '骂人']),
        ("活动/福利咨询", ['活动', '福利', '奖励', '领取', '兑换', '礼包', '码']),
        ("登录/启动问题", ['登录', '登不上', '进不去', '启动', '闪退', '黑屏', '更新']),
        ("功能使用咨询", ['怎么', '在哪里', '如何', '设置', '功能', '按钮', '界面']),
        ("账号安全/被封", ['封号', '封禁', '被盗', '安全', '异地', '盗号']),
    ]

    issue_matches = {}
    for msg in all_user_msgs:
        for issue_name, patterns in issue_patterns:
            for pat in patterns:
                if pat.lower() in msg.lower():
                    issue_matches[issue_name] = issue_matches.get(issue_name, 0) + 1
                    break

    common_issues = [
        {"issue": name, "count": cnt}
        for name, cnt in sorted(issue_matches.items(), key=lambda x: -x[1])[:8]
    ]

    # ---- 提取用户高频词汇/短语 ----
    # 简单分词（按中文标点拆分）
    all_text = ' '.join(all_user_msgs)
    # 提取2-4字短语
    phrases = Counter()
    for msg in all_user_msgs:
        # 提取关键短句
        for phrase in ['转人工', '在吗', '你好', '谢谢', '等一下', '没有人', '没有用',
                       '出问题了', '打不开', '怎么办', '为什么', '帮我', '解决',
                       '充了钱', '没收到', '找不到', '进不去', '卡住了',
                       '闪退', '黑屏', '更新不了', '绑定失败']:
            if phrase in msg:
                phrases[phrase] += 1
    top_phrases = [{"phrase": p, "count": c} for p, c in phrases.most_common(10)]

    # ---- 识别用户情绪/诉求模式 ----
    complaints = []
    negative_patterns = [
        ('用户表达不满', ['不满意', '差评', '投诉', '什么垃圾', '坑', '骗', '怎么这样']),
        ('用户催促回复', ['快点', '能不能快点', '等了很久', '怎么还没好']),
        ('用户要求赔偿', ['赔偿', '补偿', '还我', '退钱', '退款']),
        ('用户困惑/无助', ['不知道', '不会弄', '看不懂', '帮帮我', '教教我']),
    ]
    negative_count = Counter()
    for msg in all_user_msgs:
        for label, patterns in negative_patterns:
            for pat in patterns:
                if pat in msg:
                    negative_count[label] += 1
                    break
    complaints = [{"type": label, "count": cnt}
                  for label, cnt in negative_count.most_common(5) if cnt > 0]

    # ---- 识别客服解决模式 ----
    resolution_patterns = []
    # 从客服消息中提取模式
    resolution_keywords = [
        ('引导自助操作', ['您可以', '试试', '点击', '进入', '打开', '设置']),
        ('提供补偿方案', ['补偿', '补发', '给您', '赠送', '发放']),
        ('转交其他部门', ['反馈给', '技术', '专员', '跟进', '核实']),
        ('直接解决问题', ['已处理', '已解决', '好了', '完成了', '成功']),
        ('要求提供信息', ['请问', '告诉我', '提供', '截图', 'ID']),
    ]
    resolution_count = Counter()
    for s_summary in all_session_summaries:
        # 检查该会话的客服消息
        sess = next((x for x in sample_sessions if x.get('id') == s_summary['sessionId']), None)
        if sess:
            cat_d = (sess.get('categoryDetail', '') or '')
            for label, patterns in resolution_keywords:
                for pat in patterns:
                    if pat in cat_d or (sess.get('closeReason') and str(pat) in str(sess.get('closeReason', ''))):
                        resolution_count[label] += 1
                        break

    # 如果从 data 中没有找到，尝试从消息
    if not resolution_count:
        resolution_patterns = [
            f"共分析 {analyzed_count} 条会话，用户核心关注：{'、'.join([i['issue'] for i in common_issues[:3]]) if common_issues else '多样化问题'}"
        ]
    else:
        resolution_patterns = [f"{label}（{cnt}次）" for label, cnt in resolution_count.most_common(4)]

    return {
        "analyzedCount": analyzed_count,
        "totalSampled": len(sample_sessions),
        "commonIssues": common_issues,
        "topPhrases": top_phrases,
        "userComplaints": [c['type'] for c in complaints] if complaints else [],
        "resolutionPatterns": resolution_patterns,
        "sampleSessions": all_session_summaries[:5],  # 挂载前5个样本
        "analysisMethod": "基于消息内容的语义分析"
    }


@app.route('/api/sessions/cache/status', methods=['GET'])
def api_cache_status():
    """查看缓存状态"""
    date_str = request.args.get('date')
    status = get_cache_status(date_str)
    status['isExportWindow'] = is_export_window()
    status['now'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return jsonify({"code": 200, "data": status})


@app.route('/api/sessions/cache/trigger', methods=['POST'])
def api_trigger_cache():
    """手动触发会话缓存（用于测试或手动执行）"""
    data = request.json or {}
    date_str = data.get('date')
    if not date_str:
        date_str = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')

    start_ms, end_ms, _ = get_timestamp_range(date_str)

    api = QiyuAPI()
    if not api.app_key or not api.app_secret:
        return jsonify({"code": 401, "message": "请先配置 AppKey 和 AppSecret"})

    result = api.export_sessions(start_ms, end_ms)

    if result.get('code') != 200:
        err_msg = result.get('message', '未知错误')
        err_code = result.get('code', 500)
        # 判断错误类型给出友好提示
        hint = ''
        if err_code == 14400 or 'Forbidden' in str(err_msg):
            hint = '当前企业账号可能不支持批量会话导出接口，请联系七鱼客服确认是否开通此功能。'
        elif '凌晨' in str(err_msg) or '2点' in str(err_msg) or '6点' in str(err_msg) or 'time' in str(err_msg).lower():
            hint = '批量导出接口仅限凌晨2:00-6:00调用，当前不在可用时段。将在凌晨自动执行。'
        return jsonify({
            "code": err_code,
            "message": str(err_msg),
            "hint": hint
        })

    sessions = result.get('data', result.get('message', []))
    if not isinstance(sessions, list):
        # 可能包装在 result 中
        if isinstance(sessions, dict):
            for k in ['result', 'list', 'items', 'data']:
                if k in sessions and isinstance(sessions[k], list):
                    sessions = sessions[k]
                    break
            else:
                sessions = []
        else:
            sessions = []

    count = store_sessions(date_str, sessions)
    return jsonify({
        "code": 200,
        "message": f"成功缓存 {count} 条会话数据",
        "data": {"date": date_str, "count": count}
    })


def run_session_cache_job():
    """后台定时任务：在凌晨2:00-6:00抓取前一天的会话数据"""
    import time as time_mod
    last_date = None

    while True:
        try:
            now = datetime.datetime.now()
            # 只在凌晨2:00-2:10之间执行一次
            if now.hour == 2 and now.minute < 10:
                yesterday = (now - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
                if yesterday != last_date:
                    api = QiyuAPI()
                    if api.app_key and api.app_secret:
                        try:
                            start_ms, end_ms, _ = get_timestamp_range(yesterday)
                            result = api.export_sessions(start_ms, end_ms)
                            if result.get('code') == 200:
                                sessions = result.get('data', result.get('message', []))
                                if isinstance(sessions, dict):
                                    for k in ['result', 'list', 'items', 'data']:
                                        if k in sessions and isinstance(sessions[k], list):
                                            sessions = sessions[k]
                                            break
                                if isinstance(sessions, list):
                                    store_sessions(yesterday, sessions)
                            else:
                                pass
                        except Exception:
                            pass
                    last_date = yesterday
            time_mod.sleep(60)  # 每分钟检查一次
        except Exception:
            time_mod.sleep(300)  # 出错了等5分钟再试


def generate_session_summary(session: dict, messages: list) -> dict:
    """生成会话概括总结"""
    if not session:
        return {"overview": "无会话数据"}

    # 基础信息
    start_time = session.get('startTime', 0)
    end_time = session.get('endTime', 0)
    duration_sec = (end_time - start_time) / 1000 if end_time and start_time else 0

    # 统计消息
    visitor_msgs = [m for m in messages if m.get('from') == 1]
    staff_msgs = [m for m in messages if m.get('from') == 0]

    # 提取非系统消息的纯文本内容
    text_msgs = []
    for m in messages:
        if m.get('mType') == 1 and m.get('msg'):  # 文本消息
            text_msgs.append({
                "from": '访客' if m.get('from') == 1 else '客服',
                "content": m.get('msg', ''),
                "time": m.get('time', 0)
            })

    # 分析会话内容关键词
    all_text = ' '.join([m['content'] for m in text_msgs])

    # 判断会话主题
    topics = []
    keywords_map = {
        '充值问题': ['充值', '充钱', '付款', '支付', '到账', '扣款', '点券', '钻石', '元宝'],
        '账号问题': ['账号', '密码', '登录', '找回', '绑定', '解绑', '验证', '冻结'],
        '游戏Bug': ['bug', '卡住', '闪退', '掉线', '报错', '异常', '卡顿', '黑屏'],
        '活动咨询': ['活动', '礼包', '奖励', '领取', '兑换', '福利', '限时'],
        '投诉建议': ['投诉', '举报', '违规', '作弊', '外挂', '建议', '反馈'],
        '装备道具': ['装备', '道具', '武器', '皮肤', '时装', '背包', '丢失'],
        '玩法攻略': ['攻略', '怎么', '如何', '教程', '打法', '阵容', '配队'],
    }

    for topic, keywords in keywords_map.items():
        if any(kw in all_text for kw in keywords):
            topics.append(topic)

    if not topics:
        topics = ['一般咨询']

    # 满意度
    evaluation = session.get('evaluation', 0)
    eval_text = ''
    if isinstance(evaluation, (int, float)) and evaluation > 0:
        if evaluation == 5:
            eval_text = '非常满意'
        elif evaluation >= 4:
            eval_text = '满意'
        elif evaluation >= 3:
            eval_text = '一般'
        elif evaluation >= 2:
            eval_text = '不满意'
        else:
            eval_text = '非常不满意'

    # 会话特征
    features = []
    if len(visitor_msgs) > len(staff_msgs) * 3:
        features.append('访客消息量远大于客服，可能有较多追问')
    if duration_sec > 1800:
        features.append('会话时间较长（超过30分钟）')
    if duration_sec < 60 and len(messages) < 5:
        features.append('简短会话，快速解决')
    first_reply = session.get('firstReplyCost', 0)
    if first_reply and first_reply > 120000:
        features.append(f'首次响应较慢（{round(first_reply/1000)}秒）')
    redirect_type = session.get('relatedType', 0)
    if redirect_type and redirect_type > 0:
        redirect_map = {1: '从机器人转接', 2: '机器人转人工', 3: '历史会话', 4: '客服间转接', 5: '被接管'}
        features.append(f'转接会话（{redirect_map.get(redirect_type, "未知")})')

    # 解决状态
    status_map = {0: '未解决', 1: '已解决', 2: '解决中'}
    resolve_status = status_map.get(session.get('status', 0), '未知')

    return {
        "overview": f"会话时长约{round(duration_sec/60, 1)}分钟，共{len(messages)}条消息",
        "topics": topics,
        "visitorMsgCount": len(visitor_msgs),
        "staffMsgCount": len(staff_msgs),
        "durationSeconds": round(duration_sec, 1),
        "evaluation": eval_text if eval_text else '未评价',
        "evaluationScore": evaluation,
        "resolveStatus": resolve_status,
        "features": features,
        "firstReplyCost": session.get('firstReplyCost', 0),
        "category": session.get('category', ''),
        "categoryDetail": session.get('categoryDetail', ''),
        "textDialogues": text_msgs[-10:] if len(text_msgs) > 10 else text_msgs  # 最近10条文本对话
    }


# ==================== 4. 质量报告 ====================

@app.route('/api/report/daily', methods=['GET'])
def api_daily_report():
    """生成每日整体客服服务质量报告"""
    import time as time_mod
    import sys
    date_str = request.args.get('date')
    days = request.args.get('days', type=int)
    start_ms, end_ms, actual_date = get_timestamp_range(date_str, days=days)

    api = QiyuAPI()
    if not api.app_key or not api.app_secret:
        return jsonify({"code": 401, "message": "请先配置 AppKey 和 AppSecret"})

    # 检查缓存（报告缓存15分钟，周缓存20分钟）
    cache_key = f"report_{actual_date.replace(' ~ ', '_to_')}"
    cached = cache.get(cache_key)
    cache_ttl = 1200 if days and days > 1 else 600
    if cached and (time_mod.time() - cached['ts'] < cache_ttl):
        print(f"[CACHE] Returning cached report for {actual_date}")
        return jsonify({"code": 200, "data": cached['data']})

    # 获取总览数据 - 优先复用 overview 端点缓存，避免频繁调用触发 14009
    overview = {}
    overview_error = None
    overview_cache_key = get_cache_key('overview', actual_date.replace(' ~ ', '_to_'))
    cached_overview = cache.get(overview_cache_key)
    if cached_overview and (time_mod.time() - cached_overview[0] < 600):  # 10分钟内缓存有效
        overview_data = cached_overview[1].get('data', {})
        overview = overview_data.get('raw', {})
    else:
        for attempt in range(3):
            overview_result = api.get_overview(start_ms, end_ms)
            if overview_result.get('code') == 200:
                overview = overview_result.get('message', {})
                break
            elif overview_result.get('code') == 14009 and attempt < 2:
                time_mod.sleep(3)
                continue
            else:
                overview_error = f"总览API返回 code={overview_result.get('code')}: {overview_result.get('message', '')}"
                break

    if overview_error:
        return jsonify({"code": 500, "message": overview_error})

    time_mod.sleep(2)

    # 获取坐席数据 - 优先使用共享原始缓存
    cache_key_agents = get_cache_key('agents', actual_date.replace(' ~ ', '_to_'))
    raw = raw_cache.get(cache_key_agents)
    if raw and (time_mod.time() - raw['ts'] < 600):  # 缓存10分钟有效
        workload_result = raw['workload']
        quality_result = raw['quality']
        satisfaction_result = raw['satisfaction']
    else:
        workload_result = api_call_with_retry(api.get_staff_workload, start_ms, end_ms)
        time_mod.sleep(2)
        quality_result = api_call_with_retry(api.get_staff_quality, start_ms, end_ms)
        time_mod.sleep(2)
        satisfaction_result = api_call_with_retry(api.get_staff_satisfaction, start_ms, end_ms)

    workload_data = normalize_to_list(workload_result.get('message', workload_result.get('data'))) if workload_result.get('code') == 200 else []
    quality_data = normalize_to_list(quality_result.get('message', quality_result.get('data'))) if quality_result.get('code') == 200 else []
    satisfaction_data = normalize_to_list(satisfaction_result.get('message', satisfaction_result.get('data'))) if satisfaction_result.get('code') == 200 else []

    if not isinstance(workload_data, list):
        workload_data = []
    if not isinstance(quality_data, list):
        quality_data = []
    if not isinstance(satisfaction_data, list):
        satisfaction_data = []

    # 构建报告
    report = {
        "reportDate": actual_date,
        "days": days or 1,
        "generatedAt": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "overall": build_overall_section(overview),
        "agentEvaluations": build_agent_evaluations(workload_data, quality_data, satisfaction_data),
        "keyFindings": [],
        "recommendations": [],
        "scoreSummary": {}
    }

    # 生成关键发现和建议
    report['keyFindings'], report['recommendations'], report['scoreSummary'] = \
        generate_report_insights(report['overall'], report['agentEvaluations'])

    # 写入缓存
    cache[cache_key] = {'data': report, 'ts': time_mod.time()}

    return jsonify({"code": 200, "data": report})


def build_overall_section(overview: dict) -> dict:
    """构建整体概览部分"""
    def to_pct(v):
        if v is None or v < 0:
            return 0
        return round(v * 100, 1) if isinstance(v, float) and v <= 1 else round(float(v), 1)

    return {
        "totalSessions": overview.get('sessions', 0),
        "effectiveSessions": overview.get('effectSessions', 0),
        "answerRatio": to_pct(overview.get('answerRatio', 0)),
        "satisfactionRatio": to_pct(overview.get('satisfactionRatio', 0)),
        "avgFirstRespTime": overview.get('avgFirstRespTime', 0),
        "avgRespTime": overview.get('avgRespTime', 0),
        "avgSessionTime": overview.get('avgTime', 0),
        "oneOffRatio": to_pct(overview.get('oneOffRatio', 0)),
        "totalMessages": overview.get('messages', 0),
        "loginStaffCount": overview.get('loginStaffCount', 0),
        "queueCount": overview.get('queueCount', 0),
        "maxQueueTime": overview.get('maxQueueTime', 0),
        "evaRatio": to_pct(overview.get('evaRatio', 0)),
        "totalConsultCount": overview.get('totalConsultCount', 0),
        "visitCount": overview.get('visit', 0),
    }


def build_agent_evaluations(workload: list, quality: list, satisfaction: list) -> list:
    """构建每个坐席的评价"""
    agents = {}

    def safe_pct(v):
        """将 0-1 小数转为百分制，-1 保持为 -1（无数据）"""
        try:
            f = float(v)
            if f < 0:
                return -1.0
            return round(f * 100, 2) if f <= 1.0 else round(f, 2)
        except (ValueError, TypeError):
            return -1.0

    for item in workload:
        aid = item.get('id')
        if not aid:
            continue
        agents[aid] = {
            "id": aid, "name": item.get('name', ''),
            "incomingSessions": item.get('sessionsCount', 0),
            "validSessions": item.get('validSessionCount', 0),
            "noReplyRatio": safe_pct(item.get('noReplyRatio', 0)),
            "totalSessions": item.get('totalSessionCount', 0),
        }

    for item in quality:
        aid = item.get('id')
        if not aid:
            continue
        if aid not in agents:
            agents[aid] = {"id": aid, "name": item.get('name', '')}
        agents[aid].update({
            "avgRespTime": item.get('avgRespTime', 0),       # 毫秒，保持原值
            "avgFirstRespTime": item.get('avgFirstRespTime', 0),  # 毫秒，保持原值
            "replyRatio": safe_pct(item.get('replyRatio', -1)),
            "avgSessionTime": item.get('avgTime', 0),
            "evaRatio": safe_pct(item.get('evaRatio', -1)),
            "satisfactionRatio": safe_pct(item.get('satisfactionRatio', -1)),
            "oneOffRatio": safe_pct(item.get('oneOffRatio', -1)),
            "answerReplyRatio": safe_pct(item.get('answerReplyRatio', -1)),
        })

    for item in satisfaction:
        aid = item.get('id')
        if not aid:
            continue
        if aid not in agents:
            agents[aid] = {"id": aid, "name": item.get('name', '')}
        agents[aid].update({
            "verySatisfiedCount": item.get('verySatisfiedCount', 0),
            "satisfiedCount": item.get('satisfiedCount', 0),
            "normalCount": item.get('normalSatisfiedCount', 0),
            "notSatisfiedCount": item.get('notSatisfiedCount', 0),
            "veryNotSatisfiedCount": item.get('veryNotSatisfiedCount', 0),
        })

    evaluations = []
    for agent in agents.values():
        score = calculate_agent_score(agent)
        grade = get_score_grade(score)
        strengths, weaknesses = analyze_agent_performance(agent)

        evaluations.append({
            **agent,
            "score": score,
            "grade": grade,
            "strengths": strengths,
            "weaknesses": weaknesses,
            "suggestion": generate_agent_suggestion(grade, strengths, weaknesses)
        })

    evaluations.sort(key=lambda x: x['score'], reverse=True)
    return evaluations


def analyze_agent_performance(agent: dict) -> tuple:
    """分析坐席表现优缺点（比率字段已是百分制0-100，-1=无数据）"""
    strengths = []
    weaknesses = []

    def pf(v):
        """安全转float，-1视为0"""
        try:
            f = float(v)
            return max(f, 0)
        except (ValueError, TypeError):
            return 0

    reply_ratio = pf(agent.get('replyRatio', -1))
    if reply_ratio >= 95:
        strengths.append(f'应答率优秀（{reply_ratio:.1f}%）')
    elif reply_ratio > 0 and reply_ratio < 80:
        weaknesses.append(f'应答率偏低（{reply_ratio:.1f}%）')

    sat_ratio = pf(agent.get('satisfactionRatio', -1))
    if sat_ratio >= 90:
        strengths.append(f'满意度高（{sat_ratio:.1f}%）')
    elif sat_ratio > 0 and sat_ratio < 70:
        weaknesses.append(f'满意度较低（{sat_ratio:.1f}%）')

    avg_resp_ms = pf(agent.get('avgRespTime', 0))
    avg_resp = avg_resp_ms / 1000 if avg_resp_ms > 0 else 0  # 毫秒转秒
    if avg_resp > 0:
        if avg_resp <= 30:
            strengths.append(f'响应迅速（平均{avg_resp:.0f}秒）')
        elif avg_resp > 120:
            weaknesses.append(f'响应较慢（平均{avg_resp:.0f}秒）')

    no_reply = pf(agent.get('noReplyRatio', 0))
    # noReplyRatio有时是0-1小数
    if 0 < no_reply <= 1:
        no_reply = no_reply * 100
    if no_reply > 10:
        weaknesses.append(f'未回复率较高（{no_reply:.1f}%）')
    elif no_reply == 0 and agent.get('totalSessions', 0) > 0:
        strengths.append('全部会话均有回复')

    one_off = pf(agent.get('oneOffRatio', -1))
    if one_off >= 80:
        strengths.append(f'一次性解决率高（{one_off:.1f}%）')
    elif one_off > 0 and one_off < 50:
        weaknesses.append(f'一次性解决率需提升（{one_off:.1f}%）')

    eva_r = pf(agent.get('evaRatio', -1))
    if eva_r > 0 and eva_r < 30:
        weaknesses.append(f'参评率较低（{eva_r:.1f}%）')

    not_sat = agent.get('notSatisfiedCount', 0) + agent.get('veryNotSatisfiedCount', 0)
    if not_sat > 5:
        weaknesses.append(f'不满意评价较多（{not_sat}条）')

    if not strengths:
        strengths.append('各项指标均衡')
    if not weaknesses:
        weaknesses.append('无明显短板')

    return strengths, weaknesses


def generate_agent_suggestion(grade: str, strengths: list, weaknesses: list) -> str:
    """生成坐席改进建议"""
    suggestions = []
    grade_map = {
        '优秀': '表现优秀，建议作为团队标杆进行经验分享',
        '良好': '整体表现良好，可在细节上继续优化',
        '合格': '基本达到服务标准，仍有提升空间',
        '待改进': '多项指标需重点关注，建议制定专项提升计划',
        '不合格': '服务质量存在明显问题，需要进行系统培训和整改'
    }
    suggestions.append(grade_map.get(grade, ''))

    for w in weaknesses[:2]:
        if '应答率' in w:
            suggestions.append('建议关注消息提醒，提高应答及时性')
        if '响应较慢' in w:
            suggestions.append('建议优化回复模板和快捷语，缩短响应时间')
        if '满意度较低' in w or '不满意' in w:
            suggestions.append('建议复盘不满意会话，优化服务态度和问题解决能力')
        if '未回复率' in w:
            suggestions.append('建议建立会话关闭确认机制，避免遗漏回复')
        if '一次性解决率' in w:
            suggestions.append('建议深入学习业务知识，提升首次问题解决能力')
        if '参评率' in w:
            suggestions.append('建议在会话结束时主动邀请用户评价')

    return '；'.join(suggestions) if suggestions else '保持当前服务水平'


def generate_report_insights(overall: dict, agent_evals: list) -> tuple:
    """生成报告洞察"""
    findings = []
    recommendations = []
    score_summary = {}

    # 整体指标分析
    answer_ratio = float(overall.get('answerRatio', 0))
    if answer_ratio < 85:
        findings.append(f'整体应答率偏低（{answer_ratio}%），可能影响用户体验')
        recommendations.append('优化排队分配策略，确保咨询服务能被及时响应')

    sat_ratio = float(overall.get('satisfactionRatio', 0))
    if sat_ratio >= 85:
        findings.append(f'整体满意度良好（{sat_ratio}%），用户对服务质量认可度高')
    elif sat_ratio < 75:
        findings.append(f'整体满意度需提升（{sat_ratio}%），建议分析不满意的会话详情')
        recommendations.append('深入分析不满意评价的会话，找出共性问题和改进方向')

    one_off = float(overall.get('oneOffRatio', 0))
    if one_off < 60:
        findings.append(f'一次性解决率偏低（{one_off}%），用户需要多次咨询才能解决问题')
        recommendations.append('加强客服知识库建设，提升坐席首次解决问题的能力')

    avg_resp = float(overall.get('avgRespTime', 0)) / 1000  # 毫秒转秒
    if avg_resp > 60:
        findings.append(f'平均响应时间较长（{avg_resp}秒），需关注响应效率')

    # 坐席分布分析
    grades_count = {}
    for ev in agent_evals:
        g = ev['grade']
        grades_count[g] = grades_count.get(g, 0) + 1

    score_summary = {
        "grades": grades_count,
        "avgScore": round(sum(e['score'] for e in agent_evals) / len(agent_evals), 1) if agent_evals else 0,
        "topAgent": agent_evals[0]['name'] if agent_evals else '',
        "topScore": agent_evals[0]['score'] if agent_evals else 0,
        "totalAgents": len(agent_evals)
    }

    bad_count = grades_count.get('待改进', 0) + grades_count.get('不合格', 0)
    if bad_count > 0:
        findings.append(f'有{bad_count}名坐席服务质量待改进，建议重点关注')
        recommendations.append(f'对{bad_count}名表现较差的坐席进行一对一辅导和培训')

    return findings, recommendations, score_summary


# ==================== 数据刷新 ====================

@app.route('/api/cache/clear', methods=['POST'])
def api_clear_cache():
    """清除缓存"""
    global cache, raw_cache
    cache = {}
    raw_cache = {}
    return jsonify({"code": 200, "message": "缓存已清除"})


# ==================== 启动 ====================

if __name__ == '__main__':
    import webbrowser
    import threading
    import time as time_mod

    # 初始化缓存数据库
    init_db()

    # 启动后台定时缓存任务
    scheduler_thread = threading.Thread(target=run_session_cache_job, daemon=True, name="session-cache-scheduler")
    scheduler_thread.start()

    app.run(host='0.0.0.0', port=5890, debug=False, use_reloader=False)
