# 抖音表情包 → 微信我的表情

将抖音「喜欢的表情包」自动转存到微信 PC 版「我的表情」。

## 工作原理

微信 PC 版没有直接导入表情包的功能，唯一途径是：**发送图片到聊天窗口 → 右键图片 → 添加到表情**。

本工具自动化整个流程：

1. 通过 CDP 连接 Edge 浏览器，从抖音网页版聊天表情面板抓取喜欢的表情包
2. 下载 WebP/GIF 格式图片，自动转换为微信支持的 GIF 格式
3. 压缩至微信自定义表情要求（≤500KB, ≤320×320）
4. 通过剪贴板粘贴到微信聊天窗口发送
5. 用户右键保存到「我的表情」

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

1. **打开浏览器** — 启动 Edge 并连接 CDP，自动打开抖音网页版
2. **登录抖音** — 用手机扫码登录，然后打开任意聊天窗口，点击表情按钮，切到 ❤️ 标签
3. **开始抓取** — 自动抓取并下载表情包到 `downloaded_stickers/`
4. **发送到微信** — 确保微信 PC 版已打开并进入聊天窗口，程序自动逐个发送
5. **右键保存** — 在微信中右键每张图片 → 添加到表情

也可以点击「手动选择本地表情包」导入已有图片。

## 目录结构

```
├── main.py               # GUI 主程序入口
├── config.py             # 配置（路径、超时、请求头等）
├── douyin_scraper.py     # 抖音表情包抓取（Playwright + CDP）
├── wechat_importer.py    # 微信发送 + GIF 压缩（pyautogui + Pillow）
├── requirements.txt      # Python 依赖
└── downloaded_stickers/  # 下载的表情包（运行时生成）
```

## 技术细节

- **浏览器连接**: 通过 Chrome DevTools Protocol (CDP) 连接 Edge，避免重复登录
- **DOM 识别**: 多策略定位抖音表情面板（已知选择器 → 弹窗分析 → CDN 特征匹配）
- **格式转换**: WebP → GIF（微信自定义表情仅支持 GIF 格式）
- **GIF 压缩**: 优先降低色彩质量保帧率，必要时保守抽帧（保持播放速度）
- **剪贴板**: 通过 PowerShell `SetFileDropList` 以文件方式粘贴，保留 GIF 动画

## 注意事项

- 需要安装 Edge 浏览器（`playwright install chromium` 仅用于辅助）
- 微信 PC 版需要已打开并登录
- 发送前请确认已进入一个聊天窗口（推荐「文件传输助手」）
- 操作过程中不要移动鼠标到屏幕左上角（pyautogui 安全中断）
- `douyin_cookies.json` 包含登录凭证，请勿泄露
