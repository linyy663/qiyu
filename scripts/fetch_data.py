"""
GitHub Actions 数据抓取脚本
每日凌晨 2:00 自动运行，调用七鱼 API 获取统计数据并生成静态 JSON 文件
"""
import json
import os
import sys
import time
import hashlib
import datetime
import requests

# ==================== 配置（从 GitHub Secrets 环境变量读取） ====================
APP_KEY = os.environ.get('QIYU_APP_KEY', '')
APP_SECRET = os.environ.get('QIYU_APP_SECRET', '')
BASE_URL = os.environ.get('QIYU_BASE_URL', 'https://qiyukf.com').rstrip('/')
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')

if not APP_KEY or not APP_SECRET:
    print("ERROR: QIYU_APP_KEY and QIYU_APP_SECRET must be set as environment variables")
    sys.exit(1)

# ==================== 七鱼 API 客户端 ====================

class QiyuAPI:
    def __init__(self):
        self.app_key = APP_KEY
        self.app_secret = APP_SECRET
        self.base_url = BASE_URL

    def _make_sign(self, body=None):
        current_time = str(int(time.time()))
        body_str = json.dumps(body, ensure_ascii=False) if body else ''
        md5_body = hashlib.md5(body_str.encode('utf-8')).hexdigest()
        sign_str = self.app_secret + md5_body + current_time
        checksum = hashlib.sha1(sign_str.encode('utf-8')).hexdigest()
        return current_time, checksum, body_str

    def _post(self, path, body=None):
        current_time, checksum, body_str = self._make_sign(body)
        url = f"{self.base_url}{path}"
        params = {'appKey': self.app_key, 'time': current_time, 'checksum': checksum}
        headers = {'Content-Type': 'application/json;charset=utf-8'}
        try:
            body_bytes = body_str.encode('utf-8') if body_str else None
            resp = requests.post(url, params=params, data=body_bytes, headers=headers, timeout=30)
            result = resp.json()
            if 'message' in result and isinstance(result['message'], str):
                try:
                    result['message'] = json.loads(result['message'])
                except (json.JSONDecodeError, TypeError):
                    pass
            if result.get('code') != 200:
                print(f"  [API ERROR] {path}: code={result.get('code')}, message={str(result.get('message', ''))[:150]}")
            return result
        except requests.RequestException as e:
            return {"code": -1, "message": str(e)}
        except json.JSONDecodeError:
            return {"code": -1, "message": f"响应解析失败: {resp.text[:200]}"}

    def get_overview(self, start_time, end_time):
        return self._post('/openapi/statistic/overview', {"startTime": start_time, "endTime": end_time})

    def get_staff_workload(self, start_time, end_time, model=1):
        return self._post('/openapi/statistic/staffworklod', {"startTime": start_time, "endTime": end_time, "model": model})

    def get_staff_quality(self, start_time, end_time, model=1):
        return self._post('/openapi/statistic/staffquality', {"startTime": start_time, "endTime": end_time, "model": model})

    def get_staff_satisfaction(self, start_time, end_time, model=1):
        return self._post('/openapi/statistic/satisfaction/report', {"startTime": start_time, "endTime": end_time, "model": model})

    def get_staff_attendance(self, start_time, end_time):
        return self._post('/openapi/statistic/staffAttendance', {"startTime": start_time, "endTime": end_time})

    def export_sessions(self, start_ms, end_ms):
        return self._post('/openapi/export/session', {"start": str(start_ms), "end": str(end_ms)})


# ==================== 时间工具 ====================

def get_timestamp_range(date_str, days=None):
    """获取指定日期的毫秒时间戳范围（中国时区 UTC+8）"""
    if date_str:
        dt = datetime.datetime.strptime(date_str, '%Y-%m-%d')
    else:
        dt = datetime.datetime.now() - datetime.timedelta(days=1)
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)

    china_tz = datetime.timezone(datetime.timedelta(hours=8))
    start = dt.replace(tzinfo=china_tz)
    span = days if days and days > 1 else 1
    end = start + datetime.timedelta(days=span)

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    # 截断到当前时刻
    now_ms = int(datetime.datetime.now(china_tz).timestamp() * 1000)
    if end_ms > now_ms:
        end_ms = now_ms

    if span > 1:
        label = f"{dt.strftime('%Y-%m-%d')} ~ {(start + datetime.timedelta(days=span - 1)).strftime('%Y-%m-%d')}"
    else:
        label = dt.strftime('%Y-%m-%d')
    return start_ms, end_ms, label


# ==================== 数据处理 ====================

def normalize_to_list(data, key_field='id'):
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ['result', 'staffs', 'list', 'items', 'data', 'records']:
            if k in data and isinstance(data[k], list):
                return data[k]
        if key_field in data or 'name' in data:
            return [data]
    return []


def to_pct(v):
    if v is None or v < 0:
        return 0
    return round(v * 100, 1) if isinstance(v, float) and v <= 1 else round(float(v), 1)


def calculate_agent_score(agent):
    def safe_float(v, default=0):
        try:
            return max(float(v), 0)
        except (ValueError, TypeError):
            return default

    score = 50
    reply_ratio = safe_float(agent.get('replyRatio', 0))
    score += min(reply_ratio, 100) * 0.15
    sat_ratio = safe_float(agent.get('satisfactionRatio', 0))
    score += min(sat_ratio, 100) * 0.20
    total = agent.get('totalSessions', 0) or 1
    valid = agent.get('validSessions', 0)
    valid_ratio = (valid / total * 100) if total > 0 else 0
    score += min(valid_ratio, 100) * 0.10
    no_reply = safe_float(agent.get('noReplyRatio', 0))
    if 0 < no_reply <= 1:
        no_reply = no_reply * 100
    score += max(0, 100 - no_reply) * 0.10
    avg_resp_ms = safe_float(agent.get('avgRespTime', 0))
    avg_resp_sec = avg_resp_ms / 1000 if avg_resp_ms > 0 else 0
    resp_score = max(0, 100 - avg_resp_sec / 3)
    score += min(resp_score, 100) * 0.15
    one_off = safe_float(agent.get('oneOffRatio', 0))
    score += min(one_off, 100) * 0.10
    eva_r = safe_float(agent.get('evaRatio', 0))
    score += min(eva_r, 100) * 0.10
    incoming = agent.get('incomingSessions', 0) or 0
    volume_score = min(incoming / 2, 100)
    score += volume_score * 0.10
    return round(min(score, 100), 1)


def get_score_grade(score):
    if score >= 90: return '优秀'
    elif score >= 80: return '良好'
    elif score >= 70: return '合格'
    elif score >= 60: return '待改进'
    else: return '不合格'


def _safe_pct(v):
    try:
        f = float(v)
        if f < 0: return -1.0
        return round(f * 100, 2) if f <= 1.0 else round(f, 2)
    except (ValueError, TypeError):
        return -1.0


def analyze_agent_performance(agent):
    strengths = []
    weaknesses = []
    def pf(v):
        try:
            f = float(v)
            return max(f, 0)
        except: return 0

    reply_ratio = pf(agent.get('replyRatio', -1))
    if reply_ratio >= 95: strengths.append(f'应答率优秀（{reply_ratio:.1f}%）')
    elif reply_ratio > 0 and reply_ratio < 80: weaknesses.append(f'应答率偏低（{reply_ratio:.1f}%）')

    sat_ratio = pf(agent.get('satisfactionRatio', -1))
    if sat_ratio >= 90: strengths.append(f'满意度高（{sat_ratio:.1f}%）')
    elif sat_ratio > 0 and sat_ratio < 70: weaknesses.append(f'满意度较低（{sat_ratio:.1f}%）')

    avg_resp_ms = pf(agent.get('avgRespTime', 0))
    avg_resp = avg_resp_ms / 1000 if avg_resp_ms > 0 else 0
    if avg_resp > 0:
        if avg_resp <= 30: strengths.append(f'响应迅速（平均{avg_resp:.0f}秒）')
        elif avg_resp > 120: weaknesses.append(f'响应较慢（平均{avg_resp:.0f}秒）')

    no_reply = pf(agent.get('noReplyRatio', 0))
    if 0 < no_reply <= 1: no_reply = no_reply * 100
    if no_reply > 10: weaknesses.append(f'未回复率较高（{no_reply:.1f}%）')
    elif no_reply == 0 and agent.get('totalSessions', 0) > 0: strengths.append('全部会话均有回复')

    one_off = pf(agent.get('oneOffRatio', -1))
    if one_off >= 80: strengths.append(f'一次性解决率高（{one_off:.1f}%）')
    elif one_off > 0 and one_off < 50: weaknesses.append(f'一次性解决率需提升（{one_off:.1f}%）')

    eva_r = pf(agent.get('evaRatio', -1))
    if eva_r > 0 and eva_r < 30: weaknesses.append(f'参评率较低（{eva_r:.1f}%）')

    not_sat = agent.get('notSatisfiedCount', 0) + agent.get('veryNotSatisfiedCount', 0)
    if not_sat > 5: weaknesses.append(f'不满意评价较多（{not_sat}条）')

    if not strengths: strengths.append('各项指标均衡')
    if not weaknesses: weaknesses.append('无明显短板')
    return strengths, weaknesses


def generate_agent_suggestion(grade, strengths, weaknesses):
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
        if '应答率' in w: suggestions.append('建议关注消息提醒，提高应答及时性')
        if '响应较慢' in w: suggestions.append('建议优化回复模板和快捷语，缩短响应时间')
        if '满意度较低' in w or '不满意' in w: suggestions.append('建议复盘不满意会话，优化服务态度和问题解决能力')
        if '未回复率' in w: suggestions.append('建议建立会话关闭确认机制，避免遗漏回复')
        if '一次性解决率' in w: suggestions.append('建议深入学习业务知识，提升首次问题解决能力')
        if '参评率' in w: suggestions.append('建议在会话结束时主动邀请用户评价')
    return '；'.join(suggestions) if suggestions else '保持当前服务水平'


def generate_report_insights(overall, agent_evals):
    findings = []
    recommendations = []
    score_summary = {}

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

    avg_resp = float(overall.get('avgRespTime', 0)) / 1000
    if avg_resp > 60:
        findings.append(f'平均响应时间较长（{avg_resp}秒），需关注响应效率')

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


# ==================== 主抓取逻辑 ====================

def fetch_data_for_date(date_str, days=None):
    """抓取指定日期（或日期范围）的所有数据"""
    start_ms, end_ms, label = get_timestamp_range(date_str, days=days)
    print(f"\n{'='*50}")
    print(f"  Fetching data for: {label}")
    print(f"  Time range: {start_ms} - {end_ms}")
    print(f"{'='*50}")

    api = QiyuAPI()

    # ---- 1. 数据总览 ----
    print("\n[1/5] Fetching overview...")
    overview_result = api.get_overview(start_ms, end_ms)
    for attempt in range(2):
        if overview_result.get('code') == 14009:
            print("  Rate limited, retrying after 2s...")
            time.sleep(2)
            overview_result = api.get_overview(start_ms, end_ms)
        else:
            break

    if overview_result.get('code') != 200:
        print(f"  ERROR: overview API failed: {overview_result.get('message')}")
        return None

    data = overview_result.get('message', {})
    overview = {
        "date": label,
        "days": days or 1,
        "totalSessions": data.get('sessions', 0),
        "effectiveSessions": data.get('effectSessions', 0),
        "assignedRatio": to_pct(data.get('assignedRatio', 0)),
        "totalMessages": data.get('messages', 0),
        "answerRatio": to_pct(data.get('answerRatio', 0)),
        "satisfactionRatio": to_pct(data.get('satisfactionRatio', 0)),
        "avgFirstRespTime": data.get('avgFirstRespTime', 0),
        "avgRespTime": data.get('avgRespTime', 0),
        "avgSessionTime": data.get('avgTime', 0),
        "evaRatio": to_pct(data.get('evaRatio', 0)),
        "queueCount": data.get('queueCount', 0),
        "maxQueueTime": data.get('maxQueueTime', 0),
        "oneOffRatio": to_pct(data.get('oneOffRatio', 0)),
        "loginStaffCount": data.get('loginStaffCount', 0),
        "visitCount": data.get('visit', 0),
        "totalConsultCount": data.get('totalConsultCount', 0),
    }
    print(f"  OK: {overview['totalSessions']} sessions, {overview['answerRatio']}% answer, {overview['satisfactionRatio']}% satisfaction")

    # ---- 2. 坐席工作量 ----
    print("[2/5] Fetching staff workload...")
    time.sleep(1)
    workload_result = api.get_staff_workload(start_ms, end_ms)
    for attempt in range(2):
        if workload_result.get('code') == 14009:
            time.sleep(2)
            workload_result = api.get_staff_workload(start_ms, end_ms)
        else:
            break
    workload_data = normalize_to_list(workload_result.get('message', workload_result.get('data'))) if workload_result.get('code') == 200 else []
    print(f"  OK: {len(workload_data)} staff records")

    # ---- 3. 坐席质量 ----
    print("[3/5] Fetching staff quality...")
    time.sleep(1)
    quality_result = api.get_staff_quality(start_ms, end_ms)
    for attempt in range(2):
        if quality_result.get('code') == 14009:
            time.sleep(2)
            quality_result = api.get_staff_quality(start_ms, end_ms)
        else:
            break
    quality_data = normalize_to_list(quality_result.get('message', quality_result.get('data'))) if quality_result.get('code') == 200 else []
    print(f"  OK: {len(quality_data)} staff records")

    # ---- 4. 坐席满意度 ----
    print("[4/5] Fetching staff satisfaction...")
    time.sleep(1)
    satisfaction_result = api.get_staff_satisfaction(start_ms, end_ms)
    for attempt in range(2):
        if satisfaction_result.get('code') == 14009:
            time.sleep(2)
            satisfaction_result = api.get_staff_satisfaction(start_ms, end_ms)
        else:
            break
    satisfaction_data = normalize_to_list(satisfaction_result.get('message', satisfaction_result.get('data'))) if satisfaction_result.get('code') == 200 else []
    print(f"  OK: {len(satisfaction_data)} staff records")

    # ---- 5. 坐席考勤 ----
    print("[5/5] Fetching staff attendance...")
    time.sleep(1)
    attendance_result = api.get_staff_attendance(start_ms, end_ms)
    for attempt in range(2):
        if attendance_result.get('code') == 14009:
            time.sleep(2)
            attendance_result = api.get_staff_attendance(start_ms, end_ms)
        else:
            break
    attendance_data = normalize_to_list(attendance_result.get('message', attendance_result.get('data'))) if attendance_result.get('code') == 200 else []
    print(f"  OK: {len(attendance_data)} attendance records")

    # ---- 合并坐席数据 ----
    print("\n  Merging agent data...")
    agents_map = {}

    for item in workload_data:
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

    for item in quality_data:
        agent_id = item.get('id')
        if agent_id:
            if agent_id not in agents_map:
                agents_map[agent_id] = {"id": agent_id, "name": item.get('name', '')}
            agents_map[agent_id].update({
                "avgFirstRespTime": item.get('avgFirstRespTime', 0),
                "avgRespTime": item.get('avgRespTime', 0),
                "replyRatio": _safe_pct(item.get('replyRatio', -1)),
                "avgSessionTime": item.get('avgTime', 0),
                "evaRatio": _safe_pct(item.get('evaRatio', -1)),
                "satisfactionRatio": _safe_pct(item.get('satisfactionRatio', -1)),
                "evaluationDetail": item.get('evaluationDetail', ''),
                "answerReplyRatio": _safe_pct(item.get('answerReplyRatio', -1)),
                "oneOffRatio": _safe_pct(item.get('oneOffRatio', -1)),
            })

    for item in satisfaction_data:
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

    # 考勤数据
    attendance_map = {}
    for item in attendance_data:
        agent_id = item.get('id')
        if not agent_id: continue
        if agent_id not in attendance_map:
            attendance_map[agent_id] = {
                'firstLoginTs': None, 'firstOnlineTs': None, 'lastLogoutTs': None,
                'loginDuration': 0, 'onlineDuration': 0, 'pcOnlineDuration': 0,
                'mobileOnlineDuration': 0, 'workDays': 0, 'loginDays': 0,
            }
        am = attendance_map[agent_id]
        ft_login = item.get('firstLoginTs', 0) or 0
        ft_online = item.get('firstOnlineTs', 0) or 0
        lt_logout = item.get('lastLogoutTs', 0) or 0
        if ft_login > 0:
            if am['firstLoginTs'] is None or ft_login < am['firstLoginTs']: am['firstLoginTs'] = ft_login
            am['loginDays'] += 1
        if ft_online > 0:
            if am['firstOnlineTs'] is None or ft_online < am['firstOnlineTs']: am['firstOnlineTs'] = ft_online
        if lt_logout > 0:
            if am['lastLogoutTs'] is None or lt_logout > am['lastLogoutTs']: am['lastLogoutTs'] = lt_logout
        am['loginDuration'] += (item.get('loginDuration', 0) or 0)
        am['onlineDuration'] += (item.get('onlineDuration', 0) or 0)
        am['pcOnlineDuration'] += (item.get('pcOnlineDuration', 0) or 0)
        am['mobileOnlineDuration'] += (item.get('mobileOnlineDuration', 0) or 0)
        if (item.get('onlineDuration', 0) or 0) > 0: am['workDays'] += 1

    for agent_id, agent in agents_map.items():
        am = attendance_map.get(agent_id, {})
        first_online = am.get('firstOnlineTs')
        first_login = am.get('firstLoginTs')
        agent['firstOnlineTime'] = first_online if first_online else first_login
        agent['lastLogoutTime'] = am.get('lastLogoutTs')
        agent['loginDuration'] = am.get('loginDuration', 0)
        agent['onlineDuration'] = am.get('onlineDuration', 0)
        agent['pcOnlineDuration'] = am.get('pcOnlineDuration', 0)
        agent['mobileOnlineDuration'] = am.get('mobileOnlineDuration', 0)
        agent['workDays'] = am.get('workDays', 0)
        agent['loginDays'] = am.get('loginDays', 0)

    agents_list = list(agents_map.values())
    for agent in agents_list:
        agent['score'] = calculate_agent_score(agent)
        agent['grade'] = get_score_grade(agent['score'])
    agents_list.sort(key=lambda x: x['score'], reverse=True)

    print(f"  Merged: {len(agents_list)} agents")

    # ---- 生成报告 ----
    agent_evals = []
    for item in workload_data:
        aid = item.get('id')
        if not aid: continue
        agent_evals.append({
            "id": aid, "name": item.get('name', ''),
            "incomingSessions": item.get('sessionsCount', 0),
            "validSessions": item.get('validSessionCount', 0),
            "noReplyRatio": _safe_pct(item.get('noReplyRatio', 0)),
            "totalSessions": item.get('totalSessionCount', 0),
        })

    for item in quality_data:
        aid = item.get('id')
        if not aid: continue
        found = next((a for a in agent_evals if a['id'] == aid), None)
        if not found:
            agent_evals.append({"id": aid, "name": item.get('name', ''), "incomingSessions": 0, "validSessions": 0, "noReplyRatio": -1, "totalSessions": 0})
            found = agent_evals[-1]
        found.update({
            "avgRespTime": item.get('avgRespTime', 0),
            "avgFirstRespTime": item.get('avgFirstRespTime', 0),
            "replyRatio": _safe_pct(item.get('replyRatio', -1)),
            "avgSessionTime": item.get('avgTime', 0),
            "evaRatio": _safe_pct(item.get('evaRatio', -1)),
            "satisfactionRatio": _safe_pct(item.get('satisfactionRatio', -1)),
            "oneOffRatio": _safe_pct(item.get('oneOffRatio', -1)),
            "answerReplyRatio": _safe_pct(item.get('answerReplyRatio', -1)),
        })

    for item in satisfaction_data:
        aid = item.get('id')
        if not aid: continue
        found = next((a for a in agent_evals if a['id'] == aid), None)
        if not found:
            agent_evals.append({"id": aid, "name": item.get('name', ''), "incomingSessions": 0, "validSessions": 0, "noReplyRatio": -1, "totalSessions": 0})
            found = agent_evals[-1]
        found.update({
            "verySatisfiedCount": item.get('verySatisfiedCount', 0),
            "satisfiedCount": item.get('satisfiedCount', 0),
            "normalCount": item.get('normalSatisfiedCount', 0),
            "notSatisfiedCount": item.get('notSatisfiedCount', 0),
            "veryNotSatisfiedCount": item.get('veryNotSatisfiedCount', 0),
        })

    for ev in agent_evals:
        score = calculate_agent_score(ev)
        grade = get_score_grade(score)
        strengths, weaknesses = analyze_agent_performance(ev)
        ev['score'] = score
        ev['grade'] = grade
        ev['strengths'] = strengths
        ev['weaknesses'] = weaknesses
        ev['suggestion'] = generate_agent_suggestion(grade, strengths, weaknesses)

    agent_evals.sort(key=lambda x: x['score'], reverse=True)

    report_overall = {
        "totalSessions": overview.get('totalSessions', 0),
        "effectiveSessions": overview.get('effectiveSessions', 0),
        "answerRatio": overview.get('answerRatio', 0),
        "satisfactionRatio": overview.get('satisfactionRatio', 0),
        "avgFirstRespTime": overview.get('avgFirstRespTime', 0),
        "avgRespTime": overview.get('avgRespTime', 0),
        "avgSessionTime": overview.get('avgSessionTime', 0),
        "oneOffRatio": overview.get('oneOffRatio', 0),
        "totalMessages": overview.get('totalMessages', 0),
        "loginStaffCount": overview.get('loginStaffCount', 0),
        "queueCount": overview.get('queueCount', 0),
        "maxQueueTime": overview.get('maxQueueTime', 0),
        "evaRatio": overview.get('evaRatio', 0),
        "totalConsultCount": overview.get('totalConsultCount', 0),
        "visitCount": overview.get('visitCount', 0),
    }

    key_findings, recommendations, score_summary = generate_report_insights(report_overall, agent_evals)

    report = {
        "reportDate": label,
        "days": days or 1,
        "generatedAt": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "overall": report_overall,
        "agentEvaluations": agent_evals,
        "keyFindings": key_findings,
        "recommendations": recommendations,
        "scoreSummary": score_summary
    }

    # ---- 打包最终数据 ----
    result = {
        "generatedAt": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "date": label,
        "days": days or 1,
        "overview": overview,
        "agents": agents_list,
        "report": report,
    }

    return result


def try_session_export(date_str):
    """尝试导出会话数据（仅凌晨可调用，企业需开通权限）"""
    start_ms, end_ms, _ = get_timestamp_range(date_str)
    api = QiyuAPI()
    result = api.export_sessions(start_ms, end_ms)
    if result.get('code') == 200:
        sessions = result.get('data', result.get('message', []))
        if isinstance(sessions, dict):
            for k in ['result', 'list', 'items', 'data']:
                if k in sessions and isinstance(sessions[k], list):
                    sessions = sessions[k]
                    break
            else:
                sessions = []
        if isinstance(sessions, list):
            return sessions
    return None


# ==================== 入口 ====================

def main():
    print("=" * 60)
    print("  七鱼客服数据抓取 - GitHub Actions")
    print(f"  运行时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 抓取前一天的日数据
    yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    today = datetime.datetime.now().strftime('%Y-%m-%d')

    # 日数据
    daily_data = fetch_data_for_date(yesterday)
    if daily_data:
        filename = f"{yesterday}.json"
        filepath = os.path.join(OUTPUT_DIR, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(daily_data, f, ensure_ascii=False)
        print(f"\n  Saved: {filepath}")

    # 近一周数据
    week_data = fetch_data_for_date(yesterday, days=7)
    if week_data:
        filename = f"{yesterday}-week.json"
        filepath = os.path.join(OUTPUT_DIR, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(week_data, f, ensure_ascii=False)
        print(f"  Saved: {filepath}")

    # 今天数据（实时截断到当前时刻）
    today_data = fetch_data_for_date(today)
    if today_data:
        filename = f"{today}.json"
        filepath = os.path.join(OUTPUT_DIR, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(today_data, f, ensure_ascii=False)
        print(f"  Saved: {filepath}")

    # 写入 latest.json 索引
    latest = {
        "updatedAt": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "dates": [],
        "sessionExportAvailable": False
    }

    # 列出所有已生成的数据文件
    for fname in sorted(os.listdir(OUTPUT_DIR)):
        if fname.endswith('.json') and not fname.startswith('latest') and '-week' not in fname:
            latest["dates"].append(fname.replace('.json', ''))

    latest["dates"].sort(reverse=True)

    with open(os.path.join(OUTPUT_DIR, 'latest.json'), 'w', encoding='utf-8') as f:
        json.dump(latest, f, ensure_ascii=False)

    # 尝试会话导出（凌晨时段）
    now = datetime.datetime.now()
    if 2 <= now.hour < 6:
        print(f"\n  [SESSION] In export window (2-6 AM), attempting session export for {yesterday}...")
        sessions = try_session_export(yesterday)
        if sessions:
            with open(os.path.join(OUTPUT_DIR, f"sessions-{yesterday}.json"), 'w', encoding='utf-8') as f:
                json.dump({"date": yesterday, "sessions": sessions, "total": len(sessions)}, f, ensure_ascii=False)
            print(f"  [SESSION] Cached {len(sessions)} sessions")
            latest["sessionExportAvailable"] = True
            with open(os.path.join(OUTPUT_DIR, 'latest.json'), 'w', encoding='utf-8') as f:
                json.dump(latest, f, ensure_ascii=False)
        else:
            print(f"  [SESSION] Export failed (API may not be available for this enterprise)")
    else:
        print(f"\n  [SESSION] Outside export window (2-6 AM), skipping session export")

    print(f"\n{'='*60}")
    print(f"  DONE! {len(latest['dates'])} dates cached in data/")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
