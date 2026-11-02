// Tests for editor-core.js: node --test converter/test_editor_core.mjs
// No Node installed? uvx --from nodejs-wheel node --test converter/test_editor_core.mjs
import {test} from "node:test";
import assert from "node:assert/strict";
import {createRequire} from "node:module";

const C = createRequire(import.meta.url)("./editor-core.js");

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
  assert.equal(out.planned_by, "edited in editor.html");
});
