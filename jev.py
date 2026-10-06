# /// script
# requires-python = ">=3.10"
# dependencies = ["typesafe-sdk>=0.7,<0.8", "mcp>=1.9,<2"]
# ///
"""
jev-tokensaver - TypeSafe Jev as a CLI (Claude Code) and an MCP server (Cowork / Claude Desktop).
https://github.com/Dinesh-Sunny/jev-tokensaver  ·  MIT licence  ·  not affiliated with TypeSafe AI

Jev pre-reads things so only the relevant parts reach Claude's context. Jev never writes
text; it answers typed questions (noul = P(yes), choice = one of N options, score = level
on a described scale). Price (jev-1.13): $0.042 per 1M input tokens, output free.

Commands (MCP tools: jev_find, jev_prune, jev_rank, jev_map, jev_ask, jev_feedback, jev_status, jev_control)
  find      lines in a big file that answer a question
  prune     filter long command output (stdin) down to what matters; full log kept on disk
  rank      which files under a folder hold what you're looking for
  map       same questions over every item of a list file
  ask       one call: state + typed questions
  eval      measure real accuracy on your own golden set (fit thresholds on half, validate on half)
  feedback  record a miss (drives tuning)
  tune      apply thresholds / top_k fitted from eval + feedback
  review    failure patterns + recommendations
  usage     spend + estimated Claude tokens saved
  check / key / off / on / dismiss / status   account health: test, new key, pause, resume, hide alert
  setup / doctor / uninstall                  guided install, health check, clean removal
  serve     MCP server (default when run with no args)

Design rules (from TypeSafe docs + community audits):
  fail open (FALLBACK -> Claude works normally) . deterministic checks first . Jev decides
  what to SHOW, never what to delete (full text stays on disk) . probabilities are rankings
  until calibrated on your data (jev eval) . secrets never leave the Mac (deny-list + scrub)

API key: env TYPESAFE_API_KEY or ~/.config/typesafe/api_key.  Off switches: JEV_DISABLE=1,
the file ~/.config/typesafe/disabled, or a `.jevoff` file in a project folder (or any parent).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

os.environ.setdefault("TYPESAFE_LOG_LEVEL", "warning")  # keep the SDK quiet on stderr

VERSION = "0.4.0"
QUESTIONS_VERSION = "q3"            # bump when question wording changes (busts the cache)

# ================================================================ settings ===


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


CONFIG_DIR = Path(os.environ.get("JEV_CONFIG_DIR", "~/.config/typesafe")).expanduser()
DB_PATH = CONFIG_DIR / "jev.db"
TUNING_PATH = CONFIG_DIR / "tuning.json"
PRUNE_DIR = Path(os.environ.get("JEV_PRUNE_DIR", "~/.cache/jev/prune")).expanduser()

MODEL = os.environ.get("TYPESAFE_DEFAULT_MODEL", "jev-latest")   # pin e.g. jev-1.13.0 once tuned
PRICE_PER_M_INPUT = 0.042            # USD per 1M input tokens, output free
WORKERS = int(_env_float("JEV_WORKERS", 6))           # public endpoint dislikes >8 concurrent
DAILY_BUDGET_USD = _env_float("JEV_DAILY_BUDGET_USD", 0.20)
CACHE_DAYS = _env_float("JEV_CACHE_DAYS", 7)            # 0 disables the cache
BREAKER_FAILS, BREAKER_WINDOW, BREAKER_PAUSE = 3, 120, 300
AUTOTUNE_EVERY = int(_env_float("JEV_AUTOTUNE_EVERY", 10))       # 0 = off
AUTOTUNE_APPLY = os.environ.get("JEV_AUTOTUNE_APPLY", "") not in ("", "0")   # default: propose only
MONTHLY_CREDIT_USD = _env_float("JEV_MONTHLY_CREDIT_USD", 5.0)   # warn at 80% of this (0 = off)
ALERT_PATH = CONFIG_DIR / "alert.json"            # current account alert (state)
ALERT_HOOK_PATH = CONFIG_DIR / "alert-hook.json"  # what the Claude Code hook prints (present = show it)
BILLING_URL = "https://console.typesafe.ai/settings/billing"
KEYS_URL = "https://console.typesafe.ai/settings/keys"

CHUNK_MAX_UNITS = 200                # Choice allows 255 options; stay under
CHUNK_MAX_TOKENS = 20_000            # estimated; Jev limit is 32k for state + longest question
SEGMENT_CHARS = 300                  # long lines are split into 300-char segments, not truncated
MAX_LINE_SEARCH_CHARS = 400_000      # beyond this per line, chars are reported as not searched
STATE_MAX_TOKENS = 24_000            # cap for ask state / map item (est.)
RANK_HEAD_CHARS, RANK_DEFS_CHARS = 5_000, 3_000
MAX_FILE_BYTES = 5_000_000
MAX_RANK_FILES = 400
MAX_MAP_ITEMS = 2_000
PRUNE_PASSTHROUGH_LINES = 120        # shorter output is printed as-is, no Jev call
PRUNE_KEEP_FILES = 50
CLAUDE_READ_CAP_TOKENS = 25_000      # Claude Code's Read stops around here; used for honest savings
CALL_OVERHEAD_TOKENS = 150           # Claude-side cost of issuing a jev call

SKIP_DIRS = {"node_modules", "dist", "build", "out", "target", "venv", "__pycache__", "coverage",
             "vendor", "Pods", "DerivedData"}
DENY_NAME_RE = re.compile(
    r"(^\.env($|\.)|\.pem$|\.key$|\.p12$|\.pfx$|\.keystore$|\.jks$|^id_[a-z0-9]+(\.pub)?$|^\.npmrc$|"
    r"^\.pypirc$|^\.netrc$|credential|secret|\.tfstate|\.kdbx$|^api_key$|\.sqlite3?$|\.db$)", re.I)
SECRET_RES = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk_(live|test)_[A-Za-z0-9]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
]
SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|token|access[_-]?key|private[_-]?key|client[_-]?secret)"
    r"(\"?'?\s*[:=]\s*)([\"']?)[^\s\"',;]{8,}")
ERROR_LINE_RE = re.compile(
    r"(?i)(\b(error|errors|fail|failed|failing|failure|exception|traceback|panic|fatal|assert(ion)?error|"
    r"denied|segfault|undefined|cannot|unable|unhandled|rejected)\b|✗|✘|×|\bFAIL\b|ERR!|^\s*E\s{2,})")
DEF_LINE_RE = re.compile(
    r"^\s*(export\s+)?(default\s+)?(async\s+)?(def|class|function|interface|type|enum|const|let|struct|impl|fn|"
    r"func|public|private|protected|module|router\.|app\.(get|post|put|delete)|CREATE\s+TABLE)\b", re.I)

DEFAULTS = {"find.found": 0.70, "find.absent": 0.35, "find.top_k": 8, "rank.top_k": 10}
LIMITS = {"find.found": (0.6, 0.95), "find.absent": (0.10, 0.35), "find.top_k": (8, 25), "rank.top_k": (10, 30)}
MAX_STEP = 0.05                      # thresholds move at most this much per tune


def tuned(key: str):
    try:
        data = json.loads(TUNING_PATH.read_text())
        return type(DEFAULTS[key])(data["values"][key])
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULTS[key]


# ---- question templates (edit wording here, then bump QUESTIONS_VERSION) ----

def find_questions(query: str, ids: list[str]) -> dict:
    q = {"exists": {"type": "noul",
                    "instructions": f'Does any line of the document address or answer: "{query}"?',
                    "criteria": {"true": "At least one line of the document states or directly implies the answer",
                                 "false": "No line of the document addresses this"}}}
    if len(ids) >= 2:
        q["where"] = {"type": "choice",
                      "instructions": f'Which line of the document contains the answer to: "{query}"?',
                      "criteria": {i: None for i in ids}}
    return q


RANK_QUESTIONS = {"relevant": {
    "type": "noul",
    "instructions": "Is this file likely to contain the code or information that `query` is looking for?",
    "criteria": {"true": "The file itself implements, defines, configures or states what the query asks about",
                 "false": "The file is unrelated, or only mentions the topic in passing"}}}

for _name in ("httpx", "httpx2", "typesafe_sdk"):
    logging.getLogger(_name).setLevel(logging.WARNING)


# ================================================================== errors ===

class JevOff(RuntimeError):
    """Jev not used (kill switch, .jevoff, no key, paused, budget) - caller should work normally."""


class QuestionError(ValueError):
    """The questions JSON is invalid - a bug to fix, not an outage."""


# =================================================================== store ===

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, answers TEXT, tokens INT, model TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, tool TEXT, note TEXT,
  requests INT, cached INT, failed INT, tokens INT, cost REAL, avoided INT,
  models TEXT, errors TEXT, outcome TEXT, detail TEXT,
  feedback TEXT, feedback_note TEXT, answer_rank INT, feedback_ts REAL);
CREATE TABLE IF NOT EXISTS evals (ts REAL, kind TEXT, query TEXT, target TEXT, absent INT,
  exists_p REAL, answer_rank INT, model TEXT);
CREATE TABLE IF NOT EXISTS breaker (id INT PRIMARY KEY, failures TEXT, open_until REAL, trips INT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""
_db_ready: str | None = None
_db_lock = threading.Lock()


def _init_db() -> None:
    """Create/upgrade the schema once per process (under a lock: parallel first calls used to race)."""
    global _db_ready
    with _db_lock:
        if _db_ready == str(DB_PATH):
            return
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        new_db = not DB_PATH.exists()
        con = sqlite3.connect(DB_PATH, timeout=30)
        try:
            if new_db:
                os.chmod(DB_PATH, 0o600)             # holds file names and queries: keep it private
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(_SCHEMA)
            con.execute("DELETE FROM cache WHERE ts < ?", (time.time() - max(CACHE_DAYS, 1) * 86400,))
            con.commit()
        finally:
            con.close()
        _db_ready = str(DB_PATH)


@contextmanager
def db():
    _init_db()
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _meta_get(k: str) -> str | None:
    with db() as con:
        row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else None


def _meta_set(k: str, v: str) -> None:
    with db() as con:
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))


# ================================================================== client ===

_client = None
_client_lock = threading.Lock()
_resolved_model: str | None = None   # what MODEL (an alias) currently resolves to; part of the cache key


def _api_key() -> str | None:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    f = CONFIG_DIR / "api_key"
    if f.exists():
        return f.read_text().strip() or None
    return None


def _get_client():
    global _client
    with _client_lock:
        if _client is None:
            from typesafe_sdk import RetryPolicy, TypeSafeClient
            key = _api_key()
            if not key:
                raise JevOff("no TypeSafe API key - save one to ~/.config/typesafe/api_key "
                             "(create it at https://console.typesafe.ai/settings/keys)")
            _client = TypeSafeClient(
                api_key=key, model=MODEL, timeout=60.0,
                retry=RetryPolicy(max_retries=4, backoff_initial=1.0, backoff_max=15.0, timeout=90.0))
        return _client


def _disabled_reason(paths: list[Path] | None = None) -> str | None:
    if os.environ.get("JEV_DISABLE", "") not in ("", "0"):
        return "disabled by JEV_DISABLE (kill switch)"
    if (CONFIG_DIR / "disabled").exists():
        return f"disabled by {CONFIG_DIR / 'disabled'} (kill switch)"
    for p in paths or []:
        for parent in [p, *p.parents]:
            if (parent / ".jevoff").exists():
                return f"turned off for this folder ({parent / '.jevoff'})"
    return None


def _answer_to_dict(a: Any, keep_top: int = 40) -> dict:
    t = getattr(a, "type", None)
    if t == "noul":
        return {"type": "noul", "noul": round(a.noul, 4)}
    if t == "choice":
        probs = sorted(a.probabilities.items(), key=lambda kv: -kv[1])[:keep_top]
        return {"type": "choice", "choice": a.choice, "confidence": round(a.confidence, 4),
                "probabilities": {k: round(v, 4) for k, v in probs}}
    if t == "score":
        return {"type": "score", "score": round(a.score, 4), "confidence": round(a.confidence, 4),
                "probabilities": {str(int(k)): round(v, 4) for k, v in a.probabilities.items()}}
    return {"type": str(t)}


# ---- token estimate (conservative: CJK/Indic text is ~1+ token per char) ----

_NON_ASCII = re.compile(r"[^\x00-\x7f]")


def _tok(s: str) -> int:
    non = len(_NON_ASCII.findall(s))
    return int((len(s) - non) / 3.2 + non * 1.3) + 1


# ---- secrets --------------------------------------------------------------------

def _scrub(text: str) -> tuple[str, int]:
    """Redact secrets, keeping the line count identical (so line numbers stay right)."""
    n = 0

    def block(m):
        nonlocal n
        n += 1
        return "\n".join("[REDACTED]" for _ in m.group(0).split("\n"))

    for rx in SECRET_RES:
        text = rx.sub(block, text)

    def assign(m):
        nonlocal n
        n += 1
        return f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED]"

    return SECRET_ASSIGN_RE.sub(assign, text), n


def _scrub_obj(o: Any) -> Any:
    if isinstance(o, str):
        return _scrub(o)[0]
    if isinstance(o, list):
        return [_scrub_obj(x) for x in o]
    if isinstance(o, dict):
        return {k: _scrub_obj(v) for k, v in o.items()}
    return o


def _is_secret_file(p: Path) -> bool:
    return bool(DENY_NAME_RE.search(p.name))


# ---- circuit breaker (only real outages count; our own bad requests never do) ----

def _is_outage(e: Exception) -> bool:
    try:
        from typesafe_sdk import (TypeSafeAPIConnectionError, TypeSafeAuthenticationError,
                                  TypeSafeInternalServerError, TypeSafePermissionDeniedError)
    except ImportError:
        return False
    return isinstance(e, (TypeSafeAPIConnectionError, TypeSafeInternalServerError,
                          TypeSafeAuthenticationError, TypeSafePermissionDeniedError))


def _breaker_check() -> None:
    with db() as con:
        row = con.execute("SELECT open_until FROM breaker WHERE id=1").fetchone()
    if row and row[0] and row[0] > time.time():
        raise JevOff(f"paused after repeated failures until {time.strftime('%H:%M:%S', time.localtime(row[0]))}")


def _breaker_record(ok: bool) -> None:
    now = time.time()
    with db() as con:
        row = con.execute("SELECT failures, open_until, trips FROM breaker WHERE id=1").fetchone()
        fails, open_until, trips = (json.loads(row[0]), row[1], row[2]) if row else ([], 0, 0)
        if ok:
            if not fails and not open_until:
                return
            fails, open_until = [], 0
        else:
            fails = [t for t in fails if now - t < BREAKER_WINDOW] + [now]
            if len(fails) >= BREAKER_FAILS and open_until < now:
                open_until, trips, fails = now + BREAKER_PAUSE, trips + 1, []
        con.execute("INSERT OR REPLACE INTO breaker VALUES (1, ?, ?, ?)", (json.dumps(fails), open_until, trips))


# ---- account alerts: no key / key rejected or expired / out of credit ---------------
# Shown (1) at the top of every jev result, (2) to the user + Claude on every prompt via the
# Claude Code hook (alert-hook.sh prints alert-hook.json), (3) as a macOS notification once.
# Cleared automatically by the next successful call, `jev check`, `jev key`, or `jev off`.

_CREDIT_WORDS = re.compile(r"credit|balance|billing|quota|insufficient|payment|funds|top.?up|exhausted", re.I)


def _account_problem(e: Exception) -> tuple[str, str] | None:
    name, status = type(e).__name__, getattr(e, "status", None)
    msg = str(getattr(e, "_message", "") or e)            # server's message, without the URL
    if name == "JevOff" and "no TypeSafe API key" in msg:
        return "no_key", "no TypeSafe API key is set up on this Mac"
    if status == 402 or (_CREDIT_WORDS.search(msg) and name.startswith("TypeSafe")):
        return "credit", f"the TypeSafe account looks out of credit (server said: \"{msg[:100]}\")"
    if name == "TypeSafeAuthenticationError":
        return "key", "TypeSafe rejected the API key - it is wrong, revoked or expired (HTTP 401)"
    if name == "TypeSafePermissionDeniedError":
        return "access", "TypeSafe denied access (HTTP 403) - usually no credit left or the key lacks permission"
    return None


def _alert_options(kind: str) -> list[str]:
    first = {
        "credit": f"Add credit or redeem a code at {BILLING_URL}, then run `jev check`",
        "low_credit": f"Check the balance / add credit at {BILLING_URL}",
        "key": f"Create a new key at {KEYS_URL}, then run `jev key` in Terminal to save it",
        "no_key": f"Create a key at {KEYS_URL}, then run `jev key` in Terminal to save it",
        "access": f"Check credit at {BILLING_URL} and the key at {KEYS_URL}, then run `jev check`",
    }[kind]
    return [first,
            "Pause Jev for now - Claude works normally but uses more tokens: run `jev off` (`jev on` to resume)",
            "Do nothing - Claude carries on without Jev; `jev dismiss` hides this alert for 12 hours"]


def _alert_text(kind: str, reason: str) -> str:
    head = ("Jev credit is running low" if kind == "low_credit"
            else "Jev has STOPPED working - Claude is continuing without it (uses more tokens)")
    opts = "\n".join(f"  {i}. {o}" for i, o in enumerate(_alert_options(kind), 1))
    return f"{head}: {reason}.\nOptions:\n{opts}"


def _read_alert() -> dict | None:
    try:
        return json.loads(ALERT_PATH.read_text())
    except (OSError, ValueError):
        return None


def _notify(title: str, text: str) -> None:
    """macOS notification - best effort, never raises."""
    if sys.platform != "darwin" or os.environ.get("JEV_NO_NOTIFY"):
        return
    script = f'display notification {json.dumps(text[:200])} with title {json.dumps(title)} sound name "Basso"'
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def _write_hook(kind: str, reason: str, once: bool = False) -> None:
    text = _alert_text(kind, reason)
    payload = {
        "systemMessage": "⚠️ " + text.split("\n")[0] + f" Fix: {_alert_options(kind)[0]}",
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": ("JEV ALERT - start your reply with this, in plain words, before anything else, "
                                  "then continue the user's request:\n" + text)}}
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ALERT_HOOK_PATH.write_text(json.dumps(payload))
    once_flag = ALERT_HOOK_PATH.with_name(ALERT_HOOK_PATH.name + ".once")
    if once:
        once_flag.touch()
    elif once_flag.exists():
        once_flag.unlink()


def _raise_alert(kind: str, reason: str) -> None:
    try:
        prev = _read_alert() or {}
        now = time.time()
        if prev.get("kind") == kind and prev.get("dismissed_until", 0) > now:
            return                                    # user dismissed this alert for now
        is_new = prev.get("kind") != kind
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        ALERT_PATH.write_text(json.dumps({"kind": kind, "reason": reason, "ts": now,
                                          "first_ts": prev.get("first_ts", now) if not is_new else now}))
        _write_hook(kind, reason, once=(kind == "low_credit"))
        if is_new:
            _notify("Jev: credit running low" if kind == "low_credit" else "Jev stopped working",
                    f"{reason}. Options: {_alert_options(kind)[0]}")
    except OSError:
        pass


def clear_alert() -> bool:
    had = ALERT_PATH.exists() or ALERT_HOOK_PATH.exists()
    for f in (ALERT_PATH, ALERT_HOOK_PATH, ALERT_HOOK_PATH.with_name(ALERT_HOOK_PATH.name + ".once")):
        try:
            f.unlink()
        except OSError:
            pass
    return had


def _alert_block() -> str:
    a = _read_alert()
    if not a or a.get("kind") == "low_credit" or a.get("dismissed_until", 0) > time.time():
        return ""
    return ("JEV ALERT - tell the user this at the very TOP of your reply (plain words, with the options), "
            "then continue the task without Jev:\n" + _alert_text(a["kind"], a["reason"]) + "\n\n")


def _check_low_credit() -> None:
    if MONTHLY_CREDIT_USD <= 0:
        return
    lt = time.localtime()
    start = time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    with db() as con:
        spent = con.execute("SELECT COALESCE(SUM(cost),0) FROM calls WHERE ts>=?", (start,)).fetchone()[0]
    month = time.strftime("%Y-%m")
    if spent >= 0.8 * MONTHLY_CREDIT_USD and _meta_get("low_credit_warned") != month:
        _meta_set("low_credit_warned", month)
        if not _read_alert():
            _raise_alert("low_credit", f"jev has spent about ${spent:.2f} this month, 80%+ of the "
                                       f"${MONTHLY_CREDIT_USD:.2f} monthly credit (estimate from this Mac only)")


# ---- per-command accounting ------------------------------------------------------

class Run:
    """Stats for one command (possibly many Jev requests)."""

    def __init__(self, tool: str):
        self.tool, self.requests, self.cached, self.failed, self.tokens = tool, 0, 0, 0, 0
        self.models: set[str] = set()
        self.errors: dict[str, int] = {}
        self.lock = threading.Lock()
        self.spent_start: float | None = None

    def add_error(self, e: Exception) -> str:
        with self.lock:
            self.failed += 1
            self.errors[type(e).__name__] = self.errors.get(type(e).__name__, 0) + 1
        return _friendly(e)


def _friendly(e: Exception) -> str:
    name = type(e).__name__
    if name == "JevOff":
        return str(e)
    if name in ("TypeSafeAuthenticationError", "TypeSafePermissionDeniedError"):
        return (f"{name}: TypeSafe rejected the API key or the account is out of credit "
                "(console.typesafe.ai/settings/keys and /settings/billing)")
    if name == "TypeSafeRateLimitError":
        return f"{name}: rate limited - lower JEV_WORKERS"
    return f"{name}: {str(e)[:160]}"


def _spent_today() -> float:
    lt = time.localtime()
    start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    with db() as con:
        return con.execute("SELECT COALESCE(SUM(cost),0) FROM calls WHERE ts>=?", (start,)).fetchone()[0]


def _check_budget(run: Run, est_tokens: int) -> None:
    if DAILY_BUDGET_USD <= 0:
        return
    with run.lock:
        if run.spent_start is None:
            run.spent_start = _spent_today()
        projected = run.spent_start + (run.tokens + est_tokens) * PRICE_PER_M_INPUT / 1e6
    if projected > DAILY_BUDGET_USD:
        raise JevOff(f"daily Jev budget ${DAILY_BUDGET_USD:.2f} reached (raise JEV_DAILY_BUDGET_USD to allow more)")


def _cache_key(state: Any, questions: dict) -> str:
    global _resolved_model
    if _resolved_model is None:
        _resolved_model = _meta_get(f"resolved:{MODEL}") or MODEL
    blob = json.dumps([QUESTIONS_VERSION, MODEL, _resolved_model, state, questions], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def call_jev(run: Run, state: Any, questions: dict) -> dict:
    """One Jev request: kill switch -> cache -> breaker -> budget -> API. Returns {qid: answer}."""
    global _resolved_model
    reason = _disabled_reason()
    if reason:
        raise JevOff(reason)
    key = _cache_key(state, questions)
    if CACHE_DAYS > 0:
        with db() as con:
            row = con.execute("SELECT answers, model FROM cache WHERE key=? AND ts>?",
                              (key, time.time() - CACHE_DAYS * 86400)).fetchone()
        if row:
            with run.lock:
                run.requests += 1
                run.cached += 1
                run.models.add(row[1])
            return json.loads(row[0])
    _breaker_check()
    _check_budget(run, _tok(json.dumps(state, default=str)) + _tok(json.dumps(questions)))
    try:
        resp = _get_client().system_one(state=state, questions=questions)
    except Exception as e:
        problem = _account_problem(e)
        if _is_outage(e) or (problem and problem[0] != "no_key"):   # don't hammer a dead or unpaid account
            _breaker_record(False)
        if problem:
            _raise_alert(*problem)
        raise
    _breaker_record(True)
    a = _read_alert()
    if a and a.get("kind") != "low_credit":
        clear_alert()                              # it works again
    answers = {k: _answer_to_dict(v) for k, v in resp.answers.items()}
    tokens = (resp.usage.input_tokens or 0) if resp.usage else 0
    model = getattr(resp, "model", "") or MODEL
    if model != _resolved_model:                 # alias moved to a new version -> new cache namespace
        _resolved_model = model
        _meta_set(f"resolved:{MODEL}", model)
    key = _cache_key(state, questions)           # always re-key: parallel requests may have raced the update
    with run.lock:
        run.requests += 1
        run.tokens += tokens
        run.models.add(model)
    if CACHE_DAYS > 0:
        with db() as con:
            con.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?)",
                        (key, json.dumps(answers), tokens, model, time.time()))
    return answers


def _safe(run: Run, fn: Callable):
    """Isolate one request: returns (answers, None) or (None, error message)."""
    def wrapped(item):
        try:
            return fn(item), None
        except Exception as e:  # noqa: BLE001 - reported back, never raised
            return None, run.add_error(e)
    return wrapped


def _parallel(fn, items):
    if len(items) <= 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(max_workers=max(1, WORKERS)) as pool:
        return list(pool.map(fn, items))


def _finish(run: Run, outcome: str, avoided: int, note: str = "", detail: dict | None = None) -> str:
    cost = run.tokens * PRICE_PER_M_INPUT / 1e6
    call_id = None
    try:
        with db() as con:
            cur = con.execute(
                "INSERT INTO calls (ts, tool, note, requests, cached, failed, tokens, cost, avoided, models,"
                " errors, outcome, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), run.tool, note[:200], run.requests, run.cached, run.failed, run.tokens, cost,
                 max(0, avoided), ",".join(sorted(run.models)), json.dumps(run.errors), outcome,
                 json.dumps(detail or {})))
            call_id = cur.lastrowid
    except sqlite3.Error:
        pass
    if run.tokens:
        try:
            _check_low_credit()
        except sqlite3.Error:
            pass
    saved = f" · ~{max(0, avoided) / 1000:.1f}k tokens saved" if avoided > 0 else ""
    return f"[jev id={call_id} · ${cost:.5f}{saved}]"


def _fallback(run: Run, err: str, note: str = "") -> str:
    return (_alert_block() + f"FALLBACK: Jev not used ({err}). Do this step the normal way "
            f"(read / grep / read the items yourself).\n" + _finish(run, "fallback", 0, note))


# ================================================================= helpers ===

def _read_text(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"Not a file: {path}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"File is larger than {MAX_FILE_BYTES // 1_000_000} MB: {path}")
    raw = path.read_bytes()
    if b"\x00" in raw[:4096]:
        raise ValueError(f"Looks like a binary file: {path}")
    return raw.decode("utf-8-sig", errors="replace")


def _split_lines(text: str) -> list[str]:
    """Split on \\n only (str.splitlines also splits on \\x0c, \\x1c... and shifts line numbers)."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def _search_roots() -> list[Path]:
    roots = [Path(p).expanduser() for p in os.environ.get("JEV_ROOTS", "").split(os.pathsep) if p]
    home = Path.home()
    for base in (home, home / "Documents", home / "CodeProjects", home / "Desktop"):
        try:
            roots += [d for d in base.iterdir() if d.is_dir() and not d.name.startswith(".")]
        except OSError:
            continue
    return roots


def resolve_path(p: str) -> Path:
    """Expand ~, and accept Cowork VM paths (.../mnt/<folder>/rest) by mapping them to the Mac folder."""
    pp = Path(p).expanduser()
    if pp.exists():
        return pp.resolve()
    m = re.search(r"/mnt/([^/]+)(?:/(.*))?$", str(p))
    if m:
        name, rest = m.group(1), m.group(2) or ""
        for root in _search_roots():
            if root.name == name and (root / rest).exists():
                return (root / rest).resolve()
    hint = "" if pp.is_absolute() else " (use an absolute path on the Mac)"
    raise FileNotFoundError(f"Not found: {p}{hint}")


# ================================================================ find core ===

def _units(lines: list[str]) -> tuple[list[tuple[str, int, int, str]], int]:
    """(id, line_no, seg, text) per non-blank line or 300-char segment; also returns unsearched chars."""
    units, unsearched = [], 0
    for i, line in enumerate(lines, start=1):
        t = line.strip()
        if not t:
            continue
        if len(t) <= SEGMENT_CHARS:
            units.append((f"L{i:05d}", i, 0, t))
            continue
        if len(t) > MAX_LINE_SEARCH_CHARS:
            unsearched += len(t) - MAX_LINE_SEARCH_CHARS
            t = t[:MAX_LINE_SEARCH_CHARS]
        for k in range(0, len(t), SEGMENT_CHARS):
            units.append((f"L{i:05d}s{k // SEGMENT_CHARS}", i, k // SEGMENT_CHARS, t[k:k + SEGMENT_CHARS]))
    return units, unsearched


def _chunk_units(units: list) -> list[list]:
    chunks, cur, tok = [], [], 0
    for u in units:
        ut = _tok(u[3]) + 4
        if cur and (len(cur) >= CHUNK_MAX_UNITS or tok + ut > CHUNK_MAX_TOKENS):
            chunks.append(cur)
            cur, tok = [], 0
        cur.append(u)
        tok += ut
    if cur:
        chunks.append(cur)
    if len(chunks) >= 2 and len(chunks[-1]) < 30:          # avoid a tiny last chunk
        merged = chunks[-2] + chunks[-1]
        half = len(merged) // 2
        chunks[-2:] = [merged[:half], merged[half:]]
    return chunks


def _find_core(run: Run, text: str, query: str) -> dict:
    """Shared by find, prune and eval. Never raises for API problems."""
    lines = _split_lines(text)
    scrubbed, redactions = _scrub(text)
    units, unsearched = _units(_split_lines(scrubbed))
    chunks = _chunk_units(units)

    def run_chunk(chunk, depth=0):
        state = "\n".join(f"{uid}| {t}" for uid, _, _, t in chunk)
        try:
            return [(chunk, call_jev(run, state, find_questions(query, [u[0] for u in chunk])), None)]
        except JevOff as e:
            return [(chunk, None, run.add_error(e))]
        except Exception as e:  # noqa: BLE001
            too_big = type(e).__name__ in ("TypeSafeBadRequestError", "TypeSafeUnprocessableEntityError")
            if too_big and len(chunk) >= 2 and depth < 2:       # maybe over the size limit: split and retry
                half = len(chunk) // 2
                return run_chunk(chunk[:half], depth + 1) + run_chunk(chunk[half:], depth + 1)
            return [(chunk, None, run.add_error(e))]

    results = [r for group in _parallel(run_chunk, chunks) for r in group]
    ok = [(c, a) for c, a, _ in results if a is not None]
    failed = [(c, err) for c, a, err in results if a is None]
    line_scores: dict[int, float] = {}
    seg_of: dict[int, int] = {}
    best_exists, top_where = 0.0, 0.0
    for chunk, ans in ok:
        ex = ans["exists"]["noul"]
        probs = ans["where"]["probabilities"] if "where" in ans else {chunk[0][0]: 1.0}
        if ex > best_exists:
            best_exists, top_where = ex, (max(probs.values()) if probs else 0.0)
        id_map = {u[0]: u for u in chunk}
        for uid, p in probs.items():
            u = id_map.get(uid)
            if not u:
                continue
            s = round(ex * p, 4)
            if s > line_scores.get(u[1], -1):
                line_scores[u[1]], seg_of[u[1]] = s, u[2]
    hits = sorted(((s, n) for n, s in line_scores.items()), reverse=True)
    return {"lines": lines, "hits": hits, "seg_of": seg_of, "best_exists": best_exists, "top_where": top_where,
            "failed": failed, "unsearched_chars": unsearched, "chunks": len(chunks), "ok": len(ok),
            "redactions": redactions}


def _verdict(core: dict) -> str:
    found, absent = tuned("find.found"), tuned("find.absent")
    complete = not core["failed"] and not core["unsearched_chars"]
    if core["best_exists"] >= found and core["top_where"] >= 0.3:
        return "answered in this file"
    if core["best_exists"] < absent:
        return "probably NOT in this file" if complete else "unclear - part of the file was not searched"
    return "partially addressed"


def _show_line(lines: list[str], k: int, seg: int, mark: bool) -> str:
    line = lines[k - 1]
    if len(line) <= 400:
        return f"{'>' if mark else ' '}{k:6d}| {line}"
    a = max(0, seg * SEGMENT_CHARS - 50)
    return f"{'>' if mark else ' '}{k:6d}| [chars {a}-{a + 400} of {len(line):,}] {line[a:a + 400]}"


# =================================================================== tools ===

def find_impl(path: str, query: str, top_k: int = 0, context_lines: int = 2) -> str:
    run, note = Run("find"), f"{Path(path).name}: {query}"
    p = resolve_path(path)
    if _is_secret_file(p):
        return _fallback(run, f"refusing to send a likely-secret file ({p.name}) to TypeSafe", note)
    reason = _disabled_reason([p])
    if reason:
        return _fallback(run, reason, note)
    text = _read_text(p)
    if not text.strip():
        return "File is empty."
    top_k = top_k or tuned("find.top_k")
    core = _find_core(run, text, query)
    if not core["ok"]:
        return _fallback(run, core["failed"][0][1], note)
    lines, hits = core["lines"], core["hits"]
    shown_hits = [h for h in hits if h[0] >= 0.02][:max(1, top_k)] or hits[:1]
    verdict = _verdict(core)
    out = [f"{p} - {len(lines):,} lines. Verdict: {verdict} (exists={core['best_exists']:.2f}). "
           "Hits are shown below - no need to re-read them; read beyond them only if they don't answer it."]
    shown: set[int] = set()
    for score, n in sorted(shown_hits, key=lambda h: h[1]):
        if n in shown:
            continue
        lo, hi = max(1, n - context_lines), min(len(lines), n + context_lines)
        out.append(f"\n--- line {n} (score {score:.2f})")
        for k in range(lo, hi + 1):
            if k not in shown:
                shown.add(k)
                out.append(_show_line(lines, k, core["seg_of"].get(k, 0), k == n))
    if core["failed"]:
        ranges = ", ".join(f"{c[0][1]}-{c[-1][1]}" for c, _ in core["failed"])
        out.append(f"\nWARNING: lines {ranges} were NOT searched ({core['failed'][0][1]}) - grep/read them if needed.")
    if core["unsearched_chars"]:
        out.append(f"\nWARNING: {core['unsearched_chars']:,} chars on very long lines were not searched.")
    body = "\n".join(out)
    baseline = min(_tok(text), CLAUDE_READ_CAP_TOKENS)
    # only claim savings when Jev located the answer; otherwise Claude will likely read/grep anyway
    avoided = baseline - _tok(body) - CALL_OVERHEAD_TOKENS if verdict.startswith("answered") else 0
    outcome = "partial" if core["failed"] else ("ok" if verdict.startswith("answered") else "low_confidence")
    detail = {"exists": core["best_exists"], "hits": [[n, s] for s, n in hits[:50]], "lines": len(lines),
              "path": str(p), "verdict": verdict}
    return body + "\n\n" + _finish(run, outcome, avoided, note, detail)


def prune_impl(text: str, query: str, source: str = "stdin") -> str:
    """Show only what matters from long output; the full text is saved to disk (lossless)."""
    run, note = Run("prune"), f"{source}: {query}"
    lines = _split_lines(text)
    if len(lines) <= PRUNE_PASSTHROUGH_LINES:
        return text.rstrip("\n")
    PRUNE_DIR.mkdir(parents=True, exist_ok=True)
    saved = PRUNE_DIR / f"prune-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{time.time_ns() % 10**6}.log"
    saved.write_text(text)
    for old in sorted(PRUNE_DIR.glob("prune-*.log"))[:-PRUNE_KEEP_FILES]:
        try:
            old.unlink()
        except OSError:
            pass
    # deterministic first: error-looking lines, head and tail are always kept
    keep = {i for i, ln in enumerate(lines, 1) if ERROR_LINE_RE.search(ln)}
    if len(keep) > 80:
        keep = set(sorted(keep)[-80:])
    keep |= set(range(1, 6)) | set(range(max(1, len(lines) - 29), len(lines) + 1))
    jev_note = ""
    reason = _disabled_reason()
    if reason:
        jev_note = f"(Jev not used: {reason}; showing error lines + head/tail only)"
    else:
        core = _find_core(run, text, query)
        if core["ok"]:
            for _score, n in [h for h in core["hits"] if h[0] >= 0.02][:20]:
                keep |= set(range(max(1, n - 2), min(len(lines), n + 2) + 1))
        else:
            jev_note = f"(Jev not used: {core['failed'][0][1]}; showing error lines + head/tail only)"
    out, last = [], 0
    for i in sorted(keep)[:300]:
        if i > last + 1:
            out.append(f"   ... {i - last - 1} lines omitted ...")
        out.append(lines[i - 1][:500])
        last = i
    if last < len(lines):
        out.append(f"   ... {len(lines) - last} lines omitted ...")
    body = "\n".join(out)
    footer = (f"\n[jev prune: showing {min(len(keep), 300)} of {len(lines):,} lines. Full output: {saved} "
              f"- read it if something seems missing.] {jev_note}".rstrip())
    avoided = _tok(text) - _tok(body) - CALL_OVERHEAD_TOKENS
    return body + footer + "\n" + _finish(run, "fallback" if jev_note else "ok", avoided, note,
                                          {"saved": str(saved), "lines": len(lines)})


def _glob_re(pattern: str) -> re.Pattern:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + "$")


def _list_files(base: Path, pattern: str) -> tuple[list[Path], int]:
    """Files under base matching pattern; respects .gitignore in git repos; skips dot/secret/vendor paths."""
    rx = _glob_re(pattern)
    rels: list[str] = []
    try:
        res = subprocess.run(["git", "-C", str(base), "ls-files", "-co", "--exclude-standard", "-z"],
                             capture_output=True, timeout=20)
        if res.returncode == 0:
            rels = [r for r in res.stdout.decode(errors="replace").split("\0") if r]
    except (OSError, subprocess.SubprocessError):
        rels = []
    if not rels:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]
            rel_dir = os.path.relpath(dirpath, base)
            rels += [f if rel_dir == "." else f"{rel_dir}/{f}" for f in filenames]
            if len(rels) > 50_000:
                break
    keep = []
    for r in sorted(rels):
        parts = r.split("/")
        if any(x.startswith(".") or x in SKIP_DIRS for x in parts) or _is_secret_file(Path(r)):
            continue
        if rx.match(r):
            keep.append(base / r)
    return keep[:MAX_RANK_FILES], max(0, len(keep) - MAX_RANK_FILES)


def _excerpt(text: str) -> str:
    if len(text) <= RANK_HEAD_CHARS + RANK_DEFS_CHARS:
        return text
    defs, size = [], 0
    for ln in _split_lines(text[RANK_HEAD_CHARS:]):
        if DEF_LINE_RE.match(ln):
            defs.append(ln.strip()[:200])
            size += len(defs[-1])
            if size > RANK_DEFS_CHARS:
                break
    return text[:RANK_HEAD_CHARS] + "\n...\n[definitions later in the file]\n" + "\n".join(defs)


def rank_impl(query: str, root: str = "", pattern: str = "**/*", paths: list[str] | None = None,
              top_k: int = 0) -> str:
    run, note = Run("rank"), query
    top_k = top_k or tuned("rank.top_k")
    skipped_cap = 0
    if paths:
        files = [resolve_path(x) for x in paths]
        base = Path(os.path.commonpath([str(f) for f in files])) if len(files) > 1 else files[0].parent
        files = [f for f in files if not _is_secret_file(f)]
    else:
        if not root:
            return "ERROR: pass root= an absolute project folder (or explicit paths)."
        base = resolve_path(root)
        if base in (Path("/"), Path.home().resolve()):
            return "ERROR: root is too broad (/ or home) - pass a project folder."
        files, skipped_cap = _list_files(base, pattern)
    reason = _disabled_reason([base])
    if reason:
        return _fallback(run, reason, note)
    docs = []
    for f in files:
        try:
            docs.append((f.resolve(), _read_text(f)))
        except (OSError, ValueError):
            continue
    if not docs:
        return "No readable text files matched (dotfiles, secrets and vendor folders are skipped)."
    base_r = base.resolve()

    def state_for(item):
        f, text = item
        try:
            rel = str(f.relative_to(base_r))
        except ValueError:
            rel = str(f)
        return {"query": query, "file": {"path": rel, "excerpt": _scrub(_excerpt(text))[0]}}

    results = _parallel(_safe(run, lambda d: call_jev(run, state_for(d), RANK_QUESTIONS)), docs)
    unscored = [(f, err) for (a, err), (f, _) in zip(results, docs) if a is None]
    if len(unscored) == len(docs):
        return _fallback(run, unscored[0][1], note)
    ranked = sorted(((a["relevant"]["noul"], f, len(t)) for (a, _), (f, t) in zip(results, docs)
                     if a is not None), key=lambda r: -r[0])
    out = [f"Ranked {len(ranked)} files for: {query} (scores are a ranking, not exact probabilities)"]
    for p_rel, f, size in ranked[:max(1, top_k)]:
        out.append(f"{p_rel:.2f}  {f}  ({size:,} chars)")
    if len(ranked) > top_k:
        out.append(f"... {len(ranked) - top_k} more below {ranked[top_k - 1][0]:.2f}")
    if skipped_cap:
        out.append(f"WARNING: {skipped_cap} more matching files were NOT ranked (limit {MAX_RANK_FILES}) - "
                   "narrow --pattern or root.")
    if unscored:
        out.append(f"WARNING: {len(unscored)} file(s) could not be scored ({unscored[0][1]}) - check them yourself:")
        out.extend(f"  ?     {f}" for f, _ in unscored[:30])
    body = "\n".join(out)
    # honest baseline: without jev Claude would typically open ~5 candidates; with jev ~2
    tokens_of = {f: min(_tok(t), CLAUDE_READ_CAP_TOKENS) for f, t in docs}
    extra = sum(tokens_of.get(f, 0) for _, f, _ in ranked[2:5])
    avoided = extra - _tok(body) - CALL_OVERHEAD_TOKENS
    detail = {"ranked": [[str(f), round(p_, 4)] for p_, f, _ in ranked[:50]], "root": str(base_r)}
    return body + "\n\n" + _finish(run, "partial" if unscored or skipped_cap else "ok", avoided, note, detail)


# ---- questions validation (local, before any API call) ---------------------------

def parse_questions(questions: dict | str) -> dict:
    try:
        qs = json.loads(questions) if isinstance(questions, str) else dict(questions)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        raise QuestionError(f"questions is not valid JSON: {e}") from None
    if not isinstance(qs, dict) or not qs:
        raise QuestionError("questions must be a non-empty JSON object {id: question}")
    for qid, q in qs.items():
        if not isinstance(q, dict):
            raise QuestionError(f"{qid}: each question must be an object")
        t, crit = q.get("type"), q.get("criteria")
        if t not in ("noul", "choice", "score"):
            raise QuestionError(f"{qid}: type must be noul, choice or score")
        if not q.get("instructions"):
            raise QuestionError(f"{qid}: instructions (the full question) is required")
        if t == "choice" and (not isinstance(crit, dict) or not 2 <= len(crit) <= 255):
            raise QuestionError(f"{qid}: choice needs criteria {{option: description|null}} with 2-255 options")
        if t == "score" and (not isinstance(crit, list) or not 2 <= len(crit) <= 10):
            raise QuestionError(f"{qid}: score needs criteria as an ordered list of 2-10 described levels")
        if t == "noul" and crit is not None and (not isinstance(crit, dict) or not set(crit) <= {"true", "false"}):
            raise QuestionError(f"{qid}: noul criteria must be {{\"true\": ..., \"false\": ...}}")
    return qs


def _question_warnings(qs: dict) -> list[str]:
    w = []
    for qid, q in qs.items():
        if q["type"] == "choice" and not any(k.lower() in ("other", "none", "none_of_these", "unclear")
                                             for k in q["criteria"]):
            w.append(f"{qid}: consider adding an 'other'/'none' option - Jev must pick one, even when none fits")
    return w


def _fit_state(obj: Any) -> tuple[Any, bool]:
    s = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    if _tok(s) <= STATE_MAX_TOKENS:
        return obj, False
    cut = int(len(s) * STATE_MAX_TOKENS / _tok(s))
    return s[:cut] + " [...truncated]", True


def _load_items(items_path: str, items_json: Any) -> tuple[list[Any], bool]:
    inline = bool(items_json)
    if inline:
        data = json.loads(items_json) if isinstance(items_json, str) else items_json
    else:
        p = resolve_path(items_path)
        text = _read_text(p)
        suf = p.suffix.lower()
        if suf in (".jsonl", ".ndjson"):
            return [json.loads(ln) for ln in text.split("\n") if ln.strip()], False
        if suf in (".csv", ".tsv"):
            return list(csv.DictReader(io.StringIO(text), delimiter="\t" if suf == ".tsv" else ",")), False
        if suf == ".json":
            data = json.loads(text)
        else:
            blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
            return (blocks if len(blocks) > 1 else [ln for ln in _split_lines(text) if ln.strip()]), False
    if isinstance(data, dict):
        for k in ("items", "events", "data", "results", "records", "rows", "jobs", "entries"):
            if isinstance(data.get(k), list):
                return data[k], inline
        lists = sorted((v for v in data.values() if isinstance(v, list)),
                       key=lambda v: (sum(isinstance(x, dict) for x in v), len(v)), reverse=True)
        return (lists[0] if lists else [data]), inline
    return (data if isinstance(data, list) else [data]), inline


def _short(a: dict) -> str:
    t = a.get("type")
    if t == "noul":
        return f"{a['noul']:.2f}"
    if t == "choice":
        return f"{a['choice']} ({a['confidence']:.2f})"
    if t == "score":
        return f"{a['score']:.2f} ({a['confidence']:.2f})"
    return "?"


def map_impl(questions: dict | str, items_path: str = "", items_json: Any = "", id_field: str = "",
             context: str = "", sort_by: str = "", descending: bool = True, top_n: int = 50,
             out_csv: str = "") -> str:
    run = Run("map")
    try:
        qs = parse_questions(questions)
    except QuestionError as e:
        return f"QUESTION ERROR: {e} (Jev was not called - fix the questions)"
    items, inline = _load_items(items_path, items_json)
    total = len(items)
    items = items[:MAX_MAP_ITEMS]
    note = f"{len(items)} items: {','.join(qs)}"
    if not items:
        return "No items found."
    reason = _disabled_reason([resolve_path(items_path)] if items_path else None)
    if reason:
        return _fallback(run, reason, note)
    truncated = 0

    def state_for(item):
        nonlocal truncated
        it, cut = _fit_state(_scrub_obj(item))
        truncated += cut
        return {"item": it} if not context else {"context": context, "item": it}

    states = [state_for(i) for i in items]
    results = _parallel(_safe(run, lambda s: call_jev(run, s, qs)), states)
    errors = [err for a, err in results if a is None]
    if len(errors) == len(items):
        return _fallback(run, errors[0], note)
    answers = [a if a is not None else {"__error__": True} for a, _ in results]

    def label(i, item):
        if id_field and isinstance(item, dict) and id_field in item:
            return str(item[id_field])[:80]
        return f"#{i + 1}"

    warnings = _question_warnings(qs)
    sort_q, sort_label = (sort_by.split("=", 1) + [""])[:2] if sort_by else ("", "")
    if sort_q and sort_q not in qs:
        warnings.append(f"sort_by '{sort_q}' is not a question id - not sorted")
        sort_q = ""
    if sort_q and qs[sort_q]["type"] == "choice" and not sort_label:
        warnings.append(f"sort_by on a choice needs a label, e.g. {sort_q}={next(iter(qs[sort_q]['criteria']))}")
        sort_q = ""

    def sort_val(ans):
        a = ans.get(sort_q)
        if not a:
            return -math.inf if descending else math.inf
        if a["type"] == "choice":
            return a["probabilities"].get(sort_label, 0.0)
        return a.get("noul", a.get("score", 0.0))

    rows = [(i, item, ans) for i, (item, ans) in enumerate(zip(items, answers))]
    if sort_q:
        rows.sort(key=lambda r: sort_val(r[2]), reverse=descending)

    def cells(ans):
        if "__error__" in ans:
            return ["ERROR"] * len(qs)
        return [_short(ans[k]) if k in ans else "-" for k in qs]

    if out_csv:
        cp = Path(out_csv).expanduser()
        cp.parent.mkdir(parents=True, exist_ok=True)
        with cp.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["row", "label", *qs.keys()])
            for i, item, ans in rows:
                w.writerow([i + 1, label(i, item), *cells(ans)])

    out = [f"{len(items)} items x {len(qs)} questions. noul = P(yes); choice = label (confidence); "
           "score = level 0..n (confidence). Treat values as rankings.", "label | " + " | ".join(qs)]
    for i, item, ans in rows[:max(1, top_n)]:
        out.append(label(i, item) + " | " + " | ".join(cells(ans)))
    if len(rows) > top_n:
        out.append(f"... {len(rows) - top_n} more rows" + (f" (all in {out_csv})" if out_csv else ""))
    if total > MAX_MAP_ITEMS:
        warnings.append(f"only the first {MAX_MAP_ITEMS} of {total} items were processed")
    if truncated:
        warnings.append(f"{truncated} item(s) were too long and were truncated")
    if errors:
        warnings.append(f"{len(errors)} item(s) failed ({errors[0]}) - rows marked ERROR; judge those yourself "
                        "or re-run (successful rows are cached)")
    out += [f"WARNING: {w}" for w in warnings]
    body = "\n".join(out)
    item_tokens = sum(_tok(x if isinstance(x, str) else json.dumps(x, default=str)) for x in items)
    avoided = 0 if inline else item_tokens - _tok(body) - CALL_OVERHEAD_TOKENS   # inline = Claude already had it
    return body + "\n\n" + _finish(run, "partial" if errors else "ok", avoided, note)


def ask_impl(questions: dict | str, state: Any = "", state_path: str = "") -> str:
    run = Run("ask")
    try:
        qs = parse_questions(questions)
    except QuestionError as e:
        return f"QUESTION ERROR: {e} (Jev was not called - fix the questions)"
    note = ",".join(qs)
    if state_path:
        p = resolve_path(state_path)
        if _is_secret_file(p):
            return _fallback(run, f"refusing to send a likely-secret file ({p.name}) to TypeSafe", note)
        reason = _disabled_reason([p])
        if reason:
            return _fallback(run, reason, note)
        st: Any = _read_text(p)
    elif isinstance(state, str):
        try:
            st = json.loads(state)
        except (json.JSONDecodeError, TypeError):
            st = state
    else:
        st = state
    if st in ("", None) or (isinstance(st, str) and not st.strip()) or st == {} or st == []:
        return "ERROR: empty state - pass --state TEXT, --state-path FILE, or `--state -` to read stdin."
    raw_tokens = _tok(st if isinstance(st, str) else json.dumps(st, default=str))
    st, cut = _fit_state(_scrub_obj(st))
    answers, err = _safe(run, lambda _: call_jev(run, st, qs))(None)
    if answers is None:
        return _fallback(run, err, note)
    body = json.dumps(answers, indent=1)
    extra = _question_warnings(qs) + (["state was too long and was truncated"] if cut else [])
    body += "".join(f"\nWARNING: {w}" for w in extra)
    avoided = raw_tokens - _tok(body) - CALL_OVERHEAD_TOKENS if state_path else 0
    return body + "\n\n" + _finish(run, "ok", avoided, note, {"answers": answers})


# ======================================================== account controls ===

def check_impl() -> str:
    """Live 1-question call (never cached): verifies the key and credit, resets the breaker."""
    reason = _disabled_reason()
    if reason:
        return f"Jev is {reason}. Run `jev on` to turn it back on."
    try:
        resp = _get_client().system_one(
            state="jev health check", questions={"ok": {"type": "noul", "instructions": "Is this a health check?"}})
    except Exception as e:  # noqa: BLE001
        problem = _account_problem(e)
        if problem:
            _raise_alert(*problem)
            return _alert_block() or f"Jev check failed: {problem[1]}"
        if _is_outage(e):
            return f"Jev check failed - TypeSafe seems unreachable right now ({_friendly(e)}). Try again shortly."
        return f"Jev check failed: {_friendly(e)}"
    _breaker_record(True)
    had = clear_alert()
    model = getattr(resp, "model", "") or MODEL
    return f"Jev OK - key and credit work (model {model})." + (" Alert cleared." if had else "")


def key_impl() -> str:
    """CLI only: read a new key with hidden input, save it (mode 600), then check it."""
    import getpass
    print(f"Create a key at {KEYS_URL}")
    key = getpass.getpass("Paste the TypeSafe API key (input hidden): ").strip()
    if not key:
        return "No key entered - nothing changed."
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_DIR / ".api_key.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    os.replace(tmp, CONFIG_DIR / "api_key")
    global _client
    _client = None                                       # pick up the new key
    if os.environ.get("TYPESAFE_API_KEY"):
        print("NOTE: TYPESAFE_API_KEY is set in your environment and takes priority over the saved file.")
    return "Saved to ~/.config/typesafe/api_key.\n" + check_impl()


def off_impl() -> str:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / "disabled").touch()
    clear_alert()
    return "Jev is OFF - Claude works normally (no Jev calls, no alerts). Run `jev on` to resume."


def on_impl() -> str:
    try:
        (CONFIG_DIR / "disabled").unlink()
    except OSError:
        pass
    return "Jev is ON.\n" + check_impl()


def dismiss_impl() -> str:
    a = _read_alert()
    if not a:
        return "No active Jev alert."
    a["dismissed_until"] = time.time() + 12 * 3600
    ALERT_PATH.write_text(json.dumps(a))
    for f in (ALERT_HOOK_PATH, ALERT_HOOK_PATH.with_name(ALERT_HOOK_PATH.name + ".once")):
        try:
            f.unlink()
        except OSError:
            pass
    return "Alert hidden for 12 hours. Claude keeps working without Jev until it's fixed (`jev check` to retest)."


def alert_status() -> str:
    a = _read_alert()
    if (CONFIG_DIR / "disabled").exists():
        return "Status: Jev is OFF (`jev on` to resume)."
    if not a:
        return "Status: OK - no account alerts."
    hidden = " (hidden until " + time.strftime("%H:%M", time.localtime(a["dismissed_until"])) + ")" \
        if a.get("dismissed_until", 0) > time.time() else ""
    return f"Status: ALERT{hidden} - " + _alert_text(a["kind"], a["reason"])


# ============================================================ usage/review ===

def usage_impl(days: int = 30) -> str:
    with db() as con:
        rows = con.execute(
            "SELECT tool, COUNT(*), SUM(requests), SUM(cached), SUM(tokens), SUM(cost), SUM(avoided) "
            "FROM calls WHERE ts>? GROUP BY tool ORDER BY tool", (time.time() - days * 86400,)).fetchall()
    if not rows:
        return "No Jev usage logged yet."
    out = [f"Jev usage, last {days} days (today ${_spent_today():.4f} of ${DAILY_BUDGET_USD:.2f} budget):",
           "tool | calls | requests (cached) | jev tokens | jev $ | est. Claude tokens saved (net, conservative)"]
    for tool, calls, req, cached, tok, cost, avoided in rows:
        out.append(f"{tool} | {calls} | {req} ({cached}) | {tok:,} | ${cost:.4f} | {avoided:,}")
    out.append(f"TOTAL: ${sum(r[5] for r in rows):.4f} on Jev; ~{sum(r[6] for r in rows):,} Claude tokens saved "
               "(estimate - an A/B on real tasks is the true test).")
    return "\n".join(out)


# ======================================================== self-evolving loop ===

FEEDBACK_OUTCOMES = {
    "find": {"good": "hits contained the answer", "bad": "answer was in the file but hits missed it",
             "absent": "answer is not in this file"},
    "rank": {"good": "the right file was in the top results", "bad": "the right file was ranked low/missing"},
    "prune": {"good": "the shown lines were enough", "bad": "had to read the full log"},
    "map": {"good": "results looked right", "bad": "some results were wrong"},
    "ask": {"good": "answers looked right", "bad": "answers were wrong"},
}


def _resolve_id(call_id: int | str, con) -> int | None:
    if str(call_id).strip().lower() in ("last", "", "0"):
        row = con.execute("SELECT id FROM calls WHERE outcome!='fallback' AND feedback IS NULL AND ts>? "
                          "ORDER BY id DESC LIMIT 1", (time.time() - 3600,)).fetchone()
        return row[0] if row else None
    try:
        return int(call_id)
    except ValueError:
        return None


def _rank_of_line(hits: list, line: int) -> int:
    for i, (n, _) in enumerate(hits, start=1):
        if abs(n - line) <= 2:
            return i
    return len(hits) + 1


def _rank_of_file(ranked: list, answer_file: str, root: str) -> int:
    cands = {str(Path(answer_file).expanduser().resolve())}
    if root and not Path(answer_file).is_absolute():
        cands.add(str((Path(root) / answer_file).resolve()))
    tail = "/" + answer_file.lstrip("./")
    for i, (name, _) in enumerate(ranked, start=1):
        if name in cands or name.endswith(tail):
            return i
    return len(ranked) + 1


def feedback_impl(call_id: int | str, outcome: str, answer_line: int = 0, answer_file: str = "",
                  note: str = "") -> str:
    with db() as con:
        cid = _resolve_id(call_id, con)
        row = con.execute("SELECT tool, detail FROM calls WHERE id=?", (cid,)).fetchone() if cid else None
        if not row:
            return f"No call {call_id} found (\"last\" = most recent un-labelled call in the past hour)."
        tool, detail = row[0], json.loads(row[1] or "{}")
        allowed = FEEDBACK_OUTCOMES.get(tool, {"good": "", "bad": ""})
        if outcome not in allowed:
            return f"For {tool}, outcome must be one of: {', '.join(allowed)}"
        rank = None
        if tool == "find" and answer_line and detail.get("hits"):
            rank = _rank_of_line(detail["hits"], answer_line)
        if tool == "rank" and answer_file and detail.get("ranked"):
            rank = _rank_of_file(detail["ranked"], answer_file, detail.get("root", ""))
        con.execute("UPDATE calls SET feedback=?, feedback_note=?, answer_rank=?, feedback_ts=? WHERE id=?",
                    (outcome, note[:300], rank, time.time(), cid))
        labelled = con.execute("SELECT COUNT(*) FROM calls WHERE feedback IS NOT NULL").fetchone()[0]
    msg = f"Recorded {outcome} for call {cid} ({tool})" + (f", answer rank {rank}" if rank else "") + "."
    if AUTOTUNE_EVERY and labelled % AUTOTUNE_EVERY == 0:
        msg += "\n" + tune_impl(dry_run=not AUTOTUNE_APPLY)
    return msg


def _pct(values: list[float], q: float) -> float:
    v = sorted(values)
    k = max(0, min(len(v) - 1, int(round(q * (len(v) - 1)))))
    return v[k]


def _clamp(key: str, value: float, old: float):
    lo, hi = LIMITS[key]
    if isinstance(DEFAULTS[key], float):
        value = min(old + MAX_STEP, max(old - MAX_STEP, value))       # small steps only
        return round(min(hi, max(lo, value)), 3)
    return int(min(hi, max(lo, value)))


def _labels() -> tuple[list[float], list[float], dict[str, list[int]]]:
    """exists values for answered (pos) / absent (neg) find cases, and answer ranks - eval + feedback,
    current model only."""
    model = _meta_get(f"resolved:{MODEL}")
    with db() as con:
        ev = con.execute("SELECT kind, absent, exists_p, answer_rank, model FROM evals").fetchall()
        fb = con.execute("SELECT tool, feedback, answer_rank, detail, models FROM calls "
                         "WHERE feedback IS NOT NULL").fetchall()

    def same(m):
        return not model or not m or model in m

    pos, neg, ranks = [], [], {"find": [], "rank": []}
    for kind, absent, ex, rnk, m in ev:
        if not same(m):
            continue
        if kind == "find" and ex is not None:
            (neg if absent else pos).append(ex)
        if rnk and kind in ranks:
            ranks[kind].append(rnk)
    for tool, f, rnk, d, m in fb:
        if not same(m):
            continue
        ex = json.loads(d or "{}").get("exists")
        if tool == "find" and ex is not None and f in ("good", "bad", "absent"):
            (neg if f == "absent" else pos).append(ex)
        if tool in ranks and rnk:
            ranks[tool].append(rnk)
    return pos, neg, ranks


def tune_impl(dry_run: bool = False) -> str:
    pos, neg, ranks = _labels()
    old = {k: tuned(k) for k in DEFAULTS}
    new, why = dict(old), []
    if len(pos) >= 20 and len(neg) >= 10:
        new["find.absent"] = _clamp("find.absent", _pct(pos, 0.05) - 0.01, old["find.absent"])
        new["find.found"] = _clamp("find.found", _pct(neg, 0.95) + 0.01, old["find.found"])
        why.append(f"thresholds from {len(pos)} answered + {len(neg)} absent cases")
    if new["find.found"] < new["find.absent"] + 0.1:
        new["find.found"] = _clamp("find.found", new["find.absent"] + 0.1, old["find.found"])
    for tool in ("find", "rank"):
        r = ranks[tool]
        if len(r) >= 10:
            new[f"{tool}.top_k"] = _clamp(f"{tool}.top_k", _pct(r, 0.9), old[f"{tool}.top_k"])
            why.append(f"{tool}.top_k from {len(r)} answer ranks")
    if not why:
        return (f"Tune: not enough labels yet ({len(pos)} answered / {len(neg)} absent find cases, "
                f"{len(ranks['find'])}/{len(ranks['rank'])} find/rank ranks; need 20/10 and 10). "
                "Fastest way: `jev eval golden.jsonl`.")
    changes = [f"  {k}: {old[k]} -> {new[k]}" for k in DEFAULTS if new[k] != old[k]]
    if not changes:
        return f"Tune ({'; '.join(why)}): no changes needed."
    head = "Tune PROPOSAL (not applied - run `jev tune` to apply)" if dry_run else "Tuned"
    if not dry_run:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        TUNING_PATH.write_text(json.dumps({"values": new, "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                           "basis": why, "model": _meta_get(f"resolved:{MODEL}")}, indent=2))
        with (CONFIG_DIR / "tuning-history.jsonl").open("a") as f:
            f.write(json.dumps({"ts": time.time(), "old": old, "new": new, "basis": why}) + "\n")
    return f"{head} ({'; '.join(why)}):\n" + "\n".join(changes)


def eval_impl(golden_path: str, apply: bool = False) -> str:
    """Golden set JSONL rows:
      {"find": "/abs/file", "query": "...", "line": 812}      answer is at/near line 812
      {"find": "/abs/file", "query": "...", "absent": true}   answer is not in the file
      {"rank": "/abs/root", "pattern": "**/*.ts", "query": "...", "file": "src/x.ts"}
    Fits thresholds on one half and reports how they do on the other half."""
    rows = [json.loads(ln) for ln in _read_text(resolve_path(golden_path)).split("\n") if ln.strip()]
    rows.sort(key=lambda r: hashlib.md5(json.dumps(r, sort_keys=True).encode()).hexdigest())
    recs, failures = [], 0
    for r in rows:
        run = Run("eval")
        try:
            if "find" in r:
                core = _find_core(run, _read_text(resolve_path(r["find"])), r["query"])
                if not core["ok"]:
                    failures += 1
                    continue
                rank = None if r.get("absent") else _rank_of_line([[n, s] for s, n in core["hits"]], int(r["line"]))
                recs.append(("find", r["query"], r["find"], bool(r.get("absent")), core["best_exists"], rank))
            elif "rank" in r:
                out = rank_impl(r["query"], root=r["rank"], pattern=r.get("pattern", "**/*"), top_k=50)
                ranked = [ln.split("  ")[1] for ln in out.split("\n")[1:] if re.match(r"^\d\.\d\d  ", ln)]
                rank = _rank_of_file([[x, 0] for x in ranked], r["file"], r["rank"])
                recs.append(("rank", r["query"], r["rank"], False, None, rank))
        except Exception as e:  # noqa: BLE001
            failures += 1
            run.add_error(e)
    model = _meta_get(f"resolved:{MODEL}") or MODEL
    with db() as con:
        con.executemany("INSERT INTO evals VALUES (?,?,?,?,?,?,?,?)",
                        [(time.time(), k, q, t, int(a), ex, rk, model) for k, q, t, a, ex, rk in recs])
    half = len(recs) // 2
    fit, val = recs[:half], recs[half:]
    out = [f"jev eval: {len(recs)} cases ({failures} could not run), model {model}"]

    def hit_at(kind, k):
        rr = [x[5] for x in recs if x[0] == kind and x[5] is not None]
        return f"{sum(1 for x in rr if x <= k)}/{len(rr)}" if rr else "-"

    for kind, ks in (("find", (1, 3, 8)), ("rank", (1, 3, 10))):
        if any(x[0] == kind for x in recs):
            out.append(f"{kind}: " + ", ".join(f"hit@{k} {hit_at(kind, k)}" for k in ks))
    fpos = [x[4] for x in fit if x[0] == "find" and not x[3]]
    fneg = [x[4] for x in fit if x[0] == "find" and x[3]]
    if len(fpos) >= 5 and len(fneg) >= 3:
        absent = round(min(0.35, max(0.10, _pct(fpos, 0.05) - 0.01)), 3)
        found = round(min(0.95, max(0.6, absent + 0.1, _pct(fneg, 0.95) + 0.01)), 3)
        vpos = [x[4] for x in val if x[0] == "find" and not x[3]]
        vneg = [x[4] for x in val if x[0] == "find" and x[3]]
        out.append(f"thresholds fitted on half 1: absent={absent}, found={found}")
        if vpos:
            out.append(f"  half 2: wrongly 'NOT in file' on {sum(1 for x in vpos if x < absent)}/{len(vpos)} "
                       "answered cases")
        if vneg:
            out.append(f"  half 2: wrongly 'answered' on {sum(1 for x in vneg if x >= found)}/{len(vneg)} absent cases")
    else:
        out.append("Add more rows (>=10 answered + >=6 absent find cases) to fit and validate thresholds.")
    out.append("Results stored for `jev tune`" + (" - applying now:" if apply else "."))
    if apply:
        out.append(tune_impl(dry_run=False))
    return "\n".join(out)


def review_impl(days: int = 30) -> str:
    since = time.time() - days * 86400
    with db() as con:
        rows = con.execute("SELECT tool, outcome, errors, cached, requests, feedback, models FROM calls "
                           "WHERE ts>?", (since,)).fetchall()
        br = con.execute("SELECT trips FROM breaker WHERE id=1").fetchone()
        n_eval = con.execute("SELECT COUNT(*) FROM evals").fetchone()[0]
    if not rows:
        return alert_status() + "\nNothing to review yet."
    head = f"jev review - last {days} days, {len(rows)} calls (v{VERSION}, model setting {MODEL})"
    out, recs = [alert_status(), head], []
    errors: dict[str, int] = {}
    for r in rows:
        for k, v in json.loads(r[2] or "{}").items():
            errors[k] = errors.get(k, 0) + v
    out.append("tool | calls | fallback | partial | low-conf | cache hits | feedback good/bad/absent")
    for tool in sorted({r[0] for r in rows}):
        rs = [r for r in rows if r[0] == tool]
        n = len(rs)

        def fb(o, rs=rs):
            return sum(1 for r in rs if r[5] == o)

        fallback = sum(1 for r in rs if r[1] == "fallback")
        out.append(f"{tool} | {n} | {fallback} | {sum(1 for r in rs if r[1] == 'partial')} | "
                   f"{sum(1 for r in rs if r[1] == 'low_confidence')} | "
                   f"{sum(r[3] for r in rs)}/{sum(r[4] for r in rs)} | "
                   f"{fb('good')}/{fb('bad')}/{fb('absent')}")
        if fb("bad") >= 3:
            recs.append(f"{tool}: {fb('bad')} reported misses. Phrase queries as one concrete question; "
                        "for find raise -k; add these cases to your golden set and run `jev eval`.")
        if n >= 5 and fallback / n > 0.1:
            recs.append(f"{tool}: {fallback}/{n} calls fell back to normal Claude work - see errors below.")
    if errors:
        out.append("errors: " + ", ".join(f"{k} x{v}" for k, v in sorted(errors.items(), key=lambda kv: -kv[1])))
    if br and br[0]:
        out.append(f"circuit breaker tripped {br[0]} time(s) in total")
    models = sorted({m for r in rows for m in (r[6] or "").split(",") if m})
    out.append("model versions seen: " + (", ".join(models) or "-"))
    if errors.get("TypeSafeRateLimitError"):
        recs.append(f"Rate limited: lower JEV_WORKERS (now {WORKERS}) to 3-4.")
    if errors.get("TypeSafeAuthenticationError") or errors.get("TypeSafePermissionDeniedError"):
        recs.append("Key rejected or out of credit: check console.typesafe.ai/settings/keys and /settings/billing.")
    if errors.get("TypeSafeBadRequestError") or errors.get("TypeSafeUnprocessableEntityError"):
        recs.append("Requests rejected (400/422): usually input too long or an invalid question - check recent calls.")
    if errors.get("JevOff"):
        recs.append("Some calls were skipped (no key, kill switch, .jevoff, pause or daily budget) - "
                    "see FALLBACK messages; raise JEV_DAILY_BUDGET_USD if the budget is the cause.")
    if len(models) > 1:
        recs.append("Jev model version changed - re-run `jev eval`; pin TYPESAFE_DEFAULT_MODEL once happy.")
    if not n_eval:
        recs.append("No golden eval yet: mocked tests prove the code, not Jev's accuracy. Write 20-30 real "
                    "cases from your repos and run `jev eval golden.jsonl`.")
    try:
        t = json.loads(TUNING_PATH.read_text())
        out.append(f"tuning: {t['values']} (updated {t['updated']})")
    except (OSError, ValueError, KeyError):
        out.append(f"tuning: defaults {DEFAULTS}")
    out.append("\nRecommendations:" if recs else "\nRecommendations: none - all healthy.")
    out.extend(f"- {r}" for r in recs)
    return "\n".join(out)


# ============================================================== MCP server ===

def build_server():
    """Imported lazily so the CLI starts fast. Tools run in worker threads so a long call never
    blocks the server's event loop (pings, cancels and parallel calls keep working)."""
    import anyio
    from mcp.server.fastmcp import FastMCP

    srv = FastMCP("jev")

    async def th(fn: Callable, *a):
        def guarded():
            try:
                return fn(*a)
            except Exception as e:  # noqa: BLE001 - always return text to the model
                return f"ERROR: {type(e).__name__}: {e}"
        return await anyio.to_thread.run_sync(guarded)

    @srv.tool()
    async def jev_find(path: str, query: str, top_k: int = 0, context_lines: int = 2) -> str:
        """Find the lines in a LARGE text file (>~1,000 lines or ~40KB: code, logs, docs, transcripts, CSV)
        that answer a plain-language question, without reading the whole file. Use when there is no obvious
        keyword to grep. The hits are shown in the result - don't re-read them. path: absolute path on the
        Mac (Cowork /mnt/<folder>/... paths are mapped automatically)."""
        return await th(find_impl, path, query, top_k, context_lines)

    @srv.tool()
    async def jev_prune(path: str, query: str) -> str:
        """Show only the parts of a long log/output FILE that matter for `query` (error lines, head and tail
        are always kept). The full text stays on disk and the result says where."""
        return await th(lambda: prune_impl(_read_text(resolve_path(path)), query, Path(path).name))

    @srv.tool()
    async def jev_rank(query: str, root: str = "", pattern: str = "**/*", paths: list[str] | None = None,
                       top_k: int = 0) -> str:
        """Rank files under a project folder by how likely each holds what you're looking for, then open only
        the top 2-3. Use when more than ~5 files could be relevant or you don't know the keyword. root: an
        absolute project folder. Dotfiles, secrets, .gitignored and vendor files are skipped."""
        return await th(rank_impl, query, root, pattern, paths, top_k)

    @srv.tool()
    async def jev_map(questions: dict[str, Any] | str, items_path: str = "", items_json: list[Any] | str = "",
                      id_field: str = "", context: str = "", sort_by: str = "", descending: bool = True,
                      top_n: int = 50, out_csv: str = "") -> str:
        """Run the same typed questions over EVERY item of a list FILE (.json/.jsonl/.csv/.tsv/.txt) and get a
        compact table. Saves tokens only when the items are on disk and you haven't read them.
        questions: {"id": {"type": "noul"|"choice"|"score", "instructions": "full question", "criteria": ...}}
        noul criteria optional {"true":..,"false":..}; choice {"option": "meaning"|null} (2-255, add "other");
        score: ordered list of 2-10 levels described as situations (level 0 = first).
        sort_by: a noul/score id, or choice_id=label."""
        return await th(map_impl, questions, items_path, items_json, id_field, context, sort_by, descending,
                        top_n, out_csv)

    @srv.tool()
    async def jev_ask(questions: dict[str, Any] | str, state: Any = "", state_path: str = "") -> str:
        """One Jev call: typed questions (format as in jev_map) about a state (text/JSON, or a file via
        state_path). Saves tokens only with state_path on a file you haven't read."""
        return await th(ask_impl, questions, state, state_path)

    @srv.tool()
    async def jev_feedback(call_id: int | str, outcome: str, answer_line: int = 0, answer_file: str = "",
                           note: str = "") -> str:
        """Record a Jev MISS so it can improve: find -> bad (+answer_line) or absent; rank -> bad
        (+answer_file); prune -> bad. call_id is the id in the result footer, or "last"."""
        return await th(feedback_impl, call_id, outcome, answer_line, answer_file, note)

    @srv.tool()
    async def jev_status(days: int = 30) -> str:
        """Jev account status/alerts, usage, spend, estimated tokens saved, failure patterns, recommendations."""
        return await th(lambda: usage_impl(days) + "\n\n" + review_impl(days))

    @srv.tool()
    async def jev_control(action: str) -> str:
        """Act on a Jev alert or the user's request: "check" (test key + credit now, clears a fixed alert),
        "off" (pause Jev; Claude works normally), "on" (resume + check), "dismiss" (hide the alert 12h).
        To change the API key the user runs `jev key` in Terminal - never ask for the key in chat."""
        fns = {"check": check_impl, "off": off_impl, "on": on_impl, "dismiss": dismiss_impl}
        if action not in fns:
            return "action must be one of: check, off, on, dismiss"
        return await th(fns[action])

    return srv


# ================================================================ setup UX ===
# `jev setup` (run by install.sh), `jev doctor`, `jev uninstall`. Pure stdlib, testable, idempotent.
# Every file we change is backed up first (*.bak.<timestamp>) and listed in the summary.

GITHUB_URL = "https://github.com/Dinesh-Sunny/jev-tokensaver"
MD_START, MD_END = "<!-- jev-tokensaver:start -->", "<!-- jev-tokensaver:end -->"
CLAUDE_MD_BLOCK = f"""{MD_START}
## jev token saver (https://github.com/Dinesh-Sunny/jev-tokensaver)
- Long command output (tests, builds, logs): `set -o pipefail; <cmd> 2>&1 | jev prune "what I need to know"`.
- Text file over ~1,000 lines, no obvious grep keyword: `jev find FILE "question"`; read beyond the hits only if needed.
- More than ~5 candidate files: `jev rank "what I need" --root DIR --pattern GLOB`, then open the top 2-3.
- Output starting with FALLBACK: work normally. Any JEV ALERT: start your reply with it and its options.
{MD_END}"""


class UI:
    """Tiny terminal UI: colours only on a TTY and never with NO_COLOR; prompts work under curl | bash."""

    def __init__(self, assume_yes: bool = False, stream=None):
        self.out = stream or sys.stdout
        tty = hasattr(self.out, "isatty") and self.out.isatty()
        self.color = tty and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
        ci = any(os.environ.get(v) for v in ("CI", "NONINTERACTIVE", "JEV_NONINTERACTIVE"))
        self.interactive = not assume_yes and not ci and (sys.stdin.isatty() or self._tty_ok())
        self.reason = ("--yes" if assume_yes else "CI/NONINTERACTIVE is set" if ci else "no terminal") \
            if not self.interactive else ""

    @staticmethod
    def _tty_ok() -> bool:
        try:
            with open("/dev/tty"):
                return True
        except OSError:
            return False

    def _c(self, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if self.color else s

    def say(self, s: str = "") -> None:
        print(s, file=self.out, flush=True)

    def title(self, s: str) -> None:
        self.say("\n" + self._c("1", s))

    def step(self, i: int, n: int, s: str) -> None:
        self.say("\n" + self._c("1;34", f"[{i}/{n}]") + " " + self._c("1", s))

    def ok(self, s: str) -> None:
        self.say("  " + self._c("32", "✓") + " " + s)

    def warn(self, s: str) -> None:
        self.say("  " + self._c("33", "!") + " " + s)

    def bad(self, s: str) -> None:
        self.say("  " + self._c("31", "✗") + " " + s)

    def note(self, s: str) -> None:
        self.say("    " + self._c("2", s))

    def _input(self, prompt: str) -> str | None:
        if not self.interactive:
            return None
        try:
            if sys.stdin.isatty():
                return input(prompt)
            with open("/dev/tty") as tty:                 # curl | bash: stdin is the script, ask the terminal
                self.out.write(prompt)
                self.out.flush()
                return tty.readline().rstrip("\n")
        except (OSError, EOFError, KeyboardInterrupt):
            return None

    def yes_no(self, question: str, default: bool) -> bool:
        ans = self._input(f"  ? {question} {'[Y/n]' if default else '[y/N]'} ")
        if ans is None or not ans.strip():
            return default
        return ans.strip().lower().startswith("y")

    def secret(self, prompt: str) -> str | None:
        if not self.interactive:
            return None
        import getpass
        try:
            return getpass.getpass(f"  ? {prompt}").strip()  # getpass reads /dev/tty itself
        except (EOFError, KeyboardInterrupt, OSError):
            return None


def _paths() -> dict[str, Path]:
    home = Path.home()
    desktop = (home / "Library" / "Application Support" / "Claude" if sys.platform == "darwin"
               else home / ".config" / "Claude")
    return {"claude_dir": home / ".claude", "skill_dir": home / ".claude" / "skills" / "jev",
            "settings": home / ".claude" / "settings.json", "claude_md": home / ".claude" / "CLAUDE.md",
            "app_dir": home / ".local" / "share" / "jev-tokensaver",
            "hook": home / ".local" / "share" / "jev-tokensaver" / "alert-hook.sh",
            "legacy_app_dir": home / ".local" / "share" / "jev",
            "desktop_dir": desktop, "desktop_cfg": desktop / "claude_desktop_config.json"}


def _backup(p: Path, backups: list[str]) -> None:
    if p.exists():
        b = p.with_name(f"{p.name}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
        b.write_bytes(p.read_bytes())
        backups.append(str(b))


def _read_json(p: Path) -> dict:
    try:
        text = p.read_text()
        return json.loads(text) if text.strip() else {}
    except FileNotFoundError:
        return {}


def _write_json_atomic(p: Path, data: dict) -> None:
    import tempfile
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, p)


def _mask(key: str) -> str:
    return f"…{key[-4:]}" if len(key) > 8 else "…"


def _save_key(key: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    tmp = CONFIG_DIR / ".api_key.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    os.replace(tmp, CONFIG_DIR / "api_key")
    global _client
    _client = None


def _is_legacy_wrapper(path: Path) -> bool:
    try:
        return b"generated by install.sh" in path.read_bytes()[:300]
    except OSError:
        return False


def _jev_command() -> list[str]:
    """Absolute command Claude Desktop should launch (it doesn't inherit your shell PATH)."""
    import shutil
    exe = shutil.which("jev")
    if exe and not _is_legacy_wrapper(Path(exe)):
        return [str(Path(exe)), "serve"]
    uv = shutil.which("uv") or "uv"
    return [uv, "run", "--quiet", "--script", str(Path(__file__).resolve()), "serve"]


def _is_our_hook(cmd: str) -> bool:
    return cmd.endswith("alert-hook.sh") and ("jev-tokensaver" in cmd or "/share/jev/" in cmd)


def _strip_md_block(text: str) -> str:
    if MD_START in text and MD_END in text:
        a, b = text.index(MD_START), text.index(MD_END) + len(MD_END)
        text = text[:a].rstrip("\n") + ("\n" if text[:a].strip() else "") + text[b:].lstrip("\n")
    legacy = "\n## jev token saver\n"                      # block from the pre-release installer
    if legacy in "\n" + text:
        a = ("\n" + text).index(legacy)
        rest = ("\n" + text)[a + len(legacy):]
        nxt = rest.find("\n## ")
        text = (("\n" + text)[:a] + (rest[nxt:] if nxt >= 0 else "\n")).lstrip("\n")
    return text


def install_hook(p: dict, backups: list[str]) -> bool:
    p["app_dir"].mkdir(parents=True, exist_ok=True)
    p["hook"].write_text(HOOK_SH)
    p["hook"].chmod(0o755)
    cfg = _read_json(p["settings"])
    groups = cfg.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    cmd = str(p["hook"])
    present = any(h.get("command") == cmd for g in groups for h in g.get("hooks", []))
    stale = any(_is_our_hook(h.get("command", "")) and h.get("command") != cmd
                for g in groups for h in g.get("hooks", []))
    if present and not stale:
        return False
    _backup(p["settings"], backups)
    for g in groups:
        g["hooks"] = [h for h in g.get("hooks", []) if not _is_our_hook(h.get("command", ""))]
    groups[:] = [g for g in groups if g.get("hooks")]
    groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
    _write_json_atomic(p["settings"], cfg)
    return True


def remove_hook(p: dict, backups: list[str]) -> bool:
    cfg = _read_json(p["settings"])
    groups = cfg.get("hooks", {}).get("UserPromptSubmit", [])
    if not any(_is_our_hook(h.get("command", "")) for g in groups for h in g.get("hooks", [])):
        return False
    _backup(p["settings"], backups)
    for g in groups:
        g["hooks"] = [h for h in g.get("hooks", []) if not _is_our_hook(h.get("command", ""))]
    cfg["hooks"]["UserPromptSubmit"] = [g for g in groups if g.get("hooks")]
    if not cfg["hooks"]["UserPromptSubmit"]:
        del cfg["hooks"]["UserPromptSubmit"]
    if not cfg["hooks"]:
        del cfg["hooks"]
    _write_json_atomic(p["settings"], cfg)
    return True


def install_md_rule(p: dict, backups: list[str]) -> bool:
    old = p["claude_md"].read_text() if p["claude_md"].exists() else ""
    new = _strip_md_block(old).rstrip("\n")
    new = (new + "\n\n" if new else "") + CLAUDE_MD_BLOCK + "\n"
    if new == old:
        return False
    _backup(p["claude_md"], backups)
    p["claude_md"].parent.mkdir(parents=True, exist_ok=True)
    p["claude_md"].write_text(new)
    return True


def remove_md_rule(p: dict, backups: list[str]) -> bool:
    if not p["claude_md"].exists():
        return False
    old = p["claude_md"].read_text()
    new = _strip_md_block(old)
    if new == old:
        return False
    _backup(p["claude_md"], backups)
    p["claude_md"].write_text(new)
    return True


def install_desktop(p: dict, backups: list[str]) -> bool:
    cfg = _read_json(p["desktop_cfg"])
    cmd = _jev_command()
    entry = {"command": cmd[0], "args": cmd[1:]}
    if cfg.get("mcpServers", {}).get("jev") == entry:
        return False
    _backup(p["desktop_cfg"], backups)
    cfg.setdefault("mcpServers", {})["jev"] = entry
    _write_json_atomic(p["desktop_cfg"], cfg)
    return True


def remove_desktop(p: dict, backups: list[str]) -> bool:
    cfg = _read_json(p["desktop_cfg"])
    entry = cfg.get("mcpServers", {}).get("jev")
    if not entry or "jev" not in json.dumps(entry):
        return False
    _backup(p["desktop_cfg"], backups)
    del cfg["mcpServers"]["jev"]
    _write_json_atomic(p["desktop_cfg"], cfg)
    return True


def _claude_desktop_running() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        return subprocess.run(["pgrep", "-x", "Claude"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def setup_impl(assume_yes: bool = False, hook: bool | None = None, desktop: bool | None = None,
               claude_md: bool | None = None, new_key: bool = False, skip_check: bool = False) -> int:
    import platform
    import shutil
    ui, p, changed, backups = UI(assume_yes), _paths(), [], []
    n = 5
    ui.title(f"jev-tokensaver {VERSION} setup - save Claude tokens with TypeSafe Jev")
    if not ui.interactive:
        ui.note(f"Running non-interactively ({ui.reason}): using defaults and flags.")

    ui.step(1, n, "Checking your system")
    ui.ok(f"{platform.system()} {platform.machine()}, Python {platform.python_version()}")
    has_code = bool(shutil.which("claude")) or p["claude_dir"].exists()
    has_desktop = p["desktop_dir"].exists() or Path("/Applications/Claude.app").exists()
    (ui.ok if has_code else ui.warn)("Claude Code " + ("found" if has_code else
                                                      "not found - Claude Code steps will be skipped"))
    (ui.ok if has_desktop else ui.warn)("Claude Desktop (Cowork) " + ("found" if has_desktop else
                                                                     "not found - Cowork step will be skipped"))
    exe = shutil.which("jev")
    if exe:
        ui.ok(f"`jev` is on your PATH ({exe})")
    else:
        ui.warn("`jev` is not on your PATH yet - run:  uv tool update-shell   then open a new terminal")
    if (p["legacy_app_dir"] / "jev.py").exists() and exe and not _is_legacy_wrapper(Path(exe)):
        shutil.rmtree(p["legacy_app_dir"], ignore_errors=True)       # pre-release install, now replaced
        changed.append(f"removed pre-release files {p['legacy_app_dir']}")

    ui.step(2, n, "TypeSafe API key")
    existing = _api_key()
    env_key = os.environ.get("JEV_API_KEY", "").strip()
    if env_key and (new_key or not existing):
        _save_key(env_key)
        changed.append("saved API key from JEV_API_KEY")
        ui.ok(f"Saved key from JEV_API_KEY ({_mask(env_key)}) to {CONFIG_DIR / 'api_key'} (only you can read it)")
    elif existing and not new_key:
        ui.ok(f"Key found ({_mask(existing)}) - keeping it. Replace any time with:  jev key")
    else:
        ui.note(f"Jev needs a TypeSafe API key. New accounts get free monthly credit. Keys: {KEYS_URL}")
        if ui.interactive and sys.platform == "darwin" and ui.yes_no("Open the keys page in your browser?", True):
            subprocess.run(["open", KEYS_URL], capture_output=True)
        key = ui.secret("Paste your API key (hidden, Enter to skip): ")
        if key:
            _save_key(key)
            changed.append("saved API key")
            ui.ok(f"Saved ({_mask(key)}) to {CONFIG_DIR / 'api_key'} - only you can read it")
        else:
            ui.warn("Skipped - Jev will stay off until you run:  jev key")

    ui.step(3, n, "Testing the key and credit")
    if skip_check:
        ui.note("Skipped (--skip-check).")
    elif not _api_key():
        ui.warn("No key yet - skipped.")
    else:
        res = check_impl()
        if res.startswith("Jev OK"):
            ui.ok(res)
        else:
            ui.bad(res.split("\n")[0][:300])
            ui.note("Setup continues; Claude simply works without Jev until this is fixed (`jev check`).")

    ui.step(4, n, "Claude Code")
    if not has_code:
        ui.note("Skipped - install Claude Code, then re-run:  jev setup")
    else:
        p["skill_dir"].mkdir(parents=True, exist_ok=True)
        skill_file = p["skill_dir"] / "SKILL.md"
        if not skill_file.exists() or skill_file.read_text() != SKILL_MD:
            skill_file.write_text(SKILL_MD)
            changed.append(f"installed skill {skill_file}")
        ui.ok("Skill installed - Claude knows when (and when not) to use jev")
        want_hook = hook if hook is not None else ui.yes_no(
            "Warn me immediately if Jev runs out of credit or the key expires? (adds a tiny hook)", True)
        if want_hook:
            if install_hook(p, backups):
                changed.append(f"added alert hook to {p['settings']}")
            ui.ok("Account alerts on - you'll see a ⚠️ warning on your next message if Jev stops")
        elif remove_hook(p, backups):
            changed.append("removed alert hook")
        ui.note("Optional: a 5-line rule in ~/.claude/CLAUDE.md makes Claude use jev without being asked.")
        ui.note("It applies to ALL projects - drop an empty `.jevoff` file into any folder to exclude it.")
        want_md = claude_md if claude_md is not None else ui.yes_no("Add the rule to ~/.claude/CLAUDE.md?", False)
        if want_md:
            if install_md_rule(p, backups):
                changed.append(f"added jev rule to {p['claude_md']}")
            ui.ok("Rule added (marked block - `jev uninstall` removes it cleanly)")
        elif claude_md is False and remove_md_rule(p, backups):
            changed.append("removed jev rule from CLAUDE.md")

    ui.step(5, n, "Claude Desktop / Cowork")
    if not has_desktop:
        ui.note("Skipped - Claude Desktop not found (Cowork needs it). Re-run `jev setup` after installing.")
    else:
        want = desktop if desktop is not None else ui.yes_no("Add jev to Claude Desktop so Cowork can use it?", True)
        if want:
            if _claude_desktop_running() and ui.interactive:
                ui._input("  Claude Desktop is open - quit it (Cmd+Q), then press Enter ")
            if install_desktop(p, backups):
                changed.append(f"registered MCP server 'jev' in {p['desktop_cfg']}")
            ui.ok("Registered - (re)open Claude Desktop to load it")
        elif remove_desktop(p, backups):
            changed.append("removed jev from Claude Desktop")

    ui.title("All set." if _api_key() else "Installed - add your key to switch Jev on:  jev key")
    if changed:
        ui.say("  Changed:")
        for c in changed:
            ui.note("• " + c)
    if backups:
        ui.say("  Backups (in case you want to undo):")
        for b in backups:
            ui.note("• " + b)
    ui.say("\n  Try it:")
    ui.note('npm test 2>&1 | jev prune "which tests failed and why"')
    ui.note('jev find path/to/big-file.ts "where is the refund calculated?"')
    ui.say("  Health check:  jev doctor      Measure accuracy on your code:  jev eval golden.jsonl")
    ui.say(f"  Uninstall:     jev uninstall   Docs: {GITHUB_URL}\n")
    return 0


def doctor_impl(offline: bool = False) -> int:
    import shutil
    ui, p, problems = UI(True), _paths(), 0
    ui.title(f"jev doctor - jev-tokensaver {VERSION}")

    def check(ok: bool, good: str, bad: str, fix: str = "", warn_only: bool = False) -> None:
        nonlocal problems
        if ok:
            ui.ok(good)
            return
        (ui.warn if warn_only else ui.bad)(bad)
        if fix:
            ui.note("fix: " + fix)
        problems += 0 if warn_only else 1

    exe = shutil.which("jev")
    check(bool(exe), f"jev on PATH ({exe})", "jev is not on your PATH",
          "uv tool update-shell, then open a new terminal")
    check(bool(shutil.which("uv")), "uv installed", "uv not found", "brew install uv", warn_only=True)
    key = _api_key()
    check(bool(key), f"API key present ({_mask(key or '')})", "no API key", "jev key")
    kf = CONFIG_DIR / "api_key"
    if kf.exists():
        check(kf.stat().st_mode & 0o077 == 0, "key file is private (600)", "key file readable by others",
              f"chmod 600 {kf}")
    if (CONFIG_DIR / "disabled").exists():
        ui.warn("Jev is switched OFF (`jev on` to resume)")
    if key and not offline:
        res = check_impl()
        hint = ("check your internet connection, then: jev check" if "unreachable" in res
                else "follow the options above, then: jev check")
        check(res.startswith("Jev OK"), res, res.split("\n")[0][:200], hint)
    a = _read_alert()
    check(not a or a.get("kind") == "low_credit", "no account alerts",
          f"alert: {a['reason'] if a else ''}", "jev status", warn_only=False)
    if p["claude_dir"].exists():
        sf = p["skill_dir"] / "SKILL.md"
        check(sf.exists() and sf.read_text() == SKILL_MD, "Claude Code skill installed and current",
              "Claude Code skill missing or outdated", "jev setup")
        cfg = _read_json(p["settings"])
        hooked = any(h.get("command") == str(p["hook"]) for g in cfg.get("hooks", {}).get("UserPromptSubmit", [])
                     for h in g.get("hooks", []))
        check(hooked and p["hook"].exists(), "account-alert hook installed",
              "account-alert hook not installed (you won't be warned if credit runs out)", "jev setup", True)
    if p["desktop_cfg"].exists():
        entry = _read_json(p["desktop_cfg"]).get("mcpServers", {}).get("jev")
        check(bool(entry) and Path(entry["command"]).exists() if entry else False,
              "Claude Desktop / Cowork: jev registered", "Claude Desktop: jev not registered (or its path moved)",
              "jev setup", warn_only=True)
    with db() as con:
        rows = con.execute("SELECT outcome FROM calls WHERE ts>?", (time.time() - 7 * 86400,)).fetchall()
    if rows:
        fb = sum(1 for (o,) in rows if o == "fallback")
        check(fb / len(rows) < 0.2, f"last 7 days: {len(rows)} calls, {fb} fell back",
              f"last 7 days: {fb}/{len(rows)} calls fell back", "jev review", warn_only=True)
    ui.say("")
    ui.say("  " + ("All good." if not problems else f"{problems} problem(s) found - see the fixes above."))
    return 1 if problems else 0


def uninstall_impl(assume_yes: bool = False, purge: bool = False, keep_cli: bool = False) -> int:
    import shutil
    ui, p, changed, backups = UI(assume_yes), _paths(), [], []
    ui.title("Uninstalling jev-tokensaver")
    if remove_hook(p, backups):
        changed.append("removed alert hook from ~/.claude/settings.json")
    if remove_md_rule(p, backups):
        changed.append("removed jev rule from ~/.claude/CLAUDE.md")
    if remove_desktop(p, backups):
        changed.append("removed jev from Claude Desktop config")
    sf = p["skill_dir"] / "SKILL.md"
    if sf.exists() and "name: jev" in sf.read_text()[:200]:
        shutil.rmtree(p["skill_dir"], ignore_errors=True)
        changed.append(f"removed skill {p['skill_dir']}")
    for d in (p["app_dir"], p["legacy_app_dir"]):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            changed.append(f"removed {d}")
    kept = False
    if purge or ui.yes_no(f"Also delete your API key, usage log and cache ({CONFIG_DIR}, {PRUNE_DIR.parent})?", False):
        for d in (CONFIG_DIR, PRUNE_DIR.parent):
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
                changed.append(f"deleted {d}")
    else:
        kept = True
    for c in changed:
        ui.ok(c)
    if not changed:
        ui.note("Nothing to remove - jev-tokensaver integrations were not installed.")
    if kept:
        ui.note(f"Kept {CONFIG_DIR} (API key + history) - delete it yourself or use --purge.")
    for b in backups:
        ui.note("backup: " + b)
    if not keep_cli and shutil.which("uv"):
        listed = subprocess.run(["uv", "tool", "list"], capture_output=True, text=True, timeout=60).stdout
        if "jev-tokensaver" in listed:
            subprocess.run(["uv", "tool", "uninstall", "jev-tokensaver"], capture_output=True, timeout=120)
            ui.ok("removed the jev command (uv tool uninstall jev-tokensaver)")
    ui.say("\n  Done. Restart Claude Desktop if it's open. Thanks for trying jev-tokensaver!\n")
    return 0


# <assets> - generated by scripts/sync_assets.py from skills/jev/SKILL.md and hooks/alert-hook.sh
SKILL_MD = '---\nname: jev\ndescription: Save Claude tokens with TypeSafe Jev - prune long command output, find the relevant lines in big files, rank many files, triage long list files - instead of reading everything into context.\n---\n\n# Jev token saver\n\n<!-- From jev-tokensaver: https://github.com/Dinesh-Sunny/jev-tokensaver -->\n\nJev (TypeSafe) is a fast, very cheap judgment model ($0.042 per 1M input tokens, ~100-500 ms). It never writes\ntext; it answers typed questions with probabilities. Here it works as a pre-reader: it decides what to SHOW you;\nnothing is deleted (full text stays on disk).\n\n## How to call it\n- **Claude Code:** the `jev` command in Bash (`jev <cmd> -h` for flags).\n- **Cowork / Claude Desktop:** MCP tools `jev_prune`, `jev_find`, `jev_rank`, `jev_map`, `jev_ask`, `jev_feedback`, `jev_status`, `jev_control`.\n- Paths must be on the user\'s machine; absolute is safest (Cowork `/mnt/<folder>/...` paths are mapped automatically).\n- If `jev` is missing or broken, carry on normally and suggest `jev doctor` (or the install command from the repo).\n\n## The one rule: it only saves tokens if the data has NOT passed through you yet\nGood: output piped straight in, files on disk you haven\'t opened, list files written by a script.\nBad: re-sending text you already read, or writing items out yourself (`items_json`, `--state "<text>"`) - that costs MORE.\n\n## When to use it\n| Situation | Do this |\n|---|---|\n| A command will print a lot (tests, build, install, logs, `git log`) | `set -o pipefail; npm test 2>&1 \\| jev prune "which tests failed and why"` |\n| A text file > ~1,000 lines / 40KB, and no obvious keyword to grep | `jev find FILE "plain-language question"` - hits are shown; read beyond them only if they don\'t answer it |\n| More than ~5 files might be relevant, or you don\'t know the keyword | `jev rank "what I\'m looking for" --root /abs/project --pattern "src/**/*.ts"` then open the top 2-3 |\n| A list FILE of many items to filter/score (events, jobs, emails, CSV rows) | `jev map --items FILE -q \'{...}\' --sort-by ID --out-csv out.csv`; read only the top rows |\n| A yes/no or category call on something on disk or piped (`git diff \\| jev ask -q \'{...}\' --state -`) | `jev ask` |\n\n## When NOT to use it\n- Small files, or when a grep keyword exists -> grep + targeted read is cheaper.\n- Exact strings, maths, counting, dates, generation, multi-step reasoning -> do it yourself / in code.\n- Files you already have in context. Images/PDFs (text only).\n- Secret files (.env, keys) - jev refuses them anyway and scrubs secrets from everything it sends.\n- Folders containing a `.jevoff` file (client/NDA code) - jev returns FALLBACK; work normally.\n\n## Reading results\n- `JEV ALERT ...` (exit 4) - or a JEV ALERT note added to the user\'s message - means an account problem (out of credit, key expired/rejected, no key).\n  START your reply with it: one or two plain lines saying Jev has stopped and why, then the numbered options it lists. Then carry on with the task without Jev.\n  If the user picks an option you can run: `jev check` (after they top up), `jev off`, `jev on`, `jev dismiss` (MCP: `jev_control`).\n  Never ask for the API key in chat - the user runs `jev key` in Terminal (hidden input).\n- `FALLBACK: ...` (exit 2): Jev wasn\'t used (down, paused, budget, no key, switched off). Work normally - never stop the task.\n- `QUESTION ERROR` / `ERROR` (exit 3): fix your input; Jev was not called.\n- `WARNING: ... NOT searched / NOT ranked / ERROR rows / truncated`: use the partial result, cover the listed parts yourself.\n- find verdicts: "answered" -> trust the hits; "partially" / "unclear" -> check, then grep/read; "probably NOT" -> likely absent, but grep if it matters.\n- Numbers are rankings, not exact probabilities (Jev is not calibrated on this data until `jev eval` is run).\n- prune: the footer gives the full log path - read it if something seems missing.\n\n## Writing questions (map / ask)\n`{"id": {"type": "noul|choice|score", "instructions": "the full question", "criteria": ...}}`\n1. One snap judgment per question; split big judgments into several small ones and combine yourself (decomposed questions score far better than one big question).\n2. The full question goes in `instructions` (the id is not sent). Be literal; put edge cases in criteria.\n3. noul: yes = the thing is true; optional criteria `{"true": "...", "false": "..."}`.\n4. choice: `{"option": "meaning" or null}`, 2-255 options; always include `"other"` or `"none"`.\n5. score: ordered list of 2-10 levels, each describing a situation; results are levels 0..n.\n6. Ask many questions in one call - extra questions are nearly free.\n\nExample (Luma events saved to a file by a script):\n```bash\njev map --items events.json --id-field url --sort-by fit --out-csv ~/Desktop/events-ranked.csv -q \'{\n "fit": {"type":"noul","instructions":"Is this a free, in-person AI or software engineering event in London?"},\n "kind": {"type":"choice","instructions":"What kind of event is this?","criteria":{"talk":null,"workshop":null,"hackathon":null,"networking":null,"other":null}},\n "depth": {"type":"score","instructions":"How technical is the content?","criteria":["No technical content","Some technical talks for a general audience","Hands-on or deeply technical, aimed at engineers"]}}\'\n```\n\n## When Jev was wrong - one short line, misses only\n- find missed: `jev feedback <id> bad --line <where it really was>`; answer not in the file: `jev feedback <id> absent`\n- rank missed: `jev feedback <id> bad --file <right file>`; prune hid what you needed: `jev feedback <id> bad`\nThe id is in the result footer `[jev id=42 ...]` (or use `last`). Don\'t log successes - it costs tokens.\nTuning only proposes changes; the user applies them (`jev tune`), and `jev eval` on a golden set is the real accuracy check.\n'  # noqa: E501
HOOK_SH = '#!/bin/sh\n# Claude Code UserPromptSubmit hook (installed by install.sh).\n# If Jev has an account problem (no credit, key rejected/expired, no key), jev writes\n# ~/.config/typesafe/alert-hook.json; this prints it so you see a warning immediately and\n# Claude leads its reply with the problem and your options. Must stay fast and never fail.\nf="$HOME/.config/typesafe/alert-hook.json"\n[ -f "$f" ] || exit 0\ncat "$f" 2>/dev/null\n[ -f "$f.once" ] && rm -f "$f" "$f.once"\nexit 0\n'  # noqa: E501
# </assets>


# ===================================================================== CLI ===

def _json_arg(v: str) -> str:
    if v and v.startswith("@"):
        return Path(v[1:]).expanduser().read_text()
    return v


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="jev", description="Cheap TypeSafe Jev judgments for Claude and scripts.")
    ap.add_argument("--version", action="version", version=f"jev {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("find", help="find the lines in a big file that answer a question")
    s.add_argument("path"); s.add_argument("query")
    s.add_argument("-k", "--top-k", type=int, default=0); s.add_argument("-c", "--context", type=int, default=2)

    s = sub.add_parser("prune", help="filter long command output from stdin (full output kept on disk)")
    s.add_argument("query")

    s = sub.add_parser("rank", help="rank files under a folder by relevance to a query")
    s.add_argument("query"); s.add_argument("paths", nargs="*", help="explicit files (else --root/--pattern)")
    s.add_argument("--root", default="."); s.add_argument("--pattern", default="**/*")
    s.add_argument("-k", "--top-k", type=int, default=0)

    s = sub.add_parser("map", help="run typed questions over every item in a list file")
    s.add_argument("-q", "--questions", required=True, help="JSON or @file.json")
    s.add_argument("--items", default="", help=".json/.jsonl/.csv/.tsv/.txt file")
    s.add_argument("--items-json", default="")
    s.add_argument("--id-field", default=""); s.add_argument("--context", default="")
    s.add_argument("--sort-by", default="", help="noul/score id, or choice_id=label")
    s.add_argument("--asc", action="store_true")
    s.add_argument("-n", "--top-n", type=int, default=50); s.add_argument("--out-csv", default="")

    s = sub.add_parser("ask", help="typed questions about --state TEXT, --state-path FILE or --state - (stdin)")
    s.add_argument("-q", "--questions", required=True, help="JSON or @file.json")
    s.add_argument("--state", default=""); s.add_argument("--state-path", default="")

    s = sub.add_parser("eval", help="measure accuracy on your golden set (JSONL)")
    s.add_argument("golden"); s.add_argument("--apply", action="store_true", help="apply the tuned values")

    s = sub.add_parser("feedback", help="record a miss: feedback <id|last> bad|absent|good [--line N] [--file F]")
    s.add_argument("call_id"); s.add_argument("outcome")
    s.add_argument("--line", type=int, default=0); s.add_argument("--file", default="")
    s.add_argument("--note", default="")

    s = sub.add_parser("tune", help="apply thresholds/top_k fitted from eval + feedback")
    s.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("review", help="failure patterns + recommendations")
    s.add_argument("--days", type=int, default=30)

    s = sub.add_parser("usage", help="spend + estimated Claude tokens saved")
    s.add_argument("--days", type=int, default=30)

    sub.add_parser("check", help="test the API key and credit now (clears a fixed alert)")
    sub.add_parser("key", help="save a new TypeSafe API key (hidden input) and test it")
    sub.add_parser("off", help="pause Jev - Claude works normally, no alerts")
    sub.add_parser("on", help="resume Jev and test it")
    sub.add_parser("dismiss", help="hide the current alert for 12 hours")
    sub.add_parser("status", help="account status / alert")
    s = sub.add_parser("setup", help="guided setup: key, Claude Code skill + alerts, Claude Desktop/Cowork")
    s.add_argument("-y", "--yes", action="store_true", help="no prompts: accept defaults")
    s.add_argument("--hook", dest="hook", action="store_true", default=None, help="add the account-alert hook")
    s.add_argument("--no-hook", dest="hook", action="store_false")
    s.add_argument("--desktop", dest="desktop", action="store_true", default=None, help="register in Claude Desktop")
    s.add_argument("--no-desktop", dest="desktop", action="store_false")
    s.add_argument("--claude-md", dest="claude_md", action="store_true", default=None,
                   help="add the 'use jev' rule to ~/.claude/CLAUDE.md")
    s.add_argument("--no-claude-md", dest="claude_md", action="store_false")
    s.add_argument("--new-key", action="store_true", help="replace the saved API key")
    s.add_argument("--skip-check", action="store_true", help="don't make the live key/credit test call")
    s = sub.add_parser("doctor", help="check the whole installation and suggest fixes")
    s.add_argument("--offline", action="store_true", help="skip the live API check")
    s = sub.add_parser("uninstall", help="remove everything jev-tokensaver added (backups kept)")
    s.add_argument("-y", "--yes", action="store_true"); s.add_argument("--purge", action="store_true",
                                                                       help="also delete the API key and history")
    s.add_argument("--keep-cli", action="store_true", help="keep the jev command itself")
    sub.add_parser("serve", help="run as an MCP server (stdio) for Claude Desktop / Cowork")

    if argv is None and len(sys.argv) == 1:
        ap.print_help()
        return 0
    a = ap.parse_args(argv)
    if a.cmd == "setup":
        return setup_impl(a.yes, a.hook, a.desktop, a.claude_md, a.new_key, a.skip_check)
    if a.cmd == "doctor":
        return doctor_impl(a.offline)
    if a.cmd == "uninstall":
        return uninstall_impl(a.yes, a.purge, a.keep_cli)
    try:
        if a.cmd == "serve":
            build_server().run()
            return 0
        if a.cmd == "find":
            out = find_impl(a.path, a.query, a.top_k, a.context)
        elif a.cmd == "prune":
            if sys.stdin.isatty():
                print("Pipe output into it:  some-command 2>&1 | jev prune \"what matters\"", file=sys.stderr)
                return 1
            out = prune_impl(sys.stdin.read(), a.query)
        elif a.cmd == "rank":
            out = rank_impl(a.query, "" if a.paths else str(Path(a.root).expanduser().resolve()), a.pattern,
                            a.paths or None, a.top_k)
        elif a.cmd == "map":
            out = map_impl(_json_arg(a.questions), a.items, _json_arg(a.items_json), a.id_field, a.context,
                           a.sort_by, not a.asc, a.top_n, a.out_csv)
        elif a.cmd == "ask":
            state = sys.stdin.read() if a.state == "-" else a.state
            out = ask_impl(_json_arg(a.questions), state, a.state_path)
        elif a.cmd == "eval":
            out = eval_impl(a.golden, a.apply)
        elif a.cmd == "feedback":
            out = feedback_impl(a.call_id, a.outcome, a.line, a.file, a.note)
        elif a.cmd == "tune":
            out = tune_impl(a.dry_run)
        elif a.cmd == "review":
            out = review_impl(a.days)
        elif a.cmd in ("check", "key", "off", "on", "dismiss", "status"):
            out = {"check": check_impl, "key": key_impl, "off": off_impl, "on": on_impl,
                   "dismiss": dismiss_impl, "status": alert_status}[a.cmd]()
        else:
            out = usage_impl(a.days)
    except Exception as e:  # noqa: BLE001 - CLI: clean error
        print(f"jev error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(out)
    if out.startswith("JEV ALERT"):
        return 4
    if out.startswith("FALLBACK"):
        return 2
    if out.startswith(("QUESTION ERROR", "ERROR")):
        return 3
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 1:
        build_server().run()       # what Claude Desktop launches
    else:
        raise SystemExit(main())
