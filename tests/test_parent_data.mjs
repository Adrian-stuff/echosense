import test from 'node:test';
import assert from 'node:assert/strict';
import {createBackup,parseBackup,mergeBackup,emptyState,ParentRepository,mergeSessions} from '../echosense/web/parent-data.mjs';
import {progressReport,reportCSV} from '../echosense/web/parent-progress.mjs';
import {PrivacyGate} from '../echosense/web/parent-privacy.mjs';

const response={target_az:30,target_r:1,response_az:0,error_deg:30,response_time_s:2,kind:'head',trial_id:'trial-1',response_id:'response-1'};
const sample=()=>({profile:{name:'Ari',goal:'Recognize left and right'},goals:[{id:'goal-1',title:'Practice direction',targetSessions:3,created:1000,achieved:false}],sessions:[{id:'practice-1',created:new Date(2026,10,25,12).getTime(),status:'completed',seconds:60,note:'Pointed ahead',goalId:'goal-1',results:[response]}],draft:null});
const clip=()=>({id:'clip-1',created:1000,title:'A first sound',note:'Asked for another turn',sessionId:'practice-1',kind:'image',view:'depth',source:'sim',seconds:0,blob:new Blob([new Uint8Array([137,80,78,71,13,10,26,10,42,55,66])],{type:'image/png'})});

test('backup round-trip includes footage, titles, notes, goals, and session links',async()=>{
  const original=clip(),archive=await createBackup(sample(),[original]);
  const restored=await parseBackup(archive);
  assert.equal(restored.state.profile.name,'Ari');
  assert.equal(restored.state.sessions[0].results[0].response_id,'response-1');
  assert.equal(restored.state.goals[0].targetSessions,3);
  assert.equal(restored.clips[0].sessionId,'practice-1');
  assert.equal(restored.clips[0].note,'Asked for another turn');
  assert.deepEqual(new Uint8Array(await restored.clips[0].blob.arrayBuffer()),new Uint8Array(await original.blob.arrayBuffer()));
});
test('damaged and malformed backups are rejected before restore',async()=>{
  const archive=await createBackup(sample(),[clip()]);
  const bytes=new Uint8Array(await archive.arrayBuffer());
  bytes[45]^=1;
  await assert.rejects(parseBackup(new Blob([bytes])),/damaged|Invalid/);
  await assert.rejects(parseBackup(new Blob(['not a backup'])),/not an EchoSense/);
  const invalid=sample();invalid.sessions[0].results[0]={...response,error_deg:NaN};
  await assert.rejects(createBackup(invalid,[]),/Invalid numeric/);
});
test('restoring twice skips duplicates and preserves existing records',async()=>{
  const backup=await parseBackup(await createBackup(sample(),[clip()]));
  const first=mergeBackup(emptyState(),[],backup);
  const changed={...first.clips[0],note:'Original remains authoritative'};
  const second=mergeBackup(first.state,[changed],backup,false);
  assert.equal(second.clips.length,0);assert.equal(second.skipped,1);
  assert.equal(second.state.sessions.length,1);assert.equal(second.state.goals.length,1);
  assert.equal(changed.note,'Original remains authoritative');
});
test('reports group real trial errors, goal practice, observations, and linked footage',()=>{
  const state=sample();state.sessions.push({...state.sessions[0],id:'practice-2',status:'paused',seconds:999});
  const report=progressReport(state,[clip()],new Date(2026,10,25,18).getTime());
  assert.equal(report.sessions,1);assert.equal(report.minutes,1);assert.equal(report.practicedDays,1);
  assert.equal(report.streak,1);assert.equal(report.sounds[0].error,30);assert.equal(report.sounds[0].count,1);
  assert.equal(report.goals[0].sessions,1);assert.equal(report.observations[0].clips[0].title,'A first sound');
  state.sessions[0].note='=1+1';
  assert.match(reportCSV(state,[clip()]),/"'=1\+1"/);
});
test('server history reconciles results without duplicating a completed session',()=>{
  const local=sample().sessions;
  const remote=[{...local[0],results:[response,{...response,response_id:'response-2'}],trials:2}];
  const merged=mergeSessions(local,remote);
  assert.equal(merged.length,1);assert.equal(merged[0].results.length,2);
});
test('privacy lock hides private content synchronously and needs explicit unlock',()=>{
  const content={hidden:false,inert:false},screen={hidden:true,focus(){this.focused=true;}},label={textContent:''};
  const gate=new PrivacyGate(content,screen,label);
  gate.lock('Session expired');
  assert.equal(content.hidden,true);assert.equal(content.inert,true);assert.equal(screen.hidden,false);
  assert.equal(label.textContent,'Session expired');assert.equal(screen.focused,true);
  gate.unlock();assert.equal(content.hidden,false);assert.equal(content.inert,false);assert.equal(screen.hidden,true);
});
test('unavailable storage rejects cleanly without blocking direct-media workflows',async()=>{
  const repo=new ParentRepository(null,null);
  await assert.rejects(repo.saveClip(clip()),/unavailable/);
});

test('export refuses archives that would exceed its own import limits',async()=>{
  const tooMany=Array.from({length:1001},(_,i)=>({...clip(),id:`clip-${i}`}));
  await assert.rejects(createBackup(sample(),tooMany),/1,000 or fewer/);
  const large=sample();large.sessions=Array.from({length:4500},(_,i)=>({...large.sessions[0],id:`session-${i}`,results:[],note:'x'.repeat(2000)}));
  await assert.rejects(createBackup(large,[]),/8 MB backup manifest limit/);
});
test('imported unfinished history cannot masquerade as a live device session',async()=>{
  const state=sample();state.sessions[0].status='active';
  const imported=await parseBackup(await createBackup(state,[]));
  assert.equal(imported.state.sessions[0].status,'paused');
  assert.equal(imported.state.sessions[0].archived,true);
  const reconciled=mergeSessions(imported.state.sessions,[{...state.sessions[0],owner_id:1}]);
  assert.equal(reconciled[0].archived,false);
  assert.equal(reconciled[0].status,'active');
});
