import define from '../../utils/define.js';
import config from '../../../config.js';

const template = ({ data }) => /*html*/`
  <p class="footer-text">Find more on</p>
  <ul class="footer-list">
    ${data.links.map(link => /*html*/`
      <li class="footer-item">
        <a href="${link.href}" class="footer-link" target="_blank">
          <i class="${link.icon}"></i>
          <span>${link.name}</span>
        </a>
      </li>
    `).join('')}
  </ul>
`;

export default define('footer', class extends HTMLElement {
  constructor() {
    super();
    this.__setup();
  }

  __setup() {
    this.innerHTML = template({
      data: {
        links: config.footerLinks
      }
    });
    this.__events();
  }

  __events() {

  }
}, { extends: 'footer' });