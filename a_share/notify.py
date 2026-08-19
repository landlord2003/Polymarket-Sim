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


def _load_dotenv():
    """Minimal .env loader (stdlib only). Injects KEY=VALUE pairs from the
    project-root .env into os.environ so callers can use os.getenv().
    Walks up from this file's dir to find .env; does not override existing
    process env vars. Comments (#) and blank lines are skipped."""
    d = os.path.dirname(os.path.abspath(__file__))
    env_path = None
    cur = d
    for _ in range(5):
        cand = os.path.join(cur, ".env")
        if os.path.isfile(cand):
            env_path = cand
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    if not env_path:
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


_load_dotenv()

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
        resp = _post_json(_dingtalk_signed_url(), body)
        print(f"[notify] 钉钉推送成功：errcode={resp.get('errcode')} errmsg={resp.get('errmsg')}")
        return resp
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
