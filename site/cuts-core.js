// The cut editor's logic, with no page code: cuts.html draws it, converter/test_editor_core.mjs tests it.
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
    out.planned_by = "edited in the cut editor";
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

  // ---------------------------------------------------------- a PDF with no lessons.json
  //
  // The editor can open a bare PDF. It reads the text with PDF.js, and blocksFromPages
  // groups it into blocks much like convert.py does, only roughly: enough to place cuts
  // between paragraphs and estimate reading time. The converter reads the PDF properly
  // later, and cuts.json records each cut as a position on a page, so the two never need
  // to agree on block ids.

  const round = (v, step) => Math.round(v / step) * step;
  const tenth = (v) => Math.round(v * 10) / 10;
  const countWords = (text) => (String(text).match(/\S+/g) || []).length;

  // items: [{text, x, w, base, size, mono}], with base measured down from the top of the page.
  function linesFromItems(items) {
    const sorted = items.filter((it) => it.text.trim()).sort((p, q) => p.base - q.base || p.x - q.x);
    const lines = [];
    for (const it of sorted) {
      const line = lines.at(-1);
      if (line && Math.abs(line.base - it.base) < 0.5 * Math.max(line.size, it.size)) line.items.push(it);
      else lines.push({base: it.base, size: it.size, items: [it]});
    }
    return lines.map((line) => {
      const its = line.items.sort((p, q) => p.x - q.x);
      let text = "", chars = 0, monoChars = 0, size = 0, most = 0, right = 0;
      its.forEach((it, i) => {
        const prev = its[i - 1];
        if (prev && it.x - (prev.x + prev.w) > 0.15 * it.size && !/\s$/.test(text) && !/^\s/.test(it.text)) text += " ";
        text += it.text;
        const n = it.text.trim().length;
        chars += n;
        if (it.mono) monoChars += n;
        if (n > most) { most = n; size = it.size; }
        right = Math.max(right, it.x + it.w);
      });
      const base = its.find((it) => it.size === size)?.base ?? line.base;
      return {text: text.replace(/\s+$/, ""), x: its[0].x, right, base, size, mono: monoChars > chars / 2,
              charWidth: its[0].w / Math.max(1, its[0].text.length)};
    });
  }

  // A page's text from PDF.js (page.getTextContent() and page.getViewport({scale: 1})) as items.
  function itemsFromText(content, viewport) {
    return content.items.filter((it) => it.str).map((it) => {
      const [x, base] = viewport.convertToViewportPoint(it.transform[4], it.transform[5]);
      return {text: it.str, x, w: it.width, base, size: Math.hypot(it.transform[0], it.transform[1]),
              mono: content.styles[it.fontName]?.fontFamily === "monospace"};
    });
  }

  // ---------------------------------------------------------------- figures
  // The drawings and images on a page, from PDF.js (page.getOperatorList(), its OPS table and
  // viewport.transform at scale 1): graphics as [x0, top, x1, bottom, "line" | "curve" | "rect"]
  // for each painted path segment, and images as [x0, top, x1, bottom], top measured down.
  function graphicsFromOps(list, OPS, viewTransform) {
    const mul = (a, b) => [a[0] * b[0] + a[2] * b[1], a[1] * b[0] + a[3] * b[1], a[0] * b[2] + a[2] * b[3],
                           a[1] * b[2] + a[3] * b[3], a[0] * b[4] + a[2] * b[5] + a[4], a[1] * b[4] + a[3] * b[5] + a[5]];
    const box = (m, pts) => {
      const xs = [], ys = [];
      for (let i = 0; i < pts.length; i += 2) {
        xs.push(m[0] * pts[i] + m[2] * pts[i + 1] + m[4]);
        ys.push(m[1] * pts[i] + m[3] * pts[i + 1] + m[5]);
      }
      return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
    };
    const paints = new Set(["stroke", "closeStroke", "fill", "eoFill", "fillStroke", "eoFillStroke", "closeFillStroke",
                            "closeEOFillStroke"].map((k) => OPS[k]));
    const imageOps = new Set(["paintImageXObject", "paintInlineImageXObject", "paintImageMaskXObject", "paintJpegXObject",
                              "paintImageXObjectRepeat", "paintSolidColorImageMask"].map((k) => OPS[k]).filter((v) => v != null));
    const graphics = [], images = [], stack = [];
    let ctm = [1, 0, 0, 1, 0, 0];
    const {fnArray: fns, argsArray: args} = list;
    for (let i = 0; i < fns.length; i++) {
      const fn = fns[i], a = args[i];
      if (fn === OPS.save) stack.push(ctm);
      else if (fn === OPS.restore) ctm = stack.pop() || ctm;
      else if (fn === OPS.transform) ctm = mul(ctm, a);
      else if (fn === OPS.paintFormXObjectBegin) { stack.push(ctm); if (Array.isArray(a?.[0])) ctm = mul(ctm, a[0]); }
      else if (fn === OPS.paintFormXObjectEnd) ctm = stack.pop() || ctm;
      else if (imageOps.has(fn)) images.push(box(mul(viewTransform, ctm), [0, 0, 1, 0, 0, 1, 1, 1]));
      else if (fn === OPS.constructPath && paints.has(fns[i + 1])) {
        const m = mul(viewTransform, ctm), [ops, xy] = a;
        let k = 0, cx = 0, cy = 0, sx = 0, sy = 0;
        const seg = (kind, pts) => { const [x0, y0, x1, y1] = box(m, pts); graphics.push([x0, y0, x1, y1, kind]); };
        for (const op of ops) {
          if (op === OPS.moveTo) { cx = sx = xy[k++]; cy = sy = xy[k++]; }
          else if (op === OPS.lineTo) { const x = xy[k++], y = xy[k++]; seg("line", [cx, cy, x, y]); cx = x; cy = y; }
          else if (op === OPS.curveTo) { const q = xy.slice(k, k + 6); k += 6; seg("curve", [cx, cy, ...q]); cx = q[4]; cy = q[5]; }
          else if (op === OPS.curveTo2 || op === OPS.curveTo3) { const q = xy.slice(k, k + 4); k += 4; seg("curve", [cx, cy, ...q]); cx = q[2]; cy = q[3]; }
          else if (op === OPS.rectangle) { const [x, y, w, h] = xy.slice(k, k + 4); k += 4; seg("rect", [x, y, x + w, y + h]); cx = sx = x; cy = sy = y; }
          else if (op === OPS.closePath) { if (cx !== sx || cy !== sy) seg("line", [cx, cy, sx, sy]); cx = sx; cy = sy; }
        }
      }
    }
    return {graphics, images};
  }

  const CAPTION = /^(Figure|Fig\.|Table|Listing|Diagram|Exhibit|Illustration|Image)\s+[\dA-Z]+([.\-]\d+)*\s*[:.]?/i;
  const itemBox = (it) => [it.x, it.base - 0.85 * it.size, it.x + it.w, it.base + 0.25 * it.size];
  const inside = (it, box, pad = 2) => {
    const cx = it.x + it.w / 2, cy = it.base - 0.3 * it.size;
    return box[0] - pad <= cx && cx <= box[2] + pad && box[1] - pad <= cy && cy <= box[3] + pad;
  };
  // A box drawn around ordinary text (a code listing, a callout): most lines start at one margin.
  const looksLikeText = (lines) => {
    if (lines.length < 2) return false;
    const left = Math.min(...lines.map((l) => l.x));
    return lines.filter((l) => Math.abs(l.x - left) < 2.5).length >= 0.6 * lines.length;
  };

  // Boxes made of drawings or images, merged when they touch (convert.py's figure_regions).
  function figureRegions(page) {
    const {width: w, height: h} = page;
    const items = [];
    for (const [x0, top, x1, bottom, kind] of page.graphics || []) {
      const gw = x1 - x0, gh = bottom - top;
      if ((gh < 2.5 && gw > 0.25 * w) || (gw > 0.9 * w && gh > 0.9 * h) || (gw < 0.5 && gh < 0.5)) continue;
      items.push({box: [x0, top, x1, bottom], curves: kind === "curve", image: false, parts: [[x0, top, x1, bottom]]});
    }
    for (const [x0, top, x1, bottom] of page.images || []) {
      if (x1 - x0 >= 16 && bottom - top >= 16 && !(x1 - x0 > 0.9 * w && bottom - top > 0.9 * h)) {
        items.push({box: [x0, top, x1, bottom], curves: false, image: true, parts: [[x0, top, x1, bottom]]});
      }
    }
    let regions = items, merged = true;
    while (merged) {
      merged = false;
      const out = [];
      for (const it of regions) {
        const m = out.find((m) => it.box[0] <= m.box[2] + 8 && m.box[0] <= it.box[2] + 8 && it.box[1] <= m.box[3] + 8 && m.box[1] <= it.box[3] + 8);
        if (m) {
          m.box = [Math.min(m.box[0], it.box[0]), Math.min(m.box[1], it.box[1]), Math.max(m.box[2], it.box[2]), Math.max(m.box[3], it.box[3])];
          m.curves ||= it.curves; m.image ||= it.image; m.parts = m.parts.concat(it.parts);
          merged = true;
        } else out.push({...it});
      }
      regions = out;
    }
    return regions;
  }

  // The figures on one page and the text items left for the flow (convert.py's page_layout).
  // A figure's labels come out of the text; its crop box takes them in.
  function figuresOnPage(page) {
    let items = page.items || [];
    const figures = [];
    for (const region of figureRegions(page)) {
      let box = region.box;
      const [bw, bh] = [box[2] - box[0], box[3] - box[1]];
      if (!region.image && (bw < 40 || bh < 25)) continue;
      if (!(region.image || region.curves || region.parts.length >= 3)) continue;   // a frame or a shaded background
      const within = items.filter((it) => inside(it, box));
      const interior = region.parts.some((g) => g[0] > box[0] + 3 && g[1] > box[1] + 3 && g[2] < box[2] - 3 && g[3] < box[3] - 3);
      if (!region.image && !interior && looksLikeText(linesFromItems(within))) continue;
      if (!region.image) {
        // labels of a drawing often sit beside or just outside it: take the whole band
        const band = [0, box[1] - 9, page.width, box[3] + 9];
        const beside = linesFromItems(items.filter((it) => inside(it, band, 0)));
        box = looksLikeText(beside) && beside.length > 3 ? region.box : band;
      }
      const pad = box[0] === 0 ? 0 : 2;
      const taken = items.filter((it) => inside(it, box, pad));
      items = items.filter((it) => !inside(it, box, pad));
      let crop = [...region.box];
      for (const it of taken) {
        const b = itemBox(it);
        crop = [Math.min(crop[0], b[0]), Math.min(crop[1], b[1]), Math.max(crop[2], b[2]), Math.max(crop[3], b[3])];
      }
      crop = [Math.max(0, crop[0] - 4), Math.max(0, crop[1] - 4), Math.min(page.width, crop[2] + 4), Math.min(page.height, crop[3] + 4)];
      figures.push({top: box[1], bottom: box[3], kind: region.image ? "image" : "drawing", caption: "", box: crop.map(tenth)});
    }
    // One figure made of pieces in a row becomes one figure.
    const joined = [];
    for (const f of figures.sort((p, q) => p.box[0] - q.box[0])) {
      const g = joined.find((g) => Math.min(f.bottom, g.bottom) - Math.max(f.top, g.top) > 0.5 * Math.min(f.bottom - f.top, g.bottom - g.top));
      if (g) {
        g.box = [Math.min(g.box[0], f.box[0]), Math.min(g.box[1], f.box[1]), Math.max(g.box[2], f.box[2]), Math.max(g.box[3], f.box[3])];
        g.top = Math.min(g.top, f.top); g.bottom = Math.max(g.bottom, f.bottom);
        if (f.kind === "image") g.kind = "image";
      } else joined.push({...f});
    }
    return {figures: joined.sort((p, q) => p.top - q.top), items};
  }

  // A caption sits just below or just above its figure, and may wrap onto two more lines.
  // Takes the caption lines out of `lines`.
  function takeCaptions(figures, lines) {
    for (const f of figures) {
      const near = lines.filter((l) => CAPTION.test(l.text.trim()) &&
        ((l.base - f.bottom >= 0 && l.base - f.bottom <= 3.5 * l.size) || (f.top - l.base >= 0 && f.top - l.base <= 2.5 * l.size)));
      if (!near.length) continue;
      const caption = [near[0]];
      for (const l of lines) {
        const last = caption.at(-1);
        if (caption.length < 3 && l.base - last.base > 0 && l.base - last.base <= 1.5 * l.size && l.size <= caption[0].size + 0.1) caption.push(l);
      }
      f.caption = caption.map((l) => l.text.trim()).join(" ");
      lines = lines.filter((l) => !caption.includes(l));
    }
    return lines;
  }

  // Running heads and page numbers: short first or last lines that repeat across pages.
  function furniture(pages) {
    const key = (text) => text.toLowerCase().replace(/\d+/g, "#").replace(/\s+/g, " ").trim();
    const counts = new Map();
    const candidates = pages.map((p) => {
      const ends = [p.lines[0], p.lines.at(-1)].filter((l, i, all) => l && all.indexOf(l) === i);
      return ends.filter((l) => countWords(l.text) <= 10 && (l.base < 0.15 * p.height || l.base > 0.85 * p.height));
    });
    candidates.flat().forEach((l) => counts.set(key(l.text), (counts.get(key(l.text)) || 0) + 1));
    const out = new Set();
    candidates.flat().forEach((l) => {
      if (/^[#\s]+$/.test(key(l.text)) || /^[ivxlc]{1,6}$/i.test(l.text.trim()) || counts.get(key(l.text)) >= 3) out.add(l);
    });
    return out;
  }

  function mode(values) {
    const n = new Map();
    let best = values[0], most = 0;
    for (const [v, w] of values.map((v) => (Array.isArray(v) ? v : [v, 1]))) {
      const c = (n.get(v) || 0) + w;
      n.set(v, c);
      if (c > most) { most = c; best = v; }
    }
    return best;
  }

  // pages: [{number, width, height, items, graphics?, images?}]. Returns blocks shaped like
  // convert.py's (id, type, page, top, end_page, bottom, level, text, words), without runs.
  // Pages with graphics or images (graphicsFromOps) also give figure blocks: {type: "figure",
  // kind, caption, box: [x0, top, x1, bottom]}, the box to crop from the page.
  function blocksFromPages(pages) {
    const read = pages.map((p) => {
      const {figures, items} = p.graphics || p.images ? figuresOnPage(p) : {figures: [], items: p.items || []};
      const lines = takeCaptions(figures, linesFromItems(items));
      return {...p, lines, figures};
    });
    const skip = furniture(read);
    const all = read.flatMap((p) => p.lines.filter((l) => !skip.has(l)));
    if (!all.length && !read.some((p) => p.figures.length)) return [];
    const body = mode(all.filter((l) => !l.mono).map((l) => [round(l.size, 0.5), l.text.length])) || 10;
    const gaps = [];
    for (const p of read) {
      const ls = p.lines.filter((l) => !skip.has(l) && round(l.size, 0.5) === body);
      for (let i = 1; i < ls.length; i++) {
        const g = ls[i].base - ls[i - 1].base;
        if (g > 0.8 * body && g < 2.5 * body) gaps.push(tenth(g));
      }
    }
    const spacing = mode(gaps) || body * 1.2;
    // Contents pages have dot leaders; index pages are mostly "term, 12, 34" lines.
    const aside = (lines) => {
      if (lines.filter((l) => /(\.\s?){5,}\s*[\dixvlc]+$/i.test(l.text)).length >= 5) return "Contents";
      if (lines.length >= 10 && lines.filter((l) => /,\s*\d+/.test(l.text)).length >= 0.6 * lines.length) return "Index";
      return "";
    };
    const kindOf = (l) => (l.mono ? "code" : l.size >= body * 1.15 && countWords(l.text) <= 15 ? "heading" : "paragraph");

    const blocks = [];
    let cur = null;
    const finish = () => { if (cur) blocks.push(cur); cur = null; };
    for (const p of read) {
      const lines = p.lines.filter((l) => !skip.has(l));
      const pageAside = aside(lines);
      const bodyLines = lines.filter((l) => kindOf(l) === "paragraph");
      const margin = bodyLines.length ? mode(bodyLines.map((l) => round(l.x, 1))) : 0;
      const right = bodyLines.length ? Math.max(...bodyLines.map((l) => l.right)) : p.width;
      let n = 0, prev = null;
      const events = [...p.figures.map((f) => ({at: f.top, figure: f})), ...lines.map((l) => ({at: l.base, line: l}))]
        .sort((x, y) => x.at - y.at || (x.figure ? -1 : 1));
      for (const {figure, line} of events) {
        if (figure) {
          finish();
          n += 1;
          blocks.push({id: `p${p.number}.${n}`, type: "figure", page: p.number, top: tenth(figure.top), end_page: p.number,
                       bottom: tenth(figure.bottom), text: "", kind: figure.kind, caption: figure.caption, box: figure.box});
          continue;
        }
        const kind = kindOf(line);
        let fresh = !cur || cur.type !== kind;
        if (!fresh && !prev) {
          // first line of a page: a paragraph or code carries on from the last page unless it clearly ended
          fresh = kind === "heading" || (kind === "paragraph" && (/[.!?:]["'”’)]?$/.test(cur.text) || line.x > margin + 0.8 * body));
        } else if (!fresh) {
          const gap = line.base - prev.base;
          if (kind === "heading") {   // a wrapped title stays one heading, and so does "Chapter 1" above its title
            const same = round(line.size, 0.5) === round(prev.size, 0.5);
            fresh = same ? gap > 2.2 * line.size : line.size < prev.size || gap > 3 * line.size;
          }
          else if (kind === "code") fresh = gap > 1.6 * spacing;
          else {
            fresh = gap > 1.4 * spacing ||
              (line.x > margin + 0.8 * body && line.x < margin + 5 * body && prev.x <= margin + 0.3 * body) ||
              (prev.right < right - 3 * body && /[.!?:]["'”’)]?$/.test(prev.text));
          }
        }
        if (fresh) {
          finish();
          n += 1;
          cur = {id: `p${p.number}.${n}`, type: kind, page: p.number, top: tenth(line.base - 0.85 * line.size),
                 end_page: p.number, bottom: 0, text: "", size: line.size, left: line.x};
          if (pageAside) cur.aside = pageAside;
        }
        const sep = !cur.text ? "" : kind === "code" || (kind === "heading" && round(line.size, 0.5) !== round(prev.size, 0.5)) ? "\n"
          : /-$/.test(cur.text) ? "" : " ";
        cur.size = Math.max(cur.size, line.size);
        const indent = kind === "code" ? " ".repeat(Math.max(0, Math.round((line.x - cur.left) / (line.charWidth || 6)))) : "";
        cur.text += sep + indent + line.text;
        cur.end_page = p.number;
        cur.bottom = tenth(line.base + 0.3 * line.size);
        prev = line;
      }
    }
    finish();
    const sizes = [...new Set(blocks.filter((b) => b.type === "heading").map((b) => round(b.size, 0.5)))].sort((x, y) => y - x);
    return blocks.map(({size, left, ...b}) => {
      if (b.type === "heading") b.level = sizes.indexOf(round(size, 0.5)) + 1;
      b.words = countWords(b.text);
      return b;
    });
  }

  // The first block at or below a position on a page. With `words`, a block on that page
  // whose text starts with them wins, nearest first, since positions can shift a little.
  function blockAt(blocks, page, y, words) {
    const norm = (s) => String(s || "").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
    const want = norm(words).split(" ").slice(0, 6).join(" ");
    if (want) {
      let best = -1;
      blocks.forEach((b, j) => {
        if (b.page === page && norm(b.text || b.caption).startsWith(want) &&
            (best < 0 || Math.abs(b.top - y) < Math.abs(blocks[best].top - y))) best = j;
      });
      if (best >= 0) return best;
    }
    const j = blocks.findIndex((b) => b.page > page || (b.page === page && b.top >= y - 3));
    return j < 0 ? blocks.length : j;
  }

  function firstWords(block, n = 8) {
    return String(block.text || block.caption || "").split(/\s+/).filter(Boolean).slice(0, n).join(" ");
  }

  // outline: the PDF's top-level bookmarks as [{title, page, y}], y from the top of the page or null.
  // Returns the block indexes where chapters start: at the bookmarks when there are some,
  // otherwise at the largest kind of heading that appears more than once.
  function chapterStarts(blocks, outline) {
    const starts = new Set();
    for (const o of outline || []) {
      const onPage = blocks.map((b, j) => j).filter((j) => blocks[j].page === o.page);
      const norm = (s) => String(s || "").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
      const titled = onPage.find((j) => blocks[j].type === "heading" && norm(blocks[j].text).includes(norm(o.title)));
      const j = titled ?? blockAt(blocks, o.page, o.y ?? 0);
      if (j < blocks.length) starts.add(j);
    }
    if (starts.size < 2) {
      starts.clear();
      const counts = new Map();
      blocks.forEach((b) => { if (b.type === "heading") counts.set(b.level, (counts.get(b.level) || 0) + 1); });
      const level = [...counts.keys()].sort((x, y) => x - y).find((l) => counts.get(l) >= 2);
      blocks.forEach((b, j) => { if (b.type === "heading" && b.level === level) starts.add(j); });
    }
    // the contents and the index start parts of their own, which defaultSegments skips
    blocks.forEach((b, j) => { if (b.type === "heading" && SKIP_TITLES.test(headingLabel(b.text))) starts.add(j); });
    return [...starts].sort((x, y) => x - y);
  }

  const SKIP_TITLES = /^(contents|table of contents|index|bibliography)$/i;

  // The starting cut for a bare PDF: one lesson per chapter, with what comes before the
  // first chapter, the contents and the index skipped. A book with no chapters to find
  // is cut every `target` words at a paragraph.
  function defaultSegments(blocks, starts, target = 900) {
    let js = [...new Set(starts)].filter((j) => j >= 0 && j < blocks.length).sort((x, y) => x - y);
    if (!js.length) {
      js = [0];
      let n = 0;
      blocks.forEach((b, j) => {
        if (n >= target && j > 0) { js.push(j); n = 0; }
        n += b.words || 0;
      });
    }
    const segs = [];
    if (js[0] > 0) segs.push({kind: "skip", a: 0, b: js[0] - 1, subject: "", lead_in: "", teaser: "", reason: "Before the first chapter"});
    js.forEach((j, i) => {
      const b = i + 1 < js.length ? js[i + 1] - 1 : blocks.length - 1;
      const subject = subjectAt(blocks, j);
      const skip = SKIP_TITLES.test(subject);
      segs.push({kind: skip ? "skip" : "lesson", a: j, b, subject: skip ? "" : subject, lead_in: "", teaser: "",
                 reason: skip ? subject : ""});
    });
    // contents and index pages inside a part become skipped parts of their own
    const pieces = [];
    for (const s of segs) {
      let a = s.a;
      for (let j = s.a + 1; j <= s.b + 1; j++) {
        if (j <= s.b && (blocks[j].aside || "") === (blocks[a].aside || "")) continue;
        if (blocks[a].aside) pieces.push({...s, kind: "skip", a, b: j - 1, subject: "", reason: blocks[a].aside});
        else pieces.push({...s, a, b: j - 1, subject: s.kind === "lesson" && a !== s.a ? subjectAt(blocks, a) : s.subject});
        a = j;
      }
    }
    return syncTeasers(pieces);
  }

  // Split a long lesson at its headings, into parts of about `target` words.
  function splitAtSections(segs, k, blocks, target = 900) {
    const seg = segs[k];
    if (!seg || seg.kind !== "lesson") return null;
    let out = segs, start = seg.a, at = k;
    for (let j = seg.a + 1; j <= seg.b; j++) {
      if (blocks[j].type !== "heading") continue;
      const before = words(blocks, {a: start, b: j - 1}), after = words(blocks, {a: j, b: seg.b});
      if (before >= 0.7 * target && after >= 0.4 * target) {
        out = split(out, at, j, blocks);
        at += 1;
        start = j;
      }
    }
    return out === segs ? null : out;
  }

  // Every lesson longer than the warning limit, split at its sections.
  function splitLong(segs, blocks, target = 900) {
    let out = segs;
    for (let k = segs.length - 1; k >= 0; k--) {
      if (segs[k].kind === "lesson" && minutes(blocks, segs[k]) > WARN[1]) out = splitAtSections(out, k, blocks, target) || out;
    }
    return out === segs ? null : out;
  }

  // cuts.json: where each lesson or skipped part starts, as a page and a height on it in
  // points from the top, plus its first words. convert.py --cuts reads it.
  function toCuts(meta, blocks, segs) {
    return {
      format: "dripbook-cuts/1",
      book: meta.book || {},
      source: meta.source || {},
      settings: meta.settings || {},
      edited: new Date().toISOString().slice(0, 19) + "Z",
      segments: segs.map((s) => {
        const at = s.a === 0 ? {page: blocks[0].page, y: 0} : boundaryPos(blocks, s.a);
        const out = {kind: s.kind, page: at.page, y: Math.round(at.y * 10) / 10, first_words: firstWords(blocks[s.a])};
        if (s.kind === "lesson") Object.assign(out, {subject: s.subject, lead_in: s.lead_in, teaser: s.teaser});
        else out.reason = s.reason;
        return out;
      }),
    };
  }

  // Put a saved cuts.json back onto blocks. Cuts that land on the same block are dropped.
  function fromCuts(cuts, blocks) {
    const segs = [];
    for (const c of cuts.segments || []) {
      const a = segs.length ? blockAt(blocks, c.page, c.y, c.first_words) : 0;
      if (a >= blocks.length || (segs.length && a <= segs.at(-1).a)) continue;
      if (segs.length) segs.at(-1).b = a - 1;
      segs.push({kind: c.kind === "skip" ? "skip" : "lesson", a, b: blocks.length - 1, subject: c.subject || "",
                 lead_in: c.lead_in || "", teaser: c.teaser || "", reason: c.reason || ""});
    }
    if (!segs.length) throw new Error("This cuts.json has no cuts.");
    return segs;
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
          boundariesOnPage, stats, itemsFromText, linesFromItems, graphicsFromOps, blocksFromPages, blockAt, chapterStarts, defaultSegments,
          splitAtSections, splitLong, toCuts, fromCuts};
});
