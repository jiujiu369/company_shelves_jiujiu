'use strict';
const $ = id => document.getElementById(id);
const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const icon = name => `<i data-lucide="${escape(name)}"></i>`;
const badge = (text, type = '') => `<span class="badge ${type}">${escape(text)}</span>`;
const time = stamp => stamp ? new Date(stamp * 1000).toLocaleString('zh-CN', {hour12:false}) : '尚未读取';
const pretty = value => typeof value === 'object' && value !== null ? JSON.stringify(value) : value === null ? '空' : String(value ?? '未读到');
const actionNames = {'validate':'资料校验','start-browser':'打开专用浏览器','probe':'登录检查','inspect':'平台全面核对','audit-images':'图片核验','fill':'填写已知项并核对','resume':'恢复并重读','submit':'单次提交入仓','open-warehouse-tab':'仓库入口检查','model-probe':'Qwen服务复测'};
actionNames['audit-warehouse'] = '仓库全分页核对';
actionNames['model-extract'] = 'Qwen图片资料提取';
actionNames['model-suggest'] = 'Qwen标题建议';
const states = {paused:'已暂停',selected:'已选择资料',filling:'填写中',verified:'上次完整核对通过',submission_unknown:'提交结果未知',confirmed_warehouse:'真实入仓回查通过',tested_warehouse:'仅内部测试通过',pending_data:'资料待补',data_ready:'资料预检通过',terminated:'已人工终止'};
const eventNames = {validation:'资料校验',data_changed:'商品资料发生变更',ui_preparation_ack:'开始前准备确认',phase_failure:'页面步骤暂停',sku_batch_verified:'SKU批量回读',sku_material_folder:'素材文件夹读取',detail_material_mapping:'详情文件名映射',manual_detail_order:'保留人工详情排列',intent_recorded:'提交意图已持久记录',click_claimed:'提交点击已认领',submit_exception:'提交结果待回查',product_list_open_intent:'商品列表检查开始',product_list_opened:'商品列表已读取',operator_resume:'人工恢复任务'};
let token = '', view = null, selectedProduct = '', activeTab = 'overview', saving = false, loading = false, submissionContext = null;
let editor = null, rawDirty = false;

function icons() { if (window.lucide) window.lucide.createIcons({attrs:{'aria-hidden':'true'}}); }
function message(text, success = false) { $('message').hidden = false; $('message').textContent = text; $('message').className = success ? 'success' : ''; }
async function api(path, payload) {
  const response = await fetch(path, payload ? {method:'POST',headers:{'Content-Type':'application/json','X-UI-Token':token},body:JSON.stringify(payload)} : {});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '读取失败');
  return data;
}
function setImage(element, link, url) { if (element.dataset.url !== url) {element.src = url; element.dataset.url = url;} element.hidden = false; if (link) {link.href = url; link.hidden = false;} }
function passing(prefix) { const checks = view.checks.filter(c => c.path === prefix || c.path.startsWith(prefix + '.')); return checks.length > 0 && checks.every(c => c.ok && c.current_version); }
function renderPipeline() {
  const liveSuccess = view.confirmed_current_version;
  const submitted = !!view.submission;
  const imageReady = passing('main_images') && passing('detail_images') && view.checks.filter(c => c.path.endsWith('.image')).every(c => c.ok && c.current_version);
  const done = [view.issues.every(i => !i.blocking), passing('fields') && passing('skus') && imageReady, view.fresh_preflight, submitted, liveSuccess, liveSuccess];
  const labels = ['资料准备','商品填写','全面核对','提交入仓','仓库回查','编辑页回查'];
  const descriptions = ['本地资料预检','字段、图片、全部SKU','提交前重新读页','持久意图，只提交一次','新编号与仓库状态','内容一致才记成功'];
  const job = view.job?.status === 'running' && view.job.product === selectedProduct ? view.job.action : '';
  const current = ['validate'].includes(job) ? 0 : job === 'fill' ? 1 : ['inspect','audit-images'].includes(job) ? 2 : job === 'submit' ? 3 : job === 'open-warehouse-tab' || (job === 'resume' && submitted) ? 4 : -1;
  $('pipeline').innerHTML = labels.map((label,i) => `<div class="step ${done[i] ? 'recorded' : ''} ${current===i ? 'active' : ''}"><span class="step-number">${done[i] ? icon('check') : i+1}</span><div><strong>${label}</strong><p>${current===i ? '正在执行' : done[i] ? i === 0 ? '预检通过' : i === 3 ? '已记录意图，不代表成功' : i >= 4 ? '真实回查通过' : '上次记录通过' : descriptions[i]}</p></div></div>`).join('');
}
function renderPreparations() {
  $('auto-preparation').innerHTML = view.preparations.filter(p => !p.manual).map(p => `<div class="prep-row ${p.ok?'':'is-bad'}">${icon(p.ok ? 'circle-check' : 'circle-alert')}<div><strong>${escape(p.title)}</strong><p>${escape(p.detail)}</p></div>${badge(p.ok?'已检查':'待处理',p.ok?'good':'warning')}</div>`).join('');
  const target = $('manual-preparation');
  if (!target.children.length) {
    target.innerHTML = view.preparations.filter(p => p.manual).map(p => `<div class="prep-row"><label><input type="checkbox" data-preparation="${escape(p.id)}"><span><strong>${escape(p.title)}</strong><p>${escape(p.detail)}</p></span></label></div>`).join('');
    target.querySelectorAll('input').forEach(input => input.addEventListener('change', savePreparation));
  }
  const busy = view.job?.status === 'running';
  if (!saving) view.preparations.filter(p=>p.manual).forEach(p => {const input = target.querySelector(`[data-preparation="${p.id}"]`); input.checked = p.ok; input.disabled = busy;});
  const completed = view.preparations.filter(p => p.manual && p.ok).length;
  $('prep-count').textContent = `${completed}/4 人工确认`;
  $('prep-count').className = `badge ${completed===4 ? 'good' : 'warning'}`;
  $('prep-note').textContent = completed===4 ? '准备确认已保存。资料版本变更后须重新确认；提交仍须平台核验。' : '准备确认仅针对当前商品资料版本，不确认平台已保存。';
}
function renderBlockers() {
  const items = [];
  if (view.submission && view.submission.state !== 'manual_not_submitted' && view.task?.state !== 'confirmed_warehouse') items.push(['提交已留意图','只进行仓库和编辑页回查，禁止再次提交。']);
  const failed = view.checks.filter(c => !c.ok || !c.current_version);
  if (failed.length) items.push(['全面核对尚未通过',failed.slice(0,7).map(c=>c.label).join('、') + (failed.length>7?'等':'' )]);
  if (!view.submit_adapter_verified) items.push(['真实入仓闭环仍待验收','仓库分页、编辑页回查和提交绑定未全部现场验证，提交保持禁用。']);
  const event = view.events.find(e => ['phase_failure','paused','submission_unknown'].includes(e.kind));
  const reason = event?.payload?.reason || event?.payload?.error;
  let html = items.map(([title,detail])=>`<div class="blocker"><strong>${escape(title)}</strong><p>${escape(detail)}</p></div>`).join('');
  if (reason) {
    const translated = /observable completion timed out/.test(reason) ? '仓库入口操作未观察到切换完成，已暂停。' : /image|material|pixel/.test(reason) ? '图片内容或素材映射尚未核验通过。' : /unit/.test(reason) ? '单位对应关系需要核对。' : '上次页面步骤已暂停，需读取现场后继续。';
    html += `<div class="blocker"><strong>上次停点</strong><p>${translated}</p><details><summary>原始记录 · ${escape(time(event.created))}</summary><p>${escape(reason)}</p></details></div>`;
  }
  $('blockers').innerHTML = html || '<p class="empty">尚无阻塞记录，仍需进行平台实际核对。</p>';
  $('submit-boundary').textContent = view.submit_ready ? '当前有60秒内的完整平台核对；入仓前程序还会再次回读。' : '只有完整平台回读、仓库基线与生产绑定通过，才允许单次提交。勾选准备项不能解除此限制。';
}
function renderPending() {
  $('pending-count').textContent = `${view.issues.filter(i=>i.blocking).length} 项必填待补 · ${view.issues.filter(i=>!i.blocking).length} 项可选`;
  $('pending').innerHTML = view.issues.length ? view.issues.map(i=>`<div class="pending-row"><strong>${escape(i.label)}</strong><div><p>${escape(i.reason)}</p><small>来源：${escape(i.source || '未提供')}</small></div>${badge(i.blocking?'阻断相关操作':'可留空',i.blocking?'bad':'')}</div>`).join('') : '<p class="empty">本地资料预检无待补项。平台核对仍独立进行。</p>';
}
function renderChecks() {
  const rows = view.checks.filter(c=>!$('failed-only').checked || !c.ok || !c.current_version);
  $('bad-count').textContent = view.checks.filter(c=>!c.ok || !c.current_version).length || '';
  $('checks-table').innerHTML = rows.length ? `<table><thead><tr><th>字段</th><th>配置值</th><th>平台实际值</th><th>上次结果</th></tr></thead><tbody>${rows.map(c=>`<tr><td>${escape(c.label)}<small>${escape(c.path)}</small></td><td>${escape(pretty(c.expected))}</td><td>${escape(pretty(c.actual))}<small>${escape(c.source==='live_playwright'?'真实平台回读':c.source)}</small></td><td>${badge(!c.current_version?'旧版本':c.ok?'上次通过':'未通过',!c.current_version?'warning':c.ok?'good':'bad')}<small>${escape(time(c.observed))}</small></td></tr>`).join('')}</tbody></table>` : '<p class="empty">暂无符合筛选条件的核对记录</p>';
}
function renderEvents() {
  $('events-list').innerHTML = view.events.length ? view.events.map(e=>`<article class="event-row"><time>${escape(time(e.created))}</time><div><strong>${escape(eventNames[e.kind] || states[e.kind] || '页面操作记录')}</strong><details><summary>${escape(e.kind)}</summary><pre>${escape(JSON.stringify(e.payload,null,2))}</pre></details></div></article>`).join('') : '<p class="empty">尚无持久执行记录</p>';
}
function renderFacts() {
  const model=view.model||{};
  $('model-status').textContent=`${model.name||'未配置模型'} · ${model.url||'未配置服务'} · 结果待人工核实，不自动改配置或提交`;
  const oldImage=$('model-image').value;
  const filenames=model.images||[];
  if ($('model-image').dataset.names!==JSON.stringify(filenames)) {
    $('model-image').innerHTML=filenames.map(name=>`<option value="${escape(name)}">${escape(name)}</option>`).join('');
    if(filenames.includes(oldImage))$('model-image').value=oldImage;
    $('model-image').dataset.names=JSON.stringify(filenames);
  }
  const advice=model.advice?.advice;
  $('model-result').textContent=!advice?'暂无本版本模型结果':advice.facts?
    '图片提取结果（全部待人工核实）\n\n'+advice.facts.map(f=>`${f.name}：${pretty(f.value)}\n图片依据：${f.evidence_text}\n来源：${f.source}`).join('\n\n'):
    `宝贝标题建议：${advice.title}\n计数：${advice.title_count}/60\n\n导购标题建议：${advice.guide_title}\n计数：${advice.guide_title_count}/30\n\n${advice.policy_ok?'格式检查通过，内容仍待人工核实':'尚不满足标题要求，不可直接采用'}\n${(advice.missing||[]).join('\n')}\n\n依据来源：\n${(advice.sources||[]).join('\n')}`;
  $('facts-version').textContent = '资料版本 ' + view.version.slice(0,12);
  $('facts-table').innerHTML = `<table><thead><tr><th>字段</th><th>商品配置</th><th>依据来源</th><th>资料确认</th></tr></thead><tbody>${view.facts.map(f=>`<tr><td>${escape(f.label)}</td><td>${escape(pretty(f.value))}${f.reason?`<small>${escape(f.reason)}</small>`:''}</td><td>${escape(f.source || '未提供')}</td><td>${badge(f.confirmed?'已确认':'待确认',f.confirmed?'good':'warning')}</td></tr>`).join('')}</tbody></table>`;
  $('media-summary').innerHTML = `<p>本地目录：${escape(view.media.root)}</p><p>配置图片 ${Object.keys(view.media.files).length} 张；额外图片 ${view.media.extras.length} 张</p><details><summary>查看额外图片清单</summary><p>${escape(view.media.extras.join('；') || '无')}</p></details>`;
}
function renderJob() {
  const job = view.job;
  const busy = job?.status === 'running';
  const canFill = !view.submission || view.submission.state === 'manual_not_submitted';
  document.querySelectorAll('[data-action]').forEach(button => {button.disabled = busy || (button.dataset.action === 'fill' && (!view.preparation_ready || !canFill)) || (button.dataset.action === 'resume' && canFill && !view.preparation_ready);});
  $('product').disabled = busy;
  $('choose-product').disabled = busy;
  $('edit-config').disabled = busy || !!(view.submission && view.submission.state !== 'manual_not_submitted');
  $('sidebar-edit').disabled=$('edit-config').disabled;
  $('submit').disabled = busy || !view.submit_ready;
  $('submit').title = view.submit_ready ? '确认后只提交一次' : '完整平台核对和仓库回查适配尚未通过';
  $('fill').title = view.preparation_ready ? '填写有来源的已知字段，随后回读；不自动提交' : '先完成开始前人工准备确认';
  $('pause').disabled = !busy && !job?.pause_available;
  if (!job) {
    $('run-title').textContent = '本轮尚未启动';
    $('run-detail').textContent = '下方显示已有持久记录；打开工作台不会自动填写或提交。';
    $('elapsed').textContent = '';
    return;
  }
  $('run-title').textContent = (busy?'正在执行：':job.status==='unconfirmed'?'上轮结果待确认：':'当前未运行 · 上次任务：') + (actionNames[job.action] || job.action);
  $('run-detail').textContent = busy ? '脚本正在运行，步骤和核对结果按实际记录更新。' : job.status === 'unconfirmed' ? '工作台重启，不自动重跑；先核对持久记录与平台现场。' : `${states[job.result?.state] || (job.exit_code===0?'已结束':'未通过')} · ${time(job.ended)} · ${job.product}${job.result?.reason?' · 原因：'+job.result.reason:''}`;
  const seconds = Math.max(0,Math.floor(((busy?Date.now()/1000:job.ended || job.started)-job.started)));
  $('elapsed').textContent = `${busy?'已用时':'上次耗时'} ${Math.floor(seconds/60)}分${seconds%60}秒`;
  const modelBusy = busy && job.product===selectedProduct && job.action.startsWith('model-');
  $('model-extract').disabled = busy || !$('model-image').value;
  $('model-image').disabled = busy;
  $('model-progress').textContent = modelBusy ? '模型处理中，尚未产生结果…' : job.product===selectedProduct && job.action.startsWith('model-') ? `${job.exit_code===0?'模型调用已完成':'模型调用未通过'} · ${time(job.ended)}${job.result?.reason?' · '+job.result.reason:''}` : '';
}
function render() {
  $('product-name').textContent = view.title || view.task_id;
  $('shop').textContent = '店铺：' + (view.shop || '待补');
  $('version').textContent = `${view.task_id} · 资料版本 ${view.version.slice(0,12)}`;
  $('mode').textContent = view.mode==='test'?'测试商品':'正式商品配置';
  $('mode').className = 'badge ' + (view.mode==='test'?'warning':'');
  const state = view.task?.state;
  const oldSuccess = state==='confirmed_warehouse' && !view.confirmed_current_version;
  $('task-status').textContent = oldSuccess ? '资料已变更，需重新核对' : states[state] || '未执行';
  $('task-status').className = 'badge ' + (view.confirmed_current_version?'good':'warning');
  $('item-id').textContent = (oldSuccess?'原版本商品编号：':'新商品编号：') + (view.task?.item_id || '尚未产生');
  if (view.main_image) setImage($('product-image'),null,'/api/image?' + new URLSearchParams({product:selectedProduct,file:view.main_image})); else $('product-image').hidden=true;
  $('no-evidence').hidden = !!view.screenshot;
  if (view.screenshot) setImage($('evidence'),$('evidence-link'),'/api/evidence?' + new URLSearchParams({product:selectedProduct,file:view.screenshot})); else $('evidence-link').hidden=true;
  renderPipeline(); renderPreparations(); renderBlockers(); renderPending(); renderChecks(); renderEvents(); renderFacts(); renderJob(); icons();
  $('updated').textContent = '本地记录刷新：' + time(view.time);
}
async function refresh() {
  if (loading || !selectedProduct) return;
  loading = true;
  const product = selectedProduct;
  try { const result = await api('/api/state?' + new URLSearchParams({product})); if (product===selectedProduct) {view=result; render();} }
  catch (error) {message('读取失败：' + error.message);}
  finally {loading=false;}
}
async function savePreparation() {
  if (saving) return;
  saving=true;
  const checks = [...document.querySelectorAll('[data-preparation]:checked')].map(input=>input.dataset.preparation);
  document.querySelectorAll('[data-preparation]').forEach(input=>input.disabled=true);
  try {await api('/api/preparation',{product:selectedProduct,version:view.version,checks}); message('准备确认已保存；不会放行提交。',true);}
  catch(error){message(error.message);$('message').scrollIntoView({block:'center'});}
  finally {saving=false; await refresh();}
}
async function run(action, context = {product:selectedProduct,version:view.version}, extra = {}) {
  try {await api('/api/action',{...context,action,...extra}); message('已启动：' + actionNames[action],true); await refresh();if(!action.startsWith('model-')){location.hash='overview';switchView('overview');$('run-bar').scrollIntoView({block:'center'});}else{$('model-progress').scrollIntoView({block:'center'});}}
  catch(error){message(error.message);}
}
document.querySelectorAll('[data-action]').forEach(button=>button.addEventListener('click',()=>run(button.dataset.action)));
function switchView(tab) {
  activeTab=['overview','checks','events','facts'].includes(tab)?tab:'overview';
  document.querySelectorAll('.view').forEach(panel=>panel.hidden=panel.id!==activeTab);
  document.querySelectorAll('[data-view]').forEach(nav=>{const selected=nav.dataset.view===activeTab;nav.classList.toggle('selected',selected);if(selected)nav.setAttribute('aria-current','page');else nav.removeAttribute('aria-current');});
  document.querySelector('.product-summary').hidden=activeTab!=='overview';
  document.querySelector('h1').textContent={overview:'自动录入工作台',checks:'核对明细',events:'执行记录',facts:'商品资料'}[activeTab];
}
document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>{
  location.hash=button.dataset.view;switchView(button.dataset.view);document.getElementById('main').scrollIntoView({block:'start'});
}));
window.addEventListener('hashchange',()=>switchView(location.hash.slice(1)));
switchView(location.hash.slice(1));
$('model-extract').addEventListener('click',()=>run('model-extract',undefined,{file:$('model-image').value}));
$('refresh').addEventListener('click',refresh);
$('failed-only').addEventListener('change',()=>view && renderChecks());
$('pause').addEventListener('click',async()=>{try{const data=await api('/api/pause',{product:selectedProduct});message(data.reason,true);}catch(error){message(error.message);}});
$('submit').addEventListener('click',()=>{if(!view?.submit_ready)return;submissionContext=Object.freeze({product:selectedProduct,version:view.version});$('submit-description').textContent=`店铺：${view.shop}；商品：${view.title}；SKU：${view.sku_count} 条。`; $('submit-dialog').showModal();});
$('cancel-submit').addEventListener('click',()=>{submissionContext=null;$('submit-dialog').close();});
$('submit-dialog').addEventListener('cancel',()=>{submissionContext=null;});
$('confirm-submit').addEventListener('click',()=>{const context=submissionContext;submissionContext=null;$('submit-dialog').close();if(context)run('submit',context);});
$('product').addEventListener('change',()=>{selectedProduct=$('product').value;$('manual-preparation').replaceChildren();$('message').hidden=true;refresh();});
function editorFacts(data) {
  return Object.entries({shop:data.shop,category:data.category,
    ...Object.fromEntries(Object.entries(data.fields||{}).map(([k,v])=>['fields.'+k,v])),
    ...Object.fromEntries(Object.entries(data.logistics||{}).map(([k,v])=>['logistics.'+k,v])),
    ...Object.fromEntries((data.skus||[]).flatMap((sku,i)=>Object.entries(sku).map(([k,v])=>[`skus.${i}.${k}`,v])))})
    .filter(([,v])=>v && typeof v==='object' && !Array.isArray(v));
}
function factAt(data,path) {return path.split('.').reduce((v,key)=>v[key],data);}
function updateEditorData() {
  const data = rawDirty ? JSON.parse($('config-json').value) : structuredClone(editor.data);
  if (!rawDirty) document.querySelectorAll('[data-edit-path]').forEach(row=>{
    const fact=factAt(data,row.dataset.editPath), input=row.querySelector('[data-value]');
    fact.value=input.value===''?null:input.dataset.type==='number'?Number(input.value):input.value;
    fact.source=row.querySelector('[data-source]').value;
    fact.confirmed=row.querySelector('[data-confirmed]').checked;
    fact.reason=row.querySelector('[data-reason]').value;
  });
  data.media=data.media||{};data.media.root=$('material-root').value;
  data.media.input_mode=$('media-mode').value;
  if(data.media.input_mode==='local_folder') data.media.local_folder_authorization={
    root:data.media.root, confirmed:$('folder-upload-confirmed').checked,
    source:'本地工作台操作者保存配置时确认上传目录及清单'};
  return data;
}
async function openEditor() {
  try {
    editor=await api('/api/configuration?'+new URLSearchParams({product:selectedProduct}));rawDirty=false;
    $('material-root').value=editor.data.media?.root||'';
    $('media-mode').value=editor.data.media?.input_mode||'material_center';
    $('folder-upload-confirmed').checked=editor.data.media?.local_folder_authorization?.confirmed===true;
    $('config-json').value=JSON.stringify(editor.data,null,2);$('config-error').textContent='';
    const labels=new Map(view.facts.map(f=>[f.path,f.label]));
    $('config-fields').innerHTML=editorFacts(editor.data).map(([path,f])=>`<fieldset data-edit-path="${escape(path)}"><legend>${escape(labels.get(path)||path)}</legend><label>值<input data-value data-type="${typeof f.value==='number'||/\.(inventory|pack_quantity)$/.test(path)?'number':'string'}" value="${escape(f.value)}"></label><label>依据来源<input data-source value="${escape(f.source)}"></label><label>待补原因<input data-reason value="${escape(f.reason)}"></label><label class="confirmed-label"><input type="checkbox" data-confirmed ${f.confirmed?'checked':''}>资料已确认</label></fieldset>`).join('');
    $('advanced-config').open=false;$('config-dialog').showModal();icons();
  }catch(error){message(error.message);}
}
$('edit-config').addEventListener('click',openEditor);
$('sidebar-edit').addEventListener('click',openEditor);
$('config-json').addEventListener('input',()=>{rawDirty=true;$('config-fields').querySelectorAll('input').forEach(i=>i.disabled=true);try{const media=JSON.parse($('config-json').value).media||{};$('material-root').value=media.root||'';$('media-mode').value=media.input_mode||'material_center';$('folder-upload-confirmed').checked=media.local_folder_authorization?.confirmed===true;}catch(error){/* Preserve unfinished JSON while editing. */}});
$('material-root').addEventListener('input',()=>{$('folder-upload-confirmed').checked=false;});
$('advanced-config').addEventListener('toggle',()=>{if($('advanced-config').open && !rawDirty && editor)$('config-json').value=JSON.stringify(updateEditorData(),null,2);});
$('cancel-config').addEventListener('click',()=>$('config-dialog').close());
$('browse-folder').addEventListener('click',async()=>{
  $('browse-folder').disabled=true;
  try{const result=await api('/api/select',{kind:'folder'});if(result.path){$('material-root').value=result.path;$('folder-upload-confirmed').checked=false;}}
  catch(error){$('config-error').textContent=error.message;}
  finally{$('browse-folder').disabled=false;}
});
$('save-config').addEventListener('click',async()=>{
  $('save-config').disabled=true;
  try{const result=await api('/api/save-configuration',{product:editor.product,version:editor.version,data:updateEditorData()});$('config-dialog').close();message(`配置已保存为新版本；必填待补 ${result.issues.filter(i=>i.blocking).length} 项。须重新读取平台。`,true);await refresh();}
  catch(error){$('config-error').textContent=error.message;}
  finally{$('save-config').disabled=false;}
});
$('choose-product').addEventListener('click',()=>{$('import-path').value='';$('import-error').textContent='';$('import-dialog').showModal();});
$('cancel-import').addEventListener('click',()=>$('import-dialog').close());
$('browse-product').addEventListener('click',async()=>{
  $('browse-product').disabled=true;
  try{const result=await api('/api/select',{kind:'file'});if(result.path)$('import-path').value=result.path;}
  catch(error){$('import-error').textContent=error.message;}
  finally{$('browse-product').disabled=false;}
});
$('confirm-import').addEventListener('click',async()=>{
  $('confirm-import').disabled=true;
  try{const result=await api('/api/import-path',{path:$('import-path').value});const boot=await api('/api/bootstrap');$('product').innerHTML=boot.products.map(name=>`<option value="${escape(name)}">${escape(name)}</option>`).join('');selectedProduct=result.product;$('product').value=selectedProduct;$('manual-preparation').replaceChildren();$('import-dialog').close();message('已选择商品资料，原文件未修改。',true);await refresh();}
  catch(error){$('import-error').textContent=error.message;}
  finally{$('confirm-import').disabled=false;}
});
(async()=>{try{const boot=await api('/api/bootstrap');token=boot.token;$('product').innerHTML=boot.products.map(name=>`<option value="${escape(name)}">${escape(name)}</option>`).join('');selectedProduct=boot.job && ['running','unconfirmed'].includes(boot.job.status) && boot.products.includes(boot.job.product) ? boot.job.product : boot.products.includes('jg-test.json')?'jg-test.json':boot.products[0];$('product').value=selectedProduct;await refresh();setInterval(refresh,4000);setInterval(()=>view && renderJob(),1000);}catch(error){message(error.message);}icons();})();
