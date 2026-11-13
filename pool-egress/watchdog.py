import json, socket, subprocess, time, datetime, concurrent.futures, urllib.request, urllib.parse, os

CFG = os.environ.get('POOL_EGRESS_CFG', '/opt/pool-egress/config.json')
SECRET_FILE = '/opt/pool-egress/clash-secret'
LOG = '/opt/pool-egress/watchdog.log'
API = 'http://127.0.0.1:9099'
PROBE_URL = 'https://opencode.ai/zen/go/v1/models'
PROXY = 'http://127.0.0.1:10808'
INTERVAL = 5
CURL_TIMEOUT = 4
TCP_TIMEOUT = 2.0
SLOW_MS = 200
COOLDOWN = 30
HEARTBEAT = 3600
FAIL_PENALTY = 3600  # node that just failed liveness is excluded from candidacy for 1h
# 2026-10-16 事故：TCP 探测只看「本机 → 节点」的握手，节点侧全链路（DNS/出网/TLS）
# 坏掉时它照样 59ms 通过，于是坏节点长期占着 current，客户端只能吃 500。
# 补救：稳态每 CHAINCHECK_INTERVAL 秒对 current 做一次全链路探测；失败即换节点。
CHAINCHECK_INTERVAL = 30   # 秒；current 节点的全链路体检周期
CHAIN_PENALTY = 300        # 秒；全链路刚失败的节点，5 分钟内不再被选回来
# r4: periodic full-chain sweep feeds quarantine, decoupled from trigger luck
QUARANTINE_FILE = '/opt/pool-egress/quarantine.json'
SWEEP_INTERVAL = 120
SWEEP_TIMEOUT_MS = 3000
QUARANTINE_TTL = 1800  # 30min
QFAIL_N = 2  # consecutive delay fails -> quarantine
QPASS_N = 2  # consecutive delay passes -> early release (hysteresis vs fluke-pass)

def log(msg):
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(LOG, 'a') as f:
        f.write(f'{ts} {msg}\n')

def load_nodes():
    c = json.load(open(CFG))
    return [(o['tag'], o['server'], o['server_port']) for o in c['outbounds'] if o.get('type') == 'vless']

def secret():
    return open(SECRET_FILE).read().strip()

def api_get_now():
    req = urllib.request.Request(f'{API}/proxies/muse-egress', headers={'Authorization': 'Bearer ' + secret()})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r).get('now')

def api_switch(tag):
    data = json.dumps({'name': tag}).encode()
    req = urllib.request.Request(f'{API}/proxies/muse-egress', data=data, method='PUT',
                                 headers={'Authorization': 'Bearer ' + secret(), 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=5) as r:
        r.read()
    return api_get_now()

def load_quarantine():
    # reuse on-disk format {quarantined:{tag:expiry_epoch}, fails:{tag:count}}; restart recovery
    try:
        q = json.load(open(QUARANTINE_FILE))
        now = time.time()
        quar = {t: e for t, e in q.get('quarantined', {}).items() if e > now}
        fails = {t: int(v) for t, v in q.get('fails', {}).items()}
        return quar, fails
    except Exception:
        return {}, {}

def save_quarantine(quar, fails, passes, ever_ok):
    tmp = QUARANTINE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({'quarantined': quar, 'fails': fails, 'passes': passes,
                   'ever_ok': sorted(ever_ok)}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, QUARANTINE_FILE)

def prune_quarantine(quar):
    now = time.time()
    return {t: e for t, e in quar.items() if e > now}

DELAY_URL = 'https://opencode.ai/zen/go/v1/models'
DELAY_TIMEOUT_MS = 3000

def delay_probe(tag):
    # full-chain check through candidate node; ms float or None
    try:
        q = urllib.parse.quote(DELAY_URL, safe='')
        url = f'{API}/proxies/{tag}/delay?url={q}&timeout={DELAY_TIMEOUT_MS}'
        req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + secret()})
        with urllib.request.urlopen(req, timeout=DELAY_TIMEOUT_MS / 1000 + 3) as r:
            body = json.load(r)
        d = body.get('delay')
        return float(d) if d is not None else None
    except Exception:
        return None

def delay_scan(tags):
    # concurrent full-chain sweep; 19 nodes finish in ~seconds, well under 60s
    res = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(tags))) as ex:
        fut = {ex.submit(delay_probe, t): t for t in tags}
        for f in concurrent.futures.as_completed(fut):
            res[fut[f]] = f.result()
    return res

def fmt_delay(res):
    return ' '.join(f'{t}={"fail" if v is None else f"{v:.0f}ms"}' for t, v in sorted(res.items()))

def tcp_probe(host, port):
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=TCP_TIMEOUT):
            return (time.monotonic() - t0) * 1000
    except Exception:
        return None

def full_scan(nodes):
    res = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(nodes)) as ex:
        fut = {ex.submit(tcp_probe, h, p): t for t, h, p in nodes}
        for f in concurrent.futures.as_completed(fut):
            res[fut[f]] = f.result()
    return res

def fmt_scan(res):
    return ' '.join(f'{t}={"timeout" if v is None else f"{v:.0f}ms"}' for t, v in sorted(res.items()))

def run_sweep(tags, quar, fails, passes, ever_ok):
    # periodic full-chain sweep: feeds quarantine + ever_ok, decoupled from trigger
    dres = delay_scan(tags)
    for t, dv in dres.items():
        if dv is None:
            fails[t] = fails.get(t, 0) + 1
            passes[t] = 0
            if fails[t] >= QFAIL_N and t not in quar:
                quar[t] = time.time() + QUARANTINE_TTL
                log(f'quarantine {t} delay-fails={fails[t]} 30min')
        else:
            passes[t] = passes.get(t, 0) + 1
            fails[t] = 0
            ever_ok.add(t)
            if t in quar and passes.get(t, 0) >= QPASS_N:
                del quar[t]
                log(f'unquarantine {t} delay-passes={passes[t]}')
    save_quarantine(quar, fails, passes, ever_ok)
    qlist = ','.join(sorted(quar)) if quar else '-'
    log(f'periodic-sweep: {fmt_delay(dres)} quarantined={qlist}')
    return dres

def main():
    nodes = load_nodes()
    nodemap = {t: (h, p) for t, h, p in nodes}
    tags = [t for t, _, _ in nodes]
    quar, fails = load_quarantine()
    passes = {}
    ever_ok = set()
    log(f'watchdog start nodes={len(nodes)} interval={INTERVAL}s threshold={SLOW_MS}ms sweep={SWEEP_INTERVAL}s')
    res = full_scan(nodes)
    log(f'full-scan startup: {fmt_scan(res)}')
    run_sweep(tags, quar, fails, passes, ever_ok)  # immediate first sweep populates ever_ok/quarantine
    last_sweep = time.monotonic()
    last_switch = 0.0
    last_beat = time.monotonic()
    last_chaincheck = 0.0
    penalized = {}  # tag -> monotonic expiry (failure penalty)
    while True:
        try:
            cur = api_get_now()
        except Exception:
            cur = None
        now_m = time.monotonic()
        if now_m - last_sweep >= SWEEP_INTERVAL:
            quar = prune_quarantine(quar)
            run_sweep(tags, quar, fails, passes, ever_ok)
            last_sweep = time.monotonic()
            now_m = last_sweep
        penalized = {t: e for t, e in penalized.items() if e > now_m}
        # 5s steady state: TCP probe of current path entry only (>200ms/fail triggers).
        # TCP 通过不代表节点可用——每 CHAINCHECK_INTERVAL 秒补一次 current 的全链路探测。
        if cur is None or cur not in nodemap:
            ok, ms = False, -1.0
        else:
            h, p = nodemap[cur]
            ms = tcp_probe(h, p)
            ok = ms is not None
            ms = ms if ms is not None else -1.0
        chain_fail = False
        if ok and ms <= SLOW_MS and now_m - last_chaincheck >= CHAINCHECK_INTERVAL:
            last_chaincheck = now_m
            chain_fail = delay_probe(cur) is None
        if ok and ms <= SLOW_MS and not chain_fail:
            if now_m - last_beat >= HEARTBEAT:
                log(f'heartbeat now={cur} tcp={ms:.0f}ms')
                last_beat = now_m
        else:
            if chain_fail:
                reason, ms = 'chain-fail', -1.0
            else:
                reason = 'fail' if not ok else 'slow'
            if now_m - last_switch < COOLDOWN:
                log(f'{reason} tcp={ms:.0f}ms cur={cur} cooldown-skip')
            else:
                quar = prune_quarantine(quar)
                res = full_scan(nodes)
                qlist = ','.join(sorted(quar)) if quar else '-'
                log(f'full-scan trigger-{reason}={ms:.0f}ms cur={cur}: {fmt_scan(res)} quarantined={qlist}')
                cands = sorted(((t, res[t]) for t in tags
                                if t not in quar and t in ever_ok and res.get(t) is not None
                                and t != cur and t not in penalized),
                               key=lambda x: x[1])
                if not cands:
                    log('no-available-node all-timeout-or-quarantined-or-penalized keep-current')
                else:
                    tag, lat = cands[0]
                    try:
                        new = api_switch(tag)
                        if new == tag:
                            log(f'switch {cur} -> {tag} tcp={lat:.0f}ms confirmed')
                            last_switch = time.monotonic()
                            last_beat = last_switch
                            last_chaincheck = last_switch
                            if not ok:
                                penalized[cur] = last_switch + FAIL_PENALTY
                            elif chain_fail:
                                penalized[cur] = last_switch + CHAIN_PENALTY
                        else:
                            log(f'switch {cur} -> {tag} VERIFY-MISMATCH now={new}')
                    except Exception:
                        log(f'switch {cur} -> {tag} FAILED')
        time.sleep(INTERVAL)

if __name__ == '__main__':
    main()
