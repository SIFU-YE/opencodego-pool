"""LiteLLM custom callback: 记录每条成功请求使用了哪个号池账号 (OCG_KEY_n) 及 token 用量。
写入容器内 /app/pool-requests.jsonl，由号池看板 (dashboard.py) 经 docker exec 读取。
失败静默——绝不影响主请求流程。
⚠️ 升级/重建 litellm 容器后需重新 docker cp 本文件（docker restart 不受影响）。"""
import json
import os

try:
    from litellm.integrations.custom_logger import CustomLogger

    _HAVE_BASE = True
except Exception:  # pragma: no cover
    _HAVE_BASE = False

BASE = CustomLogger if _HAVE_BASE else object
LOG_PATH = "/app/pool-requests.jsonl"


def _append_log(line):
    """追加一行日志；文件以 0600 创建（不依赖进程 umask）。

    · 日志**不含完整密钥**：keyRef 只留末 24 位（看板按尾缀映射，功能不受影响）；
    · 但日志仍含会话 id / 用量 / 账号尾号等运营数据，不该被同机其他用户读到，
      所以显式 0600 创建。
    """
    fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        # 旧版本创建的文件可能是 644：每次写入顺手收紧（fchmod 在 Linux 上有效）
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            pass
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _usage_of(kwargs, response_obj):
    """防御式提取 token 用量（流式/非流式/异常响应都不得抛错）"""
    pt = ct = tt = 0
    try:
        u = (response_obj.get("usage") if isinstance(response_obj, dict) else getattr(response_obj, "usage", None)) or {}
        if isinstance(u, dict):
            pt = int(u.get("prompt_tokens") or 0)
            ct = int(u.get("completion_tokens") or 0)
            tt = int(u.get("total_tokens") or (pt + ct))
        else:
            pt = int(getattr(u, "prompt_tokens", 0) or 0)
            ct = int(getattr(u, "completion_tokens", 0) or 0)
            tt = int(getattr(u, "total_tokens", 0) or (pt + ct))
    except Exception:
        pass
    if not tt:
        try:
            cu = kwargs.get("complete_streaming_usage") or {}
            pt = int(cu.get("prompt_tokens") or 0)
            ct = int(cu.get("completion_tokens") or 0)
            tt = int(cu.get("total_tokens") or (pt + ct))
        except Exception:
            pass
    return pt, ct, tt


class PoolLogger(BASE):
    def _record(self, kwargs, start_time, end_time, response_obj=None):
        try:
            lp = kwargs.get("litellm_params") or {}
            key = lp.get("api_key") or ""
            pt, ct, tt = _usage_of(kwargs, response_obj)
            rec = {
                "ts": int(end_time.timestamp() * 1000) if end_time else 0,
                "t0": int(start_time.timestamp() * 1000) if start_time else 0,
                "model": kwargs.get("model", ""),
                "key": str(key)[-20:],
                "keyRef": (str(key)[-24:] if key else ""),   # 只留末 24 位：看板按尾缀映射，不落完整密钥
                "pt": pt, "ct": ct, "tt": tt,
            }
            _append_log(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._record(kwargs, start_time, end_time, response_obj)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._record(kwargs, start_time, end_time, response_obj)


pool_logger = PoolLogger()
