# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Manager web UI — the client script."""

JS = """<script>
const TEMPLATES=__TPLJSON__;
const SETTINGS=__SETTINGS__;
const SETTINGS_SCHEMA=__SETTINGS_SCHEMA__;
const PERSONAS=__PERSONAS__;
const SKILLS=__SKILLS__;
const CORNERS='';   // registration corners retired (Ulrich, 2026-09-12)
const I_EDIT='<svg width=13 height=13 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.5 stroke-linecap=round stroke-linejoin=round><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"></path></svg>';
const I_RUN='<svg width=13 height=13 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.5 stroke-linecap=round stroke-linejoin=round><polygon points="6 3 20 12 6 21 6 3"></polygon></svg>';
const I_DEL='<svg width=13 height=13 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.5 stroke-linecap=round stroke-linejoin=round><path d="M3 6h18"></path><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"></path><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>';
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;')}
function escT(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}

/* — tool selection in the create form — */
let TOOLS=[];
async function loadTools(){
  try{TOOLS=(await (await fetch('/api/agent-tools')).json()).tools||[];}catch(e){return;}
  document.getElementById('toolpick').innerHTML=TOOLS.map(t=>
    `<label class=radio style="font-size:13px"><input type=checkbox class=toolcb value="${esc(t.name)}" checked>`+
    `<span class=dot></span><span title="${esc(t.desc)}"><span class=mono style="font-size:12px">${escT(t.name)}</span></span></label>`).join('');
}
function toolAll(on){document.querySelectorAll('.toolcb').forEach(c=>c.checked=!!on)}

/* — Policy: what each instance may do (net/tools/secrets/MCP) + what it does (audit) — */
function _b64(buf){let str='',a=new Uint8Array(buf);for(let i=0;i<a.length;i+=0x8000)str+=String.fromCharCode.apply(null,a.subarray(i,i+0x8000));return btoa(str);}
async function loadPlugins(){
  let ps=[]; try{ps=(await (await fetch('/api/plugins')).json()).plugins||[];}catch(e){return;}
  const el=document.getElementById('pluglist'); if(!el)return;
  el.innerHTML = ps.length ? ps.map(p=>
    `<div class=card><div style="display:flex;justify-content:space-between;align-items:center;gap:8px">`+
    `<b>${escT(p.name)}</b><button class="btn btn-ghost" style="font-size:12px" onclick="plugDel('${esc(p.name)}')">L&#246;schen</button></div>`+
    `<div class=text-muted style="font-size:12px">${escT(p.kind)} &#183; ${p.files.length} file(s)</div>`+
    `<div class=mono style="font-size:11px;color:var(--color-neutral-600);word-break:break-all">${p.files.map(f=>(p.links||{})[f]?`<a href="${esc(p.links[f])}" target="_blank" rel="noopener noreferrer" title="open in VS Code">${escT(f)}</a>`:escT(f)).join(', ')}</div>`+
    `<div style="display:flex;align-items:center;gap:8px;margin-top:8px">`+
      (p.modified
        ? `<span class="tag" style="background:#c0392b;color:#fff;font-size:11px">\u26a0 ge\u00e4ndert seit Approve</span>`
        : p.pinned
          ? `<span class="tag tag-accent" style="font-size:11px">\u2713 pinned ${escT(p.sha||'')}</span>`
          : `<span class="tag tag-neutral" style="font-size:11px">not pinned</span>`)+
      ((p.modified||!p.pinned)?`<button class="btn btn-secondary" style="font-size:12px;margin-left:auto" onclick="plugApprove('${esc(p.name)}')">Approve</button>`:``)+
    `</div></div>`
  ).join('') : '<div class=text-muted>No plugins yet.</div>';
}
async function plugUpload(file){
  if(!file)return;
  const isZip=/\.zip$/i.test(file.name), name=file.name.replace(/\.(py|zip)$/i,'');
  if(!/\.(py|zip)$/i.test(file.name)){document.getElementById('plugmsg').textContent='\u26a0\ufe0f only .py or .zip';return;}
  const b64=_b64(await file.arrayBuffer());
  const body=isZip?{name,kind:'zip',data_b64:b64}:{name,kind:'py',data_b64:b64};
  const d=await (await fetch('/api/plugins',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  document.getElementById('plugmsg').textContent=d.error?('\u26a0\ufe0f '+d.error):('\u2713 '+name+' saved \u2014 restart the instance to activate');
  loadPlugins();
}
async function plugCreate(){
  const inp=document.getElementById('plugnew'),name=inp.value.trim(); if(!name)return;
  const d=await (await fetch('/api/plugins/new',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})})).json();
  document.getElementById('plugmsg').textContent=d.error?('\u26a0\ufe0f '+d.error):('\u2713 '+name+' created (tool.py) \u2014 edit in the folder, restart the instance');
  inp.value='';loadPlugins();
}
async function plugApprove(name){
  const d=await (await fetch('/api/plugins/'+encodeURIComponent(name)+'/pin',{method:'POST'})).json();
  document.getElementById('plugmsg').textContent=(d.msg==='approved'?'\u2713 '+name+' pinned ('+(d.sha||'')+')':(d.msg||'?'));
  loadPlugins();
}
async function plugDel(name){ if(!confirm('Plugin '+name+' l\u00f6schen?'))return;
  await fetch('/api/plugins/'+encodeURIComponent(name)+'/delete',{method:'POST'}); loadPlugins(); }
(function(){
  const dz=document.getElementById('plugdrop'); if(!dz)return;
  const fi=document.getElementById('plugfile');
  if(fi)fi.addEventListener('change',()=>{if(fi.files[0])plugUpload(fi.files[0]);fi.value='';});
  ['dragover','dragenter'].forEach(ev=>dz.addEventListener(ev,e=>{e.preventDefault();dz.classList.add('drag');}));
  ['dragleave','dragend'].forEach(ev=>dz.addEventListener(ev,e=>{e.preventDefault();dz.classList.remove('drag');}));
  dz.addEventListener('drop',e=>{e.preventDefault();dz.classList.remove('drag');const f=e.dataTransfer.files[0];if(f)plugUpload(f);});
})();
let POL_TOOLS=[];
let POL_DIRTY=new Set();   // instances with not-yet-saved tool changes
document.addEventListener('DOMContentLoaded',()=>{
  const pc=document.getElementById('policycards');
  if(pc)pc.addEventListener('change',e=>{const ds=e.target&&e.target.dataset;const pt=ds&&(ds.pt||ds.pm);if(pt)POL_DIRTY.add(pt);});
});
async function loadPolicy(auto){
  if(auto&&POL_DIRTY.size)return;   // do not overwrite unsaved checkboxes
  let pol={instances:[]};
  try{
    pol=await (await fetch('/api/policy')).json();
    if(!POL_TOOLS.length)POL_TOOLS=(await (await fetch('/api/agent-tools')).json()).tools||[];
    if(!MCPS.length){try{MCPS=await (await fetch('/api/mcps')).json();}catch(e){}}
  }catch(e){document.getElementById('policycards').innerHTML='<span class=text-muted>not reachable</span>';return;}
  const tag=(on,y,n)=>`<span class="tag ${on?'tag-accent':'tag-neutral'}">${on?y:n}</span>`;
  document.getElementById('policycards').innerHTML=(pol.instances||[]).map(p=>{
    const toolset=new Set(p.tools||[]);
    const tools=POL_TOOLS.map(t=>
      `<label class=radio style="font-size:12.5px"><input type=checkbox data-pt="${esc(p.name)}" value="${esc(t.name)}" ${p.tools_all||toolset.has(t.name)?'checked':''}>`+
      `<span class=dot></span><span class=mono style="font-size:11.5px" title="${esc(t.desc)}">${escT(t.name)}</span></label>`).join('');
    const secrets=(p.secrets||[]).map(s=>`<span class="tag tag-neutral" style="font-size:11px">${escT(s)}</span>`).join(' ')||'<span class=text-muted style="font-size:12px">none</span>';
    /* MCP: assign from the catalog right here. A server that needs ${SECRET}
       placeholders shows them — released ones as a key, missing ones as a
       warning (release them in the Secrets tab, or the config stays a
       placeholder and the agent reports it as unresolved). */
    const assigned=new Set(p.mcps||[]), rel=new Set(p.secrets||[]);
    const mcps=(MCPS||[]).map(m=>{
      const need=[...new Set(JSON.stringify(m).split('${').slice(1).map(s=>s.split('}')[0]))];
      const hint=need.length?` <span class=text-muted style="font-size:11px" title="secrets this server needs — released in the Secrets tab?">${need.map(k=>(rel.has(k)?'🔑 ':'⚠️ ')+escT(k)).join(' ')}</span>`:'';
      return `<label class=radio style="font-size:12.5px"><input type=checkbox data-pm="${esc(p.name)}" value="${esc(m.name)}" ${assigned.has(m.name)?'checked':''}>`+
        `<span class=dot></span><span class=mono style="font-size:11.5px" title="${esc(m.description||'')}">${escT(m.name)}</span>${hint}</label>`;
    }).join('')||'<span class=text-muted style="font-size:12px">catalog empty — add servers in the MCP tab</span>';
    return `<div class="card blueprint" style="padding:16px">${CORNERS}`+
      `<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">`+
        `<span class=card-title style="font-size:17px">${escT(p.name)}</span>`+
        `<span class=text-muted style="font-size:12px">${escT(p.template)}</span>`+
        tag(p.running,'● running','○ off')+
        `<span style="margin-left:auto;display:flex;gap:8px;align-items:center">`+
          `<button class="btn ${p.internet?'btn-secondary':'btn-primary'} btn-sm" onclick="toggleNet('${esc(p.name)}',${(!p.internet)})">${p.internet?'🌐 internet on':'🚫 offline'}</button>`+
          `<button class="btn btn-secondary btn-sm" onclick="openActivity('${esc(p.name)}')">Activity</button>`+
        `</span></div>`+
      (p.model?`<div class=kv><b>Model</b><span class=mono style="font-size:12px">${escT(p.model)}</span></div>`:'')+
      `<div class=kv><b>Secrets</b><span>${secrets}</span></div>`+
      `<div class=kv><b>MCP</b><span style="display:flex;flex-wrap:wrap;gap:2px 14px;align-items:center">${mcps}`+
        `<button class="btn btn-primary btn-sm" onclick="savePolMcps('${esc(p.name)}')">Save MCP</button><span class=msg data-mmsg="${esc(p.name)}"></span></span></div>`+
      (p.katfs_share?`<div class=kv><b>katfs</b><span class=mono style="font-size:12px">${escT(p.katfs_share)}</span></div>`:'')+
      `<div style="margin-top:8px"><div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">`+
        `<b style="font-family:var(--font-heading);font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:color-mix(in srgb,var(--color-text) 60%,transparent)">Tools</b>`+
        `<button class="btn btn-ghost" style="font-size:12px" onclick="polToolAll('${esc(p.name)}',1)">all</button>`+
        `<button class="btn btn-ghost" style="font-size:12px" onclick="polToolAll('${esc(p.name)}',0)">none</button>`+
        `<button class="btn btn-primary btn-sm" style="margin-left:auto" onclick="savePolTools('${esc(p.name)}')">Save tools</button>`+
        `<span class=msg data-tmsg="${esc(p.name)}"></span>`+
      `</div><div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:2px 14px">${tools}</div></div>`+
    `</div>`;
  }).join('')||'<span class=text-muted style="font-size:13px">no instances</span>';
  if(!auto)POL_DIRTY.clear();
}
function polToolAll(name,on){document.querySelectorAll(`input[data-pt="${CSS.escape(name)}"]`).forEach(c=>c.checked=!!on)}
function savePolTools(name){
  const tools=[...document.querySelectorAll(`input[data-pt="${CSS.escape(name)}"]:checked`)].map(c=>c.value);
  fetch(`/api/instances/${name}/tools`,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({tools})}).then(async r=>{
      const ok=r.ok; let d={}; try{d=await r.json()}catch(e){}
      const el=document.querySelector(`[data-tmsg="${CSS.escape(name)}"]`);
      if(ok){POL_DIRTY.delete(name); if(el)el.textContent=(d.msg||'saved')+' ✓';}
      else if(el)el.textContent='⚠️ '+(d.msg||('HTTP '+r.status));
    }).catch(e=>{const el=document.querySelector(`[data-tmsg="${CSS.escape(name)}"]`);if(el)el.textContent='⚠️ '+e;});
}
function savePolMcps(name){
  const list=[...document.querySelectorAll(`input[data-pm="${CSS.escape(name)}"]:checked`)].map(c=>c.value);
  fetch(`/api/instances/${name}/config`,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({key:'MCP_SERVERS',value:list.join(',')})}).then(async r=>{
      let d={}; try{d=await r.json()}catch(e){}
      const el=document.querySelector(`[data-mmsg="${CSS.escape(name)}"]`); if(!el)return;
      const m=d.msg||('HTTP '+r.status), bad=!r.ok||/error/.test(m);
      if(!bad)POL_DIRTY.delete(name);
      el.innerHTML=(bad?'⚠️ ':'✓ ')+escT(m)+(/applies after/.test(m)?` <button class="btn btn-secondary btn-sm" onclick="act('${esc(name)}','restart')">Restart now</button>`:'');
    }).catch(e=>{const el=document.querySelector(`[data-mmsg="${CSS.escape(name)}"]`);if(el)el.textContent='⚠️ '+e;});
}
function _bar(pct,max,color){
  const w=Math.max(0,Math.min(100,max?100*pct/max:0));
  return `<div style="background:var(--color-neutral-200,#e5e7eb);border-radius:4px;height:6px;overflow:hidden;min-width:60px"><div style="width:${w}%;height:100%;background:${color}"></div></div>`;
}
async function loadResources(){
  loadUsageChart();                       // the chart below the table, same tab, both entry points
  let rs=[]; try{rs=(await (await fetch('/api/resources')).json()).resources||[];}catch(e){return;}
  const el=document.getElementById('resrows'); if(!el)return;
  el.innerHTML = rs.length ? rs.map(r=>{
    const run=r.running;
    const status=run?`<span class="tag tag-accent">\u25cf running</span>`:`<span class="tag tag-neutral">\u25cb off</span>`;
    const ramUsed = run&&r.rss_mb!=null ? `${r.rss_mb} MB ${_bar(r.rss_mb,r.mem_mib,'var(--color-accent)')}` : '\u2014';
    const cpu = run&&r.cpu_pct!=null ? `${r.cpu_pct}% ${_bar(r.cpu_pct,100*r.vcpus,'#2e9e6b')}` : '\u2014';
    const disk = r.upper_used_mb!=null ? `${r.upper_used_mb} MB${r.persist?' <span class="tag tag-accent" style="font-size:10px">persist</span>':''}` : '\u2014';
    return `<tr><td data-label=Instance><b style="font-family:var(--font-heading)">${escT(r.name)}</b></td>`+
      `<td data-label=Status>${status}</td>`+
      `<td data-label=vCPU style="font-variant-numeric:tabular-nums">${r.vcpus}</td>`+
      `<td data-label="RAM (config.)" style="font-variant-numeric:tabular-nums">${r.mem_mib} MiB</td>`+
      `<td data-label="RAM (used)"><div style="display:flex;align-items:center;gap:8px;font-size:12px">${ramUsed}</div></td>`+
      `<td data-label=CPU><div style="display:flex;align-items:center;gap:8px;font-size:12px">${cpu}</div></td>`+
      `<td data-label="Disk (Overlay)" style="font-size:12px">${disk}</td></tr>`;
  }).join('') : '<tr><td colspan=7 class=text-muted style="padding:14px">no instances</td></tr>';
}
/* — LLM usage chart (Resources tab): one row per instance, tokens stacked
   by model and a cost bar, each scaled to the largest instance. — */
let USAGE_WIN=0;
const MODEL_HUES=[212,28,150,268,92,340,48,190];
function usageWindow(sec){
  USAGE_WIN=sec;
  document.querySelectorAll('#uwin button').forEach(b=>b.classList.toggle('on',(+b.dataset.w)===sec));
  loadUsageChart();
}
async function loadUsageChart(){
  const el=document.getElementById('usagechart'); if(!el)return;
  const since=USAGE_WIN?Math.floor(Date.now()/1000)-USAGE_WIN:0;
  let r,rows=[];
  try{r=await fetch('/api/usage-by-model?since='+since)}catch(e){el.textContent='usage unavailable (network: '+e.message+')';return;}
  if(!r.ok){el.textContent='usage unavailable (HTTP '+r.status+')';return;}
  try{rows=(await r.json()).rows||[];}catch(e){el.textContent='usage unavailable (answer is not JSON)';return;}
  if(!rows.length){el.textContent='no LLM calls in this window';return;}
  const models=[]; rows.forEach(r=>{if(models.indexOf(r.model)<0)models.push(r.model)});
  const col=m=>`hsl(${MODEL_HUES[models.indexOf(m)%MODEL_HUES.length]} 55% ${models.indexOf(m)>=MODEL_HUES.length?35:50}%)`;
  const inst={};
  rows.forEach(r=>{const i=inst[r.instance]||(inst[r.instance]={name:r.instance,tin:0,tout:0,cost:0,calls:0,parts:[]});
    i.tin+=r.in; i.tout+=r.out; i.cost+=r.cost; i.calls+=r.calls; i.parts.push(r);});
  const list=Object.values(inst).sort((a,b)=>(b.tin+b.tout)-(a.tin+a.tout));
  const maxI=Math.max(...list.map(i=>i.tin),1), maxO=Math.max(...list.map(i=>i.tout),1), maxC=Math.max(...list.map(i=>i.cost),1e-9);
  const totI=list.reduce((s,i)=>s+i.tin,0), totO=list.reduce((s,i)=>s+i.tout,0), totC=list.reduce((s,i)=>s+i.cost,0);
  const cell=(l,v)=>`<td data-label="${l}">${v}</td>`;
  /* in and out each on their own scale (out is ~1% of in): a stacked bar by model, the figure beside it */
  const stack=(i,key,max)=>`<div style="display:flex;align-items:center;gap:8px;font-size:12px"><span style="font-variant-numeric:tabular-nums;min-width:48px;text-align:right">${fmtTok(i[key==='in'?'tin':'tout'])}</span>`+
    `<div style="display:flex;height:12px;flex:0 0 ${Math.max(1,100*i[key==='in'?'tin':'tout']/max).toFixed(1)}%;max-width:calc(100% - 56px);min-width:2px;border-radius:3px;overflow:hidden">`+
    i.parts.filter(p=>p[key]>0).sort((a,b)=>b[key]-a[key]).map(p=>`<div title="${esc(p.model)}: ${fmtTok(p.in)} in / ${fmtTok(p.out)} out · ${fmtCost(p.cost)}" style="width:${(100*p[key]/Math.max(i[key==='in'?'tin':'tout'],1)).toFixed(2)}%;background:${col(p.model)}"></div>`).join('')+`</div></div>`;
  el.innerHTML=`<table class=table><thead><tr><th style="width:16%">Instance</th><th style="width:28%">Tokens in (by model)</th><th style="width:28%">Tokens out (by model)</th><th style="width:20%">Cost</th><th style="width:8%;text-align:right">Calls</th></tr></thead><tbody>`+
    list.map(i=>`<tr>${cell('Instance',`<b style="font-family:var(--font-heading)">${escT(i.name)}</b>`)}`+
      cell('Tokens in',stack(i,'in',maxI))+cell('Tokens out',stack(i,'out',maxO))+
      cell('Cost',`<div style="display:flex;align-items:center;gap:8px;font-size:12px"><span style="font-variant-numeric:tabular-nums;min-width:52px">${fmtCost(i.cost)}</span>${_bar(i.cost,maxC,'var(--color-accent)')}</div>`)+
      cell('Calls',`<span style="font-variant-numeric:tabular-nums;font-size:12px;float:right">${i.calls}</span>`)+`</tr>`).join('')+
    `</tbody><tfoot><tr><td class=text-muted style="font-size:12px">${list.length} instances · ${models.length} models</td><td style="font-size:12px;font-variant-numeric:tabular-nums;padding-left:8px">${fmtTok(totI)} in</td><td style="font-size:12px;font-variant-numeric:tabular-nums;padding-left:8px">${fmtTok(totO)} out</td><td style="font-size:12px;font-variant-numeric:tabular-nums">${fmtCost(totC)}</td><td></td></tr></tfoot></table>`+
    `<div style="display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:10px;font-size:12px">`+
    models.map(m=>{const rs=rows.filter(r=>r.model===m), ti=rs.reduce((s,r)=>s+r.in,0), to=rs.reduce((s,r)=>s+r.out,0), c=rs.reduce((s,r)=>s+r.cost,0);
      return `<span style="display:inline-flex;align-items:center;gap:6px"><span style="width:10px;height:10px;border-radius:2px;background:${col(m)}"></span><span class=mono>${escT(m)}</span><span class=text-muted style="font-variant-numeric:tabular-nums">${fmtTok(ti)} in · ${fmtTok(to)} out · ${fmtCost(c)}</span></span>`}).join('')+`</div>`;
}
let ACT_CUR='', ACT_EVENTS=[], ACT_WIN=0;
async function openActivity(name){
  ACT_CUR=name;
  document.getElementById('actname').textContent=name;
  document.getElementById('actdlg').style.display='grid';
  document.getElementById('actrows').innerHTML='<tr><td class=text-muted style="padding:12px">…</td></tr>';
  try{ACT_EVENTS=(await (await fetch('/api/audit/'+encodeURIComponent(name))).json()).events||[];}catch(e){ACT_EVENTS=[];}
  ACT_TURNS={};
  try{for(const t of ((await (await fetch('/api/trace/'+encodeURIComponent(name)+'?limit=200')).json()).turns||[]))ACT_TURNS[t.turn]=t;}catch(e){}
  actRender();
}
let ACT_TURNS={}, ACT_OPEN={};
function actToggle(id){ACT_OPEN[id]=!ACT_OPEN[id];actRender();}
function actMs(ms){return ms==null?'':(ms>=1000?(ms/1000).toFixed(1)+' s':ms+' ms');}
function actWindow(sec){
  ACT_WIN=sec;
  document.querySelectorAll('#actwin button').forEach(b=>b.classList.toggle('on',(+b.dataset.w)===sec));
  actRender();
}
function actRow(e){
  const d=new Date((e.ts||0)*1000).toLocaleString(undefined,{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});
  // Failed calls show WHY (the audit carries the error text now); healthy
  // ones show a peek at the result. The word changed too: "denied" implied
  // policy, but ok:false mostly means the call itself failed.
  const extra = e.ok===false
    ? (e.err?`<div style="font-size:11px;color:var(--color-neutral-600)">${escT(e.err)}</div>`:'')
    : (e.result?`<div class=text-muted style="font-size:11px">${escT(e.result)}</div>`:'');
  const ms = e.ms!=null?`<span class=text-muted style="font-size:11px;white-space:nowrap"> · ${actMs(e.ms)}</span>`:'';
  return `<tr><td class=text-muted style="white-space:nowrap;font-size:12px">${d}</td>`+
    `<td class=mono style="font-size:12.5px">${escT(e.tool)}${e.ok===false?' <span class="tag tag-neutral" style="font-size:10px">failed</span>':''}</td>`+
    `<td class=mono style="font-size:12px;word-break:break-all;color:var(--color-accent-700)">${escT(e.target||'')}${ms}${extra}</td></tr>`;
}
function actRender(){
  const cut = ACT_WIN ? (Date.now()/1000 - ACT_WIN) : 0;
  const ev = ACT_EVENTS.filter(e=>(e.ts||0) >= cut);
  // Traces: audit lines grouped by turn (newest turn first, as the audit is);
  // a group head carries what the turn row knows — duration, steps, LLM
  // calls, tokens, outcome. Lines without a turn (old audits, notify,
  // send_signal) stay as "unbound" at the end.
  const groups=[], byTurn={}, loose=[];
  for(const e of ev){
    if(!e.turn){loose.push(e);continue;}
    if(!byTurn[e.turn]){byTurn[e.turn]={id:e.turn,rows:[]};groups.push(byTurn[e.turn]);}
    byTurn[e.turn].rows.push(e);
  }
  let html='';
  for(const g of groups){
    const t=ACT_TURNS[g.id]||{}, open=!!ACT_OPEN[g.id], first=g.rows[g.rows.length-1];
    const d=new Date(((t.ts_start||first.ts)||0)*1000).toLocaleString(undefined,{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});
    const failed=g.rows.filter(e=>e.ok===false).length;
    const oc=t.outcome||'', ocTag=oc&&oc!=='ok'?` <span class="tag tag-neutral" style="font-size:10px">${escT(oc)}</span>`:'';
    const facts=[t.ms!=null?actMs(t.ms):null, t.steps!=null?t.steps+' steps':null,
      t.llm_calls?t.llm_calls+' LLM'+(t.llm_failed?' ('+t.llm_failed+' failed)':''):null,
      (t.in||t.out)?(t.in||0)+'/'+(t.out||0)+' tok':null,
      g.rows.length+' tool'+(g.rows.length===1?'':'s')+(failed?' ('+failed+' failed)':'')].filter(Boolean).join(' · ');
    html+=`<tr style="cursor:pointer;background:var(--color-neutral-50)" onclick="actToggle('${esc(g.id)}')">`+
      `<td class=text-muted style="white-space:nowrap;font-size:12px">${open?'▾':'▸'} ${d}</td>`+
      `<td class=mono style="font-size:12px">${escT(t.kind||'turn')} <span class=text-muted>${escT(g.id)}</span>${ocTag}</td>`+
      `<td class=text-muted style="font-size:11.5px">${escT(facts)}</td></tr>`;
    if(open) html+=g.rows.map(actRow).join('');
  }
  if(loose.length){
    if(groups.length) html+=`<tr><td colspan=3 class=text-muted style="font-size:11px;padding:6px 8px">unbound (no turn)</td></tr>`;
    html+=loose.map(actRow).join('');
  }
  document.getElementById('actrows').innerHTML=html||'<tr><td class=text-muted style="padding:12px">nothing in the selected range</td></tr>';
  actUsage(cut, ev.length);
}
async function actUsage(cut, nEv){
  const el=document.getElementById('actsum'); if(!el)return;
  el.textContent='…';
  try{
    const u=await (await fetch('/api/usage/'+encodeURIComponent(ACT_CUR)+'?since='+Math.floor(cut))).json();
    const k=n=>n>=1000?(n/1000).toFixed(n>=100000?0:1)+'k':(''+n);
    el.textContent=`${nEv} Aktionen · ${u.calls} LLM-Aufrufe · ${k(u.in)}→${k(u.out)} Tokens · $${(u.cost||0).toFixed(4)}`;
  }catch(e){el.textContent=`${nEv} Aktionen`;}
}
function actClose(){document.getElementById('actdlg').style.display='none'}

/* — Tasks: scheduled work per instance (backend: /api/tasks, worker in the manager) — */
function fmtTs(t){if(!t)return '—';const d=new Date(t*1000);
  return d.toLocaleString(undefined,{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});}
async function loadMissions(){
  const el=document.getElementById('missions'); if(!el)return;
  let d={}; try{d=await (await fetch('/api/missions')).json()}catch(e){}
  const all=d.by_instance?Object.entries(d.by_instance).flatMap(([i,l])=>l.map(m=>({...m,_inst:i})))
            :(d.missions||[]).map(m=>({...m,_inst:'orchestrator'}));
  const open=all.filter(m=>m.status==='active'||m.status==='paused');
  const closed=all.filter(m=>m.status==='done'||m.status==='failed');   // all of them: they can be edited/deleted
  const cor='';
  const bar=m=>{const t=(m.steps||[]).length||1,dn=(m.steps||[]).filter(s=>s.status==='done').length;
    return `<div style="display:flex;align-items:center;gap:8px;min-width:130px">
      <div style="flex:1;height:5px;background:var(--color-neutral-200)"><div style="width:${Math.round(dn/t*100)}%;height:100%;background:var(--color-accent)"></div></div>
      <span class=text-muted style="font-size:11.5px;white-space:nowrap">${dn}/${t}</span></div>`};
  const row=m=>{
    const cur=(m.steps||[]).find(s=>s.status==='doing')||(m.steps||[]).find(s=>s.status==='open');
    const st={active:'tag-accent',paused:'tag-neutral',done:'tag-accent-2',failed:'tag-neutral'}[m.status]||'tag-neutral';
    const log=(m.log||[]).slice(-1)[0]||'';
    return `<div style="padding:12px 4px;border-bottom:1px solid var(--color-divider)">
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <span class=mono style="font-size:11px;color:var(--color-neutral-500)">${escT(m.id)}</span>
        <b style="font-size:14.5px">${escT(m.goal)}</b>
        <span class="tag ${st}">${m.status}</span>
        <span class="tag tag-neutral" title="owner of the mission">${escT(m._inst||'?')}</span>
        ${bar(m)}
        <span style="margin-left:auto;display:flex;gap:6px">
          ${m.status==='active'?`<button class="btn btn-secondary btn-sm" onclick="missionAct('${esc(m.id)}','pause','${esc(m._inst||'')}')">Pause</button>`:''}
          ${m.status==='paused'?`<button class="btn btn-secondary btn-sm" onclick="missionAct('${esc(m.id)}','resume','${esc(m._inst||'')}')">Resume</button>`:''}
          ${(m.status==='active'||m.status==='paused')?`<button class="btn btn-ghost btn-sm" onclick="missionAct('${esc(m.id)}','abort','${esc(m._inst||'')}')">Cancel</button>`:''}
          <button class="btn btn-secondary btn-sm" onclick="missionEdit('${esc(m.id)}')">Edit</button>
          <button class="btn btn-ghost btn-sm" onclick="missionAct('${esc(m.id)}','delete','${esc(m._inst||'')}')">Delete</button>
        </span>
      </div>
      ${cur?`<div class=text-muted style="font-size:12.5px;margin-top:4px">current step ${cur.n}: ${escT(cur.text)} [${cur.status}]${cur.target?` · on <b>${escT(cur.target)}</b>`:''}${cur.task_id?` · task <span class=mono>${escT(cur.task_id)}</span>`:''}</div>`:''}
      ${m.summary?`<div class=text-muted style="font-size:12.5px;margin-top:4px">Summary: ${escT(m.summary)}</div>`:''}
      ${log?`<div class=text-muted style="font-size:11.5px;margin-top:3px;opacity:.75">${escT(log)}</div>`:''}
    </div>`};
  document.getElementById('missions').innerHTML=cor+
    (open.length||closed.length
      ? open.map(row).join('')
        + (closed.length?`<div class=text-muted style="margin:14px 0 4px;font-size:10px;letter-spacing:.1em;text-transform:uppercase">Recently completed</div>${closed.map(row).join('')}`:'')
      : '<span class=text-muted style="font-size:13px">No missions. Any agent creates them itself for multi-step jobs (mission_start) — e.g. via chat: "… — as a mission".</span>');
}
async function missionAct(id,action,instance){
  if(action==='abort'&&!confirm('Abort mission '+id+'?'))return;
  if(action==='delete'&&!confirm('Delete mission '+id+' for good? (any status)'))return;
  await fetch('/api/mission-admin',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id,action,instance:instance||''})}).catch(()=>{});
  loadMissions();
}
let _miEdit=null;   // the mission currently open in the edit dialog
async function missionEdit(id){
  let d={}; try{d=await (await fetch('/api/missions')).json()}catch(e){return;}
  const all=Object.entries(d.by_instance||{}).flatMap(([i,l])=>l.map(m=>({...m,_inst:i})));
  const m=all.find(x=>x.id===id); if(!m)return;
  _miEdit=m;
  document.getElementById('midlgid').textContent=m.id+' · '+(m._inst||'');
  document.getElementById('migoal').value=m.goal||'';
  document.getElementById('misteps').value=(m.steps||[]).map(s=>s.text).join('\\n');
  document.getElementById('mistatus').value=m.status||'active';
  document.getElementById('mihint').textContent=(m.steps||[]).map(s=>`${s.n}: ${s.status}`).join(' · ');
  document.getElementById('midlg').style.display='grid';
}
function midlgClose(){document.getElementById('midlg').style.display='none';_miEdit=null;}
async function missionSave(){
  if(!_miEdit)return;
  const body={id:_miEdit.id,action:'edit',instance:_miEdit._inst||'',
    goal:document.getElementById('migoal').value,
    steps:document.getElementById('misteps').value.split('\\n').map(s=>s.trim()).filter(Boolean),
    status:document.getElementById('mistatus').value};
  const r=await (await fetch('/api/mission-admin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json().catch(()=>({msg:'?'}));
  if(r.msg!=='ok'&&r.msg!=='unchanged'){document.getElementById('mihint').textContent='\u26a0\ufe0f '+r.msg;return;}
  midlgClose();loadMissions();
}
async function loadTasks(){
  let tasks=[],insts=[];
  try{
    tasks=await (await fetch('/api/tasks')).json();
    insts=await (await fetch('/api/instances')).json();
  }catch(e){document.getElementById('taskrows').innerHTML='<tr><td colspan=6 class=text-muted>not reachable</td></tr>';return;}
  const sel=document.getElementById('tk-inst');
  if(sel)sel.innerHTML=insts.map(i=>`<option value="${esc(i.name)}">${escT(i.name)}</option>`).join('')||'<option value="">— no instance —</option>';
  const tag={scheduled:'tag-accent',pending:'tag-accent',running:'tag-accent',done:'tag-neutral',error:'tag-neutral'};
  TASKS=tasks||[];
  document.getElementById('taskrows').innerHTML=(tasks||[]).map(t=>{
    const nr=t.schedule?` · next ${fmtTs(t.next_run)}`:'';
    const res=(t.result||'').slice(0,120);
    return `<tr><td data-label=Instance class=mono>${escT(t.instance)}</td>`+
      `<td data-label=Job>${escT((t.message||'').slice(0,90))}${(t.message||'').length>90?'…':''}</td>`+
      `<td data-label=Schedule class=mono style="font-size:12px">${escT(t.schedule||'once')}${nr}</td>`+
      `<td data-label=Status><span class="tag ${tag[t.status]||'tag-neutral'}">${escT(t.status)}</span></td>`+
      `<td data-label=Result class=text-muted style="font-size:12px">${escT(res)}</td>`+
      `<td style="white-space:nowrap">`+
      `<button class="btn btn-icon btn-secondary" style="width:30px;height:30px;margin-right:4px" `+
        `title="Run now" onclick="runTask('${esc(t.id)}')" ${t.status==='running'?'disabled':''}>${I_RUN}</button>`+
      `<button class="btn btn-icon btn-secondary" style="width:30px;height:30px;margin-right:4px" `+
        `title="Edit (schedule / job)" onclick="editTask('${esc(t.id)}')">${I_EDIT}</button>`+
      `<button class="btn btn-icon btn-secondary" style="width:30px;height:30px" `+
        `title=Delete onclick="delTask('${esc(t.id)}')">${I_DEL}</button></td></tr>`;
  }).join('')||'<tr><td colspan=6 class=text-muted style="padding:14px">no tasks yet</td></tr>';
}
let TASKS=[], TK_EDIT='';
/* Editing goes through the same form — a second dialog would be maintained
   twice. The instance stays locked: switching it would be a different task
   (different tools/secrets), that is what Create is for. */
function editTask(id){
  const t=TASKS.find(x=>x.id===id); if(!t)return;
  TK_EDIT=id;
  const inst=document.getElementById('tk-inst');
  inst.value=t.instance; inst.disabled=false;
  document.getElementById('tk-sched').value=t.schedule||'';
  document.getElementById('tk-msg').value=t.message||'';
  document.getElementById('tk-head').textContent='Edit task · '+t.instance;
  document.getElementById('tk-save').textContent='Save changes';
  document.getElementById('tk-cancel').style.display='';
  document.getElementById('tkmsg').textContent='';
  document.getElementById('tk-head').scrollIntoView({behavior:'smooth',block:'center'});
}
function cancelEdit(){
  TK_EDIT='';
  const inst=document.getElementById('tk-inst'); inst.disabled=false;
  document.getElementById('tk-sched').value='';
  document.getElementById('tk-msg').value='';
  document.getElementById('tk-head').textContent='New task';
  document.getElementById('tk-save').textContent='Create task';
  document.getElementById('tk-cancel').style.display='none';
}
function saveTask(){
  const instance=document.getElementById('tk-inst').value,
        message=document.getElementById('tk-msg').value.trim(),
        schedule=document.getElementById('tk-sched').value.trim();
  if(!message)return alert('Job?');
  if(TK_EDIT){
    fetch('/api/tasks/'+encodeURIComponent(TK_EDIT)+'/update',
      {method:'POST',headers:{'Content-Type':'application/json'},
       body:JSON.stringify({message,schedule,instance})})
      .then(r=>r.json()).then(d=>{
        document.getElementById('tkmsg').textContent=(d.msg||'saved')+' ✓';
        cancelEdit();loadTasks();});
    return;
  }
  if(!instance)return alert('Instance?');
  fetch('/api/tasks',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({instance,message,schedule})})
    .then(r=>r.json()).then(d=>{document.getElementById('tkmsg').textContent=(d.msg||'created')+' ✓';
      document.getElementById('tk-msg').value='';loadTasks();});
}
async function runTask(id){const r=await (await fetch('/api/tasks/'+encodeURIComponent(id)+'/run',{method:'POST'})).json();if(!/queued/.test(r.msg||''))alert(r.msg||'?');loadTasks();}
async function delTask(id){if(confirm('Delete task?')){await fetch('/api/tasks/'+encodeURIComponent(id)+'/delete',{method:'POST'});loadTasks();}}

/* — Usage: the same numbers as server-side, just fetched afterwards — */
function fmtTok(n){n=n||0;
  return n>=1e6?(n/1e6).toFixed(1).replace(/\\.0$/,'')+'M'
       : n>=1000?(n/1000).toFixed(1).replace(/\\.0$/,'')+'k' : String(n)}
function fmtCost(c){c=c||0; return '$'+(c>=0.01?c.toFixed(2):c.toFixed(4))}
async function refreshUsage(){
  let u; try{u=await (await fetch('/api/usage')).json()}catch(e){return}
  let day=0,all=0;
  document.querySelectorAll('[data-usage]').forEach(el=>{
    const v=u[el.dataset.usage]; if(!v)return;
    el.innerHTML='Tokens today '+fmtTok(v.today.in)+'&nbsp;/&nbsp;'+fmtTok(v.today.out)+
      ' · '+fmtCost(v.today.cost)+' &nbsp;·&nbsp; total '+fmtTok(v.total.in)+
      '&nbsp;/&nbsp;'+fmtTok(v.total.out)+' · '+fmtCost(v.total.cost);
  });
  Object.values(u).forEach(v=>{day+=(v.today||{}).cost||0; all+=(v.total||{}).cost||0});
  const f=document.getElementById('spend');
  if(f)f.textContent='LLM today '+fmtCost(day)+' · total '+fmtCost(all);
}

/* — tabs (hash-routed, so a reload after an action keeps the screen) — */
const TABS=['instances','personas','skills','plugins','mcp','tasks','missions','policy','models','resources','sharing','secrets','settings','changelog','architecture'];
function showTab(t){
  if(TABS.indexOf(t)<0)t='instances';
  TABS.forEach(x=>document.getElementById('s-'+x).classList.toggle('on',x===t));
  document.querySelectorAll('#tabs a,.af-nav a').forEach(a=>{
    if(a.getAttribute('href')==='#'+t)a.setAttribute('aria-current','page');
    else a.removeAttribute('aria-current');});
  /* the row scrolls without a visible bar: keep the active tab in view */
  const cur=document.querySelector('#tabs a[aria-current=page]');
  if(cur&&cur.scrollIntoView)cur.scrollIntoView({inline:'nearest',block:'nearest'});
}
window.addEventListener('hashchange',()=>{const t=location.hash.slice(1);showTab(t);if(t==='missions')loadMissions();if(t==='sharing'){loadKatfs();loadIroh();}});

async function loadPlaybooks(){
  const sel=document.getElementById('pbinst');
  if(!sel.options.length){
    try{const insts=await (await fetch('/api/instances')).json();
      sel.innerHTML=insts.map(i=>`<option${i.name==='orchestrator'?' selected':''}>${escT(i.name)}</option>`).join('');
    }catch(e){}
  }
  const inst=sel.value||'orchestrator';
  let pbs=[]; try{pbs=(await (await fetch('/api/playbooks?instance='+encodeURIComponent(inst))).json()).playbooks||[]}catch(e){}
  document.getElementById('pblist').innerHTML=pbs.length?pbs.map(p=>
    `<div style="display:flex;gap:10px;align-items:baseline;padding:7px 4px;border-bottom:1px solid var(--color-divider)">`+
    `<span class=mono style="flex:none;font-size:11px;color:var(--color-neutral-500)">${escT(p.id||'')}</span>`+
    `<span style="flex:1;font-size:13px">${escT(p.text||'')}</span>`+
    `<button class="btn btn-ghost btn-sm" onclick="delPlaybook('${esc(inst)}','${esc(p.id)}')">✕</button></div>`).join('')
    :'<span class=text-muted style="font-size:13px">No rules for this agent.</span>';
}
async function addPlaybook(){
  const inst=document.getElementById('pbinst').value,text=document.getElementById('pbtext').value.trim();
  if(!text)return;
  const d=await (await fetch('/api/playbook-add',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({instance:inst,text})})).json();
  document.getElementById('pbmsg').textContent=d.added?'added ✓':(d.note||'?');
  if(d.added)document.getElementById('pbtext').value='';
  loadPlaybooks();
}
async function delPlaybook(inst,id){
  await fetch('/api/playbook-remove',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({instance:inst,id})});
  loadPlaybooks();
}
let PROMPTS=[];
async function loadPrompts(){
  try{PROMPTS=(await (await fetch('/api/prompts')).json()).prompts||[]}catch(e){PROMPTS=[]}
  const el=document.getElementById('promptlist'); if(!el)return;
  el.innerHTML=PROMPTS.length?PROMPTS.map(p=>
    `<div style="display:flex;gap:10px;align-items:baseline;padding:7px 4px;border-bottom:1px solid var(--color-divider)">`+
    `<code style="flex:none">/${escT(p.name)}</code>`+
    `<span class=text-muted style="flex:1;font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escT(p.text)}</span>`+
    `<button class="btn btn-ghost btn-sm" onclick="editPrompt('${esc(p.name)}')">Edit</button>`+
    `<button class="btn btn-ghost btn-sm" onclick="delPrompt('${esc(p.name)}')">✕</button></div>`).join('')
    :'<span class=text-muted style="font-size:13px">No templates. Create one below — then usable in chat via /name.</span>';
}
function editPrompt(n){const p=PROMPTS.find(x=>x.name===n);if(!p)return;
  document.getElementById('prname').value=p.name;document.getElementById('prtext').value=p.text;}
async function savePrompt(){
  const name=document.getElementById('prname').value.trim(),text=document.getElementById('prtext').value.trim();
  const d=await (await fetch('/api/prompts',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name,text})})).json();
  document.getElementById('prmsg').textContent=d.msg||'?';
  if(d.msg==='saved'){document.getElementById('prname').value='';document.getElementById('prtext').value='';}
  loadPrompts();
}
async function delPrompt(n){
  if(!confirm('Delete template /'+n+'?'))return;
  await fetch('/api/prompts',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:n,delete:true})});
  loadPrompts();
}
function renderPersonas(){
  document.getElementById('personas').innerHTML=PERSONAS.map(p=>
    `<div class="card blueprint">${CORNERS}`+
    `<div style="display:flex;align-items:center;gap:10px">`+
    `<span class=card-title style="font-size:16px">${escT(p.name)}</span>`+
    `<span style="margin-left:auto;display:flex;gap:4px">`+
    `<button class="btn btn-ghost" style="font-size:12px" onclick="editPersona('${esc(p.name)}')">Edit</button>`+
    `<button class="btn btn-ghost" style="font-size:12px;color:var(--color-neutral-600)" onclick="delPersona('${esc(p.name)}')">Delete</button>`+
    `</span></div>`+
    `<p class=card-body style="display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden">${escT(p.prompt||'')}</p></div>`)
    .join('')||'<span class=text-muted style="font-size:13px">none</span>';
  const sel=document.getElementById('persona');
  if(sel)sel.innerHTML='<option value="">— Default —</option>'+PERSONAS.map(p=>`<option value="${esc(p.name)}">${escT(p.name)}</option>`).join('');
}
function editPersona(n){const p=PERSONAS.find(x=>x.name===n);if(!p)return;
  document.getElementById('pname').value=p.name;document.getElementById('pprompt').value=p.prompt||'';
  document.getElementById('pname').scrollIntoView({behavior:'smooth'});}
function savePersona(){
  const name=document.getElementById('pname').value.trim(),prompt=document.getElementById('pprompt').value;
  if(!name)return alert('Name?');
  fetch('/api/personas',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,prompt})})
    .then(()=>location.reload());
}
async function delPersona(n){if(confirm('Delete persona '+n+'?')){await fetch('/api/personas/'+encodeURIComponent(n)+'/delete',{method:'POST'});location.reload()}}

async function loadProposals(){
  const el=document.getElementById('skprops'); if(!el)return;
  let ps=[]; try{ps=(await (await fetch('/api/skill-proposals')).json()).proposals||[];}catch(e){ps=[];}
  if(!ps.length){el.innerHTML='';return;}
  // Skills the agents distilled from their own runs: nothing enters the
  // catalog without a click here.
  el.innerHTML=`<div class=text-muted style="font-size:12px;margin:0 0 6px">Proposed by agents — approve to add to the catalog</div>`+
    `<div class=grid3 style="margin-bottom:14px">`+ps.map(p=>
    `<div class="card blueprint">${CORNERS}`+
    `<div style="display:flex;align-items:center;gap:8px">`+
    `<span class=card-title style="font-size:15px;font-family:var(--font-mono);font-weight:500">${escT(p.name)}</span>`+
    (p.update?`<span class="tag tag-neutral" style="font-size:10px">update</span>`:'')+
    `<span class=text-muted style="font-size:11px;margin-left:auto">${escT(p.instance||'')}</span></div>`+
    `<p class=card-body style="font-size:12.5px">${escT(p.description||'')}</p>`+
    `<details style="font-size:12px;margin:4px 0 8px"><summary class=text-muted style="cursor:pointer">content</summary>`+
    `<pre style="white-space:pre-wrap;font-size:11.5px;max-height:260px;overflow:auto;margin:6px 0 0">${escT(p.content||'')}</pre></details>`+
    `<div style="display:flex;gap:6px"><button class="btn btn-primary btn-sm" onclick="decideProposal('${esc(p.id)}','approve')">Approve</button>`+
    `<button class="btn btn-secondary btn-sm" onclick="decideProposal('${esc(p.id)}','discard')">Discard</button></div></div>`).join('')+`</div>`;
}
async function decideProposal(id,what){
  const r=await (await fetch('/api/skill-proposals/'+encodeURIComponent(id)+'/'+what,{method:'POST'})).json();
  if(what==='approve')location.reload(); else loadProposals();
  if(r.msg&&/error|unknown/.test(r.msg))alert(r.msg);
}
function renderSkills(){
  loadProposals();
  document.getElementById('skills').innerHTML=SKILLS.map(s=>
    `<div class="card blueprint">${CORNERS}`+
    `<div style="display:flex;align-items:center;gap:8px">`+
    `<span class=card-title style="font-size:15px;font-family:var(--font-mono);font-weight:500">${escT(s.name)}</span>`+
    `<span style="margin-left:auto;display:flex;gap:2px">`+
    `<button class="btn btn-icon btn-ghost" style="width:26px;height:26px" title=Edit onclick="editSkill('${esc(s.name)}')">${I_EDIT}</button>`+
    `<button class="btn btn-icon btn-ghost" style="width:26px;height:26px;color:var(--color-neutral-600)" title=Delete onclick="delSkill('${esc(s.name)}')">${I_DEL}</button>`+
    `</span></div>`+
    `<p class=card-body style="font-size:12.5px">${escT(s.description||'')}</p></div>`)
    .join('')||'<span class=text-muted style="font-size:13px">none</span>';
}
async function editSkill(n){const s=SKILLS.find(x=>x.name===n);if(!s)return;
  document.getElementById('skname').value=s.name;document.getElementById('skdesc').value=s.description||'';
  const c=document.getElementById('skcontent');c.value='… lade';
  // The page carries only name+description (the catalog is ~1 MB); the body
  // comes on demand.
  try{c.value=await (await fetch('/api/skills/'+encodeURIComponent(n))).text()}catch(e){c.value=''}
  document.getElementById('skname').scrollIntoView({behavior:'smooth'});}
function saveSkill(){
  const name=document.getElementById('skname').value.trim(),description=document.getElementById('skdesc').value,
        content=document.getElementById('skcontent').value;
  if(!name)return alert('Name?');
  fetch('/api/skills',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,description,content})})
    .then(()=>location.reload());
}
async function delSkill(n){if(confirm('Delete skill '+n+'?')){await fetch('/api/skills/'+encodeURIComponent(n)+'/delete',{method:'POST'});location.reload()}}

function renderSettings(){
  // Key fields are masked via CSS (-webkit-text-security) instead of
  // type=password: a real password field makes Chrome's password manager
  // offer "Save password?" on navigating away — with the katfs node-id as a
  // supposed username. Chrome ignores autocomplete=off there.
  document.getElementById('settings').innerHTML=SETTINGS_SCHEMA.map(s=>{
    if(s.options)return `<div class=field><label>${escT(s.label)}</label><select class=input data-s="${esc(s.key)}">`+
      s.options.map(o=>`<option value="${esc(o.value)}"${(SETTINGS[s.key]||'')===o.value?' selected':''}>${escT(o.label)}</option>`).join('')+`</select></div>`;
    return `<div class=field><label>${escT(s.label)}</label><input class="input${s.key.indexOf('KEY')>=0?' seckey':''}" data-s="${esc(s.key)}" value="${esc(SETTINGS[s.key])}" `+
    `type=text autocomplete=off spellcheck=false></div>`;}).join('');
  voiceHealth();
}
async function voiceHealth(){
  const el=document.getElementById('voicestat'); if(!el)return;
  try{
    const d=await (await fetch('/api/voice-health')).json();
    el.innerHTML=`<span class="tag ${d.ready?'tag-accent':'tag-neutral'}">${d.ready?'● up':'○ loading'}</span> `+
      `STT <code>${escT(d.asr||'?')}</code> · TTS-Stimmen: ${(d.voices||[]).map(v=>'<code>'+escT(v)+'</code>').join(' ')}`;
  }catch(e){el.innerHTML='<span class="tag tag-neutral">○ voice service not reachable</span>';}
}
function saveSettings(){
  const d={};document.querySelectorAll('#settings input,#settings select').forEach(i=>d[i.dataset.s]=i.value);
  fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)})
    .then(()=>{Object.assign(SETTINGS,d);document.getElementById('setmsg').textContent='saved ✓';renderParams()});
}
function fieldFor(p){
  const v=p.default||SETTINGS[p.key]||'';
  if(p.type==='select'&&p.options)
    // An entry is either a string or {value,label} — so an option can carry
    // a readable label (e.g. "— account default —").
    return `<select class=input data-k="${p.key}" ${p.key==='TRANSPORT'?'onchange=applyTransport()':''}>`+p.options.map(o=>{
      const val=(o&&typeof o==='object')?(o.value||''):o, lbl=(o&&typeof o==='object')?(o.label||o.value||''):o;
      return `<option value="${esc(val)}"${val===v?' selected':''}>${escT(lbl)}</option>`;
    }).join('')+`</select>`;
  if(p.type==='select'&&p.source==='openrouter')
    return `<div style="display:flex;gap:6px">${orSelect(p.key,v,p.tools,p.relevant)}`
      +`<button type=button class="btn btn-secondary btn-icon" title="Refresh list" onclick="orRefresh(this)">⟳</button></div>`;
  return `<input class=input data-k="${p.key}" value="${esc(v)}">`;
}
/* — model pickers: a dropdown off the OpenRouter catalog, with "other model
     id…" as the way back to free text (direct providers are not in the list) — */
const OR_CUSTOM='__custom__';
function orSelect(key,v,tools,relevant){
  return `<select class=input style="flex:1;min-width:0" data-k="${esc(key)}" data-or=1 `
    +`${tools?'data-tools=1':''} ${relevant?'data-relevant=1':''} onchange="orPick(this)">`
    +`<option value="${esc(v)}">${esc(v)||'— loading… —'}</option></select>`;
}
function loadModels(sel,force){
  const cur=sel.value; sel.disabled=true;
  let q='?'; if(force)q+='refresh=1&'; if(sel.dataset.tools)q+='tools=1&'; if(sel.dataset.relevant)q+='relevant=1';
  fetch('/api/openrouter-models'+q).then(r=>r.json()).then(ms=>{
    // The fetch (upstream openrouter.ai) can take seconds. In that time the
    // user may long since have picked something ELSE — or the select was
    // replaced by a template switch. So: read the value NOW (not the one from
    // the fetch start) and never touch detached selects again.
    // Otherwise the dropdown seemingly jumps back to the initial value for no reason.
    if(!sel.isConnected){return}
    const now=sel.value||cur;
    sel.innerHTML=(ms.length?ms:[{id:now,label:now+' (list n/a)'}]).map(m=>
      `<option value="${m.id}"${m.id===now?' selected':''}>${m.label}</option>`).join('')
      +`<option value="${OR_CUSTOM}">— other model id… —</option>`;
    // A value outside the shortlist must not silently flip to the first entry
    // — it stays as its own option.
    if(now&&!ms.some(m=>m.id===now))
      sel.insertAdjacentHTML('afterbegin',`<option value="${esc(now)}" selected>${escT(now)} (not in the shortlist)</option>`);
    sel.disabled=false;
  }).catch(()=>{sel.disabled=false});
}
function orPick(sel){
  if(sel.value!==OR_CUSTOM)return;
  const box=sel.parentNode;
  box.dataset.tools=sel.dataset.tools||''; box.dataset.relevant=sel.dataset.relevant||'';
  sel.outerHTML=`<input class=input style="flex:1;min-width:0" data-k="${esc(sel.dataset.k)}" placeholder="provider/model-id">`;
  box.querySelector('button').title='Back to the list';
  box.querySelector('input[data-k]').focus();
}
function orRefresh(btn){
  const box=btn.parentNode, sel=box.querySelector('select[data-or]');
  if(sel)return loadModels(sel,1);
  const inp=box.querySelector('input[data-k]');
  if(!inp)return;
  inp.outerHTML=orSelect(inp.dataset.k,inp.value.trim(),box.dataset.tools,box.dataset.relevant);
  btn.title='Refresh list';
  loadModels(box.querySelector('select[data-or]'),0);
}
function renderParams(){
  const t=document.getElementById('tpl').value;
  const tpl=TEMPLATES.find(x=>x.template===t)||{params:[]};
  document.getElementById('params').innerHTML=(tpl.params||[]).map(p=>
    `<div class=field data-pk="${p.key}"><label>${escT(p.label||p.key)}</label>${fieldFor(p)}</div>`).join('');
  document.querySelectorAll('#params select[data-or]').forEach(s=>loadModels(s,0));
  applyTransport();
}
function applyTransport(){
  const sel=document.querySelector('#params [data-k="TRANSPORT"]');
  const web=sel&&sel.value==='web';
  ['SIGNAL_NUMBER','ALLOWED_SENDERS'].forEach(k=>{
    const el=document.querySelector('#params [data-pk="'+k+'"]');
    if(el)el.style.display=web?'none':'';
  });
}
function mountRow(m){m=m||{};return `<div class=mrow>`+
  `<input class="input mh" placeholder="/host/path" value="${esc(m.host)}" style="flex:2;min-width:150px;width:auto">`+
  `<button type=button class="btn btn-secondary btn-icon" style="width:32px;height:32px" title="Browse the host…" onclick="pkRow(this)">📁</button>`+
  `<input class="input mg" placeholder="/mnt/name" value="${esc(m.guest)}" style="flex:2;min-width:120px;width:auto">`+
  `<label class=radio><input type=checkbox class=mr ${m.readonly?'checked':''}><span class=dot></span>ro</label>`+
  `<button type=button class="btn btn-secondary btn-icon" style="width:32px;height:32px" onclick="this.parentNode.remove()">✕</button></div>`}
function addMount(m,target){document.getElementById(target||'mounts').insertAdjacentHTML('beforeend',mountRow(m))}

/* — folder picker: browses the host through /api/browse (directories only) — */
let PK={cb:null};
const PK_QUICK=['__HOME__','/mnt','/srv','/media','/opt','/'];
function pkOpen(start,cb){
  PK.cb=cb;
  document.getElementById('pkquick').innerHTML=PK_QUICK.map(p=>
    `<button type=button class="btn btn-secondary btn-sm" data-p="${esc(p)}">${escT(p)}</button>`).join('');
  document.getElementById('picker').style.display='grid';
  pkGo(start||'/home');
}
function pkClose(){document.getElementById('picker').style.display='none';PK.cb=null}
async function pkGo(p){
  let d;
  try{d=await (await fetch('/api/browse?path='+encodeURIComponent(p||'/'))).json()}
  catch(e){d={path:p,parent:'',dirs:[],error:'not reachable'}}
  document.getElementById('pkpath').value=d.path||p;
  document.getElementById('pkerr').textContent=d.error||'';
  const up=d.parent?`<button type=button class="btn pkrow" data-p="${esc(d.parent)}">↑ ..</button>`:'';
  const base=(d.path==='/'?'':d.path);
  const rows=(d.dirs||[]).map(n=>
    `<button type=button class="btn pkrow" data-p="${esc(base+'/'+n)}">📁 ${escT(n)}</button>`).join('');
  document.getElementById('pklist').innerHTML=up+rows||
    '<span class=text-muted style="font-size:13px;padding:6px">no sub-folders</span>';
}
function pkChoose(){
  const p=document.getElementById('pkpath').value.trim(),cb=PK.cb;
  pkClose(); if(cb&&p)cb(p);
}
function pkRow(btn){
  const row=btn.closest('.mrow'),h=row.querySelector('.mh'),g=row.querySelector('.mg');
  pkOpen(h.value||'/home',p=>{
    h.value=p;
    if(!g.value){const b=p.split('/').filter(Boolean).pop();if(b)g.value='/mnt/'+b}
  });
}
function collectMounts(scope){return [...scope.querySelectorAll('.mrow')].map(r=>({
  host:r.querySelector('.mh').value.trim(),guest:r.querySelector('.mg').value.trim(),
  readonly:r.querySelector('.mr').checked})).filter(m=>m.host&&m.guest)}
function create(){
  const t=document.getElementById('tpl').value,n=document.getElementById('nm').value;
  if(!n)return alert('Name?');
  const cfg={};document.querySelectorAll('#params [data-k]').forEach(i=>cfg[i.dataset.k]=i.value);
  const pn=document.getElementById('persona').value;
  if(pn){const p=PERSONAS.find(x=>x.name===pn);if(p)cfg.AGENT_SYSTEM=p.prompt;}
  const ks=document.getElementById('katfsshare').value;
  if(ks)cfg.KATFS_SHARE=ks;
  const mcps=[...document.querySelectorAll('#mcp-pick input[type=checkbox]:checked')].map(c=>c.value);
  const mounts=collectMounts(document.getElementById('mounts'));
  const internet=document.getElementById('cap-net').checked;
  const tools=[...document.querySelectorAll('.toolcb:checked')].map(c=>c.value);
  fetch('/api/create',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:n,template:t,config:cfg,mcps,mounts,internet,tools})}).then(()=>location.reload());
}
let MCPS=[];
async function renderMcps(){
  try{MCPS=await (await fetch('/api/mcps')).json();}catch(e){return;}
  const box=document.getElementById('mcps');
  if(box)box.innerHTML=MCPS.map(m=>
    `<div class="card blueprint">${CORNERS}`+
    `<div style="display:flex;align-items:center;gap:10px">`+
    `<span class=card-title style="font-size:16px">${escT(m.name)}</span>`+
    `<span style="margin-left:auto;display:flex;gap:4px">`+
    `<button class="btn btn-ghost" style="font-size:12px" onclick="editMcp('${esc(m.name)}')">Edit</button>`+
    `<button class="btn btn-ghost" style="font-size:12px;color:var(--color-neutral-600)" onclick="delMcp('${esc(m.name)}')">Delete</button>`+
    `</span></div>`+
    `<p class=card-body>${escT(m.description||'')}</p>`+
    `<div class=cmd>${escT(m.command||'')} ${escT((m.args||[]).join(' '))}</div></div>`)
    .join('')||'<span class=text-muted style="font-size:13px">none</span>';
  const pick=document.getElementById('mcp-pick');
  if(pick)pick.innerHTML=MCPS.map(m=>
    `<label class=radio><input type=checkbox value="${esc(m.name)}"><span class=dot></span>${escT(m.name)}</label>`)
    .join('')||'<span class=text-muted style="font-size:12px">no MCPs in catalog</span>';
}
function editMcp(n){const m=MCPS.find(x=>x.name===n);if(!m)return;
  document.getElementById('mcpname').value=m.name;document.getElementById('mcpdesc').value=m.description||'';
  document.getElementById('mcpcmd').value=m.command||'';document.getElementById('mcpargs').value=(m.args||[]).join('\\n');
  document.getElementById('mcpenv').value=Object.entries(m.env||{}).map(e=>e[0]+'='+e[1]).join('\\n');
  document.getElementById('mcpname').scrollIntoView({behavior:'smooth'});}
function saveMcp(){
  const name=document.getElementById('mcpname').value.trim(),description=document.getElementById('mcpdesc').value,
        command=document.getElementById('mcpcmd').value.trim(),
        args=document.getElementById('mcpargs').value.split('\\n').map(s=>s.replace(/\\r$/,'')).filter(s=>s.length);
  const env={};
  document.getElementById('mcpenv').value.split('\\n').forEach(l=>{l=l.replace(/\\r$/,'');const i=l.indexOf('=');if(i>0)env[l.slice(0,i).trim()]=l.slice(i+1);});
  if(!name||!command)return alert('Name + Command?');
  fetch('/api/mcps',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,description,command,args,env})})
    .then(()=>location.reload());
}
async function delMcp(n){if(confirm('Delete MCP '+n+'?')){await fetch('/api/mcps/'+encodeURIComponent(n)+'/delete',{method:'POST'});location.reload()}}
let MDLG='';
async function editMounts(name){
  const list=await (await fetch('/api/instances')).json();
  const inst=list.find(i=>i.name===name)||{};
  MDLG=name;
  document.getElementById('mdlgname').textContent=name;
  document.getElementById('mdlgrows').innerHTML='';
  (inst.mounts||[]).forEach(m=>addMount(m,'mdlgrows'));
  document.getElementById('mdlg').style.display='grid';
}
function mdlgClose(){document.getElementById('mdlg').style.display='none'}
async function saveMounts(){
  const mounts=collectMounts(document.getElementById('mdlgrows'));
  const r=await fetch(`/api/instances/${MDLG}/mounts`,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({mounts})});
  const d=await r.json(); mdlgClose();
  alert(d.msg||'ok');location.reload();
}

/* — Model switch for existing instances: the same picker as on create
     (fieldFor renders the OpenRouter list or the curated options per template),
     prefilled with the instance's current model. */
let MODELDLG='';
async function editModel(name){
  const list=await (await fetch('/api/instances')).json();
  const inst=list.find(i=>i.name===name)||{};
  const cfg=inst.config||{};
  const key=['OPENROUTER_MODEL','ORCAROUTER_MODEL','ANTHROPIC_MODEL','PI_MODEL','PRIME_MODEL','LLAMA_MODEL'].find(k=>k in cfg);
  if(!key)return alert('This instance has no model setting.');
  const tpl=TEMPLATES.find(t=>t.template===inst.template)||{params:[]};
  const p=(tpl.params||[]).find(x=>x.key===key)||{key:key};
  MODELDLG=name;
  document.getElementById('modeldlgname').textContent=name;
  // fieldFor takes p.default as the preset — a copy with the current model.
  document.getElementById('modeldlgbox').innerHTML=
    fieldFor(Object.assign({},p,{default:cfg[key]||''}));
  document.getElementById('modeldlg').style.display='grid';
  const sel=document.querySelector('#modeldlgbox select[data-or]');
  if(sel)loadModels(sel,0);
}
function modelDlgClose(){document.getElementById('modeldlg').style.display='none'}
async function saveModel(){
  const el=document.querySelector('#modeldlgbox [data-k]');
  const model=(el?el.value:'').trim();
  if(!model||model===OR_CUSTOM)return alert('Model id?');
  const r=await fetch(`/api/instances/${MODELDLG}/model`,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({model})});
  const d=await r.json(); modelDlgClose();
  alert(d.msg||'ok');location.reload();
}

/* — Changelog + security: findings live in security.json, the UI only toggles
     the status. Markdown is rendered deliberately minimally (headings, lists,
     bold, code) — after escaping, so nothing can break out of the text. */
let ISSUES=[];
const SEVORDER={high:0,medium:1,low:2};
async function loadChangelog(){
  try{
    const [c,i]=await Promise.all([
      (await fetch('/api/changelog')).json(),
      (await fetch('/api/security')).json()]);
    document.getElementById('changelog').innerHTML=
      ''+md(c.text||'');
    ISSUES=(i.issues||[]).slice().sort((a,b)=>
      (a.status===b.status?0:a.status==='open'?-1:1)||
      (SEVORDER[a.severity]??9)-(SEVORDER[b.severity]??9));
  }catch(e){document.getElementById('issues').textContent='not reachable';return}
  renderIssues();
}
function renderIssues(){
  const showDone=document.getElementById('showdone').checked;
  const rows=ISSUES.filter(i=>showDone||i.status!=='done');
  const open=ISSUES.filter(i=>i.status!=='done').length;
  document.getElementById('issues').innerHTML=rows.map(i=>
    `<div class="issue ${i.status==='done'?'done':''}">`+
    `<span class="sev sev-${esc(i.severity)}">${escT(i.severity)}</span>`+
    `<div><h5>${escT(i.title)}</h5>`+
    `<div class="meta text-muted">${escT(i.where||'')} · ${escT(i.kind||'')}</div>`+
    `<p>${escT(i.detail||'')}</p>`+
    (i.fix?`<p class=text-muted><b>Fix:</b> ${escT(i.fix)}</p>`:'')+`</div>`+
    `<label class=radio><input type=checkbox data-i="${esc(i.id)}" ${i.status==='done'?'checked':''}>`+
    `<span class=dot></span>done</label></div>`).join('')||
    '<span class=text-muted style="font-size:13px">nothing open</span>';
  document.getElementById('secissuemsg').textContent=open+' open';
}
function saveIssues(){
  document.querySelectorAll('#issues input[data-i]').forEach(c=>{
    const it=ISSUES.find(x=>x.id===c.dataset.i);
    if(it)it.status=c.checked?'done':'open';
  });
  fetch('/api/security',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({issues:ISSUES.map(i=>({id:i.id,status:i.status}))})})
    .then(r=>r.json()).then(d=>{document.getElementById('secissuemsg').textContent=(d.msg||'saved')+' ✓';
      loadChangelog();});
}
function md(src){
  const esc1=escT(src);
  const out=[];
  let inList=false;
  for(const raw of esc1.split('\\n')){
    const line=raw.replace(/`([^`]+)`/g,'<code>$1</code>')
                  .replace(/\\*\\*([^*]+)\\*\\*/g,'<b>$1</b>');
    const li=line.match(/^\\s*[-*] (.*)$/);
    if(li){ if(!inList){out.push('<ul>');inList=true} out.push('<li>'+li[1]+'</li>'); continue }
    if(inList){out.push('</ul>');inList=false}
    if(/^### /.test(line)) out.push('<h3>'+line.slice(4)+'</h3>');
    else if(/^## /.test(line)) out.push('<h2>'+line.slice(3)+'</h2>');
    else if(/^# /.test(line)) out.push('');            // title is already in the tab
    else if(line.trim()) out.push('<p>'+line+'</p>');
  }
  if(inList)out.push('</ul>');
  return out.join('');
}

/* — Models: the full catalog live, and from it the shortlist for the create form.
     The ticks live in CURATED (models.json), no longer in the source. — */
let CATALOG=[], PICKED=new Set();
async function loadModels2(force){
  const msg=document.getElementById('mdlmsg');
  msg.textContent=force?'fetching catalog…':'';
  try{
    const [cat,cur]=await Promise.all([
      (await fetch('/api/openrouter-models'+(force?'?refresh=1':''))).json(),
      (await fetch('/api/models')).json()]);
    CATALOG=cat; PICKED=new Set(cur.curated||[]);
  }catch(e){msg.textContent='catalog not reachable';return}
  msg.textContent='';
  renderModels();
}
function renderModels(){
  const q=document.getElementById('mdlq').value.trim().toLowerCase();
  const only=document.getElementById('mdltools').checked;
  const selOnly=document.getElementById('mdlsel').checked;
  const rows=CATALOG.filter(m=>
    (!only||m.tools)&&(!selOnly||PICKED.has(m.id))&&
    (!q||m.id.toLowerCase().includes(q)||(m.name||'').toLowerCase().includes(q)));
  document.getElementById('mdlcount').textContent=
    PICKED.size+' selected · '+rows.length+' of '+CATALOG.length+' shown';
  document.getElementById('mdlrows').innerHTML=rows.map(m=>
    `<tr><td style="width:34px;text-align:center"><input type=checkbox data-m="${esc(m.id)}" ${PICKED.has(m.id)?'checked':''}></td>`+
    `<td><div class=mono style="font-size:13px">${escT(m.id)}</div>`+
    `<div class=text-muted style="font-size:11.5px">${escT(m.name||'')}</div></td>`+
    `<td style="white-space:nowrap;font-variant-numeric:tabular-nums">${escT(m.price||'')}</td>`+
    `<td style="white-space:nowrap;font-variant-numeric:tabular-nums" class=text-muted>${m.ctx?(m.ctx/1000).toFixed(0)+'k':''}</td>`+
    `<td>${m.tools?'<span class="tag tag-accent">tools</span>':'<span class="tag tag-neutral">no tools</span>'}</td></tr>`)
    .join('')||'<tr><td class=text-muted style="padding:14px">nothing matches</td></tr>';
}
function saveModels(){
  fetch('/api/models',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({curated:[...PICKED]})})
    .then(r=>r.json()).then(d=>{
      document.getElementById('mdlmsg').textContent=(d.msg||'saved')+' ✓';
      renderParams();   // refresh the model dropdown in the create form immediately
    });
}

/* — iroh app transport: the manager's gateway node-id + the phone allowlist — */
async function loadIroh(){
  let d={node_id:'',allow:[]};
  try{d=await (await fetch('/api/iroh')).json()}catch(e){}
  const nid=document.getElementById('irohnid'); if(nid){
    nid.value=d.node_id||'';
    document.getElementById('irohhint').textContent=d.node_id
      ? 'Live — the app dials this node-id over iroh (relayed, NAT-traversed, end-to-end encrypted).'
      : 'Gateway not running. Start it: sudo systemctl enable --now iroh-gw';
  }
  const box=document.getElementById('irohallow'); if(box){
    box.innerHTML=(d.allow&&d.allow.length)? d.allow.map(a=>
      `<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--color-border,#0002)">`+
      `<span style="flex:1;min-width:0"><b>${escT(a.label||'(no label)')}</b> `+
      `<span class="mono text-muted" style="font-size:12px">${escT(a.id.slice(0,16))}…</span></span>`+
      `<button class="btn btn-ghost btn-sm" onclick="irohRemove('${escT(a.id)}')">Remove</button></div>`).join('')
      : '<span class=text-muted style="font-size:13px">No phones paired yet.</span>';
  }
}
function irohCopy(){const v=document.getElementById('irohnid').value;if(v)navigator.clipboard.writeText(v).then(()=>{document.getElementById('irohhint').textContent='Copied.';});}
async function irohAdd(){
  const id=document.getElementById('irohaddid').value.trim();
  const label=document.getElementById('irohaddlabel').value.trim();
  const d=await (await fetch('/api/iroh',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'add',id,label})})).json();
  document.getElementById('irohmsg').textContent=d.ok?('✓ '+d.msg):('⚠️ '+d.msg);
  if(d.ok){document.getElementById('irohaddid').value='';document.getElementById('irohaddlabel').value='';}
  loadIroh();
}
async function irohRemove(id){
  await fetch('/api/iroh',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'remove',id})}).catch(()=>{});
  loadIroh();
}

/* — katfs: status of the host node + the browser share it is holding — */
let KATFS_ID='';
async function loadKatfs(){
  const el=document.getElementById('katfs-status');
  let d;
  try{d=await (await fetch('/api/katfs/status')).json()}
  catch(e){d={up:false,error:'manager unreachable'}}
  // File browser: keep the last selected share as long as it is still
  // connected; otherwise the first. That way the status rows are highlighted
  // correctly right away. Clicking a row switches later (fbPick).
  const ids=(d.shares||[]).map(x=>x.id);
  if(!FB.share || !ids.includes(FB.share)) FB.share = ids[0] || '';
  const tag=(on,yes,no)=>`<span class="tag ${on?'tag-accent':'tag-neutral'}">${on?yes:no}</span>`;
  el.innerHTML=
    `<div class=kv><b>Host node</b>${tag(d.up,'● up on :'+(d.port||8790),'○ down')}`+
    (d.up?'':`<span class=text-muted style="font-size:12px">${escT(d.error||'')}</span>`)+`</div>`+
    `<div class=kv><b>Browser shares</b>${tag(d.connected,'● '+(d.count||1)+' active','○ nobody sharing')}</div>`+
    (d.shares||[]).map(s=>
      `<div class="kv fbsharerow${FB.share===s.id?' sel':''}" onclick="fbPick('${esc(s.id)}')" title="Browse this share"><b>&nbsp;</b><span><span class=mono>${escT(s.id)}</span> · `+
      `<b style="font-family:var(--font-body);font-size:13.5px;text-transform:none;letter-spacing:0;min-width:0">${escT(s.name||'?')}</b>`+
      (s.device?` <span class=text-muted>${escT(s.device)}</span>`:'')+
      (s.readonly?' <span class="tag tag-neutral">read-only</span>':'')+
      (FB.share===s.id?' <span class="tag tag-accent">browsing &#9662;</span>':'')+`</span></div>`).join('');
  KATFS_SHARES=d.shares||[]; window.KATFS_SHARES=KATFS_SHARES;
  renderShareOptions();
  KATFS_ID=d.node_id||'';
  const inp=document.getElementById('katfskey');
  if(!inp.value||inp.dataset.auto==='1'){inp.value=KATFS_ID;inp.dataset.auto='1'}
  keyHint();
  document.getElementById('katfs-new').innerHTML= d.connected
    ? '<span class="tag tag-accent">● '+(d.count||1)+' share'+((d.count||1)>1?'s':'')+' active</span> reachable via <code>remote_ls</code> / <code>remote_read</code> / <code>remote_write</code>.'
    : (d.up ? '<span class="tag tag-neutral">○ nobody sharing</span> node is up; open <a href="#sharing">Sharing</a> to hand it a folder.'
            : '<span class="tag tag-neutral">○ node down</span> the katfs node on this host is not answering.');
  fbGo(d.connected ? FB.path : '.');
  if(!d.connected){ document.getElementById('fblist').innerHTML=
    '<span class=text-muted style="font-size:13px">No folder shared right now — click “Share a folder…” above, pick a folder in the new tab, then Refresh.</span>'; }
}

/* ── katfs file browser ────────────────────────────────────────────────── */
const FB={path:'.',share:''};
function fbSize(n){n=+n||0;return n<1024?n+' B':n<1048576?(n/1024).toFixed(1)+' KB':n<1073741824?(n/1048576).toFixed(1)+' MB':(n/1073741824).toFixed(1)+' GB';}
function fbQ(p){const q='path='+encodeURIComponent(p);return FB.share?q+'&share='+encodeURIComponent(FB.share):q;}
function fbUp(){if(FB.path==='.'||FB.path==='')return;const i=FB.path.lastIndexOf('/');fbGo(i<0?'.':FB.path.slice(0,i));}
/* Click a share in the status panel -> open it in the browser. */
function fbPick(id){ FB.share=id; FB.path='.'; renderShareSel(); fbGo('.'); }
/* Highlight the active share in the status panel without reloading everything. */
function renderShareSel(){
  document.querySelectorAll('.fbsharerow').forEach(r=>{
    const on=r.getAttribute('onclick')===("fbPick('"+FB.share+"')");
    r.classList.toggle('sel',on);
  });
}
/* Download the current folder of the selected share as a ZIP. */
function fbZip(){ window.location.href='/api/katfs/zip?'+fbQ(FB.path); }
async function fbGo(p){
  FB.path=p||'.';
  document.getElementById('fbpath').textContent='/'+(FB.path==='.'?'':FB.path);
  document.getElementById('fbup').disabled=(FB.path==='.'||FB.path==='');
  const sh=(window.KATFS_SHARES||[]).find(x=>x.id===FB.share);
  const shl=document.getElementById('fbshare');
  if(shl) shl.textContent = FB.share ? ('· '+((sh&&sh.name)||FB.share)) : '· (single share)';
  const dl=document.getElementById('fbdl'); if(dl) dl.disabled=false;
  const el=document.getElementById('fblist');
  el.innerHTML='<span class=text-muted style="font-size:13px">loading…</span>';
  let d;
  try{d=await (await fetch('/api/katfs/browse?'+fbQ(FB.path))).json();}
  catch(e){el.innerHTML='<span class=text-muted style="font-size:13px">not reachable</span>';return;}
  if(d.error){el.innerHTML='<span class=text-muted style="font-size:13px">'+escT(d.error)+'</span>';return;}
  const ents=(d.entries||[]).slice().sort((a,b)=>((b.dir?1:0)-(a.dir?1:0))||String(a.name).localeCompare(b.name));
  if(!ents.length){el.innerHTML='<span class=text-muted style="font-size:13px">(empty folder)</span>';return;}
  el.innerHTML=ents.map(e=>{
    const child=(FB.path==='.'||FB.path===''?'':FB.path+'/')+e.name;
    if(e.dir) return `<div class="fbrow dir" onclick="fbGo('${esc(child)}')"><span class=fbico>📁</span><span class=fbn>${escT(e.name)}</span><span class=fbsz></span></div>`;
    return `<div class=fbrow><span class=fbico>📄</span><span class=fbn>${escT(e.name)}</span>`+
      `<span class=fbsz>${fbSize(e.size)}</span>`+
      `<a class="btn btn-ghost fbact" target=_blank rel=noopener href="/api/katfs/file?${fbQ(child)}">view</a>`+
      `<a class="btn btn-ghost fbact" href="/api/katfs/file?dl=1&${fbQ(child)}">download</a></div>`;
  }).join('');
}
/* The key is the node's node-id; iroh parses it as an EndpointId — the WASM
   bridge does not (yet) accept a ticket, hence the hard hint. */
function keyHint(){
  const inp=document.getElementById('katfskey'),v=inp.value.trim(),h=document.getElementById('keyhint');
  if(inp.value!==KATFS_ID)inp.dataset.auto='0';
  if(!v){h.textContent='Empty — the share page will use whatever the node injects.';return}
  if(!/^[0-9a-fA-F]{64}$/.test(v)){
    h.textContent='Not a node-id: iroh expects 64 hex characters (a ticket does not parse).';return;
  }
  h.textContent=(v.toLowerCase()===KATFS_ID.toLowerCase())
    ? 'This host — agents on this machine reach the share.'
    : 'Foreign node — the folder is served to that node, not to this host.';
}
/* Share selection in the create form. The value is the share-id reported by
   the sharing browser (stable across reload) — not the node-id. */
let KATFS_SHARES=[];
function renderShareOptions(){
  const sel=document.getElementById('katfsshare'),cur=sel.value;
  sel.innerHTML='<option value="">— none / decide at runtime —</option>'+
    KATFS_SHARES.map(s=>{
      const lbl=(s.name||s.id)+(s.device?' · '+s.device:'')+(s.readonly?' · read-only':'')+' — '+s.id;
      return `<option value="${esc(s.id)}"${s.id===cur?' selected':''}>${escT(lbl)}</option>`;
    }).join('');
  katfsNewHint();
}
function katfsNewHint(){
  const v=document.getElementById('katfsshare').value,h=document.getElementById('katfsurlhint');
  if(v){
    const s=KATFS_SHARES.find(x=>x.id===v)||{};
    h.innerHTML='Pinned to <span class=mono>'+escT(v)+'</span> ('+escT(s.name||'?')+') as <span class=mono>KATFS_SHARE</span>'+
      ' — survives a reload of that browser tab, but not clearing its storage.';
    return;
  }
  h.textContent=KATFS_SHARES.length>1
    ? 'With '+KATFS_SHARES.length+' shares active the node cannot guess — the agent must name one, so pick a share here.'
    : 'Fine while at most one share is active: the agent just takes the only one.';
}
function openShare(){
  const v=document.getElementById('katfskey').value.trim();
  window.open(v&&v!==KATFS_ID?'/katfs/?key='+encodeURIComponent(v):'/katfs/','_blank','noopener');
}
function resetKey(){
  const inp=document.getElementById('katfskey');
  inp.value=KATFS_ID;inp.dataset.auto='1';keyHint();
}
function copyKey(){
  const inp=document.getElementById('katfskey'),h=document.getElementById('keyhint');
  const done=()=>{h.textContent='copied ✓'};
  if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.writeText(inp.value).then(done,()=>{inp.select()});
  else{inp.select();try{document.execCommand('copy');done()}catch(e){h.textContent='select + copy manually'}}
}
async function act(n,a){await fetch(`/api/instances/${n}/${a}`,{method:'POST'});location.reload()}
async function togglePersist(n,on){
  const r=await fetch(`/api/instances/${n}/persist`,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({on})});
  const d=await r.json(); if(String(d.msg||'').startsWith('error'))alert(d.msg);
  location.reload();
}
function diskReset(n){
  if(confirm('Delete the persistent write layer of '+n+'? (installed packages gone, base image stays)'))
    fetch(`/api/instances/${n}/diskreset`,{method:'POST'}).then(r=>r.json()).then(d=>alert(d.msg||'ok'));
  return false;
}
async function toggleNet(n,on){
  await fetch(`/api/instances/${n}/internet`,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({on})});location.reload();
}
async function del(n){if(confirm('Delete instance '+n+'?')){await fetch(`/api/instances/${n}/delete`,{method:'POST'});location.reload()}}

let SECPOL={by_template:{},by_instance:{}};
async function renderSecrets(){
  const el=document.getElementById('secrets');
  let keys=[];
  try{
    const sk=await (await fetch('/api/secret-keys')).json(); keys=sk.keys||[]; SECSRC=sk.sources||{};
    SECPOL=await (await fetch('/api/secret-policy')).json();
  }catch(e){el.innerHTML='<span class=text-muted style="font-size:13px">not available</span>';return;}
  if(!keys.length){el.innerHTML='<span class=text-muted style="font-size:13px">No secret keys yet.</span>'+
    `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:12px"><input class="input mono" id=sk-name placeholder="NEW_KEY" style="width:220px;font-size:12.5px" autocomplete=off spellcheck=false>`+
    `<input class="input seckey" id=sk-val placeholder="value" style="width:300px" type=text autocomplete=off spellcheck=false><button class="btn btn-secondary btn-sm" onclick="secAdd()">Add</button></div>`;return;}
  const tpls=(TEMPLATES||[]).map(t=>t.template), bt=SECPOL.by_template||{}, gr=SECPOL.guest_readable||[];
  const head=`<tr><th>Secret key</th>`+tpls.map(t=>`<th style="text-align:center">${escT(t)}</th>`).join('')+`<th style="text-align:center" title="Only a key ticked here ever leaves the host as a raw value (get_secret). A release per template/instance alone lets the MCP hub substitute it on the host.">raw to guest</th></tr>`;
  const src=SECSRC||{};
  // Value column: the key is set (values never come to the browser); store
  // keys can be replaced or deleted here, settings keys point to their tab.
  const val=k=>src[k]==='settings'
    ?`<span class=text-muted style="font-size:11.5px">set · <a href="#settings">Settings</a></span>`
    :`<span class=text-muted style="font-size:11.5px">set</span> <button class="btn btn-ghost" style="font-size:11.5px;padding:2px 6px" title="Replace the value" onclick="secReplace('${esc(k)}')">replace</button>`+
     `<button class="btn btn-ghost" style="font-size:11.5px;padding:2px 6px;color:var(--color-neutral-600)" title="Delete from the store" onclick="secDelete('${esc(k)}')">delete</button>`;
  const body=keys.map(k=>`<tr><td data-label="Secret key" class=mono style="font-size:12.5px">${escT(k)}</td>`+
    `<td data-label="Value" style="white-space:nowrap">${val(k)}</td>`+
    tpls.map(t=>`<td data-label="${esc(t)}" style="text-align:center"><input type=checkbox data-tpl="${esc(t)}" value="${esc(k)}" ${(bt[t]||[]).indexOf(k)>=0?'checked':''}></td>`).join('')+
    `<td data-label="raw to guest" style="text-align:center"><input type=checkbox data-gr="1" value="${esc(k)}" ${gr.indexOf(k)>=0?'checked':''}></td></tr>`).join('');
  el.innerHTML=`<table class=table><thead>${head.replace('<th>Secret key</th>','<th>Secret key</th><th>Value</th>')}</thead><tbody>${body}</tbody></table>`+
    `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:12px">`+
    `<input class="input mono" id=sk-name placeholder="NEW_KEY" style="width:220px;font-size:12.5px" autocomplete=off spellcheck=false>`+
    `<input class="input seckey" id=sk-val placeholder="value" style="width:300px" type=text autocomplete=off spellcheck=false>`+
    `<button class="btn btn-secondary btn-sm" onclick="secAdd()">Add / replace</button>`+
    `<span class=text-muted style="font-size:11.5px">Written to the secret store on the host (0600), never shown again. LLM keys live in Settings.</span></div>`;
}
let SECSRC={};
async function secAdd(){
  const n=document.getElementById('sk-name').value.trim().toUpperCase(), v=document.getElementById('sk-val').value;
  if(!n||!v)return alert('Name and value?');
  const r=await (await fetch('/api/secret-store',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,value:v})})).json();
  document.getElementById('secmsg').textContent=r.msg||'';
  if(/added|replaced/.test(r.msg||'')){document.getElementById('sk-val').value='';renderSecrets();}
}
function secReplace(k){document.getElementById('sk-name').value=k;const v=document.getElementById('sk-val');v.value='';v.focus();}
async function secDelete(k){
  if(!confirm('Delete '+k+' from the secret store? Releases that use it stop working.'))return;
  const r=await (await fetch('/api/secret-store/'+encodeURIComponent(k)+'/delete',{method:'POST'})).json();
  document.getElementById('secmsg').textContent=r.msg||'';renderSecrets();
}
function saveSecrets(){
  const bt={};
  (TEMPLATES||[]).forEach(t=>{bt[t.template]=[];});
  const gr=[];
  document.querySelectorAll('#secrets input[type=checkbox]').forEach(c=>{
    if(c.dataset.gr){ if(c.checked)gr.push(c.value); return; }
    if(!bt[c.dataset.tpl])bt[c.dataset.tpl]=[];
    if(c.checked)bt[c.dataset.tpl].push(c.value);
  });
  fetch('/api/secret-policy',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({by_template:bt,by_instance:(SECPOL.by_instance||{}),guest_readable:gr})})
    .then(r=>r.json()).then(d=>{document.getElementById('secmsg').textContent=(d.msg||'saved')+' ✓';});
}
window.onload=()=>{
  showTab(location.hash.slice(1));
  renderSettings();renderParams();loadMissions();loadPrompts();loadPlaybooks();renderPersonas();renderSkills();renderSecrets();renderMcps();loadKatfs();loadIroh();loadModels2();loadChangelog();loadTools();loadPlugins();loadTasks();loadPolicy();loadResources();
  refreshUsage();
  // Tasks, policy and the usage numbers used to arrive only on page load —
  // whoever left the tab open saw arbitrarily stale state (and thought a
  // long-fixed problem was current). Refresh every 15s, but only for the
  // visible tab, not a background one.
  setInterval(()=>{
    if(document.hidden)return;
    const t=location.hash.slice(1)||'instances';
    if(t==='tasks'&&!TK_EDIT)loadTasks();
    else if(t==='missions')loadMissions();
    else if(t==='policy')loadPolicy(true);
    else if(t==='resources')loadResources();
    else if(t==='instances')refreshUsage();
  },15000);
  document.getElementById('actdlg').onclick=e=>{if(e.target.id==='actdlg')actClose()};
  // Remember ticks without rebuilding the whole table on every click.
  document.getElementById('mdlrows').onchange=e=>{
    const b=e.target.closest('[data-m]'); if(!b)return;
    b.checked?PICKED.add(b.dataset.m):PICKED.delete(b.dataset.m);
    document.getElementById('mdlcount').textContent=
      PICKED.size+' selected · '+document.querySelectorAll('#mdlrows tr').length+' shown';
  };
  // Folder picker: one handler for list + quick targets, so paths with
  // quotes/apostrophes don't have to go through inline onclick.
  ['pklist','pkquick'].forEach(id=>document.getElementById(id).onclick=e=>{
    const b=e.target.closest('[data-p]'); if(b)pkGo(b.dataset.p);
  });
  // A click on the backdrop or Esc closes the topmost dialog.
  document.getElementById('picker').onclick=e=>{if(e.target.id==='picker')pkClose()};
  document.getElementById('mdlg').onclick=e=>{if(e.target.id==='mdlg')mdlgClose()};
  document.addEventListener('keydown',e=>{
    if(e.key!=='Escape')return;
    if(document.getElementById('picker').style.display!=='none')pkClose();
    else mdlgClose();
  });
};

/* ── Benachrichtigungen (Glocke + Long-Poll + Browser-Notification) ─────── */
let NOTIF_REV=0, NOTIF_START=Math.floor(Date.now()/1000), NOTIF_SEEN=new Set(), NOTIF_LIST=[];
function nEsc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function notifBadge(u){const b=document.getElementById('nbadge');if(!b)return;if(u>0){b.textContent=u>99?'99+':u;b.hidden=false;}else b.hidden=true;}
function notifRender(list){
  if(list)NOTIF_LIST=list;
  const el=document.getElementById('nlist');if(!el)return;
  if(!NOTIF_LIST.length){el.innerHTML='<span class=text-muted style="font-size:13px;padding:12px;display:block">No notifications.</span>';return;}
  el.innerHTML=NOTIF_LIST.slice().reverse().map(n=>{
    const t=new Date((n.ts||0)*1000).toLocaleString();
    const lk=n.link?` data-link="${nEsc(n.link)}" style="cursor:pointer" title="${n.link==='missions'?'To missions':n.link==='tasks'?'To tasks':'To chat'}"`:'';
    return `<div class="nitem ${n.read?'':'unread'}"${lk}><div class=nt>${nEsc(n.title)}${n.link?' <span style="opacity:.5">\u2192</span>':''}</div>`+
      (n.body?`<div class=nb>${nEsc(n.body)}</div>`:'')+
      `<div class=nm>${nEsc(n.instance||'')} \u00b7 ${t}</div></div>`;
  }).join('');
  el.querySelectorAll('[data-link]').forEach(x=>x.onclick=()=>notifClick(x.dataset.link));
}
function notifClick(link){
  document.getElementById('npanel').hidden=true;
  if(link==='missions'){location.hash='#missions';loadMissions();}
  else if(link==='tasks'){location.hash='#tasks';}
  else if(link&&link.startsWith('chat:'))
    window.open('/chat?i='+encodeURIComponent(link.slice(5)),'_blank');
}
function notifDesktop(list){
  if(!('Notification' in window)||Notification.permission!=='granted')return;
  for(const n of (list||[])){
    if(NOTIF_SEEN.has(n.id))continue;NOTIF_SEEN.add(n.id);
    if((n.ts||0)>=NOTIF_START && !n.read){try{new Notification(n.title||'kAIm56',{body:n.body||'',tag:n.id});}catch(e){}}
  }
}
async function notifPoll(){
  for(;;){
    try{
      const r=await fetch('/api/notifications?since='+NOTIF_REV+'&wait=25');
      const d=await r.json();
      if(d.rev)NOTIF_REV=d.rev;
      if(d.notifications){notifRender(d.notifications);notifDesktop(d.notifications);}
      if(typeof d.unread==='number')notifBadge(d.unread);
    }catch(e){await new Promise(res=>setTimeout(res,3000));}
  }
}
function notifMarkAll(){
  fetch('/api/notifications/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({all:true})})
    .then(()=>{NOTIF_LIST.forEach(n=>n.read=true);notifRender();notifBadge(0);}).catch(()=>{});
}
function notifReadAll(){notifMarkAll();}
function notifClear(){
  fetch('/api/notifications/read',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({clear:true})})
    .then(()=>{NOTIF_LIST=[];notifRender();notifBadge(0);}).catch(()=>{});
}
function notifToggle(){
  const p=document.getElementById('npanel');if(!p)return;
  const show=p.hidden;p.hidden=!show;
  if(show){
    notifRender();
    if('Notification' in window && Notification.permission==='default')Notification.requestPermission();
    notifMarkAll();               // Oeffnen quittiert als gelesen
  }
}
document.addEventListener('click',e=>{
  const p=document.getElementById('npanel'),b=document.getElementById('nbell');
  if(p&&!p.hidden&&!p.contains(e.target)&&b&&!b.contains(e.target))p.hidden=true;
});
notifPoll();
</script></body></html>"""
