// Dripbook home page: rows of covers, genres, search and the book window. Data: library.json (scripts/build_library.py)
// and, once someone searches, catalog.json: the other Gutenberg books, which readers can request (scripts/build_catalog.py).
// A search with nothing in either also looks through scanned books on the Internet Archive and HathiTrust, which
// readers can request too.
(function () {
  'use strict';

  var WORDS_PER_MINUTE = 230;
  var COVER_COLORS = ['#7B2232', '#1F4A3B', '#1E3A66', '#8A6420', '#4A2D5C', '#1F5F68', '#5B3A24', '#33415C'];

  // The home page banner rotates daily through these. Opening lines checked against the Gutenberg texts.
  var FEATURED = [
    [1342, 'It is a truth universally acknowledged, that a single man in possession of a good fortune must be in want of a wife.'],
    [2701, 'Call me Ishmael. Some years ago—never mind how long precisely—having little or no money in my purse, and nothing particular to interest me on shore, I thought I would sail about a little and see the watery part of the world.'],
    [84, 'You will rejoice to hear that no disaster has accompanied the commencement of an enterprise which you have regarded with such evil forebodings.'],
    [11, 'Alice was beginning to get very tired of sitting by her sister on the bank, and of having nothing to do.'],
    [1661, 'To Sherlock Holmes she is always the woman.'],
    [98, 'It was the best of times, it was the worst of times, it was the age of wisdom, it was the age of foolishness…'],
    [64317, 'In my younger and more vulnerable years my father gave me some advice that I’ve been turning over in my mind ever since.'],
    [1260, 'There was no possibility of taking a walk that day.']
  ];

  var lib = null;            // library.json
  var cat = null;            // catalog.json, loaded on the first search: {total, books: [[id, title, author, subtitle]]}
  var catLoad = null;
  var SHOWN = 40;            // catalog matches listed at once
  var current = null;        // the book in the dialog: a library id, or {title, author, gutenberg | source} to request
  // Scanned books. The Internet Archive answers the browser directly. HathiTrust's list is searched by
  // scripts/sources.py, which runs only on our own machine for now, so the live site leaves it out.
  var IA_SEARCH = 'https://archive.org/advancedsearch.php';
  // HathiTrust: our copy of its public-domain list, as files on R2 read a slice at a time (scripts/hathi_search.py).
  // Empty shows HathiTrust as coming soon. On this machine, the files the sources tool serves.
  var HATHI_R2 = 'https://assets.dripbook.ai/hathi/';
  var HATHI = /^(localhost|127\.|10\.|192\.168\.)/.test(location.hostname)
    ? location.protocol + '//' + location.hostname + ':8765/hathi-search/' : HATHI_R2 || null;
  // The sources tool (python3 scripts/sources.py serve) opens a scan and cuts it into parts, on our own machine.
  // Its Open links show here when the site runs locally, or on the live site after visiting it once with ?editor
  // (?editor=0 hides them again).
  var TOOL = (function () {
    var local = /^(localhost|127\.|10\.|192\.168\.)/.test(location.hostname), on = false;
    try {
      var m = location.search.match(/[?&]editor(?:=([^&]*))?/);
      if (m) { if (m[1] === '0') localStorage.removeItem('dripbook-editor'); else localStorage.setItem('dripbook-editor', '1'); }
      on = localStorage.getItem('dripbook-editor') === '1';
    } catch (e) { /* storage blocked: local only */ }
    return local ? 'http://' + location.hostname + ':8765/' : on ? 'http://localhost:8765/' : null;
  })();
  var entered = '';          // the query the reader pressed Enter on: search the scans without waiting
  var far = {};              // a query's scan searches: {ia, hathi}, each 'searching', 'error', 'off' or a list
  var farTimer = null, farNext = '';
  var scans = [];            // the scans listed on screen, for their Request buttons
  var only = '', onlyFor = '';  // the one library the results are narrowed to ('' for all), and for which query
  var folded = {};           // libraries whose list the reader folded away, by name
  var allCopies = false;     // list every copy of a scan, not one per book
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
  function cloth(id, b) {
    b = b || lib.books[id];
    var c = el('div', { class: 'cloth', style: '--c:' + COVER_COLORS[id % COVER_COLORS.length] });
    c.appendChild(el('b', null, b.title));
    c.appendChild(el('i', null, b.author));
    return c;
  }

  function card(id, opts) {
    opts = opts || {};
    var b = lib.books[id];
    var li = el('li');
    var btn = el('button', { type: 'button', class: 'book', 'data-id': id,
      'aria-label': (opts.rank ? 'Number ' + opts.rank + ': ' : '') + b.title + ' by ' + b.author });
    var a = art(id, opts.eager);
    var m = mornings(b);
    if (m) a.appendChild(el('span', { class: 'len', 'aria-hidden': 'true' }, plural(m, 'morning')));
    if (opts.rank) a.appendChild(el('span', { class: 'num', 'aria-hidden': 'true' }, String(opts.rank)));
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
    var pick = banner();
    if (pick) view.appendChild(pick);
    lib.rows.forEach(function (r, i) {
      view.appendChild(row(r.name, r.books, { ranked: r.ranked, genre: r.genre, eager: i < 2 }));
    });
  }

  // A featured book above the rows: its cover, its first sentence and a way in.
  function banner() {
    var day = Math.floor(Date.now() / 864e5);
    var f = FEATURED.filter(function (x) { return lib.books[x[0]]; });
    if (!f.length) return null;
    var pick = f[day % f.length], id = pick[0], b = lib.books[id];
    var sec = el('section', { class: 'bill', 'aria-labelledby': 'bill-title' });
    if (b.cover) {
      var bg = el('div', { class: 'bill-bg', 'aria-hidden': 'true' });
      bg.style.backgroundImage = 'url(covers/' + id + '.webp)';
      sec.appendChild(bg);
    }
    var inner = el('div', { class: 'bill-in' });
    var text = el('div', { class: 'bill-text' });
    var rank = lib.rows[0] && lib.rows[0].ranked ? lib.rows[0].books.indexOf(id) + 1 : 0;
    text.appendChild(el('p', { class: 'kicker' }, rank ? 'No. ' + rank + ' on Dripbook' : "Today's pick"));
    text.appendChild(el('h2', { id: 'bill-title' }, b.title));
    var facts = [b.author + (b.year ? ', ' + b.year : '') + '.'];
    if (b.genres[0]) facts.push(b.genres[0] + '.');
    var m = mornings(b);
    if (m) facts.push(plural(m, 'morning') + ' at 10 minutes a day.');
    text.appendChild(el('p', { class: 'facts' }, facts.join(' ')));
    text.appendChild(el('blockquote', null, pick[1]));
    var acts = el('div', { class: 'acts' });
    acts.appendChild(el('button', { type: 'button', class: 'btn', 'data-id': id }, 'Start reading free'));
    if (b.genres[0]) acts.appendChild(el('button', { type: 'button', class: 'ghost', 'data-genre': b.genres[0] }, 'More ' + b.genres[0].toLowerCase()));
    text.appendChild(acts);
    inner.appendChild(text);
    var cover = el('button', { type: 'button', class: 'bill-cover', 'data-id': id, 'aria-label': b.title + ' by ' + b.author });
    cover.appendChild(art(id, true));
    inner.appendChild(cover);
    sec.appendChild(inner);
    return sec;
  }

  function showGenre(name) {
    setChip(name);
    var ids = lib.order.filter(function (id) { return lib.books[id].genres.indexOf(name) >= 0; });
    view.textContent = '';
    var head = el('div', { class: 'view-head' });
    head.appendChild(el('h1', null, name));
    head.appendChild(el('p', null, plural(ids.length, 'book')));
    view.appendChild(head);
    var top = ids.slice(0, 10);
    view.appendChild(row('Top ' + top.length + ' in ' + name, top, { ranked: true, eager: true }));
    if (ids.length > top.length) grid('More in ' + name, ids.slice(top.length));
  }

  function showSearch(q) {
    setChip(null);
    var key = q.trim().toLowerCase();
    var words = fold(q).split(/\s+/).filter(Boolean);
    var ids = lib.order.filter(function (id) {
      var b = lib.books[id], hay = b.hay || (b.hay = fold(b.title + ' ' + b.author));
      return words.every(function (w) { return hay.indexOf(w) >= 0; });
    });
    var more = cat ? findInCatalog(words) : [];
    // Nothing here or on Gutenberg: look through the scans once the reader stops typing, or at once after Enter.
    if (cat && !ids.length && !more.length && !far[key] && farNext !== key) {
      clearTimeout(farTimer);
      farNext = key;
      farTimer = setTimeout(function () { farNext = ''; if (search.value.trim().toLowerCase() === key) searchFar(q); },
                            entered === key ? 0 : 450);
    }
    var f = far[key] || (farNext === key ? { ia: 'searching', hathi: HATHI ? 'searching' : 'off' } : null);
    var found = scanRows(f, ids, more);
    if (onlyFor !== key) { only = ''; onlyFor = key; }
    var counts = { lib: ids.length, gut: more.length, ia: found.ia.length, hathi: found.hathi.length };
    var narrow = only && counts[only] ? only : '';
    function show(k) { return !narrow || narrow === k; }

    view.textContent = '';
    var head = el('div', { class: 'view-head' });
    head.appendChild(el('h1', null, 'Results for “' + q.trim() + '”'));
    view.appendChild(head);
    view.appendChild(progress(q, f, [
      ['Your library', ids.length, 'to start now', 'lib'],
      ['Project Gutenberg', cat ? more.length : 'searching', 'to request', 'gut'],
      ['Internet Archive', f ? (Array.isArray(f.ia) ? found.ia.length : f.ia) : 'idle', 'to request', 'ia'],
      ['HathiTrust', !HATHI ? 'off' : f ? (Array.isArray(f.hathi) ? found.hathi.length : f.hathi) : 'idle', 'to request', 'hathi']
    ], narrow));
    if (ids.length && show('lib')) {
      view.appendChild(el('h2', { class: 'hits-h' }, 'Start now'));
      grid('Books to start now', ids);
    }
    if ((more.length && show('gut')) || (found.ia.length && show('ia')) || (found.hathi.length && show('hathi'))) {
      var sec = el('section', { class: 'asks' });
      sec.appendChild(el('h2', null, 'Books you can request'));
      sec.appendChild(el('p', null, 'We haven’t made these into drips yet. Request one and we’ll email you when it’s ready.'));
      scans = found.ia.concat(found.hathi);
      if (more.length && show('gut')) askList(sec, 'Project Gutenberg', null, more.map(function (b) {
        return { title: b[1], by: b[2], attr: 'data-ask', id: b[0] };
      }));
      if (found.ia.length && show('ia')) askList(sec, 'Internet Archive', 'Scanned pages, so they take longer: we clean up the text first.', found.ia.map(scanItem), found.copies.ia, q);
      if (found.hathi.length && show('hathi')) askList(sec, 'HathiTrust', 'Scanned pages from university libraries. These take longest: we find a copy we can read, then clean it up.', found.hathi.map(scanItem), found.copies.hathi, q);
      view.appendChild(sec);
    }
    var busy = !cat || (f && (f.ia === 'searching' || f.hathi === 'searching'));
    if (!busy && !ids.length && !more.length && !found.ia.length && !found.hathi.length) {
      var p = el('p', { class: 'empty' },
        'No book or author matches “' + q.trim() + '”, here or in the libraries we search. Try a last name, like Austen or Tolstoy. If it’s an old book we should find, ');
      p.appendChild(el('a', { href: 'how.html#request' }, 'ask for it'));
      p.appendChild(document.createTextNode('.'));
      view.appendChild(p);
    }
    if (!cat) {
      loadCatalog().then(function () { if (search.value.trim() === q) showSearch(q); }, function () {
        var w = $('#sources');
        if (w) w.appendChild(el('p', { class: 'empty' }, 'Project Gutenberg’s list didn’t load. Search again to retry.'));
      });
    }
  }

  // Where the search has looked: one line per library, and a bar while any is still answering.
  // A library with results is a button: it narrows the results to that library, and pressed again shows them all.
  function progress(q, f, rows, narrow) {
    var box = el('div', { class: 'sources', id: 'sources' });
    var done = rows.filter(function (r) { return typeof r[1] === 'number' || r[1] === 'error'; }).length;
    var live = rows.filter(function (r) { return r[1] !== 'off' && r[1] !== 'idle'; }).length;
    if (done < live) {
      var bar = el('div', { class: 'sources-bar', role: 'progressbar', 'aria-label': 'Searching',
        'aria-valuemin': '0', 'aria-valuemax': String(live), 'aria-valuenow': String(done) });
      bar.appendChild(el('span', { style: 'width:' + Math.round(100 * done / live) + '%' }));
      box.appendChild(bar);
    }
    var list = el('ul');
    rows.forEach(function (r) {
      var n = r[1], state = typeof n === 'number' ? 'done' : n;
      var li = el('li', { class: state + (narrow && narrow !== r[3] ? ' dim' : '') });
      var label = el(state === 'done' && n ? 'button' : 'span', { class: 'source-name' });
      label.appendChild(el('b', null, r[0]));
      label.appendChild(document.createTextNode(
        state === 'done' ? (n ? n.toLocaleString('en') + ' ' + r[2] : 'none') :
        state === 'searching' ? 'searching…' : state === 'error' ? 'didn’t answer' :
        state === 'off' ? 'coming soon' : 'not searched'));
      if (label.tagName === 'BUTTON') {
        label.type = 'button';
        label.setAttribute('aria-pressed', String(narrow === r[3]));
        label.title = narrow === r[3] ? 'Show every library' : 'Show only ' + r[0];
        label.onclick = function () { only = narrow === r[3] ? '' : r[3]; showSearch(q); };
      }
      li.appendChild(label);
      list.appendChild(li);
    });
    box.appendChild(list);
    if (!f && cat) {
      var btn = el('button', { type: 'button', class: 'look-further' }, 'Search the Internet Archive' + (HATHI ? ' and HathiTrust' : '') + ' too');
      btn.onclick = function () { searchFar(q); };
      box.appendChild(btn);
    }
    return box;
  }

  // --- Scanned books: the Internet Archive and HathiTrust ------------------------------------
  function searchFar(q) {
    var key = q.trim().toLowerCase();
    if (far[key]) return;
    var f = far[key] = { q: q, ia: 'searching', hathi: HATHI ? 'searching' : 'off' };
    function again() { if (search.value.trim().toLowerCase() === key) showSearch(search.value); }
    again();
    searchArchive(q).then(function (rows) { f.ia = rows; }, function () { f.ia = 'error'; }).then(again);
    if (HATHI) searchHathi(q).then(function (rows) { f.hathi = rows; }, function () { f.hathi = 'error'; }).then(again);
  }
  function fetchJson(url) {
    var stop = new AbortController(), t = setTimeout(function () { stop.abort(); }, 20000);
    return fetch(url, { signal: stop.signal }).then(function (r) {
      clearTimeout(t);
      if (!r.ok) throw new Error(r.status);
      return r.json();
    });
  }
  // English texts the Archive marks as out of copyright, or printed before 1931; every word in the title or author.
  function searchArchive(q) {
    var words = (q.match(/[\p{L}\p{N}'-]+/gu) || []).slice(0, 8);
    if (!words.length) return Promise.resolve([]);
    var query = '(' + words.map(function (w) { return '(title:"' + w + '" OR creator:"' + w + '")'; }).join(' AND ') + ')' +
      ' AND mediatype:texts AND (language:eng OR language:English)' +
      ' AND (possible-copyright-status:NOT_IN_COPYRIGHT OR date:[1500-01-01 TO 1930-12-31])';
    var params = new URLSearchParams({ q: query, rows: '50', output: 'json' });
    ['identifier', 'title', 'creator', 'date', 'downloads', 'volume'].forEach(function (k) { params.append('fl[]', k); });
    params.append('sort[]', 'downloads desc');
    return fetchJson(IA_SEARCH + '?' + params).then(function (d) {
      return d.response.docs.map(function (x) {
        var c = x.creator;
        return { source: 'ia:' + x.identifier, title: String([].concat(x.title)[0] || ''),
                 author: Array.isArray(c) ? c.join('; ') : String(c || ''), year: String(x.date || '').slice(0, 4),
                 downloads: x.downloads || 0, volume: x.volume, url: 'https://archive.org/details/' + x.identifier, from: 'the Internet Archive' };
      });
    });
  }
  // Every word but the small ones in the title or author, shortest titles first. Four rounds of requests:
  // top.json (once), each word's dictionary block, each word's books, then those books' lines.
  var hathiTop = null, HATHI_SKIP = /^(a|an|and|the|of|in|on|by|to|for|with|or|at|from|as|is)$/;
  function hathiWords(s) {
    var seen = {};
    return (s.toLowerCase().normalize('NFKD').replace(/\p{M}/gu, '').match(/[\p{L}\p{N}]+/gu) || [])
      .filter(function (w) { return !seen[w] && (seen[w] = 1); });
  }
  // len bytes at an offset in one of the three files, from the ~1 MB piece that holds them
  // (top.pieces: where each piece starts). An index from before pieces is one file each.
  function hathiSlice(top, kind, at, len) {
    var starts = top.pieces && top.pieces[kind], file = { dict: 'dict.txt', postings: 'postings', rows: 'rows.txt' }[kind];
    if (starts) {
      var i = 0;
      while (i + 2 < starts.length && starts[i + 1] <= at) i++;
      file = kind + '-' + ('00' + i).slice(-3) + (kind === 'postings' ? '.bin' : '.txt');
      len = Math.min(len, starts[i + 1] - at);
      at -= starts[i];
    }
    var stop = new AbortController(), t = setTimeout(function () { stop.abort(); }, 20000);
    // no-store: the browser would otherwise queue requests for parts of the same file one behind another
    return fetch(HATHI + top.v + '/' + file, { cache: 'no-store', signal: stop.signal,
                                               headers: { Range: 'bytes=' + at + '-' + (at + len - 1) } })
      .then(function (r) {
        if (r.status !== 206) { stop.abort(); throw new Error('No byte ranges: ' + r.status); }  // never the whole file
        return r.arrayBuffer();
      }).then(function (b) { clearTimeout(t); return new Uint8Array(b); },
              function (e) { clearTimeout(t); throw e; });
  }
  function hathiIndex() {
    if (!hathiTop) hathiTop = fetchJson(HATHI + 'top.json');
    hathiTop.catch(function () { hathiTop = null; });
    return hathiTop;
  }
  function searchHathi(q) {
    var all = hathiWords(q), ws = all.filter(function (w) { return !HATHI_SKIP.test(w); });
    if (!ws.length) ws = all;
    if (!ws.length) return Promise.resolve([]);
    var text = new TextDecoder();
    return hathiIndex().then(function (top) {
      return Promise.all(ws.map(function (w) {
        var lo = 0, hi = top.words.length - 1;          // the last block starting at or before the word
        while (lo < hi) { var m = (lo + hi + 1) >> 1; if (top.words[m] <= w) lo = m; else hi = m - 1; }
        if (top.words[lo] > w) return null;
        return hathiSlice(top, 'dict', top.at[lo], top.len[lo]).then(function (b) {
          var line = text.decode(b).split('\n').filter(function (l) { return l.split('\t')[0] === w; })[0];
          return line ? line.split('\t').map(Number) : null;      // [, books, offset, size]
        });
      })).then(function (found) {
        if (found.some(function (x) { return !x; })) return [];
        return Promise.all(found.map(function (x) {
          return hathiSlice(top, 'postings', x[2], x[3]).then(hathiIds);
        })).then(function (lists) {
          var ids = lists.sort(function (a, b) { return a.length - b.length; }).reduce(hathiBoth).slice(0, 30);
          return Promise.all(ids.map(function (at) {
            return hathiSlice(top, 'rows', at, 1500).then(function (b) {
              var x = text.decode(b).split('\n')[0].split('\t');   // htid, bib, year, pd, title, author
              return { source: 'hathi:' + x[0], title: x[4], author: x[5], year: x[2],
                       url: 'https://catalog.hathitrust.org/Record/' + x[1], from: 'HathiTrust' };
            });
          }));
        });
      });
    });
  }
  function hathiIds(b) {             // varint gaps -> sorted ids
    var ids = [], n = 0, d = 0, shift = 0;
    for (var i = 0; i < b.length; i++) {
      d += (b[i] & 127) * Math.pow(2, shift);
      if (b[i] & 128) shift += 7; else { n += d; ids.push(n); d = 0; shift = 0; }
    }
    return ids;
  }
  function hathiBoth(a, b) {         // ids in both sorted lists
    var out = [], i = 0, j = 0;
    while (i < a.length && j < b.length) {
      if (a[i] < b[j]) i++; else if (a[i] > b[j]) j++; else { out.push(a[i]); i++; j++; }
    }
    return out;
  }
  // A query's scans, with copies of the same book and books we already list taken out (unless the reader asked
  // for every copy), and how many such copies each library had.
  function scanRows(f, ids, more) {
    var out = { ia: [], hathi: [], copies: { ia: 0, hathi: 0 } };
    if (!f) return out;
    var known = {};
    ids.forEach(function (id) { known[sameBook(lib.books[id].title, lib.books[id].author)] = 1; });
    more.forEach(function (b) { known[sameBook(b[1], b[2])] = 1; });
    ['ia', 'hathi'].forEach(function (k) {
      if (!Array.isArray(f[k])) return;
      f[k].forEach(function (r) {
        var s = sameBook(r.title, r.author, r.volume);
        if (!r.title) return;
        if (known[s]) out.copies[k]++;
        if (!known[s] || allCopies) { known[s] = 1; out[k].push(r); }
      });
      out[k] = ranked(out[k], f.q || '');
    });
    return out;
  }
  // The search's words as a phrase in the title first, then all of them in the title, then the rest
  // (the Archive's own order, by downloads, within each).
  function ranked(rows, q) {
    var phrase = fold(q).replace(/[^a-z0-9 ]+/g, ' ').trim().replace(/\s+/g, ' ');
    var words = phrase.split(' ').filter(Boolean);
    function score(r) {
      var t = fold(scanTitle(r.title).title).replace(/[^a-z0-9 ]+/g, ' ').replace(/\s+/g, ' ');
      return t.indexOf(phrase) >= 0 ? 0 : words.every(function (w) { return t.indexOf(w) >= 0; }) ? 1 : 2;
    }
    return rows.map(function (r, i) { return [score(r), i, r]; })
      .sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; }).map(function (x) { return x[2]; });
  }
  // Books are the same when the title (its first four words, before any subtitle), the first author's surname
  // and the volume are: Part I and Part II of a book are two books.
  function sameBook(title, author, volume) {
    var t = fold(title || '').split(/\s*[:;\/(\[]|,\s+(?:being|or|with|containing|by|from|and other)\b/)[0]
      .replace(/[^a-z0-9 ]+/g, ' ').split(/\s+/).filter(function (w) { return w && !/^(the|a|an)$/.test(w); });
    var a = fold(author || '').split(';')[0].replace(/\([^)]*\)/g, ' ').replace(/\d.*$/, '');
    var names = (a.indexOf(',') >= 0 ? a.split(',')[0] : a).match(/[a-z]+/g) || [''];
    return t.slice(0, 4).join(' ') + '|' + names[names.length - 1] + '|' + volumeOf(title, volume);
  }
  // 'v.2', 'Vol. II', 'Part 2', 'The Philosophy Of The Beautiful Ii', or the Archive's volume field -> '2'; '' if none.
  var ROMAN = { i: 1, ii: 2, iii: 3, iv: 4, v: 5, vi: 6, vii: 7, viii: 8, ix: 9, x: 10, xi: 11, xii: 12 };
  function volumeOf(title, volume) {
    var m = String(volume || '').match(/\d+/) ||
      fold(title || '').match(/\b(?:v|vol|volume|pt|part|book|bk)\.?\s*(\d+|[ivx]+)\b/) ||
      fold(title || '').match(/\s(ii|iii|iv|vi|vii|viii|ix|xi|xii|[2-9])\s*[.)\]]*$/);
    if (!m) return '';
    var v = m[1] || m[0];
    return String(ROMAN[v] || parseInt(v, 10) || '');
  }
  // 'The philosophy of the beautiful : being outlines… / by William Knight.' -> the title, and its subtitle
  // A very long title keeps its first phrase ('Gems for the fireside, comprising the most unique…').
  function scanTitle(t) {
    var main = t.split(/\s+\/\s+/)[0].replace(/\s*\[[^\]]*\]/g, '').replace(/[\s.,;:]+$/, '');
    var m = main.split(/\s+:\s+|\s*;\s+/);
    if (m[0].length > 70 && m[0].indexOf(', ') > 10) {
      var cut = m[0].indexOf(', ');
      m.splice(0, 1, m[0].slice(0, cut), m[0].slice(cut + 2));
    }
    return { title: m[0], sub: m.slice(1).join('; ') };
  }
  // 'Stoker, Bram, 1847-1912.' -> 'Bram Stoker'; 'Oliphant, Mrs. (Margaret), 1828-1897' -> 'Mrs. Oliphant'
  function scanAuthor(a) {
    a = (a || '').split(';')[0].replace(/,?\s*\b(\d|ca\.|b\.|d\.|active|fl\.).*$/, '').replace(/[\s.,]+$/, '');
    var bits = a.split(/\s*,\s*/);
    if (bits.length === 2) {
      var given = bits[1].replace(/\s*\([^)]*\)/g, '').trim() || bits[1].replace(/[()]/g, '');
      a = given + ' ' + bits[0];
    }
    return a.trim();
  }
  function scanItem(r) {
    var v = volumeOf(r.title, r.volume);
    return { title: scanTitle(r.title).title, by: (v ? 'Volume ' + v + ' · ' : '') + [scanAuthor(r.author), r.year].filter(Boolean).join(', '),
             attr: 'data-scan', id: scans.indexOf(r), open: toolLink(r) };
  }
  // The sources tool's page for a scan: an Archive copy opens at once; a HathiTrust one first looks for the
  // same scan on the Archive.
  function toolLink(r) {
    if (!TOOL) return null;
    return TOOL + '?' + new URLSearchParams(r.source.indexOf('ia:') === 0 ? { open: r.source }
      : { title: r.title, author: r.author || '' });
  }
  // One library's list, which the reader can fold away. A scan library says how many copies of books already listed it hid,
  // with a button to list them too.
  function askList(parent, heading, blurb, rows, copies, q) {
    var box = el('details', { class: 'ask-source' });
    box.open = !folded[heading];
    box.addEventListener('toggle', function () { folded[heading] = !box.open; });
    var sum = el('summary');
    var h = el('h3', null, heading);
    h.appendChild(el('span', { class: 'ask-count' }, rows.length.toLocaleString('en')));
    sum.appendChild(h);
    box.appendChild(sum);
    if (blurb) box.appendChild(el('p', { class: 'ask-note' }, blurb));
    if (copies) {
      var note = el('p', { class: 'ask-copies' }, allCopies
        ? 'Listing every copy, including ' + plural(copies, 'repeat') + ' of books already listed. '
        : (copies === 1 ? '1 more copy' : copies + ' more copies') + ' of books already listed, hidden. ');
      var again = el('button', { type: 'button' }, allCopies ? 'Hide repeats' : 'Show every copy');
      again.onclick = function () { allCopies = !allCopies; showSearch(q); };
      note.appendChild(again);
      box.appendChild(note);
    }
    var list = el('ul', { 'aria-label': 'Books to request from ' + heading });
    rows.slice(0, SHOWN).forEach(function (r) {
      var li = el('li');
      var btn = el('button', { type: 'button' });
      btn.setAttribute(r.attr, r.id);
      btn.appendChild(el('span', { class: 'ask-title' }, r.title));
      btn.appendChild(el('span', { class: 'ask-by' }, r.by));
      btn.appendChild(el('span', { class: 'ask-go', 'aria-hidden': 'true' }, 'Request'));
      li.appendChild(btn);
      if (r.open) li.appendChild(el('a', { class: 'ask-open', href: r.open, target: '_blank', rel: 'noopener',
                                           'aria-label': 'Open ' + r.title + ' in the sources tool' }, 'Open'));
      list.appendChild(li);
    });
    box.appendChild(list);
    if (rows.length > SHOWN) {
      box.appendChild(el('p', { class: 'ask-more' },
        (rows.length - SHOWN).toLocaleString('en') + ' more. Add a word or the author’s name to narrow it down.'));
    }
    parent.appendChild(box);
  }

  // --- The rest of Gutenberg: listed, and requested rather than started -------------------
  function loadCatalog() {
    if (!catLoad) {
      catLoad = fetch('catalog.json').then(function (r) {
        if (!r.ok) throw new Error(r.status);
        return r.json();
      }).then(function (data) {
        cat = { total: data.total, books: data.books, byId: {} };
        data.books.forEach(function (b) {
          cat.byId[b[0]] = b;
          b.push(fold(b[1] + ' ' + b[3]), fold(b[2]));   // [4] title words, [5] author
        });
      });
      catLoad.catch(function () { catLoad = null; });
    }
    return catLoad;
  }
  function findInCatalog(words) {
    var inTitle = [], other = [];
    cat.books.forEach(function (b) {
      var hay = b[4] + ' ' + b[5];
      if (!words.every(function (w) { return hay.indexOf(w) >= 0; })) return;
      (words.every(function (w) { return b[4].indexOf(w) >= 0; }) ? inTitle : other).push(b);
    });
    return inTitle.concat(other);
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
    if (h.indexOf('request/') === 0) {
      var gid = h.slice(8);
      if (!view.querySelector('.row, .grid, .asks')) showHome();
      loadCatalog().then(function () { if (cat.byId[gid]) openRequest(cat.byId[gid]); });
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
    $('#dlg-start').textContent = 'Start reading';
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
    note('');

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
  // A Gutenberg book we haven't made a drip of: same window, with an email and a request.
  function openRequest(b) {
    askWindow({ title: b[1], author: b[2], gutenberg: b[0] }, b[0],
      (b[3] ? b[3].replace(/^./, function (c) { return c.toUpperCase(); }) + '. ' : '') +
      'We haven’t made this one into a drip yet. Request it and we’ll email you when it’s ready to start.');
  }
  // A scanned book: the same, with where the scan is.
  function openScan(r) {
    var t = scanTitle(r.title), n = 0;
    for (var i = 0; i < r.source.length; i++) n = (n * 31 + r.source.charCodeAt(i)) % 9973;
    askWindow({ title: t.title, author: scanAuthor(r.author), source: r.source }, n,
      (t.sub ? t.sub.replace(/^./, function (c) { return c.toUpperCase(); }) + '. ' : '') +
      'A scan of the ' + (r.year ? r.year + ' ' : '') + 'printing, from ' + r.from + '. Scans need their text cleaned up ' +
      'before they read well, so this takes a little longer. Request it and we’ll email you when it’s ready to start.', r.url);
  }
  function askWindow(book, color, blurb, link) {
    current = book;
    var a = el('div', { class: 'art', id: 'dlg-art' });
    a.appendChild(cloth(color, book));
    $('#dlg-art').replaceWith(a);
    $('#dlg-title').textContent = book.title;
    $('#dlg-by').textContent = book.author;
    $('#dlg-tags').textContent = '';
    $('#dlg-blurb').textContent = blurb;
    if (link) {
      $('#dlg-blurb').appendChild(document.createTextNode(' '));
      $('#dlg-blurb').appendChild(el('a', { href: link, target: '_blank', rel: 'noopener' }, 'See the scan'));
    }
    $('#dlg-blurb').hidden = false;
    $('#dlg-plan').hidden = true;
    $('#dlg-start').textContent = 'Request book';
    $('#alike').textContent = '';
    note('');
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
    var t = e.target.closest('[data-id], [data-ask], [data-scan], [data-genre], [data-close], [data-pace], [data-home]');
    if (!t) return;
    if (t.matches('[data-pace]')) { pace = +t.dataset.pace; renderDays(); return; }
    if (t.matches('[data-close]')) { dialog.close(); return; }
    if (t.matches('[data-home]')) { e.preventDefault(); search.value = ''; go(''); return; }
    if (t.matches('[data-id]')) { go('#book/' + t.dataset.id); return; }
    if (t.matches('[data-ask]')) { go('#request/' + t.dataset.ask); return; }
    if (t.matches('[data-scan]')) { if (scans[+t.dataset.scan]) openScan(scans[+t.dataset.scan]); return; }
    if (t.matches('[data-genre]')) {
      search.value = '';
      if (dialog.open) dialog.close();
      go(t.dataset.genre ? '#genre/' + encodeURIComponent(t.dataset.genre) : '#');
    }
  });

  dialog.addEventListener('click', function (e) { if (e.target === dialog) dialog.close(); });
  dialog.addEventListener('close', function () {
    if (/^#(book|request)\//.test(location.hash)) history.replaceState(null, '', location.pathname + (lastView || ''));
  });
  var lastView = '';
  window.addEventListener('hashchange', function () {
    if (!/^#(book|request)\//.test(location.hash)) lastView = location.hash;
    route();
  });

  // Sign-up: api/subscribe.py emails a confirmation link; part 1 follows the click.
  function timeZone() {   // saved for when readers can pick their delivery time
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (err) { return ''; }
  }
  function note(text, state) {
    var n = $('#note');
    n.textContent = text;
    n.className = 'note' + (state ? ' ' + state : '');
  }
  $('#join').addEventListener('submit', function (e) {
    e.preventDefault();
    var btn = $('#dlg-start'), book = current, email = $('#email').value;
    var ask = typeof book === 'object';
    btn.disabled = true;
    note('Sending…');
    fetch(ask ? '/api/request' : '/api/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(ask ? { email: email, title: book.title, author: book.author, gutenberg: book.gutenberg, source: book.source }
                               : { email: email, book: book, pace: pace, tz: timeZone() })
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (current !== book) return;
        if (r.ok) note(data.message || 'Check your inbox for a link from Dripbook.', 'ok');
        else note(data.error || 'That didn\'t work. Try again in a minute.', 'err');
      });
    }).catch(function () {
      note("We couldn't reach Dripbook. Check your connection and try again.", 'err');
    }).then(function () { btn.disabled = false; });
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
  $('#search-form').addEventListener('submit', function (e) {
    e.preventDefault();
    search.blur();
    var q = search.value.trim();
    if (q.length < 2) return;
    clearTimeout(timer);
    entered = q.toLowerCase();
    if (farNext === entered) { clearTimeout(farTimer); farNext = ''; }   // already waiting: don't
    if (location.hash) history.replaceState(null, '', location.pathname);
    showSearch(q);
  });
  // Someone who clicks into the box is about to search: fetch the lists it needs now.
  search.addEventListener('focus', function () {
    loadCatalog().catch(function () {});
    if (HATHI) hathiIndex().catch(function () {});
  }, { once: true });
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
    if (!/^#(book|request)\//.test(location.hash)) lastView = location.hash;
    route();
  }).catch(function () {
    view.innerHTML = '<p class="empty">The library didn’t load. Refresh the page to try again.</p>';
  });
})();
