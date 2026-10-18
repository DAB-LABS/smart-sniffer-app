/* Agentic access: the Copy and Download buttons of the page proxy.py
   serves at <ingress>/agentic-access. The JSON is already in the page, so
   both use exactly the text shown. No Alpine, no fetch. */
(function () {
  const pre = document.getElementById('ax-json');
  const status = document.getElementById('ax-status');
  const copyBtn = document.getElementById('ax-copy');
  const downloadBtn = document.getElementById('ax-download');
  if (!pre || !copyBtn || !downloadBtn) return;
  let timer = null;

  function say(text) {
    status.textContent = text;
    clearTimeout(timer);
    timer = setTimeout(() => { status.textContent = ''; }, 1600);
  }

  // The clipboard needs a secure context; ingress over plain http is not
  // one, so the text is selected instead, ready for Ctrl+C.
  copyBtn.addEventListener('click', async () => {
    const text = pre.textContent;
    let ok = false;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        ok = true;
      }
    } catch (e) { ok = false; }
    if (!ok) {
      const range = document.createRange();
      range.selectNodeContents(pre);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      if (!ok) {
        say('Selected, press Ctrl+C');
        return;
      }
    }
    say('Copied');
  });

  // The file is named by the document's generated_at date, in UTC.
  downloadBtn.addEventListener('click', () => {
    const blob = new Blob([pre.textContent], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = downloadBtn.dataset.file || 'smart-sniffer.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  });
}());
