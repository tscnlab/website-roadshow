/* Static archive adaptations. Original presentation scripts are retained. */
document.addEventListener('submit', function (event) {
  if (event.target.matches('[data-archive-disabled]')) {
    event.preventDefault();
    event.stopImmediatePropagation();
  }
}, true);

// Keep the original fixed navigation below the visible portion of the notice.
const archiveBanner = document.querySelector('.archive-banner');
if (archiveBanner) {
  const updateArchiveHeader = () => {
    const offset = Math.max(0, archiveBanner.getBoundingClientRect().bottom);
    document.documentElement.style.setProperty('--archive-header-offset', `${offset}px`);
  };
  updateArchiveHeader();
  window.addEventListener('scroll', updateArchiveHeader, { passive: true });
  window.addEventListener('resize', updateArchiveHeader);
  if ('ResizeObserver' in window) {
    new ResizeObserver(updateArchiveHeader).observe(archiveBanner);
  }
}
