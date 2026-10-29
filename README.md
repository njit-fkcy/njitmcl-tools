# njitmcl-tools

NJIT MCL 社团内部工具的分发仓库，当前收录 **发布工具（NJITMCL-Publisher）**。

> 启动器在 [PCL-NJITMCL](https://github.com/njit-fkcy/PCL-NJITMCL)，整合包与存档在 [PCL-NJITMCL-Modpacks](https://github.com/njit-fkcy/PCL-NJITMCL-Modpacks)。

## 下载

最新版在 [latest Release](https://github.com/njit-fkcy/njitmcl-tools/releases/tag/latest)，固定三个资产：

| 文件 | 说明 |
|---|---|
| `NJITMCL-Publisher.exe` | Windows 单文件，双击即用（无需 Python） |
| `publisher.json` | 自更新清单：`version` / `versionCode` / `size` / `sha256` / `url` |
| `config.example.json` | 配置模板，复制为 `config.json` 后填入自己的凭据 |

第一次运行会在 exe 同目录生成 `config.json`（空值），按模板填写后即可使用。**凭据只放本地 `config.json`，不要提交到任何仓库。**

## 发布工具能做什么

- 启动器：编译产物一键打包、上传 GitHub / 校园内网、生成并上传 `njitmcl.json`、更新官网 `app.js`
- 整合包：拉取线上包、增删模组、重建 zip、生成增量补丁并登记中控 revision
- 存档：超过 2 GB 自动切片上传并登记

完整流程与故障排查见社团档案 `档案-05-NJITMCL启动器.md`（§三 九步发布、§五 已知坑）。

## 自更新机制

- 工具启动约 1 秒后，后台读取 `https://github.com/njit-fkcy/njitmcl-tools/releases/download/latest/publisher.json`（失败时回退 `gh-proxy.com` 前缀）。
- 只有 `versionCode` 大于本机内置值才提示，弹窗确认后打开下载页；检查失败静默，不打扰使用。
- 因此**替换 exe 必须递增 `publisher.json` 的 `versionCode`**，否则已更新的用户收不到提示（与启动器同一条规则）。

## 发新版本

1. 改 `发布工具.py` 顶部 `PUBLISHER_VERSION` 与 `PUBLISHER_VERSION_CODE`。
2. PyInstaller 打包：`python -m PyInstaller --onefile --noconsole --name NJITMCL-Publisher 发布工具.py`。
3. 计算新 exe 的 `size` 与 `sha256`，更新 `publisher.json`。
4. 覆盖 `latest` Release 的 `NJITMCL-Publisher.exe` 与 `publisher.json`（同名资产先删后传）。
5. 验证：`publisher.json` 直链 302 且 `versionCode` 已递增。

## 版本历史

| 版本 | versionCode | 日期 | 说明 |
|---|---|---|---|
| 1.0.0 | 1 | 2026-10-29 | 首个分发版本；新增启动自检更新；仓库迁至组织 `njit-fkcy` |

---

_本仓库只放社团内部工具，不放启动器与整合包资产。_
