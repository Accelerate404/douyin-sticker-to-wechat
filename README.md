# 抖音表情包 → 微信我的表情

将抖音「喜欢的表情包」按需增量抓取、高帧率优化转换并直接发送到**电脑版微信**聊天窗口，右键即可保存到「我的表情」（无需连接手机、无需手机权限）。

## 快速开始

### 方式一：直接下载 EXE（即开即用，推荐）

无需安装 Python 或任何依赖：

1. 前往本仓库的 [Releases 页面](https://github.com/Accelerate404/douyin-sticker-to-wechat/releases) 下载 `DouyinStickerToWeChat.exe`
2. 放到任意空文件夹中，**双击运行**即可（需本机已安装 Windows 自带的 Microsoft Edge 与电脑版微信）
3. 下载的表情包、转换成品、历史记录和独立浏览器登录状态会自动保存在 `.exe` 同级目录下

### 方式二：源码运行

```bash
pip install -r requirements.txt
python main.py
```

> 仅依赖本机已安装的 Microsoft Edge 浏览器，无需额外执行 `playwright install chromium`。

### 自行打包 EXE

```bash
pip install pyinstaller
python build.py
```

打包产物位于 `dist/DouyinStickerToWeChat.exe`（推送 `v*` 标签到 GitHub 也会通过 GitHub Actions 自动构建并发布 Release）。

## 操作步骤

1. **打开浏览器** — 启动纯净独立 Edge 实例并打开抖音网页版（禁用同步与插件，首次扫码登录，后续自动保留抖音登录态）
2. **设置抓取参数 & 开始抓取**：
   - 可设置 **抓取上限**（`0` 表示全部，也可填 `20` / `50` 只抓指定数量）
   - 默认勾选 **「跳过已抓取/已发送」**，以前抓过的不会重复下载或重复发送
   - 在抖音中打开任意聊天窗口 → 点击表情按钮 → 切换到红心/收藏表情 → 点击「开始抓取」
3. **发送到电脑微信**：
   - 打开电脑版微信，进入 **「文件传输助手」**（推荐）或任意聊天窗口
   - 点击 **「3. 发送到电脑微信」**，程序会自动转换并逐个发送表情到聊天窗口（可随时点「停止」或按 `ESC` 中断）
4. **保存到微信表情** — 在电脑版微信聊天窗口中，右键刚刚发出的表情 → 点击 **「添加到表情」** 即可同步到手机和电脑微信的「我的表情」

## 工作原理

1. **纯净独立 Edge 抓取**：通过 Playwright CDP 连接独立配置目录（`edge_profile/`），结合网络 JSON 拦截与表情面板二维网格 DOM 识别 + 自动滚动抓取，未打开表情面板时主动弹窗拦截，绝不误抓视频封面。
2. **统一输出高兼容透明 GIF**：
   - 依据文件魔数识别真实图片格式
   - 静态图与动图统一转为透明/高清 **GIF**（全局共享调色板 + 保持总时长的渐进压缩阶梯），确保电脑版微信右键均支持「添加到表情」
   - 支持 **高清模式 (≤950KB, 推荐)** 与 **兼容模式 (≤500KB)**
3. **纯 Win32 API 电脑微信直发**：通过 `ctypes` 直接调用 Windows `CF_HDROP` 剪贴板与窗口聚焦接口发送文件，零外部系统工具依赖，也无需手机连接或开启权限。

## 目录结构

```
├── main.py                       # GUI 主程序入口
├── config.py                     # 路径、压缩预设与增量历史管理
├── douyin_scraper.py             # 抖音表情抓取（纯净 Edge CDP + 增量跳过 + 数量限制）
├── sticker_converter.py          # 全局调色板透明 GIF 转换与高帧率压缩
├── wechat_sender.py              # 电脑版微信 Win32 API 自动粘贴发送
├── tests/test_core.py            # 核心逻辑自动化单元测试
├── build.py                      # PyInstaller 一键打包脚本
├── .github/workflows/release.yml # GitHub Actions 自动打包发布工作流
├── requirements.txt              # Python 依赖（playwright, httpx, Pillow）
├── LICENSE                       # MIT 开源协议
├── downloaded_stickers/          # 下载的原始表情包（运行时生成）
├── converted_stickers/           # 转换优化后的 GIF 成品（运行时生成）
├── sticker_history.json          # 已抓取/已发送哈希记录（运行时生成）
└── edge_profile/                 # 独立 Edge 登录状态目录（运行时生成）
```
