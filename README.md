**| [English](README_en.md) | 简体中文 | [Español](README_es.md) | [日本語](README_ja.md) |**


# StarRailCopilot

Star Rail auto script | 星铁速溶茶，崩坏：星穹铁道脚本，基于下一代Alas框架。

![gui](https://raw.githubusercontent.com/wiki/LmeSzinc/StarRailCopilot/README.assets/gui_cn.png)

![setting](https://raw.githubusercontent.com/wiki/LmeSzinc/StarRailCopilot/README.assets/setting_cn.png)

## 功能

- **打本**：[角色养成规划](https://github.com/LmeSzinc/StarRailCopilot/wiki/Planner_cn)，每日副本，双倍活动副本，历战余响。
- **收获**：完成每日任务，收派委托，收取无名勋礼奖励。
- **模拟宇宙**：刷模拟宇宙，使用开拓力刷内圈遗器。
- **后台托管**：自动启动模拟器和游戏，后台托管清体力和每日，通过仪表盘了解资源情况。
- **云游戏**：（仅国服）[在云崩坏星穹铁道上运行SRC](https://github.com/LmeSzinc/StarRailCopilot/wiki/Cloud_cn)

## 安装 [![](https://img.shields.io/github/downloads/LmeSzinc/StarRailCopilot/total?color=4e4c97)](https://github.com/LmeSzinc/StarRailCopilot/releases)

[中文安装教程](https://github.com/LmeSzinc/StarRailCopilot/wiki/Installation_cn)，包含自动安装教程，使用教程，手动安装教程。

[设备支持文档](https://github.com/LmeSzinc/AzurLaneAutoScript/wiki/Emulator_cn)，支持 Windows/Mac/Linux 以及各种骚方式运行。

本分支的安装与自更新使用 [teikengstudio/StarRailCopilot](https://github.com/teikengstudio/StarRailCopilot) 的 `master` 分支。部署配置中的 `global` 与 `cn` 均指向该仓库，不使用上游 CDN 更新包；文档与设备支持链接仍保留上游来源。

> **为什么使用模拟器？** 如果你用桌面端来运行脚本的话，游戏窗口必须保持在前台，我猜你也不想运行脚本的时候不能动鼠标键盘像个傻宝一样坐在那吧，所以用模拟器。

> **模拟器的性能表现如何？** Lme 的 8700k+1080ti 使用 MuMu 12 模拟器画质设置非常高是有 40fps 的，如果你的配置稍微新一点的话，特效最高 60fps 不是问题。

## 云游戏协议模式

在 `SRC设置 → 游戏客户端` 选择 `云游戏协议直连`，无需模拟器或 ADB。支持扫码、密码登录、人工极验及独立触控预览；日常任务和游戏内兑换码仍需真实账号验收。协议模式禁用剧情连点器、角色养成规划工具和模拟宇宙，菜单、调度器及运行入口均会拦截。

1. 点击 `采集当前浏览器设备`，保存浏览器提供的设备画像及稳定设备 ID；也可编辑设备画像 JSON。
2. 点击 `扫码登录 / 刷新`，使用移动端扫描并确认；也可展开账号密码登录，保存 RSA 密文。Cookie 或 `credentials.json` 导入位于高级设置。
3. 点击 `打开独立触控预览`，进入 `/cloud/<配置名>/preview`。打开页面不会启动云实例，点击 `连接预览` 后才连接。预览支持截图、全屏、点击、长按、滑动和游戏剪贴板。

设备画像、Cookie 和 RSA 加密后的账号密码按实例保存在 `config/cloud/<配置名>.json`，不写入普通任务配置。登录态失效时使用已保存密文尝试一次登录；需要极验时在 WebUI 中等待人工完成并继续原请求。验证码不自动求解，短信、身份验证和账号限制显示官方处理入口，网络失败不触发密码登录循环。

同一配置的多个预览和脚本共享一个云会话。脚本运行时默认只读；申请触控后等待脚本安全点暂停确认，释放控制后重新识别游戏页面。关闭预览只释放当前观看者，最后一个手动观看者离开 15 秒后主动结束实例；`结束云会话` 可立即结束手动会话，但不能关闭脚本拥有的实例。

传输中断、视频轨道结束或连续 30 秒无视频帧时，对原实例最多按 1、2、4 秒重连。服务端主动结束会话不自动新建实例；旧实例退出状态不明时阻止再次分配并提示人工确认。每次分配读取当前星云币快速排队开关，不自动切换付费队列。兑换码通过协议剪贴板及游戏粘贴按钮输入，不依赖 Android 剪贴板。

RSA 密文可用于登录，仍须按密码保护；不要分享账号文件。设备采集只读取当前 SRC WebUI 浏览器，不读取官方站点 Cookie，也不能代替官方设备指纹。默认普通队列，启用现有快速队列选项才使用星云币；预览会消耗云游戏时长。

源码运行需 Python 3.10 或以上，并重新安装 `requirements.txt`。协议核心复用 `cloud-starrail-reverse`，保留 GPLv3 许可证于 `module/device/cloud/core/LICENSE`。离线回归：`python -m unittest discover -s tests -p "test_cloud_*.py"`。

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

