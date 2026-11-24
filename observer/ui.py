PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>CSTV Observer</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1115;--card:#181b22;--line:#262a33;--text:#e7e9ee;--dim:#9097a3;--ok:#3ecf8e;--bad:#f0616d;--warn:#f2b84b;--acc:#5b8cff;--ct:#7ab0ff;--t:#f0b060}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 "Segoe UI",system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:28px 20px 60px}
header{display:flex;align-items:center;gap:14px;margin-bottom:22px}
h1{font-size:22px;margin:0;font-weight:600}.sub{color:var(--dim);font-size:13px}
.spacer{flex:1}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:16px}
.card h2{font-size:15px;margin:0 0 12px;font-weight:600;color:var(--dim);text-transform:uppercase;letter-spacing:.04em}
.step{display:flex;gap:14px;align-items:flex-start;padding:10px 0;border-top:1px solid var(--line)}
.step:first-of-type{border-top:0}
.dot{flex:none;width:26px;height:26px;border-radius:50%;display:grid;place-items:center;font-weight:700;font-size:14px;background:#2a2f3a;color:var(--dim)}
.dot.ok{background:rgba(62,207,142,.15);color:var(--ok)}.dot.bad{background:rgba(240,97,109,.15);color:var(--bad)}
.step .t{font-weight:600}.step .d{color:var(--dim);font-size:13.5px;margin-top:2px}
.step .act{margin-left:auto;flex:none}
button{font:inherit;border:0;border-radius:8px;padding:9px 16px;cursor:pointer;background:#2a3040;color:var(--text)}
button:hover{filter:brightness(1.15)}button.primary{background:var(--acc);color:#fff;font-weight:600}
button.big{padding:14px 26px;font-size:17px}button.danger{background:#3a2328;color:#ffb3ba}
.banner{padding:12px 16px;border-radius:10px;margin-bottom:16px;background:#1d2433;border:1px solid #2c3a57;display:none}
.live{display:flex;align-items:center;gap:22px;flex-wrap:wrap}
.state{font-size:26px;font-weight:700}.state.on{color:var(--ok)}.state.off{color:var(--warn)}
.oncam{font-size:20px;font-weight:600}.why{color:var(--dim)}
.kpis{display:flex;gap:26px;margin-top:14px;color:var(--dim);font-size:13px}.kpis b{display:block;color:var(--text);font-size:20px}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:6px 8px;text-align:left;border-top:1px solid var(--line)}
th{color:var(--dim);font-weight:500;border-top:0}tr.cam td{background:rgba(91,140,255,.12)}tr.dead{opacity:.35}
.CT{color:var(--ct)}.T{color:var(--t)}.bar{height:8px;border-radius:4px;background:var(--acc)}
.cuts{color:var(--dim);font-size:13.5px}.cuts div{padding:3px 0}
details summary{cursor:pointer;color:var(--dim)}label{display:block;margin:10px 0 4px;color:var(--dim);font-size:13px}
input[type=text],input[type=number]{background:#11141a;border:1px solid var(--line);color:var(--text);border-radius:8px;padding:8px 10px;width:100%;font:inherit}
.row{display:flex;gap:10px;align-items:center}.hidden{display:none}
code{background:#11141a;padding:2px 6px;border-radius:5px}
</style></head><body><main>
<header><div><h1>CSTV Observer</h1><div class="sub">Automatic camera for Counter-Strike 2 matches</div></div>
<div class="spacer"></div><button class="danger" onclick="act('quit').then(()=>document.body.innerHTML='<main><h1>CSTV Observer closed.</h1><p class=sub>You can close this tab.</p></main>')">Quit</button></header>

<div id="banner" class="banner"></div>

<div class="card" id="setupCard"><h2>Getting ready</h2>
 <div class="step"><div class="dot" id="s1">1</div><div><div class="t">Counter-Strike 2 is installed</div>
   <div class="d" id="s1d">Looking for CS2 through Steam…</div></div></div>
 <div class="step"><div class="dot" id="s2">2</div><div><div class="t">Game link file installed</div>
   <div class="d">Lets CS2 tell this app where every player is. Installed automatically.</div></div>
   <div class="act"><button id="b2" onclick="act('install_cfg')">Install again</button></div></div>
 <div class="step"><div class="dot" id="s3">3</div><div><div class="t">CS2 is running with camera control</div>
   <div class="d">Click <b>Start CS2</b>. Steam may ask to confirm the launch options. Click <b>OK</b>.
   If CS2 is already open, close it first and start it from here.</div></div>
   <div class="act"><button class="primary" onclick="act('launch_cs2')">Start CS2</button></div></div>
 <div class="step"><div class="dot" id="s4">4</div><div><div class="t">Watching a match</div>
   <div class="d">In CS2, open a match to watch: a CSTV/GOTV broadcast, a friend's server as spectator, or a demo (Watch → Your Matches).</div></div></div>
</div>

<div class="card"><h2>Live</h2>
 <div class="live">
  <div><div class="state" id="state">Waiting…</div><div class="why" id="stateWhy"></div></div>
  <div class="spacer"></div>
  <button class="big primary" id="toggle" onclick="toggle()">Pause</button>
 </div>
 <div style="margin-top:14px"><span class="why">On camera:</span> <span class="oncam" id="oncam">—</span>
   <div class="why" id="reason"></div></div>
 <div class="kpis"><div><b id="kSeen">0</b>kills shown</div><div><b id="kAll">0</b>kills total</div><div><b id="kCuts">0</b>camera cuts</div><div><b id="kMap">—</b>map / round</div></div>
</div>

<div class="card"><h2>Players</h2><table id="players"></table></div>
<div class="card"><h2>Recent camera cuts</h2><div class="cuts" id="cuts">None yet.</div></div>

<div class="card"><details><summary>Advanced settings</summary>
 <label>Camera control port (must match the launch option)</label><input type="number" id="netcon_port">
 <label class="row"><input type="checkbox" id="insecure" style="width:auto"> Add <code>-insecure</code> to CS2's launch options</label>
 <div class="sub">CS2 currently only accepts camera commands from other programs with this option. It turns off VAC for that session,
 so use it for watching only. Don't play matchmaking with this session.</div>
 <label>CS2 cfg folder (only if it wasn't found automatically)</label>
 <input type="text" id="cs2_cfg_dir" placeholder="…\steamapps\common\Counter-Strike Global Offensive\game\csgo\cfg">
 <div style="margin-top:12px"><button class="primary" onclick="save()">Save settings</button></div>
 <p class="sub" id="opts"></p>
</details></div>
</main>
<script>
let paused=false, settingsLoaded=false;
const $=id=>document.getElementById(id);
function dot(id,ok,n){const e=$(id);e.className='dot '+(ok?'ok':'bad');e.textContent=ok?'✓':n}
function banner(msg){const b=$('banner');if(msg){b.textContent=msg;b.style.display='block'}else b.style.display='none'}
async function act(a,extra){const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:a,...(extra||{})})});
 try{const j=await r.json();if(j.message)banner(j.message);return j}catch(e){}}
function toggle(){paused=!paused;act(paused?'pause':'resume');render()}
async function save(){await act('save_settings',{settings:{netcon_port:+$('netcon_port').value,insecure:$('insecure').checked,cs2_cfg_dir:$('cs2_cfg_dir').value}});settingsLoaded=false}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
let S={},T={};
function render(){
 dot('s1',S.cs2_found,1);$('s1d').textContent=S.cs2_found?('Found: '+S.cs2_dir):'Not found. If CS2 is installed, set its folder under Advanced settings.';
 dot('s2',S.cfg_installed,2);dot('s3',S.camera_control&&S.gsi_live,3);dot('s4',S.spectating,4);
 const ready=S.cs2_found&&S.cfg_installed&&S.camera_control&&S.spectating;
 $('setupCard').style.opacity=ready?.55:1;
 paused=!!T.paused;
 let st,why;
 if(!S.gsi_live){st='Waiting for CS2';why='Start CS2 and open a match to watch.'}
 else if(!S.camera_control){st='Can’t move the camera';why='Close CS2 and start it with the Start CS2 button above.'}
 else if(!S.spectating){st='Waiting for a match';why='Open a match or demo to watch in CS2.'}
 else if(paused){st='Paused';why='The camera is yours. Click Resume to turn the automatic camera back on.'}
 else{st='Automatic camera ON';why=T.phase==='freezetime'?'Freeze time: picking who to start on':''}
 $('state').textContent=st;$('state').className='state '+(S.spectating&&S.camera_control&&!paused?'on':'off');$('stateWhy').textContent=why;
 $('toggle').textContent=paused?'Resume':'Pause';
 const cam=(T.players||[]).find(p=>p.on_camera);$('oncam').textContent=cam?cam.name:'—';$('reason').textContent=T.reason?('Why: '+T.reason):'';
 const k=T.stats||{};$('kSeen').textContent=k.seen||0;$('kAll').textContent=k.kills||0;$('kCuts').textContent=k.cuts||0;
 $('kMap').textContent=T.map?(T.map.replace('de_','')+' / '+((T.round??0)+1)):'—';
 const ps=T.players||[];const mx=Math.max(1,...ps.map(p=>p.score));
 $('players').innerHTML='<tr><th>Player</th><th>HP</th><th>Action</th><th></th><th>Why</th></tr>'+ps.map(p=>
  `<tr class="${p.on_camera?'cam':''} ${p.alive?'':'dead'}"><td class="${p.team}">${p.on_camera?'🎥 ':''}${esc(p.name)}</td><td>${p.hp}</td><td style="width:140px"><div class="bar" style="width:${Math.round(130*p.score/mx)}px"></div></td><td>${p.score}</td><td class="why">${esc(p.reason)}</td></tr>`).join('');
 $('cuts').innerHTML=(T.cuts||[]).length?T.cuts.map(c=>`<div>→ <b>${esc(c.to)}</b> · ${esc(c.reason)}</div>`).join(''):'None yet.';
 if(!settingsLoaded&&S.settings){$('netcon_port').value=S.settings.netcon_port;$('insecure').checked=S.settings.insecure;$('cs2_cfg_dir').value=S.settings.cs2_cfg_dir||'';settingsLoaded=true}
 $('opts').innerHTML=S.launch_options?('CS2 is started with: <code>'+esc(S.launch_options)+'</code>'):'';
 if(S.message&&!$('banner').textContent)banner(S.message);
}
async function poll(){try{[S,T]=await Promise.all([fetch('/api/setup').then(r=>r.json()),fetch('/status').then(r=>r.json())]);render()}
 catch(e){$('state').textContent='App is not running';$('stateWhy').textContent='Start CSTV Observer again.'}}
setInterval(poll,700);poll();
</script></body></html>"""
