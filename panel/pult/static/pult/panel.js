/* Пульт beets-janitor: плееры, шкала сходства, обложки, тосты.
   Всё, что меняет данные, идёт через htmx; здесь только поведение страницы. */
(function () {
  "use strict";
  var toastEl = null, toastTimer = null;

  function toast(t) {
    toastEl = toastEl || document.getElementById("toast");
    if (!toastEl || !t) return;
    toastEl.textContent = t;
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove("show"); }, 2600);
  }
  window.pultToast = toast;
  document.body.addEventListener("toast", function (e) { toast(e.detail && e.detail.value); });
  document.body.addEventListener("htmx:responseError", function (e) {
    toast("Не вышло: сервер ответил " + (e.detail.xhr ? e.detail.xhr.status : "ошибкой"));
  });
  document.body.addEventListener("htmx:sendError", function () { toast("Пульт не отвечает — проверьте связь"); });

  /* ---------- шкала сходства ---------- */
  function zonesFor(m) {
    var bad = +(m.dataset.bad || 60), ok = +(m.dataset.ok || 85);
    if (m.dataset.mode === "pair") {
      var ask = +(m.dataset.ask || 55), same = +(m.dataset.same || 65);
      return [[0, ask, "z-bad", "другая песня"], [ask, same, "z-grey", "серая зона"],
              [same, 85, "z-mid", "тот же трек, другой мастеринг"], [85, 100, "z-same", "та же запись"]];
    }
    return [[0, bad, "z-bad", "другая песня"], [bad, ok, "z-grey", "серая зона"], [ok, 100, "z-same", "та же запись"]];
  }
  function meter(m) {
    if (m.dataset.done) return;
    m.dataset.done = "1";
    var v = Math.max(0, Math.min(100, parseInt(m.dataset.value, 10) || 0)), zones = zonesFor(m);
    var zone = zones.filter(function (z) { return v >= z[0] && v < z[1]; })[0] || zones[zones.length - 1];
    var col = zone[2] === "z-bad" ? "var(--bad)" : (zone[2] === "z-grey" ? "var(--warn)" : "var(--ok)");
    var segs = zones.map(function (z) { return '<span class="' + z[2] + '" style="width:' + (z[1] - z[0]) + '%"></span>'; }).join("");
    var ticks = '<span style="left:0%;transform:none">0</span>' + zones.slice(1).map(function (z) {
      return '<span style="left:' + z[0] + '%">' + z[0] + '</span>'; }).join("") +
      '<span style="left:100%;transform:translateX(-100%)">100</span>';
    m.innerHTML =
      '<div class="meter-top"><span><span class="meter-val" style="color:' + col + '">' + v + '%</span> ' +
      '<span class="muted" style="font-size:12.5px">' + (m.dataset.label || "совпадение отпечатков") + '</span></span>' +
      '<span class="meter-verdict" style="color:' + col + '">' + zone[3] + '</span></div>' +
      '<div class="scale-wrap"><div class="scale" role="img" aria-label="Совпадение ' + v + ' процентов, ' + zone[3] + '">' +
      segs + '</div><span class="marker" style="left:' + v + '%"></span></div><div class="ticks">' + ticks + '</div>';
  }

  /* ---------- плееры ---------- */
  function rnd(seed) { var s = seed * 9301 + 49297; return function () { s = (s * 9301 + 49297) % 233280; return s / 233280; }; }
  function wave(w) {
    if (w.dataset.done) return;
    w.dataset.done = "1";
    var r = rnd(parseInt(w.dataset.seed || "1", 10)), n = 56, html = "";
    for (var i = 0; i < n; i++) {
      var env = Math.sin(Math.PI * i / n) * .55 + .45;
      html += '<i style="height:' + Math.max(12, Math.round((r() * .7 + .3) * env * 100)) + '%"></i>';
    }
    w.innerHTML = html;
  }
  var current = null;
  function stop() {
    if (!current) return;
    current.audio.pause();
    current.btn.setAttribute("aria-pressed", "false");
    current = null;
  }
  function player(p) {
    var btn = p.querySelector(".play"), audio = p.querySelector("audio");
    if (!btn || !audio || btn.dataset.done) return;
    btn.dataset.done = "1";
    var bars = p.querySelectorAll(".wave i");
    audio.addEventListener("timeupdate", function () {
      if (!audio.duration) return;
      var k = Math.round(bars.length * audio.currentTime / audio.duration);
      for (var i = 0; i < bars.length; i++) bars[i].classList.toggle("on", i < k);
    });
    audio.addEventListener("ended", stop);
    audio.addEventListener("error", function () { stop(); toast("Не удалось проиграть: файла нет или источник не отдал превью"); });
    btn.addEventListener("click", function () {
      if (current && current.btn === btn) { stop(); return; }
      stop();
      current = {btn: btn, audio: audio};
      btn.setAttribute("aria-pressed", "true");
      var pr = audio.play();
      if (pr && pr.catch) pr.catch(function () { stop(); toast("Браузер не дал проиграть звук"); });
    });
    // перемотка кликом по волне
    var w = p.querySelector(".wave");
    if (w) w.addEventListener("click", function (e) {
      if (!audio.duration) return;
      var rect = w.getBoundingClientRect();
      audio.currentTime = audio.duration * (e.clientX - rect.left) / rect.width;
    });
  }

  /* ---------- обложки: перетаскивание ---------- */
  function drop(d) {
    if (d.dataset.done) return;
    d.dataset.done = "1";
    var input = d.querySelector("input[type=file]");
    function preview(file) {
      if (!file || !/^image\//.test(file.type)) { toast("Нужна картинка: JPG, PNG или WebP"); return false; }
      var fr = new FileReader();
      fr.onload = function () { d.style.backgroundImage = "url(" + fr.result + ")"; d.classList.add("filled"); };
      fr.readAsDataURL(file);
      return true;
    }
    input.addEventListener("change", function () { if (input.files[0]) preview(input.files[0]); });
    d.addEventListener("dragover", function (e) { e.preventDefault(); d.classList.add("over"); });
    d.addEventListener("dragleave", function () { d.classList.remove("over"); });
    d.addEventListener("drop", function (e) {
      e.preventDefault();
      d.classList.remove("over");
      var f = e.dataTransfer.files[0];
      if (!f || !preview(f)) return;
      var dt = new DataTransfer();
      dt.items.add(f);
      input.files = dt.files;
      input.dispatchEvent(new Event("change", {bubbles: true}));
    });
  }

  /* ---------- подтверждения ---------- */
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-confirm]");
    if (b) { var c = document.getElementById("confirm-" + b.dataset.confirm); if (c) c.classList.add("show"); return; }
    var x = e.target.closest("[data-close-confirm]");
    if (x) { var box = x.closest(".confirm"); if (box) box.classList.remove("show"); }
  });

  function init(root) {
    root.querySelectorAll(".meter[data-value]").forEach(meter);
    root.querySelectorAll(".wave").forEach(wave);
    root.querySelectorAll(".player").forEach(player);
    root.querySelectorAll(".drop").forEach(drop);
  }
  if (window.htmx) { htmx.onLoad(init); } else { init(document); }
})();
