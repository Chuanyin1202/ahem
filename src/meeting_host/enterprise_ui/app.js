const $ = s => document.querySelector(s);
const roles = {manager:'管理版',operator:'企業版',viewer:'會議閱覽',observer:'受限觀察者',support:'SaaS 服務後台'};
const labels = {analytics:'會議分析',trends:'每日匯入統計',incidents:'事故處理',notifications:'站內通知','alert-rules':'通知規則',meetings:'內容與授權',health:'服務狀態',audit:'存取稽核',members:'成員工作階段',account:'我的工作階段'};
labels['meeting-trends']='會議日期分析';
const descriptions = {
 analytics:'比較會議時長與主席介入，這裡不載入姓名或逐字稿。',
 meetings:'逐場授權。受限會議須由管理員配置內容許可。',
 health:'只呈現服務代碼與最近回報狀態，不包含客戶會議內容。',
 audit:'追蹤存取與授權結果；紀錄不含逐字稿。',
 members:'管理成員啟用狀態，或只終止登入中的工作階段。',
 trends:'依 UTC 匯入日期統計最近 30 天仍在保存期內的會議，不是會議發生日期。',
 incidents:'手動登錄或由已啟用規則建立事故；恢復通知不會自動結案。',
 notifications:'只包含服務代碼與狀態；已讀不代表事故結案。此頁需重新整理，不發送外部通知。',
 'alert-rules':'依收到的狀態回報建立站內通知與事故。預設關閉，未接上真實服務的自動監測。',
 account:'查看目前身分與到期時間，或登出此帳號的所有工作階段。'
};
let identity, current = 'analytics', revision = 0;
let trendDays=30;
descriptions['meeting-trends']='依管理員填寫的會議日期分析目前仍保存的資料；不是匯入日期，也不是會議效率評分。';
let expiryTimer, accessTimer;
const filters = {q:'',policy:'all',outcome:'all',offset:0};
const el = (tag, text, cls) => {
 const node = document.createElement(tag);
 if (text !== undefined) node.textContent = text;
 if (cls) node.className = cls;
 return node;
};
function error(e) { $('#status').className = 'error'; $('#status').textContent = e.message; }
function signedOut() {
 clearTimeout(expiryTimer); clearInterval(accessTimer);
 revision++;
 identity = null;
 $('#workspace').hidden = true; $('#login').hidden = false; $('#logout').hidden = true;
 $('#panel').replaceChildren(); $('#token').value = '';
 document.body.classList.remove('authenticated'); $('#sidebar').hidden = true; $('#context').hidden = true;
}
async function api(path, body, method) {
 const response = await fetch('/api/' + path, {
  method: method || (body ? 'POST' : 'GET'),
  headers: body ? {'Content-Type':'application/json'} : {},
  body: body ? JSON.stringify(body) : undefined
 });
 const data = await response.json();
 if (!response.ok) {
  if (response.status === 401) signedOut();
  const messages = {401:'憑證無效或工作階段已到期，請重新登入。',403:'此角色沒有存取權限。',404:'會議不存在、已到期，或不在您的授權範圍內。',429:'嘗試次數過多，請稍後再試。',400:'資料格式不正確，請檢查輸入內容。'};
  throw Error(messages[response.status] || '服務暫時無法完成操作，請稍後再試。');
 }
 return data;
}
function button(text, action, cls) {
 const b = el('button', text, cls); b.type = 'button';
 b.onclick = async () => {
  if (b.disabled) return;
  const epoch = revision; b.disabled = true; b.setAttribute('aria-busy','true');
  try { await action(); } catch(e) { if (epoch === revision || !identity) error(e); }
  finally { b.disabled = false; b.removeAttribute('aria-busy'); }
 };
 return b;
}
function field(text, control) {
 const box = el('div', undefined, 'field'), label = el('label', text);
 control.id = 'field-' + text; label.htmlFor = control.id; control.setAttribute('aria-label',text);
 box.append(label, control); return box;
}
function select(options) {
 const node = el('select');
 for (const [value, label] of options) { const o = el('option',label); o.value = value; node.append(o); }
 return node;
}
function table(headers, rows) {
 const wrap = el('div',undefined,'table-wrap'), t = el('table'), head = el('tr'), thead = el('thead'), body = el('tbody');
 headers.forEach(x => { const h = el('th',x); h.scope = 'col'; head.append(h); });
 thead.append(head);
 rows.forEach(row => { const tr = el('tr'); row.forEach(x => { const td = el('td'); td.append(x instanceof Node ? x : el('span',String(x))); tr.append(td); }); body.append(tr); });
 t.append(thead,body); wrap.append(t); return wrap;
}
async function start() {
 identity = await api('me');
 clearTimeout(expiryTimer);
 expiryTimer=setTimeout(()=>{signedOut();error(Error('工作階段已到期，請重新登入。'));},Math.max(0,identity.expires_at*1000-Date.now()));
 $('#login').hidden = true; $('#workspace').hidden = false; $('#logout').hidden = false;
 document.body.classList.add('authenticated'); $('#sidebar').hidden = false; $('#context').hidden = false;
 $('#context').textContent = identity.tenant + ' / ' + (identity.regulated_content ? '受限內容管理' : roles[identity.role]);
 if(identity.demo_mode)$('#context').textContent+=' / 合成資料演練';
 $('#role-art').className = identity.regulated_content ? 'cleared' : identity.role;
 const tabs = [...(identity.role === 'operator' ? ['analytics','trends','meetings','health','incidents','notifications','alert-rules','audit','members'] : identity.role === 'support' ? ['health','incidents','notifications'] : identity.role === 'viewer' ? ['meetings'] : ['analytics','trends']),'account'];
 if(['operator','manager','observer'].includes(identity.role))tabs.splice(2,0,'meeting-trends');
 current = tabs[0];
 $('#nav').replaceChildren(...tabs.map(t => {
  const b = button(labels[t],async () => { current=t; filters.offset=0; await render(); });
  b.prepend(navIcon(t)); return b;
 }));
 await render();
}
$('#login-form').onsubmit = async e => {
 e.preventDefault();
 const b = e.currentTarget.querySelector('button'); if (b.disabled) return;
 const token = $('#token').value; $('#token').value = ''; $('#status').textContent = '';
 b.disabled = true; b.textContent = '登入中…';
 try { await api('login',{token}); await start(); } catch(e) { error(e); }
 finally { b.disabled = false; b.textContent = '進入工作台'; }
};
$('#logout').onclick = async () => {
 $('#logout').disabled = true;
 try { await api('logout',{}); signedOut(); $('#status').textContent=''; $('#token').focus(); }
 catch(e) { error(e); } finally { $('#logout').disabled = false; }
};
$('#refresh').onclick = () => render().catch(error);

async function render() {
 if (!identity) return;
 clearInterval(accessTimer);
 const epoch = ++revision, page = current, pane = $('#panel');
 pane.replaceChildren(el('p','正在載入…','loading')); pane.setAttribute('aria-busy','true');
 $('#refresh').disabled = true; $('#status').textContent = '';
 $('#title').textContent = labels[page]; $('#description').textContent = descriptions[page];
 [...$('#nav').children].forEach(b => { const active=b.textContent===labels[page]; b.classList.toggle('active',active); if(active)b.setAttribute('aria-current','page'); else b.removeAttribute('aria-current'); });
 try {
  let path=page==='meetings'?'analytics':page==='account'?'me':page;
  if(['analytics','meetings','audit'].includes(page))path+='?'+new URLSearchParams({limit:20,offset:filters.offset,...(page==='audit'?{outcome:filters.outcome}:{q:filters.q,policy:filters.policy})});
  if(page==='incidents')path+='?'+new URLSearchParams({limit:20,offset:filters.offset});
  if(page==='notifications')path+='?'+new URLSearchParams({limit:20,offset:filters.offset});
  if(page==='meeting-trends')path+='?days='+trendDays;
  const data = await api(path);
  if(page==='health')data.history=await api('health/history');
  if (epoch !== revision || !identity) return;
  const fragment = document.createDocumentFragment();
  if (page === 'health') renderHealth(fragment,data);
  else if (page === 'audit') renderAudit(fragment,data);
  else if (page === 'members') renderMembers(fragment,data);
  else if (page === 'account') renderAccount(fragment,data);
  else if (page === 'incidents') renderIncidents(fragment,data);
  else if (page === 'trends') renderTrends(fragment,data);
  else if (page === 'meeting-trends') renderMeetingTrends(fragment,data);
  else if (page === 'notifications') renderNotifications(fragment,data);
  else if (page === 'alert-rules') renderAlertRules(fragment,data);
  else renderMeetings(fragment,data,page);
  pane.replaceChildren(fragment);
 } catch(e) {
  if (epoch === revision) { pane.replaceChildren(el('p','無法載入資料，請使用重新整理再試。','empty')); error(e); }
 } finally {
  if (epoch === revision || !identity) { pane.setAttribute('aria-busy','false'); $('#refresh').disabled=false; }
 }
}
function renderHealth(pane,data) {
 const names = {chair:'主席判斷',discord:'Discord 連線',stt:'即時轉錄',tts:'主席語音'};
 const states = {unknown:'尚無有效回報',ok:'正常',degraded:'效能下降',unavailable:'無法使用'};
 if(identity.demo_mode){
  pane.append(el('p','合成資料演練：這些狀態由示範操作產生，不代表真實 Discord／語音服務健康情況。','error'));
  if(identity.role==='operator'){
   const component=select(Object.entries(names)),state=select(Object.entries(states)),form=el('div',undefined,'import');
   form.append(field('演練服務',component),field('演練狀態',state),button('送出合成狀態',async()=>{
    await api('health',{component:component.value,state:state.value});await render();
   }));pane.append(form);
  }
 }
 for (const c of data.components) {
  const row=el('div',undefined,'row'), title=el('div');
  title.append(el('h2',names[c.component]),el('p',c.updated_at ? '最後回報：'+new Date(c.updated_at*1000).toLocaleString() : '等待服務監測回報','small'));
  row.append(title,el('strong',states[c.state],'state '+c.state)); pane.append(row);
 }
 pane.append(el('h2','最近狀態變更'),el('p','僅記錄收到的狀態回報；沒有回報不代表服務正常。最多顯示 100 筆，保存 30 天。','small'));
 pane.append(table(['時間','服務','狀態'],data.history.entries.map(x=>[new Date(x.at*1000).toLocaleString(),names[x.component],states[x.state]])));
}
function renderAudit(pane,data) {
 const outcome=select([['all','全部結果'],['ok','成功'],['denied','拒絕']]);outcome.value=filters.outcome;
 outcome.onchange=()=>{filters.outcome=outcome.value;filters.offset=0;render().catch(error);};pane.append(field('稽核結果',outcome));
 const actions={login:'登入',import:'匯入會議',grant:'授予閱覽',revoke:'撤銷閱覽',delete:'刪除會議',access:'權限檢查',content:'內容讀取','content:meeting_review':'讀取：會議回顧','content:incident_review':'讀取：事件調查'};
 if (!data.entries.length) { pane.append(el('p','目前尚無存取紀錄。','empty')); return; }
 pane.append(table(['時間','操作身分指紋','動作','結果'],data.entries.map(x => [new Date(x.at*1000).toLocaleString(),x.actor,actions[x.action]||x.action,x.outcome==='ok'?'成功':'拒絕'])));
 paginate(pane,data);
}
function paginate(pane,data){
 const bar=el('div',undefined,'actions');
 const prev=button('上一頁',async()=>{filters.offset=Math.max(0,filters.offset-data.limit);await render();},'secondary');prev.disabled=!data.offset;
 const next=button('下一頁',async()=>{filters.offset+=data.limit;await render();},'secondary');next.disabled=data.offset+data.limit>=data.total_count;
 bar.append(prev,el('span',`共 ${data.total_count} 筆 · 第 ${Math.floor(data.offset/data.limit)+1} 頁`),next);pane.append(bar);
}
function renderMembers(pane,data){
 pane.append(el('p','限時憑證須由管理員透過安全管道交付；沒有自動寄送 Email。恢復成員不會延長到期憑證；輪替不會解除停用。','small'));
 pane.append(button('新增限時成員',()=>credentialForm(),'secondary'));
 pane.append(table(['成員','角色','受限內容許可','登入中','操作'],data.members.map(a=>{
 const actions=el('div',undefined,'actions');actions.append(el('span',a.suspended?'已停用':a.enabled?'已啟用':'憑證已到期'));
 if(a.credential_expires_at)actions.append(el('span','憑證到期：'+new Date(a.credential_expires_at*1000).toLocaleString(),'small'));
 if(a.can_manage)actions.append(button(a.suspended?'恢復成員':'停用成員',async()=>{
  if(confirm(a.suspended?'恢復此成員？到期憑證仍需輪替。':'停用此成員並撤銷登入？')){await api('members/status',{actor:a.id,enabled:a.suspended});await render();}
 },a.suspended?'secondary':'danger'),button('輪替憑證',()=>credentialForm(a),'secondary'));
 actions.append(button('終止工作階段',async()=>{
  if(!confirm('終止此成員的所有登入工作階段？不會停用憑證。'))return;
  await api('members/revoke-sessions',{actor:a.id});await render();
 },'danger'));return [a.id,roles[a.role],a.regulated_content?'有':'無',a.sessions,actions];})));
}
function credentialForm(member){
 const pane=detailPane(member?'輪替成員憑證':'新增限時成員'),form=el('form',undefined,'import'),name=el('input'),days=el('input');
 name.required=true;name.pattern='(?:[a-z0-9_]|-){3,48}';name.maxLength=48;name.value=member?.id||'';name.readOnly=!!member;
 days.type='number';days.min=1;days.max=30;days.step=1;days.value=7;days.required=true;
 const role=select([['viewer','與會閱覽'],['manager','主管統計'],['observer','觀察統計'],['support','客服維運']]);
 const submit=el('button',member?'確認撤銷舊憑證':'產生限時憑證');submit.type='submit';
 form.append(field('成員代碼',name),field('憑證有效天數',days));if(!member)form.append(field('新增成員角色',role));form.append(submit);pane.append(form);
 pane.append(el('p','使用不含個資的代碼。此介面不能建立管理員或授予受限內容許可。輪替立即撤銷舊憑證與登入工作階段。','small'));
 form.onsubmit=async e=>{
  e.preventDefault();if(submit.disabled)return;
  if(member&&!confirm('立即使此成員的舊憑證與登入失效？'))return;
  submit.disabled=true;const epoch=revision;
  try{
   const result=await api('members/'+(member?'rotate':'create'),{actor:name.value,role:role.value,days:Number(days.value)});
   if(epoch!==revision||!identity||!pane.isConnected)return;
   pane.replaceChildren(el('h2','憑證僅顯示這一次'),el('p','請私下交付。兩分鐘後此畫面會清除；遺失時需再次輪替，不可查回明文。','small'));
   const token=el('input');token.type='password';token.readOnly=true;token.value=result.token;token.autocomplete='off';
   pane.append(field('新存取憑證',token),button('顯示／隱藏',()=>{token.type=token.type==='password'?'text':'password';},'secondary'),button('我已保存，關閉',async()=>{token.value='';pane.remove();await render();}));
   setTimeout(()=>{token.value='';pane.remove();},120000);
  }catch(e){if(epoch===revision)error(e);}finally{submit.disabled=false;}
 };
}
function renderTrends(pane,data){
 pane.append(el('p',`有 ${data.unknown_import_dates} 場舊資料缺少匯入時間，未納入下表；刪除或到期後也不再計入。此表不是效率評分。`,'small'));
 pane.append(table(['匯入日期 UTC','會議數','累計分鐘','主席介入'],data.days.map(x=>[x.day,x.meetings,x.minutes,x.interventions])));
 if(!data.days.length)pane.append(el('p','尚無具匯入日期的資料，請先匯入合成事件檔。','empty'));
}
function renderMeetingTrends(pane,data){
 const window=select([['30','最近 30 天'],['90','最近 90 天'],['365','最近 365 天']]);window.value=String(trendDays);
 window.onchange=()=>{trendDays=Number(window.value);render().catch(error);};pane.append(field('分析期間',window));
 pane.append(el('p',`資料來源：管理員填寫。有 ${data.missing_dates} 場未填日期，不猜測補值。到期或刪除後不再計入；每 10 分鐘介入次數只是頻率，不代表品質。`,'small'));
 pane.append(table(['會議日期','會議數','平均分鐘','主席介入','每 10 分鐘介入'],data.days.map(x=>[x.day,x.meetings,x.average_minutes,x.interventions,x.interventions_per_10_minutes??'無法計算'])));
 if(!data.days.length)pane.append(el('p','此期間尚無具會議日期的資料，管理員可在「內容與授權」填寫日期。','empty'));
}
function meetingDate(m){
 const pane=detailPane('填寫會議日期'),form=el('form',undefined,'import'),input=el('input');input.type='date';input.required=true;input.max=new Date().toISOString().slice(0,10);
 const submit=el('button','儲存會議日期');submit.type='submit';form.append(field('會議日期',input),submit);pane.append(el('p','請填寫實際會議日期；系統記錄為管理員提供，不會自行推測。','small'),form);
 form.onsubmit=async e=>{e.preventDefault();if(submit.disabled)return;submit.disabled=true;const epoch=revision;
  try{await api('meetings/'+m.id+'/date',{day:input.value});if(epoch===revision)await render();}catch(e){if(epoch===revision)error(e);}finally{submit.disabled=false;}
 };
}
function renderAlertRules(pane,data){
 pane.append(el('p','啟用後會立即檢查已有回報；異常或超過 5 分鐘未更新會產生通知。相同狀態不重複通知；無任何回報時不會推測故障。恢復正常僅通知，事故由人員確認結案。','small'));
 for(const r of data.rules){
  const row=el('div',undefined,'row');
  row.append(el('strong',r.component),el('span',r.enabled?'已啟用':'已關閉'),button(r.enabled?'關閉規則':'啟用規則',async()=>{
   await api('alert-rules',{component:r.component,enabled:!r.enabled});await render();
  },'secondary'));pane.append(row);
 }
}
function renderNotifications(pane,data){
 const kinds={degraded:'效能下降',unavailable:'無法使用',unknown:'回報未知或已過期',recovered:'收到恢復正常回報'};
 if(!data.entries.length)pane.append(el('p','目前沒有通知。需由管理員啟用規則，並有服務狀態回報。','empty'));
 pane.append(table(['時間','服務','通知','狀態'],data.entries.map(n=>[new Date(n.at*1000).toLocaleString(),n.component,kinds[n.kind],n.is_read?'已讀':button('標示已讀',async()=>{
  await api('notifications/'+n.id+'/read',{});await render();
 },'secondary')])));
 pane.append(el('p','通知保留 30 天；已讀狀態依每位使用者分開保存。','small'));paginate(pane,data);
}
function renderIncidents(pane,data){
 const component=select([['chair','主席判斷'],['discord','Discord'],['stt','即時轉錄'],['tts','主席語音']]),severity=select([['warning','警告'],['critical','嚴重']]);
 const form=el('div',undefined,'import');form.append(field('事故服務',component),field('嚴重程度',severity),button('登錄事故',async()=>{
  try{await api('incidents',{component:component.value,severity:severity.value});await render();}
  catch(e){throw Error('無法登錄：請檢查是否已有該服務未結案事故，或重新登入後再試。');}
 }));pane.append(form);
 const states={open:'待確認',acknowledged:'處理中',resolved:'已結案'};
 pane.append(table(['服務','程度','狀態','最後更新','操作'],data.entries.map(x=>[x.component,x.severity==='critical'?'嚴重':'警告',states[x.status],new Date(x.updated*1000).toLocaleString(),x.status==='resolved'?'—':button(x.status==='open'?'確認事故':'結案',async()=>{
  if(confirm('更新此事故狀態？結案不會把服務健康狀態改為正常。')){await api('incidents/'+x.id,{status:x.status==='open'?'acknowledged':'resolved'});await render();}
 })])));
 pane.append(el('p','已結案紀錄保存 90 天；事故內容僅包含固定服務代碼和狀態。','small'));
 paginate(pane,data);
}
function renderAccount(pane,data){
 pane.append(table(['項目','內容'],[['組織',data.tenant],['角色',roles[data.role]],['到期時間',new Date(data.expires_at*1000).toLocaleString()]]));
 pane.append(button('登出所有工作階段',async()=>{if(confirm('登出此帳號在所有裝置的工作階段？')){await api('logout-all',{});signedOut();}},'danger'));
}
function importForm() {
 const form=el('form',undefined,'import'), file=el('input'); file.type='file'; file.accept='.jsonl'; file.required=true;
 const policy=select([['team','一般團隊'],['regulated','受限內容']]), days=el('input');
 days.type='number'; days.min=1; days.max=30; days.step=1; days.value=7; days.required=true;
 const submit=el('button','匯入事件檔'); submit.type='submit';
 form.append(field('Ahem 事件檔',file),field('會議政策',policy),field('保存天數',days),submit);
 form.onsubmit=async e => {
  e.preventDefault(); if(submit.disabled)return;
  submit.disabled=true; submit.textContent='匯入中…'; const epoch=revision;
  try {
   const source=file.files[0];
   if (!source || source.size>4000000) throw Error('請選擇不超過 4 MB 的 JSONL 事件檔。');
   let events;
   try { events=(await source.text()).trim().split('\n').filter(x=>x.trim()).map(JSON.parse); }
   catch { throw Error('JSONL 格式不正確，請確認每一行都是完整的 JSON 事件。'); }
   if (!events.length || events.length>10000) throw Error('事件數必須介於 1 至 10,000 筆。');
   if(epoch!==revision)return;
   await api('meetings',{events,policy:policy.value,days:Number(days.value)});
   if(epoch===revision){ await render(); $('#status').className='success'; $('#status').textContent='會議已匯入，統計已更新。'; }
  } catch(e) { if(epoch===revision)error(e); }
  finally { submit.disabled=false; submit.textContent='匯入事件檔'; }
 };
 return form;
}
function renderMeetings(pane,data,page) {
 const stats=el('div',undefined,'stats');
 for(const [v,l] of [[data.total_count,'場會議'],[data.total_minutes,'累計分鐘'],[data.total_interventions,'主席介入']]) {
  const n=el('div',undefined,'stat'); n.append(el('strong',v),el('span',l)); stats.append(n);
 }
 pane.append(stats);
 const form=el('form',undefined,'import'),q=el('input'),policy=select([['all','全部政策'],['team','一般團隊'],['regulated','受限內容']]);
 q.value=filters.q;q.maxLength=32;q.pattern='[0-9a-fA-F]*';policy.value=filters.policy;
 const submit=el('button','套用篩選');submit.type='submit';form.append(field('會議代碼前綴',q),field('篩選政策',policy),submit);
 form.onsubmit=e=>{e.preventDefault();filters.q=q.value.trim();filters.policy=policy.value;filters.offset=0;render().catch(error);};pane.append(form);
 pane.append(button('匯出本頁統計 JSON',()=>{
  const blob=new Blob([JSON.stringify({scope:'current_page',...data},null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=el('a');a.href=url;a.download='ahem-statistics.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
 },'secondary'));
 if(page==='meetings' && identity.role==='operator')pane.append(importForm());
 if(!data.meetings.length){pane.append(el('p','目前沒有可查看的會議。請先匯入事件或取得會議授權。','empty'));return;}
 pane.append(table(['會議代碼','政策','分鐘','參與人數','發言段數','主席介入',...(page==='meetings'?['操作']:[])],data.meetings.map(m=>{
  const row=[m.id.slice(0,8),m.policy==='regulated'?'受限內容':'團隊',(m.duration_seconds/60).toFixed(1),m.participants,m.utterances,m.interventions];
  if(page==='meetings'){
   const actions=el('div',undefined,'actions');
   if(m.policy!=='regulated' || identity.regulated_content) {
    actions.append(button('查看內容',()=>detail(m)));
    if(identity.role==='operator')actions.append(button('管理授權',()=>grants(m),'secondary'));
    if(identity.role==='operator')actions.append(button('保存政策',()=>retention(m),'secondary'));
    if(identity.role==='operator')actions.append(button('填寫日期',()=>meetingDate(m),'secondary'));
   } else actions.append(el('span','需受限內容許可','small'));
   if(identity.role==='operator')actions.append(button('刪除',async()=>{
    if(confirm('永久刪除此場會議的儲存內容與授權？')){
     await api('meetings/'+m.id,null,'DELETE'); await render();
    }
   },'danger'));
   row.push(actions);
  }
  return row;
 })));
 paginate(pane,data);
}
function retention(m){
 const pane=detailPane('保存政策'),days=el('input'),policy=select(identity.regulated_content?[['team','一般團隊'],['regulated','受限內容']]:[['team','一般團隊']]);
 policy.value=m.policy;if(m.policy==='regulated')policy.disabled=true;
 days.type='number';days.min=1;days.max=30;days.step=1;days.value=1;
 pane.append(el('p','到期：'+new Date(m.expires_at*1000).toLocaleString()+'。只允許縮短保存期限，受限內容不可降級為團隊。','small'),field('新的最長保存天數',days),field('新的內容政策',policy),button('確認更新政策',async()=>{
  if(!days.checkValidity()){days.reportValidity();return;}
  if(confirm('套用保存政策？期限縮短後無法在此延長。')){await api('meetings/'+m.id+'/policy',{policy:policy.value,days:Number(days.value)});await render();}
 }));
}
function detailPane(title) {
 clearInterval(accessTimer);
 document.querySelector('.detail')?.remove();
 const pane=el('section',undefined,'detail'), heading=el('div',undefined,'detail-heading'), h=el('h2',title);
 h.tabIndex=-1;
 heading.append(h,button('關閉',()=>pane.remove(),'secondary')); pane.append(heading);
 $('#panel').append(pane); h.focus({preventScroll:true}); pane.scrollIntoView({block:'nearest'});
 return pane;
}
function detail(m) {
 const pane=detailPane('內容存取用途'), purpose=select([['meeting_review','會議回顧'],['incident_review','事件調查']]);
 pane.append(field('內容存取用途',purpose),button('確認讀取',async()=>{
  const epoch=revision;
  const data=await api('meetings/'+m.id+'/content',{purpose:purpose.value});
  if(epoch!==revision || !identity || !pane.isConnected)return;
  const list=el('div',undefined,'transcript');
  pane.replaceChildren(el('h2','會議內容'),button('關閉',()=>pane.remove(),'secondary'));
  for(const e of data.events){
   if(e.kind==='meeting')pane.append(el('h3',e.data.topic||'會議'));
   if(e.kind==='utterance'||e.kind==='spoken')list.append(el('p',(e.data.speaker||'主席')+'：'+(e.data.text||''),e.kind==='spoken'?'chair':undefined));
  }
  pane.append(list);
  clearInterval(accessTimer);
  accessTimer=setInterval(async()=>{
   if(!pane.isConnected){clearInterval(accessTimer);return;}
   try{await api('meetings/'+m.id+'/access');}catch(e){pane.remove();clearInterval(accessTimer);error(e);}
  },15000);
  if(!list.children.length)pane.append(el('p','這份事件檔沒有逐字稿。'));
 }));
}
async function grants(m) {
 const epoch=revision, data=await api('meetings/'+m.id+'/grants');
 if(epoch!==revision || !identity)return;
 const pane=detailPane('會議閱覽授權');
 if(!data.viewers.length)pane.append(el('p','此組織尚未配置閱覽帳號。'));
 for(const a of data.viewers){
  const row=el('div',undefined,'row');
  row.append(el('span',a.id),button(a.granted?'撤銷閱覽':'允許閱覽',async()=>{
   await api('meetings/'+m.id+'/grants',{actor:a.id,allow:!a.granted});
   if(epoch===revision && pane.isConnected)await grants(m);
  },a.granted?'danger':undefined));pane.append(row);
 }
}
function navIcon(kind) {
 const paths = {
  analytics:['M5 20V12','M12 20V4','M19 20V8'],
  meetings:['M6 10V7a6 6 0 0 1 12 0v3','M5 10h14v11H5z','M12 14v3'],
  health:['M2 12h4l3-8 5 16 3-8h5'],
  audit:['M8 3H5v18h14V3h-3','M9 2h6v4H9z','M8 10h8','M8 14h8','M8 18h5']
 };
 const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
 for(const [k,v] of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.7','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true',focusable:'false'}))svg.setAttribute(k,v);
 for(const d of paths[kind]||paths.audit){const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',d);svg.append(path);}
 return svg;
}
start().catch(e => { if(identity)error(e); });
