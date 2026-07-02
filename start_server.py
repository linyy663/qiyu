"""
服务守护启动脚本
自动重启 Flask 服务，确保持续运行
"""
import subprocess
import sys
import time
import os

PYTHON = sys.executable
APP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backend', 'app.py')
MAX_RESTARTS = 999
RESTART_DELAY = 3  # 秒

print(f"[守护进程] 启动服务: {APP_PATH}")
print(f"[守护进程] Python: {PYTHON}")
print(f"[守护进程] 访问地址: http://localhost:5890")
print("-" * 50)

restart_count = 0
while restart_count < MAX_RESTARTS:
    try:
        proc = subprocess.Popen([PYTHON, APP_PATH], cwd=os.path.dirname(APP_PATH))
        exit_code = proc.wait()
        restart_count += 1
        print(f"\n[守护进程] 服务退出 (code={exit_code})，{RESTART_DELAY}秒后第{restart_count}次重启...")
        time.sleep(RESTART_DELAY)
    except KeyboardInterrupt:
        print("\n[守护进程] 收到停止信号，关闭服务...")
        try:
            proc.terminate()
        except Exception:
            pass
        break
    except Exception as e:
        print(f"[守护进程] 错误: {e}")
        time.sleep(RESTART_DELAY)
