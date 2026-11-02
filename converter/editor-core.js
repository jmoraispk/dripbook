// The cut editor's logic, with no page code: editor.html draws it, test_editor_core.mjs tests it.
//
// A course is a list of segments that tile the book's blocks in order. Each segment
// is a lesson or a skipped range: {kind: "lesson" | "skip", a, b, subject, lead_in,
// teaser, reason}, where a and b are the indexes of its first and last block. A cut
// is the boundary between two segments; boundary j sits just before block j.
// Every edit returns a new list, or null when the edit isn't allowed.
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.DripbookCuts = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const WORDS_PER_MINUTE = 220;
  const TARGET = [3, 5];     // minutes
  const WARN = [2.5, 6.5];   // minutes; outside this a lesson gets a warning

  function fromPlan(plan) {
    const blocks = plan.all_blocks;
    if (!Array.isArray(blocks) || !blocks.length) {
      throw new Error("This lessons.json has no all_blocks list. Render it again with the current convert.py.");
    }
    const index = new Map(blocks.map((b, i) => [b.id, i]));
    const segs = [];
    const add = (item, kind) => {
      const a = index.get(item.first_block), b = index.get(item.last_block);
      if (a === undefined || b === undefined || a > b) {
        throw new Error(`The plan refers to blocks ${item.first_block} to ${item.last_block}, which aren't in this book.`);
      }
      segs.push({kind, a, b, subject: item.subject || "", lead_in: item.lead_in || "", teaser: item.teaser || "",
                 reason: item.reason || ""});
    };
    (plan.lessons || []).forEach((l) => add(l, "lesson"));
    (plan.skipped || []).forEach((s) => add(s, "skip"));
    segs.sort((x, y) => x.a - y.a);
    let next = 0;
    for (const s of segs) {
      if (s.a !== next) throw new Error(`The plan doesn't cover the book in order around block ${blocks[Math.min(next, blocks.length - 1)].id}.`);
      next = s.b + 1;
    }
    if (next !== blocks.length) throw new Error("The plan stops before the end of the book.");
    return {blocks, segs};
  }

  function toPlan(plan, blocks, segs, extra) {
    const out = {};
    for (const key of ["format", "book", "source", "settings", "cta_note"]) {
      if (plan[key] !== undefined) out[key] = plan[key];
    }
    Object.assign(out, extra || {});
    out.planned_by = "edited in editor.html";
    out.edited = new Date().toISOString().slice(0, 19) + "Z";
    out.lessons = segs.filter((s) => s.kind === "lesson").map((s) => ({
      first_block: blocks[s.a].id, last_block: blocks[s.b].id,
      subject: s.subject, lead_in: s.lead_in, teaser: s.teaser,
    }));
    out.skipped = segs.filter((s) => s.kind === "skip").map((s) => ({
      first_block: blocks[s.a].id, last_block: blocks[s.b].id, reason: s.reason,
    }));
    out.all_blocks = blocks;
    return out;
  }

  function words(blocks, seg) {
    let n = 0;
    for (let i = seg.a; i <= seg.b; i++) n += blocks[i].words || 0;
    return n;
  }

  function minutes(blocks, seg) {
    return Math.round((words(blocks, seg) / WORDS_PER_MINUTE) * 10) / 10;
  }

  function headingLabel(text) {
    return String(text).split("\n").map((s) => s.trim()).filter(Boolean).join(": ");
  }

  // The subject a lesson starting at block j would get: its heading, or the last heading before it, continued.
  function subjectAt(blocks, j) {
    if (blocks[j].type === "heading") return headingLabel(blocks[j].text);
    for (let i = j - 1; i >= 0; i--) {
      if (blocks[i].type === "heading") return `${headingLabel(blocks[i].text)} (continued)`;
    }
    return "Introduction";
  }

  function copy(segs) {
    return segs.map((s) => ({...s}));
  }

  // Move the cut between segments k-1 and k so that segment k starts at block j.
  // A subject the author hasn't changed follows the lesson's new first block.
  function moveCut(segs, k, j, blocks) {
    if (k <= 0 || k >= segs.length) return null;
    const prev = segs[k - 1], cur = segs[k];
    if (j <= prev.a || j > cur.b || j === cur.a) return null;
    const out = copy(segs);
    out[k - 1].b = j - 1;
    out[k].a = j;
    if (blocks && cur.kind === "lesson" && cur.subject === subjectAt(blocks, cur.a)) out[k].subject = subjectAt(blocks, j);
    return out;
  }

  // Cut segment k in two, the second part starting at block j.
  function split(segs, k, j, blocks) {
    const seg = segs[k];
    if (!seg || j <= seg.a || j > seg.b) return null;
    const out = copy(segs);
    out[k].b = j - 1;
    out.splice(k + 1, 0, {kind: seg.kind, a: j, b: seg.b, subject: subjectAt(blocks, j), lead_in: "", teaser: "",
                          reason: seg.reason});
    return out;
  }

  // Join segment k with the one after it. The result is a lesson if either was one.
  function merge(segs, k) {
    if (k < 0 || k >= segs.length - 1) return null;
    const out = copy(segs);
    const first = out[k], second = out[k + 1];
    const keep = first.kind === "lesson" || second.kind !== "lesson" ? first : second;
    out.splice(k, 2, {...keep, a: first.a, b: second.b,
                      kind: first.kind === "lesson" || second.kind === "lesson" ? "lesson" : "skip"});
    return out;
  }

  // Skip a lesson's blocks, or bring a skipped range back as a lesson.
  function setKind(segs, k, kind, blocks) {
    if (!segs[k] || segs[k].kind === kind) return null;
    const out = copy(segs);
    out[k].kind = kind;
    if (kind === "lesson" && !out[k].subject) out[k].subject = subjectAt(blocks, out[k].a);
    if (kind === "skip" && !out[k].reason) out[k].reason = "skipped by hand";
    return out;
  }

  function setText(segs, k, field, value) {
    const out = copy(segs);
    out[k][field] = value;
    return out;
  }

  // Each lesson's "Tomorrow:" line names the next lesson; the last one has none.
  function syncTeasers(segs) {
    const out = copy(segs);
    const lessons = out.filter((s) => s.kind === "lesson");
    lessons.forEach((s, i) => { s.teaser = i + 1 < lessons.length ? lessons[i + 1].subject : ""; });
    return out;
  }

  // Day numbers by segment index; skipped ranges have none.
  function days(segs) {
    let day = 0;
    return segs.map((s) => (s.kind === "lesson" ? ++day : null));
  }

  function warnings(blocks, seg) {
    if (seg.kind !== "lesson") return [];
    const out = [];
    const m = minutes(blocks, seg);
    if (m < WARN[0]) out.push(`Short: about ${m} minutes`);
    if (m > WARN[1]) out.push(`Long: about ${m} minutes`);
    const last = blocks[seg.b], next = blocks[seg.b + 1];
    if (last.type === "heading") out.push("Ends on a heading");
    if (next && last.type === "list_item" && next.type === "list_item") out.push("Ends in the middle of a list");
    if (next && next.type === "code" && String(last.text || "").trim().endsWith(":")) {
      out.push("Ends just before the code it introduces");
    }
    return out;
  }

  // Where boundary j (just before block j) is drawn: a page and a y position in points.
  function boundaryPos(blocks, j) {
    const b = blocks[j], prev = blocks[j - 1];
    const top = b.top ?? 0;
    if (prev && (prev.end_page ?? prev.page) === b.page && prev.bottom != null && prev.bottom < top) {
      return {page: b.page, y: (prev.bottom + top) / 2};
    }
    return {page: b.page, y: Math.max(0, top - 4)};
  }

  function endPage(blocks, seg) {
    const last = blocks[seg.b];
    return last.end_page ?? last.page;
  }

  // The part of `page` each segment covers, as y ranges in points, for drawing bands.
  function regionsOnPage(blocks, segs, page, height) {
    const out = [];
    segs.forEach((s, k) => {
      const first = blocks[s.a].page, last = endPage(blocks, s);
      if (page < first || page > last) return;
      const top = page === first && s.a > 0 ? boundaryPos(blocks, s.a).y : (page === first ? blocks[s.a].top ?? 0 : 0);
      const after = s.b + 1 < blocks.length ? boundaryPos(blocks, s.b + 1) : null;
      const bottom = after && after.page === page ? after.y : height;
      if (bottom > top) out.push({k, top, bottom});
    });
    return out;
  }

  // Boundaries that fall on a page, with the block that starts after each.
  function boundariesOnPage(blocks, page) {
    const out = [];
    for (let j = 1; j < blocks.length; j++) {
      if (blocks[j].page === page) out.push({j, y: boundaryPos(blocks, j).y});
    }
    return out;
  }

  function stats(blocks, segs) {
    const mins = segs.filter((s) => s.kind === "lesson").map((s) => minutes(blocks, s));
    const sorted = [...mins].sort((x, y) => x - y);
    return {
      lessons: mins.length,
      median: sorted.length ? sorted[Math.floor(sorted.length / 2)] : 0,
      shortest: sorted[0] || 0,
      longest: sorted[sorted.length - 1] || 0,
      flagged: segs.filter((s) => warnings(blocks, s).length).length,
    };
  }

  return {WORDS_PER_MINUTE, TARGET, WARN, fromPlan, toPlan, words, minutes, headingLabel, subjectAt, moveCut, split,
          merge, setKind, setText, syncTeasers, days, warnings, boundaryPos, endPage, regionsOnPage,
          boundariesOnPage, stats};
});
