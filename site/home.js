// Dripbook home page: the dripping quote, the library shelves, the reading planner and sign-up.
(function () {
  'use strict';

  var WORDS_PER_MINUTE = 230;
  var COVER_COLORS = ['#7B2232', '#1F4A3B', '#1E3A66', '#8A6420', '#4A2D5C', '#1F5F68', '#5B3A24', '#33415C'];

  // Opening lines, checked against the Gutenberg texts. Clauses arrive one at a time.
  var OPENINGS = [
    { id: 1342, title: 'Pride and Prejudice', author: 'Jane Austen', words: 127360,
      clauses: ['It is a truth universally acknowledged, ', 'that a single man in possession of a good fortune ', 'must be in want of a wife.'] },
    { id: 2701, title: 'Moby Dick', author: 'Herman Melville', words: 212796,
      clauses: ['Call me Ishmael. ', 'Some years ago—never mind how long precisely—', 'having little or no money in my purse, ', 'and nothing particular to interest me on shore, ', 'I thought I would sail about a little ', 'and see the watery part of the world.'] },
    { id: 98, title: 'A Tale of Two Cities', author: 'Charles Dickens', words: 135886,
      clauses: ['It was the best of times, ', 'it was the worst of times, ', 'it was the age of wisdom, ', 'it was the age of foolishness…'] },
    { id: 84, title: 'Frankenstein', author: 'Mary Shelley', words: 75042,
      clauses: ['You will rejoice to hear ', 'that no disaster has accompanied the commencement of an enterprise ', 'which you have regarded with such evil forebodings.'] },
    { id: 64317, title: 'The Great Gatsby', author: 'F. Scott Fitzgerald', words: 48208,
      clauses: ['In my younger and more vulnerable years ', 'my father gave me some advice ', 'that I’ve been turning over in my mind ever since.'] }
  ];

  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var state = { book: OPENINGS[0], pace: 10 };
  var books = {};                 // id -> book, from the library and the openings
  OPENINGS.forEach(function (b) { books[b.id] = b; });

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function el(tag, attrs, text) {
    var n = document.createElement(tag);
    for (var k in attrs || {}) n.setAttribute(k, attrs[k]);
    if (text != null) n.textContent = text;
    return n;
  }
  function days(book, pace) {
    return book.words ? Math.max(1, Math.ceil(book.words / (WORDS_PER_MINUTE * pace))) : null;
  }

  // --- The chosen book flows through the whole page ---------------------------------
  function choose(book) {
    state.book = book;
    $$('[data-book-button]').forEach(function (b) { b.textContent = 'Start ' + book.title; });
    $$('[data-book-name]').forEach(function (n) { n.textContent = book.title; });
    var sel = $('#planner-book');
    if (sel && !$('option[value="' + book.id + '"]', sel) && book.words) {
      sel.appendChild(el('option', { value: book.id }, book.title));
    }
    if (sel && book.words) sel.value = String(book.id);
    renderPlan();
  }

  // --- Hero: the opening line drips in, then the drop lands on "day 1" -------------
  var quote = $('#quote'), reading = $('#reading'), picker = $('#picker');
  var heroIndex = 0, timer = null, autoplay = !reduceMotion;

  OPENINGS.forEach(function (b, i) {
    var btn = el('button', { type: 'button', 'aria-pressed': i === 0 ? 'true' : 'false' }, b.title);
    btn.addEventListener('click', function () { autoplay = false; clearTimeout(timer); showOpening(i); choose(b); });
    picker.appendChild(btn);
  });

  function showOpening(i) {
    heroIndex = i;
    var b = OPENINGS[i];
    $$('button', picker).forEach(function (btn, j) { btn.setAttribute('aria-pressed', j === i ? 'true' : 'false'); });
    quote.textContent = '';
    quote.setAttribute('cite', 'https://www.gutenberg.org/ebooks/' + b.id);
    var text = b.clauses.join('');
    if (text.length > 150) quote.setAttribute('data-long', ''); else quote.removeAttribute('data-long');
    b.clauses.forEach(function (c, j) {
      var span = el('span', { class: 'clause', style: '--i:' + j }, c);
      quote.appendChild(span);
    });
    $('#where-title').textContent = b.title;
    $('#where-days').textContent = days(b, 10);
    reading.style.setProperty('--land', (b.clauses.length * 0.26 + 0.35) + 's');
    if (!reduceMotion) {
      reading.classList.remove('drip-on');
      void reading.offsetWidth;  // restart the animation
      reading.classList.add('drip-on');
    }
  }

  function cycle() {
    if (!autoplay) return;
    timer = setTimeout(function () {
      if (!autoplay) return;
      quote.classList.add('leaving');
      setTimeout(function () {
        quote.classList.remove('leaving');
        var next = (heroIndex + 1) % OPENINGS.length;
        showOpening(next);
        choose(OPENINGS[next]);
        cycle();
      }, 400);
    }, 7000 + OPENINGS[heroIndex].clauses.length * 260);
  }
  // Pause the rotation while someone is reading or using the hero.
  ['mouseenter', 'focusin'].forEach(function (ev) {
    $('.hero').addEventListener(ev, function () { clearTimeout(timer); });
  });
  ['mouseleave', 'focusout'].forEach(function (ev) {
    $('.hero').addEventListener(ev, function () { clearTimeout(timer); cycle(); });
  });

  // --- Library shelves ------------------------------------------------------------
  function colorFor(id) { return COVER_COLORS[id % COVER_COLORS.length]; }

  function fillCover(node, book) {
    node.style.setProperty('--c', colorFor(book.id));
    node.textContent = '';
    var t = el('span', { class: 't' + (book.title.length > 34 ? ' long' : '') }, book.title);
    var a = el('span', { class: 'a' }, book.author);
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'mark'); svg.setAttribute('aria-hidden', 'true');
    var use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#drop'); svg.appendChild(use);
    node.appendChild(t); node.appendChild(a); node.appendChild(svg);
  }

  function lengthLabel(book) {
    var d = days(book, 10);
    return d ? d + (d === 1 ? ' day' : ' days') + ' at 10 min' : 'Daily, 10 min';
  }

  function bookButton(book) {
    var btn = el('button', { type: 'button', class: 'book', 'aria-label': book.title + ' by ' + book.author + ', ' + lengthLabel(book) });
    var cover = el('span', { class: 'cover', 'aria-hidden': 'true' });
    fillCover(cover, book);
    btn.appendChild(cover);
    btn.appendChild(el('span', { class: 'len', 'aria-hidden': 'true' }, lengthLabel(book)));
    btn.addEventListener('click', function () { openBook(book); });
    return btn;
  }

  function shelf(row, index) {
    var wrap = el('div', { class: 'shelf' });
    var head = el('div', { class: 'shelf-head' });
    var id = 'shelf-' + index;
    head.appendChild(el('h3', { id: id }, row.name));
    var arrows = el('div', { class: 'arrows' });
    var prev = el('button', { type: 'button', 'aria-label': 'Scroll ' + row.name + ' left' });
    var next = el('button', { type: 'button', 'aria-label': 'Scroll ' + row.name + ' right' });
    prev.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M10 3 5 8l5 5"/></svg>';
    next.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m6 3 5 5-5 5"/></svg>';
    arrows.appendChild(prev); arrows.appendChild(next);
    head.appendChild(arrows);
    var list = el('div', { class: 'row', role: 'list', 'aria-labelledby': id });
    row.books.forEach(function (b, i) {
      books[b.id] = books[b.id] || b;
      var item = el('div', { role: 'listitem', class: row.top ? 'ranked' : '' });
      if (row.top) item.appendChild(el('span', { class: 'rank', 'aria-hidden': 'true' }, String(i + 1)));
      item.appendChild(bookButton(b));
      list.appendChild(item);
    });
    function step(dir) {
      list.scrollBy({ left: dir * list.clientWidth * 0.85, behavior: reduceMotion ? 'auto' : 'smooth' });
    }
    function update() {
      prev.disabled = list.scrollLeft < 8;
      next.disabled = list.scrollLeft + list.clientWidth > list.scrollWidth - 8;
    }
    prev.addEventListener('click', function () { step(-1); });
    next.addEventListener('click', function () { step(1); });
    list.addEventListener('scroll', update, { passive: true });
    window.addEventListener('resize', update);
    setTimeout(update, 0);
    wrap.appendChild(head); wrap.appendChild(list);
    return wrap;
  }

  function loadLibrary() {
    var target = $('#shelves');
    fetch('library.json').then(function (r) { return r.json(); }).then(function (data) {
      target.textContent = '';
      data.rows.forEach(function (row, i) { target.appendChild(shelf(row, i)); });
      fillPlannerOptions(data);
    }).catch(function () {
      target.textContent = '';
      target.appendChild(el('p', { class: 'small' }, 'The library could not load. Refresh the page to try again.'));
      fillPlannerOptions(null);
    });
  }

  // --- Book dialog ----------------------------------------------------------------
  var dialog = $('#book-dialog');
  function openBook(book) {
    fillCover($('#dlg-cover'), book);
    $('#dlg-title').textContent = book.title;
    $('#dlg-by').textContent = 'by ' + book.author;
    var facts = $('#dlg-facts');
    facts.textContent = '';
    var d10 = days(book, 10), d20 = days(book, 20);
    if (d10) {
      facts.appendChild(el('li', null, d10 + ' mornings at 10 minutes a day'));
      facts.appendChild(el('li', null, d20 + ' mornings at 20 minutes a day'));
    } else {
      facts.appendChild(el('li', null, 'Daily parts of about 10 minutes'));
    }
    facts.appendChild(el('li', null, 'Complete and unabridged'));
    if (book.category) facts.appendChild(el('li', null, book.category));
    $('#dlg-start').textContent = 'Start ' + book.title;
    $('#dlg-start').onclick = function () {
      choose(book);
      dialog.close();
      $('#start').scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth' });
      setTimeout(function () { $('#email-bottom').focus({ preventScroll: true }); }, reduceMotion ? 0 : 500);
    };
    if (typeof dialog.showModal === 'function') dialog.showModal();
  }
  $('[data-close]', dialog).addEventListener('click', function () { dialog.close(); });
  dialog.addEventListener('click', function (e) { if (e.target === dialog) dialog.close(); });

  // --- Reading planner ------------------------------------------------------------
  function fillPlannerOptions(data) {
    var sel = $('#planner-book');
    var seen = {};
    sel.textContent = '';
    var list = OPENINGS.slice();
    if (data) data.rows.forEach(function (r) { r.books.forEach(function (b) { if (b.words) list.push(b); }); });
    list.forEach(function (b) {
      if (seen[b.id]) return;
      seen[b.id] = true;
      sel.appendChild(el('option', { value: b.id }, b.title));
    });
    sel.value = String(state.book.id);
    renderPlan();
  }
  $('#planner-book').addEventListener('change', function (e) {
    var b = books[e.target.value];
    if (b) { autoplay = false; clearTimeout(timer); choose(b); }
  });
  $$('[data-pace]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      state.pace = Number(btn.getAttribute('data-pace'));
      $$('[data-pace]').forEach(function (b) { b.setAttribute('aria-pressed', b === btn ? 'true' : 'false'); });
      renderPlan();
    });
  });

  function renderPlan() {
    var b = state.book, d = days(b, state.pace);
    var finish = $('#finish'), sub = $('#finish-sub');
    if (!finish) return;
    finish.textContent = '';
    if (!d) {
      finish.appendChild(document.createTextNode('Pick a book to see when you’d finish it.'));
      sub.textContent = '';
      return;
    }
    var end = new Date();
    end.setDate(end.getDate() + d);
    var when = end.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long', year: end.getFullYear() !== new Date().getFullYear() ? 'numeric' : undefined });
    finish.appendChild(document.createTextNode('You’d finish '));
    finish.appendChild(el('strong', null, b.title));
    finish.appendChild(document.createTextNode(' in ' + d + (d === 1 ? ' morning.' : ' mornings.')));
    sub.textContent = 'Start tomorrow and you’re done by ' + when + '. About ' +
      Math.round(b.words / 1000) + ',000 words, ' + state.pace + ' minutes at a time.';
  }

  // --- Sign-up (not live yet) -----------------------------------------------------
  $$('[data-join]').forEach(function (form) {
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var note = form.parentNode.querySelector('.note');
      note.textContent = 'Thanks. Dripbook isn’t sending yet, so nothing was saved. Sign-up opens soon.';
    });
  });

  // --- Start ------------------------------------------------------------------------
  showOpening(0);
  choose(OPENINGS[0]);
  loadLibrary();
  cycle();
})();
