import define from "../../utils/define.js";
import {globalBus} from "../../utils/events.js";
import addResult from "./add-result.js";
import emptyResult from "./empty-result.js";


export default define('add-button', class extends HTMLButtonElement {
  constructor() {
    super();
    this.__setup();
  }

  __setup() {
    this.__events();
  }

  __events() {
    this.addEventListener('click', (e) => {
      console.log("Add button");
      document.querySelector('.modal').style.display = 'block';
      document.querySelector('.modal input').focus();
    })
  }
}, { extends: 'button' });
