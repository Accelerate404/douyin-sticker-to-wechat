# 抖音表情包 → 微信我的表情

将抖音「喜欢的表情包」自动转存到微信「我的表情」。

## 工作原理

微信没有直接导入表情包的 API，本工具的策略是：

1. 通过 Playwright CDP 连接 Edge 浏览器，从抖音网页版抓取喜欢的表情包
2. 下载 WebP/GIF 格式图片，自动转换为微信支持的 GIF 格式
3. 压缩至微信自定义表情要求（≤500KB, ≤320×320）
4. 通过 MTP（USB 文件传输）推送到手机 `DCIM/stickers/`
5. 用户在微信中手动添加：聊天 → 表情 → 红心 → 添加单个表情 → 从相册选择

## 安装

```bash
pip install -r requirements.txt
playwright install chromium
```

## 使用方法

```bash
python main.py
```

### 操作步骤

1. **打开浏览器** — 自动启动 Edge 并连接抖音网页版
2. **开始抓取** — 检查登录状态，等待扫码登录后，在浏览器中手动打开聊天表情面板（红心标签），回到程序点击抓取
3. **推送到手机** — 自动转换格式 + 压缩，通过 USB MTP 复制到手机
4. **微信添加** — 在微信中逐个添加到表情（微信限制每次只能选一张）

也可以点击「手动选择本地表情包」导入已有图片，或点击「页面诊断」分析当前页面结构。

## 目录结构

```
├── main.py               # GUI 主程序入口
├── config.py             # 配置（路径、超时、请求头等）
├── douyin_scraper.py     # 抖音表情包抓取（Playwright CDP + 页面诊断）
├── sticker_converter.py  # 图片格式转换与压缩（WebP → GIF）
├── adb_push.py           # MTP 文件推送到手机（PowerShell COM）
├── wechat_importer.py    # 微信 PC 端发送（旧方案，保留备用）
├── capture_templates.py  # 模板截图（废弃）
├── diagnose.py           # DOM 调试工具
├── requirements.txt      # Python 依赖
└── downloaded_stickers/  # 下载的表情包（运行时生成）
```

## 技术细节

- **浏览器连接**: 通过 Chrome DevTools Protocol (CDP) 连接 Edge，自动管理启动和关闭
- **DOM 识别**: 多策略定位抖音表情面板（已知选择器 → 弹窗分析 → CDN 特征匹配）
- **页面诊断**: dump 页面 DOM 结构到 `page_dump.json`，快速定位正确的选择器
- **格式转换**: WebP → GIF（微信自定义表情仅支持 GIF 格式）
- **GIF 压缩**: 优先降低色彩质量保帧率，必要时保守抽帧（保持播放速度）
- **MTP 推送**: 通过 Windows Shell COM + PowerShell 实现可靠的 MTP 文件复制

## 注意事项

- 需要安装 Edge 浏览器（`playwright install chromium` 仅用于辅助）
- 手机通过 USB 连接电脑，选择「文件传输」模式（不需要开发者模式）
- 微信添加表情时每次只能选一张，100 个表情约需 15-20 分钟手动操作
- 抖音前端 class 名频繁变化，抓取失败时使用「页面诊断」更新选择器
- `douyin_cookies.json` 包含登录凭证，请勿泄露
