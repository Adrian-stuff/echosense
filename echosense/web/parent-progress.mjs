const localDay = timestamp => {
  const d=new Date(timestamp);
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
};
export function progressReport(state, clips=[], now=Date.now()) {
  const sessions=state.sessions.filter(s=>s.status==='completed');
  const byKind=new Map(), days=new Set(sessions.filter(s=>s.results?.length).map(s=>localDay(s.created)));
  for (const session of sessions) for (const trial of session.results||[]) {
    const stats=byKind.get(trial.kind)||{kind:trial.kind,count:0,total:0,seconds:0};
    stats.count++; stats.total+=trial.error_deg; stats.seconds+=trial.response_time_s;
    byKind.set(trial.kind,stats);
  }
  const lastSeven=Array.from({length:7},(_,i)=>{
    const date=new Date(now); date.setDate(date.getDate()-6+i);
    const day=localDay(date); return {day,practiced:days.has(day)};
  });
  let streak=0; const day=new Date(now);
  if (!days.has(localDay(day))) day.setDate(day.getDate()-1);
  while(days.has(localDay(day))) { streak++; day.setDate(day.getDate()-1); }
  return {sessions:sessions.length,minutes:sessions.reduce((sum,s)=>sum+s.seconds,0)/60,
    practicedDays:lastSeven.filter(d=>d.practiced).length,lastSeven,streak,
    sounds:[...byKind.values()].map(s=>({kind:s.kind,count:s.count,error:s.total/s.count,responseSeconds:s.seconds/s.count})),
    goals:state.goals.map(g=>({...g,sessions:sessions.filter(s=>s.goalId===g.id).length})),
    observations:sessions.filter(s=>s.note).map(s=>({id:s.id,created:s.created,note:s.note,
      clips:clips.filter(c=>c.sessionId===s.id).map(c=>({id:c.id,title:c.title}))}))};
}
function csvCell(value) {
  let text=String(value??'');
  if (/^[=+@\-\t\r]/.test(text)) text=`'${text}`;
  return `"${text.replaceAll('"','""')}"`;
}
export function reportCSV(state,clips=[]) {
  const rows=[['Child','Practice date','Session ID','Goal','Sound','Target direction (degrees)',
    'Pointed direction (degrees)','Direction error (degrees)','Response time (seconds)','Observation','Linked clips']];
  for (const s of state.sessions.filter(s=>s.status==='completed')) {
    const goal=state.goals.find(g=>g.id===s.goalId)?.title||'';
    const linked=clips.filter(c=>c.sessionId===s.id).map(c=>c.title).join('; ');
    for (const r of (s.results?.length?s.results:[{}])) rows.push([
      state.profile.name||'',new Date(s.created).toISOString(),s.id,goal,r.kind||'',
      r.target_az??'',r.response_az??'',r.error_deg??'',r.response_time_s??'',s.note||'',linked]);
  }
  return '\uFEFF'+rows.map(row=>row.map(csvCell).join(',')).join('\r\n');
}
