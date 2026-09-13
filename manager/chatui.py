#!/usr/bin/env python3
"""Chat interface for the agent microVMs (served by the manager under /chat).
Just one HTML page, no dependencies, no CDN.

Backend is the manager:
  GET  /api/instances      agent list + run state
  POST /api/chat/<name>    prompt -> reply tokens as raw text (stream)

The history lives in the browser's localStorage. The microVM keeps its own
session (claude --resume / _history), so each turn only the new message goes
out — a new chat here does not start a new agent session.
"""

PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>kAIm56 chat</title>
<link rel=icon type="image/svg+xml" href="/logo.svg">
<style>
/* Design system "Industry" (claude.ai/design) — taken directly from the
   project's styles.css: token ramps, Barlow/Barlow Condensed, square
   blueprint objects with hairlines and registration corners. The dark block
   is derived from the same OKLCH ramps (the system ships only the light
   band). */
@import url('https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;700&family=Barlow+Condensed:wght@400;600&display=swap');
:root{
  color-scheme:light dark;
  --bg:#f2f2f3;--panel:#e9e9ea;--panel-2:#f5f5f8;
  --border:color-mix(in srgb,#1d1f20 16%,transparent);
  --text:#1d1f20;--muted:color-mix(in srgb,#1d1f20 55%,transparent);--heading:#1d1f20;
  --accent:#5980a6;--accent-contrast:#f2f2f3;
  --accent-100:#eef6ff;--accent-400:#94bce3;--accent-600:#597ea3;--accent-700:#416180;--accent-800:#2c455d;
  --accent-fill:#416180;--accent-fill-hover:#2c455d;   /* filled controls: deep in light, light in dark */
  --ok:#416180;--off:#98989b;
  --shadow:0 1px 2px color-mix(in srgb,#2b2b2d 14%,transparent);
  --shadow-md:0 3px 10px color-mix(in srgb,#2b2b2d 16%,transparent);
  --font-body:"Barlow",system-ui,sans-serif;
  --font-heading:"Barlow Condensed",system-ui,sans-serif;
}
@media(prefers-color-scheme:dark){:root{
  --bg:#141618;--panel:#1c1f22;--panel-2:#24272b;
  --border:color-mix(in srgb,#e8e9ea 16%,transparent);
  --text:#e8e9ea;--muted:color-mix(in srgb,#e8e9ea 55%,transparent);--heading:#e8e9ea;
  --accent:#94bce3;--accent-contrast:#141618;
  --accent-100:#1d2d3d;--accent-400:#94bce3;--accent-600:#b5d9fd;--accent-700:#94bce3;--accent-800:#2c455d;
  --accent-fill:#94bce3;--accent-fill-hover:#b5d9fd;
  --ok:#94bce3;--off:#7a7a7d;
  --shadow:0 1px 2px rgba(0,0,0,.4);
  --shadow-md:0 3px 10px rgba(0,0,0,.45);
}}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;display:flex;height:100dvh;overflow:hidden;background:var(--bg);color:var(--text);
  font-family:var(--font-body);font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
::selection{background:color-mix(in srgb,var(--accent) 30%,transparent)}
:focus{outline:none}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-thumb{background:color-mix(in srgb,var(--text) 18%,transparent)}
::-webkit-scrollbar-thumb:hover{background:color-mix(in srgb,var(--text) 30%,transparent)}
::-webkit-scrollbar-track{background:transparent}

/* — Blueprint objects: square, hairline — */
.blueprint{position:relative;border:1px solid var(--border)}
  color:color-mix(in srgb,var(--text) 55%,transparent)}

.kicker{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--accent);
  font-family:var(--font-body);font-weight:500}

/* ---- Sidebar ---- */
#side{width:270px;flex:none;background:var(--panel);border-right:1px solid var(--border);
  display:flex;flex-direction:column;transition:margin-left .2s ease}
#side.hidden{margin-left:-270px}
.side-top{padding:12px;border-bottom:1px solid var(--border)}
#new{width:100%;display:flex;align-items:center;justify-content:center;gap:6px;
  padding:9px 12px;cursor:pointer;
  font-family:var(--font-heading);font-weight:600;font-size:14.5px;letter-spacing:.01em;
  border:1px solid var(--border);border-radius:0;background:transparent;color:var(--text)}
#new:hover{background:color-mix(in srgb,var(--text) 7%,transparent);border-color:var(--accent)}
.side-label{padding:12px 14px 4px;font-size:10px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted)}
#convs{flex:1;overflow-y:auto;padding:4px 8px 8px}
.conv{display:flex;align-items:center;gap:6px;padding:8px 9px;cursor:pointer;
  font-size:13.5px;color:var(--text);white-space:nowrap;border:1px solid transparent}
.conv:hover{background:var(--panel-2)}
.conv.sel{background:var(--panel-2);border-color:var(--border);
  box-shadow:inset 2px 0 0 var(--accent)}
.conv .t{flex:1;overflow:hidden;text-overflow:ellipsis}
.conv .x{opacity:0;border:none;background:none;color:var(--muted);cursor:pointer;font-size:.95rem;padding:0 2px}
.conv:hover .x{opacity:1}
.conv .x:hover{color:var(--accent)}
.conv .ag{font-size:10px;letter-spacing:.02em;color:var(--accent-700);background:var(--accent-100);
  border-radius:0;padding:2px 7px}
.side-foot{padding:11px 13px;border-top:1px solid var(--border);font-size:.82rem}
.side-foot a{color:var(--muted);text-decoration:none}
.side-foot a:hover{color:var(--accent)}

/* ---- Main ---- */
#main{flex:1;display:flex;flex-direction:column;min-width:0}
header{display:flex;align-items:center;gap:8px;padding:9px 14px;border-bottom:1px solid var(--border);
  background:var(--panel);flex:none}
.icon{border:1px solid transparent;background:none;color:var(--muted);cursor:pointer;font-size:1.05rem;
  width:34px;height:34px;display:inline-flex;align-items:center;justify-content:center;
  border-radius:0;line-height:1;padding:0}
.icon:hover{color:var(--text);border-color:var(--border);background:color-mix(in srgb,var(--text) 7%,transparent)}
/* Gateway: "on" is a state you must see at a glance — hence colour and
   border, not just a different icon. */
.icon.on{color:var(--ok);border-color:var(--ok);background:var(--accent-100)}
.gwcount{font-size:.72rem;color:var(--muted);font-variant-numeric:tabular-nums}
#agent{font-family:var(--font-heading);font-weight:600;font-size:15.5px;letter-spacing:.01em;
  color:var(--text);background:transparent;
  border:1px solid var(--border);border-radius:0;padding:6px 10px;max-width:15rem;min-height:34px}
#agent:hover{border-color:color-mix(in srgb,var(--text) 45%,transparent)}
#agent:focus{outline:2px solid var(--accent);outline-offset:0;border-color:var(--accent)}
#state{font-size:.8rem;color:var(--muted);display:flex;align-items:center;gap:6px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--off);flex:none}
.dot.on{background:var(--ok);box-shadow:0 0 0 3px color-mix(in srgb,var(--ok) 22%,transparent)}
.grow{flex:1}

#log{flex:1;overflow-y:auto;padding:26px 14px 12px}
.wrap{max-width:760px;margin:0 auto}
.row{display:flex;gap:12px;margin:0 0 26px}
.row.me{justify-content:flex-end}
.av{width:30px;height:30px;flex:none;display:grid;place-items:center;font-size:.9rem;
  background:transparent;border:1px solid var(--border);color:var(--accent)}
.icon svg,.av svg{display:block}
.body{min-width:0;max-width:100%}
.row.me .body{background:var(--accent);color:var(--accent-contrast);padding:10px 14px;max-width:80%;
  box-shadow:var(--shadow)}
.row:not(.me) .body{border:1px solid var(--border);padding:10px 14px;background:var(--panel);
  box-shadow:var(--shadow)}
.body p{margin:.55rem 0}.body p:first-child{margin-top:0}.body p:last-child{margin-bottom:0}
.body h1,.body h2,.body h3{color:var(--heading);margin:1rem 0 .5rem;font-size:1.2rem;
  font-family:var(--font-heading);font-weight:600;letter-spacing:-.015em;line-height:1.12}
.body ul,.body ol{margin:.5rem 0;padding-left:1.3rem}
.body li{margin:.2rem 0}
.body a{color:var(--accent);text-underline-offset:3px}
.row.me .body a{color:inherit}
.body img{max-width:280px;margin:.3rem 0;display:block}
.body code{background:color-mix(in srgb,var(--text) 10%,transparent);padding:.08rem .34rem;font-size:.88em}
.body pre{position:relative;background:var(--panel-2);border:1px solid var(--border);
  padding:.8rem .9rem;overflow-x:auto;margin:.7rem 0}
.body pre code{background:none;padding:0;font-size:.85rem;line-height:1.5}
.cp{position:absolute;top:.4rem;right:.4rem;font-family:var(--font-heading);font-weight:600;
  font-size:.72rem;letter-spacing:.02em;color:var(--muted);cursor:pointer;
  background:var(--panel);border:1px solid var(--border);padding:.15rem .5rem}
.cp:hover{color:var(--text);border-color:var(--accent)}
.tools{margin-top:.4rem;height:1.1rem;display:flex;gap:12px}
.tools button{font-family:var(--font-heading);font-weight:600;letter-spacing:.02em;
  font-size:.75rem;color:var(--muted);background:none;border:none;cursor:pointer;padding:0;opacity:0}
.row:hover .tools button{opacity:1}
.tools button:hover{color:var(--accent)}
.body{user-select:text;-webkit-user-select:text}
.tools-me{justify-content:flex-end}
.row.me .tools button{color:var(--accent-contrast)}
.row.me:hover .tools button{opacity:.8}
.row.me .tools button:hover{opacity:1;color:#fff}
.think{margin:0 0 .6rem;border:1px solid var(--border);background:color-mix(in srgb,var(--text) 4%,transparent)}
.think summary{cursor:pointer;padding:.4rem .7rem;font-size:10.5px;letter-spacing:.08em;
  text-transform:uppercase;color:var(--muted);user-select:none;font-weight:500}
.think summary:hover{color:var(--text)}
.thinkbody{padding:.2rem .8rem .6rem;font-size:.82rem;line-height:1.5;color:var(--muted);white-space:pre-wrap}
.cursor{display:inline-block;width:.5rem;height:1rem;background:var(--accent);vertical-align:-2px;
  animation:blink 1s step-end infinite}
@keyframes blink{50%{opacity:0}}

/* ---- Welcome ---- */
#hello{max-width:560px;margin:0 auto;padding:11vh 0 0;text-align:center}
#hello .panel{position:relative;border:1px solid var(--border);padding:34px 30px 30px;background:var(--panel)}
#hello h2{font-family:var(--font-heading);font-weight:600;font-size:30px;color:var(--heading);
  margin:6px 0 6px;letter-spacing:-.015em;line-height:1.12}
#hello p{color:var(--muted);margin:0 0 22px;font-size:.92rem}
.chips{display:flex;flex-wrap:wrap;gap:8px;justify-content:center}
.chips button{font-family:var(--font-heading);font-weight:600;font-size:13.5px;letter-spacing:.01em;
  color:var(--text);background:transparent;cursor:pointer;
  border:1px solid var(--border);padding:8px 15px}
.chips button:hover{background:color-mix(in srgb,var(--text) 7%,transparent);border-color:var(--accent)}

#slashhint{max-height:320px;overflow-y:auto;border:1px solid var(--border);border-radius:14px;
  background:var(--panel);box-shadow:0 10px 34px rgba(0,0,0,.20);padding:6px}
.slhead{padding:9px 12px 7px;font-size:.72rem;color:var(--muted);letter-spacing:.02em}
.slrow{display:flex;gap:10px;align-items:baseline;padding:9px 12px;cursor:pointer;
  border-radius:9px;font-size:.9rem}
.slrow:hover{background:var(--panel-2)}
.slrow.slsel{background:var(--panel-2)}
.slrow code{flex:none;color:var(--text);font-weight:600;background:none;padding:0}
.slrow span{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.82rem}
.sltag{flex:none;margin-left:auto;font-size:.68rem;font-style:normal;color:var(--accent);border:1px solid var(--border);border-radius:6px;padding:0 6px;line-height:1.5}

/* ---- Branches (tree chat) ---- */
#branchbar{display:flex;align-items:center;gap:10px;padding:7px 12px;
  background:var(--accent-100);border:1px solid var(--accent-400)}
.bb-label{flex:1;font-size:.8rem;color:var(--accent-700)}
.bb-btn{font-family:var(--font-heading);font-weight:600;font-size:.8rem;cursor:pointer;
  background:var(--accent-fill);color:var(--accent-contrast);border:1px solid var(--accent-fill);padding:4px 12px}
details.ast{margin:0 0 22px;border-left:2px solid var(--accent-400);padding-left:14px}
details.ast>summary{cursor:pointer;font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--accent-700);margin-bottom:12px;user-select:none;list-style-position:inside}
details.ast .row{margin-bottom:18px}
#branchBtn.on{color:var(--accent-700);border-color:var(--accent-400);background:var(--accent-100)}

/* ---- Composer ---- */
#comp{flex:none;padding:10px 14px 18px;background:var(--bg)}
#box{max-width:760px;margin:0 auto;background:var(--panel);border:1px solid var(--border);
  box-shadow:var(--shadow-md);padding:7px 8px 7px 14px}
#box:focus-within{border-color:var(--accent)}
#thumbs{display:flex;gap:6px;padding:6px 0 2px}
#thumbs:empty{display:none}
#thumbs .th{position:relative}
#thumbs img{height:54px;display:block;border:1px solid var(--border)}
#thumbs .doc{display:flex;flex-direction:column;gap:4px;max-width:520px;padding:8px 12px;
  background:var(--panel-2);border:1px solid var(--border);cursor:pointer;font-size:12.5px}
#thumbs .doc b{font-weight:600}
#thumbs .doc small{color:var(--muted)}
#thumbs .doc pre{margin:4px 0 0;max-height:180px;overflow:auto;white-space:pre-wrap;
  font-size:11.5px;color:var(--muted);border-top:1px solid var(--border);padding-top:6px}
#thumbs .x{position:absolute;top:-6px;right:-6px;background:var(--panel-2);border:1px solid var(--border);
  color:var(--text);width:18px;height:18px;font-size:.7rem;line-height:1;cursor:pointer}
.inrow{display:flex;align-items:flex-end;gap:4px}
#t{flex:1;border:none;background:none;color:var(--text);font:inherit;font-size:1rem;resize:none;
  max-height:180px;padding:.55rem 0;outline:none}
#t::placeholder{color:var(--muted)}
#send{flex:none;width:36px;height:36px;border:1px solid var(--accent-fill);cursor:pointer;font-size:1rem;
  background:var(--accent-fill);color:var(--accent-contrast)}
#send:hover{background:var(--accent-fill-hover);border-color:var(--accent-fill-hover)}
#send[disabled]{opacity:.35;cursor:default}
#send.stop{background:var(--text);color:var(--panel);border-color:var(--text)}
.foot{max-width:760px;margin:8px auto 0;text-align:center;color:var(--muted);font-size:.72rem}
#micBtn.rec{color:var(--accent-contrast);background:var(--accent);border-color:var(--accent)}
#micBtn:disabled{opacity:.4}
.home{display:flex;align-items:center;gap:8px;text-decoration:none;color:inherit}
.home svg{flex:none}

/* ---- Inline search (design "Agent chat — session panel + inline search") ---- */
.icon.act{color:var(--accent-contrast);background:var(--accent-fill);border-color:var(--accent-fill)}
.icon.act:hover{color:var(--accent-contrast);background:var(--accent-fill-hover)}
#searchbar{display:none;align-items:center;gap:10px;padding:8px 14px;border-bottom:1px solid var(--border);
  background:var(--accent-100);flex:none}
#searchbar.open{display:flex}
#searchbar svg{flex:none;color:var(--accent-700)}
#sq{flex:1;min-height:30px;padding:3px 8px;font:inherit;font-size:13px;color:var(--text);
  background:var(--bg);border:1px solid var(--border);caret-color:var(--accent)}
#sq:focus{border-color:var(--accent)}
#smatch{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent-700);
  min-width:70px;text-align:right;font-variant-numeric:tabular-nums}
.sbtn{width:26px;height:26px;display:grid;place-items:center;cursor:pointer;background:none;
  border:1px solid var(--border);color:var(--text);padding:0}
.sbtn:hover{border-color:var(--accent);color:var(--accent)}
.sbtn.plain{border-color:transparent;color:var(--muted)}
.row.dim{opacity:.35;transition:opacity .15s}
.row.hit{opacity:1}
.row.hit.cur .body{border-color:var(--accent);box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 35%,transparent)}
.row.me.hit.cur .body{box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 35%,transparent),0 0 0 3px var(--bg)}
mark.sh{background:color-mix(in srgb,var(--accent) 30%,transparent);color:inherit;padding:0 1px}
.row.cur mark.sh{background:color-mix(in srgb,var(--accent) 55%,transparent)}

/* ---- Session panel ---- */
#panel{width:300px;flex:none;border-left:1px solid var(--border);background:var(--bg);
  padding:16px 20px;display:flex;flex-direction:column;gap:16px;font-size:13px;overflow-y:auto}
#panel.hidden{display:none}
.pk{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.phead{display:flex;justify-content:space-between;align-items:center}
.pcard{padding:14px}
.pname{font-family:var(--font-heading);font-weight:600;font-size:22px;line-height:1.1}
.pstate{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--accent-700)}
.pstate .sq{width:7px;height:7px;background:var(--off)}
.pstate.on .sq{background:var(--accent)}
.pgrid{display:grid;grid-template-columns:1fr 1fr;gap:8px 12px;margin-top:12px;font-size:12px}
.pgrid .k{color:var(--muted);font-size:10px;letter-spacing:.1em;text-transform:uppercase}
.pgrid .v{word-break:break-word}
.pbtns{display:flex;gap:6px;margin-top:14px}
.pbtn{flex:1;font-family:var(--font-heading);font-weight:600;font-size:12px;padding:5px;cursor:pointer;
  background:transparent;color:var(--text);border:1px solid var(--border)}
.pbtn:hover{background:color-mix(in srgb,var(--text) 7%,transparent);border-color:var(--accent)}
.pbtn.pri{background:var(--accent-fill);color:var(--accent-contrast);border-color:var(--accent-fill);font-size:13px;padding:7px}
.pbtn.pri:hover{background:var(--accent-fill-hover)}
.plist{display:grid}
.plist>div{display:flex;justify-content:space-between;gap:10px;padding:7px 0;border-bottom:1px solid var(--border)}
.plist .v{color:var(--accent-700);text-align:right}
.ptags{display:flex;flex-wrap:wrap;gap:6px}
.ptag{font-size:11px;letter-spacing:.02em;padding:3px 10px;border:1px solid var(--accent);color:var(--accent)}
.ptag.ok{border-color:transparent;background:var(--accent-100);color:var(--accent-800)}

@media(max-width:1100px){#panel{position:absolute;right:0;top:0;height:100%;z-index:6;box-shadow:var(--shadow-md)}}
@media(max-width:820px){
  #side{position:absolute;z-index:5;height:100%;box-shadow:var(--shadow-md)}
  #side.hidden{margin-left:-270px}
  .row.me .body{max-width:92%}
  #hello{padding-top:6vh}
}
</style></head><body>

<aside id=side>
  <div class=side-top><button id=new onclick=newChat()>＋ New chat</button></div>
  <div class=side-label>Chats</div>
  <div id=convs></div>
  <div class=side-foot><a href="/" class=home>__LOGO__<span>kAIm56</span></a></div>
</aside>

<div id=main>
  <header>
    <button class=icon title="Sidebar" onclick="document.getElementById('side').classList.toggle('hidden')">☰</button>
    <select id=agent onchange=pickAgent()></select>
    <span id=state><span class="dot"></span><span id=stateTxt>…</span></span>
    <span class=grow></span>
    <button class=icon id=searchBtn title="Search in this chat" onclick=searchToggle()><svg width=18 height=18 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.6 stroke-linecap=round stroke-linejoin=round><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg></button>
    <button class=icon id=gwBtn title="Security Gateway" onclick=gwToggle()><svg width=18 height=18 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.6 stroke-linecap=round stroke-linejoin=round><path d="M12 2l8 4v6c0 5-3.4 8.6-8 10-4.6-1.4-8-5-8-10V6z"/></svg></button>
    <span id=gwCount class=gwcount></span>
    <button class=icon id=termBtn title="Browser terminal" onclick=openTerm()><svg width=18 height=18 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.6 stroke-linecap=round stroke-linejoin=round><rect x=2 y=3 width=20 height=14 rx=2/><path d="M8 21h8M12 17v4"/></svg></button>
    <button class=icon id=panelBtn title="Session panel" onclick=panelToggle()><svg width=18 height=18 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.6 stroke-linecap=round stroke-linejoin=round><rect x="3" y="5" width="18" height="14"/><path d="M15 5v14"/></svg></button>
    <button class=icon title="Restart agent (resets the agent session)" onclick=restartAgent()><svg width=18 height=18 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.6 stroke-linecap=round stroke-linejoin=round><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg></button>
  </header>

  <div id=searchbar>
    <svg width=15 height=15 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.6 stroke-linecap=round stroke-linejoin=round><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
    <input id=sq placeholder="Search in this chat… (Enter next · Shift+Enter previous · Esc close)" autocomplete=off>
    <span id=smatch></span>
    <button class=sbtn title="Previous" onclick="searchStep(-1)"><svg width=13 height=13 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.6><path d="m18 15-6-6-6 6"/></svg></button>
    <button class=sbtn title="Next" onclick="searchStep(1)"><svg width=13 height=13 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.6><path d="m6 9 6 6 6-6"/></svg></button>
    <button class="sbtn plain" title="Close" onclick="searchToggle(false)"><svg width=14 height=14 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.6><path d="M18 6 6 18M6 6l12 12"/></svg></button>
  </div>

  <div id=log><div class=wrap id=msgs></div></div>

  <div id=comp>
    <div id=branchbar style="max-width:760px;margin:0 auto 6px;display:none">
      <span class=bb-label>⑂ Aside active (depth <span id=bdepth>1</span>) — replies run in the inherited context</span>
      <button class=bb-btn onclick=backBranch()>↩ back to main thread</button>
    </div>
    <div id=slashhint style="max-width:760px;margin:0 auto 6px;display:none"></div>
    <div id=box class=blueprint>
      <div id=thumbs></div>
      <div class=inrow>
        <button class=icon id=clipBtn title="Attach an image (vision) or a document (PDF/DOCX/text — the extracted text goes to the agent)" onclick="document.getElementById('file').click()"></button>
        <button class=icon id=branchBtn title="Open an aside: ask a follow-up without polluting the main thread (↩ brings you back)" onclick=openBranch()><svg width="17" height="17" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><rect x="5.5" y="1.5" width="5" height="4"/><rect x="1" y="10.5" width="5" height="4"/><rect x="10" y="10.5" width="5" height="4"/><path d="M8 5.5v2.5M8 8H3.5v2.5M8 8h4.5v2.5"/></svg></button>
        <button class=icon id=micBtn title="Speak (tap again = done)" onclick=micToggle()>🎙</button>
        <input type=file id=file accept="image/*,.pdf,.docx,.odt,.txt,.md,.csv,.html" hidden onchange=addAttachment(this)>
        <textarea id=t rows=1 placeholder="Message the agent…" autofocus></textarea>
        <button id=send onclick=send(true) title="Send · during a reply: ■ = stop (Enter with text = interject)">➤</button>
      </div>
    </div>
    <div class=foot id=foot></div>
  </div>
</div>

<aside id=panel class=hidden>
  <div class=phead><span class=pk>Session</span><button class="sbtn plain" title="Close" onclick="panelToggle(false)"><svg width=14 height=14 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.6><path d="M18 6 6 18M6 6l12 12"/></svg></button></div>
  <div id=pbody><div class=text-muted style="font-size:12px">…</div></div>
</aside>

<script>
// Inline SVGs instead of colour emoji: those depend on an emoji font and
// show up as grey boxes (tofu) without one. SVG renders everywhere.
const _S='<svg width=18 height=18 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.6 stroke-linecap=round stroke-linejoin=round>';
const IC={
  clip:_S+'<path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>',
  term:_S+'<rect x=2 y=3 width=20 height=14 rx=2/><path d="M8 21h8M12 17v4"/></svg>',
  refresh:_S+'<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>',
  bot:_S+'<rect x=4 y=9 width=16 height=11 rx=2/><path d="M12 9V5M9 3h6"/><circle cx=9 cy=14 r=1.2 fill=currentColor stroke=none/><circle cx=15 cy=14 r=1.2 fill=currentColor stroke=none/></svg>',
};
const AGENTS=__AGENTS__, START=__CURRENT__;
const $=id=>document.getElementById(id);
const KEY='fc-chat-convs';
const TKEY='fc-chat-tombs';
let TOMBS={};
function loadTombs(){try{TOMBS=JSON.parse(localStorage.getItem(TKEY)||'{}')||{}}catch(e){TOMBS={}}}
function saveTombs(){try{localStorage.setItem(TKEY,JSON.stringify(TOMBS))}catch(e){}}
let convs=[], cur=null, agent='', img=null, ctrl=null;

/* ---------- Persistence ---------- */
function load(){
  try{convs=JSON.parse(localStorage.getItem(KEY))||[]}catch(e){convs=[]}
  convs.forEach(c=>(c.msgs||[]).forEach(m=>{delete m.busy}));  /* aborted stream */
}
function save(){try{localStorage.setItem(KEY,JSON.stringify(convs.slice(0,200)))}catch(e){}pushShared();}

/* ---------- Sync with /api/chats (shared store with the app) ----------
   The app stores chats as {id,instance,title,updatedAt,mode,messages:[{user,text}]},
   this web UI as {id,agent,title,ts,msgs:[{role,content}]}. Map the two formats
   onto each other here, merge by updatedAt/ts — so app and web see the same
   history. */
function toShared(list){return list.map(c=>({
  id:String(c.id), title:c.title||'', mode:'server', instance:c.agent||'',
  updatedAt:c.ts||0,
  messages:(c.msgs||[]).filter(m=>!m.busy).map(m=>({user:m.role==='user', text:m.content||'', image:m.image, branch:m.branch||0}))
}));}
function fromShared(list){return (list||[]).map(c=>({
  id:String(c.id), agent:c.instance||'', title:c.title||'', ts:c.updatedAt||0,
  msgs:(c.messages||[]).map(m=>({role:m.user?'user':'assistant', content:m.text||'', image:m.image, branch:m.branch||0}))
}));}
let _pushT=null;
function pushShared(delay){
  clearTimeout(_pushT);
  _pushT=setTimeout(()=>{fetch('/api/chats',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({chats:toShared(convs),tombstones:TOMBS})}).catch(()=>{});},
    delay===undefined?400:delay);
}
let CHATS_REV=0;
/* Merge remote into the local set. true = something changed locally.
   The currently streaming chat is left untouched, else the partial text is lost. */
/* Apply incoming delete tombstones: remove locally tombstoned chats
   (unless newer than the deletion) and adopt the markers. */
function applyTombs(tombs){
  if(!tombs)return;
  let changed=false;
  for(const id in tombs){
    const dat=tombs[id]|0;
    if(!TOMBS[id]||TOMBS[id]<dat)TOMBS[id]=dat;
    const l=convs.find(c=>c.id===id);
    if(l&&(l.ts||0)<=dat){
      convs=convs.filter(c=>c.id!==id);
      if(cur&&cur.id===id)cur=null;
      changed=true;
    }
  }
  saveTombs();
  if(changed){try{localStorage.setItem(KEY,JSON.stringify(convs.slice(0,200)))}catch(e){}drawConvs();draw();}
}
function applyRemote(remote){
  const byId={}; convs.forEach(c=>byId[c.id]=c);
  let changed=false;
  remote.forEach(r=>{
    if(ctrl&&cur&&r.id===cur.id)return;
    if(TOMBS[r.id]&&(r.ts||0)<=TOMBS[r.id])return;   // tombstoned -> do not resurrect
    const l=byId[r.id];
    if(!l){byId[r.id]=r;changed=true;return;}
    if((r.ts||0)<=(l.ts||0))return;
    /* Only APPEND messages, never replace: otherwise a state from the other
       side wipes a just-typed, not-yet-pushed question. Only safe when the
       local list is a prefix of the remote one. */
    const lm=l.msgs||[], rm=r.msgs||[];
    /* Is the local history a PREFIX of the remote one? Then appending is safe. */
    let prefix=rm.length>=lm.length;
    if(prefix)for(let i=0;i<lm.length;i++)
      if(lm[i].role!==rm[i].role||lm[i].content!==rm[i].content){prefix=false;break;}
    if(prefix){
      if(rm.length>lm.length){lm.push(...rm.slice(lm.length));l.msgs=lm;changed=true;}
    }else if(rm.length>=lm.length){
      /* DIVERGENCE (no prefix), but remote is newer (r.ts>l.ts, checked above)
         and not shorter -> adopt the server state as the merge point, instead of
         letting the sync stall forever. Own not-yet-pushed changes would already
         have been pushed by this point. */
      l.msgs=rm.slice();changed=true;
    }else return;   /* local is longer -> keep it, our own push reconciles */
    if(r.title&&l.title!==r.title){l.title=r.title;changed=true;}
    l.ts=r.ts;
  });
  if(!changed)return false;
  convs=Object.values(byId).sort((a,b)=>(b.ts||0)-(a.ts||0));
  try{localStorage.setItem(KEY,JSON.stringify(convs.slice(0,200)))}catch(e){}
  drawConvs();
  // Even if cur is the SAME object (messages were mutated in), redraw —
  // otherwise the open device does not see live-appended messages.
  if(cur){const f=convs.find(c=>c.id===cur.id); if(f){cur=f;draw();}}
  return true;
}
/* Once on load: fetch, then push up our own (local-only) chats — after that
   a push on each local change is enough. */
async function syncChats(){
  try{
    const d=await (await fetch('/api/chats?since=0&wait=0')).json();
    CHATS_REV=d.rev||0;
    applyTombs(d.tombstones);
    applyRemote(fromShared(d.chats||[]));
  }catch(e){}
  pushShared(0);
}
/* Long-poll: the manager responds as soon as app OR web writes — so new
   messages from the other side show up here within a fraction of a second. */
async function chatSyncLoop(){
  for(;;){
    try{
      const d=await (await fetch('/api/chats?wait=25&since='+CHATS_REV)).json();
      if(typeof d.rev==='number')CHATS_REV=d.rev;
      applyTombs(d.tombstones);
      if(d.chats)applyRemote(fromShared(d.chats));
    }catch(e){ await new Promise(r=>setTimeout(r,3000)); }
  }
}

/* ---------- Markdown (small, no third-party code) ---------- */
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
function md(src){
  const lines=esc(src).split('\n'), out=[];
  const H=/^(#{1,6}) +(.*)$/, UL=/^\s*[-*+] +/, OL=/^\s*\d+[.)] +/, FENCE=/^\s*```/;
  const inline=s=>s
    .replace(/`([^`]+)`/g,'<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>')
    .replace(/(^|[^*\w])\*([^\s*][^*\n]*?)\*(?![\w*])/g,'$1<i>$2</i>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s"'<>&]+)\)/g,'<a href="$2" target=_blank rel=noopener>$1</a>');
  let i=0;
  while(i<lines.length){
    const l=lines[i];
    if(FENCE.test(l)){                       /* code block (may still be open in the stream) */
      const buf=[];i++;
      while(i<lines.length&&!FENCE.test(lines[i]))buf.push(lines[i++]);
      i++;
      out.push('<pre><button class=cp onclick=copyCode(this)>Copy</button><code>'+buf.join('\n')+'</code></pre>');
      continue;
    }
    const h=l.match(H);
    if(h){const n=Math.min(h[1].length,3);out.push('<h'+n+'>'+inline(h[2])+'</h'+n+'>');i++;continue}
    if(UL.test(l)){const b=[];
      while(i<lines.length&&UL.test(lines[i]))b.push(inline(lines[i++].replace(UL,'')));
      out.push('<ul><li>'+b.join('</li><li>')+'</li></ul>');continue}
    if(OL.test(l)){const b=[];
      while(i<lines.length&&OL.test(lines[i]))b.push(inline(lines[i++].replace(OL,'')));
      out.push('<ol><li>'+b.join('</li><li>')+'</li></ol>');continue}
    if(!l.trim()){i++;continue}
    const b=[];                              /* paragraph until blank line / block start */
    while(i<lines.length&&lines[i].trim()&&!FENCE.test(lines[i])&&!H.test(lines[i])
          &&!UL.test(lines[i])&&!OL.test(lines[i]))b.push(inline(lines[i++]));
    out.push('<p>'+b.join('<br>')+'</p>');
  }
  return out.join('');
}
function copyCode(b){navigator.clipboard.writeText(b.parentNode.querySelector('code').textContent);
  const o=b.textContent;b.textContent='✓';setTimeout(()=>b.textContent=o,1200)}
function copyMsg(b,i){navigator.clipboard.writeText(splitThink(cur.msgs[i].content).ans.trim());
  const o=b.textContent;b.textContent='✓ copied';setTimeout(()=>b.textContent=o,1200)}

/* ---------- Sidebar ---------- */
function drawConvs(){
  $('convs').innerHTML=convs.map(c=>
    `<div class="conv${cur&&c.id===cur.id?' sel':''}" onclick="openChat('${c.id}')">`+
    `<span class=t>${esc(c.title||'New chat')}</span>`+
    `<span class=ag>${esc(c.agent)}</span>`+
    `<button class=x title="Delete" onclick="delChat(event,'${c.id}')">✕</button></div>`).join('')
    ||'<div class=side-foot style="border:none">No chats yet.</div>';
}
function newChat(){cur=null;draw();drawConvs();branchUi();$('t').focus()}
function openChat(id){cur=convs.find(c=>c.id===id)||null;
  if(innerWidth<820)$('side').classList.add('hidden');
  if(cur){agent=cur.agent;$('agent').value=agent;refreshState()}
  if(cur&&cur.abranch===undefined)cur.abranch=(cur.msgs.length?(cur.msgs[cur.msgs.length-1].branch||0):0);
  draw();drawConvs();branchUi()}
function delChat(e,id){e.stopPropagation();
  TOMBS[id]=Date.now();saveTombs();
  convs=convs.filter(c=>c.id!==id);if(cur&&cur.id===id)cur=null;save();draw();drawConvs()}

/* ---------- Messages ---------- */
function draw(){
  const m=$('msgs');
  if(!cur||!cur.msgs.length){
    m.innerHTML=`<div id=hello><div class="panel blueprint">`+
      ``+
      `<div class=kicker>microVM agent</div>`+
      `<h2>What can ${esc(agent||'the agent')} help with?</h2>`+
      `<p>Runs in its own microVM. The agent keeps its context across chats.</p>`+
      `<div class=chips>`+
      ['What is in my workspace?','Summarise the latest changes','Which tools do you have?']
        .map(s=>`<button onclick="suggest(this)">${esc(s)}</button>`).join('')+`</div></div></div>`;
    return;
  }
  const row=(x,i)=>{
    const pic=x.image?`<img src="data:image/jpeg;base64,${x.image}" alt="">`:'';
    if(x.role==='user')
      return `<div class="row me"><div class=body>${pic}${esc(x.content).replace(/\n/g,'<br>')}`+
        `<div class="tools tools-me"><button onclick="copyMsg(this,${i})">Copy</button></div></div></div>`;
    const busy=x.busy?'<span class=cursor></span>':'';
    const tools=x.busy?'':`<div class=tools><button onclick="copyMsg(this,${i})">Copy</button>`+
      `<button onclick="speakMsg(${i})">Read aloud</button></div>`;
    return `<div class=row><div class=av>${IC.bot}</div><div class=body>${botHtml(x.content,false)}${busy}${tools}</div></div>`;
  };
  /* Branches: contiguous messages with branch>0 become one collapsible block
     — open only while the branch is still active. */
  let html='',i=0;
  while(i<cur.msgs.length){
    const b=cur.msgs[i].branch||0;
    if(!b){html+=row(cur.msgs[i],i);i++;continue}
    const start=i;let n=0;
    let seg='';
    while(i<cur.msgs.length&&(cur.msgs[i].branch||0)>0){seg+=row(cur.msgs[i],i);i++;n++}
    const live=(i>=cur.msgs.length)&&(cur.abranch||0)>0;
    html+=`<details class=ast ${live?'open':''}><summary>⑂ Aside · ${n} messages</summary>${seg}</details>`;
  }
  m.innerHTML=html;
  scroll();
  searchApply();
}
function splitThink(s){
  const A='⟦think⟧', B='⟦/think⟧'; let think='',ans='',open=false,i=0;
  for(;;){ const a=s.indexOf(A,i); if(a<0){ans+=s.slice(i);break;}
    ans+=s.slice(i,a); const b=s.indexOf(B,a+A.length);
    if(b<0){think+=s.slice(a+A.length);open=true;break;}
    think+=s.slice(a+A.length,b); i=b+B.length; }
  return {think:think.trim(), ans, open};
}
/* Tool status lines ("🔧 web_search …" plus heartbeat dots) go into a
   collapsed "Tools" block instead of the answer text. */
function splitTools(s){
  const tools=[],rest=[];
  for(const line of s.split('\n')){
    const t=line.trimStart();
    if(t.startsWith('🔧')){const n=t.slice(2).trim().replace(/[…·. ]+$/,'').trim();if(n)tools.push(n);}
    else rest.push(line);
  }
  return {tools,rest:rest.join('\n').replace(/^\n+|\n+$/g,'')};
}
function botHtml(content,cursor){
  const t=splitThink(content); let h='';
  if(t.think) h+=`<details class=think ${t.open?'open':''}><summary>💡 Thinking${t.open?' …':''}</summary><div class=thinkbody>${esc(t.think).replace(/\n/g,'<br>')}</div></details>`;
  const tl=splitTools(t.ans);
  if(tl.tools.length) h+=`<details class=think><summary>🔧 Tools · ${tl.tools.length}${cursor?' · '+esc(tl.tools[tl.tools.length-1]):''}</summary><div class=thinkbody>${tl.tools.map(x=>'🔧 '+esc(x)).join('<br>')}</div></details>`;
  h+=md(tl.rest)+(cursor?'<span class=cursor></span>':'');
  return h;
}
function scroll(){const l=$('log');l.scrollTop=l.scrollHeight}
function atBottom(){const l=$('log');return l.scrollHeight-l.scrollTop-l.clientHeight<80}
let pend=false;
function paint(){                      /* streaming: only update the last block */
  if(pend)return; pend=true;
  requestAnimationFrame(()=>{
    pend=false;
    const rows=$('msgs').querySelectorAll('.row');
    const b=rows.length?rows[rows.length-1].querySelector('.body'):null;
    if(!b)return draw();
    const stick=atBottom();
    /* If the user collapses "thinking" during the stream, the next repaint must
       not reopen it: remember and restore the state. */
    const d0=b.querySelector('details.think');
    const keepOpen=d0?d0.open:null;
    b.innerHTML=botHtml(cur.msgs[cur.msgs.length-1].content,true);
    if(keepOpen!==null){const d1=b.querySelector('details.think');if(d1)d1.open=keepOpen;}
    if(stick)scroll();
    if(SEARCH.open)searchApply();
  });
}
function suggest(b){$('t').value=b.textContent;$('t').focus();autogrow()}

/* ---------- Agents ---------- */
function drawAgents(){
  $('agent').innerHTML=AGENTS.map(a=>`<option value="${esc(a.name)}">${esc(a.name)}</option>`).join('')
    ||'<option value="">no web instance</option>';
  agent=(AGENTS.find(a=>a.name===START)||AGENTS[0]||{}).name||'';
  $('agent').value=agent;
}
/* Switching the instance switches the chat: the newest chat of that
   instance opens, or an empty one — a chat never changes its agent. */
function pickAgent(){agent=$('agent').value;
  const c=convs.filter(x=>x.agent===agent).sort((x,y)=>(y.ts||0)-(x.ts||0))[0];
  if(c){openChat(c.id);return}
  cur=null;refreshState();draw();drawConvs();branchUi();$('t').focus()}
async function refreshState(){
  let a=null;
  try{a=(await (await fetch('/api/instances')).json()).find(i=>i.name===agent)}catch(e){}
  const on=!!(a&&a.running);
  document.querySelector('#state .dot').className='dot'+(on?' on':'');
  $('stateTxt').textContent=on?'running':'off — starts on the first prompt';
  $('foot').textContent=`The agent keeps its own session — a new chat here does not reset it (restart button above).`;
  panelLoad();
}

/* ---- Session panel (design: "Agent chat — session panel + inline search") ----
   What the agent is and sees: runtime, uptime, login, its MCP servers with
   their secret state, the platform services. Open by default on wide
   screens; the choice is remembered. */
const PKEY='fc-chat-panel';
let PANEL={open:null,data:null,agent:''};
function panelWanted(){
  if(PANEL.open!==null)return PANEL.open;
  try{const v=localStorage.getItem(PKEY);if(v!==null)return v==='1'}catch(e){}
  return innerWidth>=1100;
}
function panelToggle(force){
  PANEL.open=(typeof force==='boolean')?force:!panelWanted();
  try{localStorage.setItem(PKEY,PANEL.open?'1':'0')}catch(e){}
  panelPaint();
  if(PANEL.open)panelLoad();
}
function fmtUp(s){if(!s)return '—';const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h?`${h}h ${m}m`:`${m}m`}
async function panelLoad(){
  if(!panelWanted()||!agent)return;
  try{PANEL.data=await (await fetch('/api/session/'+encodeURIComponent(agent))).json();PANEL.agent=agent}catch(e){PANEL.data=null}
  panelPaint();
}
function panelPaint(){
  const open=panelWanted();
  $('panel').classList.toggle('hidden',!open);
  $('panelBtn').classList.toggle('act',open);
  if(!open)return;
  const d=PANEL.data;
  if(!d||d.error){$('pbody').innerHTML='<div class=text-muted style="font-size:12px">no session data</div>';return;}
  const cell=(k,v)=>`<div><div class=k>${esc(k)}</div><div class=v>${esc(v)}</div></div>`;
  const plat=(d.platform||[]).map(p=>`<div><span>${esc(p.name)}</span><span class=v>${esc(p.state)}</span></div>`).join('');
  const mcps=(d.mcps||[]);
  const need=d.need_secret||0;
  $('pbody').innerHTML=
    `<div class="pcard blueprint">`+
    `<div class=phead><span class=pname>${esc(d.name)}</span><span class="pstate${d.running?' on':''}"><span class=sq></span>${d.running?'running':'off'}${d.stale?' · stale image':''}</span></div>`+
    `<div class=pgrid>${cell('Runtime',d.runtime)}${cell('Uptime',d.running?fmtUp(d.uptime):'—')}${cell('Login',d.login)}`+
    (d.model?cell('Model',d.model):'')+`</div>`+
    `<div class=pbtns><button class=pbtn onclick=restartAgent()>Restart</button><button class=pbtn onclick=openLogs()>Logs</button><button class=pbtn onclick=openTerm()>Terminal</button></div></div>`+
    `<div class=pk>Platform · kAIm56</div><div class=plist>${plat}</div>`+
    `<div class=pk>MCP servers${mcps.length?(need?` · ${need} need a secret`:' · all ready'):''}</div>`+
    (mcps.length?`<div class=ptags>${mcps.map(m=>`<span class="ptag${m.ready?' ok':''}" title="${esc(m.ready?'ready':'missing: '+m.missing.join(', '))}">${esc(m.name)}</span>`).join('')}</div>`
      :`<div class=text-muted style="font-size:12px">none assigned</div>`)+
    (need?`<a class="pbtn pri" style="text-align:center;text-decoration:none" href="/#secrets">Release secrets in the manager</a>`:'');
}
function openLogs(){if(agent)window.open('/api/session/'+encodeURIComponent(agent)+'/log','_blank')}

/* ---- Inline search over the current chat ----
   Header icon opens the bar; every message that matches stays bright, the
   rest dims; Enter / Shift+Enter walk the hits, Esc closes. The highlight is
   applied to text nodes after rendering, so Markdown stays intact. */
let SEARCH={open:false,q:'',hits:[],cur:0};
function searchToggle(force){
  SEARCH.open=(typeof force==='boolean')?force:!SEARCH.open;
  $('searchbar').classList.toggle('open',SEARCH.open);
  $('searchBtn').classList.toggle('act',SEARCH.open);
  if(SEARCH.open){$('sq').focus();$('sq').select();}
  else{SEARCH.q='';$('sq').value='';SEARCH.hits=[];SEARCH.cur=0;}
  searchApply();
}
function searchStep(d){if(!SEARCH.hits.length)return;SEARCH.cur=(SEARCH.cur+d+SEARCH.hits.length)%SEARCH.hits.length;searchApply();}
function searchMark(el,q){
  const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT,null);
  const nodes=[];let n;while((n=walker.nextNode()))nodes.push(n);
  const ql=q.toLowerCase();
  for(const t of nodes){
    const v=t.nodeValue;const lv=v.toLowerCase();let i=lv.indexOf(ql);if(i<0)continue;
    const frag=document.createDocumentFragment();let last=0;
    while(i>=0){frag.appendChild(document.createTextNode(v.slice(last,i)));
      const m=document.createElement('mark');m.className='sh';m.textContent=v.slice(i,i+q.length);frag.appendChild(m);
      last=i+q.length;i=lv.indexOf(ql,last);}
    frag.appendChild(document.createTextNode(v.slice(last)));
    t.parentNode.replaceChild(frag,t);
  }
}
function searchApply(){
  const rows=[...$('msgs').querySelectorAll('.row')];
  rows.forEach(r=>{r.classList.remove('dim','hit','cur');
    r.querySelectorAll('mark.sh').forEach(m=>m.replaceWith(document.createTextNode(m.textContent)));r.normalize();});
  const q=SEARCH.open?SEARCH.q.trim():'';
  if(!q){$('smatch').textContent='';return;}
  const ql=q.toLowerCase();
  SEARCH.hits=rows.map((r,i)=>r.querySelector('.body')&&r.querySelector('.body').textContent.toLowerCase().includes(ql)?i:-1).filter(i=>i>=0);
  if(SEARCH.cur>=SEARCH.hits.length)SEARCH.cur=0;
  rows.forEach((r,i)=>{
    if(SEARCH.hits.includes(i)){r.classList.add('hit');searchMark(r.querySelector('.body'),q);}else r.classList.add('dim');
  });
  $('smatch').textContent=SEARCH.hits.length?`${SEARCH.cur+1} / ${SEARCH.hits.length}`:'no match';
  if(SEARCH.hits.length){const r=rows[SEARCH.hits[SEARCH.cur]];r.classList.add('cur');
    const d=r.closest('details.ast');if(d)d.open=true;
    r.scrollIntoView({block:'center',behavior:'smooth'});}
}
$('sq').addEventListener('input',()=>{SEARCH.q=$('sq').value;SEARCH.cur=0;searchApply();});
$('sq').addEventListener('keydown',e=>{
  if(e.key==='Enter'){e.preventDefault();searchStep(e.shiftKey?-1:1);}
  else if(e.key==='Escape'){e.preventDefault();searchToggle(false);$('t').focus();}
});
document.addEventListener('keydown',e=>{
  if((e.ctrlKey||e.metaKey)&&e.key==='f'&&cur&&cur.msgs.length){e.preventDefault();searchToggle(true);}
});
/* ---- Security Gateway ----
   Per chat, not per agent: the same agent can process foreign text in one
   chat (gateway on) and its own notes in the next (off). The state lives on
   the manager, so app and web see the same reply. */
let GW={chats:{},stats:{},available:false};
async function gwLoad(){
  try{GW=await (await fetch('/api/gateway')).json()}catch(e){}
  gwPaint();
}
function gwPaint(){
  const b=$('gwBtn'),c=$('gwCount');
  if(!b)return;
  const on=!!(cur&&GW.chats&&GW.chats[cur.id]);
  b.classList.toggle('on',on);
  b.disabled=!cur||!GW.available;
  const s=(cur&&GW.stats&&GW.stats[cur.id])||{};
  const chars=(s.in||0)+(s.out||0);
  c.textContent=on&&(chars||s.img)?`${chars}⌫${s.img?' · '+s.img+'🖼':''}`:'';
  b.title=!GW.available?'Security Gateway unavailable (text_unicode.py missing)'
    :on?`Security Gateway ON — invisible characters stripped both ways, image metadata removed. Removed so far: ${chars} characters${s.img?', '+s.img+' image metadata blocks':''}.`
       :'Security Gateway OFF — click to filter invisible characters and image metadata for this chat';
}
async function gwToggle(){
  if(!cur||!GW.available)return;
  const on=!GW.chats[cur.id];
  if(on)GW.chats[cur.id]=true;else delete GW.chats[cur.id];
  gwPaint();
  await fetch('/api/gateway',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({chat:cur.id,on})}).catch(()=>{});
  gwLoad();
}
function openTerm(){if(agent)window.open('/i/'+encodeURIComponent(agent)+'/term/','_blank')}
async function restartAgent(){
  if(!agent||!confirm(`Restart ${agent}? The running agent session is lost.`))return;
  $('stateTxt').textContent='restarting…';
  await fetch(`/api/instances/${encodeURIComponent(agent)}/stop`,{method:'POST'});
  await fetch(`/api/instances/${encodeURIComponent(agent)}/start`,{method:'POST'});
  setTimeout(refreshState,3000);
}

/* ---------- Attachments ---------- */
/* Images go to the model as vision input. Everything else goes to the
   manager's /api/extract; what travels into the chat is the extracted TEXT
   (the model never sees the binary). doc = {name, text, note}. */
let doc=null, docOpen=false;
function addAttachment(inp){
  const f=inp.files&&inp.files[0];inp.value='';
  if(!f)return;
  if((f.type||'').startsWith('image/')){
    const r=new FileReader();
    r.onload=()=>{img=String(r.result).split(',')[1]||null;drawThumb()};
    r.readAsDataURL(f);
    return;
  }
  fetch('/api/extract?name='+encodeURIComponent(f.name),{method:'POST',body:f})
    .then(r=>r.json())
    .then(d=>{
      if(d.error){alert('Extraction failed: '+d.error);return;}
      doc={name:d.name,text:d.text,note:d.note||''};docOpen=false;drawThumb();
    })
    .catch(e=>alert('Extraction failed: '+e));
}
function drawThumb(){
  let h='';
  if(img)h+=`<div class=th><img src="data:image/jpeg;base64,${img}" alt="">`+
    `<button class=x onclick="img=null;drawThumb()">✕</button></div>`;
  if(doc){
    const esc=t=>t.replace(/&/g,'&amp;').replace(/</g,'&lt;');
    h+=`<div class="th doc" onclick="docOpen=!docOpen;drawThumb()">`+
      `<span>📄 <b>${esc(doc.name)}</b> <small>${doc.text.length} characters`+
      `${doc.note?' · '+esc(doc.note):''}${docOpen?'':' · click to preview'}</small></span>`+
      (docOpen?`<pre>${esc(doc.text.slice(0,4000))}${doc.text.length>4000?'\n…':''}</pre>`:'')+
      `</div><div class=th><button class=x onclick="event.stopPropagation();doc=null;drawThumb()">✕</button></div>`;
  }
  $('thumbs').innerHTML=h;
}

/* ---------- Voice ----------
   Recording in the browser, recognition and output in the manager. What was
   asked by voice is also read aloud — typed questions are not, otherwise it
   reads out long explanations unprompted. */
let REC=null, CHUNKS=[], VOICE_IN=false, AUDIO=null;
async function micToggle(){
  const b=$('micBtn');
  if(REC&&REC.state==='recording'){REC.stop();return;}
  let stream;
  try{ stream=await navigator.mediaDevices.getUserMedia({audio:true}); }
  catch(e){ $('foot').textContent='No microphone: '+(e&&e.name||e); return; }
  CHUNKS=[]; REC=new MediaRecorder(stream);
  REC.ondataavailable=e=>{ if(e.data&&e.data.size)CHUNKS.push(e.data); };
  REC.onstop=async()=>{
    stream.getTracks().forEach(t=>t.stop());
    b.classList.remove('rec'); b.disabled=true;
    const blob=new Blob(CHUNKS,{type:(REC.mimeType||'audio/webm')});
    try{
      const r=await fetch('/api/stt',{method:'POST',
        headers:{'Content-Type':blob.type},body:blob});
      const d=await r.json();
      if(d.text&&d.text.trim()){
        $('t').value=d.text.trim(); autogrow();
        VOICE_IN=true; send();                 /* hands-free: send directly */
      } else {
        $('foot').textContent='Nothing understood'+(d.error?': '+d.error:'');
      }
    }catch(e){ $('foot').textContent='Recognition failed'; }
    b.disabled=false;
  };
  REC.start(); b.classList.add('rec');
}
async function speakText(text){
  if(!text||!text.trim())return;
  try{
    const r=await fetch('/api/tts',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
    if(!r.ok)return;
    const url=URL.createObjectURL(await r.blob());
    if(AUDIO){ AUDIO.pause(); URL.revokeObjectURL(AUDIO.src); }
    AUDIO=new Audio(url); AUDIO.play().catch(()=>{});
  }catch(e){}
}
function speakMsg(i){ if(cur&&cur.msgs[i])speakText(splitThink(cur.msgs[i].content).ans); }

/* ---------- Branches (tree chat) ---------- */
function branchUi(){
  const d=(cur&&cur.abranch)||0;
  $('branchbar').style.display=d>0?'flex':'none';
  $('bdepth').textContent=d;
  $('branchBtn').classList.toggle('on',d>0);
}
function openBranch(){
  if(ctrl||!agent)return;
  const t=$('t').value.trim();
  const d=((cur&&cur.abranch)||0)+1;
  /* send() creates a new chat itself if needed — in the callback cur exists. */
  sendRaw('/aside'+(t?' '+t:''),d,d,()=>{
    cur.abranch=d;branchUi();
    if(t){$('t').value='';autogrow()}
  });
}
function backBranch(){
  if(ctrl||!cur||!(cur.abranch>0))return;
  const d=cur.abranch;
  sendRaw('/back',d,Math.max(0,d-1),()=>{cur.abranch=Math.max(0,d-1);branchUi()});
}

/* ---------- Steering: interject into the running agent ---------- */
async function steer(text){
  $('t').value='';autogrow();
  cur.msgs.splice(cur.msgs.length-1,0,{role:'user',content:text});  /* before the running reply */
  draw();
  try{
    const d=await (await fetch('/i/'+encodeURIComponent(agent)+'/api/steer',
      {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text})})).json();
    if(!d.queued) $('foot').textContent='No running turn anymore — please send the message normally.';
  }catch(e){ $('foot').textContent='Steering failed: '+e; }
}

/* ---------- Send ---------- */
const SLASH_BUILTIN=[
  ['/model','Switch model (e.g. /model orcarouter:anthropic/claude-sonnet-4.6)'],
  ['/reasoning','Toggle reasoning (low·medium·high·off)'],
  ['/steps','Max tool steps per turn: /steps 30 or /steps unlimited'],
  ['/goal','Set a goal — replies are refined against a judge'],
  ['/reset','Reset the context'],
  ['/fresh','One-off request in a throwaway context (history untouched)'],
  ['/aside','Open an aside — a follow-up beside the main thread (folds back into a note)'],
  ['/back','Close the aside and summarise it as a note (/back drop = discard)'],
];
let SLASH_PROMPTS=[],_spTs=0;
async function slashPrompts(){
  if(Date.now()-_spTs>30000){_spTs=Date.now();
    try{SLASH_PROMPTS=((await (await fetch('/api/prompts')).json()).prompts||[])
      .map(p=>['/'+p.name,p.text.slice(0,70),'tpl']);}catch(e){}}
  return SLASH_PROMPTS;
}
let SL_HITS=[],SL_SEL=-1,SL_DISMISS=false;
function slOpen(){return $('slashhint').style.display==='block'&&SL_HITS.length>0}
function slClose(){$('slashhint').style.display='none';SL_HITS=[];SL_SEL=-1}
function slRender(){
  const hd=`<div class=slhead>Commands \u00b7 \u2191\u2193 select \u00b7 \u21b5 apply \u00b7 Esc closes</div>`;
  $('slashhint').innerHTML=hd+SL_HITS.map((h,i)=>
    `<div class="slrow${i===SL_SEL?' slsel':''}" onmousedown="event.preventDefault()" `+
    `onclick="pickSlash('${esc(h[0])}')"><code>${esc(h[0])}</code>`+
    `<span>${esc(h[1])}</span>${h[2]==='tpl'?`<em class=sltag>template</em>`:``}</div>`).join('');
}
function slMove(d){
  if(!SL_HITS.length)return;
  SL_SEL=(SL_SEL+d+SL_HITS.length)%SL_HITS.length; slRender();
  const rows=$('slashhint').querySelectorAll('.slrow'); if(rows[SL_SEL])rows[SL_SEL].scrollIntoView({block:'nearest'});
}
async function slashHint(){
  const el=$('slashhint'),v=$('t').value;
  if(!v.startsWith('/')||v.includes(' ')&&!v.startsWith('/model ')){SL_DISMISS=false;slClose();return}
  if(SL_DISMISS)return;            // closed via Esc -> stay closed until the token changes
  const all=SLASH_BUILTIN.concat(await slashPrompts());
  SL_HITS=all.filter(x=>x[0].startsWith(v.split(' ')[0])).slice(0,10);
  if(!SL_HITS.length){slClose();return}
  SL_SEL=0; slRender(); el.style.display='block';
}
function pickSlash(c){$('t').value=c+' ';$('t').focus();slClose();autogrow()}
function autogrow(){const t=$('t');t.style.height='auto';t.style.height=Math.min(t.scrollHeight,180)+'px'}
$('t').addEventListener('input',()=>{autogrow();slashHint();});
$('t').addEventListener('keydown',e=>{
  // Esc ALWAYS closes the picker (even if SL_HITS should be empty) and keeps
  // it closed while typing continues on the same /-command.
  if(e.key==='Escape'&&$('slashhint').style.display==='block'){
    e.preventDefault();SL_DISMISS=true;slClose();return;}
  if(slOpen()){
    if(e.key==='ArrowDown'){e.preventDefault();slMove(1);return;}
    if(e.key==='ArrowUp'){e.preventDefault();slMove(-1);return;}
    if((e.key==='Enter'||e.key==='Tab')&&!e.shiftKey&&!e.isComposing&&SL_SEL>=0){
      e.preventDefault();pickSlash(SL_HITS[SL_SEL][0]);return;}
  }
  if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();send();}
});

let RAW=null;   /* {text,u,r,cb} — set by sendRaw() (branch commands) */
function sendRaw(text,u,r,cb){ if(ctrl)return; RAW={text,u,r,cb}; send(); }
async function send(fromButton){
  if(ctrl){
    const t=$('t').value.trim();
    if(!fromButton&&t){ return steer(t); }   /* Enter with text: interject */
    ctrl.abort();return;                      /* button (■) aborts */
  }
  const raw=RAW; RAW=null;
  const typed=raw?raw.text:$('t').value.trim();
  if(!typed&&!img&&!doc)return;
  const text=doc
    ?`[Attached document: ${doc.name}]\n${doc.text}\n[End of document]\n\n`+
      (typed||'Please read the attached document and summarize it.')
    :typed;
  const shown=doc?('📄 '+doc.name+(typed?'\n'+typed:'')):typed;
  if(!agent)return alert('No instance with TRANSPORT=web available.');
  if(!cur){cur={id:String(Date.now()),agent:agent,title:(typed||(doc?doc.name:'Image')).slice(0,42),ts:Date.now(),msgs:[]};
    convs.unshift(cur)}
  const tagU=raw?raw.u:((cur.abranch||0));
  const tagR=raw?raw.r:((cur.abranch||0));
  cur.msgs.push({role:'user',content:shown,image:img||undefined,branch:tagU||undefined});
  const reply={role:'assistant',content:'',busy:true,branch:tagR||undefined};
  cur.msgs.push(reply);
  if(raw&&raw.cb)raw.cb();
  const payload={message:text,chat:cur.id}; if(img)payload.image=img;
  img=null;doc=null;docOpen=false;drawThumb();if(!raw){$('t').value='';autogrow();}
  cur.ts=Date.now();save();draw();drawConvs();
  ctrl=new AbortController();
  $('send').textContent='■';$('send').classList.add('stop');
  try{
    const r=await fetch('/api/chat/'+encodeURIComponent(agent),
      {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),signal:ctrl.signal});
    const rd=r.body.getReader(),dec=new TextDecoder();
    for(;;){
      const {done,value}=await rd.read();
      if(done)break;
      reply.content+=dec.decode(value,{stream:true});
      paint();
    }
  }catch(e){
    reply.content+=(e&&e.name==='AbortError')?'\n\n_(aborted)_':'\n\n⚠️ '+e;
  }finally{
    ctrl=null;reply.busy=false;
    if(!reply.content)reply.content='_(empty reply)_';
    if(VOICE_IN){ VOICE_IN=false; speakText(splitThink(reply.content).ans); }
    $('send').textContent='➤';$('send').classList.remove('stop');
    if(text.trim&&String(text).trim()==='/reset'&&cur)cur.abranch=0;
    save();draw();refreshState();gwLoad();branchUi();
  }
}

/* ---------- Start ---------- */
// Fill the static icon buttons once (composer + header) — SVG instead of
// emoji, so no grey boxes appear without an emoji font.
$('termBtn').innerHTML=IC.term;
$('clipBtn').innerHTML=IC.clip;
[...document.querySelectorAll('.icon')].forEach(b=>{
  if(b.title&&b.title.includes('Restart agent'))b.innerHTML=IC.refresh;
});
load();loadTombs();drawAgents();
/* Fetch the shared server store first (await!), THEN open the instance's
   existing chat — otherwise a second device with empty localStorage starts a
   new thread and the history looks "out of sync". Also for the notification
   click (?i=<agent>): prefer the task chat, otherwise the most recent one. */
(async()=>{
  await syncChats();
  const last=convs.find(c=>c.id==='task-'+agent)
    ||convs.filter(c=>c.agent===agent).sort((a,b)=>(b.ts||0)-(a.ts||0))[0];
  if(last&&!cur)openChat(last.id); else if(!cur){draw();drawConvs()}
  chatSyncLoop();
})();
panelPaint();refreshState();gwLoad();setInterval(refreshState,15000);
</script></body></html>"""


def render(agents, current="", logo=""):
    """agents: list of {name, running, description} (instances with TRANSPORT=web)."""
    import json
    return (PAGE.replace("__LOGO__", logo)
                .replace("__AGENTS__", json.dumps(agents, ensure_ascii=False))
                .replace("__CURRENT__", json.dumps(current)))
