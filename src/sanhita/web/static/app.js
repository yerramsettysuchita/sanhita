/* Sanhita workbench. Vanilla JS, no build step, no dependencies.
   Two jobs: light the provenance span when a compiled field is hovered, and
   keep a keyboard-only reviewer moving through the queue. */

(function () {
  "use strict";

  /* ── 1. Provenance highlighting ─────────────────────────────────────────
     The single most persuasive interaction in the product: hovering a field on
     the right lights the words on the left that produced it. Spans overlap, so
     each <mark> carries every field covering it and we match on membership. */

  function marksFor(scope, field) {
    return scope.querySelectorAll('mark.prov[data-fields~="' + field + '"]');
  }

  function lightUp(scope, field, on) {
    marksFor(scope, field).forEach(function (mark) {
      mark.classList.toggle("is-lit", on);
    });
    if (on) {
      var first = scope.querySelector('mark.prov[data-fields~="' + field + '"]');
      if (first && first.scrollIntoView) {
        var box = first.getBoundingClientRect();
        var pane = scope.querySelector(".verbatim");
        if (pane) {
          var paneBox = pane.getBoundingClientRect();
          if (box.top < paneBox.top || box.bottom > paneBox.bottom) {
            first.scrollIntoView({ block: "center", behavior: "smooth" });
          }
        }
      }
    }
  }

  document.querySelectorAll("section.spine").forEach(function (spine) {
    spine.querySelectorAll(".field-row").forEach(function (row) {
      var field = row.getAttribute("data-field");
      if (!field) return;

      var enter = function () { lightUp(spine, field, true); row.classList.add("is-active"); };
      var leave = function () { lightUp(spine, field, false); row.classList.remove("is-active"); };

      row.addEventListener("mouseenter", enter);
      row.addEventListener("mouseleave", leave);
      // Keyboard parity: tabbing through the fields highlights too.
      row.addEventListener("focus", enter);
      row.addEventListener("blur", leave);
    });

    // Hovering the regulation lights the fields it produced, which is the same
    // relationship read from the other direction.
    spine.querySelectorAll("mark.prov").forEach(function (mark) {
      var fields = (mark.getAttribute("data-fields") || "").split(/\s+/).filter(Boolean);
      mark.addEventListener("mouseenter", function () {
        fields.forEach(function (f) {
          var row = spine.querySelector('.field-row[data-field="' + f + '"]');
          if (row) row.classList.add("is-active");
        });
        mark.classList.add("is-lit");
      });
      mark.addEventListener("mouseleave", function () {
        fields.forEach(function (f) {
          var row = spine.querySelector('.field-row[data-field="' + f + '"]');
          if (row) row.classList.remove("is-active");
        });
        mark.classList.remove("is-lit");
      });
    });
  });

  /* ── 2. Keyboard navigation ─────────────────────────────────────────────
     Someone working through 1,377 rules will not use a mouse. */

  function typing(event) {
    var el = event.target;
    if (!el) return false;
    var tag = (el.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select" || el.isContentEditable;
  }

  document.addEventListener("keydown", function (event) {
    if (typing(event) || event.metaKey || event.ctrlKey || event.altKey) return;
    var key = event.key.toLowerCase();
    var nav = window.SANHITA_NAV || {};

    if (key === "j" && nav.next) { window.location.href = (window.SANHITA_BASE || "") + "/clause/" + nav.next; return; }
    if (key === "k" && nav.prev) { window.location.href = (window.SANHITA_BASE || "") + "/clause/" + nav.prev; return; }

    if (key === "c") {
      var certify = document.querySelector(".act-certify:not([data-blocked]) button[data-key='c']");
      if (certify) { event.preventDefault(); certify.focus(); openForm(certify); }
      return;
    }
    if (key === "e" || key === "r") {
      var summary = document.querySelector("summary[data-key='" + key + "']");
      if (summary) {
        event.preventDefault();
        var details = summary.parentElement;
        details.open = true;
        var input = details.querySelector("input.field");
        if (input) input.focus();
      }
      return;
    }

    /* Queue screen: J/K move the cursor, Enter opens it. The queue moved from a
       table to a card list, so this selects anything marked .row rather than
       table rows specifically. */
    var rows = Array.prototype.slice.call(document.querySelectorAll(".row[data-clause]"));
    if (!rows.length) return;
    var current = document.querySelector(".row[data-clause].is-cursor");
    var index = current ? rows.indexOf(current) : -1;

    if (key === "j" || key === "k") {
      event.preventDefault();
      index = key === "j" ? Math.min(index + 1, rows.length - 1) : Math.max(index - 1, 0);
      rows.forEach(function (r) { r.classList.remove("is-cursor"); });
      rows[index].classList.add("is-cursor");
      rows[index].scrollIntoView({ block: "nearest" });
    } else if (key === "enter" && current) {
      window.location.href = (window.SANHITA_BASE || "") + "/clause/" + current.getAttribute("data-clause");
    }
  });

  function openForm(button) {
    var form = button.closest("form");
    if (!form) return;
    var name = form.querySelector("input[name='by']");
    if (name && !name.value) name.focus();
  }

  /* Queue rows are clickable. */
  document.querySelectorAll(".row[data-clause]").forEach(function (row) {
    row.addEventListener("click", function (event) {
      if (event.target.closest("a, input")) return;
      window.location.href = (window.SANHITA_BASE || "") + "/clause/" + row.getAttribute("data-clause");
    });
  });

  /* ── 3. Bulk selection. Rejection only, by design ───────────────────── */

  var bulk = document.getElementById("bulk");
  if (bulk) {
    var boxes = document.querySelectorAll(".rowsel");
    boxes.forEach(function (box) {
      box.addEventListener("change", function () {
        var n = document.querySelectorAll(".rowsel:checked").length;
        document.getElementById("bulk-n").textContent = String(n);
        bulk.hidden = n === 0;
      });
    });
  }

  /* ── 4. Verify-all-signatures ───────────────────────────────────────── */

  var verify = document.getElementById("verify-btn");
  if (verify) {
    verify.addEventListener("click", function () {
      var out = document.getElementById("verify-out");
      out.textContent = "Checking...";
      out.className = "t-small";
      fetch((window.SANHITA_BASE || "") + "/audit/verify", { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.error) { out.textContent = data.error; out.className = "t-small bad"; return; }
          if (data.ok) {
            out.textContent = "All good. " + data.valid + " of " + data.checked +
              " signatures match and the chain is intact.";
            out.className = "t-small good";
          } else {
            var tampered = data.tampered.length
              ? data.tampered.join(", ")
              : "none";
            out.textContent = "Problem found. Tampered rules: " + tampered +
              ". Chain problems: " + data.ledger_problems.length + ".";
            out.className = "t-small bad";
          }
        })
        .catch(function () {
          out.textContent = "verification request failed";
          out.className = "t-small bad";
        });
    });
  }

  /* ── 5. Certification animation ─────────────────────────────────────────
     Motion happens on certification and nowhere else. */
  var params = new URLSearchParams(window.location.search);
  var justCertified = params.get("certified");
  if (justCertified) {
    var seal = document.querySelector('section.spine[data-rule="' + justCertified + '"] .certified-seal');
    if (seal) seal.classList.add("just-sealed");
  }
})();
