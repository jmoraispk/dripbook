// How it works page: the library count, a fan of covers, and the book request form.
(function () {
  'use strict';

  fetch('library.json').then(function (r) { return r.json(); }).then(function (lib) {
    document.querySelectorAll('[data-count]').forEach(function (n) { n.textContent = lib.total.toLocaleString('en'); });
    var fan = document.getElementById('fan');
    lib.order.filter(function (id) { return lib.books[id].cover; }).slice(0, 7).forEach(function (id, i) {
      var li = document.createElement('li');
      li.style.setProperty('--i', i - 3);
      var img = document.createElement('img');
      img.src = 'covers/' + id + '.webp';
      img.alt = '';
      li.appendChild(img);
      fan.appendChild(li);
    });
  }).catch(function () {});

  // Requests are saved by api/request.py; the export script lists them.
  var form = document.getElementById('request-form');
  var note = document.getElementById('request-note');
  function say(text, state) {
    note.textContent = text;
    note.className = 'note' + (state ? ' ' + state : '');
  }
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var btn = form.querySelector('button');
    btn.disabled = true;
    say('Sending…');
    fetch('/api/request', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: form.elements.title.value, author: form.elements.author.value, email: form.elements.email.value,
        mine: form.elements.mine.checked, website: form.elements.website.value
      })
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (r.ok) { say(data.message || 'Thanks. Your request is in.', 'ok'); form.elements.title.value = ''; form.elements.author.value = ''; form.elements.mine.checked = false; }
        else say(data.error || "That didn't work. Try again in a minute.", 'err');
      });
    }).catch(function () {
      say("We couldn't reach Dripbook. Check your connection and try again.", 'err');
    }).then(function () { btn.disabled = false; });
  });

  var bar = document.getElementById('bar');
  window.addEventListener('scroll', function () { bar.classList.toggle('scrolled', window.scrollY > 4); }, { passive: true });
})();
