# GMS_Auto_Test_GUI.py
import atexit
import getpass
import json
import os
import queue
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
import tkinter as tk
import tkinter.simpledialog as simpledialog
from tkinter import filedialog
from tkinter import ttk, messagebox, scrolledtext

try:
    import tkinterdnd2 as tkdnd
except ImportError:
    messagebox.showerror("依赖缺失", "请运行命令安装: pip install tkinterdnd2")
    sys.exit(1)
try:
    import paramiko
except ImportError:
    messagebox.showerror("依赖缺失", "请运行命令安装: paramiko:\npip install paramiko")
    sys.exit(1)

# ==================== 创建弹框 ====================
def center_toplevel(window, width, height):
    """居中 Toplevel 弹窗"""
    screen_width = window.winfo_screenwidth()
    screen_height = window.winfo_screenheight()
    x = (screen_width - width) // 2
    y = (screen_height - height) // 2
    window.geometry(f"{width}x{height}+{x}+{y}")

# ==================== 资源路径 ====================
BASE_PATH = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

def resource_path(relative_path):
    return os.path.join(BASE_PATH, relative_path)

def substitute_ubuntu_user(config):
    ubuntu_user = config.get("ubuntu_user", "user")
    if not ubuntu_user:
        return config
    updated = {}
    for key, value in config.items():
        if isinstance(value, str) and "${ubuntu_user}" in value:
            updated[key] = value.replace("${ubuntu_user}", ubuntu_user)
        else:
            updated[key] = value
    return updated

class GmsTestGUI:
    def __init__(self, root):
        self.root = root
        self.root.withdraw()
        self.root.title("GMS 远程测试程序")
        self.root.state('zoomed')
        self.root.geometry("1000x700")
        self.root.minsize(1000, 600)
        self.root.resizable(True, True)

        self.ssh_password_cache = None
        self.ssh_pool = queue.Queue(maxsize=3)
        self.ssh_lock = threading.Lock()

        self.test_running = False
        self.selected_devices = []

        self.adb_forward_running = False
        self.usbip_connected = False
        self._last_modified = None
        self._last_gsi_system_path = ""
        self._last_gsi_vendor_path = ""
        self._updating = False
        self._skip_suite_validation = False
        self.vnc_starting = False
        self.active_screens = set()
        self.active_screens_lock = threading.Lock()
        self.root.bind('<Configure>', self.on_window_resize)
        self.root.protocol("WM_DELETE_WINDOW", self.on_window_closing)
        atexit.register(self.cleanup_on_exit)

        self.config = self.load_config()
        if not self.config:
            self.root.deiconify()
            return
        self.setup_ui()
        self.detect_and_set_windows_device_host()
        self.root.deiconify()

    # ==================== 配置管理 ====================
    def load_config(self):
        try:
            config_path = resource_path("config.json")
            if not os.path.exists(config_path):
                self.show_error("配置错误", "未找到 config.json 文件")
                return None

            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            return substitute_ubuntu_user(config)
        except json.JSONDecodeError as e:
            self.show_error("配置错误", f"config.json 格式无效: {str(e)}")
            return None
        except Exception as e:
            self.show_error("配置错误", f"加载配置文件时出错: {str(e)}")
            return None

    def show_config(self):
        try:
            config_path = resource_path("config.json")
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = json.load(f)

            def on_submit(values):
                try:
                    for name, value in values.items():
                        if name in read_only_fields:
                            continue
                        if name == 'use_key_auth':
                            config_data[name] = value.lower() in ('true', 'yes', '1', 'on')
                        else:
                            config_data[name] = value.strip()

                    old_user = self.config.get("ubuntu_user")
                    old_host = self.config.get("ubuntu_host")
                    new_user = config_data.get("ubuntu_user")
                    new_host = config_data.get("ubuntu_host")
                    if old_user != new_user or old_host != new_host:
                        self.log_message("🔄 检测到测试主机变更，正在重置连接环境...")
                        self.ssh_password_cache = None
                        self.cleanup_ssh_pool()

                    with open(config_path, 'w', encoding='utf-8') as f:
                        json.dump(config_data, f, indent=2, ensure_ascii=False)
                    self.config = self.load_config()
                    self._update_config_in_ui()
                    self.log_message("✅ 配置文件已更新并重新加载")
                    return True
                except Exception as e:
                    self.show_error("保存失败", f"保存配置文件时出错:\n{str(e)}")
                    return False

            read_only_fields = {'script_path', 'vpn_target', 'use_key_auth', 'private_key_path'}
            vpn_target_default = config_data.get('vpn_target', [])
            if isinstance(vpn_target_default, list):
                vpn_target_display = ', '.join(vpn_target_default)
            else:
                vpn_target_display = str(vpn_target_default)
            fields = [
                {'name': 'ubuntu_user', 'label': '测试主机用户:', 'default': config_data.get('ubuntu_user', 'user'), 'type': 'text'},
                {'name': 'ubuntu_host', 'label': '测试主机地址:', 'default': config_data.get('ubuntu_host', ''), 'type': 'text'},
                {'name': 'device_host', 'label': '设备主机地址:', 'default': config_data.get('device_host', ''), 'type': 'text'},
                {'name': 'local_server', 'label': '本地主机地址:', 'default': config_data.get('local_server', ''), 'type': 'text'},
                {'name': 'script_path', 'label': '测试脚本路径:', 'default': config_data.get('script_path', ''), 'type': 'readonly'},
                {'name': 'suites_path', 'label': '测试套件路径:', 'default': config_data.get('suites_path', self.get_home_path("GMS-Suite")), 'type': 'text'},
                {'name': 'vnc_password', 'label': 'VNC连接密码:', 'default': config_data.get('vnc_password', ''), 'type': 'password'},
                {'name': 'vpn_target', 'label': 'VPN测试目标:', 'default': vpn_target_display, 'type': 'readonly'},
                {'name': 'use_key_auth', 'label': '使用密钥认证:', 'default': 'true' if config_data.get('use_key_auth', False) else 'false', 'type': 'readonly'},
                {'name': 'private_key_path', 'label': '私钥文件路径:', 'default': config_data.get('private_key_path', ''), 'type': 'readonly'}
            ]
            FormDialog(self.root, "修改配置(config.json)", 500, 350, fields, on_submit, gui_app=self)
        except FileNotFoundError:
            self.show_error("配置错误", "未找到 config.json 文件")
        except json.JSONDecodeError as e:
            self.show_error("配置错误", f"config.json 格式无效: {str(e)}")
        except Exception as e:
            self.show_error("配置错误", f"加载配置文件时出错: {str(e)}")

    def _update_config_in_ui(self):
        ubuntu_val = f"{self.config.get('ubuntu_user', 'user')}@{self.config.get('ubuntu_host', 'host')}"
        self.ubuntu_host_var.set(ubuntu_val)
        config_fields = {
            'device_host': self.device_host_var,
            'local_server': self.local_server_var,
            'script_path': self.script_path_var,
            'suites_path': self.suite_path_var
        }
        for key, var in config_fields.items():
            if key in self.config:
                var.set(self.config[key])
        self.log_message("✅ UI配置已更新")
        self.root.update_idletasks()

    def get_home_path(self, *subpaths):
        ubuntu_user = self.config.get("ubuntu_user", "user")
        base_path = f"/home/{ubuntu_user}"
        if subpaths:
            path = base_path
            for part in subpaths:
                if part:
                    path = f"{path}/{str(part).strip('/')}"
            return path
        return base_path

    # ==================== 窗口管理 ====================
    def on_window_resize(self, event):
        """窗口大小或位置变化时触发（防抖处理）"""
        if event.widget != self.root:
            return
        if self.root.state() != 'normal':
            return
        if hasattr(self, '_resize_timer'):
            self.root.after_cancel(self._resize_timer)

    def on_window_closing(self):
        if self.test_running and not messagebox.askokcancel("退出确认", "测试正在运行，确定要退出吗？"):
            return
        self.cleanup_on_exit()
        self.root.destroy()

    # ==================== 资源释放 ====================
    def cleanup_on_exit(self):
        cleanup_tasks = [
            ("停止ADB端口转发", self._stop_adb_port_forward),
            ("停止USB/IP连接", self._stop_usbip_connection),
            ("停止设备投屏", self.stop_all_screens),
            ("清理SSH连接池", self.cleanup_ssh_pool),
            ("终止测试进程", self._kill_tradefed_processes),
            ("清理临时文件", self.cleanup_other_resources)
        ]
        for task_name, task_func in cleanup_tasks:
            try:
                self.log_message(f"🧹 {task_name}...")
                task_func()
                self.log_message(f"✅ {task_name}完成")
            except Exception as e:
                self.log_message(f"⚠️ {task_name}失败: {e}")

    def stop_all_screens(self):
        """停止所有设备投屏"""
        with self.active_screens_lock:
            if not hasattr(self, 'active_screens') or not self.active_screens:
                return
        try:
            self.log_message(f"📺 正在停止 {len(self.active_screens)} 个设备投屏...")
            ssh = self.get_ssh_connection()
            if not ssh:
                return
            screens_to_stop = []
            with self.active_screens_lock:
                screens_to_stop = list(self.active_screens)
            for device in screens_to_stop:
                try:
                    cmd = f"pkill -f 'scrcpy.*-s {device}'"
                    ssh.exec_command(cmd, timeout=5)
                    self.log_message(f"✅ 已停止设备 {device} 的投屏")
                    with self.active_screens_lock:
                        self.active_screens.discard(device)
                except Exception as e:
                    self.log_message(f"⚠️ 停止设备 {device} 投屏失败: {e}")
            try:
                ssh.exec_command("rm -f /tmp/scrcpy_*.log", timeout=5)
            except:
                pass
            self.release_ssh_connection(ssh)
        except Exception as e:
            self.log_message(f"❌ 停止投屏时出错: {e}")

    def cleanup_ssh_pool(self):
        try:
            self.log_message("🔌 清理SSH连接池...")
            with self.ssh_lock:
                while not self.ssh_pool.empty():
                    try:
                        ssh = self.ssh_pool.get_nowait()
                        if ssh and ssh.get_transport() and ssh.get_transport().is_active():
                            ssh.close()
                    except queue.Empty:
                        break
                    except Exception as e:
                        pass
            self.log_message("✅ SSH连接池已清理")
        except Exception as e:
            self.log_message(f"⚠️ 清理SSH连接池时出错: {e}")

    def cleanup_other_resources(self):
        try:
            if hasattr(self, '_temp_files'):
                for temp_file in self._temp_files:
                    try:
                        if os.path.exists(temp_file):
                            os.remove(temp_file)
                    except:
                        pass
            if hasattr(self, 'device_vars'):
                self.device_vars.clear()
            if hasattr(self, 'config'):
                self.config.clear()
        except Exception as e:
            self.log_message(f"⚠️ 清理其他资源时出错: {e}")

    # ==================== 界面布局 ====================
    def setup_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame = ttk.Frame(self.root, padding="15")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        main_frame.columnconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=8)
        main_frame.rowconfigure(2, weight=1)

        # === 测试参数输入区 ===
        input_container = ttk.LabelFrame(main_frame, text="参数设置", padding="10")
        input_container.grid(row=0, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(0, 15))
        input_container.columnconfigure(0, weight=7)
        input_container.columnconfigure(1, weight=4)
        input_container.columnconfigure(2, weight=1)

        # 左部分：测试类型、模块、用例
        left_frame = ttk.Frame(input_container)
        left_frame.grid(row=0, column=0, padx=(0, 5), sticky=tk.W + tk.E + tk.N + tk.S)
        left_frame.columnconfigure(1, weight=1)

        ttk.Label(left_frame, text="测试类型:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.test_type = tk.StringVar(value="CTS")
        type_combo = ttk.Combobox(left_frame, textvariable=self.test_type,
                                  values=["CTS", "GSI", "GTS", "STS", "VTS", "APTS"],
                                  state="readonly", width=15)
        type_combo.grid(row=0, column=1, sticky=tk.W, pady=5, padx=(10, 0))

        ttk.Label(left_frame, text="测试模块:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.test_module = tk.StringVar()
        self.test_module_entry = ttk.Entry(left_frame, textvariable=self.test_module)
        self.test_module_entry.grid(row=1, column=1, sticky=(tk.W, tk.E), pady=5, padx=(10, 0))

        ttk.Label(left_frame, text="测试用例:").grid(row=2, column=0, sticky=tk.W, pady=5)
        self.test_case = tk.StringVar()
        self.test_case_entry = ttk.Entry(left_frame, textvariable=self.test_case)
        self.test_case_entry.grid(row=2, column=1, sticky=(tk.W, tk.E), pady=5, padx=(10, 0))

        # 中间部分：测试主机
        middle_frame = ttk.Frame(input_container)
        middle_frame.grid(row=0, column=1, padx=5, sticky=tk.W + tk.E + tk.N + tk.S)
        middle_frame.columnconfigure(1, weight=1)

        ttk.Label(middle_frame, text="测试脚本:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.script_path_var = tk.StringVar(value=self.config.get("script_path", ""))
        ttk.Entry(middle_frame, textvariable=self.script_path_var, state='readonly').grid(
            row=0, column=1, sticky=(tk.W, tk.E), pady=5, padx=(10, 0))

        suite_container = ttk.Frame(middle_frame)
        suite_container.grid(row=1, column=1, sticky=(tk.W, tk.E), pady=5, padx=(10, 0))
        ttk.Label(middle_frame, text="测试套件:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.suite_path_var = tk.StringVar(value=self.config.get("suites_path", self.get_home_path("GMS-Suite")))
        ttk.Entry(suite_container, textvariable=self.suite_path_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(suite_container, text="📁 选择套件", command=lambda: self.browse_remote_file(mode="suite")).pack(side=tk.RIGHT)

        retry_container = ttk.Frame(middle_frame)
        retry_container.grid(row=2, column=1, sticky=(tk.W, tk.E), pady=5, padx=(10, 0))
        ttk.Label(middle_frame, text="测试报告:").grid(row=2, column=0, sticky=tk.W, pady=5)
        self.retry_result_var = tk.StringVar()
        self.retry_report_entry = ttk.Entry(retry_container, textvariable=self.retry_result_var)
        self.retry_report_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(retry_container, text="📁 选择报告", command=lambda: self.browse_remote_file(mode="retry")).pack(side=tk.RIGHT)

        # 右部分：主机配置
        right_frame = ttk.Frame(input_container)
        right_frame.grid(row=0, column=2, padx=(5, 0), sticky=tk.W + tk.E + tk.N + tk.S)
        right_frame.columnconfigure(1, weight=1)

        ttk.Label(right_frame, text="测试主机:").grid(row=0, column=0, sticky=tk.W, pady=5)
        ubuntu_val = f"{self.config.get('ubuntu_user', 'user')}@{self.config.get('ubuntu_host', 'host')}"
        self.ubuntu_host_var = tk.StringVar(value=ubuntu_val)
        ttk.Entry(right_frame, textvariable=self.ubuntu_host_var, state='readonly').grid(
            row=0, column=1, sticky=(tk.W, tk.E), pady=5, padx=(10, 0))

        ttk.Label(right_frame, text="设备主机:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.device_host_var = tk.StringVar(value=self.config.get('device_host', ''))
        self.device_host_entry = ttk.Entry(right_frame, textvariable=self.device_host_var)
        self.device_host_entry.grid(row=1, column=1, sticky=(tk.W, tk.E), pady=5, padx=(10, 0))
        self.device_host_entry.bind("<Return>", self.on_device_host_confirm)

        ttk.Label(right_frame, text="本地主机:").grid(row=2, column=0, sticky=tk.W, pady=5)
        self.local_server_var = tk.StringVar(value=self.config.get('local_server', 'host'))
        self.local_server_entry = ttk.Entry(right_frame, textvariable=self.local_server_var)
        self.local_server_entry.grid(row=2, column=1, sticky=(tk.W, tk.E), pady=5, padx=(10, 0))
        self.local_server_entry.bind("<Return>", self.on_local_server_confirm)

        # === ADB 设备区 ===
        adb_frame = ttk.LabelFrame(main_frame, text="ADB设备", padding="10")
        adb_frame.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=(0, 5), pady=5)
        adb_frame.columnconfigure(0, weight=1)
        adb_frame.rowconfigure(0, weight=1)
        adb_frame.update_idletasks()
        adb_frame.configure(height=100)

        device_list_frame = ttk.Frame(adb_frame)
        device_list_frame.grid(sticky=tk.W + tk.E + tk.N + tk.S)
        self.device_canvas = tk.Canvas(device_list_frame, height=80)
        self.device_scrollbar = tk.Scrollbar(device_list_frame, orient="vertical", command=self.device_canvas.yview, width=12)
        self.device_scrollable_frame = ttk.Frame(self.device_canvas)
        self.device_scrollable_frame.bind(
            "<Configure>",
            lambda e: self.device_canvas.configure(scrollregion=self.device_canvas.bbox("all"))
        )
        self.device_canvas.create_window((0, 0), window=self.device_scrollable_frame, anchor="nw")
        self.device_canvas.configure(yscrollcommand=self.device_scrollbar.set)
        self.device_canvas.pack(side="left", fill="both", expand=True)
        self.device_scrollbar.pack(side="right", fill="y")

        # ADB 控制按钮
        adb_button_frame = ttk.Frame(adb_frame)
        adb_button_frame.grid(row=1, column=0, sticky=tk.W, pady=(10, 0))
        ttk.Button(adb_button_frame, text="🔄 刷新设备", command=self.refresh_devices).pack(side=tk.LEFT, padx=2)
        ttk.Button(adb_button_frame, text="✅ 全选设备", command=self.select_all_devices).pack(side=tk.LEFT, padx=2)
        ttk.Button(adb_button_frame, text="⏻ 重启设备", command=self.reboot_devices).pack(side=tk.LEFT, padx=2)
        ttk.Button(adb_button_frame, text="⏻ Remount", command=self.remount_devices).pack(side=tk.LEFT, padx=2)
        ttk.Button(adb_button_frame, text="🛜 连接Wifi", command=self.connect_wifi).pack(side=tk.LEFT, padx=2)
        ttk.Button(adb_button_frame, text="🔒 锁定设备", command=lambda: self.lock_selected_devices("lock")).pack(side=tk.LEFT, padx=2)
        ttk.Button(adb_button_frame, text="🔓 解锁设备", command=lambda: self.lock_selected_devices("unlock")).pack(side=tk.LEFT, padx=2)
        ttk.Button(adb_button_frame, text="🔐 锁定状态", command=self.check_device_lock_status).pack(side=tk.LEFT, padx=2)
        ttk.Button(adb_button_frame, text="📋 设备信息", command=self.collect_device_info).pack(side=tk.LEFT, padx=2)

        vnc_button_frame = ttk.Frame(adb_frame)
        vnc_button_frame.grid(row=2, column=0, sticky=tk.W, pady=(10, 0))
        ttk.Button(vnc_button_frame, text="🔥 烧写固件", command=self.burn_firmware).pack(side=tk.LEFT, padx=2)
        ttk.Button(vnc_button_frame, text="🔥 烧写GSI", command=self.burn_gsi_image).pack(side=tk.LEFT, padx=2)
        ttk.Button(vnc_button_frame, text="🔥 烧写SN码", command=self.burn_serial_number).pack(side=tk.LEFT, padx=2)
        ttk.Button(vnc_button_frame, text="🚀 启动VNC", command=self.init_and_start_vnc).pack(side=tk.LEFT, padx=2)
        ttk.Button(vnc_button_frame, text="📺 显示屏幕", command=self.show_device_screen).pack(side=tk.LEFT, padx=2)
        ttk.Button(vnc_button_frame, text="💻 Ubuntu终端", command=self.open_embedded_terminal).pack(side=tk.LEFT, padx=2)
        self.adb_forward_button = ttk.Button(vnc_button_frame, text="🔌 端口转发", command=self.setup_adb_port_forward)
        self.adb_forward_button.pack(side=tk.LEFT, padx=2)
        self.usbip_button = ttk.Button(vnc_button_frame, text="📱 本地设备", command=self.setup_usbip_forward)
        self.usbip_button.pack(side=tk.LEFT, padx=2)

        # === 操作控制区 ===
        control_frame = ttk.LabelFrame(main_frame, text="操作控制", padding="10")
        control_frame.grid(row=1, column=1, sticky=(tk.W, tk.E, tk.N, tk.S), padx=(5, 0), pady=5)
        control_frame.columnconfigure(0, weight=1)

        vpn_btn_frame = ttk.Frame(control_frame)
        vpn_btn_frame.grid(row=0, column=0, sticky=tk.W, pady=5)
        ttk.Button(vpn_btn_frame, text="📡 检查SSHD", command=self.check_ssh_button_handler).pack(side=tk.LEFT, padx=2)
        ttk.Button(vpn_btn_frame, text="📡 检查路由", command=self.check_and_alert_routing).pack(side=tk.LEFT, padx=2)
        self.vpn_connect_button = ttk.Button(vpn_btn_frame, text="🔌 连接VPN", command=self.connect_vpn)
        self.vpn_connect_button.pack(side=tk.LEFT, padx=2)
        self.vpn_check_button = ttk.Button(vpn_btn_frame, text="📡 检查VPN", command=self.check_vpn_status)
        self.vpn_check_button.pack(side=tk.LEFT, padx=2)
        self.vpn_status_label = ttk.Label(vpn_btn_frame, text="状态: 未知", font=('TkDefaultFont', 10, 'bold'))
        self.vpn_status_label.pack(side=tk.LEFT, padx=(6, 0), pady=2)

        # 文件上传区
        upload_frame = ttk.Frame(control_frame)
        upload_frame.grid(row=1, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(10, 0))
        upload_frame.columnconfigure(1, weight=1)
        ttk.Label(upload_frame, text="📁 本地文件:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.local_file_var = tk.StringVar()
        self.local_file_entry = ttk.Entry(upload_frame, textvariable=self.local_file_var)
        self.local_file_entry.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=(10, 5))
        self.local_file_entry.drop_target_register(tkdnd.DND_FILES)
        self.local_file_entry.dnd_bind('<<Drop>>', self.on_file_drop)
        ttk.Button(upload_frame, text="📤 上传到测试主机", command=self.handle_upload_file).grid(row=0, column=2, padx=(5, 0))

        # 上传进度
        self.upload_progress_var = tk.DoubleVar(value=0)
        self.upload_progress = ttk.Progressbar(upload_frame, variable=self.upload_progress_var, maximum=100)
        self.upload_progress.grid(row=1, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(6, 0))
        self.progress_info_label = ttk.Label(upload_frame, text="", font=('TkDefaultFont', 8))
        self.progress_info_label.grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=(2, 0))

        # 测试按钮
        test_btn_frame = ttk.Frame(control_frame)
        test_btn_frame.grid(row=2, column=0, sticky=tk.W, pady=(15, 0))
        self.run_button = ttk.Button(test_btn_frame, text="▶ 开始测试", command=self.start_test, style="Accent.TButton")
        self.run_button.pack(side=tk.LEFT, padx=2)
        self.clean_button = ttk.Button(test_btn_frame, text="🧹 清除日志", command=self.clean_test)
        self.clean_button.pack(side=tk.LEFT, padx=2)
        ttk.Button(test_btn_frame, text="⚙️ 配置", command=self.show_config).pack(side=tk.LEFT, padx=2)

        # === 日志区域 ===
        log_frame = ttk.LabelFrame(main_frame, text="测试日志", padding="5")
        log_frame.grid(row=2, column=0, columnspan=2, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(10, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, font=('Consolas', 9), height=20)
        self.log_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        # 绑定互斥逻辑
        self.test_module.trace_add("write", lambda *a: self.on_input_change("module"))
        self.test_case.trace_add("write", lambda *a: self.on_input_change("case"))
        self.retry_result_var.trace_add("write", lambda *a: self.on_input_change("report"))

        self.refresh_devices()
        # self.check_vpn_status()

    def on_input_change(self, source):
        if self._updating:
            return
        test_module = self.test_module.get().strip()
        test_case = self.test_case.get().strip()
        retry_report = self.retry_result_var.get().strip()
        if not (test_module or test_case or retry_report):
            self._last_modified = None
            return
        self._updating = True
        try:
            if source == "report":
                if test_module or test_case:
                    self.test_module.set("")
                    self.test_case.set("")
                    self._last_modified = "report"
            else:
                if retry_report:
                    self.retry_result_var.set("")
                    self._last_modified = "module_or_case"
        finally:
            self._updating = False

    # ==================== 主机配置 ====================
    def on_device_host_confirm(self, event=None):
        if self.adb_forward_running:
            self.show_warning("提示", "请关闭端口转发, 再修改设备主机")
            return
        value = self.device_host_var.get().strip()
        self.config['device_host'] = value
        if not value:
            self.log_message(f"🌐 设备主机已清空")
            return
        self.log_message(f"🌐 设备主机已设为: {value}")

    def detect_and_set_windows_device_host(self):
        if sys.platform == "win32":
            try:
                username = getpass.getuser()
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                ip_address = s.getsockname()[0]
                s.close()
                device_host_value = f"{username}@{ip_address}"
                self.device_host_var.set(device_host_value)
                self.config['device_host'] = device_host_value
                self.log_message(f"✅ Windows电脑已自动设置设备主机: {device_host_value}")
            except Exception as e:
                self.log_message(f"⚠️ 获取Windows设备信息失败: {e}")

    def on_local_server_confirm(self, event=None):
        new_value = self.local_server_var.get().strip()
        if not new_value or new_value == "host":
            self.config['local_server'] = new_value
            self.log_message(f"🌐 本地主机已清空")
            return
        if "@" not in new_value:
            self.show_error("格式错误", "本地主机格式应为 user@host")
            return
        user, host = new_value.split("@", 1)
        self.config['local_server'] = new_value
        self.log_message(f"🌐 本地主机已设为: {new_value}")

        def thread_task():
            if not self.check_ssh_connectivity(user, host):
                password = None
                password_event = threading.Event()

                def get_pw():
                    nonlocal password
                    prompt_text = f"请输入{user}@{host}的SSH密码:"
                    password = self.get_password(prompt=prompt_text)
                    password_event.set()

                self.root.after(0, get_pw)
                password_event.wait(timeout=30)
                if password:
                    self.setup_ssh_key_auth(user, host, password)
                else:
                    self.log_message("⚠️ 用户取消了密码输入或超时，跳过免密配置")
            else:
                self.log_message("✅ SSH免密连接已配置")

        thread = threading.Thread(target=thread_task, daemon=True)
        thread.start()

    def browse_remote_file(self, mode=None, var=None):
        default_base_path = self.get_home_path("GMS-Suite")
        if mode == "suite":
            raw_path = self.suite_path_var.get().strip()
            if not raw_path:
                raw_path = self.config.get("suites_path", default_base_path)
            initial_path = raw_path.rstrip("/") or default_base_path
            RemoteFolderSelector(self.root, self, initial_path)
        elif mode == "retry":
            raw_path = self.retry_result_var.get().strip()
            if not raw_path:
                raw_path = self.config.get("suites_path", default_base_path)
            initial_path = raw_path.rstrip("/") or default_base_path
            RemoteFolderSelector(self.root, self, initial_path, is_retry_selector=True, is_file_selector=True)
        elif mode == "file":
            current_path = var.get().strip() if var else ""
            if not current_path:
                current_path = default_base_path
            if os.path.isfile(current_path):
                initial_path = os.path.dirname(current_path)
            else:
                initial_path = current_path.rstrip("/") or default_base_path
            self._skip_suite_validation = True
            RemoteFolderSelector(self.root, self, initial_path, is_file_selector=True)
        else:
            self.log_message("🔍 选择远程文件...")

    # ==================== 日志函数 ====================
    def log_message(self, message):
        def _append():
            self.log_text.insert(tk.END, message + "\n")
            self.log_text.see(tk.END)
        self.root.after(0, _append)

    def show_error(self, title, msg):
        self.root.after(0, lambda: messagebox.showerror(title, msg))

    def show_info(self, title, msg):
        self.root.after(0, lambda: messagebox.showinfo(title, msg))

    def show_warning(self, title, msg):
        self.root.after(0, lambda: messagebox.showwarning(title, msg))

    # ==================== 端口转发(adb server) ====================
    """📖 端口转发 使用指南
    功能: 通过SSH隧道将远程Windows/Linux主机的ADB服务转发到本地
    原理: Ubuntu → SSH隧道 → Windows/Linux → ADB服务 → Android设备

    === 设备主机端(Windows/Linux)电脑设置 ===
    1. 启动服务: adb -a nodaemon server start

    === 测试主机端(Ubuntu)电脑设置 ===
    1. 清除转发: pkill -f adb; pkill -f 'ssh.*-L 5037'
    2. 创建隧道: ssh -f -N -L 5037:127.0.0.1:5037 hcq@172.16.14.94
    3. 测试连接: adb devices
    4. 断开连接: pkill -f 'ssh.*-L 5037.*{device_host}'; pkill -f adb
    """
    def setup_adb_port_forward(self):
        if self.adb_forward_running:
            thread = threading.Thread(target=self._stop_adb_port_forward, daemon=True)
        else:
            thread = threading.Thread(target=self._start_adb_port_forward, daemon=True)
        thread.start()

    def _start_adb_port_forward(self):
        device_host = self.config.get("device_host", "")
        if not device_host:
            self.show_warning("提示", "设备主机未配置")
            return False
        self.log_message("🔌 启动ADB端口转发...")
        try:
            device_ssh = self.get_device_host_ssh_connection()
            if not device_ssh:
                self.log_message("❌ SSH连接设备主机失败")
                return False
            if self.is_windows_host(device_ssh):
                self.log_message("💻 检测到Windows设备主机")
                forward_target = "127.0.0.1:5037"
                try:
                    device_ssh.exec_command("taskkill /F /IM adb.exe 2>nul", timeout=5)
                    time.sleep(2)
                    device_ssh.exec_command("adb -a nodaemon server start", timeout=5)
                    time.sleep(2)
                except:
                    pass
            else:
                self.log_message("🐧 检测到Linux设备主机")
                forward_target = "localhost:5037"
                device_ssh.exec_command("adb kill-server; adb -a nodaemon server start &", timeout=5)
            device_ssh.close()

            # 1. 清理并建立SSH转发
            password = None
            if "@" in device_host:
                username, hostname = device_host.split("@", 1)
                # 尝试获取密码（可以缓存）
                password = self.get_password(f"请输入 {device_host} 的SSH密码:")
                if not password:
                    self.log_message("❌ 用户取消输入密码")
                    return False

            ssh = self.get_ssh_connection()
            ssh.exec_command("pkill -f adb; pkill -f 'ssh.*-L 5037'", timeout=5)

            # 使用sshpass传递密码
            if password:
                safe_password = shlex.quote(password)
                forward_cmd = f"SSHPASS={safe_password} sshpass -e ssh -f -N -L 5037:{forward_target} {device_host}"
            else:
                forward_cmd = f"ssh -f -N -L 5037:{forward_target} {device_host}"

            self.log_message(f"🔄 建立SSH转发...")
            ssh.exec_command(forward_cmd, timeout=10)

            self.release_ssh_connection(ssh)
            time.sleep(3)

            # 4. 测试连接
            ssh = self.get_ssh_connection()
            stdin, stdout, stderr = ssh.exec_command("adb devices", timeout=10)
            output = stdout.read().decode('utf-8')
            devices = [line.split('\t')[0] for line in output.splitlines() if '\tdevice' in line]
            if devices:
                self.log_message(f"✅ ADB端口转发成功! 设备: {', '.join(devices)}")
                self.adb_forward_running = True
                self.root.after(0, lambda: self.adb_forward_button.config(text="🛑 停止转发"))
                return True
            else:
                self.log_message("⚠️ 转发建立但未检测到设备")
                return False
        except Exception as e:
            self.log_message(f"❌ ADB端口转发失败: {e}")
            return False
        finally:
            if 'ssh' in locals():
                self.release_ssh_connection(ssh)

    def _stop_adb_port_forward(self):
        device_host = self.config.get("device_host", "")
        if not device_host:
            return False
        try:
            # 清理测试主机
            ssh = self.get_ssh_connection()
            ssh.exec_command(f"pkill -f 'ssh.*-L 5037.*{device_host}'; pkill -f adb", timeout=5)
            self.release_ssh_connection(ssh)

            # 清理设备主机
            device_ssh = self.get_device_host_ssh_connection()
            if not device_ssh:
                self.log_message("❌ SSH接到设备主机失败")
                return False
            if self.is_windows_host(device_ssh):
                device_ssh.exec_command("taskkill /F /IM adb.exe", timeout=3)
            else:
                device_ssh.exec_command("adb kill-server", timeout=3)
            device_ssh.close()

            self.log_message("✅ ADB端口转发已停止")
            self.adb_forward_running = False
            self.root.after(0, lambda: self.adb_forward_button.config(text="🔌 端口转发"))
            return True
        except Exception as e:
            self.log_message(f"⚠️ 端口转发停止失败: {e}")
            return False

    def get_device_host_ssh_connection(self):
        """获取设备主机的SSH连接"""
        device_host = self.config.get("device_host", "")
        if not device_host:
            return None
        try:
            if "@" not in device_host:
                self.show_error("格式错误", "设备主机格式应为 user@host")
                return
            username, hostname = device_host.split("@", 1)
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            password = self.get_password(f"请输入{username}@{hostname}的SSH密码:")
            if not password:
                return None
            ssh.connect(hostname=hostname, username=username, password=password, timeout=10)
            return ssh
        except Exception as e:
            error_msg = str(e).lower()
            self.log_message(f"❌ 连接设备主机失败: {e}")
            if "unable to connect" in error_msg or "connection refused" in error_msg:
                self.root.after(100, self.check_ssh_button_handler)
            elif "authentication" in error_msg:
                self.show_error("认证失败", "用户名或密码错误，请重试")
            elif "timeout" in error_msg:
                self.show_error("连接超时", f"连接 {hostname} 超时，请检查网络")
            return None

    def is_windows_host(self, ssh_connection):
        try:
            stdin, stdout, stderr = ssh_connection.exec_command("ver 2>&1", timeout=3)
            output = stdout.read().decode('utf-8', errors='ignore').lower()
            if "microsoft" in output or "windows" in output:
                return True
            return False
        except:
            return False

    # ==================== 本地设备(USB/IP) ====================
    """📖 USB/IP 使用指南

    === 设备主机端(Windows)电脑设置 ===
    1. 以【管理员身份】运行 PowerShell
    2. 安装工具: winget install dorssel.usbipd-win --source winget
    3. 检查版本: usbipd --version
    4. 查看设备: usbipd list
    5. 绑定设备: usbipd bind --busid 1-20
    6. 绑定状态: usbipd list
    7. 解绑设备: usbipd unbind --busid 1-20

    === 测试主机端(Ubuntu)电脑设置 ===
    1. 安装工具: sudo apt update && sudo apt install linux-tools-generic linux-cloud-tools-generic -y
    2. 加载驱动: sudo modprobe vhci_hcd
    3. 检查驱动: lsmod | grep vhci
    4. 查看设备: sudo usbip list -r 172.16.14.94
    5. 连接设备: sudo usbip attach -r 172.16.14.94 -b 1-20
    6. 查看端口: usbip port
    7. 检查设备: lsusb
    8. 查看设备: adb devices
    9. 断开设备: sudo usbip detach -p 08
    """
    def setup_usbip_forward(self):
        if self.usbip_connected:
            thread = threading.Thread(target=self._stop_usbip_connection, daemon=True)
        else:
            thread = threading.Thread(target=self._start_usbip_connection, daemon=True)
        thread.start()

    def _start_usbip_connection(self):
        if not hasattr(self, 'all_busids'):
            self.all_busids = []
        device_host = self.config.get("device_host", "")
        if not device_host:
            self.show_warning("提示", "设备主机未配置")
            return False
        self.log_message("🔌 连接本地设备...")
        try:
            usbip_connected_retry = False
            win_ssh = self.get_device_host_ssh_connection()
            if not win_ssh:
                self.log_message("❌ SSH连接设备主机失败")
                return False
            if not self.is_windows_host(win_ssh):
                self.show_warning("提示", "USB/IP 本地设备目前只支持Windows系统")
                win_ssh.close()
                return False

            stdin, stdout, stderr = win_ssh.exec_command('usbipd --version', timeout=5)
            version_output = stdout.read().decode().strip()
            error_output = stderr.read().decode().strip()
            if error_output or not version_output:
                install_guide = (
                    "以【管理员身份】运行 PowerShell 安装 usbipd\n\n"
                    "winget install dorssel.usbipd-win --source winget\n"
                )
                self.show_warning("提示", install_guide)
                win_ssh.close()
                return False

            win_ssh.exec_command('taskkill /F /IM adb.exe /T', timeout=10)
            find_busid_cmd = r'powershell -Command "usbipd list | Select-String \"Android ADB Interface\" | ForEach-Object { ($_ -split \"\s+\")[0] }"'
            stdin, stdout, stderr = win_ssh.exec_command(find_busid_cmd, timeout=10)
            busid_list = stdout.read().decode().strip().splitlines()
            if not busid_list:
                self.show_warning("提示", "未找到 Android ADB Interface 设备, 请检查adb设备或手动重启adb设备")
                win_ssh.close()
                return False
            self.log_message(f"🔎 找到 {len(busid_list)} 个 ADB 设备: {', '.join(busid_list)}")
            self.all_busids = [busid.strip() for busid in busid_list]

            bound_devices = []
            for busid in self.all_busids:
                self.log_message(f"📱 处理设备 BusID: {busid}")
                stdin, stdout, _ = win_ssh.exec_command(f"usbipd list | findstr {busid}", timeout=5)
                state_info = stdout.read().decode()
                if "Shared" in state_info:
                    self.log_message(f"🟢 设备 {busid} 已是 Shared 状态，无需重复 bind")
                    bound_devices.append(busid)
                    continue
                elif "Attached" in state_info:
                    self.log_message(f"🧹 设备 {busid} 已是 Attached状态, 先 detach 再 bind")
                    win_ssh.exec_command(f"usbipd detach --busid {busid}", timeout=15)
                    time.sleep(1)
                    win_ssh.exec_command(f"usbipd bind --busid {busid}", timeout=15)
                    time.sleep(1)
                    bound_devices.append(busid)
                    continue
                else:
                    self.log_message(f"🟡 设备 {busid} 未共享，执行 bind...")
                    stdin, stdout, stderr = win_ssh.exec_command(f"usbipd bind --busid {busid}", timeout=10)
                    bind_success = False
                    for attempt in range(8):
                        stdin, stdout, _ = win_ssh.exec_command(f"usbipd list | findstr {busid}", timeout=5)
                        state_info = stdout.read().decode()
                        if "Shared" in state_info:
                            self.log_message(f"✅ 设备 {busid} 已进入 Shared 状态")
                            bound_devices.append(busid)
                            bind_success = True
                            break
                        if "Attached" in state_info:
                            self.log_message(f"✅ 设备 {busid} 已进入 Attached 状态")
                            bound_devices.append(busid)
                            bind_success = True
                            break
                        time.sleep(1)
                    if not bind_success:
                        self.log_message(f"⚠️ 设备 {busid} 进入 Shared/Attached 状态失败，继续处理其他设备")
            if not bound_devices:
                self.log_message("❌ 没有设备成功绑定")
                win_ssh.close()
                return False
            self.log_message(f"✅ 成功绑定 {len(bound_devices)} 个设备: {', '.join(bound_devices)}")
            win_ssh.close()

            ubuntu_ssh = self.get_ssh_connection()
            if not ubuntu_ssh:
                self.log_message("❌ 无法连接 Ubuntu 主机")
                return False
            self.log_message("🐧 检查Ubuntu主机 USB/IP 驱动状态...")
            stdin, stdout, _ = ubuntu_ssh.exec_command("lsmod | grep vhci_hcd")
            if not stdout.read().decode().strip():
                self.log_message("⚠️ vhci_hcd 未加载，尝试自动加载...")
                ubuntu_ssh.exec_command("sudo modprobe vhci_hcd", get_pty=True)
                time.sleep(1)
                stdin, stdout, _ = ubuntu_ssh.exec_command("lsmod | grep vhci_hcd")
                if not stdout.read().decode().strip():
                    self.log_message("❌ vhci_hcd 驱动加载失败，请在 Ubuntu 手动安装 linux-modules-extra")
                    ubuntu_ssh.close()
                    return False

            device_ip = device_host.split('@')[1]
            stdin, stdout, stderr = ubuntu_ssh.exec_command("sudo usbip port", get_pty=True)
            initial_port_info = stdout.read().decode()
            self.log_message(f"📌 初始 USBIP 端口状态:\n{initial_port_info}")

            for busid in self.all_busids:
                self.log_message(f"🔗 正在 Attach 设备 {busid}...")                
                self._usbip_ensure_attached_on_ubuntu(ubuntu_ssh, device_ip, [busid])
                
                attach_cmd = f"sudo usbip attach -r {device_ip} -b {busid}"
                stdin, stdout, stderr = ubuntu_ssh.exec_command(attach_cmd, get_pty=True)
                time.sleep(2)
                out = stdout.read().decode()
                err = stderr.read().decode()
                if out or err:
                    self.log_message(f"📤 设备 {busid} attach 输出")
                    if out:
                        self.log_message(f"stdout: {out}")
                    if err:
                        self.log_message(f"stderr: {err}")
                else:
                    self.log_message(f"✅ 设备 {busid} attach 命令已发送")
                time.sleep(2)
    
            time.sleep(3)
            stdin, stdout, stderr = ubuntu_ssh.exec_command("sudo usbip port", get_pty=True)
            final_port_info = stdout.read().decode()
            self.log_message(f"📌 最终 USBIP 端口状态:\n{final_port_info}")

            attached_devices = []
            device_ip = device_host.split('@')[1]
            port_count = 0
            if "Port" in final_port_info:
                for line in final_port_info.split('\n'):
                    if line.startswith("Port "):
                        port_count += 1

                # 匹配所有 usbip://IP:端口/busid 格式
                usbip_pattern = rf'usbip://{re.escape(device_ip)}:\d+/(\d+-\d+)'
                matches = re.findall(usbip_pattern, final_port_info)
                for busid_found in matches:
                    if busid_found in self.all_busids and busid_found not in attached_devices:
                        attached_devices.append(busid_found)
                self.log_message(f"✅ Windows电脑{device_ip} 检测到 {port_count} 个 USB/IP 端口")
                self.log_message(f"🔍 精确匹配到 {len(attached_devices)} 个设备: {', '.join(attached_devices) if attached_devices else '无'}")
            self.log_message("⏳ 等待 USB 设备稳定...")
            ubuntu_ssh.exec_command("sleep 2", get_pty=True)
            ubuntu_ssh.exec_command("sudo udevadm trigger", get_pty=True)
            ubuntu_ssh.exec_command("sudo udevadm settle", get_pty=True)
            ubuntu_ssh.close()

            if attached_devices:
                self.log_message(f"🎉 USB/IP 设备接入完成! 共连接 {len(attached_devices)} 个设备: {', '.join(attached_devices)}")
                self.refresh_devices()
                self.usbip_connected = True
                self.root.after(0, lambda: self.usbip_button.config(text="🛑 断开设备"))
                return True
            else:
                self.log_message("❌ USB/IP 连接失败")
                if not usbip_connected_retry:
                    usbip_connected_retry = True
                    self.log_message("🔄 尝试重新连接 USB/IP 设备...")
                    return self._start_usbip_connection()
                return False
        except Exception as e:
            self.log_message(f"❌ USB/IP 连接失败: {e}")
            return False

    def _stop_usbip_connection(self):
        if not self.config.get("device_host", ""):
            self.show_warning("提示", "设备主机未配置")
            return False
        self.log_message("🔌 断开本地设备...")
        try:
            win_ssh = self.get_device_host_ssh_connection()
            if not win_ssh:
                self.log_message("❌ 无法连接 Windows 设备主机")
                return False
            self.log_message("🔓 解除所有 USB/IP 绑定...")
            stdin, stdout, stderr = win_ssh.exec_command("usbipd unbind --all", timeout=10)
            output = stdout.read().decode(errors="replace")
            error = stderr.read().decode(errors="replace")
            if output:
                self.log_message(f"📤 unbind 输出: {output}")
            if error:
                self.log_message(f"📤 unbind 错误: {error}")
            win_ssh.close()

            if hasattr(self, 'all_busids'):
                del self.all_busids
            self.usbip_connected = False
            self.root.after(0, lambda: self.usbip_button.config(text="📱 本地设备"))
            self.log_message("✅ 本地设备已断开")

            time.sleep(2)
            self.refresh_devices()
        except Exception as e:
            self.log_message(f"⚠️ 本地设备断开失败: {e}")

    def _usbip_ensure_attached_on_ubuntu(self, ssh, device_ip: str, busids: list[str]) -> bool:
        """
        确保 busids 在 Ubuntu 已 attach；若已存在映射则先 detach 再 attach（更抗 reboot 后残留会话）
        """
        stdin, stdout, _ = ssh.exec_command("sudo usbip port", get_pty=True)
        port_info = stdout.read().decode(errors="replace")
        port_map = self._parse_usbip_port_map(port_info, device_ip)

        for busid in busids:
            # 如果已映射到某个 port，先 detach 再 attach（避免僵尸连接）
            if busid in port_map:
                p = port_map[busid]
                self.log_message(f"🧹 USB/IP: busid {busid} 已在 Port {p}，先 detach")
                ssh.exec_command(f"sudo usbip detach -p {p}", get_pty=True)
                time.sleep(1)

            self.log_message(f"🔗 USB/IP: attach busid {busid}")
            ssh.exec_command(f"sudo usbip attach -r {device_ip} -b {busid}", get_pty=True)
            time.sleep(1.5)
            stdin, stdout, _ = ssh.exec_command("sudo usbip port", get_pty=True)
            port_txt = stdout.read().decode(errors="replace")

            if "Port" not in port_txt:
                self.log_message("❌ attach 后 usbip port 为空，判定失败")
                return False

        # udev settle（你 start 里也做了类似处理 :contentReference[oaicite:9]{index=9}）
        ssh.exec_command("sudo udevadm trigger", get_pty=True)
        ssh.exec_command("sudo udevadm settle", get_pty=True)
        return True

    def _parse_usbip_port_map(self, port_info: str, device_ip: str) -> dict:
        """
        解析 `usbip port` 输出，得到 {busid: port_num}
        兼容你在 _start_usbip_connection 里用的 usbip://IP:PORT/BUSID 形式 :contentReference[oaicite:8]{index=8}
        """
        mapping = {}
        # Port 00: <...>
        #   Remote: usbip://172.16.xx.xx:3240/1-20
        cur_port = None
        for line in port_info.splitlines():
            m = re.match(r"Port\s+(\d+):", line.strip())
            if m:
                cur_port = m.group(1)
                continue
            if cur_port is not None:
                # 取 busid
                m2 = re.search(rf"usbip://{re.escape(device_ip)}:\d+/(\d+-\d+)", line)
                if m2:
                    mapping[m2.group(1)] = cur_port
                    cur_port = None
        return mapping

    # ==================== SSH连接 ====================
    def get_ssh_connection(self, timeout=5):
        """从连接池获取SSH连接，带超时保护"""
        ssh = None
        try:
            ssh = self.ssh_pool.get_nowait()
        except queue.Empty:
            return self.create_ssh_client()
        
        if ssh and ssh.get_transport() and ssh.get_transport().is_active():
            try:
                transport = ssh.get_transport()
                transport.send_ignore()
                return ssh
            except:
                try:
                    ssh.close()
                except:
                    pass
                return self.create_ssh_client()
        elif ssh:
            try:
                ssh.close()
            except:
                pass
            return self.create_ssh_client()
        return None

    def release_ssh_connection(self, ssh):
        """释放SSH连接回池"""
        if ssh and ssh.get_transport() and ssh.get_transport().is_active():
            try:
                self.ssh_pool.put_nowait(ssh)
            except queue.Full:
                ssh.close()

    def create_ssh_client(self):
        ubuntu_host=self.config["ubuntu_host"]
        ubuntu_user=self.config["ubuntu_user"]
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            if self.config.get("use_key_auth", True):
                private_key = self.get_private_key()
                if private_key:
                    ssh.connect(
                        hostname=ubuntu_host,
                        username=ubuntu_user,
                        pkey=private_key,
                        timeout=10
                    )
                    return ssh
            if self.ssh_password_cache is None:
                prompt_text = f"请输入{ubuntu_user}@{ubuntu_host}的SSH密码:"
                password = self.get_password(prompt=prompt_text)
                if not password:
                    return None
                self.ssh_password_cache = password
            ssh.connect(
                hostname=ubuntu_host,
                username=ubuntu_user,
                password=self.ssh_password_cache,
                timeout=10
            )
            return ssh
        except Exception as e:
            self.log_message(f"❌ SSH 连接失败: {e}")
            return None

    def check_ssh_connectivity(self, user, host):
        ssh = self.get_ssh_connection()
        if not ssh:
            return False
        try:
            cmd = f"ssh -o BatchMode=yes -o ConnectTimeout=5 {user}@{host} 'echo OK' >/dev/null 2>&1"
            stdin, stdout, stderr = ssh.exec_command(cmd, timeout=10)
            exit_code = stdout.channel.recv_exit_status()
            return exit_code == 0
        except:
            return False
        finally:
            self.release_ssh_connection(ssh)

    def setup_ssh_key_auth(self, user, host, password):
        ssh = self.get_ssh_connection()
        if not ssh:
            return
        try:
            # 确保 SSH 密钥存在 ~/.ssh/id_rsa
            cmd1 = "mkdir -p ~/.ssh && chmod 700 ~/.ssh && [ -f ~/.ssh/id_rsa ] || ssh-keygen -t rsa -b 2048 -N '' -f ~/.ssh/id_rsa"
            ssh.exec_command(cmd1, timeout=10)

            # 安装 sshpass
            cmd2 = "which sshpass >/dev/null || sudo apt-get update && sudo apt-get install -y sshpass"
            ssh.exec_command(cmd2, timeout=60)
            quoted_pass = shlex.quote(password)
            target = shlex.quote(f"{user}@{host}")
            # 保存为 /home/username/.ssh/known_hosts
            cmd3 = f'sshpass -p {quoted_pass} ssh-copy-id -o StrictHostKeyChecking=no {target}'
            stdin, stdout, stderr = ssh.exec_command(cmd3, timeout=60)
            output = stdout.read().decode('utf-8')
            error = stderr.read().decode('utf-8')
            if "Number of key(s) added: 1" in output or "already exist on the remote system" in error:
                self.log_message("✅ SSH 免密登录配置成功")
            else:
                self.log_message(f"⚠️ 配置可能失败:\n{output}\n{error}")
                self.show_warning("警告", "SSH 免密配置可能未成功，请验证密码")
        except Exception as e:
            self.log_message(f"❌ 配置免密失败: {e}")
            self.show_error("错误", f"自动配置 SSH 免密失败:\n{e}")
        finally:
            self.release_ssh_connection(ssh)

    def get_private_key(self):
        raw_path = self.config.get("private_key_path", "")
        if not raw_path:
            self.show_error("密钥错误", "私钥文件未指定private_key_path")
            return None
        key_path = os.path.normpath(os.path.expanduser(raw_path))
        if not os.path.exists(key_path):
            self.show_error("密钥错误", f"私钥文件不存在：\n{key_path}")
            return None
        try:
            return paramiko.RSAKey.from_private_key_file(key_path)
        except paramiko.PasswordRequiredException:
            self.show_error("密钥错误", "私钥文件受密码保护，请使用无密码密钥或移除私钥密码\nssh-keygen -p -f ~/.ssh/id_rsa")
            return None
        except Exception as e:
            self.show_error("密钥错误", f"私钥文件加载失败：{e}")
            return None

    def get_password(self, prompt=None):
        result = [None]
        dialog = tk.Toplevel(self.root)
        dialog.title("SSH密码")
        dialog.transient(self.root)
        dialog.grab_set()
        center_toplevel(dialog, 500, 250)

        def on_submit(values):
            result[0] = values['password']
            dialog.destroy()
            return True

        fields = [{'name': 'password', 'label': prompt, 'type': 'password'}]
        FormDialog(dialog, "SSH密码", 500, 250, fields, on_submit, gui_app=self)
        self.root.wait_window(dialog)
        return result[0]

    def check_and_alert_routing(self):
        ubuntu_host = self.config.get("ubuntu_host", "")
        device_host = self.device_host_var.get().strip()
        if not ubuntu_host or not device_host:
            self.show_warning("提示", "测试主机或设备主机不能为空")
            return False
        try:
            ubuntu_ip = ubuntu_host.split('@')[-1] if '@' in ubuntu_host else ubuntu_host
            device_ip = device_host.split('@')[-1] if '@' in device_host else device_host
            if not (re.match(r'^\d+\.\d+\.\d+\.\d+$', ubuntu_ip) and 
                    re.match(r'^\d+\.\d+\.\d+\.\d+$', device_ip)):
                return True
            ubuntu_network = '.'.join(ubuntu_ip.split('.')[:3]) + '.0'
            device_network = '.'.join(device_ip.split('.')[:3]) + '.0'
            if ubuntu_network == device_network:
                self.log_message(f"✅ 网段相同: {ubuntu_ip} ↔ {device_ip}")
                return True

            self.log_message(f"⚠️ 网段不同: {ubuntu_ip} ↔ {device_ip}")
            is_windows = sys.platform == "win32"
            if is_windows:
                route_cmds = [
                    "# Windows路由添加命令:",
                    "# 1. 以管理员身份打开命令提示符或PowerShell",
                    f"route add {ubuntu_network} mask 255.255.255.0 {device_ip}",
                    f"route add {device_network} mask 255.255.255.0 {ubuntu_ip}",
                    "# 检查路由表: route print",
                    "# 删除路由表: route delete {网段}"
                ]
            else:
                route_cmds = [
                    "# Linux路由添加命令:",
                    f"sudo ip route add {ubuntu_network}/24 via {device_ip}",
                    f"sudo ip route add {device_network}/24 via {ubuntu_ip}",
                    "# 检查路由表: ip route show",
                    "# 删除路由表: sudo ip route del {网段}/24"
                ]

            route_help = "\n".join(route_cmds)
            message = (
                f"⚠️ 网络路由检测警告\n\n"
                f"测试主机IP: {ubuntu_ip} (网段: {ubuntu_network}/24)\n"
                f"本地主机IP: {device_ip} (网段: {device_network}/24)\n\n"
                f"检测到测试主机和设备主机不在同一网段！\n"
                f"可能影响网络通信，建议添加路由表。\n\n"
                f"--- 路由添加命令 ---\n{route_help}\n\n"
            )
            self.log_message("📋 建议路由命令:")
            for cmd in route_cmds:
                self.log_message(f"  {cmd}")
            result = messagebox.askyesno("网络路由警告", message)
            if not result:
                self.log_message("❌ 用户取消测试（路由问题）")
                return False
            self.log_message("⚠️ 用户选择继续测试")
            return True
        except Exception as e:
            self.log_message(f"⚠️ 路由检查失败: {e}")
            return True

    # ==================== SSHD检查 ====================
    def check_ssh_button_handler(self):
        def execute_check():
            self.log_message("🔍 正在检查本地Windows电脑sshd状态...\n")
            try:
                if sys.platform != "win32":
                    self.log_message("❌ 此功能仅支持 Windows 系统\n")
                    return
                status_text, has_minor, has_major = self.check_local_windows_ssh()
                for line in status_text.split("\n"):
                    self.log_message(line)
                if has_major:
                    install_guide = (
                        "未检测到sshd服务, 以【管理员身份】运行 PowerShell, 按照下面步骤安装:\n\n"
                        "1.卸载sshd\n"
                        "Get-Service sshd | Stop-Service -Force\n"
                        "Remove-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0\n\n"
                        "2️.删除残留文件\n"
                        'Remove-Item -Path "C:\\ProgramData\\ssh" -Recurse -Force -ErrorAction SilentlyContinue\n\n'
                        "3️.重启计算机\n"
                        "Restart-Computer\n\n"
                        "4️.重启后以【管理员身份】运行 PowerShell 安装sshd\n"
                        "Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0\n"
                        "Get-WindowsCapability -Online | Where-Object Name -like 'OpenSSH*'\n\n"
                        "5️.启动sshd\n"
                        "Start-Service sshd\n\n"
                        "6️.设置sshd开机自启动\n"
                        "Set-Service -Name sshd -StartupType 'Automatic'\n"
                    )
                    self.show_warning("sshd安装指导", install_guide)
                    self.log_message(f"🔴 sshd服务异常, 需要重装修复 {install_guide}\n")
                elif has_minor:
                    start_guide = (
                        "sshd服务未设置开机自启动\n\n"
                        "以【管理员身份】运行 PowerShell\n\n"
                        "Set-Service -Name sshd -StartupType 'Automatic'\n"
                        "Start-Service sshd\n"
                    )
                    self.show_warning("sshd启动项设置", start_guide)
                    self.log_message(f"🟡 sshd服务异常 {start_guide}\n")
                else:
                    self.log_message("\n✅ sshd服务运行正常\n")
            except Exception as e:
                self.log_message(f"❌ SSH检查失败: {e}\n")
        threading.Thread(target=execute_check, daemon=True).start()

    def check_local_windows_ssh(self):
        try:
            if sys.platform != "win32":
                return "⚠️ 当前不是Windows系统", False

            def run_powershell(cmd, timeout=8):
                try:
                    result = subprocess.run(
                        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                        capture_output=True,
                        text=True,
                        timeout=timeout
                    )
                    return result.stdout.strip(), result.stderr.strip(), None
                except subprocess.TimeoutExpired:
                    return "", "", "超时"
                except Exception as e:
                    return "", "", str(e)

            status_details = []
            minor_issues = []
            major_issues = []

            # 1️⃣ 检查安装状态
            install_check_cmd = r"""
    $client = Test-Path "$env:WINDIR\System32\OpenSSH\ssh.exe"
    $serverExe = Test-Path "$env:WINDIR\System32\OpenSSH\sshd.exe"
    $service = Get-Service sshd -ErrorAction SilentlyContinue

    if ($client) { "CLIENT_INSTALLED" } else { "CLIENT_NOT_INSTALLED" }
    if ($serverExe -or $service) { "SERVER_INSTALLED" } else { "SERVER_NOT_INSTALLED" }
    """
            output, _, _ = run_powershell(install_check_cmd)

            client_installed = "CLIENT_INSTALLED" in output
            server_installed = "SERVER_INSTALLED" in output

            status_details.append("✅ OpenSSH客户端: 已安装" if client_installed else "❌ OpenSSH客户端: 未安装")
            status_details.append("✅ OpenSSH服务器: 已安装" if server_installed else "❌ OpenSSH服务器: 未安装")

            if not client_installed:
                minor_issues.append("OpenSSH客户端未安装")
            if not server_installed:
                major_issues.append("OpenSSH服务器未安装或已损坏")

            # 2️⃣ SSHD 服务状态
            service_cmd = r"""
    $service = Get-Service sshd -ErrorAction SilentlyContinue
    if ($service) {
        "STATUS=" + $service.Status
        "STARTTYPE=" + $service.StartType
    } else {
        "NOT_FOUND"
    }
    """
            service_output, _, _ = run_powershell(service_cmd)

            if "NOT_FOUND" in service_output:
                status_details.append("❌ SSHD服务: 不存在")
                major_issues.append("SSHD服务不存在（SSH服务损坏）")
            else:
                status_details.append("SSHD服务信息:\n" + service_output)

                if "STATUS=Running" not in service_output:
                    minor_issues.append("SSHD服务未运行")

                if "STARTTYPE=Automatic" not in service_output:
                    minor_issues.append("SSHD服务未设置自动启动")

            # 4️⃣ 生成报告
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            status_text = f"Windows SSH检查 ({timestamp})\n" + "=" * 50 + "\n" + "\n".join(status_details)

            has_major = bool(major_issues)
            has_minor = bool(minor_issues)
            if has_minor or has_major:
                status_text += "\n" + "=" * 50
                status_text += "\n⚠️ 发现问题:\n"

                for issue in major_issues:
                    status_text += f"  🔴 {issue}\n"
                for issue in minor_issues:
                    status_text += f"  🟡 {issue}\n"
            return status_text, has_minor, has_major
        except Exception as e:
            return f"❌ 检查异常: {str(e)}", True

    # ==================== VPN连接 ====================
    def connect_vpn(self):
        self.vpn_connect_button.config(state=tk.DISABLED)
        self.vpn_check_button.config(state=tk.DISABLED)
        self.vpn_status_label.config(text="状态: 连接中...")
        self.log_message("🔄 尝试连接 VPN...")

        def connect_task(ssh):
            cmd = "sudo nmcli connection up hcq2"
            self.log_message(f"🔧 执行命令: {cmd}")
            stdin, stdout, stderr = ssh.exec_command(cmd, timeout=20)
            exit_code = stdout.channel.recv_exit_status()
            if exit_code == 0:
                return "✅ 连接成功"
            else:
                err_msg = stderr.read().decode('utf-8').strip()
                if "already active" in err_msg:
                    return "✅ 已连接"
                elif "unknown connection" in err_msg:
                    self.log_message("❌ 连接 'hcq2' 不存在")
                    return "❌ 连接不存在"
                else:
                    self.log_message(f"❌ 错误信息: {err_msg}")
                    return "❌ 连接失败"

        def update_ui(status_text):
            self._update_vpn_status_ui(status_text)

        self.execute_ssh_task(connect_task, update_ui)

    def check_vpn_status(self):
        self.vpn_check_button.config(state=tk.DISABLED)
        self.vpn_connect_button.config(state=tk.DISABLED)
        self.vpn_status_label.config(text="状态: 检查中...")

        def update_ui(status):
            if status == "connected":
                status_text = "✅ 已连接"
            elif status == "disconnected":
                status_text = "❌ 未连接"
            else:
                status_text = f"状态: {status}"
            self._update_vpn_status_ui(status_text)

        self.execute_ssh_task(self._get_vpn_status, update_ui)

    def _get_vpn_status(self, ssh_client):
        targets = self.config.get("vpn_target", [])
        if isinstance(targets, str):
            targets = [targets]
        for target in targets:
            try:
                if '.' in target and not target.replace('.', '').isdigit():
                    cmd = f"timeout 5 nslookup {target} >/dev/null 2>&1 && timeout 5 ping -c 1 -W 3 {target}"
                else:
                    cmd = f"timeout 5 ping -c 1 -W 3 {target}"
                _, stdout, _ = ssh_client.exec_command(cmd, timeout=10)
                if stdout.channel.recv_exit_status() == 0:
                    return "connected"
            except:
                continue
        return "disconnected"

    def _update_vpn_status_ui(self, status_text):
        self.root.after(0, lambda: self.vpn_status_label.config(text=f"状态: {status_text}"))
        self.root.after(0, lambda: self.vpn_check_button.config(state=tk.NORMAL))
        self.root.after(0, lambda: self.vpn_connect_button.config(state=tk.NORMAL))

    def execute_ssh_task(self, task_func, ui_update_func, *args, **kwargs):
        def run_task():
            ssh = self.get_ssh_connection()
            if not ssh:
                ui_update_func("❌ 操作失败")
                return
            try:
                result = task_func(ssh, *args, **kwargs)
                ui_update_func(result)
            except Exception as e:
                self.log_message(f"❌ 任务异常: {e}")
                ui_update_func("❌ 操作失败")
            finally:
                if ssh:
                    self.release_ssh_connection(ssh)

        thread = threading.Thread(target=run_task, daemon=True)
        thread.start()

    # ==================== 设备操作 ====================
    def refresh_devices(self):
        for widget in self.device_scrollable_frame.winfo_children():
            widget.destroy()
        self.device_vars = {}
        thread = threading.Thread(target=self._refresh_devices_thread, daemon=True)
        thread.start()

    def _refresh_devices_thread(self):
        ssh = self.get_ssh_connection()
        if not ssh:
            return
        stdin, stdout, stderr = ssh.exec_command("adb devices", timeout=10)
        output = stdout.read().decode('utf-8')
        current_devices = {line.split('\t')[0] for line in output.splitlines() if '\tdevice' in line}
        
        devices_to_remove = [dev for dev in self.device_vars.keys() if dev not in current_devices]
        devices_to_add = [dev for dev in current_devices if dev not in self.device_vars]

        def update_gui():
            for dev in devices_to_remove:
                for widget in self.device_scrollable_frame.winfo_children():
                    if isinstance(widget, ttk.Checkbutton) and widget.cget("text") == dev:
                        widget.destroy()
                        if dev in self.device_vars:
                            del self.device_vars[dev]
            for dev in devices_to_add:
                var = tk.BooleanVar()
                self.device_vars[dev] = var
                ttk.Checkbutton(self.device_scrollable_frame, text=dev, variable=var).pack(anchor=tk.W, padx=5, pady=2)
        
        self.root.after(0, update_gui)
        self.release_ssh_connection(ssh)
        self.log_message(f"✅ 刷新设备完成: {', '.join(current_devices) if current_devices else '无设备'}")

    def select_all_devices(self):
        if not self.device_vars:
            self.show_warning("设备列表", "当前未检测到任何设备，请先刷新设备列表。")
            return
        all_selected = all(var.get() for var in self.device_vars.values())
        for var in self.device_vars.values():
            var.set(not all_selected)
        self.log_message("✅ 已全选设备" if not all_selected else "🔁 已取消全选")

    def get_selected_devices(self, min_selected=1):
        selected_devices = [dev for dev, var in self.device_vars.items() if var.get()]
        if len(selected_devices) < min_selected:
            if min_selected == 1:
                self.show_warning("设备选择", "请选择一个ADB设备")
            else:
                self.show_warning("设备选择", f"请至少选择 {min_selected} 个ADB设备")
            return None
        return selected_devices

    def reboot_devices(self):
        selected_devices = self.get_selected_devices()
        if selected_devices is None:
            return
        if not messagebox.askyesno("确认重启", f"确定要重启以下 {len(selected_devices)} 个设备吗？\n" + "\n".join(selected_devices)):
            return

        def build_cmd(device):
            return f"adb -s {device} reboot"

        def wait_for_devices(ssh, devices):
            for device in devices:
                self._wait_for_device_online(ssh, device, timeout=60)

        self.execute_device_action(selected_devices, build_cmd, "重启", post_action_hook=wait_for_devices)

    def remount_devices(self):
        selected_devices = self.get_selected_devices()
        if selected_devices is None:
            return

        def build_cmd(device):
            return f"adb -s {device} root && adb -s {device} remount"

        def post_action_hook(ssh, devices):
            time.sleep(2)
            for device in devices:
                try:
                    cmd = f"adb -s {device} shell getprop ro.boot.veritymode"
                    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=10)
                    veritymode = stdout.read().decode('utf-8').strip()
                    if veritymode == "enforcing":
                        self.show_warning("设备重启提示", 
                            f"设备 {device} 需要重启才能使 remount 设置生效！\n\n"
                            "请点击「重启设备」按钮来重启设备。")
                    elif veritymode == "disabled":
                        self.log_message(f"✅ 设备 {device} verity 已禁用，无需重启")
                except Exception as e:
                    self.log_message(f"⚠️ 检查设备 {device} 状态失败: {e}")

        self.execute_device_action(selected_devices, build_cmd, "Remount", post_action_hook=post_action_hook)

    def connect_wifi(self):
        selected_devices = self.get_selected_devices()
        if selected_devices is None:
            return

        def on_submit(values):
            ssid = values['ssid'].strip()
            password = values['password'].strip()
            if not ssid or not password:
                self.show_error("输入错误", "SSID 和密码不能为空")
                return False

            def build_cmd(device):
                enable_cmd = f"adb -s {device} shell cmd wifi set-wifi-enabled enabled"
                connect_cmd = f'adb -s {device} shell cmd wifi connect-network "{ssid}" wpa2 "{password}"'
                return f"{enable_cmd} && sleep 2 && {connect_cmd}"

            self.execute_device_action(selected_devices, build_cmd, f"连接Wi-Fi({ssid})")
            return True

        fields = [
            {'name': 'ssid', 'label': 'Wi-Fi 名称:', 'default': 'AndroidWifi'},
            {'name': 'password', 'label': 'Wi-Fi 密码:', 'default': '1234567890', 'type': 'password'}
        ]
        FormDialog(self.root, "连接Wi-Fi", 500, 250, fields, on_submit, gui_app=self)

    def lock_selected_devices(self, action: str):
        selected_devices = self.get_selected_devices()
        if selected_devices is None:
            return
        if action == "lock":
            title, message = "确认锁定", (
                f"确定要锁定以下 {len(selected_devices)} 个设备吗？\n" +
                "\n".join(selected_devices) +
                "\n⚠️ 锁定后可能无法刷机或调试！"
            )
        else:
            title, message = "确认解锁", f"确定要解锁以下 {len(selected_devices)} 个设备吗？\n" + "\n".join(selected_devices)
        if not messagebox.askyesno(title, message):
            return

        def upload_lock_script():
            local_script = resource_path("run_Device_Lock.sh")
            remote_script = self.get_home_path("GMS-Suite", "run_Device_Lock.sh")
            return self.upload_file_to_ubuntu(local_script, remote_script)

        def build_cmd(device):
            remote_script = self.get_home_path("GMS-Suite", "run_Device_Lock.sh")
            return f"{remote_script} {device} {action}"

        self.execute_device_action(selected_devices, build_cmd, action, pre_action_hook=upload_lock_script)

    def check_device_lock_status(self):
        selected_devices = self.get_selected_devices()
        if selected_devices is None:
            return
        ssh = self.get_ssh_connection()
        if not ssh:
            return
        try:
            for device in selected_devices:
                self.log_message(f"🔍 查询设备 {device} 的锁定状态...")
                cmd = f"adb -s {device} shell getprop ro.boot.verifiedbootstate"
                stdin, stdout, stderr = ssh.exec_command(cmd, timeout=10)
                output = stdout.read().decode('utf-8').strip()
                error = stderr.read().decode('utf-8').strip()
                if error and "not found" not in error:
                    self.log_message(f"❌ 设备 {device} 查询失败: {error}")
                    continue
                if output == "green":
                    self.log_message(f"✅ 设备 {device}: 已锁定 (verifiedbootstate = green)")
                elif output == "orange":
                    self.log_message(f"⚠️ 设备 {device}: 未锁定 (verifiedbootstate = orange)")
                elif output == "":
                    self.log_message(f"❓ 设备 {device}: 无法获取 verifiedbootstate（可能不支持或未启动完成）")
                else:
                    self.log_message(f"ℹ️ 设备 {device}: verifiedbootstate = {output}")
        except Exception as e:
            self.log_message(f"💥 查询锁定状态异常: {e}")
        finally:
            if 'ssh' in locals() and ssh:
                self.release_ssh_connection(ssh)

    def collect_device_info(self):
        selected_devices = self.get_selected_devices()
        if selected_devices is None:
            return
        info_window = tk.Toplevel(self.root)
        info_window.title("设备信息收集")
        center_toplevel(info_window, 900, 700)
        text_widget = scrolledtext.ScrolledText(info_window, wrap=tk.WORD)
        text_widget.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        def collect_info_thread():
            ssh = self.get_ssh_connection()
            if not ssh:
                return
            info_commands = [
                ("设备序列号", "adb -s {device} shell getprop ro.serialno"),
                ("设备型号", "adb -s {device} shell getprop ro.product.model"),
                ("Android版本", "adb -s {device} shell getprop ro.build.version.release"),
                ("编译类型", "adb -s {device} shell getprop ro.build.type"),
                ("编译标签", "adb -s {device} shell getprop ro.build.tags"),
                ("编译时间", "adb -s {device} shell getprop ro.build.date"),
                ("SDK版本", "adb -s {device} shell getprop ro.build.version.sdk"),
                ("DATA分区", "adb -s {device} shell cat vendor/etc/fstab.rk30board | grep userdata"),
                ("api_level", "adb -s {device} shell getprop | grep api_level"),
                ("Mali库版本", "adb -s {device} shell getprop sys.gmali.version"),
                ("安全补丁", "adb -s {device} shell getprop ro.build.version.security_patch"),
                ("指纹", "adb -s {device} shell getprop ro.build.fingerprint"),
                ("内存信息", "adb -s {device} shell cat /proc/meminfo | grep -E 'MemTotal|MemFree'"),
                ("时区设置", "adb -s {device} shell getprop persist.sys.timezone"),
                ("语言设置", "adb -s {device} shell getprop persist.sys.locale")
            ]
            for device in selected_devices:
                text_widget.insert(tk.END, f"\n{'='*60}\n")
                text_widget.insert(tk.END, f"设备: {device}\n")
                text_widget.insert(tk.END, f"{'='*60}\n\n")
                for label, cmd_template in info_commands:
                    try:
                        cmd = cmd_template.format(device=device)
                        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=10)
                        output = stdout.read().decode('utf-8', errors='replace').strip()
                        error = stderr.read().decode('utf-8', errors='replace').strip()
                        text_widget.insert(tk.END, f"【{label}】\n")
                        if output:
                            text_widget.insert(tk.END, f"{output}\n")
                        if error and "not found" not in error:
                            text_widget.insert(tk.END, f"错误: {error}\n")
                        text_widget.insert(tk.END, "\n")
                        info_window.update()
                    except Exception as e:
                        text_widget.insert(tk.END, f"【{label}】 收集失败: {e}\n\n")
            text_widget.insert(tk.END, f"\n{'='*60}\n")
            text_widget.insert(tk.END, "设备信息收集完成\n")
            self.release_ssh_connection(ssh)

        thread = threading.Thread(target=collect_info_thread, daemon=True)
        thread.start()

    def burn_firmware(self):
        selected_devices = self.get_selected_devices()
        if selected_devices is None:
            return
        self.firmware_path_var = tk.StringVar()

        def on_submit(values):
            firmware_path = values['firmware'].strip()
            if not firmware_path:
                self.show_error("路径错误", "请选择固件文件")
                return False
            if not os.path.isfile(firmware_path):
                self.show_error("文件错误", f"固件文件不存在: {firmware_path}")
                return False
            thread = threading.Thread(target=self._burn_firmware_thread, args=(selected_devices, firmware_path), daemon=True)
            thread.start()
            return True

        fields = [{
                'name': 'firmware', 
                'label': '固件文件:', 
                'type': 'local_file', 
                'var': self.firmware_path_var,
                'filetypes': [("固件文件", "*.img *.bin *.update"), ("所有文件", "*.*")]
        }]
        FormDialog(self.root, "烧写固件", 500, 250, fields, on_submit, gui_app=self)

    def _burn_firmware_thread(self, devices, firmware_path):
        def upload_firmware():
            local_tool = resource_path("upgrade_tool")
            remote_tool = self.get_home_path("GMS-Suite", "upgrade_tool")
            if not self.upload_file_to_ubuntu(local_tool, remote_tool):
                return False
            firmware_name = os.path.basename(firmware_path)
            remote_firmware = self.get_home_path("GMS-Suite", firmware_name)
            if not self.upload_file_to_ubuntu(firmware_path, remote_firmware):
                return False
            return True
        
        def enter_loader_mode():
            ssh = self.get_ssh_connection()
            if not ssh:
                return False
            try:
                self.log_message("🚀 让设备进入 Loader 模式...")
                for device in devices:
                    try:
                        cmd = f"adb -s {device} reboot loader"
                        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=5)
                        self.log_message(f"✅ 设备 {device} 已发送进入 Loader 模式命令")
                    except Exception as e:
                        self.log_message(f"⚠️ 设备 {device} 无法发送重启命令: {e}")
                self.log_message("⏳ 等待设备进入 Loader 模式...")
                time.sleep(8)

                gms_suite_dir = self.get_home_path("GMS-Suite")
                check_cmd = f"cd {shlex.quote(gms_suite_dir)} && ./upgrade_tool ld"
                stdin, stdout, stderr = ssh.exec_command(check_cmd, timeout=5)
                output = stdout.read().decode('utf-8', errors='replace').strip()
                if output and "List of rockusb connected" in output:
                    self.log_message(f"✅ 检测到 Loader 设备:\n{output}")
                    return True
                else:
                    self.log_message("⚠️ 未检测到 Loader 设备，请检查设备连接")
                    return False
            except Exception as e:
                self.log_message(f"⚠️ 进入 Loader 模式异常: {e}")
                return False
            finally:
                if ssh:
                    self.release_ssh_connection(ssh)

        def build_cmd(device):
            firmware_name = os.path.basename(firmware_path)
            gms_suite_dir = self.get_home_path("GMS-Suite")
            return f"cd {shlex.quote(gms_suite_dir)} && ./upgrade_tool uf {shlex.quote(firmware_name)}"

        if not upload_firmware():
            self.log_message("❌ 文件上传失败，中止烧写")
            return
        if not enter_loader_mode():
            return

        self.log_message("🔧 开始烧写固件...")
        self.execute_device_action(devices, build_cmd, "烧写固件", pre_action_hook=None)

    def burn_gsi_image(self):
        selected_devices = self.get_selected_devices()
        if selected_devices is None:
            return
        self.gsi_system_var = tk.StringVar(value=self._last_gsi_system_path)
        self.gsi_vendor_var = tk.StringVar(value=self._last_gsi_vendor_path)

        def on_submit(values):
            script_path = values['script'].strip()
            system_img = self.gsi_system_var.get().strip()
            vendor_img = self.gsi_vendor_var.get().strip()
            if not script_path:
                self.show_error("路径错误", "请指定 GSI 烧写脚本路径")
                return False
            if not system_img:
                self.show_error("路径错误", "请指定 System 镜像路径")
                return False
            self._last_gsi_system_path = system_img
            self._last_gsi_vendor_path = vendor_img
            thread = threading.Thread(
                target=self._burn_gsi_image_thread,
                args=(selected_devices, system_img, vendor_img),
                daemon=True
            )
            thread.start()
            return True

        default_script = self.config.get("gsi_scripts", self.get_home_path("GMS-Suite", "run_GSI_Burn.sh"))
        fields = [
            {'name': 'script', 'label': 'GSI烧写脚本:', 'default': default_script, 'type': 'readonly'},
            {'name': 'system', 'label': 'System 镜像:', 'type': 'remote_file', 'var': self.gsi_system_var},
            {'name': 'vendor', 'label': 'Vendor Boot:', 'type': 'local_file', 'var': self.gsi_vendor_var}
        ]
        FormDialog(self.root, "烧写GSI镜像", 500, 250, fields, on_submit, gui_app=self)

    def _burn_gsi_image_thread(self, devices, system_img, vendor_img):
        def upload_gsi_files():
            success = True
            local_script = resource_path("run_GSI_Burn.sh")
            remote_script = self.get_home_path("GMS-Suite", "run_GSI_Burn.sh")
            success &= self.upload_file_to_ubuntu(local_script, remote_script)
            local_misc = resource_path("misc.img")
            remote_misc = self.get_home_path("GMS-Suite", "misc.img")
            success &= self.upload_file_to_ubuntu(local_misc, remote_misc)

            if vendor_img.strip():
                local_vendor = vendor_img
                if not os.path.isfile(local_vendor):
                    self.log_message(f"❌ Vendor Boot 镜像不存在: {local_vendor}")
                    return False
                remote_vendor = self.get_home_path("GMS-Suite", os.path.basename(local_vendor))
                success &= self.upload_file_to_ubuntu(local_vendor, remote_vendor)
                self._remote_vendor_path = remote_vendor
            else:
                self._remote_vendor_path = ""
            return success

        def build_cmd(device):
            remote_script = self.get_home_path("GMS-Suite", "run_GSI_Burn.sh")
            img_args = f"--system {shlex.quote(system_img)}"
            if self._remote_vendor_path:
                img_args += f" --vendor {shlex.quote(self._remote_vendor_path)}"
            return f"{remote_script} {device} {img_args}"

        self.execute_device_action(devices, build_cmd, "烧写 GSI", pre_action_hook=upload_gsi_files)

    def burn_serial_number(self):
        selected_devices = self.get_selected_devices()
        if selected_devices is None:
            return
        self.show_warning("提示", "该功能未实现")

    def _wait_for_device_online(self, ssh, device, timeout=60):
        self.log_message(f"⏳ 等待设备 {device} 重新上线...")
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                cmd = f"adb -s {device} get-state"
                stdin, stdout, stderr = ssh.exec_command(cmd, timeout=10)
                output = stdout.read().decode('utf-8').strip()
                if "device" in output:
                    self.log_message(f"✅ 设备 {device} 已重新上线")
                    return True
            except Exception as e:
                pass
            time.sleep(2)
        self.log_message(f"⚠️ 设备 {device} 超时未上线")
        return False

    def execute_device_action(self, devices, build_cmd_func, action_name, 
                            pre_action_hook=None, post_action_hook=None):
        if not devices:
            self.log_message(f"⚠️ {action_name}: 无设备可操作")
            return

        def execute_in_thread():
            try:
                if pre_action_hook and not pre_action_hook():
                    self.log_message(f"❌ {action_name}: 预处理失败，已中止")
                    return
                ssh = self.get_ssh_connection()
                if not ssh:
                    return
                try:
                    self.log_message(f"🔄 开始 {action_name} {len(devices)} 个设备...")
                    for device in devices:
                        cmd = build_cmd_func(device)
                        self.log_message(f"📱 {action_name} 设备: {device}")
                        stdin, stdout, stderr = ssh.exec_command(cmd, get_pty=True, timeout=300)
                        while not stdout.channel.exit_status_ready():
                            if stdout.channel.recv_ready():
                                data = stdout.channel.recv(1024).decode('utf-8', errors='replace')
                                if data:
                                    self.log_message(data.rstrip())
                            time.sleep(0.5)
                        exit_code = stdout.channel.recv_exit_status()
                        if exit_code == 0:
                            self.log_message(f"✅ 设备 {device} {action_name} 成功")
                        else:
                            error = stderr.read().decode('utf-8', errors='replace').strip()
                            self.log_message(f"❌ 设备 {device} {action_name} 失败")
                            if error:
                                self.log_message(f"stderr: {error}")
                    if post_action_hook:
                        post_action_hook(ssh, devices)
                    self.log_message(f"✅ 所有设备 {action_name} 操作完成")
                except Exception as e:
                    self.log_message(f"💥 {action_name} 异常: {e}")
                finally:
                    if ssh:
                        self.release_ssh_connection(ssh)
            except Exception as e:
                self.log_message(f"💥 {action_name} 过程出错: {e}")

        thread = threading.Thread(target=execute_in_thread, daemon=True)
        thread.start()

    # ==================== 远程桌面(VNC) ====================
    """📖 远程桌面 使用指南
    功能: Ubuntu 主机启动 x11vnc + noVNC 服务，提供浏览器访问远程桌面
    原理: Ubuntu 图形桌面 → x11vnc (VNC服务) → noVNC (WebSocket代理) → 本地浏览器

    === 测试主机端(Ubuntu)电脑设置 ===
    1. 安装工具: sudo apt-get install -y x11vnc
    2. 设置密码: x11vnc -storepasswd
    3. 安装工具: sudo apt-get update -y && sudo apt-get install -y git
                cd /opt
                sudo git clone https://github.com/novnc/noVNC.git
                sudo git clone https://github.com/novnc/websockify.git noVNC/utils/websockify
                sudo chmod +x /opt/noVNC/utils/websockify/run
    4. 自动登录: sudo nano /etc/lightdm/lightdm.conf
                    WaylandEnable=false
                    AutomaticLoginEnable = true
                    AutomaticLogin = 用户名
                sudo reboot
    5. 启动服务: export DISPLAY=:0 && export XAUTHORITY=/home/hcq/.Xauthority && 
                x11vnc -display :0 -forever -shared -rfbauth ~/.vnc/passwd -bg
                cd /opt/noVNC && nohup ./utils/websockify/run --web /opt/noVNC 6080 localhost:5901
    6. 本地界面: http://172.16.14.233:6080/vnc.html?autoconnect=true
    7. 停止服务: pkill -f x11vnc && pkill -f websockify
    """
    def init_and_start_vnc(self):
        if self.vnc_starting:
            self.log_message("⏳ VNC服务正在启动中, 请稍候...")
            return
        thread = threading.Thread(target=self._init_and_start_vnc_thread, daemon=True)
        thread.start()

    def _init_and_start_vnc_thread(self):
        self.vnc_starting = True
        self.log_message("🔧 开始启动 VNC 服务...")
        ssh = None
        try:
            ssh = self.get_ssh_connection()
            if not ssh:
                self.log_message("❌ 无法连接到 Ubuntu 主机")
                self.vnc_starting = False
                return

            # 1. 检查VNC密码
            self.log_message("🔐 检查VNC密码文件(~/.vnc/passwd)...")
            check_passwd_cmd = "[ -f ~/.vnc/passwd ] && echo 'exists' || echo 'missing'"
            stdin, stdout, stderr = ssh.exec_command(check_passwd_cmd, timeout=5)
            result = stdout.read().decode('utf-8', errors='replace')
            if "missing" in result:
                self.log_message("⚠️ VNC密码文件(~/.vnc/passwd)不存在")
                instructions = (
                    "\nsudo apt-get install -y x11vnc"
                    "\nx11vnc -storepasswd"
                )
                self.show_info("设置VNC密码", 
                    "需要在Ubuntu主机上设置VNC密码\n\n"
                    "请在打开的终端中执行命令：\n"
                    "x11vnc -storepasswd\n")
                self.log_message("📝 请在Ubuntu终端执行命令设置VNC密码: " + instructions)
                self.open_embedded_terminal(instructions=instructions)
                return
            else:
                self.log_message("✅ VNC密码文件(~/.vnc/passwd)已存在")

            # 2. 检查noVNC安装状态
            self.log_message("📦 检查noVNC安装状态...")
            command_to_execute = "[ -d /opt/noVNC ] && echo 'exists' || echo 'missing'"
            stdin, stdout, stderr = ssh.exec_command(command_to_execute, timeout=5)
            result = stdout.read().decode('utf-8', errors='replace')
            if "missing" in result:
                self.log_message("⚠️ noVNC未安装, 开始安装...")
                instructions = (
                    "\nsudo apt-get update -y"
                    "\nsudo apt-get install -y git"
                    "\ncd /opt"
                    "\nsudo git clone https://github.com/novnc/noVNC.git"
                    "\nsudo git clone https://github.com/novnc/websockify.git noVNC/utils/websockify"
                )
                self.log_message("📝 请在打开的Ubuntu终端执行命令安装noVNC: " + instructions)
                self.open_embedded_terminal(instructions=instructions)
                stdin, stdout, stderr = ssh.exec_command(command_to_execute, timeout=5)
                result = stdout.read().decode('utf-8', errors='replace')
                if "missing" in result:
                    self.log_message("❌ noVNC安装未完成")
                    self.show_info("安装noVNC", "请等待安装完成后重试")
                    self.vnc_starting = False
                    return
            else:
                self.log_message("✅ noVNC 已存在")

            # 3. 设置脚本权限
            chmod_cmd = "chmod +x /opt/noVNC/utils/websockify/run"
            ssh.exec_command(chmod_cmd, timeout=5)
            self.log_message("✅ 已设置noVNC脚本权限")

            # 4. 准备日志目录
            setup_cmd = "mkdir -p ~/logs"
            ssh.exec_command(setup_cmd, timeout=5)

            # 5. 检查图形桌面
            self.log_message("⏳ 等待图形桌面就绪...")
            display_ready = False
            for _ in range(60):
                command_to_execute = "export DISPLAY=:0 && xprop -root &>/dev/null && echo 'ready'"
                stdin, stdout, stderr = ssh.exec_command(command_to_execute, timeout=5)
                if "ready" in stdout.read().decode('utf-8', errors='replace'):
                    display_ready = True
                    break
                time.sleep(1)

            if not display_ready:
                self.log_message("❌ 图形桌面未就绪，请确保 Ubuntu 已自动登录")
                instructions = (
                    "sudo nano /etc/lightdm/lightdm.conf\n"
                    "修改以下内容：\n"
                    "WaylandEnable=false\n"
                    "AutomaticLoginEnable = true\n"
                    "AutomaticLogin = hcq\n"
                    "然后重启系统：sudo reboot"
                )
                self.open_embedded_terminal(instructions=instructions)
                self.show_info("配置自动登录", "请在终端中配置自动登录\n配置完成后需要重启系统")
                self.vnc_starting = False
                return
            self.log_message("✅ 图形桌面已就绪")

            # 6. 启动 x11vnc
            self.log_message("🚀 启动 x11vnc...")
            x11vnc_cmd = (
                "export DISPLAY=:0 && "
                f"export XAUTHORITY={self.get_home_path('.Xauthority')} && "
                "x11vnc -display :0 -forever -shared -rfbauth ~/.vnc/passwd -bg -o ~/logs/x11vnc.log"
            )
            stdin, stdout, stderr = ssh.exec_command(x11vnc_cmd, timeout=15)
            
            # 提取端口号
            output = stdout.read().decode('utf-8', errors='replace')
            vnc_port = None
            for line in output.splitlines():
                if line.startswith("PORT="):
                    try:
                        vnc_port = int(line.split("=")[1])
                        break
                    except (ValueError, IndexError):
                        pass
            if not vnc_port:
                self.log_message(f"❌ 未能获取 x11vnc 端口。输出:\n{output}")
                return
            self.log_message(f"✅ x11vnc 已启动，端口: {vnc_port}")

            # 7. 启动 noVNC
            self.log_message(f"🌐 启动 noVNC 连接 localhost:{vnc_port}")
            novnc_cmd = (
                f"cd /opt/noVNC && "
                f"nohup ./utils/websockify/run --web /opt/noVNC 6080 localhost:{vnc_port} "
                f"> ~/logs/novnc.log 2>&1 &"
            )
            ssh.exec_command(novnc_cmd, timeout=10)
            self.log_message("✅ VNC 服务已启动")

            ubuntu_host = self.config.get("ubuntu_host", "")
            self.show_info("成功", 
                "VNC 服务已启动！\n\n"
                "访问方式：\n"
                "1. 点击「显示屏幕」按钮\n"
                "2. 或浏览器访问: http://{host}:6080/vnc.html?autoconnect=true\n".format(host=ubuntu_host))
        except Exception as e:
            self.log_message(f"❌ 启动 VNC 服务失败: {e}")
            self.show_error("错误", f"启动失败：{str(e)}")
        finally:
            self.vnc_starting = False
            if ssh:
                self.release_ssh_connection(ssh)

    # ==================== 设备投屏 ====================
    """📖 设备投屏 使用指南
    功能: 通过 scrcpy 将 Android 设备投屏到 Ubuntu 桌面
    原理: Android设备 → ADB → Ubuntu scrcpy → Ubuntu 桌面 → VNC → 本地浏览器

    === 测试主机端(Ubuntu)电脑设置 ===
    1. 安装工具: wget https://github.com/Genymobile/scrcpy/releases/download/v3.3.4/scrcpy-linux-x86_64-v3.3.4.tar.gz
                tar -xzf scrcpy-linux-x86_64-v3.3.4.tar.gz -C ~/Software/
    2. 检查工具: ls ~/Software/scrcpy-linux-x86_64-v3.3.4/scrcpy
    2. 设备投屏: export DISPLAY=:0 && export XAUTHORITY=/home/hcq/.Xauthority && 
                /home/hcq/Software/scrcpy-linux-x86_64-v3.3.4/scrcpy -s RK3576GMS1 --max-size 800 --stay-awake --window-title 'RK3576GMS1'    
    4. 本地界面: http://172.16.14.233:6080/vnc.html?autoconnect=true
    3. 投屏进程: ps aux | grep scrcpy
    5. 停止投屏: pkill -f 'scrcpy.*-s RK3576GMS1'
    """
    def show_device_screen(self):
        if self.adb_forward_running:
            self.show_warning("提示", "端口转发不支持显示屏幕")
            return
        selected_devices = self.get_selected_devices()
        if selected_devices is None:
            return
        thread = threading.Thread(target=self._start_scrcpy_and_open_vnc, args=(selected_devices,), daemon=True)
        thread.start()

    def _start_scrcpy_and_open_vnc(self, devices):
        try:
            # 检查VNC服务
            ubuntu_host = self.config.get("ubuntu_host", "")
            if not self.is_port_open(ubuntu_host, 6080, timeout=3):
                self.log_message("⚠️ VNC服务未就绪")
                self.show_warning("VNC服务未就绪", "请先点击「启动VNC」")
                return

            ssh = self.get_ssh_connection()
            if not ssh:
                self.log_message("❌ SSH连接失败")
                return

            # 检查scrcpy安装
            if not self._check_and_install_scrcpy(ssh, devices[0]):
                self.release_ssh_connection(ssh)
                return

            devices = sorted(devices)
            running_devices = []
            pending_devices = []
            for device in devices:
                check_cmd = f"pgrep -f 'scrcpy.*-s {device}'"
                stdin, stdout, stderr = ssh.exec_command(check_cmd, timeout=3)
                if stdout.read().strip():
                    running_devices.append(device)
                else:
                    pending_devices.append(device)
            running_devices = sorted(running_devices)
            pending_devices = sorted(pending_devices)
            if len(pending_devices) == 0 and len(running_devices) > 0:
                self.log_message(f"✅ {len(running_devices)}个设备已在运行, 重新连接到VNC")
                self._launch_vnc_viewer_auto_connect()
                self.release_ssh_connection(ssh)
                return

            started_devices = []
            total_devices = len(running_devices) + len(pending_devices)
            all_devices = sorted(running_devices + pending_devices)
            self.log_message(f"📱 设备排序: {', '.join(all_devices)}")
            for idx, device in enumerate(pending_devices):
                try:
                    current_index = all_devices.index(device)
                    x, y, width, height = self._calculate_window_position(current_index, total_devices)
                    cmd = (
                        f"export DISPLAY=:0 && "
                        f"export XAUTHORITY={self.get_home_path('.Xauthority')} && "
                        f"{self.get_home_path('Software', 'scrcpy-linux-x86_64-v3.3.4', 'scrcpy')} "
                        f"-s {device} "
                        f"--max-size 800 "
                        f"--stay-awake "
                        f"--window-title '{device}' "
                        f"--window-x {x} "
                        f"--window-y {y} "
                        f"--window-width {width} "
                        f"--window-height {height} "
                        f"> /tmp/scrcpy_{device}.log 2>&1 &"
                    )
                    self.log_message(f"🚀 启动设备投屏: {device} (位置: {x},{y}, 尺寸: {width}x{height})")
                    ssh.exec_command(cmd, timeout=5)
                    time.sleep(0.2)
                    started_devices.append(device)
                    with self.active_screens_lock:
                        self.active_screens.add(device)
                except Exception as e:
                    self.log_message(f"⚠️ 启动设备失败 {device}")
            self._launch_vnc_viewer_auto_connect()
            if started_devices:
                self.log_message(f"✅ 已启动{len(started_devices)}个投屏设备: {', '.join(started_devices)}")
            if running_devices:
                self.log_message(f"ℹ️ {len(running_devices)}个设备已在运行: {', '.join(running_devices)}")
            self.release_ssh_connection(ssh)
        except Exception as e:
            self.log_message(f"❌ 显示屏幕失败: {e}")
            if 'device' in locals():
                with self.active_screens_lock:
                    self.active_screens.discard(device)

    @staticmethod
    def is_port_open(host, port, timeout=3):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((host, port)) == 0

    def _calculate_window_position(self, index, total_devices, screen_width=1920, screen_height=1080):
        horizontal_gap = 20
        vertical_margin = 50
        max_available_width = screen_width - (horizontal_gap * (total_devices + 1))
        window_width = min(600, max_available_width // total_devices)
        window_height = int(window_width * 16 / 9)
        max_height = int(screen_height * 0.7)
        if window_height > max_height:
            window_height = max_height
            window_width = int(window_height * 9 / 16)
        total_width = total_devices * window_width + (total_devices - 1) * horizontal_gap
        start_x = max(horizontal_gap, (screen_width - total_width) // 2)
        start_y = max(vertical_margin, (screen_height - window_height) // 2)
        x_offset = start_x + index * (window_width + horizontal_gap)
        y_offset = start_y
        if x_offset + window_width > screen_width:
            x_offset = max(0, screen_width - window_width - horizontal_gap)
        if y_offset + window_height > screen_height:
            y_offset = max(0, screen_height - window_height - vertical_margin)
        return x_offset, y_offset, window_width, window_height

    def _check_and_install_scrcpy(self, ssh, device):
        try:
            scrcpy_path = self.get_home_path("Software", "scrcpy-linux-x86_64-v3.3.4", "scrcpy")
            check_cmd = f"ls '{scrcpy_path}' >/dev/null 2>&1 && echo 'installed'"
            stdin, stdout, stderr = ssh.exec_command(check_cmd, timeout=5)
            if "installed" in stdout.read().decode():
                return True

            self.log_message("📥 安装 scrcpy...")
            local_file = resource_path("scrcpy-linux-x86_64-v3.3.4.tar.gz")
            remote_file = self.get_home_path("Software", "scrcpy-linux-x86_64-v3.3.4.tar.gz")
            if not self.upload_file_to_ubuntu(local_file, remote_file):
                return False
            extract_cmd = f"cd '{self.get_home_path('Software')}' && tar -xzf '{remote_file}'"
            stdin, stdout, stderr = ssh.exec_command(extract_cmd, timeout=30)
            if stdout.channel.recv_exit_status() != 0:
                return False
            self.log_message("✅ scrcpy 安装完成")
            return True
        except Exception as e:
            self.log_message(f"scrcpy 安装失败: {e}")
            return False

    def _launch_vnc_viewer_auto_connect(self):
        ubuntu_host = self.config.get("ubuntu_host", "")
        vnc_password = self.config.get("vnc_password", "")
        if not vnc_password:
            self.log_message("⚠️ 未配置 VNC 密码，请在 config.json 中设置 'vnc_password'")
            self.show_warning("配置缺失", "未设置 VNC 密码，无法自动连接。\n请在 config.json 中添加 'vnc_password' 字段。")
            return
        encoded_password = urllib.parse.quote(vnc_password)
        vnc_url = f"http://{ubuntu_host}:6080/vnc.html?autoconnect=true&password={encoded_password}"
        self.log_message(f"🌐 正在连接VNC: {ubuntu_host}:6080")
        webbrowser.open(vnc_url)

    # ==================== 测试操作 ====================
    def start_test(self):
        if self.test_running:
            self.stop_test()
            return
        vpn_connected = True
        try:
            ssh = self.get_ssh_connection()
            if ssh:
                status = self._get_vpn_status(ssh)
                vpn_connected = (status == "connected")
                self.release_ssh_connection(ssh)
        except Exception as e:
            self.log_message(f"❌ 检查 VPN 状态失败: {e}")
            vpn_connected = False

        if not vpn_connected:
            result = messagebox.askyesno(
                "提示",
                "检测到 VPN 未连接，是否继续测试？\n"
                "⚠️ 测试可能因网络问题失败！"
            )
            if not result:
                return

        selected_devices = self.get_selected_devices()
        if selected_devices is None:
            return
        test_type = self.test_type.get().strip().lower()
        if not test_type:
            self.show_warning("输入错误", "请选择测试类型")
            return
        retry_dir = self.retry_result_var.get().strip()
        self.log_text.delete(1.0, tk.END)
        if bool(retry_dir):
            thread = threading.Thread(target=self.execute_gms_test, args=(test_type,), kwargs={"retry_dir": retry_dir}, daemon=True)
        else:
            test_module = self.test_module.get().strip()
            test_case = self.test_case.get().strip()
            thread = threading.Thread(target=self.execute_gms_test, args=(test_type, test_module, test_case), daemon=True)
        thread.start()
        self.test_running = True
        self.root.after(0, lambda: self.run_button.config(text="⏹ 停止测试", style="Danger.TButton"))

    def stop_test(self):
        if not self.test_running:
            return
        self.log_message("⏹️ 用户请求停止测试...")
        self._kill_tradefed_processes()
        self.test_running = False
        self.root.after(0, lambda: self.run_button.config(text="▶ 开始测试", style="Accent.TButton"))
        self.refresh_devices()

    def clean_test(self):
        self.log_message("🧹 用户请求清除日志...")
        self.log_text.delete(1.0, tk.END)

    def execute_gms_test(self, test_type, test_module="", test_case="", retry_dir=None):
        ssh = None
        try:
            local_script = resource_path("run_GMS_Test_Auto.sh")
            remote_script = self.script_path_var.get().strip()
            if not self.upload_file_to_ubuntu(local_script, remote_script):
                self.test_running = False
                self.root.after(0, lambda: self.run_button.config(text="▶ 开始测试", style="Accent.TButton"))
                return
            ssh = self.get_ssh_connection()
            if not ssh:
                self.test_running = False
                self.root.after(0, lambda: self.run_button.config(text="▶ 开始测试", style="Accent.TButton"))
                return
            self.log_message("✅ SSH 连接成功")

            cmd_parts = [self.config["script_path"]]
            if retry_dir is not None:
                timestamp = os.path.basename(retry_dir.strip().rstrip('/'))
                cmd_parts.extend([test_type, "retry", timestamp])
                self.log_message(f"🔄 Retry 模式: {timestamp}")
            else:
                cmd_parts.append(test_type)
                if test_module:
                    cmd_parts.append(test_module)
                if test_case:
                    cmd_parts.append(test_case)

            selected_devices = self.get_selected_devices()
            if selected_devices:
                device_args_list = []
                if len(selected_devices) > 1:
                    device_args_list.extend(["--shard-count", str(len(selected_devices))])
                for device in selected_devices:
                    device_args_list.extend(["-s", device])
                device_args_str = " ".join(shlex.quote(arg) for arg in device_args_list)
                cmd_parts.extend(["--device-args", device_args_str])

            user_suite_path = self.suite_path_var.get().strip()
            if not user_suite_path:
                self.show_error("路径错误", "测试套件路径不能为空")
                self.test_running = False
                self.root.after(0, lambda: self.run_button.config(text="▶ 开始测试", style="Accent.TButton"))
                return
            if user_suite_path == self.get_home_path("GMS-Suite"):
                self.show_error("路径错误",
                    f"测试套件路径不能是父目录 '{self.get_home_path('GMS-Suite')}'！\n"
                    "请指定测试套件，例如：\n"
                    f"{self.get_home_path('GMS-Suite', 'android-cts-16_r2', 'android-cts', 'tools')}\n"
                    f"{self.get_home_path('GMS-Suite', 'android-gts-13.1-R2', 'android-gts', 'tools')}")
                self.test_running = False
                self.root.after(0, lambda: self.run_button.config(text="▶ 开始测试", style="Accent.TButton"))
                return
            cmd_parts.extend(["--test-suite", user_suite_path])

            local_server = self.local_server_var.get().strip()
            cmd_parts.extend(["--local-server", local_server])
            gms_cmd = ' '.join(shlex.quote(part) for part in cmd_parts)
            log_msgs = [
                f"🌐 本地主机: {local_server}",
                f"📂 测试套件: {user_suite_path}",
                f"📱 选中设备: {', '.join(selected_devices)}",
                f"🚀 执行命令: {gms_cmd}"
            ]
            for msg in log_msgs:
                self.log_message(msg)
            stdin, stdout, stderr = ssh.exec_command(gms_cmd, get_pty=True)
            while not stdout.channel.exit_status_ready() and self.test_running:
                if stdout.channel.recv_ready():
                    data = stdout.channel.recv(4096).decode('utf-8', errors='replace')
                    if data:
                        self.log_message(data.rstrip())
                if stderr.channel.recv_stderr_ready():
                    error = stderr.channel.recv_stderr(4096).decode('utf-8', errors='replace')
                    if error:
                        self.log_message(f"stderr: {error.rstrip()}")
                time.sleep(0.1)
            
            if not self.test_running:
                self.log_message("⏹️ 测试已停止")
            else:
                exit_code = stdout.channel.recv_exit_status()
                self.log_message(f"📊 测试完成，退出码: {exit_code}")
        except Exception as e:
            self.log_message(f"❌ 执行出错: {str(e)}")
        finally:
            self.test_running = False
            self.root.after(0, lambda: self.run_button.config(text="▶ 开始测试", style="Accent.TButton"))
            if ssh:
                self.release_ssh_connection(ssh)

    def _kill_tradefed_processes(self):
        """强制终止远程主机上所有 tradefed 相关进程"""
        ssh = self.get_ssh_connection()
        if not ssh:
            self.log_message("❌ 无法连接到 Ubuntu 主机，跳过进程清理")
            return
        try:
            binary_map = {
                'cts': 'cts-tradefed',
                'gsi': 'cts-tradefed',
                'gts': 'gts-tradefed',
                'sts': 'sts-tradefed',
                'vts': 'vts-tradefed',
                'apts': 'gts-tradefed'
            }
            test_type = self.test_type.get().strip().lower()
            tradefed_bin = binary_map.get(test_type)
            if not tradefed_bin:
                self.log_message(f"❌ 未知的测试类型: {test_type}")
                return
            kill_cmd = f"pkill -f '[./]?{tradefed_bin}.*run commandAndExit'"
            self.log_message(f"🧹 正在终止 {test_type.upper()} 测试进程...")
            stdin, stdout, stderr = ssh.exec_command(kill_cmd, timeout=10)
            exit_code = stdout.channel.recv_exit_status()

            if exit_code == 0:
                self.log_message(f"✅ {test_type.upper()} tradefed 进程已成功终止")
            else:
                error_output = stderr.read().decode('utf-8').strip()
                # pkill 返回 1 表示没有进程被杀死，这不是错误
                if exit_code == 1 or (error_output and "no process found" in error_output.lower()):
                    self.log_message(f"ℹ️ 未发现正在运行的 {test_type.upper()} 测试进程")
                elif error_output:
                    self.log_message(f"⚠️ 终止 {test_type.upper()} 时出现错误: {error_output}")
            time.sleep(1)
            self.refresh_devices()
        except Exception as e:
            self.log_message(f"💥 终止 tradefed 进程异常: {e}")
        finally:
            self.release_ssh_connection(ssh)

    def auto_complete_suite_path(self, ssh_client, base_path, test_type):
        maps = {
            'cts': {'subdir': 'android-cts', 'binary': 'cts-tradefed'},
            'gsi': {'subdir': 'android-cts', 'binary': 'cts-tradefed'},
            'gts': {'subdir': 'android-gts', 'binary': 'gts-tradefed'},
            'sts': {'subdir': 'android-sts', 'binary': 'sts-tradefed'},
            'vts': {'subdir': 'android-vts', 'binary': 'vts-tradefed'},
            'apts': {'subdir': 'android-gts', 'binary': 'gts-tradefed'}
        }
        config = maps.get(test_type.lower())
        if not config:
            self.log_message(f"❌ 不支持的测试类型: {test_type}")
            self.show_error("错误", f"不支持的测试类型: {test_type}")
            return None
        candidate = f"{base_path}/{config['subdir']}/tools"
        check_cmd = f"[ -x '{candidate}/{config['binary']}' ] && echo '{candidate}' || echo ''"
        self.log_message(f"🔧 检测路径: {check_cmd}")
        try:
            stdin, stdout, stderr = ssh_client.exec_command(check_cmd, timeout=8)
            result = stdout.read().decode().strip()
            if result:
                self.log_message(f"✅ 找到 {config['binary']} → 使用路径: {result}")
                return result
        except Exception as e:
            self.log_message(f"❌ 检查路径时出错: {e}")
        self.log_message(f"❌ 在 {base_path} 下未找到有效的 {config['binary']}")
        self.show_error("路径错误",
            f"无法在所选目录中找到可执行文件:\n{config['binary']}\n"
            f"请确认路径存在且权限正确：\n{candidate}")
        return None

    # ==================== 打开终端 ====================
    def open_embedded_terminal(self, instructions=None, command_to_execute=None):
        """打开嵌入式SSH终"""
        ssh = self.get_ssh_connection()
        if not ssh:
            self.show_error("SSH 错误", "无法连接到 Ubuntu 主机")
            return
        try:
            ssh.exec_command("echo test", timeout=5)
            terminal = EmbeddedTerminalWindow(self, ssh)
            if instructions:
                def show_instructions():
                    terminal._clear_log()
                    for line in instructions.split('\n'):
                        terminal._write_to_text(f"# {line}\n")
                    terminal._write_to_text("\n# 请在终端拷贝执行以上命令\n")

                    if terminal.channel and terminal.channel.send_ready():
                        terminal.channel.send('\n')

                thread = threading.Thread(target=show_instructions, daemon=True)
                thread.start()
            return terminal
        except Exception as e:
            self.show_error("终端错误", f"无法启动终端: {e}")
            self.release_ssh_connection(ssh)

    # ==================== 文件传输 ====================
    def upload_file_to_ubuntu(self, local_path: str, remote_path: str) -> bool:
        if not os.path.isfile(local_path):
            self.log_message(f"❌ 本地文件不存在: {local_path}")
            return False
        file_size = os.path.getsize(local_path)
        file_name = os.path.basename(local_path)

        def format_size(size_bytes):
            for unit in ['B', 'KB', 'MB', 'GB']:
                if size_bytes < 1024.0 or unit == 'GB':
                    return f"{size_bytes:.2f}{unit}"
                size_bytes /= 1024.0

        file_size_str = format_size(file_size)
        self.log_message(f"📤 上传文件: {file_name} → {remote_path} ({file_size_str})")

        ssh = None
        sftp = None
        try:
            self.upload_progress_var.set(0)
            ssh = self.get_ssh_connection()
            if not ssh:
                return False
            sftp = ssh.open_sftp()
            remote_dir = os.path.dirname(remote_path)
            path = ""
            for d in [d for d in remote_dir.split('/') if d]:
                path += '/' + d
                try:
                    sftp.stat(path)
                except FileNotFoundError:
                    sftp.mkdir(path)
            start_time = time.time()
            last_time = start_time
            last_size = 0

            def update_progress(transferred, total):
                nonlocal last_time, last_size
                now = time.time()
                if now - last_time < 0.5:
                    return
                percent = (transferred / total * 100) if total > 0 else 0
                time_diff = now - last_time
                size_diff = transferred - last_size
                if time_diff > 0 and size_diff > 0:
                    speed = size_diff / time_diff
                    if speed >= 1024*1024:
                        speed_str = f"{speed/1024/1024:.1f}MB/s"
                    elif speed >= 1024:
                        speed_str = f"{speed/1024:.1f}KB/s"
                    else:
                        speed_str = f"{speed:.1f}B/s"
                    remaining = total - transferred
                    if speed > 0:
                        remaining_sec = remaining / speed
                        if remaining_sec < 60:
                            remain_str = f"{remaining_sec:.0f}秒"
                        elif remaining_sec < 3600:
                            remain_str = f"{remaining_sec/60:.0f}分"
                        else:
                            remain_str = f"{remaining_sec/3600:.1f}小时"
                    else:
                        remain_str = "计算中..."
                    info = f"{percent:.1f}% | {speed_str} | 剩余: {remain_str}"
                else:
                    info = f"{percent:.1f}%"
                self.upload_progress_var.set(percent)
                self._update_progress_info(info)
                last_time = now
                last_size = transferred

            sftp.put(local_path, remote_path, callback=update_progress)
            total_time = time.time() - start_time
            avg_speed = file_size / total_time if total_time > 0 else 0

            # 设置可执行权限（如果需要）
            script_extensions = {'.sh', '.py', '.bash', '.pl', '.rb', '.exe'}
            executable_files = {'upgrade_tool'}
            ext = os.path.splitext(remote_path)[1].lower()
            filename = os.path.basename(remote_path)
            if ext in script_extensions or filename in executable_files:
                sftp.chmod(remote_path, 0o755)
                self.log_message(f"🔐 已设置可执行权限: {remote_path}")

            avg_speed_str = format_size(avg_speed) + "/s"
            self.log_message(f"✅ 上传完成 ({file_size_str}, 用时: {total_time:.1f}秒, 平均速度: {avg_speed_str})")
            self.upload_progress_var.set(100)
            self._update_progress_info("上传完成")
            return True
        except Exception as e:
            self._update_progress_info("上传失败")
            return False
        finally:
            if sftp:
                try:
                    sftp.close()
                except:
                    pass
            if ssh:
                self.release_ssh_connection(ssh)
            self.root.after(3000, lambda: self._update_progress_info(""))

    def _update_progress_info(self, text):
        if not hasattr(self, 'progress_info_label'):
            if hasattr(self, 'upload_progress'):
                parent = self.upload_progress.master
                self.progress_info_label = ttk.Label(parent, text="", font=('TkDefaultFont', 8))
                self.progress_info_label.grid(row=self.upload_progress.grid_info()['row'] + 1, 
                                            column=0, columnspan=3, sticky=tk.W, pady=(2, 0))
        if hasattr(self, 'progress_info_label'):
            self.progress_info_label.config(text=text)

    def on_file_drop(self, event):
        files = event.widget.tk.splitlist(event.data)
        if files:
            file_path = files[0].strip('{}')
            self.local_file_var.set(file_path)

    def handle_upload_file(self):
        remote_base_dir = self.config.get("suites_path", self.get_home_path("GMS-Suite")).rstrip("/")
        local_path = self.local_file_var.get().strip()
        if not local_path or not os.path.isfile(local_path):
            self.show_error("文件错误", "请选择一个有效的本地文件")
            return
        remote_path = f"{remote_base_dir}/tmp/{os.path.basename(local_path)}"
        thread = threading.Thread(target=lambda: self.upload_file_to_ubuntu(local_path, remote_path), daemon=True)
        thread.start()

class EmbeddedTerminalWindow:
    def __init__(self, parent, ssh_client):
        self.parent = parent
        self.ssh = ssh_client
        self.channel = None
        self.running = False
        self.max_lines = 5000  # 最大行数限制

        self.window = tk.Toplevel(parent.root)
        self.window.title("Ubuntu Terminal")
        center_toplevel(self.window, 900, 600)

        # 创建菜单栏
        self._create_menu_bar()
        
        # 状态栏
        self.status_frame = ttk.Frame(self.window)
        self.status_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=(0, 5))
        self.status_label = ttk.Label(self.status_frame, text="已连接", relief=tk.SUNKEN, anchor=tk.W)
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        # 终端文本区域
        self.text_frame = ttk.Frame(self.window)
        self.text_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=(0, 5))

        self.text_widget = tk.Text(
            self.text_frame,
            wrap=tk.NONE,
            font=("Consolas", 10),
            bg="black",
            fg="white",
            insertbackground="white",
            selectbackground="#264F78",
            selectforeground="white"
        )
        
        # 滚动条
        self.scroll_y = ttk.Scrollbar(self.text_frame, orient=tk.VERTICAL, command=self.text_widget.yview)
        self.scroll_x = ttk.Scrollbar(self.text_frame, orient=tk.HORIZONTAL, command=self.text_widget.xview)
        self.text_widget.configure(yscrollcommand=self.scroll_y.set, xscrollcommand=self.scroll_x.set)

        self.scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        self.scroll_x.pack(side=tk.BOTTOM, fill=tk.X)
        self.text_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 绑定事件
        self.text_widget.bind("<Key>", self._on_key_press)
        self.text_widget.bind("<Button-1>", lambda e: self.text_widget.focus_set())
        self.text_widget.bind("<Control-a>", self._select_all)
        self.text_widget.focus_set()

        # 启动SSH通道
        self._start_ssh_channel()
        self.window.protocol("WM_DELETE_WINDOW", self._on_close)

    def _create_menu_bar(self):
        """创建菜单栏"""
        menubar = tk.Menu(self.window)
        self.window.config(menu=menubar)
        edit_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="编辑", menu=edit_menu)
        edit_menu.add_command(label="复制", command=self._copy_selection, accelerator="Ctrl+C")
        edit_menu.add_command(label="粘贴", command=self._paste_from_clipboard, accelerator="Ctrl+V")
        edit_menu.add_command(label="全选", command=self._select_all, accelerator="Ctrl+A")
        edit_menu.add_command(label="清空日志", command=self._clear_log)

    def _start_ssh_channel(self):
        """启动SSH通道"""
        try:
            self.channel = self.ssh.invoke_shell(term='xterm-256color', width=120, height=30)
            self.channel.settimeout(0.05)
            self.channel.send("printf '\\e[?2004l' && stty -ixon && stty erase ^H && export TERM=xterm-256color && exec bash -l\n")
            self.channel.send("clear\n")
            time.sleep(0.1)

            self.running = True
            self.read_thread = threading.Thread(target=self._read_loop, daemon=True)
            self.read_thread.start()
            self.parent.log_message("✅ 实时终端已启动")
        except Exception as e:
            self._write_to_text(f"❌ 终端启动失败: {e}\n")
            self.parent.log_message(f"❌ 终端启动失败: {e}")
            self._update_status("连接失败")

    @staticmethod
    def _clean_ansi(text):
        """清理ANSI转义序列"""
        ansi_escape = re.compile(r'\x1B(?:\[[0-?]*[ -/]*[@-~]|\].*?(?:\x07|\x1B\\))')
        text = ansi_escape.sub('', text)
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        text = text.replace('\x07', '')
        return text

    def _read_loop(self):
        """读取SSH输出"""
        while self.running:
            try:
                if self.channel and self.channel.recv_ready():
                    data = self.channel.recv(4096)
                    if data:
                        text = data.decode('utf-8', errors='replace')
                        clean_text = self._clean_ansi(text)
                        self._write_to_text(clean_text)
                    time.sleep(0.01)
                elif self.channel and self.channel.closed:
                    self._write_to_text("\n[连接已关闭]\n")
                    self._update_status("已断开")
                    break
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    self._write_to_text(f"\n[连接错误: {e}]\n")
                    self._update_status("连接错误")
                break

    def _write_to_text(self, text):
        """写入文本到GUI"""
        def _update():
            scroll_pos = self.text_widget.yview()
            self.text_widget.insert(tk.END, text)
            line_count = int(self.text_widget.index('end-1c').split('.')[0])
            if line_count > self.max_lines:
                self.text_widget.delete('1.0', f'{line_count - self.max_lines//2}.0')
            if scroll_pos[1] >= 0.999:
                self.text_widget.see(tk.END)

        self.parent.root.after(0, _update)

    def _on_key_press(self, event):
        """处理键盘输入"""
        if not self.channel or not self.channel.send_ready() or not self.running:
            return "break"
        # 处理Ctrl组合键
        if event.state & 0x4:  # Control键按下
            if event.keysym.lower() == 'a':
                self._select_all()
                return "break"
        # 特殊键映射
        key_map = {
            'Return': '\n',
            'BackSpace': '\x7f',
            'Tab': '\t',
            'Up': '\x1b[A',
            'Down': '\x1b[B',
            'Right': '\x1b[C',
            'Left': '\x1b[D',
            'Delete': '\x1b[3~',
            'Home': '\x1b[H',
            'End': '\x1b[F',
            'Escape': '\x1b',
        }
        
        if event.keysym in key_map:
            self.channel.send(key_map[event.keysym])
            return "break"
        if event.char:
            self.channel.send(event.char)
            return "break"
        return "break"

    def _copy_selection(self):
        """复制选中的文本"""
        try:
            if self.text_widget.tag_ranges("sel"):
                selected_text = self.text_widget.get("sel.first", "sel.last")
                self.window.clipboard_clear()
                self.window.clipboard_append(selected_text)
        except tk.TclError:
            pass

    def _paste_from_clipboard(self):
        """粘贴文本到终端"""
        try:
            clipboard_text = self.window.clipboard_get()
            if clipboard_text and self.channel and self.channel.send_ready():
                self.channel.send(clipboard_text)
        except tk.TclError:
            pass

    def _select_all(self):
        """全选文本"""
        self.text_widget.tag_add("sel", "1.0", "end")
        self.text_widget.focus_set()

    def _clear_log(self):
        """清空日志"""
        self.text_widget.delete('1.0', tk.END)
        self.channel.send('\x0c')

    def _update_status(self, message):
        """更新状态栏"""
        def _update():
            self.status_label.config(text=message)
        self.parent.root.after(0, _update)

    def _on_close(self):
        self.running = False
        if self.channel:
            try:
                self.channel.settimeout(0.5)
                self.channel.send('exit\n')
                time.sleep(0.1)
            except:
                pass
            finally:
                try:
                    self.channel.close()
                except:
                    pass
        if self.window:
            self.window.destroy()
        self.parent.log_message("✅ 终端窗口已关闭")

class FormDialog:
    def __init__(self, parent, title, width, height, fields, on_submit, gui_app=None):
        self.parent = parent
        self.on_submit = on_submit
        self.gui_app = gui_app
        self.entries = {}
        
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(title)
        self.dialog.transient(parent)
        self.dialog.grab_set()
        center_toplevel(self.dialog, width, height)

        main_frame = ttk.Frame(self.dialog, padding="15")
        main_frame.pack(fill=tk.BOTH, expand=True)

        for i, field in enumerate(fields):
            frame = ttk.Frame(main_frame)
            frame.pack(fill=tk.X, pady=2)
            ttk.Label(frame, text=field['label']).pack(side=tk.LEFT)

            var = field.get('var', tk.StringVar(value=field.get('default', '')))

            widget = None
            if field.get('type') == 'password':
                widget = ttk.Entry(frame, textvariable=var, show="*", width=30)
            elif field.get('type') == 'readonly':
                widget = ttk.Entry(frame, textvariable=var, state='readonly', width=30)
            elif field.get('type') in ('remote_file', 'local_file'):
                widget = ttk.Entry(frame, textvariable=var, width=28)
                btn = ttk.Button(
                    frame,
                    text="📁",
                    command=lambda f=field, v=var: self._browse_file(f, v),
                    width=3
                )
                btn.pack(side=tk.RIGHT, padx=(0, 0))
                widget.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(10, 0))
                setattr(widget, 'browse_button', btn)
            else:
                widget = ttk.Entry(frame, textvariable=var, width=30)
            
            if not isinstance(widget, ttk.Entry):
                pass
            else:
                widget.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(10, 0))
            self.entries[field['name']] = (var, widget)

        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(pady=(15, 0))

        ttk.Button(btn_frame, text="确定", command=self._on_ok, width=10).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="取消", command=self._on_cancel, width=10).pack(side=tk.LEFT, padx=5)

        first_entry = next(iter(self.entries.values()))[1]
        first_entry.bind("<Return>", lambda e: self._on_ok())
        first_entry.bind("<Escape>", lambda e: self._on_cancel())
        first_entry.focus()

    def _browse_file(self, field, var):
        if field.get('type') == 'local_file':
            file_path = filedialog.askopenfilename(
                title=f"选择 {field['label']}",
                initialdir=os.path.expanduser("~"),
                filetypes=[("所有文件", "*.*")]
            )
            if file_path:
                var.set(file_path)
        else:
            gui_instance = self.gui_app
            if gui_instance and hasattr(gui_instance, 'browse_remote_file'):
                def callback(selected_path):
                    if selected_path:
                        var.set(selected_path)
                gui_instance._file_dialog_callback = callback
                gui_instance.browse_remote_file(mode="file", var=var)
            else:
                if hasattr(self.parent, 'nametowidget'):
                    root_widget = self.parent.nametowidget('.')

    def _on_ok(self):
        values = {name: var.get().strip() for name, (var, _) in self.entries.items()}
        if self.on_submit(values):
            self.dialog.destroy()

    def _on_cancel(self):
        self.dialog.destroy()

class RemoteFolderSelector:
    def __init__(self, parent, gui_instance, initial_path="/", is_retry_selector=False, is_file_selector=False):
        self.parent = parent
        self.gui_instance = gui_instance
        self.current_path = initial_path.rstrip("/")
        self.is_retry_selector = is_retry_selector
        self.is_file_selector = is_file_selector
        self.create_window()

    def create_window(self):
        self.top = tk.Toplevel(self.parent)
        self.top.title(f"选择远程文件夹 - {self.current_path}")
        self.top.resizable(True, True)
        self.top.transient(self.parent)
        self.top.grab_set()
        center_toplevel(self.top, 900, 500)
        content_frame = ttk.Frame(self.top)
        content_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=(10, 0))
        self.tree = ttk.Treeview(content_frame, columns=("name", "type", "size", "mtime"), show="headings", height=18)
        self.tree.heading("name", text="名称")
        self.tree.heading("type", text="类型")
        self.tree.heading("size", text="大小 (B)")
        self.tree.heading("mtime", text="修改时间")
        self.tree.column("name", width=450, anchor='w')
        self.tree.column("type", width=100, anchor='center')
        self.tree.column("size", width=120, anchor='e')
        self.tree.column("mtime", width=150, anchor='center')
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        vsb = ttk.Scrollbar(content_frame, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(content_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)

        toolbar = ttk.Frame(self.top)
        toolbar.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 10))
        ttk.Separator(self.top, orient='horizontal').pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(5, 0))
        self.path_label = ttk.Label(self.top, text=f"路径: {self.current_path}", font=("TkDefaultFont", 9), wraplength=600, justify='left')
        self.path_label.pack(side=tk.BOTTOM, padx=10, pady=(5, 0), anchor='w')

        ttk.Button(toolbar, text="🏠 根目录", command=self.go_home).pack(side=tk.LEFT, padx=5)
        ttk.Button(toolbar, text="🔙 返回上级", command=self.go_back).pack(side=tk.LEFT, padx=5)
        ttk.Button(toolbar, text="🔄 刷新", command=self.load_directory).pack(side=tk.LEFT, padx=5)
        ttk.Button(toolbar, text="✅ 选择此目录", command=self.select_folder).pack(side=tk.RIGHT, padx=5)
        self.tree.bind("<Double-1>", self.on_double_click)
        self.load_directory()

    def go_home(self):
        root_path = self.gui_instance.config.get("suites_path", self.gui_instance.get_home_path("GMS-Suite")).rstrip("/")
        if self.current_path != root_path:
            self.current_path = root_path
            self.update_title_and_path_label()
            self.load_directory()

    def go_back(self):
        if self.current_path == "/":
            self.show_info("提示", "已到达根目录")
            return
        parent_path = os.path.dirname(self.current_path)
        if parent_path != self.current_path:
            self.current_path = parent_path
            self.update_title_and_path_label()
            self.load_directory()

    def on_double_click(self, event):
        selected = self.tree.selection()
        if not selected:
            return
        values = self.tree.item(selected[0], "values")
        if values[1] == "目录":
            new_path = f"{self.current_path}/{values[0]}".rstrip("/")
            self.current_path = new_path
            self.update_title_and_path_label()
            self.load_directory()
        elif self.is_file_selector:
            full_path = (self.current_path.rstrip("/") + "/" + values[0]).replace("//", "/")
            self.current_path = full_path
            self.select_folder()

    def select_folder(self):
        if self.is_retry_selector:
            if self.is_file_selector:
                selected_items = self.tree.selection()
                if selected_items:
                    values = self.tree.item(selected_items[0], "values")
                    if values[1] == "文件":
                        full_path = (self.current_path.rstrip("/") + "/" + values[0]).replace("//", "/")
                        self.gui_instance.retry_result_var.set(full_path)
                        self.gui_instance.log_message(f"✅ 测试报告路径已设置: {full_path}")
                    else:
                        self.gui_instance.retry_result_var.set(self.current_path)
                        self.gui_instance.log_message(f"✅ 测试报告目录已设置: {self.current_path}")
                else:
                    self.gui_instance.retry_result_var.set(self.current_path)
                    self.gui_instance.log_message(f"✅ 测试报告目录已设置: {self.current_path}")
            else:
                self.gui_instance.retry_result_var.set(self.current_path)
                self.gui_instance.log_message(f"✅ 测试报告目录已设置: {self.current_path}")
            self.top.destroy()
            return

        if getattr(self.gui_instance, '_skip_suite_validation', False):
            selected_path = self.current_path
            if 'gsi_system_var' in dir(self.gui_instance) and self.gui_instance.gsi_system_var.get() == "":
                self.gui_instance.gsi_system_var.set(selected_path)
            elif 'gsi_vendor_var' in dir(self.gui_instance):
                self.gui_instance.gsi_vendor_var.set(selected_path)
            self.gui_instance.log_message(f"✅ 镜像路径已设置: {selected_path}")
            self.gui_instance._skip_suite_validation = False
            self.top.destroy()
            return

        test_type = self.gui_instance.test_type.get().strip().lower()
        if not test_type:
            self.show_warning("警告", "请先选择测试类型")
            return
        ssh = self.gui_instance.get_ssh_connection()
        if not ssh:
            return
        try:
            final_suite_path = self.gui_instance.auto_complete_suite_path(ssh, self.current_path, test_type)
            if final_suite_path:
                self.gui_instance.suite_path_var.set(final_suite_path)
                self.gui_instance.log_message(f"✅ 测试套件路径已设置: {final_suite_path}")
                self.top.destroy()
            else:
                if self.is_file_selector:
                    selected_items = self.tree.selection()
                    if selected_items:
                        values = self.tree.item(selected_items[0], "values")
                        if values[1] == "文件":
                            full_path = (self.current_path.rstrip("/") + "/" + values[0]).replace("//", "/")
                            if hasattr(self.gui_instance, '_file_dialog_callback') and self.gui_instance._file_dialog_callback:
                                self.gui_instance._file_dialog_callback(full_path)
                            else:
                                self.gui_instance.suite_path_var.set(full_path)
                                self.gui_instance.log_message(f"✅ 文件路径已设置: {full_path}")
                        else:
                            self.gui_instance.suite_path_var.set(self.current_path)
                            self.gui_instance.log_message(f"⚠️ 选择的路径可能无效: {self.current_path}")
                    else:
                        self.gui_instance.suite_path_var.set(self.current_path)
                        self.gui_instance.log_message(f"⚠️ 选择的路径可能无效: {self.current_path}")
                else:
                    self.gui_instance.suite_path_var.set(self.current_path)
                    self.gui_instance.log_message(f"⚠️ 选择的路径可能无效: {self.current_path}")
                    self.show_error("路径错误", 
                        f"在 '{self.current_path}' 及其子目录中未找到有效的测试套件。\n"
                        "请确保选择包含以下子目录的父目录：\n"
                        "- android-cts (用于 CTS/GSI)\n"
                        "- android-gts (用于 GTS/APTS)\n"
                        "- android-sts (用于 STS)\n"
                        "- android-vts (用于 VTS)")
        except Exception as e:
            self.show_error("错误", f"验证路径时出错:\n{str(e)}")
        finally:
            self.gui_instance.release_ssh_connection(ssh)

    def load_directory(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        ssh = self.gui_instance.get_ssh_connection()
        if not ssh:
            self.show_error("连接失败", "无法连接到远程主机")
            self.top.destroy()
            return
        try:
            cmd = f"cd '{self.current_path}' && ls -l"
            stdin, stdout, stderr = ssh.exec_command(cmd, timeout=10)
            lines = stdout.read().decode('utf-8').strip().splitlines()
            if lines and lines[0].startswith("total"):
                lines = lines[1:]
            for line in lines:
                parts = line.split(maxsplit=8)
                if len(parts) < 9:
                    continue
                permissions, _, _, _, size_str, month, day, time_year, name = parts[:9]
                if name in ['.', '..']:
                    continue
                is_dir = permissions.startswith('d')
                is_link = permissions.startswith('l')
                
                if is_link:
                    file_type = "链接"
                elif is_dir:
                    file_type = "目录"
                else:
                    file_type = "文件"
                    
                size = size_str if not is_dir else ""
                mtime = f"{month} {day} {time_year}"
                item_id = self.tree.insert("", "end", values=(name, file_type, size, mtime))
                if is_dir:
                    self.tree.item(item_id, tags=("directory",))
                    self.tree.tag_configure("directory", font=("TkDefaultFont", 9, "bold"))
        except Exception as e:
            self.show_error("错误", f"读取目录失败:\n{str(e)}")
        finally:
            self.gui_instance.release_ssh_connection(ssh)

    def update_title_and_path_label(self):
        self.top.title(f"选择远程文件夹 - {self.current_path}")
        for widget in self.top.winfo_children():
            if isinstance(widget, ttk.Label) and widget.cget("text").startswith("路径:"):
                widget.config(text=f"路径: {self.current_path}")
                break

def main():
    root = tkdnd.Tk()
    style = ttk.Style()
    style.theme_use('default')
    style.configure("Accent.TButton", background="#4CAF50", foreground="white", font=('TkDefaultFont', 9, 'bold'))
    style.map("Accent.TButton", background=[('active', '#43A047')])
    style.configure("Danger.TButton", background="#f44336", foreground="white", font=('TkDefaultFont', 9, 'bold'))
    style.map("Danger.TButton", background=[('active', '#d32f2f')])
    try:
        app = GmsTestGUI(root)
        root.mainloop()
    except Exception as e:
        print(f"程序异常: {e}")
    finally:
        if 'app' in locals():
            app.cleanup_on_exit()

if __name__ == "__main__":
    main()
