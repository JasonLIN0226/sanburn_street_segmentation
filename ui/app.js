'use strict';
const $=s=>document.querySelector(s);
const state={maps:[],jobs:[],selected:new Set(),region:'all',query:'',active:null,tab:'overlay',token:null,busy:false,detailKey:null,logOpen:false,flowOpen:new Map(),flowScroll:new Map(),flowVersions:new Map(),flowLoading:new Set(),flowPolling:new Set(),deleting:new Set()};
const names={queued:'Waiting',preparing:'Running',running:'Running',completed:'Complete',failed:'Failed',interrupted:'Interrupted'};
const stages={agent_initialization:'Preparing',source_review:'Source review',initial_build:'Initial label',first_review:'First review',revision:'Revision',final_gate:'Final check',final_review:'Final review',export:'Export',complete:'Complete'};
const phases=[{name:'Source review',keys:['source_review']},{name:'Initial label',keys:['initial_build']},{name:'Review and revision',keys:['first_review','revision']},{name:'Final check',keys:['final_gate','final_review','export','complete']}];
const modeNames={initial:'Initial Mask',max:'MAX Mode'};
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const n=x=>x==null?'—':Number(x).toLocaleString('en-US');
const duration=s=>{if(s==null)return '—';s=Math.floor(s);return s>=3600?`${Math.floor(s/3600)} h ${Math.floor(s%3600/60)} min`:s>=60?`${Math.floor(s/60)} min ${s%60} s`:`${s} s`;};
const money=x=>x==null?'—':`$${Number(x).toFixed(x<.01?4:2)}`;
const active=j=>['queued','preparing','running'].includes(j.status);
async function request(path,options){const r=await fetch(path,options);const d=await r.json();if(!r.ok)throw new Error(d.error||`Request failed ${r.status}`);return d;}
function renderRegions(){const groups=[['all','All'],...Array.from(new Map(state.maps.map(m=>[m.region,m.region_label])))];$('#regions').innerHTML=groups.map(([id,label])=>`<button class="region ${state.region===id?'active':''}" data-region="${esc(id)}">${esc(label)}</button>`).join('');document.querySelectorAll('.region').forEach(b=>b.onclick=()=>{state.region=b.dataset.region;renderRegions();renderMaps();});}
function renderMaps(){const items=state.maps.filter(m=>(state.region==='all'||m.region===state.region)&&(!state.query||`${m.label} ${m.region_label} ${m.file}`.toLowerCase().includes(state.query)));$('#mapList').innerHTML=items.length?items.map(m=>`<label class="map-item ${state.selected.has(m.id)?'selected':''}"><input type="checkbox" data-map="${esc(m.id)}" ${state.selected.has(m.id)?'checked':''}><span class="map-copy"><strong>${esc(m.region_label)} · ${esc(m.label)}</strong><small>${esc(m.id)}</small></span><span class="map-ext">${esc(m.format.toUpperCase())}</span></label>`).join(''):'<div class="muted">No matching maps.</div>';document.querySelectorAll('[data-map]').forEach(el=>el.onchange=()=>{if(el.checked)state.selected.add(el.dataset.map);else state.selected.delete(el.dataset.map);renderMaps();});$('#selectedCount').textContent=`${state.selected.size} selected`;$('#start').disabled=state.busy||!state.selected.size;}
function renderTasks(){if(!state.jobs.length){$('#taskList').innerHTML='<div class="empty-list">No tasks yet.</div>';return;}$('#taskList').innerHTML=state.jobs.map(j=>`<div class="task-row ${state.active===j.id?'selected':''}"><button class="task ${state.active===j.id?'active':''}" data-job="${esc(j.id)}"><span class="task-top"><strong>${esc(j.region_label)} · ${esc(j.map_label)}</strong><span class="status ${esc(j.status)}">${esc(names[j.status]||j.status)}</span></span><small>${esc(j.stage?stages[j.stage]||j.stage:(active(j)?'Waiting to start':'Stage record unavailable'))} · ${esc(duration(j.elapsed_seconds))}</small></button><button class="delete-task" data-delete="${esc(j.id)}" ${state.deleting.has(j.id)?'disabled':''} aria-label="${active(j)?'Stop and hide':'Hide'} ${esc(j.region_label)} ${esc(j.map_label)}">${state.deleting.has(j.id)?'Stopping…':active(j)?'Stop & hide':'Hide'}</button>${!active(j)?`<button class="delete-task" data-remove="${esc(j.id)}" aria-label="Delete saved files">Delete files</button>`:''}</div>`).join('');document.querySelectorAll('.task[data-job]').forEach(b=>b.onclick=()=>{state.active=b.dataset.job;const j=state.jobs.find(x=>x.id===state.active);state.tab=j&&active(j)?'workflow':'overlay';state.logOpen=false;state.detailKey=null;renderTasks();renderDetail();});document.querySelectorAll('[data-delete]').forEach(b=>b.onclick=()=>deleteTask(b.dataset.delete));document.querySelectorAll('[data-remove]').forEach(b=>b.onclick=()=>removeRun(b.dataset.remove));}
async function deleteTask(id){if(state.deleting.has(id))return;state.deleting.add(id);renderTasks();try{await request(`/api/delete/${id}`,{method:'POST',headers:{'X-UI-Token':state.token}});if(state.active===id){state.active=null;state.detailKey=null;}$('#message').textContent='Task removed. Its saved files remain in the run folder.';await refreshJobs();}catch(e){$('#message').textContent='Could not delete task: '+e.message;}finally{state.deleting.delete(id);renderTasks();}}
async function removeRun(id){if(!window.confirm('Permanently delete this run and its saved results? This cannot be undone.'))return;try{await request(`/api/remove-run/${id}`,{method:'POST',headers:{'X-UI-Token':state.token}});if(state.active===id){state.active=null;state.detailKey=null;}$('#message').textContent='Saved run deleted.';await refreshJobs();}catch(e){$('#message').textContent='Could not delete saved run: '+e.message;}}
function updateDetailNumbers(j){const time=$('#metricTime'),tokens=$('#metricTokens'),cost=$('#metricCost'),stage=$('#detailStage');if(time)time.textContent=duration(j.elapsed_seconds);if(tokens)tokens.textContent=n(j.usage?.tokens);if(cost)cost.textContent=money(j.usage?.cost_usd);if(stage)stage.textContent=j.stage?(stages[j.stage]||j.stage):'Waiting';}
function rememberFlow(){const box=$('#workflowView');if(!box||!box.dataset.job)return;const open={};box.querySelectorAll('details[data-flow-key]').forEach(d=>{open[d.dataset.flowKey]=d.open;});if(Object.keys(open).length)state.flowOpen.set(box.dataset.job,open);const feed=box.querySelector('.conversation-feed');if(feed&&box.dataset.loaded)state.flowScroll.set(box.dataset.job,{top:feed.scrollTop,atBottom:feed.scrollHeight-feed.clientHeight-feed.scrollTop<40});}
function renderDetail(){const j=state.jobs.find(x=>x.id===state.active);if(!j){state.detailKey=null;$('#detail').innerHTML='<div class="empty-detail">Select a task to view its results and progress.</div>';return;}const key=`${j.id}|${j.status}|${state.tab}|${state.logOpen}|${j.file_versions?.[state.tab]||''}`;if(key===state.detailKey){updateDetailNumbers(j);return;}state.detailKey=key;const done=j.status==='completed',flow=state.tab==='workflow';const note=(j.usage?.estimate_notes||[]).join('; ');const err=j.error?`<div class="error">${esc(j.error)}</div>`:'';const viewer=flow?`<div id="workflowView" class="workflow-view" data-job="${esc(j.id)}">Loading progress...</div>`:done?`<img src="/api/preview/${esc(j.id)}/${esc(state.tab)}?v=${esc(j.file_versions?.[state.tab]||'')}" alt="${esc(({source:'Source',overlay:'Overlay',mask:'Mask'})[state.tab])}" loading="lazy">`:`<div class="viewer-message">${j.status==='failed'||j.status==='interrupted'?'Task incomplete. View Progress or the log for details.':'Results will appear when the task is complete.'}<br>Current stage: <b id="detailStage">${esc(j.stage?(stages[j.stage]||j.stage):'Waiting')}</b></div>`;
rememberFlow();$('#detail').innerHTML=`<div class="detail-inner ${flow?'detail-flow':''}"><div class="detail-title"><div><div class="section-label">SELECTED TASK</div><h3>${esc(j.region_label)} · ${esc(j.map_label)}</h3><p>${esc(j.map_path)} · ${esc(modeNames[j.mode]||modeNames.max)} · ${esc(j.case_name)}</p></div><span class="status ${esc(j.status)}">${esc(names[j.status]||j.status)}</span></div><div class="metrics"><div class="metric"><span>Elapsed time</span><strong id="metricTime">${esc(duration(j.elapsed_seconds))}</strong></div><div class="metric"><span>Total tokens</span><strong id="metricTokens">${esc(n(j.usage?.tokens))}</strong></div><div class="metric" title="Estimated from recorded usage; actual charges may differ."><span>Estimated cost (USD)</span><strong id="metricCost">${esc(money(j.usage?.cost_usd))}</strong></div></div>${err}<div class="tabs">${[['workflow','Progress'],['source','Source'],['overlay','Overlay'],['mask','Mask']].map(([id,label])=>`<button class="tab ${state.tab===id?'active':''}" data-tab="${id}">${label}</button>`).join('')}</div><div class="${flow?'flow-shell':'viewer'}">${viewer}</div><div class="detail-actions">${done&&!flow?`<a href="/api/download/${esc(j.id)}/${esc(state.tab)}">Download ${esc(({source:'source',overlay:'overlay',mask:'full-size mask'})[state.tab])}</a>`:'<span></span>'}<button id="showLog">${state.logOpen?'Hide log':'View log'}</button></div><p class="cost-note">Estimated using recorded usage and Sol standard rates as of 2026-11-03; actual charges may differ. ${esc(note)}</p><div id="logBox"></div></div>`;
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{state.tab=b.dataset.tab;state.detailKey=null;renderDetail();});$('#showLog').onclick=()=>{state.logOpen=!state.logOpen;state.detailKey=null;renderDetail();};if(flow)loadWorkflow(j.id);if(state.logOpen)request(`/api/log/${j.id}`).then(d=>{const box=$('#logBox');if(box&&state.active===j.id)box.innerHTML=`<pre class="log">${esc(d.log)}</pre>`;}).catch(()=>{});}
function observations(data){const found=[];function walk(x,path){if(!x||typeof x!=='object')return;for(const [k,v] of Object.entries(x)){const where=path?`${path} / ${k}`:k;if(['observation','global_observation','global_check','edge','nonroad'].includes(k)&&typeof v==='string'&&v.trim())found.push({where,text:v});else if(['issues','unresolved'].includes(k)&&Array.isArray(v))v.forEach(i=>found.push({where,text:typeof i==='string'?i:JSON.stringify(i)}));else if(typeof v==='object')walk(v,where);}}walk(data,'');return found;}
function artifactTitle(name){if(name.startsWith('draft_batch')||name.startsWith('detail_observations'))return 'Source observations';if(name.startsWith('inventory'))return 'Street inventory';if(name.startsWith('qa_notes')||name==='qa_r1.json')return 'First review';if(name==='revision_review.json')return 'Revision review';if(name==='qa_final.json'||name==='summary.json')return 'Final review';return null;}
function evidenceBlock(e,id){const outputs=e.outputs.filter(o=>o.type==='images'||o.type==='missing_images');if(!outputs.length)return '';return `<details class="evidence-entry" data-flow-key="image:${e.seq}"><summary>${esc(stages[e.stage]||e.stage)} · image set ${e.seq}</summary>${outputs.map(o=>o.type==='missing_images'?'<p class="flow-note">The saved image record could not be matched.</p>':`<div class="flow-images">${o.images.map(x=>`<figure><a href="/api/workflow-image/${esc(id)}/${x.receipt}/${x.index}?v=${esc(x.version||'')}" target="_blank" rel="noopener"><img loading="lazy" src="/api/workflow-image/${esc(id)}/${x.receipt}/${x.index}?v=${esc(x.version||'')}" alt="${esc(x.path)}"></a><figcaption>${esc(x.path)}</figcaption></figure>`).join('')}</div>`).join('')}</details>`;}
function progressMarkup(j,d){
 const initial=j.mode==='initial';
 const plannedPhases=initial?phases.slice(0,2):phases;
 const latest=d.stages.map(x=>x.stage).filter(Boolean).at(-1)||j.stage||'agent_initialization';
 const completed=j.status==='completed'&&latest==='complete';
 const currentIndex=plannedPhases.findIndex(phase=>phase.keys.includes(latest));
 const observed=new Set(d.stages.map(x=>x.stage));
 const step=completed?plannedPhases.length:currentIndex;
 const width=completed?100:step<0?0:Math.round((step+.5)*100/plannedPhases.length);
 const items=plannedPhases.map((phase,index)=>{
  const seen=phase.keys.some(key=>observed.has(key));
  let label='Waiting',cls='waiting';
  if(completed||index<step){label=seen?'Complete':'Not recorded';cls=seen?'complete':'waiting';}
  else if(index===step&&seen){
   label=active(j)?'Running':names[j.status]||j.status;
   cls=active(j)?'running':'failed';
  }
  return `<div class="progress-step"><span class="step-name">${esc(phase.name)}</span><span class="status ${cls}">${esc(label)}</span></div>`;
 }).join('');
 return `<section class="progress-section"><div class="section-heading"><div><span class="section-label">WORKFLOW</span><h4>Progress</h4></div><span class="progress-current">${esc(stages[latest]||latest)}</span></div><progress class="progress-track" aria-label="Workflow stage progress" title="Based on the latest recorded stage, not estimated time remaining." max="100" value="${width}"></progress><div class="progress-steps${initial?' initial':''}">${items}</div><p class="flow-note">Based on the latest recorded stage, not estimated time remaining.</p></section>`;
}
function savedNote(name,data,key,recorded){
 const entries=observations(data);if(!entries.length)return '';
 const front=entries.slice(0,3),rest=entries.slice(3);
 const render=x=>`<div class="thread-note"><span>${esc(x.where)}</span><p>${esc(x.text)}</p></div>`;
 return `<article class="thread-message"><div class="thread-meta"><strong>${['summary.json','review_plan.json','coverage_audit.json'].includes(name)?'Saved output':'Agent output'}</strong><span>${esc(name)} · ${recorded?'snapshot after tool call':'latest saved copy; exact contents at this step are not recorded'}</span></div><div class="thread-notes">${front.map(render).join('')}${rest.length?`<details data-flow-key="notes:${esc(key)}"><summary>Show ${rest.length} more observations</summary>${rest.map(render).join('')}</details>`:''}</div></article>`;
}
function imageMessage(output,id,seq){
 if(output.type==='missing_images')return '<article class="thread-message"><div class="thread-meta"><strong>Image evidence</strong></div><p class="thread-muted">The saved image record could not be matched.</p></article>';
 return `<article class="thread-message"><div class="thread-meta"><strong>Image evidence</strong><span>Response ${seq}</span></div><div class="thread-images">${output.images.map(x=>`<figure><a href="/api/workflow-image/${esc(id)}/${x.receipt}/${x.index}?v=${esc(x.version||'')}" target="_blank" rel="noopener"><img loading="lazy" src="/api/workflow-image/${esc(id)}/${x.receipt}/${x.index}?v=${esc(x.version||'')}" alt="${esc(x.path)}"></a><figcaption>${esc(x.path)}</figcaption></figure>`).join('')}</div></article>`;
}
function labelMessage(id,rev,kind,version){const url=`/api/stage-preview/${esc(id)}/${esc(rev)}?v=${esc(version||'')}`;return `<article class="thread-message"><div class="thread-meta"><strong>Label result</strong><span>${esc(rev)} · ${esc(kind)}</span></div><figure class="thread-label"><a href="${url}" target="_blank" rel="noopener"><img src="${url}" loading="lazy" alt="${esc(rev)} full-map label overview"></a><figcaption>Saved label overview · ${esc(rev)}</figcaption></figure></article>`;}
function writtenBy(command,name){
 const escaped=name.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
 const direct=new RegExp(`(?:\\bcat\\s*>\\s*['\"]?${escaped}(?:['\"\\s]|$)|\\bPath\\(\\s*['\"]${escaped}['\"]\\s*\\)\\s*\\.\\s*write_(?:text|bytes)\\s*\\()`, 's');
 return direct.test(command);
}
function makeConversation(id,d){
 const captured=new Set(d.events.flatMap(e=>e.outputs.filter(o=>o.type==='saved_artifact').map(o=>o.name)));
 const legacyBySeq=new Map(),unplaced=[];
 for(const artifact of d.artifacts){
  if(!artifactTitle(artifact.name)||captured.has(artifact.name)||!observations(artifact.data).length)continue;
  let last=null;
  for(const event of d.events)for(const output of event.outputs)if(output.type==='command'&&output.exit_code===0&&writtenBy(output.command,artifact.name))last=event.seq;
  if(last==null)unplaced.push(artifact);else{if(!legacyBySeq.has(last))legacyBySeq.set(last,[]);legacyBySeq.get(last).push(artifact);}
 }
 const versions=new Set((d.revisions||[]).map(v=>v.revision)),revisionByName=new Map((d.revisions||[]).map(v=>[v.revision,v])),shownVersions=new Set();
 let thread='',currentStage=null;
 function add(stage,content){if(!content)return;if(stage!==currentStage){thread+=`<div class="thread-stage"><span>${esc(stages[stage]||stage||'Recorded stage')}</span></div>`;currentStage=stage;}thread+=content;}
 for(const event of d.events){
  const stage=event.stage||'agent_initialization';let body='';let buildVersions=[];
  for(const output of event.outputs){
   if(output.type==='text'&&output.text)body+=`<article class="thread-message"><div class="thread-meta"><strong>Agent</strong><span>Response ${event.seq}</span></div><p class="thread-text">${esc(output.text)}</p></article>`;
   else if(output.type==='images'||output.type==='missing_images')body+=imageMessage(output,id,event.seq);
   else if(output.type==='saved_artifact')body+=savedNote(output.name,output.data,`${event.seq}:${output.name}`,true);
   else if(output.type==='command'){
    if(output.matched&&output.exit_code!=null&&output.exit_code!==0)body+=`<details class="thread-tool-error" data-flow-key="error:${event.seq}"><summary>Tool returned exit code ${esc(output.exit_code)} · response ${event.seq}</summary><pre>${esc(output.stderr||output.stdout||'No error text was recorded.')}</pre></details>`;
    if(output.matched&&output.exit_code===0)for(const match of output.command.matchAll(/\bconditional\.py\s+build\s+(inventory_r\d+)\b/g))if(versions.has(match[1])&&!shownVersions.has(match[1]))buildVersions.push(match[1]);
   }
  }
  for(const artifact of legacyBySeq.get(event.seq)||[])body+=savedNote(artifact.name,artifact.data,`${event.seq}:${artifact.name}`,false);
  add(stage,body);
  for(const rev of buildVersions){if(shownVersions.has(rev))continue;shownVersions.add(rev);add(rev==='inventory_r1'?'initial_build':'revision',labelMessage(id,rev,'saved version referenced by tool call',revisionByName.get(rev)?.version));}
 }
 const remaining=(d.revisions||[]).filter(v=>!shownVersions.has(v.revision));
 if(remaining.length||unplaced.length){thread+='<div class="thread-stage"><span>Other saved records · exact timing unavailable</span></div>';for(const v of remaining)thread+=labelMessage(id,v.revision,'saved version',v.version);for(const a of unplaced)thread+=savedNote(a.name,a.data,`saved:${a.name}`,false);}
 return thread||'<p class="empty-section">No agent outputs have been recorded yet.</p>';
}
async function loadWorkflow(id){
 const box=$('#workflowView'),j=state.jobs.find(x=>x.id===id);
 if(!box||!j||box.dataset.job!==id||state.flowLoading.has(id))return;
 state.flowLoading.add(id);
 try{
  const d=await request(`/api/workflow/${id}`);
  if(state.active!==id||state.tab!=='workflow'||$('#workflowView')!==box)return;
  const thread=makeConversation(id,d);rememberFlow();
  const previousScroll=state.flowScroll.get(id);
  const retainedImages=new Map();box.querySelectorAll('img[src]').forEach(img=>{if(img.complete&&img.naturalWidth)retainedImages.set(img.getAttribute('src'),img);});
  box.innerHTML=`${progressMarkup(j,d)}<section class="conversation"><div class="conversation-heading"><h4>Agent activity</h4><div class="conversation-controls"><span>Updates every 5 seconds</span><button id="flowRefresh">Refresh</button></div></div><p class="sr-only">Only saved agent outputs, images and label versions are shown. Internal reasoning is not recorded.</p><div class="conversation-feed" role="region" aria-label="Agent activity, scrollable" tabindex="0">${thread}</div></section>`;
  box.querySelectorAll('img[src]').forEach(img=>{const old=retainedImages.get(img.getAttribute('src'));if(old){img.replaceWith(old);retainedImages.delete(img.getAttribute('src'));}});box.dataset.loaded='true';state.flowVersions.set(id,d.version);
  const previous=state.flowOpen.get(id)||{};box.querySelectorAll('details[data-flow-key]').forEach(el=>{if(Object.hasOwn(previous,el.dataset.flowKey))el.open=previous[el.dataset.flowKey];});
  const feed=box.querySelector('.conversation-feed');let follow=!previousScroll||previousScroll.atBottom;feed.scrollTop=follow?feed.scrollHeight:previousScroll.top;
  for(const name of ['wheel','touchstart','pointerdown','keydown'])feed.addEventListener(name,()=>{follow=false;},{passive:true});
  feed.querySelectorAll('img').forEach(img=>img.addEventListener('load',()=>{if(follow&&feed.isConnected)feed.scrollTop=feed.scrollHeight;}));
  $('#flowRefresh').onclick=()=>loadWorkflow(id);
  box.querySelectorAll('.thread-images img').forEach(img=>{img.onerror=()=>{const f=img.closest('figure');if(f)f.insertAdjacentHTML('afterbegin','<p class="flow-note">The historical image is missing or no longer matches its record.</p>');img.remove();};});
 }catch(e){if($('#workflowView')===box&&!box.dataset.loaded)box.textContent='Could not load progress: '+e.message;}
 finally{state.flowLoading.delete(id);}
}
async function pollWorkflow(){const id=state.active,box=$('#workflowView');if(state.tab!=='workflow'||!id||!box||box.dataset.job!==id||state.flowLoading.has(id)||state.flowPolling.has(id))return;state.flowPolling.add(id);try{const d=await request(`/api/workflow-version/${id}`);if(state.active===id&&state.tab==='workflow'&&(!box.dataset.loaded||state.flowVersions.get(id)!==d.version))await loadWorkflow(id);}catch(e){}finally{state.flowPolling.delete(id);}}
async function refreshJobs(){try{const d=await request('/api/jobs');state.jobs=d.jobs;if(!state.jobs.some(j=>j.id===state.active)){state.active=d.jobs[0]?.id||null;state.tab=state.jobs[0]&&active(state.jobs[0])?'workflow':'overlay';state.detailKey=null;}renderTasks();renderDetail();pollWorkflow();}catch(e){$('#message').textContent='Cannot connect to local service: '+e.message;}}
async function start(){if(state.busy||!state.selected.size)return;state.busy=true;renderMaps();$('#message').textContent='Starting tasks...';try{const d=await request('/api/start',{method:'POST',headers:{'Content-Type':'application/json','X-UI-Token':state.token},body:JSON.stringify({maps:[...state.selected],workers:Number($('#workers').value),mode:$('#mode').value})});state.selected.clear();state.active=d.job_ids[0];state.tab='workflow';state.detailKey=null;$('#message').textContent=`Created ${d.job_ids.length} independent tasks.`;await refreshJobs();}catch(e){$('#message').textContent='Start failed or outcome unknown: '+e.message+'. Check the task list before retrying.';await refreshJobs();}finally{state.busy=false;renderMaps();}}
async function init(){try{const [session,catalog]=await Promise.all([request('/api/session'),request('/api/maps')]);state.token=session.token;state.maps=catalog.maps;renderRegions();renderMaps();await refreshJobs();setInterval(refreshJobs,5000);}catch(e){$('#message').textContent='Could not initialize interface: '+e.message;}}
$('#search').oninput=e=>{state.query=e.target.value.trim().toLowerCase();renderMaps();};$('#mode').onchange=()=>{
 $('#modeHelp').textContent=$('#mode').value==='max'
  ?'Generate the mask, review it, and revise it when needed.'
  :'Generate the first mask and overlay.';
};
$('#start').onclick=start;init();
