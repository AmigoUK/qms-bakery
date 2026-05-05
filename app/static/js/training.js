/* Training feature client-side wiring.
 *
 * - Initialises SignaturePad on the declaration page.
 * - Encodes the canvas as a PNG data-URL into a hidden input on submit.
 * - Disables the submit button until both typed name and a non-empty
 *   signature are present.
 *
 * CSP-strict: no inline handlers, no unsafe-eval. Loaded with `defer`
 * so DOM is ready when the IIFE runs.
 */
(function () {
  'use strict';

  var canvas = document.getElementById('signature-pad');
  if (!canvas || typeof SignaturePad !== 'function') return;

  var pad = new SignaturePad(canvas, {
    backgroundColor: '#ffffff',
    penColor: '#0b3d2e',
    minWidth: 0.8,
    maxWidth: 2.4
  });

  var resize = function () {
    var ratio = Math.max(window.devicePixelRatio || 1, 1);
    canvas.width = canvas.offsetWidth * ratio;
    canvas.height = canvas.offsetHeight * ratio;
    canvas.getContext('2d').scale(ratio, ratio);
    pad.clear();
  };
  window.addEventListener('resize', resize);
  resize();

  var clearBtn = document.getElementById('signature-clear');
  if (clearBtn) {
    clearBtn.addEventListener('click', function () { pad.clear(); });
  }

  var form = document.getElementById('declaration-form');
  var hidden = document.getElementById('signature-data');
  // Localised "please sign first" message comes from the template via
  // a data-attribute so the strict CSP can stay 'self'-only and the PL
  // operator doesn't get an English alert popup.
  var signRequired = (form && form.dataset.signRequiredMessage) ||
    'Please sign before submitting.';
  // Render an inline error banner instead of a native alert() — alert
  // blocks the page, has no styling, and is easy to miss on touch
  // devices. Insert above the canvas, idempotently.
  function showSignError() {
    var existing = document.getElementById('signature-error');
    if (existing) {
      existing.scrollIntoView({ block: 'center', behavior: 'smooth' });
      return;
    }
    var banner = document.createElement('p');
    banner.id = 'signature-error';
    banner.className = 'field-error';
    banner.setAttribute('role', 'alert');
    banner.textContent = signRequired;
    var wrap = document.querySelector('.signature-pad-wrap') || canvas.parentNode;
    wrap.parentNode.insertBefore(banner, wrap);
    banner.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }
  if (form && hidden) {
    form.addEventListener('submit', function (event) {
      if (pad.isEmpty()) {
        event.preventDefault();
        showSignError();
        return;
      }
      hidden.value = pad.toDataURL('image/png');
    });
  }
})();
