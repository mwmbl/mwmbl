import define from '../../utils/define.js';
import {globalBus} from "../../utils/events.js";
import config from "../../../config.js";


const CURATION_KEY_PREFIX = "curation-";
const CURATION_URL = config.publicApiURL + "user/curation/";


const template = () => /*html*/`
  <span>🖫</span>
`;


export default define('save', class extends HTMLLIElement {
  constructor() {
    super();
    this.currentCurationId = null;
    this.classList.add('save');
    this.sendId = 0;
    this.sending = false;
    this.__setup();
  }

  __setup() {
    this.innerHTML = template();
    this.__events();
    // TODO: figure out when to call __sendToApi()
    // setInterval(this.__sendToApi.bind(this), 1000);
  }

  __events() {
    globalBus.on('save-curation', (e) => {
      // We might not be online, or logged in, so save the curation in local storage in case:
      console.log("Curation event", e);
      this.__setCuration(e.detail);
      this.__sendToApi();
    });
  }

  __setCuration(curation) {
    this.sendId += 1;
    const key = CURATION_KEY_PREFIX + this.sendId;
    localStorage.setItem(key, JSON.stringify(curation));
  }

  __getOldestCurationKey() {
    let oldestId = Number.MAX_SAFE_INTEGER;
    let oldestKey = null;
    for (let i=0; i<localStorage.length; ++i) {
      const key = localStorage.key(i);
      if (key.startsWith(CURATION_KEY_PREFIX)) {
        const timestamp = parseInt(key.substring(CURATION_KEY_PREFIX.length));
        if (timestamp < oldestId) {
          oldestKey = key;
          oldestId = timestamp;
        }
      }
    }
    return oldestKey;
  }

  async __sendToApi() {
    if (this.sending) {
      return;
    }
    this.sending = true;
    const auth = document.cookie
      .split('; ')
      .find((row) => row.startsWith('jwt='))
      ?.split('=')[1];

    if (!auth) {
      console.log("No auth");
      return;
    }

    if (localStorage.length > 0) {
      const key = this.__getOldestCurationKey();
      const value = JSON.parse(localStorage.getItem(key));
      console.log("Value", value);
      const url = CURATION_URL + value['type'];

      let data = value['data'];
      if (value.type !== 'begin') {
        if (this.currentCurationId === null) {
          throw ReferenceError("No current curation found");
        }
        data['curation_id'] = this.currentCurationId;
      }
      data['auth'] = auth;

      console.log("Data", data);
      const response = await fetch(url, {
          method: 'POST',
          cache: 'no-cache',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(data),
        });

      console.log("Save curation API response", response);

      if (response.status === 200) {
        localStorage.removeItem(key);
      } else {
        console.log("Bad response, skipping");
        return;
      }

      const responseData = await response.json();
      console.log("Response data", responseData);
      if (responseData["curation_id"]) {
        this.currentCurationId = responseData["curation_id"];
      }

      // There may be more to send, wait a second and see
      setTimeout(this.__sendToApi.bind(this), 1000);
    }
    this.sending = false;
  }
}, { extends: 'li' });

