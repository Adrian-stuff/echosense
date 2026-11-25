import {ParentRepository, emptyState, newId, mergeSessions, createBackup, parseBackup, mergeBackup} from './parent-data.mjs';
import {progressReport, reportCSV} from './parent-progress.mjs';
import {PrivacyGate} from './parent-privacy.mjs';

const $=id=>document.getElementById(id);
const icons={home:'M3 10 12 3l9 7v10H3Z M9 20v-7h6v7',sound:'M4 10v4h4l5 4V6l-5 4Z M17 8q5 4 0 8 M17 4q10 8 0 16',video:'M3 6h12v12H3Z M15 10l6-3v10l-6-3',device:'M5 4h14v16H5Z M9 8h6 M10 16h4',settings:'M12 3v3 M12 18v3 M3 12h3 M18 12h3 M6 6l2 2 M16 16l2 2 M6 18l2-2 M16 8l2-2 M16 12a4 4 0 1 1-8 0 4 4 0 1 1 8 0',external:'M14 4h6v6 M20 4 10 14 M10 4H4v16h16v-6',play:'M8 4l12 8-12 8Z',camera:'M3 7h5l2-3h4l2 3h5v13H3Z M16 13a4 4 0 1 1-8 0 4 4 0 1 1 8 0',search:'M16 10a6 6 0 1 1-12 0 6 6 0 1 1 12 0 M15 15l6 6',shield:'M12 3 3 7v5q0 6 9 9 9-3 9-9V7Z M8 12l3 3 5-6',users:'M10 7a3 3 0 1 1-6 0 3 3 0 1 1 6 0 M2 20v-3q0-4 5-4t5 4v3 M16 4q6 0 3 6 M16 13q6 0 6 7'};
document.querySelectorAll('[data-icon]').forEach(el=>{el.innerHTML=`<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${icons[el.dataset.icon]||icons.device}"/></svg>`;});
const gate=new PrivacyGate($('private-app'),$('privacy-lock'),$('privacy-message'));
let account=null, household=emptyState(), clips=[], repository, saveChain=Promise.resolve();
let session=null, ownsConnection=false, pendingResponse=null, pendingFinish=false, ws=null, state=null;
let lastFrame=0,lastRgb=0,recorder=null,recordSave=null,recordStarted=0,clipUrls=[];
let clipDrafts=new Map(), selectedClips=new Set(), clipFilter='', recoveries=[], fittingStep=0, pendingBackup=null;
const pending=new Map(), decoder=new TextDecoder(), feed=$('feed'), context=feed.getContext('2d',{alpha:false});
let toastTimer, journalTimer, noteTimer, responseFlight=null, controlBusy=false;
const audio={on:false,ctx:null,next:0};
const can=permission=>!gate.locked&&account?.permissions.includes(permission);
const duration=s=>`${Math.floor(s/60)}:${String(Math.floor(s%60)).padStart(2,'0')}`;
const date=timestamp=>new Date(timestamp).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
const api=(path,body)=>window.echoSenseRequest(path,body);
const el=(tag,text,className)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node;};
const button=(text,fn,className='')=>{const node=el('button',text,className);node.type='button';node.onclick=fn;return node;};
function toast(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,5000);}
function empty(root,title,text){const box=el('div',undefined,'empty');box.append(el('strong',title),el('span',text));root.append(box);}
function download(blob,name){const url=URL.createObjectURL(blob),a=el('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),60000);}
function emergencyRead(){try{return JSON.parse(localStorage.getItem('echosense.parent.recovery')||'null');}catch{return null;}}
function emergencySave(){try{localStorage.setItem('echosense.parent.recovery',JSON.stringify({...household,clipDrafts:[...clipDrafts]}));return true;}catch{return false;}}
function journal(){
  household.updatedAt=Date.now();
  household.draft=session&&session.status!=='completed'?{sessionId:session.id,userId:account?.user_id,note:$('session-note').value,pendingResponse}:null;
  household.clipDrafts=[...clipDrafts];
  emergencySave();
  const snapshot=structuredClone(household);
  saveChain=saveChain.catch(()=>{}).then(()=>repository.saveState(snapshot));
  return saveChain;
}
function scheduleJournal(){
  household.updatedAt=Date.now();
  household.draft=session&&session.status!=='completed'?{sessionId:session.id,userId:account?.user_id,note:$('session-note').value,pendingResponse}:null;
  household.clipDrafts=[...clipDrafts];emergencySave();
  clearTimeout(journalTimer);journalTimer=setTimeout(()=>journal().catch(()=>{}),350);}
function rememberSession(record){household.sessions=mergeSessions(household.sessions,[record]);}
function fresh(){return !gate.locked&&Date.now()-lastFrame<3000&&ws?.readyState===WebSocket.OPEN;}
function viewFresh(){return fresh()&&($('view-select').value==='depth'||Date.now()-lastRgb<3000);}
function send(message){if(ws?.readyState===WebSocket.OPEN&&!gate.locked){ws.send(JSON.stringify(message));return true;}return false;}
function training(cmd,fields={}){
  const isControl=['start','target','resume','pause'].includes(cmd);
  if(isControl&&controlBusy)return Promise.reject(new Error('Wait for the device to confirm the previous action.'));
  const request_id=newId();
  const message={t:'training',cmd,request_id,...(session?{session_id:session.id}:{}),...fields};
  return new Promise((resolve,reject)=>{
    if(!can('train')||!send(message)){reject(new Error('Reconnect and sign in before controlling practice.'));return;}
    if(isControl){controlBusy=true;renderTraining();}
    const timer=setTimeout(()=>{pending.delete(request_id);if(isControl){controlBusy=false;renderTraining();}reject(new Error('No acknowledgement yet. Your answer is retained; reconnect and retry.'));},6000);
    pending.set(request_id,{resolve,reject,timer,cmd,isControl});
  });
}
function applyPermissions(){
  document.querySelectorAll('[data-permission]').forEach(node=>{node.hidden=!can(node.dataset.permission);});
  $('account-name').textContent=`${account.username} · ${account.role}`;
  $('logout').disabled=false;$('password-submit').disabled=false;
  $('profile-form').querySelectorAll('input,textarea,button').forEach(node=>node.disabled=!can('train'));
}
function page(name){
  const allowed=['overview','training','clips','device','settings',...(can('configure')?['fitting']:[])];
  if(!allowed.includes(name))name='overview';
  document.querySelectorAll('[data-view]').forEach(node=>node.hidden=node.dataset.view!==name);
  document.querySelectorAll('[data-page]').forEach(node=>{node.classList.toggle('selected',node.dataset.page===name);if(node.dataset.page===name)node.setAttribute('aria-current','page');else node.removeAttribute('aria-current');});
  history.replaceState(null,'',`#${name}`);window.scrollTo(0,0);$('main').focus({preventScroll:true});
  if(name==='clips')renderClips();
  if(name==='settings'){updateStorage();if(can('accounts'))loadCaregivers();}
  if(name==='fitting')loadFitting();
}
document.querySelectorAll('[data-page],[data-go]').forEach(node=>node.onclick=()=>page(node.dataset.page||node.dataset.go));
$('profile-shortcut').onclick=()=>page('settings');$('quick-train').onclick=()=>page('training');

function renderProfile(){
  $('child-label').textContent=household.profile.name||'Your child';
  $('initial').textContent=(household.profile.name||'C')[0].toUpperCase();
  $('child-name').value=household.profileDraft?.name??household.profile.name??'';$('child-goal').value=household.profileDraft?.goal??household.profile.goal??'';
}
$('profile-form').onsubmit=async event=>{
  event.preventDefault();if(!can('train'))return;
  household.profile={name:$('child-name').value.trim(),goal:$('child-goal').value.trim()};
  delete household.profileDraft;renderProfile();try{await journal();toast('Child profile saved on this browser.');}catch{toast('The profile is kept in memory. Export a backup before leaving this page.');}
};
$('profile-form').oninput=()=>{household.profileDraft={name:$('child-name').value,goal:$('child-goal').value};scheduleJournal();};
function renderGoals(){
  const list=$('goal-list');list.replaceChildren();
  if(!household.goals.length)empty(list,'Start with one small goal.','Link a goal to a practice session to see consistent effort over time.');
  const report=progressReport(household,clips);
  for(const goal of report.goals){
    const row=el('div',undefined,'goal-row');row.append(el('strong',goal.title),el('p',`${goal.sessions} of ${goal.targetSessions} planned sessions${goal.achieved?' · marked achieved':''}`));
    if(can('train'))row.append(button(goal.achieved?'Reopen goal':'Mark achieved',async()=>{
      household.goals=household.goals.map(g=>g.id===goal.id?{...g,achieved:!g.achieved}:g);await journal().catch(()=>{});renderGoals();
    },'text-button'));
    list.append(row);
  }
  const selected=$('training-goal').value;$('training-goal').replaceChildren(new Option('General practice',''));
  household.goals.filter(g=>!g.achieved).forEach(g=>$('training-goal').append(new Option(g.title,g.id)));
  if(household.goals.some(g=>g.id===selected&&!g.achieved))$('training-goal').value=selected;
}
$('goal-form').onsubmit=async event=>{
  event.preventDefault();if(!can('train'))return;
  household.goals.push({id:newId(),title:$('goal-title').value.trim(),targetSessions:Number($('goal-target').value),created:Date.now(),achieved:false});
  $('goal-title').value='';await journal().catch(()=>toast('Goal kept in memory; backup before leaving.'));renderGoals();
};
function renderProgress(){
  const report=progressReport(household,clips),root=$('progress-content');root.replaceChildren();
  root.append(el('p',`${report.practicedDays} practice ${report.practicedDays===1?'day':'days'} in the last week · ${report.streak}-day current streak. ${report.sessions} completed ${report.sessions===1?'session':'sessions'}, ${report.minutes.toFixed(1)} minutes of active practice.`));
  const days=el('div',undefined,'practice-days');
  report.lastSeven.forEach(day=>{const node=el('div',`${day.day.slice(5)} ${day.practiced?'Practiced':'—'}`,`practice-day${day.practiced?' practiced':''}`);node.setAttribute('aria-label',`${day.day}: ${day.practiced?'practice recorded':'no practice recorded'}`);days.append(node);});root.append(days);
  if(report.sounds.length){
    const table=el('table',undefined,'progress-table');const head=el('tr');['Sound','Responses','Mean error','Mean response'].forEach(t=>head.append(el('th',t)));const thead=el('thead');thead.append(head);table.append(thead);const body=el('tbody');
    const names={body:'Body-height',head:'Head-level',low:'Low obstacle',drop:'Drop-off'};
    report.sounds.forEach(s=>{const row=el('tr');[names[s.kind]||s.kind,s.count,`${s.error.toFixed(1)}°`,`${s.responseSeconds.toFixed(1)} s`].forEach(t=>row.append(el('td',t)));body.append(row);});table.append(body);root.append(table);
  }else empty(root,'A first response starts the picture.','Completed practice will show direction errors by sound type.');
  root.append(el('p','These measurements describe this sound exercise. They are not a clinical assessment or a score of safe mobility.','fine'));
}
function linkOptions(select,selected=''){
  select.replaceChildren(new Option('No session',''));
  household.sessions.slice(0,200).forEach(s=>select.append(new Option(`${date(s.created)} · ${s.status}${s.child?` · ${s.child}`:''}`,s.id)));
  if(household.sessions.some(s=>s.id===selected))select.value=selected;
}
function renderSessions(){
  for(const id of ['recent-sessions','session-list']){
    const root=$(id);root.replaceChildren();const records=id==='recent-sessions'?household.sessions.slice(0,3):household.sessions;
    if(!records.length)empty(root,'Their journey starts with a sound.','Finish a practice session to start building a history.');
    for(const s of records){
      const item=el('details',undefined,'session-entry');item.append(el('summary',`Listen & point · ${s.results?.length||s.trials||0} ${(s.results?.length||s.trials||0)===1?'response':'responses'} · ${s.status}${s.archived?' · imported history':''}`));
      item.append(el('small',`${date(s.created)} · ${duration(s.seconds)}${s.error!=null?` · ${s.error.toFixed(1)}° mean direction error`:''}`));
      const goal=household.goals.find(g=>g.id===s.goalId);if(goal)item.append(el('small',`Goal: ${goal.title}`));
      if(s.note)item.append(el('p',s.note,'session-observation'));
      const linked=clips.filter(c=>c.sessionId===s.id);
      item.append(button(`Review linked footage (${linked.length})`,()=>{clipFilter=s.id;page('clips');},'text-button'));
      if(can('train')&&(s.owner_id===account.user_id||account.role==='owner')&&s.status==='paused'&&!s.archived)item.append(button('Continue this session',()=>{session=s;$('session-note').value=s.note||'';page('training');renderTraining();},'text-button'));
      root.append(item);
    }
  }
  const selected=$('clip-session').value;linkOptions($('clip-session'),selected||session?.id||'');
  renderProgress();renderGoals();
}
$('download-report').onclick=()=>download(new Blob([reportCSV(household,clips)],{type:'text/csv;charset=utf-8'}),'echosense-progress.csv');
$('export-sessions').onclick=()=>download(new Blob([JSON.stringify({profile:household.profile,sessions:household.sessions,goals:household.goals},null,2)],{type:'application/json'}),'echosense-sessions.json');
async function syncHistory(){
  if(gate.locked)return;
  const remote=await api('/api/training/history');
  household.sessions=mergeSessions(household.sessions,remote.sessions);
  if(household.draft?.userId===account.user_id){
    const restored=remote.sessions.find(s=>s.id===household.draft.sessionId);
    if(restored&&restored.status!=='completed'&&!session){session=restored;$('session-note').value=household.draft.note||restored.note||'';pendingResponse=household.draft.pendingResponse||null;}
    if(restored?.status==='completed')household.draft=null;
  }
  if(session){const authoritative=remote.sessions.find(s=>s.id===session.id);if(authoritative){session={...authoritative};if(pendingResponse&&session.results.some(r=>r.response_id===pendingResponse.response_id))pendingResponse=null;}}
  await journal().catch(()=>{});renderSessions();renderTraining();
}

async function refreshClips(){try{clips=await repository.listClips();}catch{}$('clip-count').textContent=clips.length;renderRecentClips();renderSessions();if(!$('clip-library').closest('[data-view]').hidden)renderClips();updateStorage();}
function renderRecentClips(){
  const root=$('recent-clips');root.replaceChildren();if(!clips.length)empty(root,'A place for your first moment.','Record a clip or take a snapshot from the live view.');
  clips.slice(0,3).forEach(c=>{const row=el('div',undefined,'list-row'),text=el('div');text.append(el('strong',c.title),el('small',`${date(c.created)} · ${c.view} · ${c.kind==='video'?duration(c.seconds):'Snapshot'}`));row.append(text,button('Review',()=>{clipFilter='';page('clips');},'text-button'));root.append(row);});
}
function renderClips(){
  clipUrls.forEach(URL.revokeObjectURL);clipUrls=[];const root=$('clip-library');root.replaceChildren();
  const query=$('clip-search').value.toLowerCase();const list=clips.filter(c=>(!clipFilter||c.sessionId===clipFilter)&&`${c.title} ${c.note}`.toLowerCase().includes(query));
  if(clipFilter)root.append(button('Show all sessions',()=>{clipFilter='';renderClips();},'text-button'));
  if(!list.length)empty(root,'No matching moments.','Record a clip, change your search, or choose another session.');
  // Render a bounded page; media for later clips is not attached until requested.
  const count=Number(root.dataset.limit)||12;
  list.slice(0,count).forEach(c=>{
    const article=el('article',undefined,'clip'),media=el(c.kind==='video'?'video':'img'),url=URL.createObjectURL(c.blob);clipUrls.push(url);media.src=url;
    if(c.kind==='video'){media.controls=true;media.preload='none';media.playsInline=true;}else{media.alt=c.title;media.loading='lazy';}
    const selected=el('label',undefined,'check-label'),check=el('input');check.type='checkbox';check.checked=selectedClips.has(c.id);check.onchange=()=>{check.checked?selectedClips.add(c.id):selectedClips.delete(c.id);};selected.append(check,el('span','Include in selected backup'));
    const draft=clipDrafts.get(c.id)||c;
    const titleLabel=el('label','Clip title','field'),title=el('input');title.value=draft.title;title.maxLength=100;title.disabled=!can('train');titleLabel.append(title);
    const noteLabel=el('label','Training note','field'),note=el('textarea');note.rows=2;note.maxLength=2000;note.value=draft.note||'';note.disabled=!can('train');noteLabel.append(note);
    const sessionLabel=el('label','Linked practice session','field'),linked=el('select');linkOptions(linked,draft.sessionId||'');linked.disabled=!can('train');sessionLabel.append(linked);
    const update=()=>{clipDrafts.set(c.id,{title:title.value.trim()||c.title,note:note.value.trim(),sessionId:linked.value});scheduleJournal();};title.oninput=note.oninput=update;linked.onchange=update;
    const actions=el('div',undefined,'clip-actions');
    if(can('train'))actions.append(button('Save details',async()=>{update();try{const saved={...c,...clipDrafts.get(c.id)};await repository.saveClip(saved);clipDrafts.delete(c.id);await journal();await refreshClips();toast('Clip details saved.');}catch{toast('Details are retained in memory. Export this footage before leaving.');}}));
    actions.append(button('Download footage',()=>download(c.blob,`echosense-${c.id}.${c.kind==='image'?'png':c.blob.type.includes('mp4')?'mp4':'webm'}`)));
    if(can('train'))actions.append(button('Delete',async()=>{if(!confirm('Delete this saved moment from this browser?'))return;try{await repository.deleteClip(c.id);clipDrafts.delete(c.id);selectedClips.delete(c.id);await refreshClips();}catch{toast('Could not delete this moment.');}},'danger'));
    article.append(media,selected,titleLabel,el('small',`${date(c.created)} · ${c.view} · ${c.source}`),noteLabel,sessionLabel,actions);root.append(article);
  });
  if(list.length>count)root.append(button('Show more moments',()=>{root.dataset.limit=count+12;renderClips();}));
}
$('clip-search').oninput=()=>{$('clip-library').dataset.limit='12';renderClips();};
async function saveClipDrafts(){
  for(const [id,draft]of clipDrafts){const original=clips.find(c=>c.id===id);if(original){await repository.saveClip({...original,...draft});clipDrafts.delete(id);}}
  await journal();
}
function renderRecoveries(){
  const root=$('recording-recovery');root.replaceChildren();root.hidden=!recoveries.length;
  if(!recoveries.length)return;
  root.append(el('strong','This footage needs a download. It has not been saved to the library.'));
  for(const recovery of recoveries){
    root.append(button(`Download ${recovery.meta.title}`,()=>download(recovery.blob,`echosense-${recovery.meta.id}.${recovery.meta.kind==='image'?'png':recovery.blob.type.includes('mp4')?'mp4':'webm'}`)));
    root.append(button('Download its notes',()=>download(new Blob([JSON.stringify(recovery.meta,null,2)],{type:'application/json'}),`echosense-${recovery.meta.id}-notes.json`)));
    root.append(button('I saved these downloads',()=>{recoveries=recoveries.filter(r=>r!==recovery);renderRecoveries();}));
  }
}
async function saveMoment(blob,meta){
  const clip={...meta,blob};
  try{await repository.saveClip(clip);await refreshClips();toast('Moment saved to your clip library.');}
  catch{recoveries.push({blob,meta});renderRecoveries();toast('Storage is unavailable. Your footage is ready to download; keep this page open.');}
}
function momentMeta(kind,seconds=0){return {id:newId(),created:Date.now(),title:`${kind==='video'?'Practice clip':'Snapshot'} · ${new Date().toLocaleDateString()}`,note:session?$('session-note').value.trim():'',view:$('view-select').value,source:state?.source||'unknown',kind,seconds,sessionId:$('clip-session').value||''};}
function stopRecording(){if(recorder?.state==='recording'){recorder.stop();$('record').disabled=true;}return recordSave||Promise.resolve();}
$('record').onclick=()=>{
  if(recorder){stopRecording();return;}if(!can('train')||!viewFresh())return;
  if(!window.MediaRecorder||!feed.captureStream){toast('This browser cannot record the view. Use Snapshot or a supported browser.');return;}
  const stream=feed.captureStream(15),chunks=[],meta=momentMeta('video');
  try{
    const type=['video/webm;codecs=vp9','video/webm;codecs=vp8','video/mp4'].find(t=>MediaRecorder.isTypeSupported(t));
    const current=new MediaRecorder(stream,type?{mimeType:type}:undefined);recorder=current;recordStarted=Date.now();
    recordSave=new Promise(resolve=>{
      current.ondataavailable=event=>{if(event.data.size)chunks.push(event.data);};
      current.onstop=async()=>{
        stream.getTracks().forEach(track=>track.stop());recorder=null;meta.seconds=(Date.now()-recordStarted)/1000;
        const blob=new Blob(chunks,{type:current.mimeType});
        $('record').innerHTML='<span class="record-dot"></span>Record clip';$('record').classList.remove('recording');$('record-time').hidden=true;$('view-select').disabled=false;
        if(blob.size)await saveMoment(blob,meta);else toast('The clip contained no footage. Try recording again.');
        recordSave=null;resolve();
      };
      current.onerror=()=>{toast('Recording stopped unexpectedly. Recover any available footage below.');stopRecording();};
    });
    current.start(1000);$('record').innerHTML='<span class="record-dot"></span>Stop & save';$('record').classList.add('recording');$('record-time').hidden=false;$('view-select').disabled=true;
    toast('Recording started · maximum two minutes per clip.');
  }catch{stream.getTracks().forEach(track=>track.stop());recorder=null;recordSave=null;toast('Recording could not start. Try a snapshot instead.');}
};
$('snapshot').onclick=()=>{if(!can('train')||!viewFresh())return;const meta=momentMeta('image');feed.toBlob(blob=>{if(blob)saveMoment(blob,meta);else toast('Snapshot failed.');},'image/png');};

function renderTraining(){
  const ready=fresh()&&can('train')&&!controlBusy,active=session?.status==='active'&&ownsConnection;
  const answered=session?.results.some(r=>r.trial_id===session.trial_id);
  $('training-start').disabled=!ready||pendingFinish||!!(session&&session.status!=='completed')||state?.training?.active;
  $('training-next').disabled=!ready||!active||!!pendingResponse||pendingFinish;
  $('answers').disabled=!ready||!active||!session.trial_id||answered||!!pendingResponse||pendingFinish;
  $('training-pause').disabled=!ready||!active||pendingFinish;
  $('training-resume').disabled=!ready||session?.status!=='paused'||pendingFinish;
  $('training-finish').disabled=!ready||!session||session.status==='completed'||pendingFinish;
  $('training-kind').disabled=!can('train')||pendingFinish;$('training-distance').disabled=!can('train')||pendingFinish;
  $('training-goal').disabled=!!(session&&session.status!=='completed')||!can('train');
  $('session-note').disabled=!can('train');
  $('retry-response').hidden=!pendingResponse;
  if(session){$('training-status').textContent=session.status==='active'?active?'Practice is supervised. Play a direction when your child is ready.':'Another connection is supervising. Pause there before resuming here.':session.reason||`Practice ${session.status}.`;}
  else $('training-status').textContent=!can('train')?'Your account can review practice but cannot control training.':state?.training?.active?`Training is active${state.supervision?.owner_name?` with ${state.supervision.owner_name}`:''}. Finish it there before starting another session.`:'Ready for a short, seated exercise.';
}
async function updateSession(result,{own=false}={}){
  session=result;if(own)ownsConnection=result.status==='active';if(result.status!=='active')ownsConnection=false;
  rememberSession(result);
  if(pendingResponse&&result.results.some(r=>r.response_id===pendingResponse.response_id))pendingResponse=null;
  await journal().catch(()=>{});renderSessions();renderTraining();
}
$('training-start').onclick=async()=>{
  if(!can('train'))return;try{const result=await training('start',{goal_id:$('training-goal').value,child:household.profile.name||''});$('session-note').value='';$('trial-feedback').textContent='';pendingResponse=null;await updateSession(result,{own:true});$('clip-session').value=result.id;}catch(error){toast(error.message);}
};
$('training-next').onclick=async()=>{
  if(pendingResponse){toast('Save or retry the previous answer first.');return;}
  try{const result=await training('target',{trial_id:newId(),az:[-60,-30,0,30,60][Math.floor(Math.random()*5)],r:Number($('training-distance').value),kind:$('training-kind').value});await updateSession(result,{own:true});$('trial-feedback').textContent='Listen, then choose where your child pointed.';}catch(error){toast(error.message);}
};
async function submitPending(){
  if(responseFlight)return responseFlight;
  if(!pendingResponse)return;
  const payload={...pendingResponse};
  responseFlight=(async()=>{
    const result=await training('respond',payload);await updateSession(result,{own:true});
    const last=result.results.find(r=>r.response_id===payload.response_id);
    $('trial-feedback').textContent=last?`Response saved · ${last.error_deg}° from the sound.`:'Response saved.';
  })();
  try{return await responseFlight;}finally{responseFlight=null;}
}
document.querySelectorAll('[data-az]').forEach(node=>node.onclick=async()=>{
  if(!session||pendingResponse||!ownsConnection)return;
  pendingResponse={session_id:session.id,trial_id:session.trial_id,response_id:newId(),az:Number(node.dataset.az)};
  await journal().catch(()=>{});renderTraining();try{await submitPending();}catch(error){toast(error.message);renderTraining();}
});
$('retry-response').onclick=async()=>{try{await submitPending();}catch(error){toast(error.message);}};
$('training-pause').onclick=async()=>{try{await updateSession(await training('pause'));}catch(error){toast(error.message);}};
$('training-resume').onclick=async()=>{try{await updateSession(await training('resume'),{own:true});if(pendingResponse)await submitPending();}catch(error){toast(error.message);}};
$('training-finish').onclick=async()=>{
  if(pendingFinish)return;pendingFinish=true;renderTraining();
  try{
    if(pendingResponse)await submitPending();
    const finished=await training('finish',{note:$('session-note').value.trim()});
    await updateSession(finished);household.draft=null;session=null;ownsConnection=false;
    await journal().catch(()=>{});renderTraining();toast('Practice, all confirmed answers, and your observation were saved on the device.');
  }catch(error){toast(error.message);}
  finally{pendingFinish=false;renderTraining();}
};
$('session-note').oninput=()=>{
  scheduleJournal();clearTimeout(noteTimer);
  noteTimer=setTimeout(()=>{if(session&&fresh()&&can('train'))training('note',{note:$('session-note').value.trim()}).then(result=>{rememberSession(result);renderSessions();}).catch(()=>{});},700);
};
$('export-unsaved').onclick=()=>download(new Blob([JSON.stringify(household.draft||{session,note:$('session-note').value,pendingResponse},null,2)],{type:'application/json'}),'echosense-unfinished-practice.json');

function onJson(message){
  if(message.type==='training_ack'){
    const waiter=pending.get(message.request_id);
    if(waiter){clearTimeout(waiter.timer);pending.delete(message.request_id);if(waiter.isControl)controlBusy=false;waiter.resolve(message.session);}
    return;
  }
  if(message.type==='supervision'){
    const snapshot=message.session;
    if(session&&snapshot?.id===session.id){session={...session,...snapshot};if(snapshot.status!=='active')ownsConnection=false;renderTraining();}
    return;
  }
  if(message.type==='event'&&message.kind==='error'){
    const waiter=pending.get(message.request_id);
    if(waiter){clearTimeout(waiter.timer);pending.delete(message.request_id);if(waiter.isControl)controlBusy=false;waiter.reject(new Error(message.text));renderTraining();}
    else toast(message.text);
  }
}
function drawFrame(meta,pixels,color=false){
  const width=color?meta.w:meta.intr.w,height=color?meta.h:meta.intr.h;
  if(feed.width!==width||feed.height!==height){feed.width=width;feed.height=height;}
  const image=context.createImageData(width,height);
  for(let i=0,j=0;i<width*height;i++,j+=4){
    if(color){image.data[j]=pixels[i*3];image.data[j+1]=pixels[i*3+1];image.data[j+2]=pixels[i*3+2];}
    else{const value=pixels[i],t=Math.min(1,value/4500);image.data[j]=value?140*(1-t)+15:8;image.data[j+1]=value?190*(1-t)+24:13;image.data[j+2]=value?235*(1-t)+38:18;}
    image.data[j+3]=255;
  }
  context.putImageData(image,0,0);$('feed-empty').hidden=true;
}
function connect(){
  if(gate.locked)return;
  const socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws`);ws=socket;socket.binaryType='arraybuffer';
  socket.onopen=()=>{send({t:'rgb',on:$('view-select').value==='color'});if(audio.on)send({t:'audio',on:true});syncHistory().catch(()=>{});};
  socket.onmessage=event=>{
    if(gate.locked)return;
    try{
      if(typeof event.data==='string'){onJson(JSON.parse(event.data));return;}
      const type=new Uint8Array(event.data)[0];if(type===2){playAudio(new Int16Array(event.data,4));return;}
      const length=new DataView(event.data).getUint32(4,true),meta=JSON.parse(decoder.decode(new Uint8Array(event.data,8,length)));
      if(type===1){state=meta;lastFrame=Date.now();if(session&&meta.supervision?.id===session.id){session={...session,...meta.supervision};if(session.status!=='active')ownsConnection=false;}if($('view-select').value==='depth')drawFrame(meta,new Uint16Array(event.data,8+length));}
      if(type===3&&$('view-select').value==='color'){lastRgb=Date.now();drawFrame(meta,new Uint8Array(event.data,8+length),true);}
    }catch(error){console.error('EchoSense stream:',error);}
  };
  socket.onclose=async()=>{
    if(ws!==socket)return;ownsConnection=false;lastFrame=0;stopRecording();renderTraining();
    if(session?.status==='active')session={...session,status:'paused',reason:'Connection ended. Resume explicitly after reconnecting.'};
    journal().catch(()=>{});
    try{const status=await api('/api/auth/status');if(!status.authenticated){window.echoSenseLock();return;}}catch{}
    if(!gate.locked)setTimeout(connect,2000);
  };
  socket.onerror=()=>socket.close();
}
$('view-select').onchange=()=>{lastRgb=0;context.clearRect(0,0,feed.width,feed.height);$('feed-empty').hidden=false;$('view-label').textContent=$('view-select').value==='color'?'Color camera':'Depth view';send({t:'rgb',on:$('view-select').value==='color'});};
function renderState(){
  if(gate.locked)return;
  const ready=fresh();$('connection').textContent=ready?'Connected on local network':'Device disconnected';$('connection').className=`connection${ready?' online':''}`;
  $('record').disabled=!recorder&&(!viewFresh()||!can('train')||!!recordSave);$('snapshot').disabled=!viewFresh()||!can('train');$('listen').disabled=!ready;
  $('live-state').textContent=ready?(viewFresh()?'LIVE':'NO COLOR DATA'):'OFFLINE';
  if(!ready){$('source-notice').textContent='No live sensor data. Check device power and your connection.';if(recorder)stopRecording();renderTraining();return;}
  const simulated=/sim|fake/.test(state.source),health=state.health==='ok'?'Sensor responding':state.health.replaceAll('_',' ');
  $('source-label').textContent=state.source_label||state.source;$('source-notice').className=`notice${simulated?' simulation':''}`;
  $('source-notice').textContent=simulated?'Simulator connected · View and readings are simulated.':state.health==='ok'?'EchoSense is streaming. Stay nearby during practice.':`Sensor needs attention: ${health}. Pause and check the device.`;
  $('sensor-health').textContent=health;$('battery').textContent=state.battery.percent==null?'Unavailable':`${Math.round(state.battery.percent)}%`;$('battery-note').textContent='(simulated)';$('range').textContent=`${state.range_m} m`;$('audio-mode').textContent=state.training.active?'Training sounds':state.settings.mode;
  $('frame-detail').textContent=`${state.intr.w} × ${state.intr.h} · ${state.metrics.fps} fps`;$('view-select').options[1].disabled=!state.has_rgb;
  if(recorder&&!viewFresh())stopRecording();
  const details=$('device-details');details.replaceChildren();
  for(const [key,value]of Object.entries({'Source':`${state.source_label||state.source}${simulated?' · simulated':''}`,'Sensor health':health,'Camera':state.has_rgb?'Color and depth available':'Depth only','Battery':`${$('battery').textContent} · simulated`,'Child fitting':state.settings.fitting_checked?'Measurements saved; verify the mount before use':'Not checked — complete child fitting','Training':state.training.active?'Virtual training sounds':'Live obstacle cues','Local storage':'This browser; back up important footage'})){
    const row=el('div');row.append(el('dt',key),el('dd',value));details.append(row);
  }
  renderTraining();
}
setInterval(()=>{renderState();if(recorder){$('record-time').textContent=`Recording ${duration((Date.now()-recordStarted)/1000)}`;if(Date.now()-recordStarted>=120000)stopRecording();}},500);
setInterval(()=>{if(session?.status==='active'&&ownsConnection&&fresh()&&!document.hidden)send({t:'training',cmd:'heartbeat',session_id:session.id});},2000);
document.addEventListener('visibilitychange',()=>{if(document.hidden&&session?.status==='active'&&ownsConnection)training('pause').then(updateSession).catch(()=>{});});
$('listen').onclick=async()=>{try{audio.ctx??=new AudioContext();await audio.ctx.resume();audio.on=!audio.on;audio.next=0;send({t:'audio',on:audio.on});$('listen').textContent=audio.on?'Stop browser audio':'Listen in browser';}catch{toast('Browser audio could not start.');}};
function playAudio(pcm){if(!audio.on||!audio.ctx)return;const count=pcm.length/2,buffer=audio.ctx.createBuffer(2,count,24000);for(let ch=0;ch<2;ch++){const samples=buffer.getChannelData(ch);for(let i=0;i<count;i++)samples[i]=pcm[i*2+ch]/32768;}const src=audio.ctx.createBufferSource();src.buffer=buffer;src.connect(audio.ctx.destination);const now=audio.ctx.currentTime;if(audio.next<now+.03||audio.next>now+.6)audio.next=now+.1;src.start(audio.next);audio.next+=buffer.duration;}

async function updateStorage(){
  const bytes=clips.reduce((sum,c)=>sum+c.blob.size,0);let text=`Clip library: ${(bytes/1048576).toFixed(1)} MB on this browser.`;
  try{if(navigator.storage?.estimate){const estimate=await navigator.storage.estimate();text+=` Browser usage: ${((estimate.usage||0)/1048576).toFixed(1)} MB of approximately ${((estimate.quota||0)/1048576).toFixed(0)} MB.`;}if(await navigator.storage?.persisted?.())text+=' Persistent storage is enabled.';}catch{}
  $('storage-usage').textContent=text;
}
$('request-persistence').onclick=async()=>{try{const granted=await navigator.storage?.persist?.();toast(granted?'Persistent storage enabled. Keep backups too.':'Persistent storage was not granted. Download backups of important footage.');await updateStorage();}catch{toast('Persistent storage is unavailable here. Use backups.');}};
$('backup-export').onclick=async()=>{
  $('backup-export').disabled=true;
  try{await saveClipDrafts().catch(()=>{});await refreshClips();const all=[...clips,...recoveries.map(r=>({...r.meta,blob:r.blob}))].map(c=>({...c,...clipDrafts.get(c.id)}));const chosen=selectedClips.size?all.filter(c=>selectedClips.has(c.id)):all;download(await createBackup(household,chosen),'echosense-family-backup.zip');toast(`Backup prepared with ${chosen.length} ${chosen.length===1?'moment':'moments'}, notes, goals, and practice history.`);}catch(error){toast(error.message);}
  finally{$('backup-export').disabled=false;}
};
$('backup-file').onchange=async()=>{
  pendingBackup=null;$('restore-submit').disabled=true;const file=$('backup-file').files[0];if(!file)return;
  try{pendingBackup=await parseBackup(file);$('restore-preview').textContent=`Ready to restore ${pendingBackup.clips.length} clips, ${pendingBackup.state.sessions.length} sessions, and ${pendingBackup.state.goals.length} goals. Existing record IDs will be kept.`;$('restore-submit').disabled=false;}catch(error){$('restore-preview').textContent=error.message;}
};
$('restore-form').onsubmit=async event=>{
  event.preventDefault();if(!can('train')||!pendingBackup)return;if(recorder||recordSave||pendingResponse||session?.status==='active'){toast('Finish practice and recording before restoring a backup.');return;}$('restore-submit').disabled=true;
  try{const merged=mergeBackup(household,clips,pendingBackup,$('restore-profile').checked);await repository.restore(merged.state,merged.clips);household=merged.state;await journal();pendingBackup=null;$('backup-file').value='';renderProfile();await refreshClips();$('restore-preview').textContent=`Restored ${merged.clips.length} moments. Kept ${merged.skipped} existing moments without overwriting them.`;}catch(error){$('restore-preview').textContent=`Restore failed: ${error.message}`;$('restore-submit').disabled=false;}
};

async function loadCaregivers(){
  try{const result=await api('/api/caregivers'),root=$('caregiver-list');root.replaceChildren();result.users.forEach(user=>{
    const row=el('div',undefined,'list-row');row.append(el('span',`${user.username} · ${user.role}${user.disabled?' · disabled':''}`));
    if(user.role!=='owner')row.append(button(user.disabled?'Restore access':'Revoke access',async()=>{try{await api(`/api/caregivers/${user.id}/access`,{disabled:user.disabled?'false':'true'});await loadCaregivers();toast('Caregiver access updated.');}catch(error){toast(error.message);}}));root.append(row);
  });}catch(error){$('caregiver-message').textContent=error.message;}
}
$('caregiver-form').onsubmit=async event=>{
  event.preventDefault();if(!can('accounts'))return;
  try{const result=await api('/api/caregivers/invite',{username:$('caregiver-username').value.trim(),role:$('caregiver-role').value});$('caregiver-message').textContent=`Share this username and one-time invitation code privately: ${result.code}. The caregiver should choose “Use a caregiver invitation” on the login page. Valid for 24 hours.`;}catch(error){$('caregiver-message').textContent=error.message;}
};
function fittingValues(){return {head_height:Number($('fit-head').value)/100,ear_height:Number($('fit-ear').value)/100,mount_height:Number($('fit-mount').value)/100,mount_pitch:Number($('fit-pitch').value),fitting_checked:$('fit-checked').checked};}
function fittingStepView(){
  document.querySelectorAll('[data-fitting-step]').forEach(node=>node.hidden=Number(node.dataset.fittingStep)!==fittingStep);
  $('fitting-step-label').textContent=['Measure your child','Fit the sensor','Review and save'][fittingStep];$('fit-back').hidden=fittingStep===0;$('fit-next').hidden=fittingStep===2;$('fit-save').hidden=fittingStep!==2;
}
function fittingFields(data){const s=data.settings;$('fit-head').value=s.head_height?Math.round(s.head_height*1000)/10:'';$('fit-ear').value=s.ear_height?Math.round(s.ear_height*1000)/10:'';$('fit-mount').value=Math.round((s.mount_height??state?.mount?.height??1)*1000)/10;$('fit-pitch').value=s.mount_pitch??state?.mount?.pitch??-10;$('fit-checked').checked=false;}
async function loadFitting(){try{const data=await api('/api/fitting');fittingFields(data);fittingStep=0;fittingStepView();$('fitting-message').textContent=data.settings.fitting_checked?'Previous measurements are saved. Recheck them as your child grows.':'';}catch(error){$('fitting-message').textContent=error.message;}}
function renderCoverage(coverage){const root=$('fit-coverage');root.replaceChildren();const dl=el('dl',undefined,'detail-list');for(const [label,key]of [['Floor first visible','floor_from'],['Head height first visible','head_from'],['Full corridor width visible','corridor_from']]){const row=el('div');row.append(el('dt',label),el('dd',coverage[key]==null?'Outside the sensing range':`${coverage[key]} m from the sensor`));dl.append(row);}root.append(dl,el('p',`Measured head height: ${(coverage.head_height*100).toFixed(1)} cm. Sensor range: ${coverage.min_depth}–${coverage.max_depth} m.`));}
$('fit-next').onclick=async()=>{
  const current=document.querySelector(`[data-fitting-step="${fittingStep}"]`);for(const input of current.querySelectorAll('input'))if(!input.reportValidity())return;
  if(fittingStep===0){if(Number($('fit-ear').value)>Number($('fit-head').value)){$('fitting-message').textContent='Ear height must be at or below head height.';return;}fittingStep=1;}
  else{try{renderCoverage((await api('/api/fitting/preview',fittingValues())).coverage);fittingStep=2;}catch(error){$('fitting-message').textContent=error.message;return;}}
  $('fitting-message').textContent='';fittingStepView();
};
$('fit-back').onclick=()=>{fittingStep=Math.max(0,fittingStep-1);fittingStepView();};
$('fit-calibrate').onclick=async()=>{try{const data=await api('/api/fitting/calibrate',{});$('fit-mount').value=(data.settings.mount_height*100).toFixed(1);$('fit-pitch').value=data.settings.mount_pitch;$('fitting-message').textContent='Floor calibration applied. Review the lens height and tilt before continuing.';}catch(error){$('fitting-message').textContent=error.message;}};
$('fitting-form').onsubmit=async event=>{event.preventDefault();if(fittingStep!==2)return;try{const data=await api('/api/fitting',fittingValues());renderCoverage(data.coverage);$('fitting-message').textContent='Child fitting saved to the device. Check the mount before every practice.';}catch(error){$('fitting-message').textContent=error.message;}};

window.echoSenseLock=async()=>{
  if(gate.locked&&account===null)return;
  gate.lock();ownsConnection=false;
  if(session?.status==='active')session={...session,status:'paused',reason:'Sign in and resume explicitly.'};
  ws?.close();audio.on=false;audio.ctx?.suspend().catch(()=>{});
  context.clearRect(0,0,feed.width,feed.height);document.querySelectorAll('#clip-library video').forEach(video=>video.pause());
  clipUrls.forEach(URL.revokeObjectURL);clipUrls=[];
  const saved=await Promise.allSettled([stopRecording(),journal(),saveClipDrafts()]);
  $('privacy-message').textContent=saved.every(result=>result.status==='fulfilled')&&!recoveries.length?'Your work was preserved. Sign in here to unlock and review it.':'Your work is kept in this open page. Sign in here to recover it; avoid closing or reloading.';
};
$('unlock-form').onsubmit=async event=>{
  event.preventDefault();$('unlock-submit').disabled=true;$('unlock-message').textContent='Signing in…';
  try{account=await window.echoSenseReauthenticate({username:$('unlock-username').value.trim(),password:$('unlock-password').value});gate.unlock();applyPermissions();$('unlock-password').value='';$('unlock-message').textContent='';renderProfile();renderSessions();renderRecentClips();renderRecoveries();renderClips();$('listen').textContent='Listen in browser';connect();}
  catch(error){$('unlock-message').textContent=error.message;}
  finally{$('unlock-password').value='';$('unlock-submit').disabled=false;}
};
window.echoSenseCanSignOut=async()=>{
  if(recorder||recordSave||session?.status==='active'||pendingResponse||recoveries.length){toast('Finish practice and download any recovery footage before signing out.');return false;}
  await saveClipDrafts().catch(()=>{});return true;
};
addEventListener('beforeunload',event=>{if(recorder||recordSave||pendingResponse||recoveries.length||session?.status==='active'){event.preventDefault();event.returnValue='';}});

async function initialize(){
  account=await window.echoSenseAccount;
  if(!account.authenticated){account=null;gate.lock();return;}
  try{repository=new ParentRepository();household=await repository.loadState();}
  catch{household=emptyState();toast('Local storage is unavailable. Recording can still be downloaded directly.');}
  const recovery=emergencyRead();
  if(recovery&&(recovery.updatedAt||0)>(household.updatedAt||0)){household={...household,...recovery};clipDrafts=new Map(recovery.clipDrafts||[]);}
  else if(recovery?.draft&&!household.draft){household.draft=recovery.draft;clipDrafts=new Map(recovery.clipDrafts||[]);}
  else clipDrafts=new Map(household.clipDrafts||[]);
  gate.unlock();applyPermissions();renderProfile();renderSessions();
  await refreshClips();page(location.hash.slice(1)||'overview');connect();
  if(household.draft){$('draft-notice').hidden=false;$('draft-notice').textContent='Unfinished practice was recovered. Reconnect, review its history, and resume explicitly.';$('export-unsaved').hidden=false;}
}
initialize().catch(error=>{gate.lock('The app could not finish loading. Reload after checking the device connection.');console.error(error);});
