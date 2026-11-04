# njitmcl-tools · NJIT MCL 发布工具

NJIT MCL 社团的整合包 / 存档发布工具。把打包好的 zip 推到「校内服务器 + GitHub Release」，再把元数据登记到社团网站，最后回读各下载源做校验。

> 从 **1.1.0** 起改为分发 **单文件 Python 脚本**（`njitmcl-publisher.py`），不再提供打包 exe。

## 下载

| 文件 | 说明 |
|---|---|
| [`njitmcl-publisher.py`](https://github.com/njit-fkcy/njitmcl-tools/releases/download/latest/njitmcl-publisher.py) | 工具本体（单文件，约 97 KB） |
| [`publisher.json`](https://github.com/njit-fkcy/njitmcl-tools/releases/download/latest/publisher.json) | 自更新清单（版本号、大小、sha256） |
| [`config.example.json`](https://github.com/njit-fkcy/njitmcl-tools/releases/download/latest/config.example.json) | 配置模板（凭据字段为空） |

## 运行

需要 Python 3.9+ 与 `paramiko`：

```powershell
python -m pip install paramiko
python njitmcl-publisher.py
```

首次运行会在同目录生成 `config.json`，把 GitHub token、内网服务器账号填进去即可（**不要提交到仓库**）。

## 功能

- **整合包**：上传 zip 到 `wg:/var/www/modpacks/<服务器ID>/`，建 GitHub Release（tag `<服务器ID>-<版本>`），登记中控 `servers`
- **存档**：上传到 `saves/` 子目录，Release tag `saves-<服务器ID>`
- **补丁**：按 revision 生成增量补丁，自动补 5 个下载源（内网 + GitHub + 3 个中转）
- **原始包识别**：从 mrpack / CurseForge / 离线包自动读取名称、版本、加载器、Java 版本要求
- **发布后校验**：回读各源，校验状态码、大小、sha256

## 自更新

启动 1.2 秒后在后台读取 `publisher.json`（GitHub 直链，失败回退 `gh-proxy.com`，8 秒超时，失败静默）。只有远端 `versionCode` **大于**本机值才弹窗，并打开下载页。

## 发布新版本

1. 改 `发布工具.py`，递增 `PUBLISHER_VERSION` 与 `PUBLISHER_VERSION_CODE`
2. 复制为 `njitmcl-publisher.py`，算 `sha256` 与 `size`
3. 更新 `publisher.json`（version / versionCode / publishedAt / sha256 / size / url）
4. 覆盖 `latest` Release 的两个资产（先删后传）
5. 同步本仓库根目录的 `njitmcl-publisher.py` 与 README 版本说明
6. 在 `njit-fkcy/njitmc-changelog` 的 `changes/<日期>.md` 记一条

**不递增 `versionCode` 就不会有人收到更新提示**（与启动器同一规则）。

## 版本历史

| 版本 | versionCode | 变更 |
|---|---|---|
| 1.1.0 | 3 | 改为单文件 py 分发：`pack_edit.py` 内联进主脚本；不再提供打包 exe |
| 1.0.1 | 2 | 原始包自动识别（名称/版本/加载器/Java 要求）；新增 Java 版本与启用开关；修正整合包误发到启动器仓库 |
| 1.0.0 | 1 | 加入自更新检查；仓库改为组织 `njit-fkcy` |

## 说明

- 配置里的凭据只放本机 `config.json`，仓库与文档不记录
- 本工具只在 Windows 本机跑（需要同时访问 GitHub 与校内服务器）
