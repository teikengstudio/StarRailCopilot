**| [English](README_en.md) | 简体中文 | [Español](README_es.md) | [日本語](README_ja.md) |**


# StarRailCopilot

Star Rail auto script | 星铁速溶茶，崩坏：星穹铁道脚本，基于下一代Alas框架。

本仓是 [LmeSzinc/StarRailCopilot](https://github.com/LmeSzinc/StarRailCopilot) 的云游戏协议直连增强分支。保留原有安卓模式，新增不经过模拟器或 ADB 的云游戏设备后端。

下方截图及上游 Wiki 为通用界面说明；本分支的协议模式以本文为准，其他语言 README 尚未同步本分支功能。

![gui](https://raw.githubusercontent.com/wiki/LmeSzinc/StarRailCopilot/README.assets/gui_cn.png)

![setting](https://raw.githubusercontent.com/wiki/LmeSzinc/StarRailCopilot/README.assets/setting_cn.png)

## 功能

- **打本**：每日副本、双倍活动副本、历战余响；安卓模式另支持[角色养成规划](https://github.com/LmeSzinc/StarRailCopilot/wiki/Planner_cn)。
- **收获**：完成每日任务，收派委托，收取无名勋礼奖励。
- **模拟宇宙**：安卓模式可刷模拟宇宙及内圈遗器；协议模式禁用。
- **后台托管**：自动启动游戏、清体力和完成日常，通过仪表盘查看资源及云游戏钱包。
- **云游戏**：仅国服。支持[安卓云客户端](https://github.com/LmeSzinc/StarRailCopilot/wiki/Cloud_cn)及下述协议直连模式；直连模式提供扫码、密码登录、人工极验与浏览器触控预览。

## 安装与更新

[中文安装教程](https://github.com/LmeSzinc/StarRailCopilot/wiki/Installation_cn)，包含自动安装教程，使用教程，手动安装教程。

上游安装教程和安装包不代表已包含本分支的协议功能。使用协议模式请从本仓获取源码，或将已有部署的更新源切换到本仓。当前已验证的 Python 和 Docker 基线为 **Python 3.10**；先创建并激活虚拟环境，再执行：

```sh
git clone https://github.com/teikengstudio/StarRailCopilot.git
cd StarRailCopilot
python -m pip install -r requirements.txt
```

首次手动安装时，将 `config/deploy.template.yaml` 复制为 `config/deploy.yaml`，按环境设置 Git 路径和 WebUI 配置；已有部署保留原配置，不覆盖。向局域网或公网提供访问前应设置 WebUI 密码。配置完成后启动：

```sh
python gui.py
```

已有环境升级时需重新安装 `requirements.txt`，不能只更新代码而保留旧版 PyAV/WebSocket 依赖。WebUI 地址、端口和密码由 `config/deploy.yaml` 决定。

[设备支持文档](https://github.com/LmeSzinc/AzurLaneAutoScript/wiki/Emulator_cn)，支持 Windows/Mac/Linux 以及各种骚方式运行。

本分支的安装与自更新使用 [teikengstudio/StarRailCopilot](https://github.com/teikengstudio/StarRailCopilot) 的 `master` 分支。部署配置中的 `global` 与 `cn` 均指向该仓库，不使用上游 CDN 更新包；文档与设备支持链接仍保留上游来源。

> **是否必须使用模拟器？** 安卓模式仍需要模拟器或 Android 设备；云游戏协议直连模式不需要模拟器，也不占用本机鼠标键盘。普通桌面游戏窗口控制不是本分支的接入方式。

> **模拟器的性能表现如何？** Lme 的 8700k+1080ti 使用 MuMu 12 模拟器画质设置非常高是有 40fps 的，如果你的配置稍微新一点的话，特效最高 60fps 不是问题。

## 云游戏协议模式

在 `SRC设置 → 游戏客户端` 选择 `云游戏协议直连`。原有安卓配置不会自动切换，请按配置实例选择模式。

### 登录与设备配置

1. 点击 `采集当前浏览器设备`，保存浏览器画像及稳定设备 ID。可在设备区修改字段，或在高级设置中编辑设备画像 JSON。
2. 点击 `扫码登录 / 刷新`，按提示扫描并确认；也可展开账号密码登录，点击 `保存加密凭据并登录`。已有 Cookie 或 `credentials.json` 可在高级设置中导入。
3. 登录后配置日常任务并运行脚本；需要查看或人工操作云画面时，点击 `打开独立触控预览`。

设备画像、Cookie 和 RSA 加密后的账号密码按实例保存在 `config/cloud/<配置名>.json`。明文账号密码不写入普通任务配置，保存后清空输入；登录态被明确判定失效时，使用已保存密文尝试一次重登。

需要极验时，WebUI 显示官方 v3/v4 组件，等待人工完成后继续原请求。验证码不自动求解；短信、身份验证及账号限制提示官方处理入口。网络失败不会触发密码登录循环。

**RSA 密文仍可用于登录，必须按密码保护，不要分享账号文件。** 设备采集读取的是当前 SRC WebUI 浏览器，不会跨站读取官方 Cookie，也不能代替官方设备指纹。

### 独立预览与触控

预览地址为 `/cloud/<配置名>/preview`，例如 `/cloud/src/preview`，沿用 SRC 的访问密码。打开页面本身不启动云实例，点击 `连接预览` 后才连接；预览会消耗云游戏时长。

- 支持截图、全屏、点击、长按、滑动、多指触控及游戏剪贴板。
- 同一配置的多个预览与 WebUI 启动的脚本共享一个云会话，默认只读，同时只允许一个浏览器控制者。
- 脚本运行时，申请触控需等待脚本在安全点暂停确认；释放控制后重新识别游戏页面。
- `关闭预览` 只释放当前观看者。最后一个手动观看者离开 15 秒后主动结束实例；脚本拥有的实例不会因预览关闭而结束。
- `结束云会话` 可立即关闭手动会话，但不能关闭脚本拥有的实例。命令行独立运行的脚本不使用 WebUI 共享会话。

### 钱包、排队与恢复

`仪表盘更新` 任务会读取协议钱包，更新免费时长分钟数、星云币可用分钟数、畅玩卡剩余天数及时间戳。畅玩卡按整天向上取整，仍有效但不足一天时显示 1 天。钱包查询复用当前会话；单独查询钱包时只使用 HTTP，不额外分配云实例。完整的仪表盘更新任务还要读取游戏内资源，因此仍需连接游戏。

默认使用普通队列。每次分配实例时读取 `在云游戏中使用星云币快速排队`，只有显式启用才选择星云币队列，不会因失败自动切换付费队列。

排队不设本地轮询次数或总等待时限，服务端仍返回排队中时会按其查询间隔持续等待。停止脚本或取消连接会发送退队请求；明确的服务端失败、请求错误仍会结束本次排队。实例分配后的握手、视频帧和停止确认继续保留有限超时。等待下一项任务时关闭云游戏会直接停止，不会为了截图重新启动连接或排队。

每次排队轮询会在 SRC 日志中打印服务端返回的当前排名和预计等待分钟数；WebUI 调度脚本也会收到这些日志。服务端未返回的字段显示 `?`，预计时间为 0 时保留显示为 0。

传输中断、视频轨道结束或连续 30 秒无视频帧时，对原实例最多按 1、2、4 秒重连。服务端主动结束会话不自动新建实例；旧实例退出状态不明时阻止再次分配，并提示人工确认。

登录后先调用官方云账号初始化接口，再查询钱包或排队。`-110003` 表示服务端判定可用时长不足；出现该错误时应核对官方页面余额与所选账号，不能只据未初始化的余额判定时长已耗尽。该错误不触发自动重登或付费队列切换。

### 已验证范围与限制

- 已用真实 `src` 配置完成副本（支援与连续战斗）、委托、无名勋礼、每日任务、白嫖奖励和仪表盘资源更新；协议钱包读取及跨进程传递也已验证。
- 兑换码通过协议剪贴板和游戏粘贴按钮输入。`STARRAILGIFT` 实测完整粘贴、提交，并正确识别“无效的兑换码”；**有效兑换码发奖尚未验证**。
- 扫码、密码密文重登和极验已接入；本地验证回传不等同于全部真实账号风控组合均已验收。
- Linux Docker 的依赖、WebUI、扫码面板和独立预览入口已部署验证；上述游戏内日常验收基于 Windows 环境。
- 协议模式禁用饰品提取、剧情连点器、角色养成规划工具和模拟宇宙，菜单、调度器及运行入口均拦截。自动养成、自动换队和战败处理不是本分支新增能力；通用游戏任务问题跟随上游修复。

云节点的 TLS 连接仍验证证书链和主机名，未关闭证书验证。协议核心复用 `cloud-starrail-reverse`，GPLv3 许可证保留在 `module/device/cloud/core/LICENSE`。

## Docker 部署注意事项

已有 Docker 部署升级时应保留原镜像名、容器名、端口、设备映射和工程目录挂载，不需要额外的数据目录。配置与日志仍位于原工程的 `config/`、`log/`；协议凭据位于同一配置目录下的 `config/cloud/`。

本分支不提供新的统一 Docker 镜像或 Compose 模板。沿用已有 Dockerfile 时，需将镜像内依赖更新到 `requirements.txt` 对应版本，再重建或替换容器；只拉取源码不足以完成旧环境升级。原实例的客户端模式、凭据、访问密码与自动运行列表应保留，不自动迁移到协议模式。

如果挂载目录只因可执行权限而被 Git 识别为大量修改，可在容器的 `/app` 中执行 `git config --local core.fileMode false`。更新器会在重新初始化仓库后保留该设置，避免仅权限差异导致 `stash` 后仍无法快进更新；文件内容修改仍按原有保留本地修改流程处理。

## 跟随上游更新

[Sync upstream 工作流](https://github.com/teikengstudio/StarRailCopilot/actions/workflows/sync-upstream.yml) 计划每天北京时间 **11:23**（UTC 03:23）检查 `LmeSzinc/StarRailCopilot` 的 `master`；GitHub 定时触发可能延迟。也可在 Actions 页面手动运行，操作账号须具备所需仓库权限。

工作流将上游合并到本仓 `master` 并普通推送，保留本仓云协议改动，不重置分支、不强制推送。发生合并冲突时停止并在运行摘要列出冲突文件，线上分支不变，需人工处理。该工作流限定在 `teikengstudio/StarRailCopilot` 执行；其他 fork 需自行调整仓库条件。

## 开发

QQ一群 752620927 (有开发意向请加一群)
QQ二群 1033583803
Discord https://discord.gg/aJkt3mKDEr

- [小地图识别原理](https://github.com/LmeSzinc/StarRailCopilot/wiki/MinimapTracking)
- 开发文档（目录在侧边栏）：[Alas wiki](https://github.com/LmeSzinc/AzurLaneAutoScript/wiki/1.-Start)，但很多内容是新写的，建议阅读源码和历史提交。
- 开发路线图：见置顶 issue，欢迎提交 PR，挑选你感兴趣的部分进行开发即可。

> **如何添加多语言/多服务器支持？** 需要适配 assets，参考 [开发文档 “添加一个 Button” 一节](https://github.com/LmeSzinc/AzurLaneAutoScript/wiki/4.1.-Detection-objects#%E6%B7%BB%E5%8A%A0%E4%B8%80%E4%B8%AA-button)。

## 关于 Alas

SRC 基于碧蓝航线脚本 [AzurLaneAutoScript](https://github.com/LmeSzinc/AzurLaneAutoScript) 开发，Alas 经过三年的发展现在已经达到一个高完成度，但也累积了不少屎山难以改动，我们希望在新项目上解决这些问题。

- 更新 OCR 库。Alas 在 cnocr==1.2.2 上训练了多个模型，但依赖的 [mxnet](https://github.com/apache/mxnet) 已经不怎么活跃了，机器学习发展迅速，新模型的速度和正确率都碾压旧模型。
- 配置文件 [pydantic](https://github.com/pydantic/pydantic) 化。自任务和调度器的概念加入后用户设置数量倍增，Alas 土制了一个代码生成器来完成配置文件的更新和访问，pydantic 将让这部分更加简洁。
- 更好的 Assets 管理。button_extract 帮助 Alas 轻易维护了 4000+ 模板图片，但它有严重的性能问题，对外服缺失 Assets 的提示也淹没在了大量垃圾 log 中。
- 减少对于碧蓝的耦合。Alas 框架和 Alas GUI 有对接其他游戏及其脚本的能力，但已经完成的明日方舟 [MAA](https://github.com/MaaAssistantArknights/MaaAssistantArknights) 插件和正在开发的 [fgo-py](https://github.com/hgjazhgj/FGO-py) 插件都发现了 Alas 与碧蓝航线游戏本身耦合严重的问题。

