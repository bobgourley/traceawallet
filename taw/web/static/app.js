// TraceAWallet: copy buttons, printing, and Clerk sign-in.
(function () {
  "use strict";

  document.addEventListener("click", function (e) {
    var b = e.target.closest("button.copy");
    if (!b) return;
    var text = b.getAttribute("data-copy") || "";
    var done = function () { b.classList.add("copied"); setTimeout(function () { b.classList.remove("copied"); }, 1500); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () {});
    }
  });

  window.addEventListener("beforeprint", function () {
    document.querySelectorAll("details").forEach(function (d) { d.setAttribute("open", ""); });
  });
  var printButton = document.getElementById("print");
  if (printButton) printButton.addEventListener("click", function () { window.print(); });

  window.addEventListener("load", async function () {
    if (!window.Clerk) return;
    try {
      await Clerk.load({ ui: { ClerkUI: window.__internal_ClerkUICtor } });
    } catch (err) {
      return;
    }
    var box = document.getElementById("sign-in");
    if (box) {
      var next = box.getAttribute("data-next") || "/";
      if (next.charAt(0) !== "/" || next.charAt(1) === "/") next = "/";
      if (Clerk.isSignedIn) { window.location.replace(next); return; }
      Clerk.mountSignIn(box, { forceRedirectUrl: next, signUpForceRedirectUrl: next });
    }
    var out = document.getElementById("sign-out");
    if (out) out.addEventListener("click", function () { Clerk.signOut({ redirectUrl: "/" }); });
  });
})();
