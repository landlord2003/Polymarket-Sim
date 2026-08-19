"""钉钉 / 企业微信 推送（仅标准库，无需额外依赖）

配置（环境变量，切勿提交到仓库）：
  DINGTALK_WEBHOOK  - 钉钉机器人 Webhook 完整 URL（含 access_token）
  DINGTALK_SECRET   - 钉钉机器人加签密钥 SECxxx
  WECOM_WEBHOOK     - （可选）企业微信机器人 Webhook
未配置时 send_* 返回 None 并打印提示，不报错；便于离线验证。
"""

import os
import json
import hmac
import hashlib
import base64
import time
import urllib.parse
import urllib.request

WEBHOOK = os.getenv("DINGTALK_WEBHOOK")
SECRET = os.getenv("DINGTALK_SECRET")
WECOM_WEBHOOK = os.getenv("WECOM_WEBHOOK")
_TIMEOUT = 10


def _dingtalk_signed_url() -> str:
    ts = str(round(time.time() * 1000))
    raw = f"{ts}\n{SECRET}"
    sig = base64.b64encode(
        hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).digest()
    ).decode()
    return f"{WEBHOOK}&timestamp={ts}&sign={urllib.parse.quote(sig)}"


def _post_json(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_markdown(title: str, text: str) -> dict | None:
    if not WEBHOOK or not SECRET:
        print("[notify] 未配置 DINGTALK_WEBHOOK/SECRET，跳过钉钉推送")
        return None
    body = {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
    try:
        return _post_json(_dingtalk_signed_url(), body)
    except Exception as e:
        print(f"[notify] 钉钉推送失败: {e}")
        return None


def send_wecom(text: str) -> dict | None:
    if not WECOM_WEBHOOK:
        return None
    body = {"msgtype": "markdown", "markdown": {"content": text}}
    try:
        return _post_json(WECOM_WEBHOOK, body)
    except Exception as e:
        print(f"[notify] 企业微信推送失败: {e}")
        return None
