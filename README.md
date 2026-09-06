# AstrBot CPA Usage

匹配当前 AstrBot Provider，实时读取 CPA Auth File 的套餐和剩余配额，并渲染为容量卡片。整个查询链只读：不访问 `usage-queue`，不统计已用量，也不会执行 consume/reset。

## 查询范围

- 通过 CPA `/v0/management/auth-files` 获取 Auth Files。
- 通过 `/v0/management/api-call` 代发 provider 官方只读 quota 请求。
- 已适配 `codex`、`claude`、`gemini-cli`、`antigravity`、`kimi`、`xai`。
- API-key provider 不会误用 OAuth quota endpoint。
- 卡片只包含 provider 类型/名称、脱敏账号、套餐、续期/重置时间以及各时间窗口剩余量；不会放入 Auth File ID、上游账号 ID 或管理 Token。

## 渲染结构

视觉模板与业务代码分离：

```text
templates/quota_card.html
templates/res/css/quota_card.css
templates/res/fonts/
templates/res/backgrounds/
```

字体和背景在渲染时转换成 Data URI，生成的是不依赖外部资源的 HTML。用户上传背景优先，内置背景兜底；支持 `random`、`daily`、`fixed` 三种选择方式。资源只能位于插件目录或 AstrBot 插件数据目录，单文件最大 5 MB。

AstrBot 路径直接使用框架的 `html_render(..., return_url=True)`，不需要 Node.js、Chromium 或 ffmpeg。Node.js/Playwright 只供仓库外的手动截图入口使用。

## AstrBot 配置与触发

把仓库放入 AstrBot 的 `data/plugins/astrbot_plugin_cpa_usage`，然后在插件配置中添加 CPA Provider 映射：

```json
{
  "provider_id": "AstrBot 中的 Provider ID",
  "base_url": "http://host.docker.internal:8317",
  "token": "CPA Management Token",
  "provider_type": "codex",
  "enabled": true
}
```

在配置界面点击“绑定 AstrBot Provider”，选择目标 Provider Source 下任意一个聊天模型。界面保存的是锚点模型 ID，插件运行时会解析它的 `provider_source_id`；当前会话切换到同一 Source 下的其他模型时仍会命中。旧版没有 Provider Source 分层时回退为 Provider ID 精确匹配。

触发命令：

```text
/cpausage
```

仅注册 `/cpausage` 一个指令入口。

## 手动实时测试

先复制并编辑 `.env.example` 为 `.env`。本机需有 Node.js 20+、Chromium 和 Playwright 驱动：

```bash
cd /mnt/storage/github/astrbot_plugin_cpa_usage
npm install
python3 run_manual_test.py --provider-id N5-CPA --provider-type codex
```

脚本每次都会重新访问 CPA；成功后输出：

```text
/tmp/cpa-quota-live.html
/tmp/cpa-quota-live.png
```

如果无法自动找到 Chromium：

```bash
python3 run_manual_test.py \
  --provider-id N5-CPA \
  --provider-type codex \
  --chromium-path /path/to/chrome
```

自定义背景示例（文件须在仓库或 `--asset-dir` 内）：

```bash
python3 run_manual_test.py \
  --provider-id N5-CPA \
  --provider-type codex \
  --asset-dir /path/to/cpa-assets \
  --background-image background-a.webp \
  --background-image background-b.png \
  --background-strategy daily
```

## 独立模块

```python
from cpa_usage import CPAUsageClient, QuotaCardRenderer

client = CPAUsageClient("http://127.0.0.1:8317", "management-key")
snapshot = client.fetch_quota_snapshot(provider_type="codex")

renderer = QuotaCardRenderer(background_strategy="daily")
renderer.write_html(snapshot, "/tmp/cpa-quota.html")
renderer.write_png(snapshot, "/tmp/cpa-quota.png", html_path="/tmp/cpa-quota.html")
```

## 测试

标准库测试不启动浏览器：

```bash
python3 -m unittest discover -s tests -v
```

额外执行 Chromium 集成测试：

```bash
CPA_RUN_CHROMIUM_TESTS=1 python3 -m unittest \
  tests.test_render.RenderTests.test_write_png_creates_png_file -v
```
