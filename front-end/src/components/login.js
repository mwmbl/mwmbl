import define from '../utils/define.js';
import config from "../../config.js";

const template = () => /*html*/`
  <form>
    <h5>Login</h5>
    <div>
      <label for="login-email-or-username">Email or Username</label>
      <div>
        <input class="form-control" type="text" id="login-email-or-username" autocomplete="email" required="" minlength="3">
      </div>
    </div>
    <div>
      <label for="login-password">Password</label>
      <div>
        <input type="password" id="login-password" autocomplete="current-password" required="" maxlength="60">
        <button type="button" disabled="" title="You will not be able to reset your password without an email.">forgot password</button>
      </div>
    </div>
    <div>
      <button class="btn btn-secondary" type="submit">Login</button>
    </div>
  </form>
`;

export default define('login', class extends HTMLElement {
  constructor() {
    super();
    this.loginForm = null;
    this.emailOrUsernameInput = null;
    this.passwordInput = null;
    this.__setup();
    this.__events();
  }

  __setup() {
    this.innerHTML = template();
    this.loginForm = this.querySelector('form');
    this.emailOrUsernameInput = this.querySelector('#login-email-or-username');
    this.passwordInput = this.querySelector('#login-password');
  }

  __events() {
    this.loginForm.addEventListener('submit', (e) => {
      e.preventDefault();
      this.__handleLogin(e);
    });
  }

  __handleLogin = async () => {
    const response = await fetch(`${config.publicApiURL}user/login`, {
        method: 'POST',
        cache: 'no-cache',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          "username_or_email": this.emailOrUsernameInput.value,
          "password": this.passwordInput.value,
        })
      });
    if (response.status === 200) {
      const loginData = await response.json();
      console.log("Login data", loginData);
      document.cookie = `jwt=${loginData["jwt"]}; SameSite=Strict`;
      console.log("Login success");
    } else {
      console.log("Login error", response);
    }
  }
});