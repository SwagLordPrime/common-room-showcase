"use strict";
(function(root){
 class PendingJournal {
  constructor(storage){this.storage=storage;this.key='common-room-pending-v1';this.pending=null;this.instance=null;this.actor=null;}
  bind(instance,actor){
   this.pending=null;this.instance=instance;this.actor=actor;
   let saved;try{const raw=this.storage.getItem(this.key);saved=raw?JSON.parse(raw):null;}catch(e){this.storage.removeItem(this.key);return 'Unreadable pending action discarded. Inspect the task before acting again.';}
   if(saved&&(saved.version!==1||saved.instance!==instance||saved.actor!==actor||!['command','submit'].includes(saved.kind)||typeof saved.request!=='object'||!saved.request||typeof saved.request.request_id!=='string')){
    this.clear();return 'Pending action discarded because the room or participant changed. Inspect the saved task before acting again.';
   }
   this.pending=saved;return '';
  }
  signature(kind,request){const {request_id,ref,...content}=request;return JSON.stringify({kind,content});}
  begin(kind,request){
   if(!this.instance||!this.actor)throw Error('Sign in before saving an action.');
   if(this.pending){
    if(this.signature(kind,request)!==this.signature(this.pending.kind,this.pending.request))throw Error('Recover the pending action before starting another.');
    return this.pending;
   }
   const next={version:1,instance:this.instance,actor:this.actor,kind,request:JSON.parse(JSON.stringify(request))};
   this.storage.setItem(this.key,JSON.stringify(next));this.pending=next;return next;
  }
  update(change){const next=JSON.parse(JSON.stringify(this.pending));Object.assign(next.request,change);this.storage.setItem(this.key,JSON.stringify(next));this.pending=next;}
  clear(){this.pending=null;this.storage.removeItem(this.key);}
 }
 if(typeof module!=='undefined'&&module.exports)module.exports={PendingJournal};
 else root.PendingJournal=PendingJournal;
})(globalThis);
