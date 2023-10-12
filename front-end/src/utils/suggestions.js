/**
 * Handle redirect requests from the suggestion back-end.
 */


import config from "../../config.js";

const redirectToSuggestions = () => {
  const search = decodeURIComponent(document.location.search).replace(/\+/g, ' ').substr(3);
  console.log("Search", search);
  for (const [command, urlTemplate] of Object.entries(config.commands)) {
    console.log("Command", command);
    if (search.startsWith(command)) {
      const newUrl = urlTemplate + search.substr(command.length);
      window.location.replace(newUrl);
      return true;
    }
  }
  return false;
}

export {
  redirectToSuggestions
};