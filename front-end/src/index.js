/**
 * This file is mainly used as an entry point
 * to import components or define globals.
 * 
 * Please do not pollute this file if you can make
 * util or component files instead.
 */

// Waiting for top-level await to be better supported.
(async () => {
  // Check if a suggestion redirect is needed.
  const { redirectToSuggestions } = await import("./utils/suggestions.js");
  const redirected = redirectToSuggestions();

  if (!redirected) {
    // Load components only after redirects are checked.
    import('./components/app.js');
    import("./components/organisms/search-bar.js");
    import("./components/organisms/results.js");
    import("./components/organisms/footer.js");
  }
})();
