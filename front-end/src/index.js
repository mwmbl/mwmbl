/**
 * This file is mainly used as an entry point
 * to import components or define globals.
 * 
 * Please do not pollute this file if you can make
 * util or component files instead.
 */
import 'vite/modulepreload-polyfill';

// Waiting for top-level await to be better supported.
(async () => {
  // Check if a suggestion redirect is needed.
  const { redirectToSuggestions } = await import("./utils/suggestions.js");
  const redirected = redirectToSuggestions();

  if (!redirected) {
    // Load components only after redirects are checked.
    import("./components/organisms/results.js");
    import("./components/organisms/save.js");
    import("./components/molecules/add-button.js");
    import("./components/molecules/add-result.js");
    import("./components/molecules/delete-button.js");
    import("./components/molecules/result.js");
    import("./components/molecules/validate-button.js");
  }
})();
