// Prevent double submits: a second click on "Печать" must not take another batch of codes.
document.querySelectorAll("form[data-once]").forEach((form) => {
  form.addEventListener("submit", (e) => {
    if (form.dataset.sent) { e.preventDefault(); return; }
    form.dataset.sent = "1";
    form.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  });
});

document.querySelectorAll("form[data-confirm]").forEach((form) => {
  form.addEventListener("submit", (e) => { if (!confirm(form.dataset.confirm)) e.preventDefault(); });
});

document.querySelectorAll("tr[data-href]").forEach((row) => {
  row.addEventListener("click", (e) => { if (e.target.tagName !== "A") location.href = row.dataset.href; });
});

// Enter in the quantity field moves on to the personal code instead of submitting without it.
document.querySelectorAll("input[name=qty]").forEach((qty) => {
  qty.addEventListener("keydown", (e) => {
    const code = qty.form.querySelector("input[name=personal_code]");
    if (e.key === "Enter" && code && !code.value) { e.preventDefault(); code.focus(); }
  });
});

// After a search the field is selected, so the next scan replaces the old text.
document.querySelectorAll("input[data-select]").forEach((input) => { if (input.value) input.select(); });

const checkAll = document.querySelector("[data-check-all]");
if (checkAll) {
  checkAll.addEventListener("change", () => {
    document.querySelectorAll("input[name=code_id]").forEach((c) => { c.checked = checkAll.checked; });
  });
}

function setupPrint() {
  const frame = document.getElementById("pdf");
  const button = document.getElementById("print-btn");
  const open = () => {
    try { frame.contentWindow.focus(); frame.contentWindow.print(); }
    catch (e) { window.open(frame.src, "_blank"); }
  };
  frame.addEventListener("load", () => {
    button.disabled = false;
    setTimeout(open, 300);
  });
  button.addEventListener("click", open);
}

function toggleLoginFields() {
  const role = document.getElementById("role");
  const fields = document.getElementById("login-fields");
  const update = () => {
    fields.style.display = role.value === "admin" ? "" : "none";
  };
  role.addEventListener("change", update);
  update();
}

function setupPeopleSearch() {
  const input = document.getElementById("people-search");
  const list = document.getElementById("people-suggest");
  const people = JSON.parse(document.getElementById("people-data").textContent);
  let shown = [], active = 0;
  const norm = (s) => s.toLowerCase().replace(/ё/g, "е").replace(/\s+/g, "");
  const open = (p) => { window.location = "/users/" + p.id; };
  const render = () => {
    list.innerHTML = "";
    shown.forEach((p, i) => {
      const li = document.createElement("li");
      li.className = i === active ? "active" : "";
      li.textContent = p.name;
      if (p.code) {
        const code = document.createElement("span");
        code.className = "mono muted";
        code.textContent = p.code;
        li.appendChild(code);
      }
      li.addEventListener("mousedown", (e) => { e.preventDefault(); open(p); });
      list.appendChild(li);
    });
    list.hidden = shown.length === 0;
  };
  input.addEventListener("input", () => {
    const q = input.value.trim().toLowerCase().replace(/ё/g, "е");
    const qc = norm(input.value);
    active = 0;
    if (!q) { shown = []; render(); return; }
    const score = (p) => {
      const name = p.name.toLowerCase().replace(/ё/g, "е");
      const code = norm(p.code);
      if (code && code === qc) return 0;
      if (name.startsWith(q) || name.split(/\s+/).some((w) => w.startsWith(q))) return 1;
      if (code && code.startsWith(qc)) return 2;
      if (name.includes(q) || (code && code.includes(qc))) return 3;
      return -1;
    };
    shown = people.map((p) => [score(p), p]).filter(([s]) => s >= 0)
      .sort((a, b) => a[0] - b[0] || (a[0] === 2 ? a[1].code.localeCompare(b[1].code)
                                                  : a[1].name.localeCompare(b[1].name, "ru", {numeric: true})))
      .slice(0, 10).map(([, p]) => p);
    render();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" && shown.length) { active = (active + 1) % shown.length; render(); e.preventDefault(); }
    else if (e.key === "ArrowUp" && shown.length) { active = (active - 1 + shown.length) % shown.length; render(); e.preventDefault(); }
    else if (e.key === "Enter") { e.preventDefault(); if (shown[active]) open(shown[active]); }
    else if (e.key === "Escape") { shown = []; render(); }
  });
  input.addEventListener("blur", () => { list.hidden = true; });
  input.addEventListener("focus", () => { list.hidden = shown.length === 0; });
}
