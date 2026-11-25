const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = value => value == null ? '待核实' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits:2});
const statuses = {queued:'排队中', running:'正在查数和计算', pending_review:'等你确认建议', archived:'已保存报告', failed:'未完成，可重试', interrupted:'已中断，可继续'};
let current=null, source=null, reportData=null, metadata=null, revealReport=null;
let agentSteps={};
function updateClock(){const clock=$('clock');if(clock)clock.textContent=new Date().toLocaleTimeString('zh-CN',{hour12:false});}
updateClock();setInterval(updateClock,1000);
const product = sku => metadata.products[sku] || {name:sku,unit:'件',spec:''};
function cn(value) { let text=String(value??''); for(const key of Object.keys(metadata?.words||{}).sort((a,b)=>b.length-a.length)) text=text.split(key).join(metadata.words[key]); return text.replace(/TimeoutError/g,'等待超时').replace(/RuntimeError|ValueError/g,'数据或流程异常'); }
function toast(message) { $('toast').textContent=cn(message); $('toast').classList.remove('hidden'); setTimeout(()=>$('toast').classList.add('hidden'),8000); }
function resetAgentSteps(){
  agentSteps={};
  for(const key of Object.keys(metadata?.agents||{})) agentSteps[key]={status:'waiting',actions:[],started:null,finished:null};
  renderAgentSteps();
}
function renderAgentSteps(){
  const root=$('agent-steps'); if(!root||!metadata)return;
  const labels={waiting:'等待执行',running:'正在执行',done:'已完成',failed:'执行失败'};
  root.innerHTML=Object.entries(metadata.agents||{}).map(([key,info])=>{
    const step=agentSteps[key]||{status:'waiting',actions:[]};
    const actions=step.actions.length?step.actions.map(a=>`<li>${esc(a)}</li>`).join(''):'<li class="step-empty">还没有动作</li>';
    return `<article class="agent-step ${esc(step.status)}"><div class="agent-step-head"><span class="step-dot"></span><div><b>${esc(info.name)}</b><small>${esc(labels[step.status]||'等待执行')}</small></div></div><p>${esc(info.description)}</p><details><summary>查看执行动作（${step.actions.length}）</summary><ol>${actions}</ol></details></article>`;
  }).join('');
}
function recordAgentEvent(data){
  const rawTool=data.tool||(data.node==='tool'?String(data.message||'').split(' · ')[0]:'');
  const key=data.agent||(data.node==='tool'?({sales_summary:'sales_agent',inventory_snapshot:'inventory_agent'}[rawTool]||'replenishment_agent'):data.node);
  if(!agentSteps[key])return;
  const step=agentSteps[key], kind=data.kind||(data.message==='开始执行'?'start':data.message==='执行完成'?'complete':(data.node==='supervisor'||data.node.endsWith('_agent')?'result':'action'));
  if(kind==='start') {step.status='running';step.started=data.time;}
  else if(kind==='complete'||kind==='result') {step.status='done';step.finished=data.time;}
  else if(kind==='waiting') step.status='running';
  else if(data.node==='error') step.status='failed';
  let action='';
  if(kind==='tool'&&rawTool) action=`查询：${metadata.tools[rawTool]||cn(rawTool)}（${data.evidence?.rows??0} 条记录）`;
  else if(kind!=='start'&&kind!=='complete'&&data.message) action=cn(data.message);
  if(action&&(!step.actions.length||step.actions[step.actions.length-1]!==action)) step.actions.push(action);
  renderAgentSteps();
}
async function api(path,options={}) {
  const response=await fetch(path,{headers:{'Content-Type':'application/json'},...options});
  const data=await response.json();
  if(!response.ok) throw new Error(typeof data.detail==='string'?data.detail:'填写内容不正确，请检查商品、日期和数量。');
  return data;
}
async function history() {
  const tasks=await api('/api/tasks');
  $('history').innerHTML=tasks.map(t=>`<button class="history-item" data-task="${esc(t.id)}">${esc(metadata.scenarios[t.request.scenario]||'经营分析')} · ${esc(metadata.tenants[t.request.tenant]||'商家')}<small>${esc(metadata.warehouses[t.request.warehouse]||'仓库')} · ${esc(statuses[t.status])}<br>${esc(new Date(t.created_at).toLocaleString('zh-CN'))}</small></button>`).join('')||'<p class="hint">做好第一次分析后，可以在这里找回。</p>';
  document.querySelectorAll('[data-task]').forEach(b=>b.onclick=()=>openTask(b.dataset.task).catch(e=>toast(e.message)));
}
function watch(id) {
  if(source) source.close();
  $('events').innerHTML='';
  resetAgentSteps();
  source=new EventSource(`/api/tasks/${id}/events`);
  const connection=source, seen=new Set();
  connection.addEventListener('progress',event=>{
    if(current!==id||seen.has(event.lastEventId)) return;
    seen.add(event.lastEventId);
    const data=JSON.parse(event.data),item=document.createElement('div');item.className='event';
    recordAgentEvent(data);
    item.innerHTML=`<small>${esc(new Date(data.time).toLocaleTimeString('zh-CN'))}</small><strong>${esc(metadata.nodes[data.node]||'处理进度')}</strong>${esc(cn(data.message))}`;
    $('events').appendChild(item);$('events').scrollTop=$('events').scrollHeight;
    document.querySelectorAll('[data-node]').forEach(n=>n.classList.toggle('running',n.dataset.node===data.node));
  });
  connection.addEventListener('done',()=>{connection.close();if(current===id)refreshTask(id).catch(e=>toast(e.message));history().catch(e=>toast(e.message));});
  connection.onerror=()=>{if(current===id)$('task-status').textContent='连接中断，正在重新连接…';};
}
async function refreshTask(id) {
  const task=await api(`/api/tasks/${id}`);if(current!==id)return;
  $('task-status').textContent=statuses[task.status]||'处理中';
  if($('live-label')) $('live-label').textContent=task.status==='running'?'正在实时分析':(task.status==='pending_review'?'等待人工确认':'本地分析引擎');
  $('resume').classList.toggle('hidden',!['failed','interrupted'].includes(task.status));
  if(task.error)toast(task.error);
  if(task.report){
    renderReport(task.report,task.status,task.display);
    if(revealReport===id){revealReport=null;$('report').scrollIntoView({behavior:'smooth',block:'start'});}
  }else $('report').classList.add('hidden');
}
async function openTask(id) {current=id;localStorage.setItem('retail-current-task',id);await refreshTask(id);watch(id);}
function renderReport(report,status,view) {
  reportData=report;
  const req=report.request,rows=report.recommendations;
  $('report').classList.remove('hidden');
  $('report').innerHTML=`
    <div class="section-title"><div><h2>第三步：看看哪些该买，哪些先别买</h2><p class="hint">${esc(view.shop)} · ${esc(view.warehouse)} · ${esc(view.scenario)}<br>根据 ${esc(req.start_date)} 至 ${esc(req.end_date)} 的业务记录计算</p></div><span>${esc(statuses[status])}</span></div>
    <div class="action-cards">${view.cards.map(c=>`<article class="action-card ${esc(c.tone)}"><small>${esc(c.name)} · ${esc(c.spec)}</small><h3>${esc(c.action)}</h3><p>${esc(c.why)}</p>${c.calculation?`<details><summary>这个数量怎么算出来的？</summary><p>${esc(c.calculation)}</p></details>`:''}</article>`).join('')}</div>
    <p class="hint">上面是系统给出的原始建议。你可以在下方确认或调整数量；保存后的最终数量见审核记录。</p>
    <details id="numbers"><summary>想核对数字？查看销量、库存和到货明细</summary><p class="hint">数量单位：咖啡和花茶按盒，马克杯按个。左右滑动可查看全部列。</p><div class="table-wrap"><table><thead><tr><th>商品</th><th>这段时间卖了多少</th><th>比上期变化</th><th>平均每天卖</th><th>现在还能卖</th><th>按历史速度还能卖几天</th><th>预计及时到货</th><th>送货需几天</th><th>希望备到多少</th><th>建议补多少</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${esc(product(r.sku).name)}<br><small>单位：${esc(product(r.sku).unit)}</small></td><td>${fmt(r.sales_qty)}</td><td>${r.growth_percent==null?'无法比较':fmt(r.growth_percent)+'%'}</td><td>${fmt(r.daily_sales)}</td><td>${fmt(r.available_stock)}</td><td>${r.coverage_days==null?'无法估算':fmt(r.coverage_days)}</td><td>${fmt(r.eligible_incoming_qty)}</td><td>${fmt(r.lead_days)}</td><td>${fmt(r.target_stock)}</td><td><strong>${fmt(r.suggested_qty)}</strong></td></tr>`).join('')}</tbody></table></div><p class="hint">上期指紧接着的前一段同样长的时间。预计及时到货，只算截止日之后、供应商送货周期内预计能到的未收货商品。历史销量为零时不估算能卖几天。</p></details>
    ${report.gaps.length?'<h3>这些问题，请先找相关同事核实</h3>'+report.gaps.map(g=>`<div class="gap"><strong>${esc(product(g.sku).name)} · ${g.blocking?'核实前不给采购数量':'需要留意'}</strong>${esc(cn(g.message))}</div>`).join(''):'<p class="hint">这次没有发现资料缺失或库存记录冲突。</p>'}
    <details><summary>查看分析说明和核对记录</summary><p>${esc(cn(report.narrative.overview))}</p><p>${esc(cn(report.narrative.reasoning))}</p><p>${esc(cn(report.narrative.cautions))}</p><ul>${report.checks.map(c=>`<li>${esc(cn(c))}</li>`).join('')}</ul><p>${esc(cn(report.llm_status))}</p></details>
    <details><summary>这些数据从哪里来？</summary><div class="table-wrap"><table><thead><tr><th>业务记录</th><th>统计范围</th><th>记录条数</th><th>读取方式</th><th>查询时间</th></tr></thead><tbody>${report.evidence.map(s=>`<tr><td>${esc(metadata.tools[s.tool]||'业务数据')}</td><td>${esc(s.period)}</td><td>${s.rows}</td><td>${s.cached?'使用近期查询结果':'重新查询'}</td><td>${esc(new Date(s.fetched_at).toLocaleString('zh-CN'))}</td></tr>`).join('')}</tbody></table></div></details><div id="review-area"></div>`;
  if(status==='pending_review')renderReview(rows);
  if(status==='archived'){
    const approved=report.status==='approved';
    $('review-area').innerHTML=`<div class="saved-review"><h3>${approved?'已确认并保存建议':'已驳回并保存记录'}</h3><p>审核人：${esc(report.review.reviewer)}<br>审核说明：${esc(report.review.note||'未填写')}</p><h3>${approved?'最终确认数量':'驳回时的参考数量，不作为采购安排'}</h3><ul>${Object.entries(report.review.final_quantities).map(([sku,q])=>`<li>${esc(product(sku).name)}：<strong>${fmt(q)} ${esc(product(sku).unit)}</strong></li>`).join('')}</ul>${report.review.adjustments.map(a=>`<p class="hint">${esc(product(a.sku).name)} 调整依据：${esc(a.reason)}</p>`).join('')}<a href="/api/tasks/${encodeURIComponent(current)}/report-readable" download>下载中文报告</a><p class="hint">报告可直接用浏览器打开或打印。${approved?'确认只保存建议，实际采购仍需你到采购系统操作。':'本次建议未通过审核，可以核实资料后重新分析。'}</p></div>`;
  }
}
function renderReview(rows){
  $('review-area').innerHTML=`<form id="review-form" class="review-form"><h2>最后由你决定：这次买多少？</h2><p class="hint">有建议的数量已填好，可以修改。写着“待核实”的商品，要补上你确认的数量和原因；还没查清楚时，可填零暂不买，也可以驳回整份建议。</p><div class="review-grid"><label>谁来确认<input id="reviewer" placeholder="例如：采购负责人王芳" required maxlength="80"></label><label>这次决定的说明<input id="review-note" placeholder="例如：咖啡正常补货，杯子暂停，花茶等仓管核实后再买" maxlength="2000"></label></div>${rows.map((r,i)=>`<div class="review-item"><span>${esc(product(r.sku).name)}<small>单位：${esc(product(r.sku).unit)}</small></span><label>确认数量<input type="number" min="0" max="10000000" step="1" id="qty-${i}" value="${r.suggested_qty??''}" placeholder="待核实" aria-label="${esc(product(r.sku).name)}确认数量"></label><label>调整或补填的原因<input id="reason-${i}" maxlength="500" placeholder="例如：资料还没补齐，先不买" aria-label="${esc(product(r.sku).name)}调整原因"></label></div>`).join('')}<label class="check"><input type="checkbox" id="ack">我已经核对以上问题，并确认自己填写的数量和原因</label><div class="actions"><button type="submit" class="primary">确认数量并保存建议</button><button type="button" class="secondary" id="reject">这次不通过，保存原因</button></div></form>`;
  $('review-form').onsubmit=e=>{e.preventDefault();submitReview('approve').catch(e=>toast(e.message));};
  $('reject').onclick=()=>submitReview('reject').catch(e=>toast(e.message));
}
async function submitReview(decision){
  const adjustments=[];
  for(let i=0;i<reportData.recommendations.length;i++){
    const row=reportData.recommendations[i],raw=$(`qty-${i}`).value;
    if(decision==='approve'&&raw==='')throw new Error(`请填写${product(row.sku).name}的确认数量，暂不采购可填零。`);
    if(raw!==''&&Number(raw)!==row.suggested_qty)adjustments.push({sku:row.sku,quantity:Number(raw),reason:$(`reason-${i}`).value.trim()});
  }
  const id=current;
  await api(`/api/tasks/${id}/review`,{method:'POST',body:JSON.stringify({reviewer:$('reviewer').value.trim(),decision,note:$('review-note').value,acknowledge_gaps:$('ack').checked,adjustments:decision==='approve'?adjustments:[]})});
  await refreshTask(id);watch(id);
}
function scenarioChanged(){
  const selected=document.querySelector('[name=scenario]:checked').value;
  $('scenario-help').textContent={shortage:'重点看：哪些商品可能在下一批到货前卖完，需要补多少，以及是否要加急。',slow:'重点看：哪些商品卖得慢、库存能卖很久，适合暂停采购或考虑促销。不会自动生成降价方案。',promotion:'重点看：如果活动期间销量增加，需要比平时多准备多少货。这里按整个备货周期使用你填写的销量倍数估算。'}[selected];
  $('factor-label').classList.toggle('hidden',selected!=='promotion');
}
document.querySelectorAll('[name=scenario]').forEach(input=>input.onchange=scenarioChanged);
$('analysis-form').onsubmit=async e=>{
  e.preventDefault();$('submit').disabled=true;
  try{
    const skus=[...document.querySelectorAll('[name=product]:checked')].map(i=>i.value);
    if(!skus.length)throw new Error('请至少选择一种商品。');
    const task=await api('/api/tasks',{method:'POST',body:JSON.stringify({tenant:$('tenant').value,warehouse:$('warehouse').value,skus,start_date:$('start').value,end_date:$('end').value,scenario:document.querySelector('[name=scenario]:checked').value,safety_days:Number($('safety').value),review_days:Number($('review-days').value),promotion_multiplier:Number($('factor').value),question:$('question').value})});
    revealReport=task.task_id;$('progress-details').open=true;await openTask(task.task_id);await history();
  }catch(error){toast(error.message);}finally{$('submit').disabled=false;}
};
$('resume').onclick=async()=>{try{await api(`/api/tasks/${current}/resume`,{method:'POST'});await refreshTask(current);watch(current);}catch(e){toast(e.message);}};
$('refresh').onclick=()=>history().catch(e=>toast(e.message));
(async()=>{try{
  metadata=await api('/api/meta');
  $('tenant').innerHTML=Object.entries(metadata.tenants).map(([value,label])=>`<option value="${esc(value)}">${esc(label)}</option>`).join('');
  $('warehouse').innerHTML=Object.entries(metadata.warehouses).map(([value,label])=>`<option value="${esc(value)}">${esc(label)}</option>`).join('');
  $('products').innerHTML=Object.entries(metadata.products).map(([value,p])=>`<label class="product-choice"><input type="checkbox" name="product" value="${esc(value)}" checked><span><b>${esc(p.name)}</b><small>${esc(p.spec)} · 按${esc(p.unit)}统计</small></span></label>`).join('');
  $('start').value=metadata.start_date;$('end').value=metadata.end_date;
  $('mode').textContent=metadata.llm_enabled?'已启用智能解读':'无需模型密钥，也能体验';
  $('question-help').textContent=metadata.llm_enabled?'智能解读会参考这里的问题；补货数字仍按上方商品、日期和条件计算。':'当前未启用智能解读，这段文字仅记录你的关注点。补货计算仍可正常使用，按所选商品、日期和条件执行。';
  scenarioChanged();resetAgentSteps();$('submit').disabled=false;await history();
  const last=localStorage.getItem('retail-current-task');if(last){try{await openTask(last);}catch{localStorage.removeItem('retail-current-task');}}
}catch(e){toast(e.message);}})();
