/* Shared account controls. Credentials never enter browser storage. */
(() => {
  const byId=id=>document.getElementById(id);
  let csrf=null, account=null, setup=false, invited=false, resolveAccount;
  window.echoSenseAccount=new Promise(resolve=>{resolveAccount=resolve;});
  let channel;
  try { channel=new BroadcastChannel('echosense-auth'); channel.onmessage=()=>lock(); } catch {}
  function lock() {
    account=null; csrf=null;
    if (typeof window.echoSenseLock==='function') window.echoSenseLock();
    else if (!byId('login-form')) location.replace('/login?reason=expired');
  }
  async function request(path,data) {
    const response=await fetch(path,{method:data===undefined?'GET':'POST',credentials:'same-origin',cache:'no-store',
      headers:data===undefined?{}:{'Content-Type':'application/json',...(csrf?{'X-CSRF-Token':csrf}:{})},
      ...(data===undefined?{}:{body:JSON.stringify(data)})});
    const raw=await response.text();
    let result;
    try { result=JSON.parse(raw); } catch { result={error:raw.slice(0,240)||'The device could not process this request.'}; }
    if (!response.ok) {
      if (response.status===401 && path!='/api/auth/login') lock();
      const error=new Error(result.error||'Please try again.'); error.status=response.status; throw error;
    }
    return result;
  }
  window.echoSenseRequest=request;
  function message(text,kind='') {
    const el=byId('auth-message')||byId('account-message');
    if (el) {el.textContent=text;el.className=`auth-message ${kind}`;}
  }
  function renderLogin() {
    const creating=setup||invited;
    byId('auth-title').textContent=setup?'Make this space yours.':invited?'Join their circle.':'Welcome back.';
    byId('auth-description').textContent=setup?'Create the owner account for this EchoSense device.':invited?'Use the username and invitation code shared by the device owner.':'Sign in to your parent or caregiver account.';
    byId('setup-code-field').hidden=!creating;
    byId('setup-code').required=creating;
    byId('code-label').textContent=invited?'Caregiver invitation code':'Device setup code';
    byId('setup-help').textContent=invited?'Invitations expire after 24 hours and can be used once.':'Find this code in the terminal running EchoSense. It lets the device owner create the first account.';
    byId('confirm-field').hidden=!creating; byId('confirm-password').required=creating;
    byId('password-help').hidden=!creating;
    byId('password').autocomplete=creating?'new-password':'current-password';
    byId('password').minLength=creating?15:1;
    byId('auth-submit').textContent=setup?'Create parent account':invited?'Accept invitation':'Sign in';
    byId('recovery-help').hidden=creating;
    byId('invite-toggle').hidden=setup;
    byId('invite-toggle').textContent=invited?'Back to sign in':'Use a caregiver invitation';
    byId('login-form').hidden=false;
  }
  async function refresh() {
    const status=await request('/api/auth/status');
    if (!status.authenticated) { lock(); return status; }
    if(account&&status.user_id!==account.user_id){lock();return {...status,authenticated:false};}
    account=status; csrf=status.csrf;
    return status;
  }
  window.echoSenseReauthenticate=async credentials=>{
    await request('/api/auth/login',credentials);
    const status=await refresh();
    if(!status.authenticated)throw new Error('Sign in again to unlock.');
    return status;
  };
  const loginForm=byId('login-form'), passwordForm=byId('password-form'), logout=byId('logout');
  async function initialize() {
    try {
      const status=await request('/api/auth/status');
      csrf=status.csrf; account=status.authenticated?status:null;
      if (loginForm) {
        if (status.authenticated) {location.replace('/parents');return;}
        setup=status.setup_required; renderLogin();
        const reason=new URLSearchParams(location.search).get('reason');
        message(reason==='expired'?'Your session ended. Sign in to continue.':reason==='changed'?'Password changed. Sign in with your new password.':'');
      } else {
        resolveAccount(status);
        if (!status.authenticated) {lock();return;}
        if (byId('account-name')) byId('account-name').textContent=`${status.username} · ${status.role}`;
        if (logout) logout.disabled=false;
        if (byId('password-submit')) byId('password-submit').disabled=false;
        document.querySelectorAll('[data-permission]').forEach(el=>{el.hidden=!status.permissions.includes(el.dataset.permission);});
        if(document.getElementById('three')){
          if(!status.permissions.includes('configure'))document.querySelectorAll(
            '[data-action]:not([data-action="training"]),#sources button,#scene,#preset,#s-autowalk,#s-bob,#s-blocked,#s-sun,#s-reset,#s-batt,#m-height,#m-pitch,#m-cal,#c-warn,#c-near,#c-announce,#c-mirror,#zone button,#sens button'
          ).forEach(node=>node.disabled=true);
          if(!status.permissions.includes('train'))document.querySelectorAll(
            '[data-action="training"],#t-start,#t-blind,#t-r,#t-kind,#t-az button'
          ).forEach(node=>node.disabled=true);
        }
        setInterval(()=>refresh().catch(()=>{}),15000);
        addEventListener('focus',()=>refresh().catch(()=>{}));
      }
    } catch(error) { resolveAccount({authenticated:false}); message(`${error.message} Check the device connection, then reload.`,'error'); }
  }
  if (loginForm) {
    byId('invite-toggle').onclick=()=>{invited=!invited;renderLogin();message('');};
    byId('show-password').onchange=event=>{
      byId('password').type=event.target.checked?'text':'password';
      byId('confirm-password').type=event.target.checked?'text':'password';
    };
    loginForm.onsubmit=async event=>{
      event.preventDefault();
      if ((setup||invited)&&byId('password').value!==byId('confirm-password').value) {message('Passwords do not match.','error');return;}
      byId('auth-submit').disabled=true; message('Connecting to your family’s space…');
      try {
        await request(`/api/auth/${setup?'setup':invited?'accept-invite':'login'}`,{
          username:byId('username').value.trim(),password:byId('password').value,
          ...((setup||invited)?{code:byId('setup-code').value.trim()}:{})});
        location.replace('/parents');
      } catch(error) {
        message(error.message,'error');byId('password').value='';byId('confirm-password').value='';byId('auth-submit').disabled=false;
      }
    };
  }
  if (logout) logout.onclick=async()=>{
    if (window.echoSenseCanSignOut && !await window.echoSenseCanSignOut()) return;
    logout.disabled=true;
    try {await request('/api/auth/logout',{});channel?.postMessage('lock');lock();location.replace('/login');}
    catch(error) {message(error.message,'error');logout.disabled=false;}
  };
  if (passwordForm) passwordForm.onsubmit=async event=>{
    event.preventDefault();
    if (window.echoSenseCanSignOut && !await window.echoSenseCanSignOut()) return;
    if (byId('new-password').value!==byId('new-password-confirm').value) {message('New passwords do not match.','error');return;}
    byId('password-submit').disabled=true;
    try {await request('/api/auth/password',{current:byId('current-password').value,password:byId('new-password').value});passwordForm.reset();channel?.postMessage('lock');lock();location.replace('/login?reason=changed');}
    catch(error) {message(error.message,'error');byId('password-submit').disabled=false;}
  };
  initialize();
})();
