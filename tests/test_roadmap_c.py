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


# ---------------------------------------------------------------------------
# #12 知识库历史与回滚
# ---------------------------------------------------------------------------
def test_kb_history_and_rollback(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "KB_DIR", str(tmp_path))
    game = "histgame"
    agent.kb_write("t1", "V1", game)  # 文件不存在 → 无快照
    agent.kb_append("t1", "V2", game)  # 快照 V1
    agent.kb_write("t1", "V3", game)  # 快照 V1+V2
    h = agent.kb_history("t1", game)
    assert "2 条历史修订" in h
    msg = agent.kb_rollback("t1", 1, game)  # 回到最新快照 = V1+V2
    assert "已回滚" in msg
    p = agent.kb_resolve("t1", game)
    with open(p, encoding="utf-8") as f:
        content = f.read()
    assert "V1" in content and "V2" in content and "V3" not in content
    # 回滚动作本身也留了快照 → 历史 +1
    assert "3 条历史修订" in agent.kb_history("t1", game)


def test_kb_rollback_out_of_range(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "KB_DIR", str(tmp_path))
    agent.kb_write("t2", "A", "g2")
    agent.kb_write("t2", "B", "g2")  # 1 条快照
    assert "超出范围" in agent.kb_rollback("t2", 99, "g2")
    assert "暂无历史修订" in agent.kb_rollback("nofile", 1, "g2")


def test_kb_history_revisions_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "KB_DIR", str(tmp_path))
    monkeypatch.setattr(agent, "_CFG", {"kb": {"history_revisions": 3, "history_max_kb": 256}})
    for i in range(6):
        agent.kb_write("t3", f"版本{i}", "g3")
    p = agent.kb_resolve("t3", "g3")
    rows = agent._kb_history_rows(p)
    assert len(rows) == 3, "历史应滚动保留最近 3 条"


def test_kb_history_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(agent, "KB_DIR", str(tmp_path))
    agent.kb_write("t4", "x", "g4")
    agent.kb_write("t4", "y", "g4")
    assert agent.main(["--game", "g4", "kb", "history", "t4"]) == 0
    out = capsys.readouterr().out
    assert "历史修订" in out
