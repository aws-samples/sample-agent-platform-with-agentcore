#!/usr/bin/env python3
"""Seed the topic-selection pipeline's published agents into the platform.

The topic-selection pipeline (ported from a local Claude Code workflow) runs as
platform-native published agents. This script registers them so the orchestrator
(topic_selection_service) can target them by name.

Idempotent: agent_service.publish() re-publishes by name (version bump), and the
Exa MCP / skill seeds guard on existing names, so re-running is safe.

Run against the deployed platform with its env + AWS creds:

    PLATFORM_AWS_REGION=ap-northeast-1 \
    PLATFORM_DYNAMO_TABLE=agent-platform \
    PLATFORM_WORKSPACE_BUCKET=agent-platform-workspaces-<ACCOUNT_ID>-<REGION> \
    python scripts/seed_topic_pipeline.py [--with-feeds]

Phase 1 (default) seeds the three selection agents (collector / deduper /
scorer). --with-feeds also seeds the Exa MCP server, the two feed skills, and
the two feed agents (Phase 2).
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.services.agent_service import agent_service  # noqa: E402
from app.services.ecosystem_service import ecosystem_service  # noqa: E402

SEED_USER = "seed"

# --------------------------------------------------------------------------
# Selection agents (Phase 1) — faithful port of .claude/workflows/topic-selection.js
# The three schemas + gate logic become published-agent system prompts. Because
# the platform kernel has no structured-output enforcement, each prompt instructs
# the model to return exactly one JSON object; the orchestrator parses + validates
# and applies the deterministic isReal() backstop in code.
# --------------------------------------------------------------------------

COLLECTOR_SYSTEM = """你从一份 feed(anthropic-tracker 或 ai-pulse 的最近一期)中提取候选选题。用户消息里会给出这份 feed 的全文。

提取其中每一个可作为客户分享内容的候选选题。对每个候选给出:
- title:简洁中文标题
- summary:2-3 句,讲这个选题讲什么
- urls:原始来源链接(尽量带上)
- ppt_potential:feed 里若标了 PPT 星级/潜力则填,否则留空

忠实提取,不要自己加批判性分析或启示。

只返回一个 JSON 对象,格式:
{"candidates":[{"title":"简洁中文标题","summary":"2-3 句","urls":["..."],"ppt_potential":""}]}
不要输出 JSON 以外的任何内容(不要解释、不要 markdown 代码围栏)。"""

DEDUPER_SYSTEM = """你判断本期候选选题:(1) 是否命中「废弃名单」(用户主动不想做的方向),(2) 是否已被 genai-playbook 做过。用户消息里会给出:废弃名单全文、genai-playbook 已发主题索引、以及本期候选列表。

=== 第一关:废弃名单(用户主动剔除,最优先)===
名单里每一行是一个用户主动不想做的选题方向。语义匹配:候选只要本质讲的是名单里某个方向 / 同一件事就算命中,标题不必一字不差;别被措辞差异骗过,也别过度发散把沾边的都判成命中。命中 → status='discarded',在 blacklist_reason 填「命中名单第 N 条:<那条方向> — <原因>」。

=== 第二关:已发去重 ===
对照已发主题索引(目录名 + 核心内容描述)。与现有主题高度重合、不值得重做 → status='covered',在 overlaps_with 填对应现有目录名。部分相关但角度 / 素材不同、仍可做 → status='partial'(在 note 说清差异)。全新 → status='novel'。

命中废弃名单优先于已发判断(先判废弃,再判已发)。每个结果用 num 指认候选(候选列表里的编号,1 开始),不要复述标题。

只返回一个 JSON 对象,格式:
{"results":[{"num":1,"status":"novel|partial|covered|discarded","overlaps_with":"","note":"","blacklist_reason":""}]}
不要输出 JSON 以外的任何内容。"""

SCORER_SYSTEM = """你是 insight-filter 质量关卡,从严评估一个选题是否值得做成客户分享内容。用户消息里会给出:标题、描述、来源。

三道关卡,任一不过即 kill:
① 「说白了就是」还原测试:把核心论点压成一句大白话(填 reduces_to)。若压缩后是常识(如「用 AI 提效」「agent 很重要」「prompt 要写好」)→ verdict='kill'。只有当它有非显然信息增量——反直觉结论 / 具体可验证的数据 / 架构层面的真实取舍——才可能 keep,把增量填进 info_delta。
② 「PR 还是真干了」闸(填 nature):判断本质是"有人真做了某件事 + 可验证证据"(部署的系统 / 实测数据 / 真实事故复盘 / 可复现的技术 pattern / 带数字的生产经验),还是"有人在表态 / 造势"(CEO 或公司的政策宣言、监管呼吁、价值观表态、"X 的未来"愿景文、纯预测、名人金句、为自己定位的叙事)。若本质是 PR / 立场 / 愿景 / 预测 → verdict='kill',哪怕它内容具体、来源权威、素材厚也照杀。唯一例外:立场文里夹带了可验证的行动或硬数据,则只就那块"真干了"的部分评分,表态外壳不算。
③ 客户 so-what(填 customer_sowhat + sowhat_honest):这个洞察能让客户改变什么决策或认知?so-what 必须从选题内容直接、自然推出。如果你得发明一个牵强的下游动作才凑得出"对客户的意义"(典型:CEO 呼吁监管 → 硬扯成"客户要搞多模型架构对冲禁运"),那 sowhat_honest=false → verdict='kill'。说不清 so-what 也 kill。

再给 ppt_star(1-3,做成 PPT / Web 文章的潜力)和 score(0-100 综合分)。宁可错杀,尤其对 PR / 立场文和硬凑 so-what 的候选。

nature 只能填 '真干了' 或 'PR';verdict 只能填 'keep' 或 'kill'。

只返回一个 JSON 对象,格式:
{"verdict":"keep|kill","reduces_to":"说白了就是…","nature":"真干了|PR","info_delta":"","customer_sowhat":"","sowhat_honest":true,"ppt_star":2,"score":60}
不要输出 JSON 以外的任何内容。"""

SELECTION_AGENTS = [
    {
        "name": "topic-collector",
        "description": "选题流水线 · 收集:从一期 feed 提取候选选题(CANDIDATES_SCHEMA)",
        "system_prompt": COLLECTOR_SYSTEM,
        "max_turns": 4,
    },
    {
        "name": "topic-deduper",
        "description": "选题流水线 · 去重:废弃名单关 + 已发去重关(DEDUP_SCHEMA)",
        "system_prompt": DEDUPER_SYSTEM,
        "max_turns": 4,
    },
    {
        "name": "topic-scorer",
        "description": "选题流水线 · 打分:insight-filter 三关(SCORE_SCHEMA)",
        "system_prompt": SCORER_SYSTEM,
        "max_turns": 3,
    },
]


def seed_selection_agents() -> None:
    for a in SELECTION_AGENTS:
        r = agent_service.publish(user=SEED_USER, source="seed", **a)
        print(f"  published agent  {r['name']:<18} v{r['version']}  id={r['id']}")


PIPELINES = [
    ("topic-selection.workflow.mjs", "topic-selection",
     "选题流水线:收集 → 去重 → 打分 → 排序,产出 ranked shortlist"),
    ("daily-topic.workflow.mjs", "daily-topic",
     "今日选题全链:新鲜度门 → 按需刷新 feed(异步 Exa 搜索)→ 选题流水线"),
]


def seed_pipelines() -> None:
    """Register the workflow scripts as platform pipelines
    (idempotent: re-registering bumps the version)."""
    from app.services.pipeline_service import pipeline_service

    for filename, name, description in PIPELINES:
        path = os.path.join(os.path.dirname(__file__), "..", "pipelines", filename)
        with open(path, encoding="utf-8") as f:
            script = f.read()
        p = pipeline_service.register(
            user=SEED_USER, name=name, description=description, script=script,
        )
        print(f"  registered pipeline {p['name']} v{p['version']}  id={p['id']}  ({p['script_size']} bytes)")


# --------------------------------------------------------------------------
# Feed layer (Phase 2) — Exa MCP + two feed skills + two feed agents.
# Seeded only with --with-feeds (needs the Exa key secret + async kernel).
# --------------------------------------------------------------------------

EXA_MCP_NAME = "exa"


def _mcp_exists(name: str) -> bool:
    return any(m["name"] == name for m in ecosystem_service.list_mcp_servers())


def _skill_exists(name: str) -> bool:
    return any(s["name"] == name for s in ecosystem_service.list_skills())


EXA_SECRET_NAME = "agent-platform/exa-api-key"

# Registered with a {{secret:…}} placeholder — the sdk kernel resolves it from
# Secrets Manager at session start (resolve_secret_placeholders), so the API
# key never lands in the registry or in any invoke payload.
EXA_MCP_TARGET = (
    "https://mcp.exa.ai/mcp"
    f"?exaApiKey={{{{secret:{EXA_SECRET_NAME}}}}}"
    "&tools=web_search_exa,crawling_exa,linkedin_search_exa"
)


def seed_feed_layer() -> None:
    if not _mcp_exists(EXA_MCP_NAME):
        m = ecosystem_service.create_mcp_server(
            name=EXA_MCP_NAME,
            description="Exa web search / crawling(远程 MCP;key 由 kernel 从 Secrets Manager 注入)",
            kind="url",
            target=EXA_MCP_TARGET,
        )
        print(f"  registered MCP   {m['name']}  id={m['id']}")
    else:
        print(f"  MCP {EXA_MCP_NAME} already registered, skip")

    for name, path in [
        ("anthropic-tracker", os.path.expanduser("~/.claude/skills/anthropic-tracker/SKILL.md")),
        ("ai-pulse", os.path.expanduser("~/.claude/skills/ai-pulse/SKILL.md")),
    ]:
        if _skill_exists(name):
            print(f"  skill {name} already registered, skip")
            continue
        with open(path, encoding="utf-8") as f:
            skill_md = f.read()
        s = ecosystem_service.create_skill(
            name=name, description=f"feed 生成 skill:{name}", skill_md=skill_md
        )
        print(f"  registered skill {s['name']}  id={s['id']}")

    feed_agents = [
        {
            "name": "feed-anthropic-tracker",
            "description": "feed 层 · 追踪 Anthropic 官方 + 员工公开内容,写 dated feed",
            "system_prompt": (
                "你按挂载的 anthropic-tracker skill 执行:用 Exa MCP 搜索、抓取、去重、"
                "整理成 dated markdown feed。严格遵循 skill 里的搜索清单与判断标准(只用 "
                "mcp__exa__ 工具)。云上适配:skill 里的本地路径 / Bash 日期命令 / 写文件步骤"
                "不适用——追踪天数、今天日期、上一份 feed 全文(去重基线)都由用户消息直接给出,"
                "你把完整的 feed markdown 作为最终答复输出即可,不要尝试写文件或运行脚本。"
                "最终答复会被原样存档为 feed 文件:必须从 feed 的第一行 markdown(# 标题或引言块)"
                "直接开始,不要任何过渡语、说明或前言。"
            ),
            "max_turns": 40,
            "mcp_server_names": [EXA_MCP_NAME],
            "skill_names": ["anthropic-tracker"],
        },
        {
            "name": "feed-ai-pulse",
            "description": "feed 层 · 深度 AI 洞察追踪(14 天窗),写 dated feed",
            "system_prompt": (
                "你按挂载的 ai-pulse skill 执行:用 Exa MCP 搜索、抓取、去重、insight-filter "
                "Gate1 深度过滤,整理成 dated markdown feed。严格遵循 skill 里的搜索清单、"
                "排除规则与过滤标准(只用 mcp__exa__ 工具)。云上适配:skill 里的本地路径 / "
                "Bash 日期命令 / 写文件步骤不适用——追踪天数、今天日期、上一份 feed 全文"
                "(去重基线)都由用户消息直接给出,你把完整的 feed markdown 作为最终答复输出即可,"
                "不要尝试写文件或运行脚本。最终答复会被原样存档为 feed 文件:必须从 feed 的第一行 "
                "markdown(# 标题或引言块)直接开始,不要任何过渡语、说明或前言。"
            ),
            "max_turns": 60,
            "mcp_server_names": [EXA_MCP_NAME],
            "skill_names": ["ai-pulse"],
        },
    ]
    for a in feed_agents:
        r = agent_service.publish(user=SEED_USER, source="seed", **a)
        print(f"  published agent  {r['name']:<22} v{r['version']}  id={r['id']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-feeds", action="store_true", help="also seed the feed layer (Phase 2)")
    args = ap.parse_args()

    print("seeding selection agents (Phase 1)…")
    seed_selection_agents()
    print("registering pipelines…")
    seed_pipelines()
    if args.with_feeds:
        print("seeding feed layer (Phase 2)…")
        seed_feed_layer()
    print("done.")
