#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
校园网自动登录（锐捷 Ruijie SAM+ 新门户 / 公安定制版）

用法：
    python 校园网自动登录.py              # 正常模式：检测网络，需要就登录
    python 校园网自动登录.py --诊断        # 详细探测，把看到的一切写进日志（不发登录请求）
    python 校园网自动登录.py --测试        # 只做一次探测+登录尝试，不重试
    python 校园网自动登录.py --检查        # 只报告当前是否在线

设计要点：
  * 只用 Python 标准库，不依赖 requests / selenium 等第三方包。
  * 账号密码从同目录的 校园网配置.json 读取，日志中一律脱敏为 ***。
  * 登录协议按逆向出的锐捷门户接口实现，并内置多条候选路径，运行时会
    逐条尝试并把每条的原始响应写进日志，便于在真实断网环境下自我诊断。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import socket
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

# ---------------------------------------------------------------- 基本路径

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "校园网配置.json")
LOG_DIR = os.path.join(APP_DIR, "日志")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_CONFIG = {
    "userName": "",
    "password": "",
    "portal_ip": "10.254.241.66",
    "portal_url": "http://10.254.241.66/portal/entry/pc/finish;flowParams=undefined;from=",
    "flowKey": "portal_auth",
    "authType": None,
    # 运营商。门户列出来的是 [{"key":"电信","value":"ctcc"}, ...]，
    # serviceLogin 接口要的是 value，所以这里填 ctcc / cmcc / unicom，
    # 写中文「电信/移动/联通」也会自动归一化。留空则自动挑列表里第一个。
    "service": "",
    # 前两个是纯 IP 直连（不需要 DNS）：未认证时校园网往往连域名都不给解析，
    # 只有这种"直接甩 IP 的 HTTP 请求"才发得出去 —— 而它一出去就会被交换机
    # 拦下来跳到门户，sessionId 就在那个跳转地址里。这是断网时拿参数的关键。
    # 顺序有讲究：223.5.5.5 是国内地址，最稳，放第一个能让在线探测立刻返回；
    # 1.1.1.1（Cloudflare）在国内部分网络下会超时，每次白白多等 6 秒。
    "check_urls": [
        "http://223.5.5.5/",
        "http://1.1.1.1/",
        "http://connect.rom.miui.com/generate_204",
        "http://www.baidu.com",
    ],
    "check_timeout_seconds": 6,
    # 兼容模式：是否尝试已确认失效的老接口（会把密码发给无效端点），默认关闭
    "enable_legacy_strategies": False,
    "retry_interval_seconds": 30,
    "max_retries": 3,
    "verify_wait_seconds": 3,
    "startup_delay_seconds": 20,
    "log_keep_days": 30,
    "daemon_interval_seconds": 60,
    # 掉线时的重试间隔。故意比在线探测频繁得多：
    # 在线时网是好的，慢慢查就行了；一旦掉线，尤其是刚开机网卡还没就绪的时候，
    # 越快重试越早恢复上网。
    "daemon_offline_interval_seconds": 20,
}

# 守护模式单实例锁使用的本地端口
_DAEMON_LOCK_PORT = 51873

# 门户返回的服务列表形如 [{"key":"电信","value":"ctcc"}, {"key":"移动","value":"cmcc"},
# {"key":"联通","value":"unicom"}]；而 serviceLogin 接口要的 service 字段是 value
# （前端源码里的调用是 serviceLogin({sessionId, service: selectType.value})）。
# 这里允许配置里写中文或英文，统一归一化成 value。
_SERVICE_ALIAS = {
    "电信": "ctcc", "中国电信": "ctcc", "ctcc": "ctcc", "telecom": "ctcc",
    "移动": "cmcc", "中国移动": "cmcc", "cmcc": "cmcc", "mobile": "cmcc",
    "联通": "unicom", "中国联通": "unicom", "unicom": "unicom", "cucc": "unicom",
}


def normalize_service(v) -> str:
    """把配置/服务列表里的运营商名归一化成接口要的 value（ctcc/cmcc/unicom）。"""
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    return _SERVICE_ALIAS.get(s) or _SERVICE_ALIAS.get(s.lower()) or s

# ---------------------------------------------------------------- 日志

_LOG_FP = None
_LOG_TEE = True


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str, level: str = "INFO") -> None:
    line = f"[{_now()}] [{level}] {msg}"
    if _LOG_FP is not None:
        try:
            _LOG_FP.write(line + "\n")
            _LOG_FP.flush()
        except Exception:
            pass
    if _LOG_TEE:
        try:
            print(line)
        except Exception:
            pass


def _setup_log() -> str:
    global _LOG_FP
    os.makedirs(LOG_DIR, exist_ok=True)
    name = datetime.datetime.now().strftime("校园网-%Y%m%d.log")
    path = os.path.join(LOG_DIR, name)
    _LOG_FP = open(path, "a", encoding="utf-8")
    return path


def _cleanup_logs(keep_days: int) -> None:
    try:
        cutoff = time.time() - max(1, int(keep_days)) * 86400
        for fn in os.listdir(LOG_DIR):
            p = os.path.join(LOG_DIR, fn)
            if fn.endswith(".log") and os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
    except Exception:
        pass


def mask(text: str, cfg: dict) -> str:
    """把日志里的密码抹掉。"""
    if not text:
        return text
    for key in ("password",):
        val = cfg.get(key) or ""
        if isinstance(val, str) and len(val) >= 2:
            text = text.replace(val, "***")
    return text


# ---------------------------------------------------------------- HTTP 基础

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止自动跟随重定向，这样才能拿到门户的 Location。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _base_headers(cfg: dict, referer: str | None = None) -> dict:
    ip = cfg.get("portal_ip", "")
    h = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if ip:
        h["Origin"] = f"http://{ip}"
        h["Referer"] = referer or f"http://{ip}/portal/entry/pc/login"
    return h


def http_request(
    url: str,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict | None = None,
    timeout: float = 10,
    opener=None,
    no_redirect: bool = False,
) -> dict:
    """发一个请求，永远不抛异常，返回结构化结果。"""
    hdr = dict(headers or {})
    if no_redirect:
        if opener is None:
            op = urllib.request.build_opener(_NoRedirect)
        else:
            # 关键：不能直接 build_opener(_NoRedirect) —— 那会丢掉调用方的 Cookie 处理，
            # 于是 index.jsp 那一跳下发的 JSESSIONID 在后续 POST 里就不会被带上。
            # 这里把原 opener 上的 Cookie 处理器摘出来复用，只替换重定向处理器。
            keep = [h for h in opener.handlers
                    if isinstance(h, urllib.request.HTTPCookieProcessor)]
            op = urllib.request.build_opener(_NoRedirect, *keep)
    else:
        op = opener if opener is not None else urllib.request.build_opener()
    req = urllib.request.Request(url, data=body, headers=hdr, method=method)
    out = {
        "url": url,
        "method": method,
        "status": None,
        "location": None,
        "content_type": None,
        "text": "",
        "error": None,
        "headers": {},
        "final_url": url,
    }
    try:
        r = op.open(req, timeout=timeout)
        out["status"] = r.status
        out["headers"] = dict(r.headers)
        out["content_type"] = r.headers.get("Content-Type", "")
        out["location"] = r.headers.get("Location")
        # 跟随重定向后 r.geturl() 才是最终落点 —— sessionId 就在那一跳里
        try:
            out["final_url"] = r.geturl()
        except Exception:
            pass
        out["text"] = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        out["status"] = e.code
        out["headers"] = dict(e.headers or {})
        out["content_type"] = (e.headers or {}).get("Content-Type", "")
        out["location"] = (e.headers or {}).get("Location")
        try:
            out["text"] = e.read().decode("utf-8", "replace")
        except Exception:
            out["text"] = ""
    except urllib.error.URLError as e:
        out["error"] = f"URLError: {getattr(e, 'reason', e)}"
    except socket.timeout:
        out["error"] = "timeout"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def http_post_json(url: str, obj, headers: dict, timeout: float = 10, opener=None) -> dict:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    h = dict(headers)
    h["Content-Type"] = "application/json;charset=UTF-8"
    return http_request(url, "POST", data, h, timeout, opener=opener, no_redirect=True)


def http_post_form(url: str, fields: dict, headers: dict, timeout: float = 10, opener=None) -> dict:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    h = dict(headers)
    h["Content-Type"] = "application/x-www-form-urlencoded"
    return http_request(url, "POST", data, h, timeout, opener=opener, no_redirect=True)


def parse_json(text: str):
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


# ---------------------------------------------------------------- 配置

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if not k.startswith("_"):
                        cfg[k] = v
        except Exception as e:
            print(f"配置文件读取失败：{e}")
    return cfg


def validate_config(cfg: dict) -> bool:
    u = (cfg.get("userName") or "").strip()
    p = (cfg.get("password") or "").strip()
    bad = (not u) or (not p) or ("在这里填" in u) or ("在这里填" in p)
    if bad:
        log("配置文件里还没有填账号或密码，请编辑 " + CONFIG_PATH, "ERROR")
        return False
    return True


# ---------------------------------------------------------------- 网络探测

PORTAL_HINTS = (
    "portal/entry",
    "wlanuserip",
    "wlanacname",
    "eportal",
    "sessionId=",
    "/portal/",
)


def _looks_like_portal(text: str, cfg: dict) -> bool:
    if not text:
        return False
    ip = cfg.get("portal_ip") or ""
    if ip and ip in text:
        return True
    low = text
    for h in PORTAL_HINTS:
        if h in low:
            return True
    return False


def _extract_portal_url(text: str, cfg: dict) -> str | None:
    """从 HTML / JS 里把门户跳转地址抠出来。"""
    if not text:
        return None
    ip = cfg.get("portal_ip") or ""
    patterns = [
        r"""https?://%s[^\s"'<>\\]*""" % re.escape(ip),
        r"""https?://[^\s"'<>\\]*?/portal/entry/[^\s"'<>\\]*""",
        r"""(?:location\.href|location\.replace\(|window\.location)\s*=?\s*["']([^"']+)["']""",
        r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]+url=([^"'>\s]+)""",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            val = m.group(1) if m.groups() else m.group(0)
            val = val.strip().strip("'\"")
            if val.lower().startswith("http"):
                return val
    return None


def probe(cfg: dict, verbose: bool = False) -> dict:
    """
    检测是否在线。
    返回 {online: bool, portal_url: str|None, reason: str, raw: [...]}
    """
    result = {"online": False, "portal_url": None, "reason": "", "raw": []}
    timeout = float(cfg.get("check_timeout_seconds", 6))
    headers = {"User-Agent": UA, "Accept": "*/*", "Connection": "close"}

    for url in cfg.get("check_urls", []):
        r = http_request(url, "GET", None, headers, timeout, no_redirect=True)
        brief = {
            "url": url,
            "status": r["status"],
            "location": r["location"],
            "error": r["error"],
            "len": len(r["text"]),
        }
        result["raw"].append(brief)
        if verbose:
            log(f"  探测 {url} -> status={r['status']} loc={r['location']} err={r['error']} len={len(r['text'])}")

        if r["error"]:
            continue

        # 明确的重定向到门户
        loc = r["location"] or ""
        if loc and _looks_like_portal(loc, cfg):
            result["portal_url"] = urllib.parse.urljoin(url, loc)
            result["reason"] = f"{url} 被重定向到门户"
            save_evidence("重定向", f"探测地址: {url}\nLocation: {loc}\n\n正文:\n{r['text'][:20000]}")
            return result

        # 只要真的收到了 HTTP 响应，就说明这条路通到外面了：
        #   2xx/204 正常；3xx 是站点自身的跳转（指向门户的上面已挡掉）；
        #   4xx/5xx 也一样（例如 223.5.5.5 对根路径就回 404）—— 都算在线。
        # 断网时这些请求会被交换机接管，变成跳门户或门户正文，走上面的分支。
        if r["status"]:
            if _looks_like_portal(r["text"], cfg):
                pu = _extract_portal_url(r["text"], cfg)
                result["portal_url"] = pu
                result["reason"] = f"{url} 返回了门户内容"
                save_evidence(
                    "门户正文",
                    f"探测地址: {url}\nstatus: {r['status']}\n\n正文:\n{r['text'][:20000]}",
                )
                return result
            result["online"] = True
            result["reason"] = f"{url} 正常（status={r['status']}）"
            return result

    if not result["reason"]:
        result["reason"] = "所有检测地址都无法访问"
    return result


# ---------------------------------------------------------------- 会话参数

SESSION_KEYS = (
    "sessionId",
    "userIp",
    "userMac",
    "nodeMac",
    "mac",
    "nasIp",
    "wlanacname",
    "acName",
    "ssid",
    "accessType",
    "redirectUrl",
    "flowKey",
    "workFlowKey",
    "flowParams",
    "from",
    "mode",
    "customer",
    "guestFlow",
    "waitMac",
    "terminalType",
    "accessTime",
    "customPageId",
    "valid",
)


def parse_portal_url(url: str) -> dict:
    """把门户跳转 URL 里的参数全部解析出来。"""
    out = {"_url": url or ""}
    if not url:
        return out
    try:
        sp = urllib.parse.urlsplit(url)
    except Exception:
        return out
    out["_path"] = sp.path
    # SPA 矩阵参数（;flowParams=xxx;from=）
    for part in sp.path.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out.setdefault(k.strip(), urllib.parse.unquote(v.strip()))
    for k, vs in urllib.parse.parse_qs(sp.query, keep_blank_values=True).items():
        out[k] = vs[0] if len(vs) == 1 else vs
    out["_query"] = sp.query
    return out


def build_query_string_candidates(params: dict) -> list:
    """新老协议对 queryString 的理解不同，这里给出全部候选。"""
    cands = []
    for key in ("redirectUrl", "queryString", "_query"):
        v = params.get(key)
        if isinstance(v, str) and v:
            cands.append(v)
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def local_ip(target: str = "10.254.241.66") -> str:
    """查本机到门户的出口 IP（只做一次路由查询，不真正发包）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target.split(":")[0], 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return ""


def resolve_redirect_info(cfg: dict, query: str, verbose: bool = True) -> dict:
    """
    兜底：拿地址栏上的 query 去问门户「这个终端是谁」。

    前端逻辑（chunks/7243 的路由守卫）是：
        Dr = location.href.slice(location.href.indexOf("?") + 1)     # 整个 query 原样
        GET /eportal/json/resolveRedirectInfo/resolve?<Dr>
    返回的 message 里有 userIp / userMac / nasIp / acName / ssid / accessType / redirectUrl。

    另外前端还有一条按 MAC 查的路径（deCodeURL）：
        GET /resolveRedirectInfo/resolve?jsonp=getJSONP.getJSONPjje&?nodeMac=<mac>
    （那个多余的 "?" 是前端原样拼出来的，这里照抄。）

    两条都是只读接口，不含账号密码。
    """
    base = f"http://{cfg['portal_ip']}"
    headers = _base_headers(cfg, referer=base + "/")
    q = (query or "").lstrip("?")
    if q:
        # 前端原做法：把地址栏上那一整串 query 原样接在后面
        path = "/eportal/json/resolveRedirectInfo/resolve?" + q
    else:
        # 没有 query 时，用「本机 IP + MAC」问一次。
        # 实测：wlanuserip/wlanusermac 这两个参数名有效，服务器据此解析出了真实 nasIp，
        # 但因为缺少交换机原样的重定向参数串，valid 仍为 false。
        mi = local_ip(cfg["portal_ip"])
        mm = local_mac().replace("-", "").lower()
        args = []
        if mi:
            args.append("wlanuserip=" + urllib.parse.quote(mi))
        if mm:
            args.append("wlanusermac=" + urllib.parse.quote(mm))
        if not args:
            return {}
        path = "/eportal/json/resolveRedirectInfo/resolve?" + "&".join(args)

    if verbose:
        log(f"  拿 query 向门户解析终端信息：GET {path[:300]}")
    r = http_request(base + path, "GET", None, headers, 10, no_redirect=True)
    if verbose:
        log(f"      -> status={r['status']} {mask(r['text'][:300], cfg)}")
    j = parse_json(r["text"])
    msg = j.get("message") if isinstance(j, dict) else None
    if not isinstance(msg, dict):
        return {}
    if verbose:
        if msg.get("valid"):
            log("      门户确认这个终端有效。")
        else:
            log(f"      门户说解析无效：{msg.get('message') or '(没有说明)'}")
    return {k: v for k, v in msg.items() if v not in (None, "", [], {})}


def save_evidence(tag: str, content: str) -> str | None:
    """把门户返回的原始内容另存一份，方便排查。里面不会有账号密码。"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        name = f"门户原始内容-{datetime.datetime.now():%Y%m%d-%H%M%S}-{tag}.txt"
        path = os.path.join(LOG_DIR, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content or "")
        log(f"      门户原始内容已存到：{path}")
        return path
    except Exception:
        return None


def follow_portal_redirect(cfg: dict, portal_url: str, opener, verbose: bool = True) -> tuple:
    """
    本部署最关键的一跳。

    交换机拦下请求后给的是 /eportal/index.jsp?wlanuserip=...&wlanacname=...，
    这条地址自己还会再 302 一次，跳到
    /portal/portal-main?sessionId=xxx&userIp=...&nasIp=...&customPageId=...&...
    sessionId 就是那一跳现场生成的（每次都不一样、用过即换），
    所以不跟这一跳就永远拿不到它 —— 这正是之前一直 "没有 sessionId" 的真正原因。

    返回 (最终URL, 参数dict)；跟不动就原样返回。
    """
    if not portal_url or opener is None:
        return portal_url, {}
    headers = _base_headers(cfg)
    r = http_request(portal_url, "GET", None, headers, 15, opener=opener, no_redirect=False)
    final = r.get("final_url") or portal_url
    if verbose:
        log(f"  跟随门户跳转：{portal_url}")
        log(f"      -> {final}")
        if r["error"]:
            log(f"      出错：{r['error']}")
    if not final or final == portal_url:
        return portal_url, {}
    return final, parse_portal_url(final)


def collect_session(cfg: dict, portal_url: str | None, verbose: bool = True, opener=None) -> dict:
    """
    拿到 sessionId 等会话参数。

    顺序：
      1) NAS 重定向 URL 上的参数
      2) 关键：跟着那条地址再跳一次，sessionId 就在这一跳里（本部署的唯一来源）
      3) 还没有，就用 query 去 resolveRedirectInfo 换终端信息
      4) 再不行，退回配置里的 portal_url
    """
    if portal_url:
        p = parse_portal_url(portal_url)
        p["_source"] = "NAS重定向"
        if verbose:
            log(f"  门户跳转 URL：{portal_url}")
    else:
        p = parse_portal_url(cfg.get("portal_url") or "")
        p["_source"] = "配置文件"
        if verbose:
            log(f"  未捕获到 NAS 重定向，改用配置里的 portal_url：{cfg.get('portal_url')}")

    # 关键一跳
    if not p.get("sessionId"):
        final, fp = follow_portal_redirect(cfg, p.get("_url") or portal_url or "", opener, verbose)
        if fp.get("sessionId"):
            fp["_source"] = "跟随门户跳转"
            p = fp
            if verbose:
                log("  [√] 已从这一跳里拿到 sessionId。")

    if not p.get("sessionId"):
        merged = resolve_redirect_info(cfg, p.get("_query") or "", verbose)
        if merged:
            p.update(merged)
            p["_source"] += "+resolve解析"

    if verbose:
        for k in SESSION_KEYS:
            if k in p:
                log(f"    {k} = {p[k]}")
        if not p.get("sessionId"):
            log("  [!] 这次没拿到 sessionId。它是登录必需的，通常由交换机在重定向时写在 URL 上。")
            log("      请在被门户拦住的状态下再跑一次 --诊断，并把日志发回。")

    return p


def local_mac() -> str:
    try:
        n = uuid.getnode()
        s = f"{n:012x}"
        return "-".join(s[i : i + 2] for i in range(0, 12, 2)).upper()
    except Exception:
        return ""


# ---------------------------------------------------------------- 登录策略
#
# 每条策略返回 (是否发送成功, 说明, 响应对象)
# 判定“成功”统一以“再次探测已在线”为准，这里只负责把请求发出去。

def _log_req(tag: str, path: str, payload, cfg: dict) -> None:
    log(f"  [{tag}] POST {path}")
    log(f"        payload = {mask(json.dumps(payload, ensure_ascii=False), cfg)}")


def strategy_workflow(cfg: dict, params: dict, opener, headers: dict) -> list:
    """策略 A：锐捷工作流（/eportal/workFlow/*）—— 本部署实测可达。"""
    base = f"http://{cfg['portal_ip']}"
    sid = params.get("sessionId") or ""
    flow_key = params.get("flowKey") or params.get("workFlowKey") or cfg.get("flowKey") or "portal_auth"
    if not sid:
        return [(False, "没有 sessionId，跳过工作流方式", None)]

    results = []

    # A1 查询当前节点
    p1 = {"sessionId": sid, "flowKey": flow_key}
    _log_req("A1", "/eportal/workFlow/getCurrentNode", p1, cfg)
    r1 = http_post_json(base + "/eportal/workFlow/getCurrentNode", p1, headers, opener=opener)
    log(f"        -> {r1['status']} {mask(r1['text'][:400], cfg)}")
    results.append((r1["status"] == 200, "getCurrentNode", r1))

    node = ""
    j1 = parse_json(r1["text"])
    if isinstance(j1, dict):
        d = j1.get("data")
        if isinstance(d, dict):
            node = d.get("currentNodePath") or d.get("nodePath") or ""
        elif isinstance(d, str):
            node = d
    log(f"        当前节点 currentNodePath = {node!r}")
    params["_last_node"] = node

    # A2 提交账号密码
    cred = {
        "userName": (cfg.get("userName") or "").strip(),
        "userPwd": cfg.get("password") or "",
        "authType": cfg.get("authType"),
    }
    p2 = {"result": "success", "sessionId": sid, "updateSessionData": cred}
    _log_req("A2", "/eportal/workFlow/updateUserSession", p2, cfg)
    r2 = http_post_json(base + "/eportal/workFlow/updateUserSession", p2, headers, opener=opener)
    log(f"        -> {r2['status']} {mask(r2['text'][:400], cfg)}")
    results.append((r2["status"] == 200, "updateUserSession", r2))

    # A3 再查一次节点，看流程是否推进
    _log_req("A3", "/eportal/workFlow/getCurrentNode", p1, cfg)
    r3 = http_post_json(base + "/eportal/workFlow/getCurrentNode", p1, headers, opener=opener)
    log(f"        -> {r3['status']} {mask(r3['text'][:400], cfg)}")
    results.append((r3["status"] == 200, "getCurrentNode(复核)", r3))

    j3 = parse_json(r3["text"])
    if isinstance(j3, dict):
        d3 = j3.get("data")
        if isinstance(d3, dict):
            node3 = d3.get("currentNodePath") or ""
            if node3:
                params["_last_node"] = node3
                log(f"        提交账号后节点 currentNodePath = {node3!r}")

    return results


def strategy_flowprocedure(cfg: dict, params: dict, opener, headers: dict) -> list:
    """策略 B：公安定制版 /portal/flowProcedure/auth（走 smpdomain 前缀）。"""
    base = f"http://{cfg['portal_ip']}"
    qs_cands = build_query_string_candidates(params)
    results = []
    for prefix in ("/api/aggregation", "", "/sam"):
        for qs in (qs_cands or [None]):
            payload = {
                "userId": (cfg.get("userName") or "").strip(),
                "password": cfg.get("password") or "",
                "service": cfg.get("service"),
                "queryString": qs,
                "autoLogin": False,
                "macAddr": params.get("userMac") or params.get("nodeMac") or params.get("mac") or local_mac(),
                "mode": params.get("mode") or "normal",
            }
            path = prefix + "/portal/flowProcedure/auth"
            _log_req("B", path, payload, cfg)
            r = http_post_json(base + path, payload, headers, opener=opener)
            log(f"        -> {r['status']} loc={r['location']} {mask(r['text'][:300], cfg)}")
            results.append((r["status"] == 200, f"flowProcedure/auth{prefix} qs={str(qs)[:40]}", r))
    return results


def strategy_portalauthen_login(cfg: dict, params: dict, opener, headers: dict) -> list:
    """策略 C：标准 SAM 门户 /portal/portalAuthen/login。"""
    base = f"http://{cfg['portal_ip']}"
    qs_cands = build_query_string_candidates(params)
    results = []
    for prefix in ("/api/aggregation", ""):
        for qs in (qs_cands or [None]):
            payload = {
                "userId": (cfg.get("userName") or "").strip(),
                "password": cfg.get("password") or "",
                "service": cfg.get("service"),
                "queryString": qs,
                "autoLogin": False,
                "macAddr": params.get("userMac") or local_mac(),
                "mode": params.get("mode") or "normal",
            }
            path = prefix + "/portal/portalAuthen/login"
            _log_req("C", path, payload, cfg)
            r = http_post_json(base + path, payload, headers, opener=opener)
            log(f"        -> {r['status']} loc={r['location']} {mask(r['text'][:300], cfg)}")
            results.append((r["status"] == 200, f"portalAuthen/login{prefix}", r))
    return results


def strategy_service_login(cfg: dict, params: dict, opener, headers: dict) -> list:
    """策略 D：先选服务再登录（/eportal/network/*）。

    前端源码依据（chunk 2972 ServiceSelectionModule）：
        getServiceList({sessionId})         -> POST /eportal/network/serviceSelection
        serviceLogin({sessionId, service})  -> POST /eportal/network/serviceLogin
    其中 service 取的是列表项的 value（ctcc/cmcc/unicom），不是 key（电信/移动/联通）。
    判定成功要看 data.authResult === "success"，失败原因在 data.authMessage。
    """
    base = f"http://{cfg['portal_ip']}"
    sid = params.get("sessionId") or ""
    if not sid:
        return [(False, "没有 sessionId，跳过服务方式", None)]
    results = []

    p1 = {"sessionId": sid}
    _log_req("D1", "/eportal/network/serviceSelection", p1, cfg)
    r1 = http_post_json(base + "/eportal/network/serviceSelection", p1, headers, opener=opener)
    log(f"        -> {r1['status']} {mask(r1['text'][:500], cfg)}")
    results.append((r1["status"] == 200, "serviceSelection", r1))

    # 把可选运营商记进日志，方便排障时核对
    options = []
    j1 = parse_json(r1["text"])
    if isinstance(j1, dict) and isinstance(j1.get("data"), list):
        for it in j1["data"]:
            if isinstance(it, dict):
                options.append((it.get("value"), it.get("key")))
    if options:
        log("        可选运营商：" + "、".join(f"{k}({v})" for v, k in options))

    # 配置优先；没配就自动挑列表里第一个
    service = normalize_service(cfg.get("service"))
    if not service and options:
        service = options[0][0] or ""
        log(f"        配置里没写运营商，自动选第一个：{service}")

    if not service:
        log("        没有可选服务，跳过 serviceLogin。", "WARN")
        return results

    p2 = {"sessionId": sid, "service": service}
    _log_req("D2", "/eportal/network/serviceLogin", p2, cfg)
    r2 = http_post_json(base + "/eportal/network/serviceLogin", p2, headers, opener=opener)
    log(f"        -> {r2['status']} {mask(r2['text'][:500], cfg)}")
    results.append((_service_login_ok(r2, service), f"serviceLogin({service})", r2))
    return results


def _service_login_ok(r: dict, service: str) -> bool:
    """serviceLogin 的成败不只看 HTTP 200，还要看 data.authResult。"""
    if r.get("status") != 200:
        return False
    j = parse_json(r.get("text") or "")
    if isinstance(j, dict):
        d = j.get("data")
        if isinstance(d, dict):
            auth = d.get("authResult")
            if auth is not None:
                if auth == "success":
                    return True
                log(f"        门户说运营商 {service} 认证失败：{d.get('authMessage')}", "WARN")
                return False
    return True


def strategy_useronline(cfg: dict, params: dict, opener, headers: dict) -> list:
    """策略 E：查在线状态（只读，用于确认会话有效）。"""
    base = f"http://{cfg['portal_ip']}"
    sid = params.get("sessionId")
    payload = {"sessionId": sid} if sid else {}
    _log_req("E", "/eportal/network/userOnline", payload, cfg)
    r = http_post_json(base + "/eportal/network/userOnline", payload, headers, opener=opener)
    log(f"        -> {r['status']} {mask(r['text'][:400], cfg)}")
    return [(r["status"] == 200, "userOnline", r)]


# ---------------------------------------------------------------- 主流程

def _verify_online(cfg: dict, tag: str) -> bool:
    """等一小会儿再探测，确认是不是真的通了。"""
    wait = float(cfg.get("verify_wait_seconds", 3))
    log(f"  等待 {wait:.0f} 秒后验证网络 ……")
    time.sleep(wait)
    vr = probe(cfg, verbose=False)
    log(f"  {tag} 验证结果：online={vr['online']}（{vr['reason']}）")
    return bool(vr["online"])


def _sid_dead(results: list) -> bool:
    """从响应文本判断 sessionId 是不是已经失效了。"""
    for item in results:
        r = item[2] if len(item) > 2 else None
        if not r:
            continue
        t = r.get("text") or ""
        if ("没有查找到当前sessionId" in t) or ("invalid sessionId" in t):
            return True
    return False


def run_login_once(cfg: dict, do_probe: bool = True, verbose: bool = True, force: bool = False) -> bool:
    """执行一次「探测 -> 收集会话 -> 逐条尝试 -> 一旦成功立刻停止」，返回是否已在线。"""
    if do_probe:
        log("检测网络状态 ……")
        pr = probe(cfg, verbose=verbose)
        log(f"  结果：online={pr['online']} 说明：{pr['reason']}")
        if pr["online"] and not force:
            log("网络已经可用，无需登录。")
            return True
        portal_url = pr["portal_url"]
    else:
        portal_url = None

    # CookieJar / opener 必须在这里就建好：拿 sessionId 那一跳会下发 JSESSIONID，
    # 后面的登录接口要带上同一个会话。
    cj = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    headers = _base_headers(cfg)

    params = collect_session(cfg, portal_url, verbose=verbose, opener=opener)
    sid = params.get("sessionId")
    log(f"  会话来源：{params.get('_source')}   sessionId={sid!r}")
    if not sid:
        log("  警告：没有拿到 sessionId，登录接口多半会失败。", "WARN")
        log("        详细情况见 --诊断 输出。", "WARN")

    log("开始尝试登录 ……")

    # 0) 只读，确认会话状态，不含账号密码
    strategy_useronline(cfg, params, opener, headers)

    # 1) 工作流方式 —— 本部署实测可用的入口
    res = strategy_workflow(cfg, params, opener, headers)
    if _sid_dead(res):
        log("  sessionId 已失效，本次不再继续尝试。", "WARN")
        log("  请重新触发一次认证（用浏览器随便打开一个网页，等它跳到门户）后再运行。", "WARN")
        return False
    # 门户把流程停在「选运营商」这一步时，说明账号已经过了，只差选服务放行网络；
    # 这时候没必要白等一轮探测，直接进服务选择。
    if params.get("_last_node") == "serviceSelection":
        log("  门户要求先选运营商，直接进入服务选择 ……")
    elif _verify_online(cfg, "工作流方式"):
        log("登录成功（工作流方式），网络已可用。")
        return True

    # 2) 先选服务再登录
    strategy_service_login(cfg, params, opener, headers)
    if _verify_online(cfg, "服务选择方式"):
        log("登录成功（服务选择方式），网络已可用。")
        return True

    # 3) 兼容模式：老接口。
    #    已实测确认这些接口在本部署不可用（405 / 302），打开只会
    #    把账号密码白白发给一堆无效端点，所以默认关闭。
    if cfg.get("enable_legacy_strategies"):
        log("兼容模式已开启，尝试老接口 ……", "WARN")
        strategy_flowprocedure(cfg, params, opener, headers)
        if _verify_online(cfg, "flowProcedure 方式"):
            return True
        strategy_portalauthen_login(cfg, params, opener, headers)
        if _verify_online(cfg, "portalAuthen 方式"):
            return True

    log("仍未联网。", "WARN")
    return False


def diagnose(cfg: dict) -> int:
    """诊断模式：把能看到的一切都写进日志，不发账号密码。"""
    log("=" * 70)
    log("诊断模式开始（不会提交账号密码）")
    log(f"本机主机名：{socket.gethostname()}")
    log(f"本机 MAC ：{local_mac()}")
    log(f"门户 IP  ：{cfg.get('portal_ip')}")
    log(f"门户 URL ：{cfg.get('portal_url')}")

    log("-" * 70)
    log("一、连通性探测")
    pr = probe(cfg, verbose=True)
    log(f"  结论：online={pr['online']}  说明：{pr['reason']}")
    log(f"  捕获到的门户地址：{pr['portal_url']}")
    dns_failed = [x for x in pr["raw"] if x.get("error") and "getaddrinfo" in str(x["error"])]
    if dns_failed:
        log(f"  [线索] 有 {len(dns_failed)} 个域名检测地址解析失败——未认证时校园网常常连 DNS 都不放行，")
        log("         所以域名类检测地址在那个时刻根本发不出请求（这不是程序坏了）。")
        log("         程序已自动改用纯 IP 直连的检测地址去撞交换机：请看下面 1.1.1.1 / 223.5.5.5")
        log("         那两行的 status 和 loc——如果它们变成了跳向 10.254.241.66 的 302，")
        log("         那个 Location 里就带着登录要用的 sessionId。")

    log("-" * 70)
    log("二、直接访问门户与候选接口（GET，只读）")
    base = f"http://{cfg['portal_ip']}"
    headers = _base_headers(cfg)
    for path in (
        "/",
        "/self/index",
        "/portal/entry/pc/login",
        cfg.get("portal_url") or "/portal/entry/pc/finish",
        "/eportal/network/getIdentityConfig",
    ):
        if not path:
            continue
        url = path if path.startswith("http") else base + path
        r = http_request(url, "GET", None, headers, 10, no_redirect=True)
        log(f"  GET {url}")
        log(f"      -> status={r['status']} loc={r['location']} err={r['error']} len={len(r['text'])}")
        if r["text"]:
            log(f"      正文前 200 字：{r['text'][:200]!r}")

    log("-" * 70)
    log("三、解析会话参数（若上面捕获到门户地址）")
    if pr["portal_url"]:
        pp = parse_portal_url(pr["portal_url"])
        for k, v in pp.items():
            log(f"    {k} = {v}")
    else:
        log("  没有捕获到门户跳转地址——如果此刻你确实上不了网，这是个重要线索。")
        log("  请把这份日志发给开发者。")

    log("-" * 70)
    log("四、收集会话参数（跟门户跳一次 + resolveRedirectInfo 兜底，全部只读）")
    cj = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    ses = collect_session(cfg, pr["portal_url"], verbose=True, opener=opener)
    if not ses.get("sessionId"):
        log("  [!] 依然没有 sessionId。")
        log("      拿它的唯一办法：在断网状态下用浏览器打开任意网页，等它跳到校园网页面，")
        log("      把地址栏那一整条 URL 复制下来，然后马上运行（里面的 sessionId 几分钟就过期）：")
        log('        py 校园网自动登录.py --测试 --门户地址 "<把那条 URL 粘到这里>"')

    log("-" * 70)
    log("诊断模式结束")
    return 0


def run_daemon(cfg: dict) -> int:
    """
    守护模式：常驻后台，周期性检查；一旦掉线就自动登录。
    用绑定本地端口的方式保证只有一个实例。
    """
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # 注意 Windows 和 Linux 的差别：SO_REUSEADDR 在 Linux 上表示"可以复用
    # 处于 TIME_WAIT 的地址"，在 Windows 上却是"允许别的进程抢占同一个地址"。
    # 用它做单实例锁在 Windows 上等于没锁——两个进程都能 bind 成功、都以为
    # 自己是唯一的。Windows 必须改用 SO_EXCLUSIVEADDRUSE 才能真正独占端口。
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        lock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        lock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        lock.bind(("127.0.0.1", _DAEMON_LOCK_PORT))
        lock.listen(1)
    except OSError:
        lock.close()
        log("已经有一个守护进程在运行了，本次退出。")
        return 0

    interval = max(5, int(cfg.get("daemon_interval_seconds", 60)))
    offline_interval = max(5, int(cfg.get("daemon_offline_interval_seconds", 20)))
    log(f"守护模式启动：在线时每 {interval} 秒查一次，"
        f"掉线时每 {offline_interval} 秒重试一次（按 Ctrl+C 可停止）。")

    fail_streak = 0
    while True:
        try:
            online = run_login_once(cfg)
            if online:
                if fail_streak:
                    log("网络已恢复。")
                fail_streak = 0
            else:
                fail_streak += 1
                # 掉线时缩短轮询间隔，尽快恢复
                log(f"当前未联网（连续第 {fail_streak} 次）。")
        except Exception as e:
            fail_streak += 1
            log(f"守护循环异常：{type(e).__name__}: {e}", "ERROR")

        sleep_s = offline_interval if fail_streak else interval
        time.sleep(sleep_s)


def main() -> int:
    global _LOG_TEE
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--诊断", "--diagnose", dest="diagnose", action="store_true")
    ap.add_argument("--测试", "--test", dest="test", action="store_true")
    ap.add_argument("--检查", "--check", dest="check", action="store_true")
    ap.add_argument("--安静", "--quiet", dest="quiet", action="store_true")
    ap.add_argument("--守护", "--daemon", dest="daemon", action="store_true")
    ap.add_argument(
        "--强制",
        "--force",
        dest="force",
        action="store_true",
        help="即使检测到已经在线，也照样完整走一遍登录（用来验证登录接口是否通）",
    )
    ap.add_argument(
        "--门户地址",
        "--url",
        dest="portal_url",
        default=None,
        help="手动指定交换机给你的门户跳转地址：断网时用浏览器打开任意网页，"
        "等它跳到校园网页面，把地址栏那一整条 URL 复制过来。"
        "注意这种地址里的 sessionId 通常几分钟就失效，复制后要马上运行。",
    )
    args = ap.parse_args()

    if args.quiet:
        _LOG_TEE = False

    cfg = load_config()
    if args.portal_url:
        cfg["portal_url"] = args.portal_url
    log_path = _setup_log()
    _cleanup_logs(cfg.get("log_keep_days", 30))

    log("=" * 70)
    log(f"校园网自动登录启动  (日志：{log_path})")

    if args.check:
        pr = probe(cfg, verbose=False)
        log(f"在线状态：{pr['online']}（{pr['reason']}）")
        return 0 if pr["online"] else 1

    if args.diagnose:
        rc = diagnose(cfg)
        log(f"退出码 {rc}")
        return rc

    if not validate_config(cfg):
        return 2

    if args.daemon:
        return run_daemon(cfg)

    delay = int(cfg.get("startup_delay_seconds", 20))
    if delay > 0 and not args.test:
        log(f"启动延迟 {delay} 秒，等待网络就绪 ……")
        time.sleep(delay)

    retries = 1 if args.test else max(1, int(cfg.get("max_retries", 3)))
    interval = max(1, int(cfg.get("retry_interval_seconds", 30)))

    for i in range(1, retries + 1):
        log(f"—— 第 {i}/{retries} 次尝试 ——")
        try:
            if run_login_once(cfg, force=args.force):
                log("结束：网络已可用。")
                return 0
        except Exception as e:
            log(f"本次尝试出现异常：{type(e).__name__}: {e}", "ERROR")
        if i < retries:
            log(f"{interval} 秒后重试 ……")
            time.sleep(interval)

    log("结束：多次尝试后仍未登录成功。", "ERROR")
    return 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
