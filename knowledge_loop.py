"""知识闭环（S18）：学 → 检索 → 决策 → 复盘 → 回写。

本模块把"越打越强"从文档措辞变成可复现、可量化的闭环，由四部分组成：

1. ``seed_knowledge(game)``  —— 按游戏档案生成 seed 知识（学），落在
   ``knowledge_md/<game>/`` 下，保证检索能命中本游戏而非根目录旧模板。
2. ``retrieve(game, keyword)`` —— 检索并**同步记账**（检索次数 / 命中次数）。
3. ``extract_tactics`` / ``apply_tactics`` —— 把命中的条目翻译成**决策可用的规则**，
   使无 LLM 的兜底路径也能被知识影响（此前兜底完全忽略 kb_tactics，闭环断开）。
4. ``LearningStats`` + ``save/query_stats`` —— 结构化指标，持久化到
   ``run_logs/learning_stats.json``，可按游戏查询命中率 / 引用率。

设计约束（本机环境）：无网络、无 LLM/VLM 密钥，因此本模块全部为**纯本地文件与
内存操作**，不依赖任何外部服务；检索只走文本匹配，不引入 chromadb 等重依赖。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime

import yaml

import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KB_DIR = os.path.join(BASE_DIR, config.get("paths.knowledge_md", "knowledge_md"))
RUN_LOG_DIR = os.path.join(BASE_DIR, config.get("paths.run_logs", "run_logs"))
STATS_PATH = os.path.join(RUN_LOG_DIR, "learning_stats.json")

# 检索"未命中"的判定词（与 mcp_server._text_search 的返回口径保持一致）
MISS_TOKENS = ("未找到", "没有找到", "无相关", "未找到相关内容")

# 决策规则：命中知识 → 动作倾向。键是规则名，值是触发词。
# 词表来自 space_invaders / florr 两份档案的 combat.tactics 实测文本，
# 不是凭空想的。v2.0 P2 之前这里只有 4 个词组，结果档案里写的
# 「优先清除」「横向机动」「降低风险」一个都匹配不上 ——
# 知识入库了，但检索侧根本认不出来。
TACTIC_RULES = {
    "retreat": (
        "撤退", "避战", "不要恋战", "低血量", "放弃", "判负",
        "脱离", "撤", "降低风险", "规避",
    ),
    "keep_distance": (
        "保持距离", "拉开距离", "远离", "安全距离", "横向机动",
        "机动", "转移", "不站桩", "绕", "拉开",
    ),
    "focus_fire": (
        "集火", "优先攻击", "优先击杀", "优先集火", "优先清除",
        "主动", "清底排", "清", "切换目标", "输出",
    ),
    "protect_ally": (
        "保护队友", "队友", "team", "配合", "分工", "抱团",
    ),
}

# ---------------------------------------------------------------------------
# v2.0 P2：战术标签的「适用条件」
# ---------------------------------------------------------------------------
# A/B 实验（devplan/P2_AB_EXPERIMENT.md）证伪了「知识闭环有增益」：
# 知识库里的 retreat / keep_distance 让决策少活 5 回合。逐条探针显示，
# 这两条标签在 arena 场景里**无适用面** —— 知识库存的是战术名词，
# 不是「对这一局面真的有用」的经验。
#
# 根因在入库侧：build_seed_docs 生成的是「- 战术: <一句话>」，
# 没有任何适用条件。extract_tactics 只能靠关键词猜，
# 于是「撤退」二字出现在任何文件里就抽出 retreat 标签。
#
# 修法：条目必须声明「何时适用」，检索时逐条校验。
# 关键词命中但没有适用条件的条目 **不产生标签** ——
# 宁可漏掉一条可能有用的经验，也不要用一条不适用的经验去改坏决策。

#: 声明适用条件的引导词。条目里出现其一即视为「声明了适用面」。
CONDITION_MARKERS = ("适用", "条件", "当", "若", "时", "情况")

#: 条目里出现这些词，说明它写的是通用名词而非具体局面
GENERIC_MARKERS = ("不要恋战", "优先集火", "主动", "团队", "配合")


#: 条件短语 → 可判定谓词。键是条件里出现的特征词。
#: 决策层拿当前局面来问"这条知识现在适不适用"。
# 所有谓词统一用 **ctx 签名 —— condition_matches 一律 pred(**ctx) 调用。
# 之前「血量低于四成」写成 lambda hp_ratio: ...，用 **ctx 传进去会抛
# TypeError，被 `except TypeError: return False` 吞掉 ——
# 于是「低血量」这条条件**永远不匹配**，知识永远不生效，
# 而报错被静默吃掉，查起来极难。
# 教训：不要用 except TypeError 兜底来掩盖签名不匹配，
# 统一签名 + 让签名错误立刻暴露。
def _low_hp(hp_ratio=1.0, **_):
    return hp_ratio < 0.4


def _high_threat(threat_ratio=0.0, **_):
    return threat_ratio is not None and threat_ratio >= 1.0


def _few_enemies(n_enemies=0, **_):
    return (n_enemies or 0) <= 3


def _has_allies(has_allies=False, **_):
    return bool(has_allies)


def _losing(decision="", **_):
    return decision in ("retreat", "cautious_fight")


CONDITION_PREDICATES = {
    "低血量": _low_hp,
    "血量低于四成": _low_hp,
    "最高威胁": _high_threat,
    "存在高威胁目标": _high_threat,
    "敌方数量不超过三个": _few_enemies,
    "队友": _has_allies,
    "战斗评估判为劣势": _losing,
}


def parse_condition(line: str) -> dict:
    """从一条战术里解析出它的适用条件。

    返回 ``{"tag": 名称, "conditions": [...]}`` 或 ``{}``。
    conditions 里每项是一个可判定谓词的名字，由 ``condition_matches``
    在决策时拿当前局面去问。
    """
    if not _entry_states_condition(line):
        return {}
    raw = str(line).lower()
    # 标签只看「战术正文」，不能看条件部分 ——
    # 条件里出现「血量低于四成」会让 retreat 误命中（那说的是触发时机，不是战术本身）。
    body, _, cond_part = raw.partition("适用条件")
    if not cond_part:
        body, _, cond_part = raw.partition("条件：")
    # 视频学习 / 复盘产出的自由文本往往把条件内嵌在战术句子里，
    # 例如「与高威胁目标保持距离，低血量立即撤退」——
    # 没有「适用条件：」前缀，但「低血量」本身就是条件词。
    # 所以条件搜索范围是「括号之后 + 整行」，而不是只看 cond_part。
    cond_scope = cond_part if cond_part else raw
    tag = None
    for name, words in TACTIC_RULES.items():
        if any(w in body for w in words):
            tag = name
            break
    if tag is None:
        return {}
    conds = [c for c in CONDITION_PREDICATES if c in cond_scope]
    # 自由文本里常见的口语化条件，补上映射
    for phrase, key in (("低血量", "低血量"), ("血量不足", "低血量"),
                        ("被夹击", "战斗评估判为劣势"), ("残兵", "战斗评估判为劣势")):
        if phrase in cond_scope and key not in conds:
            conds.append(key)
    # 有条件引导词但一个可判定谓词都解析不出来 → 视为「无条件」，
    # 因为决策层无法判断它何时该生效。
    if not conds:
        return {}
    return {"tag": tag, "conditions": conds}


def condition_matches(conds: list, hp_ratio: float = 1.0, threat_ratio: float = 0.0,
                      n_enemies: int = 0, has_allies: bool = False,
                      decision: str = "") -> bool:
    """当前局面是否满足这条知识的全部适用条件。

    多个条件取「与」—— 条件写「适用条件：敌方数量不超过三个时」
    只有一个谓词，多条件条目要求同时满足。
    谓词全部认不出来时保守返回 False（不生效）。
    """
    if not conds:
        return False
    ctx = dict(hp_ratio=hp_ratio, threat_ratio=threat_ratio,
               n_enemies=n_enemies, has_allies=has_allies, decision=decision)
    checks = []
    for c in conds:
        pred = CONDITION_PREDICATES.get(c)
        if pred is None:
            return False      # 认不出的条件 → 不生效
        # 签名不匹配属于代码 bug，必须暴露，不能静默当成"条件不满足"。
        # （之前正是这里 return False 把「血量低于四成」永远禁用了。）
        checks.append(bool(pred(**ctx)))
    return all(checks)


#: 出现在句子里就说明「这条战术自带触发条件」的短语。
#: 视频学习与人工复盘很少写成「适用条件：xxx」的标准格式，
#: 更常见的是「低血量立即撤退」「残兵时降低风险」这种内嵌写法。
EMBEDDED_CONDITIONS = ("低血量", "血量不足", "血量低于", "被夹击", "残兵",
                       "残兵", "高威胁", "被压制", "血量低")


def _entry_states_condition(line: str) -> bool:
    """判断单条战术是否声明了适用条件。

    两种写法都算：
      1. 标准格式：含「适用 / 条件 / 当 / 若 / 时 / 情况」等引导词
      2. 内嵌格式：句子自带触发短语（「低血量立即撤退」）

    另外排除纯通用名词 —— 「当情况允许时积极主动」这种空话不算有条件。
    """
    low = str(line or "").lower()
    has_marker = any(m in low for m in CONDITION_MARKERS)
    has_embedded = any(m in low for m in EMBEDDED_CONDITIONS)
    if not (has_marker or has_embedded):
        return False
    # 通用名词即便带了"当…时"也多半是空话，除非还有具体量词
    if any(g in low for g in GENERIC_MARKERS):
        # 带数字/百分比/明确数量时视为具体条件
        import re as _re
        if not _re.search(r"\d", low):
            return False
    return True

_LOCK = threading.Lock()
_STATS: dict = {}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _safe_name(name: str) -> str:
    """把任意游戏名清洗成合法目录名（空/非法一律落到 default）。"""
    s = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or "")).strip("_")
    return s or "default"


def game_dir(game: str = None) -> str:
    """该游戏的知识目录 ``knowledge_md/<game>/``。"""
    return os.path.join(KB_DIR, _safe_name(game or config.active_game()))


def _load_profile(game: str) -> dict:
    """读取游戏档案（缺文件/解析失败一律返回空 dict，不抛异常）。"""
    path = os.path.join(config.PROFILE_DIR, f"{_safe_name(game)}.yaml")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 1. 学：seed 知识生成
# ---------------------------------------------------------------------------
def _with_condition(tactic: str) -> str:
    """给一句战术补上适用条件。

    档案里写的是通用战术（"保持距离"），对决策层来说信息量为零 ——
    任何局面都适用 = 任何局面都不该由它改决策。

    这里按战术的关键词补一个可判定的适用面：
      - 撤退类    → 适用于血量低于四成或被最高威胁锁定时
      - 距离类    → 适用于敌方数量不超过三个时（被围住时拉开距离反而撞进敌群）
      - 集火类    → 适用于场上存在高威胁目标时
      - 队友类    → 适用于组队对局时
    """
    t = str(tactic or "")
    low = t.lower()
    if any(w in low for w in TACTIC_RULES["retreat"]):
        cond = "适用条件：血量低于四成，或被最高威胁实体锁定时"
    elif any(w in low for w in TACTIC_RULES["keep_distance"]):
        cond = "适用条件：敌方数量不超过三个时"
    elif any(w in low for w in TACTIC_RULES["focus_fire"]):
        cond = "适用条件：场上存在高威胁目标时"
    elif any(w in low for w in TACTIC_RULES["protect_ally"]):
        cond = "适用条件：组队对局且队友血量偏低时"
    else:
        cond = "适用条件：该局面已由战斗评估判为劣势时"
    return f"{t}（{cond}）"


def build_seed_docs(game: str = None) -> dict:
    """按游戏档案生成 seed 文档内容 ``{文件名(不含扩展名): markdown}``。

    至少产出 ``tactics``（含"战术"关键词）与 ``boss_guide``（含 boss 关键词），
    与 agent_main 主循环的两类检索词（"战术" / "boss"）对齐。
    """
    game = game or config.active_game()
    prof = _load_profile(game)
    combat = (prof.get("combat") or {}) if isinstance(prof, dict) else {}
    pred = (prof.get("predictor") or {}) if isinstance(prof, dict) else {}
    desc = ""
    if isinstance(prof.get("game"), dict):
        desc = (prof["game"].get("description") or "").strip()

    tactics = [str(t) for t in (combat.get("tactics") or []) if str(t).strip()]
    if not tactics:
        tactics = [
            "血量不足且附近存在高威胁实体时立即撤退，不要恋战",
            "与高威胁实体保持距离，等其转移后再回场",
            "低威胁目标主动集火清理，保持场面干净",
        ]
    # v2.0 P2：战术条目必须声明适用条件，否则 extract_tactics 会拒绝它。
    # 「战术: <一句话>」这种无条件写法已被证明会让决策变差（-5 回合），
    # 见 devplan/P2_AB_EXPERIMENT.md。
    lines = "\n".join(f"- 战术: {_with_condition(t)}" for t in tactics)

    # 最高威胁实体：highest_boss 档优先，其次 boss 档
    top = []
    for key in ("rarity_highest_boss", "rarity_boss", "rarity_elite"):
        vals = pred.get(key) or []
        if isinstance(vals, str):
            vals = [vals]
        top = [str(v) for v in vals if str(v).strip()]
        if top:
            break
    top_txt = "、".join(top) if top else "未声明（档案 predictor 段缺失）"

    tactics_md = (
        f"# {game} 战术知识（seed）\n\n"
        f"{desc}\n\n"
        "## 战术条目\n"
        f"{lines}\n\n"
        "## 使用说明\n"
        "- 本文件由 knowledge_loop.seed_knowledge 依据游戏档案 combat.tactics 生成\n"
        "- 决策前检索「战术」应命中本文件，命中条目会进入兜底决策规则\n"
    )

    boss_md = (
        f"# {game} 高威胁目标指南（seed）\n\n"
        f"## 最高威胁实体（boss 档）\n- {top_txt}\n\n"
        "## 应对原则\n"
        "- 最高威胁目标出现时优先判断打/跑；血量不足立即撤退并拉开距离\n"
        "- 中低威胁目标可在保持距离的前提下集火清理\n"
        "- 组队时优先保护队友输出位\n"
    )
    return {"tactics": tactics_md, "boss_guide": boss_md}


def seed_knowledge(game: str = None, force: bool = False) -> list:
    """把 seed 知识写入 ``knowledge_md/<game>/``，返回实际写入的文件路径列表。

    ``force=False``（默认）时不覆盖已存在文档，避免冲掉复盘等真实积累。
    """
    game = game or config.active_game()
    d = game_dir(game)
    os.makedirs(d, exist_ok=True)
    written = []
    for fname, content in build_seed_docs(game).items():
        fp = os.path.join(d, f"{fname}.md")
        if os.path.exists(fp) and not force:
            continue
        with open(fp, "w", encoding="utf-8") as f:
            f.write(content)
        written.append(fp)
    return written


# ---------------------------------------------------------------------------
# 2. 检索 + 记账
# ---------------------------------------------------------------------------
def _text_search(keyword: str, search_base: str) -> str:
    """本地文本检索，输出口径与 mcp_server._text_search 一致。"""
    if not os.path.isdir(search_base):
        return "未找到相关内容"
    results = []
    kw = str(keyword or "").lower()
    for root, _dirs, files in os.walk(search_base):
        for fname in sorted(f for f in files if f.endswith(".md")):
            fp = os.path.join(root, fname)
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue
            if kw in content.lower():
                results.append(f"## {os.path.relpath(fp, search_base)}\n{content[:2000]}")
    if not results:
        return "未找到相关内容"
    return f"共找到 {len(results)} 条结果:\n" + "\n\n---\n\n".join(results)


def is_hit(text: str) -> bool:
    """检索结果是否命中（非空且不含未命中提示）。"""
    if not text:
        return False
    low = str(text).lower()
    return not any(tok in low for tok in MISS_TOKENS)


def retrieve(game: str, keyword: str, stats: "LearningStats" = None,
             base_dir: str = None) -> tuple:
    """检索该游戏知识并记账，返回 ``(文本, 是否命中)``。"""
    base = base_dir or game_dir(game)
    text = _text_search(keyword, base)
    hit = is_hit(text)
    (stats or get_stats(game)).record_search(keyword, hit)
    return text, hit


# ---------------------------------------------------------------------------
# 3. 命中条目 → 决策规则
# ---------------------------------------------------------------------------
def extract_tactics(text: str) -> list:
    """从检索结果中抽取**声明了适用条件**的战术规则名。

    v2.0 P2 之前，这里是纯关键词匹配：文档里出现「撤退」二字就抽出
    ``retreat``。A/B 实验证明这会让决策变差（-5 回合），
    因为知识库存的是战术名词而非「对这一局面真的有用」的经验。

    现在逐行判定：关键词命中 **且** 该行声明了可判定的适用条件，
    才产生 ``(标签, 条件元组)``。没有适用条件的条目被跳过 —— 宁可漏，不可错。

    返回值从 ``["retreat", ...]`` 变成 ``[("retreat", ("战斗评估判为劣势",)), ...]``，
    因为条件必须一路带到决策层才能校验 —— 只传标签的话，
    「何时用」就丢了，知识又会退化成无条件覆盖。

    副作用（有意为之）：旧的 seed 文档（`- 战术: xxx` 无条件）会抽不出
    任何标签。build_seed_docs 已同步改成生成带条件的格式。
    """
    if not is_hit(text):
        return []
    out: list = []
    seen = set()
    for line in str(text).splitlines():
        parsed = parse_condition(line)
        if not parsed:
            continue
        tag, conds = parsed["tag"], tuple(parsed["conditions"])
        if tag in seen:
            continue
        seen.add(tag)
        out.append((tag, conds))
    return out


def apply_tactics(decision: str, tactics: list, has_allies: bool = False,
                  hp_ratio: float = 1.0, threat_ratio: float = 0.0,
                  n_enemies: int = 0, **ctx) -> str:
    """知识驱动的兜底决策：返回受知识影响的动作，无影响时返回空串。

    这是"命中条目真的进入决策"的落点 —— 没有它，无 LLM 时知识只进 prompt 不进动作。

    v2.0 P2 实证修正 —— A/B 实验（30 组 × 200 回合）发现知识组比无知识组
    **少活 8 回合**（67 vs 75）。逐回合统计定位到两处语义错误：

      1. `retreat` 被无条件改写成 `defend`。但撤退与防守在这个动作空间里
         不是一回事：撤退是脱离接触，防守是原地蹲守。面对围攻时"蹲守"
         只会挨打，知识反而害了决策。
      2. `protect_ally`（保护队友）在**没有队友**的场景也生效。
         单机局里检索到的组队经验属于知识串味，不该影响决策。

    现在：撤退类知识只影响"谨慎战斗"，不再把撤退本身变成防守；
    队友类知识（protect_ally / focus_fire）需要 `has_allies=True` 才生效。

    参数 `has_allies` 缺省 False，即"拿不准就当没有队友"——
    错误的加成（无谓的跑路）代价高于漏掉的加成。
    """
    if not tactics:
        return ""
    t = set()
    for item in tactics:
        if isinstance(item, (tuple, list)) and item:
            tag = item[0]
            conds = list(item[1]) if len(item) > 1 and isinstance(item[1], (list, tuple)) else []
            # 条件不满足就不参与 —— 这是 v2.0 P2 的关键一环：
            # 知识写了「何时用」，不等于「现在就该用」。
            if not condition_matches(conds, hp_ratio=hp_ratio,
                                     threat_ratio=threat_ratio,
                                     n_enemies=n_enemies,
                                     has_allies=has_allies,
                                     decision=decision, **ctx):
                continue
            t.add(tag)
        else:
            t.add(str(item))
    if not t:
        return ""
    # 队友类标签只在真有队友时生效，避免单机局被组队经验误导
    ally_tags = (t & {"protect_ally", "focus_fire"}) if has_allies else set()

    if decision == "cautious_fight":
        # 谨慎时若知识里有「保持距离 / 撤退」，转为明确防守动作
        if t & {"retreat", "keep_distance"}:
            return "defend"
    if decision == "fight":
        # 只有在确实有队友时才谈集火与保护
        if ally_tags:
            return "attack"
    return ""


# v2.0 S20：S18 实测暴露「知识优先级过硬」—— 空场也一路防守。知识应当是
# **局面相关的加权意见**，而不是无条件覆盖战斗评估的硬规则。
KB_GATE_HP = 0.6        # 血量低于此比例时，撤退/保持距离类知识才生效
KB_GATE_THREAT = 1.0    # 威胁比高于此值时，谨慎类知识生效


def knowledge_gate(decision: str, tactics: list, hp_ratio: float = 1.0,
                   threat_ratio: float = 0.0) -> bool:
    """判断知识此刻是否应当影响决策。

    - 场上无威胁（threat_ratio<=0）→ 不生效：空场不该因为"知识里写了撤退"就一直防守
    - retreat            → 生效：知识强化逃生
    - cautious_fight     → 仅在低血量或高威胁时生效
    - fight              → 仅在局面占优时生效（知识指导集火）
    """
    if not tactics:
        return False
    threat_ratio = float(threat_ratio or 0.0)
    if threat_ratio <= 0:
        return False
    if decision == "retreat":
        return True
    if decision == "cautious_fight":
        return float(hp_ratio or 1.0) < KB_GATE_HP or threat_ratio > KB_GATE_THREAT
    if decision == "fight":
        return threat_ratio < KB_GATE_THREAT
    return False


def decide_action(decision: str, tactics: list, hp_ratio: float = 1.0,
                  threat_ratio: float = 0.0, has_allies: bool = False,
                  n_enemies: int = 0) -> str:
    """闸门 + 规则映射：返回受知识影响的动作，未通过闸门返回空串。

    注意必须把局面参数**继续传下去**给 apply_tactics ——
    v2.0 P2 引入「适用条件」后，条件校验发生在 apply_tactics 里。
    早先这里只传 has_allies，导致 hp_ratio / threat_ratio / n_enemies 丢失，
    带条件的知识永远匹配不上（表现为「知识检索到了但不起作用」）。
    """
    if not knowledge_gate(decision, tactics, hp_ratio, threat_ratio):
        return ""
    return apply_tactics(decision, tactics, has_allies=has_allies,
                         hp_ratio=hp_ratio, threat_ratio=threat_ratio,
                         n_enemies=n_enemies)


def decide_with_knowledge(decision: str, kb_text: str, stats: "LearningStats" = None,
                          keyword: str = "", game: str = None,
                          has_allies: bool = False) -> tuple:
    """``(动作, 是否引用了知识)``：命中且产生规则才算一次有效引用。"""
    tactics = extract_tactics(kb_text)
    action = apply_tactics(decision, tactics, has_allies=has_allies)
    cited = bool(action) and bool(tactics)
    if cited and stats is not None:
        stats.record_citation(keyword or "-")
    return action, cited


# ---------------------------------------------------------------------------
# 4. 指标：LearningStats
# ---------------------------------------------------------------------------
class LearningStats:
    """检索 / 命中 / 引用三计数器，支持按关键词下钻与持久化。"""

    def __init__(self, game: str = "", searches: int = 0, hits: int = 0,
                 citations: int = 0, by_keyword: dict = None, updated_at: str = ""):
        self.game = game or ""
        self.searches = int(searches)
        self.hits = int(hits)
        self.citations = int(citations)
        self.by_keyword = {k: list(v) for k, v in (by_keyword or {}).items()}
        self.updated_at = updated_at or ""

    # -- 记账 -------------------------------------------------------------
    def record_search(self, keyword: str, hit: bool) -> None:
        hit = bool(hit)
        self.searches += 1
        self.hits += int(hit)
        col = self._col(keyword)
        col[0] += 1
        col[1] += int(hit)
        self._touch()

    def record_citation(self, keyword: str = "-") -> None:
        self.citations += 1
        self._col(keyword)[2] += 1
        self._touch()

    def _col(self, keyword: str) -> list:
        """关键词下钻列 ``[检索, 命中, 引用]``。"""
        return self.by_keyword.setdefault(str(keyword or "-"), [0, 0, 0])

    def _touch(self) -> None:
        self.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # -- 指标 -------------------------------------------------------------
    @property
    def hit_rate(self) -> float:
        return round(self.hits / self.searches, 4) if self.searches else 0.0

    @property
    def citation_rate(self) -> float:
        return round(self.citations / self.hits, 4) if self.hits else 0.0

    def to_dict(self) -> dict:
        return {
            "game": self.game,
            "searches": self.searches,
            "hits": self.hits,
            "citations": self.citations,
            "hit_rate": self.hit_rate,
            "citation_rate": self.citation_rate,
            "by_keyword": {k: list(v) for k, v in self.by_keyword.items()},
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LearningStats":
        d = d or {}
        return cls(
            game=d.get("game", ""),
            searches=d.get("searches", 0),
            hits=d.get("hits", 0),
            citations=d.get("citations", 0),
            by_keyword=d.get("by_keyword") or {},
            updated_at=d.get("updated_at", ""),
        )

    def __repr__(self):  # pragma: no cover - 调试用
        return (f"<LearningStats {self.game} s={self.searches} h={self.hits} "
                f"c={self.citations} hit={self.hit_rate} cit={self.citation_rate}>")


# ---------------------------------------------------------------------------
# 注册表与持久化
# ---------------------------------------------------------------------------
def get_stats(game: str = None) -> LearningStats:
    """取（或建）该游戏的进程内计数器。"""
    g = _safe_name(game or config.active_game())
    with _LOCK:
        if g not in _STATS:
            _STATS[g] = LearningStats(game=g)
        return _STATS[g]


def reset_stats(game: str = None) -> None:
    """清空计数器（None = 全部）。测试用。"""
    with _LOCK:
        if game is None:
            _STATS.clear()
        else:
            _STATS.pop(_safe_name(game), None)


def save(path: str = None) -> str:
    """把全部游戏计数落盘为 JSON，返回路径。"""
    p = path or STATS_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with _LOCK:
        payload = {
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "games": {g: s.to_dict() for g, s in _STATS.items()},
        }
    tmp = f"{p}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return p


def read_stats(path: str = None) -> dict:
    """读盘（文件不存在返回空结构）。"""
    p = path or STATS_PATH
    if not os.path.exists(p):
        return {"updated_at": "", "games": {}}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"updated_at": "", "games": {}}
    if not isinstance(data, dict):
        return {"updated_at": "", "games": {}}
    data.setdefault("games", {})
    return data


def query_stats(game: str = None, path: str = None) -> dict:
    """查询指标：``game=None`` 时额外给出 overall 聚合。

    优先返回进程内实时值（未落盘也算），盘上其它游戏一并合并。
    """
    data = read_stats(path)
    games = {}
    for g, d in (data.get("games") or {}).items():
        st = LearningStats.from_dict(d)
        games[g] = st
    with _LOCK:
        for g, st in _STATS.items():
            # 进程内计数覆盖盘上同名词条（实时优先）
            games[g] = LearningStats(
                game=g, searches=st.searches, hits=st.hits, citations=st.citations,
                by_keyword=st.by_keyword, updated_at=st.updated_at)

    if game:
        st = games.get(_safe_name(game))
        return st.to_dict() if st else {
            "game": game, "searches": 0, "hits": 0, "citations": 0,
            "hit_rate": 0.0, "citation_rate": 0.0, "by_keyword": {}, "updated_at": ""}

    total = LearningStats(game="__all__")
    for st in games.values():
        total.searches += st.searches
        total.hits += st.hits
        total.citations += st.citations
        for k, col in st.by_keyword.items():
            dst = total.by_keyword.setdefault(k, [0, 0, 0])
            for i in range(3):
                dst[i] += col[i]
    return {
        "games": {g: s.to_dict() for g, s in games.items()},
        "overall": {
            "games": len(games),
            "searches": total.searches,
            "hits": total.hits,
            "citations": total.citations,
            "hit_rate": total.hit_rate,
            "citation_rate": total.citation_rate,
        },
        "updated_at": data.get("updated_at", ""),
    }


def stats_summary(game: str = None) -> str:
    """人类可读的一行摘要（给 CLI / 日志用）。"""
    q = query_stats(game)
    if game:
        return (f"[{game}] 检索 {q['searches']} 次 / 命中 {q['hits']} 次 "
                f"(命中率 {q['hit_rate']:.0%}) / 引用 {q['citations']} 次 "
                f"(引用率 {q['citation_rate']:.0%})")
    ov = q["overall"]
    return (f"[全部 {ov['games']} 款游戏] 检索 {ov['searches']} 次 / 命中 {ov['hits']} 次 "
            f"(命中率 {ov['hit_rate']:.0%}) / 引用 {ov['citations']} 次 "
            f"(引用率 {ov['citation_rate']:.0%})")
