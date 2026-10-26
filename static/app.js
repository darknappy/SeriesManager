(function () {
  "use strict";

  // Confirmaciones declarativas: <form data-confirm="texto">
  document.addEventListener("submit", function (event) {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  });

  // Copiar rutas al portapapeles.
  document.addEventListener("click", function (event) {
    const button = event.target.closest("[data-copy]");
    if (!button) return;
    event.preventDefault();
    const value = button.getAttribute("data-copy");
    if (!value) return;
    navigator.clipboard.writeText(value).then(function () {
      const original = button.textContent;
      button.textContent = "Copiado";
      setTimeout(function () { button.textContent = original; }, 1200);
    }).catch(function () {
      window.prompt("Copia la ruta:", value);
    });
  });

  function jobLabel(job) {
    return job.label || job.kind;
  }

  async function pollIndicator() {
    const target = document.getElementById("job-indicator");
    if (!target) return;
    try {
      const response = await fetch("/api/jobs?active_only=1", { headers: { Accept: "application/json" } });
      if (!response.ok) return;
      const data = await response.json();
      const jobs = data.jobs || [];
      if (!jobs.length) {
        target.innerHTML = "";
        return;
      }
      target.innerHTML = jobs.slice(0, 2).map(function (job) {
        return '<a class="pill" href="/jobs/' + encodeURIComponent(job.id) + '">' +
          '<span class="spinner"></span><span>' + escapeHtml(jobLabel(job)) +
          '</span><span class="muted">' + job.progress + "%</span></a>";
      }).join(" ");
    } catch (error) {
      /* silencioso */
    }
  }

  function escapeHtml(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // Página de detalle de un trabajo: refresco en vivo.
  function initJobLive() {
    const box = document.getElementById("job-live");
    if (!box) return;
    const jobId = box.getAttribute("data-job-id");
    const initialStatus = box.getAttribute("data-status") || "";
    if (initialStatus === "done" || initialStatus === "error" || initialStatus.indexOf("finished-") === 0) {
      return;
    }
    let finished = false;
    async function tick() {
      if (finished) return;
      try {
        const response = await fetch("/api/jobs/" + encodeURIComponent(jobId));
        if (!response.ok) return;
        const job = await response.json();
        const bar = document.getElementById("job-bar");
        const pct = document.getElementById("job-pct");
        const msg = document.getElementById("job-message");
        const status = document.getElementById("job-status");
        if (bar) bar.style.width = Math.max(0, Math.min(100, job.progress || 0)) + "%";
        if (pct) pct.textContent = (job.progress || 0) + "%";
        if (msg) msg.textContent = job.message || "";
        if (status) {
          status.textContent = job.status;
          status.className = "badge " + (job.status === "done" ? "good" : job.status === "error" ? "bad" : "accent");
        }
        if (job.status === "done" || job.status === "error") {
          finished = true;
          setTimeout(function () { window.location.reload(); }, 900);
          return;
        }
      } catch (error) {
        /* reintentar */
      }
      setTimeout(tick, 1800);
    }
    tick();
  }

  document.addEventListener("DOMContentLoaded", function () {
    pollIndicator();
    setInterval(pollIndicator, 4000);
    initJobLive();
  });
})();
