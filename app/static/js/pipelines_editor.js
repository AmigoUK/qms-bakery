(function () {
  const list = document.getElementById("stage-list");
  const tpl = document.getElementById("stage-template");
  const addBtn = document.getElementById("add-stage");
  const form = document.getElementById("pipeline-form");
  const hidden = document.getElementById("stages-json");

  if (window.Sortable) {
    Sortable.create(list, { handle: ".stage-handle", animation: 150 });
  }

  function bindRow(row) {
    row.querySelector(".stage-remove").addEventListener("click", () => row.remove());
  }
  list.querySelectorAll("[data-stage]").forEach(bindRow);

  addBtn.addEventListener("click", () => {
    const node = tpl.content.firstElementChild.cloneNode(true);
    list.appendChild(node);
    bindRow(node);
    node.querySelector("[data-field='code']").focus();
  });

  form.addEventListener("submit", () => {
    const rows = list.querySelectorAll("[data-stage]");
    const stages = [];
    rows.forEach((row) => {
      const get = (k) => row.querySelector(`[data-field='${k}']`);
      stages.push({
        code: get("code").value,
        name_pl: get("name_pl").value,
        name_en: get("name_en").value,
        required_role_code: get("required_role_code").value,
        sla_minutes: get("sla_minutes").value,
        is_ccp_checkpoint: get("is_ccp_checkpoint").checked,
      });
    });
    hidden.value = JSON.stringify(stages);
  });
})();
