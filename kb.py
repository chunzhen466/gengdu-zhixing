# -*- coding: utf-8 -*-
"""kb.py —— 轻量知识库：中文二元分词 + BM25 检索（仅用标准库）

设计目标：
1. 零第三方依赖，保证在任意装有 Python 3.8+ 的电脑上直接可跑；
2. 不依赖分词器，中文按「单字 + 二元组」切分，对短查询与专有名词召回稳定；
3. 按 Markdown 的 ## 小节切块，检索结果可精确回溯到「文档 + 小节」，
   便于在界面上展示引用来源，也便于上传到扣子等平台时按小节拆分。
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

CJK = r"\u4e00-\u9fff"
TOKEN_RE = re.compile(rf"[{CJK}]+|[A-Za-z]+|\d+(?:\.\d+)?")

# 口语与书面语的同义归一。学生提问多用口语（"叶子发黄""怎么办"），
# 知识库正文多用书面语（"叶片发黄""如何处置"），不做扩展会漏召回。
SYNONYMS: Dict[str, List[str]] = {
    "叶子": ["叶片", "叶面"],
    "叶片": ["叶子", "叶面"],
    "发黄": ["黄化", "变黄", "叶片发黄"],
    "黄化": ["发黄", "变黄"],
    "怎么办": ["怎么处理", "如何处理", "处置", "措施"],
    "怎么处理": ["怎么办", "处置", "措施"],
    "浇水": ["灌溉", "补水", "水分"],
    "灌溉": ["浇水", "补水"],
    "施肥": ["追肥", "基肥"],
    "太干": ["偏干", "干旱", "缺水"],
    "太湿": ["过湿", "积水"],
    "烂根": ["根腐", "积水"],
    "徒长": ["细长", "茎细"],
    "虫子": ["蚜虫", "害虫", "虫害"],
    "生病": ["病害", "病虫害", "真菌"],
    "种什么": ["品种", "适宜", "种植"],
    "多久": ["时长", "时间"],
    "打分": ["评价", "成绩", "考核"],
    "奖励": ["成果", "认定"],
}


def tokenize(text: str) -> List[str]:
    """中文取单字与相邻二元组；英文数字取小写词。"""
    tokens: List[str] = []
    for raw in TOKEN_RE.findall(text or ""):
        low = raw.lower()
        if re.fullmatch(rf"[{CJK}]+", raw):
            tokens.extend(list(low))
            tokens.extend(low[i:i + 2] for i in range(len(low) - 1))
        else:
            tokens.append(low)
    return tokens


@dataclass
class Chunk:
    doc: str
    section: str
    text: str
    tf: Dict[str, int] = field(default_factory=dict)
    length: int = 0

    @property
    def label(self) -> str:
        return f"{self.doc} · {self.section}" if self.section else self.doc

    def snippet(self, query: str = "", width: int = 220) -> str:
        """挑出与查询最相关的一段，并按句子边界截断，避免展示半截话。"""
        body = re.sub(r"[ \t]+", " ", self.text)
        # 去掉 Markdown 标记，避免摘录里冒出 "### 小标题" 这样的原始符号
        body = re.sub(r"^\s*#{1,6}\s*", "", body, flags=re.MULTILINE)
        body = re.sub(r"^\s*>\s?", "", body, flags=re.MULTILINE)
        body = re.sub(r"\*\*([^*]+)\*\*", r"\1", body)
        body = re.sub(r"`([^`]+)`", r"\1", body)
        body = re.sub(r"\n{2,}", "\n", body).strip()
        if not body:
            return ""
        if not query:
            return body[:width] + ("…" if len(body) > width else "")

        terms = [t for t in set(tokenize(query)) if len(t) >= 2]
        # 按句子切分，逐句打分，选信息量最大的一句作锚点
        sentences = [s for s in re.split(r"(?<=[。！？\n])", body) if s.strip()]
        if not sentences:
            return body[:width]

        def sent_score(s: str) -> int:
            hit = sum(1 for t in terms if t in s)
            return hit * 10 + min(len(s), 60) // 10

        best_i = max(range(len(sentences)), key=lambda i: sent_score(sentences[i]))
        # 从锚点句子开始向后拼，达到宽度即停
        out = ""
        for s in sentences[best_i:]:
            if len(out) + len(s) > width and out:
                break
            out += s
        out = out.strip()
        if len(out) < 40 and best_i > 0:
            out = sentences[best_i - 1] + out
        if best_i > 0:
            out = "…" + out
        if len(out) > width + 40:
            out = out[:width] + "…"
        return out


class KnowledgeBase:
    """Markdown 知识库，按 ## 小节切块并建立 BM25 索引。"""

    def __init__(self, folder: str, k1: float = 1.35, b: float = 0.72):
        self.folder = folder
        self.k1, self.b = k1, b
        self.chunks: List[Chunk] = []
        self.df: Dict[str, int] = {}
        self.avgdl = 0.0
        self.docs: List[str] = []
        self._build()

    # ---------- 构建 ----------
    def _read_docs(self) -> List[Tuple[str, str]]:
        if not os.path.isdir(self.folder):
            return []
        out: List[Tuple[str, str]] = []
        for name in sorted(os.listdir(self.folder)):
            if not name.lower().endswith(".md"):
                continue
            path = os.path.join(self.folder, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    out.append((name, fh.read()))
            except OSError:
                continue
        return out

    @staticmethod
    def _split_sections(text: str) -> Tuple[str, List[Tuple[str, str]]]:
        """返回 (文档大标题, [(小节标题, 小节正文), ...])"""
        doc_title = ""
        sections: List[Tuple[str, str]] = []
        cur_title = ""
        cur_lines: List[str] = []

        def flush():
            body = "\n".join(cur_lines).strip()
            if body:
                sections.append((cur_title, body))

        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("# ") and not stripped.startswith("## "):
                if not doc_title:
                    doc_title = stripped[2:].strip()
                continue
            if stripped.startswith("## "):
                flush()
                cur_title = stripped[3:].strip()
                cur_lines = []
                continue
            cur_lines.append(line)
        flush()
        self_title = doc_title
        return self_title, sections

    def _build(self) -> None:
        for name, text in self._read_docs():
            doc_title, sections = self._split_sections(text)
            doc_title = doc_title or os.path.splitext(name)[0]
            self.docs.append(doc_title)
            for section, body in sections:
                # 只把小节标题拼进检索文本：文档大标题会把同样的词注入所有小节，
                # 抹掉同文档内的区分度（例如"劳模"会同时命中"人物事迹"和"叙事角度"两节）。
                indexed = f"{section} {body}"
                tf: Dict[str, int] = {}
                toks = tokenize(indexed)
                for t in toks:
                    tf[t] = tf.get(t, 0) + 1
                self.chunks.append(
                    Chunk(doc=doc_title, section=section, text=body,
                          tf=tf, length=len(toks))
                )
        for c in self.chunks:
            for term in c.tf:
                self.df[term] = self.df.get(term, 0) + 1
        self.avgdl = (sum(c.length for c in self.chunks) / len(self.chunks)
                      if self.chunks else 0.0)

    # ---------- 检索 ----------
    def _idf(self, term: str) -> float:
        n = len(self.chunks)
        df = self.df.get(term, 0)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def search(self, query: str, k: int = 3,
               boost_docs: List[str] | None = None,
               boost: float = 1.35) -> List[Tuple[Chunk, float]]:
        """检索。boost_docs 用于按意图收敛检索域（如养护类问题优先作物养护文档）。"""
        if not self.chunks or not query:
            return []
        raw_terms = tokenize(query)
        # 概念组：一个原词连同它的同义写法算"同一个概念"，命中任一即算命中
        concepts: Dict[str, List[str]] = {}
        for t in raw_terms:
            if len(t) < 2:
                continue
            group = [t] + SYNONYMS.get(t, [])
            key = t
            merged = set(concepts.get(key, []))
            for g in group:
                if g in self.df:
                    merged.add(g)
            if merged:
                concepts[key] = sorted(merged)

        q_terms = [t for t in raw_terms if t in self.df]
        for grp in concepts.values():
            q_terms.extend(grp)
        q_terms = list(dict.fromkeys(q_terms))
        if not q_terms:
            return []

        scored: List[Tuple[Chunk, float]] = []
        for c in self.chunks:
            score = 0.0
            for term in q_terms:
                f = c.tf.get(term, 0)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * c.length / (self.avgdl or 1))
                score += self._idf(term) * f * (self.k1 + 1) / denom
            if score <= 0:
                continue
            # 覆盖度重排：命中查询中的不同概念越多，越可能是答案而非"顺带提一句"
            if concepts:
                hit = sum(1 for grp in concepts.values()
                          if any(t in c.tf for t in grp))
                coverage = hit / len(concepts)
            else:
                coverage = 0.5
            score *= 0.45 + 0.55 * coverage
            if boost_docs and any(kw in c.doc for kw in boost_docs):
                score *= boost
            scored.append((c, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[0][1] if scored else 1.0
        return [(c, s / top) for c, s in scored[:k]]

    def stats(self) -> Dict[str, int]:
        return {"documents": len(self.docs), "chunks": len(self.chunks),
                "terms": len(self.df)}


if __name__ == "__main__":
    base = KnowledgeBase(os.path.join(os.path.dirname(__file__), "knowledge"))
    print("知识库统计:", base.stats())
    for q in ["番茄土壤湿度多少合适", "芒种在广东能种什么", "讲一个劳模故事",
              "黄瓜叶子发黄怎么办", "劳动时长怎么认定"]:
        print("\n查询:", q)
        for chunk, score in base.search(q, k=2):
            print(f"  [{score:.2f}] {chunk.label}")
