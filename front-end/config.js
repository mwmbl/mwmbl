/**
 * This file is made for tweaking parameters on the front-end
 * without having to dive in the source code.
 * 
 * THIS IS NOT A PLACE TO PUT SENSIBLE DATA LIKE API KEYS.
 * THIS FILE IS PUBLIC.
 */

export default {
  componentPrefix: 'mwmbl',
  publicApiURL: 'https://api.mwmbl.org/',
  // publicApiURL: 'http://localhost:5000/',
  searchQueryParam: 'q',
  footerLinks: [
    {
      name: 'Github',
      icon: 'ph-github-logo-bold',
      href: 'https://github.com/mwmbl/mwmbl'
    },
    {
      name: 'Wiki',
      icon: 'ph-info-bold',
      href: 'https://github.com/mwmbl/mwmbl/wiki'
    }
  ],
  commands: {
    'go: ': 'https://',
    'search: google.com ': 'https://www.google.com/search?q=',
  }
}
