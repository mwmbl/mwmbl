import config from '../../config.js';

/** Define a web component, this is a wrapper
 *  around the `customElements.define` native function.
 * @function define 
 * @param {string} name Name of the component (will be prefixed by the config `componentPrefix`)
 * @param {CustomElementConstructor} constructor
 * @param {ElementDefinitionOptions} [options]
 * @returns {string} Returns the element name ready for the DOM (.e.g `<search-bar></search-bar>`)
 */
 export default (name, constructor, options) => { 
  const componentName = `${config.componentPrefix}-${name}`;
  if (!customElements.get(componentName)) customElements.define(componentName, constructor, options); 
  return componentName;
}