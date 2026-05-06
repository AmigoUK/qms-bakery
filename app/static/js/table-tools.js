/* Client-side filter + sort for `<table class="data">`.
 *
 * Auto-discovers every .data table on DOMContentLoaded and:
 *   - inserts a search box above it (filters rows after 3+ chars)
 *   - turns each <th> into a button that cycles asc → desc → none
 *
 * Filtering is case-insensitive and matches against the visible text of
 * the entire row (so a single search box covers every column). Sorting
 * detects the column type per click — numeric / ISO-date / text — by
 * sniffing the cells, so date columns sort chronologically without the
 * template having to declare types.
 *
 * Opt-out: any table with `data-no-tools` is skipped. Tables with no
 * <thead>, fewer than 2 data rows, or rows that span columns (e.g.
 * single "no data" placeholders) are skipped automatically.
 *
 * Pagination caveat: this only sorts/filters rows currently in the
 * DOM. Paginated lists (audit log, tickets) only affect the visible
 * page. Server-side sort/filter is a separate piece of work.
 */
(function () {
  'use strict';

  const FILTER_MIN_CHARS = 3;

  function init() {
    const tables = document.querySelectorAll('table.data');
    tables.forEach(enhance);
  }

  function enhance(table) {
    if (table.hasAttribute('data-no-tools')) return;

    const thead = table.tHead;
    if (!thead || thead.rows.length === 0) return;

    const headerRow = thead.rows[thead.rows.length - 1];
    const headers = Array.from(headerRow.cells);
    if (headers.length < 2) return;

    const tbody = table.tBodies[0];
    if (!tbody) return;
    const dataRows = Array.from(tbody.rows).filter(
      (r) => r.cells.length === headers.length
    );
    if (dataRows.length < 2) return;

    const placeholderText =
      table.dataset.filterPlaceholder ||
      document.documentElement.dataset.tableFilterPlaceholder ||
      'Filter (3+ chars)';
    const noMatchText =
      document.documentElement.dataset.tableNoMatches || 'No matches';

    const toolbar = buildToolbar(table, placeholderText);
    table.parentNode.insertBefore(toolbar, table);

    const emptyState = document.createElement('p');
    emptyState.className = 'table-no-matches muted';
    emptyState.textContent = noMatchText;
    emptyState.hidden = true;
    table.parentNode.insertBefore(emptyState, table.nextSibling);

    const state = {
      table: table,
      tbody: tbody,
      rows: dataRows,
      headers: headers,
      emptyState: emptyState,
      filterText: '',
      sortIndex: -1,
      sortDir: 0, // 0 = none, 1 = asc, -1 = desc
      originalOrder: dataRows.slice(),
    };

    wireFilter(toolbar.querySelector('input[type="search"]'), state);
    wireSort(state);
  }

  function buildToolbar(table, placeholder) {
    const toolbar = document.createElement('div');
    toolbar.className = 'table-toolbar';

    const wrap = document.createElement('label');
    wrap.className = 'table-filter';

    const input = document.createElement('input');
    input.type = 'search';
    input.placeholder = placeholder;
    input.autocomplete = 'off';
    input.spellcheck = false;
    input.setAttribute('aria-label', placeholder);

    wrap.appendChild(input);
    toolbar.appendChild(wrap);
    return toolbar;
  }

  function wireFilter(input, state) {
    input.addEventListener('input', () => {
      const q = input.value.trim().toLowerCase();
      state.filterText = q.length >= FILTER_MIN_CHARS ? q : '';
      applyFilter(state);
    });
  }

  function applyFilter(state) {
    let visibleCount = 0;
    state.rows.forEach((row) => {
      const match =
        !state.filterText || row.textContent.toLowerCase().includes(state.filterText);
      row.hidden = !match;
      if (match) visibleCount += 1;
    });
    state.emptyState.hidden = visibleCount > 0 || !state.filterText;
  }

  function wireSort(state) {
    state.headers.forEach((th, idx) => {
      // Empty header (e.g. action column) is not sortable.
      if (!th.textContent.trim()) return;
      th.setAttribute('role', 'button');
      th.setAttribute('tabindex', '0');
      th.setAttribute('aria-sort', 'none');
      th.classList.add('th-sortable');
      th.addEventListener('click', () => cycleSort(state, idx));
      th.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          cycleSort(state, idx);
        }
      });
    });
  }

  function cycleSort(state, idx) {
    if (state.sortIndex !== idx) {
      state.sortIndex = idx;
      state.sortDir = 1;
    } else if (state.sortDir === 1) {
      state.sortDir = -1;
    } else {
      state.sortIndex = -1;
      state.sortDir = 0;
    }
    state.headers.forEach((th, i) => {
      if (i !== state.sortIndex || state.sortDir === 0) {
        th.setAttribute('aria-sort', 'none');
      } else {
        th.setAttribute('aria-sort', state.sortDir === 1 ? 'ascending' : 'descending');
      }
    });
    applySort(state);
  }

  function applySort(state) {
    const ordered =
      state.sortDir === 0
        ? state.originalOrder.slice()
        : state.rows
            .slice()
            .sort(buildComparator(state.sortIndex, state.sortDir));
    const fragment = document.createDocumentFragment();
    ordered.forEach((row) => fragment.appendChild(row));
    state.tbody.appendChild(fragment);
  }

  function buildComparator(idx, dir) {
    return function (a, b) {
      const av = cellValue(a.cells[idx]);
      const bv = cellValue(b.cells[idx]);
      if (av.kind === 'number' && bv.kind === 'number') {
        return dir * (av.num - bv.num);
      }
      if (av.kind === 'date' && bv.kind === 'date') {
        return dir * (av.num - bv.num);
      }
      return dir * av.text.localeCompare(bv.text);
    };
  }

  // Sniff per-cell. Empty cells sort last regardless of direction —
  // a typed-in null shouldn't drag rows to the top of "asc".
  function cellValue(cell) {
    const raw = (cell.textContent || '').trim();
    if (!raw) return { kind: 'text', text: '￿', num: 0 };
    // ISO date or timestamp: 2026-05-06 or 2026-05-06 12:34:56
    if (/^\d{4}-\d{2}-\d{2}(\s\d{2}:\d{2}(:\d{2})?)?$/.test(raw)) {
      const ts = Date.parse(raw.replace(' ', 'T'));
      if (!Number.isNaN(ts)) return { kind: 'date', text: raw, num: ts };
    }
    // Numeric: integer, decimal, signed. Reject mixed alphanumeric.
    if (/^-?\d+(\.\d+)?$/.test(raw)) {
      return { kind: 'number', text: raw, num: parseFloat(raw) };
    }
    return { kind: 'text', text: raw.toLowerCase(), num: 0 };
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
