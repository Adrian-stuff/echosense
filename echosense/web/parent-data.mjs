/* Local household data and portable backups. No network or credentials in archives. */
export const BACKUP_LIMIT = 256 * 1024 * 1024;
const idPattern = /^[A-Za-z0-9_.:-]{1,100}$/;
export const newId = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
export const emptyState = () => ({profile: {}, sessions: [], goals: [], draft: null});

export class ParentRepository {
  constructor(factory = globalThis.indexedDB, legacy = undefined) {
    if(legacy===undefined){try{legacy=globalThis.localStorage;}catch{legacy=null;}}
    this.legacy = legacy;
    this.ready = new Promise((resolve, reject) => {
      if (!factory) { reject(new Error('Browser storage is unavailable.')); return; }
      const request = factory.open('echosense-parent', 2);
      const timer = setTimeout(() => reject(new Error('Browser storage is blocked. Close other EchoSense tabs and retry.')), 5000);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains('clips')) db.createObjectStore('clips', {keyPath: 'id'});
        if (!db.objectStoreNames.contains('state')) db.createObjectStore('state');
      };
      request.onerror = () => { clearTimeout(timer); reject(request.error); };
      request.onblocked = () => { clearTimeout(timer); reject(new Error('Close other EchoSense tabs to upgrade local storage.')); };
      request.onsuccess = () => {
        clearTimeout(timer);
        request.result.onversionchange = () => request.result.close();
        resolve(request.result);
      };
    });
    this.ready.catch(() => {});
  }
  async transaction(names, mode, run) {
    const db = await this.ready;
    return new Promise((resolve, reject) => {
      const tx = db.transaction(names, mode);
      let result;
      try { result = run(tx); } catch (error) { tx.abort(); reject(error); return; }
      tx.oncomplete = () => resolve(typeof result === 'function' ? result() : result);
      tx.onerror = tx.onabort = () => reject(tx.error || new Error('Local storage could not save this change.'));
    });
  }
  async loadState() {
    let request;
    const saved = await this.transaction(['state'], 'readonly', tx => {
      request = tx.objectStore('state').get('household'); return () => request.result;
    });
    if (saved) return saved;
    const state = emptyState();
    try {
      state.profile = JSON.parse(this.legacy?.getItem('echosense.parent.profile') || '{}');
      state.sessions = JSON.parse(this.legacy?.getItem('echosense.parent.sessions') || '[]');
      if (!Array.isArray(state.sessions)) state.sessions = [];
      state.sessions = state.sessions.map(s => ({...s, id: s.id || newId(), status: s.status || 'completed'}));
    } catch { /* Keep an intact empty state when legacy data is corrupt. */ }
    await this.saveState(state);
    return state;
  }
  saveState(state) {
    return this.transaction(['state'], 'readwrite', tx => tx.objectStore('state').put(state, 'household'));
  }
  listClips() {
    let request;
    return this.transaction(['clips'], 'readonly', tx => {
      request = tx.objectStore('clips').getAll(); return () => request.result.sort((a,b) => b.created - a.created);
    });
  }
  saveClip(clip) { return this.transaction(['clips'], 'readwrite', tx => tx.objectStore('clips').put(clip)); }
  deleteClip(id) { return this.transaction(['clips'], 'readwrite', tx => tx.objectStore('clips').delete(id)); }
  restore(state, clips) {
    return this.transaction(['state','clips'], 'readwrite', tx => {
      tx.objectStore('state').put(state, 'household');
      clips.forEach(clip => tx.objectStore('clips').put(clip));
    });
  }
}

export function mergeSessions(local, remote) {
  const map = new Map(local.map(s => [s.id || `legacy-${s.created}`, s]));
  remote.forEach(s => map.set(s.id, {...map.get(s.id), ...s, archived:false}));
  return [...map.values()].sort((a,b) => b.created - a.created);
}

const crcTable = Array.from({length:256}, (_, n) => {
  let c = n; for (let i=0;i<8;i++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; return c >>> 0;
});
export function crc32(bytes) {
  let c = 0xffffffff;
  for (const byte of bytes) c = crcTable[(c ^ byte) & 255] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}
function zipHeader(size) { const bytes = new Uint8Array(size); return [bytes, new DataView(bytes.buffer)]; }
export async function zipFiles(files) {
  const parts = [], central = [];
  let offset = 0, centralSize = 0;
  const encoder = new TextEncoder();
  for (const [name, blob] of files) {
    const encoded = encoder.encode(name), bytes = new Uint8Array(await blob.arrayBuffer()), crc = crc32(bytes);
    const [local, view] = zipHeader(30 + encoded.length);
    view.setUint32(0, 0x04034b50, true); view.setUint16(4,20,true); view.setUint16(12,33,true);
    view.setUint32(14,crc,true); view.setUint32(18,blob.size,true); view.setUint32(22,blob.size,true);
    view.setUint16(26,encoded.length,true); local.set(encoded,30);
    parts.push(local,blob);
    const [entry, dir] = zipHeader(46 + encoded.length);
    dir.setUint32(0,0x02014b50,true); dir.setUint16(4,20,true); dir.setUint16(6,20,true);
    dir.setUint16(14,33,true); dir.setUint32(16,crc,true); dir.setUint32(20,blob.size,true);
    dir.setUint32(24,blob.size,true); dir.setUint16(28,encoded.length,true); dir.setUint32(42,offset,true);
    entry.set(encoded,46); central.push(entry); centralSize += entry.length; offset += local.length + blob.size;
  }
  const [end, view] = zipHeader(22);
  view.setUint32(0,0x06054b50,true); view.setUint16(8,files.length,true); view.setUint16(10,files.length,true);
  view.setUint32(12,centralSize,true); view.setUint32(16,offset,true);
  return new Blob([...parts,...central,end],{type:'application/zip'});
}
function readZip(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength), files = new Map();
  let offset=0;
  while (offset + 4 <= bytes.length && view.getUint32(offset,true) === 0x04034b50) {
    if (offset+30 > bytes.length || files.size >= 1002) throw new Error('Invalid or oversized backup.');
    const flags = view.getUint16(offset+6,true), method=view.getUint16(offset+8,true);
    const size=view.getUint32(offset+18,true), unpacked=view.getUint32(offset+22,true);
    const length=view.getUint16(offset+26,true), extra=view.getUint16(offset+28,true);
    const start=offset+30+length+extra, end=start+size;
    if (flags !== 0 || method !== 0 || size !== unpacked || end > bytes.length) throw new Error('Use an unmodified EchoSense backup ZIP.');
    const name=new TextDecoder('utf-8',{fatal:true}).decode(bytes.subarray(offset+30,offset+30+length));
    if (!name || name.includes('..') || name.startsWith('/') || files.has(name)) throw new Error('Invalid or duplicate backup entry.');
    const data=bytes.subarray(start,end);
    if (crc32(data) !== view.getUint32(offset+14,true)) throw new Error('The backup is damaged. No data was restored.');
    files.set(name,data); offset=end;
  }
  if (offset+4 > bytes.length || view.getUint32(offset,true) !== 0x02014b50) throw new Error('This is not an EchoSense backup ZIP.');
  return files;
}
function text(value,max=2000) {
  if (typeof value !== 'string' || value.length>max) throw new Error('Invalid text in backup.');
  return value;
}
function finite(value,min=0,max=Number.MAX_SAFE_INTEGER) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value<min || value>max) throw new Error('Invalid numeric data in backup.');
  return value;
}
function validId(value) { if (typeof value !== 'string' || !idPattern.test(value)) throw new Error('Invalid record identifier.'); return value; }
export function validateState(input) {
  if (!input || typeof input !== 'object' || !input.profile || !Array.isArray(input.sessions) || !Array.isArray(input.goals)) throw new Error('Invalid backup records.');
  if (input.sessions.length>10000 || input.goals.length>1000) throw new Error('Too many backup records.');
  const profile={name:text(input.profile.name||'',60),goal:text(input.profile.goal||'',300)};
  const sessions=input.sessions.map(s=>{
    if (!Array.isArray(s.results)||s.results.length>1000) throw new Error('Invalid practice results.');
    const results=s.results.map(r=>({
      target_az:finite(r.target_az,-90,90),target_r:finite(r.target_r,0.25,4),
      response_az:finite(r.response_az,-90,90),error_deg:finite(r.error_deg,0,180),
      response_time_s:finite(r.response_time_s,0,86400),kind:text(r.kind,12),
      ...(r.trial_id?{trial_id:validId(r.trial_id)}:{}),...(r.response_id?{response_id:validId(r.response_id)}:{}),
    }));
    if (results.some(r=>!['body','head','low','drop'].includes(r.kind))) throw new Error('Invalid sound type in backup.');
    return {id:validId(s.id),created:finite(s.created),seconds:finite(s.seconds,0,31536000),
      status:s.status==='completed'?'completed':'paused',archived:true,
      results,note:text(s.note||''),goalId:s.goalId?validId(s.goalId):'',child:text(s.child||'',60),
      trials:results.length,error:results.length?results.reduce((a,r)=>a+r.error_deg,0)/results.length:null,
      ...(s.owner_name?{owner_name:text(s.owner_name,64)}:{})};
  });
  const goals=input.goals.map(g=>({id:validId(g.id),title:text(g.title,300),created:finite(g.created),
    targetSessions:finite(g.targetSessions,1,365),achieved:!!g.achieved}));
  if (new Set(sessions.map(s=>s.id)).size!==sessions.length || new Set(goals.map(g=>g.id)).size!==goals.length) throw new Error('Duplicate records in backup.');
  return {profile,sessions,goals,draft:null};
}
export async function createBackup(state, clips) {
  if(clips.length>1000)throw new Error('Select 1,000 or fewer clips and export multiple backups.');
  const clean=validateState({...state,draft:null});
  const files=[], metadata=[];
  let total=0;
  for (const c of clips) {
    validId(c.id);
    if (!(c.blob instanceof Blob)) throw new Error('One clip has no media. Download it separately.');
    total+=c.blob.size;
    if (total>BACKUP_LIMIT-2*1024*1024) throw new Error('Select fewer clips to make backups smaller than 256 MB.');
    const extension=c.kind==='image'?'png':c.blob.type.includes('mp4')?'mp4':'webm';
    const path=`clips/${c.id}.${extension}`;
    const {blob,...meta}=c;
    metadata.push({...meta,path,mime:blob.type}); files.push([path,blob]);
  }
  const manifest={format:'echosense-backup',version:1,exported:Date.now(),state:clean,clips:metadata};
  const manifestBlob=new Blob([JSON.stringify(manifest)],{type:'application/json'});
  if(manifestBlob.size>8*1024*1024)throw new Error('Practice history exceeds the 8 MB backup manifest limit. Download session history separately.');
  files.unshift(['manifest.json',manifestBlob]);
  const backup=await zipFiles(files);
  if (backup.size>BACKUP_LIMIT) throw new Error('This backup exceeds 256 MB. Select fewer clips.');
  return backup;
}
export async function parseBackup(file) {
  if (!file.size || file.size>BACKUP_LIMIT) throw new Error('Choose an EchoSense backup ZIP smaller than 256 MB.');
  const files=readZip(new Uint8Array(await file.arrayBuffer()));
  const raw=files.get('manifest.json');
  if (!raw || raw.length>8*1024*1024) throw new Error('Backup manifest is missing or too large.');
  const manifest=JSON.parse(new TextDecoder().decode(raw));
  if (manifest.format!=='echosense-backup'||manifest.version!==1||!Array.isArray(manifest.clips)||manifest.clips.length>1000) throw new Error('Unsupported EchoSense backup.');
  const state=validateState(manifest.state);
  const clips=manifest.clips.map(c=>{
    const id=validId(c.id), path=text(c.path,140), data=files.get(path);
    if (!path.startsWith(`clips/${id}.`)||!data||!data.length) throw new Error('Backup clip footage is missing.');
    const kind=c.kind==='image'?'image':c.kind==='video'?'video':null;
    const mime=kind==='image'?'image/png':text(c.mime,100);
    if (!kind || !(mime==='image/png'||mime.startsWith('video/webm')||mime.startsWith('video/mp4'))) throw new Error('Unsupported media in backup.');
    return {id,created:finite(c.created),title:text(c.title,100),note:text(c.note||''),
      view:text(c.view,20),source:text(c.source,80),kind,seconds:finite(c.seconds,0,86400),
      sessionId:c.sessionId?validId(c.sessionId):'',blob:new Blob([data],{type:mime})};
  });
  if (new Set(clips.map(c=>c.id)).size!==clips.length) throw new Error('Duplicate clips in backup.');
  return {state,clips};
}
export function mergeBackup(current, existing, backup, replaceProfile=true) {
  const clipIds=new Set(existing.map(c=>c.id)), sessionIds=new Set(current.sessions.map(s=>s.id)), goalIds=new Set(current.goals.map(g=>g.id));
  const added=backup.clips.filter(c=>!clipIds.has(c.id));
  return {state:{...current,profile:replaceProfile?backup.state.profile:current.profile,
    sessions:[...current.sessions,...backup.state.sessions.filter(s=>!sessionIds.has(s.id))].sort((a,b)=>b.created-a.created),
    goals:[...current.goals,...backup.state.goals.filter(g=>!goalIds.has(g.id))]}, clips:added,
    skipped:backup.clips.length-added.length};
}
