/* Sanhita landing page motion. Vanilla JS, no libraries, nothing loaded from a
   network. Three jobs only:

     1. tilt the hero frame toward the pointer, in real 3D
     2. light each feature card where the cursor actually is
     3. reveal sections as they enter the viewport

   Everything reads the pointer through requestAnimationFrame and writes only
   transform and opacity, so nothing here triggers layout. If the visitor has
   asked for reduced motion, the whole file turns itself off and the page is
   simply static. */

(function () {
  "use strict";

  var reduced = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ── 1. Hero frame tilt ─────────────────────────────────────────────────
     The frame sits on a perspective parent. Pointer position maps to a small
     rotation, kept deliberately shallow so text on the frame stays readable.
     The floating chips move further than the frame, which is what sells the
     depth. */

  var stage = document.querySelector(".lp-stage");
  var frame = document.querySelector(".lp-frame");
  var floats = Array.prototype.slice.call(document.querySelectorAll(".lp-float"));

  if (stage && frame && !reduced) {
    var target = { x: 0, y: 0 };
    var current = { x: 0, y: 0 };
    var raf = null;

    function onMove(event) {
      var box = stage.getBoundingClientRect();
      // Normalise to roughly -1..1 around the centre of the stage.
      target.x = ((event.clientX - box.left) / box.width - 0.5) * 2;
      target.y = ((event.clientY - box.top) / box.height - 0.5) * 2;
      if (!raf) raf = requestAnimationFrame(tick);
    }

    function tick() {
      // Ease toward the pointer rather than snapping to it.
      current.x += (target.x - current.x) * 0.08;
      current.y += (target.y - current.y) * 0.08;

      /* The frame barely turns now. Most of the response to the pointer has
         moved into a light that travels across its surface, which reads as the
         same depth without the text underneath swinging about. */
      var ry = current.x * 2.5;            // degrees, was 7
      var rx = 12 - current.y * 2;         // 12deg resting tilt, was 13 +/- 6
      frame.style.setProperty("--ry", ry.toFixed(2) + "deg");
      frame.style.setProperty("--rx", rx.toFixed(2) + "deg");

      // Where the specular highlight sits, as a percentage of the frame.
      frame.style.setProperty("--mx", (50 + current.x * 38).toFixed(1) + "%");
      frame.style.setProperty("--my", (34 + current.y * 26).toFixed(1) + "%");

      /* Small travel on purpose. These chips used to move up to 36px, which at
         the edge of their excursion carried them over the frame's own text.
         Depth still separates them from the frame, but the furthest any of
         them now goes is 12px, so nothing can drift onto a label. */
      floats.forEach(function (chip, i) {
        var depth = 6 + i * 3;
        chip.style.transform =
          "translate3d(" + (-current.x * depth).toFixed(1) + "px," +
          (-current.y * depth * 0.5).toFixed(1) + "px,0)";
      });

      if (Math.abs(target.x - current.x) > 0.001 ||
          Math.abs(target.y - current.y) > 0.001) {
        raf = requestAnimationFrame(tick);
      } else {
        raf = null;
      }
    }

    function onLeave() {
      target.x = 0; target.y = 0;
      if (!raf) raf = requestAnimationFrame(tick);
    }

    window.addEventListener("pointermove", onMove, { passive: true });
    document.addEventListener("pointerleave", onLeave);
  }

  /* ── 2. Cursor lit cards ────────────────────────────────────────────────
     Each card gets a soft highlight positioned where the pointer is, plus a
     very small tilt. Subtle on purpose. A card that leans hard looks like a
     template; a card that leans two degrees looks considered. */

  if (!reduced) {
    document.querySelectorAll(".lp-card").forEach(function (card) {
      card.addEventListener("pointermove", function (event) {
        var box = card.getBoundingClientRect();
        var px = (event.clientX - box.left) / box.width;
        var py = (event.clientY - box.top) / box.height;

        card.style.setProperty("--mx", (px * 100).toFixed(1) + "%");
        card.style.setProperty("--my", (py * 100).toFixed(1) + "%");
        card.style.transform =
          "perspective(900px) rotateY(" + ((px - 0.5) * 4).toFixed(2) +
          "deg) rotateX(" + ((0.5 - py) * 4).toFixed(2) + "deg) translateY(-4px)";
      }, { passive: true });

      card.addEventListener("pointerleave", function () {
        card.style.transform = "";
      });
    });
  }

  /* ── 3. Scroll reveal ───────────────────────────────────────────────────
     IntersectionObserver, unobserving on first entry so a section never
     animates twice. Anything without the observer available just shows. */

  var revealables = document.querySelectorAll(".reveal");

  if (reduced || !("IntersectionObserver" in window)) {
    revealables.forEach(function (el) { el.classList.add("is-in"); });
    return;
  }

  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      entry.target.classList.add("is-in");
      observer.unobserve(entry.target);
    });
  }, { rootMargin: "0px 0px -12% 0px", threshold: 0.08 });

  revealables.forEach(function (el) { observer.observe(el); });
})();
