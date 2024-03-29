/**
 * This file is made for tweaking parameters on the front-end
 * without having to dive in the source code.
 * 
 * THIS IS NOT A PLACE TO PUT SENSIBLE DATA LIKE API KEYS.
 * THIS FILE IS PUBLIC.
 */

export default {
  componentPrefix: 'mwmbl',
  publicApiURL: '/api/v1/',
  // publicApiURL: 'http://localhost:5000/',
  searchQueryParam: 'q',
  commands: {
    'go: ': 'https://',
    'search: google.com ': 'https://www.google.com/search?q=',
  }
}
