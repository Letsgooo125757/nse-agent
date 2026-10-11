// NSE desk: small progressive enhancements. Every page works without JS
// except the price chart; forms post normally if htmx is unavailable.
(function () {
  "use strict";

  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  // ---------------------------------------------------------- price chart
  function priceChart() {
    const box = document.getElementById("price-chart");
    const dataEl = document.getElementById("price-data");
    if (!box || !dataEl || !window.Chart) return;
    const data = JSON.parse(dataEl.textContent);
    if (!data.points.length) return;
    const canvas = box.querySelector("canvas");
    const all = data.points.map(([d, v]) => ({ x: d, y: v }));
    const fmt = new Intl.NumberFormat("en-KE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const dateFmt = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" });

    const line = css("--chart-line");
    const chart = new Chart(canvas, {
      type: "line",
      data: {
        labels: all.map((p) => p.x),
        datasets: [{
          label: data.label, data: all.map((p) => p.y),
          borderColor: line, borderWidth: 2, pointRadius: 0, pointHoverRadius: 5,
          pointHoverBackgroundColor: line, pointHoverBorderColor: css("--surface"), pointHoverBorderWidth: 2,
          fill: true, backgroundColor: line + "1a", tension: 0, borderJoinStyle: "round", borderCapStyle: "round",
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            displayColors: false,
            callbacks: {
              title: (items) => dateFmt.format(new Date(items[0].label)),
              label: (item) => "KES " + fmt.format(item.parsed.y),
            },
          },
        },
        scales: {
          x: {
            grid: { display: false }, border: { color: css("--line") },
            ticks: { color: css("--muted"), maxTicksLimit: 6, maxRotation: 0,
                     callback(v) { return dateFmt.format(new Date(this.getLabelForValue(v))).replace(/^\d+ /, ""); } },
          },
          y: {
            grid: { color: css("--grid") }, border: { display: false },
            ticks: { color: css("--muted"), maxTicksLimit: 5, callback: (v) => fmt.format(v) },
          },
        },
      },
    });

    // range buttons: 1M / 3M / 6M / 1Y / All
    document.querySelectorAll("[data-range]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const days = Number(btn.dataset.range);
        const last = new Date(all[all.length - 1].x);
        const from = days ? new Date(last.getTime() - days * 86400000) : null;
        const pts = from ? all.filter((p) => new Date(p.x) >= from) : all;
        chart.data.labels = pts.map((p) => p.x);
        chart.data.datasets[0].data = pts.map((p) => p.y);
        chart.update();
        document.querySelectorAll("[data-range]").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
      });
    });
  }

  // ---------------------------------------------------------- assistant
  function assistant() {
    const form = document.getElementById("ask-form");
    if (!form) return;
    const box = form.querySelector("textarea");
    const chat = document.getElementById("chat");
    const scroll = () => window.scrollTo({ top: document.body.scrollHeight });

    box.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); form.requestSubmit(); }
    });
    document.querySelectorAll("[data-suggest]").forEach((b) =>
      b.addEventListener("click", () => { box.value = b.dataset.suggest; box.focus(); }));
    form.addEventListener("htmx:beforeRequest", () => {
      const pending = document.createElement("div");
      pending.className = "q"; pending.id = "pending-q"; pending.textContent = box.value.trim();
      chat.appendChild(pending); scroll();
    });
    form.addEventListener("htmx:afterRequest", (e) => {
      const p = document.getElementById("pending-q"); if (p) p.remove();
      if (e.detail.successful) { box.value = ""; scroll(); }
      box.focus();
    });
    scroll();
  }

  document.addEventListener("DOMContentLoaded", () => { priceChart(); assistant(); });
})();
