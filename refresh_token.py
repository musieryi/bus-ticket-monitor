# -*- coding: utf-8 -*-
"""
Token 刷新模块
==============
登录链路（逆向自小程序，均已验证）：
1. sendCode  : 发送短信验证码（phone 明文，请求需签名，无需登录态）
2. login     : 验证码换 token（无需签名）

phone 字段加密：AES-ECB，key='k6yslkrdqm3v6moy'，PKCS7，Base64（已用抓包样本验证一致）
"""

import base64
import json
from datetime import datetime

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

import monitor
from monitor import make_sign

LOGIN_BASE = (
    "https://dzgj.bjbus.com:9663/prod-api/sharebus-passenger/general/passengerLogin"
)
PHONE_AES_KEY = b"k6yslkrdqm3v6moy"


def encrypt_phone(phone):
    """手机号加密为接口要求的密文（已验证与小程序输出一致）。"""
    cipher = AES.new(PHONE_AES_KEY, AES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(pad(phone.encode("utf-8"), 16))).decode()


def send_sms_code(cfg_api, key, phone):
    """发送短信验证码。失败抛异常。"""
    ts = datetime.now().strftime(cfg_api.get("timestamp_format", "%Y-%m-%d %H:%M:%S"))

    req = {
        "timestamp": ts,
        "version": "2.0.0",
        "clientType": "01",
        "merchantId": "10000001",
        "data": {"phone": phone},
        "phone": "",
        "userId": "",
        "token": "",
    }

    req["sign"] = make_sign(req, key)

    r = requests.post(
        f"{LOGIN_BASE}/sendCode",
        json=req,
        headers=cfg_api.get("headers") or {},
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")

    resp = r.json()
    if resp.get("status") != "0000" and resp.get("code") not in ("0000", 200):
        raise RuntimeError(f"发送验证码失败: {json.dumps(resp, ensure_ascii=False)[:200]}")


def login_with_code(cfg_api, phone, code):
    """用短信验证码登录，返回 {userId, token}。失败抛异常。"""
    body = {"data": {"code": code, "phone": phone, "clientType": "01"}}

    r = requests.post(
        f"{LOGIN_BASE}/login",
        json=body,
        headers=cfg_api.get("headers") or {},
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")

    resp = r.json()
    if resp.get("status") != "0000" and resp.get("code") not in ("0000", 200):
        raise RuntimeError(f"登录失败: {json.dumps(resp, ensure_ascii=False)[:200]}")

    data = resp.get("data") or {}
    if not data.get("token") or not data.get("userId"):
        raise RuntimeError(f"响应缺少 token/userId: {json.dumps(resp, ensure_ascii=False)[:200]}")
    return {"userId": data["userId"], "token": data["token"]}


def apply_new_auth(config_path, phone_plain, user_id, token):
    """把新登录态写回 config.json（fixed_fields 更新 phone/userId/token）。返回新 cfg。"""
    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)

    ff = cfg.setdefault("api", {}).setdefault("fixed_fields", {})
    ff["phone"] = encrypt_phone(phone_plain)
    ff["userId"] = user_id
    ff["token"] = token

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg
