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

  var chips = [].slice.call(document.querySelectorAll(".chipb"));
  chips.forEach(function (b) {
    b.addEventListener("click", function () {
      chips.forEach(function (c) { c.classList.remove("on"); c.setAttribute("aria-selected", "false"); });
      b.classList.add("on"); b.setAttribute("aria-selected", "true");
      paint(b.getAttribute("data-k"), true);
    });
  });
  if (run) run.addEventListener("click", function () { paint(cur, true); });
  paint("power", false);
})();
