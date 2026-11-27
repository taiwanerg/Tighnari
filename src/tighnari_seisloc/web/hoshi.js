import { $, node, time, fmt, magnitude, depthText } from './common.js?v=20261127-best-fit-v1';
import { jobLines, mechanismCard, selectionSummary } from './mechanisms.js?v=20261127-cap-band-v1';
let csrf = '', offset = 0, selected = null, requestVersion = 0, csrfWidget = null, bridgeFrame = null, bridgeToken = "", busy = false;
let detailVersion = 0, mechanismRequest = null, displayedMechanisms = null;
const size = 30, labels = {pending:'待確認',automatic:'',confirmed:'已人工確認'};
function message(text, error = false) { $('message').textContent = text; $('message').hidden = !text; $('message').classList.toggle('danger',error); }
async function api(path, options = {}) {
  const response = await fetch('/hoshi/api/' + path, {cache:'no-store', ...options, headers:{'Content-Type':'application/json', ...(csrf ? {'X-CSRF-Token':csrf} : {}), ...options.headers}});
  let data = {}; try { data = await response.json(); } catch {}
  if (!response.ok) { if (response.status === 401 && path !== 'login') signedOut(); throw Error(typeof data.detail === 'string' ? data.detail : '資料格式有誤，請檢查輸入內容。'); }
  return data;
}
function signedOut() { csrf='';selected=null;detailVersion++;$('workspace').hidden=true;$('logout').hidden=true;$('login-panel').hidden=false;$('editor').hidden=true;$('empty-selection').hidden=false; }
async function signedIn(data) { csrf=data.csrf_token;$('password').value='';$('login-panel').hidden=true;$('workspace').hidden=false;$('logout').hidden=false;await listing(); }
function tags(review) { const out=node('div',{class:'tags'});if(labels[review.status]) out.append(node('span',{class:'hoshi-tag '+review.status},labels[review.status]));if(review.error) out.append(node('span',{class:'hoshi-tag error'},'錯誤事件'));if(review.deleted) out.append(node('span',{class:'hoshi-tag'},'已刪除'));if(review.magnitude_override!==null && review.magnitude_override!==undefined) out.append(node('span',{class:'hoshi-tag'},'人工 ML'));return out; }
async function listing() {
  const version=++requestVersion;
  const data=await api('events?'+new URLSearchParams({q:$('search').value,review:$('filter').value,offset,limit:size}));if(version!==requestVersion)return;
  $('total').textContent=`共 ${data.total} 筆`;$('page').textContent=`第 ${Math.floor(offset/size)+1} 頁`;$('prev').disabled=offset===0;$('next').disabled=offset+size>=data.total;
  $('events').replaceChildren(...data.items.map(eventRow));
  if(!data.items.length)$('events').append(node('p',{class:'muted'},'沒有符合條件的事件。'));
}
function eventRow(event) {
  const row = node('div', {class:'hoshi-row'});
  const place = event.place || '推估震央';
  const detail = node('button', {type:'button', class:'hoshi-event'+(selected?.id===event.id?' selected':''), 'data-event-id':event.id});
  detail.append(node('strong',{},place), node('small',{},time(event.origin)+' · ML '+fmt(magnitude(event))+' · r'+event.revision), tags(event.review));
  detail.onclick = () => openEvent(event.id).catch(error => message(error.message, true));
  row.append(detail);
  if (!event.review.deleted) {
    const remove = node('button', {type:'button', class:'danger hoshi-row-delete', 'aria-label':`刪除事件 ${place} ${time(event.origin)} ${event.id}`}, '刪除');
    remove.onclick = () => toggleDeleted(true, event);
    row.append(remove);
  }
  return row;
}
async function openEvent(id) {
  if (busy) return;
  const version = ++detailVersion;
  const data = await api('events/'+encodeURIComponent(id));
  if (version !== detailVersion) return;
  renderEditor(data);
  document.querySelectorAll('.hoshi-event').forEach(button => button.classList.toggle('selected', button.dataset.eventId === id));
}
function renderMechanisms(eid, data) {
  const section = $('mechanism-research');
  section.hidden = !data;
  if (!data) {
    displayedMechanisms = null;
    return;
  }
  const key = JSON.stringify([eid, data]);
  if (key === displayedMechanisms) return;
  displayedMechanisms = key;
  $('mechanism-research-state').textContent = selectionSummary(data) + ' · 後台診斷';
  const imageBase = '/hoshi/api/events/' + encodeURIComponent(eid) + '/mechanisms/';
  $('mechanism-research-candidates').replaceChildren(
    ...jobLines(data), ...data.candidates.map(row => mechanismCard(row, imageBase)));
}
async function loadMechanisms() {
  if (!selected || mechanismRequest || document.hidden) return;
  const id = selected.id;
  const request = api('events/' + encodeURIComponent(id) + '/mechanisms');
  mechanismRequest = request;
  try {
    const result = await request;
    if (selected?.id === id) renderMechanisms(id, result);
  } catch {
    if (selected?.id === id && !$('mechanism-research').hidden) {
      displayedMechanisms = null;
      $('mechanism-research-state').textContent = '暫時無法更新，稍後重試。';
    }
  } finally {
    mechanismRequest = null;
    if (selected && selected.id !== id) void loadMechanisms();
  }
}
setInterval(loadMechanisms, 10000);
function renderEditor(data) {
  if (selected?.id !== data.event.id) renderMechanisms(data.event.id, null);
  selected=data.event;
  void loadMechanisms();
  const r=selected.review;$('empty-selection').hidden=true;$('editor').hidden=false;
  $('event-title').textContent=selected.place||'推估震央';$('event-info').textContent=`${time(selected.origin)} UTC+8 · ${fmt(selected.lat,4)}°N / ${fmt(selected.lon,4)}°E · 深度 ${depthText(selected)}\n${selected.id} · 第 ${selected.revision} 次修訂`;
  $('public-link').href='/events/'+encodeURIComponent(selected.id);$('public-link').hidden=r.deleted;$('deleted-notice').hidden=!r.deleted;
  $('review-status').value=r.status;$('comment').value=r.comment;$('error-event').checked=r.error;$('manual-ml').checked=r.magnitude_override!==null;$('ml').value=r.magnitude_override??magnitude(selected)??'';$('ml').disabled=!$('manual-ml').checked;$('reason').value='';$('delete').hidden=r.deleted;$('restore').hidden=!r.deleted;
  $('audit').replaceChildren(...data.audit.map(a=>{const entry=node('div',{class:'audit-entry'});entry.append(node('small',{class:'muted'},time(a.created_at)),node('p',{},a.reason));const changes=[];for(const [key,label] of [['status','確認狀態'],['magnitude_override','手動 ML'],['comment','公開註解'],['error','錯誤標記'],['deleted','刪除狀態']])if(a.before[key]!==a.after[key]) {const display=v=>key==='status'?(labels[v]||'自動（無標記）'):typeof v==='boolean'?(v?'是':'否'):v??'自動';changes.push(`${label}：${display(a.before[key])} → ${display(a.after[key])}`);}entry.append(node('p',{class:'muted'},changes.join('\n')));return entry;}));if(!data.audit.length)$('audit').append(node('p',{class:'muted'},'尚無人工操作紀錄。'));
}
async function save(changes, reason, event = selected) {
  if (busy || !event) return;
  busy = true;
  detailVersion++;
  document.querySelectorAll('#edit-form button, .hoshi-row-delete').forEach(button => button.disabled = true);
  try {
    const result = await api('events/'+encodeURIComponent(event.id), {
      method:'PATCH', body:JSON.stringify({expected_revision:event.revision, reason, ...changes}),
    });
    if (selected?.id === event.id) renderEditor(result);
    await listing();
    message(changes.deleted === true ? '事件已刪除。' : changes.deleted === false ? '事件已還原。' : '變更已儲存，公開資料已更新。');
  } catch (error) {
    message(error.message, true);
  } finally {
    busy = false;
    document.querySelectorAll('#edit-form button, .hoshi-row-delete').forEach(button => button.disabled = false);
  }
}
$('edit-form').onsubmit=async e=>{e.preventDefault();if($('manual-ml').checked&&!$('ml').value){message('請填寫手動規模。',true);return;}await save({status:$('review-status').value,comment:$('comment').value,error:$('error-event').checked,magnitude_override:$('manual-ml').checked?Number($('ml').value):null},$('reason').value);};
$('manual-ml').onchange=()=>{$('ml').disabled=!$('manual-ml').checked;};
async function toggleDeleted(deleted, event = selected) {
  const dialog = $('confirm-dialog');
  if (!event || busy || dialog.open) return;
  $('confirm-title').textContent = deleted ? '刪除事件？' : '還原事件？';
  $('confirm-event').textContent = `${event.place || '推估震央'} · ${time(event.origin)} · ${event.id}`;
  $('confirm-text').textContent = deleted ? '事件將從公開頁及列表隱藏，可隨時從後台還原。' : '事件將恢復公開可見；若仍標記為錯誤事件，則繼續從公開列表排除。';
  $('confirm-action').textContent = deleted ? '刪除事件' : '還原事件';
  $('confirm-reason').value = '';
  dialog.returnValue = 'cancel';
  dialog.showModal();
  const choice = await new Promise(resolve => dialog.addEventListener('close', () => resolve(dialog.returnValue), {once:true}));
  if (choice === 'confirm') await save({deleted}, $('confirm-reason').value, event);
}
$('delete').onclick=()=>toggleDeleted(true);$('restore').onclick=()=>toggleDeleted(false);
$('filters').onsubmit=e=>{e.preventDefault();offset=0;listing().catch(error=>message(error.message,true));};$('refresh').onclick=()=>{listing().then(()=>selected?openEvent(selected.id):null).catch(error=>message(error.message,true));};$('prev').onclick=()=>{offset=Math.max(0,offset-size);listing().catch(error=>message(error.message,true));};$('next').onclick=()=>{offset+=size;listing().catch(error=>message(error.message,true));};
$('login-form').onsubmit=async e=>{e.preventDefault();$('login-submit').disabled=true;message('');try{const token=bridgeFrame?bridgeToken:csrfWidget!==null?window.turnstile.getResponse(csrfWidget):'';const result=await api('login',{method:'POST',body:JSON.stringify({username:$('username').value,password:$('password').value,turnstile_token:token})});await signedIn(result);}catch(error){message(error.message,true);}finally{$('password').value='';$('login-submit').disabled=false;if(bridgeFrame){bridgeToken='';bridgeFrame.contentWindow.postMessage({type:'hoshi-turnstile-reset'},new URL(bridgeFrame.src).origin);}else if(csrfWidget!==null)window.turnstile.reset(csrfWidget);}};
$('logout').onclick=async()=>{try{await api('logout',{method:'POST',body:'{}'});signedOut();message('已安全登出。');}catch(error){message(error.message,true);}};
async function init(){const config=await api('config');if(config.turnstile_required){if(!config.turnstile_site_key){$('login-submit').disabled=true;message('人機驗證尚未完成設定，請聯絡維護者。',true);return;}if(config.turnstile_frame_origin && config.turnstile_frame_origin!==location.origin){
  bridgeFrame=document.createElement('iframe');bridgeFrame.title='Cloudflare 人機驗證';bridgeFrame.className='hoshi-captcha-frame';bridgeFrame.src=config.turnstile_frame_origin+'/hoshi/challenge?'+new URLSearchParams({parent_origin:location.origin});$('captcha').append(bridgeFrame);
  window.addEventListener('message',event=>{if(event.origin===config.turnstile_frame_origin&&event.source===bridgeFrame.contentWindow&&event.data?.type==='hoshi-turnstile'&&typeof event.data.token==='string')bridgeToken=event.data.token;});
}else{const script=document.createElement('script');script.src='https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';script.async=true;script.onload=()=>{csrfWidget=window.turnstile.render('#captcha',{sitekey:config.turnstile_site_key,action:'hoshi_login',theme:'dark'});};script.onerror=()=>message('人機驗證載入失敗，請重新整理。',true);document.head.append(script);}}try{await signedIn(await api('session'));}catch(error){if(!error.message.includes('請先登入')&&!error.message.includes('登入已到期'))message(error.message,true);}}
init().catch(error=>message(error.message,true));
