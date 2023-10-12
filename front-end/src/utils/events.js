/**
 * A class destined to be used as an event bus.
 * 
 * It is simply a trick using a div element
 * to carry events.
 */
class Bus {
  constructor() {
    this.element = document.createElement('div');
  }

  on(eventName, callback) {
    this.element.addEventListener(eventName, callback);
  }

  dispatch(event) {
    this.element.dispatchEvent(event);
  }
}

/**
 * A global event bus that can be used to
 * dispatch events in between components
 * */
const globalBus = new Bus();

export {
  Bus,
  globalBus,
}