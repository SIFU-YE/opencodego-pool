import base64, json, urllib.parse, secrets, os
raw = open('/opt/pool-egress/sub3.raw','rb').read().strip().decode()
raw += '=' * (-len(raw) % 4)
dec = base64.b64decode(raw).decode()
links = [l.strip() for l in dec.splitlines() if l.strip().startswith('vless://')]
def remark(l):
    return urllib.parse.unquote(l.split('#',1)[1]) if '#' in l else ''
jp = [l for l in links if remark(l).startswith('🇯🇵')]
tokyo = sorted([l for l in jp if '日本东京' in remark(l)], key=lambda l: remark(l))
aws = sorted([l for l in jp if l not in tokyo], key=lambda l: remark(l))
# order tokyo by number in remark, aws by number
import re
def num(l):
    m = re.search(r'(东京|日本)(\d+)', remark(l))
    return int(m.group(2)) if m else 99
tokyo = sorted(tokyo, key=num)
aws = sorted(aws, key=num)
assert len(tokyo)==9, len(tokyo)
assert len(aws)==10, len(aws)
ordered = [(f'jp-tokyo{i+1:02d}', l) for i,l in enumerate(tokyo)] + [(f'jp-aws{i+1:02d}', l) for i,l in enumerate(aws)]
outbounds = []
for tag, l in ordered:
    body = l.split('://',1)[1].split('#',1)[0]
    userhost, _, q = body.partition('?')
    creds, _, hostport = userhost.rpartition('@')
    uuid = creds
    host, _, port = hostport.partition(':')
    qs = urllib.parse.parse_qs(q)
    g = lambda k: (qs.get(k,[''])[0])
    net, sec, sni, fp = g('type'), g('security'), g('sni'), g('fp') or 'chrome'
    flow, pbk, sid = g('flow'), g('pbk'), g('sid')
    h, path = g('host'), g('path')
    ob = {'type':'vless','tag':tag,'server':host,'server_port':int(port),'uuid':uuid}
    # 2026-10-16：本机没有 IPv6 默认路由，而十个节点域名同时有 A/AAAA 记录。
    # sing-box 默认把解析结果原样交给 Go 拨号器：解析结果里一旦只剩 AAAA（A 查询抖动时
    # 解析器会返回部分结果），拨号就地 ENETUNREACH —— 当天 743 次，客户端侧表现为
    # litellm InternalServerError: Connection error -> 500，且 TCP 探测完全看不到。
    # 钉死 ipv4_only：只解析 A、只拨 IPv4（十个节点域名均已确认存在 A 记录）。
    ob['domain_strategy'] = 'ipv4_only'
    if flow: ob['flow'] = flow
    if sec == 'reality':
        ob['tls'] = {'enabled':True,'server_name':sni,'utls':{'enabled':True,'fingerprint':fp},
                     'reality':{'enabled':True,'public_key':pbk,'short_id':sid}}
    else:
        ob['tls'] = {'enabled':True,'server_name':sni,'utls':{'enabled':True,'fingerprint':fp}}
    if net == 'ws':
        t = {'type':'ws','path':path or '/'}
        if h: t['headers'] = {'Host': h}
        ob['transport'] = t
    outbounds.append(ob)
tags = [t for t,_ in ordered]
outbounds.append({'type':'selector','tag':'muse-egress','outbounds':tags,'default':'jp-tokyo06'})
outbounds.append({'type':'direct','tag':'direct'})
secret_path = '/opt/pool-egress/clash-secret'
clash_secret = secrets.token_hex(16)
open(secret_path,'w').write(clash_secret)
os.chmod(secret_path, 0o600)
# Docker 网桥监听地址由部署方用环境变量提供（不同宿主的网桥网关不同，仓库不写死内网地址）；
# 未设置时只监听 127.0.0.1，容器内流量需要宿主代理时请显式设置 POOL_DOCKER_BRIDGE。
docker_bridge = (os.environ.get('POOL_DOCKER_BRIDGE') or '').strip()
inbounds = [{'type':'mixed','tag':'pool-in','listen':'127.0.0.1','listen_port':10808}]
if docker_bridge:
    inbounds.append({'type':'mixed','tag':'pool-in-docker','listen':docker_bridge,'listen_port':10808})
else:
    print('NOTE: POOL_DOCKER_BRIDGE 未设置，sing-box 只监听 127.0.0.1')
cfg = {
 'log': {'level':'info','timestamp':True},
 'experimental': {'clash_api':{'external_controller':'127.0.0.1:9099','secret':clash_secret}},
 'inbounds': inbounds,
 'outbounds': outbounds,
 'route': {'rules':[{'domain_suffix':['opencode.ai'],'outbound':'muse-egress'}],'final':'direct'}
}
open('/opt/pool-egress/config.json','w').write(json.dumps(cfg, indent=1, ensure_ascii=False))
os.chmod('/opt/pool-egress/config.json', 0o600)
os.chmod(secret_path, 0o600)
print('OK tags=',len(tags),'default=jp-tokyo06')
