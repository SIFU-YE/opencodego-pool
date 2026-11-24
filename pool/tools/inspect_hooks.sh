#!/bin/bash
# 摸清 litellm 的流式钩子与 header 透传能力（只读）
docker exec litellm python3 - <<'PY'
from litellm.integrations.custom_logger import CustomLogger
names = [m for m in dir(CustomLogger) if "stream" in m.lower()]
print("STREAM_HOOKS:", names)
import inspect
for n in names:
    fn = getattr(CustomLogger, n)
    try:
        print("---", n, inspect.signature(fn))
    except Exception as e:
        print("---", n, "sig?", e)
PY
echo "=== header 透传开关 ==="
docker exec litellm python3 - <<'PY'
import litellm
print("forward_client_headers_to_llm_api attr:", hasattr(litellm, "forward_client_headers_to_llm_api"))
print("value:", getattr(litellm, "forward_client_headers_to_llm_api", None))
PY
echo "=== 版本 ==="
docker exec litellm python3 -c "from importlib.metadata import version; print(version('litellm'))"
echo "=== 现网 pool_logger 与仓库副本是否一致 ==="
md5sum /opt/litellm/pool_logger.py
