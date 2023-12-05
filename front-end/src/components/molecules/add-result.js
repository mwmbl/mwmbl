import define from '../../utils/define.js';
import config from "../../../config.js";
import {globalBus} from "../../utils/events.js";


const FETCH_URL = '/app/fetch?'


export default define('add-result', class extends HTMLDivElement {
  connectedCallback() {
    this.classList.add('modal');
    this.__setup();
  }

  __setup() {
    this.__events();
    this.style.display = 'none';
  }

  __events() {
    this.querySelector('.close').addEventListener('click', e => {
      this.style.display = 'none';
    });

    this.addEventListener('click', e => {
      this.style.display = 'none';
    });

    this.querySelector('form').addEventListener('click', e => {
      // Clicking on the form shouldn't close it
      e.stopPropagation();
    });
  }
}, { extends: 'div' });
