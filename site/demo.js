/* Hero demo: switch between real findings from the bundled sample node (v1.0 -> v1.1).
   No dependencies, no network. Markup works without it: the first scenario is rendered here,
   and the page is still complete if this file never runs. */
(function () {
  "use strict";
  var out = document.getElementById("demo-out");
  var run = document.getElementById("demo-run");
  if (!out) return;

  // Each scenario is lines of [css class, text]. All values come from the sample report.
  var S = {
    power: [
      ["c", "[!! CRITICAL] R001  PSU_BANK cannot supply the node under its redundancy policy"],
      ["d", "4 supplies totalling 8000 W installed, 4000 W usable under 2+2."],
      ["d", "Declared draw 4190 W. Losing a supply would drop the node."],
      ["r", "confidence: rule_based"]
    ],
    driver: [
      ["c", "[!! CRITICAL] R006  Installed accelerator driver is below the required minimum"],
      ["d", "Node declares 470.223.02; H100 NVL requires 525.60.13."],
      ["r", "confidence: rule_based"]
    ],
    conn: [
      ["w", "[ ! WARNING] R007  Power connector changed: 8-pin EPS → 12VHPWR on 8 devices"],
      ["d", "The existing harness does not fit. This is physical rework, not a software change."],
      ["r", "confidence: rule_based"]
    ],
    drift: [
      ["c", "[!! CRITICAL] V007  The base configuration does not match the machine it describes"],
      ["d", "Observed by redfish, nvidia-smi: DIMM11 not reported by the machine."],
      ["v", "confidence: verified"]
    ]
  };

  var cur = "power", timer = null;
  var reduce = false;
  try { reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) {}

  function paint(key, animate) {
    cur = key;
    if (timer) { clearTimeout(timer); timer = null; }
    out.textContent = "";
    var lines = S[key], i = 0;
    function next() {
      if (i >= lines.length) return;
      var d = document.createElement("div");
      d.className = "ln " + lines[i][0];
      d.textContent = lines[i][1];
      out.appendChild(d);
      i++;
      if (animate && !reduce) timer = setTimeout(next, 260); else next();
    }
    next();
  }

  var order = ["power", "driver", "conn", "drift"];
  var chips = [].slice.call(document.querySelectorAll(".chipb"));
  var CYCLE = 6000, auto = !reduce, hold = false, onscreen = true, timer2 = null;
  var demo = document.getElementById("demo");
  if (demo) demo.style.setProperty("--cycle", CYCLE + "ms");

  function mark(key) {
    chips.forEach(function (c) {
      var on = c.getAttribute("data-k") === key;
      c.classList.toggle("on", on);
      c.setAttribute("aria-selected", on ? "true" : "false");
      c.classList.remove("auto");
    });
  }
  function startBar(key) {
    var b = chips.filter(function (c) { return c.getAttribute("data-k") === key; })[0];
    if (!b) return;
    void b.offsetWidth;                      // restart the fill animation
    b.classList.add("auto");
  }
  function clearAuto() { if (timer2) { clearTimeout(timer2); timer2 = null; } }
  function schedule() {
    clearAuto();
    if (!auto || hold || !onscreen) return;
    startBar(cur);
    timer2 = setTimeout(function () {
      var next = order[(order.indexOf(cur) + 1) % order.length];
      mark(next); paint(next, true); schedule();
    }, CYCLE);
  }
  function stopAuto() {
    auto = false; clearAuto();
    chips.forEach(function (c) { c.classList.remove("auto"); });
  }

  chips.forEach(function (b) {
    b.addEventListener("click", function () {
      stopAuto();                            // a person took over; never fight them
      mark(b.getAttribute("data-k"));
      paint(b.getAttribute("data-k"), true);
    });
  });
  if (run) run.addEventListener("click", function () { stopAuto(); paint(cur, true); });

  /* Pause while the pointer is over the card or focus is inside it, and while it is off screen. */
  if (demo) {
    var pause = function () { hold = true; clearAuto(); chips.forEach(function (c) { c.classList.remove("auto"); }); };
    var resume = function () { hold = false; schedule(); };
    demo.addEventListener("pointerenter", pause);
    demo.addEventListener("pointerleave", resume);
    demo.addEventListener("focusin", pause);
    demo.addEventListener("focusout", resume);
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(function (es) {
        onscreen = es[0].isIntersecting;
        if (onscreen) schedule(); else clearAuto();
      }, { threshold: 0.3 }).observe(demo);
    }
  }

  paint("power", false);
  schedule();
})();
