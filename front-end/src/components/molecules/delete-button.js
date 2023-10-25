import define from "../../utils/define.js";
import {globalBus} from "../../utils/events.js";


export default define('delete-button', class extends HTMLButtonElement {
  constructor() {
    super();
    this.__setup();
  }

  __setup() {
    this.__events();
  }

  __events() {
    this.addEventListener('click', (e) => {
      console.log("Delete button");

      const result = this.closest('.result');
      const parent = result.parentNode;

      const index = Array.prototype.indexOf.call(parent.children, result);
      console.log("Delete index", index);

      const beginCuratingEvent = new CustomEvent('curate-delete-result', {
        detail: {
          data: {
            delete_index: index
          }
        }
      });
      globalBus.dispatch(beginCuratingEvent);
    })
  }
}, { extends: 'button' });
