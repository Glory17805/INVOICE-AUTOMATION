/* Applied before the stylesheet paints, so a chosen theme never flashes the
   other one on load. Storage can throw - private windows, blocked cookies -
   and the page must still render if it does.

   In its own file rather than inline in the head: the Content-Security-Policy
   this frontend now sends forbids inline script, and weakening the policy to
   keep one four-line block inline would give up the protection that stops a
   script injection walking off with someone's session. */
try {
  var saved = localStorage.getItem("gst.theme");
  if (saved === "light" || saved === "dark") {
    document.documentElement.setAttribute("data-theme", saved);
  }
} catch (e) {
  /* fall through to the system preference */
}
