#!/usr/bin/env python3
"""GMS Auto Test Web Application - Flask 后端服务"""

import json
import os
import subprocess
import threading
import time
import queue
import configparser
import socket
import shlex
import traceback
import uuid
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, Response, session
from flask_socketio import SocketIO, emit, join_room, leave_room, rooms
from functools import wraps
import paramiko
from paramiko import AuthenticationException, SSHException

# Flask 应用
app = Flask(__name__)
app.config['SECRET_KEY'] = 'gms-auto-test-secret-key-2025'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading', manage_session=False)

# 全局状态
user_states = {}           # {client_id: {running, devices, logs, ...}}
user_states_lock = threading.Lock()

device_locks = {}          # {device_id: {client_id, user, timestamp}}
device_locks_lock = threading.Lock()

ssh_pool = queue.Queue(maxsize=5)  # SSH 连接池
ssh_lock = threading.Lock()

scrcpy_sessions = {}       # {device_id: {client_id, start_time}}
scrcpy_sessions_lock = threading.Lock()

# 设备列表缓存（3秒 TTL）
device_cache = {'devices': [], 'timestamp': 0, 'lock': threading.Lock()}
DEVICE_CACHE_TTL = 3

# ==================== USB/IP 状态管理 ====================
# USB/IP 连接状态（按客户端隔离）
usbip_states = {}  # {client_id: {'connected': bool, 'timestamp': float}}
usbip_states_lock = threading.Lock()

# USB/IP 设备来源记录（全局共享，支持多用户）
# 记录每个设备的来源主机，用于在设备管理界面区分 USB/IP 设备和本地直连设备
# {device_id: {'source': device_host, 'timestamp': float}}
usbip_devices_source = {}
usbip_devices_lock = threading.Lock()

# API 响应工具类
class ApiResponse:
    """统一的 API 响应格式"""

    @staticmethod
    def success(data=None, message="操作成功"):
        """成功响应"""
        response = {'success': True}
        if data is not None:
            response['data'] = data
        if message:
            response['message'] = message
        return jsonify(response)

    @staticmethod
    def error(error_message, status_code=500):
        """错误响应"""
        return jsonify({'success': False, 'error': error_message}), status_code

    @staticmethod
    def device_results(results, operation_name):
        """设备批量操作结果"""
        success_count = sum(1 for r in results if r.get('success', False))
        fail_count = len(results) - success_count
        return ApiResponse.success({
            'results': results,
            'summary': {'total': len(results), 'success': success_count, 'failed': fail_count}
        }, f"{operation_name}完成: 成功 {success_count} 台, 失败 {fail_count} 台")


# 错误处理装饰器
def handle_errors(operation_name="操作"):
    """统一错误处理"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            except SSHException as e:
                return ApiResponse.error(f"SSH连接失败: {str(e)}", 500)
            except AuthenticationException as e:
                return ApiResponse.error(f"认证失败: {str(e)}", 401)
            except ValueError as e:
                return ApiResponse.error(f"参数错误: {str(e)}", 400)
            except Exception as e:
                print(f"Error in {operation_name}: {str(e)}")
                traceback.print_exc()
                return ApiResponse.error(f"{operation_name}失败: {str(e)}", 500)
        return decorated_function
    return decorator


def handle_device_operation(operation_name="操作"):
    """设备操作错误处理"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            except SSHException as e:
                return ApiResponse.error(f"SSH连接失败: {str(e)}", 500)
            except AuthenticationException as e:
                return ApiResponse.error(f"认证失败: {str(e)}", 401)
            except ValueError as e:
                return ApiResponse.error(f"参数错误: {str(e)}", 400)
            except Exception as e:
                print(f"Error in {operation_name}: {str(e)}")
                traceback.print_exc()
                return ApiResponse.error(f"{operation_name}失败: {str(e)}", 500)
        return decorated_function
    return decorator


def execute_device_operation(devices, operation_func, operation_name):
    """批量执行设备操作"""
    results = []
    for device_id in devices:
        try:
            operation_result = operation_func(device_id)
            results.append({'device': device_id, 'success': True, 'data': operation_result})
        except Exception as e:
            results.append({'device': device_id, 'success': False, 'error': str(e)})
    return results


# 客户端标识管理
def get_client_id():
    """获取客户端标识 (username@ip)"""
    client_ip = request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or \
                request.headers.get('X-Real-IP') or request.remote_addr
    client_username = session.get('client_username', 'unknown')
    return f"{client_username}@{client_ip}"

def get_user_state():
    """获取当前用户状态"""
    client_id = get_client_id()
    with user_states_lock:
        if client_id not in user_states:
            user_states[client_id] = {
                'running': False, 'devices': [], 'logs': [],
                'ssh_connected': False, 'log_file': None,
                'test_type': 'cts', 'created_at': datetime.now().isoformat(),
                'client_id': client_id
            }
        return user_states[client_id]

def update_user_state(updates):
    """更新用户状态"""
    client_id = get_client_id()
    with user_states_lock:
        if client_id in user_states:
            user_states[client_id].update(updates)
            return user_states[client_id]
    return None

def get_user_state_by_id(client_id):
    """根据 ID 获取用户状态"""
    with user_states_lock:
        return user_states.get(client_id)

def cleanup_old_sessions():
    """清理超过 24 小时的旧会话"""
    with user_states_lock:
        now = datetime.now()
        expired_sessions = [
            cid for cid, state in user_states.items()
            if 'created_at' in state and
            (now - datetime.fromisoformat(state['created_at'])) > timedelta(hours=24)
        ]
        for cid in expired_sessions:
            del user_states[cid]

# 后台清理任务（每小时）
def cleanup_task():
    while True:
        time.sleep(3600)
        cleanup_old_sessions()

threading.Thread(target=cleanup_task, daemon=True).start()

# 多用户辅助函数
def emit_to_user(client_id, event, data):
    """向指定用户发送 Socket.IO 消息"""
    socketio.emit(event, data, room=client_id)

def append_user_log(client_id, log_message):
    """添加用户日志"""
    with user_states_lock:
        if client_id in user_states:
            user_states[client_id]['logs'].append(log_message)
            return True
    return False

def set_user_running(client_id, running):
    """设置用户测试运行状态"""
    with user_states_lock:
        if client_id in user_states:
            user_states[client_id]['running'] = running
            return True
    return False

# Socket.IO 事件处理
@socketio.on('connect')
def handle_connect():
    """客户端连接"""
    client_id = get_client_id()
    join_room(client_id)
    emit('connected', {'client_id': client_id})

@socketio.on('disconnect')
def handle_disconnect():
    """客户端断开时离开房间并释放占用的设备和终端SSH连接"""
    client_id = get_client_id()
    leave_room(client_id)

    # 检查用户是否正在运行测试
    user_state = get_user_state_by_id(client_id)
    test_running = user_state and user_state.get('running', False) if user_state else False

    # 释放该用户占用的所有设备（除非测试正在运行）
    with device_locks_lock:
        devices_to_release = [dev_id for dev_id, info in device_locks.items() if info.get('client_id') == client_id]
        for device_id in devices_to_release:
            # 如果测试正在运行，不要释放设备锁
            if not test_running:
                del device_locks[device_id]

    # 清理该用户的终端SSH连接（如果存在）
    with terminal_lock:
        if request.sid in terminal_ssh:
            try:
                terminal_ssh[request.sid]['ssh'].close()
                print(f"[TERMINAL] Closed SSH connection for socket {request.sid}")
            except Exception as e:
                print(f"[TERMINAL] Error closing SSH for socket {request.sid}: {e}")
            del terminal_ssh[request.sid]


# ==================== 设备锁定管理 ====================
def try_lock_devices(client_id, devices, user_info=''):
    """尝试锁定设备，返回成功和失败的设备列表"""
    locked_devices = []
    failed_devices = []

    with device_locks_lock:
        for device_id in devices:
            if device_id not in device_locks:
                # 设备未被占用，锁定它
                device_locks[device_id] = {
                    'client_id': client_id,
                    'user': user_info,
                    'timestamp': datetime.now().isoformat()
                }
                locked_devices.append(device_id)
            else:
                # 设备已被占用
                lock_info = device_locks[device_id]
                failed_devices.append({
                    'device_id': device_id,
                    'locked_by': lock_info.get('user', 'Unknown'),
                    'locked_at': lock_info.get('timestamp', '')
                })

    return locked_devices, failed_devices

def release_devices(client_id, devices):
    """释放指定设备"""
    with device_locks_lock:
        for device_id in devices:
            if device_id in device_locks and device_locks[device_id].get('client_id') == client_id:
                del device_locks[device_id]

def get_device_locks_status():
    """获取所有设备的锁定状态"""
    with device_locks_lock:
        return dict(device_locks)

# ==================== Configuration ====================
def load_config():
    """Load configuration from config.json and config_dynamic.json"""
    base_dir = os.path.dirname(__file__)

    # 加载静态配置
    config_path = os.path.join(base_dir, 'config.json')
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
            # Substitute ubuntu_user
            ubuntu_user = config.get('ubuntu_user', 'hcq')
            for key, value in config.items():
                if isinstance(value, str) and '${ubuntu_user}' in value:
                    config[key] = value.replace('${ubuntu_user}', ubuntu_user)
    except Exception as e:
        print(f"Error loading config: {e}")
        config = {}

    # 加载动态配置
    dynamic_path = os.path.join(base_dir, 'config_dynamic.json')
    try:
        with open(dynamic_path, 'r', encoding='utf-8') as f:
            dynamic_config = json.load(f)
            config.update(dynamic_config)
    except FileNotFoundError:
        pass  # 动态配置文件不存在时使用默认值
    except Exception as e:
        print(f"Error loading dynamic config: {e}")

    return config

def save_config(config):
    """Save configuration to config.json"""
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Error saving config: {e}")
        return False

def save_dynamic_config(dynamic_config):
    """Save dynamic configuration to config_dynamic.json"""
    dynamic_path = os.path.join(os.path.dirname(__file__), 'config_dynamic.json')
    try:
        with open(dynamic_path, 'w', encoding='utf-8') as f:
            json.dump(dynamic_config, f, indent=4, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Error saving dynamic config: {e}")
        return False

# ==================== Test Log Management ====================
def save_test_logs(test_type, client_id, exit_code=None):
    """保存测试日志到文件"""
    user_state = get_user_state_by_id(client_id)
    if not user_state:
        print(f"[ERROR] Session {client_id} not found")
        return None

    # 创建 logs 目录
    logs_dir = os.path.join(os.path.dirname(__file__), 'logs')
    os.makedirs(logs_dir, exist_ok=True)

    # 生成日志文件名（带时间戳和用户标识）
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    user_short_id = client_id[:8]
    log_filename = f"{test_type}_{timestamp}_{user_short_id}.log"
    log_path = os.path.join(logs_dir, log_filename)

    try:
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write(f"GMS 测试日志 - {test_type.upper()}\n")
            f.write(f"保存时间: {timestamp}\n")
            f.write(f"用户ID: {user_short_id}\n")
            f.write(f"退出代码: {exit_code if exit_code is not None else '未知'}\n")
            f.write("=" * 80 + "\n\n")

            # 写入所有日志条目
            for log_entry in user_state['logs']:
                f.write(log_entry + '\n')

        # 更新 user_state
        with user_states_lock:
            user_state['log_file'] = log_path

        return log_path

    except Exception as e:
        print(f"[ERROR] 保存日志失败: {e}")
        return None

# ==================== SSH Connection Management ====================
def create_ssh_connection(config):
    """Create and return a new SSH connection"""
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())


        if config.get('use_key_auth', False):
            key_path = os.path.expanduser(config.get('private_key_path', '~/.ssh/id_rsa'))
            key = paramiko.RSAKey.from_private_key_file(key_path)
            ssh.connect(
                config['ubuntu_host'],
                username=config['ubuntu_user'],
                pkey=key,
                timeout=10
            )
        else:
            password = config.get('ubuntu_pswd', '')
            if not password:
                print("[ERROR] No SSH password configured in config.json")
                return None
            ssh.connect(
                config['ubuntu_host'],
                username=config['ubuntu_user'],
                password=password,
                timeout=10
            )

        return ssh
    except Exception as e:
        print(f"[ERROR] SSH connection error: {e}")
        return None

def get_ssh_connection(config):
    """Get an SSH connection from pool or create new one"""
    try:
        return ssh_pool.get_nowait()
    except queue.Empty:
        return create_ssh_connection(config)

def return_ssh_connection(ssh):
    """Return SSH connection to pool"""
    try:
        ssh_pool.put_nowait(ssh)
    except queue.Full:
        ssh.close()

def execute_ssh_command(ssh, command, timeout=30):
    """Execute command on remote server via SSH"""
    try:
        stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
        output = stdout.read().decode('utf-8', errors='ignore')
        error = stderr.read().decode('utf-8', errors='ignore')
        return output, error, stdout.channel.recv_exit_status()
    except Exception as e:
        return "", str(e), -1

def create_device_ssh_connection(config):
    """Create SSH connection to device host (Windows)"""
    device_host = config.get('device_host', '')
    if not device_host:
        return None

    if '@' not in device_host:
        print("[ERROR] Device host format should be user@host")
        return None

    username, hostname = device_host.split('@', 1)
    password = config.get('device_pswd', '')

    if not password:
        print("[ERROR] No SSH password configured for device host")
        return None

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=hostname, username=username, password=password, timeout=10)
        return ssh
    except Exception as e:
        print(f"[ERROR] Failed to connect to device host: {e}")
        return None

def is_windows_host(ssh):
    """Check if SSH host is Windows"""
    try:
        stdin, stdout, stderr = ssh.exec_command('ver 2>&1', timeout=3)
        output = stdout.read().decode('utf-8', errors='ignore').lower()
        return 'microsoft' in output or 'windows' in output
    except:
        return False

def find_device_host_password(config, device_host):
    """从 client_ssh_credentials 中查找对应 device_host 的密码"""
    if '@' not in device_host:
        return None

    username, hostname = device_host.split('@', 1)

    # 从 client_ssh_credentials 中查找匹配的凭据
    for cred in config.get('client_ssh_credentials', []):
        if cred.get('username') == username:
            print(f"[USB/IP] Found SSH credential for username={username}")
            return cred.get('password')

    print(f"[USB/IP] No SSH credential found for {device_host}")
    return None

# ==================== Device Management ====================
def get_connected_devices(config, force_refresh=False):
    """
    Get list of connected Android devices
    使用缓存来减少SSH调用次数
    """
    current_time = time.time()

    # 检查缓存是否有效
    with device_cache['lock']:
        if not force_refresh and (current_time - device_cache['timestamp']) < DEVICE_CACHE_TTL:
            return device_cache['devices']

    # 缓存失效，重新获取设备列表
    ssh = get_ssh_connection(config)
    if not ssh:
        print("[ERROR] Failed to get SSH connection")
        return []

    try:
        output, error, code = execute_ssh_command(ssh, "adb devices")
        if error and code != 0:
            pass

        devices = []
        for line in output.split('\n')[1:]:
            if line.strip() and '\tdevice' in line:
                device_id = line.split('\t')[0]
                devices.append(device_id)
        return_ssh_connection(ssh)

        # 更新缓存
        with device_cache['lock']:
            device_cache['devices'] = devices
            device_cache['timestamp'] = current_time

        return devices
    except Exception as e:
        print(f"[ERROR] Error getting devices: {e}")
        return []

# ==================== Test Execution ====================
def run_test_suite(config, test_params, client_id):
    """Run GMS test suite with full parameter support - matches GUI implementation"""
    user_state = get_user_state_by_id(client_id)
    if not user_state:
        print(f"[ERROR] Session {client_id} not found in run_test_suite")
        return


    # 从 client_id 解析 device_host (格式: username@ip)
    config['device_host'] = client_id

    user_state['running'] = True
    user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] Starting test suite...")
    socketio.emit('log_update', {'log': 'Starting test suite...'}, room=client_id)

    try:
        ssh = get_ssh_connection(config)
        if not ssh:
            user_state['logs'].append("[ERROR] Failed to establish SSH connection")
            user_state['running'] = False
            return

        # Step 1: Upload script to remote server
        # Script is in tools directory
        local_script = os.path.join(os.path.dirname(__file__), 'tools', 'run_GMS_Test_Auto.sh')
        # Upload to GMS-Suite directory on remote server
        suites_path = config.get('suites_path', '').replace('${ubuntu_user}', config['ubuntu_user'])
        remote_script = os.path.join(suites_path, 'run_GMS_Test_Auto.sh')

        if not os.path.exists(local_script):
            user_state['logs'].append(f"[ERROR] Local script not found: {local_script}")
            user_state['running'] = False
            return

        # Upload script via SFTP
        script_size = os.path.getsize(local_script)
        size_kb = script_size / 1024
        user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] 📤 上传文件: run_GMS_Test_Auto.sh → {remote_script} ({size_kb:.2f}KB)")
        socketio.emit('log_update', {'log': f"📤 上传文件: run_GMS_Test_Auto.sh → {remote_script} ({size_kb:.2f}KB)"}, room=client_id)

        try:
            sftp = ssh.open_sftp()
            sftp.put(local_script, remote_script)
            sftp.close()

            # Set executable permission
            chmod_cmd = f"chmod +x '{remote_script}'"
            execute_ssh_command(ssh, chmod_cmd)

            user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] 🔐 已设置可执行权限: {remote_script}")
            socketio.emit('log_update', {'log': f"🔐 已设置可执行权限: {remote_script}"}, room=client_id)
            user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ 上传完成 ({size_kb:.2f}KB)")
            socketio.emit('log_update', {'log': f"✅ 上传完成 ({size_kb:.2f}KB)"}, room=client_id)

        except Exception as e:
            user_state['logs'].append(f"[ERROR] Failed to upload script: {str(e)}")
            user_state['running'] = False
            return_ssh_connection(ssh)
            return

        user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ SSH 连接成功")
        socketio.emit('log_update', {'log': '✅ SSH 连接成功'}, room=client_id)

        # Extract parameters
        test_type = test_params.get('test_type', 'cts')
        test_module = test_params.get('test_module', '')
        test_case = test_params.get('test_case', '')
        retry_dir = test_params.get('retry_dir', '')
        test_suite = test_params.get('test_suite', '')
        local_server = test_params.get('local_server', '')
        devices = test_params.get('devices', [])

        # Build command parts (matching GUI lines 1472-1522)
        cmd_parts = [remote_script]

        # Add retry mode or normal mode
        if retry_dir:
            timestamp = os.path.basename(retry_dir.strip().rstrip('/'))
            cmd_parts.extend([test_type, "retry", timestamp])
            user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] Retry mode: {timestamp}")
        else:
            cmd_parts.append(test_type)
            if test_module:
                cmd_parts.append(test_module)
                user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] Test module: {test_module}")
            if test_case:
                cmd_parts.append(test_case)
                user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] Test case: {test_case}")

        # Add device arguments
        if devices:
            device_args_list = []
            if len(devices) > 1:
                device_args_list.extend(["--shard-count", str(len(devices))])
                user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] Sharding across {len(devices)} devices")
            for device in devices:
                device_args_list.extend(["-s", device])

            device_args_str = " ".join(device_args_list)
            cmd_parts.extend(["--device-args", device_args_str])
            user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] Devices: {', '.join(devices)}")

        # Add test suite path
        if test_suite:
            cmd_parts.extend(["--test-suite", test_suite])
            user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] 📂 测试套件: {test_suite}")
            socketio.emit('log_update', {'log': f"📂 测试套件: {test_suite}"}, room=client_id)

        # Add local server
        if local_server:
            cmd_parts.extend(["--local-server", local_server])
            user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] 🌐 本地主机: {local_server}")
            socketio.emit('log_update', {'log': f"🌐 本地主机: {local_server}"}, room=client_id)

        # Build final command
        command = ' '.join(shlex.quote(part) for part in cmd_parts)
        command = f"cd {os.path.dirname(remote_script)} && {command}"

        user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] 🚀 执行命令: {command}")
        socketio.emit('log_update', {'log': f"🚀 执行命令: {command}"}, room=client_id)

        # Execute with real-time output (matching GUI.py logic)
        stdin, stdout, stderr = ssh.exec_command(command, get_pty=True)

        # Real-time output reading loop (matching GUI lines 2420-2429)
        while not stdout.channel.exit_status_ready() and user_state['running']:
            if stdout.channel.recv_ready():
                data = stdout.channel.recv(4096).decode('utf-8', errors='replace')
                if data:
                    for line in data.splitlines():
                        if line.strip():
                            log_line = line.strip()
                            # Send raw log line to frontend (without timestamp prefix)
                            socketio.emit('log_update', {'log': log_line}, room=client_id)
                            # Store with timestamp for history
                            user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] {log_line}")

            if stderr.channel.recv_stderr_ready():
                error = stderr.channel.recv_stderr(4096).decode('utf-8', errors='replace')
                if error:
                    for line in error.splitlines():
                        if line.strip():
                            log_line = line.strip()
                            socketio.emit('log_update', {'log': log_line}, room=client_id)
                            user_state['logs'].append(f"[STDERR] {log_line}")

            time.sleep(0.1)

        # Get final exit status
        exit_status = stdout.channel.recv_exit_status()
        return_ssh_connection(ssh)

        if exit_status == 0:
            user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Test completed successfully")
            socketio.emit('log_update', {'log': f"✅ Test completed successfully (exit code: {exit_status})"}, room=client_id)
        else:
            user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Test failed with exit code {exit_status}")
            socketio.emit('log_update', {'log': f"❌ Test failed with exit code {exit_status}"}, room=client_id)

        # 保存测试日志
        log_file = save_test_logs(test_type, client_id, exit_status)
        if log_file:
            user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] 📁 日志已保存: {log_file}")
            socketio.emit('log_update', {'log': f"📁 日志已保存: {log_file}"}, room=client_id)

    except Exception as e:
        error_msg = f"[ERROR] {str(e)}"
        user_state['logs'].append(error_msg)
        socketio.emit('log_update', {'log': error_msg}, room=client_id)
        # Print full traceback to server logs
        print(f"[ERROR] Exception in run_test_suite:")
        print(traceback.format_exc())

        # 异常时也保存日志
        log_file = save_test_logs(test_type, client_id, None)
        if log_file:
            user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] 📁 日志已保存: {log_file}")
            socketio.emit('log_update', {'log': f"📁 日志已保存: {log_file}"}, room=client_id)

    # Release devices when test completes
    devices_to_release = test_params.get('devices', [])
    release_devices(client_id, devices_to_release)

    user_state['running'] = False
    user_state['devices'] = []
    socketio.emit('test_complete', {}, room=client_id)

# ==================== Routes ====================
@app.route('/')
def index():
    """Main page"""
    config = load_config()
    return render_template('index.html', config=config)

@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    """Get or update configuration"""
    if request.method == 'GET':
        return jsonify(load_config())
    else:
        new_config = request.json
        # Preserve existing SSH passwords if not provided in update
        existing_config = load_config()
        for key in ['ubuntu_pswd', 'device_pswd']:
            if key not in new_config or new_config.get(key, '') == '':
                if key in existing_config:
                    new_config[key] = existing_config[key]
        if save_config(new_config):
            return jsonify({'success': True})
        return jsonify({'success': False, 'error': 'Failed to save config'}), 500

@app.route('/api/client-info', methods=['GET', 'POST'])
def handle_client_info():
    """获取客户端IP或记录客户端信息"""
    # 获取客户端IP
    client_ip = request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or \
                request.headers.get('X-Real-IP') or request.remote_addr

    if request.method == 'GET':
        return jsonify({'ip': client_ip})

    # POST: 记录客户端信息
    data = request.json
    client_username = data.get('username', 'unknown')

    # 更新 session 中的用户名
    session['client_username'] = client_username
    session['client_ip'] = client_ip

    user_state = get_user_state()
    user_state.update({
        'client_ip': client_ip,
        'client_username': client_username,
        'last_seen': datetime.now().isoformat()
    })
    print(f"[ClientInfo] IP: {client_ip} | Username: {client_username}")
    return jsonify({'success': True, 'client_id': get_client_id()})

@app.route('/api/client-info/detect', methods=['POST'])
def detect_client():
    """自动检测客户端用户名（支持手动SSH凭据）"""
    data = request.json
    client_ip = data.get('ip') or request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or request.remote_addr
    config = load_config()

    # 手动SSH凭据
    if data.get('username') and data.get('password'):
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(client_ip, username=data['username'], password=data['password'], timeout=10)
            username = ssh.exec_command('whoami')[1].read().decode().strip().split('\\')[-1]
            ssh.close()
            # 保存凭据到动态配置
            config.setdefault('client_hosts', {})[client_ip] = username
            creds = config.setdefault('client_ssh_credentials', [])
            if not any(c.get('username') == data['username'] for c in creds):
                creds.insert(0, {'username': data['username'], 'password': data['password']})
            config['device_host'] = f'{username}@{client_ip}'
            # 只保存动态配置
            dynamic_config = {
                'device_host': config['device_host'],
                'device_pswd': config.get('device_pswd', ''),
                'client_hosts': config['client_hosts'],
                'client_ssh_credentials': config['client_ssh_credentials']
            }
            save_dynamic_config(dynamic_config)
            print(f"[ClientInfo] Updated device_host: {config['device_host']}")
            return jsonify({'success': True, 'username': username})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 401

    # 检查已保存的映射
    username = config.get('client_hosts', {}).get(client_ip)
    if username:
        config['device_host'] = f'{username}@{client_ip}'
        dynamic_config = {
            'device_host': config['device_host'],
            'device_pswd': config.get('device_pswd', ''),
            'client_hosts': config['client_hosts'],
            'client_ssh_credentials': config['client_ssh_credentials']
        }
        save_dynamic_config(dynamic_config)
        print(f"[ClientInfo] Updated device_host: {config['device_host']}")
        return jsonify({'success': True, 'username': username})

    # 尝试已保存的SSH凭据
    for cred in config.get('client_ssh_credentials', []):
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(client_ip, username=cred['username'], password=cred['password'], timeout=5)
            username = ssh.exec_command('whoami')[1].read().decode().strip().split('\\')[-1]
            ssh.close()
            config.setdefault('client_hosts', {})[client_ip] = username
            config['device_host'] = f'{username}@{client_ip}'
            dynamic_config = {
                'device_host': config['device_host'],
                'device_pswd': config.get('device_pswd', ''),
                'client_hosts': config['client_hosts'],
                'client_ssh_credentials': config['client_ssh_credentials']
            }
            save_dynamic_config(dynamic_config)
            print(f"[ClientInfo] Updated device_host: {config['device_host']}")
            return jsonify({'success': True, 'username': username})
        except: pass

    return jsonify({'success': False, 'error': '请提供SSH凭据'})


@app.route('/api/users')
def list_users():
    """获取所有在线用户列表"""
    with user_states_lock:
        users = []
        now = datetime.now()

        for client_id, state in user_states.items():
            # 检查会话是否活跃（最近24小时内有活动）
            if 'last_seen' in state:
                last_seen = datetime.fromisoformat(state['last_seen'])
                if (now - last_seen) > timedelta(hours=24):
                    continue

            # 解析client_id (username@ip)
            parts = client_id.split('@')
            username = parts[0] if len(parts) > 0 else 'unknown'
            ip = parts[1] if len(parts) > 1 else 'unknown'

            user_info = {
                'client_id': client_id,
                'username': username,
                'ip': ip,
                'running': state.get('running', False),
                'devices': state.get('devices', []),
                'last_seen': state.get('last_seen', ''),
                'created_at': state.get('created_at', '')
            }
            users.append(user_info)

        return jsonify({
            'total': len(users),
            'users': users
        })


@app.route('/api/devices')
def list_devices():
    """Get list of connected devices with lock status"""
    config = load_config()

    # 检查是否需要强制刷新（绕过缓存）
    force_refresh = request.args.get('force_refresh', '0') == '1'
    devices = get_connected_devices(config, force_refresh=force_refresh)

    # 获取当前会话ID和设备锁定状态
    client_id = get_client_id()
    locks = get_device_locks_status()

    # 为每个设备添加锁定状态信息
    devices_with_status = []
    for device in devices:
        # Handle both string device IDs and dict devices
        if isinstance(device, str):
            device_id = device
            device_dict = {'device_id': device_id, 'model': ''}
        else:
            device_id = device.get('device_id', '')
            device_dict = device

        lock_info = locks.get(device_id, {})

        device_with_status = {
            **device_dict,
            'locked': device_id in locks,
            'locked_by': lock_info.get('user', ''),
            'locked_by_self': lock_info.get('client_id') == client_id if device_id in locks else False
        }
        devices_with_status.append(device_with_status)

    return jsonify(devices_with_status)

@app.route('/api/devices/locks')
def get_device_locks():
    """获取所有设备的锁定状态"""
    locks = get_device_locks_status()

    # 转换为前端友好的格式
    lock_list = []
    for device_id, info in locks.items():
        lock_list.append({
            'device_id': device_id,
            'user': info.get('user', ''),
            'timestamp': info.get('timestamp', '')
        })

    return jsonify(lock_list)

@app.route('/api/test/start', methods=['POST'])
def start_test():
    """Start test execution - matches GUI with full parameter support"""

    # 获取当前用户状态
    user_state = get_user_state()
    if user_state['running']:
        return jsonify({'success': False, 'error': '您已有测试正在运行'}), 400

    data = request.json
    devices = data.get('devices', [])

    if not devices:
        return jsonify({'success': False, 'error': 'No devices selected'}), 400

    # 检查设备锁定状态
    client_id = get_client_id()
    user_info = f"用户{client_id[:8]}"  # 简短的用户标识
    locked_devices, failed_devices = try_lock_devices(client_id, devices, user_info)

    if failed_devices:
        # 有设备被占用，返回错误信息
        error_msg = "以下设备已被其他用户占用：\n"
        for fail in failed_devices:
            error_msg += f"- {fail['device_id']} (被 {fail['locked_by']} 占用)\n"

        # 释放已锁定的设备
        release_devices(client_id, locked_devices)

        return jsonify({
            'success': False,
            'error': error_msg.strip(),
            'failed_devices': failed_devices
        }), 409  # 409 Conflict

    # Extract all test parameters (matching GUI lines 2297-2442)
    test_params = {
        'test_type': data.get('test_type', 'cts'),
        'test_module': data.get('test_module', ''),
        'test_case': data.get('test_case', ''),
        'retry_dir': data.get('retry_dir', ''),
        'test_suite': data.get('test_suite', ''),
        'local_server': data.get('local_server', ''),
        'devices': devices,
        'client_id': client_id
    }

    config = load_config()

    # Save test_type for stop_test function
    update_user_state({'test_type': test_params['test_type']})

    # Start test in background thread with all parameters
    test_thread = threading.Thread(
        target=run_test_suite,
        args=(config, test_params, client_id)
    )
    test_thread.daemon = True
    test_thread.start()

    # Update user state to mark test as running and save devices
    update_user_state({'running': True, 'devices': devices})
    return jsonify({'success': True, 'message': 'Test started'})

@app.route('/api/test/stop', methods=['POST'])
def stop_test():
    """Stop test execution - matches GUI by actually killing tradefed processes"""
    user_state = get_user_state()
    client_id = get_client_id()

    user_state['running'] = False
    user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] ⏹️ 用户请求停止测试...")
    socketio.emit('log_update', {'log': '⏹️ 用户请求停止测试...'}, room=client_id)

    # Release devices when stopping test
    devices_to_release = user_state.get('devices', [])
    release_devices(client_id, devices_to_release)
    user_state['devices'] = []

    config = load_config()
    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        # Kill tradefed processes (matching GUI lines 2444-2483)
        test_type = user_state.get('test_type', 'cts')

        # Map test type to binary name
        binary_map = {
            'cts': 'cts-tradefed',
            'gsi': 'cts-tradefed',
            'gts': 'gts-tradefed',
            'sts': 'sts-tradefed',
            'xts': 'xts-tradefed'
        }

        tradefed_bin = binary_map.get(test_type, 'tradefed')
        kill_cmd = f"pkill -f '[./]?{tradefed_bin}.*run commandAndExit'"

        user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] 🧹 正在终止 {test_type.upper()} 测试进程...")
        socketio.emit('log_update', {'log': f"🧹 正在终止 {test_type.upper()} 测试进程..."}, room=client_id)
        output, error, code = execute_ssh_command(ssh, kill_cmd)

        return_ssh_connection(ssh)

        if code == 0:
            user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ {test_type.upper()} tradefed 进程已成功终止")
            socketio.emit('log_update', {'log': f"✅ {test_type.upper()} tradefed 进程已成功终止"}, room=client_id)
        else:
            user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ 未找到运行中的测试进程或终止失败")
            socketio.emit('log_update', {'log': '⚠️ 未找到运行中的测试进程或终止失败'}, room=client_id)

        return jsonify({
            'success': True,
            'message': '测试已停止'
        })
    except Exception as e:
        return_ssh_connection(ssh)
        user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ 停止测试时出错: {str(e)}")
        socketio.emit('log_update', {'log': f"❌ 停止测试时出错: {str(e)}"}, room=client_id)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/test/clean', methods=['POST'])
def clean_test():
    """Clean test logs"""
    user_state = get_user_state()
    user_state['logs'] = []
    return jsonify({'success': True})

@app.route('/api/test/logs/download')
def download_logs():
    """下载当前测试日志文件"""
    user_state = get_user_state()

    log_file = user_state.get('log_file')
    if not log_file or not os.path.exists(log_file):
        return jsonify({'success': False, 'error': 'No log file available'}), 404

    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            log_content = f.read()

        filename = os.path.basename(log_file)
        return Response(
            log_content,
            mimetype='text/plain',
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"'
            }
        )
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/test/logs/save-current', methods=['POST'])
def save_current_logs():
    """立即保存当前测试日志（从前端获取实际日志内容）"""
    user_state = get_user_state()
    client_id = get_client_id()

    try:
        # 从前端获取日志内容和测试类型
        data = request.json
        log_content = data.get('content', '')
        test_type = data.get('test_type', 'unknown')

        if not log_content:
            return jsonify({'success': False, 'error': 'No log content provided'}), 400

        # 创建 logs 目录
        logs_dir = os.path.join(os.path.dirname(__file__), 'logs')
        os.makedirs(logs_dir, exist_ok=True)

        # 生成日志文件名（带时间戳和用户标识）
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        user_short_id = client_id[:8]
        log_filename = f"{test_type}_{timestamp}_{user_short_id}.log"
        log_path = os.path.join(logs_dir, log_filename)

        # 写入日志内容
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write(f"GMS 测试日志 - {test_type.upper()}\n")
            f.write(f"保存时间: {timestamp}\n")
            f.write(f"用户ID: {user_short_id}\n")
            f.write("=" * 80 + "\n\n")
            f.write(log_content)

        # 更新 user_state
        user_state['log_file'] = log_path

        return jsonify({
            'success': True,
            'log_file': log_path,
            'filename': log_filename
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/test/logs/list')
def list_logs():
    """列出所有可用的测试日志文件"""
    try:
        logs_dir = os.path.join(os.path.dirname(__file__), 'logs')
        if not os.path.exists(logs_dir):
            return jsonify({'logs': []})

        log_files = []
        for filename in os.listdir(logs_dir):
            if filename.endswith('.log'):
                filepath = os.path.join(logs_dir, filename)
                stat = os.stat(filepath)
                log_files.append({
                    'filename': filename,
                    'size': stat.st_size,
                    'modified': stat.st_mtime
                })

        # 按修改时间降序排列
        log_files.sort(key=lambda x: x['modified'], reverse=True)
        return jsonify({'logs': log_files})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/status')
def get_status():
    """Get current test status - 优化版本，减少数据传输"""
    user_state = get_user_state()

    # 获取请求参数，只返回需要的数据
    since = request.args.get('since', type=int)
    include_logs = request.args.get('logs', 'true').lower() == 'true'

    response = {
        'running': user_state['running'],
        'devices': user_state['devices'],
    }

    # 只在需要时返回日志，并且基于since参数返回增量日志
    if include_logs:
        logs = user_state['logs']
        if since is not None and 0 <= since < len(logs):
            # 只返回新日志（增量）
            response['logs'] = logs[since:]
            response['log_count'] = len(logs)
        else:
            # 返回最近50条日志（从100减少到50）
            response['logs'] = logs[-50:]
            response['log_count'] = len(logs)

    return jsonify(response)

@app.route('/api/devices/reboot', methods=['POST'])
def reboot_devices():
    """Reboot selected devices and wait for them to come back online"""
    data = request.json
    devices = data.get('devices', [])
    config = load_config()

    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        results = []
        import time

        for device_id in devices:
            try:
                # 1. 执行重启命令
                output, error, code = execute_ssh_command(ssh, f"adb -s {device_id} reboot")
                reboot_success = code == 0

                if not reboot_success:
                    results.append({
                        'device': device_id,
                        'success': False,
                        'error': error or '重启命令执行失败'
                    })
                    continue

                # 2. 等待设备重新上线（最多60秒）
                start_time = time.time()
                device_back_online = False
                wait_time = 0

                while time.time() - start_time < 60:  # 等待最多60秒
                    # 检查设备状态
                    check_cmd = f"adb -s {device_id} get-state"
                    check_output, _, check_code = execute_ssh_command(ssh, check_cmd)

                    if 'device' in check_output.lower():
                        device_back_online = True
                        wait_time = time.time() - start_time
                        break

                    time.sleep(2)  # 每2秒检查一次

                results.append({
                    'device': device_id,
                    'success': True,
                    'back_online': device_back_online,
                    'wait_time': round(wait_time, 1)
                })

            except Exception as e:
                results.append({
                    'device': device_id,
                    'success': False,
                    'error': str(e)
                })

        return_ssh_connection(ssh)
        return jsonify({'success': True, 'results': results})
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/devices/remount', methods=['POST'])
def remount_devices():
    """Remount selected devices with root and veritymode check"""
    data = request.json
    devices = data.get('devices', [])
    config = load_config()

    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        results = []
        for device_id in devices:
            try:
                # 1. 执行 adb root
                root_cmd = f"adb -s {device_id} root"
                root_output, root_error, root_code = execute_ssh_command(ssh, root_cmd)

                # 等待 root 完成
                import time
                time.sleep(2)

                # 2. 执行 remount
                remount_cmd = f"adb -s {device_id} remount"
                remount_output, remount_error, remount_code = execute_ssh_command(ssh, remount_cmd)

                # 3. 检查 veritymode
                verity_cmd = f"adb -s {device_id} shell getprop ro.boot.veritymode"
                verity_output, _, _ = execute_ssh_command(ssh, verity_cmd)
                verity_mode = verity_output.strip()

                # 判断是否需要重启
                needs_reboot = 'enforcing' in verity_mode or verity_mode not in ['disabled', '']

                result = {
                    'device': device_id,
                    'success': remount_code == 0,
                    'verity_mode': verity_mode,
                    'needs_reboot': needs_reboot,
                    'output': remount_output[-200:] if remount_output else remount_error
                }

                if needs_reboot:
                    result['warning'] = '设备处于 enforcing 模式，需要重启才能使 remount 生效'

                results.append(result)
            except Exception as e:
                results.append({
                    'device': device_id,
                    'success': False,
                    'error': str(e)
                })

        return_ssh_connection(ssh)
        return jsonify({'success': True, 'results': results})
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/devices/connect-wifi', methods=['POST'])
def connect_wifi():
    """Connect devices to WiFi"""
    data = request.json
    devices = data.get('devices', [])
    ssid = data.get('ssid', 'AndroidWifi')
    password = data.get('password', '1234567890')
    config = load_config()

    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        results = []
        for device_id in devices:
            # Enable WiFi and connect to network
            enable_cmd = f"adb -s {device_id} shell cmd wifi set-wifi-enabled enabled"
            connect_cmd = f'adb -s {device_id} shell cmd wifi connect-network "{ssid}" wpa2 "{password}"'
            full_cmd = f"{enable_cmd} && sleep 2 && {connect_cmd}"

            output, error, code = execute_ssh_command(ssh, full_cmd)
            results.append({'device': device_id, 'success': code == 0})
        return_ssh_connection(ssh)
        return jsonify({'success': True, 'results': results})
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/devices/lock', methods=['POST'])
def lock_devices():
    """Lock or unlock selected devices using run_Device_Lock.sh script"""
    data = request.json
    devices = data.get('devices', [])
    action = data.get('action', 'lock')  # 'lock' or 'unlock'
    config = load_config()

    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        results = []

        # 本地脚本路径 - 动态获取当前用户主目录
        local_script = os.path.join(os.path.expanduser('~'), 'GMS_Auto_Test', 'run_Device_Lock.sh')
        # 远程脚本路径
        remote_script = f"/home/{config['ubuntu_user']}/GMS-Suite/run_Device_Lock.sh"

        # 检查本地脚本是否存在
        import os
        if not os.path.exists(local_script):
            return jsonify({
                'success': False,
                'error': f'脚本文件不存在: {local_script}'
            }), 404

        # 上传脚本到远程服务器
        try:
            sftp = ssh.open_sftp()
            sftp.put(local_script, remote_script)
            sftp.close()
            # 设置执行权限
            execute_ssh_command(ssh, f"chmod +x '{remote_script}'")
        except Exception as e:
            return jsonify({
                'success': False,
                'error': f'上传脚本失败: {str(e)}'
            }), 500

        # 对每个设备执行锁定/解锁操作
        for device_id in devices:
            try:
                # 执行脚本
                cmd = f"bash '{remote_script}' '{device_id}' '{action}'"
                output, error, code = execute_ssh_command(ssh, cmd)

                # 等待设备重新上线
                if code == 0:
                    import time
                    start_time = time.time()
                    while time.time() - start_time < 60:  # 等待最多60秒
                        check_cmd = f"adb -s {device_id} get-state"
                        check_output, _, check_code = execute_ssh_command(ssh, check_cmd)
                        if 'device' in check_output.lower():
                            break
                        time.sleep(2)

                results.append({
                    'device': device_id,
                    'success': code == 0,
                    'output': output[-200:] if output else error
                })
            except Exception as e:
                results.append({
                    'device': device_id,
                    'success': False,
                    'error': str(e)
                })

        return_ssh_connection(ssh)
        return jsonify({'success': True, 'results': results})
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/devices/lock-status', methods=['POST'])
def check_lock_status():
    """Check verified boot lock status of selected devices"""
    data = request.json
    devices = data.get('devices', [])
    config = load_config()

    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        results = []
        for device_id in devices:
            # Check verified boot state (GREEN = locked, ORANGE = unlocked)
            output, error, code = execute_ssh_command(
                ssh,
                f"adb -s {device_id} shell getprop ro.boot.verifiedbootstate"
            )
            state = output.strip()

            # 根据状态判断是否锁定
            if state == 'green':
                is_locked = True
                status_text = '已锁定 (GREEN)'
            elif state == 'orange':
                is_locked = False
                status_text = '未锁定 (ORANGE)'
            elif state == 'yellow':
                is_locked = False
                status_text = '未锁定 (YELLOW)'
            else:
                is_locked = False
                status_text = f'未知状态 ({state})'

            results.append({
                'device': device_id,
                'locked': is_locked,
                'state': state,
                'status': status_text
            })
        return_ssh_connection(ssh)
        return jsonify({'success': True, 'results': results})
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/devices/info', methods=['POST'])
def get_device_info():
    """Get device information - matches GUI implementation with 15 specific properties"""
    data = request.json
    devices = data.get('devices', [])
    config = load_config()

    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        # Define info commands matching GUI (lines 1677-1732 in GMS_Auto_Test_GUI.py)
        info_commands = [
            ("设备序列号", "adb -s {device} shell getprop ro.serialno"),
            ("设备型号", "adb -s {device} shell getprop ro.product.model"),
            ("Android版本", "adb -s {device} shell getprop ro.build.version.release"),
            ("编译类型", "adb -s {device} shell getprop ro.build.type"),
            ("编译标签", "adb -s {device} shell getprop ro.build.tags"),
            ("编译时间", "adb -s {device} shell getprop ro.build.date"),
            ("SDK版本", "adb -s {device} shell getprop ro.build.version.sdk"),
            ("DATA分区", "adb -s {device} shell cat vendor/etc/fstab.rk30board | grep userdata"),
            ("API级别", "adb -s {device} shell getprop | grep api_level"),
            ("Mali库版本", "adb -s {device} shell getprop sys.gmali.version"),
            ("安全补丁", "adb -s {device} shell getprop ro.build.version.security_patch"),
            ("指纹", "adb -s {device} shell getprop ro.build.fingerprint"),
            ("内存信息", "adb -s {device} shell cat /proc/meminfo | grep -E 'MemTotal|MemFree'"),
            ("时区设置", "adb -s {device} shell getprop persist.sys.timezone"),
            ("语言设置", "adb -s {device} shell getprop persist.sys.locale")
        ]

        results = []
        for device_id in devices:
            device_info = {'device': device_id, 'properties': {}}

            for label, cmd_template in info_commands:
                cmd = cmd_template.format(device=device_id)
                output, error, code = execute_ssh_command(ssh, cmd)

                # Clean output
                value = output.strip()
                if '\n' in value:
                    # Take first line if multiline
                    value = value.split('\n')[0].strip()
                elif not value:
                    value = "未知"

                device_info['properties'][label] = value

            results.append(device_info)

        return_ssh_connection(ssh)
        return jsonify({'success': True, 'results': results})
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== Device Management ====================
@app.route('/api/devices/management')
def get_devices_management():
    """Get devices management info with source type and host info"""
    config = load_config()

    # Get all connected devices from test host
    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'devices': []})

    try:
        # Get basic device list
        output, _, _ = execute_ssh_command(ssh, "adb devices", timeout=5)
        device_ids = []
        for line in output.split('\n')[1:]:
            if line.strip() and '\tdevice' in line:
                device_id = line.split('\t')[0]
                device_ids.append(device_id)

        if not device_ids:
            return_ssh_connection(ssh)
            return jsonify({'devices': []})

        # Get device lock status
        locks = get_device_locks_status()
        client_id = get_client_id()

        # Batch fetch all device properties in ONE command (optimized)
        device_props_cmd = " && ".join([
            f"adb -s {device_id} shell 'echo \"===DEVICE:{device_id}===\" && getprop ro.serialno && getprop ro.product.model && getprop ro.build.version.release'"
            for device_id in device_ids
        ])

        props_output, _, _ = execute_ssh_command(ssh, device_props_cmd, timeout=15)

        # Parse the batch output
        device_data = {}
        current_device = None

        for line in props_output.split('\n'):
            line = line.strip()
            if line.startswith('===DEVICE:'):
                current_device = line.split('===DEVICE:')[1].split('===')[0]
                device_data[current_device] = {'serial_no': '', 'model': '', 'android_version': ''}
            elif current_device and line:
                if not device_data[current_device]['serial_no']:
                    device_data[current_device]['serial_no'] = line
                elif not device_data[current_device]['model']:
                    device_data[current_device]['model'] = line
                elif not device_data[current_device]['android_version']:
                    device_data[current_device]['android_version'] = line

        return_ssh_connection(ssh)

        # Build response (no more SSH commands needed)
        devices_info = []
        ubuntu_host = config.get("ubuntu_host", "")
        ubuntu_user = config.get("ubuntu_user", "")

        # 获取 USB/IP 设备来源记录（全局共享，支持多用户）
        with usbip_devices_lock:
            usbip_devices_source_copy = usbip_devices_source.copy()

            # 清理已不存在的设备来源记录
            # 如果设备已不在当前设备列表中，说明设备已断开/移除，应该清除其来源记录
            current_device_set = set(device_ids)
            devices_to_remove = [
                dev_id for dev_id in usbip_devices_source.keys()
                if dev_id not in current_device_set
            ]
            if devices_to_remove:
                print(f"[Device Management] Cleaning up removed devices: {devices_to_remove}")
                for dev_id in devices_to_remove:
                    del usbip_devices_source[dev_id]

        for device_id in device_ids:
            data = device_data.get(device_id, {})

            # 判断设备来源类型
            # 通过检查全局 USB/IP 设备来源记录，区分 USB/IP 设备和本地直连设备
            if device_id in usbip_devices_source_copy:
                # 设备在 USB/IP 记录中 -> 通过 USB/IP 添加的设备
                source_type = 'usbip'
                source_host = usbip_devices_source_copy[device_id].get('source', 'Unknown')
            else:
                # 设备不在 USB/IP 记录中 -> 本地直连设备
                source_type = 'local'
                source_host = f'{ubuntu_user}@{ubuntu_host}'

            device_info = {
                'device_id': device_id,
                'serial_no': data.get('serial_no', ''),
                'model': data.get('model', ''),
                'android_version': data.get('android_version', ''),
                'source_type': source_type,
                'source_host': source_host,
                'status': 'online',
                'locked_by': '',
                'locked_by_self': False
            }

            # Check lock status
            lock_info = locks.get(device_id, {})
            if lock_info:
                device_info['locked_by'] = lock_info.get('user', '')
                device_info['locked_by_self'] = lock_info.get('client_id') == client_id

            devices_info.append(device_info)

        return jsonify({'devices': devices_info})

    except Exception as e:
        print(f"[ERROR] Error in get_devices_management: {e}")
        if ssh:
            return_ssh_connection(ssh)
        return jsonify({'devices': []})

# ==================== VNC ====================
@app.route('/api/vnc/start', methods=['POST'])
def start_vnc():
    """Start VNC with x11vnc and noVNC - 自动检测本地或远程"""
    import time
    config = load_config()
    ubuntu_host = config.get("ubuntu_host", "")
    ubuntu_user = config.get("ubuntu_user", "hcq")

    # 检查是否是本地主机
    local_hosts = ['localhost', '127.0.0.1', '::1', socket.gethostname()]
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
        local_hosts.append(local_ip)
    except:
        pass

    is_local = ubuntu_host in local_hosts

    if is_local:
        # 本地主机的 VNC 启动
        try:
            print("[VNC] Starting local VNC services...")

            # 调用本地 VNC 启动函数
            ensure_local_vnc_services()

            # 等待服务启动
            time.sleep(2)

            # 验证服务是否运行
            result = subprocess.run(
                ['pgrep', '-f', 'x11vnc.*:0'],
                capture_output=True,
                text=True
            )
            x11vnc_running = result.returncode == 0

            result = subprocess.run(
                ['pgrep', '-f', 'websockify.*6080'],
                capture_output=True,
                text=True
            )
            websockify_running = result.returncode == 0

            if x11vnc_running and websockify_running:
                return jsonify({
                    'success': True,
                    'message': '✅ VNC服务已启动(本地)',
                    'x11vnc_running': True,
                    'websockify_running': True,
                    'vnc_port': 5900,
                    'web_port': 6080,
                    'url': f"http://{ubuntu_host}:6080/vnc.html?autoconnect=true",
                    'local': True
                })
            elif x11vnc_running or websockify_running:
                return jsonify({
                    'success': True,
                    'message': '⚠ VNC服务部分运行(本地)',
                    'x11vnc_running': x11vnc_running,
                    'websockify_running': websockify_running,
                    'vnc_port': 5900,
                    'web_port': 6080,
                    'url': f"http://{ubuntu_host}:6080/vnc.html?autoconnect=true",
                    'local': True
                })
            else:
                return jsonify({
                    'success': False,
                    'error': 'VNC服务启动失败'
                }), 500

        except Exception as e:
            print(f"[VNC] Local VNC start error: {str(e)}")
            return jsonify({'success': False, 'error': str(e)}), 500

    # 远程主机的 VNC 启动 (原有逻辑)
    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:

        # 1. Check VNC password file (matching GUI lines 2587-2592)
        check_passwd_cmd = "[ -f ~/.vnc/passwd ] && echo 'exists' || echo 'missing'"
        passwd_output, passwd_error, passwd_code = execute_ssh_command(ssh, check_passwd_cmd)

        if "missing" in passwd_output:
            return_ssh_connection(ssh)
            return jsonify({
                'success': False,
                'error': 'VNC密码文件(~/.vnc/passwd)不存在，请先运行: x11vnc -storepasswd',
                'instructions': 'x11vnc -storepasswd'
            }), 404

        # 2. Check noVNC installation (matching GUI lines 2594-2605)
        check_novnc_cmd = "[ -d /opt/noVNC ] && echo 'exists' || echo 'missing'"
        novnc_output, novnc_error, novnc_code = execute_ssh_command(ssh, check_novnc_cmd)

        if "missing" in novnc_output:
            return_ssh_connection(ssh)
            return jsonify({
                'success': False,
                'error': 'noVNC未安装',
                'instructions': '''sudo apt-get update -y
sudo apt-get install -y git
cd /opt
sudo git clone https://github.com/novnc/noVNC.git
sudo git clone https://github.com/novnc/websockify.git noVNC/utils/websockify'''
            }), 404

        # 3. Set script permissions (matching GUI line 2608)
        chmod_cmd = "chmod +x /opt/noVNC/utils/websockify/run"
        execute_ssh_command(ssh, chmod_cmd)

        # 4. Wait for display ready (matching GUI lines 2610-2617)
        display_ready = False
        for _ in range(60):
            display_cmd = "export DISPLAY=:0 && xprop -root &>/dev/null && echo 'ready'"
            disp_output, _, _ = execute_ssh_command(ssh, display_cmd)
            if "ready" in disp_output:
                display_ready = True
                break
            time.sleep(1)

        if not display_ready:
            return_ssh_connection(ssh)
            return jsonify({
                'success': False,
                'error': 'DISPLAY未就绪，请确保已登录图形界面',
                'warning': '需要在主机桌面环境中运行'
            }), 503

        # 5. Start x11vnc (matching GUI lines 2619-2631)
        # First check if x11vnc is already running on port 5900
        check_x11_cmd = "pgrep -f 'x11vnc.*:0' && echo 'RUNNING' || echo 'NOT_RUNNING'"
        check_output, _, _ = execute_ssh_command(ssh, check_x11_cmd, timeout=5)
        x11vnc_running = 'RUNNING' in check_output

        if not x11vnc_running:
            x11vnc_cmd = (
                "export DISPLAY=:0 && "
                f"export XAUTHORITY=/home/{ubuntu_user}/.Xauthority && "
                "x11vnc -display :0 -forever -shared -rfbauth ~/.vnc/passwd -bg -o ~/logs/x11vnc.log"
            )

            # Kill existing x11vnc first (to be safe)
            execute_ssh_command(ssh, "pkill -f 'x11vnc.*:0'", timeout=5)
            time.sleep(1)

            x11_output, x11_error, x11_code = execute_ssh_command(ssh, x11vnc_cmd, timeout=15)

        # Wait and extract port from x11vnc output
        time.sleep(2)
        vnc_port = 5900  # default

        # Try to extract port from log
        log_cmd = "cat ~/logs/x11vnc.log 2>/dev/null | grep -oP 'PORT=\\K\\d+' | head -1"
        port_output, _, _ = execute_ssh_command(ssh, log_cmd)
        if port_output.strip():
            vnc_port = int(port_output.strip())

        # 6. Start noVNC websockify (matching GUI lines 2633-2639)
        # First check if websockify is already running on port 6080
        check_websockify_cmd = "pgrep -f 'websockify.*6080' && echo 'RUNNING' || echo 'NOT_RUNNING'"
        check_ws_output, _, _ = execute_ssh_command(ssh, check_websockify_cmd, timeout=5)
        websockify_running = 'RUNNING' in check_ws_output

        if not websockify_running:
            # Kill existing websockify first (to be safe)
            execute_ssh_command(ssh, "pkill -f 'websockify.*6080'", timeout=5)
            time.sleep(1)

            novnc_cmd = (
                f"cd /opt/noVNC && "
                f"nohup ./utils/websockify/run --web /opt/noVNC 6080 localhost:{vnc_port} "
                f"> ~/logs/novnc.log 2>&1 &"
            )
            execute_ssh_command(ssh, novnc_cmd, timeout=10)

        return_ssh_connection(ssh)

        # Build appropriate message
        if x11vnc_running and websockify_running:
            message = 'ℹ️ VNC服务已在运行'
        elif x11vnc_running or websockify_running:
            message = '✅ VNC服务已启动（部分服务已在运行）'
        else:
            message = '✅ VNC服务已启动'

        return jsonify({
            'success': True,
            'message': message,
            'x11vnc_running': x11vnc_running,
            'websockify_running': websockify_running,
            'vnc_port': vnc_port,
            'web_port': 6080,
            'url': f"http://{ubuntu_host}:6080/vnc.html?autoconnect=true",
            'instructions': '访问方式：点击「显示屏幕」按钮或浏览器访问上面的URL'
        })
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/vnc/stop', methods=['POST'])
def stop_vnc():
    """Stop VNC server"""
    config = load_config()
    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        # Stop VNC server
        output, error, code = execute_ssh_command(ssh, "vncserver -kill :1")
        return_ssh_connection(ssh)
        return jsonify({'success': True, 'output': output})
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/vnc/status', methods=['GET'])
def vnc_status():
    """Check VNC server status"""
    config = load_config()
    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        # Check if VNC server is running
        output, error, code = execute_ssh_command(ssh, "ps aux | grep 'Xvnc :1' | grep -v grep")

        if output.strip():
            return_ssh_connection(ssh)
            return jsonify({
                'success': True,
                'running': True,
                'display': ':1',
                'port': '6080',
                'host': config.ubuntu_host
            })
        else:
            return_ssh_connection(ssh)
            return jsonify({
                'success': True,
                'running': False
            })
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/devices/screen', methods=['POST'])
def show_device_screen():
    """Show device screen via scrcpy with tiled layout support - matches GUI implementation"""
    data = request.json
    devices = data.get('devices', [])
    config = load_config()
    ubuntu_user = config.get("ubuntu_user", "hcq")
    ubuntu_host = config.get("ubuntu_host", "")

    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        # 1. Check VNC service is ready (matching GUI lines 2723-2726)
        vnc_check_cmd = f"curl -s -o /dev/null -w '%{{http_code}}' http://{ubuntu_host}:6080 --connect-timeout 3"
        vnc_output, _, _ = execute_ssh_command(ssh, vnc_check_cmd, timeout=5)

        if '200' not in vnc_output and '000' not in vnc_output:
            return_ssh_connection(ssh)
            return jsonify({
                'success': False,
                'error': 'VNC服务未就绪',
                'warning': '请先点击「启动VNC」按钮'
            }), 503

        # 2. Check scrcpy if needed (matching GUI lines 2728-2730)
        # Use configured path or fallback to 'which'
        scrcpy_path = config.get("scrcpy_path", "")
        if scrcpy_path:
            # Substitute ubuntu_user in path
            scrcpy_path = scrcpy_path.replace('${ubuntu_user}', ubuntu_user)
            scrcpy_check_cmd = f"test -f '{scrcpy_path}' && echo 'exists' || echo 'not_found'"
            scrcpy_output, _, scrcpy_code = execute_ssh_command(ssh, scrcpy_check_cmd)

            if "not_found" in scrcpy_output:
                return_ssh_connection(ssh)
                return jsonify({
                    'success': False,
                    'error': f'scrcpy未找到: {scrcpy_path}',
                    'instructions': '请检查配置文件中的 scrcpy_path 路径'
                }), 404
        else:
            # Fallback to checking PATH
            scrcpy_check_cmd = "which scrcpy"
            scrcpy_output, _, scrcpy_code = execute_ssh_command(ssh, scrcpy_check_cmd)

            if scrcpy_code != 0:
                return_ssh_connection(ssh)
                return jsonify({
                    'success': False,
                    'error': 'scrcpy未安装',
                    'instructions': 'sudo apt-get install -y scrcpy'
                }), 404
            scrcpy_path = "scrcpy"  # Use command from PATH

        # 3. Check for already running scrcpy instances (matching GUI lines 2732-2739)
        running_devices = []
        pending_devices = []
        for device in devices:
            check_cmd = f"pgrep -f 'scrcpy.*-s {device}'"
            check_output, _, _ = execute_ssh_command(ssh, check_cmd)
            if check_output.strip():
                running_devices.append(device)
            else:
                pending_devices.append(device)

        # 4. Calculate window positions for tiled layout (matching GUI lines 2740-2760)
        import math
        total_devices = len(pending_devices) + len(running_devices)
        all_devices = sorted(running_devices + pending_devices)

        # Screen dimensions (assuming 1920x1080)
        screen_width = 1920
        screen_height = 1080
        horizontal_gap = 20
        vertical_margin = 50

        # Calculate window dimensions with GUI logic (matching GUI lines 2240-2259)
        max_available_width = screen_width - (horizontal_gap * (total_devices + 1))
        window_width = min(600, max_available_width // total_devices)
        window_height = int(window_width * 16 / 9)  # 16:9 aspect ratio
        max_height = int(screen_height * 0.7)
        if window_height > max_height:
            window_height = max_height
            window_width = int(window_height * 9 / 16)

        # Center the windows (GUI logic)
        total_width = total_devices * window_width + (total_devices - 1) * horizontal_gap
        start_x = max(horizontal_gap, (screen_width - total_width) // 2)
        start_y = max(vertical_margin, (screen_height - window_height) // 2)

        results = []
        vnc_urls = []

        # 5. Start scrcpy for each pending device with calculated positions (matching GUI lines 2196-2221)
        for idx, device_id in enumerate(pending_devices):
            # Calculate position using all_devices index (GUI line 2198)
            current_index = all_devices.index(device_id)
            x_offset = start_x + current_index * (window_width + horizontal_gap)
            y_offset = start_y

            # Boundary checks (GUI lines 2255-2258)
            if x_offset + window_width > screen_width:
                x_offset = max(0, screen_width - window_width - horizontal_gap)
            if y_offset + window_height > screen_height:
                y_offset = max(0, screen_height - window_height - vertical_margin)

            # Clear old log file
            execute_ssh_command(ssh, f"rm -f /tmp/scrcpy_{device_id}.log", timeout=5)

            # Build scrcpy command (matching GUI lines 2200-2213)
            scrcpy_cmd = (
                f"export DISPLAY=:0 && "
                f"export XAUTHORITY=/home/{ubuntu_user}/.Xauthority && "
                f"{scrcpy_path} -s {device_id} "
                f"--max-size 800 "
                f"--stay-awake "
                f"--window-title '{device_id}' "
                f"--window-x {x_offset} "
                f"--window-y {y_offset} "
                f"--window-width {window_width} "
                f"--window-height {window_height} "
                f"> /tmp/scrcpy_{device_id}.log 2>&1 &"
            )

            output, error, code = execute_ssh_command(ssh, scrcpy_cmd)

            # Wait for scrcpy to start and verify
            import time
            time.sleep(0.2)  # Give scrcpy time to start (matching GUI line 2216)

            # Check if scrcpy process is running
            check_cmd = f"pgrep -f 'scrcpy.*-s {device_id}' && echo 'RUNNING' || echo 'NOT_RUNNING'"
            check_output, _, _ = execute_ssh_command(ssh, check_cmd, timeout=5)

            # Read log file for errors
            log_cmd = f"cat /tmp/scrcpy_{device_id}.log 2>/dev/null || echo 'NO_LOG'"
            log_output, _, _ = execute_ssh_command(ssh, log_cmd, timeout=5)

            is_running = 'RUNNING' in check_output
            has_error = 'ERROR' in log_output or 'FATAL' in log_output or ('INFO: scrcpy' not in log_output and log_output != 'NO_LOG')

            # Determine success
            success = is_running and not has_error

            results.append({
                'device': device_id,
                'success': success,
                'running': is_running,
                'position': {'x': x_offset, 'y': y_offset, 'width': window_width, 'height': window_height},
                'error': log_output[:200] if not success and log_output else 'Failed to start scrcpy'
            })

        # 6. Generate VNC viewer URL (matching GUI lines 2753-2757)
        if results:
            vnc_urls.append({
                'device': 'all',
                'url': f"http://{ubuntu_host}:6080/vnc.html?autoconnect=true",
                'note': '所有设备屏幕已平铺显示'
            })

        return_ssh_connection(ssh)

        # Count successful and failed starts
        successful = [r for r in results if r['success']]
        failed = [r for r in results if not r['success']]
        successful_devices = [r['device'] for r in successful]
        failed_devices = [r['device'] for r in failed]

        # Build response with error details
        response = {
            'success': len(failed) == 0,
            'results': results,
            'running_devices': running_devices,
            'started_count': len(successful),
            'failed_count': len(failed),
            'vnc_urls': vnc_urls,
            'layout_info': {
                'total_devices': total_devices,
                'all_devices': all_devices,
                'window_size': {'width': window_width, 'height': window_height},
                'screen_size': {'width': screen_width, 'height': screen_height}
            }
        }

        # Build appropriate message (matching GUI lines 2223-2226)
        if len(successful) > 0:
            response['message'] = f"✅ 已启动{len(successful)}个投屏设备: {', '.join(successful_devices)}"
        if len(running_devices) > 0:
            if len(successful) > 0:
                response['message'] += f"\nℹ️ {len(running_devices)}个设备已在运行: {', '.join(running_devices)}"
            else:
                response['message'] = f"ℹ️ {len(running_devices)}个设备已在运行: {', '.join(running_devices)}"
        if len(failed) > 0:
            if len(successful) > 0 or len(running_devices) > 0:
                response['message'] += f"\n⚠️ {len(failed)}个设备启动失败: {', '.join(failed_devices)}"
            else:
                response['message'] = f"❌ 所有设备启动失败: {', '.join(failed_devices)}"
            response['errors'] = [
                f"{r['device']}: {r.get('error', 'Unknown error')}"
                for r in failed
            ]

        return jsonify(response)
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== ADB Forward ====================
@app.route('/api/adb-forward/start', methods=['POST'])
def start_adb_forward():
    """Start ADB port forwarding - matches GUI with sshpass and device host ADB server"""
    config = load_config()

    # 使用当前客户端的 device_host
    device_host = get_client_id()
    config['device_host'] = device_host

    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        device_password = config.get('device_password', '')

        # Detect if Windows host (matching GUI lines 946-954)
        is_windows = '@' in device_host and 'windows' in device_host.lower()

        # Clean up old SSH tunnels (matching GUI line 959)
        execute_ssh_command(ssh, "pkill -f adb; pkill -f 'ssh.*-L 5037'", timeout=5)

        # Start ADB server on device host (matching GUI lines 946-954)
        if is_windows:
            # Get device host SSH connection
            # For Windows, kill old ADB and start new one
            # Note: We can't directly connect to Windows host from here
            # This would require SSH credentials to device host
            test_output = "Windows device host detected - requires manual ADB server start"
        else:
            # For Linux device host, start ADB server via SSH
            start_adb_cmd = f"ssh {device_host} 'adb kill-server; adb -a nodaemon server start &'"
            execute_ssh_command(ssh, start_adb_cmd, timeout=5)
            test_output = "Linux ADB server started"

        # Setup SSH tunnel with sshpass for password authentication (matching GUI lines 962-965)
        forward_target = "localhost:5037"

        if device_password:
            # Use sshpass with password
            import shlex
            safe_password = shlex.quote(device_password)
            forward_cmd = f"SSHPASS={safe_password} sshpass -e ssh -f -N -L 5037:{forward_target} {device_host}"
        else:
            # Use SSH without password (key-based auth)
            forward_cmd = f"ssh -f -N -L 5037:{forward_target} {device_host}"

        execute_ssh_command(ssh, forward_cmd, timeout=10)

        # Wait for tunnel to establish
        import time
        time.sleep(3)

        # Test connection (matching GUI lines 971-976)
        test_output, test_error, test_code = execute_ssh_command(ssh, "adb devices", timeout=10)

        # Check if devices are connected
        devices = []
        for line in test_output.split('\n'):
            if '\tdevice' in line:
                devices.append(line.split('\t')[0])

        return_ssh_connection(ssh)

        return jsonify({
            'success': True,
            'devices': devices,
            'device_count': len(devices),
            'adb_output': test_output[:500],
            'message': f'✅ ADB端口转发成功! 设备: {", ".join(devices) if devices else "无"}'
        })
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/adb-forward/stop', methods=['POST'])
def stop_adb_forward():
    """Stop ADB port forwarding"""
    config = load_config()
    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        # Kill SSH tunnel and ADB processes
        execute_ssh_command(ssh, "pkill -f 'ssh.*5037'")
        execute_ssh_command(ssh, "pkill -f 'adb.*forward'")
        execute_ssh_command(ssh, "adb disconnect")
        return_ssh_connection(ssh)
        return jsonify({'success': True})
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== USB/IP 管理类 ====================
class USBIPManager:
    """USB/IP 设备管理器 - 处理 Windows 到 Ubuntu 的 USB 设备转发"""

    @staticmethod
    def find_android_devices(ssh, config=None):
        """查找 Android 设备的 BUSID"""
        output, _, _ = execute_ssh_command(ssh, 'usbipd list', timeout=10)
        print(f"[USB/IP] Scanning devices:\n{output}")

        # 从配置中获取 VID:PID
        vid_pid = config.get('usbip_vid_pid') if config else None

        print(f"[USB/IP] Using VID:PID pattern: {vid_pid}")

        devices = []
        in_connected = False

        for line in output.split('\n'):
            if 'Connected:' in line:
                in_connected = True
            elif 'Persisted:' in line:
                break
            elif in_connected and ('Android ADB Interface' in line or (vid_pid and vid_pid in line)):
                parts = line.strip().split()
                if parts and '-' in parts[0]:
                    devices.append(parts[0])

        print(f"[USB/IP] Found devices: {devices}")
        return devices

    @staticmethod
    def bind_device(ssh, busid):
        """绑定单个设备到 USB/IP"""
        output, _, _ = execute_ssh_command(ssh, f'usbipd list | findstr {busid}', timeout=5)

        if 'Shared' in output:
            print(f"[USB/IP] Device {busid} already shared")
            return True
        elif 'Attached' in output:
            print(f"[USB/IP] Detaching {busid}...")
            execute_ssh_command(ssh, f'usbipd detach --busid {busid}', timeout=15)
            time.sleep(1)

        print(f"[USB/IP] Binding {busid}...")
        execute_ssh_command(ssh, f'usbipd bind --busid {busid}', timeout=15)
        time.sleep(2)
        print(f"[USB/IP] Device {busid} bound")
        return True

    @staticmethod
    def bind_devices(ssh, busids):
        """绑定所有设备"""
        return [busid for busid in busids if USBIPManager.bind_device(ssh, busid)]

    @staticmethod
    def attach_device(ssh, device_ip, busid):
        """在 Ubuntu 上 attach 设备"""
        cmd = f'sudo usbip attach -r {device_ip} -b {busid}'
        print(f"[USB/IP] Attaching {busid} from {device_ip}...")
        execute_ssh_command(ssh, cmd, timeout=10)
        time.sleep(2)
        print(f"[USB/IP] Device {busid} attached")
        return True

    @staticmethod
    def attach_devices(ssh, device_ip, busids):
        """
        在 Ubuntu 上 attach 所有设备，并返回实际通过 USB/IP 添加的设备列表

        通过比较 attach 前后的设备列表差异，准确识别通过 USB/IP 新添加的设备，
        避免将测试主机直连的设备误标记为 USB/IP 设备。

        Args:
            ssh: SSH 连接对象
            device_ip: Windows 设备主机 IP
            busids: 要 attach 的设备 BUSID 列表

        Returns:
            tuple: (attached_busids, new_device_ids)
                - attached_busids: 成功 attach 的设备 BUSID 列表
                - new_device_ids: 通过 USB/IP 新添加的设备 ID 列表
        """
        # 获取 attach 之前的设备列表
        output_before, _, _ = execute_ssh_command(ssh, 'adb devices', timeout=10)
        devices_before = set(line.split('\t')[0] for line in output_before.split('\n')[1:] if '\tdevice' in line)
        print(f"[USB/IP] Devices before attach: {devices_before}")

        # 执行 attach 操作
        attached = [busid for busid in busids if USBIPManager.attach_device(ssh, device_ip, busid)]

        # 等待设备稳定
        time.sleep(3)
        execute_ssh_command(ssh, 'sudo udevadm trigger', timeout=10)
        execute_ssh_command(ssh, 'sudo udevadm settle', timeout=10)

        # 检查 attach 之后的设备列表
        output, _, _ = execute_ssh_command(ssh, 'adb devices', timeout=10)
        devices_after = set(line.split('\t')[0] for line in output.split('\n')[1:] if '\tdevice' in line)
        print(f"[USB/IP] Devices after attach: {devices_after}")

        # 计算新增的设备（通过 USB/IP 添加的设备）
        new_devices = list(devices_after - devices_before)
        print(f"[USB/IP] New devices added via USB/IP: {new_devices}")

        return attached, new_devices

    @staticmethod
    def ensure_vhci_driver(ssh):
        """确保 vhci_hcd 驱动已加载"""
        output, _, _ = execute_ssh_command(ssh, 'lsmod | grep vhci_hcd')
        if not output.strip():
            print("[USB/IP] Loading vhci_hcd driver...")
            execute_ssh_command(ssh, 'sudo modprobe vhci_hcd', timeout=10)
            time.sleep(1)


@app.route('/api/usbip/start', methods=['POST'])
def start_usbip():
    """启动 USB/IP 转发"""
    config = load_config()
    device_host = get_client_id()
    # 保存原始 Windows 设备主机地址，用于记录设备来源
    windows_device_host = device_host
    config['device_host'] = device_host

    # 自动从 client_ssh_credentials 中查找密码
    request_data = request.get_json() or {}
    device_password = request_data.get('device_password')

    # 如果请求中没有提供密码，从已保存的凭据中查找
    if not device_password:
        device_password = find_device_host_password(config, device_host)

    # 如果仍然没有密码，才尝试使用旧的 device_pswd
    if not device_password:
        device_password = config.get('device_pswd', '')

    if not device_password:
        print(f"[USB/IP] No password found for {device_host}")
        return jsonify({
            'success': False,
            'error': f'未找到 {device_host} 的SSH凭据，请先在登录页面输入SSH密码'
        }), 401

    temp_config = {**config, 'device_pswd': device_password}

    # 连接 Windows 主机
    print(f"[USB/IP] Connecting to {device_host}...")
    win_ssh = create_device_ssh_connection(temp_config)
    if not win_ssh:
        print(f"[USB/IP] Failed to connect to {device_host}")
        return jsonify({
            'success': False,
            'error': f'SSH连接失败，请检查 {device_host} 的SSH凭据'
        }), 401

    try:
        # 检查系统类型
        if not is_windows_host(win_ssh):
            win_ssh.close()
            return jsonify({'success': False, 'error': 'USB/IP 仅支持 Windows 主机'}), 400

        # 检查 usbipd
        output, error, _ = execute_ssh_command(win_ssh, 'usbipd --version', timeout=5)
        if error or not output:
            win_ssh.close()
            install_guide = (
                "Windows 设备主机未安装 usbipd 工具\n\n"
                "请在 Windows 电脑上以【管理员身份】运行 PowerShell，执行以下命令安装：\n\n"
                "winget install dorssel.usbipd-win --source winget\n\n"
                "安装完成后，验证安装：\n"
                "usbipd --version"
            )
            return jsonify({
                'success': False,
                'error': 'usbipd 未安装',
                'install_guide': install_guide
            }), 400

        # 终止 ADB
        execute_ssh_command(win_ssh, 'taskkill /F /IM adb.exe /T', timeout=10)

        # 查找设备
        busids = USBIPManager.find_android_devices(win_ssh, config)
        if not busids:
            win_ssh.close()
            return jsonify({'success': False, 'error': '未找到 Android 设备'}), 400

        # 绑定设备
        bound = USBIPManager.bind_devices(win_ssh, busids)
        win_ssh.close()

        if not bound:
            return jsonify({'success': False, 'error': '设备绑定失败'}), 500

        # 连接 Ubuntu 并 attach 设备
        ubuntu_ssh = get_ssh_connection(config)
        if not ubuntu_ssh:
            return jsonify({'success': False, 'error': '无法连接 Ubuntu 主机'}), 500

        try:
            USBIPManager.ensure_vhci_driver(ubuntu_ssh)

            device_ip = device_host.split('@')[1] if '@' in device_host else device_host
            attached, device_list = USBIPManager.attach_devices(ubuntu_ssh, device_ip, busids)

            # 保存密码
            if device_password and device_password != config.get('device_pswd', ''):
                config['device_pswd'] = device_password
                save_config(config)

            # 更新 USB/IP 连接状态
            client_id = get_client_id()
            with usbip_states_lock:
                usbip_states[client_id] = {'connected': True, 'timestamp': time.time()}
            print(f"[USB/IP Start] Set connected=True for client_id={client_id}")

            # 记录 USB/IP 设备来源（全局记录，支持多用户场景）
            # 将通过 USB/IP 添加的设备及其来源主机记录到全局字典中
            # 所有用户都可以看到这些设备的来源信息
            with usbip_devices_lock:
                for device_id in device_list:
                    usbip_devices_source[device_id] = {
                        'source': windows_device_host,
                        'timestamp': time.time()
                    }
                print(f"[USB/IP Start] Recorded device source: {windows_device_host} for devices: {device_list}")

            # 归还 SSH 连接（在所有状态更新完成后）
            return_ssh_connection(ubuntu_ssh)

            return jsonify({
                'success': True,
                'message': f'成功连接 {len(attached)} 个设备: {", ".join(attached)}',
                'devices': attached,
                'device_list': device_list
            })
        except Exception as e:
            ubuntu_ssh.close()
            return jsonify({'success': False, 'error': str(e)}), 500

    except Exception as e:
        win_ssh.close()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/usbip/stop', methods=['POST'])
def stop_usbip():
    """停止 USB/IP 转发"""
    config = load_config()
    device_host = get_client_id()
    config['device_host'] = device_host
    client_id = get_client_id()

    # 自动从 client_ssh_credentials 中查找密码
    device_password = find_device_host_password(config, device_host)
    if not device_password:
        device_password = config.get('device_pswd', '')

    if device_password:
        config['device_pswd'] = device_password

    win_ssh = create_device_ssh_connection(config)
    if not win_ssh:
        # 无法连接到 Windows，只清除连接状态
        # 注意：不清除设备来源记录，因为设备仍然在测试主机上
        with usbip_states_lock:
            usbip_states[client_id] = {'connected': False, 'timestamp': time.time()}
        print(f"[USB/IP Stop] Connection cleared (device source preserved)")
        return jsonify({'success': True, 'message': '本地设备已断开'})

    try:
        execute_ssh_command(win_ssh, 'usbipd unbind --all', timeout=10)
        win_ssh.close()
        time.sleep(2)

        # 只更新 USB/IP 连接状态，不清除设备来源记录
        # 设备仍然在测试主机上，来源信息应该保留
        with usbip_states_lock:
            usbip_states[client_id] = {'connected': False, 'timestamp': time.time()}

        print(f"[USB/IP Stop] Connection cleared (device source preserved)")

        return jsonify({
            'success': True,
            'message': '本地设备已断开'
        })
    except Exception as e:
        win_ssh.close()
        # 即使失败也清除连接状态，但保留设备来源记录
        with usbip_states_lock:
            usbip_states[client_id] = {'connected': False, 'timestamp': time.time()}
        print(f"[USB/IP Stop] Connection cleared on error (device source preserved)")
        return jsonify({'success': True, 'message': '本地设备已断开'})


@app.route('/api/usbip/status', methods=['GET'])
def get_usbip_status():
    """
    获取 USB/IP 状态

    通过检查多个维度来判断 USB/IP 连接状态：
    1. 检查当前客户端的连接状态记录
    2. 检查全局 USB/IP 设备来源记录（支持刷新页面后恢复状态）
    """
    client_id = get_client_id()

    # 方法1：检查当前客户端的连接状态
    with usbip_states_lock:
        state_info = usbip_states.get(client_id, {'connected': False, 'timestamp': 0})
        connected = state_info['connected']

    # 方法2：如果当前客户端没有记录，检查是否有全局 USB/IP 设备记录
    # 这样可以支持刷新页面后恢复按钮状态
    if not connected:
        with usbip_devices_lock:
            # 如果有任何 USB/IP 设备记录，说明有 USB/IP 连接
            has_usbip_devices = len(usbip_devices_source) > 0
            if has_usbip_devices:
                connected = True

    print(f"[USB/IP Status] client_id={client_id}, connected={connected}, all_states={list(usbip_states.keys())}, device_count={len(usbip_devices_source)}")
    return jsonify({'connected': connected})


# ==================== Advanced Test Features ====================
@app.route('/api/test/kill-tradefed', methods=['POST'])
def kill_tradefed():
    """Kill running tradefed processes"""
    config = load_config()
    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        # Kill all tradefed processes
        output, error, code = execute_ssh_command(ssh, "pkill -9 -f tradefed")
        # Also kill any java test processes
        output2, error2, code2 = execute_ssh_command(ssh, "pkill -9 -f 'java.*test'")

        return_ssh_connection(ssh)
        return jsonify({'success': True, 'killed_tradefed': code == 0, 'killed_test': code2 == 0})
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/test/autocomplete-suite', methods=['POST'])
def autocomplete_suite():
    """Auto-complete test suite path with tools subdirectory"""
    data = request.json
    test_type = data.get('test_type', 'CTS').lower()
    base_path = data.get('base_path', '')


    config = load_config()
    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        if not base_path:
            return jsonify({'success': False, 'error': 'Base path is required'}), 400

        # Map test types to their suite directories and binaries (same as GUI)
        suite_map = {
            'cts': {'subdir': 'android-cts', 'binary': 'cts-tradefed'},
            'gsi': {'subdir': 'android-cts', 'binary': 'cts-tradefed'},
            'gts': {'subdir': 'android-gts', 'binary': 'gts-tradefed'},
            'sts': {'subdir': 'android-sts', 'binary': 'sts-tradefed'},
            'vts': {'subdir': 'android-vts', 'binary': 'vts-tradefed'},
            'apts': {'subdir': 'android-gts', 'binary': 'gts-tradefed'}
        }

        config_info = suite_map.get(test_type)
        if not config_info:
            return_ssh_connection(ssh)
            return jsonify({'success': False, 'error': f'不支持的测试类型: {test_type}'}), 400

        subdir = config_info['subdir']
        binary = config_info['binary']

        # Try multiple path patterns to find the test suite
        candidates = []

        # Pattern 1: {base_path}/{subdir}/tools (standard structure)
        candidates.append(f"{base_path}/{subdir}/tools")

        # Pattern 2: Search for {subdir} in subdirectories of base_path
        # This handles structures like: base_path/android-gts-13.1-R1/android-gts/tools
        find_cmd = f"find '{base_path}' -maxdepth 3 -type d -name '{subdir}' 2>/dev/null | head -5"
        find_output, _, _ = execute_ssh_command(ssh, find_cmd, timeout=10)

        if find_output.strip():
            for line in find_output.strip().split('\n'):
                # Add tools subdirectory to each found subdir
                candidates.append(f"{line}/tools")

        # Pattern 3: Check if base_path itself is already the tools directory
        # Check for binary directly in base_path
        check_direct = f"[ -x '{base_path}/{binary}' ] && echo '{base_path}' || echo ''"
        direct_output, _, _ = execute_ssh_command(ssh, check_direct)
        if direct_output.strip():
            return_ssh_connection(ssh)
            return jsonify({
                'success': True,
                'path': base_path,
                'binary': binary,
                'autocompleted': True
            })

        # Try each candidate path
        for candidate in candidates:
            check_cmd = f"[ -x '{candidate}/{binary}' ] && echo '{candidate}' || echo ''"
            output, error, code = execute_ssh_command(ssh, check_cmd)

            if output.strip():
                final_path = output.strip()
                return_ssh_connection(ssh)
                return jsonify({
                    'success': True,
                    'path': final_path,
                    'binary': binary,
                    'autocompleted': True
                })

        # If binary not found, return original path with warning (GUI behavior)
        return_ssh_connection(ssh)
        return jsonify({
            'success': True,
            'path': base_path,
            'autocompleted': False,
            'warning': f'未找到 {binary}，请确认路径正确'
        })

    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== Test Reports ====================
@app.route('/api/reports/list')
def list_test_reports():
    """List all test report directories with summary information"""
    config = load_config()
    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'reports': []})

    try:
        ubuntu_user = config.get('ubuntu_user', 'hcq')
        results_base = f"/home/{ubuntu_user}/gms_test_results"

        # Check if results directory exists
        check_cmd = f"[ -d '{results_base}' ] && echo 'exists' || echo 'not_found'"
        output, _, _ = execute_ssh_command(ssh, check_cmd, timeout=5)

        if output.strip() != 'exists':
            return_ssh_connection(ssh)
            return jsonify({'reports': []})

        # List all timestamp directories, sorted by newest first
        list_cmd = f"find '{results_base}' -maxdepth 1 -type d -name '[0-9]*' -printf '%T@ %p\\n' 2>/dev/null | sort -rn | head -20"
        list_output, _, _ = execute_ssh_command(ssh, list_cmd, timeout=10)

        reports = []
        for line in list_output.strip().split('\n'):
            if not line.strip():
                continue

            parts = line.strip().split(' ', 1)
            if len(parts) < 2:
                continue

            result_dir = parts[1]
            timestamp = os.path.basename(result_dir)

            # Get test_result.xml info
            result_xml = f"{result_dir}/test_result.xml"
            check_xml = f"[ -f '{result_xml}' ] && echo 'exists' || echo 'not_found'"
            xml_exists, _, _ = execute_ssh_command(ssh, check_xml, timeout=3)

            report_info = {
                'timestamp': timestamp,
                'path': result_dir,
                'has_xml': xml_exists.strip() == 'exists'
            }

            # Parse test_result.xml if exists
            if report_info['has_xml']:
                # Get pass and fail counts
                pass_cmd = f"grep -o 'pass=\"[0-9]*\"' '{result_xml}' 2>/dev/null | head -1 | sed 's/pass=\"//; s/\"//' || echo 0"
                fail_cmd = f"grep -o 'failed=\"[0-9]*\"' '{result_xml}' 2>/dev/null | head -1 | sed 's/failed=\"//; s/\"//' || echo 0"

                pass_output, _, _ = execute_ssh_command(ssh, pass_cmd, timeout=3)
                fail_output, _, _ = execute_ssh_command(ssh, fail_cmd, timeout=3)

                report_info['pass'] = int(pass_output.strip() or 0)
                report_info['fail'] = int(fail_output.strip() or 0)
                report_info['total'] = report_info['pass'] + report_info['fail']

            reports.append(report_info)

        return_ssh_connection(ssh)
        return jsonify({'reports': reports})

    except Exception as e:
        print(f"[ERROR] Error listing test reports: {e}")
        if ssh:
            return_ssh_connection(ssh)
        return jsonify({'reports': []})

@app.route('/api/reports/<path:report_timestamp>/files')
def list_report_files(report_timestamp):
    """List files in a specific test report directory"""
    config = load_config()
    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        ubuntu_user = config.get('ubuntu_user', 'hcq')
        report_dir = f"/home/{ubuntu_user}/gms_test_results/{report_timestamp}"

        # List files recursively
        list_cmd = f"find '{report_dir}' -type f 2>/dev/null | head -50"
        list_output, _, _ = execute_ssh_command(ssh, list_cmd, timeout=10)

        files = []
        for line in list_output.strip().split('\n'):
            if not line.strip():
                continue

            # Get relative path from report_dir
            rel_path = line.replace(report_dir + '/', '')
            file_size_cmd = f"stat -c%s '{line}' 2>/dev/null || echo 0"
            size_output, _, _ = execute_ssh_command(ssh, file_size_cmd, timeout=3)

            files.append({
                'name': os.path.basename(line),
                'path': line,
                'relative_path': rel_path,
                'size': int(size_output.strip() or 0)
            })

        return_ssh_connection(ssh)
        return jsonify({'success': True, 'files': files})

    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/reports/view')
def view_report_file():
    """View a test report file content"""
    file_path = request.args.get('path')
    if not file_path:
        return jsonify({'success': False, 'error': 'File path is required'}), 400

    config = load_config()
    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        # Read file content
        cat_cmd = f"cat '{file_path}' 2>/dev/null"
        output, error, code = execute_ssh_command(ssh, cat_cmd, timeout=30)

        return_ssh_connection(ssh)

        # Determine content type based on file extension
        file_ext = os.path.splitext(file_path)[1].lower()
        if file_ext in ['.xml', '.html']:
            content_type = 'text/html'
        elif file_ext == '.json':
            content_type = 'application/json'
        elif file_ext in ['.log', '.txt']:
            content_type = 'text/plain'
        else:
            content_type = 'text/plain'

        return jsonify({
            'success': True,
            'content': output,
            'content_type': content_type
        })

    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== Advanced Screen Mirroring ====================
def calculate_window_positions(devices, screen_width=1920, screen_height=1080):
    """
    计算投屏窗口的位置和大小

    Args:
        devices: 设备ID列表
        screen_width: 屏幕宽度
        screen_height: 屏幕高度

    Returns:
        dict: 包含窗口大小和起始位置的字典
    """
    devices = sorted(devices)
    total_devices = len(devices)

    horizontal_gap = 20
    vertical_margin = 50

    max_available_width = screen_width - (horizontal_gap * (total_devices + 1))
    window_width = min(600, max_available_width // total_devices)
    window_height = int(window_width * 16 / 9)  # 16:9 aspect ratio

    max_height = int(screen_height * 0.7)
    if window_height > max_height:
        window_height = max_height
        window_width = int(window_height * 9 / 16)

    # Center the windows
    total_width = total_devices * window_width + (total_devices - 1) * horizontal_gap
    start_x = max(horizontal_gap, (screen_width - total_width) // 2)
    start_y = max(vertical_margin, (screen_height - window_height) // 2)

    return {
        'window_width': window_width,
        'window_height': window_height,
        'start_x': start_x,
        'start_y': start_y,
        'horizontal_gap': horizontal_gap
    }


def check_vnc_service(ssh, ubuntu_host):
    """检查VNC服务是否可用"""
    vnc_check_cmd = f"curl -s -o /dev/null -w '%{{http_code}}' http://{ubuntu_host}:6080 --connect-timeout 3"
    vnc_output, _, _ = execute_ssh_command(ssh, vnc_check_cmd, timeout=5)
    return vnc_output.strip() == '200'


def check_scrcpy_availability(ssh, config, ubuntu_user):
    """
    检查scrcpy是否可用

    Returns:
        tuple: (scrcpy_path, error_response) 如果失败则返回错误响应
    """
    scrcpy_path = config.get("scrcpy_path", "")

    if scrcpy_path:
        # Substitute ubuntu_user in path
        scrcpy_path = scrcpy_path.replace('${ubuntu_user}', ubuntu_user)
        scrcpy_check_cmd = f"test -f '{scrcpy_path}' && echo 'exists' || echo 'not_found'"
        scrcpy_output, _, scrcpy_code = execute_ssh_command(ssh, scrcpy_check_cmd)

        if "not_found" in scrcpy_output:
            return None, {
                'success': False,
                'error': f'scrcpy未找到: {scrcpy_path}',
                'instructions': '请检查配置文件中的 scrcpy_path 路径'
            }
    else:
        # Fallback to checking PATH
        scrcpy_check_cmd = "which scrcpy"
        scrcpy_output, _, scrcpy_code = execute_ssh_command(ssh, scrcpy_check_cmd)

        if scrcpy_code != 0:
            return None, {
                'success': False,
                'error': 'scrcpy未安装',
                'instructions': 'sudo apt-get install -y scrcpy'
            }
        scrcpy_path = "scrcpy"  # Use command from PATH

    return scrcpy_path, None


def is_device_mirroring(ssh, device_id):
    """
    检查设备是否正在投屏

    Returns:
        tuple: (is_process_running, has_window)
    """
    check_cmd = f"pgrep -f 'scrcpy.*-s {device_id}' && echo 'RUNNING' || echo 'NOT_RUNNING'"
    check_output, _, _ = execute_ssh_command(ssh, check_cmd, timeout=5)
    is_process_running = 'RUNNING' in check_output

    # Check if scrcpy window actually exists
    has_window = False
    try:
        window_check_cmd = f"wmctrl -l | grep '{device_id}' && echo 'HAS_WINDOW' || echo 'NO_WINDOW'"
        window_output, _, _ = execute_ssh_command(ssh, window_check_cmd, timeout=5)
        has_window = 'HAS_WINDOW' in window_output
    except Exception:
        # wmctrl not available, fall back to process-only check
        has_window = is_process_running

    return is_process_running, has_window


def start_device_mirroring(ssh, device_id, position, scrcpy_path, ubuntu_user, vnc_available):
    """
    启动单个设备的投屏

    Args:
        ssh: SSH连接
        device_id: 设备ID
        position: 窗口位置字典
        scrcpy_path: scrcpy可执行文件路径
        ubuntu_user: Ubuntu用户名
        vnc_available: VNC是否可用

    Returns:
        dict: 操作结果
    """
    x_offset = position['x']
    y_offset = position['y']
    window_width = position['width']
    window_height = position['height']
    horizontal_gap = position['gap']

    # Boundary checks
    screen_width = 1920
    screen_height = 1080
    vertical_margin = 50

    if x_offset + window_width > screen_width:
        x_offset = max(0, screen_width - window_width - horizontal_gap)
    if y_offset + window_height > screen_height:
        y_offset = max(0, screen_height - window_height - vertical_margin)

    if vnc_available:
        cmd = (
            f"export DISPLAY=:0 && "
            f"export XAUTHORITY=/home/{ubuntu_user}/.Xauthority && "
            f"{scrcpy_path} -s {device_id} "
            f"--max-size 800 "
            f"--stay-awake "
            f"--window-title '{device_id}' "
            f"--window-x {x_offset} "
            f"--window-y {y_offset} "
            f"--window-width {window_width} "
            f"--window-height {window_height} "
            f"> /tmp/scrcpy_{device_id}.log 2>&1 &"
        )
    else:
        cmd = (
            f"export DISPLAY=:0 && "
            f"export XAUTHORITY=/home/{ubuntu_user}/.Xauthority && "
            f"{scrcpy_path} -s {device_id} "
            f"> /tmp/scrcpy_{device_id}.log 2>&1 &"
        )

    execute_ssh_command(ssh, cmd, timeout=10)
    time.sleep(0.2)

    # Verify scrcpy started successfully
    check_cmd = f"pgrep -f 'scrcpy.*-s {device_id}' && echo 'RUNNING' || echo 'NOT_RUNNING'"
    check_output, _, _ = execute_ssh_command(ssh, check_cmd, timeout=5)
    is_started = 'RUNNING' in check_output

    return is_started


@app.route('/api/screen/start', methods=['POST'])
def start_screen_mirroring():
    """Start scrcpy with VNC for screen mirroring - Redirects to desktop page for VNC viewing"""
    data = request.json
    devices = data.get('devices', [])
    config = load_config()
    ubuntu_user = config.get('ubuntu_user', 'hcq')
    ubuntu_host = config.get('ubuntu_host', '')

    if not devices:
        return jsonify({'success': False, 'error': 'No devices selected'}), 400

    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        client_id = get_client_id()
        results = []
        vnc_ports = []
        already_running_devices = []

        # Check VNC service status
        vnc_available = check_vnc_service(ssh, ubuntu_host)

        # Check scrcpy availability
        scrcpy_path, error_response = check_scrcpy_availability(ssh, config, ubuntu_user)
        if error_response:
            return_ssh_connection(ssh)
            return jsonify(error_response), 404

        # Calculate window positions
        positions = calculate_window_positions(devices)

        # Process each device
        for idx, device_id in enumerate(sorted(devices)):
            with scrcpy_sessions_lock:
                session_info = scrcpy_sessions.get(device_id)

            # Check if device is already being mirrored
            is_process_running, has_window = is_device_mirroring(ssh, device_id)

            if session_info and is_process_running and has_window:
                # Device is already being mirrored with active window, skip
                already_running_devices.append(device_id)
                results.append({
                    'device': device_id,
                    'success': True,
                    'already_running': True,
                    'message': '已在投屏'
                })
                vnc_ports.append({
                    'device': device_id,
                    'url': f"http://{ubuntu_host}:6080/vnc.html?autoconnect=true" if vnc_available else None,
                    'message': 'VNC查看可用（已投屏）'
                })
                continue

            # If process exists but window is closed, clean it up
            if is_process_running and not has_window:
                execute_ssh_command(ssh, f"pkill -f 'scrcpy.*-s {device_id}'", timeout=5)
                time.sleep(1)

            # Calculate position for this device
            x_offset = positions['start_x'] + idx * (positions['window_width'] + positions['horizontal_gap'])
            y_offset = positions['start_y']

            position = {
                'x': x_offset,
                'y': y_offset,
                'width': positions['window_width'],
                'height': positions['window_height'],
                'gap': positions['horizontal_gap']
            }

            # Start mirroring
            is_started = start_device_mirroring(
                ssh, device_id, position, scrcpy_path, ubuntu_user, vnc_available
            )

            if is_started:
                # Record the scrcpy session
                with scrcpy_sessions_lock:
                    scrcpy_sessions[device_id] = {
                        'client_id': client_id,
                        'start_time': datetime.now().isoformat()
                    }

                results.append({
                    'device': device_id,
                    'success': True,
                    'already_running': False,
                    'position': {'x': x_offset, 'y': y_offset, 'width': positions['window_width'], 'height': positions['window_height']}
                })

                vnc_ports.append({
                    'device': device_id,
                    'url': f"http://{ubuntu_host}:6080/vnc.html?autoconnect=true" if vnc_available else None,
                    'message': 'VNC查看可用' if vnc_available else '仅本地显示'
                })
            else:
                # Failed to start scrcpy
                results.append({
                    'device': device_id,
                    'success': False,
                    'already_running': False,
                    'error': 'Failed to start scrcpy'
                })

        return_ssh_connection(ssh)

        # Build response
        newly_started = [r['device'] for r in results if r.get('success') and not r.get('already_running')]
        failed_devices = [r['device'] for r in results if not r.get('success')]

        # Build message
        message_parts = []
        if newly_started:
            message_parts.append(f"✅ 已启动{len(newly_started)}个投屏设备: {', '.join(newly_started)}")
        if already_running_devices:
            message_parts.append(f"ℹ️ {len(already_running_devices)}个设备已在投屏: {', '.join(already_running_devices)}")
        if failed_devices:
            message_parts.append(f"❌ {len(failed_devices)}个设备启动失败: {', '.join(failed_devices)}")

        message = '\n'.join(message_parts) if message_parts else '没有处理任何设备'

        return jsonify({
            'success': len(failed_devices) == 0,
            'results': results,
            'vnc_sessions': vnc_ports,
            'message': message,
            'newly_started': newly_started,
            'already_running': already_running_devices,
            'failed': failed_devices,
            'desktop_url': f"/desktop",
            'note': '点击"主机桌面"查看屏幕' if vnc_available else 'VNC未启动，屏幕仅在本地显示'
        })
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== VPN ====================
@app.route('/api/vpn/check-sshd', methods=['GET'])
def check_sshd():
    """Check SSH daemon status"""
    config = load_config()
    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        output, error, code = execute_ssh_command(ssh, "ps aux | grep sshd | grep -v grep")
        return_ssh_connection(ssh)
        running = len(output.strip()) > 0
        return jsonify({'success': True, 'running': running})
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/vpn/check-routing', methods=['GET'])
def check_routing():
    """Check VPN routing by pinging targets - matches GUI implementation"""
    config = load_config()
    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        # Get VPN targets from config (matching GUI lines 1218-1247)
        vpn_target = config.get("vpn_target", [])
        if isinstance(vpn_target, str):
            vpn_target = [t.strip() for t in vpn_target.split(',')]

        if not vpn_target:
            return_ssh_connection(ssh)
            return jsonify({
                'success': True,
                'message': '未配置VPN目标',
                'results': []
            })

        results = []
        success_count = 0
        failed_targets = []

        # Ping each target (matching GUI implementation)
        for target in vpn_target:
            cmd = f"ping -c 1 -W 2 {target} 2>&1"
            output, error, code = execute_ssh_command(ssh, cmd)

            # Check if ping was successful
            is_reachable = '1 packets transmitted, 1 received' in output or '1 received' in output

            result = {
                'target': target,
                'reachable': is_reachable,
                'output': output[:200]  # Truncate output
            }
            results.append(result)

            if is_reachable:
                success_count += 1
            else:
                failed_targets.append(target)

        return_ssh_connection(ssh)

        return jsonify({
            'success': True,
            'results': results,
            'summary': {
                'total': len(vpn_target),
                'success': success_count,
                'failed': len(failed_targets),
                'success_rate': f"{success_count}/{len(vpn_target)}"
            },
            'failed_targets': failed_targets
        })
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/vpn/connect', methods=['POST'])
def connect_vpn():
    """Connect VPN using nmcli (matches GUI implementation)"""
    config = load_config()
    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        # Connect VPN using nmcli (matching GUI implementation)
        vpn_cmd = "sudo nmcli connection up hcq2"
        output, error, code = execute_ssh_command(ssh, vpn_cmd, timeout=20)

        import time
        time.sleep(2)

        # Check connection result
        if code == 0:
            is_connected = True
            message = 'VPN 连接成功'
        elif 'already active' in (error or ''):
            is_connected = True
            message = 'VPN 已连接'
        elif 'unknown connection' in (error or ''):
            return_ssh_connection(ssh)
            return jsonify({
                'success': False,
                'error': 'VPN 连接 hcq2 不存在，请先在 NetworkManager 中配置'
            }), 404
        else:
            is_connected = False
            message = f'VPN 连接失败: {error or output}'

        return_ssh_connection(ssh)
        return jsonify({
            'success': is_connected,
            'connected': is_connected,
            'message': message,
            'output': output[:500] if output else ''
        })
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/vpn/disconnect', methods=['POST'])
def disconnect_vpn():
    """Disconnect VPN using nmcli"""
    config = load_config()
    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        # Disconnect VPN using nmcli
        disconnect_cmd = "sudo nmcli connection down hcq2"
        output, error, code = execute_ssh_command(ssh, disconnect_cmd)

        return_ssh_connection(ssh)
        return jsonify({'success': True, 'message': 'VPN 已断开'})
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/vpn/status', methods=['GET'])
def get_vpn_status():
    """Get VPN connection status"""
    config = load_config()
    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        vpn_target = config.get('vpn_target', ['www.google.com'])[0]
        output, error, code = execute_ssh_command(
            ssh,
            f"ping -c 1 -W 2 {vpn_target} 2>&1"
        )
        return_ssh_connection(ssh)
        connected = '1 packets transmitted, 1 received' in output or '1 received' in output
        return jsonify({'success': True, 'connected': connected})
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== File Upload ====================
@app.route('/api/upload/file', methods=['POST'])
def upload_file_from_browser():
    """
    Upload file from browser to remote server

    This endpoint receives a file upload from the browser,
    saves it temporarily, and then uploads it to the remote test host via SFTP.
    """
    import os
    import tempfile

    # Check if file is in request
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file provided'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'}), 400

    config = load_config()

    # Create temporary directory for uploads
    upload_dir = os.path.join(tempfile.gettempdir(), 'gms_uploads')
    os.makedirs(upload_dir, exist_ok=True)

    # Save uploaded file temporarily
    temp_path = os.path.join(upload_dir, file.filename)
    file.save(temp_path)

    try:
        # Connect to remote server
        ssh = get_ssh_connection(config)
        if not ssh:
            os.remove(temp_path)
            return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

        # Upload to remote server
        remote_path = f"/home/{config['ubuntu_user']}/{file.filename}"
        sftp = ssh.open_sftp()
        sftp.put(temp_path, remote_path)
        sftp.close()
        return_ssh_connection(ssh)

        # Clean up temporary file
        os.remove(temp_path)

        return jsonify({
            'success': True,
            'remote_path': remote_path,
            'message': f'文件已上传到 {remote_path}'
        })
    except Exception as e:
        # Clean up temporary file on error
        if os.path.exists(temp_path):
            os.remove(temp_path)
        if ssh:
            return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/upload', methods=['POST'])
def upload_file():
    """Upload file to remote server"""
    data = request.json
    file_path = data.get('file_path', '')
    config = load_config()

    if not file_path:
        return jsonify({'success': False, 'error': 'No file path provided'}), 400

    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        import os
        filename = os.path.basename(file_path)
        remote_path = f"/home/{config['ubuntu_user']}/{filename}"

        # Use scp to upload file
        sftp = ssh.open_sftp()
        sftp.put(file_path, remote_path)
        sftp.close()
        return_ssh_connection(ssh)

        return jsonify({'success': True, 'remote_path': remote_path, 'message': f'文件已上传到 {remote_path}'})
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/upload/progress', methods=['POST'])
def upload_file_with_progress():
    """Upload file with real-time progress tracking"""
    data = request.json
    file_path = data.get('file_path', '')
    remote_path = data.get('remote_path', '')
    config = load_config()

    if not file_path:
        return jsonify({'success': False, 'error': 'No file path provided'}), 400

    if not remote_path:
        import os
        filename = os.path.basename(file_path)
        remote_path = f"/home/{config['ubuntu_user']}/{filename}"

    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        import os

        file_size = os.path.getsize(file_path)

        # Upload with progress callback
        def progress_callback(transferred, total):
            percentage = int((transferred / total) * 100) if total > 0 else 0
            socketio.emit('upload_progress', {
                'transferred': transferred,
                'total': total,
                'percentage': percentage,
                'filename': os.path.basename(file_path)
            })

        sftp = ssh.open_sftp()
        sftp.put(file_path, remote_path, callback=progress_callback)
        sftp.close()
        return_ssh_connection(ssh)

        # Final progress update
        socketio.emit('upload_progress', {
            'transferred': file_size,
            'total': file_size,
            'percentage': 100,
            'filename': os.path.basename(file_path),
            'complete': True
        })

        return jsonify({'success': True, 'remote_path': remote_path})
    except Exception as e:
        return_ssh_connection(ssh)
        socketio.emit('upload_progress', {
            'error': str(e),
            'complete': True
        })
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== Firmware Burning ====================
@app.route('/api/firmware/burn', methods=['POST'])
def burn_firmware():
    """
    Burn firmware image to selected devices

    ⚠️ IMPORTANT DIFFERENCE FROM GUI VERSION ⚠️

    GUI Version (GMS_Auto_Test_GUI.py lines 2057-2147):
        - Uses upgrade_tool (Rockchip proprietary tool)
        - Requires device in loader mode (maskrom mode)
        - Supports parameter extraction (CPU ID, SN code)
        - Command: upgrade_tool UL <system.img> parameter...
        - Provides more detailed progress and error messages

    Web Version (this implementation):
        - Uses fastboot (standard Android tool)
        - Requires device in bootloader/fastboot mode
        - Uses standard fastboot flash commands
        - Command: fastboot flash system <system.img>
        - More compatible but less device-specific

    REASON FOR DIFFERENCE:
        - upgrade_tool is Rockchip-specific and requires proprietary libraries
        - fastboot is standard across all Android devices
        - Web version prioritizes compatibility over device-specific features

    For Rockchip devices with specific requirements, use the GUI version instead.
    """
    data = request.json
    devices = data.get('devices', [])
    system_img = data.get('system_img', '')
    vendor_img = data.get('vendor_img', '')
    misc_img = data.get('misc_img', f"/home/{config['ubuntu_user']}/GMS-Suite/misc.img")

    if not devices:
        return jsonify({'success': False, 'error': 'No devices selected'}), 400

    if not system_img:
        return jsonify({'success': False, 'error': 'System image path is required'}), 400

    config = load_config()
    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        results = []

        # Build the burn script command based on run_GSI_Burn.sh
        for device_id in devices:
            # Check if system image exists
            check_cmd = f"test -f '{system_img}' && echo 'exists' || echo 'not_found'"
            output, error, code = execute_ssh_command(ssh, check_cmd)

            if 'not_found' in output:
                results.append({
                    'device': device_id,
                    'success': False,
                    'error': f'System image not found: {system_img}'
                })
                continue

            # Build firmware burn command
            burn_cmd = f"cd /home/{config['ubuntu_user']} && "
            burn_cmd += f"adb -s {device_id} reboot bootloader && "
            burn_cmd += "sleep 5 && "
            burn_cmd += f"fastboot -s {device_id} oem at-unlock-vboot && "
            burn_cmd += f"fastboot -s {device_id} reboot fastboot && "
            burn_cmd += "sleep 3 && "
            burn_cmd += f"fastboot -s {device_id} delete-logical-partition product && "
            burn_cmd += f"fastboot -s {device_id} delete-logical-partition product_a && "
            burn_cmd += f"fastboot -s {device_id} delete-logical-partition product_b && "
            burn_cmd += f"fastboot -s {device_id} flash system '{system_img}' && "

            # Flash misc image if provided
            if misc_img:
                burn_cmd += f"fastboot -s {device_id} flash misc '{misc_img}' && "

            # Flash vendor_boot image if provided
            if vendor_img:
                check_vendor = f"test -f '{vendor_img}' && echo 'exists' || echo 'not_found'"
                v_output, _, _ = execute_ssh_command(ssh, check_vendor)
                if 'exists' in v_output:
                    burn_cmd += f"fastboot -s {device_id} flash vendor_boot '{vendor_img}' && "

            burn_cmd += f"fastboot -s {device_id} reboot"

            # Execute the burn command
            output, error, code = execute_ssh_command(ssh, burn_cmd, timeout=300)

            results.append({
                'device': device_id,
                'success': code == 0,
                'output': output[-500:] if output else error  # Last 500 chars
            })

            # Emit log update
            socketio.emit('log_update', {
                'log': f"Firmware burn for {device_id}: {'Success' if code == 0 else 'Failed'}",
                'type': 'success' if code == 0 else 'error'
            })

        return_ssh_connection(ssh)
        return jsonify({'success': True, 'results': results})
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== File Browser ====================
@app.route('/api/files/list', methods=['POST'])
def list_files():
    """List files in a remote directory"""
    data = request.json
    path = data.get('path', '')
    config = load_config()

    if not path:
        # Default to user home directory
        path = f"/home/{config.get('ubuntu_user', 'hcq')}"

    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        # Check if path exists
        check_cmd = f"test -e '{path}' && echo 'exists' || echo 'not_found'"
        output, _, _ = execute_ssh_command(ssh, check_cmd)

        if 'not_found' in output:
            return_ssh_connection(ssh)
            return jsonify({'success': False, 'error': f'Path not found: {path}'}), 404

        # List files with details (name, type, size, modified time)
        # Using ls -la to get detailed information
        list_cmd = f"ls -la '{path}' 2>/dev/null || echo 'ERROR'"
        output, error, code = execute_ssh_command(ssh, list_cmd)

        if 'ERROR' in output or code != 0:
            return_ssh_connection(ssh)
            return jsonify({'success': False, 'error': 'Failed to list directory'}), 500

        files = []
        for line in output.split('\n'):
            if line.startswith('total') or not line.strip():
                continue

            # Parse ls -la output
            parts = line.split()
            if len(parts) >= 9:
                permissions = parts[0]
                name = ' '.join(parts[8:])
                is_dir = permissions.startswith('d')
                size = parts[4] if not is_dir else '0'

                # Skip . and ..
                if name in ['.', '..']:
                    continue

                files.append({
                    'name': name,
                    'type': 'directory' if is_dir else 'file',
                    'size': size,
                    'permissions': permissions
                })

        # Sort: directories first, then files, alphabetically
        files.sort(key=lambda x: (x['type'] != 'directory', x['name'].lower()))

        return_ssh_connection(ssh)
        return jsonify({
            'success': True,
            'path': path,
            'files': files
        })
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== GSI Burning ====================
@app.route('/api/gsi/burn', methods=['POST'])
def burn_gsi():
    """
    Burn GSI (Generic System Image) to selected devices

    ℹ️ IMPLEMENTATION NOTES

    This implementation uses the run_GSI_Burn.sh script from the GMS-Suite,
    which matches the GUI version's approach (GMS_Auto_Test_GUI.py lines 2149-2242).

    Both versions use the same script with parameters:
        - run_GSI_Burn.sh <device> --system <system.img> [--vendor <vendor.img>]

    The script handles:
        - Flashing system image to system partition
        - Optional vendor image flashing
        - Proper partition management
        - Device reboot after completion

    Difference: The GUI version provides more detailed progress feedback through
    real-time output parsing, while the web version uses Socket.IO for updates.
    """
    data = request.json
    devices = data.get('devices', [])
    system_img = data.get('system_img', '')
    vendor_img = data.get('vendor_img', '')

    # Load config first to get ubuntu_user
    config = load_config()
    script_path = data.get('script_path', config.get('gsi_scripts', f"/home/{config['ubuntu_user']}/GMS-Suite/run_GSI_Burn.sh"))

    if not devices:
        return jsonify({'success': False, 'error': 'No devices selected'}), 400

    if not system_img:
        return jsonify({'success': False, 'error': 'System image path is required'}), 400
    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        results = []

        # Ensure GMS-Suite directory exists
        suite_dir = f"/home/{config['ubuntu_user']}/GMS-Suite"
        mkdir_cmd = f"mkdir -p '{suite_dir}'"
        execute_ssh_command(ssh, mkdir_cmd)

        for device_id in devices:
            # Check if system image exists on remote
            check_cmd = f"test -f '{system_img}' && echo 'exists' || echo 'not_found'"
            output, _, _ = execute_ssh_command(ssh, check_cmd)

            if 'not_found' in output:
                results.append({
                    'device': device_id,
                    'success': False,
                    'error': f'System image not found: {system_img}'
                })
                continue

            # Build GSI burn command using the script
            # The script format: run_GSI_Burn.sh <device> --system <system.img> [--vendor <vendor.img>]
            burn_cmd = f"bash '{script_path}' '{device_id}' --system '{system_img}'"

            # Add vendor image if provided
            if vendor_img:
                v_check_cmd = f"test -f '{vendor_img}' && echo 'exists' || echo 'not_found'"
                v_output, _, _ = execute_ssh_command(ssh, v_check_cmd)
                if 'exists' in v_output:
                    burn_cmd += f" --vendor '{vendor_img}'"

            # Execute the GSI burn command
            output, error, code = execute_ssh_command(ssh, burn_cmd, timeout=600)

            results.append({
                'device': device_id,
                'success': code == 0,
                'output': output[-1000:] if output else error  # Last 1000 chars
            })

            # Emit log update
            socketio.emit('log_update', {
                'log': f"GSI burn for {device_id}: {'Success' if code == 0 else 'Failed'}",
                'type': 'success' if code == 0 else 'error'
            })

        return_ssh_connection(ssh)
        return jsonify({'success': True, 'results': results})
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== SN Burning ====================
@app.route('/api/sn/burn', methods=['POST'])
def burn_sn():
    """Burn serial number to selected devices"""
    data = request.json
    devices = data.get('devices', [])
    sn_code = data.get('sn_code', '')

    if not devices:
        return jsonify({'success': False, 'error': 'No devices selected'}), 400

    if not sn_code:
        return jsonify({'success': False, 'error': 'SN code is required'}), 400

    config = load_config()
    ssh = get_ssh_connection(config)
    if not ssh:
        return jsonify({'success': False, 'error': 'SSH connection failed'}), 500

    try:
        results = []

        for device_id in devices:
            # SN burning typically requires upgrade_tool in loader mode
            # For now, this is a placeholder implementation
            results.append({
                'device': device_id,
                'success': False,
                'error': 'SN burning requires device in loader mode. This feature needs to be implemented with specific tool support.'
            })

        return_ssh_connection(ssh)
        return jsonify({'success': True, 'results': results})
    except Exception as e:
        return_ssh_connection(ssh)
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== SocketIO Events ====================
@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    emit('connected', {'data': 'Connected to GMS Auto Test Server'})

@socketio.on('refresh_devices')
def handle_refresh_devices():
    """Handle device refresh request"""
    config = load_config()
    devices = get_connected_devices(config)
    emit('devices_updated', devices)

# ==================== Terminal Events ====================
terminal_ssh = {}
terminal_lock = threading.Lock()

@socketio.on('terminal_connect')
def handle_terminal_connect(data):
    """Handle terminal SSH connection request (optimized for speed)"""
    import paramiko
    import threading

    try:
        config = load_config()
        host = data.get('host', config.get('ubuntu_host'))
        user = data.get('user', config.get('ubuntu_user'))
        password = data.get('password', config.get('ubuntu_pswd', ''))

        # Use request.sid for terminal connection (per-socket isolation)
        sid = request.sid

        # Log connection attempt
        print(f"[TERMINAL] Connection request from {sid} to {user}@{host}")

        # Create SSH client with optimized settings
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # Optimized SSH parameters for faster connection
        ssh_connect_timeout = 5  # Reduced from 10s to 5s
        ssh_banner_timeout = 3   # Reduced from 10s to 3s

        # Determine authentication method based on config
        use_key_auth = config.get('use_key_auth', False)

        connected = False
        last_error = None

        # Try key authentication first if enabled (faster than password)
        if use_key_auth:
            try:
                key_path = os.path.expanduser(config.get('private_key_path', '~/.ssh/id_rsa'))
                key = paramiko.RSAKey.from_private_key_file(key_path)
                ssh.connect(
                    host,
                    username=user,
                    pkey=key,
                    timeout=ssh_connect_timeout,
                    banner_timeout=ssh_banner_timeout,
                    compress=True  # Enable compression for faster data transfer
                )
                connected = True
                print(f"[TERMINAL] Connected using key authentication")
            except Exception as e:
                last_error = e
                print(f"[TERMINAL] Key auth failed: {e}")

        # Try password authentication if key failed or not enabled
        if not connected and password:
            try:
                ssh.connect(
                    host,
                    username=user,
                    password=password,
                    timeout=ssh_connect_timeout,
                    banner_timeout=ssh_banner_timeout,
                    compress=True
                )
                connected = True
                print(f"[TERMINAL] Connected using password authentication")
            except Exception as e:
                last_error = e
                print(f"[TERMINAL] Password auth failed: {e}")

        if not connected:
            error_msg = f'SSH连接失败：{str(last_error) if last_error else "请检查用户名、密码或密钥配置"}'
            emit('terminal_error', {'error': error_msg})
            return

        # Create shell channel with PTY
        channel = ssh.invoke_shell(term='xterm-256color')
        channel.setblocking(0)

        # Set initial terminal size
        channel.resize_pty(width=120, height=30)

        # Store SSH connection with thread safety
        with terminal_lock:
            # Close old connection if exists
            if sid in terminal_ssh:
                try:
                    terminal_ssh[sid]['ssh'].close()
                except:
                    pass

            terminal_ssh[sid] = {
                'ssh': ssh,
                'channel': channel,
                'host': host,
                'user': user,
                'connected_at': time.time()
            }

        print(f"[TERMINAL] Terminal session created for {sid} (connect time: {time.time() - float(connected) if isinstance(connected, float) else 'N/A'})")
        emit('terminal_connected')

        # Start reading thread
        def read_output():
            """Thread to continuously read terminal output"""
            try:
                buffer = ''
                while True:
                    # Check if session still exists
                    if sid not in terminal_ssh:
                        print(f"[TERMINAL] Session {sid} no longer exists, stopping read thread")
                        break

                    try:
                        # Read data with small chunk size for better responsiveness
                        data_chunk = terminal_ssh[sid]['channel'].recv(1024)
                        if not data_chunk:
                            # Connection closed
                            print(f"[TERMINAL] No data received, connection可能已关闭")
                            break

                        # Decode and emit
                        try:
                            text = data_chunk.decode('utf-8')
                        except UnicodeDecodeError:
                            text = data_chunk.decode('utf-8', errors='ignore')

                        socketio.emit('terminal_data', text, room=sid)

                    except socket.timeout:
                        # Timeout is normal, just continue
                        continue
                    except Exception as e:
                        print(f"[TERMINAL] Read error: {e}")
                        break

                    socketio.sleep(0.01)  # Small delay to prevent CPU spinning

            except Exception as e:
                print(f"[TERMINAL] Read thread error: {e}")
            finally:
                # Clean up connection
                with terminal_lock:
                    if sid in terminal_ssh:
                        try:
                            terminal_ssh[sid]['ssh'].close()
                        except:
                            pass
                        del terminal_ssh[sid]
                        print(f"[TERMINAL] Cleaned up session {sid}")

                # Notify client of disconnection
                socketio.emit('terminal_error', {'error': '连接已断开'}, room=sid)

        threading.Thread(target=read_output, daemon=True, name=f"terminal_read_{sid}").start()

    except AuthenticationException:
        emit('terminal_error', {'error': 'SSH认证失败：用户名或密码错误'})
    except SSHException as e:
        emit('terminal_error', {'error': f'SSH连接错误：{str(e)}'})
    except Exception as e:
        print(f"[TERMINAL] Connection error: {e}")
        emit('terminal_error', {'error': f'连接失败：{str(e)}'})

@socketio.on('terminal_input')
def handle_terminal_input(data):
    """Handle terminal input from user"""
    sid = request.sid

    with terminal_lock:
        if sid in terminal_ssh:
            try:
                input_data = data.get('input', data.get('data', ''))
                terminal_ssh[sid]['channel'].send(input_data)
            except Exception as e:
                print(f"[TERMINAL] Input error for {sid}: {e}")
                emit('terminal_error', {'error': f'发送数据失败：{str(e)}'})

@socketio.on('terminal_resize')
def handle_terminal_resize(data):
    """Handle terminal resize request"""
    client_id = get_client_id()

    with terminal_lock:
        if client_id in terminal_ssh:
            try:
                cols = data.get('cols', 120)
                rows = data.get('rows', 30)
                terminal_ssh[client_id]['channel'].resize_pty(width=cols, height=rows)
                print(f"[TERMINAL] Terminal resized for session {client_id}: {cols}x{rows}")
            except Exception as e:
                print(f"[TERMINAL] Resize error for session {client_id}: {e}")

# ==================== Local VNC Auto-Start ====================
def ensure_local_vnc_services():
    """确保本地 VNC 服务(x11vnc 和 noVNC)在应用启动时自动运行"""
    import os
    import subprocess
    import time

    home = os.path.expanduser('~')
    os.makedirs(f'{home}/logs', exist_ok=True)

    # 0. 检查图形桌面
    print("\n[0/3] 检查图形桌面就绪...")
    for _ in range(60):
        try:
            if subprocess.run(['xprop', '-root'], capture_output=True, timeout=2,
                            env={**os.environ, 'DISPLAY': ':0'}).returncode == 0:
                print("✓ 图形桌面已就绪")
                break
        except:
            pass
        time.sleep(1)

    # 1. 启动 x11vnc
    print("\n[1/3] 检查 x11vnc 服务...")
    if subprocess.run(['pgrep', '-f', 'x11vnc.*:0'], capture_output=True).returncode != 0:
        vnc_passwd = f'{home}/.vnc/passwd'
        if os.path.exists(vnc_passwd):
            env = {**os.environ, 'DISPLAY': ':0', 'XAUTHORITY': f'{home}/.Xauthority'}
            subprocess.run(['x11vnc', '-display', ':0', '-forever', '-shared',
                          '-rfbauth', vnc_passwd, '-bg', '-o', f'{home}/logs/x11vnc.log'],
                         env=env, capture_output=True)
            print("✓ x11vnc 启动成功")
        else:
            print("⚠ VNC密码文件不存在, 跳过x11vnc启动")
    else:
        print("✓ x11vnc 已在运行")

    # 2. 启动 websockify
    print("\n[2/3] 检查 noVNC websockify 服务...")
    if subprocess.run(['pgrep', '-f', 'websockify.*6080'], capture_output=True).returncode != 0:
        websockify_run = '/opt/noVNC/utils/websockify/run'
        if os.path.exists(websockify_run):
            subprocess.run(['chmod', '+x', websockify_run], capture_output=True)
            subprocess.run(f'cd /opt/noVNC && nohup {websockify_run} --web /opt/noVNC 6080 localhost:5900 '
                         f'> {home}/logs/novnc.log 2>&1 &', shell=True, capture_output=True)
            print("✓ noVNC websockify 启动成功")
        else:
            print("⚠ websockify未找到, 请确保noVNC已安装在/opt/noVNC")
    else:
        print("✓ noVNC websockify 已在运行")

    # 3. 验证状态
    print("\n[3/3] 验证服务状态...")
    time.sleep(2)
    x11vnc_ok = subprocess.run(['pgrep', '-f', 'x11vnc.*:0'], capture_output=True).returncode == 0
    websockify_ok = subprocess.run(['pgrep', '-f', 'websockify.*6080'], capture_output=True).returncode == 0
    print(f"  x11vnc: {'✓ 运行中' if x11vnc_ok else '✗ 未运行'}")
    print(f"  websockify: {'✓ 运行中' if websockify_ok else '✗ 未运行'}")

    print("\n" + "=" * 60)
    print("VNC 服务检查完成 | x11vnc:5900 | noVNC:6080")
    print("=" * 60 + "\n")


# ==================== Main ====================
if __name__ == '__main__':
    print("Starting GMS Auto Test Web Application...")
    print("Access the application at: http://localhost:5000")

    # 自动启动本地 VNC 服务
    try:
        ensure_local_vnc_services()
    except Exception as e:
        print(f"警告: VNC 服务自动启动失败: {str(e)}")
        print("你可以稍后通过 Web 界面的「启动VNC」按钮手动启动")

    socketio.run(app, host='0.0.0.0', port=5000, debug=True, allow_unsafe_werkzeug=True)
