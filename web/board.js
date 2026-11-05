"use strict";

const $=id=>document.getElementById(id);let state=null,busy=false,pending=null,lastSuccess=0,plansLoaded=false;

const el=(tag,text,cls)=>{const x=document.createElement(tag);if(text!==undefined)x.textContent=text;if(cls)x.className=cls;return x;};

function message(text=''){$('message').textContent=text;}

// The roster: a Greek-letter sigil and a plain-English kind per seat (display only; the room
// decides who may do what). Only letters that cannot pass for Latin ones: Ω the owner has the last
// word; Σ gathers and leads (syn-); Π builds (Prometheus, who gave craft); Δ judges (dikastes);
// Λ writes (logos); Γ carries letters (grammata, for Hermes); Φ keeps company (philos); Θ watches.
const SEATS={keeper:['Ω','Owner','The last word'],'claude-room':['Σ','Orchestrator','Plans and leads'],ferrule:['Φ','Claude chat','A companion chat'],
 tinker:['Δ','Reviewer','Judges the work'],vesper:['Δ','Reviewer','Judges the work'],'crew-audit':['Δ','Reviewer','Judges the work'],
 hermes:['Γ','Hermes','The messenger'],'local-a':['Λ','Local model','A scribe on your GPU'],'local-b':['Λ','Local model','A scribe on your GPU'],
 'astra-room':['Π','Astra builder','Builds under house law'],'astra-audit':['Δ','Astra reviewer','Judges the work'],
 'claude-code':['Π','Claude Code','Builds on the PC'],'claude-code-b':['Π','Claude Code','Builds on the PC']};
function seatOf(p){return SEATS[p.id]||(p.role==='owner'?['Ω','Owner','The last word']:p.role==='reviewer'?['Δ','Reviewer','Judges the work']:p.role==='worker'?['Π','Worker','Does the work']:['Θ','Observer','Watches']);}

// At a glance: one tile per state that matters, each a shortcut to that filter.
// The roster: seated members first (owner, orchestrator, workers, reviewers), retired historical
// identities folded away at the end. The contact line is the room's own words, coloured by state.
// board.py's wordings: "Credential contact active; ...", "Fixture browser active", "Browser active; not a
// provider connection" (live); "... stale ...", "... key inactive" (stale); "Key issued; handshake pending",
// "Configured; ...", "Provider result recorded" (pending); "No active key", "No ... contact" (off).
function contactState(text){text=text||'';return /stale|inactive/i.test(text)?'stale':/^no\b/i.test(text)?'off':/\bactive\b/i.test(text)?'live':/pending|configured|recorded/i.test(text)?'pending':'off';}
function renderRoster(){
 const rank=p=>p.role==='owner'?0:p.id==='claude-room'?1:p.role==='worker'?2:p.role==='reviewer'?3:4;
 const person=p=>{const n=el('div',undefined,'person');const [sigil,kind,epithet]=seatOf(p);const live=contactState(p.contact);n.dataset.state=live;const mark=el('span',sigil,'sigil');mark.title=epithet;const text=el('div',undefined,'person-text');text.append(el('span',kind,'kind'),el('strong',p.label||p.id),el('small',[p.id,p.role,p.session?'declared session '+p.session:''].filter(Boolean).join(' · ')),el('span',p.contact,'contact '+live));n.append(mark,text);return n;};
 const seated=state.participants.filter(p=>!p.legacy).sort((a,b)=>rank(a)-rank(b)||String(a.label||a.id).localeCompare(String(b.label||b.id)));
 const retired=state.participants.filter(p=>p.legacy);
 const grid=el('div',undefined,'roster');grid.append(...seated.map(person));const parts=[grid];
 const prior=$('participants').querySelector?.('details.retired'),wasOpen=!!(prior&&prior.open);
 if(retired.length){const fold=el('details',undefined,'retired');fold.open=wasOpen;fold.append(el('summary','Retired seats ('+retired.length+') · '+[...new Set(retired.map(p=>p.contact||'No contact'))].join(' · ')));const list=el('div',undefined,'roster');list.append(...retired.map(person));fold.append(list);parts.push(fold);}
 $('participants').replaceChildren(...parts);
 const count=$('roster-count');if(count){const live=seated.filter(p=>contactState(p.contact)==='live').length;count.textContent=seated.length+(seated.length===1?' seat':' seats')+' · '+live+' checked in this minute';}
}

function renderSummary(blocked){
 const count=f=>state.tasks.filter(f).length,owner=state.actor.role==='owner';
 const tiles=[['chip-decisions',count(blocked),owner?'Awaiting your word':'Waiting on a decision','Questions and stopped work','needs'],
  ['chip-review',count(t=>t.status==='ready_for_review'),'Before the judges','Results waiting for review','ready_for_review'],
  ['chip-working',count(t=>t.status==='claimed'),'At the forge','Claimed and in progress','claimed'],
  ['chip-inbox',count(t=>['draft','delivered','acknowledged'].includes(t.status)),'Arrivals','New or not started','arrivals'],
  ['chip-done',count(t=>t.status==='done'),'Laurels','Accepted and done','done']];
 $('summary').replaceChildren(...tiles.map(([cls,n,label,hint,filter])=>{const b=el('button',undefined,'chip '+cls+(n?'':' calm'));b.type='button';b.setAttribute('aria-pressed',String($('filter-status').value===filter));b.append(el('b',String(n)),el('span',label),el('small',hint));b.onclick=()=>{$('filter-status').value=filter;render();};return b;}));
}

async function api(path,payload){const response=await fetch(path,{method:payload===undefined?'GET':'POST',headers:payload===undefined?{}:{'Content-Type':'application/json'},credentials:'same-origin',body:payload===undefined?undefined:JSON.stringify(payload)});const result=await response.json();if(!response.ok){const e=Error(result.message||result.error);e.status=response.status;throw e;}return result;}

function signedOut(){$('workspace').hidden=true;$('login-panel').hidden=false;state=null;pendingRun=null;$('recover-run').hidden=true;plansLoaded=false;usageAt=0;$('usage-panel').hidden=true;$('plans-panel').hidden=true;$('plans').replaceChildren();$('plans-result').replaceChildren();}

async function load(){try{state=await api('/api/state');lastSuccess=Date.now();$('identity').className='';const notice=journal.bind(state.instance_id,state.actor.id);if(notice)message(notice);showPending();$('login-panel').hidden=true;$('workspace').hidden=false;render();showPlansPanel();loadUsage();}catch(e){if(e.status===401)signedOut();else {message('Disconnected: showing the last saved view. '+e.message);$('updated').textContent='Disconnected · last successful refresh is stale';$('identity').className='stale';}}}

const journal=new PendingJournal(sessionStorage);

const recoverAction=el('button','Recover pending action','secondary');recoverAction.id='recover-action';recoverAction.hidden=true;recoverAction.type='button';$('message').after(recoverAction);

function showPending(){recoverAction.hidden=!journal.pending;recoverAction.disabled=busy;if(journal.pending)recoverAction.textContent='Recover pending '+(journal.pending.kind==='submit'?'result submission':journal.pending.request.action);}

async function performPending(){

 if(busy||!journal.pending)return false;busy=true;showPending();

 try{

  let saved=journal.pending;

  if(saved.kind==='submit'){

   if(!saved.request.ref){

    const r=saved.request,ref=await api('/api/handoff',{operation:'stage_text',payload:{task_id:r.task_id,fence:r.fence,text:r.text}});

    journal.update({ref});saved=journal.pending;

   }

   const r=saved.request;await api('/api/command',{action:'finish',payload:{task_id:r.task_id,fence:r.fence,body:r.body,refs:[r.ref]},request_id:r.request_id});

  }else await api('/api/command',saved.request);

  journal.clear();message('Action saved.');await load();return true;

 }catch(e){

  if(e.status&&e.status<500&&e.status!==401)journal.clear();

  if(e.status===401)signedOut();

  message(e.message+(journal.pending?' Sign in if needed, then use Recover pending action to retry the original request.':' Refresh the task before trying a changed action.'));

  return false;

 }finally{busy=false;showPending();}

}

async function command(action,payload){

 if(busy)return false;

 try{journal.begin('command',{action,payload,request_id:crypto.randomUUID()});return await performPending();}

 catch(e){message(e.message);return false;}

}

async function submitText(task_id,fence,text,body){

 if(busy)return false;

 try{journal.begin('submit',{task_id,fence,text,body,request_id:crypto.randomUUID()});return await performPending();}

 catch(e){message(e.message);return false;}

}

recoverAction.onclick=()=>{if(state)performPending();else message('Sign in as the same participant to recover the pending action.');};

function selectedAuthority(){return state?.authorities.find(a=>a.id===$('authority').value);}

function button(parent,label,action,payload){const b=el('button',label,'secondary');b.type='button';b.onclick=()=>command(action,typeof payload==='function'?payload():payload);parent.append(b);}

function cardForm(parent,title,fields,submit,labels={}){

 const open=el('button',title,'secondary');open.type='button';const form=el('form',undefined,'card-form');form.hidden=true;const inputs={};

 for(const f of fields){const label=el('label',f.label),input=el(f.options?'select':f.multiline?'textarea':'input');if(f.options)for(const o of f.options){const option=el('option',o.label);option.value=o.value;input.append(option);}input.id='field-'+crypto.randomUUID();label.htmlFor=input.id;input.required=true;input.maxLength=f.limit;if(f.pattern)input.pattern=f.pattern;inputs[f.name]=input;form.append(label,input);if(f.count){const count=el('small','0 / '+f.limit+' characters');input.addEventListener('input',()=>{count.textContent=input.value.length+' / '+f.limit+' characters';});form.append(count);}}

 const save=el('button',labels.save||'Save'),cancel=el('button',labels.close||'Cancel','secondary');save.type='submit';cancel.type='button';form.append(save,cancel);

 open.onclick=()=>{form.hidden=false;form.dataset.open='true';Object.values(inputs)[0].focus();};cancel.onclick=()=>{form.hidden=true;delete form.dataset.open;};

 form.onsubmit=async e=>{e.preventDefault();try{await submit(Object.fromEntries(Object.entries(inputs).map(([k,v])=>[k,v.value])));}catch(error){message(error.message);}};

 parent.append(open,form);

}

// a2b-mark-begin: the undoable mark (A2 D10, A2b). Pure functions only: tests/test_a2b_undoable_mark.py cuts this block out and runs it in node.
const MARK_CHOICES=[{value:'unmarked',label:'Not marked (counts as can\'t undo)'},{value:'true',label:'Undoable'},{value:'false',label:'Can\'t undo'}];
function markWords(undoable){return undoable===true?'Undoable':undoable===false?'Can\'t undo':'Not marked (counts as can\'t undo)';}
function markField(choice){return choice==='true'?{undoable:true}:choice==='false'?{undoable:false}:{};}
function isMarker(st,authorityId,actorId){return ((st&&st.authorities||[]).find(a=>a.id===authorityId)?.review_standins||[]).some(s=>(s.markers||[]).includes(actorId));}
function intakePayload(namespace,reference,msg,choice){return {source_namespace:namespace,external_reference:reference,message:msg,...markField(choice)};}
function helperPayload(taskId,fence,fields){const {mark,...rest}=fields;return {task_id:taskId,fence,...rest,...markField(mark)};}
// a2b-mark-end

function renderStop(){

 const stop=state.stop||{active:false},owner=state.actor.role==='owner',banner=$('stop-banner');

 banner.hidden=!stop.active;banner.textContent=stop.active?'THE ROOM IS STOPPED. Every authority is paused, so no AI can claim or finish work. '+(stop.by?'Stopped by '+stop.by+(stop.at?' at '+new Intl.DateTimeFormat('en-US',{timeZone:'America/Chicago',hour:'numeric',minute:'2-digit'}).format(new Date(stop.at*1000))+' CT':'')+'. ':'')+(stop.reason?'Reason: '+stop.reason:''):'';

 $('stop-all').hidden=!owner||stop.active;$('resume-all').hidden=!owner||!stop.active;if(stop.active)$('stop-form').hidden=true;

}

// Needs Shane (turnons panels): the list common_room/needs.py puts in /api/state as `needs`, first on the page. Text only.
const NEED_KINDS={stop:'Room stopped',stopped:'Stopped at two strikes',decision:'Your decision',question:'Question for you',seat:'Seat without a key',slow:'Waiting on claude-room'};
const ct=epoch=>new Intl.DateTimeFormat('en-US',{timeZone:'America/Chicago',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}).format(new Date(epoch*1000))+' CT';
function renderNeeds(){
 const items=Array.isArray(state.needs)?state.needs:[],owner=state.actor.role==='owner';
 $('needs-count').textContent=items.length?items.length+(items.length===1?' item':' items'):'';
 $('needs-empty').hidden=items.length>0;$('needs-empty').textContent=owner?'Nothing needs you.':'Nothing needs Shane.';
 $('needs-list').replaceChildren(...items.map(i=>{const li=el('li',undefined,'need need-'+i.kind);li.dataset.needId=i.id;
  li.append(el('span',NEED_KINDS[i.kind]||i.kind,'need-kind'),el('p',i.summary,'need-summary'),el('code',i.action,'need-action'));
  if(typeof i.since==='number')li.append(el('small','Since '+ct(i.since)));return li;}));
}

// Usage (turnons panels): owner only, from GET /api/usage, at most once a minute (the logs are files, not the room).
let usageAt=0;
const span=s=>s<90?Math.round(s)+' s':s<5400?Math.round(s/60)+' min':(s/3600).toFixed(1)+' h';
const tally=t=>t.runs+(t.runs===1?' run':' runs')+' · '+span(t.seconds)+(t.prompt_tokens===null&&t.output_tokens===null?'':' · '+((t.prompt_tokens||0)+(t.output_tokens||0)).toLocaleString('en-US')+' tokens');
function renderUsage(data){
 const seats=Array.isArray(data?.seats)?data.seats:[],out=[];
 if(!seats.length)out.push(el('p','No worker jobs recorded in the last 7 days.','muted'));
 else{const table=el('table',undefined,'usage-table'),head=el('tr');head.append(el('th','Seat'),el('th','Today'),el('th','Last 7 days'));table.append(head);
  for(const s of seats){const row=el('tr');row.append(el('td',s.seat+' ('+s.kind+')'),el('td',tally(s.today)),el('td',tally(s.week)));table.append(row);}out.push(table);}
 if(data?.skipped)out.push(el('p',data.skipped+' damaged usage line(s) skipped; crew-shane usage lists them.','warning'));
 $('usage').replaceChildren(...out);
}
async function loadUsage(force){
 if(state?.actor.role!=='owner'){$('usage-panel').hidden=true;return;}
 $('usage-panel').hidden=false;if(!force&&Date.now()-usageAt<60000)return;usageAt=Date.now();
 try{renderUsage(await api('/api/usage'));}catch(e){if(e.status===401)signedOut();else $('usage').replaceChildren(el('p','Usage could not be loaded: '+e.message,'warning'));}
}

function render(){

 $('identity').textContent=state.actor.id+' · '+state.actor.role+(state.fixture_only&&state.mode!=='handoff'?' fixture':'');$('updated').textContent='Refreshed '+new Intl.DateTimeFormat('en-US',{timeZone:'America/Chicago',hour:'numeric',minute:'2-digit',second:'2-digit'}).format(new Date())+' CT · browser clock';

 const connection=state.connection;

 $('connection-panel').hidden=!connection;

 if(connection){

   $('fixture-label').textContent=connection.mode==='codex'?(connection.blocked_reason?'Codex connection check pending':'Codex connection · explicit runs only'):'Offline connection test · no real model calls';

   $('connection-status').textContent=connection.blocked_reason?connection.blocked_reason:connection.busy?'A participant is running. This board stays available.':'Ready for an explicit run. Use the buttons on a delivered task or a result awaiting review.';

   $('walkthrough').replaceChildren(el('h2','A task, with a second pair of eyes'),el('p','Add a word-count task and Deliver it. Choose Run worker on its card, then Run reviewer when its result is ready. Each button starts at most one bounded run. Notes stay quiet.'));

   document.querySelector('.footer').textContent='Word-count scope only · Explicit runs · Durable receipts and separate review';

   $('connection-receipts').replaceChildren(...connection.recent.map(r=>el('p',r.role+' · '+r.status+(r.error?' · '+r.error:'')+(r.message?' · '+r.message:''))));

 }

 if(state.mode==='handoff'){

   $('fixture-label').textContent='Local handoffs · Shared work for the sessions you start';

   $('walkthrough').replaceChildren(el('h2','One request. Clear ownership.'),el('p','Queue a handoff with its original request reference. Deliver it when its scope is ready. A participant must acknowledge and claim it before work is shown as owned. A claimed task can ask a helper for a separate piece. A separate reviewer checks each result.'),el('p','Assignment does not confirm delivery to an assistant. Use this browser or the explicit participant client. Open saved reports before reviewing. Check replies before work and at checkpoints. Browser refresh keeps the login connected, up to the key expiry. Closed assistants are not awakened.'));

   document.querySelector('.footer').textContent='Local coordination · Explicit participant actions · Separate review';

 }

 const previous=$('authority').value;$('authority').replaceChildren(...state.authorities.map(a=>{const o=el('option',a.id+(a.revoked?' · revoked':a.paused?' · paused':''));o.value=a.id;return o;}));if(state.authorities.some(a=>a.id===previous))$('authority').value=previous;

 $('scope').textContent=selectedAuthority()?.scope||'No scope available';const assigned=$('recipient').value;$('recipient').replaceChildren(...state.participants.filter(p=>p.role==='worker'&&!p.legacy&&(!selectedAuthority()?.members||selectedAuthority().members.includes(p.id))).map(p=>{const o=el('option',p.label||p.id);o.value=p.id;return o;}));if([...$('recipient').options].some(o=>o.value===assigned))$('recipient').value=assigned;

 updateHandoffForm();

 $('pause').hidden=state.actor.role!=='owner';$('pause').textContent=selectedAuthority()?.paused?'Resume work':'Pause work';

 renderStop();

 renderNeeds();

 renderRoster();

 function options(id,values){const previous=$(id).value;$(id).replaceChildren(el('option','All'));$(id).firstChild.value='';for(const [value,label] of values){const o=el('option',label);o.value=value;$(id).append(o);}$(id).value=previous;}

 options('filter-recipient',state.participants.filter(p=>p.role==='worker').map(p=>[p.id,p.id]));options('filter-scope',state.authorities.map(a=>[a.id,a.id+' · '+a.scope]));

 const blocked=t=>{const a=state.authorities.find(a=>a.id===t.authority_id);return t.status==='waiting_decision'||(!['done','cancelled'].includes(t.status)&&(a?.paused||a?.revoked));};

 renderSummary(blocked);

 const arriving=t=>['draft','delivered','acknowledged'].includes(t.status);
 const query=$('search').value.trim().toLowerCase();const visible=state.tasks.filter(t=>(!query||[t.title,t.body,t.id,t.decision,t.decision_response,t.cancel_note].filter(Boolean).join(' ').toLowerCase().includes(query))&&(!$('filter-status').value||($('filter-status').value==='needs'?blocked(t):$('filter-status').value==='arrivals'?arriving(t):t.status===$('filter-status').value))&&(!$('filter-recipient').value||t.recipient===$('filter-recipient').value)&&(!$('filter-scope').value||t.authority_id===$('filter-scope').value));

 $('filter-count').textContent=visible.length+' of '+state.tasks.length+' tasks shown';

 const columns=[['Inbox',['draft','delivered','acknowledged'],'Arrivals'],['Working',['claimed'],'At the forge'],['Review',['ready_for_review'],'Before the judges'],['Done',['done','cancelled'],'Laurels'],['Owner decisions',['waiting_decision'],'Awaiting your word']];

 $('board').replaceChildren(...columns.map(([name,statuses,epithet])=>{const col=el('section',undefined,'column');col.dataset.column=name;const items=visible.filter(t=>statuses.includes(t.status));const h=el('h2',name);h.append(el('b',String(items.length)));h.setAttribute('aria-label',name);col.append(h,el('p',epithet,'epithet'));if(!items.length)col.append(el('p','Quiet here for now.','empty'));

 for(const t of items){const c=el('article',undefined,'card');c.append(el('span',t.kind==='handoff.record'&&t.status==='draft'?'queued':t.status,'task-status'),el('h3',t.title),el('p',t.body),el('small',t.sender+' → '+t.recipient));c.dataset.taskId=t.id;const a=state.authorities.find(a=>a.id===t.authority_id);c.append(el('small','Scope: '+t.authority_id));c.append(el('small','Mark: '+markWords(t.undoable),'task-mark'));if(t.tier||t.effort)c.append(el('small','Tier: '+(t.tier||'-')+' · effort '+(t.effort||'-')+(t.tier_mismatch?' · '+t.recipient+' is labelled '+t.tier_mismatch:''),t.tier_mismatch?'warning':undefined));if(t.intake)c.append(el('small','Request: '+t.intake.source_namespace+' / '+t.intake.external_reference));if(a?.revoked||a?.paused)c.append(el('p',a.revoked?'Scope revoked · work refused':'Scope paused · waiting for owner resume','warning'));if(t.lease&&t.lease.until<=state.server_time)c.append(el('p','Lease expired · no current worker claim','warning'));if(t.decision)c.append(el('p','Needs Shane: '+t.decision,'warning'));if(t.decision_response)c.append(el('p','Recorded decision: '+t.decision_response));if(t.review_note)c.append(el('p','Review: '+t.review_note));if(t.result_body)c.append(el('p',t.result_body));if(t.status==='cancelled')c.append(el('p','Cancelled by '+t.cancelled_by+': '+t.cancel_note,'warning'));

 const detail=el('details'),summary=el('summary','Acceptance & artifacts');detail.append(summary,el('p',t.acceptance));for(const ref of t.result_refs||[]){

 detail.append(el('p',ref.path),el('code',ref.sha256,'digest'));

 if(t.kind==='handoff.record'){

  const view=el('button','Read saved report','secondary');view.type='button';

  view.onclick=async()=>{try{const report=await api('/api/handoff',{operation:'view_text',payload:{task_id:t.id,...ref}});detail.append(el('p','Verified saved report  /  SHA-256 '+report.sha256),el('pre',report.text,'receipt'));view.disabled=true;detail.open=true;}catch(e){message(e.message);}};detail.append(view);

 }

};c.append(detail);const history=el('details');history.append(el('summary','Durable event receipts'));for(const e of state.history||[]){if(e.task_id!==t.id)continue;const item=el('div',undefined,'receipt');item.append(el('p','#'+e.seq+' · '+e.kind+' · '+e.actor),el('p',e.detail||''),el('code',e.sha256,'digest'));if(e.review_kind||e.paperwork_returns)item.insertBefore(el('p','Review kind: '+(e.review_kind||'substance')+(e.paperwork_returns?' · paperwork returns on this task: '+e.paperwork_returns:'')),item.lastChild);if(e.refund)item.insertBefore(el('p','Strike refunded by '+e.refund.by+': '+e.refund.strikes_before+' to '+e.refund.strikes_after+'. Reason: '+e.refund.reason),item.lastChild);history.append(item);}c.append(history);

 if(t.kind==='document.count'&&connection?.can_run&&t.owner===state.actor.id&&t.recipient===connection.worker&&!a?.paused&&!a?.revoked){

   const role=['delivered','acknowledged','claimed'].includes(t.status)?'worker':t.status==='ready_for_review'?'reviewer':null;

   if(role){const run=el('button',role==='worker'?'Run worker':'Run reviewer');run.type='button';run.disabled=connection.busy;run.onclick=()=>runParticipant(t.id,role);c.append(run);}

 }

 if(t.status==='draft' &&(state.actor.id===t.sender||state.actor.id===t.owner))button(c,'Deliver','deliver',{task_id:t.id});

 if(t.recipient===state.actor.id&&t.status==='delivered')button(c,'Acknowledge','ack',{task_id:t.id});

 if(t.recipient===state.actor.id&&t.status==='acknowledged')button(c,'Claim','claim',{task_id:t.id,seconds:1800});

 if(t.kind==='handoff.record'){

   const exportButton=el('button','View receipt','secondary');exportButton.type='button';exportButton.onclick=async()=>{try{const receipt=await api('/api/handoff',{operation:'receipt',payload:{task_id:t.id}});const pre=el('pre',JSON.stringify(receipt,null,2),'receipt');c.append(pre);exportButton.disabled=true;}catch(e){message(e.message);}};c.append(exportButton);

   if(t.status==='claimed'&&t.lease?.holder===state.actor.id&&t.lease.until>state.server_time){

     button(c,'Renew claim','renew',{task_id:t.id,fence:t.lease.fence,seconds:1800});
     const candidates=state.participants.filter(p=>p.role==='worker'&&p.id!==state.actor.id&&(state.authorities.find(a=>a.id===t.authority_id)?.members||[]).includes(p.id));
     if(candidates.length&&t.chain_depth<4)cardForm(c,'Ask a helper',[{name:'recipient',label:'Helper',options:candidates.map(p=>({value:p.id,label:p.label||p.id}))},{name:'title',label:'Helper assignment',limit:120},{name:'body',label:'Separate piece of work within this project',limit:4000,multiline:true},{name:'acceptance',label:'What the helper must return',limit:2000,multiline:true},...(isMarker(state,t.authority_id,state.actor.id)?[{name:'mark',label:'Can this work be undone?',options:MARK_CHOICES}]:[])],fields=>command('request_help',helperPayload(t.id,t.lease.fence,fields)));


     cardForm(c,'Write result in browser',[{name:'text',label:'Full report (up to 12000 UTF-8 bytes)',limit:12000,multiline:true},{name:'body',label:'Completion summary',limit:4000,multiline:true}],({text,body})=>submitText(t.id,t.lease.fence,text,body));

     cardForm(c,'Submit result',[{name:'path',label:'Relative result path in this room',limit:240},{name:'sha256',label:'Result SHA-256',limit:64,pattern:'[0-9a-f]{64}'},{name:'body',label:'Completed work',limit:4000,multiline:true}],async({path,sha256,body})=>{const ref=await api('/api/handoff',{operation:'import',payload:{task_id:t.id,fence:t.lease.fence,path,sha256}});await command('finish',{task_id:t.id,fence:t.lease.fence,body,refs:[ref]});});

   }

 }

 if(t.parent_id)c.append(el('p','Helping task '+t.parent_id));
 if(t.status==='ready_for_review'&&state.actor.role==='reviewer'&&state.actor.id!==t.builder){

 const review=el('form',undefined,'card-form');review.hidden=true;let accepted=true;

 const label=el('label','Review note'),note=el('textarea');note.id='review-note-'+t.id;label.htmlFor=note.id;note.required=true;note.maxLength=2000;

 const save=el('button','Save review'),cancel=el('button','Cancel','secondary');save.type='submit';cancel.type='button';cancel.onclick=()=>{review.hidden=true;delete review.dataset.open;};

 const count=el('small','0 / 2000 characters');note.addEventListener('input',()=>{count.textContent=note.value.length+' / 2000 characters';});const paperLabel=el('label','This return is paperwork only (the work is right; the report or evidence is not). It costs no strike until the third one.','paperwork-choice'),paper=el('input');paper.type='checkbox';paper.id='review-paperwork-'+t.id;paperLabel.htmlFor=paper.id;paperLabel.prepend(paper);paperLabel.hidden=true;review.append(label,note,count,paperLabel,save,cancel);review.onsubmit=async e=>{e.preventDefault();if(note.value.trim()){const body={task_id:t.id,accepted,note:note.value};if(!accepted&&paper.checked)body.kind='paperwork';await command('review',body);}};

 for(const verdict of [true,false]){const b=el('button',verdict?'Accept':'Return for decision','secondary');b.type='button';b.onclick=()=>{accepted=verdict;review.hidden=false;review.dataset.open='true';save.textContent=verdict?'Save acceptance':'Return with note';paperLabel.hidden=verdict;if(verdict)paper.checked=false;note.focus();};c.append(b);}c.append(review);

 }

 const owns=state.actor.id===t.owner,delegated=t.intake?.source_namespace==='room-helper'&&state.actor.id===t.sender;
 if(t.status==='waiting_decision'&&(owns||delegated)){cardForm(c,owns?'Record decision':'Answer question',[{name:'body',label:owns?'Decision within the approved scope':'Answer for this helper',limit:4000,multiline:true}],({body})=>command('resolve_decision',{task_id:t.id,body}));}
 if(t.failed_attempts>=1&&t.status!=='cancelled'&&state.actor.id===t.owner&&!(t.lease&&t.lease.until>state.server_time)){cardForm(c,'Reinstate task ('+t.failed_attempts+' recorded failure'+(t.failed_attempts===1?'':'s')+')',[{name:'body',label:'Why this task is going back to work',limit:4000,multiline:true}],({body})=>command('reinstate',{task_id:t.id,body}));}
 if(!['done','cancelled'].includes(t.status)&&(owns||delegated))cardForm(c,'Cancel task',[{name:'body',label:'Why this task is being cancelled (it cannot be undone)',limit:2000,multiline:true,count:true}],({body})=>command('cancel',{task_id:t.id,body}),{save:'Cancel this task',close:'Keep task'});
 col.append(c);}return col;}));

 $('notes').replaceChildren(...state.notes.map(note=>{const n=el('div',undefined,'note');n.append(el('strong',note.sender),el('p',note.body));if(note.timestamp!==undefined){const receipt=el('small',new Date(note.timestamp*1000).toLocaleString()+' · receipt '+note.seq);receipt.title='Event SHA-256: '+note.sha256;n.append(receipt);}return n;}));

}

$('login-form').onsubmit=async e=>{e.preventDefault();try{await api('/api/login',{token:$('access-key').value});$('access-key').value='';message();await load();}catch(error){message(error.message);}};

function updateHandoffForm(){

 const handoff=selectedAuthority()?.capabilities?.includes('handoff.record');$('handoff-fields').hidden=!handoff;$('external-reference').required=!!handoff;

 const prior=$('source-namespace').value;$('source-namespace').replaceChildren(...(selectedAuthority()?.source_namespaces||[]).map(n=>{const o=el('option',n);o.value=n;return o;}));if([...$('source-namespace').options].some(o=>o.value===prior))$('source-namespace').value=prior;

 $('task-submit').textContent=handoff?'Queue handoff':'Add draft task';$('task-submit').disabled=(!!handoff&&state.actor.role!=='owner')||!$('recipient').options.length;

 if(handoff&&$('acceptance').value==='A reproducible word count, with a checked result.')$('acceptance').value='A saved result and separate review against the stated scope.';

}

$('task-form').onsubmit=async e=>{e.preventDefault();if(!selectedAuthority())return;const handoff=selectedAuthority().capabilities?.includes('handoff.record');const msg={title:$('task-title').value,body:$('task-body').value,recipient:$('recipient').value,authority_id:$('authority').value,kind:handoff?'handoff.record':'document.count',acceptance:$('acceptance').value};const ok=await command(handoff?'intake':'create',handoff?intakePayload($('source-namespace').value,$('external-reference').value,msg,$('task-mark').value):msg);if(ok){$('task-title').value='';$('task-body').value='';$('task-mark').value='unmarked';}};

$('note-form').onsubmit=async e=>{e.preventDefault();if(await command('note',{authority_id:$('authority').value,body:$('note-body').value}))$('note-body').value='';};

$('refresh').onclick=load;$('authority').onchange=()=>{render();updateHandoffForm();$('scope').textContent=selectedAuthority()?.scope||'';$('pause').textContent=selectedAuthority()?.paused?'Resume work':'Pause work';};

$('stop-all').onclick=()=>{$('stop-form').hidden=false;$('stop-confirm').value='';$('stop-go').disabled=true;$('stop-confirm').focus();};

$('stop-confirm').oninput=()=>{$('stop-go').disabled=$('stop-confirm').value!=='STOP';};

$('stop-cancel').onclick=()=>{$('stop-form').hidden=true;};

$('stop-form').onsubmit=async e=>{e.preventDefault();if($('stop-confirm').value!=='STOP')return;if(await command('stop_all',{reason:'Paused from the board by the keeper.'}))$('stop-form').hidden=true;};

$('resume-all').onclick=()=>command('resume_all',{});

$('pause').onclick=()=>{const a=selectedAuthority();if(a)command(a.paused?'resume':'pause',{authority_id:a.id});};

$('logout').onclick=async()=>{try{await api('/api/logout',{});signedOut();showPending();}catch(e){message(e.message);}};

setInterval(()=>{if(state&&!document.hidden&&!busy&&!document.querySelector('.card-form[data-open], .card details[open]'))load();},15000);load();



for(const id of ['search','filter-status','filter-recipient','filter-scope'])$(id).addEventListener('input',()=>{if(state)render();});

$('needs-shane').onclick=()=>{$('filter-status').value='needs';render();};

$('usage-refresh').onclick=()=>loadUsage(true);

$('clear-filters').onclick=()=>{for(const id of ['search','filter-status','filter-recipient','filter-scope'])$(id).value='';render();};

setInterval(()=>{if(state&&Date.now()-lastSuccess>45000){$('updated').textContent='Stale view · refresh to confirm current state';$('identity').className='stale';}},5000);



let pendingRun=null;

async function runParticipant(task_id,role){

 if(busy)return;const signature=JSON.stringify({task_id,role});

 if(pendingRun&&pendingRun.signature!==signature){message('Retry the previous run request to recover its receipt first.');return;}

 pendingRun=pendingRun||{signature,payload:{task_id,role,request_id:crypto.randomUUID()}};busy=true;

 try{await api('/api/run',pendingRun.payload);pendingRun=null;message('Run recorded. Its result will appear here.');await load();}

 catch(e){if(e.status&&e.status<500)pendingRun=null;message(e.message+'. Retry the same button if its response was lost.');}

 finally{busy=false;$('recover-run').hidden=!pendingRun;}

}

$('recover-run').onclick=()=>{if(pendingRun)runParticipant(pendingRun.payload.task_id,pendingRun.payload.role);};

setInterval(()=>{if(state?.connection?.busy&&!busy&&!document.hidden)load();},1000);


// Owner-only Plans panel (2026-09-24, autonomy build B). The room checks the role, the file's
// SHA-256 and the typed confirmation again on every request; this panel only shows the whole
// file and posts {id, sha256, confirm}. It is loaded once per sign-in and on its own Refresh
// button, never by the 15-second board refresh, so a half-typed confirmation is never wiped.


function showPlansPanel(){

 if(state?.actor.role!=='owner'){$('plans-panel').hidden=true;return;}

 $('plans-panel').hidden=false;if(!plansLoaded){plansLoaded=true;loadPlans();}

}

async function loadPlans(){

 if(state?.actor.role!=='owner'){$('plans-panel').hidden=true;return;}

 try{renderPlans(await api('/api/plans'));}

 catch(e){if(e.status===401)signedOut();else $('plans').replaceChildren(el('p','Plans could not be loaded: '+e.message,'warning'));}

}

function confirmForm(labelText,buttonText,expected,send,done){

 const form=el('form',undefined,'plan-confirm'),label=el('label',labelText),input=el('input'),button=el('button',buttonText),note=el('div');

 input.id='confirm-'+crypto.randomUUID();label.htmlFor=input.id;input.autocomplete='off';input.maxLength=200;input.required=true;button.type='submit';

 form.append(label,input,button,note);

 form.onsubmit=async e=>{

  e.preventDefault();

  if(input.value!==expected){note.replaceChildren(el('p','Type exactly "'+expected+'". Nothing was sent.','warning'));return;}

  if(busy)return;busy=true;button.disabled=true;

  try{const result=await send(input.value);$('plans-result').replaceChildren(el('strong',done),...(result.lines||[]).map(line=>el('p',line)));message(done);await load();await loadPlans();}

  catch(error){if(error.status===401)signedOut();note.replaceChildren(el('p',error.message+' Nothing further was sent.','warning'));}

  finally{busy=false;button.disabled=false;}

 };

 return form;

}

function planCard(p){

 const card=el('article',undefined,'plan-card');card.dataset.fileId=p.id;card.append(el('p','File '+p.id,'eyebrow'));

 if(p.error){card.append(el('p',p.error,'warning'));if(p.sha256)card.append(el('code',p.sha256,'digest'));return card;}

 card.append(el('h4',p.plan_id+' · '+p.title),el('p','Scope '+p.scope+' · '+p.tasks.length+' task'+(p.tasks.length===1?'':'s')+(p.in_room?' · '+p.in_room+' already in the room':'')));

 card.append(el('small','File SHA-256, checked again when you approve'),el('code',p.sha256,'digest'));

 if(p.approved)card.append(el('p','A receipt sits beside this file, but not every task is in the room yet.','warning'));

 for(const t of p.tasks){

  const box=el('div',undefined,'plan-task');box.append(el('strong','['+t.key+'] → '+t.to+': '+t.title));

  for(const line of t.body.split(/\r?\n/))if(line.startsWith('INPUT:'))box.append(el('code',line,'plan-input'));

  box.append(el('p','Acceptance: '+t.acceptance));

  box.append(el('p','Mark: '+markWords(t.undoable),'plan-mark'));

  const envelope=(p.envelope||[]).find(line=>line.startsWith('RESERVE ['+t.key+']'));if(t.reserve&&envelope)box.append(el('p',envelope,'plan-reserve'));

  const full=el('details');full.append(el('summary','Full task text'),el('pre',t.body,'receipt'));box.append(full);

  card.append(box);

 }

 for(const line of p.envelope||[])if(!line.startsWith('RESERVE ['))card.append(el('p',line,'plan-reserve'));

 for(const problem of p.problems)card.append(el('p','Problem: '+problem,'warning'));

 for(const key of p.conflicts)card.append(el('p','Conflict: task "'+key+'" was already approved with different content. It needs a new key or a new plan id.','warning'));

 if(p.problems.length||p.conflicts.length){card.append(el('p','This plan cannot be approved until the orchestrator fixes it.','warning'));return card;}

 card.append(confirmForm('Type the plan id to approve','Approve',p.plan_id,typed=>api('/api/plan-approve',{id:p.id,sha256:p.sha256,confirm:typed}),'Plan '+p.plan_id+' approved.'));

 return card;

}

function decisionsCard(d){

 const card=el('article',undefined,'plan-card');card.dataset.fileId=d.id;card.append(el('p','File '+d.id,'eyebrow'));

 if(d.error){card.append(el('p',d.error,'warning'));if(d.sha256)card.append(el('code',d.sha256,'digest'));return card;}

 card.append(el('small','File SHA-256, checked again when you apply'),el('code',d.sha256,'digest'));

 d.entries.forEach((entry,i)=>{

  const box=el('div',undefined,'plan-task');box.append(el('strong',(i+1)+'. '+entry.action+' · '+(entry.task_id||entry.task)));

  box.append(entry.error?el('p','Cannot be applied: '+entry.error,'warning'):el('p','Current question: '+(entry.question||'(none)')+' · status '+entry.status));

  box.append(el('p','Your answer: '+entry.text));card.append(box);

 });

 card.append(confirmForm('Type apply','Apply','apply',typed=>api('/api/decisions-apply',{id:d.id,sha256:d.sha256,confirm:typed}),'Answers in '+d.id+' applied.'));

 return card;

}

function renderPlans(data){

 const parts=[];

 if(!data||data.configured!==true){$('plans').replaceChildren(el('p','No plans folder is set for this room, so there is nothing to show here. The Command Prompt approve line still works.','muted'));return;}

 if(!data.root_exists)parts.push(el('p','The plans folder does not exist yet. Plans appear here once the orchestrator writes one.','muted'));

 for(const note of data.truncated||[])parts.push(el('p',note,'warning'));

 for(const r of data.refused||[])parts.push(el('p','Refused: '+r.id+' · '+r.reason,'warning'));

 const plans=data.plans||[],decisions=data.decisions||[];

 const waiting=plans.filter(p=>p.error||!p.approved||p.in_room<p.tasks.length),approved=plans.filter(p=>!waiting.includes(p));

 const open=decisions.filter(d=>d.error||!d.applied),applied=decisions.filter(d=>!open.includes(d));

 parts.push(el('h3','Plans waiting for you ('+waiting.length+')'));if(!waiting.length)parts.push(el('p','Nothing to approve.','muted'));parts.push(...waiting.map(planCard));

 parts.push(el('h3','Answers waiting for you ('+open.length+')'));if(!open.length)parts.push(el('p','Nothing to apply.','muted'));parts.push(...open.map(decisionsCard));

 if(approved.length){const d=el('details');d.append(el('summary','Approved plans ('+approved.length+')'),...approved.map(p=>el('p',p.id+' · '+p.plan_id+' · '+p.tasks.length+' task(s), receipt present')));parts.push(d);}

 if(applied.length){const d=el('details');d.append(el('summary','Applied answer files ('+applied.length+')'),...applied.map(x=>el('p',x.id+' · '+x.entries.length+' entr'+(x.entries.length===1?'y':'ies')+', receipt present')));parts.push(d);}

 $('plans').replaceChildren(...parts);

}

$('plans-refresh').onclick=()=>{$('plans-result').replaceChildren();loadPlans();};

// Effects switch (A7B, display only): the MyLife v3 look's scanlines, hazard march and masthead kanji glitch
// run only under html.fx-on. On by default; the choice is kept in localStorage commonroom.effects.
// The device's reduced-motion setting stops every animation in board.css.
(()=>{try{
 const root=document.documentElement;if(!root||!root.classList)return;
 const sw=$('fx-toggle');if(!sw)return;
 let on=true;try{on=localStorage.getItem('commonroom.effects')!=='off';}catch(e){}
 const show=()=>{root.classList.toggle('fx-on',on);sw.setAttribute('aria-pressed',on?'true':'false');sw.textContent=on?'Effects on':'Effects off';};
 show();
 sw.onclick=()=>{on=!on;try{localStorage.setItem('commonroom.effects',on?'on':'off');}catch(e){}show();};
}catch(e){}})();
