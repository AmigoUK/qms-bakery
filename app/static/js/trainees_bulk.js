/* Trainees list — bulk-select wiring.
 *
 * - Select-all in the table header toggles every visible row checkbox.
 * - Bottom bulk-bar appears when ≥ 1 row is checked, with a live count.
 * - Form submit refuses unless a course is picked AND ≥ 1 row checked.
 *
 * CSP-strict: no inline handlers, defer-loaded so the DOM is ready
 * when the IIFE runs. Mirrors static/js/study.js structure.
 */
(function () {
  'use strict';

  var form = document.getElementById('trainees-bulk-form');
  if (!form) return;

  var selectAll = document.getElementById('trainees-select-all');
  var picks = Array.prototype.slice.call(
    form.querySelectorAll('.trainee-pick')
  );
  var bar = document.getElementById('bulk-bar');
  var countEl = document.getElementById('bulk-count-n');
  var courseSel = form.querySelector('select[name="course_code"]');

  function refresh() {
    var n = 0;
    picks.forEach(function (cb) {
      if (cb.checked) {
        n += 1;
        cb.closest('tr').classList.add('row-selected');
      } else {
        cb.closest('tr').classList.remove('row-selected');
      }
    });
    if (countEl) countEl.textContent = String(n);
    if (bar) bar.hidden = n === 0;
    if (selectAll) {
      selectAll.checked = n > 0 && n === picks.length;
      selectAll.indeterminate = n > 0 && n < picks.length;
    }
  }

  picks.forEach(function (cb) { cb.addEventListener('change', refresh); });

  if (selectAll) {
    selectAll.addEventListener('change', function () {
      picks.forEach(function (cb) { cb.checked = selectAll.checked; });
      refresh();
    });
  }

  form.addEventListener('submit', function (event) {
    var anyChecked = picks.some(function (cb) { return cb.checked; });
    if (!anyChecked) {
      event.preventDefault();
      return;
    }
    if (courseSel && !courseSel.value) {
      event.preventDefault();
      courseSel.focus();
    }
  });

  refresh();
})();
