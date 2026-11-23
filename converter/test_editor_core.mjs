// Tests for the cut editor's logic, site/cuts-core.js: node --test converter/test_editor_core.mjs
// No Node installed? uvx --from nodejs-wheel node --test converter/test_editor_core.mjs
import {test} from "node:test";
import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {readFileSync} from "node:fs";
import {createContext, runInContext} from "node:vm";

const C = createRequire(import.meta.url)("../site/cuts-core.js");

// Run the page's actual event handlers without loading PDF.js or rendering canvases.
function editorPage(storage = new Map()) {
  const elements = new Map();
  const element = (selector) => {
    if (!elements.has(selector)) elements.set(selector, {
      hidden: true, listeners: new Map(),
      addEventListener(type, listener) { this.listeners.set(type, listener); },
    });
    return elements.get(selector);
  };
  const context = createContext({
    window: {DripbookCuts: C, addEventListener() {}},
    document: {querySelector: element, addEventListener() {}},
    location: {search: ""}, URLSearchParams,
    localStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key),
    },
  });
  const html = readFileSync(new URL("../site/cuts.html", import.meta.url), "utf8");
  runInContext(html.match(/<script type="module">([\s\S]*?)<\/script>/)[1], context);
  runInContext("refresh = () => {};", context);
  const state = context.window.dripbookEditor;
  Object.assign(state, {plan: plan(), ...C.fromPlan(plan()), key: "book", mode: "plan"});
  return {state, element, storage, run: (code) => runInContext(code, context)};
}

test("merging the last lesson with the previous one selects the merged lesson", () => {
  const {state, element} = editorPage();
  state.k = 1;
  element("#panel").listeners.get("click")({target: {closest: () => ({dataset: {act: "merge-prev"}})}});
  assert.equal(state.k, 0);
  assert.equal(state.segs.length, 1);
  assert.equal(state.segs[state.k].subject, "Chapter 1: Start");
  assert.equal(state.undo.length, 1);
});

test("restores a draft when only the free lesson count changed", () => {
  const first = editorPage();
  first.element("#free").listeners.get("change")({target: {value: "2"}});
  const reloaded = editorPage(first.storage);
  reloaded.run("offerDraft()");
  assert.equal(reloaded.element("#draft").hidden, false);
  reloaded.element("#draft").onclick({target: {dataset: {act: "restore"}}});
  assert.equal(reloaded.state.plan.settings.free_lessons, 2);
  assert.equal(reloaded.element("#free").value, 2);
  assert.equal(reloaded.state.saved, false);
  assert.equal(reloaded.element("#draft").hidden, true);
});

test("does not offer an unchanged draft", () => {
  const page = editorPage();
  page.run("saveDraft(); offerDraft()");
  assert.equal(page.element("#draft").hidden, true);
});

// Blocks on pages 1-3: a chapter heading, paragraphs, a list, code and a section heading.
function book() {
  const b = (id, type, page, top, bottom, words, extra = {}) => ({id, type, page, top, end_page: page, bottom, words, ...extra});
  return [
    b("p1.1", "heading", 1, 80, 110, 3, {level: 1, text: "Chapter 1\nStart"}),
    b("p1.2", "paragraph", 1, 130, 300, 400, {text: "Words"}),
    b("p1.3", "paragraph", 1, 320, 500, 300, {text: "Look at this:"}),
    b("p1.4", "code", 1, 510, 560, 20, {text: "x = 1"}),
    b("p2.1", "list_item", 2, 80, 120, 200, {marker: "•", text: "one"}),
    b("p2.2", "list_item", 2, 125, 160, 200, {marker: "•", text: "two"}),
    b("p2.3", "heading", 2, 200, 220, 2, {level: 2, text: "1.1 More"}),
    b("p2.4", "paragraph", 2, 240, 700, 700, {text: "More words"}),
    b("p3.1", "paragraph", 3, 80, 400, 500, {text: "Even more"}),
  ];
}

function plan() {
  return {
    book: {title: "T"}, source: {file: "t.pdf"}, settings: {free_lessons: 1},
    lessons: [
      {first_block: "p1.1", last_block: "p2.2", subject: "Chapter 1: Start", lead_in: "", teaser: "1.1 More"},
      {first_block: "p2.3", last_block: "p3.1", subject: "1.1 More", lead_in: "", teaser: ""},
    ],
    skipped: [],
    all_blocks: book(),
  };
}

test("reads a plan into segments that tile the book", () => {
  const {blocks, segs} = C.fromPlan(plan());
  assert.equal(blocks.length, 9);
  assert.deepEqual(segs.map((s) => [s.kind, s.a, s.b]), [["lesson", 0, 5], ["lesson", 6, 8]]);
});

test("rejects a plan with a gap or without all_blocks", () => {
  const p = plan();
  p.lessons[1].first_block = "p2.4";
  assert.throws(() => C.fromPlan(p), /in order/);
  assert.throws(() => C.fromPlan({lessons: []}), /all_blocks/);
});

test("moves a cut within the two lessons it separates", () => {
  const {segs} = C.fromPlan(plan());
  const moved = C.moveCut(segs, 1, 4);
  assert.deepEqual(moved.map((s) => [s.a, s.b]), [[0, 3], [4, 8]]);
  assert.equal(C.moveCut(segs, 1, 0), null, "the first lesson can't become empty");
  assert.equal(C.moveCut(segs, 1, 9), null, "the second lesson can't become empty");
  assert.equal(C.moveCut(segs, 0, 3), null, "there is no cut before the first lesson");
  assert.deepEqual(segs.map((s) => [s.a, s.b]), [[0, 5], [6, 8]], "edits never change the list they're given");
});

test("an automatic subject follows the cut; an edited one stays", () => {
  const {blocks, segs} = C.fromPlan(plan());
  assert.equal(C.moveCut(segs, 1, 5, blocks)[1].subject, "Chapter 1: Start (continued)");
  const edited = C.setText(segs, 1, "subject", "My own title");
  assert.equal(C.moveCut(edited, 1, 5, blocks)[1].subject, "My own title");
});

test("splits a lesson and names the new one after its heading", () => {
  const {blocks, segs} = C.fromPlan(plan());
  const out = C.split(segs, 1, 8, blocks);
  assert.deepEqual(out.map((s) => [s.a, s.b, s.subject]),
                   [[0, 5, "Chapter 1: Start"], [6, 7, "1.1 More"], [8, 8, "1.1 More (continued)"]]);
  assert.equal(C.split(segs, 1, 6, blocks), null, "a split must leave something before it");
});

test("merges and skips", () => {
  const {blocks, segs} = C.fromPlan(plan());
  const merged = C.merge(segs, 0);
  assert.deepEqual(merged.map((s) => [s.a, s.b, s.subject]), [[0, 8, "Chapter 1: Start"]]);
  const skipped = C.setKind(segs, 0, "skip", blocks);
  assert.equal(skipped[0].kind, "skip");
  assert.deepEqual(C.days(skipped), [null, 1]);
  const back = C.merge(skipped, 0);
  assert.equal(back[0].kind, "lesson", "a skipped range merged into a lesson becomes part of it");
  assert.equal(back[0].subject, "1.1 More");
});

test("keeps Tomorrow lines in step with the next subject", () => {
  const {blocks, segs} = C.fromPlan(plan());
  const out = C.syncTeasers(C.split(segs, 1, 8, blocks));
  assert.deepEqual(out.map((s) => s.teaser), ["1.1 More", "1.1 More (continued)", ""]);
});

test("warns about lessons that end badly or run long", () => {
  const {blocks} = C.fromPlan(plan());
  assert.deepEqual(C.warnings(blocks, {kind: "lesson", a: 0, b: 2}), ["Ends just before the code it introduces"]);
  assert.deepEqual(C.warnings(blocks, {kind: "lesson", a: 4, b: 4}), ["Short: about 0.9 minutes", "Ends in the middle of a list"]);
  assert.match(C.warnings(blocks, {kind: "lesson", a: 0, b: 8})[0], /^Long/);
});

test("places cuts between blocks and bands on pages", () => {
  const {blocks, segs} = C.fromPlan(plan());
  assert.deepEqual(C.boundaryPos(blocks, 2), {page: 1, y: 310});
  assert.deepEqual(C.boundaryPos(blocks, 4), {page: 2, y: 76}, "a block at the top of a page is cut just above it");
  assert.deepEqual(C.regionsOnPage(blocks, segs, 2, 800), [{k: 0, top: 0, bottom: 180}, {k: 1, top: 180, bottom: 800}]);
  assert.deepEqual(C.boundariesOnPage(blocks, 2).map((x) => x.j), [4, 5, 6, 7]);
});

test("writes the plan back with block ids, keeping the book's details", () => {
  const p = plan();
  const {blocks, segs} = C.fromPlan(p);
  const out = C.toPlan(p, blocks, C.moveCut(segs, 1, 4));
  assert.deepEqual(out.lessons.map((l) => [l.first_block, l.last_block]), [["p1.1", "p1.4"], ["p2.1", "p3.1"]]);
  assert.equal(out.book.title, "T");
  assert.equal(out.all_blocks.length, 9);
  assert.equal(out.planned_by, "edited in the cut editor");
});

// ------------------------------------------------------------- a bare PDF, read in the browser

// A five-page book as PDF.js would give it: [text, base, size, x, mono] per item.
function pdfPages() {
  const items = (rows) => rows.map(([text, base, size = 10, x = 72, mono = false]) => ({text, base, size, x, w: text.length * size * 0.5, mono}));
  const full = (s) => s.padEnd(80, " filler words");
  return [
    {number: 1, width: 600, height: 800, items: items([["My Book", 300, 30]])},
    {number: 2, width: 600, height: 800, items: items([["Contents", 100, 24],
      ...[1, 2, 3, 4, 5, 6].map((n) => [`${n} Section . . . . . . . . ${n + 2}`, 150 + n * 14])])},
    {number: 3, width: 600, height: 800, items: items([["3 My Book", 40], ["Chapter 1", 120, 20], ["Start", 170, 25],
      [full("The first paragraph"), 220], [full("goes on"), 232], ["and ends.", 244],
      ["x = 1", 262, 10, 82, true], ["print(x)", 274, 10, 82, true],
      [full("A second paragraph"), 300], ["that runs onto the next", 312], ["3", 760]])},
    {number: 4, width: 600, height: 800, items: items([["4 My Book", 40], ["page, where it ends.", 80],
      ["1.1 More", 120, 14], [full("Text under the section"), 140], ["ends.", 152],
      ["Chapter 2", 300, 20], ["Next", 350, 25], ["Words of chapter two.", 400], ["4", 760]])},
    {number: 5, width: 600, height: 800, items: items([["5 My Book", 40], ["Index", 100, 24],
      ...[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((n) => [`term${n}, ${n}, ${n + 3}`, 150 + n * 14]), ["5", 760]])},
  ];
}

test("joins a line's pieces with spaces where there's a gap", () => {
  const [line] = C.linesFromItems([
    {text: "two", x: 40, w: 15, base: 100, size: 10, mono: false},
    {text: "One", x: 10, w: 15, base: 100.4, size: 10, mono: false},
    {text: "x()", x: 60, w: 15, base: 100, size: 10, mono: true},
  ]);
  assert.equal(line.text, "One two x()");
  assert.equal(line.mono, false);
});

test("reads pages into headings, paragraphs and code, without running heads", () => {
  const blocks = C.blocksFromPages(pdfPages());
  const p3 = blocks.filter((b) => b.page === 3);
  assert.deepEqual(p3.map((b) => b.type), ["heading", "paragraph", "code", "paragraph"]);
  assert.equal(p3[0].text, "Chapter 1\nStart", "a chapter label joins the title below it");
  assert.equal(p3[2].text, "x = 1\nprint(x)");
  assert.ok(!blocks.some((b) => /My Book$/.test(b.text) && b.page > 1), "running heads are dropped");
  assert.ok(!blocks.some((b) => /^\d$/.test(b.text)), "page numbers are dropped");
  const carried = blocks.find((b) => /runs onto the next/.test(b.text));
  assert.equal(carried.type, "paragraph");
  assert.match(carried.text, /next page, where it ends\.$/, "a paragraph carries on over a page break");
  assert.equal(carried.end_page, 4);
  assert.ok(blocks.filter((b) => b.page === 2).every((b) => b.aside === "Contents"));
  assert.ok(blocks.filter((b) => b.page === 5).every((b) => b.aside === "Index"));
  const more = blocks.find((b) => b.text === "1.1 More");
  assert.ok(more.level > blocks.find((b) => b.text === "Chapter 1\nStart").level, "smaller headings get deeper levels");
});

test("cuts a bare PDF into one lesson per chapter, skipping the front matter, contents and index", () => {
  const blocks = C.blocksFromPages(pdfPages());
  const fromHeadings = C.chapterStarts(blocks, []);
  const fromOutline = C.chapterStarts(blocks, [{title: "Start", page: 3, y: 60}, {title: "Next", page: 4, y: null}]);
  assert.deepEqual(fromOutline, fromHeadings, "bookmarks and headings find the same chapters");
  const segs = C.defaultSegments(blocks, fromOutline);
  assert.deepEqual(segs.map((s) => [s.kind, s.subject || s.reason]), [
    ["skip", "Before the first chapter"], ["skip", "Contents"], ["lesson", "Chapter 1: Start"],
    ["lesson", "Chapter 2: Next"], ["skip", "Index"]]);
  assert.equal(segs[2].teaser, "Chapter 2: Next");
  assert.equal(segs.at(-1).b, blocks.length - 1);
});

test("splits a long lesson at its sections", () => {
  const blocks = C.blocksFromPages(pdfPages());
  const segs = C.defaultSegments(blocks, C.chapterStarts(blocks, []));
  const k = segs.findIndex((s) => s.subject === "Chapter 1: Start");
  const out = C.splitAtSections(segs, k, blocks, 20);
  assert.deepEqual(out.slice(k, k + 2).map((s) => s.subject), ["Chapter 1: Start", "1.1 More"]);
  assert.equal(C.splitAtSections(segs, k, blocks, 10000), null, "nothing to split when the lesson is short");
});

test("cuts.json records positions, and reads back onto the same blocks", () => {
  const blocks = C.blocksFromPages(pdfPages());
  const segs = C.defaultSegments(blocks, C.chapterStarts(blocks, []));
  const cuts = C.toCuts({book: {title: "My Book"}, settings: {free_lessons: 1}}, blocks, segs);
  assert.equal(cuts.format, "dripbook-cuts/1");
  assert.deepEqual(cuts.segments.map((c) => c.kind), segs.map((s) => s.kind));
  const chapter2 = cuts.segments[3];
  assert.equal(chapter2.page, 4);
  assert.equal(chapter2.first_words, "Chapter 2 Next");
  assert.deepEqual(C.fromCuts(cuts, blocks).map((s) => [s.a, s.b, s.subject]), segs.map((s) => [s.a, s.b, s.subject]));
  chapter2.y += 40;   // a reader that measures positions a little differently still finds the block by its words
  assert.equal(C.fromCuts(cuts, blocks)[3].a, segs[3].a);
});
