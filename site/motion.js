/* Motion for the VCM landing page.

   The stylesheet leaves every element in its final state, so this file only ever *replays*
   an arrival. If it never runs, fails, or is blocked, the page is complete and correct --
   just still. Counters carry their real value in the markup for the same reason.

   No dependencies, no network. */

(function () {
  "use strict";

  var reduced = false;
  try {
    reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch (e) { /* matchMedia unavailable; treat motion as allowed */ }

  var header = document.querySelector("header.site");
  function onScrollState() {
    if (header) header.classList.toggle("scrolled", window.scrollY > 24);
  }
  window.addEventListener("scroll", onScrollState, { passive: true });
  onScrollState();

  if (reduced) return;            // nothing to replay; markup is already correct
  document.documentElement.classList.add("js");

  /* Headline: wrap each word so it can rise in turn. Text content is unchanged. */
  [].slice.call(document.querySelectorAll("[data-split]")).forEach(function (el) {
    var words = el.textContent.replace(/\u00a0/g, "\u00a0").split(/(\s+)/);
    el.textContent = "";
    var i = 0;
    words.forEach(function (w) {
      if (!w) return;
      if (/^\s+$/.test(w)) { el.appendChild(document.createTextNode(" ")); return; }
      var s = document.createElement("span");
      s.className = "wd"; s.style.setProperty("--i", i++); s.textContent = w;
      el.appendChild(s);
    });
  });

  /* Steps: number badges pop in sequence once the row is visible. */
  [].slice.call(document.querySelectorAll("[data-steps] .stepn")).forEach(function (n, i) {
    n.style.setProperty("--k", i);
  });

  /* Flow: nodes and packets take turns, left to right. */
  [].slice.call(document.querySelectorAll("[data-flow] .fnode")).forEach(function (n, i) {
    n.style.setProperty("--n", i);
  });
  [].slice.call(document.querySelectorAll("[data-flow] .fwire")).forEach(function (w, i) {
    w.style.setProperty("--n", i);
  });

  /* Scroll progress bar, throttled to animation frames. */
  var bar = document.querySelector(".progress i");
  if (bar) {
    var ticking = false;
    var update = function () {
      var max = document.documentElement.scrollHeight - window.innerHeight;
      bar.style.transform = "scaleX(" + (max > 0 ? Math.min(1, window.scrollY / max) : 0).toFixed(4) + ")";
      ticking = false;
    };
    window.addEventListener("scroll", function () {
      if (!ticking) { ticking = true; requestAnimationFrame(update); }
    }, { passive: true });
    update();
  }

  /* Pointer glow: expose the pointer position to CSS on the stage and on cards. */
  function track(el) {
    var raf = 0, x = 0, y = 0;
    el.addEventListener("pointermove", function (e) {
      var r = el.getBoundingClientRect();
      x = e.clientX - r.left; y = e.clientY - r.top;
      if (raf) return;
      raf = requestAnimationFrame(function () {
        el.style.setProperty("--mx", x + "px");
        el.style.setProperty("--my", y + "px");
        raf = 0;
      });
    });
  }
  [].slice.call(document.querySelectorAll(".stage, .card")).forEach(track);

  /* Stagger children of a group so a row of cards arrives in sequence. */
  [].slice.call(document.querySelectorAll("[data-stagger]")).forEach(function (group) {
    var step = parseInt(group.getAttribute("data-stagger"), 10) || 90;
    [].slice.call(group.children).forEach(function (child, i) {
      child.setAttribute("data-reveal", "");
      child.style.setProperty("--d", (i * step) + "ms");
    });
  });

  /* Terminal lines carry their own stagger offset. */
  [].slice.call(document.querySelectorAll(".term pre .ln")).forEach(function (line, i) {
    line.style.setProperty("--ld", ((i % 30) * 55) + "ms");
  });

  /* Rings are drawn from their real percentage; the sweep animates from empty to that. */
  [].slice.call(document.querySelectorAll(".ring-svg")).forEach(function (svg) {
    var val = svg.querySelector("circle.val");
    if (!val) return;
    var circ = 2 * Math.PI * parseFloat(val.getAttribute("r"));
    svg.style.setProperty("--circ", circ.toFixed(1));
  });

  /* ------------------------------------------------------------- counters */

  function fmt(n) { return String(Math.round(n)); }

  function runCounter(el) {
    var target = parseFloat(el.getAttribute("data-count"));
    if (isNaN(target)) return;
    var final = el.textContent;                       // whatever the markup already says
    var started = null, dur = 1100, done = false;

    function finish() {
      if (done) return;
      done = true;
      el.textContent = final;                         // always end on the markup's value
    }
    // If rAF is throttled or never runs, the real number is restored anyway.
    window.setTimeout(finish, dur + 400);

    function step(ts) {
      if (done) return;
      if (started === null) started = ts;
      var p = Math.min(1, (ts - started) / dur);
      el.textContent = fmt(target * (1 - Math.pow(1 - p, 3)));
      if (p < 1) requestAnimationFrame(step);
      else finish();
    }
    requestAnimationFrame(step);
  }

  /* --------------------------------------------------------------- replay */

  var targets = [].slice.call(document.querySelectorAll(
    "[data-reveal], [data-steps], [data-flow], .art, .term, .blast, .sync, .meter, .ring-svg, [data-count]"));

  function play(el) {
    if (el.classList.contains("in")) return;
    el.classList.add("in");
    if (el.hasAttribute("data-count")) runCounter(el);
  }

  if (!("IntersectionObserver" in window)) {
    targets.forEach(play);
    return;
  }

  /* If observers never fire -- a stalled callback, an odd embedding -- replay everything
     rather than leave an entrance animation parked on its hidden first frame. */
  window.setTimeout(function () { targets.forEach(play); }, 2500);

  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      play(entry.target);
      io.unobserve(entry.target);
    });
  }, { rootMargin: "0px 0px -10% 0px", threshold: 0.12 });

  targets.forEach(function (el) { io.observe(el); });
})();
