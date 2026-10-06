"""Offline tests for jev.py - the TypeSafe API is mocked (no key or network needed).
They prove the CODE behaves; Jev's real accuracy on your files is measured with `jev eval`.

Run:  uv run pytest            (from a checkout; uses pyproject.toml)
"""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx2
import pytest
from typesafe_sdk import RetryPolicy, TypeSafeClient

JEV_PY = Path(__file__).resolve().parent.parent / "jev.py"
NOUL = '{"a":{"type":"noul","instructions":"Is it about refunds?"}}'


class FakeJev:
    """Mock TypeSafe endpoint. Lines/items containing `keyword` score high."""

    def __init__(self, keyword="refund"):
        self.keyword, self.mode, self.fail_if, self.bodies = keyword, "up", None, []

    @property
    def requests(self):
        return len(self.bodies)

    def __call__(self, req):
        body = json.loads(req.content)
        self.bodies.append(body)
        s = json.dumps(body["state"]).lower()
        if self.mode == "down" or (self.fail_if and self.fail_if in s):
            return httpx2.Response(529, json={"error": "overloaded"})
        if self.mode == "ratelimit":
            return httpx2.Response(429, json={"error": "slow down"})
        if self.mode in ("401", "402", "403"):
            msgs = {"401": "Invalid or expired API key", "402": "Insufficient credit", "403": "Forbidden"}
            return httpx2.Response(int(self.mode), json={"detail": msgs[self.mode]})
        if self.mode == "toolong" and len(s) > 2000:
            return httpx2.Response(422, json={"detail": "state too long"})
        st = body["state"]
        judged = json.dumps(st.get("file", st.get("item", st)) if isinstance(st, dict) else st).lower()
        ans = {}
        for qid, q in body["questions"].items():
            if q["type"] == "noul":
                ans[qid] = {"type": "noul", "noul": 0.9 if self.keyword in judged else 0.05}
            elif q["type"] == "choice":
                opts = list(q["criteria"])
                if isinstance(st, str) and opts[0].startswith("L"):
                    lines = {ln.split("| ", 1)[0]: ln.lower() for ln in st.split("\n")}
                    w = [50.0 if self.keyword in lines.get(o, "") else 0.01 for o in opts]
                else:
                    w = [1.0] + [0.1] * (len(opts) - 1)
                t = sum(w)
                p = {o: x / t for o, x in zip(opts, w)}
                ans[qid] = {"type": "choice", "choice": max(p, key=p.get), "probabilities": p, "confidence": 0.8}
            else:
                n = len(q["criteria"])
                ans[qid] = {"type": "score", "score": 1.0, "confidence": 0.6,
                            "legend": {str(i): c for i, c in enumerate(q["criteria"])},
                            "probabilities": {str(i): 1 / n for i in range(n)}}
        return httpx2.Response(200, json={"model": "jev-1.13.0", "answers": ans,
                                          "usage": {"input_tokens": max(1, len(s) // 4), "output_tokens": 5}})


def load_jev(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("JEV_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("JEV_PRUNE_DIR", str(tmp_path / "prune"))
    monkeypatch.setenv("JEV_AUTOTUNE_EVERY", "0")
    monkeypatch.setenv("JEV_NO_NOTIFY", "1")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("JEV_DISABLE", raising=False)
    spec = importlib.util.spec_from_file_location(f"jev_{tmp_path.name}", JEV_PY)
    jev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(jev)
    jev._client = TypeSafeClient(api_key="test", transport=httpx2.MockTransport(fake),
                                 retry=RetryPolicy(max_retries=0))
    return jev


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = FakeJev()
    jev = load_jev(tmp_path, monkeypatch, fake)
    big = tmp_path / "big.py"
    lines = [f"def f{i}(x):\n    return x * {i}" for i in range(500)]
    lines[300] = "def compute_refund(order):\n    return order.paid - order.fees"
    big.write_text("\n".join(lines))
    return jev, fake, tmp_path, big


# ---------------------------------------------------------------- find -------

def test_find_returns_relevant_lines(env):
    jev, fake, d, big = env
    out = jev.find_impl(str(big), "where is the refund calculated?")
    assert "Verdict: answered in this file" in out and "compute_refund" in out
    assert "jev id=1" in out and "tokens saved" in out


def test_long_minified_line_is_searched_not_truncated(env):
    jev, fake, d, big = env
    f = d / "bundle.min.js"
    f.write_text("var a=1;" * 30_000 + "function refund(){return 1}" + ";var z=2" * 100)
    out = jev.find_impl(str(f), "where is refund defined?")
    assert "NOT in this file" not in out
    assert "function refund" in out and "[chars" in out
    g = d / "huge.min.js"                       # past the per-line search limit: must say unclear, not NOT
    g.write_text("var a=1;" * 51_000 + "function refund(){}")
    out = jev.find_impl(str(g), "where is refund defined?")
    assert "unclear" in out and "not searched" in out and "tokens saved" not in out


def test_line_numbers_ignore_form_feeds(env):
    jev, fake, d, big = env
    f = d / "ff.txt"
    f.write_text("\n".join(["page one\x0cstill line 1"] + [f"line {i}" for i in range(2, 400)] + ["refund here"]))
    out = jev.find_impl(str(f), "refund?", context_lines=0)
    assert "   400| refund here" in out


def test_cjk_chunks_stay_under_token_cap(env):
    jev, fake, d, big = env
    f = d / "cjk.txt"
    f.write_text("\n".join("退款政策说明" * 40 for _ in range(400)))
    jev.find_impl(str(f), "refund?")
    for b in fake.bodies:
        assert jev._tok(b["state"]) <= jev.CHUNK_MAX_TOKENS + 50


def test_too_long_chunk_is_split_and_retried(env):
    jev, fake, d, big = env
    fake.mode = "toolong"
    out = jev.find_impl(str(big), "refund?")
    assert "compute_refund" in out
    assert not fake.mode == "down"


def test_cache_makes_repeat_calls_free(env):
    jev, fake, d, big = env
    jev.find_impl(str(big), "refund?")
    before = fake.requests
    out = jev.find_impl(str(big), "refund?")
    assert fake.requests == before and "$0.00000" in out


def test_partial_failure_is_reported_and_no_false_not_verdict(env):
    jev, fake, d, big = env
    fake.keyword = "zzz-not-present"
    fake.fail_if = "x * 450"
    out = jev.find_impl(str(big), "refund?")
    assert "were NOT searched" in out
    assert "probably NOT in this file" not in out and "unclear" in out


# ------------------------------------------------------------- secrets -------

def test_secrets_are_scrubbed_before_sending(env):
    jev, fake, d, big = env
    f = d / "config.ts"
    body = ["// config"] * 200 + [
        'const stripe = "sk_live_51HxABCDEFGHIJKLMNOP";',
        "const aws = 'AKIAABCDEFGHIJKLMNOP';",
        "password = hunter2hunter2",
        "-----BEGIN RSA PRIVATE KEY-----", "MIIEowIBAAKCAQEA", "-----END RSA PRIVATE KEY-----",
        "export function refund() {}"]
    f.write_text("\n".join(body))
    out = jev.find_impl(str(f), "refund?")
    sent = json.dumps(fake.bodies)
    for secret in ("sk_live_51Hx", "AKIAABCDEFGHIJKLMNOP", "hunter2hunter2", "MIIEowIBAAKCAQEA"):
        assert secret not in sent
    assert "export function refund" in out   # line numbers intact after block redaction


def test_secret_files_are_never_sent(env):
    jev, fake, d, big = env
    (d / "proj").mkdir()
    (d / "proj" / "billing.ts").write_text("export function refund() {}")
    (d / "proj" / ".env").write_text("STRIPE=sk_live_xxxxxxxxxxxxxxxx refund")
    (d / "proj" / "id_rsa").write_text("refund key")
    (d / "proj" / "server.pem").write_text("refund")
    out = jev.rank_impl("refund logic", root=str(d / "proj"))
    sent = json.dumps(fake.bodies)
    assert "Ranked 1 files" in out and ".env" not in sent and "id_rsa" not in sent
    assert jev.find_impl(str(d / "proj" / ".env"), "refund?").startswith("FALLBACK")


def test_rank_refuses_home_and_needs_root(env, monkeypatch):
    jev, fake, d, big = env
    assert jev.rank_impl("x", root="").startswith("ERROR")
    assert "too broad" in jev.rank_impl("x", root=str(Path.home()))


# ------------------------------------------------------------- rank ----------

def test_rank_orders_skips_vendor_and_warns_on_cap(env, monkeypatch):
    jev, fake, d, big = env
    p = d / "proj"
    (p / "node_modules" / "x").mkdir(parents=True)
    (p / "node_modules" / "x" / "refund.js").write_text("refund")
    (p / "billing.ts").write_text("export function refund() {}")
    (p / "ui.ts").write_text("export const button = 1")
    out = jev.rank_impl("refund logic", root=str(p))
    assert "Ranked 2 files" in out and out.index("billing.ts") < out.index("ui.ts")
    monkeypatch.setattr(jev, "MAX_RANK_FILES", 1)
    assert "NOT ranked" in jev.rank_impl("refund logic", root=str(p))


def test_rank_sees_definitions_at_end_of_big_file(env):
    jev, fake, d, big = env
    p = d / "proj2"
    p.mkdir()
    (p / "huge.ts").write_text("// filler\n" * 3000 + "export function issueRefund() {}\n")
    (p / "other.ts").write_text("export const x = 1")
    out = jev.rank_impl("refund", root=str(p))
    assert out.index("huge.ts") < out.index("other.ts")


def test_rank_feedback_matches_absolute_and_relative_paths(env):
    jev, fake, d, big = env
    p = d / "proj3"
    (p / "src").mkdir(parents=True)
    (p / "src" / "data.ts").write_text("refund data")
    (p / "src" / "a.ts").write_text("nothing")
    jev.rank_impl("refund", root=str(p))
    msg = jev.feedback_impl("1", "bad", answer_file="src/data.ts")
    assert "answer rank 1" in msg
    jev.rank_impl("refund again", root=str(p))
    assert "answer rank 1" in jev.feedback_impl("2", "bad", answer_file=str(p / "src" / "data.ts"))


# -------------------------------------------------------- resilience ---------

def test_total_outage_falls_back_with_exit_code_2(env):
    jev, fake, d, big = env
    fake.mode = "down"
    assert jev.ask_impl(NOUL, "hello").startswith("FALLBACK")
    assert jev.main(["ask", "-q", NOUL, "--state", "hi"]) == 2


def test_breaker_trips_on_outages_only(env):
    jev, fake, d, big = env
    for _ in range(3):                                   # malformed questions: caught locally
        assert jev.ask_impl('{"a":{"type":"score","instructions":"x","criteria":[]}}', "s").startswith("QUESTION ERROR")
    fake.mode = "ratelimit"
    for i in range(3):                                   # rate limits: not an outage
        jev.ask_impl('{"a":{"type":"noul","instructions":"rl%d"}}' % i, "s")
    fake.mode = "up"
    assert "paused" not in jev.ask_impl(NOUL, "refund please")
    fake.mode = "down"
    for i in range(3):
        jev.ask_impl('{"a":{"type":"noul","instructions":"x%d"}}' % i, "s")
    sent = fake.requests
    out = jev.ask_impl('{"a":{"type":"noul","instructions":"again"}}', "s")
    assert fake.requests == sent and "paused" in out


def test_question_validation(env):
    jev, fake, d, big = env
    bad = ['{}', 'not json', '{"a":{"type":"maybe","instructions":"x"}}',
           '{"a":{"type":"choice","instructions":"x","criteria":{"only":null}}}',
           '{"a":{"type":"score","instructions":"x","criteria":["1","2","3","4","5","6","7","8","9","10","11"]}}',
           '{"a":{"type":"noul"}}']
    for q in bad:
        assert jev.ask_impl(q, "s").startswith("QUESTION ERROR"), q
    assert fake.requests == 0
    assert jev.main(["ask", "-q", "{}", "--state", "x"]) == 3


def test_budget_kill_switch_jevoff_and_no_key(env, monkeypatch):
    jev, fake, d, big = env
    monkeypatch.setattr(jev, "DAILY_BUDGET_USD", 1e-9)
    out = jev.find_impl(str(big), "refund?")
    assert out.startswith("FALLBACK") and "budget" in out
    monkeypatch.setattr(jev, "DAILY_BUDGET_USD", 1.0)
    (jev.CONFIG_DIR / "disabled").touch()
    assert "kill switch" in jev.find_impl(str(big), "refund?")
    (jev.CONFIG_DIR / "disabled").unlink()
    (d / "client").mkdir()
    (d / "client" / ".jevoff").touch()
    (d / "client" / "a.txt").write_text("refund\n" * 500)
    assert ".jevoff" in jev.find_impl(str(d / "client" / "a.txt"), "refund?")
    jev._client = None
    assert "no TypeSafe API key" in jev.ask_impl(NOUL, "s")
    assert fake.requests == 0


# --------------------------------------------------------------- prune -------

def test_prune_passthrough_short_output(env):
    jev, fake, d, big = env
    assert jev.prune_impl("ok\nall good", "failures?") == "ok\nall good"
    assert fake.requests == 0


def test_prune_keeps_errors_and_saves_full_log(env):
    jev, fake, d, big = env
    log = [f"PASS test_{i}" for i in range(2000)]
    log[700] = "FAIL test_refund: AssertionError expected 10 got 0"
    log[1500] = "  refund amount was miscalculated in compute_refund"
    out = jev.prune_impl("\n".join(log), "why did the refund test fail?")
    assert "AssertionError" in out and "miscalculated" in out
    saved = Path(out.split("Full output: ")[1].split(" - read")[0])
    assert saved.read_text().count("\n") == 1999
    assert "lines omitted" in out and len(out) < 10_000


def test_prune_fails_open_to_deterministic(env):
    jev, fake, d, big = env
    fake.mode = "down"
    log = [f"ok {i}" for i in range(500)]
    log[250] = "ERROR: database connection refused"
    out = jev.prune_impl("\n".join(log), "why?")
    assert "database connection refused" in out and "Jev not used" in out


# ----------------------------------------------------------------- map -------

def test_map_sorting_errors_csv_bom_and_inline_savings(env):
    jev, fake, d, big = env
    items = d / "events.csv"
    items.write_text("﻿title,url\nYoga,u1\nrefund workshop,u2\nbroken,u3\n", encoding="utf-8")
    fake.fail_if = "broken"
    q = json.dumps({"fit": {"type": "noul", "instructions": "about refunds?"},
                    "kind": {"type": "choice", "instructions": "kind?", "criteria": {"talk": None, "other": None}}})
    out = jev.map_impl(q, items_path=str(items), id_field="title", sort_by="fit", out_csv=str(d / "o.csv"))
    rows = [ln for ln in out.splitlines() if " | " in ln][1:]
    assert rows[0].startswith("refund workshop") and rows[-1].startswith("broken | ERROR")
    assert (d / "o.csv").read_text().count("\n") == 4
    out2 = jev.map_impl(q, items_json=[{"t": "refund"}], sort_by="kind")
    assert "needs a label" in out2 and "tokens saved" not in out2      # inline items save nothing


def test_map_picks_the_right_list_in_json_object(env):
    jev, fake, d, big = env
    f = d / "feed.json"
    f.write_text(json.dumps({"tags": ["a", "b", "c", "d"], "events": [{"t": "refund"}, {"t": "x"}]}))
    out = jev.map_impl(NOUL, items_path=str(f), id_field="t")
    assert "2 items" in out


# ------------------------------------------------------------- ask / CLI -----

def test_ask_stdin_only_with_dash_and_empty_refused(env, tmp_path):
    jev, fake, d, big = env
    assert jev.ask_impl(NOUL, "").startswith("ERROR")
    env_vars = {**os.environ, "JEV_CONFIG_DIR": str(tmp_path / "cfg2")}
    r = subprocess.run([sys.executable, str(JEV_PY), "ask", "-q", NOUL, "--state", "-"], input="refund",
                       capture_output=True, text=True, env=env_vars, timeout=60)
    assert "no TypeSafe API key" in r.stdout and r.returncode == 4   # read stdin, then alerted (no key)


# ------------------------------------------------------- self-evolving -------

def test_tune_needs_enough_labels_and_moves_in_small_steps(env):
    jev, fake, d, big = env
    for i in range(5):
        jev.find_impl(str(big), f"refund {i}")
        jev.feedback_impl(str(i + 1), "good", answer_line=601)
    assert "not enough labels" in jev.tune_impl()
    assert not jev.TUNING_PATH.exists()
    for i in range(5, 25):
        jev.find_impl(str(big), f"refund {i}")
        jev.feedback_impl(str(i + 1), "good", answer_line=601)
    fake.keyword = "zzz"
    for i in range(25, 37):
        jev.find_impl(str(big), f"unrelated {i}")
        jev.feedback_impl(str(i + 1), "absent")
    out = jev.tune_impl()
    vals = json.loads(jev.TUNING_PATH.read_text())["values"]
    assert abs(vals["find.absent"] - 0.35) <= 0.05 + 1e-9 and vals["find.absent"] <= 0.35
    assert vals["find.top_k"] >= 8                       # never shrinks below default
    assert "Tuned" in out or "no changes" in out


def test_autotune_only_proposes_by_default(env, monkeypatch):
    jev, fake, d, big = env
    monkeypatch.setattr(jev, "AUTOTUNE_EVERY", 2)
    for i in range(2):
        jev.find_impl(str(big), f"refund {i}")
    msgs = [jev.feedback_impl(str(i + 1), "bad", answer_line=601) for i in range(2)]
    assert "Tune" in msgs[-1] and not jev.TUNING_PATH.exists()


def test_feedback_last_and_validation(env):
    jev, fake, d, big = env
    jev.find_impl(str(big), "refund?")
    assert "must be one of" in jev.feedback_impl("last", "maybe")
    assert "Recorded absent for call 1" in jev.feedback_impl("last", "absent")
    assert "No call" in jev.feedback_impl("last", "bad")      # nothing un-labelled left


def test_eval_fits_and_validates(env):
    jev, fake, d, big = env
    rows = [{"find": str(big), "query": f"refund q{i}", "line": 601} for i in range(14)]
    other = d / "other.py"
    other.write_text("\n".join(f"x{i} = {i}" for i in range(400)))
    rows += [{"find": str(other), "query": f"refund q{i}", "absent": True} for i in range(8)]
    g = d / "golden.jsonl"
    g.write_text("\n".join(json.dumps(r) for r in rows))
    out = jev.eval_impl(str(g))
    assert "22 cases" in out and "hit@1 14/14" in out and "half 2" in out
    assert "No golden eval yet" not in jev.review_impl()


def test_review_recommendations(env):
    jev, fake, d, big = env
    fake.mode = "ratelimit"
    jev.ask_impl(NOUL, "s")
    rev = jev.review_impl()
    assert "lower JEV_WORKERS" in rev and "No golden eval yet" in rev


# ---------------------------------------------------------------- MCP --------

def test_cowork_vm_paths_are_mapped(env, monkeypatch):
    jev, fake, d, big = env
    root = d / "Projects" / "Jev"
    root.mkdir(parents=True)
    (root / "notes.txt").write_text("hi")
    monkeypatch.setenv("JEV_ROOTS", str(root))
    p = jev.resolve_path("/sessions/abc-123/mnt/Jev/notes.txt")
    assert p == (root / "notes.txt").resolve()
    with pytest.raises(FileNotFoundError, match="absolute path"):
        jev.resolve_path("relative/missing.txt")


def test_mcp_tools_accept_loose_types_and_run_off_the_event_loop(env):
    jev, fake, d, big = env
    srv = jev.build_server()

    async def go():
        tools = {t.name for t in await srv.list_tools()}
        assert tools == {"jev_find", "jev_prune", "jev_rank", "jev_map", "jev_ask", "jev_feedback", "jev_status",
                         "jev_control"}
        r = await srv.call_tool("jev_control", {"action": "check"})
        text = r[0][0].text if isinstance(r, tuple) else r[0].text
        assert text.startswith("Jev OK")
        r = await srv.call_tool("jev_ask", {"questions": {"a": {"type": "noul", "instructions": "refund?"}},
                                            "state": "refund please"})
        text = r[0][0].text if isinstance(r, tuple) else r[0].text
        assert '"noul": 0.9' in text
        r = await srv.call_tool("jev_feedback", {"call_id": 1, "outcome": "good"})
        text = r[0][0].text if isinstance(r, tuple) else r[0].text
        assert "Recorded good" in text
        r = await srv.call_tool("jev_find", {"path": "missing.txt", "query": "x"})
        text = r[0][0].text if isinstance(r, tuple) else r[0].text
        assert text.startswith("ERROR")

    asyncio.run(go())


def test_cli_starts_without_importing_mcp(env):
    jev, fake, d, big = env
    code = (f"import runpy,sys; sys.argv=['jev','--version']\ntry:\n runpy.run_path({str(JEV_PY)!r}, "
            "run_name='__main__')\nexcept SystemExit: pass\nprint('mcp' in sys.modules)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert r.stdout.strip().endswith("False")


# ------------------------------------------------------- account alerts ------

def test_expired_key_alerts_user_with_options(env):
    jev, fake, d, big = env
    fake.mode = "401"
    out = jev.find_impl(str(big), "refund?")
    assert out.startswith("JEV ALERT") and "very TOP of your reply" in out
    assert "revoked or expired" in out and "jev key" in out and "jev off" in out and "FALLBACK" in out
    hook = json.loads(jev.ALERT_HOOK_PATH.read_text())
    assert hook["systemMessage"].startswith("⚠️ Jev has STOPPED") and "jev key" in hook["systemMessage"]
    assert hook["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "start your reply with this" in hook["hookSpecificOutput"]["additionalContext"]
    assert jev.main(["ask", "-q", NOUL, "--state", "x"]) == 4


def test_out_of_credit_is_classified_and_points_to_billing(env):
    jev, fake, d, big = env
    fake.mode = "402"
    out = jev.ask_impl(NOUL, "refund please")
    assert out.startswith("JEV ALERT") and "out of credit" in out and "settings/billing" in out


def test_alert_persists_through_breaker_and_clears_after_top_up(env):
    jev, fake, d, big = env
    fake.mode = "402"
    for i in range(4):
        out = jev.ask_impl('{"a":{"type":"noul","instructions":"q%d"}}' % i, "s")
    assert out.startswith("JEV ALERT") and "paused" in out          # still told why, even while paused
    fake.mode = "up"
    assert jev.check_impl().startswith("Jev OK")
    assert not jev.ALERT_PATH.exists() and not jev.ALERT_HOOK_PATH.exists()
    assert "paused" not in jev.ask_impl(NOUL, "refund please")      # breaker reset by check


def test_successful_call_clears_alert_automatically(env):
    jev, fake, d, big = env
    fake.mode = "401"
    jev.ask_impl(NOUL, "a")
    fake.mode = "up"
    jev.ask_impl('{"b":{"type":"noul","instructions":"other"}}', "b")
    assert not jev.ALERT_HOOK_PATH.exists()


def test_dismiss_off_on(env):
    jev, fake, d, big = env
    fake.mode = "401"
    jev.ask_impl(NOUL, "a")
    assert "hidden for 12 hours" in jev.dismiss_impl()
    assert not jev.ALERT_HOOK_PATH.exists()
    out = jev.ask_impl('{"c":{"type":"noul","instructions":"again"}}', "a")
    assert out.startswith("FALLBACK")                                 # same problem stays quiet
    fake.mode = "402"
    jev.ask_impl('{"c":{"type":"noul","instructions":"new"}}', "a")
    assert jev.ALERT_HOOK_PATH.exists()                               # a different problem re-alerts
    assert "Jev is OFF" in jev.off_impl() and not jev.ALERT_HOOK_PATH.exists()
    assert "kill switch" in jev.ask_impl(NOUL, "a")
    fake.mode = "up"
    assert "Jev OK" in jev.on_impl()


def test_notification_once_per_new_problem(env, monkeypatch):
    jev, fake, d, big = env
    sent = []
    monkeypatch.setattr(jev, "_notify", lambda title, text: sent.append(title))
    fake.mode = "401"
    for i in range(3):
        jev.ask_impl('{"a":{"type":"noul","instructions":"n%d"}}' % i, "s")
    assert sent == ["Jev stopped working"]


def test_hook_script_shows_alert_then_low_credit_only_once(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cfg = home / ".config" / "typesafe"
    fake = FakeJev()
    monkeypatch.setenv("HOME", str(home))
    jev = load_jev(tmp_path, monkeypatch, fake)
    monkeypatch.setattr(jev, "CONFIG_DIR", cfg)
    monkeypatch.setattr(jev, "DB_PATH", cfg / "jev.db")
    monkeypatch.setattr(jev, "ALERT_PATH", cfg / "alert.json")
    monkeypatch.setattr(jev, "ALERT_HOOK_PATH", cfg / "alert-hook.json")
    hook = Path(__file__).resolve().parent.parent / "hooks" / "alert-hook.sh"
    run_hook = lambda: subprocess.run(["sh", str(hook)], capture_output=True, text=True,  # noqa: E731
                                      env={**os.environ, "HOME": str(home)}, timeout=10)
    assert run_hook().stdout == "" and run_hook().returncode == 0       # nothing to say
    fake.mode = "401"
    jev.ask_impl(NOUL, "s")
    for _ in range(2):                                                   # repeats until fixed
        r = run_hook()
        assert json.loads(r.stdout)["systemMessage"].startswith("⚠️") and r.returncode == 0
    fake.mode = "up"
    monkeypatch.setattr(jev, "MONTHLY_CREDIT_USD", 1e-9)
    jev.ask_impl('{"z":{"type":"noul","instructions":"spend"}}', "refund")
    assert "running low" in json.loads(run_hook().stdout)["systemMessage"]
    assert run_hook().stdout == ""                                       # low-credit note shows once


def test_key_command_saves_private_file(env, monkeypatch):
    jev, fake, d, big = env
    import getpass
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "  ts_test_key  ")
    monkeypatch.setattr(jev, "check_impl", lambda: "Jev OK")
    out = jev.key_impl()
    f = jev.CONFIG_DIR / "api_key"
    assert "Jev OK" in out and f.read_text() == "ts_test_key" and oct(f.stat().st_mode & 0o777) == "0o600"


# ------------------------------------------------- packaging / setup UX ------

ROOT = JEV_PY.parent


def test_embedded_assets_match_repo_files(env):
    jev, fake, d, big = env
    assert jev.SKILL_MD == (ROOT / "skills" / "jev" / "SKILL.md").read_text(), "run: python scripts/sync_assets.py"
    assert jev.HOOK_SH == (ROOT / "hooks" / "alert-hook.sh").read_text(), "run: python scripts/sync_assets.py"


def test_version_matches_pyproject(env):
    jev, fake, d, big = env
    import re as _re
    v = _re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), _re.M).group(1)
    assert v == jev.VERSION


def _fake_home(tmp_path, monkeypatch, jev, desktop=True):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")                    # no real `claude`/`jev` from this machine
    client = jev._client
    monkeypatch.setattr(jev, "_get_client", lambda: client)        # keep the mock after a key is saved
    p = jev._paths()
    if desktop:
        p["desktop_dir"].mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps(
        {"theme": "dark", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}))
    (home / ".claude" / "CLAUDE.md").write_text("# Mine\n- keep this\n")
    return home, p


def test_setup_is_idempotent_and_uninstall_restores_everything(env, tmp_path, monkeypatch, capsys):
    jev, fake, d, big = env
    home, p = _fake_home(tmp_path, monkeypatch, jev)
    before = {k: p[k].read_text() for k in ("settings", "claude_md")}
    monkeypatch.setenv("JEV_API_KEY", "ts_test_key_123456")
    assert jev.setup_impl(assume_yes=True, claude_md=True) == 0
    out = capsys.readouterr().out
    assert "[5/5]" in out and "Jev OK" in out and "…3456" in out and "ts_test_key_123456" not in out
    assert (p["skill_dir"] / "SKILL.md").read_text() == jev.SKILL_MD
    cfg = json.loads(p["settings"].read_text())
    assert cfg["theme"] == "dark" and "Stop" in cfg["hooks"] and cfg["hooks"]["UserPromptSubmit"]
    assert jev.MD_START in p["claude_md"].read_text() and "keep this" in p["claude_md"].read_text()
    assert json.loads(p["desktop_cfg"].read_text())["mcpServers"]["jev"]["args"][-1] == "serve"
    assert oct((jev.CONFIG_DIR / "api_key").stat().st_mode & 0o777) == "0o600"
    snapshot = {k: p[k].read_text() for k in ("settings", "claude_md", "desktop_cfg")}
    jev.setup_impl(assume_yes=True, claude_md=True)                 # re-run: nothing changes
    assert {k: p[k].read_text() for k in snapshot} == snapshot
    assert "Changed:" not in capsys.readouterr().out
    assert jev.uninstall_impl(assume_yes=True, keep_cli=True) == 0
    assert {k: p[k].read_text() for k in ("settings", "claude_md")} == {
        "settings": json.dumps(json.loads(before["settings"]), indent=2) + "\n", "claude_md": before["claude_md"]}
    assert "jev" not in json.loads(p["desktop_cfg"].read_text()).get("mcpServers", {})
    assert not p["skill_dir"].exists() and (jev.CONFIG_DIR / "api_key").exists()   # key kept without --purge


def test_setup_without_claude_or_key_still_succeeds(env, tmp_path, monkeypatch, capsys):
    jev, fake, d, big = env
    monkeypatch.setenv("HOME", str(tmp_path / "empty"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    jev._client = None
    assert jev.setup_impl(assume_yes=True) == 0
    out = capsys.readouterr().out
    assert "Claude Code not found" in out and "jev key" in out


def test_legacy_install_is_migrated(env, tmp_path, monkeypatch):
    jev, fake, d, big = env
    home, p = _fake_home(tmp_path, monkeypatch, jev)
    p["claude_md"].write_text("# Mine\n\n## jev token saver\n- old rule\n\n## Other\n- keep\n")
    cfg = json.loads(p["settings"].read_text())
    cfg["hooks"]["UserPromptSubmit"] = [{"hooks": [{"type": "command",
                                                    "command": str(home / ".local/share/jev/alert-hook.sh")}]}]
    p["settings"].write_text(json.dumps(cfg))
    jev.setup_impl(assume_yes=True, claude_md=True, skip_check=True)
    md = p["claude_md"].read_text()
    assert "old rule" not in md and "## Other" in md and md.count("jev token saver") == 1
    groups = json.loads(p["settings"].read_text())["hooks"]["UserPromptSubmit"]
    cmds = [h["command"] for g in groups for h in g["hooks"]]
    assert cmds == [str(p["hook"])]


def test_doctor_reports_problems_with_fixes(env, tmp_path, monkeypatch, capsys):
    jev, fake, d, big = env
    home, p = _fake_home(tmp_path, monkeypatch, jev)
    assert jev.doctor_impl(offline=True) == 1
    out = capsys.readouterr().out
    assert "no API key" in out and "fix: jev key" in out and "skill missing" in out


def test_cli_without_args_prints_help(env, monkeypatch, capsys):
    jev, fake, d, big = env
    monkeypatch.setattr(sys, "argv", ["jev"])
    assert jev.main() == 0 and "usage: jev" in capsys.readouterr().out


def test_installer_script_is_valid():
    r = subprocess.run(["bash", "-n", str(ROOT / "install.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    import shutil
    if shutil.which("shellcheck"):
        r = subprocess.run(["shellcheck", "-S", "warning", str(ROOT / "install.sh")], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout
