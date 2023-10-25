import define from '../../utils/define.js';
import config from "../../../config.js";
import {globalBus} from "../../utils/events.js";


const FETCH_URL = `${config['publicApiURL']}crawler/fetch?`


const template = () => /*html*/`
    <form class="modal-content">
      <span class="close">&times;</span>
      <input class="add-result" placeholder="Enter a URL...">
      <button>Save</button>
    </form>
`;

export default define('add-result', class extends HTMLDivElement {
  constructor() {
    super();
    this.classList.add('modal');
    this.__setup();
  }

  __setup() {
    this.innerHTML = template();
    this.__events();
    this.style.display = 'none';
  }

  __events() {
    this.querySelector('.close').addEventListener('click', e => {
      if (e.target === this) {
        this.style.display = 'none';
      }
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
      const data = await response.json();
      console.log("Data", data);

      const addResultEvent = new CustomEvent('curate-add-result', {detail: data});
      globalBus.dispatch(addResultEvent);
    } else {
      console.log("Bad response", response);
      // TODO
    }
  }
}, { extends: 'div' });
