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

// Drop-down suggestions under a search box. items(query) returns (or resolves to) a list;
// row(item) builds the list element; pick(item) opens it. With pickFirst, Enter opens the top
// suggestion; otherwise Enter submits the form unless one was chosen with the arrow keys.
function attachSuggest(input, list, { items, row, pick, pickFirst }) {
  let shown = [], active = -1, request = 0, timer = null;
  const render = () => {
    list.innerHTML = "";
    shown.forEach((item, i) => {
      const li = row(item);
      if (i === active) li.classList.add("active");
      li.addEventListener("mousedown", (e) => { e.preventDefault(); pick(item); });
      list.appendChild(li);
    });
    list.hidden = shown.length === 0;
  };
  const update = async () => {
    const mine = ++request;
    const result = await items(input.value.trim());
    if (mine !== request) return;
    shown = result;
    active = pickFirst && shown.length ? 0 : -1;
    render();
  };
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(update, 120); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" && shown.length) { active = (active + 1) % shown.length; render(); e.preventDefault(); }
    else if (e.key === "ArrowUp" && shown.length) { active = (active - 1 + shown.length) % shown.length; render(); e.preventDefault(); }
    else if (e.key === "Enter" && active >= 0 && shown[active]) { e.preventDefault(); pick(shown[active]); }
    else if (e.key === "Enter" && pickFirst) { e.preventDefault(); }
    else if (e.key === "Escape") { shown = []; render(); }
  });
  input.addEventListener("blur", () => { list.hidden = true; });
  input.addEventListener("focus", () => { list.hidden = shown.length === 0; });
}

function suggestRow(text, side, sideClass) {
  const li = document.createElement("li");
  li.append(document.createTextNode(text));
  if (side) {
    const span = document.createElement("span");
    span.className = sideClass;
    span.textContent = side;
    li.appendChild(span);
  }
  return li;
}

function setupPeopleSearch() {
  const people = JSON.parse(document.getElementById("people-data").textContent);
  const norm = (s) => s.toLowerCase().replace(/ё/g, "е").replace(/\s+/g, "");
  attachSuggest(document.getElementById("people-search"), document.getElementById("people-suggest"), {
    pickFirst: true,
    row: (p) => suggestRow(p.name, p.code, "mono muted"),
    pick: (p) => { window.location = "/users/" + p.id; },
    items: (value) => {
      const q = value.toLowerCase().replace(/ё/g, "е");
      const qc = norm(value);
      if (!q) return [];
      const score = (p) => {
        const name = p.name.toLowerCase().replace(/ё/g, "е");
        const code = norm(p.code);
        if (code && code === qc) return 0;
        if (name.startsWith(q) || name.split(/\s+/).some((w) => w.startsWith(q))) return 1;
        if (code && code.startsWith(qc)) return 2;
        if (name.includes(q) || (code && code.includes(qc))) return 3;
        return -1;
      };
      return people.map((p) => [score(p), p]).filter(([s]) => s >= 0)
        .sort((a, b) => a[0] - b[0] || (a[0] === 2 ? a[1].code.localeCompare(b[1].code)
                                                    : a[1].name.localeCompare(b[1].name, "ru", {numeric: true})))
        .slice(0, 10).map(([, p]) => p);
    },
  });
}

function setupProductSearch() {
  attachSuggest(document.getElementById("product-search"), document.getElementById("product-suggest"), {
    pickFirst: false,
    row: (p) => {
      const text = [p.article, p.size && "р. " + p.size, p.color].filter(Boolean).join(" · ");
      const li = suggestRow(text, p.stock > 0 ? p.stock + " шт." : "нет кодов", p.stock > 0 ? "muted" : "bad");
      if (p.stock <= 0) li.classList.add("empty");
      return li;
    },
    pick: (p) => { window.location = "/pack/product/" + p.id; },
    items: async (q) => {
      // A barcode is scanned whole and opened by Enter, so digits alone get no suggestions.
      if (q.length < 2 || /^\d{8,}$/.test(q)) return [];
      try {
        const r = await fetch("/pack/suggest?q=" + encodeURIComponent(q));
        return r.ok ? await r.json() : [];
      } catch (e) { return []; }
    },
  });
}
