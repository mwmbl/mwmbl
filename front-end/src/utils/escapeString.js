/**
 * Escapes string with HTML Characters Codes.
 * @param {string} input String to escape
 * @returns {string}
 */
export default (input) => {
  return String(input).replace(/[^\w. ]/gi, (character) => {
    return `&#${character.charCodeAt(0)};`;
  });
}