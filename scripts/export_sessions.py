"""
GitHub Actions 每日会话导出脚本
每日凌晨 2:00 (UTC 18:00) 运行，导出前一天会话数据为静态 JSON
"""
import json
import os
import sys
import time
import hashlib
import datetime
import zipfile
import io
import requests

# ==================== 配置 ====================
APP_KEY = os.environ.get('QIYU_APP_KEY', '')
APP_SECRET = os.environ.get('QIYU_APP_SECRET', '')
BASE_URL = os.environ.get('QIYU_BASE_URL', 'https://qiyukf.com').rstrip('/')
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')

if not APP_KEY or not APP_SECRET:
    print("ERROR: QIYU_APP_KEY and QIYU_APP_SECRET must be set as environment variables")
    sys.exit(1)


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
            if result.get('code') != 200:
                print(f"  [API ERROR] {path}: code={result.get('code')}, message={str(result.get('message', ''))[:150]}")
            return result
        except Exception as e:
            return {"code": -1, "message": str(e)}

    def submit_session_export(self, start_ms, end_ms):
        return self._post('/openapi/export/session', {"start": str(start_ms), "end": str(end_ms)})

    def check_session_export(self, key):
        return self._post('/openapi/export/session/check', {"key": key})


def get_timestamp_range(date_str):
    """获取指定日期 0:00~23:59 的毫秒时间戳范围（UTC+8）"""
    dt = datetime.datetime.strptime(date_str, '%Y-%m-%d')
    china_tz = datetime.timezone(datetime.timedelta(hours=8))
    start = dt.replace(tzinfo=china_tz)
    end = start + datetime.timedelta(days=1)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    now_ms = int(datetime.datetime.now(china_tz).timestamp() * 1000)
    if end_ms > now_ms:
        end_ms = now_ms
    return start_ms, end_ms


def export_sessions_for_date(date_str, max_wait=180):
    """
    异步会话导出：提交任务 → 轮询 → 下载ZIP → 解析
    """
    start_ms, end_ms = get_timestamp_range(date_str)
    api = QiyuAPI()

    print(f"  [SESSION] Submitting export task for {date_str} ({start_ms} ~ {end_ms})...")
    submit_result = api.submit_session_export(start_ms, end_ms)

    if submit_result.get('code') != 200:
        print(f"  [SESSION] Submit failed: code={submit_result.get('code')}, msg={submit_result.get('message')}")
        return None

    task_key = submit_result.get('message')
    if not task_key:
        print(f"  [SESSION] Invalid task key: {task_key}")
        return None

    print(f"  [SESSION] Task submitted, key={task_key}. Polling...")

    poll_interval = 10
    waited = 0
    download_url = None

    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        check_result = api.check_session_export(task_key)
        code = check_result.get('code')
        msg = check_result.get('message')

        print(f"  [SESSION] Poll ({waited}s): code={code}")

        if code == 200:
            download_url = msg
            break
        elif code == 14403:
            continue
        else:
            print(f"  [SESSION] Poll failed: msg={str(msg)[:100]}")
            return None

    if not download_url:
        print(f"  [SESSION] Timed out after {max_wait}s")
        return None

    print(f"  [SESSION] Downloading ZIP...")
    try:
        resp = requests.get(download_url, timeout=60)
        resp.raise_for_status()
        zip_bytes = resp.content
        print(f"  [SESSION] Downloaded {len(zip_bytes)} bytes")
    except Exception as e:
        print(f"  [SESSION] Download failed: {e}")
        return None

    # 解压（密码=appkey前12位）
    zip_password = APP_KEY[:12].encode('utf-8')
    sessions = []
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                print(f"  [SESSION] Extracting: {name}")
                try:
                    content = zf.read(name, pwd=zip_password).decode('utf-8')
                except Exception:
                    content = zf.read(name).decode('utf-8')

                for line in content.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            sessions.append(obj)
                        elif isinstance(obj, list):
                            sessions.extend(obj)
                    except json.JSONDecodeError:
                        pass

        print(f"  [SESSION] Parsed {len(sessions)} sessions")
        return sessions

    except Exception as e:
        print(f"  [SESSION] Extraction failed: {e}")
        return None


def main():
    print("=" * 60)
    print("  七鱼客服会话数据导出")
    print(f"  运行时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    china_tz = datetime.timezone(datetime.timedelta(hours=8))
    now_cn = datetime.datetime.now(china_tz)
    yesterday = (now_cn - datetime.timedelta(days=1)).strftime('%Y-%m-%d')

    # 导出昨天的会话
    sessions = export_sessions_for_date(yesterday, max_wait=180)

    if sessions:
        filepath = os.path.join(OUTPUT_DIR, f"sessions-{yesterday}.json")
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump({
                "date": yesterday,
                "generatedAt": now_cn.strftime('%Y-%m-%d %H:%M:%S'),
                "sessions": sessions,
                "total": len(sessions)
            }, f, ensure_ascii=False)
        print(f"\n  [SESSION] Cached {len(sessions)} sessions → {filepath}")

        # 更新 latest.json
        latest_path = os.path.join(OUTPUT_DIR, 'latest.json')
        latest = {"updatedAt": now_cn.strftime('%Y-%m-%d %H:%M:%S'), "dates": [], "sessionDates": []}
        if os.path.exists(latest_path):
            with open(latest_path, 'r', encoding='utf-8') as f:
                latest = json.load(f)
        if yesterday not in latest.get("sessionDates", []):
            latest.setdefault("sessionDates", []).append(yesterday)
            latest["sessionDates"] = sorted(list(set(latest["sessionDates"])), reverse=True)
        with open(latest_path, 'w', encoding='utf-8') as f:
            json.dump(latest, f, ensure_ascii=False)
        print(f"  Updated latest.json with session date {yesterday}")
    else:
        print(f"\n  [SESSION] Export failed or no data for {yesterday}")

    print("\n  DONE!")


if __name__ == '__main__':
    main()
