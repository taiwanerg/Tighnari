// This frame never receives an account or password. Only a one-use challenge
// token crosses to the exact, server-allowlisted LAN parent origin.
const parentOrigin = new URLSearchParams(location.search).get('parent_origin');
let widget = null;
const publish = token => parent.postMessage({type:'hoshi-turnstile',token},parentOrigin);
window.addEventListener('message',event=>{if(event.origin===parentOrigin&&event.source===parent&&event.data?.type==='hoshi-turnstile-reset'&&widget!==null){publish('');window.turnstile.reset(widget);}});
try {
  const response = await fetch('/hoshi/api/config',{cache:'no-store'});
  if(!response.ok) throw Error('驗證設定無法載入');
  const config = await response.json();
  const script = document.createElement('script');
  script.src='https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';script.async=true;
  script.onload=()=>{widget=window.turnstile.render('#captcha',{sitekey:config.turnstile_site_key,action:'hoshi_login',theme:'dark',callback:publish,'expired-callback':()=>publish(''),'error-callback':()=>publish('')});};
  script.onerror=()=>{document.getElementById('error').textContent='人機驗證載入失敗，請重新整理。';};
  document.head.append(script);
} catch { document.getElementById('error').textContent='人機驗證暫時無法載入。'; }
