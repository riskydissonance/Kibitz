// Vendored locally (frontend/vendor) so the board works fully offline — no CDN dependency.
import { Chessground } from "/vendor/chessground.min.js";
import { Chess } from "/vendor/chess.min.js";

// --- state ---------------------------------------------------------------
const chess = new Chess();
let ground = null;

let timeline = []; // nodes 0..N for the whole game
let mistakes = [];
let player = "white"; // the reviewed side (drives the header label)
let orient = "white"; // board orientation; starts at `player` but the `f` hotkey flips it
// Current game's PGN + player names, so "Review other side" can re-open the same game reviewing
// the opponent without a refetch. Set when a game is opened (provisional) and on /session.
let currentPgn = null;
let gameWhite = "";
let gameBlack = "";

let cur = 0; // current timeline node (valid when !exploring)
let anchorNode = 0; // the review (mistake) node we started from
let currentMistake = -1;
let currentPrompt = "";

let exploring = false; // off the game line, free-playing variations
let exploreBaseNode = 0; // node we left the timeline from
// Verdict for the move just tried in explore mode, surfaced in the (always-visible) status
// banner under the board — the full #verdict panel lives far down the scrolling side column,
// so without this you'd never see "good / mistake / blunder" for a variation you played.
// null = none, "pending" = evaluating, move dict = result, {error:true} = failed.
let exploreVerdict = null;

// Both arrow kinds are ON by default; `...Wanted` remembers the user's choice so the forced-off
// state during an analysis sweep (see setAnalyzingUI) restores their preference afterwards.
let bestArrowOn = true;
let bestArrowWanted = true;
// Live best-move arrows: progressively deepen and refine while you sit on a position,
// cancelled the moment the position changes, with a hard time cap so it never runs forever.
// They grade the move just completed on the board: the search runs from the position that move
// was played FROM, and when the played move was itself the engine's best, `playedWasBest`
// collapses the dark played-move arrow and the green best-move arrow into one green arrow.
let bestArrows = [];
let playedWasBest = false;
// Threat arrows (yellow): what the side that just moved is threatening to play next
// (a null-move engine search server-side). Toggled like the best-move arrows.
let threatArrowOn = true;
let threatArrowWanted = true;
let threatArrows = [];
const THREAT_DEPTH = 16; // one fixed-depth probe is enough for "what's the threat?"
let searchGen = 0; // bumped on every position change to invalidate in-flight searches
// Abort superseded engine requests instead of letting them run to completion server-side:
// scrubbing with arrows on used to stack abandoned /best-moves calls on the (2-process) engine
// pool, starving other requests — including the app-mode heartbeat, which read as a closed tab.
let searchAbort = null;
const SEARCH_DEPTHS = [14, 18, 22]; // escalating precision; arrows update after each
const SEARCH_MAX_MS = 5000; // stop deepening after this, even if more depth is available
const SEARCH_DEBOUNCE_MS = 120; // coalesce rapid navigation before hitting the engine
let evalShapes = []; // extra board shapes from the last /api/evaluate (e.g. red refutation arrow)
// Chat context: always the position BEFORE the move in question + that move's SAN, so Claude
// can ground "why is this bad?" on the exact move regardless of timeline vs. explore mode.
let chatFen = null;
let chatMove = null;
let chatSession = null; // claude -p session id, threaded across questions
let chatGen = 0; // bumped on each game open; invalidates an in-flight restoreChat for the old game

// History panel + progressive (navigate-while-analyzing) open.
let analyzing = false; // true during phase 1: provisional PGN timeline, no engine evals yet
let historyMode = "normal"; // "normal" (local games) | "lichess" | "chesscom" | "paste"
let myPlayerId = ""; // configured user's id, for inferring side on lichess lookups
let pollTimer = null; // analysis-status poller
let batchInfo = null; // {total, self_handle, lastDone} while a multi-game upload is analyzing
// Remote tabs (Lichess / Chess.com) share one loader; per-provider paging + shown handle.
const remoteCount = { lichess: 5, chesscom: 5 }; // how many recent games to show ("Load more" grows it)
const remoteUser = { lichess: "", chesscom: "" }; // the handle currently shown (for "Load more")
const LICHESS_PAGE = 5; // initial count + how many more each "Load more"
// "My games": /api/history returns every analysed game; we render them in pages (inside the
// fixed-height scroll box) so the list starts short and grows on "Show more", not the page.
let historyGames = []; // all rows from the last /api/history fetch
let historyCount = 10; // how many to show now ("Show more" grows it)
let unreviewedOnly = false; // "Unreviewed only" checkbox in the games list
const HISTORY_PAGE = 10; // initial count + how many more each "Show more"
// Puzzle trainer: when non-null the board is a "solve your own mistake" drill, not a game review.
// { list:[puzzle...], idx, filter:{motif}, tries, revealed, solved }. See the puzzle section below.
let puzzle = null;
let preDrillMeta = ""; // #game-meta text from before the drill, restored on exit
let preDrillOrient = null; // board orientation from before the drill (puzzles flip to the solver)

// App mode (double-click launcher): on open, auto-load the user's most recent game.
// `appUsername` is the Lichess handle (config.LICHESS_USERNAME); it drives "open my latest game"
// and the Lichess panel placeholder. `chesscomUsername` is the configured chess.com handle — it
// drives the automatic chess.com sync on launch (new games are fetched + analyzed into My games)
// and the chess.com autoload for users without a Lichess handle.
let appMode = false;
let appUsername = "";
let chesscomUsername = "";
// Auto-sync the configured chess.com user's newest games on launch (a Settings option, default
// on). The server gates it too (POST /sync/chesscom body.auto honours the setting); this flag
// just skips the pointless call client-side when it's off.
let chesscomSync = true;
// On-demand Claude-written end-of-game summary: generated when the user presses the button, or
// automatically per game when `coachAiAuto` is on (a Settings option). `coachAiToken` invalidates
// an in-flight request when a new game is opened so a stale summary never lands on the wrong game.
let coachAiAuto = false;
let coachAiToken = 0;
// Whether chat questions inject the cross-game coaching profile (a Settings option, default on).
let personalizeHistory = true;

const $ = (id) => document.getElementById(id);
const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));

// --- chess helpers -------------------------------------------------------
function computeDests() {
  const dests = new Map();
  for (const m of chess.moves({ verbose: true })) {
    if (!dests.has(m.from)) dests.set(m.from, []);
    dests.get(m.from).push(m.to);
  }
  return dests;
}
function turnColor() {
  return chess.turn() === "w" ? "white" : "black";
}
function isPromotion(from, to) {
  return chess
    .moves({ verbose: true })
    .some((m) => m.from === from && m.to === to && m.flags.includes("p"));
}
function samePosition(fenA, fenB) {
  // compare board + side-to-move + castling + ep, ignore clocks
  return fenA.split(" ").slice(0, 4).join(" ") === fenB.split(" ").slice(0, 4).join(" ");
}
function pieceGlyph(san) {
  if (san.startsWith("O-O")) return "♚";
  return { N: "♞", B: "♝", R: "♜", Q: "♛", K: "♚" }[san[0]] || "♟";
}

// --- board rendering -----------------------------------------------------
function renderBoard() {
  const color = turnColor();
  ground.set({
    fen: chess.fen(),
    orientation: orient,
    turnColor: color,
    check: chess.inCheck(),
    movable: { color, dests: computeDests(), free: false, showDests: true },
  });
  drawArrows();
}

function arrowShape(uci, brush) {
  return { orig: uci.slice(0, 2), dest: uci.slice(2, 4), brush };
}
// True when we're parked on a selected mistake's anchor: the position BEFORE your move, with you
// to move — so the best-move arrows and anything you try are for YOUR side. Free browsing sits
// AFTER the last move instead (upstream fix, ported).
function atMistakeAnchor() {
  return currentMistake >= 0 && cur === anchorNode;
}
// The timeline index of the move "under review" at the cursor: at a mistake anchor it's this
// node's own OUTGOING move (you're before it, about to play); while browsing it's the move that
// just landed us here (cur - 1). -1 at the very start (no move to show).
function reviewedMoveNode() {
  return atMistakeAnchor() ? cur : cur - 1;
}
// That move's timeline node (or null). Every "which move is this?" site goes through here so the
// convention lives in one place. Note its `fen` is the position the move was played FROM.
function reviewedNode() {
  const i = reviewedMoveNode();
  return i >= 0 ? timeline[i] || null : null;
}
function drawArrows() {
  const shapes = [];
  // The move under review, as a dark arrow — "here's what was played", not a judgement. At a
  // mistake anchor that's this node's OUTGOING move (the board sits BEFORE it, so the green
  // best-move arrow is for your side and grey-vs-green reads as played-vs-better); while browsing
  // it's the move that just completed. Suppressed when the engine confirmed it WAS the best move:
  // then the single green arrow on the same squares says "played + best" at a glance.
  const done = reviewedNode();
  if (!exploring && !analyzing && done && done.move_uci && !(bestArrowOn && playedWasBest)) {
    shapes.push(arrowShape(done.move_uci, "grey"));
  }
  if (bestArrowOn) for (const a of bestArrows) shapes.push(a);
  if (threatArrowOn) for (const a of threatArrows) shapes.push(a);
  for (const s of evalShapes) shapes.push(s);
  // autoShapes (not setShapes): app-managed annotations that survive piece press/drag and
  // only change when we redraw — so the played-move arrow stays until you actually move.
  ground.setAutoShapes(shapes);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Map top engine moves → arrows. The best move is a bold arrow; alternatives are
// clearly thinner (with proportionally smaller heads, since chessground scales the arrowhead
// with stroke width) so the recommendation stands out at a glance.
function movesToArrows(moves, brush = "green", boldWidth = 13) {
  if (!moves.length) return [];
  const best = moves[0].win_percent;
  const out = [];
  for (let i = 0; i < moves.length; i++) {
    const delta = best - moves[i].win_percent;
    if (i > 0 && delta > 12) break; // only surface genuinely good alternatives
    // best = bold; alternatives start much thinner (≤7) and taper with how much worse.
    const lineWidth = i === 0 ? boldWidth : Math.max(4, 7 - delta);
    out.push({
      orig: moves[i].uci.slice(0, 2),
      dest: moves[i].uci.slice(2, 4),
      brush,
      modifiers: { lineWidth },
    });
  }
  return out;
}

// Refresh the engine-driven arrows (best moves + threats) for the current position. Bumps
// searchGen so any in-flight search for a previous position cancels itself; each enabled
// arrow kind then fetches independently.
function refreshBestMoves() {
  if (puzzle) return; // trainer: engine arrows would draw the solution — the drill owns the board
  searchGen += 1; // cancel any in-flight search
  if (searchAbort) searchAbort.abort(); // stop its request chain (no further depths get sent)
  searchAbort = new AbortController();
  bestArrows = [];
  threatArrows = [];
  playedWasBest = false;
  drawArrows();
  const myGen = searchGen;
  // Best-move arrows grade the move under review, so they search the position it was played FROM
  // ("what should have been played"). At a mistake anchor that IS the current position (the board
  // sits before your move); while browsing it's the previous node's fen. At the start position,
  // or while exploring your own lines, there's no reviewed move — then they show the best move to
  // play now.
  const done = !exploring ? reviewedNode() : null;
  if (bestArrowOn) {
    deepenBestMoves(done && done.fen ? done.fen : chess.fen(), myGen, done ? done.move_uci : null);
  }
  // Threat arrows show what the side that made the reviewed move THREATENS NEXT (a null-move
  // search on the position AFTER that move) — e.g. a knight stepping into forking range gets a
  // yellow arrow on the fork. At a mistake anchor the board sits BEFORE the move, so the
  // after-position is the next timeline node, not the board; everywhere else they coincide.
  let threatFen = chess.fen();
  if (!exploring && atMistakeAnchor() && timeline[cur + 1] && timeline[cur + 1].fen) {
    threatFen = timeline[cur + 1].fen;
  }
  if (threatArrowOn) fetchThreats(threatFen, myGen);
}

// Run an escalating-depth best-move search; cancels itself on any position change (searchGen)
// and stops after SEARCH_MAX_MS. `playedUci` is the move actually played from `fen` (if any):
// when it matches the engine's best, the played move gets the single bold green arrow and the
// dark played-move arrow is dropped (see drawArrows).
async function deepenBestMoves(fen, myGen, playedUci) {
  const signal = searchAbort && searchAbort.signal; // this generation's abort handle
  await sleep(SEARCH_DEBOUNCE_MS); // coalesce rapid arrow-key scrubbing
  if (myGen !== searchGen) return;
  const t0 = performance.now();
  for (const depth of SEARCH_DEPTHS) {
    if (myGen !== searchGen) return;
    let res;
    try {
      res = await fetch("/api/best-moves", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fen, depth, multipv: 3 }),
        signal,
      }).then((r) => r.json());
    } catch (_) {
      return; // aborted (superseded) or network error — either way this search is done
    }
    if (myGen !== searchGen) return; // superseded while the engine was thinking
    if (res && res.moves && res.moves.length) {
      playedWasBest = playedUci != null && res.moves[0].uci === playedUci;
      // Played the best move: one green arrow over it says played + best; alternatives would
      // only clutter a move that needs no correcting.
      bestArrows = movesToArrows(playedWasBest ? res.moves.slice(0, 1) : res.moves);
      drawArrows();
    }
    if (performance.now() - t0 > SEARCH_MAX_MS) break; // time cap
  }
}

// One fixed-depth null-move probe: what does the side that just moved threaten to play next?
// Drawn as yellow arrows, slightly thinner than the green best-move arrows.
async function fetchThreats(fen, myGen) {
  const signal = searchAbort && searchAbort.signal; // this generation's abort handle
  await sleep(SEARCH_DEBOUNCE_MS);
  if (myGen !== searchGen) return;
  let res;
  try {
    res = await fetch("/api/threats", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fen, depth: THREAT_DEPTH, multipv: 3 }),
      signal,
    }).then((r) => r.json());
  } catch (_) {
    return; // aborted (superseded) or network error — either way this probe is done
  }
  if (myGen !== searchGen) return;
  if (res && res.moves) {
    threatArrows = movesToArrows(res.moves, "yellow", 11);
    drawArrows();
  }
}

// The eval bar matches board orientation: the side at the BOTTOM of the board fills from
// the bottom. White-at-bottom (reviewing white) → white fills up; black-at-bottom → black.
function applyEvalBarTheme() {
  const light = "#f0f0f0";
  const dark = "#2b2a27";
  const fill = $("evalbar-fill");
  const bar = $("evalbar");
  if (orient === "white") {
    fill.style.background = light;
    bar.style.background = dark;
  } else {
    fill.style.background = dark;
    bar.style.background = light;
  }
}
function setEvalBar(winWhite) {
  const w = winWhite == null ? 50 : winWhite; // phase-1 (no eval yet) -> neutral
  const bottomShare = orient === "white" ? w : 100 - w;
  $("evalbar-fill").style.height = `${clamp(bottomShare, 0, 100)}%`;
}

// --- verdict / status ----------------------------------------------------
function renderVerdict(payload) {
  if (!payload) return void ($("verdict").innerHTML = "");
  if (payload.error) return void ($("verdict").innerHTML = `<span class="line">${payload.error}</span>`);
  const m = payload.move;
  const refute = m.refutation_line_san.slice(0, 6).join(" ");
  const better = m.is_engine_best ? "Engine's top choice." : `Best was <b>${m.better_move_san}</b>.`;
  // "best" classification = within BEST_EPS of the top move. If it's NOT literally the engine's
  // top choice, show it as "good" so the badge doesn't contradict the "Best was …" text.
  const label = m.classification === "best" && !m.is_engine_best ? "good" : m.classification;
  $("verdict").innerHTML =
    `<span class="tag ${label}">${label}</span>` +
    `<b>${m.move_san}</b> — win ${m.win_before}% → ${m.win_after}% ` +
    `(swing ${m.win_swing}, eval ${m.eval_after}). ${better}` +
    (refute ? `<div class="line">Reply: ${refute}</div>` : "");
}

function nodeLabel(i) {
  const n = timeline[i];
  if (!n || i === 0) return "the start";
  const prev = timeline[i - 1];
  return `${prev.move_number}${prev.color === "white" ? "." : "…"} ${prev.move_san}`;
}

// Compact verdict for the move just tried in explore mode, shown inline in the status banner.
function exploreVerdictHtml() {
  if (exploreVerdict === "pending") return ` <span class="line">evaluating…</span>`;
  if (!exploreVerdict) return "";
  if (exploreVerdict.error) return ` <span class="line">couldn't evaluate that move</span>`;
  const m = exploreVerdict;
  // Mirror renderVerdict: a "best" that isn't literally the engine's #1 reads as "good".
  const label = m.classification === "best" && !m.is_engine_best ? "good" : m.classification;
  return (
    ` <span class="tag ${label}">${label}</span>` +
    `<b>${escapeHtml(m.move_san)}</b> — win ${m.win_before}% → ${m.win_after}%` +
    (m.is_engine_best ? "" : ` · best was <b>${escapeHtml(m.better_move_san || "")}</b>`)
  );
}

function updateStatus() {
  const el = $("status");
  if (exploring) {
    el.className = "status away";
    el.innerHTML =
      `🔍 Exploring a variation.${exploreVerdictHtml()} ` +
      `<button id="ret">Back to review move</button>`;
    $("ret").onclick = returnToReview;
  } else if (cur !== anchorNode) {
    el.className = "status away";
    el.innerHTML = `Viewing ${nodeLabel(cur)} — not the review move. <button id="ret">Back to review move</button>`;
    $("ret").onclick = returnToReview;
  } else {
    el.className = "status";
    const done = reviewedNode(); // grade the move under review (see reviewedMoveNode)
    const g = done ? classGlyph(done.classification) : "";
    el.innerHTML = g + escapeHtml(currentPrompt || nodeLabel(cur));
  }
}

// --- navigation ----------------------------------------------------------
function gotoNode(n) {
  exploring = false;
  cur = clamp(n, 0, timeline.length - 1);
  evalShapes = [];
  // chat context: the move under review is the move in question (the mistake's own move at an
  // anchor, else the move that just landed us here) — chatFen is where it was played from.
  const done = reviewedNode();
  if (done && done.move_san) {
    chatFen = done.fen;
    chatMove = done.move_san;
  } else {
    chatFen = timeline[cur] ? timeline[cur].fen : null;
    chatMove = null;
  }
  chess.load(timeline[cur].fen);
  renderBoard();
  setEvalBar(timeline[cur].win_white);
  renderVerdict(null);
  updateStatus();
  updateNav();
  renderGraph();
  highlightCurrentMove();
  refreshBestMoves();
}

function returnToReview() {
  gotoNode(anchorNode);
}

// Flip the board (hotkey `f`). The eval bar + win graph follow `orient`, so flip them too.
function flipBoard() {
  orient = orient === "white" ? "black" : "white";
  applyEvalBarTheme();
  renderBoard();
  setEvalBar(timeline[cur] ? timeline[cur].win_white : 50);
  renderGraph();
}

// Toggle the "Show best move" arrows from the keyboard (hotkey `l`), keeping the checkbox in sync.
function toggleBestArrows() {
  const box = $("best-toggle");
  box.checked = !box.checked;
  bestArrowOn = bestArrowWanted = box.checked;
  refreshBestMoves();
}

// Toggle the yellow "Show threats" arrows from the keyboard (hotkey `t`), keeping the checkbox in sync.
function toggleThreatArrows() {
  const box = $("threat-toggle");
  box.checked = !box.checked;
  threatArrowOn = threatArrowWanted = box.checked;
  refreshBestMoves();
}

function stepBack() {
  if (exploring) undoOne();
  else if (cur > 0) gotoNode(cur - 1);
}
function stepForward() {
  if (!exploring && cur < timeline.length - 1) gotoNode(cur + 1);
}

function undoOne() {
  chess.undo();
  if (samePosition(chess.fen(), timeline[exploreBaseNode].fen)) {
    gotoNode(exploreBaseNode); // rejoined the game line
    return;
  }
  chatFen = chess.fen(); // backed up mid-line: ask about the position, no single move
  chatMove = null;
  exploreVerdict = null; // no specific move under judgement at the backed-up position
  renderBoard();
  renderVerdict(null);
  updateStatus();
  renderGraph();
  syncExplore(); // refresh the eval bar for the new explored position
  refreshBestMoves(); // and the best-move arrows
}

async function syncExplore() {
  try {
    const info = await fetch("/api/best-move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fen: chess.fen() }),
    }).then((r) => r.json());
    setEvalBar(info.side_to_move === "white" ? info.win_percent : 100 - info.win_percent);
  } catch (_) {}
}

// --- user moves ----------------------------------------------------------
async function onUserMove(orig, dest) {
  if (puzzle) return onPuzzleMove(orig, dest); // puzzle trainer owns the board while active
  const moverColor = turnColor();
  const fenBefore = chess.fen();
  const promo = isPromotion(orig, dest) ? "q" : undefined;
  const uci = orig + dest + (promo ?? "");

  // Following the actual game move while on the timeline → just advance.
  if (!exploring && timeline[cur] && timeline[cur].move_uci === uci) {
    const fm = chess.move({ from: orig, to: dest, promotion: promo });
    chatFen = fenBefore;
    chatMove = (fm && fm.san) || null;
    cur += 1;
    renderBoard();
    setEvalBar(timeline[cur].win_white);
    renderVerdict(null);
    updateStatus();
    updateNav();
    renderGraph();
    highlightCurrentMove();
    refreshBestMoves();
    return;
  }

  // Otherwise we're exploring a variation.
  if (!exploring) {
    exploring = true;
    exploreBaseNode = cur;
  }
  const moveObj = chess.move({ from: orig, to: dest, promotion: promo });
  chatFen = fenBefore; // position before the move in question (consistent in explore mode)
  chatMove = (moveObj && moveObj.san) || null;
  evalShapes = [];
  exploreVerdict = "pending"; // banner shows "evaluating…" until the engine replies
  renderBoard();
  updateStatus();
  renderGraph();
  refreshBestMoves(); // live best-move arrows for the new position

  $("verdict").innerHTML = `<span class="line">Evaluating…</span>`;
  let res;
  try {
    const r = await fetch("/api/evaluate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fen: fenBefore, move: uci }),
    });
    if (!r.ok) throw new Error(`engine error (${r.status})`);
    res = await r.json();
  } catch (err) {
    // Never leave the verdict stuck on "Evaluating…": surface the failure so the user
    // can retry instead of thinking the board froze.
    exploreVerdict = { error: true };
    updateStatus();
    renderVerdict({ error: "Couldn't evaluate that move — the engine may be busy or restarting. Try again." });
    return;
  }
  exploreVerdict = res.move || (res.error ? { error: true } : null);
  updateStatus(); // surface good/mistake/blunder in the always-visible banner under the board
  renderVerdict(res);
  if (res.move) {
    setEvalBar(moverColor === "white" ? res.move.win_after : 100 - res.move.win_after);
    evalShapes = res.shapes || []; // red refutation arrow drawn on the resulting position
    drawArrows();
  }
}

// --- win graph -----------------------------------------------------------
const GW = 1000;
const GH = 100;

function renderGraph() {
  const svg = $("graph");
  const n = timeline.length;
  if (n < 2) {
    svg.innerHTML = "";
    return;
  }
  svg.setAttribute("viewBox", `0 0 ${GW} ${GH}`);
  const x = (i) => (i / (n - 1)) * GW;
  const y = (w) => GH - (w / 100) * GH;
  // Plot from the reviewed player's perspective, matching the eval bar: the filled area
  // grows from the bottom as YOUR side does better, so for black it reads black-on-bottom.
  // During phase-1 (analysing) nodes have no win_white yet -> treat as 50 (flat baseline).
  const hasEval = timeline.some((nd) => nd.win_white != null);
  const val = (nd) => {
    const w = nd.win_white == null ? 50 : nd.win_white;
    return orient === "white" ? w : 100 - w;
  };

  // Two-tone fill split at the eval curve, mirroring the eval bar: each side keeps its own
  // colour (light = White, dark = Black) and the reviewed player's side sits on the bottom.
  const pts = timeline.map((nd, i) => `${x(i).toFixed(1)},${y(val(nd)).toFixed(1)}`).join(" L");
  const belowArea = `M0,${GH} L${pts} L${GW},${GH} Z`; // bottom = the player's side
  const aboveArea = `M0,0 L${pts} L${GW},0 Z`; // top = the opponent's side
  const LIGHT = "rgba(236,234,228,0.22)"; // White
  const DARK = "rgba(0,0,0,0.45)"; // Black
  const bottomFill = orient === "white" ? LIGHT : DARK;
  const topFill = orient === "white" ? DARK : LIGHT;

  const line = timeline
    .map((nd, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(val(nd)).toFixed(1)}`)
    .join(" ");

  const flagged = timeline.filter((nd) => nd.mistake_index != null);
  const mistakeDots = flagged
    .map(
      (nd) =>
        `<circle cx="${x(nd.node).toFixed(1)}" cy="${y(val(nd)).toFixed(1)}" r="3" ` +
        `fill="${classColor(nd.classification)}" vector-effect="non-scaling-stroke"/>`
    )
    .join("");

  // Transparent, full-height click targets over each mistake (a ply-wide band) so clicking a dot
  // reliably opens it via real SVG hit-testing — no fragile pixel math — while clicks elsewhere on
  // the graph hit nothing. `data-mi` is the index into the mistakes array (see onGraphClick).
  const half = GW / (n - 1) / 2;
  const mistakeHits = flagged
    .map(
      (nd) =>
        `<rect class="mdot-hit" data-mi="${nd.mistake_index}" pointer-events="all" ` +
        `x="${(x(nd.node) - half).toFixed(1)}" y="0" width="${(half * 2).toFixed(1)}" ` +
        `height="${GH}" fill="transparent"/>`
    )
    .join("");

  const cx = x(cur).toFixed(1);
  const cy = y(val(timeline[cur])).toFixed(1);
  const marker =
    `<line x1="${cx}" y1="0" x2="${cx}" y2="${GH}" stroke="#629924" stroke-width="1" vector-effect="non-scaling-stroke"/>` +
    `<circle cx="${cx}" cy="${cy}" r="4" fill="#629924" vector-effect="non-scaling-stroke"/>`;

  const analyzingNote = hasEval
    ? ""
    : `<text x="${GW / 2}" y="${GH / 2 - 4}" fill="#9c9890" font-size="9" text-anchor="middle" ` +
      `vector-effect="non-scaling-stroke">analyzing… moves are navigable now</text>`;

  svg.innerHTML =
    `<rect x="0" y="0" width="${GW}" height="${GH}" fill="#14130f"/>` +
    `<path d="${aboveArea}" fill="${topFill}"/>` +
    `<path d="${belowArea}" fill="${bottomFill}"/>` +
    `<line x1="0" y1="${GH / 2}" x2="${GW}" y2="${GH / 2}" stroke="#4a4843" stroke-width="1" stroke-dasharray="4 4" vector-effect="non-scaling-stroke"/>` +
    (hasEval
      ? `<path d="${line}" fill="none" stroke="#e8e6e3" stroke-width="1.5" vector-effect="non-scaling-stroke"/>`
      : "") +
    mistakeDots +
    marker +
    analyzingNote +
    mistakeHits; // last = on top, so the transparent bands reliably catch clicks
}

function classColor(cls) {
  return (
    { inaccuracy: "#e0a800", mistake: "#e08000", blunder: "#dd3333" }[cls] || "#629924"
  );
}

// A small graded badge for a move's classification (only the reviewed player's moves carry one).
// `good` and opponent moves (null) get no glyph so the notation stays readable.
const GLYPHS = { blunder: "??", mistake: "?", inaccuracy: "?!", best: "✓" };
function classGlyph(cls) {
  const g = GLYPHS[cls];
  return g ? `<span class="glyph ${cls}">${g}</span>` : "";
}

// Clicking a flagged-mistake dot opens that mistake (same as the mistakes tab, via selectMistake) —
// detected by real SVG hit-testing of the transparent bands drawn in renderGraph (robust to the
// stretched viewBox). Clicking anywhere else on the graph scrubs to that ply (plain gotoNode jump).
function onGraphClick(ev) {
  const target = ev.target && ev.target.closest && ev.target.closest("[data-mi]");
  if (target) {
    selectMistake(Number(target.getAttribute("data-mi")));
    return;
  }
  const n = timeline.length;
  if (n < 2) return;
  const rect = $("graph").getBoundingClientRect();
  const frac = (ev.clientX - rect.left) / rect.width;
  gotoNode(Math.round(frac * (n - 1))); // gotoNode clamps to a valid node
}

// --- mistakes list -------------------------------------------------------
function renderMistakeList() {
  const ol = $("mistakes");
  ol.innerHTML = "";
  if (analyzing && !mistakes.length) {
    const li = document.createElement("li");
    li.className = "ph";
    li.textContent = "Analyzing… mistakes will appear here when the engine finishes.";
    ol.appendChild(li);
    return;
  }
  mistakes.forEach((m, i) => {
    const li = document.createElement("li");
    li.dataset.index = i;
    const num = `${m.move_number}${m.color === "white" ? "." : "…"}`;
    li.innerHTML =
      `<span class="move"><span class="dot ${m.classification}"></span>` +
      `<span class="piece-glyph">${pieceGlyph(m.move_san)}</span>${num} ${m.move_san}</span>` +
      `<span class="muted">${m.classification} −${m.win_swing}</span>`;
    li.addEventListener("click", () => selectMistake(i));
    ol.appendChild(li);
  });
}

async function selectMistake(i) {
  const myGen = chatGen;
  const pos = await fetch(`/api/position/${i}`).then((r) => r.json());
  if (myGen !== chatGen) return; // a different game opened while we were fetching
  currentMistake = i;
  currentPrompt = pos.error ? "" : pos.prompt;
  // Land on the position BEFORE the mistake (you're on the move) so the engine's best-move arrow
  // and any move you try are for YOUR side; the grey arrow still shows the move you actually
  // played (upstream fix — previously the board sat after the move, so trying an alternative
  // moved the opponent's pieces).
  anchorNode = mistakes[i].node_index;
  [...$("mistakes").children].forEach((li) =>
    li.classList.toggle("active", Number(li.dataset.index) === i)
  );
  gotoNode(anchorNode);
  $("comment").textContent = mistakes[i].comment || "";
}

// --- scoreboard (game report header) -------------------------------------
// Headline stats for the reviewed side, derived entirely from the /session payload: opening,
// per-side accuracy, and counts of blunders / mistakes / inaccuracies (the `mistakes` array is
// exactly the reviewed player's flagged moves). Clicking a count chip jumps to the first of that
// class. Rendered from applySession (phase-2), so it only ever shows real numbers.
function renderScoreboard(session) {
  const board = $("scoreboard");
  if (!board) return;
  const reviewed = session.player === "black" ? "black" : "white";
  const sideLabel = reviewed === "white" ? "White" : "Black";
  const myAcc = reviewed === "white" ? session.accuracy_white : session.accuracy_black;
  const oppAcc = reviewed === "white" ? session.accuracy_black : session.accuracy_white;
  const counts = { blunder: 0, mistake: 0, inaccuracy: 0 };
  (session.mistakes || []).forEach((m) => {
    if (counts[m.classification] != null) counts[m.classification] += 1;
  });
  board.innerHTML =
    `<div class="sb-opening" title="Opening">${escapeHtml(session.opening || "—")}</div>` +
    `<div class="sb-acc">` +
    `<span class="sb-acc-main"><b>${myAcc}</b><span class="sb-acc-lbl">accuracy (${sideLabel})</span></span>` +
    `<span class="sb-acc-opp">opponent ${oppAcc}</span>` +
    `</div>` +
    `<div class="sb-counts">` +
    scoreboardChip("blunder", counts.blunder, "Blunders") +
    scoreboardChip("mistake", counts.mistake, "Mistakes") +
    scoreboardChip("inaccuracy", counts.inaccuracy, "Inaccuracies") +
    `</div>`;
  board.hidden = false;
  board.querySelectorAll(".chip[data-cls]").forEach((el) =>
    el.addEventListener("click", () => jumpToClass(el.dataset.cls))
  );
}

function scoreboardChip(cls, n, label) {
  return (
    `<button type="button" class="chip ${cls}" data-cls="${cls}" title="${label}">` +
    `<span class="chip-n">${n}</span> <span class="chip-lbl">${label}</span></button>`
  );
}

function jumpToClass(cls) {
  const i = mistakes.findIndex((m) => m.classification === cls);
  if (i >= 0) selectMistake(i);
}

// --- move list (clickable notation) --------------------------------------
// The full game as a compact, scrollable two-column notation panel. Built from the same `timeline`
// the graph/arrows use, so it works on the provisional (phase-1) timeline too — glyphs just fill in
// when engine analysis lands. Clicking a flagged move routes through selectMistake (surfacing its
// comment + anchor); any other move is a plain gotoNode jump.
function renderMoveList() {
  const ol = $("movelist");
  if (!ol) return;
  ol.innerHTML = "";
  const plies = timeline.filter((nd) => nd.move_san); // skip the final (terminal) node
  if (!plies.length) return;
  const rows = new Map(); // move_number -> {w, b}
  for (const nd of plies) {
    if (!rows.has(nd.move_number)) rows.set(nd.move_number, { w: null, b: null });
    rows.get(nd.move_number)[nd.color === "white" ? "w" : "b"] = nd;
  }
  for (const [num, pair] of rows) {
    const li = document.createElement("li");
    li.className = "move-row";
    li.innerHTML = `<span class="moveno">${num}.</span>${plyCell(pair.w)}${plyCell(pair.b)}`;
    ol.appendChild(li);
  }
  ol.querySelectorAll(".ply[data-node]").forEach((el) =>
    el.addEventListener("click", () => onMoveClick(Number(el.dataset.node)))
  );
  highlightCurrentMove();
}

function plyCell(nd) {
  if (!nd) return `<span class="ply empty"></span>`;
  return `<span class="ply" data-node="${nd.node}">${classGlyph(nd.classification)}${nd.move_san}</span>`;
}

function onMoveClick(i) {
  const nd = timeline[i];
  if (nd && nd.mistake_index != null) selectMistake(nd.mistake_index);
  else gotoNode(i + 1); // show the position with the clicked move just completed
}

// Highlight the move at the current node and keep it scrolled into view (works in compact mode).
function highlightCurrentMove() {
  const ol = $("movelist");
  if (!ol) return;
  let active = null;
  ol.querySelectorAll(".ply[data-node]").forEach((el) => {
    // Highlight the move under review: the mistake's own move at an anchor, else the last move.
    const on = Number(el.dataset.node) === reviewedMoveNode();
    el.classList.toggle("active", on);
    if (on) active = el;
  });
  if (active) active.scrollIntoView({ block: "nearest" });
}

// Compact (a few rows, scrollable) <-> expanded (whole game). Default is compact.
function toggleMoveList() {
  const ol = $("movelist");
  if (!ol) return;
  const expanded = ol.classList.toggle("expanded");
  ol.classList.toggle("compact", !expanded);
  $("movelist-expand").textContent = expanded ? "Collapse ▴" : "Show all ▾";
  highlightCurrentMove();
}

function updateNav() {
  $("back").disabled = !exploring && cur <= 0;
  $("fwd").disabled = exploring || cur >= timeline.length - 1;
  $("prev-mistake").disabled = currentMistake <= 0;
  $("next-mistake").disabled = currentMistake < 0 || currentMistake >= mistakes.length - 1;
}

// --- chat ("why?") -------------------------------------------------------
const escapeHtml = (s) =>
  s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
// For text interpolated into a double-quoted HTML attribute (title=, data-tip=…), where a stray
// quote would end the attribute — escapeHtml alone doesn't cover that.
const escapeAttr = (s) => escapeHtml(s).replace(/"/g, "&quot;");

// Minimal, safe markdown → HTML: escape first, then bold / italic / code / lists / paragraphs.
function renderMarkdown(text) {
  const lines = escapeHtml(text).split("\n");
  let html = "";
  let inList = false;
  const inline = (s) =>
    s
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  for (const raw of lines) {
    const line = raw.trim();
    const li = line.match(/^[-*]\s+(.*)/);
    if (li) {
      if (!inList) {
        html += "<ul>";
        inList = true;
      }
      html += `<li>${inline(li[1])}</li>`;
    } else {
      if (inList) {
        html += "</ul>";
        inList = false;
      }
      if (line) html += `<p>${inline(line)}</p>`;
    }
  }
  if (inList) html += "</ul>";
  return html || "<p></p>";
}

function addChatMsg(cls, text) {
  const d = document.createElement("div");
  d.className = `chat-msg ${cls}`;
  if (cls === "bot") d.innerHTML = renderMarkdown(text); // only the final answer is markdown
  else d.textContent = text;
  const box = $("chat-messages");
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
  return d;
}

// Repopulate the chat panel from the server's in-memory transcript for the current game, so
// switching to another game and back shows the conversation you'd had. Best-effort: a failure
// just leaves the (already-cleared) panel empty.
async function restoreChat() {
  const myGen = chatGen;
  let hist;
  try {
    hist = await fetch("/api/chat-history").then((r) => r.json());
  } catch (_) {
    return;
  }
  if (myGen !== chatGen) return; // a newer game opened while we were fetching — don't clobber it
  const msgs = (hist && hist.messages) || [];
  $("chat-messages").innerHTML = "";
  for (const m of msgs) addChatMsg(m.role === "bot" ? "bot" : "user", m.text);
  chatSession = (hist && hist.session_id) || null;
}

async function sendChat(ev) {
  ev.preventDefault();
  const input = $("chat-input");
  // Empty box → context-aware default (the placeholder becomes a one-click question).
  const typed = input.value.trim();
  const q =
    typed ||
    (chatMove
      ? `Why is ${chatMove} bad here?`
      : "What's the best move in this position, and why?");
  input.value = "";
  addChatMsg("user", q);
  $("chat-send").disabled = true;
  const pending = addChatMsg("bot pending", "Kibitz is thinking… (a few seconds)");
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: q,
        fen: chess.fen(), // the exact board on screen → "what should I do here?"
        last_move: chatMove, // the move in question → "why is this bad?"
        move_fen: chatFen, // the position that move was played from
        session_id: chatSession,
        use_profile: personalizeHistory, // personalize with cross-game history (Settings toggle)
      }),
    }).then((r) => r.json());
    pending.remove();
    if (res.error) {
      addChatMsg("bot err", res.error);
    } else {
      addChatMsg("bot", res.answer || "(no answer)");
      if (res.session_id) chatSession = res.session_id;
    }
  } catch (e) {
    pending.remove();
    addChatMsg("bot err", "Request failed: " + e);
  } finally {
    $("chat-send").disabled = false;
    input.focus();
  }
}

// --- init ----------------------------------------------------------------
function applySession(session) {
  const sens = session.review_elo
    ? ` · sensitivity ~${Math.round(session.review_elo)} Elo`
    : "";
  $("game-meta").textContent =
    `${session.white} vs ${session.black} — ${session.result} · reviewing ${session.player} ` +
    `(acc W ${session.accuracy_white} / B ${session.accuracy_black}) · ${session.num_mistakes} mistakes${sens}`;
  mistakes = session.mistakes;
  // Remember the game (PGN + names) so "Review other side" can re-open it for the opponent.
  if (session.pgn) currentPgn = session.pgn;
  gameWhite = session.white || gameWhite;
  gameBlack = session.black || gameBlack;
  updateFlipReviewButton();
  renderMistakeList();
  renderScoreboard(session);
  renderCoach(session);
  // NB: prepareCoachAI() is intentionally NOT called here — it's run by the caller AFTER
  // applyTimeline(), so it sees the new game's timeline (applySession runs before applyTimeline).
}

// Free, engine-grounded templated blurb (rides on /api/session) — always shown when present.
function renderCoach(session) {
  const el = $("coach");
  if (!el) return;
  const text = session && session.coach_summary;
  el.textContent = text || "";
  el.hidden = !text;
}

// Claude-written summary (board column, bottom). The card shows its state; the button offers
// on-demand generation. Server caches per game, so re-requests don't spend Claude again.
function showCoachButton(show) {
  const btn = $("coach-ai-btn");
  if (btn) btn.hidden = !show;
}

function setCoachAI(state, text) {
  const el = $("coach-ai");
  if (!el) return;
  if (state === "hidden") {
    el.hidden = true;
    el.className = "coach-ai";
    el.textContent = "";
  } else if (state === "pending") {
    el.hidden = false;
    el.className = "coach-ai pending";
    el.textContent = "Kibitz is writing up a full game summary…";
  } else if (state === "error") {
    el.hidden = false;
    el.className = "coach-ai err";
    el.textContent = text || "Couldn't generate the summary.";
  } else {
    el.hidden = false;
    el.className = "coach-ai";
    // Render bold / italics / lists / paragraphs (same safe markdown as the chat answers). The ⟳
    // button re-runs the summary on demand (spends Claude) — handy if it's a stale saved one.
    el.innerHTML =
      `<span class="coach-ai-tag">AI coach (Kibitz)` +
      `<button id="coach-ai-refresh" class="coach-ai-refresh" type="button" ` +
      `title="Regenerate this summary (uses your Claude subscription)" aria-label="Regenerate summary">⟳</button>` +
      `</span>${renderMarkdown(text)}`;
    const rb = $("coach-ai-refresh");
    if (rb) rb.addEventListener("click", () => fetchCoachAI(true));
  }
}

// Set up the AI-summary UI for the freshly-loaded game: show an already-saved summary outright,
// else auto-generate (if enabled), else just offer the button.
function prepareCoachAI(session) {
  coachAiToken++; // any earlier in-flight request is now stale
  setCoachAI("hidden");
  if (!timeline.length) { showCoachButton(false); return; }
  // Already generated for this game (this session, or restored from the cache on reopen) → show it
  // immediately, no button press and no second Claude call.
  if (session && session.coach_ai_text) {
    setCoachAI("ready", session.coach_ai_text);
    showCoachButton(false);
    return;
  }
  if (coachAiAuto) fetchCoachAI();
  else showCoachButton(true);
}

// Actually request the summary (button press, or the auto path). Always allowed — it only ever
// runs from an explicit user choice, so it spends Claude only when asked.
async function fetchCoachAI(force = false) {
  const tok = ++coachAiToken;
  showCoachButton(false);
  setCoachAI("pending");
  let res;
  try {
    res = await fetch("/api/coach", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force: !!force }),
    }).then((r) => r.json());
  } catch (_) {
    if (tok === coachAiToken) { setCoachAI("error", "Couldn't reach the summary service."); showCoachButton(true); }
    return;
  }
  if (tok !== coachAiToken) return; // a newer game superseded this request
  if (res && res.summary) {
    setCoachAI("ready", res.summary);
  } else {
    setCoachAI(res && res.error ? "error" : "hidden", res && res.error);
    showCoachButton(true); // let the user retry
  }
}

function applyTimeline(tl) {
  timeline = tl.nodes || [];
  player = tl.player || "white";
  orient = player; // orientation follows the reviewed side until the user flips (f)
  applyEvalBarTheme();
  renderMoveList();
}

async function loadAll() {
  // A fresh app session starts the Kibitz chat clean. The transcript lives server-side in memory,
  // so a server that actually restarts is already empty — but if the same long-lived board is reused
  // across app launches (e.g. an MCP-hosted board that didn't exit on tab-close), the old conversation
  // would linger. A brand-new browser session (new launch / new tab) has no sessionStorage flag → wipe;
  // a refresh keeps the flag → preserves the chat (so refresh never loses state).
  try {
    if (!sessionStorage.getItem("chessAppSession")) {
      sessionStorage.setItem("chessAppSession", "1");
      await fetch("/api/chat-reset", { method: "POST" }).catch(() => {});
    }
  } catch (_) {}
  // Identity + app-mode come from the server (settings-backed), so one source of truth.
  try {
    const cfg = await fetch("/api/app-config").then((r) => r.json());
    appMode = !!cfg.app_mode;
    appUsername = (cfg.lichess_username || "").trim();
    chesscomUsername = (cfg.chesscom_username || "").trim();
    chesscomSync = cfg.chesscom_sync !== false; // default on
    coachAiAuto = !!cfg.coach_ai_auto;
    personalizeHistory = cfg.personalize_history !== false; // default on
  } catch (_) {}
  if (appUsername) $("lichess-user").placeholder = appUsername;
  if (chesscomUsername) $("chesscom-user").placeholder = chesscomUsername;
  if (appMode) startHeartbeat(); // so closing this tab quits the standalone app
  startHealthWatcher(); // all modes: never leave the tab silently dead if the server goes away
  checkSetup(); // surface a banner if Stockfish / the claude CLI is missing (fire-and-forget)
  checkUpdates(); // surface an "update available" banner in app mode (fire-and-forget)
  checkOnline(); // surface a banner if there's no internet (network features unavailable)

  // If the user opens a game while this startup load is still in flight (beginProvisional bumps
  // chatGen), abandon it — otherwise the stale session/timeline would clobber the new game's state.
  const myGen = chatGen;
  const session = await fetch("/api/session").then((r) => r.json());
  if (myGen !== chatGen) return;
  if (session.empty) {
    // App mode: try to auto-load the user's most recent Lichess game instead of an empty board.
    if (appMode && (await maybeAutoload())) return;
    $("game-meta").textContent =
      "No game analysed yet — pick one from the Games panel, or run analyze_game.";
    return;
  }
  applySession(session);
  showSidePane("review"); // a game is on the board — surface its analysis, not the games list
  const tl = await fetch("/api/timeline").then((r) => r.json());
  if (myGen !== chatGen) return;
  applyTimeline(tl);
  if (mistakes.length) selectMistake(session.current_index ?? 0);
  else gotoNode(0);
  restoreChat(); // restore any in-memory Q&A for the game already on the board (e.g. after a refresh)
  prepareCoachAI(session); // show a saved AI summary outright, else offer the button
}

// --- setup self-check banner ---------------------------------------------
// Hits /api/doctor (mirrors `python -m server.doctor`). A missing Stockfish is a blocker (red,
// always shown); a missing `claude` CLI only disables the AI chat + coach summary (amber, and
// dismissible — remembered so we don't nag). Fire-and-forget; failures are silent.
async function checkSetup() {
  const banner = $("setup-banner");
  if (!banner) return;
  let checks;
  try {
    checks = (await fetch("/api/doctor").then((r) => r.json())).checks || {};
  } catch (_) {
    return;
  }
  const sf = checks.stockfish || { ok: true };
  const cl = checks.claude || { ok: true };

  if (!sf.ok) {
    showSetupBanner(
      banner,
      true,
      `<b>Stockfish engine not found.</b> ${escapeHtml(sf.hint || "Install Stockfish to analyze games.")}`,
      null
    );
  } else if (!cl.ok && localStorage.getItem("hideClaudeSetupBanner") !== "1") {
    showSetupBanner(
      banner,
      false,
      "<b>AI chat &amp; AI coach summary are off</b> — the <code>claude</code> CLI isn't installed. " +
        "Everything else works. Install it from " +
        '<a href="https://code.claude.com/docs/en/quickstart" target="_blank" rel="noopener">code.claude.com/docs</a>, ' +
        "then run <code>claude login</code>.",
      () => localStorage.setItem("hideClaudeSetupBanner", "1")
    );
  } else {
    banner.hidden = true;
  }
}

function showSetupBanner(banner, isErr, msgHtml, onDismiss) {
  banner.classList.toggle("err", isErr);
  banner.innerHTML =
    `<span class="sb-msg">${msgHtml}</span>` +
    `<button class="sb-x" type="button" aria-label="Dismiss" title="Dismiss">×</button>`;
  banner.querySelector(".sb-x").addEventListener("click", () => {
    banner.hidden = true;
    if (onDismiss) onDismiss();
  });
  banner.hidden = false;
}

// --- offline notice ------------------------------------------------------
// Hits /api/connectivity (cached server-side reachability probe). When there's no internet, the
// network-only features won't work: Lichess game fetch + endgame tablebase always, and the Claude-
// backed AI chat / coach summary too — UNLESS a local LLM is configured (then AI stays available
// offline). Amber, informational, dismissible (remembered for the session so we don't nag).
async function checkOnline() {
  const banner = $("offline-banner");
  if (!banner) return;
  if (sessionStorage.getItem("hideOfflineBanner") === "1") return;
  let info;
  try {
    info = await fetch("/api/connectivity").then((r) => r.json());
  } catch (_) {
    return; // can't even reach our own server — leave it to the page-load failure to be visible
  }
  if (!info || info.online !== false) return; // online (or unknown) — nothing to warn about

  let msg =
    "<b>You're offline.</b> Local analysis (Stockfish) works as normal, but " +
    "<b>Lichess game fetch</b> and the <b>endgame tablebase</b> need internet and won't be available.";
  if (!info.local_llm) {
    // No local LLM, so the AI chat / coach summary go through Claude over the network.
    msg +=
      " The <b>AI chat &amp; coach summary</b> also won't work — they need internet (or a " +
      "local LLM, which you can set up in ⚙ Settings).";
  }
  msg += " Paste or upload a PGN to review a game.";
  showSetupBanner(banner, false, msg, () =>
    sessionStorage.setItem("hideOfflineBanner", "1")
  );
}

// --- update-available banner (app mode only) -----------------------------
// Hits /api/update-check (throttled GitHub release lookup). Non-blocking, dismissible notice when a
// newer release is out: info-blue for minor/patch, red for a major bump. Self-updatable installs
// (git/zip) get a one-click "Update now" (staged, applied by the launcher on the next reopen); the
// read-only .app gets a download link. Dismissal is remembered per version, so a newer release
// re-notifies. Fire-and-forget; failures are silent.
async function checkUpdates() {
  if (!appMode) return; // only nag end-user app launches, never MCP/dev sessions
  const banner = $("update-banner");
  if (!banner) return;
  let info;
  try {
    info = await fetch("/api/update-check").then((r) => r.json());
  } catch (_) {
    return;
  }
  if (!info || !info.update_available || !info.latest) return;
  if (localStorage.getItem("hideUpdateBanner") === info.latest) return; // dismissed this version

  const major = info.severity === "major";
  banner.classList.remove("guide");
  banner.classList.toggle("err", major); // red for major, else the info-blue .update
  banner.classList.toggle("update", !major);
  const v = escapeHtml(info.latest);
  const lead = major ? `<b>Major update v${v} available.</b>` : `<b>Update available — v${v}.</b>`;
  // git/zip self-update in place; the read-only .app needs a guided manual download.
  const how = info.can_self_update
    ? "Click Update now, then reopen the app to finish. Your games &amp; settings are kept."
    : "A new version is ready.";
  const action = info.can_self_update
    ? `<button class="sb-btn" type="button" id="update-now">Update now</button>`
    : `<button class="sb-btn" type="button" id="update-guide">How to update</button>`;

  banner.innerHTML =
    `<span class="sb-msg">${lead} ${how}</span>` +
    action +
    dismissX();
  wireDismiss(banner, info);
  const now = banner.querySelector("#update-now");
  if (now) now.addEventListener("click", () => applyUpdate(now));
  const guide = banner.querySelector("#update-guide");
  if (guide) guide.addEventListener("click", () => showAppUpdateGuide(banner, info));
  banner.hidden = false;
}

function dismissX() {
  return `<button class="sb-x" type="button" aria-label="Dismiss" title="Dismiss">×</button>`;
}
function wireDismiss(banner, info) {
  banner.querySelector(".sb-x").addEventListener("click", () => {
    banner.hidden = true;
    localStorage.setItem("hideUpdateBanner", info.latest); // remember per version
  });
}

// The .app is read-only at runtime, so it can't self-update — expand the banner into step-by-step
// install instructions (incl. the one-time unsigned-app "Open" step) and reassure that data is kept.
function showAppUpdateGuide(banner, info) {
  const url = escapeHtml(info.release_url || "#");
  banner.classList.add("guide"); // full-width stacked layout
  banner.innerHTML =
    `<span class="sb-msg"><b>Update to v${escapeHtml(info.latest)}</b>` +
    `<ol class="sb-steps">` +
    `<li><a href="${url}" target="_blank" rel="noopener">Download the latest version</a> from the Releases page (the <code>…-macos.zip</code>).</li>` +
    `<li>Double-click the downloaded <code>.zip</code> to unzip it.</li>` +
    `<li>Drag <b>Kibitz.app</b> into your <b>Applications</b> folder, replacing the old one. (Any location works — it doesn't have to be Applications.)</li>` +
    `<li>First open: right-click the app → <b>Open</b> → <b>Open</b> (macOS asks once because the app isn't Apple-signed).</li>` +
    `<li>That's it — your games, analysis, and settings carry over automatically.</li>` +
    `</ol></span>` +
    dismissX();
  wireDismiss(banner, info);
}

// Stage a one-click update: POST /api/apply-update writes a sentinel the launcher applies on the
// next start. We can't reliably relaunch from a browser tab, so we just tell the user to reopen.
async function applyUpdate(btn) {
  btn.disabled = true;
  btn.textContent = "Staging…";
  const msg = $("update-banner").querySelector(".sb-msg");
  let res = {};
  try {
    res = await fetch("/api/apply-update", { method: "POST" }).then((r) => r.json());
  } catch (_) {}
  if (res && res.ok) {
    if (msg) msg.innerHTML = "<b>Update staged.</b> Quit and reopen the app to finish updating.";
    btn.textContent = "Quit now";
    btn.disabled = false;
    btn.onclick = () => {
      try {
        navigator.sendBeacon("/api/closing"); // app-liveness then shuts the server down
      } catch (_) {}
      window.close(); // works if the tab was script-opened; otherwise the message guides the user
    };
  } else {
    if (msg)
      msg.innerHTML =
        "<b>Couldn't stage the update.</b> " + escapeHtml((res && res.error) || "Try again later.");
    btn.textContent = "Update now";
    btn.disabled = false;
  }
}

// --- app mode: auto-open the latest Lichess game -------------------------
// App mode: let the server know when this tab is really gone, so it (and its terminal window) can
// quit. The reliable signal is `pagehide` → a close beacon; a slow heartbeat is just a backstop for
// the rare case pagehide never fires. We deliberately do NOT treat "lost focus / backgrounded" as
// closed — background tabs throttle timers, so a short heartbeat would false-quit during use.
let heartbeatTimer = null;
function startHeartbeat() {
  if (heartbeatTimer) return;
  const ping = () => fetch("/api/ping", { method: "POST", keepalive: true }).catch(() => {});
  ping();
  heartbeatTimer = setInterval(ping, 15000); // backstop only; server tolerates minutes of silence
  // Fires on tab close, navigation, and refresh. sendBeacon delivers even as the page unloads.
  window.addEventListener("pagehide", () => {
    try {
      navigator.sendBeacon("/api/closing");
    } catch (_) {}
  });
}

// --- server-stopped overlay + health watcher ------------------------------
// Runs in ALL modes (MCP-hosted board included), not just the standalone app: whatever kills the
// server — a crash, the app-liveness/idle watchdogs, or the user pressing Quit — the tab must say
// so instead of just silently stopping working. Polls a cheap, side-effect-free GET; after a
// couple of consecutive misses it shows the overlay, and hides it again if the server comes back
// (covers a transient blip, or a crash-restart by the .app launcher's supervisor loop).
const HEALTH_POLL_MS = 5000;
const HEALTH_FAIL_THRESHOLD = 2; // ~10s of silence before we say anything
let healthTimer = null;
let healthFails = 0;
let userInitiatedShutdown = false; // set by the Quit button, so the overlay reads "shut down", not "stopped"

function startHealthWatcher() {
  if (healthTimer) return;
  const poll = async () => {
    try {
      const r = await fetch("/api/health", { cache: "no-store" });
      if (!r.ok) throw new Error(String(r.status));
      healthFails = 0;
      if (!userInitiatedShutdown) hideServerStoppedOverlay();
    } catch (_) {
      healthFails += 1;
      if (healthFails >= HEALTH_FAIL_THRESHOLD) showServerStoppedOverlay();
    }
  };
  poll();
  healthTimer = setInterval(poll, HEALTH_POLL_MS);
}

function showServerStoppedOverlay() {
  const overlay = $("server-stopped");
  if (!overlay) return;
  const title = $("server-stopped-title");
  const body = $("server-stopped-body");
  if (userInitiatedShutdown) {
    if (title) title.textContent = "Kibitz has shut down";
    if (body) body.textContent = "You can close this tab.";
  } else {
    if (title) title.textContent = "Kibitz has stopped";
    body.textContent = appMode
      ? "The Kibitz server is no longer running. Reopen the app to continue."
      : "The Kibitz server is no longer running.";
  }
  overlay.hidden = false;
}

function hideServerStoppedOverlay() {
  const overlay = $("server-stopped");
  if (overlay) overlay.hidden = true;
}

// Settings → Quit Kibitz (app mode only): confirm, then POST /api/shutdown. The server responds
// 200 before it actually exits (see routes_board.post_shutdown), so we always get an answer.
async function quitApp() {
  if (!confirm("Quit Kibitz? This stops the local server; reopen the app to use it again.")) return;
  let res = {};
  try {
    res = await fetch("/api/shutdown", { method: "POST" }).then((r) => r.json());
  } catch (_) {
    // The request itself failing usually means the server is already gone — treat it the same.
    userInitiatedShutdown = true;
    showServerStoppedOverlay();
    return;
  }
  if (res && res.ok) {
    userInitiatedShutdown = true;
    showServerStoppedOverlay();
  } else if (res && res.message) {
    alert(res.message);
  }
}

// App-mode empty board: first try the chess.com auto-sync (new games found -> the board shows the
// first of them while the rest analyze); else auto-load the configured user's latest game
// (Lichess, then chess.com); otherwise prompt for a username.
async function maybeAutoload() {
  if (chesscomSync && (await syncChesscom(true))) return true;
  if (appUsername) await autoOpenLatest(appUsername);
  else if (chesscomUsername) await autoOpenLatestChesscom(chesscomUsername);
  else showFirstRun("");
  return true;
}

// Auto-sync: ask the server to fetch the configured chess.com user's newest games and analyze the
// ones history hasn't seen. Returns true when a sync batch was started (the board shows its first
// game). `quiet` marks the automatic launch-time sync: it suppresses the "nothing new" message and
// lets the server honour CHESS_CHESSCOM_SYNC=0 (an explicit ⟳ click always syncs).
async function syncChesscom(quiet) {
  if (!chesscomUsername) return false;
  if (!quiet) $("history-status").textContent = "Syncing chess.com games…";
  let res;
  try {
    res = await fetch("/api/sync/chesscom", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto: !!quiet }),
    }).then((r) => r.json());
  } catch (_) {
    if (!quiet) $("history-status").textContent = "Could not reach Chess.com.";
    return false;
  }
  if (!res || res.error || !res.new_games) {
    if (!quiet)
      $("history-status").textContent =
        res && res.error ? res.error : "chess.com is up to date — no new games.";
    return false;
  }
  const n = res.new_games;
  batchInfo = { total: n, self_handle: res.self_handle, lastDone: -1 };
  $("history-status").textContent =
    `Syncing ${n} new chess.com game${n === 1 ? "" : "s"} → they'll appear in My games.`;
  beginProvisional(
    res.first_pgn,
    res.first_side,
    `Syncing ${n} new chess.com game${n === 1 ? "" : "s"}… you can step through this one now.`
  );
  startPolling();
  return true;
}

// Open the configured chess.com user's most recent game (autoload for chess.com-only users).
async function autoOpenLatestChesscom(username) {
  $("game-meta").textContent = `Loading ${username}'s most recent chess.com game…`;
  const q = new URLSearchParams({ username, max: "1" });
  let data;
  try {
    data = await fetch("/api/chesscom/games?" + q.toString()).then((r) => r.json());
  } catch (_) {
    $("game-meta").textContent = "Could not reach Chess.com — pick a game from the Games panel.";
    return;
  }
  if (data.error || !(data.games || []).length) {
    $("game-meta").textContent = data.error
      ? data.error
      : `No chess.com games found for ${username} — pick one from the Games panel.`;
    return;
  }
  const g = data.games[0];
  openGame(g.pgn, sideForUser(g, username));
}

// Persist both handles server-side (Lichess + chess.com) in one write and reflect them locally.
// Used by the first-run prompt, which offers both fields at once.
async function saveIdentity(lichess, chesscom) {
  appUsername = (lichess || "").trim();
  chesscomUsername = (chesscom || "").trim();
  if (appUsername) $("lichess-user").placeholder = appUsername;
  try {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: appUsername, chesscom_username: chesscomUsername }),
    });
  } catch (_) {}
}

// Persist the configured username server-side (unified identity) and reflect it locally.
async function saveUsername(username) {
  appUsername = (username || "").trim();
  if (appUsername) $("lichess-user").placeholder = appUsername;
  try {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: appUsername }),
    });
  } catch (_) {}
}

// Infer which side `who` played in a Lichess game (same rule as renderHistory's lichess branch).
function sideForUser(game, who) {
  const w = (game.white || "").toLowerCase();
  const b = (game.black || "").toLowerCase();
  const me = (who || "").toLowerCase();
  return me && w === me ? "white" : me && b === me ? "black" : "auto";
}

async function autoOpenLatest(username) {
  $("game-meta").textContent = `Loading ${username}'s most recent Lichess game…`;
  const q = new URLSearchParams({ username, max: "1" });
  let data;
  try {
    data = await fetch("/api/lichess/games?" + q.toString()).then((r) => r.json());
  } catch (_) {
    $("game-meta").textContent = "Could not reach Lichess — pick a game from the Games panel.";
    return;
  }
  if (data.error || !(data.games || []).length) {
    $("game-meta").textContent = data.error
      ? data.error
      : `No Lichess games found for ${username} — pick one from the Games panel.`;
    return;
  }
  const g = data.games[0];
  openGame(g.pgn, sideForUser(g, username));
}

function showFirstRun(defaultUsername) {
  const overlay = $("firstrun");
  if (!overlay) return;
  $("firstrun-user").value = defaultUsername || "";
  $("firstrun-chesscom-user").value = chesscomUsername || "";
  overlay.hidden = false;
  $("firstrun-user").focus();
}

// --- progressive open: navigate the PGN immediately, swap in engine analysis when ready ----
// Build a provisional timeline from a PGN entirely client-side (chess.js), so the board is
// steppable the instant a game is opened — no engine, no win%/classifications yet (those arrive
// in phase 2). Shape matches the server timeline's navigation fields. Throws on an unparseable PGN.
function buildProvisionalTimeline(pgn) {
  const c = new Chess();
  c.loadPgn(pgn);
  const moves = c.history({ verbose: true });
  if (!moves.length) throw new Error("no moves");
  const nodes = moves.map((mv, i) => ({
    node: i,
    fen: mv.before,
    win_white: null,
    color: mv.color === "w" ? "white" : "black",
    move_number: Math.floor(i / 2) + 1,
    ply: i + 1,
    move_san: mv.san,
    move_uci: mv.from + mv.to + (mv.promotion || ""),
    best_uci: null,
    best_san: null,
    classification: null,
    mistake_index: null,
  }));
  const last = moves[moves.length - 1];
  nodes.push({
    node: moves.length,
    fen: last.after,
    win_white: null,
    color: c.turn() === "w" ? "white" : "black",
    move_number: Math.floor(moves.length / 2) + 1,
  });
  return nodes;
}

function setAnalyzingUI(on) {
  $("best-toggle").disabled = on; // engine pool is busy with the sweep
  $("threat-toggle").disabled = on;
  if (on) {
    // Force the arrows off during the sweep (the pool is busy), but remember the user's
    // preference (`...Wanted`) so they come back by themselves when the analysis lands.
    bestArrowOn = false;
    $("best-toggle").checked = false;
    threatArrowOn = false;
    $("threat-toggle").checked = false;
  } else if (bestArrowOn !== bestArrowWanted || threatArrowOn !== threatArrowWanted) {
    bestArrowOn = bestArrowWanted;
    $("best-toggle").checked = bestArrowWanted;
    threatArrowOn = threatArrowWanted;
    $("threat-toggle").checked = threatArrowWanted;
    refreshBestMoves();
  }
  const box = $("analysis-progress");
  if (box) box.hidden = !on;
  if (on) renderProgress(null); // start indeterminate until the first status arrives
}

// Render the sweep progress bar over the win graph. `st` is the /analysis-status payload (or null
// for the initial indeterminate state). We show a measured fill + ETA once the job reports a stable
// per-ply rate (eta_seconds), and an indeterminate shimmer before that (engine pool warming up).
function renderProgress(st) {
  const fill = $("analysis-progress-fill");
  const label = $("analysis-progress-label");
  if (!fill || !label) return;
  // Multi-game batch: prefix the per-game bar with "Game k of N".
  const multi = st && (st.total_games || 1) > 1;
  const prefix = multi ? `Game ${st.current_game} of ${st.total_games} · ` : "";
  const done = st && st.total ? st.done : 0;
  const total = st && st.total ? st.total : 0;
  const eta = st ? st.eta_seconds : null;
  if (!total || eta == null) {
    fill.classList.add("indeterminate");
    label.textContent = `${prefix}Analyzing… estimating time`;
    return;
  }
  fill.classList.remove("indeterminate");
  const pct = Math.max(0, Math.min(100, Math.round((done / total) * 100)));
  fill.style.width = pct + "%";
  const secs = Math.max(1, Math.round(eta));
  label.textContent = `${prefix}Analyzing… ${pct}% · ~${secs}s left`;
}

// Set up the board to navigate a PGN immediately (provisional timeline, no engine yet) and reset
// per-game UI state. Shared by single-game opens and the first game of a batch upload.
function beginProvisional(pgn, side, metaText) {
  if (puzzle) {
    puzzle = null; // opening a game ends the drill; the board belongs to the review again
    renderPuzzlePanel();
  }
  analyzing = true;
  currentMistake = -1;
  anchorNode = 0;
  mistakes = [];
  currentPgn = pgn; // enable "Review other side" immediately; names fill in at phase-2
  gameWhite = "";
  gameBlack = "";
  currentPrompt = "";
  $("comment").textContent = "";
  $("verdict").innerHTML = "";
  setAnalyzingUI(true);
  renderMistakeList();
  $("scoreboard").hidden = true; // stale until the new game's stats land in phase-2
  $("coach").hidden = true;
  coachAiToken++; // invalidate any in-flight AI summary from the previous game
  setCoachAI("hidden");
  showCoachButton(false);
  // Clear the chat panel for the new game; its prior in-memory transcript (if any) is restored
  // in onAnalysisReady once the session is loaded. chatSession is reset so we don't thread the
  // previous game's conversation into this one. Bumping chatGen invalidates any in-flight
  // restoreChat from the game we're leaving, so a late response can't repopulate this panel.
  chatGen++;
  $("chat-messages").innerHTML = "";
  chatSession = null;

  let prov = null;
  try {
    prov = buildProvisionalTimeline(pgn);
  } catch (_) {
    prov = null; // unparseable PGN -> fall back to a blocking spinner (phase-2 still works)
  }
  if (prov && prov.length >= 2) {
    timeline = prov;
    player = side === "white" || side === "black" ? side : "white";
    orient = player;
    applyEvalBarTheme();
    renderMoveList(); // provisional notation: navigable immediately, glyphs fill in later
    gotoNode(0);
    $("game-meta").textContent = metaText || "Analyzing… you can step through the moves now (← / →).";
  } else {
    timeline = [];
    renderMoveList();
    $("game-meta").textContent = "Analyzing…";
  }
  updateFlipReviewButton(); // side is known now; names fill in at phase-2
}

// Show/label the "Review other side" button for the loaded game (hidden until a game is open).
// It re-analyses the SAME game from the opponent's perspective — a separate history record
// (keyed by reviewed_side, so it never collides with the side already analysed).
function updateFlipReviewButton() {
  const btn = $("flip-review");
  if (!btn) return;
  if (!currentPgn || !timeline.length) {
    btn.hidden = true;
    return;
  }
  const otherColor = player === "white" ? "black" : "white";
  const name = otherColor === "white" ? gameWhite : gameBlack;
  const label = name && name !== "?" ? name : otherColor === "white" ? "White" : "Black";
  btn.textContent = `↺ Review ${label}'s side`;
  btn.hidden = false;
}

// Re-open the current game reviewing the opponent (the wrong side may have been auto-picked).
function reviewOtherSide() {
  if (!currentPgn) return;
  openGame(currentPgn, player === "white" ? "black" : "white");
}

async function openGame(pgn, side) {
  batchInfo = null; // a single open is not a batch
  showBoard(); // navigate to the Review view so the opened game is in front
  beginProvisional(pgn, side);
  // AWAIT the POST: jobs.start switches the server's session/job synchronously, so by the time it
  // returns the status reflects THIS game. Polling before that could observe the *previous* game's
  // lingering "ready" and load the wrong session (showing its chat/board). On a cache hit the server
  // is already "ready", so we skip polling and render immediately.
  let st = null;
  try {
    st = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pgn, player: side || "auto" }),
    }).then((r) => r.json());
  } catch (_) {}
  if (st && st.status === "ready") { onAnalysisReady(); return; }
  if (st && st.status === "error") { onAnalysisError(st.error); return; }
  startPolling();
}

// Analyze a multi-game PGN (e.g. a Chess.com export): the backend splits + analyzes each game in
// the background and records them to "My games"; we show the first game while the rest run.
async function openBatch(pgnText, side, username) {
  let res;
  try {
    res = await fetch("/api/analyze-batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pgn: pgnText, player: side || "auto", username: username || "" }),
    }).then((r) => r.json());
  } catch (_) {
    $("history-status").textContent = "Could not start analysis.";
    return;
  }
  if (res.error || !res.total_games) {
    $("history-status").textContent = res.error || "No valid games found in that PGN.";
    return;
  }
  batchInfo = { total: res.total_games, self_handle: res.self_handle, lastDone: -1 };
  const who = res.self_handle ? ` as ${res.self_handle}` : "";
  $("history-status").textContent = `Analyzing ${res.total_games} games${who} → they'll appear in My games.`;
  showBoard(); // navigate to the Review view so the opened game is in front
  beginProvisional(res.first_pgn, res.first_side, `Analyzing game 1 of ${res.total_games}…`);
  startPolling();
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(async () => {
    let st;
    try {
      st = await fetch("/api/analysis-status").then((r) => r.json());
    } catch (_) {
      return;
    }
    // Batch: surface each finished game in "My games" as it lands.
    if (batchInfo && st.done_games != null && st.done_games !== batchInfo.lastDone) {
      batchInfo.lastDone = st.done_games;
      if (historyMode === "normal") loadHistory();
    }
    if (st.status === "ready") {
      stopPolling();
      onAnalysisReady();
    } else if (st.status === "error" && (!batchInfo || (st.total_games || 1) === 1)) {
      // A single-game failure is terminal; in a batch we keep going (error is just the last note).
      stopPolling();
      onAnalysisError(st.error);
    } else {
      renderProgress(st); // pending: advance the bar / ETA
    }
  }, 800);
}
function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

async function onAnalysisReady() {
  // Where the user navigated during phase 1 — only meaningful if a provisional timeline existed
  // for THIS game (when the PGN couldn't be replayed client-side, `cur` is a stale index from the
  // previous game and honouring it would land on an arbitrary move with no mistake selected).
  const prevCur = timeline.length ? cur : 0;
  const session = await fetch("/api/session").then((r) => r.json());
  const tl = await fetch("/api/timeline").then((r) => r.json());
  if (session.empty) return; // superseded/cleared
  analyzing = false;
  setAnalyzingUI(false); // hides the progress bar
  applySession(session);
  applyTimeline(tl);
  if (puzzle) {
    // Mid-drill: don't yank the board over to the finished game — the session/timeline are
    // loaded, so Exit puzzles (or opening it from My games) shows it without a refetch.
    if (historyMode === "normal") loadHistory();
    return;
  }
  // Keep the user where they were navigating; if they hadn't moved, jump to the first mistake.
  if (prevCur === 0 && mistakes.length) selectMistake(session.current_index ?? 0);
  else gotoNode(clamp(prevCur, 0, timeline.length - 1));
  restoreChat(); // repopulate this game's in-memory Q&A (if we've chatted about it this session)
  prepareCoachAI(session); // timeline is set now → show saved summary, auto-generate, or offer button
  if (batchInfo) {
    // Whole upload done: surface every game in "My games".
    const n = batchInfo.total;
    const who = batchInfo.self_handle ? ` as ${batchInfo.self_handle}` : "";
    batchInfo = null;
    activateTab("normal");
    loadHistory(`Analyzed ${n} game${n === 1 ? "" : "s"}${who}. Showing the first below.`);
  } else if (historyMode === "normal") {
    loadHistory(); // the just-analyzed game now appears in the list
  }
}

function onAnalysisError(msg) {
  analyzing = false;
  setAnalyzingUI(false);
  renderGraph();
  $("history-status").textContent = `Analysis failed: ${msg || "unknown error"}`;
  $("game-meta").textContent = "Analysis failed — you can still step through the moves.";
}

// --- history / lichess panel ---------------------------------------------
async function loadHistory(doneMsg) {
  $("history-status").textContent = "Loading…";
  let data;
  try {
    data = await fetch("/api/history").then((r) => r.json());
  } catch (_) {
    $("history-status").textContent = "Could not load history.";
    return;
  }
  myPlayerId = data.player_id || myPlayerId;
  if (myPlayerId) $("lichess-user").placeholder = myPlayerId;
  historyGames = data.games || [];
  historyCount = HISTORY_PAGE; // a fresh fetch resets paging back to the first page
  renderMyGames();
  $("history-status").textContent = historyGames.length ? doneMsg || "" : "No analyzed games yet.";
  scheduleInsights(); // the aggregate reflects whatever just landed in history
}

// Render the current page of "My games" into the (fixed-height, scrollable) list, with a
// "Show more" row when there are extra games beyond what's shown. No refetch — pages a cached list.
function renderMyGames() {
  let games = historyGames;
  if (unreviewedOnly) games = games.filter((g) => !g.reviewed);
  renderHistory(games.slice(0, historyCount), "normal");
  const remaining = games.length - historyCount;
  if (remaining > 0) {
    const li = document.createElement("li");
    li.className = "load-more";
    li.textContent = `Show more (${remaining})`;
    li.addEventListener("click", () => {
      historyCount += HISTORY_PAGE;
      renderMyGames();
    });
    $("history-list").appendChild(li);
  }
}

async function toggleReviewed(g) {
  const next = !g.reviewed;
  try {
    await fetch("/api/reviews", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ game_id: g.game_id, reviewed_side: g.reviewed_side, reviewed: next }),
    });
  } catch (_) {
    return;
  }
  g.reviewed = next; // mutate the cached row so re-render reflects it without a refetch
  renderMyGames();
}

async function editNote(g) {
  const next = window.prompt("Note for this game:", g.note || "");
  if (next === null) return; // cancelled
  try {
    await fetch("/api/reviews", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ game_id: g.game_id, reviewed_side: g.reviewed_side, note: next }),
    });
  } catch (_) {
    return;
  }
  g.note = next;
  renderMyGames();
  if (currentView === "notes") renderNotesView();
}

// The Notes view: every game with a note, newest-first, linking back to open the game.
function renderNotesView() {
  const body = $("notes-body");
  if (!body) return;
  const noted = historyGames.filter((g) => g.note && g.note.trim());
  if (!noted.length) {
    body.innerHTML = `<p class="muted">No notes yet. Mark a game with a note in the Games list.</p>`;
    return;
  }
  const ol = document.createElement("ol");
  ol.className = "notes-list";
  for (const g of noted) {
    const side = g.reviewed_side;
    const opp = side === "white" ? g.black : g.white;
    const li = document.createElement("li");
    li.className = "note-item";
    li.innerHTML =
      `<div class="note-game"><span>${escapeHtml((resultWord(g.player_result) || "vs") + " " + (opp || "?"))}</span>` +
      `<span class="note-meta">${escapeHtml((g.date || "") + " · " + (g.opening || "—"))}</span></div>` +
      `<div class="note-text"></div><div class="note-links"></div>`;
    li.querySelector(".note-text").textContent = g.note;
    const links = li.querySelector(".note-links");
    const open = document.createElement("button");
    open.type = "button";
    open.className = "linklike";
    open.textContent = "Open game →";
    if (g.has_pgn && g.pgn) {
      open.addEventListener("click", () => openGame(g.pgn, side));
    } else {
      open.disabled = true;
      open.title = "No PGN stored for this game — re-analyze it to reopen.";
    }
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "linklike";
    edit.textContent = "Edit";
    edit.addEventListener("click", () => editNote(g));
    links.appendChild(open);
    links.appendChild(edit);
    ol.appendChild(li);
  }
  body.innerHTML = "";
  body.appendChild(ol);
}

// The two remote-games tabs differ only in endpoint, label, whose games "auto" side-inference
// falls back to, and Lichess's "is this account already me?" reflection — one table entry each.
const REMOTE_PROVIDERS = {
  lichess: {
    label: "Lichess",
    endpoint: "/api/lichess/games",
    defaultWho: () => myPlayerId,
    onLoaded: (who) => reflectSetAsMe(who), // is the looked-up account already "me"?
  },
  chesscom: {
    label: "Chess.com",
    endpoint: "/api/chesscom/games",
    defaultWho: () => chesscomUsername,
    onLoaded: null,
  },
};

async function loadRemote(provider, username) {
  const p = REMOTE_PROVIDERS[provider];
  remoteUser[provider] = username;
  $("history-status").textContent = `Fetching from ${p.label}…`;
  const q = new URLSearchParams();
  if (username) q.set("username", username);
  q.set("max", String(remoteCount[provider]));
  let data;
  try {
    data = await fetch(p.endpoint + "?" + q.toString()).then((r) => r.json());
  } catch (_) {
    $("history-status").textContent = `Could not reach ${p.label}.`;
    return;
  }
  if (data.error) {
    $("history-status").textContent = data.error;
    $("history-list").innerHTML = "";
    return;
  }
  const games = data.games || [];
  const who = (username || p.defaultWho() || "").toLowerCase();
  if (p.onLoaded) p.onLoaded(who);
  renderHistory(games, "remote", who);
  $("history-status").textContent = games.length ? "" : "No games found.";
  // While the server keeps returning a full page, there are probably more to fetch.
  if (games.length >= remoteCount[provider]) {
    const li = document.createElement("li");
    li.className = "load-more";
    li.textContent = "Load more";
    li.addEventListener("click", () => {
      remoteCount[provider] += LICHESS_PAGE;
      loadRemote(provider, remoteUser[provider]);
    });
    $("history-list").appendChild(li);
  }
}

const resultClass = (r) => (r === "win" ? "win" : r === "loss" ? "loss" : r === "draw" ? "draw" : "");
const resultWord = (r) => ({ win: "Won", loss: "Lost", draw: "Drew" }[r] || "");

function renderHistory(games, mode, who) {
  const ol = $("history-list");
  ol.innerHTML = "";
  for (const g of games) {
    const li = document.createElement("li");
    let side, title, sub, blunders, disabled, cls;
    if (mode === "normal") {
      side = g.reviewed_side;
      const opp = side === "white" ? g.black : g.white;
      cls = resultClass(g.player_result);
      const acc = g.accuracy != null ? `${g.accuracy}%` : "?";
      title = `${resultWord(g.player_result) || "vs"} ${opp || "?"}`;
      sub = `${g.date || ""} · ${g.opening || "—"} · ${acc} · ${g.speed}`;
      blunders = (g.counts && g.counts.blunder) || 0;
      disabled = !g.has_pgn;
      // Tint games recorded under the configured user ("you") so they stand out from games
      // analysed for someone else (e.g. an opponent-side review, or another account's PGN).
      // The backend computes `is_me` against the CURRENT identity (so a chess.com game recorded
      // before that handle was added to "me" still tints); fall back to the frozen id match.
      if (g.is_me || (myPlayerId && g.player_id && g.player_id === myPlayerId)) cls += " mine";
      cls += g.reviewed ? " reviewed" : " unreviewed";
    } else {
      const w = (g.white || "").toLowerCase();
      const b = (g.black || "").toLowerCase();
      side = who && w === who ? "white" : who && b === who ? "black" : "auto";
      cls = "";
      title = `${g.white || "?"} vs ${g.black || "?"}`;
      sub = `${g.date || ""} · ${g.opening || "—"} · ${g.speed} · ${g.result || ""}`;
      blunders = null;
      disabled = !g.pgn;
    }
    li.className = cls + (disabled ? " disabled" : "");
    li.innerHTML =
      `<div class="h-title"><span>${escapeHtml(title)}</span>` +
      (blunders ? `<span class="h-blunders">●${blunders}</span>` : "") +
      `</div><div class="h-sub">${escapeHtml(sub)}</div>`;
    if (disabled) {
      li.title =
        mode === "normal"
          ? "Can't reopen — this game was analyzed before PGNs were stored. Re-analyze it from Lichess."
          : "No PGN available for this game.";
    } else {
      li.addEventListener("click", () => openGame(g.pgn, side));
    }
    if (mode === "normal") {
      if (g.note) {
        const note = document.createElement("div");
        note.className = "h-note";
        note.textContent = g.note;
        li.appendChild(note);
      }
      const actions = document.createElement("div");
      actions.className = "h-actions";
      const rev = document.createElement("button");
      rev.type = "button";
      rev.className = "h-act" + (g.reviewed ? " on" : "");
      rev.textContent = g.reviewed ? "✓ Reviewed" : "Mark reviewed";
      rev.addEventListener("click", (e) => {
        e.stopPropagation();
        toggleReviewed(g);
      });
      const noteBtn = document.createElement("button");
      noteBtn.type = "button";
      noteBtn.className = "h-act";
      noteBtn.textContent = g.note ? "✎ Edit note" : "＋ Note";
      noteBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        editNote(g);
      });
      actions.appendChild(rev);
      actions.appendChild(noteBtn);
      li.appendChild(actions);
    }
    ol.appendChild(li);
  }
}

// --- insights panel --------------------------------------------------------
// Cross-game themes + stats for the configured user over a chosen time window, aggregated
// server-side from the analyzed-games history (GET /api/insights). Refreshed whenever the
// history list reloads (i.e. as new games are analyzed) and when the period changes.
let insightsDays = 30;
let insightsTimer = null;

// Coalesce refresh bursts into one fetch: during a multi-game sync, every finished game reloads
// the history list, and re-aggregating the whole history once per game would be wasted work.
function scheduleInsights() {
  clearTimeout(insightsTimer);
  insightsTimer = setTimeout(loadInsights, 800);
}

async function loadInsights() {
  const body = $("insights-body");
  if (!body) return;
  let data;
  try {
    data = await fetch(`/api/insights?days=${insightsDays}`).then((r) => r.json());
  } catch (_) {
    body.innerHTML = `<p class="muted">Could not load insights.</p>`;
    return;
  }
  renderInsights(data);
}

// One reusable tooltip for the insights charts: shown near the hovered mark, hidden on leave.
// Marks opt in via data-tip; delegation keeps it one listener however often the pane re-renders.
function wireChartTips(root) {
  const tip = $("chart-tip");
  if (!tip) return;
  root.addEventListener("mousemove", (e) => {
    const mark = e.target.closest("[data-tip]");
    if (!mark) {
      tip.hidden = true;
      return;
    }
    tip.textContent = mark.dataset.tip;
    tip.hidden = false;
    const pane = $("insights").getBoundingClientRect();
    tip.style.left = `${Math.min(e.clientX - pane.left + 12, pane.width - tip.offsetWidth - 8)}px`;
    tip.style.top = `${e.clientY - pane.top - 34}px`;
  });
  root.addEventListener("mouseleave", () => {
    tip.hidden = true;
  });
}

// Accuracy-per-game line (SVG): one series in the accent hue, hairline gridlines, an end dot with
// a surface ring, and an invisible per-point hover band feeding the shared tooltip.
function accuracyTrendSvg(trend) {
  const pts = trend.filter((t) => t.accuracy != null);
  if (pts.length < 2) return "";
  const W = 320;
  const H = 96;
  const PAD = { l: 30, r: 10, t: 8, b: 6 };
  const lo = Math.max(0, Math.min(...pts.map((p) => p.accuracy), 60) - 5);
  const x = (i) => PAD.l + (i * (W - PAD.l - PAD.r)) / (pts.length - 1);
  const y = (a) => PAD.t + (100 - a) * ((H - PAD.t - PAD.b) / (100 - lo));
  const line = pts.map((p, i) => `${x(i).toFixed(1)},${y(p.accuracy).toFixed(1)}`).join(" ");
  const grid = [100, Math.round((100 + lo) / 2), Math.round(lo)]
    .map(
      (v) =>
        `<line x1="${PAD.l}" y1="${y(v)}" x2="${W - PAD.r}" y2="${y(v)}" class="grid"/>` +
        `<text x="${PAD.l - 4}" y="${y(v) + 3}" class="tick">${v}</text>`
    )
    .join("");
  const resultWordLc = { win: "won", loss: "lost", draw: "drew" };
  const hovers = pts
    .map((p, i) => {
      const w = (W - PAD.l - PAD.r) / (pts.length - 1);
      const label = `${p.date || ""} · ${p.accuracy}%` +
        (p.result ? ` · ${resultWordLc[p.result] || p.result}` : "") +
        (p.opening ? ` · ${p.opening}` : "");
      return (
        `<g data-tip="${escapeAttr(label)}">` +
        `<rect x="${(x(i) - w / 2).toFixed(1)}" y="0" width="${w.toFixed(1)}" height="${H}" fill="transparent"/>` +
        `<circle cx="${x(i).toFixed(1)}" cy="${y(p.accuracy).toFixed(1)}" r="3" class="pt"/>` +
        `</g>`
      );
    })
    .join("");
  const last = pts[pts.length - 1];
  return (
    `<svg viewBox="0 0 ${W} ${H}" class="ins-chart" role="img" aria-label="Accuracy per game">` +
    grid +
    `<polyline points="${line}" class="trend"/>` +
    hovers +
    `<circle cx="${x(pts.length - 1)}" cy="${y(last.accuracy)}" r="4" class="end-dot"/>` +
    `<text x="${W - PAD.r}" y="${Math.max(10, y(last.accuracy) - 7)}" class="end-label">${last.accuracy}%</text>` +
    `</svg>`
  );
}

// A labelled horizontal bar row (shared by the weakness + phase charts): name, thin accent bar
// scaled to max, value at the bar end. Direct labels on every row, so color never carries alone.
function barRow(label, value, max, valueText, extra = "") {
  const pct = max > 0 ? Math.max(2, (value / max) * 100) : 0;
  return (
    `<div class="ins-bar-row" ${extra}><span class="ins-bar-label">${escapeHtml(label)}</span>` +
    `<span class="ins-bar-track"><span class="ins-bar-fill" style="width:${pct.toFixed(1)}%"></span></span>` +
    `<span class="ins-bar-val">${escapeHtml(String(valueText))}</span></div>`
  );
}

function renderInsights(d) {
  const body = $("insights-body");
  if (!d || d.error) {
    body.innerHTML = `<p class="muted">${escapeHtml((d && d.error) || "No data.")}</p>`;
    return;
  }
  if (!d.games) {
    body.innerHTML = `<p class="muted">No analyzed games in this period yet — open one from the
      Games tab (or ⟳ Sync) and it will start showing up here.</p>`;
    return;
  }
  const r = d.results || {};
  const mt = d.mistake_totals || {};
  const mpg = d.mistakes_per_game || {};
  const parts = [];

  // KPI row: the three headline numbers, each a stat tile (value + context line).
  parts.push(
    `<div class="ins-tiles">` +
      `<div class="ins-tile"><div class="ins-tile-label">Games</div>` +
      `<div class="ins-tile-value">${d.games}</div>` +
      `<div class="ins-tile-sub">${r.win || 0}W · ${r.draw || 0}D · ${r.loss || 0}L</div></div>` +
      `<div class="ins-tile"><div class="ins-tile-label">Avg accuracy</div>` +
      `<div class="ins-tile-value">${d.avg_accuracy != null ? d.avg_accuracy + "%" : "—"}</div>` +
      `<div class="ins-tile-sub">across the period</div></div>` +
      `<div class="ins-tile"><div class="ins-tile-label">Blunders / game</div>` +
      `<div class="ins-tile-value">${mpg.blunder != null ? mpg.blunder : "—"}</div>` +
      `<div class="ins-tile-sub">${mt.blunder || 0} total</div></div>` +
      `</div>`
  );

  // Accuracy trend: per-game line, oldest → newest.
  const trendSvg = accuracyTrendSvg(d.trend || []);
  if (trendSvg) parts.push(`<h3>Accuracy by game</h3>${trendSvg}`);

  // Mistake severity: one stacked bar (2px surface gaps) + a fully-labelled legend line, so the
  // yellow/orange/red never has to be told apart by hue alone.
  const sevTotal = (mt.inaccuracy || 0) + (mt.mistake || 0) + (mt.blunder || 0);
  if (sevTotal) {
    const seg = (k, cls) =>
      mt[k]
        ? `<span class="sev-seg ${cls}" style="flex-grow:${mt[k]}" data-tip="${mt[k]} ${k === "inaccuracy" ? "inaccuracies" : k + "s"}"></span>`
        : "";
    parts.push(
      `<h3>Mistake severity</h3>` +
        `<div class="sev-bar">${seg("inaccuracy", "inacc")}${seg("mistake", "mist")}${seg("blunder", "blun")}</div>` +
        `<div class="sev-legend">` +
        `<span><i class="sev-dot inacc"></i>${mt.inaccuracy || 0} inaccuracies</span>` +
        `<span><i class="sev-dot mist"></i>${mt.mistake || 0} mistakes</span>` +
        `<span><i class="sev-dot blun"></i>${mt.blunder || 0} blunders</span>` +
        `</div>`
    );
  }

  // Recurring weaknesses: bars by count, each clickable → drills that motif in the Puzzles tab.
  const motifs = (d.top_motifs || []).slice(0, 6);
  if (motifs.length) {
    const max = motifs[0].count;
    parts.push(
      `<h3>Recurring weaknesses <span class="ins-hint">click one to train it</span></h3>` +
        `<div class="ins-bars">` +
        motifs
          .map((m) =>
            barRow(m.label || m.motif, m.count, max, `×${m.count}`,
              `data-motif="${escapeAttr(m.motif)}" role="button" tabindex="0" title="Train ${escapeAttr(m.label || m.motif)} in Puzzles"`)
          )
          .join("") +
        `</div>`
    );
  }

  // Where the win% leaks, by game phase — per game, because the raw window totals (hundreds of
  // summed percentage points) mean nothing to a reader. The weakest phase is named, not colored.
  const pl = d.phase_loss_total || {};
  const perGame = (k) => (pl[k] || 0) / d.games;
  const phases = ["opening", "middlegame", "endgame"].filter((k) => pl[k] != null);
  const plMax = Math.max(...phases.map(perGame), 0);
  if (plMax > 0) {
    parts.push(
      `<h3>Win% lost per game, by phase</h3><div class="ins-bars">` +
        phases.map((k) => barRow(k, perGame(k), plMax, `${perGame(k).toFixed(1)}%`)).join("") +
        `</div>` +
        (d.weakest_phase
          ? `<div class="ins-stat muted">Most of the damage happens in the <b>${escapeHtml(d.weakest_phase)}</b>.</div>`
          : "")
    );
  }

  // Repertoire report: per-color opening tables, plus a "worst openings" callout — replaces the
  // old flat "most played openings" table. Every row is clickable → drills that opening's
  // mistakes in the Puzzles trainer (same click-to-train pattern as the weakness bars above).
  const rep = d.repertoire || {};
  const eco = (o) => escapeAttr(o.eco || "");
  const scoreText = (s) => `${(s && s.win) || 0}-${(s && s.draw) || 0}-${(s && s.loss) || 0}`;
  const worst = (rep.worst || []).filter((o) => o.eco);
  if (worst.length) {
    const max = Math.max(...worst.map((o) => o.opening_loss_per_game), 0);
    parts.push(
      `<h3>Your worst openings <span class="ins-hint">click one to train it</span></h3>` +
        `<div class="ins-bars">` +
        worst
          .map((o) =>
            barRow(
              `${o.opening} (${o.side === "white" ? "White" : "Black"})`,
              o.opening_loss_per_game,
              max,
              `${o.opening_loss_per_game.toFixed(1)}%/g · ${o.games}g`,
              `data-eco="${eco(o)}" role="button" tabindex="0" title="Train ${escapeAttr(o.opening)} in Puzzles"`
            )
          )
          .join("") +
        `</div>`
    );
  }
  const repTable = (rows, title) => {
    const withEco = rows.filter((o) => o.eco);
    return (
      `<h3>${escapeHtml(title)} <span class="ins-hint">click a row to train it</span></h3>` +
      `<table class="ins-table"><thead><tr><th>Opening</th><th>Games</th><th>Score</th>` +
      `<th>Acc</th><th>In book</th><th>Loss/g (opening)</th></tr></thead><tbody>` +
      rows
        .map(
          (o) =>
            `<tr${o.eco ? ` class="ins-row-clickable" data-eco="${eco(o)}" role="button" tabindex="0"` : ""}>` +
            `<td>${escapeHtml(o.opening)}</td><td>${o.games}</td><td>${scoreText(o.score)}</td>` +
            `<td>${o.avg_accuracy != null ? o.avg_accuracy + "%" : "—"}</td>` +
            `<td>${o.book_ply != null ? o.book_ply + " plies" : "—"}</td>` +
            `<td>${o.opening_loss_per_game.toFixed(1)}%</td></tr>`
        )
        .join("") +
      `</tbody></table>`
    );
  };
  if ((rep.white || []).length) parts.push(repTable(rep.white, "As White"));
  if ((rep.black || []).length) parts.push(repTable(rep.black, "As Black"));
  const speeds = (d.by_speed || []).filter((s) => s.games > 0).slice(0, 4);
  if (speeds.length > 1) {
    parts.push(
      `<h3>By time control</h3><table class="ins-table"><thead>` +
        `<tr><th>Mode</th><th>Games</th><th>Acc</th><th>Blun/g</th></tr></thead><tbody>` +
        speeds
          .map(
            (s) =>
              `<tr><td>${escapeHtml(s.speed)}</td><td>${s.games}</td>` +
              `<td>${s.avg_accuracy != null ? s.avg_accuracy + "%" : "—"}</td>` +
              `<td>${s.blunders_per_game != null ? s.blunders_per_game : "—"}</td></tr>`
          )
          .join("") +
        `</tbody></table>`
    );
  }

  body.innerHTML = parts.join("");
  // Weakness bars drill straight into the puzzle trainer for that motif.
  body.querySelectorAll("[data-motif]").forEach((el) => {
    const go = () => startPuzzles(el.dataset.motif, ""); // navigates to the Puzzles view itself
    el.addEventListener("click", go);
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        go();
      }
    });
  });
  // Repertoire rows drill straight into the puzzle trainer for that opening.
  body.querySelectorAll("[data-eco]").forEach((el) => {
    const go = () => startPuzzles("", el.dataset.eco); // navigates to the Puzzles view itself
    el.addEventListener("click", go);
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        go();
      }
    });
  });
  if (!body.dataset.tipsWired) {
    body.dataset.tipsWired = "1";
    wireChartTips(body);
  }
}

// --- app shell: the icon rail routes between full views --------------------------------------
// One view is visible at a time: review (board + games/analysis), puzzles (board + filters),
// insights, settings. The board column is ONE shared element, re-parented into whichever view
// is active (review-board-slot / puzzle-board-slot) so there's a single Chessground instance.
const VIEWS = ["review", "puzzles", "insights", "notes", "settings"];
let currentView = "review";
let lastMainView = "review"; // where Cancel/Save in Settings returns to

function placeBoard(view) {
  const slot = $(view === "puzzles" ? "puzzle-board-slot" : "review-board-slot");
  const boardCol = document.querySelector(".board-col");
  if (!slot || !boardCol || boardCol.parentElement === slot) return;
  slot.appendChild(boardCol);
  // Chessground caches its bounds; re-measure after the move settles in the new layout.
  if (ground) requestAnimationFrame(() => ground.redrawAll());
}

function showView(name) {
  if (!VIEWS.includes(name)) return;
  if (name !== "settings") lastMainView = name;
  currentView = name;
  for (const v of VIEWS) {
    $(`view-${v}`).classList.toggle("active", v === name);
    $(`rail-${v}`).classList.toggle("active", v === name);
  }
  placeBoard(name);
  // Entering a data view refreshes it (cheap: local reads), so it's never stale.
  if (name === "review" && sidePane === "games") setMode(historyMode);
  else if (name === "puzzles") loadPuzzleThemes();
  else if (name === "insights") loadInsights();
  else if (name === "notes") loadHistory().then(() => renderNotesView());
  else if (name === "settings") populateSettings();
}

// Review's right panel: the games list, or the analysis of the open game.
let sidePane = "games";
function showSidePane(pane) {
  sidePane = pane === "review" ? "review" : "games";
  $("side-games").classList.toggle("active", sidePane === "games");
  $("side-review").classList.toggle("active", sidePane === "review");
  $("games-pane").hidden = sidePane !== "games";
  $("review-pane").hidden = sidePane !== "review";
}

// Bring the board + analysis in front (opening a game lands here) — every "open" path calls this.
function showBoard() {
  showView("review");
  showSidePane("review");
}

// Games view: which source is active (My games / Lichess / Chess.com / Paste).
function activateTab(mode) {
  historyMode = mode;
  for (const m of ["normal", "lichess", "chesscom", "paste"]) {
    $(`mode-${m}`).classList.toggle("active", mode === m);
  }
  $("lichess-form").style.display = mode === "lichess" ? "flex" : "none";
  $("chesscom-form").style.display = mode === "chesscom" ? "flex" : "none";
  $("paste-form").style.display = mode === "paste" ? "flex" : "none";
  $("history-list").style.display = mode !== "paste" ? "" : "none";
  $("unreviewed-filter").style.display = mode === "normal" ? "" : "none";
}

// Update the "Set as my account" button to reflect whether `who` (lowercased) is already you.
function reflectSetAsMe(who) {
  const btn = $("set-as-me");
  if (!btn) return;
  const isMe = !!appUsername && appUsername.toLowerCase() === who && !!who;
  btn.disabled = isMe;
  btn.textContent = isMe ? "✓ This is your account" : "Set as my account";
}

// --- puzzle trainer ------------------------------------------------------
// Solve your OWN mistakes: each puzzle sits you in a position you went wrong in and asks for the
// move you missed (the engine's best, from the stored analysis — no new engine work). Themes come
// from your recurring weaknesses (same motifs the Insights panel names), and the whole set can be
// exported to a Lichess study of practice chapters.
let puzzleDays = 0; // time window for themes + puzzles (0 = all history)
let puzzleKinds = ""; // severity filter: "" (all) | "mistake,blunder" | "blunder"
let puzzleOrder = "srs"; // "srs" (due/failed first, server-ordered) | "random" (client-shuffled)
let puzzleEco = ""; // opening filter, set when drilling a repertoire row from Insights

async function loadPuzzleThemes() {
  const wrap = $("puzzle-themes");
  const exportBtn = $("puzzle-export");
  if (!wrap) return;
  wrap.innerHTML = `<span class="muted">Loading your weaknesses…</span>`;
  let data;
  try {
    const q = new URLSearchParams({ days: String(puzzleDays), kinds: puzzleKinds });
    data = await fetch("/api/puzzles/themes?" + q.toString()).then((r) => r.json());
  } catch (_) {
    wrap.innerHTML = `<span class="muted">Couldn't load puzzles.</span>`;
    return;
  }
  const themes = (data && data.themes) || [];
  const total = themes.reduce((n, t) => n + t.count, 0);
  if (!themes.length) {
    wrap.innerHTML = `<span class="muted">No puzzles yet — analyze some of your games first, then
      your blunders and missed tactics become puzzles here.</span>`;
    if (exportBtn) exportBtn.disabled = true;
    return;
  }
  if (exportBtn) exportBtn.disabled = false;
  // "All" chip first (train everything), then one chip per weakness, most common first.
  const chips = [{ motif: "", label: "All weaknesses", count: total }].concat(themes);
  wrap.innerHTML = chips
    .map(
      (t) =>
        `<button type="button" class="puzzle-chip" data-motif="${escapeHtml(t.motif)}">` +
        `${escapeHtml(t.label)} <span class="chip-n">${t.count}</span>` +
        (t.due ? `<span class="chip-due">${t.due} due</span>` : "") +
        `</button>`
    )
    .join("");
  wrap.querySelectorAll(".puzzle-chip").forEach((btn) => {
    btn.addEventListener("click", () => startPuzzles(btn.dataset.motif || "", ""));
  });
}

async function startPuzzles(motif, eco = "") {
  puzzleEco = eco;
  $("puzzles-status").textContent = "Loading puzzles…";
  const q = new URLSearchParams({ days: String(puzzleDays), limit: "60", kinds: puzzleKinds, order: puzzleOrder });
  if (motif) q.set("motif", motif);
  if (puzzleEco) q.set("eco", puzzleEco);
  let data;
  try {
    data = await fetch("/api/puzzles?" + q.toString()).then((r) => r.json());
  } catch (_) {
    $("puzzles-status").textContent = "Couldn't load puzzles.";
    return;
  }
  const list = (data && data.puzzles) || [];
  if (!list.length) {
    $("puzzles-status").textContent = "No puzzles for that theme yet.";
    return;
  }
  // Random order (Fisher–Yates): the API returns hardest-first, so without this the drill always
  // opens on the same position and the answers get memorised instead of learned. When
  // order="srs" (default) the server already ordered due/failed puzzles first with a per-tier
  // shuffle, so we leave that order alone — unless this build of the API didn't annotate it
  // (older server / fallback), in which case we still shuffle client-side.
  const serverOrdered = puzzleOrder === "srs" && list.every((p) => p && p.srs);
  if (!serverOrdered) {
    for (let i = list.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [list[i], list[j]] = [list[j], list[i]];
    }
  }
  if (!puzzle) {
    preDrillMeta = $("game-meta").textContent;
    preDrillOrient = orient;
  }
  puzzle = { list, idx: 0, motif, tries: 0, revealed: false, solved: false };
  showView("puzzles"); // the drill runs on the board slot inside the Puzzles view
  $("puzzles-status").textContent = "";
  showPuzzle(0);
}

// The full drill sequence for a puzzle: solver moves at even indices, engine replies between.
// Single-move puzzles (old records with no cached line, quiet positional slips) have length 1.
function puzzleLine(p) {
  return p.line_uci && p.line_uci.length ? p.line_uci : [p.solution_uci];
}

// Reset per-puzzle state and paint the board for puzzle `i` (side to move = the solver).
function showPuzzle(i) {
  if (!puzzle) return;
  puzzle.idx = clamp(i, 0, puzzle.list.length - 1);
  puzzle.tries = 0;
  puzzle.plies = 0; // how far into the drill line the board currently is
  puzzle.gen = (puzzle.gen || 0) + 1; // invalidates queued auto-replies from the previous puzzle
  puzzle.revealed = false;
  puzzle.solved = false;
  puzzle.failed = false; // whether a wrong move has happened yet on this puzzle
  puzzle.attemptSent = false; // guards against double-posting an attempt for this puzzle
  // Leave any game-review mode: the board is a puzzle now, not a timeline.
  analyzing = false;
  exploring = false;
  evalShapes = [];
  searchGen += 1; // cancel any in-flight arrow search from the previous position
  bestArrows = [];
  threatArrows = [];
  playedWasBest = false;
  paintPuzzleBoard();
  puzzleStatusPrompt();
  renderPuzzlePanel();
}

// (Re)build the board at the drill's current progress (p.fen + the line's first `puzzle.plies`
// moves) with only the solver movable — used on load and to snap back after a wrong guess.
function paintPuzzleBoard(shapes = []) {
  const p = puzzle.list[puzzle.idx];
  chess.load(p.fen);
  for (const uci of puzzleLine(p).slice(0, puzzle.plies)) {
    chess.move({ from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci[4] });
  }
  orient = p.color;
  applyEvalBarTheme();
  setEvalBar(null);
  const done = puzzle.solved || puzzle.revealed;
  ground.set({
    fen: chess.fen(),
    orientation: orient,
    turnColor: turnColor(),
    check: chess.inCheck(),
    movable: { color: done ? undefined : p.color, dests: done ? new Map() : computeDests(), free: false, showDests: true },
    lastMove: undefined,
  });
  ground.setAutoShapes(shapes);
  const opp = (p.color === "white" ? p.black : p.white) || "?";
  $("game-meta").textContent = `Puzzle ${puzzle.idx + 1} of ${puzzle.list.length} — from your game vs ${opp}`;
}

// The "your move" prompt line, with sequence progress when the drill is longer than one move.
function puzzleStatusPrompt() {
  const p = puzzle.list[puzzle.idx];
  const line = puzzleLine(p);
  const total = Math.ceil(line.length / 2); // solver moves in the drill
  const num = Math.floor(puzzle.plies / 2) + 1;
  const toMove = p.color === "white" ? "White" : "Black";
  const glyph = { blunder: "🔴", mistake: "🟠", inaccuracy: "🟡" }[p.classification] || "";
  $("status").className = "status";
  if (puzzle.plies === 0) {
    const seq = total > 1 ? (p.mate ? ` Mate in ${total} — play the whole sequence.` : ` ${total}-move sequence.`) : "";
    $("status").innerHTML = `${glyph} <b>${toMove} to move</b> — find the move you missed.${seq}`;
  } else {
    $("status").innerHTML = `✅ Keep going — <b>move ${num} of ${total}</b>.`;
  }
}

// Fire-and-forget: tell the SRS scheduler how this puzzle went. Guarded by `attemptSent` so a
// puzzle only ever counts once (a reveal after a wrong move shouldn't post twice, etc).
function recordPuzzleAttempt(result, firstTry) {
  if (!puzzle || puzzle.attemptSent) return;
  puzzle.attemptSent = true;
  const p = puzzle.list[puzzle.idx];
  fetch("/api/puzzles/attempt", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ puzzle_id: p.id, result, first_try: firstTry }),
  }).catch(() => {});
}

function onPuzzleMove(orig, dest) {
  const p = puzzle.list[puzzle.idx];
  const line = puzzleLine(p);
  const expected = line[puzzle.plies];
  const promo = isPromotion(orig, dest) ? "q" : undefined;
  const uci = orig + dest + (promo ?? "");
  // Accept an orig/dest match even if the expected move underpromotes — the board defaults to a
  // queen, and making the solver guess the promotion piece isn't the lesson here.
  if (uci === expected || orig + dest === expected.slice(0, 4)) {
    chess.move({ from: orig, to: dest, promotion: expected[4] || promo });
    puzzle.plies += 1;
    const firstMove = puzzle.plies === 1;
    const shapes = [{ orig, dest, brush: "green" }];
    // After the first correct move, contrast it with what you ACTUALLY played in the game (red).
    if (firstMove && p.played_uci && p.played_uci !== expected) {
      shapes.push(arrowShape(p.played_uci, "red"));
    }
    ground.set({
      fen: chess.fen(),
      turnColor: turnColor(),
      check: chess.inCheck(),
      movable: { color: undefined, dests: new Map() },
    });
    ground.setAutoShapes(shapes);

    if (puzzle.plies >= line.length) {
      // Sequence complete.
      puzzle.solved = true;
      recordPuzzleAttempt("pass", puzzle.tries === 0 && !puzzle.failed);
      const extra = puzzle.tries === 0 ? "first try!" : `after ${puzzle.tries + 1} tries.`;
      const what = line.length > 1 ? (p.mate ? "Checkmate!" : "Full sequence!") : escapeHtml(p.solution_san) + ",";
      $("status").innerHTML = `✅ <b>Correct</b> — ${what} ${extra}` +
        (firstMove && p.played_san ? ` <span class="muted">(red = your ${escapeHtml(p.played_san)} from the game)</span>` : "");
      renderPuzzlePanel();
      return;
    }
    // Auto-play the opponent's reply from the line, then hand the move back to the solver.
    const gen = puzzle.gen;
    $("status").innerHTML = `✅ <b>${escapeHtml(p.line_san ? p.line_san[puzzle.plies - 1] : "Correct")}</b>` +
      (firstMove && p.played_san ? ` <span class="muted">(red = your ${escapeHtml(p.played_san)} from the game)</span>` : "");
    setTimeout(() => {
      if (!puzzle || puzzle.gen !== gen) return; // puzzle changed while we waited
      const reply = line[puzzle.plies];
      chess.move({ from: reply.slice(0, 2), to: reply.slice(2, 4), promotion: reply[4] });
      puzzle.plies += 1;
      ground.set({
        fen: chess.fen(),
        turnColor: turnColor(),
        check: chess.inCheck(),
        movable: { color: p.color, dests: computeDests(), free: false, showDests: true },
        lastMove: [reply.slice(0, 2), reply.slice(2, 4)],
      });
      ground.setAutoShapes([]);
      puzzleStatusPrompt();
    }, 550);
    return;
  }
  // Wrong: flash a red arrow on the attempt, snap the board back (keeping sequence progress),
  // and let them try again.
  puzzle.tries += 1;
  if (!puzzle.failed) {
    puzzle.failed = true;
    recordPuzzleAttempt("fail", false);
  }
  const wasPlayed = uci === p.played_uci && puzzle.plies === 0 ? " — that's the move you played in the game" : "";
  $("status").innerHTML = `❌ Not the best${wasPlayed}. Try again${puzzle.tries >= 2 ? ", or Reveal." : "."}`;
  paintPuzzleBoard([{ orig, dest, brush: "red" }]);
  renderPuzzlePanel();
}

// Play out the remaining drill moves one by one, ending with green = the key move and red = the
// move actually played in the game.
function revealPuzzle() {
  if (!puzzle) return;
  const p = puzzle.list[puzzle.idx];
  const line = puzzleLine(p);
  puzzle.revealed = true;
  recordPuzzleAttempt("fail", false);
  const gen = puzzle.gen;
  const step = () => {
    if (!puzzle || puzzle.gen !== gen) return;
    const uci = line[puzzle.plies];
    chess.move({ from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci[4] });
    puzzle.plies += 1;
    ground.set({
      fen: chess.fen(),
      turnColor: turnColor(),
      check: chess.inCheck(),
      movable: { color: undefined, dests: new Map() },
      lastMove: [uci.slice(0, 2), uci.slice(2, 4)],
    });
    if (puzzle.plies < line.length) {
      setTimeout(step, 450);
      return;
    }
    // Arrows must match the FINAL position: green on the last move played out; the red
    // played-in-game arrow only when the board still sits one move in (its squares refer to the
    // puzzle's start position).
    const shapes = [arrowShape(line[line.length - 1], "green")];
    if (line.length === 1 && p.played_uci && p.played_uci !== line[0]) {
      shapes.push(arrowShape(p.played_uci, "red"));
    }
    ground.setAutoShapes(shapes);
    const sans = (p.line_san || [p.solution_san]).join(" ");
    $("status").innerHTML = `💡 Best was <b>${escapeHtml(sans)}</b>.` +
      (p.played_san ? ` <span class="muted">(red = your ${escapeHtml(p.played_san)} from the game)</span>` : "");
    renderPuzzlePanel();
  };
  step();
}

function nextPuzzle() {
  if (!puzzle) return;
  if (puzzle.idx + 1 >= puzzle.list.length) {
    $("status").innerHTML = `🎉 That's all ${puzzle.list.length} puzzles in this set. Nice work.`;
    puzzle.finished = true;
    renderPuzzlePanel();
    return;
  }
  showPuzzle(puzzle.idx + 1);
}

// The puzzle control row under the board (Reveal / Skip / Next) + open-game context link.
function renderPuzzlePanel() {
  const bar = $("puzzle-controls");
  if (!bar) return;
  // The drill borrows the nav-buttons slot: game navigation is meaningless mid-puzzle, so the
  // Reveal/Next bar replaces it (and gives it back on exit) instead of stacking below the fold.
  $("game-controls").style.display = puzzle ? "none" : "";
  if (!puzzle) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  const p = puzzle.list[puzzle.idx];
  const done = puzzle.solved || puzzle.revealed;
  const gameLink = p.game_url
    ? `<a href="${escapeHtml(p.game_url)}" target="_blank" rel="noopener" class="linklike">see the full game ↗</a>`
    : "";
  bar.innerHTML =
    `<button type="button" id="pz-reveal" ${done ? "disabled" : ""}>💡 Reveal</button>` +
    `<button type="button" id="pz-next">${done ? "Next ▶" : "Skip ▶"}</button>` +
    `<button type="button" id="pz-exit" class="linklike">Exit puzzles</button>` +
    (done ? `<span class="pz-context">${gameLink}</span>` : "");
  $("pz-reveal").onclick = revealPuzzle;
  $("pz-next").onclick = nextPuzzle;
  $("pz-exit").onclick = exitPuzzleMode;
}

function exitPuzzleMode() {
  puzzle = null;
  renderPuzzlePanel(); // hides the drill bar and gives the nav buttons their slot back
  // Restore whatever game was on the board before (if any), else a neutral message.
  if (timeline.length) {
    $("game-meta").textContent = preDrillMeta;
    if (preDrillOrient) orient = preDrillOrient; // undo the puzzle's solver-side flip
    applyEvalBarTheme();
    gotoNode(clamp(cur, 0, timeline.length - 1));
  } else {
    $("status").textContent = "";
    $("game-meta").textContent = "Pick a game in Games, or paste a PGN.";
  }
  showView("puzzles"); // back to where the drill was started from
}

// Export the current puzzle selection to a Lichess study (needs a study:write token). `motif` is
// the active theme filter (blank = all), matched to what the trainer is currently showing.
async function exportPuzzlesToStudy() {
  const btn = $("puzzle-export");
  const status = $("puzzle-export-status");
  const motif = (puzzle && puzzle.motif) || "";
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = "Creating study…";
  if (status) status.textContent = "";
  let res = {};
  try {
    res = await fetch("/api/puzzles/lichess-study", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ motif, kinds: puzzleKinds, days: puzzleDays, limit: 60 }),
    }).then((r) => r.json());
  } catch (_) {
    res = { error: "Couldn't reach the server." };
  }
  btn.disabled = false;
  btn.textContent = prev;
  if (res && res.study_url) {
    if (status)
      status.innerHTML =
        `✅ Created a study with ${res.chapters} chapter${res.chapters === 1 ? "" : "s"} — ` +
        `<a href="${escapeHtml(res.study_url)}" target="_blank" rel="noopener">open it on Lichess ↗</a>`;
  } else if (status) {
    status.textContent = (res && res.error) || "Couldn't create the study.";
  }
}

// --- settings panel ------------------------------------------------------
// "Coaching memory" is a friendly single-choice front for the two profile windows
// (profile_recent + profile_lifetime). Each preset sets both at once.
const PROFILE_PRESETS = {
  balanced: {
    recent: "100", lifetime: "all",
    hint: "Coaches on your last 100 games for current form, plus your whole history for long-term patterns.",
  },
  recent: {
    recent: "100", lifetime: "0",
    hint: "Focuses only on your last 100 games; older games are ignored.",
  },
  all: {
    recent: "0", lifetime: "all",
    hint: "Weighs every game you've played equally, recent or old.",
  },
};

// The two Advanced number fields are the source of truth; the dropdown is a convenience that
// fills them. Match the current field values to a preset (or "custom" for any other combination).
function profileModeFromFields() {
  const r = $("set-recent").value.trim();
  const l = $("set-lifetime").value.trim().toLowerCase() || "all"; // blank lifetime == "all"
  for (const [mode, p] of Object.entries(PROFILE_PRESETS)) {
    if (r === p.recent && l === p.lifetime) return mode;
  }
  return "custom";
}

// Picking a named preset writes its windows into the Advanced fields.
function applyProfilePreset() {
  const p = PROFILE_PRESETS[$("set-profile-mode").value];
  if (p) {
    $("set-recent").value = p.recent;
    $("set-lifetime").value = p.lifetime;
  }
  updateProfileHint();
}

// Editing a window by hand flips the dropdown to the matching preset (or "Custom").
function syncProfileModeFromFields() {
  $("set-profile-mode").value = profileModeFromFields();
  updateProfileHint();
}

function updateProfileHint() {
  const mode = $("set-profile-mode").value;
  $("set-profile-hint").textContent = PROFILE_PRESETS[mode]
    ? PROFILE_PRESETS[mode].hint
    : "Using your own window sizes from Advanced below.";
}

// Mistake sensitivity: a checkbox auto-scales it to each game's PGN rating (default on). Unchecking
// reveals a slider whose value is a representative Elo (stored as `player_elo`; blank = Auto). The
// tier label tracks the same casual/intermediate/advanced/master bands the backend tunes against.
const SKILL_DEFAULT_ELO = "1200"; // where the slider starts when first switching to manual

// Named levels along the slider. Each entry is [minimum Elo, label]; the label shown is the highest
// tier whose minimum the value has reached. Anchors: Beginner 400, Intermediate 1200, Advanced 1800,
// Master 2300, with in-between levels so every slider stop reads as a recognisable strength.
const SKILL_TIERS = [
  [400, "Beginner"],
  [700, "Novice"],
  [1000, "Casual"],
  [1200, "Intermediate"],
  [1500, "Club player"],
  [1800, "Advanced"],
  [2100, "Expert"],
  [2300, "Master"],
];

function eloTier(elo) {
  let label = SKILL_TIERS[0][1];
  for (const [min, name] of SKILL_TIERS) {
    if (elo >= min) label = name;
  }
  return label;
}

// Show/hide the "how many recent games to check" field to match the auto-sync checkbox.
function updateChesscomSyncUI() {
  $("chesscom-sync-max-field").hidden = !$("set-chesscom-sync").checked;
}

// Show/hide the slider to match the checkbox and refresh the readout.
function updateSkillUI() {
  const auto = $("set-skill-auto").checked;
  $("skill-manual").hidden = auto;
  if (!auto) {
    const elo = parseInt($("set-elo").value, 10);
    $("set-elo-label").textContent = `${eloTier(elo)} · ~${elo} Elo`;
  }
}

// Fill the Settings view's form from the server — runs every time the view is entered.
async function populateSettings() {
  $("settings-status").textContent = "";
  $("settings-quit-row").hidden = !appMode; // Quit only makes sense for the standalone .app
  let data;
  try {
    data = await fetch("/api/settings").then((r) => r.json());
  } catch (_) {
    $("settings-status").textContent = "Could not load settings.";
    return;
  }
  const s = data.settings || {};
  $("set-username").value = s.username || "";
  $("set-chesscom").value = s.chesscom_username || "";
  $("set-chesscom-sync").checked = s.chesscom_sync !== false; // auto-sync new games (default on)
  $("set-chesscom-sync-max").value = s.chesscom_sync_max || "5";
  updateChesscomSyncUI();
  $("set-aliases").value = s.aliases || "";
  $("set-token").value = s.lichess_token || "";
  // Coaching memory: load the raw windows into the Advanced fields, then point the
  // dropdown at whichever preset they match (or "Custom" for any other combination).
  $("set-recent").value = s.profile_recent || "";
  $("set-lifetime").value = s.profile_lifetime || "";
  syncProfileModeFromFields();
  // Mistake sensitivity: a stored rating means manual (slider shown); blank means auto-scale.
  const elo = (s.player_elo || "").trim();
  $("set-skill-auto").checked = !elo;
  $("set-elo").value = elo || SKILL_DEFAULT_ELO;
  updateSkillUI();
  $("set-stockfish").value = s.stockfish_path || "";
  $("set-local-llm-url").value = s.local_llm_base_url || "";
  $("set-local-llm-model").value = s.local_llm_model || "";
  $("set-ollama-status").textContent = "";
  $("set-ollama-pick-row").hidden = true; // picker only appears after a successful Detect
  $("set-coach-ai-auto").checked = !!s.coach_ai_auto; // auto-generate per game (default off)
  $("set-coach-ai-persist").checked = s.coach_ai_persist !== false; // remember summaries (default on)
  $("set-personalize").checked = s.personalize_history !== false; // personalize chat (default on)
  $("set-sf-status").textContent = data.stockfish_ok
    ? "Stockfish engine found ✓"
    : "Stockfish not found — analysis won't run until this points at the engine.";
}

// One-click Ollama setup: fill in the default URL if blank, ask the backend what models Ollama
// has pulled, populate the model picker, and auto-select the first one if none is chosen yet.
async function detectOllama() {
  const status = $("set-ollama-status");
  status.textContent = "Looking for Ollama…";
  const url = $("set-local-llm-url").value.trim();
  let data;
  try {
    const q = url ? `?url=${encodeURIComponent(url)}` : "";
    data = await fetch(`/api/ollama/models${q}`).then((r) => r.json());
  } catch (_) {
    status.textContent = "Could not reach the server.";
    return;
  }
  if (!data.ok) {
    status.textContent = data.error || "No Ollama found.";
    return;
  }
  if (!url) $("set-local-llm-url").value = data.base_url; // adopt the URL we found it at
  const sel = $("set-ollama-model-select");
  sel.innerHTML = "";
  for (const name of data.models) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  }
  if (!data.models.length) {
    $("set-ollama-pick-row").hidden = true;
    status.textContent = "Ollama is running but has no models. Pull one: ollama pull qwen2.5-coder";
    return;
  }
  // Show the picker; keep the existing model if it's one Ollama has, else default to the first.
  $("set-ollama-pick-row").hidden = false;
  const current = $("set-local-llm-model").value.trim();
  const chosen = data.models.includes(current) ? current : data.models[0];
  sel.value = chosen;
  $("set-local-llm-model").value = chosen;
  status.textContent = `Found ${data.models.length} model${data.models.length === 1 ? "" : "s"} ✓ — pick one and Save.`;
}

async function saveSettings(e) {
  e.preventDefault();
  $("settings-status").textContent = "Saving…";
  const patch = {
    username: $("set-username").value.trim(),
    chesscom_username: $("set-chesscom").value.trim(),
    chesscom_sync: $("set-chesscom-sync").checked,
    chesscom_sync_max: $("set-chesscom-sync-max").value.trim(),
    aliases: $("set-aliases").value.trim(),
    lichess_token: $("set-token").value.trim(),
    stockfish_path: $("set-stockfish").value.trim(),
    local_llm_base_url: $("set-local-llm-url").value.trim(),
    local_llm_model: $("set-local-llm-model").value.trim(),
    coach_ai_auto: $("set-coach-ai-auto").checked,
    coach_ai_persist: $("set-coach-ai-persist").checked,
    personalize_history: $("set-personalize").checked,
  };
  // The Advanced fields are the source of truth (the dropdowns just fill them).
  patch.profile_recent = $("set-recent").value.trim();
  patch.profile_lifetime = $("set-lifetime").value.trim();
  // Auto-scale on -> blank (read each game's Elo); off -> the slider's chosen Elo.
  patch.player_elo = $("set-skill-auto").checked ? "" : $("set-elo").value.trim();
  let res;
  try {
    res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }).then((r) => r.json());
  } catch (_) {
    $("settings-status").textContent = "Could not save settings.";
    return;
  }
  if (res.error) {
    $("settings-status").textContent = res.error;
    return;
  }
  appUsername = (res.settings && res.settings.username) || "";
  chesscomUsername = (res.settings && res.settings.chesscom_username) || "";
  if (appUsername) $("lichess-user").placeholder = appUsername;
  showView(lastMainView); // saved — leave the Settings view
  // Apply the auto-summary preference without clobbering a summary that's already shown/pending:
  // only act when nothing's there yet (turn-on -> generate now; turn-off -> offer the button).
  coachAiAuto = !!(res.settings && res.settings.coach_ai_auto);
  personalizeHistory = !(res.settings && res.settings.personalize_history === false);
  chesscomSync = !(res.settings && res.settings.chesscom_sync === false);
  const card = $("coach-ai");
  const busy = card && !card.hidden && !card.classList.contains("err");
  if (timeline.length && !busy) {
    if (coachAiAuto) fetchCoachAI();
    else { setCoachAI("hidden"); showCoachButton(true); }
  } else if (!timeline.length) {
    setCoachAI("hidden");
    showCoachButton(false);
  }
  if (historyMode === "normal") loadHistory(); // identity may have changed -> refresh My games
}

function setMode(mode) {
  activateTab(mode);
  if (mode === "normal") {
    loadHistory();
  } else if (mode === "lichess" || mode === "chesscom") {
    remoteCount[mode] = LICHESS_PAGE; // fresh search starts at the first page
    loadRemote(mode, $(`${mode}-user`).value.trim());
  } else {
    // paste: nothing to fetch; just a hint until they submit.
    updatePasteHint();
  }
}

// Count games in a PGN by its [Event headers (>=1: a header-less PGN is still one game).
const countGames = (pgn) => Math.max(1, (pgn.match(/^\s*\[Event\b/gm) || []).length);

function updatePasteHint() {
  if (historyMode !== "paste") return;
  const pgn = $("paste-pgn").value.trim();
  if (!pgn) {
    $("history-status").textContent = "Paste or upload a PGN (one or many games), then Analyze.";
    return;
  }
  const n = countGames(pgn);
  $("history-status").textContent =
    n > 1 ? `${n} games detected — all will be analyzed into My games.` : "1 game ready to analyze.";
}

// Kick off analysis of a PGN typed/uploaded/dropped into the Paste panel. Shared by the Analyze
// button, the file picker, and the drag-and-drop drop zone. Routes multi-game PGNs to openBatch.
function startPasteAnalysis(pgn, side, username) {
  $("firstrun").hidden = true; // in case the first-run prompt was still up
  $("history-status").textContent = "";
  if (countGames(pgn) > 1) openBatch(pgn, side, username);
  else openGame(pgn, side);
}

// Read a dropped/picked .pgn file, mirror it into the Paste textarea (so the user sees what loaded
// and can still pick a side), and — when `analyze` — start the sweep straight away. Dropping a file
// anywhere on the Games panel uses analyze=true so people don't have to click Upload then Analyze.
function loadPgnFile(file, analyze) {
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    setMode("paste"); // reveal the Paste panel so the loaded PGN is visible/editable
    $("paste-pgn").value = reader.result || "";
    updatePasteHint();
    if (!analyze) return;
    const pgn = ($("paste-pgn").value || "").trim();
    if (!pgn) {
      $("history-status").textContent = "That file had no PGN text.";
      return;
    }
    startPasteAnalysis(pgn, $("paste-side").value || "auto", ($("paste-username").value || "").trim());
  };
  reader.onerror = () => {
    $("history-status").textContent = "Could not read that file.";
  };
  reader.readAsText(file);
}

// True when a drag carries files (vs. selected text), so we only hijack file drags.
function dragHasFiles(e) {
  return !!e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
}

// First dropped file that looks like a PGN (by extension — many sources set no MIME type).
function firstPgnFile(dataTransfer) {
  const files = dataTransfer && dataTransfer.files ? Array.from(dataTransfer.files) : [];
  return files.find((f) => /\.(pgn|txt)$/i.test(f.name || "")) || null;
}

// Drag-and-drop a .pgn onto the Games panel (any tab) to load + analyze it. dragenter/leave use a
// depth counter so the highlight doesn't flicker as the cursor crosses child elements.
function initPgnDrop() {
  const col = $("history-col");
  if (!col) return;
  let depth = 0;
  const clear = () => {
    depth = 0;
    col.classList.remove("drag-over");
  };
  col.addEventListener("dragenter", (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    depth++;
    col.classList.add("drag-over");
  });
  col.addEventListener("dragover", (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  });
  col.addEventListener("dragleave", (e) => {
    if (!dragHasFiles(e)) return;
    if (--depth <= 0) clear();
  });
  col.addEventListener("drop", (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    clear();
    const file = firstPgnFile(e.dataTransfer);
    if (!file) {
      setMode("paste");
      $("history-status").textContent = "Drop a .pgn file to analyze it.";
      return;
    }
    loadPgnFile(file, true);
  });
  // Stop the browser from navigating away if a file is dropped outside the panel (only intercept
  // file drags, so dragging selected text into inputs/textarea still works normally).
  ["dragover", "drop"].forEach((ev) =>
    document.addEventListener(ev, (e) => {
      if (dragHasFiles(e)) e.preventDefault();
    })
  );
}

// --- board resizing (the iPad-style drag handle, #col-resizer) ------------------------------
// Everything board-sized derives from the CSS var --board-size, which falls back to the
// responsive --board-default unless we set an explicit --board-user (a px override). We keep the
// user's chosen px in localStorage and re-clamp it on every apply, so it never overflows the row.
const BOARD_SIZE_KEY = "chessBoardSize";
let boardSizeUser = null; // px override, or null = use the responsive default

function boardSizeBounds() {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const RAIL = 64; // the icon rail (styles.css --rail-w)
  const PAD = 44; // the centred .duo row's left+right padding
  const GAP = 48; // gaps around the resizer between board and side columns
  const SIDE_MIN = 320; // keep the side column usable
  const EVALBAR = 28; // eval bar + its gap (col-width = board-size + 28)
  const maxByWidth = vw - RAIL - PAD - GAP - SIDE_MIN - EVALBAR;
  const maxByHeight = Math.round(vh * 0.92);
  const min = 240;
  const max = Math.max(min, Math.min(maxByWidth, maxByHeight));
  return { min, max };
}

let boardRedrawPending = false;
function applyBoardSize() {
  const root = document.documentElement;
  if (boardSizeUser == null) {
    root.style.removeProperty("--board-user");
  } else {
    const { min, max } = boardSizeBounds();
    boardSizeUser = Math.round(Math.max(min, Math.min(max, boardSizeUser)));
    root.style.setProperty("--board-user", boardSizeUser + "px");
  }
  // Chessground caches its bounds, so it needs a redraw to re-place pieces at the new square size.
  if (ground && !boardRedrawPending) {
    boardRedrawPending = true;
    requestAnimationFrame(() => {
      boardRedrawPending = false;
      if (ground) ground.redrawAll();
    });
  }
}

function restoreBoardSize() {
  try {
    const v = parseInt(localStorage.getItem(BOARD_SIZE_KEY) || "", 10);
    if (Number.isFinite(v) && v > 0) boardSizeUser = v;
  } catch (_) {}
  applyBoardSize();
}

function resetBoardSize() {
  boardSizeUser = null;
  try {
    localStorage.removeItem(BOARD_SIZE_KEY);
  } catch (_) {}
  applyBoardSize();
}

function persistBoardSize() {
  if (boardSizeUser == null) return;
  try {
    localStorage.setItem(BOARD_SIZE_KEY, String(boardSizeUser));
  } catch (_) {}
}

function initBoardResizer() {
  const rez = $("col-resizer");
  if (!rez) return;
  let startX = 0;
  let startSize = 0;
  let dragging = false;
  const onMove = (e) => {
    if (!dragging) return;
    // Board is on the left, so dragging right (positive delta) grows it.
    boardSizeUser = startSize + (e.clientX - startX);
    applyBoardSize();
  };
  const onUp = () => {
    if (!dragging) return;
    dragging = false;
    rez.classList.remove("dragging");
    document.body.style.userSelect = "";
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    persistBoardSize();
  };
  rez.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    dragging = true;
    startX = e.clientX;
    // Start from whatever the board is actually rendered at (works whether or not an override is set).
    startSize = Math.round($("board").getBoundingClientRect().width);
    rez.classList.add("dragging");
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  });
  // Double-click resets to the responsive default.
  rez.addEventListener("dblclick", resetBoardSize);
  // Keyboard nudge for accessibility (handle is focusable).
  rez.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const base = boardSizeUser == null ? Math.round($("board").getBoundingClientRect().width) : boardSizeUser;
    boardSizeUser = base + (e.key === "ArrowRight" ? 24 : -24);
    applyBoardSize();
    persistBoardSize();
  });
  // Re-clamp a fixed board size when the window changes (so it can't overflow a now-smaller window).
  window.addEventListener("resize", () => {
    if (boardSizeUser != null) applyBoardSize();
  });
}

function init() {
  showView("review"); // landing view: board + games list (the games pane is active by default)
  ground = Chessground($("board"), {
    fen: chess.fen(),
    orientation: orient,
    movable: { free: false, color: "white", dests: computeDests(), showDests: true },
    events: { move: onUserMove },
    drawable: { enabled: true },
  });
  // Add a neutral dark brush for the "move you played" arrow (it marks what you did, not a
  // judgement, so a dark neutral reads more intuitively than a coloured verdict). Keeps all
  // default brushes intact.
  ground.state.drawable.brushes.grey = {
    key: "grey",
    color: "#4a4a4a",
    opacity: 0.9,
    lineWidth: 10,
  };

  // Board-resize handle: apply any saved board size now that Chessground exists, then wire drag.
  restoreBoardSize();
  initBoardResizer();

  $("back").addEventListener("click", stepBack);
  $("fwd").addEventListener("click", stepForward);
  $("prev-mistake").addEventListener("click", () => currentMistake > 0 && selectMistake(currentMistake - 1));
  $("next-mistake").addEventListener(
    "click",
    () => currentMistake < mistakes.length - 1 && selectMistake(currentMistake + 1)
  );
  $("reset").addEventListener("click", returnToReview);
  $("flip-review").addEventListener("click", reviewOtherSide);
  $("best-toggle").addEventListener("change", (e) => {
    bestArrowOn = bestArrowWanted = e.target.checked;
    refreshBestMoves(); // starts the live search when on, clears arrows when off
  });
  $("threat-toggle").addEventListener("change", (e) => {
    threatArrowOn = threatArrowWanted = e.target.checked;
    refreshBestMoves();
  });
  $("graph").addEventListener("click", onGraphClick);
  $("movelist-expand").addEventListener("click", toggleMoveList);
  $("coach-ai-btn").addEventListener("click", fetchCoachAI);
  $("chat-form").addEventListener("submit", sendChat);

  // The rail: primary navigation between the four views (Settings included).
  for (const v of VIEWS) {
    $(`rail-${v}`).addEventListener("click", () => showView(v));
  }
  // Review's right panel: Games list vs Analysis of the open game.
  $("side-games").addEventListener("click", () => {
    showSidePane("games");
    setMode(historyMode);
  });
  $("side-review").addEventListener("click", () => showSidePane("review"));
  // Games pane: source tabs.
  $("mode-normal").addEventListener("click", () => setMode("normal"));
  $("mode-lichess").addEventListener("click", () => setMode("lichess"));
  $("mode-chesscom").addEventListener("click", () => setMode("chesscom"));
  $("mode-paste").addEventListener("click", () => setMode("paste"));
  $("unreviewed-only").addEventListener("change", (e) => {
    unreviewedOnly = e.target.checked;
    historyCount = HISTORY_PAGE;
    renderMyGames();
  });
  // Puzzle trainer: severity + time-window filters and the Lichess-study export.
  $("puzzle-days").addEventListener("change", (e) => {
    puzzleDays = Number(e.target.value) || 0;
    loadPuzzleThemes();
  });
  $("puzzle-kinds").addEventListener("change", (e) => {
    puzzleKinds = e.target.value || "";
    loadPuzzleThemes();
  });
  $("puzzle-order").addEventListener("change", (e) => {
    puzzleOrder = e.target.value || "srs";
    if (puzzle) startPuzzles(puzzle.motif, puzzleEco); // re-fetch in the new order for the current set
  });
  $("puzzle-export").addEventListener("click", exportPuzzlesToStudy);
  // Paste-PGN: analyze any PGN (e.g. Chess.com), single or multi-game, without the Lichess fetch.
  $("paste-upload").addEventListener("click", () => $("paste-file").click());
  $("paste-file").addEventListener("change", (e) => {
    const file = e.target.files && e.target.files[0];
    loadPgnFile(file, false); // picker just loads into the textarea; user presses Analyze
    e.target.value = ""; // allow re-selecting the same file later
  });
  initPgnDrop(); // drag a .pgn onto the Games panel to load + analyze it
  $("paste-pgn").addEventListener("input", updatePasteHint);
  $("paste-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const pgn = $("paste-pgn").value.trim();
    if (!pgn) {
      $("history-status").textContent = "Paste or upload a PGN first.";
      return;
    }
    startPasteAnalysis(pgn, $("paste-side").value || "auto", ($("paste-username").value || "").trim());
  });
  for (const provider of ["lichess", "chesscom"]) {
    $(`${provider}-form`).addEventListener("submit", (e) => {
      e.preventDefault();
      remoteCount[provider] = LICHESS_PAGE; // a new lookup starts fresh
      loadRemote(provider, $(`${provider}-user`).value.trim());
    });
  }
  // Manual "Sync new games": fetch + analyze anything history hasn't seen for the configured user.
  $("chesscom-sync").addEventListener("click", () => {
    if (!chesscomUsername) {
      $("history-status").textContent = "Set your chess.com username in ⚙ Settings first.";
      return;
    }
    syncChesscom(false);
  });
  // "Set as my account": make the looked-up Lichess handle your unified identity.
  $("set-as-me").addEventListener("click", async () => {
    const u = ($("lichess-user").value.trim() || remoteUser.lichess || "").trim();
    if (!u) return;
    await saveUsername(u);
    reflectSetAsMe((u || "").toLowerCase());
    loadHistory(); // "My games" now resolves to this account
  });
  // First-run prompt (no configured account): two fields, fill in either. Save both, then sync +
  // open the user's latest game (chess.com sync first, else the Lichess/chess.com autoload).
  $("firstrun-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const lichess = $("firstrun-user").value.trim();
    const chesscom = $("firstrun-chesscom-user").value.trim();
    if (!lichess && !chesscom) return;
    await saveIdentity(lichess, chesscom);
    $("firstrun").hidden = true;
    maybeAutoload();
  });
  // Insights panel: re-aggregate when the time period changes.
  $("insights-period").addEventListener("change", (e) => {
    insightsDays = Number(e.target.value) || 0;
    $("insights-body").innerHTML = `<p class="muted">Loading…</p>`;
    loadInsights();
  });
  // Settings view (opened from the rail; populate happens in showView).
  $("settings-cancel").addEventListener("click", () => showView(lastMainView));
  $("settings-form").addEventListener("submit", saveSettings);
  $("settings-quit").addEventListener("click", quitApp);
  $("set-profile-mode").addEventListener("change", applyProfilePreset);
  $("set-recent").addEventListener("input", syncProfileModeFromFields);
  $("set-lifetime").addEventListener("input", syncProfileModeFromFields);
  $("set-skill-auto").addEventListener("change", updateSkillUI);
  $("set-chesscom-sync").addEventListener("change", updateChesscomSyncUI);
  $("set-elo").addEventListener("input", updateSkillUI);
  $("set-ollama-detect").addEventListener("click", detectOllama);
  $("set-ollama-model-select").addEventListener("change", (e) => {
    $("set-local-llm-model").value = e.target.value; // picking a detected model fills the saved field
  });
  window.addEventListener("keydown", (e) => {
    // Escape blurs the chat box (or any field) so board hotkeys work again.
    if (e.key === "Escape" && (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA")) {
      e.target.blur();
      return;
    }
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
    if (puzzle) {
      // Puzzle mode: → / space / n advance, Escape exits; game-review navigation is suspended.
      if (e.key === "ArrowRight" || e.key === " " || e.key === "n" || e.key === "N") {
        e.preventDefault();
        nextPuzzle();
      } else if (e.key === "Escape") {
        e.preventDefault();
        exitPuzzleMode();
      }
      return;
    }
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      stepBack();
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      stepForward();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      gotoNode(0); // jump to the start of the game (Lichess: ↑)
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      gotoNode(timeline.length - 1); // jump to the end of the game (Lichess: ↓)
    } else if (e.key === " ") {
      e.preventDefault();
      stepForward(); // space = next move (Lichess)
    } else if (e.key === "f" || e.key === "F") {
      e.preventDefault();
      flipBoard(); // f = flip board (Lichess)
    } else if (e.key === "l" || e.key === "L") {
      e.preventDefault();
      toggleBestArrows(); // l = toggle best-move arrows (Lichess: local engine)
    } else if (e.key === "t" || e.key === "T") {
      e.preventDefault();
      toggleThreatArrows(); // t = toggle threat arrows
    } else if (e.key === "n" || e.key === "N") {
      e.preventDefault();
      if (currentMistake < mistakes.length - 1) selectMistake(currentMistake + 1); // next mistake
    } else if (e.key === "p" || e.key === "P") {
      e.preventDefault();
      if (currentMistake > 0) selectMistake(currentMistake - 1); // previous mistake
    } else if (e.key === "r" || e.key === "R") {
      e.preventDefault();
      showView("review"); // jump back to the board
    } else if (e.key === "g" || e.key === "G") {
      e.preventDefault();
      showView("review"); // the games list lives in Review's right panel
      showSidePane("games");
    }
  });

  loadAll();
  loadHistory(); // populate the games panel regardless of whether a game is loaded
}

init();
