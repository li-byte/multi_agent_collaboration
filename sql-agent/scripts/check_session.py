"""运行真实前端 JS，验证当前会话多轮视图和重连去重（无数据库）。"""
import subprocess
import shutil
import tempfile
from pathlib import Path
from check_export import SETUP, SCRIPT_RE, WEB

TESTS = r'''
newSession();
store.conversationId = 'conv-a'; store.runId = 'run-1';
const event = (run, seq, type, payload) => ({global_task_id:run, seq, role:'planner', round:0, event_type:type, payload});
handleEvent(event('run-1', 1, 'run_created', {question:'第一问', turn:1, conversation_id:'conv-a'}));
const usage = event('run-1', 2, 'llm_call', {total_tokens:10});
handleEvent(usage); handleEvent(usage);
__ok(store.usage.calls === 1, '重连回放同一事件不重复计算消耗');
store.audit = [{global_task_id:'run-1', sql:'SELECT 1'}];
beginTurn(); store.runId = 'run-2';
handleEvent(event('run-2', 3, 'run_created', {question:'第二问', turn:2, conversation_id:'conv-a'}));
handleEvent(event('run-2', 4, 'llm_call', {total_tokens:20}));
__ok(store.events.length === 4 && store.usage.total_tokens === 30, '继续提问保留全会话事件与累计消耗');
__ok(buildSequence().filter(s=>s.kind==='band').length === 2, '时序图包含两轮对话');
__ok(buildFlow().filter(s=>s.kind==='start').length === 2, '数据流转包含两轮起点');
global.fetch = async url => ({json:async()=>({tasks:[{sub_task_id:'task-2'}], audit:[{sql:'SELECT 2'}]})});
await refreshDetail(); await refreshDetail();
__ok(store.audit.length === 2, '刷新只替换当前轮审计、不清空前轮或重复追加');
handleEvent(event('run-2', 5, 'await_confirm', {sql_hash:'a'}));
handleEvent(event('run-2', 6, 'confirmed', {approve:false,by:'用户'}));
const cancelledCard = store.blocks.flatMap(b=>b.items).find(i=>i.type==='confirm');
beginTurn(); store.runId = 'run-3';
handleEvent(event('run-3', 7, 'await_confirm', {sql_hash:'b'}));
handleEvent(event('run-3', 8, 'confirmed', {approve:true,by:'用户'}));
__ok(cancelledCard.approve === false, '后轮确认不能改写前轮取消记录');
let requested = '';
global.fetch = async url => {requested=String(url);return {json:async()=>({total:{calls:2,total_tokens:30},calls:[],llm_enabled:true})};};
await openInfo('usage');
__ok(requested === '/api/conversations/conv-a/usage', '消耗详情按当前会话请求');
newSession();
__ok(store.events.length === 0 && store.usage.calls === 0 && store.audit.length === 0, '新建会话隔离旧记录');
const turns = [1,2].map(i=>({global_task_id:'r'+i,question:'问题'+i,status:'done',turn:i,
 audit:[{sql:'SELECT '+i}],events:[event('r'+i,i*10,'run_created',{question:'问题'+i,turn:i,conversation_id:'conv-b'}),
 event('r'+i,i*10+1,'llm_call',{total_tokens:i*10})]}));
global.fetch = async url => ({json:async()=>String(url).includes('/api/conversations/')?{turns}:[]});
await openSession('conv-b');
__ok(store.usage.total_tokens === 30 && store.audit.length === 2, '历史会话重开累计全部轮次');

const svg = buildUsageSvg({llm_enabled:true,total:{calls:2,total_tokens:30},calls:[
 {call_id:1,role:'planner',turn:1,round:0,cursor:0},
 {call_id:2,role:'planner',turn:2,round:0,cursor:0}]}).svg;
__ok(svg.includes('第 1 轮') && svg.includes('第 2 轮'), '消耗导出保留各对话轮次');
newSession(); document.getElementById('input').value = '旧会话问题';
let resolveCreate; const startUrls=[];
global.fetch = async url => {
 if (String(url)==='/api/runs') return {json:()=>new Promise(resolve=>{resolveCreate=resolve;})};
 startUrls.push(String(url)); return {json:async()=>[]};
};
const sending = send();
await new Promise(resolve=>setTimeout(resolve,0));
newSession(); store.conversationId='new-conv'; store.runId='new-run';
resolveCreate({global_task_id:'old-run',conversation_id:'old-conv',turn:1});
await sending;
__ok(store.conversationId==='new-conv' && store.runId==='new-run', '创建任务响应不能覆盖已切换会话');
__ok(startUrls.includes('/api/runs/old-run/start'), '已授权旧任务仍正常启动');
let resolveHistory;
global.fetch = async url => ({json:()=>new Promise(resolve=>{resolveHistory=resolve;})});
const reopening = openSession('conv-old');
await new Promise(resolve=>setTimeout(resolve,0));
newSession(); store.conversationId='conv-new';
resolveHistory({turns}); await reopening;
__ok(store.events.length===0 && store.conversationId==='conv-new', '延迟历史回放不能污染新会话');
if (__results.some(([ok])=>!ok)) process.exitCode=1;

'''

if __name__ == '__main__':
    node = shutil.which('node')
    if not node: raise SystemExit('需要 Node.js 验证前端行为')
    with tempfile.TemporaryDirectory() as td:
        harness = Path(td)/'session.mjs'
        harness.write_text(SETUP+'\n'+SCRIPT_RE.findall(WEB.read_text(encoding='utf-8'))[0]+'\n'+TESTS, encoding='utf-8')
        p = subprocess.run([node,str(harness),str(WEB),td],capture_output=True,text=True,encoding='utf-8')
        print(p.stdout); print(p.stderr)
        raise SystemExit(p.returncode)
