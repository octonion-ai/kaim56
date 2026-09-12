# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Manager web UI — page markup. ARCH_SVG separate: the update cycle edits it."""

HTML_TOP = """</head><body>
<div class=shell>
<header class=topbar><div class=topbar-in>
  <div class=brand>__LOGO__<b>kAIm56</b></div>
  <nav class=tabs id=tabs>
    <a href="#instances">Instances</a>
    <a href="#personas">Personas</a>
    <a href="#skills">Skills</a>
    <a href="#plugins">Plugins</a>
    <a href="#mcp">MCP</a>
    <a href="#tasks">Tasks</a>
    <a href="#missions">Missions</a>
    <a href="#policy">Policy</a>
    <a href="#models">Models</a>
    <a href="#resources">Resources</a>
    <a href="#sharing">Sharing</a>
    <a href="#secrets">Secrets</a>
    <a href="#settings">Settings</a>
  </nav>
  <button class=nbell id=nbell onclick=notifToggle() title="Benachrichtigungen" aria-label=Benachrichtigungen>
    <svg width=19 height=19 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.7 stroke-linecap=round stroke-linejoin=round><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"></path><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"></path></svg>
    <span class=nbadge id=nbadge hidden>0</span>
  </button>
  <div class=npanel id=npanel hidden>
    <div class=nhead><b>Benachrichtigungen</b><button class="btn btn-ghost" style="font-size:12px" onclick=notifClear()>Leeren</button></div>
    <div id=nlist><span class=text-muted style="font-size:13px;padding:12px;display:block">…</span></div>
  </div>
</div></header>
<main>

<section class="screen" id=s-instances>
  <div class=sec-head>
    <div><h6>microVM</h6><h3>Instances</h3></div>
    <span class="note text-muted">💬 Chat opens <a href="/chat">/chat</a> for every <code>TRANSPORT=web</code> instance · a stopped instance starts on the first prompt</span>
  </div>
  <table class=table>
    <thead><tr><th style="width:32%">Instance</th><th>Status</th><th>vCPU / RAM</th><th>Guest IP</th><th style="text-align:right">Actions</th></tr></thead>
    <tbody>__ROWS__</tbody>
  </table>

  <div style="margin-top:44px">
    <h6 style="color:var(--color-accent);margin:0 0 2px">Provision</h6>
    <h3 style="margin:0 0 18px">New instance from template</h3>
    <div class="panel blueprint">
      
      <div class=grid2>
        <div class=field><label>Template</label><select class=input id=tpl onchange=renderParams()>__TPLS__</select></div>
        <div class=field><label>Instance name</label><input class=input id=nm placeholder="e.g. fabric-gpt4o"></div>
        <div class=field><label>Persona / system prompt (optional)</label>
          <select class=input id=persona><option value="">— Default —</option></select></div>
      </div>
      <div class=grid2 id=params style="margin-top:20px"></div>
      <div class=grid2 style="margin-top:20px">
        <div class="field span2"><label>MCP servers (optional)</label><div id=mcp-pick style="display:flex;gap:20px;flex-wrap:wrap;padding-top:2px"></div></div>
        <div class="field span2"><label>katfs share (optional)</label>
          <div id=katfs-new class=text-muted style="font-size:12px;margin-bottom:6px">…</div>
          <select class=input id=katfsshare onchange=katfsNewHint()></select>
          <span class=text-muted id=katfsurlhint style="font-size:12px"></span>
        </div>
        <div class="field span2"><label>Mount host folders (optional)</label>
          <div id=mounts></div>
          <div style="display:flex;align-items:center;gap:12px;margin-top:6px">
            <button type=button class="btn btn-secondary btn-sm" onclick="addMount()">+ Folder</button>
            <span class=text-muted style="font-size:12px">Host path → guest path · pick with 📁 · "ro" = read-only · the agent writes as host user <code>kaim56-guest</code>, so a "rw" folder must let that user in · no host folder? <a href="#sharing">share one from your browser via katfs</a></span>
          </div>
        </div>
        <div class="field span2"><label>Capabilities</label>
          <label class=radio style="margin:2px 0 8px"><input type=checkbox id=cap-net checked><span class=dot></span>Internet access (LAN + web) — without it the agent only reaches the manager broker and <b>cannot</b> query the LLM</label>
          <div style="display:flex;align-items:center;gap:10px;margin:2px 0 6px">
            <span class=text-muted style="font-size:12px">Agent tools:</span>
            <button type=button class="btn btn-ghost" style="font-size:12px" onclick="toolAll(1)">all</button>
            <button type=button class="btn btn-ghost" style="font-size:12px" onclick="toolAll(0)">none</button>
          </div>
          <div id=toolpick style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:4px 16px"></div>
        </div>
      </div>
      <div class=panel-foot><button class="btn btn-primary" onclick=create()>Create instance</button></div>
    </div>
  </div>
</section>

<section class="screen" id=s-personas>
  <div class=sec-head>
    <div><h6>System prompts</h6><h3>Personas</h3></div>
    <span class="note text-muted">Selectable as "Persona" when creating an instance → sets <code>AGENT_SYSTEM</code> (applied at start, no rebuild)</span>
  </div>
  <div class=grid2 id=personas></div>
  <div class="panel blueprint" style="margin-top:32px">
    
    <h4 style="margin:0 0 16px">New persona</h4>
    <div style="display:grid;grid-template-columns:280px 1fr;gap:20px" class=pgrid>
      <div class=field><label>Name (a-z 0-9 _ -)</label><input class=input id=pname placeholder="e.g. researcher"></div>
      <div class=field style="grid-row:span 2"><label>Prompt</label><textarea class=input id=pprompt style="min-height:110px"></textarea></div>
      <div style="align-self:end;display:flex;align-items:center;gap:12px">
        <button class="btn btn-primary" onclick=savePersona()>Save persona</button><span id=pmsg class=msg></span>
      </div>
    </div>
  </div>

  <div class=sec-head style="margin-top:36px">
    <div><h6>Recurring jobs</h6><h3 style="font-size:22px">Prompt templates</h3></div>
    <span class="note text-muted">As a slash command in chat: <code>/name [extra]</code> — the agent expands it server-side (web, app and Signal)</span>
  </div>
  <div class="panel blueprint">
    <div id=promptlist><span class=text-muted style="font-size:13px">…</span></div>
    <div class=grid2 style="margin-top:16px">
      <div class=field><label>Name (becomes /name)</label><input class=input id=prname placeholder="daily"></div>
      <div class="field span2"><label>Prompt-Text</label><textarea class=input id=prtext style="min-height:70px" placeholder="Write my daily briefing: …"></textarea></div>
    </div>
    <div class=panel-foot><span id=prmsg class=msg></span><button class="btn btn-primary" onclick=savePrompt()>Save template</button></div>
  </div>

  <div class=sec-head style="margin-top:36px">
    <div><h6>Learned rules</h6><h3 style="font-size:22px">Playbooks</h3></div>
    <span class="note text-muted">Fixed rules per agent — apply EVERY turn. The agent learns them itself from corrections (playbook_add); view, add, remove them here.</span>
  </div>
  <div class="panel blueprint">
    <div class=field style="max-width:280px;margin-bottom:12px"><label>Agent</label>
      <select class=input id=pbinst onchange=loadPlaybooks()></select></div>
    <div id=pblist><span class=text-muted style="font-size:13px">…</span></div>
    <div class="field span2" style="margin-top:14px"><label>New rule</label>
      <textarea class=input id=pbtext style="min-height:56px" placeholder="Always fetch stock prices via http_fetch from query1.finance.yahoo.com …"></textarea></div>
    <div class=panel-foot><span id=pbmsg class=msg></span><button class="btn btn-primary" onclick=addPlaybook()>Add rule</button></div>
  </div>
</section>

<section class="screen" id=s-skills>
  <div class=sec-head>
    <div><h6>Expert knowledge</h6><h3>Skills</h3></div>
    <span class="note text-muted">Loaded into context on demand via <code>load_skill("name")</code> · <code>list_skills</code> shows them · central, no rebuild</span>
  </div>
  <div id=skprops></div>
  <div class=grid3 id=skills></div>
  <div class="panel blueprint" style="margin-top:32px">
    
    <h4 style="margin:0 0 16px">New skill</h4>
    <div class=grid2>
      <div class=field><label>Name (a-z 0-9 _ -)</label><input class=input id=skname placeholder="e.g. postgres-expert"></div>
      <div class=field><label>Short description</label><input class=input id=skdesc placeholder="When to load this skill?"></div>
      <div class="field span2"><label>Content (Markdown)</label><textarea class=input id=skcontent style="min-height:130px"></textarea></div>
    </div>
    <div class=panel-foot><span id=skmsg class=msg></span><button class="btn btn-primary" onclick=saveSkill()>Save skill</button></div>
  </div>
</section>

<section class="screen" id=s-plugins>
  <div class=sec-head>
    <div><h6>Custom tools</h6><h3>Plugins</h3></div>
    <span class="note text-muted">Drag a <code>.py</code> file or a <code>.zip</code> (multi-file &#8594; its own folder) here. Convention: <code>DESC / PARAMS / REQUIRED / run()</code>. Runs in the agent VM (sandbox, stdlib) &#183; restart the instance to activate.</span>
  </div>
  <div id=plugdrop class=plugdrop>
    <b>.py</b> or <b>.zip</b> — drag here &#8212; or <label class=pluglnk>browse<input type=file id=plugfile accept=".py,.zip" hidden></label>
  </div>
  <div class=grid2 style="margin-top:16px">
    <div class=field><label>New tool from boilerplate (name: a-z 0-9 _ -)</label><input class=input id=plugnew placeholder="e.g. weather_lookup"></div>
    <div class=field style="align-self:end"><button class="btn btn-secondary" onclick=plugCreate()>Create boilerplate</button></div>
  </div>
  <div id=plugmsg class=msg style="margin-top:8px"></div>
  <div id=pluglist class=grid3 style="margin-top:20px"></div>
</section>

<section class="screen" id=s-mcp>
  <div class=sec-head>
    <div><h6>Catalog</h6><h3>MCP servers</h3></div>
    <span class="note text-muted">Attachable per instance · use <code>${SECRET_NAME}</code> in args to inject from the secret store at create time</span>
  </div>
  <div class=grid2 id=mcps></div>
  <div class="panel blueprint" style="margin-top:32px">
    
    <h4 style="margin:0 0 16px">New MCP server</h4>
    <div class=grid2>
      <div class=field><label>Name (a-z 0-9 _ -)</label><input class=input id=mcpname placeholder="e.g. homeassistant"></div>
      <div class=field><label>Description</label><input class=input id=mcpdesc placeholder="What is this MCP?"></div>
      <div class=field><label>Command</label><input class=input id=mcpcmd placeholder="e.g. mcp-remote  or  npx"></div>
      <div class=field><label>Args (one per line)</label><textarea class=input id=mcpargs style="min-height:80px;font-family:var(--font-mono);font-size:12.5px" placeholder="http://10.0.0.10:8123/mcp_server/sse&#10;--header&#10;Authorization: Bearer ${HA_TOKEN}"></textarea></div>
      <div class="field span2"><label>Env (KEY=VALUE per line; use ${SECRET} for secrets)</label><textarea class=input id=mcpenv style="min-height:64px;font-family:var(--font-mono);font-size:12.5px" placeholder="PORTAINER_URL=http://10.0.0.20:9000&#10;PORTAINER_API_KEY=${PORTAINER_API_KEY}"></textarea></div>
    </div>
    <div class=panel-foot><span id=mcpmsg class=msg></span><button class="btn btn-primary" onclick=saveMcp()>Save MCP</button></div>
  </div>
</section>

<section class="screen" id=s-resources>
  <div class=sec-head>
    <div><h6>Sizing &amp; live usage</h6><h3>Resources</h3></div>
    <span class="note text-muted">Configured size (vCPU/RAM) and live usage per instance &#183; CPU% is relative to ONE core (a 2-vCPU guest goes up to ~200%) &#183; Disk = written overlay layer.</span>
  </div>
  <table class=table id=restable>
    <thead><tr><th style="width:22%">Instance</th><th>Status</th><th>vCPU</th><th>RAM (config.)</th><th>RAM (used)</th><th>CPU</th><th>Disk (Overlay)</th></tr></thead>
    <tbody id=resrows><tr><td colspan=7 class=text-muted style="padding:14px">…</td></tr></tbody>
  </table>
</section>

<section class="screen" id=s-models>
  <div class=sec-head>
    <div><h6>OpenRouter</h6><h3>Models</h3></div>
    <span class="note text-muted">The full catalog, fetched live · ticked models are the shortlist offered when creating an instance</span>
  </div>
  <div class="banner blueprint">
    
    <svg width=16 height=16 viewBox="0 0 24 24" fill=none stroke="var(--color-accent-700)" stroke-width=1.5 stroke-linecap=round stroke-linejoin=round><circle cx=12 cy=12 r=10></circle><path d="M12 8v4"></path><path d="M12 16h.01"></path></svg>
    <span>The openrouter template only offers models that can do tool calling — a model without it stays hidden even when ticked.</span>
  </div>
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
    <input class=input id=mdlq placeholder="filter by id or name…" style="flex:1;min-width:220px;width:auto" oninput=renderModels()>
    <label class=radio><input type=checkbox id=mdltools checked onchange=renderModels()><span class=dot></span>tool calling only</label>
    <label class=radio><input type=checkbox id=mdlsel onchange=renderModels()><span class=dot></span>selected only</label>
    <span class=text-muted id=mdlcount style="font-size:12px"></span>
    <button class="btn btn-secondary btn-sm" onclick="loadModels2(1)">Refresh catalog</button>
  </div>
  <div style="max-height:60vh;overflow:auto;border:1px solid var(--color-divider)">
    <table class=table id=mdltable><tbody id=mdlrows></tbody></table>
  </div>
  <div class=panel-foot><span id=mdlmsg class=msg></span><button class="btn btn-primary" onclick=saveModels()>Save shortlist</button></div>
</section>

<section class="screen" id=s-policy>
  <div class=sec-head>
    <div><h6>What each instance may do & does</h6><h3>Policy</h3></div>
    <span class="note text-muted">Network, tools, secrets and MCP in one place · per instance the tools and URLs called most recently</span>
  </div>
  <div id=policycards style="display:flex;flex-direction:column;gap:18px"></div>
</section>

<section class="screen" id=s-tasks>
  <div class=sec-head>
    <div><h6>Scheduled work</h6><h3>Tasks</h3></div>
    <span class="note text-muted">One message runs on one instance — once or recurring · a stopped instance is started for it</span>
  </div>
  <table class=table>
    <thead><tr><th style="width:20%">Instance</th><th>Job</th><th>Schedule</th><th>Status</th><th>Last result</th><th></th></tr></thead>
    <tbody id=taskrows></tbody>
  </table>
  <div class="panel blueprint" style="margin-top:32px">
    
    <h4 style="margin:0 0 16px" id=tk-head>New task</h4>
    <div class=grid2>
      <div class=field><label>Instance</label><select class=input id=tk-inst></select></div>
      <div class=field><label>Schedule (empty = once, right away)</label>
        <input class=input id=tk-sched placeholder="every 30m · every 2h · daily 08:00 · hourly">
        <span class=text-muted style="font-size:12px">Formats: <code>every Nm|Nh|Nd</code>, <code>daily HH:MM</code>, <code>hourly</code></span>
      </div>
      <div class="field span2"><label>Job (the message sent to the agent)</label>
        <textarea class=input id=tk-msg style="min-height:90px" placeholder="e.g. Summarise the new Home Assistant events and report anything unusual."></textarea></div>
    </div>
    <div class=panel-foot><span id=tkmsg class=msg></span><button class="btn btn-secondary" id=tk-cancel style="display:none" onclick=cancelEdit()>Cancel</button><button class="btn btn-primary" id=tk-save onclick=saveTask()>Create task</button></div>
  </div>
</section>

<section class="screen" id=s-missions>
  <div class=sec-head>
    <div><h6>Multi-step work</h6><h3>Missions</h3></div>
    <span class="note text-muted">Multi-step jobs from the orchestrator — plan + progress survive restart and context reset · a finished task immediately triggers the next step · every mission can be edited or deleted here, whatever its status</span>
  </div>
  <div id=missions class="panel blueprint">
    <span class=text-muted style="font-size:13px">…</span>
  </div>
  <p class=text-muted style="font-size:12.5px;margin-top:14px">Any agent creates a mission itself when a job needs several steps — e.g. via chat: "… — as a mission". The owner plans, the steps run on whichever instance has the needed tools.</p>
</section>

<section class="screen" id=s-sharing>
  <div class=sec-head>
    <div><h6>Browser → Agent</h6><h3>katfs sharing</h3></div>
    <span class="note text-muted">A folder from the browser you are sitting at, handed to the agents over P2P (iroh) · nothing is mounted into the microVM</span>
  </div>
  <div class="panel blueprint">
    
    <div id=katfs-status><span class=text-muted style="font-size:13px">checking…</span></div>
    <div class=field style="margin-top:18px">
      <label>Sharing key — node-id of the katfs node the browser connects to</label>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <input class="input mono" id=katfskey spellcheck=false autocomplete=off
               placeholder="64 hex characters" style="flex:1;min-width:260px;width:auto"
               oninput=keyHint()>
        <button class="btn btn-secondary" onclick=copyKey()>Copy</button>
        <button class="btn btn-secondary" onclick=resetKey() title="Back to this host's node-id">Reset</button>
      </div>
      <span class=text-muted id=keyhint style="font-size:12px"></span>
    </div>
    <div class=panel-foot>
      <span class=text-muted style="font-size:12px;margin-right:auto">The share lives in the browser tab — close it and the agents lose access.</span>
      <button class="btn btn-secondary" onclick=loadKatfs()>Refresh</button>
      <button class="btn btn-primary" onclick=openShare()>Share a folder…</button>
    </div>
  </div>
  <div class=sec-head style="margin-top:36px">
    <div><h6>App &#8596; Manager</h6><h3 style="font-size:22px">App transport (iroh)</h3></div>
    <span class="note text-muted">The Android app reaches the manager over iroh (P2P) &#183; no VPN, no exposed HTTPS port &#183; only allow-listed node-ids get through &#183; desktops use the same path via <code>kaim56-tunnel</code> (local port &#8594; iroh), so any plain-HTTP client rides the tunnel</span>
  </div>
  <div class="panel blueprint">
    
    <div class=field>
      <label>Manager node-id &#8212; paste this into the app (Settings &#8250; Server connection)</label>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <input class="input mono" id=irohnid readonly spellcheck=false
               placeholder="gateway not running (start the iroh-gw service)" style="flex:1;min-width:260px;width:auto">
        <button class="btn btn-secondary" onclick=irohCopy()>Copy</button>
      </div>
      <span class=text-muted id=irohhint style="font-size:12px"></span>
    </div>
    <div class=field style="margin-top:18px">
      <label>Paired phones (allow-listed node-ids)</label>
      <div id=irohallow><span class=text-muted style="font-size:13px">&#8230;</span></div>
    </div>
    <div class=grid2 style="margin-top:14px">
      <div class=field><label>Add a phone &#8212; its node-id (64 hex, shown in the app)</label>
        <input class="input mono" id=irohaddid spellcheck=false placeholder="64 hex characters"></div>
      <div class=field><label>Label (optional)</label>
        <div style="display:flex;gap:6px">
          <input class=input id=irohaddlabel placeholder="e.g. Ulrich Phone" style="flex:1">
          <button class="btn btn-primary" onclick=irohAdd()>Add</button>
        </div></div>
    </div>
    <div id=irohmsg class=msg style="margin-top:8px"></div>
  </div>

  <div class=grid2 style="margin-top:32px">
    <div class="card blueprint">
      
      <span class=card-title style="font-size:16px">How the agent reaches it</span>
      <p class=card-body>The share is not a filesystem — it is reachable only through the agent tools
      <code>remote_ls</code>, <code>remote_read(path)</code> and <code>remote_write(path, content)</code>,
      which talk to the katfs node on the host gateway. Paths are relative to the shared folder.
      Without an active share those tools answer <code>503 no browser connected</code>.</p>
    </div>
    <div class="card blueprint">
      
      <span class=card-title style="font-size:16px">Host folders instead</span>
      <p class=card-body>A folder that already lives on this host belongs in
      <b>Mount host folders</b> when creating an instance, or behind 📁 in the instance table —
      that one is a real live mount (NFS) inside the guest and survives without a browser tab.</p>
    </div>
  </div>

  <div class=sec-head style="margin-top:32px">
    <div><h6>Shared folder</h6><h3>File browser</h3></div>
    <span class="note text-muted">Browse the folder currently shared from a browser tab · read-only view</span>
  </div>
  <div class="panel blueprint">
    
    <div id=fbbar style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px">
      <button class="btn btn-secondary" id=fbup onclick=fbUp()>↑ up</button>
      <span id=fbpath class=mono style="font-size:12.5px;color:var(--color-neutral-600)">/</span>
      <span id=fbshare class=text-muted style="font-size:12px"></span>
      <button class="btn btn-ghost" id=fbdl style="margin-left:auto;font-size:12px" onclick=fbZip() disabled>&#8595; Download all</button>
      <button class="btn btn-ghost" style="font-size:12px" onclick="fbGo(FB.path)">Refresh</button>
    </div>
    <div id=fblist><span class=text-muted style="font-size:13px">…</span></div>
  </div>
</section>

<section class="screen" id=s-secrets>
  <div class=sec-head>
    <div><h6>Runtime access</h6><h3>Secrets access</h3></div>
    <span class="note text-muted">Per template/instance: which keys the host may substitute into that agent's MCP servers · <b>raw to guest</b>: which of those an agent may fetch via <code>get_secret(name)</code> — default none, since the hub and the LLM key proxy exist · values never written to disk</span>
  </div>
  <div class="banner blueprint" style="background:var(--color-neutral-100);margin:16px 0 20px;padding:9px 14px">
    
    <svg width=15 height=15 viewBox="0 0 24 24" fill=none stroke="var(--color-neutral-700)" stroke-width=1.5 stroke-linecap=round stroke-linejoin=round><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"></path><path d="M12 9v4"></path><path d="M12 17h.01"></path></svg>
    <span style="color:var(--color-neutral-800);font-size:12.5px">Keep dangerous keys (Docker / network admin) unchecked.</span>
  </div>
  <div id=secrets></div>
  <div class=panel-foot style="border:none;padding-top:0"><span id=secmsg class=msg></span><button class="btn btn-primary" onclick=saveSecrets()>Save access</button></div>
</section>

<section class="screen" id=s-changelog>
  <div class=sec-head>
    <div><h6>Open items</h6><h3>Security issues</h3></div>
    <span class="note text-muted">Findings from working on the system · text and rating live in <code>security.json</code>, only the status can be toggled here</span>
  </div>
  <div class="panel blueprint">
    
    <div id=issues><span class=text-muted style="font-size:13px">…</span></div>
  </div>
  <div class=panel-foot><span id=secissuemsg class=msg></span>
    <label class=radio style="margin-right:auto"><input type=checkbox id=showdone onchange=renderIssues()><span class=dot></span>show fixed ones</label>
    <button class="btn btn-primary" onclick=saveIssues()>Save status</button></div>

  <div class=sec-head style="margin-top:44px">
    <div><h6>History</h6><h3>Changelog</h3></div>
    <span class="note text-muted">from <code>CHANGELOG.md</code></span>
  </div>
  <div class="panel blueprint md" id=changelog>
    
  </div>
</section>


<section class="screen" id=s-architecture>
  <div class=sec-head>
    <div><h6>System</h6><h3>Architecture</h3></div>
    <span class="note text-muted">every box below runs on this host, except the phone, the Halo glasses, the Linux desktop, the user PC and the external services &#183; app and desktop reach the manager over iroh (P2P), no public port</span>
  </div>

  <div class="panel blueprint" style="padding:18px">
    
    """

ARCH_SVG = """<svg id="archsvg" viewBox="0 0 960 672" style="width:100%;height:auto;display:block;font-family:inherit">
      <defs><marker id="arw" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
        <path d="M0 0 L8 4 L0 8 z" fill="var(--color-neutral-500)"/></marker></defs>

      <rect class="bx" x="40" y="16" width="176" height="56"/>
      <text class="tt" x="128" y="38" text-anchor="middle">Desktop client</text>
      <text class="ss" x="128" y="56" text-anchor="middle">VAD &#183; wake word &#183; TTS out</text>
      <rect class="bx" x="244" y="16" width="120" height="56"/>
      <text class="tt" x="304" y="38" text-anchor="middle">Halo glasses</text>
      <text class="ss" x="304" y="56" text-anchor="middle">BLE &#183; photo &#183; voice</text>
      <rect class="bx" x="380" y="16" width="176" height="56"/>
      <text class="tt" x="468" y="38" text-anchor="middle">KatAgent (Android)</text>
      <text class="ss" x="468" y="56" text-anchor="middle">chat &#183; voice &#183; assistant key</text>
      <rect class="bx" x="584" y="16" width="130" height="56"/>
      <text class="tt" x="649" y="38" text-anchor="middle">Browser</text>
      <text class="ss" x="649" y="56" text-anchor="middle">admin + chat UI</text>
      <rect class="bx" x="760" y="16" width="160" height="56"/>
      <text class="tt" x="840" y="38" text-anchor="middle">Signal (phone)</text>
      <text class="ss" x="840" y="56" text-anchor="middle">chat with katbot</text>

      <rect class="bx" x="40" y="124" width="300" height="52"/>
      <text class="tt" x="190" y="145" text-anchor="middle">iroh-gw &#183; P2P relay</text>
      <text class="ss" x="190" y="162" text-anchor="middle">NodeId allowlist &#183; no public port</text>
      <rect class="bx" x="368" y="124" width="180" height="52"/>
      <text class="tt" x="458" y="145" text-anchor="middle">Traefik &#183; TLS+auth</text>
      <text class="ss" x="458" y="162" text-anchor="middle">__PUBLIC_HOST__</text>
      <rect class="bx" x="660" y="124" width="260" height="52"/>
      <text class="tt" x="790" y="145" text-anchor="middle">signal-cli REST</text>
      <text class="ss" x="790" y="162" text-anchor="middle">__SIGNAL_HOST__</text>

      <path class="ln" d="M364 44 L380 44" marker-end="url(#arw)"/><text class="lb" x="354" y="34">BLE</text>
      <path class="ln" d="M468 72 L250 124"/><text class="lb" x="300" y="96">iroh</text>
      <path class="ln" d="M649 72 L470 124"/>
      <path class="ln" d="M128 72 L150 124"/><text class="lb" x="156" y="104">iroh tunnel</text>
      <path class="ln" d="M840 72 L790 124"/>
      <path class="ln" d="M190 176 L210 224"/>
      <path class="ln" d="M458 176 L430 224"/>

      <rect class="bx2" x="40" y="224" width="560" height="186"/>
      <text class="tt" x="60" y="248">manager.py &#183; :8700 (root, systemd)</text>
      <text class="ss" x="60" y="274">&#183; REST APIs + admin UI + chat UI</text>
      <text class="ss" x="60" y="294">&#183; chat sync: long-poll, shared store</text>
      <text class="ss" x="60" y="314">&#183; secret broker (guest by source IP)</text>
      <text class="ss" x="60" y="334">&#183; security gateway (unicode + image meta)</text>
      <text class="ss" x="60" y="354">&#183; task scheduler + orchestrator ping</text>
      <text class="ss" x="330" y="274">&#183; voice :8770 &#183; mcp-hub :8771 &#183; embed :8772</text>
      <text class="ss" x="330" y="294">&#183; signal send (allowlist + rate limit)</text>
      <text class="ss" x="330" y="314">&#183; usage &#183; audit &#183; memory</text>
      <text class="ss" x="330" y="334">&#183; katfs proxy &#8594; :8790</text>
      <text class="ss" x="330" y="354">&#183; guest POST allowlist &#183; GET denylist (403)</text>
      <text class="ss" x="330" y="374">&#183; guest&#8594;host: only :8700 + NFS &#183; anti-spoof</text>
      <text class="ss" x="330" y="394">&#183; per-instance model / tools / mounts</text>

      <rect class="bx" x="660" y="224" width="260" height="56"/>
      <text class="tt" x="790" y="246" text-anchor="middle">voice service (Docker)</text>
      <text class="ss" x="790" y="264" text-anchor="middle">127.0.0.1:8770 &#183; Parakeet STT &#183; Piper TTS</text>
      <rect class="bx" x="660" y="312" width="260" height="56"/>
      <text class="tt" x="790" y="334" text-anchor="middle">katfs node &#183; :8790</text>
      <text class="ss" x="790" y="352" text-anchor="middle">P2P share &#8596; user PC</text>

      <path class="ln" d="M600 252 L660 252"/>
      <path class="ln" d="M600 340 L660 340"/>
      <path class="ln" d="M620 232 L680 180"/><text class="lb" x="665" y="205">/v2/send</text>

      <rect class="bx" x="40" y="444" width="560" height="44"/>
      <text class="ss" x="320" y="470" text-anchor="middle">chats.json &#183; memory.json &#183; history.db &#183; gateway.json &#183; settings.json &#183; instances/*.json &#183; audit/*.jsonl</text>
      <path class="ln" d="M320 410 L320 444"/>

      <rect class="bx" x="40" y="532" width="560" height="124"/>
      <text class="tt" x="60" y="556">Firecracker microVMs &#8212; one per agent</text>
      <text class="ss" x="60" y="580">&#183; tap fcN &#183; 172.30.N.2/30 &#183; NAT egress via host uplink</text>
      <text class="ss" x="60" y="600">&#183; private rootfs copy per start (sparse, removed on stop)</text>
      <text class="ss" x="60" y="620">&#183; guest: agent.py tool loop &#183; web_bridge :8080 &#183; webterm :7682</text>
      <text class="ss" x="60" y="640">&#183; MCP via manager &#8594; hub (no LAN, no tokens in guest)</text>
      <path class="ln" d="M320 488 L320 532"/>
      <path class="ln" d="M340 532 L340 488"/>
      <text class="lb" x="352" y="514">/i/&#8249;name&#8250; proxy &#183; broker &#183; tool calls</text>

      <rect class="bx" x="660" y="532" width="260" height="124"/>
      <text class="tt" x="790" y="556" text-anchor="middle">external</text>
      <text class="ss" x="790" y="580" text-anchor="middle">OpenRouter / Anthropic APIs</text>
      <text class="ss" x="790" y="600" text-anchor="middle">Home Assistant (MCP &#183; SSE)</text>
      <text class="ss" x="790" y="620" text-anchor="middle">Portainer (MCP, read-only)</text>
      <text class="ss" x="790" y="640" text-anchor="middle">user PC (katfs P2P)</text>
      <rect class="bx" x="660" y="400" width="260" height="56"/>
      <text class="tt" x="790" y="422" text-anchor="middle">mcp-hub (Docker)</text>
      <text class="ss" x="790" y="440" text-anchor="middle">127.0.0.1:8771 &#183; mcp-remote &#183; mcp-portainer</text>
      <path class="ln" d="M600 380 L660 424"/>
      <path class="ln" d="M790 456 L790 532"/>
      <path class="ln" d="M600 588 L660 588"/><text class="lb" x="606" y="580">NAT</text>
    </svg>"""

HTML_BOTTOM = """
  </div>

  <div class=sec-head style="margin-top:44px">
    <div><h6>Reference</h6><h3>Components</h3></div>
  </div>
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(430px,1fr));gap:14px">

  <div class="card blueprint"><span class=card-title>manager.py &#8212; the core</span>
  <p class=card-body>Split into an <code>mgr/</code> package (ui, store, signal, missions, notify, rules, mcp, katfs, gateway, routes); manager.py stays the systemd entry, facade and composition root (VM lifecycle, networking, secrets, the HTTP handler). The HTTP surface is migrating from an if-chain to a routing table (<code>mgr/routes.py</code>): exact paths beat prefixes, every route carries whether a guest VM may call it, and the inventory is enumerable for audits. mgr modules never import back (no cycles); cross-refs are injected. Single-file Python service (stdlib only), runs as root under systemd
  (<code>firecracker-manager</code>), listens on :8700 behind Traefik basicAuth. Serves the admin UI,
  the chat UI (<code>chatui.py</code>), and every API. Creates/starts/stops microVMs (openrouter rootfs boots as a shared read-only base +
  per-instance overlay upper &#8212; optionally persistent, so installs survive restarts), sets up
  tap devices and NAT, builds per-instance config disks, proxies requests into the guests
  (<code>/i/&#8249;name&#8250;/&#8230;</code>), and is the only component that guests can talk to.
  Guest requests are identified by source IP (a per-tap anti-spoof rule pins it); writes from
  guests are limited to an explicit allowlist, and the admin UI, chat, katfs and the
  <code>/i/&#8249;name&#8250;/</code> proxy (incl. the terminal) are denied to guests on GET
  &#8212; everything else returns 403. Guests may task only themselves, an ephemeral VM, or the
  instances in their <code>DELEGATE_TARGETS</code>; the roster, task history and approval ids
  they see are scoped the same way, and the inbox is the orchestrator's alone. On the host the
  INPUT chain lets guest traffic reach only :8700 and NFS. NFS is per instance: the workspace
  <code>agent/&#8249;name&#8250;</code> and each host folder are exported to that VM's address only, every
  write squashed to the system user <code>kaim56-guest</code>, which owns nothing else.</p></div>

  <div class="card blueprint"><span class=card-title>Firecracker microVMs</span>
  <p class=card-body>One VM per agent instance. Each gets a tap device <code>fc&#8249;N&#8250;</code> with a
  /30 subnet (host 172.30.N.1, guest 172.30.N.2) and NAT egress over the host uplink; internet
  can be switched off per instance. On every start the VM receives a fresh private copy of its
  template rootfs (sparse, ~550&#8201;MB real), deleted again on stop &#8212; VMs are stateless by design,
  durable state lives centrally. A small read-only config disk carries the non-secret instance
  settings into the guest.</p></div>

  <div class="card blueprint"><span class=card-title>Agent runtime (agent.py)</span>
  <p class=card-body>Tool-calling loop against an OpenAI-compatible backend inside each VM
  (templates: openrouter, <b>orcarouter</b>, pi, prime; the claude template runs Claude Code headless
  instead). The same agent code drives OpenRouter, <b>OrcaRouter</b> (gateway,
  <code>api.orcarouter.ai</code> or self-hosted OrcaRouter-Lite) and a local llama.cpp &#8212; the
  backend is picked by which env is set (<code>ORCAROUTER_MODEL</code> / <code>LLAMA_ENDPOINT</code>,
  else OpenRouter); the key comes from the Settings tab — with <code>LLM_KEY_PROXY</code> on (default here), LLM keys never enter a VM at all: agents call <code>/api/llm/&#8249;backend&#8250;</code> on the manager, which injects the Authorization header on egress (OneCLI pattern); the broker remains for other secrets. Built-in tools: bash, files,
  http_fetch, web_search, read_pdf, spawn_subagent, create_task, read_inbox, list_agents,
  recall_tasks, skills, memory, katfs remote files, secrets, send_signal. The system prompt
  (persona &#8594; <code>AGENT_SYSTEM</code>) always gets a standing memory instruction appended; on the
  first turn after a boot the agent injects its stored facts from <code>memory.json</code> into the
  prompt. Conversation context lives in VM RAM and dies with a restart &#8212; that is deliberate.</p></div>

  <div class="card blueprint"><span class=card-title>Harness patterns (context, goals, guardrails)</span>
  <p class=card-body>Ported from strands-agents/harness-sdk (Apache-2.0) into the stdlib agent, no new deps.
  <b>Summarizing context:</b> on overflow the oldest turns are folded into a pinned
  <code>[Summary]</code> block instead of being dropped &#8212; last ~10 turns stay verbatim.
  <b>Context offloader:</b> tool output over <code>OFFLOAD_MIN</code> is written to <code>.offload/</code>
  whole; the model sees a preview + reference and pages the rest via <code>offload_read</code>.
  <b>Goal loop:</b> <code>/goal &lt;criterion&gt;</code> makes a judge check each answer and refine it up to
  3 times. <b>Guardrails:</b> a hard bash denylist (rm&#8209;rf&#160;/, fork&#8209;bomb, mkfs) is always on; the <b>oracle</b> tool gives a second opinion before destructive actions (challenges assumptions, never acts — playbook-enforced for the orchestrator); risky
  tools can require Signal approval (<code>HITL=1</code> &#8594; manager asks &#8220;ok&#160;&lt;id&gt;&#8221;, routes
  <code>/api/hitl</code>). <b>Guardrails:</b> per-instance daily token budget + LLM rate-limit enforced at the key proxy, a task-frequency cap (>6/h -> paused), optional per-instance egress allowlist (<code>EGRESS_ALLOW</code>), and a secret leak-filter on outgoing notify/Signal. <b>Retry:</b> model calls back off on 429/5xx. <b>Local-model robustness:</b> llama.cpp/Qwen3 reasoning (<code>reasoning_content</code>) is streamed as a collapsible think block instead of being dropped; a tool-call-JSON 500 retries the turn without tools; and a heartbeat keeps the stream alive during long tool execution so a proxy idle-timeout can&#8217;t cut it mid-sentence. <b>Runtime control:</b> <code>/model</code> switches model/backend mid-session; <code>/steps &#8249;n&#8250;|unlimited</code> sets the per-turn tool-round cap; <b>steering</b> injects a user message between tool steps of a running turn (<code>POST /api/steer</code>); <b>prompt templates</b> (Personas tab) expand as <code>/name</code> in any channel; <b>tool plugins</b> (a single .py OR a multi-file folder in <code>plugins/</code>, added by drag-and-drop in the Plugins tab; each is SHA-256 content-pinned so a later out-of-band edit shows as \u201cmodified\u201d until re-approved, and every file name in the tab opens the file in the host&#8217;s VS Code, so approval is never blind) ride the config disk into the VM and register at agent start. <b>Tree-chat:</b> <code>/branch</code>/<code>/back</code> fork the context for a side question and fold it back into a one-line note.</p></div>

  <div class="card blueprint"><span class=card-title>Tests (E2E)</span>
  <p class=card-body>Stdlib <code>unittest</code>, no dependency: <code>tests/e2e.py</code> /
  <code>./run-tests.sh</code>. Three tiers that cleanly skip a missing environment &#8212;
  <b>OFFLINE</b> imports agent and manager directly and checks the core logic (backend choice,
  summarizing, offloader, hook denylist, goal, provider switch, HITL store, katfs ZIP walk);
  <b>HTTP</b> runs against the live manager (<code>/api/agents</code> backend+model,
  <code>/api/hitl</code>, katfs status); <b>LIVE</b> does a free <code>/goal</code> round-trip
  to the orchestrator VM. Runs on every change, together with the changelog and this tab.</p></div>

  <div class="card blueprint"><span class=card-title>Templates &amp; rootfs images</span>
  <p class=card-body><b>Install anywhere:</b> <code>install.sh</code> in the repo deploys the whole stack on a fresh KVM machine (preflight, layout, Firecracker download, builds, systemd) — verified end-to-end in a nested-KVM QEMU rig. Four templates (claude, openrouter, pi, prime), each with a Docker-built
  ext4 image under <code>instances/*.ext4</code>. The openrouter image carries node (npx MCP
  servers), python, mcp-remote, mcp-portainer and poppler; the agent code itself does not live
  in it any more: a <b>harness drive</b> (8&#8201;MB ext4, read-only, rebuilt from
  <code>AGENT_SRC</code> whenever the sources change) is attached to every VM on that image and
  mounted at <code>/harness</code>. An agent fix is one instance restart; a rootfs rebuild is
  for packages. The Instances tab flags VMs started before the last rebuild of either.</p></div>

  <div class="card blueprint"><span class=card-title>Secret broker &amp; policy</span>
  <p class=card-body><b>LLM keys go one step further:</b> with <code>LLM_KEY_PROXY</code> they never enter a VM — the manager injects them on egress (<code>/api/llm/&#8249;backend&#8250;</code>). API keys and tokens never land in instance configs or on the config disk.
  Two rights per key (<code>secret-policy.json</code>): a <b>release</b> to a template or instance
  lets the MCP hub substitute the value into a server config <i>on the host</i>; only a key that is
  also <b>guest-readable</b> leaves the host raw via <code>/api/secret/&#8249;name&#8250;</code>, the
  instance identified by source IP. Sources: the 0600 secret store and the manager settings. MCP
  configs are assembled server-side the same way (<code>/api/mcp-config</code>).</p></div>

  <div class="card blueprint"><span class=card-title>Security gateway</span>
  <p class=card-body>Per-chat toggle (shield icon in app and web). Strips invisible Unicode
  &#8212; tag characters U+E0020&#8211;E007F, zero-width, bidi overrides, homoglyph spaces, plus Unicode noncharacters (U+FDD0&#8211;FDEF, U+xFFFE/xFFFF) and reserved default-ignorables &#8212; from chat
  text in <em>both</em> directions, and EXIF/XMP/C2PA metadata from uploaded JPEG/PNG/WEBP,
  byte-surgically, before anything reaches the guest. Streams are cut at word boundaries so
  emoji ZWJ chains survive. State and counters live in <code>gateway.json</code>, filtering happens in
  the manager &#8212; a guest cannot switch it off. Removed characters are counted visibly.</p></div>

  <div class="card blueprint"><span class=card-title>Chat sync</span>
  <p class=card-body>One shared store (<code>chats.json</code>) for app, web and Signal turns.
  Clients long-poll <code>/api/chats?since=&#8249;rev&#8250;&amp;wait=&#8249;s&#8250;</code>; every write bumps a
  monotonic revision and wakes all waiters, so a message typed on the phone appears in the
  browser in sub-second time without polling. Deletions propagate too, via
  <b>tombstones</b> (<code>{id: deletedAt}</code>): the server never resurrects a deleted chat
  (even on an app re-push) unless it is genuinely re-edited afterwards. The store is display
  history &#8212; it is not fed back into the model.</p></div>

  <div class="card blueprint"><span class=card-title>Tasks &amp; orchestrator</span>
  <p class=card-body>Task queue with schedules (<code>every Nh</code>, <code>daily HH:MM</code>, &#8230;), editable in
  the Tasks tab. Tasks run on a capable instance or an ephemeral VM; results land in the shared
  chat history and in <code>history.db</code> (<code>task_runs</code>), queryable by agents via
  <code>recall_tasks</code>. New user messages ping the orchestrator instance, which routes work via
  <code>create_task</code> instead of doing it itself. Only the orchestrator (env <code>TASK_ADMIN</code>)
  gets the <code>list_tasks</code>/<code>delete_task</code>/<code>edit_task</code> tools, so it can prune or
  reschedule the queue itself; the matching <code>/api/task-delete</code> and <code>/api/task-edit</code> routes
  are gated to that instance. <code>llm_usage</code> in the same DB feeds the per-instance spend counter and the Activity panel&#8217;s per-window usage (<code>/api/usage/&#8249;name&#8250;?since=</code>): tokens are summed per time window, not attributed to single audit lines (a turn triggers 0..N tool calls).</p></div>

  <div class="card blueprint"><span class=card-title>Voice</span>
  <p class=card-body>Docker container bound to 127.0.0.1:8770, reachable only through the
  manager (<code>/api/stt</code>, <code>/api/tts</code>). STT: Parakeet TDT v3 int8 (RTF &#8776;0.08 on this CPU),
  TTS: Piper (RTF &#8776;0.07) with three voices baked in (de-thorsten, de-eva_k, en-amy) &#8212;
  voice and speed are picked in the Settings tab and injected by the manager into every
  <code>/api/tts</code> call, so clients keep sending only the text. The app records AAC, the service
  converts via ffmpeg. Speech-to-send, tap-bubble-to-stop and barge-in live in the app; the
  long-press assistant key starts listening immediately. A Linux desktop client
  (<code>voice-client/</code> in the repo) sits in the GNOME/KDE topbar and runs hands-free
  conversations over the same three endpoints: an energy VAD segments utterances, the mic is
  muted while the agent thinks or speaks, and the target instance is picked from the tray menu.
  An optional wake word gates busy rooms (conference calls) — as a transcript check, or as a
  LOCAL model (own-voice MFCC/DTW templates, enrolled in 30 seconds): then audio leaves the
  desktop only after the word, which is cut from the audio before STT ever sees it.
  It rides the iroh transport by default: given the gateway NodeId in its config it runs the
  embedded <code>kaim56-tunnel</code> as a child process — one binary, no HTTPS endpoint, no VPN.</p></div>

  <div class="card blueprint"><span class=card-title>Signal</span>
  <p class=card-body>Two directions, both through the signal-cli REST API
  (<code>__SIGNAL_HOST__</code>, run in <code>json-rpc</code> mode). Inbound: the manager holds a
  stdlib WebSocket to <code>/v1/receive</code>; a message from an allow-listed sender is handed straight to
  the orchestrator (prefixed <code>/fresh</code> so each trigger starts on a clean context) and the turn lands
  in the shared chat history. json-rpc mode fixed the native-mode lock where a long receive blocked sending.
  Outbound: agents call <code>send_signal</code>, the manager checks the recipient against
  <code>ALLOWED_SENDERS</code> (only people who may command the bot can be written to), rate-limits
  10 per 5 minutes, audits every call. Bot number and API stay on the host.</p></div>

  <div class="card blueprint"><span class=card-title>Notifications</span>
  <p class=card-body>A push channel alongside Signal: the agent tool <code>notify(title, message)</code> writes
  via <code>/api/notify</code> into a small store (<code>notifications.json</code>, rev + long-poll
  like the chat store, capped, rate-limited). It is fetched via <code>/api/notifications?since=&amp;wait=</code>:
  the <b>web manager</b> shows a bell with an unread badge + dropdown and can raise a browser notification;
  the <b>app</b> polls the same endpoint and raises an Android system notification. Every notification carries a <code>link</code> target
  (missions / tasks / chat:&#8249;instance&#8250;) — a click (web bell) or tap (Android) leads straight
  to the action. Unlike <code>send_signal</code> this rings on app/web, not in Signal.</p></div>

  <div class="card blueprint"><span class=card-title>KatAgent app</span>
  <p class=card-body>Android/Compose client. Talks only to the manager: chat via
  <code>/i/&#8249;name&#8250;/api/chat[/stream]</code>, sync via long-poll, voice via <code>/api/stt|tts</code>,
  gateway toggle via <code>/api/gateway</code>. Registers as the digital assistant (long-press power)
  and starts recording on invocation; silence auto-sends (adaptive threshold, 1.8&#8201;s hang).
  Local Gemma mode works offline on-device. Over iroh it dials the manager by NodeId
  (no public port); it is also the bridge for the Halo glasses (BLE).</p></div>

  <div class="card blueprint"><span class=card-title>Halo glasses</span>
  <p class=card-body>Brilliant Labs Halo, a BLE peripheral of the KatAgent app &#8212; not a direct
  manager client. GATT framing <code>[0x01, code, len_hi, len_lo, payload]</code>, receiver-paced
  acks; the device runs an ASCII-only Lua app (runtime reads latin-1). Photo capture (0x07/0x08)
  and mic audio (0x05/0x06) travel over BLE to the app, which forwards a photo or a WAV to the
  same <code>/api/stt</code> the app dictation uses; a voice command triggers the shot. Built and
  proven against a vendor-faithful emulator and a JVM-testable protocol layer before hardware.</p></div>

  <div class="card blueprint"><span class=card-title>ESP32 client (MrVoice)</span>
  <p class=card-body>Push-to-talk on a Seeed XIAO ESP32-S3 (<code>espclient/</code>, ESP-IDF 5.4, stock
  components only): button held = recording into PSRAM, release &#8594; WAV &#8594; <code>/api/stt</code>
  &#8594; <code>/api/chat/&#8249;inst&#8250;</code> (sentence-streamed) &#8594; <code>/api/tts</code> &#8594; I2S amplifier.
  Same server contract as the desktop client, over HTTPS with Basic auth; WiFi and manager
  credentials live in NVS (captive-portal setup), <code>probe</code> on the serial console runs the
  chain without the microphone. Its turns show up live in the web chat (voice session, archived on <code>/reset</code>).</p></div>

  <div class="card blueprint"><span class=card-title>Desktop client (voice)</span>
  <p class=card-body>One static Go binary in the GNOME/KDE topbar (<code>voice-client/</code>). Hands-free:
  an energy VAD segments utterances, an optional wake word gates busy rooms &#8212; as a transcript
  check or a LOCAL own-voice model (MFCC/DTW, enrolled in 30&#8201;s) that keeps audio on the desktop
  until the word is heard. Then record &#8594; <code>/api/stt</code> &#8594; <code>/api/chat/&#8249;inst&#8250;</code>
  (sentence-streamed to the first spoken reply) &#8594; <code>/api/tts</code>. Reaches the manager over
  iroh via an <b>embedded</b> <code>kaim56-tunnel</code> (started as a child process) &#8212; one file,
  no HTTPS endpoint. Target instance and a custom prompt are configurable.</p></div>

  <div class="card blueprint"><span class=card-title>katfs</span>
  <p class=card-body>P2P file share between this host and the user PC (node on :8790, loopback).
  Agents reach it through manager-proxied tools (<code>remote_ls/read/write/delete</code>); the share
  page under Sharing manages it. Several browser tabs can serve at once &#8212; each is one share
  (id, name, device); the built-in file browser lists them, a click scopes the tree to one share,
  and <b>Download all</b> streams the current folder recursively as a ZIP (<code>/api/katfs/zip</code>).
  Gives agents a controlled window into user files without mounting anything into a VM.</p></div>

  <div class="card blueprint"><span class=card-title>Memory &#8212; short &amp; long term</span>
  <p class=card-body><b>Short term</b> is the conversation itself &#8212; the agent&#8217;s <code>_history</code> in VM RAM; <code>/reset</code> clears it, a restart too. <b>Long term is semantic:</b> <code>memory_store</code> embeds each note (multilingual-e5 on the CPU, <code>embed</code> container behind the manager) and stores text+vector in <code>history.db</code>. Every turn the agent embeds the user&#8217;s message and the manager returns the meaning-nearest notes (cosine), injected as a fresh <code>[Memory]</code> block &#8212; only what fits the question, not the whole store. No LLM and no graph DB needed, so it runs on this host today; degrades to no recall (never an error) if the embedder is down. A richer knowledge-graph memory (Graphiti/Cognee) stays a possible upgrade.</p></div>

  <div class="card blueprint"><span class=card-title>Playbooks &#8212; rules the agent learns</span>
  <p class=card-body>Standing rules that ALWAYS apply, distinct from the meaning-based semantic memory. When the user says how to do something, states a lasting preference, or corrects the approach, the agent records it with <code>playbook_add</code>; every turn all playbooks are injected as a <code>[Playbooks]</code> block, so the orchestrator&#8217;s know-how grows with the user&#8217;s wishes. Per-instance store (<code>playbooks.json</code>, cap 40), tools <code>playbooks</code>/<code>playbook_forget</code>. Editable in the Personas tab (Playbooks panel). Proven: teach &#8220;stock prices via http_fetch from Yahoo&#8221; once &#8594; after a context reset the vague question &#8220;how&#8217;s Apple?&#8221; is answered correctly without naming the source again.</p></div>

  <div class="card blueprint"><span class=card-title>Missions &#8212; multi-step autonomy</span>
  <p class=card-body>Plan + progress store for multi-step assignments, persisted on the host
  (<code>missions.json</code>, keyed by the OWNER instance) so the working state survives resets and
  restarts. <b>Cross-instance:</b> every agent may own missions &#8212; it plans
  (<code>mission_start</code>: goal + steps), picks the capable instance per step
  (<code>list_agents</code>) and delegates via <code>create_task(target=&#8249;instance&#8250;)</code>,
  recording task-id <i>and</i> target on the step. Plan and execution therefore live on different
  agents. When tasks finish, completions are <b>collected per owner</b> for a short window and flushed
  as one push that advances the mission (event-driven, heartbeat only as fallback &#8212; a burst of
  finished steps costs one turn, not one each). Active missions are injected every turn as a
  <code>[Missions]</code> block; each agent only ever sees its own (the manager keys reads/writes by
  the calling instance, ephemeral VMs excluded). Guardrails: max 5 active / 20 steps per owner, 7-day
  TTL auto-pause, finish writes a summary into semantic memory and pushes a notification. UI: Missions
  tab (web) / screen (app) with owner, progress, current step + executing agent, and pause/abort.</p></div>

  <div class="card blueprint"><span class=card-title>Reasoning &amp; thinking</span>
  <p class=card-body>Per-agent runtime toggle via the slash command <code>/reasoning [low|medium|high|off]</code> (sets OpenRouter&#8217;s reasoning parameter; off by default, <code>OPENROUTER_REASONING</code> for a persistent default). The model&#8217;s thinking is streamed separately (marker-wrapped in the token stream, kept OUT of the conversation context so it never bloats follow-ups) and rendered in web and app as a collapsible &#8220;Denken&#8221; block; copy and speak take only the answer. Costs extra tokens, so it is a toggle, not always-on.</p></div>

  <div class="card blueprint"><span class=card-title>Storage (all under firecracker/)</span>
  <p class=card-body><code>instances/*.json</code> instance configs &#183; <code>chats.json</code> shared chat store &#183; <code>notifications.json</code> push-Benachrichtigungen &#183;
  <code>memory.json</code> per-agent key-value memory &#183; <code>playbooks.json</code> per-agent standing rules &#183;
  <code>history.db</code> task runs + LLM usage + semantic memory (vectors) &#183; <code>gateway.json</code> security-gateway state &#183;
  <code>settings.json</code> shared settings, 0600 &#183; <code>secret-policy.json</code> secret allowlists &#183;
  <code>personas.json</code>, <code>skills/</code>, <code>mcp-catalog.json</code> catalogs &#183;
  <code>audit/*.jsonl</code> per-instance tool audit trail &#183; <code>run/</code> pidfiles, sockets, logs,
  config disks and throwaway overlay uppers &#183; <code>instances/&#8249;n&#8250;-upper.ext4</code> persistent
  write layers (per-instance opt-in).</p></div>

  <div class="card blueprint"><span class=card-title>MCP servers &#8212; hub on the host</span>
  <p class=card-body>Catalog in <code>mcp-catalog.json</code> (homeassistant via <code>mcp-remote</code>/SSE,
  portainer via <code>mcp-portainer</code>, read-only, caldav via <code>caldav-mcp</code> for
  calendar entries and tasks). The server processes run in the
  <code>mcp-hub</code> container on the host (127.0.0.1:8771), one per (instance, server). Guests speak
  plain JSON-RPC to the manager (<code>/api/mcp</code>); the manager authorizes by source IP against the
  instance&#8217;s <code>MCP_SERVERS</code>, injects the secrets host-side and forwards to the hub &#8212;
  and for voice light control the <code>ha_control</code> tool matches a spoken target against HA
  entities/areas server-side (exact &#8594; area &#8594; fuzzy) and auto-learns a spoken alias on a
  fuzzy hit, so &#8220;Gartenhaus denke rechts&#8221; keeps working after STT mishears &#8220;Decke&#8221; &#8212;
  tokens and LAN never reach a VM. Every <code>tools/call</code> lands in the audit trail. A server is
  active only when listed in <code>MCP_SERVERS</code>; the hub respawns dead processes and replays
  their initialization.</p></div>

  <div class="card blueprint"><span class=card-title>Web UIs</span>
  <p class=card-body>Two pages, both served by the manager, both in the Industry design system:
  this admin UI (embedded in <code>manager.py</code>, hash-routed tabs) and the chat UI
  (<code>chatui.py</code> under <code>/chat</code>: streaming, images, voice, gateway toggle, per-browser
  history in localStorage). No CDN, no build step &#8212; one file each.</p></div>

  </div>
</section>

<section class="screen" id=s-settings style="max-width:640px">
  <div style="margin-bottom:18px"><h6 style="color:var(--color-accent);margin:0 0 2px">Shared, persisted</h6><h3 style="margin:0">Settings</h3></div>
  <p class=text-muted style="font-size:13px;margin-bottom:22px">Values apply to all new instances; empty template fields are pre-filled from here.</p>
  <div class="panel blueprint">
    
    <div class="banner blueprint" style="margin:0 0 16px;padding:9px 14px"><span id=voicestat class=text-muted style="font-size:12.5px">Voice: …</span></div>
    <div class=stack id=settings></div>
    <div class=panel-foot><span id=setmsg class=msg></span><button class="btn btn-primary" onclick=saveSettings()>Save</button></div>
  </div>
</section>

</main>

<footer class=appfoot>
  <span class=af-brand>kAIm56</span>
  <span class=af-stat>NAT via __HOSTIF__</span>
  <span class=af-stat>Pool __POOL__</span>
  <span class=af-stat id=spend></span>
  <nav class=af-nav>
    <a href="#changelog">Changelog</a>
    <a href="#architecture">Architecture</a>
    __CODE_LINK__
  </nav>
</footer>
</div>

<div id=picker class=dialog-backdrop style="display:none;z-index:60">
  <div class="dialog blueprint" style="width:min(560px,100%)">
    
    <div class=dialog-title>Choose a host folder</div>
    <div style="display:flex;gap:6px">
      <input class=input id=pkpath spellcheck=false onkeydown="if(event.key==='Enter')pkGo(this.value)">
      <button class="btn btn-secondary" onclick="pkGo(document.getElementById('pkpath').value)">Go</button>
    </div>
    <div class=pkquick id=pkquick></div>
    <div class=pklist id=pklist></div>
    <div id=pkerr class=text-muted style="font-size:12px;min-height:1em"></div>
    <div class=dialog-actions>
      <span class=text-muted style="font-size:12px;margin-right:auto">Not on this host? <a href="#sharing" onclick=pkClose()>share it from your browser via katfs</a></span>
      <button class="btn btn-secondary" onclick=pkClose()>Cancel</button>
      <button class="btn btn-primary" onclick=pkChoose()>Use this folder</button>
    </div>
  </div>
</div>

<div id=midlg class=dialog-backdrop style="display:none" onclick="if(event.target===this)midlgClose()">
  <div class="dialog blueprint" style="width:min(720px,100%)">
    
    <div class=dialog-title>Edit mission &#8212; <span id=midlgid class=mono style="font-size:16px"></span></div>
    <div class=dialog-body>Any status. Steps are matched by position: existing ones keep their status and result, new lines start open, removed lines are dropped. Setting the status back to <b>active</b> reopens the mission for its owner.</div>
    <div class=field><label>Goal</label><input class=input id=migoal maxlength=300></div>
    <div class=field><label>Steps (one per line)</label><textarea class=input id=misteps rows=8 style="font-family:var(--font-mono);font-size:12.5px"></textarea></div>
    <div class=field><label>Status</label><select class=input id=mistatus><option value=active>active</option><option value=paused>paused</option><option value=done>done</option><option value=failed>failed</option></select></div>
    <div id=mihint class=text-muted style="font-size:12px"></div>
    <div class=dialog-actions>
      <button class="btn btn-secondary" onclick=midlgClose()>Cancel</button>
      <button class="btn btn-primary" onclick=missionSave()>Save</button>
    </div>
  </div>
</div>
<div id=mdlg class=dialog-backdrop style="display:none">
  <div class="dialog blueprint" style="width:min(720px,100%)">
    
    <div class=dialog-title>Host folders — <span id=mdlgname class=mono style="font-size:16px"></span></div>
    <div class=dialog-body>Host path → guest path, "ro" = read-only. Saved changes are picked up by the reconciler in the running guest.</div>
    <div id=mdlgrows></div>
    <div><button type=button class="btn btn-secondary btn-sm" onclick="addMount(null,'mdlgrows')">+ Folder</button></div>
    <div class=dialog-actions>
      <button class="btn btn-secondary" onclick=mdlgClose()>Cancel</button>
      <button class="btn btn-primary" onclick=saveMounts()>Save</button>
    </div>
  </div>
</div>

<div id=modeldlg class=dialog-backdrop style="display:none">
  <div class="dialog blueprint" style="width:min(560px,100%)">
    
    <div class=dialog-title>Model — <span id=modeldlgname class=mono style="font-size:16px"></span></div>
    <div class=dialog-body>The new model takes effect after the next stop/start of the instance.</div>
    <div id=modeldlgbox></div>
    <div class=dialog-actions>
      <button class="btn btn-secondary" onclick=modelDlgClose()>Cancel</button>
      <button class="btn btn-primary" onclick=saveModel()>Save</button>
    </div>
  </div>
</div>

<div id=actdlg class=dialog-backdrop style="display:none">
  <div class="dialog blueprint" style="width:min(720px,100%)">
    
    <div class=dialog-title>Activity — <span id=actname class=mono style="font-size:16px"></span></div>
    <div class=dialog-body>Tools and targets called most recently (URLs/paths/queries). No secret values, no file contents.</div>
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:2px 0 10px">
      <div class=actwin id=actwin>
        <button data-w=3600 onclick="actWindow(3600)">1h</button>
        <button data-w=86400 onclick="actWindow(86400)">24h</button>
        <button data-w=604800 onclick="actWindow(604800)">7d</button>
        <button data-w=0 class=on onclick="actWindow(0)">Alle</button>
      </div>
      <span id=actsum class=text-muted style="font-size:12px;margin-left:auto"></span>
    </div>
    <div style="max-height:56vh;overflow:auto;border:1px solid var(--color-divider)">
      <table class=table><tbody id=actrows></tbody></table>
    </div>
    <div class=dialog-actions>
      <button class="btn btn-secondary" onclick=actClose()>Close</button>
      <button class="btn btn-secondary" onclick="openActivity(ACT_CUR)">Refresh</button>
    </div>
  </div>
</div>

"""
