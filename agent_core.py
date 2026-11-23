# -*- coding: utf-8 -*-
"""agent_core.py —— 「耕读知行」智能体核心

职责：
1. 意图识别（养护咨询 / 数据查询 / 成长报告 / 劳动知识 / 思政叙事）
2. 工具调用（物联网读数、打卡记录、天气）
3. 知识库检索（BM25 + 意图收敛）
4. 回答编排：接入大模型时由模型组织语言；未配置密钥时用检索结果与真实
   数据模板化编排，保证任何环境下都能演示，不会白屏。

设计上刻意把「有模型」与「无模型」两条路径都做实：比赛现场网络或额度出问题
时，智能体仍然可用，只是语言组织不如大模型自然。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional

from kb import KnowledgeBase, Chunk
from tools import IoTStore, judge_reading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CST_FORMAT = "%Y年%m月%d日"

PERSONA = (
    "你是「耕读知行」，一个面向高校劳动教育实践的智能体，服务对象是参加校园"
    "责任田劳动的大学生。你的知识来自学校劳动教育制度、农事技术资料与劳动教育"
    "政策文件。回答要具体、可执行，能给出数字与操作步骤；涉及本校制度而资料中"
    "没有的内容，要明确说明「以本校正式方案为准」，不得编造条款。"
    "在合适的时候，可以自然地联系劳动精神与育人价值，但不要空喊口号，"
    "要贴着学生手上的事说。"
)

# 意图关键词
INTENT_RULES = [
    ("report", ["成长报告", "实践报告", "总结报告", "写一份报告", "生成报告",
                "学期总结", "汇报", "总结一下"]),
    ("data", ["湿度", "温度", "光照", "读数", "数据", "传感", "几号地", "地块",
              "battery", "电量", "现在多少"]),
    ("weather", ["天气", "下雨", "降雨", "气候", "会热", "台风", "降温"]),
    ("planting", ["种什么", "能种", "适合种", "什么时候种", "播种", "时令",
                  "节气", "农事", "什么时候该", "整地", "育苗", "定植"]),
    ("care", ["发黄", "黄了", "枯萎", "蔫", "虫", "病", "浇水", "施肥", "灌溉",
              "养护", "怎么办", "怎么处理", "长不好", "烂根", "徒长", "不开花",
              "不结果", "裂果"]),
    ("spirit", ["劳模", "劳模精神", "劳动精神", "工匠", "王进喜", "时传祥",
                "袁隆平", "张桂梅", "申纪兰", "故事", "意义", "为什么劳动"]),
    ("policy", ["政策", "纲要", "要求", "学时", "评价", "考核", "五育", "规定",
                "认定", "打卡", "制度", "劳动教育"]),
]

# 症状类词：命中时把检索范围强收敛到养护手册
SYMPTOM_WORDS = ["发黄", "黄了", "枯萎", "蔫", "烂根", "徒长", "裂果",
                 "虫", "病", "不开花", "不结果", "长不好"]

# 安全边界：涉及心理危机、人身安全、极端选择的话题，不做知识库检索，
# 直接给出求助指引。这是思政类应用必须守住的一条线，也是答辩时的加分项。
SAFETY_WORDS = ["自杀", "轻生", "不想活", "活不下去", "结束生命", "自残",
                "想死", "抑郁", "退学", "转专业不想读"]

SAFETY_REPLY = (
    "这个话题我不能用农业知识来回应，但我认真在听。\n\n"
    "如果你正在经历很难熬的时刻，请优先找能帮到你的人：\n\n"
    "- **学校心理健康教育与咨询中心**——多数学校提供免费咨询，在学校官网或学工部页面可查预约方式；\n"
    "- **辅导员或班主任**——处理这类情况更有经验，也是最快能找到你的人；\n"
    "- 如果情况紧急，可拨打**全国心理援助热线 12356**，或请身边的人陪同就医。\n\n"
    "退学、转专业这类决定，建议先和辅导员、家长把事情说清楚再定，"
    "学校一般还有学业指导与资助政策可以一起看。\n\n"
    "我随时可以陪你聊地里的事，但上面这些，才是现在更该先做的。"
)

PLOT_RE = re.compile(r"(P0?\d|[一二三四五六七八九]号地)")


def detect_intent(text: str, has_plot: bool = False) -> str:
    scores: Dict[str, int] = {}
    for name, kws in INTENT_RULES:
        scores[name] = sum(1 for k in kws if k in text)
    best = max(scores.items(), key=lambda kv: kv[1])
    # 报告意图优先级最高：命中即按报告处理
    if scores.get("report", 0) > 0:
        return "report"
    # 明确点名了某块地、且没在描述症状，按数据查询处理
    # （"三号地现在情况怎么样"这类从地块卡片点进来的问法）
    if has_plot and not any(w in text for w in SYMPTOM_WORDS) \
            and scores.get("spirit", 0) == 0:
        return "data"
    if best[1] == 0:
        return "knowledge"
    return best[0]


def find_plot(text: str) -> Optional[str]:
    m = PLOT_RE.search(text or "")
    if not m:
        return None
    raw = m.group(1)
    if raw.startswith("P"):
        return raw.upper() if len(raw) > 2 else "P0" + raw[1]
    cn = "一二三四五六七八九"
    idx = cn.find(raw[0]) + 1
    return f"P0{idx}"


# ---------- 大模型客户端（OpenAI 兼容协议） ----------
class LLMClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.enabled = bool(self.cfg.get("enabled")) and bool(
            self.cfg.get("api_key")) and bool(self.cfg.get("base_url"))

    @property
    def model(self) -> str:
        return self.cfg.get("model", "deepseek-chat")

    def complete(self, messages: List[dict]) -> Optional[str]:
        if not self.enabled:
            return None
        url = self.cfg["base_url"].rstrip("/") + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": float(self.cfg.get("temperature", 0.6)),
            "max_tokens": int(self.cfg.get("max_tokens", 900)),
            "stream": False,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.cfg['api_key']}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                    req, timeout=float(self.cfg.get("timeout", 30))) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
        except (urllib.error.URLError, KeyError, IndexError,
                json.JSONDecodeError, TimeoutError, OSError):
            return None


class Agent:
    def __init__(self, kb: KnowledgeBase, store: IoTStore, config: dict):
        self.kb = kb
        self.store = store
        self.config = config or {}
        self.llm = LLMClient(config.get("llm", {}))

    # ---------- 对外主入口 ----------
    def chat(self, message: str, history: List[dict] | None = None) -> dict:
        message = (message or "").strip()
        if not message:
            return {"answer": "请描述一下你的问题，比如「三号地现在湿度多少」或「番茄叶子发黄怎么办」。",
                    "intent": "empty", "citations": [], "tools": [], "mode": "local"}

        intent = detect_intent(message, has_plot=bool(find_plot(message)))
        plot_id = find_plot(message)
        tools_used: List[dict] = []

        # 0) 安全边界优先于一切业务逻辑
        if any(w in message for w in SAFETY_WORDS):
            return {"answer": SAFETY_REPLY, "intent": "safety", "plot": None,
                    "citations": [], "tools": [], "mode": "guard"}

        # 1) 取工具事实
        facts = self._run_tools(intent, plot_id, message, tools_used)

        # 2) 取知识库
        citations: List[dict] = []
        chunks = self._retrieve(message, intent)
        for c, score in chunks:
            citations.append({
                "label": c.label, "doc": c.doc, "section": c.section,
                "score": round(score, 3), "snippet": c.snippet(message),
            })

        # 3) 组织回答
        if intent == "report":
            answer = self._compose_report(message, facts, citations)
            mode = "template"
        elif self.llm.enabled:
            answer = self._compose_with_llm(message, intent, facts,
                                            citations, history)
            mode = "llm" if answer else "template"
            if not answer:
                answer = self._compose_template(message, intent, facts, citations)
        else:
            answer = self._compose_template(message, intent, facts, citations)
            mode = "template"

        return {"answer": answer, "intent": intent,
                "plot": plot_id, "citations": citations,
                "tools": tools_used, "mode": mode}

    # ---------- 工具执行 ----------
    def _run_tools(self, intent: str, plot_id: Optional[str], message: str,
                   sink: List[dict]) -> dict:
        facts: dict = {}

        def call(name: str, fn, *args, **kwargs):
            try:
                result = fn(*args, **kwargs)
                sink.append({"name": name, "ok": True,
                             "args": list(args) or kwargs, "result": result})
                return result
            except Exception as exc:                      # noqa: BLE001
                sink.append({"name": name, "ok": False, "error": str(exc)})
                return None

        if intent in ("data", "care", "report"):
            plots = call("list_plots", self.store.list_plots)
            facts["plots"] = plots or []

        if intent in ("data", "care") and plot_id:
            facts["plot"] = call("get_plot_data", self.store.get_plot_data, plot_id)
        elif intent == "care" and not plot_id:
            # 未指明地块时：优先挑提示了作物名的地，其次挑读数最异常的地，
            # 都没有就挑第一块地——养护问题总要落在一块具体的地上回答。
            plots = facts.get("plots", [])
            target = None
            for p in plots:
                if p["crop"] and p["crop"] in message:
                    target = p["plot_id"]
                    break
            if target is None:
                for p in plots:
                    if p["soil"] < 45 or p["soil"] > 85:
                        target = p["plot_id"]
                        break
            if target is None and plots:
                target = plots[0]["plot_id"]
            if target:
                facts["plot"] = call("get_plot_data", self.store.get_plot_data, target)

        if intent == "report":
            target = plot_id or "P03"
            facts["plot"] = call("get_plot_data", self.store.get_plot_data, target)
            facts["practice"] = call("summarize_practice",
                                     self.store.summarize_practice, target)
            facts["logs"] = call("get_practice_logs",
                                 self.store.get_practice_logs, target)

        if intent in ("care", "report", "data", "planting", "weather"):
            facts["weather"] = call("get_weather", self.store.get_weather)

        return facts

    # ---------- 检索 ----------
    def _retrieve(self, message: str, intent: str) -> List[tuple[Chunk, float]]:
        boost, factor = None, 1.35
        if intent == "care":
            # 说清症状时，答案几乎一定在养护手册里，直接加强收敛
            if any(w in message for w in SYMPTOM_WORDS):
                boost, factor = ["养护"], 2.6
            else:
                boost = ["养护"]
        elif intent == "planting":
            boost, factor = ["节气"], 2.0
        elif intent == "spirit":
            boost = ["劳模"]
        elif intent == "policy":
            boost = ["劳动教育", "校园劳动实践"]
        elif intent == "report":
            boost = ["劳动教育"]
        return self.kb.search(message, k=3, boost_docs=boost, boost=factor)

    # ---------- 提示词与 LLM 组织 ----------
    def _build_messages(self, message: str, intent: str, facts: dict,
                        citations: List[dict],
                        history: List[dict] | None) -> List[dict]:
        ctx_parts: List[str] = []
        if facts.get("plots"):
            ctx_parts.append("【地块实时数据】")
            for p in facts["plots"]:
                ctx_parts.append(
                    f"- {p['plot_id']} {p['name']}（{p['crop']}，{p['area_m2']}㎡，{p['owner']}）"
                    f" 土壤湿度 {p['soil']}%，气温 {p['temp']}℃，光照 {p['light']} lux，"
                    f"设备电量 {p['battery']}%，数据来源 {'真实设备' if p['source']=='device' else '仿真'}，"
                    f"采集时间 {p['ts']}")
        if facts.get("plot") and facts["plot"].get("ok"):
            p = facts["plot"]
            ctx_parts.append(f"【{p['plot_id']} 判读结论】状态：{p['status']}")
            ctx_parts.extend(f"- {a}" for a in p["alerts"])
        if facts.get("practice"):
            s = facts["practice"]
            ctx_parts.append(
                f"【劳动打卡汇总】共 {s['times']} 次，累计 {s['total_hours']} 小时，"
                f"时间跨度 {s['first_date']} 至 {s['last_date']}")
        if facts.get("logs"):
            ctx_parts.append("【劳动记录明细】")
            for r in facts["logs"]:
                ctx_parts.append(
                    f"- {r['date']} {r['task']}，{r['hours']}小时，{r['people']}人，备注：{r.get('note','')}")
        if facts.get("weather"):
            w = facts["weather"]
            ctx_parts.append(
                f"【气候参考】{w['city']} {w['month']}月 常年特征：{w['desc']}，"
                f"气温约 {w['temp_c']}℃，相对湿度约 {w['humidity']}%")

        if citations:
            ctx_parts.append("【知识库摘录】")
            for i, c in enumerate(citations, 1):
                ctx_parts.append(f"[{i}] {c['label']}：{c['snippet']}")

        intent_hint = {
            "care": "这是养护咨询，先给判断结论，再给具体操作步骤（含时间、用量、注意事项）。",
            "data": "这是数据查询，先直接报数字，再给一句是否在适宜区间的判断。",
            "planting": "这是时令种植提问，给出具体品种与时令窗口，并结合本地气候提醒。",
            "weather": "这是天气/气候提问，给出参考气候并落到田间作业安排上。",
            "spirit": "这是思政类提问，讲清事实，最后落到与校园劳动实践的联系。",
            "policy": "这是制度类提问，按资料回答，本校没有明确规定的要说明“以本校正式方案为准”。",
            "report": "这是报告生成请求，按结构输出完整报告。",
            "knowledge": "按知识库资料回答，资料不足时说明不知道，不要编造。",
        }.get(intent, "")

        messages = [{"role": "system", "content": PERSONA + "\n\n" + intent_hint}]
        for h in (history or [])[-4:]:
            role = h.get("role")
            if role in ("user", "assistant") and h.get("content"):
                messages.append({"role": role, "content": str(h["content"])[:1200]})
        messages.append({
            "role": "user",
            "content": ("以下是系统提供的实时数据与知识库资料，回答时必须以这些事实为准，"
                        "不要自行编造数字：\n\n" + "\n".join(ctx_parts) +
                        f"\n\n学生的问题：{message}"),
        })
        return messages

    def _compose_with_llm(self, message: str, intent: str, facts: dict,
                          citations: List[dict],
                          history: List[dict] | None) -> Optional[str]:
        return self.llm.complete(self._build_messages(message, intent, facts,
                                                      citations, history))

    # ---------- 无模型时的模板化编排 ----------
    def _compose_template(self, message: str, intent: str, facts: dict,
                          citations: List[dict]) -> str:
        lines: List[str] = []
        now = datetime.now().strftime(CST_FORMAT)
        w = facts.get("weather") or {}

        if intent == "data":
            p = facts.get("plot")
            if p and p.get("ok"):
                lines.append(f"**{p['name']}（{p['crop']}）实时读数**（{p['ts']}）\n")
                lines.append(f"- 土壤湿度：**{p['soil']}%**")
                lines.append(f"- 气温：**{p['temp']}℃**")
                lines.append(f"- 光照：**{p['light']} lux**")
                lines.append(f"- 设备电量：{p['battery']}%"
                             f"（{'真实设备上报' if p['source']=='device' else '仿真数据'}）")
                lines.append(f"\n**判读：{p['status']}**")
                lines.extend(f"- {a}" for a in p["alerts"])
            else:
                plots = facts.get("plots", [])
                if plots:
                    lines.append("**当前各地块状态**\n")
                    lines.append("| 地块 | 作物 | 土壤湿度 | 气温 | 光照 | 状态 |")
                    lines.append("|---|---|---|---|---|---|")
                    for p in plots:
                        st = judge_reading(p["crop"], type("R", (), {
                            "soil": p["soil"], "temp": p["temp"],
                            "light": p["light"], "battery": p["battery"]})())["status"]
                        lines.append(f"| {p['plot_id']} {p['name']} | {p['crop']} | "
                                     f"{p['soil']}% | {p['temp']}℃ | {p['light']}lx | {st} |")
                    lines.append("\n可以继续问「三号地现在湿度多少」查看单块地的详细判读。")
                else:
                    lines.append("暂时读不到地块数据，请确认感知设备是否在线。")

        elif intent == "weather":
            if w:
                lines.append(f"**{w['city']} {w['month']}月气候参考**\n")
                lines.append(f"- 天气：{w['desc']}")
                lines.append(f"- 气温：约 {w['temp_c']}℃")
                lines.append(f"- 相对湿度：约 {w['humidity']}%")
                lines.append("")
                lines.append("**对田间作业的影响**")
                tips = []
                if w["temp_c"] >= 30:
                    tips.append("- 高温时段避开 11:00—15:00 下地，灌溉改到早晚；")
                if w["month"] in (6, 7, 8, 9):
                    tips.append("- 正值汛期，下地前检查排水沟，暴雨后及时排涝；")
                if w["humidity"] >= 72:
                    tips.append("- 湿度大，注意通风，预防霜霉病、白粉病；")
                if not tips:
                    tips.append("- 这个气候条件下适合下地作业，注意劳逸结合、及时补水；")
                lines.extend(tips)
                lines.append("- 到地里前先在左侧看一眼实时读数，比看天气预报准。")
            else:
                lines.append("暂时取不到气候数据，请稍后再试。")

        elif intent == "planting":
            if citations:
                lines.append(f"**时令参考**（《{citations[0]['doc']}》）\n")
                lines.append(citations[0]["snippet"])
                lines.append("")
            if w:
                lines.append(f"**当前气候**：{w['city']} {w['month']}月 常年特征为"
                             f"{w['desc']}，气温约 {w['temp_c']}℃，相对湿度约 {w['humidity']}%。")
                lines.append("")
            lines.append("**建议**：按上面的时令窗口选品种；岭南四季不分明，"
                         "以气温与降雨为准比死守节气更可靠。")

        elif intent == "care":
            p = facts.get("plot")
            if p and p.get("ok"):
                lines.append(f"**先说结论**：{p['name']}（{p['crop']}）当前状态为「{p['status']}」。\n")
                lines.append("**监测到的情况**")
                lines.extend(f"- {a}" for a in p["alerts"])
                lines.append("")
            if citations:
                lines.append(f"**处置建议**（依据《{citations[0]['doc']}》）")
                lines.append(citations[0]["snippet"])
                lines.append("")
            lines.append("**具体操作**")
            lines.append("1. 先按上面的建议处理最紧急的一项，不要同时改动多个条件；")
            lines.append("2. 处理后 24 小时回看读数是否回到适宜区间；")
            lines.append("3. 若两天内没有改善，换上现场照片向指导教师求助。")

        elif intent == "report":
            return self._compose_report(message, facts, citations)

        elif intent in ("spirit", "policy", "knowledge"):
            if citations:
                lines.append(f"**参考资料**：《{citations[0]['doc']}》· {citations[0]['section']}\n")
                for i, c in enumerate(citations, 1):
                    lines.append(f"{'**核心内容**' if i == 1 else '**补充**'}：{c['snippet']}")
                    lines.append("")
                if intent == "spirit":
                    lines.append("把这件事放到你自己的责任田里看：持续照料一块地的过程，"
                                 "本身就是劳动精神最朴素的练习。")
            else:
                lines.append("知识库里暂时没有直接对应的资料。可以换个说法再问，"
                             "或者问本学期的劳动安排、打卡认定这类具体问题。")
        else:
            lines.append("我在知识库里没有找到很贴合的内容。你可以试试这样问：")
            lines.append("- 「三号地现在湿度多少」")
            lines.append("- 「番茄叶子发黄怎么办」")
            lines.append("- 「芒种前后在广东能种什么」")
            lines.append("- 「帮我生成这学期的劳动实践成长报告」")

        if citations and intent in ("care", "planting", "spirit", "policy", "knowledge"):
            lines.append("")
            lines.append(f"> 参考：{citations[0]['label']}")
        return "\n".join(lines).strip()

    # ---------- 成长报告 ----------
    def _compose_report(self, message: str, facts: dict,
                        citations: List[dict]) -> str:
        p = facts.get("plot") or {}
        s = facts.get("practice") or {}
        logs = facts.get("logs") or []
        w = facts.get("weather") or {}
        name = "同学"
        now = datetime.now()

        L: List[str] = []
        L.append(f"# 劳动实践成长报告（草稿）\n")
        L.append(f"> 由「耕读知行」智能体依据真实监测数据自动生成，生成时间："
                 f"{now.strftime('%Y年%m月%d日 %H:%M')}。"
                 f"第五部分为待你本人补充的反思内容，系统不代为撰写。\n")

        L.append("## 一、实践概况\n")
        if p.get("ok"):
            L.append(f"- 责任地块：{p['plot_id']} {p['name']}，面积 {p['area_m2']} ㎡，"
                     f"种植作物：{p['crop']}")
            L.append(f"- 承担小组：{p.get('owner','—')}")
        if s:
            L.append(f"- 劳动次数：{s['times']} 次")
            L.append(f"- 累计劳动时长：{s['total_hours']} 小时")
            L.append(f"- 实践周期：{s['first_date']} 至 {s['last_date']}")

        L.append("\n## 二、劳动过程记录\n")
        if logs:
            L.append("| 日期 | 劳动内容 | 时长 | 参与人数 | 备注 |")
            L.append("|---|---|---|---|---|")
            for r in logs:
                L.append(f"| {r['date']} | {r['task']} | {r['hours']}h | "
                         f"{r['people']}人 | {r.get('note','')} |")
        else:
            L.append("（本学期的打卡记录尚未同步，请在劳动教育管理平台导出后补充。）")

        L.append("\n## 三、地块监测数据\n")
        if p.get("ok"):
            L.append(f"截至 {p['ts']}，地块实时读数为：土壤湿度 {p['soil']}%、"
                     f"气温 {p['temp']}℃、光照 {p['light']} lux，"
                     f"系统判读状态为「{p['status']}」。\n")
            L.extend(f"- {a}" for a in p["alerts"])
            L.append("\n监测数据由土壤湿度、空气温湿度与光照传感器采集，"
                     "经 MQTT 网关上报至云平台，智能体通过接口实时读取。"
                     "这些数据用于佐证劳动过程真实发生，不替代打卡记录。")
        else:
            L.append("（监测数据暂未获取，请确认设备在线。）")

        L.append("\n## 四、问题与解决\n")
        if p.get("ok") and p["status"] != "正常":
            L.append(f"本学期监测中发现的主要问题：{'；'.join(p['alerts'][:2])}。")
            L.append("处置思路：先按养护手册判断是水分、温度还是光照引起的，"
                     "再对症处理，处理后在 24 小时内回看数据是否改善。")
        else:
            L.append("本学期地块整体运行平稳，未出现需要紧急处置的异常。")

        L.append("\n## 五、思想收获（**请本人撰写**）\n")
        L.append("这一部分需要你自己写，评审最看重的也是这里。建议按三层来写，"
                 "避免停留在「劳动很辛苦」这种空泛表述：\n")
        L.append("1. **具体的事实**：某一天你做了什么、遇到什么具体问题、怎么解决的；")
        L.append("2. **认知的转变**：动手之前你怎么看这件事，做完之后有什么不同；")
        L.append("3. **价值的确认**：这件事让你对劳动、对专业、对责任有了什么新的理解，准备怎么延续。")

        L.append("\n## 六、下一步计划\n")
        if w:
            L.append(f"结合{w['city']}{w['month']}月的气候特征（{w['desc']}，"
                     f"气温约 {w['temp_c']}℃），后续管理重点为：")
        L.append("- 按作物生育期调整水肥，保持土壤湿度在适宜区间；")
        L.append("- 台风与强降雨前完成排水沟疏通与棚架加固；")
        L.append("- 学期末完成测产与劳动成果处置登记，形成完整的实践档案。")

        L.append("\n---\n")
        L.append("**本报告可用于**：劳动教育课程作业、综合素质评价材料、"
                 "竞赛作品中的“应用情况介绍”页。提交前请补全第五部分并核对数据。")
        return "\n".join(L)


def load_config(path: str | None = None) -> dict:
    path = path or os.path.join(BASE_DIR, "config.json")
    cfg = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
        except (OSError, json.JSONDecodeError):
            cfg = {}
    # 环境变量优先，便于部署时不改文件
    llm = cfg.setdefault("llm", {})
    if os.environ.get("LLM_API_KEY"):
        llm["api_key"] = os.environ["LLM_API_KEY"]
        llm["enabled"] = True
    if os.environ.get("LLM_BASE_URL"):
        llm["base_url"] = os.environ["LLM_BASE_URL"]
    if os.environ.get("LLM_MODEL"):
        llm["model"] = os.environ["LLM_MODEL"]
    return cfg


if __name__ == "__main__":
    import sys
    sys.path.insert(0, BASE_DIR)
    kb = KnowledgeBase(os.path.join(BASE_DIR, "knowledge"))
    store = IoTStore()
    agent = Agent(kb, store, load_config())
    print("模型状态:", "已接入" if agent.llm.enabled else "未接入（模板模式）")
    tests = [
        "三号地现在湿度多少",
        "番茄叶子发黄怎么办",
        "芒种前后在广东能种什么",
        "讲一个劳模故事",
        "帮我生成这学期的劳动实践成长报告",
    ]
    for q in tests:
        r = agent.chat(q)
        print("\n" + "=" * 70)
        print("问：", q, f"  [意图={r['intent']} 模式={r['mode']}]")
        print("-" * 70)
        print(r["answer"][:700])
