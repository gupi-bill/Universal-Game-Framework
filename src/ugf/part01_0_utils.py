# ===========================================================================
# 0. 轻量工具
# ===========================================================================
def _load_dotenv(path: str | None = None):
    """极简 .env 加载（省掉 python-dotenv 依赖）。已存在的环境变量优先。"""
    path = path or os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


_load_dotenv()


def _setup_console():
    """控制台编码硬化（ROADMAP #19）：Windows GBK 终端下中文日志不再抛
    UnicodeEncodeError——统一把 stdout/stderr 切到 UTF-8，失败静默降级。"""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")


_setup_console()


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def retry_call(fn, attempts: int, backoff: float, label: str) -> tuple:
    """外部依赖统一调用（ROADMAP #5）：返回 (ok, result, err)。

    - attempts: 总尝试次数（含首次）；backoff: 线性退避基数（第 i 次失败后睡 backoff*i 秒）
    - 每次失败细节进日志；最终失败返回 (False, None, 最后一次错误)
    """
    attempts = max(1, safe_int(attempts, 1))
    backoff = max(0.0, safe_float(backoff, 0.0))
    last_err = ""
    for i in range(attempts):
        try:
            return True, fn(), ""
        except Exception as e:  # 外部边界统一收敛：网络/解析/服务异常都算失败
            last_err = f"{type(e).__name__}: {e}"
            if i + 1 < attempts:
                log(
                    f"[降级] {label} 第 {i + 1}/{attempts} 次失败: {last_err}，{backoff * (i + 1):.1f}s 后重试"
                )
                if backoff > 0:
                    time.sleep(backoff * (i + 1))
    log(f"[降级] {label} {attempts} 次尝试均失败: {last_err}")
    return False, None, last_err


def safe_float(v, default: float = 0.0) -> float:
    """脏数据（None / 字符串 / NaN / Inf）一律收敛成有限 float。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def safe_int(v, default: int = 0) -> int:
    if v is None or isinstance(v, bool):
        return default
    if isinstance(v, float):
        return int(v) if math.isfinite(v) else default
    try:
        return int(v)
    except (TypeError, ValueError):
        pass
    f = safe_float(v, float(default))
    return int(f)


def _rotate_logs_if_needed(force: bool = False):
    """ROADMAP #16：日志轮转落地——超过 logs.max_size_mb 压缩轮转（gzip），
    超过 logs.retention_days 的旧日志删除。默认 60 秒最多检查一次，长跑低开销。"""
    global _last_rotate_check
    now = time.time()
    if not force and now - _last_rotate_check < 60:
        return
    _last_rotate_check = now
    try:
        os.makedirs(RUN_LOGS, exist_ok=True)
        max_mb = safe_float(cfg_get("logs.max_size_mb", 20), 20)
        retention = safe_int(cfg_get("logs.retention_days", 7), 7)
        cutoff = now - max(0, retention) * 86400
        for fn in os.listdir(RUN_LOGS):
            if not fn.startswith("agent_") or ".log" not in fn:
                continue
            fp = os.path.join(RUN_LOGS, fn)
            try:
                stt = os.stat(fp)
            except OSError:
                continue
            if fn.endswith(".log") and max_mb > 0 and stt.st_size > max_mb * 1024 * 1024:
                import gzip

                gz = f"{fp}.{time.strftime('%H%M%S')}.gz"
                try:
                    with open(fp, "rb") as src, gzip.open(gz, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    os.truncate(fp, 0)
                except OSError:
                    pass
            elif stt.st_mtime < cutoff:
                with contextlib.suppress(OSError):
                    os.remove(fp)
    except OSError:
        pass


_last_rotate_check = 0.0

EVENTS_FILE_NAME = "events.jsonl"


def log_event(kind: str, **data):
    """ROADMAP #16：结构化事件流（run_logs/events.jsonl），一行一事件，可机器分析。

    kind 约定：decision / death / tune / learn / session_end / config_reload。
    """
    try:
        os.makedirs(RUN_LOGS, exist_ok=True)
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind}
        rec.update(data)
        with open(os.path.join(RUN_LOGS, EVENTS_FILE_NAME), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def read_events(tail: int = 20, kind: str = "") -> list:
    """读取结构化事件（可选按 kind 过滤），返回最后 tail 条。"""
    path = os.path.join(RUN_LOGS, EVENTS_FILE_NAME)
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if kind and rec.get("kind") != kind:
                    continue
                out.append(rec)
    except OSError:
        return []
    return out[-max(1, safe_int(tail, 20)) :]


def log(msg: str):
    """带时间戳的日志；同时写 run_logs/agent_YYYYMMDD.log（带轮转）。"""
    _rotate_logs_if_needed()
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(RUN_LOGS, exist_ok=True)
        with open(
            os.path.join(RUN_LOGS, f"agent_{datetime.now().strftime('%Y%m%d')}.log"), "a", encoding="utf-8"
        ) as f:
            f.write(line + "\n")
    except OSError:
        pass


def safe_name(name: str, sep: str = "_") -> str:
    """把外部传入的文件名/游戏名清洗成单层安全名，杜绝路径穿越。"""
    raw = (name or "").strip().replace("\\", sep).replace("/", sep)
    raw = "".join(ch if not ch.isspace() else sep for ch in raw)
    raw = raw.replace("\0", "").replace("..", "")
    return raw.strip().strip(sep).strip(".").strip()


