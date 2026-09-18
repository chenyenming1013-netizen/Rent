"""LINE Messaging API 的最小封裝（只用標準函式庫，不需額外安裝套件）。

PythonAnywhere 免費帳號會透過代理連線，urllib 會自動讀取 https_proxy 環境變數。
"""
import base64
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request

API = "https://api.line.me/v2/bot"


def secret():
    return os.environ.get("LINE_CHANNEL_SECRET", "")


def token():
    return os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")


def enabled():
    return bool(secret() and token())


def verify_signature(body: bytes, signature: str) -> bool:
    if not secret() or not signature:
        return False
    digest = hmac.new(secret().encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


def _post(path, payload):
    """回傳 (成功與否, 說明)。"""
    if not token():
        return False, "尚未設定 LINE_CHANNEL_ACCESS_TOKEN"
    req = urllib.request.Request(
        API + path, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token()}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300, str(resp.status)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        return False, f"HTTP {e.code} {detail}"
    except Exception as e:  # 網路或代理錯誤
        return False, f"{type(e).__name__}: {e}"[:300]


def _messages(texts):
    if isinstance(texts, str):
        texts = [texts]
    return [{"type": "text", "text": t[:4900]} for t in texts][:5]


def group_summary(gid):
    """取得群組名稱；失敗時回傳空字串。"""
    if not token():
        return ""
    req = urllib.request.Request(f"{API}/group/{gid}/summary",
                                 headers={"Authorization": f"Bearer {token()}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode()).get("groupName", "")
    except Exception:
        return ""


def profile(uid):
    """取得使用者的 LINE 顯示名稱；失敗時回傳空字串。"""
    if not token():
        return ""
    req = urllib.request.Request(f"{API}/profile/{uid}",
                                 headers={"Authorization": f"Bearer {token()}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode()).get("displayName", "")
    except Exception:
        return ""


def push(to, texts):
    return _post("/message/push", {"to": to, "messages": _messages(texts)})


def reply(reply_token, texts):
    return _post("/message/reply", {"replyToken": reply_token, "messages": _messages(texts)})
