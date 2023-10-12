import define from '../../utils/define.js';

const template = () => /*html*/`
  <p>We could not find anything for your search...</p>
`;

export default define('empty-result', class extends HTMLLIElement {
  constructor() {
    super();
    this.classList.add('empty-result');
    this.__setup();
  }

  __setup() {
    this.innerHTML = template();
  }
}, { extends: 'li' });