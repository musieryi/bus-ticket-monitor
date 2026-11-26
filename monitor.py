# -*- coding: utf-8 -*-
"""
北京定制公交余票监控（核心模块）
================================
监控 080101 / 080801 两趟车「后 N 个工作日」每天的余票（N 默认 3），
只要这 N 天里任意一天余票 > 0，就通过 PushPlus / Server酱 / 企业微信机器人 推送到微信。

接口：dzgj.bjbus.com:9663 官方小程序 API（queryRemainingTicket）
签名：RSA-SHA256（私钥 private_key.pem，算法逆向自小程序包）

用法：
    python monitor.py              # 常驻运行（纯命令行模式）
    python monitor.py --once       # 只查询一轮（测试用）
    python monitor.py --test-push  # 只发一条测试推送（验证微信通路）

依赖：requests pycryptodome
"""

import base64
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta

import requests
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15

# 打包为 exe 时，资源在 exe 所在目录
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# 可变数据目录（state.db / logs）：
#   云端 PaaS 的容器文件系统是临时的，重建即丢；把数据目录指到挂载卷上可持久化。
#   未设置该环境变量时行为与原来完全一致（仍放在程序目录旁）。
DATA_DIR = os.environ.get("BUS_MON_DATA_DIR") or BASE_DIR
os.makedirs(DATA_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# 时区：强制用北京时间
# ---------------------------------------------------------------------------
# 云端容器默认 UTC，比北京晚 8 小时。若不处理会有两处静默出错：
#   1. date.today() 在凌晨 0-8 点会返回「昨天」，导致「后 3 个工作日」算错（把今天算进去）
#   2. 请求签名里的 timestamp 会用 UTC 时间，接口可能校验失败
# 这里统一返回「北京时间（naive）」，保持与原有 naive 时间比较逻辑兼容。
_TZ = None


def _local_now():
    """当前北京时间（naive datetime，不带 tzinfo，便于与历史状态字符串比较）"""
    global _TZ
    if _TZ is None:
        try:
            from zoneinfo import ZoneInfo

            _TZ = ZoneInfo("Asia/Shanghai")
        except Exception:  # noqa: BLE001  容器缺 tzdata 时退化为固定 UTC+8（中国无夏令时）
            from datetime import timezone as _tz

            _TZ = _tz(timedelta(hours=8))
    return datetime.now(_TZ).replace(tzinfo=None)


def _local_today():
    """当前北京日期"""
    return _local_now().date()


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
log_dir = os.path.join(DATA_DIR, "logs")
os.makedirs(log_dir, exist_ok=True)
_handlers = [
    logging.FileHandler(
        os.path.join(log_dir, f"monitor_{_local_today():%Y%m%d}.log"),
        encoding="utf-8",
    )
]
# 打包成无控制台窗口的 exe 时 sys.stdout 为 None，此时只写文件日志
if sys.stdout is not None:
    _handlers.insert(0, logging.StreamHandler(sys.stdout))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("bus-monitor")

_auth_alert_date = None  # 「登录失效」告警最后发送日期（每天最多一次）

# ---------------------------------------------------------------------------
# 签名（逆向自小程序 addSignFn）
# ---------------------------------------------------------------------------

def _sort_obj(t):
    """递归键排序（升序）；数组内基本类型也排序。"""
    if isinstance(t, dict):
        return {k: _sort_obj(t[k]) for k in sorted(t.keys())}
    if isinstance(t, list):
        if t and isinstance(t[0], (str, int, float)):
            return sorted(t, key=str)
        return [_sort_obj(x) for x in t]
    return t


def _to_qs(t):
    """拼接为 k=v&k=v；data 键取一层键排序后的紧凑 JSON。"""
    parts = []
    for k in sorted(t.keys()):
        v = t[k]
        if k == "data":
            d = {ik: v[ik] for ik in sorted(v.keys())}
            sv = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
        else:
            sv = str(v)
        parts.append(f"{k}={sv}")
    return "&".join(parts)


def make_sign(req, key):
    msg = _to_qs(_sort_obj(req))
    h = SHA256.new(msg.encode("utf-8"))
    return base64.b64encode(pkcs1_15.new(key).sign(h)).decode()


# ---------------------------------------------------------------------------
# 状态存储（SQLite）
# ---------------------------------------------------------------------------
DB_PATH = os.path.join(DATA_DIR, "state.db")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS line_state (
            line_id   TEXT PRIMARY KEY,
            last_tickets  INTEGER,
            last_seen     TEXT,
            alert_date    TEXT,
            alert_count   INTEGER DEFAULT 0,
            last_alert_at TEXT
        )"""
    )
    # 老库升级：新增 detail_sig（后 N 日票情指纹，用于检测"票情变化"）
    cols = {r[1] for r in conn.execute("PRAGMA table_info(line_state)")}
    if "detail_sig" not in cols:
        conn.execute("ALTER TABLE line_state ADD COLUMN detail_sig TEXT")
        conn.commit()
    # 通用键值表：存放需要跨进程持久化的标记
    # （云端定时任务每次都是新进程，不能靠进程内变量去重）
    conn.execute(
        """CREATE TABLE IF NOT EXISTS kv (
            k TEXT PRIMARY KEY,
            v TEXT
        )"""
    )
    conn.commit()
    return conn


def kv_get(conn, key, default=None):
    """读取持久化标记"""
    row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return row[0] if row else default


def kv_set(conn, key, value):
    """写入持久化标记"""
    with conn:
        conn.execute(
            "INSERT INTO kv(k, v) VALUES(?, ?)"
            " ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, value),
        )


def get_state(conn, line_id):
    row = conn.execute(
        "SELECT last_tickets, alert_date, alert_count, last_alert_at, detail_sig"
        " FROM line_state WHERE line_id=?",
        (line_id,),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO line_state(line_id, last_tickets, last_seen, alert_date, alert_count)"
            " VALUES(?,NULL,?,NULL,0)",
            (line_id, _local_now().isoformat(timespec="seconds")),
        )
        conn.commit()
        return None, None, 0, None, None
    return row[0], row[1], row[2], row[3], row[4]


def set_state(conn, line_id, tickets, alert_date=None, alert_count=None,
              last_alert_at=None, detail_sig=None):
    with conn:
        conn.execute("UPDATE line_state SET last_tickets=?, last_seen=? WHERE line_id=?",
                     (tickets, _local_now().isoformat(timespec="seconds"), line_id))
        if alert_date is not None:
            conn.execute("UPDATE line_state SET alert_date=?, alert_count=? WHERE line_id=?",
                         (alert_date, alert_count, line_id))
        if last_alert_at is not None:
            conn.execute("UPDATE line_state SET last_alert_at=? WHERE line_id=?",
                         (last_alert_at, line_id))
        if detail_sig is not None:
            conn.execute("UPDATE line_state SET detail_sig=? WHERE line_id=?",
                         (detail_sig, line_id))


# ---------------------------------------------------------------------------
# 余票查询
# ---------------------------------------------------------------------------

class QueryError(Exception):
    """查询异常，带可分类的 kind：auth（登录态失效）/ business / network"""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


def _post_query(cfg_api, key, line, timeout):
    """单次请求：成功返回 ticketLeftInfo，失败抛 QueryError"""
    url = cfg_api["url"]
    headers = cfg_api.get("headers") or {}
    fixed = cfg_api.get("fixed_fields") or {}
    ts = _local_now().strftime(cfg_api.get("timestamp_format", "%Y-%m-%d %H:%M:%S"))

    req = {
        "timestamp": ts,
        "data": {
            "classNo": line["class_no"],
            "type": 0,
            "routeNo": line["route_no"],
            "caseId": line["case_id"],
            "lineId": line["line_id"],
        },
    }
    req.update(fixed)
    req["sign"] = make_sign(req, key)

    r = requests.post(url, json=req, headers=headers, timeout=timeout)
    if r.status_code != 200:
        raise QueryError("network", f"HTTP {r.status_code}: {r.text[:160]}")
    resp = r.json()
    if resp.get("code") != "0000":
        # 实测 token 失效返回：{"code":500,"msg":"0005","data":"登录失败"}
        text = f'{resp.get("msg", "")} {resp.get("data", "")}'
        low = text.lower()
        if "登录" in text or "越权" in text or "0005" in text or "token" in low:
            raise QueryError("auth", "登录失效（token 已过期）")
        raise QueryError("business", f"业务错误: {text[:160]}")
    return resp.get("data", {}).get("ticketLeftInfo", [])


def query_tickets_detail(cfg_api, key, line):
    """查询并返回 (info, err)。

    err 为 None 表示成功；否则 {'kind': 'auth'|'business'|'network', 'message': str}。
    auth 类错误不做重试（token 过期重试无意义）。
    """
    timeout = cfg_api.get("timeout_seconds", 10)
    retry = cfg_api.get("retry", 2)

    last = None
    for attempt in range(retry + 1):
        try:
            return _post_query(cfg_api, key, line, timeout), None
        except QueryError as e:
            last = e
            if e.kind == "auth":
                break
        except Exception as e:  # noqa: BLE001
            last = QueryError("network", f"{type(e).__name__}: {e}"[:160])
        if attempt < retry:
            time.sleep(2)
    log.error("查询失败 %s: %s", line.get("name", "?"), last)
    return None, {"kind": last.kind, "message": last.message}


def query_tickets(cfg_api, key, line):
    """查询 line 的全部可购日期余票。成功返回 list[dict]，失败返回 None。"""
    info, _err = query_tickets_detail(cfg_api, key, line)
    return info


def _is_workday(d):
    """周一到周五视为工作日（接口返回的日期本身已跳过周末，这里再兜一层）"""
    return d.weekday() < 5


def pick_targets(info, days=3):
    """从余票列表中选出「今天之后的前 N 个工作日」的余票。

    返回 list[dict]，按日期升序，每项形如
    {"date": "2026-11-25", "tickets": 1, "total": 41, "buy_type": 3}
    不足 N 天时返回实际能取到的天数（可能为空列表）。
    """
    today = _local_today()
    rows = []
    for x in info or []:
        qd = (x.get("queryDate") or "").strip()
        if not qd:
            continue
        try:
            d = date.fromisoformat(qd)
        except ValueError:
            continue
        if d <= today or not _is_workday(d):
            continue
        rows.append(
            {
                "date": qd,
                "tickets": int(x.get("remainTicket") or 0),
                "total": int(x.get("tickeCount") or 0),
                "buy_type": x.get("buyTicketType"),
            }
        )
    rows.sort(key=lambda r: r["date"])
    return rows[:days]


def pick_target(info):
    """兼容旧接口：取下一可购工作日（等价于 pick_targets(...)[0]）。"""
    rows = pick_targets(info, 1)
    return {"queryDate": rows[0]["date"],
            "remainTicket": rows[0]["tickets"],
            "tickeCount": rows[0]["total"]} if rows else None


# ---------------------------------------------------------------------------
# 微信推送
# ---------------------------------------------------------------------------

def _channel_ready(cfg_push, channel):
    """判断某个渠道是否填了可用凭据"""
    if channel == "serverchan":
        k = cfg_push.get("serverchan_sendkey", "")
        return bool(k) and "REPLACE_ME" not in k
    if channel == "pushplus":
        t = cfg_push.get("pushplus_token", "")
        return bool(t) and "REPLACE_ME" not in t
    if channel == "wecom":
        return bool(cfg_push.get("wecom_webhook", ""))
    return False


def send_push(cfg_push, title, content, attempts=2, retry_wait=3):
    """发送推送。

    - 渠道链：主渠道 + fallback_channels（依次尝试，主渠道失败自动降级）
    - 每个渠道失败自动重试（网络抖动很常见，不应让一次失败丢掉整天的提醒）
    """
    chain = [cfg_push.get("channel", "serverchan")]
    chain += list(cfg_push.get("fallback_channels") or [])
    tried = []
    for ch in chain:
        if ch in tried:
            continue
        tried.append(ch)
        if not _channel_ready(cfg_push, ch):
            log.warning("推送渠道 %s 未配置，跳过", ch)
            continue
        sub = dict(cfg_push)
        sub["channel"] = ch
        for i in range(attempts):
            try:
                _send_push_once(sub, title, content)
                extra = ""
                if ch == "pushplus":
                    t = (cfg_push.get("pushplus_topic") or "").strip()
                    extra = f"，群组 {t}" if t else "，仅自己"
                log.info("推送成功（渠道: %s%s%s）", ch,
                         "，已降级" if len(tried) > 1 else "", extra)
                return True
            except Exception as e:  # noqa: BLE001
                log.warning("推送失败（渠道 %s，第 %d/%d 次）: %s", ch, i + 1, attempts, e)
                if i + 1 < attempts:
                    time.sleep(retry_wait)
    log.error("所有推送渠道均失败（已尝试: %s）", ", ".join(tried) or "无")
    return False


def _send_push_once(cfg_push, title, content):
    channel = cfg_push.get("channel", "serverchan")
    if channel == "serverchan":
        k = cfg_push.get("serverchan_sendkey", "")
        if not k or "REPLACE_ME" in k:
            raise RuntimeError("Server酱 SendKey 未配置")
        r = requests.post(f"https://sctapi.ftqq.com/{k}.send",
                          data={"title": title, "desp": content}, timeout=10)
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Server酱返回异常: {data}")
        return True
    if channel == "pushplus":
        tk = cfg_push.get("pushplus_token", "")
        if not tk or "REPLACE_ME" in tk:
            raise RuntimeError("PushPlus token 未配置")
        payload = {"token": tk, "title": title, "content": content, "template": "txt"}
        # 群组编码：填了则推送给该群组全部订阅者（一对多），不填只发给自己
        topic = (cfg_push.get("pushplus_topic") or "").strip()
        if topic:
            payload["topic"] = topic
        r = requests.post("https://www.pushplus.plus/send", json=payload, timeout=10)
        data = r.json()
        if data.get("code") != 200:
            raise RuntimeError(f"PushPlus返回异常: {data}")
        return True
    if channel == "wecom":
        hook = cfg_push.get("wecom_webhook", "")
        if not hook:
            raise RuntimeError("企业微信 Webhook 未配置")
        r = requests.post(hook,
                          json={"msgtype": "text", "text": {"content": f"{title}\n{content}"}},
                          timeout=10)
        data = r.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"企业微信返回异常: {data}")
        return True
    raise RuntimeError(f"未知推送渠道: {channel}")


# ---------------------------------------------------------------------------
# 监控主逻辑
# ---------------------------------------------------------------------------

def _fmt_push_body(name, rows):
    """把后 N 个工作日的余票拼成推送正文（Markdown，PushPlus/Server酱都能正常显示）"""
    lines = [f"**车次**：{name}", ""]
    for r in rows:
        mark = "**有余票**" if r["tickets"] > 0 else "无票"
        lines.append(f"- {r['date']}：{r['tickets']} / {r['total']} 张（{mark}）")
    hits = [r for r in rows if r["tickets"] > 0]
    lines.append("")
    lines.append(f"**有票日期**：{'、'.join(r['date'] for r in hits)}")
    lines.append(f"**时间**：{_local_now():%Y-%m-%d %H:%M:%S}")
    lines.append("")
    lines.append("请尽快打开「北京定制公交」小程序购票。")
    return "\n".join(lines)


def check_once(cfg, conn, key, dryrun_push=False):
    """执行一轮：逐车次查询后 N 个工作日的余票、判断状态、必要时推送。

    任一工作日余票 > 0 即视为「有票」，触发推送。
    返回 {车次名: {...结果...}}，供 Web UI 使用。
    """
    api_cfg = cfg["api"]
    alert_cfg = cfg["alert"]
    push_cfg = cfg["push"]
    days = int(alert_cfg.get("watch_workdays", 3) or 3)
    today = _local_today().isoformat()
    results = {}
    auth_failed = False
    ok_count = 0

    for line in cfg["lines"]:
        name = line["name"]
        info, qerr = query_tickets_detail(api_cfg, key, line)

        if info is None:
            kind = (qerr or {}).get("kind", "unknown")
            msg = (qerr or {}).get("message", "查询失败")
            log.warning("[%s] 查询失败（%s）: %s", name, kind, msg)
            if kind == "auth":
                results[name] = {
                    "ok": False,
                    "err_kind": "auth",
                    "error": "登录已失效（token 过期），点「刷新登录」",
                }
                auth_failed = True
            else:
                results[name] = {
                    "ok": False,
                    "err_kind": kind,
                    "error": f"查询失败（{msg}）",
                }
            continue

        rows = pick_targets(info, days)
        if not rows:
            log.warning("[%s] 未查询到未来 %s 个工作日", name, days)
            results[name] = {"ok": False, "err_kind": "nopublish",
                             "error": f"未放票（接口未返回未来 {days} 个工作日）"}
            continue

        ok_count += 1

        hit_rows = [r for r in rows if r["tickets"] > 0]
        has_ticket = bool(hit_rows)
        # 用于状态存储的「代表余票数」：有票则记有票日的最大张数，否则 0
        tickets = max((r["tickets"] for r in rows), default=0)
        detail_sig = "|".join(f"{r['date']}:{r['tickets']}" for r in rows)

        last_tickets, alert_date, alert_count, last_alert_at, last_sig = get_state(conn, line["class_no"])

        log.info(
            "[%s] 后 %s 个工作日：%s（上次=%s）",
            name, days,
            "，".join(f"{r['date']}={r['tickets']}/{r['total']}" for r in rows),
            last_tickets,
        )

        # 每日重置提醒计数
        if alert_date != today:
            alert_date, alert_count = today, 0

        max_alerts = alert_cfg.get("max_alerts_per_line_per_day", 1)
        interval_min = alert_cfg.get("realert_interval_minutes", 0)
        notify_new_day = alert_cfg.get("notify_new_ticket_day", True)

        should_alert = False
        reason = ""
        if has_ticket:
            if alert_count < max_alerts:
                should_alert = True
                reason = "有票"
            elif interval_min > 0 and last_alert_at:
                elapsed = (_local_now() - datetime.fromisoformat(last_alert_at)).total_seconds() / 60
                if elapsed >= interval_min:
                    should_alert = True
                    reason = "有票（间隔补发）"
            # 余票所在日期发生变化（比如原来只有 11-25 有票，现在 11-30 也放出来了）
            elif notify_new_day and last_sig and detail_sig != last_sig:
                should_alert = True
                reason = "票情变化"

        alerted = False
        if should_alert:
            title = f"【定制公交】{name} 有票：{'、'.join(r['date'] for r in hit_rows)}"
            content = _fmt_push_body(name, rows)
            if dryrun_push:
                log.info("[TEST] 将推送: %s", title)
                alerted = True
            else:
                alerted = send_push(push_cfg, title, content)
                if alerted:
                    log.info("[%s] 已推送提醒（%s）：%s", name, reason,
                             "、".join(f"{r['date']} {r['tickets']}张" for r in hit_rows))

            if alerted:
                alert_count += 1
                set_state(conn, line["class_no"], tickets, alert_date=today,
                          alert_count=alert_count,
                          last_alert_at=_local_now().isoformat(timespec="seconds"),
                          detail_sig=detail_sig)
            else:
                # 推送失败：不消耗当日提醒额度，下一轮继续尝试（否则一次抖动就整天静默）
                log.warning("[%s] 提醒发送失败，下一轮重试（不占用当日提醒次数）", name)
                set_state(conn, line["class_no"], tickets, detail_sig=detail_sig)
        else:
            set_state(conn, line["class_no"], tickets, detail_sig=detail_sig)

        results[name] = {
            "ok": True,
            "days": rows,
            "has_ticket": has_ticket,
            "target": rows[0]["date"],
            "tickets": tickets,
            "total": rows[0]["total"],
            "hit_dates": [r["date"] for r in hit_rows],
            "alerted": alerted,
            "alert_count_today": alert_count,
        }

    conn.commit()

    # 登录态失效主动告警（每天最多一次，避免 token 过期后一直静默）
    # 去重标记存在数据库里：云端定时任务每次都是新进程，进程内变量跨轮次无效，
    # 否则 token 一过期会每轮推一条（每 5 分钟一次 = 一天 288 条）
    if auth_failed and alert_cfg.get("notify_on_auth_error", True):
        global _auth_alert_date
        last_auth_alert = kv_get(conn, "auth_alert_date")
        if last_auth_alert != today:
            title = "【定制公交】登录已失效，余票监控暂停"
            content = (
                "**问题**：登录态（token）已过期，接口返回「登录失败」，当前无法获取余票。\n\n"
                f"**时间**: {_local_now():%Y-%m-%d %H:%M:%S}\n\n"
                "**处理**：打开「定制公交余票监控」窗口 → 点「刷新登录」→ 输入手机号与短信验证码，"
                "登录后自动恢复监控。\n\n"
                "在你处理之前，程序会继续重试；恢复后本条告警当天不再重复发送。"
            )
            sent = False
            if dryrun_push:
                log.info("[TEST] 将推送登录失效告警")
            elif send_push(push_cfg, title, content):
                log.warning("已推送「登录失效」告警")
                sent = True
            # 只有真的发出去才记标记，发送失败下一轮还能重试
            if sent or dryrun_push:
                kv_set(conn, "auth_alert_date", today)
                _auth_alert_date = today

    return results


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------

def cli_loop(cfg):
    """命令行常驻循环（无 Web UI 时使用）。"""
    key = RSA.import_key(open(os.path.join(BASE_DIR, cfg["api"]["private_key_pem"])).read())
    conn = db()

    _running = True

    def _stop(signum, frame):  # noqa: ARG001
        nonlocal _running
        _running = False
        log.info("收到退出信号 %s，准备退出", signum)

    signal.signal(signal.SIGINT, _stop)
    try:
        signal.signal(signal.SIGTERM, _stop)
    except (AttributeError, ValueError):
        pass

    interval = cfg["alert"].get("query_interval_seconds", 60)
    days = int(cfg["alert"].get("watch_workdays", 3) or 3)
    log.info("监控启动：车次=%s，间隔=%ss，目标=后 %s 个工作日（任一天有余票即推送）",
             [l["class_no"] for l in cfg["lines"]], interval, days)
    while _running:
        start = time.monotonic()
        try:
            check_once(cfg, conn, key)
        except Exception:  # noqa: BLE001
            log.exception("一轮查询发生未捕获异常")
        elapsed = time.monotonic() - start
        sleep_s = max(0.0, interval - elapsed)
        end = time.monotonic() + sleep_s
        while _running and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))
    log.info("监控退出")
    conn.close()


def main():
    cfg = json.load(open(CONFIG_PATH, encoding="utf-8"))

    if "--test-push" in sys.argv:
        ok = send_push(cfg["push"], "【定制公交】测试消息", "这是一条测试推送，收到即通路正常。")
        log.info("测试推送结果: %s", "成功" if ok else "失败")
        return

    if "--once" in sys.argv:
        key = RSA.import_key(open(os.path.join(BASE_DIR, cfg["api"]["private_key_pem"])).read())
        conn = db()
        results = check_once(cfg, conn, key)

        # 给 CI/脚本用的结构化输出：本轮是否发生过推送
        # 云端定时任务靠这个决定「要不要把 state.db 提交回去」，避免每轮都产生一次提交
        any_alert = any(r.get("alerted") for r in results.values() if isinstance(r, dict))
        any_ok = any(r.get("ok") for r in results.values() if isinstance(r, dict))
        print(f"::MONITOR_RESULT alerted={'true' if any_alert else 'false'} "
              f"ok={'true' if any_ok else 'false'}")
        log.info("单轮结束：%s，有票推送=%s", "查询正常" if any_ok else "查询失败",
                 "是" if any_alert else "否")
        conn.close()

        # 退出码：查询全部失败时返回 1，让 Actions 标红提醒（但不影响推送逻辑）
        if not any_ok:
            sys.exit(1)
        return

    cli_loop(cfg)


if __name__ == "__main__":
    main()
