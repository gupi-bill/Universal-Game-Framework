<div align="center">

# 🎮 Universal-Game-Framework

**A single-file game agent that runs itself**

See the screen → predict motion → evaluate combat → act → consult/write experience → review → report → learn. All in one `agent.py`.

[![CI](https://github.com/gupi-bill/Universal-Game-Framework/actions/workflows/ci.yml/badge.svg)](https://github.com/gupi-bill/Universal-Game-Framework/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![Coverage](https://img.shields.io/badge/coverage-72%25-green)](.github/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-blue)](./LICENSE)

**[中文 README](./README.md)** | English

> ⚠️ **Disclaimer**: For local AI-agent research only. Running bots on official florr.io servers violates the game's ToS and may get your account banned. Use on local / self-hosted / authorized environments only.

</div>

---

## Quick start

```bash
git clone https://github.com/gupi-bill/Universal-Game-Framework.git
cd Universal-Game-Framework

# Offline dry run — no keyboard/mouse control, no API keys, zero third-party deps
python agent.py selftest                 # end-to-end self check, expect 19/19
python agent.py run --dry-run --rounds 20

# Optional: full experience
pip install -r requirements.txt          # pyyaml + requests
```

Prefer a single downloadable file? Grab `ugf.pyz` from [Releases](https://github.com/gupi-bill/Universal-Game-Framework/releases) (or run `python build.py pyz`) — any bare Python 3.10+ runs it:

```bash
python ugf.pyz selftest
```

Or install as a package: `pip install .` → `ugf run --dry-run --rounds 20`.

## What it does

| Capability | How |
|---|---|
| 🎬 Video learning | Extract frames → perceptual-hash dedup → VLM tactic extraction → multi-frame voting → knowledge base |
| 🔮 Motion prediction | 3 models (linear / constant-acceleration / circular) with auto-selection by backtested residual; per-entity confidence |
| ⚔️ Combat evaluation | Threat pyramid + power ratio → fight / cautious / retreat, with mindsets and team coordination |
| 🧠 Knowledge loop | Markdown knowledge base with BM25 ranking; tactics carry **applicability conditions** and only affect decisions when conditions match |
| 🕹️ Human-like input | Movement jitter, random pauses, safe-zone clamping, corner-pause failsafe |
| 📝 Death review | Configurable triggers/templates; structured fields for death-cause statistics |
| 🎚️ Auto-tuning | Adjusts thresholds from combat stats, with audit trail and manual locks |
| 📊 Reporting & panel | Markdown reports, webhook push, JSONL event stream, optional stdlib-only web panel |
| 🎮 Game profiles | One YAML per game — swap games without touching the core |

## Perception backends

| Backend | Use case | Extra deps |
|---|---|---|
| `mock` | Offline synthetic scenes (default for dry-run) | none |
| `http` | External detection service returning the standard payload | requests |
| `local` | On-device inference with a YOLOv8-exported `.onnx` | mss, numpy, onnxruntime, opencv |
| `template` | OpenCV template matching — zero-model lightweight option | mss, numpy, opencv |

Set via `UGF_PERCEPTION_BACKEND` or `perception.backend` in config. All backends return the same payload contract:

```json
{"player": {"alive": true, "hp": 100, "max_hp": 100, "x": 960, "y": 540, "power_score": 120},
 "entities": [{"raw_id": "hornet", "rarity": "Common", "x": 400, "y": 300}],
 "teammates": [], "afk_popup": false}
```

## Commands

```
run        main loop (--rounds N / --interval S / --game NAME / --dry-run)
mode       show runtime mode (dry-run? backend? LLM? current game?)
selftest   offline end-to-end self check (19 assertions)
perceive / predict    manual single-frame inspection
action / set / afk    manual action, loadout switch, AFK-popup guidance
kb         knowledge base: list/search/write/append/export/import/boss/tactic/history/rollback/clean/maintain
learn      video learning: learn video.mp4  or  learn --url <link>
report / session / tune / logs / panel / profile-check / guide
```

Full manual: `python agent.py guide`.

## LLM brain (optional)

Any OpenAI-compatible endpoint via `.env`:

```
LLM_API_URL=... LLM_API_KEY=... LLM_MODEL=...
```

LLM output goes through JSON-schema validation with one automatic repair retry, then falls back to the rule-based engine — every action carries a `source` tag (`llm` / `kb` / `rule`). Without keys, the rule engine + knowledge loop still runs the full pipeline.

## Architecture

Single file, 17 logical sections, four TypedDict contracts (`FramePayload` / `EntityPred` / `CombatEval` / `ActionDict`), typed decision chain with mypy clean. Config merges in four levels with hot reload:

```
built-in DEFAULT < config.yaml < game_profiles/<game>.yaml < tuned_overrides.yaml
```

Details: [docs/architecture.md](docs/architecture.md) (zh) · Add a new game: [docs/game-profile-guide.md](docs/game-profile-guide.md) (zh) · [CHANGELOG](CHANGELOG.md) · [ROADMAP](ROADMAP.md)

## Engineering

- **Tests**: 116 (unit + fault-injection + e2e), coverage floor 70% enforced in CI
- **CI**: pytest on Python 3.11/3.13 · ruff check+format (blocking) · mypy (non-blocking, 0 errors) · profile-check · secret scan · Docker build smoke · zipapp e2e
- **Distribution**: `pip install .` (console script `ugf`) · `dist/ugf.pyz` zipapp · multi-stage Docker image (non-root, healthcheck, compose)
- **Runtime data**: knowledge base / logs / state live in `UGF_HOME` (default: repo dir, or `~/.ugf` when installed)

## Security & boundaries

- `UGF_DRY_RUN=1` logs intended actions without touching keyboard/mouse
- Real-input mode declares Windows DPI awareness and scales coordinates from the perception source resolution
- Mouse-to-corner failsafe pauses the agent instantly
- Secret scanner (`tools/secret_scan.py`) runs in pre-commit and CI
- API keys stay in your `.env` (gitignored) or environment — never in the repo or binaries

## License

[MIT](./LICENSE)
