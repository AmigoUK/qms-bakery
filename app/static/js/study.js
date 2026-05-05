/* Training "study" step — flash-card practice before the exam.
 *
 * - Reads every <article class="flashcard"> already rendered server-side.
 * - Maintains a shuffled order on the client (Fisher-Yates) so two
 *   trainees on the same magic link don't see the same sequence.
 * - Toggles each card between question and answer view by flipping the
 *   data-state attribute; CSS does the visual show/hide based on that.
 * - Falls back gracefully without JS: cards are all visible at once
 *   (front + back together) and the skip-to-exam <a> still works.
 *
 * CSP-strict: no inline handlers, no unsafe-eval. Loaded with `defer`
 * so the DOM is already populated when this IIFE runs.
 */
(function () {
  'use strict';

  var stack = document.getElementById('flashcard-stack');
  var counter = document.getElementById('study-counter');
  if (!stack || !counter) return;

  var cards = Array.prototype.slice.call(
    stack.querySelectorAll('.flashcard')
  );
  if (cards.length === 0) return;

  // Initial order is the document order; we shuffle on first paint so
  // a refresh produces a new sequence (per-load, not per-session).
  var order = cards.map(function (_, i) { return i; });
  var pos = 0;

  function shuffle(arr) {
    for (var i = arr.length - 1; i > 0; i--) {
      var j = Math.floor(Math.random() * (i + 1));
      var tmp = arr[i]; arr[i] = arr[j]; arr[j] = tmp;
    }
    return arr;
  }

  function show(i) {
    pos = ((i % order.length) + order.length) % order.length;
    cards.forEach(function (card, idx) {
      var visible = idx === order[pos];
      card.hidden = !visible;
      // Reset to question-side whenever we navigate, so the trainee
      // always sees the prompt first on the new card.
      if (visible) card.dataset.state = 'question';
    });
    counter.textContent = (pos + 1) + ' / ' + order.length;
  }

  function bindToggle(card) {
    var btn = card.querySelector('.flashcard-toggle');
    if (!btn) return;
    btn.addEventListener('click', function () {
      card.dataset.state =
        card.dataset.state === 'answer' ? 'question' : 'answer';
    });
  }
  function bindNext(card) {
    var btn = card.querySelector('.flashcard-next');
    if (!btn) return;
    btn.addEventListener('click', function () { show(pos + 1); });
  }
  cards.forEach(bindToggle);
  cards.forEach(bindNext);

  var prevBtn = document.getElementById('study-prev');
  var nextBtn = document.getElementById('study-next');
  var shuffleBtn = document.getElementById('study-shuffle');

  if (prevBtn) prevBtn.addEventListener('click', function () { show(pos - 1); });
  if (nextBtn) nextBtn.addEventListener('click', function () { show(pos + 1); });
  if (shuffleBtn) {
    shuffleBtn.addEventListener('click', function () {
      shuffle(order);
      show(0);
    });
  }

  // First paint: a fresh shuffle so the trainee gets random order on
  // every visit, then show the first card.
  shuffle(order);
  show(0);
})();
