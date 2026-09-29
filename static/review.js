"use strict";
/* Review replay shell — vanilla JS, no third-party code.
 *
 * Trust rule: every model-controlled string (reasoning excerpts, finding
 * fields, raw archive bytes) passes through sanitizeMarkdown() and is
 * rendered with textContent-backed DOM nodes only. There is intentionally
 * no markup-parsing sink anywhere in this file (the contract test names
 * the forbidden list) — that absence is the XSS boundary, because sessionStorage
 * is readable by any script on this origin.
 *
 * Auth rule: the replay token lives in sessionStorage under TOKEN_KEY and
 * is attached as an Authorization: Bearer header on fetch calls. It is
 * never placed in a URL, never written to persistent client storage.
 */

var TOKEN_KEY = "pr-reviewer.replay-token";
var NUL = "\x00";

/* --- §5 span-preserving sanitizer (mirrors lambda/common/sanitize.py) --- */
/* Three phases, in this order:
 *   1a. extractFencedBlocks — stash ``` fenced blocks behind placeholders
 *   1b. extractInlineSpans  — stash `inline code` spans behind placeholders
 *   2.  neutralizeProseText — neutralize ![img](u) / [link](u) / <http...>
 *       in the remaining prose
 *   3.  restoreSpans        — splice the stashed spans back byte-identical
 * Code spans survive as code; everything else renders as inert text. */

function stashPlaceholder(index) {
  return NUL + "CODE_SPAN_" + index + NUL;
}

function sanitizeError(reason) {
  var err = new Error("invalid sanitize input: text: " + reason);
  err.name = "SanitizeError";
  return err;
}

function extractFencedBlocks(text, stash) {
  // Opening line: optional indent + run of 3+ backticks + backtick-free
  // info string. Closing line: optional indent + run of >= opening length
  // and nothing else. An unclosed fence runs to end of text.
  var lines = text.split("\n");
  var starts = [];
  var pos = 0;
  var i;
  for (i = 0; i < lines.length; i++) {
    starts.push(pos);
    pos += lines[i].length + 1;
  }
  var chunks = [];
  var cursor = 0;
  i = 0;
  while (i < lines.length) {
    var opened = lines[i].match(/^[ \t]*(`{3,})([^`]*)$/);
    if (opened === null) {
      i++;
      continue;
    }
    var runLen = opened[1].length;
    var closeRe = new RegExp("^[ \\t]*`{" + runLen + ",}[ \\t]*$");
    var j = i + 1;
    while (j < lines.length && !closeRe.test(lines[j])) {
      j++;
    }
    var end = j < lines.length ? starts[j] + lines[j].length : text.length;
    chunks.push(text.slice(cursor, starts[i]));
    chunks.push(stashPlaceholder(stash.length));
    stash.push(text.slice(starts[i], end));
    cursor = end;
    i = j < lines.length ? j + 1 : lines.length;
  }
  chunks.push(text.slice(cursor));
  return chunks.join("");
}

function extractInlineSpans(text, stash) {
  // Maximal backtick runs open; the closer is the next run of exactly the
  // same length. An opener with no closer stays literal.
  var out = [];
  var i = 0;
  while (i < text.length) {
    if (text[i] !== "`") {
      out.push(text[i]);
      i++;
      continue;
    }
    var j = i;
    while (j < text.length && text[j] === "`") {
      j++;
    }
    var run = j - i;
    var found = -1;
    var k = j;
    while (k < text.length) {
      if (text[k] !== "`") {
        k++;
        continue;
      }
      var m = k;
      while (m < text.length && text[m] === "`") {
        m++;
      }
      if (m - k === run) {
        found = k;
        break;
      }
      k = m;
    }
    if (found === -1) {
      out.push("`");
      i++;
      continue;
    }
    out.push(stashPlaceholder(stash.length));
    stash.push(text.slice(i, found + run));
    i = found + run;
  }
  return out.join("");
}

function neutralizeProseText(prose) {
  // Neutralize exactly three prose constructs so model markdown cannot
  // carry live links, images, or autolinks into the render tree.
  // Neutralization target example: an embedded <script>alert(1)</script>
  // payload must survive this phase as inert text and reach the DOM only
  // through textContent, never as live markup.
  // Images precede links so ![a](b) is never eaten as a link; link/image
  // text tolerates one nested bracket pair (linked images still defuse).
  var nested = "(?:[^\\[\\]]|\\[[^\\[\\]]*\\])*";
  var imageRe = new RegExp("!\\[(" + nested + ")\\]\\(([^)]*)\\)", "g");
  var linkRe = new RegExp("\\[(" + nested + ")\\]\\(([^)]*)\\)", "g");
  var autolinkRe = /<[Hh][Tt][Tt][Pp]([^<>\s]+)>/g;
  var out = prose.replace(imageRe, "[Image: $1] ($2)");
  out = out.replace(linkRe, "$1 ($2)");
  out = out.replace(autolinkRe, function (match) {
    return "`" + match + "`";
  });
  return out;
}

function restoreSpans(prose, stash) {
  var out = prose;
  for (var index = 0; index < stash.length; index++) {
    out = out.split(stashPlaceholder(index)).join(stash[index]);
  }
  return out;
}

function sanitizeMarkdown(text) {
  // Total on strings; fails closed on non-strings and on NUL bytes (the
  // placeholder-collision domain) — mirrors sanitize().
  if (typeof text !== "string") {
    throw sanitizeError("bad_type");
  }
  if (text.indexOf(NUL) !== -1) {
    throw sanitizeError("nul_byte");
  }
  var stash = [];
  var prose = extractFencedBlocks(text, stash);
  prose = extractInlineSpans(prose, stash);
  prose = neutralizeProseText(prose);
  return restoreSpans(prose, stash);
}

/* --- sanitized DOM builder ----------------------------------------- */
/* Renders sanitized markdown into textContent-backed nodes: fenced spans
 * become pre>code, inline spans become code, prose becomes paragraphs.
 * Model-controlled leaves never touch a markup parser. */

function splitSegments(stash, target) {
  // Yields [{code:boolean, text}] segments in document order by cutting
  // the placeholder runs our own extractors left behind.
  var segments = [];
  var re = new RegExp(NUL + "CODE_SPAN_(\\d+)" + NUL, "g");
  var last = 0;
  var m;
  while ((m = re.exec(target)) !== null) {
    if (m.index > last) {
      segments.push({ code: false, text: target.slice(last, m.index) });
    }
    var stashed = stash[Number(m[1])];
    segments.push({ code: true, text: stashed === undefined ? "" : stashed });
    last = m.index + m[0].length;
  }
  if (last < target.length) {
    segments.push({ code: false, text: target.slice(last) });
  }
  return segments;
}

function appendInlineProse(parent, text) {
  // Inline code spans (already extraction-verified) become <code>;
  // the rest becomes plain text nodes.
  var doc = parent.ownerDocument;
  var stash = [];
  var withHoles = extractInlineSpans(text, stash);
  var segments = splitSegments(stash, withHoles);
  segments.forEach(function (seg) {
    if (seg.code) {
      var code = doc.createElement("code");
      code.textContent = seg.text;
      parent.appendChild(code);
    } else {
      parent.appendChild(doc.createTextNode(seg.text));
    }
  });
}

function renderModelMarkdown(container, text) {
  var doc = container.ownerDocument;
  container.textContent = "";
  var safe;
  try {
    safe = sanitizeMarkdown(text);
  } catch (err) {
    var fallback = doc.createElement("p");
    fallback.className = "empty";
    fallback.textContent = "[unrenderable content withheld]";
    container.appendChild(fallback);
    return;
  }
  // Re-segment the restored output so stashed code spans keep code styling.
  var stash = [];
  var withHoles = extractFencedBlocks(safe, stash);
  var segments = splitSegments(stash, withHoles);
  var para = null;
  function flushPara() {
    if (para !== null && para.firstChild !== null) {
      container.appendChild(para);
    }
    para = null;
  }
  segments.forEach(function (seg) {
    if (seg.code) {
      flushPara();
      var pre = doc.createElement("pre");
      var code = doc.createElement("code");
      var body = seg.text;
      // Strip the fence lines for display; keep the inner bytes verbatim.
      var lines = body.split("\n");
      if (lines.length >= 2 && /^[ \t]*`{3,}/.test(lines[0])) {
        lines = lines.slice(1);
      }
      if (lines.length >= 1 && /^[ \t]*`+[ \t]*$/.test(lines[lines.length - 1])) {
        lines = lines.slice(0, -1);
      }
      code.textContent = lines.join("\n");
      pre.appendChild(code);
      container.appendChild(pre);
      return;
    }
    seg.text.split("\n").forEach(function (line) {
      if (line.trim() === "") {
        flushPara();
        return;
      }
      if (para === null) {
        para = doc.createElement("p");
      }
      if (para.firstChild !== null) {
        para.appendChild(doc.createTextNode(" "));
      }
      appendInlineProse(para, line);
    });
  });
  flushPara();
  if (container.firstChild === null) {
    var empty = doc.createElement("p");
    empty.className = "empty";
    empty.textContent = "[empty]";
    container.appendChild(empty);
  }
}

/* --- auth + fetch --------------------------------------------------- */

function getToken() {
  return window.sessionStorage.getItem(TOKEN_KEY);
}

function setToken(token) {
  window.sessionStorage.setItem(TOKEN_KEY, token);
}

function clearToken() {
  window.sessionStorage.removeItem(TOKEN_KEY);
}

function authHeaders() {
  return { Authorization: "Bearer " + getToken() };
}

function fetchAuthed(path) {
  return window.fetch(path, { headers: authHeaders() });
}

/* --- page state ----------------------------------------------------- */

function routeParams() {
  // Production path /runs/{pr}/{sha}/; local dev falls back to query
  // params or the manual inputs. The token itself never appears in any URL.
  var pathMatch = window.location.pathname.match(/^\/runs\/(\d+)\/([0-9a-f]{40})\/?$/);
  if (pathMatch !== null) {
    return { pr: pathMatch[1], sha: pathMatch[2] };
  }
  var query = new window.URLSearchParams(window.location.search);
  return { pr: query.get("pr"), sha: query.get("sha") };
}

function notice(message) {
  var el = document.getElementById("notice");
  el.textContent = message;
  el.hidden = false;
}

function clearNotice() {
  var el = document.getElementById("notice");
  el.textContent = "";
  el.hidden = true;
}

function fmtTime(ts) {
  var d = new Date(ts);
  function pad(n, w) {
    var s = String(n);
    while (s.length < w) {
      s = "0" + s;
    }
    return s;
  }
  return (
    pad(d.getHours(), 2) + ":" + pad(d.getMinutes(), 2) + ":" +
    pad(d.getSeconds(), 2) + "." + pad(d.getMilliseconds(), 3)
  );
}

/* --- DAG replay ------------------------------------------------------ */

var DAG_NODES = ["correctness", "security", "tests", "verifier", "synthesizer"];

function nodeEl(name) {
  return document.getElementById("node-" + name);
}

function resetDag() {
  DAG_NODES.forEach(function (name) {
    nodeEl(name).classList.remove("active", "done", "failed");
  });
  ["edge-c-v", "edge-s-v", "edge-t-v", "edge-v-y"].forEach(function (id) {
    document.getElementById(id).classList.remove("lit");
  });
}

function applyStage(step) {
  // step: {kind:"start"|"end", node, ok}
  var node = nodeEl(step.node);
  if (step.kind === "start") {
    node.classList.add("active");
    return;
  }
  node.classList.remove("active");
  node.classList.add(step.ok ? "done" : "failed");
  if (step.node === "verifier") {
    ["edge-c-v", "edge-s-v", "edge-t-v"].forEach(function (id) {
      document.getElementById(id).classList.add("lit");
    });
  }
  if (step.node === "synthesizer") {
    document.getElementById("edge-v-y").classList.add("lit");
  }
}

function planReplay(events) {
  // Compress the event stream into ordered DAG stage transitions.
  // Unknown specialties/sinks are skipped — a surprise event must never
  // break the replay loop.
  var known = {};
  DAG_NODES.forEach(function (name) {
    known[name] = true;
  });
  var steps = [];
  events.forEach(function (ev) {
    var t = ev.type;
    if ((t === "agent_started" || t === "agent_completed" || t === "agent_failed") &&
        !known[ev.specialty]) {
      return;
    }
    if (t === "agent_started") {
      steps.push({ kind: "start", node: ev.specialty });
    } else if (t === "agent_completed") {
      steps.push({ kind: "end", node: ev.specialty, ok: true });
    } else if (t === "agent_failed") {
      steps.push({ kind: "end", node: ev.specialty, ok: false });
    } else if (t === "verification_done") {
      steps.push({ kind: "end", node: "verifier", ok: true });
    } else if (t === "verification_failed") {
      steps.push({ kind: "end", node: "verifier", ok: false });
    } else if (t === "review_synthesized") {
      steps.push({ kind: "start", node: "synthesizer" });
      steps.push({ kind: "end", node: "synthesizer", ok: true });
    } else if (t === "synthesizer_failed") {
      steps.push({ kind: "end", node: "synthesizer", ok: false });
    }
  });
  return steps;
}

var replayTimer = null;

function stopReplay() {
  if (replayTimer !== null) {
    window.clearTimeout(replayTimer);
    replayTimer = null;
  }
}

function runReplay(steps) {
  stopReplay();
  resetDag();
  var label = document.getElementById("replay-state");
  if (steps.length === 0) {
    label.textContent = "no pipeline events";
    return;
  }
  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced) {
    steps.forEach(applyStage);
    label.textContent = "final state (" + steps.length + " steps)";
    return;
  }
  var i = 0;
  label.textContent = "replaying…";
  function tick() {
    if (i >= steps.length) {
      label.textContent = "done (" + steps.length + " steps)";
      replayTimer = null;
      return;
    }
    applyStage(steps[i]);
    i++;
    label.textContent = "step " + i + " of " + steps.length;
    replayTimer = window.setTimeout(tick, 380);
  }
  tick();
}

/* --- section renderers ------------------------------------------------ */

function renderReasoning(events, completedBySpecialty) {
  var host = document.getElementById("reasoning");
  host.textContent = "";
  var bySpecialty = {};
  events.forEach(function (ev) {
    // Render reasoning_excerpt WHEN PRESENT — absent means no card
    // content for that field, never a placeholder.
    if (ev.type === "agent_reasoning" && typeof ev.reasoning_excerpt === "string" &&
        ev.reasoning_excerpt !== "") {
      bySpecialty[ev.specialty] = ev.reasoning_excerpt;
    }
  });
  var names = Object.keys(bySpecialty).sort();
  if (names.length === 0) {
    var empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "No reasoning excerpts recorded for this run.";
    host.appendChild(empty);
    return;
  }
  names.forEach(function (name) {
    var card = document.createElement("article");
    card.className = "card";
    var head = document.createElement("div");
    head.className = "card-head";
    var tag = document.createElement("span");
    tag.className = "agent-tag";
    tag.textContent = name;
    head.appendChild(tag);
    var done = completedBySpecialty[name];
    if (done !== undefined) {
      var stats = document.createElement("span");
      stats.className = "agent-stats";
      stats.textContent =
        String(done.findings_n) + " findings · " + String(done.latency_ms) + " ms";
      head.appendChild(stats);
    }
    card.appendChild(head);
    var prose = document.createElement("div");
    prose.className = "prose";
    renderModelMarkdown(prose, bySpecialty[name]);
    card.appendChild(prose);
    host.appendChild(card);
  });
}

function timelineDetail(ev) {
  switch (ev.type) {
    case "agent_started":
      return "specialist dispatched";
    case "agent_reasoning":
      return "reasoning excerpt recorded";
    case "agent_completed":
      return String(ev.findings_n) + " findings · " + String(ev.latency_ms) + " ms";
    case "agent_failed":
      return "failed: " + String(ev.error_class);
    case "verification_done":
      return (
        String(ev.survived_n) + " survived · " + String(ev.killed_n) +
        " killed · " + String(ev.escalated_n) + " escalated"
      );
    case "verification_failed":
      return "failed: " + String(ev.error_class);
    case "review_synthesized":
      return (
        String(ev.findings_merged_n) + " merged · " +
        String(ev.dropped_as_duplicate_n) + " duplicates dropped"
      );
    case "synthesizer_failed":
      return "failed: " + String(ev.error_class);
    case "degraded_to_single_pass":
      return "degraded (" + String(ev.reason) + " at " + String(ev.failed_stage) + ")";
    case "review_published":
      return "comment " + String(ev.comment_id);
    case "review_started":
      return "run opened";
    case "checkpoint":
      return "stage: " + String(ev.stage);
    default:
      return "";
  }
}

function renderTimeline(events) {
  var host = document.getElementById("timeline");
  host.textContent = "";
  var ordered = events.slice().sort(function (a, b) {
    return a.ts - b.ts;
  });
  if (ordered.length === 0) {
    var li = document.createElement("li");
    var detail = document.createElement("span");
    detail.className = "t-detail empty";
    detail.textContent = "No events recorded.";
    li.appendChild(detail);
    host.appendChild(li);
    return;
  }
  ordered.forEach(function (ev) {
    var li = document.createElement("li");
    var ts = document.createElement("span");
    ts.className = "t-ts";
    ts.textContent = typeof ev.ts === "number" ? fmtTime(ev.ts) : "—";
    var type = document.createElement("span");
    type.className = "t-type";
    type.textContent = String(ev.type);
    var detail = document.createElement("span");
    detail.className = "t-detail";
    detail.textContent = timelineDetail(ev);
    li.appendChild(ts);
    li.appendChild(type);
    li.appendChild(detail);
    host.appendChild(li);
  });
}

function pickFindings(events) {
  // Prefer the synthesizer's merged truth; fall back to verifier
  // survivors, then to raw specialist findings.
  var synth = null;
  var verified = null;
  var completed = [];
  events.forEach(function (ev) {
    if (ev.type === "review_synthesized" && Array.isArray(ev.findings)) {
      synth = ev.findings;
    }
    if (ev.type === "verification_done") {
      verified = (Array.isArray(ev.verified) ? ev.verified : []).concat(
        Array.isArray(ev.escalated) ? ev.escalated : []
      );
    }
    if (ev.type === "agent_completed" && Array.isArray(ev.findings)) {
      completed = completed.concat(ev.findings);
    }
  });
  if (synth !== null) {
    return synth;
  }
  if (verified !== null) {
    return verified;
  }
  return completed;
}

function renderFindings(findings) {
  var host = document.getElementById("findings");
  host.textContent = "";
  var count = document.getElementById("findings-count");
  count.textContent = findings.length === 0 ? "" : "(" + findings.length + ")";
  if (findings.length === 0) {
    var empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "No significant issues found.";
    host.appendChild(empty);
    return;
  }
  findings.forEach(function (item) {
    var card = document.createElement("article");
    var severity = typeof item.severity === "string" ? item.severity : "LOW";
    card.className = "card sev-" + severity;
    var head = document.createElement("div");
    head.className = "card-head";
    var sev = document.createElement("span");
    sev.className = "sev";
    sev.textContent = severity;
    head.appendChild(sev);
    var loc = document.createElement("span");
    loc.className = "loc";
    var line = item.line_start !== undefined ? item.line_start : item.line;
    loc.textContent = String(item.file_path || item.path || "unknown") + ":" + String(line);
    head.appendChild(loc);
    if (item.escalated === true) {
      var flag = document.createElement("span");
      flag.className = "flag";
      flag.textContent = "[Requires Verification]";
      head.appendChild(flag);
    }
    card.appendChild(head);
    var title = document.createElement("div");
    title.className = "card-title prose";
    renderModelMarkdown(title, String(item.title || "(untitled)"));
    card.appendChild(title);
    if (typeof item.description === "string" && item.description !== "") {
      var desc = document.createElement("div");
      desc.className = "prose";
      renderModelMarkdown(desc, item.description);
      card.appendChild(desc);
    }
    if (typeof item.suggested_fix === "string" && item.suggested_fix !== "") {
      var fix = document.createElement("div");
      fix.className = "prose";
      renderModelMarkdown(fix, "Fix: " + item.suggested_fix);
      card.appendChild(fix);
    }
    host.appendChild(card);
  });
}

function renderRawArchive(eventsText, metaText) {
  var eventsPre = document.getElementById("raw-events");
  var metaPre = document.getElementById("raw-meta");
  // Raw archive bytes are data, not markup: sanitize first, then render
  // as inert text. textContent is the second half of the boundary.
  try {
    eventsPre.textContent = sanitizeMarkdown(eventsText);
  } catch (err) {
    eventsPre.textContent = "[events.jsonl withheld: undecodable]";
  }
  try {
    metaPre.textContent = sanitizeMarkdown(metaText);
  } catch (err) {
    metaPre.textContent = "[meta.json withheld: undecodable]";
  }
}

/* --- loading ----------------------------------------------------------- */

function parseJsonl(text) {
  var events = [];
  text.split("\n").forEach(function (line) {
    if (line.trim() === "") {
      return;
    }
    try {
      events.push(JSON.parse(line));
    } catch (err) {
      // One corrupt line must not sink the replay; the raw tab still
      // shows the bytes for inspection.
    }
  });
  return events;
}

function archivePaths(pr, sha, latest) {
  // archive_s3_key names the events object; meta.json is its sibling.
  var key = typeof latest.archive_s3_key === "string" ? latest.archive_s3_key : null;
  var runId = typeof latest.run_id === "string" ? latest.run_id : null;
  var headSha = typeof latest.sha === "string" ? latest.sha : sha;
  var eventsPath;
  var metaPath;
  if (key !== null && key.slice(-12) === "events.jsonl") {
    eventsPath = "/" + key;
    metaPath = "/" + key.slice(0, -12) + "meta.json";
  } else if (runId !== null) {
    eventsPath = "/runs/" + pr + "/" + headSha + "/" + runId + "/events.jsonl";
    metaPath = "/runs/" + pr + "/" + headSha + "/" + runId + "/meta.json";
  } else {
    return null;
  }
  return { eventsPath: eventsPath, metaPath: metaPath };
}

function loadReview(pr, sha) {
  clearNotice();
  return fetchAuthed("/api/runs/" + pr + "/latest").then(function (resp) {
    if (resp.status === 401) {
      clearToken();
      syncSession();
      throw new Error("Token rejected (401) — paste the current replay token.");
    }
    if (resp.status === 404) {
      throw new Error("No runs recorded for PR " + pr + " yet.");
    }
    if (!resp.ok) {
      throw new Error("Latest-run lookup failed (" + resp.status + ").");
    }
    return resp.json();
  }).then(function (latest) {
    if (typeof latest.run_id !== "string") {
      throw new Error("Latest-run response is missing run_id.");
    }
    document.getElementById("meta-pr").textContent = String(pr);
    document.getElementById("meta-sha").textContent = String(latest.sha || sha).slice(0, 12);
    document.getElementById("meta-run").textContent = String(latest.run_id).slice(0, 12);
    document.getElementById("meta-status").textContent = String(latest.status || "—");
    document.getElementById("meta-pipeline").textContent = String(latest.pipeline || "—");
    document.getElementById("run-meta").hidden = false;
    var paths = archivePaths(pr, sha, latest);
    if (paths === null) {
      throw new Error("Latest-run response carries no archive location.");
    }
    return window.Promise.all([
      fetchAuthed(paths.eventsPath),
      fetchAuthed(paths.metaPath)
    ]).then(function (pair) {
      var eventsResp = pair[0];
      var metaResp = pair[1];
      if (eventsResp.status === 401 || metaResp.status === 401) {
        clearToken();
        syncSession();
        throw new Error("Token rejected (401) — paste the current replay token.");
      }
      return window.Promise.all([
        eventsResp.ok ? eventsResp.text() : "",
        metaResp.ok ? metaResp.text() : ""
      ]).then(function (texts) {
        if (!eventsResp.ok) {
          notice("events.jsonl unavailable (" + eventsResp.status + ") — showing run header only.");
        } else if (!metaResp.ok) {
          notice("meta.json unavailable (" + metaResp.status + ") — archive metadata hidden.");
        }
        return { eventsText: texts[0], metaText: texts[1] };
      });
    });
  }).then(function (archives) {
    var events = parseJsonl(archives.eventsText);
    var completedBySpecialty = {};
    events.forEach(function (ev) {
      if (ev.type === "agent_completed" && typeof ev.specialty === "string") {
        completedBySpecialty[ev.specialty] = {
          findings_n: ev.findings_n || 0,
          latency_ms: ev.latency_ms || 0
        };
      }
    });
    document.getElementById("review").hidden = false;
    renderReasoning(events, completedBySpecialty);
    renderTimeline(events);
    renderFindings(pickFindings(events));
    renderRawArchive(archives.eventsText, archives.metaText);
    var steps = planReplay(events);
    runReplay(steps);
    document.getElementById("replay-btn").onclick = function () {
      runReplay(steps);
    };
  }).catch(function (err) {
    notice(err instanceof Error ? err.message : "Review failed to load.");
  });
}

/* --- session + boot ----------------------------------------------------- */

function syncSession() {
  var authed = getToken() !== null;
  document.getElementById("session-state").textContent = authed ? "unlocked" : "locked";
  document.getElementById("session-state").classList.toggle("open", authed);
  document.getElementById("logout-btn").hidden = !authed;
}

function boot() {
  var params = routeParams();
  if (params.pr !== null && params.pr !== undefined) {
    document.getElementById("in-pr").value = params.pr;
  }
  if (params.sha !== null && params.sha !== undefined) {
    document.getElementById("in-sha").value = params.sha;
  }

  document.getElementById("tab-events").onclick = function () {
    document.getElementById("raw-events").hidden = false;
    document.getElementById("raw-meta").hidden = true;
    document.getElementById("tab-events").setAttribute("aria-selected", "true");
    document.getElementById("tab-meta").setAttribute("aria-selected", "false");
  };
  document.getElementById("tab-meta").onclick = function () {
    document.getElementById("raw-events").hidden = true;
    document.getElementById("raw-meta").hidden = false;
    document.getElementById("tab-events").setAttribute("aria-selected", "false");
    document.getElementById("tab-meta").setAttribute("aria-selected", "true");
  };

  document.getElementById("logout-btn").onclick = function () {
    stopReplay();
    clearToken();
    syncSession();
    document.getElementById("review").hidden = true;
    document.getElementById("run-meta").hidden = true;
    document.getElementById("in-token").value = "";
    notice("Logged out — the tab token was discarded.");
  };

  document.getElementById("login-form").onsubmit = function (ev) {
    ev.preventDefault();
    var pr = document.getElementById("in-pr").value.trim();
    var sha = document.getElementById("in-sha").value.trim().toLowerCase();
    var token = document.getElementById("in-token").value;
    if (!/^\d+$/.test(pr)) {
      notice("Enter a numeric pull request number.");
      return;
    }
    if (!/^[0-9a-f]{40}$/.test(sha)) {
      notice("Enter the 40-character head SHA.");
      return;
    }
    if (token === "") {
      notice("Paste the replay token.");
      return;
    }
    setToken(token);
    document.getElementById("in-token").value = "";
    syncSession();
    loadReview(pr, sha);
  };

  syncSession();
  if (getToken() !== null && params.pr && params.sha) {
    loadReview(params.pr, params.sha);
  }
}

document.addEventListener("DOMContentLoaded", boot);
