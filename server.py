#!/usr/bin/env python3
"""
boss-dashboard 本地后端（仅标准库，无额外依赖）。

职责：
  1. 提供 /api/status    —— CDP 是否就绪、结果目录、scraper 是否安装
  2. 提供 /api/files     —— 列出 scraper 历史结果文件
  3. 提供 /api/jobs      —— 读取指定结果文件的岗位数据
  4. 提供 /api/scrape    —— 触发一次真实抓取（后台线程），日志可轮询

只监听 127.0.0.1。文件名参数严格白名单校验，不接受路径穿越。
"""

import glob
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from urllib import error as urllib_error

HOST = "127.0.0.1"
PORT = int(os.environ.get("BDS_PORT", "8765"))


def _load_secrets():
    """读取同目录 secrets.env（K=V，# 开头为注释），只补未设置的变量。

    这样 `python server.py` 直接启动也能读到本地配置；仓库里不含该文件。
    已有环境变量优先，不会被文件覆盖。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secrets.env")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v and not os.environ.get(k):
                    os.environ[k] = v
    except OSError:
        pass


_load_secrets()

# ---- scraper 定位（自动探测）----------------------------------------
# 看板只是前端，真正抓数据的是独立的 boss-zhipin-scraper 仓库。
# 别人克隆本仓库后并不知道它在哪、装没装依赖，所以这里按优先级逐个找：
#   1. BDS_REPO 环境变量 / secrets.env显式指定
#   2. 与本文件同级的 boss-zhipin-scraper/（推荐：clone 时把两者放一起）
#   3. 上级目录下的 boss-zhipin-scraper/
#   4. ~/Desktop 等常见位置（兼容原有单机布局）
# 全部找不到时保持 None，由前端提示安装步骤，而不是静默报 500。
SCRAPER_RELPATH = os.path.join("scripts", "boss_cdp_raw.py")
REPO_HINT = "https://github.com/Adrian0314/boss-zhipin-scraper"
HERE = os.path.dirname(os.path.abspath(__file__))


def scraper_python(repo):
    """选一个能解释 scraper 的 Python：优先项目 venv（装了依赖），退回当前解释器。"""
    if not repo:
        return None
    for rel in ((".venv", "Scripts", "python.exe"),
                (".venv", "bin", "python"),
                ("venv", "Scripts", "python.exe"),
                ("venv", "bin", "python")):
        p = os.path.join(repo, *rel)
        if os.path.isfile(p):
            return p
    return sys.executable


def _is_repo(path):
    return bool(path) and os.path.isfile(os.path.join(path, SCRAPER_RELPATH))


def find_scraper_repo():
    """定位 boss-zhipin-scraper 目录；找不到返回 None。"""
    env = os.environ.get("BDS_REPO", "").strip()
    if env:
        return os.path.abspath(env) if _is_repo(env) else None
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(HERE, "boss-zhipin-scraper"),
        os.path.join(os.path.dirname(HERE), "boss-zhipin-scraper"),
        os.path.join(HERE, "..", "boss-zhipin-scraper"),
    ]
    if home:
        for base in ("Desktop", "Downloads", "Documents", ""):
            candidates.append(os.path.join(home, base, "boss-zhipin-scraper"))
    for c in candidates:
        c = os.path.abspath(c)
        if _is_repo(c):
            return c
    return None


REPO = find_scraper_repo()
SCRAPER = os.path.join(REPO, SCRAPER_RELPATH) if REPO else None


def install_hint():
    """scraper 缺失时给前端一段可直接复制的安装步骤。

    返回空字符串表示环境已就绪。注意必须指向本人的 fork：
    全职/兼职筛选依赖它新增的 --job-type/--count 参数，原版上游没有。
    """
    if REPO:
        return ""
    target = os.path.join(HERE, "boss-zhipin-scraper")
    return (
        f'git clone {REPO_HINT}.git "{target}"\n'
        f'cd "{target}"\n'
        'python -m venv .venv\n'
        '.venv\\Scripts\\activate\n'
        'pip install -r requirements.txt'
    )


RESULT_DIR = os.environ.get("BDS_RESULT_DIR",
                            os.path.expanduser("~/.boss-zhipin-scraper/job-result"))
CDP_PORT = int(os.environ.get("BDS_CDP_PORT", "9222"))

# ---- AI 配置 ---------------------------------------------------------
# 优先级：设置面板写入的 settings.json > 环境变量 > secrets.env（已并入环境）。
# 这样非技术用户可以在页面里直接改模型和密钥，不必碰配置文件。
SETTINGS_PATH = os.path.join(HERE, "settings.json")
SETTINGS_LOCK = threading.Lock()

# 保存在 settings.json 里的键（其余键忽略，防止被塞脏数据）
SETTINGS_KEYS = ("ai_base_url", "ai_api_key", "ai_model", "verbose_log")
_SECRET_KEYS = ("ai_api_key",)


def load_settings():
    """读设置文件；损坏或不存在时返回空 dict，绝不让看板起不来。"""
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        return {k: raw[k] for k in SETTINGS_KEYS if k in raw}
    except (OSError, ValueError):
        return {}


def save_settings(patch):
    """合并写入设置文件；返回写入后的完整设置。"""
    with SETTINGS_LOCK:
        cur = load_settings()
        cur.update({k: patch[k] for k in SETTINGS_KEYS if k in patch})
        tmp = SETTINGS_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=2)
            os.replace(tmp, SETTINGS_PATH)      # 原子替换，避免写一半损坏
        except OSError as e:
            raise RuntimeError(f"保存设置失败: {e}") from e
        return cur


_settings = load_settings()


def cfg(key, env_name, default=""):
    """运行期取配置：settings.json 优先，其次环境变量。"""
    v = _settings.get(key)
    if isinstance(v, str) and v.strip():
        return v.strip()
    return os.environ.get(env_name, default).strip()


def reload_settings():
    """保存后刷新内存副本 —— 不刷新则 cfg() 仍读旧值（页面保存后看不到变化）。"""
    global _settings, VERBOSE_LOG
    _settings = load_settings()
    VERBOSE_LOG = str(_settings.get("verbose_log", False)).lower() in (
        "1", "true", "yes")
    return _settings


AI_BASE_URL = cfg("ai_base_url", "BDS_AI_BASE_URL")
AI_API_KEY = cfg("ai_api_key", "BDS_AI_API_KEY")
AI_MODEL = cfg("ai_model", "BDS_AI_MODEL")
AI_TIMEOUT = int(os.environ.get("BDS_AI_TIMEOUT", "90"))
# 送进模型的岗位条数上限：0 表示不限制（全部发送）。
# 默认 0——看板的数据量通常几百条，全量发送让分析更完整；
# 若你的中转站上下文窗口较小、或想控制费用，可设成具体数字。
AI_MAX_JOBS = int(os.environ.get("BDS_AI_MAX_JOBS", "0"))
# 推理型模型会先花大量 token 思考，预算给足，否则 content 会是 null
AI_MAX_TOKENS = int(os.environ.get("BDS_AI_MAX_TOKENS", "8000"))

# 详细日志开关：默认关闭 —— 首次使用的人面对一屏 Python traceback 只会懵，
# 默认只给进度条 + 一句话结果，需要排查时在设置里打开。
VERBOSE_LOG = str(_settings.get("verbose_log", False)).lower() in ("1", "true", "yes")

INDEX_HTML = os.path.join(HERE, "index.html")

# 只接受 scraper 自己产出的文件名
SAFE_NAME = re.compile(r"^boss_(jobs|details)_\d{8}_\d{4}\.json$")

# 抓取任务表：job_id -> {status, log, file, started}
JOBS = {}
JOBS_LOCK = threading.Lock()


def log(msg):
    """控制台日志。Windows 终端可能是 GBK，避免中文导致 UnicodeEncodeError。"""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("utf-8", "replace").decode("ascii", "replace"), flush=True)


def cdp_ready():
    """探测 CDP 调试端口是否可连接。"""
    try:
        with socket.create_connection(("127.0.0.1", CDP_PORT), timeout=1.5):
            return True
    except OSError:
        return False


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def list_files():
    """列出结果目录下的 JSON 文件，按抓取时间倒序。"""
    out = []
    for path in glob.glob(os.path.join(RESULT_DIR, "boss_*.json")):
        name = os.path.basename(path)
        if not SAFE_NAME.match(name):
            continue
        item = {"name": name, "kind": "details" if "_details_" in name else "jobs"}
        try:
            data = _read_json(path)
            if isinstance(data, dict):
                item.update({
                    "keyword": data.get("keyword", ""),
                    "city": data.get("city", ""),
                    "total": data.get("total", len(data.get("jobs", []))),
                    "scraped_at": data.get("scraped_at", ""),
                })
        except (json.JSONDecodeError, OSError):
            pass
        out.append(item)
    out.sort(key=lambda x: x["name"], reverse=True)
    return out


def resolve_file(name):
    """把用户传入的文件名解析为安全绝对路径，非法则返回 None。"""
    if not name or not SAFE_NAME.match(name):
        return None
    path = os.path.join(RESULT_DIR, name)
    # 双保险：确保解析后仍在结果目录内
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(RESULT_DIR):
        return None
    return path


def _split_pipes(text):
    return [x.strip() for x in str(text or "").split("|") if x.strip()]


def _salary_min(salary):
    """从 '30-60K·15薪' 里取月薪下限(K)。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*-?\s*\d*(?:\.\d+)?\s*[Kk]", str(salary or ""))
    return float(m.group(1)) if m else None


def build_jobs_context(jobs, limit):
    """把岗位列表压成紧凑的上下文文本。limit<=0 表示不限制。"""
    rows = list(jobs) if not limit or limit <= 0 else list(jobs[:limit])
    lines = []
    for i, j in enumerate(rows, 1):
        bits = [str(j.get(k)) for k in ("boss_name", "company_industry", "company_scale") if j.get(k)]
        parts = [f"{i}. {j.get('title','')} | {j.get('salary','')} | {j.get('location','')}"]
        if bits:
            parts.append(" | ".join(bits))
        if j.get("tags"):
            parts.append(f"[{j['tags']}]")
        sk = _split_pipes(j.get("skills"))
        if sk:
            parts.append("技能:" + ",".join(sk[:6]))
        wf = _split_pipes(j.get("welfare"))
        if wf:
            parts.append("福利:" + ",".join(wf[:4]))
        if j.get("is_new"):
            parts.append("[新]")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _take_jobs(jobs):
    """按 AI_MAX_JOBS 截取；0 或负数视为不限制，返回全部。"""
    return list(jobs) if AI_MAX_JOBS <= 0 else list(jobs[:AI_MAX_JOBS])


def build_stats_block(jobs):
    """本地算好统计结果喂给模型：省掉模型的计算负担，显著降低其思考长度。"""
    rows = _take_jobs(jobs)
    n = len(rows)
    if not n:
        return "（无数据）"

    lows = [v for v in (_salary_min(j.get("salary")) for j in rows) if v is not None]
    out = [f"岗位数：{n}"]

    if lows:
        s = sorted(lows)
        median = s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2
        out.append(f"月薪下限：均值 {sum(s)/len(s):.1f}K，中位数 {median}K，"
                   f"最低 {min(s)}K，最高 {max(s)}K")

        buckets = [("<6K", lambda v: v < 6), ("6-8K", lambda v: 6 <= v < 8),
                   ("8-10K", lambda v: 8 <= v < 10), ("10-12K", lambda v: 10 <= v < 12),
                   ("12-15K", lambda v: 12 <= v < 15), ("≥15K", lambda v: v >= 15)]
        dist = [f"{name} {sum(1 for v in s if f(v))}个" for name, f in buckets]
        out.append("月薪下限分档：" + "，".join(dist))

    months = {}
    for j in rows:
        m = re.search(r"(\d{2})\s*薪", str(j.get("salary") or ""))
        if m:
            months[m.group(1)] = months.get(m.group(1), 0) + 1
    if months:
        out.append("年薪月数分布：" + "，".join(f"{k}薪 {v}个" for k, v in sorted(months.items())))

    for label, field in (("经验", "tags"), ("学历", "tags")):
        c = {}
        for j in rows:
            for t in _split_pipes(j.get(field)):
                if label == "经验" and ("年" in t or "应届" in t or "在校" in t or "不限" in t):
                    c[t] = c.get(t, 0) + 1
                elif label == "学历" and ("本科" in t or "硕士" in t or "博士" in t
                                          or "大专" in t or "学历" in t):
                    c[t] = c.get(t, 0) + 1
        if c:
            top = sorted(c.items(), key=lambda x: -x[1])[:6]
            out.append(f"{label}要求：" + "，".join(f"{k} {v}个" for k, v in top))

    sc = {}
    for j in rows:
        for k in _split_pipes(j.get("skills")):
            sc[k] = sc.get(k, 0) + 1
    if sc:
        top = sorted(sc.items(), key=lambda x: -x[1])[:15]
        out.append("高频技能：" + "，".join(f"{k}({v})" for k, v in top))

    ind = {}
    for j in rows:
        if j.get("company_industry"):
            ind[j["company_industry"]] = ind.get(j["company_industry"], 0) + 1
    if ind:
        top = sorted(ind.items(), key=lambda x: -x[1])[:6]
        out.append("行业分布：" + "，".join(f"{k} {v}个" for k, v in top))

    return "\n".join(out)


def ai_base():
    return cfg("ai_base_url", "BDS_AI_BASE_URL")


def ai_model():
    return cfg("ai_model", "BDS_AI_MODEL")


def ai_key():
    """每次调用时读，改完设置立即生效，不必重启。"""
    return cfg("ai_api_key", "BDS_AI_API_KEY")


def ai_ready():
    """三项配置齐全才算可调用；缺哪项在报错里点名，避免 500 时无从下手。"""
    missing = []
    if not ai_base():
        missing.append("接口地址")
    if not ai_key():
        missing.append("API 密钥")
    if not ai_model():
        missing.append("模型名")
    return missing


def call_ai(question, jobs, model=None, history=None):
    """调用 OpenAI 兼容接口，把岗位数据作为上下文交给模型。"""
    import urllib.request

    key = ai_key()
    missing = ai_ready()
    if missing:
        raise RuntimeError(
            "AI 功能未配置完整，缺少：" + "、".join(missing) +
            "。请设置对应环境变量，或写入同目录 secrets.env 后重启看板。")

    ctx = build_jobs_context(jobs, AI_MAX_JOBS)
    stats = build_stats_block(jobs)
    system = (
        "你是一个招聘数据分析师。下面是从 BOSS直聘抓取到的真实岗位数据，"
        "并且已经替你算好了统计结果。\n"
        "规则：\n"
        "1. 只依据给定数据和统计结果回答，用简体中文，结论先行。\n"
        "2. **直接给结论，不要逐条枚举、不要写代码、不要描述你是怎么算的**"
        "（统计结果已给出，无需你再计算）。\n"
        "3. 薪资字段如 '30-60K·15薪' 表示月薪 30-60K，一年 15 薪。\n"
        "4. 数据不足以判断某一点时，明确说“当前数据无法判断”，绝不编造。\n"
        "5. 全文控制在 400 字以内。"
    )
    sent = len(_take_jobs(jobs))
    user = (f"【已算好的统计结果】\n{stats}\n\n"
            f"【岗位明细（共 {sent} 条）】\n{ctx}\n\n"
            f"【问题】{question}")

    msgs = [{"role": "system", "content": system}]
    for h in (history or [])[-6:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            msgs.append({"role": h["role"], "content": str(h["content"])[:2000]})
    msgs.append({"role": "user", "content": user})

    payload = json.dumps({
        "model": model or ai_model(),
        "messages": msgs,
        "temperature": 0.3,
        "max_tokens": AI_MAX_TOKENS,
    }).encode("utf-8")

    req = urllib.request.Request(
        ai_base().rstrip("/") + "/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=AI_TIMEOUT) as r:
            body = json.loads(r.read().decode("utf-8"))
    except urllib_error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"AI 接口返回 {e.code}: {detail}") from e
    except urllib_error.URLError as e:
        raise RuntimeError(f"无法连接 AI 接口: {e.reason}") from e

    try:
        choice = body["choices"][0]
        msg = choice["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"AI 响应格式异常: {str(body)[:200]}") from e

    text = msg.get("content")
    # 推理模型在 token 预算内没写完正文时，content 会是 null，
    # 但结论往往已经出现在 reasoning_content 末尾 —— 兜底救回，避免白等一次。
    if not text:
        reasoning = (msg.get("reasoning_content") or "").strip()
        if reasoning:
            return ("（本次回答未能在长度限制内输出完整结论，以下为模型推理摘要）\n\n"
                    + reasoning[-1200:])
        reason = choice.get("finish_reason")
        raise RuntimeError(f"模型未返回内容（finish_reason={reason}）。"
                           f"可尝试减少岗位数量，或调大 BDS_AI_MAX_TOKENS。")
    return text


# ---- 进度解析 ---------------------------------------------------------
# scraper 的 stdout 是给人看的流水账（每行一条岗位、夹着翻页等待）。
# 前端要的是「到哪一步了、还剩多少」，所以在这里把它翻译成结构化进度。
# 不解析成功也不算错：拿不到进度就继续只显示阶段文字，功能不受影响。
RE_PAGE = re.compile(r"^-{3}\s*\[(\d+)/(\d+)\s*页,\s*(\d+)(?:/(\d+))?\s*条已抓\]")
RE_JOB_LINE = re.compile(r"^\s*[✓√]\s")
RE_PAGE_DONE = re.compile(r"本页\s*(\d+)\s*条")
RE_TARGET_HIT = re.compile(r"已达目标\s*(\d+)\s*条")
RE_TARGET_DONE = re.compile(r"目标\s*(\d+)\s*条\s*✅")
RE_WAIT = re.compile(r"翻页等待\s*(\d+)\s*s")
RE_LOGIN = re.compile(r"等待\s*BOSS\s*登录完成")
RE_CDP_OK = re.compile(r"✅\s*已检测到\s*BOSS\s*登录态|✅\s*已登录")


def parse_progress(line, cur):
    """把一行输出并入进度状态（原地修改并返回 cur）。

    字段：phase 阶段文字 / cur/总共 / pct 百分比 / jobs 已抓条数 / target 目标
    """
    text = line.rstrip("\r\n")
    m = RE_PAGE.match(text)
    if m:
        page, pages, got = int(m.group(1)), int(m.group(2)), int(m.group(3))
        target = int(m.group(4)) if m.group(4) else 0
        cur.update(phase=f"正在抓第 {page}/{pages} 页", cur=page, total=pages,
                   jobs=got, target=target or cur.get("target", 0))
        return cur
    if RE_LOGIN.search(text):
        cur.update(phase="等待登录完成", indeterminate=True)
        return cur
    if RE_CDP_OK.search(text):
        cur.update(phase="已登录，开始抓取", indeterminate=True)
        return cur
    if RE_JOB_LINE.match(text):
        cur["jobs"] = cur.get("jobs", 0) + 1
        return cur
    m = RE_TARGET_HIT.search(text)
    if m:
        cur.update(phase=f"已达目标 {m.group(1)} 条", pct_done=True)
        return cur
    m = RE_TARGET_DONE.search(text)
    if m:
        cur.update(phase=f"目标 {m.group(1)} 条已达成", pct_done=True)
        return cur
    m = RE_WAIT.search(text)
    if m:
        cur.update(phase=f"翻页等待 {m.group(1)} 秒", waiting=True)
        return cur
    if "=== BOSS直聘抓取 ===" in text:
        cur.update(phase="准备抓取", indeterminate=True)
    return cur


def progress_view(cur):
    """把内部进度转成前端直接可渲染的结构。"""
    if not cur:
        return {"phase": "", "pct": 0, "jobs": 0, "target": 0,
                "indeterminate": True, "waiting": False}
    target = cur.get("target") or 0
    jobs = cur.get("jobs") or 0
    pct = 0
    if target:
        pct = min(100, round(jobs / target * 100))
    elif cur.get("total"):
        pct = min(100, round((cur.get("cur") or 0) / cur["total"] * 100))
    if cur.get("pct_done"):
        pct = 100
    return {
        "phase": cur.get("phase", ""),
        "pct": pct,
        "jobs": jobs,
        "target": target,
        "indeterminate": bool(cur.get("indeterminate")) and not pct,
        "waiting": bool(cur.get("waiting")),
    }


def run_command(job_id, cmd, cwd=REPO):
    """后台线程通用执行器：跑一条命令，边跑边把 stdout 写进 JOBS[job_id]['log']。"""
    def set_state(**kw):
        with JOBS_LOCK:
            JOBS[job_id].update(kw)

    set_state(status="running", cmd=" ".join(cmd), started=time.time())
    prog = {}
    try:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        for line in proc.stdout:
            parse_progress(line, prog)
            with JOBS_LOCK:
                JOBS[job_id]["log"] += line
                JOBS[job_id]["progress"] = progress_view(prog)
        proc.wait()
        vm = progress_view(prog)
        if proc.returncode == 0:
            vm.update(pct=100, indeterminate=False, phase=vm["phase"] or "完成")
        set_state(status="ok" if proc.returncode == 0 else "failed",
                  returncode=proc.returncode, progress=vm)
    except (OSError, ValueError) as e:
        set_state(status="failed",
                  log=JOBS[job_id].get("log", "") + f"\n启动失败: {e}",
                  progress={"phase": "启动失败", "pct": 0, "jobs": 0,
                            "target": 0, "indeterminate": False, "waiting": False})


def run_scrape(job_id, keyword, city, count, retries=3, detail=False, job_type=""):
    """跑一次真实抓取，完成后定位本轮产出的结果文件（mtime 在启动之后）。

    以「目标条数」为停止条件：scraper 逐页累积，达到 count 即停；
    连续 retries 页没有新增也会停（说明已无更多结果）。

    job_type: ""=全部 / "full"=全职 / "part"=兼职，
             对应 scraper 的 --job-type（服务端过滤，不是事后筛）。

    detail=False（默认）→ 传 --no-detail，只抓列表页。
    列表页已包含看板展示的全部字段（薪资/城市/公司/标签/技能/新增）。
    """
    py = scraper_python(REPO) or sys.executable
    if not SCRAPER:
        with JOBS_LOCK:
            JOBS[job_id].update(status="failed",
                                 log="未找到 boss-zhipin-scraper，抓取无法执行。")
        return
    cmd = [py, SCRAPER,
           "--keyword", keyword, "--city", city,
           "--count", str(count),
           "--max-retries", str(retries)]
    if job_type in ("full", "part"):
        cmd += ["--job-type", job_type]
    if not detail:
        cmd.append("--no-detail")
    run_command(job_id, cmd)
    try:
        newest = max(
            (p for p in glob.glob(os.path.join(RESULT_DIR, "boss_jobs_*.json"))),
            key=os.path.getmtime, default=None,
        )
        if newest:
            with JOBS_LOCK:
                JOBS[job_id]["file"] = os.path.basename(newest)
    except (OSError, ValueError):
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "boss-dashboard"

    def _no_scraper(self):
        """scraper 未安装时的统一应答：给可执行步骤，而不是裸 500。"""
        return self._err(503,
                         "未找到 boss-zhipin-scraper（抓取功能依赖它）。"
                         f"请先安装：\n{install_hint()}\n"
                         "装好后重启本服务即可自动识别。")

    def _settings_public(self):
        """回给前端的设置：密钥只回「已配置/未配置」与打码值，绝不回明文。"""
        key = ai_key()
        masked = ""
        if key:
            masked = key[:4] + "•" * 8 + key[-4:] if len(key) > 10 else "•" * len(key)
        return {
            "ai_base_url": ai_base(),
            "ai_model": ai_model(),
            "ai_key_masked": masked,
            "ai_key_set": bool(key),
            "verbose_log": VERBOSE_LOG,
            "ai_ready": not ai_ready(),
            "missing": ai_ready(),
            "settings_path": SETTINGS_PATH,
        }

    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    # ---- helpers ----
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._send(code, {"error": msg})

    def _start_task(self, action, label):
        """注册一个后台命令任务并立即返回 job id（供前端轮询日志）。"""
        py = scraper_python(REPO) or sys.executable
        job_id = uuid.uuid4().hex[:12]
        with JOBS_LOCK:
            JOBS[job_id] = {"status": "pending", "log": "", "file": None,
                            "action": label, "cmd": f"{py} {SCRAPER} {action}"}
        threading.Thread(target=run_command,
                         args=(job_id, [py, SCRAPER, action]),
                         daemon=True).start()
        return self._send(200, {"job": job_id})

    # ---- routes ----
    def do_GET(self):
        parsed = urlparse(self.path)
        route, params = parsed.path, parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            if not os.path.exists(INDEX_HTML):
                return self._err(500, "index.html 不存在")
            with open(INDEX_HTML, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")

        if route == "/api/status":
            py = scraper_python(REPO)
            return self._send(200, {
                "cdp_ready": cdp_ready(),
                "cdp_port": CDP_PORT,
                "scraper_exists": bool(REPO),
                "python_exists": bool(py),
                "repo": REPO,
                "repo_hint": REPO_HINT,
                "install_hint": install_hint(),
                "result_dir": RESULT_DIR,
                "result_dir_exists": os.path.isdir(RESULT_DIR),
                "verbose_log": VERBOSE_LOG,
            })

        if route == "/api/settings":
            return self._send(200, self._settings_public())

        if route == "/api/files":
            try:
                return self._send(200, {"files": list_files()})
            except OSError as e:
                return self._err(500, f"读取结果目录失败: {e}")

        if route == "/api/jobs":
            name = (params.get("file") or [""])[0]
            path = resolve_file(name)
            if not path:
                return self._err(400, "非法的文件名")
            if not os.path.exists(path):
                return self._err(404, "文件不存在")
            try:
                return self._send(200, _read_json(path))
            except (json.JSONDecodeError, OSError) as e:
                return self._err(500, f"解析 JSON 失败: {e}")

        if route == "/api/scrape/log":
            jid = (params.get("job") or [""])[0]
            with JOBS_LOCK:
                job = JOBS.get(jid)
            if not job:
                return self._err(404, "任务不存在")
            return self._send(200, job)

        if route == "/api/chrome":
            action = (params.get("action") or [""])[0]
            if action == "state":
                return self._send(200, {"cdp_ready": cdp_ready()})
            if action == "version":
                # CDP 起来后拿到浏览器版本，顺带证明端口确实是调试端口
                try:
                    import urllib.request
                    with urllib.request.urlopen(
                            f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=3) as r:
                        return self._send(200, json.loads(r.read().decode("utf-8")))
                except (OSError, ValueError) as e:
                    return self._err(503, f"读取 CDP 版本失败: {e}")

        return self._err(404, "未知路由")

    def do_POST(self):
        parsed = urlparse(self.path)

        # ---- 保存设置（AI 配置 + 详细日志开关）----
        if parsed.path == "/api/settings":
            global VERBOSE_LOG
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, ValueError):
                return self._err(400, "请求体不是合法 JSON")
            if not isinstance(payload, dict):
                return self._err(400, "请求体不是对象")

            patch = {}
            for k in ("ai_base_url", "ai_model"):
                if k in payload:
                    patch[k] = str(payload[k]).strip()
            # 密钥特殊处理：空字符串 = 不改动（避免用户只改模型时把密钥清空）
            if payload.get("ai_api_key"):
                patch["ai_api_key"] = str(payload["ai_api_key"]).strip()
            if "ai_api_key" in payload and payload["ai_api_key"] == "":
                patch["ai_api_key"] = ""      # 显式传空 = 清除
            if "verbose_log" in payload:
                patch["verbose_log"] = bool(payload["verbose_log"])

            # 接口地址做个轻量校验，避免存进明显不对的值
            url = patch.get("ai_base_url")
            if url and not (url.startswith("http://") or url.startswith("https://")):
                return self._err(400, "接口地址需以 http:// 或 https:// 开头")

            try:
                save_settings(patch)
            except RuntimeError as e:
                return self._err(500, str(e))

            # 重新载入内存配置，让改动立即生效（不必重启服务）
            reload_settings()
            return self._send(200, self._settings_public())

        # ---- Chrome / 登录态管理 ----
        if parsed.path in ("/api/chrome/start", "/api/chrome/stop", "/api/chrome/check"):
            if not SCRAPER:
                return self._no_scraper()
            action = {"start": "--setup-chrome",
                      "stop": "--stop-chrome",
                      "check": "--check"}[parsed.path.rsplit("/", 1)[1]]
            return self._start_task(action, label=f"chrome-{parsed.path.rsplit('/', 1)[1]}")

        # ---- AI 提问 ----
        if parsed.path == "/api/ask":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, ValueError):
                return self._err(400, "请求体不是合法 JSON")

            question = str(payload.get("question", "")).strip()
            if not question:
                return self._err(400, "问题不能为空")
            jobs = payload.get("jobs")
            if not isinstance(jobs, list) or not jobs:
                return self._err(400, "没有可用的岗位数据，请先拉取或选择结果文件")
            missing = ai_ready()
            if missing:
                return self._err(500, "AI 功能未配置完整，缺少：" + "、".join(missing) +
                                      "。请设置环境变量或 secrets.env 后重启看板")

            try:
                answer = call_ai(question, jobs,
                                 model=payload.get("model") or None,
                                 history=payload.get("history"))
            except RuntimeError as e:
                return self._err(502, str(e))
            return self._send(200, {"answer": answer, "model": payload.get("model") or ai_model()})

        if parsed.path != "/api/scrape":
            return self._err(404, "未知路由")

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, ValueError):
            return self._err(400, "请求体不是合法 JSON")

        keyword = str(payload.get("keyword", "")).strip()
        city = str(payload.get("city", "")).strip()
        try:
            count = max(1, min(500, int(payload.get("count", 60))))
        except (TypeError, ValueError):
            count = 60
        try:
            retries = max(1, min(10, int(payload.get("retries", 3))))
        except (TypeError, ValueError):
            retries = 3
        detail = bool(payload.get("detail", False))
        # 用工类型：只认 full/part，其余一律当「全部」，避免脏值透传给 scraper
        job_type = str(payload.get("jobType", "")).strip()
        if job_type not in ("full", "part"):
            job_type = ""

        if not keyword or not city:
            return self._err(400, "关键词和城市不能为空")
        if not SCRAPER:
            return self._no_scraper()

        # 预检：CDP 没起来就别启动 scraper，否则只会抛一屏 traceback
        if not cdp_ready():
            return self._err(409, "Chrome CDP 未启动。请先点「启动 Chrome」按钮，"
                                  "并在弹出的浏览器窗口里登录 BOSS直聘，然后再拉取。")

        job_id = uuid.uuid4().hex[:12]
        with JOBS_LOCK:
            JOBS[job_id] = {"status": "pending", "log": "", "file": None,
                            "keyword": keyword, "city": city,
                            "count": count, "detail": detail,
                            "job_type": job_type}
        threading.Thread(target=run_scrape,
                         args=(job_id, keyword, city, count, retries, detail, job_type),
                         daemon=True).start()
        return self._send(200, {"job": job_id})


if __name__ == "__main__":
    if not os.path.exists(RESULT_DIR):
        try:
            os.makedirs(RESULT_DIR, exist_ok=True)
        except OSError as e:
            log(f"[warn] 无法创建结果目录: {e}")
    log(f"boss-dashboard  ->  http://{HOST}:{PORT}")
    log(f"repo    : {REPO}")
    log(f"results : {RESULT_DIR}")
    log(f"CDP     : {'ready' if cdp_ready() else 'not ready (先跑 --setup-chrome)'}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
