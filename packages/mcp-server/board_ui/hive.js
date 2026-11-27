/**
 * @fileoverview Swarm pane Overview and Members views.
 *
 * Renders the swarm goal, threads, running tasks, progress (Overview) and
 * the 3-tier member list (Members: Coordinator, Tier-2 Hermes workers, and
 * nested read-only Tier-3 subagents) from the ./api/swarm snapshot app.js
 * polls. Members are drawn as pixel bees. app.js calls
 * window.SwarmHive.update(snapshot, element, {view: 'overview' | 'cards'}).
 * Every string from the board or plan goes through esc().
 */
(function() {
  let S = {}, P = [];
  const state = {view: 'overview'};
  const $ = (s, el = document) => el.querySelector(s);
  const esc = s => String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const people = sc => Object.entries(sc.members).filter(([, x]) => !x.system);
  const ORDER = {needs_you: 0, working: 1, waiting_on: 2, parked: 3};

  /* ---------- pixel bees ---------- */
  const HUE = {coordinator:46};
  function hsl2hex(h,s,l){ h=((h%360)+360)%360; s/=100; l/=100; const k=n=>(n+h/30)%12; const a=s*Math.min(l,1-l); const f=n=>l-a*Math.max(-1,Math.min(k(n)-3,Math.min(9-k(n),1))); return '#'+[f(0),f(8),f(4)].map(x=>Math.round(x*255).toString(16).padStart(2,'0')).join(''); }
  const shiftHue=(h,target,amt)=>{ const d=((target-h+540)%360)-180; return h+d*amt; };
  function ramp(n){ const h=hueOf(n); return {
    spec: hsl2hex(shiftHue(h,55,0.28), 96, 90), hi: hsl2hex(shiftHue(h,55,0.18), 90, 74), mid: hsl2hex(h, 78, 60),
    sh: hsl2hex(shiftHue(h,265,0.22), 58, 44), ol: hsl2hex(shiftHue(h,265,0.35), 42, 19),
    skh: hsl2hex(shiftHue(h,265,0.15), 20, 30), sk: hsl2hex(shiftHue(h,265,0.3), 24, 18), skd: hsl2hex(shiftHue(h,265,0.45), 32, 10) }; }
  const tcol = n => hsl2hex(hueOf(n), 78, 60);
  const hueOf = n => HUE[n] ?? [12,150,262,330,196,228,96,28,290,175,350,60][hash(String(n))%12];
  const hash = s => [...s].reduce((a,c)=>(a*31+c.charCodeAt(0))>>>0, 7);
  const HEXD = (s => encodeURIComponent(s).replace(/'/g, '%27'))(`<svg xmlns='http://www.w3.org/2000/svg' width='28' height='24.25' viewBox='0 0 28 24.25'><path d='M14 0l12.12 7v14l-12.12 7L1.88 21V7z' fill='none' stroke='rgba(128,128,140,0.14)' stroke-width='1.2'/></svg>`);
  const tileTint = n => `color-mix(in srgb, ${tcol(n)} 22%, var(--sw-card, #16171b))`;
  const hexBG = n => n==='coordinator' ? `background:var(--sw-coord-bg, #1C1B22) url('data:image/svg+xml,${HEXD}')` : `background:${tileTint(n)}`;

  function drawBee(cv, name, beeState, frame){
    const W=22,H=18; cv.width=W; cv.height=H; const c=cv.getContext('2d'); c.imageSmoothingEnabled=false; c.clearRect(0,0,W,H);
    const R=ramp(name), isCoord=name==='coordinator', landed=(beeState==='parked'||beeState==='landed'), asleep=beeState==='parked';
    const P=(x,y,col)=>{ if(x<0||y<0||x>=W||y>=H) return; c.fillStyle=col; c.fillRect(x,y,1,1); };
    const cx=11, cy=10.5, rx=7.3, ry=4.8;
    const inside=(x,y)=>{ const nx=(x+0.5-cx)/rx, ny=(y+0.5-cy)/ry; return nx*nx+ny*ny<=1; };
    let L=[-0.42,-0.62,0.66]; const Ln=Math.hypot(...L); L=L.map(v=>v/Ln);
    const N4=[[1,0],[-1,0],[0,1],[0,-1]];
    const rows=(spec)=>{ const pts=[]; spec.forEach(([y,x0,x1])=>{ for(let x=x0;x<=x1;x++) pts.push([x,y]); }); return pts; };
    const drawWing=(pts, glint)=>{ const set=new Set(pts.map(p=>p.join(','))); pts.forEach(([x,y])=>{ const edge=N4.some(([dx,dy])=>!set.has((x+dx)+','+(y+dy))); P(x,y, edge?'rgba(140,160,192,0.9)':'rgba(255,255,255,0.72)'); });
      if(glint){ const tl=pts.filter(([x,y])=>!N4.some(([dx,dy])=>!set.has((x+dx)+','+(y+dy)))).sort((a,b)=>(a[1]-b[1])||(a[0]-b[0])); if(tl[0]) P(tl[0][0],tl[0][1],'#FFFFFF'); } };
    let back=null, front;
    if(landed){ front=rows([[5,14,17],[6,13,18]]); }
    else if(frame===0){ back=rows([[1,16,17],[2,16,18],[3,15,18],[4,15,17],[5,15,16]]); front=rows([[0,13,14],[1,12,15],[2,11,15],[3,11,14],[4,11,13],[5,11,12]]); }
    else { back=rows([[3,16,18],[4,15,19],[5,16,18]]); front=rows([[3,13,16],[4,12,17],[5,12,17],[6,13,15]]); }
    if(back) drawWing(back,false);
    const stripe=(x,y)=>{ const ny=(y+0.5-cy)/ry; const u=x+0.5+1.15*ny*ny; return (u>10.6&&u<12.5)||(u>14.3&&u<16.1)||(u>17.9); };
    for(let y=0;y<H;y++) for(let x=0;x<W;x++){
      if(!inside(x,y)) continue;
      const nx=(x+0.5-cx)/rx, ny=(y+0.5-cy)/ry, rr=Math.min(1,nx*nx+ny*ny), nz=Math.sqrt(1-rr);
      const I=nx*L[0]+ny*L[1]+nz*L[2];
      P(x,y, stripe(x,y) ? (I>0.80?R.skh : I>0.30?R.sk : R.skd) : (I>0.965?R.spec : I>0.78?R.hi : I>0.26?R.mid : R.sh));
    }
    for(let y=0;y<H;y++) for(let x=0;x<W;x++){
      if(inside(x,y)) continue;
      const nbs=N4.filter(([dx,dy])=>inside(x+dx,y+dy)); if(!nbs.length) continue;
      const nx=(x+0.5-cx)/rx, ny=(y+0.5-cy)/ry, n=Math.hypot(nx,ny); const d=(nx/n)*L[0]+(ny/n)*L[1];
      const nextToStripe=nbs.some(([dx,dy])=>stripe(x+dx,y+dy));
      P(x,y, (d>0.5 && !nextToStripe) ? R.sh : R.ol);
    }
    // stinger
    P(19,10,R.ol); P(20,10,R.ol);
    // face
    if(asleep){ P(6,10,R.ol); P(7,10,R.ol); }
    else { P(6,9,'#FFFFFF'); P(7,9,R.skd); P(6,10,R.skd); P(7,10,R.skd); }
    P(5,11,'rgba(255,128,150,0.55)'); P(4,11,'rgba(255,128,150,0.35)');
    // antennae or coordinator crown
    if(isCoord){ const g1='#F7CE5B', g2='#FFE9A6', g3='#C98A2B'; [[5,5],[6,5],[7,5],[8,5],[9,5]].forEach(([x,y])=>P(x,y,g1)); P(5,4,g2); P(7,4,g2); P(9,4,g2); P(7,3,g2); P(5,5,g3); P(9,5,g3); P(7,5,'#E4566A'); }
    else { [[7,5],[6,4],[5,3]].forEach(([x,y])=>P(x,y,R.ol)); P(4,3,R.sk); [[9,5],[9,4],[9,3]].forEach(([x,y])=>P(x,y,R.ol)); P(8,2,R.sk); }
    drawWing(front,true);
    if(asleep){ const z='#8C90A6'; [[0,0],[1,0],[2,0],[3,0],[2,1],[1,2],[0,3],[1,3],[2,3],[3,3]].forEach(([x,y])=>P(x,y,z)); }
  }
  const spriteTag = (n, beeState) => `<span class="beebox" data-state="${esc(beeState||'working')}"><span class="beewrap" data-bee="${esc(n)}" data-state="${esc(beeState||'working')}"><canvas></canvas><canvas></canvas></span><i class="gsh"></i></span>`;
  function paintSprites(el){ el.querySelectorAll('.beewrap').forEach(w=>{ const [a,b]=w.querySelectorAll('canvas'); drawBee(a, w.dataset.bee, w.dataset.state, 0); drawBee(b, w.dataset.bee, w.dataset.state, 1); }); }

  const beeImgs = new Map();
  function beeImg(n){
    if(!beeImgs.has(n)){ const cv=document.createElement('canvas'); drawBee(cv, n, 'landed', 0); beeImgs.set(n, cv.toDataURL()); }
    return beeImgs.get(n);
  }

  function snapBees(root){
    const dpr=window.devicePixelRatio||1;
    const boxes=[...root.querySelectorAll('.beebox')];
    const rects=boxes.map(b=>{ const r=b.getBoundingClientRect(); const m=/translate\(([-\d.]+)px,\s*([-\d.]+)px\)/.exec(b.style.transform||''); return {x:r.left-(m?+m[1]:0), y:r.top-(m?+m[2]:0)}; });
    boxes.forEach((b,i)=>{ const fx=(rects[i].x*dpr)%1, fy=(rects[i].y*dpr)%1; const dx=(fx>0.5?1-fx:-fx)/dpr, dy=(fy>0.5?1-fy:-fy)/dpr;
      b.style.transform = (Math.abs(dx)>0.01||Math.abs(dy)>0.01) ? `translate(${dx}px,${dy}px)` : ''; });
  }
  let snapQueued=false;
  const snapAll=()=>{ if(snapQueued) return; snapQueued=true; requestAnimationFrame(()=>{ snapQueued=false; document.querySelectorAll('.pane').forEach(snapBees); }); };
  window.addEventListener('scroll', snapAll, {passive:true, capture:true});
  window.addEventListener('resize', snapAll);

  const COPY_UI = { team:'The team', lead:'Coordinator · Antigravity', blocked:'Blocked', running:'Running', planned:'Planned next', done:'Done',
    more:'+{n} more', goal:'Goal', threads:'Threads', now:'Now', progress:'Progress', compare:'Compare with now', expand:'Open full timeline', none_blocked:'Nothing blocked', needs:'Needs attention', waiting:'Waiting on @coordinator', stuck:'Stuck', none_waiting:'Nothing waiting', none_stuck:'Nothing stuck', none_needs:'Nothing needs attention', none_running:'Nothing running', none_planned:'Nothing planned' };
  const ISSUE_KINDS = new Set(['failure','stuck','error']);
  const waitOn = n => `Waiting on @${n}`;
  function copy(){ const a=(window.SwarmHive||{}).viewCopy||{};
    return { ui:a.ui||{}, lead:a.lead||{}, members:a.members||{}, items:a.items||{} }; }
  function ui(k){ const c=copy().ui||{}; return c[k] || COPY_UI[k]; }
  function clipWords(s, n){ s=String(s||'').replace(/\s*\([^)]*\)/g,'').replace(/\s+/g,' ').trim(); const w=s.split(' '); return w.length>n ? w.slice(0,n).join(' ').replace(/[,;:.]$/,'')+'…' : s.replace(/\.$/,''); }
  function firstClause(s){ return String(s||'').split(/(?<=[.!?])\s+(?=[A-Z])|;\s|\s[–—-]\s/)[0]; }
  const stripDone = s => String(s||'').replace(/^\s*(?:\*\*|\*|`|_)?\[done\](?:\*\*|\*|`|_)?[\s:,.–—-]*/i,'');
  function itemCopy(id, title, line, full){ const c=(copy().items||{})[id]||{}; return { title: c.title || (full ? String(title||'') : clipWords(title, 14)), line: c.line || clipWords(firstClause(stripDone(line)), 12) }; }
  const liveText = (live, text) =>
    `<span class="animate-shimmer-text">${esc(text ?? live.text)}</span>`;
  const liveTip = live => `${live.kind==='thinking' ? 'Thinking' : 'Latest action'}: ${live.text}`;
  const fmtTok = n => {
    const v = Number(n) || 0;
    if (v < 1000) return String(Math.round(v));
    if (v < 100000) return `${(v / 1000).toFixed(1)}k`;
    if (v < 1000000) return `${Math.round(v / 1000)}k`;
    return `${(v / 1000000).toFixed(2)}M`;
  };
  const fmtTps = v => {
    const n = Number(v) || 0;
    return n < 10 ? `${n.toFixed(1)} t/s` : `${Math.round(n)} t/s`;
  };
  const fmtMs = ms => {
    const n = Number(ms) || 0;
    if (n < 1000) return `${Math.round(n)}ms`;
    if (n < 60000) return `${(n / 1000).toFixed(1)}s`;
    return `${Math.floor(n / 60000)}m${Math.round((n % 60000) / 1000)}s`;
  };
  const fmtCost = c => {
    if (c == null) return '';
    const n = Number(c);
    if (!Number.isFinite(n) || n <= 0) return '';
    return n < 0.01 ? `$${n.toFixed(3)}` : `$${n.toFixed(2)}`;
  };
  const memberTokDisplay = (tok) => {
    if (!tok || typeof tok !== 'object') return { val: 0, total: 0, cacheRead: 0, tip: '' };
    const newTok = Number(tok.new);
    const total = Number(tok.total) || 0;
    const cacheRead = Number(tok.cacheRead) || 0;
    const val = Number.isFinite(newTok) && newTok > 0 ? newTok : total;
    const tip = cacheRead > 0
      ? `${fmtTok(val)} new · ${fmtTok(cacheRead)} cached${total > val ? ` · ${fmtTok(total)} total` : ''}`
      : '';
    return { val, total, cacheRead, tip };
  };
  const fmtToolCall = t => {
    if (!t || !t.name) return '';
    const prev = String(t.preview || '').trim();
    return prev ? `${t.name}(${prev})` : String(t.name);
  };
  function memberLine(n, x){ if(x && x.live) return clipWords(firstClause(x.live.text), 8); const c=copy().members||{}; if(c[n]) return c[n]; if(n==='coordinator'){ const l=(copy().lead||{}).doing; if(l) return l; } return clipWords(firstClause(x.now||''), 6); }
  function cleanNote(s, fallback){
    let x = String(s||'').replace(/\s*\(#\d+(?:\s*,\s*#\d+)*\)\s*\.?$/,'').replace(/^\s+/,'').replace(/^(?:\*\*|\*|`|_)?\[done\](?:\*\*|\*|`|_)?[\s:,.–—-]*/i,'').replace(/\*\*|__|`/g,'').replace(/\s+/g,' ').trim();
    if(x) x = x.charAt(0).toUpperCase() + x.slice(1);
    return x || String(fallback||'').trim();
  }

  /* ---------- Shared bee badges, compact ring, Running card, and Done disclosure ---------- */
  function workerBadges(assignees, max, sc){
    const lim = max || 2;
    const list = (assignees||[]).slice(0, lim);
    const extra = Math.max(0, (assignees||[]).length - list.length);
    return `<span class="o2-wchips">${list.map(n=>{
      const m = sc && sc.members && sc.members[n];
      const st = (sc && !sc.finished && m && !m.killed && m.status==='working') ? 'working' : 'landed';
      return `<span class="o2-wchip bee" title="@${esc(n)}">${spriteTag(n, st)}<span>${esc(n)}</span></span>`;
    }).join('')}${extra>0?`<span class="o2-wchip more">+${extra}</span>`:''}</span>`;
  }

  function taskProgressPill(w){
    if(w && w.progress && w.progress.total>0){
      const pct = Math.min(100, Math.max(0, Math.round((w.progress.done / w.progress.total) * 100)));
      const lbl = w.progress.label || `${w.progress.done}/${w.progress.total}`;
      return `<span class="o2-tprog" title="${esc(lbl)} done"><span class="o2-tpbar"><i style="width:${pct}%"></i></span><span>${esc(lbl)}</span></span>`;
    }
    if(w && w.rawState==='review') return `<span class="o2-tprog rev">In review</span>`;
    return '';
  }

  function ringSvg(tot){
    const sum = tot.done + tot.running + tot.blocked + tot.queued;
    if(sum > 0 && tot.done === sum){
      return `<span class="o2-ring full" aria-hidden="true">✓</span>`;
    }
    const total = sum || 1;
    const C = 43.98;
    let off = 0;
    const segs = ['done','running','blocked'].map(k=>{
      if(!tot[k]) return '';
      const len = (tot[k] / total) * C;
      const s = `<circle cx="9" cy="9" r="7" fill="none" class="seg ${k}" stroke-width="2.2" stroke-dasharray="${len.toFixed(2)} ${C.toFixed(2)}" stroke-dashoffset="${(-off).toFixed(2)}"/>`;
      off += len;
      return s;
    }).join('');
    return `<svg class="o2-ring" width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><circle cx="9" cy="9" r="7" fill="none" class="trk" stroke-width="2.2"/>${segs}</svg>`;
  }

  function miniBar(n){
    return `<span class="o2-mbar" aria-hidden="true">${['done','running','blocked','queued'].map(k=>n[k]?`<i class="${k}" style="flex:${n[k]}"></i>`:'').join('')}</span>`;
  }

  function runningCard(sc, runningWs, summaryText, goalTitle, goalLine){
    if(!runningWs.length || sc.finished) return '';
    let sum = sc.runningSummary || '';
    if(!sum && sc.headline && sc.headline !== goalTitle && sc.headline !== goalLine) sum = sc.headline;
    if(!sum && !sc.fromPlan) sum = summaryText || '';
    const lim = state.o2runAll ? runningWs.length : 8;
    const vis = runningWs.slice(0, lim);
    const more = runningWs.length - vis.length;
    const ph = sc.phase && sc.phase.total > 1 ? `<span class="o2-runph">${esc(sc.phase.label)} · ${sc.phase.index}/${sc.phase.total}</span>` : '';
    const rows = vis.map(({w, st})=>{
      const c = itemCopy(w.id, w.title, w.note);
      const note = cleanNote(w.note, c.line);
      const activeMem = st==='running' ? (w.assignees||[]).map(n=>sc.members[n]).find(m=>m && !m.killed && m.status==='working') : null;
      const lm = st==='running' ? (w.assignees||[]).map(n=>sc.members[n]).find(m=>m && !m.killed && m.live) : null;
      const telMem = activeMem || lm || (w.assignees||[]).map(n=>sc.members[n]).find(m=>m && !m.killed && ((m.tokens?.new > 0 || m.tokens?.total > 0) || m.tps > 0));
      const telTok = memberTokDisplay(telMem?.tokens);
      const telParts = [];
      if (telTok.val > 0) telParts.push(`${fmtTok(telTok.val)} tok`);
      if (telMem?.tps > 0) telParts.push(fmtTps(telMem.tps));
      const telHtml = telParts.length ? `<span class="o2-runtel" data-testid="swarm-running-telemetry"${telTok.tip ? ` title="${esc(telTok.tip)}"` : ''}>${esc(telParts.join(' · '))}</span>` : '';
      const activeToolLabel = activeMem?.currentTool?.name ? `Running ${fmtToolCall(activeMem.currentTool)}` : '';
      const liveObj = activeToolLabel ? { kind: 'tool', text: activeToolLabel } : lm?.live;
      const extra = liveObj && note && note.toLowerCase() !== liveObj.text.toLowerCase() ? `\n${note}` : '';
      const noteHtml = liveObj
        ? `<p class="o2-runnote o2-live" data-testid="swarm-running-activity" title="${esc(liveTip(liveObj) + extra)}">${liveText(liveObj)}</p>`
        : (note ? `<p class="o2-runnote" title="${esc(w.note||note)}">${esc(note)}</p>` : '');
      return `<li class="o2-runrow ${st}">
        <span class="o2-ti ${st}" aria-label="${st}"></span>
        <div class="o2-runmain">
          <div class="o2-runtop">
            <b class="o2-runt">${esc(c.title)}</b>
            ${telHtml}
            ${taskProgressPill(w)}
            ${workerBadges(w.assignees, 2, sc)}
          </div>
          ${noteHtml}
        </div>
      </li>`;
    }).join('');
    return `<section class="o2-run">
      <div class="o2-runh"><span class="o2-rundot" aria-hidden="true"></span><b>${ui('running')}</b><em>${runningWs.length}</em>${ph}</div>
      ${sum ? `<p class="o2-runsum">${esc(sum)}</p>` : ''}
      <ul class="o2-runlist">${rows}</ul>
      ${more>0 ? `<button class="o2-more" data-o2="runAll">${ui('more').replace('{n}', more)}</button>` : ''}
    </section>`;
  }

  function waitChips(waits, stuck){
    const out = [...waits.entries()].sort((a,b)=>b[1]-a[1]).slice(0,2)
      .map(([n,c])=>`<span class="o2-pchip warn"><i></i>${c} ${esc(waitOn(n).replace(/^W/,'w'))}</span>`);
    if(stuck > 0) out.push(`<span class="o2-pchip bad"><i></i>${stuck} stuck</span>`);
    return out;
  }
  const TSTATE = { done:'done', active:'running', review:'running', blocked:'blocked', failed:'blocked', queued:'queued' };
  const stOfWs = (sc, w) => { if(w.state!=='done' && w.state!=='blocked'){ const m=sc.members&&sc.members[w.assignees[0]]; if(m && m.status==='waiting_on') return 'blocked'; } return TSTATE[w.state]||'queued'; };

  function progressCard(sc, tot, doneWs, queuedWs, openThreads, openAsks, extraChips){
    const totalCount = tot.done + tot.running + tot.blocked + tot.queued;
    const open = !!state.o2doneOpen;
    const revCount = (sc.workstreams||[]).filter(w=>w.rawState==='review' && stOfWs(sc, w)!=='blocked' && (!state.o2who || (w.assignees||[]).includes(state.o2who))).length;
    const chips = [];
    if(openAsks > 0) chips.push(`<span class="o2-pchip warn"><i></i>${openAsks} need${openAsks===1?'s':''} attention</span>`);
    chips.push(...(extraChips || []));
    if(revCount > 0) chips.push(`<span class="o2-pchip info"><i></i>${revCount} in review</span>`);
    if(openThreads) chips.push(`<span class="o2-pchip">${openThreads} thread${openThreads===1?'':'s'} open</span>`);
    else if(tot.running - revCount > 0) chips.push(`<span class="o2-pchip info"><i></i>${tot.running - revCount} active</span>`);
    const doneOrdered = doneWs.slice().sort((a,b)=>(b.seq||0)-(a.seq||0));
    const lim = state.o2doneAll ? doneOrdered.length : 8;
    const visDone = doneOrdered.slice(0, lim);
    const more = doneOrdered.length - visDone.length;
    const doneBody = visDone.length
      ? `<ul class="o2-donelist">${visDone.map(w=>{
          const c = itemCopy(w.id, w.title, w.note);
          const sub = cleanNote(c.line || w.note, '');
          return `<li><span class="o2-ti done" aria-label="done">✓</span><div class="o2-donem"><b>${esc(c.title)}</b>${sub?`<span>${esc(sub)}</span>`:''}</div>${workerBadges(w.assignees, 2, sc)}</li>`;
        }).join('')}</ul>${more>0?`<button class="o2-more" data-o2="doneAll">${ui('more').replace('{n}', more)}</button>`:''}`
      : `<p class="o2-doneempty">No tasks completed yet.</p>`;
    const queuedBody = queuedWs.length
      ? `<div class="o2-queuedsub"><div class="o2-qhdr">Up next · ${queuedWs.length}</div><ul class="o2-donelist queued">${queuedWs.slice(0,6).map(w=>{
          const c = itemCopy(w.id, w.title, w.note);
          return `<li><span class="o2-ti queued" aria-label="queued"></span><div class="o2-donem"><b>${esc(c.title)}</b></div>${workerBadges(w.assignees, 2, sc)}</li>`;
        }).join('')}</ul></div>`
      : '';
    return `<section class="o2-prog ${open?'open':''}">
      <button type="button" class="o2-progh" data-o2="doneOpen" aria-expanded="${open}">
        ${ringSvg(tot)}
        <span class="o2-progt"><b>${tot.done} of ${totalCount}</b> tasks done</span>
        <span class="o2-progr">${chips.join('')}<span class="o2-chev" aria-hidden="true"></span></span>
      </button>
      ${open ? `<div class="o2-progb">${doneBody}${queuedBody}</div>` : ''}
    </section>`;
  }

  function reelCard(_sc){
    return '';
  }

  function threadModel(sc){
    const goal = sc.goal || null;
    const threads = (sc.threads || []).filter(t=>t && Array.isArray(t.workstreams));
    return threads.length ? {goal, threads} : null;
  }
  function threadsView(sc, TM){
    const wsById = new Map(sc.workstreams.map(w=>[w.id, w]));
    const attById = new Map(sc.attention.filter(a=>a.id).map(a=>[a.id, a]));
    const openYou = a => !sc.finished && a && a.wait==='you';
    const openAsk = a => !sc.finished && a && a.wait==='coordinator';
    const openIssue = a => !sc.finished && a && a.wait==='issue';
    if (state.o2who && (!sc.members[state.o2who] || sc.members[state.o2who].killed)) state.o2who = null;
    const who = state.o2who || null;
    const stOf = w => { if(w.state!=='done' && w.state!=='blocked'){ const m=sc.members[w.assignees[0]]; if(m && m.status==='waiting_on') return 'blocked'; } return TSTATE[w.state]||'queued'; };
    const waiterOf = w => { if(stOf(w)!=='blocked') return null; const m=sc.members[w.assignees[0]]; return (w.state==='blocked' && w.blocked_on) || (m && m.waiting_on) || null; };
    const all = []; const used = new Set();
    const runningWs = [], doneWs = [], queuedWs = [];
    const seenWs = new Set();
    const collectWs = w => {
      if(seenWs.has(w.id)) return;
      seenWs.add(w.id);
      const s = stOf(w);
      all.push(s);
      if(s==='running' || s==='blocked') runningWs.push({w, st:s});
      else if(s==='done') doneWs.push(w);
      else queuedWs.push(w);
    };
    const rows = TM.threads.map(t=>{
      const ws = t.workstreams.map(id=>wsById.get(id)).filter(Boolean).filter(w=>!who || w.assignees.includes(who));
      ws.forEach(w=>{ used.add(w.id); collectWs(w); });
      const att = (t.attention||[]).map(id=>attById.get(id)).filter(a=>!who || (a && a.from===who));
      const you = att.filter(openYou), asks = [...you, ...att.filter(openAsk)], issues = att.filter(openIssue);
      const n = {done:0, running:0, blocked:0, queued:0}; ws.forEach(w=>{ n[stOf(w)]++; });
      const waiters = new Set([...(asks.some(openAsk) ? ['coordinator'] : []), ...ws.map(waiterOf).filter(Boolean)]);
      const explicitState = t.state === 'done' ? 'done' : t.state === 'active' || t.state === 'running' ? 'running' : t.state === 'blocked' || t.state === 'waiting' ? 'waiting' : t.state === 'failed' || t.state === 'stuck' ? 'stuck' : '';
      const tState = you.length ? 'needs' : (waiters.size || n.blocked) ? 'waiting' : issues.length ? 'stuck' : (n.running ? 'running' : (n.queued && !n.done ? 'queued' : (n.queued ? 'running' : (ws.length ? 'done' : (explicitState || 'done')))));
      return {t, ws, asks, issues, n, state: tState, waiters};
    }).filter(r=>r.ws.length || r.asks.length || r.issues.length || (!who && (r.t.goal || r.t.now || r.t.state)));
    const rest = sc.workstreams.filter(w=>!used.has(w.id) && (!who || w.assignees.includes(who)));
    if(rest.length){ const n={done:0,running:0,blocked:0,queued:0}; rest.forEach(w=>{ collectWs(w); n[stOf(w)]++; }); const waiters=new Set(rest.map(waiterOf).filter(Boolean));
      rows.push({t:{id:'t-other', title: TM.threads.length ? 'Other' : 'Tasks', goal:'', now:''}, ws:rest, asks:[], issues:[], waiters, n, state: (waiters.size || n.blocked) ? 'waiting' : n.running ? 'running' : (n.queued ? 'queued' : 'done')}); }
    const listed = new Set(TM.threads.flatMap(t=>t.attention||[]));
    const loose = sc.attention.filter(a=>!a.id || !listed.has(a.id)).filter(a=>!who || a.from===who);
    const looseYou = loose.filter(openYou), looseAsks = loose.filter(openAsk), looseIssues = loose.filter(openIssue);
    if(looseYou.length) rows.push({t:{id:'t-you', title:ui('needs'), goal:'', now:''}, ws:[], asks:looseYou, issues:[], n:{done:0,running:0,blocked:0,queued:0}, state:'needs'});
    const strayAsks = [];
    looseAsks.forEach(a=>{ const r=rows.find(x=>x.ws.some(w=>w.assignees.includes(a.from)));
      if(r){ r.asks.push(a); r.waiters.add('coordinator'); if(r.state!=='needs') r.state='waiting'; } else strayAsks.push(a); });
    if(strayAsks.length) rows.push({t:{id:'t-waiting', title:waitOn('coordinator'), goal:'', now:''}, ws:[], asks:strayAsks, issues:[], n:{done:0,running:0,blocked:0,queued:0}, state:'waiting', waiters:new Set(['coordinator'])});
    if(looseIssues.length) rows.push({t:{id:'t-stuck', title:ui('stuck'), goal:'', now:''}, ws:[], asks:[], issues:looseIssues, n:{done:0,running:0,blocked:0,queued:0}, state:'stuck', waiters:new Set()});
    const order = {needs:0, waiting:1, stuck:2, running:3, queued:4, done:5};
    rows.sort((a,b)=>order[a.state]-order[b.state]);
    runningWs.sort((a,b)=>(a.st==='blocked'?0:1)-(b.st==='blocked'?0:1));
    const tot = {done:0,running:0,blocked:0,queued:0}; all.forEach(s=>tot[s]++);
    const LABEL = {needs:ui('needs'), stuck:ui('stuck'), running:'Moving', queued:'Not started', done:'Done'};
    const labelOf = r => r.state!=='waiting' ? LABEL[r.state] : r.waiters.size===1 ? waitOn([...r.waiters][0]) : r.waiters.size>1 ? `Waiting on ${r.waiters.size}` : 'Waiting';
    const ICON = {done:'✓', running:'', blocked:'', queued:''};
    const goalCopy = TM.goal || {};
    const goalTitle = goalCopy.title || clipWords(sc.headline, 9);
    const g = goalTitle ? `<section class="o2-goal">
      <div class="o2-glbl">${ui('goal')}</div>
      <b>${esc(goalTitle)}</b>
      ${goalCopy.line ? `<p>${esc(goalCopy.line)}</p>` : ''}
    </section>` : '';
    const threadSummaryFallback = rows.filter(r=>r.state!=='done' && r.t.now).map(r=>r.t.now).slice(0,2).join(' · ');
    const openThreads = rows.filter(r=>r.state!=='done').length;
    const openAsks = rows.reduce((acc,r)=>acc+r.asks.filter(openYou).length, 0);
    const pairs = new Set();
    rows.forEach(r=>r.asks.filter(openAsk).forEach(a=>pairs.add(`${a.from}>coordinator`)));
    runningWs.forEach(({w})=>{ const x=waiterOf(w); if(x) pairs.add(`${w.assignees[0]}>${x}`); });
    const waits = new Map(); pairs.forEach(k=>{ const n=k.slice(k.indexOf('>')+1); waits.set(n, (waits.get(n)||0)+1); });
    const stuckN = rows.reduce((acc,r)=>acc+r.issues.length, 0);
    const runHtml = runningCard(sc, runningWs, threadSummaryFallback, goalTitle, goalCopy.line||'');
    const progHtml = progressCard(sc, tot, doneWs, queuedWs, openThreads, openAsks, waitChips(waits, stuckN));
    const thread = r => { const id=r.t.id; const open = r.ws.length>0 && (state['o2t:'+id] ?? (r.state==='needs' || r.state==='waiting' || r.state==='stuck'));
      const sum = r.n.done+r.n.running+r.n.blocked+r.n.queued;
      const asks = [...r.asks, ...r.issues].map(a=>{ const c=itemCopy(a.id, a.title, a.why, a.full); const on = (a.wait==='coordinator' && r.t.id!=='t-waiting' && !r.ws.some(w=>w.assignees.includes(a.from))) ? `<span class="o2-askon">${esc(waitOn('coordinator'))}</span>` : '';
        return `<button type="button" class="o2-ask ${a.wait==='issue'?'issue':''}" ${a.post ? `data-ov-seq="${a.post}"` : `data-open-member="${esc(a.from)}"`}><span class="o2-askd" aria-hidden="true"></span><span class="o2-askb"><b>${esc(c.title)}</b><span class="o2-askl">${esc(c.line)}</span>${on}</span></button>`; }).join('');
      const tasks = r.ws.slice().sort((a,b)=>({blocked:0,running:1,queued:2,done:3}[stOf(a)]-({blocked:0,running:1,queued:2,done:3}[stOf(b)]))).map(w=>{ const s=stOf(w); const c=itemCopy(w.id, w.title, w.note); return `<li class="${s}"><span class="o2-ti ${s}" aria-label="${s}">${ICON[s]}</span><span class="o2-tt">${esc(c.title)}${waiterOf(w) ? `<span class="o2-tw">${esc(waitOn(waiterOf(w)))}</span>` : ''}</span>${taskProgressPill(w)}${workerBadges(w.assignees, 2, sc)}</li>`; }).join('');
      const head = `<span class="o2-thd ${r.state}" aria-hidden="true"></span>
          <span class="o2-tht"><b>${esc(r.t.title)}</b>${labelOf(r) !== r.t.title ? `<em>${esc(labelOf(r))}</em>` : ''}</span>
          <span class="o2-thc">${sum ? `${miniBar(r.n)}<span>${r.n.done}/${sum}</span>` : `<span>${r.asks.length + r.issues.length}</span>`}</span>`;
      return `<section class="o2-th ${r.state} ${open?'open':''}">
        ${r.ws.length ? `<button class="o2-thh" data-o2t="${esc(id)}" aria-expanded="${open}">${head}<span class="o2-chev" aria-hidden="true"></span></button>` : `<div class="o2-thh">${head}</div>`}
        ${r.t.goal ? `<p class="o2-thg">${esc(r.t.goal)}</p>` : ''}
        ${r.t.now && r.state!=='done' ? `<p class="o2-thn"><span>${ui('now')}</span>${esc(r.t.now)}</p>` : (r.t.now ? `<p class="o2-thn done">${esc(r.t.now)}</p>` : '')}
        ${asks}
        ${open ? `<ul class="o2-tasks">${tasks}</ul>` : ''}
      </section>`; };
    return {goal:g, running:runHtml, progress:progHtml, reel: reelCard(sc), threads: rows.map(thread).join('')};
  }

  /* ---------- Members view: Board-style separated rows with Bees tiles + Tier-3 Subagents ---------- */
  function sourceChip(src, onlyProblems){
    if(!src || (onlyProblems && src.state==='live')) return '';
    const hosts = Array.isArray(src.hosts) ? src.hosts : [];
    const label = src.label || 'Remote';
    const text = {live:`${label} · ${hosts.length} machine${hosts.length===1?'':'s'}`, stale:`${label} · reconnecting`, connecting:`${label} · connecting`, error:`${label} · unavailable`}[src.state] || label;
    const tip = [`Shared ${label} board ${src.swarm_id||''}`, src.db||'', ...hosts.map(o=>o&&o.label?String(o.label):''), src.error?`Error: ${src.error}`:''].filter(Boolean).join('\n');
    return `<div class="o2-src ${esc(src.state||'')}" data-testid="swarm-source" title="${esc(tip)}"><i aria-hidden="true"></i>${esc(text)}</div>`;
  }
  function stallBanner(sc){
    if(!sc.stall || sc.finished || sc.starting) return '';
    const s = sc.stall;
    return `<section class="o2-stall" data-testid="swarm-stall-banner">
      <div class="o2-stmain">
        <div class="o2-sttop"><span class="o2-stdot" aria-hidden="true"></span><b>Swarm stalled — no agents working</b><button type="button" class="o2-sttag" data-ov-seq="${s.seq}" title="${esc(`Go to #${s.seq} on the Board`)}">@${esc(s.agent)} #${s.seq}</button></div>
        <p class="o2-sttxt">${esc(s.text)}</p>
      </div>
    </section>`;
  }
  const agoText = ms => { const m=Math.round(ms/60000); if(m<1) return 'seen just now'; if(m<60) return `last seen ${m}m ago`; const hr=Math.round(m/60); return hr<48 ? `last seen ${hr}h ago` : `last seen ${Math.round(hr/24)}d ago`; };
  const wsShort = s => {
    const list = String(s||'').split(/,\s+/).map(p=>p.trim()).filter(Boolean);
    if(!list.length) return '';
    const one = p => {
      const norm = p.replace(/\\/g,'/');
      const segs = norm.replace(/\/+$/,'').split('/').filter(Boolean);
      if(!segs.length) return norm.startsWith('/') ? '/' : p;
      const out = segs.length > 2 ? '…/' + segs.slice(-2).join('/') : ((norm.startsWith('/') ? '/' : '') + segs.join('/'));
      return out.length > 48 ? '…' + out.slice(-47) : out;
    };
    return list.length > 1 ? `${one(list[0])} +${list.length-1}` : one(list[0]);
  };
  function memberTags(x){
    const tags = [];
    if(x.host) tags.push(`<span class="o2-mtag" data-testid="swarm-member-host" title="${esc(`Runs on ${x.host.label}`)}">${esc(x.host.label.includes('@') ? x.host.label : '@'+x.host.label)}</span>`);
    if(!x.local && x.last_seen) tags.push(`<span class="o2-mtag" data-testid="swarm-member-seen">${esc(agoText(Date.now()-x.last_seen))}</span>`);
    const wsMode = x.workspace_mode && x.workspace_mode!=='inherit' ? x.workspace_mode : '';
    const wsPath = x.workspace || '';
    if(wsMode || wsPath){
      const short = wsShort(wsPath);
      const lbl = wsMode && short ? `${wsMode} · ${short}` : (wsMode || short);
      const tip = wsMode && wsPath ? `${wsMode} · ${wsPath}` : (wsPath || wsMode);
      if(lbl) tags.push(`<span class="o2-mtag" data-testid="swarm-member-workspace" title="${esc(tip)}">${esc(lbl)}</span>`);
    }
    const chans = (x.channels||[]).filter(c=>c && c!=='main');
    if(chans.length) tags.push(`<span class="o2-mtag" data-testid="swarm-member-channels">${esc(chans.includes('*') ? 'all channels' : chans.map(c=>'#'+c).join(' '))}</span>`);
    if(x.role && x.role!=='worker' && x.role!=='coordinator') tags.push(`<span class="o2-mtag role" data-testid="swarm-member-role">${esc(x.role)}</span>`);
    if(x.replied && !x.killed) tags.push(`<span class="o2-mtag" data-testid="swarm-member-replied" title="${esc(x.replyAsk ? String(x.replyAsk) : 'Replied to user')}">replied</span>`);
    if(Array.isArray(x.subagents) && x.subagents.length){
      const activeSub = x.subagents.filter(s=>s.status==='working').length;
      const lbl = activeSub ? `${x.subagents.length} subagent${x.subagents.length===1?'':'s'} · ${activeSub} active` : `${x.subagents.length} subagent${x.subagents.length===1?'':'s'}`;
      tags.push(`<span class="o2-mtag sub" data-testid="swarm-member-subagents-count">${esc(lbl)}</span>`);
    }
    return tags.length ? `<div class="o2-mtags">${tags.join('')}</div>` : '';
  }

  function memberMetricsHtml(x){
    const parts = [];
    const steps = Number(x.steps) || 0;
    const tools = Number(x.toolCalls) || 0;
    const t = x.tokens && typeof x.tokens === 'object' ? x.tokens : {};
    const processed = Number(t.totalProcessed ?? t.total) || 0;
    const pure = Number(t.pureInput ?? t.uncachedInput ?? t.input) || 0;
    const cached = Number(t.cachedInput ?? t.cacheRead) || 0;
    const outTok = Number(t.output) || 0;
    const ctxUsed = Number(t.contextUsed) || 0;
    const ctxLimit = Number(t.contextLimit) || 0;
    let ctxPct = Number(t.contextPct) || 0;
    if (ctxPct <= 0 && ctxUsed > 0 && ctxLimit > 0) ctxPct = (ctxUsed / ctxLimit) * 100;
    const showCtx = ctxPct > 0 || (ctxUsed > 0 && ctxLimit > 0);
    const tps = Number(x.tps) || 0;
    const tpsPrefix = x.tpsEstimated ? '~' : '';
    const cost = x.costUsd != null ? fmtCost(x.costUsd) : '';
    if (steps > 0) parts.push(`<span>${esc(`${steps} ${steps === 1 ? 'step' : 'steps'}`)}</span>`);
    if (tools > 0) parts.push(`<span>${esc(`${tools} ${tools === 1 ? 'tool' : 'tools'}`)}</span>`);
    if (processed > 0) {
      const tip = `Processed: ${fmtTok(processed)} · Pure: ${fmtTok(pure)} · Cached: ${fmtTok(cached)} · Out: ${fmtTok(outTok)}`;
      parts.push(`<span title="${esc(tip)}">${esc(`${fmtTok(processed)} proc`)}</span>`);
    }
    if (pure > 0) parts.push(`<span>${esc(`${fmtTok(pure)} pure`)}</span>`);
    if (cached > 0) parts.push(`<span>${esc(`${fmtTok(cached)} cached`)}</span>`);
    if (tps > 0) parts.push(`<span title="${x.tpsEstimated ? 'Estimated live TPS' : 'Measured TPS'}">${esc(`${tpsPrefix}${fmtTps(tps)}`)}</span>`);
    if (showCtx) {
      const pctLabel = Math.round(ctxPct);
      const ctxLabel = (ctxUsed > 0 && ctxLimit > 0)
        ? `${fmtTok(ctxUsed)}/${fmtTok(ctxLimit)} ${pctLabel}%`
        : `${pctLabel}% ctx`;
      const ctxCls = ctxPct >= 85 ? ' class="o2-ctx-crit"' : (ctxPct >= 70 ? ' class="o2-ctx-warn"' : '');
      parts.push(`<span${ctxCls} title="Context window usage">${esc(ctxLabel)}</span>`);
    }
    if (cost) parts.push(`<span>${esc(cost)}</span>`);
    if (!parts.length) return '';
    return `<div class="o2-mmetrics" data-testid="swarm-member-metrics">${parts.join('')}</div>`;
  }

  function recentToolsHtml(n, x){
    const list = Array.isArray(x.recentTools) ? x.recentTools : [];
    if (!list.length) return '';
    const isOpen = !!(state.o2toolsOpen && state.o2toolsOpen[n]);
    const rows = list.slice(0, 12).map(t => {
      const call = fmtToolCall(t) || 'tool';
      const st = String(t.status || 'ok');
      const dur = Number(t.durationMs) > 0 ? fmtMs(t.durationMs) : '';
      const meta = [st, dur].filter(Boolean).join(' · ');
      return `<li class="o2-tool-row ${esc(st)}"><span class="o2-tool-call" title="${esc(call)}">${esc(call)}</span><span class="o2-tool-meta">${esc(meta)}</span></li>`;
    }).join('');
    return `<details class="o2-tools" data-o2tools="${esc(n)}" data-testid="swarm-member-recent-tools"${isOpen ? ' open' : ''}><summary>Recent activity (${list.length})</summary><ul class="o2-tools-list">${rows}</ul></details>`;
  }

  function subagentsTreeHtml(x, parentName){
    const subs = Array.isArray(x.subagents) ? x.subagents : [];
    if(!subs.length) return '';
    const pName = String(parentName || x.rawName || x.name || '').trim();
    const items = subs.slice(0, 12).map(s => {
      const subId = String(s.sessionId || s.id || '').trim();
      const rawName = String(s.name || s.id || 'subagent');
      const rawTask = String(s.task || s.summary || '');
      const dup = rawTask && (rawName === rawTask || (rawName.length > 36 && rawTask.startsWith(rawName.slice(0, 36))));
      const name = dup ? (s.id && s.id !== rawTask ? String(s.id).slice(0, 14) : 'subagent') : rawName;
      const task = rawTask;
      const st = s.status || 'idle';
      const metaParts = [];
      if(s.model) metaParts.push(String(s.model).split('/').pop());
      const subSteps = Number(s.steps) || 0;
      if(subSteps > 0) metaParts.push(`${subSteps} ${subSteps === 1 ? 'step' : 'steps'}`);
      const subTok = s.tokens && (Number(s.tokens.totalProcessed ?? s.tokens.total) || ((Number(s.tokens.pureInput ?? s.tokens.uncachedInput ?? s.tokens.input) || 0) + (Number(s.tokens.output) || 0)));
      if(subTok > 0) metaParts.push(`${fmtTok(subTok)} proc`);
      if(Number(s.tps) > 0) metaParts.push(`${s.tpsEstimated ? '~' : ''}${fmtTps(Number(s.tps))}`);
      const subCtxUsed = Number(s.tokens?.contextUsed) || 0;
      const subCtxLim = Number(s.tokens?.contextLimit) || 0;
      let subCtxPct = Number(s.tokens?.contextPct) || 0;
      if (subCtxPct <= 0 && subCtxUsed > 0 && subCtxLim > 0) subCtxPct = (subCtxUsed / subCtxLim) * 100;
      if (subCtxPct > 0) metaParts.push(`${Math.round(subCtxPct)}% ctx`);
      if(Number(s.elapsedMs) > 0) metaParts.push(fmtMs(Number(s.elapsedMs)));
      else if(!subSteps && st && st !== 'idle') metaParts.push(st);
      const clickAttrs = (pName && subId)
        ? ` data-open-member="${esc(pName)}" data-open-subagent="${esc(subId)}" role="button" tabindex="0" title="Open ${esc(name)} transcript"`
        : '';
      return `<li class="o2-sub-row${subId ? ' clickable' : ''}" data-testid="swarm-subagent-row"${clickAttrs}>
        <span class="o2-sub-dot ${esc(st)}" aria-hidden="true"></span>
        <span class="o2-sub-name" title="${esc(name)}">${esc(name)}</span>
        ${task ? `<span class="o2-sub-task" title="${esc(task)}">${esc(task)}</span>` : ''}
        ${metaParts.length ? `<span class="o2-sub-meta">${esc(metaParts.join(' · '))}</span>` : ''}
      </li>`;
    }).join('');
    const more = subs.length > 12 ? `<li class="o2-sub-row"><span class="o2-sub-task">+${subs.length - 12} more subagents</span></li>` : '';
    return `<div class="o2-subagents" data-testid="swarm-subagents-tree" role="region" aria-label="Leaf subagents">
      <div class="o2-sub-head">Subagents · ${subs.length}</div>
      <ul class="o2-sub-list">${items}${more}</ul>
    </div>`;
  }

  function membersView(sc){
    state.style = 'bees';
    const fmtMod = m => String(m||'').replace(/-(?:dropdown|high|medium)$/i,'');
    const allEntries = Object.entries(sc.members).filter(([,x])=>!x.system);
    const roleEntries = Object.entries(sc.members).filter(([,x])=>x.system && x.roleWorker).map(([n,x])=>[x.rawName||n, x]);
    const lead = sc.members.coordinator || {rawName:'coordinator', role:'coordinator', title:'Coordinator · Antigravity', status:'parked', now:'', posts:0, subagents:[]};
    const workers = allEntries.filter(([n])=>n!=='coordinator');
    const liveWorkers = workers.filter(([,x])=>!x.killed);
    const liveRoleWorkers = roleEntries.filter(([,x])=>!x.killed).sort((a,b)=>a[0].localeCompare(b[0]));
    const retiredWorkers = [...workers, ...roleEntries].filter(([,x])=>x.killed).sort((a,b)=>(b[1].posts||0)-(a[1].posts||0));
    const isAct = x => !sc.finished && (x.status==='needs_you' || x.status==='working' || x.status==='waiting_on' || !!x.issue);
    const actOrder = {needs_you:0, working:1, waiting_on:2, parked:3};
    const activeWorkers = liveWorkers.filter(([,x])=>isAct(x)).sort((a,b)=>actOrder[a[1].status]-actOrder[b[1].status] || (b[1].posts||0)-(a[1].posts||0));
    const standbyWorkers = liveWorkers.filter(([,x])=>!isAct(x)).sort((a,b)=>((b[1].last?.seq||0)-(a[1].last?.seq||0)) || a[0].localeCompare(b[0]));

    const searchIcon = `<svg class="icon" width="15" height="15" viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.25" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="m10.25 10.25 3.25 3.25" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`;
    const topBar = `${sourceChip(sc.source)}<div class="o2-mtop">
      <label class="o2-msearch">${searchIcon}<input type="search" class="o2-msin" placeholder="Search members" aria-label="Search members" autocomplete="off" value="${esc(state.o2mq||'')}"></label>
    </div>`;

    const leftBeeTile = (n, x) => {
      const st = sc.finished || x.killed ? '' : x.status;
      const dot = (st==='working' || st==='needs_you' || st==='waiting_on') ? `<i class="o2-mavd ${st}" aria-hidden="true"></i>` : '';
      return `<span class="o2-mav bee" style="${hexBG(n)}" aria-hidden="true">${spriteTag(n, x.killed?'landed':(sc.finished?'landed':x.status))}${dot}</span>`;
    };

    const rowHtml = (n, x) => {
      const isCoord = n==='coordinator';
      const st = (sc.finished || x.killed) ? 'parked' : x.status;
      const metaParts = [];
      const mod = fmtMod(x.model);
      if(mod) metaParts.push(mod);
      if(x.last && x.last.seq) metaParts.push('#'+x.last.seq);
      else if(x.lastPostSeq > 0) metaParts.push('#'+x.lastPostSeq);
      else if(x.posts) metaParts.push(x.posts + (x.posts===1?' post':' posts'));
      if(x.lastReadSeq > 0) metaParts.push('read #' + x.lastReadSeq);
      const meta = metaParts.join(' · ');

      let statTag = '';
      if(isCoord){
        statTag = st==='needs_you' ? `<span class="o2-mstat warn">${ui('needs')}</span>` : st==='working' ? `<span class="o2-mstat info">Coordinator · Antigravity · Active</span>` : `<span class="o2-mstat">Coordinator · Antigravity</span>`;
      } else if(x.killed){
        statTag = x.title ? `<span class="o2-mstat">${esc(x.title)}</span>` : `<span class="o2-mstat">Retired</span>`;
      } else if(x.issue && !sc.finished){
        statTag = `<span class="o2-mstat bad">${x.issue.kind==='stuck' ? 'Stuck' : 'Error'}</span>`;
      } else if(x.roleWorker){
        statTag = st==='working' ? `<span class="o2-mstat info">${esc(x.role || n)} · Working</span>` : `<span class="o2-mstat">${esc(x.role || n)} · Watching</span>`;
      } else if(st==='working'){
        statTag = `<span class="o2-mstat info">${x.role && x.role!=='worker' ? esc(x.role) + ' · ' : ''}Working</span>`;
      } else if(st==='waiting_on' && x.waiting_on){
        statTag = `<span class="o2-mstat warn">${esc(waitOn(x.waiting_on))}</span>`;
      } else if(x.title){
        statTag = `<span class="o2-mstat">${esc(x.title)}</span>`;
      } else if(x.role && x.role!=='worker'){
        statTag = `<span class="o2-mstat">${esc(x.role)}</span>`;
      }

      const hasIssue = !isCoord && !!x.issue && !sc.finished;
      const showRoleSub = !isCoord && !x.killed && (st==='working' || st==='waiting_on' || hasIssue || x.roleWorker) && x.title;
      let bodyLine = isCoord ? (x.now || memberLine('coordinator', x)) : (x.now || '');
      if(isCoord && st==='needs_you' && x.ask){
        bodyLine = x.ask.title;
      } else if(x.ask && (st==='needs_you' || st==='waiting_on')){
        const ac = itemCopy(x.ask.id, x.ask.title, x.ask.why, x.ask.full);
        bodyLine = ac.line ? `${ac.title} — ${ac.line}` : ac.title;
      } else if(hasIssue){
        bodyLine = itemCopy(x.issue.id, x.issue.title, x.issue.why).line || x.issue.title;
      }

      const wsMode = x.workspace_mode && x.workspace_mode!=='inherit' ? x.workspace_mode : '';
      const subText = (x.subagents||[]).map(s=>`${s.name||''} ${s.task||''}`).join(' ');
      const hay = `${n} ${x.role||''} ${x.title||''} ${x.model||''} ${wsMode} ${x.workspace||''} ${bodyLine||''} ${subText} ${st}`;
      const toolCallStr = !sc.finished && !x.killed && st==='working' && x.currentTool?.name ? fmtToolCall(x.currentTool) : '';
      const toolPillHtml = toolCallStr
        ? `<div class="o2-toolpill" data-testid="swarm-member-current-tool" title="${esc('Running ' + toolCallStr)}"><span class="o2-tooldot" aria-hidden="true"></span><span class="o2-tooltxt">Running <b>${esc(toolCallStr)}</b></span></div>`
        : '';
      const live = !sc.finished && !x.killed && !hasIssue && x.live ? x.live : null;
      const redundantLive = !!(toolCallStr && live && live.kind==='tool' && live.text.toLowerCase().startsWith(`running ${x.currentTool.name.toLowerCase()}`));
      const nowHtml = (live && !redundantLive)
        ? `<div class="o2-mnow o2-mact ${esc(live.kind)}" data-testid="swarm-member-activity" title="${esc(liveTip(live))}">${liveText(live)}</div>`
        : (!toolPillHtml && bodyLine ? `<div class="o2-mnow">${esc(bodyLine)}</div>` : '');
      const metricsHtml = memberMetricsHtml(x);
      const recentHtml = recentToolsHtml(n, x);
      const openAttr = `data-open-member="${esc(x.rawName || n)}"`;
      const cls = `o2-mrow bees ${st} ${hasIssue?'issue':''} ${x.killed?'retired':''} ${isCoord?'coord':''}`;

      return `<article class="${cls}" ${openAttr} data-mhay="${esc(hay)}">
        ${leftBeeTile(n, x)}
        <div class="o2-mcol">
          <header class="o2-mhead">
            <span class="o2-mwho"><span class="o2-mauth">@${esc(isCoord?'coordinator':n)}</span>${statTag}</span>
            <span class="o2-mright">${meta?`<span class="o2-mts">${esc(meta)}</span>`:''}</span>
          </header>
          ${showRoleSub ? `<div class="o2-mrole">${esc(x.title)}</div>` : ''}
          ${memberTags(x)}
          ${toolPillHtml}
          ${nowHtml}
          ${metricsHtml}
          ${recentHtml}
          ${subagentsTreeHtml(x, x.rawName || n)}
        </div>
      </article>`;
    };

    const groupHtml = (label, list) => list.length
      ? `<section class="o2-mgroup"><h3 class="o2-msec"><span>${label}</span><em>${list.length}</em></h3><div class="o2-mlist">${list.map(([n,x])=>rowHtml(n,x)).join('')}</div></section>`
      : '';

    const retOpen = !!state.o2mRetired;
    const retiredHtml = retiredWorkers.length
      ? `<section class="o2-mgroup o2-mretgrp ${retOpen?'open':''}">
          <button type="button" class="o2-mret" data-o2="mRetired" aria-expanded="${retOpen}">
            <span>Retired</span><em>${retiredWorkers.length}</em><span class="o2-chev" aria-hidden="true"></span>
          </button>
          ${retOpen ? `<div class="o2-mlist">${retiredWorkers.map(([n,x])=>rowHtml(n,x)).join('')}</div>` : ''}
        </section>`
      : '';

    return `<div class="o2 o2-members">
      ${topBar}
      <div class="o2-mbody">
        <section class="o2-mgroup o2-mcoord">${rowHtml('coordinator', lead)}</section>
        ${groupHtml('Active', activeWorkers)}
        ${groupHtml(sc.finished ? 'Team' : 'Standing by', standbyWorkers)}
        ${groupHtml('Role workers', liveRoleWorkers)}
        ${retiredHtml}
      </div>
    </div>`;
  }

  function onboardView(sc){
    const real = people(sc).filter(([n,x])=>n!=='coordinator' && !x.killed).map(([n])=>n).slice(0,3);
    const ws = [...real, ...['worker-a','worker-b','worker-c']].slice(0,3);
    const Y = [30, 78, 126];
    const path = (y,i) => `<path id="o2-onb-p${i}" d="M84 78 C150 78 160 ${y} 226 ${y}"/>`;
    const dot = (i, fill, back, begin) => `<circle r="3" fill="${fill}" opacity="0"><animateMotion dur="2.4s" begin="${begin}s" repeatCount="indefinite" keyPoints="${back?'1;0':'0;1'}" keyTimes="0;1" calcMode="linear"><mpath href="#o2-onb-p${i}"/></animateMotion><animate attributeName="opacity" values="0;1;1;0" keyTimes="0;0.15;0.85;1" dur="2.4s" begin="${begin}s" repeatCount="indefinite"/></circle>`;
    const bee = (n, cls, style) => `<span class="o2-mav bee ${cls}" style="${hexBG(n)};${style}">${spriteTag(n,'working')}</span>`;
    const icon = n => `<span class="o2-onb-i" style="${hexBG(n)}"><img src="${beeImg(n)}" alt="" width="22" height="18"></span>`;
    return `<section class="o2-onb" data-testid="swarm-onboarding">
      <div class="o2-onb-stage" aria-hidden="true">
        <svg class="o2-onb-paths" viewBox="0 0 320 156" width="320" height="156">
          <g class="o2-onb-lines">${Y.map(path).join('')}</g>
          ${ws.map((n,i)=>dot(i, tcol('coordinator'), false, i*0.8) + dot(i, tcol(n), true, i*0.8+1.2)).join('')}
        </svg>
        ${bee('coordinator', 'o2-onb-q', 'left:24px;top:56px')}
        ${ws.map((n,i)=>bee(n, `o2-onb-w s${i}`, `left:236px;top:${Y[i]-22}px`)).join('')}
      </div>
      <h2 class="o2-onb-h">Your swarm is starting</h2>
      <ol class="o2-onb-steps">
        <li>${icon('coordinator')}<div><b>One goal</b><span>The coordinator splits it into tasks.</span></div></li>
        <li>${icon(ws[0])}<div><b>Many workers</b><span>Each takes a task, and they run in parallel.</span></div></li>
        <li>${icon(ws[1])}<div><b>One board</b><span>Updates land here. You step in only when asked.</span></div></li>
      </ol>
      <p class="o2-onb-wait"><span class="animate-shimmer-text">Waiting for the first update</span></p>
    </section>`;
  }

  const ONBOARD_MIN_MS = 30000;

  function overviewView(sc, el){
    state.style = 'bees';
    const att = k => sc.finished ? [] : sc.attention.filter(a=>a.wait===k);
    const you = att('you'), hot = att('coordinator'), issues = att('issue');
    const open = sc.workstreams.filter(w=>w.state!=='done');
    const items = [];
    open.forEach(w=>{
      let why=null;
      if(w.state==='blocked') why={kind:'agent', who:w.assignees[0], on:w.blocked_on||null, seq:w.seq};
      else if(w.state==='active'){ for(const o of w.assignees){ const m=sc.members[o]; if(m && m.status==='waiting_on'){ why={kind:'agent', who:o, on:m.waiting_on, seq:m.waiting_seq}; break; } } }
      items.push({id:w.id, title:w.title, line:w.note, assignee:w.assignees[0], assignees:w.assignees, why, group: why ? 'blocked' : (w.state==='queued' ? 'planned' : 'running')});
    });
    if (state.o2who && (!sc.members[state.o2who] || sc.members[state.o2who].killed)) state.o2who = null;
    const who = state.o2who || null;
    const mine = list => who ? list.filter(i=>(i.assignees||[i.assignee]).includes(who)) : list;
    const G = k => mine(items.filter(i=>i.group===k));
    const running=G('running');

    const M = n => sc.members[n] || {status:'parked', now:''};
    const art = (n) => { const x=M(n); return spriteTag(n, x.killed ? 'landed' : (sc.finished?'landed':x.status)); };
    const tile = (n, cls) => `<span class="o2-tile ${cls||''} bees" style="${hexBG(n)}">${art(n)}</span>`;

    const lead = M('coordinator');
    const crew = [...people(sc)].filter(([n,x])=>n!=='coordinator' && !x.killed)
      .sort((a,b)=>ORDER[a[1].status]-ORDER[b[1].status]);
    const MAXT = 5; let shown = crew.length>MAXT+1 ? crew.slice(0,MAXT) : crew;
    if(who && !shown.some(([n])=>n===who)){ const sel=crew.find(([n])=>n===who); if(sel) shown=[...shown.slice(0,MAXT-1), sel]; }
    const extra = crew.length-shown.length;
    const trio = crew.slice(0,3).map(([n])=>n);
    const swarmArt = `<span class="o2-swarm">${trio.map((n,i)=>`<span class="o2-sb s${i}">${spriteTag(n, sc.finished?'landed':'working')}</span>`).join('')}</span>`;
    const allTile = `<button class="o2-m all ${who?'':'on'}" aria-pressed="${!who}" title="All workers" data-o2who=""><span class="o2-tile sm all bees">${swarmArt}</span><span>All</span></button>`;
    const team = `<section class="o2-team">
      <h3 class="o2-h">${ui('team')}</h3>
      <div class="o2-lead" data-open-member="coordinator" role="button" tabindex="0" title="Open Coordinator chat or filter posts">${tile('coordinator','lg')}<div class="o2-lb"><b>${ui('lead')}</b>${lead.live && !sc.finished ? `<p class="o2-live" title="${esc(liveTip(lead.live))}">${liveText(lead.live, memberLine('coordinator', lead))}</p>` : `<p>${esc(memberLine('coordinator', lead))}</p>`}</div></div>
      ${crew.length ? `<div class="o2-crew" role="group" aria-label="Filter by worker">${allTile}${shown.map(([n,x])=>`<button class="o2-m ${sc.finished?'':x.status} ${who===n?'on':''}" aria-pressed="${who===n}" title="${esc(n)}: ${esc(memberLine(n,x))}" data-o2who="${esc(n)}">${tile(n,'sm')}<i class="o2-st" aria-hidden="true"></i><span>@${esc(n)}</span></button>`).join('')}${extra>0?`<button class="o2-m more" id="o2More"><span class="o2-tile sm plus">+${extra}</span><span>&nbsp;</span></button>`:''}</div>` : ''}
    </section>`;

    const card = it => { const c=itemCopy(it.id, it.title, it.line, it.full);
      return `<div class="o2-card">${tile(it.assignee,'md')}<div class="o2-cb"><b>${esc(c.title)}</b><p>${esc(c.line)}</p><div class="o2-who">@${esc(it.assignee)}</div></div></div>`; };
    const branch = (k, list, none, limit) => { const lim=state['o2'+k]?list.length:limit; const vis=list.slice(0,lim); const more=list.length-vis.length;
      return `<section class="o2-br ${k}"><h3 class="o2-h"><i></i>${ui(k)}<em>${list.length}</em></h3>
        ${list.length ? `<div class="o2-cards">${vis.map(card).join('')}</div>${more>0?`<button class="o2-more" data-o2="${k}">${ui('more').replace('{n}',more)}</button>`:''}` : `<p class="o2-none">${ui(none)}</p>`}</section>`; };

    if(sc.starting){
      if(!state.onboardAt && el && el.offsetWidth){ state.onboardAt = Date.now(); setTimeout(() => { ROOTS.forEach(r => { r.etag = ''; }); sync(); }, ONBOARD_MIN_MS); }
      if(!sc.workerStarted || !state.onboardAt || Date.now() - state.onboardAt < ONBOARD_MIN_MS) return `<div class="o2">${sourceChip(sc.source, true)}${onboardView(sc)}${team}</div>`;
    }
    const TM = threadModel(sc);
    if(!TM && !sc.builtin){
      const stOf = w => { if(w.state!=='done' && w.state!=='blocked'){ const m=sc.members[w.assignees[0]]; if(m && m.status==='waiting_on') return 'blocked'; } return TSTATE[w.state]||'queued'; };
      const tot = {done:0, running:0, blocked:0, queued:0};
      const run = [], done = [], queued = [];
      sc.workstreams.filter(w=>!who || w.assignees.includes(who)).forEach(w=>{ const st=stOf(w); tot[st]++; if(st==='running'||st==='blocked') run.push({w, st}); else if(st==='done') done.push(w); else queued.push(w); });
      const toItem = a=>({id:a.id||('ask-'+a.seq), title:a.title, full:a.full, line:a.why, assignee:a.from, assignees:[a.from]});
      const needs = mine(you.map(toItem)), waiting = mine(hot.map(toItem)), stuck = mine(issues.map(toItem));
      const waits = new Map(); const bump = n => waits.set(n, (waits.get(n)||0)+1);
      waiting.forEach(()=>bump('coordinator'));
      run.forEach(({w})=>{ const x = w.state==='blocked' ? w.blocked_on : (sc.members[w.assignees[0]]||{}).waiting_on; if(x && (w.state==='blocked' || stOf(w)==='blocked')) bump(x); });
      return `<div class="o2">${sourceChip(sc.source, true)}${stallBanner(sc)}${needs.length ? branch('needs', needs, 'none_needs', 3) : ''}${waiting.length ? branch('waiting', waiting, 'none_waiting', 3) : ''}${stuck.length ? branch('stuck', stuck, 'none_stuck', 3) : ''}${runningCard(sc, run, '', '', '') || branch('running', running, 'none_running', 3)}${progressCard(sc, tot, done, queued, null, needs.length, waitChips(waits, stuck.length))}${reelCard(sc)}${team}</div>`;
    }
    const v = threadsView(sc, TM || {goal: sc.goal, threads: []});
    return `<div class="o2">${sourceChip(sc.source, true)}${stallBanner(sc)}${v.goal}${v.running}${v.progress}${v.reel}<section class="o2-threads"><h3 class="o2-h">${ui('threads')}</h3>${v.threads}</section>${team}</div>`;
  }

  function applyMemberFilter(el, rawQ) {
    const q = String(rawQ || '').trim().toLowerCase();
    el.querySelectorAll('.o2-mrow').forEach(row => {
      row.style.display = (!q || (row.dataset.mhay || '').toLowerCase().includes(q)) ? '' : 'none';
    });
    el.querySelectorAll('.o2-mgroup').forEach(grp => {
      grp.style.display = (!q || [...grp.querySelectorAll('.o2-mrow')].some(r => r.style.display !== 'none')) ? '' : 'none';
    });
  }

  /* ---------- render ---------- */
  const LAST_RENDER = new WeakMap();
  function render(el, view) {
    const sc = S.live;
    if (!sc) return;
    const v = view === 'cards' ? 'cards' : 'overview';
    const html = v === 'cards' ? membersView(sc) : overviewView(sc, el);
    const prev = LAST_RENDER.get(el);
    if (prev && prev.view === v && prev.html === html && el.dataset.view === v) return;
    LAST_RENDER.set(el, { view: v, html });
    if (v === 'cards' && el.dataset.view === 'cards') {
      const mbody = el.querySelector('.o2-mbody');
      const marker = '<div class="o2-mbody">';
      const idx = html.indexOf(marker);
      if (mbody && idx !== -1) {
        const inner = html.slice(idx + marker.length, html.lastIndexOf('</div>\n    </div>') !== -1 ? html.lastIndexOf('</div>\n    </div>') : html.lastIndexOf('</div>'));
        const bodyHtml = inner.replace(/<\/div>\s*$/, '');
        if (mbody.innerHTML !== bodyHtml) {
          mbody.innerHTML = bodyHtml;
          paintSprites(mbody);
          requestAnimationFrame(() => snapBees(mbody));
          bind(mbody);
          if (state.o2mq) applyMemberFilter(el, state.o2mq);
        }
        return;
      }
    }
    const active = document.activeElement;
    const typing = active && active.classList && active.classList.contains('o2-msin') && el.contains(active)
        ? {start: active.selectionStart, end: active.selectionEnd} : null;
    el.innerHTML = html;
    el.dataset.view = v;
    paintSprites(el);
    requestAnimationFrame(() => snapBees(el));
    bind(el);
    if (typing) {
      const inp = el.querySelector('.o2-msin');
      if (inp) { inp.focus({preventScroll: true}); inp.setSelectionRange(typing.start, typing.end); }
    }
  }

  function closeReel() { const lb = $('.o2-tv'); if (lb) (lb.closeReel || (() => lb.remove()))(); }

  function openReel(F, start, onMove) {
    closeReel();
    if (!F.length) return;
    const back = document.activeElement;
    const lb = document.createElement('div');
    lb.className = 'o2-tv'; lb.setAttribute('role', 'dialog'); lb.setAttribute('aria-modal', 'true'); lb.setAttribute('aria-label', ui('progress'));
    lb.innerHTML = `<div class="o2-tvh"><b>${ui('progress')}</b><span class="o2-tvpos"></span><span class="o2-tvwhen"></span><button class="o2-tvx" type="button" aria-label="Close">\u2715</button></div>
      <div class="o2-tvstage"><div class="o2-tvscroll"><img class="o2-tvimg" alt=""></div><button class="o2-tvnav prev" type="button" aria-label="Previous">\u2039</button><button class="o2-tvnav next" type="button" aria-label="Next">\u203a</button></div>
      <div class="o2-tvtext"><p class="o2-tvcap"></p><p class="o2-tvmeta"></p></div>
      <div class="o2-tvstrip" role="listbox" aria-label="Frames">${F.map((f, k) => `<button class="o2-tvth" type="button" role="option" data-k="${k}" aria-label="${esc(`${k + 1}. ${f.cap || ''}`)}"><img src="${esc(f.src)}" alt="" loading="lazy"><span>${k + 1}</span></button>`).join('')}</div>`;
    document.body.appendChild(lb);
    const img = $('.o2-tvimg', lb), scroller = $('.o2-tvscroll', lb), strip = $('.o2-tvstrip', lb);
    let i = -1;
    const go = (k, opening) => {
      const n = Math.max(0, Math.min(F.length - 1, k));
      if (n === i) return;
      i = n; const f = F[i];
      scroller.classList.remove('zoom');
      img.src = f.src; img.alt = f.cap || '';
      $('.o2-tvpos', lb).textContent = i === F.length - 1 ? `${ui('now')} \u00b7 ${i + 1} / ${F.length}` : `${i + 1} / ${F.length}`;
      $('.o2-tvwhen', lb).textContent = f.when || '';
      $('.o2-tvcap', lb).textContent = f.cap || '';
      $('.o2-tvmeta', lb).textContent = f.total ? `${f.done} of ${f.total} tasks done` : '';
      $('.o2-tvnav.prev', lb).disabled = i === 0; $('.o2-tvnav.next', lb).disabled = i === F.length - 1;
      strip.querySelectorAll('.o2-tvth').forEach((b, k2) => { const on = k2 === i; b.classList.toggle('on', on); b.setAttribute('aria-selected', on); if (on) b.scrollIntoView({block: 'nearest', inline: 'center'}); });
      if (onMove && !opening) onMove(i);
    };
    const close = () => {
      document.removeEventListener('keydown', key, true); lb.remove();
      const to = back && back.isConnected ? back : $('.o2-reel .o2-rexp');
      if (to && to.focus) to.focus({preventScroll: true});
    };
    lb.closeReel = close;
    const key = e => {
      if (e.key === 'Escape') { e.preventDefault(); close(); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); go(i - 1); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); go(i + 1); }
      else if (e.key === 'Home') { e.preventDefault(); go(0); }
      else if (e.key === 'End') { e.preventDefault(); go(F.length - 1); }
    };
    document.addEventListener('keydown', key, true);
    $('.o2-tvx', lb).onclick = close;
    $('.o2-tvnav.prev', lb).onclick = () => go(i - 1);
    $('.o2-tvnav.next', lb).onclick = () => go(i + 1);
    strip.querySelectorAll('.o2-tvth').forEach(b => b.onclick = () => go(+b.dataset.k));
    img.onclick = () => scroller.classList.toggle('zoom');
    lb.onclick = e => { if (e.target === lb || e.target === scroller) close(); };
    go(start, true);
    lb.tabIndex = -1; lb.focus({preventScroll: true});
  }

  function bind(el) {
    const hooks = (window.SwarmHive && window.SwarmHive.hooks) || {};
    el.querySelectorAll('.o2-reel').forEach(r => {
      let F = [];
      try { F = JSON.parse(r.dataset.reel || '[]'); } catch (_) {}
      const img = r.querySelector('.o2-rimg'), scrub = r.querySelector('.o2-rscrub'), sw = r.querySelector('.o2-rswipe'),
          line = r.querySelector('.o2-rline'), cmp = r.querySelector('.o2-rcmp');
      const show = i => {
        const f = F[i]; if (!f) return;
        img.src = f.src; img.alt = f.cap;
        r.querySelector('.o2-rcap').textContent = f.cap;
        r.querySelector('.o2-rwhen').textContent = f.when;
        r.querySelector('.o2-rmeta').textContent = r.querySelector('.o2-rmeta').textContent.replace(/^\d+/, f.done);
        r.querySelector('.o2-rtag').textContent = i === F.length - 1 ? 'Now' : `${i + 1} / ${F.length}`;
        state.o2reel = i;
      };
      const clip = () => {
        if (!cmp || !sw || !line) return;
        const on = cmp.getAttribute('aria-pressed') === 'true';
        img.style.clipPath = on ? `inset(0 ${100 - sw.value}% 0 0)` : 'none';
        line.style.left = sw.value + '%';
      };
      if (scrub) scrub.oninput = () => show(+scrub.value);
      if (sw) sw.oninput = () => { state.o2reelSwipe = +sw.value; clip(); };
      if (state.o2reelCmp && cmp) clip();
      const open = () => openReel(F, state.o2reel ?? F.length - 1, i => { state.o2reel = i === F.length - 1 ? null : i; sync(); });
      r.querySelector('.o2-rexp').onclick = (e) => { e.stopPropagation(); open(); };
      img.onclick = () => { if (!cmp || cmp.getAttribute('aria-pressed') !== 'true') open(); };
      if (cmp) cmp.onclick = () => {
        const on = cmp.getAttribute('aria-pressed') !== 'true';
        state.o2reelCmp = on;
        cmp.setAttribute('aria-pressed', on);
        cmp.textContent = on ? 'Hide compare' : 'Compare with now';
        if (sw) sw.hidden = !on;
        if (line) line.hidden = !on;
        clip();
      };
    });
    el.querySelectorAll('[data-o2t]').forEach(b => b.onclick = (e) => {
      e.stopPropagation(); state['o2t:' + b.dataset.o2t] = b.getAttribute('aria-expanded') !== 'true'; sync();
    });
    el.querySelectorAll('[data-o2who]').forEach(b => b.onclick = (e) => {
      e.stopPropagation(); const n = b.dataset.o2who; state.o2who = (n && state.o2who !== n) ? n : null; sync();
    });
    el.querySelectorAll('[data-o2]').forEach(b => b.onclick = (e) => {
      e.stopPropagation(); const k = 'o2' + b.dataset.o2; state[k] = !state[k]; sync();
    });
    const om = $('#o2More', el);
    if (om) om.onclick = () => { if (hooks.members) hooks.members(); };
    el.querySelectorAll('details.o2-tools').forEach(d => {
      d.addEventListener('click', e => e.stopPropagation());
      d.addEventListener('toggle', () => {
        state.o2toolsOpen = state.o2toolsOpen || {};
        state.o2toolsOpen[d.dataset.o2tools] = d.open;
      });
    });
    el.querySelectorAll('[data-open-member]').forEach(b => {
      b.onclick = (e) => {
        if (e.target.closest('details.o2-tools')) return;
        const subEl = e.target.closest('[data-open-subagent]');
        if (subEl && subEl !== b) return;
        const btn = e.target.closest('button');
        if (btn && btn !== b) return;
        if (b.dataset.openSubagent) e.stopPropagation();
        if (hooks.member) hooks.member(b.dataset.openMember, b.dataset.openSubagent || '');
      };
      if (b.getAttribute('role') === 'button') {
        b.onkeydown = (e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            e.stopPropagation();
            if (hooks.member) hooks.member(b.dataset.openMember, b.dataset.openSubagent || '');
          }
        };
      }
    });
    el.querySelectorAll('.o2-msin').forEach(inp => {
      const apply = () => {
        state.o2mq = inp.value;
        applyMemberFilter(el, inp.value);
      };
      inp.oninput = apply;
      if (inp.value) apply();
    });
  }

  /* ================= adapter: plugin snapshot -> view model ================= */
  function toModel(snap){
    const rawMembers = Array.isArray(snap.members) ? snap.members : [];
    const byName = new Map(rawMembers.map(m=>[m.name,m]));
    const isRole = n => { const m=byName.get(n); return !!(m && (m.roleWorker || m.narrator)) || /^(?:narrator|monitor)(?:-\d+)?$/.test(n); };
    const leadM = rawMembers.find(m=>m.role==='coordinator');
    const L = leadM ? leadM.name : 'coordinator';
    const key = n => n===L ? 'coordinator' : n;
    window.SwarmHive = window.SwarmHive || {}; window.SwarmHive.leadName = L;
    const rawMsgs = Array.isArray(snap.messages) ? snap.messages : [];
    const msgs = rawMsgs.filter(m=>{
      const sender = m.agent || m.sender;
      return !isRole(sender) && !(m.control && typeof m.control==='object');
    });
    const cleanText = t => String(t||'').replace(/^\s*(?:[-*]|#+|\d+\.)\s+/gm,'').replace(/\*\*([^*]+)\*\*/g,'$1').replace(/\s+/g,' ').trim().slice(0,400);
    P = msgs.map(m=>{
      const sender = m.agent || m.sender;
      return { s:m.seq, a:key(sender), t:new Date(m.ts || Date.now()).toISOString(),
        to:(m.all?['*']:(m.mentions||[]).filter(n=>n!==sender && !isRole(n)).map(key)),
        x:cleanText(m.text || m.summary || '') };
    });
    const head = snap.head || (P.length?P[P.length-1].s:0);
    const narratorView = snap.view && snap.view.json && typeof snap.view.json==='object' ? snap.view.json : null;
    const plan = snap.plan && typeof snap.plan==='object' ? snap.plan : null;
    const planTaskList = [];
    const planTaskByKey = new Map();
    if (plan && plan.tasks) {
      const addPlanTask = (keyId, rawVal) => {
        if (typeof rawVal === 'string') {
          const title = rawVal.trim();
          if (!title) return;
          const rec = { id: keyId, title, status: '', note: '', assignee: keyId, assignees: keyId ? [keyId] : [], progress: null };
          planTaskList.push(rec);
          if (keyId) planTaskByKey.set(keyId, rec);
          return;
        }
        if (!rawVal || typeof rawVal !== 'object') return;
        const id = String(rawVal.id || keyId || rawVal.assignee || ('task-' + planTaskList.length));
        const title = String(rawVal.title || rawVal.task || rawVal.name || '').trim();
        const assignee = String(rawVal.assignee || (!Array.isArray(plan.tasks) && keyId) || '').trim();
        const assignees = Array.isArray(rawVal.assignees) ? rawVal.assignees.map(String) : (assignee ? [assignee] : []);
        const rec = {
          id,
          title,
          status: String(rawVal.status || rawVal.state || '').trim(),
          note: String(rawVal.note || rawVal.summary || '').trim(),
          assignee,
          assignees,
          progress: rawVal.progress ?? null,
        };
        planTaskList.push(rec);
        if (id) planTaskByKey.set(id, rec);
        if (assignee && !planTaskByKey.has(assignee)) planTaskByKey.set(assignee, rec);
      };
      if (Array.isArray(plan.tasks)) {
        plan.tasks.forEach((v, idx) => addPlanTask(v && typeof v === 'object' ? (v.id || v.assignee || ('task-' + idx)) : ('task-' + idx), v));
      } else if (typeof plan.tasks === 'object') {
        Object.entries(plan.tasks).forEach(([k, v]) => addPlanTask(k, v));
      }
    }
    const metaGoal = (snap.meta && (snap.meta.goal || snap.meta.mission)) || snap.goal || '';
    const normGoal = (g) => {
      if (!g) return null;
      if (typeof g === 'string') {
        const s = g.trim();
        return s ? { title: s, line: metaGoal && metaGoal !== s ? metaGoal : '' } : null;
      }
      if (typeof g === 'object' && g.title) return { title: String(g.title), line: String(g.line || '') };
      return null;
    };
    const planGoalObj = normGoal(plan && plan.goal) || (plan && plan.summary ? { title: String(plan.summary), line: metaGoal && metaGoal !== plan.summary ? metaGoal : '' } : null) || (metaGoal ? { title: String(metaGoal), line: snap.meta?.mission && snap.meta.mission !== metaGoal ? String(snap.meta.mission) : '' } : null);
    const normPlanThreads = (Array.isArray(plan?.threads) ? plan.threads : [])
      .filter(t => t && t.title && (Array.isArray(t.tasks) || Array.isArray(t.workstreams)))
      .map((t, i) => ({
        id: t.id || ('t' + i),
        title: t.title,
        goal: t.goal || '',
        now: t.now || '',
        state: t.state || '',
        workstreams: (Array.isArray(t.tasks) ? t.tasks : t.workstreams).filter(n => typeof n === 'string').map(n => n === L ? 'coordinator' : n),
      }));
    const normViewThreads = (Array.isArray(narratorView?.threads) ? narratorView.threads : [])
      .filter(t => t && t.title)
      .map((t, i) => {
        const rawList = Array.isArray(t.workstreams) ? t.workstreams : (Array.isArray(t.tasks) ? t.tasks : []);
        return {
          id: t.id || ('t' + i),
          title: t.title,
          goal: t.goal || t.why || '',
          now: t.now || '',
          state: t.state || t.outcome || '',
          workstreams: rawList.filter(n => typeof n === 'string').map(n => n === L ? 'coordinator' : n),
          attention: Array.isArray(t.attention) ? t.attention : [],
        };
      });
    const useViewThreads = normViewThreads.some(t => t.workstreams.length > 0) || !normPlanThreads.length;
    const vj = narratorView ? {
      ...narratorView,
      goal: normGoal(narratorView.goal) || planGoalObj,
      headline: narratorView.headline || (narratorView.now && typeof narratorView.now === 'object' ? (narratorView.now.phase || narratorView.now.summary || '') : ''),
      phase: narratorView.phase || (narratorView.now && typeof narratorView.now === 'object' && typeof narratorView.now.phase === 'string' ? { label: narratorView.now.phase, index: 1, total: 1 } : null),
      threads: useViewThreads ? normViewThreads : normPlanThreads,
    } : (plan || metaGoal ? {
      goal: planGoalObj,
      threads: normPlanThreads,
    } : {});
    const team = rawMembers.filter(m=>!m.roleWorker && !m.narrator);
    const cleanPath = str => String(str||'').replace(/(?:~\/\.gemini\/[\w.-]+\/brain|\/(?:usr\/local\/google\/home|google\/src\/cloud))\/[^\s`'")]+/g, m=>{
      const parts=m.split('/').filter(Boolean); return parts.length>=2 ? '…/'+parts.slice(-2).join('/') : m;
    });
    const clipW = (str, maxW) => {
      const w = String(str||'').replace(/\s+/g,' ').trim().split(' ').filter(Boolean);
      return w.length > maxW ? w.slice(0, maxW).join(' ').replace(/[,;:.]+$/,'') + '…' : w.join(' ').replace(/[.:]+$/,'');
    };
    const cleanAssignment = raw => {
      let s = String(raw||'').trim();
      if(!s) return '';
      if(/^\[(?:Batched\s+)?Board\s+@mention\s+wakeup/i.test(s)) return '';
      if(/^You are\s+[`'"*]?[\w-]+[`'"*]?\s*\(Tier-/i.test(s)) return '';
      s = s.replace(/^You are\s+[`'"*]?[\w-]+[`'"*]?\s+in\s+swarm\s+[`'"*]?[\w-]+[`'"*]?(?:\s*\([^)]*\))?[.:—–\-\s]*/i, '').trim();
      if(!s) return '';
      const line = s.split(/\n/).map(l => l.trim()).find(Boolean) || '';
      return clipW(cleanPath(line), 16);
    };
    const briefOf = n => { const re=new RegExp('^@'+n.replace(/[.*+?^${}()|[\]\\-]/g,'\\$&')+'(?![\\w-])'); const p=msgs.find(m=>(m.agent||m.sender)===L && re.test(String(m.text||''))); if(!p) return '';
      let s=String(p.text).replace(/^@\S+[\s,:]*/,'').replace(/\*\*([^*]+)\*\*/g,'$1').split(/(?<=[.!?])\s|\n/)[0];
      s=s.replace(/\s*\((?:you\b|your\b)[^)]*\)/gi,'').replace(/\s*\((?:you\b|your\b)[^)]*$/gi,'');
      s=s.replace(/^(?:you\s+(?:are|own|handle|drive|run|lead|manage)|your\s+(?:role|job|task|assignment|goal|focus)\s+is(?:\s+to)?|assignment\s*:|role\s*:|task\s*:)\s*/i,'');
      const nm=n.replace(/[.*+?^${}()|[\]\\-]/g,'\\$&');
      s=s.replace(new RegExp('^[`\'"*]*@?'+nm+'(?![\\w-])[`\'"*]*\\s*(?:[,:–—-]|\\bis\\b)?\\s*','i'),'').replace(/\*\*|__|`/g,'');
      s=s.replace(/^(?:turning|making|building|auditing|fixing)\s+(?:the\s+)?/i, m=>m.charAt(0).toUpperCase()+m.slice(1));
      s=s.replace(/^(?:the|a|an|this\s+swarm's)\s+/i,'');
      s=clipW(cleanPath(s), 16);
      return s ? s.charAt(0).toUpperCase()+s.slice(1) : ''; };
    const lastPostBy = n => { for(let i=P.length-1;i>=0;i--) if(P[i].a===n) return P[i]; return null; };
    const lastRawPostBy = n => {
      for (let i = rawMsgs.length - 1; i >= 0; i--) {
        const q = rawMsgs[i];
        if ((q.control && typeof q.control === 'object') || key(q.agent || q.sender) !== n) continue;
        return { s: q.seq, t: new Date(q.ts || Date.now()).toISOString(), x: cleanText(q.text || q.summary || '') };
      }
      return null;
    };
    const hasActiveSub = (name) => {
      const m = byName.get(name) || (name === 'coordinator' ? leadM : null);
      return !!(m && !m.killed && Array.isArray(m.subagents) && m.subagents.some(sub => sub && sub.status === 'working'));
    };
    const KINDS=['decision','failure','review','stuck','quota','error','fyi'];
    const attention = (Array.isArray(vj.attention)?vj.attention:[]).filter(a=>a&&a.title).map(a=>{
      const refs=Array.isArray(a.refs)?a.refs:[]; const seqRef=refs.find(r=>r&&r.seq); const memRef=refs.find(r=>r&&r.member&&byName.has(r.member)&&byName.get(r.member).role!=='coordinator');
      const acts=Array.isArray(a.actions)?a.actions:[]; const p=seqRef?P.find(q=>q.s===seqRef.seq):null; const url=acts.find(x=>x&&x.type==='url');
      const jump=acts.find(x=>x&&x.type==='board_post'&&P.some(q=>q.s===Number(x.target)));
      return { id: a.id ? String(a.id) : null, kind:KINDS.includes(a.kind)?a.kind:'fyi', severity:a.severity||'low', title:String(a.title).slice(0,160), why:String(a.why||'').slice(0,300), from: memRef?key(memRef.member):(p?p.a:'coordinator'), seq: (seqRef && Number(seqRef.seq)) || head, post: jump ? Number(jump.target) : (p ? p.s : null), url: url?url.target:null }; }).filter(a => !(a.kind === 'stuck' && hasActiveSub(a.from)));
    (Array.isArray(snap.systemAttention)?snap.systemAttention:[]).forEach(s=>{ if(s && s.member && !isRole(s.member) && !(s.kind==='stuck' && hasActiveSub(s.member))) attention.push({id: String(s.id || `sys-${s.member}-${s.kind}`), sys: true, kind: s.kind==='quota'?'quota':s.kind==='stuck'?'stuck':'error', severity:'high', title: s.kind==='quota'?`@${s.member} ran out of quota`:s.kind==='stuck'?`@${s.member} looks stuck`:`@${s.member} hit an error`, why: String(s.text||s.detail||'').slice(0,200), from:key(s.member), seq:head}); });
    (()=>{ const items={}, members={}; const sh=x=>x&&typeof x.short==='object'&&x.short?{title:String(x.short.title||'').slice(0,60), line:String(x.short.line||'').slice(0,90)}:null;
      (Array.isArray(vj.attention)?vj.attention:[]).forEach(a=>{ const s=sh(a); if(s&&a.id) items[String(a.id)]=s; });
      (Array.isArray(vj.workstreams)?vj.workstreams:[]).forEach(w=>{ const s=sh(w); if(s&&w.id) items[String(w.id)]=s; });
      if(vj.now && typeof vj.now==='object') Object.entries(vj.now).forEach(([n,v])=>{ if(typeof v==='string') members[n===L?'coordinator':n]=v.slice(0,48); });
      window.SwarmHive = window.SwarmHive || {}; window.SwarmHive.viewCopy = {items, members, lead: members.coordinator?{doing:members.coordinator}:{} }; })();
    const askByFrom = new Map();
    const SEV = {high:0, med:1, medium:1, low:2};
    attention.sort((a,b)=>(SEV[a.severity]??2)-(SEV[b.severity]??2));
    attention.filter(a=>a.kind!=='fyi' && !ISSUE_KINDS.has(a.kind) && a.from!=='coordinator').forEach(a=>{ if(!askByFrom.has(a.from)) askByFrom.set(a.from, a); });
    const issueByFrom = new Map();
    attention.filter(a=>ISSUE_KINDS.has(a.kind)).forEach(a=>{ if(!issueByFrom.has(a.from)) issueByFrom.set(a.from, a); });
    const lastP = P[P.length-1];
    const closed = !team.some(m=>!m.killed && m.status==='working') && !attention.some(a=>a.sys) &&
      !!lastP && lastP.a==='coordinator' && /^(@[A-Za-z0-9_-]+[\s,:–—-]*)*(?:\*\*|\*|`|_)?\[done\]/i.test(lastP.x);
    const needsYou = new Set(closed ? [] : askByFrom.keys());
    const members = Object.create(null);
    const taskTitleFor = (name) => {
      const pt = planTaskByKey.get(name);
      if (pt && pt.title) return pt.title.slice(0, 120);
      const mem = byName.get(name);
      const assign = cleanAssignment(mem && mem.assignment);
      if (assign) return assign.slice(0, 120);
      const br = briefOf(name);
      if (br) return br;
      if (mem && mem.role && mem.role !== 'worker' && mem.role !== 'coordinator') {
        return mem.role.charAt(0).toUpperCase() + mem.role.slice(1);
      }
      return '';
    };
    team.forEach(m=>{ const n=key(m.name); const lp=lastPostBy(n);
      let status = m.killed ? 'parked' : (m.status==='working' ? 'working' : 'parked'); let waiting_on=null, waiting_seq=null;
      if(status==='parked' && !closed && !m.killed && lp && lp.to.length && !lp.to.includes('*') && /\?|could you|can you|please|ping me|waiting on/i.test(lp.x)){ const t=lp.to[0]; if(!P.some(q=>q.s>lp.s && q.a===t)){ status='waiting_on'; waiting_on=t; waiting_seq=lp.s; } }
      if(needsYou.has(n) && !m.killed){ status='waiting_on'; waiting_on='coordinator'; waiting_seq=askByFrom.get(n).seq; }
      const stripLead = s => String(s||'').replace(/^(@#?[A-Za-z0-9_-]+[\s,:–—-]*)+/,'').replace(/^(?:\*\*|\*|`|_)*\[(?:done|stall)\](?:\*\*|\*|`|_)*[\s:,–—-]*/i,'');
      let wake = null;
      if(!m.killed && m.status==='working' && !m.activity){
        const wakeFrom = Math.max(0, Number(m.wait_since ?? m.waitSince) || 0, lp ? lp.s : 0);
        for(let i = rawMsgs.length - 1; i >= 0 && Number(rawMsgs[i].seq) > wakeFrom; i--){
          const q = rawMsgs[i];
          const qAgent = q.agent || q.sender;
          if((q.control && typeof q.control==='object') || key(qAgent)===n) continue;
          const via = q.via_channel || q.viaChannel || [];
          if((q.mentions||[]).some(t=>key(t)===n) || via.some(t=>key(t)===n) || (q.all && !isRole(qAgent))){
            wake = {s: q.seq, a: key(qAgent), x: stripLead(cleanText(q.text || q.summary))}; break;
          }
        }
      }
      const normTool = m.currentTool && typeof m.currentTool==='object' && m.currentTool.name
        ? {name:String(m.currentTool.name), preview:String(m.currentTool.preview||''), startedAt:String(m.currentTool.startedAt||'')}
        : null;
      const activityText = m.activity || (m.status==='working' && normTool ? `Running ${fmtToolCall(normTool)}` : '');
      const rawNow = (m.status==='working' && activityText) ? String(activityText)
          : wake ? `#${wake.s} from @${wake.a}` + (wake.x ? `: ${wake.x}` : '')
          : (lp ? stripLead(lp.x) : 'No posts yet');
      const now = cleanPath(rawNow).replace(/`([^`]+)`/g,'$1').slice(0,180);
      const live = (m.status==='working' && !m.killed && !m.failure && activityText && (status==='working' || n==='coordinator'))
        ? {text: cleanPath(String(activityText)).replace(/`([^`]+)`/g,'$1').slice(0,180), kind: m.activityKind==='thinking' ? 'thinking' : 'tool'}
        : null;
      const ask = !m.killed ? (askByFrom.get(n) || null) : null;
      const rawIssue = !m.killed ? (issueByFrom.get(n) || null) : null;
      const issue = (rawIssue && rawIssue.kind === 'stuck' && hasActiveSub(m.name)) ? null : rawIssue;
      const hostObj = m.host && m.host.label ? {label:String(m.host.label), kind:String(m.host.kind||'')} : null;
      const wsMode = typeof (m.workspace_mode ?? m.workspaceMode) === 'string' ? String(m.workspace_mode ?? m.workspaceMode).trim() : '';
      const lastSeenVal = m.last_seen ?? m.lastSeen;
      members[n] = {
        rawName:m.name, role:m.role,
        title: n==='coordinator' ? 'Coordinator · Antigravity' : taskTitleFor(m.name),
        model:m.model||'', status, waiting_on, waiting_seq, waiting_for:'', now, live, ask, issue,
        steps:Number(m.steps)||0, toolCalls:Number(m.toolCalls)||0,
        currentTool: normTool,
        recentTools: Array.isArray(m.recentTools) ? m.recentTools.slice(0, 12) : [],
        tokens: m.tokens && typeof m.tokens==='object' ? m.tokens : null,
        tps: Number(m.tps)||0,
        tpsEstimated: !!m.tpsEstimated,
        elapsedMs: Number(m.elapsedMs)||0,
        costUsd: m.costUsd!=null && Number.isFinite(Number(m.costUsd)) ? Number(m.costUsd) : null,
        posts:m.posts||0, killed:!!m.killed,
        lastPostSeq: Number(m.lastPostSeq ?? m.last_post_seq) || 0,
        lastReadSeq: Number(m.lastReadSeq ?? m.last_read_seq) || 0,
        replied: !!m.replied, replyAsk: m.replyAsk ? String(m.replyAsk) : '',
        last: lp?{seq:lp.s,time:lp.t,text:cleanPath(lp.x)}:null,
        host: hostObj, local: m.local!==false,
        workspace: typeof m.workspace==='string' ? m.workspace.trim() : '', workspace_mode: wsMode,
        last_seen: Number.isFinite(lastSeenVal) ? lastSeenVal : null,
        channels: Array.isArray(m.channels) ? m.channels.map(String) : [],
        subagents: Array.isArray(m.subagents) ? m.subagents : [],
      };
    });
    if(!members.coordinator) members.coordinator = { rawName:L, role:'coordinator', title:'Coordinator · Antigravity', model:'', status:'parked', now:'', ask:null, steps:0, toolCalls:0, currentTool:null, recentTools:[], tokens:null, tps:0, tpsEstimated:false, elapsedMs:0, costUsd:null, posts:0, lastPostSeq:0, lastReadSeq:0, last:null, subagents:[] };
    const roleBounds = Object.create(null);
    rawMsgs.forEach(q => {
      if (!q || (q.control && typeof q.control === 'object')) return;
      const qName = key(q.agent || q.sender);
      if (!isRole(qName)) return;
      const sq = Number(q.seq) || 0;
      if (!sq) return;
      if (!roleBounds[qName]) roleBounds[qName] = { firstSeq: sq, lastSeq: sq, seqs: [] };
      roleBounds[qName].firstSeq = Math.min(roleBounds[qName].firstSeq, sq);
      roleBounds[qName].lastSeq = Math.max(roleBounds[qName].lastSeq, sq);
      roleBounds[qName].seqs.push(sq);
    });
    rawMembers.filter(m=>m.roleWorker||m.narrator).forEach(m=>{
      const n = key(m.name);
      const lp = lastRawPostBy(n);
      const roleName = m.role || (m.narrator ? 'narrator' : 'role');
      const st = m.killed ? 'parked' : (m.status === 'working' ? 'working' : 'parked');
      const defaultNow = roleName === 'narrator' ? 'Maintaining overview view.json' : 'Watching board for stalls';
      const normTool = m.currentTool && typeof m.currentTool==='object' && m.currentTool.name
        ? {name:String(m.currentTool.name), preview:String(m.currentTool.preview||''), startedAt:String(m.currentTool.startedAt||'')}
        : null;
      const activityText = m.activity || (m.status==='working' && normTool ? `Running ${fmtToolCall(normTool)}` : '');
      const rawNow = (m.status === 'working' && activityText) ? String(activityText) : (lp ? lp.x : defaultNow);
      const live = (!m.killed && m.status === 'working' && !m.failure && activityText)
        ? { text: cleanPath(String(activityText)).replace(/`([^`]+)`/g, '$1').slice(0, 180), kind: m.activityKind === 'thinking' ? 'thinking' : 'tool' }
        : null;
      members[n] = {
        system: true,
        roleWorker: true,
        rawName: m.name,
        name: m.name,
        role: roleName,
        title: cleanAssignment(m.assignment) || (roleName === 'narrator' ? 'Overview narrator' : roleName === 'monitor' ? 'Stall monitor' : roleName),
        model: m.model || '',
        status: st,
        killed: !!m.killed,
        replied: !!m.replied,
        replyAsk: m.replyAsk ? String(m.replyAsk) : '',
        now: cleanPath(rawNow).replace(/`([^`]+)`/g, '$1').slice(0, 180),
        live,
        steps: Number(m.steps) || 0,
        toolCalls: Number(m.toolCalls) || 0,
        currentTool: normTool,
        recentTools: Array.isArray(m.recentTools) ? m.recentTools.slice(0, 12) : [],
        tokens: m.tokens && typeof m.tokens==='object' ? m.tokens : null,
        tps: Number(m.tps) || 0,
        tpsEstimated: !!m.tpsEstimated,
        elapsedMs: Number(m.elapsedMs) || 0,
        costUsd: m.costUsd!=null && Number.isFinite(Number(m.costUsd)) ? Number(m.costUsd) : null,
        posts: m.posts || 0,
        last: lp ? { seq: lp.s, time: lp.t, text: cleanPath(lp.x) } : null,
        channels: Array.isArray(m.channels) ? m.channels.map(String) : [],
        subagents: Array.isArray(m.subagents) ? m.subagents : [],
      };
    });

    const isDonePost = lp => !!(lp && /^(@[A-Za-z0-9_-]+[\s,:–—-]*)*(?:\*\*|\*|`|_)?\[done\]/i.test(lp.x));
    const finished = closed;
    const STATES=['queued','active','blocked','review','done','failed'];
    const parseProg = (w)=>{
      if(w.progress && typeof w.progress==='object' && Number(w.progress.total)>0){
        const tot=Math.max(1,Number(w.progress.total)||1), dn=Math.max(0,Math.min(tot,Number(w.progress.done)||0));
        return {done:dn, total:tot, label:String(w.progress.label||(dn+'/'+tot)).slice(0,24)};
      }
      if(typeof w.progress==='number' && w.progress>=0 && w.progress<=100){
        const p=Math.round(w.progress); return {done:p, total:100, label:p+'%'};
      }
      const fm=/(?<!cl\/)(?<!\d\.)\b(\d{1,2})\s*(?:\/|of)\s*(\d{1,2})\b(?!\s*(?:AM|PM|am|pm))/.exec(String(w.note||''));
      if(fm){ const d=+fm[1], t=+fm[2]; if(t>=2 && t<=60 && d<=t) return {done:d, total:t, label:`${d}/${t}`}; }
      return null;
    };
    const rawAssignees = w => {
      if (Array.isArray(w.assignees)) return w.assignees;
      if (Array.isArray(w.owners)) return w.owners;
      if (w.assignee) return [w.assignee];
      if (typeof w.id === 'string' && w.id.startsWith('ws-') && byName.has(w.id.slice(3))) return [w.id.slice(3)];
      return [];
    };
    const normSeqs = refs => (Array.isArray(refs) ? refs : []).map(r => {
      if (typeof r === 'string') {
        const m = r.trim().match(/^#?(\d{1,9})$/);
        return m ? Number(m[1]) : 0;
      }
      return r && Number(r.seq) ? Number(r.seq) : 0;
    }).filter(Boolean);
    let workstreams = (Array.isArray(vj.workstreams)?vj.workstreams:[]).filter(w=>w&&(w.title||w.task)).map((w,i)=>{
      const seqs = normSeqs(w.refs);
      const rawState0=STATES.includes(w.state)?w.state:'active';
      const rawState=(finished && rawState0!=='failed') ? 'done' : rawState0;
      return { id:w.id||('w'+i), title:String(w.title||w.task).slice(0,120), assignees:rawAssignees(w).filter(o=>byName.has(o)).map(key), state: rawState==='failed'?'blocked':rawState, rawState, progress:parseProg(w), note:String(w.note||w.evidence||'').slice(0,200), seq: seqs.length?Math.max(...seqs):head };
    });
    workstreams.forEach(w=>{ if(!w.assignees.length) w.assignees=['coordinator']; });
    const fallbackState = (x, lp) => x.killed ? 'done' : x.status==='waiting_on' ? 'blocked' :
        (x.status==='working' || x.status==='needs_you') ? 'active' : lp ? ((finished || isDonePost(lp)) ? 'done' : 'review') : 'queued';
    if(!workstreams.length) {
      if (planTaskList.length && planTaskList.every(t => t && t.title)) {
        workstreams = planTaskList.map(t => {
          const assigneeName = t.assignee || t.assignees[0] || t.id;
          const n = key(assigneeName);
          const mObj = members[n];
          const lp = lastPostBy(n) || lastRawPostBy(n);
          const rawSt = STATES.includes(t.status) ? t.status : (mObj ? fallbackState(mObj, lp) : 'queued');
          return {
            id: t.id,
            title: String(t.title).slice(0, 120),
            assignees: byName.has(assigneeName) ? [n] : ['coordinator'],
            state: rawSt === 'failed' ? 'blocked' : rawSt,
            rawState: rawSt,
            progress: parseProg(t),
            note: String(t.note || (mObj ? mObj.now : '')).slice(0, 200),
            seq: lp ? lp.s : head,
          };
        });
      } else {
        workstreams = team.filter(m=>m.role!=='coordinator').map(m=>{ const n=key(m.name); const lp=lastPostBy(n); const st=fallbackState(members[n], lp); return {id:n, title: taskTitleFor(m.name) || 'Task', assignees:[n], state: st, rawState: st, blocked_on: members[n].waiting_on||null, note: members[n].now, seq: lp?lp.s:head}; });
      }
    }
    if (Array.isArray(vj.threads) && vj.threads.length) {
      const existingIds = new Set(workstreams.map(w => w.id));
      vj.threads.forEach(t => {
        if (!Array.isArray(t.workstreams)) return;
        t.workstreams = t.workstreams.map(wid => {
          if (existingIds.has(wid)) return wid;
          const matched = workstreams.find(w => w.assignees.includes(wid));
          if (matched) return matched.id;
          const pt = planTaskByKey.get(wid);
          const mObj = members[wid];
          if (!pt && !mObj) return wid;
          const lp = lastPostBy(wid) || lastRawPostBy(wid);
          const rawSt = (pt && STATES.includes(pt.status)) ? pt.status : (mObj ? fallbackState(mObj, lp) : 'queued');
          existingIds.add(wid);
          workstreams.push({
            id: wid,
            title: String((pt && pt.title) || taskTitleFor(wid) || (mObj && mObj.title) || wid).slice(0, 120),
            assignees: byName.has(wid) ? [key(wid)] : ['coordinator'],
            state: rawSt === 'failed' ? 'blocked' : rawSt,
            rawState: rawSt,
            progress: pt ? parseProg(pt) : null,
            note: String((pt && pt.note) || (mObj ? mObj.now : '')).slice(0, 200),
            seq: lp ? lp.s : head,
          });
          return wid;
        });
      });
    }
    Object.entries(members).forEach(([n,x])=>{
      if(!x.system && n!=='coordinator' && !x.title){
        const ow = workstreams.filter(w=>w.assignees.includes(n));
        if(ow.length) x.title = clipW(ow[ow.length-1].title, 7);
      }
    });
    const done = workstreams.filter(w=>w.state==='done').length;
    const coordAsk = (!finished && leadM && !leadM.killed && leadM.status==='waiting' && leadM.replied && leadM.replyAsk) ? String(leadM.replyAsk) : '';
    const coordReplied = !!coordAsk;
    attention.forEach(a=>{ a.wait = a.kind==='fyi' ? null
      : (a.from==='coordinator' && String(a.id||'').startsWith('sys-')) ? 'you'
      : ISSUE_KINDS.has(a.kind) ? 'issue' : 'coordinator'; });
    if(coordReplied) attention.unshift({id:'sys-coordinator-reply', kind:'decision', severity:'high', title:coordAsk, full:true, why:'The coordinator asked. Answer it in its chat.', from:'coordinator', seq:head, url:null, wait:'you'});
    const youItems = finished ? [] : attention.filter(a=>a.wait==='you');
    if(youItems.length && members.coordinator){ members.coordinator.status='needs_you'; members.coordinator.ask=youItems[0]; }

    const hasLiveWorkers = team.some(m=>m.role!=='coordinator' && !m.killed);
    const isMonitor = a => byName.get(a)?.role==='monitor' || /^monitor(-\d+)?$/.test(a);
    const monPosts = rawMsgs.filter(m=>m && !(m.control && typeof m.control==='object') && isMonitor(m.agent||m.sender) && /\[stall\]/i.test(String(m.text||'')));
    const lastMonPost = monPosts.length ? monPosts[monPosts.length-1] : null;
    const fmtStall = raw => {
      let s = cleanPath(String(raw||''))
          .replace(/^\s*(?:[-*]|#+)\s+/gm,'')
          .replace(/\*\*([^*]+)\*\*/g,'$1')
          .replace(/`([^`]+)`/g,'$1')
          .replace(/[*`_]*\[stall\][*`_]*[\s:,–—-]*/gi,' ')
          .replace(/\s+/g,' ')
          .trim();
      s = s.replace(/^((?:@[#A-Za-z0-9_-]+(?![#A-Za-z0-9_-])[\s,:–—-]*)+)(?=\S)(?!@[#A-Za-z0-9_-])/, (_, m) => m.match(/@[#A-Za-z0-9_-]+/g).join(' ') + ': ');
      return s.slice(0, 220);
    };
    const coordLastRead = Number(leadM?.last_read_seq ?? leadM?.lastReadSeq) || 0;
    const coordHandledStall = !!(leadM && !leadM.killed && leadM.status==='waiting' && leadM.replied && lastMonPost && coordLastRead >= (Number(lastMonPost.seq)||0));
    const lastMon = (!closed && !team.some(m=>!m.killed && m.status==='working') && !youItems.length && !coordHandledStall && hasLiveWorkers && lastMonPost && (!lastP || lastMonPost.seq >= lastP.s)) ? lastMonPost : null;
    const stall = lastMon ? { seq: Number(lastMon.seq)||0, agent: String(lastMon.agent||lastMon.sender||'monitor'), text: fmtStall(lastMon.text) } : null;
    const nowIso = new Date().toISOString();
    const planNow = ids => { const x = ids.map(n=>members[n]).find(m=>m && !m.killed && m.status==='working'); return x ? clipW(x.now, 8) : ''; };
    const titleStr = snap.title || snap.meta?.title || 'Untitled swarm';
    return { title: titleStr,
      headline: vj.headline ? String(vj.headline).slice(0,220) : (plan && vj.goal && vj.goal.line) ? String(vj.goal.line).slice(0,220) : (metaGoal ? String(metaGoal).slice(0,220) : ''),
      phase: vj.phase && vj.phase.label ? {label:String(vj.phase.label).slice(0,40), index:Number(vj.phase.index)||1, total:Number(vj.phase.total)||1} : {label: finished?'Done':'In progress', index:1, total:1},
      done, total: workstreams.length, as_of: head, board_head: head, now: nowIso, started: P.length?P[0].t:nowIso, narrator_seq: Number(vj.as_of_seq)||head, finished, stall,
      builtin: snap.builtinOverview === true,
      starting: snap.builtinOverview === true && !finished && !narratorView && !attention.length && !stall && !P.some(p=>p.a!=='coordinator') && !team.some(m=>m.role!=='coordinator'),
      workerStarted: team.some(m=>m.role!=='coordinator' && (m.steps || m.posts || m.status==='working')),
      latest: lastP?{agent:lastP.a,seq:lastP.s,time:lastP.t,text:lastP.x}:{agent:'coordinator',seq:0,time:nowIso,text:''},
      attention, workstreams, members, roleBounds,
      telemetry: snap.telemetry && typeof snap.telemetry==='object' ? snap.telemetry : null,
      source: snap.source && typeof snap.source==='object' ? snap.source : null,
      reel: (()=>{ const fr=(vj.reel && Array.isArray(vj.reel.frames) && vj.reel.frames.length) ? vj.reel.frames : (Array.isArray(snap.frames) ? snap.frames : []);
        const urlConv=new URLSearchParams(location.search).get('conversationId')||'';
        const out=fr.filter(f=>f && typeof f.path==='string' && f.path.trim()).slice(0,40).map(f=>{
          const q=new URLSearchParams({ path: f.path, seq: String(Number(f.seq)||0) });
          if (urlConv) q.set('conversationId', urlConv);
          return { src:`./api/frame?${q}`, seq:Number(f.seq)||0, caption:String(f.caption||'').slice(0,90) };
        });
        return out.length>=1 ? out : null; })(),
      goal: vj.goal && typeof vj.goal==='object' && vj.goal.title ? {title:String(vj.goal.title).slice(0,80), line:String(vj.goal.line||'').slice(0,120)} : null,
      fromPlan: !!plan,
      runningSummary: vj.running_summary ? String(vj.running_summary).slice(0,200) : '',
      threads: (Array.isArray(vj.threads)?vj.threads:[]).filter(t=>t&&t.title&&Array.isArray(t.workstreams)).slice(0,12).map(t=>({id:String(t.id||t.title).slice(0,48), title:String(t.title).slice(0,40), goal:String(t.goal||'').slice(0,100), now:String(t.now||(plan ? planNow(t.workstreams) : '')).slice(0,80), state:String(t.state||''), workstreams:t.workstreams.map(String), attention:(Array.isArray(t.attention)?t.attention:[]).map(String)})),
      hiveThreads: (Array.isArray(vj.threads)?vj.threads:[]).filter(t=>t&&t.title&&Array.isArray(t.workstreams)).slice(0,400).map(t=>({id:String(t.id||t.title).slice(0,48), title:String(t.title).slice(0,40), goal:String(t.goal||'').slice(0,100), now:String(t.now||(plan ? planNow(t.workstreams) : '')).slice(0,80), state:String(t.state||''), workstreams:t.workstreams.map(String), attention:(Array.isArray(t.attention)?t.attention:[]).map(String)})),
    };
  }

  /* ---------- Hive map (Graph tab) ---------- */
  const HV = {host: null, root: null, key: '', swarm: null, beam: null, ro: null, args: null};
  const HV_RING = [[0,-1],[1,-1],[1,0],[0,1],[-1,1],[-1,0]];
  const HV_FEW = {1:[0], 2:[5,1], 3:[0,2,4], 4:[5,1,4,2], 5:[5,0,1,4,2]};
  const HV_ASK = /\?|could you|can you|please|ping me|waiting on/i;
  const HV_BUSY_MS = 45 * 60 * 1000, HV_ASK_MS = 6 * 3600 * 1000;

  const HV_REPLAY = { msgs: null, len: 0, fin: null, i: -1, born: null, lastPost: null, lastIn: null };
  function hiveStates(sc, seq) {
    const out = Object.create(null), names = Object.keys(sc.members).filter(n => !sc.members[n].system || sc.members[n].roleWorker);
    const owned = n => sc.workstreams.filter(w => w.assignees.includes(n));
    if (seq == null) {
      names.forEach(n => { const x = sc.members[n]; let st = 'idle', who = null;
        if (x.killed) st = 'retired';
        else if (sc.finished) st = n === 'coordinator' ? 'idle' : 'done';
        else if (x.issue) st = 'issue';
        else if (x.status === 'working') st = 'working';
        else if (x.status === 'waiting_on') { st = 'wait'; who = x.waiting_on; }
        else if (n !== 'coordinator' && owned(n).length && owned(n).every(w => w.state === 'done')) st = 'done';
        out[n] = {st, who}; });
      if (!sc.finished && out.coordinator && out.coordinator.st !== 'retired' && sc.attention.some(a => a.wait === 'you')) out.coordinator = {st: 'needs', who: null};
      const lp = P[P.length - 1];
      return {st: out, done: null, beam: lp && Date.now() - Date.parse(lp.t) < 90 * 1000 ? lp : null};
    }
    let i = -1; while (i + 1 < P.length && P[i + 1].s <= seq) i++;
    const tms = k => Date.parse(P[k].t);
    const to = p => p.to.includes('*') ? [] : p.to;
    if (HV_REPLAY.msgs !== P || HV_REPLAY.len !== P.length || i < HV_REPLAY.i) {
      const fin = Object.create(null);
      P.forEach((p, k) => { fin[p.a] = k; });
      HV_REPLAY.msgs = P;
      HV_REPLAY.len = P.length;
      HV_REPLAY.fin = fin;
      HV_REPLAY.i = -1;
      HV_REPLAY.born = Object.assign(Object.create(null), {coordinator: true});
      HV_REPLAY.lastPost = Object.create(null);
      HV_REPLAY.lastIn = Object.create(null);
    }
    const { fin, born, lastPost, lastIn } = HV_REPLAY;
    for (let k = HV_REPLAY.i + 1; k <= i; k++) {
      const p = P[k];
      born[p.a] = true;
      lastPost[p.a] = k;
      to(p).forEach(n => { born[n] = true; lastIn[n] = k; });
    }
    HV_REPLAY.i = i;
    const now = i >= 0 ? tms(i) : 0;
    names.forEach(n => { const x = sc.members[n];
      if (x.roleWorker) {
        const rb = sc.roleBounds && sc.roleBounds[n];
        let st = 'idle';
        if (seq === 0 && !rb && x.status !== 'working' && !x.posts) st = 'unborn';
        else if (x.killed && (!rb || seq >= rb.lastSeq)) st = 'retired';
        else if (rb && rb.seqs.includes(seq)) st = 'working';
        else if (x.status === 'working') st = 'working';
        else if (owned(n).length && owned(n).every(w => w.state === 'done')) st = 'done';
        out[n] = {st, who: null};
        return;
      }
      const lp = lastPost[n] ?? -1, li = lastIn[n] ?? -1; let st = 'idle', who = null;
      if (!born[n]) st = 'unborn';
      else if (x.killed && (fin[n] != null ? (i >= fin[n] && (i > fin[n] + 8 || now - tms(fin[n]) > HV_BUSY_MS)) : i > li + 8)) st = 'retired';
      else if (li > lp && now - tms(li) < HV_BUSY_MS) { st = 'working'; who = P[li].a; }
      else if (lp >= 0 && HV_ASK.test(P[lp].x) && to(P[lp]).length && now - tms(lp) < HV_ASK_MS && (lastPost[to(P[lp])[0]] ?? -1) <= lp) { st = 'wait'; who = to(P[lp])[0]; }
      else if (lp >= 0 && fin[n] <= i && n !== 'coordinator') st = 'done';
      else if (lp >= 0 && i - lp < 3 && now - tms(lp) < 4 * 60 * 1000) st = 'working';
      out[n] = {st, who: who === n ? null : who}; });
    return {st: out, done: n => out[n] && (out[n].st === 'retired' || (sc.members[n]?.roleWorker ? out[n].st === 'done' : (fin[n] != null && fin[n] <= i))), beam: i >= 0 ? P[i] : null};
  }

  function hiveCells(sc, hs) {
    const workers = Object.keys(sc.members).filter(n => n !== 'coordinator' && hs.st[n] && (!sc.members[n].system || sc.members[n].roleWorker));
    const wsById = new Map(sc.workstreams.map(w => [w.id, w]));
    const wsDone = w => hs.done ? w.assignees.every(o => o === 'coordinator' || hs.done(o)) : w.state === 'done';
    const settled = n => ['done', 'retired'].includes(hs.st[n].st);
    const cells = [];
    const threads = sc.hiveThreads || sc.threads;
    if (threads.length) {
      threads.forEach(t => { const ws = t.workstreams.map(id => wsById.get(id)).filter(Boolean);
        cells.push({id: t.id, title: t.title, goal: t.goal || '', ws, rawRefs: new Set(t.workstreams), bees: [], total: ws.length, done: ws.filter(wsDone).length}); });
      const homed = new Set();
      workers.forEach(n => { const mine = cells.filter(c => c.ws.some(w => w.assignees.includes(n)) || c.rawRefs.has(n)); if (!mine.length) return;
        (mine.find(c => c.ws.some(w => w.assignees.includes(n) && !wsDone(w))) || mine[0]).bees.push(n); homed.add(n); });
      const rest = workers.filter(n => !homed.has(n) && !sc.members[n].roleWorker);
      if (rest.length) cells.push({id: '_other', title: 'Other', bees: rest, total: rest.length, done: rest.filter(settled).length});
      const roleRest = workers.filter(n => !homed.has(n) && sc.members[n].roleWorker);
      if (roleRest.length) cells.push({id: '_role_workers', title: 'Role workers', bees: roleRest, total: roleRest.length, done: roleRest.filter(settled).length});
    } else {
      const stdWorkers = workers.filter(n => !sc.members[n].roleWorker);
      [['Working', n => hs.st[n].st === 'working'], ['Waiting', n => ['wait', 'issue'].includes(hs.st[n].st)],
       ['Resting', n => hs.st[n].st === 'idle'], ['Done', settled]].forEach(([title, pick]) => {
        const bees = stdWorkers.filter(pick); if (bees.length) cells.push({id: '_' + title, title, bees, total: bees.length, done: title === 'Done' ? bees.length : 0, count: bees.length}); });
      const roleWorkers = workers.filter(n => sc.members[n].roleWorker);
      if (roleWorkers.length) cells.push({id: '_role_workers', title: 'Role workers', bees: roleWorkers, total: roleWorkers.length, done: roleWorkers.filter(settled).length, count: roleWorkers.length});
    }
    return cells.slice(0, 400);
  }

  function hiveSlots(n) {
    if (n < 1) return [];
    if (n < 6) return HV_FEW[n].map(k => HV_RING[k]);
    const out = [], D = [[1,0],[0,1],[-1,1],[-1,0],[0,-1],[1,-1]];
    for (let k = 1; out.length < n; k++) { let q = 0, r = -k; D.forEach(([dq, dr]) => { for (let j = 0; j < k; j++) { out.push([q, r]); q += dq; r += dr; } }); }
    return out.slice(0, n);
  }

  function hiveDraw() {
    const {root} = HV, sc = S.live; if (!root || !sc || !HV.args) return;
    const stage = root.querySelector('.hv-stage'), svg = root.querySelector('.hv-svg'), layer = root.querySelector('.hv-bees'), sum = root.querySelector('.hv-sum'), view = root.querySelector('.hv-view');
    const W = Math.floor(root.querySelector('.hv').clientWidth); if (W < 40) return;
    const seq = HV.args.seq, key = `${S.etag}|${seq}|${W}|${HV.zoom}|${HV.sel}`;
    if (key === HV.key) return;
    HV.key = key;
    const hs = hiveStates(sc, seq), cells = hiveCells(sc, hs), K = Math.sqrt(3) / 2;
    const slots = hiveSlots(cells.length);
    const selC = cells.find(c => c.id === HV.sel) || null; if (!selC) HV.sel = null;
    HV.cellIds = cells.map(c => c.id);
    const ux = q => 1.5 * q, uy = (q, r) => 2 * K * (r + q / 2);
    const xs = [0, ...slots.map(([q]) => ux(q))], ys = [0, ...slots.map(([q, r]) => uy(q, r))];
    const spanX = Math.max(...xs) - Math.min(...xs) + 2, spanY = Math.max(...ys) - Math.min(...ys) + 2 * K;
    const fit = (W - 8) / spanX, gap = fit < 44 ? 3 : 7;
    const Rfit = Math.max(12, Math.min(108, fit - gap / 2)), zmax = 108 / Rfit;
    HV.zmax = zmax; HV.zoom = Math.max(1, Math.min(zmax, HV.zoom));
    HV.key = `${S.etag}|${seq}|${W}|${HV.zoom}|${HV.sel}`;
    const R = Math.min(108, Rfit * HV.zoom), G = R + gap / 2;
    const mode = R >= 76 ? 'full' : R >= 40 ? 'mid' : 'tiny';
    const CW = Math.max(W, Math.ceil(spanX * G + 8));
    const ox = CW / 2 - (Math.max(...xs) + Math.min(...xs)) / 2 * G, oy = 4 + K * R - Math.min(...ys) * G, H = Math.ceil(spanY * G + 8);
    const hex = (x, y, r, a0) => { let d = ''; for (let k = 0; k < 6; k++) { const a = (a0 + 60 * k) * Math.PI / 180; d += (k ? 'L' : 'M') + (x + r * Math.cos(a)).toFixed(1) + ' ' + (y + r * Math.sin(a)).toFixed(1); } return d + 'Z'; };
    const wrap = (t, max) => { const out = []; let cur = ''; String(t).split(' ').forEach(w => { if (cur && (cur + ' ' + w).length > max) { out.push(cur); cur = w; } else cur = (cur + ' ' + w).trim(); }); if (cur) out.push(cur); if (out.length > 2) { out.length = 2; out[1] = out[1].replace(/.$/, '…'); } return out; };
    const clip = (t, max) => { const s = String(t); return s.length > max ? s.slice(0, Math.max(1, max - 1)).trimEnd() + '…' : s; };
    const names = R >= 92, pos = {}, tucked = new Set();
    const agg = c => { const s = c.bees.map(n => hs.st[n].st); return s.includes('issue') ? 'bad' : s.includes('wait') ? 'warn' : s.includes('working') ? 'busy' : ''; };
    let cellSvg = '', textSvg = '', selSvg = '';
    const cell = (x, y, f, cls, tip, k) => { cellSvg += `<path class="hv-cell${f >= 1 && !/busy|warn|bad/.test(cls) ? ' full' : ''}${cls ? ' ' + cls : ''}" data-c="${k}" d="${hex(x, y, R, 0)}"><title>${esc(tip)}</title></path>` + (f > 0 && f < 1 || (f >= 1 && mode !== 'tiny') ? `<path class="hv-prog" pathLength="1" stroke-dasharray="${Math.min(1, f).toFixed(3)} 1" d="${hex(x, y, R - 2, -120)}"/>` : ''); };
    const allDone = hs.done ? cells.reduce((a, c) => a + c.done, 0) : sc.done, allTot = hs.done ? cells.reduce((a, c) => a + c.total, 0) : sc.total;
    const you = hs.st.coordinator && hs.st.coordinator.st === 'needs';
    cell(ox, oy, allTot ? allDone / allTot : 0, mode === 'tiny' ? 'hub' : '', `Coordinator · ${allDone} of ${allTot} done${you ? ' · ' + ui('needs') : ''}`, -1);
    if (mode === 'full') {
      pos.coordinator = {x: ox, y: oy - R * .12, size: 'q'};
      textSvg += `<text class="hv-t" x="${ox}" y="${(oy + R * .44).toFixed(1)}">Coordinator</text><text class="hv-n${you ? ' warn' : ''}" x="${ox}" y="${(oy + R * .44 + 15).toFixed(1)}">${you ? ui('needs') : `${allDone} of ${allTot} done`}</text>`;
    } else if (mode === 'mid') {
      pos.coordinator = {x: ox, y: oy - 6, size: 'm'};
      textSvg += `<text class="hv-n sm${you ? ' warn' : ''}" x="${ox}" y="${(oy + R * .62).toFixed(1)}">${you ? ui('needs') : `${allDone}/${allTot}`}</text>`;
    } else pos.coordinator = {x: ox, y: oy, size: R >= 26 ? 'm' : 'xs'};
    cells.forEach((c, k) => { const [q, r] = slots[k], x = ox + ux(q) * G, y = oy + uy(q, r) * G;
      const tip = `${c.title} · ${c.count ?? (c.total ? `${c.done}/${c.total} done` : 'no tasks')}${c.bees.length ? ' · ' + c.bees.slice(0, 6).map(n => '@' + n).join(', ') + (c.bees.length > 6 ? ` +${c.bees.length - 6}` : '') : ''}`;
      cell(x, y, c.total ? c.done / c.total : c.done, (mode === 'tiny' ? agg(c) : '') + (c === selC ? ' sel' : ''), tip, k);
      if (c === selC) selSvg = `<path class="hv-selring" d="${hex(x, y, R + 1.5, 0)}"/>`;
      if (mode === 'tiny') {
        const pick = R >= 18 && (c.bees.find(n => hs.st[n].st === 'issue') || c.bees.find(n => hs.st[n].st === 'wait') || c.bees.find(n => hs.st[n].st === 'working'));
        c.bees.forEach(n => { if (n === pick) pos[n] = {x, y, size: 'xs'}; else { pos[n] = {x, y, cell: true}; tucked.add(n); } });
        return;
      }
      const rank = n => ({issue: 0, wait: 1, working: 2})[hs.st[n].st] ?? 3;
      const born = c.bees.filter(n => hs.st[n].st !== 'unborn').sort((a, b) => rank(a) - rank(b));
      c.bees.forEach(n => { if (!born.includes(n)) pos[n] = {x, y, size: 'xs'}; });
      if (mode === 'mid') {
        const ty = y - K * R + 15;
        textSvg += `<text class="hv-t sm" x="${x.toFixed(1)}" y="${ty.toFixed(1)}">${esc(clip(c.title, Math.floor((R + 8) / 6.2)))}</text><text class="hv-n sm" x="${x.toFixed(1)}" y="${(ty + 12).toFixed(1)}">${c.count ?? `${c.done}/${c.total}`}</text>`;
        const show = born.length > 3 ? born.slice(0, 2) : born, by = y + R * .34;
        show.forEach((n, j) => { pos[n] = {x: x + (j - (show.length - 1 + (show.length < born.length ? 1 : 0)) / 2) * 24, y: by, size: 'xs'}; });
        if (show.length < born.length) { textSvg += `<text class="hv-n sm" x="${(x + 24).toFixed(1)}" y="${(by + 4).toFixed(1)}">+${born.length - show.length}</text>`;
          born.slice(show.length).forEach(n => { pos[n] = {x: x + 24, y: by, size: 'xs'}; tucked.add(n); }); }
        return;
      }
      const lines = wrap(c.title, R >= 92 ? 15 : 12), ty = y - K * R + (lines.length > 1 ? 20 : 26);
      lines.forEach((l, j) => { textSvg += `<text class="hv-t" x="${x.toFixed(1)}" y="${(ty + j * 14).toFixed(1)}">${esc(l)}</text>`; });
      textSvg += `<text class="hv-n" x="${x.toFixed(1)}" y="${(ty + lines.length * 14).toFixed(1)}">${c.count ?? `${c.done}/${c.total}`}</text>`;
      const per = Math.max(2, Math.min(3, Math.floor(1.6 * R / 50))), cap = per * 2, show = born.length > cap ? born.slice(0, cap - 1) : born;
      const sw = Math.min(58, 1.66 * R / per), rows = show.length > per ? [show.slice(0, per), show.slice(per)] : [show], rg = names ? 54 : 40;
      const top = ty + lines.length * 14 + 8, bot = y + K * R - (names ? 16 : 6), y0 = (top + bot) / 2 - (rows.length - 1) * rg / 2 - (names ? 6 : 0);
      rows.forEach((rw, ri) => rw.forEach((n, j) => { pos[n] = {x: x + (j - (rw.length - 1) / 2) * sw, y: y0 + ri * rg, size: 'm'}; }));
      if (show.length < born.length) { const lr = rows[rows.length - 1], px = x + ((lr.length + 1) / 2) * sw - sw / 2 + 4, py = y0 + (rows.length - 1) * rg;
        textSvg += `<text class="hv-n" x="${px.toFixed(1)}" y="${(py + 4).toFixed(1)}">+${born.length - show.length}</text>`;
        born.slice(show.length).forEach(n => { pos[n] = {x: px, y: py, size: 'xs'}; tucked.add(n); }); } });
    const beam = hs.beam, fresh = !!beam && HV.beam !== beam.s, still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const head = (fx, fy, x, y) => { const l = Math.hypot(x - fx, y - fy) || 1, tx = (x - fx) / l, ty = (y - fy) / l, bx = x - tx * 10, by = y - ty * 10;
      return `<path class="hv-ah" d="M${x.toFixed(1)} ${y.toFixed(1)}L${(bx - ty * 3.8).toFixed(1)} ${(by + tx * 3.8).toFixed(1)}L${(bx + ty * 3.8).toFixed(1)} ${(by - tx * 3.8).toFixed(1)}Z"/>`; };
    const PAD = {q: 36, m: 24, xs: 13}, padOf = p => p.cell ? R * .5 : PAD[p.size];
    const drawn = new Set();
    const link = (a, b, cls) => { const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1, pa = padOf(a), pb = padOf(b);
      if (len < pa + pb + 6) return '';
      const id = `${cls}|${a.x.toFixed(0)},${a.y.toFixed(0)}|${b.x.toFixed(0)},${b.y.toFixed(0)}`; if (drawn.has(id)) return ''; drawn.add(id);
      const vx = dx / len, vy = dy / len, x1 = a.x + vx * pa, y1 = a.y + vy * pa, x2 = b.x - vx * pb, y2 = b.y - vy * pb, bend = Math.min(26, (len - pa - pb) * .2);
      const mx = (x1 + x2) / 2 - vy * bend, my = (y1 + y2) / 2 + vx * bend, short = len - pa - pb < 30;
      const cut = cls === 'wait' && !short ? 9 : 0, hl = Math.hypot(x2 - mx, y2 - my) || 1, ex = x2 - (x2 - mx) / hl * cut, ey = y2 - (y2 - my) / hl * cut;
      const d = `M${x1.toFixed(1)} ${y1.toFixed(1)} Q${mx.toFixed(1)} ${my.toFixed(1)} ${ex.toFixed(1)} ${ey.toFixed(1)}`;
      const dot = cls !== 'beam' ? '' : (fresh && !still)
          ? `<circle class="hv-bd" r="2.5"><animateMotion class="hv-fly" dur="0.7s" begin="indefinite" fill="freeze" calcMode="spline" keyTimes="0;1" keySplines=".3 0 .2 1" path="${d}"/></circle>`
          : `<circle class="hv-bd" r="2.5" cx="${x2.toFixed(1)}" cy="${y2.toFixed(1)}"/>`;
      return (cls === 'wait' && short ? '' : `<path class="hv-ln ${cls}" d="${d}"/>`) + dot + (cls === 'wait' ? (short ? head(a.x, a.y, x2, y2) : head(mx, my, x2, y2)) : ''); };
    const seen = n => pos[n] && hs.st[n] && hs.st[n].st !== 'unborn';
    let lines = '';
    Object.entries(hs.st).forEach(([n, s]) => { if (s.st === 'wait' && s.who && seen(n) && seen(s.who) && (selC ? (selC.bees.includes(n) || selC.bees.includes(s.who)) : cells.length <= 18)) lines += link(pos[n], pos[s.who], 'wait'); });
    if (beam && seen(beam.a)) beam.to.forEach(t => { if (seen(t)) lines += link(pos[beam.a], pos[t], 'beam'); });
    const tally = k => cells.filter(c => agg(c) === k).length, doneT = cells.filter(c => !agg(c) && c.total && c.done >= c.total).length;
    const many = cells.length > 18;
    sum.hidden = !many;
    if (many) sum.innerHTML = `<span><b>${cells.length}</b> thread${cells.length === 1 ? '' : 's'}</span>` +
        [['busy', 'working'], ['warn', 'waiting'], ['bad', 'stuck']].map(([k, l]) => tally(k) ? `<span class="${k}"><i></i>${tally(k)} ${l}</span>` : '').join('') + (doneT ? `<span class="ok"><i></i>${doneT} done</span>` : '');
    root.querySelector('.hv').dataset.size = mode;
    stage.style.height = H + 'px';
    stage.style.width = CW + 'px';
    svg.setAttribute('viewBox', `0 0 ${CW} ${H}`);
    const zoomed = HV.zoom > 1.001, f = HV.focus; HV.focus = null;
    view.classList.toggle('zoomed', zoomed);
    view.style.height = zoomed ? Math.max(320, Math.min(Math.ceil(spanY * (Rfit + gap / 2) + 8), window.innerHeight - 160)) + 'px' : '';
    if (zoomed && f) { view.scrollLeft = ox + f.u * G - (f.ax ?? view.clientWidth / 2); view.scrollTop = oy + f.v * G - (f.ay ?? view.clientHeight / 2); }
    HV.geo = {ox, oy, G, units: [[0, 0], ...slots.map(([q, r]) => [ux(q), uy(q, r)])]};
    const zg = root.querySelector('.hv-zoom'), [zOut, zIn, zFit] = zg.querySelectorAll('button');
    zg.hidden = zmax < 1.15; zOut.disabled = zFit.disabled = !zoomed; zIn.disabled = HV.zoom > zmax - .001;
    root.querySelector('.hv').classList.toggle('can-zoom', zmax >= 1.15 && mode !== 'full');
    if (HV.lastZoom !== HV.zoom) { const j = HV.jump = (HV.jump || 0) + 1; layer.classList.add('jump'); requestAnimationFrame(() => requestAnimationFrame(() => { if (j === HV.jump) layer.classList.remove('jump'); })); HV.lastZoom = HV.zoom; }
    svg.classList.toggle('fresh', fresh);
    HV.beam = beam ? beam.s : null;
    svg.innerHTML = cellSvg + selSvg + lines + textSvg;
    svg.querySelectorAll('.hv-fly').forEach(a => { const c = a.parentNode; c.style.opacity = 0; a.beginElement && a.beginElement(); requestAnimationFrame(() => { c.style.opacity = ''; }); });
    layer.classList.toggle('names', names);
    const label = s => ({working: s.who ? `Working for @${s.who}` : 'Working', wait: `Waiting on @${s.who}`, needs: ui('needs'), issue: 'Stuck', done: 'Done', idle: 'Resting', retired: 'Retired', unborn: 'Not started'}[s.st]);
    const have = new Map([...layer.children].map(b => [b.dataset.n, b]));
    Object.keys(pos).forEach(n => { const x = sc.members[n], s = hs.st[n]; if (!x || !s || tucked.has(n)) return;
      let b = have.get(n);
      if (!b) { b = document.createElement('button'); b.type = 'button'; b.className = 'hv-bee'; b.dataset.n = n;
        b.innerHTML = `${spriteTag(n, 'landed')}<i class="hv-dot"></i><span class="hv-nm">${esc(n)}</span>`; layer.appendChild(b); }
      have.delete(n);
      const isCoord = n === 'coordinator', art = s.st === 'working' ? 'working' : s.st === 'wait' ? 'waiting_on' : 'landed', size = pos[n].size;
      if (b.dataset.art !== art) { b.dataset.art = art; b.querySelectorAll('.beebox, .beewrap').forEach(e => { e.dataset.state = art; }); paintSprites(b); }
      b.className = `hv-bee ${s.st} sz-${size}${isCoord ? ' q' : ''}`;
      b.tabIndex = s.st === 'unborn' ? -1 : 0; b.setAttribute('aria-hidden', String(s.st === 'unborn'));
      const [w, h] = {q: [66, 54], m: [44, 36], xs: [22, 18]}[size];
      b.style.transform = `translate(${Math.round(pos[n].x - w / 2)}px,${Math.round(pos[n].y - h / 2)}px)`;
      const what = label(s);
      const tip = `@${n}${x.title && !isCoord ? ' · ' + x.title : ''}${what ? ' · ' + what : ''}`;
      b.title = tip; b.setAttribute('aria-label', tip); });
    have.forEach(b => b.remove());
    const card = root.querySelector('.hv-card');
    card.hidden = !selC;
    const cardHtml = !selC ? '' : `<div class="hv-ch"><div class="hv-cht"><b>${esc(selC.title)}</b>${selC.goal ? `<span>${esc(selC.goal)}</span>` : ''}</div>` +
        `<span class="hv-cc">${selC.count == null && selC.total ? `${selC.done} of ${selC.total} done` : `${selC.bees.length} bee${selC.bees.length === 1 ? '' : 's'}`}</span>` +
        `<button type="button" class="icon-btn" data-hvx title="Close" aria-label="Close"><svg class="icon" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8"/></svg></button></div>` +
        (selC.bees.length ? `<ul class="hv-rows">${selC.bees.map(n => { const x = sc.members[n], st = hs.st[n];
          return `<li><button type="button" class="hv-row" data-n="${esc(n)}" title="Open @${esc(n)}"><img src="${beeImg(n)}" alt="" width="44" height="36"><span class="hv-rb"><span class="hv-rh"><b>@${esc(n)}</b>${x.title ? `<i>${esc(x.title)}</i>` : ''}</span>${x.now && st.st !== 'unborn' ? `<span class="hv-rl">${esc(x.now)}</span>` : ''}</span><span class="hv-rs ${st.st}">${esc(label(st))}</span></button></li>`; }).join('')}</ul>`
        : `<p class="hv-re">No one is on this thread yet.</p>`);
    if (HV.cardHtml !== cardHtml) { HV.cardHtml = cardHtml; card.innerHTML = cardHtml; }
  }

  function hiveZoom(z, ax, ay, k) {
    const view = HV.root && HV.root.querySelector('.hv-view'), g = HV.geo; if (!view || !g) return;
    z = Math.max(1, Math.min(HV.zmax || 1, z));
    const unit = k != null && g.units[k + 1], cx = ax ?? view.clientWidth / 2, cy = ay ?? view.clientHeight / 2;
    HV.focus = unit ? {u: unit[0], v: unit[1]}
        : {u: (view.scrollLeft + cx - g.ox) / g.G, v: (view.scrollTop + cy - g.oy) / g.G, ax, ay};
    if (z === HV.zoom) { HV.focus = null; return; }
    HV.zoom = z;
    try { hiveDraw(); } catch (e) { console.warn('SwarmHive', e); }
  }

  function hiveMap(snap, el, opts) {
    if (!snap || !el) return;
    const swarmKey = snap.swarm || snap.id;
    if (HV.host !== el || !HV.root || !HV.root.isConnected || HV.swarm !== swarmKey) {
      el.innerHTML = `<div class="pane embedded"><div class="o2 hv"><div class="hv-bar"><div class="hv-sum" hidden></div><div class="btn-group hv-zoom" role="group" aria-label="Zoom" hidden><button type="button" class="icon-btn" data-hvz="out" title="Zoom out" aria-label="Zoom out"><svg class="icon" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8h9"/></svg></button><button type="button" class="icon-btn" data-hvz="in" title="Zoom in" aria-label="Zoom in"><svg class="icon" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8h9M8 3.5v9"/></svg></button><button type="button" class="icon-btn" data-hvz="fit" title="Fit the whole swarm" aria-label="Fit the whole swarm"><svg class="icon" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 6V3h3M10 3h3v3M13 10v3h-3M6 13H3v-3"/></svg></button></div></div><div class="hv-view"><div class="hv-stage"><svg class="hv-svg" role="img" aria-label="Where each worker is, by thread"></svg><div class="hv-bees"></div></div></div>
        <div class="hv-key" aria-hidden="true"><span><i class="hv-kd"></i>Tasks done</span><span><i class="hv-km"></i>Message just sent</span><span><i class="hv-kw"></i>Waiting on</span><span class="hv-tk"><i class="hv-sw busy"></i>Working</span><span class="hv-tk"><i class="hv-sw warn"></i>Waiting</span><span class="hv-tk"><i class="hv-sw ok"></i>Done</span></div><section class="hv-card" aria-live="polite" hidden></section></div></div>`;
      Object.assign(HV, {host: el, root: el.firstElementChild, key: '', swarm: swarmKey, beam: null, zoom: 1, zmax: 1, focus: null, geo: null, lastZoom: 1, dragged: false, sel: null, cellIds: [], cardHtml: null});
      const redraw = () => { try { hiveDraw(); } catch (e) { console.warn('SwarmHive', e); } };
      if (!HV.keyed) { HV.keyed = true; document.addEventListener('keydown', e => { if (e.key === 'Escape' && !e.defaultPrevented && HV.sel && HV.root && HV.root.offsetParent) { HV.sel = null; redraw(); } }); }
      const hooks = () => (window.SwarmHive && window.SwarmHive.hooks) || {};
      const view = HV.root.querySelector('.hv-view');
      HV.root.addEventListener('click', e => {
        if (HV.dragged) { HV.dragged = false; return; }
        const zb = e.target.closest('[data-hvz]');
        if (zb) { hiveZoom(zb.dataset.hvz === 'fit' ? 1 : HV.zoom * (zb.dataset.hvz === 'in' ? 1.6 : 1 / 1.6)); return; }
        if (e.target.closest('[data-hvx]')) { HV.sel = null; redraw(); return; }
        const row = e.target.closest('.hv-row');
        if (row) { if (hooks().member) hooks().member(row.dataset.n); return; }
        const b = e.target.closest('.hv-bee'), hv = HV.root.querySelector('.hv'), small = hv.classList.contains('can-zoom');
        const c = e.target.closest('.hv-cell') || (b && small && hv.dataset.size === 'tiny' && document.elementsFromPoint(e.clientX, e.clientY).find(x => x.classList && x.classList.contains('hv-cell')));
        if (b && !c) { if (hooks().member) hooks().member(b.dataset.n); return; }
        if (c && hv.dataset.size === 'tiny') { if (small) hiveZoom(Math.max(HV.zoom * 1.6, 96 / (108 / HV.zmax)), null, null, Number(c.dataset.c)); return; }
        const id = c ? HV.cellIds[Number(c.dataset.c)] : null;
        if (id != null || (HV.sel && e.target.closest('.hv-view'))) { HV.sel = id != null && id !== HV.sel ? id : null; redraw(); }
      });
      view.addEventListener('wheel', e => { if (!e.ctrlKey && !e.metaKey) return; e.preventDefault();
        const b = view.getBoundingClientRect(); hiveZoom(HV.zoom * Math.exp(-e.deltaY * .01), e.clientX - b.left, e.clientY - b.top); }, {passive: false});
      view.addEventListener('pointerdown', e => { if (e.button !== 0 || !view.classList.contains('zoomed')) return;
        const x0 = e.clientX, y0 = e.clientY, sl = view.scrollLeft, st = view.scrollTop; let moved = false;
        const move = ev => { const dx = ev.clientX - x0, dy = ev.clientY - y0; if (!moved && Math.hypot(dx, dy) < 5) return; moved = true; view.classList.add('panning'); view.scrollLeft = sl - dx; view.scrollTop = st - dy; };
        const up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); window.removeEventListener('pointercancel', up); view.classList.remove('panning');
          if (moved) { HV.dragged = true; setTimeout(() => { HV.dragged = false; }, 0); } };
        window.addEventListener('pointermove', move); window.addEventListener('pointerup', up); window.addEventListener('pointercancel', up); });
      if (HV.ro) HV.ro.disconnect();
      HV.ro = new ResizeObserver(() => { try { hiveDraw(); } catch (e) { console.warn('SwarmHive', e); } });
      HV.ro.observe(HV.root);
    }
    HV.args = {seq: opts && opts.seq != null ? Number(opts.seq) : null};
    try {
      if (swarmKey !== S.swarm) {
        S.swarm = swarmKey;
        S.etag = '';
        S.live = null;
        beeImgs.clear();
      }
      if (!snap.etag || S.etag !== snap.etag || !S.live) { S.live = toModel(snap); S.etag = snap.etag || ''; }
      hiveDraw();
    } catch (e) { console.warn('SwarmHive', e); }
  }

  const ROOTS = new Map();
  function sync() { ROOTS.forEach((r, el) => { const tab = el.closest('.tab-body'); if (r.root.isConnected && (!tab || tab.classList.contains('active'))) render(r.root, r.view); }); }
  const RO = new ResizeObserver(es => { if (S.live && S.live.starting && !state.onboardAt && es.some(e => e.contentRect.width > 0)) sync(); });
  window.SwarmHive = Object.assign(window.SwarmHive || {}, {
    colorOf(n) { return tcol(n === (window.SwarmHive.leadName || 'coordinator') ? 'coordinator' : n); },
    avatarHtml(name) {
      const n = name === (window.SwarmHive.leadName || 'coordinator') ? 'coordinator' : String(name || '?');
      return `<span class="msg-av bee" style="${hexBG(n)}" aria-hidden="true"><img src="${beeImg(n)}" alt="" width="22" height="18"></span>`;
    },
    beeSrc(name) { return beeImg(name === (window.SwarmHive.leadName || 'coordinator') ? 'coordinator' : String(name || '?')); },
    map(snap, el, opts) { hiveMap(snap, el, opts); },
    update(snap, el, opts) {
      if (!snap || !el) return;
      const view = (opts && opts.view) || 'overview';
      window.SwarmHive.embedded = true;
      let r = ROOTS.get(el);
      if (!r || !r.root.isConnected || r.root.parentNode !== el) {
        el.innerHTML = '';
        const root = document.createElement('div');
        root.className = 'pane embedded';
        el.appendChild(root);
        RO.observe(root);
        r = {root, view};
        ROOTS.set(el, r);
      }
      const swarmKey = snap.swarm || snap.id;
      if (swarmKey !== S.swarm) {
        closeReel();
        for (const k of Object.keys(state)) delete state[k];
        state.view = 'overview';
        S.swarm = swarmKey;
        S.etag = '';
        S.live = null;
        beeImgs.clear();
        ROOTS.forEach((x) => { x.etag = ''; });
      }
      if (r.etag && snap.etag && r.etag === snap.etag && r.view === view) return;
      r.view = view;
      const tab = el.closest('.tab-body');
      if (tab && !tab.classList.contains('active')) return;
      try {
        if (!snap.etag || S.etag !== snap.etag || !S.live) { S.live = toModel(snap); S.etag = snap.etag || ''; }
        r.etag = snap.etag || '';
        render(r.root, view);
      } catch (e) { console.warn('SwarmHive', e); }
    },
  });
})();
