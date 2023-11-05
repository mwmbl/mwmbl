import define from '../../utils/define.js';
import config from '../../../config.js';
import { globalBus } from '../../utils/events.js';
import debounce from '../../utils/debounce.js'

const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion)').matches;

const template = () => /*html*/`
  <form class="search-bar">
    <i class="ph-magnifying-glass-bold"></i>
    <input 
      type='search' 
      class='search-bar-input' 
      placeholder='Search on mwmbl...' 
      title='Use "CTRL+K" or "/" to focus.'
      autocomplete='off'
    >
  </form>
`;

export default define('search-bar', class extends HTMLElement {
  constructor() {
    super();
    this.searchInput = null;
    this.searchForm = null;
    this.abortController = new AbortController();
    this.__setup();
  }

  __setup() {
    this.innerHTML = template();
    this.searchInput = this.querySelector('input');
    this.searchForm = this.querySelector('form');
    this.__events();
  }

  __dispatchSearch({ results = null, error = null }) {
    const searchEvent = new CustomEvent('search', {
      detail: {
        results,
        error,
      },
    });
    globalBus.dispatch(searchEvent)
  }

  /**
   * Updates the overall layout of the page.
   *
   * `home` centers the search bar on the page.
   * `compact` raises it to the top and makes room for displaying results.
   *
   * @param {'compact' | 'home'} mode
   * @return {void}
   */
  __setDisplayMode(mode) {
    switch (mode) {
      case 'compact': {
        document.body.style.paddingTop = '25px';
        document.querySelector('.search-menu').classList.add('compact');
        break;
      }
      case 'home': {
        document.body.style.paddingTop = '30vh';
        document.querySelector('.search-menu').classList.remove('compact');
        break;
      }
    }
  }

  async __executeSearch() {
    this.abortController.abort();
    this.abortController = new AbortController();
    // Get response from API
    const response = await fetch(`${config.publicApiURL}search?s=${encodeURIComponent(this.searchInput.value)}`, {
      signal: this.abortController.signal
    });
    // Getting results from API
    const search = await (response).json();
    return search;
  }

  __handleSearch = async () => {
    // Update page title
    document.title = `MWMBL - ${this.searchInput.value || "Search"}`;

    // Update query params
    const queryParams = new URLSearchParams(document.location.search);
    // Sets query param if search value is not empty
    if (this.searchInput.value) queryParams.set(config.searchQueryParam, this.searchInput.value);
    else queryParams.delete(config.searchQueryParam);
    // New URL with query params
    const newURL = 
      document.location.protocol 
      + "//" 
      + document.location.host 
      + document.location.pathname 
      + (this.searchInput.value ? '?' : '')
      + queryParams.toString();
    // Replace history state
    window.history.replaceState({ path: newURL }, '', newURL);

    if (this.searchInput.value) {
      this.__setDisplayMode('compact')

      try {
        const search = await this.__executeSearch()
        // This is a guess at an explanation
        // Check the searcInput.value before setting the results to prevent
        // race condition where the user has cleared the search input after
        // submitting an original search but before the search results have
        // come back from the API
        this.__dispatchSearch({ results: this.searchInput.value ? search : null });
      }
      catch(error) {
        this.__dispatchSearch({ error })
      }
    }
    else {
      this.__setDisplayMode('home')
      this.__dispatchSearch({ results: null });
    }
  }

  __events() {
    /**
     * Always add the submit event, it makes things feel faster if
     * someone does not prefer reduced motion and reflexively hits
     * return once they've finished typing.
     */
    this.searchForm.addEventListener('submit', (e) => {
      e.preventDefault();
      this.__handleSearch(e);
    });

    /**
     * Only add the "real time" search behavior when the client does
     * not prefer reduced motion; this prevents the page from changing
     * while the user is still typing their query.
     */
    if (!prefersReducedMotion) {
      this.searchInput.addEventListener('input', debounce(this.__handleSearch, 500))
    }

    // Focus search bar when pressing `ctrl + k` or `/`
    document.addEventListener('keydown', (e) => {
      if ((e.key === 'k' && e.ctrlKey) || e.key === '/' || e.key === 'Escape') {
        e.preventDefault();
        this.searchInput.focus();
      }
    });

    // Focus first result when pressing down arrow
    this.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown' && this.searchInput.value) {
        e.preventDefault();
        const focusResultEvent = new CustomEvent('focus-result');
        globalBus.dispatch(focusResultEvent);
      }
    });

    globalBus.on('focus-search', (e) => {
      this.searchInput.focus();
    });
  }

  connectedCallback() {
    // Focus search input when component is connected
    this.searchInput.focus();

    const searchQuery = new URLSearchParams(document.location.search).get(config.searchQueryParam);
    this.searchInput.value = searchQuery;
    /**
     * Trigger search handling to coordinate the value pulled from the query string
     * across the rest of the UI and to actually retrieve the results if the search
     * value is now non-empty.
     */
    this.__handleSearch();
  }
});
