#!/usr/bin/env python3
"""Build the tenkm.org demo: a read-only snapshot of a 10km loop, shown in mtv.

    python3 build.py LOOP_ROOT [--mtv PATH] [--out DIR] [--name NAME]

LOOP_ROOT  a live loop or a copy of one (the folder holding .10km/).
--mtv      an mtv checkout to take the page and data layer from (default: the
           installed mtv package).
--out      the site folder to write demo/ into (default: this script's folder).
--name     loop name shown in the demo (default: LOOP_ROOT's folder name).

It never writes to the loop. The database is copied with SQLite's backup API,
so a running loop is captured consistently. The output is static files:

    demo/index.html     mtv's page, with a shim that answers its API from the snapshot
    demo/snapshot.js    every API response the page can ask for, precomputed
    demo/vendor/        mtv's bundled chart library and its licence

snapshot.js is a script, not JSON, so the demo also works opened from disk.
Standard library only.
"""
import argparse
import calendar
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_TAIL = 200_000


def load_mtv(repo):
    """Import mtv (and mtv.data) from a checkout, or the installed package."""
    if repo:
        pkg = os.path.join(os.path.abspath(repo), 'mtv')
        spec = importlib.util.spec_from_file_location('mtv', os.path.join(pkg, '__init__.py'),
                                                      submodule_search_locations=[pkg])
        mod = importlib.util.module_from_spec(spec)
        sys.modules['mtv'] = mod
        spec.loader.exec_module(mod)
    import mtv
    import mtv.data
    return mtv, mtv.data, os.path.join(os.path.dirname(mtv.__file__), 'static')


def copy_db(src, dst):
    """Consistent copy of a (possibly live, WAL-mode) loop.db."""
    try:
        s = sqlite3.connect(f'file:{src}?mode=ro', uri=True, timeout=10)
        d = sqlite3.connect(dst)
        s.backup(d)
        s.close()
    except sqlite3.OperationalError:
        # Can't open read-only in place (e.g. no write access for the -shm file): copy the files.
        for ext in ('', '-wal', '-shm'):
            if os.path.exists(src + ext):
                shutil.copyfile(src + ext, dst + ext)
        d = sqlite3.connect(dst)
    d.execute('pragma journal_mode=delete')
    d.close()


def stage(root, tmp):
    """Copy what mtv reads into a scratch loop root."""
    dot, sdot = os.path.join(root, '.10km'), os.path.join(tmp, '.10km')
    os.makedirs(sdot)
    copy_db(os.path.join(dot, 'loop.db'), os.path.join(sdot, 'loop.db'))
    for f in ('usage.jsonl', 'dispatch.json'):
        if os.path.exists(os.path.join(dot, f)):
            shutil.copyfile(os.path.join(dot, f), os.path.join(sdot, f))
    os.makedirs(os.path.join(tmp, 'central', 'loop'))
    for n in ('GOALS', 'PLAN', 'LEARNINGS'):
        p = os.path.join(root, 'central', 'loop', n + '.md')
        if os.path.exists(p):
            shutil.copyfile(p, os.path.join(tmp, 'central', 'loop', n + '.md'))
    logs = []
    for n in sorted(os.listdir(root)):
        p = os.path.join(root, n, 'agent.log')
        if os.path.isfile(p):
            os.makedirs(os.path.join(tmp, n))
            with open(p, 'rb') as f:
                f.seek(max(0, os.path.getsize(p) - LOG_TAIL))
                data = f.read()
            with open(os.path.join(tmp, n, 'agent.log'), 'wb') as f:
                f.write(data)
            logs.append(n)
    return logs


def repos(root):
    """Branch and commit of each repo in central/, read from .git without running git."""
    out = []
    c = os.path.join(root, 'central')
    for n in sorted(os.listdir(c)) if os.path.isdir(c) else []:
        g = os.path.join(c, n, '.git')
        if os.path.isfile(g):                       # worktree: "gitdir: <path>"
            with open(g) as f:
                g = f.read().split(':', 1)[1].strip()
        if not os.path.isdir(g):
            continue
        try:
            with open(os.path.join(g, 'HEAD')) as f:
                head = f.read().strip()
        except OSError:
            continue
        branch, sha = '', ''
        if head.startswith('ref: '):
            ref = head[5:]
            branch = ref.replace('refs/heads/', '')
            for base in (g, os.path.join(g, '..', '..')):        # worktree refs live in the main repo
                p = os.path.join(base, ref)
                if os.path.exists(p):
                    with open(p) as f:
                        sha = f.read().strip()
                    break
            else:
                pr = os.path.join(g, 'packed-refs')
                if os.path.exists(pr):
                    with open(pr) as f:
                        for line in f:
                            if line.strip().endswith(' ' + ref):
                                sha = line.split()[0]
        else:
            sha = head
        out.append({'name': n, 'branch': branch, 'commit': sha[:7], 'when': None, 'message': '',
                    'dirty': 0, 'ahead': 0, 'behind': 0})
    return out


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))


def epoch(s):
    try:
        return calendar.timegm(time.strptime(str(s)[:19], '%Y-%m-%dT%H:%M:%S'))
    except (ValueError, TypeError):
        return None


def stopped_at(L):
    """When the loop last did anything: the latest timestamp in loop.db and usage.jsonl."""
    q = ["select updated_at t from loop", "select max(planned_at) t from cycles", "select max(curated_at) t from cycles",
         "select max(coalesce(curated_at, done_at, started_at, created_at)) t from tasks",
         "select max(coalesce(resolved_at, answered_at, created_at)) t from issues",
         "select max(created_at) t from audits", "select max(updated_at) t from sessions"]
    ts = [epoch((L.q1(s) or {}).get('t')) for s in q]
    ts += [r['t'] for r in L.usage.rows if r.get('t')]
    ts = [t for t in ts if t]
    return max(ts) if ts else time.time()


def snapshot(data, tmp, root, name, logs, version, at=None):
    L = data.Loop(tmp, name, name)
    L.repos = lambda max_age=30: repos(root)
    L.usage.refresh()
    at = at or stopped_at(L)
    # The demo's clock is the moment the loop stopped, not the build time: freeze mtv's "now" there too.
    real_time = data.time.time
    data.time.time = lambda: at
    try:
        return _snapshot(L, logs, version, at)
    finally:
        data.time.time = real_time


def _snapshot(L, logs, version, at):
    live = L.live()
    for s in live['sessions']:          # process checks mean nothing in a snapshot
        if s['status'] == 'active':
            s['alive'] = True
    d = live.get('dispatch') or {}
    for k in ('started_at', 'finished_at'):     # a dispatcher record newer than the snapshot (e.g. a trimmed copy)
        if epoch(d.get(k)) and epoch(d[k]) > at:
            d[k] = iso(at)
    live['actions'] = {'enabled': False, 'reason': 'Demo: a read-only snapshot. Actions are off.'}
    live['loops'] = [{'id': live['id'], 'name': live['name']}]
    live['version'] = version
    names = {r['session'] for r in L.usage.rows if r['session']} | {r['name'] for r in L.q('select name from sessions')}
    api = {
        'snapshot_at': at, 'mtv': version, 'live': live, 'docs': L.docs(), 'audits': L.audits_list(),
        'metrics': {b: L.metrics({'by': b}) for b in ('cycle', 'hour', 'day', 'month')},
        'timeline': L.timeline({}),
        'cycles': L.cycles_page({'limit': '100000'}),
        'issues': L.issues_page({'limit': '100000', 'type': 'all', 'status': 'all'}),
        'cycle': {str(r['id']): L.cycle(r['id']) for r in L.q('select id from cycles')},
        'task': {r['id']: L.task(r['id']) for r in L.q('select id from tasks')},
        'issue': {r['id']: L.issue(r['id']) for r in L.q('select id from issues')},
        'audit': {str(r['id']): L.audit(r['id']) for r in L.q('select id from audits')},
        'session': {n: L.session(n) for n in names},
        'log': {n: L.agent_log(n) for n in logs},
        'status': {'code': 0, 'out': "(snapshot) 10km status isn't available in the demo.", 'err': ''},
    }
    return api


def scrub(text, root, tmp):
    """Hide local paths: the loop root becomes ~/..., the home directory ~."""
    home = os.path.expanduser('~')
    shown = '~/' + os.path.relpath(root, home) if root.startswith(home + os.sep) else os.path.basename(root)
    for p, r in ((os.path.realpath(tmp), shown), (tmp, shown), (os.path.realpath(root), shown), (root, shown), (home, '~')):
        text = text.replace(json.dumps(p)[1:-1], r)
    return text


def check(text):
    """Warn about anything that looks like a secret or a local path."""
    pats = {
        'AWS key': r'AKIA[0-9A-Z]{16}', 'GitHub token': r'gh[pousr]_[A-Za-z0-9]{20,}', 'API key': r'\bsk-[A-Za-z0-9_-]{20,}',
        'bearer token': r'Bearer\s+[A-Za-z0-9._-]{20,}', 'assignment': r'(?i)(?:password|secret|token|api[_-]?key)["\']?\s*[=:]\s*["\']?[A-Za-z0-9/+_-]{12,}',
        'home path': r'/(?:Users|home)/[A-Za-z0-9._-]+',
    }
    found = {k: sorted(set(re.findall(p, text)))[:5] for k, p in pats.items()}
    return {k: v for k, v in found.items() if v}


SHIM = r"""<script src="snapshot.js"></script>
<script>
/* Demo: answers mtv's API from window.__MTV_SNAPSHOT. Read-only. */
(()=>{const real=window.fetch.bind(window);
 const J=(o,st)=>new Response(JSON.stringify(o),{status:st||(o==null?404:200),headers:{'Content-Type':'application/json',ETag:'"snap"'}});
 const page=(R,q)=>{const off=+q.get('offset')||0,lim=+q.get('limit')||50;return {total:R.length,offset:off,rows:R.slice(off,off+lim)}};
 window.fetch=async(u,o)=>{const url=new URL(u,location.href);if(!url.pathname.includes('/api/'))return real(u,o);const S=window.__MTV_SNAPSHOT;
  if(!S)return J({error:'Demo snapshot (snapshot.js) not loaded.'},500);
  const parts=url.pathname.split('/api/').pop().split('/'),q=url.searchParams;
  if(o&&o.method==='POST')return J({error:'This is a read-only demo: actions are off.'},403);
  if(parts[0]==='loops')return J({version:S.mtv,loops:[{id:S.live.id,name:S.live.name,escalations:S.live.escalations.filter(x=>x.assignee_role==='operator').length}],actions:S.live.actions});
  const k=parts[1];
  if(k==='live'){if(o&&o.headers&&o.headers['If-None-Match']==='"snap"')return new Response(null,{status:304,headers:{ETag:'"snap"'}});return J(S.live)}
  if(k==='metrics')return J(S.metrics[q.get('by')]||S.metrics.cycle);
  if(k==='timeline'){const fr=+q.get('from')||0,to=+q.get('to')||1e12,t=Object.assign({},S.timeline);t.rows=S.timeline.rows.filter(r=>r.e>=fr&&r.s<=to);t.total=t.rows.length;return J(t)}
  if(k==='cycles'){const s=(q.get('q')||'').toLowerCase();return J(page(S.cycles.rows.filter(c=>!s||(c.headline||'').toLowerCase().includes(s)),q))}
  if(k==='issues'){const s=(q.get('q')||'').toLowerCase(),ty=q.get('type'),st=q.get('status'),to=q.get('assignee_role'),fr=q.get('assigner_role');
   return J(page(S.issues.rows.filter(i=>(!ty||ty==='all'||i.type===ty)&&(!st||st==='all'||i.status===st)&&(!to||i.assignee_role===to)&&(!fr||i.assigner_role===fr)&&(!s||(i.title||'').toLowerCase().includes(s)||((S.issue[i.id]||{}).description||'').toLowerCase().includes(s))),q))}
  if(k==='audits'||k==='docs')return J(S[k]);
  if(k==='cycle'||k==='audit'||k==='task'||k==='issue')return J(S[k][q.get('id')]||null);
  if(k==='session')return J(S.session[q.get('name')]||null);
  if(k==='log')return J(S.log[q.get('session')]||{exists:false,events:[]});
  if(k==='status')return J(S.status);
  return J(null)}})();
</script>
"""

CLOCK = """<script>/* Demo: the clock stands still at the moment the loop stopped, so ages, durations and the timeline read as they did then. */
(()=>{const S=window.__MTV_SNAPSHOT;if(!S)return;const T=S.snapshot_at*1000,R=Date;
 const F=function(...a){return a.length?new R(...a):new R(T)};F.now=()=>T;
 /* ECharts times its animations with the clock, so with the clock stopped the charts never grow: draw them without animation. */
 if(window.echarts){const init=echarts.init;echarts.init=function(...a){const c=init.apply(this,a),so=c.setOption;c.setOption=function(o,...r){return so.call(this,o&&typeof o==='object'?Object.assign({},o,{animation:false}):o,...r)};return c}}F.UTC=R.UTC;F.parse=R.parse;F.prototype=R.prototype;window.Date=F})();</script>
"""

BANNER = """<div id="demo-banner"><span>mtv demo · a read-only snapshot of the <b id="snapn"></b> loop, taken <span id="snapt"></span> · <a href="https://github.com/pingdynasty/mtv" target="_blank" rel="noopener">mtv on GitHub</a></span><a id="demo-close" href="../" title="Close the demo and return to tenkm.org" aria-label="Close the demo and return to tenkm.org"><svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 3l10 10M13 3L3 13" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg><span>Close</span></a></div>
<style>#demo-banner{position:sticky;top:0;z-index:50;display:flex;align-items:center;justify-content:center;min-height:32px;font:13px var(--page-sans);background:var(--page-lime);color:var(--page-on,#fff);padding:6px 104px 6px 14px;text-align:center}#demo-banner a{color:inherit;text-decoration:underline}#demo-banner b{font-weight:600}#demo-close{position:absolute;right:8px;top:50%;transform:translateY(-50%);display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border:1px solid currentColor;border-radius:4px;font-weight:600;text-decoration:none!important;white-space:nowrap}#demo-close:hover,#demo-close:focus-visible{background:rgba(255,255,255,.2)}@media(max-width:600px){#demo-banner{padding-right:48px}#demo-close span{display:none}#demo-close{padding:5px 7px}}</style>
<script>addEventListener("keydown",e=>{if(e.key==="Escape"&&!document.querySelector("dialog[open],.modal:not([hidden])")&&!/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName))location.href="../"})</script>
<script>(()=>{const S=window.__MTV_SNAPSHOT;if(!S)return;document.getElementById('snapn').textContent=S.live.name;
 document.getElementById('snapt').textContent=new Date(S.snapshot_at*1000).toLocaleString([],{day:'numeric',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit',hourCycle:'h23'})})()</script>
"""


def page(static):
    with open(os.path.join(static, 'index.html'), encoding='utf-8') as f:
        h = f.read()
    tag = '<script src="/static/vendor/echarts.min.js"></script>'
    if tag not in h or '<body>' not in h or '<title>' not in h:
        sys.exit("build: mtv's index.html doesn't have the expected script tag, <title> or <body>; update build.py")
    # snapshot.js (in SHIM) must load before CLOCK, which reads the snapshot time.
    h = h.replace(tag, '<script src="vendor/echarts.min.js"></script>\n' + SHIM + CLOCK, 1)
    h = re.sub(r'<title>[^<]*</title>', '<title>mtv demo · tenkm.org</title>\n<meta name="description" content="A read-only snapshot of a 10km agent loop in mtv, the monkey televisor.">', h, count=1)
    h = h.replace('<body>', '<body>' + BANNER, 1)
    return h


def main(argv=None):
    ap = argparse.ArgumentParser(description='Build the tenkm.org demo from a 10km loop.')
    ap.add_argument('root', metavar='LOOP_ROOT')
    ap.add_argument('--mtv', help='mtv checkout (default: installed mtv)')
    ap.add_argument('--out', default=HERE, help='site folder (default: this folder)')
    ap.add_argument('--name', help='loop name shown in the demo')
    ap.add_argument('--at', help='freeze the demo clock at this UTC time, e.g. 2026-11-20T09:24:37Z (default: the loop\'s last activity)')
    ap.add_argument('--force', action='store_true', help='write even if the secrets check finds something')
    ns = ap.parse_args(argv)
    root = os.path.abspath(os.path.expanduser(ns.root))
    if not os.path.exists(os.path.join(root, '.10km', 'loop.db')):
        sys.exit(f'build: no loop database at {root}/.10km/loop.db')
    name = ns.name or os.path.basename(root)
    mtv, data, static = load_mtv(ns.mtv)
    tmp = tempfile.mkdtemp(prefix='tenkm-demo-')
    try:
        logs = stage(root, tmp)
        api = snapshot(data, tmp, root, name, logs, mtv.__version__, epoch(ns.at) if ns.at else None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    text = scrub(json.dumps(api, default=str, separators=(',', ':')), root, tmp)
    found = check(text)
    if found:
        print('build: the snapshot contains things that look private:')
        for k, v in found.items():
            print(f'  {k}: {", ".join(v)}')
        if not ns.force:
            sys.exit('build: nothing written; fix the loop data or pass --force')
    demo = os.path.join(os.path.abspath(ns.out), 'demo')
    os.makedirs(os.path.join(demo, 'vendor'), exist_ok=True)
    with open(os.path.join(demo, 'snapshot.js'), 'w', encoding='utf-8') as f:
        f.write('window.__MTV_SNAPSHOT=' + text.replace('</', '<\\/') + ';\n')
    with open(os.path.join(demo, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(page(static))
    for fn in os.listdir(os.path.join(static, 'vendor')):
        shutil.copyfile(os.path.join(static, 'vendor', fn), os.path.join(demo, 'vendor', fn))
    old = os.path.join(demo, 'snapshot.json')
    if os.path.exists(old):
        os.remove(old)
    c = api['live']['counts']
    print(f'build: {name}: {c["cycles"]} cycles, {c["tasks"]} tasks, {c["issues"]} issues, '
          f'{c["usage_sessions"]} sessions; clock frozen at {iso(api["snapshot_at"])}; {len(text) / 1e6:.1f} MB; mtv {mtv.__version__}')
    print(f'build: wrote {demo}/ (open demo/index.html, or serve {os.path.abspath(ns.out)})')


if __name__ == '__main__':
    main()
