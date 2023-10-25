import define from '../utils/define.js';
import config from "../../config.js";

const template = () => /*html*/`
  <form>
    <h5>Register</h5>
    <div>
      <label for="register-email">Email</label>
      <div>
        <input class="form-control" type="text" id="register-email" autocomplete="email" required="" minlength="3">
      </div>
      <label for="register-username">Username</label>
      <div>
        <input class="form-control" type="text" id="register-username" autocomplete="username" required="" minlength="3">
      </div>
    </div>
    <div>
      <label for="register-password">Password</label>
      <div>
        <input type="password" id="register-password" autocomplete="password" required="" maxlength="60">
      </div>
    </div>
    <div>
      <label for="register-password">Confirm password</label>
      <div>
        <input type="password" id="register-password-verify" autocomplete="confirm password" required="" maxlength="60">
      </div>
    </div>
    <div>
      <button class="btn btn-secondary" type="submit">Register</button>
    </div>
  </form>
`;

export default define('register', class extends HTMLElement {
  constructor() {
    super();
    this.registerForm = null;
    this.emailInput = null;
    this.usernameInput = null;
    this.passwordInput = null;
    this.passwordVerifyInput = null;
    this.__setup();
    this.__events();
  }

  __setup() {
    this.innerHTML = template();
    this.registerForm = this.querySelector('form');
    this.emailInput = this.querySelector('#register-email');
    this.usernameInput = this.querySelector('#register-username');
    this.passwordInput = this.querySelector('#register-password');
    this.passwordVerifyInput = this.querySelector('#register-password-verify');
  }

  __events() {
    this.registerForm.addEventListener('submit', (e) => {
      e.preventDefault();
      this.__handleRegister(e);
    });
  }

  __handleRegister = async () => {
    const response = await fetch(`${config.publicApiURL}user/register`, {
        method: 'POST',
        cache: 'no-cache',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          "username": this.usernameInput.value,
          "email": this.emailInput.value,
          "password": this.passwordInput.value,
          "password_verify": this.passwordVerifyInput.value,
        })
      });
    if (response.status === 200) {
      const registerData = await response.json();
      console.log("Register data", registerData);
      document.cookie = `jwt=${registerData["jwt"]}; SameSite=Strict`;
      console.log("Register success");
    } else {
      console.log("Register error", response);
    }
  }
});