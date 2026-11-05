"use strict";
const labels={draft:"Queued",delivered:"Delivered",acknowledged:"Ready",claimed:"Claimed",ready_for_review:"For review",done:"Done",waiting_decision:"Needs a decision"};
async function refreshStatus(){
 const freshness=document.getElementById("freshness");
 try{
  const response=await fetch("/api/status",{cache:"no-store",credentials:"omit"});
  if(!response.ok)throw new Error("Room status is unavailable");
  const state=await response.json();
  const rows=Object.entries(labels).map(([key,label])=>{const p=document.createElement("p");p.textContent=label+": "+(state.counts[key]||0);return p;});
  document.getElementById("counts").replaceChildren(...rows);
  document.getElementById("claims").textContent=state.active_claims+" current claims · "+state.expired_claims+" expired claims awaiting a next action";
  document.getElementById("receipt").textContent="Last recorded event: "+state.last_event_seq;
  freshness.textContent="Checked "+new Date(state.server_time*1000).toLocaleString();
 }catch(error){freshness.textContent="Stale or unavailable. Refresh when the room is running.";}
}
document.getElementById("refresh-status").onclick=refreshStatus;
setInterval(()=>{if(!document.hidden)refreshStatus();},15000);refreshStatus();
