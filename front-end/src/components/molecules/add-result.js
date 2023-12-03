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

    this.addEventListener('submit', this.__urlSubmitted.bind(this));
  }

  async __urlSubmitted(e) {
    e.preventDefault();
    const value = this.querySelector('input').value;
    console.log("Input value", value);

    const query = document.querySelector('.search-bar input').value;

    const url = `${FETCH_URL}url=${encodeURIComponent(value)}&query=${encodeURIComponent(query)}`;
    const response = await fetch(url);
    if (response.status === 200) {
      const data = await response.text();
      console.log("Data", data);

      const addResultEvent = new CustomEvent('curate-add-result', {detail: data});
      globalBus.dispatch(addResultEvent);
    } else {
      console.log("Bad response", response);
      // TODO
    }
  }
}, { extends: 'div' });
