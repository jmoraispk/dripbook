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

  // No server yet: the request opens an email to Dripbook, already filled in.
  var form = document.getElementById('request-form');
  var note = document.getElementById('request-note');
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var to = form.dataset.to;
    if (!to || to.indexOf('{{') === 0) {
      note.textContent = "Requests aren't open yet. Try again soon.";
      return;
    }
    var title = form.title.value.trim(), author = form.author.value.trim(), email = form.email.value.trim();
    var mine = form.mine.checked;
    var subject = (mine ? 'Author request: ' : 'Book request: ') + title + (author ? ' by ' + author : '');
    var body = 'Book: ' + title + '\nAuthor: ' + (author || 'not sure') + '\nEmail: ' + email +
      (mine ? '\n\nI wrote this book and would like it on Dripbook.' : '');
    location.href = 'mailto:' + to + '?subject=' + encodeURIComponent(subject) + '&body=' + encodeURIComponent(body);
    note.textContent = 'Your email app should open with the request filled in. Send it and we’ll take it from there.';
  });

  var bar = document.getElementById('bar');
  window.addEventListener('scroll', function () { bar.classList.toggle('scrolled', window.scrollY > 4); }, { passive: true });
})();
