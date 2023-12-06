/**
 * This file is mainly used as an entry point
 * to import components or define globals.
 * 
 * Please do not pollute this file if you can make
 * util or component files instead.
 */
import 'vite/modulepreload-polyfill';
import {setupResultsLoadedListener} from "./utils/events.js";

// Waiting for top-level await to be better supported.
(async () => {
  // Check if a suggestion redirect is needed.
  const { redirectToSuggestions } = await import("./utils/suggestions.js");
  const redirected = redirectToSuggestions();
  setupResultsLoadedListener();

  if (!redirected) {
    // Load components only after redirects are checked.
    import("./components/molecules/add-button.js");
    import("./components/molecules/add-result.js");
    import("./components/molecules/result.js");
  }
})();
