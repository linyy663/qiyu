"""
网易七鱼 API 客户端
封装所有七鱼开放接口的鉴权和调用
"""
import hashlib
import json
import time
import requests
from config_manager import load_config


class QiyuAPI:
    """网易七鱼 API 客户端"""

    def __init__(self):
        self.config = load_config()
        self.app_key = self.config.get('appKey', '')
        self.app_secret = self.config.get('appSecret', '')
        self.base_url = self.config.get('baseUrl', 'https://qiyukf.com').rstrip('/')

    def reload_config(self):
        """重新加载配置"""
        self.config = load_config()
        self.app_key = self.config.get('appKey', '')
        self.app_secret = self.config.get('appSecret', '')
        self.base_url = self.config.get('baseUrl', 'https://qiyukf.com').rstrip('/')

    def _make_sign(self, body: dict = None) -> tuple:
        """生成请求签名和序列化后的body字符串
        返回: (time_str, checksum, body_str)
        
        checksum = SHA1(appSecret + md5(body_json) + time)
        关键：body_json 必须与 POST 请求的 body 字节完全一致！
        """
        current_time = str(int(time.time()))
        # 使用与 requests 库相同的 JSON 序列化方式（默认有空格）
        # 注意：API文档要求 md5 = MD5(bodyContent)，bodyContent 是请求中实际发送的字节
        body_str = json.dumps(body, ensure_ascii=False) if body else ''
        md5_body = hashlib.md5(body_str.encode('utf-8')).hexdigest()
        sign_str = self.app_secret + md5_body + current_time
        checksum = hashlib.sha1(sign_str.encode('utf-8')).hexdigest()
        return current_time, checksum, body_str

    def _post(self, path: str, body: dict = None) -> dict:
        """发送 POST 请求"""
        current_time, checksum, body_str = self._make_sign(body)
        url = f"{self.base_url}{path}"
        params = {
            'appKey': self.app_key,
            'time': current_time,
            'checksum': checksum
        }
        headers = {
            'Content-Type': 'application/json;charset=utf-8'
        }
        try:
            # 用 body_str 作为 data，确保与签名时的 JSON 完全一致
            body_bytes = body_str.encode('utf-8') if body_str else None
            resp = requests.post(url, params=params, data=body_bytes, headers=headers, timeout=30)
            result = resp.json()
            
            # API的message字段有时候是JSON字符串，需要二次解析
            if 'message' in result and isinstance(result['message'], str):
                try:
                    result['message'] = json.loads(result['message'])
                except (json.JSONDecodeError, TypeError):
                    pass
            
            return result
        except requests.RequestException as e:
            return {"code": -1, "message": str(e)}
        except json.JSONDecodeError:
            return {"code": -1, "message": f"响应解析失败: {resp.text[:200]}"}
        except OSError as e:
            return {"code": -1, "message": f"系统错误: {e}"}

    # ==================== 数据统计接口 ====================

    def get_overview(self, start_time: int, end_time: int, staff_ids: list = None) -> dict:
        """获取历史数据总览
        POST /openapi/statistic/overview
        """
        body = {
            "startTime": start_time,
            "endTime": end_time
        }
        if staff_ids:
            body["staffIds"] = staff_ids
        return self._post('/openapi/statistic/overview', body)

    def get_staff_workload(self, start_time: int, end_time: int,
                           model: int = 1, staff_id_list: list = None) -> dict:
        """获取客服工作量报表
        POST /openapi/statistic/staffworklod
        model: 1=全部(推荐,返回result数组), 2=客服组, 3=客服(可能返回14500)
        """
        body = {
            "startTime": start_time,
            "endTime": end_time,
            "model": model
        }
        if staff_id_list:
            body["staffIdList"] = staff_id_list
        return self._post('/openapi/statistic/staffworklod', body)

    def get_staff_quality(self, start_time: int, end_time: int,
                          model: int = 1, staff_id_list: list = None) -> dict:
        """获取客服质量报表
        POST /openapi/statistic/staffquality
        model: 1=全部(推荐), 3=客服(可能返回14500)
        """
        body = {
            "startTime": start_time,
            "endTime": end_time,
            "model": model
        }
        if staff_id_list:
            body["staffIdList"] = staff_id_list
        return self._post('/openapi/statistic/staffquality', body)

    def get_staff_attendance(self, start_time: int, end_time: int) -> dict:
        """获取坐席考勤/在线状态报表（上岗下岗时间、在线时长等）
        POST /openapi/statistic/staffAttendance
        返回每个坐席每天的登录/在线/挂起/小休等详细数据
        """
        body = {
            "startTime": start_time,
            "endTime": end_time
        }
        return self._post('/openapi/statistic/staffAttendance', body)

    def get_staff_satisfaction(self, start_time: int, end_time: int,
                               model: int = 1, staff_id_list: list = None) -> dict:
        """获取客服满意度报表
        POST /openapi/statistic/satisfaction/report
        model: 1=全部(推荐), 3=客服(可能返回14500)
        """
        body = {
            "startTime": start_time,
            "endTime": end_time,
            "model": model
        }
        if staff_id_list:
            body["staffIdList"] = staff_id_list
        return self._post('/openapi/statistic/satisfaction/report', body)

    # ==================== 会话查询接口 ====================

    def export_sessions(self, start_ms: int, end_ms: int) -> dict:
        """批量导出会话列表
        POST /openapi/export/session
        注意：该接口限定在凌晨2点-6点之间调用
        """
        body = {
            "start": str(start_ms),
            "end": str(end_ms)
        }
        return self._post('/openapi/export/session', body)

    def get_session_detail(self, session_id: int) -> dict:
        """根据会话ID获取会话详情
        POST /openapi/export/session/one
        返回格式: {"code":200, "message":"success", "data":{...}}
        """
        body = {"sessionId": session_id}
        return self._post('/openapi/export/session/one', body)

    def get_session_messages(self, session_id: int, m_types: str = None) -> dict:
        """根据会话ID获取会话消息
        POST /openapi/export/session/one/message
        返回格式: {"code":200, "message":"success", "data":[...]}
        """
        body = {"sessionId": session_id}
        if m_types:
            body["mTypes"] = m_types
        return self._post('/openapi/export/session/one/message', body)
