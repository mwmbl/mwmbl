import define from '../utils/define.js';
import addResult from "./molecules/add-result.js";
import save from "./organisms/save.js";

const template = () => /*html*/`
  <header class="search-menu">
    <ul>
      <li is="${save}"></li>
    </ul>
    <div class="branding">
      <img class="brand-icon" src="/images/logo.svg" width="40" height="40" alt="mwmbl logo">
      <span class="brand-title">MWMBL</span>
    </div>
    <mwmbl-search-bar></mwmbl-search-bar>
  </header>
  <main>
    <mwmbl-results></mwmbl-results>
  </main>
  <div is="${addResult}"></div>
  <footer is="mwmbl-footer"></footer>
`;

export default define('app', class extends HTMLElement {
  constructor() {
    super();
    this.__setup();
  }

  __setup() {
    this.innerHTML = template();
  }
});