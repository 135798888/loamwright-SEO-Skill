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
| SEOPress 适配 | 新增 `scripts/wordpress/seopress_api.py`，直接调用 SEOPress 自带的 REST API 写入并回读；`verify_post` 的草稿检查改为通过同一接口读取（原版只认 RankMath，SEOPress 草稿会永远校验失败） |
| `hermes_adapter/hermes/seo-article/SKILL.md` | 给 Hermes 用的技能说明 |
| `tests/` | 47 个离线测试（含真实编排器的全链路测试），不花钱 |

---

## 需要哪些 API

| API | 用途 | 是否必需 | 密钥放哪里 |
|---|---|---|---|
| 中转站大模型（OpenAI 兼容） | 所有写作、研究、核查、审稿 agent | 必需 | `~/.xuanran-seo/llm.yaml` |
| Tavily | 研究阶段：深度研究、搜索、抓取竞品页面 | 必需 | `credentials/tavily.key` 或 `TAVILY_API_KEY` |
| SerpApi | 真实 Google 搜索结果特征（PAA、AI 概览等） | 实际上必需（研究阶段要求） | `credentials/serpapi.key` 或 `SERPAPI_KEY` |
| 生图（二选一或都配） | 封面和配图 | 要图就必需 | 见第 3 步 |
| WordPress 应用密码 | 发布草稿 + 写 SEOPress 字段 | 必需 | `credentials/wordpress/clawclipfactory.json` |
| Crossref | 查学术来源 | 免费，无需 key（建议设 `CROSSREF_MAILTO=你的邮箱`） | 环境变量 |
| Bing IndexNow / GSC | 发布后通知收录 | 可选（草稿阶段用不到） | — |

文字类质量检查（EEAT、引用、AI 味评分等）都是本地规则计算，不调用额外的模型。**Gemini 在文章流水线里只用于生图**，不需要 Gemini 文字模型。

## 第 1 步：服务器环境

需要 Python **3.11 或更高**。

```bash
cd ~
git clone https://github.com/135798888/loamwright-SEO-Skill.git loamwright-seo-skill
cd loamwright-seo-skill
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # 若 .venv 里没有 pip（用 uv 建的环境）：
                                  # uv pip install --python .venv/bin/python -r requirements.txt
python -m pytest tests/ -q        # 应全部 passed
```

> 以后每次更新：`git pull`，**然后一定再装一次依赖**（新版本可能加了包）：
> `uv pip install --python .venv/bin/python -r requirements.txt`（或 `.venv/bin/python -m pip install -r requirements.txt`）

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

### 只用 ChatGPT（OpenAI）模型

可以全部用 GPT 模型（包括生图用 gpt-image-2）。`llm.yaml` 里参考 `llm.example.yaml` 的 "ChatGPT-only setup" 段：写手、事实核查、去 AI 味、审稿这 4 个决定质量的角色用中转站里最强的 GPT，其他用 mini 版省钱。注意：

- GPT-5 这类推理模型不接受 `max_tokens`，只接受 `max_completion_tokens`，也不接受自定义 temperature。adapter 会按模型名自动选择；遇到识别不了的中转站别名，会根据接口返回的报错自动改参数重试，并记住，下次不再出错。
- 推理模型的"思考"也算在输出 token 里，`max_output_tokens` 建议调到 32000，否则长段落可能写一半被截断。
- 写手和审稿人是同一家的模型，少了一层跨模型交叉检查。审稿人每次都是全新上下文、看不到前面的过程，偏向会小一些，但比不上换一家模型。

### Gemini 第二意见（跨模型复审）

原作者设计过"用另一家模型复查"，但从没接进流水线。现在它是真正会执行的一步：在独立审稿人（第 4 道质量门）通过之后、发布之前，把草稿交给另一家模型，按 4 条标准逐条判断：

1. **事实**：有没有工厂资料里没有、或互相矛盾的说法（MOQ、交期、产能、认证等），有没有像编造的数据
2. **采购价值**：对批发商、品牌方是否真的有用，而不是泛泛的消费者科普
3. **关键词意图**：是否在文章前部就回答了搜索意图
4. **自然度**：读起来像不像模板化的 AI 文章

`llm.yaml` 里的 `second_opinion` 段：

| mode | 行为 |
|---|---|
| `off` | 不运行 |
| `advisory`（建议先用这个） | 运行并记录到 `second-opinion.json` 和 Telegram 通知，**不拦截** |
| `block` | 不通过 → 按它指出的问题自动修复 → 再判一次，仍不通过就**停在发布前**。返回结果读不懂也算不通过 |

建议前 10 篇左右用 `advisory`，对照草稿看它判得准不准，准的话再改成 `block`。模型用中转站里的 Gemini（和写手不同家），默认 `gemini-3.8-flash-high`，备选 `gemini-3.1-pro-low`。记得在 `prices` 里填上它的单价。

部署后先跑 `python -m hermes_adapter.run_article --check`：它会逐个测试每个配置的模型能否调用（带工具调用），并给第二意见模型一篇故意编造数据的小样文，确认它能返回可解析的结论、并且能识别出编造。

## 第 3 步：其他 API 密钥

都放在 `~/.xuanran-seo/credentials/`（仓库外，不会被提交）：

```bash
mkdir -p ~/.xuanran-seo/credentials/wordpress
echo "tvly-你的key"   > ~/.xuanran-seo/credentials/tavily.key     # 必需：研究阶段搜索
echo "你的serpapi key" > ~/.xuanran-seo/credentials/serpapi.key    # 强烈建议：真实 SERP 数据
echo "Vertex快速模式key" > ~/.xuanran-seo/credentials/vertex-gemini.key  # 生图首选：Gemini
echo "sk-中转站key"  > ~/.xuanran-seo/credentials/openai.key     # 生图备选：中转站 gpt-image-2
chmod 600 ~/.xuanran-seo/credentials/*.key
```

**生图**：编辑 `~/.xuanran-seo/config.yaml`。下面两个服务商按顺序尝试，第一个失败自动换第二个，只配一个也行：

```yaml
image:
  default_mode: realtime
  providers:
    - name: vertex-gemini                 # 首选：Gemini 3 Pro Image（原插件实测过 4K）
      protocol: vertex_gemini
      base_url: https://aiplatform.googleapis.com/v1/publishers/google/models
      credential: vertex-gemini           # 读 credentials/vertex-gemini.key 或 VERTEX_GEMINI_API_KEY
      model: gemini-3-pro-image-preview
    - name: relay                         # 备选：中转站的 gpt-image-2
      base_url: https://你的中转站/v1
      credential: openai                  # 读 credentials/openai.key
      model: gpt-image-2
cost_limits:                    # 注意键名：原 README 写的 per_article/daily 不会被读取，代码读的是下面这些
  per_article_usd: 8.0          # 单次脚本调用（研究、生图等）的预估上限。
                                # 生图会把整批图按 OpenAI 官方 4K 高质量价（约 $1.64/张）预估，
                                # 中转站实际便宜得多。设成 3.0 时 2 张图（$3.28）就会被拦截、一张都不生成。
  per_image_batch_usd: 6.0      # 一批图片的上限
  daily_total_usd: 40.0         # 每天总花费上限：包括 adapter 记入账本的大模型花费，超了会拦截脚本调用
```

> ⚠ Gemini 的 key 必须是 **Vertex AI 快速模式（Express mode）** 的 API key：原插件直接请求 `aiplatform.googleapis.com`。Google AI Studio 的 key（`AIza` 开头）在这个地址不能用。如果你只有 AI Studio 的 key，或者中转站提供 Gemini 生图，告诉我，我加一个对应的接入方式。
>
> 不要用 `gemini-3.1-flash-image-preview`（Nano Banana 2）：已有公开报告它在 Vertex 上会忽略 4K 设置、只返回约 1K 的图，而原插件要求 4K。

> ⚠ 原插件生图**固定请求 4K 尺寸**（如 3840x2160），返回尺寸不对就判失败。有些中转站不管要求多大都只返回 1672x941，这时可以在那个 provider 下加一行 `min_long_edge: 1200`：
>
> ```yaml
>     - name: relay
>       base_url: https://你的中转站/v1
>       credential: openai
>       model: gpt-image-2
>       min_long_edge: 1200      # 接受长边 ≥1200 的图；比例不对会居中裁剪；不会放大
> ```
>
> 博客正文宽度一般在 1200px 以内，1672x941 足够清晰。注意：1:1 方图从 1672x941 裁出来只有 941x941，会低于 1200 被拒绝，所以这种中转站只适合 16:9 和 4:3 的图。不加这一行的 provider，行为和原版完全一样。
>
> B2B 工厂站建议每篇少放 AI 图（`--image-count 2` 或 `3`）。数据图表是本地渲染的，不花钱。真实工厂照片后期在 WordPress 里替换效果更好。

**WordPress 应用密码**：WordPress 后台 → 用户 → 个人资料 → 应用程序密码，新建一个（这个用户至少要是"编辑"角色）。

```bash
cat > ~/.xuanran-seo/credentials/wordpress/clawclipfactory.json <<'EOF'
{ "url": "https://clawclipfactory.com", "username": "你的WP用户名", "app_password": "xxxx xxxx xxxx xxxx xxxx xxxx" }
EOF
chmod 600 ~/.xuanran-seo/credentials/wordpress/clawclipfactory.json
```

## 第 4 步：确认 SEOPress 接口可用（不用装插件）

SEOPress 免费版自带 REST API，流水线直接用你的应用密码调用它写入 SEO 标题、描述、关键词和 robots。网站上不需要额外装任何东西。

只需确认两点：SEOPress 已启用；应用密码对应的 WordPress 用户是"编辑"或"管理员"。第 5 步的 `--check-wp` 会自动检查这两项。

## 第 5 步：填工厂资料并初始化项目

编辑 `hermes_adapter/templates/clawclipfactory/business-context.json`，把所有 `TODO` 换成**真实**信息：品牌名、成立年数、材料、MOQ、打样 / 开模 / 大货周期、产能、定制项、质检、认证、包装、署名作者、WordPress 里已有的博客分类名。

写手提到你们工厂的数字时**只能用这里的内容**，还有一道自动检查专门拦截和这里矛盾的数字。所以这一步填得越准，文章越像工厂自己写的，也越不会出现编造的数据。不确定的项直接删掉那一行，不要猜。

```bash
python -m hermes_adapter.bootstrap_project --from hermes_adapter/templates/clawclipfactory --check-wp
```

返回 `"ok": true` 且 `seo_plugin: "seopress"` 就好了。还有 TODO 没填，它会列出来并拒绝安装。

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

**7.1 让 Hermes 能读到 API 密钥。** Hermes 在后台执行命令时，通常不会加载 `~/.bashrc`，所以只设置 `export LW_LLM_API_KEY` 不一定生效。最稳妥的做法是把中转站的 key 直接写进 `~/.xuanran-seo/llm.yaml`（这个文件在仓库外，不会被提交）：

```yaml
api_key: sk-你的中转站key        # 写这一行，删掉或注释掉 api_key_env 那行
```

```bash
chmod 600 ~/.xuanran-seo/llm.yaml
```

**7.2 安装技能。** Hermes 的自定义技能放在 `~/.hermes/skills/` 下，复制进去即可，不需要注册：

```bash
mkdir -p ~/.hermes/skills
cp -r ~/loamwright-seo-skill/hermes_adapter/hermes/seo-article ~/.hermes/skills/
hermes skills list | grep seo-article      # 能看到就说明装上了
```

技能只在**新会话**生效：在 Hermes 里新开对话，或发送 `/reset`。

> 仓库以后 `git pull` 更新时，技能文件可能也有变化，再执行一次上面的 `cp` 即可。

**7.3 使用。** 在 Telegram（或其他接入 Hermes 的地方）发：

- `/seo-article 写一篇关于 custom claw clips wholesale 的文章`
- 或直接说"帮我写一篇关于 acetate claw clips 的 SEO 文章"
- "把 keywords.txt 里下一个关键词写了"
- "刚才那篇文章跑得怎么样了？"

Hermes 会在后台启动任务，过一段时间查看日志，跑完后把草稿链接、文章 ID 和花费告诉你。一篇大约 30–90 分钟，期间可以随时问进度。

## 第 8 步：定时自动跑（每天一篇）

把要写的关键词一行一个放进 `~/loamwright-seo-skill/keywords.txt`，然后 `crontab -e` 加一行（每天北京时间 9 点跑一篇）：

```cron
CRON_TZ=Asia/Shanghai
0 9 * * * cd ~/loamwright-seo-skill && .venv/bin/python -m hermes_adapter.run_article --project clawclipfactory --queue keywords.txt >> logs/cron.log 2>&1
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
