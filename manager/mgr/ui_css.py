# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Manager web UI — head and stylesheet."""

CSS = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>kAIm56</title>
<link rel=icon type="image/svg+xml" href="/logo.svg">
<style>
@import url('https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;700&family=Barlow+Condensed:wght@400;600&display=swap');
/* ── Industry — design-system tokens (claude.ai/design) ─────────────────── */
:root{
  color-scheme:light dark;   /* both themes are real: no browser auto-darkening on top */
  --color-bg:#f2f2f3; --color-surface:#e9e9ea; --color-text:#1d1f20;
  --color-accent:#5980a6; --color-accent-2:#728fab;
  --color-divider:color-mix(in srgb,#1d1f20 16%,transparent);
  --color-neutral-100:#f5f5f8;--color-neutral-200:#e7e7ea;--color-neutral-300:#d4d4d7;
  --color-neutral-400:#b7b7ba;--color-neutral-500:#98989b;--color-neutral-600:#7a7a7d;
  --color-neutral-700:#5d5d60;--color-neutral-800:#424244;--color-neutral-900:#2b2b2d;
  --color-accent-100:#eef6ff;--color-accent-200:#d6ebff;--color-accent-300:#b5d9fd;
  --color-accent-400:#94bce3;--color-accent-500:#749dc4;--color-accent-600:#597ea3;
  --color-accent-700:#416180;--color-accent-800:#2c455d;--color-accent-900:#1d2d3d;
  --font-heading:"Barlow Condensed",system-ui,sans-serif; --font-heading-weight:600;
  --font-body:"Barlow",system-ui,sans-serif;
  --font-mono:ui-monospace,SFMono-Regular,Menlo,monospace;
  --space-1:3.4px;--space-2:6.8px;--space-3:10.2px;--space-4:13.6px;--space-6:20.4px;--space-8:27.2px;
  --radius-sm:2px;--radius-md:4px;--radius-lg:7px;
  --shadow-sm:0 1px 2px color-mix(in srgb,#2b2b2d 14%,transparent);
  --shadow-md:0 3px 10px color-mix(in srgb,#2b2b2d 16%,transparent);
  --shadow-lg:0 12px 32px color-mix(in srgb,#2b2b2d 22%,transparent);
  --color-ok:#416180;
}
/* Dark rendering of the same system — same roles, ground flipped. */
@media(prefers-color-scheme:dark){:root{
  --color-bg:#141618; --color-surface:#1c1f22; --color-text:#e8e9ea;
  --color-accent:#94bce3; --color-accent-2:#9ebbd8;
  --color-divider:color-mix(in srgb,#e8e9ea 18%,transparent);
  --color-neutral-100:#212326;--color-neutral-200:#2b2d31;--color-neutral-300:#3a3d41;
  --color-neutral-400:#4e5155;--color-neutral-500:#6b6e73;--color-neutral-600:#8b8e93;
  --color-neutral-700:#a9acb1;--color-neutral-800:#c8cacd;--color-neutral-900:#e8e9ea;
  --color-accent-100:#1d2d3d;--color-accent-200:#2c455d;--color-accent-300:#416180;
  --color-accent-400:#597ea3;--color-accent-500:#749dc4;--color-accent-600:#94bce3;
  --color-accent-700:#b5d9fd;--color-accent-800:#d6ebff;--color-accent-900:#eef6ff;
  --color-ok:#b5d9fd;
}}
*,*::before,*::after{box-sizing:border-box}
body{margin:0;font-family:var(--font-body);font-size:15px;line-height:1.55;font-weight:400;
  background:var(--color-bg);color:var(--color-text);-webkit-font-smoothing:antialiased}
h1,h2,h3,h4,h5,h6{font-family:var(--font-heading);font-weight:var(--font-heading-weight);
  line-height:1.12;letter-spacing:-.015em;margin:0 0 var(--space-2)}
h1{font-size:42px}h2{font-size:32px}h3{font-size:25px}h4{font-size:20px}h5{font-size:16px}
h6{font-size:13px;letter-spacing:.08em;text-transform:uppercase}
p{margin:0 0 var(--space-3)}
a{color:var(--color-accent);text-underline-offset:3px}
a:hover{color:var(--color-accent-700)}
.text-muted{color:color-mix(in srgb,var(--color-text) 55%,transparent)}
:focus{outline:none}
:focus-visible{outline:2px solid var(--color-accent);outline-offset:2px}
::selection{background:color-mix(in srgb,var(--color-accent) 30%,transparent)}
code{font-family:var(--font-mono);font-size:.88em;background:var(--color-neutral-100);
  border:1px solid var(--color-divider);padding:.02rem .3rem}
/* — blueprint frame — */
.blueprint{position:relative;border:1px solid var(--color-divider);border-radius:0}
/* — buttons — */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;cursor:pointer;
  text-decoration:none;font-family:var(--font-heading);font-weight:var(--font-heading-weight);
  font-size:14px;line-height:1.2;color:var(--color-text);background:transparent;
  border:1px solid var(--color-divider);border-radius:0;
  padding:var(--space-2) calc(var(--space-3)*1.2)}
.btn svg{display:block}
.btn:disabled{opacity:.45;cursor:not-allowed}
/* Primary: a deep accent with white type in the light theme (6.7:1), the
   light accent with dark type in the dark one — small condensed labels
   need more contrast than the mid accent gives. */
.btn-primary{background:var(--color-accent-700);color:#fff;border-color:var(--color-accent-700)}
.btn-primary:hover{background:var(--color-accent-800);border-color:var(--color-accent-800)}
.btn-primary:active{background:var(--color-accent-900)}
@media(prefers-color-scheme:dark){
  .btn-primary{background:var(--color-accent);color:var(--color-bg);border-color:var(--color-accent)}
  .btn-primary:hover{background:var(--color-accent-700);border-color:var(--color-accent-700)}
  .btn-primary:active{background:var(--color-accent-800)}
  .actwin button.on{background:var(--color-accent);color:var(--color-bg)}
  .nbadge{background:var(--color-accent);color:var(--color-bg)}
}
.btn-secondary:hover{background:color-mix(in srgb,var(--color-text) 7%,transparent)}
.btn-secondary:active{background:color-mix(in srgb,var(--color-text) 14%,transparent)}
.btn-ghost{color:var(--color-accent);border-color:transparent;padding-inline:var(--space-1)}
.btn-ghost:hover{background:color-mix(in srgb,var(--color-accent) 10%,transparent)}
.btn-icon{width:36px;height:36px;padding:0}
.btn-sm{font-size:13px;padding:5px 10px}
/* — forms — */
.field>label{display:block;font-size:12px;margin-bottom:5px;
  color:color-mix(in srgb,var(--color-text) 70%,transparent)}
.input{width:100%;min-height:36px;padding:6px 10px;font:inherit;font-size:14px;
  color:var(--color-text);caret-color:var(--color-accent);background:var(--color-surface);
  border:1px solid var(--color-divider);border-radius:0}
.input:hover{border-color:color-mix(in srgb,var(--color-text) 45%,transparent)}
.input:focus-visible{border-color:var(--color-accent);outline-offset:0}
textarea.input{min-height:90px;resize:vertical;line-height:1.5}
.radio{display:inline-flex;align-items:center;gap:8px;cursor:pointer;font-size:14px}
.radio input{position:absolute;opacity:0;width:0;height:0;pointer-events:none}
.radio .dot{width:16px;height:16px;flex:none;border-radius:50%;border:1.5px solid var(--color-divider)}
.radio:hover .dot{border-color:var(--color-accent)}
.radio input:checked+.dot{border-color:var(--color-accent);background:var(--color-accent);
  box-shadow:inset 0 0 0 4px var(--color-bg)}
.radio input:focus-visible+.dot{outline:2px solid var(--color-accent);outline-offset:2px}
input[type=checkbox]{accent-color:var(--color-accent)}
/* — cards — */
.plugdrop{border:2px dashed var(--color-divider);border-radius:14px;padding:34px;text-align:center;color:var(--color-neutral-600);transition:.15s}
.plugdrop.drag{border-color:var(--color-accent);background:color-mix(in srgb,var(--color-accent) 7%,transparent);color:var(--color-accent)}
.pluglnk{color:var(--color-accent);cursor:pointer;text-decoration:underline}
.card{display:flex;flex-direction:column;gap:var(--space-2);padding:var(--space-3);
  border-radius:0;background:transparent;border:1px solid var(--color-divider)}
.card-title{font-family:var(--font-heading);font-weight:var(--font-heading-weight);
  font-size:17px;line-height:1.2}
.card-body{margin:0;font-size:13px;opacity:.8;flex:1}
/* — tags — */
.tag{display:inline-flex;align-items:center;font-size:11px;letter-spacing:.02em;
  padding:3px 10px;border-radius:0;white-space:nowrap}
.tag-accent{background:var(--color-accent-100);color:var(--color-accent-800)}
.tag-neutral{background:var(--color-neutral-100);color:var(--color-neutral-800)}
/* — tables — */
.table{width:100%;border-collapse:collapse;font-size:14px}
.table th{text-align:left;font-size:11px;letter-spacing:.08em;text-transform:uppercase;
  color:color-mix(in srgb,var(--color-text) 60%,transparent);
  padding:var(--space-2);border-bottom:1px solid var(--color-divider)}
.table td{padding:var(--space-2);border-bottom:1px solid color-mix(in srgb,var(--color-text) 8%,transparent);
  vertical-align:top}
.table tbody tr:hover{background:color-mix(in srgb,var(--color-text) 4%,transparent)}
/* — app shell — */
.shell{min-height:100vh;display:flex;flex-direction:column}
.topbar{border-bottom:1px solid var(--color-divider);background:var(--color-bg);
  position:sticky;top:0;z-index:10}
.topbar-in{max-width:1160px;margin:0 auto;padding:0 28px;display:flex;align-items:center;
  gap:32px;min-height:58px}
.brand{display:flex;align-items:center;gap:10px;margin-right:auto}
.brand b{line-height:1}
.brand .mark{flex:none;display:block}
.brand b{font-family:var(--font-heading);font-weight:600;font-size:19px;letter-spacing:.01em}
.brand span{font-size:11px;letter-spacing:.08em;text-transform:uppercase}
.appfoot{display:flex;align-items:center;gap:18px;padding:14px 28px;margin-top:24px;
  border-top:1px solid var(--color-divider);color:var(--color-neutral-600)}
.af-brand{font-family:var(--font-heading);font-weight:600;font-size:13px;letter-spacing:.02em}
.af-stat{font-size:12px;color:var(--color-neutral-600);white-space:nowrap}
.af-nav{display:flex;gap:18px;margin-left:auto}
.af-nav a{color:var(--color-neutral-600);text-decoration:none;font-size:13px}
.af-nav a:hover,.af-nav a[aria-current=page]{color:var(--color-accent)}
.nbell{position:relative;flex:none;margin-left:14px;width:38px;height:38px;display:flex;align-items:center;justify-content:center;border:1px solid var(--color-divider);border-radius:10px;background:var(--color-surface);color:var(--color-neutral-700);cursor:pointer}
.nbell:hover{border-color:var(--color-accent);color:var(--color-accent)}
.nbadge{position:absolute;top:-6px;right:-6px;min-width:17px;height:17px;padding:0 4px;border-radius:9px;background:var(--color-accent-700);color:#fff;font-size:11px;font-weight:700;line-height:17px;text-align:center}
::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-thumb{background:color-mix(in srgb,var(--color-text) 18%,transparent);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:color-mix(in srgb,var(--color-text) 32%,transparent)}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-corner{background:transparent}
.npanel{position:absolute;top:58px;right:16px;width:340px;max-width:calc(100vw - 32px);max-height:60vh;overflow:auto;background:var(--color-surface);border:1px solid var(--color-divider);border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.18);z-index:60}
.nhead{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-bottom:1px solid var(--color-divider);position:sticky;top:0;background:var(--color-surface)}
.nitem{padding:10px 14px;border-bottom:1px solid var(--color-divider)}
.nitem.unread{background:var(--color-neutral-100)}
.nitem[data-link]:hover{background:var(--color-neutral-200)}
.nitem .nt{font-weight:600;font-size:13.5px;display:flex;gap:8px;align-items:baseline}
.nitem .nb{font-size:13px;color:var(--color-neutral-700);margin-top:2px;white-space:pre-wrap;word-break:break-word}
.nitem .nm{font-size:11px;color:var(--color-neutral-500);margin-top:4px}
.actwin{display:inline-flex;border:1px solid var(--color-divider);border-radius:9px;overflow:hidden}
.actwin button{border:0;background:var(--color-surface);color:var(--color-neutral-700);font-size:12px;padding:5px 12px;cursor:pointer;border-right:1px solid var(--color-divider)}
.actwin button:last-child{border-right:0}
.actwin button:hover{color:var(--color-accent)}
.actwin button.on{background:var(--color-accent-700);color:#fff}
.seckey{-webkit-text-security:disc}
/* Fifteen tabs in one row: compact type and padding so they fit the 1160px
   shell; if a window is narrower still, the row scrolls with a visible thin
   bar instead of hiding the last tabs (Secrets, Settings fell off once). */
.tabs{display:flex;gap:2px;align-self:stretch;flex-wrap:nowrap;overflow-x:auto;overflow-y:hidden;scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tabs.more{-webkit-mask-image:linear-gradient(90deg,#000 calc(100% - 36px),transparent);mask-image:linear-gradient(90deg,#000 calc(100% - 36px),transparent)}
.tabs a{display:flex;align-items:center;padding:0 8px;font-size:12px;letter-spacing:.01em;
  text-decoration:none;color:var(--color-text);white-space:nowrap;
  border-bottom:2px solid transparent;margin-bottom:-1px}
.tabs a:hover{color:var(--color-accent-700)}
.tabs a[aria-current=page]{color:var(--color-accent-700);border-bottom-color:var(--color-accent)}
main{max-width:1160px;width:100%;margin:0 auto;padding:36px 28px 64px;flex:1}
.sec-head{display:flex;align-items:baseline;justify-content:space-between;gap:16px;margin-bottom:14px}
.sec-head h6{color:var(--color-accent);margin:0 0 2px}
.sec-head h3{margin:0}
.sec-head .note{font-size:12.5px;text-align:right;max-width:520px}
.banner{display:flex;align-items:center;gap:12px;padding:10px 14px;margin-bottom:28px;
  background:var(--color-accent-100)}
.banner span{font-size:13px;color:var(--color-accent-800)}
.panel{padding:24px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px 28px}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:22px}
.span2{grid-column:1/-1}
.panel-foot{display:flex;justify-content:flex-end;align-items:center;gap:12px;
  margin-top:24px;padding-top:18px;border-top:1px solid var(--color-divider)}
.msg{font-size:13px;color:var(--color-accent-700)}
.mono{font-family:var(--font-mono);font-size:13px}
.cmd{font-family:var(--font-mono);font-size:12px;background:var(--color-neutral-100);
  border:1px solid var(--color-divider);padding:10px 12px;white-space:pre-wrap;line-height:1.6;
  overflow-x:auto}
.acts{display:flex;gap:6px;justify-content:flex-end;align-items:center;flex-wrap:wrap}
footer{border-top:1px solid var(--color-divider)}
.foot-in{max-width:1160px;margin:0 auto;padding:14px 28px;display:flex;gap:24px;
  flex-wrap:wrap;font-size:12px}
.screen{display:none}.screen.on{display:block}
.fbrow{display:flex;align-items:center;gap:10px;padding:7px 8px;border-bottom:1px solid var(--color-divider);font-size:13.5px}
.fbrow:hover{background:var(--color-neutral-100)}
.fbrow.dir{cursor:pointer}
.fbico{width:1.2em;flex:none;text-align:center}
.fbn{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fbsharerow{cursor:pointer;border-radius:8px;padding:4px 8px;transition:background .12s ease}
.fbsharerow:hover{background:var(--color-neutral-100)}
.fbsharerow.sel{background:var(--color-neutral-100);box-shadow:inset 3px 0 0 var(--color-accent)}
.fbsz{color:var(--color-neutral-500);font-size:12px;font-variant-numeric:tabular-nums}
.fbact{font-size:12px;padding:2px 8px}
/* Architecture diagram. The rules live HERE and not as a <style> inside the SVG:
   a style element in inline SVG ends the SVG context during HTML parsing, and
   everything after it drops invisibly out of the image (Chrome; jsdom forgives it). */
#archsvg .bx{fill:var(--color-surface);stroke:var(--color-divider)}
#archsvg .bx2{fill:none;stroke:var(--color-accent)}
#archsvg .tt{fill:var(--color-text);font-size:13px;font-weight:600}
#archsvg .ss{fill:var(--color-neutral-600);font-size:11px}
#archsvg .ln{stroke:var(--color-neutral-500);stroke-width:1.2;marker-end:url(#arw);fill:none}
#archsvg .lb{fill:var(--color-neutral-600);font-size:10px}
.stack{display:flex;flex-direction:column;gap:18px}
.mrow{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0}
/* — dialog — */
.dialog-backdrop{position:fixed;inset:0;display:grid;place-items:center;padding:var(--space-4);
  background:color-mix(in srgb,var(--color-neutral-900) 50%,transparent);z-index:50}
.dialog{width:min(440px,100%);max-height:88vh;overflow-y:auto;overflow-x:hidden;display:flex;flex-direction:column;
  gap:var(--space-3);padding:var(--space-6);border-radius:0;background:var(--color-bg);
  border:1px solid var(--color-divider);box-shadow:var(--shadow-lg)}
.dialog-title{font-family:var(--font-heading);font-weight:var(--font-heading-weight);font-size:20px}
.dialog-body{font-size:13px;opacity:.85}
.dialog-actions{display:flex;justify-content:flex-end;align-items:center;gap:var(--space-2);
  margin-top:var(--space-2);padding-top:var(--space-3);border-top:1px solid var(--color-divider)}
/* — folder picker — */
.pklist{max-height:46vh;overflow:auto;border:1px solid var(--color-divider);
  background:var(--color-surface);padding:4px;display:flex;flex-direction:column}
.pkrow{justify-content:flex-start;width:100%;border-color:transparent;color:var(--color-text);
  font-family:var(--font-body);font-size:13.5px;padding:5px 8px;gap:8px}
.pkrow:hover{background:color-mix(in srgb,var(--color-accent) 12%,transparent)}
.pkquick{display:flex;gap:6px;flex-wrap:wrap}
.kv{display:flex;gap:12px;align-items:baseline;font-size:13.5px;padding:7px 0;
  border-bottom:1px solid color-mix(in srgb,var(--color-text) 8%,transparent)}
.kv:last-child{border-bottom:none}
.sev{display:inline-flex;align-items:center;font-size:10px;letter-spacing:.08em;
  text-transform:uppercase;padding:3px 8px;white-space:nowrap;flex:none}
.sev-high{background:var(--color-accent-700);color:var(--color-bg)}
.sev-medium{background:var(--color-accent-200);color:var(--color-accent-900)}
.sev-low{background:var(--color-neutral-200);color:var(--color-neutral-800)}
.issue{display:grid;grid-template-columns:78px 1fr auto;gap:12px;align-items:start;
  padding:12px 0;border-bottom:1px solid color-mix(in srgb,var(--color-text) 8%,transparent)}
.issue:last-child{border-bottom:none}
.issue.done{opacity:.5}
.issue h5{margin:0 0 3px;font-size:15px}
.issue .meta{font-size:11.5px;font-family:var(--font-mono)}
.issue p{margin:4px 0 0;font-size:13px}
.md h2{font-size:22px;margin:26px 0 8px}
.md h3{font-size:16px;margin:18px 0 6px;color:var(--color-accent-700)}
.md ul{margin:0 0 10px;padding-left:18px}
.md li{font-size:13.5px;margin:3px 0}
.md p{font-size:13.5px}
.kv b{font-family:var(--font-heading);font-weight:600;font-size:12px;letter-spacing:.06em;
  text-transform:uppercase;min-width:130px;color:color-mix(in srgb,var(--color-text) 60%,transparent)}
@media(max-width:860px){
  .grid2,.grid3{grid-template-columns:1fr}
  .topbar-in,main,.foot-in{padding-left:16px;padding-right:16px}
  .sec-head{flex-direction:column;align-items:flex-start}
  .sec-head .note{text-align:left}
  .table thead{display:none}
  .table,.table tbody,.table tr,.table td{display:block;width:100%}
  .table tr{border:1px solid var(--color-divider);margin:12px 0;padding:6px 4px}
  .table td{border:none;padding:6px 10px}
  .table td::before{content:attr(data-label);display:block;font-size:10px;
    letter-spacing:.08em;text-transform:uppercase;
    color:color-mix(in srgb,var(--color-text) 55%,transparent)}
  .acts{justify-content:flex-start}
}
</style>"""
