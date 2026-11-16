// Dripbook home page: rows of covers, genres, search and the book window. Data: library.json (scripts/build_library.py).
(function () {
  'use strict';

  var WORDS_PER_MINUTE = 230;
  var COVER_COLORS = ['#7B2232', '#1F4A3B', '#1E3A66', '#8A6420', '#4A2D5C', '#1F5F68', '#5B3A24', '#33415C'];
  var NOTE = "Thanks. Dripbook isn't sending yet, so nothing was saved. Sign-up opens soon.";

  var lib = null;            // library.json
  var current = null;        // the book in the dialog
  var pace = 10;
  var view = $('#view');
  var dialog = $('#book-dialog');
  var search = $('#q');

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function el(tag, attrs, text) {
    var n = document.createElement(tag);
    for (var k in attrs || {}) n.setAttribute(k, attrs[k]);
    if (text != null) n.textContent = text;
    return n;
  }
  function mornings(book, minutes) {
    return book.words ? Math.max(1, Math.ceil(book.words / (WORDS_PER_MINUTE * (minutes || 10)))) : null;
  }
  function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }
  function fold(s) { return s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase(); }

  // --- Covers ---------------------------------------------------------------------------
  function art(id, eager) {
    var b = lib.books[id];
    var box = el('div', { class: 'art' });
    if (b.cover) {
      var img = el('img', { src: 'covers/' + id + '.webp', alt: '', decoding: 'async' });
      if (!eager) img.loading = 'lazy';
      img.onerror = function () { img.replaceWith(cloth(id)); };
      box.appendChild(img);
    } else {
      box.appendChild(cloth(id));
    }
    return box;
  }
  function cloth(id) {
    var b = lib.books[id];
    var c = el('div', { class: 'cloth', style: '--c:' + COVER_COLORS[id % COVER_COLORS.length] });
    c.appendChild(el('b', null, b.title));
    c.appendChild(el('i', null, b.author));
    return c;
  }

  function card(id, opts) {
    opts = opts || {};
    var b = lib.books[id];
    var li = el('li');
    if (opts.rank) li.appendChild(el('span', { class: 'num', 'aria-hidden': 'true' }, String(opts.rank)));
    var btn = el('button', { type: 'button', class: 'book', 'data-id': id,
      'aria-label': (opts.rank ? 'Number ' + opts.rank + ': ' : '') + b.title + ' by ' + b.author });
    var a = art(id, opts.eager);
    var m = mornings(b);
    if (m) a.appendChild(el('span', { class: 'len', 'aria-hidden': 'true' }, plural(m, 'morning')));
    btn.appendChild(a);
    if (opts.meta) {
      var meta = el('div', { class: 'meta' }, b.title);
      meta.appendChild(el('span', null, b.author));
      btn.appendChild(meta);
    }
    li.appendChild(btn);
    return li;
  }

  // --- A row of covers that scrolls sideways ----------------------------------------------
  function row(title, ids, opts) {
    opts = opts || {};
    var sec = el('section', { class: 'row' + (opts.ranked ? ' ranked' : '') });
    var head = el('div', { class: 'row-head' });
    var h = el('h2', null, title);
    head.appendChild(h);
    if (opts.genre) {
      var more = el('button', { type: 'button', class: 'more', 'data-genre': opts.genre, 'aria-label': 'See all ' + opts.genre }, 'See all');
      head.appendChild(more);
    }
    sec.appendChild(head);
    var wrap = el('div', { class: 'shelf-wrap' });
    var list = el('ul', { class: 'shelf', 'aria-label': title });
    ids.forEach(function (id, i) { list.appendChild(card(id, { rank: opts.ranked ? i + 1 : 0, eager: opts.eager && i < 8 })); });
    var prev = arrowButton('prev', 'Scroll ' + title + ' back', 'M15 5l-7 7 7 7');
    var next = arrowButton('next', 'Scroll ' + title + ' on', 'M9 5l7 7-7 7');
    prev.onclick = function () { list.scrollBy({ left: -list.clientWidth * 0.85, behavior: 'smooth' }); };
    next.onclick = function () { list.scrollBy({ left: list.clientWidth * 0.85, behavior: 'smooth' }); };
    function update() {
      prev.hidden = list.scrollLeft < 8;
      next.hidden = list.scrollLeft + list.clientWidth > list.scrollWidth - 8;
    }
    list.addEventListener('scroll', update, { passive: true });
    window.addEventListener('resize', update);
    wrap.appendChild(prev); wrap.appendChild(list); wrap.appendChild(next);
    sec.appendChild(wrap);
    requestAnimationFrame(update);
    return sec;
  }
  function arrowButton(cls, label, d) {
    var b = el('button', { type: 'button', class: 'arrow ' + cls, 'aria-label': label, tabindex: '-1' });
    b.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="' + d + '"/></svg>';
    return b;
  }

  // --- Views ----------------------------------------------------------------------------
  function setChip(name) {
    $$('.chip').forEach(function (c) { c.setAttribute('aria-pressed', String(c.dataset.genre === name)); });
  }

  function showHome() {
    setChip('');
    view.textContent = '';
    lib.rows.forEach(function (r, i) {
      view.appendChild(row(r.name, r.books, { ranked: r.ranked, genre: r.genre, eager: i < 2 }));
    });
  }

  function showGenre(name) {
    setChip(name);
    var ids = lib.order.filter(function (id) { return lib.books[id].genres.indexOf(name) >= 0; });
    view.textContent = '';
    var head = el('div', { class: 'view-head' });
    head.appendChild(el('h1', null, name));
    head.appendChild(el('p', null, plural(ids.length, 'book')));
    view.appendChild(head);
    view.appendChild(row('Top 5 in ' + name, ids.slice(0, 5), { ranked: true, eager: true }));
    grid('Every book in ' + name, ids.slice(5));
  }

  function showSearch(q) {
    setChip(null);
    var words = fold(q).split(/\s+/).filter(Boolean);
    var ids = lib.order.filter(function (id) {
      var b = lib.books[id], hay = b.hay || (b.hay = fold(b.title + ' ' + b.author));
      return words.every(function (w) { return hay.indexOf(w) >= 0; });
    });
    view.textContent = '';
    var head = el('div', { class: 'view-head' });
    head.appendChild(el('h1', null, 'Results for “' + q.trim() + '”'));
    head.appendChild(el('p', null, plural(ids.length, 'book')));
    view.appendChild(head);
    if (!ids.length) {
      view.appendChild(el('p', { class: 'empty' },
        'No book or author matches “' + q.trim() + '”. Try a last name, like Austen or Tolstoy, or pick a genre above.'));
      return;
    }
    grid('Matching books', ids);
  }

  function grid(label, ids) {
    var list = el('ul', { class: 'grid', 'aria-label': label });
    ids.forEach(function (id, i) { list.appendChild(card(id, { meta: true, eager: i < 12 })); });
    view.appendChild(list);
  }

  // --- Routing: #genre/Romance and #book/1342 ---------------------------------------------
  function route() {
    var h = decodeURIComponent(location.hash.slice(1));
    if (h.indexOf('book/') === 0) {
      if (!view.querySelector('.row, .grid')) showHome();
      var id = h.slice(5);
      if (lib.books[id]) openBook(id);
      return;
    }
    if (dialog.open) dialog.close();
    if (h.indexOf('genre/') === 0 && lib.genres.some(function (g) { return g.name === h.slice(6); })) {
      search.value = '';
      showGenre(h.slice(6));
    } else if (!search.value.trim()) {
      showHome();
    }
    window.scrollTo(0, 0);
  }
  function go(hash) {
    if (hash === '#') hash = '';
    if (location.hash === hash) route(); else location.hash = hash;
  }

  // --- The book window ------------------------------------------------------------------
  function openBook(id) {
    var b = lib.books[id];
    current = id;
    var a = art(id, true);
    a.id = 'dlg-art';
    $('#dlg-art').replaceWith(a);
    $('#dlg-title').textContent = b.title;
    $('#dlg-by').textContent = b.author + (b.year ? ', ' + b.year : '');
    var tags = $('#dlg-tags');
    tags.textContent = '';
    b.genres.concat(b.lists).forEach(function (g) {
      var isGenre = b.genres.indexOf(g) >= 0;
      var t = el('button', { type: 'button' }, g);
      if (isGenre) t.dataset.genre = g; else t.disabled = true;
      tags.appendChild(t);
    });
    $('#dlg-blurb').textContent = b.blurb;
    $('#dlg-blurb').hidden = !b.blurb;
    $('#dlg-plan').hidden = !b.words;
    renderDays();
    $('#note').textContent = '';

    var alike = $('#alike');
    alike.textContent = '';
    var g = b.genres[0];
    if (g) {
      var ids = lib.order.filter(function (o) { return o !== +id && lib.books[o].genres.indexOf(g) >= 0; }).slice(0, 14);
      if (ids.length >= 3) alike.appendChild(row('More in ' + g, ids));
    }
    if (!dialog.open) dialog.showModal();
    dialog.scrollTop = 0;
  }
  function renderDays() {
    var b = lib.books[current], m = mornings(b, pace);
    if (!m) return;
    var p = $('#dlg-days');
    p.innerHTML = '';
    p.appendChild(document.createTextNode('Finish it in '));
    p.appendChild(el('strong', null, plural(m, 'morning')));
    p.appendChild(document.createTextNode(', ' + pace + ' minutes each.'));
    $$('.pace button').forEach(function (btn) { btn.setAttribute('aria-pressed', String(+btn.dataset.pace === pace)); });
  }

  // --- Wiring ---------------------------------------------------------------------------
  function buildChips() {
    var nav = $('#genres');
    var all = el('button', { type: 'button', class: 'chip', 'data-genre': '' }, 'All books');
    nav.appendChild(all);
    lib.genres.forEach(function (g) { nav.appendChild(el('button', { type: 'button', class: 'chip', 'data-genre': g.name }, g.name)); });
  }

  document.addEventListener('click', function (e) {
    var t = e.target.closest('[data-id], [data-genre], [data-close], [data-pace], [data-home]');
    if (!t) return;
    if (t.matches('[data-pace]')) { pace = +t.dataset.pace; renderDays(); return; }
    if (t.matches('[data-close]')) { dialog.close(); return; }
    if (t.matches('[data-home]')) { e.preventDefault(); search.value = ''; go(''); return; }
    if (t.matches('[data-id]')) { go('#book/' + t.dataset.id); return; }
    if (t.matches('[data-genre]')) {
      search.value = '';
      if (dialog.open) dialog.close();
      go(t.dataset.genre ? '#genre/' + encodeURIComponent(t.dataset.genre) : '#');
    }
  });

  dialog.addEventListener('click', function (e) { if (e.target === dialog) dialog.close(); });
  dialog.addEventListener('close', function () {
    if (location.hash.indexOf('#book/') === 0) history.replaceState(null, '', location.pathname + (lastView || ''));
  });
  var lastView = '';
  window.addEventListener('hashchange', function () {
    if (location.hash.indexOf('#book/') !== 0) lastView = location.hash;
    route();
  });

  $('#join').addEventListener('submit', function (e) {
    e.preventDefault();
    $('#note').textContent = NOTE;
  });

  var timer;
  search.addEventListener('input', function () {
    clearTimeout(timer);
    timer = setTimeout(function () {
      var q = search.value.trim();
      if (q.length >= 2) { if (location.hash) history.replaceState(null, '', location.pathname); showSearch(q); }
      else if (!q) showHome();
    }, 120);
  });
  $('#search-form').addEventListener('submit', function (e) { e.preventDefault(); search.blur(); });
  document.addEventListener('keydown', function (e) {
    if (e.key === '/' && !/INPUT|TEXTAREA/.test(document.activeElement.tagName) && !dialog.open) {
      e.preventDefault(); search.focus();
    }
  });

  var bar = $('#bar');
  window.addEventListener('scroll', function () { bar.classList.toggle('scrolled', window.scrollY > 4); }, { passive: true });

  fetch('library.json').then(function (r) { return r.json(); }).then(function (data) {
    lib = data;
    search.placeholder = 'Search ' + lib.total.toLocaleString('en') + ' books and authors';
    buildChips();
    if (location.hash.indexOf('#book/') !== 0) lastView = location.hash;
    route();
  }).catch(function () {
    view.innerHTML = '<p class="empty">The library didn’t load. Refresh the page to try again.</p>';
  });
})();
