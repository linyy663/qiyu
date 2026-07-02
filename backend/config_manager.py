"""
网易七鱼配置管理模块
管理 AppKey、AppSecret 等配置的存储与读取
"""
import os
import json

CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.json')

DEFAULT_CONFIG = {
    "appKey": "",
    "appSecret": "",
    "baseUrl": "https://qiyukf.com",
    "autoRefresh": False,
    "refreshInterval": 300
}


def load_config() -> dict:
    """加载配置文件"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
            # 合并默认值
            for key, value in DEFAULT_CONFIG.items():
                if key not in config:
                    config[key] = value
            return config
        except Exception:
            return dict(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)


def save_config(config: dict) -> None:
    """保存配置文件"""
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def get_config_status() -> dict:
    """获取配置状态（不泄露Secret完整值）"""
    config = load_config()
    app_key = config.get('appKey', '')
    app_secret = config.get('appSecret', '')
    return {
        "configured": bool(app_key and app_secret),
        "appKey": app_key[:8] + '****' if len(app_key) > 8 else app_key,
        "appKeySet": bool(app_key),
        "appSecretSet": bool(app_secret),
        "baseUrl": config.get('baseUrl', 'https://qiyukf.com'),
        "autoRefresh": config.get('autoRefresh', False),
        "refreshInterval": config.get('refreshInterval', 300)
    }
