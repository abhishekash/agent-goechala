/* Live observed activity, independent from the selected investigation trace. */
(() => {
  const node = (tag, cls, text) => {const element=document.createElement(tag);if(cls)element.className=cls;if(text!==undefined)element.textContent=text;return element;};
  const panel=node('section','panel live-work');panel.id='live-work';
  const heading=node('div','work-heading');
  const intro=node('div');intro.append(node('div','eyebrow','Live observatory'),node('h1','','What your agents are doing'));
  const state=node('span','work-live','Connecting…');heading.append(intro,state);panel.append(heading);
  const note=node('p','muted work-note');panel.append(note);
  const stats=node('div','work-stats');panel.append(stats);
  const sessions=node('div','work-sessions');panel.append(sessions);
  const expand=node('button','work-expand','Show all sessions');expand.type='button';panel.append(expand);
  const machine=node('details','work-machine');panel.append(machine);
  const toolbar=node('div','work-toolbar');
  const search=node('input');search.type='search';search.placeholder='Search commands, files, tools, messages…';search.setAttribute('aria-label','Search live activity');
  const agent=node('select');agent.setAttribute('aria-label','Filter activity by agent');agent.append(new Option('All agents',''));
  const sessionFilter=node('select');sessionFilter.setAttribute('aria-label','Filter activity by session');sessionFilter.append(new Option('All sessions',''));
  const pause=node('button','refresh','Pause');pause.type='button';
  const follow=node('button','refresh','Follow latest');follow.type='button';toolbar.append(search,agent,sessionFilter,pause,follow);panel.append(toolbar);
  const categories=node('div','work-categories');panel.append(categories);
  const layout=node('div','work-layout');const list=node('div','work-feed');list.setAttribute('aria-label','Observed activity');
  const inspector=node('div','work-inspector');layout.append(list,inspector);panel.append(layout);
  const footer=node('div','muted work-footer');panel.append(footer);
  const investigation=document.getElementById('investigation');investigation.before(panel);
  let rows=[],selected=null,domain='',paused=false,busy=false,health={},metrics={},timer,following=true,inspected='',allSessions=false;
  const labels={all:'Everything',model:'Models',mcp:'MCP',shell:'Commands',file:'Files',browser:'Browser',tool:'Tools',message:'Messages',agent:'Agent',context:'Context'};
  const symbols={model:'✦',shell:'$_',file:'↳',browser:'↗',tool:'⌘',message:'“',agent:'◉',context:'◇'};
  const date=value=>new Date(Number(value)/1e6);
  function detailText(value){if(typeof value==='string'){try{return JSON.stringify(JSON.parse(value),null,2)}catch{return value}}return JSON.stringify(value,null,2);}
  function renderInspector(){
    const item=rows.find(item=>item.source_id===selected)||rows[0];
    const key=item?item.source_id+':'+item.detail:'empty';
    if(key===inspected)return;inspected=key;
    inspector.replaceChildren();
    if(!item){inspector.append(node('div','work-empty','Select an action to inspect its evidence.'));return;}
    inspector.append(node('div','eyebrow',item.domain+' / '+item.action),node('h2','',item.subject||item.action));
    const meta=node('div','work-evidence');meta.append(node('span','',item.service),node('span','',item.result),node('time','',date(item.time_unix_nano).toLocaleString()));inspector.append(meta);
    let details;try{details=JSON.parse(item.detail)}catch{details=item.detail}
    if(details&&typeof details==='object'&&!Array.isArray(details)){
      for(const [key,value] of Object.entries(details)){
        if(value==null||value==='')continue;
        const block=node('div','work-detail');block.append(node('h3','',key.replaceAll('_',' ')),node('pre','',detailText(value)));inspector.append(block);
      }
    }else if(details){inspector.append(node('pre','work-content',detailText(details)));}
    else inspector.append(node('p','muted','This action has no additional recorded content.'));
    const open=node('button','refresh','Open related trace');open.type='button';open.onclick=async()=>{try{await openTrace(item.trace_id);investigation.scrollIntoView({behavior:'smooth',block:'start'});}catch(error){footer.textContent='Could not open trace: '+error.message;}};inspector.append(open);
    inspector.append(node('p','muted work-provenance','Evidence from '+item.source+'. Content may be truncated; recognizable credentials are redacted by local session adapters.'));
  }
  function render(){
    const local=health.local_agents||{};
    const runtime=local.runtime||{};
    state.textContent=paused?'Paused':local.enabled?'Watching local agents':'Receiving telemetry';
    state.classList.toggle('paused',paused);
    note.textContent=local.enabled?'Actual local session records · '+(local.capture_content?'activity and bounded content':'metadata only')+' · Updates every 2 seconds':'Connect an instrumented agent to see its observed actions here.';
    stats.replaceChildren();
    const values=[['Sessions observed',local.sources||0],['Agent processes',(runtime.processes||[]).length],['Input / output tokens',Number(metrics.input_tokens||0).toLocaleString()+' / '+Number(metrics.output_tokens||0).toLocaleString()],['Recorded errors',metrics.errors||0]];
    for(const [label,value] of values){const card=node('div','work-stat');card.append(node('span','muted',label),node('strong','',String(value)));stats.append(card);}
    sessions.replaceChildren();
    const observed=(local.sessions||[]).slice().sort((a,b)=>(b.last_activity?.time_unix_nano||0)-(a.last_activity?.time_unix_nano||0));
    const sessionIds=new Set([...sessionFilter.options].map(option=>option.value));
    for(const session of observed)if(!sessionIds.has(session.session_id))sessionFilter.append(new Option(session.runtime+' · '+(session.project||'session')+' · '+session.session_id.slice(0,8),session.session_id));
    expand.hidden=observed.length<=4;expand.textContent=allSessions?'Show recent sessions':'Show all '+observed.length+' sessions';
    for(const session of (allSessions?observed:observed.slice(0,4))){
      const card=node('div','work-session');const head=node('div','work-session-head');head.append(node('strong','',session.runtime),node('span','muted',session.project||'Local session'));card.append(head);
      const latest=session.last_activity;
      const fresh=latest&&Date.now()-Number(latest.time_unix_nano)/1e6<35000;
      card.append(node('span','work-presence'+(fresh?' recent':''),fresh?'Recent activity':'Idle / last observed'));
      card.append(node('p','work-task',session.task||'Task text not recorded'));
      card.append(node('div','muted work-model',session.model||'Model not reported'));
      if(latest)card.append(node('div','work-last',latest.domain+' · '+latest.action+' · '+latest.subject));
      const inspect=node('button','work-expand','View session activity');inspect.type='button';inspect.onclick=()=>{sessionFilter.value=session.session_id;selected=null;following=true;refresh();};card.append(inspect);
      sessions.append(card);
    }
    const wasOpen=machine.open;machine.replaceChildren(node('summary','',`${runtime.host||'Local host'} · ${(runtime.processes||[]).length} known agent processes · runtime details`));machine.open=wasOpen;
    const table=node('table','work-processes');const header=node('tr');for(const label of ['Agent / PID','Project','State','RAM','CPU total'])header.append(node('th','',label));table.append(header);
    for(const process of runtime.processes||[]){const row=node('tr');for(const value of [process.name+' #'+process.pid,process.project||'—',process.state,Math.round(process.memory_rss_kib/1024)+' MiB',process.cpu_seconds+'s'])row.append(node('td','',value));table.append(row);}machine.append(table);
    machine.append(node('p','muted work-provenance','Process inventory is separate from session observations. '+(runtime.load_average?'Host load averages: '+runtime.load_average.map(value=>Number(value).toFixed(2)).join(' / '):'Host load unavailable.')));
    const known=new Set([...agent.options].map(option=>option.value));
    for(const service of new Set(rows.map(item=>item.service)))if(!known.has(service))agent.append(new Option(service,service));
    categories.replaceChildren();
    for(const [key,label] of Object.entries(labels)){
      const count=key==='all'?rows.length:rows.filter(item=>item.domain===key).length;
      const button=node('button','activity-filter'+((domain||'all')===key?' active':''),label+' '+count);button.type='button';button.setAttribute('aria-pressed',String((domain||'all')===key));button.onclick=()=>{domain=key==='all'?'':key;selected=null;render();};categories.append(button);
    }
    const scroll=list.scrollTop;
    list.replaceChildren();
    const visible=rows.filter(item=>!domain||item.domain===domain);
    if(following||!visible.some(item=>item.source_id===selected))selected=visible[0]?.source_id||null;
    if(!visible.length)list.append(node('div','work-empty',rows.length?'No actions match this category.':'Waiting for observed activity. Local logs are discovered automatically.'));
    for(const item of visible){
      const button=node('button','work-event'+(item.source_id===(selected||visible[0]?.source_id)?' selected':'')+(['error','failed','denied','blocked'].includes(item.result)?' error':''));button.type='button';
      const symbol=node('span','work-symbol',symbols[item.domain]||'·');symbol.title=item.domain;
      const content=node('div','work-event-content');const top=node('div','work-event-top');top.append(node('strong','',item.action),node('span','muted',item.service),node('span','work-result',item.result));
      content.append(top,node('div','work-subject',item.subject||item.domain));button.append(symbol,content,node('time','muted',date(item.time_unix_nano).toLocaleTimeString()));
      button.onclick=()=>{following=false;selected=item.source_id;render();};list.append(button);
    }
    list.scrollTop=following?0:scroll;
    renderInspector();
    footer.textContent=(runtime.host?runtime.host+' · '+runtime.os+' / '+runtime.architecture+' · ':'')+'Latest 100 observed traces · '+(local.read_errors||0)+' read errors · '+(local.skipped_records||0)+' skipped records. Only recorded activity is shown. Input counts include cached and repeated context. Costs depend on reported telemetry.';
  }
  async function refresh(){
    if(busy||paused||document.hidden)return;busy=true;
    try{
      const params=new URLSearchParams({limit:'150',service:agent.value,session_id:sessionFilter.value,q:search.value.trim()});
      const data=await Promise.all([get('/api/health'),get('/api/activity?'+params),get('/api/metrics')]);
      health=data[0];rows=data[1].activity;metrics=data[2];render();
    }catch(error){state.textContent='Reconnecting…';footer.textContent='Activity refresh failed: '+error.message;}finally{busy=false;}
  }
  pause.onclick=()=>{paused=!paused;pause.textContent=paused?'Resume':'Pause';render();if(!paused)refresh();};
  follow.onclick=()=>{following=true;selected=null;render();};
  agent.onchange=()=>{selected=null;refresh();};
  sessionFilter.onchange=()=>{selected=null;following=true;refresh();};
  expand.onclick=()=>{allSessions=!allSessions;render();};
  search.oninput=()=>{clearTimeout(timer);timer=setTimeout(()=>{selected=null;refresh();},250);};
  window.refreshLiveActivity=refresh;
  refresh();setInterval(refresh,2000);
})();
