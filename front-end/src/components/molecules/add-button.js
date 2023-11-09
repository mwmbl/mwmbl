import define from "../../utils/define.js";


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
