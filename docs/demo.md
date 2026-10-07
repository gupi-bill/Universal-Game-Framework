# 5 分钟 Demo 走查

> 照做必成功的一条动线，全程离线（不碰键鼠、不需要任何密钥）。
> 一键版：`bash tools/demo.sh`（幂等，可重复跑）。

## 1. 自检（环境是否健康）

```bash
python agent.py selftest
```
预期：逐项 ✓，最后 `自检结果：19/19 通过`。

## 2. 环境体检

```bash
python agent.py doctor
```
预期：Python ✓、依赖清单（缺的只是可选能力 ⚠）、档案校验 ✓、mock 后端 ✓。

## 3. 离线试跑 20 回合

```bash
python agent.py run --dry-run --rounds 20
```
预期：每回合一行 `[回合 N] HP=… 决策=… → 动作`，收尾有 `[汇总]` 与 `[汇报]`。

## 4. 看事件流与统计

```bash
python agent.py logs --events --tail 5
python agent.py logs --stats
```
预期：JSONL 事件（decision/session_end…）；统计含动作分布、决策来源、死亡率。

## 5. 开局侦察 + 战绩报告

```bash
python agent.py brief
python agent.py session --report
```
预期：brief 五板块（模式/档案/知识库/战绩/调参）；报告按游戏分组统计。

## 6. 对局回放

```bash
python agent.py replay --tail 30
```
预期：时间线逐回合动作，死亡回合带 ☠ 标记。

## 7. 监控面板（可选）

```bash
python agent.py panel &        # 默认 http://127.0.0.1:5002
curl -s http://127.0.0.1:5002/healthz
kill %1
```
预期：healthz 返回 `{"status":"ok",...}`；浏览器打开可看实时面板。

## 8. 性能基准（可选）

```bash
python agent.py bench --rounds 500
```
预期：五段耗时表 + 吞吐（参考值：数百~上千回合/秒，机器而定）。

## 下一步

- 接新游戏：[game-profile-guide.md](game-profile-guide.md)
- 真机链路：配 `.env`（LLM/感知），去掉 `--dry-run`（仅限本地/自建/已授权环境）
