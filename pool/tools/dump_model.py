import json
import sys

d = json.load(open("/tmp/modelinfo.json"))
want = sys.argv[1] if len(sys.argv) > 1 else "deepseek-v4.1-flash@a4s1"
ms = [m for m in d["data"] if m["model_name"] == want]
print("count:", len(ms))
if ms:
    p = dict(ms[0].get("litellm_params") or {})
    for k in list(p):
        if "key" in k.lower():
            p[k] = str(p[k])[-12:]
    print(json.dumps(p, indent=1, ensure_ascii=False)[:1500])
    print("model_info:", json.dumps(ms[0].get("model_info") or {}, ensure_ascii=False)[:400])
