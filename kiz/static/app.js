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
