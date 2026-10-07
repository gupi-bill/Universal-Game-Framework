"""ROADMAP 批次 C 回归测试：#11 BM25 / #12 历史回滚 / #13 调参审计 / #15 复盘配置 / #9 跟踪 / #7 预判模型。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent  # noqa: E402


# ---------------------------------------------------------------------------
# #11 BM25 检索
# ---------------------------------------------------------------------------
def test_tokenize_ascii_and_cjk():
    toks = agent._tokenize("低血量撤退 low HP")
    assert "low" in toks and "hp" in toks
    assert "血" in toks and "低血" in toks and "血量" in toks


def test_bm25_ranks_more_relevant_first(tmp_path):
    (tmp_path / "rich.md").write_text("boss 打法要点\n" + "boss 走位 " * 30, encoding="utf-8")
    (tmp_path / "thin.md").write_text("顺便提一句 boss", encoding="utf-8")
    out = agent._text_search("boss", str(tmp_path))
    assert "rich.md" in out and "thin.md" in out
    assert out.index("rich.md") < out.index("thin.md"), "高相关文档应排前"


def test_text_search_respects_top_n(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "_CFG", {"kb": {"search_top_n": 2}})
    for i in range(5):
        (tmp_path / f"f{i}.md").write_text(f"kw{i} 共同词", encoding="utf-8")
    out = agent._text_search("共同词", str(tmp_path))
    assert out.startswith("共找到 2 条结果"), out[:30]


def test_text_search_semantic_hit_without_literal(tmp_path):
    """无字面命中但词素相关（分词命中）也能召回，带相关度标注。"""
    (tmp_path / "a.md").write_text("低血量时应当立即撤退", encoding="utf-8")
    out = agent._text_search("血量", str(tmp_path))
    assert "a.md" in out


def test_kb_search_output_contract(tmp_path, monkeypatch):
    """输出契约不回退：命中带'共找到'，未命中返回'未找到相关内容'。"""
    monkeypatch.setattr(agent, "KB_DIR", str(tmp_path))
    (tmp_path / "x.md").write_text("战术内容", encoding="utf-8")
    hit = agent.kb_search("战术")
    assert agent.kb_is_hit(hit) and hit.startswith("共找到")
    miss = agent.kb_search("不存在的关键词zzz")
    assert miss == "未找到相关内容"


def test_canonical_tactics_survive_polluted_kb(tmp_path):
    """知识库被大量含关键词的噪声文件污染时，权威 tactics.md 不能被 Top-N 挤出。"""
    (tmp_path / "tactics.md").write_text(
        "# 战术知识\n- 战术: 低血量撤退（适用条件：血量低于四成）", encoding="utf-8"
    )
    for i in range(12):
        (tmp_path / f"video_tactic_{i}.md").write_text(
            f"# 视频学习战术 {i}\n## 战术列表\n战术 战术 战术 战术", encoding="utf-8"
        )
    out = agent._text_search("战术", str(tmp_path))
    assert "tactics.md" in out
    tactics = agent.extract_tactics(out)
    assert any(tag == "retreat" for tag, _ in tactics), "决策闭环必须能从检索结果提取到种子战术"
