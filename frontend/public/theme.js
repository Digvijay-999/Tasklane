// Runs before the page paints: apply a remembered "light" or "dark" choice. With no choice
// (or storage blocked) the CSS follows the system setting.
try {
  var theme = localStorage.getItem('tasklane.theme');
  if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
} catch (e) {
  /* storage unavailable: follow the system */
}
