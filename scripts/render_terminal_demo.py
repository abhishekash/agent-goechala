"""Render the terminal walkthrough using synthetic records only.

Requires the project, cairosvg and Pillow. No receiver or network is used.
"""
import asyncio, os, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
from types import SimpleNamespace
os.environ.pop('NO_COLOR', None)
os.environ['TERM'] = 'xterm-256color'
sys.path.insert(0, str(ROOT))
from observatory.terminal import Snapshot, Feed
from observatory.terminal_ui import ObservatoryApp

now = time.time_ns()
trace_id = 'demotrace000000000000000000000001'
source_id = 'codex:demo-session-42'
session_id = 'demo-session-42'
service = 'codex'
def event(i, domain, action, subject, detail=None, **extra):
    return {'source_id': f'demo-event-{i}', 'time_unix_nano': now-(8-i)*1_000_000_000,
            'domain': domain, 'action': action, 'subject': subject, 'detail': detail or {},
            'service': service, 'session_id': session_id, 'result': 'success',
            'trace_id': trace_id, **extra}
activities = [
 event(1,'message','user','Summarize the latest database migration risks.'),
 event(2,'model','call','gpt-6-sol'),
 event(3,'shell','run','git status --short', {'command':'git status --short','exit_code':0,'output':' M src/migrations.py'}),
 event(4,'mcp','call','github / search_issues', {'query':'migration rollback strategy'}, mcp_server='github', mcp_tool='search_issues', span_id='span-mcp-demo'),
 event(5,'file','edit','src/migrations.py', {'operation':'edit','diff':'@@ -8,1 +8,2 @@\n-timeout = 15\n+timeout = 30'}),
 event(6,'message','assistant','I found one rollback risk and updated the timeout.'),
]
spans = [
 {'trace_id':trace_id,'span_id':'span-run-demo','parent_span_id':None,'name':'agent.run','start_unix_nano':now-9_000_000_000,'end_unix_nano':now,'status':'OK','service':service,'attributes':{'agent.task':'Review the migration and report the main risks.'},'events':[],'links':[]},
 {'trace_id':trace_id,'span_id':'span-model-demo','parent_span_id':'span-run-demo','name':'chat gpt-6-sol','start_unix_nano':now-8_000_000_000,'end_unix_nano':now-5_000_000_000,'status':'OK','service':service,'attributes':{'gen_ai.request.model':'gpt-6-sol','gen_ai.usage.input_tokens':1840,'gen_ai.usage.output_tokens':326},'events':[],'links':[]},
 {'trace_id':trace_id,'span_id':'span-mcp-demo','parent_span_id':'span-run-demo','name':'github.search_issues','start_unix_nano':now-4_000_000_000,'end_unix_nano':now-3_000_000_000,'status':'OK','service':service,'attributes':{'mcp.server.name':'github','mcp.tool.name':'search_issues'},'events':[],'links':[]},
]
summary = {'trace_id':trace_id,'task':'Review migration risks','status':'OK','active':False,'span_count':3,'input_tokens':1840,'output_tokens':326,'service':service}
snapshot = Snapshot(
 totals={'input_tokens':1840,'output_tokens':326},
 agents=[{'source_id':source_id,'service':service,'state':'running','span_count':3,'last_seen_ns':now,'integrations':['local-session-log']}],
 traces=[summary], detail={'trace_id':trace_id,'summary':summary,'spans':spans,'span_page':{'trace_total':3}},
 updated=time.time(), local_status={'sessions':[{'source_id':source_id,'service':service,'runtime':'Codex','project':'payments-api','session_id':session_id,'model':'gpt-6-sol','task':'Review migration risks','last_activity':activities[-1]}],
 'runtime':{'host':'devbox','os':'Linux','architecture':'x86_64','load_average':[0.64,0.72,0.80],'processes':[{'pid':3012,'name':'codex','project':'payments-api','state':'S (sleeping)','memory_rss_kib':184000,'cpu_seconds':23.7},{'pid':3088,'name':'python','project':'payments-api','state':'S (sleeping)','memory_rss_kib':96500,'cpu_seconds':8.2}]},'sources':1,'lookback_hours':24,'read_errors':0},
 activity=activities,
 mcp=[{'id':'demo-mcp-call','time_unix_nano':now-4_000_000_000,'server':'github','tool':'search_issues','method':'tools/call','status':'OK','duration_ms':842,'service':service,'trace_id':trace_id,'span_id':'span-mcp-demo','source':'MCP span','arguments':{'query':'migration rollback strategy','state':'open','limit':10},'output':{'matches':3,'top_issue':'Rollback after partially applied migration'},'session_id':session_id}],
 sessions=[{'session_id':session_id,'trace_count':1,'input_tokens':1840,'output_tokens':326,'known_cost_usd':0,'unknown_cost_runs':1}])
# A small, deliberately synthetic fleet makes the hierarchy visible.
for i, (runtime, project, model, task, seconds) in enumerate([
    ('Claude Code', 'checkout-web', 'claude-sonnet', 'Fix keyboard navigation in the checkout form', 12),
    ('Codex', 'infra', 'gpt-6-sol', 'Investigate the failing integration test', 28),
    ('Claude Code', 'docs', 'claude-sonnet', 'Document the new authentication flow', 420),
    ('Codex', 'payments-api', 'gpt-6-sol', 'Review API compatibility before release', 1800),
], 1):
    last = event(20+i, 'shell', 'run', 'pytest tests/integration -q')
    last['time_unix_nano'] = now-seconds*1_000_000_000
    snapshot.local_status['sessions'].append({
        'source_id': f'demo-source-{i}', 'session_id': f'demo-session-{i:06}',
        'service': 'codex' if runtime=='Codex' else 'claude', 'runtime': runtime,
        'project': project, 'model': model, 'task': task, 'last_activity': last})
for i, (server, tool, outcome, duration) in enumerate([
    ('filesystem', 'read_file', 'OK', 18),
    ('github', 'get_pull_request', 'OK', 312),
    ('browser', 'navigate', 'ERROR', 5000),
    ('filesystem', 'list_directory', 'OK', 9),
    ('github', 'search_issues', 'OK', 264),
    ('browser', 'get_page_content', 'OK', 92),
], 1):
    snapshot.mcp.append({**snapshot.mcp[0], 'id': f'demo-call-{i}',
        'time_unix_nano': now-(i+4)*1_000_000_000,
        'server': server, 'tool': tool, 'status': outcome, 'duration_ms': duration,
        'arguments': {'url': 'https://example.com/preview'} if server=='browser' else {'path': 'src/migrations.py'} if server=='filesystem' else {'repository': 'example/payments-api'},
        'output': {'error': 'Navigation timed out'} if outcome=='ERROR' else {'status': 'completed'}})
snapshot.totals = {'input_tokens': 284640, 'output_tokens': 18326}
snapshot.activity = sorted(snapshot.activity, key=lambda item:item['time_unix_nano'], reverse=True)

class Thread:
    def is_alive(self): return True
class FakeFeed(Feed):
    def __init__(self):
        super().__init__(SimpleNamespace(endpoint='http://127.0.0.1:14318'))
        self.snapshot=snapshot; self.thread=Thread(); self.paused=False; self.include_reviews=False
        self.api=SimpleNamespace(endpoint='http://127.0.0.1:14318')
        self.selected_trace=trace_id; self.selected_span_id=None; self.pinned_trace=False
        self.session_id=''; self.service=''; self.query=''
    def request_refresh(self):
        self.snapshot.activity_results = [row for row in self.snapshot.activity
            if (not self.service or row.get('service') == self.service)
            and (not self.session_id or row.get('session_id') == self.session_id)
            and (not self.activity_prompts or (row.get('domain') == 'message' and row.get('action') == 'user'))]
        self.snapshot.mcp_results = [row for row in self.snapshot.mcp
            if (not self.service or row.get('service') == self.service)
            and (not self.session_id or row.get('session_id') == self.session_id)]
        self.snapshot.queries = {'activity': self.activity_query, 'mcp': self.mcp_query, 'traces': self.query}
        self.snapshot.updated = time.time()


async def main():
    import argparse
    from io import BytesIO
    import cairosvg
    from PIL import Image
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshots',type=Path,help='Also write wide and compact preview PNGs')
    args=parser.parse_args()
    def raster(svg):
        svg=svg.replace('Fira Code','DejaVu Sans Mono').replace('FiraCode','DejaVu Sans Mono')
        return Image.open(BytesIO(cairosvg.svg2png(bytestring=svg.encode(),output_width=1500))).convert('RGB')
    app=ObservatoryApp(FakeFeed())
    frames=[]
    async with app.run_test(size=(150,46)) as pilot:
        await pilot.pause(.4)
        app.demo_mode = True
        app.update_layout()
        for page in ('agents','activity','mcp','traces','host'):
            app.action_page(page)
            await pilot.pause(.5)
            frame=raster(app.export_screenshot())
            frames.append(frame)
            if args.snapshots:
                args.snapshots.mkdir(parents=True,exist_ok=True)
                frame.save(args.snapshots/f'{page}-wide.png')
        if args.snapshots:
            await pilot.resize_terminal(80,24)
            for page in ('agents','mcp'):
                app.action_page(page)
                await pilot.pause(.5)
                raster(app.export_screenshot()).save(args.snapshots/f'{page}-compact.png')
            app.action_expand()
            await pilot.pause(.3)
            raster(app.export_screenshot()).save(args.snapshots/'mcp-expanded.png')
    target=ROOT/'media'/'goechala-terminal-demo.gif'
    frames[0].save(target,save_all=True,append_images=frames[1:],duration=[2500,1800,2500,1800,1800],loop=0,optimize=True)
    print(f'Wrote synthetic walkthrough: {target} ({target.stat().st_size:,} bytes)')

if __name__=='__main__':
    asyncio.run(main())
