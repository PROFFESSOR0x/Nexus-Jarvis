"use strict";
/* ============================================================================
   NEXUS ENTITY3D — golden holographic agent visualizer (command deck)
   ----------------------------------------------------------------------------
   Replaces the old 2D "nodes/cards/wires" graph with a living 3D hologram:

     - LEADER  : big golden ember-sphere (like the reference frames) —
                 layered particle shell + hot core + gyro rings + wire shell
                 + floor glow. Owns the MODEL / MEMORY sub-chips (HTML).
     - WORKERS : same hologram, smaller, tethered to the leader by luminous
                 beams. Each carries its own tool satellites, status + label.
     - Every backend event is choreography, never decoration:
                 tool start/done, radio talk, joint plan, delegate spawn,
                 thinking, errors, final answer — each has its own motion,
                 color, burst, ring, beam and glyph per tool (see TOOL_FX).

   Loading strategy (works online AND offline):
     1. tries to dynamically import Three.js from CDN (unpkg -> jsdelivr);
     2. if that fails (offline / blocked), a built-in 2D canvas engine draws
        the same golden orbs/tethers/pulses on the same <canvas>, with the
        same public API, so app.js never cares which engine is active.

   Public API (window.Entity3D) — all methods never throw:
     ping(v) · setMode(m) · errFlash() · ensureWorker(id,label,tint)
     setWorkerStatus(id,st) · setLastAction(id,txt) · retireWorker(id,ok)
     clearWorkers() · commandPulse(id,n) · resultPulse(id,ok) · peerPulse(a,b)
     toolStart(owner,tool,label) · toolDone(owner,tool,ok,elapsed)
     thinkingPulse(who) · planPulse(who) · spawnFlash(id) · finalBurst(ok)
      setHud({mode,tools,workers,round,model,mem}) · resetAll()
      setLayoutMode(m) · getLayoutMode() — layout law switch (/layout):
        "flow" (computed-map scatter around the caller) |
        "lattice" (imaginary sphere-grid around the caller)
    ========================================================================== */
(function () {

/* ============================ 0. CONFIG & PALETTE ======================== */
var GOLD = {
  hot:  "#F9E7B0",  // champagne highlight (lustrous, not yellow)
  core: "#FFF8E1",  // near-white hot center
  mid:  "#D4AF37",  // classic metallic gold
  copper:"#C47B2B", // copper glints
  deep: "#8C6414",  // bronze shadow
  dim:  "#6E5218",  // faint antique gold
  teal: "#35f0d0"   // deck accent (kept for HUD text only)
};
var GOLD_RGB = "212,175,55";
var HOT_RGB = "249,231,176";
var NEXUS = {  // leader body: deck-theme green, never gold
  hot: "#90f7e5", core: "#c2fbf1", mid: "#35f0d0",
  copper: "#5df3d9", deep: "#1d8472", dim: "#135449"
};

/* Worker accent tints (small ring + label dot only — bodies stay gold). */
var WORKER_TINTS = [
  "#b48cff", "#ffb454", "#3dffa2", "#ff7ad9", "#5aa9ff", "#ffd24a",
  "#ff8a5c", "#7df9ff", "#d0ff5c", "#ff5d7a", "#8affda", "#e0aaff"
];

/* staging: leader centered, big; workers large; camera pulled to fit */
var LEADER_R = 1.62, WORKER_R = 0.72;
var CAM_DIST0 = 10.6;
var MAIN_TIERS = [  // verified: min pairwise distance 1.68 over full cycles
  { r: 3.0, y: 0.0, sp: 0.16 }, { r: 4.8, y: 0.2, sp: -0.12 },
  { r: 3.6, y: 2.6, sp: 0.10 }, { r: 4.0, y: -2.4, sp: -0.09 },
  { r: 5.8, y: 2.6, sp: 0.08 }, { r: 5.8, y: -2.4, sp: -0.13 }
];
/* NO-OVERLAP NEST LAW: nested agents (depth>=2) NEVER cluster around their
   parent — parent spacing (down to 1.55) cannot fit satellite rings, proven
   by contradiction. They ride OUTER rings (r 8.0/9.6, own phases/speeds)
   grouped by PARENT, tethered back to it. Same rigid-seat math => spacing
   frozen forever on every level. */
var NEST_TIERS = [
  { r: 8.0, y: 0.0, sp: 0.09 }, { r: 9.6, y: 0.5, sp: -0.07 }
];
var DEPTH_R = [0, WORKER_R, 0.5, 0.38]; // body radius by depth (1,2,3+)
var FAM_R = 13.5, FAM_Y = 0.0, FAM_SP = 0.06; // caps (ring itself is dynamic)
var FAM_LIST = [{ r: 8, y: 2.4, sp: FAM_SP }]; // parents-with-children: upper layer, radius grows with count
/* FAMILY LAW (no-overlap proof): a worker WITH live children leaves its round
   ring for the family ring (r=13.5, camera widens); its children satellite
   around ITS body at rho(k)=max(1.7, 0.53/sin(PI/k)) capped 2.6 — chord stays
   >= body diameter, and the innermost satellite (13.5-2.6=10.9) clears the
   widest youth ring (9.6+bodies). A removed parent dissolves the family:
   orphans fall back to youth rings automatically. */
function isSatellite(w) {
  return !!(w && w.parent && S.famSet && S.famSet[w.parent] &&
    S.workers[w.parent] && S.workers[w.parent].node);
}
function satRho(w) { return (S.famRho && S.famRho[w.parent]) || 1.9; }
function listFor(w) { return w._tierList || tierListOf(w); }
function tiersArr(w) {
  var L = listFor(w);
  return L === "F" ? FAM_LIST : (L === "N" ? NEST_TIERS : MAIN_TIERS);
}
function angDiff(a, b) { return Math.atan2(Math.sin(a - b), Math.cos(a - b)); }
/* COMPUTED-MAP math: area per body + bounded randomness, no-overlap proven.
   packRadius: ring radius from 1D circle packing (chord >= 2b+gap). Jitter is
   carved ONLY from leftover free arc, so bodies can never merge. hash01 is
   deterministic per id => the scatter is stable, not reshuffled chaos. */
function hash01(s) {
  var h = 2166136261;
  s = String(s == null ? "" : s);
  for (var i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  h ^= h >>> 13; h = Math.imul(h, 0x5bd1e995); h ^= h >>> 15;
  return (h >>> 0) / 4294967296;
}
function packRadius(n, b, gap) {
  return Math.max(3.2, (Math.max(n, 2) * (2 * b + gap)) / (2 * Math.PI) * 1.12);
}
var claimPts = []; // world-approx occupied points, rebuilt every layout
function claimFree(x, y, z, minD) {
  for (var j = 0; j < claimPts.length; j++) {
    var dx = x - claimPts[j].x, dy = (y - claimPts[j].y) * 1.2, dz = z - claimPts[j].z;
    if (dx * dx + dy * dy + dz * dz < minD * minD) return false;
  }
  return true;
}
function claimScore(x, y, z) {
  var m = 1e9;
  for (var j = 0; j < claimPts.length; j++) {
    var dx = x - claimPts[j].x, dy = (y - claimPts[j].y) * 1.2, dz = z - claimPts[j].z;
    var q = dx * dx + dy * dy + dz * dz;
    if (q < m) m = q;
  }
  return Math.sqrt(m);
}
function claimAdd(x, y, z) { claimPts.push({ x: x, y: y, z: z }); return { x: x, y: y, z: z }; }
function bodyUnitR(w) {
  if (!w) return WORKER_R;
  if (w.kind !== "worker") return LEADER_R; // NEXUS + peers: immovable bulk
  return DEPTH_R[Math.min(depthOf(w), 3)] || WORKER_R;
}
/* RUNTIME SEPARATION GUARANTEE: layout homes never overlap by construction,
   but bodies EASE through space — joins/leaves re-home everyone, parents
   drift while children chase them, newborns fly in from the leader — so
   paths cross and two cores can end up merged. This per-frame solver pushes
   every overlapping pair apart (damped, never snapping), so a steady-state
   merge is impossible in either engine. Leaders/peers never move: the
   worker takes the full push. */
var SEP_GAP = 0.55; // skin over r1+r2 (covers gyro rings + wobble + drift)
var LEADER_GAP = 1.6; // NEXUS keep-out skin: MUCH wider than worker-worker.
   // Tethers/pulses still reach the leader — bodies just never rest on it.
function leaderKeepOut(w) { return LEADER_R + bodyUnitR(w) + LEADER_GAP; }
function gapFor(A, B) {
  return ((A.kind !== "worker") || (B.kind !== "worker")) ? LEADER_GAP : SEP_GAP;
}
function separateStep(dt) {
  if (S.engine !== "three") return;
  var bodies = [];
  if (S.leader && S.leader.node) bodies.push(S.leader);
  for (var k in S.leaders) {
    var q = S.leaders[k];
    if (q && q !== S.leader && q.node) bodies.push(q);
  }
  for (var i = 0; i < S.order.length; i++) {
    var w = S.workers[S.order[i]];
    if (w && w.node) bodies.push(w);
  }
  var n = bodies.length;
  if (n < 2) return;
  var rate = Math.min(1, dt * 5);
  for (var a = 0; a < n; a++) {
    for (var b = a + 1; b < n; b++) {
      var A = bodies[a], B = bodies[b];
      var pa = A.node.group.position, pb = B.node.group.position;
      var minD = bodyUnitR(A) + bodyUnitR(B) + gapFor(A, B);
      var dx = pb.x - pa.x, dy = pb.y - pa.y, dz = pb.z - pa.z;
      var d = Math.sqrt(dx * dx + dy * dy + dz * dz);
      if (d >= minD) continue;
      var ux, uy, uz;
      if (d < 1e-4) { // exactly merged: split along a stable hashed axis
        var h1 = hash01(A.id + "|sep"), h2 = hash01(B.id + "|sep");
        ux = Math.cos(h1 * 6.283); uy = h2 - 0.5; uz = Math.sin(h1 * 6.283);
        var il = 1 / (Math.hypot(ux, uy, uz) || 1);
        ux *= il; uy *= il; uz *= il;
        d = 0;
      } else { ux = dx / d; uy = dy / d; uz = dz / d; }
      var overlap = minD - d;
      var aLead = (A.kind !== "worker"), bLead = (B.kind !== "worker");
      if (aLead && !bLead) {
        pb.x += ux * overlap * rate; pb.y += uy * overlap * rate; pb.z += uz * overlap * rate;
      } else if (bLead && !aLead) {
        pa.x -= ux * overlap * rate; pa.y -= uy * overlap * rate; pa.z -= uz * overlap * rate;
      } else {
        var h = overlap * 0.5 * rate;
        pa.x -= ux * h; pa.y -= uy * h; pa.z -= uz * h;
        pb.x += ux * h; pb.y += uy * h; pb.z += uz * h;
      }
    }
  }
}
function separateFlat() {
  if (S.engine !== "flat" || !FL.leaderXY) return;
  var L = FL.leaderXY();
  var bodies = [{ lx: true, x: L[0], y: L[1], r: 66 }];
  for (var i = 0; i < S.order.length; i++) {
    var w = S.workers[S.order[i]];
    if (w && w.node) bodies.push({ w: w, x: w.node.x, y: w.node.y,
      r: (depthOf(w) >= 3 ? 18 : (depthOf(w) === 2 ? 24 : 32)) });
  }
  var n = bodies.length;
  if (n < 2) return;
  var rate = 0.08, GAP = 16, LGAP = 70; // NEXUS keep-out (px) >> worker gap
  for (var a = 0; a < n; a++) {
    for (var b = a + 1; b < n; b++) {
      var A = bodies[a], B = bodies[b];
      var minD = A.r + B.r + ((A.lx || B.lx) ? LGAP : GAP);
      var dx = B.x - A.x, dy = B.y - A.y;
      var d = Math.sqrt(dx * dx + dy * dy);
      if (d >= minD) continue;
      var ux, uy;
      if (d < 1e-4) {
        var h1 = hash01((A.w ? A.w.id : "leader") + "|sep");
        ux = Math.cos(h1 * 6.283); uy = Math.sin(h1 * 6.283); d = 0;
      } else { ux = dx / d; uy = dy / d; }
      var overlap = minD - d;
      if (A.lx && B.w) {
        B.w.node.x += ux * overlap * rate; B.w.node.y += uy * overlap * rate;
        B.x += ux * overlap * rate; B.y += uy * overlap * rate;
      } else if (B.lx && A.w) {
        A.w.node.x -= ux * overlap * rate; A.w.node.y -= uy * overlap * rate;
        A.x -= ux * overlap * rate; A.y -= uy * overlap * rate;
      } else if (A.w && B.w) {
        var h = overlap * 0.5 * rate;
        A.w.node.x -= ux * h; A.w.node.y -= uy * h; A.x -= ux * h; A.y -= uy * h;
        B.w.node.x += ux * h; B.w.node.y += uy * h; B.x += ux * h; B.y += uy * h;
      }
    }
  }
}
/* leader-base anchor for top-level homes (boss drift is << separation) */
function bossBase() { return { x: 0, y: 0.15, z: 0 }; }
/* free-disc home: PROVEN even base (packing bound) + hashed jitter carved
   ONLY from leftover free arc. Randomness budget shrinks to ~0 when crowded
   (math forbids overlap), opens up when sparse. Deterministic per id. */
function scatterDisc(w, grp, R, yBase, k, n) {
  /* radius capped (never sprawls): overflow stacks in y layers instead.
     Separation is GLOBAL (all groups share one claim field). */
  var b = 1.0, minD = 2 * b + 0.5;
  n = Math.max(n || 1, 1); k = k || 0;
  R = Math.min(R, 9);
  /* NEXUS keep-out: no disc home may rest inside the leader exclusion zone
     (leader bulk + own bulk + LEADER_GAP) — the solver enforces it at
     runtime too, this just stops spawning inside it. */
  R = Math.max(R, leaderKeepOut(w));
  var perLayer = Math.max(4, Math.floor((2 * Math.PI * R) / minD));
  var layer = Math.floor(k / perLayer);
  var inLayer = k % perLayer, nLayer = Math.min(perLayer, n - layer * perLayer);
  var yL = yBase + ((layer % 2) ? 1 : -1) * Math.ceil(layer / 2) * 2.4;
  var chord = 2 * R * Math.sin(Math.PI / Math.max(nLayer, 2));
  var free = Math.max(0, chord - minD);
  var base = (inLayer / Math.max(nLayer, 1)) * Math.PI * 2;
  var jA = Math.min(0.42, (free / R) * 0.45);
  var jR = Math.min(1.0, R * 0.15, free * 0.3);
  var jY = Math.min(0.45, free * 0.2);
  var ha = hash01(w.id + "|a"), hr = hash01(w.id + "|r"), hy = hash01(w.id + "|y");
  var LB = bossBase();
  var a = base, r = R, y = yL, ok = false, best = null, bestScore = -1;
  for (var t = 0; t < 32; t++) {
    var ta = base + ((t === 0) ? (ha - 0.5) * 2 * jA : (hash01(w.id + "|ja" + t) - 0.5) * 2 * jA + (t * 0.3));
    var tr = R + ((t === 0) ? (hr - 0.5) * 2 * jR : (hash01(w.id + "|jr" + t) - 0.5) * 2 * jR);
    var ty = yL + ((t === 0) ? (hy - 0.5) * 2 * jY : (hash01(w.id + "|jy" + t) - 0.5) * 2 * jY);
    var px = LB.x + Math.cos(ta) * tr, pz = LB.z + Math.sin(ta) * tr, py = LB.y + ty;
    var sc = claimScore(px, py, pz);
    if (sc > bestScore) { bestScore = sc; best = { a: ta, r: tr, y: ty, px: px, py: py, pz: pz }; }
    if (sc >= minD) { a = ta; r = tr; y = ty; ok = true; break; }
  }
  if (!ok && best) { a = best.a; r = best.r; y = best.y; }
  claimAdd(LB.x + Math.cos(a) * r, LB.y + y, LB.z + Math.sin(a) * r);
  w._wpt = { x: LB.x + Math.cos(a) * r, y: LB.y + y, z: LB.z + Math.sin(a) * r };
  w._home = { a: a, r: r, y: y };
  w._home3 = null;
  w._seat = a; w._spotR = r; w._spotY = y; w._tierPhase = 0;
  if (w._orbA == null) w._orbA = a;
  return w._home;
}
/* 3D shell home around the live parent: hashed direction (upper-biased),
   radius by sibling count, angular separation enforced.
   CALLER-CENTERED LAW: children cluster around the body that spawned them
   (their parent), never around NEXUS — NEXUS stays the center of top-level
   agents only. Randomness budget is carved from the LEFTOVER free arc
   (chord - minD): crowded shells get ~0 jitter (math forbids overlap),
   sparse shells get the full scatter. Expansion is bounded (rho<=4.2,
   overflow stacks on a second shell, never sprawls). Spread is FULL-SPHERE:
   children cover all directions around the parent (up/down/left/right/
   front/back) — baseP rides a golden-ratio spiral over the whole sphere. */
function scatterShell(w, k, total) {
  total = Math.max(total || 1, 1); k = k || 0;
  var rho0 = satRho(w);
  /* overflow: >8 siblings share two shells instead of one huge sphere */
  var perShell = 8;
  var layer = Math.floor(k / perShell);
  var inLayer = k % perShell, nLayer = Math.min(perShell, total - layer * perShell);
  var rho = Math.min(4.2, rho0 + layer * 1.3);
  var yOff = layer * 1.1;
  /* leftover free arc on THIS shell -> jitter budget (0.15..1) */
  var chord = 2 * rho * Math.sin(Math.PI / Math.max(nLayer, 2));
  var b2 = 1.0, minD2 = 2 * b2 + 0.5;
  var free = Math.max(0, chord - minD2);
  var budget = clamp(free / minD2, 0.15, 1);
  var baseA = (inLayer / Math.max(nLayer, 1)) * Math.PI * 2;
  var baseP = Math.acos(1 - 2 * (((inLayer * 0.61803398875) % 1 + 1) % 1));
  var anchor = (S.workers[w.parent] && S.workers[w.parent]._wpt) || bossBase();
  var LA = bossBase(); // NEXUS keep-out center (leader drifts ±0.4; GAP covers it)
  var keepR = leaderKeepOut(w);
  var v = null, best2 = null, bestScore2 = -1;
  for (var t = 0; t < 32; t++) {
    var sfx = (t === 0) ? "" : String(t);
    var jit = (t === 0) ? 1 : 0.4;
    var jScale = budget * jit;
    var th = baseA + (hash01(w.id + "|sx" + sfx) - 0.5) * 2 * 0.5 * jScale;
    var ph = baseP + (hash01(w.id + "|sp" + sfx) - 0.5) * 2 * 0.35 * jScale;
    ph = Math.min(Math.PI - 0.15, Math.max(0.15, ph));
    var cx = Math.sin(ph) * Math.cos(th), cz = Math.sin(ph) * Math.sin(th);
    /* full sphere: cos(ph) spans [-1,1] => up AND down AND equator.
       Tiny +0.12 lift keeps the cluster readable (children rarely hide
       under the parent body) without forbidding any direction. */
    var cy = Math.cos(ph) * 0.9 + 0.12, il = 1 / (Math.hypot(cx, cy, cz) || 1);
    cx *= il; cy *= il; cz *= il;
    var qx = anchor.x + cx * rho, qy = anchor.y + (cy * rho + yOff), qz = anchor.z + cz * rho;
    /* NEXUS keep-out first: a shell point leaning back into the leader zone
       is rejected no matter how empty it is (the solver is the backstop) */
    var dL = Math.sqrt((qx - LA.x) * (qx - LA.x) + (qy - LA.y) * (qy - LA.y) + (qz - LA.z) * (qz - LA.z));
    var sc2 = (dL >= keepR) ? claimScore(qx, qy, qz) : -1;
    if (sc2 > bestScore2) { bestScore2 = sc2; best2 = { x: cx * rho, y: cy * rho + yOff, z: cz * rho, qx: qx, qy: qy, qz: qz }; }
    if (sc2 >= minD2) { v = { x: cx * rho, y: cy * rho + yOff, z: cz * rho }; break; }
  }
  if (!v && best2) v = { x: best2.x, y: best2.y, z: best2.z };
  if (!v) { // every sample leaned into NEXUS: park on the parent's far side
    var fx = anchor.x - LA.x, fz = anchor.z - LA.z;
    var fl2 = Math.hypot(fx, fz) || 1;
    v = { x: fx / fl2 * rho, y: rho * 0.5 + yOff, z: fz / fl2 * rho };
  }
  claimAdd(anchor.x + v.x, anchor.y + v.y, anchor.z + v.z);
  w._wpt = { x: anchor.x + v.x, y: anchor.y + v.y, z: anchor.z + v.z };
  w._home3 = v;
  w._home = null;
  w._seat = Math.atan2(v.z, v.x); w._spotR = rho;
}
/* ================= LATTICE MODE (sphere grid around the caller) ==========
   Second layout law beside the flow (computed-map) law: an IMAGINARY
   mathematical lattice on a sphere around the CALLER body —
     - nested children: Fibonacci-sphere lattice around the live PARENT,
     - top-level agents: Fibonacci-sphere lattice shell around NEXUS.
   Slots are REGULAR (Fibonacci sphere = near-equal cells); occupancy is
   RANDOM (hashed start offset per family + small hashed jitter inside the
   cell). Bodies read as a crystal sphere with organic scatter.
   Same safety nets as flow: leader keep-out, global claim field, and the
   per-frame separation solver. Switch live with /layout (E.setLayoutMode). */
function fibDir(i, M) {
  if (M <= 1) return [0, 1, 0];
  var y = 1 - (i / (M - 1)) * 2;
  var rr = Math.sqrt(Math.max(0, 1 - y * y));
  var ph = i * 2.399963; // golden angle: near-equal cells
  return [Math.cos(ph) * rr, y, Math.sin(ph) * rr];
}
/* random point inside slot i's cell: hashed offset scaled by cell size */
function latticeJit(id, sfx, M) {
  var w = 0.9 / Math.sqrt(Math.max(M, 1));
  return [(hash01(id + "|lx" + sfx) - 0.5) * 2 * w,
          (hash01(id + "|ly" + sfx) - 0.5) * 2 * w,
          (hash01(id + "|lz" + sfx) - 0.5) * 2 * w];
}
function latticeShell(w, k, total) {
  total = Math.max(total || 1, 1); k = k || 0;
  var rho0 = satRho(w);
  var perShell = 8;
  var layer = Math.floor(k / perShell);
  var inLayer = k % perShell;
  var rho = Math.min(4.2, rho0 + layer * 1.3);
  var yOff = layer * 1.1;
  var M = Math.max(total * 2, 6); // grid bigger than the family: random subset
  var off = Math.floor(hash01(w.parent + "|lattice") * M);
  var anchor = (S.workers[w.parent] && S.workers[w.parent]._wpt) || bossBase();
  var LA = bossBase(), keepR = leaderKeepOut(w);
  var b2 = 1.0, minD2 = 2 * b2 + 0.5;
  var v = null;
  for (var t = 0; t < M; t++) {
    var slot = (inLayer + off + t) % M;
    var d = fibDir(slot, M), j = latticeJit(w.id, "s" + slot, M);
    var cx = d[0] + j[0], cy = d[1] + j[1], cz = d[2] + j[2];
    var il = 1 / (Math.hypot(cx, cy, cz) || 1);
    cx *= il; cy *= il; cz *= il;
    var qx = anchor.x + cx * rho, qy = anchor.y + cy * rho + yOff, qz = anchor.z + cz * rho;
    var dL = Math.sqrt((qx - LA.x) * (qx - LA.x) + (qy - LA.y) * (qy - LA.y) + (qz - LA.z) * (qz - LA.z));
    if (dL < keepR) continue; // leans into NEXUS: try the next lattice slot
    if (claimScore(qx, qy, qz) >= minD2 || t === M - 1) {
      v = { x: cx * rho, y: cy * rho + yOff, z: cz * rho }; break;
    }
  }
  if (!v) { // every slot leaned into NEXUS: far side of the parent
    var fx = anchor.x - LA.x, fz = anchor.z - LA.z, fl2 = Math.hypot(fx, fz) || 1;
    v = { x: fx / fl2 * rho, y: rho * 0.5 + yOff, z: fz / fl2 * rho };
  }
  claimAdd(anchor.x + v.x, anchor.y + v.y, anchor.z + v.z);
  w._wpt = { x: anchor.x + v.x, y: anchor.y + v.y, z: anchor.z + v.z };
  w._home3 = v; w._home = null;
  w._seat = Math.atan2(v.z, v.x); w._spotR = rho;
}
function latticeDisc(w, grp, R, yBase, k, n) {
  n = Math.max(n || 1, 1); k = k || 0;
  R = Math.min(R, 12);
  R = Math.max(R, leaderKeepOut(w)); // NEXUS keep-out
  var M = Math.max(n + 6, 14); // grid bigger than the crowd: random subset
  var off = Math.floor(hash01(grp + "|lattice") * M);
  var LB = bossBase();
  var b = 1.0, minD = 2 * b + 0.5;
  var v = null, a = 0, y = 0;
  for (var t = 0; t < 24; t++) {
    var slot = (k + off + t) % M;
    var d = fibDir(slot, M), j = latticeJit(w.id, "d" + slot, M);
    var cx = d[0] + j[0], cy = d[1] * 0.6 + j[1], cz = d[2] + j[2];
    var il = 1 / (Math.hypot(cx, cy, cz) || 1);
    cx *= il; cy *= il; cz *= il;
    var px = LB.x + cx * R, py = LB.y + cy * R + yBase, pz = LB.z + cz * R;
    var sc = claimScore(px, py, pz);
    if (sc >= minD || t === 23) {
      v = { x: px, y: py, z: pz };
      a = Math.atan2(pz - LB.z, px - LB.x);
      y = py - LB.y;
      break;
    }
  }
  if (!v) v = { x: LB.x + R, y: LB.y + yBase, z: LB.z };
  claimAdd(v.x, v.y, v.z);
  w._wpt = v;
  w._home = { a: a, r: Math.hypot(v.x - LB.x, v.z - LB.z), y: y };
  w._home3 = null;
  w._seat = a; w._spotR = R; w._spotY = y; w._tierPhase = 0;
  if (w._orbA == null) w._orbA = a;
  return w._home;
}
/* Round-shared orbit rings: agents of the same round (in the same session)
   ride ONE ellipse with even rigid seats (k/n * 2PI on a shared tier phase),
   so spacing is frozen forever and bodies can never merge. Joins/leaves only
   reseat their own ring (members ease, never jump). Overflow round-groups
   merge into the emptiest tier and share its seats evenly. */
function depthOf(w) { return (w && w.depth) || 1; }
function groupKey(w) {
  if (S.famSet && S.famSet[w.id]) return "fam";
  if (depthOf(w) >= 2) return "nest::" + (w.parent || "?");
  return (w.sid || "_") + "::" + (w.round || "");
}
function tiersFor(w) { return tiersArr(w); } // legacy alias (list-aware)
function tierListOf(w) { return depthOf(w) >= 2 ? "N" : "M"; }
function tierDef(w) {
  var L = tiersArr(w);
  return L[w._tier] || L[0];
}
function mainGroupFirst() {
  // oldest group (by first-born worker) owns tier 0
  var seen = {}, order = [];
  S.order.forEach(function (id) {
    var w = S.workers[id];
    if (!w) return;
    var g = groupKey(w);
    if (!seen[g]) { seen[g] = true; order.push(g); }
  });
  return order;
}
function tierPhase(i) {
  if (!S.tierPh || S.tierPh.length !== MAIN_TIERS.length) {
    S.tierPh = [];
    for (var z = 0; z < MAIN_TIERS.length; z++) S.tierPh.push(0);
  }
  return S.tierPh[i] || 0;
}
function nestPhase(i) {
  if (!S.nestPh || S.nestPh.length !== NEST_TIERS.length) {
    S.nestPh = [];
    for (var z2 = 0; z2 < NEST_TIERS.length; z2++) S.nestPh.push(0);
  }
  return S.nestPh[i] || 0;
}
function phaseFor(w, ti) {
  var L = listFor(w);
  if (L === "F") return S.famPh || 0;
  return L === "N" ? nestPhase(ti) : tierPhase(ti);
}
/* rigid rings: each tier advances its shared phase once per frame;
   members only ease toward seat + phase, so geometry never shears. */
function advanceTierPh(dstep) {
  tierPhase(0); nestPhase(0);
  var pace = 1.6 + S.work * 2.4;
  for (var i = 0; i < MAIN_TIERS.length; i++) {
    S.tierPh[i] += dstep * MAIN_TIERS[i].sp * pace;
  }
  for (var j = 0; j < NEST_TIERS.length; j++) {
    S.nestPh[j] += dstep * NEST_TIERS[j].sp * pace;
  }
  S.famPh = (S.famPh || 0) + dstep * FAM_SP * pace;
}
function layoutOrbits() {
  /* CALLER-CENTERED LAW: NEXUS is the center of top-level agents only.
     Any subagent WITH live children stays where it is (in the field disc)
     and becomes the center of its own children — no exile to a far family
     ring, no huge sprawl. Satellite radius rho grows ONLY with sibling
     count (circle packing), capped at 4.2; overflow stacks on a 2nd shell. */
  var kidCount = {};
  S.order.forEach(function (id) {
    var cw = S.workers[id];
    if (cw && cw.parent && !cw.deadAt && S.workers[cw.parent] && !S.workers[cw.parent].deadAt) {
      kidCount[cw.parent] = (kidCount[cw.parent] || 0) + 1;
    }
  });
  S.famSet = {}; S.famRho = {};
  Object.keys(kidCount).forEach(function (pid) {
    var kk = kidCount[pid];
    S.famSet[pid] = kk;
    /* packing bound: chord = 2*rho*sin(PI/k) >= minD(2.4) ->
       rho >= minD/(2*sin(PI/k)); floor 1.7, ceiling 4.2 (bounded growth) */
    var need = (kk < 2) ? 1.9 : (2.5 / (2 * Math.sin(Math.PI / Math.max(kk, 2)))) * 1.12;
    S.famRho[pid] = Math.min(4.2, Math.max(1.9, need));
  });
  var groups = {};
  S.order.forEach(function (id) {
    var w = S.workers[id];
    if (!w) return;
    var g = groupKey(w);
    (groups[g] = groups[g] || []).push(w);
  });
  var order = mainGroupFirst();
  /* one tier per group per LIST (M = round rings, N = outer nest rings);
     overflow merges into the emptiest tier of its own list */
  var gTier = {}, gList = {}, tierCount = {};
  order.forEach(function (g, gi) {
    var members0 = groups[g] || [];
    var Lkey = (g === "fam") ? "F"
      : ((members0.length && depthOf(members0[0]) >= 2) ? "N" : "M");
    var L = Lkey === "F" ? FAM_LIST : (Lkey === "N" ? NEST_TIERS : MAIN_TIERS);
    gList[g] = Lkey;
    var counts = (tierCount[Lkey] = tierCount[Lkey] || {});
    var ti;
    if (gi < L.length && !counts[gi]) ti = gi;
    else {
      ti = 0;
      for (var c = 1; c < L.length; c++) {
        if ((counts[c] || 0) < (counts[ti] || 0)) ti = c;
      }
    }
    gTier[g] = ti;
    counts[ti] = (counts[ti] || 0) + members0.length;
  });
  /* seats: even shares over the (list,tier) collective, arrival order */
  var byTier = {};
  order.forEach(function (g) {
    var members = groups[g] || [], key = gList[g] + gTier[g];
    (byTier[key] = byTier[key] || []).push.apply(byTier[key], members);
  });
  /* COMPUTED HOMES: ONE field disc around the boss for every top-level
     agent (parents included — they never leave) + orphans; 3D shells
     around each LIVE parent for its children. Global claim field => no
     two bodies ever merge. Radius from live area (packing), never a
     fixed far exile. */
  claimPts = [];
  S.usedMaxR = 5.8;
  var _field = [];
  S.order.forEach(function (id) {
    var _fw = S.workers[id];
    if (!_fw) return;
    if (depthOf(_fw) === 1) _field.push(_fw);
    else if (!(_fw.parent && S.workers[_fw.parent] && !S.workers[_fw.parent].deadAt)) _field.push(_fw);
  });
  var _RF = packRadius(_field.length, 1.0, 1.0);
  var _lat = ((S.layoutMode || "flow") === "lattice");
  _field.forEach(function (w, _k) {
    if (_lat) latticeDisc(w, "field", _RF, 0.2, _k, _field.length);
    else scatterDisc(w, "field", _RF, 0.2, _k, _field.length);
    w._tier = 0; w._tierList = (depthOf(w) >= 2) ? "N" : "M";
    w._tierSrc = (depthOf(w) >= 2) ? "nest" : "round";
  });
  if (_RF > S.usedMaxR) S.usedMaxR = _RF;
  S.order.forEach(function (id) {
    var _dw = S.workers[id];
    if (!_dw || depthOf(_dw) < 2) return;
    if (_dw.parent && S.workers[_dw.parent] && !S.workers[_dw.parent].deadAt) {
      _dw._tier = 0; _dw._tierList = "N"; _dw._tierSrc = "nest";
    }
  });
  var _sibTotal = {}, _sibIdx = {};
  S.order.forEach(function (id) {
    var _cw = S.workers[id];
    if (_cw && depthOf(_cw) >= 2 && _cw.parent && S.workers[_cw.parent] && !S.workers[_cw.parent].deadAt)
      _sibTotal[_cw.parent] = (_sibTotal[_cw.parent] || 0) + 1;
  });
  S.order.forEach(function (id) {
    var _sw = S.workers[id];
    if (!_sw || depthOf(_sw) < 2) return;
    if (_sw.parent && S.workers[_sw.parent] && !S.workers[_sw.parent].deadAt) {
      var _kk = (_sibIdx[_sw.parent] || 0);
      _sibIdx[_sw.parent] = _kk + 1;
      if (_lat) latticeShell(_sw, _kk, _sibTotal[_sw.parent] || 1);
      else scatterShell(_sw, _kk, _sibTotal[_sw.parent] || 1);
    }
  });
  for (var _ci = 0; _ci < claimPts.length; _ci++) {
    var _cr = Math.hypot(claimPts[_ci].x, claimPts[_ci].z) + 1.5;
    if (_cr > S.usedMaxR) S.usedMaxR = _cr;
  }
  Object.keys(byTier).forEach(function (key) {
    var members = byTier[key], n = members.length;
    var Lkey = key.charAt(0), ti = +key.slice(1);
    var L = (Lkey === "N") ? NEST_TIERS : MAIN_TIERS;
    if (L[ti] && L[ti].r > S.usedMaxR) S.usedMaxR = L[ti].r;
    members.forEach(function (w, k) {
      w._tier = ti;
      w._tierList = Lkey;
      w._tierSrc = (Lkey === "N") ? "nest" : "round";
      w._tierPhase = 0;
      if (w._orbA == null) w._orbA = w._seat + phaseFor(w, ti);
    });
  });
}
/* rigid seats: ease toward seat + shared tier phase at a fixed rate.
   Same-round agents share one line with frozen spacing by construction. */
function stepWorkerAngle(w, dt) {
  if (isSatellite(w)) {
    // satellites ease around the PARENT body on the shared family phase
    if (w._seat == null) { layoutOrbits(); if (w._seat == null) return; }
    var wantS = (w._seat || 0) + (S.famPh || 0);
    if (w._orbA == null) w._orbA = wantS;
    else w._orbA += angDiff(wantS, w._orbA) * Math.min(1, dt * 3);
    return;
  }
  var ti = (w._tier == null) ? -1 : w._tier;
  if (!tierDef(w) || !tiersArr(w)[ti]) {
    layoutOrbits(); ti = w._tier;
    if (!tierDef(w) || !tiersArr(w)[ti]) return;
  }
  if (w._seat == null) { layoutOrbits(); if (w._seat == null) return; }
  var want = w._seat + phaseFor(w, ti);
  if (w._orbA == null) w._orbA = want;
  else w._orbA += angDiff(want, w._orbA) * Math.min(1, dt * 3);
}
function h2(s) {
  var h = 0; s = String(s == null ? "?" : s);
  for (var i = 0; i < s.length; i++) h = ((h * 37) + s.charCodeAt(i)) >>> 0;
  return h;
}
function hashStr(s) {
  var h = 0; s = String(s == null ? "?" : s);
  for (var i = 0; i < s.length; i++) h = ((h * 31) + s.charCodeAt(i)) >>> 0;
  return h;
}
/* Per-tool choreography. color=hex, rgb for canvas/three, glyph floats up,
   burst=ember particles on start, ring=shockwave on done, beam=vertical
   light pillar while running, spin=extra gyro kick 0..1, halo=colored shell
   tint while the call is in flight. */
var TOOL_FX = {
  "exec.shell":     { color: "#35f0d0", rgb: "53,240,208",   glyph: "$", burst: 46, ring: true,  beam: false, spin: 0.55, halo: true, theme: "#35f0d0", spinMul: 1.10, bobMul: 1.00, ringMul: 1.30 },
  "exec.code":      { color: "#b48cff", rgb: "180,140,255",  glyph: "#", burst: 60, ring: true,  beam: true,  spin: 0.80, halo: true, theme: "#b48cff", spinMul: 1.60, bobMul: 1.10, ringMul: 1.60 },
  "web.search":     { color: "#5aa9ff", rgb: "90,169,255",   glyph: "?", burst: 34, ring: true,  beam: false, spin: 0.45, halo: false, theme: "#5aa9ff", spinMul: 0.90, bobMul: 1.30, ringMul: 1.10 },
  "web.fetch":      { color: "#c86bff", rgb: "200,107,255",  glyph: "~", burst: 40, ring: true,  beam: true,  spin: 0.60, halo: true, theme: "#c86bff", spinMul: 1.20, bobMul: 0.80, ringMul: 1.40 },
  "parallel":       { color: "#ffd24a", rgb: "255,210,74",   glyph: "=", burst: 90, ring: true,  beam: true,  spin: 1.00, halo: true, theme: "#ffd24a", spinMul: 1.80, bobMul: 1.20, ringMul: 1.80 },
  "agent.delegate": { color: "#ff9a3c", rgb: "255,154,60",   glyph: "@", burst: 80, ring: true,  beam: true,  spin: 0.90, halo: true, theme: "#ff9a3c", spinMul: 1.30, bobMul: 1.10, ringMul: 1.50 },
  "agent.say":      { color: "#ffb454", rgb: "255,180,84",   glyph: "\u00bb", burst: 16, ring: false, beam: false, spin: 0.25, halo: false, theme: "#ffb454", spinMul: 1.00, bobMul: 1.40, ringMul: 1.00 },
  "agent.plan":     { color: "#3dffa2", rgb: "61,255,162",   glyph: "\u2713", burst: 26, ring: true,  beam: false, spin: 0.40, halo: false, theme: "#3dffa2", spinMul: 0.80, bobMul: 1.10, ringMul: 0.90 },
  "memory.read":    { color: "#35f0d0", rgb: "53,240,208",   glyph: "R", burst: 20, ring: false, beam: false, spin: 0.30, halo: false, theme: "#35f0d0", spinMul: 0.70, bobMul: 1.20, ringMul: 0.80 },
  "memory.remember":{ color: "#ffd24a", rgb: "255,210,74",   glyph: "W", burst: 44, ring: true,  beam: false, spin: 0.50, halo: true, theme: "#ffe9a8", spinMul: 0.60, bobMul: 1.40, ringMul: 0.70 },
  "memory.forget":  { color: "#ff5d5d", rgb: "255,93,93",    glyph: "X", burst: 40, ring: true,  beam: false, spin: 0.50, halo: true, theme: "#ff5d5d", spinMul: 1.40, bobMul: 0.70, ringMul: 1.50 },
  "memory.profile": { color: "#ff7ad9", rgb: "255,122,217",  glyph: "P", burst: 30, ring: true,  beam: false, spin: 0.40, halo: true, theme: "#ff7ad9", spinMul: 0.90, bobMul: 1.10, ringMul: 1.00 }
};
function toolFx(name) {
  return TOOL_FX[name] || { color: "#ffe9a8", rgb: GOLD_RGB, glyph: "\u2022",
    burst: 24, ring: false, beam: false, spin: 0.35, halo: false,
    theme: "#ffe9a8", spinMul: 1.15, bobMul: 1, ringMul: 1.2 };
}

/* ================================ 1. UTILS =============================== */
function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }
function lerp(a, b, t) { return a + (b - a) * t; }
function rand(a, b) { return a + Math.random() * (b - a); }
function pick(arr) { return arr[(Math.random() * arr.length) | 0]; }
function $(id) { return document.getElementById(id); }
function nowMs() { return (typeof performance !== "undefined" ? performance.now() : Date.now()); }
/* deterministic hue slot per worker id -> stable tint across turns */
function tintFor(id) {
  var h = 0, s = String(id == null ? "?" : id);
  for (var i = 0; i < s.length; i++) h = ((h * 31) + s.charCodeAt(i)) >>> 0;
  return WORKER_TINTS[h % WORKER_TINTS.length];
}
/* per-entity body palette: NEXUS teal for the leader, task color for
   workers, GOLD fallback. Never invents colors. */
function paletteFor(w) {
  if (w && w.kind !== "worker") return NEXUS;
  var base = (w && /^#[0-9a-fA-F]{6}$/.test(w.taskColor || ""))
    ? w.taskColor : null;
  if (!base) return GOLD;
  function mix(a, b, f) {
    var A = hexToRgb(a), B = hexToRgb(b);
    function hx(v) { v = Math.round(Math.max(0, Math.min(255, v))).toString(16); return v.length < 2 ? "0" + v : v; }
    return "#" + hx(A[0] + (B[0] - A[0]) * f) + hx(A[1] + (B[1] - A[1]) * f) + hx(A[2] + (B[2] - A[2]) * f);
  }
  return { hot: mix(base, "#ffffff", 0.45), core: mix(base, "#ffffff", 0.7),
    mid: base, copper: mix(base, "#ffffff", 0.2),
    deep: mix(base, "#000000", 0.45), dim: mix(base, "#000000", 0.65) };
}
function hexToRgb(hex) {
  var h = String(hex || "#ffffff").replace("#", "");
  if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
  var n = parseInt(h.slice(0, 6), 16);
  if (isNaN(n)) return [255, 255, 255];
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
function escHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/* ================================ 2. STATE =============================== */
/* Engine-agnostic sim state. Both the WebGL engine and the 2D fallback read
   and animate this; app.js only talks to the public API below. */
var S = {
  engine: null,            // "three" | "flat" | null(booting)
  mode: "IDLE",            // IDLE | WORKING | ERROR (mirrors coreState)
  errUntil: 0,
  energy: 0,               // 0..1 excitement, decays every frame
  leader: null,            // FOCUSED leader entity (camera + HUD follow it)
  leaders: {},            // sid -> leader-class entity (main + peers)
  workers: {},             // id -> entity record
  order: [],               // worker id insertion order (layout)
  layoutMode: "flow",      // flow (computed-map scatter) | lattice (sphere grid)
  pulses: [],              // travelling signals {a,b,t,sp,color,curve,arch}
  arcs: [],                // peer-talk arcs {a,b,ttl}
  bursts: [],              // particle explosions (engine-owned payload)
  rings: [],               // shockwaves {at,t,ttl,color,size}
  floats: [],              // rising glyphs {at,txt,color,t,ttl}
  beams: [],               // vertical pillars {at,color,t,ttl}
  flashes: [],             // fullscreen-ish sprite pops {at,color,t,ttl,size}
  hud: { mode: "IDLE", tools: 0, workers: 0, round: "\u2014", model: "\u2014", mem: 0, sess: "\u2014", link: "LINK", clock: "--:--:--" },
  camE: 0,           // smoothed excitement for the camera (no pumping)
  hover: { id: null, x: 0, y: 0 }, // hovered entity + cursor pos (css px in wrap)
  focus: null, // clicked entity id the camera follows ("leader" default)
  work: 0, workT: 0,   // sustained activity + its target (eased, never jumps)
  eT: 0,               // energy target (eased, never jumps)
  tierPh: null,         // per-tier shared ring phases (lazy sized to MAIN_TIERS)
  wanderT: 0,        // acceleration-aware clock: runs fast while work runs
  flick: 1, glitch: 0, // hologram flicker state
  camShake: 0,
  bootedAt: nowMs()
};
var widSeq = 0;

function makeEntity(id, label, kind, tint) {
  return {
    id: id, label: label || id, kind: kind, // "leader" | "worker"
    tint: tint || "#ffb545",
    status: kind === "leader" ? "idle" : "queued", // queued|working|done|fail
    lastAction: "",
    orbiters: [],          // in-flight tool satellites {tool,color,angle,r,speed,glyph}
    deadAt: 0,             // timestamp when retired (fade-out), 0 = alive
    ok: true,
    bornAt: nowMs(),
    phase: Math.random() * Math.PI * 2,  // float-cycle offset
    flash: 0, flashColor: "#ffffff",     // hit-flash 0..1
    flare: 0,              // core flare from pings (dynamic focus) 0..1.4
    think: 0,              // thinking glow 0..1
    halo: null,            // {color} while a halo tool runs
    spinKick: 0,           // extra gyro speed 0..1+
    act: 0, actT: 0,        // per-agent sustained activity 0..1.3 (own real work only)
    exc: 0, excT: 0,        // per-body flash/excitement (own events only, never global)
    ck: Math.random() * 100, // private clock: own phase AND own rate (desynced bodies)
    // engine payloads (filled by active engine):
    node: null,            // THREE.Group or flat {x,y}
    ember: null, mats: null
  };
}
function liveWorkers() {
  var out = [];
  for (var i = 0; i < S.order.length; i++) {
    var w = S.workers[S.order[i]];
    if (w && !w.deadAt) out.push(w);
  }
  return out;
}
function getEntity(id) {
  if (!id || id === "leader") return S.leader;
  if (S.leaders[id]) return S.leaders[id];
  if (typeof id === "string" && id.slice(0, 2) === "L:") {
    var lw = S.leaders[id.slice(2)];
    if (lw) return lw;
  }
  return S.workers[id] || S.leader;
}
/* click-to-focus: the camera follows one entity until released.
   Stale ids (removed workers) fall back to the leader automatically. */
function focusValid(id) {
  if (!id || id === "leader") return true;
  if (S.workers[id] || S.leaders[id]) return true;
  if (typeof id === "string" && id.slice(0, 2) === "L:" && S.leaders[id.slice(2)]) return true;
  return false;
}
function focusTarget() {
  if (S.focus && !focusValid(S.focus)) S.focus = null;
  var f = S.focus ? getEntity(S.focus) : null;
  return f || S.leader;
}
/* leader entity key for a session: the adopted main body answers "leader" */
function lkeyE(sid) {
  if (!sid) return "leader";
  var w = S.leaders[sid];
  if (!w) return "leader";
  return (w === S.leader) ? "leader" : ("L:" + sid);
}
function bossOf(id) {
  var w = (id && id !== "leader") ? (S.workers[id] || null) : null;
  return (w && w.sid) || null;
}
/* NEST pulse law: a nested child's messages (command/result/plan/radio)
   belong to its LIVE parent, never to the NEXUS leader. Top-level agents
   still answer to their session leader. */
function pulseHome(id) {
  var w = (id && id !== "leader") ? (S.workers[id] || null) : null;
  if (w && w.parent && S.workers[w.parent] && !S.workers[w.parent].deadAt) return w.parent;
  return lkeyE(bossOf(id));
}

/* ============================ 3. DOM / OVERLAY =========================== */
var cv = null, wrap = null, labelLayer = null, hudEl = null, tipEl = null;
var labelDivs = {};        // id -> {box,name,sub,chips}
function ensureDom() {
  cv = $("coreCanvas");
  wrap = $("entityWrap") || (cv ? cv.parentNode : null);
  labelLayer = $("entityLabels");
  hudEl = $("entityHud");
  tipEl = $("entityTip");
  if (cv && !labelLayer && wrap) {
    labelLayer = document.createElement("div");
    labelLayer.id = "entityLabels";
    wrap.appendChild(labelLayer);
  }
  if (cv && !hudEl && wrap) {
    hudEl = document.createElement("div");
    hudEl.id = "entityHud";
    wrap.appendChild(hudEl);
  }
  if (cv && !tipEl && wrap) {
    tipEl = document.createElement("div");
    tipEl.id = "entityTip";
    wrap.appendChild(tipEl);
  }
}
function makeLabel(id, title, tint) {
  if (!labelLayer || labelDivs[id]) return labelDivs[id];
  var box = document.createElement("div");
  box.className = "e3-label" + (id === "leader" ? " leader" : "");
  box.innerHTML =
    '<div class="e3-name"><i style="background:' + escHtml(tint) + '"></i>' +
    '<b>' + escHtml(title) + '</b><span class="e3-st"></span></div>' +
    '<div class="e3-sub"></div>' +
    '<div class="e3-chips"></div>';
  labelLayer.appendChild(box);
  var rec = { box: box,
    st: box.querySelector(".e3-st"),
    sub: box.querySelector(".e3-sub"),
    chips: box.querySelector(".e3-chips") };
  labelDivs[id] = rec;
  return rec;
}
function dropLabel(id) {
  var rec = labelDivs[id];
  if (rec && rec.box.parentNode) rec.box.parentNode.removeChild(rec.box);
  delete labelDivs[id];
}
/* ====================== 4. CANVAS TEXTURES (shared) ====================== */
var TEX = { glow: null, dot: null, ring: null, glyphs: {} };
function canvasTex(size, draw) {
  var c = document.createElement("canvas");
  c.width = c.height = size;
  draw(c.getContext("2d"), size);
  return c;
}
function buildSharedTextures() {
  if (TEX.glow) return;
  /* soft radial glow (core halo, floor light, pulse heads) */
  TEX.glow = canvasTex(128, function (g, s) {
    var r = g.createRadialGradient(s/2, s/2, 1, s/2, s/2, s/2);
    r.addColorStop(0.00, "rgba(255,250,235,1)");
    r.addColorStop(0.22, "rgba(249,231,176,0.9)");
    r.addColorStop(0.50, "rgba(212,175,55,0.32)");
    r.addColorStop(0.78, "rgba(150,105,30,0.10)");
    r.addColorStop(1.00, "rgba(120,85,20,0)");
    g.fillStyle = r; g.fillRect(0, 0, s, s);
  });
  /* tight hot dot (embers read as crisp sparks even zoomed) */
  TEX.dot = canvasTex(64, function (g, s) {
    var r = g.createRadialGradient(s/2, s/2, 1, s/2, s/2, s/2);
    r.addColorStop(0.00, "rgba(255,252,240,1)");
    r.addColorStop(0.18, "rgba(255,246,220,1)");
    r.addColorStop(0.38, "rgba(232,190,100,0.55)");
    r.addColorStop(0.70, "rgba(180,135,50,0.12)");
    r.addColorStop(1.00, "rgba(150,110,40,0)");
    g.fillStyle = r; g.fillRect(0, 0, s, s);
  });
  /* thin ring (shockwaves) */
  TEX.ring = canvasTex(256, function (g, s) {
    g.strokeStyle = "rgba(255,255,255,0.95)";
    g.lineWidth = 7;
    g.beginPath(); g.arc(s/2, s/2, s/2 - 12, 0, Math.PI * 2); g.stroke();
    g.strokeStyle = "rgba(255,255,255,0.30)";
    g.lineWidth = 16;
    g.beginPath(); g.arc(s/2, s/2, s/2 - 16, 0, Math.PI * 2); g.stroke();
  });
}
function glyphTex(ch, color) {
  var key = ch + "|" + color;
  if (TEX.glyphs[key]) return TEX.glyphs[key];
  var c = canvasTex(96, function (g, s) {
    g.font = "bold 56px Consolas,monospace";
    g.textAlign = "center"; g.textBaseline = "middle";
    g.shadowColor = color; g.shadowBlur = 18;
    g.fillStyle = color;
    g.fillText(ch, s/2, s/2 + 2);
    g.shadowBlur = 0;
    g.fillStyle = "rgba(255,255,255,0.85)";
    g.font = "bold 30px Consolas,monospace";
    g.fillText(ch, s/2, s/2 + 1);
  });
  TEX.glyphs[key] = c;
  return c;
}

/* ==================== 5. PUBLIC API (engine-agnostic) ==================== */
function safe(fn) { try { return fn(); } catch (e) { return undefined; } }
var E = {
  ready: false,
  engine: function () { return S.engine; },

  ping: function (v) {
    S.eT = clamp((S.eT || 0) + (+v || 0), 0, 1.4);
    if (S.leader) S.leader.flare = Math.min(0.9, (S.leader.flare || 0) + (+v || 0) * 0.25);
  },

  setMode: function (m) {
    if (m !== "ERROR" && Date.now() < S.errUntil) return;
    S.mode = m || "IDLE";
    S.hud.mode = S.mode;
    if (S.mode === "WORKING") S.workT = Math.max(S.workT || 0, 0.55);
  },

  errFlash: function () {
    S.errUntil = Date.now() + 2500;
    S.mode = "ERROR"; S.hud.mode = "ERROR";
    S.camShake = 1;
    S.workT = Math.min(1.3, (S.workT || 0) + 0.5);
    if (S.engine === "three") T3.lightPing("leader", "#ff5d5d", 90);
    if (S.leader) { S.leader.flash = 1; S.leader.flashColor = "#ff5d5d"; }
    addRing("leader", "#ff5d5d", 1.6);
    addFloat("leader", "!", "#ff5d5d");
  },

  ensureWorker: function (id, label, tint, boss, parent, depth) {
    if (!id || id === "leader") return S.leader;
    var w = S.workers[id];
    if (boss) w && (w.sid = boss);
    if (w) {
      if (w.deadAt || w.retired) { w.deadAt = 0; w.retired = false; w.status = "queued"; w.bornAt = nowMs(); } // revive -> pop-in
      if (/^#[0-9a-fA-F]{6}$/.test(tint || "")) { w.tint = tint; w.taskColor = tint; w.base = paletteFor(w); }
      if (parent !== undefined) w.parent = parent || null;
      if (depth !== undefined) w.depth = Math.max(1, +depth || 1);
      layoutOrbits();
      return w;
    }
    w = makeEntity(id, label || id, "worker", tint || tintFor(id));
    if (/^#[0-9a-fA-F]{6}$/.test(tint || "")) w.taskColor = tint;
    if (boss) w.sid = boss;
    w.parent = parent || null; // nesting: parent worker KEY (flat ekey form)
    w.depth = Math.max(1, +depth || 1); // 1 = leader-spawned, 2+ = nested
    S.workers[id] = w; S.order.push(id);
    makeLabel(id, (label || id).toUpperCase().slice(0, 12), w.tint);
    layoutOrbits();
    if (S.engine === "three") T3.spawnWorkerNode(w); // spawnWorkerNode guards scene/node itself
    else if (S.engine === "flat") FL.layout();
    S.hud.workers = liveWorkers().length;
    return w;
  },

  /* extra NEXUS entities: one leader-class body per live session.
     The first adopted session keeps the boot (teal) body; peers are gold. */
  ensureLeader: function (sid, label) {
    if (!sid) return S.leader;
    var w = S.leaders[sid];
    if (w) return w;
    if (S.leader && !S.leader.sid) {
      // adopt the boot body for the first session
      S.leader.sid = sid;
      S.leaders[sid] = S.leader;
      if (labelLayer && labelDivs["leader"]) {
        try {
          var rec = labelDivs["leader"];
          var b = rec.box.querySelector("b");
          if (b) b.textContent = label || "NEXUS";
        } catch (e) {}
      }
      return S.leader;
    }
    w = makeEntity("L:" + sid, label || "NEXUS", "peer", GOLD.mid);
    w.sid = sid;
    w.focusDim = 0.55;
    S.leaders[sid] = w;
    makeLabel("L:" + sid, label || "NEXUS", GOLD.mid);
    try {
      if (labelDivs["L:" + sid]) labelDivs["L:" + sid].box.classList.add("leader");
    } catch (e) {}
    var idx = Object.keys(S.leaders).length - 1;
    w._slotPos = { a: idx * 2.4 + 0.5, r: 6.2, y: 1.6 };
    if (S.engine === "three" && T3.spawnPeerNode) T3.spawnPeerNode(w);
    return w;
  },

  /* focus: camera/HUD follow this leader; others dim but stay alive */
  setActiveLeader: function (sid) {
    var w = sid ? S.leaders[sid] : null;
    if (!w) return S.leader;
    S.leader = w;
    Object.keys(S.leaders).forEach(function (k) {
      S.leaders[k].focusDim = (k === sid) ? 1 : 0.55;
    });
    Object.keys(S.workers).forEach(function (k) {
      var o = S.workers[k];
      if (o) o.focusDim = (!o.sid || o.sid === sid) ? 1 : 0.55;
    });
    return w;
  },

  /* camera follow: click any agent to track it; click empty space (or the
     same agent, double-click, ESC) to release back to the leader */
  setFocus: function (id) {
    if (!focusValid(id)) return null;
    S.focus = id || "leader";
    return S.focus;
  },
  clearFocus: function () { S.focus = null; },
  toggleFocus: function (id) {
    if (!id || !focusValid(id)) { S.focus = null; return null; }
    S.focus = (S.focus === id) ? null : id;
    return S.focus;
  },
  getFocus: function () { return S.focus; },

  setWorkerStatus: function (id, st) {
    var w = (id === "leader") ? S.leader : S.workers[id];
    if (w) w.status = st || w.status;
    if (w && st === "working") { kickAct(id, 0.5); kickExc(id, 0.3); }
  },

  setRound: function (id, round) {
    var w = (id === "leader") ? S.leader : S.workers[id];
    if (w && round) w.round = String(round).slice(0, 24);
    if (w && w.kind === "worker") layoutOrbits();
  },

  /* layout law switch (/layout): flow = computed-map scatter around the
     caller, lattice = imaginary sphere-grid around the caller. Relayouts
     live; every future join/leave uses the active law. */
  setLayoutMode: function (m) {
    m = (String(m || "").toLowerCase() === "lattice") ? "lattice" : "flow";
    if (S.layoutMode === m) return m;
    S.layoutMode = m;
    try { layoutOrbits(); } catch (e) {}
    return m;
  },
  getLayoutMode: function () { return S.layoutMode || "flow"; },

  setLastAction: function (id, txt) {
    var w = ensureOwner(id);
    if (w) w.lastAction = String(txt || "").slice(0, 42);
  },

  /* retire with a 6s golden/green (ok) or red (fail) fade, like before */
  /* retired agents stay on stage drifting slowly (dimmed), never vanish.
     Only beyond 12 retired is the oldest evicted. */
  retireWorker: function (id, ok) {
    var w = S.workers[id];
    if (!w) return;
    w.status = ok ? "done" : "fail";
    w.ok = !!ok;
    w.retired = false;
    w.deadAt = nowMs();
    w.lastAction = ok ? "done" : "FAILED";
    layoutOrbits();
    w.orbiters.length = 0;
    w.halo = null;
    w.themeStack = []; tintDialOf(w);
    addRing("leader", ok ? "#3dffa2" : "#ff5d5d", 0.9);
    var old = S.order.filter(function (k) { return S.workers[k] && S.workers[k].retired; });
    if (old.length > 12) {
      var drop = old[0];
      if (S.engine === "three") T3.removeWorkerNode(drop);
      else {
        delete S.workers[drop];
        var ix = S.order.indexOf(drop);
        if (ix >= 0) S.order.splice(ix, 1);
        dropLabel(drop);
        layoutOrbits();
      }
    }
  },

  clearWorkers: function () {
    safe(function () { T3.purgeSignals(); });
    Object.keys(S.workers).forEach(dropLabel);
    if (S.engine === "three") T3.clearWorkerNodes();
    S.workers = {}; S.order = [];
    S.pulses.length = 0; S.arcs.length = 0;
    S.hud.workers = 0; S.usedMaxR = 5.8;
  },

  resetAll: function () {
    safe(function () { T3.purgeSignals(); });
    S.focus = null;
    E.clearWorkers();
    // drop peer NEXUS bodies + labels (fresh boot state; focus uses setActiveLeader instead)
    Object.keys(S.leaders).forEach(function (k) {
      var w = S.leaders[k];
      if (w && w !== S.leader && w.node && T3.scene) {
        try { T3.scene.remove(w.node.group); } catch (e) {}
      }
      dropLabel("L:" + k);
      delete S.leaders[k];
    });
    if (S.leader) S.leader.sid = null; // next ensureLeader re-adopts the boot body
    S.pulses.length = 0; S.arcs.length = 0;
    S.bursts.length = 0; S.rings.length = 0;
    S.floats.length = 0; S.beams.length = 0; S.flashes.length = 0;
    S.energy = 0; S.camShake = 0;
    if (S.leader) {
      S.leader.orbiters.length = 0; S.leader.flash = 0; S.leader.act = 0; S.leader.actT = 0; S.leader.exc = 0; S.leader.excT = 0;
      S.leader.think = 0; S.leader.spinKick = 0; S.leader.halo = null;
      S.leader.themeStack = []; tintDialOf(S.leader);
      S.leader.lastAction = "";
    }
    S.hud.tools = 0;
  },

  /* violet command pulse caller -> worker (parent-aware: nested children
     answer to their LIVE parent, top-level to their session leader) */
  _pulseLeader: function (id) { return pulseHome(id); },
  commandPulseFrom: function (anchor, id, n) {
    var home = anchor || E._pulseLeader(id);
    S.workT = Math.min(1.3, (S.workT || 0) + 0.35);
    kickAct(id, 0.5); kickAct(home, 0.25); kickExc(id, 0.3);
    if (S.engine === "three") T3.lightPing(home, "#b48cff", 25);
    var k = Math.max(1, Math.min(4, n || 1));
    for (var i = 0; i < k; i++) {
      S.pulses.push({ a: home, b: id, t: -Math.random() * 0.35,
        sp: rand(0.020, 0.034), color: "#b48cff", arch: 0 });
    }
    trimPulses();
    E.ping(0.25);
  },
  commandPulse: function (id, n) {
    E.commandPulseFrom(E._pulseLeader(id), id, n);
  },

  resultPulseTo: function (id, anchor, ok) {
    var home = anchor || E._pulseLeader(id);
    kickAct(id, 0.45); kickExc(id, 0.3);
    S.pulses.push({ a: id, b: home, t: 0, sp: 0.030,
      color: ok ? "#3dffa2" : "#ff5d5d", arch: 0 });
    trimPulses();
    E.ping(ok ? 0.3 : 0.5);
  },
  resultPulse: function (id, ok) {
    E.resultPulseTo(id, E._pulseLeader(id), ok);
  },

  peerPulse: function (a, b) {
    S.workT = Math.min(1.3, (S.workT || 0) + 0.2);
    kickAct(a, 0.3); kickAct(b, 0.3); kickExc(a, 0.15); kickExc(b, 0.15);
    S.arcs.push({ a: a, b: b, ttl: 1 });
    S.pulses.push({ a: a, b: b, t: 0, sp: 0.026, color: "#ffb454", arch: 1 });
    trimPulses();
    E.ping(0.12);
  },

  leaderPulse: function (color) {
    S.pulses.push({ a: "leader", b: "leader", t: 0, sp: 0.05,
      color: color || "#ffe9a8", arch: 0 });
    trimPulses();
  },

  spawnFlash: function (id) {
    S.workT = Math.min(1.3, (S.workT || 0) + 0.3);
    if (S.engine === "three") T3.lightPing(id, "#ffffff", 55);
    var w = getEntity(id);
    kickAct(id, 0.4); kickExc(id, 0.25);
    if (w) { w.flash = 0.65; w.flashColor = GOLD.hot; w.spinKick = Math.min(1.4, w.spinKick + 0.9); }
    addRing(id, "#ffe9a8", 1.2);
    addFlash(id, "#ffe9a8", 1.4);
    E.ping(0.35);
  },

  thinkingPulse: function (who) {
    kickAct(who, 0.5); kickExc(who, 0.3);
    S.workT = Math.min(1.3, (S.workT || 0) + 0.4);
    var w = ensureOwner(who);
    if (w && w.status !== "done" && w.status !== "fail") { w.think = 1; w.lastAction = "thinking\u2026"; }
    E.ping(0.2);
  },

  planPulse: function (who) {
    kickAct(who, 0.4); kickExc(who, 0.2);
    S.workT = Math.min(1.3, (S.workT || 0) + 0.3);
    if (S.engine === "three") T3.lightPing(who, "#3dffa2", 30);
    addRing(who, "#3dffa2", 1.0);
    addFloat(who, "\u2713", "#3dffa2");
    var w = getEntity(who);
    if (w) w.spinKick = Math.min(1.2, w.spinKick + 0.45);
    S.pulses.push({ a: who, b: E._pulseLeader(who), t: 0, sp: 0.028, color: "#b48cff", arch: 0 });
    trimPulses();
    E.ping(0.15);
  },

  /* ---- per-tool choreography ---- */
  toolStart: function (owner, tool, label) {
    var fx = toolFx(tool);
    kickAct(owner, 0.65); kickExc(owner, 0.35);
    S.workT = Math.min(1.3, (S.workT || 0) + 0.55);
    if (S.engine === "three") T3.lightPing(owner, fx.color, 45);
    var w = ensureOwner(owner);
    if (!w) return;
    w.orbiters.push({ tool: tool, color: fx.color, glyph: fx.glyph,
      angle: Math.random() * Math.PI * 2, r: 0, speed: rand(1.6, 2.6) });
    if (w.orbiters.length > 6) w.orbiters.shift();
    w.spinKick = Math.min(1.5, w.spinKick + fx.spin);
    w.flash = Math.max(w.flash, 0.45); w.flashColor = fx.color;
    if (fx.halo) w.halo = { color: fx.color };
    pushTheme(w, tool);
    if (fx.beam) S.beams.push({ at: w.id, color: fx.color, t: 0, ttl: 2.6 });
    addBurst(w.id, fx.color, fx.burst);
    if (tool === "agent.say" && w.id !== "leader")
      S.pulses.push({ a: w.id, b: "leader", t: 0, sp: 0.03, color: fx.color, arch: 1 });
    E.ping(0.22);
  },

  toolDone: function (owner, tool, ok, elapsed) {
    var fx = toolFx(tool);
    ensureOwner(owner);
    kickAct(owner, 0.4); kickExc(owner, 0.3);
    S.workT = Math.min(1.3, (S.workT || 0) + 0.35);
    var w = getEntity(owner);
    if (w) {
      for (var i = w.orbiters.length - 1; i >= 0; i--) {
        if (w.orbiters[i].tool === tool) { w.orbiters.splice(i, 1); break; }
      }
      if (!w.orbiters.length) w.halo = null;
      w.lastAction = tool + " " + (+elapsed || 0).toFixed(1) + "s";
      w.flash = Math.max(w.flash, 0.6);
      popTheme(w, tool);
      w.flashColor = ok ? "#3dffa2" : "#ff5d5d";
    }
    if (S.engine === "three") T3.lightPing(owner, ok ? "#3dffa2" : "#ff5d5d", 65);
    if (fx.ring) addRing(owner, ok ? fx.color : "#ff5d5d", ok ? 0.9 : 1.2);
    addFloat(owner, ok ? "\u2713" : "\u2718", ok ? "#3dffa2" : "#ff5d5d");
    if (!ok) { S.camShake = Math.max(S.camShake, 0.7); }
    E.ping(ok ? 0.3 : 0.5);
  },

  /* turn completion: a calm settling glow, never an explosion */
  finalBurst: function (ok, at) {
    var tgt = at || "leader";
    addRing(tgt, ok === false ? "#ff5d5d" : NEXUS.hot, 0.7);
    var lw = getEntity(tgt);
    if (lw) {
      lw.flare = Math.min(1.2, (lw.flare || 0) + 0.55);
      lw.spinKick = Math.min(1.2, lw.spinKick + 0.3);
    }
    if (S.engine === "three") T3.lightPing(tgt, NEXUS.hot, 22);
    E.ping(0.25);
  },

  /* safety net: live workers must have bodies — respawn any missing node */
  reconcile: function () {
    var ids = S.order.slice();
    for (var i = 0; i < ids.length; i++) {
      var w = S.workers[ids[i]];
      if (!w || w.deadAt) continue;
      if (!w.node) {
        if (S.engine === "three") T3.spawnWorkerNode(w);
        else if (S.engine === "flat") FL.layout();
      }
    }
  },
  setHud: function (h) {
    if (!h) return;
    if (h.mode !== undefined) S.hud.mode = h.mode;
    if (h.tools !== undefined) S.hud.tools = h.tools;
    if (h.workers !== undefined) S.hud.workers = h.workers;
    if (h.round !== undefined) S.hud.round = h.round;
    if (h.model !== undefined) S.hud.model = h.model;
    if (h.mem !== undefined) S.hud.mem = h.mem;
    if (h.sess !== undefined) S.hud.sess = h.sess;
    if (h.link !== undefined) S.hud.link = h.link;
    if (h.clock !== undefined) S.hud.clock = h.clock;
  }
};

/* per-agent acceleration (real, not synced): every body owns act/actT.
   Events kick ONLY the body that really works (toolStart/toolDone/thinking/
   radio/plan on that id). The global S.work stays for background/camera. */
function resolveAct(id) {
  if (!id || id === "leader") return S.leader;
  if (typeof id === "string" && id.slice(0, 2) === "L:") return S.leaders[id.slice(2)] || null;
  return S.workers[id] || null;
}
function kickAct(id, v) {
  var w = resolveAct(id);
  if (!w) return;
  w.actT = Math.min(1.3, (w.actT || 0) + (+v || 0));
}
function kickExc(id, v) {
  var w = resolveAct(id);
  if (!w) return;
  w.excT = Math.min(1.3, (w.excT || 0) + (+v || 0));
}
function eachActEntity(fn) {
  try { if (S.leader) fn(S.leader); } catch (e) {}
  for (var i = 0; i < S.order.length; i++) {
    var w = S.workers[S.order[i]];
    if (w) { try { fn(w); } catch (e) {} }
  }
  for (var k in S.leaders) {
    var q = S.leaders[k];
    if (q && q !== S.leader) { try { fn(q); } catch (e) {} }
  }
}
/* the center must stay fast while ANYTHING runs: leader is pinned while any
   live worker really works (status/act/orbiters/think) or anything is hot. */
function anyRealWork() {
  if ((S.work || 0) > 0.12) return true;
  for (var i = 0; i < S.order.length; i++) {
    var w = S.workers[S.order[i]];
    if (!w || w.deadAt) continue;
    if (w.status === "working") return true;
    if ((w.act || 0) > 0.08) return true;
    if (w.orbiters && w.orbiters.length) return true;
    if ((w.think || 0) > 0.25) return true;
  }
  if (S.leader && S.leader.orbiters && S.leader.orbiters.length) return true;
  return false;
}
/* eased channels: values glide toward their targets at fixed rates, so
   acceleration always ramps (attack) and relaxes (release) gradually */
function smoothChannels(dt) {
  S.eT = (S.eT || 0) * Math.pow(0.25, dt);
  var de = (S.eT || 0) - S.energy;
  S.energy = clamp(S.energy + clamp(de, -1.1 * dt, 1.6 * dt), 0, 1.4);
  S.workT = (S.workT || 0) * Math.pow(0.4, dt);
  var dw = (S.workT || 0) - S.work;
  S.work = clamp(S.work + clamp(dw, -0.5 * dt, 1.4 * dt), 0, 1.3);
  eachActEntity(function (w) {
    w.actT = (w.actT || 0) * Math.pow(0.4, dt);
    var dd = (w.actT || 0) - (w.act || 0);
    w.act = clamp((w.act || 0) + clamp(dd, -0.5 * dt, 1.4 * dt), 0, 1.3);
    w.excT = (w.excT || 0) * Math.pow(0.25, dt);
    var de2 = (w.excT || 0) - (w.exc || 0);
    w.exc = clamp((w.exc || 0) + clamp(de2, -1.1 * dt, 1.6 * dt), 0, 1.4);
  });
  if (S.leader && anyRealWork()) {
    S.leader.actT = Math.max(S.leader.actT || 0, 0.95);
    S.leader.act = Math.max(S.leader.act || 0, 0.95);
    S.leader.excT = Math.max(S.leader.excT || 0, 0.8);
    S.leader.exc = Math.max(S.leader.exc || 0, 0.8);
  }
}
/* thinking mode: light violet, slow majestic drift, deep breathing */
var THINK_FX = { color: "#c9a6ff", theme: "#c9a6ff",
  spinMul: 0.55, bobMul: 1.6, ringMul: 0.6 };
/* a tool/thinking event must never theme the wrong body: unknown owners
   get their worker spawned on the spot */
function ensureOwner(id) {
  if (id && id !== "leader" && !S.workers[id]) E.ensureWorker(id, id);
  return getEntity(id);
}
/* theme stack per entity: newest running tool/state owns the whole look */
function pushTheme(w, key) {
  if (!w) return;
  w.themeStack = w.themeStack || [];
  var fx = (key === "thinking") ? THINK_FX : toolFx(key);
  w.themeStack.push({ key: key, fx: fx });
  tintDialOf(w);
}
function popTheme(w, key) {
  if (!w || !w.themeStack) return;
  for (var i = w.themeStack.length - 1; i >= 0; i--) {
    if (w.themeStack[i].key === key) { w.themeStack.splice(i, 1); break; }
  }
  tintDialOf(w);
}
function themeOf(w) {
  if (!w || !w.themeStack || !w.themeStack.length) return null;
  var top = w.themeStack[w.themeStack.length - 1];
  return { color: top.fx.theme || top.fx.color,
    spinMul: top.fx.spinMul || 1, bobMul: top.fx.bobMul || 1,
    ringMul: top.fx.ringMul || 1 };
}
/* dial ticks re-tint on theme change (rare: only on push/pop) */
function tintDialOf(w) {
  if (!w || !w.node || !w.node.dial || !T3.THREE) return;
  if (S.engine !== "three") return;
  var thm = themeOf(w);
  var THREE = T3.THREE;
  var BB = w.base || GOLD;
  var cGold = new THREE.Color(BB.mid), cHot = new THREE.Color(BB.hot),
      cDim = new THREE.Color(BB.dim);
  var tc = thm ? new THREE.Color(thm.color) : null;
  var n = w.node.dial.count || 72;
  for (var di = 0; di < n; di++) {
    var base = (di % 6 === 0 ? cHot : (di % 2 ? cGold : cDim)).clone();
    if (tc) base.lerp(tc, 0.65);
    w.node.dial.setColorAt(di, base);
  }
  if (w.node.dial.instanceColor) w.node.dial.instanceColor.needsUpdate = true;
}
/* glide every tintable material toward the active theme (smooth, per frame) */
function applyThemeColors(w, n, dt) {
  if (!T3.tmpCol || !T3.tmpCol2) return;
  var thm = themeOf(w);
  var B = w.base || GOLD;
  var rate = 1 - Math.pow(0.02, dt);
  var C = T3.tmpCol, D = T3.tmpCol2;
  var satBoost = (w.kind === "worker") ? 0.15 : 0; // small bodies: push harder
  function glide(mat, base, amt) {
    if (!mat || !mat.color) return;
    C.set(base);
    if (thm) { D.set(thm.color); C.lerp(D, Math.min(1, (amt == null ? 0.7 : amt) + satBoost)); }
    mat.color.lerp(C, rate);
  }
  glide(n.coreMat, B.core);
  glide(n.kernelMat, "#ffffff", 0.5);
  glide(n.glow.material, B.mid, 0.8);
  glide(n.atmoMat, B.mid, 0.8);
  glide(n.shellMat, B.mid, 0.75);
  if (n.outer) glide(n.outer.material, B.dim, 0.7);
  if (thm && n.emberMat) { D.set(thm.color); n.emberMat.color.lerp(D, rate * 0.65); }
  glide(n.dustMat, "#ffffff", 0.6);
  glide(n.moteMat, B.hot, 0.7);
  if (S.leader === w && T3.floorGlow) glide(T3.floorGlow.material, B.mid, 0.35);
  for (var i = 0; n.rings && i < n.rings.length; i++) {
    var rgm = n.rings[i];
    if (rgm.userData.keep || rgm.userData.baseHex == null) continue;
    glide(rgm.material, rgm.userData.baseHex, 0.7);
  }
}
function trimPulses() {
  if (S.pulses.length > 80) S.pulses.splice(0, S.pulses.length - 80);
  if (S.arcs.length > 24) S.arcs.splice(0, S.arcs.length - 24);
}
function addRing(at, color, size) {
  S.rings.push({ at: at, color: color, t: 0, ttl: 1.1, size: size || 1 });
  if (S.rings.length > 24) S.rings.shift();
}
function addBurst(at, color, n) {
  S.bursts.push({ at: at, color: color, t: 0, ttl: 1.4, n: Math.min(130, n || 30) });
  if (S.bursts.length > 16) S.bursts.shift();
}
function addFloat(at, txt, color) {
  S.floats.push({ at: at, txt: txt, color: color, t: 0, ttl: 1.5 });
  if (S.floats.length > 20) S.floats.shift();
}
function addFlash(at, color, size) {
  S.flashes.push({ at: at, color: color, t: 0, ttl: 0.55, size: size || 1 });
  if (S.flashes.length > 12) S.flashes.shift();
}

/* ================= 6. THREE.JS LOADER (CDN -> fallback) ================== */
var THREE_URLS = [
  "https://unpkg.com/three@0.160.0/build/three.module.js",
  "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js"
];
function loadThree(i) {
  return safe(function () {
    var p;
    if (i < THREE_URLS.length) {
      p = import(THREE_URLS[i]).catch(function () { return loadThree(i + 1); });
    } else {
      p = Promise.reject(new Error("no-cdn"));
    }
    return p;
  }) || Promise.reject(new Error("no-import"));
}
function boot() {
  ensureDom();
  buildSharedTextures();
  if (!cv) return; // no ENTITY panel on this page
  S.leader = makeEntity("leader", "NEXUS", "leader", NEXUS.mid);
  makeLabel("leader", "NEXUS", NEXUS.mid);
  loadThree(0).then(function (mod) {
    var THREE = mod && (mod.default || mod);
    if (!THREE || !THREE.Scene) throw new Error("bad-three");
    S.engine = "three";
    T3.init(THREE);
    E.ready = true;
  }).catch(function () {
    S.engine = "flat";
    FL.init();
    E.ready = true;
  });
}
/* ============ 7. THREE ENGINE (golden hologram world) ==================== */
var T3 = {
  THREE: null, renderer: null, scene: null, camera: null,
  root: null, stars: null, floorGlow: null,
  tetherLines: {},   // workerId -> THREE.Line
  arcLines: [],      // {line, rec}
  pulseSprites: [],  // {sp, rec}
  fx: [],            // {kind, obj, ...} transient fx meshes
  burstPool: [],
  dragAz: 0, dragEl: 0, mouseX: 0, mouseY: 0,
  panX: 0, panZ: 0, userZoomT: 0, // map pan offset (world units, clamped)
  dragging: false, lastPX: 0, lastPY: 0,
  reconTick: 0, camDist: 10.6,
  ndcX: 0, ndcY: 0, px: 0, py: 0, // pointer for parallax + hover raycast
  raycaster: null, hoverTick: 0, look: null,
  calmCol: null, hotCol: null, tmpCol: null, tmpCol2: null,
  tetherTips: {},   // workerId -> [tipA, tipB] endpoint glow sprites
  time: 0
};

T3.goldMat = function (color, opacity) {
  return new T3.THREE.MeshBasicMaterial({
    color: new T3.THREE.Color(color),
    transparent: true, opacity: opacity == null ? 1 : opacity,
    blending: T3.THREE.AdditiveBlending, depthWrite: false
  });
};
/* real shaded metal for rings/shells: reads as solid gold when zoomed */
T3.goldMetal = function (color, emissiveIntensity) {
  return new T3.THREE.MeshStandardMaterial({
    color: new T3.THREE.Color(color),
    metalness: 0.9, roughness: 0.3,
    emissive: new T3.THREE.Color("#2a1a05"),
    emissiveIntensity: emissiveIntensity == null ? 0.9 : emissiveIntensity,
    transparent: true, opacity: 0.96
  });
};
T3.goldWire = function (color, opacity) {
  return new T3.THREE.MeshStandardMaterial({
    color: new T3.THREE.Color(color),
    metalness: 0.85, roughness: 0.38,
    emissive: new T3.THREE.Color("#1c1002"),
    emissiveIntensity: 1.0,
    wireframe: true, transparent: true,
    opacity: opacity == null ? 0.22 : opacity
  });
};
/* hologram beam material: translucent additive light, never solid metal */
T3.holoMat = function (color, opacity) {
  return new T3.THREE.MeshBasicMaterial({
    color: new T3.THREE.Color(color),
    transparent: true, opacity: opacity == null ? 0.5 : opacity,
    blending: T3.THREE.AdditiveBlending, depthWrite: false
  });
};
/* hologram wireframe: pure light lattice */
T3.holoWire = function (color, opacity) {
  return new T3.THREE.MeshBasicMaterial({
    color: new T3.THREE.Color(color),
    wireframe: true, transparent: true,
    opacity: opacity == null ? 0.15 : opacity,
    blending: T3.THREE.AdditiveBlending, depthWrite: false
  });
};
T3.texSprite = function (texCanvas, color, scale, opacity) {
  var THREE = T3.THREE;
  var tex = new THREE.CanvasTexture(texCanvas);
  var m = new THREE.SpriteMaterial({
    map: tex, color: new THREE.Color(color || "#ffffff"),
    transparent: true, opacity: opacity == null ? 1 : opacity,
    blending: THREE.AdditiveBlending, depthWrite: false
  });
  var sp = new THREE.Sprite(m);
  sp.scale.set(scale, scale, 1);
  return sp;
};

T3.init = function (THREE) {
  T3.THREE = THREE;
  var W = 800, H = 300;
  try {
    T3.renderer = new THREE.WebGLRenderer({ canvas: cv, antialias: true, alpha: false });
  } catch (e) { FL.init(); S.engine = "flat"; return; }
  T3.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  T3.renderer.setClearColor(new THREE.Color("#040a10"), 1);
  T3.scene = new THREE.Scene();
  T3.scene.fog = new THREE.FogExp2(new THREE.Color("#040a10"), 0.028);
  T3.camera = new THREE.PerspectiveCamera(50, W / H, 0.1, 120);
  T3.camera.position.set(0, 1.0, CAM_DIST0);
  T3.camDist = CAM_DIST0;
  T3.look = new THREE.Vector3(0, 0.1, 0);
  T3.calmCol = new THREE.Color("#a8843f");
  T3.hotCol = new THREE.Color("#ffffff");
  T3.tmpCol = new THREE.Color("#ffffff");
  T3.tmpCol2 = new THREE.Color("#ffffff");
  T3.root = new THREE.Group();
  T3.scene.add(T3.root);

  /* faint blueprint grid dots far behind (cheap: one Points cloud) */
  (function gridDots() {
    var n = 260, pos = new Float32Array(n * 3), i;
    for (i = 0; i < n; i++) {
      pos[i*3] = rand(-14, 14); pos[i*3+1] = rand(-6, 7); pos[i*3+2] = rand(-9, -4);
    }
    var g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    var m = new THREE.PointsMaterial({ color: 0x1a5a5e, size: 0.05,
      transparent: true, opacity: 0.7, depthWrite: false });
    T3.scene.add(new THREE.Points(g, m));
  })();

  /* deep-space starfield: far veil + near bright layer, counter-twinkling */
  (function stars() {
    function layer(n, rMin, rMax, size, op) {
      var pos = new Float32Array(n * 3), col = new Float32Array(n * 3), i, r, th, ph;
      var palette = [[1, 1, 1], [0.72, 0.93, 0.9], [1.0, 0.9, 0.66], [0.62, 0.72, 1.0]];
      for (i = 0; i < n; i++) {
        r = rand(rMin, rMax); th = rand(0, Math.PI * 2); ph = Math.acos(rand(-1, 1));
        pos[i*3] = r * Math.sin(ph) * Math.cos(th);
        pos[i*3+1] = r * Math.cos(ph) * 0.6;
        pos[i*3+2] = -Math.abs(r * Math.sin(ph) * Math.sin(th)) - 4;
        var c = palette[(Math.random() * palette.length) | 0];
        col[i*3] = c[0]; col[i*3+1] = c[1]; col[i*3+2] = c[2];
      }
      var g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
      g.setAttribute("color", new THREE.BufferAttribute(col, 3));
      var m = new THREE.PointsMaterial({ size: size, map: new THREE.CanvasTexture(TEX.dot),
        vertexColors: true, transparent: true, opacity: op,
        depthWrite: false, blending: THREE.AdditiveBlending });
      var pts = new THREE.Points(g, m);
      T3.scene.add(pts);
      return pts;
    }
    T3.starsFar = layer(650, 24, 60, 0.14, 0.6);
    T3.starsNear = layer(170, 14, 26, 0.22, 0.85);
  })();

  /* deep-space nebulae: huge faint gas veils drifting behind everything */
  (function nebulae() {
    if (!T3.nebulaTex) {
      var nc = document.createElement("canvas");
      nc.width = nc.height = 256;
      var g2 = nc.getContext("2d");
      var blobs = [
        [128, 120, 110, "90,60,140"], [70, 150, 80, "30,110,120"],
        [190, 90, 70, "150,110,50"], [120, 190, 90, "60,50,120"],
        [200, 180, 60, "30,90,110"], [60, 60, 55, "120,70,140"]
      ];
      for (var bi = 0; bi < blobs.length; bi++) {
        var b = blobs[bi];
        var rg2 = g2.createRadialGradient(b[0], b[1], 1, b[0], b[1], b[2]);
        rg2.addColorStop(0, "rgba(" + b[3] + ",0.55)");
        rg2.addColorStop(1, "rgba(" + b[3] + ",0)");
        g2.fillStyle = rg2;
        g2.fillRect(0, 0, 256, 256);
      }
      T3.nebulaTex = new THREE.CanvasTexture(nc);
    }
    T3.nebulae = [];
    var defs = [
      { x: -16, y: 5, z: -30, s: 46, op: 0.16, tint: "#7a6aff" },
      { x: 14, y: -3, z: -34, s: 52, op: 0.13, tint: "#3aa8a0" },
      { x: 2, y: 9, z: -40, s: 60, op: 0.10, tint: "#8a6a3a" }
    ];
    for (var ni = 0; ni < defs.length; ni++) {
      var d = defs[ni];
      var m = new THREE.SpriteMaterial({ map: T3.nebulaTex,
        color: new THREE.Color(d.tint), transparent: true, opacity: d.op,
        depthWrite: false });
      var sp = new THREE.Sprite(m);
      sp.position.set(d.x, d.y, d.z);
      sp.scale.set(d.s, d.s, 1);
      T3.scene.add(sp);
      T3.nebulae.push(sp);
    }
  })();

  T3.floorGlow = T3.texSprite(TEX.glow, GOLD.mid, 12, 0.30);
  /* formation paths are per-worker now (T3.ensureOrbitLine); no shared rings */
  T3.orbitRings = [];
  T3.meteors = []; T3.meteorT = 4; // rare shooting stars
  T3.floorGlow.position.set(0, -2.3, 0);
  T3.scene.add(T3.floorGlow);

  /* stage lighting: warm key + cool rim so the metal reads as metal */
  (function stageLights() {
    var amb = new THREE.AmbientLight(new THREE.Color("#33414d"), 0.85);
    T3.scene.add(amb);
    var key = new THREE.PointLight(new THREE.Color("#ffd27a"), 110, 60, 1.8);
    key.position.set(5.5, 4.5, 7.5);
    T3.scene.add(key);
    var rim = new THREE.DirectionalLight(new THREE.Color("#6fd8ff"), 0.55);
    rim.position.set(-6, 2.5, -5);
    T3.scene.add(rim);
    var under = new THREE.PointLight(new THREE.Color("#c47b2b"), 34, 22, 2);
    under.position.set(0, -3.2, 2.5);
    T3.scene.add(under);
  })();
  /* + one event light: every real event splashes its color onto the scene */
  T3.eventLight = new THREE.PointLight(new THREE.Color("#ffffff"), 0, 34, 1.9);
  T3.eventLight.position.set(0, 0.5, 2);
  T3.scene.add(T3.eventLight);

  /* leader entity — centered, large */
  T3.buildEntityNode(S.leader, LEADER_R, 1500, 110);
  S.leader.node.group.position.set(0, 0.15, 0);
  T3.scene.add(S.leader.node.group);
  /* adopt workers that arrived while Three.js was still loading */
  for (var wi = 0; wi < S.order.length; wi++) {
    var ww = S.workers[S.order[wi]];
    if (ww && !ww.node && !ww.deadAt) {
      makeLabel(ww.id, String(ww.label || ww.id).toUpperCase().slice(0, 12), ww.tint);
      T3.spawnWorkerNode(ww);
    }
  }

  T3.bindInput();
  T3.resize();
  window.addEventListener("resize", T3.resize);
  try {
    if (window.ResizeObserver) {
      var roScheduled = false;
      var ro = new window.ResizeObserver(function () {
        if (roScheduled) return;
        roScheduled = true;
        setTimeout(function () { roScheduled = false; T3.resize(); }, 120);
      });
      ro.observe(wrap);
    }
  } catch (e) { /* resize listener above is enough */ }
  E._toScreen = T3.toScreen;
  requestAnimationFrame(T3.loop);
};

T3.lightPing = function (at, color, power) {
  if (!T3.eventLight) return;
  var p = T3.entityPos(at);
  if (!p) return;
  T3.eventLight.position.set(p.x, p.y + 0.4, p.z + 1.2);
  try { T3.eventLight.color.set(color || "#ffffff"); } catch (e) { /* ignore */ }
  T3.eventLight.intensity = Math.max(T3.eventLight.intensity, power || 40);
};

/* spiral energy-vein curve inside the sphere */
T3.filamentCurve = function (R, seed) {
  var THREE = T3.THREE;
  var pts = [], N = 90, turns = 2.1 + (seed % 2.7) * 0.45;
  for (var i = 0; i <= N; i++) {
    var f = i / N;
    var a = f * Math.PI * 2 * turns + seed * 1.7;
    var rr = R * (0.28 + 0.58 * Math.abs(Math.sin(f * Math.PI * 2 + seed)));
    pts.push(new THREE.Vector3(
      Math.cos(a) * rr,
      Math.sin(f * Math.PI * 3 + seed * 2.3) * R * 0.55,
      Math.sin(a) * rr));
  }
  return new THREE.CatmullRomCurve3(pts);
};

/* ---- entity construction: layered ember sphere ---- */
T3.buildEntityNode = function (w, R, emberN, moteN) {
  var THREE = T3.THREE;
  var group = new THREE.Group();
  var node = { group: group, R: R, rings: [], orbiterSprites: [],
    emberPts: null, motePts: null, moteDat: null,
    core: null, coreMat: null, glow: null, inner: null,
    shell: null, shellMat: null, halo: null, flash: null,
    beam: null, anchor: null, spin: rand(0.25, 0.5) };
  var PAL = paletteFor(w); // worker task color or GOLD
  w.base = PAL;

  /* hot core + fresnel atmosphere shell + glow */
  node.coreMat = new THREE.MeshBasicMaterial({ color: new THREE.Color(PAL.core) });
  node.core = new THREE.Mesh(new THREE.SphereGeometry(R * 0.30, 32, 24), node.coreMat);
  group.add(node.core);
  /* molten inner flicker: smaller, brighter, offset-pulsing kernel */
  node.kernelMat = new THREE.MeshBasicMaterial({ color: new THREE.Color("#ffffff") });
  node.kernel = new THREE.Mesh(new THREE.SphereGeometry(R * 0.16, 20, 14), node.kernelMat);
  group.add(node.kernel);
  /* fresnel-style atmosphere: backside shell = volumetric rim from any angle */
  node.atmoMat = new THREE.MeshBasicMaterial({ color: new THREE.Color(PAL.mid),
    transparent: true, opacity: 0.35, side: THREE.BackSide,
    blending: THREE.AdditiveBlending, depthWrite: false });
  node.atmo = new THREE.Mesh(new THREE.SphereGeometry(R * 0.52, 32, 24), node.atmoMat);
  group.add(node.atmo);
  node.glow = T3.texSprite(TEX.glow, PAL.mid, R * 2.9, 0.85);
  group.add(node.glow);
  node.inner = T3.texSprite(TEX.glow, "#ffffff", R * 1.25, 0.9);
  group.add(node.inner);

  /* faceted light-lattice shells: hologram beams, never solid */
  node.shellMat = T3.holoWire(PAL.mid, 0.20);
  node.shell = new THREE.Mesh(new THREE.IcosahedronGeometry(R, 2), node.shellMat);
  group.add(node.shell);
  var outer = new THREE.Mesh(
    new THREE.IcosahedronGeometry(R * 1.14, 1), T3.holoWire(PAL.dim, 0.10));
  outer.rotation.set(0.4, 0.2, 0);
  group.add(outer);
  node.outer = outer;

  /* 3 gyro rings, distinct tilts + speeds (the "armillary" look) */
  var tilts = [
    { x: Math.PI / 2.25, y: 0.15, z: 0.0, sp: 0.55 },
    { x: Math.PI / 1.75, y: -0.5, z: 0.35, sp: -0.38 },
    { x: 0.35, y: 0.9, z: -0.5, sp: 0.27 }
  ];
  /* full-circle lines are NEXUS-only: subagents carry severed energy arcs.
     (isLeader is declared below, so test kind directly — the old read saw a
     hoisted undefined and gave everybody exactly one full ring.) */
  var nRings = (w.kind === "leader") ? 3 : 0;
  for (var i = 0; i < nRings; i++) {
    var rg = new THREE.Mesh(
      new THREE.TorusGeometry(R * 1.14, Math.max(0.006, R * 0.011), 12, 160),
      T3.holoMat(i === 0 ? PAL.hot : PAL.mid, i === 0 ? 0.55 : 0.38));
    rg.rotation.set(tilts[i].x, tilts[i].y, tilts[i].z);
    rg.userData.sp = tilts[i].sp;
    rg.userData.baseOp = i === 0 ? 0.55 : 0.38;
    rg.userData.baseHex = i === 0 ? PAL.hot : PAL.mid;
    group.add(rg);
    node.rings.push(rg);
  }
  /* broken-orbit fragments: small severed arcs whipping around the sphere.
     They accelerate hard while work runs (see drive in animEntity). */
  var isLeader = (w.kind === "leader");
  /* severed-orbit fragments: deliberately varied short lengths (0.4..1.35 rad).
     Radius breathes below (see rings loop) so each strip visibly grows/shrinks. */
  var arcDefs = isLeader ? [
    { r: R * 1.22, tube: R * 0.013, arc: 1.15, tilt: [1.15, 0.2, 0.3],  sp: 0.85 },
    { r: R * 1.28, tube: R * 0.010, arc: 0.62, tilt: [1.35, -0.45, 0.9], sp: -0.75 },
    { r: R * 1.34, tube: R * 0.012, arc: 0.48, tilt: [0.5, 0.7, -0.4],  sp: 0.62 },
    { r: R * 1.40, tube: R * 0.008, arc: 1.35, tilt: [1.0, 1.2, 0.5],   sp: -1.05 },
    { r: R * 1.45, tube: R * 0.009, arc: 0.72, tilt: [0.35, -0.8, 1.1], sp: 0.52 },
    { r: R * 1.25, tube: R * 0.007, arc: 0.40, tilt: [1.45, 0.9, -1.0], sp: 1.3 }
  ] : [
    { r: R * 1.24, tube: R * 0.013, arc: 0.9, tilt: [1.2, 0.3, 0.0], sp: 1.0 },
    { r: R * 1.38, tube: R * 0.009, arc: 0.6, tilt: [0.6, -0.5, 0.4], sp: -0.8 }
  ];
  for (var ai = 0; ai < arcDefs.length; ai++) {
    var ad = arcDefs[ai];
    var arc = new THREE.Mesh(
      new THREE.TorusGeometry(ad.r, ad.tube, 10, 170, ad.arc),
      T3.holoMat(ai === 0 ? PAL.hot : PAL.mid, ai === 0 ? 0.6 : 0.45));
    arc.rotation.set(ad.tilt[0], ad.tilt[1], ad.tilt[2]);
    arc.userData.sp = ad.sp;
    arc.userData.baseOp = ai === 0 ? 0.6 : 0.45;
    arc.userData.baseHex = ai === 0 ? PAL.hot : PAL.mid;
    group.add(arc);
    node.rings.push(arc);
    /* free tip light: its OWN private orbit, never slaved to the arc.
       It drifts near the fragment shell on an independent schedule. */
    var tip = T3.texSprite(TEX.glow, PAL.hot, R * 0.26, 0.95);
    group.add(tip);
    (node.freeTips = node.freeTips || []).push({
      el: tip,
      r: ad.r * rand(0.94, 1.06),
      a: rand(0, Math.PI * 2),
      w: (0.45 + Math.random() * 0.65) * (ai % 2 ? -1 : 1),
      tiltX: rand(0.6, 1.4), tiltZ: rand(0, 3),
      yAmp: ad.r * 0.3
    });
  }
  // DISABLED on request (orbiting rider lights) — restore by uncommenting.
  // node.riders = [];
  // var nRiders = isLeader ? 4 : 0; // workers stay clean
  // for (var ri = 0; ri < nRiders; ri++) {
  //   var rsp = T3.texSprite(TEX.glow, GOLD.hot, R * 0.34, 0.95);
  //   group.add(rsp);
  //   node.riders.push({ sp: rsp, a: (ri / nRiders) * Math.PI * 2 + rand(0, 1),
  //     r: R * (1.24 + (ri % 3) * 0.08), speed: (0.9 + ri * 0.22) * (ri % 2 ? -1 : 1),
  //     tiltX: 1.1 + ri * 0.18, tiltZ: ri * 0.5 });
  // }
  node.riders = [];
  /* invisible hit sphere for hover raycast */
  node.hit = new THREE.Mesh(
    new THREE.SphereGeometry(R * 1.65, 8, 6),
    new THREE.MeshBasicMaterial({ visible: false }));
  node.hit.userData.eid = w.id;
  group.add(node.hit);

  /* plasma filaments: glowing energy veins inside the sphere */
  node.filaments = [];
  var nFil = isLeader ? 2 : 1; // spaghetti trimmed: leader keeps 2 veins
  for (var fi = 0; fi < nFil; fi++) {
    var curve = T3.filamentCurve(R, fi * 2.39 + 0.7);
    var tube = new THREE.Mesh(
      new THREE.TubeGeometry(curve, 130, Math.max(0.004, R * 0.0075), 5, false),
      new THREE.MeshBasicMaterial({ color: new THREE.Color(fi % 2 ? PAL.copper : PAL.hot),
        transparent: true, opacity: 0.5,
        blending: THREE.AdditiveBlending, depthWrite: false }));
    group.add(tube);
    node.filaments.push({ mesh: tube, sp: (0.25 + fi * 0.12) * (fi % 2 ? -1 : 1), ph: fi * 1.3 });
  }

  /* measurement dial: 72 gold ticks on a tilted pivot (arcane instrument) */
  (function dial() {
    var nTicks = isLeader ? 72 : 24;
    var tickGeo = new THREE.BoxGeometry(R * 0.016, R * 0.105, R * 0.016);
    var tickMat = new THREE.MeshBasicMaterial({ color: new THREE.Color("#ffffff") });
    var dialMesh = new THREE.InstancedMesh(tickGeo, tickMat, nTicks);
    var dummy = new THREE.Object3D();
    var cGold = new THREE.Color(PAL.mid), cHot = new THREE.Color(PAL.hot);
    var cDim = new THREE.Color(PAL.dim);
    for (var di = 0; di < nTicks; di++) {
      var da = (di / nTicks) * Math.PI * 2;
      dummy.position.set(Math.cos(da) * R * 1.16, 0, Math.sin(da) * R * 1.16);
      dummy.rotation.set(0, -da, 0);
      dummy.updateMatrix();
      dialMesh.setMatrixAt(di, dummy.matrix);
      dialMesh.setColorAt(di, di % 6 === 0 ? cHot : (di % 2 ? cGold : cDim));
    }
    if (dialMesh.instanceMatrix) dialMesh.instanceMatrix.needsUpdate = true;
    if (dialMesh.instanceColor) dialMesh.instanceColor.needsUpdate = true;
    var pivot = new THREE.Group();
    pivot.rotation.set(1.25, 0.15, -0.2);
    pivot.add(dialMesh);
    group.add(pivot);
    node.dial = dialMesh;
    node.dialPivot = pivot;
  })();

  /* gold dust: ultra-fine counter-rotating layer for close-up richness */
  (function dust() {
    if (!isLeader) return; // workers stay clean
    var N = 1500, pos = new Float32Array(N * 3), col = new Float32Array(N * 3);
    var cA = hexToRgb(PAL.dim), cB = hexToRgb(PAL.deep), cC = hexToRgb(PAL.copper);
    for (var k = 0; k < N; k++) {
      var th = rand(0, Math.PI * 2), ph = Math.acos(rand(-1, 1));
      var rr = R * rand(0.5, 1.12);
      pos[k*3] = rr * Math.sin(ph) * Math.cos(th);
      pos[k*3+1] = rr * Math.cos(ph);
      pos[k*3+2] = rr * Math.sin(ph) * Math.sin(th);
      var f = Math.random(), c = f < 0.45 ? cA : (f < 0.8 ? cB : cC);
      col[k*3] = c[0] / 255; col[k*3+1] = c[1] / 255; col[k*3+2] = c[2] / 255;
    }
    var g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    g.setAttribute("color", new THREE.BufferAttribute(col, 3));
    var m = new THREE.PointsMaterial({ size: R * 0.042, map: new THREE.CanvasTexture(TEX.dot),
      vertexColors: true, transparent: true, opacity: 0.85,
      depthWrite: false, blending: THREE.AdditiveBlending, sizeAttenuation: true });
    node.dust = new THREE.Points(g, m);
    node.dustMat = m;
    group.add(node.dust);
  })();

  /* ember shell: points scattered between 0.55R and 1.06R, gold gradient */
  (function embers() {
    var pos = new Float32Array(emberN * 3);
    var col = new Float32Array(emberN * 3);
    var cHot = hexToRgb(PAL.hot), cMid = hexToRgb(PAL.mid),
        cDeep = hexToRgb(PAL.deep), cCop = hexToRgb(PAL.copper);
    for (var k = 0; k < emberN; k++) {
      var th = rand(0, Math.PI * 2), ph = Math.acos(rand(-1, 1));
      var rr = R * rand(0.55, 1.06);
      pos[k*3] = rr * Math.sin(ph) * Math.cos(th);
      pos[k*3+1] = rr * Math.cos(ph);
      pos[k*3+2] = rr * Math.sin(ph) * Math.sin(th);
      var f = Math.random();
      var c = f < 0.22 ? cHot : (f < 0.55 ? cMid : (f < 0.8 ? cDeep : cCop));
      col[k*3] = c[0] / 255; col[k*3+1] = c[1] / 255; col[k*3+2] = c[2] / 255;
    }
    var g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    g.setAttribute("color", new THREE.BufferAttribute(col, 3));
    var tex = new THREE.CanvasTexture(TEX.dot);
    var m = new THREE.PointsMaterial({ size: R * 0.068, map: tex,
      vertexColors: true, transparent: true, opacity: 0.95,
      depthWrite: false, blending: THREE.AdditiveBlending, sizeAttenuation: true });
    node.emberPts = new THREE.Points(g, m);
    node.emberMat = m;
    group.add(node.emberPts);
  })();

  /* rising motes: sparks drifting upward through the sphere */
  (function motes() {
    var N = moteN, pos = new Float32Array(N * 3), dat = [];
    for (var k = 0; k < N; k++) {
      var a = rand(0, Math.PI * 2), rr = R * Math.sqrt(Math.random()) * 0.9;
      dat.push({ a: a, r: rr, y: rand(-R, R), sp: rand(0.25, 0.8) });
    }
    var g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    var tex = new THREE.CanvasTexture(TEX.dot);
    var m = new THREE.PointsMaterial({ size: R * 0.07, map: tex,
      color: new THREE.Color(PAL.hot), transparent: true, opacity: 0.9,
      depthWrite: false, blending: THREE.AdditiveBlending });
    node.motePts = new THREE.Points(g, m);
    node.moteMat = m;
    node.moteDat = dat;
    group.add(node.motePts);
  })();

  /* halo (tool tint while running) + hit flash */
  node.halo = T3.texSprite(TEX.glow, "#ffffff", R * 3.4, 0);
  group.add(node.halo);
  node.flash = T3.texSprite(TEX.glow, "#ffffff", R * 3.0, 0);
  group.add(node.flash);
  /* permanent power aura: vast faint presence breathing with work */
  node.aura = T3.texSprite(TEX.glow, PAL.mid, R * 4.4, 0.14);
  group.add(node.aura);

  /* reusable vertical beam (web.fetch / exec.code / parallel / delegate) */
  var beamMat = new THREE.MeshBasicMaterial({ color: new THREE.Color("#ffffff"),
    transparent: true, opacity: 0, blending: THREE.AdditiveBlending,
    depthWrite: false, side: THREE.DoubleSide });
  node.beam = new THREE.Mesh(new THREE.CylinderGeometry(R * 0.07, R * 0.13, R * 3.6, 12, 1, true), beamMat);
  node.beam.position.y = R * 0.9;
  node.beam.visible = false;
  group.add(node.beam);

  node.anchor = new THREE.Object3D();
  node.anchor.position.set(0, -R * 1.9, 0); // labels hang below the body
  group.add(node.anchor);

  /* hologram scan sweep: a bright slice travelling through the sphere */
  node.scan = new THREE.Mesh(
    new THREE.TorusGeometry(R * 1.0, Math.max(0.004, R * 0.006), 8, 120),
    T3.holoMat(PAL.hot, 0));
  node.scan.rotation.x = Math.PI / 2;
  node.scan.visible = false;
  group.add(node.scan);
  w._scanT = rand(0, 7);

  w.node = node;
  return node;
};

T3.spawnWorkerNode = function (w) {
  if (!T3.scene || w.node) return;
  T3.buildEntityNode(w, DEPTH_R[Math.min(depthOf(w), 3)], 180, 16);
  w.bornAt = nowMs(); // node age drives pop-in, not record age
  var lp = (S.leader && S.leader.node) ? S.leader.node.group.position : { x: 0, y: 0, z: 0 };
  w.node.group.position.set(lp.x + rand(-1, 1), lp.y - 2.2, lp.z + rand(-0.5, 0.5));
  w.node.group.scale.set(0.01, 0.01, 0.01); // birth pop-in
  T3.scene.add(w.node.group);
  /* tether beam leader -> worker + glowing endpoints */
  var THREE = T3.THREE;
  var g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(6), 3));
  var line = new THREE.Line(g, new THREE.LineBasicMaterial({
    color: new THREE.Color(GOLD.mid), transparent: true, opacity: 0.45,
    blending: THREE.AdditiveBlending, depthWrite: false }));
  T3.scene.add(line);
  T3.tetherLines[w.id] = line;
  var tipA = T3.texSprite(TEX.glow, GOLD.hot, 0.5, 0.9);
  var tipB = T3.texSprite(TEX.glow, GOLD.mid, 0.42, 0.9);
  T3.scene.add(tipA); T3.scene.add(tipB);
  T3.tetherTips[w.id] = [tipA, tipB];
};

T3.orbitLines = {};
/* private path line: one hidden ellipse per worker, revealed only while its
   owner is hovered (tooltip target). Unit circle in XZ; the owner block
   scales/positions it every frame. Tint follows the worker. */
T3.ensureOrbitLine = function (w) {
  if (!T3.scene || !T3.THREE || !w) return null;
  var id = w.id, ln = T3.orbitLines[id];
  if (!ln) {
    var THREE = T3.THREE, pts = [];
    for (var s = 0; s <= 96; s++) {
      var a = (s / 96) * Math.PI * 2;
      pts.push(new THREE.Vector3(Math.cos(a), 0, Math.sin(a)));
    }
    var tint = "#D4AF37";
    try { if (/^#[0-9a-fA-F]{6}$/.test(w.tint || "")) tint = w.tint; } catch (e) {}
    ln = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({ color: new THREE.Color(tint),
        transparent: true, opacity: 0.30,
        blending: THREE.AdditiveBlending, depthWrite: false }));
    ln.visible = false;
    T3.scene.add(ln);
    T3.orbitLines[id] = ln;
  }
  ln.visible = !!((S.hover && S.hover.id === id) || (S.focus && S.focus === id));
  return ln;
};

T3.spawnPeerNode = function (w) {
  if (!T3.scene || w.node) return;
  T3.buildEntityNode(w, 0.95, 340, 34);
  var sp = w._slotPos || { a: 0.5, r: 6.2, y: 1.6 };
  w.node.group.position.set(Math.cos(sp.a) * sp.r, sp.y, Math.sin(sp.a) * sp.r * 0.8 - 0.5);
  w.node.group.scale.set(0.01, 0.01, 0.01);
  T3.scene.add(w.node.group);
  w.bornAt = nowMs();
};

T3.clearWorkerNodes = function () {
  Object.keys(S.workers).forEach(function (id) {
    var w = S.workers[id];
    if (w && w.node) { T3.scene.remove(w.node.group); w.node = null; }
    if (T3.orbitLines && T3.orbitLines[id]) {
      T3.scene.remove(T3.orbitLines[id]);
      delete T3.orbitLines[id];
    }
  });
  Object.keys(T3.tetherLines).forEach(function (id) {
    T3.scene.remove(T3.tetherLines[id]);
    delete T3.tetherLines[id];
  });
  Object.keys(T3.tetherTips).forEach(function (id) {
    T3.tetherTips[id].forEach(function (sp) { T3.scene.remove(sp); });
    delete T3.tetherTips[id];
  });
};

/* compat: formation lives in layoutOrbits() now */
T3.layout = function () { layoutOrbits(); };
/* ---- transient FX spawners (three) ---- */
T3.entityPos = function (id) {
  var w = getEntity(id);
  if (w && w.node) return w.node.group.position;
  return null;
};
T3.spawnShockwave = function (at, color, size) {
  var p = T3.entityPos(at);
  if (!p) return;
  var sp = T3.texSprite(TEX.ring, color, 0.6, 0.95);
  sp.position.copy(p);
  T3.scene.add(sp);
  T3.fx.push({ kind: "ring", obj: sp, t: 0, ttl: 1.1, size: (size || 1) });
};
T3.spawnBurst = function (at, color, n) {
  var THREE = T3.THREE;
  var p = T3.entityPos(at);
  var w = getEntity(at);
  var R = (w && w.node) ? w.node.R : 1;
  if (!p) return;
  n = Math.min(130, n || 30);
  var pos = new Float32Array(n * 3), vel = [];
  for (var i = 0; i < n; i++) {
    pos[i*3] = p.x; pos[i*3+1] = p.y; pos[i*3+2] = p.z;
    var th = rand(0, Math.PI * 2), ph = Math.acos(rand(-1, 1)), sp = rand(0.8, 3.2) * R;
    vel.push([Math.sin(ph) * Math.cos(th) * sp, Math.abs(Math.cos(ph)) * sp * 0.9 + 0.4, Math.sin(ph) * Math.sin(th) * sp]);
  }
  var g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  var m = new THREE.PointsMaterial({ size: 0.09, map: new THREE.CanvasTexture(TEX.dot),
    color: new THREE.Color(color), transparent: true, opacity: 1,
    depthWrite: false, blending: THREE.AdditiveBlending });
  var pts = new THREE.Points(g, m);
  T3.scene.add(pts);
  T3.fx.push({ kind: "burst", obj: pts, vel: vel, t: 0, ttl: 1.4 });
};
T3.spawnBeam = function (at, color, ttl) {
  return; // beam pillar removed: nothing pierces the center anymore
};
T3.spawnGlyphFloat = function (at, txt, color) {
  var p = T3.entityPos(at);
  if (!p) return;
  var sp = T3.texSprite(glyphTex(txt, color), color, 0.55, 1);
  sp.position.set(p.x + rand(-0.2, 0.2), p.y + 0.6, p.z);
  T3.scene.add(sp);
  T3.fx.push({ kind: "glyph", obj: sp, t: 0, ttl: 1.5, vy: 1.1 });
};
T3.spawnFlash = function (at, color, size) {
  var p = T3.entityPos(at);
  if (!p) return;
  var w = getEntity(at);
  var R = (w && w.node) ? w.node.R : 1;
  var sp = T3.texSprite(TEX.glow, color, R * 2, 0.95);
  sp.position.copy(p);
  T3.scene.add(sp);
  T3.fx.push({ kind: "flash", obj: sp, t: 0, ttl: 0.55, R: R, size: size || 1 });
};
/* drain engine-agnostic queues (rings/bursts/floats/flashes/beams) */
T3.drainQueues = function () {
  var i, q;
  for (i = S.rings.length - 1; i >= 0; i--) {
    q = S.rings[i]; S.rings.splice(i, 1);
    T3.spawnShockwave(q.at, q.color, q.size);
  }
  for (i = S.bursts.length - 1; i >= 0; i--) {
    q = S.bursts[i]; S.bursts.splice(i, 1);
    T3.spawnBurst(q.at, q.color, q.n);
  }
  for (i = S.floats.length - 1; i >= 0; i--) {
    q = S.floats[i]; S.floats.splice(i, 1);
    T3.spawnGlyphFloat(q.at, q.txt, q.color);
  }
  for (i = S.flashes.length - 1; i >= 0; i--) {
    q = S.flashes[i]; S.flashes.splice(i, 1);
    T3.spawnFlash(q.at, q.color, q.size);
  }
  for (i = S.beams.length - 1; i >= 0; i--) {
    q = S.beams[i]; S.beams.splice(i, 1);
    T3.spawnBeam(q.at, q.color, q.ttl);
  }
};

/* ---- per-frame entity animation ---- */
T3.animEntity = function (w, dt, t, boost) {
  var n = w.node;
  if (!n) return;
  var R = n.R;
  /* decay one-shot channels */
  w.flash = Math.max(0, w.flash - dt * 2.2);
  w.flare = Math.max(0, (w.flare || 0) - dt * 2.6);
  w.think = Math.max(0, w.think - dt * 0.8);
  w.spinKick = Math.max(0, w.spinKick - dt * 0.9);
  var excite = clamp(S.energy, 0, 1.2);
  /* drive: PER-ENTITY — this body accelerates on its OWN real work only
     (w.act kicked by its own tool/think/radio/plan events). The leader stays
     pinned fast while anything runs (see smoothChannels/anyRealWork). */
  var wAct = (w.act || 0);
  /* dormant: a finished body holds its place and fades — it must NOT wander
     on its own or it visually merges into its neighbours (annoying). */
  var dormant = !!w.deadAt;
  var drive = dormant ? 0.12 : (1 + wAct * 3.2);
  /* this body first, room tone second — no more unison throbbing */
  var xcite = clamp((w.exc || 0) * 0.9 + excite * 0.2, 0, 1.3);
  /* private clock: own phase AND own rate (faster while THIS body works) */
  if (!dormant) w.ck = (w.ck == null ? Math.random() * 100 : w.ck) + dt * (1 + wAct * 1.2);
  else if (w.ck == null) w.ck = Math.random() * 100;
  var ck = w.ck;
  var flick = (S.flick == null ? 1 : S.flick);
  var wt = S.wanderT; // old ambient motions read this clock, so they speed up with work
  /* thinking owns a mode while it burns (hysteresis: no flicker at the edge) */
  if (w.think > 0.4 && !w._thinkThemed) { w._thinkThemed = true; pushTheme(w, "thinking"); }
  else if (w._thinkThemed && w.think < 0.12) { w._thinkThemed = false; popTheme(w, "thinking"); }
  var thm0 = themeOf(w);
  var spinMul = thm0 ? (thm0.spinMul || 1) : 1;
  var ringMul = thm0 ? (thm0.ringMul || 1) : 1;
  var bobMul = thm0 ? (thm0.bobMul || 1) : 1;
  /* retired agents: slow drift (pace) + dimmed light (dimF), still alive */
  var pace = w.retired ? 0.25 : 1;
  var dimF = (w.retired ? 0.5 : 1) * (w.focusDim == null ? 1 : w.focusDim);
  drive *= pace;
  var spin = (0.35 + w.spinKick * 1.6 + xcite * 0.9 + (w.flare || 0) * 0.8) * boost * drive * spinMul;

  /* ---- lifespan scale: birth pop-in / 6s retire fade ---- */
  var sAlive = 1;
  if (w.deadAt && !w.retired) {
    sAlive = clamp(1 - (nowMs() - w.deadAt) / 6000, 0, 1);
    if (sAlive <= 0) { T3.removeWorkerNode(w.id); try { layoutOrbits(); } catch (e) {} return; }
  } else if (n.group.scale.x < 0.995 && (nowMs() - w.bornAt) < 4000) {
    sAlive = clamp(n.group.scale.x + dt * 2.2, 0.01, 1);
  }
  /* breathing (the hologram is alive) */
  var isNEXUS = (w.kind !== "worker");
  var breath = (dormant || isNEXUS) ? 1 : (1 + Math.sin(ck * 1.7 + w.phase) * 0.018 * bobMul + xcite * 0.012);
  n.group.scale.set(sAlive * breath, sAlive * breath, sAlive * breath);

  /* ---- free motion ---- */
  var gp = n.group.position;
  if (w.kind === "worker") {
    /* computed-home homing (no rails): ease to the stable hashed point.
       Revolution is over on purpose — spin/fan keeps the rhythm. */
    var _boss = (w.sid && S.leaders[w.sid] && S.leaders[w.sid].node) ? S.leaders[w.sid] : S.leader;
    var lp = (_boss && _boss.node) ? _boss.node.group.position : { x: 0, y: 0.15, z: 0 };
    var wob = Math.sin(ck * 0.5 + w.phase) * 0.10 * bobMul;
    var _satP = isSatellite(w) ? S.workers[w.parent] : null;
    var _satC = _satP ? _satP.node.group.position : null;
    var tx, ty, tz, _lr;
    var _hp = (w._home3 && w.parent && S.workers[w.parent] && S.workers[w.parent].node && !S.workers[w.parent].deadAt)
      ? S.workers[w.parent].node.group.position : null;
    if (_hp && w._home3) {
      /* nested child: its computed shell point around the parent body */
      tx = _hp.x + w._home3.x + Math.sin(ck * 0.43 + w.phase) * 0.05;
      ty = _hp.y + w._home3.y + Math.sin(ck * 0.9 + w.phase) * 0.06 * bobMul;
      tz = _hp.z + w._home3.z + wob * 0.3;
      _lr = Math.hypot(w._home3.x, w._home3.z) || 1.7;
    } else {
      /* top-level / parent / orphan: its computed disc point around the boss */
      var _h = w._home || { a: 0, r: 5, y: 0 };
      _lr = _h.r;
      tx = lp.x + Math.cos(_h.a) * _h.r + Math.sin(ck * 0.43 + w.phase) * 0.08;
      ty = lp.y + _h.y + Math.sin(ck * 0.9 + w.phase) * 0.08 * bobMul;
      tz = lp.z + Math.sin(_h.a) * _h.r - 0.5 + wob * 0.4;
    }
    var k = dormant ? 0 : (1 - Math.pow(0.001, dt));
    gp.x = lerp(gp.x, tx, k);
    gp.y = lerp(gp.y, ty, k);
    gp.z = lerp(gp.z, tz, k);
    /* path lines: family + satellite rings stay faintly visible (lineage must
       read at a glance); round rings appear on hover/focus as before */
    if (T3.scene && S.engine === "three") {
      var _ol = T3.ensureOrbitLine(w);
      if (_ol) {
        var _hot = (S.hover && S.hover.id === w.id) || (S.focus && S.focus === w.id);
        if (_hp && w._home3) {
          _ol.scale.set(_lr, 1, _lr);
          _ol.position.set(_hp.x, _hp.y, _hp.z);
          _ol.material.opacity = _hot ? 0.5 : 0.16;
          _ol.visible = true;
        } else {
          var _hh = w._home || { r: 5, y: 0 };
          _ol.scale.set(_lr, 1, _lr);
          _ol.position.set(lp.x, lp.y + _hh.y, lp.z - 0.5);
          if (w._tierList === "F") {
            _ol.material.opacity = _hot ? 0.5 : 0.14;
            _ol.visible = true;
          }
        }
      }
    }
  } else if (w.kind === "peer" && w._slotPos) {
    /* peer NEXUS holds its shelf slot with a gentle bob (never piles center) */
    var _sp = w._slotPos;
    var _pk = 1 - Math.pow(0.001, dt);
    gp.x = lerp(gp.x, Math.cos(_sp.a) * _sp.r + Math.sin(wt * 0.4 + w.phase) * 0.1, _pk);
    gp.y = lerp(gp.y, _sp.y + Math.sin(wt * 0.7 + w.phase) * 0.12 * bobMul, _pk);
    gp.z = lerp(gp.z, Math.sin(_sp.a) * _sp.r * 0.8 - 0.5, _pk);
  } else {
    /* leader wanders the center stage (lissajous drift, always near middle) */
    gp.x = Math.sin(wt * 0.21) * 0.42 + Math.sin(wt * 0.083 + 1.7) * 0.18;
    gp.y = 0.15 + Math.sin(wt * 0.33 + w.phase) * 0.22 * bobMul + Math.sin(wt * 0.9) * 0.03 * bobMul;
    gp.z = Math.cos(wt * 0.17) * 0.32;
  }

  /* ---- core: breathing + heat + error tint + flare ---- */
  var pulse = 1 + Math.sin(ck * (2.2 + xcite * 2.5 + w.think * 4 + wAct * 1.2)) * (0.04 + xcite * 0.04 + w.think * 0.05)
    + (w.flare || 0) * 0.30;
  n.core.scale.set(pulse, pulse, pulse);
  var errMix = (S.mode === "ERROR") ? 0.75 : 0;
  n.coreMat.color.set((w.base || GOLD).hot);
  if (errMix) n.coreMat.color.lerp(new T3.THREE.Color("#ff3b3b"), errMix * (0.6 + 0.4 * Math.sin(t * 14)));
  n.glow.material.opacity = clamp(0.65 + xcite * 0.3 + w.think * 0.25 + w.flash * 0.3 + (w.flare || 0) * 0.4, 0, 1) * dimF;
  var gs = R * (2.9 + Math.sin(ck * 3) * 0.12 + xcite * 0.35 + (w.flare || 0) * 0.6);
  n.glow.scale.set(gs, gs, 1);
  n.inner.material.opacity = 0.75 + w.think * 0.25;
  if (n.kernelMat) n.kernelMat.color.set((w.base || GOLD).core);
  if (n.inner) n.inner.material.color.set((w.base || GOLD).hot);
  /* molten kernel flicker + breathing atmosphere */
  if (n.kernel) {
    var kp = 1 + Math.sin(ck * 7.3 + w.phase) * 0.12 + Math.sin(ck * 13.7) * 0.06 + (w.flare || 0) * 0.25;
    n.kernel.scale.set(kp, kp, kp);
  }
  if (n.atmo) {
    n.atmoMat.opacity = clamp(0.30 + xcite * 0.18 + (w.flare || 0) * 0.3 + w.think * 0.12, 0, 0.9) * dimF;
    n.atmo.rotation.y -= dt * 0.3 * drive;
  }

  /* ember heat follows excitement (dynamic focus: calm bronze -> white hot) */
  if (n.emberMat) {
    if (w.base && w.base !== GOLD) n.emberMat.color.set("#ffffff"); // task color: pure vertex hues
    else n.emberMat.color.copy(T3.calmCol).lerp(T3.hotCol, clamp(0.25 + xcite * 0.75, 0, 1));
  }

  /* gyro rings + severed fragments: accelerate with drive, radius breathes
     (deeper + faster while work runs) so strips grow/shrink live */
  for (var i = 0; i < n.rings.length; i++) {
    var rg = n.rings[i];
    rg.rotation.z += dt * (rg.userData.sp || 0.3) * (1 + xcite * 1.6 + w.spinKick) * drive * ringMul;
    rg.rotation.x += dt * 0.05 * (i % 2 ? 1 : -1) * drive * ringMul;
    var br = 1 + Math.sin(ck * (0.7 + (i % 3) * 0.35) + w.phase + i * 1.7) *
      (0.035 + wAct * 0.035);
    rg.scale.set(br, br, br);
    if (rg.userData.baseOp != null) {
      rg.material.opacity = rg.userData.baseOp * (0.72 + 0.28 * flick) * dimF;
    }
  }
  if (n.shellMat) n.shellMat.opacity = 0.15 * flick;
  if (n.emberMat) {
      n.emberMat.opacity = 0.95 * flick * dimF;
      n.emberMat.size = R * 0.068 * (1 + wAct * 0.45);
    }
  // DISABLED on request (orbiting rider lights) — restore by uncommenting.
  // if (n.riders) {
  //   for (var ri = 0; ri < n.riders.length; ri++) {
  //     var rd = n.riders[ri];
  //     rd.a += dt * rd.speed * (1 + excite * 0.8) * drive;
  //     var rx = Math.cos(rd.a) * rd.r, rz = Math.sin(rd.a) * rd.r;
  //     rd.sp.position.set(rx, Math.sin(rd.a * 1.4 + rd.tiltZ) * rd.r * 0.32, rz * 0.8);
  //     var rs = R * (0.30 + 0.08 * Math.sin(wt * 5 + ri * 2.1));
  //     rd.sp.scale.set(rs, rs, 1);
  //   }
  // }
  n.shell.rotation.y += dt * spin * 0.4;
  n.shell.rotation.x = Math.sin(ck * 0.3 + w.phase) * 0.25;
  if (n.outer) n.outer.rotation.y -= dt * spin * 0.25;
  if (n.emberPts) {
    n.emberPts.rotation.y += dt * spin * (w.kind === "leader" ? 0.55 : 0.8);
    n.emberPts.rotation.z = Math.sin(ck * 0.4 + w.phase) * 0.12;
  }
  /* gold dust counter-rotates against the embers (parallax depth) */
  if (n.dust) {
    n.dust.rotation.y -= dt * spin * 0.35;
    n.dust.rotation.x = Math.cos(ck * 0.33 + w.phase) * 0.1;
  }
  /* plasma veins writhe slowly */
  if (n.filaments) {
    for (var fi = 0; fi < n.filaments.length; fi++) {
      var fm = n.filaments[fi];
      fm.mesh.rotation.y += dt * fm.sp * (1 + xcite) * drive;
      fm.mesh.rotation.x = Math.sin(wt * 0.4 + fm.ph) * 0.18;
    }
  }
  /* measurement dial ticks forward (accelerates with work) */
  if (n.dial) {
    n.dial.rotation.y += dt * (0.22 + xcite * 0.5 + w.spinKick * 0.8) * drive * ringMul;
    n.dial.material.opacity = 0.85 * flick * dimF;
  }
  /* free tip lights drift on fully private orbits — decoupled from arcs */
  if (n.freeTips) {
    for (var fti = 0; fti < n.freeTips.length; fti++) {
      var ft = n.freeTips[fti];
      ft.a += dt * ft.w * (0.6 + xcite * 0.5 + wAct * 1.5) * (w.retired ? 0.4 : 1);
      var fx = Math.cos(ft.a) * ft.r;
      var fz = Math.sin(ft.a) * ft.r * 0.8;
      var fy = Math.sin(ft.a * 1.3 + ft.tiltZ) * ft.yAmp + Math.cos(ft.a * 0.7) * ft.r * 0.12;
      var cy = Math.cos(ft.tiltX), sy2 = Math.sin(ft.tiltX);
      ft.el.position.set(fx, fy * cy - fz * sy2, fy * sy2 + fz * cy);
      var fs3 = R * (0.24 + 0.06 * Math.sin(ck * 7 + fti * 2.1));
      ft.el.scale.set(fs3, fs3, 1);
    }
  }

  /* rising motes */
  if (n.motePts && n.moteDat) {
    var pa = n.motePts.geometry.attributes.position.array;
    for (var m = 0; m < n.moteDat.length; m++) {
      var d = n.moteDat[m];
      d.y += dt * d.sp * (1 + xcite + wAct * 1.6) * pace;
      d.a += dt * 0.5 * drive;
      if (d.y > R * 1.1) { d.y = -R * 1.1; d.a = rand(0, Math.PI * 2); d.r = R * Math.sqrt(Math.random()) * 0.9; }
      pa[m*3] = Math.cos(d.a) * d.r;
      pa[m*3+1] = d.y;
      pa[m*3+2] = Math.sin(d.a) * d.r;
    }
    n.motePts.geometry.attributes.position.needsUpdate = true;
  }
  /* whole-entity theme glide (colors chase the active mode) */
  applyThemeColors(w, n, dt);

  /* permanent power aura breathes with work */
  if (n.aura) {
    n.aura.material.opacity = (0.13 + wAct * 0.14 + (w.flare || 0) * 0.25 + xcite * 0.06) * dimF;
    var as = R * (4.3 + Math.sin(wt * 1.3 + w.phase) * 0.25 + wAct * 0.7);
    n.aura.scale.set(as, as, 1);
  }
  /* halo tint while a halo-tool runs */
  if (w.halo) {
    n.halo.material.color.set(w.halo.color);
    n.halo.material.opacity = 0.28 + 0.12 * Math.sin(ck * 5);
  } else {
    n.halo.material.opacity = Math.max(0, n.halo.material.opacity - dt * 2);
  }
  /* hit flash */
  if (w.flash > 0) {
    n.flash.material.color.set(w.flashColor || "#ffffff");
    n.flash.material.opacity = w.flash * 0.85;
    var fs = R * (2.2 + (1 - w.flash) * 2.2);
    n.flash.scale.set(fs, fs, 1);
  } else {
    n.flash.material.opacity = 0;
  }
  /* hologram scan sweep: bright slice travelling pole to pole */
  if (n.scan) {
    w._scanT = (w._scanT || 0) + (dormant ? 0 : dt * (1 + wAct * 0.8) * pace);
    var scyc = (w._scanT % 7) / 7;
    if (scyc < 0.26) {
      var sy = lerp(-R * 1.15, R * 1.15, scyc / 0.26);
      n.scan.position.y = sy;
      var ssw = Math.max(0.25, 1 - Math.abs(sy) / (R * 1.6));
      n.scan.scale.set(ssw, ssw, 1);
      n.scan.visible = true;
      n.scan.material.opacity = 0.6 * Math.sin((scyc / 0.26) * Math.PI) * flick;
    } else {
      n.scan.visible = false;
      n.scan.material.opacity = 0;
    }
  }
  /* reusable beam TTL */
  if (n.beam.visible) {
    w._beamT = (w._beamT || 0) + dt;
    var bt = w._beamT, bttl = w._beamTtl || 2.2;
    n.beam.material.opacity = 0.5 * Math.max(0, 1 - bt / bttl) * (0.7 + 0.3 * Math.sin(ck * 9));
    n.beam.rotation.y += dt * 2 * (1 + wAct * 2) * pace;
    var stillBeaming = false;
    for (var b = 0; b < S.beams.length; b++) if (S.beams[b].at === w.id) stillBeaming = true;
    if (bt > bttl && !stillBeaming && !w.orbiters.length) n.beam.visible = false;
    else if (bt > bttl) { w._beamT = 0; } // hold while tools still orbit
  }

  /* tool satellites: plain colored dots orbiting the shell (no glyphs) */
  var want = w.orbiters.length;
  while (n.orbiterSprites.length < want) {
    var sp = T3.texSprite(TEX.dot, "#ffffff", R * 0.55, 0.95);
    n.group.add(sp);
    n.orbiterSprites.push(sp);
  }
  while (n.orbiterSprites.length > want) {
    var old = n.orbiterSprites.pop();
    n.group.remove(old);
  }
  for (var oi = 0; oi < want; oi++) {
    var orb = w.orbiters[oi], osp = n.orbiterSprites[oi];
    orb.angle += dt * orb.speed * (1 + excite + wAct * 2);
    var orad = R * 1.32;
    osp.position.set(Math.cos(orb.angle) * orad, Math.sin(orb.angle * 1.3) * orad * 0.7, Math.sin(orb.angle) * orad);
    if (osp.userData.g !== orb.color) {
      osp.material.color.set(orb.color);
      osp.userData.g = orb.color;
    }
    var s = R * (0.5 + 0.12 * Math.sin(ck * 6 + oi * 2));
    osp.scale.set(s, s, 1);
  }
};

/* drop in-flight signal sprites/lines (session switch / reset) */
T3.purgeSignals = function () {
  var i;
  for (i = 0; i < S.pulses.length; i++) {
    if (S.pulses[i]._sp && T3.scene) T3.scene.remove(S.pulses[i]._sp);
  }
  for (i = 0; i < S.arcs.length; i++) {
    if (S.arcs[i]._line && T3.scene) T3.scene.remove(S.arcs[i]._line);
  }
  T3.pulseSprites.length = 0;
};
T3.removeWorkerNode = function (id) {
  var w = S.workers[id];
  if (w && w.node) {
    T3.scene.remove(w.node.group);
    w.node = null;
  }
  if (T3.orbitLines && T3.orbitLines[id]) {
    T3.scene.remove(T3.orbitLines[id]);
    delete T3.orbitLines[id];
  }
  if (T3.tetherLines[id]) {
    T3.scene.remove(T3.tetherLines[id]);
    delete T3.tetherLines[id];
  }
  if (T3.tetherTips[id]) {
    T3.tetherTips[id].forEach(function (sp) { T3.scene.remove(sp); });
    delete T3.tetherTips[id];
  }
  delete S.workers[id];
  var ix = S.order.indexOf(id);
  if (ix >= 0) S.order.splice(ix, 1);
  dropLabel(id);
  S.hud.workers = liveWorkers().length;
  layoutOrbits();
};
/* ---- tethers, pulses, arcs, fx per-frame ---- */
T3.entityAnchor = function (id) {
  var w = getEntity(id);
  if (w && w.node) return w.node.group.position;
  return null;
};
T3.updateTethers = function () {
  var lp = T3.entityAnchor("leader");
  if (!lp) return;
  /* private path lines follow their owners (positioned in the worker block) */
  Object.keys(T3.tetherLines).forEach(function (id) {
    var line = T3.tetherLines[id];
    var wp = T3.entityAnchor(id);
    if (!wp) return;
    var _bw = S.workers[id];
    var _pw = (_bw && _bw.parent) ? S.workers[_bw.parent] : null;
    var _bl = (_pw && _pw.node) ? _pw.node.group.position
      : ((_bw && _bw.sid && S.leaders[_bw.sid] && S.leaders[_bw.sid].node)
      ? S.leaders[_bw.sid].node.group.position : lp);
    var a = line.geometry.attributes.position.array;
    a[0] = _bl.x; a[1] = _bl.y; a[2] = _bl.z;
    a[3] = wp.x; a[4] = wp.y; a[5] = wp.z;
    line.geometry.attributes.position.needsUpdate = true;
    var wrec = S.workers[id];
    var tdim = (wrec && wrec.retired) ? 0.3 : 1;
    line.material.opacity = (0.36 + S.energy * 0.28 + 0.07 * Math.sin(T3.time * 3 + (wrec ? wrec.phase : id.length))) * tdim;
    var tips = T3.tetherTips[id];
    if (tips) {
      tips[0].position.set(_bl.x, _bl.y, _bl.z);
      tips[1].position.set(wp.x, wp.y, wp.z);
      var ts = 0.42 + S.energy * 0.22 + 0.08 * Math.sin(T3.time * 5 + (wrec ? wrec.phase : 0));
      tips[0].scale.set(ts + 0.1, ts + 0.1, 1);
      tips[1].scale.set(ts, ts, 1);
    }
  });
  /* heartbeat: idle links breathe a slow gold pulse so they feel alive */
  if (S.pulses.length < 24) {
    var liveHB = liveWorkers();
    for (var hi = 0; hi < liveHB.length; hi++) {
      var hw = liveHB[hi];
      if (!hw._lastBeat) hw._lastBeat = T3.time + Math.random() * 2.8; // desync first beat
      if (!hw.retired && T3.time - hw._lastBeat > 2.8) {
        hw._lastBeat = T3.time;
        var hb = (hw.parent && S.workers[hw.parent] && !S.workers[hw.parent].deadAt) ? hw.parent : "leader";
        S.pulses.push({ a: hb, b: hw.id, t: 0, sp: 0.022, color: "#D4AF37", arch: 0 });
        trimPulses();
      }
    }
  }
};
T3.updatePulses = function (dt, boost) {
  var i, p, k;
  /* advance + cull */
  for (i = S.pulses.length - 1; i >= 0; i--) {
    p = S.pulses[i];
    p.t += (p.sp || 0.028) * boost * (1 + S.energy * 1.5) * (1 + S.work * 2);
    if (p.t > 1.15) {
      if (p._sp) { T3.scene.remove(p._sp); p._sp = null; }
      S.pulses.splice(i, 1);
    }
  }
  /* draw */
  for (k = 0; k < S.pulses.length; k++) {
    p = S.pulses[k];
    if (p.t < 0) continue;
    var A = T3.entityAnchor(p.a), B = T3.entityAnchor(p.b);
    if (!A || !B) continue;
    var t = clamp(p.t, 0, 1), x, y, z;
    if (p.a === p.b) { // self orbit (leader heartbeat)
      var ang = t * Math.PI * 2, R = 1.5;
      x = A.x + Math.cos(ang) * R; y = A.y + Math.sin(ang * 2) * 0.4; z = A.z + Math.sin(ang) * R;
    } else if (p.arch) { // peer arc: lifted midpoint
      var mx = (A.x + B.x) / 2, my = (A.y + B.y) / 2 + 0.9, mz = (A.z + B.z) / 2;
      var u = 1 - t;
      x = u*u*A.x + 2*u*t*mx + t*t*B.x;
      y = u*u*A.y + 2*u*t*my + t*t*B.y;
      z = u*u*A.z + 2*u*t*mz + t*t*B.z;
    } else { // straight beam ride with slight bow
      var bow = Math.sin(t * Math.PI) * 0.12;
      x = lerp(A.x, B.x, t); y = lerp(A.y, B.y, t) + bow; z = lerp(A.z, B.z, t);
    }
    if (!p._sp) {
      p._sp = T3.texSprite(TEX.glow, p.color || "#ffffff", 0.5, 1);
      T3.scene.add(p._sp);
      T3.pulseSprites.push(p);
    }
    p._sp.position.set(x, y, z);
    p._sp.material.color.set(p.color || "#ffffff");
    var s = 0.42 + 0.2 * Math.sin(t * Math.PI);
    p._sp.scale.set(s, s, 1);
  }
  /* drop sprites of dead pulses */
  for (i = T3.pulseSprites.length - 1; i >= 0; i--) {
    if (S.pulses.indexOf(T3.pulseSprites[i]) < 0) {
      var dead = T3.pulseSprites[i];
      if (dead._sp) T3.scene.remove(dead._sp);
      T3.pulseSprites.splice(i, 1);
    }
  }
  /* peer arcs */
  for (i = S.arcs.length - 1; i >= 0; i--) {
    var arc = S.arcs[i];
    arc.ttl -= dt * 0.8;
    if (arc.ttl <= 0) {
      if (arc._line) T3.scene.remove(arc._line);
      S.arcs.splice(i, 1);
      continue;
    }
    var PA = T3.entityAnchor(arc.a), PB = T3.entityAnchor(arc.b);
    if (!PA || !PB) continue;
    var pts = [];
    for (var s2 = 0; s2 <= 16; s2++) {
      var tt = s2 / 16, uu = 1 - tt;
      pts.push(new T3.THREE.Vector3(
        uu*uu*PA.x + 2*uu*tt*((PA.x+PB.x)/2) + tt*tt*PB.x,
        uu*uu*PA.y + 2*uu*tt*((PA.y+PB.y)/2 + 0.9) + tt*tt*PB.y,
        uu*uu*PA.z + 2*uu*tt*((PA.z+PB.z)/2) + tt*tt*PB.z));
    }
    if (!arc._line) {
      arc._line = new T3.THREE.Line(
        new T3.THREE.BufferGeometry().setFromPoints(pts),
        new T3.THREE.LineBasicMaterial({ color: new T3.THREE.Color("#ffb454"),
          transparent: true, opacity: 0.6, blending: T3.THREE.AdditiveBlending, depthWrite: false }));
      T3.scene.add(arc._line);
    } else {
      arc._line.geometry.setFromPoints(pts);
      arc._line.material.opacity = 0.6 * arc.ttl;
    }
  }
};
T3.updateFx = function (dt) {
  for (var i = T3.fx.length - 1; i >= 0; i--) {
    var f = T3.fx[i];
    f.t += dt;
    var k = f.t / f.ttl;
    if (k >= 1) {
      T3.scene.remove(f.obj);
      if (f.obj.geometry) f.obj.geometry.dispose();
      T3.fx.splice(i, 1);
      continue;
    }
    if (f.kind === "ring") {
      var s = (0.5 + k * 3.4) * (f.size || 1);
      f.obj.scale.set(s, s, 1);
      f.obj.material.opacity = 0.95 * (1 - k);
    } else if (f.kind === "burst") {
      var pa = f.obj.geometry.attributes.position.array;
      for (var j = 0; j < f.vel.length; j++) {
        pa[j*3] += f.vel[j][0] * dt;
        pa[j*3+1] += f.vel[j][1] * dt;
        pa[j*3+2] += f.vel[j][2] * dt;
        f.vel[j][0] *= (1 - dt * 1.4); f.vel[j][1] = f.vel[j][1] * (1 - dt * 1.4) + dt * 0.5;
        f.vel[j][2] *= (1 - dt * 1.4);
      }
      f.obj.geometry.attributes.position.needsUpdate = true;
      f.obj.material.opacity = 1 - k;
    } else if (f.kind === "glyph") {
      f.obj.position.y += (f.vy || 1) * dt;
      f.obj.material.opacity = 1 - k;
      var gs = 0.55 + k * 0.3;
      f.obj.scale.set(gs, gs, 1);
    } else if (f.kind === "flash") {
      var fs = (f.R || 1) * (2 + k * 3.2) * (f.size || 1);
      f.obj.scale.set(fs, fs, 1);
      f.obj.material.opacity = 0.95 * (1 - k);
    }
  }
};

/* ---- main loop ---- */
T3.loop = function () {
  requestAnimationFrame(T3.loop);
  if (!T3.renderer) return;
  var dt = Math.min(0.05, 0.016 + Math.random() * 0.004);
  T3.time += dt;
  S.wanderT += dt * (1 + S.work * 1.3);
  advanceTierPh(dt);
  var t = T3.time;
  T3.drainQueues();
  smoothChannels(dt);
  /* hologram flicker: breathing shimmer + rare glitch dip */
  if (Math.random() < 0.0009) S.glitch = 0.12;
  if (S.glitch > 0) S.glitch -= dt;
  S.flick = (0.93 + 0.05 * Math.sin(t * 29) + 0.02 * Math.sin(t * 47 + 1)) *
    (S.glitch > 0 ? 0.82 : 1);
  if (T3.eventLight && T3.eventLight.intensity > 0.5) {
    T3.eventLight.intensity *= Math.pow(0.02, dt);
    if (T3.eventLight.intensity < 0.5) T3.eventLight.intensity = 0;
  }
  var boost = (S.mode === "IDLE") ? 1 : 2.1;
  if (S.mode === "ERROR" && Date.now() > S.errUntil) {
    S.mode = (liveWorkers().length ? "WORKING" : "IDLE");
  }

  if (S.leader && S.leader.node) T3.animEntity(S.leader, dt, t, boost);
  var live = liveWorkers();
  for (var i = 0; i < live.length; i++) {
    if (live[i].node) T3.animEntity(live[i], dt, t, boost);
  }
  separateStep(dt); // runtime guarantee: no two cores ever rest merged
  T3.updateTethers();
  T3.updatePulses(dt, boost);
  T3.updateFx(dt);
  if (T3.starsFar) {
    T3.starsFar.rotation.y += dt * 0.0035;
    T3.starsFar.material.opacity = 0.5 + 0.14 * Math.sin(t * 0.9);
  }
  if (T3.starsNear) {
    T3.starsNear.rotation.y -= dt * 0.002;
    T3.starsNear.material.opacity = 0.72 + 0.16 * Math.sin(t * 1.3 + 2);
  }
  if (T3.nebulae) {
    for (var nbi = 0; nbi < T3.nebulae.length; nbi++) {
      T3.nebulae[nbi].material.rotation += dt * 0.004 * (nbi + 1);
    }
  }
  if (T3.grid) T3.grid.rotation.y += dt * 0.008;
  /* rare shooting stars */
  if (T3.meteors) {
    T3.meteorT -= dt;
    if (T3.meteorT <= 0 && T3.meteors.length < 3) {
      T3.meteorT = rand(7, 16);
      var ms = T3.texSprite(TEX.glow, "#cfe8ff", 1, 0.9);
      ms.position.set(rand(-14, 6), rand(3, 8), rand(-14, -8));
      ms.scale.set(2.4, 0.14, 1);
      T3.scene.add(ms);
      T3.meteors.push({ sp: ms, vx: rand(6, 10), vy: rand(-3.5, -2), t: 0, ttl: rand(0.9, 1.4) });
    }
    for (var mti = T3.meteors.length - 1; mti >= 0; mti--) {
      var mt = T3.meteors[mti];
      mt.t += dt;
      var mk = mt.t / mt.ttl;
      if (mk >= 1) { T3.scene.remove(mt.sp); T3.meteors.splice(mti, 1); continue; }
      mt.sp.position.x += mt.vx * dt;
      mt.sp.position.y += mt.vy * dt;
      mt.sp.material.opacity = 0.9 * Math.sin(mk * Math.PI);
    }
  }
  if (T3.floorGlow) T3.floorGlow.material.opacity = 0.26 + S.energy * 0.18 + 0.03 * Math.sin(t * 2);

  /* camera: cinematic drift + parallax + drag orbit + shake + energy push-in.
     Click-to-focus: look-target tracks the followed agent (leader by
     default); workers get framed closer, user zoom always wins. */
  S.camShake = Math.max(0, S.camShake - dt * 2.2);
  var shx = (Math.random() - 0.5) * S.camShake * 0.16;
  var shy = (Math.random() - 0.5) * S.camShake * 0.16;
  var az = Math.sin(t * 0.06) * 0.12 + T3.dragAz;
  /* elevation angle: -72 deg (deep below) .. +87 deg (near top-down) */
  var th = clamp(0.10 + Math.sin(t * 0.045) * 0.012 + T3.dragEl, -1.25, 1.53);
  S.camE = lerp(S.camE || 0, clamp(S.energy, 0, 1.2), 1 - Math.pow(0.02, dt));
  var nLead = Object.keys(S.leaders).length;
  if (!T3._effDist) T3._effDist = T3.camDist;
  var _ft = focusTarget();
  var _base = (_ft && _ft.kind === "worker") ? 7.4
    : ((S.usedMaxR || 5.8) + 4.8 + (nLead - 1) * 3.4);
  var _held = false;
  try { _held = (nowMs() - (T3.userZoomT || 0)) < 10000; } catch (e) {}
  var wantDist = _held ? T3.camDist : Math.max(T3.camDist, _base);
  T3._effDist = lerp(T3._effDist, wantDist, 1 - Math.pow(0.05, dt));
  var cd = T3._effDist - S.camE * 0.9; // slow push-in while excited, never pumping
  var ch = Math.cos(th) * cd;
  T3.camera.position.set(Math.sin(az) * ch + shx + (T3.panX || 0), 0.1 + Math.sin(th) * cd + shy, Math.cos(az) * ch + (T3.panZ || 0));
  var _fp = (_ft && _ft.node) ? _ft.node.group.position : null;
  /* caller-centered stage: children orbit just above their parent body,
     so the look-target lifts slightly only while nesting exists */
  var _lift = (S.famSet && Object.keys(S.famSet).length) ? 0.55 : 0;
  var _px = T3.panX || 0, _pz = T3.panZ || 0;
  if (_fp) {
    if (_lift) T3.look.lerp({ x: _fp.x + _px, y: _fp.y + _lift, z: _fp.z + _pz }, 1 - Math.pow(0.005, dt));
    else T3.look.lerp({ x: _fp.x + _px, y: _fp.y, z: _fp.z + _pz }, 1 - Math.pow(0.005, dt));
  } else if (S.leader && S.leader.node) {
    var _lp0 = S.leader.node.group.position;
    T3.look.lerp({ x: _lp0.x + _px, y: _lp0.y + _lift, z: _lp0.z + _pz }, 1 - Math.pow(0.005, dt));
  }
  T3.camera.lookAt(T3.look);
  /* focus marker: thin gold ring hugging the followed agent */
  if (T3.THREE && T3.scene) {
    if (!T3.focusRing) {
      try {
        T3.focusRing = new T3.THREE.Mesh(
          new T3.THREE.RingGeometry(1, 1.07, 64),
          new T3.THREE.MeshBasicMaterial({ color: new T3.THREE.Color("#ffe9a8"),
            transparent: true, opacity: 0.8, side: T3.THREE.DoubleSide,
            blending: T3.THREE.AdditiveBlending, depthWrite: false }));
        T3.focusRing.visible = false;
        T3.scene.add(T3.focusRing);
      } catch (e) { T3.focusRing = null; }
    }
    var _fr = T3.focusRing;
    if (_fr) {
      var _fw = (S.focus && _ft && _ft.node) ? _ft : null;
      _fr.visible = !!_fw;
      if (_fw) {
        var _rr = (_fw.kind === "worker" ? 1.35 : 2.35) * (1 + 0.06 * Math.sin(t * 4));
        _fr.position.copy(_fw.node.group.position);
        _fr.scale.set(_rr, _rr, _rr);
        _fr.lookAt(T3.camera.position);
        _fr.material.opacity = 0.55 + 0.25 * Math.sin(t * 4);
      }
    }
  }

  T3.hoverTick = (T3.hoverTick + 1) % 3;
  if (T3.hoverTick === 0) T3.updateHover();
  T3.reconTick = (T3.reconTick + 1) % 150;
  if (T3.reconTick === 0) E.reconcile();

  T3.renderer.render(T3.scene, T3.camera);
  updateLabels();
  refreshTip();
  updateHudDom();
};

/* project entity -> css px inside the wrap */
T3._v = null;
T3.toScreen = function (id) {
  var w = getEntity(id);
  if (!w || !w.node || !T3.camera || !cv) return null;
  var THREE = T3.THREE;
  if (!T3._v) T3._v = new THREE.Vector3();
  var anchor = w.node.anchor || w.node.group;
  T3._v.setFromMatrixPosition(anchor.matrixWorld);
  T3._v.project(T3.camera);
  var r = cv.getBoundingClientRect();
  var wrapR = wrap.getBoundingClientRect();
  return {
    x: (T3._v.x * 0.5 + 0.5) * r.width + (r.left - wrapR.left),
    y: (-T3._v.y * 0.5 + 0.5) * r.height + (r.top - wrapR.top),
    behind: T3._v.z > 1
  };
};

/* pick: raycast the invisible hit spheres -> entity id or null (shared by
   hover tracking and click-to-focus) */
T3.pickAt = function (nx, ny) {
  if (!T3.raycaster) {
    try { T3.raycaster = new T3.THREE.Raycaster(); } catch (e) { return null; }
  }
  if (!T3.camera) return null;
  var hits = [];
  Object.keys(S.leaders).forEach(function (k) {
    var lw = S.leaders[k];
    if (lw && lw.node && lw.node.hit) hits.push(lw.node.hit);
  });
  for (var i = 0; i < S.order.length; i++) {
    var w = S.workers[S.order[i]];
    if (w && w.node && w.node.hit && !w.deadAt) hits.push(w.node.hit);
  }
  if (!hits.length) return null;
  T3.raycaster.setFromCamera({ x: nx, y: ny }, T3.camera);
  try {
    var xs = T3.raycaster.intersectObjects(hits, false);
    if (xs && xs.length) return xs[0].object.userData.eid || null;
  } catch (e) { /* ignore */ }
  return null;
};
/* hover: raycast the invisible hit spheres -> rich tooltip follows cursor */
T3.updateHover = function () {
  var found = T3.pickAt(T3.ndcX, T3.ndcY);
  /* cursor position in wrap css px (for the tooltip) */
  var r = cv.getBoundingClientRect(), wrapR = wrap.getBoundingClientRect();
  setHover(found, (T3.ndcX * 0.5 + 0.5) * r.width + (r.left - wrapR.left),
    (-T3.ndcY * 0.5 + 0.5) * r.height + (r.top - wrapR.top));
};

T3.resize = function () {
  if (!T3.renderer || !cv || !wrap) return;
  var w = Math.max(80, wrap.clientWidth - 4);
  var h = 300;
  if (wrap.clientHeight >= 200) h = Math.min(1400, wrap.clientHeight);
  else {
    try {
      var cs = window.getComputedStyle(cv);
      var hh = parseInt(cs.height, 10);
      if (hh >= 200 && hh <= 1400) h = hh;
    } catch (e) { /* keep 300 */ }
  }
  T3.renderer.setSize(w, h, false);
  cv.style.width = w + "px"; cv.style.height = h + "px";
  T3.camera.aspect = w / h;
  T3.camera.updateProjectionMatrix();
};

/* map pan: glide over the plane in camera space (Shift+drag, right-drag,
   arrows/WASD). Clamped to a 30-unit leash so you never lose the map. */
T3.panBy = function (dxPx, dyPx) {
  var az = T3.dragAz || 0;
  var cd = T3._effDist || 10;
  var k = cd * 0.0016;
  var fx = -Math.sin(az), fz = -Math.cos(az);
  var rx = Math.cos(az), rz = -Math.sin(az);
  T3.panX = (T3.panX || 0) - (dxPx * rx + dyPx * fx) * k;
  T3.panZ = (T3.panZ || 0) - (dxPx * rz + dyPx * fz) * k;
  var pl = Math.hypot(T3.panX, T3.panZ);
  if (pl > 30) { T3.panX *= 30 / pl; T3.panZ *= 30 / pl; }
};
T3.bindInput = function () {
  if (!cv) return;
  cv.style.cursor = "grab";
  cv.addEventListener("pointerdown", function (e) {
    T3.dragging = true; T3.lastPX = e.clientX; T3.lastPY = e.clientY;
    T3.downX = e.clientX; T3.downY = e.clientY; T3.downT = nowMs();
    cv.style.cursor = "grabbing";
    try { cv.setPointerCapture(e.pointerId); } catch (err) { /* noop */ }
  });
  window.addEventListener("pointermove", function (e) {
    var r = cv.getBoundingClientRect();
    var nx = clamp(((e.clientX - r.left) / Math.max(1, r.width) - 0.5) * 2, -1, 1);
    var ny = clamp(((e.clientY - r.top) / Math.max(1, r.height) - 0.5) * 2, -1, 1);
    T3.ndcX = nx; T3.ndcY = -ny; // hover tracking never needs a press
    T3.px = e.clientX; T3.py = e.clientY;
    if (!T3.dragging) return; // camera moves by press+drag ONLY
    T3.mouseX = nx; T3.mouseY = ny;
    var _mdx = e.clientX - T3.lastPX, _mdy = e.clientY - T3.lastPY;
    if (e.shiftKey || e.buttons === 2) T3.panBy(_mdx, _mdy); // map glide
    else {
      T3.dragAz = clamp(T3.dragAz - _mdx * 0.005, -3.14, 3.14);
      T3.dragEl = clamp(T3.dragEl + _mdy * 0.005, -2.2, 2.2);
    }
    T3.lastPX = e.clientX; T3.lastPY = e.clientY;
  });
  cv.addEventListener("contextmenu", function (e) { try { e.preventDefault(); } catch (err) {} });
  window.addEventListener("pointerup", function (e) {
    /* click (not drag): short press, almost no travel, started on canvas */
    try {
      if (e && e.target === cv && T3.downT) {
        var moved = Math.hypot((e.clientX || 0) - (T3.downX || 0), (e.clientY || 0) - (T3.downY || 0));
        if (moved < 6 && nowMs() - T3.downT < 450) {
          var r = cv.getBoundingClientRect();
          var nx = clamp(((e.clientX - r.left) / Math.max(1, r.width) - 0.5) * 2, -1, 1);
          var ny = clamp(((e.clientY - r.top) / Math.max(1, r.height) - 0.5) * 2, -1, 1);
          E.toggleFocus(T3.pickAt(nx, -ny));
        }
      }
    } catch (err) { /* never break input */ }
    T3.downT = 0;
    T3.dragging = false; cv.style.cursor = "grab";
  });
  window.addEventListener("keydown", function (e) {
    if (e && e.key === "Escape") E.clearFocus();
    try {
      var _tag = (e.target && e.target.tagName) || "";
      if (_tag === "INPUT" || _tag === "TEXTAREA" || e.ctrlKey || e.metaKey) return;
      var _kp = 56, _kd = false;
      if (e.key === "ArrowLeft" || e.key === "a" || e.key === "A") { T3.panBy(-_kp, 0); _kd = true; }
      else if (e.key === "ArrowRight" || e.key === "d" || e.key === "D") { T3.panBy(_kp, 0); _kd = true; }
      else if (e.key === "ArrowUp" || e.key === "w" || e.key === "W") { T3.panBy(0, -_kp); _kd = true; }
      else if (e.key === "ArrowDown" || e.key === "s" || e.key === "S") { T3.panBy(0, _kp); _kd = true; }
      if (_kd && e.preventDefault) e.preventDefault();
    } catch (err) {}
  });
  cv.addEventListener("dblclick", function () {
    T3.dragAz = 0; T3.dragEl = 0; T3.camDist = CAM_DIST0;
    T3.panX = 0; T3.panZ = 0;
    E.clearFocus();
  });
  cv.addEventListener("wheel", function (e) {
    e.preventDefault();
    T3.camDist = clamp(T3.camDist + (e.deltaY > 0 ? 0.6 : -0.6), 2.5, 42);
    T3.userZoomT = nowMs(); // manual zoom holds ~10s before auto-frame resumes
  }, { passive: false });
};
/* ============ 8. FLAT FALLBACK (offline golden orbs, 2D) ==================
   Same API, same choreography language — plain canvas2d so the deck still
   shows leader + tethered workers + pulses + shockwaves with zero deps. */
var FL = {
  g: null, W: 300, H: 300, t: 0,
  parts: [], // burst particles {x,y,vx,vy,life,color}
  waves: [], // shockwaves {x,y,r,vr,life,color}
  texts: []  // rising glyphs {x,y,txt,color,life}
};
FL.init = function () {
  try { FL.g = cv.getContext("2d"); } catch (e) { FL.g = null; }
  if (!FL.g) return;
  FL.resize();
  window.addEventListener("resize", FL.resize);
  FL.mouse = { x: 0, y: 0, inside: false };
  cv.addEventListener("pointermove", function (e) {
    var r = cv.getBoundingClientRect();
    FL.mouse.x = (e.clientX - r.left) * (FL.W / Math.max(1, r.width));
    FL.mouse.y = (e.clientY - r.top) * (FL.H / Math.max(1, r.height));
    FL.mouse.inside = true;
  });
  cv.addEventListener("pointerleave", function () { FL.mouse.inside = false; });
  FL.pickAt = function (x, y) {
    var L0 = FL.leaderXY(), live0 = liveWorkers(), id = null, best = 1e9;
    if (Math.hypot(x - L0[0], y - L0[1]) < 100) { id = "leader"; best = 1e9 - 1; }
    for (var hi = 0; hi < live0.length; hi++) {
      var hw = live0[hi];
      if (!hw.node) continue;
      var dw = Math.hypot(x - hw.node.x, y - hw.node.y);
      if (dw < 48 && dw < best) { id = hw.id; best = dw; }
    }
    return id;
  };
  cv.addEventListener("pointerdown", function (e) {
    FL.downX = e.clientX; FL.downY = e.clientY; FL.downT = nowMs();
  });
  cv.addEventListener("pointerup", function (e) {
    try {
      if (FL.downT && Math.hypot((e.clientX || 0) - (FL.downX || 0), (e.clientY || 0) - (FL.downY || 0)) < 6 &&
          nowMs() - FL.downT < 450) {
        var r = cv.getBoundingClientRect();
        E.toggleFocus(FL.pickAt(
          (e.clientX - r.left) * (FL.W / Math.max(1, r.width)),
          (e.clientY - r.top) * (FL.H / Math.max(1, r.height))));
      }
    } catch (err) { /* never break input */ }
    FL.downT = 0;
  });
  cv.addEventListener("dblclick", function () { E.clearFocus(); });
  try {
    if (window.ResizeObserver) {
      var fro = new window.ResizeObserver(function () { FL.resize(); });
      fro.observe(wrap);
    }
  } catch (e) { /* window resize listener is enough */ }
  E._toScreen = FL.toScreen;
  FL.layout();
  requestAnimationFrame(FL.loop);
};
FL.resize = function () {
  if (!wrap || !cv) return;
  FL.W = Math.max(80, wrap.clientWidth - 4);
  FL.H = (wrap.clientHeight >= 200) ? Math.min(1400, wrap.clientHeight) : 300;
  cv.width = FL.W; cv.height = FL.H;
  cv.style.width = FL.W + "px"; cv.style.height = FL.H + "px";
};
FL.leaderXY = function () { return [FL.W * 0.5 + Math.sin(S.wanderT * 0.21) * 14, FL.H * 0.52 + Math.sin(S.wanderT * 0.33) * 8]; };
FL.layout = function () {
  layoutOrbits();
  var live = liveWorkers();
  for (var i = 0; i < live.length; i++) {
    var w = live[i];
    if (!w.node) {
      var L = FL.leaderXY();
      w.node = { x: L[0] + 40, y: L[1] + 80 };
    }
    if (w._orbA == null) w._orbA = 0;
  }
};
FL.toScreen = function (id) {
  var r = cv.getBoundingClientRect(), wrapR = wrap.getBoundingClientRect();
  var p, dy;
  if (id === "leader") { p = FL.leaderXY(); dy = 82; }
  else {
    var w = S.workers[id];
    if (!w || !w.node) return null;
    p = [w.node.x, w.node.y]; dy = 52;
  }
  return { x: (p[0] / FL.W) * r.width + (r.left - wrapR.left),
           y: (p[1] / FL.H) * r.height + (r.top - wrapR.top) + dy,
           behind: false };
};
FL.GOLD_STOPS = ["255,248,225", "232,196,110", "190,130,40", "150,100,25"];
FL.NEXUS_STOPS = ["235,255,250", "110,230,215", "45,150,140", "20,90,85"];
FL.orb = function (x, y, R, hot, alpha, flick, stops) {
  var g = FL.g;
  var st = stops || FL.GOLD_STOPS;
  var gr = g.createRadialGradient(x, y, 1, x, y, R);
  gr.addColorStop(0, "rgba(" + st[0] + "," + alpha + ")");
  gr.addColorStop(0.35, "rgba(" + st[1] + "," + (alpha * 0.75) + ")");
  gr.addColorStop(0.7, "rgba(" + st[2] + "," + (alpha * 0.30 * flick) + ")");
  gr.addColorStop(1, "rgba(" + st[3] + ",0)");
  g.fillStyle = gr;
  g.beginPath(); g.arc(x, y, R, 0, 7); g.fill();
  /* ember speckles on the shell */
  g.fillStyle = "rgba(249,231,176," + (alpha * 0.9) + ")";
  for (var i = 0; i < 26; i++) {
    var a = (i * 2.399) + FL.t * (0.4 + (i % 3) * 0.2);
    var rr = R * (0.55 + 0.4 * Math.abs(Math.sin(i * 7.3)));
    g.fillRect(x + Math.cos(a) * rr, y + Math.sin(a) * rr * 0.9, 1.6, 1.6);
  }
  /* gyro ellipses */
  g.strokeStyle = "rgba(255,200,110," + (alpha * 0.5) + ")";
  g.lineWidth = 1;
  for (var k = 0; k < 2; k++) {
    g.beginPath();
    g.ellipse(x, y, R * 1.25, R * (0.42 + k * 0.2),
      Math.sin(FL.t * (0.5 + k * 0.3)) * 0.6, 0, Math.PI * 2);
    g.stroke();
  }
};
FL.loop = function () {
  requestAnimationFrame(FL.loop);
  var g = FL.g;
  if (!g) return;
  FL.t += 0.016;
  S.wanderT += 0.016 * (1 + S.work * 1.3);
  advanceTierPh(0.016);
  var t = FL.t;
  var wt = S.wanderT; // fallback ambient motions ride the same clock
  smoothChannels(0.016);
  var boost = (S.mode === "IDLE") ? 1 : 2.1;
  if (S.mode === "ERROR" && Date.now() > S.errUntil) {
    S.mode = liveWorkers().length ? "WORKING" : "IDLE";
  }
  g.clearRect(0, 0, FL.W, FL.H);
  g.fillStyle = "#040a10";
  g.fillRect(0, 0, FL.W, FL.H);
  g.fillStyle = "rgba(53,240,208,.08)";
  for (var gx = 12; gx < FL.W; gx += 26)
    for (var gy = 12; gy < FL.H; gy += 26) g.fillRect(gx, gy, 1, 1);

  var L = FL.leaderXY();
  var excite = clamp(S.energy, 0, 1.2);
  /* round formation paths: one faint ellipse per used tier */
  g.strokeStyle = "rgba(212,175,55,0.10)";
  g.lineWidth = 1.2;
  if (S.famSet && Object.keys(S.famSet).length) {
    /* caller-centered: each parent owns a small ring around its own body
       (no far exile ellipse — that sprawl is gone) */
    Object.keys(S.famSet).forEach(function (pid) {
      var pp = S.workers[pid];
      if (pp && pp.node) {
        g.beginPath();
        g.arc(pp.node.x, pp.node.y, ((S.famRho && S.famRho[pid]) || 1.7) * 46, 0, Math.PI * 2);
        g.stroke();
      }
    });
  }
  var _seenT = {};
  liveWorkers().forEach(function (pw) {
    var _pL = tiersArr(pw), _pti = (pw._tier == null) ? 0 : pw._tier;
    var _pkey = tierListOf(pw) + _pti;
    if (_seenT[_pkey] || !_pL[_pti]) return;
    _seenT[_pkey] = true;
    g.beginPath();
    g.ellipse(L[0], L[1] + _pL[_pti].y * 30,
      _pL[_pti].r * 46, 40, 0, 0, Math.PI * 2);
    g.stroke();
  });
  /* drain queues into flat particles */
  var i, q;
  for (i = S.bursts.length - 1; i >= 0; i--) {
    q = S.bursts[i]; S.bursts.splice(i, 1);
    var bp = (q.at === "leader") ? L : (((S.workers[q.at] || {}).node) || { x: L[0], y: L[1] });
    for (var b = 0; b < Math.min(90, q.n); b++) {
      var a = rand(0, Math.PI * 2), sp = rand(20, 130);
      FL.parts.push({ x: bp.x !== undefined ? (bp.x || L[0]) : L[0],
        y: (bp.y !== undefined ? bp.y : L[1]),
        vx: Math.cos(a) * sp, vy: Math.sin(a) * sp - 30,
        life: 1, color: q.color });
    }
  }
  /* rings -> expanding circles, flashes -> quick pops, floats -> rising glyphs */
  (function drainFlatFx() {
    var k, qq, qp;
    for (k = 0; k < S.rings.length; k++) {
      qq = S.rings[k];
      qp = (qq.at === "leader") ? { x: L[0], y: L[1] } : (((S.workers[qq.at] || {}).node) || { x: L[0], y: L[1] });
      FL.waves.push({ x: qp.x, y: qp.y, r: 12, vr: 130 * (qq.size || 1), life: 1, color: qq.color });
    }
    S.rings.length = 0;
    for (k = 0; k < S.flashes.length; k++) {
      qq = S.flashes[k];
      qp = (qq.at === "leader") ? { x: L[0], y: L[1] } : (((S.workers[qq.at] || {}).node) || { x: L[0], y: L[1] });
      FL.waves.push({ x: qp.x, y: qp.y, r: 6, vr: 220 * (qq.size || 1), life: 0.8, color: qq.color });
    }
    S.flashes.length = 0;
    for (k = 0; k < S.floats.length; k++) {
      qq = S.floats[k];
      qp = (qq.at === "leader") ? { x: L[0], y: L[1] } : (((S.workers[qq.at] || {}).node) || { x: L[0], y: L[1] });
      FL.texts.push({ x: qp.x + 14, y: qp.y - 30, txt: qq.txt, color: qq.color, life: 1 });
    }
    S.floats.length = 0;
    S.beams.length = 0;
    if (FL.waves.length > 24) FL.waves.splice(0, FL.waves.length - 24);
    if (FL.texts.length > 20) FL.texts.splice(0, FL.texts.length - 20);
  })();

  /* free orbits around the centered leader */
  var live = liveWorkers();
  live.forEach(function (w) {
    if (!w.node) return;
    if (w.deadAt) {
      var _f = clamp(1 - (nowMs() - w.deadAt) / 2500, 0, 1);
      if (_f <= 0) {
        delete S.workers[w.id];
        var _ix = S.order.indexOf(w.id);
        if (_ix >= 0) S.order.splice(_ix, 1);
        dropLabel(w.id);
        return;
      }
      w._flatFade = _f;
    } else {
      w._flatFade = 1;
    }
    var tx2, ty2;
    var _fhp = (w._home3 && w.parent && S.workers[w.parent] && S.workers[w.parent].node) ? S.workers[w.parent].node : null;
    if (_fhp && w._home3) {
      tx2 = _fhp.x + w._home3.x * 46 + Math.sin(wt * 0.43 + w.phase) * 2;
      ty2 = _fhp.y + w._home3.y * 46 + Math.sin(wt * 0.9 + w.phase) * 2;
    } else {
      var _fh2 = w._home || { a: 0, r: 5, y: 0 };
      tx2 = L[0] + Math.cos(_fh2.a) * _fh2.r * 46 + Math.sin(wt * 0.43 + w.phase) * 3;
      ty2 = L[1] + _fh2.y * 30 + Math.sin(_fh2.a) * _fh2.r * 18 + Math.sin(wt * 0.9 + w.phase) * 3;
    }
    w.node.x = lerp(w.node.x, tx2, 0.06);
    w.node.y = lerp(w.node.y, ty2, 0.06);
    var fade = (w._flatFade != null ? w._flatFade : 1) * (w.retired ? 0.5 : 1);
    g.strokeStyle = "rgba(212,175,55," + (0.42 * fade) + ")";
    g.lineWidth = 1.6;
    /* caller-centered tether (2D): child -> live parent body, else leader */
    var _fsP = (w.parent && S.workers[w.parent] && S.workers[w.parent].node && !S.workers[w.parent].deadAt)
      ? S.workers[w.parent].node : null;
    var _fsA = _fsP ? { x: _fsP.x, y: _fsP.y } : { x: L[0], y: L[1] };
    g.beginPath(); g.moveTo(_fsA.x, _fsA.y);
    var mx = (_fsA.x + w.node.x) / 2, my = (_fsA.y + w.node.y) / 2 - 10;
    g.quadraticCurveTo(mx, my, w.node.x, w.node.y);
    g.stroke();
    /* endpoint glows */
    g.fillStyle = "rgba(255,233,168," + (0.85 * fade) + ")";
    g.beginPath(); g.arc(_fsA.x, _fsA.y,[1], 3, 0, 7); g.fill();
    g.fillStyle = "rgba(232,196,110," + (0.8 * fade) + ")";
    g.beginPath(); g.arc(w.node.x, w.node.y, 2.6, 0, 7); g.fill();
  });
  separateFlat(); // runtime guarantee (2D): no two orbs ever rest merged

  /* pulses (caller-centered: a/b may be parent bodies, leaders, workers) */
  for (i = S.pulses.length - 1; i >= 0; i--) {
    var p = S.pulses[i];
    p.t += (p.sp || 0.028) * boost * (1 + S.energy * 1.5) * (1 + S.work * 2);
    if (p.t > 1.15) { S.pulses.splice(i, 1); continue; }
    if (p.t < 0) continue;
    var _fA = function (id) {
      if (!id || id === "leader") return { x: L[0], y: L[1] };
      if (S.workers[id] && S.workers[id].node) return S.workers[id].node;
      var _ls = (id.slice(0, 2) === "L:") ? S.leaders[id.slice(2)] : S.leaders[id];
      if (_ls && _ls.node) return { x: L[0], y: L[1] }; // peers share the deck center in 2D
      return { x: L[0], y: L[1] };
    };
    var A = _fA(p.a);
    var B = _fA(p.b);
    var tt = clamp(p.t, 0, 1);
    var px = lerp(A.x, B.x, tt), py = lerp(A.y, B.y, tt) + Math.sin(tt * Math.PI) * 8;
    g.fillStyle = p.color || "#fff";
    g.beginPath(); g.arc(px, py, 3.2, 0, 7); g.fill();
    g.fillStyle = "rgba(255,255,255,.35)";
    g.beginPath(); g.arc(px, py, 6.5, 0, 7); g.fill();
  }
  for (i = S.arcs.length - 1; i >= 0; i--) {
    S.arcs[i].ttl -= 0.02;
    if (S.arcs[i].ttl <= 0) S.arcs.splice(i, 1);
  }

  /* burst particles */
  for (i = FL.parts.length - 1; i >= 0; i--) {
    var pt = FL.parts[i];
    pt.life -= 0.02;
    if (pt.life <= 0) { FL.parts.splice(i, 1); continue; }
    pt.x += pt.vx * 0.016; pt.y += pt.vy * 0.016;
    pt.vx *= 0.985; pt.vy = pt.vy * 0.985 - 0.4;
    g.fillStyle = pt.color || "#ffcf7a";
    g.globalAlpha = clamp(pt.life, 0, 1);
    g.fillRect(pt.x, pt.y, 2, 2);
    g.globalAlpha = 1;
  }
  if (FL.parts.length > 500) FL.parts.splice(0, FL.parts.length - 500);

  /* leader + workers orbs (big stage: leader 66, workers 32) */
  var flick = 0.85 + 0.15 * Math.sin(t * 7) * Math.sin(t * 3.1);
  var R0 = 66 + ((S.leader && S.leader.exc) || 0) * 12 + ((S.leader && S.leader.act) || 0) * 6 + Math.sin(wt * 2.2) * 3;
  if (S.mode === "ERROR") {
    FL.orb(L[0], L[1], R0, true, 1, flick, FL.NEXUS_STOPS);
    g.fillStyle = "rgba(255,60,60,.25)";
    g.beginPath(); g.arc(L[0], L[1], R0 * 1.1, 0, 7); g.fill();
  } else {
    FL.orb(L[0], L[1], R0, true, 1, flick, FL.NEXUS_STOPS);
  }
  if (S.focus === "leader") {
    g.strokeStyle = "rgba(255,233,168,.85)";
    g.lineWidth = 2;
    g.beginPath(); g.arc(L[0], L[1], R0 + 12 + 3 * Math.sin(t * 4), 0, 7); g.stroke();
  }
  g.fillStyle = "rgba(235,250,246,.95)";
  g.font = "bold 13px Consolas,monospace"; g.textAlign = "left";
  live.forEach(function (w) {
    if (!w.node) return;
    var fade2 = (w._flatFade != null ? w._flatFade : 1) * (w.retired ? 0.5 : 1);
    var wAct2 = (w.act || 0), wFlick = flick * (0.92 + 0.08 * Math.sin(t * 5 + (w.phase || 0)));
    FL.orb(w.node.x, w.node.y, (depthOf(w) >= 3 ? 18 : (depthOf(w) === 2 ? 24 : 32)) * (1 + wAct2 * 0.12), false, fade2, wFlick);
    if (S.focus === w.id) {
      g.strokeStyle = "rgba(255,233,168,.85)";
      g.lineWidth = 2;
      g.beginPath(); g.arc(w.node.x, w.node.y, 40 + 2 * Math.sin(t * 4), 0, 7); g.stroke();
    }
    g.fillStyle = w.tint || "#ffb545";
    g.beginPath(); g.arc(w.node.x - 22, w.node.y - 22, 4.5, 0, 7); g.fill();
    g.fillStyle = "rgba(235,240,250,.95)";
  });

  /* hover by proximity (fallback has no raycaster) */
  (function flatHover() {
    var id = null;
    if (FL.mouse && FL.mouse.inside) {
      var best = 1e9;
      var dl = Math.hypot(FL.mouse.x - L[0], FL.mouse.y - L[1]);
      if (dl < R0 * 1.5) { id = "leader"; best = dl; }
      for (var hi = 0; hi < live.length; hi++) {
        var hw = live[hi];
        if (!hw.node) continue;
        var dw = Math.hypot(FL.mouse.x - hw.node.x, FL.mouse.y - hw.node.y);
        if (dw < 48 && dw < best) { id = hw.id; best = dw; }
      }
      var r = cv.getBoundingClientRect(), wrapR = wrap.getBoundingClientRect();
      setHover(id,
        (FL.mouse.x / FL.W) * r.width + (r.left - wrapR.left),
        (FL.mouse.y / FL.H) * r.height + (r.top - wrapR.top));
    } else {
      setHover(null);
    }
  })();

  /* shockwaves + rising glyphs */
  for (i = FL.waves.length - 1; i >= 0; i--) {
    var wv = FL.waves[i];
    wv.life -= 0.025; wv.r += wv.vr * 0.016;
    if (wv.life <= 0) { FL.waves.splice(i, 1); continue; }
    g.strokeStyle = wv.color || "#ffcf7a";
    g.globalAlpha = clamp(wv.life, 0, 1) * 0.9;
    g.lineWidth = 2;
    g.beginPath(); g.arc(wv.x, wv.y, wv.r, 0, 7); g.stroke();
    g.globalAlpha = 1;
  }
  g.textAlign = "center";
  for (i = FL.texts.length - 1; i >= 0; i--) {
    var tx = FL.texts[i];
    tx.life -= 0.02; tx.y -= 0.7;
    if (tx.life <= 0) { FL.texts.splice(i, 1); continue; }
    g.globalAlpha = clamp(tx.life, 0, 1);
    g.fillStyle = tx.color || "#ffe9a8";
    g.font = "bold 15px Consolas,monospace";
    g.fillText(tx.txt, tx.x, tx.y);
    g.globalAlpha = 1;
  }
  g.textAlign = "left";

  /* status strip */
  g.fillStyle = "rgba(230,255,250,.95)";
  g.font = "bold 15px Consolas,monospace"; g.textAlign = "left";
  g.fillText(S.mode, 10, FL.H - 26);
  g.fillStyle = "rgba(95,127,122,.95)";
  g.font = "11px Consolas,monospace";
  g.fillText("TOOLS " + S.hud.tools + " \u00b7 WORKERS " + liveWorkers().length +
    " \u00b7 " + S.hud.round + (S.focus ? " \u00b7 \u25c9 " + S.focus : ""), 10, FL.H - 10);
  FL.reconTick = ((FL.reconTick || 0) + 1) % 150;
  if (FL.reconTick === 0) E.reconcile();
  updateLabels();
  refreshTip();
  updateHudDom();
};

/* ================= 9. LABELS + TOOLTIP + HUD =========================== */
function setHover(id, x, y) {
  if (S.hover.id !== id) S.hover.id = id;
  if (x !== undefined) { S.hover.x = x; S.hover.y = y; }
}
function tipHtml(id) {
  var w = getEntity(id);
  if (!w) return "";
  var isL = (id === "leader") || (w.kind === "peer");
  var rows = "";
  if (w.sid) rows = '<div class="row"><span class="k">session</span><span class="v">' +
    escHtml(String(w.sid).slice(-8)) + "</span></div>";
  var st = isL ? S.mode.toLowerCase() : (w.status || "?");
  rows += '<div class="row"><span class="k">status</span><span class="v">' + escHtml(st) + "</span></div>";
  if (w.lastAction) rows += '<div class="row"><span class="k">last</span><span class="v">' + escHtml(w.lastAction) + "</span></div>";
  if (isL) {
    rows += '<div class="row"><span class="k">model</span><span class="v">' + escHtml(String(S.hud.model)) + "</span></div>";
    rows += '<div class="row"><span class="k">memory</span><span class="v">' + escHtml(String(S.hud.mem)) + " facts</span></div>";
    rows += '<div class="row"><span class="k">turn</span><span class="v">tools ' + escHtml(String(S.hud.tools)) +
      " \u00b7 workers " + escHtml(String(S.hud.workers)) + " \u00b7 " + escHtml(String(S.hud.round)) + "</span></div>";
  } else {
    rows += '<div class="row"><span class="k">tint</span><span class="v" style="color:' + escHtml(w.tint) + '">' +
      escHtml(w.tint) + "</span></div>";
    if (depthOf(w) >= 2) {
      rows += '<div class="row"><span class="k">nested</span><span class="v">depth ' +
        escHtml(String(depthOf(w))) + " \u00b7 parent " + escHtml(String((w.parent || "?")).slice(-14)) + "</span></div>";
    }
  }
  var tools = "";
  if (w.orbiters && w.orbiters.length) {
    tools = '<div class="tools">' + w.orbiters.map(function (o) {
      return '<span class="e3-chip" style="border-color:' + escHtml(o.color) + ";color:" + escHtml(o.color) +
        '">' + escHtml(o.tool) + "</span>";
    }).join("") + "</div>";
  } else {
    tools = '<div class="row"><span class="k">tools</span><span class="v dim">idle</span></div>';
  }
  return "<h4>" + escHtml(isL ? "NEXUS · LEADER" : String(w.label || id).toUpperCase()) + "</h4>" + rows + tools;
}
var lastTipKey = "", lastTipTick = 0;
function refreshTip() {
  if (!tipEl) return;
  var id = S.hover.id;
  var w = id ? getEntity(id) : null;
  if (!w || (w.deadAt && id !== "leader")) {
    if (tipEl.style.display !== "none") tipEl.style.display = "none";
    lastTipKey = "";
    return;
  }
  var now = nowMs();
  var key = id + "|" + (w.status || "") + "|" + (w.lastAction || "") + "|" +
    (w.orbiters ? w.orbiters.length : 0) + "|" + S.hud.model + "|" + S.hud.mem + "|" + S.hud.tools;
  if (key !== lastTipKey || now - lastTipTick > 600) {
    tipEl.innerHTML = tipHtml(id);
    lastTipKey = key; lastTipTick = now;
  }
  tipEl.style.display = "block";
  var wrapW = (wrap && wrap.clientWidth) || 600;
  var tx = clamp(S.hover.x + 18, 8, Math.max(8, wrapW - 260));
  var ty = clamp(S.hover.y - 10, 8, 400);
  tipEl.style.transform = "translate(" + Math.round(tx) + "px," + Math.round(ty) + "px)";
}
var lastLabelTick = 0;
function statusColor(st) {
  if (st === "working") return "#ffb454";
  if (st === "done") return "#3dffa2";
  if (st === "fail") return "#ff5d5d";
  return "#5f7f7a";
}
function updateLabels() {
  var now = nowMs();
  if (now - lastLabelTick < 66) return; // ~15fps DOM writes
  lastLabelTick = now;
  if (!E._toScreen || !labelLayer) return;
  var ids = Object.keys(S.leaders).map(function (k) { return S.leaders[k] === S.leader ? "leader" : k; });
  ids = ids.concat(S.order.filter(function (id) { return S.workers[id]; }));
  for (var i = 0; i < ids.length; i++) {
    (function (id) {
      var w = getEntity(id);
      if (!w) return;
      var rec = labelDivs[id] || makeLabel(id,
        String(w.label || id).toUpperCase().slice(0, 12),
        id === "leader" ? GOLD.mid : w.tint);
      var pos = safe(function () { return E._toScreen(id); });
      if (!pos || pos.behind) { rec.box.style.display = "none"; return; }
      rec.box.style.display = "block";
      var deadF = w.retired ? 0.8 : (w.deadAt ? clamp(1 - (now - w.deadAt) / 6000, 0, 1) : 1);
      rec.box.style.opacity = deadF.toFixed(2);
      rec.box.style.transform = "translate(" + Math.round(pos.x) + "px," +
        Math.round(pos.y) + "px) translate(-50%, 6px)";
      var isHot = (S.hover.id === id) || (S.focus === id);
      if (rec.box.classList) {
        if (isHot) rec.box.classList.add("hot");
        else rec.box.classList.remove("hot");
      }
      var st = (id === "leader") ? S.mode.toLowerCase() : (w.status || "?");
      var stHtml = '<span style="color:' + statusColor(id === "leader" ? "" : w.status) + '">' +
        escHtml(st) + "</span>";
      if (rec._st !== st) { rec.st.innerHTML = stHtml; rec._st = st; }
      // info line: model + memory for the leader; status + action + round for workers
      var sub;
      if (id === "leader") {
        sub = "MODEL " + String(S.hud.model).split("/").pop().slice(0, 16) +
          " \u00b7 MEM " + String(S.hud.mem);
        if (w.lastAction) sub += " \u00b7 " + w.lastAction.slice(0, 24);
      } else {
        sub = (w.lastAction || "").slice(0, 30);
        if (w.round) sub += (sub ? " \u00b7 " : "") + w.round.slice(0, 8);
        sub = sub.slice(0, 44);
      }
      if (rec._sub !== sub) { rec.sub.textContent = sub; rec._sub = sub; }
      if (rec._chips !== "") { rec.chips.innerHTML = ""; rec._chips = ""; }
    })(ids[i]);
  }
}
var lastHudWrite = 0, lastHudStr = "";
function updateHudDom() {
  if (!hudEl) return;
  var now = nowMs();
  if (now - lastHudWrite < 400) return;
  var str = S.hud.mode + "|" + S.hud.tools + "|" + S.hud.workers + "|" +
    S.hud.round + "|" + S.hud.model + "|" + S.hud.mem + "|" + (S.focus || "") + "|" +
    S.hud.sess + "|" + S.hud.link + "|" + S.hud.clock;
  if (str === lastHudStr) return;
  lastHudStr = str; lastHudWrite = now;
  var _fw = S.focus ? getEntity(S.focus) : null;
  var _flbl = _fw ? String(_fw.label || S.focus).toUpperCase().slice(0, 12) : "";
  var _linkCls = (/RETRY|ERROR|WORKING/.test(String(S.hud.link))) ? "e3-hud-link retry" : "e3-hud-link";
  hudEl.innerHTML =
    '<b class="e3-hud-mode">' + escHtml(S.hud.mode) + "</b>" +
    '<span>TOOLS ' + escHtml(String(S.hud.tools)) + " \u00b7 WORKERS " +
    escHtml(String(S.hud.workers)) + " \u00b7 " + escHtml(String(S.hud.round)) +
    (S.focus ? " \u00b7 \u25c9 " + escHtml(_flbl) : "") + "</span>" +
    '<span class="e3-hud-sess">' + escHtml(String(S.hud.sess)) + "</span>" +
    '<span class="e3-hud-model">' + escHtml(String(S.hud.model)) + "</span>" +
    '<span class="' + _linkCls + '">\u25cf ' + escHtml(String(S.hud.link)) + "</span>" +
    '<span class="e3-hud-clock">' + escHtml(String(S.hud.clock)) + "</span>";
}

/* ============================ 10. BOOT & EXPORT ========================== */
E._debugTheme = function (id) {
  var w = getEntity(id);
  if (!w) return null;
  return { stack: (w.themeStack || []).map(function (s) { return s.key; }),
    top: themeOf(w), status: w.status, orbiters: (w.orbiters || []).length };
};
E._debugNest = function () {
  var out = [];
  for (var i = 0; i < S.order.length; i++) {
    var w = S.workers[S.order[i]];
    if (w) out.push({ id: w.id, parent: w.parent || null, depth: depthOf(w),
      home: w._home ? [+w._home.r.toFixed(2), +w._home.y.toFixed(2)] : null,
      shell: !!w._home3, status: w.status });
  }
  return out;
};
window.Entity3D = E;
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
})();





