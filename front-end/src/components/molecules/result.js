import define from '../../utils/define.js';
import escapeString from '../../utils/escapeString.js';
import { globalBus } from '../../utils/events.js';


export default define('result', class extends HTMLLIElement {
  constructor() {
    super();
    this.classList.add('result');
    this.__setup();
  }

  __setup() {
    this.__events();
  }

  __events() {
    this.addEventListener('keydown', (e) => {
      if (this.firstElementChild === document.activeElement) {
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          this?.nextElementSibling?.firstElementChild.focus();
        }
        if (e.key === 'ArrowUp') {
          e.preventDefault();
          if (this.previousElementSibling)
            this.previousElementSibling?.firstElementChild.focus();
          else {
            const focusSearchEvent = new CustomEvent('focus-search');
            globalBus.dispatch(focusSearchEvent);
          }
        }
      }
    })
  }

  __handleBold(input) {
    let text = '';
    for (const part of input) {
      if (part.is_bold) text += `<strong>${escapeString(part.value)}</strong>`;
      else text += escapeString(part.value);
    }
    return text;
  }
}, { extends: 'li' });