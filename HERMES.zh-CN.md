# 用 Hermes / 中转站 API 跑 loamwright SEO 流水线（部署说明）

这个 fork 加了一个 `hermes_adapter/`，让原本只能在 Claude Code 里运行的文章流水线，可以在服务器上用**任何兼容 OpenAI 接口的模型 API（包括中转站）**跑起来，并由 Hermes 或 cron 触发。

## 改了什么、没改什么

**没改（质量机制全部保留）**：45 个阶段的编排器 `scripts/pipeline/orchestrator.py`、调度器 `run_pipeline.py`、所有 lint / 质量门 / 事实核查 / 独立审稿 / 发布前检查 / 发布后线上校验。阶段算不算"完成"，仍然只由原来的 `verify_stage()` 决定，adapter 没有任何办法跳过。

**新增 / 修改**：

| 内容 | 作用 |
|---|---|
| `hermes_adapter/driver.py` | 替代"Claude Code 读 SKILL.md 手动跑循环"：调用 `run_pipeline`，遇到需要大模型的阶段就派给对应 agent，质量门不过就修复后重跑 |
| `hermes_adapter/agent.py` + `tools.py` | 用中转站模型跑 `agents/*.md` 里的每个子 agent，**只给它定义里声明的工具**（写手只有 Read/Write，不能联网），写 JSON 时自动做 schema 校验，跑命令前做成本检查 |
| `hermes_adapter/run_article.py` | 命令行入口（Hermes / cron 调这个） |
| `hermes_adapter/bootstrap_project.py` + `templates/clawclipfactory/` | 用"填表"代替交互式 `/init`，工厂事实只能来自你填的内容 |
| SEOPress 适配 | 新增 MU 插件 `install/wordpress-mu-plugin/xuanran-seopress-rest-bridge.php`；`wp_publisher` 写 SEOPress 字段并回读校验；`verify_post` 的草稿检查支持 SEOPress |
| `hermes_adapter/hermes/seo-article/SKILL.md` | 给 Hermes 用的技能说明 |
| `tests/` | 22 个离线测试（含真实编排器的全链路测试），不花钱 |

---

## 第 1 步：服务器环境

需要 Python **3.11 或更高**。

```bash
cd ~
git clone https://github.com/135798888/loamwright-SEO-Skill.git loamwright-seo-skill
cd loamwright-seo-skill
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests/ -q        # 应显示 22 passed
```

> 以后每次更新：`cd ~/loamwright-seo-skill && git pull`

## 第 2 步：配置模型（中转站）

```bash
mkdir -p ~/.xuanran-seo
cp hermes_adapter/llm.example.yaml ~/.xuanran-seo/llm.yaml
nano ~/.xuanran-seo/llm.yaml      # 改 base_url、各角色的模型名、单价
echo 'export LW_LLM_API_KEY="你的中转站key"' >> ~/.bashrc && source ~/.bashrc
python -m hermes_adapter.run_article --check
```

`--check` 返回 `"ok": true` 才算通。要点：

- **模型必须支持 function calling（工具调用）**，否则 agent 没法读写文件。
- 写手（writer）、审稿（reviewer）、润色（humanizer）建议用最强的模型，这三个决定文章质量。
- `prices` 填中转站的实际单价（美元/百万 token），用于单篇预算上限 `max_llm_usd_per_article`。没填单价的模型按 0 计算，报告里会提示。

## 第 3 步：其他 API 密钥

都放在 `~/.xuanran-seo/credentials/`（仓库外，不会被提交）：

```bash
mkdir -p ~/.xuanran-seo/credentials/wordpress
echo "tvly-你的key"   > ~/.xuanran-seo/credentials/tavily.key     # 必需：研究阶段搜索
echo "你的serpapi key" > ~/.xuanran-seo/credentials/serpapi.key    # 强烈建议：真实 SERP 数据
echo "sk-生图用的key"  > ~/.xuanran-seo/credentials/openai.key     # 生图（不要图可跳过）
chmod 600 ~/.xuanran-seo/credentials/*.key
```

**生图走中转站**：编辑 `~/.xuanran-seo/config.yaml`，加上：

```yaml
image:
  default_mode: realtime
  model: gpt-image-2            # 填中转站里的生图模型名，必须支持 4K 尺寸（见下方说明）
  providers:
    - name: relay
      base_url: https://你的中转站/v1
      credential: openai        # 用上面的 openai.key
cost_limits:                    # 注意键名：原 README 写的 per_article/daily 不会被读取，代码读的是下面这些
  per_article_usd: 3.0          # 单次脚本调用（研究、生图等）的预估上限
  per_image_batch_usd: 6.0      # 一批图片的上限
  daily_total_usd: 40.0         # 每天总花费上限：包括 adapter 记入账本的大模型花费，超了会拦截脚本调用
```

> ⚠ 原插件生图**固定请求 4K 尺寸**（如 3840x2160），只有 gpt-image-2 或 Gemini 3 Pro Image 这类模型支持。中转站只有 gpt-image-1 / dall-e-3 的话会报尺寸错误。这种情况先告诉我，我把尺寸改成可配置。`--image-count 0`（纯文字）原插件允许，但我还没验证它能完整走完后面的图片检查。
>
> B2B 工厂站建议每篇少放 AI 图（`--image-count 2` 或 `3`）。数据图表是本地渲染的，不花钱。真实工厂照片后期在 WordPress 里替换效果更好。

**WordPress 应用密码**：WordPress 后台 → 用户 → 个人资料 → 应用程序密码，新建一个（这个用户至少要是"编辑"角色）。

```bash
cat > ~/.xuanran-seo/credentials/wordpress/clawclipfactory.json <<'EOF'
{ "url": "https://clawclipfactory.com", "username": "你的WP用户名", "app_password": "xxxx xxxx xxxx xxxx xxxx xxxx" }
EOF
chmod 600 ~/.xuanran-seo/credentials/wordpress/clawclipfactory.json
```

## 第 4 步：WordPress 装 SEOPress 桥接插件

把 `install/wordpress-mu-plugin/xuanran-seopress-rest-bridge.php` 上传到网站的
`wp-content/mu-plugins/` 目录（没有这个目录就新建）。MU 插件不用启用，上传即生效。

验证：浏览器打开 `https://clawclipfactory.com/wp-json/xuanran/v1/seopress-bridge`，看到
`"bridge_active": true` 和 `"seopress_active": true` 就对了。

> 没装这个插件，文章还是能发成草稿，但 SEO 标题、描述、关键词写不进 SEOPress，流水线最后的线上校验会失败。

## 第 5 步：填工厂资料并初始化项目

编辑 `hermes_adapter/templates/clawclipfactory/business-context.json`，把所有 `TODO` 换成**真实**信息：品牌名、成立年数、材料、MOQ、打样 / 开模 / 大货周期、产能、定制项、质检、认证、包装、署名作者、WordPress 里已有的博客分类名。

写手提到你们工厂的数字时**只能用这里的内容**，还有一道自动检查专门拦截和这里矛盾的数字。所以这一步填得越准，文章越像工厂自己写的，也越不会出现编造的数据。不确定的项直接删掉那一行，不要猜。

```bash
python -m hermes_adapter.bootstrap_project --from hermes_adapter/templates/clawclipfactory --check-wp
```

返回 `"ok": true` 且 `seopress_bridge: true` 就好了。还有 TODO 没填，它会列出来并拒绝安装。

## 第 6 步：手动试跑一篇（第一次建议盯着看）

```bash
python -m hermes_adapter.run_article --project clawclipfactory \
  --keyword "custom claw clips wholesale" --image-count 2
```

屏幕上会滚动显示每个阶段。跑完最后一行是 JSON：`status: complete` 表示 WordPress 里已经有一篇**草稿**，并且线上校验通过。去后台检查后手动发布即可。

每篇任务的过程文件在 `memory/workspace/<任务ID>/`：
- `hermes-run.jsonl`：每一步做了什么
- `hermes-run-report.json`：最终结果和花费
- `draft.md`、`fact-check.json`、`review.json` 等：各阶段产物

中途失败、修好原因后可以接着跑，已完成的阶段不会重做：

```bash
python -m hermes_adapter.run_article --resume <任务ID>
```

## 第 7 步：接入 Hermes

把技能复制到 Hermes 的技能目录（下面是默认路径，你的 Hermes 版本如果不同，以它的文档为准）：

```bash
mkdir -p ~/.hermes/skills
cp -r hermes_adapter/hermes/seo-article ~/.hermes/skills/
```

之后在 Telegram 里对 Hermes 说"写一篇关于 custom claw clips wholesale 的 SEO 文章"，它会在后台启动任务、过一会儿查看进度、最后把草稿链接发给你。

## 第 8 步：定时自动跑（每天一篇）

把要写的关键词一行一个放进 `~/loamwright-seo-skill/keywords.txt`，然后 `crontab -e` 加一行（每天北京时间 9 点跑一篇）：

```cron
CRON_TZ=Asia/Shanghai
0 9 * * * cd ~/loamwright-seo-skill && LW_LLM_API_KEY=你的key .venv/bin/python -m hermes_adapter.run_article --project clawclipfactory --queue keywords.txt >> logs/cron.log 2>&1
```

- 每次只取一个关键词，跑完记到 `keywords.txt.done`（含状态和任务 ID）。
- 上一篇还没跑完时，新的一次会直接退出，不会叠加花钱。
- 想要 Telegram 通知，在 `llm.yaml` 里配置 `telegram` 段。也可以让 Hermes 的定时任务去调同一个命令，由 Hermes 汇报结果。

## 费用控制（三层）

1. `llm.yaml → limits.max_llm_usd_per_article`：单篇大模型花费上限，超了立刻停。
2. `config.yaml → cost_limits`：原插件自带，管单次调用和每日总额（每日总额也包含大模型花费）。
3. 质量门修复轮数 `repair_rounds` 和重试次数 `llm_stage_retries`：防止反复重试烧钱。

## 已知限制（请先看）

- **还没在真实环境跑过完整一篇。** 离线测试覆盖了工具隔离、schema 校验、重试、修复、预算和 SEOPress 字段，也用真实编排器测通了"研究阶段 → 校验 → 进入下一阶段"。但真实模型能不能一次过所有质量门，要在你服务器上跑了才知道。第一篇出问题很正常，把 `memory/workspace/<任务ID>/hermes-run.jsonl` 和终端最后几十行发给我就行。
- 原插件 agent 里提到的 `mcp__tavily` 等 MCP 工具在这里不可用。原设计中它们只是备用，主路径是仓库里的 Python 脚本，不影响主要功能。
- 原插件的提示词是按 Claude Opus 写的，又长又严格。换成较弱的模型，过质量门的轮数会变多，花费会上升。
- 发布永远是**草稿**，由人工确认后发布（原插件的硬规则 5a）。
