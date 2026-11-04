# -*- coding: utf-8 -*-
"""
NJIT MCL 整合包 / 存档发布工具（本机可视化）

用途：
  把打包好的整合包或存档 zip 一次性推送到「社团内网服务器 + GitHub Release」，
  再把元数据登记到社团网站（njitmc.club）的启动器接口，最后回读各下载源做校验。

目录约定（保证 GitHub 与内网服务器结构一致）：
  整合包：wg  /modpacks/<服务器ID>/<服务器ID>-<版本>.zip
          GitHub Release tag <服务器ID>-<版本>，资产同名
  存档：  wg  /modpacks/<服务器ID>/saves/<服务器ID>-<存档标识>.zip
          GitHub Release tag saves-<服务器ID>，资产同名

为什么要在本机跑：
  njitmc.club 是公网服务器，访问不到只在内网/组网里的 wg.njit-fkcy.top；
  本机既能连 GitHub，又能连内网服务器，所以由本机做中转与校验。

依赖：标准库 + paramiko（pip install paramiko）
"""

import hashlib
import http.client
import json
import os
import queue
import re
import shutil
import ssl
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    import paramiko
except ImportError:  # 缺依赖时给出明确提示，而不是直接崩
    paramiko = None

# ===== 以下为整合包编辑模块（原 pack_edit.py，已内联，便于单文件分发） =====
OVERRIDE_PREFIXES = ("overrides/", "client-overrides/", "server-overrides/")


def detect_format(zip_path):
    """识别包格式：modrinth / curseforge / offline。"""
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    if "modrinth.index.json" in names:
        return "modrinth"
    if "manifest.json" in names:
        return "curseforge"
    return "offline"


def read_entries(zip_path):
    """读取包内条目，返回 (format, entries)。

    entry = {
        kind:   manifest（清单条目）/ override（覆盖文件）/ file（离线包文件）
        path:   清单里的安装路径，或 zip 内的相对路径
        label:  展示用文本
        size:   字节数
        raw:    原始清单条目（仅 manifest）
    }
    """
    fmt = detect_format(zip_path)
    entries = []
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        sizes = {n: zf.getinfo(n).file_size for n in names}
        if fmt == "modrinth":
            index = json.loads(zf.read("modrinth.index.json").decode("utf-8"))
            for item in index.get("files", []):
                path = str(item.get("path") or "")
                if not path:
                    continue
                # 没有下载源但 overrides 里有同名文件的条目，实际文件由 overrides 提供，
                # 这里不再重复展示，避免出现两条同样的路径
                if not item.get("downloads") and ("overrides/" + path) in names:
                    continue
                entries.append({"kind": "manifest", "path": path, "label": path,
                                "size": int(item.get("fileSize") or 0), "raw": item})
            for name in names:
                if name == "modrinth.index.json":
                    continue
                if name.startswith(OVERRIDE_PREFIXES):
                    entries.append({"kind": "override", "path": name, "label": name,
                                    "size": sizes[name], "raw": None})
        elif fmt == "curseforge":
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            for item in manifest.get("files", []):
                pid, fid = item.get("projectID"), item.get("fileID")
                entries.append({"kind": "manifest", "path": "project %s / file %s" % (pid, fid),
                                "label": "CurseForge 项目 %s 文件 %s" % (pid, fid),
                                "size": 0, "raw": item})
            for name in names:
                if name == "manifest.json":
                    continue
                if name.startswith("overrides/"):
                    entries.append({"kind": "override", "path": name, "label": name,
                                    "size": sizes[name], "raw": None})
        else:
            for name in names:
                entries.append({"kind": "file", "path": name, "label": name,
                                "size": sizes[name], "raw": None})
    entries.sort(key=lambda e: (e["kind"] != "manifest", e["path"]))
    return fmt, entries


def sha1_of(path):
    digest = hashlib.sha1()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha512_of(path):
    digest = hashlib.sha512()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def modrinth_entry_for_jar(jar_path, download_urls=None):
    """把本地 jar 转成 modrinth.index.json 的 files 条目（用于「从 Modrinth 添加」或自定义 jar）。"""
    name = os.path.basename(jar_path)
    return {
        "path": "mods/" + name,
        "hashes": {"sha1": sha1_of(jar_path), "sha512": sha512_of(jar_path)},
        "env": {"client": "required", "server": "required"},
        "downloads": list(download_urls or []),
        "fileSize": os.path.getsize(jar_path),
    }


def fetch_modrinth_version(slug, loader=None, game_version=None):
    """从 Modrinth 取一个版本的清单条目；返回 (entry, 版本号, 文件名)。"""
    url = "https://api.modrinth.com/v2/project/%s/version" % slug
    req = urllib.request.Request(url, headers={"User-Agent": "NJITMCL-Publisher"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        versions = json.loads(resp.read().decode("utf-8"))
    for ver in versions:
        if loader and loader.lower() not in [x.lower() for x in (ver.get("loaders") or [])]:
            continue
        if game_version and game_version not in (ver.get("game_versions") or []):
            continue
        files = ver.get("files") or []
        primary = next((f for f in files if f.get("primary")), files[0] if files else None)
        if not primary:
            continue
        hashes = primary.get("hashes") or {}
        entry = {
            "path": "mods/" + primary.get("filename", "mod.jar"),
            "hashes": {"sha1": hashes.get("sha1", ""), "sha512": hashes.get("sha512", "")},
            "env": {"client": "required", "server": "required"},
            "downloads": [primary.get("url")],
            "fileSize": int(primary.get("size") or 0),
        }
        return entry, ver.get("version_number", ""), primary.get("filename", "")
    raise RuntimeError("Modrinth 上没有匹配当前加载器/游戏版本的版本")


def build_modified_pack(src_zip, dst_zip, remove_paths, add_files=None):
    """按删除清单与新增文件重建整合包。

    remove_paths: 要删除的路径集合（清单里的 path 或 zip 内路径）
    add_files:    [(zip 内目标路径, 本地文件路径)]，例如 ("mods/xx.jar", "D:/xx.jar")
    返回 dict：{format, replaced: [清单内路径], removed: [zip 内路径], added: [zip 内路径], manifest_name}
    """
    fmt = detect_format(src_zip)
    if fmt == "curseforge":
        raise RuntimeError("CurseForge 包暂不支持修改（清单只有 projectID/fileID，无法按路径安全增删）")
    remove_paths = set(remove_paths or [])
    add_files = list(add_files or [])
    # 清单里的 path 形如 mods/xx.jar，而覆盖文件在 overrides/mods/xx.jar，
    # 删除时要连同各 overrides 前缀一起匹配，否则文件会残留在包里
    remove_zip_names = set()
    for path in remove_paths:
        normalized = path.replace("\\", "/")
        remove_zip_names.add(normalized)
        for prefix in OVERRIDE_PREFIXES:
            if not normalized.startswith(prefix):
                remove_zip_names.add(prefix + normalized)
    replaced, removed, added = [], [], []
    # 输出路径不能等于源包：ZipFile(dst, "w") 会立即截断源文件
    if os.path.exists(dst_zip) and os.path.realpath(src_zip) == os.path.realpath(dst_zip):
        raise ValueError("输出路径不能与源整合包相同，请另存为其他文件名")
    with zipfile.ZipFile(src_zip) as src, zipfile.ZipFile(dst_zip, "w", zipfile.ZIP_DEFLATED) as dst:
        names = [n for n in src.namelist() if not n.endswith("/")]
        if fmt == "modrinth":
            index = json.loads(src.read("modrinth.index.json").decode("utf-8"))
            kept = []
            for item in index.get("files", []):
                path = str(item.get("path") or "")
                if path in remove_paths:
                    replaced.append(path)
                    continue
                # 没有下载源、但 overrides 里已有同名文件的条目会让客户端安装失败，
                # 这类条目直接丢弃，文件由 overrides 提供
                if not item.get("downloads") and ("overrides/" + path) in names:
                    replaced.append(path)
                    continue
                kept.append(item)
            add_map = {}
            for target, local in add_files:
                with open(local, "rb") as fp:
                    # 本地 jar 没有公开下载地址，只放进 overrides，不写进清单
                    add_map["overrides/" + target.replace("\\", "/")] = fp.read()
            for override_name, data in add_map.items():
                dst.writestr(override_name, data)
                added.append(override_name)
            add_targets = set(add_map)
            index["files"] = kept
            dst.writestr("modrinth.index.json",
                         json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8"))
            written = set()
            for name in names:
                if name == "modrinth.index.json" or name in written:
                    continue
                if name in add_targets:
                    # 源包里的同名旧文件必须跳过，否则会把刚写入的新文件覆盖回去
                    continue
                if name in remove_zip_names:
                    removed.append(name)
                    continue
                dst.writestr(name, src.read(name))
                written.add(name)
            manifest_name = "modrinth.index.json"
        else:
            written = set()
            for name in names:
                if name in written:
                    continue
                if name in remove_zip_names:
                    removed.append(name)
                    continue
                dst.writestr(name, src.read(name))
                written.add(name)
            for target, local in add_files:
                data = open(local, "rb").read()
                dst.writestr(target.replace("\\", "/"), data)
                added.append(target)
            manifest_name = ""
        using = set(dst.namelist())
    return {"format": fmt, "replaced": replaced, "removed": removed,
            "added": added, "manifest_name": manifest_name, "entries": sorted(using)}


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_pack_meta(zip_path):
    """从原始整合包读取可自动填写的元数据，读不到就返回空字段。

    返回 dict：format / name / version / loader / loader_version / mc_version。
    - Modrinth .mrpack：modrinth.index.json（name、versionId、dependencies）
    - CurseForge / 通用 zip：manifest.json（name、version、minecraft.modLoaders）
    - 离线包：只能判断 format，其余留空
    """
    meta = {"format": "", "name": "", "version": "", "loader": "",
            "loader_version": "", "mc_version": ""}
    try:
        fmt = detect_format(zip_path)
    except Exception:
        return meta
    meta["format"] = {"modrinth": "mrpack",
                      "curseforge": "curseforge-zip",
                      "offline": "offline-zip"}.get(fmt, "")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            if fmt == "modrinth":
                data = json.loads(zf.read("modrinth.index.json").decode("utf-8"))
                meta["name"] = str(data.get("name") or "").strip()
                meta["version"] = str(data.get("versionId") or "").strip()
                deps = data.get("dependencies") or {}
                if isinstance(deps, dict):
                    meta["mc_version"] = str(deps.get("minecraft") or "")
                    for key, loader in (("forge", "forge"), ("neoforge", "neoforge"),
                                        ("fabric-loader", "fabric"), ("quilt-loader", "quilt")):
                        if deps.get(key):
                            meta["loader"] = loader
                            meta["loader_version"] = str(deps[key])
                            break
            elif fmt == "curseforge":
                data = json.loads(zf.read("manifest.json").decode("utf-8"))
                meta["name"] = str(data.get("name") or "").strip()
                meta["version"] = str(data.get("version") or "").strip()
                mc = data.get("minecraft") or {}
                meta["mc_version"] = str(mc.get("version") or "")
                loaders = [item for item in (mc.get("modLoaders") or []) if isinstance(item, dict)]
                primary = next((item for item in loaders if item.get("primary")), None)
                if primary is None and loaders:
                    primary = loaders[0]
                if primary is not None:
                    raw = str(primary.get("id") or "")
                    if "-" in raw:
                        meta["loader"], meta["loader_version"] = raw.split("-", 1)
    except Exception:
        return meta
    return meta


def read_manifest(zip_path):
    """提取包的安装清单（实例内相对路径 + 文件大小），供客户端自动识别本地版本。

    modrinth：取 modrinth.index.json 的 files；
    offline：直接取 zip 内全部文件（安装时会解压到实例目录）。
    curseforge 的 manifest 只有项目 ID，无法生成路径清单，返回空。
    """
    fmt = detect_format(zip_path)
    result = []
    with zipfile.ZipFile(zip_path) as zf:
        if fmt == "modrinth":
            index = json.loads(zf.read("modrinth.index.json").decode("utf-8"))
            sizes = {}
            for item in index.get("files", []):
                path = str(item.get("path") or "").replace("\\", "/").strip()
                size = int(item.get("fileSize") or 0)
                if path and size > 0:
                    sizes[path] = size
            # overrides 里的文件安装时会覆盖到实例目录，必须一并登记；
            # 否则新增/替换的模组不体现在清单里，客户端识别不出差异
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/")
                for prefix in OVERRIDE_PREFIXES:
                    if name.startswith(prefix):
                        rel = name[len(prefix):]
                        if rel:
                            sizes[rel] = info.file_size
                        break
            result = [{"path": path, "size": sizes[path]} for path in sorted(sizes)]
        elif fmt == "offline":
            for info in zf.infolist():
                if info.is_dir():
                    continue
                result.append({"path": info.filename.replace("\\", "/"), "size": info.file_size})
    return result


if getattr(sys, "frozen", False):
    # 打包成 exe 后 __file__ 指向临时解压目录，配置文件要跟着 exe 走
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULT_CONFIG = {
    "apiBase": "https://njitmc.club",
    "githubOwner": "njit-fkcy",
    "githubRepo": "PCL-NJITMCL-Modpacks",
    "githubRepoLauncher": "PCL-NJITMCL",
    "githubToken": "",
    "wgHost": "172.21.17.188",
    "wgUser": "root",
    "wgPassword": "",
    "wgKeyFile": "",
    "wgRemoteDir": "/var/www/modpacks",
    "proxies": [
        "https://ghfast.top/",
        "https://gh-proxy.com/",
        "https://gh.llkk.cc/",
    ],
}

# GitHub Release 单文件上限 2 GB，留出余量后按 1.9 GB 切片（split 命名规范）
SPLIT_LIMIT = 1_900_000_000

# ---------------------------------------------------------------- 版本与自更新

# 发布工具自身的版本号；每次重新分发 exe 时递增 versionCode 并覆盖 njitmcl-tools 仓库 latest Release 的资产
PUBLISHER_VERSION = "1.1.0"
PUBLISHER_VERSION_CODE = 3
PUBLISHER_REPO = "njitmcl-tools"
PUBLISHER_RELEASE_TAG = "latest"
PUBLISHER_MANIFEST_URLS = [
    "https://github.com/njit-fkcy/%s/releases/download/%s/publisher.json" % (PUBLISHER_REPO, PUBLISHER_RELEASE_TAG),
    "https://gh-proxy.com/https://github.com/njit-fkcy/%s/releases/download/%s/publisher.json" % (PUBLISHER_REPO, PUBLISHER_RELEASE_TAG),
]
PUBLISHER_DOWNLOAD_PAGE = "https://github.com/njit-fkcy/%s/releases/tag/%s" % (PUBLISHER_REPO, PUBLISHER_RELEASE_TAG)


# ---------------------------------------------------------------- 基础工具


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
                cfg.update(json.load(fp))
        except (OSError, ValueError):
            pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
        json.dump(cfg, fp, ensure_ascii=False, indent=2)


def sha256_of(path, progress=None):
    """分块计算 SHA256，避免大文件占满内存。"""
    digest = hashlib.sha256()
    total = os.path.getsize(path)
    done = 0
    with open(path, "rb") as fp:
        while True:
            chunk = fp.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    return digest.hexdigest()


def http_json(method, url, token=None, payload=None, timeout=30):
    """发一个 JSON 请求，返回 (状态码, 解析后的对象或文本)。"""
    data = None
    headers = {"User-Agent": "NJITMCL-Publisher"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Admin-Token"] = token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode("utf-8"))
            except ValueError:
                return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw.decode("utf-8"))
        except ValueError:
            return err.code, raw.decode("utf-8", "replace")


def github_api(method, url, token, payload=None):
    data = None
    headers = {
        "Authorization": "token " + token,
        "User-Agent": "NJITMCL-Publisher",
        "Accept": "application/vnd.github+json",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw.decode("utf-8")) if raw else {})
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw.decode("utf-8"))
        except ValueError:
            return err.code, raw.decode("utf-8", "replace")


def github_upload_asset_safe(owner, repo, token, release_id, name, path, force=True, progress=None):
    """上传资产；同名已存在时先删再传，并处理 GitHub 删除异步生效导致的 already_exists。"""
    last_error = ""
    for attempt in range(4):
        code, release = github_api(
            "GET", "https://api.github.com/repos/%s/%s/releases/%s" % (owner, repo, release_id), token)
        if code == 200:
            for item in release.get("assets") or []:
                if item.get("name") == name:
                    if not force:
                        return item
                    github_api("DELETE",
                               "https://api.github.com/repos/%s/%s/releases/assets/%s" % (owner, repo, item["id"]),
                               token)
                    time.sleep(1.0 + attempt * 0.8)
                    break
        try:
            return github_upload_asset(owner, repo, token, release_id, name, path, progress=progress)
        except RuntimeError as exc:
            last_error = str(exc)
            if "already_exists" not in last_error or attempt >= 3:
                raise
            time.sleep(1.5 + attempt)
    raise RuntimeError("GitHub 资产上传冲突：" + last_error)


def github_upload_asset(owner, repo, token, release_id, name, path, progress=None):
    """流式上传 Release 资产，避免把整个文件读进内存。"""
    size = os.path.getsize(path)
    conn = http.client.HTTPSConnection("uploads.github.com", timeout=300)
    quoted = urllib.parse.quote(name, safe="")
    conn.putrequest("POST", f"/repos/{owner}/{repo}/releases/{release_id}/assets?name={quoted}")
    conn.putheader("Authorization", "token " + token)
    conn.putheader("User-Agent", "NJITMCL-Publisher")
    conn.putheader("Content-Type", "application/zip")
    conn.putheader("Content-Length", str(size))
    conn.endheaders()
    sent = 0
    with open(path, "rb") as fp:
        while True:
            chunk = fp.read(1024 * 1024)
            if not chunk:
                break
            conn.send(chunk)
            sent += len(chunk)
            if progress:
                progress(sent, size)
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", "replace")
    conn.close()
    if resp.status not in (200, 201):
        raise RuntimeError(f"GitHub 上传失败（{resp.status}）：{body[:300]}")
    return json.loads(body)


def head_size(url, timeout=15):
    """HEAD 一个地址，返回 (状态码, Content-Length)，失败抛异常。"""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "NJITMCL-Publisher"})
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
        length = resp.headers.get("Content-Length")
        return resp.status, int(length) if length else None


# ---------------------------------------------------------------- 图形界面


class Publisher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("NJIT MCL 整合包发布工具 v%s" % PUBLISHER_VERSION)
        self.geometry("920x760")
        self.minsize(880, 700)

        self.cfg = load_config()
        self.admin_token = ""
        self.servers = []
        self.log_queue = queue.Queue()

        self.pack_var_text = tk.StringVar()
        self.sha_var = tk.StringVar(value="—")
        self.size_var = tk.StringVar(value="—")
        self.status_var = tk.StringVar(value="请先登录中控")

        self._build_ui()
        self.after(120, self._drain_log)
        self.after(200, self._auto_login_if_possible)
        self.after(1200, self._check_publisher_update)

    # ---------------- UI

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # 中控登录
        login = ttk.LabelFrame(self, text="1. 登录中控（njitmc.club 管理员）")
        login.pack(fill="x", **pad)

        ttk.Label(login, text="站点地址").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.api_var = tk.StringVar(value=self.cfg["apiBase"])
        ttk.Entry(login, textvariable=self.api_var, width=34).grid(row=0, column=1, sticky="w")

        ttk.Label(login, text="管理员密码").grid(row=0, column=2, sticky="w", padx=6)
        self.pwd_var = tk.StringVar()
        ttk.Entry(login, textvariable=self.pwd_var, show="*", width=22).grid(row=0, column=3, sticky="w")

        ttk.Label(login, text="动态验证码").grid(row=0, column=4, sticky="w", padx=6)
        self.totp_var = tk.StringVar()
        ttk.Entry(login, textvariable=self.totp_var, width=10).grid(row=0, column=5, sticky="w")

        ttk.Button(login, text="登录并拉取服务器", command=self.login).grid(row=0, column=6, padx=8)
        ttk.Label(login, textvariable=self.status_var, foreground="#666").grid(row=1, column=0, columnspan=7, sticky="w", padx=6, pady=(0, 6))

        # 包信息
        info = ttk.LabelFrame(self, text="2. 发布内容")
        info.pack(fill="x", **pad)

        self.mode_var = tk.StringVar(value="pack")
        modebar = ttk.Frame(info)
        modebar.grid(row=0, column=0, columnspan=4, sticky="w", padx=6, pady=(6, 0))
        ttk.Radiobutton(modebar, text="整合包", value="pack", variable=self.mode_var,
                        command=self._on_mode_change).pack(side="left")
        ttk.Radiobutton(modebar, text="存档", value="save", variable=self.mode_var,
                        command=self._on_mode_change).pack(side="left", padx=(14, 0))
        ttk.Label(modebar, text="（存档会登记到所选服务器的「已结档」卡片）", foreground="#888").pack(side="left", padx=(10, 0))

        ttk.Label(info, text="目标服务器").grid(row=1, column=0, sticky="w", padx=6, pady=6)
        self.server_box = ttk.Combobox(info, state="readonly", width=40)
        self.server_box.grid(row=1, column=1, columnspan=3, sticky="w")
        self.server_box.bind("<<ComboboxSelected>>", lambda _e: self._refresh_save_packs())

        ttk.Label(info, text="名称").grid(row=2, column=0, sticky="w", padx=6)
        self.name_var = tk.StringVar()
        ttk.Entry(info, textvariable=self.name_var, width=44).grid(row=2, column=1, sticky="w")

        ttk.Label(info, text="版本号").grid(row=2, column=2, sticky="w", padx=6)
        self.version_var = tk.StringVar(value="1.0.0")
        ttk.Entry(info, textvariable=self.version_var, width=14).grid(row=2, column=3, sticky="w")

        ttk.Label(info, text="加载器").grid(row=3, column=0, sticky="w", padx=6, pady=6)
        self.loader_var = tk.StringVar(value="fabric")
        ttk.Combobox(info, textvariable=self.loader_var, values=["fabric", "forge", "neoforge", "quilt", "vanilla"], width=12).grid(row=3, column=1, sticky="w")

        ttk.Label(info, text="加载器版本").grid(row=3, column=2, sticky="w", padx=6)
        self.loader_ver_var = tk.StringVar()
        ttk.Entry(info, textvariable=self.loader_ver_var, width=14).grid(row=3, column=3, sticky="w")

        ttk.Label(info, text="包格式").grid(row=4, column=0, sticky="w", padx=6)
        self.format_var = tk.StringVar(value="curseforge-zip")
        ttk.Combobox(info, textvariable=self.format_var, values=["curseforge-zip", "offline-zip", "mrpack"], width=16).grid(row=4, column=1, sticky="w")

        ttk.Label(info, text="Java 版本要求").grid(row=4, column=2, sticky="w", padx=6)
        self.java_var = tk.StringVar(value="自动（不指定）")
        self.java_box = ttk.Combobox(info, textvariable=self.java_var, state="readonly", width=14,
                                     values=["自动（不指定）", "17", "21", "22", "24", "25"])
        self.java_box.grid(row=4, column=3, sticky="w")

        custom = ttk.Frame(info)
        custom.grid(row=5, column=0, columnspan=4, sticky="w", padx=6, pady=(2, 2))
        self.default_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(custom, text="设为该服务器的默认整合包", variable=self.default_var).pack(side="left")
        self.enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(custom, text="在网站与启动器中启用", variable=self.enabled_var).pack(side="left", padx=(18, 0))

        ttk.Label(info, text="名称、版本、加载器在选包后自动识别，可再手动修改；自定义项均可留默认。",
                  foreground="#888").grid(row=6, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 6))

        # 存档专用信息
        self.savebox = ttk.LabelFrame(self, text="2b. 存档信息（存档模式）")
        ttk.Label(self.savebox, text="存档名称").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.save_name_var = tk.StringVar()
        ttk.Entry(self.savebox, textvariable=self.save_name_var, width=44).grid(row=0, column=1, sticky="w")
        ttk.Label(self.savebox, text="存档标识").grid(row=0, column=2, sticky="w", padx=6)
        self.save_id_var = tk.StringVar(value="save1")
        ttk.Entry(self.savebox, textvariable=self.save_id_var, width=16).grid(row=0, column=3, sticky="w")
        ttk.Label(self.savebox, text="关联整合包").grid(row=1, column=0, sticky="w", padx=6, pady=(0, 6))
        self.save_pack_box = ttk.Combobox(self.savebox, state="readonly", width=44)
        self.save_pack_box.grid(row=1, column=1, columnspan=3, sticky="w")
        ttk.Label(self.savebox, text="标识用于文件名与下载目录（小写字母、数字、短横线）；关联整合包决定解压到哪个已安装版本。",
                  foreground="#888").grid(row=2, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 6))

        # 文件
        self.filebox = filebox = ttk.LabelFrame(self, text="3. 选择文件")
        filebox.pack(fill="x", **pad)
        self.file_var = tk.StringVar()
        ttk.Entry(filebox, textvariable=self.file_var, width=76, state="readonly").grid(row=0, column=0, padx=6, pady=6, sticky="w")
        ttk.Button(filebox, text="浏览…", command=self.pick_file).grid(row=0, column=1, padx=6)
        ttk.Button(filebox, text="重新计算校验", command=self.recalc).grid(row=0, column=2, padx=6)
        ttk.Label(filebox, text="大小").grid(row=1, column=0, sticky="w", padx=6)
        ttk.Label(filebox, textvariable=self.size_var).grid(row=1, column=1, sticky="w")
        ttk.Label(filebox, text="SHA256").grid(row=2, column=0, sticky="w", padx=6, pady=(0, 6))
        ttk.Label(filebox, textvariable=self.sha_var, foreground="#444").grid(row=2, column=1, columnspan=2, sticky="w")

        # 上传目标
        target = ttk.LabelFrame(self, text="4. 上传目标（wg 仅组网内可下载）")
        target.pack(fill="x", **pad)
        self.use_wg = tk.BooleanVar(value=True)
        self.use_gh = tk.BooleanVar(value=True)
        self.use_proxy = tk.BooleanVar(value=True)
        ttk.Checkbutton(target, text="社团内网 wg.njit-fkcy.top", variable=self.use_wg).grid(row=0, column=0, sticky="w", padx=6, pady=6)
        ttk.Checkbutton(target, text="GitHub Release", variable=self.use_gh).grid(row=0, column=1, sticky="w", padx=6)
        self.proxy_text = tk.StringVar()
        ttk.Checkbutton(target, textvariable=self.proxy_text, variable=self.use_proxy).grid(row=0, column=2, sticky="w", padx=6)
        self.force_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(target, text="允许覆盖同名版本（会先删 GitHub 旧资产）",
                        variable=self.force_var).grid(row=1, column=0, columnspan=3, sticky="w", padx=6, pady=(0, 6))
        self._refresh_proxy_text()

        # 操作
        action = ttk.Frame(self)
        action.pack(fill="x", **pad)
        self.go_btn = ttk.Button(action, text="开始推送", command=self.start_publish)
        self.go_btn.pack(side="left")
        ttk.Button(action, text="修订整合包…", command=self.open_patch_window).pack(side="left", padx=(8, 0))
        self.progress = ttk.Progressbar(action, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=12)

        # 日志
        logbox = ttk.LabelFrame(self, text="运行日志")
        logbox.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(logbox, height=18, wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)
        self.log_text.configure(state="disabled")

        ttk.Label(self, text="配置文件：config.json（含令牌与密码，请勿外传）", foreground="#888").pack(anchor="w", padx=12, pady=(0, 6))

    def _refresh_proxy_text(self):
        proxies = self.cfg.get("proxies") or []
        self.proxy_text.set(f"公共中转（由 GitHub 派生，{len(proxies)} 个）")

    def _on_mode_change(self):
        """在「整合包 / 存档」之间切换时，显示对应字段并调整提示。"""
        if self.mode_var.get() == "save":
            self.savebox.pack(fill="x", padx=8, pady=4, before=self.filebox)
            self.filebox.configure(text="3. 选择存档 zip（压缩包内应只有一个世界文件夹）")
            self.java_box.configure(state="disabled")
            self._refresh_save_packs()
        else:
            self.savebox.pack_forget()
            self.filebox.configure(text="3. 选择整合包文件")
            self.java_box.configure(state="readonly")

    # ---------------- 整合包修订（删减模组）

    def open_patch_window(self):
        """打开「修订整合包」窗口：勾掉要删除的文件，登记为一次增量修订。"""
        if not getattr(self, "admin_token", ""):
            messagebox.showwarning("需要登录", "请先登录中控并拉取服务器列表")
            return
        if not getattr(self, "servers", None):
            messagebox.showwarning("没有服务器", "服务器列表为空，请重新登录")
            return

        win = tk.Toplevel(self)
        win.title("修订整合包（删减模组）")
        win.geometry("920x700")
        win.transient(self)

        state = {"packs": [], "zip": "", "format": "", "entries": [],
                 "remove": set(), "add": [], "iid_map": {}}
        temp_root = {"dir": ""}
        server_labels = [f"{'【在档】' if s.get('archived') else '【在营】'}{s.get('name')}（{s.get('id')}）"
                         for s in self.servers]

        box = ttk.Frame(win, padding=10)
        box.pack(fill="both", expand=True)
        box.columnconfigure(1, weight=1)
        box.rowconfigure(6, weight=1)

        ttk.Label(box, text="服务器").grid(row=0, column=0, sticky="w")
        server_box = ttk.Combobox(box, state="readonly", values=server_labels)
        server_box.grid(row=0, column=1, columnspan=3, sticky="ew", pady=3)

        ttk.Label(box, text="整合包").grid(row=1, column=0, sticky="w")
        pack_box = ttk.Combobox(box, state="readonly")
        pack_box.grid(row=1, column=1, columnspan=3, sticky="ew", pady=3)

        info_var = tk.StringVar(value="")
        ttk.Label(box, textvariable=info_var, foreground="#666").grid(row=2, column=1, columnspan=3, sticky="w")

        ttk.Label(box, text="整合包文件").grid(row=3, column=0, sticky="w")
        zip_var = tk.StringVar()
        ttk.Entry(box, textvariable=zip_var, state="readonly").grid(row=3, column=1, sticky="ew", pady=3)

        ttk.Label(box, text="过滤前缀").grid(row=4, column=0, sticky="w")
        filter_var = tk.StringVar(value="mods/")
        ttk.Entry(box, textvariable=filter_var, width=26).grid(row=4, column=1, sticky="w", pady=3)
        ttk.Label(box, text="逗号分隔可写多个（如 mods/,config/），留空显示全部",
                  foreground="#888").grid(row=4, column=2, columnspan=2, sticky="w")

        list_wrap = ttk.Frame(box)
        list_wrap.grid(row=6, column=0, columnspan=4, sticky="nsew", pady=4)
        tree = ttk.Treeview(list_wrap, columns=("size", "action"), show="tree headings", height=16)
        tree.heading("#0", text="文件 / 清单条目", anchor="w")
        tree.heading("size", text="大小", anchor="e")
        tree.heading("action", text="操作", anchor="center")
        tree.column("#0", width=560, anchor="w")
        tree.column("size", width=90, anchor="e", stretch=False)
        tree.column("action", width=50, anchor="center", stretch=False)
        scroll = ttk.Scrollbar(list_wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        ttk.Label(box, text="修订说明").grid(row=7, column=0, sticky="w")
        note_var = tk.StringVar()
        ttk.Entry(box, textvariable=note_var).grid(row=7, column=1, columnspan=3, sticky="ew", pady=3)

        status_var = tk.StringVar(value="先选整合包与本地 zip，再勾选要删除的文件")
        ttk.Label(box, textvariable=status_var, foreground="#666").grid(row=9, column=0, columnspan=4, sticky="w")

        def human(size):
            size = int(size or 0)
            if size >= 1048576:
                return "%.1f MB" % (size / 1048576)
            if size >= 1024:
                return "%.0f KB" % (size / 1024)
            return "%d B" % size

        def render_tree(*_args):
            tree.delete(*tree.get_children())
            state["iid_map"] = {}
            prefixes = [p.strip().replace("\\", "/") for p in filter_var.get().split(",") if p.strip()]
            shown = 0
            for entry in state["entries"]:
                if entry["path"] in state["remove"]:
                    continue
                if prefixes and not any(entry["path"].startswith(p) for p in prefixes):
                    continue
                iid = tree.insert("", "end", text=entry["label"],
                                  values=(human(entry.get("size")), "×"))
                state["iid_map"][iid] = entry
                shown += 1
            status_var.set("共 %d 项（显示 %d，已标记删除 %d，待添加 %d）" % (
                len(state["entries"]), shown, len(state["remove"]), len(state["add"])))

        def load_files(path):
            try:
                fmt, entries = read_entries(path)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("读取失败", "无法读取整合包：%s" % exc, parent=win)
                return
            state.update({"zip": path, "format": fmt, "entries": entries,
                          "remove": set(), "add": [], "iid_map": {}})
            zip_var.set(path)
            names = {"modrinth": "Modrinth 清单包", "curseforge": "CurseForge 清单包（只读）",
                     "offline": "离线包（直接改文件）"}
            info_var.set("包格式：%s　共 %d 项" % (names.get(fmt, fmt), len(entries)))
            render_tree()

        def on_tree_click(event):
            if tree.identify("region", event.x, event.y) != "cell":
                return
            if tree.identify_column(event.x) != "#2":
                return
            entry = state.get("iid_map", {}).get(tree.identify_row(event.y))
            if not entry:
                return
            if state["format"] == "curseforge":
                messagebox.showinfo("暂不支持", "CurseForge 清单只有 projectID/fileID，无法按路径安全删除", parent=win)
                return
            state["remove"].add(entry["path"])
            render_tree()

        def cleanup_temp():
            path = temp_root.get("dir")
            if path and os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            temp_root["dir"] = ""

        def pick_local():
            path = filedialog.askopenfilename(
                title="选择本地整合包 zip（兜底方式）",
                filetypes=[("整合包", "*.zip *.mrpack"), ("所有文件", "*.*")])
            if path:
                load_files(path)

        def download_pack():
            """与启动器一致：多源测速 → 下载到临时目录 → 列出文件，关窗即删。"""
            idx = pack_box.current()
            if idx < 0 or idx >= len(state["packs"]):
                messagebox.showwarning("未选择", "请先选择整合包", parent=win)
                return
            pack = state["packs"][idx]
            urls = []
            for source in (pack.get("sources") or []):
                url = str(source.get("url") if isinstance(source, dict) else source or "")
                if url.startswith("http"):
                    urls.append(url)
            if not urls:
                messagebox.showwarning("没有下载源", "该整合包没有可用的下载地址", parent=win)
                return

            status_var.set("正在测速选择下载源…")

            def worker():
                best, best_ms = "", None
                for url in urls:
                    started = time.time()
                    try:
                        head_size(url, timeout=8)
                        ms = (time.time() - started) * 1000
                        if best_ms is None or ms < best_ms:
                            best, best_ms = url, ms
                    except Exception:  # noqa: BLE001
                        continue
                if not best:
                    self.after(0, lambda: status_var.set("所有下载源都不可用，可改用「本地 zip…」"))
                    return
                self.after(0, lambda ms=best_ms: status_var.set("已选择最快源（%.0f ms），开始下载…" % ms))
                cleanup_temp()
                target_dir = tempfile.mkdtemp(prefix="njitmcl_patch_")
                temp_root["dir"] = target_dir
                target = os.path.join(target_dir, pack.get("file") or "pack.zip")
                try:
                    req = urllib.request.Request(best, headers={"User-Agent": "NJITMCL-Publisher"})
                    context = ssl.create_default_context()
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                    with urllib.request.urlopen(req, timeout=60, context=context) as resp, open(target, "wb") as out:
                        total = int(resp.headers.get("Content-Length") or 0)
                        done = 0
                        while True:
                            chunk = resp.read(256 * 1024)
                            if not chunk:
                                break
                            out.write(chunk)
                            done += len(chunk)
                            if total:
                                pct = done * 100 // total
                                self.after(0, lambda p=pct, d=done, t=total: status_var.set(
                                    "正在下载整合包… %d%%（%.1f / %.1f MB）" % (p, d / 1048576, t / 1048576)))
                    self.after(0, lambda f=target: load_files(f))
                except Exception as exc:  # noqa: BLE001
                    self.after(0, lambda e=exc: status_var.set("下载失败：%s" % e))

            threading.Thread(target=worker, daemon=True).start()

        btn_wrap = ttk.Frame(box)
        btn_wrap.grid(row=3, column=2, columnspan=2, sticky="w", padx=(6, 0))
        ttk.Button(btn_wrap, text="拉取线上包", command=download_pack).pack(side="left")
        ttk.Button(btn_wrap, text="本地 zip…", command=pick_local).pack(side="left", padx=(6, 0))

        # 关窗时清掉临时下载的整合包
        win.protocol("WM_DELETE_WINDOW", lambda: (cleanup_temp(), win.destroy()))

        def on_pack_select(*_args):
            idx = pack_box.current()
            if idx < 0 or idx >= len(state["packs"]):
                info_var.set("")
                return
            pack = state["packs"][idx]
            rev = pack.get("revision") or 1
            patches = pack.get("patches") or []
            info_var.set("当前 revision %s，历史修订 %d 次，包文件 %s"
                         % (rev, len(patches), pack.get("file") or "-"))

        def load_packs(*_args):
            idx = server_box.current()
            if idx < 0:
                return
            state["packs"] = list(self.servers[idx].get("modpacks") or [])
            labels = ["%s（%s）v%s" % (p.get("name") or p.get("id"), p.get("id"), p.get("version") or "?")
                      for p in state["packs"]]
            pack_box["values"] = labels
            if labels:
                pack_box.current(0)
                on_pack_select()
            else:
                info_var.set("该服务器还没有登记整合包")

        def add_local_jar():
            if not state["zip"]:
                messagebox.showwarning("未选择整合包", "请先拉取或选择整合包", parent=win)
                return
            if state["format"] == "curseforge":
                messagebox.showinfo("暂不支持", "CurseForge 包暂不支持添加模组", parent=win)
                return
            path = filedialog.askopenfilename(title="选择要加入的模组 jar",
                                              filetypes=[("模组 jar", "*.jar"), ("所有文件", "*.*")])
            if not path:
                return
            state["add"].append(("mods/" + os.path.basename(path), path))
            render_tree()

        def add_modrinth():
            if not state["zip"]:
                messagebox.showwarning("未选择整合包", "请先拉取或选择整合包", parent=win)
                return
            if state["format"] == "curseforge":
                messagebox.showinfo("暂不支持", "CurseForge 包暂不支持添加模组", parent=win)
                return
            slug = simpledialog.askstring("从 Modrinth 添加", "输入 Modrinth 项目 slug（如 sodium）", parent=win)
            if not slug:
                return
            status_var.set("正在从 Modrinth 获取 %s …" % slug)

            def worker():
                try:
                    entry, version, filename = fetch_modrinth_version(slug.strip())
                    # 放进本窗口的临时根目录，关窗时随 cleanup_temp 一起删除
                    if not temp_root.get("dir"):
                        temp_root["dir"] = tempfile.mkdtemp(prefix="njitmcl_patch_")
                    tmp = tempfile.mkdtemp(prefix="njitmcl_mod_", dir=temp_root["dir"])
                    target = os.path.join(tmp, filename)
                    urllib.request.urlretrieve(entry["downloads"][0], target)
                    state["add"].append(("mods/" + filename, target))
                    self.after(0, lambda: (status_var.set("已添加 %s（%s）" % (filename, version)), render_tree()))
                except Exception as exc:  # noqa: BLE001
                    self.after(0, lambda e=exc: status_var.set("添加失败：%s" % e))

            threading.Thread(target=worker, daemon=True).start()

        def build_pack():
            if not state["zip"]:
                messagebox.showwarning("未选择整合包", "请先拉取或选择整合包", parent=win)
                return
            if not state["remove"] and not state["add"]:
                messagebox.showinfo("没有改动", "还没有删除或添加任何文件", parent=win)
                return
            out = filedialog.asksaveasfilename(title="保存修改后的整合包", defaultextension=".zip",
                                               initialfile=os.path.basename(state["zip"]))
            if not out:
                return
            try:
                result = build_modified_pack(state["zip"], out, state["remove"], state["add"])
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("生成失败", str(exc), parent=win)
                return
            changed = len(result["removed"]) + len(result["replaced"])
            status_var.set("已生成 %s（删除 %d，新增 %d）" % (out, changed, len(result["added"])))
            self.log("整合包已生成：%s，删除 %d，新增 %d" % (out, changed, len(result["added"])))

        def publish_revision():
            """生成新包 → 覆盖内网/GitHub → 更新中控哈希 → 登记删除补丁。"""
            pidx, sidx = pack_box.current(), server_box.current()
            if pidx < 0 or sidx < 0 or not state["zip"]:
                messagebox.showwarning("未就绪", "请先选择服务器、整合包并拉取线上包", parent=win)
                return
            if not state["remove"] and not state["add"]:
                messagebox.showinfo("没有改动", "还没有删除或添加任何文件", parent=win)
                return
            if not messagebox.askyesno(
                    "确认发布",
                    "将生成新的整合包并覆盖线上同名文件。\n"
                    "中控里的 sha256 会更新，已安装旧版的用户会收到强制更新提示。继续？", parent=win):
                return
            pack = state["packs"][pidx]
            server = self.servers[sidx]
            # 后台线程不再触碰 Tk 变量：发布开始时做一次快照
            snapshot = {"remove": list(state["remove"]), "add": list(state["add"]),
                        "note": note_var.get().strip()}
            rev_btn.configure(state="disabled")
            status_var.set("正在生成新包…")

            def worker():
                work = None
                try:
                    work = tempfile.mkdtemp(prefix="njitmcl_pub_")
                    out = os.path.join(work, pack.get("file") or "pack.zip")
                    build_modified_pack(state["zip"], out, snapshot["remove"], snapshot["add"])
                    digest = file_sha256(out)
                    size = os.path.getsize(out)
                    asset_name = os.path.basename(out)
                    job = {
                        "server": server, "path": out,
                        "pack_id": pack.get("id"), "name": pack.get("name"),
                        "version": pack.get("version"), "loader": pack.get("loader", ""),
                        "loader_version": pack.get("loaderVersion", ""),
                        "format": pack.get("format", ""), "default": pack.get("default", False),
                        "size": size, "force": True,
                    }
                    self.after(0, lambda: status_var.set("正在覆盖内网文件…"))
                    self._publish_wg(job, asset_name, digest, local_path=out)
                    self.after(0, lambda: status_var.set("正在覆盖 GitHub 资产…"))
                    try:
                        self._publish_github(job, asset_name, digest, force=True, local_path=out)
                    except Exception as exc:  # noqa: BLE001
                        # 必须中止登记：否则 sources 指向旧资产、中控哈希却是新包，玩家校验必然失败
                        raise RuntimeError("GitHub 覆盖失败，已中止登记（内网文件已更新）：%s" % exc)
                    # 新增的模组作为补丁资产上传到同一个 Release，并补齐多源（内网 + GitHub + 中转）
                    adds = []
                    target_rev = (pack.get("revision") or 1) + 1
                    proxies = [str(p).strip() for p in (self.cfg.get("proxies") or []) if str(p).strip()][:3]
                    if snapshot["add"]:
                        tag = "%s-%s" % (server.get("id"), pack.get("version"))
                        owner, repo = self.cfg.get("githubOwner"), self.cfg.get("githubRepo")
                        token = self.cfg.get("githubToken") or ""
                        status, release = github_api(
                            "GET", "https://api.github.com/repos/%s/%s/releases/tags/%s" % (owner, repo, tag), token)
                        if status != 200 or not isinstance(release, dict) or not release.get("id"):
                            raise RuntimeError("GitHub 上找不到 Release %s，无法上传补丁文件" % tag)
                        existing = {a.get("name"): a.get("id") for a in (release.get("assets") or [])}
                        for target_path, local in snapshot["add"]:
                            asset = "patch-r%d-%s" % (target_rev, os.path.basename(local))
                            info = github_upload_asset_safe(owner, repo, token, release["id"], asset, local,
                                                            force=asset in existing)
                            add_digest = file_sha256(local)
                            add_size = os.path.getsize(local)
                            add_url = info.get("browser_download_url") or ""
                            add_sources = []
                            try:
                                wg_url = self._publish_wg(job, asset, add_digest, local_path=local)
                                add_sources.append({"id": "wg", "label": "校内组网", "url": wg_url})
                            except Exception as exc:  # noqa: BLE001
                                self.log("补丁内网镜像上传失败（将只登记 GitHub 与中转）：%s" % exc)
                            if add_url:
                                add_sources.append({"id": "github", "label": "GitHub", "url": add_url})
                                for index, prefix in enumerate(proxies, start=1):
                                    if not prefix.endswith("/"):
                                        prefix += "/"
                                    add_sources.append({"id": "proxy%d" % index,
                                                        "label": "中转 %d" % index,
                                                        "url": prefix + add_url})
                            adds.append({"path": target_path, "url": add_url,
                                         "sha256": add_digest, "size": add_size,
                                         "sources": add_sources})
                    self.after(0, lambda: status_var.set("正在更新中控登记…"))
                    payload = {
                        "serverId": server.get("id"),
                        "force": True,
                        "pack": {
                            "id": pack.get("id"), "name": pack.get("name"),
                            "version": pack.get("version"), "loader": pack.get("loader", ""),
                            "loaderVersion": pack.get("loaderVersion", ""),
                            "format": pack.get("format", ""), "file": asset_name,
                            "size": size, "sha256": digest,
                            "default": bool(pack.get("default")),
                            "sources": pack.get("sources") or [],
                            # 文件清单：客户端据此自动识别本地实例属于哪个 revision
                            "manifest": read_manifest(out),
                        },
                    }
                    code, data = http_json("POST", self.cfg["apiBase"].rstrip("/") + "/api/admin/launcher/packs",
                                           token=self.admin_token, payload=payload)
                    if code != 200 or not data.get("success"):
                        raise RuntimeError("更新整合包登记失败（%s）：%s" % (code, data))

                    # 删除清单转成实例内相对路径（overrides/ 前缀要去掉）
                    deletes = []
                    for path in snapshot["remove"]:
                        rel = path
                        for prefix in ("overrides/", "client-overrides/", "server-overrides/"):
                            if rel.startswith(prefix):
                                rel = rel[len(prefix):]
                                break
                        # 根目录文件（options.txt、servers.dat 等）也必须登记，否则老实例删不掉；
                        # 只排除启动器自己的标记目录
                        if rel and not rel.startswith("PCL/"):
                            deletes.append(rel)
                    if deletes or adds:
                        patch_body = {"packId": pack.get("id"), "delete": deletes,
                                      "note": snapshot["note"]}
                        if adds:
                            patch_body["add"] = adds
                        code, data = http_json("POST", self.cfg["apiBase"].rstrip("/") + "/api/admin/launcher/packs",
                                               token=self.admin_token,
                                               payload={"serverId": server.get("id"), "patch": patch_body})
                        if code != 200 or not data.get("success"):
                            raise RuntimeError("登记补丁失败（%s）：%s" % (code, data))
                        revision = data.get("revision")
                    else:
                        revision = pack.get("revision") or 1

                    # 自检：整包哈希变了但修订号没有前进时，补一条空补丁，
                    # 否则已安装旧版的客户端会因缺少 from=<本地修订> 的补丁而报「没有可用的增量补丁」
                    if revision == (pack.get("revision") or 1) and \
                            digest != (pack.get("sha256") or "").lower():
                        patch_body = {"packId": pack.get("id"), "delete": [], "add": [],
                                      "note": snapshot["note"] or "整包重新发布（无文件变更）"}
                        code, data = http_json("POST", self.cfg["apiBase"].rstrip("/") + "/api/admin/launcher/packs",
                                               token=self.admin_token,
                                               payload={"serverId": server.get("id"), "patch": patch_body})
                        if code == 200 and data.get("success"):
                            revision = data.get("revision")
                            self.log("整包哈希已变化，已补登记空补丁，revision=%s" % revision)
                        else:
                            # 不能只记日志：整包已覆盖而无补丁，所有玩家都会卡在「没有可用的增量补丁」
                            raise RuntimeError("整包哈希已变化但补丁登记失败（%s）：%s" % (code, data))

                    def done():
                        status_var.set("已发布：sha256 %s…，revision %s，删除 %d 项"
                                       % (digest[:8], revision, len(deletes)))
                        self.log("整合包 %s 已覆盖发布，新 sha256=%s，revision=%s"
                                 % (pack.get("id"), digest, revision))
                        messagebox.showinfo("已发布",
                                            "新包已覆盖线上文件\nsha256：%s\nrevision：%s\n删除：%d 项"
                                            % (digest[:16], revision, len(deletes)), parent=win)
                    self.after(0, done)
                except Exception as exc:  # noqa: BLE001
                    self.after(0, lambda e=exc: status_var.set("发布失败：%s" % e))
                    self.log("修订发布失败：%s" % exc)
                finally:
                    self.after(0, lambda: rev_btn.configure(state="normal"))
                    if work:
                        shutil.rmtree(work, ignore_errors=True)

            threading.Thread(target=worker, daemon=True).start()

        btn_row = ttk.Frame(box)
        btn_row.grid(row=8, column=0, columnspan=4, sticky="w", pady=6)
        ttk.Button(btn_row, text="添加本地 jar", command=add_local_jar).pack(side="left")
        ttk.Button(btn_row, text="从 Modrinth 添加", command=add_modrinth).pack(side="left", padx=(6, 0))
        ttk.Button(btn_row, text="生成新包", command=build_pack).pack(side="left", padx=(18, 0))
        rev_btn = ttk.Button(btn_row, text="发布修订", command=publish_revision)
        rev_btn.pack(side="left", padx=(18, 0))

        tree.bind("<Button-1>", on_tree_click)
        filter_var.trace_add("write", render_tree)
        server_box.bind("<<ComboboxSelected>>", load_packs)
        pack_box.bind("<<ComboboxSelected>>", on_pack_select)
        if server_labels:
            server_box.current(0)
            load_packs()

    def _refresh_save_packs(self):
        """按所选服务器刷新「关联整合包」下拉项。"""
        index = self.server_box.current()
        if index < 0 or index >= len(self.servers):
            self.save_pack_box["values"] = []
            self.save_pack_box.set("")
            return
        server = self.servers[index]
        labels = []
        for pack in server.get("modpacks") or []:
            pack_id = str(pack.get("id") or "")
            if not pack_id:
                continue
            labels.append(f"{pack_id}（{pack.get('name', '')} {pack.get('version', '')}）")
        self.save_pack_box["values"] = labels
        if labels:
            self.save_pack_box.current(0)
        else:
            self.save_pack_box.set("")

    # ---------------- 日志与进度

    def log(self, text):
        self.log_queue.put(text)

    def _drain_log(self):
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.after(120, self._drain_log)

    # ---------------- 自更新

    def _check_publisher_update(self):
        threading.Thread(target=self._check_publisher_update_worker, daemon=True).start()

    def _check_publisher_update_worker(self):
        """启动时静默检查 GitHub 上的 publisher.json；失败不打扰用户。"""
        data = None
        for url in PUBLISHER_MANIFEST_URLS:
            try:
                request = urllib.request.Request(
                    url, headers={"User-Agent": "NJITMCL-Publisher/%s" % PUBLISHER_VERSION})
                with urllib.request.urlopen(request, timeout=8) as response:
                    data = json.loads(response.read().decode("utf-8"))
                break
            except Exception:  # noqa: BLE001
                continue
        if not isinstance(data, dict):
            return
        try:
            latest_code = int(data.get("versionCode") or 0)
        except (TypeError, ValueError):
            return
        if latest_code <= PUBLISHER_VERSION_CODE:
            self.log("发布工具 v%s 已是最新版本" % PUBLISHER_VERSION)
            return
        latest = str(data.get("version") or "?")
        note = str(data.get("note") or "").strip()
        self.log("发现发布工具新版本 v%s（当前 v%s）" % (latest, PUBLISHER_VERSION))

        def ask():
            message = "发现发布工具新版本 v%s（当前 v%s）" % (latest, PUBLISHER_VERSION)
            if note:
                message += "\n\n" + note
            message += "\n\n是否打开下载页？"
            if messagebox.askyesno("发布工具更新", message):
                webbrowser.open(PUBLISHER_DOWNLOAD_PAGE)

        self.after(0, ask)

    def set_progress(self, done, total):
        percent = 0 if not total else done * 100.0 / total
        self.after(0, lambda: self.progress.configure(value=percent))

    def _auto_login_if_possible(self):
        if not self.cfg.get("githubToken"):
            self.log("提示：尚未配置 GitHub Token，推送 GitHub 会失败。请编辑 config.json 填写 githubToken。")

    # ---------------- 登录

    def login(self):
        url = self.api_var.get().rstrip("/")
        password = self.pwd_var.get()
        totp = self.totp_var.get().strip()
        if not password or len(totp) != 6:
            messagebox.showwarning("缺少信息", "请填写管理员密码和 6 位动态验证码")
            return
        self.status_var.set("正在登录…")
        threading.Thread(target=self._login_worker, args=(url, password, totp), daemon=True).start()

    def _login_worker(self, url, password, totp):
        try:
            # 站点后台登录要求同源 + JSON，登录成功后令牌只在 Set-Cookie 里
            body = json.dumps({"method": "totp", "password": password, "totp": totp}).encode("utf-8")
            req = urllib.request.Request(url + "/api/manage/login", data=body, method="POST", headers={
                "Content-Type": "application/json",
                "Origin": url,
                "User-Agent": "NJITMCL-Publisher",
            })
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    cookies = resp.headers.get_all("Set-Cookie") or []
            except urllib.error.HTTPError as err:
                raw = err.read().decode("utf-8", "replace")
                raise RuntimeError(f"登录失败（{err.code}）：{raw[:200]}")
            token = ""
            for item in cookies:
                if item.startswith("admin_token="):
                    token = item.split(";", 1)[0].split("=", 1)[1]
                    break
            if not token or not data.get("success"):
                raise RuntimeError("登录未通过，请检查密码和动态验证码")
            self.admin_token = token
            self.cfg["apiBase"] = url
            save_config(self.cfg)
            code, data = http_json("GET", url + "/api/admin/launcher/packs", token=self.admin_token)
            if code != 200 or not data.get("success"):
                raise RuntimeError(f"拉取服务器失败（{code}）")
            # 在营的排前面，在档的排后面（同组内保持网站顺序）
            self.servers = sorted(data.get("servers", []), key=lambda item: bool(item.get("archived")))
            labels = [f"{'【在档】' if s.get('archived') else '【在营】'}{s['name']}（{s['id']}）"
                      for s in self.servers]
            self.after(0, lambda: self._fill_servers(labels))
            self.after(0, lambda: self.status_var.set(f"已登录，共 {len(self.servers)} 台服务器"))
            self.log(f"登录成功，服务器数量：{len(self.servers)}")
        except Exception as exc:  # noqa: BLE001
            self.after(0, lambda: self.status_var.set("登录失败"))
            self.log("登录失败：" + str(exc))

    def _fill_servers(self, labels):
        self.server_box["values"] = labels
        if labels:
            self.server_box.current(0)
        self._refresh_save_packs()

    # ---------------- 文件

    def pick_file(self):
        if self.mode_var.get() == "save":
            filetypes = [("存档压缩包", "*.zip"), ("所有文件", "*.*")]
            title = "选择存档 zip"
        else:
            filetypes = [("整合包", "*.zip *.mrpack"), ("所有文件", "*.*")]
            title = "选择整合包"
        path = filedialog.askopenfilename(title=title, filetypes=filetypes)
        if not path:
            return
        self.file_var.set(path)
        self.size_var.set(f"{os.path.getsize(path) / 1024 / 1024:.2f} MB")
        self.sha_var.set("计算中…")
        threading.Thread(target=self._hash_worker, args=(path,), daemon=True).start()
        if self.mode_var.get() == "save":
            threading.Thread(target=self._save_meta_worker, args=(path,), daemon=True).start()
        else:
            threading.Thread(target=self._meta_worker, args=(path,), daemon=True).start()

    def recalc(self):
        path = self.file_var.get()
        if not path or not os.path.exists(path):
            messagebox.showwarning("没有文件", "请先选择整合包文件")
            return
        self.sha_var.set("计算中…")
        threading.Thread(target=self._hash_worker, args=(path,), daemon=True).start()

    def _hash_worker(self, path):
        digest = sha256_of(path, progress=self.set_progress)
        self.after(0, lambda: self.sha_var.set(digest))
        self.after(0, lambda: self.progress.configure(value=0))
        self.log(f"已计算 SHA256：{digest}")

    def _meta_worker(self, path):
        """从原始整合包读取名称、版本与加载器，回到主线程填表。"""
        meta = {}
        try:
            meta = read_pack_meta(path)
        except Exception as exc:  # noqa: BLE001
            self.log("读取整合包信息失败：%s" % exc)
        self.after(0, lambda: self._apply_meta(meta))

    def _apply_meta(self, meta):
        if not meta:
            return
        if meta.get("name"):
            self.name_var.set(meta["name"])
        if meta.get("version"):
            self.version_var.set(meta["version"])
        if meta.get("loader"):
            self.loader_var.set(meta["loader"])
        if meta.get("loader_version"):
            self.loader_ver_var.set(meta["loader_version"])
        if meta.get("format"):
            self.format_var.set(meta["format"])
        labels = (("name", "名称"), ("version", "版本"), ("loader", "加载器"),
                  ("loader_version", "加载器版本"), ("mc_version", "MC 版本"))
        detail = "、".join("%s=%s" % (label, meta.get(key))
                           for key, label in labels if meta.get(key))
        if detail:
            self.log("已从原始包识别：" + detail)
        else:
            self.log("原始包未包含可识别的元数据，请手动填写名称、版本与加载器")

    def _save_meta_worker(self, path):
        """存档包：用压缩包内第一层目录名猜存档名称与标识。"""
        try:
            with zipfile.ZipFile(path) as zf:
                dirs = sorted({name.split("/")[0] for name in zf.namelist() if "/" in name})
            if not dirs:
                return
            name = dirs[0]
            ref = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32] or "save1"
            self.after(0, lambda: self.save_name_var.set(name))
            self.after(0, lambda: self.save_id_var.set(ref))
            self.log("已从存档包识别：%s（标识 %s）" % (name, ref))
        except Exception as exc:  # noqa: BLE001
            self.log("读取存档包信息失败：%s" % exc)

    def _java_choice(self):
        """返回选中的 Java 大版本；「自动（不指定）」为 0。"""
        raw = self.java_var.get().strip()
        return int(raw) if raw.isdigit() else 0

    # ---------------- 推送

    def start_publish(self):
        if not self.admin_token:
            messagebox.showwarning("未登录", "请先登录中控")
            return
        if self.mode_var.get() == "save":
            self._start_save_publish()
            return
        path = self.file_var.get()
        if not path or not os.path.exists(path):
            messagebox.showwarning("没有文件", "请先选择整合包文件")
            return
        index = self.server_box.current()
        if index < 0 or index >= len(self.servers):
            messagebox.showwarning("没有服务器", "请选择目标服务器")
            return
        if not (self.use_wg.get() or self.use_gh.get() or self.use_proxy.get()):
            messagebox.showwarning("没有目标", "至少勾选一个上传目标")
            return
        server = self.servers[index]
        pack_version = self.version_var.get().strip()
        if not pack_version:
            messagebox.showwarning("缺少版本", "请填写版本号")
            return
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", pack_version):
            messagebox.showwarning("版本号不合法", "版本号只能包含字母、数字、点、下划线、短横线（最长 40 位）")
            return
        pack_id = f"{server['id']}-{pack_version}".lower()
        name = self.name_var.get().strip() or f"{server['name']} 整合包"
        size = os.path.getsize(path)
        if self.use_proxy.get() and not self.use_gh.get():
            messagebox.showwarning("依赖 GitHub", "公共中转是从 GitHub 地址派生的，必须同时勾选 GitHub Release")
            return
        if self.use_proxy.get() and not (self.cfg.get("proxies") or []):
            messagebox.showwarning("没有中转", "config.json 里的 proxies 为空，请先填写中转前缀")
            return
        self.go_btn.configure(state="disabled")
        job = {
            "server": server,
            "path": path,
            "pack_id": pack_id,
            "name": name,
            "version": pack_version,
            "size": size,
            "loader": self.loader_var.get().strip(),
            "loader_version": self.loader_ver_var.get().strip(),
            "format": self.format_var.get().strip(),
            "default": self.default_var.get(),
            "enabled_web": self.enabled_var.get(),
            "java_version": self._java_choice(),
            "old_sha": ((next((p for p in server.get("modpacks") or []
                               if p.get("id") == pack_id), {}) or {}).get("sha256") or "").lower(),
            "use_wg": self.use_wg.get(),
            "use_gh": self.use_gh.get(),
            "use_proxy": self.use_proxy.get(),
            "force": self.force_var.get(),
        }
        threading.Thread(target=self._publish_worker, args=(job,), daemon=True).start()

    def _publish_worker(self, job):
        try:
            self.log("=" * 60)
            self.log(f"开始推送：{job['name']} {job['version']}（{job['size']} 字节）")
            state = "在档" if job["server"].get("archived") else "在营"
            self.log(f"目标服务器：{job['server']['name']}（{job['server']['id']}，{state}）")
            # 始终按当前文件现算，避免沿用界面上其他文件留下的哈希
            digest = sha256_of(job["path"], progress=self.set_progress)
            self.after(0, lambda: self.sha_var.set(digest))
            asset_name = f"{job['pack_id']}.zip"
            self._check_duplicate(job, digest)
            sources = []

            if job["use_wg"]:
                url = self._publish_wg(job, asset_name, digest)
                sources.append({"id": "wg", "label": "校内组网", "url": url})

            github_url = ""
            if job["use_gh"]:
                # 整合包统一走 -Modpacks 仓库；启动器由单独的九步流程发布
                github_url = self._publish_github(job, asset_name, digest)
                sources.append({"id": "github", "label": "GitHub", "url": github_url})

            if job["use_proxy"] and github_url:
                # 公共中转不需要另传文件，只是给 GitHub 地址套一层国内可用的前缀
                # 中控限制每个整合包最多 6 个源，这里按剩余额度截断
                room = max(0, 6 - len(sources))
                proxies = [str(p).strip() for p in (self.cfg.get("proxies") or []) if str(p).strip()]
                if len(proxies) > room:
                    self.log(f"中转数量超过中控上限，只登记前 {room} 个（可在 config.json 调整顺序）")
                    proxies = proxies[:room]
                for index, prefix in enumerate(proxies, start=1):
                    if not prefix.endswith("/"):
                        prefix += "/"
                    sources.append({
                        "id": "proxy%d" % index,
                        "label": "中转 %d" % index,
                        "url": prefix + github_url,
                    })
                self.log(f"已登记 {len(proxies)} 个公共中转源")

            payload = {
                "serverId": job["server"]["id"],
                "force": bool(job.get("force")),
                "pack": {
                    "id": job["pack_id"],
                    "name": job["name"],
                    "version": job["version"],
                    "loader": job["loader"],
                    "loaderVersion": job["loader_version"],
                    "format": job["format"],
                    "file": asset_name,
                    "size": job["size"],
                    "sha256": digest,
                    "default": job["default"],
                    "enabled": job.get("enabled_web", True),
                    # 0 表示不指定，中控会原样透传给启动器
                    "javaVersion": int(job.get("java_version") or 0),
                    "sources": sources,
                    # 文件清单：客户端据此自动识别本地实例属于哪个 revision
                    "manifest": read_manifest(job["path"]) if job.get("path") else [],
                },
            }
            code, data = http_json("POST", self.cfg["apiBase"].rstrip("/") + "/api/admin/launcher/packs",
                                   token=self.admin_token, payload=payload)
            if code != 200 or not data.get("success"):
                raise RuntimeError(f"登记失败（{code}）：{data}")
            self.log("已登记到中控：" + job["pack_id"])

            # 整包哈希变了但主流程不推进 revision，补一条带说明的空补丁，
            # 否则已安装旧版的客户端会报「没有可用的增量补丁」
            if job.get("old_sha") and job["old_sha"] != digest:
                code, data = http_json("POST", self.cfg["apiBase"].rstrip("/") + "/api/admin/launcher/packs",
                                       token=self.admin_token,
                                       payload={"serverId": job["server"]["id"],
                                                "patch": {"packId": job["pack_id"], "delete": [], "add": [],
                                                          "note": "整包重新发布（无文件变更）"}})
                if code != 200 or not data.get("success"):
                    raise RuntimeError("整包哈希已变化但补丁登记失败（%s）：%s" % (code, data))
                self.log("已补登记空补丁，revision=%s" % data.get("revision"))

            failed = self._verify(sources, job["size"])
            if failed:
                self.log("校验未通过：" + "、".join(failed))
                self.after(0, lambda f=list(failed): messagebox.showwarning(
                    "已发布但有校验异常", "登记已完成，但以下源未通过校验：\n" + "\n".join(f)))
            else:
                self.log("全部完成 ✔ 可以去启动器或网站查看")
                self.after(0, lambda: messagebox.showinfo("完成", "整合包已发布并登记到中控"))
        except Exception as exc:  # noqa: BLE001
            self.log("失败：" + str(exc))
            self.after(0, lambda: messagebox.showerror("失败", str(exc)))
        finally:
            self.after(0, lambda: self.go_btn.configure(state="normal"))
            self.after(0, lambda: self.progress.configure(value=0))

    # ---------------- 存档推送

    def _start_save_publish(self):
        path = self.file_var.get()
        if not path or not os.path.exists(path):
            messagebox.showwarning("没有文件", "请先选择存档 zip")
            return
        index = self.server_box.current()
        if index < 0 or index >= len(self.servers):
            messagebox.showwarning("没有服务器", "请选择目标服务器")
            return
        save_name = self.save_name_var.get().strip()
        if not save_name:
            messagebox.showwarning("缺少名称", "请填写存档名称")
            return
        save_ref = self.save_id_var.get().strip().lower()
        if not re.match(r"^[a-z0-9][a-z0-9._-]{0,31}$", save_ref):
            messagebox.showwarning("标识不合法", "存档标识只能用小写字母、数字、点、下划线、短横线，且以字母或数字开头")
            return
        if not (self.use_wg.get() or self.use_gh.get() or self.use_proxy.get()):
            messagebox.showwarning("没有目标", "至少勾选一个上传目标")
            return
        if self.use_proxy.get() and not self.use_gh.get():
            messagebox.showwarning("依赖 GitHub", "公共中转是从 GitHub 地址派生的，必须同时勾选 GitHub Release")
            return
        if self.use_proxy.get() and not (self.cfg.get("proxies") or []):
            messagebox.showwarning("没有中转", "config.json 里的 proxies 为空，请先填写中转前缀")
            return
        pack_label = self.save_pack_box.get().strip()
        pack_id = pack_label.split("（", 1)[0].strip() if pack_label else ""
        if not pack_id:
            messagebox.showwarning("缺少关联", "请选择该存档关联的整合包")
            return
        server = self.servers[index]
        self.go_btn.configure(state="disabled")
        job = {
            "server": server,
            "path": path,
            "save_id": f"{server['id']}-{save_ref}",
            "save_name": save_name,
            "save_ref": save_ref,
            "pack_id": pack_id,
            "size": os.path.getsize(path),
            "use_wg": self.use_wg.get(),
            "use_gh": self.use_gh.get(),
            "use_proxy": self.use_proxy.get(),
        }
        threading.Thread(target=self._save_publish_worker, args=(job,), daemon=True).start()

    def _split_file(self, path, base_name):
        """按 coreutils split 命名把整包切成 .part01 起的等长分片，返回分片信息。"""
        folder = os.path.join(os.path.dirname(path), base_name + ".parts")
        os.makedirs(folder, exist_ok=True)
        parts = []
        index = 1
        with open(path, "rb") as source:
            while True:
                name = f"{base_name}.part{index:02d}"
                target = os.path.join(folder, name)
                written = 0
                with open(target, "wb") as output:
                    while written < SPLIT_LIMIT:
                        chunk = source.read(min(1024 * 1024, SPLIT_LIMIT - written))
                        if not chunk:
                            break
                        output.write(chunk)
                        written += len(chunk)
                if written == 0:
                    os.remove(target)
                    break
                digest = sha256_of(target)
                parts.append({"path": target, "name": name, "size": written, "sha256": digest})
                self.log(f"已切片 {name}（{written} 字节，sha256 {digest[:12]}…）")
                index += 1
        return parts

    def _save_publish_worker(self, job):
        try:
            self.log("=" * 60)
            self.log(f"开始推送存档：{job['save_name']}（{job['size']} 字节）")
            self.log(f"目标服务器：{job['server']['name']}（{job['server']['id']}）")
            digest = self.sha_var.get()
            if len(digest) != 64:
                self.log("SHA256 未计算完成，重新计算…")
                digest = sha256_of(job["path"], progress=self.set_progress)
                self.after(0, lambda: self.sha_var.set(digest))
            asset_name = f"{job['server']['id']}-{job['save_ref']}.zip"
            sources = []
            if job["use_wg"]:
                url = self._publish_wg(job, asset_name, digest, subdir="/saves")
                sources.append({"id": "wg", "label": "校内组网", "url": url})
            parts = []
            if job["size"] > SPLIT_LIMIT:
                # 超过 GitHub 单文件 2 GB 上限：整包留在内网，GitHub 收分片
                self.log(f"文件超过 {SPLIT_LIMIT / 1024 / 1024 / 1024:.1f} GB，按 split 规范切片")
                parts = self._split_file(job["path"], asset_name)
                if job["use_gh"]:
                    for part in parts:
                        part_url = self._publish_github(
                            job, part["name"], part["sha256"],
                            tag=f"saves-{job['server']['id']}",
                            title=f"{job['server']['name']} 存档",
                            force=True, local_path=part["path"],
                        )
                        part_sources = [{"id": "github", "label": "GitHub", "url": part_url}]
                        if job["use_proxy"]:
                            for index, prefix in enumerate((self.cfg.get("proxies") or [])[:5], start=1):
                                if not prefix.endswith("/"):
                                    prefix += "/"
                                part_sources.append({
                                    "id": "proxy%d" % index,
                                    "label": "中转 %d" % index,
                                    "url": prefix + part_url,
                                })
                        part["sources"] = part_sources
                    self.log(f"分片上传完成，共 {len(parts)} 片")
            else:
                github_url = ""
                if job["use_gh"]:
                    github_url = self._publish_github(
                        job, asset_name, digest,
                        tag=f"saves-{job['server']['id']}",
                        title=f"{job['server']['name']} 存档",
                        force=True,
                    )
                    sources.append({"id": "github", "label": "GitHub", "url": github_url})
                if job["use_proxy"] and github_url:
                    room = max(0, 6 - len(sources))
                    proxies = [str(p).strip() for p in (self.cfg.get("proxies") or []) if str(p).strip()]
                    if len(proxies) > room:
                        self.log(f"中转数量超过中控上限，只登记前 {room} 个")
                        proxies = proxies[:room]
                    for index, prefix in enumerate(proxies, start=1):
                        if not prefix.endswith("/"):
                            prefix += "/"
                        sources.append({
                            "id": "proxy%d" % index,
                            "label": "中转 %d" % index,
                            "url": prefix + github_url,
                        })
                    self.log(f"已登记 {len(proxies)} 个公共中转源")
            payload = {
                "serverId": job["server"]["id"],
                "save": {
                    "id": job["save_id"],
                    "name": job["save_name"],
                    "file": asset_name,
                    "size": job["size"],
                    "sha256": digest,
                    "packId": job["pack_id"],
                    "sources": sources,
                    "parts": [
                        {
                            "file": part["name"],
                            "size": part["size"],
                            "sha256": part["sha256"],
                            "sources": part["sources"],
                        }
                        for part in parts if part.get("sources")
                    ],
                },
            }
            code, data = http_json("POST", self.cfg["apiBase"].rstrip("/") + "/api/admin/launcher/packs",
                                   token=self.admin_token, payload=payload)
            if code != 200 or not data.get("success"):
                raise RuntimeError(f"登记失败（{code}）：{data}")
            self.log("已登记到中控：" + job["save_id"])
            self._verify(sources, job["size"])
            self.log("存档推送完成 ✔ 启动器「已结档」卡片会出现下载存档入口")
            self.after(0, lambda: messagebox.showinfo("完成", "存档已发布并登记到中控"))
        except Exception as exc:  # noqa: BLE001
            self.log("失败：" + str(exc))
            self.after(0, lambda: messagebox.showerror("失败", str(exc)))
        finally:
            self.after(0, lambda: self.go_btn.configure(state="normal"))
            self.after(0, lambda: self.progress.configure(value=0))

    # ---------------- 重复推送检查

    def _check_duplicate(self, job, digest):
        """同名版本已存在时：文件一致就提示无需推送，不一致就要求改版本号或强制覆盖。"""
        code, data = http_json("GET", self.cfg["apiBase"] + "/api/admin/launcher/packs", token=self.admin_token)
        if code != 200 or not data.get("success"):
            raise RuntimeError(f"读取已登记整合包失败（{code}）：{data}")
        server = next((s for s in data.get("servers", []) if s.get("id") == job["server"]["id"]), None)
        existing = next((p for p in (server or {}).get("modpacks", []) if str(p.get("id")) == job["pack_id"]), None)
        if not existing:
            return None
        same = (str(existing.get("sha256", "")).lower() == digest
                and int(existing.get("size") or 0) == job["size"])
        if same:
            raise RuntimeError(f"版本 {job['pack_id']} 已推送过，且文件完全一致，无需重复推送")
        if not job.get("force"):
            raise RuntimeError(f"版本 {job['pack_id']} 已存在且文件不一致。"
                               f"请把版本号改成新的（例如提升补丁位），或勾选“允许覆盖同名版本”")
        self.log(f"同名版本 {job['pack_id']} 已存在且文件不一致，按勾选执行覆盖")
        return existing

    # ---------------- 各上传目标

    def _publish_wg(self, job, asset_name, digest="", subdir="", local_path=None):
        local = local_path or job["path"]
        if paramiko is None:
            raise RuntimeError("缺少 paramiko，请先执行 pip install paramiko")
        cfg = self.cfg
        self.log(f"连接内网服务器 {cfg['wgHost']} …")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        key_file = (cfg.get("wgKeyFile") or "").strip()
        password = cfg.get("wgPassword") or ""
        connected = False
        if key_file and os.path.exists(key_file):
            try:
                client.connect(cfg["wgHost"], username=cfg["wgUser"], key_filename=key_file, timeout=20)
                self.log("已用密钥登录内网服务器")
                connected = True
            except paramiko.AuthenticationException:
                self.log("密钥登录被拒绝，改用密码登录")
            except Exception as exc:  # noqa: BLE001
                self.log(f"密钥登录失败（{exc}），改用密码登录")
        if not connected:
            if not password:
                raise RuntimeError("内网服务器没有可用凭据：请填写 wgPassword，或把 wgKeyFile 指向该服务器的私钥")
            client.connect(cfg["wgHost"], username=cfg["wgUser"], password=password, timeout=20)
            self.log("已用密码登录内网服务器")
        try:
            remote_dir = f"{cfg['wgRemoteDir'].rstrip('/')}/{job['server']['id']}{subdir}"
            url_path = f"/modpacks/{job['server']['id']}{subdir}/{urllib.parse.quote(asset_name)}"
            client.exec_command(f"mkdir -p {remote_dir}")[1].channel.recv_exit_status()
            remote_path = f"{remote_dir}/{asset_name}"
            sftp = client.open_sftp()
            try:
                stat = sftp.stat(remote_path)
                same = stat.st_size == os.path.getsize(local)
            except IOError:
                same = False
            if same and digest:
                # 重试时避免重复上传几百 MB：远端哈希一致就跳过
                out = client.exec_command(f"sha256sum '{remote_path}'")[1].read().decode("utf-8", "replace")
                remote_hash = out.split(" ")[0].strip().lower() if out.strip() else ""
                if remote_hash == digest.lower():
                    self.log("内网服务器上已有相同文件，跳过上传")
                    sftp.close()
                    return "https://wg.njit-fkcy.top" + url_path
            self.log(f"上传到 {remote_path} …")
            sftp.put(local, remote_path, callback=lambda done, total: self.set_progress(done, total))
            sftp.close()
            self.log("内网服务器上传完成")
        finally:
            client.close()
        return "https://wg.njit-fkcy.top" + url_path

    def _ensure_github_repo(self, owner, repo, token):
        """确保仓库存在且不是空仓库——空仓库不能创建 Release。"""
        code, _ = github_api("GET", f"https://api.github.com/repos/{owner}/{repo}", token)
        if code != 200:
            self.log(f"GitHub 仓库 {owner}/{repo} 不存在，正在创建（公开）…")
            code, data = github_api("POST", "https://api.github.com/user/repos", token,
                                    {"name": repo, "private": False, "auto_init": True,
                                     "description": "NJIT MCL 社团整合包发行文件"})
            if code not in (200, 201):
                raise RuntimeError(f"创建 GitHub 仓库失败（{code}）：{data}")
            self.log("GitHub 仓库已创建")
        for attempt in range(10):
            if self._ensure_repo_has_commit(owner, repo, token):
                return
            time.sleep(1)
        raise RuntimeError("GitHub 仓库仍为空，无法创建 Release，请稍后重试")

    def _ensure_repo_has_commit(self, owner, repo, token):
        """空仓库时写一个 README 制造初始提交，返回仓库是否已有内容。"""
        code, _ = github_api("GET", f"https://api.github.com/repos/{owner}/{repo}/contents/README.md", token)
        if code == 200:
            return True
        if code != 404:
            return False
        import base64
        content = base64.b64encode("# NJIT MCL 社团整合包发行文件\n\n本仓库用于存放社团在档服务器的整合包清单与发行文件。\n".encode("utf-8")).decode()
        code, data = github_api("PUT", f"https://api.github.com/repos/{owner}/{repo}/contents/README.md", token,
                                {"message": "初始化仓库", "content": content})
        if code in (200, 201):
            self.log("已为 GitHub 空仓库创建初始提交")
            time.sleep(2)
            return True
        self.log(f"写入初始提交失败（{code}）：{data}")
        return False

    def _publish_github(self, job, asset_name, digest, tag=None, title=None, force=None, local_path=None, repo=None):
        local = local_path or job["path"]
        token = self.cfg.get("githubToken", "").strip()
        if not token:
            raise RuntimeError("未配置 GitHub Token（config.json → githubToken）")
        owner, repo = self.cfg["githubOwner"], repo or self.cfg["githubRepo"]
        self._ensure_github_repo(owner, repo, token)
        tag = tag or job["pack_id"]
        title = title or f"{job['name']} {job['version']}"
        use_force = job.get("force") if force is None else force
        code, release = github_api("GET", f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}", token)
        if code == 404:
            self.log(f"创建 GitHub Release：{tag}")
            for attempt in range(3):
                code, release = github_api("POST", f"https://api.github.com/repos/{owner}/{repo}/releases", token, {
                    "tag_name": tag,
                    "name": title,
                    "body": f"服务器：{job['server']['name']}（{job['server']['id']}）\nSHA256：{digest}",
                    "draft": False,
                    "prerelease": False,
                })
                if code in (200, 201):
                    break
                message = str(release)
                if "Repository is empty" in message and attempt < 2:
                    self.log("GitHub 提示仓库为空，等待后重试…")
                    self._ensure_repo_has_commit(owner, repo, token)
                    time.sleep(2)
                    continue
                raise RuntimeError(f"创建 Release 失败（{code}）：{release}")
        elif code != 200:
            raise RuntimeError(f"读取 Release 失败（{code}）：{release}")
        existing = {item["name"]: item for item in release.get("assets", [])}
        if asset_name in existing and not use_force:
            remote_size = existing[asset_name].get("size")
            local_size = os.path.getsize(local)
            if remote_size != local_size:
                raise RuntimeError(
                    f"GitHub 上已有同名资产 {asset_name}（{remote_size} 字节），"
                    f"与本地上传文件（{local_size} 字节）不一致；请勾选「允许覆盖同名版本」后重试")
            self.log(f"GitHub 上已存在同名资产 {asset_name} 且大小一致，跳过上传")
        else:
            self.log(f"覆盖 GitHub 资产 {asset_name} …" if asset_name in existing else "上传 GitHub 资产…")
            github_upload_asset_safe(owner, repo, token, release["id"], asset_name, local,
                                     force=use_force, progress=self.set_progress)
            self.log("GitHub 上传完成（已覆盖）" if asset_name in existing else "GitHub 上传完成")
        return f"https://github.com/{owner}/{repo}/releases/download/{tag}/{urllib.parse.quote(asset_name)}"

    # ---------------- 校验

    def _verify(self, sources, expected_size):
        failed = []
        for source in sources:
            label = source.get("label") or source["id"]
            try:
                status, length = head_size(source["url"])
                if status == 200 and (length is None or length == expected_size):
                    self.log(f"校验通过：{label}（{status}，{length} 字节）")
                else:
                    failed.append(f"{label}（{status}，{length} 字节，期望 {expected_size}）")
                    self.log(f"校验异常：{label}（{status}，{length} 字节，期望 {expected_size}）")
            except Exception as exc:  # noqa: BLE001
                if source["id"] == "wg":
                    self.log(f"校验跳过：{label}（需连接校内组网）：{exc}")
                else:
                    failed.append(f"{label}（{exc}）")
                    self.log(f"校验失败：{label}：{exc}")
        return failed


if __name__ == "__main__":
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
    app = Publisher()
    app.mainloop()
