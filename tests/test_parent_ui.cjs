// Exercise the actual parent handlers with browser APIs replaced at their boundaries.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
(async()=>{
  const modules={...await import('../echosense/web/parent-data.mjs'),...await import('../echosense/web/parent-progress.mjs'),...await import('../echosense/web/parent-privacy.mjs')};
  const elements=new Map();let serial=0;
  function element(id){
    if(!elements.has(id))elements.set(id,{
      id,value:id==='view-select'?'depth':'',disabled:false,hidden:false,textContent:'',innerHTML:'',dataset:{},options:[{},{}],children:[],
      classList:{add(){},remove(){},toggle(){}},setAttribute(){},removeAttribute(){},focus(){},pause(){},
      replaceChildren(...nodes){this.children=[...nodes];},append(...nodes){this.children.push(...nodes);},
      querySelectorAll(){return [];},closest(){return {hidden:true};},
      getContext(){return {clearRect(){}};},
      captureStream(){return {getTracks:()=>[{stop(){}}]};},
    });
    return elements.get(id);
  }
  const directions=[-60,-30,0,30,60].map(az=>{const node=element(`az-${az}`);node.dataset.az=String(az);return node;});
  const commands=[];
  class WebSocket{static OPEN=1;readyState=1;send(text){commands.push(JSON.parse(text));}close(){this.readyState=3;}}
  class MediaRecorder{
    static isTypeSupported(){return true;}
    constructor(){this.state='inactive';this.mimeType='video/webm';}
    start(){this.state='recording';}
    stop(){this.state='inactive';this.ondataavailable({data:new Blob([new Uint8Array([26,69,223,163,1])],{type:'video/webm'})});queueMicrotask(()=>this.onstop());}
  }
  const sandbox={...modules,console,TextDecoder,WebSocket,MediaRecorder,Blob,Date,Math,Map,Set,URL,Option:function(t,v){this.textContent=t;this.value=v;},structuredClone,
    document:{getElementById:element,querySelectorAll:selector=>selector==='[data-az]'?directions:[],querySelector:()=>element('fitting-step'),createElement:tag=>element(`${tag}-${serial++}`),addEventListener(){}},
    location:{hash:'',protocol:'http:',host:'localhost:8080'},history:{replaceState(){}},navigator:{},
    localStorage:{getItem:()=>null,setItem(){}},setInterval(){},addEventListener(){},
    setTimeout,clearTimeout,queueMicrotask,
    window:{echoSenseAccount:new Promise(()=>{}),scrollTo(){},MediaRecorder},
  };
  vm.createContext(sandbox);
  const source=fs.readFileSync('echosense/web/parent.js','utf8').replace(/^import .*;\n/gm,'');
  vm.runInContext(source,sandbox);
  vm.runInContext(`
    gate.unlock();account={user_id:1,username:'parent',role:'owner',permissions:['view','train','configure','accounts']};
    repository={saveState:async()=>{},saveClip:async()=>{throw new Error('No browser storage');},listClips:async()=>[]};
    ws=new WebSocket();state={training:{active:true}};lastFrame=Date.now();ownsConnection=true;
    session={id:'practice-1',created:Date.now(),status:'active',seconds:1,owner_id:1,results:[],trial_id:'trial-1'};
  `,sandbox);
  const answer=directions.find(node=>node.dataset.az==='0').onclick();
  const finish=element('training-finish').onclick();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(commands.filter(m=>m.cmd==='respond').length,1,'Finish shares the in-flight response instead of duplicating it');
  assert.equal(commands.some(m=>m.cmd==='finish'),false,'Finish must wait for the response acknowledgement');
  const response=commands.find(m=>m.cmd==='respond');
  const confirmed={id:'practice-1',created:Date.now(),status:'active',seconds:2,owner_id:1,results:[{trial_id:'trial-1',response_id:response.response_id,target_az:30,target_r:1,response_az:0,error_deg:30,response_time_s:1,kind:'body'}],trials:1,error:30};
  sandbox.confirmed=confirmed;sandbox.responseRequest=response.request_id;
  vm.runInContext("onJson({type:'training_ack',request_id:responseRequest,session:confirmed})",sandbox);
  await new Promise(resolve=>setImmediate(resolve));
  const finishing=commands.find(m=>m.cmd==='finish');assert.ok(finishing);
  sandbox.finishRequest=finishing.request_id;sandbox.completed={...confirmed,status:'completed'};
  vm.runInContext("onJson({type:'training_ack',request_id:finishRequest,session:completed})",sandbox);
  await Promise.all([answer,finish]);
  assert.equal(vm.runInContext('household.sessions[0].results.length',sandbox),1,'The final response survives finishing');
  // No IndexedDB is needed to start capturing; failed saves expose a persistent recovery copy.
  vm.runInContext("state.source='sim';state.training.active=false;lastFrame=Date.now();",sandbox);
  element('record').onclick();
  assert.equal(vm.runInContext('recorder.state',sandbox),'recording');
  await vm.runInContext('stopRecording()',sandbox);
  assert.equal(vm.runInContext('recoveries.length',sandbox),1);
  assert.equal(element('recording-recovery').hidden,false);
  // An expired session hides the app immediately, while preservation runs asynchronously.
  const locking=sandbox.window.echoSenseLock();
  assert.equal(element('private-app').hidden,true);
  assert.equal(element('private-app').inert,true);
  await locking;
  assert.equal(vm.runInContext('recoveries.length',sandbox),1,'The lock retains unsaved media in this open page');
  assert.match(element('privacy-message').textContent,/kept in this open page/);
  sandbox.window.echoSenseReauthenticate=async()=>({user_id:1,username:'parent',role:'owner',permissions:['view','train','configure','accounts']});
  vm.runInContext("clips=[{id:'saved-1',title:'Saved clip',created:Date.now(),kind:'video',view:'depth',source:'sim',seconds:1,note:'',blob:new Blob(['test-footage'],{type:'video/webm'})}]",sandbox);
  await element('unlock-form').onsubmit({preventDefault(){}});
  const rebuiltUrl=vm.runInContext('clipUrls[0]',sandbox);
  assert.ok(rebuiltUrl,'unlock rebuilds media URLs revoked by the privacy lock');
  assert.equal(await (await fetch(rebuiltUrl)).text(),'test-footage');
  assert.equal(element('private-app').hidden,false);
  console.log('Parent handlers: in-flight Finish, no-storage recording, recovery, privacy lock, and media recovery passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
