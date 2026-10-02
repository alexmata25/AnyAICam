/* Click-to-enlarge for AnyAiCam app screenshots.
   Each screenshot is wrapped in <a class="app-shot-zoom" href="full-size.webp">, so without
   JavaScript (or without <dialog> support) the link simply opens the full-size image. */
(function () {
  var links = document.querySelectorAll('a.app-shot-zoom');
  if (!links.length || typeof HTMLDialogElement !== 'function') return;

  var dialog = document.createElement('dialog');
  dialog.className = 'app-shot-dialog';
  dialog.setAttribute('aria-label', 'Enlarged app screenshot');
  dialog.innerHTML =
    '<button type="button" class="app-shot-close" aria-label="Close enlarged screenshot">&times;</button>' +
    '<img alt=""><p class="app-shot-dialog-caption"></p>';
  document.body.appendChild(dialog);

  var img = dialog.querySelector('img');
  var caption = dialog.querySelector('.app-shot-dialog-caption');

  function close() { dialog.close(); }

  dialog.querySelector('.app-shot-close').addEventListener('click', close);
  // A click on the dark backdrop (the dialog itself, not its contents) closes it.
  dialog.addEventListener('click', function (e) { if (e.target === dialog) close(); });
  dialog.addEventListener('close', function () { img.removeAttribute('src'); });

  Array.prototype.forEach.call(links, function (link) {
    link.addEventListener('click', function (e) {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;
      e.preventDefault();
      var thumb = link.querySelector('img');
      var figcaption = link.closest('figure') && link.closest('figure').querySelector('figcaption');
      img.src = link.getAttribute('href');
      img.alt = thumb ? thumb.alt : '';
      caption.textContent = figcaption ? figcaption.textContent : '';
      dialog.showModal();
    });
  });
})();
