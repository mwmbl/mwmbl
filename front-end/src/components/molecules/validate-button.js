import define from "../../utils/define.js";
import {globalBus} from "../../utils/events.js";


const VALIDATED_CLASS = "validated";

export default define('validate-button', class extends HTMLButtonElement {
  constructor() {
    super();
    this.__setup();
  }

  __setup() {
    this.__events();
  }

  __events() {
    this.addEventListener('click', (e) => {
      console.log("Validate button");

      const result = this.closest('.result');
      const parent = result.parentNode;

      const index = Array.prototype.indexOf.call(parent.children, result);
      console.log("Validate index", index);

      const curationValidateEvent = new CustomEvent('curate-validate-result', {
        detail: {
          data: {
            validate_index: index
          }
        }
      });
      globalBus.dispatch(curationValidateEvent);
    })
  }

  isValidated() {
    return this.classList.contains(VALIDATED_CLASS);
  }

  validate() {
    this.classList.add(VALIDATED_CLASS);
  }

  unvalidate() {
    this.classList.remove(VALIDATED_CLASS);
  }

  toggleValidate() {
    this.classList.toggle(VALIDATED_CLASS);
  }
}, { extends: 'button' });
